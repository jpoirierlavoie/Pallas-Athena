"""Two-tier hearing vocabulary + modalité + CalDAV CONFERENCE (2026-07-24).

Pins the spec's silent-failure zones: vocabulary parity, the migration table,
the DAV non-effacement rule (§4.3), non-escaping of the CONFERENCE URI (§10.3),
and the conference_uri scheme whitelist (§10.4 — the stored-XSS guard).
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.hearing as h


def _video(**over) -> dict:
    base = {
        "vevent_uid": "u1", "title": "T", "start_datetime": None,
        "end_datetime": None, "all_day": False, "hearing_type": "instruction",
        "status": "confirmée", "modalite": "visioconférence",
        "conference_uri": "https://ex.com/j?a=1,2;b=3",
    }
    base.update(over)
    return base


# ── §10.1 parity ─────────────────────────────────────────────────────────


def test_every_type_has_label_color_forum_and_suggestion():
    for t in h.VALID_HEARING_TYPES:
        assert t in h.HEARING_TYPE_LABELS, t
        assert t in h.HEARING_TYPE_COLORS, t
        assert t in h.HEARING_TITLE_SUGGESTIONS, t
        assert h.forum_of(t) in h.VALID_FORUMS, t


def test_no_orphan_label_or_color():
    for key in h.HEARING_TYPE_LABELS:
        assert key in h.VALID_HEARING_TYPES, key
    for key in h.HEARING_TYPE_COLORS:
        assert key in h.VALID_HEARING_TYPES, key


def test_forum_lists_are_disjoint_and_cover_the_domain():
    j, e = set(h.VALID_HEARING_TYPES_JUDICIAIRE), set(h.VALID_HEARING_TYPES_EXTRAJUDICIAIRE)
    assert j.isdisjoint(e)
    assert j | e == set(h.VALID_HEARING_TYPES)


def test_modalite_parity():
    for m in h.VALID_MODALITES:
        assert m in h.MODALITE_LABELS, m
    for key in h.MODALITE_LABELS:
        assert key in h.VALID_MODALITES, key


# ── §10.2 migration ──────────────────────────────────────────────────────


def test_migration_table_is_well_formed():
    for src, dst in h._HEARING_TYPE_MIGRATION.items():
        assert src not in h.VALID_HEARING_TYPES, f"{src} is still live"
        assert dst in h.VALID_HEARING_TYPES, f"{dst} not in live domain"


def test_read_migration_folds_and_defaults_modality():
    d = h._migrate_hearing({"hearing_type": "procès"})
    assert d["hearing_type"] == "instruction"
    assert d["modalite"] == "présentiel" and d["conference_uri"] == ""
    assert h._migrate_hearing({"hearing_type": "appel"})["hearing_type"] == "audience"
    assert h._migrate_hearing({"hearing_type": "médiation"})["hearing_type"] == "autre"
    # A live type is untouched
    assert h._migrate_hearing({"hearing_type": "audience"})["hearing_type"] == "audience"


# ── §10.3 DAV serialization ──────────────────────────────────────────────


def test_conference_serialized_as_uri_without_escaping():
    ics = h.hearing_to_vevent(_video())
    line = next(l for l in ics.splitlines() if l.startswith("CONFERENCE"))
    assert "VALUE=URI" in line and "FEATURE=VIDEO" in line
    # Raw comma + semicolon, never escaped (Teams links carry them).
    assert "a=1,2;b=3" in line
    assert "\\," not in line and "\\;" not in line


def test_no_conference_when_not_video():
    for m in ("présentiel", "téléphonique"):
        assert "CONFERENCE" not in h.hearing_to_vevent(_video(modalite=m))


def test_categories_stays_mono_valued():
    # A second CATEGORIES value would add a second colored jtx tile.
    assert h.hearing_to_vevent(_video()).count("CATEGORIES") == 1


def test_roundtrip_preserves_modalite_and_uri():
    back = h.vevent_to_hearing(h.hearing_to_vevent(_video()))
    assert back["modalite"] == "visioconférence"
    assert back["conference_uri"] == "https://ex.com/j?a=1,2;b=3"


def test_non_effacement_absent_props_omit_keys():
    """A PUT of a VEVENT stripped of CONFERENCE / X-PALLAS-MODALITE must leave
    the stored values intact — the parser OMITS the keys so update_hearing's
    {**existing, **data} merge keeps them."""
    bare = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:u1\nSUMMARY:T\n"
        "END:VEVENT\nEND:VCALENDAR"
    )
    data = h.vevent_to_hearing(bare)
    assert "conference_uri" not in data
    assert "modalite" not in data


# ── §10.4 security (scheme whitelist) ────────────────────────────────────


@pytest.mark.parametrize("uri", [
    "javascript:alert(1)", "data:text/html,<script>1</script>",
    "vbscript:msgbox", "ftp://ex.com/x", "  javascript:alert(1)  ",
])
def test_validate_rejects_unsafe_conference_uri(uri):
    errors = h._validate({
        "title": "T", "start_datetime": h.datetime(2026, 1, 1, tzinfo=h.timezone.utc),
        "conference_uri": uri,
    })
    assert any("http" in e.lower() for e in errors), (uri, errors)


@pytest.mark.parametrize("uri", ["https://ex.com/a", "http://ex.com", ""])
def test_validate_accepts_safe_conference_uri(uri):
    errors = h._validate({
        "title": "T", "start_datetime": h.datetime(2026, 1, 1, tzinfo=h.timezone.utc),
        "conference_uri": uri,
    })
    assert not any("http" in e.lower() for e in errors), (uri, errors)


def test_incoming_unsafe_uri_is_dropped_not_propagated():
    """A CalDAV PUT carrying a javascript: CONFERENCE must not reach storage:
    the parser omits the key (stored value survives), never propagates it."""
    evil = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:u1\nSUMMARY:T\n"
        "CONFERENCE;VALUE=URI:javascript:alert(1)\nEND:VEVENT\nEND:VCALENDAR"
    )
    assert "conference_uri" not in h.vevent_to_hearing(evil)


# ── Write-layer merge (non-effacement, end to end) ───────────────────────


def test_update_hearing_merge_preserves_omitted_conference_uri(monkeypatch):
    """update_hearing merges {**existing, **data}, so a PUT payload that omits
    conference_uri (parser dropped the absent key) keeps the stored link."""
    stored = {
        "id": "h1", "title": "T",
        "start_datetime": h.datetime(2026, 1, 1, 14, 0, tzinfo=h.timezone.utc),
        "end_datetime": h.datetime(2026, 1, 1, 15, 0, tzinfo=h.timezone.utc),
        "modalite": "visioconférence", "conference_uri": "https://ex.com/keep",
        "hearing_type": "instruction", "status": "confirmée",
    }
    written = {}

    class _Doc:
        def get(self):
            return type("S", (), {"exists": True, "to_dict": lambda s: dict(stored)})()

        def set(self, payload):
            written.update(payload)

    monkeypatch.setattr(h, "db", type("DB", (), {
        "collection": lambda s, n: type("C", (), {"document": lambda s2, i: _Doc()})(),
    })())
    # Client edits only the start time (still before the stored 15:00 end);
    # conference_uri/modalite absent from the payload.
    updated, errors = h.update_hearing("h1", {
        "start_datetime": h.datetime(2026, 1, 1, 14, 30, tzinfo=h.timezone.utc),
    })
    assert errors == []
    assert written["conference_uri"] == "https://ex.com/keep"
    assert written["modalite"] == "visioconférence"
