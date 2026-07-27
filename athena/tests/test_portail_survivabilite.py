"""Survivabilité du lien d'invitation du portail (2026-07-27).

Le lien de connexion Firebase est à USAGE UNIQUE : il est consommé AVANT que
la session du portail existe. Tout refus survenant après ce point ne refuse
pas seulement la requête — il détruit l'invitation du client, sans recours.
Ces tests épinglent les chemins qui doivent dégrader **sans rien détruire**,
et la ré-entrée décidée avec le juriste (D-2 : un lot soumis reste ouvert
jusqu'au traitement).

La suite existante ne faisait JAMAIS lever `lire` — elle ne remplaçait que sa
valeur de retour — ce qui est précisément pourquoi la confusion entre « panne
de lecture » et « révocation » est passée inaperçue.
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

import client.app as client_app  # noqa: E402
import client.routes as routes  # noqa: E402
from client import limiter  # noqa: E402
from client.config import INVITATION_MAX_RENVOIS  # noqa: E402
from client.services import invitations, stockage, taches  # noqa: E402


@pytest.fixture(scope="module")
def app():
    with mock.patch("utils.tracing_setup.init_app"):
        with mock.patch.object(client_app, "_init_firebase"):
            application = client_app.create_portail_app()
    application.config["TESTING"] = True
    limiter.enabled = False
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
        "quota_files": 3, "quota_mb": 10,
        "soumissions": [], "accuses": {}, "resend_count": 0,
    }
    base.update(over)
    return base


@pytest.fixture()
def connecte(web, monkeypatch):
    monkeypatch.setattr(invitations, "lire", lambda inv_id: _invitation())
    with web.session_transaction() as s:
        s["inv_id"] = "inv1"
        s["uid"] = "u1"
        s["email"] = "client@exemple.com"
    return _invitation()


def _post_json(web, path, payload):
    with mock.patch("flask_wtf.csrf.validate_csrf", return_value=None):
        return web.post(path, data=json.dumps(payload),
                        content_type="application/json")


def _lire_qui_leve(*_a, **_k):
    raise invitations.LectureIndisponible


def _upload(web, **over):
    charge = {"name": "a.pdf", "size": 10,
              "content_type": "application/pdf"}
    charge.update(over)
    return _post_json(web, "/api/televersement", charge)


# ── Panne de lecture ≠ révocation ────────────────────────────────────────


def test_lecture_indisponible_donne_503_et_preserve_la_session(
        web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire", _lire_qui_leve)
    r = _post_json(web, "/api/finaliser", {"files": []})
    assert r.status_code == 503
    with web.session_transaction() as s:
        assert s.get("inv_id") == "inv1"


def test_lecture_indisponible_sur_session_donne_503(web, monkeypatch):
    """Le site le plus dommageable : le code du lien est DÉJÀ consommé ici."""
    monkeypatch.setattr(invitations, "lire", _lire_qui_leve)
    with mock.patch("firebase_admin.auth.verify_id_token", return_value={
        "portail": True, "email_verified": True,
        "email": "client@exemple.com", "uid": "u1",
    }):
        r = _post_json(web, "/session", {"token": "t", "i": "inv1"})
    assert r.status_code == 503


def test_renvoi_reste_identique_pendant_une_panne(web, monkeypatch):
    monkeypatch.setattr(invitations, "chercher_par_email", lambda e: [])
    monkeypatch.setattr(invitations, "lire", lambda i: None)
    attendu = _post_json(web, "/api/renvoi", {"courriel": "x@exemple.com"})
    monkeypatch.setattr(invitations, "lire", _lire_qui_leve)
    monkeypatch.setattr(invitations, "chercher_par_email", _lire_qui_leve)
    pendant = _post_json(web, "/api/renvoi",
                         {"courriel": "x@exemple.com", "i": "inv1"})
    assert pendant.status_code == attendu.status_code == 200
    assert pendant.get_json() == attendu.get_json()


def test_refus_ne_vide_pas_le_secret_csrf(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="révoquée"))
    with web.session_transaction() as s:
        s["csrf_token"] = "secret-csrf"
    _post_json(web, "/api/finaliser", {"files": []})
    with web.session_transaction() as s:
        assert "inv_id" not in s
        assert s.get("csrf_token") == "secret-csrf"


# ── Renvoi : repli et plafond ────────────────────────────────────────────


def test_renvoi_retombe_sur_une_invitation_active_si_i_est_perime(
        web, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(id="vieille", statut="traitée"))
    monkeypatch.setattr(
        invitations, "chercher_par_email",
        lambda e: [_invitation(id="neuve", statut="ouverte")],
    )
    signale = mock.Mock()
    monkeypatch.setattr(taches, "signaler", signale)
    r = _post_json(web, "/api/renvoi",
                   {"courriel": "client@exemple.com", "i": "vieille"})
    assert r.status_code == 200
    signale.assert_called_once_with("renvoi", "neuve")


def test_renvoi_refuse_au_dela_du_plafond_mais_repond_pareil(web, monkeypatch):
    monkeypatch.setattr(
        invitations, "lire",
        lambda i: _invitation(resend_count=INVITATION_MAX_RENVOIS),
    )
    monkeypatch.setattr(invitations, "chercher_par_email", lambda e: [])
    signale = mock.Mock()
    monkeypatch.setattr(taches, "signaler", signale)
    r = _post_json(web, "/api/renvoi",
                   {"courriel": "client@exemple.com", "i": "inv1"})
    assert r.status_code == 200
    assert r.get_json() == {"message": routes._REPONSE_RENVOI}
    signale.assert_not_called()


def test_message_de_renvoi_porte_le_canal_de_secours():
    """Le plafond étant appliqué en silence, le message constant DOIT donner
    une issue — sinon le client attend un courriel qui ne viendra jamais."""
    assert "737-2525" in routes._REPONSE_RENVOI


# ── D-2 : ré-entrée après « soumise » ────────────────────────────────────


def test_soumise_autorise_encore_le_televersement(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="soumise"))
    monkeypatch.setattr(stockage, "ouvrir_session_reprenable",
                        lambda *a, **k: "https://upload.example/x")
    assert _upload(web).status_code == 200


def test_traitee_est_terminal(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="traitée"))
    assert _upload(web).status_code == 401


def test_traitee_refuse_aussi_la_finalisation(web, connecte, monkeypatch):
    """La dérogation « ne pas orphéliner des octets » ne couvre PAS une
    invitation traitée : ses fichiers de quarantaine sont purgés et son
    enveloppe archivée, donc il n'y a plus rien à sauver — tandis que la
    finalisation appellerait ``ajouter_soumission``, qui remettait le statut à
    « soumise », d'où le client peut de nouveau téléverser. Le lot resté en vol
    est couvert par l'alerte ``lot_abandonne`` de la réconciliation."""
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="traitée"))
    assert _post_json(web, "/api/finaliser", {"files": []}).status_code == 401


def test_soumise_expiree_reste_refusee(web, connecte, monkeypatch):
    """Le piège que le helper combiné existe pour éviter : un simple test
    d'appartenance laisserait téléverser une invitation EXPIRÉE."""
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(
        statut="soumise",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    assert _upload(web).status_code == 401


def test_finaliser_survit_a_un_statut_soumise(web, connecte, monkeypatch):
    """Les octets sont DÉJÀ dans GCS : refuser ici orpheline tout le lot."""
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="soumise"))
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    monkeypatch.setattr(taches, "signaler", mock.Mock())
    with web.session_transaction() as s:
        s["batch"] = "B1"
    r = _post_json(web, "/api/finaliser", {"files": [
        {"objet": "submissions/inv1/B1/files/001_a.pdf", "name": "a.pdf",
         "size": 10, "content_type": "application/pdf"},
    ]})
    assert r.status_code == 200


def test_finaliser_refuse_encore_une_invitation_revoquee(
        web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="révoquée"))
    monkeypatch.setattr(stockage, "ecrire_enveloppe", mock.Mock())
    with web.session_transaction() as s:
        s["batch"] = "B1"
    r = _post_json(web, "/api/finaliser", {"files": [
        {"objet": "submissions/inv1/B1/files/001_a.pdf", "name": "a.pdf",
         "size": 10, "content_type": "application/pdf"},
    ]})
    assert r.status_code == 401


def test_quota_cumule_sur_les_lots_precedents(web, connecte, monkeypatch):
    """Sans cumul, chaque ré-entrée rendrait un quota complet tout neuf."""
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(
        statut="soumise",
        soumissions=[{"batch": "B0", "files_count": 3, "total_bytes": 10}],
    ))
    r = _upload(web)
    assert r.status_code == 422
    assert "Nombre maximal" in r.get_json()["erreur"]


def test_compteur_de_session_ne_double_compte_pas(web, connecte, monkeypatch):
    """Les compteurs stockés restent propres à la session : y replier les
    lots passés les ré-additionnerait à chaque téléversement."""
    monkeypatch.setattr(invitations, "lire", lambda i: _invitation(
        statut="soumise",
        soumissions=[{"batch": "B0", "files_count": 1, "total_bytes": 5}],
    ))
    monkeypatch.setattr(stockage, "ouvrir_session_reprenable",
                        lambda *a, **k: "https://upload.example/x")
    assert _upload(web).status_code == 200
    with web.session_transaction() as s:
        assert s["files_count"] == 1        # et non 2
        assert s["total_bytes"] == 10       # et non 15


# ── /entree réutilise une session vivante ────────────────────────────────


def test_entree_redirige_une_session_vivante_vers_documents(web, connecte):
    r = web.get("/entree?i=inv1")
    assert r.status_code == 302
    assert "/documents" in r.headers["Location"]


def test_entree_ne_deconnecte_pas_sur_un_i_etranger(web, connecte):
    """Un « ?i= » d'une AUTRE invitation ne doit pas tuer une session en cours
    de téléversement."""
    web.get("/entree?i=autre")
    with web.session_transaction() as s:
        assert s.get("inv_id") == "inv1"


def test_entree_ne_livre_pas_la_session_d_autrui_sur_un_i_etranger(
    web, connecte
):
    """L'autre moitié — celle qui manquait.

    Chaque courriel d'invitation pointe /entree?i={id} (le lien Firebase ET
    l'URL de secours), donc un « ?i= » étranger est l'arrivée ORDINAIRE d'un
    2e client sur un navigateur partagé. Le rediriger vers /documents le
    plaçait dans la session du premier : ses fichiers sous le préfixe de
    l'autre invitation, et l'accusé (noms, tailles, SHA-512) expédié à
    l'autre client. _garde prouve que l'INVITATION est vivante, jamais que le
    VISITEUR en est le destinataire.
    """
    r = web.get("/entree?i=autre")
    assert r.status_code == 200          # la page de connexion, pas un 302
    with web.session_transaction() as s:
        assert s.get("inv_id") == "inv1"  # et toujours sans déconnexion


def test_entree_sans_i_reutilise_encore_la_session(web, connecte):
    """Le raccourci de ré-entrée reste entier quand aucune invitation n'est
    nommée (signet, saisie directe)."""
    r = web.get("/entree")
    assert r.status_code == 302
    assert "/documents" in r.headers["Location"]


def test_entree_expiree_ne_ment_pas_et_ne_piege_pas(web, connecte, monkeypatch):
    """Une invitation EXPIRÉE garde le statut « envoyée » : un test par statut
    l'envoyait sur /confirmation, qui affirme « Transmission reçue » — un faux
    accusé — et refermait la boucle / → /entree → /confirmation, sans chemin
    vers « Demander un nouveau lien », le seul cas où un lien neuf est la
    réponse."""
    passe = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(expires_at=passe))
    r = web.get("/entree?i=inv1")
    assert r.status_code == 200


def test_entree_soumise_puis_expiree_mene_a_la_confirmation(
    web, connecte, monkeypatch
):
    """Un lot RÉELLEMENT soumis, lui, a droit à la confirmation.

    Tant que l'invitation reste ouverte, « soumise » mène à /documents (D-2 :
    « j'ai oublié une page »). C'est une fois expirée que la confirmation est
    le bon écran — et elle ne ment pas : la transmission a bien eu lieu.
    """
    passe = datetime.now(timezone.utc) - timedelta(days=1)
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="soumise",
                                              expires_at=passe))
    r = web.get("/entree?i=inv1")
    assert r.status_code == 302
    assert "/confirmation" in r.headers["Location"]


def test_entree_soumise_encore_ouverte_mene_aux_documents(
    web, connecte, monkeypatch
):
    monkeypatch.setattr(invitations, "lire",
                        lambda i: _invitation(statut="soumise"))
    r = web.get("/entree?i=inv1")
    assert r.status_code == 302
    assert "/documents" in r.headers["Location"]


def test_entree_reste_rendue_pendant_une_panne(web, connecte, monkeypatch):
    monkeypatch.setattr(invitations, "lire", _lire_qui_leve)
    r = web.get("/entree?i=inv1")
    assert r.status_code == 200
    with web.session_transaction() as s:
        assert s.get("inv_id") == "inv1"


def test_entree_sans_session_rend_la_page(web):
    assert web.get("/entree").status_code == 200


# ── App Check : jamais destructeur sur /session ──────────────────────────


def test_app_check_ne_bloque_pas_la_creation_de_session(app):
    """Une attestation ratée refusait /session APRÈS que le lien ait été
    consommé — elle détruisait l'invitation. La garde est levée pour ce seul
    point d'entrée (celui-ci garde ses propres contrôles)."""
    from flask import request as rq

    from client import security as psec

    with app.test_request_context("/session", method="POST"):
        # Le routage précède before_request : l'endpoint est déjà résolu.
        assert rq.endpoint == "portail.creer_session"
        assert psec.verify_app_check() is None


def test_app_check_reste_applique_sur_le_renvoi(app, monkeypatch):
    """L'inverse doit rester vrai : /api/renvoi n'est PAS authentifié et son
    effet est un courriel sortant — c'est la surface que App Check protège."""
    from client import security as psec

    monkeypatch.setitem(
        app.config, "RECAPTCHA_ENTERPRISE_SITE_KEY", "site-key"
    )
    with app.test_request_context("/api/renvoi", method="POST"):
        with pytest.raises(Exception) as info:      # werkzeug Unauthorized
            psec.verify_app_check()
        assert "401" in str(info.value)
