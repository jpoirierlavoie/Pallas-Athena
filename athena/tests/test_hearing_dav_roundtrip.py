"""Audiences — ce qu'un aller-retour par le téléphone laisse intact (lot 0b, B4).

Le téléphone renvoie le VEVENT qu'on lui a servi à CHAQUE édition, de
n'importe quel champ. Tout ce que le sérialiseur perd ou ajoute se paie donc
à chaque synchro, en silence (DavX5 ne dit rien) :

* **Le statut.** RFC 5545 n'a que trois valeurs de STATUS pour cinq statuts :
  « reportée » et « terminée » se relisaient « à_confirmer » et
  « confirmée ». Une audience reportée ou terminée dans l'application
  reprenait son ancien sens à la première édition faite au téléphone.

Épinglé au niveau du sérialiseur ET par la vraie route PUT/GET de
``dav/dossier_collections.py`` sur le vrai client Firestore
(``tests/_fake_firestore.py``). La vérification sur l'appareil reste à faire
(CLAUDE.md, composant nº 2) : DavX5 doit conserver la propriété X-.
"""

import os
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # its db is patched below
    import models.hearing as h

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync,)

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
START = datetime(2026, 10, 15, 13, tzinfo=UTC)


def _hearing(**over) -> dict:
    base = {
        "id": "h1", "vevent_uid": "uid-h1", "title": "Audience",
        "hearing_type": "audience", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "Tremblay c. Lavoie",
        "start_datetime": START, "end_datetime": START + timedelta(hours=1),
        "all_day": False, "status": "confirmée", "modalite": "présentiel",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 2, tzinfo=UTC),
    }
    base.update(over)
    return base


def _replace_line(ical: str, prefix: str, new: str | None) -> str:
    out = []
    for line in ical.split("\r\n"):
        if line.startswith(prefix):
            if new is not None:
                out.append(new)
            continue
        out.append(line)
    return "\r\n".join(out)


# ══════════════════════════════════════════════════════════════════════
# Le statut
# ══════════════════════════════════════════════════════════════════════


def test_the_status_map_covers_the_whole_status_domain():
    assert set(h._DAV_STATUS) == set(h.VALID_STATUSES)
    assert set(h._DAV_STATUS_REVERSE.values()) <= set(h.VALID_STATUSES)


@pytest.mark.parametrize("status", h.VALID_STATUSES)
def test_every_status_survives_the_round_trip(status):
    """THE defect for « reportée » and « terminée »: STATUS alone read them
    back as « à_confirmer » and « confirmée »."""
    ical = h.hearing_to_vevent(_hearing(status=status))
    assert h.vevent_to_hearing(ical)["status"] == status


def test_the_exact_status_is_emitted_beside_the_standard_one():
    ical = h.hearing_to_vevent(_hearing(status="reportée"))
    assert "STATUS:TENTATIVE" in ical
    assert "X-PALLAS-STATUS:reportée" in ical


@pytest.mark.parametrize("stored, phone_status, expected", [
    ("reportée", "CONFIRMED", "confirmée"),
    ("reportée", "CANCELLED", "annulée"),
    ("terminée", "TENTATIVE", "à_confirmer"),
    ("terminée", "CANCELLED", "annulée"),
    ("annulée", "CONFIRMED", "confirmée"),
])
def test_a_status_changed_on_the_phone_wins_over_the_stale_x_property(
    stored, phone_status, expected
):
    """The X-property is what the phone was SERVED; a STATUS it no longer
    maps to is a change the user made on the phone, and that change wins."""
    ical = _replace_line(h.hearing_to_vevent(_hearing(status=stored)),
                         "STATUS:", f"STATUS:{phone_status}")
    assert f"X-PALLAS-STATUS:{stored}" in ical
    assert h.vevent_to_hearing(ical)["status"] == expected


def test_an_absent_status_omits_the_key_whatever_the_x_property_says():
    """Non-effacement: update_hearing merges, so a present key overwrites.
    No STATUS received means nothing to say about the status."""
    ical = _replace_line(h.hearing_to_vevent(_hearing(status="reportée")),
                         "STATUS:", None)
    assert "X-PALLAS-STATUS:reportée" in ical
    assert "status" not in h.vevent_to_hearing(ical)


def test_without_the_x_property_status_reads_as_before():
    """A client that dropped the X-property (or an event created on the
    phone) falls back to the historical STATUS-only reading."""
    ical = _replace_line(h.hearing_to_vevent(_hearing(status="terminée")),
                         "X-PALLAS-STATUS:", None)
    assert h.vevent_to_hearing(ical)["status"] == "confirmée"


def test_an_unknown_x_status_is_ignored():
    ical = _replace_line(h.hearing_to_vevent(_hearing(status="reportée")),
                         "X-PALLAS-STATUS:", "X-PALLAS-STATUS:inventée")
    assert h.vevent_to_hearing(ical)["status"] == "à_confirmer"


def test_a_decomposed_x_status_is_still_recognized():
    """Android does not guarantee NFC: « reportée » may come back as
    « reporte » + U+0301 (the reason VTODO CATEGORIES carry ASCII codes).
    Unrecognized, it would silently degrade to « à_confirmer »."""
    nfd = unicodedata.normalize("NFD", "reportée")
    assert nfd != "reportée"
    ical = _replace_line(h.hearing_to_vevent(_hearing(status="reportée")),
                         "X-PALLAS-STATUS:", f"X-PALLAS-STATUS:{nfd}")
    assert h.vevent_to_hearing(ical)["status"] == "reportée"


# ── Par la vraie route ──────────────────────────────────────────────────


@pytest.fixture
def fake(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Tremblay c. Lavoie", "status": "actif"})
    return fake


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def _round_trip(client, href: str, edit=None) -> None:
    got = client.get(href, headers=AUTH)
    assert got.status_code == 200
    body = got.get_data(as_text=True)
    if edit:
        body = edit(body)
    resp = client.put(href, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar",
                               "If-Match": got.headers["ETag"]})
    assert resp.status_code == 204, resp.get_data(as_text=True)


@pytest.mark.parametrize("status", ["reportée", "terminée"])
def test_a_phone_edit_keeps_a_postponed_or_finished_hearing_status(
    fake, client, status
):
    fake.seed("hearings/h1", _hearing(status=status, etag="e0"))
    _round_trip(client, "/dav/dossier-d1/h1.ics",
                edit=lambda b: b.replace("SUMMARY:Audience",
                                         "SUMMARY:Audience (salle 2.08)"))
    stored = fake.peek("hearings/h1")
    assert stored["title"] == "Audience (salle 2.08)"
    assert stored["status"] == status


def test_a_status_changed_on_the_phone_reaches_the_store(fake, client):
    fake.seed("hearings/h1", _hearing(status="reportée", etag="e0"))
    _round_trip(client, "/dav/dossier-d1/h1.ics",
                edit=lambda b: b.replace("STATUS:TENTATIVE",
                                         "STATUS:CONFIRMED"))
    assert fake.peek("hearings/h1")["status"] == "confirmée"


# ══════════════════════════════════════════════════════════════════════
# Le suffixe de DESCRIPTION
# ══════════════════════════════════════════════════════════════════════

VISIO = "https://teams.example.com/l/meetup?a=1,2;b=3"
BLOCK = "\n".join([
    "Dossier: 2026-001 - Tremblay c. Lavoie",
    "Type: Audience",
    "Modalité: Visioconférence",
    f"Visioconférence: {VISIO}",
    "Cour: Cour supérieure",
    "Juge: Hon. Roy",
])


def _visio(**over) -> dict:
    return _hearing(modalite="visioconférence", conference_uri=VISIO,
                    court="Cour supérieure", judge="Hon. Roy", **over)


def _description(ical: str) -> str:
    import icalendar
    for comp in icalendar.Calendar.from_ical(ical).walk():
        if comp.name == "VEVENT":
            return str(comp.get("description") or "")
    return ""


def test_the_serializer_and_the_stripper_share_one_block():
    hearing = _visio(notes="Apporter les pièces")
    assert h.dav_description_suffix(hearing) == BLOCK
    assert _description(h.hearing_to_vevent(hearing)) == (
        f"Apporter les pièces\n{BLOCK}")
    assert h.dav_description_suffix({"title": "T"}) == ""


def test_the_visio_line_is_still_served_to_the_phone():
    """The Android calendar drops CONFERENCE: the DESCRIPTION line is the
    only place the link renders. Stripping its ECHO must not stop serving
    it."""
    desc = _description(h.hearing_to_vevent(_visio(notes="")))
    assert f"Visioconférence: {VISIO}" in desc


@pytest.mark.parametrize("incoming, expected", [
    (f"Texte\n{BLOCK}", "Texte"),
    ("Texte\r\n" + BLOCK.replace("\n", "\r\n"), "Texte"),
    (BLOCK, ""),
    (BLOCK.replace("\n", "\r\n"), ""),
    # Legacy damage: blocks accumulated before the fix peel off too — every
    # one of them is the serializer's own output, never the lawyer's text.
    (f"Texte\n{BLOCK}\n{BLOCK}\n{BLOCK}", "Texte"),
    # Anything that is not EXACTLY the block is the lawyer's: left alone.
    (f"Texte\n{BLOCK} (bis)", f"Texte\n{BLOCK} (bis)"),
    (f"Texte {BLOCK}", f"Texte {BLOCK}"),
    ("Juge: Hon. Roy", "Juge: Hon. Roy"),
    ("", ""),
])
def test_only_the_exact_serializer_block_is_stripped(incoming, expected):
    data = {"notes": incoming}
    h.strip_dav_description_suffix(data, _visio())
    assert data["notes"] == expected


def test_no_notes_key_stays_no_notes_key():
    """Non-effacement: a VEVENT without DESCRIPTION must not gain an empty
    notes key (update_hearing merges, and a present-but-empty key erases)."""
    data = {"title": "T"}
    h.strip_dav_description_suffix(data, _visio())
    assert "notes" not in data


def test_the_non_effacement_of_the_conference_link_is_untouched():
    """The strip works on notes only: a VEVENT without CONFERENCE still
    omits conference_uri/modalite, so the stored link survives."""
    ical = _replace_line(h.hearing_to_vevent(_visio(notes="N")),
                         "CONFERENCE", None)
    ical = _replace_line(ical, "X-PALLAS-MODALITE", None)
    data = h.vevent_to_hearing(ical)
    h.strip_dav_description_suffix(data, _visio(notes="N"))
    assert "conference_uri" not in data and "modalite" not in data
    assert data["notes"] == "N"


@pytest.mark.parametrize("notes", ["Apporter les pièces", ""])
def test_putting_back_an_unchanged_vevent_does_not_grow_the_notes(
    fake, client, notes
):
    """The round trip the phone performs on every edit of ANY field. On the
    old code the stored notes gained the whole metadata block each time."""
    fake.seed("hearings/h1", _visio(notes=notes, etag="e0"))
    for _ in range(3):
        _round_trip(client, "/dav/dossier-d1/h1.ics")
    stored = fake.peek("hearings/h1")
    assert stored["notes"] == notes
    assert stored["conference_uri"] == VISIO
    assert stored["modalite"] == "visioconférence"


def test_a_phone_edit_of_the_notes_keeps_the_edit_and_drops_the_block(
    fake, client
):
    fake.seed("hearings/h1", _visio(notes="Apporter les pièces", etag="e0"))
    _round_trip(client, "/dav/dossier-d1/h1.ics",
                edit=lambda b: b.replace("Apporter les pièces",
                                         "Apporter les pièces et le cahier"))
    assert fake.peek("hearings/h1")["notes"] == (
        "Apporter les pièces et le cahier")


def test_legacy_accumulated_blocks_heal_on_the_next_phone_edit(fake, client):
    fake.seed("hearings/h1",
              _visio(notes=f"Apporter les pièces\n{BLOCK}\n{BLOCK}",
                     etag="e0"))
    _round_trip(client, "/dav/dossier-d1/h1.ics")
    assert fake.peek("hearings/h1")["notes"] == "Apporter les pièces"


# ══════════════════════════════════════════════════════════════════════
# Finitions, sync-7 — the block the phone keeps sending back
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("incoming, expected", [
    # The lawyer types AFTER the block — where the phone shows it.
    (f"Texte\n{BLOCK}\nAjout du téléphone", "Texte\nAjout du téléphone"),
    ("Texte\r\n" + BLOCK.replace("\n", "\r\n") + "\r\nAjout",
     "Texte\r\nAjout"),
    (f"{BLOCK}\nAjout", "Ajout"),
    # A retouched line keeps the whole run the lawyer's.
    (f"Texte\n{BLOCK.replace('Hon. Roy', 'Hon. Roy (remplacé)')}\nAjout",
     f"Texte\n{BLOCK.replace('Hon. Roy', 'Hon. Roy (remplacé)')}\nAjout"),
])
def test_the_block_goes_wherever_it_stands_as_whole_lines(incoming, expected):
    data = {"notes": incoming}
    h.strip_dav_description_suffix(data, _visio())
    assert data["notes"] == expected


def test_text_typed_after_the_block_on_the_phone_never_stores_it(fake, client):
    fake.seed("hearings/h1", _visio(notes="Apporter les pièces", etag="e0"))

    def _append_on_the_phone(body: str) -> str:
        import icalendar
        cal = icalendar.Calendar.from_ical(body)
        for comp in cal.walk():
            if comp.name == "VEVENT":
                served = str(comp.get("description"))
                assert served.endswith("Juge: Hon. Roy")
                comp["description"] = icalendar.vText(
                    served + "\nApporter aussi le bordereau")
        return cal.to_ical().decode("utf-8")

    _round_trip(client, "/dav/dossier-d1/h1.ics", edit=_append_on_the_phone)
    stored = fake.peek("hearings/h1")
    assert stored["notes"] == ("Apporter les pièces\n"
                               "Apporter aussi le bordereau")
    assert "Dossier:" not in stored["notes"]


@pytest.mark.parametrize("served_from", ["d1", ""])
def test_an_event_moved_between_calendars_does_not_import_its_block(
        fake, client, served_from):
    """A calendar app moves an event by re-uploading it under a NEW name in
    the target collection — the CREATE branch — with the DESCRIPTION it was
    served. Nothing stripped it there."""
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                              "title": "Autre c. Autre", "status": "actif"})
    served = _visio(notes="Apporter les pièces", dossier_id=served_from,
                    dossier_file_number="2026-001" if served_from else "",
                    dossier_title="Tremblay c. Lavoie" if served_from else "")
    body = h.hearing_to_vevent(served).replace("UID:uid-h1", "UID:uid-moved")
    resp = client.put("/dav/dossier-d2/moved-1.ics", data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar",
                               "If-None-Match": "*"})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    stored = fake.peek("hearings/moved-1")
    assert stored["notes"] == "Apporter les pièces"
    assert stored["dossier_id"] == "d2"
    # And what the phone is served back carries ONE block, this dossier's.
    desc = _description(h.hearing_to_vevent(stored))
    assert desc.count("Type: Audience") == 1
    assert "Dossier: 2026-002 - Autre c. Autre" in desc


def test_a_phone_typed_event_keeps_every_line_of_its_text(fake, client):
    """A new event typed on the phone carries no X-PALLAS property: nothing
    of its description is the serializer's, whatever its lines say."""
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//t//t//FR",
        "BEGIN:VEVENT", "UID:uid-phone", "DTSTAMP:20260920T160000Z",
        "DTSTART:20261015T130000Z", "DTEND:20261015T140000Z",
        "SUMMARY:Rendez-vous", "DESCRIPTION:Type: Rencontre\\nMa note",
        "END:VEVENT", "END:VCALENDAR", ""])
    resp = client.put("/dav/dossier-d1/phone-1.ics", data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar"})
    assert resp.status_code == 201
    assert fake.peek("hearings/phone-1")["notes"] == "Type: Rencontre\nMa note"
