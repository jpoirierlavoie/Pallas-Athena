"""Dossier management routes — list, detail, create, edit, delete."""

from datetime import datetime, timezone

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from models.client import display_name as client_display_name
from models.client import get_client
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


def _form_data() -> dict:
    """Extract dossier fields from the submitted form."""
    f = request.form
    return {
        "file_number": f.get("file_number", "").strip(),
        "title": f.get("title", "").strip(),
        "client_id": f.get("client_id", "").strip(),
        "client_name": f.get("client_name", "").strip(),
        # Classification
        "matter_type": f.get("matter_type", "litige_civil"),
        "court": f.get("court", ""),
        "district": f.get("district", ""),
        "court_file_number": f.get("court_file_number", "").strip(),
        # Parties
        "role": f.get("role", "demandeur"),
        "opposing_party": f.get("opposing_party", "").strip(),
        "opposing_counsel": f.get("opposing_counsel", "").strip(),
        "opposing_counsel_firm": f.get("opposing_counsel_firm", "").strip(),
        "opposing_counsel_phone": f.get("opposing_counsel_phone", "").strip(),
        "opposing_counsel_email": f.get("opposing_counsel_email", "").strip(),
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

    # "tous" means no status filter
    effective_filter = status_filter if status_filter != "tous" else None

    dossiers = list_dossiers(
        status_filter=effective_filter,
        search=search or None,
        sort_by=sort_by,
    )

    # Compute prescription warnings
    _attach_prescription_warnings(dossiers)

    ctx = _template_context()
    ctx.update(
        dossiers=dossiers,
        status_filter=status_filter,
        search=search,
        sort_by=sort_by,
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
        "temps": "dossiers/_tab_placeholder.html",
        "facturation": "dossiers/_tab_placeholder.html",
        "audiences": "dossiers/_tab_placeholder.html",
        "taches": "dossiers/_tab_placeholder.html",
        "protocole": "dossiers/_tab_placeholder.html",
        "documents": "dossiers/_tab_placeholder.html",
    }

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

    if _is_htmx():
        if success:
            resp = redirect(url_for("dossiers.dossier_list"))
            resp.headers["HX-Redirect"] = url_for("dossiers.dossier_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("dossiers.dossier_list"))
