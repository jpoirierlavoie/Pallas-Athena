"""La version d'un budget se lit d'une source qui ne ment pas (lot 0b, B6).

``create_budget`` numérote la nouvelle version à 1 + la plus haute version
existante. Il lisait l'historique par ``list_budget_versions``, qui échoue
OUVERT (``[]``) : un hoquet de lecture valait « aucun historique », et
l'enregistrement frappait une VERSION 1 EN DOUBLE. Celle-ci se trie
SOUS la vraie dernière version (``(version, created_at)`` décroissant) : le
juriste voyait « enregistré », et son budget n'apparaissait nulle part — ni à
l'onglet, ni à l'estimation remise au client. Le ``try/except`` qui gardait
l'appel était du code mort : le lecteur ne levait jamais.

Désormais l'enregistrement lit par ``_list_budget_versions_strict``, qui
PROPAGE, et une lecture ratée REFUSE l'enregistrement avec un message
français ; ``list_budget_versions`` reste ouvert pour l'affichage.

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : la
panne est injectée au serveur (``run_query``), pas dans le modèle — le test
échoue donc sur l'ancien code (vérifié en le rétablissant), qui avalait la
panne et écrivait la v1.
"""

import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import budget as budget_model
    import routes.budgets as budgets_routes
    import routes.dossiers as dossiers_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "hourly_rate": 30000, "clients": [], "client_ids": [],
    })
    return f


def _seed_version(fake, bid: str, version: int, day: int) -> None:
    fake.seed(f"budgets/{bid}", {
        "id": bid, "dossier_id": "dos1", "version": version,
        "hourly_rate": 30000, "note": "",
        "lines": [{"sous_phase": "PRE-01", "hours": 2.0, "frais_cents": 0}],
        "created_at": datetime(2026, 9, day, tzinfo=UTC),
        "updated_at": datetime(2026, 9, day, tzinfo=UTC), "etag": f"e{version}",
    })


def _data(**over) -> dict:
    d = {
        "dossier_id": "dos1", "hourly_rate": 30000, "note": "",
        "lines": [{"sous_phase": "PRE-01", "hours": 3, "frais_cents": 0}],
    }
    d.update(over)
    return d


@pytest.fixture
def reads_fail(fake, monkeypatch):
    """Every QUERY fails at the server; keyed gets and writes still work —
    the blip that made the old reader answer « no history »."""
    def _boom(*_a, **_kw):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(fake._fake_server, "run_query", _boom)


def test_une_lecture_ratee_refuse_l_enregistrement(fake, reads_fail):
    """Régression — l'ancien code avalait la panne et écrivait une v1 en
    double, sous les versions 1 et 2 déjà enregistrées."""
    _seed_version(fake, "b1", 1, 1)
    _seed_version(fake, "b2", 2, 2)
    doc, errs = budget_model.create_budget(_data())
    assert doc is None
    assert errs == [budget_model.VERSION_READ_ERROR]
    assert sorted(fake.peek_collection("budgets")) == ["b1", "b2"]


def test_le_lecteur_strict_propage_l_affichage_reste_ouvert(fake, reads_fail):
    with pytest.raises(RuntimeError):
        budget_model._list_budget_versions_strict("dos1")
    assert budget_model.list_budget_versions("dos1") == []
    assert budget_model.get_latest_budget("dos1") is None


def test_la_nouvelle_version_suit_la_plus_haute(fake):
    """Témoin : la numérotation 1 + max tient, trous compris."""
    _seed_version(fake, "b1", 1, 1)
    _seed_version(fake, "b3", 3, 3)
    doc, errs = budget_model.create_budget(_data())
    assert errs == [], errs
    assert doc["version"] == 4
    assert fake.peek(f"budgets/{doc['id']}")["version"] == 4
    assert budget_model.get_latest_budget("dos1")["id"] == doc["id"]


def test_la_premiere_version_est_la_un(fake):
    doc, errs = budget_model.create_budget(_data())
    assert errs == [], errs
    assert doc["version"] == 1


def test_un_dossier_absent_reste_refuse_sans_lecture(fake, reads_fail):
    doc, errs = budget_model.create_budget(_data(dossier_id=""))
    assert doc is None
    assert "Un dossier doit être associé au budget." in errs


# ── Le chemin web : le refus s'affiche, rien ne s'écrit ─────────────────────


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (budgets_routes.budgets_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _form() -> dict:
    return {
        "dossier_id": "dos1", "hourly_rate": "300,00", "note": "",
        "lines_json": json.dumps(
            [{"sous_phase": "PRE-01", "hours": 3, "frais": ""}]),
    }


def test_le_formulaire_dit_la_lecture_ratee_et_n_ecrit_rien(fake, client, reads_fail):
    """Régression — l'ancien code redirigeait vers l'onglet Budget (succès
    apparent) après avoir écrit une v1 invisible."""
    _seed_version(fake, "b1", 1, 1)
    resp = client.post("/budgets/", data=_form())
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "n&#39;ont pas pu être lues" in html
    assert "aucune version n&#39;a été enregistrée" in html
    assert sorted(fake.peek_collection("budgets")) == ["b1"]


def test_le_formulaire_enregistre_la_version_suivante(fake, client):
    _seed_version(fake, "b1", 1, 1)
    resp = client.post("/budgets/", data=_form())
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    versions = sorted(b["version"] for b in fake.peek_collection("budgets").values())
    assert versions == [1, 2]
