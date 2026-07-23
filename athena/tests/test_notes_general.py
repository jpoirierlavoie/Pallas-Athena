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
