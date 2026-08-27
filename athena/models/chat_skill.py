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

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import chat_reference_files as reference_files
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

# Reference files. The mechanism itself lives in
# models/chat_reference_files.py — shared, since the charter grew the
# same one (2026-08-27). The caps are RE-EXPORTED because they are part
# of THIS module's documented surface: skill_form.html mirrors them in
# maxlength attributes.
#
# They are pinned by the `_enforce_request_size` ceiling on the skill
# form POST. Realistic French text urlencodes at ~1.3 bytes/char, but a
# run of accented characters costs SIX (`é` -> `%C3%A9`) and an em dash
# NINE — so 6 x 40 000 content chars + the 30 000-char body = 270 000
# chars is ~354 KB realistic and **1.55 MiB at a true x6**, over the
# 1 MB cap, which aborts 413 into a raw error page and loses everything
# typed. The form is therefore multipart, where the same payload stays
# ~297 KB whatever the alphabet. (The previous note here read « ~816 KB
# pessimistic » under a « x6 » heading — a x3 figure; the arithmetic did
# not survive its own statement.) Raising a cap means redoing this,
# never editing the number.
FILES_SUBCOLLECTION = reference_files.SUBCOLLECTION
FILE_NAME_MAX_LENGTH = reference_files.FILE_NAME_MAX_LENGTH
FILE_DESCRIPTION_MAX_LENGTH = reference_files.FILE_DESCRIPTION_MAX_LENGTH
FILE_MAX_CHARS = reference_files.FILE_MAX_CHARS
MAX_FILES = reference_files.MAX_FILES

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
    entries, file_errors = reference_files.validate_files(files_raw)
    errors.extend(file_errors)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    skill_id = str(uuid.uuid4())
    merged.update(
        {
            "id": skill_id,
            "files": reference_files.manifest(entries),
            "active": bool(merged.get("active", True)),
            "current_version": 1,
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )
    try:
        batch = db.batch()
        for sha, payload in reference_files.content_writes(entries, now).items():
            batch.set(reference_files.file_ref(db, COLLECTION, skill_id, sha), payload)
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
        entries, file_errors = reference_files.validate_files(files)
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
            for sha, payload in reference_files.content_writes(entries, now).items():
                txn.set(reference_files.file_ref(db, COLLECTION, skill_id, sha), payload)
            head["files"] = reference_files.manifest(entries)
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
    """The UI seam (form seeding + detail view) — see
    ``chat_reference_files.list_contents``: fails OPEN per file, so a
    missing content doc never breaks the page."""
    return reference_files.list_contents(db, COLLECTION, skill_id, manifest)


def get_version_file(
    skill_id: str, version: int, filename: str
) -> tuple[Optional[str], Optional[str]]:
    """The executor seam: the file's content AT A PINNED VERSION.

    This module owns resolving WHICH manifest — a keyed get of
    ``versions/{n:06d}``, never a re-read head; the shared half then
    matches the name case-insensitively and reads the content doc.
    """
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
    rows = (vsnap.to_dict() or {}).get("files") or []
    if not rows:
        return None, "Cette compétence n'a aucun fichier de référence."
    return reference_files.read_from_manifest(db, COLLECTION, skill_id, rows, filename)


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
