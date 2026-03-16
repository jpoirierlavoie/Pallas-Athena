"""Protocol management routes — create, detail, edit, steps, completion."""

from datetime import datetime, timezone

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from models.dossier import get_dossier
from models.protocol import (
    PROTOCOL_TYPE_COLORS,
    PROTOCOL_TYPE_LABELS,
    PROTOCOL_TYPE_SHORT_LABELS,
    STATUS_LABELS,
    STEP_STATUS_COLORS,
    STEP_STATUS_LABELS,
    VALID_PROTOCOL_TYPES,
    VALID_STATUSES,
    add_step,
    check_overdue_steps,
    complete_step,
    create_protocol,
    delete_protocol,
    delete_step,
    get_protocol,
    get_protocol_for_dossier,
    list_protocols,
    recompute_deadlines,
    update_protocol,
    update_step,
)

protocols_bp = Blueprint("protocols", __name__, url_prefix="/protocoles")


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


def _template_context() -> dict:
    """Return shared template context for protocol views."""
    return {
        "protocol_type_labels": PROTOCOL_TYPE_LABELS,
        "protocol_type_short_labels": PROTOCOL_TYPE_SHORT_LABELS,
        "protocol_type_colors": PROTOCOL_TYPE_COLORS,
        "status_labels": STATUS_LABELS,
        "step_status_labels": STEP_STATUS_LABELS,
        "step_status_colors": STEP_STATUS_COLORS,
        "valid_protocol_types": VALID_PROTOCOL_TYPES,
        "valid_statuses": VALID_STATUSES,
        "now": datetime.now(timezone.utc),
    }


# ── List ────────────────────────────────────────────────────────────────


@protocols_bp.route("/")
@login_required
def protocol_list() -> str:
    """Render the protocol list view."""
    status_filter = request.args.get("status", "").strip()
    type_filter = request.args.get("type", "").strip()

    protocols = list_protocols(
        status_filter=status_filter or None,
        protocol_type_filter=type_filter or None,
    )

    ctx = _template_context()
    ctx.update(
        protocols=protocols,
        status_filter=status_filter,
        type_filter=type_filter,
    )

    if _is_htmx():
        return render_template("protocols/_protocol_rows.html", **ctx)

    return render_template("protocols/list.html", **ctx)


# ── Create (wizard flow) ───────────────────────────────────────────────


@protocols_bp.route("/new")
@login_required
def protocol_new() -> str:
    """Render the protocol creation page. Requires dossier_id query param."""
    dossier_id = request.args.get("dossier_id", "")
    if not dossier_id:
        return redirect(url_for("dossiers.dossier_list"))

    dossier = get_dossier(dossier_id)
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))

    # Check if dossier already has an active protocol
    existing = get_protocol_for_dossier(dossier_id)
    if existing and existing.get("status") == "actif":
        return redirect(
            url_for("protocols.protocol_detail", protocol_id=existing["id"])
        )

    ctx = _template_context()
    ctx.update(dossier=dossier, protocol=None, errors=[])
    return render_template("protocols/form.html", **ctx)


@protocols_bp.route("/", methods=["POST"])
@login_required
def protocol_create() -> str:
    """Handle protocol creation form submission."""
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    protocol_type = f.get("protocol_type", "").strip()
    start_date = _parse_date(f.get("start_date", ""))
    auto_create_tasks = f.get("auto_create_tasks") == "on"

    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        ctx = _template_context()
        ctx.update(dossier=None, protocol=None, errors=["Dossier introuvable."])
        return render_template("protocols/form.html", **ctx)

    data = {
        "title": f.get("title", "").strip() or "Protocole de l'instance",
        "court": dossier.get("court", ""),
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "notes": f.get("notes", "").strip(),
    }

    protocol, errors = create_protocol(
        dossier_id=dossier_id,
        protocol_type=protocol_type,
        start_date=start_date,
        data=data,
        auto_create_tasks=auto_create_tasks,
    )

    if errors:
        ctx = _template_context()
        ctx.update(dossier=dossier, protocol=data, errors=errors)
        return render_template("protocols/form.html", **ctx)

    target = url_for("protocols.protocol_detail", protocol_id=protocol["id"])
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Detail ──────────────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>")
@login_required
def protocol_detail(protocol_id: str) -> str:
    """Render the protocol detail view with timeline."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    # Check overdue steps
    check_overdue_steps(protocol_id)
    # Reload after overdue check
    protocol = get_protocol(protocol_id)

    ctx = _template_context()
    ctx["protocol"] = protocol

    # Compute progress
    steps = protocol.get("steps", [])
    total = len(steps)
    completed = sum(1 for s in steps if s.get("status") == "complété")
    ctx["progress_pct"] = int((completed / total * 100) if total > 0 else 0)
    ctx["steps_completed"] = completed
    ctx["steps_total"] = total

    # Compute days remaining/overdue for each step
    now = datetime.now(timezone.utc)
    for step in steps:
        deadline = step.get("deadline_date")
        if deadline and step.get("status") != "complété":
            delta = (deadline - now).days
            step["_days_remaining"] = delta
        else:
            step["_days_remaining"] = None

    return render_template("protocols/detail.html", **ctx)


# ── Edit protocol metadata ─────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/edit")
@login_required
def protocol_edit(protocol_id: str) -> str:
    """Render the protocol edit form."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    dossier = get_dossier(protocol["dossier_id"]) if protocol.get("dossier_id") else None

    ctx = _template_context()
    ctx.update(protocol=protocol, dossier=dossier, errors=[], edit_mode=True)
    return render_template("protocols/form.html", **ctx)


@protocols_bp.route("/<protocol_id>", methods=["POST"])
@login_required
def protocol_update(protocol_id: str) -> str:
    """Handle protocol metadata edit."""
    f = request.form
    data = {
        "title": f.get("title", "").strip(),
        "notes": f.get("notes", "").strip(),
        "status": f.get("status", "actif"),
    }

    # Handle start date change
    new_start_date = _parse_date(f.get("start_date", ""))
    protocol = get_protocol(protocol_id)

    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    # Check if start date changed and recompute if needed
    recompute = False
    if new_start_date and protocol.get("start_date"):
        if new_start_date != protocol["start_date"]:
            recompute = True
            data["start_date"] = new_start_date

    updated, errors = update_protocol(protocol_id, data)

    if errors:
        dossier = get_dossier(protocol["dossier_id"]) if protocol.get("dossier_id") else None
        ctx = _template_context()
        ctx.update(protocol=protocol, dossier=dossier, errors=errors, edit_mode=True)
        return render_template("protocols/form.html", **ctx)

    if recompute:
        recompute_deadlines(protocol_id, new_start_date)

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Delete protocol ─────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/delete", methods=["POST"])
@login_required
def protocol_delete(protocol_id: str) -> str:
    """Delete a protocol and redirect to dossier."""
    protocol = get_protocol(protocol_id)
    dossier_id = protocol.get("dossier_id") if protocol else None

    success, error = delete_protocol(protocol_id)

    target = (
        url_for("dossiers.dossier_detail", dossier_id=dossier_id)
        if dossier_id
        else url_for("dossiers.dossier_list")
    )

    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(target)


# ── Step operations ─────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/steps", methods=["POST"])
@login_required
def step_add(protocol_id: str) -> str:
    """Add a new custom step to the protocol."""
    f = request.form
    step_data = {
        "title": f.get("title", "").strip(),
        "description": f.get("description", "").strip(),
        "cpc_reference": f.get("cpc_reference", "").strip(),
        "deadline_date": _parse_date(f.get("deadline_date", "")),
        "mandatory": False,
        "deadline_locked": False,
    }

    step, errors = add_step(protocol_id, step_data)

    if errors and _is_htmx():
        return f'<div class="text-red-600 text-sm p-3">{errors[0]}</div>', 422

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@protocols_bp.route("/<protocol_id>/steps/<step_id>", methods=["POST"])
@login_required
def step_update(protocol_id: str, step_id: str) -> str:
    """Update a step (deadline, notes, etc.)."""
    f = request.form
    data = {}

    if f.get("deadline_date") is not None:
        data["deadline_date"] = _parse_date(f.get("deadline_date", ""))
    if f.get("notes") is not None:
        data["notes"] = f.get("notes", "").strip()
    if f.get("status"):
        data["status"] = f.get("status", "")

    step, errors = update_step(protocol_id, step_id, data)

    if errors and _is_htmx():
        return f'<div class="text-red-600 text-sm p-3">{errors[0]}</div>', 422

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@protocols_bp.route("/<protocol_id>/steps/<step_id>/complete", methods=["POST"])
@login_required
def step_complete(protocol_id: str, step_id: str) -> str:
    """Toggle step completion status."""
    step, errors = complete_step(protocol_id, step_id)

    if errors and _is_htmx():
        return f'<div class="text-red-600 text-sm p-3">{errors[0]}</div>', 422

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@protocols_bp.route("/<protocol_id>/steps/<step_id>/delete", methods=["POST"])
@login_required
def step_delete(protocol_id: str, step_id: str) -> str:
    """Delete a custom (non-mandatory) step."""
    success, error = delete_step(protocol_id, step_id)

    if not success and _is_htmx():
        return f'<div class="text-red-600 text-sm p-3">{error}</div>', 422

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)
