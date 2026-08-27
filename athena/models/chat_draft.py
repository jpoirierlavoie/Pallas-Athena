"""Brouillons de rédaction versionnés (Phase N) — the « rédaction »
deliverable of the chat client.

Append-only versioning, the budget/trust doctrine applied to text: a draft
is a head document (``chat_drafts/{id}``, Rule 7 complete — it IS mutated:
the head content moves) plus an append-only ``versions/{n:06d}``
subcollection (write-once, no ``etag`` — the audit_events exception,
documented in CLAUDE.md Rule 7). « Modifier » means « appendre la version
n+1 et déplacer la tête » ; every prior version stays readable for ever.

**No delete function exists in this module, at all** (SPEC §1 — pinned by
``tests/test_chat_draft.py``). Erasure is the out-of-band Loi-25 procedure.

Provenance travels through :data:`PROVENANCE` — a ``ContextVar``, never
schema arguments (model-supplied provenance would be forgeable, and the
write tools' ``additionalProperties: false`` would refuse it anyway). The
chat executor sets it around handler execution with a token-based reset;
absent, the write is attributed to the external connector. Keys are
whitelisted (:data:`_PROVENANCE_KEYS`) so junk can never ride in.

The dossier binding is IMMUTABLE after creation (accepted default,
2026-08-26): ``revise_draft`` has no dossier argument — a moved draft would
vanish from the dossier view its provenance cites. ``dossier_id == ""``
means a floating draft (the notes convention).

The content cap deliberately equals ``utils.markdown_docx.MAX_MARKDOWN_CHARS``
(120 000): every storable draft is thereby guaranteed convertible by
« Verser en Word » (the H.3 pipeline), and the head copy stays ~240 KB
UTF-8 worst case — far under the Firestore 1 MiB document limit.
"""

import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models import db
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils.markdown_docx import MAX_MARKDOWN_CHARS

logger = logging.getLogger(__name__)

COLLECTION = "chat_drafts"
VERSIONS_SUBCOLLECTION = "versions"

TITLE_MAX_LENGTH = 200
CONTENT_MAX_LENGTH = MAX_MARKDOWN_CHARS  # 120 000 — the « versable » guarantee
_FIELD_MAX_LENGTH = 2000

# Provenance seam. The dict set here is stamped VERBATIM (whitelisted) on
# the version being written. created_via ∈ {"chat", "connector",
# "scheduled"}; everything else is nullable.
PROVENANCE: ContextVar[Optional[dict]] = ContextVar(
    "chat_draft_provenance", default=None
)

_PROVENANCE_KEYS = (
    "created_via",
    "conversation_id",
    "turn_id",
    "model",
    "skill_versions",
    "charter_version",
    # Le numéro seul ne dit pas SOUS QUOI le brouillon a été rédigé :
    # « 1 » vaut à la fois « tour d'avant le lot », « amorçage » et
    # « repli ». La provenance porte donc les deux.
    "charter_source",
)


def _provenance() -> dict:
    """The whitelisted provenance for the write being performed."""
    raw = PROVENANCE.get() or {}
    out = {key: raw.get(key) for key in _PROVENANCE_KEYS}
    out["created_via"] = str(raw.get("created_via") or "connector")
    return out


def _default_doc() -> dict:
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "content": "",
        "content_length": 0,
        "current_version": 0,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            if key == "content":
                limit = CONTENT_MAX_LENGTH
            elif key == "title":
                limit = TITLE_MAX_LENGTH
            else:
                limit = _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    if not data.get("title", "").strip():
        errors.append("Le titre du brouillon est requis.")
    if not data.get("content", "").strip():
        errors.append("Le contenu du brouillon est requis.")
    return errors


def validate_payload(data: dict) -> list[str]:
    """Validation without any write — the handlers' dry_run seam."""
    return _validate({**_default_doc(), **_sanitize_data(data)})


def _version_ref(draft_id: str, version: int):
    return (
        db.collection(COLLECTION)
        .document(draft_id)
        .collection(VERSIONS_SUBCOLLECTION)
        .document(f"{version:06d}")
    )


def _version_doc(head: dict, now: datetime) -> dict:
    return {
        "version": int(head["current_version"]),
        "title": head["title"],
        "content": head["content"],
        "content_length": int(head["content_length"]),
        "created_at": now,
        "provenance": _provenance(),
    }


# ── CRUD (create / revise / read — no delete, by design) ────────────────────


def create_draft(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create a draft at version 1. Returns (head_doc, errors).

    Atomic: the head and version 1 commit in one batch — a head whose
    ``versions`` subcollection is empty would break the append-only
    invariant every reader assumes. The caller (handler/route) resolves and
    denormalizes the dossier BEFORE calling, and refuses an unresolvable
    id rather than blanking it (the create_note doctrine).
    """
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    draft_id = str(uuid.uuid4())  # a caller-supplied id is never honoured
    merged.update(
        {
            "id": draft_id,
            "content_length": len(merged["content"]),
            "current_version": 1,
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )

    try:
        batch = db.batch()
        batch.set(db.collection(COLLECTION).document(draft_id), merged)
        batch.set(_version_ref(draft_id, 1), _version_doc(merged, now))
        batch.commit()
    except Exception:
        log_unexpected("chat draft create failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def revise_draft(
    draft_id: str,
    *,
    content: str,
    title: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Append version n+1 and move the head — transactionally.

    The read-head → write-version → update-head sequence runs in ONE
    Firestore transaction: two concurrent revisions serialize through the
    transaction retry instead of both writing « n+1 ». An unknown id is a
    refusal, never a create — a mistyped id must not fork a new draft.
    """
    clean = _sanitize_data({"content": content or ""})
    payload_errors: list[str] = []
    if not clean["content"].strip():
        payload_errors.append("Le contenu du brouillon est requis.")
    new_title = None
    if title is not None:
        new_title = _sanitize_data({"title": title})["title"]
        if not new_title.strip():
            payload_errors.append("Le titre du brouillon ne peut pas être vide.")
    if payload_errors:
        return None, payload_errors

    ref = db.collection(COLLECTION).document(draft_id)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> tuple[Optional[dict], list[str]]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None, ["Brouillon introuvable."]
        head = snap.to_dict()
        now = datetime.now(timezone.utc)
        head["content"] = clean["content"]
        head["content_length"] = len(clean["content"])
        if new_title is not None:
            head["title"] = new_title
        head["current_version"] = int(head.get("current_version") or 0) + 1
        head["updated_at"] = now
        head["etag"] = str(uuid.uuid4())
        txn.set(
            _version_ref(draft_id, head["current_version"]),
            _version_doc(head, now),
        )
        txn.update(
            ref,
            {
                "content": head["content"],
                "content_length": head["content_length"],
                "title": head["title"],
                "current_version": head["current_version"],
                "updated_at": now,
                "etag": head["etag"],
            },
        )
        return head, []

    try:
        return _txn(transaction)
    except Exception:
        log_unexpected("chat draft revise failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]


def get_draft(draft_id: str) -> Optional[dict]:
    try:
        doc = db.collection(COLLECTION).document(draft_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "get_draft failed for %s: %s", sanitize_log_value(draft_id), exc
        )
    return None


def list_drafts(
    dossier_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Bounded global read, newest-updated first; Python-filtered.

    Deliberately NO ``where(dossier_id ==)`` + ``order_by`` combination —
    that pair needs a composite index, and the collection is small enough
    that the bounded global read + Python filter is the house answer (the
    scheduled-tasks / conversation-list doctrine). Fails open to ``[]`` —
    a display list, never a destructive decision input.
    """
    try:
        query = (
            db.collection(COLLECTION)
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        rows = [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("list_drafts failed", exc_info=True)
        return []
    if dossier_id is not None:
        rows = [r for r in rows if r.get("dossier_id", "") == dossier_id]
    return rows


def list_versions(draft_id: str, limit: int = 50) -> list[dict]:
    """A draft's version history, newest first. Fails open to ``[]``."""
    try:
        query = (
            db.collection(COLLECTION)
            .document(draft_id)
            .collection(VERSIONS_SUBCOLLECTION)
            .order_by("version", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning(
            "list_versions failed for %s", sanitize_log_value(draft_id)
        )
        return []


def get_version(draft_id: str, version: int) -> Optional[dict]:
    try:
        snap = _version_ref(draft_id, int(version)).get()
        if snap.exists:
            return snap.to_dict()
    except Exception as exc:
        logger.warning(
            "get_version failed for %s: %s", sanitize_log_value(draft_id), exc
        )
    return None
