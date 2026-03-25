"""Document management routes — upload, list, detail, edit, delete, download.

Includes folder management routes for hierarchical document organization.
"""

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required
from pagination import paginate
from models.dossier import get_dossier, list_dossiers
from models.document import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    delete_document,
    format_file_size,
    get_document,
    get_document_summary,
    get_file_icon,
    get_signed_url,
    list_documents,
    move_document,
    move_documents_bulk,
    update_metadata,
    upload_document,
)
from models.folder import (
    create_folder,
    delete_folder,
    get_folder,
    get_folder_breadcrumb,
    get_folder_tree,
    list_folders,
    move_folder,
    rename_folder,
)

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _attach_computed_fields(documents: list[dict]) -> None:
    """Attach display helpers to document dicts."""
    for d in documents:
        d["_file_size_fmt"] = format_file_size(d.get("file_size", 0))
        d["_file_icon"] = get_file_icon(d.get("file_type", ""))


def _attach_folder_counts(folders: list[dict], dossier_id: str) -> None:
    """Attach item counts to folder dicts for display."""
    from models.folder import _count_items
    for f in folders:
        counts = _count_items(dossier_id, f["id"])
        f["_item_count"] = counts["folders"] + counts["documents"]


# ── List / Browser ───────────────────────────────────────────────────────


@documents_bp.route("/")
@login_required
def document_list() -> str:
    """Render the document browser with folder navigation."""
    dossier_id = request.args.get("dossier_id", "").strip()
    folder_id = request.args.get("folder_id", "").strip() or None
    category_filter = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "created_at")
    page = request.args.get("page", 1, type=int)

    # When a dossier is selected and not searching, filter by folder
    if dossier_id and not search:
        documents = list_documents(
            dossier_id=dossier_id,
            folder_id=folder_id,
            category=category_filter or None,
            sort_by=sort_by,
        )
        folders_list = list_folders(dossier_id, parent_folder_id=folder_id)
        _attach_folder_counts(folders_list, dossier_id)
        breadcrumb = get_folder_breadcrumb(dossier_id, folder_id)
    elif dossier_id and search:
        # Search across all folders
        documents = list_documents(
            dossier_id=dossier_id,
            category=category_filter or None,
            search=search,
            sort_by=sort_by,
        )
        folders_list = []
        breadcrumb = []
    else:
        # No dossier selected — show all documents flat
        documents = list_documents(
            dossier_id=None,
            category=category_filter or None,
            search=search or None,
            sort_by=sort_by,
        )
        folders_list = []
        breadcrumb = []

    _attach_computed_fields(documents)

    documents, pagination = paginate(documents, page)
    pagination["url"] = url_for("documents.document_list")
    pagination["target"] = "#browser-content"
    if folder_id:
        pagination["extra_vals"] = {"folder_id": folder_id}

    ctx = {
        "documents": documents,
        "folders": folders_list,
        "breadcrumb": breadcrumb,
        "dossier_id": dossier_id,
        "folder_id": folder_id,
        "category_filter": category_filter,
        "search": search,
        "sort_by": sort_by,
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
        "pagination": pagination,
    }

    if _is_htmx():
        return render_template("documents/_browser.html", **ctx)

    ctx["dossiers"] = list_dossiers()
    return render_template("documents/list.html", **ctx)


# ── Detail ────────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>")
@login_required
def document_detail(document_id: str) -> str:
    """Render the document detail/viewer page."""
    doc = get_document(document_id)
    if not doc:
        return redirect(url_for("documents.document_list"))

    doc["_file_size_fmt"] = format_file_size(doc.get("file_size", 0))
    doc["_file_icon"] = get_file_icon(doc.get("file_type", ""))

    signed_url = get_signed_url(document_id)

    # Folder breadcrumb for context
    dossier_id = doc.get("dossier_id", "")
    folder_breadcrumb = get_folder_breadcrumb(dossier_id, doc.get("folder_id"))

    # Folder tree for the move modal
    folder_tree = get_folder_tree(dossier_id) if dossier_id else []

    return render_template(
        "documents/detail.html",
        document=doc,
        signed_url=signed_url,
        category_labels=CATEGORY_LABELS,
        folder_breadcrumb=folder_breadcrumb,
        folder_tree=folder_tree,
    )


# ── Download ──────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/download")
@login_required
def document_download(document_id: str) -> str:
    """Redirect to a signed download URL."""
    signed_url = get_signed_url(document_id, download=True)
    if not signed_url:
        return redirect(url_for("documents.document_list"))
    return redirect(signed_url)


# ── Upload ────────────────────────────────────────────────────────────────


@documents_bp.route("/upload", methods=["GET"])
@login_required
def document_upload_form() -> str:
    """Render the upload form."""
    dossier_id = request.args.get("dossier_id", "").strip()
    folder_id = request.args.get("folder_id", "").strip() or None
    dossier = get_dossier(dossier_id) if dossier_id else None

    # Folder breadcrumb for context
    folder_breadcrumb = []
    if dossier_id and folder_id:
        folder_breadcrumb = get_folder_breadcrumb(dossier_id, folder_id)

    return render_template(
        "documents/upload.html",
        dossier=dossier,
        dossiers=list_dossiers(),
        category_labels=CATEGORY_LABELS,
        folder_id=folder_id,
        folder_breadcrumb=folder_breadcrumb,
        errors=[],
    )


@documents_bp.route("/upload", methods=["POST"])
@login_required
def document_upload() -> str:
    """Handle file upload(s)."""
    dossier_id = request.form.get("dossier_id", "").strip()
    folder_id = request.form.get("folder_id", "").strip() or None
    dossier = get_dossier(dossier_id) if dossier_id else None

    if not dossier:
        errors = ["Veuillez sélectionner un dossier."]
        return render_template(
            "documents/upload.html",
            dossier=None,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
            folder_id=folder_id,
            folder_breadcrumb=[],
            errors=errors,
        )

    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        folder_breadcrumb = get_folder_breadcrumb(dossier_id, folder_id) if folder_id else []
        errors = ["Veuillez sélectionner au moins un fichier."]
        return render_template(
            "documents/upload.html",
            dossier=dossier,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
            folder_id=folder_id,
            folder_breadcrumb=folder_breadcrumb,
            errors=errors,
        )

    user_id = session.get("user_id", "unknown")
    category = request.form.get("category", "autre").strip()
    description = request.form.get("description", "").strip()
    tags_raw = request.form.get("tags", "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

    uploaded = []
    all_errors: list[str] = []

    for f in files:
        if not f.filename:
            continue

        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        metadata = {
            "category": category,
            "description": description,
            "tags": tags,
            "display_name": request.form.get("display_name", "").strip() or "",
            "folder_id": folder_id,
        }

        doc, errors = upload_document(
            dossier_id=dossier_id,
            dossier_file_number=dossier.get("file_number", ""),
            file_stream=f,
            filename=f.filename,
            file_size=file_size,
            metadata=metadata,
            user_id=user_id,
        )

        if errors:
            all_errors.extend([f"{f.filename} : {e}" for e in errors])
        elif doc:
            uploaded.append(doc)

    if all_errors and not uploaded:
        folder_breadcrumb = get_folder_breadcrumb(dossier_id, folder_id) if folder_id else []
        return render_template(
            "documents/upload.html",
            dossier=dossier,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
            folder_id=folder_id,
            folder_breadcrumb=folder_breadcrumb,
            errors=all_errors,
        )

    # Redirect back to document browser at the current folder
    target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=folder_id or "")
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Edit metadata ─────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/edit")
@login_required
def document_edit(document_id: str) -> str:
    """Render the metadata edit form."""
    doc = get_document(document_id)
    if not doc:
        return redirect(url_for("documents.document_list"))

    return render_template(
        "documents/edit.html",
        document=doc,
        category_labels=CATEGORY_LABELS,
        errors=[],
    )


@documents_bp.route("/<document_id>/edit", methods=["POST"])
@login_required
def document_update(document_id: str) -> str:
    """Handle metadata edit form submission."""
    f = request.form
    tags_raw = f.get("tags", "").strip()

    data = {
        "display_name": f.get("display_name", "").strip(),
        "category": f.get("category", "autre").strip(),
        "description": f.get("description", "").strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else [],
    }

    doc, errors = update_metadata(document_id, data)

    if errors:
        existing = get_document(document_id) or {}
        existing.update(data)
        return render_template(
            "documents/edit.html",
            document=existing,
            category_labels=CATEGORY_LABELS,
            errors=errors,
        )

    target = url_for("documents.document_detail", document_id=document_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Move document ─────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/move", methods=["POST"])
@login_required
def document_move(document_id: str) -> str:
    """Move a document to a different folder."""
    doc = get_document(document_id)
    if not doc:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Document introuvable.</div>', 404
        return redirect(url_for("documents.document_list"))

    dossier_id = doc["dossier_id"]
    target_folder_id = request.form.get("target_folder_id", "").strip() or None

    updated_doc, errors = move_document(dossier_id, document_id, target_folder_id)

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        resp = redirect(url_for("documents.document_detail", document_id=document_id))
        resp.headers["HX-Redirect"] = url_for("documents.document_detail", document_id=document_id)
        return resp

    return redirect(url_for("documents.document_detail", document_id=document_id))


@documents_bp.route("/move-bulk", methods=["POST"])
@login_required
def document_move_bulk() -> str:
    """Move multiple documents to a folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    target_folder_id = request.form.get("target_folder_id", "").strip() or None
    doc_ids = request.form.getlist("document_ids")

    if not dossier_id or not doc_ids:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Paramètres manquants.</div>', 422
        return redirect(url_for("documents.document_list"))

    moved, errors = move_documents_bulk(dossier_id, doc_ids, target_folder_id)

    target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=target_folder_id or "")
    if _is_htmx():
        if errors and moved == 0:
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Delete ────────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/delete", methods=["POST"])
@login_required
def document_delete(document_id: str) -> str:
    """Delete a document and redirect."""
    doc = get_document(document_id)
    dossier_id = doc.get("dossier_id", "") if doc else ""
    folder_id = doc.get("folder_id") if doc else None

    success, error = delete_document(document_id)

    if _is_htmx():
        if success:
            if dossier_id:
                target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=folder_id or "")
            else:
                target = url_for("documents.document_list")
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    if dossier_id:
        return redirect(url_for("documents.document_list", dossier_id=dossier_id, folder_id=folder_id or ""))
    return redirect(url_for("documents.document_list"))


# ── Folder CRUD routes ───────────────────────────────────────────────────


@documents_bp.route("/folders/create", methods=["POST"])
@login_required
def folder_create() -> str:
    """Create a new folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    name = request.form.get("name", "").strip()
    parent_folder_id = request.form.get("parent_folder_id", "").strip() or None

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    folder, errors = create_folder(dossier_id, name, parent_folder_id)

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        # Refresh the browser at the current folder location
        target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=parent_folder_id or "")
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(url_for("documents.document_list", dossier_id=dossier_id, folder_id=parent_folder_id or ""))


@documents_bp.route("/folders/<folder_id>/rename", methods=["POST"])
@login_required
def folder_rename(folder_id: str) -> str:
    """Rename a folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    new_name = request.form.get("new_name", "").strip()

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    folder, errors = rename_folder(dossier_id, folder_id, new_name)

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        parent_id = folder.get("parent_folder_id") if folder else None
        target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=parent_id or "")
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(url_for("documents.document_list", dossier_id=dossier_id))


@documents_bp.route("/folders/<folder_id>/move", methods=["POST"])
@login_required
def folder_move(folder_id: str) -> str:
    """Move a folder to a new parent."""
    dossier_id = request.form.get("dossier_id", "").strip()
    new_parent_folder_id = request.form.get("new_parent_folder_id", "").strip() or None

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    folder, errors = move_folder(dossier_id, folder_id, new_parent_folder_id)

    if _is_htmx():
        if errors:
            return f'<div class="text-red-600 text-sm">{errors[0]}</div>', 422
        target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=new_parent_folder_id or "")
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(url_for("documents.document_list", dossier_id=dossier_id))


@documents_bp.route("/folders/<folder_id>/delete", methods=["POST"])
@login_required
def folder_delete_route(folder_id: str) -> str:
    """Delete a folder."""
    dossier_id = request.form.get("dossier_id", "").strip()
    recursive = request.form.get("recursive", "") == "true"

    if not dossier_id:
        if _is_htmx():
            return '<div class="text-red-600 text-sm">Dossier juridique requis.</div>', 422
        return redirect(url_for("documents.document_list"))

    # Get parent before deleting
    folder_data = get_folder(dossier_id, folder_id)
    parent_id = folder_data.get("parent_folder_id") if folder_data else None

    success, error = delete_folder(dossier_id, folder_id, recursive=recursive)

    if _is_htmx():
        if not success:
            return f'<div class="text-red-600 text-sm">{error}</div>', 422
        target = url_for("documents.document_list", dossier_id=dossier_id, folder_id=parent_id or "")
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(url_for("documents.document_list", dossier_id=dossier_id, folder_id=parent_id or ""))


# ── Folder tree API (for move modal) ─────────────────────────────────────


@documents_bp.route("/folder-tree")
@login_required
def folder_tree_partial() -> str:
    """Return folder tree HTML for move modal."""
    dossier_id = request.args.get("dossier_id", "").strip()
    if not dossier_id:
        return ""
    tree = get_folder_tree(dossier_id)
    return render_template(
        "documents/_folder_tree.html",
        folder_tree=tree,
        dossier_id=dossier_id,
    )
