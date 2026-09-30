"""Le formulaire du dossier ferme, rouvre et resynchronise le téléphone
par ``services/dossier_dav`` (lot 4a).

Les VRAIES routes, les VRAIS gabarits, les VRAIS modèles, au-dessus du faux
Firestore partagé (le client est le vrai) : on relit ce qui est STOCKÉ.

Ce que la route faisait avant ce lot, et que ces tests refusent :

* elle écrivait le statut PUIS purgeait sur des listes lues en échec
  ouvert : une panne de lecture fermait le dossier sans rien tombstoner,
  bumpait le CTag, et la collection quittait la découverte — tout restait
  sur le téléphone, sans une erreur ;
* elle ne disait RIEN d'une purge qui échouait après l'écriture du statut
  (l'exception remontait en 500 sur une sauvegarde pourtant faite), et
  n'offrait aucun moyen de la reprendre.
"""

import os
import pathlib
import re
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
    import dav.sync as dav_sync  # its db is patched below
    from models import dossier as dossier_model
    from models import note as note_model
    from models import task as task_model
    import routes.doc_templates as doc_templates_routes
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.hearings as hearings_routes
    import routes.invoices as invoices_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.reception as reception_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    from services import dossier_dav

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, dossier_dav)

UTC = timezone.utc
_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               time_expenses_routes.time_expenses_bp,
               documents_routes.documents_bp, notes_routes.notes_bp,
               tasks_routes.tasks_bp, protocols_routes.protocols_bp,
               hearings_routes.hearings_bp,
               doc_templates_routes.doc_templates_bp,
               invoices_routes.invoices_bp,
               # The detail header links « Demander des documents ».
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


@pytest.fixture
def dossier(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    did = doc["id"]
    task, errors = task_model.create_task(
        {"title": "Préparer la requête", "dossier_id": did})
    assert errors == [], errors
    note, errors = note_model.create_note(
        {"title": "Recherche", "content": "x", "category": "recherche",
         "dossier_id": did})
    assert errors == [], errors
    return {"id": did, "members": {task["id"], note["id"]}}


def _form(db, did, status):
    """What the edit form posts — rendered from the stored dossier."""
    stored = db.peek(f"dossiers/{did}")
    return {
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients_json": ('[{"id": "p1", "name": "Jean Tremblay", '
                         '"roles": ["demandeur"]}]'),
        "status": status, "forum_type": "judiciaire",
        "mandate_type": "judiciaire", "fee_type": "hourly",
        "expected_etag": stored["etag"],
    }


def _tombstones(db, did):
    return set(db.peek_collection(f"dav_sync/dossier:{did}/tombstones"))


def _ctag(db, did):
    return (db.peek(f"dav_sync/dossier:{did}") or {}).get("ctag")


def _fail_queries_on(monkeypatch, db, collection):
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _fail_dav_commits(db, did):
    prefix = f"dav_sync/dossier:{did}"

    def hook(info):
        if any(p.startswith(prefix) for _k, p in info.ops):
            raise gexc.ServiceUnavailable("injected commit failure")

    return db.add_commit_hook(hook)


# ══════════════════════════════════════════════════════════════════════
# 1. Fermer / rouvrir par le formulaire
# ══════════════════════════════════════════════════════════════════════


def test_closing_from_the_form_drains_the_phone(client, db, dossier):
    did = dossier["id"]
    resp = client.post(f"/dossiers/{did}", data=_form(db, did, "fermé"))
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/dossiers/{did}")
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"
    assert _tombstones(db, did) == dossier["members"]


def test_reopening_from_the_form_restores_the_phone(client, db, dossier):
    did = dossier["id"]
    client.post(f"/dossiers/{did}", data=_form(db, did, "fermé"))
    ctag = _ctag(db, did)
    resp = client.post(f"/dossiers/{did}", data=_form(db, did, "actif"))
    assert resp.status_code == 302
    assert _tombstones(db, did) == set() and _ctag(db, did) != ctag


def test_an_unreadable_membership_refuses_the_close_at_200(
        client, db, dossier, monkeypatch):
    """Régression — l'ancienne route écrivait « fermé » puis tombstonait des
    listes vides : le dossier quittait le téléphone sans rien retirer."""
    did = dossier["id"]
    form = _form(db, did, "fermé")
    before = _ctag(db, did)
    _fail_queries_on(monkeypatch, db, "notes")

    resp = client.post(f"/dossiers/{did}", data=form)

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "rien n&#39;a été enregistré" in html or "rien n'a été enregistré" in html
    assert db.peek(f"dossiers/{did}")["status"] == "actif"
    assert _tombstones(db, did) == set() and _ctag(db, did) == before
    # The re-render keeps the SUBMITTED etag: the retry is still guarded.
    assert _ETAG_INPUT.findall(html) == [form["expected_etag"]]


def test_a_stale_close_writes_no_dav_marker(client, db, dossier):
    did = dossier["id"]
    form = _form(db, did, "fermé")
    stored = db.peek(f"dossiers/{did}")
    db.external_write(f"dossiers/{did}",
                      {**stored, "sommaire": "ailleurs",
                       "etag": "11111111-2222-4333-8444-555555555555"})
    before = _ctag(db, did)
    resp = client.post(f"/dossiers/{did}", data=form)
    assert resp.status_code == 200
    assert "Cet élément a été modifié entre-temps." in resp.get_data(as_text=True)
    assert db.peek(f"dossiers/{did}")["status"] == "actif"
    assert _tombstones(db, did) == set() and _ctag(db, did) == before


def test_a_drain_that_fails_after_the_save_warns_on_the_detail_page(
        client, db, dossier):
    """The status IS written — a refusal would lie, a plain success would
    hide a phone left out of step: the detail page says so and offers the
    resync."""
    did = dossier["id"]
    remove = _fail_dav_commits(db, did)
    resp = client.post(f"/dossiers/{did}", data=_form(db, did, "fermé"))
    remove()
    assert resp.status_code == 302
    assert "avertissement=dav_fermeture" in resp.headers["Location"]
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"

    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La fermeture est enregistrée mais la synchronisation du " \
           "téléphone est incomplète" in page
    resync = f'action="/dossiers/{did}/dav/resync"'
    banner = page[page.index("La fermeture est enregistrée"):]
    assert resync in banner[:600]
    assert 'name="csrf_token" value="tok"' in banner[:600]


def test_a_restore_that_fails_says_reouverture(client, db, dossier):
    did = dossier["id"]
    client.post(f"/dossiers/{did}", data=_form(db, did, "fermé"))
    remove = _fail_dav_commits(db, did)
    resp = client.post(f"/dossiers/{did}", data=_form(db, did, "actif"))
    remove()
    assert "avertissement=dav_reouverture" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La réouverture est enregistrée" in page


def test_an_archiving_whose_drain_fails_says_archivage_not_fermeture(
        client, db, dossier):
    """Régression (revue 4a) — un archivage dont la purge échouait
    s'annonçait « La fermeture est enregistrée » : le bandeau disait une
    chose qui n'avait pas eu lieu."""
    did = dossier["id"]
    remove = _fail_dav_commits(db, did)
    resp = client.post(f"/dossiers/{did}", data=_form(db, did, "archivé"))
    remove()
    assert db.peek(f"dossiers/{did}")["status"] == "archivé"
    assert "avertissement=dav_archivage" in resp.headers["Location"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert ("L&#39;archivage est enregistré" in page
            or "L'archivage est enregistré" in page)
    assert "La fermeture est enregistrée" not in page


def test_an_unknown_old_status_never_claims_a_reopening(client, db, dossier):
    """Régression (revue 4a) — la pré-lecture du statut échoue (inconnu,
    jamais deviné), le dossier ACTIF est simplement modifié, et le
    rétablissement qui suit échoue : le bandeau annonçait « La réouverture
    est enregistrée » pour un dossier que personne n'avait rouvert."""
    did = dossier["id"]
    form = _form(db, did, "actif")
    server = db._fake_server
    real = server.batch_get_documents
    failed = []

    def fail_first_dossier_read(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if not failed and f"dossiers/{did}" in names:
            failed.append(1)
            raise gexc.ServiceUnavailable("injected pre-read failure")
        return real(request, metadata=metadata, **kwargs)

    server.batch_get_documents = fail_first_dossier_read
    remove = _fail_dav_commits(db, did)
    try:
        resp = client.post(f"/dossiers/{did}", data=form)
    finally:
        remove()
        server.batch_get_documents = real
    assert failed == [1]
    assert resp.status_code == 302
    assert "avertissement=dav_enregistrement" in resp.headers["Location"]
    assert db.peek(f"dossiers/{did}")["status"] == "actif"
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Le dossier est enregistré mais la synchronisation" in page
    assert "réouverture est enregistrée" not in page


@pytest.mark.parametrize("path", ["/dossiers/%20", "/dossiers/%20/dav/resync"])
def test_a_blank_dossier_id_goes_to_the_list_never_a_500(client, db, path):
    """Régression (revue 4a) — le service refuse un id blanc par une
    ValueError (ses lecteurs liraient TOUTE la collection) : une URL
    fabriquée « /dossiers/%20 » répondait 500. Rien n'est écrit."""
    db.reset_logs()
    resp = client.post(path, data={"status": "fermé", "title": "x",
                                   "file_number": "1"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dossiers/")
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 2. « Resynchroniser le téléphone »
# ══════════════════════════════════════════════════════════════════════


def test_the_resync_button_repairs_and_redirects_with_a_message(
        client, db, dossier):
    did = dossier["id"]
    remove = _fail_dav_commits(db, did)
    client.post(f"/dossiers/{did}", data=_form(db, did, "fermé"))
    remove()
    assert _tombstones(db, did) == set()

    resp = client.post(f"/dossiers/{did}/dav/resync")
    assert resp.status_code == 302
    assert "message=dav_resync" in resp.headers["Location"]
    assert _tombstones(db, did) == dossier["members"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Synchronisation du téléphone refaite" in page


def test_a_failed_resync_redirects_with_an_error(client, db, dossier, monkeypatch):
    did = dossier["id"]
    _fail_queries_on(monkeypatch, db, "tasks")
    ctag = _ctag(db, did)
    resp = client.post(f"/dossiers/{did}/dav/resync")
    assert resp.status_code == 302
    assert "erreur=dav_resync" in resp.headers["Location"]
    assert _ctag(db, did) == ctag  # nothing written


def test_the_resync_of_a_missing_dossier_goes_to_the_list(client, db):
    resp = client.post("/dossiers/a0000000-0000-4000-8000-000000000000/dav/resync")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dossiers/")


def test_the_header_always_offers_the_resync_with_its_csrf_token(
        client, db, dossier):
    did = dossier["id"]
    page = client.get(f"/dossiers/{did}").get_data(as_text=True)
    form = re.search(
        r'<form method="post" action="/dossiers/[^"]+/dav/resync">(.*?)</form>',
        page, re.S)
    assert form, "resync form missing from the header"
    assert 'name="csrf_token" value="tok"' in form.group(1)
    assert 'aria-label="Resynchroniser le téléphone"' in form.group(1)


@pytest.mark.parametrize("query", [
    "?message=Votre+compte+est+suspendu",
    "?erreur=%3Cscript%3Ealert(1)%3C/script%3E",
    "?avertissement=autre",
])
def test_an_unknown_banner_code_renders_nothing(client, db, dossier, query):
    """Closed codes only: a crafted link cannot put words on the page."""
    page = client.get(f"/dossiers/{dossier['id']}{query}").get_data(as_text=True)
    assert "suspendu" not in page and "alert(1)" not in page
    assert "bg-amber-50 border border-amber-200" not in page


# ══════════════════════════════════════════════════════════════════════
# 3. Les classes du balisage existent dans l'artefact compilé
# ══════════════════════════════════════════════════════════════════════


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_new_markup_uses_only_compiled_classes(client, db, dossier):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    did = dossier["id"]
    pages = [client.get(f"/dossiers/{did}?{q}").get_data(as_text=True)
             for q in ("message=dav_resync", "erreur=dav_resync",
                       "avertissement=dav_fermeture")]
    snippets = []
    for page in pages:
        for marker in ("Synchronisation du téléphone refaite",
                       "La resynchronisation du téléphone",
                       "La fermeture est enregistrée"):
            if marker in page:
                i = page.index(marker)
                snippets.append(page[max(0, i - 300):i + 700])
    head = re.search(
        r'<form method="post" action="/dossiers/[^"]+/dav/resync">.*?</form>',
        pages[0], re.S)
    snippets.append(head.group(0))
    assert len(snippets) == 4
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    assert classes
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent


def test_the_resync_route_is_a_post_behind_login(db, dossier):
    """Its own POST on the (CSRF-enforced) dossiers blueprint — never a GET,
    never on an exempt prefix — and nothing runs without a session."""
    from flask import Blueprint
    from flask import Flask as _F
    app = _F(__name__)
    app.secret_key = "t"
    auth_stub = Blueprint("auth", __name__, url_prefix="/auth")
    auth_stub.add_url_rule("/login", "login", lambda: "login")
    app.register_blueprint(auth_stub)
    app.register_blueprint(dossiers_routes.dossiers_bp)
    methods = {r.rule: r.methods for r in app.url_map.iter_rules()
               if r.rule.endswith("/dav/resync")}
    assert methods == {"/dossiers/<dossier_id>/dav/resync": {"POST", "OPTIONS"}}

    did = dossier["id"]
    ctag = _ctag(db, did)
    resp = app.test_client().post(f"/dossiers/{did}/dav/resync")
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]
    assert _ctag(db, did) == ctag
