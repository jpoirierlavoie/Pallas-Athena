"""Dossier management routes — list, detail, create, edit, delete."""

import json
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import bump_ctag, record_tombstone
from pagination import paginate
from models.time_entry import (
    get_time_summary,
    list_time_entries,
)
from models.expense import (
    CATEGORY_LABELS as EXPENSE_CATEGORY_LABELS,
    get_expense_summary,
    list_expenses,
)
from models.invoice import (
    STATUS_LABELS as INVOICE_STATUS_LABELS,
    get_invoice_summary,
    list_invoices,
)
from models.hearing import (
    HEARING_TYPE_LABELS,
    STATUS_LABELS as HEARING_STATUS_LABELS,
    get_hearing_summary,
    list_hearings,
)
from models.task import (
    CATEGORY_LABELS as TASK_CATEGORY_LABELS,
    PRIORITY_LABELS as TASK_PRIORITY_LABELS,
    STATUS_LABELS as TASK_STATUS_LABELS,
    get_task_summary,
    list_tasks,
)
from models.protocol import (
    PROTOCOL_TYPE_COLORS,
    PROTOCOL_TYPE_SHORT_LABELS,
    check_overdue_steps,
    get_protocol,
    get_protocol_for_dossier,
    get_protocol_summary,
    list_protocols_for_dossier,
)
from models.document import (
    CATEGORY_LABELS as DOCUMENT_CATEGORY_LABELS,
    format_file_size,
    get_document_summary,
    get_file_icon,
    list_documents,
)
from models.folder import list_folders
from models.dossier import (
    FEE_TYPE_LABELS,
    MATTER_TYPE_LABELS,
    ROLE_LABELS,
    STATUS_LABELS,
    VALID_COURTS,
    VALID_DISTRICTS,
    VALID_STATUSES,
    create_dossier,
    delete_dossier,
    get_dossier,
    list_dossiers,
    suggest_file_number,
    update_dossier,
)

dossiers_bp = Blueprint(
    "dossiers", __name__, url_prefix="/dossiers"
)


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse_cents(value: str) -> int:
    """Parse a dollar string (e.g., '250.00') into integer cents."""
    if not value or not value.strip():
        return 0
    try:
        return int(round(float(value.strip().replace(",", ".")) * 100))
    except (ValueError, TypeError):
        return 0


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


def _parse_parties_json(raw: str) -> list[dict]:
    """Parse a JSON string of [{id, name}, ...] from a hidden form field."""
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        return [
            {"id": str(p["id"]), "name": str(p["name"])}
            for p in items
            if isinstance(p, dict) and p.get("id")
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _form_data() -> dict:
    """Extract dossier fields from the submitted form."""
    f = request.form
    return {
        "file_number": f.get("file_number", "").strip(),
        "title": f.get("title", "").strip(),
        # Parties (JSON arrays)
        "clients": _parse_parties_json(f.get("clients_json", "")),
        "opposing_parties": _parse_parties_json(f.get("opposing_parties_json", "")),
        # Classification
        "matter_type": f.get("matter_type", "litige_civil"),
        "court": f.get("court", ""),
        "district": f.get("district", ""),
        "court_file_number": f.get("court_file_number", "").strip(),
        "district_judiciaire": f.get("district_judiciaire", "").strip(),
        "tribunal": f.get("tribunal", "").strip(),
        "competence": f.get("competence", "").strip(),
        "palais_de_justice": f.get("palais_de_justice", "").strip(),
        "greffe_number": f.get("greffe_number", "").strip(),
        "juridiction_number": f.get("juridiction_number", "").strip(),
        "is_administrative_tribunal": f.get("is_administrative_tribunal") == "true",
        # Role
        "role": f.get("role", "demandeur"),
        # Financial
        "fee_type": f.get("fee_type", "hourly"),
        "hourly_rate": _parse_cents(f.get("hourly_rate", "")),
        "flat_fee": _parse_cents(f.get("flat_fee", "")) or None,
        "retainer_amount": _parse_cents(f.get("retainer_amount", "")),
        "retainer_balance": _parse_cents(f.get("retainer_balance", "")),
        # Status
        "status": f.get("status", "actif"),
        "opened_date": _parse_date(f.get("opened_date", "")),
        "closed_date": _parse_date(f.get("closed_date", "")),
        # Prescription
        "prescription_date": _parse_date(f.get("prescription_date", "")),
        "prescription_notes": f.get("prescription_notes", "").strip(),
        # Notes
        "notes": f.get("notes", "").strip(),
        "internal_notes": f.get("internal_notes", "").strip(),
    }


def _template_context() -> dict:
    """Return shared template context for dossier views."""
    return {
        "matter_type_labels": MATTER_TYPE_LABELS,
        "status_labels": STATUS_LABELS,
        "role_labels": ROLE_LABELS,
        "fee_type_labels": FEE_TYPE_LABELS,
        "valid_courts": VALID_COURTS,
        "valid_districts": VALID_DISTRICTS,
    }


def _attach_prescription_warnings(dossiers: list[dict]) -> None:
    """Attach _prescription_warning ('red', 'orange', or '') to each dossier."""
    now = datetime.now(timezone.utc)
    for d in dossiers:
        pd = d.get("prescription_date")
        if pd and hasattr(pd, "date"):
            delta = (pd - now).days
            if delta <= 30:
                d["_prescription_warning"] = "red"
            elif delta <= 60:
                d["_prescription_warning"] = "orange"
            else:
                d["_prescription_warning"] = ""
        else:
            d["_prescription_warning"] = ""


# ── List ──────────────────────────────────────────────────────────────────


@dossiers_bp.route("/")
@login_required
def dossier_list() -> str:
    """Render the dossier list with optional filters."""
    status_filter = request.args.get("status", "actif")
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "opened_date")
    page = request.args.get("page", 1, type=int)

    # "tous" means no status filter
    effective_filter = status_filter if status_filter != "tous" else None

    dossiers = list_dossiers(
        status_filter=effective_filter,
        search=search or None,
        sort_by=sort_by,
    )

    # Compute prescription warnings
    _attach_prescription_warnings(dossiers)

    dossiers, pagination = paginate(dossiers, page)
    pagination["url"] = url_for("dossiers.dossier_list")
    pagination["target"] = "#dossier-rows"

    ctx = _template_context()
    ctx.update(
        dossiers=dossiers,
        status_filter=status_filter,
        search=search,
        sort_by=sort_by,
        pagination=pagination,
    )

    if _is_htmx():
        return render_template("dossiers/_dossier_rows.html", **ctx)

    return render_template("dossiers/list.html", **ctx)


# ── Detail ────────────────────────────────────────────────────────────────


@dossiers_bp.route("/<dossier_id>")
@login_required
def dossier_detail(dossier_id: str) -> str:
    """Render the dossier detail hub page."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))

    _attach_prescription_warnings([dossier])

    ctx = _template_context()
    ctx["dossier"] = dossier
    return render_template("dossiers/detail.html", **ctx)


# ── Tab content (HTMX) ───────────────────────────────────────────────────


@dossiers_bp.route("/<dossier_id>/tab/<tab_name>")
@login_required
def dossier_tab(dossier_id: str, tab_name: str) -> str:
    """Return HTML fragment for a dossier detail tab."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return '<p class="text-red-600 text-sm">Dossier introuvable.</p>', 404

    _attach_prescription_warnings([dossier])

    ctx = _template_context()
    ctx["dossier"] = dossier

    templates = {
        "apercu": "dossiers/_tab_overview.html",
        "temps": "dossiers/_tab_temps.html",
        "facturation": "dossiers/_tab_facturation.html",
        "audiences": "dossiers/_tab_audiences.html",
        "taches": "dossiers/_tab_taches.html",
        "protocole": "dossiers/_tab_protocole.html",
        "documents": "dossiers/_tab_documents.html",
    }

    # Load time/expense data for the temps tab
    if tab_name == "temps":
        ctx["time_entries"] = list_time_entries(dossier_id=dossier_id)
        ctx["expenses"] = list_expenses(dossier_id=dossier_id)
        ctx["time_summary"] = get_time_summary(dossier_id)
        ctx["expense_summary"] = get_expense_summary(dossier_id)
        ctx["category_labels"] = EXPENSE_CATEGORY_LABELS

    # Load hearing data for the audiences tab
    if tab_name == "audiences":
        ctx["hearings"] = list_hearings(dossier_id=dossier_id)
        ctx["hearing_summary"] = get_hearing_summary(dossier_id)
        ctx["hearing_type_labels"] = HEARING_TYPE_LABELS
        ctx["status_labels"] = HEARING_STATUS_LABELS

    # Load task data for the taches tab
    if tab_name == "taches":
        ctx["tasks"] = list_tasks(dossier_id=dossier_id)
        ctx["task_summary"] = get_task_summary(dossier_id)
        ctx["category_labels"] = TASK_CATEGORY_LABELS
        ctx["priority_labels"] = TASK_PRIORITY_LABELS
        ctx["status_labels"] = TASK_STATUS_LABELS
        ctx["now"] = datetime.now(timezone.utc)

    # Load protocol data for the protocole tab
    if tab_name == "protocole":
        active_protocol = get_protocol_for_dossier(dossier_id, active_only=True)
        if active_protocol:
            check_overdue_steps(active_protocol["id"])
            active_protocol = get_protocol(active_protocol["id"])

        # Historical protocols (completed/suspended)
        all_protocols = list_protocols_for_dossier(dossier_id)
        historical_protocols = [
            p for p in all_protocols
            if p.get("status") in ("complété", "suspendu")
        ]

        ctx["protocol"] = active_protocol
        ctx["historical_protocols"] = historical_protocols
        ctx["protocol_summary"] = get_protocol_summary(dossier_id)
        ctx["protocol_type_colors"] = PROTOCOL_TYPE_COLORS
        ctx["protocol_type_short_labels"] = PROTOCOL_TYPE_SHORT_LABELS
        ctx["now"] = datetime.now(timezone.utc)

    # Load document data for the documents tab
    if tab_name == "documents":
        # Notes
        from models.note import list_notes, get_notes_summary, CATEGORY_LABELS as NOTE_CATEGORY_LABELS
        ctx["notes"] = list_notes(dossier_id=dossier_id)
        ctx["notes_summary"] = get_notes_summary(dossier_id)
        ctx["note_category_labels"] = NOTE_CATEGORY_LABELS

        # Root-level folders with item counts
        root_folders = list_folders(dossier_id, parent_folder_id=None)
        from models.folder import _count_items
        for f in root_folders:
            counts = _count_items(dossier_id, f["id"])
            f["_item_count"] = counts["folders"] + counts["documents"]
        ctx["root_folders"] = root_folders

        # Root-level documents only (no folder_id)
        docs = list_documents(dossier_id=dossier_id, folder_id=None)
        for d in docs:
            d["_file_size_fmt"] = format_file_size(d.get("file_size", 0))
            d["_file_icon"] = get_file_icon(d.get("file_type", ""))
        ctx["documents"] = docs
        ctx["document_summary"] = get_document_summary(dossier_id)
        ctx["category_labels"] = DOCUMENT_CATEGORY_LABELS

    # Load invoice data for the facturation tab
    if tab_name == "facturation":
        ctx["invoices"] = list_invoices(dossier_id=dossier_id)
        ctx["invoice_summary"] = get_invoice_summary(dossier_id)
        ctx["status_labels"] = INVOICE_STATUS_LABELS

    template = templates.get(tab_name, "dossiers/_tab_placeholder.html")
    ctx["tab_name"] = tab_name
    return render_template(template, **ctx)


# ── Create ────────────────────────────────────────────────────────────────


@dossiers_bp.route("/new")
@login_required
def dossier_new() -> str:
    """Render the empty dossier form."""
    suggested = suggest_file_number()
    ctx = _template_context()
    ctx.update(dossier=None, errors=[], suggested_file_number=suggested)
    return render_template("dossiers/form.html", **ctx)


@dossiers_bp.route("/", methods=["POST"])
@login_required
def dossier_create() -> str:
    """Handle new dossier form submission."""
    data = _form_data()
    dossier, errors = create_dossier(data)

    if errors:
        ctx = _template_context()
        ctx.update(
            dossier=data,
            errors=errors,
            suggested_file_number=data.get("file_number", ""),
        )
        return render_template("dossiers/form.html", **ctx)

    bump_ctag("dossiers")

    if _is_htmx():
        resp = redirect(
            url_for("dossiers.dossier_detail", dossier_id=dossier["id"])
        )
        resp.headers["HX-Redirect"] = url_for(
            "dossiers.dossier_detail", dossier_id=dossier["id"]
        )
        return resp

    return redirect(
        url_for("dossiers.dossier_detail", dossier_id=dossier["id"])
    )


# ── Edit ──────────────────────────────────────────────────────────────────


@dossiers_bp.route("/<dossier_id>/edit")
@login_required
def dossier_edit(dossier_id: str) -> str:
    """Render the edit form pre-filled with dossier data."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))

    ctx = _template_context()
    ctx.update(
        dossier=dossier,
        errors=[],
        suggested_file_number=dossier.get("file_number", ""),
    )
    return render_template("dossiers/form.html", **ctx)


@dossiers_bp.route("/<dossier_id>", methods=["POST"])
@login_required
def dossier_update(dossier_id: str) -> str:
    """Handle edit form submission."""
    data = _form_data()
    dossier, errors = update_dossier(dossier_id, data)

    if errors:
        data["id"] = dossier_id
        ctx = _template_context()
        ctx.update(
            dossier=data,
            errors=errors,
            suggested_file_number=data.get("file_number", ""),
        )
        return render_template("dossiers/form.html", **ctx)

    bump_ctag("dossiers")

    if _is_htmx():
        resp = redirect(
            url_for("dossiers.dossier_detail", dossier_id=dossier_id)
        )
        resp.headers["HX-Redirect"] = url_for(
            "dossiers.dossier_detail", dossier_id=dossier_id
        )
        return resp

    return redirect(
        url_for("dossiers.dossier_detail", dossier_id=dossier_id)
    )


# ── Delete ────────────────────────────────────────────────────────────────


@dossiers_bp.route("/<dossier_id>/delete", methods=["POST"])
@login_required
def dossier_delete(dossier_id: str) -> str:
    """Delete a dossier and redirect to the list."""
    success, error = delete_dossier(dossier_id)

    if success:
        record_tombstone("dossiers", dossier_id)
        bump_ctag("dossiers")

    if _is_htmx():
        if success:
            resp = redirect(url_for("dossiers.dossier_list"))
            resp.headers["HX-Redirect"] = url_for("dossiers.dossier_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("dossiers.dossier_list"))


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("file_number", "N° dossier"),
    ("title", "Titre"),
    ("_client_names", "Client(s)"),
    ("matter_type", "Type"),
    ("court", "Tribunal"),
    ("status", "Statut"),
    ("opened_date", "Ouverture"),
]

_EXPORT_COLUMNS_PDF = [
    ("file_number", "N° dossier", 1.0),
    ("title", "Titre", 2.0),
    ("_client_names", "Client(s)", 1.5),
    ("matter_type", "Type", 1.0),
    ("court", "Tribunal", 1.0),
    ("status", "Statut", 0.8),
    ("opened_date", "Ouverture", 1.0),
]


def _get_export_dossiers() -> list[dict]:
    """Fetch and pre-process dossiers for export, respecting current filters."""
    status_filter = request.args.get("status", "actif")
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "opened_date")

    effective_filter = status_filter if status_filter != "tous" else None

    dossiers = list_dossiers(
        status_filter=effective_filter,
        search=search or None,
        sort_by=sort_by,
    )
    for d in dossiers:
        d["_client_names"] = ", ".join(c.get("name", "") for c in d.get("clients", []))
        d["matter_type"] = MATTER_TYPE_LABELS.get(d.get("matter_type", ""), d.get("matter_type", ""))
        d["status"] = STATUS_LABELS.get(d.get("status", ""), d.get("status", ""))
    return dossiers


# ── Court file number parsing ─────────────────────────────────────────


@dossiers_bp.route("/parse-court-file", methods=["POST"])
@login_required
def parse_court_file():
    """Parse a court file number and return judicial metadata as JSON."""
    court_file_number = request.form.get("court_file_number", "").strip()

    from models.reference import parse_court_file_number
    result = parse_court_file_number(court_file_number)

    return jsonify({
        "district_judiciaire": (
            result["greffe"]["district_judiciaire"]
            if result.get("greffe") else ""
        ),
        "tribunal": (
            result["juridiction"]["tribunal"]
            if result.get("juridiction") else ""
        ),
        "competence": (
            result["juridiction"]["competence"]
            if result.get("juridiction") else ""
        ),
        "palais_de_justice": (
            result["greffe"]["palais_de_justice"]
            if result.get("greffe") else ""
        ),
        "greffe_number": result.get("greffe_number", ""),
        "juridiction_number": result.get("juridiction_number", ""),
        "is_administrative": result.get("is_administrative", False),
        "parse_error": result.get("parse_error"),
    })


# ── Export ────────────────────────────────────────────────────────────


@dossiers_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export dossiers as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_dossiers()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"dossiers_{date_str}.csv",
    )


@dossiers_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export dossiers as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_dossiers()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Dossiers",
        filename=f"dossiers_{date_str}.pdf",
    )
