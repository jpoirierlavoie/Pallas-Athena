"""Graph calendar glue for the Bookings sync (spec L2 §4.3-4.4).

Pins the deterministic prefix predicate, the UTC parse (Graph's 7-digit
fractional seconds + default-UTC), the client-attendee extraction, and the
cancel call shape (Calendars.ReadWrite).
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from utils import graph_calendrier as gc  # noqa: E402

UTC = timezone.utc
UPN = "juriste@example.com"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", UPN)
    monkeypatch.setattr(Config, "BOOKINGS_SUBJECT_PREFIXES", ("RDV",))


def _ev(**over):
    base = {
        "id": "EVT1", "iCalUId": "ical-1",
        "subject": "RDV — Consultation initiale",
        "start": {"dateTime": "2026-09-01T13:30:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-01T14:30:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Bureau"},
        "isOnlineMeeting": True,
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/x?a=1,2"},
        "attendees": [
            {"emailAddress": {"address": "CLIENT@ex.com", "name": "Client X"}},
            {"emailAddress": {"address": UPN, "name": "Juriste"}},
        ],
        "organizer": {"emailAddress": {"address": UPN}},
        "isCancelled": False,
        "lastModifiedDateTime": "2026-07-25T10:00:00Z",
    }
    base.update(over)
    return base


# ── est_reservation ───────────────────────────────────────────────────────

def test_predicate_matches_organizer_and_prefix():
    assert gc.est_reservation(_ev()) is True


def test_predicate_rejects_other_organizer():
    assert gc.est_reservation(
        _ev(organizer={"emailAddress": {"address": "someone@else.com"}})
    ) is False


def test_predicate_rejects_wrong_subject_prefix():
    assert gc.est_reservation(_ev(subject="Réunion interne")) is False


def test_predicate_false_when_upn_unset(monkeypatch):
    monkeypatch.setattr(Config, "BOOKINGS_JURISTE_UPN", "")
    assert gc.est_reservation(_ev()) is False


# ── extraire ──────────────────────────────────────────────────────────────

def test_extraire_parses_utc_and_trims_fractional():
    r = gc.extraire(_ev())
    assert r["start_datetime"] == datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    assert r["end_datetime"] == datetime(2026, 9, 1, 14, 30, tzinfo=UTC)


def test_extraire_maps_teams_link_onto_native_fields():
    r = gc.extraire(_ev())
    assert r["modalite"] == "visioconférence"
    assert r["conference_uri"] == "https://teams.microsoft.com/l/x?a=1,2"


def test_extraire_presentiel_when_not_online():
    r = gc.extraire(_ev(isOnlineMeeting=False, onlineMeeting={}))
    assert r["modalite"] == "présentiel"
    assert r["conference_uri"] == ""


def test_extraire_picks_the_client_attendee_lowercased():
    r = gc.extraire(_ev())
    assert r["client_email"] == "client@ex.com"
    assert r["client_nom"] == "Client X"


def test_extraire_carries_uid_and_cancel_flag():
    r = gc.extraire(_ev(isCancelled=True))
    assert r["graph_ical_uid"] == "ical-1"
    assert r["graph_event_id"] == "EVT1"
    assert r["is_cancelled"] is True


def test_parse_graph_dt_handles_trailing_z_no_fraction():
    dt = gc._parse_graph_dt({"dateTime": "2026-09-01T13:30:00Z", "timeZone": "UTC"})
    assert dt == datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def test_parse_graph_dt_none():
    assert gc._parse_graph_dt(None) is None
    assert gc._parse_graph_dt({}) is None


# ── lister_reservations / annuler_reservation ─────────────────────────────

def test_lister_reservations_calls_calendarview():
    with mock.patch.object(gc.graph, "graph_get",
                           return_value={"value": [_ev(), _ev(id="EVT2")]}) as g:
        rows = gc.lister_reservations(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        )
    assert len(rows) == 2
    path = g.call_args.args[0]
    params = g.call_args.kwargs["params"]
    assert path == f"/users/{UPN}/calendarView"
    assert "$select" in params and "$top" in params
    assert params["startDateTime"].startswith("2026-08-01")


def test_annuler_reservation_posts_cancel():
    with mock.patch.object(gc.graph, "graph_post", return_value=None) as p:
        gc.annuler_reservation("EVT1", "Refusé par le juriste")
    path = p.call_args.args[0]
    body = p.call_args.args[1]
    assert path == f"/users/{UPN}/events/EVT1/cancel"
    assert body["Comment"] == "Refusé par le juriste"
