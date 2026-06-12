"""Expense Firestore CRUD and summary functions."""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, db
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils.logging_setup import sanitize_log_value

logger = logging.getLogger(__name__)

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

    # math.isfinite rejects NaN/Infinity (NaN passes "<= 0" comparisons)
    amount = data.get("amount", 0)
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
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
    except Exception as exc:
        logger.warning("get_expense failed for %s: %s", sanitize_log_value(expense_id), exc)
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


def _filtered_query(
    dossier_id: Optional[str],
    billable_filter: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> "firestore.Query":
    """Build the filtered, (date DESC, id DESC)-ordered expense query.

    Shared by :func:`list_expenses_page` and
    :func:`get_filtered_expense_totals` so the exact same composite index
    serves both the page reads and the totals aggregation. The ``date``
    range filters ride the primary order field (no extra index dimension).
    Only ``non_facture`` is meaningful for expenses (mirrors
    :func:`list_expenses`); the dossier_id + billable_filter combination is
    NOT supported server-side — callers route it through the legacy
    :func:`list_expenses` full scan.
    """
    query = db.collection(COLLECTION)
    if dossier_id:
        query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))
    if billable_filter == "non_facture":
        query = query.where(filter=FieldFilter("invoiced", "==", False))
    if date_from:
        query = query.where(filter=FieldFilter("date", ">=", date_from))
    if date_to:
        query = query.where(filter=FieldFilter("date", "<=", date_to))
    return (
        query
        .order_by("date", direction=firestore.Query.DESCENDING)
        .order_by("id", direction=firestore.Query.DESCENDING)
    )


def list_expenses_page(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of expenses plus an opaque next-page cursor.

    Firestore-native cursor pagination: ``order_by(date DESC, id DESC)``
    with ``start_after`` — reads ~``limit`` docs per page instead of
    streaming the whole collection (the ``id`` field mirrors the document ID
    and is always set, giving a total order for ties on ``date``).
    ``list_expenses`` remains the full-scan path for exports, summaries and
    the dossier_id + billable_filter combination.
    """
    try:
        query = _filtered_query(dossier_id, billable_filter, date_from, date_to)
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            # decode_cursor preserves encode order: [date, id]
            query = query.start_after({"date": values[0], "id": values[1]})
        docs = [d.to_dict() for d in query.limit(limit + 1).stream()]
        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("date"), last.get("id")])
        return docs, next_cursor
    except Exception as exc:
        # PII-free: log the exception only, never filter values or doc content.
        logger.warning("list_expenses_page: paginated query failed: %s", exc)
        return [], None


def get_filtered_expense_totals(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """Return ``{"amount": int}`` over the list-view filters.

    Server-side SUM aggregation replacing the legacy "materialize
    everything, sum in Python" total on the /temps/ dépenses tab. Built on
    the same ordered query as :func:`list_expenses_page`, but the
    aggregation needs its own composite index per filter — (filter,
    date DESC, id DESC, amount DESC): the SUM field must trail the index,
    direction matching the sort. Returns a safe zero on failure — a broken
    total must never break the list view.
    """
    try:
        query = _filtered_query(dossier_id, billable_filter, date_from, date_to)
        agg_query = query.sum("amount", alias="amount")
        values = aggregation_values(agg_query.get())
        amount = values.get("amount", 0) or 0
        return {"amount": int(round(amount))}
    except Exception as exc:
        logger.warning("get_filtered_expense_totals: aggregation failed: %s", exc)
        return {"amount": 0}


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


def mark_expenses_invoiced(expense_ids: list[str], invoice_id: str) -> list[str]:
    """Update expenses as invoiced. Returns the IDs that failed to update.

    Note: invoice creation no longer uses this helper — it flips sources
    inside its own transaction. Kept for callers needing a standalone flip.
    """
    now = datetime.now(timezone.utc)
    failed_ids: list[str] = []
    for eid in expense_ids:
        try:
            db.collection(COLLECTION).document(eid).update({
                "invoiced": True,
                "invoice_id": invoice_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception as exc:
            logger.warning(
                "mark_expenses_invoiced failed for %s: %s",
                sanitize_log_value(eid), exc,
            )
            failed_ids.append(eid)
    return failed_ids
