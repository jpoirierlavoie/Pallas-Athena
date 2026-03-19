"""Document Firestore CRUD and Firebase Storage operations."""

import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import google.auth
from google.auth.transport import requests as auth_requests
from google.cloud.firestore_v1.base_query import FieldFilter
from firebase_admin import storage
from models import db
from security import sanitize

# Firestore collection path
COLLECTION = "documents"

# Allowed MIME types for upload
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "image/tiff",
}

# Allowed extensions (fallback when MIME detection fails)
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

# Max upload size: 25 MB
MAX_FILE_SIZE = 25 * 1024 * 1024

# Valid document categories
VALID_CATEGORIES = (
    "procédure",
    "pièce",
    "correspondance",
    "preuve",
    "jugement",
    "entente",
    "note",
    "autre",
)

# Display labels (French)
CATEGORY_LABELS = {
    "procédure": "Procédure",
    "pièce": "Pièce",
    "correspondance": "Correspondance",
    "preuve": "Preuve",
    "jugement": "Jugement",
    "entente": "Entente",
    "note": "Note",
    "autre": "Autre",
}

# File type icons (category for template rendering)
FILE_TYPE_ICONS = {
    "application/pdf": "pdf",
    "application/msword": "word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "image/jpeg": "image",
    "image/png": "image",
    "image/tiff": "image",
}


def _default_doc() -> dict:
    """Return a dict with every document field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "filename": "",
        "original_filename": "",
        "display_name": "",
        "file_type": "",
        "file_size": 0,
        "storage_path": "",
        "category": "autre",
        "description": "",
        "tags": [],
        "folder_id": None,
        "version": 1,
        "parent_document_id": None,
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        elif isinstance(val, list):
            out[key] = [sanitize(v, max_length=200) if isinstance(v, str) else v for v in val]
        else:
            out[key] = val
    return out


def _validate_metadata(data: dict) -> list[str]:
    """Validate document metadata fields. Returns list of error messages."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à ce document.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    return errors


def _validate_file(filename: str, file_size: int) -> list[str]:
    """Validate file name, extension and size. Returns list of error messages."""
    errors: list[str] = []

    if not filename:
        errors.append("Le nom du fichier est requis.")
        return errors

    # Check extension
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        errors.append(
            "Type de fichier non autorisé. Formats acceptés : PDF, DOCX, DOC, JPG, PNG, TIFF."
        )

    if file_size > MAX_FILE_SIZE:
        errors.append("Le fichier dépasse la taille maximale de 25 Mo.")

    if file_size == 0:
        errors.append("Le fichier est vide.")

    return errors


def format_file_size(size_bytes: int) -> str:
    """Format byte count into human-readable string (Ko/Mo)."""
    if size_bytes < 1024:
        return f"{size_bytes} o"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} Ko"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} Mo"


def get_file_icon(file_type: str) -> str:
    """Return an icon category string based on MIME type."""
    return FILE_TYPE_ICONS.get(file_type, "file")


# ── CRUD ──────────────────────────────────────────────────────────────────


def upload_document(
    dossier_id: str,
    dossier_file_number: str,
    file_stream,
    filename: str,
    file_size: int,
    metadata: dict,
    user_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Upload a file to Firebase Storage and create a Firestore record.

    Returns (doc, errors).
    """
    # Validate file
    file_errors = _validate_file(filename, file_size)
    if file_errors:
        return None, file_errors

    # Detect MIME type
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"

    # Build metadata
    merged = {**_default_doc(), **_sanitize_data(metadata)}
    merged["dossier_id"] = dossier_id
    merged["dossier_file_number"] = dossier_file_number

    # Validate folder_id if provided
    folder_id = merged.get("folder_id")
    if folder_id:
        from models.folder import get_folder
        folder = get_folder(dossier_id, folder_id)
        if not folder:
            return None, ["Le dossier de destination est introuvable."]

    # Validate metadata
    meta_errors = _validate_metadata(merged)
    if meta_errors:
        return None, meta_errors

    now = datetime.now(timezone.utc)
    document_id = str(uuid.uuid4())
    etag = str(uuid.uuid4())

    # Sanitize filename for storage (keep original for display)
    safe_filename = filename.replace("/", "_").replace("\\", "_")
    storage_path = f"users/{user_id}/dossiers/{dossier_id}/documents/{document_id}/{safe_filename}"

    merged.update({
        "id": document_id,
        "filename": safe_filename,
        "original_filename": filename,
        "display_name": merged.get("display_name") or filename.rsplit(".", 1)[0],
        "file_type": content_type,
        "file_size": file_size,
        "storage_path": storage_path,
        "created_at": now,
        "updated_at": now,
        "etag": etag,
    })

    # Upload to Firebase Storage
    try:
        bucket = storage.bucket()
        blob = bucket.blob(storage_path)
        blob.upload_from_file(file_stream, content_type=content_type)
    except Exception as exc:
        return None, [f"Erreur lors du téléversement : {exc}"]

    # Save metadata to Firestore
    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception as exc:
        # Attempt to clean up the uploaded file
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except Exception:
            pass
        return None, [f"Erreur lors de la sauvegarde des métadonnées : {exc}"]

    return merged, []


def get_document(document_id: str) -> Optional[dict]:
    """Fetch a single document metadata by ID."""
    try:
        doc = db.collection(COLLECTION).document(document_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


# Sentinel value: distinguishes "no folder filter" from "filter to root (None)"
_UNSET = object()


def list_documents(
    dossier_id: Optional[str] = None,
    folder_id: object = _UNSET,
    category: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "created_at",
) -> list[dict]:
    """Return documents, optionally filtered by dossier, folder, category, search.

    folder_id behaviour:
    - _UNSET (default): no folder filter, return all documents
    - None: return only documents at dossier root (folder_id is None)
    - str: return only documents in that specific folder
    - When search is active, folder_id filter is ignored (search across all)
    """
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        if category and category in VALID_CATEGORIES:
            query = query.where(filter=FieldFilter("category", "==", category))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side search (across all folders)
        if search:
            term = search.lower()
            filtered = []
            for d in results:
                searchable = " ".join([
                    d.get("display_name", ""),
                    d.get("filename", ""),
                    d.get("description", ""),
                    " ".join(d.get("tags", [])),
                ]).lower()
                if term in searchable:
                    filtered.append(d)
            results = filtered
        elif folder_id is not _UNSET:
            # Filter by folder (only when not searching)
            results = [d for d in results if d.get("folder_id") == folder_id]

        # Sort
        if sort_by == "name":
            results.sort(key=lambda d: (d.get("display_name") or "").lower())
        elif sort_by == "size":
            results.sort(key=lambda d: d.get("file_size", 0), reverse=True)
        else:
            # Default: by date, newest first
            results.sort(
                key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )

        return results
    except Exception:
        return []


def update_metadata(
    document_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update document metadata (display_name, category, tags, description)."""
    existing = get_document(document_id)
    if not existing:
        return None, ["Document introuvable."]

    # Only allow updating specific metadata fields
    allowed_fields = {"display_name", "category", "description", "tags"}
    sanitized = _sanitize_data({k: v for k, v in data.items() if k in allowed_fields})
    merged = {**existing, **sanitized}

    # Validate
    if merged.get("category") and merged["category"] not in VALID_CATEGORIES:
        return None, ["Catégorie invalide."]

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_document(document_id: str) -> tuple[bool, str]:
    """Delete a document from both Firebase Storage and Firestore."""
    existing = get_document(document_id)
    if not existing:
        return False, "Document introuvable."

    storage_path = existing.get("storage_path", "")

    # Delete from Firebase Storage
    if storage_path:
        try:
            bucket = storage.bucket()
            blob = bucket.blob(storage_path)
            blob.delete()
        except Exception as exc:
            return False, f"Erreur lors de la suppression du fichier : {exc}"

    # Delete from Firestore
    try:
        db.collection(COLLECTION).document(document_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def get_signed_url(
    document_id: str,
    expiry_minutes: int = 15,
    download: bool = False,
) -> Optional[str]:
    """Generate a signed URL for downloading/viewing a document.

    When *download* is True the URL includes response headers that force
    the browser to save the file instead of displaying it inline.
    """
    doc = get_document(document_id)
    if not doc:
        return None

    storage_path = doc.get("storage_path", "")
    if not storage_path:
        return None

    try:
        bucket = storage.bucket()
        blob = bucket.blob(storage_path)

        query_params: dict[str, str] = {}
        if download:
            filename = doc.get("display_name") or doc.get("original_filename") or doc.get("filename", "document")
            # Ensure the filename has an extension so the OS recognises the file type
            if "." not in os.path.basename(filename):
                ext = mimetypes.guess_extension(doc.get("file_type", "")) or ""
                # mimetypes may return '.jpe' for JPEG; prefer common extensions
                if ext == ".jpe":
                    ext = ".jpg"
                filename += ext
            query_params["response-content-disposition"] = (
                f'attachment; filename="{filename}"'
            )
            content_type = doc.get("file_type")
            if content_type:
                query_params["response-content-type"] = content_type

        # On App Engine Standard, Application Default Credentials come from
        # the metadata server and lack a local private key.  Passing the
        # service account email + access token tells the library to sign
        # via the IAM signBlob API instead.
        signing_creds, _ = google.auth.default()
        signing_creds.refresh(auth_requests.Request())

        url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiry_minutes),
            method="GET",
            query_parameters=query_params,
            service_account_email=signing_creds.service_account_email,
            access_token=signing_creds.token,
        )
        return url
    except Exception:
        return None


# ── Move ─────────────────────────────────────────────────────────────────


def move_document(
    dossier_id: str,
    document_id: str,
    target_folder_id: Optional[str],
) -> tuple[Optional[dict], list[str]]:
    """Move a document to a different folder. Returns (updated_doc, errors)."""
    doc = get_document(document_id)
    if not doc:
        return None, ["Document introuvable."]
    if doc.get("dossier_id") != dossier_id:
        return None, ["Le document n'appartient pas à ce dossier."]

    # Validate target folder
    if target_folder_id:
        from models.folder import get_folder
        folder = get_folder(dossier_id, target_folder_id)
        if not folder:
            return None, ["Le dossier de destination est introuvable."]

    now = datetime.now(timezone.utc)
    doc["folder_id"] = target_folder_id
    doc["updated_at"] = now
    doc["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(document_id).set(doc)
    except Exception as exc:
        return None, [f"Erreur lors du déplacement : {exc}"]

    return doc, []


def move_documents_bulk(
    dossier_id: str,
    document_ids: list[str],
    target_folder_id: Optional[str],
) -> tuple[int, list[str]]:
    """Move multiple documents to a folder. Returns (count_moved, errors)."""
    # Validate target folder
    if target_folder_id:
        from models.folder import get_folder
        folder = get_folder(dossier_id, target_folder_id)
        if not folder:
            return 0, ["Le dossier de destination est introuvable."]

    now = datetime.now(timezone.utc)
    moved = 0
    errors: list[str] = []
    batch = db.batch()

    for doc_id in document_ids:
        doc = get_document(doc_id)
        if not doc:
            errors.append(f"Document {doc_id} introuvable.")
            continue
        if doc.get("dossier_id") != dossier_id:
            errors.append(f"Document {doc_id} n'appartient pas à ce dossier.")
            continue

        ref = db.collection(COLLECTION).document(doc_id)
        batch.update(ref, {
            "folder_id": target_folder_id,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        moved += 1

    if moved > 0:
        try:
            batch.commit()
        except Exception as exc:
            return 0, [f"Erreur lors du déplacement : {exc}"]

    return moved, errors


# ── Summary ──────────────────────────────────────────────────────────────


def get_document_summary(dossier_id: str) -> dict:
    """Return summary stats for a dossier's documents."""
    docs = list_documents(dossier_id=dossier_id)
    total_size = sum(d.get("file_size", 0) for d in docs)

    return {
        "total": len(docs),
        "total_size": total_size,
        "total_size_formatted": format_file_size(total_size),
    }
