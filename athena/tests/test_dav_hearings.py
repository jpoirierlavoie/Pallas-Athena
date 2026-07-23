"""DAV tests: per-dossier collections and « Général ».

The first DAV tests in the suite. DavX5 fails SILENTLY — a wrong component
set, a hearing served from two collections, or a missing CTag bump produces
no error anywhere, so these pin the invariants the deploy gate can actually
check. What they cannot check is the device itself; see the curl procedure
in CLAUDE.md.
"""

import os
import sys

# Parse server output with defusedxml, the same guard dav/xml_utils.py
# applies to inbound request bodies (Bandit B314). Nothing here builds XML,
# so xml.etree is not imported at all.
from defusedxml.ElementTree import fromstring as safe_fromstring
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask

with mock.patch("google.cloud.firestore.Client"):
    import dav.dossier_collections as dc
    import dav.sync as dav_sync
    import models.hearing as hearing_model

UTC = timezone.utc
CAL = "urn:ietf:params:xml:ns:caldav"
DAVNS = "DAV:"
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _hearing(hid="h1", dossier_id="d1", title="Audience Tremblay"):
    return {
        "id": hid,
        "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Tremblay c. Lavoie" if dossier_id else "",
        "title": title,
        "hearing_type": "audience",
        "start_datetime": datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        "end_datetime": datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        "all_day": False,
        "status": "confirmée",
        "reminder_minutes": 1440,
        "vevent_uid": f"uid-{hid}",
        "etag": f"etag-{hid}",
        "created_at": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
    }


# ══════════════════════════════════════════════════════════════════════
# Serialization — the mandatory stamps
# ══════════════════════════════════════════════════════════════════════

def test_vevent_carries_dtstamp_and_created():
    """DTSTAMP is mandatory (RFC 5545 §3.6.1) and CREATED is the jtx Board
    icalobject.created NOT-NULL trap. Both were missing; the Android
    calendar provider tolerated it, jtx would not."""
    ical = hearing_model.hearing_to_vevent(_hearing())
    assert "DTSTAMP:" in ical
    assert "CREATED:" in ical
    assert "UID:uid-h1" in ical


def test_vevent_stamps_survive_a_hearing_with_no_timestamps():
    """A legacy doc missing created_at/updated_at must not crash the
    serializer — DAV would 500 on the whole collection."""
    bare = _hearing()
    bare.pop("created_at")
    bare.pop("updated_at")
    ical = hearing_model.hearing_to_vevent(bare)
    assert "BEGIN:VEVENT" in ical


# ══════════════════════════════════════════════════════════════════════
# Collection membership
# ══════════════════════════════════════════════════════════════════════

def test_dav_href_follows_the_dossier_link():
    assert hearing_model.dav_href_for("d1", "h1") == "/dav/dossier-d1/h1.ics"
    assert hearing_model.dav_href_for("", "h1") == "/dav/general/h1.ics"


def test_collection_for_is_the_one_routing_rule():
    """Every write path routes its CTag bump through this. Tasks store None
    for "no dossier", notes and hearings "" — both must land in Général, or
    the item is written and never syncs."""
    assert dav_sync.collection_for("d1") == "dossier:d1"
    assert dav_sync.collection_for(None) == dav_sync.GENERAL_COLLECTION
    assert dav_sync.collection_for("") == dav_sync.GENERAL_COLLECTION
    assert dav_sync.GENERAL_COLLECTION == "general"


# ══════════════════════════════════════════════════════════════════════
# Component set — two advertisements that must not drift
# ══════════════════════════════════════════════════════════════════════

def test_component_set_includes_vevent():
    assert "VEVENT" in dc.DOSSIER_COMPONENTS
    assert set(dc.DOSSIER_COMPONENTS) == {"VEVENT", "VTODO", "VJOURNAL"}


def test_root_propfind_and_collection_propfind_advertise_the_same_set():
    """Root discovery promising a capability the collection then denies is a
    classic silent desync. Both sites must read the same constant."""
    import inspect

    import dav as dav_pkg

    root_src = inspect.getsource(dav_pkg)
    collection_src = inspect.getsource(dc._add_collection_props)
    # Neither may hard-code a component name of its own.
    assert "DOSSIER_COMPONENTS" in root_src
    assert "DOSSIER_COMPONENTS" in collection_src
    for literal in ('"VTODO"', '"VJOURNAL"', '"VEVENT"'):
        assert literal not in collection_src


def test_detect_component_type_handles_all_three():
    assert dc._detect_component_type("BEGIN:VEVENT\nEND:VEVENT") == "VEVENT"
    assert dc._detect_component_type("BEGIN:VTODO\nEND:VTODO") == "VTODO"
    assert dc._detect_component_type("BEGIN:VJOURNAL\n") == "VJOURNAL"
    assert dc._detect_component_type("BEGIN:VFREEBUSY\n") is None


# ══════════════════════════════════════════════════════════════════════
# calendar-query comp-filter
# ══════════════════════════════════════════════════════════════════════

def _query(*components):
    inner = "".join(f'<C:comp-filter name="{c}"/>' for c in components)
    return safe_fromstring(
        f'<C:calendar-query xmlns:C="{CAL}" xmlns:D="{DAVNS}"><D:prop/>'
        f'<C:filter><C:comp-filter name="VCALENDAR">{inner}</C:comp-filter>'
        f"</C:filter></C:calendar-query>"
    )


def test_requested_components_parses_the_filter():
    assert dc.requested_components(_query("VEVENT")) == {"VEVENT"}
    assert dc.requested_components(_query("VTODO", "VJOURNAL")) == {
        "VTODO", "VJOURNAL",
    }


def test_requested_components_degrades_to_unfiltered_not_to_empty():
    """An absent or unparseable filter must return everything, never
    nothing — an empty collection reads to the client as 'all deleted'."""
    assert dc.requested_components(None) is None
    assert dc.requested_components(_query()) is None
    no_filter = safe_fromstring(f'<C:calendar-query xmlns:C="{CAL}"/>')
    assert dc.requested_components(no_filter) is None


# ══════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def app(monkeypatch):
    """Flask app with the DAV blueprints and auth stubbed out."""
    monkeypatch.setattr(
        "dav.dav_auth._check_credentials", lambda u, p: True
    )
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    application = Flask(__name__)
    application.config["SECRET_KEY"] = "test-secret"
    application.register_blueprint(dc.dossier_dav_bp)
    return application


@pytest.fixture()
def linked_and_standalone(monkeypatch):
    """One dossier-linked hearing, one standalone."""
    linked = _hearing("h-linked", "d1")
    standalone = _hearing("h-solo", "", title="Rendez-vous")
    everything = [linked, standalone]

    def _list(dossier_id=None, **kwargs):
        if dossier_id:
            return [h for h in everything if h.get("dossier_id") == dossier_id]
        return list(everything)

    monkeypatch.setattr(dc, "list_hearings", _list)
    monkeypatch.setattr(
        dc, "get_hearing", lambda i: next((h for h in everything if h["id"] == i), None)
    )
    monkeypatch.setattr(dc, "list_tasks", lambda dossier_id=None: [])
    monkeypatch.setattr(dc, "list_notes", lambda dossier_id=None: [])
    monkeypatch.setattr(dc, "get_task", lambda i: None)
    monkeypatch.setattr(dc, "get_note", lambda i: None)
    monkeypatch.setattr(
        dc, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001",
                   "title": "Tremblay c. Lavoie", "status": "actif"} if i == "d1" else None,
    )
    for module in (dc,):
        monkeypatch.setattr(module, "get_ctag", lambda n: "ctag-1")
        monkeypatch.setattr(module, "get_sync_token", lambda n: "token-1")
        monkeypatch.setattr(module, "get_tombstones", lambda n: [])
    return linked, standalone


def _hrefs(payload: bytes) -> set[str]:
    root = safe_fromstring(payload)
    return {
        el.text for el in root.iter(f"{{{DAVNS}}}href") if el.text
    }


def test_general_collection_hides_dossier_linked_hearings(
    app, linked_and_standalone
):
    """Serving the same hearing from both collections makes DavX5 import the
    court date twice, and a write through one never bumps the other."""
    resp = app.test_client().open(
        "/dav/general/", method="PROPFIND", headers={**AUTH, "Depth": "1"}
    )
    assert resp.status_code == 207
    hrefs = _hrefs(resp.data)
    assert "/dav/general/h-solo.ics" in hrefs
    assert "/dav/general/h-linked.ics" not in hrefs


def test_dossier_collection_lists_its_hearings(app, linked_and_standalone):
    resp = app.test_client().open(
        "/dav/dossier-d1/", method="PROPFIND", headers={**AUTH, "Depth": "1"}
    )
    assert resp.status_code == 207
    assert "/dav/dossier-d1/h-linked.ics" in _hrefs(resp.data)


def test_general_collection_404s_a_dossier_linked_hearing(
    app, linked_and_standalone
):
    client = app.test_client()
    assert client.get("/dav/general/h-solo.ics", headers=AUTH).status_code == 200
    assert client.get("/dav/general/h-linked.ics", headers=AUTH).status_code == 404
    deleted = client.delete("/dav/general/h-linked.ics", headers=AUTH)
    assert deleted.status_code == 404


def test_dossier_collection_serves_the_hearing_as_vevent(
    app, linked_and_standalone
):
    resp = app.test_client().get("/dav/dossier-d1/h-linked.ics", headers=AUTH)
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "BEGIN:VEVENT" in body
    assert "DTSTAMP:" in body
    assert resp.headers["ETag"] == '"etag-h-linked"'


def test_collection_propfind_advertises_vevent(app, linked_and_standalone):
    resp = app.test_client().open(
        "/dav/dossier-d1/", method="PROPFIND", headers={**AUTH, "Depth": "0"}
    )
    names = {
        el.get("name")
        for el in safe_fromstring(resp.data).iter(f"{{{CAL}}}comp")
    }
    assert names == {"VEVENT", "VTODO", "VJOURNAL"}


def test_calendar_query_vevent_filter_returns_only_hearings(
    app, linked_and_standalone, monkeypatch
):
    """Without comp-filter support a VEVENT-scoped query hands the client
    every VTODO and VJOURNAL of the dossier too."""
    monkeypatch.setattr(
        dc, "list_tasks",
        lambda dossier_id=None: [{"id": "t1", "etag": "e", "title": "Tâche",
                                  "dossier_id": "d1", "status": "à_faire",
                                  "priority": "normale", "vtodo_uid": "u"}],
    )
    body = (
        f'<C:calendar-query xmlns:C="{CAL}" xmlns:D="{DAVNS}"><D:prop/>'
        f'<C:filter><C:comp-filter name="VCALENDAR">'
        f'<C:comp-filter name="VEVENT"/>'
        f"</C:comp-filter></C:filter></C:calendar-query>"
    )
    resp = app.test_client().open(
        "/dav/dossier-d1/", method="REPORT", headers=AUTH, data=body
    )
    assert resp.status_code == 207
    hrefs = _hrefs(resp.data)
    assert "/dav/dossier-d1/h-linked.ics" in hrefs
    assert "/dav/dossier-d1/t1.ics" not in hrefs


def test_calendar_query_without_filter_still_returns_everything(
    app, linked_and_standalone, monkeypatch
):
    monkeypatch.setattr(
        dc, "list_tasks",
        lambda dossier_id=None: [{"id": "t1", "etag": "e", "title": "Tâche",
                                  "dossier_id": "d1", "status": "à_faire",
                                  "priority": "normale", "vtodo_uid": "u"}],
    )
    body = f'<C:calendar-query xmlns:C="{CAL}"/>'
    resp = app.test_client().open(
        "/dav/dossier-d1/", method="REPORT", headers=AUTH, data=body
    )
    hrefs = _hrefs(resp.data)
    assert "/dav/dossier-d1/h-linked.ics" in hrefs
    assert "/dav/dossier-d1/t1.ics" in hrefs


def test_sync_collection_reports_hearings(app, linked_and_standalone):
    body = f'<D:sync-collection xmlns:D="{DAVNS}"><D:sync-token/></D:sync-collection>'
    resp = app.test_client().open(
        "/dav/dossier-d1/", method="REPORT", headers=AUTH, data=body
    )
    assert resp.status_code == 207
    assert "/dav/dossier-d1/h-linked.ics" in _hrefs(resp.data)


# ══════════════════════════════════════════════════════════════════════
# PUT — the URL decides the collection
# ══════════════════════════════════════════════════════════════════════

def test_put_forces_the_dossier_from_the_url(app, monkeypatch):
    """hearing_to_vevent emits X-PALLAS-DOSSIER-ID and vevent_to_hearing
    reads it back, so a round-tripped payload could otherwise claim a
    different dossier than the collection it was PUT into — and the CTag
    bump would then target the wrong collection."""
    seen = {}
    monkeypatch.setattr(dc, "get_hearing", lambda i: None)
    monkeypatch.setattr(dc, "get_task", lambda i: None)
    monkeypatch.setattr(dc, "get_note", lambda i: None)
    monkeypatch.setattr(
        dc, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001",
                   "title": "Tremblay", "status": "actif"},
    )
    monkeypatch.setattr(
        dc, "vevent_to_hearing",
        lambda s: {"title": "A", "dossier_id": "AUTRE-DOSSIER",
                   "start_datetime": datetime(2026, 9, 1, tzinfo=UTC)},
    )

    def _create(data):
        seen.update(data)
        return {**data, "etag": "new"}, []

    monkeypatch.setattr(dc, "create_hearing", _create)
    bumps = []
    monkeypatch.setattr(dc, "bump_ctag", lambda n: bumps.append(n))
    monkeypatch.setattr(dc, "remove_tombstone", lambda n, r: None)

    resp = app.test_client().put(
        "/dav/dossier-d1/h-new.ics",
        headers=AUTH,
        data="BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR",
    )
    assert resp.status_code == 201
    assert seen["dossier_id"] == "d1"          # URL wins, not the payload
    assert seen["id"] == "h-new"               # id comes from the URL
    assert bumps == ["dossier:d1"]


def test_create_hearing_honours_a_supplied_id_and_uid(monkeypatch):
    """A CalDAV PUT names the resource in its URL. Minting a fresh uuid
    stored the event under an id the client never learns: every later GET of
    that href 404s while a duplicate syncs down under another id."""
    written = {}

    class _Doc:
        def set(self, payload):
            written.update(payload)

    class _Coll:
        def document(self, doc_id):
            written["_doc_id"] = doc_id
            return _Doc()

    monkeypatch.setattr(
        hearing_model, "db", mock.Mock(collection=lambda name: _Coll())
    )
    created, errors = hearing_model.create_hearing({
        "id": "from-url",
        "vevent_uid": "client-uid",
        "title": "Audience",
        "start_datetime": datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
    })
    assert errors == []
    assert created["id"] == "from-url"
    assert created["vevent_uid"] == "client-uid"
    assert written["_doc_id"] == "from-url"
    assert created["dav_href"] == "/dav/general/from-url.ics"


def test_create_hearing_still_mints_an_id_when_none_given(monkeypatch):
    monkeypatch.setattr(
        hearing_model, "db",
        mock.Mock(collection=lambda name: mock.Mock(
            document=lambda i: mock.Mock(set=lambda p: None)
        )),
    )
    created, errors = hearing_model.create_hearing({
        "title": "Audience",
        "start_datetime": datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
        "dossier_id": "d1",
    })
    assert errors == []
    assert created["id"]
    assert created["vevent_uid"]
    assert created["dav_href"] == f"/dav/dossier-d1/{created['id']}.ics"


# ══════════════════════════════════════════════════════════════════════
# « Général » — the collection for everything without a dossier
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def general_members(monkeypatch):
    """One of each component, all dossier-less, plus one linked hearing."""
    solo_hearing = _hearing("h-solo", "", title="Rendez-vous")
    linked_hearing = _hearing("h-linked", "d1")
    solo_task = {"id": "t-solo", "dossier_id": None, "title": "Rappel",
                 "status": "à_faire", "priority": "normale", "etag": "e-t",
                 "vtodo_uid": "u-t", "category": "autre"}
    solo_note = {"id": "n-solo", "dossier_id": "", "title": "Veille",
                 "content": "Texte", "category": "recherche", "etag": "e-n",
                 "vjournal_uid": "u-n",
                 "created_at": datetime(2026, 7, 1, tzinfo=UTC),
                 "updated_at": datetime(2026, 7, 1, tzinfo=UTC)}

    monkeypatch.setattr(dc, "list_hearings",
                        lambda dossier_id=None, **k: (
                            [linked_hearing] if dossier_id
                            else [solo_hearing, linked_hearing]))
    monkeypatch.setattr(dc, "list_tasks",
                        lambda dossier_id=None, **k: [] if dossier_id else [solo_task])
    monkeypatch.setattr(dc, "list_notes",
                        lambda dossier_id=None, **k: [] if dossier_id else [solo_note])
    monkeypatch.setattr(dc, "get_hearing", lambda i: solo_hearing if i == "h-solo" else None)
    monkeypatch.setattr(dc, "get_task", lambda i: solo_task if i == "t-solo" else None)
    monkeypatch.setattr(dc, "get_note", lambda i: solo_note if i == "n-solo" else None)
    monkeypatch.setattr(dc, "get_ctag", lambda n: "ctag-g")
    monkeypatch.setattr(dc, "get_sync_token", lambda n: "token-g")
    monkeypatch.setattr(dc, "get_tombstones", lambda n: [])
    return solo_hearing, solo_task, solo_note


def test_general_lists_all_three_component_types(app, general_members):
    resp = app.test_client().open(
        "/dav/general/", method="PROPFIND", headers={**AUTH, "Depth": "1"}
    )
    assert resp.status_code == 207
    hrefs = _hrefs(resp.data)
    assert "/dav/general/h-solo.ics" in hrefs
    assert "/dav/general/t-solo.ics" in hrefs
    assert "/dav/general/n-solo.ics" in hrefs      # notes had NO home before
    # A dossier-linked item must never appear here.
    assert "/dav/general/h-linked.ics" not in hrefs


def test_general_advertises_the_same_component_set_as_a_dossier(
    app, general_members
):
    resp = app.test_client().open(
        "/dav/general/", method="PROPFIND", headers={**AUTH, "Depth": "0"}
    )
    names = {el.get("name") for el in safe_fromstring(resp.data).iter(f"{{{CAL}}}comp")}
    assert names == set(dc.DOSSIER_COMPONENTS)


def test_general_displayname_is_general(app, general_members):
    resp = app.test_client().open(
        "/dav/general/", method="PROPFIND", headers={**AUTH, "Depth": "0"}
    )
    names = [el.text for el in safe_fromstring(resp.data).iter(f"{{{DAVNS}}}displayname")]
    assert "Général" in names


def test_general_comp_filter_still_applies(app, general_members):
    body = (
        f'<C:calendar-query xmlns:C="{CAL}" xmlns:D="{DAVNS}"><D:prop/>'
        f'<C:filter><C:comp-filter name="VCALENDAR">'
        f'<C:comp-filter name="VJOURNAL"/>'
        f"</C:comp-filter></C:filter></C:calendar-query>"
    )
    resp = app.test_client().open(
        "/dav/general/", method="REPORT", headers=AUTH, data=body
    )
    hrefs = _hrefs(resp.data)
    assert "/dav/general/n-solo.ics" in hrefs
    assert "/dav/general/h-solo.ics" not in hrefs
    assert "/dav/general/t-solo.ics" not in hrefs


def test_general_serves_a_dossier_less_note_as_vjournal(app, general_members):
    resp = app.test_client().get("/dav/general/n-solo.ics", headers=AUTH)
    assert resp.status_code == 200
    assert "BEGIN:VJOURNAL" in resp.data.decode("utf-8")


def test_put_into_general_forces_an_empty_dossier(app, monkeypatch):
    """Symmetric to the dossier scope forcing its id: the URL decides, so a
    payload claiming a dossier must not drag the item out of Général."""
    seen = {}
    for name in ("get_hearing", "get_task", "get_note"):
        monkeypatch.setattr(dc, name, lambda i: None)
    monkeypatch.setattr(
        dc, "vevent_to_hearing",
        lambda s: {"title": "A", "dossier_id": "UN-DOSSIER",
                   "start_datetime": datetime(2026, 9, 1, tzinfo=UTC)},
    )
    monkeypatch.setattr(dc, "create_hearing",
                        lambda d: (seen.update(d) or ({**d, "etag": "e"}, [])))
    bumps = []
    monkeypatch.setattr(dc, "bump_ctag", lambda n: bumps.append(n))
    monkeypatch.setattr(dc, "remove_tombstone", lambda n, r: None)

    resp = app.test_client().put(
        "/dav/general/h-new.ics", headers=AUTH,
        data="BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR",
    )
    assert resp.status_code == 201
    assert seen["dossier_id"] == ""
    assert seen["dossier_file_number"] == ""   # never the pseudo-dossier label
    assert seen["dossier_title"] == ""
    assert bumps == ["general"]


def test_general_scope_is_never_drained(app, general_members):
    """A dossier collection empties when the file closes. Général has no
    lifecycle — its items must always be listed."""
    dossier, active = dc._resolve_scope("")
    assert active is True
    assert dc._is_general(dossier["id"])


# ══════════════════════════════════════════════════════════════════════
# Collection display names
# ══════════════════════════════════════════════════════════════════════

def test_display_name_is_the_short_reference():
    """DavX5 truncates a long label mid-word in its collection list and in
    the Android calendar name, so it stays « N/R : <numéro> »."""
    assert dc.collection_display_name(
        {"id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie"}
    ) == "N/R : 2026-001"
    assert dc.collection_display_name({"id": "", "file_number": "x"}) == "Général"


def test_display_name_falls_back_when_there_is_no_file_number():
    """A bare « N/R : » identifies nothing — show the title instead."""
    assert dc.collection_display_name(
        {"id": "d1", "file_number": "", "title": "Sans numéro"}
    ) == "Sans numéro"
    assert dc.collection_display_name(
        {"id": "d1", "file_number": "", "title": ""}
    ) == "Dossier"


def test_root_and_collection_agree_on_the_display_name(app, linked_and_standalone):
    """They used to build the string separately and had drifted — the root
    prefixed « Pallas Athena — » and the collection did not, so the label a
    client showed depended on which response it read last."""
    import inspect

    import dav as dav_pkg

    assert "collection_display_name" in inspect.getsource(dav_pkg)

    resp = app.test_client().open(
        "/dav/dossier-d1/", method="PROPFIND", headers={**AUTH, "Depth": "0"}
    )
    shown = [
        el.text for el in safe_fromstring(resp.data).iter(f"{{{DAVNS}}}displayname")
    ]
    assert shown == ["N/R : 2026-001"]
    assert not any("Pallas Athena" in (s or "") for s in shown)


def test_addressbook_display_name():
    from dav.carddav import ADDRESSBOOK_DISPLAY_NAME

    assert ADDRESSBOOK_DISPLAY_NAME == "Clients et parties impliqués"
    assert "Pallas Athena" not in ADDRESSBOOK_DISPLAY_NAME
