"""Bookings sync cron handler (spec L2 §4).

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable only
by App Engine cron: the ``X-Appengine-Cron: true`` header is STRIPPED from all
external traffic, so the in-handler guard is proof of origin (the
before_request bypasses in security.py rely on the same fact).

Every 10 minutes it reads « Bookings with me » reservations from the juriste's
mailbox and upserts them as hearings gated behind ``confirmation="à_confirmer"``
(invisible to DAV/MCP/Calendar-DavX5 until the juriste confirms in Réception).
Idempotent: every write is conditioned on a real change, so a re-run writes
nothing (acceptance criterion §7.6).
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, request

from config import Config
# NB: no dav.sync import here on purpose — a Bookings IMPORT never bumps a
# CTag (it stays invisible in DAV until confirmed). The bump lives in the
# Réception confirmation route.
from models import hearing
from tz import to_mtl
from utils import graph_calendrier
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event

logger = logging.getLogger(__name__)

taches_bookings_bp = Blueprint(
    "taches_bookings", __name__, url_prefix="/taches/bookings"
)


def _fmt(dt) -> str:
    """Human-readable Montréal timestamp for a divergence detail line."""
    if not isinstance(dt, datetime):
        return "—"
    local = to_mtl(dt)
    return f"{local.strftime('%Y-%m-%d %H:%M')}"


def _type_audience(mot_cle: str) -> str:
    """hearing_type dérivé du service Bookings détecté.

    Le juriste publie plusieurs types de rendez-vous (« Consultation »,
    « Réunion »…) ; les couler tous dans « consultation » perdrait d'emblée
    l'information que Bookings donne gratuitement. Un mot-clé non mappé
    retombe sur le défaut mais le DIT : un service ajouté dans app.yaml sans
    entrée dans la table serait sinon importé sous un type faux, en silence.
    """
    plie = graph_calendrier._plier(mot_cle)
    type_ = Config.BOOKINGS_TYPE_PAR_MOT_CLE.get(plie)
    if type_ is None:
        logger.warning(
            "bookings: mot-clé sans type d'audience mappé, repli sur %s",
            Config.BOOKINGS_TYPE_DEFAUT,
        )
        return Config.BOOKINGS_TYPE_DEFAUT
    return type_


def _creer(r: dict, counters: dict) -> None:
    """Create a new hearing from a Bookings reservation (NO CTag bump —
    it stays invisible in DAV until confirmed)."""
    data = {
        "source": "bookings",
        "confirmation": "à_confirmer",
        "hearing_type": _type_audience(r["mot_cle"]),
        # The meeting IS confirmed with the client (Bookings); « confirmation »
        # is the Athéna-side review gate, orthogonal to « status ».
        "status": "confirmée",
        "title": r["title"],
        "start_datetime": r["start_datetime"],
        "end_datetime": r["end_datetime"],
        "location": r["location"],
        "modalite": r["modalite"],
        "conference_uri": r["conference_uri"],
        "client_email": r["client_email"],
        "client_nom": r["client_nom"],
        "graph_event_id": r["graph_event_id"],
        "graph_ical_uid": r["graph_ical_uid"],
        "graph_last_modified": r["graph_last_modified"],
    }
    _created, errors = hearing.create_hearing(data)
    if errors:
        # Sans ce journal, un refus de validation était avalé en silence et
        # retenté toutes les 10 minutes indéfiniment : la réservation ne
        # paraissait jamais en Réception et rien n'en disait la cause. Les
        # messages du modèle ne portent ni sujet ni adresse.
        logger.warning("bookings: création refusée par le modèle: %s", errors)
        return
    counters["crees"] += 1


def _rapprocher_modif(existing: dict, r: dict, counters: dict) -> None:
    """Reconcile a modified reservation against its stored hearing.

    Idempotence: no write unless lastModifiedDateTime advanced. An à_confirmer
    import is updated silently; a CONFIRMED one is never overwritten — a start/
    end change becomes a divergence alert for the juriste to apply or ignore.
    """
    incoming_mod = r.get("graph_last_modified") or ""
    if incoming_mod and incoming_mod == (existing.get("graph_last_modified") or ""):
        return  # nothing changed since last sync

    conf = existing.get("confirmation") or ""
    if conf == "à_confirmer":
        hearing.update_hearing(existing["id"], {
            "title": r["title"],
            "start_datetime": r["start_datetime"],
            "end_datetime": r["end_datetime"],
            "location": r["location"],
            "modalite": r["modalite"],
            "conference_uri": r["conference_uri"],
            "client_email": r["client_email"],
            "client_nom": r["client_nom"],
            "graph_event_id": r["graph_event_id"],
            "graph_last_modified": incoming_mod,
        })
        counters["modifies"] += 1
        return

    # Confirmed: never overwrite. Record a divergence when the slot changed;
    # otherwise just advance graph_last_modified so we don't re-evaluate it.
    old_start = existing.get("start_datetime")
    old_end = existing.get("end_datetime")
    if r["start_datetime"] != old_start or r["end_datetime"] != old_end:
        new_start = r["start_datetime"]
        new_end = r["end_datetime"]
        div = {
            "motif": "modifié_côté_client",
            "detail": f"{_fmt(old_start)} → {_fmt(new_start)}",
            "nouveau_debut": new_start.isoformat() if new_start else "",
            "nouveau_fin": new_end.isoformat() if new_end else "",
            "vu": False,
        }
        hearing.update_hearing(existing["id"], {
            "bookings_divergence": div,
            "graph_last_modified": incoming_mod,
        })
        counters["divergences"] += 1
    else:
        hearing.update_hearing(existing["id"], {"graph_last_modified": incoming_mod})


def _appliquer_annulation(existing: dict, counters: dict) -> None:
    """A reservation vanished / was cancelled client-side.

    à_confirmer → confirmation becomes « annulée_client ». CONFIRMED → a
    « annulé_côté_client » divergence, without touching confirmation (§4.5).
    Idempotent: an already-annulée or already-flagged hearing is left alone.
    """
    conf = existing.get("confirmation") or ""
    if conf == "à_confirmer":
        hearing.update_hearing(existing["id"], {"confirmation": "annulée_client"})
        counters["annules"] += 1
    elif conf == "":  # confirmed
        current = existing.get("bookings_divergence") or {}
        if current.get("motif") == "annulé_côté_client":
            return  # already flagged
        div = {
            "motif": "annulé_côté_client",
            "detail": "Le client a annulé le rendez-vous côté Bookings.",
            "vu": False,
        }
        hearing.update_hearing(existing["id"], {"bookings_divergence": div})
        counters["annules"] += 1


def _debug_payload(bruts: list[dict]) -> None:
    """§4.4 predicate tuning: log the first detected + first undetected event.
    PII-FREE — the meeting SUBJECT is NEVER logged (it embeds the client name);
    only the predicate booleans + domain-reduced addresses, which is what
    actually diagnoses why an event matched or not.

    Emitted at INFO, not DEBUG: the root logger sits at INFO in production
    (utils/logging_setup), so a DEBUG line produced NOTHING there — the one
    tool meant for tuning the predicate was mute exactly where it is needed.
    BOOKINGS_DEBUG_PAYLOAD is itself the gate, and it defaults to false.
    """
    def _domains(ev: dict) -> list[str]:
        out = []
        for att in ev.get("attendees") or []:
            addr = ((att.get("emailAddress") or {}).get("address")) or ""
            out.append(addr.rsplit("@", 1)[-1] if "@" in addr else "?")
        return out

    upn = Config.BOOKINGS_JURISTE_UPN.lower()
    detected = next((e for e in bruts if graph_calendrier.est_reservation(e)), None)
    undetected = next(
        (e for e in bruts if not graph_calendrier.est_reservation(e)), None
    )
    for label, ev in (("detected", detected), ("undetected", undetected)):
        if ev is None:
            continue
        org = (
            ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address")
            or ""
        ).lower()
        subj = ev.get("subject") or ""
        mot_cle = graph_calendrier.mot_cle_correspondant(ev)
        logger.info(
            "bookings predicate sample (%s): organizer_match=%s keyword_match=%s "
            "mot_cle=%r subject_len=%d organizer_domain=%r attendee_domains=%r",
            label,
            org == upn,
            bool(mot_cle),
            mot_cle,
            len(subj),
            org.rsplit("@", 1)[-1],
            _domains(ev),
        )


def _synchroniser() -> dict:
    """Read the window, reconcile by graph_ical_uid, return the counters."""
    now = datetime.now(timezone.utc)
    debut = now - timedelta(days=Config.BOOKINGS_SYNC_LOOKBACK_DAYS)
    fin = now + timedelta(days=Config.BOOKINGS_SYNC_LOOKAHEAD_DAYS)

    bruts = graph_calendrier.lister_reservations(debut, fin)
    if Config.BOOKINGS_DEBUG_PAYLOAD:
        _debug_payload(bruts)

    # THE TRAP: the reconciliation lookup must see EVERY prior import,
    # including refusée and annulée_client. list_hearings(include_unconfirmed=
    # True) hides refusée (dropped in both modes), so a refused reservation
    # whose best-effort Outlook cancel failed would return from calendarView
    # and be re-imported as a fresh à_confirmer every cycle. list_bookings_all
    # bypasses the confirmation filter for this ONE sync-internal caller.
    existants = {
        h["graph_ical_uid"]: h
        for h in hearing.list_bookings_all()
        if h.get("graph_ical_uid")
    }

    counters = {
        "vus": len(bruts), "detectes": 0, "crees": 0,
        "modifies": 0, "annules": 0, "divergences": 0,
    }
    detected_uids: set[str] = set()

    for ev in bruts:
        if not graph_calendrier.est_reservation(ev):
            continue
        r = graph_calendrier.extraire(ev)
        counters["detectes"] += 1
        uid = r["graph_ical_uid"]
        if not uid:
            continue  # no stable key → cannot reconcile
        detected_uids.add(uid)
        existing = existants.get(uid)
        # A refused booking is a FINAL juriste decision. Never resurrect it,
        # even if the best-effort Outlook cancellation didn't take and Graph
        # still returns the event as active (uid is in detected_uids, so the
        # absence loop below leaves it alone too).
        if existing is not None and existing.get("confirmation") == "refusée":
            continue
        if r["is_cancelled"]:
            if existing:
                _appliquer_annulation(existing, counters)
            continue
        if existing is None:
            _creer(r, counters)
        else:
            _rapprocher_modif(existing, r, counters)

    # Absence-based cancellation: a stored booking whose start is IN the window
    # but which Graph no longer returns was cancelled client-side. Never
    # conclude cancellation for an out-of-window event (§4.5).
    for uid, existing in existants.items():
        if uid in detected_uids:
            continue
        start = existing.get("start_datetime")
        if isinstance(start, datetime) and debut <= start <= fin:
            _appliquer_annulation(existing, counters)

    return counters


@taches_bookings_bp.get("/sync")
def sync():
    # Proof of origin: App Engine strips X-Appengine-* from all external
    # traffic; only a genuine cron dispatch carries this value.
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    if not Config.BOOKINGS_SYNC_ACTIVE:
        return jsonify({"actif": False})
    if not Config.BOOKINGS_SUBJECT_KEYWORDS:
        # Un BOOKINGS_SUBJECT_KEYWORDS vide (valeur vide, virgule seule,
        # espaces) produit un tuple vide, et le prédicat ne mord alors JAMAIS :
        # la synchro tourne toutes les 10 minutes sans jamais rien importer, et
        # rien ne le dit. Pire, la boucle d'absence finirait par déclarer
        # « annulées côté client » les réservations déjà importées. Le dire
        # fort est la seule défense contre une panne totale silencieuse.
        log_bookings_event(
            "bookings_sync_erreur_graph", "failure", reason="aucun_mot_cle"
        )
        return jsonify({"actif": True, "mots_cles": 0}), 200
    if not Config.bookings_configured():
        # Graph creds or the mailbox are absent — nothing to poll. Fail-open.
        log_bookings_event(
            "bookings_sync_erreur_graph", "refused", reason="not_configured"
        )
        return jsonify({"actif": True, "configure": False})

    try:
        counters = _synchroniser()
    except (GraphError, GraphNotConfigured):
        # A Graph outage is transient — log and return 200 (the next 10-min
        # cycle retries); a 500 would only spawn a cron retry storm.
        logger.exception("bookings sync graph call failed")
        log_bookings_event(
            "bookings_sync_erreur_graph", "failure", reason="graph_error"
        )
        return jsonify({"actif": True, "erreur": "graph"}), 200

    log_bookings_event("bookings_sync_execute", **counters)
    return jsonify({"actif": True, **counters})
