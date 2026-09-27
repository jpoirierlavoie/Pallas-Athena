"""Audiences — ce que ``update_hearing`` écrit (lot 0b, B4).

Deux défauts silencieux, épinglés contre le VRAI client Firestore
(``tests/_fake_firestore.py`` : seul le serveur est faux) :

1. **La plage horaire.** La fusion ``{**existing, **data}`` ne remplissait
   ``end_datetime`` que s'il MANQUAIT. Un début déplacé sans fin gardait donc
   l'ANCIENNE fin absolue : avancer d'un jour une rencontre d'une heure en
   faisait un événement de 25 heures (``_validate`` ne vérifie que fin >
   début) — au téléphone, dans Outlook, à l'écran. Et basculer « toute la
   journée » relisait l'instant stocké dans l'autre convention : l'événement
   tombait sur le mauvais jour civil.
2. **Les clés.** La fusion honorait N'IMPORTE QUELLE clé : un « id » corrompait
   le CHAMP id (le chemin du document restait), un ``serie_id: ""`` détachait
   une occurrence en silence, un « confirmation » ouvrait ou fermait la
   visibilité DAV/MCP, un « graph_* » cassait la réconciliation Bookings.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import models.hearing as h

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import mtl_to_utc, to_mtl  # noqa: E402

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Tremblay c. Lavoie", "status": "actif"})
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                              "title": "Roy c. Gagnon", "status": "actif"})
    return fake


def _mtl(y, m, d, hh=0, mm=0) -> datetime:
    return mtl_to_utc(datetime(y, m, d, hh, mm))


def _seed_timed(fake, hid="h1", start=None, end=None, **over) -> dict:
    start = start or _mtl(2026, 10, 15, 9)
    end = end or start + timedelta(hours=1)
    doc = {"id": hid, "title": "Rencontre", "hearing_type": "rencontre",
           "dossier_id": "d1", "dossier_file_number": "2026-001",
           "dossier_title": "Tremblay c. Lavoie",
           "start_datetime": start, "end_datetime": end, "all_day": False,
           "status": "confirmée", "vevent_uid": f"uid-{hid}", "etag": "e0",
           "dav_href": f"/dav/dossier-d1/{hid}.ics"}
    doc.update(over)
    fake.seed(f"hearings/{hid}", doc)
    return doc


def _seed_all_day(fake, hid="h1", day=date(2026, 10, 15), days=1, **over):
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return _seed_timed(fake, hid, start=start,
                       end=start + timedelta(days=days), all_day=True, **over)


def _stored(fake, hid="h1") -> dict:
    return fake.peek(f"hearings/{hid}")


def _dt(fake_value) -> datetime:
    """The fake server returns DatetimeWithNanoseconds; compare as datetime."""
    return datetime.fromtimestamp(fake_value.timestamp(), tz=UTC)


# ══════════════════════════════════════════════════════════════════════
# 1. La plage horaire
# ══════════════════════════════════════════════════════════════════════


def test_moving_the_start_earlier_keeps_the_duration(fake):
    """THE defect: the old code kept the old absolute end, so a one-hour
    meeting moved one day earlier became a 25-hour event."""
    _seed_timed(fake)
    new_start = _mtl(2026, 10, 14, 9)
    doc, errors = h.update_hearing("h1", {"start_datetime": new_start})
    assert errors == []
    stored = _stored(fake)
    assert _dt(stored["start_datetime"]) == new_start
    assert _dt(stored["end_datetime"]) == new_start + timedelta(hours=1)
    assert doc["end_datetime"] - doc["start_datetime"] == timedelta(hours=1)


def test_moving_the_start_past_the_old_end_keeps_the_duration(fake):
    """The mirror case the old code REFUSED (« fin avant début ») — a phone
    that moves an event later without resending DTEND is an edit, not an
    error."""
    _seed_timed(fake, end=_mtl(2026, 10, 15, 10, 30))
    new_start = _mtl(2026, 10, 16, 14)
    doc, errors = h.update_hearing("h1", {"start_datetime": new_start})
    assert errors == []
    assert doc["end_datetime"] == new_start + timedelta(hours=1, minutes=30)


def test_a_named_end_is_kept(fake):
    _seed_timed(fake)
    new_start, new_end = _mtl(2026, 10, 14, 9), _mtl(2026, 10, 14, 12)
    doc, errors = h.update_hearing(
        "h1", {"start_datetime": new_start, "end_datetime": new_end})
    assert errors == [] and doc["end_datetime"] == new_end


def test_an_end_named_none_falls_back_to_one_hour(fake):
    """The web form sends end=None when the end time is cleared: that is
    « no end », the create default — distinct from an ABSENT key."""
    _seed_timed(fake, end=_mtl(2026, 10, 15, 12))
    new_start = _mtl(2026, 10, 14, 9)
    doc, errors = h.update_hearing(
        "h1", {"start_datetime": new_start, "end_datetime": None})
    assert errors == [] and doc["end_datetime"] == new_start + timedelta(hours=1)


def test_an_update_that_names_no_slot_field_leaves_the_slot_alone(fake):
    seeded = _seed_timed(fake, end=_mtl(2026, 10, 15, 11, 45))
    doc, errors = h.update_hearing("h1", {"title": "Rencontre reportée"})
    assert errors == []
    assert doc["start_datetime"] == seeded["start_datetime"]
    assert doc["end_datetime"] == seeded["end_datetime"]


def test_an_end_before_the_start_is_still_refused(fake):
    _seed_timed(fake)
    doc, errors = h.update_hearing(
        "h1", {"end_datetime": _mtl(2026, 10, 15, 8)})
    assert doc is None and errors


def test_timed_to_all_day_lands_on_the_montreal_day(fake):
    """21 h in Montréal on the 15th is 01 h UTC on the 16th. The old code
    kept the instant, and the serializer read its UTC date: the all-day
    event showed on the 16th."""
    _seed_timed(fake, start=_mtl(2026, 10, 15, 21))
    doc, errors = h.update_hearing("h1", {"all_day": True})
    assert errors == []
    assert doc["start_datetime"] == datetime(2026, 10, 15, tzinfo=UTC)
    ical = h.hearing_to_vevent(doc)
    assert "DTSTART;VALUE=DATE:20261015" in ical
    assert "DTEND;VALUE=DATE:20261016" in ical


def test_timed_to_all_day_at_utc_midnight_still_uses_the_montreal_day(fake):
    """20 h in Montréal in summer IS 00 h UTC the next day. Taking a
    midnight-UTC value « as already the convention » would put the event on
    the 16th; the stored value was an instant, so its day is Montréal's."""
    start = _mtl(2026, 10, 15, 20)
    assert start == datetime(2026, 10, 16, tzinfo=UTC)
    _seed_timed(fake, start=start)
    doc, errors = h.update_hearing("h1", {"all_day": True})
    assert errors == []
    assert doc["start_datetime"] == datetime(2026, 10, 15, tzinfo=UTC)


def test_timed_to_all_day_does_not_carry_the_timed_duration(fake):
    """A three-hour meeting made all-day is a ONE-day event, not a slot
    that ends three hours after midnight UTC (19 h the evening before)."""
    _seed_timed(fake, end=_mtl(2026, 10, 15, 12))
    doc, errors = h.update_hearing("h1", {"all_day": True})
    assert errors == []
    assert doc["end_datetime"] == doc["start_datetime"] + timedelta(hours=1)
    assert "DTEND;VALUE=DATE:20261016" in h.hearing_to_vevent(doc)


def test_all_day_to_timed_keeps_the_civil_day(fake):
    """The stored all-day value is the DATE at midnight UTC; read as an
    instant it is 20 h the evening BEFORE in Montréal."""
    _seed_all_day(fake)
    doc, errors = h.update_hearing("h1", {"all_day": False})
    assert errors == []
    assert to_mtl(doc["start_datetime"]).date() == date(2026, 10, 15)
    assert doc["start_datetime"] == _mtl(2026, 10, 15, 0)
    assert doc["end_datetime"] == doc["start_datetime"] + timedelta(hours=1)


def test_moving_an_all_day_span_keeps_the_span(fake):
    _seed_all_day(fake, days=3)
    new_start = datetime(2026, 11, 2, tzinfo=UTC)
    doc, errors = h.update_hearing("h1", {"start_datetime": new_start})
    assert errors == []
    assert doc["end_datetime"] == new_start + timedelta(days=3)
    ical = h.hearing_to_vevent(doc)
    assert "DTSTART;VALUE=DATE:20261102" in ical
    assert "DTEND;VALUE=DATE:20261105" in ical


def test_a_timed_start_named_for_an_all_day_event_takes_its_montreal_day(fake):
    _seed_all_day(fake)
    doc, errors = h.update_hearing(
        "h1", {"start_datetime": _mtl(2026, 10, 20, 22)})
    assert errors == []
    assert doc["start_datetime"] == datetime(2026, 10, 20, tzinfo=UTC)
    assert doc["end_datetime"] == datetime(2026, 10, 21, tzinfo=UTC)


def test_a_timed_end_named_for_an_all_day_event_becomes_exclusive(fake):
    _seed_all_day(fake)
    doc, errors = h.update_hearing("h1", {
        "start_datetime": datetime(2026, 10, 15, tzinfo=UTC),
        "end_datetime": _mtl(2026, 10, 17, 17),
    })
    assert errors == []
    assert doc["end_datetime"] == datetime(2026, 10, 18, tzinfo=UTC)
    assert "DTEND;VALUE=DATE:20261018" in h.hearing_to_vevent(doc)


def test_a_vevent_all_day_edit_round_trips_unchanged(fake):
    """What the phone sends for an all-day event — DATE values, an
    exclusive DTEND — is already the convention and is stored as is."""
    _seed_all_day(fake)
    doc, errors = h.update_hearing("h1", {
        "start_datetime": datetime(2026, 10, 22, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 24, tzinfo=UTC),
        "all_day": True,
    })
    assert errors == []
    assert doc["start_datetime"] == datetime(2026, 10, 22, tzinfo=UTC)
    assert doc["end_datetime"] == datetime(2026, 10, 24, tzinfo=UTC)


def test_the_dav_href_follows_a_dossier_move(fake):
    _seed_timed(fake)
    doc, errors = h.update_hearing("h1", {
        "dossier_id": "d2", "dossier_file_number": "2026-002",
        "dossier_title": "Roy c. Gagnon"})
    assert errors == []
    assert _stored(fake)["dav_href"] == "/dav/dossier-d2/h1.ics"
    doc, errors = h.update_hearing("h1", {
        "dossier_id": "", "dossier_file_number": "", "dossier_title": ""})
    assert errors == []
    assert _stored(fake)["dav_href"] == "/dav/general/h1.ics"


# ══════════════════════════════════════════════════════════════════════
# 2. Les clés
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("key, value", [
    ("id", "autre"),
    ("vevent_uid", "forged"),
    ("serie_id", ""),
    ("serie_rule", None),
    ("confirmation", "à_confirmer"),
    ("source", "bookings"),
    ("graph_event_id", "EVT-X"),
    ("bookings_divergence", {"motif": "x"}),
    ("etag", "e9"),
    ("created_at", datetime(2020, 1, 1, tzinfo=UTC)),
    ("anything_else", "x"),
])
def test_a_generic_update_cannot_name_a_server_owned_key(fake, key, value):
    seeded = _seed_timed(fake, serie_id="s1")
    before = _stored(fake)
    doc, errors = h.update_hearing("h1", {"title": "T2", key: value})
    assert doc is None
    assert errors == [
        f"Le champ « {key} » ne peut pas être modifié par cette voie."]
    assert _stored(fake) == before, "nothing may be written on a refusal"
    assert seeded["serie_id"] == "s1"


def test_server_fields_reach_only_the_server_owned_keys(fake):
    _seed_timed(fake, source="bookings", confirmation="à_confirmer")
    doc, errors = h.update_hearing(
        "h1", {}, server_fields={"confirmation": "", "partie_id": "p1"})
    assert errors == []
    stored = _stored(fake)
    assert stored["confirmation"] == "" and stored["partie_id"] == "p1"

    doc, errors = h.update_hearing(
        "h1", {}, server_fields={"title": "détour"})
    assert doc is None
    assert errors == [
        "Le champ « title » n'appartient pas aux champs réservés au serveur."]


def test_every_key_vevent_to_hearing_produces_is_updatable_but_the_uid():
    """The DAV PUT feeds the parser's output to update_hearing (minus the
    UID the route drops). A key the parser emits that the whitelist does
    not name would 422 EVERY phone edit — DavX5 fails silently."""
    event = h.hearing_to_vevent({
        "vevent_uid": "u1", "title": "T", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "X",
        "start_datetime": _mtl(2026, 10, 15, 9),
        "end_datetime": _mtl(2026, 10, 15, 10),
        "location": "Palais", "court": "CS", "judge": "J",
        "notes": "N", "hearing_type": "instruction", "status": "confirmée",
        "modalite": "visioconférence", "conference_uri": "https://ex.com/v",
        "reminder_minutes": 60,
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 2, tzinfo=UTC),
    })
    keys = set(h.vevent_to_hearing(event))
    assert "vevent_uid" in keys
    assert keys - {"vevent_uid"} <= h.UPDATE_FIELDS
    assert not (h.UPDATE_FIELDS & h.SERVER_FIELDS)


def test_unlink_goes_through_server_fields(fake):
    _seed_timed(fake, serie_id="s1", serie_rule={"freq": "hebdomadaire"})
    doc, errors = h.unlink_hearing("h1")
    assert errors == []
    stored = _stored(fake)
    assert stored["serie_id"] == "" and stored["serie_rule"] is None


# ══════════════════════════════════════════════════════════════════════
# 2 bis. La version lue (lot 1a, L4) — expected_etag
# ══════════════════════════════════════════════════════════════════════
#
# The web edit form now hands the model the etag it was rendered from.
# Before, every update_hearing was a blind full-document set(): a tab left
# open over a phone edit (or the Bookings sync's silent slot update)
# rewrote the stale slot in silence.


def test_a_current_etag_commits_and_moves_the_etag(fake):
    _seed_timed(fake)
    doc, errors = h.update_hearing("h1", {"notes": "Salle 2.08"},
                                   expected_etag="e0")
    assert errors == []
    stored = _stored(fake)
    assert stored["notes"] == "Salle 2.08"
    assert stored["etag"] == doc["etag"] != "e0"


def test_a_stale_etag_writes_nothing(fake):
    _seed_timed(fake)
    before = _stored(fake)
    doc, errors = h.update_hearing("h1", {"notes": "x"}, expected_etag="old")
    assert doc is None
    assert errors == [h.concurrency.STALE_ETAG_ERROR]
    assert _stored(fake) == before


def test_a_stale_etag_is_answered_before_validation(fake):
    """« Re-read » is the useful answer to an outdated view — not the first
    field error the outdated view happens to trip."""
    _seed_timed(fake)
    _doc, errors = h.update_hearing("h1", {"title": ""}, expected_etag="old")
    assert errors == [h.concurrency.STALE_ETAG_ERROR]


def test_a_write_landing_between_the_read_and_the_commit_is_refused(fake):
    """The comparison runs INSIDE the transaction: another writer racing the
    commit (the Bookings sync, the phone) makes the save refuse, never
    overwrite."""
    _seed_timed(fake)
    raced = []

    def _race(_info):
        if not raced:
            raced.append(1)
            doc = _stored(fake)
            doc.update(notes="Écrit par le téléphone", etag="e-phone")
            fake.external_write("hearings/h1", doc)

    remove = fake.add_commit_hook(_race)
    try:
        doc, errors = h.update_hearing("h1", {"notes": "x"},
                                       expected_etag="e0")
    finally:
        remove()
    assert doc is None and errors == [h.concurrency.STALE_ETAG_ERROR]
    assert _stored(fake)["notes"] == "Écrit par le téléphone"


def test_no_etag_is_the_unchanged_legacy_write(fake):
    """The DAV PUT and the Bookings sync pass none: last write wins, as
    before — their own guards (If-Match, lastModified) are elsewhere."""
    _seed_timed(fake, etag="e-other")
    doc, errors = h.update_hearing("h1", {"notes": "x"})
    assert errors == [] and _stored(fake)["notes"] == "x"


# ══════════════════════════════════════════════════════════════════════
# 3. Le PUT DAV (la vraie route)
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def test_a_phone_edit_is_accepted_and_keeps_the_stored_uid(fake, client):
    """The UID in the body names the existing resource; the route drops it
    before update_hearing, which would otherwise refuse the key (a 422 on
    every phone edit — DavX5 would swallow it)."""
    _seed_timed(fake)
    got = client.get("/dav/dossier-d1/h1.ics", headers=AUTH)
    assert got.status_code == 200
    body = got.get_data(as_text=True).replace("SUMMARY:Rencontre",
                                              "SUMMARY:Rencontre déplacée")
    resp = client.put("/dav/dossier-d1/h1.ics", data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar",
                               "If-Match": got.headers["ETag"]})
    assert resp.status_code == 204
    stored = _stored(fake)
    assert stored["title"] == "Rencontre déplacée"
    assert stored["vevent_uid"] == "uid-h1"


def test_a_phone_move_without_dtend_keeps_the_duration(fake, client):
    """A client may resend DTSTART alone (or DURATION, which the parser
    does not read): the stored duration is kept, never the old end."""
    _seed_timed(fake, end=_mtl(2026, 10, 15, 11))
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//FR",
        "BEGIN:VEVENT", "UID:uid-h1", "SUMMARY:Rencontre",
        "DTSTART:20261013T130000Z", "DTSTAMP:20260926T120000Z",
        "END:VEVENT", "END:VCALENDAR", ""])
    resp = client.put("/dav/dossier-d1/h1.ics", data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar"})
    assert resp.status_code == 204
    stored = _stored(fake)
    assert _dt(stored["start_datetime"]) == datetime(2026, 10, 13, 13, tzinfo=UTC)
    assert _dt(stored["end_datetime"]) == datetime(2026, 10, 13, 15, tzinfo=UTC)
