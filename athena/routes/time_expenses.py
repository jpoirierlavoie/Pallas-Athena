"""Time tracking and expense management routes."""

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
from models.time_entry import (
    QUICK_DESCRIPTIONS,
    create_time_entry,
    delete_time_entry,
    get_time_entry,
    list_time_entries,
    update_time_entry,
)
from models.expense import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    create_expense,
    delete_expense,
    get_expense,
    list_expenses,
    update_expense,
)
from models.dossier import (
    get_dossier,
    list_dossiers,
)

time_expenses_bp = Blueprint(
    "time_expenses", __name__, url_prefix="/temps"
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


def _parse_hours(value: str) -> float:
    """Parse an hours string (e.g., '1.5') into a float."""
    if not value or not value.strip():
        return 0.0
    try:
        return round(float(value.strip().replace(",", ".")), 1)
    except (ValueError, TypeError):
        return 0.0


def _template_context() -> dict:
    """Return shared template context for time/expense views."""
    return {
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
        "quick_descriptions": QUICK_DESCRIPTIONS,
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
    return data


# ── Dossier search (for autocomplete in forms) ───────────────────────────


@time_expenses_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    """HTMX autocomplete endpoint for dossier selection."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return '<div class="px-3 py-2 text-sm text-gray-500">Tapez au moins 2 caractères…</div>'

    dossiers = list_dossiers(search=q)[:10]

    if not dossiers:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun dossier trouvé</div>'

    html_parts = ['<ul class="divide-y divide-gray-100">']
    for d in dossiers:
        html_parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f'    data-dossier-id="{d["id"]}"'
            f'    data-dossier-file-number="{d.get("file_number", "")}"'
            f'    data-dossier-title="{d.get("title", "")}"'
            f'    data-dossier-rate="{d.get("hourly_rate", 0)}">'
            f'  <span class="font-medium text-gray-900">{d.get("file_number", "")}</span>'
            f'  <span class="text-gray-500 ml-1">{d.get("title", "")}</span>'
            f'</li>'
        )
    html_parts.append('</ul>')
    return "\n".join(html_parts)


# ── Standalone list ──────────────────────────────────────────────────────


@time_expenses_bp.route("/")
@login_required
def time_list() -> str:
    """Render the standalone time & expense list."""
    active_tab = request.args.get("tab", "heures")
    billable_filter = request.args.get("filter", "")
    dossier_id = request.args.get("dossier_id", "").strip()
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))

    ctx = _template_context()
    ctx.update(
        active_tab=active_tab,
        billable_filter=billable_filter,
        dossier_id=dossier_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )

    if active_tab == "depenses":
        entries = list_expenses(
            dossier_id=dossier_id or None,
            billable_filter=billable_filter or None,
            date_from=date_from,
            date_to=date_to,
        )
        ctx["expenses"] = entries
        ctx["total_amount"] = sum(e.get("amount", 0) for e in entries)

        if _is_htmx():
            return render_template("time_expenses/_expense_rows.html", **ctx)
    else:
        entries = list_time_entries(
            dossier_id=dossier_id or None,
            billable_filter=billable_filter or None,
            date_from=date_from,
            date_to=date_to,
        )
        ctx["time_entries"] = entries
        ctx["total_hours"] = round(sum(e.get("hours", 0) for e in entries), 1)
        ctx["total_amount"] = sum(e.get("amount", 0) for e in entries)

        if _is_htmx():
            return render_template("time_expenses/_time_rows.html", **ctx)

    return render_template("time_expenses/list.html", **ctx)


# ── Time entry CRUD ──────────────────────────────────────────────────────


@time_expenses_bp.route("/new")
@login_required
def time_entry_new() -> str:
    """Render the empty time entry form."""
    ctx = _template_context()
    # Pre-fill dossier if provided via query string
    dossier_id = request.args.get("dossier_id", "")
    prefilled = None
    if dossier_id:
        dossier = get_dossier(dossier_id)
        if dossier:
            prefilled = {
                "dossier_id": dossier["id"],
                "dossier_file_number": dossier.get("file_number", ""),
                "dossier_title": dossier.get("title", ""),
                "rate": dossier.get("hourly_rate", 0),
            }
    ctx.update(entry=prefilled, errors=[])
    return render_template("time_expenses/time_form.html", **ctx)


@time_expenses_bp.route("/", methods=["POST"])
@login_required
def time_entry_create() -> str:
    """Handle new time entry form submission."""
    f = request.form
    data = {
        "dossier_id": f.get("dossier_id", "").strip(),
        "date": _parse_date(f.get("date", "")),
        "description": f.get("description", "").strip(),
        "hours": _parse_hours(f.get("hours", "")),
        "rate": _parse_cents(f.get("rate", "")),
        "billable": f.get("billable") == "on",
    }
    data = _enrich_dossier_info(data)

    entry, errors = create_time_entry(data)

    if errors:
        ctx = _template_context()
        # Preserve form display values
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(entry=data, errors=errors)
        return render_template("time_expenses/time_form.html", **ctx)

    if _is_htmx():
        resp = redirect(url_for("time_expenses.time_list"))
        resp.headers["HX-Redirect"] = url_for("time_expenses.time_list")
        return resp

    return redirect(url_for("time_expenses.time_list"))


@time_expenses_bp.route("/<entry_id>/edit")
@login_required
def time_entry_edit(entry_id: str) -> str:
    """Render the edit form pre-filled with time entry data."""
    entry = get_time_entry(entry_id)
    if not entry:
        return redirect(url_for("time_expenses.time_list"))

    ctx = _template_context()
    ctx.update(entry=entry, errors=[])
    return render_template("time_expenses/time_form.html", **ctx)


@time_expenses_bp.route("/<entry_id>", methods=["POST"])
@login_required
def time_entry_update(entry_id: str) -> str:
    """Handle edit form submission for time entry."""
    f = request.form
    data = {
        "dossier_id": f.get("dossier_id", "").strip(),
        "date": _parse_date(f.get("date", "")),
        "description": f.get("description", "").strip(),
        "hours": _parse_hours(f.get("hours", "")),
        "rate": _parse_cents(f.get("rate", "")),
        "billable": f.get("billable") == "on",
    }
    data = _enrich_dossier_info(data)

    entry, errors = update_time_entry(entry_id, data)

    if errors:
        data["id"] = entry_id
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(entry=data, errors=errors)
        return render_template("time_expenses/time_form.html", **ctx)

    if _is_htmx():
        resp = redirect(url_for("time_expenses.time_list"))
        resp.headers["HX-Redirect"] = url_for("time_expenses.time_list")
        return resp

    return redirect(url_for("time_expenses.time_list"))


@time_expenses_bp.route("/<entry_id>/delete", methods=["POST"])
@login_required
def time_entry_delete(entry_id: str) -> str:
    """Delete a time entry and redirect to the list."""
    success, error = delete_time_entry(entry_id)

    if _is_htmx():
        if success:
            resp = redirect(url_for("time_expenses.time_list"))
            resp.headers["HX-Redirect"] = url_for("time_expenses.time_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("time_expenses.time_list"))


# ── Expense CRUD ─────────────────────────────────────────────────────────


@time_expenses_bp.route("/depenses/new")
@login_required
def expense_new() -> str:
    """Render the empty expense form."""
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
    ctx.update(expense=prefilled, errors=[])
    return render_template("time_expenses/expense_form.html", **ctx)


@time_expenses_bp.route("/depenses", methods=["POST"])
@login_required
def expense_create() -> str:
    """Handle new expense form submission."""
    f = request.form
    data = {
        "dossier_id": f.get("dossier_id", "").strip(),
        "date": _parse_date(f.get("date", "")),
        "description": f.get("description", "").strip(),
        "category": f.get("category", "autre"),
        "amount": _parse_cents(f.get("amount", "")),
        "taxable": f.get("taxable") == "on",
    }
    data = _enrich_dossier_info(data)

    expense, errors = create_expense(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(expense=data, errors=errors)
        return render_template("time_expenses/expense_form.html", **ctx)

    if _is_htmx():
        resp = redirect(url_for("time_expenses.time_list", tab="depenses"))
        resp.headers["HX-Redirect"] = url_for("time_expenses.time_list", tab="depenses")
        return resp

    return redirect(url_for("time_expenses.time_list", tab="depenses"))


@time_expenses_bp.route("/depenses/<expense_id>/edit")
@login_required
def expense_edit(expense_id: str) -> str:
    """Render the edit form pre-filled with expense data."""
    expense = get_expense(expense_id)
    if not expense:
        return redirect(url_for("time_expenses.time_list", tab="depenses"))

    ctx = _template_context()
    ctx.update(expense=expense, errors=[])
    return render_template("time_expenses/expense_form.html", **ctx)


@time_expenses_bp.route("/depenses/<expense_id>", methods=["POST"])
@login_required
def expense_update(expense_id: str) -> str:
    """Handle edit form submission for expense."""
    f = request.form
    data = {
        "dossier_id": f.get("dossier_id", "").strip(),
        "date": _parse_date(f.get("date", "")),
        "description": f.get("description", "").strip(),
        "category": f.get("category", "autre"),
        "amount": _parse_cents(f.get("amount", "")),
        "taxable": f.get("taxable") == "on",
    }
    data = _enrich_dossier_info(data)

    expense, errors = update_expense(expense_id, data)

    if errors:
        data["id"] = expense_id
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(expense=data, errors=errors)
        return render_template("time_expenses/expense_form.html", **ctx)

    if _is_htmx():
        resp = redirect(url_for("time_expenses.time_list", tab="depenses"))
        resp.headers["HX-Redirect"] = url_for("time_expenses.time_list", tab="depenses")
        return resp

    return redirect(url_for("time_expenses.time_list", tab="depenses"))


@time_expenses_bp.route("/depenses/<expense_id>/delete", methods=["POST"])
@login_required
def expense_delete(expense_id: str) -> str:
    """Delete an expense and redirect to the list."""
    success, error = delete_expense(expense_id)

    if _is_htmx():
        if success:
            resp = redirect(url_for("time_expenses.time_list", tab="depenses"))
            resp.headers["HX-Redirect"] = url_for("time_expenses.time_list", tab="depenses")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("time_expenses.time_list", tab="depenses"))
