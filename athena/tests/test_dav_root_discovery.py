"""La découverte DavX5 — le PROPFIND Depth:1 sur ``/dav/`` (correctifs du
lot 4).

La racine liste les collections que DavX5 doit synchroniser : le carnet, la
collection « Général » et UNE collection par dossier actif. Jusqu'aux
correctifs du lot 4, elle bâtissait la liste des dossiers par
``list_dossiers``, qui échoue OUVERT : une panne Firestore répondait « aucun
dossier », la découverte annonçait ZÉRO collection de dossier dans un 207
bien formé — et DavX5, qui lit une collection absente de la découverte comme
une collection disparue, pouvait les retirer toutes du téléphone. Pire, la
même lecture avalait une exception de MIGRATION : un seul document de
dossier illisible vidait la liste entière de son statut.

Deux règles, désormais, épinglées ici sur le VRAI modèle au-dessus du faux
Firestore partagé (``tests/_fake_firestore.py``) :

* STRICTE sur la REQUÊTE — une lecture qui échoue répond 503 avec
  ``Retry-After`` (la réponse de la collection d'un dossier sur une lecture
  stricte échouée depuis le lot 1a, ``dav.dossier_collections.
  _read_unavailable``), jamais une liste partielle ni vide ;
* TOLÉRANTE par DOCUMENT — un document de dossier que la racine ne sait pas
  lire est sauté, avec une ligne typée (``log_unexpected``, l'identifiant
  seulement), et les autres sont listés.

Et le cas ordinaire rend EXACTEMENT le XML d'avant, épinglé octet pour octet
(capturé sur ce même magasin avant le correctif).
"""

import logging
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
    import dav as dav_pkg
    import dav.sync as dav_sync
    from models import dossier as dossier_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc

D_OLD = "d0000000-0000-4000-8000-000000000001"
D_NEW = "d0000000-0000-4000-8000-000000000002"
D_PENDING = "d0000000-0000-4000-8000-000000000003"
D_CLOSED = "d0000000-0000-4000-8000-000000000004"
D_BAD_PARTY = "d0000000-0000-4000-8000-000000000005"
D_BAD_NUMBER = "d0000000-0000-4000-8000-000000000006"
D_BAD_ID = "d0000000-0000-4000-8000-000000000007"
D_NO_ID = "d0000000-0000-4000-8000-000000000008"
D_BAD_DATE = "d0000000-0000-4000-8000-000000000009"


def _dossier(did, status, file_number, opened, **extra) -> dict:
    return {
        "id": did, "status": status, "file_number": file_number,
        "title": "Tremblay c. Lavoie",
        "opened_date": datetime(2026, 1, opened, tzinfo=UTC),
        "created_at": datetime(2026, 1, opened, 12, tzinfo=UTC),
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
        "client_ids": ["p1"],
        "opposing_parties": [], "opposing_party_ids": [],
        "etag": "e", **extra,
    }


@pytest.fixture
def db(monkeypatch):
    fake = install(monkeypatch, dossier_model, dav_sync)
    for name in ("parties", "general", f"dossier:{D_OLD}",
                 f"dossier:{D_NEW}", f"dossier:{D_PENDING}",
                 f"dossier:{D_BAD_PARTY}", f"dossier:{D_BAD_NUMBER}",
                 f"dossier:{D_BAD_ID}", f"dossier:{D_NO_ID}",
                 f"dossier:{D_BAD_DATE}"):
        fake.seed(f"dav_sync/{name}", {"ctag": f"ctag-{name}",
                                       "sync_token": f"ctag-{name}"})
    fake.seed(f"dossiers/{D_OLD}", _dossier(D_OLD, "actif", "2026-001", 5))
    fake.seed(f"dossiers/{D_NEW}", _dossier(D_NEW, "actif", "2026-002", 9))
    fake.seed(f"dossiers/{D_PENDING}",
              _dossier(D_PENDING, "en_attente", "", 7))
    fake.seed(f"dossiers/{D_CLOSED}",
              _dossier(D_CLOSED, "fermé", "2026-004", 3))
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    return fake


def _seed_malformed(db) -> None:
    """Three active dossiers the listing cannot use, one per failure mode."""
    # The migration raises: a legacy client entry with no id and no
    # client_ids mirror (_migrate_parties indexes c["id"]).
    bad_party = _dossier(D_BAD_PARTY, "actif", "2026-005", 6)
    bad_party["clients"] = [{"name": "Jean Tremblay"}]
    del bad_party["client_ids"]
    db.seed(f"dossiers/{D_BAD_PARTY}", bad_party)
    # The listing cannot render it: collection_display_name strips a
    # non-string file number.
    db.seed(f"dossiers/{D_BAD_NUMBER}",
            _dossier(D_BAD_NUMBER, "actif", 42, 8))
    # Its stored id is not its document id: the URL would 404.
    db.seed(f"dossiers/{D_BAD_ID}",
            {**_dossier(D_BAD_ID, "en_attente", "2026-007", 4), "id": "autre"})


def _propfind(depth="1"):
    from flask import Flask

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    app.register_blueprint(dav_pkg.dav_bp)
    return app.test_client().open(
        "/dav/", method="PROPFIND",
        headers={"Depth": depth,
                 "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="})


def _fail_queries_on(monkeypatch, db, collection: str):
    """Every QUERY on *collection* fails at the transport — the blip."""
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _unexpected(caplog, message):
    return [r for r in caplog.records
            if r.name == "pallas.unexpected" and message in r.getMessage()]


def _hrefs(body: str) -> list[str]:
    return [chunk.split("</D:href>")[0]
            for chunk in body.split("<D:href>")[1:]]


# ══════════════════════════════════════════════════════════════════════
# 1. Le cas ordinaire — le XML d'avant, octet pour octet
# ══════════════════════════════════════════════════════════════════════

# Captured from the root BEFORE the lot 4 fixes (the fail-open
# list_dossiers), over this exact store: actif newest-opened first, then
# en_attente, the closed dossier absent, the title standing in for a blank
# file number.
_GOLDEN = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<D:multistatus xmlns:C="urn:ietf:params:xml:ns:carddav" xmlns:CAL="urn'
    ':ietf:params:xml:ns:caldav" xmlns:CS="http://calendarserver.org/ns/" x'
    'mlns:D="DAV:">'
    '<D:response><D:href>/dav/</D:href><D:propstat><D:prop>'
    '<D:resourcetype><D:collection /></D:resourcetype>'
    '<D:displayname>Pallas Athena</D:displayname>'
    '<D:current-user-principal><D:href>/dav/</D:href>'
    '</D:current-user-principal><C:addressbook-home-set>'
    '<D:href>/dav/addressbook/</D:href></C:addressbook-home-set>'
    '<CAL:calendar-home-set><D:href>/dav/</D:href></CAL:calendar-home-set>'
    '</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>'
    '</D:response>'
    '<D:response><D:href>/dav/addressbook/</D:href><D:propstat><D:prop>'
    '<D:resourcetype><D:collection /><C:addressbook /></D:resourcetype>'
    '<D:displayname>Clients et parties impliqués</D:displayname>'
    '<CS:getctag>ctag-parties</CS:getctag></D:prop>'
    '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
    '<D:response><D:href>/dav/general/</D:href><D:propstat><D:prop>'
    '<D:resourcetype><D:collection /><CAL:calendar /></D:resourcetype>'
    '<D:displayname>Général</D:displayname>'
    '<CAL:supported-calendar-component-set><CAL:comp name="VEVENT" />'
    '<CAL:comp name="VTODO" /><CAL:comp name="VJOURNAL" />'
    '</CAL:supported-calendar-component-set><CS:getctag>ctag-general'
    '</CS:getctag></D:prop><D:status>HTTP/1.1 200 OK</D:status>'
    '</D:propstat></D:response>'
    '<D:response>'
    '<D:href>/dav/dossier-d0000000-0000-4000-8000-000000000002/</D:href>'
    '<D:propstat><D:prop><D:resourcetype><D:collection /><CAL:calendar />'
    '</D:resourcetype><D:displayname>N/R : 2026-002</D:displayname>'
    '<CAL:supported-calendar-component-set><CAL:comp name="VEVENT" />'
    '<CAL:comp name="VTODO" /><CAL:comp name="VJOURNAL" />'
    '</CAL:supported-calendar-component-set>'
    '<CS:getctag>ctag-dossier:d0000000-0000-4000-8000-000000000002'
    '</CS:getctag></D:prop><D:status>HTTP/1.1 200 OK</D:status>'
    '</D:propstat></D:response>'
    '<D:response>'
    '<D:href>/dav/dossier-d0000000-0000-4000-8000-000000000001/</D:href>'
    '<D:propstat><D:prop><D:resourcetype><D:collection /><CAL:calendar />'
    '</D:resourcetype><D:displayname>N/R : 2026-001</D:displayname>'
    '<CAL:supported-calendar-component-set><CAL:comp name="VEVENT" />'
    '<CAL:comp name="VTODO" /><CAL:comp name="VJOURNAL" />'
    '</CAL:supported-calendar-component-set>'
    '<CS:getctag>ctag-dossier:d0000000-0000-4000-8000-000000000001'
    '</CS:getctag></D:prop><D:status>HTTP/1.1 200 OK</D:status>'
    '</D:propstat></D:response>'
    '<D:response>'
    '<D:href>/dav/dossier-d0000000-0000-4000-8000-000000000003/</D:href>'
    '<D:propstat><D:prop><D:resourcetype><D:collection /><CAL:calendar />'
    '</D:resourcetype><D:displayname>Tremblay c. Lavoie</D:displayname>'
    '<CAL:supported-calendar-component-set><CAL:comp name="VEVENT" />'
    '<CAL:comp name="VTODO" /><CAL:comp name="VJOURNAL" />'
    '</CAL:supported-calendar-component-set>'
    '<CS:getctag>ctag-dossier:d0000000-0000-4000-8000-000000000003'
    '</CS:getctag></D:prop><D:status>HTTP/1.1 200 OK</D:status>'
    '</D:propstat></D:response></D:multistatus>'
)


def test_the_ordinary_listing_is_byte_identical(db):
    resp = _propfind()
    assert resp.status_code == 207
    assert resp.content_type == "application/xml; charset=utf-8"
    assert resp.get_data(as_text=True) == _GOLDEN


def test_depth_0_reads_no_dossier(db, monkeypatch):
    """Depth:0 describes the root alone — not even a failing store reaches
    it (the listing is Depth:1 only, as before)."""
    _fail_queries_on(monkeypatch, db, "dossiers")
    resp = _propfind(depth="0")
    assert resp.status_code == 207
    assert _hrefs(resp.get_data(as_text=True)) == ["/dav/", "/dav/",
                                                    "/dav/addressbook/", "/dav/"]


# ══════════════════════════════════════════════════════════════════════
# 2. STRICTE sur la requête — 503, jamais une liste partielle ni vide
# ══════════════════════════════════════════════════════════════════════


def test_a_failed_dossier_query_answers_503_and_lists_nothing(
        db, monkeypatch, caplog):
    _fail_queries_on(monkeypatch, db, "dossiers")
    with caplog.at_level(logging.INFO):
        resp = _propfind()
    assert resp.status_code == 503
    # The dossier collections' own answer on a failed strict read (lot 1a).
    assert resp.headers["Retry-After"] == "30"
    body = resp.get_data(as_text=True)
    assert body == "Service Unavailable"
    assert "multistatus" not in body and "/dav/" not in body
    (error,) = _unexpected(caplog, "dav root propfind read failed")
    assert error.json_fields["check"] == "dossiers"
    assert error.exc_info is not None          # the traceback rides along
    dav_lines = [r for r in caplog.records if r.name == "pallas.dav"]
    assert [(r.json_fields["operation"], r.json_fields["collection_type"],
             r.json_fields["status_code"], r.json_fields["reason"])
            for r in dav_lines] == [
        ("propfind", "root", 503, "lecture_indisponible")]


def test_one_failing_status_is_a_503_never_a_partial_listing(db, monkeypatch):
    """The actif query succeeds, the en_attente one fails: a 207 listing the
    actif dossiers alone would tell DavX5 the pending ones are gone."""
    real = dossier_model.list_dossiers_by_status_strict

    def half(status):
        if status == "en_attente":
            raise gexc.ServiceUnavailable("injected")
        return real(status)

    monkeypatch.setattr(dossier_model, "list_dossiers_by_status_strict", half)
    resp = _propfind()
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    assert D_OLD not in resp.get_data(as_text=True)


def test_a_failed_ctag_read_answers_503(db, monkeypatch, caplog):
    """The batched CTag read already propagated (sync-token hygiene): it
    used to surface as a 500. A read failure of discovery is a 503."""
    def failing(names):
        raise gexc.ServiceUnavailable("injected")

    monkeypatch.setattr(dav_sync, "get_ctags_bulk", failing)
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = _propfind()
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "30"
    (error,) = _unexpected(caplog, "dav root propfind read failed")
    assert error.json_fields["check"] == "ctags"


# ══════════════════════════════════════════════════════════════════════
# 3. TOLÉRANTE par document — sauté, journalisé, les autres listés
# ══════════════════════════════════════════════════════════════════════


def test_a_malformed_dossier_is_skipped_and_the_others_listed(db, caplog):
    _seed_malformed(db)
    with caplog.at_level(logging.ERROR, logger="pallas.unexpected"):
        resp = _propfind()
    assert resp.status_code == 207
    body = resp.get_data(as_text=True)
    # Exactly the ordinary listing: the three skipped dossiers leave no
    # trace — not even a half-built <D:response>.
    assert body == _GOLDEN
    for did in (D_BAD_PARTY, D_BAD_NUMBER, D_BAD_ID):
        assert did not in body
    model_skips = _unexpected(
        caplog, "list_dossiers_by_status_strict: document skipped")
    assert sorted(r.json_fields["dossier_id"] for r in model_skips) == sorted(
        [D_BAD_PARTY, D_BAD_ID])
    (render_skip,) = _unexpected(caplog, "dav root propfind: dossier skipped")
    assert render_skip.json_fields["dossier_id"] == D_BAD_NUMBER
    for record in (*model_skips, render_skip):
        assert "Tremblay" not in record.getMessage()
        assert "2026-" not in record.getMessage()


def test_a_dossier_stored_without_its_id_is_listed_under_its_document_id(db):
    doc = _dossier(D_NO_ID, "actif", "2026-008", 1)
    del doc["id"]
    db.seed(f"dossiers/{D_NO_ID}", doc)
    resp = _propfind()
    assert resp.status_code == 207
    assert f"/dav/dossier-{D_NO_ID}/" in _hrefs(resp.get_data(as_text=True))


def test_an_unsortable_opening_date_sorts_last_instead_of_failing(db):
    """A TypeError in the sort would have turned one bad date into a 503 of
    the whole discovery: the dossier is listed, oldest."""
    db.seed(f"dossiers/{D_BAD_DATE}",
            {**_dossier(D_BAD_DATE, "actif", "2026-009", 2),
             "opened_date": "2026-01-02"})
    resp = _propfind()
    assert resp.status_code == 207
    dossiers = [h for h in _hrefs(resp.get_data(as_text=True))
                if h.startswith("/dav/dossier-")]
    assert dossiers == [f"/dav/dossier-{D_NEW}/", f"/dav/dossier-{D_OLD}/",
                        f"/dav/dossier-{D_BAD_DATE}/",
                        f"/dav/dossier-{D_PENDING}/"]


# ══════════════════════════════════════════════════════════════════════
# 4. Le lecteur du modèle
# ══════════════════════════════════════════════════════════════════════


def test_the_strict_reader_refuses_an_unknown_status_before_any_read(db):
    """list_dossiers IGNORES an unknown filter and returns every dossier —
    discovery would advertise the whole firm, closed dossiers included."""
    db.reset_logs()
    with pytest.raises(ValueError):
        dossier_model.list_dossiers_by_status_strict("inconnu")
    assert db.reads == []


def test_the_strict_reader_propagates_where_the_display_reader_answers_empty(
        db, monkeypatch):
    _fail_queries_on(monkeypatch, db, "dossiers")
    with pytest.raises(gexc.ServiceUnavailable):
        dossier_model.list_dossiers_by_status_strict("actif")
    assert dossier_model.list_dossiers(status_filter="actif") == []


def test_the_strict_reader_keeps_the_display_readers_order_and_shape(db):
    strict = dossier_model.list_dossiers_by_status_strict("actif")
    display = dossier_model.list_dossiers(status_filter="actif")
    assert [d["id"] for d in strict] == [d["id"] for d in display] == [
        D_NEW, D_OLD]
    assert strict == display


def test_one_malformed_document_no_longer_empties_its_status(db):
    """The display reader's comprehension sat inside its try: one document
    the migration cannot read emptied the whole status. The strict reader
    skips that one alone."""
    _seed_malformed(db)
    assert dossier_model.list_dossiers(status_filter="actif") == []
    assert [d["id"] for d in
            dossier_model.list_dossiers_by_status_strict("actif")] == [
        D_NEW, D_BAD_NUMBER, D_OLD]
