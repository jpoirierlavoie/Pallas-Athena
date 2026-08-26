"""Hearing (court date) Firestore CRUD and RFC-5545 VEVENT serialization."""

import logging
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

import icalendar

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize
from tz import MTL, mtl_to_utc, to_mtl
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


def create_hearing(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    # Auto-set end_datetime if not provided (start + 1 hour)
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    # Honour a caller-supplied id / UID, as create_task and create_note do.
    # A CalDAV PUT names the resource in its URL, so minting a fresh uuid
    # here stored the event under an id the client never learns: it PUTs
    # /dav/.../abc.ics, gets 201, and every later GET of abc.ics 404s while
    # a duplicate under another id syncs down. Same for vevent_uid — a
    # regenerated UID reads as a different event to the client.
    hearing_id = merged.get("id") or str(uuid.uuid4())
    vevent_uid = merged.get("vevent_uid") or str(uuid.uuid4())

    merged.update({
        "id": hearing_id,
        "created_at": merged.get("created_at") or now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "vevent_uid": vevent_uid,
        "dav_href": dav_href_for(merged.get("dossier_id", ""), hearing_id),
    })

    try:
        db.collection(COLLECTION).document(hearing_id).set(merged)
    except Exception:
        log_unexpected("hearing write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


def get_hearing(hearing_id: str) -> Optional[dict]:
    """Fetch a single hearing by ID."""
    try:
        doc = db.collection(COLLECTION).document(hearing_id).get()
        if doc.exists:
            return _migrate_hearing(doc.to_dict())
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
    """
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("source", "==", "bookings")
        )
        return [_migrate_hearing(doc.to_dict()) for doc in query.stream()]
    except Exception:
        logger.warning("list_bookings_all: query failed")
        return []


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


def update_hearing(
    hearing_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing hearing. Returns (updated_doc, errors)."""
    existing = get_hearing(hearing_id)
    if not existing:
        return None, ["Audience introuvable."]

    merged = {**existing, **_sanitize_data(data)}

    # Auto-set end_datetime if not provided
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(hearing_id).set(merged)
    except Exception:
        log_unexpected("hearing write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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
        return start.date()
    return to_mtl(start).date()


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

    # L'appelant ne nomme JAMAIS l'identité d'une occurrence. create_hearing
    # honore un id fourni (l'affordance CalDAV), donc laisser passer un id ou
    # un vevent_uid ici ferait N batch.set() sur LA MÊME référence : Firestore
    # garde le dernier, en silence, et 59 occurrences sur 60 disparaissent
    # avec un retour de succès. Idem pour les champs appartenant au serveur.
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
            "created_at": now,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
            "vevent_uid": str(uuid.uuid4()),
            "dav_href": dav_href_for(dossier_id, hearing_id),
        }
        occurrences.append(occ)

    try:
        batch = db.batch()
        for occ in occurrences:
            batch.set(db.collection(COLLECTION).document(occ["id"]), occ)
        bump_ctag_in_batch(batch, collection_for(dossier_id))
        batch.commit()
    except Exception:
        log_unexpected("hearing series write failed")
        return [], ["Erreur lors de la sauvegarde. Veuillez réessayer."]

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
    """Supprimer une série — les occurrences, leurs pierres tombales et le
    bump de CTag dans UN SEUL lot.

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

    # 2N + 1 opérations (N suppressions + N pierres tombales + 1 bump).
    if 2 * len(rows) + 1 > _BATCH_CHUNK:
        return [], [
            "Cette série est trop longue pour être supprimée d'un seul bloc."
        ]

    dossier_id = rows[0].get("dossier_id", "")
    sync_name = collection_for(dossier_id)
    ids = [h["id"] for h in rows]

    try:
        batch = db.batch()
        for hid in ids:
            batch.delete(db.collection(COLLECTION).document(hid))
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
    return update_hearing(hearing_id, {"serie_id": "", "serie_rule": None})


# ── Summary ──────────────────────────────────────────────────────────────


def get_hearing_summary(dossier_id: str) -> dict:
    """Return hearing counts for a dossier."""
    hearings = list_hearings(dossier_id=dossier_id)
    now = datetime.now(timezone.utc)
    upcoming = [h for h in hearings if h.get("start_datetime") and h["start_datetime"] > now and h.get("status") not in ("annulée", "terminée")]
    past = [h for h in hearings if h.get("start_datetime") and h["start_datetime"] <= now or h.get("status") in ("terminée",)]
    return {
        "total": len(hearings),
        "upcoming": len(upcoming),
        "past": len(past),
    }


# ── RFC-5545 VEVENT serialization ─────────────────────────────────────────


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

    # DESCRIPTION — combine notes with dossier info
    desc_parts = []
    if hearing.get("notes"):
        desc_parts.append(hearing["notes"])
    # Standalone agenda events have no dossier — omit the line entirely.
    if hearing.get("dossier_id"):
        desc_parts.append(
            f"Dossier: {hearing.get('dossier_file_number', '')} - {hearing.get('dossier_title', '')}"
        )
    if hearing.get("hearing_type"):
        label = HEARING_TYPE_LABELS.get(hearing["hearing_type"], hearing["hearing_type"])
        desc_parts.append(f"Type: {label}")
    # Modalité in DESCRIPTION only (visible in every client). NOT in
    # CATEGORIES — that would add a second colored tile in a jtx-style client.
    if hearing.get("modalite"):
        desc_parts.append(
            f"Modalité: {MODALITE_LABELS.get(hearing['modalite'], hearing['modalite'])}"
        )
    # The video link ALSO goes in DESCRIPTION, not only in the RFC 7986
    # CONFERENCE property below: VEVENTs sync to the device CALENDAR (Google
    # Calendar via DavX5), whose Android CalendarContract has no conferencing
    # field — DavX5 drops CONFERENCE and the link never shows. Google Calendar
    # renders a bare URL in the description as a tappable link (user report
    # 2026-07-24, Pixel 10 Pro). CONFERENCE is kept for standards-aware clients.
    if hearing.get("modalite") == "visioconférence" and hearing.get("conference_uri"):
        desc_parts.append(f"Visioconférence: {hearing['conference_uri']}")
    if hearing.get("court"):
        desc_parts.append(f"Cour: {hearing['court']}")
    if hearing.get("judge"):
        desc_parts.append(f"Juge: {hearing['judge']}")
    if desc_parts:
        event.add("description", "\n".join(desc_parts))

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

    # STATUS mapping
    status_map = {
        "confirmée": "CONFIRMED",
        "à_confirmer": "TENTATIVE",
        "reportée": "TENTATIVE",
        "annulée": "CANCELLED",
        "terminée": "CONFIRMED",
    }
    event.add("status", status_map.get(hearing.get("status", ""), "TENTATIVE"))

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

        # DESCRIPTION → notes (just the first line; rest is metadata)
        desc = component.get("description")
        if desc:
            data["notes"] = str(desc)

        # STATUS
        status = component.get("status")
        if status:
            status_str = str(status).upper()
            reverse_map = {
                "CONFIRMED": "confirmée",
                "TENTATIVE": "à_confirmer",
                "CANCELLED": "annulée",
            }
            data["status"] = reverse_map.get(status_str, "à_confirmer")

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
