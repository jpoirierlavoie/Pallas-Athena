"""Routes des tâches — le dossier introuvable (lot 1a, L4).

``_enrich_dossier_info`` BLANCHISSAIT un ``dossier_id`` qui ne se résout pas
(``strict=False``, ``blank_value=None``) : le modèle accepte une tâche sans
dossier, donc une tâche de dossier — souvent une échéance — passait en
silence dans « Général », hors de l'onglet du dossier et dans une autre
collection DAV sur le téléphone, derrière une redirection de succès. Elle
est désormais REFUSÉE, re-rendue à 200 (htmx n'échange que les 2xx), comme
les notes et les audiences. Un champ VIDE reste « aucun dossier », stocké
``None`` — la valeur des tâches pour cela.

Tout passe par la vraie route, le vrai gabarit et le vrai modèle, au-dessus
du faux Firestore partagé (seul le serveur est faux) : on relit ce qui est
STOCKÉ.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from markupsafe import escape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import routes.tasks as tasks_routes
    from models import dossier as dossier_model
    from models import task as task_model

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
NOT_FOUND = (
    "Dossier introuvable. Choisissez un dossier existant, ou laissez le "
    "champ vide pour classer la tâche dans « Général »."
)


@pytest.fixture
def db(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    return install(monkeypatch, *modules)


@pytest.fixture
def bumps(monkeypatch):
    """Every DAV primitive the routes can reach, through the route's own
    names AND through dav.sync (relocate_resource looks them up there)."""
    seen = []
    for module in (tasks_routes, dav_sync):
        for name, fn in (("bump_ctag", seen.append),
                         ("record_tombstone", lambda *a: seen.append(a)),
                         ("remove_tombstone", lambda *a: seen.append(a))):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fn)
    return seen


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, jsattr=lambda v: v)
    app.register_blueprint(tasks_routes.tasks_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


@pytest.fixture
def dossier_id(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return doc["id"]


def _form(dossier_id, **over) -> dict:
    base = {"dossier_id": dossier_id, "dossier_display": "2026-001",
            "title": "Déposer la réponse", "description": "Premier",
            "priority": "normale", "status": "à_faire", "category": "autre"}
    base.update(over)
    return base


def _tasks(db) -> dict:
    return db.peek_collection("tasks")


# ── Création ────────────────────────────────────────────────────────────


def test_create_refuses_an_unresolvable_dossier(client, db, bumps):
    """THE defect: the old route blanked the id and created the task under
    « Général » with a success redirect."""
    resp = client.post("/taches/", data=_form("parti"))
    assert resp.status_code == 200
    assert str(escape(NOT_FOUND)) in resp.get_data(as_text=True)
    assert _tasks(db) == {}
    assert bumps == []


def test_create_with_a_real_dossier_files_it_there(client, db, bumps,
                                                   dossier_id):
    resp = client.post("/taches/", data=_form(dossier_id))
    assert resp.status_code == 302
    (stored,) = _tasks(db).values()
    assert stored["dossier_id"] == dossier_id
    assert stored["dossier_file_number"] == "2026-001"
    assert stored["dossier_title"] == "Tremblay c. Lavoie"
    assert bumps == [f"dossier:{dossier_id}"]


def test_an_empty_dossier_is_still_general_and_stored_none(client, db, bumps):
    """Tasks keep None for « no dossier » — the strict helper keeps the
    empty string as-is, and the route turns it back into None."""
    resp = client.post("/taches/", data=_form(""))
    assert resp.status_code == 302
    (stored,) = _tasks(db).values()
    assert stored["dossier_id"] is None
    assert stored["dossier_file_number"] == "" and stored["dossier_title"] == ""
    assert bumps == ["general"]


# ── Modification ────────────────────────────────────────────────────────


def _seed(db, dossier_id) -> dict:
    doc, errors = task_model.create_task({
        "dossier_id": dossier_id, "title": "Déposer la réponse",
        "description": "Premier"})
    assert errors == [], errors
    return doc


def test_update_refuses_an_unresolvable_dossier(client, db, bumps, dossier_id):
    task = _seed(db, dossier_id)
    before = db.peek(f"tasks/{task['id']}")
    resp = client.post(f"/taches/{task['id']}", data={
        **_form("parti", description="Changé"),
        "expected_etag": task["etag"]})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert str(escape(NOT_FOUND)) in html
    # Nothing written: the task stays in its dossier, on the phone too.
    assert db.peek(f"tasks/{task['id']}") == before
    assert bumps == [], "a refused edit must not touch any DAV collection"
    # The re-render keeps protecting the retry with the version it read.
    assert f'name="expected_etag" value="{task["etag"]}"' in html


def test_update_to_general_is_still_a_deliberate_move(client, db, bumps,
                                                      dossier_id):
    task = _seed(db, dossier_id)
    resp = client.post(f"/taches/{task['id']}", data={
        **_form(""), "expected_etag": task["etag"]})
    assert resp.status_code == 302
    stored = db.peek(f"tasks/{task['id']}")
    assert stored["dossier_id"] is None
    # The move tombstones the old collection and un-tombstones « Général ».
    assert (f"dossier:{dossier_id}", task["id"]) in bumps
    assert "general" in bumps
