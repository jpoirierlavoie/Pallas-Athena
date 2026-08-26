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
from models.audit_event import record_deletion
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
    set_time_entry_phase,
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
    set_expense_phase,
    update_expense,
)
from models.dossier import (
    get_dossier,
)
from models.protocol import get_current_phase_for_dossier
from utils import phases
from utils.logging_setup import log_dossier_event
from routes._helpers import dossier_search_fragment, enrich_dossier_labels, is_htmx, parse_date_input, standard_dossier_row

time_expenses_bp = Blueprint(
    "time_expenses", __name__, url_prefix="/temps"
)


_is_htmx = is_htmx


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


_parse_date = parse_date_input


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
        # Phase O cascading picker (components/_phase_selector.html). Cached
        # in utils.phases, so handing it to every view costs a dict reference;
        # only the forms actually serialize it.
        "phases_payload": phases.form_payload(),
        "phase_labels": phases.PHASE_LABELS,
        "sous_phase_labels": phases.SOUS_PHASE_LABELS,
        "today": datetime.now(MTL).strftime("%Y-%m-%d"),
    }


def _phase_prefill(dossier_id: str, recent_rows: list[dict]) -> dict:
    """Suggested phase + recent codes for a form opened WITH a dossier.

    The protocol derivation costs ~10 reads — paid only when the dossier is
    already known at GET (the dossier-tab entry point, the dominant case);
    a blank form pays nothing and the DAV/MCP paths never come here.
    """
    if not dossier_id:
        return {"phase_default": "", "sous_phase_default": "",
                "phase_recents": []}
    phase, sous = get_current_phase_for_dossier(dossier_id)
    return {"phase_default": phase, "sous_phase_default": sous,
            "phase_recents": _recent_codes(recent_rows)}


def _recent_codes(recent_rows: list[dict]) -> list[str]:
    """The dossier's recently-used sub-codes, in order, deduplicated.

    Shared with the reclassification form, which offers the same chips but
    deliberately NOT the protocol-derived default: suggesting the dossier's
    CURRENT phase for a two-year-old billed entry would propose the wrong
    answer with the authority of a default.
    """
    recents: list[str] = []
    for row in recent_rows:
        code = row.get("sous_phase") or ""
        if code and code not in recents:
            recents.append(code)
    return recents[:8]


def _enrich_dossier_info(data: dict) -> dict:
    """Attach the denormalized dossier labels (shared helper).

    Conscious behavior change (audit 2026-08-26): an UNRESOLVABLE id used
    to be KEPT silently — the entry saved referencing a dead dossier with
    blank labels. It is now blanked, so the models' « Un dossier doit être
    associé… » validation refuses the save instead.
    """
    data, _ = enrich_dossier_labels(
        data, blank_value="", resolver=get_dossier
    )
    return data


# ── Dossier search (for autocomplete in forms) ───────────────────────────


@time_expenses_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    """HTMX autocomplete endpoint for dossier selection."""

    def _row(d: dict) -> str:
        # The dossier's rate rides along so the form can prefill it.
        rate = escape(d.get("hourly_rate", 0))
        return standard_dossier_row(
            d, extra_attrs=f' data-dossier-rate="{rate}"'
        )

    return dossier_search_fragment(request.args.get("q", ""), _row)


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
            recent_rows, _ = list_time_entries_page(
                dossier_id=dossier_id, limit=10
            )
            ctx.update(_phase_prefill(dossier_id, recent_rows))
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
        "phase": f.get("phase", ""),
        "sous_phase": f.get("sous_phase", ""),
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
        "phase": f.get("phase", ""),
        "sous_phase": f.get("sous_phase", ""),
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
    existing = get_time_entry(entry_id)
    success, error = delete_time_entry(entry_id)

    if success:
        # Append-only deletion trail (PA-G06).
        record_deletion(
            "time_entry", entry_id,
            dossier_id=(existing or {}).get("dossier_id", ""),
            title=(existing or {}).get("description", ""),
            status="facturée" if (existing or {}).get("invoiced") else "",
        )

    target = safe_internal_redirect(return_to, url_for("time_expenses.time_list"))
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

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
            recent_rows, _ = list_expenses_page(
                dossier_id=dossier_id, limit=10
            )
            ctx.update(_phase_prefill(dossier_id, recent_rows))
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
        "phase": f.get("phase", ""),
        "sous_phase": f.get("sous_phase", ""),
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
        "phase": f.get("phase", ""),
        "sous_phase": f.get("sous_phase", ""),
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
    existing = get_expense(expense_id)
    success, error = delete_expense(expense_id)

    if success:
        record_deletion(
            "expense", expense_id,
            dossier_id=(existing or {}).get("dossier_id", ""),
            title=(existing or {}).get("description", ""),
            status="facturée" if (existing or {}).get("invoiced") else "",
        )

    fallback = url_for("time_expenses.time_list", tab="depenses")
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(target)


# ── Export ───────────────────────────────────────────────────────────────


# ── Reclassement de phase (août 2026) ────────────────────────────────────
#
# La SEULE écriture que l'application autorise sur une ligne déjà facturée,
# et elle ne porte que sur `phase`/`sous_phase` : ce couple ne figure sur
# aucune facture (les postes en sont des copies indépendantes qui n'en
# portent pas), sur aucun gabarit, dans aucun sérialiseur DAV — il ne sert
# qu'au budget par phase, lequel compte le travail FACTURÉ. Le formulaire
# d'édition ordinaire garde donc son refus intact : c'est une porte étroite
# à côté du mur, jamais une brèche dedans.


def _phase_form_context(
    item: dict, *, kind: str, recent_rows: list[dict]
) -> dict:
    """Shared context of the reclassification form."""
    ctx = _template_context()
    ctx.update(
        item=item,
        kind=kind,
        errors=[],
        phase_recents=_recent_codes(recent_rows),
    )
    return ctx


def _phase_form_data() -> tuple[str, str, str]:
    """(phase, sous_phase, return_to) from the submitted form."""
    f = request.form
    return (
        f.get("phase", "").strip(),
        f.get("sous_phase", "").strip(),
        f.get("return_to", ""),
    )


def _log_reclassement(item: dict, kind: str, sous_phase: str) -> None:
    """Codes and ids only — never a description, never an amount."""
    log_dossier_event(
        "phase_reclassified",
        item.get("dossier_id", ""),
        entity_type=kind,
        entity_id=item.get("id", ""),
        from_sous_phase=item.get("sous_phase", ""),
        to_sous_phase=sous_phase,
        invoiced=bool(item.get("invoiced")),
    )


@time_expenses_bp.route("/<entry_id>/phase")
@login_required
def time_entry_phase_edit(entry_id: str) -> str:
    """Render the phase-only form for a time entry (billed or not)."""
    entry = get_time_entry(entry_id)
    if not entry:
        return redirect(url_for("time_expenses.time_list"))

    recent_rows, _ = list_time_entries_page(
        dossier_id=entry.get("dossier_id", ""), limit=10
    )
    ctx = _phase_form_context(entry, kind="time_entry", recent_rows=recent_rows)
    ctx.update(return_to=request.args.get("return_to", ""))
    return render_template("time_expenses/phase_form.html", **ctx)


@time_expenses_bp.route("/<entry_id>/phase", methods=["POST"])
@login_required
def time_entry_phase_update(entry_id: str) -> str:
    """Apply a phase reclassification to a time entry."""
    phase, sous_phase, return_to = _phase_form_data()
    existing = get_time_entry(entry_id)
    entry, errors, changed = set_time_entry_phase(entry_id, phase, sous_phase)

    if errors:
        recent_rows, _ = list_time_entries_page(
            dossier_id=(existing or {}).get("dossier_id", ""), limit=10
        )
        ctx = _phase_form_context(
            {**(existing or {"id": entry_id}), "phase": phase,
             "sous_phase": sous_phase},
            kind="time_entry", recent_rows=recent_rows,
        )
        ctx.update(errors=errors, return_to=return_to)
        return render_template("time_expenses/phase_form.html", **ctx)

    if changed:
        _log_reclassement(existing or {}, "time_entry", sous_phase or phase)

    target = safe_internal_redirect(return_to, url_for("time_expenses.time_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


@time_expenses_bp.route("/depenses/<expense_id>/phase")
@login_required
def expense_phase_edit(expense_id: str) -> str:
    """Render the phase-only form for a disbursement (billed or not)."""
    expense = get_expense(expense_id)
    if not expense:
        return redirect(url_for("time_expenses.time_list", tab="depenses"))

    recent_rows, _ = list_expenses_page(
        dossier_id=expense.get("dossier_id", ""), limit=10
    )
    ctx = _phase_form_context(expense, kind="expense", recent_rows=recent_rows)
    ctx.update(return_to=request.args.get("return_to", ""))
    return render_template("time_expenses/phase_form.html", **ctx)


@time_expenses_bp.route("/depenses/<expense_id>/phase", methods=["POST"])
@login_required
def expense_phase_update(expense_id: str) -> str:
    """Apply a phase reclassification to a disbursement."""
    phase, sous_phase, return_to = _phase_form_data()
    existing = get_expense(expense_id)
    expense, errors, changed = set_expense_phase(expense_id, phase, sous_phase)

    if errors:
        recent_rows, _ = list_expenses_page(
            dossier_id=(existing or {}).get("dossier_id", ""), limit=10
        )
        ctx = _phase_form_context(
            {**(existing or {"id": expense_id}), "phase": phase,
             "sous_phase": sous_phase},
            kind="expense", recent_rows=recent_rows,
        )
        ctx.update(errors=errors, return_to=return_to)
        return render_template("time_expenses/phase_form.html", **ctx)

    if changed:
        _log_reclassement(existing or {}, "expense", sous_phase or phase)

    fallback = url_for("time_expenses.time_list", tab="depenses")
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


_TIME_EXPORT_COLUMNS_CSV = [
    ("date", "Date"),
    ("dossier_file_number", "Dossier"),
    ("description", "Description"),
    ("sous_phase", "Phase"),
    ("hours", "Heures"),
    ("rate", "Taux"),
    ("amount", "Montant"),
    ("billable", "Facturable"),
    ("invoiced", "Facturé"),
]

_TIME_EXPORT_COLUMNS_PDF = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.2),
    ("sous_phase", "Phase", 1.2),
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
    ("sous_phase", "Phase"),
    ("amount", "Montant"),
    ("taxable", "Taxable"),
    ("invoiced", "Facturé"),
]

_EXPENSE_EXPORT_COLUMNS_PDF = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.2),
    ("category", "Catégorie", 1.0),
    ("sous_phase", "Phase", 1.2),
    ("amount", "Montant", 0.8),
    ("taxable", "Taxable", 0.6),
    ("invoiced", "Facturé", 0.6),
]

# Phase O — exports show the LABEL, never the bare code (D-13: « CTS » is a
# visual anagram of the action domaine « CST » in a CSV). The "" entry is
# deliberately excluded so a legacy row exports an empty cell, not
# « Non renseignée » on every pre-Phase-O line.
_PHASE_EXPORT_LABELS = {
    code: label for code, label in phases.SOUS_PHASE_LABELS.items() if code
}


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
    from utils.export_csv import export_csv, prepare_export_rows

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    entries = list_time_entries(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    entries = prepare_export_rows(
        entries, label_maps={"sous_phase": _PHASE_EXPORT_LABELS}
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
    from utils.export_csv import prepare_export_rows

    dossier_id, billable_filter, date_from, date_to = _get_export_filters()
    entries = list_time_entries(
        dossier_id=dossier_id or None,
        billable_filter=billable_filter or None,
        date_from=date_from,
        date_to=date_to,
    )
    entries = prepare_export_rows(
        entries, label_maps={"sous_phase": _PHASE_EXPORT_LABELS}
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
    expenses = prepare_export_rows(
        expenses,
        label_maps={"category": CATEGORY_LABELS,
                    "sous_phase": _PHASE_EXPORT_LABELS},
    )
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
    expenses = prepare_export_rows(
        expenses,
        label_maps={"category": CATEGORY_LABELS,
                    "sous_phase": _PHASE_EXPORT_LABELS},
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf(
        rows=expenses,
        columns=_EXPENSE_EXPORT_COLUMNS_PDF,
        title="Dépenses",
        filename=f"depenses_{date_str}.pdf",
        cents_fields=["amount"],
    )
