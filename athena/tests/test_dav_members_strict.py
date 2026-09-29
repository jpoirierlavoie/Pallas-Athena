"""A DAV collection whose MEMBERS could not be read answers 503 — never an
empty 207 (finitions, sync-1).

The member listing every CalDAV collection and the address book share
(PROPFIND Depth:1, sync-collection, calendar-query, the bulk multiget,
addressbook-query) read through the fail-open ``list_hearings`` /
``list_tasks`` / ``list_notes`` / ``list_parties``, each answering a
Firestore blip with ``[]``. An empty list is a well-formed EMPTY 207, and
DavX5 acts on it: its calendar sync (the default past-event limit) lists the
collection by calendar-query whenever the CTag moved and DELETES every local
event the server did not list — the dossier's court dates gone from the
phone until the collection's next write — while a sync-collection answer,
carrying the new token, moves the client past changes (and tombstones) it
never received.

Everything here runs the REAL handlers, the REAL strict readers and the
REAL ``dav.sync`` over the shared fake Firestore; the failure is injected at
the transport (``run_query``), where a real blip lands.
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
    from models import dossier as dossier_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import task as task_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}
CAL = "urn:ietf:params:xml:ns:caldav"
CARD = "urn:ietf:params:xml:ns:carddav"
D1 = "d0000000-0000-4000-8000-000000000001"


def _hearing(hid: str, dossier_id: str) -> dict:
    start = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
    return {
        "id": hid, "dossier_id": dossier_id, "title": "Audience",
        "hearing_type": "audience", "start_datetime": start,
        "end_datetime": start.replace(hour=15), "all_day": False,
        "status": "confirmée", "reminder_minutes": 1440, "etag": f"e-{hid}",
        "vevent_uid": f"u-{hid}", "confirmation": "", "source": "",
        "created_at": start, "updated_at": start,
    }


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    fake = install(monkeypatch, dav_sync, dossier_model, hearing_model,
                   task_model, note_model, partie_model)
    for name in ("parties", "general", f"dossier:{D1}"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"ctag-{name}",
                                       "sync_token": f"ctag-{name}"})
    fake.seed(f"dossiers/{D1}", {
        "id": D1, "status": "actif", "file_number": "2026-001",
        "title": "Tremblay c. Lavoie", "clients": [], "client_ids": [],
        "opposing_parties": [], "opposing_party_ids": [], "etag": "e",
    })
    fake.seed("hearings/h-d1", _hearing("h-d1", D1))
    fake.seed("hearings/h-gen", _hearing("h-gen", ""))
    fake.seed("tasks/t-d1", {"id": "t-d1", "dossier_id": D1, "title": "Tâche",
                             "status": "à_faire", "priority": "normale",
                             "etag": "e-t", "vtodo_uid": "u-t"})
    fake.seed("parties/p1", {"id": "p1", "type": "individual",
                             "contact_role": "client", "first_name": "Jean",
                             "last_name": "Tremblay", "etag": "e-p",
                             "vcard_uid": "u-p"})
    return fake


def _client():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    app.register_blueprint(dc.dossier_dav_bp)
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def _fail_queries_on(monkeypatch, db, collection: str) -> None:
    """Every QUERY on *collection* fails at the transport — the blip."""
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _sync_body(token: str = "") -> str:
    return (f'<D:sync-collection xmlns:D="DAV:"><D:sync-token>{token}'
            '</D:sync-token><D:prop><D:getetag/></D:prop></D:sync-collection>')


def _query_body() -> str:
    return (f'<C:calendar-query xmlns:C="{CAL}" xmlns:D="DAV:"><D:prop/>'
            f'<C:filter><C:comp-filter name="VCALENDAR">'
            f'<C:comp-filter name="VEVENT"/></C:comp-filter></C:filter>'
            f'</C:calendar-query>')


def _multiget_body(scope: str, ids) -> str:
    hrefs = "".join(f"<D:href>{scope}{i}.ics</D:href>" for i in ids)
    return (f'<C:calendar-multiget xmlns:C="{CAL}" xmlns:D="DAV:">'
            f'<D:prop><D:getetag/></D:prop>{hrefs}</C:calendar-multiget>')


_CALDAV_CASES = {
    "propfind": ("PROPFIND", {"Depth": "1"}, None),
    "sync-collection": ("REPORT", {}, _sync_body()),
    "calendar-query": ("REPORT", {}, _query_body()),
}


@pytest.mark.parametrize("scope", [f"/dav/dossier-{D1}/", "/dav/general/"])
@pytest.mark.parametrize("case", sorted(_CALDAV_CASES))
@pytest.mark.parametrize("failing", ["hearings", "tasks", "notes"])
def test_a_collection_whose_members_cannot_be_read_answers_503(
        db, monkeypatch, scope, case, failing):
    method, headers, body = _CALDAV_CASES[case]
    client = _client()
    ok = client.open(scope, method=method, headers={**AUTH, **headers}, data=body)
    assert ok.status_code == 207  # the store answering: a real listing

    _fail_queries_on(monkeypatch, db, failing)
    resp = client.open(scope, method=method, headers={**AUTH, **headers}, data=body)

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    assert b"multistatus" not in resp.data


@pytest.mark.parametrize("scope", [f"/dav/dossier-{D1}/", "/dav/general/"])
def test_the_bulk_multiget_answers_503_never_a_404_per_href(db, monkeypatch, scope):
    """Past the bulk threshold the multiget resolves from the listing; an
    empty listing would answer every href 404 — « deleted » to DavX5."""
    ids = ["h-d1", "h-gen", "t-d1", "x", "y"]
    _fail_queries_on(monkeypatch, db, "hearings")
    resp = _client().open(scope, method="REPORT", headers=AUTH,
                          data=_multiget_body(scope, ids))
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"


def test_a_sync_collection_whose_tombstones_cannot_be_read_answers_503(
        db, monkeypatch):
    """The answer carries the CURRENT token: a deletion the failed read
    omitted would be skipped by that client for good."""
    db.seed(f"dav_sync/dossier:{D1}/tombstones/t-gone",
            {"deleted_at": datetime.now(UTC), "sync_token": "x"})
    client = _client()
    ok = client.open(f"/dav/dossier-{D1}/", method="REPORT", headers=AUTH,
                     data=_sync_body())
    assert ok.status_code == 207 and b"t-gone" in ok.data

    _fail_queries_on(monkeypatch, db, "tombstones")
    resp = client.open(f"/dav/dossier-{D1}/", method="REPORT", headers=AUTH,
                       data=_sync_body())
    assert resp.status_code == 503
    assert b"sync-token" not in resp.data


def test_the_legacy_fail_open_tombstone_reader_still_degrades(db, monkeypatch):
    _fail_queries_on(monkeypatch, db, "tombstones")
    assert dav_sync.get_tombstones(f"dossier:{D1}") == []
    with pytest.raises(Exception):
        dav_sync.get_tombstones_strict(f"dossier:{D1}")


_CARDDAV_CASES = {
    "propfind": ("PROPFIND", {"Depth": "1"}, None),
    "sync-collection": ("REPORT", {}, _sync_body()),
    "addressbook-query": ("REPORT", {},
                          f'<C:addressbook-query xmlns:C="{CARD}" xmlns:D="DAV:">'
                          f'<D:prop><D:getetag/></D:prop></C:addressbook-query>'),
}


@pytest.mark.parametrize("case", sorted(_CARDDAV_CASES))
def test_an_address_book_that_cannot_be_read_answers_503(db, monkeypatch, case):
    method, headers, body = _CARDDAV_CASES[case]
    client = _client()
    ok = client.open("/dav/addressbook/", method=method,
                     headers={**AUTH, **headers}, data=body)
    assert ok.status_code == 207 and b"p1.vcf" in ok.data

    _fail_queries_on(monkeypatch, db, "parties")
    resp = client.open("/dav/addressbook/", method=method,
                       headers={**AUTH, **headers}, data=body)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"


def test_the_address_book_sync_answers_503_when_its_tombstones_fail(db, monkeypatch):
    _fail_queries_on(monkeypatch, db, "tombstones")
    resp = _client().open("/dav/addressbook/", method="REPORT", headers=AUTH,
                          data=_sync_body())
    assert resp.status_code == 503


def test_the_general_readers_list_only_what_belongs_to_no_dossier(db):
    assert [h["id"] for h in
            hearing_model.list_hearings_without_dossier_strict(
                include_unconfirmed=False)] == ["h-gen"]
    assert task_model.list_tasks_without_dossier_strict() == []
    db.seed("tasks/t-gen", {"id": "t-gen", "dossier_id": None, "title": "x",
                            "status": "à_faire", "etag": "e"})
    assert [t["id"] for t in task_model.list_tasks_without_dossier_strict()] == ["t-gen"]


def test_a_dav_collection_reads_its_members_only_through_strict_readers():
    """The tripwire: the fail-open readers must never come back into the
    DAV member path (their ``[]`` IS the empty 207)."""
    import ast
    import inspect

    for module, banned in ((dc, {"list_hearings", "list_tasks", "list_notes",
                                 "get_tombstones"}),
                           (carddav, {"list_parties", "get_tombstones"})):
        tree = ast.parse(inspect.getsource(module))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not (names & banned), (module.__name__, names & banned)
