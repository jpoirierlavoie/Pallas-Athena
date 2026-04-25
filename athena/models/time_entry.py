"""Time entry Firestore CRUD and summary functions."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "timeentries"

# Quick-select description chips (French)
QUICK_DESCRIPTIONS = (
    "Appel téléphonique",
    "Correspondance",
    "Rédaction",
    "Recherche juridique",
    "Audience",
    "Révision",
    "Rencontre client",
    "Préparation",
    "Déplacement",
    "Négociation",
)


def _default_doc() -> dict:
    """Return a dict with every time entry field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "date": None,
        "description": "",
        "hours": 0.0,
        "rate": 0,          # cents
        "amount": 0,        # cents (computed: hours * rate)
        "billable": True,
        "invoiced": False,
        "invoice_id": None,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _compute_amount(hours: float, rate: int) -> int:
    """Compute amount in cents from hours and rate (cents)."""
    return int(round(hours * rate))


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette entrée de temps.")

    if not data.get("date"):
        errors.append("La date est requise.")

    if not data.get("description", "").strip():
        errors.append("La description est requise.")

    hours = data.get("hours", 0)
    if not isinstance(hours, (int, float)) or hours <= 0:
        errors.append("Le nombre d'heures doit être supérieur à zéro.")

    rate = data.get("rate", 0)
    if not isinstance(rate, (int, float)) or rate < 0:
        errors.append("Le taux horaire ne peut pas être négatif.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_time_entry(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    merged["amount"] = _compute_amount(merged.get("hours", 0), merged.get("rate", 0))

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    entry_id = str(uuid.uuid4())

    merged.update({
        "id": entry_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    })

    try:
        db.collection(COLLECTION).document(entry_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_time_entry(entry_id: str) -> Optional[dict]:
    """Fetch a single time entry by ID."""
    try:
        doc = db.collection(COLLECTION).document(entry_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_time_entry failed for %s: %s", entry_id, exc)
    return None


def list_time_entries(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return time entries, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters (Firestore single-field index limitation)
        if billable_filter == "billable":
            results = [r for r in results if r.get("billable")]
        elif billable_filter == "non_facture":
            results = [r for r in results if not r.get("invoiced")]

        if date_from:
            results = [r for r in results if r.get("date") and r["date"] >= date_from]
        if date_to:
            results = [r for r in results if r.get("date") and r["date"] <= date_to]

        # Sort by date descending
        results.sort(
            key=lambda e: e.get("date") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        return results
    except Exception:
        return []


def update_time_entry(
    entry_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing time entry. Returns (updated_doc, errors)."""
    existing = get_time_entry(entry_id)
    if not existing:
        return None, ["Entrée de temps introuvable."]

    if existing.get("invoiced"):
        return None, ["Impossible de modifier une entrée déjà facturée."]

    merged = {**existing, **_sanitize_data(data)}
    merged["amount"] = _compute_amount(merged.get("hours", 0), merged.get("rate", 0))

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(entry_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_time_entry(entry_id: str) -> tuple[bool, str]:
    """Delete a time entry. Returns (success, error_message)."""
    existing = get_time_entry(entry_id)
    if not existing:
        return False, "Entrée de temps introuvable."

    if existing.get("invoiced"):
        return False, "Impossible de supprimer une entrée déjà facturée."

    try:
        db.collection(COLLECTION).document(entry_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


# ── Summary & batch operations ────────────────────────────────────────────


def get_time_summary(dossier_id: str) -> dict:
    """Return totals for a dossier: total_hours, total_billable_amount, unbilled_hours, unbilled_amount."""
    entries = list_time_entries(dossier_id=dossier_id)
    total_hours = 0.0
    total_billable_amount = 0
    unbilled_hours = 0.0
    unbilled_amount = 0

    for e in entries:
        h = e.get("hours", 0)
        amt = e.get("amount", 0)
        total_hours += h
        if e.get("billable"):
            total_billable_amount += amt
        if not e.get("invoiced") and e.get("billable"):
            unbilled_hours += h
            unbilled_amount += amt

    return {
        "total_hours": round(total_hours, 1),
        "total_billable_amount": total_billable_amount,
        "unbilled_hours": round(unbilled_hours, 1),
        "unbilled_amount": unbilled_amount,
    }


def get_unbilled_time_entries(dossier_id: str) -> list[dict]:
    """Return time entries not yet invoiced for a dossier."""
    entries = list_time_entries(dossier_id=dossier_id)
    return [e for e in entries if e.get("billable") and not e.get("invoiced")]


def mark_time_entries_invoiced(entry_ids: list[str], invoice_id: str) -> None:
    """Batch update time entries as invoiced."""
    now = datetime.now(timezone.utc)
    for eid in entry_ids:
        try:
            db.collection(COLLECTION).document(eid).update({
                "invoiced": True,
                "invoice_id": invoice_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception as exc:
            logger.warning("mark_time_entries_invoiced failed for %s: %s", eid, exc)
