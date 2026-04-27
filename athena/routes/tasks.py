"""Task management routes — list, detail, create, edit, delete, toggle."""

from datetime import datetime, timezone

from tz import MTL

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
    get_dossiers_bulk,
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
        "related_note_id": f.get("related_note_id", "").strip() or None,
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
    status_filter = request.args.get("status", "").strip()
    priority_filter = request.args.get("priority", "").strip()
    category_filter = request.args.get("category", "").strip()

    tasks = list_tasks(
        dossier_id=dossier_filter or None,
        status_filter=status_filter or None,
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
        status_filter=status_filter,
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
    related_note_id = request.args.get("related_note_id", "")
    prefilled = None

    if related_note_id:
        from models.note import get_note
        note = get_note(related_note_id)
        if note:
            dossier_id = note.get("dossier_id", "")
            dossier = get_dossier(dossier_id) if dossier_id else None
            prefilled = {
                "related_note_id": related_note_id,
                "dossier_id": dossier_id,
                "dossier_file_number": dossier.get("file_number", "") if dossier else "",
                "dossier_title": dossier.get("title", "") if dossier else "",
            }

    if not prefilled and dossier_id:
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

    if task.get("dossier_id"):
        bump_ctag(f"dossier:{task['dossier_id']}")
    else:
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

    # Resolve related note for display
    related_note = None
    if task.get("related_note_id"):
        from models.note import get_note
        related_note = get_note(task["related_note_id"])
    ctx["related_note"] = related_note

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
    # Capture the old dossier_id before update
    existing_task = get_task(task_id)
    old_dossier_id = existing_task.get("dossier_id") if existing_task else None

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

    new_dossier_id = task.get("dossier_id")
    if old_dossier_id != new_dossier_id:
        # Task moved between dossiers (or to/from standalone)
        if old_dossier_id:
            record_tombstone(f"dossier:{old_dossier_id}", task_id)
            bump_ctag(f"dossier:{old_dossier_id}")
        else:
            record_tombstone("tasks", task_id)
            bump_ctag("tasks")
        if new_dossier_id:
            bump_ctag(f"dossier:{new_dossier_id}")
        else:
            bump_ctag("tasks")
    elif new_dossier_id:
        bump_ctag(f"dossier:{new_dossier_id}")
    else:
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
    existing_task = get_task(task_id)
    dossier_id = existing_task.get("dossier_id") if existing_task else None

    success, error = delete_task(task_id)

    if success:
        if dossier_id:
            record_tombstone(f"dossier:{dossier_id}", task_id)
            bump_ctag(f"dossier:{dossier_id}")
        else:
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

    if task.get("dossier_id"):
        bump_ctag(f"dossier:{task['dossier_id']}")
    else:
        bump_ctag("tasks")

    if _is_htmx():
        # Re-fetch with active filters (posted via hx-include on the toggle buttons)
        dossier_filter = request.form.get("dossier", "").strip()
        status_filter = request.form.get("status", "").strip()
        priority_filter = request.form.get("priority", "").strip()
        category_filter = request.form.get("category", "").strip()

        now = datetime.now(timezone.utc)
        tasks = list_tasks(
            dossier_id=dossier_filter or None,
            status_filter=status_filter or None,
            priority_filter=priority_filter or None,
            category_filter=category_filter or None,
        )
        active_tasks = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]
        completed_tasks = [t for t in tasks if t.get("status") == "terminée"]
        cancelled_tasks = [t for t in tasks if t.get("status") == "annulée"]

        ctx = _template_context()
        ctx.update(
            active_tasks=active_tasks,
            completed_tasks=completed_tasks,
            cancelled_tasks=cancelled_tasks,
            now=now,
            dossier_filter=dossier_filter,
            status_filter=status_filter,
            priority_filter=priority_filter,
            category_filter=category_filter,
        )
        return render_template("tasks/_task_rows.html", **ctx)

    # Non-HTMX (e.g. detail page form): redirect back to the task detail
    return redirect(url_for("tasks.task_detail", task_id=task_id))


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("title", "Titre"),
    ("dossier_file_number", "Dossier"),
    ("priority", "Priorité"),
    ("category", "Catégorie"),
    ("status", "Statut"),
    ("due_date", "Échéance"),
]

_EXPORT_COLUMNS_PDF = [
    ("title", "Titre", 3.0),
    ("priority", "Priorité", 0.8),
    ("category", "Catégorie", 1.0),
    ("status", "Statut", 0.8),
    ("due_date", "Échéance", 1.0),
]

_PRIORITY_RANK = {"haute": 0, "normale": 1, "basse": 2}


def _filtered_tasks() -> list[dict]:
    """Fetch tasks honouring the current query-string filters."""
    return list_tasks(
        dossier_id=request.args.get("dossier", "").strip() or None,
        status_filter=request.args.get("status", "").strip() or None,
        priority_filter=request.args.get("priority", "").strip() or None,
        category_filter=request.args.get("category", "").strip() or None,
    )


def _get_export_tasks() -> list[dict]:
    """Fetch and pre-process tasks for export (CSV — flat, label-mapped)."""
    from utils.export_csv import prepare_export_rows

    return prepare_export_rows(
        _filtered_tasks(),
        label_maps={
            "priority": PRIORITY_LABELS,
            "category": CATEGORY_LABELS,
            "status": STATUS_LABELS,
        },
    )


def _get_export_task_groups() -> list[tuple[str, list[dict]]]:
    """Group filtered tasks by dossier for the grouped PDF report.

    - Group label: "<file_number> — <title>" or "Sans dossier".
    - Group order: dossier file_number ascending; "Sans dossier" last.
    - Within-group order: due_date ascending (None last), then priority desc.
    - Single batched dossier fetch keeps titles fresh and avoids N+1 reads.
    """
    from utils.export_csv import prepare_export_rows

    tasks = _filtered_tasks()
    if not tasks:
        return []

    referenced_ids = [t["dossier_id"] for t in tasks if t.get("dossier_id")]
    dossiers_by_id = get_dossiers_bulk(referenced_ids) if referenced_ids else {}

    buckets: dict[str | None, list[dict]] = {}
    for task in tasks:
        key = task.get("dossier_id") or None
        buckets.setdefault(key, []).append(task)

    def task_sort_key(t: dict):
        due = t.get("due_date")
        priority_rank = _PRIORITY_RANK.get(t.get("priority", ""), 99)
        # Tasks without a due date sort after dated ones; priority ascending
        # rank means Haute (0) < Normale (1) < Basse (2) → highest priority first.
        return (
            1 if due is None else 0,
            due if due is not None else datetime.max.replace(tzinfo=timezone.utc),
            priority_rank,
        )

    def group_label_for(dossier_id: str | None, sample_task: dict) -> str:
        if dossier_id is None:
            return "Sans dossier"
        live = dossiers_by_id.get(dossier_id)
        if live:
            file_no = live.get("file_number", "") or sample_task.get("dossier_file_number", "")
            title = live.get("title", "") or sample_task.get("dossier_title", "")
        else:
            # Dossier deleted but tasks linger — fall back to denormalized values.
            file_no = sample_task.get("dossier_file_number", "")
            title = sample_task.get("dossier_title", "")
        if file_no and title:
            return f"{file_no} — {title}"
        return file_no or title or "Dossier inconnu"

    def group_sort_key(item: tuple[str | None, list[dict]]):
        dossier_id, group_tasks = item
        if dossier_id is None:
            return (1, "")  # "Sans dossier" → last
        live = dossiers_by_id.get(dossier_id) or {}
        file_no = live.get("file_number") or group_tasks[0].get("dossier_file_number", "")
        return (0, file_no)

    groups: list[tuple[str, list[dict]]] = []
    for dossier_id, group_tasks in sorted(buckets.items(), key=group_sort_key):
        sorted_tasks = sorted(group_tasks, key=task_sort_key)
        labelled = prepare_export_rows(
            sorted_tasks,
            label_maps={
                "priority": PRIORITY_LABELS,
                "category": CATEGORY_LABELS,
                "status": STATUS_LABELS,
            },
        )
        groups.append((group_label_for(dossier_id, group_tasks[0]), labelled))
    return groups


@tasks_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export tasks as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_tasks()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"taches_{date_str}.csv",
    )


@tasks_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export tasks as a PDF report grouped by dossier."""
    from utils.export_pdf import export_pdf_grouped

    groups = _get_export_task_groups()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf_grouped(
        groups=groups,
        columns=_EXPORT_COLUMNS_PDF,
        title="Tâches",
        filename=f"taches_{date_str}.pdf",
    )
