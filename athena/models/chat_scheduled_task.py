"""Tâches planifiées du clavardage (Phase N §12) — runtime-editable.

A scheduled run is an ORDINARY conversation whose first turn is initiated
by cron. Definitions live here (Rule 7 complete — edited through the UI);
the due computation and the dispatcher live in ``chat/planification.py``.

**Deletion does not exist** (SPEC §12.1 — activate/deactivate only, the
skills philosophy). Recurrence is a CLOSED three-word vocabulary — never
cron expressions. Occurrence identity is keyed on the LOCAL Montréal date
(``occurrences["YYYY-MM-DD"]``), which is what makes DST a non-event.

:func:`dispatch_occurrence` is the §12.2 idempotency CAS **and** the whole
dispatch in ONE transaction: it re-checks ``active`` and the occurrence's
absence, then writes the conversation, its two turns, and the occurrence
marker atomically — a duplicate cron delivery dispatches nothing twice,
and a crash can never leave a marked occurrence without its conversation
(the repair pass then only ever deals with un-ENQUEUED chains, never
half-created ones).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from config import Config
from models import db
from models.chat_conversation import (
    COLLECTION as CONV_COLLECTION,
    TURNS_SUBCOLLECTION,
)
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

COLLECTION = "chat_scheduled_tasks"

VALID_RECURRENCES = ("quotidien", "jours_ouvrables", "hebdomadaire")
RECURRENCE_LABELS = {
    "quotidien": "Quotidien",
    "jours_ouvrables": "Jours ouvrables",
    "hebdomadaire": "Hebdomadaire",
}
# ISO weekday labels for the hebdomadaire day picker (0 = lundi, Python
# date.weekday() convention).
WEEKDAY_LABELS = (
    "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche",
)

NAME_MAX_LENGTH = 120
PROMPT_MAX_LENGTH = 20_000
_FIELD_MAX_LENGTH = 2000

_ZERO_TOTALS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "web_search_requests": 0,
    "model_calls": 0,
}


def _default_doc() -> dict:
    return {
        "id": "",
        "name": "",
        "prompt": "",
        "model": Config.CHAT_DEFAULT_MODEL,   # FLAG 12 — Sonnet by default
        "skill_selection": [],
        "recurrence": {"kind": "quotidien", "day": 0},
        "hour_local": 7,
        "dossier_id": "",                     # default floating (§12.1)
        "dossier_file_number": "",
        "dossier_title": "",
        "deliver_email": False,               # FLAG 13 — default off
        "active": True,
        "last_occurrence": "",
        "occurrences": {},                    # {"YYYY-MM-DD": {...}} — the CAS
        "usage_totals": dict(_ZERO_TOTALS),
        "usd_micros_total": 0,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            if key == "prompt":
                limit = PROMPT_MAX_LENGTH
            elif key == "name":
                limit = NAME_MAX_LENGTH
            else:
                limit = _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    if not data.get("name", "").strip():
        errors.append("Le nom de la tâche est requis.")
    if not data.get("prompt", "").strip():
        errors.append("La consigne (prompt) est requise.")
    if data.get("model") not in Config.CHAT_MODELS:
        errors.append("Modèle inconnu — l'allowlist est fermée.")
    recurrence = data.get("recurrence") or {}
    kind = recurrence.get("kind", "")
    if kind not in VALID_RECURRENCES:
        errors.append("Récurrence invalide (quotidien, jours_ouvrables, hebdomadaire).")
    if kind == "hebdomadaire":
        day = recurrence.get("day")
        if not isinstance(day, int) or not 0 <= day <= 6:
            errors.append("Jour de la semaine invalide (0 = lundi … 6 = dimanche).")
    hour = data.get("hour_local")
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        errors.append("Heure invalide (0 à 23, heure de Montréal).")
    return errors


def create_task(data: dict) -> tuple[Optional[dict], list[str]]:
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    task_id = str(uuid.uuid4())
    merged.update(
        {
            "id": task_id,
            "skill_selection": [str(s) for s in (merged.get("skill_selection") or [])],
            "active": bool(merged.get("active", True)),
            "deliver_email": bool(merged.get("deliver_email", False)),
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )
    try:
        db.collection(COLLECTION).document(task_id).set(merged)
    except Exception:
        log_unexpected("chat scheduled task create failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


# The editable surface. Occurrence/usage bookkeeping is NEVER form-writable.
_EDITABLE_FIELDS = (
    "name", "prompt", "model", "skill_selection", "recurrence",
    "hour_local", "dossier_id", "dossier_file_number", "dossier_title",
    "deliver_email",
)


def update_task(task_id: str, data: dict) -> tuple[Optional[dict], list[str]]:
    """Edit a definition — effective at the NEXT occurrence (§12.1)."""
    existing = get_task(task_id)
    if existing is None:
        return None, ["Tâche introuvable."]
    clean = _sanitize_data(data)
    merged = {
        **existing,
        **{k: clean[k] for k in _EDITABLE_FIELDS if k in clean},
    }
    errors = _validate(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())
    try:
        db.collection(COLLECTION).document(task_id).update(
            {
                **{k: merged[k] for k in _EDITABLE_FIELDS},
                "updated_at": now,
                "etag": merged["etag"],
            }
        )
    except Exception:
        log_unexpected("chat scheduled task update failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def set_active(task_id: str, active: bool) -> tuple[Optional[dict], list[str]]:
    existing = get_task(task_id)
    if existing is None:
        return None, ["Tâche introuvable."]
    now = datetime.now(timezone.utc)
    try:
        db.collection(COLLECTION).document(task_id).update(
            {"active": bool(active), "updated_at": now, "etag": str(uuid.uuid4())}
        )
    except Exception:
        log_unexpected("chat scheduled task toggle failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    existing.update({"active": bool(active), "updated_at": now})
    return existing, []


def get_task(task_id: str) -> Optional[dict]:
    try:
        doc = db.collection(COLLECTION).document(task_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "get_task failed for %s: %s", sanitize_log_value(task_id), exc
        )
    return None


def list_tasks(limit: int = 200) -> list[dict]:
    """All definitions, name-ordered — tiny bounded collection, no index.
    Fails open (a display list; the DISPATCHER uses list_active)."""
    try:
        query = (
            db.collection(COLLECTION).order_by("name").limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("list_tasks failed", exc_info=True)
        return []


def list_active(limit: int = 200) -> list[dict]:
    """Active definitions for the dispatcher — single-field equality
    (auto-indexed) + the bound; due-filtering happens in Python (house
    doctrine: a tiny collection never earns a composite index)."""
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("active", "==", True))
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() for snap in query.stream()]
    except Exception:
        logger.warning("list_active failed", exc_info=True)
        return []


def dispatch_occurrence(
    task_id: str,
    occurrence: str,
    *,
    conversation: dict,
    user_turn: dict,
    assistant_turn: dict,
) -> str:
    """The §12.2 dispatch — ONE transaction, idempotent by construction.

    Returns ``"dispatched"`` | ``"already"`` (the CAS observed the marked
    occurrence — a duplicate cron delivery, or a replayed sweep) |
    ``"inactive"`` | ``"missing"``. The three docs are the PREPARED output
    of models/chat_conversation.prepare_conversation/prepare_turn_pair —
    the shapes live in one place.
    """
    task_ref = db.collection(COLLECTION).document(task_id)
    conv_ref = db.collection(CONV_COLLECTION).document(conversation["id"])
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn) -> str:
        snap = task_ref.get(transaction=txn)
        if not snap.exists:
            return "missing"
        task = snap.to_dict()
        if not task.get("active"):
            return "inactive"
        occurrences = dict(task.get("occurrences") or {})
        if occurrence in occurrences:
            return "already"
        now = datetime.now(timezone.utc)
        occurrences[occurrence] = {
            "conversation_id": conversation["id"],
            "dispatched_at": now,
        }
        txn.set(conv_ref, conversation)
        turns = conv_ref.collection(TURNS_SUBCOLLECTION)
        txn.set(turns.document(user_turn["id"]), user_turn)
        txn.set(turns.document(assistant_turn["id"]), assistant_turn)
        txn.update(
            task_ref,
            {
                "occurrences": occurrences,
                "last_occurrence": occurrence,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            },
        )
        return "dispatched"

    return _txn(transaction)
