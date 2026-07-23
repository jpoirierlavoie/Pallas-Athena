"""Web-route tests for notes without a dossier (« Général »).

Two invariants that fail SILENTLY if broken: the CTag bump must name a
collection even when there is no dossier, and a dossier_id that does not
resolve must be an error rather than a quiet downgrade to « Général ».
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

from flask import Flask

with mock.patch("google.cloud.firestore.Client"):
    import routes.notes as notes_routes

UTC = timezone.utc
ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def bumps(monkeypatch):
    recorded = []
    monkeypatch.setattr(notes_routes, "bump_ctag", lambda n: recorded.append(n))
    return recorded


@pytest.fixture()
def tombstones(monkeypatch):
    recorded = {"record": [], "remove": []}
    monkeypatch.setattr(
        notes_routes, "record_tombstone",
        lambda n, r: recorded["record"].append((n, r)),
    )
    monkeypatch.setattr(
        notes_routes, "remove_tombstone",
        lambda n, r: recorded["remove"].append((n, r)),
    )
    return recorded


@pytest.fixture()
def client(monkeypatch):
    import json

    from markupsafe import Markup

    app = Flask(__name__, template_folder=os.path.join(ATHENA_DIR, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    # The note form uses these two app-level filters (registered in
    # main.create_app, which we do not build here).
    app.jinja_env.filters["jsattr"] = lambda v: Markup(
        json.dumps("" if v is None else str(v))
    )
    app.jinja_env.filters["markdown"] = lambda v: v
    app.jinja_env.globals["csp_nonce"] = "test-nonce"
    from security import csrf

    app.config["WTF_CSRF_ENABLED"] = False
    csrf.init_app(app)
    app.register_blueprint(notes_routes.notes_bp)
    test_client = app.test_client()
    with test_client.session_transaction() as sess:
        sess["user_id"] = "test-user"
        sess["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return test_client


def _post(client, **form):
    return client.post("/notes/", data={
        "title": "Veille", "content": "Corps", "category": "recherche", **form,
    })


def test_note_without_a_dossier_bumps_the_general_ctag(
    client, bumps, monkeypatch
):
    """The old code bumped only `if note.get("dossier_id")`. A general note
    would be written, shown in the app, and never reach the phone."""
    monkeypatch.setattr(
        notes_routes, "create_note",
        lambda data: ({"id": "n1", "dossier_id": data["dossier_id"]}, []),
    )
    resp = _post(client, dossier_id="")
    assert resp.status_code in (302, 303)
    assert bumps == ["general"]


def test_note_with_a_dossier_bumps_that_dossier(client, bumps, monkeypatch):
    monkeypatch.setattr(
        notes_routes, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001", "title": "T"},
    )
    monkeypatch.setattr(
        notes_routes, "create_note",
        lambda data: ({"id": "n1", "dossier_id": data["dossier_id"]}, []),
    )
    _post(client, dossier_id="d1")
    assert bumps == ["dossier:d1"]


def test_unknown_dossier_is_an_error_not_a_silent_general_note(
    client, bumps, monkeypatch
):
    """models/note._validate no longer requires a dossier, so blanking an
    unresolvable id would file the note under « Général » with no message."""
    monkeypatch.setattr(notes_routes, "get_dossier", lambda i: None)

    def _must_not_run(_data):
        raise AssertionError("wrote a note despite an unknown dossier_id")

    monkeypatch.setattr(notes_routes, "create_note", _must_not_run)
    resp = _post(client, dossier_id="inexistant")
    # Re-rendered form (200), never a redirect to a created note.
    assert resp.status_code == 200
    assert "Dossier introuvable" in resp.data.decode("utf-8")
    assert bumps == []


def test_enrich_distinguishes_absent_from_unresolvable(monkeypatch):
    monkeypatch.setattr(notes_routes, "get_dossier", lambda i: None)
    data, errors = notes_routes._enrich_dossier_info({"dossier_id": ""})
    assert errors == [] and data["dossier_id"] == ""      # legitimate: Général
    data, errors = notes_routes._enrich_dossier_info({"dossier_id": "nope"})
    assert errors and "introuvable" in errors[0].lower()  # never blanked
    assert data["dossier_id"] == "nope"


def test_note_delete_records_a_tombstone(client, bumps, tombstones, monkeypatch):
    """Deletions travel ONLY via tombstones (sync-collection reports live
    members + tombstones; an unmentioned href reads as 'unchanged'). A bare
    CTag bump left the deleted note on the phone forever."""
    monkeypatch.setattr(
        notes_routes, "get_note", lambda i: {"id": "n1", "dossier_id": "d1"}
    )
    monkeypatch.setattr(notes_routes, "delete_note", lambda i: (True, ""))
    resp = client.post("/notes/n1/delete", data={})
    assert resp.status_code in (302, 303)
    assert tombstones["record"] == [("dossier:d1", "n1")]
    assert bumps == ["dossier:d1"]


def test_note_move_tombstones_the_old_collection(
    client, bumps, tombstones, monkeypatch
):
    """Reassigning a note's dossier must tombstone + bump the OLD collection
    (the shape routes/tasks.py uses) — bumping only the new one leaves the
    stale copy under the old dossier in DavX5 indefinitely."""
    monkeypatch.setattr(
        notes_routes, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "title": "T",
                   "content": "C", "category": "recherche"},
    )
    monkeypatch.setattr(
        notes_routes, "get_dossier",
        lambda i: {"id": i, "file_number": "2026-002", "title": "B"},
    )
    monkeypatch.setattr(
        notes_routes, "update_note",
        lambda nid, data: ({"id": nid, **data}, []),
    )
    resp = client.post("/notes/n1", data={
        "title": "T", "content": "C", "category": "recherche",
        "dossier_id": "d2",
    })
    assert resp.status_code in (302, 303)
    assert tombstones["record"] == [("dossier:d1", "n1")]
    assert tombstones["remove"] == [("dossier:d2", "n1")]
    assert bumps == ["dossier:d1", "dossier:d2"]


def test_analyse_note_dossier_change_is_refused(
    client, bumps, tombstones, monkeypatch
):
    """The théorie de la cause is bound to its dossier: the form locks the
    picker, and the route refuses a hand-crafted POST that would move it
    (a moved analyse note is invisible in every app view)."""
    monkeypatch.setattr(
        notes_routes, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "is_analyse": True,
                   "title": "Théorie de la cause", "content": "C",
                   "category": "stratégie"},
    )
    monkeypatch.setattr(
        notes_routes, "get_dossier",
        lambda i: {"id": i, "file_number": "2026-002", "title": "B"},
    )

    def _must_not_run(nid, data):
        raise AssertionError("moved the analyse note")

    monkeypatch.setattr(notes_routes, "update_note", _must_not_run)
    resp = client.post("/notes/n1", data={
        "title": "Théorie de la cause", "content": "C",
        "category": "stratégie", "dossier_id": "d2",
    })
    assert resp.status_code == 200  # re-rendered form, no redirect
    assert "liée à son dossier" in resp.data.decode("utf-8")
    assert bumps == [] and tombstones["record"] == []
