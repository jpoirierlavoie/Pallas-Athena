"""Time tracking and expense management routes."""

import math
from datetime import datetime, timezone

from markupsafe import escape

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
from security import safe_internal_redirect
from pagination import PAGE_SIZE, cursor_pagination, paginate, parse_trail
from models.time_entry import (
    QUICK_DESCRIPTIONS,
    create_time_entry,
    delete_time_entry,
    get_filtered_time_totals,
    get_time_entry,
    list_time_entries,
    list_time_entries_page,
    update_time_entry,
)
from models.expense import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    create_expense,
    delete_expense,
    get_expense,
    get_filtered_expense_totals,
    list_expenses,
    list_expenses_page,
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
        cents = float(value.strip().replace(",", ".")) * 100
        # Reject NaN/Infinity ("nan"/"inf" parse as floats but corrupt totals)
        if not math.isfinite(cents):
            return 0
        return int(round(cents))
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
        hours = float(value.strip().replace(",", "."))
        # Reject NaN/Infinity ("nan"/"inf" parse as floats but corrupt totals)
        if not math.isfinite(hours):
            return 0.0
        return round(hours, 1)
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
        # Escape stored values: this fragment bypasses Jinja autoescaping
        dossier_id = escape(d["id"])
        file_number = escape(d.get("file_number", ""))
        title = escape(d.get("title", ""))
        rate = escape(d.get("hourly_rate", 0))
        html_parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f'    data-dossier-id="{dossier_id}"'
            f'    data-dossier-file-number="{file_number}"'
            f'    data-dossier-title="{title}"'
            f'    data-dossier-rate="{rate}">'
            f'  <span class="font-medium text-gray-900">{file_number}</span>'
            f'  <span class="text-gray-500 ml-1">{title}</span>'
            f'</li>'
        )
    html_parts.append('</ul>')
    return "\n".join(html_parts)


# ── Standalone list ──────────────────────────────────────────────────────


@time_expenses_bp.route("/")
@login_required
def time_list() -> str:
    """Render the standalone time & expense list.

    Both tabs use Firestore-native cursor pagination (~PAGE_SIZE reads per
    page) with server-side filters, and an aggregation query for the running
    totals. One exception stays on the legacy in-memory path: dossier_id
    combined with the billable/invoiced filter — each such pairing would
    need its own composite index, and a dossier-scoped result set stays
    small enough for a full scan.
    """
    active_tab = request.args.get("tab", "heures")
    billable_filter = request.args.get("filter", "")
    dossier_id = request.args.get("dossier_id", "").strip()
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    page = request.args.get("page", 1, type=int)
    cursor = request.args.get("cursor", "") or None
    trail = parse_trail(request.args.get("trail", ""))

    ctx = _template_context()
    ctx.update(
        active_tab=active_tab,
        billable_filter=billable_filter,
        dossier_id=dossier_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
    )

    list_url = url_for("time_expenses.time_list")
    rows_target = "#entry-rows"
    filters = {
        "dossier_id": dossier_id or None,
        "billable_filter": billable_filter or None,
        "date_from": date_from,
        "date_to": date_to,
    }
    # Rare combo without its own composite index → legacy full-scan path.
    use_legacy = bool(dossier_id and billable_filter)

    if active_tab == "depenses":
        if use_legacy:
            entries = list_expenses(**filters)
            ctx["total_amount"] = sum(e.get("amount", 0) for e in entries)
            entries, pagination = paginate(entries, page)
            pagination.update(url=list_url, target=rows_target)
        else:
            entries, next_cursor = list_expenses_page(
                **filters, limit=PAGE_SIZE, cursor=cursor
            )
            ctx["total_amount"] = get_filtered_expense_totals(**filters)["amount"]
            pagination = cursor_pagination(
                cursor=cursor,
                trail=trail,
                next_cursor=next_cursor,
                url=list_url,
                target=rows_target,
                extra_vals={"tab": "depenses"},
            )
        ctx["expenses"] = entries
        ctx["pagination"] = pagination

        if _is_htmx():
            return render_template("time_expenses/_expense_rows.html", **ctx)
    else:
        if use_legacy:
            entries = list_time_entries(**filters)
            ctx["total_hours"] = round(sum(e.get("hours", 0) for e in entries), 1)
            ctx["total_amount"] = sum(e.get("amount", 0) for e in entries)
            entries, pagination = paginate(entries, page)
            pagination.update(url=list_url, target=rows_target)
        else:
            entries, next_cursor = list_time_entries_page(
                **filters, limit=PAGE_SIZE, cursor=cursor
            )
            totals = get_filtered_time_totals(**filters)
            ctx["total_hours"] = totals["hours"]
            ctx["total_amount"] = totals["amount"]
            pagination = cursor_pagination(
                cursor=cursor,
                trail=trail,
                next_cursor=next_cursor,
                url=list_url,
                target=rows_target,
                extra_vals={"tab": "heures"},
            )
        ctx["time_entries"] = entries
        ctx["pagination"] = pagination

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
    ctx.update(entry=prefilled, errors=[], return_to=request.args.get("return_to", ""))
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
    return_to = f.get("return_to", "")

    entry, errors = create_time_entry(data)

    if errors:
        ctx = _template_context()
        # Preserve form display values
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(entry=data, errors=errors, return_to=return_to)
        return render_template("time_expenses/time_form.html", **ctx)

    target = safe_internal_redirect(return_to, url_for("time_expenses.time_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@time_expenses_bp.route("/<entry_id>/edit")
@login_required
def time_entry_edit(entry_id: str) -> str:
    """Render the edit form pre-filled with time entry data."""
    entry = get_time_entry(entry_id)
    if not entry:
        return redirect(url_for("time_expenses.time_list"))

    ctx = _template_context()
    ctx.update(entry=entry, errors=[], return_to=request.args.get("return_to", ""))
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
    return_to = f.get("return_to", "")

    entry, errors = update_time_entry(entry_id, data)

    if errors:
        data["id"] = entry_id
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(entry=data, errors=errors, return_to=return_to)
        return render_template("time_expenses/time_form.html", **ctx)

    target = safe_internal_redirect(return_to, url_for("time_expenses.time_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@time_expenses_bp.route("/<entry_id>/delete", methods=["POST"])
@login_required
def time_entry_delete(entry_id: str) -> str:
    """Delete a time entry and redirect to the list (or back to the caller)."""
    return_to = request.form.get("return_to", "")
    success, error = delete_time_entry(entry_id)

    target = safe_internal_redirect(return_to, url_for("time_expenses.time_list"))
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(target)


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
    ctx.update(expense=prefilled, errors=[], return_to=request.args.get("return_to", ""))
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
    return_to = f.get("return_to", "")

    expense, errors = create_expense(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(expense=data, errors=errors, return_to=return_to)
        return render_template("time_expenses/expense_form.html", **ctx)

    fallback = url_for("time_expenses.time_list", tab="depenses")
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@time_expenses_bp.route("/depenses/<expense_id>/edit")
@login_required
def expense_edit(expense_id: str) -> str:
    """Render the edit form pre-filled with expense data."""
    expense = get_expense(expense_id)
    if not expense:
        return redirect(url_for("time_expenses.time_list", tab="depenses"))

    ctx = _template_context()
    ctx.update(expense=expense, errors=[], return_to=request.args.get("return_to", ""))
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
    return_to = f.get("return_to", "")

    expense, errors = update_expense(expense_id, data)

    if errors:
        data["id"] = expense_id
        data["dossier_file_number"] = data.get("dossier_file_number", f.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(expense=data, errors=errors, return_to=return_to)
        return render_template("time_expenses/expense_form.html", **ctx)

    fallback = url_for("time_expenses.time_list", tab="depenses")
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


@time_expenses_bp.route("/depenses/<expense_id>/delete", methods=["POST"])
@login_required
def expense_delete(expense_id: str) -> str:
    """Delete an expense and redirect to the list (or back to the caller)."""
    return_to = request.form.get("return_to", "")
    success, error = delete_expense(expense_id)

    fallback = url_for("time_expenses.time_list", tab="depenses")
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(target)


# ── Export ───────────────────────────────────────────────────────────────


_TIME_EXPORT_COLUMNS_CSV = [
    ("date", "Date"),
    ("dossier_file_number", "Dossier"),
    ("description", "Description"),
    ("hours", "Heures"),
    ("rate", "Taux"),
    ("amount", "Montant"),
    ("billable", "Facturable"),
    ("invoiced", "Facturé"),
]

_TIME_EXPORT_COLUMNS_PDF = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.5),
    ("hours", "Heures", 0.6),
    ("rate", "Taux", 0.8),
    ("amount", "Montant", 0.8),
    ("billable", "Facturable", 0.6),
    ("invoiced", "Facturé", 0.6),
]

_EXPENSE_EXPORT_COLUMNS_CSV = [
    ("date", "Date"),
    ("dossier_file_number", "Dossier"),
    ("description", "Description"),
    ("category", "Catégorie"),
    ("amount", "Montant"),
    ("taxable", "Taxable"),
    ("invoiced", "Facturé"),
]

_EXPENSE_EXPORT_COLUMNS_PDF = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.5),
    ("category", "Catégorie", 1.0),
    ("amount", "Montant", 0.8),
    ("taxable", "Taxable", 0.6),
    ("invoiced", "Facturé", 0.6),
]


def _get_export_filters() -> tuple:
    """Read shared filter params for time/expense exports."""
    dossier_id = request.args.get("dossier_id", "").strip()
    billable_filter = request.args.get("filter", "")
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    return dossier_id, billable_filter, date_from, date_to


@time_expenses_bp.route("/export/csv")
@login_required
def export_time_csv_route() -> Response:
    """Export time entries as CSV."""
    from utils.export_csv import export_csv

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    entries = list_time_entries(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=entries,
        columns=_TIME_EXPORT_COLUMNS_CSV,
        filename=f"heures_{date_str}.csv",
        cents_fields=["rate", "amount"],
        hours_fields=["hours"],
    )


@time_expenses_bp.route("/export/pdf")
@login_required
def export_time_pdf_route() -> Response:
    """Export time entries as PDF report."""
    from utils.export_pdf import export_pdf

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    entries = list_time_entries(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf(
        rows=entries,
        columns=_TIME_EXPORT_COLUMNS_PDF,
        title="Heures",
        filename=f"heures_{date_str}.pdf",
        cents_fields=["rate", "amount"],
        hours_fields=["hours"],
    )


@time_expenses_bp.route("/depenses/export/csv")
@login_required
def export_expense_csv_route() -> Response:
    """Export expenses as CSV."""
    from utils.export_csv import export_csv, prepare_export_rows

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    expenses = list_expenses(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    expenses = prepare_export_rows(expenses, label_maps={"category": CATEGORY_LABELS})
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=expenses,
        columns=_EXPENSE_EXPORT_COLUMNS_CSV,
        filename=f"depenses_{date_str}.csv",
        cents_fields=["amount"],
    )


@time_expenses_bp.route("/depenses/export/pdf")
@login_required
def export_expense_pdf_route() -> Response:
    """Export expenses as PDF report."""
    from utils.export_pdf import export_pdf
    from utils.export_csv import prepare_export_rows

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    expenses = list_expenses(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    expenses = prepare_export_rows(expenses, label_maps={"category": CATEGORY_LABELS})
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf(
        rows=expenses,
        columns=_EXPENSE_EXPORT_COLUMNS_PDF,
        title="Dépenses",
        filename=f"depenses_{date_str}.pdf",
        cents_fields=["amount"],
    )
