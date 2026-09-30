"""Suppression d'un contact REFUSÉE : dite à l'écran, jamais un nom au journal.

Deux défauts voisins de la règle à rebours du lot 0b (B7), relevés en revue :

* le formulaire web « Supprimer » d'une fiche redirigeait vers la LISTE
  quelle que soit l'issue — un refus (contact lié à un dossier, mandataire
  d'un autre contact) passait pour une suppression réussie, sans un mot, et
  le contact était toujours là ;
* le DELETE CardDAV répondait 500 « Erreur serveur. » à un refus d'affaires,
  et journalisait le TEXTE du refus par ``logger.error`` — « … mandataire de
  Sophie Gagnon » : un nom, que le filtre de caviardage ne touche pas.

Tout passe par le VRAI client Firestore (``tests/_fake_firestore.py``), les
vraies routes et le document STOCKÉ relu.
"""

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.carddav as carddav
    import dav.sync as dav_sync  # its db is patched below
    from models import partie as pm
    import routes.parties as parties_routes

from flask import Blueprint, Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (dav_sync,)

UTC = timezone.utc
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _personne(pid: str, first: str, last: str, **over) -> dict:
    doc = pm._default_doc()
    doc.update({
        "id": pid, "type": "individual", "contact_role": "client",
        "first_name": first, "last_name": last,
        "etag": f"etag-{pid}", "vcard_uid": f"uid-{pid}",
    })
    doc.update(over)
    return doc


@pytest.fixture
def famille(db):
    """Sophie est représentée par sa tutrice Marie : Marie ne se supprime pas."""
    db.seed("parties/marie", _personne("marie", "Marie", "Gagnon"))
    db.seed("parties/sophie", _personne(
        "sophie", "Sophie", "Gagnon",
        mandataires=[{"id": "marie", "kind": "tuteur", "notes": ""}],
    ))
    return db


# ══════════════════════════════════════════════════════════════════════
# Le formulaire web
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    app.register_blueprint(parties_routes.parties_bp)
    # The contact page links to three other blueprints; stubs carry the
    # endpoint names so the REAL template renders without their imports.
    for name, rules in (
        ("doc_templates", (("/gabarits/generer", "generate_modal"),)),
        ("dossiers", (("/dossiers/new", "dossier_new"),
                      ("/dossiers/<dossier_id>", "dossier_detail"))),
        ("reception", (("/reception/inviter", "inviter_form"),)),
    ):
        stub = Blueprint(name, __name__ + "." + name)
        for rule, endpoint in rules:
            stub.add_url_rule(rule, endpoint, lambda **kw: "")
        app.register_blueprint(stub)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def test_a_refused_web_delete_says_so_on_the_contact_page(famille, web):
    """THE web defect: the refusal redirected to the LIST like a success,
    and the contact silently stayed. It must land back on the contact's
    own page, carrying the French reason, the contact intact."""
    resp = web.post("/parties/marie/delete")
    assert resp.status_code == 302
    target = urlparse(resp.headers["Location"])
    assert target.path.endswith("/parties/marie")
    erreur = parse_qs(target.query).get("erreur", [""])[0]
    assert "mandataire de Sophie Gagnon" in erreur
    assert famille.peek("parties/marie") is not None

    page = web.get(resp.headers["Location"])
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Impossible de supprimer" in body
    assert "mandataire de Sophie Gagnon" in body


def test_a_successful_web_delete_still_goes_to_the_list(db, web):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    resp = web.post("/parties/seul/delete")
    assert resp.status_code == 302
    assert urlparse(resp.headers["Location"]).path.endswith("/parties/")
    assert db.peek("parties/seul") is None


def test_the_contact_page_renders_no_error_banner_by_default(famille, web):
    body = web.get("/parties/marie").get_data(as_text=True)
    assert "Impossible de supprimer" not in body


# ══════════════════════════════════════════════════════════════════════
# CardDAV
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def dav(db, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(carddav.carddav_bp)
    return app.test_client()


def test_a_carddav_delete_of_a_mandataire_is_a_french_409_with_no_name_logged(
    famille, dav, caplog
):
    """THE DAV defect: 500 « Erreur serveur. » for a business refusal, and
    the refusal TEXT — a contact's name — logged at ERROR."""
    with caplog.at_level(logging.DEBUG):
        resp = dav.delete("/dav/addressbook/marie.vcf", headers=AUTH)
    assert resp.status_code == 409
    body = resp.get_data(as_text=True)
    assert "mandataire de Sophie Gagnon" in body
    assert famille.peek("parties/marie") is not None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Sophie" not in logged and "Gagnon" not in logged
    assert "Marie" not in logged


def test_a_carddav_delete_whose_check_cannot_run_is_a_503(
    famille, dav, monkeypatch
):
    def _outage(pid):
        raise RuntimeError("lecture impossible")

    monkeypatch.setattr(pm, "list_mandataire_referrers", _outage)
    resp = dav.delete("/dav/addressbook/marie.vcf", headers=AUTH)
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")
    assert famille.peek("parties/marie") is not None


def test_a_carddav_delete_that_fails_to_write_is_still_a_500(
    db, dav, monkeypatch
):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    monkeypatch.setattr(
        pm, "delete_partie",
        lambda pid: (False, pm.PARTIE_DELETE_FAILED),
    )
    monkeypatch.setattr(carddav, "delete_partie", pm.delete_partie)
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 500


def test_a_carddav_delete_of_a_free_contact_still_succeeds(db, dav):
    db.seed("parties/seul", _personne("seul", "Paul", "Roy"))
    resp = dav.delete("/dav/addressbook/seul.vcf", headers=AUTH)
    assert resp.status_code == 204
    assert db.peek("parties/seul") is None
    assert db.peek("dav_sync/parties/tombstones/seul") is not None
