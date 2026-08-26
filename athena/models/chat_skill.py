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
"""

import logging
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


def _default_doc() -> dict:
    return {
        "id": "",
        "name": "",
        "description": "",
        "body": "",                # head copy — one read serves assembly
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
        "created_at": now,
    }


def create_skill(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create a skill at version 1 (head + version, one atomic batch)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    skill_id = str(uuid.uuid4())
    merged.update(
        {
            "id": skill_id,
            "active": bool(merged.get("active", True)),
            "current_version": 1,
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )
    try:
        batch = db.batch()
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
) -> tuple[Optional[dict], list[str]]:
    """Append version n+1 and move the head — transactionally."""
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
