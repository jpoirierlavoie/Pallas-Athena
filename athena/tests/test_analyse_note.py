"""Théorie de la cause (feuille « Analyse ») — model invariants.

Four silent-failure zones pinned here:
- the dateless VJOURNAL (no DTSTART, but CREATED/DTSTAMP kept — the jtx
  icalobject.created NOT-NULL trap);
- the include_analyse contract (Notes views exclude by default; a DAV/MCP
  caller left on the default silently drops the note);
- create_analyse_note idempotence (a double-clicked init button must never
  mint a second note);
- update_note's partial merge (the standard note form must not strip
  dateless/is_analyse/created_at when the analyse note is edited).
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

with mock.patch("google.cloud.firestore.Client"):
    import models.note as note
    import models.dossier as dossier_model

UTC = timezone.utc
DT = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


def _analyse(**over) -> dict:
    base = {
        "id": "n-analyse", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "T. c. L.",
        "title": note.ANALYSE_TITLE, "content": note._ANALYSE_SEED,
        "category": "stratégie", "pinned": False,
        "dateless": True, "is_analyse": True,
        "vjournal_uid": "uid-analyse", "created_at": DT, "updated_at": DT,
    }
    base.update(over)
    return base


def _ordinary(**over) -> dict:
    base = {
        "id": "n1", "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T. c. L.", "title": "Recherche", "content": "Corps",
        "category": "recherche", "pinned": False,
        "vjournal_uid": "uid-n1", "created_at": DT, "updated_at": DT,
    }
    base.update(over)
    return base


# ── Serialization: the dateless VJOURNAL ─────────────────────────────────


def test_vjournal_omits_dtstart_when_dateless_but_keeps_the_stamps():
    ics = note.note_to_vjournal(_analyse())
    assert "DTSTART" not in ics
    # The jtx icalobject.created NOT-NULL trap: both stamps stay.
    assert "CREATED" in ics and "DTSTAMP" in ics and "UID:uid-analyse" in ics
    assert "X-PALLAS-ANALYSE:true" in ics
    assert "SUMMARY:Théorie de la cause" in ics


def test_ordinary_note_still_carries_dtstart_and_no_analyse_flag():
    ics = note.note_to_vjournal(_ordinary())
    assert "DTSTART" in ics
    assert "X-PALLAS-ANALYSE" not in ics


def test_round_trip_restores_dateless_and_is_analyse():
    data = note.vjournal_to_note(note.note_to_vjournal(_analyse()))
    assert data["dateless"] is True
    assert data["is_analyse"] is True


def test_parser_never_demotes_is_analyse_when_the_xprop_is_stripped():
    """A client that drops unknown X- properties must not flip the stored
    flag: the parser leaves the key ABSENT so update_note's merge keeps the
    existing value."""
    data = note.vjournal_to_note(note.note_to_vjournal(_ordinary()))
    assert "is_analyse" not in data
    assert data["dateless"] is False  # DTSTART present → dated again


# ── Seed content ─────────────────────────────────────────────────────────


def test_seed_has_the_eight_blocks_and_fits_the_cap():
    for letter in "ABCDEFGH":
        assert f"## Bloc {letter} — " in note._ANALYSE_SEED
    assert len(note._ANALYSE_SEED) < note.CONTENT_MAX_LENGTH
    # sanitize() must pass the seed through untouched (no <...> pair, no
    # over-length) — otherwise the stored note differs from the template.
    from security import sanitize
    assert sanitize(note._ANALYSE_SEED,
                    max_length=note.CONTENT_MAX_LENGTH) == note._ANALYSE_SEED


# ── create_analyse_note ──────────────────────────────────────────────────
# The fakes live further down (the include_analyse section); the existence
# check inside create_analyse_note queries db DIRECTLY (fail closed), so
# these tests drive it through the fake db, not through get_analyse_note.


def test_create_analyse_note_is_idempotent(monkeypatch):
    monkeypatch.setattr(note, "db", _DB([_ordinary(), _analyse()]))

    def _must_not_run(_data):
        raise AssertionError("created a second analyse note")

    monkeypatch.setattr(note, "create_note", _must_not_run)
    result, errors = note.create_analyse_note("d1")
    assert errors == [] and result["id"] == "n-analyse"


def test_create_analyse_note_payload(monkeypatch):
    monkeypatch.setattr(note, "db", _DB([_ordinary()]))  # no analyse yet
    monkeypatch.setattr(
        dossier_model, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001", "title": "T. c. L."},
    )
    captured = {}

    def _create(data):
        captured.update(data)
        return dict(data, id="n-new"), []

    monkeypatch.setattr(note, "create_note", _create)
    result, errors = note.create_analyse_note("d1")
    assert errors == [] and result["id"] == "n-new"
    assert captured["title"] == note.ANALYSE_TITLE
    assert captured["content"] == note._ANALYSE_SEED
    assert captured["category"] == "stratégie"
    assert captured["dateless"] is True and captured["is_analyse"] is True
    assert captured["dossier_file_number"] == "2026-001"
    assert captured["dossier_title"] == "T. c. L."


def test_create_analyse_note_refuses_an_unknown_dossier(monkeypatch):
    monkeypatch.setattr(note, "db", _DB([]))
    monkeypatch.setattr(dossier_model, "get_dossier", lambda i: None)

    def _must_not_run(_data):
        raise AssertionError("wrote a note for an unknown dossier")

    monkeypatch.setattr(note, "create_note", _must_not_run)
    result, errors = note.create_analyse_note("nope")
    assert result is None and errors


def test_create_analyse_note_fails_closed_on_read_error(monkeypatch):
    """A transient read failure must NEVER read as « no analyse note yet »:
    get_analyse_note (via list_notes) swallows errors into [], so a
    fail-open existence check would seed a duplicate over the lawyer's
    filled analysis. The write path errors out instead."""

    class _BrokenDB:
        def collection(self, name):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(note, "db", _BrokenDB())

    def _must_not_run(_data):
        raise AssertionError("seeded a duplicate despite a read failure")

    monkeypatch.setattr(note, "create_note", _must_not_run)
    result, errors = note.create_analyse_note("d1")
    assert result is None and errors


# ── The include_analyse contract ─────────────────────────────────────────


class _Doc:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return dict(self._d)


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def where(self, filter):
        return _Query([
            r for r in self._rows
            if r.get(filter.field_path) == filter.value
        ])

    def order_by(self, field, direction=None):
        floor = datetime.min.replace(tzinfo=UTC)
        return _Query(sorted(
            self._rows, key=lambda r: r.get(field) or floor, reverse=True,
        ))

    def limit(self, n):
        return _Query(self._rows[:n])

    def stream(self):
        return [_Doc(r) for r in self._rows]


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def collection(self, name):
        return _Query(self._rows)


@pytest.fixture()
def two_notes_db(monkeypatch):
    monkeypatch.setattr(note, "db", _DB([_ordinary(), _analyse()]))


def test_list_notes_excludes_the_analyse_note_by_default(two_notes_db):
    ids = [n["id"] for n in note.list_notes(dossier_id="d1")]
    assert ids == ["n1"]


def test_list_notes_includes_it_on_request(two_notes_db):
    ids = {n["id"] for n in note.list_notes(dossier_id="d1",
                                            include_analyse=True)}
    assert ids == {"n1", "n-analyse"}


def test_list_notes_recent_honours_the_same_contract(two_notes_db):
    ids = [n["id"] for n in note.list_notes_recent(dossier_id="d1")]
    assert ids == ["n1"]
    ids = {n["id"] for n in note.list_notes_recent(dossier_id="d1",
                                                   include_analyse=True)}
    assert ids == {"n1", "n-analyse"}


def test_get_analyse_note_targets_the_flagged_note(two_notes_db):
    found = note.get_analyse_note("d1")
    assert found and found["id"] == "n-analyse"
    assert note.has_analyse("d1") is True


def test_get_notes_summary_counts_the_analyse_note(two_notes_db):
    """Its only caller is the MCP get_dossier, whose read paths expose the
    note — the count must agree with what the MCP list_notes returns."""
    assert note.get_notes_summary("d1") == {"total": 2}


# ── Edit-form merge safety ───────────────────────────────────────────────


def test_update_note_preserves_the_flags_and_created_at(monkeypatch):
    """The standard note form submits only 5 fields; update_note's partial
    merge must carry dateless/is_analyse/created_at through unchanged —
    otherwise one edit re-dates the note in jtx and unhides it."""
    stored = _analyse()
    monkeypatch.setattr(note, "get_note", lambda i: dict(stored))

    class _SetDB:
        written = None

        def collection(self, name):
            return self

        def document(self, doc_id):
            return self

        def set(self, data):
            _SetDB.written = data

    monkeypatch.setattr(note, "db", _SetDB())
    updated, errors = note.update_note("n-analyse", {
        "dossier_id": "d1", "title": note.ANALYSE_TITLE,
        "content": "## Bloc A — rempli", "category": "stratégie",
        "pinned": False,
    })
    assert errors == []
    assert _SetDB.written["dateless"] is True
    assert _SetDB.written["is_analyse"] is True
    assert _SetDB.written["created_at"] == DT
