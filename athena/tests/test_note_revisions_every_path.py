"""D17 (2026-09-27) — tout remplacement du contenu d'une note garde une révision.

Jusqu'au lot 1b, seuls les outils du connecteur demandaient l'instantané
(``update_note(revision=…)``) : le formulaire web et le PUT DAV du téléphone
remplaçaient le texte sans rien en garder. L'avocat a tranché : QUEL QUE
SOIT le chemin — application, téléphone, connecteur —, le texte remplacé est
conservé write-once dans ``notes/{id}/revisions/``, dans la même
transaction que l'écriture. Un contenu inchangé n'en laisse aucune.

Épinglé ici, contre le VRAI client Firestore (``tests/_fake_firestore.py``) :

1. le formulaire web : une révision quand le contenu change, aucune sinon ;
2. le PUT DAV : de même ;
3. un appelant qui n'a nommé aucune version (PUT DAV, page ancienne) garde
   le « dernier écrit gagne » — mais l'instantané est celui du texte
   RÉELLEMENT écrasé, même quand une écriture rivale s'intercale ;
4. le plafond de l'instantané couvre le plafond du contenu ;
5. la page de la note dit « Versions précédentes : N », lecture seule, et
   ne dit rien quand le compte est illisible ;
6. les textes du connecteur le disent — INSTRUCTIONS, écran de
   consentement, descriptions d'outils.
"""

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
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import mcp.endpoint as mcp_endpoint
    import mcp.tools as mcp_tools
    from models import concurrency
    from models import note as note_model
    from models import revision as revision_model
    import routes.dossiers as dossiers_routes
    import routes.notes as notes_routes
    import routes.tasks as tasks_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
NID = "5d0f2c1a-7b3c-4d5e-8f60-718293a4b5c6"
REV = f"notes/{NID}/{revision_model.SUBCOLLECTION}"


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so the revision
    module (and any a path starts to read tomorrow) shares the fake."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Tremblay c. Lavoie", "status": "actif"})
    for name in ("dossier:d1", "general"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"c0-{name}",
                                       "sync_token": f"c0-{name}"})
    return fake


def _note(**over) -> dict:
    doc = {
        **note_model._default_doc(),
        "id": NID, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "title": "Recherche",
        "content": "Premier jet.", "category": "recherche",
        "vjournal_uid": "uid-note", "created_at": DT, "updated_at": DT,
        "etag": "o0",
    }
    doc.update(over)
    return doc


def _seed(fake, **over) -> dict:
    doc = _note(**over)
    fake.seed(f"notes/{NID}", doc)
    return doc


def _revisions(fake) -> list[dict]:
    return list(fake.peek_collection(REV).values())


@pytest.fixture
def web(fake):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, jsattr=lambda v: v,
                                 markdown=markdown_to_safe_html)
    app.register_blueprint(notes_routes.notes_bp)
    app.register_blueprint(dossiers_routes.dossiers_bp)
    app.register_blueprint(tasks_routes.tasks_bp)   # the note page links to it
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u1"
        sess["user_email"] = "test@example.com"
        sess["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


@pytest.fixture
def dav(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def _form(content: str, **over) -> dict:
    data = {"dossier_id": "d1", "title": "Recherche", "content": content,
            "category": "recherche", "expected_etag": "o0"}
    data.update(over)
    return data


def _vjournal(description: str, summary: str = "Recherche") -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jtx//FR",
             "BEGIN:VJOURNAL", "UID:uid-note", "DTSTAMP:20260926T120000Z",
             "CREATED:20260720T150000Z", "DTSTART;VALUE=DATE:20260720",
             f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
             "X-PALLAS-NOTE-CATEGORY:recherche",
             "END:VJOURNAL", "END:VCALENDAR", ""]
    return "\r\n".join(lines).encode("utf-8")


def _put(client, body: bytes):
    return client.put(f"/dav/dossier-d1/{NID}.ics", data=body,
                      headers={**AUTH, "Content-Type": "text/calendar"})


# ══════════════════════════════════════════════════════════════════════
# 1. Le formulaire web
# ══════════════════════════════════════════════════════════════════════


def test_a_web_save_that_changes_the_content_keeps_one_revision(fake, web):
    _seed(fake)
    resp = web.post(f"/notes/{NID}", data=_form("Second jet."))
    assert resp.status_code == 302
    stored = fake.peek(f"notes/{NID}")
    assert stored["content"] == "Second jet."
    (rev,) = _revisions(fake)
    assert rev["previous_value"] == "Premier jet."
    assert rev["field"] == "content"
    assert rev["previous_etag"] == "o0" and rev["new_etag"] == stored["etag"]
    assert rev["via"] == "web"


def test_a_web_save_that_keeps_the_content_leaves_no_revision(fake, web):
    _seed(fake)
    resp = web.post(f"/notes/{NID}", data=_form("Premier jet.",
                                                title="Titre revu"))
    assert resp.status_code == 302
    assert fake.peek(f"notes/{NID}")["title"] == "Titre revu"
    assert _revisions(fake) == []


def test_a_web_page_older_than_its_etag_field_still_keeps_the_revision(
    fake, web,
):
    """A form posted without ``expected_etag`` (a page rendered before the
    field existed) asserts no version — and still loses nothing."""
    _seed(fake)
    data = _form("Second jet.")
    del data["expected_etag"]
    assert web.post(f"/notes/{NID}", data=data).status_code == 302
    (rev,) = _revisions(fake)
    assert rev["previous_value"] == "Premier jet."


def test_a_stale_web_save_writes_neither_the_note_nor_a_revision(fake, web):
    _seed(fake, etag="o-rival", content="Au téléphone.")
    resp = web.post(f"/notes/{NID}", data=_form("Second jet."))
    assert resp.status_code == 200          # the conflict re-render
    assert fake.peek(f"notes/{NID}")["content"] == "Au téléphone."
    assert _revisions(fake) == []


# ══════════════════════════════════════════════════════════════════════
# 2. Le PUT DAV (le téléphone)
# ══════════════════════════════════════════════════════════════════════


def test_a_dav_put_that_changes_the_content_keeps_one_revision(fake, dav):
    _seed(fake)
    resp = _put(dav, _vjournal("Revu au téléphone."))
    assert resp.status_code == 204
    stored = fake.peek(f"notes/{NID}")
    assert stored["content"] == "Revu au téléphone."
    (rev,) = _revisions(fake)
    assert rev["previous_value"] == "Premier jet."
    assert rev["via"] == "dav" and rev["new_etag"] == stored["etag"]
    assert resp.headers["ETag"] == f'"{stored["etag"]}"'


def test_a_dav_put_that_keeps_the_content_leaves_no_revision(fake, dav):
    _seed(fake)
    resp = _put(dav, _vjournal("Premier jet.", summary="Titre du téléphone"))
    assert resp.status_code == 204
    assert fake.peek(f"notes/{NID}")["title"] == "Titre du téléphone"
    assert _revisions(fake) == []


def test_two_phone_edits_keep_two_revisions_in_order(fake, dav):
    _seed(fake)
    assert _put(dav, _vjournal("Deuxième.")).status_code == 204
    assert _put(dav, _vjournal("Troisième.")).status_code == 204
    kept = sorted(r["previous_value"] for r in _revisions(fake))
    assert kept == ["Deuxième.", "Premier jet."]


# ══════════════════════════════════════════════════════════════════════
# 3. Aucune version nommée : le dernier écrit gagne, rien ne se perd
# ══════════════════════════════════════════════════════════════════════


def test_a_rival_write_before_an_unguarded_commit_is_what_gets_snapshotted(
    fake, monkeypatch,
):
    """The model guards the commit on the version it read, for the
    revision. A write landing in between makes that commit lose: the model
    reads again and re-merges — the caller named no version, so it is never
    refused — and the snapshot is the RIVAL's text, the one actually
    overwritten, never the stale read."""
    _seed(fake)
    real = note_model.get_note
    calls = []

    def racing(nid):
        doc = real(nid)
        calls.append(1)
        if len(calls) == 1:
            fake.external_write(f"notes/{NID}", _note(
                content="Au téléphone.", etag="o-rival"))
        return doc

    monkeypatch.setattr(note_model, "get_note", racing)
    note, errors = note_model.update_note(NID, {"content": "Second jet."})
    assert errors == [] and len(calls) == 2
    assert fake.peek(f"notes/{NID}")["content"] == "Second jet."
    (rev,) = _revisions(fake)
    assert rev["previous_value"] == "Au téléphone."
    assert rev["previous_etag"] == "o-rival"


def test_an_unguarded_caller_that_keeps_losing_is_refused_not_looped(
    fake, monkeypatch,
):
    _seed(fake)
    real = note_model.get_note
    counter = iter(range(100))

    def always_racing(nid):
        doc = real(nid)
        fake.external_write(f"notes/{NID}", _note(
            content="Rival.", etag=f"o-rival-{next(counter)}"))
        return doc

    monkeypatch.setattr(note_model, "get_note", always_racing)
    note, errors = note_model.update_note(NID, {"content": "Second jet."})
    assert note is None and errors == [concurrency.STALE_ETAG_ERROR]
    assert _revisions(fake) == []
    assert fake.peek(f"notes/{NID}")["content"] == "Rival."


def test_a_named_version_is_still_refused_rather_than_retried(
    fake, monkeypatch,
):
    """The retry is for a caller that asserted nothing. One that named a
    version asked to be refused if it moved — the connector's tools, a
    current web form."""
    _seed(fake)
    real = note_model.get_note

    def racing(nid):
        doc = real(nid)
        fake.external_write(f"notes/{NID}", _note(
            content="Au téléphone.", etag="o-rival"))
        return doc

    monkeypatch.setattr(note_model, "get_note", racing)
    note, errors = note_model.update_note(NID, {"content": "Second jet."},
                                          expected_etag="o0")
    assert note is None and errors == [concurrency.STALE_ETAG_ERROR]
    assert _revisions(fake) == []


# ══════════════════════════════════════════════════════════════════════
# 4. Le plafond de l'instantané couvre celui du contenu
# ══════════════════════════════════════════════════════════════════════


def test_the_snapshot_cap_covers_the_content_cap():
    """A note is sanitized at CONTENT_MAX_LENGTH on every write, so the
    content any edit replaces is at most that long: were the snapshot cap
    below it, a full-size note would become uneditable — on every path
    now, the web and the phone included."""
    assert revision_model.MAX_SNAPSHOT_CHARS >= note_model.CONTENT_MAX_LENGTH


def test_a_full_size_note_is_replaced_with_its_revision(fake):
    full = "é" * note_model.CONTENT_MAX_LENGTH
    _seed(fake, content=full)
    note, errors = note_model.update_note(NID, {"content": "Court."})
    assert errors == []
    (rev,) = _revisions(fake)
    assert rev["previous_value"] == full
    assert rev["previous_length"] == note_model.CONTENT_MAX_LENGTH


# ══════════════════════════════════════════════════════════════════════
# 5. « Versions précédentes : N » sur la page de la note
# ══════════════════════════════════════════════════════════════════════


def test_the_note_page_counts_the_previous_versions(fake, web):
    _seed(fake)
    page = web.get(f"/notes/{NID}").get_data(as_text=True)
    assert "Versions précédentes" not in page
    note_model.update_note(NID, {"content": "Deuxième."})
    note_model.update_note(NID, {"content": "Troisième."})
    page = web.get(f"/notes/{NID}").get_data(as_text=True)
    assert "Versions précédentes&nbsp;: 2" in page


def test_an_unreadable_count_prints_nothing_rather_than_zero(
    fake, web, monkeypatch,
):
    _seed(fake)
    note_model.update_note(NID, {"content": "Deuxième."})

    def failing(*_a, **_k):
        raise RuntimeError("injected aggregation failure")

    monkeypatch.setattr(fake._fake_server, "run_aggregation_query", failing)
    assert revision_model.count_revisions("notes", NID) is None
    resp = web.get(f"/notes/{NID}")
    assert resp.status_code == 200
    assert "Versions précédentes" not in resp.get_data(as_text=True)


def test_the_count_needs_no_document_read(fake):
    _seed(fake)
    note_model.update_note(NID, {"content": "Deuxième."})
    fake.reset_logs()
    assert revision_model.count_revisions("notes", NID) == 1
    # ONE aggregation RPC — no document get, no streamed query.
    assert [r.rpc for r in fake.reads] == ["run_aggregation_query"]
    assert revision_model.count_revisions("dossiers", "d1") is None


# ══════════════════════════════════════════════════════════════════════
# 6. Les textes du connecteur le disent
# ══════════════════════════════════════════════════════════════════════


def test_every_surface_says_the_history_is_kept_whatever_the_path():
    """Before D17 the history was the connector's alone, and nothing said
    otherwise because nothing said anything: a caller could believe that a
    note edited on the phone had lost its text. The three surfaces now say
    it — and none may claim the revision is the connector's alone."""
    instructions = mcp_endpoint.INSTRUCTIONS
    assert ("whatever replaced it (the app, the phone, an append or an "
            "edit here)") in instructions
    consent = (_ATHENA / "templates" / "mcp" / "families"
               / "_agenda.html").read_text(encoding="utf-8")
    assert "sur votre téléphone ou par un ajout" in consent
    append = mcp_tools.TOOLS["append_to_note"]["description"]
    assert "kept in its revision history" in append
    update = mcp_tools.TOOLS["update_note"]["description"]
    assert "in the app or on the phone too" in update
    for text in (instructions, consent, append, update):
        lowered = text.lower()
        assert "only the connector" not in lowered
        assert "seul le connecteur" not in lowered
