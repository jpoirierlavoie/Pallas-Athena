"""DAV — la création et le déplacement des événements (VEVENT) et des notes
(VJOURNAL) au téléphone (lot 1a, étape L1).

Même classe que la réparation VTODO/CardDAV du lot 0b, reproduite ici
contre le VRAI client Firestore (``tests/_fake_firestore.py`` : seul le
serveur est faux) et la vraie route PUT/GET de ``dav/dossier_collections.py`` :

1. **La lecture qui échoue ouvert.** Le PUT décide « modifier » ou « créer »
   sur l'existence de la ressource, et la lisait par ``get_hearing`` /
   ``get_note``, qui rendent ``None`` sur une erreur de lecture. Une
   modification faite au téléphone pendant une panne passagère partait donc
   dans la branche de CRÉATION — où ``create_hearing`` / ``create_note``
   honoraient l'``id`` du corps et écrivaient par ``set()`` : le document
   stocké était REMPLACÉ en entier (lien de série, porte Bookings, clés
   graph_* : perdus). Le PUT lit désormais STRICTEMENT et répond 503.
2. **L'identifiant de création.** La ressource est rangée sous le nom de son
   URL et l'UID du client, par le mot-clé explicite ``dav_id`` et un
   ``create()`` qui refuse (412) au lieu d'écraser ; un nom inutilisable est
   refusé (400) ; un nom déjà tenu par un autre composant aussi (412).
3. **Le déplacement.** Une note PUT dans une autre collection ne posait
   AUCUNE pierre tombale dans l'ancienne : le téléphone gardait les deux
   copies. Tâches, notes et audiences passent par
   ``dav.sync.relocate_resource``, APRÈS l'écriture — un refus (422) ne
   tombstone plus une collection que la ressource n'a pas quittée.
4. **Le suffixe « Dossier: … » d'une tâche recréée.** Un déplacement jtx
   téléverse la tâche sous un NOUVEAU nom (la branche de création) avec la
   description qu'on lui a servie ; le suffixe est retiré là aussi.

DavX5 échoue en silence : ces tests épinglent ce que la porte de
déploiement peut vérifier ; le reste se vérifie au ``curl`` puis sur
l'appareil (CLAUDE.md, composant nº 2).
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    from models import hearing as hearing_model
    from models import note as note_model
    from models import task as task_model

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
RID = "7b1d2c3e-4f50-4a61-8b72-9c83d4e5f601"
HREF = f"/dav/dossier-d1/{RID}.ics"
SUFFIX_D1 = "Dossier: 2026-001 - Tremblay c. Lavoie"


def _ics(*component_lines: str, kind: str = "VEVENT") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DAVx5//FR",
             f"BEGIN:{kind}", *component_lines, f"END:{kind}",
             "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _vevent(uid: str = "phone-evt-uid", summary: str = "Rendez-vous",
            start: str = "20261015T130000Z",
            end: str = "20261015T140000Z") -> str:
    return _ics(f"UID:{uid}", "DTSTAMP:20260926T120000Z",
                "CREATED:20260926T120000Z", f"SUMMARY:{summary}",
                f"DTSTART:{start}", f"DTEND:{end}")


def _vjournal(uid: str = "phone-note-uid", summary: str = "Appel du client",
              description: str = "Il rappellera lundi.") -> str:
    return _ics(f"UID:{uid}", "DTSTAMP:20260926T120000Z",
                "CREATED:20260926T120000Z", "DTSTART:20260926T120000Z",
                f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
                kind="VJOURNAL")


def _vtodo(uid: str = "phone-todo-uid", summary: str = "Appeler le greffe") -> str:
    return _ics(f"UID:{uid}", "DTSTAMP:20260926T120000Z",
                "CREATED:20260926T120000Z", f"SUMMARY:{summary}",
                "STATUS:NEEDS-ACTION", kind="VTODO")


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model the
    DAV layer starts to read tomorrow cannot reach the mocked client."""
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


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def _put(client, href: str, body: str, **headers):
    return client.put(href, data=body.encode("utf-8"),
                      headers={**AUTH, "Content-Type": "text/calendar",
                               **headers})


def _fail_reads_of(monkeypatch, fake, path: str) -> None:
    """Every read naming *path* fails at the transport — a Firestore blip on
    that one document, everything else healthy (the test_dav_sync_ctag
    injection)."""
    server = fake._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if path in [server.doc_rel(n) for n in request["documents"]]:
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)


def _ctag(fake, name: str) -> str:
    return fake.peek(f"dav_sync/{name}")["ctag"]


def _tombstones(fake, name: str) -> set[str]:
    return set(fake.peek_collection(f"dav_sync/{name}/tombstones"))


# ══════════════════════════════════════════════════════════════════════
# 1. A failed read is a 503 — never a create that overwrites
# ══════════════════════════════════════════════════════════════════════

_STORED_HEARING = {
    "id": RID, "title": "Rencontre hebdomadaire", "hearing_type": "rencontre",
    "status": "confirmée", "all_day": False,
    "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
    "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC),
    "dossier_id": "d1", "dossier_file_number": "2026-001",
    "dossier_title": "Tremblay c. Lavoie", "vevent_uid": "phone-evt-uid",
    "serie_id": "serie-1", "serie_rule": {"freq": "hebdomadaire"},
    "source": "", "confirmation": "", "etag": "e0",
}

_STORED_NOTE = {
    "id": RID, "title": "Appel du client", "content": "Texte du juriste.",
    "category": "appel", "pinned": True, "dossier_id": "d1",
    "dossier_file_number": "2026-001", "dossier_title": "Tremblay c. Lavoie",
    "vjournal_uid": "phone-note-uid", "etag": "n0",
}


def test_a_phone_edit_during_a_read_failure_never_replaces_the_event(
    fake, client, monkeypatch
):
    """THE defect: on the old code the fail-open read routed this EDIT into
    the create branch, and create_hearing's set() replaced the stored
    occurrence — its series link gone, with a 201."""
    fake.seed(f"hearings/{RID}", dict(_STORED_HEARING))
    before = fake.peek(f"hearings/{RID}")
    _fail_reads_of(monkeypatch, fake, f"hearings/{RID}")
    resp = _put(client, HREF, _vevent(summary="Rencontre déplacée"))
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")
    assert fake.peek(f"hearings/{RID}") == before


def test_a_phone_edit_during_a_read_failure_never_replaces_the_note(
    fake, client, monkeypatch
):
    fake.seed(f"notes/{RID}", dict(_STORED_NOTE))
    before = fake.peek(f"notes/{RID}")
    _fail_reads_of(monkeypatch, fake, f"notes/{RID}")
    resp = _put(client, HREF, _vjournal(description="Remplacé ?"))
    assert resp.status_code == 503
    assert fake.peek(f"notes/{RID}") == before


def test_a_phone_task_edit_during_a_read_failure_is_a_503_not_a_412(
    fake, client, monkeypatch
):
    """The task creator already refused with create() (lot 0b), but that
    answered 412 to a legitimate edit; a strict read answers 503, which the
    client retries."""
    fake.seed(f"tasks/{RID}", {"id": RID, "title": "Existante",
                               "status": "à_faire", "vtodo_uid": "u0",
                               "dossier_id": "d1", "etag": "t0"})
    before = fake.peek(f"tasks/{RID}")
    _fail_reads_of(monkeypatch, fake, f"tasks/{RID}")
    resp = _put(client, HREF, _vtodo())
    assert resp.status_code == 503
    assert fake.peek(f"tasks/{RID}") == before


def test_a_failed_read_of_another_component_blocks_the_create(
    fake, client, monkeypatch
):
    """The cross-component check reads strictly too: an unreadable note
    under this name might be hidden by the event about to be created."""
    _fail_reads_of(monkeypatch, fake, f"notes/{RID}")
    resp = _put(client, HREF, _vevent())
    assert resp.status_code == 503
    assert fake.peek_collection("hearings") == {}


# ══════════════════════════════════════════════════════════════════════
# 2. The create keeps the URL id and the phone UID — through create()
# ══════════════════════════════════════════════════════════════════════


def test_a_phone_created_event_is_served_back_at_its_own_href(fake, client):
    resp = _put(client, HREF, _vevent())
    assert resp.status_code == 201
    stored = fake.peek(f"hearings/{RID}")
    assert stored["id"] == RID and stored["vevent_uid"] == "phone-evt-uid"
    assert stored["dossier_id"] == "d1"
    assert stored["hearing_type"] == "rencontre"
    assert list(fake.peek_collection("hearings")) == [RID]
    got = client.get(HREF, headers=AUTH)
    assert got.status_code == 200
    assert "UID:phone-evt-uid" in got.get_data(as_text=True)
    assert got.headers["ETag"] == resp.headers["ETag"]


def test_a_phone_created_note_is_served_back_at_its_own_href(fake, client):
    resp = _put(client, HREF, _vjournal())
    assert resp.status_code == 201
    stored = fake.peek(f"notes/{RID}")
    assert stored["id"] == RID and stored["vjournal_uid"] == "phone-note-uid"
    got = client.get(HREF, headers=AUTH)
    assert got.status_code == 200
    assert "UID:phone-note-uid" in got.get_data(as_text=True)
    assert got.headers["ETag"] == resp.headers["ETag"]


@pytest.mark.parametrize("collection, body, reader, stored", [
    ("hearings", _vevent(summary="Second"), "get_hearing_strict",
     _STORED_HEARING),
    ("notes", _vjournal(summary="Second"), "get_note_strict", _STORED_NOTE),
], ids=["vevent", "vjournal"])
def test_a_create_racing_a_stored_resource_is_refused_never_overwritten(
    fake, client, monkeypatch, collection, body, reader, stored
):
    """A racing PUT stores the resource between the read and the create:
    create() refuses and DAV answers 412."""
    fake.seed(f"{collection}/{RID}", dict(stored))
    before = fake.peek(f"{collection}/{RID}")
    monkeypatch.setattr(dc, reader, lambda i: None)     # the race
    resp = _put(client, HREF, body)
    assert resp.status_code == 412
    assert fake.peek(f"{collection}/{RID}") == before


@pytest.mark.parametrize("body, collection", [
    (_vevent(), "hearings"), (_vjournal(), "notes"),
], ids=["vevent", "vjournal"])
@pytest.mark.parametrize("bad", ["__reserved__", "x" * 129],
                         ids=["reserved", "too-long"])
def test_an_unusable_resource_name_is_refused_before_any_write(
    fake, client, body, collection, bad
):
    resp = _put(client, f"/dav/dossier-d1/{bad}.ics", body)
    assert resp.status_code == 400
    assert fake.peek_collection(collection) == {}


@pytest.mark.parametrize("held_by, body, collection", [
    ("tasks", _vevent(), "hearings"),
    ("notes", _vevent(), "hearings"),
    ("tasks", _vjournal(), "notes"),
    ("hearings", _vjournal(), "notes"),
], ids=["vevent-over-task", "vevent-over-note", "vjournal-over-task",
        "vjournal-over-hearing"])
def test_a_create_under_an_id_another_component_holds_is_refused(
    fake, client, held_by, body, collection
):
    """One href space per collection, and _resolve_resource tries the task,
    then the note, then the event: two components under one id hide one of
    them from every later GET."""
    fake.seed(f"{held_by}/{RID}", {"id": RID, "title": "Déjà là",
                                   "dossier_id": "d1", "etag": "x0"})
    resp = _put(client, HREF, body)
    assert resp.status_code == 412
    assert fake.peek_collection(collection) == {}


def test_create_note_without_a_dav_id_never_honours_a_caller_id(fake):
    """The web form, the connector and the analyse seed call create_note
    with a dict: an ``id``/``vjournal_uid`` in it must never pick (and,
    through set(), overwrite) the document."""
    fake.seed("notes/victim", {"id": "victim", "title": "Ne pas écraser",
                               "etag": "v0"})
    doc, errors = note_model.create_note({
        "id": "victim", "vjournal_uid": "forged", "title": "T",
        "content": "C", "category": "autre", "dossier_id": "d1"})
    assert errors == []
    assert doc["id"] != "victim" and doc["vjournal_uid"] != "forged"
    assert fake.peek("notes/victim")["title"] == "Ne pas écraser"


def test_create_note_keeps_a_vjournal_dtstart_as_its_creation_date(fake):
    """What create_note must still honour from a payload: the VJOURNAL's
    DTSTART (a dated journal entry), carried as created_at."""
    when = datetime(2026, 9, 1, 15, tzinfo=UTC)
    doc, errors = note_model.create_note(
        {"title": "T", "content": "C", "category": "autre",
         "created_at": when}, dav_id="n-1")
    assert errors == []
    assert fake.peek("notes/n-1")["created_at"] == when


# ══════════════════════════════════════════════════════════════════════
# 3. The move — relocate_resource, after the write
# ══════════════════════════════════════════════════════════════════════


def test_a_note_put_into_another_collection_tombstones_the_old_one(
    fake, client
):
    """THE defect: the note update branch bumped only the NEW collection —
    the old one never learned the note had left, and the phone kept both
    copies for ever."""
    fake.seed(f"notes/{RID}", dict(_STORED_NOTE))
    old_ctag = _ctag(fake, "dossier:d1")
    resp = _put(client, f"/dav/dossier-d2/{RID}.ics", _vjournal())
    assert resp.status_code == 204
    assert fake.peek(f"notes/{RID}")["dossier_id"] == "d2"
    assert RID in _tombstones(fake, "dossier:d1")
    assert _ctag(fake, "dossier:d1") != old_ctag
    assert RID not in _tombstones(fake, "dossier:d2")


def test_a_hearing_put_into_another_collection_moves_it(fake, client):
    fake.seed(f"hearings/{RID}", dict(_STORED_HEARING))
    fake.seed(f"dav_sync/dossier:d2/tombstones/{RID}",
              {"deleted_at": datetime(2026, 9, 20, tzinfo=UTC),
               "sync_token": "old"})
    resp = _put(client, f"/dav/dossier-d2/{RID}.ics", _vevent())
    assert resp.status_code == 204
    assert fake.peek(f"hearings/{RID}")["dossier_id"] == "d2"
    assert RID in _tombstones(fake, "dossier:d1")
    # The stale tombstone of a previous stay in d2 is removed: one REPORT
    # must never call the event both live and deleted.
    assert RID not in _tombstones(fake, "dossier:d2")


def test_a_refused_move_leaves_the_old_collection_untouched(fake, client):
    """The old code tombstoned and bumped the OLD collection BEFORE the
    update: a 422 left a tombstone for an event that never left it."""
    fake.seed(f"hearings/{RID}", dict(_STORED_HEARING))
    old_ctag = _ctag(fake, "dossier:d1")
    resp = _put(client, f"/dav/dossier-d2/{RID}.ics",
                _vevent(start="20261015T140000Z", end="20261015T130000Z"))
    assert resp.status_code == 422
    assert fake.peek(f"hearings/{RID}")["dossier_id"] == "d1"
    assert _tombstones(fake, "dossier:d1") == set()
    assert _ctag(fake, "dossier:d1") == old_ctag


def test_an_edit_in_place_bumps_one_collection_only(fake, client):
    fake.seed(f"notes/{RID}", dict(_STORED_NOTE))
    before = {n: _ctag(fake, n) for n in ("dossier:d1", "dossier:d2", "general")}
    assert _put(client, HREF, _vjournal()).status_code == 204
    after = {n: _ctag(fake, n) for n in before}
    assert [n for n in before if before[n] != after[n]] == ["dossier:d1"]
    assert _tombstones(fake, "dossier:d1") == set()


# ══════════════════════════════════════════════════════════════════════
# 4. The task's « Dossier: … » line, on the CREATE branch
# ══════════════════════════════════════════════════════════════════════


def _stored_task(fake, description: str) -> dict:
    doc, errors = task_model.create_task({
        "title": "Préparer la requête", "description": description,
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
    })
    assert errors == []
    return doc


def test_a_jtx_move_does_not_carry_the_old_dossier_line(fake, client):
    """A jtx move uploads the task to the target collection under a NEW
    name — the create branch — with the description it was served, « \\n\\n
    Dossier: 2026-001 - … » included. The old code stored that line as the
    lawyer's text."""
    doc = _stored_task(fake, "Rédiger le projet")
    served = client.get(f"/dav/dossier-d1/{doc['id']}.ics", headers=AUTH)
    body = served.get_data(as_text=True)
    assert SUFFIX_D1 in body.replace("\r\n ", "")
    new_id = "0e0f1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"
    resp = _put(client, f"/dav/dossier-d2/{new_id}.ics", body)
    assert resp.status_code == 201
    moved = fake.peek(f"tasks/{new_id}")
    assert moved["dossier_id"] == "d2"
    assert moved["description"] == "Rédiger le projet"


def test_a_created_task_keeps_a_lawyer_text_that_only_resembles_the_line(
    fake, client
):
    """Only the serializer's EXACT output is removed."""
    text = f"Voir la lettre ({SUFFIX_D1})"
    body = _ics("UID:u-lawyer", "DTSTAMP:20260926T120000Z",
                "SUMMARY:Lettre", f"DESCRIPTION:{text}",
                "X-PALLAS-DOSSIER-ID:d1", kind="VTODO")
    new_id = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    assert _put(client, f"/dav/dossier-d2/{new_id}.ics", body).status_code == 201
    assert fake.peek(f"tasks/{new_id}")["description"] == text


def test_a_created_task_from_an_unreadable_dossier_strips_nothing(
    fake, client, monkeypatch
):
    """The line is rebuilt from the dossier the resource was served from;
    when that dossier cannot be read the text is left whole — never guessed
    at."""
    escaped = f"Texte\\n\\n{SUFFIX_D1}"
    body = _ics("UID:u-x", "DTSTAMP:20260926T120000Z", "SUMMARY:T",
                f"DESCRIPTION:{escaped}", "X-PALLAS-DOSSIER-ID:d-disparu",
                kind="VTODO")
    new_id = "2b3c4d5e-6f70-4a8b-9c0d-1e2f3a4b5c6d"
    assert _put(client, f"/dav/dossier-d2/{new_id}.ics", body).status_code == 201
    assert fake.peek(f"tasks/{new_id}")["description"] == f"Texte\n\n{SUFFIX_D1}"
