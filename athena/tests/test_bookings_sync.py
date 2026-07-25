"""Bookings reconciliation upsert + cron endpoint (spec L2 §4.2-4.5).

Pins the silent-failure zones:
- a NEW reservation creates an à_confirmer hearing with NO CTag bump;
- the sync's own lookup uses include_unconfirmed=True, so a second run finds
  its own imports and writes NOTHING (idempotence §7.6 — the duplicate trap);
- a CONFIRMED booking is never overwritten (modified/cancelled → divergence);
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
    import routes.taches_bookings as tb
    import models.hearing as h

from config import Config  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"
NOW = datetime.now(UTC)
OLD = "2026-07-25T10:00:00Z"
NEW = "2026-07-25T18:00:00Z"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_PREFIXES", ("RDV",))
    monkeypatch.setattr(Config, "BOOKINGS_SYNC_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(Config, "BOOKINGS_SYNC_LOOKAHEAD_DAYS", 90)
    monkeypatch.setattr(Config, "BOOKINGS_DEBUG_PAYLOAD", False)


class _Spy:
    def __init__(self):
        self.creates = []
        self.updates = []

    def create(self, data):
        self.creates.append(data)
        return dict(data, id="h-new"), []

    def update(self, hid, data):
        self.updates.append((hid, data))
        return {"id": hid, **data}, []


@pytest.fixture()
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(h, "create_hearing", s.create)
    monkeypatch.setattr(h, "update_hearing", s.update)
    return s


def _ev(uid="ical-1", last_mod=OLD, cancelled=False, days=3, subject="RDV — X"):
    start = NOW + timedelta(days=days)
    end = start + timedelta(hours=1)
    return {
        "id": f"EVT-{uid}", "iCalUId": uid, "subject": subject,
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "location": {"displayName": "Bureau"},
        "isOnlineMeeting": False, "onlineMeeting": {},
        "attendees": [{"emailAddress": {"address": "client@ex.com", "name": "C"}}],
        "organizer": {"emailAddress": {"address": UPN}},
        "isCancelled": cancelled, "lastModifiedDateTime": last_mod,
    }


def _stored(uid="ical-1", confirmation="à_confirmer", last_mod=OLD, days=3,
            divergence=None):
    start = NOW + timedelta(days=days)
    return {
        "id": f"h-{uid}", "source": "bookings", "graph_ical_uid": uid,
        "graph_event_id": f"EVT-{uid}", "graph_last_modified": last_mod,
        "confirmation": confirmation, "title": "RDV — X",
        "start_datetime": start, "end_datetime": start + timedelta(hours=1),
        "bookings_divergence": divergence,
    }


def _run(monkeypatch, reservations, existing):
    monkeypatch.setattr(tb.graph_calendrier, "lister_reservations",
                        lambda debut, fin: reservations)
    # The reconciliation reads its prior imports via list_bookings_all (which
    # includes refusée), NOT list_hearings.
    monkeypatch.setattr(h, "list_bookings_all", lambda: existing)
    return tb._synchroniser()


# ── Create + idempotence (the duplicate trap) ─────────────────────────────

def test_new_reservation_creates_a_confirmer(monkeypatch, spy):
    counters = _run(monkeypatch, [_ev("ical-1")], [])
    assert counters["crees"] == 1 and counters["detectes"] == 1
    assert not spy.updates
    data = spy.creates[0]
    assert data["confirmation"] == "à_confirmer"
    assert data["source"] == "bookings"
    assert data["hearing_type"] == "consultation"
    assert data["status"] == "confirmée"


def test_second_run_is_idempotent(monkeypatch, spy):
    """The sync loads its own à_confirmer imports (include_unconfirmed=True):
    same lastModified → zero writes. Without True it would duplicate."""
    counters = _run(monkeypatch, [_ev("ical-1", last_mod=OLD)],
                    [_stored("ical-1", last_mod=OLD)])
    assert not spy.creates and not spy.updates
    assert counters == {"vus": 1, "detectes": 1, "crees": 0,
                        "modifies": 0, "annules": 0, "divergences": 0}


def test_sync_lookup_uses_list_bookings_all(monkeypatch, spy):
    """The lookup must go through list_bookings_all (which includes refusée),
    never list_hearings (which drops it) — otherwise a refused booking whose
    Outlook cancel failed is re-imported every cycle."""
    called = {"all": 0}
    monkeypatch.setattr(tb.graph_calendrier, "lister_reservations",
                        lambda debut, fin: [])
    monkeypatch.setattr(h, "list_bookings_all",
                        lambda: called.__setitem__("all", called["all"] + 1) or [])

    def _forbidden(**k):
        raise AssertionError("sync must not call list_hearings for its lookup")

    monkeypatch.setattr(h, "list_hearings", _forbidden)
    tb._synchroniser()
    assert called["all"] == 1


def test_refused_booking_not_resurrected(monkeypatch, spy):
    """A refused booking whose Outlook cancel failed (Graph still returns it,
    isCancelled=false) must NOT be re-imported as a fresh à_confirmer."""
    counters = _run(monkeypatch, [_ev("ical-1", last_mod=NEW)],
                    [_stored("ical-1", confirmation="refusée", last_mod=OLD)])
    assert not spy.creates and not spy.updates
    assert counters["crees"] == 0 and counters["detectes"] == 1


# ── Modified ──────────────────────────────────────────────────────────────

def test_modified_pending_updates_silently(monkeypatch, spy):
    counters = _run(monkeypatch, [_ev("ical-1", last_mod=NEW)],
                    [_stored("ical-1", confirmation="à_confirmer", last_mod=OLD)])
    assert counters["modifies"] == 1 and counters["divergences"] == 0
    hid, data = spy.updates[0]
    assert hid == "h-ical-1"
    assert data["graph_last_modified"] == NEW
    assert "bookings_divergence" not in data


def test_modified_confirmed_records_divergence_without_overwrite(monkeypatch, spy):
    # stored confirmed (confirmation="") at day 3; incoming at day 5 → slot moved
    ev = _ev("ical-1", last_mod=NEW, days=5)
    counters = _run(monkeypatch, [ev],
                    [_stored("ical-1", confirmation="", last_mod=OLD, days=3)])
    assert counters["divergences"] == 1 and counters["modifies"] == 0
    hid, data = spy.updates[0]
    div = data["bookings_divergence"]
    assert div["motif"] == "modifié_côté_client" and div["vu"] is False
    assert div["nouveau_debut"]
    # The confirmed event's start/end are NEVER overwritten here.
    assert "start_datetime" not in data and "end_datetime" not in data


# ── Cancelled ─────────────────────────────────────────────────────────────

def test_cancelled_flag_pending_becomes_annule_client(monkeypatch, spy):
    counters = _run(monkeypatch, [_ev("ical-1", cancelled=True)],
                    [_stored("ical-1", confirmation="à_confirmer")])
    assert counters["annules"] == 1
    _hid, data = spy.updates[0]
    assert data == {"confirmation": "annulée_client"}


def test_cancelled_confirmed_records_divergence(monkeypatch, spy):
    counters = _run(monkeypatch, [_ev("ical-1", cancelled=True)],
                    [_stored("ical-1", confirmation="")])
    assert counters["annules"] == 1
    _hid, data = spy.updates[0]
    assert data["bookings_divergence"]["motif"] == "annulé_côté_client"
    assert "confirmation" not in data


def test_absence_in_window_cancels(monkeypatch, spy):
    # No reservations returned, but a stored à_confirmer starts in the window.
    counters = _run(monkeypatch, [], [_stored("gone", confirmation="à_confirmer",
                                              days=3)])
    assert counters["annules"] == 1
    _hid, data = spy.updates[0]
    assert data == {"confirmation": "annulée_client"}


def test_absence_out_of_window_left_alone(monkeypatch, spy):
    # A stored booking far beyond the lookahead must NOT be concluded cancelled.
    _run(monkeypatch, [], [_stored("far", confirmation="à_confirmer", days=200)])
    assert not spy.updates


def test_already_flagged_confirmed_cancel_is_idempotent(monkeypatch, spy):
    div = {"motif": "annulé_côté_client", "detail": "x", "vu": False}
    _run(monkeypatch, [], [_stored("c", confirmation="", days=3, divergence=div)])
    assert not spy.updates


# ── Cron endpoint guards ──────────────────────────────────────────────────

@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(tb.taches_bookings_bp)
    return app.test_client()


def test_sync_forbidden_without_cron_header(client):
    assert client.get("/taches/bookings/sync").status_code == 403


def test_sync_short_circuits_when_inactive(client, monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_SYNC_ACTIVE", False)
    r = client.get("/taches/bookings/sync", headers={"X-Appengine-Cron": "true"})
    assert r.status_code == 200 and r.get_json() == {"actif": False}


def test_sync_short_circuits_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_SYNC_ACTIVE", True)
    # graph creds absent → bookings_configured() False
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    r = client.get("/taches/bookings/sync", headers={"X-Appengine-Cron": "true"})
    assert r.status_code == 200 and r.get_json()["configure"] is False


def test_sync_runs_and_returns_counters(client, monkeypatch, spy):
    monkeypatch.setattr(Config, "BOOKINGS_SYNC_ACTIVE", True)
    for k, v in [("GRAPH_TENANT_ID", "t"), ("GRAPH_CLIENT_ID", "c"),
                 ("GRAPH_CLIENT_SECRET", "s"), ("GRAPH_SENDER_UPN", "r@x")]:
        monkeypatch.setattr(Config, k, v)
    monkeypatch.setattr(tb.graph_calendrier, "lister_reservations",
                        lambda debut, fin: [_ev("ical-1")])
    monkeypatch.setattr(h, "list_bookings_all", lambda: [])
    r = client.get("/taches/bookings/sync", headers={"X-Appengine-Cron": "true"})
    body = r.get_json()
    assert r.status_code == 200 and body["actif"] is True and body["crees"] == 1


def test_debug_payload_never_logs_the_subject(monkeypatch, caplog):
    """PII: the predicate-tuning debug log must NOT contain the meeting
    subject (it embeds the client name); only booleans + domains."""
    import logging as _logging
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_PREFIXES", ("RDV",))
    ev = _ev("ical-1", subject="RDV — Consultation Marie Tremblay")
    with caplog.at_level(_logging.DEBUG, logger=tb.logger.name):
        tb._debug_payload([ev])
    text = caplog.text
    assert "Marie Tremblay" not in text and "Consultation" not in text
    assert "prefix_match" in text and "organizer_match" in text
