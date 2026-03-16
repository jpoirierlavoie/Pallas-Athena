"""Document management routes — upload, list, detail, edit, delete, download."""

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required
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
    update_metadata,
    upload_document,
)

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


# ── List ──────────────────────────────────────────────────────────────────


@documents_bp.route("/")
@login_required
def document_list() -> str:
    """Render the document list with optional filters."""
    dossier_id = request.args.get("dossier_id", "").strip()
    category_filter = request.args.get("category", "").strip()
    search = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "created_at")

    documents = list_documents(
        dossier_id=dossier_id or None,
        category=category_filter or None,
        search=search or None,
        sort_by=sort_by,
    )

    # Attach computed fields
    for d in documents:
        d["_file_size_fmt"] = format_file_size(d.get("file_size", 0))
        d["_file_icon"] = get_file_icon(d.get("file_type", ""))

    ctx = {
        "documents": documents,
        "dossier_id": dossier_id,
        "category_filter": category_filter,
        "search": search,
        "sort_by": sort_by,
        "category_labels": CATEGORY_LABELS,
        "valid_categories": VALID_CATEGORIES,
    }

    if _is_htmx():
        return render_template("documents/_document_rows.html", **ctx)

    # Load dossier list for the filter dropdown
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

    # Generate a signed URL for viewing/downloading
    signed_url = get_signed_url(document_id)

    return render_template(
        "documents/detail.html",
        document=doc,
        signed_url=signed_url,
        category_labels=CATEGORY_LABELS,
    )


# ── Download ──────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/download")
@login_required
def document_download(document_id: str) -> str:
    """Redirect to a signed download URL."""
    signed_url = get_signed_url(document_id)
    if not signed_url:
        return redirect(url_for("documents.document_list"))
    return redirect(signed_url)


# ── Upload ────────────────────────────────────────────────────────────────


@documents_bp.route("/upload", methods=["GET"])
@login_required
def document_upload_form() -> str:
    """Render the upload form."""
    dossier_id = request.args.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None

    return render_template(
        "documents/upload.html",
        dossier=dossier,
        dossiers=list_dossiers(),
        category_labels=CATEGORY_LABELS,
        errors=[],
    )


@documents_bp.route("/upload", methods=["POST"])
@login_required
def document_upload() -> str:
    """Handle file upload(s)."""
    dossier_id = request.form.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None

    if not dossier:
        errors = ["Veuillez sélectionner un dossier."]
        return render_template(
            "documents/upload.html",
            dossier=None,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
            errors=errors,
        )

    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        errors = ["Veuillez sélectionner au moins un fichier."]
        return render_template(
            "documents/upload.html",
            dossier=dossier,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
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

        # Read file content to get size
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        metadata = {
            "category": category,
            "description": description,
            "tags": tags,
            "display_name": request.form.get("display_name", "").strip() or "",
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
        return render_template(
            "documents/upload.html",
            dossier=dossier,
            dossiers=list_dossiers(),
            category_labels=CATEGORY_LABELS,
            errors=all_errors,
        )

    # Redirect back to dossier detail (documents tab) or document list
    target = url_for("dossiers.dossier_detail", dossier_id=dossier_id)
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


# ── Delete ────────────────────────────────────────────────────────────────


@documents_bp.route("/<document_id>/delete", methods=["POST"])
@login_required
def document_delete(document_id: str) -> str:
    """Delete a document and redirect."""
    doc = get_document(document_id)
    dossier_id = doc.get("dossier_id", "") if doc else ""

    success, error = delete_document(document_id)

    if _is_htmx():
        if success:
            if dossier_id:
                target = url_for("dossiers.dossier_detail", dossier_id=dossier_id)
            else:
                target = url_for("documents.document_list")
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{error}</div>', 422

    if dossier_id:
        return redirect(url_for("dossiers.dossier_detail", dossier_id=dossier_id))
    return redirect(url_for("documents.document_list"))
