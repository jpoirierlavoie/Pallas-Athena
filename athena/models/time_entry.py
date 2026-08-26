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
from utils import phases
from utils.logging_setup import log_unexpected, sanitize_log_value

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

# Phase-of-litigation vocabulary (Phase O, axis 1) — lives in utils/phases.py,
# NOT here (the taxonomie.py pattern: one place to edit; mcp/tools.py derives
# its enums from the same pure module).
VALID_PHASES = phases.VALID_PHASES
VALID_SOUS_PHASES = phases.VALID_SOUS_PHASES
PHASE_LABELS = phases.PHASE_LABELS
SOUS_PHASE_LABELS = phases.SOUS_PHASE_LABELS


def _default_doc() -> dict:
    """Return a dict with every time entry field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "date": None,
        "description": "",
        # Phase O — "" = non renseignée (legacy docs are never backfilled)
        "phase": "",
        "sous_phase": "",
        "hours": 0.0,
        "rate": 0,          # cents
        "amount": 0,        # cents (computed: hours * rate)
        "billable": True,
        "invoiced": False,
        "invoice_id": None,
        # Identifiant de l'enregistrement dans le système d'origine,
        # posé par la reprise historique (août 2026) ; « » partout
        # ailleurs. C'est l'ancre anti-doublon DURABLE : la clé
        # d'idempotence MCP expire en 24 h et une reprise s'étale sur
        # des jours, donc seul un identifiant porté par la donnée
        # elle-même permet à une reprise interrompue de retrouver ce
        # qu'elle a déjà écrit. Jamais sérialisé en vCard ni en iCal.
        "legacy_ref": "",
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


def _compute_entry_amount(hours: float, rate: int, billable: bool) -> int:
    """Return the calculated cost in cents for a time entry.

    Unbillable time carries no billable value: when *billable* is false the
    calculated cost is always 0, regardless of hours × rate. Zeroing it at
    write time keeps the stored amount, the /temps list totals, the CSV/PDF
    exports and the on-screen display consistent — and, together with the
    ``billable == True`` filter on :func:`get_unbilled_totals`, keeps
    unbillable time out of the dashboard's unbilled tracker entirely.
    """
    if not billable:
        return 0
    return _compute_amount(hours, rate)


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

    errors.extend(phases.validate_pair(data))

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_time_entry(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    merged["amount"] = _compute_entry_amount(
        merged.get("hours", 0), merged.get("rate", 0), bool(merged.get("billable"))
    )
    phases.apply_sous_phase_default(merged)

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
    except Exception:
        log_unexpected("time entry write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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

    The refusal is ENFORCED here, not only documented (audit 2026-08-26):
    every failure path on this collection swallows FAILED_PRECONDITION into
    an empty list or a zero total, so a third caller that skipped the
    routing guard would render a silently empty billing list with a $0
    total — the June 2026 incident's shape. Raising is the honest failure.
    """
    if dossier_id and billable_filter:
        raise ValueError(
            "combinaison dossier_id + billable_filter non indexée — "
            "passer par list_time_entries (balayage legacy)"
        )
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
    merged["amount"] = _compute_entry_amount(
        merged.get("hours", 0), merged.get("rate", 0), bool(merged.get("billable"))
    )
    phases.apply_sous_phase_default(merged)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(entry_id).set(merged)
    except Exception:
        log_unexpected("time entry write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def get_time_entries_bulk(entry_ids: list[str]) -> dict[str, dict]:
    """Fetch many time entries in ONE round-trip. Returns {id: doc}.

    Mirrors ``models.dossier.get_dossiers_bulk`` / ``partie.get_parties_bulk``
    — document-ID lookups need no index. **Unlike those two, this one fails
    CLOSED**: it propagates. Its caller is the bulk phase reclassification, a
    WRITE path, and a read failure degraded to ``{}`` there would report every
    single item « introuvable » — a batch of fabricated refusals the caller
    would take at face value. Same reasoning as ``folder.subtree_members``
    (fails closed) against ``folder.list_documents`` (fails open): a list on a
    screen may degrade, a write's view of the world may not.
    """
    unique_ids = [e for e in dict.fromkeys(entry_ids) if e]
    if not unique_ids:
        return {}
    refs = [db.collection(COLLECTION).document(eid) for eid in unique_ids]
    return {
        snap.id: snap.to_dict() for snap in db.get_all(refs) if snap.exists
    }


def set_time_entry_phase(
    entry_id: str, phase: str, sous_phase: str
) -> tuple[Optional[dict], list[str], bool]:
    """Reclassify a time entry's litigation phase — INVOICED OR NOT.

    The ONE writer allowed past the ``invoiced`` wall, and the reason it is
    allowed is that the wall protects the invoice's MONEY figures and the
    phase is not one of them: ``phase``/``sous_phase`` appear on no invoice,
    no ``lineitems`` record, no gabarit placeholder and no DAV serializer —
    they feed the budget's ``aggregate_actuals`` and the list badges, and
    nothing else. ``invoiced`` is therefore never consulted here.

    What keeps that claim true is the WRITE SHAPE, not a promise: a partial
    ``update()`` of exactly four keys, never the merged full-document
    ``set()`` that :func:`update_time_entry` performs. The function is
    structurally incapable of moving hours, rate, amount or description, and
    a test pins the key set. Never "simplify" this into ``update_time_entry``
    with a relaxed guard — that guard's refusal is what makes the connector's
    ``update_time_entry`` output schema (« invoiced: always false ») true.

    Validation is ``phases.validate_pair`` ALONE, deliberately not the
    module's ``_validate``: a legacy row with a blank description or zero
    hours must stay reclassifiable, and none of those fields is written.

    Returns ``(doc, errors, changed)``. The third member deviates from the
    house ``(doc, errors)`` convention on purpose (the
    ``folder.delete_folder`` precedent): the bulk caller must report
    « applied » apart from « already carried that code », and deriving that
    in each caller is how two callers drift. An unchanged pair writes
    NOTHING — no updated_at churn, no etag churn — which is what makes a
    reclassification pass replayable (the ``complete_task`` doctrine).
    """
    existing = get_time_entry(entry_id)
    if not existing:
        return None, ["Entrée de temps introuvable."], False

    resolved, sous = phases.resolve_pair(phase, sous_phase)
    pair = {"phase": resolved, "sous_phase": sous}
    # Validate FIRST: an unknown sub-code leaves the parent underived, and
    # reporting that as « phase requise » would send the caller to fix the
    # wrong half.
    errors = phases.validate_pair(pair)
    if errors:
        return None, errors, False
    if not pair["phase"]:
        # Reclassifying means ASSIGNING a code. « Hors phase » (HOR) is the
        # vocabulary's own answer for unclassifiable work — blanking is a
        # regression this path deliberately cannot perform.
        return None, ["Une phase du litige est requise."], False

    if (existing.get("phase", ""), existing.get("sous_phase", "")) == (
        pair["phase"], pair["sous_phase"]
    ):
        return existing, [], False

    now = datetime.now(timezone.utc)
    etag = str(uuid.uuid4())
    try:
        db.collection(COLLECTION).document(entry_id).update({
            "phase": pair["phase"],
            "sous_phase": pair["sous_phase"],
            "updated_at": now,
            "etag": etag,
        })
    except Exception:
        log_unexpected("time entry phase write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], False

    return {**existing, **pair, "updated_at": now, "etag": etag}, [], True


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
    except Exception:
        log_unexpected("time entry delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


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
