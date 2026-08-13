"""Téléversement DIRECT-à-GCS du formulaire juriste (décision 2026-08-12).

Les octets ne transitent jamais par l'application — App Engine Standard
plafonne toute requête à 32 Mo (le miroir du plafond de 32 Mo par réponse
qui a frappé les téléchargements la veille) : ouverture d'une session
reprenable sous staging/{uid}/, PUT navigateur→GCS, puis finalisation qui
sniffe une sonde de 512 octets et ingère par copie côté serveur
(ingest_blob_as_document), le staging étant consommé dans les deux issues.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.documents as rd

from flask import Flask  # noqa: E402


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


@pytest.fixture()
def web_rendu(monkeypatch):
    """App qui rend RÉELLEMENT les gabarits — pour épingler le href que
    l'avocat clique, pas la source du gabarit. Bon marché : base.html
    n'appelle url_for que pour des fichiers statiques (csp_nonce non défini
    se rend vide, inoffensif) ; motif de tests/test_mcp_oauth._make_app."""
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.register_blueprint(rd.documents_bp)
    monkeypatch.setattr(rd, "list_dossiers", lambda: [])
    monkeypatch.setattr(
        rd, "get_dossier",
        lambda did: {"id": did, "file_number": "2026-001", "title": "Essai"},
    )
    monkeypatch.setattr(rd, "get_folder_breadcrumb", lambda d, f: [])
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _post(web, path, payload):
    return web.post(path, data=json.dumps(payload),
                    content_type="application/json")


# ── GET /documents/upload — return_to validé au rendu ────────────────────


@pytest.mark.parametrize("cible,attendu", [
    ("/dossiers/d1?tab=documents", "/dossiers/d1?tab=documents"),
    ("https://evil.example", ""),
    ("//evil.example", ""),
    ("javascript:alert(1)", ""),
])
def test_upload_form_valide_return_to_au_rendu(web, monkeypatch, cible, attendu):
    # Le JS rejoue return_to dans window.location à la fin du téléversement.
    # Le POST multipart supprimé le passait par safe_internal_redirect — la
    # validation vit désormais AU RENDU (revue 2026-08-12 : sans elle, la
    # redirection ouverte régressait).
    monkeypatch.setattr(rd, "list_dossiers", lambda: [])
    captures: dict = {}
    monkeypatch.setattr(rd, "render_template",
                        lambda gabarit, **ctx: captures.update(ctx) or "ok")
    reponse = web.get("/documents/upload", query_string={"return_to": cible})
    assert reponse.status_code == 200
    assert captures["return_to"] == attendu


def test_les_liens_retour_et_annuler_ramenent_au_navigateur_filtre(web_rendu):
    """Le bogue signalé le 2026-08-13 : le repli du SUCCÈS était scopé, mais
    les liens « retour » et « Annuler » — rendus CÔTÉ SERVEUR — retombaient
    sur la liste PLATE (les trois entrées du navigateur ne passent aucun
    return_to). Rendu RÉEL : on épingle le href cliqué, pas la source du
    gabarit — l'épinglage de source du correctif précédent n'avait rien vu.
    NB : l'autoéchappement Jinja rend « & » en « &amp; » dans un attribut."""
    html = web_rendu.get(
        "/documents/upload?dossier_id=d1&folder_id=f1"
    ).get_data(as_text=True)
    scope = "/documents/?dossier_id=d1&amp;folder_id=f1"
    assert html.count(f'href="{scope}"') == 2      # retour + Annuler
    assert 'href="/documents/"' not in html        # jamais la liste plate


def test_les_liens_retour_et_annuler_sans_dossier_de_classement(web_rendu):
    html = web_rendu.get("/documents/upload?dossier_id=d1").get_data(as_text=True)
    assert html.count('href="/documents/?dossier_id=d1&amp;folder_id="') == 2
    assert 'href="/documents/"' not in html


def test_les_liens_retour_et_annuler_sans_dossier(web_rendu):
    # Arrivée générique (aucun dossier) : la liste plate EST l'endroit d'où
    # l'on vient — le repli scopé ne doit pas inventer un dossier.
    html = web_rendu.get("/documents/upload").get_data(as_text=True)
    assert html.count('href="/documents/"') == 2


def test_return_to_garde_priorite_aux_deux_liens(web_rendu):
    html = web_rendu.get(
        "/documents/upload?dossier_id=d1&folder_id=f1"
        "&return_to=/dossiers/d1%3Ftab%3Ddocuments"
    ).get_data(as_text=True)
    assert html.count('href="/dossiers/d1?tab=documents"') == 2
    assert "dossier_id=d1&amp;folder_id=f1" not in html


def test_le_gabarit_replie_vers_le_navigateur_filtre():
    # Après téléversement, le repli du JS vise le navigateur FILTRÉ au
    # dossier (et au dossier de classement) — la sémantique de l'ancien
    # POST multipart, restaurée le 2026-08-13. Épinglage de source.
    chemin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "templates", "documents", "upload.html",
    )
    html = open(chemin, encoding="utf-8").read()
    assert "'?dossier_id=' + encodeURIComponent(dossierId)" in html
    assert "'&folder_id=' + encodeURIComponent(dossierClassement)" in html


# ── /documents/api/televersement ─────────────────────────────────────────


@pytest.mark.parametrize("nom", ["script.exe", "page.html", "sans_extension"])
def test_televersement_extension_refusee(web, nom):
    reponse = _post(web, "/documents/api/televersement",
                    {"name": nom, "size": 100})
    assert reponse.status_code == 422


def test_televersement_taille_refusee(web):
    trop = 201 * 1024 * 1024
    assert _post(web, "/documents/api/televersement",
                 {"name": "gros.zip", "size": trop}).status_code == 422
    assert _post(web, "/documents/api/televersement",
                 {"name": "vide.pdf", "size": 0}).status_code == 422


def test_televersement_ouvre_une_session_staging(web, monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s1"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/documents/api/televersement", {
        "name": "pieces.zip", "size": 130 * 1024 * 1024,
        "content_type": "application/zip",
    })
    assert reponse.status_code == 200
    donnees = reponse.get_json()
    assert donnees["url"] == "https://up.example/s1"
    assert donnees["objet"].startswith("staging/u1/")
    assert donnees["objet"].endswith("/pieces.zip")
    kwargs = blob.create_resumable_upload_session.call_args.kwargs
    assert kwargs["size"] == 130 * 1024 * 1024   # GCS applique le déclaré
    assert kwargs["origin"]                       # CORS porté par la SESSION


# ── /documents/api/finaliser ─────────────────────────────────────────────


def test_finaliser_objet_etranger_400(web):
    # Le client ne nomme jamais que SES objets staging.
    assert _post(web, "/documents/api/finaliser", {
        "objet": "users/u1/dossiers/d1/documents/x/a.pdf",
        "name": "a.pdf", "dossier_id": "d1",
    }).status_code == 400
    assert _post(web, "/documents/api/finaliser", {
        "objet": "staging/autre-uid/x/a.pdf",
        "name": "a.pdf", "dossier_id": "d1",
    }).status_code == 400


def test_finaliser_ingere_et_consomme_le_staging(web, monkeypatch):
    monkeypatch.setattr(
        rd, "get_dossier",
        lambda d: {"id": "d1", "file_number": "2026-001"} if d == "d1" else None,
    )
    blob = mock.MagicMock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    ingest = mock.Mock(return_value=({"id": "doc9"}, []))
    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)

    reponse = _post(web, "/documents/api/finaliser", {
        "objet": "staging/u1/aaaa/pieces.zip", "name": "pieces.zip",
        "dossier_id": "d1", "category": "pièce", "tags": "a, b",
        "folder_id": "", "description": "", "display_name": "",
        "document_date": "",
    })
    assert reponse.status_code == 200
    assert reponse.get_json()["document_id"] == "doc9"
    args = ingest.call_args.args
    assert args[0] is blob and args[1] == "d1" and args[2] == "2026-001"
    assert args[3] == "pieces.zip"
    assert args[4]["tags"] == ["a", "b"]
    blob.delete.assert_called_once()             # staging consommé


def test_finaliser_refus_consomme_le_staging_aussi(web, monkeypatch):
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": "d1", "file_number": "2026-001"})
    blob = mock.MagicMock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(
        rd, "ingest_blob_as_document",
        mock.Mock(return_value=(None, ["Le contenu du fichier ne "
                                       "correspond pas à son extension."])),
    )
    reponse = _post(web, "/documents/api/finaliser", {
        "objet": "staging/u1/aaaa/piece.pdf", "name": "piece.pdf",
        "dossier_id": "d1",
    })
    assert reponse.status_code == 422
    assert "extension" in reponse.get_json()["erreur"]
    blob.delete.assert_called_once()             # des octets refusés non plus
