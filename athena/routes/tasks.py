"""Task management routes — list, detail, create, edit, delete, toggle."""

from datetime import datetime, timezone

from tz import MTL

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import bump_ctag, record_tombstone
from models.task import (
    CATEGORY_LABELS,
    PRIORITY_COLORS,
    PRIORITY_LABELS,
    STATUS_LABELS,
    VALID_CATEGORIES,
    VALID_PRIORITIES,
    VALID_STATUSES,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    toggle_task_complete,
    update_task,
)
from models.dossier import (
    get_dossier,
    list_dossiers,
)

tasks_bp = Blueprint("tasks", __name__, url_prefix="/taches")


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
    """Return shared template context for task views."""
    return {
        "priority_labels": PRIORITY_LABELS,
        "priority_colors": PRIORITY_COLORS,
        "status_labels": STATUS_LABELS,
        "category_labels": CATEGORY_LABELS,
        "valid_priorities": VALID_PRIORITIES,
        "valid_statuses": VALID_STATUSES,
        "valid_categories": VALID_CATEGORIES,
        "today": datetime.now(MTL).strftime("%Y-%m-%d"),
    }


def _enrich_dossier_info(data: dict) -> dict:
    """Look up dossier and attach denormalized file_number + title."""
    dossier_id = data.get("dossier_id", "")
    if dossier_id:
        dossier = get_dossier(dossier_id)
        if dossier:
            data["dossier_file_number"] = dossier.get("file_number", "")
            data["dossier_title"] = dossier.get("title", "")
        else:
            # Invalid dossier ID — clear it
            data["dossier_id"] = None
            data["dossier_file_number"] = ""
            data["dossier_title"] = ""
    else:
        data["dossier_id"] = None
        data["dossier_file_number"] = ""
        data["dossier_title"] = ""
    return data


def _form_data() -> dict:
    """Extract task fields from the submitted form."""
    f = request.form
    return {
        "dossier_id": f.get("dossier_id", "").strip() or None,
        "title": f.get("title", "").strip(),
        "description": f.get("description", "").strip(),
        "priority": f.get("priority", "normale"),
        "status": f.get("status", "à_faire"),
        "category": f.get("category", "autre"),
        "due_date": _parse_date(f.get("due_date", "")),
    }


# ── Dossier search (for autocomplete in forms) ───────────────────────────


@tasks_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    """HTMX autocomplete endpoint for dossier selection."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return '<div class="px-3 py-2 text-sm text-gray-500">Tapez au moins 2 caractères\u2026</div>'

    dossiers = list_dossiers(search=q)[:10]

    if not dossiers:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun dossier trouvé</div>'

    html_parts = ['<ul class="divide-y divide-gray-100">']
    for d in dossiers:
        html_parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f'    data-dossier-id="{d["id"]}"'
            f'    data-dossier-file-number="{d.get("file_number", "")}"'
            f'    data-dossier-title="{d.get("title", "")}">'
            f'  <span class="font-medium text-gray-900">{d.get("file_number", "")}</span>'
            f'  <span class="text-gray-500 ml-1">{d.get("title", "")}</span>'
            f'</li>'
        )
    html_parts.append("</ul>")
    return "\n".join(html_parts)


# ── List ─────────────────────────────────────────────────────────────────


@tasks_bp.route("/")
@login_required
def task_list() -> str:
    """Render the task list view, grouped by status."""
    dossier_filter = request.args.get("dossier", "").strip()
    priority_filter = request.args.get("priority", "").strip()
    category_filter = request.args.get("category", "").strip()

    tasks = list_tasks(
        dossier_id=dossier_filter or None,
        priority_filter=priority_filter or None,
        category_filter=category_filter or None,
    )

    now = datetime.now(timezone.utc)

    # Group tasks by status
    active_tasks = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]
    completed_tasks = [t for t in tasks if t.get("status") == "terminée"]
    cancelled_tasks = [t for t in tasks if t.get("status") == "annulée"]

    ctx = _template_context()
    ctx.update(
        active_tasks=active_tasks,
        completed_tasks=completed_tasks,
        cancelled_tasks=cancelled_tasks,
        all_tasks=tasks,
        now=now,
        dossier_filter=dossier_filter,
        priority_filter=priority_filter,
        category_filter=category_filter,
    )

    if _is_htmx():
        return render_template("tasks/_task_rows.html", **ctx)

    return render_template("tasks/list.html", **ctx)


# ── Create ───────────────────────────────────────────────────────────────


@tasks_bp.route("/new")
@login_required
def task_new() -> str:
    """Render the empty task form."""
    ctx = _template_context()
    dossier_id = request.args.get("dossier_id", "")
    prefilled = None
    if dossier_id:
        dossier = get_dossier(dossier_id)
        if dossier:
            prefilled = {
                "dossier_id": dossier["id"],
                "dossier_file_number": dossier.get("file_number", ""),
                "dossier_title": dossier.get("title", ""),
            }
    ctx.update(task=prefilled, errors=[])
    return render_template("tasks/form.html", **ctx)


@tasks_bp.route("/", methods=["POST"])
@login_required
def task_create() -> str:
    """Handle new task form submission."""
    data = _form_data()
    data = _enrich_dossier_info(data)

    task, errors = create_task(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get("dossier_file_number", request.form.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(task=data, errors=errors)
        return render_template("tasks/form.html", **ctx)

    bump_ctag("tasks")

    if _is_htmx():
        resp = redirect(url_for("tasks.task_list"))
        resp.headers["HX-Redirect"] = url_for("tasks.task_list")
        return resp

    return redirect(url_for("tasks.task_list"))


# ── Detail ───────────────────────────────────────────────────────────────


@tasks_bp.route("/<task_id>")
@login_required
def task_detail(task_id: str) -> str:
    """Render the task detail view."""
    task = get_task(task_id)
    if not task:
        return redirect(url_for("tasks.task_list"))

    ctx = _template_context()
    ctx["task"] = task
    ctx["now"] = datetime.now(timezone.utc)
    return render_template("tasks/detail.html", **ctx)


# ── Edit ─────────────────────────────────────────────────────────────────


@tasks_bp.route("/<task_id>/edit")
@login_required
def task_edit(task_id: str) -> str:
    """Render the edit form pre-filled with task data."""
    task = get_task(task_id)
    if not task:
        return redirect(url_for("tasks.task_list"))

    ctx = _template_context()
    ctx.update(task=task, errors=[])
    return render_template("tasks/form.html", **ctx)


@tasks_bp.route("/<task_id>", methods=["POST"])
@login_required
def task_update(task_id: str) -> str:
    """Handle edit form submission."""
    data = _form_data()
    data = _enrich_dossier_info(data)

    task, errors = update_task(task_id, data)

    if errors:
        data["id"] = task_id
        data["dossier_file_number"] = data.get("dossier_file_number", request.form.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(task=data, errors=errors)
        return render_template("tasks/form.html", **ctx)

    bump_ctag("tasks")

    if _is_htmx():
        resp = redirect(url_for("tasks.task_detail", task_id=task_id))
        resp.headers["HX-Redirect"] = url_for("tasks.task_detail", task_id=task_id)
        return resp

    return redirect(url_for("tasks.task_detail", task_id=task_id))


# ── Delete ───────────────────────────────────────────────────────────────


@tasks_bp.route("/<task_id>/delete", methods=["POST"])
@login_required
def task_delete(task_id: str) -> str:
    """Delete a task and redirect to the list."""
    success, error = delete_task(task_id)

    if success:
        record_tombstone("tasks", task_id)
        bump_ctag("tasks")

    if _is_htmx():
        if success:
            resp = redirect(url_for("tasks.task_list"))
            resp.headers["HX-Redirect"] = url_for("tasks.task_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("tasks.task_list"))


# ── Toggle complete (HTMX) ──────────────────────────────────────────────


@tasks_bp.route("/<task_id>/toggle", methods=["POST"])
@login_required
def task_toggle(task_id: str) -> str:
    """Toggle task completion status. Returns updated task list for HTMX."""
    task, errors = toggle_task_complete(task_id)

    if errors:
        return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422

    bump_ctag("tasks")

    if _is_htmx():
        # From the list page: re-fetch all tasks and return the full grouped list
        now = datetime.now(timezone.utc)
        tasks = list_tasks()
        active_tasks = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]
        completed_tasks = [t for t in tasks if t.get("status") == "terminée"]
        cancelled_tasks = [t for t in tasks if t.get("status") == "annulée"]

        ctx = _template_context()
        ctx.update(
            active_tasks=active_tasks,
            completed_tasks=completed_tasks,
            cancelled_tasks=cancelled_tasks,
            now=now,
        )
        return render_template("tasks/_task_rows.html", **ctx)

    # Non-HTMX (e.g. detail page form): redirect back to the task detail
    return redirect(url_for("tasks.task_detail", task_id=task_id))
