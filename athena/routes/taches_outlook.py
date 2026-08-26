"""Miroir Outlook cron handler — audiences Athéna → calendrier du juriste.

MACHINE blueprint — no @login_required, CSRF-exempt (main.py), reachable only
by App Engine cron: the ``X-Appengine-Cron: true`` header is STRIPPED from all
external traffic, so the in-handler guard is proof of origin.

Every 10 minutes it reconciles the confirmed hearings against the mirrored
events in the juriste's DEFAULT Outlook calendar (create / patch / delete by
diff). One-way: Athéna is authoritative, an Outlook-side edit is stomped back
(user decision 2026-07-29). The sweep is READ-ONLY on Firestore — the mapping
lives in each mirrored event's extended property (see utils/graph_miroir).

Loop safety versus the Bookings sync (which polls the SAME calendar): every
mirrored event carries the marker that ``mot_cle_correspondant`` refuses
before its keyword logic, and ``_retenir`` below never mirrors a
``source == "bookings"`` hearing (it already IS an Outlook event).
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, request

from config import Config
# NB: pas d'import dav.sync — le miroir n'écrit RIEN dans Firestore, donc
# aucun CTag à toucher (Outlook n'est pas une collection DAV).
from models import hearing
from utils import graph_miroir
from utils.graph import GraphError, GraphNotConfigured
from utils.logging_setup import log_bookings_event

logger = logging.getLogger(__name__)

taches_outlook_bp = Blueprint(
    "taches_outlook", __name__, url_prefix="/taches/outlook"
)

# Au-delà de cette lecture, la fenêtre est TRONQUÉE : le jeu désiré ne décrit
# plus tout ce qui existe, et la phase de suppression est désarmée (voir
# _synchroniser — un desired tronqué piloterait une suppression massive).
#
# Relevé de 500 à 1500 avec l'arrivée des séries récurrentes : une série
# hebdomadaire au plafond (60 occurrences) occupe ~56 lignes en permanence
# dans la fenêtre glissante de 395 jours, si bien que neuf séries suffisaient
# à atteindre 500 et à désarmer définitivement le nettoyage des orphelins.
# NE PAS réduire MIROIR_OUTLOOK_LOOKAHEAD_DAYS à la place : l'invariant
# anti-orphelin ci-dessous tient UNIQUEMENT parce qu'un miroir ne peut sortir
# de la fenêtre que par le bord passé.
_LIMITE_FENETRE = 1500

# Plafond de MUTATIONS Graph par balayage (créations + corrections +
# suppressions). Chaque mutation est un aller-retour HTTPS sérialisé
# (~300-600 ms, poignée de main TLS comprise) et le cron cible le service
# default (gunicorn --timeout 60) : ~100-200 mutations — première
# activation, rattrapage, série volumineuse — franchissaient le mur et le
# worker était SIGKILLé en plein balayage, sans compteurs ni journal. Le
# balayage étant incrémental et idempotent (dédup par marqueur), le reste
# part au cycle suivant, 10 minutes plus tard ; le reliquat est compté
# (`restants`) et journalisé, jamais tu.
_PLAFOND_MUTATIONS = 80


def _retenir(h: dict) -> bool:
    """Une audience méritant un miroir : confirmée, ni import Bookings, ni
    annulée.

    ``source == "bookings"`` est exclu parce que l'événement EXISTE déjà dans
    Outlook — c'est Bookings qui l'y a créé ; le miroiter doublerait chaque
    rendez-vous client. ``confirmation == ""`` ré-applique explicitement le
    contrat de visibilité (le fetch passe include_unconfirmed=True pour
    garder le signal de troncature honnête).
    """
    return bool(
        h.get("id")
        and isinstance(h.get("start_datetime"), datetime)
        and (h.get("confirmation") or "") == ""
        and h.get("source") != "bookings"
        and h.get("status") != "annulée"
    )


def _synchroniser() -> dict | None:
    """Diff Athéna ↔ Outlook sur UNE fenêtre partagée ; rend les compteurs.

    La fenêtre est la même des deux côtés — l'invariant anti-orphelin : un
    miroir ne peut sortir de la fenêtre que par le bord PASSÉ (sa date est
    figée, le bord futur avance avec l'horloge), donc une audience reportée
    au-delà de l'horizon laisse son miroir DANS la fenêtre, où le diff le
    supprime comme orphelin puis le recrée quand la nouvelle date entre.
    """
    now = datetime.now(timezone.utc)
    debut = now - timedelta(days=Config.MIROIR_OUTLOOK_LOOKBACK_DAYS)
    fin = now + timedelta(days=Config.MIROIR_OUTLOOK_LOOKAHEAD_DAYS)

    # list_hearings_in_range_state, jamais la variante simple : ce balayage
    # SUPPRIME sur la foi d'une absence, donc il lui faut les deux signaux que
    # la liste seule ne porte pas.
    #
    #   * window_full est mesuré sur la fenêtre BRUTE. Le mesurer sur les
    #     lignes rendues serait faux : _filter_confirmation retire les imports
    #     « refusée » DANS LES DEUX MODES, si bien qu'une seule réservation
    #     refusée dans la fenêtre faisait passer une lecture tronquée pour
    #     complète — et réarmait la suppression des miroirs situés au-delà de
    #     la coupe, c'est-à-dire de vraies dates de cour dans Outlook.
    #   * ok distingue « rien ne correspond » de « la requête a échoué ». Les
    #     deux rendent une liste vide, et les confondre supprime TOUS les
    #     miroirs sur un simple hoquet Firestore.
    fenetre = hearing.list_hearings_in_range_state(
        debut, fin, limit=_LIMITE_FENETRE, include_unconfirmed=True
    )
    if not fenetre.ok:
        # Jeu désiré non fiable : ne rien créer, ne rien corriger, ne rien
        # supprimer. Le cycle suivant reprendra dans 10 minutes.
        log_bookings_event(
            "miroir_outlook_erreur_graph", "failure", reason="lecture_firestore"
        )
        return None
    rows = fenetre.rows
    fenetre_pleine = fenetre.window_full
    desired = {h["id"]: h for h in rows if _retenir(h)}

    counters = {
        "vus": len(rows),
        "miroirs": 0,
        "crees": 0,
        "corriges": 0,
        "supprimes": 0,
        "ignores": len(rows) - len(desired),
        "erreurs": 0,
        "restants": 0,
    }
    mutations = 0

    def _budget_epuise() -> bool:
        # Une tentative compte, réussie ou non — c'est l'aller-retour qui
        # coûte, pas le résultat.
        return mutations >= _PLAFOND_MUTATIONS

    miroirs = graph_miroir.lister_miroirs(debut, fin)
    counters["miroirs"] = len(miroirs)

    par_audience: dict[str, list[dict]] = {}
    for ev in miroirs:
        hid, _etag = graph_miroir.lire_marqueur(ev)
        par_audience.setdefault(hid, []).append(ev)

    for hid, evs in par_audience.items():
        # Doublons (retry de création passé entre les mailles) : garder le
        # plus petit id — déterministe — et purger le reste, toujours sûr.
        evs.sort(key=lambda e: e.get("id") or "")
        garde, doublons = evs[0], evs[1:]
        for ev in doublons:
            if _budget_epuise():
                counters["restants"] += 1
                continue
            mutations += 1
            try:
                graph_miroir.supprimer_miroir(ev.get("id") or "")
                counters["supprimes"] += 1
            except GraphError:
                counters["erreurs"] += 1
        h = desired.get(hid)
        if h is None:
            # Orphelin — l'audience a disparu du jeu désiré. JAMAIS sur une
            # fenêtre tronquée : le desired incomplet transformerait chaque
            # audience au-delà de la coupe en « orpheline » à supprimer.
            if fenetre_pleine:
                continue
            if _budget_epuise():
                counters["restants"] += 1
                continue
            mutations += 1
            try:
                graph_miroir.supprimer_miroir(garde.get("id") or "")
                counters["supprimes"] += 1
            except GraphError:
                counters["erreurs"] += 1
        elif graph_miroir.doit_corriger(h, garde):
            if _budget_epuise():
                counters["restants"] += 1
                continue
            mutations += 1
            try:
                graph_miroir.corriger_miroir(garde.get("id") or "", h)
                counters["corriges"] += 1
            except GraphError:
                counters["erreurs"] += 1

    for hid, h in desired.items():
        if hid in par_audience:
            continue
        if _budget_epuise():
            counters["restants"] += 1
            continue
        mutations += 1
        try:
            graph_miroir.creer_miroir(h)
            counters["crees"] += 1
        except GraphError:
            counters["erreurs"] += 1

    # Le reliquat au-delà du plafond voyage dans la ligne de compteurs
    # (`restants` dans miroir_outlook_execute) : rien de perdu — le balayage
    # est idempotent et le cycle suivant reprend dans 10 minutes — et jamais
    # muet, un arriéré qui ne se résorbe pas se lit à chaque cycle.

    if fenetre_pleine:
        # ERROR, jamais muet : tant que la fenêtre déborde, les suppressions
        # sont désarmées et des audiences au-delà de la coupe n'ont pas de
        # miroir. Le remède est de relever _LIMITE_FENETRE.
        log_bookings_event(
            "miroir_outlook_erreur_graph",
            "failure",
            reason="fenetre_pleine",
            vus=counters["vus"],
        )
    return counters


@taches_outlook_bp.get("/sync")
def sync():
    # Proof of origin: App Engine strips X-Appengine-* from all external
    # traffic; only a genuine cron dispatch carries this value.
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)

    if not Config.MIROIR_OUTLOOK_ACTIF:
        # Kill switch : gèle les miroirs EN PLACE (aucun nettoyage) — la
        # purge manuelle est la suppression des événements catégorisés
        # « Pallas Athéna » dans Outlook.
        return jsonify({"actif": False})
    if not Config.bookings_configured():
        # Graph creds or the mailbox are absent — nothing to write. Fail-open.
        log_bookings_event(
            "miroir_outlook_erreur_graph", "refused", reason="not_configured"
        )
        return jsonify({"actif": True, "configure": False})

    try:
        counters = _synchroniser()
    except (GraphError, GraphNotConfigured):
        # A Graph outage is transient — log and return 200 (the next 10-min
        # cycle retries); a 500 would only spawn a cron retry storm.
        logger.exception("outlook mirror graph call failed")
        log_bookings_event(
            "miroir_outlook_erreur_graph", "failure", reason="graph_error"
        )
        return jsonify({"actif": True, "erreur": "graph"}), 200

    if counters is None:
        # Lecture Firestore en échec : le balayage s'est abstenu (il a déjà
        # journalisé). 200 — le cycle suivant reprend.
        return jsonify({"actif": True, "erreur": "lecture"}), 200

    log_bookings_event("miroir_outlook_execute", **counters)
    return jsonify({"actif": True, **counters})
