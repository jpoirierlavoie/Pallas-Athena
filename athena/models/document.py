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
from google.api_core.exceptions import AlreadyExists, PreconditionFailed
from google.auth.transport import requests as auth_requests
from google.cloud import firestore
from google.cloud.exceptions import NotFound
from google.cloud.firestore_v1.base_query import FieldFilter
from firebase_admin import storage
from werkzeug.utils import secure_filename
from models import concurrency, db, provenance
from security import sanitize
from tz import to_mtl
from utils import storage_identity
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
        # LE champ du juriste — le seul texte libre qu'il saisit.
        # `description` a été retiré le 2026-08-31 : il était une TROISIÈME
        # zone de texte, et `record_analyse` y recopiait le résumé de
        # l'analyse, donc après toute analyse les deux disaient la même
        # chose. Un document porte désormais le texte du juriste (ici) et
        # celui du modèle (`analyse.resume`). Rien d'autre.
        "notes_internes": "",
        # La PROVENANCE d'un document produit par la machine — « Générée
        # depuis la facture 2026-003-03 ». Ni l'un ni l'autre des deux
        # textes ci-dessus : elle ne paraît sur aucun formulaire et ne
        # s'édite pas, exactement comme les champs `portail_*` qui ont
        # quitté `description` pour la même raison le 2026-08-27.
        "genere_depuis": "",
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
    """Sanitize all string values in *data*.

    Since lot 2A (T1) its only caller is the analysis derivation (the
    extract a model returns). The metadata a caller WRITES is refused
    rather than passed through here — see ``_metadata_value_errors``.
    """
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


# ── Metadata written by a caller: whitelisted, refused rather than mangled ──
#
# Lot 2A, T1 (2026-09-27). Two defects lived here:
#
# * ``_prepare_document_record`` merged ``{**_default_doc(), **metadata}``:
#   any key a caller handed in was persisted — ``analyse``, ``confirme``,
#   ``category_source``, ``storage_path``, ``version``… A document could be
#   born « analysed and confirmed » without any analysis, or pointing at
#   another document's bytes. The metadata a caller may give is now a
#   WHITELIST; the machine provenance of a portal document travels through
#   its own keyword (``portail=``), never through the metadata.
# * ``_sanitize_data`` TRUNCATED at 2000 characters and DELETED every
#   ``<…>`` run (``security.sanitize``), in silence: « Lettre <brouillon> »
#   was stored « Lettre », a long note lost its end, and the save reported
#   success. A value that ``sanitize`` would alter is now REFUSED, with a
#   French message naming the field and never quoting it.
#
# A malformed ``document_date`` used to become ``None`` — the stored date
# ERASED by a typo. It is refused too; only an empty value clears the date.

DISPLAY_NAME_MAX = 300
NOTES_INTERNES_MAX = 2000
GENERE_DEPUIS_MAX = 2000
TAG_MAX = 200
TAGS_MAX_ITEMS = 30

# What a creator's caller may choose. `genere_depuis` stays here — it is set
# by the application's own generators, never by a form — while the portal's
# three provenance fields travel through `portail=` (see above).
_RECORD_METADATA_KEYS = (
    "display_name", "category", "tags", "document_date", "folder_id",
    "notes_internes", "genere_depuis",
)
# What an EDIT may change. The folder moves through `move_document` only.
_METADATA_EDIT_KEYS = (
    "display_name", "category", "tags", "document_date", "notes_internes",
)
_PORTAIL_KEYS = ("portail_invitation_id", "portail_lot", "portail_sha512")
_PORTAIL_MAX = 200

_TEXT_LIMITS = {
    "display_name": DISPLAY_NAME_MAX,
    "notes_internes": NOTES_INTERNES_MAX,
    "genere_depuis": GENERE_DEPUIS_MAX,
}
_FIELD_LABELS = {
    "display_name": "Nom d'affichage",
    "notes_internes": "Notes internes",
    "genere_depuis": "Provenance",
    "category": "Catégorie",
    "tags": "Étiquettes",
    "document_date": "Date du document",
    "folder_id": "Dossier de classement",
}

DOCUMENT_DATE_ERROR = "Date du document invalide : attendu AAAA-MM-JJ."
CATEGORY_ERROR = "Catégorie invalide."
INVALID_DOCUMENT_ID = "Identifiant de document invalide."

# `category_source` a CALLER may pose. « analyse » is minted by
# `record_analyse` alone — a creator or an edit that claimed it would make a
# category read as derived from an analysis that never ran.
_POSABLE_CATEGORY_SOURCES = ("juriste", "mcp")

# D15 (2026-09-25): Claude may set a category directly, as PRESUMED — but
# never on an analysed document, whose category DERIVES from the analysis's
# closed sub-nature. `record_document_analysis` stays the path there.
MCP_CATEGORY_ON_ANALYSED = (
    "Ce document a été analysé : sa catégorie dérive de l'analyse et ne se "
    "pose pas directement. Enregistrez une nouvelle analyse, ou laissez le "
    "juriste la corriger dans l'application."
)


# D18 (2026-09-28, confirmed by the lawyer): Claude's PRESUMED category
# (D15) never replaces a category the LAWYER chose or confirmed. See
# :func:`category_set_by_lawyer` for how a lawyer's choice is told apart
# from an untouched default — and why a legacy document counts as his
# unless its category is « autre » or empty.
MCP_CATEGORY_ON_LAWYERS = (
    "La catégorie de ce document a été choisie ou confirmée par le juriste "
    "dans l'application — ou posée avant ce suivi, et tenue pour la sienne : "
    "une catégorie présumée ne la remplace pas. "
    "Signalez-lui l'écart ; lui seul la corrige."
)
# Where the web UPLOAD form starts (the select's pre-selected value): a
# NEW upload still equal to it was not chosen, and a LEGACY document (no
# marker) holding it is the one legacy category Claude may replace — see
# category_set_by_lawyer.
# Réception's versement form starts ELSEWHERE (« pièce »,
# routes/reception.VERSEMENT_DEFAULT_CATEGORY): each form is judged against
# its own pre-selection, never this one.
UPLOAD_DEFAULT_CATEGORY = "autre"


def category_set_by_lawyer(doc: Optional[dict]) -> bool:
    """True when *doc*'s category is the LAWYER's own choice (D18).

    The marker ``category_set_by_lawyer`` (a bool) is written:

    * True by an explicit lawyer gesture — the web edit form CHANGING the
      category (``update_metadata`` with a « juriste » source), « Confirmer
      la catégorie » (:func:`confirmer_categorie`), the confirmation or the
      edition of an analysis (:func:`confirmer_analyse`,
      :func:`update_analyse` — both make the category « a determination of
      the lawyer »), or an upload whose form the lawyer moved OFF its own
      pre-selected value (the web upload: other than
      :data:`UPLOAD_DEFAULT_CATEGORY`; Réception's versement: other than
      ``routes.reception.VERSEMENT_DEFAULT_CATEGORY``);
    * False by every other writer of a category — a connector category
      (presumed, D15), an upload left on the default, a generation (the
      template's own category), an analysis.

    A copy inherits its source's answer (:func:`copy_document`). And a
    category whose provenance is not « juriste » (a presumed « mcp » one,
    or one an analysis derived) is never the lawyer's, whatever the marker.

    LEGACY documents (no marker — every document stored before
    2026-09-28; nothing is migrated): the lawyer's when
    ``category_confirmed_by`` is present (a « Confirmer la catégorie »), OR
    when their category is anything but empty or the upload default
    :data:`UPLOAD_DEFAULT_CATEGORY` (« autre »). The lawyer's own words
    (2026-09-28): « refuse on a confirmed one » — and before the marker
    existed, nothing recorded whether a non-default category was typed on
    the form, left on Réception's « pièce » or printed by a generation, so
    every one of them is read as his. That errs on the side he chose:
    Claude may fail to replace a category nobody chose (he fixes it in the
    application), never overwrite one he did. Only a legacy « autre » (or
    empty) — the one value an untouched upload stores — stays replaceable,
    beside the « mcp » and « analyse » sources, which are never his. An
    « autre » the lawyer wants kept is made his by choosing it in the edit
    form (a change, then back), or by confirming it once Claude presumed
    another.

    *doc* must be read through :func:`_migrate_category` (every reader of
    this module does): a retired value (« entente », « note ») is judged as
    the « autre » it reads as.
    """
    doc = doc or {}
    if str(doc.get("category_source") or "juriste") != "juriste":
        return False
    marker = doc.get("category_set_by_lawyer")
    if isinstance(marker, bool):
        return marker
    if str(doc.get("category_confirmed_by") or "").strip():
        return True
    category = str(doc.get("category") or "").strip()
    return bool(category) and category != UPLOAD_DEFAULT_CATEGORY


class _Refused(Exception):
    """Raised inside a transactional body: nothing is written, the French
    errors travel back to the caller (the real ``transactional`` decorator
    rolls back on any exception)."""

    def __init__(self, errors: list[str]):
        super().__init__("refused")
        self.errors = list(errors)


def is_canonical_uuid4(value: object) -> bool:
    """True for a lowercase, hyphenated UUIDv4 — the shape this app mints.

    ``uuid.UUID`` also accepts braces, ``urn:uuid:``, uppercase and other
    versions; a document id a caller RESERVES must be exactly what
    ``uuid.uuid4()`` would have produced, or it could name something that is
    not a document at all (a ``staging/{uid}/exports/…`` segment).
    """
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return (
        str(parsed) == value and parsed.version == 4
        and parsed.variant == uuid.RFC_4122
    )


def has_analysis(doc: Optional[dict]) -> bool:
    """True when *doc* carries an analysis (its category then DERIVES)."""
    doc = doc or {}
    return bool((doc.get("analyse") or {}).get("sous_nature")) or (
        doc.get("category_source") == "analyse"
    )


def _parse_document_date(raw) -> tuple[Optional[datetime], Optional[str]]:
    """Strict twin of :func:`_coerce_document_date` for WRITES.

    ``None``/blank → ``(None, None)`` — an explicit clearing. A datetime or a
    date → its calendar date at midnight UTC. A ``YYYY-MM-DD`` string → the
    same. Anything else → ``(None, DOCUMENT_DATE_ERROR)``: a typo must never
    read as « clear the date ».
    """
    if raw is None:
        return None, None
    if isinstance(raw, (datetime, date)):
        return _coerce_document_date(raw), None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, None
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None, DOCUMENT_DATE_ERROR
        return parsed.replace(tzinfo=timezone.utc), None
    return None, DOCUMENT_DATE_ERROR


def _metadata_input(data: dict, keys: tuple[str, ...]) -> tuple[dict, list[str]]:
    """The whitelisted keys of *data*, typed; unknown keys are ignored.

    Only SHAPE is checked here (a text is a text, a date parses, the tags
    are a list of texts): the content rules live in
    :func:`_metadata_value_errors`, so that an edit can hold them to the
    values it actually CHANGES.
    """
    out: dict = {}
    errors: list[str] = []
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        label = _FIELD_LABELS[key]
        if key == "document_date":
            parsed, error = _parse_document_date(value)
            if error:
                errors.append(error)
            else:
                out[key] = parsed
        elif key == "tags":
            if value is None:
                out[key] = []
            elif isinstance(value, (list, tuple)) and all(
                isinstance(t, str) for t in value
            ):
                out[key] = list(value)
            else:
                errors.append(f"{label} : une liste de textes est attendue.")
        elif key == "folder_id":
            if value in (None, ""):
                out[key] = None
            elif isinstance(value, str):
                out[key] = value
            else:
                errors.append(f"{label} invalide.")
        elif value is None:
            out[key] = ""
        elif isinstance(value, str):
            out[key] = value
        else:
            errors.append(f"{label} : un texte est attendu.")
    return out, errors


TEXT_CHEVRONS_ERROR = (
    "{label} : un passage entre chevrons serait retiré à l'enregistrement "
    "— retirez les chevrons."
)


def _text_errors(label: str, value: str, limit: int) -> list[str]:
    """Refuse what ``security.sanitize`` would alter — never alter it.

    The message names the chevrons in WORDS, never as « < … > »: a refusal
    travels on ``?erreur=`` (Réception, the document page), and those pages
    ``sanitize`` it before display — a literal ``<…>`` run in the message
    was itself stripped, leaving « (« ») » where the explanation stood.
    """
    if len(value) > limit:
        return [f"{label} : {limit} caractères au plus."]
    if sanitize(value, max_length=limit) != value:
        return [TEXT_CHEVRONS_ERROR.format(label=label)]
    return []


def _metadata_value_errors(fields: dict) -> list[str]:
    """Content rules on already-typed metadata (see :func:`_metadata_input`)."""
    errors: list[str] = []
    for key, value in fields.items():
        if key in _TEXT_LIMITS:
            errors += _text_errors(_FIELD_LABELS[key], value, _TEXT_LIMITS[key])
        elif key == "category":
            if value not in VALID_CATEGORIES:
                errors.append(CATEGORY_ERROR)
        elif key == "tags":
            label = _FIELD_LABELS["tags"]
            if len(value) > TAGS_MAX_ITEMS:
                errors.append(f"{label} : {TAGS_MAX_ITEMS} au plus.")
            tag_errors: list[str] = []
            for tag in value:
                if not tag:
                    tag_errors.append(
                        f"{label} : une étiquette vide n'est pas acceptée."
                    )
                else:
                    tag_errors += _text_errors(
                        f"{label} (chacune)", tag, TAG_MAX
                    )
            errors += list(dict.fromkeys(tag_errors))
    return errors


def _record_metadata(metadata: Optional[dict]) -> tuple[dict, list[str]]:
    """A new record's metadata: the whitelisted keys, typed, then checked."""
    fields, errors = _metadata_input(metadata or {}, _RECORD_METADATA_KEYS)
    if not errors:
        errors = _metadata_value_errors(fields)
    return fields, errors


def record_metadata_errors(metadata: Optional[dict]) -> list[str]:
    """What a new record's *metadata* would be refused for — PURE.

    The exact rules :func:`_prepare_document_record` applies (shape, the
    refusal of what ``sanitize`` would alter, the category vocabulary), and
    nothing that needs I/O (the folder is checked at creation). The direct
    upload form asks it BEFORE opening the resumable session: refused only
    at finalization, the lawyer's display name or tags would cost the whole
    upload, the staging object being consumed on refusal.
    """
    return _record_metadata(metadata)[1]


def _portail_fields(portail: Optional[dict]) -> tuple[dict, list[str]]:
    """The portal provenance keyword, checked: the three keys, short texts."""
    if not portail:
        return {}, []
    if not isinstance(portail, dict) or set(portail) - set(_PORTAIL_KEYS):
        return {}, ["Provenance du portail invalide."]
    out: dict = {}
    for key in _PORTAIL_KEYS:
        value = portail.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > _PORTAIL_MAX:
            return {}, ["Provenance du portail invalide."]
        out[key] = value
    return out, []


# The link a GENERATED note d'honoraires keeps to its invoice (lot 3a, step
# 2): which invoice it renders, and the fingerprint of what it printed —
# ``services.note_honoraires`` finds an identical note again instead of
# filing a duplicate. Set by that service alone, through the
# ``generated_from_invoice`` keyword; never a metadata key a form can post.
GENERATION_FINGERPRINT_LENGTH = 64   # a SHA-256, lowercase hex
_GENERATED_FROM_INVOICE_ERROR = "Provenance de génération invalide."


def _generated_from_invoice_fields(
    link: Optional[dict],
) -> tuple[dict, list[str]]:
    """``{source_invoice_id, generation_fingerprint}`` from the keyword,
    checked: exactly the two keys, an addressable invoice id, a SHA-256."""
    if link is None:
        return {}, []
    if not isinstance(link, dict) or set(link) != {"invoice_id", "fingerprint"}:
        return {}, [_GENERATED_FROM_INVOICE_ERROR]
    invoice_id = link.get("invoice_id")
    fingerprint = link.get("fingerprint")
    if (
        not is_addressable_id(invoice_id)
        or not isinstance(fingerprint, str)
        or len(fingerprint) != GENERATION_FINGERPRINT_LENGTH
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
    ):
        return {}, [_GENERATED_FROM_INVOICE_ERROR]
    return {"source_invoice_id": invoice_id,
            "generation_fingerprint": fingerprint}, []


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


def _storage_uid(user_id: object) -> tuple[Optional[str], list[str]]:
    """The uid a Storage path may be written under, or a French refusal.

    ``utils.storage_identity.require_uid`` decides; this only turns its
    exception into the model convention ``(value, errors)``.
    """
    try:
        return storage_identity.require_uid(user_id), []
    except storage_identity.StorageIdentityUnavailable as exc:
        return None, [str(exc)]


def _prepare_document_record(
    dossier_id: str,
    dossier_file_number: str,
    filename: str,
    ext: str,
    content_type: str,
    file_size: int,
    metadata: dict,
    user_id: str,
    *,
    document_id: Optional[str] = None,
    category_source: str = "juriste",
    portail: Optional[dict] = None,
    analyse_seed: Optional[dict] = None,
    lawyer_set_category: bool = False,
    generated_from_invoice: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Build the Firestore record + storage path shared by the two
    ingestion paths (through-app stream and GCS-side copy).

    Validates the metadata (folder, category) and sanitizes the filename
    used in the Storage path — the raw client name is kept ONLY in
    original_filename/display_name (display purposes). The uid segment is
    validated here too, where the path is built, whoever computed it: the
    two public callers check it first as well, to refuse before any I/O.

    *metadata* is read through a WHITELIST (``_RECORD_METADATA_KEYS``) and
    refused rather than mangled; everything else a record carries is set
    here or by an explicit keyword:

    * ``document_id`` — a RESERVED id (a canonical UUIDv4, refused
      otherwise), so a retried ingestion lands on the same record instead
      of minting a second one. ``None`` mints a fresh one.
    * ``category_source`` — who posed the category: « juriste » (a form,
      a template's own category) or « mcp » (Claude — shown « présumée »).
    * ``portail`` — the three ``portail_*`` provenance fields of a document
      versed from Réception.
    * ``analyse_seed`` — the PROTECTION a copy inherits from its source
      (:func:`protection_seed`, lot 2A T8): stored as the record's
      ``analyse`` cache, never a qualification (no sub-nature — the
      category stays the caller's). Refused unless it is exactly that shape.
    * ``lawyer_set_category`` — D18: the lawyer CHOSE this category (an
      upload form moved off its default, a copy of his choice). Stored as
      the marker :func:`category_set_by_lawyer` reads; never True beside a
      source other than « juriste ».
    * ``generated_from_invoice`` — ``{"invoice_id", "fingerprint"}``: the
      invoice a generated note d'honoraires renders and the SHA-256 of what
      it printed (``services.note_honoraires``, lot 3a), stored as
      ``source_invoice_id`` / ``generation_fingerprint`` and read back by
      :func:`find_generated_for_invoice`. Absent on every other record.
    """
    uid, uid_errors = _storage_uid(user_id)
    if uid_errors:
        return None, uid_errors
    if document_id is not None and not is_canonical_uuid4(document_id):
        return None, [INVALID_DOCUMENT_ID]
    if category_source not in _POSABLE_CATEGORY_SOURCES:
        return None, ["Provenance de catégorie invalide."]
    if analyse_seed is not None and not _is_protection_seed(analyse_seed):
        return None, [PROTECTION_SEED_ERROR]
    fields, errors = _record_metadata(metadata)
    portail_fields, portail_errors = _portail_fields(portail)
    errors += portail_errors
    invoice_fields, invoice_errors = _generated_from_invoice_fields(
        generated_from_invoice)
    errors += invoice_errors
    if errors:
        return None, errors

    merged = {**_default_doc(), **fields, **portail_fields, **invoice_fields}
    merged["dossier_id"] = dossier_id
    merged["dossier_file_number"] = dossier_file_number
    merged["category_source"] = category_source
    merged["category_set_by_lawyer"] = (
        bool(lawyer_set_category) and category_source == "juriste")
    if analyse_seed is not None:
        merged["analyse"] = dict(analyse_seed)

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
    document_id = document_id or str(uuid.uuid4())

    printable = "".join(ch for ch in filename if ch.isprintable())
    safe_filename = secure_filename(printable)
    if len(safe_filename) > 200:
        safe_filename = safe_filename[: 200 - len(ext)] + ext
    if not safe_filename or not safe_filename.lower().endswith(ext):
        safe_filename = "document" + ext
    storage_path = f"users/{uid}/dossiers/{dossier_id}/documents/{document_id}/{safe_filename}"

    merged.update({
        "id": document_id,
        "filename": safe_filename,
        "original_filename": filename,
        "display_name": merged.get("display_name") or filename.rsplit(".", 1)[0],
        "file_type": content_type,
        "file_size": file_size,
        "storage_path": storage_path,
    })
    provenance.stamp_create(merged, now)
    return merged, []


def ingest_blob_as_document(
    source_blob,
    dossier_id: str,
    dossier_file_number: str,
    filename: str,
    metadata: dict,
    user_id: str,
    *,
    document_id: Optional[str] = None,
    category_source: str = "juriste",
    portail: Optional[dict] = None,
    analyse_seed: Optional[dict] = None,
    lawyer_set_category: bool = False,
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

    ``document_id`` (keyword) RESERVES the record's id — a canonical
    UUIDv4, refused otherwise. The upload form passes the id segment of its
    staging path, so two finalizations of one upload (a double click, a
    retried request whose answer was lost) land on ONE document. What makes
    that safe is structural, never a pre-read someone can race:

    * the copy is written with ``if_generation_match=0`` — it can only
      CREATE the destination object. A 412 means another call already wrote
      it: this call then adopts it (same size, same checksum as the source)
      or refuses — it never overwrites it and never deletes it;
    * the record is written with ``create()`` — ``AlreadyExists`` means
      another call committed it: the existing record is returned when it is
      the same document (same dossier, same size), refused otherwise;
    * a failure deletes the destination ONLY when this call created it
      (the generation it wrote), and never when a committed record
      references it. The old rollback deleted the canonical path blindly —
      with a reserved id that path can belong to a call that already
      committed, whose document would have lost its bytes in silence.

    ``analyse_seed`` (keyword, lot 2A T8) — a copy's inherited protection
    (:func:`copy_document`): the record carries it as its ``analyse`` cache,
    and its write-once journal entry (``analyses/{analyse_id}``) is written
    in the SAME commit as the record — a copy of a protected document never
    exists, even for an instant, without the level it inherits.

    Every path that RETURNS a record notes the commit
    (``provenance.note_commit``): the record exists, so a failure after it
    in the caller is « ENREGISTRÉE — NE PAS RÉESSAYER », never a refusal a
    retry would answer with a second document.
    """
    _, uid_errors = _storage_uid(user_id)
    if uid_errors:
        return None, uid_errors
    if document_id is not None and not is_canonical_uuid4(document_id):
        return None, [INVALID_DOCUMENT_ID]
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
        return None, [_INGEST_READ_FAILED]
    content_type = _sniff_header(header, ext)
    if not content_type or content_type not in ALLOWED_MIME_TYPES:
        return None, [
            "Le contenu du fichier ne correspond à aucun format autorisé. "
            "Formats acceptés : PDF, Word (DOC/DOCX), Excel (XLS/XLSX), "
            "JPG, PNG, TIFF, ZIP, courriels (EML/MSG)."
        ]
    if EXTENSION_MIME_TYPES.get(ext) != content_type:
        return None, ["Le contenu du fichier ne correspond pas à son extension."]

    size = int(source_blob.size or 0)
    reserved = document_id is not None
    merged, errors = _prepare_document_record(
        dossier_id, dossier_file_number, filename, ext,
        content_type, size, metadata, user_id,
        document_id=document_id, category_source=category_source,
        portail=portail, analyse_seed=analyse_seed,
        lawyer_set_category=lawyer_set_category,
    )
    if errors:
        return None, errors
    storage_path = merged["storage_path"]
    document_id = merged["id"]
    ref = db.collection(COLLECTION).document(document_id)

    if reserved:
        # A replay whose first attempt FINISHED: answer it, copy nothing.
        # Fails closed — an unreadable record is not « no record ».
        try:
            snap = ref.get()
        except Exception:
            log_unexpected("document ingest: reserved record unreadable",
                           document_id=document_id)
            return None, [_INGEST_FAILED]
        if snap.exists:
            return _noted(_same_ingested(snap.to_dict() or {}, dossier_id, size))

    created_generation = None
    try:
        bucket = storage.bucket()
        dest = bucket.blob(storage_path)
        # GCS-side rewrite (loops for large objects), CREATE-ONLY: the same
        # precondition on every call of the loop (GCS requires the
        # continuation calls to repeat the first one's parameters).
        try:
            token, _, _ = dest.rewrite(source_blob, if_generation_match=0)
            while token is not None:
                token, _, _ = dest.rewrite(
                    source_blob, token=token, if_generation_match=0
                )
        except PreconditionFailed:
            # Someone else wrote this path: never overwrite it, never
            # delete it. Already ingested (a concurrent finalization of the
            # same upload committed) → its document is the answer. No record
            # yet (that call is still between its copy and its record, or
            # died there) → adopt the object only when it IS this source's
            # bytes; the record's create() below settles who wins.
            if reserved:
                done = _read_ingested(ref, dossier_id, size)
                if done is not None:
                    return _noted(done)
            if not _same_object(dest, source_blob):
                return None, [_INGEST_OCCUPIED]
        else:
            created_generation = dest.generation
        # The destination gets the SNIFFED type — never the source's
        # client-declared one — and the attachment discipline of the
        # non-previewable types; the precondition keeps the patch on the
        # generation this call wrote or adopted.
        dest.content_type = content_type
        if content_type in _ATTACHMENT_ONLY_TYPES:
            dest.content_disposition = "attachment"
        dest.patch(if_generation_match=dest.generation)
    except Exception as exc:
        logger.warning(
            "ingest_blob failed for document %s: %s",
            document_id, type(exc).__name__,
        )
        # A concurrent finalization may have ADOPTED this very generation
        # (its rewrite answered 412, the bytes were the same) and committed
        # its record before this call's patch failed.
        _delete_own_object_unreferenced(ref, storage_path, created_generation)
        if reserved:
            # The concurrent call that consumed the staging object may have
            # committed meanwhile — then this call's answer is its document.
            done = _read_ingested(ref, dossier_id, size)
            if done is not None:
                return _noted(done)
        return None, [_INGEST_FAILED]

    try:
        if analyse_seed is None:
            ref.create(merged)
        else:
            # The record and its seed's journal entry: ONE commit (a batch's
            # create() carries the same « must not exist » precondition, and
            # answers AlreadyExists the same way).
            batch = db.batch()
            batch.create(ref, merged)
            batch.set(
                ref.collection(ANALYSES_SUBCOLLECTION).document(
                    merged["analyse"]["analyse_id"]),
                merged["analyse"],
            )
            batch.commit()
    except AlreadyExists:
        # Another call committed this id. Its record is the answer when it
        # is the same document; this call's own copy is removed only when
        # no committed record points at it.
        try:
            existing = ref.get().to_dict() or {}
        except Exception:
            log_unexpected("document ingest: committed record unreadable",
                           document_id=document_id)
            return None, [_INGEST_FAILED]
        if existing.get("storage_path") != storage_path:
            _delete_own_object(storage_path, created_generation)
        return _noted(_same_ingested(existing, dossier_id, size))
    except Exception as exc:
        logger.warning(
            "ingest_blob failed for document %s: %s",
            document_id, type(exc).__name__,
        )
        # An error on a write does not prove it did not land: the commit
        # may have gone through and only its answer been lost. The record
        # then points at this copy, which must outlive the failure — and it
        # IS this call's answer (reserved or fresh, the id is this upload's).
        _delete_own_object_unreferenced(ref, storage_path, created_generation)
        done = _read_ingested(ref, dossier_id, size)
        if done is not None:
            return _noted(done)
        return None, [_INGEST_FAILED]

    provenance.note_commit(COLLECTION, document_id)
    return merged, []


def _noted(result: tuple[Optional[dict], list[str]]) -> tuple[Optional[dict], list[str]]:
    """*result*, with its commit noted when it carries a record.

    The ingestion paths that ANSWER with an already-committed record (a
    replay, a concurrent twin, a commit whose answer was lost): the record
    exists, so a failure after it in the caller must read as committed."""
    doc, _errors = result
    if doc is not None and doc.get("id"):
        provenance.note_commit(COLLECTION, doc["id"])
    return result


_INGEST_FAILED = "Erreur lors du versement. Veuillez réessayer."
_INGEST_READ_FAILED = "Lecture du fichier source impossible. Réessayez."
# The refusals of `ingest_blob_as_document` that say « the store failed,
# nothing was filed, the SAME call may succeed » — as opposed to a refusal
# of the content itself (its type, its size, its metadata, an id already
# taken). The connector's upload ticket keeps its bytes for a retry on the
# former and consumes them on the latter (lot 2A, T9): consuming 200 MB on a
# store blip would cost the caller the whole upload.
RETRYABLE_INGEST_ERRORS = (_INGEST_FAILED, _INGEST_READ_FAILED)
_INGEST_OCCUPIED = (
    "Un autre fichier occupe déjà l'emplacement de ce document : rien n'a "
    "été versé, et rien n'a été supprimé. Téléversez le fichier de nouveau."
)
_INGEST_CONFLICT = (
    "Un autre document porte déjà cet identifiant : rien n'a été versé."
)


def _same_ingested(
    existing: dict, dossier_id: str, size: int
) -> tuple[Optional[dict], list[str]]:
    """The committed record, when it is this ingestion's document."""
    if (existing.get("dossier_id") == dossier_id
            and int(existing.get("file_size") or 0) == size):
        return _migrate_category(existing), []
    return None, [_INGEST_CONFLICT]


def _read_ingested(ref, dossier_id: str, size: int):
    """``(record, [])`` when a matching record exists now, else ``None``."""
    try:
        snap = ref.get()
    except Exception:
        return None
    if not snap.exists:
        return None
    doc, errors = _same_ingested(snap.to_dict() or {}, dossier_id, size)
    return (doc, errors) if doc is not None else None


def _same_object(dest, source) -> bool:
    """True when *dest* (an existing object) holds exactly *source*'s bytes.

    Size, then a stored checksum both sides carry (MD5, else CRC32C —
    a rewrite preserves both); no common checksum is « not proven ».
    """
    import hmac

    try:
        dest.reload()
    except Exception:
        return False
    if dest.size is None or int(dest.size) != int(source.size or 0):
        return False
    for attr in ("md5_hash", "crc32c"):
        mine, theirs = getattr(dest, attr, None), getattr(source, attr, None)
        if isinstance(mine, str) and isinstance(theirs, str) and mine and theirs:
            return hmac.compare_digest(mine, theirs)
    return False


def _delete_own_object_unreferenced(ref, storage_path: str, generation) -> None:
    """:func:`_delete_own_object`, unless a committed record points at it.

    The generation guard proves the object is the one THIS call wrote — not
    that nobody depends on it. Two failure branches can leave a committed
    record behind this call's copy: a concurrent finalization that adopted
    the generation and committed before this call's patch failed, and this
    call's own ``create()`` whose commit landed while its answer was lost.
    Deleting there left a document without its bytes, in silence. An
    unreadable record keeps the object (fail closed): an orphan under the
    canonical prefix costs storage, a record without bytes costs a document.
    """
    if generation is None:
        return
    try:
        snap = ref.get()
    except Exception:
        log_unexpected("document ingest: rollback check unreadable",
                       document_id=ref.id)
        return
    if snap.exists and (snap.to_dict() or {}).get("storage_path") == storage_path:
        return
    _delete_own_object(storage_path, generation)


def _delete_own_object(storage_path: str, generation) -> None:
    """Delete the object at *storage_path* ONLY at the generation this call
    wrote. ``None`` — this call wrote nothing there — deletes nothing."""
    if generation is None:
        return
    try:
        storage.bucket().blob(storage_path).delete(
            if_generation_match=generation
        )
    except (NotFound, PreconditionFailed):
        pass
    except Exception:
        log_unexpected("document ingest: own copy rollback failed")


def upload_document(
    dossier_id: str,
    dossier_file_number: str,
    file_stream,
    filename: str,
    file_size: int,
    metadata: dict,
    user_id: str,
    *,
    category_source: str = "juriste",
    generated_from_invoice: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Upload a file to Firebase Storage and create a Firestore record.

    Returns (doc, errors). *metadata* is whitelisted and refused rather
    than mangled (see ``_prepare_document_record``); ``category_source``
    says who chose the category — « juriste » for the application's own
    generators (the template's category), « mcp » when Claude chose it;
    ``generated_from_invoice`` links a generated note d'honoraires to its
    invoice (see ``_prepare_document_record``).
    """
    _, uid_errors = _storage_uid(user_id)
    if uid_errors:
        return None, uid_errors
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
        category_source=category_source,
        generated_from_invoice=generated_from_invoice,
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

    # Save metadata to Firestore — create(): a fresh id can only be CREATED.
    ref = db.collection(COLLECTION).document(document_id)
    try:
        ref.create(merged)
    except Exception as exc:
        logger.warning("upload_document failed for document %s: %s", document_id, type(exc).__name__)
        # The id is fresh, so a record holding it can only be THIS call's —
        # committed by an attempt whose answer was lost: the client retries
        # a commit on ServiceUnavailable, and a retried create() answers
        # AlreadyExists (revue de T1: with the old set() the retry simply
        # rewrote the same record). Deleting the file then would leave the
        # committed document without its bytes. Its record is the answer;
        # the file is removed only when no record points at it.
        _delete_own_object_unreferenced(ref, storage_path, blob.generation)
        done = _read_ingested(ref, dossier_id, file_size)
        if done is not None:
            return _noted(done)
        return None, ["Erreur lors du téléversement. Veuillez réessayer."]

    # The commit point (lot 2A, T8): the connector's generations reach this
    # writer, and a failure AFTER it (the payload, the audit line) must be
    # « ENREGISTRÉE — NE PAS RÉESSAYER », never a refusal a retry answers
    # with a second document.
    provenance.note_commit(COLLECTION, document_id)
    return merged, []


# Firestore's own ceiling on a document id (« Must be no longer than 1,500
# bytes »).
_FIRESTORE_ID_MAX_BYTES = 1500


def is_addressable_id(value: object) -> bool:
    """True when *value* can name ONE top-level record: a non-empty string
    with no « / », and an id Firestore itself accepts.

    The Firestore client joins then re-splits a document id on « / », so
    ``document("{id}/analyses/{analyse_id}")`` addresses a record DEEPER in
    the tree — an entry of a document's write-once analysis journal read as
    if it were the document, and a partial update staged ONTO it (lot 2A,
    T7: the connector hands ids through verbatim). No id this application
    mints carries a slash (UUIDv4, or the UUIDv5 of a system folder), so a
    slashed id is an ABSENCE, never a path. The web routes never reach this
    (Flask's default converter stops at « / »).

    Review of T7: the ids the SERVER refuses — « . », « .. », anything
    matching ``__.*__``, more than 1 500 bytes (Firestore's documented id
    rules) — are absences too. The client builds such a reference without
    complaint and the RPC fails: the strict reader then reported « lecture
    impossible — réessayez » for an id no retry will ever read, and ONE such
    id in a bulk move sank the whole batch as a store failure. Tested by
    hand, no regex: the id is the caller's string."""
    if not isinstance(value, str) or not value or "/" in value:
        return False
    if value in (".", ".."):
        return False
    if len(value) >= 4 and value.startswith("__") and value.endswith("__"):
        return False
    try:
        encoded = value.encode("utf-8")   # a lone surrogate is not UTF-8
    except UnicodeEncodeError:
        return False
    return len(encoded) <= _FIRESTORE_ID_MAX_BYTES


def get_document_strict(document_id: str) -> Optional[dict]:
    """Fetch a single document by ID; a read failure PROPAGATES.

    ``None`` means the store answered « no such document » (or the id could
    not name one, :func:`is_addressable_id`) — never « the read failed ». For
    a caller about to WRITE on the strength of the answer (the connector's
    edits), for which « introuvable » on a transient error would send the
    caller hunting for an id that was right all along. Read-migrated like
    :func:`get_document`.
    """
    if not is_addressable_id(document_id):
        return None
    doc = db.collection(COLLECTION).document(document_id).get()
    if doc.exists:
        return _migrate_category(doc.to_dict() or {})
    return None


def get_document(document_id: str) -> Optional[dict]:
    """Fetch a single document metadata by ID.

    An id that cannot name one document (:func:`is_addressable_id`) is an
    absence, never read (review of T7): a slashed id reached a record
    DEEPER in the tree, and the connector's ``get_document_text`` answered
    ``found: true`` for an entry of a document's analysis journal."""
    if not is_addressable_id(document_id):
        return None
    try:
        doc = db.collection(COLLECTION).document(document_id).get()
        if doc.exists:
            return _migrate_category(doc.to_dict())
    except Exception as exc:
        logger.warning("get_document failed for %s: %s", sanitize_log_value(document_id), exc)
    return None


def find_generated_for_invoice(invoice_id: str) -> list[dict]:
    """The documents generated from *invoice_id* — its notes d'honoraires
    (``source_invoice_id``, lot 3a) —, newest first. A read failure
    PROPAGATES: the caller decides whether to FILE a new note on the
    answer, and « none » on a transient error is a duplicate.

    A single-field equality, served by the automatic index. Only notes
    generated since lot 3a carry the field; an older one is not found.
    """
    if not is_addressable_id(invoice_id):
        return []
    rows = [
        _migrate_category(snap.to_dict() or {})
        for snap in db.collection(COLLECTION)
        .where(filter=FieldFilter("source_invoice_id", "==", invoice_id))
        .stream()
    ]
    rows.sort(
        key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return rows


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
                    # Les DEUX textes du document, plus la provenance —
                    # « la note d'honoraires de la facture 2026-003 » se
                    # retrouve encore par son numéro.
                    (d.get("analyse") or {}).get("resume", ""),
                    d.get("notes_internes", ""),
                    d.get("genere_depuis", ""),
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


def _resolve_category_source(source: Optional[str]) -> tuple[str, list[str]]:
    """Who poses a category on an EDIT — never « analyse » (see above).

    ``None`` derives it from the writer: the connector is « mcp », every
    other path « juriste ». And under the connector the answer is « mcp »
    WHATEVER the caller passed: a category Claude chose must never read as
    the lawyer's determination, and a handler that forgot the keyword (or
    passed the wrong one) must not be able to make it so.
    """
    if source is not None and source not in _POSABLE_CATEGORY_SOURCES:
        return "", ["Provenance de catégorie invalide."]
    if provenance.current_via() == "mcp":
        return "mcp", []
    return source or "juriste", []


def _changed_metadata(existing: dict, proposed: dict) -> dict:
    """The proposed values that differ from what is stored (read-migrated,
    absent keys at their default) — the ONLY keys an edit writes."""
    defaults = _default_doc()
    return {
        key: value for key, value in proposed.items()
        if existing.get(key, defaults.get(key)) != value
    }


TARGET_FOLDER_NOT_FOUND = "Le dossier de destination est introuvable."


def _check_target_folder(transaction, dossier_id: str, target: Optional[str]) -> None:
    """Refuse (:class:`_Refused`) unless *target* is a folder of
    *dossier_id* — read THROUGH *transaction*, so a folder deleted between
    this read and the commit aborts the write (the real client re-runs the
    body, which then refuses) instead of filing the document under a dead
    ``folder_id`` — the document then shows in no folder and at no root.
    ``None`` (the dossier root) needs no read. Call it before any staged
    write (reads first)."""
    if target is None:
        return
    from models import folder as folder_model

    if not is_addressable_id(target):
        raise _Refused([TARGET_FOLDER_NOT_FOUND])
    snap = db.collection(folder_model.COLLECTION).document(target).get(
        transaction=transaction)
    if not snap.exists or (snap.to_dict() or {}).get("dossier_id") != dossier_id:
        raise _Refused([TARGET_FOLDER_NOT_FOUND])


def update_metadata(
    document_id: str,
    data: dict,
    *,
    expected_etag: Optional[str] = None,
    source: Optional[str] = None,
    folder_id: object = _UNSET,
) -> tuple[Optional[dict], list[str], bool]:
    """Update document metadata — a partial, transactional write.

    Returns ``(doc, errors, changed)``: the third member says whether
    anything was written (the ``set_time_entry_phase`` deviation from the
    CRUD pair) — a save that changes nothing writes nothing at all, no
    ``updated_at`` churn and no new etag.

    Rewritten in lot 2A (T1, 2026-09-27). It used to read the document,
    merge ``{**existing, **data}`` and ``set()`` the WHOLE document. Four
    defects followed:

    * a concurrent write landing between the read and the ``set()`` — an
      analysis the connector recorded, a folder move — was reverted by it;
      now the read, the checks and the write share ONE transaction, and
      only the CHANGED keys are written (``update()``), so a write to any
      other field survives by construction;
    * every save stamped ``category_source = « juriste »`` because the
      edit form always posts ``category``: an innocuous tag edit silently
      turned a PRESUMED category into the lawyer's determination. The
      provenance now moves only when the category VALUE changes;
    * a malformed ``document_date`` ERASED the stored date; it is refused;
    * over-long values and ``<…>`` runs were truncated or deleted by
      ``sanitize``; they are refused with a French message.

    ``expected_etag`` (keyword-only): the write commits only if the stored
    etag is still that one (``models.concurrency`` — checked on the
    transactional read, before any validation); a stale one returns
    ``[STALE_ETAG_ERROR]`` and writes nothing. ``None`` asserts nothing
    about the version — but the write stays transactional and partial.

    ``source`` (keyword-only) — who poses a changed category: « juriste »
    or « mcp » (D15: Claude's category is PRESUMED, shown « présumée » with
    a « Confirmer » button). ``None`` derives it from the writer, and the
    connector is always « mcp » (:func:`_resolve_category_source`). A
    category change posed by « mcp » is REFUSED on an analysed document,
    whose category derives from its analysis — and (D18) on a category the
    lawyer chose or confirmed (:func:`category_set_by_lawyer`). A category
    change stamps the marker: True under « juriste » (the web edit form),
    False under « mcp ».

    Values are validated only where they CHANGE: a legacy value the form
    posts back untouched is not a new write, and must not block the save of
    another field. The returned document carries the NEW etag: a caller
    chaining ``update_analyse`` on the same save passes that one.

    ``folder_id`` (keyword-only, lot 2A T7) — ALSO move the document, in the
    SAME transaction: ``None`` or ``""`` is the dossier root, a string a
    folder of the document's OWN dossier (read through the transaction —
    :func:`_check_target_folder`). Omitted, the folder is left alone. It is
    a keyword and never a key of *data* on purpose: the web edit form's
    *data* cannot move a document (a ``folder_id`` in it is ignored like any
    key outside the whitelist), and a caller that renames AND refiles in one
    call gets one etag check and one commit — never a metadata write that
    lands while the move that was to follow it is refused.
    """
    if not is_addressable_id(document_id):
        return None, ["Document introuvable."], False
    source, errors = _resolve_category_source(source)
    if errors:
        return None, errors, False
    proposed, errors = _metadata_input(data or {}, _METADATA_EDIT_KEYS)
    if errors:
        return None, errors, False
    moving = folder_id is not _UNSET
    target = (folder_id or None) if moving else None
    if moving and target is not None and not isinstance(target, str):
        return None, [TARGET_FOLDER_NOT_FOUND], False
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool]:
        # The read comes FIRST (the real client refuses a transactional read
        # after a staged write), and every check below reads THIS snapshot.
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        existing = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        changes = _changed_metadata(existing, proposed)
        value_errors = _metadata_value_errors(changes)
        if value_errors:
            raise _Refused(value_errors)
        if moving and (existing.get("folder_id") or None) != target:
            # A read, still before any staged write.
            _check_target_folder(
                transaction, existing.get("dossier_id") or "", target)
            changes["folder_id"] = target
        if "category" in changes:
            if source == "mcp" and has_analysis(existing):
                raise _Refused([MCP_CATEGORY_ON_ANALYSED])
            if source == "mcp" and category_set_by_lawyer(existing):
                # D18 — judged on THIS transactional read: a lawyer's
                # choice landing between the connector's read and its
                # commit aborts the commit, and the re-run refuses.
                raise _Refused([MCP_CATEGORY_ON_LAWYERS])
            changes["category_source"] = source
            changes["category_set_by_lawyer"] = source == "juriste"
        if not changes:
            return existing, False
        fields = {
            **changes,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, fields)
        return {**existing, **fields}, True

    try:
        doc, changed = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("document write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], False
    if changed:
        provenance.note_commit(COLLECTION, document_id)
    return doc, [], changed


def confirmer_categorie(
    document_id: str, par: str, *, expected_etag: Optional[str] = None
) -> tuple[Optional[dict], list[str]]:
    """The lawyer confirms a category Claude posed (D15) — the ONLY path
    turning « mcp » (présumée) into « juriste ». Web-only, like
    :func:`confirmer_analyse`; a source sweep pins its single caller.

    Confirming says « I read THIS version »: ``expected_etag`` is the etag
    of the page the button was on (``None`` for a page rendered before the
    button carried it — no check). A partial update of the provenance and
    its stamp, read and written in one transaction; nothing else moves.
    A category presumed by an ANALYSIS is confirmed with the analysis
    (:func:`confirmer_analyse`), never here.
    """
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        existing = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        origin = existing.get("category_source") or "juriste"
        if origin == "analyse":
            raise _Refused([
                "Cette catégorie vient de l'analyse du document : confirmez "
                "l'analyse elle-même."
            ])
        if origin != "mcp":
            raise _Refused(["Aucune catégorie présumée à confirmer."])
        now = datetime.now(timezone.utc)
        fields = {
            "category_source": "juriste",
            # D18: from now on the category is the lawyer's — the
            # connector can no longer replace it.
            "category_set_by_lawyer": True,
            "category_confirmed_by": sanitize(str(par or ""), max_length=200),
            "category_confirmed_at": now,
            **provenance.update_fields(now),
        }
        transaction.update(ref, fields)
        return {**existing, **fields}

    try:
        doc = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("document category confirm failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, document_id)
    return doc, []


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

    uid, uid_errors = _storage_uid(user_id)
    if uid_errors:
        return None, uid_errors
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
    zip_path = f"staging/{uid}/exports/{uuid.uuid4()}/{zip_name}"

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
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """Move a document to a different folder. Returns ``(doc, errors, changed)``.

    Lot 2A (T1, 2026-09-27): a partial ``update()`` of ``folder_id`` and its
    stamp, read and written in ONE transaction — the document AND the
    target folder are read through it. It used to ``set()`` the whole
    document from a copy read beforehand, so an analysis recorded or a
    metadata edit saved in between was silently reverted by the move. A move
    to the folder the document is already in writes NOTHING (it used to
    mint a fresh etag, turning every open edit tab stale for no change).

    ``expected_etag`` (keyword-only): see ``models.concurrency``; ``None``
    asserts nothing about the version (a move cannot lose another field's
    edit any more — it writes one key).
    """
    target = target_folder_id or None
    if not is_addressable_id(document_id):
        return None, ["Document introuvable."], False
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        doc = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(doc, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        if doc.get("dossier_id") != dossier_id:
            raise _Refused(["Le document n'appartient pas à ce dossier."])
        _check_target_folder(transaction, dossier_id, target)
        if (doc.get("folder_id") or None) == target:
            return doc, False
        fields = {
            "folder_id": target,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, fields)
        return {**doc, **fields}, True

    try:
        doc, changed = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("document move failed")
        return None, ["Erreur lors du déplacement. Veuillez réessayer."], False
    if changed:
        provenance.note_commit(COLLECTION, document_id)
    return doc, [], changed


# The per-row outcomes of move_documents_bulk.
MOVE_MOVED = "moved"
MOVE_UNCHANGED = "unchanged"
MOVE_REFUSED = "refused"
MOVE_NOT_FOUND = "Document introuvable."
MOVE_OTHER_DOSSIER = "Ce document appartient à un autre dossier."
MOVE_DUPLICATE_IDS = "Un même document est nommé deux fois dans la liste."
MOVE_BULK_MAX = 50


def move_documents_bulk(
    dossier_id: str,
    document_ids: list[str],
    target_folder_id: Optional[str],
) -> tuple[list[dict], list[str]]:
    """Move documents of ONE dossier into one folder — every row read, and
    every move written, in ONE transaction.

    Returns ``(rows, errors)``. *errors* non-empty refuses the WHOLE call and
    nothing is written: an unknown target folder, more than
    :data:`MOVE_BULK_MAX` ids, an id named twice, a store that cannot be
    read, a commit that failed. Otherwise *rows* holds one row per requested
    id, IN REQUEST ORDER: ``{"id", "outcome", "reason",
    "previous_folder_id", "doc"}`` — ``outcome`` ∈ ``moved`` / ``unchanged``
    (already filed there: nothing written for it) / ``refused`` (no such
    document, or a document of another dossier — ``reason`` says which,
    in French, without the id), ``doc`` the document as stored AFTER the
    call (``None`` on a refused row).

    Rewritten in lot 2A (T7, 2026-09-27) before the connector reached it.
    The old body read each document OUTSIDE any transaction through the
    fail-OPEN :func:`get_document` — a read error reported as « introuvable »
    while the other rows moved —, rewrote rows already in the target (a new
    etag for nothing), checked the target folder once, beforehand, so a
    folder deleted before the commit left documents under a dead
    ``folder_id`` (in no folder, at no root), and answered with loose
    strings. Now: the target folder and every document are read through the
    transaction (a concurrent write to any of them aborts the commit, and
    the real client re-runs the body on fresh data — a move never reverts
    another write, it writes ``folder_id`` and its stamp only), a read error
    refuses everything, and the moved rows commit together or not at all.
    """
    ids = [str(i) for i in (document_ids or [])]
    if len(ids) > MOVE_BULK_MAX:
        return [], [f"{MOVE_BULK_MAX} documents au plus par déplacement."]
    if len(set(ids)) != len(ids):
        return [], [MOVE_DUPLICATE_IDS]
    target = target_folder_id or None
    refs = {
        i: db.collection(COLLECTION).document(i)
        for i in ids if is_addressable_id(i)
    }

    @firestore.transactional
    def _apply(transaction) -> list[dict]:
        # Reads first: the target folder, then every document in ONE
        # batched read (``get_all`` answers in no particular order — keyed
        # back by id below, never zipped against the request).
        _check_target_folder(transaction, dossier_id, target)
        found: dict[str, dict] = {}
        if refs:
            for snap in transaction.get_all(list(refs.values())):
                if snap.exists:
                    found[snap.id] = _migrate_category(snap.to_dict() or {})
        rows: list[dict] = []
        moving: list[str] = []
        for doc_id in ids:
            doc = found.get(doc_id)
            row = {"id": doc_id, "outcome": MOVE_REFUSED, "reason": None,
                   "previous_folder_id": None, "doc": None}
            if doc is None:
                row["reason"] = MOVE_NOT_FOUND
            elif doc.get("dossier_id") != dossier_id:
                row["reason"] = MOVE_OTHER_DOSSIER
            else:
                previous = doc.get("folder_id") or None
                row.update(previous_folder_id=previous, doc=doc)
                if previous == target:
                    row["outcome"] = MOVE_UNCHANGED
                else:
                    row["outcome"] = MOVE_MOVED
                    moving.append(doc_id)
            rows.append(row)
        if moving:
            now = datetime.now(timezone.utc)
            by_id = {r["id"]: r for r in rows}
            for doc_id in moving:
                # One instant, one etag PER ROW: each document's etag is its
                # own concurrency token (update_fields mints a fresh uuid).
                fields = {"folder_id": target, **provenance.update_fields(now)}
                transaction.update(refs[doc_id], fields)
                by_id[doc_id]["doc"] = {**by_id[doc_id]["doc"], **fields}
        return rows

    try:
        rows = _apply(db.transaction())
    except _Refused as refusal:
        return [], refusal.errors
    except Exception:
        log_unexpected("document bulk move failed")
        return [], ["Erreur lors du déplacement. Rien n'a été déplacé — "
                    "réessayez."]
    for row in rows:
        if row["outcome"] == MOVE_MOVED:
            provenance.note_commit(COLLECTION, row["id"])
    return rows, []


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

# « mcp » (D15, lot 2A) : une catégorie posée par Claude HORS analyse —
# présumée, comme celle d'une analyse, jusqu'à `confirmer_categorie`.
VALID_CATEGORY_SOURCES = ("juriste", "analyse", "mcp")
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
    declenche_par: str = "mcp",
    modele: str = "",
    dossier: Optional[dict] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Enregistre une analyse : le cache, la catégorie dérivée, le journal.

    Rend le document mis à jour. N'ÉCRIT JAMAIS `confirme: true` — voir §7.
    Aucun `bump_ctag` : `documents` n'est pas exposée en DAV.

    Lot 2A (T1, 2026-09-27) : la lecture, la dérivation et l'écriture sont
    UNE transaction. Avant, le modèle lisait le document hors transaction
    puis faisait un `set()` du document ENTIER. Deux défauts en sortaient :

    * une écriture glissée entre les deux — une édition des métadonnées, un
      déplacement — était annulée par le `set()` ;
    * deux analyses PARALLÈLES du même document dérivaient chacune de la
      MÊME lecture périmée : niveau stocké nul, A écrit 3, B écrit 1 en
      dernier, et le niveau tombait de 3 à 1 SANS divergence — le plancher
      de non-déclassement contourné par les appels parallèles du
      connecteur lui-même. Désormais le commit du perdant avorte, et sa
      reprise dérive de nouveau AU-DESSUS du niveau que le gagnant a posé.

    Le journal (`set()` de l'entrée) et le cache (`update()` des seules
    clés que l'analyse possède) partent ensemble ou pas du tout.

    Un identifiant à barre oblique est une ABSENCE (lot 2A, T7 —
    :func:`is_addressable_id`) : le connecteur le transmet tel quel, et
    ``document("{id}/analyses/{a}")`` désignait une entrée du journal, sur
    laquelle ce modèle écrivait un cache et ouvrait un journal à elle.

    ``expected_etag`` (mot-clé, finitions contracts-6) : la version du
    document que l'appelant a LUE. La transaction ne protégeait que contre
    une autre analyse : une édition ou une confirmation de l'analyse par le
    juriste, glissée entre la lecture du texte par Claude et son
    enregistrement, était remplacée en silence. Une version dépassée rend
    ``[STALE_ETAG_ERROR]``, rien d'écrit ; ``None``, la règle d'avant.
    """
    if not is_addressable_id(document_id):
        return None, ["Document introuvable."]
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        existing = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])

        # La dérivation lit CETTE lecture — c'est elle que le plancher de
        # non-déclassement doit voir.
        champ, erreurs = _analyse_derivee(
            sortie, document=existing, dossier=dossier
        )
        if erreurs:
            raise _Refused(erreurs)

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
            # Ce que l'écrasement remplace. Sans cela la divergence de
            # classement — « l'un des deux signalements qui valent le plus »
            # — deviendrait inobservable, puisqu'il n'y a plus deux valeurs
            # à comparer.
            "date_document_precedente": existing.get("document_date"),
            "categorie_precedente": ancienne,
            "categorie_precedente_source": ancienne_source,
            "categorie_remplacee": bool(ancienne and ancienne != nouvelle),
            # L'avertissement n'est levé que si l'on écrase un choix HUMAIN.
            # Un « autre » posé par défaut au versement n'en mérite pas.
            "remplace_un_choix_du_juriste": bool(
                ancienne and ancienne != nouvelle
                and ancienne_source == "juriste"
            ),
        })

        # L'analyse alimente encore UN champ natif : la date lue devient la
        # date du document. Elle l'ÉCRASE (décision du praticien,
        # 2026-08-27), et l'ancienne valeur part au journal — rien ne
        # disparaît sans trace. Le résumé, lui, ne se recopie plus nulle
        # part : il EST le texte du modèle, et il vit dans `analyse.resume`.
        natifs: dict = {}
        if champ.get("date_document_str"):
            lue = _coerce_document_date(champ["date_document_str"])
            if lue is not None:
                natifs["document_date"] = lue

        fields = {
            "analyse": champ, "category": nouvelle,
            "category_source": "analyse", **natifs,
            **provenance.update_fields(now),
        }
        transaction.set(
            ref.collection(ANALYSES_SUBCOLLECTION).document(analyse_id), champ
        )
        transaction.update(ref, fields)
        return {**existing, **fields}

    try:
        merged = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("document analyse write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, document_id)
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
    document_id: str, champs: dict, *, par: str,
    expected_etag: Optional[str] = None,
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

    ``expected_etag`` (keyword-only): the journal entry AND the cache
    commit together, and only if the stored etag is still that one
    (``models.concurrency``); a stale one writes neither and returns
    ``[STALE_ETAG_ERROR]``. It is the guard that keeps a stale tab from
    rewriting, under the lawyer's name, an analysis recorded since it
    opened — and from LOWERING a protection level through the one path
    that can.

    Lot 2A (T1, 2026-09-27): read, validated and written in ONE transaction
    whatever the caller passes — the journal ``set()`` and a partial
    ``update()`` of the keys the analysis owns (``analyse``, ``category``,
    ``category_source`` and the stamp). The old full-document ``set()``
    from a copy read beforehand reverted any metadata edit or move landing
    in between; with ``None`` it now asserts nothing about the version, but
    it can no longer undo another field's write.
    """
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        existing = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        champ, erreurs = _analyse_editee(existing, champs, par=par)
        if erreurs:
            raise _Refused(erreurs)
        now = datetime.now(timezone.utc)
        champ.update({"genere_le": now, "confirme_le": now})
        fields = {
            "analyse": champ, "category": champ["nature_detectee"],
            "category_source": "juriste",
            # D18: the lawyer's edit IS his determination (« éditer vaut
            # confirmer ») — a copy of this document keeps it his.
            "category_set_by_lawyer": True,
            **provenance.update_fields(now),
        }
        transaction.set(
            ref.collection(ANALYSES_SUBCOLLECTION).document(champ["analyse_id"]),
            champ,
        )
        transaction.update(ref, fields)
        return {**existing, **fields}

    try:
        merged = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("document analyse edit failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, document_id)
    return merged, []


def _analyse_editee(
    existing: dict, champs: dict, *, par: str
) -> tuple[dict, list[str]]:
    """The analysis field after the lawyer's correction — PURE.

    Validates *champs* against the closed vocabularies and against the
    analysis already stored (Annexe C: the two proof axes are checked on
    the value that will be STORED), derives the nature and the family from
    the code, and stamps the journal fields — everything except the
    instant, which the writer adds.
    """
    from utils import analyse_protection as prot
    from utils import analyse_taxonomies as tax

    champ = dict(existing.get("analyse") or {})
    if not champ.get("sous_nature") and "sous_nature" not in champs:
        return {}, ["Aucune analyse à corriger."]

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
        return {}, erreurs

    champ.update(propose)

    # La nature et la famille ne sont jamais saisies : elles DÉRIVENT du
    # code, exactement comme sur le chemin du modèle. C'est ce qui rend
    # impossible d'inventer une catégorie, à la main comme autrement.
    sous_nature = str(champ.get("sous_nature") or "")
    nature = tax.nature_of(sous_nature)
    champ["nature_detectee"] = nature
    champ["famille"] = tax.famille_of(sous_nature)

    ancienne = str(existing.get("category") or "")
    champ.update({
        "declenche_par": "juriste",
        "modifie_par": sanitize(str(par or ""), max_length=200),
        "modele": "",
        "analyse_id": str(uuid.uuid4()),
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
    })
    return champ, []


def confirmer_analyse(
    document_id: str, par: str, *, expected_etag: Optional[str] = None
) -> tuple[Optional[dict], list[str]]:
    """Le SEUL chemin passant `confirme` à vrai (§7). Aucun automatisme.

    ``expected_etag`` (mot-clé) — l'etag de la page d'où le juriste a
    cliqué « Confirmer ». Confirmer, c'est dire « j'ai vu » : sans lui, un
    onglet ouvert AVANT que le connecteur ne réanalyse le document
    confirmait, sous le nom du juriste, une qualification qu'il n'avait
    jamais lue — un niveau de protection compris. Donné, l'écriture ne
    s'engage que contre cette version (``models.concurrency``, dans une
    transaction) ; périmé, rien n'est écrit et la liste d'erreurs porte
    ``STALE_ETAG_ERROR``. ``None`` (une page rendue avant que le bouton ne
    porte l'etag) n'affirme rien sur la version.

    Lot 2A (T1, 2026-09-27) : lecture et écriture dans UNE transaction, et
    l'écriture est un `update()` partiel de l'analyse, de la provenance et
    du tampon — l'ancien `set()` du document entier, fait d'une copie lue
    avant, annulait toute écriture glissée entre les deux.
    """
    ref = db.collection(COLLECTION).document(document_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Document introuvable."])
        existing = _migrate_category(snap.to_dict() or {})
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        champ = dict(existing.get("analyse") or {})
        if not champ.get("sous_nature"):
            raise _Refused(["Aucune analyse à confirmer."])
        now = datetime.now(timezone.utc)
        champ.update({"confirme": True,
                      "confirme_par": sanitize(str(par or ""), max_length=200),
                      "confirme_le": now})
        fields = {
            "analyse": champ,
            # La confirmation fait de la catégorie une détermination de
            # l'avocat : la mention « présumé » doit tomber avec elle.
            "category_source": "juriste",
            # D18 (revue des correctifs du lot 2A) : et le marqueur le dit,
            # sans quoi la COPIE de ce document (qui n'emporte pas l'analyse)
            # lisait sa catégorie comme un défaut que Claude peut remplacer.
            "category_set_by_lawyer": True,
            **provenance.update_fields(now),
        }
        transaction.update(ref, fields)
        return {**existing, **fields}

    try:
        merged = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("document analyse confirm failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, document_id)
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


# ── Copie d'un document (lot 2A, T8) ─────────────────────────────────────
#
# Une copie est un NOUVEAU document, dans le dossier de sa source — jamais
# ailleurs : copier la pièce privilégiée d'un client dans le dossier d'un
# autre, sans contrôle d'identifiants, est exactement la fuite que les
# gabarits (enregistrés ou tirés d'un document, contrôle des identifiants
# résiduels compris) existent pour éviter. La fonction ne prend donc AUCUN
# dossier cible : la règle est structurelle, pas une comparaison qu'un
# appelant pourrait sauter.
#
# Les octets ne transitent jamais par l'application : c'est une réécriture
# GCS (``ingest_blob_as_document``), vers un chemin neuf, en création seule
# (``if_generation_match=0``) — la source n'est ni réécrite, ni supprimée.
#
# La PROTECTION suit la copie. Le plancher de non-déclassement lit
# ``document["analyse"]["niveau_protection"]`` : une copie d'une lettre au
# secret professionnel qui n'emporterait pas ce niveau s'afficherait sans
# régime, et une analyse de la copie pourrait se poser PLUS BAS — la faute
# professionnelle que tout le module d'analyse protège (sous-protéger).
# D'où :func:`protection_seed` : le niveau et les privilèges de la source,
# PRÉSUMÉS (``confirme`` faux, ``declenche_par`` « copie »), sans
# sous-nature — une copie n'est pas qualifiée, elle hérite d'un plancher.

COPY_SOURCE_NOT_FOUND = "Document source introuvable : rien n'a été copié."
COPY_SOURCE_UNREADABLE = (
    "Lecture du document source impossible — réessayez. Rien n'a été copié."
)
COPY_FILE_MISSING = (
    "Le fichier du document source est introuvable dans le stockage : rien "
    "n'a été copié."
)
COPY_NAME_PREFIX = "Copie de "
PROTECTION_SEED_ERROR = "Protection héritée invalide : rien n'a été versé."
MOTIF_COPIE = (
    "Niveau repris du document source (copie) — présumé, à confirmer par "
    "le juriste."
)
_SEED_KEYS = frozenset({
    "niveau_protection", "privileges", "motifs_protection", "confirme",
    "confirme_par", "confirme_le", "declenche_par", "source_document_id",
    "genere_le", "analyse_id",
})


def _is_level(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value in (0, 1, 2, 3))


def protection_seed(source: dict, *, now: datetime) -> Optional[dict]:
    """The protection a copy of *source* inherits — PURE; ``None`` when the
    source carries no level (nothing to inherit, and « none » is never
    invented as « public »).

    The level and the privileges that found it, and nothing of the
    qualification: never the sub-nature, the category derivation or the
    extract — a copy is not qualified, it inherits a FLOOR. ``confirme`` is
    false, ``declenche_par`` « copie »: presumed, like every automatic
    path."""
    from utils import analyse_taxonomies as tax

    analyse = source.get("analyse") or {}
    level = analyse.get("niveau_protection")
    if not _is_level(level):
        return None
    privileges = list(dict.fromkeys(
        p for p in (analyse.get("privileges") or [])
        if isinstance(p, str) and p in tax.VALID_PRIVILEGES
    ))
    return {
        "niveau_protection": level,
        "privileges": privileges,
        "motifs_protection": [MOTIF_COPIE],
        "confirme": False,
        "confirme_par": None,
        "confirme_le": None,
        "declenche_par": "copie",
        "source_document_id": str(source.get("id") or ""),
        "genere_le": now,
        "analyse_id": str(uuid.uuid4()),
    }


def _is_protection_seed(seed: object) -> bool:
    """Exactly the shape :func:`protection_seed` builds — a creator must
    never be handed a QUALIFICATION through this door (a sub-nature would
    make the copy read « analysée », its category derived)."""
    from utils import analyse_taxonomies as tax

    if not isinstance(seed, dict) or set(seed) != _SEED_KEYS:
        return False
    privileges = seed.get("privileges")
    return (
        _is_level(seed.get("niveau_protection"))
        and isinstance(privileges, list)
        and all(isinstance(p, str) and p in tax.VALID_PRIVILEGES
                for p in privileges)
        and seed.get("confirme") is False
        and seed.get("declenche_par") == "copie"
        and is_canonical_uuid4(seed.get("analyse_id"))
    )


def copy_category_source(source: dict) -> str:
    """Who posed the COPY's category — PURE.

    « juriste » only when the source's was the lawyer's (or a legacy
    document, which reads as his); otherwise « mcp »: a category an
    analysis derived, or Claude presumed, stays PRESUMED on the copy — and
    never « analyse », which would claim a qualification the copy does not
    carry (its seed has no sub-nature)."""
    source_cs = str(source.get("category_source") or "juriste")
    return "juriste" if source_cs == "juriste" else "mcp"


def default_copy_name(source: dict) -> str:
    """« Copie de {nom} », cut to the display-name ceiling (a default the
    application builds, never a caller's value — cutting it alters nothing
    anyone typed)."""
    name = source.get("display_name") or source.get("filename") or "document"
    return (COPY_NAME_PREFIX + str(name))[:DISPLAY_NAME_MAX].rstrip()


def copy_document(
    source_id: str,
    *,
    user_id: str,
    folder_id: Optional[str] = None,
    display_name: Optional[str] = None,
    genere_depuis: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Copy stored document *source_id* into a NEW document of ITS dossier.

    Returns ``(copy, [])`` or ``(None, french_errors)``. *folder_id* is the
    target folder of that dossier (``None`` = its root — the caller
    resolves « Projets »); *display_name* defaults to
    :func:`default_copy_name`. The copy carries the source's category
    (:func:`copy_category_source` says who posed it), its document date and
    its protection (:func:`protection_seed`); never its notes internes (the
    lawyer's text about THE SOURCE), its tags, its analysis extract or its
    portal provenance. The source is read strictly — an outage is never
    « introuvable » — and its object reloaded (the size and checksums the
    copy is judged on).
    """
    _, uid_errors = _storage_uid(user_id)
    if uid_errors:
        return None, uid_errors
    if not is_addressable_id(source_id):
        return None, [COPY_SOURCE_NOT_FOUND]
    try:
        source = get_document_strict(source_id)
    except Exception:
        log_unexpected("document copy: source unreadable")
        return None, [COPY_SOURCE_UNREADABLE]
    if source is None:
        return None, [COPY_SOURCE_NOT_FOUND]
    storage_path = str(source.get("storage_path") or "")
    filename = str(source.get("filename") or "")
    if not storage_path or not filename:
        return None, [COPY_FILE_MISSING]
    try:
        blob = storage.bucket().blob(storage_path)
        blob.reload()
    except NotFound:
        return None, [COPY_FILE_MISSING]
    except Exception:
        log_unexpected("document copy: source object unreadable")
        return None, [COPY_SOURCE_UNREADABLE]

    metadata = {
        "display_name": (default_copy_name(source)
                         if display_name is None else display_name),
        "category": source.get("category") or "autre",
        "document_date": source.get("document_date"),
        "folder_id": folder_id,
        "genere_depuis": genere_depuis,
    }
    return ingest_blob_as_document(
        blob,
        str(source.get("dossier_id") or ""),
        str(source.get("dossier_file_number") or ""),
        filename,
        metadata,
        user_id,
        category_source=copy_category_source(source),
        analyse_seed=protection_seed(source, now=datetime.now(timezone.utc)),
        # D18: a copy of the lawyer's category is still his choice.
        lawyer_set_category=category_set_by_lawyer(source),
    )
