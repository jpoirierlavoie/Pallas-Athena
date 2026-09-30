"""Upload tickets — the connector's write-only door for external files
(plan lot 2A, step T5; decision D4).

The lot 2 tools ``begin_upload`` / ``finalize_upload`` let Claude bring a file
in from its code sandbox: ``begin`` opens a resumable GCS upload session (the
one documented exception to « no signed URL in tool output »), the sandbox
PUTs the bytes, ``finalize`` checks them and files them. This module is the
RECORD of that exchange — ``mcp_upload_tickets/{ticket_id}`` — and its state
machine. It holds everything bound at ``begin`` so ``finalize`` re-supplies
nothing, and it NEVER holds the session URL: not here, not in
``mcp_idempotency`` (``mcp.write_support``'s persist/rehydrate hooks).

What a ticket binds at creation (never re-read from the caller afterwards):

* the purpose — ``document`` (a file for a dossier) or ``gabarit`` (a
  template file, created or replacing one);
* the declared size and MD5 — ``finalize`` compares the uploaded object's
  own size and MD5 against them (``hmac.compare_digest``), which is what
  makes a leaked session URL useless for injection: any other bytes are
  refused and deleted;
* the metadata the document or template will carry, validated by the
  handler at ``begin``;
* a NEUTRAL staging object, ``staging/{uid}/mcp/{ticket_id}/upload{ext}``:
  under the owner's uid (plan rule 8 — ``require_uid``, never an
  « unknown » prefix), with no client file name in any path or URL, and
  shaped so neither web finalizer can file it — ``routes/documents.api_finaliser``
  and ``routes/admin_ledger.api_recu`` (the receipt finalizer) both accept
  only ``staging/{uid}/{uuid4}/{name}``, four segments, the third a canonical
  UUIDv4, and ``mcp/{ticket_id}/upload{ext}`` is five; swept by the
  canonical bucket's ``staging/`` 7-day lifecycle rule if nothing does;
* RESERVED ids, minted here: the document id (purpose ``document``) or the
  template id (purpose ``gabarit``, mode create). They are what make a
  stale claim safely RECLAIMABLE — a second finalizer files the bytes under
  the same id, so a crash between the filing and the ticket's completion
  can never produce a second document or template. A replacement has no id
  to reserve; its finalizer records the staged bytes' SHA-256 first
  (:func:`record_staged_digest`), so a reclaim can recognise a replacement
  that already landed.

The state machine::

    en_attente ──claim──▶ en_cours ──complete──▶ versé
        │   ▲                │   └──refuse────▶ refusé
        │   └──release───────┘
        └── past open_until, at the next claim ──▶ expiré

* ``open_until`` = creation + 1 h: after it, a claim marks the ticket
  ``expiré`` and refuses. Expiry is enforced HERE, in code, on every claim.
* A claim is transactional (``en_attente`` → ``en_cours``, a fresh
  ``claim_id`` and ``claimed_at``). An ``en_cours`` claim younger than
  :data:`STALE_CLAIM_AFTER` refuses a second finalizer (« en cours ») ; an
  older one is RECLAIMED — whatever the clock says about ``open_until``,
  since the first claim was made in time — which is safe only because of
  the reserved ids above. ``claim_id`` then changes, so the stale holder's
  later ``complete``/``refuse``/``release`` can no longer touch it.
* ``expire_at`` is the TTL field (the fieldOverride in
  ``firestore.indexes.json``) — garbage collection ONLY, the
  ``mcp_idempotency`` doctrine: an open ticket keeps ``open_until`` + 24 h
  (one idempotency window, so a replayed ``begin_upload`` reads « fermé »
  rather than « introuvable »), and a ticket that reaches ``versé``,
  ``refusé`` or ``expiré`` is kept :data:`FINAL_RETENTION` (7 days, the
  staging lifecycle) so a ``finalize`` replayed without its key still finds
  the answer it gave.

Commit points (``models.provenance.note_commit``). :func:`create_ticket` and
:func:`complete_ticket` note theirs: they are the tool's own writes. The
claim, its release, the staged digest and a refusal deliberately do NOT —
they are protocol bookkeeping, and ``finalize_upload`` refuses AFTER them
(« not uploaded yet », « different bytes »): noted, those refusals would
reach ``run_write`` as « ENREGISTRÉE — NE PAS RÉESSAYER », which is false.
``tests/test_upload_ticket.py`` pins both halves.

Rule 6 holds (UUIDv4 ids, server-minted). Rule 7 holds too: every write is
stamped by ``models.provenance`` (``etag``, ``updated_via``…), though no
caller compares the etag — the transitions are transactional. No composite
index: keyed ``get()``s only. Not DAV-exposed: no CTag.

Failure posture. A claim that cannot read or write the ticket RAISES
:class:`TicketStoreUnavailable` (nothing may be filed on a claim that was
not established); :func:`get_ticket` and :func:`get_open_ticket` raise too —
« unreadable » is never « absent ». :func:`release_ticket` and
:func:`refuse_ticket` are best-effort (``False`` and an ``unexpected`` line):
a claim they fail to clear goes stale in five minutes and is reclaimed.

The staging object (lot 2A, T9). The bytes of an upload live, until
finalization, at the ticket's staging object — Storage bookkeeping of the
exchange, never a filed record — and every Storage verb on it lives HERE,
so the connector's handlers reach it only through this module:

* :func:`open_ticket` validates, opens the resumable session, THEN writes
  the ticket — a session that cannot be opened writes nothing (a ticket
  without a session would be a commit the caller could not use), and a
  ticket write that fails after the session opened leaves an orphan
  session, harmless: nothing finalizes it, and the bucket's ``staging/``
  lifecycle sweeps anything uploaded through it. :func:`open_session`
  re-opens one for a still-open ticket (``begin_upload``'s replay). The
  session URI is a CAPABILITY: returned to the caller, never stored,
  logged or traced here. Its object is created only
  (``if_generation_match=0``), capped by GCS at the declared size, and
  carries the declared MD5 in its initiation metadata;
* :func:`staged_blob` / :func:`staged_mismatch` — the uploaded object,
  reloaded, and the comparison of its OWN size and MD5 with what the
  ticket bound (``hmac.compare_digest``);
* :func:`read_staged_bytes` — a template's bytes, read at the generation
  that was checked and re-checked against the bound MD5;
* :func:`discard_staging` — the object consumed once the ticket is
  SETTLED (versé, refusé, expiré). A claim RELEASED for a later retry
  (« not uploaded yet », a transient failure) keeps its object: the ticket
  is still open, and the bytes are what the retry will file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from firebase_admin import storage
from google.api_core.exceptions import AlreadyExists, NotFound, PreconditionFailed
from google.cloud import firestore

from models import db, provenance
from models.doc_template import DOCX_MIME, MAX_TEMPLATE_SIZE
from models.document import (
    ALLOWED_EXTENSIONS,
    EXTENSION_MIME_TYPES,
    MAX_FILE_SIZE,
    is_canonical_uuid4,
)
from security import sanitize
from utils import storage_identity
from utils.logging_setup import log_unexpected

__all__ = [
    "ALREADY_RECEIVED_MESSAGE",
    "COLLECTION",
    "FINAL_RETENTION",
    "OPEN_WINDOW",
    "REFUSAL_REASONS",
    "RETRYABLE_OPEN_ERRORS",
    "STALE_CLAIM_AFTER",
    "StagedBytesChanged",
    "TicketClaim",
    "TicketStoreUnavailable",
    "UploadSessionUnavailable",
    "claim_ticket",
    "complete_ticket",
    "content_type_for",
    "create_ticket",
    "discard_staging",
    "get_open_ticket",
    "get_ticket",
    "message_for",
    "open_session",
    "open_ticket",
    "read_staged_bytes",
    "record_staged_digest",
    "refuse_ticket",
    "release_ticket",
    "session_error_message",
    "staged_after_window",
    "staged_blob",
    "staged_exists",
    "staged_mismatch",
]

COLLECTION = "mcp_upload_tickets"

PURPOSE_DOCUMENT = "document"
PURPOSE_GABARIT = "gabarit"
VALID_PURPOSES = (PURPOSE_DOCUMENT, PURPOSE_GABARIT)

STATUS_OPEN = "en_attente"
STATUS_CLAIMED = "en_cours"
STATUS_DONE = "versé"
STATUS_REFUSED = "refusé"
STATUS_EXPIRED = "expiré"
VALID_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE, STATUS_REFUSED,
                  STATUS_EXPIRED)

MODE_CREATE = "create"
MODE_REPLACE = "replace"

OPEN_WINDOW = timedelta(hours=1)
STALE_CLAIM_AFTER = timedelta(minutes=5)
# An open ticket lives one idempotency window past its window.
OPEN_RETENTION = timedelta(hours=24)
# A settled ticket lives as long as the canonical bucket's staging/ rule.
FINAL_RETENTION = timedelta(days=7)

MAX_FILENAME_CHARS = 200
MAX_DOSSIER_ID_CHARS = 64
MAX_TEXT_CHARS = 2000
MAX_LIST_ITEMS = 50

# Why a ticket was refused — machine-stable, logged as such.
REFUSAL_REASONS = (
    "expire",                   # set by the claim itself, past open_until
    "taille_differente",        # the object's size is not the declared one
    "empreinte_differente",     # its MD5 is not the declared one
    "contenu_refuse",           # the ingestion or template write refused it
    "identifiants_residuels",   # the leak scan found unaccepted residues
    "sans_dossier_source",      # a gabarit naming no source dossier and not
                                # declaring none (a ticket stored before the
                                # fixups of lot 2A — never filed unchecked)
)

# What a claim answered.
CLAIMED = "claimed"
RECLAIMED = "reclaimed"
DONE = "done"
REFUSED = "refused"

REASON_NOT_FOUND = "introuvable"
REASON_EXPIRED = "expire"
REASON_REFUSED = "refuse"
REASON_BUSY = "en_cours"
REASON_CLOSED = "ferme"
# Already filed: the answer exists, finalize_upload returns it (review of
# T9 — see _MESSAGES).
REASON_FILED = "verse"

# « Ouvrez-en un nouveau » says WHICH key: the write protocol tells a caller
# to REUSE its idempotency_key on a retry, and a replayed opening with that
# key rehydrates the SAME closed ticket and refuses again, for the whole
# 24 h window — without the clause, the natural reading loops.
_NEW_TICKET = "ouvrez-en un nouveau, avec une NOUVELLE idempotency_key"
_MESSAGES = {
    REASON_NOT_FOUND: (
        f"Ce ticket de téléversement est introuvable : {_NEW_TICKET}."
    ),
    REASON_EXPIRED: (
        "Ce ticket de téléversement a expiré (il vaut une heure) : rien n'a "
        f"été versé. {_NEW_TICKET[0].upper()}{_NEW_TICKET[1:]} ; un fichier "
        "envoyé trop tard est effacé automatiquement."
    ),
    REASON_REFUSED: (
        f"Ce ticket de téléversement a déjà été refusé : {_NEW_TICKET}."
    ),
    REASON_BUSY: (
        "Une finalisation de ce ticket est déjà en cours : attendez quelques "
        "minutes, puis réessayez."
    ),
    REASON_CLOSED: (
        f"Ce ticket de téléversement est fermé : {_NEW_TICKET}."
    ),
    # Review of T9: a ticket already FILED must never send its caller to a
    # new ticket. A replayed begin_upload reaches it with the SAME key — the
    # natural retry of a task re-run, a key derived from the file's own
    # digest — and « ouvrez-en un nouveau » would have filed the same file a
    # second time. finalize_upload answers a filed ticket's result again.
    REASON_FILED: (
        "Ce ticket a déjà été versé : ne téléversez pas ce fichier de "
        "nouveau — appelez finalize_upload avec ce ticket_id, qui rend le "
        "résultat du versement sans rien verser une seconde fois."
    ),
}
_STORE_MESSAGE = (
    "Le registre des téléversements est illisible pour le moment : rien n'a "
    "été versé. Réessayez dans un instant."
)
_SAVE_MESSAGE = (
    "Erreur lors de l'enregistrement du ticket de téléversement : rien n'a "
    "été ouvert. Réessayez."
)
_SESSION_MESSAGE = (
    "Le téléversement n'a pas pu être ouvert auprès du stockage : rien n'a "
    "été ouvert. Réessayez dans un instant."
)
# A replayed opening whose ticket ALREADY holds its bytes: a new session
# on the same create-only object could only fail at the PUT.
ALREADY_RECEIVED_MESSAGE = (
    "Ce ticket a déjà reçu son fichier : ne le téléversez pas de nouveau — "
    "appelez finalize_upload avec ce ticket_id."
)
_STAGING_SHAPE_MESSAGE = (
    "Ce ticket ne désigne pas un objet de téléversement valide : rien n'a "
    "été versé."
)


# The refusals of :func:`open_ticket` that say « the store failed, nothing
# was opened, the same call may succeed » — as opposed to a refusal of what
# was asked. (The connector logs them under a retry code.)
RETRYABLE_OPEN_ERRORS = (_SESSION_MESSAGE, _SAVE_MESSAGE)


class UploadSessionUnavailable(Exception):
    """A resumable session could not be opened. French message; never the
    URL (there is none on failure) nor an exception's text.

    Its text is chosen by KIND, never passed in (2026-09-30): the default
    « could not open, retry », or — ``already_received=True`` — the ticket
    already holds its bytes. Callers show it through
    :func:`session_error_message`, never ``str(exc)``.
    """

    def __init__(self, *, already_received: bool = False) -> None:
        self.already_received = bool(already_received)
        super().__init__(
            ALREADY_RECEIVED_MESSAGE if self.already_received
            else _SESSION_MESSAGE
        )


def session_error_message(exc: UploadSessionUnavailable) -> str:
    """The French sentence for *exc*: one of two module constants, read off
    nothing but its kind flag (what a caller hands a client — the connector,
    the ticket store — must never be built from exception data)."""
    if exc.already_received:
        return ALREADY_RECEIVED_MESSAGE
    return _SESSION_MESSAGE


class StagedBytesChanged(Exception):
    """The bytes read back are not the ones the checks passed on."""

_CREATE_KEYS = frozenset({
    "purpose", "dossier_id", "filename", "declared_size", "declared_md5_b64",
    "bound_metadata", "template_params",
})
_METADATA_KEYS = frozenset({
    "folder_id", "category", "display_name", "document_date", "tags",
})
_TEMPLATE_KEYS = frozenset({
    "mode", "name", "category", "kind", "description", "template_id",
    "expected_version", "accept_residual", "scrub_properties",
    "aucun_dossier_source",
})
_TEMPLATE_CREATE_ONLY = frozenset({"name", "category", "kind", "description"})
_TEMPLATE_REPLACE_ONLY = frozenset({"template_id", "expected_version"})
_RESULT_KEYS = frozenset({"document_id", "template_id", "version"})
_HEX = frozenset("0123456789abcdef")


class TicketStoreUnavailable(Exception):
    """The ticket store could not be read or written. French message."""

    def __init__(self, message: str = _STORE_MESSAGE) -> None:
        super().__init__(message)


def message_for(reason: str) -> str:
    """The French sentence for a claim/lookup *reason* code."""
    return _MESSAGES.get(reason, _MESSAGES[REASON_NOT_FOUND])


@dataclass(frozen=True)
class TicketClaim:
    """What :func:`claim_ticket` answered.

    ``state`` ∈ ``claimed`` | ``reclaimed`` (this call now holds the
    ticket, under ``claim_id``) | ``done`` (already ``versé``: return the
    stored result) | ``refused`` (``reason`` says why; ``message`` is the
    French sentence). ``ticket`` is the record as it stands after the call,
    ``None`` when it does not exist.
    """

    ticket: Optional[dict]
    state: str
    reason: str = ""
    claim_id: str = ""

    @property
    def holds(self) -> bool:
        return self.state in (CLAIMED, RECLAIMED)

    @property
    def message(self) -> str:
        return message_for(self.reason) if self.reason else ""


# ── Helpers ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> datetime:
    if value is None:
        return _now()
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _as_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ref(ticket_id: str):
    return db.collection(COLLECTION).document(ticket_id)


def _snapshot_dict(snap) -> Optional[dict]:
    """The stored ticket, ``None`` when absent. A snapshot that exists but
    holds no dict (a MagicMock) is unreadable, never absent."""
    if not snap.exists:
        return None
    data = snap.to_dict()
    if not isinstance(data, dict):
        raise TicketStoreUnavailable()
    return data


def _canonical_md5(value: Any) -> Optional[str]:
    """The declared MD5 as GCS reports one — standard base64 of 16 bytes,
    re-encoded so two spellings of the same digest compare equal — or
    ``None`` when *value* is not that."""
    if not isinstance(value, str) or len(value) != 24:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 16:
        return None
    return base64.b64encode(raw).decode("ascii")


def _clean_text(value: Any, label: str, errors: list[str], *,
                limit: int = MAX_TEXT_CHARS, required: bool = False) -> str:
    """A bound string, refused rather than altered: over-long, or carrying
    what ``security.sanitize`` would strip."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors.append(f"« {label} » doit être du texte.")
        return ""
    text = value.strip()
    if required and not text:
        errors.append(f"« {label} » est requis.")
    elif len(text) > limit:
        errors.append(f"« {label} » dépasse {limit} caractères.")
    elif sanitize(text, len(text) + 1) != text:
        errors.append(f"« {label} » contient des chevrons (< >) : retirez-les.")
    return text


def _clean_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"« {label} » doit être une liste de textes.")
        return []
    if len(value) > MAX_LIST_ITEMS:
        errors.append(f"« {label} » compte plus de {MAX_LIST_ITEMS} éléments.")
        return []
    return [_clean_text(v, label, errors, limit=200) for v in value]


def _clean_filename(value: Any, errors: list[str]) -> tuple[str, str]:
    """``(file name, lower-cased extension)`` — refused, never mangled."""
    if not isinstance(value, str) or not value.strip():
        errors.append("Le nom du fichier est requis.")
        return "", ""
    name = value.strip()
    if len(name) > MAX_FILENAME_CHARS:
        errors.append(
            f"Le nom du fichier dépasse {MAX_FILENAME_CHARS} caractères.")
        return "", ""
    if "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        errors.append(
            "Le nom du fichier ne peut contenir ni barre oblique ni "
            "caractère de contrôle.")
        return "", ""
    if sanitize(name, len(name) + 1) != name:
        errors.append("Le nom du fichier contient des chevrons (< >).")
        return "", ""
    return name, os.path.splitext(name)[1].lower()


def _clean_metadata(value: Any, errors: list[str]) -> dict:
    """The document metadata bound at ``begin`` — shapes only; the handler
    has already validated each value as the document model would."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append("Les métadonnées du document doivent être un objet.")
        return {}
    unknown = sorted(str(k) for k in value if k not in _METADATA_KEYS)
    if unknown:
        errors.append("Métadonnée non reconnue : " + ", ".join(unknown) + ".")
        return {}
    out: dict = {}
    for key in ("folder_id", "category", "display_name"):
        if key in value:
            out[key] = _clean_text(value[key], key, errors, limit=300)
    if "document_date" in value:
        date_value = value["document_date"]
        if date_value is not None and not isinstance(date_value, datetime):
            errors.append("« document_date » doit être une date.")
        else:
            out["document_date"] = date_value
    if "tags" in value:
        out["tags"] = _clean_list(value["tags"], "tags", errors)
    return out


def _clean_template_params(value: Any, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("Les paramètres du gabarit doivent être un objet.")
        return {}
    unknown = sorted(str(k) for k in value if k not in _TEMPLATE_KEYS)
    if unknown:
        errors.append("Paramètre de gabarit non reconnu : "
                      + ", ".join(unknown) + ".")
        return {}
    mode = value.get("mode")
    if mode not in (MODE_CREATE, MODE_REPLACE):
        errors.append("Le mode du gabarit est « create » ou « replace ».")
        return {}
    out: dict = {"mode": mode}
    if mode == MODE_CREATE:
        stray = sorted(k for k in _TEMPLATE_REPLACE_ONLY if k in value)
        if stray:
            errors.append("Un nouveau gabarit ne nomme pas de gabarit "
                          "existant (" + ", ".join(stray) + ").")
        out["name"] = _clean_text(value.get("name"), "name", errors,
                                  limit=120, required=True)
        for key in ("category", "kind"):
            out[key] = _clean_text(value.get(key), key, errors, limit=60)
        out["description"] = _clean_text(value.get("description"),
                                         "description", errors)
    else:
        stray = sorted(k for k in _TEMPLATE_CREATE_ONLY if k in value)
        if stray:
            errors.append("Un remplacement de fichier ne change pas les "
                          "métadonnées du gabarit (" + ", ".join(stray) + ").")
        template_id = value.get("template_id")
        if not is_canonical_uuid4(template_id):
            errors.append("Le gabarit à remplacer est requis (identifiant).")
        version = value.get("expected_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            errors.append("« expected_version » doit être un entier ≥ 1.")
        out["template_id"] = template_id if isinstance(template_id, str) else ""
        out["expected_version"] = version if isinstance(version, int) else 0
    out["accept_residual"] = _clean_list(value.get("accept_residual"),
                                         "accept_residual", errors)
    scrub = value.get("scrub_properties", False)
    if not isinstance(scrub, bool):
        errors.append("« scrub_properties » est vrai ou faux.")
        scrub = False
    out["scrub_properties"] = scrub
    # Fixups of lot 2A: a gabarit either names the dossier its file comes
    # from (the leak scan runs against it) or DECLARES it comes from none —
    # never a silent skip. Judged against the dossier in create_ticket.
    no_source = value.get("aucun_dossier_source", False)
    if not isinstance(no_source, bool):
        errors.append("« aucun_dossier_source » est vrai ou faux.")
        no_source = False
    out["aucun_dossier_source"] = no_source
    return out


def _settle_fields(at: datetime) -> dict:
    """The fields every settled status (versé, refusé, expiré) carries."""
    return {"finalized_at": at, "expire_at": at + FINAL_RETENTION}


def _transaction():
    return db.transaction()


# ── Create / read ────────────────────────────────────────────────────────


def create_ticket(
    data: dict, *, user_id: str, now: Optional[datetime] = None,
    before_write: Optional[Callable[[dict], None]] = None,
) -> tuple[Optional[dict], list[str]]:
    """Open a ticket: validate and BIND everything ``finalize`` will use.

    *data* keys (a whitelist — any other key is refused): ``purpose``,
    ``dossier_id`` (required for a document), ``filename``,
    ``declared_size``, ``declared_md5_b64``, ``bound_metadata`` (document
    only), ``template_params`` (gabarit only). *user_id* is the owner uid
    the staging object is keyed under, re-checked by ``require_uid``.

    The ids are minted here, never supplied: the ticket id, and the
    reserved document or template id. Written with ``create()`` — a ticket
    is never overwritten. Returns ``(ticket, [])`` or ``(None, errors)``;
    nothing is written on a refusal.

    *before_write* (keyword, lot 2A T9) runs on the validated ticket AFTER
    every check and BEFORE the write — :func:`open_ticket` opens the
    resumable session there, so a session that cannot be opened writes no
    ticket. An :class:`UploadSessionUnavailable` it raises is this call's
    refusal; anything else propagates.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["Requête de téléversement invalide."]
    unknown = sorted(str(k) for k in data if k not in _CREATE_KEYS)
    if unknown:
        return None, ["Champ non reconnu : " + ", ".join(unknown) + "."]

    purpose = data.get("purpose")
    if purpose not in VALID_PURPOSES:
        return None, ["La destination est « document » ou « gabarit »."]

    filename, ext = _clean_filename(data.get("filename"), errors)
    if filename:
        if purpose == PURPOSE_GABARIT and ext != ".docx":
            errors.append("Un gabarit est un document Word (.docx).")
        elif purpose == PURPOSE_DOCUMENT and ext not in ALLOWED_EXTENSIONS:
            errors.append("Ce type de fichier n'est pas accepté.")

    size = data.get("declared_size")
    cap = MAX_TEMPLATE_SIZE if purpose == PURPOSE_GABARIT else MAX_FILE_SIZE
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        errors.append("La taille déclarée doit être un entier positif.")
    elif size > cap:
        errors.append(
            f"Le fichier dépasse la taille maximale ({cap // (1024 * 1024)} Mo).")

    md5 = _canonical_md5(data.get("declared_md5_b64"))
    if md5 is None:
        errors.append(
            "L'empreinte MD5 déclarée doit être celle du fichier, en base64 "
            "(24 caractères).")

    dossier_id = data.get("dossier_id") or ""
    if not isinstance(dossier_id, str) or len(dossier_id) > MAX_DOSSIER_ID_CHARS \
            or "/" in dossier_id:
        errors.append("Identifiant de dossier invalide.")
        dossier_id = ""
    elif purpose == PURPOSE_DOCUMENT and not dossier_id.strip():
        errors.append("Un document est versé dans un dossier : il est requis.")
    dossier_id = dossier_id.strip()

    metadata: dict = {}
    template_params: dict = {}
    if purpose == PURPOSE_DOCUMENT:
        if data.get("template_params"):
            errors.append("Un document ne porte pas de paramètres de gabarit.")
        metadata = _clean_metadata(data.get("bound_metadata"), errors)
    else:
        if data.get("bound_metadata"):
            errors.append("Un gabarit ne porte pas de métadonnées de document.")
        template_params = _clean_template_params(
            data.get("template_params"), errors)
        if template_params:
            declared = template_params.get("aucun_dossier_source") is True
            if dossier_id.strip() and declared:
                errors.append(
                    "Un gabarit nomme son dossier source OU déclare n'en "
                    "venir d'aucun — pas les deux.")
            elif not dossier_id.strip() and not declared:
                errors.append(
                    "Un gabarit nomme le dossier dont son fichier est tiré, "
                    "ou déclare expressément n'en venir d'aucun : sans l'un "
                    "ni l'autre, ses identifiants ne seraient contrôlés "
                    "contre rien, en silence.")

    uid = ""
    try:
        uid = storage_identity.require_uid(user_id)
    except storage_identity.StorageIdentityUnavailable as exc:
        errors.append(storage_identity.public_message(exc))
    if errors:
        return None, errors

    at = _aware(now)
    ticket_id = str(uuid.uuid4())
    creating_template = (purpose == PURPOSE_GABARIT
                         and template_params.get("mode") == MODE_CREATE)
    open_until = at + OPEN_WINDOW
    doc: dict = {
        "id": ticket_id,
        "purpose": purpose,
        "dossier_id": dossier_id,
        "original_filename": filename,
        "ext": ext,
        "declared_size": size,
        "declared_md5_b64": md5,
        "staging_object": f"staging/{uid}/mcp/{ticket_id}/upload{ext}",
        "reserved_document_id": (
            str(uuid.uuid4()) if purpose == PURPOSE_DOCUMENT else ""),
        "reserved_template_id": str(uuid.uuid4()) if creating_template else "",
        "bound_metadata": metadata,
        "template_params": template_params,
        "status": STATUS_OPEN,
        "open_until": open_until,
        "expire_at": open_until + OPEN_RETENTION,
        "claim_id": "",
        "claimed_at": None,
        "claim_count": 0,
        "staged_sha256": "",
        "finalized_at": None,
        "result": {},
        "refusal_reason": "",
    }
    doc.update(provenance.create_fields(at))
    if before_write is not None:
        try:
            before_write(doc)
        except UploadSessionUnavailable as exc:
            return None, [session_error_message(exc)]
    try:
        _ref(ticket_id).create(doc)
    except AlreadyExists:
        return None, [_SAVE_MESSAGE]
    except Exception:
        log_unexpected("upload ticket create failed", ticket_id=ticket_id)
        return None, [_SAVE_MESSAGE]
    provenance.note_commit(COLLECTION, ticket_id)
    return doc, []


def open_ticket(
    data: dict, *, user_id: str, now: Optional[datetime] = None
) -> tuple[Optional[dict], Optional[str], list[str]]:
    """``begin_upload``'s write: :func:`create_ticket`, with the resumable
    session opened BEFORE the ticket is written.

    Returns ``(ticket, session_url, [])`` or ``(None, None, errors)``. The
    order is the point: a session that cannot be opened refuses with
    nothing written — a stored ticket without a session would be a commit
    the caller could not use, reported « ENREGISTRÉE » by the write
    protocol — while a ticket write that fails after the session opened
    leaves only an orphan session, which nothing finalizes.
    """
    opened: dict = {}

    def _session(ticket: dict) -> None:
        opened["url"] = open_session(ticket)

    ticket, errors = create_ticket(data, user_id=user_id, now=now,
                                   before_write=_session)
    if ticket is None:
        return None, None, errors
    return ticket, opened["url"], []


# ── The staging object (GCS) ─────────────────────────────────────────────


def content_type_for(ticket: dict) -> str:
    """The MIME type the session declares — the extension's own (a
    template is always a .docx). The ingestion re-sniffs the bytes and
    stores the SNIFFED type either way; this only labels the upload."""
    if (ticket or {}).get("purpose") == PURPOSE_GABARIT:
        return DOCX_MIME
    return EXTENSION_MIME_TYPES.get((ticket or {}).get("ext") or "",
                                    "application/octet-stream")


def _staging_name(ticket: dict) -> str:
    """The ticket's staging object name, re-checked against the ONE shape
    :func:`create_ticket` builds — ``staging/{uid}/mcp/{ticket_id}/upload{ext}``
    — so a record whose path was altered can never point the Storage verbs
    below at another object (a document's canonical path, above all)."""
    ticket = ticket or {}
    name = ticket.get("staging_object")
    ticket_id = ticket.get("id")
    if not isinstance(name, str) or not is_canonical_uuid4(ticket_id):
        raise ValueError(_STAGING_SHAPE_MESSAGE)
    parts = name.split("/")
    ext = ticket.get("ext") or ""
    if (len(parts) != 5 or parts[0] != "staging" or parts[2] != "mcp"
            or parts[3] != ticket_id or parts[4] != f"upload{ext}"):
        raise ValueError(_STAGING_SHAPE_MESSAGE)
    try:
        storage_identity.require_uid(parts[1])
    except storage_identity.StorageIdentityUnavailable:
        raise ValueError(_STAGING_SHAPE_MESSAGE) from None
    return name


def open_session(ticket: dict) -> str:
    """Open a resumable upload session for the ticket's staging object and
    return its URI — the CAPABILITY. Never stored, logged or traced here.

    * ``size=`` the declared size — GCS refuses any byte beyond it;
    * ``if_generation_match=0`` — the session can only CREATE the object:
      once the bytes landed, no session (not a leaked one, not a replay's)
      can replace them;
    * the declared MD5 travels in the initiation metadata
      (``Blob.md5_hash``, a writable property of google-cloud-storage
      3.10.1 — ``md5Hash`` is in its ``_WRITABLE_FIELDS``), so the service
      can reject a PUT of other bytes; ``finalize_upload`` re-checks size
      and MD5 on the stored object either way, and THAT check is the
      authoritative one;
    * no ``origin=`` — the PUT comes from Claude's code sandbox, not a
      browser page: there is no CORS exchange to authorise.

    Raises :class:`UploadSessionUnavailable` on any failure.
    """
    try:
        name = _staging_name(ticket)
        size = int(ticket.get("declared_size") or 0)
        md5 = _canonical_md5(ticket.get("declared_md5_b64"))
        if size < 1 or md5 is None:
            raise ValueError("unbound size or digest")
    except (TypeError, ValueError):
        raise UploadSessionUnavailable() from None
    try:
        blob = storage.bucket().blob(name)
        blob.md5_hash = md5
        url = blob.create_resumable_upload_session(
            content_type=content_type_for(ticket), size=size,
            if_generation_match=0,
        )
    except PreconditionFailed:
        raise UploadSessionUnavailable(already_received=True) from None
    except Exception as exc:
        # The class only: an initiation failure carries no session URI, but
        # its text is the service's, and nothing here needs it.
        log_unexpected("upload session open failed", exc_info=False,
                       ticket_id=ticket.get("id"),
                       error_type=type(exc).__name__)
        raise UploadSessionUnavailable() from None
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UploadSessionUnavailable()
    return url


def staged_blob(ticket: dict):
    """The uploaded object, RELOADED (its size, MD5 and generation are the
    service's). Raises ``NotFound`` when nothing was uploaded yet, anything
    else on a store failure — the caller tells the two apart."""
    try:
        name = _staging_name(ticket)
    except ValueError:
        raise NotFound(_STAGING_SHAPE_MESSAGE) from None
    blob = storage.bucket().blob(name)
    blob.reload()
    return blob


def staged_exists(ticket: dict) -> Optional[bool]:
    """Whether the ticket's object exists — ``None`` when unknowable."""
    try:
        staged_blob(ticket)
    except NotFound:
        return False
    except Exception:
        return None
    return True


def _same_digest(stored: Any, declared: Any) -> bool:
    if not isinstance(stored, str) or not isinstance(declared, str):
        return False
    if not stored or not declared:
        return False
    return hmac.compare_digest(stored.encode("ascii", "replace"),
                               declared.encode("ascii", "replace"))


def staged_mismatch(ticket: dict, blob) -> Optional[str]:
    """``None`` when the uploaded object IS what the ticket bound — its
    own size and MD5 equal the declared ones — else the refusal reason,
    ``taille_differente`` or ``empreinte_differente``.

    The defence against a leaked session URI: anyone holding it could PUT,
    once, bytes of the declared size — never bytes of the declared MD5
    without being the file. A composite object carries no MD5: refused.
    Compared with ``hmac.compare_digest`` (constant time)."""
    try:
        size = int(blob.size)
    except (TypeError, ValueError):
        return "taille_differente"
    if size != int(ticket.get("declared_size") or -1):
        return "taille_differente"
    if not _same_digest(getattr(blob, "md5_hash", None),
                        _canonical_md5(ticket.get("declared_md5_b64"))):
        return "empreinte_differente"
    return None


def staged_after_window(ticket: dict, blob) -> bool:
    """True when a claim taken PAST the ticket's window found bytes that
    were themselves created after it — a late upload, never filed.

    A fresh claim is taken before ``open_until`` (else the claim itself
    expires the ticket), so its bytes are in time by construction. A stale
    claim is RECLAIMED whatever the clock (module docstring) — right when
    the first finalizer died holding bytes that arrived in time, wrong when
    it died holding NOTHING (its release failed) and the PUT came after the
    hour: without this check that late upload would be filed, where the
    ticket, the consent screen and every refusal say it never is (review
    of T9). Judged on the object's OWN creation instant (``timeCreated``,
    loaded by ``reload()``); an object whose instant is unknown is not
    called late."""
    ticket = ticket or {}
    open_until = _as_datetime(ticket.get("open_until"))
    claimed_at = _as_datetime(ticket.get("claimed_at"))
    created = _as_datetime(getattr(blob, "time_created", None))
    if open_until is None or claimed_at is None or created is None:
        return False
    return claimed_at >= open_until and created > open_until


def read_staged_bytes(ticket: dict, blob) -> bytes:
    """The staged bytes — a template's, ≤ 10 MB — read at the generation
    that was checked, and re-checked against the bound size and MD5.

    Raises :class:`StagedBytesChanged` when what came back is not the file
    the checks passed on, anything else on a read failure. The object is
    create-only, so a change is not expected: this makes the bytes the
    template is built from the bytes that were compared, by construction.
    """
    data = blob.download_as_bytes(if_generation_match=blob.generation)
    declared = _canonical_md5(ticket.get("declared_md5_b64"))
    digest = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")  # nosec B324 — an integrity check against GCS's own MD5, not a security hash
    if len(data) != int(ticket.get("declared_size") or -1) \
            or not _same_digest(digest, declared):
        raise StagedBytesChanged()
    return data


def discard_staging(ticket: dict) -> bool:
    """Consume the ticket's staging object — bookkeeping of the exchange,
    never a filed record: refused bytes are not kept, filed bytes live on
    at their canonical path, and an expired upload was never filed.

    Best-effort: ``True`` when the object is gone (already, or now), else
    ``False`` with an ``unexpected`` line — an object left behind is swept
    by the canonical bucket's ``staging/`` 7-day lifecycle rule. Called only
    once the ticket is SETTLED (module docstring)."""
    try:
        name = _staging_name(ticket)
    except ValueError:
        return False
    try:
        storage.bucket().blob(name).delete()
    except NotFound:
        return True
    except Exception as exc:
        log_unexpected("upload staging cleanup failed", exc_info=False,
                       ticket_id=(ticket or {}).get("id"),
                       error_type=type(exc).__name__)
        return False
    return True


def get_ticket(ticket_id: str) -> Optional[dict]:
    """The ticket, ``None`` when it does not exist (or the id is not one
    this module mints). RAISES :class:`TicketStoreUnavailable` on a read
    failure — « unreadable » is never « absent »."""
    if not is_canonical_uuid4(ticket_id):
        return None
    try:
        return _snapshot_dict(_ref(ticket_id).get())
    except TicketStoreUnavailable:
        raise
    except Exception:
        log_unexpected("upload ticket read failed", ticket_id=ticket_id)
        raise TicketStoreUnavailable() from None


def get_open_ticket(
    ticket_id: str, *, now: Optional[datetime] = None
) -> tuple[Optional[dict], str]:
    """``(ticket, "")`` when the ticket is still OPEN for an upload
    (``en_attente``, before ``open_until``), else ``(ticket or None,
    reason)`` — ``introuvable``, ``expire`` (expired, or open but past its
    window), ``refuse`` (refused), ``en_cours`` (a finalization holds it:
    wait, then retry — it may be released « not received », or filed) or
    ``verse`` (already filed: finalize_upload returns its result — never
    « open a new one », which would file the same file twice).
    Read-only: a replayed ``begin_upload`` asks this before handing out a
    fresh session. Raises :class:`TicketStoreUnavailable` on a read
    failure."""
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return None, REASON_NOT_FOUND
    status = ticket.get("status")
    if status == STATUS_EXPIRED:
        return ticket, REASON_EXPIRED
    if status == STATUS_REFUSED:
        return ticket, REASON_REFUSED
    if status == STATUS_DONE:
        return ticket, REASON_FILED
    if status == STATUS_CLAIMED:
        return ticket, REASON_BUSY
    if status != STATUS_OPEN:
        return ticket, REASON_CLOSED
    open_until = _as_datetime(ticket.get("open_until"))
    if open_until is None or _aware(now) >= open_until:
        return ticket, REASON_EXPIRED
    return ticket, ""


# ── Transitions ──────────────────────────────────────────────────────────


def claim_ticket(
    ticket_id: str,
    *,
    now: Optional[datetime] = None,
    stale_after: timedelta = STALE_CLAIM_AFTER,
) -> TicketClaim:
    """Take the ticket for a finalization — transactionally.

    ``en_attente`` within its window → ``en_cours`` under a fresh
    ``claim_id`` (``claimed``); past its window → ``expiré``, refused.
    ``en_cours`` → refused ``en_cours`` while the claim is younger than
    *stale_after*, else RECLAIMED under a new ``claim_id``. ``versé`` →
    ``done`` (the caller returns the stored result). ``refusé`` / ``expiré``
    / missing → refused. Raises :class:`TicketStoreUnavailable` when the
    ticket cannot be read or written — nothing may proceed on a claim that
    was not established.
    """
    if not is_canonical_uuid4(ticket_id):
        return TicketClaim(None, REFUSED, REASON_NOT_FOUND)
    at = _aware(now)

    @firestore.transactional
    def _body(txn) -> TicketClaim:
        ref = _ref(ticket_id)
        ticket = _snapshot_dict(ref.get(transaction=txn))
        if ticket is None:
            return TicketClaim(None, REFUSED, REASON_NOT_FOUND)
        status = ticket.get("status")
        if status == STATUS_DONE:
            return TicketClaim(ticket, DONE)
        if status == STATUS_REFUSED:
            return TicketClaim(ticket, REFUSED, REASON_REFUSED)
        if status == STATUS_EXPIRED:
            return TicketClaim(ticket, REFUSED, REASON_EXPIRED)
        if status == STATUS_OPEN:
            open_until = _as_datetime(ticket.get("open_until"))
            if open_until is None or at >= open_until:
                fields = {"status": STATUS_EXPIRED, "refusal_reason": "expire",
                          **_settle_fields(at), **provenance.update_fields(at)}
                txn.update(ref, fields)
                ticket.update(fields)
                return TicketClaim(ticket, REFUSED, REASON_EXPIRED)
            state = CLAIMED
        elif status == STATUS_CLAIMED:
            claimed_at = _as_datetime(ticket.get("claimed_at"))
            if claimed_at is not None and at - claimed_at < stale_after:
                return TicketClaim(ticket, REFUSED, REASON_BUSY)
            state = RECLAIMED
        else:
            raise TicketStoreUnavailable()
        claim_id = str(uuid.uuid4())
        fields = {
            "status": STATUS_CLAIMED,
            "claim_id": claim_id,
            "claimed_at": at,
            "claim_count": int(ticket.get("claim_count") or 0) + 1,
            **provenance.update_fields(at),
        }
        txn.update(ref, fields)
        ticket.update(fields)
        return TicketClaim(ticket, state, "", claim_id)

    try:
        return _body(_transaction())
    except TicketStoreUnavailable:
        raise
    except Exception:
        log_unexpected("upload ticket claim failed", ticket_id=ticket_id)
        raise TicketStoreUnavailable() from None


def _holder_update(ticket_id: str, claim_id: str, fields_for, *,
                   at: datetime) -> Optional[dict]:
    """In one transaction: when the ticket is ``en_cours`` under *claim_id*,
    apply ``fields_for(ticket)`` (``None`` → nothing to write) and return
    the ticket as written; ``None`` when this caller does not hold it."""
    @firestore.transactional
    def _body(txn) -> Optional[dict]:
        ref = _ref(ticket_id)
        ticket = _snapshot_dict(ref.get(transaction=txn))
        if (ticket is None or ticket.get("status") != STATUS_CLAIMED
                or not claim_id or ticket.get("claim_id") != claim_id):
            return None
        fields = fields_for(ticket)
        if fields:
            fields = {**fields, **provenance.update_fields(at)}
            txn.update(ref, fields)
            ticket.update(fields)
        return ticket

    return _body(_transaction())


def release_ticket(
    ticket_id: str, *, claim_id: str, now: Optional[datetime] = None
) -> bool:
    """Give the ticket back (``en_cours`` → ``en_attente``) — nothing was
    filed and it may be finalized later (« pas encore téléversé »).

    Only the holder can: ``False`` when *claim_id* no longer holds it (a
    reclaim happened). Best-effort — a store failure logs and returns
    ``False``; the claim then goes stale and is reclaimed.
    """
    if not is_canonical_uuid4(ticket_id):
        return False
    at = _aware(now)
    try:
        written = _holder_update(
            ticket_id, claim_id,
            lambda _t: {"status": STATUS_OPEN, "claim_id": "",
                        "claimed_at": None},
            at=at)
    except Exception:
        log_unexpected("upload ticket release failed", ticket_id=ticket_id)
        return False
    return written is not None


def record_staged_digest(
    ticket_id: str, *, claim_id: str, sha256: str,
    now: Optional[datetime] = None,
) -> bool:
    """Record the staged bytes' SHA-256 BEFORE a template replacement is
    written, so a reclaim can recognise a replacement that already landed.

    Holder only (``False`` otherwise). Refused (``False``) when a different
    digest is already recorded — the bytes under this ticket cannot have
    changed, since their MD5 is bound. Raises :class:`TicketStoreUnavailable`
    on a store failure: the caller must not write what it could not record.
    """
    digest = (sha256 or "").strip().lower() if isinstance(sha256, str) else ""
    if not is_canonical_uuid4(ticket_id) or len(digest) != 64 \
            or not set(digest) <= _HEX:
        return False
    at = _aware(now)
    outcome = {"ok": True}

    def _fields(ticket: dict) -> Optional[dict]:
        # Reset on EVERY attempt: the transactional decorator re-runs this
        # after an Aborted, and a verdict left over from a discarded attempt
        # would contradict what the committed attempt did.
        outcome["ok"] = True
        stored = ticket.get("staged_sha256") or ""
        if stored == digest:
            return None
        if stored:
            outcome["ok"] = False
            return None
        return {"staged_sha256": digest}

    try:
        written = _holder_update(ticket_id, claim_id, _fields, at=at)
    except Exception:
        log_unexpected("upload ticket digest failed", ticket_id=ticket_id)
        raise TicketStoreUnavailable() from None
    return written is not None and outcome["ok"]


def _clean_result(result: Any) -> Optional[dict]:
    if not isinstance(result, dict) or not result:
        return None
    if set(result) - _RESULT_KEYS:
        return None
    out: dict = {}
    for key in ("document_id", "template_id"):
        if key in result:
            if not is_canonical_uuid4(result[key]):
                return None
            out[key] = result[key]
    if not out:
        return None
    if "version" in result:
        version = result["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return None
        out["version"] = version
    return out


def complete_ticket(
    ticket_id: str, *, claim_id: str, result: dict,
    now: Optional[datetime] = None,
) -> tuple[Optional[dict], list[str]]:
    """Mark the ticket ``versé`` with what it produced — transactionally.

    *result* (a whitelist): ``document_id`` and/or ``template_id``
    (canonical UUIDv4) and an optional ``version``. The holder of
    *claim_id* completes it; a ticket ALREADY ``versé`` with the same result
    answers with it (idempotent), with another result refuses. ``expire_at``
    moves to +7 days so the answer outlives the upload's own window.
    """
    clean = _clean_result(result)
    if clean is None:
        return None, ["Résultat de versement invalide."]
    if not is_canonical_uuid4(ticket_id):
        return None, [message_for(REASON_NOT_FOUND)]
    at = _aware(now)
    outcome: dict = {"errors": [], "changed": False}

    @firestore.transactional
    def _body(txn) -> Optional[dict]:
        # Reset on EVERY attempt (the decorator re-runs this after an
        # Aborted): an attempt whose write was DISCARDED must not leave
        # « changed » behind, or a retry that finds the ticket already versé
        # by the other finalizer would note a commit this call never made.
        outcome["errors"] = []
        outcome["changed"] = False
        ref = _ref(ticket_id)
        ticket = _snapshot_dict(ref.get(transaction=txn))
        if ticket is None:
            outcome["errors"] = [message_for(REASON_NOT_FOUND)]
            return None
        status = ticket.get("status")
        if status == STATUS_DONE:
            if ticket.get("result") == clean:
                return ticket
            outcome["errors"] = [
                "Ce ticket a déjà été versé, avec un autre résultat."]
            return None
        if status != STATUS_CLAIMED or ticket.get("claim_id") != claim_id \
                or not claim_id:
            outcome["errors"] = [message_for(
                REASON_BUSY if status == STATUS_CLAIMED else REASON_CLOSED)]
            return None
        fields = {"status": STATUS_DONE, "result": clean,
                  **_settle_fields(at), **provenance.update_fields(at)}
        txn.update(ref, fields)
        ticket.update(fields)
        outcome["changed"] = True
        return ticket

    try:
        ticket = _body(_transaction())
    except Exception:
        log_unexpected("upload ticket completion failed", ticket_id=ticket_id)
        return None, [_STORE_MESSAGE]
    if ticket is None:
        return None, outcome["errors"]
    if outcome["changed"]:
        provenance.note_commit(COLLECTION, ticket_id)
    return ticket, []


def refuse_ticket(
    ticket_id: str, *, claim_id: str, reason: str,
    now: Optional[datetime] = None,
) -> bool:
    """Mark the ticket ``refusé`` for *reason* (:data:`REFUSAL_REASONS`).

    Holder only (``False`` otherwise). A settled ticket is kept +7 days.
    Best-effort — a store failure logs and returns ``False``: the claim
    goes stale and a later finalization re-runs the same checks.
    ``ValueError`` for a reason outside the vocabulary (a caller bug).
    """
    if reason not in REFUSAL_REASONS:
        raise ValueError(f"unknown refusal reason: {reason!r}")
    if not is_canonical_uuid4(ticket_id):
        return False
    at = _aware(now)
    try:
        written = _holder_update(
            ticket_id, claim_id,
            lambda _t: {"status": STATUS_REFUSED, "refusal_reason": reason,
                        **_settle_fields(at)},
            at=at)
    except Exception:
        log_unexpected("upload ticket refusal failed", ticket_id=ticket_id)
        return False
    return written is not None
