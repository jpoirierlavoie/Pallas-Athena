"""Portail client — fabrique, isolement des routes, garde, APIs (spec L1 §13).

Pins: (a) the route map of client.wsgi's app is EXACTLY the portal set — no
main-service route exists in the process; (g) the before_request guard
(revoked/expired → redirect + purge); CSRF on POSTs; (c) extension/size/
quota validation; (f) double finalization → 409; (k) an enqueue failure at
finalization never fails the submission.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")
os.environ.setdefault("PORTAIL_SECRET_KEY", "test-portail-secret")

from google.api_core.exceptions import PreconditionFailed  # noqa: E402

import client.app as client_app  # noqa: E402
from client import limiter  # noqa: E402
from client.services import invitations, stockage, taches  # noqa: E402


@pytest.fixture(scope="module")
def app():
    with mock.patch("utils.tracing_setup.init_app"):
        with mock.patch.object(client_app, "_init_firebase"):
            application = client_app.create_portail_app()
    application.config["TESTING"] = True
    limiter.enabled = False  # per-test counts must not accumulate
    return application


@pytest.fixture()
def web(app):
    return app.test_client()


def _invitation(**over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "id": "inv1", "type": "documents", "email": "client@exemple.com",
        "statut": "ouverte", "display_label": "Dossier 2026-001",
        "dossier_id": "d1", "created_at": now,
        "expires_at": now + timedelta(days=10),
        "quota_files": 3, "quota_mb": 10,  # small quotas for the tests
        "soumissions": [], "accuses": {},
    }
    base.update(over)
    return base


@pytest.fixture()
def connecte(web, monkeypatch):
    """A logged-in portal session against an active invitation."""
    inv = _invitation()
    monkeypatch.setattr(invitations, "lire", lambda inv_id: dict(inv))
    with web.session_transaction() as s:
        s["inv_id"] = "inv1"
        s["uid"] = "u1"
        s["email"] = "client@exemple.com"
    return inv


def _post_json(web, path, payload):
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        return web.post(path, data=json.dumps(payload),
                        content_type="application/json")


# ── §13.a — route-map isolation ──────────────────────────────────────────


def test_route_map_is_exactly_the_portal_set(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert rules == {
        "/", "/entree", "/session", "/api/renvoi", "/documents",
        "/api/televersement", "/api/finaliser", "/confirmation", "/sante",
        # Formulaire d'ouverture (L3)
        "/ouverture", "/api/intake/etape", "/api/intake/finaliser",
        "/static/<path:filename>",
    }
    # Belt: none of the main service's surfaces exist in this process.
    for interdit in ("/dossiers/", "/mcp", "/dav/", "/auth/login",
                     "/fideicommis/", "/reception/"):
        assert not any(r.startswith(interdit) for r in rules), interdit


def test_cookie_name_and_key_separation(app):
    assert app.config["SESSION_COOKIE_NAME"] == "pa_portail"
    assert app.config["SECRET_KEY"] == "test-portail-secret"
    assert app.config["MAX_CONTENT_LENGTH"] == 1024 * 1024


# ── §13.g — before_request guard ─────────────────────────────────────────


def test_guard_without_session_redirects(web):
    reponse = web.get("/documents")
    assert reponse.status_code == 302
    assert "/entree" in reponse.headers["Location"]


def test_guard_revoked_invitation_redirects_and_purges(web, monkeypatch):
    monkeypatch.setattr(
        invitations, "lire",
        lambda inv_id: _invitation(statut="révoquée"),
    )
    with web.session_transaction() as s:
        s["inv_id"] = "inv1"
    assert web.get("/documents").status_code == 302
    with web.session_transaction() as s:
        assert "inv_id" not in s  # purged


def test_guard_expired_invitation_refused_on_api(web, monkeypatch):
    monkeypatch.setattr(
        invitations, "lire",
        lambda inv_id: _invitation(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        ),
    )
    with web.session_transaction() as s:
        s["inv_id"] = "inv1"
    reponse = _post_json(web, "/api/televersement",
                         {"name": "a.pdf", "size": 10, "content_type": "application/pdf"})
    assert reponse.status_code == 401
    assert "erreur" in reponse.get_json()


def test_exempt_pages_reachable_without_session(web):
    assert web.get("/entree").status_code == 200
    assert web.get("/sante").get_json() == {"statut": "ok"}
    assert web.get("/").status_code == 302  # → /entree


def test_pages_documents_et_confirmation_rendent(web, connecte):
    page = web.get("/documents")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Dossier 2026-001" in html
    assert "strictement personnel" in html
    assert web.get("/confirmation").status_code == 200


# ── CSRF ─────────────────────────────────────────────────────────────────


def test_post_without_csrf_token_is_400(web, connecte):
    reponse = web.post("/api/televersement", data=json.dumps({}),
                       content_type="application/json")
    assert reponse.status_code == 400
    assert "erreur" in reponse.get_json()


def test_csrf_ssl_strict_disabled_for_no_referrer_policy(app):
    # Regression: the portal sends Referrer-Policy: no-referrer, so an HTTPS
    # request carries NO Referer. flask-wtf's SSL-strict check would 400
    # ("Session invalide") on such a request even WITH a valid token — which
    # broke /session in production while the HTTP test client (is_secure
    # False) never tripped it. With WTF_CSRF_SSL_STRICT False, a valid token
    # over HTTPS + no Referer must pass CSRF and reach the view logic.
    assert app.config["WTF_CSRF_SSL_STRICT"] is False

    import re
    # Both requests on the same HTTPS origin so the session cookie (and its
    # CSRF secret) is retained, and is_secure is True (where the strict check
    # lives).
    base = "https://portail.poirierlavoie.ca"
    web = app.test_client()
    # GET /entree renders <meta name="csrf-token" ...>, which mints a real
    # token AND stores its secret in the session cookie the client retains.
    html = web.get("/entree", base_url=base).get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    # Production edge: HTTPS, no Referer header.
    reponse = web.post(
        "/session",
        data=json.dumps({"token": "x", "i": "inv1"}),
        content_type="application/json",
        headers={"X-CSRFToken": token},
        base_url=base,
    )
    # CSRF must PASS (not 400) → the /session logic runs and refuses the fake
    # Firebase token with its own 403. A 400 here would be the CSRF regression.
    assert reponse.status_code != 400, reponse.get_data(as_text=True)
    assert reponse.status_code == 403


# ── §13.c — televersement validation ─────────────────────────────────────


@pytest.mark.parametrize("nom", ["script.exe", "archive.zip", "page.html",
                                 "image.svg", "sans_extension"])
def test_televersement_extension_refusee(web, connecte, nom):
    reponse = _post_json(web, "/api/televersement",
                         {"name": nom, "size": 100, "content_type": "x"})
    assert reponse.status_code == 422


def test_televersement_taille_refusee(web, connecte):
    trop = 201 * 1024 * 1024
    reponse = _post_json(web, "/api/televersement",
                         {"name": "gros.pdf", "size": trop, "content_type": "application/pdf"})
    assert reponse.status_code == 422
    reponse = _post_json(web, "/api/televersement",
                         {"name": "vide.pdf", "size": 0, "content_type": "application/pdf"})
    assert reponse.status_code == 422


def test_televersement_quota_fichiers(web, connecte, monkeypatch):
    monkeypatch.setattr(stockage, "ouvrir_session_reprenable",
                        lambda objet, content_type, size: "https://up.example/s")
    with web.session_transaction() as s:
        s["files_count"] = 3  # quota_files == 3 in the fixture
    reponse = _post_json(web, "/api/televersement",
                         {"name": "a.pdf", "size": 100, "content_type": "application/pdf"})
    assert reponse.status_code == 422


def test_televersement_ouvre_une_session_et_nomme_l_objet(web, connecte, monkeypatch):
    captures = {}

    def _ouvrir(objet, content_type, size):
        captures.update(objet=objet, content_type=content_type, size=size)
        return "https://up.example/s1"

    monkeypatch.setattr(stockage, "ouvrir_session_reprenable", _ouvrir)
    reponse = _post_json(web, "/api/televersement", {
        "name": "Déposition finale.pdf", "size": 1234,
        "content_type": "application/pdf",
    })
    assert reponse.status_code == 200
    donnees = reponse.get_json()
    assert donnees["url"] == "https://up.example/s1"
    assert donnees["objet"].startswith("submissions/inv1/")
    assert donnees["objet"].endswith("/files/001_Déposition finale.pdf")
    assert captures["size"] == 1234


# ── §13.f/§13.k — finalization ───────────────────────────────────────────


def _preparer_batch(web):
    with web.session_transaction() as s:
        s["batch"] = "20260725T120000"


def test_finaliser_ecrit_l_enveloppe_et_signale(web, connecte, monkeypatch):
    enveloppes = []
    monkeypatch.setattr(stockage, "ecrire_enveloppe",
                        lambda inv_id, batch, env: enveloppes.append(env))
    signaux = []
    monkeypatch.setattr(taches, "signaler",
                        lambda event, inv_id, batch=None: signaux.append(event))
    _preparer_batch(web)
    reponse = _post_json(web, "/api/finaliser", {"files": [{
        "objet": "submissions/inv1/20260725T120000/files/001_a.pdf",
        "name": "Nom d'origine intégral (avec accents).pdf",
        "size": 10, "content_type": "application/pdf",
    }]})
    assert reponse.status_code == 200
    assert reponse.get_json()["suivant"].endswith("/confirmation")
    assert signaux == ["soumise"]
    env = enveloppes[0]
    assert env["invitation_id"] == "inv1" and env["dossier_id"] == "d1"
    assert env["files"][0]["name"] == "Nom d'origine intégral (avec accents).pdf"
    # The batch is closed: a new upload would start a fresh one.
    with web.session_transaction() as s:
        assert "batch" not in s


def test_finaliser_double_soumission_est_un_succes_et_purge_le_lot(
    web, connecte, monkeypatch
):
    """L'enveloppe existe déjà → le lot est ACQUIS, donc c'est un succès.

    Le chemin réel est la réponse perdue sur un lien mobile : le navigateur
    réarme le bouton et rejoue le POST. Répondre 409 laissait ``batch`` et les
    compteurs en session — les téléversements suivants tombaient dans un lot
    déjà manifesté (jamais hachés, jamais listés, purgés au « traiter »), tout
    envoi ultérieur re-409ait, et le quota comptait le lot deux fois.
    """
    monkeypatch.setattr(
        stockage, "ecrire_enveloppe",
        mock.Mock(side_effect=PreconditionFailed("exists")),
    )
    _preparer_batch(web)
    reponse = _post_json(web, "/api/finaliser", {"files": [{
        "objet": "submissions/inv1/20260725T120000/files/001_a.pdf",
        "name": "a.pdf", "size": 10, "content_type": "application/pdf",
    }]})
    assert reponse.status_code == 200
    assert reponse.get_json()["suivant"].endswith("/confirmation")
    with web.session_transaction() as s:
        for cle in ("batch", "seq", "files_count", "total_bytes"):
            assert cle not in s


def test_finaliser_echec_enfilage_ne_fait_pas_echouer(web, connecte, monkeypatch):
    ecrit = mock.Mock()
    monkeypatch.setattr(stockage, "ecrire_enveloppe", ecrit)
    monkeypatch.setattr(
        taches, "signaler", mock.Mock(side_effect=RuntimeError("queue down"))
    )
    _preparer_batch(web)
    reponse = _post_json(web, "/api/finaliser", {"files": [{
        "objet": "submissions/inv1/20260725T120000/files/001_a.pdf",
        "name": "a.pdf", "size": 10, "content_type": "application/pdf",
    }]})
    # Envelope written → submission acquired; reconciliation will replay.
    assert reponse.status_code == 200
    assert ecrit.called


def test_finaliser_objet_etranger_refuse(web, connecte, monkeypatch):
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    _preparer_batch(web)
    reponse = _post_json(web, "/api/finaliser", {"files": [{
        "objet": "submissions/AUTRE/20260101T000000/files/001_x.pdf",
        "name": "x.pdf", "size": 10, "content_type": "application/pdf",
    }]})
    assert reponse.status_code == 400


def test_finaliser_sans_batch_400(web, connecte):
    reponse = _post_json(web, "/api/finaliser", {"files": []})
    assert reponse.status_code == 400


# ── /session (§6.4) ──────────────────────────────────────────────────────


def _decoded(**over):
    base = {"uid": "u1", "email": "client@exemple.com",
            "email_verified": True, "portail": True}
    base.update(over)
    return base


def test_session_sans_claim_refusee(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation())
    with mock.patch("firebase_admin.auth.verify_id_token",
                    return_value=_decoded(portail=False)):
        reponse = _post_json(web, "/session", {"token": "t", "i": "inv1"})
    assert reponse.status_code == 403
    assert reponse.get_json()["erreur"] == "Invitation invalide ou expirée."


def test_session_email_mismatch_refusee(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation())
    with mock.patch("firebase_admin.auth.verify_id_token",
                    return_value=_decoded(email="autre@exemple.com")):
        reponse = _post_json(web, "/session", {"token": "t", "i": "inv1"})
    assert reponse.status_code == 403


def test_session_valide_signale_ouverte(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="envoyée"))
    signaux = []
    monkeypatch.setattr(taches, "signaler",
                        lambda event, inv_id, batch=None: signaux.append(event))
    with mock.patch("firebase_admin.auth.verify_id_token",
                    return_value=_decoded()):
        reponse = _post_json(web, "/session", {"token": "t", "i": "inv1"})
    assert reponse.status_code == 200
    assert reponse.get_json()["suivant"].endswith("/documents")
    assert signaux == ["ouverte"]
    with web.session_transaction() as s:
        assert s["inv_id"] == "inv1" and s["uid"] == "u1"


# ── /api/renvoi — anti-enumeration ───────────────────────────────────────


def test_renvoi_reponse_identique_valide_ou_non(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda i: None)
    monkeypatch.setattr(invitations, "chercher_par_email", lambda e: [])
    r1 = _post_json(web, "/api/renvoi",
                    {"courriel": "inconnu@exemple.com"})
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation())
    signale = mock.Mock()
    monkeypatch.setattr(taches, "signaler", signale)
    r2 = _post_json(web, "/api/renvoi",
                    {"courriel": "client@exemple.com", "i": "inv1"})
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json() == r2.get_json()  # byte-identical bodies
    signale.assert_called_once_with("renvoi", "inv1")
