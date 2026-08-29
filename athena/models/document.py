"""Document Firestore CRUD and Firebase Storage operations."""

import logging
import mimetypes
import os
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import BinaryIO, NamedTuple, Optional
from urllib.parse import quote

import google.auth
from google.auth.transport import requests as auth_requests
from google.cloud.exceptions import NotFound
from google.cloud.firestore_v1.base_query import FieldFilter
from firebase_admin import storage
from werkzeug.utils import secure_filename
from models import db
from security import sanitize
from tz import to_mtl
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "documents"

# All generated documents (gabarits + notes d'honoraires) land in this
# per-dossier folder (Phase H.2).
GENERATED_FOLDER_NAME = "Projets"

# Documents versés depuis la quarantaine du portail client (spec L1 §9.2)
# land in this per-dossier folder (routes/reception.py).
PORTAL_FOLDER_NAME = "Reçus du portail"


def projet_document_name(reference: str, template_name: str, day: date) -> str:
    """Uniform display name for a generated document (Phase H.2):
    ``"REF - YYYY-MM-DD - Projet Nom du gabarit"``.

    ``reference`` is the dossier's internal file number (« notre référence »);
    an empty reference is simply dropped from the front.
    """
    parts = [
        (reference or "").strip(),
        day.isoformat(),
        f"Projet {(template_name or '').strip()}".strip(),
    ]
    return " - ".join(p for p in parts if p)

# Allowed MIME types for upload (11 since the 2026-08-13 user decision —
# Excel — after 2026-08-11 widened the original 6 with ZIP and email files)
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "application/zip",
    "message/rfc822",
    "application/vnd.ms-outlook",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Allowed extensions (fallback when MIME detection fails)
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".zip", ".eml", ".msg", ".xls", ".xlsx",
}

# Expected MIME type for each allowed extension (used to detect a
# mismatch between the sniffed content and the client-supplied name)
EXTENSION_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".zip": "application/zip",
    ".eml": "message/rfc822",
    ".msg": "application/vnd.ms-outlook",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Max document size: 200 MB (user decision 2026-08-12 — was 25 MB; the
# ceiling became a pure POLICY once the byte paths stopped transiting the
# application: App Engine Standard caps any request AND response at 32 MB,
# so uploads go browser→GCS direct and ingestion is a GCS-side rewrite).
MAX_FILE_SIZE = 200 * 1024 * 1024

# Phase N — the ONE whole-object read-to-memory path for documents
# (get_document_bytes), and its ceiling. 40 MB, from arithmetic, not taste:
# the tool that consumes it (get_document_text) runs on the DEFAULT service
# too (external MCP connector — F2, ~512 MB RAM, gunicorn --timeout 60), and
# pypdf's parse memory peaks around 2-3x file size on object-stream-heavy
# PDFs; 40 MB × 3 ≈ 120 MB over the ~200 MB baseline stays comfortably
# inside the instance, and the parse time stays inside the 60 s SIGKILL
# ceiling. A 200 MB (MAX_FILE_SIZE) document would OOM the instance
# mid-request — hence the refusal happens on the Firestore SIZE METADATA,
# BEFORE any byte moves (the ingest_blob_as_document doctrine).
DOCUMENT_TEXT_MAX_BYTES = 40 * 1024 * 1024

# Valid document categories. NOTE (spec §6): « facture » and « déboursé »
# here mean DOCUMENTS (the PDF of a received invoice, a disbursement receipt),
# NOT the Honoraires / Dépenses records — a deliberate name overlap.
VALID_CATEGORIES = (
    "procédure",
    "pièce",
    "jugement",
    "correspondance",
    "déboursé",
    "facture",
    "preuve",
    "procès_verbal",
    "procès_verbal_signification",
    "procès_verbal_audience",
    "transcription",
    "mandat",
    "autre",
)

# Display labels (French)
CATEGORY_LABELS = {
    "procédure": "Procédure",
    "pièce": "Pièce",
    "jugement": "Jugement",
    "correspondance": "Correspondance",
    "déboursé": "Déboursé",
    "facture": "Facture",
    "preuve": "Preuve",
    "procès_verbal": "Procès-verbal",
    "procès_verbal_signification": "Procès-verbal de signification",
    "procès_verbal_audience": "Procès-verbal d'audience",
    "transcription": "Transcription",
    "mandat": "Mandat",
    "autre": "Autre",
}

# The categories OFFERED AT INPUT — the labels minus the legacy
# « procès_verbal », split in two on 2026-08-26 because the two documents
# share nothing: a signification PV is drawn by a huissier under oath and
# art. 119 C.p.c. closes its list of mentions; an audience PV is the
# clerk's record of what happened and may CARRY the judgment itself. One
# key could not express two disjoint sets of expected fields.
#
# ⚠ This is NOT the render vocabulary. `CATEGORY_LABELS` stays complete
# and is what the EDIT form and the list filter iterate: a legacy document
# still carrying « procès_verbal » must keep a selected option, or the
# browser falls back to the first one (« procédure ») and the next
# innocuous metadata save REWRITES the category in silence — the exact
# reclassification this split exists to avoid, introduced by the split
# itself. Reclassing is a human gesture, one document at a time, for ever
# (no bulk pass: « aucune capacité de suppression » forbids overwriting a
# classification the lawyer made).
#
# The legacy key is deliberately NOT in `_CATEGORY_MIGRATION`: folding it
# would have to GUESS between signification and audience, and the
# migration table's own invariant (a source key is never still valid)
# forbids it anyway.
CATEGORY_CHOICES = {
    key: label
    for key, label in CATEGORY_LABELS.items()
    if key != "procès_verbal"
}

# Removed category keys → live key, applied ON READ (_migrate_category),
# BEFORE validation. Mirrors models/dossier._MANDATE_TYPE_MIGRATION.
_CATEGORY_MIGRATION = {
    # A settlement is neither a judgment nor a mandate: explicit fallback.
    "entente": "autre",
    # « note » is redundant since notes became a distinct entity (late
    # July 2026 split).
    "note": "autre",
}


def _migrate_category(doc: dict) -> dict:
    """Fold a removed document-category key onto its live target (read-time)."""
    old = doc.get("category", "")
    if old in _CATEGORY_MIGRATION:
        doc["category"] = _CATEGORY_MIGRATION[old]
    return doc

# File type icons (category for template rendering)
FILE_TYPE_ICONS = {
    "application/pdf": "pdf",
    "application/msword": "word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "image/jpeg": "image",
    "image/png": "image",
    "image/tiff": "image",
    "application/zip": "archive",
    "message/rfc822": "mail",
    "application/vnd.ms-outlook": "mail",
    "application/vnd.ms-excel": "sheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "sheet",
}

# Types the app never renders inline: their blobs are stored with
# Content-Disposition: attachment so even a signed URL WITHOUT a
# response-disposition override serves a download, never a page.
_ATTACHMENT_ONLY_TYPES = {
    "application/zip",
    "message/rfc822",
    "application/vnd.ms-outlook",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# Download-filename extensions, deterministic across platforms —
# mimetypes reads OS registries and may return ".jpe" for JPEG,
# ".mht"/None for rfc822/ms-outlook depending on the host.
_DOWNLOAD_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "application/zip": ".zip",
    "message/rfc822": ".eml",
    "application/vnd.ms-outlook": ".msg",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
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
        # Le champ du JURISTE. `description` appartient à l'analyse, qui
        # la réécrit à chaque exécution : sans un second champ, écrire une
        # note et relancer une analyse s'excluaient.
        "notes_internes": "",
        "tags": [],
        # The DOCUMENT'S OWN date (a procès-verbal's service date, a
        # judgment's date…) — MANUAL, date-only at midnight UTC, never
        # derived: created_at is the upload/generation instant, which for
        # scanned or received papers is days after the event (PA-G03).
        # Absent on legacy docs; no backfill exists or is possible (the
        # display-name convention only dates generated docs, with the
        # upload date).
        "document_date": None,
        "folder_id": None,
        # Provenance du portail, en champs DÉDIÉS (2026-08-27). Elle vivait
        # dans `description` — le seul champ de texte libre offert au
        # juriste —, qu'elle rendait inutilisable. Vides sur tout document
        # qui ne vient pas du portail.
        "portail_invitation_id": "",
        "portail_lot": "",
        "portail_sha512": "",
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


def _coerce_document_date(raw) -> Optional[datetime]:
    """Coerce a form/date value into a date-only midnight-UTC datetime.

    Accepts a datetime (time dropped), a date, or a "YYYY-MM-DD" string;
    anything else — including "" (the form's « no date ») — is None. The
    convention mirrors dossier.opened_date / partie.birth_date: render with
    strftime/date_str, never to_mtl.
    """
    if isinstance(raw, datetime):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            d = datetime.strptime(raw.strip(), "%Y-%m-%d")
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


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
            "Type de fichier non autorisé. Formats acceptés : PDF, "
            "Word (DOC/DOCX), Excel (XLS/XLSX), JPG, PNG, TIFF, ZIP, "
            "courriels (EML/MSG)."
        )

    if file_size > MAX_FILE_SIZE:
        errors.append("Le fichier dépasse la taille maximale de 200 Mo.")

    if file_size == 0:
        errors.append("Le fichier est vide.")

    return errors


# Bounded sniff probe: covers every magic below plus the first header
# line of an RFC 822 message (the .eml heuristic).
_SNIFF_PROBE_BYTES = 512


def _looks_like_eml(head: bytes) -> bool:
    """True when *head* opens like an RFC 822/5322 message.

    .eml has no magic bytes, so the test is structural: after an optional
    UTF-8 BOM (some export tools prepend one), the first line must be a
    header field — a 1-77 byte field name of printable US-ASCII (no
    space, no control char) followed by a colon. Single pass over the
    bounded probe, no regex (CWE-1333 linearity doctrine). A leading
    mbox « From  » line fails (space before any colon) — deliberate.
    """
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    line = head.split(b"\n", 1)[0].rstrip(b"\r")
    name, sep, _value = line.partition(b":")
    if not sep or not 0 < len(name) <= 77:
        return False
    return all(33 <= b <= 126 for b in name)


def _sniff_content_type(file_stream: BinaryIO, ext: str) -> Optional[str]:
    """Sniff the MIME type from the stream's magic bytes (stdlib only).

    Reads the first bytes of *file_stream* then seeks back to the start.
    Returns the detected MIME type, or None when no known signature
    matches. Two container signatures are ambiguous and resolved by the
    caller-supplied extension: PK (any zip — .docx vs .zip) and OLE2
    (any compound document — .doc vs .msg). .eml has no signature at
    all, so it is recognized LAST via the header-shape heuristic — a
    real magic always wins over it (a PDF renamed .eml sniffs as PDF,
    then fails the extension-agreement check upstream).
    """
    try:
        header = file_stream.read(_SNIFF_PROBE_BYTES)
        file_stream.seek(0)
    except Exception as exc:
        logger.warning("_sniff_content_type: stream read failed: %s", type(exc).__name__)
        return None
    return _sniff_header(header, ext)


def _sniff_header(header: bytes, ext: str) -> Optional[str]:
    """Decide the MIME type from already-read leading bytes.

    Same contract as _sniff_content_type — this bytes-level seam exists so
    the GCS-side ingestion path (ingest_blob_as_document) can sniff a
    512-byte ranged read without ever holding the object's stream.
    """
    if not isinstance(header, bytes):
        return None
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89\x50\x4e\x47"):
        return "image/png"
    if header.startswith(b"\x49\x49\x2a\x00") or header.startswith(b"\x4d\x4d\x00\x2a"):
        return "image/tiff"
    if header.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2 compound document — legacy Word, Outlook .msg and legacy
        # Excel share the signature; only these extensions are trusted.
        if ext == ".doc":
            return "application/msword"
        if ext == ".msg":
            return "application/vnd.ms-outlook"
        if ext == ".xls":
            return "application/vnd.ms-excel"
        return None
    if header.startswith(b"\x50\x4b\x03\x04"):
        # ZIP container — a .docx/.xlsx IS a zip; only the zip-based
        # kinds are trusted. (An empty archive starts PK\x05\x06 and is
        # deliberately refused: no evidentiary value.)
        if ext == ".docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if ext == ".xlsx":
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == ".zip":
            return "application/zip"
        return None
    if ext == ".eml" and _looks_like_eml(header):
        return "message/rfc822"
    return None


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


def _prepare_document_record(
    dossier_id: str,
    dossier_file_number: str,
    filename: str,
    ext: str,
    content_type: str,
    file_size: int,
    metadata: dict,
    user_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Build the Firestore record + storage path shared by the two
    ingestion paths (through-app stream and GCS-side copy).

    Validates the metadata (folder, category) and sanitizes the filename
    used in the Storage path — the raw client name is kept ONLY in
    original_filename/display_name (display purposes).
    """
    merged = {**_default_doc(), **_sanitize_data(metadata)}
    merged["dossier_id"] = dossier_id
    merged["dossier_file_number"] = dossier_file_number
    merged["document_date"] = _coerce_document_date(merged.get("document_date"))

    folder_id = merged.get("folder_id")
    if folder_id:
        from models.folder import get_folder
        folder = get_folder(dossier_id, folder_id)
        if not folder:
            return None, ["Le dossier de destination est introuvable."]

    meta_errors = _validate_metadata(merged)
    if meta_errors:
        return None, meta_errors

    now = datetime.now(timezone.utc)
    document_id = str(uuid.uuid4())

    printable = "".join(ch for ch in filename if ch.isprintable())
    safe_filename = secure_filename(printable)
    if len(safe_filename) > 200:
        safe_filename = safe_filename[: 200 - len(ext)] + ext
    if not safe_filename or not safe_filename.lower().endswith(ext):
        safe_filename = "document" + ext
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
        "etag": str(uuid.uuid4()),
    })
    return merged, []


def ingest_blob_as_document(
    source_blob,
    dossier_id: str,
    dossier_file_number: str,
    filename: str,
    metadata: dict,
    user_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Ingest an EXISTING GCS object as a document via a server-side copy.

    The bytes never transit the application — App Engine Standard caps any
    request AND response at 32 MB (both directions burned us; see the
    Known Gotchas): validation reads only the blob's metadata and a
    512-byte ranged probe, and the copy is a GCS rewrite. Serves the
    Réception versement (source in the quarantine bucket) and the
    direct-to-GCS upload form (source under staging/ in the canonical
    bucket). The CALLER must have reload()ed *source_blob* (its .size is
    what the size policy is enforced on) and owns the source's cleanup.
    """
    file_errors = _validate_file(filename, int(source_blob.size or 0))
    if file_errors:
        return None, file_errors
    ext = "." + filename.rsplit(".", 1)[1].lower()  # validated above

    try:
        header = source_blob.download_as_bytes(
            start=0, end=_SNIFF_PROBE_BYTES - 1
        )
    except Exception as exc:
        logger.warning(
            "ingest_blob: header read failed: %s", type(exc).__name__
        )
        return None, ["Lecture du fichier source impossible. Réessayez."]
    content_type = _sniff_header(header, ext)
    if not content_type or content_type not in ALLOWED_MIME_TYPES:
        return None, [
            "Le contenu du fichier ne correspond à aucun format autorisé. "
            "Formats acceptés : PDF, Word (DOC/DOCX), Excel (XLS/XLSX), "
            "JPG, PNG, TIFF, ZIP, courriels (EML/MSG)."
        ]
    if EXTENSION_MIME_TYPES.get(ext) != content_type:
        return None, ["Le contenu du fichier ne correspond pas à son extension."]

    merged, errors = _prepare_document_record(
        dossier_id, dossier_file_number, filename, ext,
        content_type, int(source_blob.size or 0), metadata, user_id,
    )
    if errors:
        return None, errors
    storage_path = merged["storage_path"]
    document_id = merged["id"]

    try:
        bucket = storage.bucket()
        dest = bucket.blob(storage_path)
        # GCS-side rewrite (loops for large objects). The destination then
        # gets the SNIFFED type — never the source's client-declared one —
        # and the attachment discipline of the non-previewable types.
        token, _, _ = dest.rewrite(source_blob)
        while token is not None:
            token, _, _ = dest.rewrite(source_blob, token=token)
        dest.content_type = content_type
        if content_type in _ATTACHMENT_ONLY_TYPES:
            dest.content_disposition = "attachment"
        dest.patch()
    except Exception as exc:
        logger.warning(
            "ingest_blob failed for document %s: %s",
            document_id, type(exc).__name__,
        )
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except Exception:
            pass
        return None, ["Erreur lors du versement. Veuillez réessayer."]

    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception as exc:
        logger.warning(
            "ingest_blob failed for document %s: %s",
            document_id, type(exc).__name__,
        )
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except Exception as cleanup_exc:
            logger.warning(
                "ingest_blob: storage rollback failed for document %s: %s",
                document_id, type(cleanup_exc).__name__,
            )
        return None, ["Erreur lors du versement. Veuillez réessayer."]

    return merged, []


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

    # Extension (already validated against ALLOWED_EXTENSIONS above)
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()

    # Detect MIME type from the file content (magic bytes) — never trust
    # the client-supplied filename for the stored/served content type.
    content_type = _sniff_content_type(file_stream, ext)
    if not content_type or content_type not in ALLOWED_MIME_TYPES:
        return None, [
            "Le contenu du fichier ne correspond à aucun format autorisé. "
            "Formats acceptés : PDF, Word (DOC/DOCX), Excel (XLS/XLSX), "
            "JPG, PNG, TIFF, ZIP, courriels (EML/MSG)."
        ]
    if EXTENSION_MIME_TYPES.get(ext) != content_type:
        return None, ["Le contenu du fichier ne correspond pas à son extension."]

    merged, errors = _prepare_document_record(
        dossier_id, dossier_file_number, filename, ext,
        content_type, file_size, metadata, user_id,
    )
    if errors:
        return None, errors
    storage_path = merged["storage_path"]
    document_id = merged["id"]

    # Upload to Firebase Storage
    # (Log only the document ID + exception type — the storage path and
    # filename embed client names and must not reach the logs.)
    try:
        bucket = storage.bucket()
        blob = bucket.blob(storage_path)
        if content_type in _ATTACHMENT_ONLY_TYPES:
            # Belt and braces: any signed URL WITHOUT a response-disposition
            # override still serves these as a download, never inline.
            blob.content_disposition = "attachment"
        blob.upload_from_file(file_stream, content_type=content_type)
    except Exception as exc:
        logger.warning("upload_document failed for document %s: %s", document_id, type(exc).__name__)
        return None, ["Erreur lors du téléversement. Veuillez réessayer."]

    # Save metadata to Firestore
    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception as exc:
        logger.warning("upload_document failed for document %s: %s", document_id, type(exc).__name__)
        # Attempt to clean up the uploaded file
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except Exception as cleanup_exc:
            logger.warning(
                "upload_document: storage rollback failed for document %s: %s",
                document_id,
                type(cleanup_exc).__name__,
            )
        return None, ["Erreur lors du téléversement. Veuillez réessayer."]

    return merged, []


def get_document(document_id: str) -> Optional[dict]:
    """Fetch a single document metadata by ID."""
    try:
        doc = db.collection(COLLECTION).document(document_id).get()
        if doc.exists:
            return _migrate_category(doc.to_dict())
    except Exception as exc:
        logger.warning("get_document failed for %s: %s", sanitize_log_value(document_id), exc)
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

        # Read-time category migration (display). The server-side category
        # filter above still matches the STORED key, so a legacy « entente »
        # doc surfaces under « autre » only after the one-shot script rewrites
        # it — acceptable (same as the dossier migration net).
        results = [_migrate_category(doc.to_dict()) for doc in query.stream()]

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
    allowed_fields = {
        "display_name", "category", "description", "tags", "document_date",
        "notes_internes",
    }
    sanitized = _sanitize_data({k: v for k, v in data.items() if k in allowed_fields})
    merged = {**existing, **sanitized}
    if "category" in sanitized:
        # Le juriste reprend la main. Sans cette ligne, une catégorie
        # corrigée À LA MAIN restait marquée « analyse », donc « présumée »
        # à l'écran et au connecteur — sur une valeur que le juriste venait
        # de poser lui-même. Le formulaire est une détermination, pas une
        # suggestion.
        merged["category_source"] = "juriste"
    if "document_date" in sanitized:
        # Presence-gated: a caller that does not carry the key never touches
        # the stored date; a carried empty string clears it deliberately.
        merged["document_date"] = _coerce_document_date(
            sanitized["document_date"]
        )

    # Validate
    if merged.get("category") and merged["category"] not in VALID_CATEGORIES:
        return None, ["Catégorie invalide."]

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception:
        log_unexpected("document write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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
        except NotFound:
            # Blob already gone — treat as deleted and proceed with the
            # Firestore delete so the metadata never becomes undeletable.
            logger.info(
                "delete_document: blob already missing for document %s",
                sanitize_log_value(document_id),
            )
        except Exception:
            log_unexpected("document file delete failed")
            return False, "Erreur lors de la suppression du fichier. Veuillez réessayer."

    # Delete from Firestore
    try:
        db.collection(COLLECTION).document(document_id).delete()
        return True, ""
    except Exception:
        log_unexpected("document delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


def build_attachment_disposition(filename: str) -> str:
    """RFC 6266 ``Content-Disposition`` value forcing a download of *filename*.

    A double quote would malform the quoted-string, so it is dropped; the
    plain filename= keeps an ASCII fallback and non-ASCII names travel in
    filename*=UTF-8''. Callers strip control characters first.
    """
    filename = (filename or "document").replace('"', "")
    ascii_name = (
        filename.encode("ascii", "ignore").decode("ascii").strip()
        or "document"
    )
    disposition = f'attachment; filename="{ascii_name}"'
    if filename != ascii_name:
        disposition += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return disposition


def sign_blob_url(blob, query_params: dict[str, str],
                  expiry_minutes: int = 15) -> str:
    """V4-sign a GET on *blob* — works on ANY bucket the runtime SA reads.

    On App Engine Standard, Application Default Credentials come from the
    metadata server and lack a local private key. Passing the service
    account email + access token tells the library to sign via the IAM
    signBlob API instead (requires iam.serviceAccountTokenCreator on
    itself — see CLAUDE.md, IAM requirements). Raises on failure — each
    caller owns its degradation.
    """
    signing_creds, _ = google.auth.default()
    signing_creds.refresh(auth_requests.Request())
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=expiry_minutes),
        method="GET",
        query_parameters=query_params,
        service_account_email=signing_creds.service_account_email,
        access_token=signing_creds.token,
    )


def get_document_bytes(
    document_id: str,
    *,
    max_bytes: int = DOCUMENT_TEXT_MAX_BYTES,
) -> tuple[Optional[bytes], str]:
    """Read a stored document's BYTES into memory, bounded — Phase N.

    Returns ``(data, "")`` on success or ``(None, reason)`` with a
    machine-stable reason: ``not_found`` | ``no_storage_path`` |
    ``too_large`` | ``download_failed``. The size gate runs on the
    Firestore ``file_size`` metadata BEFORE any byte is downloaded, and is
    re-checked on the actual byte count afterwards (stale metadata must not
    smuggle an oversized object past the gate). Mirrors
    ``doc_template.get_template_bytes`` — the only other whole-object read
    in the app — with the bound that module never needed (gabarits are
    ≤ 10 MB by upload policy; documents reach 200 MB).

    Callers turn ``reason`` into French; logs carry the exception TYPE only
    (a storage path embeds the dossier's file number).
    """
    doc = get_document(document_id)
    if not doc:
        return None, "not_found"
    storage_path = doc.get("storage_path", "")
    if not storage_path:
        return None, "no_storage_path"
    declared = int(doc.get("file_size") or 0)
    if declared > max_bytes:
        return None, "too_large"
    try:
        bucket = storage.bucket()
        data = bucket.blob(storage_path).download_as_bytes()
    except Exception as exc:
        logger.warning(
            "get_document_bytes failed for %s: %s",
            sanitize_log_value(document_id),
            type(exc).__name__,
        )
        return None, "download_failed"
    if len(data) > max_bytes:
        return None, "too_large"
    return data, ""


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
            # Ensure the filename has an extension so the OS recognises the
            # file type. _DOWNLOAD_EXTENSIONS first — mimetypes reads OS
            # registries and is platform-variant (".jpe" for JPEG, ".mht"
            # or None for rfc822/ms-outlook).
            if "." not in os.path.basename(filename):
                file_type = doc.get("file_type", "")
                ext = (
                    _DOWNLOAD_EXTENSIONS.get(file_type)
                    or mimetypes.guess_extension(file_type)
                    or ""
                )
                filename += ext
            query_params["response-content-disposition"] = (
                build_attachment_disposition(filename)
            )
            content_type = doc.get("file_type")
            if content_type:
                query_params["response-content-type"] = content_type

        return sign_blob_url(blob, query_params, expiry_minutes)
    except Exception:
        return None


# ── Archive ZIP d'un dossier de classement (décision 2026-08-13) ─────────
# L'archive est composée DANS GCS (flux, jamais entière en RAM — App Engine
# plafonne toute réponse à 32 Mo) puis remise par URL signé V4. ZIP_STORED :
# le corpus (PDF/images/ZIP/DOCX) ne se recompresse pas, et DEFLATE sur un
# cœur F2 transformerait une route I/O en route CPU — le risque SIGKILL du
# timeout gunicorn de 60 s.

# Les DEUX plafonds sont nécessaires (appliqués sur les métadonnées AVANT
# tout octet) : au plancher conservateur de ~20 Mo/s, 400 Mo ≈ 21 s ; et à
# ~100 ms d'initiation GET par fichier, 150 fichiers ≈ 15 s — le pire cas
# conjoint reste ≈ 38 s sous les 60 s. Le plafond d'octets seul ne protège
# pas : 4 000 petits fichiers = ~400 s d'initiations.
MAX_ZIP_TOTAL_BYTES = 400 * 1024 * 1024
MAX_ZIP_FILES = 150
_ZIP_CHUNK = 8 * 1024 * 1024   # multiple obligatoire de 256 Kio (BlobWriter)

# Caractères que l'extraction Windows refuse — un zip que l'Explorateur ne
# peut extraire dénature le dossier autant qu'un fichier manquant.
_WINDOWS_HOSTILES = '<>:"/\\|?*'
_DOS_RESERVES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class _ZipEntry(NamedTuple):
    arcname: str
    storage_path: str
    file_size: int
    created_at: object


def _zip_component(name: str) -> str:
    """Assainit UN segment de chemin d'archive (nom de dossier ou de
    fichier) pour une extraction Windows propre. Balayages linéaires,
    aucun regex (doctrine CWE-1333)."""
    propre = "".join(
        ch for ch in str(name or "") if ord(ch) >= 32 and ord(ch) != 127
    )
    propre = "".join(
        "-" if ch in _WINDOWS_HOSTILES else ch for ch in propre
    )
    sortie: list[str] = []
    for ch in propre:
        if ch.isspace():
            if sortie and sortie[-1] == " ":
                continue
            sortie.append(" ")
        elif ch == "-" and sortie and sortie[-1] == "-":
            continue
        else:
            sortie.append(ch)
    propre = "".join(sortie).strip().rstrip(". ")
    if propre and propre.split(".", 1)[0].upper() in _DOS_RESERVES:
        propre = "_" + propre
    if not propre:
        return "sans-titre"
    if len(propre) > 150:
        base, ext = os.path.splitext(propre)
        garde = ext if len(ext) <= 10 else ""
        propre = base[: 150 - len(garde)] + garde
    return propre


def _zip_entry_basename(doc: dict) -> str:
    """Nom de fichier d'une entrée d'archive, avec garantie d'extension.

    Précédent get_signed_url (display_name → original_filename → filename),
    resserré à dessein : le test n'est pas « un point quelque part » mais
    « se termine par une extension CONNUE du type » — « Pièce P-1.2 » doit
    devenir « Pièce P-1.2.pdf » (sur disque l'extension choisit l'ouvreur)
    sans doubler « photo.jpeg » en « photo.jpeg.jpg »."""
    file_type = doc.get("file_type", "")
    nom = _zip_component(
        doc.get("display_name") or doc.get("original_filename")
        or doc.get("filename") or "document"
    )
    connues = [e for e, m in EXTENSION_MIME_TYPES.items() if m == file_type]
    if not any(nom.lower().endswith(e) for e in connues):
        nom += (
            _DOWNLOAD_EXTENSIONS.get(file_type)
            or (connues[0] if connues else "")
            or (mimetypes.guess_extension(file_type) or "")
        )
    return nom


def _zip_dedupe(nom: str, used: set, is_dir: bool) -> str:
    """Suffixe « (2) » avant l'extension, en casse pliée (l'extraction
    Windows est insensible à la casse — et un fichier ne doit pas non plus
    entrer en collision avec un dossier frère)."""
    stem, ext = (nom, "") if is_dir else os.path.splitext(nom)
    candidat = nom
    n = 2
    while candidat.casefold() in used:
        candidat = f"{stem} ({n}){ext}"
        n += 1
    used.add(candidat.casefold())
    return candidat


def _zip_entry_dt(created_at) -> tuple:
    try:
        local = to_mtl(created_at)
        return (local.year, local.month, local.day,
                local.hour, local.minute, local.second)
    except Exception:
        return (1980, 1, 1, 0, 0, 0)


def _collect_zip_entries(
    tree: list, docs_by_folder: dict, folder_id: Optional[str],
) -> tuple[list, list]:
    """DFS du sous-arbre → (entrées de fichiers, répertoires).

    folder_id None ⇒ racine du dossier : enfants = racines de l'arbre,
    fichiers = racine (folder_id None) PLUS tout document dont le
    folder_id ne correspond à aucun nœud (référence pendante) — jamais
    silencieusement omis. Dédoublonnage par répertoire : les dossiers
    réclament leurs noms d'abord, puis les fichiers triés."""
    entries: list = []
    dirs: list = []

    def _find(nodes, fid):
        for n in nodes:
            if n.get("id") == fid:
                return n
            trouve = _find(n.get("children", []), fid)
            if trouve is not None:
                return trouve
        return None

    def _walk(prefix, children, docs_here):
        used: set = set()
        nommes = []
        for child in children:
            nom = _zip_dedupe(
                _zip_component(child.get("name") or ""), used, is_dir=True
            )
            nommes.append((nom, child))
        bases = sorted(
            ((_zip_entry_basename(d), d) for d in docs_here),
            key=lambda x: x[0].casefold(),
        )
        for base, d in bases:
            base = _zip_dedupe(base, used, is_dir=False)
            entries.append(_ZipEntry(
                prefix + base,
                d.get("storage_path", ""),
                int(d.get("file_size") or 0),
                d.get("created_at"),
            ))
        for nom, child in nommes:
            arc = prefix + nom
            dirs.append(arc)
            _walk(arc + "/", child.get("children", []),
                  docs_by_folder.get(child.get("id"), []))

    if folder_id:
        racine = _find(tree, folder_id)
        if racine is None:
            return [], []
        _walk("", racine.get("children", []),
              docs_by_folder.get(folder_id, []))
    else:
        connus: set = set()

        def _ids(nodes):
            for n in nodes:
                connus.add(n.get("id"))
                _ids(n.get("children", []))

        _ids(tree)
        racine_docs = list(docs_by_folder.get(None, []))
        for fid, ds in docs_by_folder.items():
            if fid is not None and fid not in connus:
                racine_docs.extend(ds)
        _walk("", tree, racine_docs)
    return entries, dirs


def build_folder_zip_url(
    dossier_id: str,
    folder_id: Optional[str],
    user_id: str,
    expiry_minutes: int = 15,
) -> tuple[Optional[str], list[str]]:
    """Compose le zip du sous-arbre dans GCS et retourne (url signé, erreurs).

    Politique d'échec TOUT-OU-RIEN : un blob introuvable annule l'archive —
    une archive silencieusement incomplète dénaturerait le dossier. L'abandon
    est propre par construction : BlobWriter.__exit__ TERMINE la session
    recomposable sur exception, donc aucun objet partiel n'existe jamais ;
    le seul résidu possible est un zip COMPLET dont la signature a échoué,
    que la règle de cycle de vie staging/ 7 j balaie."""
    from models.dossier import get_dossier
    from models.folder import get_folder, get_folder_tree

    dossier = get_dossier(dossier_id)
    if not dossier:
        return None, ["Dossier introuvable."]
    if folder_id:
        folder = get_folder(dossier_id, folder_id)
        if not folder:
            return None, ["Dossier de documents introuvable."]
        root_name = folder.get("name") or "Documents"
    else:
        root_name = "Documents"

    tree = get_folder_tree(dossier_id)
    docs = list_documents(dossier_id=dossier_id)
    docs_by_folder: dict = {}
    for d in docs:
        docs_by_folder.setdefault(d.get("folder_id"), []).append(d)

    entries, dirs = _collect_zip_entries(tree, docs_by_folder, folder_id)

    if not entries:
        return None, ["Ce dossier ne contient aucun document."]
    if len(entries) > MAX_ZIP_FILES:
        return None, [
            f"Ce dossier contient plus de {MAX_ZIP_FILES} documents. "
            "Téléchargez par sous-dossier."
        ]
    total = sum(e.file_size for e in entries)
    if total > MAX_ZIP_TOTAL_BYTES:
        return None, [
            "L'archive dépasserait la limite de 400 Mo (contenu : "
            f"{format_file_size(total)}). Téléchargez les documents "
            "individuellement ou par sous-dossier."
        ]

    zip_name = _zip_component(
        f"{dossier.get('file_number', '')} - {root_name}".strip(" -")
    ) + ".zip"
    zip_path = f"staging/{user_id}/exports/{uuid.uuid4()}/{zip_name}"

    try:
        bucket = storage.bucket()
        zip_blob = bucket.blob(zip_path)
        # Portée par l'initiation de la session recomposable.
        zip_blob.content_disposition = "attachment"
        # ignore_flush OBLIGATOIRE : zipfile appelle flush() sur son puits
        # et BlobWriter.flush() lève sans lui ; chunk_size explicite — le
        # tampon par défaut du writer est 40 Mio.
        with zip_blob.open(
            "wb", chunk_size=_ZIP_CHUNK, ignore_flush=True,
            content_type="application/zip",
        ) as sink:
            with zipfile.ZipFile(
                sink, "w", zipfile.ZIP_STORED, allowZip64=True
            ) as zf:
                for arcname in dirs:
                    zf.mkdir(arcname)
                for e in entries:
                    zi = zipfile.ZipInfo(
                        e.arcname, date_time=_zip_entry_dt(e.created_at)
                    )
                    zi.compress_type = zipfile.ZIP_STORED
                    with zf.open(zi, "w") as sortie:
                        # UN GET en flux par document (blob sans chunk_size
                        # = téléchargement streamé d'une seule requête).
                        bucket.blob(e.storage_path).download_to_file(sortie)
    except NotFound:
        return None, [
            "Un document de ce dossier est introuvable dans le stockage. "
            "Archive annulée — aucun fichier partiel n'a été conservé."
        ]
    except Exception:
        log_unexpected("folder zip failed")
        return None, [
            "Erreur lors de la préparation de l'archive. Veuillez réessayer."
        ]

    try:
        return sign_blob_url(zip_blob, {
            "response-content-disposition": build_attachment_disposition(zip_name),
            "response-content-type": "application/zip",
        }, expiry_minutes), []
    except Exception:
        log_unexpected("folder zip signing failed")
        return None, [
            "Erreur lors de la préparation de l'archive. Veuillez réessayer."
        ]


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
    except Exception:
        log_unexpected("document move failed")
        return None, ["Erreur lors du déplacement. Veuillez réessayer."]

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
        except Exception:
            log_unexpected("document bulk move failed")
            return 0, ["Erreur lors du déplacement. Veuillez réessayer."]

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


# ── Analyse documentaire (SPEC Phase K, §8) ──────────────────────────────
#
# La couche PURE existe déjà et fait foi : `utils/analyse_taxonomies` (le
# vocabulaire fermé) et `utils/analyse_protection` (les dérivations, dont la
# règle §6.3 d'échec vers le haut). Ce module ne DÉCIDE rien — il compose ces
# deux-là, écrit le cache et appose au journal.
#
# ⚠ ÉCART ASSUMÉ avec la §5.3, décision du praticien du 2026-08-27 : l'analyse
# écrit DIRECTEMENT dans `category`, là où la spec l'interdisait absolument et
# réservait un geste « adopter la nature ». Trois choses rendent l'écart
# tenable, et les retirer le rouvre :
#
#   1. La catégorie n'est JAMAIS choisie par le modèle. Il fournit une
#      `sous_nature` — un code d'une table fermée — et `nature_of()` en dérive
#      la catégorie. Une catégorie inventée est structurellement impossible.
#   2. `category_source` porte la provenance, et c'est elle qui fait paraître
#      la mention « présumé » à l'écran et dans la sortie MCP — les exigences
#      2 et 3 de la §7, servies sans le second clic.
#   3. Le journal garde `categorie_precedente` ET sa source. Écraser détruit
#      la comparaison à deux valeurs dont vivait `divergence_categorie`; la
#      garder au journal est ce qui laisse la divergence connaissable.
#
# Le journal `documents/{id}/analyses/{analyseId}` est WRITE-ONCE. Aucun verbe
# ne le modifie ni ne l'efface — c'est la doctrine « aucune suppression », et
# c'est aussi ce qui rend la règle de non-déclassement APPLICABLE : sans
# historique, on ne peut pas constater qu'un niveau a baissé.

ANALYSES_SUBCOLLECTION = "analyses"

VALID_CATEGORY_SOURCES = ("juriste", "analyse")
VALID_ANALYSE_STATUTS = (
    "en_attente", "en_cours", "prete", "echec", "non_applicable",
)

# Les champs d'extraction que le modèle fournit, et EUX SEULS. Une liste
# blanche, jamais `**args` : `record_analyse` écrit un document complet.
MOTIF_NON_DECLASSEMENT = (
    "Niveau tenu : une analyse antérieure retenait une protection plus "
    "élevée, et une réanalyse ne déclasse jamais."
)

_EXTRACTION_FIELDS = (
    "resume", "langue_detectee", "qualite_reconnaissance",
    "extraction_tronquee", "numero_dossier_cour", "tribunal",
    "district_judiciaire", "auteur", "parties_mentionnees",
    "date_signature_str", "date_document_str", "contient_dispositif",
    "dispositif", "moyen_preuve", "qualification_ecrit", "parait_original",
    "indices_protection", "confiance",
)


def _analyse_derivee(
    sortie: dict, *, document: dict, dossier: Optional[dict] = None
) -> tuple[dict, list[str]]:
    """Le champ `analyse` complet — tout ce qui se dérive, dérivé.

    Pur : aucune écriture, aucune lecture Firestore. C'est ce qui le rend
    testable sans harnais, et c'est ici que vit la garantie que le modèle ne
    choisit jamais une catégorie.
    """
    from utils import analyse_protection as prot
    from utils import analyse_taxonomies as tax

    sous_nature = str(sortie.get("sous_nature") or "").strip()
    if sous_nature not in tax.VALID_SOUS_NATURES:
        return {}, [f"Sous-nature inconnue : {sous_nature or '(vide)'}."]

    # LA garantie : la nature est DÉRIVÉE du code, jamais reçue.
    nature = tax.nature_of(sous_nature)
    erreurs = tax.validate_pair(nature, sous_nature)
    if erreurs:
        return {}, erreurs

    privileges = tuple(
        str(p).strip() for p in (sortie.get("privileges") or []) if str(p).strip()
    )
    inconnus = [p for p in privileges if p not in tax.VALID_PRIVILEGES]
    if inconnus:
        return {}, [f"Privilège inconnu : {', '.join(sorted(inconnus))}."]

    erreurs_preuve = tax.validate_preuve(
        str(sortie.get("moyen_preuve") or ""),
        str(sortie.get("qualification_ecrit") or ""),
    )
    if erreurs_preuve:
        return {}, erreurs_preuve

    extrait = {k: sortie.get(k) for k in _EXTRACTION_FIELDS if k in sortie}
    contient_dispositif = extrait.get("contient_dispositif")
    absents = prot.champs_attendus_absents(
        sous_nature, extrait, contient_dispositif=contient_dispositif
    )
    retenus, niveau, motifs = prot.appliquer_regime(
        nature=nature,
        sous_nature=sous_nature,
        privileges=privileges,
        champs_absents=absents,
        domaine_dossier=str((dossier or {}).get("domaine") or ""),
        numero_dossier_extrait=str(extrait.get("numero_dossier_cour") or ""),
        numero_dossier_du_dossier=str(
            (dossier or {}).get("court_file_number") or ""
        ),
    )

    # ── La règle de NON-DÉCLASSEMENT (§6.3 règle 2) ────────────────────
    #
    # Elle était PROMISE et pas implémentée. `appliquer_regime` ne monte
    # qu'à l'intérieur d'UN appel : elle ne reçoit rien de l'analyse
    # antérieure, si bien qu'une réanalyse retenant moins de privilèges
    # faisait tomber le niveau — mesuré le 2026-08-27, 3 → 1, en silence,
    # sur un document couvert par le secret professionnel. Pendant ce
    # temps la description de l'outil MCP, la compétence et CLAUDE.md
    # affirmaient toutes trois que le code gardait le plus élevé.
    #
    # Le sens de l'erreur décide : sous-estimer la protection peut mener à
    # une divulgation par inadvertance (art. 60.4 du Code des professions),
    # la surestimer fait perdre du temps. Un chemin AUTOMATIQUE ne descend
    # donc jamais — il retient l'UNION des privilèges et le plus haut des
    # deux niveaux, et lève `divergence_protection` pour que l'écart soit
    # vu plutôt que subi.
    #
    # ⚠ La voie de descente existe, et c'est le JURISTE : `update_analyse`
    # écrit ce qu'il pose, y compris plus bas. Sans elle la règle serait
    # collante et une sur-protection fautive deviendrait définitive.
    precedent = document.get("analyse") or {}
    niveau_avant = precedent.get("niveau_protection")
    niveau_analyse = niveau          # ce que CE passage a conclu, avant plancher
    divergence = False
    if isinstance(niveau_avant, int) and (niveau is None or niveau < niveau_avant):
        divergence = True
        anciens = {
            c for c in (precedent.get("privileges") or []) if c in prot.PRIVILEGES
        }
        # L'union, pas seulement le niveau : garder un niveau 3 sans le
        # privilège qui le fonde produirait une carte incohérente, où la
        # protection ne s'expliquerait par rien.
        retenus = tuple(sorted(set(retenus) | anciens))
        niveau = max(niveau_avant, niveau or 0)
        motifs = tuple(motifs) + (MOTIF_NON_DECLASSEMENT,)

    champ: dict = {
        "statut": "prete",
        "nature_detectee": nature,
        "sous_nature": sous_nature,
        "famille": tax.famille_of(sous_nature),
        "privileges": list(retenus),
        "niveau_protection": niveau,
        # Ce que l'analyse a conclu AVANT le plancher — sans quoi la
        # divergence serait invérifiable : on verrait un niveau tenu sans
        # savoir de quoi il a été tenu.
        "niveau_protection_analyse": niveau_analyse,
        "niveau_protection_precedent": niveau_avant,
        "divergence_protection": divergence,
        "motifs_protection": list(motifs),
        "champs_attendus_absents": list(absents),
        "alerte_dispositif_detecte": prot.alerte_dispositif_detecte(
            sous_nature, contient_dispositif
        ),
        "alerte_renonciation_possible": prot.alerte_renonciation_possible(
            nature, retenus
        ),
        # §7 — jamais vrai par un chemin automatique. Seul
        # `confirmer_analyse` le lève.
        "confirme": False,
        "confirme_par": None,
        "confirme_le": None,
    }
    champ.update(_sanitize_data(extrait))
    return champ, []


def record_analyse(
    document_id: str,
    sortie: dict,
    *,
    declenche_par: str = "chat",
    modele: str = "",
    dossier: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Enregistre une analyse : le cache, la catégorie dérivée, le journal.

    Rend le document mis à jour. N'ÉCRIT JAMAIS `confirme: true` — voir §7.
    Aucun `bump_ctag` : `documents` n'est pas exposée en DAV.
    """
    existing = get_document(document_id)
    if not existing:
        return None, ["Document introuvable."]

    champ, erreurs = _analyse_derivee(sortie, document=existing, dossier=dossier)
    if erreurs:
        return None, erreurs

    ancienne = str(existing.get("category") or "")
    ancienne_source = str(existing.get("category_source") or "juriste")
    nouvelle = champ["nature_detectee"]

    now = datetime.now(timezone.utc)
    analyse_id = str(uuid.uuid4())
    champ.update({
        "declenche_par": str(declenche_par or "")[:40],
        "modele": sanitize(str(modele or ""), max_length=120),
        "genere_le": now,
        "analyse_id": analyse_id,
        "message_erreur": None,
        # Ce que l'écrasement remplace. Sans cela la divergence de classement
        # — « l'un des deux signalements qui valent le plus » — deviendrait
        # inobservable, puisqu'il n'y a plus deux valeurs à comparer.
        "description_precedente": existing.get("description") or "",
        "date_document_precedente": existing.get("document_date"),
        "categorie_precedente": ancienne,
        "categorie_precedente_source": ancienne_source,
        "categorie_remplacee": bool(ancienne and ancienne != nouvelle),
        # L'avertissement n'est levé que si l'on écrase un choix HUMAIN. Un
        # « autre » posé par défaut au versement n'en mérite pas.
        "remplace_un_choix_du_juriste": bool(
            ancienne and ancienne != nouvelle and ancienne_source == "juriste"
        ),
    })

    # L'analyse ALIMENTE les champs natifs du document — c'est ce qui la
    # rend utile hors de sa propre carte : le résumé devient la
    # description, la date lue devient la date du document, et le
    # formulaire d'édition les montre et les laisse corriger.
    #
    # ⚠ Elle les ÉCRASE (décision du praticien, 2026-08-27, renversant le
    # remplir-si-vide de la veille). C'est tenable parce que `description`
    # cesse d'être partagée : le juriste écrit dans `notes_internes`, que
    # rien ne réécrit jamais. Sans ce second champ, écrire une note et
    # relancer une analyse s'excluaient. La valeur remplacée part au
    # journal, comme celle de la catégorie — rien ne disparaît sans trace.
    natifs: dict = {}
    if champ.get("resume"):
        natifs["description"] = champ["resume"]
    if champ.get("date_document_str"):
        lue = _coerce_document_date(champ["date_document_str"])
        if lue is not None:
            natifs["document_date"] = lue

    merged = {**existing, "analyse": champ, "category": nouvelle,
              "category_source": "analyse", "updated_at": now,
              "etag": str(uuid.uuid4()), **natifs}
    try:
        ref = db.collection(COLLECTION).document(document_id)
        # Le journal AVANT le cache : une entrée orpheline est inerte, un
        # cache sans entrée est une analyse dont on ne saura jamais l'origine.
        ref.collection(ANALYSES_SUBCOLLECTION).document(analyse_id).set(champ)
        ref.set(merged)
    except Exception:
        log_unexpected("document analyse write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


# Tout ce que l'analyse produit et que le juriste peut reprendre. La liste
# est EXHAUSTIVE par décision (2026-08-27) : « whatever the analysis outputs
# becomes an editable field ». Les trois premiers sont DÉRIVANTS — les
# changer recalcule ce qui en dépend ; les autres sont l'extrait tel quel.
_ANALYSE_DERIVANTS = ("sous_nature", "privileges", "niveau_protection")
_ANALYSE_LISTES = (
    "parties_mentionnees", "indices_protection", "motifs_protection",
    "champs_attendus_absents",
)
_ANALYSE_BOOLEENS = (
    "contient_dispositif", "extraction_tronquee", "parait_original",
    "alerte_dispositif_detecte", "alerte_renonciation_possible",
)
_ANALYSE_TEXTES = (
    "resume", "numero_dossier_cour", "tribunal", "district_judiciaire",
    "auteur", "date_document_str", "date_signature_str", "dispositif",
    "langue_detectee", "confiance", "qualite_reconnaissance", "moyen_preuve",
    "qualification_ecrit",
)
ANALYSE_EDITABLE = (
    _ANALYSE_DERIVANTS + _ANALYSE_LISTES + _ANALYSE_BOOLEENS + _ANALYSE_TEXTES
)
VALID_NIVEAUX = (0, 1, 2, 3)


def update_analyse(
    document_id: str, champs: dict, *, par: str
) -> tuple[Optional[dict], list[str]]:
    """Le juriste corrige l'analyse — et c'est la SEULE voie de déclassement.

    Deux raisons de ne pas passer par `record_analyse` :

    * Celui-là DÉRIVE tout d'une sortie de modèle et applique le plancher de
      non-déclassement. Ici, la valeur posée est celle de l'avocat : elle
      peut descendre, parce que c'est sa détermination et non une
      supposition. Sans cette porte, la règle de non-déclassement serait
      collante et une sur-protection fautive deviendrait définitive.
    * Éditer, c'est CONFIRMER. Le juriste qui corrige un champ a vu la
      carte ; lui redemander un second clic sur « Confirmer » serait lui
      faire dire deux fois la même chose. `confirmer_analyse` reste la voie
      de celui qui accepte SANS corriger.

    Contrat de présence, comme partout dans ce modèle : une clé absente
    laisse la valeur stockée intacte, une clé présente et vide l'efface.
    Les vocabulaires restent FERMÉS — le juriste choisit dans la table, il
    n'invente pas plus de code que le modèle.

    L'entrée au journal porte ``declenche_par: "juriste"``, si bien que
    l'historique distingue ce que le modèle a proposé de ce que l'avocat a
    arrêté. Rien ne s'efface, ici comme ailleurs.
    """
    from utils import analyse_protection as prot
    from utils import analyse_taxonomies as tax

    existing = get_document(document_id)
    if not existing:
        return None, ["Document introuvable."]
    champ = dict(existing.get("analyse") or {})
    if not champ.get("sous_nature") and "sous_nature" not in champs:
        return None, ["Aucune analyse à corriger."]

    erreurs: list[str] = []
    propose = {k: v for k, v in champs.items() if k in ANALYSE_EDITABLE}

    if "sous_nature" in propose:
        code = str(propose["sous_nature"] or "").strip()
        if code not in tax.VALID_SOUS_NATURES:
            erreurs.append(f"Sous-nature inconnue : {code}.")
        else:
            propose["sous_nature"] = code

    if "privileges" in propose:
        brut = propose["privileges"] or []
        if isinstance(brut, str):
            brut = [p.strip() for p in brut.split(",") if p.strip()]
        inconnus = sorted({p for p in brut if p not in prot.PRIVILEGES})
        if inconnus:
            erreurs.append(f"Privilège inconnu : {', '.join(inconnus)}.")
        else:
            propose["privileges"] = sorted(set(brut))

    if "niveau_protection" in propose:
        brut = propose["niveau_protection"]
        if brut in ("", None):
            propose["niveau_protection"] = None
        else:
            try:
                niveau = int(brut)
            except (TypeError, ValueError):
                niveau = -1
            if niveau not in VALID_NIVEAUX:
                erreurs.append(
                    "Niveau de protection invalide : attendu 0, 1, 2 ou 3."
                )
            else:
                propose["niveau_protection"] = niveau

    for cle in _ANALYSE_LISTES:
        if cle in propose:
            brut = propose[cle] or []
            if isinstance(brut, str):
                brut = [x.strip() for x in brut.split(",") if x.strip()]
            propose[cle] = [
                sanitize(str(x), max_length=300) for x in brut if str(x).strip()
            ]

    for cle in _ANALYSE_BOOLEENS:
        if cle in propose:
            propose[cle] = bool(propose[cle])

    for cle in _ANALYSE_TEXTES:
        if cle in propose:
            propose[cle] = sanitize(str(propose[cle] or ""), max_length=2000)

    # Annexe C : les deux axes se valident ENSEMBLE, et sur la valeur qui
    # sera STOCKÉE — un axe corrigé seul doit rester cohérent avec l'autre
    # tel qu'il est déjà en place.
    erreurs += tax.validate_preuve(
        str(propose.get("moyen_preuve", champ.get("moyen_preuve") or "")),
        str(propose.get(
            "qualification_ecrit", champ.get("qualification_ecrit") or ""
        )),
    )
    for cle, valides in (
        ("qualite_reconnaissance", set(tax.QUALITES_RECONNAISSANCE)),
    ):
        v = str(propose.get(cle) or "")
        if v and v not in valides:
            erreurs.append(f"Valeur invalide pour {cle} : {v}.")

    if erreurs:
        return None, erreurs

    champ.update(propose)

    # La nature et la famille ne sont jamais saisies : elles DÉRIVENT du
    # code, exactement comme sur le chemin du modèle. C'est ce qui rend
    # impossible d'inventer une catégorie, à la main comme autrement.
    sous_nature = str(champ.get("sous_nature") or "")
    nature = tax.nature_of(sous_nature)
    champ["nature_detectee"] = nature
    champ["famille"] = tax.famille_of(sous_nature)

    now = datetime.now(timezone.utc)
    analyse_id = str(uuid.uuid4())
    ancienne = str(existing.get("category") or "")
    champ.update({
        "declenche_par": "juriste",
        "modifie_par": sanitize(str(par or ""), max_length=200),
        "modele": "",
        "genere_le": now,
        "analyse_id": analyse_id,
        "message_erreur": None,
        # Le juriste a tranché : la divergence n'est plus en attente, et le
        # niveau qu'il pose devient le plancher des analyses suivantes.
        "divergence_protection": False,
        "niveau_protection_analyse": champ.get("niveau_protection"),
        "niveau_protection_precedent": (existing.get("analyse") or {}).get(
            "niveau_protection"
        ),
        "categorie_precedente": ancienne,
        "categorie_precedente_source": str(
            existing.get("category_source") or "juriste"
        ),
        "categorie_remplacee": bool(ancienne and ancienne != nature),
        # Jamais un avertissement contre le juriste lui-même.
        "remplace_un_choix_du_juriste": False,
        # Éditer, c'est confirmer.
        "confirme": True,
        "confirme_par": sanitize(str(par or ""), max_length=200),
        "confirme_le": now,
    })

    merged = {
        **existing, "analyse": champ, "category": nature,
        "category_source": "juriste", "updated_at": now,
        "etag": str(uuid.uuid4()),
    }
    try:
        ref = db.collection(COLLECTION).document(document_id)
        ref.collection(ANALYSES_SUBCOLLECTION).document(analyse_id).set(champ)
        ref.set(merged)
    except Exception:
        log_unexpected("document analyse edit failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def confirmer_analyse(
    document_id: str, par: str
) -> tuple[Optional[dict], list[str]]:
    """Le SEUL chemin passant `confirme` à vrai (§7). Aucun automatisme."""
    existing = get_document(document_id)
    if not existing:
        return None, ["Document introuvable."]
    champ = dict(existing.get("analyse") or {})
    if not champ.get("sous_nature"):
        return None, ["Aucune analyse à confirmer."]
    now = datetime.now(timezone.utc)
    champ.update({"confirme": True,
                  "confirme_par": sanitize(str(par or ""), max_length=200),
                  "confirme_le": now})
    merged = {**existing, "analyse": champ,
              # La confirmation fait de la catégorie une détermination de
              # l'avocat : la mention « présumé » doit tomber avec elle.
              "category_source": "juriste",
              "updated_at": now, "etag": str(uuid.uuid4())}
    try:
        db.collection(COLLECTION).document(document_id).set(merged)
    except Exception:
        log_unexpected("document analyse confirm failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def list_analyses(document_id: str, limit: int = 20) -> list[dict]:
    """Le journal, du plus récent au plus ancien. Échoue OUVERT — c'est un
    affichage d'historique, jamais une garde."""
    try:
        snaps = (
            db.collection(COLLECTION).document(document_id)
            .collection(ANALYSES_SUBCOLLECTION)
            .order_by("genere_le", direction="DESCENDING")
            .limit(max(1, min(int(limit or 20), 100))).stream()
        )
        return [s.to_dict() for s in snaps]
    except Exception:
        log_unexpected("document analyses read failed")
        return []
