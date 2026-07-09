"""Document templates ("gabarits") — Firestore + Storage persistence (Phase H).

Standard CRUD per CLAUDE.md (``_sanitize_data`` → ``_validate``; no
``_normalize``). Not DAV-exposed — no DAV UID, no CTag bumping. Template
files live in Firebase Storage at
``users/{userId}/templates/{templateId}/{filename}`` and are NOT
``documents`` records; generated outputs are independent copies saved via
``models/document.upload_document``.

Placeholder extraction/classification happens at upload and file
replacement (utils/docx_fill + utils/template_fields) so the template doc
always carries its current field inventory.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import google.auth
from google.auth.transport import requests as auth_requests
from firebase_admin import storage
from google.cloud.exceptions import NotFound
from werkzeug.utils import secure_filename

from models import db
from security import sanitize
from utils.docx_fill import validate_template
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils.template_fields import classify_placeholders

logger = logging.getLogger(__name__)

COLLECTION = "doc_templates"

VALID_CATEGORIES = ("procédure", "correspondance", "autre")
CATEGORY_LABELS = {
    "procédure": "Procédure",
    "correspondance": "Correspondance",
    "autre": "Autre",
}

MAX_TEMPLATE_SIZE = 10 * 1024 * 1024  # compressed .docx cap (also in docx_fill)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_SPLIT_RUN_WARNING = (
    "Le champ «{name}» semble fragmenté par Word et ne sera pas rempli. "
    "Retapez le champ d'un seul trait dans Word, sans pause ni correction "
    "automatique, puis téléversez le fichier à nouveau."
)


def _default_doc() -> dict:
    return {
        "id": "",
        "name": "",
        "description": "",
        "category": "autre",
        "filename": "",
        "original_filename": "",
        "file_size": 0,
        "storage_path": "",
        "version": 1,
        "placeholders": [],
        "auto_fields": [],
        "manual_fields": [],
        "block_fields": [],
        "slots_required": [],
        "validation_warnings": [],
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    cleaned: dict = {}
    for key, value in data.items():
        if isinstance(value, str):
            cleaned[key] = sanitize(value, max_length=2000)
        else:
            cleaned[key] = value
    return cleaned


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    name = (data.get("name") or "").strip()
    if not name:
        errors.append("Le nom du gabarit est requis.")
    elif len(name) > 120:
        errors.append("Le nom du gabarit ne peut dépasser 120 caractères.")
    category = data.get("category") or ""
    if category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")
    return errors


def _validate_file(filename: str, file_size: int) -> list[str]:
    errors: list[str] = []
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()
    if ext != ".docx":
        errors.append("Seuls les fichiers .docx sont acceptés comme gabarits.")
    if file_size > MAX_TEMPLATE_SIZE:
        errors.append("Le fichier dépasse la taille maximale de 10 Mo.")
    if file_size == 0:
        errors.append("Le fichier est vide.")
    return errors


def _safe_filename(filename: str) -> str:
    printable = "".join(ch for ch in filename if ch.isprintable())
    safe = secure_filename(printable)
    if len(safe) > 200:
        safe = safe[: 200 - len(".docx")] + ".docx"
    if not safe or not safe.lower().endswith(".docx"):
        safe = "gabarit.docx"
    return safe


def _extraction_fields(docx_bytes: bytes) -> tuple[Optional[dict], list[str]]:
    """Validate the archive and build the extracted-inventory fields.

    Returns ``(fields, errors)`` — structural errors refuse the upload;
    split-run suspects become warnings and the upload proceeds.
    """
    validation = validate_template(docx_bytes)
    if validation.errors:
        return None, validation.errors

    classification = classify_placeholders(validation.placeholders)
    manual_set = set(classification.manual_scalar) | set(classification.unknown)
    fields = {
        "placeholders": validation.placeholders,
        "auto_fields": [
            n for n in validation.placeholders if n in classification.auto
        ],
        "manual_fields": [
            n for n in validation.placeholders if n in manual_set
        ],
        "block_fields": [
            n for n in validation.placeholders if n in classification.blocks
        ],
        "slots_required": sorted(classification.slots_required),
        "validation_warnings": [
            _SPLIT_RUN_WARNING.format(name=n)
            for n in validation.split_run_suspects
        ],
    }
    return fields, []


# ── CRUD ────────────────────────────────────────────────────────────────

def create_template(
    file_stream,
    filename: str,
    file_size: int,
    metadata: dict,
    user_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Validate, extract placeholders, upload to Storage, persist the doc."""
    file_errors = _validate_file(filename, file_size)
    if file_errors:
        return None, file_errors

    try:
        docx_bytes = file_stream.read()
    except Exception as exc:
        logger.warning("create_template: stream read failed: %s", type(exc).__name__)
        return None, ["Le fichier n'a pas pu être lu. Veuillez réessayer."]

    extraction, errors = _extraction_fields(docx_bytes)
    if errors:
        return None, errors

    merged = {**_default_doc(), **_sanitize_data(metadata)}
    meta_errors = _validate(merged)
    if meta_errors:
        return None, meta_errors

    now = datetime.now(timezone.utc)
    template_id = str(uuid.uuid4())
    safe_filename = _safe_filename(filename)
    storage_path = f"users/{user_id}/templates/{template_id}/{safe_filename}"

    merged.update(extraction)
    merged.update(
        {
            "id": template_id,
            "filename": safe_filename,
            "original_filename": filename,
            "file_size": len(docx_bytes),
            "storage_path": storage_path,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
    )

    # Upload to Firebase Storage (never log the path — it may embed names).
    try:
        bucket = storage.bucket()
        bucket.blob(storage_path).upload_from_string(
            docx_bytes, content_type=DOCX_MIME
        )
    except Exception as exc:
        logger.warning(
            "create_template failed for template %s: %s",
            template_id, type(exc).__name__,
        )
        return None, ["Erreur lors du téléversement. Veuillez réessayer."]

    try:
        db.collection(COLLECTION).document(template_id).set(merged)
    except Exception as exc:
        logger.warning(
            "create_template failed for template %s: %s",
            template_id, type(exc).__name__,
        )
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except Exception as cleanup_exc:
            logger.warning(
                "create_template: storage rollback failed for template %s: %s",
                template_id, type(cleanup_exc).__name__,
            )
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def get_template(template_id: str) -> Optional[dict]:
    try:
        doc = db.collection(COLLECTION).document(template_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        logger.warning(
            "get_template failed for %s: %s",
            sanitize_log_value(template_id), type(exc).__name__,
        )
        return None


def list_templates(
    category: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """All templates ordered by name; small bounded collection (tens of
    docs) — category/search filtering happens client-side, no index."""
    try:
        query = db.collection(COLLECTION).order_by("name")
        results = [doc.to_dict() for doc in query.stream()]
    except Exception as exc:
        logger.warning("list_templates failed: %s", type(exc).__name__)
        return []

    if category and category in VALID_CATEGORIES:
        results = [t for t in results if t.get("category") == category]
    if search:
        term = search.lower()
        results = [
            t
            for t in results
            if term
            in " ".join([t.get("name", ""), t.get("description", "")]).lower()
        ]
    return results


def update_template(
    template_id: str,
    data: dict,
    file_stream=None,
    filename: Optional[str] = None,
    file_size: Optional[int] = None,
) -> tuple[Optional[dict], list[str]]:
    """Update metadata; optionally replace the file (re-validate,
    re-extract placeholders, new Storage object, version += 1)."""
    existing = get_template(template_id)
    if not existing:
        return None, ["Gabarit introuvable."]

    merged = {**existing, **_sanitize_data(data)}
    meta_errors = _validate(merged)
    if meta_errors:
        return None, meta_errors

    old_storage_path = existing.get("storage_path", "")
    new_storage_path = None

    if file_stream is not None and filename:
        file_errors = _validate_file(filename, file_size or 0)
        if file_errors:
            return None, file_errors
        try:
            docx_bytes = file_stream.read()
        except Exception as exc:
            logger.warning(
                "update_template: stream read failed: %s", type(exc).__name__
            )
            return None, ["Le fichier n'a pas pu être lu. Veuillez réessayer."]

        extraction, errors = _extraction_fields(docx_bytes)
        if errors:
            return None, errors

        # The user id segment is fixed at creation — reuse it from the
        # existing path (users/{uid}/templates/...).
        parts = old_storage_path.split("/")
        user_segment = parts[1] if len(parts) > 3 else "unknown"
        safe_filename = _safe_filename(filename)
        new_storage_path = (
            f"users/{user_segment}/templates/{template_id}/{safe_filename}"
        )

        # Same sanitized filename → same path → the upload overwrites the
        # previous object in place. Snapshot the old bytes first so a
        # Firestore failure below can restore them (rollback parity with
        # create_template).
        overwrite_backup: Optional[bytes] = None
        if new_storage_path == old_storage_path:
            try:
                bucket = storage.bucket()
                overwrite_backup = bucket.blob(old_storage_path).download_as_bytes()
            except NotFound:
                # Old object already gone — nothing to protect.
                overwrite_backup = None
            except Exception as exc:
                # Fail CLOSED: without a restore point, an in-place
                # overwrite followed by a Firestore failure would destroy
                # the previous template irrecoverably.
                logger.warning(
                    "update_template: pre-overwrite backup failed for %s: %s",
                    sanitize_log_value(template_id), type(exc).__name__,
                )
                return None, [
                    "Le fichier existant n'a pas pu être sauvegardé avant "
                    "remplacement. Veuillez réessayer."
                ]

        try:
            bucket = storage.bucket()
            bucket.blob(new_storage_path).upload_from_string(
                docx_bytes, content_type=DOCX_MIME
            )
        except Exception as exc:
            logger.warning(
                "update_template failed for template %s: %s",
                sanitize_log_value(template_id), type(exc).__name__,
            )
            return None, ["Erreur lors du téléversement. Veuillez réessayer."]

        merged.update(extraction)
        merged.update(
            {
                "filename": safe_filename,
                "original_filename": filename,
                "file_size": len(docx_bytes),
                "storage_path": new_storage_path,
                "version": int(existing.get("version", 1)) + 1,
            }
        )

    merged["updated_at"] = datetime.now(timezone.utc)
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(template_id).set(merged)
    except Exception as exc:
        logger.warning(
            "update_template failed for template %s: %s",
            sanitize_log_value(template_id), type(exc).__name__,
        )
        # Roll the Storage side back so it keeps matching the (unchanged)
        # Firestore doc: restore the overwritten bytes on a same-path
        # replacement, or delete the orphaned new object otherwise.
        if new_storage_path:
            try:
                bucket = storage.bucket()
                if new_storage_path == old_storage_path:
                    if overwrite_backup is not None:
                        bucket.blob(old_storage_path).upload_from_string(
                            overwrite_backup, content_type=DOCX_MIME
                        )
                else:
                    bucket.blob(new_storage_path).delete()
            except Exception as rollback_exc:
                logger.warning(
                    "update_template: storage rollback failed for %s: %s",
                    sanitize_log_value(template_id),
                    type(rollback_exc).__name__,
                )
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    # Delete the superseded Storage object only after the doc points at
    # the new one (a stale extra object is preferable to a broken doc).
    if new_storage_path and old_storage_path and old_storage_path != new_storage_path:
        try:
            bucket = storage.bucket()
            bucket.blob(old_storage_path).delete()
        except NotFound:
            # Old object already gone — nothing to clean up.
            pass
        except Exception as exc:
            logger.warning(
                "update_template: old object cleanup failed for %s: %s",
                sanitize_log_value(template_id), type(exc).__name__,
            )

    return merged, []


def delete_template(template_id: str) -> tuple[bool, str]:
    """Delete the Firestore doc and its Storage object."""
    existing = get_template(template_id)
    if not existing:
        return False, "Gabarit introuvable."

    storage_path = existing.get("storage_path", "")
    if storage_path:
        try:
            bucket = storage.bucket()
            bucket.blob(storage_path).delete()
        except NotFound:
            logger.info(
                "delete_template: blob already missing for %s",
                sanitize_log_value(template_id),
            )
        except Exception:
            log_unexpected("template file delete failed")
            return False, "Erreur lors de la suppression du fichier. Veuillez réessayer."

    try:
        db.collection(COLLECTION).document(template_id).delete()
        return True, ""
    except Exception:
        log_unexpected("template delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


def get_template_bytes(template_id: str) -> Optional[bytes]:
    """Download the current template file (for filling)."""
    template = get_template(template_id)
    if not template or not template.get("storage_path"):
        return None
    try:
        bucket = storage.bucket()
        return bucket.blob(template["storage_path"]).download_as_bytes()
    except Exception as exc:
        logger.warning(
            "get_template_bytes failed for %s: %s",
            sanitize_log_value(template_id), type(exc).__name__,
        )
        return None


def get_signed_url(template_id: str, expires_in_minutes: int = 15) -> Optional[str]:
    """Signed download URL for the template file (15-minute expiry)."""
    template = get_template(template_id)
    if not template or not template.get("storage_path"):
        return None
    try:
        bucket = storage.bucket()
        blob = bucket.blob(template["storage_path"])

        # On App Engine Standard, ADC lacks a local private key — sign via
        # the IAM signBlob API (same approach as models/document.py).
        signing_creds, _ = google.auth.default()
        signing_creds.refresh(auth_requests.Request())

        filename = template.get("filename") or "gabarit.docx"
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expires_in_minutes),
            method="GET",
            query_parameters={
                "response-content-disposition": f'attachment; filename="{filename}"',
                "response-content-type": DOCX_MIME,
            },
            service_account_email=signing_creds.service_account_email,
            access_token=signing_creds.token,
        )
    except Exception:
        return None
