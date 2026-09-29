"""Two concurrent initializations of the théorie de la cause never FORK it
(finitions, robustness-4).

``ensure_analyse_note`` read ``find_analyse_note_strict``, found none, then
called ``create_note``, which minted a fresh uuid4 and ``set()`` it — no
transaction, no deterministic id. Two callers racing (the web « Ajouter une
théorie de la cause » and a connector ``edit_analyse`` init; two inits under
different keys) both saw « none » and both created. The duplicate was then
load-bearing: ``find_analyse_note_strict`` raises ``AnalyseDuplicateError``,
so every later replace / append / rewrite was refused until the lawyer
deleted one by hand.

The note is now created with ``create()`` at a DETERMINISTIC id
(``analyse_note_id`` — the system folders' uuid5 precedent): the second
caller's ``create()`` fails ``AlreadyExists`` and reads the note back,
``created=False``, no CTag bump. Real model, shared fake Firestore.
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
    import dav.sync as dav_sync
    from models import dossier as dossier_model
    from models import note as note_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, note_model, dossier_model, dav_sync)
    f.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif", "clients": [], "client_ids": [],
        "opposing_parties": [], "opposing_party_ids": [], "etag": "e-d1",
    })
    return f


def _analyses(fake) -> list[str]:
    return [i for i, n in fake.peek_collection("notes").items()
            if n.get("is_analyse")]


def test_a_second_initializer_that_raced_the_first_reads_it_back(fake, monkeypatch):
    first, errors, created = note_model.ensure_analyse_note("d1")
    assert errors == [] and created is True
    assert first["id"] == note_model.analyse_note_id("d1")

    # The second caller's existence check ran BEFORE the first created —
    # it saw « none » — and only then does it create.
    real = note_model.find_analyse_note_strict
    calls = {"n": 0}

    def stale_then_real(dossier_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(dossier_id)

    monkeypatch.setattr(note_model, "find_analyse_note_strict", stale_then_real)
    second, errors2, created2 = note_model.ensure_analyse_note("d1")

    assert errors2 == [] and created2 is False
    assert second["id"] == first["id"]
    assert _analyses(fake) == [first["id"]]          # ONE théorie, never two
    # …and every later edit still finds exactly one.
    monkeypatch.setattr(note_model, "find_analyse_note_strict", real)
    assert note_model.find_analyse_note_strict("d1")["id"] == first["id"]


def test_the_id_is_per_dossier_and_stable():
    a = note_model.analyse_note_id("d1")
    assert a == note_model.analyse_note_id("d1")
    assert a != note_model.analyse_note_id("d2")
    assert len(a) == 36


def test_a_legacy_analyse_note_under_a_uuid4_is_found_not_duplicated(fake):
    fake.seed("notes/legacy-1", {
        "id": "legacy-1", "dossier_id": "d1", "title": note_model.ANALYSE_TITLE,
        "content": "A.", "category": "stratégie", "is_analyse": True,
        "dateless": True, "etag": "e", "vjournal_uid": "u",
        "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
    })
    note, errors, created = note_model.ensure_analyse_note("d1")
    assert errors == [] and created is False and note["id"] == "legacy-1"
    assert _analyses(fake) == ["legacy-1"]


def test_the_web_init_drops_a_stale_tombstone_of_the_deterministic_id(
        fake, monkeypatch):
    """A théorie deleted then re-initialized comes back under the id its
    tombstone names: the web route removes it on creation."""
    from flask import Flask

    with mock.patch("google.cloud.firestore.Client"):
        import routes.dossiers as rd
    nid = note_model.analyse_note_id("d1")
    fake.seed("dav_sync/dossier:d1", {"ctag": "c", "sync_token": "c"})
    fake.seed(f"dav_sync/dossier:d1/tombstones/{nid}",
              {"deleted_at": datetime.now(UTC), "sync_token": "c"})
    monkeypatch.setattr(rd, "get_dossier", lambda i: {"id": "d1"} if i == "d1" else None)
    monkeypatch.setattr(rd, "render_template", lambda *a, **k: "ok")
    monkeypatch.setattr(rd, "_template_context", lambda: {})
    monkeypatch.setattr(rd, "url_for", lambda *a, **k: "/x")
    app = Flask(__name__)
    app.secret_key = "t"
    with app.test_request_context("/dossiers/d1/analyse/init", method="POST"):
        rd.dossier_analyse_init.__wrapped__("d1")
    assert fake.peek(f"notes/{nid}") is not None
    assert fake.peek(f"dav_sync/dossier:d1/tombstones/{nid}") is None
