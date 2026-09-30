"""Réception « Rendez-vous » — the tab, the decisions, the divergences.

Retargeted deliberately in lot 1a (L4), when the confirm / refuse / list
orchestration moved out of ``routes/reception.py`` into
``services/rendez_vous.py``. The previous version drove the routes over
spies (``get_hearing``, ``update_hearing``, ``list_hearings`` replaced by
lambdas): it could not see what the service now guarantees against the
STORE, so the decisions are proved here through the real route, the real
service and the real models on the shared fake Firestore
(``tests/_fake_firestore.py`` — only the server is fake), and assertions
read what is STORED, never a dict handed to a mock. What each test pins
that the old code did not do is named in its docstring:

* the tab reads through a STRICT reader — a Firestore blip shows the
  warning banner, never « Aucun rendez-vous à confirmer »;
* the confirmed contact is matched SERVER-SIDE on the requester's exact
  address — a posted ``partie_id`` is ignored;
* confirming an import the client cancelled is refused;
* the page's version is checked BEFORE Graph cancels a meeting; the write
  after the call is unconditional; a local failure after a successful
  cancellation is a success with a warning, never a retryable error.

The divergence route did not move; its tests keep their spies.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask, render_template  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    import models.hearing as hearing_model
    import models.partie as partie_model
    import routes.reception as reception
    from services import rendez_vous

from models import concurrency  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.graph import GraphError  # noqa: E402
from utils.icons import ms  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync,)

UTC = timezone.utc
_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
START = datetime(2026, 10, 15, 14, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def db(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n == "dav.sync")
               and getattr(m, "db", None) is not None]
    store = install(monkeypatch, *modules)
    store.seed("dav_sync/general", {"ctag": "c0", "sync_token": "c0"})
    return store


@pytest.fixture
def app(db):
    app = Flask(__name__, template_folder=_TEMPLATES)
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms)
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.register_blueprint(reception.reception_bp)
    return app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)
    return c


@pytest.fixture
def graph(monkeypatch):
    """Graph configured; every cancellation recorded as (event id, text)."""
    calls = []
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)
    monkeypatch.setattr(
        rendez_vous.graph_calendrier, "annuler_reservation",
        lambda gid, motif="": calls.append((gid, motif)),
    )
    return calls


def _booking(db, hid="h1", **over) -> dict:
    doc = {
        "id": hid, "title": "Consultation", "hearing_type": "consultation",
        "status": "confirmée", "source": "bookings",
        "confirmation": "à_confirmer", "dossier_id": "",
        "dossier_file_number": "", "dossier_title": "",
        "start_datetime": START, "end_datetime": START + timedelta(hours=1),
        "all_day": False, "graph_event_id": f"EVT-{hid}",
        "graph_ical_uid": f"UID-{hid}", "client_email": "client@ex.com",
        "client_nom": "Jean Tremblay", "vevent_uid": f"v-{hid}",
        "etag": f"e-{hid}",
    }
    doc.update(over)
    db.seed(f"hearings/{hid}", doc)
    return doc


def _partie(db, pid, email, *, updated_at=START, **over) -> dict:
    doc = {"id": pid, "type": "individual", "contact_role": "client",
           "first_name": "Jean", "last_name": pid, "email": email,
           "updated_at": updated_at}
    doc.update(over)
    db.seed(f"parties/{pid}", doc)
    return doc


def _stored(db, hid="h1") -> dict:
    return db.peek(f"hearings/{hid}")


def _ctag(db) -> str:
    return db.peek("dav_sync/general")["ctag"]


def _query(resp) -> dict:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    return {k: v[0] for k, v in
            parse_qs(urlsplit(resp.headers["Location"]).query).items()}


def _fail_queries_on(monkeypatch, db, collection: str) -> None:
    """Every query on *collection* fails at the transport — a Firestore
    blip there, everything else healthy."""
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if sq.from_ and sq.from_[0].collection_id == collection:
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _fail_hearing_commits(db, times: int = 10**6) -> list:
    """The next *times* commits touching ``hearings/`` fail server-side —
    nothing of them is applied."""
    failed = []

    def hook(info):
        if len(failed) < times and any(p.startswith("hearings/")
                                       for _, p in info.ops):
            failed.append(1)
            raise gexc.ServiceUnavailable("injected commit failure")

    db.add_commit_hook(hook)
    return failed


def _race_on_commit(db, **changes) -> None:
    """Another writer (the sync, another tab) lands on hearings/h1 at the
    start of the first commit that writes it."""
    raced = []

    def race(info):
        if not raced and any(p == "hearings/h1" for _, p in info.ops):
            raced.append(1)
            doc = _stored(db)
            doc.update(changes)
            db.external_write("hearings/h1", doc)

    db.add_commit_hook(race)


# ══════════════════════════════════════════════════════════════════════
# 1. La lecture — stricte
# ══════════════════════════════════════════════════════════════════════


def test_the_tab_lists_pending_imports_and_links_the_matched_contact(app, db):
    _booking(db, "a", start_datetime=START + timedelta(days=1),
             client_email="Client@Ex.com")
    _booking(db, "b", confirmation="annulée_client",
             client_email="nobody@x.com")
    _booking(db, "c", confirmation="",
             bookings_divergence={"motif": "modifié_côté_client", "vu": False})
    _booking(db, "d", confirmation="")                          # plain confirmed
    _booking(db, "e", confirmation="refusée")                   # never listed
    _booking(db, "f", confirmation="",
             bookings_divergence={"motif": "annulé_côté_client", "vu": True})
    _partie(db, "p1", "client@ex.com")
    with app.test_request_context():
        ctx = reception._contexte_rdv()
    assert ctx["erreur_rdv"] is False
    assert [h["id"] for h in ctx["rdvs"]] == ["b", "a"]         # chronological
    assert [h["id"] for h in ctx["divergences"]] == ["c"]
    a = next(h for h in ctx["rdvs"] if h["id"] == "a")
    assert a["_partie_id"] == "p1" and a["_partie_nom"] == "Jean p1"
    b = next(h for h in ctx["rdvs"] if h["id"] == "b")
    assert b["_partie_id"] == ""


def test_the_divergences_stay_chronological(app, db):
    """Review of lot 1a (L4): the old tab read list_hearings, which sorts
    by start; the strict reader is unordered, and the first cut of the
    service left the divergence cards in document-id order."""
    div = {"motif": "modifié_côté_client", "vu": False}
    _booking(db, "a-late", confirmation="", bookings_divergence=div,
             start_datetime=START + timedelta(days=9))
    _booking(db, "z-early", confirmation="", bookings_divergence=div,
             start_datetime=START + timedelta(days=1))
    _booking(db, "m-mid", confirmation="", bookings_divergence=div,
             start_datetime=START + timedelta(days=4))
    with app.test_request_context():
        ctx = reception._contexte_rdv()
    assert [h["id"] for h in ctx["divergences"]] == [
        "z-early", "m-mid", "a-late"]


def _render_tab(app) -> str:
    with app.test_request_context():
        return render_template("reception/_rdv.html", feature_intake=True,
                               **reception._contexte_rdv())


def test_a_failed_read_shows_the_banner_and_never_an_empty_state(
    app, db, monkeypatch
):
    """THE defect: list_hearings swallowed the error into [], the route's
    try could never fire, and a blip printed « Aucun rendez-vous à
    confirmer » over a reservation waiting for an answer."""
    _booking(db)
    _fail_queries_on(monkeypatch, db, "hearings")
    html = _render_tab(app)
    assert rendez_vous.LECTURE_IMPOSSIBLE in html
    assert "Aucun rendez-vous à confirmer." not in html


def test_an_unreadable_contacts_index_is_a_failed_read_too(
    app, db, monkeypatch
):
    """Read as « aucune partie », the outage offered the onboarding form to
    an existing client."""
    _booking(db)
    _fail_queries_on(monkeypatch, db, "parties")
    html = _render_tab(app)
    assert "Lecture des rendez-vous impossible" in html
    assert "Envoyer le formulaire d'ouverture" not in html


def test_nothing_pending_still_says_so(app, db):
    html = _render_tab(app)
    assert "Aucun rendez-vous à confirmer." in html
    assert "Lecture des rendez-vous impossible" not in html


def test_the_cards_carry_the_version_and_no_partie_id(app, db):
    _booking(db)
    _partie(db, "p1", "client@ex.com")
    html = _render_tab(app)
    confirm = html[html.index('action="/reception/rdv/h1/confirmer"'):]
    confirm = confirm[:confirm.index("</form>")]
    refuse = html[html.index('action="/reception/rdv/h1/refuser"'):]
    refuse = refuse[:refuse.index("</form>")]
    for form in (confirm, refuse):
        assert 'name="expected_etag" value="e-h1"' in form
    assert 'name="partie_id"' not in confirm
    assert "Lier à Jean p1" in confirm


def test_the_strict_reader_propagates_and_never_returns_refusee(
    db, monkeypatch
):
    _booking(db, "a")
    _booking(db, "b", confirmation="refusée")
    _booking(db, "c", confirmation="")
    _booking(db, "x", source="", confirmation="à_confirmer")
    assert {h["id"] for h in hearing_model.list_bookings_strict()} == {"a"}
    assert {h["id"] for h in hearing_model.list_bookings_strict(
        include_confirmed=True)} == {"a", "c"}
    _fail_queries_on(monkeypatch, db, "hearings")
    with pytest.raises(gexc.ServiceUnavailable):
        hearing_model.list_bookings_strict()


def test_the_contacts_index_keeps_the_scan_precedence(db):
    """The most recently updated contact wins an address two carry; the
    personal address before the professional one; matched lower-cased."""
    _partie(db, "old", "shared@ex.com", updated_at=START)
    _partie(db, "new", "Shared@Ex.com", updated_at=START + timedelta(days=1))
    _partie(db, "w", "perso@ex.com", email_work="pro@ex.com")
    index = partie_model.index_by_email_strict()
    assert index["shared@ex.com"]["id"] == "new"
    assert index["pro@ex.com"]["id"] == "w"


# ── La pastille ─────────────────────────────────────────────────────────


def test_the_badge_counts_pending_imports(db, monkeypatch):
    monkeypatch.setattr(reception.pi, "compter_soumises", lambda: 2)
    _booking(db, "a")
    _booking(db, "b")
    _booking(db, "c", confirmation="annulée_client")
    _booking(db, "d", confirmation="")
    reception._badge_cache["at"] = 0.0
    try:
        assert reception.compteur_reception() == 4
    finally:
        reception._badge_cache.update(at=0.0, n=None)


def test_the_badge_fails_open_when_both_are_unavailable(db, monkeypatch):
    monkeypatch.setattr(reception.pi, "compter_soumises", lambda: None)
    _fail_queries_on(monkeypatch, db, "hearings")
    reception._badge_cache["at"] = 0.0
    try:
        assert reception.compteur_reception() is None
    finally:
        reception._badge_cache.update(at=0.0, n=None)


# ══════════════════════════════════════════════════════════════════════
# 2. Confirmer
# ══════════════════════════════════════════════════════════════════════


def test_confirm_enters_dav_and_writes_only_the_gate(client, db):
    _booking(db)
    db.seed("dav_sync/general/tombstones/h1", {"id": "h1", "sync_token": "x"})
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": "e-h1"}))
    assert "message" in q and "erreur" not in q
    stored = _stored(db)
    assert stored["confirmation"] == ""
    assert stored["etag"] != "e-h1" and stored["updated_via"] == "web"
    # A partial write: nothing but the gate and the stamp moved.
    moved = {k for k in stored if stored.get(k) != before.get(k)}
    assert moved == {"confirmation", "etag", "updated_at", "updated_via"}
    assert "partie_id" not in stored
    assert _ctag(db) != "c0"
    assert db.peek("dav_sync/general/tombstones/h1") is None


def test_confirm_links_the_contact_matched_server_side(client, db):
    """A posted partie_id is ignored: the old route linked ANY existing
    contact a forged form named."""
    _booking(db)
    _partie(db, "p-match", "client@ex.com")
    _partie(db, "p-other", "autre@ex.com")
    q = _query(client.post("/reception/rdv/h1/confirmer", data={
        "lier": "on", "partie_id": "p-other", "expected_etag": "e-h1"}))
    assert "erreur" not in q
    assert _stored(db)["partie_id"] == "p-match"


def test_confirm_asked_to_link_but_no_contact_matches_is_refused(client, db):
    _booking(db)
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"lier": "on", "expected_etag": "e-h1"}))
    assert q["erreur"] == rendez_vous.AUCUN_CONTACT
    assert _stored(db) == before and _ctag(db) == "c0"


def test_confirming_an_import_the_client_cancelled_is_refused(client, db):
    """The old route confirmed it: a meeting the client had cancelled
    entered the calendar and the phone."""
    _booking(db, confirmation="annulée_client")
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": "e-h1"}))
    assert q["erreur"] == rendez_vous.ANNULE_PAR_LE_CLIENT
    assert _stored(db) == before and _ctag(db) == "c0"


def test_a_stale_confirm_is_refused_before_anything(client, db):
    """The sync moved the slot after the page rendered: confirming would
    confirm a time nobody saw."""
    _booking(db)
    moved = _stored(db)
    moved.update(start_datetime=START + timedelta(days=2), etag="e-sync")
    db.external_write("hearings/h1", moved)
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": "e-h1"}))
    assert q["erreur"] == reception._RDV_PERIME
    assert _stored(db) == before and _ctag(db) == "c0"


def test_a_confirm_racing_a_sync_write_is_refused(client, db):
    """The comparison also runs inside the transaction."""
    _booking(db)
    _race_on_commit(db, title="Consultation (déplacée)", etag="e-sync")
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": "e-h1"}))
    assert q["erreur"] == reception._RDV_PERIME
    assert _stored(db)["confirmation"] == "à_confirmer"
    assert _ctag(db) == "c0"


def test_a_page_without_the_field_confirms_on_the_legacy_path(client, db):
    _booking(db, etag="e-other")
    q = _query(client.post("/reception/rdv/h1/confirmer"))
    assert "erreur" not in q
    assert _stored(db)["confirmation"] == ""


def test_confirming_twice_writes_nothing_the_second_time(client, db):
    _booking(db)
    client.post("/reception/rdv/h1/confirmer", data={"expected_etag": "e-h1"})
    after = _stored(db)
    ctag = _ctag(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": after["etag"]}))
    assert q["message"] == "Ce rendez-vous était déjà confirmé."
    assert _stored(db) == after and _ctag(db) == ctag


def test_a_double_click_reads_already_confirmed_not_changed(client, db):
    """The second click carries the page's OLD version. The no-op is
    answered before the version check: « déjà confirmé », never « ce
    rendez-vous a changé » over the lawyer's own first click."""
    _booking(db)
    client.post("/reception/rdv/h1/confirmer", data={"expected_etag": "e-h1"})
    after = _stored(db)
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"expected_etag": "e-h1"}))
    assert q["message"] == "Ce rendez-vous était déjà confirmé."
    assert _stored(db) == after


def test_a_legacy_confirm_still_commits_against_what_it_read(client, db):
    """No version posted (a page from before the field): the service
    compares at commit against the version IT read, on which it judged the
    transition — the sync flipping the import to annulée_client in between
    is refused, never confirmed over."""
    _booking(db)
    _race_on_commit(db, confirmation="annulée_client", etag="e-sync")
    q = _query(client.post("/reception/rdv/h1/confirmer"))
    assert q["erreur"] == reception._RDV_PERIME
    assert _stored(db)["confirmation"] == "annulée_client"
    assert _ctag(db) == "c0"


def test_a_non_booking_hearing_is_not_a_rendez_vous(client, db):
    _booking(db, source="")
    q = _query(client.post("/reception/rdv/h1/confirmer"))
    assert q["erreur"] == "Rendez-vous introuvable."


def test_an_unreadable_hearing_is_never_introuvable(db, monkeypatch):
    """get_hearing is fail-open (« introuvable » over a blip): the service
    reads strictly and says the read failed."""
    _booking(db)
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        raise gexc.ServiceUnavailable("injected read failure")

    monkeypatch.setattr(server, "batch_get_documents", failing)
    for decide in (lambda: rendez_vous.confirmer("h1", lier_partie=False),
                   lambda: rendez_vous.refuser("h1")):
        _h, errors, _r = decide()
        assert errors == [hearing_model.CONFIRMATION_READ_ERROR]
    monkeypatch.setattr(server, "batch_get_documents", real)
    assert _stored(db)["confirmation"] == "à_confirmer"


# ── Déclencheur (a) : formulaire d'ouverture à la confirmation (L3) ──────


@pytest.fixture()
def emissions(db, monkeypatch):
    """A Bookings import whose address matches no contact; the invitations
    the route emits are captured."""
    appels = []

    def _emettre(type_, email, **kw):
        appels.append({"type": type_, "email": email, **kw})
        return {"id": "inv9"}, [], ""

    _booking(db, client_email="nouveau@exemple.com",
             client_nom="Nouveau Client")
    monkeypatch.setattr(reception.emission, "emettre_invitation", _emettre)
    monkeypatch.setattr(reception.Config, "FEATURE_INTAKE", True)
    return appels


def test_intake_coche_emet_une_invitation(client, emissions):
    r = client.post("/reception/rdv/h1/confirmer", data={"intake": "on"})
    assert r.status_code == 302
    assert emissions[0]["type"] == "intake"
    assert emissions[0]["email"] == "nouveau@exemple.com"
    # Libellé générique : le client ne doit rien apprendre du dossier.
    assert emissions[0]["display_label"] == "Ouverture de votre dossier client"


def test_intake_non_coche_n_emet_rien(client, emissions):
    client.post("/reception/rdv/h1/confirmer")
    assert emissions == []


def test_intake_inerte_quand_le_drapeau_est_baisse(client, emissions,
                                                    monkeypatch):
    monkeypatch.setattr(reception.Config, "FEATURE_INTAKE", False)
    client.post("/reception/rdv/h1/confirmer", data={"intake": "on"})
    assert emissions == []


def test_intake_non_emis_quand_une_partie_est_liee(client, db, emissions):
    """Le formulaire d'ouverture sert un NOUVEAU client : un contact déjà
    reconnu n'a rien à remplir."""
    _partie(db, "p1", "nouveau@exemple.com")
    client.post("/reception/rdv/h1/confirmer",
                data={"intake": "on", "lier": "on"})
    assert emissions == []
    assert _stored(db)["partie_id"] == "p1"


def test_intake_non_emis_quand_la_confirmation_est_refusee(client, db,
                                                            emissions):
    """Nothing confirmed, nothing sent: the email follows the decision."""
    moved = _stored(db)
    moved.update(title="Consultation (déplacée)", etag="e-sync")
    db.external_write("hearings/h1", moved)
    client.post("/reception/rdv/h1/confirmer",
                data={"intake": "on", "expected_etag": "e-h1"})
    assert emissions == []


def test_une_panne_d_emission_n_annule_pas_la_confirmation(client, db,
                                                            emissions,
                                                            monkeypatch):
    """La confirmation est DÉJÀ commise (CTag bumpé) : un échec d'envoi
    produit un bandeau, jamais un échec — sinon le juriste croirait le
    rendez-vous non confirmé alors qu'il l'est."""
    monkeypatch.setattr(
        reception.emission, "emettre_invitation",
        mock.Mock(side_effect=RuntimeError("graph down")),
    )
    q = _query(client.post("/reception/rdv/h1/confirmer",
                           data={"intake": "on"}))
    assert "message" in q and "erreur" not in q
    assert _stored(db)["confirmation"] == ""


# ══════════════════════════════════════════════════════════════════════
# 3. Refuser — Outlook annule, le client est notifié
# ══════════════════════════════════════════════════════════════════════


def test_refuse_cancels_outlook_with_the_fixed_text_then_writes(
    client, db, graph
):
    _booking(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert graph == [("EVT-h1", rendez_vous.REFUS_MOTIF)]
    stored = _stored(db)
    assert stored["confirmation"] == "refusée"
    assert stored["updated_via"] == "web"
    assert "annulée" in q["message"] and "erreur" not in q
    assert _ctag(db) == "c0"            # a pending import was never in DAV


def test_a_stale_refusal_never_reaches_outlook(client, db, graph):
    """THE outbound rule: the old route called Graph first and read
    nothing — a page older than the sync's last update cancelled the
    client's meeting on the strength of what it no longer showed."""
    _booking(db)
    moved = _stored(db)
    moved.update(start_datetime=START + timedelta(days=2), etag="e-sync")
    db.external_write("hearings/h1", moved)
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert graph == []
    assert q["erreur"] == reception._RDV_PERIME
    assert "Outlook n'a pas été touché" in q["erreur"]
    assert _stored(db) == before


def test_after_the_cancellation_the_write_is_unconditional(
    client, db, monkeypatch
):
    """Once the client is notified, a write landing during the Graph call
    (the sync moving the slot) must not turn the refusal into an error."""
    _booking(db)
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)

    def cancel_while_the_sync_writes(gid, motif=""):
        doc = _stored(db)
        doc.update(title="Consultation (déplacée)", etag="e-sync")
        db.external_write("hearings/h1", doc)

    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        cancel_while_the_sync_writes)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert "erreur" not in q
    stored = _stored(db)
    assert stored["confirmation"] == "refusée"
    assert stored["title"] == "Consultation (déplacée)"  # partial: kept


def test_a_local_failure_after_the_cancellation_is_a_warned_success(
    client, db, graph
):
    """Never an error a retry would turn into a second Graph call — and
    the message says so. The old route printed the generic save error, and
    the lawyer's retry told him to « cancel manually » a meeting already
    cancelled."""
    _booking(db)
    failed = _fail_hearing_commits(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert graph == [("EVT-h1", rendez_vous.REFUS_MOTIF)]
    assert len(failed) == 2                 # the write, then its one retry
    assert q["erreur"] == rendez_vous.REFUS_NON_INSCRIT
    assert _stored(db)["confirmation"] == "à_confirmer"


def test_the_service_reports_the_warned_success_as_no_error(db, graph):
    _booking(db)
    _fail_hearing_commits(db)
    hearing, errors, report = rendez_vous.refuser("h1", expected_etag="e-h1")
    assert errors == [] and hearing is None
    assert report["graph_cancelled"] is True
    assert report["local_written"] is False
    assert report["warning"] == rendez_vous.REFUS_NON_INSCRIT


def test_one_retry_rescues_a_blip_after_the_cancellation(client, db, graph):
    _booking(db)
    _fail_hearing_commits(db, times=1)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert "erreur" not in q
    assert _stored(db)["confirmation"] == "refusée"


def test_a_graph_failure_still_refuses_with_the_manual_warning(
    client, db, monkeypatch
):
    _booking(db)
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)

    def _boom(gid, motif=""):
        raise GraphError("boom")

    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        _boom)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert q["erreur"] == rendez_vous.ANNULATION_OUTLOOK_ECHOUEE
    assert _stored(db)["confirmation"] == "refusée"


def test_removing_a_client_cancelled_import_does_not_call_graph(
    client, db, graph
):
    _booking(db, confirmation="annulée_client")
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert graph == []                      # already cancelled client-side
    assert q["message"] == "Rendez-vous refusé."
    assert _stored(db)["confirmation"] == "refusée"


def _spy_refuse_log(monkeypatch) -> list:
    seen = []
    monkeypatch.setattr(
        rendez_vous, "log_bookings_event",
        lambda event, outcome="success", **kw: seen.append(
            (event, outcome, kw.get("reason"))),
    )
    return seen


def test_refusing_a_live_import_outlook_cannot_reach_says_so(
    client, db, monkeypatch
):
    """Review of lot 1a (L4): Graph unconfigured — the old route (and the
    service as first hoisted) answered a green « Rendez-vous refusé. »,
    while the client's meeting was still booked and nobody had told him.
    Lot 1b's connector would have relayed that as a clean refusal."""
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured",
                        lambda: False)
    calls = []
    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        lambda *a, **k: calls.append(a))
    logs = _spy_refuse_log(monkeypatch)
    _booking(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert calls == []
    # Red, never the green « Rendez-vous refusé. » the old code printed.
    assert "message" not in q and "erreur" in q
    assert "n'a PAS été annulée" in q["erreur"]
    assert q["erreur"] == rendez_vous.ANNULATION_OUTLOOK_NON_TENTEE
    assert _stored(db)["confirmation"] == "refusée"   # the decision stands
    assert logs == [("reception_rdv_refuse", "refused", "not_configured")]


def test_a_live_import_without_an_event_id_is_warned_too(db, graph,
                                                         monkeypatch):
    logs = _spy_refuse_log(monkeypatch)
    _booking(db, graph_event_id="")
    _h, errors, report = rendez_vous.refuser("h1", expected_etag="e-h1")
    assert errors == [] and graph == []
    assert report["graph_attempted"] is False
    assert report["warning"], "a still-booked client must never read as done"
    assert report["warning"] == rendez_vous.ANNULATION_OUTLOOK_NON_TENTEE
    assert report["local_written"] is True
    assert logs == [("reception_rdv_refuse", "refused", "sans_evenement")]


def test_removing_a_client_cancelled_import_is_not_warned(db, monkeypatch):
    """An annulée_client card needs no Outlook call — and no warning,
    whether Graph is configured or not."""
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured",
                        lambda: False)
    logs = _spy_refuse_log(monkeypatch)
    _booking(db, confirmation="annulée_client", graph_event_id="")
    _h, errors, report = rendez_vous.refuser("h1", expected_etag="e-h1")
    assert errors == [] and report["warning"] == ""
    assert logs == [("reception_rdv_refuse", "success", None)]


def test_a_confirmed_rendez_vous_is_not_refused_here(client, db, graph):
    _booking(db, confirmation="")
    before = _stored(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert graph == []
    assert q["erreur"] == rendez_vous.DEJA_CONFIRME_NE_SE_REFUSE_PLUS
    assert _stored(db) == before


def test_a_refusal_racing_a_confirmation_tombstones_the_event(
    client, db, monkeypatch
):
    """Another tab confirmed the import during the Graph call: it entered
    DAV. The meeting is cancelled, so the refusal stands — and the event
    must leave the phone (a bump alone would leave it there)."""
    _booking(db)
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)

    def cancel_while_confirmed_elsewhere(gid, motif=""):
        doc = _stored(db)
        doc.update(confirmation="", etag="e-other-tab")
        db.external_write("hearings/h1", doc)

    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        cancel_while_confirmed_elsewhere)
    _query(client.post("/reception/rdv/h1/refuser",
                       data={"expected_etag": "e-h1"}))
    assert _stored(db)["confirmation"] == "refusée"
    assert db.peek("dav_sync/general/tombstones/h1") is not None
    assert _ctag(db) != "c0"


def test_without_graph_the_write_is_guarded_by_the_version(db, monkeypatch):
    """No external effect happened, so nothing forbids the check at commit."""
    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: False)
    _booking(db)
    _race_on_commit(db, title="Consultation (déplacée)", etag="e-sync")
    _h, errors, report = rendez_vous.refuser("h1", expected_etag="e-h1")
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert report["graph_attempted"] is False
    assert _stored(db)["confirmation"] == "à_confirmer"


def test_refusing_twice_writes_nothing_the_second_time(client, db, graph):
    _booking(db)
    client.post("/reception/rdv/h1/refuser", data={"expected_etag": "e-h1"})
    after = _stored(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": after["etag"]}))
    assert q["message"] == "Ce rendez-vous était déjà refusé."
    assert graph == [("EVT-h1", rendez_vous.REFUS_MOTIF)]   # once
    assert _stored(db) == after


def test_a_double_click_on_refuse_never_calls_graph_twice(client, db, graph):
    _booking(db)
    client.post("/reception/rdv/h1/refuser", data={"expected_etag": "e-h1"})
    after = _stored(db)
    q = _query(client.post("/reception/rdv/h1/refuser",
                           data={"expected_etag": "e-h1"}))
    assert q["message"] == "Ce rendez-vous était déjà refusé."
    assert len(graph) == 1
    assert _stored(db) == after


def test_the_decision_writer_refuses_any_other_value(db):
    _booking(db)
    before = _stored(db)
    for value in ("annulée_client", "à_confirmer", "confirmée"):
        doc, errors, _prev = hearing_model.set_bookings_confirmation(
            "h1", value)
        assert doc is None and errors
    assert _stored(db) == before


# ── Le câblage, vu du source ────────────────────────────────────────────


def _called_names(path: str) -> set[str]:
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute) else
                      getattr(f, "id", ""))
    return names


def test_the_decisions_read_only_through_strict_readers():
    """The fail-open readers answered an outage with « rien en attente » or
    « introuvable ». Neither may come back into the decision path."""
    service = _called_names(rendez_vous.__file__)
    assert {"list_hearings", "get_hearing", "list_parties"} & service == set()
    assert {"list_bookings_strict", "get_hearing_strict",
            "index_by_email_strict"} <= service
    route = _called_names(reception.__file__)
    assert "list_hearings" not in route


def test_the_route_no_longer_writes_a_decision_itself():
    """Confirm and refuse go through the service: the route never calls
    Graph nor writes the confirmation gate (the divergence route still
    uses update_hearing, for the slot and the divergence)."""
    import inspect
    for fn, verb in ((reception.rdv_confirmer, "confirmer"),
                     (reception.rdv_refuser, "refuser")):
        src = inspect.getsource(fn)
        assert f"rendez_vous.{verb}(" in src      # not vacuous
        assert "update_hearing" not in src
        assert "annuler_reservation" not in src
        assert "bump_ctag" not in src


# ══════════════════════════════════════════════════════════════════════
# 4. Divergences (la route n'a pas bougé — ses espions restent)
# ══════════════════════════════════════════════════════════════════════


def _spy_update(monkeypatch):
    """Record each update as the ONE merged payload the model would write.

    The spy runs the model's own key check, so a route that put a
    server-owned key back in ``data`` fails here instead of in production.
    """
    calls = []

    def _update(i, d, *, server_fields=None, expected_etag=None):
        assert hearing_model.update_key_errors(d, server_fields) == [], (
            d, server_fields,
        )
        merged = {**d, **(server_fields or {})}
        calls.append((i, merged))
        return {"id": i, **merged}, []

    monkeypatch.setattr(reception, "update_hearing", _update)
    return calls


def _spy_bump(monkeypatch):
    bumps = []
    monkeypatch.setattr(reception, "bump_ctag", lambda name: bumps.append(name))
    return bumps


def _spy_tombstones(monkeypatch):
    records = []
    monkeypatch.setattr(reception, "record_tombstone",
                        lambda name, rid: records.append((name, rid)))
    return records


def _seeded(**over) -> dict:
    base = {"id": "h1", "source": "bookings", "confirmation": "",
            "dossier_id": "", "graph_event_id": "EVT1"}
    base.update(over)
    return base


def test_divergence_appliquer_updates_slot_and_bumps(client, monkeypatch):
    nd = datetime(2026, 9, 5, 14, 0, tzinfo=UTC).isoformat()
    nf = datetime(2026, 9, 5, 15, 0, tzinfo=UTC).isoformat()
    div = {"motif": "modifié_côté_client", "nouveau_debut": nd,
           "nouveau_fin": nf, "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _seeded(bookings_divergence=div))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    client.post("/reception/rdv/h1/divergence/appliquer")
    data = updates[0][1]
    assert data["bookings_divergence"] is None
    assert data["start_datetime"] == datetime(2026, 9, 5, 14, 0, tzinfo=UTC)
    assert bumps == ["general"]


@pytest.mark.parametrize("action", ["ignorer", "conserver"])
def test_divergence_ignorer_marks_vu_and_bumps(client, monkeypatch, action):
    """Flipped by the finitions (sync-2) — this test used to pin the
    MISSING bump. Dismissing the alert changes nothing the phone shows, but
    update_hearing regenerates the etag of a CONFIRMED (DAV-listed)
    rendez-vous: without the bump the phone keeps the old etag and its next
    edit of the event, sent with If-Match on it, answers 412."""
    motif = "modifié_côté_client" if action == "ignorer" else "annulé_côté_client"
    div = {"motif": motif, "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _seeded(bookings_divergence=div,
                                          dossier_id="d1"))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    records = _spy_tombstones(monkeypatch)
    client.post(f"/reception/rdv/h1/divergence/{action}")
    assert updates[0][1]["bookings_divergence"]["vu"] is True
    assert bumps == ["dossier:d1"]
    assert records == []  # the event stays live: no tombstone


def test_divergence_annuler_tombstones_and_bumps(client, monkeypatch):
    """A confirmed (synced) event leaving the DAV live set MUST record a
    tombstone — a CTag bump alone leaves the cancelled meeting on DavX5."""
    div = {"motif": "annulé_côté_client", "vu": False}
    monkeypatch.setattr(reception, "get_hearing",
                        lambda i: _seeded(bookings_divergence=div))
    updates = _spy_update(monkeypatch)
    bumps = _spy_bump(monkeypatch)
    records = _spy_tombstones(monkeypatch)
    client.post("/reception/rdv/h1/divergence/annuler")
    assert updates[0][1]["confirmation"] == "annulée_client"
    assert records == [("general", "h1")]
    assert bumps == ["general"]


def test_divergence_unknown_action_rejected(client, monkeypatch):
    monkeypatch.setattr(reception, "get_hearing", lambda i: _seeded())
    updates = _spy_update(monkeypatch)
    r = client.post("/reception/rdv/h1/divergence/zzz")
    assert "erreur=" in r.headers["Location"] and not updates
