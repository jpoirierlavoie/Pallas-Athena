"""Invoice Firestore CRUD, tax computation, and line-item management."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, db
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils.logging_setup import sanitize_log_value

logger = logging.getLogger(__name__)


# Firestore collection paths
COLLECTION = "invoices"
LINE_ITEMS_SUB = "lineitems"
COUNTERS_COLLECTION = "counters"


class _SourceConflictError(Exception):
    """Raised when a billing source changed concurrently during invoicing."""

# Valid statuses and their French labels
VALID_STATUSES = ("brouillon", "envoyée", "payée", "en_retard", "annulée")

STATUS_LABELS = {
    "brouillon": "Brouillon",
    "envoyée": "Envoyée",
    "payée": "Payée",
    "en_retard": "En retard",
    "annulée": "Annulée",
}

# Allowed status transitions
STATUS_TRANSITIONS = {
    "brouillon": ("envoyée", "annulée"),
    "envoyée": ("payée", "en_retard", "annulée"),
    "en_retard": ("payée", "annulée"),
}

# Tax rates stored as basis points (×100 for GST, ×1000 for QST precision)
GST_RATE_BPS = 500       # 5.00%
QST_RATE_BPS = 9975      # 9.975%

DEFAULT_PAYMENT_TERMS = "Payable dans les 30 jours suivant la date de facturation."


def _default_doc() -> dict:
    """Return a dict with every invoice field set to its default value."""
    return {
        "id": "",
        "invoice_number": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "client_id": "",
        "client_name": "",
        "billing_address": {
            "name": "",
            "street": "",
            "unit": "",
            "city": "",
            "province": "QC",
            "postal_code": "",
        },
        "date": None,
        "due_date": None,
        "status": "brouillon",
        # Financials (all in cents)
        "subtotal_fees": 0,
        "subtotal_expenses": 0,
        "subtotal": 0,
        "gst_rate": GST_RATE_BPS,
        "gst_amount": 0,
        "qst_rate": QST_RATE_BPS,
        "qst_amount": 0,
        "total": 0,
        "retainer_applied": 0,
        "amount_due": 0,
        # Tax numbers (from config, snapshotted at creation)
        "gst_number": "",
        "qst_number": "",
        "notes": "",
        "payment_terms": DEFAULT_PAYMENT_TERMS,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _default_line_item() -> dict:
    """Return a dict with every line item field set to its default value."""
    return {
        "id": "",
        "type": "fee",
        "source_id": "",
        "date": None,
        "description": "",
        "hours": None,
        "rate": None,
        "amount": 0,
        "taxable": True,
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        elif isinstance(val, dict):
            out[key] = _sanitize_data(val)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette facture.")

    if not data.get("date"):
        errors.append("La date de la facture est requise.")

    return errors


# ── Tax computation ──────────────────────────────────────────────────────


def compute_totals(line_items: list[dict]) -> dict:
    """Compute subtotals, taxes, and total from a list of line items.

    All monetary values are integer cents. Uses Python Decimal for tax
    computation to avoid floating-point issues, as required by CLAUDE.md.
    """
    subtotal_fees = 0
    subtotal_expenses = 0
    taxable_amount = 0

    for item in line_items:
        amt = item.get("amount", 0)
        if item.get("type") == "fee":
            subtotal_fees += amt
        else:
            subtotal_expenses += amt
        if item.get("taxable", True):
            taxable_amount += amt

    subtotal = subtotal_fees + subtotal_expenses

    # Tax computation with Decimal (QST is NOT compounded on GST since 2013)
    taxable_dec = Decimal(taxable_amount)
    gst_amount = int(
        (taxable_dec * Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    qst_amount = int(
        (taxable_dec * Decimal("0.09975")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    total = subtotal + gst_amount + qst_amount

    return {
        "subtotal_fees": subtotal_fees,
        "subtotal_expenses": subtotal_expenses,
        "subtotal": subtotal,
        "gst_amount": gst_amount,
        "qst_amount": qst_amount,
        "total": total,
    }


# ── Invoice number generation ────────────────────────────────────────────


def _scan_max_invoice_seq(prefix: str) -> int:
    """Return the highest existing invoice sequence for *prefix* via a scan.

    Used only to seed the transactional counter the first time it is created
    for a given year, so pre-counter numbering continues without duplicates.
    """
    max_seq = 0
    for doc in db.collection(COLLECTION).stream():
        num = doc.to_dict().get("invoice_number", "")
        if num.startswith(prefix):
            try:
                max_seq = max(max_seq, int(num[len(prefix):]))
            except (ValueError, IndexError):
                continue
    return max_seq


def _generate_invoice_number() -> str:
    """Allocate the next sequential invoice number (YYYY-FNNN) atomically.

    Uses a monotonic Firestore counter (``counters/invoices-{year}``)
    incremented inside a transaction, so concurrent allocations can never
    produce the same number. On the first allocation of a year, the counter
    is seeded from the highest existing invoice number for that year.

    Any failure propagates to the caller — a number is never guessed, since
    a silent fallback could mint a colliding invoice number.
    """
    year = datetime.now(timezone.utc).strftime("%Y")
    prefix = f"{year}-F"
    counter_ref = db.collection(COUNTERS_COLLECTION).document(f"invoices-{year}")

    # Seed scan happens before the transaction (collection scans are not
    # transactional); the transaction then takes max(counter.seq, seed) so
    # existing numbering continues without duplicates.
    seed = 0
    if not counter_ref.get().exists:
        seed = _scan_max_invoice_seq(prefix)

    transaction = db.transaction()

    @firestore.transactional
    def _allocate(txn: firestore.Transaction) -> int:
        snapshot = counter_ref.get(transaction=txn)
        current = int((snapshot.to_dict() or {}).get("seq", 0)) if snapshot.exists else 0
        next_seq = max(current, seed) + 1
        txn.set(counter_ref, {
            "seq": next_seq,
            "updated_at": datetime.now(timezone.utc),
        })
        return next_seq

    return f"{prefix}{_allocate(transaction):03d}"


# ── CRUD ─────────────────────────────────────────────────────────────────


def create_invoice(
    dossier_id: str,
    selected_entry_ids: list[str],
    selected_expense_ids: list[str],
    data: dict,
) -> tuple[Optional[dict], list[str]]:
    """Create an invoice with line items from selected time entries and expenses.

    Sources that are missing, already invoiced, or that belong to another
    dossier are skipped. The invoice document, all line items, and the
    invoiced=True flips for the retained sources are committed in a single
    Firestore transaction that re-reads each source, so a concurrent
    invoicing aborts the whole creation (no orphan invoices, no
    double-billing). Returns (invoice, errors).
    """
    from models.time_entry import COLLECTION as TE_COLLECTION, get_time_entry
    from models.expense import COLLECTION as EXP_COLLECTION, get_expense

    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if not selected_entry_ids and not selected_expense_ids:
        errors.append("Sélectionnez au moins une entrée de temps ou une dépense.")
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    invoice_id = str(uuid.uuid4())

    # Build line items from selected sources. Skip any source that is
    # missing, already invoiced, or owned by a different dossier — only the
    # sources that actually become line items get flipped to invoiced.
    line_items: list[dict] = []
    valid_entry_ids: list[str] = []
    valid_expense_ids: list[str] = []
    # etag captured at pre-read time; the transaction re-checks it so a
    # concurrent content edit (hours/amount) aborts instead of snapshotting
    # a stale value into the line items.
    expected_etags: dict[str, str] = {}

    for eid in selected_entry_ids:
        entry = get_time_entry(eid)
        if (
            not entry
            or entry.get("invoiced")
            or entry.get("dossier_id") != dossier_id
        ):
            continue
        valid_entry_ids.append(eid)
        expected_etags[eid] = entry.get("etag", "")
        line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "fee",
            "source_id": eid,
            "date": entry.get("date"),
            "description": entry.get("description", ""),
            "hours": entry.get("hours", 0),
            "rate": entry.get("rate", 0),
            "amount": entry.get("amount", 0),
            "taxable": True,
        })

    for eid in selected_expense_ids:
        expense = get_expense(eid)
        if (
            not expense
            or expense.get("invoiced")
            or expense.get("dossier_id") != dossier_id
        ):
            continue
        valid_expense_ids.append(eid)
        expected_etags[eid] = expense.get("etag", "")
        line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "expense",
            "source_id": eid,
            "date": expense.get("date"),
            "description": expense.get("description", ""),
            "hours": None,
            "rate": None,
            "amount": expense.get("amount", 0),
            "taxable": expense.get("taxable", True),
        })

    if not line_items:
        return None, ["Aucune entrée valide sélectionnée."]

    # Compute totals
    totals = compute_totals(line_items)

    # Retainer must stay within [0, total] so amount_due can never go negative.
    retainer_applied = merged.get("retainer_applied", 0)
    if (
        not isinstance(retainer_applied, int)
        or isinstance(retainer_applied, bool)
        or retainer_applied < 0
        or retainer_applied > totals["total"]
    ):
        return None, [
            "La provision appliquée doit être comprise entre 0 $ et le total de la facture."
        ]

    # Allocate the invoice number via the transactional counter. Any failure
    # aborts the creation — never fall back to a guessed number.
    try:
        invoice_number = _generate_invoice_number()
    except Exception as exc:
        logger.error("create_invoice: invoice number allocation failed: %s", exc)
        return None, [
            "Impossible de générer le numéro de facture. Veuillez réessayer."
        ]

    merged.update({
        "id": invoice_id,
        "invoice_number": invoice_number,
        "dossier_id": dossier_id,
        **totals,
        "gst_rate": GST_RATE_BPS,
        "qst_rate": QST_RATE_BPS,
        "retainer_applied": retainer_applied,
        "amount_due": totals["total"] - retainer_applied,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    })

    # Set default due date if not provided
    if not merged.get("due_date") and merged.get("date"):
        merged["due_date"] = merged["date"] + timedelta(days=30)

    invoice_ref = db.collection(COLLECTION).document(invoice_id)
    source_refs = [
        db.collection(TE_COLLECTION).document(eid) for eid in valid_entry_ids
    ] + [
        db.collection(EXP_COLLECTION).document(eid) for eid in valid_expense_ids
    ]

    transaction = db.transaction()

    @firestore.transactional
    def _txn_create(txn: firestore.Transaction) -> None:
        # All reads must precede all writes in a Firestore transaction.
        # Re-read every retained source so a concurrent invoicing (or
        # deletion / dossier reassignment) aborts this creation entirely.
        for ref in source_refs:
            snap = ref.get(transaction=txn)
            src = snap.to_dict() if snap.exists else None
            if (
                not src
                or src.get("invoiced")
                or src.get("dossier_id") != dossier_id
                or src.get("etag", "") != expected_etags.get(ref.id, "")
            ):
                raise _SourceConflictError(ref.id)

        txn.set(invoice_ref, merged)
        for item in line_items:
            txn.set(
                invoice_ref.collection(LINE_ITEMS_SUB).document(item["id"]),
                item,
            )
        for ref in source_refs:
            txn.update(ref, {
                "invoiced": True,
                "invoice_id": invoice_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })

    try:
        _txn_create(transaction)
    except _SourceConflictError:
        return None, [
            "Certaines entrées sélectionnées ont été modifiées ou facturées entre-temps. Veuillez réessayer."
        ]
    except Exception as exc:
        logger.error("create_invoice: transaction failed for %s: %s", invoice_id, exc)
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_invoice(invoice_id: str) -> Optional[dict]:
    """Fetch a single invoice by ID (without line items)."""
    try:
        doc = db.collection(COLLECTION).document(invoice_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_invoice failed for %s: %s", sanitize_log_value(invoice_id), exc)
    return None


def get_invoice_with_items(invoice_id: str) -> tuple[Optional[dict], list[dict]]:
    """Fetch an invoice and all its line items. Returns (invoice, items)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return None, []

    items: list[dict] = []
    try:
        docs = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        items = [d.to_dict() for d in docs]
        # Sort by date
        items.sort(
            key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
        )
    except Exception as exc:
        logger.warning(
            "get_invoice_with_items: line items load failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )

    return invoice, items


def list_invoices(
    status_filter: Optional[str] = None,
    dossier_id: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return invoices, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if status_filter:
            results = [r for r in results if r.get("status") == status_filter]

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


def list_invoices_page(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of invoices plus an opaque cursor for the next page.

    Cursor-mode counterpart of :func:`list_invoices` for the list view:
    reads ~``limit`` documents per page (server-side filters + ``order_by``
    + ``start_after``) instead of streaming the whole collection. The
    legacy :func:`list_invoices` remains the path for exports, summaries
    and dossier-scoped views.

    The status filter is an equality and the optional date range targets
    ``date`` — the same field as the primary sort — so both composite
    indexes serve every filter combination here:
    ``(date DESC, id ASC)`` and ``(status ASC, date DESC, id ASC)``.

    Sort order: ``date DESC, id ASC``. The ``id`` field mirrors the
    document ID and is always set — a stable tiebreaker when several
    invoices share the same date.
    """
    try:
        query = db.collection(COLLECTION)

        if status_filter and status_filter in VALID_STATUSES:
            query = query.where(filter=FieldFilter("status", "==", status_filter))

        # Range filters on the primary order field need no extra index.
        if date_from:
            query = query.where(filter=FieldFilter("date", ">=", date_from))
        if date_to:
            query = query.where(filter=FieldFilter("date", "<=", date_to))

        query = query.order_by(
            "date", direction=firestore.Query.DESCENDING
        ).order_by("id")

        # decode_cursor yields values in encode order: [date, id].
        # Anything malformed (None or wrong arity) degrades to page 1.
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            query = query.start_after({"date": values[0], "id": values[1]})

        # Fetch one extra row to know whether a next page exists.
        docs = [doc.to_dict() for doc in query.limit(limit + 1).stream()]

        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("date"), last.get("id")])

        return docs, next_cursor
    except Exception as exc:
        # PII-free: log only the exception type, never document contents.
        logger.warning("list_invoices_page failed: %s", type(exc).__name__)
        return [], None


def update_status(invoice_id: str, new_status: str) -> tuple[bool, str]:
    """Transition an invoice to a new status. Returns (success, error)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    current = invoice.get("status", "")
    allowed = STATUS_TRANSITIONS.get(current, ())
    if new_status not in allowed:
        return False, f"Transition de « {STATUS_LABELS.get(current, current)} » vers « {STATUS_LABELS.get(new_status, new_status)} » non permise."

    now = datetime.now(timezone.utc)
    try:
        db.collection(COLLECTION).document(invoice_id).update({
            "status": new_status,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        return True, ""
    except Exception as exc:
        return False, f"Erreur : {exc}"


def void_invoice(invoice_id: str) -> tuple[bool, str]:
    """Void an invoice: set status to annulée and release linked entries/expenses.

    All-or-nothing: every source release AND the status flip are committed in
    a single batch. On any failure the invoice keeps its current status and
    no source is left stranded as invoiced.
    """
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    current = invoice.get("status", "")
    if current == "annulée":
        return False, "Cette facture est déjà annulée."
    if current == "payée":
        return False, "Impossible d'annuler une facture déjà payée."

    now = datetime.now(timezone.utc)

    try:
        # Read all line items first to find source IDs
        items = [
            item_doc.to_dict()
            for item_doc in (
                db.collection(COLLECTION)
                .document(invoice_id)
                .collection(LINE_ITEMS_SUB)
                .stream()
            )
        ]

        batch = db.batch()

        for item in items:
            source_id = item.get("source_id", "")
            if not source_id:
                continue

            # Determine collection based on type
            col = TE_COLLECTION if item.get("type") == "fee" else EXP_COLLECTION
            batch.update(db.collection(col).document(source_id), {
                "invoiced": False,
                "invoice_id": None,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })

        # Update invoice status in the same atomic commit
        batch.update(db.collection(COLLECTION).document(invoice_id), {
            "status": "annulée",
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })

        batch.commit()
        return True, ""
    except Exception as exc:
        logger.error("void_invoice failed for %s: %s", sanitize_log_value(invoice_id), exc)
        return False, f"Erreur lors de l'annulation : {exc}"


def delete_invoice(invoice_id: str) -> tuple[bool, str]:
    """Delete a cancelled invoice and its line items. Returns (success, error)."""
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    if invoice.get("status") != "annulée":
        return False, "Seule une facture annulée peut être supprimée."

    # Guard: refuse deletion while any source still references this invoice.
    # void_invoice should have released them; this protects the stranded case.
    # Invoice number reuse is prevented by the monotonic counter
    # (counters/invoices-{year}), so hard deletion never frees a number.
    try:
        stranded = list(
            db.collection(TE_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        ) or list(
            db.collection(EXP_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        )
        if stranded:
            return False, (
                "Des entrées de temps ou des dépenses référencent encore cette "
                "facture. Elles doivent être libérées avant la suppression."
            )
    except Exception as exc:
        logger.error(
            "delete_invoice: reference check failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )
        return False, f"Erreur lors de la vérification des références : {exc}"

    try:
        # Delete all line items in the subcollection
        items_ref = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        for item_doc in items_ref:
            item_doc.reference.delete()

        # Delete the invoice document
        db.collection(COLLECTION).document(invoice_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


# Shared implementation lives in models/__init__.py; aliased so this module's
# helpers (and their tests) keep a stable local name.
_aggregation_values = aggregation_values


def get_outstanding_total() -> int:
    """Return the total amount due (cents) on outstanding invoices.

    A single server-side SUM aggregation over
    ``status in (envoyée, en_retard)`` replaces the dashboard's two full
    list scans. The ``in`` filter counts as an equality for index purposes,
    so this requires the ``invoices`` composite index
    (status ASC, amount_due ASC); see ``firestore.indexes.json``.

    Returns 0 on failure (graceful degradation for the dashboard stat).
    """
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("status", "in", ["envoyée", "en_retard"])
        )
        agg_query = query.sum("amount_due", alias="outstanding")
        values = _aggregation_values(agg_query.get())
        return int(round(values.get("outstanding", 0) or 0))
    except Exception as exc:
        logger.warning("get_outstanding_total: aggregation query failed: %s", exc)
        return 0


def get_invoice_summary(dossier_id: str) -> dict:
    """Return invoice summary for a dossier."""
    invoices = list_invoices(dossier_id=dossier_id)
    total_invoiced = 0
    total_paid = 0
    count = 0

    for inv in invoices:
        if inv.get("status") == "annulée":
            continue
        count += 1
        total_invoiced += inv.get("total", 0)
        if inv.get("status") == "payée":
            total_paid += inv.get("total", 0)

    return {
        "count": count,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "total_outstanding": total_invoiced - total_paid,
    }
