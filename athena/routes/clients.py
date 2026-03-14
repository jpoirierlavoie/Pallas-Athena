"""Client management routes — list, detail, create, edit, delete."""

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from models.client import (
    ROLE_LABELS,
    VALID_CONTACT_ROLES,
    create_client,
    delete_client,
    display_name,
    get_client,
    list_clients,
    update_client,
)
from models.dossier import (
    STATUS_LABELS as DOSSIER_STATUS_LABELS,
    MATTER_TYPE_LABELS as DOSSIER_MATTER_TYPE_LABELS,
    list_dossiers_for_client,
)

clients_bp = Blueprint(
    "clients", __name__, url_prefix="/clients"
)


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _form_data() -> dict:
    """Extract client fields from the submitted form."""
    f = request.form
    return {
        "type": f.get("type", "individual"),
        "contact_role": f.get("contact_role", "client"),
        # Individual
        "prefix": f.get("prefix", ""),
        "first_name": f.get("first_name", "").strip(),
        "last_name": f.get("last_name", "").strip(),
        # Organization
        "organization_name": f.get("organization_name", "").strip(),
        "contact_person": f.get("contact_person", "").strip(),
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


@clients_bp.route("/")
@login_required
def client_list() -> str:
    """Render the client list with optional filters."""
    type_filter = request.args.get("type", "")
    role_filter = request.args.get("role", "")
    search = request.args.get("q", "").strip()

    clients = list_clients(
        type_filter=type_filter or None,
        role_filter=role_filter or None,
        search=search or None,
    )

    # Attach display names
    for c in clients:
        c["_display_name"] = display_name(c)

    if _is_htmx():
        return render_template(
            "clients/_client_rows.html",
            clients=clients,
            role_labels=ROLE_LABELS,
        )

    return render_template(
        "clients/list.html",
        clients=clients,
        type_filter=type_filter,
        role_filter=role_filter,
        search=search,
        role_labels=ROLE_LABELS,
        valid_roles=VALID_CONTACT_ROLES,
    )


# ── Search (HTMX autocomplete for other modules) ─────────────────────────


@clients_bp.route("/search")
@login_required
def client_search() -> str:
    """Return a small HTML fragment of matching clients (for autocomplete)."""
    q = request.args.get("q", "").strip()
    results = list_clients(search=q) if q else []
    for c in results:
        c["_display_name"] = display_name(c)
    return render_template("clients/_search_results.html", clients=results[:10])


# ── Detail ────────────────────────────────────────────────────────────────


@clients_bp.route("/<client_id>")
@login_required
def client_detail(client_id: str) -> str:
    """Render the client detail page."""
    client = get_client(client_id)
    if not client:
        return render_template(
            "clients/list.html",
            clients=[],
            type_filter="",
            role_filter="",
            search="",
            role_labels=ROLE_LABELS,
            valid_roles=VALID_CONTACT_ROLES,
            error="Contact introuvable.",
        )

    client["_display_name"] = display_name(client)
    dossiers = list_dossiers_for_client(client_id)
    return render_template(
        "clients/detail.html",
        client=client,
        role_labels=ROLE_LABELS,
        dossiers=dossiers,
        dossier_status_labels=DOSSIER_STATUS_LABELS,
        dossier_matter_type_labels=DOSSIER_MATTER_TYPE_LABELS,
    )


# ── Create ────────────────────────────────────────────────────────────────


@clients_bp.route("/new")
@login_required
def client_new() -> str:
    """Render the empty client form."""
    return render_template(
        "clients/form.html",
        client=None,
        errors=[],
        role_labels=ROLE_LABELS,
    )


@clients_bp.route("/", methods=["POST"])
@login_required
def client_create() -> str:
    """Handle new client form submission."""
    data = _form_data()
    client, errors = create_client(data)

    if errors:
        return render_template(
            "clients/form.html",
            client=data,
            errors=errors,
            role_labels=ROLE_LABELS,
        )

    if _is_htmx():
        resp = redirect(url_for("clients.client_detail", client_id=client["id"]))
        resp.headers["HX-Redirect"] = url_for(
            "clients.client_detail", client_id=client["id"]
        )
        return resp

    return redirect(url_for("clients.client_detail", client_id=client["id"]))


# ── Edit ──────────────────────────────────────────────────────────────────


@clients_bp.route("/<client_id>/edit")
@login_required
def client_edit(client_id: str) -> str:
    """Render the edit form pre-filled with client data."""
    client = get_client(client_id)
    if not client:
        return redirect(url_for("clients.client_list"))

    return render_template(
        "clients/form.html",
        client=client,
        errors=[],
        role_labels=ROLE_LABELS,
    )


@clients_bp.route("/<client_id>", methods=["POST"])
@login_required
def client_update(client_id: str) -> str:
    """Handle edit form submission."""
    data = _form_data()
    client, errors = update_client(client_id, data)

    if errors:
        data["id"] = client_id
        return render_template(
            "clients/form.html",
            client=data,
            errors=errors,
            role_labels=ROLE_LABELS,
        )

    if _is_htmx():
        resp = redirect(url_for("clients.client_detail", client_id=client_id))
        resp.headers["HX-Redirect"] = url_for(
            "clients.client_detail", client_id=client_id
        )
        return resp

    return redirect(url_for("clients.client_detail", client_id=client_id))


# ── Delete ────────────────────────────────────────────────────────────────


@clients_bp.route("/<client_id>/delete", methods=["POST"])
@login_required
def client_delete(client_id: str) -> str:
    """Delete a client and redirect to the list."""
    success, error = delete_client(client_id)

    if _is_htmx():
        if success:
            resp = redirect(url_for("clients.client_list"))
            resp.headers["HX-Redirect"] = url_for("clients.client_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("clients.client_list"))
