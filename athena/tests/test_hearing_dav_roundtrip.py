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
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import models.hearing as h

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

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
