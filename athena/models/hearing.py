"""Hearing (court date) Firestore CRUD and RFC-5545 VEVENT serialization."""

import logging
import unicodedata
import uuid
from datetime import date, datetime, time, timezone, timedelta
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import icalendar

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import concurrency, dav_ids, db, provenance
from security import sanitize
from tz import MTL, mtl_to_utc, to_mtl
from utils import deadlines
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "hearings"

# Two-tier hearing-type vocabulary (2026-07-24). The forum (judiciaire /
# extrajudiciaire) is DERIVED from the type, never stored (the two lists are
# disjoint) — see forum_of(). Tuple ORDER is the <option> display order.
VALID_HEARING_TYPES_JUDICIAIRE = (
    "conférence_de_gestion",
    "conférence_de_règlement",
    "conférence_préparatoire",
    "audience",
    "instruction",
)
VALID_HEARING_TYPES_EXTRAJUDICIAIRE = (
    "consultation",
    "rencontre",
    "conférence",
    "interrogatoire",
    "autre",
)
# Full domain — what _validate accepts.
VALID_HEARING_TYPES = (
    VALID_HEARING_TYPES_JUDICIAIRE + VALID_HEARING_TYPES_EXTRAJUDICIAIRE
)

VALID_FORUMS = ("judiciaire", "extrajudiciaire")
FORUM_LABELS = {
    "judiciaire": "Judiciaire",
    "extrajudiciaire": "Extrajudiciaire",
}

# Event modality (2026-07-24).
VALID_MODALITES = ("présentiel", "visioconférence", "téléphonique")
MODALITE_LABELS = {
    "présentiel": "Présentiel",
    "visioconférence": "Visioconférence",
    "téléphonique": "Téléphonique",
}

VALID_STATUSES = (
    "confirmée",
    "à_confirmer",
    "reportée",
    "annulée",
    "terminée",
)
VALID_REMINDER_MINUTES = (15, 30, 60, 120, 1440, 2880, 10080)

# Display labels (French). « conférence » (extrajudiciaire) is a strict PREFIX
# of the three judicial « conférence_… » keys — only dict access / strict
# equality anywhere, never startswith("conférence").
HEARING_TYPE_LABELS = {
    "conférence_de_gestion": "Conférence de gestion",
    "conférence_de_règlement": "Conférence de règlement à l'amiable",
    "conférence_préparatoire": "Conférence préparatoire",
    "audience": "Audience",
    "instruction": "Instruction",
    "consultation": "Consultation",
    "rencontre": "Rencontre",
    "conférence": "Conférence",
    "interrogatoire": "Interrogatoire",
    "autre": "Autre",
}
STATUS_LABELS = {
    "confirmée": "Confirmée",
    "à_confirmer": "À confirmer",
    "reportée": "Reportée",
    "annulée": "Annulée",
    "terminée": "Terminée",
}
REMINDER_LABELS = {
    15: "15 minutes",
    30: "30 minutes",
    60: "1 heure",
    120: "2 heures",
    1440: "24 heures",
    2880: "48 heures",
    10080: "1 semaine",
}

# Hearing type → suggested color for calendar display. Bare tint names; the
# templates' Tailwind class dicts must stay in sync. Every tint is already in
# the compiled CSS artifact (no recompile). purple is a DELIBERATE duplicate
# (conférence_préparatoire / conférence — distinct forums, nine tints for ten
# types).
HEARING_TYPE_COLORS = {
    "conférence_de_gestion": "blue",
    "conférence_de_règlement": "teal",
    "conférence_préparatoire": "purple",
    "audience": "indigo",
    "instruction": "red",
    "consultation": "green",
    "rencontre": "orange",
    "conférence": "purple",
    "interrogatoire": "amber",
    "autre": "gray",
}


# Type → forum (« extrajudiciaire » by default). The forum is fully derived
# from the type; no Firestore field, no migration, no drift between two fields.
_TYPE_FORUM = {
    **{t: "judiciaire" for t in VALID_HEARING_TYPES_JUDICIAIRE},
    **{t: "extrajudiciaire" for t in VALID_HEARING_TYPES_EXTRAJUDICIAIRE},
}


def forum_of(hearing_type: str) -> str:
    """Forum of a hearing type, « extrajudiciaire » by default."""
    return _TYPE_FORUM.get(hearing_type or "", "extrajudiciaire")


def is_safe_conference_uri(uri: str) -> bool:
    """True when *uri* is a syntactically valid http/https URL.

    conference_uri is rendered as an ``<a href>`` in the hearing detail, so a
    ``javascript:`` / ``data:`` / ``vbscript:`` scheme would be a stored-XSS
    vector executed under the app origin. WHITELIST {http, https} only — never
    a blacklist. Called by _validate (web form → error) and by
    vevent_to_hearing (CalDAV PUT → the bad value is dropped, not propagated).
    """
    if not uri:
        return True  # empty is valid (no conference link)
    parsed = urlsplit(uri.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

# Quick-select courthouse locations
QUICK_LOCATIONS = (
    "Palais de justice de Montréal, 1 rue Notre-Dame Est",
    "Palais de justice de Québec, 300 boulevard Jean-Lesage",
    "Palais de justice de Laval, 2800 boulevard Saint-Martin Ouest",
    "Palais de justice de Longueuil, 1111 boulevard Jacques-Cartier Est",
)

# Suggested hearing titles per type (every live type MUST have a key — a
# missing one breaks the form's Alpine suggestTitle block).
HEARING_TITLE_SUGGESTIONS = {
    "conférence_de_gestion": "Conférence de gestion",
    "conférence_de_règlement": "Conférence de règlement à l'amiable",
    "conférence_préparatoire": "Conférence préparatoire",
    "audience": "Audience sur demande",
    "instruction": "Instruction au fond",
    "consultation": "Consultation",
    "rencontre": "Rencontre",
    "conférence": "Conférence",
    "interrogatoire": "Interrogatoire préalable",
    "autre": "",
}

# Removed hearing-type keys → live key, applied ON READ (_migrate_hearing),
# BEFORE any validation. Mirrors models/dossier._MANDATE_TYPE_MIGRATION.
_HEARING_TYPE_MIGRATION = {
    # The Code names the audition au fond « instruction ».
    "procès": "instruction",
    # An appeal hearing is still an audience (before the Cour d'appel); also
    # resolves the homograph with the « Appel » note category (phone call).
    "appel": "audience",
    # « Médiation » has no proper equivalent in the retained extrajudicial
    # vocabulary — explicit fallback to « autre » (user reclassifies on next
    # edit), like _MANDATE_TYPE_MIGRATION["mediation_arbitrage"].
    "médiation": "autre",
}


def _migrate_hearing(doc: dict) -> dict:
    """Read-time migration: fold removed hearing-type keys onto live ones and
    default the modalité fields absent on legacy docs (get_hearing returns
    to_dict() without a _default_doc merge). The permanent net (spec §7.1);
    the one-shot script rewrites storage so jtx tiles refresh.
    """
    old = doc.get("hearing_type", "")
    if old in _HEARING_TYPE_MIGRATION:
        doc["hearing_type"] = _HEARING_TYPE_MIGRATION[old]
    doc.setdefault("modalite", "présentiel")
    doc.setdefault("conference_uri", "")
    # Bookings sync (phase L2) — default every booking field on read so the
    # filter and the UI never hit a KeyError on a legacy hearing. « confirmation »
    # defaults to "" (confirmed): a doc that predates L2 is NEVER given a
    # non-empty value here, so existing hearings stay visible everywhere.
    doc.setdefault("source", "")
    doc.setdefault("confirmation", "")
    doc.setdefault("graph_event_id", "")
    doc.setdefault("graph_ical_uid", "")
    doc.setdefault("graph_last_modified", "")
    doc.setdefault("client_email", "")
    doc.setdefault("client_nom", "")
    doc.setdefault("bookings_divergence", None)
    doc.setdefault("partie_id", "")
    # Séries récurrentes. Additifs, sans migration : un document hérité lit
    # serie_id == "" — « autonome » — ce qui est vrai.
    doc.setdefault("serie_id", "")
    doc.setdefault("serie_rule", None)
    return doc


# ── Confirmation gate (phase L2) ───────────────────────────────────────────
# A hearing whose « confirmation » is one of these values is not a plain
# confirmed event. Mirrors the models/note.py include_analyse contract: the
# LIST functions exclude by default so DAV, MCP and the dashboard never see an
# unconfirmed Bookings import; get_hearing (single fetch) does NOT filter, so
# Réception and the confirmation route can still reach one.
#
# NB: "à_confirmer" is ALSO a hearing STATUS value (a court date pending
# scheduling) — an entirely separate concept. Only the « confirmation » field
# gates visibility; « status » never does.
_UNCONFIRMED_ALL = ("à_confirmer", "annulée_client", "refusée")
# "refusée" is the deleted-equivalent — removed from EVERY list, both modes.
_UNCONFIRMED_REFUSED = ("refusée",)


def _filter_confirmation(rows: list[dict], include_unconfirmed: bool) -> list[dict]:
    """Drop unconfirmed hearings unless the caller opts in.

    ``include_unconfirmed=False`` (DAV/MCP/dashboard/exports) keeps only
    confirmed rows. ``True`` (Calendar + Réception) keeps confirmed +
    à_confirmer + annulée_client, but « refusée » is dropped in both modes.
    """
    drop = _UNCONFIRMED_REFUSED if include_unconfirmed else _UNCONFIRMED_ALL
    return [r for r in rows if r.get("confirmation") not in drop]


def _default_doc() -> dict:
    """Return a dict with every hearing field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        # « audience » serves the WEB form (routes/hearings.py defaults it
        # anyway) and Bookings (explicit types). The DAV PUT create path
        # overrides this to « rencontre » BEFORE create_hearing — a
        # phone-created VEVENT carries no X-PALLAS-HEARING-TYPE, and
        # stamping it « audience » made every personal appointment
        # forum="judiciaire" (PA-D01). Change one without the other and the
        # defect returns silently.
        "hearing_type": "audience",
        "start_datetime": None,
        "end_datetime": None,
        "all_day": False,
        "location": "",
        "court": "",
        "judge": "",
        "notes": "",
        "reminder_minutes": 1440,
        "status": "à_confirmer",
        # Modality (2026-07-24). conference_uri is kept even when modalite
        # leaves visioconférence (round-trip); CONFERENCE is only emitted when
        # modalite IS visioconférence and the URI is non-empty.
        "modalite": "présentiel",
        "conference_uri": "",
        # Bookings sync (phase L2). source="" for an internal hearing;
        # "bookings" for a « Bookings with me » import. confirmation="" reads
        # as confirmed (visible everywhere); "à_confirmer"/"annulée_client"/
        # "refusée" gate it out of DAV+MCP (and, except à_confirmer, the
        # Calendar). See _filter_confirmation.
        "source": "",
        "confirmation": "",
        "graph_event_id": "",
        "graph_ical_uid": "",
        "graph_last_modified": "",
        "client_email": "",
        "client_nom": "",
        "bookings_divergence": None,
        "partie_id": "",
        # ── Séries récurrentes ────────────────────────────────────────────
        # serie_id : UUIDv4 partagé par toutes les occurrences d'une chaîne.
        # "" = occurrence autonome. Toutes les occurrences sont ÉGALES — pas
        # de maître, pas d'index : un index se périmerait au premier
        # détachement, et un maître ferait du détachement une promotion au
        # lieu d'une écriture d'un champ.
        #
        # ATTENTION : "" est une VALEUR STOCKÉE, pas une sentinelle. Une
        # égalité Firestore sur "" ramène TOUTE audience autonome du cabinet,
        # d'où le refus en tête de list_series / delete_series.
        #
        # serie_rule : le motif TEL QU'ENGENDRÉ (dates ISO), un constat qu'on
        # ne réétend jamais à la lecture. Il existe parce qu'après le premier
        # détachement ou la première suppression les dates ne déterminent plus
        # la règle. Les DEUX champs appartiennent au serveur : jamais lus
        # d'une charge DAV, jamais émis dans un VEVENT.
        "serie_id": "",
        "serie_rule": None,
        "created_at": None,
        "updated_at": None,
        "etag": "",
        # DAV-specific
        "vevent_uid": "",
        "dav_href": "",
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid).

    A dossier link is optional: hearings may be standalone agenda events with
    no dossier (mirroring standalone tasks). Such events still sync to DavX5
    via the « Général » collection.
    """
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre de l'audience est requis.")

    if not data.get("start_datetime"):
        errors.append("La date et l'heure de début sont requises.")

    ht = data.get("hearing_type", "")
    if ht and ht not in VALID_HEARING_TYPES:
        errors.append("Type d'audience invalide.")

    modalite = data.get("modalite", "")
    if modalite and modalite not in VALID_MODALITES:
        errors.append("Modalité invalide.")

    if not is_safe_conference_uri(data.get("conference_uri") or ""):
        errors.append(
            "L'hyperlien de visioconférence doit être une adresse "
            "http:// ou https://."
        )

    st = data.get("status", "")
    if st and st not in VALID_STATUSES:
        errors.append("Statut invalide.")

    # End must be after start
    start = data.get("start_datetime")
    end = data.get("end_datetime")
    if start and end and end <= start:
        errors.append("L'heure de fin doit être après l'heure de début.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def dav_href_for(dossier_id: str, hearing_id: str) -> str:
    """DAV path of a hearing: per-dossier when linked, shared otherwise.

    A dossier-linked hearing is served from its dossier's collection; one
    with no dossier lives in « Général » alongside dossier-less tasks and
    notes. Stored for reference; the
    DAV layer always derives the href from the URL it was reached through,
    so a stale value here can never misroute a request.
    """
    if dossier_id:
        return f"/dav/dossier-{dossier_id}/{hearing_id}.ics"
    return f"/dav/general/{hearing_id}.ics"


def create_hearing(
    data: dict,
    *,
    dav_id: Optional[str] = None,
    dav_uid: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors).

    The id and the VEVENT UID are minted here — an ``id`` or ``vevent_uid``
    inside *data* is DISCARDED, whoever sends it. Until 2026-09-27 both were
    honoured and the document written with ``set()``: a forwarded ``id``
    let a caller pick, and silently overwrite, an existing hearing — the
    DAV PUT reached this function whenever its FAIL-OPEN read missed the
    stored event, so a transient read error turned a phone edit into a
    full-document replacement (serie_id, source, confirmation and every
    graph_* key gone).

    ``dav_id`` / ``dav_uid`` (keyword-only) serve ONE caller, the DAV PUT
    create branch (the ``create_task`` rule, lot 0b B3). A CalDAV client
    names the new resource in its URL and carries its own UID; the event is
    stored under both, or every later GET of the client's href 404s while a
    duplicate with another UID syncs down. With ``dav_id`` the document is
    written with ``create()``, never ``set()``: an event already stored
    under that id is refused (``[DAV_ID_TAKEN]``), never overwritten. An
    unusable name returns ``[DAV_ID_INVALID]``. ``dav_uid`` is kept only
    when it can be stored verbatim (``dav_ids.client_uid``), and is ignored
    without ``dav_id``.
    """
    data = dict(data)
    data.pop("id", None)
    data.pop("vevent_uid", None)
    if dav_id is not None and not dav_ids.valid_resource_id(dav_id):
        return None, [dav_ids.DAV_ID_INVALID]

    merged = {**_default_doc(), **_sanitize_data(data)}

    # Auto-set end_datetime if not provided (start + 1 hour)
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    hearing_id = dav_id if dav_id is not None else str(uuid.uuid4())
    vevent_uid = (
        (dav_ids.client_uid(dav_uid) if dav_id is not None else None)
        or str(uuid.uuid4())
    )

    merged.update({
        "id": hearing_id,
        "vevent_uid": vevent_uid,
        "dav_href": dav_href_for(merged.get("dossier_id", ""), hearing_id),
    })
    provenance.stamp_create(
        merged, now, created_at=merged.get("created_at") or now,
    )

    try:
        ref = db.collection(COLLECTION).document(hearing_id)
        if dav_id is not None:
            ref.create(merged)
        else:
            ref.set(merged)
    except AlreadyExists:
        return None, [dav_ids.DAV_ID_TAKEN]
    except Exception:
        log_unexpected("hearing write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, hearing_id)

    return merged, []


def get_hearing_strict(hearing_id: str) -> Optional[dict]:
    """Fetch a single hearing by ID; a read failure PROPAGATES.

    ``None`` means the store answered « no such document » — never « the
    read failed ». For a caller that WRITES on the strength of an absence:
    the DAV PUT routes a missing resource to its create branch, and
    :func:`get_hearing`'s fail-open ``None`` would route a phone EDIT there
    on a transient error (refused by ``create()`` since 2026-09-27, but
    answered 412 — a refusal of a legitimate edit — rather than the 503 a
    client retries).
    """
    doc = db.collection(COLLECTION).document(hearing_id).get()
    if doc.exists:
        return _migrate_hearing(doc.to_dict())
    return None


def get_hearing(hearing_id: str) -> Optional[dict]:
    """Fetch a single hearing by ID (fail-open: ``None`` on a read error)."""
    try:
        return get_hearing_strict(hearing_id)
    except Exception as exc:
        logger.warning("get_hearing failed for %s: %s", sanitize_log_value(hearing_id), exc)
    return None


def list_hearings(
    dossier_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    hearing_type_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    include_unconfirmed: bool = False,
) -> list[dict]:
    """Return hearings, optionally filtered.

    ``include_unconfirmed`` (default False) mirrors models/note.py's
    include_analyse contract: unconfirmed Bookings imports are excluded unless
    the caller opts in. Only Réception and the Calendar view (and the sync
    job's own upsert lookup) pass True — DAV, MCP and the dashboard keep the
    default so a pending reservation never syncs or shows up as a real event.
    """
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
        results = _filter_confirmation(results, include_unconfirmed)

        # Client-side filters (Firestore single-field index limitation)
        if status_filter and status_filter in VALID_STATUSES:
            results = [r for r in results if r.get("status") == status_filter]

        if hearing_type_filter and hearing_type_filter in VALID_HEARING_TYPES:
            results = [r for r in results if r.get("hearing_type") == hearing_type_filter]

        if date_from:
            results = [r for r in results if r.get("start_datetime") and r["start_datetime"] >= date_from]
        if date_to:
            results = [r for r in results if r.get("start_datetime") and r["start_datetime"] <= date_to]

        # Sort by start_datetime ascending (chronological)
        results.sort(
            key=lambda h: h.get("start_datetime") or datetime.min.replace(tzinfo=timezone.utc),
        )

        return results
    except Exception:
        return []


def list_bookings_all() -> list[dict]:
    """Every ``source == "bookings"`` hearing, NO confirmation filter — the
    Bookings sync's reconciliation lookup ONLY.

    Unlike ``list_hearings(include_unconfirmed=True)``, this INCLUDES
    ``refusée`` (and ``annulée_client``): the sync must see the decisions the
    juriste already made, or a refused reservation whose Outlook cancellation
    failed (best-effort) would be re-imported as a brand-new ``à_confirmer``
    every cycle. **Never call this from a UI/DAV/MCP path** — those must keep
    ``refusée`` hidden (see :func:`_filter_confirmation`). Single-field
    ``source`` equality → auto-indexed, no composite index.

    A read failure PROPAGATES (lot 1a, L4). It used to answer ``[]``, which
    the sync reads as « no prior import »: every reservation in the window
    was then created AGAIN as a fresh ``à_confirmer`` card, once per
    10-minute cycle for as long as the outage lasted — and the duplicates
    share the real event's ``graph_event_id``, so refusing one in Réception
    cancels the client's actual meeting. The caller aborts the cycle
    instead (``routes/taches_bookings.sync``).
    """
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("source", "==", "bookings")
    )
    return [_migrate_hearing(doc.to_dict()) for doc in query.stream()]


# ── Bookings rendez-vous: the Réception reads and the decision write ────
# (lot 1a, L4 — services/rendez_vous.py is the one caller of the decision
# write, and of the reader for every list a decision is taken from;
# routes/reception._compter_rdv also counts through the reader, for the
# fail-open nav badge, which decides nothing.)
#
# The imports still awaiting the lawyer's decision: « à_confirmer » (a live
# reservation) and « annulée_client » (cancelled by the client, still shown
# so the card can be removed). « refusée » is the deleted-equivalent, never
# listed (the _filter_confirmation rule), and "" is a confirmed event.
BOOKINGS_PENDING: tuple[str, ...] = ("à_confirmer", "annulée_client")
# The two values a decision writes: "" (confirmed — the event enters the
# calendar and DAV) and « refusée ».
BOOKINGS_DECISIONS: tuple[str, ...] = ("", "refusée")

CONFIRMATION_READ_ERROR = (
    "Lecture du rendez-vous impossible — rien n'a été enregistré. "
    "Réessayez dans un moment."
)


def list_bookings_strict(*, include_confirmed: bool = False) -> list[dict]:
    """The Bookings imports awaiting a decision — a STRICT read.

    ``source == "bookings"`` equality (single field → auto-indexed, no
    composite index); a read failure PROPAGATES. Keeps the
    :data:`BOOKINGS_PENDING` rows; ``include_confirmed`` also keeps the
    confirmed ones (``confirmation == ""``), on which Réception's divergence
    alerts live. ``refusée`` is dropped in both modes.

    Why not ``list_hearings(include_unconfirmed=True)``: that reader
    streams the WHOLE collection and swallows every error into ``[]``, so
    « nothing pending » was indistinguishable from a Firestore outage —
    and Réception printed « Aucun rendez-vous à confirmer » over a blip,
    its own error banner being dead code (the try around it could never
    fire). Unordered: the caller sorts.
    """
    keep = BOOKINGS_PENDING + (("",) if include_confirmed else ())
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("source", "==", "bookings")
    )
    rows = [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
    return [r for r in rows if (r.get("confirmation") or "") in keep]


def set_bookings_confirmation(
    hearing_id: str,
    confirmation: str,
    *,
    partie_id: Optional[str] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], str]:
    """Write a lawyer's decision on a Bookings import — a PARTIAL update.

    Only the confirmation gate (and, when given, the linked ``partie_id``)
    plus the write's own stamp (``provenance.update_fields``) — never the
    merged full-document ``set()`` of :func:`update_hearing`. That shape is
    the guarantee: a decision can move nothing else (not the slot the
    Bookings sync may just have updated, not a divergence), and it cannot
    be refused by a field validation that has nothing to do with it —
    which matters most AFTER an Outlook cancellation, when the refusal must
    be recorded whatever the rest of the document holds.

    *confirmation* must be one of :data:`BOOKINGS_DECISIONS`, and the
    hearing a Bookings import (``source == "bookings"``); the transition
    rules (confirming an ``annulée_client`` import is refused, a confirmed
    one is not refused here…) belong to ``services/rendez_vous.py``, which
    is the only caller. The read is STRICT: a read failure answers
    :data:`CONFIRMATION_READ_ERROR`, never « introuvable ».

    ``expected_etag`` (keyword-only): when given, the write commits only if
    the stored etag is still that one (``models.concurrency.commit_fields``,
    same partial key set) — a stale one returns ``[STALE_ETAG_ERROR]``,
    nothing written. ``None`` is the unconditional single ``update()`` —
    what the refusal performs after an Outlook cancellation, when the truth
    has already left the building.

    Returns ``(doc, errors, previous)``: ``previous`` is the confirmation
    the write REPLACED, as read (``""`` on a refusal). The third member
    deviates from the house pair on purpose (the ``set_time_entry_phase``
    precedent): a refusal recorded unconditionally over an import another
    tab had just confirmed must tombstone it out of DAV, and only the
    writer knows what it replaced.
    """
    if confirmation not in BOOKINGS_DECISIONS:
        return None, ["Décision inconnue pour un rendez-vous."], ""
    try:
        existing = get_hearing_strict(hearing_id)
    except Exception:
        log_unexpected("hearing read failed before a confirmation write")
        return None, [CONFIRMATION_READ_ERROR], ""
    if not existing or existing.get("source") != "bookings":
        return None, ["Rendez-vous introuvable."], ""
    previous = existing.get("confirmation") or ""
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], previous

    now = datetime.now(timezone.utc)
    fields = {"confirmation": confirmation, **provenance.update_fields(now)}
    if partie_id is not None:
        fields["partie_id"] = sanitize(str(partie_id), max_length=100)
    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(hearing_id), fields,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR], previous
    except concurrency.Vanished:
        return None, ["Rendez-vous introuvable."], previous
    except Exception:
        log_unexpected("hearing confirmation write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], previous
    provenance.note_commit(COLLECTION, hearing_id)
    return {**existing, **fields}, [], previous


class HearingWindow(NamedTuple):
    """A bounded hearing fetch together with what the caller cannot re-derive.

    ``rows`` alone loses two facts that a destructive consumer needs:

    * ``window_full`` is measured on the RAW window, BEFORE the confirmation
      filter shrinks it. Measuring it on ``rows`` is wrong and dangerous: one
      ``refusée`` Bookings import inside the window makes a genuinely truncated
      fetch look complete, and the Outlook mirror then treats every hearing
      beyond the cut as an orphan and deletes real court dates from Exchange.
    * ``ok`` distinguishes "nothing matched" from "the query failed". Both
      yield an empty ``rows``, and a consumer that conflates them deletes every
      mirror it has on a transient Firestore hiccup.
    """

    rows: list[dict]
    window_full: bool
    ok: bool


def list_hearings_in_range_state(
    date_from: datetime,
    date_to: datetime,
    limit: int = 100,
    include_unconfirmed: bool = False,
) -> HearingWindow:
    """:func:`list_hearings_in_range` plus the truncation and failure signals.

    Callers that only render a list want the plain variant. A caller that
    DELETES on the strength of an absence (the Outlook mirror) must use this
    one — see :class:`HearingWindow` for why each field exists.
    """
    try:
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("start_datetime", ">=", date_from))
            .where(filter=FieldFilter("start_datetime", "<=", date_to))
            .order_by("start_datetime")
            .limit(limit)
        )
        raw = [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
        # Measured on the RAW window before the confirmation filter shrinks it,
        # so a truncated fetch is still detected even when some rows are
        # dropped. This value is the whole reason this function exists.
        window_full = len(raw) >= limit
        if window_full:
            logger.warning(
                "list_hearings_in_range: result window full (limit=%d) — "
                "some hearings may be hidden", limit,
            )
        return HearingWindow(
            _filter_confirmation(raw, include_unconfirmed), window_full, True
        )
    except Exception as exc:
        logger.warning("list_hearings_in_range: query failed: %s", exc)
        return HearingWindow([], False, False)


def list_hearings_in_range(
    date_from: datetime,
    date_to: datetime,
    limit: int = 100,
    include_unconfirmed: bool = False,
) -> list[dict]:
    """Return hearings starting within [date_from, date_to], chronologically.

    Unlike :func:`list_hearings` (which streams the whole collection and
    filters in Python), the date range, ordering, and bound are pushed
    server-side. Both range filters and the order_by target the same field
    (start_datetime), so the automatic single-field index serves the query —
    no composite index required. Status filtering (e.g. excluding annulée)
    stays with the caller, applied over the bounded result.

    ``include_unconfirmed`` (default False) excludes unconfirmed Bookings
    imports, applied in Python over the bounded window (same accepted
    limitation as the existing caller-side status filtering).

    Returns [] on failure (the dashboard degrades gracefully). A caller that
    would DESTROY something on the strength of an empty result must call
    :func:`list_hearings_in_range_state` instead and honour its ``ok`` flag.
    """
    return list_hearings_in_range_state(
        date_from, date_to, limit, include_unconfirmed
    ).rows


def list_hearings_window(
    pivot: datetime,
    direction: str = "upcoming",
    limit: int = 100,
    include_unconfirmed: bool = False,
) -> list[dict]:
    """Return a bounded window of hearings on one side of *pivot*.

    - ``direction="upcoming"``: ``start_datetime >= pivot``, chronological
      (the next *limit* hearings).
    - ``direction="past"``: ``start_datetime < pivot``, reverse
      chronological (the *limit* most recent past hearings).

    Unlike :func:`list_hearings` (full collection stream + Python filter),
    the range filter, ordering, and bound are pushed server-side. The
    range filter and the order_by target the same field (start_datetime),
    so the automatic single-field index serves both queries — no composite
    index required. Type/status filtering stays with the caller, applied
    over the bounded window.

    ``include_unconfirmed`` (default False) excludes unconfirmed Bookings
    imports over the bounded window (see :func:`list_hearings_in_range`).

    Returns [] on failure (the agenda view degrades gracefully).
    """
    try:
        if direction == "past":
            query = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("start_datetime", "<", pivot))
                .order_by(
                    "start_datetime", direction=firestore.Query.DESCENDING
                )
                .limit(limit)
            )
        else:
            query = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("start_datetime", ">=", pivot))
                .order_by("start_datetime")
                .limit(limit)
            )
        raw = [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
        if len(raw) >= limit:
            logger.warning(
                "list_hearings_window: result window full "
                "(direction=%s, limit=%d) — some hearings may be hidden",
                direction, limit,
            )
        return _filter_confirmation(raw, include_unconfirmed)
    except Exception as exc:
        # PII-free: log only the exception type, never document contents.
        logger.warning(
            "list_hearings_window: query failed: %s", type(exc).__name__
        )
        return []


# ── Update: which keys a caller may name ────────────────────────────────
# update_hearing merges {**existing, **data} and writes the WHOLE document,
# so until 2026-09-26 it honoured any key a caller sent: an « id » corrupted
# the id FIELD (the document path stayed put), a « serie_id »: "" silently
# detached an occurrence, a « confirmation » re-gated or un-gated DAV/MCP
# visibility, a « graph_* » broke the Bookings reconciliation. The keys are
# now in two closed sets.
#
# CONTENT — what a generic edit names: the web form, the DAV PUT (every key
# vevent_to_hearing can produce, minus the UID the route drops), a future
# connector edit. Anything else in ``data`` is REFUSED, naming the field.
UPDATE_FIELDS = frozenset({
    "dossier_id", "dossier_file_number", "dossier_title",
    "title", "hearing_type",
    "start_datetime", "end_datetime", "all_day",
    "location", "court", "judge", "notes",
    "reminder_minutes", "status", "modalite", "conference_uri",
})
# SERVER-OWNED — set only by the machine paths that own them, through the
# explicit ``server_fields=`` keyword: the Bookings sync and Réception (the
# confirmation gate, the graph_* reconciliation keys, the requester, the
# divergence, the linked partie) and unlink_hearing (the series link). A
# dictionary relayed from a form or a VEVENT can never reach them.
SERVER_FIELDS = frozenset({
    "source", "confirmation",
    "graph_event_id", "graph_ical_uid", "graph_last_modified",
    "client_email", "client_nom", "bookings_divergence", "partie_id",
    "serie_id", "serie_rule",
})
# Neither set: id, vevent_uid, dav_href (recomputed on every update),
# created_at/created_via, updated_at/updated_via, etag — identity and stamps
# belong to the model alone.

_SLOT_FIELDS = frozenset({"start_datetime", "end_datetime", "all_day"})
_DEFAULT_DURATION = timedelta(hours=1)
_ONE_DAY = timedelta(days=1)
_ONE_MICROSECOND = timedelta(microseconds=1)


def update_key_errors(
    data: dict, server_fields: Optional[dict] = None
) -> list[str]:
    """French refusals for keys ``update_hearing`` will not honour.

    Pure. Exposed so a caller's test can prove it splits its payload the
    way the model requires, without a Firestore round trip. Names the field
    (a code identifier, never user content).
    """
    errors = [
        f"Le champ « {key} » ne peut pas être modifié par cette voie."
        for key in sorted(k for k in data if k not in UPDATE_FIELDS)
    ]
    errors += [
        f"Le champ « {key} » n'appartient pas aux champs réservés au serveur."
        for key in sorted(
            k for k in (server_fields or {}) if k not in SERVER_FIELDS
        )
    ]
    return errors


def _is_utc_midnight(dt: datetime) -> bool:
    u = _to_utc(dt)
    return (u.hour, u.minute, u.second, u.microsecond) == (0, 0, 0, 0)


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _as_all_day_start(dt: datetime) -> datetime:
    """A start in the all-day convention: midnight UTC of its civil day.

    A value already at midnight UTC IS the convention (the web form's
    date input, a VEVENT DATE) and is kept. Anything else is a timed
    instant, whose civil day is the MONTRÉAL one — reading ``.date()`` on
    the UTC value would put a 21 h event on the next day.
    """
    if _is_utc_midnight(dt):
        return _to_utc(dt)
    return _utc_midnight(to_mtl(dt).date())


def _as_all_day_end(dt: datetime) -> datetime:
    """The exclusive all-day end a TIMED instant stands for.

    Called only for a named end that is not already at midnight UTC — a
    midnight-UTC end is the convention itself (the exclusive DTEND a VEVENT
    DATE carries, the web form's date input) and is never rewritten. A
    timed instant covers its Montréal civil day, so the exclusive end is
    the NEXT midnight — measured just before the instant, so an end at 00 h
    Montréal does not claim the following day.
    """
    return _utc_midnight(to_mtl(dt - _ONE_MICROSECOND).date() + _ONE_DAY)


def _renormalize_slot(existing: dict, data: dict, merged: dict) -> None:
    """Keep the stored slot coherent with what an update named. Mutates
    *merged*.

    The merge used to fill ``end_datetime`` only when it was MISSING, so a
    rescheduled start kept the OLD absolute end: moving a one-hour meeting
    to the previous day silently made it a 25-hour event (``_validate``
    only checks end > start), on the phone, in Outlook and in the web.
    And flipping « toute la journée » reinterpreted the stored instant in
    the other convention, landing the event on the wrong civil day.

    The rules, by what *data* names (presence, never truthiness):

    * nothing about the slot → left exactly as stored;
    * a start WITHOUT an end key → the stored DURATION is kept (the stored
      end is in the same convention as the stored start, so an all-day
      span stays a span and a one-hour meeting stays one hour);
    * « all_day » flipped with no start named → the stored start is
      carried to the other convention on the SAME civil day (all-day:
      midnight UTC of its Montréal day; timed: 00 h Montréal of the stored
      date), and an unnamed end falls back to the default slot — a timed
      duration means nothing across the conventions, nor an exclusive
      all-day end;
    * a start named for an all-day event keeps the convention: midnight
      UTC is taken as it stands, any other instant lands on its Montréal
      civil day; a named timed end becomes the exclusive midnight after its
      civil day (RFC 5545 §3.8.2.2 — what hearing_to_vevent and the Outlook
      mirror both read);
    * an end named as ``None``, or still missing → start + 1 h, as create
      does (hearing_to_vevent serializes that as a one-day all-day event).
    """
    start = merged.get("start_datetime")
    if not (_SLOT_FIELDS & data.keys()) or not isinstance(start, datetime):
        if isinstance(start, datetime) and not merged.get("end_datetime"):
            merged["end_datetime"] = start + _DEFAULT_DURATION
        return

    all_day = bool(merged.get("all_day"))
    toggled = all_day != bool(existing.get("all_day"))
    start_named = "start_datetime" in data
    end_named = "end_datetime" in data

    if all_day and start_named:
        start = _as_all_day_start(start)
    elif all_day and toggled:
        # Timed → all-day: the stored value is certainly an INSTANT, even
        # one that happens to sit at midnight UTC (20 h in Montréal in
        # summer), so its day is the Montréal one — never the UTC date.
        start = _utc_midnight(to_mtl(start).date())
    elif toggled and not start_named:
        # All-day → timed: the stored value is a civil DATE at midnight UTC.
        # Read as an instant it is 19 h/20 h the previous Montréal day.
        start = mtl_to_utc(datetime.combine(_to_utc(start).date(), time()))
    merged["start_datetime"] = start

    end = merged.get("end_datetime") if end_named else None
    if end_named:
        if all_day and isinstance(end, datetime) and not _is_utc_midnight(end):
            end = _as_all_day_end(end)
            if end <= start:
                end = None  # a timed end inside the start's own day
    elif not toggled:
        old_start = existing.get("start_datetime")
        old_end = existing.get("end_datetime")
        if (
            isinstance(old_start, datetime)
            and isinstance(old_end, datetime)
            and old_end > old_start
        ):
            end = start + (old_end - old_start)
    merged["end_datetime"] = end or start + _DEFAULT_DURATION


def update_hearing(
    hearing_id: str,
    data: dict,
    *,
    server_fields: Optional[dict] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Update an existing hearing. Returns (updated_doc, errors).

    *data* may name only :data:`UPDATE_FIELDS`; the server-owned
    :data:`SERVER_FIELDS` travel through ``server_fields`` (keyword-only).
    Any other key — ``id``, ``vevent_uid``, a stamp — is refused before
    anything is read. Presence-merge as before: a key present overwrites,
    an absent key survives. The slot is renormalized (see
    :func:`_renormalize_slot`) and ``dav_href`` recomputed.

    ``expected_etag`` (keyword-only): when given, the write commits only if
    the stored etag is still that one (``models.concurrency``) — a stale one
    returns ``[STALE_ETAG_ERROR]`` and writes nothing. It is compared right
    after the read, before any validation: the useful answer to an outdated
    view is « re-read », not the first field error the outdated view trips.
    ``None`` (the DAV PUT, the Bookings sync, a page rendered before its
    form carried an etag) is the unchanged single ``set()``.
    """
    errors = update_key_errors(data, server_fields)
    if errors:
        return None, errors

    existing = get_hearing(hearing_id)
    if not existing:
        return None, ["Audience introuvable."]
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR]

    merged = {
        **existing,
        **_sanitize_data(data),
        **_sanitize_data(server_fields or {}),
    }
    _renormalize_slot(existing, data, merged)
    merged["id"] = hearing_id
    merged["dav_href"] = dav_href_for(merged.get("dossier_id", ""), hearing_id)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    provenance.stamp_update(merged, now)

    try:
        concurrency.commit_document(
            db.collection(COLLECTION).document(hearing_id), merged,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR]
    except concurrency.Vanished:
        return None, ["Audience introuvable."]
    except Exception:
        log_unexpected("hearing write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, hearing_id)

    return merged, []


def delete_hearing(hearing_id: str) -> tuple[bool, str]:
    """Delete a hearing. Returns (success, error_message)."""
    existing = get_hearing(hearing_id)
    if not existing:
        return False, "Audience introuvable."

    try:
        db.collection(COLLECTION).document(hearing_id).delete()
        return True, ""
    except Exception:
        log_unexpected("hearing delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


# ── Séries récurrentes ────────────────────────────────────────────────────
# Une série est MATÉRIALISÉE : N audiences ordinaires partageant un serie_id.
# Aucun lecteur existant ne change — tableau de bord, grille du mois, onglet
# du dossier, collection DAV, MCP, exports, miroir Outlook voient N audiences
# ordinaires. Le contraire (un document porteur d'une RRULE, étendu à la
# lecture) obligerait chacun d'eux à savoir étendre : toutes les requêtes
# bornées filtrent ET trient sur start_datetime, dont un document à règle n'a
# qu'un seul exemplaire.


def occurrence_day(hearing: dict) -> "date | None":
    """Le jour civil d'une audience, dans le bon référentiel.

    Une audience all-day est stockée à minuit UTC (convention _parse_date) —
    sa date UTC EST son jour. Une audience horodatée est stockée en UTC après
    conversion depuis Montréal : à 21 h le 15, l'UTC tombe le 16, donc lire
    ``.date()`` sur la valeur stockée désignerait le mauvais jour.
    """
    start = hearing.get("start_datetime")
    if not isinstance(start, datetime):
        return None
    if hearing.get("all_day"):
        return _to_utc(start).date()
    return to_mtl(start).date()


# ── Read windows judged by the civil day ────────────────────────────────
# A window « from today on » cannot open on one instant: the two storage
# conventions put the start of the same day at two different instants. An
# all-day hearing of the 16th is stored at 00:00 UTC on the 16th (20:00 or
# 19:00 on the 15th in Montréal); a timed hearing of the 16th starts at the
# earliest at midnight MONTRÉAL, 04:00/05:00 UTC on the 16th. A window opened
# at midnight Montréal therefore missed the day's all-day hearings (the 07:00
# briefing never listed them), and one opened at the instant « now » lost
# them from 20:00 the EVENING BEFORE. The rule: read from the earlier of the
# two instants (midnight UTC), then judge every row by its civil day
# (:func:`occurrence_day`). No index: the range stays on start_datetime alone.


def civil_day_floor(day: date) -> datetime:
    """The lowest ``start_datetime`` a hearing of civil day *day* or later
    can carry: midnight UTC of *day*.

    It precedes midnight Montréal by 4 h (EDT) or 5 h (EST), so a read from
    it also brings back the previous evening's timed hearings (20:00 to
    midnight in Montréal), which :func:`on_or_after_day` drops.
    """
    return _utc_midnight(day)


def civil_day_ceiling(day: date) -> datetime:
    """An EXCLUSIVE bound before which every hearing of civil day *day* or
    earlier starts: midnight Montréal of the following day.

    It also admits the next day's all-day hearings (stored at midnight UTC,
    before midnight Montréal), which a filter on :func:`occurrence_day`
    drops. Computed with ``mtl_to_utc`` on the civil date, never with a
    fixed offset, so it holds across a daylight-saving change.
    """
    return mtl_to_utc(datetime.combine(day + _ONE_DAY, time()))


def on_or_after_day(hearings: list[dict], day: date) -> list[dict]:
    """The hearings whose civil day is *day* or later, order kept.

    A row with no readable start is KEPT: the caller's window already
    bounded it, and hiding it would lose a hearing rather than show it
    badly dated (the dossier tab's historical behaviour).
    """
    kept = []
    for hearing in hearings:
        day_of = occurrence_day(hearing)
        if day_of is None or day_of >= day:
            kept.append(hearing)
    return kept


def before_day(hearings: list[dict], day: date) -> list[dict]:
    """The hearings whose civil day precedes *day*, order kept — the exact
    complement of :func:`on_or_after_day` over dated rows."""
    return [
        h for h in hearings
        if (day_of := occurrence_day(h)) is not None and day_of < day
    ]


def _occurrence_slots(
    prototype: dict, dates: list["date"]
) -> list[tuple[datetime, datetime]]:
    """(début, fin) UTC de chaque occurrence, à durée constante.

    Pour une audience horodatée, l'heure MURALE de Montréal est tenue fixe et
    chaque occurrence est convertie SÉPARÉMENT par ``mtl_to_utc`` : c'est ce
    qui maintient « 9 h » à 9 h de part et d'autre d'un changement d'heure.
    Ajouter des timedelta à la valeur UTC stockée décalerait en silence toutes
    les occurrences postérieures à la bascule de mars ou de novembre.
    """
    start = prototype["start_datetime"]
    end = prototype.get("end_datetime") or (start + timedelta(hours=1))
    duree = end - start

    slots: list[tuple[datetime, datetime]] = []
    if prototype.get("all_day"):
        for jour in dates:
            debut = datetime(
                jour.year, jour.month, jour.day, tzinfo=timezone.utc
            )
            slots.append((debut, debut + duree))
        return slots

    heure = to_mtl(start).time()
    for jour in dates:
        debut = mtl_to_utc(datetime.combine(jour, heure))
        slots.append((debut, debut + duree))
    return slots


def create_hearing_series(
    data: dict,
    frequency: str,
    *,
    count: Optional[int] = None,
    until: "date | None" = None,
) -> tuple[list[dict], list[str]]:
    """Créer une série : N audiences liées, écrites en UN SEUL lot.

    Le prototype est validé UNE fois (les occurrences ne diffèrent que par
    leurs dates), puis les N documents et le bump de CTag sont mis dans le
    même ``db.batch()`` — voir ``dav.sync.bump_ctag_in_batch`` pour pourquoi
    le bump ne peut pas venir après le commit.

    Retourne (occurrences, erreurs). La liste est vide si quoi que ce soit a
    été refusé : rien n'est écrit partiellement.
    """
    from dav.sync import (
        _BATCH_CHUNK,
        bump_ctag_in_batch,
        collection_for,
    )
    from utils import recurrence

    merged = {**_default_doc(), **_sanitize_data(data)}
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    # The caller NEVER names an occurrence's identity. This function builds
    # its own batch (it does not go through create_hearing, which discards
    # an id since 2026-09-27), so letting an id or a vevent_uid through here
    # would make N batch.set() on THE SAME reference: Firestore keeps the
    # last one, silently, and 59 occurrences out of 60 vanish behind a
    # success return. Same for the server-owned fields.
    for cle in ("id", "vevent_uid", "dav_href", "serie_id", "serie_rule"):
        merged.pop(cle, None)
    merged = {**_default_doc(), **merged}

    errors = _validate(merged)
    if errors:
        return [], errors

    depart = occurrence_day(merged)
    if depart is None:
        return [], ["La date et l'heure de début sont requises."]

    errors = recurrence.validate_rule(
        frequency, count=count, until=until, start=depart
    )
    if errors:
        return [], errors

    dates = recurrence.occurrence_dates(
        depart, frequency, count=count, until=until
    )
    slots = _occurrence_slots(merged, dates)

    # Ceinture : le plafond vit dans utils.recurrence, mais un lot doit rester
    # atomique quoi qu'il arrive à cette constante. N + 1 opérations ici.
    if len(slots) + 1 > _BATCH_CHUNK:
        return [], [
            "Cette série est trop longue pour être écrite d'un seul bloc."
        ]

    serie_id = str(uuid.uuid4())
    rule = recurrence.build_rule(
        frequency, depart, count=count, until=until
    )
    now = datetime.now(timezone.utc)
    dossier_id = merged.get("dossier_id", "")

    occurrences: list[dict] = []
    for debut, fin in slots:
        hearing_id = str(uuid.uuid4())
        occ = {
            **merged,
            "id": hearing_id,
            "start_datetime": debut,
            "end_datetime": fin,
            "serie_id": serie_id,
            "serie_rule": rule,
            "vevent_uid": str(uuid.uuid4()),
            "dav_href": dav_href_for(dossier_id, hearing_id),
        }
        occurrences.append(provenance.stamp_create(occ, now))

    try:
        batch = db.batch()
        for occ in occurrences:
            batch.set(db.collection(COLLECTION).document(occ["id"]), occ)
        bump_ctag_in_batch(batch, collection_for(dossier_id))
        batch.commit()
    except Exception:
        log_unexpected("hearing series write failed")
        return [], ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    for occ in occurrences:
        provenance.note_commit(COLLECTION, occ["id"])

    return occurrences, []


def list_series(serie_id: str) -> list[dict]:
    """Les occurrences d'une série, dans l'ordre chronologique.

    REFUSE un identifiant vide. "" est une VALEUR STOCKÉE : une égalité
    Firestore dessus ramènerait toute audience autonome du cabinet, et le
    déclencheur ne demande aucun attaquant — « Détacher » pose serie_id = ""
    et un onglet resté ouvert affiche encore « Supprimer la série ».

    PROPAGE une erreur de lecture, contrairement à list_hearings qui rend [].
    Un dialogue destructeur ne doit jamais sous-estimer ce qu'il détruira
    (doctrine subtree_members contre list_folders).
    """
    if not serie_id:
        return []
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("serie_id", "==", serie_id)
    )
    rows = [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
    rows.sort(
        key=lambda h: (
            h.get("start_datetime")
            or datetime.min.replace(tzinfo=timezone.utc),
            h.get("id") or "",
        )
    )
    return rows


def delete_series(
    serie_id: str, *, from_date: "date | None" = None
) -> tuple[list[dict], list[str]]:
    """Supprimer une série — les occurrences, leurs pierres tombales et les
    bumps de CTag dans UN SEUL lot, chaque occurrence dans la collection DAV
    de SON dossier.

    ``from_date`` borne la portée « cette occurrence et les suivantes » : une
    occurrence dont le jour civil précède cette date n'est jamais touchée.
    Une occurrence passée est le constat de ce qui a eu lieu.

    Retourne (occurrences supprimées, erreurs).
    """
    from dav.sync import (
        _BATCH_CHUNK,
        bump_ctag_in_batch,
        collection_for,
        record_tombstones_in_batch,
    )

    if not serie_id:
        return [], ["Série introuvable."]

    try:
        rows = list_series(serie_id)
    except Exception:
        log_unexpected("hearing series read failed")
        return [], ["Erreur lors de la lecture de la série. Veuillez réessayer."]

    if from_date is not None:
        rows = [
            h for h in rows
            if (occurrence_day(h) or from_date) >= from_date
        ]
    if not rows:
        return [], []

    # Chaque occurrence est retirée de SA collection DAV. Une occurrence
    # rattachée à un autre dossier (le formulaire web et le PUT DAV le
    # permettent) vit dans une autre collection : ne tombstoner que celle de
    # la première ligne la supprimait de Firestore en la laissant pour
    # toujours sur le téléphone. Ordre d'apparition préservé, déterministe.
    by_collection: dict[str, list[str]] = {}
    for row in rows:
        by_collection.setdefault(
            collection_for(row.get("dossier_id", "")), []
        ).append(row["id"])

    # 2N + K opérations (N suppressions + N pierres tombales + un bump par
    # collection touchée).
    if 2 * len(rows) + len(by_collection) > _BATCH_CHUNK:
        return [], [
            "Cette série est trop longue pour être supprimée d'un seul bloc."
        ]

    try:
        batch = db.batch()
        for row in rows:
            batch.delete(db.collection(COLLECTION).document(row["id"]))
        for sync_name, ids in by_collection.items():
            token = bump_ctag_in_batch(batch, sync_name)
            record_tombstones_in_batch(batch, sync_name, ids, token)
        batch.commit()
    except Exception:
        log_unexpected("hearing series delete failed")
        return [], ["Erreur lors de la suppression. Veuillez réessayer."]

    return rows, []


def unlink_hearing(hearing_id: str) -> tuple[Optional[dict], list[str]]:
    """Détacher une occurrence : elle devient une audience ordinaire.

    Un seul champ change de part et d'autre — il n'y a ni maître à promouvoir
    ni index à renuméroter, ce qui est précisément pourquoi toutes les
    occurrences sont égales.
    """
    existing = get_hearing(hearing_id)
    if not existing:
        return None, ["Audience introuvable."]
    if not existing.get("serie_id"):
        return None, ["Cette audience ne fait pas partie d'une série."]
    return update_hearing(
        hearing_id, {}, server_fields={"serie_id": "", "serie_rule": None}
    )


# ── Summary ──────────────────────────────────────────────────────────────


def get_hearing_summary(
    dossier_id: str, today: Optional[date] = None
) -> dict:
    """Return hearing counts for a dossier (the MCP ``get_dossier``
    ``summaries.hearings``).

    « Upcoming » and « past » are split by CIVIL day (:func:`occurrence_day`
    against the Montréal *today*), the rule every « from today on » surface
    reads by since lot 1a. The counts compared ``start_datetime`` with the
    instant « now »: an all-day hearing — stored at midnight UTC, 20:00/19:00
    the evening BEFORE in Montréal — was counted « past » from the evening
    before its own day, while the dossier « Calendrier » tab, ``get_agenda``
    and the dashboard list it as today's. A row with no readable start is in
    neither count (as before); a cancelled one is never « upcoming »; a
    « terminée » one is always « past ».
    """
    hearings = list_hearings(dossier_id=dossier_id)
    today = today or deadlines.today_mtl()
    upcoming = past = 0
    for hearing in hearings:
        day = occurrence_day(hearing)
        status = hearing.get("status")
        if (
            day is not None and day >= today
            and status not in ("annulée", "terminée")
        ):
            upcoming += 1
        if (day is not None and day < today) or status == "terminée":
            past += 1
    return {
        "total": len(hearings),
        "upcoming": upcoming,
        "past": past,
    }


# ── RFC-5545 VEVENT serialization ─────────────────────────────────────────


# What hearing_to_vevent puts between the notes and the metadata lines, and
# between the metadata lines themselves.
_DESCRIPTION_SEPARATOR = "\n"


def dav_description_suffix(hearing: dict) -> str:
    """The metadata lines ``hearing_to_vevent`` appends to DESCRIPTION, or
    ``""``.

    Display for the phone, never part of the hearing's notes — see
    :func:`strip_dav_description_suffix` for why that distinction had to
    become code. Order is fixed: Dossier, Type, Modalité, Visioconférence,
    Cour, Juge.
    """
    lines: list[str] = []
    # Standalone agenda events have no dossier — omit the line entirely.
    if hearing.get("dossier_id"):
        lines.append(
            f"Dossier: {hearing.get('dossier_file_number', '')} - "
            f"{hearing.get('dossier_title', '')}"
        )
    if hearing.get("hearing_type"):
        label = HEARING_TYPE_LABELS.get(
            hearing["hearing_type"], hearing["hearing_type"]
        )
        lines.append(f"Type: {label}")
    # Modalité in DESCRIPTION only (visible in every client). NOT in
    # CATEGORIES — that would add a second colored tile in a jtx-style client.
    if hearing.get("modalite"):
        lines.append(
            "Modalité: "
            f"{MODALITE_LABELS.get(hearing['modalite'], hearing['modalite'])}"
        )
    # The video link ALSO goes in DESCRIPTION, not only in the RFC 7986
    # CONFERENCE property: VEVENTs sync to the device CALENDAR (Google
    # Calendar via DavX5), whose Android CalendarContract has no conferencing
    # field — DavX5 drops CONFERENCE and the link never shows. Google Calendar
    # renders a bare URL in the description as a tappable link (user report
    # 2026-07-24, Pixel 10 Pro). CONFERENCE is kept for standards-aware
    # clients.
    if (hearing.get("modalite") == "visioconférence"
            and hearing.get("conference_uri")):
        lines.append(f"Visioconférence: {hearing['conference_uri']}")
    if hearing.get("court"):
        lines.append(f"Cour: {hearing['court']}")
    if hearing.get("judge"):
        lines.append(f"Juge: {hearing['judge']}")
    return _DESCRIPTION_SEPARATOR.join(lines)


def strip_dav_description_suffix(data: dict, existing: dict) -> dict:
    """Take the serializer's metadata lines back off an incoming DESCRIPTION.

    ``vevent_to_hearing`` reads DESCRIPTION whole into ``notes``, and the
    phone sends back the text it was served — metadata lines included.
    Stored as notes, those lines came back once more on the next GET, so
    every edit made on the phone (of ANY field) grew the notes by one
    « Dossier:/Type:/Modalité:/… » block, until the 2000-character ceiling
    truncated the lawyer's own text.

    Exactly the serializer's output is removed, and nothing else: the block
    built from *existing* (what the phone was last served) when the text
    ends with ``"\\n" + block`` or IS the block — in LF or CRLF form. It is
    peeled repeatedly, which also heals the blocks legacy edits already
    accumulated (each one the serializer's own output). Any other text, a
    retouched line included, is left untouched. A *data* without ``notes``
    is returned unchanged (non-effacement: an absent key must stay absent).
    The conference link stays in the DESCRIPTION the phone is SERVED; only
    its echo is kept out of the notes.

    Called by the DAV PUT UPDATE branch only. Mutates *data*; returns it.
    """
    suffix = dav_description_suffix(existing)
    text = data.get("notes")
    if not suffix or not isinstance(text, str):
        return data
    blocks = (suffix, suffix.replace("\n", "\r\n"))
    # CRLF separator first: tried after "\n", it would match the tail of a
    # "\r\n" join and leave a stray "\r" on the lawyer's text.
    tails = tuple(sep + b for sep in ("\r\n", "\n") for b in blocks)
    while True:
        if text in blocks:
            text = ""
            break
        tail = next((t for t in tails if text.endswith(t)), None)
        if tail is None:
            break
        text = text[: -len(tail)]
    data["notes"] = text
    return data


# Hearing status → RFC 5545 VEVENT STATUS. The map is LOSSY — five statuses,
# three values — so « reportée » and « terminée » read back as « à_confirmer »
# and « confirmée » from STATUS alone, and every phone edit (of ANY field)
# silently rewrote a postponed or finished hearing's status. The exact value
# rides in X-PALLAS-STATUS (DavX5 keeps unknown X- properties, as it does
# X-PALLAS-MODALITE); vevent_to_hearing honours it ONLY when
# _DAV_STATUS[exact] equals the STATUS received, so a change made on the
# phone always wins over the stale X-property. Keys are the full
# VALID_STATUSES domain (pinned by test).
_DAV_STATUS = {
    "confirmée": "CONFIRMED",
    "à_confirmer": "TENTATIVE",
    "reportée": "TENTATIVE",
    "annulée": "CANCELLED",
    "terminée": "CONFIRMED",
}
# What STATUS alone reads back as (no or contradicted X-PALLAS-STATUS).
_DAV_STATUS_REVERSE = {
    "CONFIRMED": "confirmée",
    "TENTATIVE": "à_confirmer",
    "CANCELLED": "annulée",
}


def _to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (for iCalendar UTC stamps)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hearing_to_vevent(hearing: dict) -> str:
    """Serialize a hearing dict to an RFC-5545 VEVENT string wrapped in VCALENDAR."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Audience//FR")
    cal.add("version", "2.0")

    event = icalendar.Event()
    event.add("uid", hearing.get("vevent_uid", ""))
    event.add("summary", hearing.get("title", ""))

    # DTSTART / DTEND — emit in America/Montreal so CalDAV clients
    # display the correct local time and include a VTIMEZONE component.
    mtl = MTL  # the one tz authority (tz.py)
    start = hearing.get("start_datetime")
    end = hearing.get("end_datetime")
    if hearing.get("all_day"):
        # DTEND is EXCLUSIVE for a DATE value (RFC 5545 §3.8.2.2), so a
        # one-day event ends on the NEXT day. Emitting end.date() raw was
        # wrong in both directions: create_hearing defaults end to
        # start + 1 h, which for a midnight-UTC all-day gives 01:00 the SAME
        # day, so DTEND equalled DTSTART (a zero-length all-day event, which
        # RFC 5545 forbids); and a genuine multi-day span dropped its last
        # day on the phone. utils/graph_miroir._dates_journee has patched
        # this on the Outlook side since the mirror shipped — the DAV side
        # never was, and a series multiplies the defect by N.
        if start and hasattr(start, "date"):
            debut = start.date()
            event.add("dtstart", debut)
            fin = (
                end.date()
                if end and hasattr(end, "date") and end.date() > debut
                else debut + timedelta(days=1)
            )
            event.add("dtend", fin)
    else:
        if start:
            if start.tzinfo is None or start.tzinfo == timezone.utc:
                start = start.replace(tzinfo=timezone.utc).astimezone(mtl)
            event.add("dtstart", start)
        if end:
            if end.tzinfo is None or end.tzinfo == timezone.utc:
                end = end.replace(tzinfo=timezone.utc).astimezone(mtl)
            event.add("dtend", end)

    # LOCATION
    if hearing.get("location"):
        event.add("location", hearing["location"])

    # DESCRIPTION — the notes, then the metadata lines. The lines are built
    # by dav_description_suffix, which the PUT path uses to take them back
    # OFF (strip_dav_description_suffix): one builder, so the two can never
    # drift apart.
    notes = hearing.get("notes") or ""
    suffix = dav_description_suffix(hearing)
    description = _DESCRIPTION_SEPARATOR.join(p for p in (notes, suffix) if p)
    if description:
        event.add("description", description)

    # CONFERENCE (RFC 7986 §5.11) — only for a video event with a link. Kept
    # for standards-aware clients even though the Android calendar drops it
    # (the DESCRIPTION line above is what actually shows on the device).
    # icalendar 7.0.3 knows CONFERENCE as a URI property and serializes it
    # WITHOUT escaping (raw comma/semicolon preserved — Teams links carry
    # them); do NOT rewrite this to a TEXT encoding.
    if hearing.get("modalite") == "visioconférence" and hearing.get("conference_uri"):
        event.add(
            "conference",
            hearing["conference_uri"],
            parameters={"VALUE": "URI", "FEATURE": "VIDEO"},
        )

    # STATUS — RFC 5545 has three VEVENT values for five statuses, so the
    # exact status ALSO travels as X-PALLAS-STATUS (see _DAV_STATUS for why
    # the parser trusts it only when it agrees with STATUS).
    status = hearing.get("status", "")
    event.add("status", _DAV_STATUS.get(status, "TENTATIVE"))
    if status in _DAV_STATUS:
        event.add("x-pallas-status", status)

    # CATEGORIES
    if hearing.get("hearing_type"):
        label = HEARING_TYPE_LABELS.get(hearing["hearing_type"], hearing["hearing_type"])
        event.add("categories", [label])

    # VALARM — reminder
    reminder_min = hearing.get("reminder_minutes", 1440)
    if reminder_min and reminder_min > 0:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", hearing.get("title", "Audience"))
        alarm.add("trigger", timedelta(minutes=-reminder_min))
        event.add_component(alarm)

    # CREATED + DTSTAMP as UTC date-times. DTSTAMP is MANDATORY per RFC 5545
    # §3.6.1 and was missing entirely; the Android calendar provider tolerates
    # the omission, which is why it went unnoticed while hearings only ever
    # lived in one shared calendar. CREATED matters as soon as a VEVENT reaches a
    # per-dossier collection that jtx Board also subscribes to: its
    # icalobject.created column is NOT NULL and ical4android writes null when
    # the component omits CREATED (the same trap documented for VJOURNAL in
    # models/note.py).
    created = hearing.get("created_at")
    if created and hasattr(created, "hour"):
        event.add("created", _to_utc(created))
    updated = hearing.get("updated_at")
    stamp = updated or created
    if stamp and hasattr(stamp, "hour"):
        event.add("dtstamp", _to_utc(stamp))

    # LAST-MODIFIED
    if updated:
        event.add("last-modified", updated)

    event.add("sequence", 0)

    # Custom X- properties for round-trip fidelity
    if hearing.get("dossier_id"):
        event.add("x-pallas-dossier-id", hearing["dossier_id"])
    if hearing.get("court"):
        event.add("x-pallas-court", hearing["court"])
    if hearing.get("judge"):
        event.add("x-pallas-judge", hearing["judge"])
    if hearing.get("hearing_type"):
        event.add("x-pallas-hearing-type", hearing["hearing_type"])
    # Modalité round-trip (invisible to the client; DESCRIPTION carries the
    # human-readable line).
    if hearing.get("modalite"):
        event.add("x-pallas-modalite", hearing["modalite"])

    cal.add_component(event)
    return cal.to_ical().decode("utf-8")


def vevent_to_hearing(ical_str: str) -> dict:
    """Parse an RFC-5545 VEVENT string into a hearing dict (for CalDAV PUT)."""
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # UID
        uid = component.get("uid")
        if uid:
            data["vevent_uid"] = str(uid)

        # SUMMARY → title
        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        # DTSTART → start_datetime (normalize to UTC for storage)
        dtstart = component.get("dtstart")
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                data["start_datetime"] = dt
                data["all_day"] = False
            else:
                data["start_datetime"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )
                data["all_day"] = True

        # DTEND → end_datetime (normalize to UTC for storage)
        dtend = component.get("dtend")
        if dtend:
            dt = dtend.dt
            if hasattr(dt, "hour"):
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                data["end_datetime"] = dt
            else:
                data["end_datetime"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # LOCATION
        location = component.get("location")
        if location:
            data["location"] = str(location)

        # DESCRIPTION → notes, WHOLE. The metadata lines hearing_to_vevent
        # appended are still on it: only the caller knows which lines the
        # phone was served, so the DAV UPDATE branch takes them off
        # (strip_dav_description_suffix).
        desc = component.get("description")
        if desc:
            data["notes"] = str(desc)

        # STATUS — the exact status comes back through X-PALLAS-STATUS, but
        # only when it still AGREES with the STATUS received: a status the
        # user changed on the phone (TENTATIVE → CONFIRMED) contradicts the
        # stale X-property and wins. An absent STATUS omits the key
        # (non-effacement), whatever the X-property says.
        status = component.get("status")
        if status:
            status_str = str(status).upper()
            exact = unicodedata.normalize(
                "NFC", str(component.get("x-pallas-status") or "")
            )
            if _DAV_STATUS.get(exact) == status_str:
                data["status"] = exact
            else:
                data["status"] = _DAV_STATUS_REVERSE.get(
                    status_str, "à_confirmer"
                )

        # Custom X- properties
        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        court = component.get("x-pallas-court")
        if court:
            data["court"] = str(court)

        judge = component.get("x-pallas-judge")
        if judge:
            data["judge"] = str(judge)

        hearing_type = component.get("x-pallas-hearing-type")
        if hearing_type:
            ht = str(hearing_type)
            if ht in VALID_HEARING_TYPES:
                data["hearing_type"] = ht

        # Modalité / CONFERENCE — NON-EFFACEMENT rule (spec §4.3): OMIT the
        # key when the property is absent from the incoming VEVENT, never
        # write "". A client (jtx/DavX5) that drops these on a plain time
        # edit would otherwise wipe the stored conference link, because
        # update_hearing merges {**existing, **data} — a present-but-empty
        # key overwrites, an absent key survives.
        if "X-PALLAS-MODALITE" in component:
            m = str(component.get("x-pallas-modalite"))
            if m in VALID_MODALITES:
                data["modalite"] = m
        if "CONFERENCE" in component:
            uri = str(component.get("conference"))
            # An incoming URI is client-supplied: re-run the scheme whitelist.
            # A rejected URI is IGNORED (key omitted → stored value survives),
            # never propagated.
            if is_safe_conference_uri(uri):
                data["conference_uri"] = uri

        # VALARM → reminder_minutes
        for sub in component.subcomponents:
            if sub.name == "VALARM":
                trigger = sub.get("trigger")
                if trigger and hasattr(trigger, "dt"):
                    td = trigger.dt
                    if isinstance(td, timedelta):
                        data["reminder_minutes"] = abs(int(td.total_seconds() / 60))

        break  # Only process first VEVENT

    return data
