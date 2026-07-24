"""Hearing / calendar routes — list, detail, create, edit, delete."""

from datetime import datetime, timezone

from markupsafe import escape

from tz import mtl_to_utc, to_mtl

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import bump_ctag, collection_for, record_tombstone, remove_tombstone
from security import safe_internal_redirect
from models.hearing import (
    FORUM_LABELS,
    HEARING_TYPE_COLORS,
    HEARING_TYPE_LABELS,
    HEARING_TITLE_SUGGESTIONS,
    MODALITE_LABELS,
    QUICK_LOCATIONS,
    REMINDER_LABELS,
    STATUS_LABELS,
    VALID_FORUMS,
    VALID_HEARING_TYPES,
    VALID_HEARING_TYPES_EXTRAJUDICIAIRE,
    VALID_HEARING_TYPES_JUDICIAIRE,
    VALID_MODALITES,
    VALID_REMINDER_MINUTES,
    VALID_STATUSES,
    create_hearing,
    delete_hearing,
    forum_of,
    get_hearing,
    list_hearings,
    list_hearings_in_range,
    list_hearings_window,
    update_hearing,
)
from models.dossier import (
    get_dossier,
    list_dossiers,
    VALID_COURTS,
)

hearings_bp = Blueprint("hearings", __name__, url_prefix="/audiences")


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


def _parse_datetime(date_str: str, time_str: str) -> datetime | None:
    """Parse separate date and time strings into a UTC datetime.

    The user enters times in Montreal local time; we convert to UTC
    before storage.
    """
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()
    time_str = (time_str or "").strip()
    try:
        if time_str:
            naive = datetime.strptime(
                f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
            )
        else:
            naive = datetime.strptime(date_str, "%Y-%m-%d")
        return mtl_to_utc(naive)
    except ValueError:
        return None


def _parse_int(value: str, default: int = 0) -> int:
    """Parse string to int with a default fallback."""
    if not value or not value.strip():
        return default
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return default


def _template_context() -> dict:
    """Return shared template context for hearing views."""
    return {
        "hearing_type_labels": HEARING_TYPE_LABELS,
        "hearing_type_colors": HEARING_TYPE_COLORS,
        "hearing_title_suggestions": HEARING_TITLE_SUGGESTIONS,
        "status_labels": STATUS_LABELS,
        "reminder_labels": REMINDER_LABELS,
        "valid_hearing_types": VALID_HEARING_TYPES,
        # Two-tier vocabulary: the form filters types by the derived forum.
        "valid_hearing_types_judiciaire": VALID_HEARING_TYPES_JUDICIAIRE,
        "valid_hearing_types_extrajudiciaire": VALID_HEARING_TYPES_EXTRAJUDICIAIRE,
        "forum_labels": FORUM_LABELS,
        "forum_of": forum_of,
        "modalite_labels": MODALITE_LABELS,
        "valid_modalites": VALID_MODALITES,
        "valid_statuses": VALID_STATUSES,
        "valid_reminder_minutes": VALID_REMINDER_MINUTES,
        "valid_courts": VALID_COURTS,
        "quick_locations": QUICK_LOCATIONS,
    }


def _enrich_dossier_info(data: dict) -> dict:
    """Look up dossier and attach denormalized file_number + title.

    Hearings may be standalone agenda events (no dossier). When the dossier
    field is empty — or references a dossier that no longer exists — clear the
    id and its denormalized file_number/title so no stale label lingers on a
    hearing that was detached from its dossier.
    """
    dossier_id = data.get("dossier_id", "")
    if dossier_id:
        dossier = get_dossier(dossier_id)
        if dossier:
            data["dossier_file_number"] = dossier.get("file_number", "")
            data["dossier_title"] = dossier.get("title", "")
            return data
        # Invalid dossier ID — fall through and clear it.
    data["dossier_id"] = ""
    data["dossier_file_number"] = ""
    data["dossier_title"] = ""
    return data


def _form_data() -> dict:
    """Extract hearing fields from the submitted form."""
    f = request.form
    all_day = f.get("all_day") == "on"

    if all_day:
        start_dt = _parse_date(f.get("start_date", ""))
        end_dt = _parse_date(f.get("end_date", ""))
    else:
        start_dt = _parse_datetime(f.get("start_date", ""), f.get("start_time", ""))
        end_dt = _parse_datetime(f.get("start_date", ""), f.get("end_time", ""))

    return {
        "dossier_id": f.get("dossier_id", "").strip(),
        "title": f.get("title", "").strip(),
        "hearing_type": f.get("hearing_type", "audience"),
        "start_datetime": start_dt,
        "end_datetime": end_dt,
        "all_day": all_day,
        "location": f.get("location", "").strip(),
        "court": f.get("court", "").strip(),
        "judge": f.get("judge", "").strip(),
        "notes": f.get("notes", "").strip(),
        "reminder_minutes": _parse_int(f.get("reminder_minutes", ""), 1440),
        "status": f.get("status", "à_confirmer"),
        "modalite": f.get("modalite", "présentiel"),
        "conference_uri": f.get("conference_uri", "").strip(),
    }


# ── Dossier search (for autocomplete in forms) ───────────────────────────


@hearings_bp.route("/dossier-search")
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
        dossier_id = escape(d["id"])
        file_number = escape(d.get("file_number", ""))
        title = escape(d.get("title", ""))
        html_parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f'    data-dossier-id="{dossier_id}"'
            f'    data-dossier-file-number="{file_number}"'
            f'    data-dossier-title="{title}">'
            f'  <span class="font-medium text-gray-900">{file_number}</span>'
            f'  <span class="text-gray-500 ml-1">{title}</span>'
            f'</li>'
        )
    html_parts.append("</ul>")
    return "\n".join(html_parts)


# ── List (Calendar view) ─────────────────────────────────────────────────


def _matches_filters(
    hearing: dict, hearing_type_filter: str, status_filter: str
) -> bool:
    """Apply the optional type/status filters to one hearing.

    Mirrors the legacy list_hearings semantics: unknown filter values are
    ignored rather than matching nothing.
    """
    if (
        hearing_type_filter
        and hearing_type_filter in VALID_HEARING_TYPES
        and hearing.get("hearing_type") != hearing_type_filter
    ):
        return False
    if (
        status_filter
        and status_filter in VALID_STATUSES
        and hearing.get("status") != status_filter
    ):
        return False
    return True


@hearings_bp.route("/")
@login_required
def hearing_list() -> str:
    """Render the hearing calendar / list view."""
    view = request.args.get("view", "list")
    hearing_type_filter = request.args.get("type", "").strip()
    status_filter = request.args.get("status", "").strip()
    month_str = request.args.get("month", "")

    now = datetime.now(timezone.utc)
    now_mtl = to_mtl(now)

    if view == "month":
        # Parse month param (YYYY-MM) or use current month (in Montreal tz)
        if month_str:
            try:
                year, month = int(month_str[:4]), int(month_str[5:7])
            except (ValueError, IndexError):
                year, month = now_mtl.year, now_mtl.month
        else:
            year, month = now_mtl.year, now_mtl.month

        # Compute month boundaries
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        # The month range is pushed server-side (bounded read) instead of
        # streaming the whole collection; type/status filters then apply
        # in Python over the (small) month window.
        hearings = [
            h
            for h in list_hearings_in_range(month_start, month_end)
            if _matches_filters(h, hearing_type_filter, status_filter)
        ]

        # Build calendar grid data
        import calendar
        cal = calendar.Calendar(firstweekday=0)  # Monday first
        month_days = cal.monthdayscalendar(year, month)

        # Map day → list of hearings (use Montreal time for grouping)
        day_hearings: dict[int, list[dict]] = {}
        for h in hearings:
            sd = h.get("start_datetime")
            if sd:
                local_sd = to_mtl(sd)
                day = local_sd.day
                day_hearings.setdefault(day, []).append(h)

        # Prev / next month
        if month == 1:
            prev_month = f"{year - 1}-12"
        else:
            prev_month = f"{year}-{month - 1:02d}"
        if month == 12:
            next_month = f"{year + 1}-01"
        else:
            next_month = f"{year}-{month + 1:02d}"

        # French month name
        month_names = [
            "", "janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre"
        ]
        month_label = f"{month_names[month]} {year}"

        ctx = _template_context()
        ctx.update(
            hearings=hearings,
            view=view,
            hearing_type_filter=hearing_type_filter,
            status_filter=status_filter,
            month_days=month_days,
            day_hearings=day_hearings,
            year=year,
            month=month,
            month_label=month_label,
            prev_month=prev_month,
            next_month=next_month,
            current_month=f"{year}-{month:02d}",
        )

        if _is_htmx():
            return render_template("hearings/_month_grid.html", **ctx)
        return render_template("hearings/list.html", **ctx)

    else:
        # List view. Not paginated (matching the existing UX), but bounded
        # server-side instead of streaming the whole collection: the next
        # 100 upcoming hearings (start_datetime >= now, chronological) and,
        # only when a filter is active, the 100 most recent past ones
        # (start_datetime < now, reverse chronological). Type/status filters
        # apply in Python over each bounded window.
        #
        # The default view is forward-looking: past hearings (and cancelled
        # future ones) surface ONLY through the type/status filters — the
        # collapsed « Passées » disclosure was removed 2026-07-23. Skipping
        # the past-window query when nothing will display it saves the reads.
        filters_active = bool(hearing_type_filter or status_filter)

        upcoming_window = [
            h
            for h in list_hearings_window(now, direction="upcoming")
            if _matches_filters(h, hearing_type_filter, status_filter)
        ]

        # Cancelled future hearings belong with « Passées » (existing UX).
        upcoming = [
            h for h in upcoming_window if h.get("status") != "annulée"
        ]

        past: list[dict] = []
        if filters_active:
            past = [
                h for h in upcoming_window if h.get("status") == "annulée"
            ] + [
                h
                for h in list_hearings_window(now, direction="past")
                if _matches_filters(h, hearing_type_filter, status_filter)
            ]
            # Past: reverse chronological
            past.sort(
                key=lambda h: h.get("start_datetime")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )

        ctx = _template_context()
        ctx.update(
            upcoming=upcoming,
            past=past,
            view=view,
            hearing_type_filter=hearing_type_filter,
            status_filter=status_filter,
            now=now,
        )

        if _is_htmx():
            return render_template("hearings/_hearing_rows.html", **ctx)
        return render_template("hearings/list.html", **ctx)


# ── Create ────────────────────────────────────────────────────────────────


@hearings_bp.route("/new")
@login_required
def hearing_new() -> str:
    """Render the empty hearing form."""
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
                "court": dossier.get("court", ""),
            }
    ctx.update(hearing=prefilled, errors=[], return_to=request.args.get("return_to", ""))
    return render_template("hearings/form.html", **ctx)


@hearings_bp.route("/", methods=["POST"])
@login_required
def hearing_create() -> str:
    """Handle new hearing form submission."""
    data = _form_data()
    data = _enrich_dossier_info(data)
    return_to = request.form.get("return_to", "")

    hearing, errors = create_hearing(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get("dossier_file_number", request.form.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(hearing=data, errors=errors, return_to=return_to)
        return render_template("hearings/form.html", **ctx)

    bump_ctag(collection_for(hearing.get("dossier_id")))

    target = safe_internal_redirect(return_to, url_for("hearings.hearing_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Detail ────────────────────────────────────────────────────────────────


@hearings_bp.route("/<hearing_id>")
@login_required
def hearing_detail(hearing_id: str) -> str:
    """Render the hearing detail view."""
    hearing = get_hearing(hearing_id)
    if not hearing:
        return redirect(url_for("hearings.hearing_list"))

    ctx = _template_context()
    ctx["hearing"] = hearing
    ctx["return_to"] = request.args.get("return_to", "")
    return render_template("hearings/detail.html", **ctx)


# ── Edit ──────────────────────────────────────────────────────────────────


@hearings_bp.route("/<hearing_id>/edit")
@login_required
def hearing_edit(hearing_id: str) -> str:
    """Render the edit form pre-filled with hearing data."""
    hearing = get_hearing(hearing_id)
    if not hearing:
        return redirect(url_for("hearings.hearing_list"))

    ctx = _template_context()
    ctx.update(hearing=hearing, errors=[], return_to=request.args.get("return_to", ""))
    return render_template("hearings/form.html", **ctx)


@hearings_bp.route("/<hearing_id>", methods=["POST"])
@login_required
def hearing_update(hearing_id: str) -> str:
    """Handle edit form submission."""
    # Capture the old dossier BEFORE the write: reassigning a hearing moves it
    # between DAV collections, and the old one needs a tombstone or DavX5
    # keeps a duplicate copy of the court date forever.
    existing_hearing = get_hearing(hearing_id)
    old_dossier_id = existing_hearing.get("dossier_id") if existing_hearing else None

    data = _form_data()
    data = _enrich_dossier_info(data)
    return_to = request.form.get("return_to", "")

    hearing, errors = update_hearing(hearing_id, data)

    if errors:
        data["id"] = hearing_id
        data["dossier_file_number"] = data.get("dossier_file_number", request.form.get("dossier_display", ""))
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(hearing=data, errors=errors, return_to=return_to)
        return render_template("hearings/form.html", **ctx)

    new_dossier_id = hearing.get("dossier_id")
    if old_dossier_id != new_dossier_id:
        record_tombstone(collection_for(old_dossier_id), hearing_id)
        bump_ctag(collection_for(old_dossier_id))
        # The hearing (re)enters its new collection — drop any stale tombstone
        # so one sync REPORT never reports it as both live and deleted.
        remove_tombstone(collection_for(new_dossier_id), hearing_id)
    bump_ctag(collection_for(new_dossier_id))

    fallback = url_for("hearings.hearing_detail", hearing_id=hearing_id)
    target = safe_internal_redirect(return_to, fallback)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Delete ────────────────────────────────────────────────────────────────


@hearings_bp.route("/<hearing_id>/delete", methods=["POST"])
@login_required
def hearing_delete(hearing_id: str) -> str:
    """Delete a hearing and redirect to the list (or back to the caller)."""
    return_to = request.form.get("return_to", "")
    existing_hearing = get_hearing(hearing_id)
    dossier_id = existing_hearing.get("dossier_id") if existing_hearing else None
    success, error = delete_hearing(hearing_id)

    if success:
        record_tombstone(collection_for(dossier_id), hearing_id)
        bump_ctag(collection_for(dossier_id))

    target = safe_internal_redirect(return_to, url_for("hearings.hearing_list"))
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(target)


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("start_datetime", "Date"),
    ("title", "Titre"),
    ("dossier_file_number", "Dossier"),
    ("hearing_type", "Type"),
    ("location", "Lieu"),
    ("status", "Statut"),
]

_EXPORT_COLUMNS_PDF = [
    ("start_datetime", "Date", 1.0),
    ("title", "Titre", 2.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("hearing_type", "Type", 1.0),
    ("location", "Lieu", 1.5),
    ("status", "Statut", 0.8),
]


def _get_export_hearings() -> list[dict]:
    """Fetch and pre-process hearings for export, respecting current filters."""
    from utils.export_csv import prepare_export_rows

    hearing_type_filter = request.args.get("type", "").strip()
    status_filter = request.args.get("status", "").strip()

    hearings = list_hearings(
        hearing_type_filter=hearing_type_filter or None,
        status_filter=status_filter or None,
    )
    return prepare_export_rows(
        hearings,
        label_maps={
            "hearing_type": HEARING_TYPE_LABELS,
            "status": STATUS_LABELS,
        },
    )


@hearings_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export hearings as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_hearings()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"audiences_{date_str}.csv",
    )


@hearings_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export hearings as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_hearings()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Audiences",
        filename=f"audiences_{date_str}.pdf",
    )
