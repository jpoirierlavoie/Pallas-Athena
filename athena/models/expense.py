"""Expense Firestore CRUD and summary functions."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

# Firestore collection path
COLLECTION = "expenses"

# Valid expense categories
VALID_CATEGORIES = (
    "signification",
    "expertise",
    "transcription",
    "deplacement",
    "photocopie",
    "timbre_judiciaire",
    "autre",
)

# Display labels (French)
CATEGORY_LABELS = {
    "signification": "Signification",
    "expertise": "Expertise",
    "transcription": "Transcription",
    "deplacement": "Déplacement",
    "photocopie": "Photocopie",
    "timbre_judiciaire": "Timbre judiciaire",
    "autre": "Autre",
}


def _default_doc() -> dict:
    """Return a dict with every expense field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "date": None,
        "description": "",
        "category": "autre",
        "amount": 0,          # cents
        "taxable": True,
        "receipt_document_id": None,
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


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette dépense.")

    if not data.get("date"):
        errors.append("La date est requise.")

    if not data.get("description", "").strip():
        errors.append("La description est requise.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    amount = data.get("amount", 0)
    if not isinstance(amount, (int, float)) or amount <= 0:
        errors.append("Le montant doit être supérieur à zéro.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_expense(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    expense_id = str(uuid.uuid4())

    merged.update({
        "id": expense_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    })

    try:
        db.collection(COLLECTION).document(expense_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_expense(expense_id: str) -> Optional[dict]:
    """Fetch a single expense by ID."""
    try:
        doc = db.collection(COLLECTION).document(expense_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


def list_expenses(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return expenses, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if billable_filter == "non_facture":
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


def update_expense(
    expense_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing expense. Returns (updated_doc, errors)."""
    existing = get_expense(expense_id)
    if not existing:
        return None, ["Dépense introuvable."]

    if existing.get("invoiced"):
        return None, ["Impossible de modifier une dépense déjà facturée."]

    merged = {**existing, **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(expense_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_expense(expense_id: str) -> tuple[bool, str]:
    """Delete an expense. Returns (success, error_message)."""
    existing = get_expense(expense_id)
    if not existing:
        return False, "Dépense introuvable."

    if existing.get("invoiced"):
        return False, "Impossible de supprimer une dépense déjà facturée."

    try:
        db.collection(COLLECTION).document(expense_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


# ── Summary & batch operations ────────────────────────────────────────────


def get_expense_summary(dossier_id: str) -> dict:
    """Return totals for a dossier: total_expenses, unbilled_expenses."""
    entries = list_expenses(dossier_id=dossier_id)
    total = 0
    unbilled = 0

    for e in entries:
        amt = e.get("amount", 0)
        total += amt
        if not e.get("invoiced"):
            unbilled += amt

    return {
        "total_expenses": total,
        "unbilled_expenses": unbilled,
    }


def get_unbilled_expenses(dossier_id: str) -> list[dict]:
    """Return expenses not yet invoiced for a dossier."""
    entries = list_expenses(dossier_id=dossier_id)
    return [e for e in entries if not e.get("invoiced")]


def mark_expenses_invoiced(expense_ids: list[str], invoice_id: str) -> None:
    """Batch update expenses as invoiced."""
    now = datetime.now(timezone.utc)
    for eid in expense_ids:
        try:
            db.collection(COLLECTION).document(eid).update({
                "invoiced": True,
                "invoice_id": invoice_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception:
            pass
