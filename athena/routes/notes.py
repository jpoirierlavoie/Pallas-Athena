"""Dossier note routes — list, detail, create, edit, delete, pin."""

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from dav.sync import bump_ctag
from models.note import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    create_note,
    delete_note,
    get_note,
    list_notes,
    toggle_pin,
    update_note,
)
from models.dossier import get_dossier, list_dossiers

notes_bp = Blueprint("notes", __name__, url_prefix="/notes")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _template_context() -> dict:
    """Return shared template context for note views."""
    return {
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
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
            data["dossier_id"] = ""
            data["dossier_file_number"] = ""
            data["dossier_title"] = ""
    else:
        data["dossier_id"] = ""
        data["dossier_file_number"] = ""
        data["dossier_title"] = ""
    return data


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


@notes_bp.route("/")
@login_required
def note_list() -> str:
    """Render the note list view."""
    dossier_filter = request.args.get("dossier_id", "").strip()
    category_filter = request.args.get("category", "").strip()
    search_query = request.args.get("q", "").strip()

    notes = list_notes(
        dossier_id=dossier_filter or None,
        category=category_filter or None,
        search=search_query or None,
    )

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
    ctx.update(note=prefilled, errors=[])
    return render_template("notes/form.html", **ctx)


@notes_bp.route("/", methods=["POST"])
@login_required
def note_create() -> str:
    """Handle new note form submission."""
    data = _form_data()
    data = _enrich_dossier_info(data)

    note, errors = create_note(data)

    if errors:
        ctx = _template_context()
        data["dossier_file_number"] = data.get(
            "dossier_file_number", request.form.get("dossier_display", "")
        )
        data["dossier_title"] = data.get("dossier_title", "")
        ctx.update(note=data, errors=errors)
        return render_template("notes/form.html", **ctx)

    if note.get("dossier_id"):
        bump_ctag(f"dossier:{note['dossier_id']}")

    if _is_htmx():
        resp = redirect(url_for("notes.note_list"))
        resp.headers["HX-Redirect"] = url_for("notes.note_list")
        return resp

    return redirect(url_for("notes.note_list"))


# ── Detail ───────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>")
@login_required
def note_detail(note_id: str) -> str:
    """Render the note detail view."""
    note = get_note(note_id)
    if not note:
        return redirect(url_for("notes.note_list"))

    ctx = _template_context()
    ctx["note"] = note
    return render_template("notes/detail.html", **ctx)


# ── Edit ─────────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>/edit")
@login_required
def note_edit(note_id: str) -> str:
    """Render the edit form pre-filled with note data."""
    note = get_note(note_id)
    if not note:
        return redirect(url_for("notes.note_list"))

    ctx = _template_context()
    ctx.update(note=note, errors=[])
    return render_template("notes/form.html", **ctx)


@notes_bp.route("/<note_id>", methods=["POST"])
@login_required
def note_update(note_id: str) -> str:
    """Handle edit form submission."""
    existing_note = get_note(note_id)
    if not existing_note:
        return redirect(url_for("notes.note_list"))

    data = _form_data()
    data = _enrich_dossier_info(data)

    note, errors = update_note(note_id, data)

    if errors:
        data["id"] = note_id
        data["dossier_file_number"] = data.get(
            "dossier_file_number", request.form.get("dossier_display", "")
        )
        data["dossier_title"] = data.get("dossier_title", "")
        ctx = _template_context()
        ctx.update(note=data, errors=errors)
        return render_template("notes/form.html", **ctx)

    if note.get("dossier_id"):
        bump_ctag(f"dossier:{note['dossier_id']}")

    if _is_htmx():
        resp = redirect(url_for("notes.note_detail", note_id=note_id))
        resp.headers["HX-Redirect"] = url_for("notes.note_detail", note_id=note_id)
        return resp

    return redirect(url_for("notes.note_detail", note_id=note_id))


# ── Delete ───────────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>/delete", methods=["POST"])
@login_required
def note_delete(note_id: str) -> str:
    """Delete a note and redirect to the list."""
    existing_note = get_note(note_id)
    dossier_id = existing_note.get("dossier_id") if existing_note else None

    success, error = delete_note(note_id)

    if success and dossier_id:
        bump_ctag(f"dossier:{dossier_id}")

    if _is_htmx():
        if success:
            resp = redirect(url_for("notes.note_list"))
            resp.headers["HX-Redirect"] = url_for("notes.note_list")
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    return redirect(url_for("notes.note_list"))


# ── Pin toggle ───────────────────────────────────────────────────────────


@notes_bp.route("/<note_id>/pin", methods=["POST"])
@login_required
def note_pin(note_id: str) -> str:
    """Toggle pin status of a note."""
    note, errors = toggle_pin(note_id)

    if errors:
        if _is_htmx():
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        return redirect(url_for("notes.note_list"))

    if note.get("dossier_id"):
        bump_ctag(f"dossier:{note['dossier_id']}")

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
