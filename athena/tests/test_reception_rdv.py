"""Réception « Rendez-vous » tab + actions (Bookings L2 §5).

Pins the confirm/refuse/divergence routes, the refuse-cancels-Outlook path
(Calendars.ReadWrite, best-effort), the partie exact-email linkage, and the
badge count. get_hearing does NOT filter on confirmation, so these routes can
reach an à_confirmer import.
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
    import routes.reception as reception

from config import Config  # noqa: E402
from utils.graph import GraphError  # noqa: E402

UTC = timezone.utc


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(reception.reception_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


def _spy_update(monkeypatch):
    calls = []
    monkeypatch.setattr(
        reception, "update_hearing",
        lambda i, d: (calls.append((i, d)) or ({"id": i, **d}, [])),
    )
    return calls


def _spy_bump(monkeypatch):
    bumps = []
    monkeypatch.setattr(reception, "bump_ctag", lambda name: bumps.append(name))
    return bumps


def _spy_tombstones(monkeypatch):
    records, removes = [], []
    monkeypatch.setattr(reception, "record_tombstone",
                        lambda name, rid: records.append((name, rid)))
    monkeypatch.setattr(reception, "remove_tombstone",
                        lambda name, rid: removes.append((name, rid)))
    return records, removes


def _booking(**over):
    base = {"id": "h1", "source": "bookings", "confirmation": "à_confirmer",
            "dossier_id": "", "graph_event_id": "EVT1"}
    base.update(over)
    return base


# ── confirmer ─────────────────────────────────────────────────────────────

def test_confirmer_clears_confirmation_and_bumps(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _booking())
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    _records, removes = _spy_tombstones(monkeypatch)
    r = client.post("/reception/rdv/h1/confirmer")
    assert r.status_code == 302 and "onglet=rdv" in r.headers["Location"]
    assert updates[0][1]["confirmation"] == ""
    assert "partie_id" not in updates[0][1]
    assert bumps == ["general"]  # collection_for("") → général
    assert removes == [("general", "h1")]  # stale-tombstone clear on (re)entry


def test_confirmer_links_partie_when_checked(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _booking())
    monkeypatch.setattr(reception, "get_partie", lambda i: {"id": "p1"})
    updates = _spy_update(monkeypatch)
    _spy_bump(monkeypatch)
    _spy_tombstones(monkeypatch)
    client.post("/reception/rdv/h1/confirmer",
                data={"lier": "on", "partie_id": "p1"})
    assert updates[0][1]["partie_id"] == "p1"


def test_confirmer_refuses_non_booking(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: {"id": "h1", "source": ""})
    updates = _spy_update(monkeypatch)
    r = client.post("/reception/rdv/h1/confirmer")
    assert "erreur=" in r.headers["Location"]
    assert not updates


# ── refuser (Calendars.ReadWrite) ─────────────────────────────────────────

def test_refuser_cancels_outlook_and_marks_refusee(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _booking())
    monkeypatch.setattr(reception.Config, "bookings_configured", lambda: True)
    cancels = []
    monkeypatch.setattr(reception.graph_calendrier, "annuler_reservation",
                        lambda gid, motif="": cancels.append(gid))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    r = client.post("/reception/rdv/h1/refuser")
    assert cancels == ["EVT1"]
    assert updates[0][1] == {"confirmation": "refusée"}
    assert not bumps  # never was in DAV
    assert "message=" in r.headers["Location"]


def test_refuser_graph_failure_still_refuses_with_warning(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _booking())
    monkeypatch.setattr(reception.Config, "bookings_configured", lambda: True)

    def _boom(gid, motif=""):
        raise GraphError("boom")

    monkeypatch.setattr(reception.graph_calendrier, "annuler_reservation", _boom)
    updates = _spy_update(monkeypatch)
    r = client.post("/reception/rdv/h1/refuser")
    assert updates[0][1] == {"confirmation": "refusée"}
    assert "erreur=" in r.headers["Location"]  # manual-cancel warning


def test_refuser_annule_client_does_not_recall_graph(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _booking(confirmation="annulée_client"))
    monkeypatch.setattr(reception.Config, "bookings_configured", lambda: True)
    called = []
    monkeypatch.setattr(reception.graph_calendrier, "annuler_reservation",
                        lambda gid, motif="": called.append(gid))
    updates = _spy_update(monkeypatch)
    client.post("/reception/rdv/h1/refuser")
    assert not called  # already cancelled client-side
    assert updates[0][1] == {"confirmation": "refusée"}


# ── divergence ────────────────────────────────────────────────────────────

def test_divergence_appliquer_updates_slot_and_bumps(client, monkeypatch):
    nd = datetime(2026, 9, 5, 14, 0, tzinfo=UTC).isoformat()
    nf = datetime(2026, 9, 5, 15, 0, tzinfo=UTC).isoformat()
    div = {"motif": "modifié_côté_client", "nouveau_debut": nd,
           "nouveau_fin": nf, "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _booking(confirmation="", bookings_divergence=div))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    client.post("/reception/rdv/h1/divergence/appliquer")
    data = updates[0][1]
    assert data["bookings_divergence"] is None
    assert data["start_datetime"] == datetime(2026, 9, 5, 14, 0, tzinfo=UTC)
    assert bumps == ["general"]


def test_divergence_ignorer_marks_vu_without_bump(client, monkeypatch):
    div = {"motif": "modifié_côté_client", "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _booking(confirmation="", bookings_divergence=div))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    client.post("/reception/rdv/h1/divergence/ignorer")
    assert updates[0][1]["bookings_divergence"]["vu"] is True
    assert not bumps


def test_divergence_annuler_tombstones_and_bumps(client, monkeypatch):
    """A confirmed (synced) event leaving the DAV live set MUST record a
    tombstone — a CTag bump alone leaves the cancelled meeting on DavX5."""
    div = {"motif": "annulé_côté_client", "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _booking(confirmation="", bookings_divergence=div))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    records, _removes = _spy_tombstones(monkeypatch)
    client.post("/reception/rdv/h1/divergence/annuler")
    assert updates[0][1]["confirmation"] == "annulée_client"
    assert records == [("general", "h1")]
    assert bumps == ["general"]


def test_divergence_unknown_action_rejected(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _booking())
    updates = _spy_update(monkeypatch)
    r = client.post("/reception/rdv/h1/divergence/zzz")
    assert "erreur=" in r.headers["Location"] and not updates


# ── context + linkage + badge ─────────────────────────────────────────────

def test_contexte_rdv_selects_and_links(monkeypatch):
    hearings = [
        {"id": "a", "source": "bookings", "confirmation": "à_confirmer",
         "client_email": "Client@Ex.com", "start_datetime": None},
        {"id": "b", "source": "bookings", "confirmation": "annulée_client",
         "client_email": "nobody@x.com", "start_datetime": None},
        {"id": "c", "source": "bookings", "confirmation": "",
         "bookings_divergence": {"motif": "modifié_côté_client", "vu": False},
         "client_email": "", "start_datetime": None},
        {"id": "d", "source": "", "confirmation": ""},  # not a booking
    ]
    monkeypatch.setattr(reception, "list_hearings", lambda **k: hearings)
    monkeypatch.setattr(reception, "list_parties",
                        lambda: [{"id": "p1", "email": "client@ex.com",
                                  "first_name": "A", "last_name": "B"}])
    monkeypatch.setattr(reception, "display_name", lambda p: "A B")
    ctx = reception._contexte_rdv()
    assert {h["id"] for h in ctx["rdvs"]} == {"a", "b"}
    assert {h["id"] for h in ctx["divergences"]} == {"c"}
    a = next(h for h in ctx["rdvs"] if h["id"] == "a")
    assert a["_partie_id"] == "p1" and a["_partie_nom"] == "A B"
    b = next(h for h in ctx["rdvs"] if h["id"] == "b")
    assert b["_partie_id"] == ""  # no exact match


def test_compteur_sums_soumises_and_rdv(monkeypatch):
    monkeypatch.setattr(reception.pi, "compter_soumises", lambda: 2)
    monkeypatch.setattr(reception, "list_hearings", lambda **k: [
        {"confirmation": "à_confirmer"}, {"confirmation": "à_confirmer"},
        {"confirmation": ""},
    ])
    reception._badge_cache["at"] = 0.0  # force refresh
    assert reception.compteur_reception() == 4


def test_compteur_fail_open_when_both_unavailable(monkeypatch):
    monkeypatch.setattr(reception.pi, "compter_soumises", lambda: None)
    monkeypatch.setattr(reception, "list_hearings",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("down")))
    reception._badge_cache["at"] = 0.0
    assert reception.compteur_reception() is None
