"""The web update/delete routes take the OLD DAV collection from a read that
can be trusted (finitions, sync-4).

``routes/tasks``, ``routes/hearings`` and ``routes/notes`` captured the
record's current dossier with the fail-open ``get_task`` / ``get_hearing`` /
``get_note`` BEFORE calling the model, which re-read on its own. When the
route's read failed and the model's succeeded, the old dossier read ``None``:

* the DELETE tombstoned and bumped « Général » instead of the dossier's
  collection — the deleted task, note or court date stayed on the phone for
  good;
* the UPDATE's ``relocate_resource`` treated a real dossier-to-dossier move
  as one from « Général », never tombstoned the old collection, and left a
  duplicate copy on the phone.

Now the deletes tombstone the collection of the document the MODEL read and
deleted (``delete_*(deleted_out=)``), and the updates read the old dossier
STRICTLY and refuse the save when they cannot. Real routes, real models, the
shared fake Firestore; the blip is injected at the transport, on the FIRST
point read of the record only — exactly the window the old routes fell in.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

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
    import routes.notes as rn
    import routes.tasks as rt

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for d in ("d1", "d2"):
        fake.seed(f"dossiers/{d}", {
            "id": d, "file_number": f"2026-00{d[-1]}", "title": f"Dossier {d}",
            "status": "actif", "clients": [], "client_ids": [],
            "opposing_parties": [], "opposing_party_ids": [], "etag": f"e-{d}",
        })
    fake.seed("tasks/t1", {
        "id": "t1", "dossier_id": "d1", "title": "Tâche", "description": "",
        "status": "à_faire", "priority": "normale", "category": "autre",
        "etag": "e-t", "vtodo_uid": "u-t", "created_at": NOW, "updated_at": NOW,
    })
    fake.seed("notes/n1", {
        "id": "n1", "dossier_id": "d1", "title": "Note", "content": "C",
        "category": "recherche", "etag": "e-n", "vjournal_uid": "u-n",
        "created_at": NOW, "updated_at": NOW,
    })
    fake.seed("hearings/h1", {
        "id": "h1", "dossier_id": "d1", "title": "Audience",
        "hearing_type": "audience", "start_datetime": NOW,
        "end_datetime": NOW + timedelta(hours=1), "all_day": False,
        "status": "confirmée", "etag": "e-h", "vevent_uid": "u-h",
        "confirmation": "", "source": "", "modalite": "présentiel",
        "reminder_minutes": 1440, "created_at": NOW, "updated_at": NOW,
    })
    return fake


@pytest.fixture
def client():
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n")
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.jinja_env.filters["markdown"] = lambda v: v
    for bp in (rt.tasks_bp, rh.hearings_bp, rn.notes_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


def _fail_first_read_of(monkeypatch, db, path: str) -> None:
    """The FIRST point read of *path* fails at the transport; the next ones
    succeed — the blip the old route's pre-read fell in, while the model's
    own read (a separate round trip) worked."""
    server = db._fake_server
    real = server.batch_get_documents
    state = {"failed": False}

    def blip(request, metadata=None, **kwargs):
        rels = [server.doc_rel(n) for n in request["documents"]]
        if path in rels and not state["failed"]:
            state["failed"] = True
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", blip)


def _tombstones(db, collection: str) -> set:
    return set(db.peek_collection(f"dav_sync/{collection}/tombstones"))


@pytest.mark.parametrize("url, path, rid", [
    ("/taches/t1/delete", "tasks/t1", "t1"),
    ("/notes/n1/delete", "notes/n1", "n1"),
    ("/audiences/h1/delete", "hearings/h1", "h1"),
])
def test_a_delete_never_tombstones_general_for_a_dossier_s_record(
        db, client, monkeypatch, url, path, rid):
    _fail_first_read_of(monkeypatch, db, path)
    client.post(url, data={})
    assert rid not in _tombstones(db, "general")
    if db.peek(path) is None:
        # Deleted: the tombstone went to ITS collection.
        assert rid in _tombstones(db, "dossier:d1")
    else:
        # Refused on the failed read: nothing written anywhere.
        assert _tombstones(db, "dossier:d1") == set()


@pytest.mark.parametrize("url, path, rid", [
    ("/taches/t1/delete", "tasks/t1", "t1"),
    ("/notes/n1/delete", "notes/n1", "n1"),
    ("/audiences/h1/delete", "hearings/h1", "h1"),
])
def test_a_delete_tombstones_the_collection_of_what_the_model_deleted(
        db, client, url, path, rid):
    client.post(url, data={})
    assert db.peek(path) is None
    assert rid in _tombstones(db, "dossier:d1")
    assert rid not in _tombstones(db, "general")


def test_a_task_move_whose_old_dossier_cannot_be_read_is_refused(
        db, client, monkeypatch):
    _fail_first_read_of(monkeypatch, db, "tasks/t1")
    resp = client.post("/taches/t1", data={
        "title": "Tâche", "dossier_id": "d2", "priority": "normale",
        "status": "à_faire", "category": "autre", "expected_etag": "e-t"})
    stored = db.peek("tasks/t1")
    if stored["dossier_id"] == "d2":
        # Moved: then the OLD collection must have been tombstoned.
        assert "t1" in _tombstones(db, "dossier:d1")
    else:
        assert resp.status_code == 200
        assert str(escape("n'a pas pu être lu")) in resp.get_data(as_text=True)
        assert _tombstones(db, "dossier:d1") == set()
    assert "t1" not in _tombstones(db, "general")


def test_a_hearing_move_whose_old_dossier_cannot_be_read_is_refused(
        db, client, monkeypatch):
    _fail_first_read_of(monkeypatch, db, "hearings/h1")
    local = to_mtl(NOW)
    resp = client.post("/audiences/h1", data={
        "title": "Audience", "dossier_id": "d2", "hearing_type": "audience",
        "status": "confirmée", "start_date": local.strftime("%Y-%m-%d"),
        "start_time": local.strftime("%H:%M"), "end_time": "23:00",
        "reminder_minutes": "1440", "modalite": "présentiel",
        "expected_etag": "e-h"})
    stored = db.peek("hearings/h1")
    if stored["dossier_id"] == "d2":
        assert "h1" in _tombstones(db, "dossier:d1")
    else:
        assert resp.status_code == 200
        assert str(escape("n'a pas pu être lu")) in resp.get_data(as_text=True)
    assert "h1" not in _tombstones(db, "general")


def test_a_chain_delete_whose_hearing_cannot_be_read_refuses_on_a_2xx(
        db, client, monkeypatch):
    """The chain scope reads the STORED hearing strictly: unreadable, it
    refuses (a redirect carrying the banner) — never quietly deletes this
    one occurrence instead."""
    _fail_first_read_of(monkeypatch, db, "hearings/h1")
    resp = client.post("/audiences/h1/delete", data={"scope": "suivantes"})
    assert resp.status_code == 302
    assert "erreur=" in resp.headers["Location"]
    assert db.peek("hearings/h1") is not None


def test_a_note_edit_whose_note_cannot_be_read_keeps_the_lawyer_s_text(
        db, client, monkeypatch):
    """Review of the finitions (sync-4). note_update read the note fail-open
    and, on a blip, redirected to the LIST — no save, no word, the lawyer's
    edit gone. It now reads strictly and re-renders the form with the
    submitted text and the refusal; nothing is written, no collection
    touched."""
    _fail_first_read_of(monkeypatch, db, "notes/n1")
    resp = client.post("/notes/n1", data={
        "title": "Note", "content": "Texte que l'avocat vient de taper",
        "category": "recherche", "dossier_id": "d2", "expected_etag": "e-n"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert str(escape("n'a pas pu être lu")) in body
    assert str(escape("Texte que l'avocat vient de taper")) in body
    stored = db.peek("notes/n1")
    assert stored["dossier_id"] == "d1" and stored["content"] == "C"
    assert _tombstones(db, "dossier:d1") == set()
    assert "n1" not in _tombstones(db, "general")


def test_a_note_the_store_says_is_absent_still_goes_to_the_list(db, client):
    resp = client.post("/notes/absente", data={
        "title": "Note", "content": "C", "category": "recherche"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/notes/")
