"""Chemins de service des nouveaux types (décision 2026-08-11).

Deux invariants structurels — plus une convention de gabarit :
1. la page détail ne mint l'URL signé INLINE que pour les types
   prévisualisables (PDF + images) ; ZIP/.eml/.msg n'en reçoivent jamais
   et tombent sur le repli « Télécharger » ;
2. l'URL signé de téléchargement force `attachment` (nom RFC 6266) et le
   Content-Type stocké (sniffé), avec l'extension déterministe de
   `_DOWNLOAD_EXTENSIONS` quand le nom d'affichage n'en porte pas.
"""

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

with mock.patch("google.cloud.firestore.Client"):
    import models.document as doc_model
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


def _doc(file_type: str) -> dict:
    return {
        "id": "doc1", "dossier_id": "d1", "folder_id": None,
        "file_type": file_type, "file_size": 4,
        "display_name": "Pièce", "filename": "piece",
        "storage_path": "users/u1/dossiers/d1/documents/doc1/piece",
    }


def _preparer_detail(monkeypatch, file_type: str) -> tuple[mock.Mock, dict]:
    monkeypatch.setattr(rd, "get_document", lambda i: _doc(file_type))
    signed = mock.Mock(return_value="https://signed.example/x")
    monkeypatch.setattr(rd, "get_signed_url", signed)
    monkeypatch.setattr(rd, "get_folder_breadcrumb", lambda d, f: [])
    monkeypatch.setattr(rd, "get_folder_tree", lambda d: [])
    captures: dict = {}
    monkeypatch.setattr(
        rd, "render_template",
        lambda gabarit, **ctx: captures.update(ctx) or "ok",
    )
    return signed, captures


@pytest.mark.parametrize("file_type", [
    "application/zip", "message/rfc822", "application/vnd.ms-outlook",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
])
def test_detail_ne_mint_pas_l_url_inline_hors_previsualisable(
    web, monkeypatch, file_type
):
    signed, captures = _preparer_detail(monkeypatch, file_type)
    reponse = web.get("/documents/doc1")
    assert reponse.status_code == 200
    signed.assert_not_called()          # aucun signBlob dépensé non plus
    assert captures["signed_url"] is None


def test_detail_mint_l_url_inline_pour_un_pdf(web, monkeypatch):
    signed, captures = _preparer_detail(monkeypatch, "application/pdf")
    reponse = web.get("/documents/doc1")
    assert reponse.status_code == 200
    signed.assert_called_once_with("doc1")
    assert captures["signed_url"] == "https://signed.example/x"


# ── get_signed_url(download=True) : disposition + extension ──────────────


def _preparer_signature(monkeypatch, document: dict) -> mock.Mock:
    monkeypatch.setattr(doc_model, "get_document", lambda i: document)
    blob = mock.Mock()
    blob.generate_signed_url.return_value = "https://signed.example/dl"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(doc_model.storage, "bucket", lambda: bucket)
    creds = mock.Mock(service_account_email="sa@example.iam", token="jeton")
    monkeypatch.setattr(doc_model.google.auth, "default", lambda: (creds, "p"))
    return blob


def test_signed_url_download_force_attachment_et_extension_zip(monkeypatch):
    document = _doc("application/zip")
    document["display_name"] = "Pièces jointes"      # sans extension, accent
    blob = _preparer_signature(monkeypatch, document)

    url = doc_model.get_signed_url("doc1", download=True)
    assert url == "https://signed.example/dl"
    qp = blob.generate_signed_url.call_args.kwargs["query_parameters"]
    disposition = qp["response-content-disposition"]
    assert disposition.startswith('attachment; filename="')
    assert '.zip"' in disposition        # extension déterministe (pas mimetypes)
    assert "filename*=UTF-8''" in disposition   # nom accenté, repli RFC 6266
    assert qp["response-content-type"] == "application/zip"


def test_signed_url_download_extension_msg_deterministe(monkeypatch):
    # mimetypes.guess_extension ignore application/vnd.ms-outlook sur la
    # plupart des plateformes — la table _DOWNLOAD_EXTENSIONS doit trancher.
    document = _doc("application/vnd.ms-outlook")
    document["display_name"] = "message client"
    blob = _preparer_signature(monkeypatch, document)

    assert doc_model.get_signed_url("doc1", download=True)
    qp = blob.generate_signed_url.call_args.kwargs["query_parameters"]
    assert '.msg"' in qp["response-content-disposition"]
    assert qp["response-content-type"] == "application/vnd.ms-outlook"
