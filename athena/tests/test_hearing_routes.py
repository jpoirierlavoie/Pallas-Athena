"""Routes des audiences — ce que le formulaire web envoie au modèle (lot 0b, B4).

* **Le dossier introuvable.** ``_enrich_dossier_info`` BLANCHISSAIT un
  ``dossier_id`` qui ne se résout pas : le modèle accepte ``""`` (une audience
  peut être autonome), donc la date de cour passait en silence dans
  « Général » — hors de l'onglet du dossier, dans une autre collection DAV
  sur le téléphone — avec une redirection de succès. Elle est désormais
  REFUSÉE, re-rendue à 200 (htmx n'échange que les 2xx), comme les notes.
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
from markupsafe import escape  # noqa: E402

from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.hearings as rh

UTC = timezone.utc
NOT_FOUND = (
    "Dossier introuvable. Choisissez un dossier existant, ou laissez le "
    "champ vide pour classer l'audience dans « Général »."
)
DOSSIERS = {"d1": {"id": "d1", "file_number": "2026-001",
                   "title": "Tremblay c. Lavoie"}}


@pytest.fixture()
def client(monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rh.hearings_bp)
    monkeypatch.setattr(rh, "get_dossier", DOSSIERS.get)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


@pytest.fixture()
def bumps(monkeypatch):
    seen = []
    monkeypatch.setattr(rh, "bump_ctag", seen.append)
    monkeypatch.setattr(rh, "record_tombstone", lambda *a: seen.append(a))
    monkeypatch.setattr(rh, "remove_tombstone", lambda *a: seen.append(a))
    return seen


def _form(**over) -> dict:
    base = {"title": "Audience", "start_date": "2026-10-15",
            "start_time": "09:00", "end_time": "10:00",
            "hearing_type": "audience", "status": "confirmée",
            "dossier_id": "d1", "dossier_display": "2026-001"}
    base.update(over)
    return base


def _forbid(name):
    def _raise(*a, **k):
        raise AssertionError(f"{name} ne doit pas être appelé")
    return _raise


def test_create_refuses_an_unresolvable_dossier(client, monkeypatch, bumps):
    """THE defect: the old route blanked the id and created the court date
    under « Général » with a success redirect."""
    monkeypatch.setattr(rh, "create_hearing", _forbid("create_hearing"))
    r = client.post("/audiences/", data=_form(dossier_id="parti"))
    assert r.status_code == 200
    assert str(escape(NOT_FOUND)) in r.get_data(as_text=True)
    assert bumps == []


def test_a_series_create_refuses_an_unresolvable_dossier(client, monkeypatch):
    monkeypatch.setattr(rh, "create_hearing_series",
                        _forbid("create_hearing_series"))
    r = client.post("/audiences/", data=_form(
        dossier_id="parti", frequence="hebdomadaire", fin_mode="count",
        fin_count="4"))
    assert r.status_code == 200
    assert str(escape(NOT_FOUND)) in r.get_data(as_text=True)


def test_update_refuses_an_unresolvable_dossier(client, monkeypatch, bumps):
    monkeypatch.setattr(rh, "get_hearing", lambda hid: {
        "id": hid, "dossier_id": "d1", "title": "Audience"})
    monkeypatch.setattr(rh, "update_hearing", _forbid("update_hearing"))
    r = client.post("/audiences/h1", data=_form(dossier_id="parti"))
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert str(escape(NOT_FOUND)) in body
    assert bumps == [], "a refused edit must not touch any DAV collection"


def test_an_empty_dossier_is_still_general(client, monkeypatch, bumps):
    seen = {}

    def _create(data):
        seen.update(data)
        return {**data, "id": "h9"}, []

    monkeypatch.setattr(rh, "create_hearing", _create)
    r = client.post("/audiences/", data=_form(dossier_id=""))
    assert r.status_code == 302
    assert seen["dossier_id"] == ""
    assert seen["dossier_file_number"] == "" and seen["dossier_title"] == ""
    assert bumps == ["general"]


def test_a_resolvable_dossier_gets_its_labels(client, monkeypatch, bumps):
    seen = {}
    monkeypatch.setattr(rh, "get_hearing", lambda hid: {
        "id": hid, "dossier_id": "d1", "title": "Audience"})

    def _update(hid, data):
        seen.update(data)
        return {**data, "id": hid}, []

    monkeypatch.setattr(rh, "update_hearing", _update)
    r = client.post("/audiences/h1", data=_form(dossier_id="d1"))
    assert r.status_code == 302
    assert seen["dossier_file_number"] == "2026-001"
    assert seen["dossier_title"] == "Tremblay c. Lavoie"
    assert bumps == ["dossier:d1"]
