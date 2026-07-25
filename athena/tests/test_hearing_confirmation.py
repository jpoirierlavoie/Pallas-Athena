"""Bookings confirmation gate (phase L2) — the include_unconfirmed contract.

Mirrors tests/test_analyse_note.py's include_analyse pins. The silent-failure
zones here:
- a Bookings import (confirmation="à_confirmer") must NOT leak into DAV, MCP or
  the dashboard — the list functions exclude it by DEFAULT;
- a legacy hearing (no `confirmation` field) must stay visible EVERYWHERE
  (never rétro-rempli);
- "refusée" is dropped in BOTH modes (deleted-equivalent);
- the sync job's own lookup needs include_unconfirmed=True, so True must
  actually surface à_confirmer + annulée_client.
"""

import operator
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

from google.cloud import firestore  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import models.hearing as h
    import dav.dossier_collections as dc
    import mcp.handlers as handlers

UTC = timezone.utc
DT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
_OPS = {"==": operator.eq, ">=": operator.ge, "<=": operator.le,
        "<": operator.lt, ">": operator.gt}


# ── Fake Firestore (honours the range/order/limit the model uses) ─────────

class _Doc:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return dict(self._d)


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def where(self, filter=None):
        op = _OPS[filter.op_string]
        return _Query([
            r for r in self._rows
            if r.get(filter.field_path) is not None
            and op(r.get(filter.field_path), filter.value)
        ])

    def order_by(self, field, direction=None):
        desc = direction == firestore.Query.DESCENDING
        floor = datetime.min.replace(tzinfo=UTC)
        return _Query(
            sorted(self._rows, key=lambda r: r.get(field) or floor, reverse=desc)
        )

    def limit(self, n):
        return _Query(self._rows[:n])

    def stream(self):
        return [_Doc(r) for r in self._rows]


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def collection(self, name):
        return _Query(list(self._rows))


def _hearing(hid, confirmation="", start=None, **over):
    base = {
        "id": hid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T. c. L.", "title": f"H {hid}",
        "hearing_type": "audience",
        "start_datetime": start or datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
        "end_datetime": (start or datetime(2026, 9, 1, 13, 0, tzinfo=UTC))
        + timedelta(hours=1),
        "all_day": False, "status": "confirmée", "reminder_minutes": 1440,
        "modalite": "présentiel", "conference_uri": "",
        "confirmation": confirmation,
        "source": "bookings" if confirmation else "",
        "vevent_uid": f"uid-{hid}", "etag": f"e-{hid}",
        "created_at": DT, "updated_at": DT,
    }
    base.update(over)
    return base


def _legacy(hid, start=None):
    """A pre-L2 hearing: no `confirmation`/`source` keys at all."""
    return {
        "id": hid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T. c. L.", "title": f"Legacy {hid}",
        "hearing_type": "audience",
        "start_datetime": start or datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
        "end_datetime": (start or datetime(2026, 9, 2, 13, 0, tzinfo=UTC))
        + timedelta(hours=1),
        "all_day": False, "status": "confirmée", "reminder_minutes": 1440,
        "vevent_uid": f"uid-{hid}", "etag": f"e-{hid}",
        "created_at": DT, "updated_at": DT,
    }


# ── _filter_confirmation (pure) ───────────────────────────────────────────

def test_filter_default_keeps_only_confirmed():
    rows = [
        {"id": "ok", "confirmation": ""},
        {"id": "legacy"},  # no key
        {"id": "pending", "confirmation": "à_confirmer"},
        {"id": "cxl", "confirmation": "annulée_client"},
        {"id": "no", "confirmation": "refusée"},
    ]
    ids = [r["id"] for r in h._filter_confirmation(rows, include_unconfirmed=False)]
    assert ids == ["ok", "legacy"]


def test_filter_true_keeps_pending_and_annule_but_never_refused():
    rows = [
        {"id": "ok", "confirmation": ""},
        {"id": "pending", "confirmation": "à_confirmer"},
        {"id": "cxl", "confirmation": "annulée_client"},
        {"id": "no", "confirmation": "refusée"},
    ]
    ids = {r["id"] for r in h._filter_confirmation(rows, include_unconfirmed=True)}
    assert ids == {"ok", "pending", "cxl"}


# ── list_hearings ─────────────────────────────────────────────────────────

@pytest.fixture()
def mixed_db(monkeypatch):
    monkeypatch.setattr(h, "db", _DB([
        _hearing("h-ok"),
        _legacy("h-legacy"),
        _hearing("h-pending", "à_confirmer"),
        _hearing("h-cxl", "annulée_client"),
        _hearing("h-refuse", "refusée"),
    ]))


def test_list_hearings_default_excludes_unconfirmed(mixed_db):
    ids = {r["id"] for r in h.list_hearings(dossier_id="d1")}
    assert ids == {"h-ok", "h-legacy"}


def test_list_hearings_include_true_surfaces_pending_and_cancelled(mixed_db):
    ids = {r["id"] for r in h.list_hearings(dossier_id="d1",
                                            include_unconfirmed=True)}
    assert ids == {"h-ok", "h-legacy", "h-pending", "h-cxl"}


def test_legacy_hearing_gets_confirmation_default_and_stays_visible(mixed_db):
    """A doc with no `confirmation` key reads as confirmed (setdefault "")."""
    rows = {r["id"]: r for r in h.list_hearings(dossier_id="d1",
                                                include_unconfirmed=True)}
    assert rows["h-legacy"]["confirmation"] == ""
    assert rows["h-legacy"]["source"] == ""


def test_list_bookings_all_includes_refusee(monkeypatch):
    """The sync-only lookup sees EVERY booking, incl. refusée — unlike
    list_hearings(include_unconfirmed=True), which drops refusée. Prevents the
    resurrection of a refused reservation whose Outlook cancel failed."""
    monkeypatch.setattr(h, "db", _DB([
        _hearing("b-ok", source="bookings"),     # confirmed booking
        _hearing("b-refuse", "refusée"),
        _hearing("b-pending", "à_confirmer"),
        _legacy("not-a-booking"),                # no source → excluded
    ]))
    ids = {r["id"] for r in h.list_bookings_all()}
    assert ids == {"b-ok", "b-refuse", "b-pending"}
    # And refusée is STILL hidden from the normal include_unconfirmed=True path.
    assert "b-refuse" not in {
        r["id"] for r in h.list_hearings(include_unconfirmed=True)
    }


# ── list_hearings_in_range / _window ──────────────────────────────────────

def test_list_hearings_in_range_honours_the_contract(monkeypatch):
    monkeypatch.setattr(h, "db", _DB([
        _hearing("r-ok"), _hearing("r-pending", "à_confirmer"),
    ]))
    lo = datetime(2026, 8, 1, tzinfo=UTC)
    hi = datetime(2026, 10, 1, tzinfo=UTC)
    assert {r["id"] for r in h.list_hearings_in_range(lo, hi)} == {"r-ok"}
    assert {r["id"] for r in h.list_hearings_in_range(
        lo, hi, include_unconfirmed=True)} == {"r-ok", "r-pending"}


def test_list_hearings_window_honours_the_contract(monkeypatch):
    monkeypatch.setattr(h, "db", _DB([
        _hearing("w-ok"), _hearing("w-pending", "à_confirmer"),
    ]))
    pivot = datetime(2026, 8, 1, tzinfo=UTC)
    assert {r["id"] for r in h.list_hearings_window(pivot)} == {"w-ok"}
    assert {r["id"] for r in h.list_hearings_window(
        pivot, include_unconfirmed=True)} == {"w-ok", "w-pending"}


# ── DAV: _collection_members must exclude unconfirmed ─────────────────────

def test_dav_collection_members_excludes_unconfirmed(monkeypatch):
    """dav/dossier_collections._collection_members calls list_hearings on the
    DEFAULT, so a pending Bookings import never lists in DavX5."""
    monkeypatch.setattr(h, "db", _DB([
        _hearing("d-ok"), _hearing("d-pending", "à_confirmer"),
    ]))
    monkeypatch.setattr(dc, "list_tasks", lambda **k: [])
    monkeypatch.setattr(dc, "list_notes", lambda **k: [])
    hearings, _tasks, _notes = dc._collection_members("d1")
    assert {x["id"] for x in hearings} == {"d-ok"}


# ── MCP: the agenda tools must exclude unconfirmed ────────────────────────

def test_mcp_list_hearings_excludes_unconfirmed(monkeypatch):
    """MCP list_hearings reads list_hearings_in_range on the default — a
    pending reservation must not reach Claude (decision D-L2-3)."""
    soon = datetime.now(UTC) + timedelta(days=3)
    monkeypatch.setattr(h, "db", _DB([
        _hearing("m-ok", start=soon),
        _hearing("m-pending", "à_confirmer", start=soon),
    ]))
    payload = handlers.list_hearings({})
    ids = {row["id"] for row in payload["items"]}
    assert "m-ok" in ids
    assert "m-pending" not in ids
    # The whitelist row must not leak any booking field to Claude.
    assert all("confirmation" not in row for row in payload["items"])
