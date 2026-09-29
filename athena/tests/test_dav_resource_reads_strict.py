"""A per-resource read that FAILED is never « not found » (finitions, sync-3).

Three shapes of the same defect, each closed here:

(a) the CalDAV collections resolved a resource — for GET, a resource
    PROPFIND, the small multiget and DELETE — through the fail-open
    ``get_task`` → ``get_note`` → ``get_hearing`` chain and fell through to
    404. DavX5 takes a 404 on DELETE as « already gone »: it drops its copy
    while the server keeps the record, which reappears at the collection's
    next change. It is a 503 + ``Retry-After`` now;
(b) CardDAV read every contact with the fail-open ``get_partie``: a phone
    EDIT sent with ``If-Match`` that met a blip was answered 412, a
    conflict — never the 503 a client retries. Strict now, like the
    calendar collections;
(c) even behind a strict handler read, ``update_hearing`` / ``update_task``
    / ``update_note`` / ``update_partie`` re-read with their fail-open
    getter, so a blip between the two reads came back « … introuvable » —
    DAV's 422 « Données invalides. », the connector's « introuvable ». They
    read strictly and answer ``concurrency.READ_UNAVAILABLE_ERROR``, which
    DAV maps to 503 and the connector to reason ``read_unavailable``.

Real handlers, real models, the shared fake Firestore; the failure is
injected at the transport (``batch_get_documents``), where a blip lands.
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

from flask import Flask  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.dossier_collections as dc
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.tools as tools
    from models import concurrency
    from models import dossier as dossier_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import task as task_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
D1 = "d0000000-0000-4000-8000-000000000001"
T1 = "t0000000-0000-4000-8000-000000000001"
N1 = "n0000000-0000-4000-8000-000000000001"
H1 = "h0000000-0000-4000-8000-000000000001"
P1 = "p0000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    fake = install(monkeypatch, dav_sync, dossier_model, hearing_model,
                   task_model, note_model, partie_model)
    for name in ("parties", "general", f"dossier:{D1}"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"c-{name}", "sync_token": f"c-{name}"})
    fake.seed(f"dossiers/{D1}", {
        "id": D1, "status": "actif", "file_number": "2026-001",
        "title": "Tremblay c. Lavoie", "clients": [], "client_ids": [],
        "opposing_parties": [], "opposing_party_ids": [], "etag": "e-d",
    })
    fake.seed(f"tasks/{T1}", {
        "id": T1, "dossier_id": D1, "title": "Tâche", "status": "à_faire",
        "priority": "normale", "category": "autre", "etag": "e-t",
        "vtodo_uid": "u-t", "created_at": NOW, "updated_at": NOW,
    })
    fake.seed(f"parties/{P1}", {
        "id": P1, "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay", "etag": "e-p",
        "vcard_uid": "u-p", "created_at": NOW, "updated_at": NOW,
    })
    return fake


def _client():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    app.register_blueprint(dc.dossier_dav_bp)
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def _fail_point_reads(monkeypatch, db, collection: str, *, after: int = 0):
    """Every point read of a *collection* document fails at the transport —
    after the first *after* of them succeeded (to fail the MODEL's read
    behind a handler read that worked)."""
    server = db._fake_server
    real = server.batch_get_documents
    seen = {"n": 0}

    def failing(request, metadata=None, **kwargs):
        rels = [db._fake_server.doc_rel(n) for n in request["documents"]]
        if any(r.startswith(f"{collection}/") for r in rels):
            seen["n"] += 1
            if seen["n"] > after:
                raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)


def _503(resp):
    assert resp.status_code == 503, (resp.status_code, resp.data)
    assert resp.headers["Retry-After"] == "30"


# ── (a) the calendar collections ─────────────────────────────────────────


@pytest.mark.parametrize("failing", ["tasks", "notes", "hearings"])
@pytest.mark.parametrize("method", ["GET", "PROPFIND", "DELETE"])
def test_a_failed_resource_read_is_503_never_404(db, monkeypatch, failing, method):
    """The resource is a TASK; any of the three component reads failing
    (the chain reads each until one matches) answers 503. The task is
    looked up first, so a failing note or hearing read matters only for a
    resource that is not a task — use an id that is none of the three."""
    client = _client()
    rid = T1 if failing == "tasks" else "x0000000-0000-4000-8000-00000000000x"
    _fail_point_reads(monkeypatch, db, failing)
    resp = client.open(f"/dav/dossier-{D1}/{rid}.ics", method=method,
                       headers={**AUTH, "Depth": "0"})
    _503(resp)
    assert db.peek(f"tasks/{T1}") is not None  # nothing deleted


def test_the_small_multiget_answers_503_never_a_404_per_href(db, monkeypatch):
    body = (f'<C:calendar-multiget xmlns:C="urn:ietf:params:xml:ns:caldav" '
            f'xmlns:D="DAV:"><D:prop><D:getetag/></D:prop>'
            f'<D:href>/dav/dossier-{D1}/{T1}.ics</D:href></C:calendar-multiget>')
    _fail_point_reads(monkeypatch, db, "tasks")
    _503(_client().open(f"/dav/dossier-{D1}/", method="REPORT",
                        headers=AUTH, data=body))


def test_an_absent_resource_is_still_a_404(db):
    """The fix changes the answer to a FAILED read only."""
    resp = _client().delete(f"/dav/dossier-{D1}/x0000000-0000-4000-8000-00000000000x.ics",
                            headers=AUTH)
    assert resp.status_code == 404


def test_a_deleted_task_is_still_deleted(db):
    resp = _client().delete(f"/dav/dossier-{D1}/{T1}.ics", headers=AUTH)
    assert resp.status_code == 204
    assert db.peek(f"tasks/{T1}") is None


# ── (b) the address book ──────────────────────────────────────────────────


def _vcard(uid: str = "u-p", given: str = "Jean") -> str:
    return ("BEGIN:VCARD\r\nVERSION:4.0\r\n"
            f"UID:{uid}\r\nFN:{given} Tremblay\r\nN:Tremblay;{given};;;\r\n"
            "CATEGORIES:client\r\nEND:VCARD\r\n")


@pytest.mark.parametrize("method, headers, data", [
    ("GET", {}, None),
    ("PROPFIND", {"Depth": "0"}, None),
    ("DELETE", {}, None),
    # A phone EDIT with If-Match: the fail-open read answered 412, a
    # « conflict », to a blip.
    ("PUT", {"If-Match": '"e-p"', "Content-Type": "text/vcard"}, _vcard(given="Jeanne")),
])
def test_a_failed_contact_read_is_503(db, monkeypatch, method, headers, data):
    _fail_point_reads(monkeypatch, db, "parties")
    resp = _client().open(f"/dav/addressbook/{P1}.vcf", method=method,
                          headers={**AUTH, **headers}, data=data)
    _503(resp)
    assert db.peek(f"parties/{P1}")["first_name"] == "Jean"


def test_the_addressbook_multiget_answers_503(db, monkeypatch):
    body = ('<C:addressbook-multiget xmlns:C="urn:ietf:params:xml:ns:carddav" '
            'xmlns:D="DAV:"><D:prop><D:getetag/></D:prop>'
            f'<D:href>/dav/addressbook/{P1}.vcf</D:href></C:addressbook-multiget>')
    _fail_point_reads(monkeypatch, db, "parties")
    _503(_client().open("/dav/addressbook/", method="REPORT", headers=AUTH, data=body))


# ── (c) the model's own re-read ───────────────────────────────────────────


@pytest.mark.parametrize("update, collection, doc_id, data", [
    (lambda i, d: task_model.update_task(i, d), "tasks", T1, {"title": "Autre"}),
    (lambda i, d: partie_model.update_partie(i, d), "parties", P1, {"notes": "x"}),
])
def test_an_update_whose_read_fails_says_so_never_introuvable(
        db, monkeypatch, update, collection, doc_id, data):
    before = db.peek(f"{collection}/{doc_id}")
    _fail_point_reads(monkeypatch, db, collection)
    doc, errors = update(doc_id, data)
    assert doc is None
    assert errors == [concurrency.READ_UNAVAILABLE_ERROR]
    assert not any("introuvable" in e for e in errors)
    assert db.peek(f"{collection}/{doc_id}") == before


def test_update_note_and_update_hearing_read_strictly_too(db, monkeypatch):
    db.seed(f"notes/{N1}", {"id": N1, "dossier_id": D1, "title": "N",
                            "content": "C", "category": "autre", "etag": "e-n",
                            "vjournal_uid": "u-n", "created_at": NOW,
                            "updated_at": NOW})
    db.seed(f"hearings/{H1}", {"id": H1, "dossier_id": D1, "title": "H",
                               "hearing_type": "audience",
                               "start_datetime": NOW, "end_datetime": NOW,
                               "all_day": False, "status": "confirmée",
                               "etag": "e-h", "vevent_uid": "u-h",
                               "confirmation": "", "source": ""})
    _fail_point_reads(monkeypatch, db, "notes")
    assert note_model.update_note(N1, {"title": "X"}) == (
        None, [concurrency.READ_UNAVAILABLE_ERROR])
    _fail_point_reads(monkeypatch, db, "hearings")
    assert hearing_model.update_hearing(H1, {"title": "X"}) == (
        None, [concurrency.READ_UNAVAILABLE_ERROR])


def _vtodo(summary: str) -> str:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//FR\r\n"
            "BEGIN:VTODO\r\n"
            f"UID:u-t\r\nDTSTAMP:20260920T160000Z\r\nCREATED:20260920T160000Z\r\n"
            f"SUMMARY:{summary}\r\nSTATUS:NEEDS-ACTION\r\n"
            "END:VTODO\r\nEND:VCALENDAR\r\n")


def test_a_phone_edit_whose_model_read_fails_is_503_never_422(db, monkeypatch):
    """The handler's strict read works; the MODEL's re-read fails. Before,
    « Tâche introuvable » became a 422 « Données invalides. » — a permanent
    refusal DavX5 does not retry."""
    _fail_point_reads(monkeypatch, db, "tasks", after=1)
    resp = _client().put(f"/dav/dossier-{D1}/{T1}.ics", headers=AUTH,
                         data=_vtodo("Revue au téléphone"),
                         content_type="text/calendar")
    _503(resp)
    assert db.peek(f"tasks/{T1}")["title"] == "Tâche"


def test_a_contact_edit_whose_model_read_fails_is_503(db, monkeypatch):
    _fail_point_reads(monkeypatch, db, "parties", after=1)
    resp = _client().put(f"/dav/addressbook/{P1}.vcf",
                         headers={**AUTH, "If-Match": '"e-p"'},
                         data=_vcard(given="Jeanne"), content_type="text/vcard")
    _503(resp)
    assert db.peek(f"parties/{P1}")["first_name"] == "Jean"


def test_the_connector_names_the_model_s_failed_read(db, monkeypatch):
    """Through _raise_if_stale, every connector write over update_* raises
    the model's failed-read refusal under reason read_unavailable — the
    stop-the-batch signal — never « introuvable »."""
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers._raise_if_stale(
            [concurrency.READ_UNAVAILABLE_ERROR], tool="update_task",
            subject="x", reread=lambda: None)
    assert excinfo.value.reason == "read_unavailable"
    assert "introuvable" not in str(excinfo.value)
    # A stale refusal is still the stale one.
    with pytest.raises(tools.ToolArgumentError) as stale:
        handlers._raise_if_stale(
            [concurrency.STALE_ETAG_ERROR], tool="update_task",
            subject="Cette tâche a été modifiée", reread=lambda: None)
    assert stale.value.reason == "stale_etag"
