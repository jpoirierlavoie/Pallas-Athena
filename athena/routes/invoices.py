"""Invoice management routes — list, create, detail, status updates."""

from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from security import safe_internal_redirect
from config import Config
from models.invoice import (
    STATUS_LABELS,
    STATUS_TRANSITIONS,
    create_invoice,
    delete_invoice,
    get_invoice_with_items,
    list_invoices,
    update_status,
    void_invoice,
)
from models.dossier import get_dossier, list_dossiers
from models.partie import display_name, get_partie
from models.time_entry import get_unbilled_time_entries
from models.expense import get_unbilled_expenses

invoices_bp = Blueprint("invoices", __name__, url_prefix="/factures")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse_date(value: str) -> datetime | None:
    """Parse an HTML date input (YYYY-MM-DD) into a UTC datetime."""
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _parse_cents(value: str) -> int:
    """Parse a dollar string (e.g., '250.00') into integer cents."""
    if not value or not value.strip():
        return 0
    try:
        return int(round(float(value.strip().replace(",", ".")) * 100))
    except (ValueError, TypeError):
        return 0


def _firm_info() -> dict:
    """Return firm info from config for invoice display."""
    return {
        "name": Config.FIRM_NAME,
        "street": Config.FIRM_STREET,
        "unit": Config.FIRM_UNIT,
        "city": Config.FIRM_CITY,
        "province": Config.FIRM_PROVINCE,
        "postal_code": Config.FIRM_POSTAL_CODE,
        "phone": Config.FIRM_PHONE,
        "email": Config.FIRM_EMAIL,
        "gst_number": Config.GST_NUMBER,
        "qst_number": Config.QST_NUMBER,
    }


def _build_billing_address(partie: dict) -> dict:
    """Snapshot the billing address from a partie record."""
    name = display_name(partie)
    # Prefer work address if available, fall back to personal
    if partie.get("work_address_street"):
        return {
            "name": name,
            "street": partie.get("work_address_street", ""),
            "unit": partie.get("work_address_unit", ""),
            "city": partie.get("work_address_city", ""),
            "province": partie.get("work_address_province", "QC"),
            "postal_code": partie.get("work_address_postal_code", ""),
        }
    return {
        "name": name,
        "street": partie.get("address_street", ""),
        "unit": partie.get("address_unit", ""),
        "city": partie.get("address_city", ""),
        "province": partie.get("address_province", "QC"),
        "postal_code": partie.get("address_postal_code", ""),
    }


def _template_context() -> dict:
    """Return shared template context for invoice views."""
    return {
        "status_labels": STATUS_LABELS,
        "firm": _firm_info(),
    }


# ── Invoice list ─────────────────────────────────────────────────────────


@invoices_bp.route("/")
@login_required
def invoice_list() -> str:
    """Render the invoice list with optional filters."""
    status_filter = request.args.get("status", "")
    dossier_id = request.args.get("dossier_id", "").strip()
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))

    invoices = list_invoices(
        status_filter=status_filter or None,
        dossier_id=dossier_id or None,
        date_from=date_from,
        date_to=date_to,
    )

    ctx = _template_context()
    ctx.update(
        invoices=invoices,
        status_filter=status_filter,
        dossier_id=dossier_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )

    if _is_htmx():
        return render_template("invoices/_invoice_rows.html", **ctx)

    return render_template("invoices/list.html", **ctx)


# ── Invoice creation flow ────────────────────────────────────────────────


@invoices_bp.route("/new")
@login_required
def invoice_new() -> str:
    """Step 1: Select a dossier, then show unbilled items."""
    dossier_id = request.args.get("dossier_id", "").strip()
    return_to = request.args.get("return_to", "")

    ctx = _template_context()
    ctx["return_to"] = return_to

    if not dossier_id:
        # Show dossier selector
        dossiers = list_dossiers(status_filter="actif")
        ctx["dossiers"] = dossiers
        ctx["errors"] = []
        return render_template("invoices/create.html", **ctx)

    # Load dossier + unbilled items
    dossier = get_dossier(dossier_id)
    if not dossier:
        ctx["dossiers"] = list_dossiers(status_filter="actif")
        ctx["errors"] = ["Dossier introuvable."]
        return render_template("invoices/create.html", **ctx)

    unbilled_entries = get_unbilled_time_entries(dossier_id)
    unbilled_expenses = get_unbilled_expenses(dossier_id)

    # Get first client info for billing address
    client_partie = None
    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    clients = dossier.get("clients", [])
    if clients:
        client_partie = get_partie(clients[0].get("id", ""))
        if client_partie:
            billing_address = _build_billing_address(client_partie)

    today = datetime.now(timezone.utc)

    ctx.update(
        dossier=dossier,
        unbilled_entries=unbilled_entries,
        unbilled_expenses=unbilled_expenses,
        billing_address=billing_address,
        client_name=clients[0].get("name", "") if clients else "",
        client_id=clients[0].get("id", "") if clients else "",
        invoice_date=today.strftime("%Y-%m-%d"),
        errors=[],
    )
    return render_template("invoices/create.html", **ctx)


@invoices_bp.route("/unbilled/<dossier_id>")
@login_required
def unbilled_items(dossier_id: str) -> str:
    """HTMX endpoint: return unbilled items for a dossier."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return '<p class="text-red-600 text-sm">Dossier introuvable.</p>', 404

    unbilled_entries = get_unbilled_time_entries(dossier_id)
    unbilled_expenses = get_unbilled_expenses(dossier_id)

    # Get client info
    clients = dossier.get("clients", [])
    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    if clients:
        client_partie = get_partie(clients[0].get("id", ""))
        if client_partie:
            billing_address = _build_billing_address(client_partie)

    today = datetime.now(timezone.utc)

    ctx = _template_context()
    ctx.update(
        dossier=dossier,
        unbilled_entries=unbilled_entries,
        unbilled_expenses=unbilled_expenses,
        billing_address=billing_address,
        client_name=clients[0].get("name", "") if clients else "",
        client_id=clients[0].get("id", "") if clients else "",
        invoice_date=today.strftime("%Y-%m-%d"),
        errors=[],
    )
    return render_template("invoices/_unbilled_items.html", **ctx)


@invoices_bp.route("/", methods=["POST"])
@login_required
def invoice_create() -> str:
    """Handle invoice creation form submission."""
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    return_to = f.get("return_to", "")

    dossier = get_dossier(dossier_id)
    if not dossier:
        ctx = _template_context()
        ctx["dossiers"] = list_dossiers(status_filter="actif")
        ctx["errors"] = ["Dossier introuvable."]
        ctx["return_to"] = return_to
        return render_template("invoices/create.html", **ctx)

    # Collect selected items
    selected_entry_ids = f.getlist("selected_entries")
    selected_expense_ids = f.getlist("selected_expenses")

    # Build client info
    clients = dossier.get("clients", [])
    client_name = clients[0].get("name", "") if clients else ""
    client_id = clients[0].get("id", "") if clients else ""

    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    if client_id:
        client_partie = get_partie(client_id)
        if client_partie:
            billing_address = _build_billing_address(client_partie)

    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "client_id": client_id,
        "client_name": client_name,
        "billing_address": billing_address,
        "date": _parse_date(f.get("invoice_date", "")),
        "due_date": _parse_date(f.get("due_date", "")),
        "notes": f.get("notes", "").strip(),
        "payment_terms": f.get("payment_terms", "").strip(),
        "retainer_applied": _parse_cents(f.get("retainer_applied", "")),
        "gst_number": Config.GST_NUMBER,
        "qst_number": Config.QST_NUMBER,
    }

    invoice, errors = create_invoice(
        dossier_id, selected_entry_ids, selected_expense_ids, data
    )

    if errors:
        unbilled_entries = get_unbilled_time_entries(dossier_id)
        unbilled_expenses = get_unbilled_expenses(dossier_id)
        ctx = _template_context()
        ctx.update(
            dossier=dossier,
            unbilled_entries=unbilled_entries,
            unbilled_expenses=unbilled_expenses,
            billing_address=billing_address,
            client_name=client_name,
            client_id=client_id,
            invoice_date=f.get("invoice_date", ""),
            errors=errors,
            return_to=return_to,
        )
        return render_template("invoices/create.html", **ctx)

    # On success, prefer the caller's URL when supplied (e.g. dossier hub) so the
    # user lands back where they started rather than on the invoice detail.
    fallback = url_for("invoices.invoice_detail", invoice_id=invoice["id"])
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Invoice detail ───────────────────────────────────────────────────────


@invoices_bp.route("/<invoice_id>")
@login_required
def invoice_detail(invoice_id: str) -> str:
    """Render the invoice detail / print-ready view."""
    invoice, items = get_invoice_with_items(invoice_id)
    if not invoice:
        return redirect(url_for("invoices.invoice_list"))

    # Separate items by type
    fee_items = [i for i in items if i.get("type") == "fee"]
    expense_items = [i for i in items if i.get("type") == "expense"]

    # Available status transitions
    current_status = invoice.get("status", "")
    transitions = STATUS_TRANSITIONS.get(current_status, ())

    ctx = _template_context()
    ctx.update(
        invoice=invoice,
        fee_items=fee_items,
        expense_items=expense_items,
        transitions=transitions,
        return_to=request.args.get("return_to", ""),
    )
    return render_template("invoices/detail.html", **ctx)


# ── Status transitions ──────────────────────────────────────────────────


@invoices_bp.route("/<invoice_id>/status", methods=["POST"])
@login_required
def invoice_update_status(invoice_id: str) -> str:
    """Update invoice status."""
    new_status = request.form.get("status", "").strip()
    success, error = update_status(invoice_id, new_status)

    target = url_for("invoices.invoice_detail", invoice_id=invoice_id)

    if not success:
        if _is_htmx():
            return f'<div class="text-red-600 text-sm p-2">{error}</div>', 422
        return redirect(target)

    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@invoices_bp.route("/<invoice_id>/void", methods=["POST"])
@login_required
def invoice_void(invoice_id: str) -> str:
    """Void an invoice and release linked entries/expenses."""
    success, error = void_invoice(invoice_id)

    target = url_for("invoices.invoice_detail", invoice_id=invoice_id)

    if not success:
        if _is_htmx():
            return f'<div class="text-red-600 text-sm p-2">{error}</div>', 422
        return redirect(target)

    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@invoices_bp.route("/<invoice_id>/delete", methods=["POST"])
@login_required
def invoice_delete(invoice_id: str) -> str:
    """Delete a cancelled invoice."""
    return_to = request.form.get("return_to", "")
    success, error = delete_invoice(invoice_id)

    if not success:
        target = url_for("invoices.invoice_detail", invoice_id=invoice_id)
        if _is_htmx():
            return f'<div class="text-red-600 text-sm p-2">{error}</div>', 422
        return redirect(target)

    target = safe_internal_redirect(return_to, url_for("invoices.invoice_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("invoice_number", "N° facture"),
    ("date", "Date"),
    ("dossier_file_number", "Dossier"),
    ("client_name", "Client"),
    ("subtotal", "Sous-total"),
    ("gst_amount", "TPS"),
    ("qst_amount", "TVQ"),
    ("total", "Total"),
    ("status", "Statut"),
]

_EXPORT_COLUMNS_PDF = [
    ("invoice_number", "N° facture", 1.0),
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("client_name", "Client", 1.5),
    ("subtotal", "Sous-total", 0.8),
    ("gst_amount", "TPS", 0.6),
    ("qst_amount", "TVQ", 0.6),
    ("total", "Total", 0.8),
    ("status", "Statut", 0.8),
]


def _get_export_invoices() -> list[dict]:
    """Fetch and pre-process invoices for export, respecting current filters."""
    from utils.export_csv import prepare_export_rows

    status_filter = request.args.get("status", "")
    dossier_id = request.args.get("dossier_id", "").strip()

    def _parse_date_local(value: str) -> datetime | None:
        if not value or not value.strip():
            return None
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    date_from = _parse_date_local(request.args.get("date_from", ""))
    date_to = _parse_date_local(request.args.get("date_to", ""))

    invoices = list_invoices(
        status_filter=status_filter or None,
        dossier_id=dossier_id or None,
        date_from=date_from,
        date_to=date_to,
    )
    return prepare_export_rows(invoices, label_maps={"status": STATUS_LABELS})


@invoices_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export invoices as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_invoices()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"factures_{date_str}.csv",
        cents_fields=["subtotal", "gst_amount", "qst_amount", "total"],
    )


@invoices_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export invoices as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_invoices()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Factures",
        filename=f"factures_{date_str}.pdf",
        cents_fields=["subtotal", "gst_amount", "qst_amount", "total"],
    )
