"""La théorie de la cause — ses règles vivent dans le MODÈLE (lot 1a, L3).

Avant cette étape, la garde « la note d'analyse ne change pas de dossier »
n'existait que dans la route web ; le PUT DAV la contournait (une copie jtx
glissée dans une autre collection DÉPLAÇAIT l'analyse du juriste là où
aucune vue ne la liste), une mise à jour pouvait promouvoir une note
ordinaire en SECONDE note d'analyse, et la recherche de l'analyse sur les
chemins d'écriture échouait OUVERT. Tout est éprouvé ici contre le VRAI
client Firestore (``tests/_fake_firestore.py`` : seul le serveur est faux) :

1. la garde du modèle, sur tous les chemins — web, DAV PUT, appel direct ;
2. ``dateless`` : l'analyse reste sans date, une note ordinaire garde son
   aller-retour Note ↔ Journal ;
3. la révision d'un remplacement, écrite dans LA MÊME transaction ;
4. ``find_analyse_note_strict`` / ``ensure_analyse_note`` : échouer FERMÉ,
   ne créer qu'une fois, ne signaler une création que si elle a eu lieu ;
5. la suppression de l'analyse laisse un instantané — web et DAV ;
6. la route d'initialisation et la branche de création DAV.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
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
NID = "0c7e2f1a-2b3c-4d5e-8f60-718293a4b5c6"
REV = f"notes/{NID}/{revision_model.SUBCOLLECTION}"
STALE = [concurrency.STALE_ETAG_ERROR]


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model a
    path starts to read tomorrow cannot reach the mocked client."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Tremblay c. Lavoie", "status": "actif"})
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                              "title": "Gagnon c. Roy", "status": "actif"})
    for name in ("dossier:d1", "dossier:d2", "general"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"c0-{name}",
                                       "sync_token": f"c0-{name}"})
    return fake


def _analyse(nid: str = NID, dossier: str = "d1", **over) -> dict:
    doc = {
        **note_model._default_doc(),
        "id": nid, "dossier_id": dossier, "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "title": note_model.ANALYSE_TITLE,
        "content": note_model._ANALYSE_SEED, "category": "stratégie",
        "dateless": True, "is_analyse": True, "vjournal_uid": "uid-analyse",
        "created_at": DT, "updated_at": DT, "etag": "e0",
    }
    doc.update(over)
    return doc


def _ordinary(nid: str = "n-ord", **over) -> dict:
    doc = {
        **note_model._default_doc(),
        "id": nid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "title": "Recherche",
        "content": "Premier jet.", "category": "recherche",
        "vjournal_uid": f"uid-{nid}", "created_at": DT, "updated_at": DT,
        "etag": "o0",
    }
    doc.update(over)
    return doc


def _seed(fake, doc: dict) -> dict:
    fake.seed(f"notes/{doc['id']}", doc)
    return doc


def _ctag(fake, name: str) -> str:
    return fake.peek(f"dav_sync/{name}")["ctag"]


def _analyse_notes(fake, dossier: str = "d1") -> list[dict]:
    return [n for n in fake.peek_collection("notes").values()
            if n.get("dossier_id") == dossier and n.get("is_analyse")]


def _fail_note_queries(monkeypatch, fake, times: int = 10**6) -> list:
    """The next *times* queries on ``notes`` fail at the transport — a
    Firestore blip — while every other read stays healthy."""
    server = fake._fake_server
    real = server.run_query
    failures = []

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if sq.from_ and sq.from_[0].collection_id == "notes" and len(failures) < times:
            failures.append(1)
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    return failures


# ══════════════════════════════════════════════════════════════════════
# 1. La garde du modèle — un appel direct
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("target", ["d2", ""])
def test_the_analyse_note_never_leaves_its_dossier(fake, target):
    """THE defect: update_note merged any dossier_id — the guard lived only
    in the web route. Moved to another dossier or to « Général », the
    analysis vanishes from every app view."""
    _seed(fake, _analyse())
    before = fake.peek(f"notes/{NID}")
    note, errors = note_model.update_note(NID, {"dossier_id": target,
                                                "content": "Déplacée ?"})
    assert note is None
    assert errors == [note_model.ANALYSE_DOSSIER_LOCKED_ERROR]
    assert fake.peek(f"notes/{NID}") == before


def test_the_analyse_note_may_be_saved_within_its_own_dossier(fake):
    _seed(fake, _analyse())
    note, errors = note_model.update_note(NID, {"dossier_id": "d1",
                                                "content": "## Bloc A — x"})
    assert errors == [] and fake.peek(f"notes/{NID}")["content"] == "## Bloc A — x"


def test_an_update_never_demotes_the_analyse_note(fake):
    _seed(fake, _analyse())
    note_model.update_note(NID, {"is_analyse": False, "title": "Autre titre"})
    stored = fake.peek(f"notes/{NID}")
    assert stored["is_analyse"] is True and stored["title"] == "Autre titre"


def test_an_update_never_promotes_an_ordinary_note(fake):
    """A second is_analyse note in a dossier would silently shadow the
    lawyer's analysis (one-per-dossier singleton)."""
    _seed(fake, _analyse())
    _seed(fake, _ordinary())
    note_model.update_note("n-ord", {"is_analyse": True})
    assert fake.peek("notes/n-ord")["is_analyse"] is False
    assert len(_analyse_notes(fake)) == 1


def test_the_web_route_and_the_model_refuse_with_the_same_words():
    """Belt and braces: routes/notes.py keeps its own guard, and imports
    the model's message rather than restating it."""
    assert notes_routes.ANALYSE_DOSSIER_LOCKED_ERROR is (
        note_model.ANALYSE_DOSSIER_LOCKED_ERROR
    )


# ══════════════════════════════════════════════════════════════════════
# 2. dateless
# ══════════════════════════════════════════════════════════════════════


def test_the_analyse_note_stays_dateless(fake):
    _seed(fake, _analyse())
    note_model.update_note(NID, {"dateless": False})
    assert fake.peek(f"notes/{NID}")["dateless"] is True


def test_an_ordinary_note_keeps_its_note_journal_round_trip(fake):
    """The design popped `dateless` on EVERY note; the review caught it: a
    jtx Note turned into a dated Journal entry would have kept its old
    flag, and the next sync stripped the date the lawyer set."""
    _seed(fake, _ordinary(dateless=True))
    note_model.update_note("n-ord", {"dateless": False})
    assert fake.peek("notes/n-ord")["dateless"] is False
    note_model.update_note("n-ord", {"dateless": True})
    assert fake.peek("notes/n-ord")["dateless"] is True


# ══════════════════════════════════════════════════════════════════════
# 3. La révision d'un remplacement
# ══════════════════════════════════════════════════════════════════════


def test_a_replacement_keeps_the_replaced_content_in_the_same_commit(fake):
    stored = _seed(fake, _analyse())
    fake.reset_logs()
    note, errors = note_model.update_note(
        NID, {"content": "Réécrite."}, expected_etag="e0", revision="bloc:C",
    )
    assert errors == []
    revisions = fake.peek_collection(REV)
    assert list(revisions) == [note["_revision_id"]]
    snap = revisions[note["_revision_id"]]
    # The WHOLE previous content, whatever the field names.
    assert snap["previous_value"] == stored["content"]
    assert snap["field"] == "bloc:C"
    assert snap["previous_etag"] == "e0"
    assert snap["new_etag"] == fake.peek(f"notes/{NID}")["etag"] == note["etag"]
    # ONE commit carried the snapshot and the replacement.
    guarded = [c for c in fake.commits if c.transaction is not None]
    assert len(guarded) == 1
    assert {p for _, p in guarded[0].ops} == {
        f"notes/{NID}", f"{REV}/{note['_revision_id']}"}
    # The transient key is never stored.
    assert "_revision_id" not in fake.peek(f"notes/{NID}")


def test_the_revision_names_the_writer_from_the_provenance_context(fake):
    from models import provenance

    _seed(fake, _analyse())
    with provenance.writing_via("mcp", tool="edit_analyse"):
        note, _errors = note_model.update_note(
            NID, {"content": "Par le connecteur."}, expected_etag="e0",
            revision="content",
        )
    snap = fake.peek(f"{REV}/{note['_revision_id']}")
    assert snap["via"] == "mcp" and snap["tool"] == "edit_analyse"
    assert fake.peek(f"notes/{NID}")["updated_via"] == "mcp"


def test_a_stale_replacement_writes_neither_the_note_nor_a_revision(fake):
    _seed(fake, _analyse())
    before = fake.peek(f"notes/{NID}")
    note, errors = note_model.update_note(
        NID, {"content": "Réécrite."}, expected_etag="perimee",
        revision="content",
    )
    assert note is None and errors == STALE
    assert fake.peek(f"notes/{NID}") == before
    assert fake.peek_collection(REV) == {}


def test_a_rival_write_between_read_and_commit_leaves_no_revision(
    fake, monkeypatch
):
    _seed(fake, _analyse())
    real = note_model.get_note

    def racing(nid):
        doc = real(nid)
        fake.external_write(f"notes/{NID}", _analyse(content="Au téléphone.",
                                                     etag="e-rival"))
        return doc

    monkeypatch.setattr(note_model, "get_note", racing)
    note, errors = note_model.update_note(
        NID, {"content": "Réécrite."}, expected_etag="e0", revision="content",
    )
    assert note is None and errors == STALE
    assert fake.peek(f"notes/{NID}")["content"] == "Au téléphone."
    assert fake.peek_collection(REV) == {}


def test_an_unchanged_content_leaves_no_revision(fake):
    _seed(fake, _analyse())
    note, errors = note_model.update_note(
        NID, {"content": note_model._ANALYSE_SEED, "title": "Titre revu"},
        expected_etag="e0", revision="content",
    )
    assert errors == [] and "_revision_id" not in note
    assert fake.peek_collection(REV) == {}


@pytest.mark.parametrize("kwargs", [
    {"revision": "content"},                              # no etag
    {"revision": "titre", "expected_etag": "e0"},         # unknown field
    {"revision": revision_model.DELETE_FIELD, "expected_etag": "e0"},
])
def test_a_revision_on_the_unguarded_path_is_refused(fake, kwargs):
    """commit_document is atomic only with an etag: without one, the
    snapshot would be written first and could outlive a failed write."""
    _seed(fake, _analyse())
    before = fake.peek(f"notes/{NID}")
    note, errors = note_model.update_note(NID, {"content": "Réécrite."}, **kwargs)
    assert note is None and errors
    assert fake.peek(f"notes/{NID}") == before
    assert fake.peek_collection(REV) == {}


# ══════════════════════════════════════════════════════════════════════
# 4. find_analyse_note_strict / ensure_analyse_note
# ══════════════════════════════════════════════════════════════════════


def test_the_strict_lookup_answers_none_one_or_refuses(fake, monkeypatch):
    assert note_model.find_analyse_note_strict("d1") is None
    _seed(fake, _ordinary())
    assert note_model.find_analyse_note_strict("d1") is None
    _seed(fake, _analyse())
    assert note_model.find_analyse_note_strict("d1")["id"] == NID
    _seed(fake, _analyse(nid="n-bis"))
    with pytest.raises(note_model.AnalyseDuplicateError) as exc:
        note_model.find_analyse_note_strict("d1")
    assert exc.value.count == 2


def test_the_strict_lookup_raises_on_a_read_failure(fake, monkeypatch):
    """The fail-open get_analyse_note reads an outage as « none yet »."""
    _seed(fake, _analyse())
    _fail_note_queries(monkeypatch, fake)
    assert note_model.get_analyse_note("d1") is None       # the display reader
    with pytest.raises(note_model.AnalyseLookupError):
        note_model.find_analyse_note_strict("d1")


def test_ensure_creates_once_and_says_so(fake):
    note, errors, created = note_model.ensure_analyse_note("d1")
    assert errors == [] and created is True
    stored = fake.peek(f"notes/{note['id']}")
    assert stored["is_analyse"] is True and stored["dateless"] is True
    assert stored["content"] == note_model._ANALYSE_SEED
    assert stored["dossier_file_number"] == "2026-001"
    fake.reset_logs()
    again, errors, created = note_model.ensure_analyse_note("d1")
    assert errors == [] and created is False and again["id"] == note["id"]
    assert fake.commits == []
    assert len(_analyse_notes(fake)) == 1


def test_ensure_refuses_on_a_read_failure_and_writes_nothing(fake, monkeypatch):
    _fail_note_queries(monkeypatch, fake)
    note, errors, created = note_model.ensure_analyse_note("d1")
    assert note is None and created is False
    assert errors == [note_model.ANALYSE_READ_ERROR]
    assert fake.peek_collection("notes") == {}


def test_ensure_refuses_a_duplicate(fake):
    _seed(fake, _analyse())
    _seed(fake, _analyse(nid="n-bis"))
    note, errors, created = note_model.ensure_analyse_note("d1")
    assert note is None and created is False
    assert errors == [note_model.ANALYSE_DUPLICATE_ERROR]
    assert len(_analyse_notes(fake)) == 2


def test_ensure_refuses_an_unknown_dossier(fake):
    note, errors, created = note_model.ensure_analyse_note("inconnu")
    assert note is None and created is False and errors
    assert fake.peek_collection("notes") == {}


def test_create_analyse_note_keeps_its_two_member_shape(fake):
    note, errors = note_model.create_analyse_note("d1")
    assert errors == [] and note["is_analyse"] is True


# ══════════════════════════════════════════════════════════════════════
# 5. Supprimer l'analyse laisse un instantané
# ══════════════════════════════════════════════════════════════════════


def test_deleting_the_analyse_note_snapshots_it_in_the_same_commit(fake):
    stored = _seed(fake, _analyse())
    fake.reset_logs()
    ok, error = note_model.delete_note(NID)
    assert ok and error == ""
    assert fake.peek(f"notes/{NID}") is None
    revisions = list(fake.peek_collection(REV).values())
    assert len(revisions) == 1
    snap = revisions[0]
    assert snap["field"] == revision_model.DELETE_FIELD
    assert snap["previous_value"] == stored["content"]
    assert snap["previous_etag"] == "e0" and snap["new_etag"] == ""
    guarded = [c for c in fake.commits if c.transaction is not None]
    assert len(guarded) == 1
    assert ("delete", f"notes/{NID}") in guarded[0].ops


def test_a_delete_racing_an_edit_is_refused_and_nothing_is_lost(fake):
    """The snapshot must be of what disappears: an edit landing between the
    read and the commit refuses the delete — it never deletes the edit
    unsnapshotted."""
    _seed(fake, _analyse())

    def rival(info):
        if ("delete", f"notes/{NID}") in info.ops:
            remove()
            fake.external_write(f"notes/{NID}", _analyse(
                content="Édité au téléphone.", etag="e-rival"))

    remove = fake.add_commit_hook(rival)
    ok, error = note_model.delete_note(NID)
    assert not ok and error == concurrency.STALE_ETAG_ERROR
    assert fake.peek(f"notes/{NID}")["content"] == "Édité au téléphone."
    assert fake.peek_collection(REV) == {}


def test_an_ordinary_note_is_deleted_without_a_snapshot(fake):
    _seed(fake, _ordinary())
    ok, _error = note_model.delete_note("n-ord")
    assert ok and fake.peek("notes/n-ord") is None
    assert fake.peek_collection(f"notes/n-ord/{revision_model.SUBCOLLECTION}") == {}


# ══════════════════════════════════════════════════════════════════════
# Les bancs : DAV et web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def dav(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


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


def _vjournal(*, analyse: bool = True, dtstart: bool = False,
              description: str = "Texte revu au téléphone.") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jtx//FR",
             "BEGIN:VJOURNAL", "UID:uid-analyse", "DTSTAMP:20260926T120000Z",
             "CREATED:20260720T150000Z", "SUMMARY:Théorie de la cause",
             f"DESCRIPTION:{description}"]
    if dtstart:
        lines.append("DTSTART;VALUE=DATE:20260926")
    if analyse:
        lines.append("X-PALLAS-ANALYSE:true")
    lines += ["END:VJOURNAL", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _put(client, href: str, body: str):
    return client.put(href, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar"})


# ══════════════════════════════════════════════════════════════════════
# 1 bis. La garde, au PUT DAV
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("href", [f"/dav/dossier-d2/{NID}.ics",
                                  f"/dav/general/{NID}.ics"])
def test_a_dav_put_never_moves_the_analyse_note(fake, dav, href):
    """THE DAV defect: the PUT forces the URL's dossier onto the data, and
    update_note used to accept it — relocate_resource then tombstoned the
    note out of its own dossier. The model refuses; nothing moves."""
    _seed(fake, _analyse())
    before = fake.peek(f"notes/{NID}")
    old_ctag = _ctag(fake, "dossier:d1")
    resp = _put(dav, href, _vjournal())
    assert resp.status_code == 422
    assert fake.peek(f"notes/{NID}") == before
    assert fake.peek_collection("dav_sync/dossier:d1/tombstones") == {}
    assert _ctag(fake, "dossier:d1") == old_ctag


def test_a_dav_put_keeps_the_analyse_note_flagged_and_dateless(fake, dav):
    """A client that strips X-PALLAS-ANALYSE and adds a DTSTART (a jtx
    Note turned into a Journal entry) edits the text — and demotes nothing,
    dates nothing."""
    _seed(fake, _analyse())
    resp = _put(dav, f"/dav/dossier-d1/{NID}.ics",
                _vjournal(analyse=False, dtstart=True))
    assert resp.status_code == 204
    stored = fake.peek(f"notes/{NID}")
    assert stored["content"] == "Texte revu au téléphone."
    assert stored["is_analyse"] is True and stored["dateless"] is True
    assert "DTSTART" not in note_model.note_to_vjournal(stored)


def test_a_dav_put_never_promotes_an_ordinary_note(fake, dav):
    _seed(fake, _analyse())
    _seed(fake, _ordinary("n-ord"))
    resp = _put(dav, "/dav/dossier-d1/n-ord.ics", _vjournal(dtstart=True))
    assert resp.status_code == 204
    assert fake.peek("notes/n-ord")["is_analyse"] is False
    assert len(_analyse_notes(fake)) == 1


def test_an_ordinary_dateless_note_put_back_with_a_dtstart_is_dated(fake, dav):
    """The review's pin: the ordinary Note ↔ Journal round trip survives."""
    _seed(fake, _ordinary("n-ord", dateless=True))
    resp = _put(dav, "/dav/dossier-d1/n-ord.ics",
                _vjournal(analyse=False, dtstart=True))
    assert resp.status_code == 204
    assert fake.peek("notes/n-ord")["dateless"] is False


# ══════════════════════════════════════════════════════════════════════
# 5 bis. La suppression, par DAV et par le web
# ══════════════════════════════════════════════════════════════════════


def test_a_dav_delete_of_the_analyse_note_leaves_its_snapshot(fake, dav):
    _seed(fake, _analyse())
    resp = dav.delete(f"/dav/dossier-d1/{NID}.ics", headers=AUTH)
    assert resp.status_code == 204
    assert fake.peek(f"notes/{NID}") is None
    assert len(fake.peek_collection(REV)) == 1
    assert NID in fake.peek_collection("dav_sync/dossier:d1/tombstones")


def test_a_dav_delete_racing_an_edit_is_a_412(fake, dav):
    _seed(fake, _analyse())

    def rival(info):
        if ("delete", f"notes/{NID}") in info.ops:
            remove()
            fake.external_write(f"notes/{NID}", _analyse(
                content="Édité ailleurs.", etag="e-rival"))

    remove = fake.add_commit_hook(rival)
    resp = dav.delete(f"/dav/dossier-d1/{NID}.ics", headers=AUTH)
    assert resp.status_code == 412
    assert fake.peek(f"notes/{NID}")["content"] == "Édité ailleurs."
    assert fake.peek_collection("dav_sync/dossier:d1/tombstones") == {}


def test_a_web_delete_of_the_analyse_note_leaves_its_snapshot(fake, web):
    _seed(fake, _analyse())
    resp = web.post(f"/notes/{NID}/delete", data={})
    assert resp.status_code in (302, 303)
    assert fake.peek(f"notes/{NID}") is None
    assert len(fake.peek_collection(REV)) == 1


def test_a_refused_web_delete_comes_back_to_the_note_with_a_banner(fake, web):
    """It used to return silently to the list, the note still standing."""
    _seed(fake, _analyse())

    def rival(info):
        if ("delete", f"notes/{NID}") in info.ops:
            remove()
            fake.external_write(f"notes/{NID}", _analyse(
                content="Édité ailleurs.", etag="e-rival"))

    remove = fake.add_commit_hook(rival)
    resp = web.post(f"/notes/{NID}/delete", data={})
    assert resp.status_code in (302, 303)
    assert f"/notes/{NID}" in resp.headers["Location"]
    assert "suppression_erreur=modifiee" in resp.headers["Location"]
    page = web.get(resp.headers["Location"])
    assert page.status_code == 200
    assert "PAS été supprimée" in page.get_data(as_text=True)
    assert fake.peek(f"notes/{NID}") is not None


# ══════════════════════════════════════════════════════════════════════
# 6. L'initialisation web, et la création DAV
# ══════════════════════════════════════════════════════════════════════


def test_the_init_route_creates_once_and_bumps_once(fake, web):
    old = _ctag(fake, "dossier:d1")
    resp = web.post("/dossiers/d1/analyse/init")
    assert resp.status_code == 200
    assert len(_analyse_notes(fake)) == 1
    bumped = _ctag(fake, "dossier:d1")
    assert bumped != old
    resp = web.post("/dossiers/d1/analyse/init")
    assert resp.status_code == 200
    assert len(_analyse_notes(fake)) == 1
    assert _ctag(fake, "dossier:d1") == bumped          # found: no bump


def test_a_read_blip_during_init_neither_bumps_nor_creates(
    fake, web, monkeypatch
):
    """THE route defect: the fail-open pre-check read the blip as « no note
    yet », the model's own check then FOUND the note, and the route bumped
    the CTag for a creation that never happened (DavX5 re-synced for
    nothing). One strict lookup now answers — or refuses, visibly."""
    _seed(fake, _analyse())
    old = _ctag(fake, "dossier:d1")
    _fail_note_queries(monkeypatch, fake, times=1)
    resp = web.post("/dossiers/d1/analyse/init")
    assert resp.status_code == 200
    assert note_model.ANALYSE_READ_ERROR in resp.get_data(as_text=True)
    assert _ctag(fake, "dossier:d1") == old
    assert len(_analyse_notes(fake)) == 1


def test_init_on_a_duplicate_shows_the_refusal_not_an_empty_sheet(fake, web):
    _seed(fake, _analyse())
    _seed(fake, _analyse(nid="n-bis"))
    resp = web.post("/dossiers/d1/analyse/init")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "plusieurs notes" in body
    assert "Aucune théorie de la cause" not in body
    assert len(_analyse_notes(fake)) == 2


def test_init_on_an_unknown_dossier_answers_in_a_swappable_2xx(fake, web):
    """htmx swaps no 4xx: the old 404 fragment never showed."""
    resp = web.post("/dossiers/inconnu/analyse/init")
    assert resp.status_code == 200
    assert "Dossier introuvable" in resp.get_data(as_text=True)


def test_a_dav_create_carrying_the_flag_is_a_503_when_the_lookup_fails(
    fake, dav, monkeypatch
):
    """THE create defect: the fail-open lookup read the blip as « this
    dossier has no analyse note », kept the flag, and a jtx copy became a
    SECOND théorie de la cause over the filled one."""
    _seed(fake, _analyse())
    _fail_note_queries(monkeypatch, fake)
    resp = _put(dav, "/dav/dossier-d1/n-copie.ics", _vjournal())
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")
    assert fake.peek("notes/n-copie") is None
    assert len(_analyse_notes(fake)) == 1


def test_a_dav_created_copy_in_a_dossier_that_has_one_is_ordinary(fake, dav):
    _seed(fake, _analyse())
    resp = _put(dav, "/dav/dossier-d1/n-copie.ics", _vjournal())
    assert resp.status_code == 201
    assert fake.peek("notes/n-copie")["is_analyse"] is False
    assert len(_analyse_notes(fake)) == 1


def test_a_dav_created_copy_in_a_dossier_with_duplicates_is_ordinary(fake, dav):
    _seed(fake, _analyse())
    _seed(fake, _analyse(nid="n-bis"))
    resp = _put(dav, "/dav/dossier-d1/n-copie.ics", _vjournal())
    assert resp.status_code == 201
    assert fake.peek("notes/n-copie")["is_analyse"] is False


def test_a_dav_created_theorie_in_an_empty_dossier_keeps_the_flag(fake, dav):
    """The legitimate jtx move: the target dossier has no analysis yet."""
    resp = _put(dav, "/dav/dossier-d2/n-copie.ics", _vjournal())
    assert resp.status_code == 201
    assert fake.peek("notes/n-copie")["is_analyse"] is True
    assert fake.peek("notes/n-copie")["dateless"] is True
