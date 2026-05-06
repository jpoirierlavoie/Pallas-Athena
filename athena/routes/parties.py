"""Partie (contact/party) management routes — list, detail, create, edit, delete."""

from datetime import datetime

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import bump_ctag, record_tombstone
from pagination import paginate
from models.partie import (
    ROLE_LABELS,
    VALID_CONTACT_ROLES,
    create_partie,
    delete_partie,
    display_name,
    get_partie,
    list_parties,
    update_partie,
)
from models.dossier import (
    STATUS_LABELS as DOSSIER_STATUS_LABELS,
    MATTER_TYPE_LABELS as DOSSIER_MATTER_TYPE_LABELS,
    list_dossiers_for_partie,
)

parties_bp = Blueprint(
    "parties", __name__, url_prefix="/parties"
)


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _form_data() -> dict:
    """Extract partie fields from the submitted form."""
    f = request.form
    return {
        "type": f.get("type", "individual"),
        "contact_role": f.get("contact_role", "client"),
        # Individual
        "prefix": f.get("prefix", ""),
        "first_name": f.get("first_name", "").strip(),
        "last_name": f.get("last_name", "").strip(),
        # Organization (personne morale)
        "organization_name": f.get("organization_name", "").strip(),
        "trade_name": f.get("trade_name", "").strip(),
        "governing_law": f.get("governing_law", "").strip(),
        # Demographics
        "language": f.get("language", ""),
        "gender": f.get("gender", ""),
        "pronouns": f.get("pronouns", ""),
        # Professional coordinates
        "job_title": f.get("job_title", "").strip(),
        "job_role": f.get("job_role", "").strip(),
        "organization": f.get("organization", "").strip(),
        # Personal contact
        "email": f.get("email", "").strip(),
        "phone_home": f.get("phone_home", "").strip(),
        "phone_cell": f.get("phone_cell", "").strip(),
        # Professional contact
        "email_work": f.get("email_work", "").strip(),
        "phone_work": f.get("phone_work", "").strip(),
        "fax": f.get("fax", "").strip(),
        # Personal address
        "address_street": f.get("address_street", "").strip(),
        "address_unit": f.get("address_unit", "").strip(),
        "address_city": f.get("address_city", "").strip(),
        "address_province": f.get("address_province", "QC").strip(),
        "address_postal_code": f.get("address_postal_code", "").strip().upper(),
        "address_country": f.get("address_country", "CA").strip(),
        # Work address
        "work_address_street": f.get("work_address_street", "").strip(),
        "work_address_unit": f.get("work_address_unit", "").strip(),
        "work_address_city": f.get("work_address_city", "").strip(),
        "work_address_province": f.get("work_address_province", "").strip(),
        "work_address_postal_code": f.get("work_address_postal_code", "").strip().upper(),
        "work_address_country": f.get("work_address_country", "CA").strip(),
        # Legal identifiers
        "bar_number": f.get("bar_number", "").strip(),
        "company_neq": f.get("company_neq", "").strip(),
        # Compliance (only when contact_role == client)
        "identity_verified": f.get("identity_verified", "non_vérifié"),
        "identity_verified_notes": f.get("identity_verified_notes", "").strip(),
        "conflict_check": f.get("conflict_check", "non_vérifié"),
        "conflict_check_notes": f.get("conflict_check_notes", "").strip(),
        # Notes
        "notes": f.get("notes", "").strip(),
    }


# ── List ──────────────────────────────────────────────────────────────────


@parties_bp.route("/")
@login_required
def partie_list() -> str:
    """Render the partie list with optional filters."""
    role_filter = request.args.get("role", "client")
    search = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    parties = list_parties(
        role_filter=role_filter if role_filter != "tous" else None,
        search=search or None,
    )

    # Attach display names
    for p in parties:
        p["_display_name"] = display_name(p)

    parties, pagination = paginate(parties, page)
    pagination["url"] = url_for("parties.partie_list")
    pagination["target"] = "#partie-rows"

    if _is_htmx():
        return render_template(
            "parties/_partie_rows.html",
            parties=parties,
            role_labels=ROLE_LABELS,
            pagination=pagination,
        )

    return render_template(
        "parties/list.html",
        parties=parties,
        role_filter=role_filter,
        search=search,
        role_labels=ROLE_LABELS,
        valid_roles=VALID_CONTACT_ROLES,
        pagination=pagination,
    )


# ── Search (HTMX autocomplete for other modules) ─────────────────────────


@parties_bp.route("/search")
@login_required
def partie_search() -> str:
    """Return a small HTML fragment of matching parties (for autocomplete)."""
    q = request.args.get("q", "").strip()
    results = list_parties(search=q) if q else []
    for p in results:
        p["_display_name"] = display_name(p)
    return render_template("parties/_search_results.html", parties=results[:10])


# ── Detail ────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>")
@login_required
def partie_detail(partie_id: str) -> str:
    """Render the partie detail page."""
    partie = get_partie(partie_id)
    if not partie:
        return render_template(
            "parties/list.html",
            parties=[],
            role_filter="",
            search="",
            role_labels=ROLE_LABELS,
            valid_roles=VALID_CONTACT_ROLES,
            error="Contact introuvable.",
        )

    partie["_display_name"] = display_name(partie)
    dossiers = list_dossiers_for_partie(partie_id)
    return render_template(
        "parties/detail.html",
        partie=partie,
        role_labels=ROLE_LABELS,
        dossiers=dossiers,
        dossier_status_labels=DOSSIER_STATUS_LABELS,
        dossier_matter_type_labels=DOSSIER_MATTER_TYPE_LABELS,
    )


# ── Create ────────────────────────────────────────────────────────────────


@parties_bp.route("/new")
@login_required
def partie_new() -> str:
    """Render the empty partie form."""
    return render_template(
        "parties/form.html",
        partie=None,
        errors=[],
        role_labels=ROLE_LABELS,
    )


@parties_bp.route("/", methods=["POST"])
@login_required
def partie_create() -> str:
    """Handle new partie form submission."""
    data = _form_data()
    partie, errors = create_partie(data)

    if errors:
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            role_labels=ROLE_LABELS,
        )

    bump_ctag("parties")

    if _is_htmx():
        resp = redirect(url_for("parties.partie_detail", partie_id=partie["id"]))
        resp.headers["HX-Redirect"] = url_for(
            "parties.partie_detail", partie_id=partie["id"]
        )
        return resp

    return redirect(url_for("parties.partie_detail", partie_id=partie["id"]))


# ── Edit ──────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/edit")
@login_required
def partie_edit(partie_id: str) -> str:
    """Render the edit form pre-filled with partie data."""
    partie = get_partie(partie_id)
    if not partie:
        return redirect(url_for("parties.partie_list"))

    return render_template(
        "parties/form.html",
        partie=partie,
        errors=[],
        role_labels=ROLE_LABELS,
    )


@parties_bp.route("/<partie_id>", methods=["POST"])
@login_required
def partie_update(partie_id: str) -> str:
    """Handle edit form submission."""
    data = _form_data()
    partie, errors = update_partie(partie_id, data)

    if errors:
        data["id"] = partie_id
        return render_template(
            "parties/form.html",
            partie=data,
            errors=errors,
            role_labels=ROLE_LABELS,
        )

    bump_ctag("parties")

    if _is_htmx():
        resp = redirect(url_for("parties.partie_detail", partie_id=partie_id))
        resp.headers["HX-Redirect"] = url_for(
            "parties.partie_detail", partie_id=partie_id
        )
        return resp

    return redirect(url_for("parties.partie_detail", partie_id=partie_id))


# ── Delete ────────────────────────────────────────────────────────────────


@parties_bp.route("/<partie_id>/delete", methods=["POST"])
@login_required
def partie_delete(partie_id: str) -> str:
    """Delete a partie and redirect to the list."""
    success, error = delete_partie(partie_id)

    if success:
        record_tombstone("parties", partie_id)
        bump_ctag("parties")

    if _is_htmx():
        if success:
            resp = redirect(url_for("parties.partie_list"))
            resp.headers["HX-Redirect"] = url_for("parties.partie_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("parties.partie_list"))


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("_display_name", "Nom"),
    ("contact_role", "Rôle"),
    ("email", "Courriel"),
    ("phone_cell", "Cellulaire"),
    ("phone_work", "Tél. professionnel"),
    ("organization", "Organisation"),
    ("address_city", "Ville"),
]

_EXPORT_COLUMNS_PDF = [
    ("_display_name", "Nom", 2.0),
    ("contact_role", "Rôle", 1.0),
    ("email", "Courriel", 1.5),
    ("phone_cell", "Cellulaire", 1.0),
    ("phone_work", "Tél. professionnel", 1.0),
    ("organization", "Organisation", 1.5),
    ("address_city", "Ville", 1.0),
]


def _get_export_parties() -> list[dict]:
    """Fetch and pre-process parties for export, respecting current filters."""
    role_filter = request.args.get("role", "client")
    search = request.args.get("q", "").strip()

    parties = list_parties(
        role_filter=role_filter if role_filter != "tous" else None,
        search=search or None,
    )
    for p in parties:
        p["_display_name"] = display_name(p)
        p["contact_role"] = ROLE_LABELS.get(p.get("contact_role", ""), p.get("contact_role", ""))
    return parties


@parties_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export parties as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"parties_{date_str}.csv",
    )


@parties_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export parties as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_parties()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Parties",
        filename=f"parties_{date_str}.pdf",
    )
