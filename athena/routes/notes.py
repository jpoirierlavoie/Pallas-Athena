"""Dossier note routes — list, detail, create, edit, delete, pin."""

from datetime import datetime

from markupsafe import escape

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import (
    bump_ctag,
    collection_for,
    record_tombstone,
    remove_tombstone,
)
from models.audit_event import record_deletion
from security import safe_internal_redirect
from models.note import (
    CATEGORY_LABELS,
    CONTENT_MAX_LENGTH,
    VALID_CATEGORIES,
    create_note,
    delete_note,
    get_note,
    list_notes,
    list_notes_recent,
    toggle_pin,
    update_note,
)
from models.dossier import get_dossier, list_dossiers

# Reserved filter value: items belonging to no dossier. Not a dossier id
# (those are UUIDv4), so it can never collide with a real one.
GENERAL_FILTER = "general"

notes_bp = Blueprint("notes", __name__, url_prefix="/notes")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _template_context() -> dict:
    """Return shared template context for note views."""
    return {
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
        "content_max_length": CONTENT_MAX_LENGTH,
    }


def _enrich_dossier_info(data: dict) -> tuple[dict, list[str]]:
    """Attach the denormalized dossier labels; return (data, errors).

    Since a note may legitimately have NO dossier (it then belongs to
    « Général »), an unresolvable dossier_id can no longer be blanked: the
    model would accept the result and quietly file a dossier note under
    Général instead. « Aucun dossier choisi » and « dossier introuvable »
    must stay distinguishable, so the second returns an error.
    """
    dossier_id = (data.get("dossier_id") or "").strip()
    data["dossier_id"] = dossier_id
    if not dossier_id:
        data["dossier_file_number"] = ""
        data["dossier_title"] = ""
        return data, []

    dossier = get_dossier(dossier_id)
    if not dossier:
        return data, [
            "Dossier introuvable. Choisissez un dossier existant, ou laissez "
            "le champ vide pour classer la note dans « Général »."
        ]
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")
    return data, []


def _form_data() -> dict:
    """Extract note fields from the submitted form."""
    f = request.form
    return {
        "dossier_id": f.get("dossier_id", "").strip(),
        "title": f.get("title", "").strip(),
        "content": f.get("content", "").strip(),
        "category": f.get("category", "autre"),
        "pinned": f.get("pinned") == "on",
    }


# ── Dossier search (for autocomplete in forms) ───────────────────────────


@notes_bp.route("/dossier-search")
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


# ── List ─────────────────────────────────────────────────────────────────


@notes_bp.route("/")
@login_required
def note_list() -> str:
    """Render the note list view."""
    dossier_filter = request.args.get("dossier_id", "").strip()
    category_filter = request.args.get("category", "").strip()
    search_query = request.args.get("q", "").strip()

    # Invalid categories are dropped (the legacy filter ignored them anyway)
    # so a junk query string cannot force the unbounded fallback.
    if category_filter not in VALID_CATEGORIES:
        category_filter = ""

    general_only = dossier_filter == GENERAL_FILTER
    # « Général » has no server-side query: the models cannot express
    # "no dossier" (tasks store None, notes ""), so it filters in Python
    # over the materialized list.
    model_dossier = None if general_only else (dossier_filter or None)

    if search_query or category_filter or general_only:
        # Legacy fallback: search scans title + content and category is a
        # Python-side filter, so both need the fully materialized list.
        # These are occasional paths; the default view stays bounded below.
        notes = list_notes(
            dossier_id=model_dossier,
            category=category_filter or None,
            search=search_query or None,
        )
    else:
        # Bounded default path: pinned notes + most recent unpinned notes,
        # ordered and limited server-side (~PINNED_LIMIT + RECENT_LIMIT
        # reads max) while keeping the pinned-first / newest-first order.
        notes = list_notes_recent(dossier_id=model_dossier)

    if general_only:
        notes = [n for n in notes if not n.get("dossier_id")]

    ctx = _template_context()
    ctx.update(
        notes=notes,
        dossier_filter=dossier_filter,
        category_filter=category_filter,
        search_query=search_query,
    )

    if _is_htmx():
        return render_template("notes/_note_rows.html", **ctx)

    return render_template("notes/list.html", **ctx)


# ── Create ───────────────────────────────────────────────────────────────


@notes_bp.route("/new")
@login_required
def note_new() -> str:
    """Render the empty note form."""
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
    ctx.update(note=prefilled, errors=[], return_to=request.args.get("return_to", ""))
    return render_template("notes/form.html", **ctx)


@notes_bp.route("/", methods=["POST"])
@login_required
def note_create() -> str:
    """Handle new note form submission."""
    data = _form_data()
    data, link_errors = _enrich_dossier_info(data)
    return_to = request.form.get("return_to", "")

    note, errors = (None, link_errors) if link_errors else create_note(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get(
            "dossier_file_number", request.form.get("dossier_display", "")
        )
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(note=data, errors=errors, return_to=return_to)
        return render_template("notes/form.html", **ctx)

    bump_ctag(collection_for(note.get("dossier_id")))

    # Land on the freshly created note itself; thread a validated return_to so
    # the detail view's « Retour » link still points back to the caller
    # (e.g. the dossier Documents tab). Only validate when present, so the
    # common no-return_to case doesn't emit a spurious redirect_rejected event.
    clean_return = safe_internal_redirect(return_to, "") if return_to else ""
    target = url_for(
        "notes.note_detail", note_id=note["id"], return_to=clean_return or None
    )
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Detail ───────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>")
@login_required
def note_detail(note_id: str) -> str:
    """Render the note detail view."""
    note = get_note(note_id)
    if not note:
        return redirect(url_for("notes.note_list"))

    # Find tasks linked to this note
    from models.task import list_tasks
    all_dossier_tasks = list_tasks(dossier_id=note.get("dossier_id"))
    linked_tasks = [t for t in all_dossier_tasks if t.get("related_note_id") == note_id]

    ctx = _template_context()
    ctx["note"] = note
    ctx["linked_tasks"] = linked_tasks
    ctx["return_to"] = request.args.get("return_to", "")
    ctx["gabarit_error"] = _GABARIT_ERRORS.get(
        request.args.get("gabarit_erreur", "")
    )
    return render_template("notes/detail.html", **ctx)


# ── Impression via gabarit (kind « note », Phase H.3) ───────────────────

_GABARIT_ERRORS = {
    "aucun_gabarit": (
        "Aucun gabarit d'impression de note n'est configuré. Téléversez-en un "
        "dans « Gabarits » et choisissez le type « Note (impression) »."
    ),
    "fichier_introuvable": (
        "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
    ),
    "gabarit_invalide": "Le gabarit est invalide et n'a pas pu être rempli.",
    "erreur": "Erreur lors de la génération. Veuillez réessayer.",
    "htmx": "Utilisez le bouton « Imprimer (Word) » de la page de la note.",
}


def _gabarit_error_redirect(note_id: str, code: str) -> Response:
    return redirect(
        url_for("notes.note_detail", note_id=note_id, gabarit_erreur=code)
    )


@notes_bp.route("/<note_id>/gabarit-docx", methods=["POST"])
@login_required
def note_gabarit_docx(note_id: str) -> Response:
    """Fill the note-print gabarit (kind « note ») from this note and stream
    the .docx as a DIRECT download — never saved into the dossier's
    documents (user decision 2026-08-10). Analyse notes included. The note's
    Markdown body travels through the engine's rich hook (markdown →
    formatted Word content)."""
    import io as _io

    from flask import send_file
    from werkzeug.utils import secure_filename

    from models.doc_template import (
        DOCX_MIME,
        get_note_template,
        get_template_bytes,
    )
    from tz import MTL
    from utils.cabinet import cabinet_dict
    from utils.docx_fill import DocxFillError, fill_docx
    from utils.logging_setup import log_template_event, log_unexpected
    from utils.note_docx import assemble_note_print_values, build_note_context
    from utils.tracing_setup import add_attributes, span

    note = get_note(note_id)
    if not note:
        return redirect(url_for("notes.note_list"))

    # An HTMX submit landing here would swap raw .docx bytes into the page.
    if _is_htmx():
        return _gabarit_error_redirect(note_id, "htmx")

    template = get_note_template()
    if not template:
        log_template_event(
            "generation_failed", reason="no_note_print_template", note_id=note_id
        )
        return _gabarit_error_redirect(note_id, "aucun_gabarit")
    template_id = template["id"]

    dossier_id = note.get("dossier_id", "")
    dossier = get_dossier(dossier_id) if dossier_id else None
    today = datetime.now(MTL).date()

    ctx = build_note_context(
        note, dossier=dossier, firm=cabinet_dict(), today=today
    )
    values = assemble_note_print_values(template, ctx)
    add_attributes(
        template_id=template_id, note_id=note_id, field_count=len(values)
    )

    docx_bytes = get_template_bytes(template_id)
    if docx_bytes is None:
        log_template_event(
            "generation_failed",
            template_id=template_id,
            reason="template_file_unavailable",
            note_id=note_id,
        )
        return _gabarit_error_redirect(note_id, "fichier_introuvable")

    try:
        with span("template.fill", template_id=template_id, note_id=note_id):
            filled = fill_docx(docx_bytes, values, rich_values=ctx.rich_values)
    except DocxFillError:
        log_template_event(
            "generation_failed",
            template_id=template_id,
            reason="fill_error",
            note_id=note_id,
        )
        return _gabarit_error_redirect(note_id, "gabarit_invalide")
    except Exception:
        log_unexpected("note gabarit fill failed", template_id=template_id)
        log_template_event(
            "generation_failed",
            template_id=template_id,
            reason="fill_error",
            note_id=note_id,
        )
        return _gabarit_error_redirect(note_id, "erreur")

    # Filename: "{ref} - YYYY-MM-DD - {titre}" — deliberately NOT
    # projet_document_name (its « Projet » prefix is wrong for a note print).
    reference = note.get("dossier_file_number", "") or "Note"
    display = " - ".join(
        p
        for p in (reference, today.isoformat(), note.get("title", "")[:80])
        if p
    )
    out_name = secure_filename(f"{display}.docx")
    if not out_name.lower().endswith(".docx"):
        out_name = f"note_{today.isoformat()}.docx"  # accent-only title case

    log_template_event(
        "document_generated",
        template_id=template_id,
        dossier_id=dossier_id or None,
        note_id=note_id,
        source="note",
        field_count=len(values),
    )
    return send_file(
        _io.BytesIO(filled),
        mimetype=DOCX_MIME,
        as_attachment=True,
        download_name=out_name,
    )


# ── Edit ─────────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>/edit")
@login_required
def note_edit(note_id: str) -> str:
    """Render the edit form pre-filled with note data."""
    note = get_note(note_id)
    if not note:
        return redirect(url_for("notes.note_list"))

    ctx = _template_context()
    ctx.update(note=note, errors=[], return_to=request.args.get("return_to", ""))
    return render_template("notes/form.html", **ctx)


@notes_bp.route("/<note_id>", methods=["POST"])
@login_required
def note_update(note_id: str) -> str:
    """Handle edit form submission."""
    existing_note = get_note(note_id)
    if not existing_note:
        return redirect(url_for("notes.note_list"))

    data, link_errors = _enrich_dossier_info(_form_data())
    return_to = request.form.get("return_to", "")

    # The « Théorie de la cause » note is bound to its dossier: the form
    # locks the picker, so a differing dossier_id can only come from a
    # hand-crafted POST. Moving (or clearing) it would break the
    # one-analyse-per-dossier invariant and strand the lawyer's analysis
    # where no app view lists it.
    if not link_errors and existing_note.get("is_analyse") and (
        (data.get("dossier_id") or "")
        != (existing_note.get("dossier_id") or "")
    ):
        link_errors = [
            "La note d'analyse est liée à son dossier — le dossier ne peut "
            "pas être modifié."
        ]

    note, errors = (
        (None, link_errors) if link_errors else update_note(note_id, data)
    )

    if errors:
        data["id"] = note_id
        data["dossier_file_number"] = data.get(
            "dossier_file_number", request.form.get("dossier_display", "")
        )
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(note=data, errors=errors, return_to=return_to)
        return render_template("notes/form.html", **ctx)

    old_scope = collection_for(existing_note.get("dossier_id"))
    new_scope = collection_for(note.get("dossier_id"))
    if old_scope != new_scope:
        # Note moved between collections (dossier <-> dossier, or to/from
        # « Général »). Deletions travel ONLY via tombstones — without one
        # the old collection's DavX5 copy survives forever (sync-collection
        # reports live members + tombstones; an unmentioned href reads as
        # "unchanged"). Mirrors routes/tasks.py task_update.
        record_tombstone(old_scope, note_id)
        bump_ctag(old_scope)
        # The note (re)enters its new collection — drop any stale tombstone
        # there so one sync REPORT never reports it as both live and deleted.
        remove_tombstone(new_scope, note_id)
    bump_ctag(new_scope)

    # Return to the note itself after saving; thread a validated return_to for
    # the detail view's « Retour » link (see note_create for the guard rationale).
    clean_return = safe_internal_redirect(return_to, "") if return_to else ""
    target = url_for(
        "notes.note_detail", note_id=note_id, return_to=clean_return or None
    )
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Delete ───────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>/delete", methods=["POST"])
@login_required
def note_delete(note_id: str) -> str:
    """Delete a note and redirect to the list (or back to the caller)."""
    existing_note = get_note(note_id)
    dossier_id = existing_note.get("dossier_id") if existing_note else None
    return_to = request.form.get("return_to", "")

    success, error = delete_note(note_id)

    if success:
        # Tombstone BEFORE bumping: the bump makes DavX5 re-sync, and the
        # sync report announces deletions only through tombstones — a bare
        # bump leaves the deleted note on the phone forever (mirrors
        # routes/tasks.py task_delete; was the note-delete gap the Analyse
        # init→delete→re-init cycle made visible).
        scope = collection_for(dossier_id)
        record_tombstone(scope, note_id)
        bump_ctag(scope)
        # Append-only deletion trail (PA-G06) — the snapshot keeps the
        # title only, never the note's content.
        record_deletion(
            "note", note_id,
            dossier_id=dossier_id or "",
            title=(existing_note or {}).get("title", ""),
            status=(existing_note or {}).get("category", ""),
        )

    target = safe_internal_redirect(return_to, url_for("notes.note_list"))
    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(target)


# ── Pin toggle ───────────────────────────────────────────────────────────


# ── Export ──────────��────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("created_at", "Date"),
    ("title", "Titre"),
    ("dossier_file_number", "Dossier"),
    ("category", "Catégorie"),
    ("content", "Contenu"),
]

_EXPORT_COLUMNS_PDF = [
    ("created_at", "Date", 1.0),
    ("title", "Titre", 2.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("category", "Catégorie", 1.0),
    ("content", "Contenu", 3.0),
]


def _get_export_notes() -> list[dict]:
    """Fetch and pre-process notes for export, respecting current filters."""
    from utils.export_csv import prepare_export_rows

    dossier_filter = request.args.get("dossier_id", "").strip()
    category_filter = request.args.get("category", "").strip()
    search_query = request.args.get("q", "").strip()

    notes = list_notes(
        dossier_id=dossier_filter or None,
        category=category_filter or None,
        search=search_query or None,
    )
    return prepare_export_rows(notes, label_maps={"category": CATEGORY_LABELS})


@notes_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export notes as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_notes()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"notes_{date_str}.csv",
    )


@notes_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export notes as PDF report."""
    from utils.export_pdf import export_pdf

    rows = _get_export_notes()
    date_str = datetime.now().strftime("%Y-%m-%d")
    return export_pdf(
        rows=rows,
        columns=_EXPORT_COLUMNS_PDF,
        title="Notes",
        filename=f"notes_{date_str}.pdf",
    )


# ── Pin toggle ────────────────────────────────────────���──────────────────


@notes_bp.route("/<note_id>/pin", methods=["POST"])
@login_required
def note_pin(note_id: str) -> str:
    """Toggle pin status of a note."""
    note, errors = toggle_pin(note_id)

    if errors:
        if _is_htmx():
            return f'<div class="text-red-600 text-sm">{escape(errors[0])}</div>', 422
        return redirect(url_for("notes.note_list"))

    bump_ctag(collection_for(note.get("dossier_id")))

    if _is_htmx():
        # Redirect back to where the user was
        referer = request.headers.get("Referer", "")
        if f"/notes/{note_id}" in referer:
            resp = redirect(url_for("notes.note_detail", note_id=note_id))
            resp.headers["HX-Redirect"] = url_for("notes.note_detail", note_id=note_id)
            return resp
        resp = redirect(url_for("notes.note_list"))
        resp.headers["HX-Redirect"] = url_for("notes.note_list")
        return resp

    return redirect(url_for("notes.note_detail", note_id=note_id))
