"""Routes des audiences — ce que le formulaire web envoie au modèle (lot 0b, B4).

* **Le dossier introuvable.** ``_enrich_dossier_info`` BLANCHISSAIT un
  ``dossier_id`` qui ne se résout pas : le modèle accepte ``""`` (une audience
  peut être autonome), donc la date de cour passait en silence dans
  « Général » — hors de l'onglet du dossier, dans une autre collection DAV
  sur le téléphone — avec une redirection de succès. Elle est désormais
  REFUSÉE, re-rendue à 200 (htmx n'échange que les 2xx), comme les notes.
* **Le formulaire d'une audience d'une journée entière.** Il lisait la date
  stockée (minuit UTC) par ``to_mtl`` : il affichait la VEILLE et la
  réécrivait à chaque sauvegarde — l'événement reculait d'un jour à chaque
  édition web, et basculer vers un horaire le posait sur la veille.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

from tz import mtl_to_utc, to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import routes.hearings as rh

UTC = timezone.utc
NOT_FOUND = (
    "Dossier introuvable. Choisissez un dossier existant, ou laissez le "
    "champ vide pour classer l'audience dans « Général »."
)
DOSSIERS = {"d1": {"id": "d1", "file_number": "2026-001",
                   "title": "Tremblay c. Lavoie"}}


@pytest.fixture()
def client(monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.jinja_env.globals.update(
        csrf_token=lambda: "tok", ms=ms, csp_nonce=lambda: "n"
    )
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.jinja_env.filters["jsattr"] = lambda v: v
    app.register_blueprint(rh.hearings_bp)
    monkeypatch.setattr(rh, "get_dossier", DOSSIERS.get)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


@pytest.fixture()
def bumps(monkeypatch):
    """Every DAV primitive the routes can reach, recorded in order: through
    the route's own imported names AND through dav.sync, where
    relocate_resource (the edit route's move choreography since lot 1a)
    looks them up at call time."""
    seen = []
    for module in (rh, dav_sync):
        for name, fn in (("bump_ctag", seen.append),
                         ("record_tombstone", lambda *a: seen.append(a)),
                         ("remove_tombstone", lambda *a: seen.append(a))):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fn)
    return seen


def _form(**over) -> dict:
    base = {"title": "Audience", "start_date": "2026-10-15",
            "start_time": "09:00", "end_time": "10:00",
            "hearing_type": "audience", "status": "confirmée",
            "dossier_id": "d1", "dossier_display": "2026-001"}
    base.update(over)
    return base


def _forbid(name):
    def _raise(*a, **k):
        raise AssertionError(f"{name} ne doit pas être appelé")
    return _raise


def test_create_refuses_an_unresolvable_dossier(client, monkeypatch, bumps):
    """THE defect: the old route blanked the id and created the court date
    under « Général » with a success redirect."""
    monkeypatch.setattr(rh, "create_hearing", _forbid("create_hearing"))
    r = client.post("/audiences/", data=_form(dossier_id="parti"))
    assert r.status_code == 200
    assert str(escape(NOT_FOUND)) in r.get_data(as_text=True)
    assert bumps == []


def test_a_series_create_refuses_an_unresolvable_dossier(client, monkeypatch):
    monkeypatch.setattr(rh, "create_hearing_series",
                        _forbid("create_hearing_series"))
    r = client.post("/audiences/", data=_form(
        dossier_id="parti", frequence="hebdomadaire", fin_mode="count",
        fin_count="4"))
    assert r.status_code == 200
    assert str(escape(NOT_FOUND)) in r.get_data(as_text=True)


def test_update_refuses_an_unresolvable_dossier(client, monkeypatch, bumps):
    monkeypatch.setattr(rh, "get_hearing", lambda hid: {
        "id": hid, "dossier_id": "d1", "title": "Audience"})
    monkeypatch.setattr(rh, "update_hearing", _forbid("update_hearing"))
    r = client.post("/audiences/h1", data=_form(dossier_id="parti"))
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert str(escape(NOT_FOUND)) in body
    assert bumps == [], "a refused edit must not touch any DAV collection"


def test_an_empty_dossier_is_still_general(client, monkeypatch, bumps):
    seen = {}

    def _create(data):
        seen.update(data)
        return {**data, "id": "h9"}, []

    monkeypatch.setattr(rh, "create_hearing", _create)
    r = client.post("/audiences/", data=_form(dossier_id=""))
    assert r.status_code == 302
    assert seen["dossier_id"] == ""
    assert seen["dossier_file_number"] == "" and seen["dossier_title"] == ""
    assert bumps == ["general"]


def test_a_resolvable_dossier_gets_its_labels(client, monkeypatch, bumps):
    seen = {}
    monkeypatch.setattr(rh, "get_hearing", lambda hid: {
        "id": hid, "dossier_id": "d1", "title": "Audience"})

    def _update(hid, data):
        seen.update(data)
        return {**data, "id": hid}, []

    monkeypatch.setattr(rh, "update_hearing", _update)
    r = client.post("/audiences/h1", data=_form(dossier_id="d1"))
    assert r.status_code == 302
    assert seen["dossier_file_number"] == "2026-001"
    assert seen["dossier_title"] == "Tremblay c. Lavoie"
    assert bumps == ["dossier:d1"]


# ══════════════════════════════════════════════════════════════════════
# Le formulaire d'une audience d'une journée entière
# ══════════════════════════════════════════════════════════════════════
#
# Une audience « toute la journée » est une DATE stockée à minuit UTC, de fin
# EXCLUSIVE (RFC 5545 — ce que lisent le téléphone et le miroir Outlook). Le
# formulaire lisait ces valeurs par to_mtl : la date affichée était la VEILLE
# (minuit UTC = 20 h la veille à Montréal), et enregistrer le formulaire tel
# quel réécrivait cette veille — chaque sauvegarde reculait l'événement d'un
# jour, et basculer vers un horaire le posait sur la veille.


def _value(html: str, field: str) -> str:
    m = re.search(rf'id="{field}" name="{field}"\s+value="([^"]*)"', html)
    assert m, field
    return m.group(1)


def _all_day(hid="h1", day=15, days=1, end=None):
    start = datetime(2026, 10, day, tzinfo=UTC)
    return {"id": hid, "title": "Journée d'instruction", "all_day": True,
            "hearing_type": "instruction", "status": "confirmée",
            "dossier_id": "d1", "dossier_file_number": "2026-001",
            "dossier_title": "Tremblay c. Lavoie",
            "start_datetime": start,
            "end_datetime": end or start + timedelta(days=days)}


def _edit_page(client, monkeypatch, hearing) -> str:
    monkeypatch.setattr(rh, "get_hearing", lambda hid: dict(hearing))
    r = client.get(f"/audiences/{hearing['id']}/edit")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_edit_form_shows_an_all_day_hearing_on_its_own_day(
    client, monkeypatch
):
    """THE defect: the date input read 2026-10-14 for an event of the 15th."""
    html = _edit_page(client, monkeypatch, _all_day())
    assert _value(html, "start_date") == "2026-10-15"
    assert _value(html, "end_date") == ""          # a one-day event


@pytest.mark.parametrize("end", [
    datetime(2026, 10, 16, tzinfo=UTC),            # exclusive DTEND (phone)
    datetime(2026, 10, 15, 1, tzinfo=UTC),         # the create default +1 h
])
def test_a_one_day_event_shows_no_end_date(client, monkeypatch, end):
    html = _edit_page(client, monkeypatch, _all_day(end=end))
    assert _value(html, "start_date") == "2026-10-15"
    assert _value(html, "end_date") == ""


def test_a_multi_day_event_shows_its_last_day(client, monkeypatch):
    html = _edit_page(client, monkeypatch, _all_day(days=3))  # 15, 16, 17
    assert _value(html, "end_date") == "2026-10-17"


def test_saving_an_all_day_form_unchanged_writes_the_same_slot(
    client, monkeypatch, bumps
):
    """The round trip that moved the event one day earlier on every save."""
    hearing = _all_day(days=3)
    html = _edit_page(client, monkeypatch, hearing)
    seen = {}

    def _update(hid, data):
        seen.update(data)
        return {**hearing, **data}, []

    monkeypatch.setattr(rh, "update_hearing", _update)
    r = client.post("/audiences/h1", data=_form(
        all_day="on", start_date=_value(html, "start_date"),
        end_date=_value(html, "end_date")))
    assert r.status_code == 302
    assert seen["start_datetime"] == hearing["start_datetime"]
    assert seen["end_datetime"] == hearing["end_datetime"]


def test_the_end_date_entered_is_the_last_day_of_the_event(
    client, monkeypatch, bumps
):
    seen = {}
    monkeypatch.setattr(rh, "create_hearing",
                        lambda d: (seen.update(d) or ({**d, "id": "h9"}, [])))
    client.post("/audiences/", data=_form(
        all_day="on", start_date="2026-10-15", end_date="2026-10-17"))
    assert seen["start_datetime"] == datetime(2026, 10, 15, tzinfo=UTC)
    assert seen["end_datetime"] == datetime(2026, 10, 18, tzinfo=UTC)


def test_switching_an_all_day_hearing_to_a_time_keeps_its_day(
    client, monkeypatch, bumps
):
    hearing = _all_day()
    html = _edit_page(client, monkeypatch, hearing)
    seen = {}
    monkeypatch.setattr(rh, "update_hearing",
                        lambda hid, d: (seen.update(d) or ({**hearing, **d}, [])))
    client.post("/audiences/h1", data=_form(
        start_date=_value(html, "start_date"), start_time="09:30",
        end_time="10:30"))
    assert seen["all_day"] is False
    assert seen["start_datetime"] == mtl_to_utc(datetime(2026, 10, 15, 9, 30))


def test_a_timed_hearing_still_reads_in_montreal_time(client, monkeypatch):
    start = mtl_to_utc(datetime(2026, 10, 15, 21, 0))   # 01 h UTC the 16th
    html = _edit_page(client, monkeypatch, {
        "id": "h1", "title": "Rencontre", "all_day": False,
        "hearing_type": "rencontre", "status": "confirmée", "dossier_id": "",
        "start_datetime": start, "end_datetime": start + timedelta(hours=1)})
    assert _value(html, "start_date") == "2026-10-15"
    assert _value(html, "start_time") == "21:00"
    assert _value(html, "end_time") == "22:00"


# ══════════════════════════════════════════════════════════════════════
# Par le VRAI modèle (revue du lot 0b, B4)
# ══════════════════════════════════════════════════════════════════════
#
# Every test above replaces update_hearing with a spy that accepts ANY key.
# Since lot 0b the model refuses a key outside UPDATE_FIELDS and
# renormalizes the slot, so nothing above proved that what the web form
# actually posts is (a) accepted by the whitelist and (b) written back
# unchanged by _renormalize_slot. A form field added outside UPDATE_FIELDS
# would refuse EVERY web edit of a hearing; a renormalization rule that
# disagreed with _form_data would move the event on every save — the exact
# defect this lot removed. These run the real route on the real client
# (tests/_fake_firestore.py: only the server is fake).


@pytest.fixture()
def real_store(monkeypatch):
    from tests._fake_firestore import install
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    store = install(monkeypatch, *modules)
    # The blueprint's own references — the spies of the other tests are
    # monkeypatched per test, so these are the model functions again here.
    import models.hearing as hearing_model
    monkeypatch.setattr(rh, "get_hearing", hearing_model.get_hearing)
    monkeypatch.setattr(rh, "update_hearing", hearing_model.update_hearing)
    return store


def _stored_dt(value) -> datetime:
    return datetime.fromtimestamp(value.timestamp(), tz=UTC)


def _post_unchanged(client, hid: str, html: str, **fields) -> None:
    form = {
        "title": fields.pop("title"),
        "start_date": _value(html, "start_date"),
        "start_time": _value(html, "start_time"),
        "end_time": _value(html, "end_time"),
        "end_date": _value(html, "end_date"),
        "reminder_minutes": "1440", "modalite": "présentiel",
        **fields,
    }
    r = client.post(f"/audiences/{hid}", data=form)
    assert r.status_code == 302, r.get_data(as_text=True)


def test_the_web_form_payload_passes_the_model_whitelist(client):
    """Every key _form_data + _enrich_dossier_info hand to update_hearing is
    a key the model honours. Derived from the route's own payload, so a new
    form field outside UPDATE_FIELDS fails here, not on every real edit."""
    import models.hearing as hearing_model
    app = client.application
    with app.test_request_context("/audiences/h1", method="POST",
                                  data=_form(all_day="on", end_date="")):
        data, errors = rh._enrich_dossier_info(rh._form_data())
    assert errors == []
    assert hearing_model.update_key_errors(data) == []


def test_saving_an_all_day_span_unchanged_through_the_real_model(
    client, real_store
):
    start = datetime(2026, 10, 15, tzinfo=UTC)
    real_store.seed("hearings/h1", {
        **_all_day(days=3), "status": "reportée", "vevent_uid": "u-h1",
        "etag": "e0", "modalite": "présentiel"})
    html = client.get("/audiences/h1/edit").get_data(as_text=True)
    _post_unchanged(client, "h1", html, title="Journée d'instruction",
                    all_day="on", hearing_type="instruction",
                    status="reportée", dossier_id="d1")
    stored = real_store.peek("hearings/h1")
    assert _stored_dt(stored["start_datetime"]) == start
    assert _stored_dt(stored["end_datetime"]) == start + timedelta(days=3)
    assert stored["all_day"] is True and stored["status"] == "reportée"


def test_saving_a_timed_hearing_unchanged_through_the_real_model(
    client, real_store
):
    start = mtl_to_utc(datetime(2026, 10, 15, 21, 0))   # 01 h UTC the 16th
    real_store.seed("hearings/h1", {
        "id": "h1", "title": "Rencontre", "all_day": False,
        "hearing_type": "rencontre", "status": "confirmée", "dossier_id": "",
        "dossier_file_number": "", "dossier_title": "",
        "start_datetime": start, "end_datetime": start + timedelta(hours=1),
        "vevent_uid": "u-h1", "etag": "e0", "modalite": "présentiel"})
    html = client.get("/audiences/h1/edit").get_data(as_text=True)
    _post_unchanged(client, "h1", html, title="Rencontre",
                    hearing_type="rencontre", status="confirmée",
                    dossier_id="")
    stored = real_store.peek("hearings/h1")
    assert _stored_dt(stored["start_datetime"]) == start
    assert _stored_dt(stored["end_datetime"]) == start + timedelta(hours=1)
