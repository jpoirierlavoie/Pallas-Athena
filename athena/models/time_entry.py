"""Time entry Firestore CRUD and summary functions."""

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
    product = hours * rate
    # Guard against NaN/Infinity (int(round(...)) would raise or corrupt totals)
    if not math.isfinite(product):
        return 0
    return int(round(product))


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette entrée de temps.")

    if not data.get("date"):
        errors.append("La date est requise.")

    if not data.get("description", "").strip():
        errors.append("La description est requise.")

    # math.isfinite rejects NaN/Infinity (NaN passes "<= 0" comparisons)
    hours = data.get("hours", 0)
    if not isinstance(hours, (int, float)) or not math.isfinite(hours) or hours <= 0:
        errors.append("Le nombre d'heures doit être supérieur à zéro.")

    rate = data.get("rate", 0)
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
        errors.append("Le taux horaire ne peut pas être négatif.")

    amount = data.get("amount", 0)
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
        errors.append("Le montant calculé est invalide.")

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
        logger.warning("get_time_entry failed for %s: %s", sanitize_log_value(entry_id), exc)
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


def _filtered_query(
    dossier_id: Optional[str],
    billable_filter: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> "firestore.Query":
    """Build the filtered, (date DESC, id DESC)-ordered time entry query.

    Shared by :func:`list_time_entries_page` and
    :func:`get_filtered_time_totals` so the exact same composite index serves
    both the page reads and the totals aggregation. The ``date`` range
    filters ride the primary order field, so they need no extra index
    dimension. Note: ``dossier_id`` and ``billable_filter`` combined is NOT
    supported server-side (each pairing would need its own composite index);
    callers route that rare combination through the legacy
    :func:`list_time_entries` full scan.
    """
    query = db.collection(COLLECTION)
    if dossier_id:
        query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))
    if billable_filter == "billable":
        query = query.where(filter=FieldFilter("billable", "==", True))
    elif billable_filter == "non_facture":
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


def list_time_entries_page(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of time entries plus an opaque next-page cursor.

    Firestore-native cursor pagination: ``order_by(date DESC, id DESC)``
    with ``start_after`` — reads ~``limit`` docs per page instead of
    streaming the whole collection (the ``id`` field mirrors the document ID
    and is always set, giving a total order for ties on ``date``).
    ``list_time_entries`` remains the full-scan path for exports, summaries
    and the dossier_id + billable_filter combination.
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
        logger.warning("list_time_entries_page: paginated query failed: %s", exc)
        return [], None


def get_filtered_time_totals(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """Return ``{"hours": float, "amount": int}`` over the list-view filters.

    Server-side aggregation (two SUMs in one RunAggregationQuery) replacing
    the legacy "materialize everything, sum in Python" totals on the /temps/
    list. Built on the same ordered query as :func:`list_time_entries_page`,
    but the aggregation needs its own composite index per filter —
    (filter, date DESC, id DESC, amount DESC, hours DESC): the SUM fields
    must trail the index in alphabetical order, directions matching the
    sort. Returns safe zeros on failure — a broken total must never break
    the list view.
    """
    try:
        query = _filtered_query(dossier_id, billable_filter, date_from, date_to)
        agg_query = query.sum("hours", alias="hours").sum("amount", alias="amount")
        values = _aggregation_values(agg_query.get())
        hours = float(values.get("hours", 0) or 0)
        amount = values.get("amount", 0) or 0
        return {"hours": round(hours, 1), "amount": int(round(amount))}
    except Exception as exc:
        logger.warning("get_filtered_time_totals: aggregation failed: %s", exc)
        return {"hours": 0.0, "amount": 0}


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


# Shared implementation lives in models/__init__.py; aliased so this module's
# helpers (and their tests) keep a stable local name.
_aggregation_values = aggregation_values


def get_unbilled_totals() -> dict:
    """Return unbilled billable totals across all dossiers via aggregation.

    Issues a single server-side Firestore aggregation query (two SUMs in one
    request) over ``billable == True AND invoiced == False`` instead of
    materializing the whole collection — O(1) payload for the dashboard.
    Requires the ``timeentries`` composite index
    (billable ASC, invoiced ASC, amount ASC, hours ASC) — Firestore matches
    aggregations only when the aggregated fields trail the index in
    alphabetical order; see ``firestore.indexes.json``.

    Returns ``{"hours": float, "amount": int}`` with hours rounded to one
    decimal (matching the dashboard's historical display) and amount in
    integer cents. On any failure, returns safe zeros — a failed stat must
    never break the dashboard.
    """
    try:
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("billable", "==", True))
            .where(filter=FieldFilter("invoiced", "==", False))
        )
        # Both SUMs ride in one aggregation query — google-cloud-firestore
        # 2.27 supports multiple aggregations per RunAggregationQuery.
        agg_query = query.sum("hours", alias="hours").sum("amount", alias="amount")
        values = _aggregation_values(agg_query.get())
        hours = float(values.get("hours", 0) or 0)
        amount = values.get("amount", 0) or 0
        return {"hours": round(hours, 1), "amount": int(round(amount))}
    except Exception as exc:
        logger.warning("get_unbilled_totals: aggregation query failed: %s", exc)
        return {"hours": 0.0, "amount": 0}


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


def mark_time_entries_invoiced(entry_ids: list[str], invoice_id: str) -> list[str]:
    """Update time entries as invoiced. Returns the IDs that failed to update.

    Note: invoice creation no longer uses this helper — it flips sources
    inside its own transaction. Kept for callers needing a standalone flip.
    """
    now = datetime.now(timezone.utc)
    failed_ids: list[str] = []
    for eid in entry_ids:
        try:
            db.collection(COLLECTION).document(eid).update({
                "invoiced": True,
                "invoice_id": invoice_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception as exc:
            logger.warning(
                "mark_time_entries_invoiced failed for %s: %s",
                sanitize_log_value(eid), exc,
            )
            failed_ids.append(eid)
    return failed_ids
