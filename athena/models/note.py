"""Notes — timestamped journal entries, usually linked to a case file.

Each note becomes a VJOURNAL resource in a CalDAV collection:
    /dav/dossier-{dossierId}/{noteId}.ics   when linked to a dossier
    /dav/general/{noteId}.ics               when it has none

``dossier_id`` is OPTIONAL (July 2026): a note with none is a free journal
entry — legal watch, a research memo tied to no file — and lives in the
« Général » collection alongside dossier-less tasks and hearings. Callers
must still distinguish "no dossier chosen" from "dossier not found": the
model can no longer tell them apart, so blanking an unknown id silently
turns a dossier note into a general one.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import concurrency, dav_ids, db, provenance
from models import revision as revision_model
from security import sanitize
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

COLLECTION = "notes"

VALID_CATEGORIES = (
    "rencontre",
    "consultation",
    "analyse",
    "recherche",
    "stratégie",
    "vacation",
    "autre",
)

CATEGORY_LABELS = {
    "rencontre": "Rencontre",
    "consultation": "Consultation",
    "analyse": "Analyse",
    "recherche": "Recherche",
    "stratégie": "Stratégie",
    "vacation": "Vacation",
    "autre": "Autre",
}

# Removed category keys → live key, applied ON READ (_migrate_category),
# BEFORE validation. Mirrors models/dossier._MANDATE_TYPE_MIGRATION. NOTE
# (spec §5): `appel` AND `correspondance` both fold to `autre` — the phone-
# call / correspondence distinction is intentionally lost (user decision
# 2026-07-24). `stratégie` is KEPT (the « Théorie de la cause » note uses it).
_CATEGORY_MIGRATION = {
    "audience": "vacation",   # a court appearance is a vacation
    "appel": "autre",
    "correspondance": "autre",
}


def _migrate_category(doc: dict) -> dict:
    """Fold a removed category key onto its live target (read-time net)."""
    old = doc.get("category", "")
    if old in _CATEGORY_MIGRATION:
        doc["category"] = _CATEGORY_MIGRATION[old]
    return doc


def _to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (for iCalendar UTC stamps)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _default_doc() -> dict:
    """Return a dict with every note field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "content": "",
        "category": "autre",
        "pinned": False,
        # A dateless note is a pure note (VJOURNAL without DTSTART) — jtx
        # Board files it under « Notes » instead of the dated journal view.
        "dateless": False,
        # The single « Théorie de la cause » note of a dossier (the Analyse
        # sheet). Hidden from the Notes views, INCLUDED on the DAV and MCP
        # read paths — see list_notes(include_analyse=...).
        "is_analyse": False,
        # DAV
        "vjournal_uid": "",
        # Metadata
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


# Note content (Markdown) is long-form — meeting minutes, research, strategy —
# so it gets a far more generous cap than the short scalar fields. The ceiling
# sits well under Firestore's 1 MiB document limit and the 1 MB request-size
# guard in security.py, and the content textarea carries a matching ``maxlength``
# so the cap is enforced (and visible) in the browser instead of silently
# truncating on save. Every other string field (title, denormalized dossier
# labels) keeps the app-wide 2000-char bound.
CONTENT_MAX_LENGTH = 100_000
_FIELD_MAX_LENGTH = 2000


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*.

    ``content`` is bounded to :data:`CONTENT_MAX_LENGTH`; every other string
    field to :data:`_FIELD_MAX_LENGTH`.
    """
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            limit = CONTENT_MAX_LENGTH if key == "content" else _FIELD_MAX_LENGTH
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    # dossier_id is deliberately NOT required: an empty one means the note
    # belongs to « Général ». The caller is responsible for refusing a
    # dossier_id that was supplied but does not resolve — see
    # routes/notes._enrich_dossier_info and mcp/handlers.create_note.
    if not data.get("title", "").strip():
        errors.append("Le titre de la note est requis.")
    if not data.get("content", "").strip():
        errors.append("Le contenu de la note est requis.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_note(
    data: dict,
    *,
    dav_id: Optional[str] = None,
    dav_uid: Optional[str] = None,
    _reserved_id: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors).

    The id and the VJOURNAL UID are minted here — an ``id`` or
    ``vjournal_uid`` inside *data* is DISCARDED, whoever sends it. Until
    2026-09-27 both were honoured and the document written with ``set()``:
    a forwarded ``id`` let a caller pick, and silently overwrite, an
    existing note — the DAV PUT reached this function whenever its
    FAIL-OPEN read missed the stored note, so a transient read error turned
    a phone edit into a full-document replacement.

    ``dav_id`` / ``dav_uid`` (keyword-only) serve ONE caller, the DAV PUT
    create branch (the ``create_task`` rule, lot 0b B3): the note is stored
    under the URL's resource name and the client's UID, written with
    ``create()`` — a note already stored under that id is refused
    (``[DAV_ID_TAKEN]``), never overwritten; an unusable name returns
    ``[DAV_ID_INVALID]``; ``dav_uid`` is kept only when it can be stored
    verbatim and is ignored without ``dav_id``. A ``created_at`` in *data*
    (a VJOURNAL's DTSTART) is still honoured.

    ``_reserved_id`` (private, finitions robustness-4) serves ONE caller,
    :func:`ensure_analyse_note`: the note is written with ``create()`` at
    that server-computed, deterministic id (:func:`analyse_note_id`), so an
    ``AlreadyExists`` (``[DAV_ID_TAKEN]``) tells a parallel initializer
    created it first — never a second note.
    """
    data = dict(data)
    data.pop("id", None)
    data.pop("vjournal_uid", None)
    if dav_id is not None and not dav_ids.valid_resource_id(dav_id):
        return None, [dav_ids.DAV_ID_INVALID]

    merged = {**_default_doc(), **_sanitize_data(data)}
    if merged.get("is_analyse"):
        # The théorie de la cause is a dateless jtx *Note*, on every path.
        # update_note ignores « dateless » for it, so an analyse note BORN
        # dated — a DAV create carrying X-PALLAS-ANALYSE and a DTSTART —
        # could never be corrected afterwards (lot 1a L3 review).
        merged["dateless"] = True

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    note_id = (dav_id if dav_id is not None
               else _reserved_id or str(uuid.uuid4()))
    vjournal_uid = (
        (dav_ids.client_uid(dav_uid) if dav_id is not None else None)
        or str(uuid.uuid4())
    )

    merged.update({
        "id": note_id,
        "vjournal_uid": vjournal_uid,
    })
    provenance.stamp_create(
        merged, now, created_at=merged.get("created_at") or now,
    )

    try:
        ref = db.collection(COLLECTION).document(note_id)
        if dav_id is not None or _reserved_id:
            ref.create(merged)
        else:
            ref.set(merged)
    except AlreadyExists:
        return None, [dav_ids.DAV_ID_TAKEN]
    except Exception:
        log_unexpected("note write failed")
        if dav_id is not None or _reserved_id:
            # A phone-chosen name: a document there may be a racing PUT's.
            return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
        # A FRESH id: read it back before answering (robustness-1).
        outcome, _stored = concurrency.settle_failed_create(ref)
        if outcome == concurrency.WRITE_UNKNOWN:
            return None, [concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR]
        if outcome == concurrency.WRITE_ABSENT:
            return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, note_id)

    return merged, []


def get_note_strict(note_id: str) -> Optional[dict]:
    """Fetch a single note by ID; a read failure PROPAGATES.

    ``None`` means the store answered « no such document » — never « the
    read failed ». For the DAV PUT, which routes a missing resource to its
    create branch: :func:`get_note`'s fail-open ``None`` would route a
    phone EDIT there on a transient read error.
    """
    doc = db.collection(COLLECTION).document(note_id).get()
    if doc.exists:
        return _migrate_category(doc.to_dict())
    return None


def get_note(note_id: str) -> Optional[dict]:
    """Fetch a single note by ID (fail-open: ``None`` on a read error)."""
    try:
        return get_note_strict(note_id)
    except Exception as exc:
        logger.warning("get_note failed for %s: %s", sanitize_log_value(note_id), exc)
    return None


def list_notes(
    dossier_id: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    pinned_first: bool = True,
    include_analyse: bool = False,
) -> list[dict]:
    """Return notes, pinned first then newest first.

    Search scans title + content (client-side, same as other modules).

    ``include_analyse`` — the « Théorie de la cause » note (``is_analyse``)
    is EXCLUDED by default so the Notes views never show it. The DAV
    collection paths and the MCP note tools MUST pass ``True``: a DAV
    caller left on the default silently drops the note from DavX5 (the
    client just stops seeing the resource — no error anywhere). Python
    filter on purpose — no Firestore index.
    """
    try:
        results = _raw_notes(dossier_id, include_analyse=include_analyse)

        # Client-side filters
        if category and category in VALID_CATEGORIES:
            results = [r for r in results if r.get("category") == category]

        if search:
            q = search.lower()
            results = [
                r for r in results
                if q in (r.get("title", "") or "").lower()
                or q in (r.get("content", "") or "").lower()
            ]

        # Sort: pinned first (if requested), then newest first
        results.sort(
            key=lambda n: (
                0 if pinned_first and n.get("pinned") else 1,
                -(n.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            ),
        )

        return results
    except Exception:
        return []


def _raw_notes(dossier_id: Optional[str], *, include_analyse: bool) -> list[dict]:
    """The note rows of one dossier (every note when *dossier_id* is falsy),
    migrated, the analyse note kept only when *include_analyse*.

    THE query body :func:`list_notes` (fail-open) and
    :func:`list_notes_strict` (propagates) share, so the two can never
    disagree about who belongs to a dossier. Raises on a read failure.
    """
    query = db.collection(COLLECTION)
    if dossier_id:
        query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))
    results = [_migrate_category(doc.to_dict()) for doc in query.stream()]
    if not include_analyse:
        results = [r for r in results if not r.get("is_analyse")]
    return results


def list_notes_strict(dossier_id: str, *, include_analyse: bool) -> list[dict]:
    """Every note of ONE dossier — a read failure PROPAGATES.

    For a caller that WRITES on the strength of the answer: the DavX5 drain
    of a closing dossier (``services/dossier_dav.dav_member_ids``) tombstones
    exactly what this returns, and :func:`list_notes` answers a Firestore
    blip with ``[]`` — a drain built on it tombstoned nothing and stranded
    every note on the phone.

    *include_analyse* is REQUIRED, with no default (the ``include_analyse``
    lesson): a DAV caller must decide consciously, since the default of
    :func:`list_notes` drops the « Théorie de la cause » note — which the
    collection DOES list — and a drain on it would strand that one note.

    An empty or blank *dossier_id* is REFUSED before any query: the shared
    body reads EVERY note for a falsy id. Unordered.
    """
    if not isinstance(dossier_id, str) or not dossier_id.strip():
        raise ValueError("list_notes_strict needs a dossier id")
    return _raw_notes(dossier_id, include_analyse=include_analyse)


def list_notes_without_dossier_strict(*, include_analyse: bool) -> list[dict]:
    """Every note with NO dossier — the DAV « Général » collection — a read
    failure PROPAGATES (the :func:`list_notes` ``[]`` served an empty
    collection). *include_analyse* is REQUIRED, with no default: the DAV
    paths list the analyse note (the ``include_analyse`` lesson). Unordered.
    """
    return [n for n in _raw_notes(None, include_analyse=include_analyse)
            if not n.get("dossier_id")]


# Bounded read caps for the default /notes/ list view (no search/category
# filter). Pinned notes are a small curated set; the recent-unpinned cap
# covers day-to-day browsing. Older notes stay reachable via search.
PINNED_LIMIT = 50
RECENT_LIMIT = 100


def list_notes_recent(
    dossier_id: Optional[str] = None,
    pinned_limit: int = PINNED_LIMIT,
    recent_limit: int = RECENT_LIMIT,
    include_analyse: bool = False,
) -> list[dict]:
    """Return pinned notes plus the most recent unpinned notes, bounded.

    Two server-side queries (``pinned == True`` then ``pinned == False``,
    each ordered by ``created_at`` descending and limited) replace the
    full-collection stream of :func:`list_notes` for the default list view.
    The concatenation preserves the legacy pinned-first / newest-first
    display order with at most ``pinned_limit + recent_limit`` reads.

    Requires the composite index (pinned ASC, created_at DESC) and, when
    *dossier_id* is given, (dossier_id ASC, pinned ASC, created_at DESC).

    ``include_analyse`` — same contract as :func:`list_notes`.

    Returns [] on failure (the list view degrades to an empty state).
    """
    try:
        results: list[dict] = []
        for pinned, limit in ((True, pinned_limit), (False, recent_limit)):
            query = db.collection(COLLECTION).where(
                filter=FieldFilter("pinned", "==", pinned)
            )
            if dossier_id:
                query = query.where(
                    filter=FieldFilter("dossier_id", "==", dossier_id)
                )
            query = query.order_by(
                "created_at", direction=firestore.Query.DESCENDING
            ).limit(limit)
            results.extend(
                _migrate_category(doc.to_dict()) for doc in query.stream()
            )
        if not include_analyse:
            results = [r for r in results if not r.get("is_analyse")]
        return results
    except Exception as exc:
        logger.warning("list_notes_recent: query failed: %s", exc)
        return []


# The refusal when a write would move the « Théorie de la cause » out of its
# dossier. ONE constant: routes/notes.py keeps its own identical guard (belt
# and braces, tests/test_notes_general.py) and says the same words.
ANALYSE_DOSSIER_LOCKED_ERROR = (
    "La note d'analyse est liée à son dossier — le dossier ne peut pas être "
    "modifié."
)

# The revision field a content replacement is filed under when the caller
# names none — a web save, a DAV PUT, an append: the whole body replaced.
CONTENT_REVISION_FIELD = "content"

# A ``revision`` field outside ``models.revision.VALID_FIELDS`` (or the
# delete snapshot's) is a programming error: refused, nothing written.
_REVISION_FIELD_ERROR = (
    "Remplacement refusé : champ de révision inconnu. Rien n'a été "
    "enregistré."
)

# How many times a caller that asserted NO version (``expected_etag=None`` —
# a DAV PUT, a page rendered before its form carried an etag) is re-read and
# re-merged when a write lands between the model's read and its guarded
# commit. The revision needs that guard (it must snapshot exactly what the
# write replaces), but such a caller never asked to be refused: it keeps
# its last-write-wins behaviour, now with the text it overwrote on record.
_UNGUARDED_ATTEMPTS = 3


def _same_text(a: str, b: str) -> bool:
    """True when two note bodies differ at most by their line endings.

    A browser submits a textarea with CRLF, and the phone hands the same
    text back with LF (RFC 5545 escapes a line break as a backslash-n): a jtx
    edit of a note's CATEGORY alone re-sends a web-authored body that is
    byte-different and word-for-word identical. That is no replacement, and
    it must not file a revision the lawyer would take for an edit.
    """
    def _lf(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    return a == b or _lf(a) == _lf(b)


def update_note(
    note_id: str,
    data: dict,
    *,
    expected_etag: Optional[str] = None,
    revision: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Update an existing note. Returns (updated_doc, errors).

    ``expected_etag`` (keyword-only): when given, the write commits only if
    the stored etag is still that one (``models.concurrency``); a stale one
    returns ``[STALE_ETAG_ERROR]`` and writes nothing — which is what keeps
    a stale edit tab from erasing a block appended since it opened. ``None``
    (DAV PUT, a page rendered before its form carried an etag) asserts no
    version: it is the unchanged single ``set()`` when the content does not
    change, and last-write-wins when it does (below).

    EVERY replacement of the stored content keeps a revision (D17,
    2026-09-27 — until then only the connector's tools asked for one): the
    web form, the DAV PUT from the phone, ``append_to_note``,
    ``update_note``, ``edit_analyse``. When the content actually changes,
    the WHOLE content it replaces is snapshotted write-once into
    ``notes/{id}/revisions/`` in the SAME transaction as the replacement
    (plan rule 4): both commit or neither does. The commit is therefore
    always the GUARDED one — against ``expected_etag`` when the caller named
    a version, else against the etag this function just read, so the
    snapshot is exactly the text overwritten; a caller that named none and
    loses that race is re-read and re-merged (:data:`_UNGUARDED_ATTEMPTS`)
    rather than refused. An unchanged content — line endings aside
    (:func:`_same_text`: the phone hands a web-authored body back in LF) —
    writes no snapshot, and the write keeps its legacy shape.

    ``revision`` (keyword-only) only NAMES what was replaced
    (``"content:rewrite"``, ``"bloc:C"``…); omitted, the snapshot is filed
    under :data:`CONTENT_REVISION_FIELD`. A name outside
    ``models.revision.VALID_FIELDS`` — or the delete snapshot's — is
    refused, nothing written. The returned document carries the snapshot's
    id under ``_revision_id`` — a transient key, never stored. The snapshot
    holds the whole previous content whatever the field names: a restore
    needs nothing else.

    The rules of the théorie de la cause live HERE, on every path — web,
    DAV PUT, connector:

    * ``is_analyse`` is never taken from *data*: an update can neither
      demote the dossier's analyse note (a client stripping the X-property
      only omits the key, but a hand-crafted ``false`` would not) nor
      promote an ordinary note into a second one;
    * the analyse note never leaves its dossier — neither for another one
      nor for « Général » (:data:`ANALYSE_DOSSIER_LOCKED_ERROR`): a moved
      analyse note is invisible in every app view;
    * the analyse note stays dateless (``dateless`` is ignored for it). An
      ORDINARY note keeps the DAV round trip ``vjournal_to_note`` feeds:
      turned in jtx from a Note into a dated Journal entry (DTSTART added)
      it becomes dated, and back.
    """
    field = CONTENT_REVISION_FIELD if revision is None else revision
    if (field not in revision_model.VALID_FIELDS
            or field == revision_model.DELETE_FIELD):
        return None, [_REVISION_FIELD_ERROR]

    attempts = 1 if expected_etag is not None else _UNGUARDED_ATTEMPTS
    for _attempt in range(attempts):
        # STRICT (finitions, sync-3): a failed read is never « introuvable ».
        try:
            existing = get_note_strict(note_id)
        except Exception:
            log_unexpected("note update: read failed", note_id=note_id)
            return None, [concurrency.READ_UNAVAILABLE_ERROR]
        if not existing:
            return None, ["Note introuvable."]
        if not concurrency.matches(existing, expected_etag):
            return None, [concurrency.STALE_ETAG_ERROR]

        changes = dict(data)
        changes.pop("is_analyse", None)
        if existing.get("is_analyse"):
            changes.pop("dateless", None)
            if "dossier_id" in changes and (
                changes.get("dossier_id") or ""
            ) != (existing.get("dossier_id") or ""):
                return None, [ANALYSE_DOSSIER_LOCKED_ERROR]

        merged = {**existing, **_sanitize_data(changes)}

        errors = _validate(merged)
        if errors:
            return None, errors

        now = datetime.now(timezone.utc)
        provenance.stamp_update(merged, now)

        read_etag = concurrency.etag_of(existing)
        extra_sets: list = []
        revision_id = ""
        previous_content = existing.get("content", "") or ""
        if not _same_text(merged.get("content") or "", previous_content):
            try:
                rev_ref, rev_data = revision_model.build_revision(
                    parent_collection=COLLECTION,
                    parent_id=note_id,
                    field=field,
                    previous_value=previous_content,
                    previous_etag=read_etag,
                    new_etag=merged["etag"],
                    now=now,
                )
            except revision_model.RevisionRefused:
                log_unexpected("note revision refused", field=field)
                return None, [
                    "Erreur lors de la sauvegarde. Veuillez réessayer."]
            extra_sets.append((rev_ref, rev_data))
            revision_id = rev_data["id"]

        # A revision travels ONLY on the guarded commit (models/revision.py):
        # a caller that named no version is guarded on the version just read.
        guard = expected_etag
        if extra_sets and guard is None:
            guard = read_etag

        try:
            concurrency.commit_document(
                db.collection(COLLECTION).document(note_id), merged,
                expected_etag=guard,
                read_etag=read_etag,
                extra_sets=extra_sets,
            )
        except concurrency.StaleWrite:
            if expected_etag is None:
                # This function guarded the commit ITSELF, for the revision:
                # the caller asked for last-write-wins — read again.
                continue
            return None, [concurrency.STALE_ETAG_ERROR]
        except concurrency.Vanished:
            return None, ["Note introuvable."]
        except Exception:
            log_unexpected("note write failed")
            return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
        provenance.note_commit(COLLECTION, note_id)

        if revision_id:
            return {**merged, "_revision_id": revision_id}, []
        return merged, []

    log_unexpected("note write lost its race on every attempt",
                   exc_info=False, note_id=note_id)
    return None, [concurrency.STALE_ETAG_ERROR]


def delete_note(
    note_id: str, *, deleted_out: Optional[dict] = None,
) -> tuple[bool, str]:
    """Delete a note. Returns (success, error_message).

    The dossier's « Théorie de la cause » is never deleted bare: its content
    is first snapshotted write-once into ``notes/{id}/revisions/`` (field
    ``content:delete``), and the snapshot and the delete commit in ONE
    transaction guarded by the etag just read — so the snapshot is exactly
    what disappeared, and a delete racing an edit is refused
    (``STALE_ETAG_ERROR``) instead of losing the edit unsnapshotted. The
    revisions OUTLIVE the note: Firestore does not cascade, and nothing in
    the application deletes them (the ``documents/{id}/analyses`` journal
    doctrine), so a deleted analysis stays recoverable. Ordinary notes keep
    the plain delete. Callers on every path — the web route and the DAV
    DELETE — go through here, so neither can skip the snapshot.

    ``deleted_out`` (keyword-only, finitions sync-4): when given, receives
    the document this call READ and deleted — the version whose
    ``dossier_id`` names the DAV collection to tombstone. A caller must take
    the collection from HERE, never from its own earlier read: that read is
    a separate round trip (a fail-open one in the web routes, whose blip
    read « no dossier » and tombstoned « Général » instead, the deleted item
    staying on the phone for good). The read is STRICT: a failure answers
    ``concurrency.READ_UNAVAILABLE_ERROR``, never « introuvable »."""
    try:
        existing = get_note_strict(note_id)
    except Exception:
        log_unexpected("note delete: read failed", note_id=note_id)
        return False, concurrency.READ_UNAVAILABLE_ERROR
    if not existing:
        return False, "Note introuvable."

    ref = db.collection(COLLECTION).document(note_id)
    if existing.get("is_analyse"):
        ok, error = _delete_analyse_note(ref, note_id, existing)
        if ok and deleted_out is not None:
            deleted_out.update(existing)
        return ok, error

    try:
        ref.delete()
        if deleted_out is not None:
            deleted_out.update(existing)
        return True, ""
    except Exception:
        log_unexpected("note delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


def _delete_analyse_note(ref, note_id: str, existing: dict) -> tuple[bool, str]:
    """Snapshot the analyse note's content, then delete it — atomically."""
    etag = concurrency.etag_of(existing)
    try:
        rev_ref, rev_data = revision_model.build_revision(
            parent_collection=COLLECTION,
            parent_id=note_id,
            field=revision_model.DELETE_FIELD,
            previous_value=existing.get("content", "") or "",
            previous_etag=etag,
            new_etag="",
            now=datetime.now(timezone.utc),
        )
        concurrency.commit_delete(
            ref, expected_etag=etag, read_etag=etag,
            extra_sets=[(rev_ref, rev_data)],
        )
    except concurrency.StaleWrite:
        return False, concurrency.STALE_ETAG_ERROR
    except concurrency.Vanished:
        return False, "Note introuvable."
    except Exception:
        log_unexpected("analyse note delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."
    return True, ""


def set_pinned(
    note_id: str, pinned: bool
) -> tuple[Optional[dict], list[str], bool]:
    """Pin or unpin a note — SET to a target, never a toggle.

    Returns ``(doc, errors, changed)``. A note already in the requested
    state writes NOTHING (``changed`` False: no etag churn, and the caller
    bumps no CTag, so a phone is not woken for an unchanged note). That is
    what makes a stale page harmless: a button rendered « Épingler » that
    is clicked after another tab pinned the note asks for what is already
    true, and a toggle would have UNPINNED it.

    The write is a partial ``update()`` of ``pinned`` and its stamp — never
    the merged full-document ``set()`` of ``update_note``, which would
    rewrite the content the page read (a block the connector appended since
    it opened, say) along with the flag. A partial update cannot erase what
    it does not name, so it needs no etag.
    """
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."], False
    pinned = bool(pinned)
    if bool(existing.get("pinned", False)) == pinned:
        return existing, [], False

    stamp = provenance.update_fields(datetime.now(timezone.utc))
    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(note_id),
            {"pinned": pinned, **stamp},
            expected_etag=None,
        )
    except NotFound:
        return None, ["Note introuvable."], False
    except Exception:
        log_unexpected("note pin write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], False
    provenance.note_commit(COLLECTION, note_id)
    return {**existing, "pinned": pinned, **stamp}, [], True


def toggle_pin(note_id: str) -> tuple[Optional[dict], list[str], bool]:
    """Flip the pinned status — for a caller that did not say which way.

    Only a page rendered before the pin button posted its target reaches
    this (routes/notes.note_pin); every current caller names the state it
    wants and goes through :func:`set_pinned`.
    """
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."], False
    return set_pinned(note_id, not existing.get("pinned", False))


# ── Théorie de la cause (feuille « Analyse ») ────────────────────────────

# The SUMMARY of the single analyse note (shown as-is in jtx Board). The
# sheet's tab label is « Analyse »; the note keeps its full name.
ANALYSE_TITLE = "Théorie de la cause"

# Seed content: the 8-block « Gabarit B » working template (méthode
# d'élaboration de la théorie d'une cause, École du Barreau). Verbatim from
# SPEC_Analyse_theorie_de_la_cause.md — Annexe A; edit the spec first if the
# template must change. Markdown tables + « ☐ » checkboxes render through the
# `markdown` filter (tables extension already active).
_ANALYSE_SEED = """\
# Théorie de la cause

*Dossier : … | Partie représentée : ☐ Demandeur ☐ Défendeur ☐ Mis en cause | Rédigé par : … | Date de l'analyse : …*

Outil de travail interne (méthode d'élaboration de la théorie d'une cause, version complète et stratégique). Les blocs F et G — forces/faiblesses et théorie adverse — n'ont pas vocation à être versés au dossier de la Cour.

---

## Bloc A — Identification et cadre procédural

### Parties et leur qualité

| Partie | Rôle | Qualité / capacité / intérêt (art. 85 C.p.c.) |
|---|---|---|
| … | … | … |

### Cadre procédural

Tribunal et compétence d'attribution : …
District (compétence territoriale) : …
Montant ou valeur en jeu : …
Voie procédurale envisagée : …

### Verrous préliminaires

- ☐ **Prescription** — délai applicable : … *(à défaut de délai particulier, 3 ans : art. 2925 C.c.Q.)* — point de départ : … — date pour agir : …
- ☐ Intérêt et qualité pour agir (art. 85 C.p.c.)
- ☐ Compétence (matière et territoire)
- ☐ Mise en demeure / avis préalable requis ou envoyé
- ☐ Autres conditions de recevabilité : …

*Questions-repères : le client a-t-il l'intérêt et la qualité requis ? Le recours est-il encore dans les délais ? Le bon tribunal est-il saisi ? Une démarche préalable est-elle exigée ?*

---

## Bloc B — Les faits

### Récit chronologique

…

### Cartographie des faits

| Fait | Générateur du droit ? | Admis / non contesté | Contesté (à prouver) | Défavorable |
|------|:---:|:---:|:---:|:---:|
| … | ☐ | ☐ | ☐ | ☐ |

### Faits défavorables à gérer

(comment les neutraliser ou les expliquer) …

### Faits manquants ou à investiguer

(documents, témoins, expertises à obtenir) …

*Questions-repères : quels faits font naître le droit invoqué ? Lesquels l'autre partie admettra-t-elle ? Quels faits me nuisent, et comment les aborder de front ? Que dois-je encore aller chercher ?*

---

## Bloc C — Le fondement juridique et ses éléments constitutifs

### Fondement principal

Cause d'action (ou, en défense, moyen principal opposé) : …
Sources : ☐ législation … ☐ jurisprudence … ☐ doctrine …

### Fondements subsidiaires

(fondements de rechange, et pourquoi chacun est subsidiaire) …

*Un fondement subsidiaire qui survit à la chute du principal est un actif : le noter comme tel.*

### Éléments constitutifs à réunir

*Exemple — responsabilité civile : faute, préjudice, lien de causalité (art. 1457 C.c.Q. extracontractuel ; art. 1458 C.c.Q. contractuel).*

| Élément constitutif | Fait(s) qui l'établit | Preuve disponible | Solide ? |
|---|---|---|:--:|
| … | … | … | ☐ |
| … | … | … | ☐ |
| … | … | … | ☐ |

### Moyens de défense / d'exception envisageables

(les miens et ceux de l'adversaire) …

*Questions-repères : ai-je isolé chacune des conditions que la loi exige ? Chaque condition est-elle appuyée par un fait et par une preuve ? Une seule condition non établie fait-elle échouer le recours ? Si le fondement principal tombe, que reste-t-il ?*

---

## Bloc D — Qualification et syllogisme

**Majeure (la règle) :** …

**Mineure (les faits qualifiés) :** …

**Conclusion (l'application) :** …

### Qualification juridique retenue

(nature exacte du rapport ou de l'acte) …

*Questions-repères : chaque condition de la règle trouve-t-elle appui dans un fait ? Un fait vient-il contredire l'application de la règle ?*

---

## Bloc E — La stratégie de preuve

### Fardeau et norme

Fardeau de preuve — qui doit prouver quoi (art. 2803 C.c.Q.) : …
Norme applicable : prépondérance des probabilités (art. 2804 C.c.Q.), sauf exigence légale plus stricte : …

### Moyens de preuve

*Art. 2811 C.c.Q. : écrit, témoignage, présomption, aveu, présentation d'un élément matériel.*

| Élément / fait à prouver | Sur qui repose le fardeau | Moyen de preuve prévu | Source / pièce / témoin | Lacune |
|---|---|---|---|---|
| … | … | … | … | … |

*Questions-repères : pour chaque fait contesté, ai-je un moyen de preuve ? La preuve est-elle admissible et disponible ? Où sont mes trous de preuve, et comment les combler ? Quelle preuve l'adversaire opposera-t-il ?*

---

## Bloc F — Analyse critique

### Forces de ma position

- …

### Faiblesses et risques

- …

### Théorie adverse anticipée

(prétentions probables de la partie adverse — faits, fondement, preuve — et ma réponse à chacune)

| Prétention adverse anticipée | Ma réponse / parade |
|---|---|
| … | … |

*Questions-repères : si j'étais l'avocat de l'autre partie, quelle serait ma meilleure théorie ? Quel est le maillon le plus faible de ma cause ? Résiste-t-elle au contre-interrogatoire et au scénario adverse le plus favorable ?*

---

## Bloc G — La théorie de la cause (synthèse persuasive)

### Théorie factuelle

(le récit, cohérent et favorable, de ce qui s'est passé) …

### Théorie juridique

(le fondement de droit qui commande le résultat recherché) …

### Le thème

(l'idée-force, l'angle d'équité ou de bon sens qui donne au tribunal une raison de trancher en ma faveur) …

### Énoncé de la théorie (une à deux phrases)

> « … »

*Test de solidité : la théorie est-elle cohérente (sans contradiction interne), crédible (conforme au bon sens et à l'expérience), complète (elle absorbe même les faits défavorables) et simple (mémorable, exprimable en une phrase) ?*

---

## Bloc H — Conclusions recherchées et suites

### Conclusions recherchées

(remèdes précis, tels qu'ils devront être formulés à l'acte de procédure — clarté, précision, concision, ordre logique et numérotation : art. 99 C.p.c.)

1. …
2. …

### Objectifs réels du client

(et scénarios de règlement acceptables) …

### Prochaines étapes et échéancier

…

### Éléments encore à obtenir

(preuve, expertise, mandat, provision) …
"""

# The seed, public: the connector compares each bloc against it to report
# which ones are still the template (``mcp/handlers._analyse_structure``).
ANALYSE_SEED = _ANALYSE_SEED


def get_analyse_note(dossier_id: str) -> Optional[dict]:
    """Return the dossier's single ``is_analyse`` note, or ``None``.

    FOR DISPLAY ONLY. It flows through :func:`list_notes`, which swallows a
    read error into ``[]``, so ``None`` may mean « the read failed », and a
    duplicate resolves silently to the first match. A WRITE path that must
    know whether the note exists uses :func:`find_analyse_note_strict`.

    Python scan over the per-dossier list (deliberately no
    ``.where("is_analyse", ...)`` — that would need a composite index
    deployed before the code).
    """
    for note in list_notes(dossier_id=dossier_id, include_analyse=True):
        if note.get("is_analyse"):
            return note
    return None


def has_analyse(dossier_id: str) -> bool:
    """True when the dossier already has its « Théorie de la cause » note
    (display helper — fail-open, see :func:`get_analyse_note`)."""
    return get_analyse_note(dossier_id) is not None


class AnalyseLookupError(Exception):
    """The analyse-note lookup could not answer: nothing may be written."""


class AnalyseDuplicateError(AnalyseLookupError):
    """The dossier holds more than one « Théorie de la cause » note."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(f"{count} analyse notes")


ANALYSE_DUPLICATE_ERROR = (
    "Ce dossier compte plusieurs notes « Théorie de la cause » ; il ne doit y "
    "en avoir qu'une. Rien n'a été créé ni modifié : gardez-en une seule "
    "(les autres sont visibles au téléphone, dans jtx Board), puis "
    "réessayez."
)
ANALYSE_READ_ERROR = "Erreur de lecture. Veuillez réessayer."


def find_analyse_note_strict(dossier_id: str) -> Optional[dict]:
    """The dossier's analyse note, read to FAIL CLOSED — for write paths.

    ``None`` only when the store answered « there is none ». Raises
    :class:`AnalyseLookupError` when the read fails, and
    :class:`AnalyseDuplicateError` (a subclass) when there are several: a
    write must never pick one of two analyses at random, nor seed a third
    because a transient error read as « none yet ». One equality query on
    ``dossier_id`` (automatic index), filtered in Python.
    """
    if not dossier_id:
        return None
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        rows = [snap.to_dict() or {} for snap in query.stream()]
        found = [_migrate_category(r) for r in rows if r.get("is_analyse")]
    except Exception as exc:
        raise AnalyseLookupError("analyse lookup failed") from exc
    if len(found) > 1:
        raise AnalyseDuplicateError(len(found))
    return found[0] if found else None


# The namespace of the analyse note's deterministic id (finitions,
# robustness-4) — frozen: changing it would orphan every analyse note
# created since (they would no longer be found by id; the dossier_id query
# still finds them, so nothing is lost, but the fork guard would weaken).
_ANALYSE_ID_NAMESPACE = uuid.UUID("5b3f0c1e-7d2a-4a8e-9c61-2f4e8a9d7b30")


def analyse_note_id(dossier_id: str) -> str:
    """The deterministic id a dossier's théorie de la cause is CREATED at.

    A documented exception to Architecture Rule 6 (UUIDv4 ids, never
    reused): a UUIDv5 of the dossier — the system folders' precedent
    (``models.folder.system_folder_id``). Two initializers racing (the web
    « Ajouter une théorie de la cause » and a connector ``edit_analyse``
    init, two inits under different keys) can then only ever target ONE
    document, whose ``create()`` the second loses. A théorie deleted and
    re-initialized is recreated at the same id (its tombstone is removed on
    creation) — and inherits what the deleted one left under that id: its
    ``revisions`` subcollection (write-once, outliving the delete by
    design, the ``content:delete`` snapshot included) reads as the new
    note's history, and ``audit_events`` keeps a ``note`` deletion for an id
    that exists again. Legacy analyse notes keep their uuid4 id: the
    existence check queries by dossier, never by this id.
    """
    return str(uuid.uuid5(_ANALYSE_ID_NAMESPACE, f"{dossier_id}:analyse"))


def ensure_analyse_note(
    dossier_id: str,
) -> tuple[Optional[dict], list[str], bool]:
    """The dossier's single analyse note, created pre-seeded if absent.

    Returns ``(note, errors, created)``: ``created`` is True ONLY when this
    call wrote the note, which is what lets the caller bump the CTag on an
    actual creation and never on a « found » (the house rule keeps the bump
    in the route). IDEMPOTENT: a re-clicked init button, or a replayed
    connector call, finds the note and writes nothing.

    Fails CLOSED through :func:`find_analyse_note_strict`: a read error or
    a duplicate returns an error and writes nothing — the fail-open
    :func:`get_analyse_note` would have read an outage as « no note yet »
    and seeded a duplicate over the lawyer's filled analysis. The writer's
    provenance (``created_via``) comes from ``models.provenance``'s context
    — the request's blueprint, or the connector's ``writing_via`` — never
    from an argument (Architecture Rule 5's lot-0a corollary: a caller never
    sets it).
    """
    try:
        existing = find_analyse_note_strict(dossier_id)
    except AnalyseDuplicateError:
        return None, [ANALYSE_DUPLICATE_ERROR], False
    except AnalyseLookupError:
        log_unexpected("analyse existence check failed")
        return None, [ANALYSE_READ_ERROR], False
    if existing:
        return existing, [], False

    from models.dossier import get_dossier

    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        return None, ["Dossier introuvable."], False

    note, errors = create_note({
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": ANALYSE_TITLE,
        "content": _ANALYSE_SEED,
        "category": "stratégie",
        "pinned": False,
        "dateless": True,
        "is_analyse": True,
    }, _reserved_id=analyse_note_id(dossier_id))
    if errors == [dav_ids.DAV_ID_TAKEN]:
        # A parallel initializer created it between our existence check and
        # our create(): the id is deterministic, so it is THE note — read it
        # back (finitions, robustness-4). Before, both callers minted a
        # fresh uuid4 and FORKED the théorie: every later edit refused as a
        # duplicate until the lawyer deleted one by hand.
        try:
            existing = find_analyse_note_strict(dossier_id)
        except AnalyseDuplicateError:
            return None, [ANALYSE_DUPLICATE_ERROR], False
        except AnalyseLookupError:
            log_unexpected("analyse read-back failed")
            return None, [ANALYSE_READ_ERROR], False
        if existing:
            return existing, [], False
        return None, [ANALYSE_READ_ERROR], False
    return note, errors, bool(note is not None and not errors)


def create_analyse_note(dossier_id: str) -> tuple[Optional[dict], list[str]]:
    """``(note, errors)`` of :func:`ensure_analyse_note` — kept for callers
    that do not need to know whether the note was created. It cannot tell
    « found » from « created »: a caller that bumps a CTag uses
    :func:`ensure_analyse_note`."""
    note, errors, _created = ensure_analyse_note(dossier_id)
    return note, errors


# ── Summary ──────────────────────────────────────────────────────────────


def _find_note_by_vjournal_uid(vjournal_uid: str) -> Optional[dict]:
    """Find a note by its VJOURNAL UID. Used for RELATED-TO resolution."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("vjournal_uid", "==", vjournal_uid)
        ).limit(1)
        for doc in query.stream():
            return doc.to_dict()
    except Exception as exc:
        logger.warning(
            "_find_note_by_vjournal_uid failed for %s: %s",
            sanitize_log_value(vjournal_uid), exc,
        )
    return None


def get_notes_summary(dossier_id: str) -> dict:
    """Return {total} for the MCP get_dossier summary (its only caller).

    Includes the analyse note: the MCP read paths expose it, so the count
    must agree with what the MCP list_notes tool returns.
    """
    notes = list_notes(dossier_id=dossier_id, include_analyse=True)
    return {"total": len(notes)}


# ── RFC-5545 VJOURNAL serialization ─────────────────────────────────────


def note_to_vjournal(note: dict) -> str:
    """Serialize a note to an RFC-5545 VJOURNAL string wrapped in VCALENDAR.

    Properties:
    - UID: note's vjournal_uid
    - SUMMARY: note title
    - DESCRIPTION: note content
    - DTSTART: note created_at (date only) — OMITTED when ``dateless`` is
      set: a VJOURNAL without DTSTART is a pure *Note* in jtx Board instead
      of a dated *Journal* entry. CREATED/DTSTAMP stay unconditional (the
      jtx icalobject.created NOT-NULL trap).
    - CATEGORIES: note category label (French)
    - STATUS: FINAL (notes are always finalized records)
    - LAST-MODIFIED: note updated_at
    - SEQUENCE: 0
    - X-PALLAS-NOTE-CATEGORY: category key (for round-trip fidelity)
    - X-PALLAS-DOSSIER-ID: dossier_id
    - X-PALLAS-ANALYSE: "true" when the note is the théorie de la cause
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Note//FR")
    cal.add("version", "2.0")

    journal = icalendar.Journal()
    journal.add("uid", note.get("vjournal_uid", ""))
    journal.add("summary", note.get("title", ""))

    if note.get("content"):
        journal.add("description", note["content"])

    created = note.get("created_at")
    if created and hasattr(created, "date") and not note.get("dateless"):
        journal.add("dtstart", created.date())

    # CREATED + DTSTAMP as UTC date-times. Required for jtx Board: its
    # icalobject.created column is NOT NULL, and DavX5/ical4android writes
    # null (SQLITE_CONSTRAINT_NOTNULL on update) when the VJOURNAL omits
    # CREATED. DTSTAMP is mandatory per RFC 5545 §3.6.3.
    if created and hasattr(created, "hour"):
        journal.add("created", _to_utc(created))
    stamp = note.get("updated_at") or created
    if stamp and hasattr(stamp, "hour"):
        journal.add("dtstamp", _to_utc(stamp))

    journal.add("status", "FINAL")

    if note.get("category"):
        label = CATEGORY_LABELS.get(note["category"], note["category"])
        journal.add("categories", [label])

    updated = note.get("updated_at")
    if updated:
        journal.add("last-modified", updated)

    journal.add("sequence", 0)

    # Custom X- properties
    if note.get("category"):
        journal.add("x-pallas-note-category", note["category"])
    if note.get("dossier_id"):
        journal.add("x-pallas-dossier-id", note["dossier_id"])
    if note.get("pinned"):
        journal.add("x-pallas-pinned", "true")
    if note.get("is_analyse"):
        journal.add("x-pallas-analyse", "true")

    cal.add_component(journal)
    return cal.to_ical().decode("utf-8")


def vjournal_to_note(ical_str: str) -> dict:
    """Parse a VJOURNAL string into a note dict (for DAV PUT).

    Extracts standard properties and X-PALLAS-* custom properties.
    """
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VJOURNAL":
            continue

        uid = component.get("uid")
        if uid:
            data["vjournal_uid"] = str(uid)

        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        desc = component.get("description")
        if desc:
            data["content"] = str(desc)

        dtstart = component.get("dtstart")
        # A VJOURNAL without DTSTART is a pure note — record that so a PUT
        # round-trip through jtx never re-dates the analyse note. When
        # DTSTART is present the note is (back to) a dated journal entry.
        data["dateless"] = dtstart is None
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                data["created_at"] = dt
            else:
                data["created_at"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # X- properties
        category = component.get("x-pallas-note-category")
        if category:
            cat = str(category)
            if cat in VALID_CATEGORIES:
                data["category"] = cat

        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        pinned = component.get("x-pallas-pinned")
        if pinned and str(pinned).lower() == "true":
            data["pinned"] = True

        # Never set is_analyse=False here: a client that strips unknown
        # X- properties must not demote the stored flag — update_note's
        # merge keeps the existing value when the key is absent.
        analyse = component.get("x-pallas-analyse")
        if analyse and str(analyse).lower() == "true":
            data["is_analyse"] = True

        break  # Only process first VJOURNAL

    return data
