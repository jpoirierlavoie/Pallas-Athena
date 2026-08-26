"""Compétences du clavardage (Phase N) — runtime-managed skills.

Skills are DATA, not code (SPEC §5): the hot-editable layer above the
source-controlled charter. Same append-only versioning as
``models/chat_draft.py`` — a head document (``chat_skills/{id}``, Rule 7
complete) plus an append-only ``versions/{n:06d}`` subcollection (Rule-7
exception: write-once, no etag). « Modifier » = append version n+1 and move
the head. **Deactivation exists; deletion does not** — no delete function
in this module, pinned by test.

The version binding is head-at-each-turn (FLAG 4): the turn engine reads
the head at assembly time and records the exact ``(skill_id, version)``
pairs on the turn, so the registre shows precisely which text governed
which output — a mid-conversation edit takes effect on the NEXT turn.

Reference files (2026-08-26 — the Claude Code skill model): the body stays
the short always-loaded part; FILES are read on demand by the model via the
chat-local ``get_skill_file`` tool. Storage is content-addressed:
``chat_skills/{id}/fichiers/{sha256}`` holds ``{content, chars, created_at}``
write-once (re-referencing the same content re-sets a byte-identical doc —
only ``created_at`` is best-effort metadata), and every VERSION doc carries
the manifest ``files: [{name, description, sha256, chars}]``, mirrored on
the head (the ``body`` head-copy motif). Removing a file = a new version
without it; prior versions keep their manifests for ever. File CONTENT is
deliberately NOT passed through ``security.sanitize`` — its tag-stripping
regex would mutilate reference material — which is safe because the content
is only ever rendered under Jinja autoescape and returned as tool_result
text, never executed; the only cleaning is C0-control stripping (``\\n`` and
``\\t`` kept, so ``\\r`` disappears and CRLF pastes normalize to LF, keeping
the sha stable across OSes).
"""

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import db
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

COLLECTION = "chat_skills"
VERSIONS_SUBCOLLECTION = "versions"

NAME_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 500
# Skill bodies enter the system prompt of every turn that selects them —
# a generous but bounded ceiling (well under the notes cap; a 30k-char
# skill is already ~7k tokens of every prompt).
BODY_MAX_LENGTH = 30_000
_FIELD_MAX_LENGTH = 2000

# Reference files. The caps are pinned by the 1 MB `_enforce_request_size`
# ceiling on the urlencoded skill form POST (default service): realistic
# French text urlencodes at ≈1.3 bytes/char (worst-case accented runs ×6),
# so 6 × 40 000 content chars + the 30 000-char body ≈ 354 KB realistic /
# ~816 KB pessimistic — inside the cap with margin, where the sketch's
# 10 × 100 000 was NOT. Raising either cap requires redoing that arithmetic
# (or moving the form to multipart). 40 000 chars is also ~10-13k tokens per
# on-demand read — a sane per-read prompt cost.
FILES_SUBCOLLECTION = "fichiers"
FILE_NAME_MAX_LENGTH = 80
FILE_DESCRIPTION_MAX_LENGTH = 200
FILE_MAX_CHARS = 40_000
MAX_FILES = 6

# C0 controls except \t (\x09) and \n (\x0a), plus DEL — the ONLY cleaning
# file content receives (see the module docstring's sanitize deviation).
_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _default_doc() -> dict:
    return {
        "id": "",
        "name": "",
        "description": "",
        "body": "",                # head copy — one read serves assembly
        "files": [],               # manifest head copy (never the contents)
        "active": True,
        "current_version": 0,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            if key == "body":
                limit = BODY_MAX_LENGTH
            elif key == "name":
                limit = NAME_MAX_LENGTH
            elif key == "description":
                limit = DESCRIPTION_MAX_LENGTH
            else:
                limit = _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    if not data.get("name", "").strip():
        errors.append("Le nom de la compétence est requis.")
    if not data.get("body", "").strip():
        errors.append("Le contenu de la compétence est requis.")
    return errors


def _clean_file_content(content: str) -> str:
    """VERBATIM except C0 controls (\\t and \\n kept) — never sanitize()."""
    return _C0_RE.sub("", content)


def _format_int_fr(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _validate_files(files: object) -> tuple[list[dict], list[str]]:
    """Normalize the submitted file rows → (entries, French errors).

    Entries carry ``{name, description, content, sha256, chars}``; the
    manifest strips ``content`` before anything persists on head/version.
    Over-long content is REFUSED, never truncated (a silently shortened
    reference file is worse than an error the user can fix in place).
    """
    if files is None:
        return [], []
    if not isinstance(files, list):
        return [], ["Le format des fichiers de référence est invalide."]
    errors: list[str] = []
    entries: list[dict] = []
    if len(files) > MAX_FILES:
        errors.append(
            f"Au plus {MAX_FILES} fichiers de référence par compétence."
        )
    seen: set[str] = set()
    for position, raw in enumerate(files, start=1):
        if not isinstance(raw, dict):
            errors.append("Le format des fichiers de référence est invalide.")
            continue
        name = sanitize(
            str(raw.get("name", "")), max_length=FILE_NAME_MAX_LENGTH
        ).strip()
        description = sanitize(
            str(raw.get("description", "")),
            max_length=FILE_DESCRIPTION_MAX_LENGTH,
        ).strip()
        content = _clean_file_content(str(raw.get("content", "")))
        if not name:
            errors.append(
                f"Le fichier de référence n° {position} doit porter un nom."
            )
            continue
        if name.casefold() in seen:
            errors.append(
                "Deux fichiers de référence portent le même nom : "
                f"« {name} »."
            )
            continue
        seen.add(name.casefold())
        if not content.strip():
            errors.append(f"Le fichier « {name} » est vide.")
            continue
        if len(content) > FILE_MAX_CHARS:
            errors.append(
                f"Le fichier « {name} » dépasse "
                f"{_format_int_fr(FILE_MAX_CHARS)} caractères "
                f"({_format_int_fr(len(content))})."
            )
            continue
        entries.append(
            {
                "name": name,
                "description": description,
                "content": content,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "chars": len(content),
            }
        )
    return entries, errors


def _manifest(entries: list[dict]) -> list[dict]:
    """The persisted shape — NEVER carries content (the 1 MiB/doc guard)."""
    return [
        {
            "name": e["name"],
            "description": e["description"],
            "sha256": e["sha256"],
            "chars": e["chars"],
        }
        for e in entries
    ]


def _content_writes(entries: list[dict], now: datetime) -> dict[str, dict]:
    """sha-deduped {sha: payload} — Firestore refuses writing the SAME doc
    twice in one transaction, and two files with identical content share a
    sha (legal: they collapse to one content doc; names stay distinct in
    the manifest)."""
    writes: dict[str, dict] = {}
    for entry in entries:
        writes[entry["sha256"]] = {
            "content": entry["content"],
            "chars": entry["chars"],
            "created_at": now,
        }
    return writes


def _file_ref(skill_id: str, sha256: str):
    return (
        db.collection(COLLECTION)
        .document(skill_id)
        .collection(FILES_SUBCOLLECTION)
        .document(sha256)
    )


def _version_ref(skill_id: str, version: int):
    return (
        db.collection(COLLECTION)
        .document(skill_id)
        .collection(VERSIONS_SUBCOLLECTION)
        .document(f"{version:06d}")
    )


def _version_doc(head: dict, now: datetime) -> dict:
    return {
        "version": int(head["current_version"]),
        "name": head["name"],
        "body": head["body"],
        "files": list(head.get("files") or []),
        "created_at": now,
    }


def create_skill(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create a skill at version 1 (head + version + content docs, one
    atomic batch). ``data["files"]`` is popped BEFORE ``_sanitize_data`` —
    the raw rows carry content, which must never reach the head document."""
    data = dict(data or {})
    files_raw = data.pop("files", None)
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    entries, file_errors = _validate_files(files_raw)
    errors.extend(file_errors)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    skill_id = str(uuid.uuid4())
    merged.update(
        {
            "id": skill_id,
            "files": _manifest(entries),
            "active": bool(merged.get("active", True)),
            "current_version": 1,
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )
    try:
        batch = db.batch()
        for sha, payload in _content_writes(entries, now).items():
            batch.set(_file_ref(skill_id, sha), payload)
        batch.set(db.collection(COLLECTION).document(skill_id), merged)
        batch.set(_version_ref(skill_id, 1), _version_doc(merged, now))
        batch.commit()
    except Exception:
        log_unexpected("chat skill create failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def revise_skill(
    skill_id: str,
    *,
    body: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    files: Optional[list] = None,
) -> tuple[Optional[dict], list[str]]:
    """Append version n+1 and move the head — transactionally.

    ``files=None`` keeps the current manifest (no content writes); a list
    (even ``[]``) REPLACES it — prior versions keep their own manifests,
    and content docs are never removed (append-only doctrine). Validation
    happens BEFORE the transaction; inside it: one head read, then writes
    only (sha-deduped content docs, version, head)."""
    clean = _sanitize_data({"body": body or ""})
    payload_errors: list[str] = []
    if not clean["body"].strip():
        payload_errors.append("Le contenu de la compétence est requis.")
    new_name = None
    if name is not None:
        new_name = _sanitize_data({"name": name})["name"]
        if not new_name.strip():
            payload_errors.append("Le nom de la compétence ne peut pas être vide.")
    new_description = (
        _sanitize_data({"description": description})["description"]
        if description is not None
        else None
    )
    entries: Optional[list[dict]] = None
    if files is not None:
        entries, file_errors = _validate_files(files)
        payload_errors.extend(file_errors)
    if payload_errors:
        return None, payload_errors

    ref = db.collection(COLLECTION).document(skill_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[Optional[dict], list[str]]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None, ["Compétence introuvable."]
        head = snap.to_dict()
        now = datetime.now(timezone.utc)
        head["body"] = clean["body"]
        if new_name is not None:
            head["name"] = new_name
        if new_description is not None:
            head["description"] = new_description
        if entries is not None:
            for sha, payload in _content_writes(entries, now).items():
                txn.set(_file_ref(skill_id, sha), payload)
            head["files"] = _manifest(entries)
        else:
            head["files"] = list(head.get("files") or [])
        head["current_version"] = int(head.get("current_version") or 0) + 1
        head["updated_at"] = now
        head["etag"] = str(uuid.uuid4())
        txn.set(
            _version_ref(skill_id, head["current_version"]),
            _version_doc(head, now),
        )
        txn.update(
            ref,
            {
                "body": head["body"],
                "name": head["name"],
                "description": head["description"],
                "files": head["files"],
                "current_version": head["current_version"],
                "updated_at": now,
                "etag": head["etag"],
            },
        )
        return head, []

    try:
        return _txn(transaction)
    except Exception:
        log_unexpected("chat skill revise failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]


def set_active(skill_id: str, active: bool) -> tuple[Optional[dict], list[str]]:
    """Activate/deactivate — the ONLY lifecycle verb (deletion by design
    does not exist)."""
    ref = db.collection(COLLECTION).document(skill_id)
    try:
        snap = ref.get()
        if not snap.exists:
            return None, ["Compétence introuvable."]
        now = datetime.now(timezone.utc)
        ref.update(
            {
                "active": bool(active),
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            }
        )
        head = snap.to_dict()
        head.update({"active": bool(active), "updated_at": now})
        return head, []
    except Exception:
        log_unexpected("chat skill toggle failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]


def get_skill(skill_id: str) -> Optional[dict]:
    try:
        doc = db.collection(COLLECTION).document(skill_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "get_skill failed for %s: %s", sanitize_log_value(skill_id), exc
        )
    return None


def list_file_contents(skill_id: str, manifest: list[dict]) -> list[dict]:
    """The UI seam (form seeding + detail view): manifest entries with
    their content attached. Fails OPEN per file — a missing or unreadable
    content doc yields ``content: ""`` + ``missing: True`` rather than
    breaking the page (the manifest still tells the truth about what the
    version references)."""
    rows: list[dict] = []
    for entry in manifest or []:
        row = {
            "name": entry.get("name", ""),
            "description": entry.get("description", ""),
            "sha256": entry.get("sha256", ""),
            "chars": int(entry.get("chars") or 0),
            "content": "",
            "missing": True,
        }
        try:
            snap = _file_ref(skill_id, row["sha256"]).get()
            if snap.exists:
                row["content"] = (snap.to_dict() or {}).get("content", "")
                row["missing"] = False
        except Exception as exc:
            logger.warning(
                "skill file read failed for %s: %s",
                sanitize_log_value(skill_id),
                exc,
            )
        rows.append(row)
    return rows


def get_version_file(
    skill_id: str, version: int, filename: str
) -> tuple[Optional[str], Optional[str]]:
    """The executor seam: the file's content AT A PINNED VERSION.

    Resolution: keyed get of ``versions/{n:06d}`` → case-folded name match
    in that version's manifest → keyed get of the content doc. Returns
    ``(content, None)`` on success, ``(None, reason_fr)`` otherwise — the
    unknown-name reason LISTS the available names (non-privileged metadata
    already in the prompt) so the model can self-correct."""
    wanted = str(filename or "").strip()
    try:
        vsnap = _version_ref(skill_id, int(version)).get()
    except Exception as exc:
        logger.warning(
            "get_version_file failed for %s: %s",
            sanitize_log_value(skill_id),
            exc,
        )
        return None, (
            "Erreur de lecture du fichier de référence. Veuillez réessayer."
        )
    if not vsnap.exists:
        return None, "Version de compétence introuvable."
    manifest = (vsnap.to_dict() or {}).get("files") or []
    if not manifest:
        return None, "Cette compétence n'a aucun fichier de référence."
    match = next(
        (
            e
            for e in manifest
            if str(e.get("name", "")).casefold() == wanted.casefold()
        ),
        None,
    )
    if match is None:
        names = ", ".join(str(e.get("name", "")) for e in manifest)
        return None, f"Fichier inconnu. Fichiers disponibles : {names}."
    try:
        csnap = _file_ref(skill_id, str(match.get("sha256", ""))).get()
    except Exception as exc:
        logger.warning(
            "get_version_file content read failed for %s: %s",
            sanitize_log_value(skill_id),
            exc,
        )
        return None, (
            "Erreur de lecture du fichier de référence. Veuillez réessayer."
        )
    if not csnap.exists:
        # The manifest references a content doc that is absent — storage
        # incoherence worth a loud line (content docs are write-once and
        # nothing removes them).
        log_unexpected("chat skill file content missing", exc_info=False)
        return None, "Fichier illisible (incohérence de stockage)."
    return (csnap.to_dict() or {}).get("content", ""), None


def list_skills(limit: int = 200) -> list[dict]:
    """All skills, name-ordered — a small bounded collection (no index)."""
    try:
        query = (
            db.collection(COLLECTION)
            .order_by("name")
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("list_skills failed", exc_info=True)
        return []


def get_heads(skill_ids: list[str]) -> list[dict]:
    """The head docs of a selection, ACTIVE only, in the order given.

    The turn engine's assembly seam. Fails OPEN per skill (a missing or
    unreadable skill is skipped): a turn must degrade to fewer skills, not
    refuse to run — the recorded ``(skill_id, version)`` pairs show exactly
    what governed the output either way.
    """
    heads: list[dict] = []
    for skill_id in skill_ids or []:
        head = get_skill(skill_id)
        if head and head.get("active") and head.get("body", "").strip():
            heads.append(head)
    return heads


def list_versions(skill_id: str, limit: int = 50) -> list[dict]:
    try:
        query = (
            db.collection(COLLECTION)
            .document(skill_id)
            .collection(VERSIONS_SUBCOLLECTION)
            .order_by("version", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning(
            "list_versions failed for %s", sanitize_log_value(skill_id)
        )
        return []


def get_version(skill_id: str, version: int) -> Optional[dict]:
    try:
        snap = _version_ref(skill_id, int(version)).get()
        if snap.exists:
            return snap.to_dict()
    except Exception as exc:
        logger.warning(
            "get_version failed for %s: %s", sanitize_log_value(skill_id), exc
        )
    return None
