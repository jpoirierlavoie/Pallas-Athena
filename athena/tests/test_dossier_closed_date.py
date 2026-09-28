"""``opened_date`` / ``closed_date`` sont des dates SEULES (lot 4a).

Toutes deux se rendent par ``strftime`` (carte Aperçu, formulaire), sortent
par ``mcp.tools.date_str`` et nourrissent ``retention_date`` : la convention
maison est minuit UTC du jour civil. Le modèle les estampillait pourtant de
``datetime.now(timezone.utc)`` — un horodatage : un dossier fermé après 20 h
(19 h l'hiver) lisait la date du LENDEMAIN, et sa date de rétention un jour
trop tard, sans une erreur.

Les horloges sont figées à 22 h 30, heure de Montréal, le 28 septembre 2026
— déjà le 29 en UTC —, la bande du soir où l'ancien code se trompait. On
relit ce qui est STOCKÉ, au-dessus du faux Firestore partagé.
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
    from models import dossier as dossier_model
    from utils import deadlines

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
# 22:30 in Montréal (EDT, UTC-4) on 28 September = 02:30 UTC on the 29th.
EVENING_UTC = datetime(2026, 9, 29, 2, 30, tzinfo=UTC)
MONTREAL_DAY = datetime(2026, 9, 28, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return EVENING_UTC if tz is None else EVENING_UTC.astimezone(tz)


@pytest.fixture
def db(monkeypatch):
    # BOTH clocks the model can read are frozen in the evening band: the
    # Montréal calendar (utils.deadlines) and the model's own `now`.
    monkeypatch.setattr(deadlines, "datetime", _FrozenDatetime)
    monkeypatch.setattr(dossier_model, "datetime", _FrozenDatetime)
    assert deadlines.today_mtl().isoformat() == "2026-09-28"
    return install(monkeypatch, dossier_model)


def _create(**over):
    data = {"file_number": "2026-001", "title": "Tremblay c. Lavoie",
            "clients": [{"id": "p1", "name": "Jean Tremblay",
                         "roles": ["demandeur"]}]}
    data.update(over)
    doc, errors = dossier_model.create_dossier(data)
    assert errors == [], errors
    return doc["id"]


def test_an_auto_stamped_opened_date_is_the_montreal_day(db):
    did = _create()
    assert db.peek(f"dossiers/{did}")["opened_date"] == MONTREAL_DAY


def test_a_supplied_opened_date_is_kept(db):
    supplied = datetime(2025, 1, 15, tzinfo=UTC)
    did = _create(opened_date=supplied)
    assert db.peek(f"dossiers/{did}")["opened_date"] == supplied


@pytest.mark.parametrize("status", ["fermé", "archivé"])
def test_a_dossier_created_closed_is_stamped_the_montreal_day(db, status):
    did = _create(status=status)
    assert db.peek(f"dossiers/{did}")["closed_date"] == MONTREAL_DAY


def test_closing_stamps_the_montreal_day_not_tomorrow(db):
    """Régression — l'ancien code stockait 2026-09-29 02:30 UTC : le
    dossier fermé le soir du 28 se lisait fermé le 29."""
    did = _create()
    _doc, errors = dossier_model.update_dossier(did, {"status": "fermé"})
    assert errors == []
    stored = db.peek(f"dossiers/{did}")["closed_date"]
    assert stored == MONTREAL_DAY
    assert (stored.hour, stored.minute) == (0, 0)


def test_a_supplied_closed_date_is_kept(db):
    did = _create()
    supplied = datetime(2026, 9, 1, tzinfo=UTC)
    dossier_model.update_dossier(did, {"status": "fermé",
                                       "closed_date": supplied})
    assert db.peek(f"dossiers/{did}")["closed_date"] == supplied


def test_archiving_a_closed_dossier_keeps_its_closing_date(db):
    did = _create()
    first = datetime(2026, 8, 3, tzinfo=UTC)
    dossier_model.update_dossier(did, {"status": "fermé", "closed_date": first})
    dossier_model.update_dossier(did, {"status": "archivé", "closed_date": None})
    assert db.peek(f"dossiers/{did}")["closed_date"] == first


def test_reopening_clears_the_closing_date(db):
    did = _create()
    dossier_model.update_dossier(did, {"status": "fermé"})
    dossier_model.update_dossier(did, {"status": "actif"})
    assert db.peek(f"dossiers/{did}")["closed_date"] is None


# ── Même jour d'ouverture : l'ordre chronologique survit (revue 4a) ────────
#
# Une date SEULE fait se lier deux dossiers ouverts le même jour. Les listes
# triées EN PYTHON (fiche d'un contact, recherche de dossiers, exports) les
# rendaient alors dans l'ordre du flux — l'ordre des identifiants, des UUID
# au hasard —, là où l'ancien horodatage les gardait chronologiques.
# `created_at` (un vrai horodatage, règle 7) départage. Les identifiants sont
# choisis pour que l'ordre du flux (croissant) soit l'INVERSE de l'ordre de
# création : sans le départage, le test échoue.

_SAME_DAY = datetime(2026, 9, 28, tzinfo=UTC)


@pytest.fixture
def same_day(monkeypatch):
    fake = install(monkeypatch, dossier_model)
    for rid, created in (
        ("a0000000-0000-4000-8000-000000000001",
         datetime(2026, 9, 28, 13, 0, tzinfo=UTC)),   # créé en premier
        ("b0000000-0000-4000-8000-000000000002",
         datetime(2026, 9, 28, 18, 0, tzinfo=UTC)),   # créé ensuite
    ):
        fake.seed(f"dossiers/{rid}", {
            "id": rid, "file_number": rid[:4], "title": "T",
            "status": "actif", "opened_date": _SAME_DAY,
            "created_at": created,
            "clients": [{"id": "p1", "name": "Jean Tremblay", "roles": []}],
            "client_ids": ["p1"], "opposing_parties": [],
            "opposing_party_ids": [], "avocat_ids": [],
        })
    return ["b0000000-0000-4000-8000-000000000002",
            "a0000000-0000-4000-8000-000000000001"]


def test_same_day_dossiers_list_newest_created_first(same_day):
    assert [d["id"] for d in dossier_model.list_dossiers()] == same_day


def test_same_day_dossiers_of_a_partie_list_newest_created_first(same_day):
    rows = dossier_model.list_dossiers_for_partie("p1")
    assert [d["id"] for d in rows] == same_day
