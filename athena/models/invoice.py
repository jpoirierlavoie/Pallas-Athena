"""Invoice Firestore CRUD, tax computation, and line-item management."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize


# Firestore collection paths
COLLECTION = "invoices"
LINE_ITEMS_SUB = "lineitems"

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


def _generate_invoice_number() -> str:
    """Auto-generate sequential invoice number in YYYY-FNNN format."""
    year = datetime.now(timezone.utc).strftime("%Y")
    prefix = f"{year}-F"

    try:
        # Find highest existing number for this year
        docs = db.collection(COLLECTION).stream()
        max_seq = 0
        for doc in docs:
            d = doc.to_dict()
            num = d.get("invoice_number", "")
            if num.startswith(prefix):
                try:
                    seq = int(num[len(prefix):])
                    max_seq = max(max_seq, seq)
                except (ValueError, IndexError):
                    pass
        return f"{prefix}{max_seq + 1:03d}"
    except Exception:
        return f"{prefix}001"


# ── CRUD ─────────────────────────────────────────────────────────────────


def create_invoice(
    dossier_id: str,
    selected_entry_ids: list[str],
    selected_expense_ids: list[str],
    data: dict,
) -> tuple[Optional[dict], list[str]]:
    """Create an invoice with line items from selected time entries and expenses.

    Marks the source entries/expenses as invoiced. Returns (invoice, errors).
    """
    from models.time_entry import get_time_entry, mark_time_entries_invoiced
    from models.expense import get_expense, mark_expenses_invoiced

    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if not selected_entry_ids and not selected_expense_ids:
        errors.append("Sélectionnez au moins une entrée de temps ou une dépense.")
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    invoice_id = str(uuid.uuid4())
    invoice_number = _generate_invoice_number()

    # Build line items from selected sources
    line_items: list[dict] = []

    for eid in selected_entry_ids:
        entry = get_time_entry(eid)
        if not entry or entry.get("invoiced"):
            continue
        item = {
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
        }
        line_items.append(item)

    for eid in selected_expense_ids:
        expense = get_expense(eid)
        if not expense or expense.get("invoiced"):
            continue
        item = {
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
        }
        line_items.append(item)

    if not line_items:
        return None, ["Aucune entrée valide sélectionnée."]

    # Compute totals
    totals = compute_totals(line_items)
    retainer_applied = merged.get("retainer_applied", 0)

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

    try:
        # Write invoice document
        db.collection(COLLECTION).document(invoice_id).set(merged)

        # Write line items as subcollection
        for item in line_items:
            db.collection(COLLECTION).document(invoice_id).collection(
                LINE_ITEMS_SUB
            ).document(item["id"]).set(item)

        # Mark sources as invoiced
        if selected_entry_ids:
            mark_time_entries_invoiced(selected_entry_ids, invoice_id)
        if selected_expense_ids:
            mark_expenses_invoiced(selected_expense_ids, invoice_id)

    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_invoice(invoice_id: str) -> Optional[dict]:
    """Fetch a single invoice by ID (without line items)."""
    try:
        doc = db.collection(COLLECTION).document(invoice_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
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
    except Exception:
        pass

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
    """Void an invoice: set status to annulée and release linked entries/expenses."""
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
        # Get line items to find source IDs
        items_ref = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )

        for item_doc in items_ref:
            item = item_doc.to_dict()
            source_id = item.get("source_id", "")
            if not source_id:
                continue

            # Determine collection based on type
            col = TE_COLLECTION if item.get("type") == "fee" else EXP_COLLECTION
            try:
                db.collection(col).document(source_id).update({
                    "invoiced": False,
                    "invoice_id": None,
                    "updated_at": now,
                    "etag": str(uuid.uuid4()),
                })
            except Exception:
                pass

        # Update invoice status
        db.collection(COLLECTION).document(invoice_id).update({
            "status": "annulée",
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        return True, ""
    except Exception as exc:
        return False, f"Erreur : {exc}"


def delete_invoice(invoice_id: str) -> tuple[bool, str]:
    """Delete a cancelled invoice and its line items. Returns (success, error)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    if invoice.get("status") != "annulée":
        return False, "Seule une facture annulée peut être supprimée."

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
