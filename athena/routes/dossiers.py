"""Dossier management routes — list, detail, create, edit, delete."""

import json
import math
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import escape

from auth import login_required
from dav.sync import (
    bump_ctag,
    clear_tombstones,
    delete_sync_state,
    record_tombstone,
    remove_tombstone,
)
from pagination import PAGE_SIZE, cursor_pagination, paginate, parse_trail
from models.time_entry import (
    get_time_summary,
    list_time_entries,
)
from models.expense import (
    CATEGORY_LABELS as EXPENSE_CATEGORY_LABELS,
    get_expense_summary,
    list_expenses,
)
from models.invoice import (
    STATUS_LABELS as INVOICE_STATUS_LABELS,
    get_invoice_summary,
    list_invoices,
)
from models.hearing import (
    HEARING_TYPE_LABELS,
    STATUS_LABELS as HEARING_STATUS_LABELS,
    get_hearing_summary,
    list_hearings,
)
from models.task import (
    CATEGORY_LABELS as TASK_CATEGORY_LABELS,
    PRIORITY_LABELS as TASK_PRIORITY_LABELS,
    STATUS_LABELS as TASK_STATUS_LABELS,
    get_task_summary,
    list_tasks,
)
from models.protocol import (
    PROTOCOL_TYPE_COLORS,
    PROTOCOL_TYPE_SHORT_LABELS,
    check_overdue_steps,
    get_protocol,
    get_protocol_for_dossier,
    get_protocol_summary,
    list_protocols_for_dossier,
)
from models.document import (
    CATEGORY_LABELS as DOCUMENT_CATEGORY_LABELS,
    format_file_size,
    get_document_summary,
    get_file_icon,
    list_documents,
)
from models.folder import list_folders
from models.dossier import (
    DOMAINE_LABELS,
    FEE_TYPE_LABELS,
    MANDATE_TYPE_LABELS,
    ROLE_LABELS,
    STATUS_LABELS,
    create_dossier,
    delete_dossier,
    get_dossier,
    list_dossiers,
    list_dossiers_page,
    suggest_file_number,
    update_dossier,
)
from utils import taxonomie
from utils.recours import PRESCRIPTION_LABELS, compute_class
from utils.template_fields import format_honoraires, retention_date

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
        cents = float(value.strip().replace(",", ".")) * 100
        if not math.isfinite(cents):
            return 0
        return int(round(cents))
    except (ValueError, TypeError):
        return 0


def _parse_percent(value: str) -> int:
    """Parse a percentage string (e.g., '25' or '33.33') into basis points."""
    return _parse_cents(value)  # same ×100 transform: 25 → 2500


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


def _parse_parties_json(raw: str) -> list[dict]:
    """Parse a JSON string of [{id, name}, ...] from a hidden form field."""
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        return [
            {"id": str(p["id"]), "name": str(p["name"])}
            for p in items
            if isinstance(p, dict) and p.get("id")
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _form_data() -> dict:
    """Extract dossier fields from the submitted form."""
    f = request.form
    return {
        "file_number": f.get("file_number", "").strip(),
        "title": f.get("title", "").strip(),
        # Parties (JSON arrays)
        "clients": _parse_parties_json(f.get("clients_json", "")),
        "opposing_parties": _parse_parties_json(f.get("opposing_parties_json", "")),
        # Classification
        "mandate_type": f.get("mandate_type", "judiciaire"),
        "court_file_number": f.get("court_file_number", "").strip(),
        "district_judiciaire": f.get("district_judiciaire", "").strip(),
        "tribunal": f.get("tribunal", "").strip(),
        "competence": f.get("competence", "").strip(),
        "palais_de_justice": f.get("palais_de_justice", "").strip(),
        "greffe_number": f.get("greffe_number", "").strip(),
        "juridiction_number": f.get("juridiction_number", "").strip(),
        "is_administrative_tribunal": f.get("is_administrative_tribunal") == "true",
        # Role
        "role": f.get("role", "demandeur"),
        # Financial
        "fee_type": f.get("fee_type", "hourly"),
        "hourly_rate": _parse_cents(f.get("hourly_rate", "")),
        "flat_fee": _parse_cents(f.get("flat_fee", "")) or None,
        "contingency_percent": _parse_percent(f.get("contingency_percent", "")) or None,
        "fee_notes": f.get("fee_notes", "").strip(),
        # Status
        "status": f.get("status", "actif"),
        "opened_date": _parse_date(f.get("opened_date", "")),
        "closed_date": _parse_date(f.get("closed_date", "")),
        # Recours & prescription (prescription_date is derived on save from
        # droit_action_date + prescription_type — see the model layer).
        # domaine/action are the taxonomy pair; the model rejects an action
        # that does not belong to the submitted domaine.
        "domaine": f.get("domaine", "").strip(),
        "action": f.get("action", "").strip(),
        "action_precision": f.get("action_precision", "").strip(),
        "valeur": _parse_cents(f.get("valeur", "")) or None,
        "prescription_type": f.get("prescription_type", "").strip(),
        "droit_action_date": _parse_date(f.get("droit_action_date", "")),
        "prescription_notes": f.get("prescription_notes", "").strip(),
    }


def _template_context() -> dict:
    """Return shared template context for dossier views."""
    return {
        "domaine_labels": DOMAINE_LABELS,
        "delai_type_labels": taxonomie.DELAI_TYPE_LABELS,
        # The whole taxonomy, for the form's cascading picker. Cached in
        # utils.taxonomie, so handing it to every dossier view (list, tabs)
        # costs a dict reference; only form.html actually serializes it.
        "taxonomie_payload": taxonomie.form_payload(),
        "mandate_type_labels": MANDATE_TYPE_LABELS,
        "status_labels": STATUS_LABELS,
        "role_labels": ROLE_LABELS,
        "fee_type_labels": FEE_TYPE_LABELS,
        "prescription_labels": PRESCRIPTION_LABELS,
    }


# « Rétention » (fermeture + 7 ans) and the joint « honoraires + taux »
# display are shared with the gabarit field catalog so the Mandat card and a
# generated document render identically — see utils.template_fields.


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

    if search or sort_by != "opened_date":
        # Legacy fallback: Firestore has no full-text search, so the search
        # path materializes the collection and filters in Python, sliced by
        # paginate(). Search is occasional — the read cost is acceptable.
        # The non-default sort (file_number) also falls back here: a cursor
        # path for it would need its own composite indexes.
        page = request.args.get("page", 1, type=int)
        dossiers = list_dossiers(
            status_filter=effective_filter,
            search=search or None,
            sort_by=sort_by,
        )
        dossiers, pagination = paginate(dossiers, page)
        pagination["url"] = url_for("dossiers.dossier_list")
        pagination["target"] = "#dossier-rows"
        if sort_by != "opened_date":
            # q/status travel via hx-include; sort isn't in #filters.
            pagination["extra_vals"] = {"sort": sort_by}
    else:
        # Cursor pagination (default browse path): ~PAGE_SIZE reads per page.
        cursor = request.args.get("cursor", "") or None
        trail = parse_trail(request.args.get("trail", ""))
        dossiers, next_cursor = list_dossiers_page(
            status_filter=effective_filter,
            limit=PAGE_SIZE,
            cursor=cursor,
        )
        pagination = cursor_pagination(
            cursor=cursor,
            trail=trail,
            next_cursor=next_cursor,
            url=url_for("dossiers.dossier_list"),
            target="#dossier-rows",
        )

    # Compute prescription warnings
    _attach_prescription_warnings(dossiers)

    ctx = _template_context()
    ctx.update(
        dossiers=dossiers,
        status_filter=status_filter,
        search=search,
        sort_by=sort_by,
        pagination=pagination,
    )

    if _is_htmx():
        return render_template("dossiers/_dossier_rows.html", **ctx)

    return render_template("dossiers/list.html", **ctx)


# ── Detail ────────────────────────────────────────────────────────────────


_VALID_TABS = (
    "temps",
    "facturation",
    "audiences",
    "taches",
    "protocole",
    "documents",
)


@dossiers_bp.route("/<dossier_id>")
@login_required
def dossier_detail(dossier_id: str) -> str:
    """Render the dossier detail hub page."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))

    _attach_prescription_warnings([dossier])

    requested_tab = request.args.get("tab", "").strip()
    initial_tab = requested_tab if requested_tab in _VALID_TABS else "temps"

    ctx = _template_context()
    ctx["dossier"] = dossier
    ctx["initial_tab"] = initial_tab
    ctx["value_class"] = compute_class(dossier.get("valeur"))
    ctx["fee_display"] = format_honoraires(dossier) or "—"
    ctx["retention_date"] = retention_date(dossier.get("closed_date"))
    # Taxonomy display values, resolved route-side like value_class/fee_display.
    ctx["action_obj"] = taxonomie.get_action(dossier.get("action", ""))
    ctx["action_display"] = taxonomie.action_label(dossier.get("action", ""))
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
        "temps": "dossiers/_tab_temps.html",
        "facturation": "dossiers/_tab_facturation.html",
        "audiences": "dossiers/_tab_audiences.html",
        "taches": "dossiers/_tab_taches.html",
        "protocole": "dossiers/_tab_protocole.html",
        "documents": "dossiers/_tab_documents.html",
    }

    # Load time/expense data for the temps tab
    if tab_name == "temps":
        ctx["time_entries"] = list_time_entries(dossier_id=dossier_id)
        ctx["expenses"] = list_expenses(dossier_id=dossier_id)
        ctx["time_summary"] = get_time_summary(dossier_id)
        ctx["expense_summary"] = get_expense_summary(dossier_id)
        ctx["category_labels"] = EXPENSE_CATEGORY_LABELS

    # Load hearing data for the audiences tab
    if tab_name == "audiences":
        ctx["hearings"] = list_hearings(dossier_id=dossier_id)
        ctx["hearing_summary"] = get_hearing_summary(dossier_id)
        ctx["hearing_type_labels"] = HEARING_TYPE_LABELS
        ctx["status_labels"] = HEARING_STATUS_LABELS

    # Load task data for the taches tab
    if tab_name == "taches":
        ctx["tasks"] = list_tasks(dossier_id=dossier_id)
        ctx["task_summary"] = get_task_summary(dossier_id)
        ctx["category_labels"] = TASK_CATEGORY_LABELS
        ctx["priority_labels"] = TASK_PRIORITY_LABELS
        ctx["status_labels"] = TASK_STATUS_LABELS
        ctx["now"] = datetime.now(timezone.utc)

    # Load protocol data for the protocole tab
    if tab_name == "protocole":
        active_protocol = get_protocol_for_dossier(dossier_id, active_only=True)
        if active_protocol:
            check_overdue_steps(active_protocol["id"])
            active_protocol = get_protocol(active_protocol["id"])

        # Historical protocols (completed/suspended)
        all_protocols = list_protocols_for_dossier(dossier_id)
        historical_protocols = [
            p for p in all_protocols
            if p.get("status") in ("complété", "suspendu")
        ]

        ctx["protocol"] = active_protocol
        ctx["historical_protocols"] = historical_protocols
        ctx["protocol_summary"] = get_protocol_summary(dossier_id)
        ctx["protocol_type_colors"] = PROTOCOL_TYPE_COLORS
        ctx["protocol_type_short_labels"] = PROTOCOL_TYPE_SHORT_LABELS
        ctx["now"] = datetime.now(timezone.utc)

    # Load document data for the documents tab
    if tab_name == "documents":
        # Notes
        from models.note import list_notes, get_notes_summary, CATEGORY_LABELS as NOTE_CATEGORY_LABELS
        ctx["notes"] = list_notes(dossier_id=dossier_id)
        ctx["notes_summary"] = get_notes_summary(dossier_id)
        ctx["note_category_labels"] = NOTE_CATEGORY_LABELS

        # Root-level folders with item counts
        root_folders = list_folders(dossier_id, parent_folder_id=None)
        from models.folder import _count_items
        for f in root_folders:
            counts = _count_items(dossier_id, f["id"])
            f["_item_count"] = counts["folders"] + counts["documents"]
        ctx["root_folders"] = root_folders

        # Root-level documents only (no folder_id)
        docs = list_documents(dossier_id=dossier_id, folder_id=None)
        for d in docs:
            d["_file_size_fmt"] = format_file_size(d.get("file_size", 0))
            d["_file_icon"] = get_file_icon(d.get("file_type", ""))
        ctx["documents"] = docs
        ctx["document_summary"] = get_document_summary(dossier_id)
        ctx["category_labels"] = DOCUMENT_CATEGORY_LABELS

    # Load invoice data for the facturation tab
    if tab_name == "facturation":
        ctx["invoices"] = list_invoices(dossier_id=dossier_id)
        ctx["invoice_summary"] = get_invoice_summary(dossier_id)
        ctx["status_labels"] = INVOICE_STATUS_LABELS

    template = templates.get(tab_name, "dossiers/_tab_placeholder.html")
    ctx["tab_name"] = tab_name
    # URL that child + / detail / edit links should send the user back to.
    ctx["tab_return_to"] = url_for(
        "dossiers.dossier_detail", dossier_id=dossier_id, tab=tab_name
    )
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


_ACTIVE_DOSSIER_STATUSES = ("actif", "en_attente")


def _sync_dossier_dav_visibility(
    dossier_id: str, old_status: str, new_status: str
) -> None:
    """Drain or restore a dossier's DAV collection on a status transition.

    A dossier's ``/dav/dossier-{id}/`` collection is advertised to DavX5 only
    while the dossier is ``actif``/``en_attente``. When it is closed or
    archived the collection leaves discovery; if DavX5 still holds the
    dossier's tasks/notes when that happens, its sync errors (the stale
    per-collection worker hits a collection that is gone while local rows still
    reference it).

    To make the teardown clean we record a tombstone for every task and note
    and bump the collection CTag: DavX5's next sync then reports them all as
    deleted and drops its local copies BEFORE the collection disappears.
    Reopening a dossier removes those tombstones (and bumps the CTag) so the
    items sync back.

    No task/note documents are touched — tombstones live in ``dav_sync`` and
    are DAV markers only. The underlying records stay in Firestore and in the
    web UI regardless of the dossier's status.
    """
    was_active = old_status in _ACTIVE_DOSSIER_STATUSES
    is_active = new_status in _ACTIVE_DOSSIER_STATUSES
    if was_active == is_active:
        return  # Visibility unchanged — nothing to drain or restore.

    from models.note import list_notes

    sync_name = f"dossier:{dossier_id}"
    resource_ids = [t["id"] for t in list_tasks(dossier_id=dossier_id)]
    resource_ids += [n["id"] for n in list_notes(dossier_id=dossier_id)]

    if is_active:
        # Reopened: resources re-enter the collection — drop stale tombstones
        # so one sync REPORT never reports an id as both live and deleted.
        for rid in resource_ids:
            remove_tombstone(sync_name, rid)
    else:
        # Closed/archived: tombstone every resource so DavX5 drains cleanly.
        for rid in resource_ids:
            record_tombstone(sync_name, rid)
    bump_ctag(sync_name)


@dossiers_bp.route("/<dossier_id>", methods=["POST"])
@login_required
def dossier_update(dossier_id: str) -> str:
    """Handle edit form submission."""
    existing = get_dossier(dossier_id)
    old_status = existing.get("status", "") if existing else ""

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

    # A status change to/from closed/archived changes the dossier's DAV
    # collection visibility — drain or restore it so DavX5 syncs cleanly.
    _sync_dossier_dav_visibility(
        dossier_id, old_status, dossier.get("status", "")
    )

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

    if success:
        # No DAV endpoint reads a "dossiers" sync collection post-D1; instead,
        # tear down the deleted dossier's live per-collection DAV sync state
        # (its /dav/dossier-{id}/ collection no longer exists).
        sync_name = f"dossier:{dossier_id}"
        clear_tombstones(sync_name)
        delete_sync_state(sync_name)

    if _is_htmx():
        if success:
            resp = redirect(url_for("dossiers.dossier_list"))
            resp.headers["HX-Redirect"] = url_for("dossiers.dossier_list")
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(url_for("dossiers.dossier_list"))


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("file_number", "N° dossier"),
    ("title", "Titre"),
    ("_client_names", "Client(s)"),
    ("_domaine", "Domaine"),
    ("_action", "Action"),
    ("tribunal", "Tribunal"),
    ("status", "Statut"),
    ("opened_date", "Ouverture"),
]

_EXPORT_COLUMNS_PDF = [
    ("file_number", "N° dossier", 1.0),
    ("title", "Titre", 2.0),
    ("_client_names", "Client(s)", 1.5),
    ("_domaine", "Domaine", 1.2),
    ("tribunal", "Tribunal", 1.0),
    ("status", "Statut", 0.8),
    ("opened_date", "Ouverture", 1.0),
]


def _get_export_dossiers() -> list[dict]:
    """Fetch and pre-process dossiers for export, respecting current filters."""
    status_filter = request.args.get("status", "actif")
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "opened_date")

    effective_filter = status_filter if status_filter != "tous" else None

    dossiers = list_dossiers(
        status_filter=effective_filter,
        search=search or None,
        sort_by=sort_by,
    )
    for d in dossiers:
        d["_client_names"] = ", ".join(c.get("name", "") for c in d.get("clients", []))
        # Derived into _-prefixed keys rather than overwriting the stored
        # fields in place (the old matter_type line did), so the export can
        # carry both domaine and action without either clobbering the other.
        d["_domaine"] = DOMAINE_LABELS.get(d.get("domaine", ""), "")
        d["_action"] = taxonomie.action_label(d.get("action", ""))
        d["status"] = STATUS_LABELS.get(d.get("status", ""), d.get("status", ""))
    return dossiers


# ── Court file number parsing ─────────────────────────────────────────


@dossiers_bp.route("/parse-court-file", methods=["POST"])
@login_required
def parse_court_file():
    """Parse a court file number and return judicial metadata as JSON."""
    court_file_number = request.form.get("court_file_number", "").strip()

    from models.reference import parse_court_file_number
    result = parse_court_file_number(court_file_number)

    return jsonify({
        "district_judiciaire": (
            result["greffe"]["district_judiciaire"]
            if result.get("greffe") else ""
        ),
        "tribunal": (
            result["juridiction"]["tribunal"]
            if result.get("juridiction") else ""
        ),
        "competence": (
            result["juridiction"]["competence"]
            if result.get("juridiction") else ""
        ),
        "palais_de_justice": (
            result["greffe"]["palais_de_justice"]
            if result.get("greffe") else ""
        ),
        "greffe_number": result.get("greffe_number", ""),
        "juridiction_number": result.get("juridiction_number", ""),
        "is_administrative": result.get("is_administrative", False),
        "parse_error": result.get("parse_error"),
    })


# ── Export ────────────────────────────────────────────────────────────


@dossiers_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export dossiers as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_dossiers()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"dossiers_{date_str}.csv",
    )


@dossiers_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export dossiers as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_dossiers()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Dossiers",
        filename=f"dossiers_{date_str}.pdf",
    )
