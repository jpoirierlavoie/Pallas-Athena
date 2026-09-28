"""Document templates ("gabarits") — Firestore + Storage persistence (Phase H).

Standard CRUD per CLAUDE.md, with the lot 2A (step T3, 2026-09-27) rules
layered on. Not DAV-exposed — no DAV UID, no CTag bumping. Template files
live in Firebase Storage and are NOT ``documents`` records; generated
outputs are independent copies saved via ``models/document.upload_document``.

Placeholder extraction/classification happens at upload and file
replacement (utils/docx_fill + utils/template_fields) so the template doc
always carries its current field inventory.

Three rules since T3, each closing a silent defect:

* **Whitelist.** The caller's metadata is read through ``_METADATA_KEYS``
  (name, description, category, kind) and nothing else: the old
  ``{**existing, **_sanitize_data(data)}`` merge let any caller overwrite
  ``storage_path``, ``placeholders``, ``id`` or ``version``. A name or a
  description that ``security.sanitize`` would ALTER (a ``<…>`` run, an
  over-long text) is refused with a French message, never truncated.

* **Versions (D11).** A file replacement never overwrites and never
  deletes: the new bytes go to a FRESH object
  ``users/{uid}/templates/{template_id}/v{N}/{filename}`` (created with
  ``if_generation_match=0``, so two racing replacements can never land on
  one object), and a WRITE-ONCE entry ``doc_templates/{id}/versions/{N}``
  records that file — its name, size, SHA-256, path, when and by which
  path it was installed, and its placeholder inventory — in the SAME
  transaction that moves the template to version N. The previous object
  stays: it IS version N-1's bytes. The same SHA-256 as the current file
  is a no-op. « Rétablir » (:func:`restore_template_version`) installs an
  older version's bytes as a NEW version: history only ever grows.
  Before T3 a replacement destroyed the previous file (an in-place
  overwrite, or a delete of the old object) — the one template printed on
  every client's invoice note could be lost in a click.

* **The « actif » designation (D11).** For each special kind — the invoice
  note d'honoraires and the note print — ONE template is DESIGNATED by the
  lawyer (``active_for`` = the kind, ``active_designated_at``,
  ``active_designated_by``), through :func:`set_active_template`, whose
  only callers are the web route and the one-shot migration script.
  :func:`get_active_template` reads that designation and nothing else: no
  recency fallback. Before T3 the « most recently updated » template of the
  kind won, so ANY edit — a description tweak — silently switched the
  letterhead printed on every client's invoice note. None designated →
  ``None`` (the generators refuse, naming the fix); an unreadable store →
  :class:`TemplateReadError`, never « none designated », which would tell
  the lawyer to designate a template that already is.

Firestore: ``versions`` is a subcollection (keyed ``get()`` and a
single-field ``order_by("version")``); the designation reads are
single-field equalities (``active_for``, ``kind``) — every one served by an
automatic index, no composite index. The version entries follow the
``audit_events`` exception to Rule 7: write-once, no ``etag``.
"""

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import google.auth
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport import requests as auth_requests
from firebase_admin import storage
from google.cloud import firestore
from google.cloud.exceptions import NotFound
from google.cloud.firestore_v1.base_query import FieldFilter
from werkzeug.utils import secure_filename

from models import concurrency, db, provenance
from security import sanitize
from utils import storage_identity
from utils.docx_fill import validate_template
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils.template_fields import classify_placeholders

logger = logging.getLogger(__name__)

COLLECTION = "doc_templates"
VERSIONS = "versions"

# A gabarit's category is a DISTINCT, deliberately-narrow taxonomy — it shares
# only key names with models/document.VALID_CATEGORIES. Do NOT align it with
# the (much larger) documents vocabulary (spec §11).
VALID_CATEGORIES = ("procédure", "correspondance", "autre")
CATEGORY_LABELS = {
    "procédure": "Procédure",
    "correspondance": "Correspondance",
    "autre": "Autre",
}

# Discriminator (Phase H.2, extended H.3): an ordinary gabarit vs the two
# purpose-bound templates — the invoice note-d'honoraires the /factures page
# fills, and the note-print gabarit the /notes page fills (markdown body →
# formatted Word content). Kept separate from `category` so the user's own
# category taxonomy stays free. For each special kind, the template the
# LAWYER DESIGNATED is the one used (lot 2A, T3 — before, the most recently
# updated one won, and any edit could switch it).
VALID_KINDS = ("gabarit", "note_honoraires", "note")
KIND_LABELS = {
    "gabarit": "Gabarit",
    "note_honoraires": "Note d'honoraires (facture)",
    "note": "Note (impression)",
}
# The kinds that carry an « actif » designation — exactly one template each.
SPECIAL_KINDS = ("note_honoraires", "note")
# The short name the refusals give each special kind.
ACTIVE_KIND_NAMES = {
    "note_honoraires": "Note d'honoraires",
    "note": "Note (impression)",
}

# The designation fields. Deliberately NOT in _default_doc: absent means
# « never designated », and only set_active_template writes them.
ACTIVE_FIELDS = ("active_for", "active_designated_at", "active_designated_by")

# The only metadata a caller can set — on create and on update alike.
_METADATA_KEYS = ("name", "description", "category", "kind")
NAME_MAX = 120
DESCRIPTION_MAX = 2000
_FIELD_LABELS = {
    "name": "Le nom du gabarit",
    "description": "La description",
    "category": "La catégorie",
    "kind": "Le type de gabarit",
}
_TEXT_LIMITS = {"name": NAME_MAX, "description": DESCRIPTION_MAX}

# An orphan version object — a replacement whose bytes reached Storage but
# whose transaction never committed, and whose rollback failed too — can
# only be told from a replacement in flight by its AGE: every request is
# SIGKILLed by gunicorn at 60 s, so an unreferenced object older than this
# belongs to no live request.
_ORPHAN_MIN_AGE = timedelta(minutes=5)

# A replacement file is written under the uid segment of the STORED path;
# when that path cannot name the owner, the replacement is refused (see
# update_template) rather than written under a fallback prefix.
TEMPLATE_PATH_INVALID_MESSAGE = (
    "Le fichier de ce gabarit est rangé à un emplacement qui ne désigne pas "
    "le propriétaire du cabinet : il ne peut pas être remplacé. Rien n'a été "
    "modifié. Téléversez le nouveau fichier comme un nouveau gabarit."
)
MAX_TEMPLATE_SIZE = 10 * 1024 * 1024  # compressed .docx cap (also in docx_fill)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_SPLIT_RUN_WARNING = (
    "Le champ «{name}» semble fragmenté par Word et ne sera pas rempli. "
    "Retapez le champ d'un seul trait dans Word, sans pause ni correction "
    "automatique, puis téléversez le fichier à nouveau."
)

TEXT_CHEVRONS_ERROR = (
    "{label} : un passage entre chevrons serait retiré à l'enregistrement "
    "— retirez les chevrons."
)
NOT_FOUND_ERROR = "Gabarit introuvable."
READ_ERROR = (
    "Le gabarit n'a pas pu être lu — lecture impossible. Rien n'a été "
    "modifié : réessayez dans un instant."
)
SAVE_ERROR = "Erreur lors de la sauvegarde. Veuillez réessayer."
UPLOAD_ERROR = "Erreur lors du téléversement. Veuillez réessayer."
ACTIVE_KIND_CHANGE_ERROR = (
    "Ce gabarit est le gabarit actif des « {kind} » : son type ne peut pas "
    "changer. Désignez d'abord un autre gabarit actif pour ce type, puis "
    "modifiez celui-ci."
)
NOT_SPECIAL_ERROR = (
    "Seuls les gabarits « Note d'honoraires » et « Note (impression) » se "
    "désignent comme gabarit actif."
)
VERSION_IN_PROGRESS_ERROR = (
    "Une autre version de ce gabarit est en cours d'enregistrement. Rien n'a "
    "été modifié : réessayez dans quelques minutes."
)
VERSION_NOT_FOUND_ERROR = "Cette version du gabarit n'existe pas."
VERSION_IS_CURRENT_ERROR = (
    "Cette version est déjà la version en vigueur du gabarit."
)
VERSION_FILE_MISSING_ERROR = (
    "Le fichier de cette version est introuvable : elle ne peut pas être "
    "rétablie."
)
VERSION_INTEGRITY_ERROR = (
    "Le fichier de cette version ne correspond plus à l'empreinte enregistrée "
    "lors de son installation : rétablissement refusé. Rien n'a été modifié."
)


class TemplateReadError(RuntimeError):
    """The templates could not be read — distinct from « none designated »."""


class _Refused(Exception):
    """Raised inside a transactional body: nothing is written, the French
    errors travel back to the caller (the real ``transactional`` decorator
    rolls back on any exception)."""

    def __init__(self, errors: list[str]):
        super().__init__("refused")
        self.errors = list(errors)


class _NewFile(NamedTuple):
    """A replacement file, validated and extracted, not yet stored."""

    data: bytes
    safe_filename: str
    original_filename: str
    sha256: str
    extraction: dict
    user_segment: str
    base_version: int


def _default_doc() -> dict:
    return {
        "id": "",
        "name": "",
        "description": "",
        "category": "autre",
        "kind": "gabarit",
        "filename": "",
        "original_filename": "",
        "file_size": 0,
        "storage_path": "",
        "sha256": "",
        "version": 1,
        "placeholders": [],
        "auto_fields": [],
        "manual_fields": [],
        "passthrough_fields": [],
        "slots_required": [],
        "validation_warnings": [],
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def is_active(template: Optional[dict]) -> bool:
    """True when *template* is the designated template of its own kind."""
    template = template or {}
    kind = template.get("kind") or "gabarit"
    return kind in SPECIAL_KINDS and template.get("active_for") == kind


# ── Metadata: whitelist, typed, refused rather than mangled ────────────


def _metadata_input(data: dict) -> tuple[dict, list[str]]:
    """The whitelisted keys of *data*, typed; every other key is IGNORED.

    Only SHAPE is checked here; the content rules live in
    :func:`_metadata_value_errors`, so an edit holds them to the values it
    actually CHANGES (a legacy value posted back untouched is not a new
    write, and must not block the save of another field).
    """
    out: dict = {}
    errors: list[str] = []
    for key in _METADATA_KEYS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value
        else:
            errors.append(f"{_FIELD_LABELS[key]} : un texte est attendu.")
    return out, errors


def _text_errors(label: str, value: str, limit: int) -> list[str]:
    """Refuse what ``security.sanitize`` would alter — never alter it."""
    if len(value) > limit:
        return [f"{label} : {limit} caractères au plus."]
    if sanitize(value, max_length=limit) != value:
        return [TEXT_CHEVRONS_ERROR.format(label=label)]
    return []


def _metadata_value_errors(fields: dict) -> list[str]:
    errors: list[str] = []
    for key, value in fields.items():
        if key in _TEXT_LIMITS:
            errors += _text_errors(_FIELD_LABELS[key], value, _TEXT_LIMITS[key])
        elif key == "category" and value not in VALID_CATEGORIES:
            errors.append("Catégorie invalide.")
        elif key == "kind" and value not in VALID_KINDS:
            errors.append("Type de gabarit invalide.")
    return errors


def _validate(data: dict) -> list[str]:
    """Whole-record rules (the per-value ones are above)."""
    errors: list[str] = []
    if not (data.get("name") or "").strip():
        errors.append("Le nom du gabarit est requis.")
    if (data.get("category") or "") not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")
    if (data.get("kind") or "gabarit") not in VALID_KINDS:
        errors.append("Type de gabarit invalide.")
    return errors


def _changed_metadata(existing: dict, proposed: dict) -> dict:
    defaults = _default_doc()
    return {
        key: value for key, value in proposed.items()
        if existing.get(key, defaults.get(key)) != value
    }


# ── File validation and extraction ─────────────────────────────────────


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
    manual_set = set(classification.manual)
    passthrough_set = set(classification.passthrough)
    fields = {
        "placeholders": validation.placeholders,
        "auto_fields": [
            n for n in validation.placeholders if n in classification.auto
        ],
        "manual_fields": [
            n for n in validation.placeholders if n in manual_set
        ],
        # Left verbatim in the output for the user to complete in Word
        # (former ALL-CAPS blocks, civilité, salutations, unknown names).
        "passthrough_fields": [
            n for n in validation.placeholders if n in passthrough_set
        ],
        "slots_required": sorted(classification.slots_required),
        "validation_warnings": [
            _SPLIT_RUN_WARNING.format(name=n)
            for n in validation.split_run_suspects
        ],
    }
    return fields, []


_INVENTORY_KEYS = (
    "placeholders", "auto_fields", "manual_fields", "passthrough_fields",
    "slots_required", "validation_warnings",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Storage ────────────────────────────────────────────────────────────


def _template_object_path(
    uid: object, template_id: str, version: int, safe_filename: str
) -> str:
    """The ONE builder of a template object path — ``v{N}`` per version.

    A version's object is never overwritten, so its path must be unique to
    it: the version segment guarantees it (and it can never equal a legacy
    path, which has no version segment). The uid goes through
    ``require_uid`` whoever computed it (plan rule 8).
    """
    uid = storage_identity.require_uid(uid)
    return f"users/{uid}/templates/{template_id}/v{int(version)}/{safe_filename}"


def _stored_owner_segment(storage_path: str) -> str:
    """The uid segment of a stored template path, validated — or raise.

    The owner is fixed at creation: a replacement lives under the SAME
    prefix as the file it replaces. A path without the
    ``users/{uid}/templates/…`` layout, or whose segment is not a usable
    uid, raises: the old code fell back to ``"unknown"`` and wrote the new
    file under ``users/unknown/``, where it resolved and nothing ever
    looked wrong.
    """
    parts = (storage_path or "").split("/")
    if len(parts) < 5 or parts[0] != "users" or parts[2] != "templates":
        raise storage_identity.InvalidStorageUid()
    return storage_identity.require_uid(parts[1])


def _upload_create_only(storage_path: str, data: bytes) -> Optional[int]:
    """Create the object at *storage_path*; return its generation.

    ``if_generation_match=0``: the service refuses (412) when ANY object is
    already there, so two replacements racing on one version path can
    never overwrite each other's bytes. Raises ``PreconditionFailed`` on
    that refusal, anything else on a store failure.
    """
    blob = storage.bucket().blob(storage_path)
    blob.upload_from_string(
        data, content_type=DOCX_MIME, if_generation_match=0
    )
    return blob.generation


def _referenced(template_id: str, storage_path: str) -> Optional[bool]:
    """Does a committed record name *storage_path*? ``None`` = unreadable."""
    ref = db.collection(COLLECTION).document(template_id)
    try:
        snap = ref.get()
        if snap.exists and (snap.to_dict() or {}).get("storage_path") == storage_path:
            return True
        hits = list(
            ref.collection(VERSIONS)
            .where(filter=FieldFilter("storage_path", "==", storage_path))
            .limit(1)
            .stream()
        )
        return bool(hits)
    except Exception:
        return None


def _delete_own_object_unreferenced(
    template_id: str, storage_path: str, generation: Optional[int]
) -> None:
    """Roll back THIS call's object — unless a committed record names it.

    The generation guard proves the object is the one this call wrote; the
    reference check proves nobody depends on it (this call's own commit
    may have landed while its answer was lost). An unreadable record keeps
    the object: an orphan costs storage, a version without bytes costs a
    template.
    """
    if generation is None:
        return
    referenced = _referenced(template_id, storage_path)
    if referenced is not False:
        return
    try:
        storage.bucket().blob(storage_path).delete(
            if_generation_match=generation
        )
    except (NotFound, PreconditionFailed):
        pass
    except Exception:
        log_unexpected("template version rollback failed",
                       template_id=template_id)


def _clear_stale_orphan(template_id: str, storage_path: str) -> bool:
    """Delete an OLD, unreferenced object at *storage_path*; True if done.

    Called when a create-only upload met an existing object. Only an object
    older than :data:`_ORPHAN_MIN_AGE` that no committed record names is an
    orphan (a failed replacement whose rollback failed too); anything
    younger may be a replacement in flight, and is left alone.
    """
    blob = storage.bucket().blob(storage_path)
    try:
        blob.reload()
    except NotFound:
        return True  # gone meanwhile — the next attempt can create it
    except Exception:
        return False
    created = getattr(blob, "time_created", None)
    if not isinstance(created, datetime):
        return False
    if datetime.now(timezone.utc) - created < _ORPHAN_MIN_AGE:
        return False
    if _referenced(template_id, storage_path) is not False:
        return False
    try:
        blob.delete(if_generation_match=blob.generation)
    except NotFound:
        pass
    except Exception:
        return False
    return True


def _store_version_bytes(
    template_id: str, storage_path: str, data: bytes
) -> tuple[Optional[int], list[str]]:
    """Upload a new version's bytes (create-only); ``(generation, errors)``."""
    for attempt in (1, 2):
        try:
            return _upload_create_only(storage_path, data), []
        except PreconditionFailed:
            if attempt == 2 or not _clear_stale_orphan(template_id, storage_path):
                return None, [VERSION_IN_PROGRESS_ERROR]
        except Exception:
            log_unexpected("template upload failed", template_id=template_id)
            return None, [UPLOAD_ERROR]
    return None, [UPLOAD_ERROR]  # pragma: no cover — the loop always returns


# ── Version entries (write-once) ───────────────────────────────────────


def _versions_ref(template_id: str):
    return db.collection(COLLECTION).document(template_id).collection(VERSIONS)


def _version_entry(
    version: int,
    *,
    filename: str,
    original_filename: str,
    file_size: int,
    storage_path: str,
    sha256: str,
    inventory: dict,
    now: datetime,
    restored_from: Optional[int] = None,
    restored_by: str = "",
) -> dict:
    """A version's write-once record — no etag, never updated."""
    entry = {
        "version": int(version),
        "filename": filename,
        "original_filename": original_filename,
        "file_size": int(file_size),
        "storage_path": storage_path,
        "sha256": sha256,
        "created_at": now,
        "created_via": provenance.current_via(),
        "restored_from": restored_from,
        "restored_by": sanitize(str(restored_by or ""), max_length=200),
    }
    for key in _INVENTORY_KEYS:
        entry[key] = list(inventory.get(key) or [])
    return entry


def _backfilled_entry(current: dict, now: datetime) -> dict:
    """The entry of a file installed BEFORE versions were recorded.

    A template created before T3 has no entry for its current file; the
    first replacement records one from the main document, so that version
    stays listed and restorable. Its installation instant is known only for
    a version 1 (the template's creation); a later one reads « inconnue ».
    """
    version = int(current.get("version") or 1)
    first = version == 1
    entry = {
        "version": version,
        "filename": current.get("filename", ""),
        "original_filename": current.get("original_filename", ""),
        "file_size": int(current.get("file_size") or 0),
        "storage_path": current.get("storage_path", ""),
        "sha256": current.get("sha256", "") or "",
        "created_at": current.get("created_at") if first else None,
        "created_via": (current.get("created_via", "") or "") if first else "",
        "restored_from": None,
        "restored_by": "",
        "backfilled": True,
        "recorded_at": now,
    }
    for key in _INVENTORY_KEYS:
        entry[key] = list(current.get(key) or [])
    return entry


# ── CRUD ────────────────────────────────────────────────────────────────

def create_template(
    file_stream,
    filename: str,
    file_size: int,
    metadata: dict,
    user_id: str,
) -> tuple[Optional[dict], list[str]]:
    """Validate, extract placeholders, upload to Storage, persist the doc.

    The metadata goes through the whitelist; a special-kind template is
    NEVER active on creation — the lawyer designates it (D11). The file is
    version 1, at its ``v1`` path, recorded in ``versions/1`` in the same
    batch as the template itself.
    """
    # The uid FIRST — before the stream is read: a template must never be
    # written under a prefix that is not the owner's (users/unknown/…).
    try:
        user_id = storage_identity.require_uid(user_id)
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, [str(exc)]
    fields, errors = _metadata_input(metadata or {})
    if not errors:
        errors = _metadata_value_errors(fields)
    if errors:
        return None, errors
    merged = {**_default_doc(), **fields}
    meta_errors = _validate(merged)
    if meta_errors:
        return None, meta_errors

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

    now = datetime.now(timezone.utc)
    template_id = str(uuid.uuid4())
    safe_filename = _safe_filename(filename)
    storage_path = _template_object_path(user_id, template_id, 1, safe_filename)
    digest = _sha256(docx_bytes)

    merged.update(extraction)
    merged.update(
        {
            "id": template_id,
            "filename": safe_filename,
            "original_filename": filename,
            "file_size": len(docx_bytes),
            "storage_path": storage_path,
            "sha256": digest,
            "version": 1,
            **provenance.create_fields(now),
        }
    )
    entry = _version_entry(
        1, filename=safe_filename, original_filename=filename,
        file_size=len(docx_bytes), storage_path=storage_path, sha256=digest,
        inventory=extraction, now=now,
    )

    # Upload to Firebase Storage (never log the path — it may embed names).
    generation, errors = _store_version_bytes(template_id, storage_path, docx_bytes)
    if errors:
        return None, errors

    ref = db.collection(COLLECTION).document(template_id)
    try:
        batch = db.batch()
        batch.create(ref, merged)
        batch.create(_versions_ref(template_id).document("1"), entry)
        batch.commit()
    except Exception as exc:
        logger.warning(
            "create_template failed for template %s: %s",
            template_id, type(exc).__name__,
        )
        _delete_own_object_unreferenced(template_id, storage_path, generation)
        return None, [SAVE_ERROR]

    provenance.note_commit(COLLECTION, template_id)
    return merged, []


def get_template(template_id: str, *, strict: bool = False) -> Optional[dict]:
    """The template, ``None`` when absent.

    Fails OPEN to ``None`` by default (the web pages answer « introuvable »
    either way). ``strict=True`` RAISES :class:`TemplateReadError` on a read
    failure instead — for a caller that must never report « this template
    does not exist » over an outage (the connector's ``list_templates``).
    """
    try:
        doc = db.collection(COLLECTION).document(template_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        if strict:
            log_unexpected("template read failed", template_id=template_id)
            raise TemplateReadError(template_id) from exc
        logger.warning(
            "get_template failed for %s: %s",
            sanitize_log_value(template_id), type(exc).__name__,
        )
        return None


def _read_template_strict(template_id: str) -> Optional[dict]:
    """The template, ``None`` when absent — RAISES on a read failure.

    For the writers: a read error must never read as « introuvable ».
    """
    if not template_id:
        return None
    doc = db.collection(COLLECTION).document(template_id).get()
    return (doc.to_dict() or {}) if doc.exists else None


def list_templates(
    category: Optional[str] = None,
    search: Optional[str] = None,
    *,
    strict: bool = False,
) -> list[dict]:
    """All templates ordered by name; small bounded collection (tens of
    docs) — category/search filtering happens client-side, no index.

    Fails OPEN to ``[]`` by default — right for the web list and the popup
    select. ``strict=True`` RAISES :class:`TemplateReadError` instead: the
    connector must never answer « the practice has no template » over an
    unreadable store, a false statement a caller would act on.
    """
    try:
        query = db.collection(COLLECTION).order_by("name")
        results = [doc.to_dict() for doc in query.stream()]
    except Exception as exc:
        if strict:
            log_unexpected("template list failed")
            raise TemplateReadError("list") from exc
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


# ── The « actif » designation (D11) ────────────────────────────────────


def recency_winner(templates: list[dict], kind: str) -> Optional[dict]:
    """The PRE-T3 selection rule, kept verbatim for the migration script.

    The most recently updated template of *kind* (legacy docs with no
    ``kind`` never match; a missing ``updated_at`` sorts last; ties keep
    the stream order). It is no longer how a template is SELECTED — only
    how ``scripts/designer_gabarits_actifs.py`` designates, once, the
    template production was already using.
    """
    matches = [t for t in templates if t.get("kind") == kind]
    if not matches:
        return None
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    matches.sort(key=lambda t: t.get("updated_at") or epoch, reverse=True)
    return matches[0]


def get_active_template(kind: str) -> Optional[dict]:
    """The template the lawyer DESIGNATED for *kind*, or ``None``.

    No recency fallback, ever: a template Claude created or edited must
    never become the letterhead by being the newest. Raises
    :class:`TemplateReadError` when the store cannot be read — distinct
    from « none designated », whose message tells the lawyer to designate
    one. Two designated templates (only a hand edit of the store can do
    that — :func:`set_active_template` clears the previous holder in its
    transaction) → the latest designation, and an ERROR log line.
    """
    if kind not in SPECIAL_KINDS:
        raise ValueError(f"not a special template kind: {kind!r}")
    try:
        holders = [
            snap.to_dict() or {}
            for snap in db.collection(COLLECTION)
            .where(filter=FieldFilter("active_for", "==", kind))
            .stream()
        ]
    except Exception as exc:
        log_unexpected("active template read failed", kind=kind)
        raise TemplateReadError(kind) from exc
    matches = [t for t in holders if (t.get("kind") or "gabarit") == kind]
    if not matches:
        return None
    if len(matches) > 1:
        log_unexpected(
            "several templates designated active for one kind",
            exc_info=False, kind=kind, count=len(matches),
        )
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        matches.sort(
            key=lambda t: t.get("active_designated_at") or epoch, reverse=True
        )
    return matches[0]


def get_note_honoraires_template() -> Optional[dict]:
    """The DESIGNATED invoice note-d'honoraires template (Phase H.2).

    Raises :class:`TemplateReadError` on a read failure."""
    return get_active_template("note_honoraires")


def get_note_template() -> Optional[dict]:
    """The DESIGNATED note-print template (kind « note », Phase H.3).

    Raises :class:`TemplateReadError` on a read failure."""
    return get_active_template("note")


def set_active_template(
    template_id: str, *, par: str, expected_etag: Optional[str]
) -> tuple[Optional[dict], list[str]]:
    """Designate *template_id* as THE template of its special kind.

    The lawyer's act (D11): the web route and the migration script are its
    only callers — a source sweep pins it, and no connector path reaches
    it. One transaction reads the target, every template of its kind and
    every current holder of the designation (reads first), then clears the
    previous holder and designates the target — so two racing designations
    can never leave two active templates: the loser re-runs on the
    winner's commit and clears it.

    ``expected_etag`` — the version the button was rendered from:
    designating says « I read THIS template » (``None``: no check). A
    template that already is the sole designation writes nothing.
    """
    ref = db.collection(COLLECTION).document(template_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, list[str]]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused([NOT_FOUND_ERROR])
        target = snap.to_dict() or {}
        if not concurrency.matches(target, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        kind = target.get("kind") or "gabarit"
        if kind not in SPECIAL_KINDS:
            raise _Refused([NOT_SPECIAL_ERROR])
        # Every template of the kind is READ — not only the holders — so a
        # rival designation (which writes one of them) conflicts with this
        # transaction even when no template was designated yet.
        _same_kind = list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("kind", "==", kind))
            .stream(transaction=transaction)
        )
        holders = [
            s for s in db.collection(COLLECTION)
            .where(filter=FieldFilter("active_for", "==", kind))
            .stream(transaction=transaction)
            if s.id != template_id
        ]
        if target.get("active_for") == kind and not holders:
            return target, []
        now = datetime.now(timezone.utc)
        for holder in holders:
            transaction.update(holder.reference, {
                "active_for": firestore.DELETE_FIELD,
                "active_designated_at": firestore.DELETE_FIELD,
                "active_designated_by": firestore.DELETE_FIELD,
                **provenance.update_fields(now),
            })
        fields = {
            "active_for": kind,
            "active_designated_at": now,
            "active_designated_by": sanitize(str(par or ""), max_length=200),
            **provenance.update_fields(now),
        }
        transaction.update(ref, fields)
        return {**target, **fields}, [h.id for h in holders] + [template_id]

    try:
        doc, written = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("template designation failed")
        return None, [SAVE_ERROR]
    for doc_id in written:
        provenance.note_commit(COLLECTION, doc_id)
    return doc, []


# ── Update, file replacement, restore ──────────────────────────────────


def _prepare_new_file(
    existing: dict, template_id: str, docx_bytes: bytes, filename: str,
    original_filename: str,
) -> tuple[Optional[_NewFile], list[str]]:
    """Validate and extract a replacement file (nothing is stored yet)."""
    try:
        segment = _stored_owner_segment(existing.get("storage_path", ""))
    except storage_identity.StorageIdentityUnavailable:
        return None, [TEMPLATE_PATH_INVALID_MESSAGE]
    extraction, errors = _extraction_fields(docx_bytes)
    if errors:
        return None, errors
    return _NewFile(
        data=docx_bytes,
        safe_filename=_safe_filename(filename),
        original_filename=original_filename,
        sha256=_sha256(docx_bytes),
        extraction=extraction,
        user_segment=segment,
        base_version=int(existing.get("version") or 1),
    ), []


def _commit_update(
    template_id: str,
    proposed: dict,
    new_file: Optional[_NewFile],
    *,
    expected_etag: Optional[str],
    expected_version: Optional[int],
    restored_from: Optional[int] = None,
    restored_by: str = "",
) -> tuple[Optional[dict], list[str], bool]:
    """Write the metadata changes and/or a new version, in ONE transaction.

    A new version's bytes are stored FIRST, at their own ``v{N}`` path
    (create-only); the transaction then records the version entry and
    moves the template to it, refusing if the template left the version
    the file was built on. Any refusal or failure removes only this call's
    own, unreferenced object.
    """
    storage_path = ""
    generation: Optional[int] = None
    if new_file is not None:
        storage_path = _template_object_path(
            new_file.user_segment, template_id, new_file.base_version + 1,
            new_file.safe_filename,
        )
        generation, errors = _store_version_bytes(
            template_id, storage_path, new_file.data
        )
        if errors:
            return None, errors, False

    ref = db.collection(COLLECTION).document(template_id)
    versions = _versions_ref(template_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool]:
        # Every read comes FIRST (the real client refuses a transactional
        # read after a staged write).
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused([NOT_FOUND_ERROR])
        current = snap.to_dict() or {}
        previous = None
        if new_file is not None:
            previous = versions.document(str(new_file.base_version)).get(
                transaction=transaction
            )
        if not concurrency.matches(current, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        stored_version = int(current.get("version") or 1)
        if expected_version is not None and stored_version != int(expected_version):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        if new_file is not None and stored_version != new_file.base_version:
            # Another version was installed since this file was prepared:
            # it would silently REPLACE that one. Refused like a stale save.
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        changes = _changed_metadata(current, proposed)
        errors = _metadata_value_errors(changes)
        errors += _validate({**_default_doc(), **current, **changes})
        if errors:
            raise _Refused(errors)
        if "kind" in changes and is_active(current):
            raise _Refused([ACTIVE_KIND_CHANGE_ERROR.format(
                kind=ACTIVE_KIND_NAMES.get(current.get("kind"), ""))])
        if not changes and new_file is None:
            return current, False
        now = datetime.now(timezone.utc)
        fields = dict(changes)
        if new_file is not None:
            new_version = new_file.base_version + 1
            fields.update(new_file.extraction)
            fields.update({
                "filename": new_file.safe_filename,
                "original_filename": new_file.original_filename,
                "file_size": len(new_file.data),
                "storage_path": storage_path,
                "sha256": new_file.sha256,
                "version": new_version,
            })
            if previous is not None and not previous.exists:
                transaction.create(
                    versions.document(str(new_file.base_version)),
                    _backfilled_entry(current, now),
                )
            transaction.create(
                versions.document(str(new_version)),
                _version_entry(
                    new_version, filename=new_file.safe_filename,
                    original_filename=new_file.original_filename,
                    file_size=len(new_file.data), storage_path=storage_path,
                    sha256=new_file.sha256, inventory=new_file.extraction,
                    now=now, restored_from=restored_from,
                    restored_by=restored_by,
                ),
            )
        fields.update(provenance.update_fields(now))
        transaction.update(ref, fields)
        return {**current, **fields}, True

    try:
        doc, changed = _apply(db.transaction())
    except _Refused as refusal:
        _delete_own_object_unreferenced(template_id, storage_path, generation)
        return None, refusal.errors, False
    except Exception as exc:
        logger.warning(
            "update_template failed for template %s: %s",
            sanitize_log_value(template_id), type(exc).__name__,
        )
        _delete_own_object_unreferenced(template_id, storage_path, generation)
        return None, [SAVE_ERROR], False
    if changed:
        provenance.note_commit(COLLECTION, template_id)
    return doc, [], changed


def update_template(
    template_id: str,
    data: dict,
    file_stream=None,
    filename: Optional[str] = None,
    file_size: Optional[int] = None,
    *,
    expected_etag: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """Update metadata; optionally install a new version of the file.

    Returns ``(template, errors, changed)``. *data* is read through the
    metadata whitelist (name, description, category, kind) — nothing else
    a caller sends is ever written — and the write is a PARTIAL update of
    what changed, so it can never resurrect a field another writer moved
    (the « actif » designation, a concurrent version). A value is checked
    only where it CHANGES.

    A file replacement never overwrites and never deletes (module
    docstring). The same SHA-256 as the current file is a no-op for the
    file; nothing changed at all → nothing written, ``changed`` False.

    ``expected_etag`` / ``expected_version`` (keyword-only): the write
    commits only against that version of the template — a stale one
    returns ``[STALE_ETAG_ERROR]`` and writes nothing. Without them a
    replacement is still refused if ANOTHER version was installed since
    this call read the template: it would silently replace that one.

    Changing the kind of the DESIGNATED template of a special kind is
    refused (the kind would be left with a designation that no longer
    matches it).
    """
    proposed, errors = _metadata_input(data or {})
    if errors:
        return None, errors, False
    try:
        existing = _read_template_strict(template_id)
    except Exception:
        log_unexpected("template read failed", template_id=template_id)
        return None, [READ_ERROR], False
    if existing is None:
        return None, [NOT_FOUND_ERROR], False

    new_file: Optional[_NewFile] = None
    if file_stream is not None and filename:
        # The owner segment is checked BEFORE the stream is read.
        try:
            _stored_owner_segment(existing.get("storage_path", ""))
        except storage_identity.StorageIdentityUnavailable:
            return None, [TEMPLATE_PATH_INVALID_MESSAGE], False
        file_errors = _validate_file(filename, file_size or 0)
        if file_errors:
            return None, file_errors, False
        try:
            docx_bytes = file_stream.read()
        except Exception as exc:
            logger.warning(
                "update_template: stream read failed: %s", type(exc).__name__
            )
            return None, ["Le fichier n'a pas pu être lu. Veuillez réessayer."], False
        new_file, errors = _prepare_new_file(
            existing, template_id, docx_bytes, filename, filename
        )
        if errors:
            return None, errors, False
        stored_sha = existing.get("sha256") or ""
        if stored_sha and hmac.compare_digest(stored_sha, new_file.sha256):
            new_file = None  # the same bytes: not a new version

    return _commit_update(
        template_id, proposed, new_file,
        expected_etag=expected_etag, expected_version=expected_version,
    )


def restore_template_version(
    template_id: str, version: int, *, par: str, expected_etag: Optional[str]
) -> tuple[Optional[dict], list[str], bool]:
    """Install version *version*'s bytes as a NEW version (never rewrite).

    Web only (« Rétablir »). The restored file becomes version N+1 whose
    entry names the version it came from; the history keeps everything.
    The stored bytes are checked against the SHA-256 recorded when that
    version was installed — a mismatch refuses. Restoring bytes identical
    to the current file writes nothing (``changed`` False).
    """
    try:
        version = int(version)
    except (TypeError, ValueError):
        return None, [VERSION_NOT_FOUND_ERROR], False
    try:
        existing = _read_template_strict(template_id)
        entry_snap = (
            _versions_ref(template_id).document(str(version)).get()
            if existing is not None else None
        )
    except Exception:
        log_unexpected("template version read failed", template_id=template_id)
        return None, [READ_ERROR], False
    if existing is None:
        return None, [NOT_FOUND_ERROR], False
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], False
    if version == int(existing.get("version") or 1):
        return None, [VERSION_IS_CURRENT_ERROR], False
    if entry_snap is None or not entry_snap.exists:
        return None, [VERSION_NOT_FOUND_ERROR], False
    entry = entry_snap.to_dict() or {}
    path = entry.get("storage_path") or ""
    if not path:
        return None, [VERSION_FILE_MISSING_ERROR], False
    try:
        blob = storage.bucket().blob(path)
        blob.reload()
        if blob.size is not None and int(blob.size) > MAX_TEMPLATE_SIZE:
            return None, [VERSION_FILE_MISSING_ERROR], False
        docx_bytes = blob.download_as_bytes()
    except NotFound:
        return None, [VERSION_FILE_MISSING_ERROR], False
    except Exception:
        log_unexpected("template version download failed",
                       template_id=template_id)
        return None, [READ_ERROR], False
    recorded = entry.get("sha256") or ""
    if recorded and not hmac.compare_digest(recorded, _sha256(docx_bytes)):
        return None, [VERSION_INTEGRITY_ERROR], False

    filename = entry.get("filename") or "gabarit.docx"
    new_file, errors = _prepare_new_file(
        existing, template_id, docx_bytes, filename,
        entry.get("original_filename") or filename,
    )
    if errors:
        return None, errors, False
    stored_sha = existing.get("sha256") or ""
    if stored_sha and hmac.compare_digest(stored_sha, new_file.sha256):
        # The current file already holds these bytes: nothing to install.
        # (The page's version was checked above — a no-op answered on a
        # template that moved since would confirm what the lawyer never saw.)
        return existing, [], False
    return _commit_update(
        template_id, {}, new_file,
        expected_etag=expected_etag, expected_version=None,
        restored_from=version, restored_by=par,
    )


def list_versions(
    template_id: str, limit: int = 50, *, strict: bool = False,
) -> list[dict]:
    """The template's recorded versions, newest first — DISPLAY only.

    Fails open to ``[]`` (a history pane, never a guard). A template
    created before T3 lists nothing until its first replacement records
    the file it had. ``strict=True`` RAISES :class:`TemplateReadError` on a
    read failure instead, for a caller that must tell « no history
    recorded » from « history unreadable » (the connector says which).
    """
    try:
        query = (
            _versions_ref(template_id)
            .order_by("version", direction=firestore.Query.DESCENDING)
            .limit(max(1, int(limit)))
        )
        return [snap.to_dict() or {} for snap in query.stream()]
    except Exception as exc:
        log_unexpected("template versions list failed", template_id=template_id)
        if strict:
            raise TemplateReadError(template_id) from exc
        return []


def delete_template(template_id: str) -> tuple[bool, str]:
    """Delete the template, every version entry, then every stored file.

    The records go FIRST, in one transaction (the template and all its
    ``versions/*``, read inside it — a replacement racing the delete aborts
    it rather than leaving an entry under a deleted template); the objects
    of every version follow, one by one. A failed object delete leaves an
    orphan under the owner's prefix and a log line — never a template
    whose file is gone.
    """
    ref = db.collection(COLLECTION).document(template_id)
    versions = _versions_ref(template_id)

    @firestore.transactional
    def _apply(transaction) -> set[str]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused([NOT_FOUND_ERROR])
        entries = list(versions.stream(transaction=transaction))
        paths = {(snap.to_dict() or {}).get("storage_path") or ""}
        for entry in entries:
            paths.add((entry.to_dict() or {}).get("storage_path") or "")
            transaction.delete(entry.reference)
        transaction.delete(ref)
        return {p for p in paths if p}

    try:
        paths = _apply(db.transaction())
    except _Refused as refusal:
        return False, refusal.errors[0]
    except Exception:
        log_unexpected("template delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."
    provenance.note_commit(COLLECTION, template_id)

    bucket = storage.bucket()
    for path in sorted(paths):
        try:
            bucket.blob(path).delete()
        except NotFound:
            logger.info(
                "delete_template: blob already missing for %s",
                sanitize_log_value(template_id),
            )
        except Exception:
            log_unexpected("template file delete failed",
                           template_id=template_id)
    return True, ""


def get_template_bytes(template_id: str) -> Optional[bytes]:
    """Download the current template file (for filling)."""
    return template_file_bytes(get_template(template_id))


def template_file_bytes(template: Optional[dict]) -> Optional[bytes]:
    """The bytes of the file THIS record names — the version it reports.

    A replacement writes a NEW object (``v{N}`` paths) and never overwrites
    one, so a record a caller already holds keeps naming its own bytes. A
    caller that reports « gabarit X, version N » fills from this, never from
    :func:`get_template_bytes`, whose re-read could pick up a replacement
    committed in between and print version N+1 under the name of N (lot 2A,
    T8). ``None`` when the record names no file or the download fails.
    """
    if not template or not template.get("storage_path"):
        return None
    try:
        bucket = storage.bucket()
        return bucket.blob(template["storage_path"]).download_as_bytes()
    except Exception as exc:
        logger.warning(
            "template_file_bytes failed for %s: %s",
            sanitize_log_value(str(template.get("id") or "")),
            type(exc).__name__,
        )
        return None


def _signed_download_url(
    storage_path: str, filename: str, expires_in_minutes: int
) -> Optional[str]:
    try:
        bucket = storage.bucket()
        blob = bucket.blob(storage_path)

        # On App Engine Standard, ADC lacks a local private key — sign via
        # the IAM signBlob API (same approach as models/document.py).
        signing_creds, _ = google.auth.default()
        signing_creds.refresh(auth_requests.Request())

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


def get_signed_url(template_id: str, expires_in_minutes: int = 15) -> Optional[str]:
    """Signed download URL for the template file (15-minute expiry)."""
    template = get_template(template_id)
    if not template or not template.get("storage_path"):
        return None
    return _signed_download_url(
        template["storage_path"], template.get("filename") or "gabarit.docx",
        expires_in_minutes,
    )


def get_version_signed_url(
    template_id: str, version: int, expires_in_minutes: int = 15
) -> Optional[str]:
    """Signed download URL (15 min) for one recorded version's file — web
    only, never emitted by the connector."""
    try:
        snap = _versions_ref(template_id).document(str(int(version))).get()
    except Exception:
        return None
    if not snap.exists:
        return None
    entry = snap.to_dict() or {}
    if not entry.get("storage_path"):
        return None
    filename = entry.get("filename") or "gabarit.docx"
    return _signed_download_url(
        entry["storage_path"], f"v{int(version)}-{filename}", expires_in_minutes,
    )
