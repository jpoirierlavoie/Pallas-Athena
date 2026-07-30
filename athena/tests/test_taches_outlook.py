"""Miroir Outlook — balayage cron (routes/taches_outlook).

Pins the silent-failure zones of the sweep:
- the mirror NEVER writes to Firestore (read-only by design — negative
  assertion, the test_sync_lookup_uses_list_bookings_all pattern);
- a Bookings-sourced hearing is never mirrored (it already IS the Outlook
  event — mirroring would duplicate every client meeting);
- a truncated fetch window disarms the DELETE phase (a truncated desired set
  would mass-delete valid mirrors) and says so at ERROR;
- per-event Graph failures are counted, the sweep continues;
- the cron endpoint 403s without the X-Appengine-Cron header.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.taches_outlook as to
    import models.hearing as h

from config import Config  # noqa: E402
from utils.graph import GraphError  # noqa: E402
from utils.graph_calendrier import MIROIR_CATEGORIE, MIROIR_PROP_ID  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"
NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)
    monkeypatch.setattr(Config, "MIROIR_OUTLOOK_ACTIF", True)
    monkeypatch.setattr(Config, "MIROIR_OUTLOOK_LOOKBACK_DAYS", 30)
    monkeypatch.setattr(Config, "MIROIR_OUTLOOK_LOOKAHEAD_DAYS", 365)


def _audience(hid="h-1", etag="e-1", days=3, **over):
    start = NOW + timedelta(days=days)
    base = {
        "id": hid,
        "etag": etag,
        "title": "Audience CS",
        "start_datetime": start,
        "end_datetime": start + timedelta(hours=1),
        "all_day": False,
        "location": "Palais de justice",
        "reminder_minutes": 1440,
        "dossier_file_number": "2026-001",
        "modalite": "présentiel",
        "conference_uri": "",
        "confirmation": "",
        "source": "",
        "status": "confirmée",
    }
    base.update(over)
    return base


def _miroir_de(aud, event_id="EVT-1", etag_stampe=None, **over):
    """L'événement Graph conforme à *aud* (doit_corriger → False), sauf
    surcharges."""
    ev = {
        "id": event_id,
        "subject": aud["title"],
        "start": {
            "dateTime": aud["start_datetime"]
            .astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000",
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": aud["end_datetime"]
            .astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + ".0000000",
            "timeZone": "UTC",
        },
        "isAllDay": False,
        "location": {"displayName": aud["location"]},
        "isReminderOn": True,
        "reminderMinutesBeforeStart": aud["reminder_minutes"],
        "showAs": "busy",
        "categories": [MIROIR_CATEGORIE],
        "singleValueExtendedProperties": [
            {
                "id": MIROIR_PROP_ID,
                "value": f"{aud['id']}|{etag_stampe or aud['etag']}",
            }
        ],
    }
    ev.update(over)
    return ev


class _MiroirSpy:
    def __init__(self, echoue_sur=()):
        self.crees, self.corriges, self.supprimes = [], [], []
        self._echoue_sur = set(echoue_sur)

    def creer(self, aud):
        if aud["id"] in self._echoue_sur:
            raise GraphError("Échec d'un POST Graph (HTTP 503).")
        self.crees.append(aud["id"])

    def corriger(self, event_id, aud):
        if event_id in self._echoue_sur:
            raise GraphError("Échec d'un PATCH Graph (HTTP 503).")
        self.corriges.append((event_id, aud["id"]))

    def supprimer(self, event_id):
        if event_id in self._echoue_sur:
            raise GraphError("Échec d'un DELETE Graph (HTTP 503).")
        self.supprimes.append(event_id)


def _run(monkeypatch, audiences, miroirs, echoue_sur=()):
    s = _MiroirSpy(echoue_sur)
    monkeypatch.setattr(
        h, "list_hearings_in_range",
        lambda debut, fin, limit, include_unconfirmed: audiences,
    )
    monkeypatch.setattr(to.graph_miroir, "lister_miroirs",
                        lambda debut, fin: miroirs)
    monkeypatch.setattr(to.graph_miroir, "creer_miroir", s.creer)
    monkeypatch.setattr(to.graph_miroir, "corriger_miroir", s.corriger)
    monkeypatch.setattr(to.graph_miroir, "supprimer_miroir", s.supprimer)
    return to._synchroniser(), s


# ── Le diff ───────────────────────────────────────────────────────────────


def test_cree_les_manquants(monkeypatch):
    counters, s = _run(monkeypatch, [_audience()], [])
    assert counters["crees"] == 1 and s.crees == ["h-1"]
    assert not s.corriges and not s.supprimes


def test_noop_cycle_stable(monkeypatch):
    """Un miroir conforme ne déclenche RIEN — l'idempotence du balayage."""
    aud = _audience()
    counters, s = _run(monkeypatch, [aud], [_miroir_de(aud)])
    assert not s.crees and not s.corriges and not s.supprimes
    assert counters["miroirs"] == 1 and counters["corriges"] == 0


def test_corrige_sur_etag_change(monkeypatch):
    """L'audience a changé dans Athéna : l'etag stampé est en retard."""
    aud = _audience(etag="e-2")
    _counters, s = _run(monkeypatch, [aud],
                        [_miroir_de(aud, etag_stampe="e-1")])
    assert s.corriges == [("EVT-1", "h-1")]


def test_corrige_apres_modification_outlook(monkeypatch):
    """Athéna écrase (décision 2026-07-29) : un titre retouché dans Outlook
    est rétabli au cycle suivant, même à etag stampé à jour."""
    aud = _audience()
    _counters, s = _run(monkeypatch, [aud],
                        [_miroir_de(aud, subject="Titre modifié dans Outlook")])
    assert s.corriges == [("EVT-1", "h-1")]


def test_supprime_l_orphelin(monkeypatch):
    """L'audience a disparu du jeu désiré (supprimée, reportée hors fenêtre,
    annulée) : son miroir est retiré d'Outlook."""
    aud = _audience()
    counters, s = _run(monkeypatch, [], [_miroir_de(aud)])
    assert s.supprimes == ["EVT-1"] and counters["supprimes"] == 1


def test_doublons_miroirs_purges(monkeypatch):
    """Deux miroirs pour la même audience (retry de création passé entre les
    mailles) : le plus petit id est gardé, l'autre purgé — déterministe."""
    aud = _audience()
    counters, s = _run(
        monkeypatch, [aud],
        [_miroir_de(aud, event_id="EVT-B"), _miroir_de(aud, event_id="EVT-A")],
    )
    assert s.supprimes == ["EVT-B"]
    assert not s.corriges and not s.crees
    assert counters["supprimes"] == 1


# ── Ce qui n'est JAMAIS miroité ───────────────────────────────────────────


def test_ignore_source_bookings(monkeypatch):
    """Un import Bookings EST déjà un événement Outlook — le miroiter
    doublerait chaque rendez-vous client dans le calendrier."""
    counters, s = _run(
        monkeypatch,
        [_audience(source="bookings", graph_ical_uid="ical-1")],
        [],
    )
    assert not s.crees and counters["ignores"] == 1


def test_ignore_non_confirme_et_annulee(monkeypatch):
    counters, s = _run(
        monkeypatch,
        [
            _audience(hid="h-a", confirmation="à_confirmer"),
            _audience(hid="h-b", confirmation="refusée"),
            _audience(hid="h-c", status="annulée"),
        ],
        [],
    )
    assert not s.crees and counters["ignores"] == 3


def test_miroir_jamais_d_ecriture_firestore(monkeypatch):
    """Le miroir est en LECTURE SEULE sur Firestore, par conception : écrire
    l'id Outlook sur l'audience régénérerait etag/updated_at (churn DavX5) et
    s'emmêlerait avec les champs graph_* de l'import Bookings."""
    def _interdit(*a, **k):
        raise AssertionError("le miroir ne doit jamais écrire dans Firestore")

    monkeypatch.setattr(h, "create_hearing", _interdit)
    monkeypatch.setattr(h, "update_hearing", _interdit)
    monkeypatch.setattr(h, "delete_hearing", _interdit)
    aud = _audience()
    orphelin = _miroir_de(_audience(hid="h-parti"), event_id="EVT-X")
    # Du travail sur les trois phases : créer, corriger, supprimer.
    counters, s = _run(
        monkeypatch,
        [aud, _audience(hid="h-2", etag="e-9")],
        [_miroir_de(_audience(hid="h-2"), event_id="EVT-2",
                    etag_stampe="e-1"), orphelin],
    )
    assert counters["crees"] == 1 and counters["corriges"] == 1
    assert counters["supprimes"] == 1


# ── Fenêtre pleine : les suppressions sont désarmées ─────────────────────


def test_fenetre_pleine_bloque_les_suppressions(monkeypatch):
    """Un desired TRONQUÉ ne décrit plus tout ce qui existe : piloter les
    suppressions avec lui effacerait en masse des miroirs valides. Créations
    et corrections restent sûres (le jeu tronqué dit vrai sur son contenu)."""
    evenements = []
    monkeypatch.setattr(
        to, "log_bookings_event",
        lambda event, outcome="success", **k: evenements.append(
            (event, outcome, k)
        ),
    )
    monkeypatch.setattr(to, "_LIMITE_FENETRE", 2)
    audiences = [_audience(hid="h-1"), _audience(hid="h-2")]  # fenêtre pleine
    orphelin = _miroir_de(_audience(hid="h-coupe"), event_id="EVT-ORPHELIN")
    counters, s = _run(monkeypatch, audiences, [orphelin])
    assert s.supprimes == []                    # jamais sur fenêtre tronquée
    assert counters["crees"] == 2               # les créations restent sûres
    assert ("miroir_outlook_erreur_graph", "failure") == evenements[0][:2]
    assert evenements[0][2]["reason"] == "fenetre_pleine"


def test_erreur_graph_par_evenement_continue(monkeypatch):
    """Une panne sur UN événement est comptée ; le reste du balayage passe."""
    counters, s = _run(
        monkeypatch,
        [_audience(hid="h-1"), _audience(hid="h-2")],
        [],
        echoue_sur={"h-1"},
    )
    assert counters["erreurs"] == 1 and counters["crees"] == 1
    assert s.crees == ["h-2"]


# ── Garde cron + interrupteurs ────────────────────────────────────────────


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(to.taches_outlook_bp)
    return app.test_client()


def test_sync_forbidden_without_cron_header(client):
    assert client.get("/taches/outlook/sync").status_code == 403


def test_kill_switch_inactif(client, monkeypatch):
    """Le kill switch gèle les miroirs EN PLACE — Graph n'est pas touché."""
    monkeypatch.setattr(Config, "MIROIR_OUTLOOK_ACTIF", False)
    appele = []
    monkeypatch.setattr(to.graph_miroir, "lister_miroirs",
                        lambda *a, **k: appele.append(1) or [])
    r = client.get("/taches/outlook/sync", headers={"X-Appengine-Cron": "true"})
    assert r.status_code == 200 and r.get_json() == {"actif": False}
    assert appele == []


def test_non_configure_rend_200(client, monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    r = client.get("/taches/outlook/sync", headers={"X-Appengine-Cron": "true"})
    assert r.status_code == 200 and r.get_json()["configure"] is False


def test_erreur_graph_rend_200(client, monkeypatch):
    """Une panne Graph est transitoire : 200 (le prochain cycle réessaie) —
    un 500 déclencherait une tempête de reprises cron."""
    for k, v in [("GRAPH_TENANT_ID", "t"), ("GRAPH_CLIENT_ID", "c"),
                 ("GRAPH_CLIENT_SECRET", "s"), ("GRAPH_SENDER_UPN", "r@x")]:
        monkeypatch.setattr(Config, k, v)
    monkeypatch.setattr(
        h, "list_hearings_in_range",
        lambda debut, fin, limit, include_unconfirmed: [],
    )

    def _panne(*a, **k):
        raise GraphError("Échec d'un GET Graph (HTTP 503).")

    monkeypatch.setattr(to.graph_miroir, "lister_miroirs", _panne)
    r = client.get("/taches/outlook/sync", headers={"X-Appengine-Cron": "true"})
    assert r.status_code == 200 and r.get_json()["erreur"] == "graph"


def test_sync_runs_and_returns_counters(client, monkeypatch):
    for k, v in [("GRAPH_TENANT_ID", "t"), ("GRAPH_CLIENT_ID", "c"),
                 ("GRAPH_CLIENT_SECRET", "s"), ("GRAPH_SENDER_UPN", "r@x")]:
        monkeypatch.setattr(Config, k, v)
    monkeypatch.setattr(
        h, "list_hearings_in_range",
        lambda debut, fin, limit, include_unconfirmed: [_audience()],
    )
    monkeypatch.setattr(to.graph_miroir, "lister_miroirs", lambda debut, fin: [])
    monkeypatch.setattr(to.graph_miroir, "creer_miroir", lambda aud: None)
    r = client.get("/taches/outlook/sync", headers={"X-Appengine-Cron": "true"})
    body = r.get_json()
    assert r.status_code == 200 and body["actif"] is True and body["crees"] == 1


def test_la_fenetre_est_partagee(monkeypatch):
    """L'invariant anti-orphelin : la MÊME fenêtre des deux côtés — un miroir
    ne peut sortir de la fenêtre que par le bord passé."""
    fenetres = {}

    def _athena(debut, fin, limit, include_unconfirmed):
        fenetres["athena"] = (debut, fin)
        assert include_unconfirmed is True  # signal de troncature honnête
        return []

    def _outlook(debut, fin):
        fenetres["outlook"] = (debut, fin)
        return []

    monkeypatch.setattr(h, "list_hearings_in_range", _athena)
    monkeypatch.setattr(to.graph_miroir, "lister_miroirs", _outlook)
    to._synchroniser()
    assert fenetres["athena"] == fenetres["outlook"]
