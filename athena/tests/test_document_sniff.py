"""Première couverture directe de `_sniff_content_type` (décision 2026-08-11 :
ZIP + .eml + .msg versables) + `_validate_file` + `upload_document` bout-en-bout.

Les deux signatures de conteneur sont ambiguës (PK = tout zip, OLE2 = tout
document composé) : l'extension tranche, et tout couple non prévu est refusé
— fail-closed. `.eml` n'a aucune signature : heuristique d'en-tête RFC 5322
SANS regex (doctrine de linéarité CWE-1333), évaluée en DERNIER pour qu'une
magie réelle gagne toujours sur elle.
"""

import io
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.document as doc


_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 24
_PK = b"\x50\x4b\x03\x04" + b"\x00" * 26


def _sniff(data: bytes, ext: str):
    return doc._sniff_content_type(io.BytesIO(data), ext)


# ── Magies réelles (comportement historique inchangé) ────────────────────


@pytest.mark.parametrize("data,ext,attendu", [
    (b"%PDF-1.7 ...", ".pdf", "application/pdf"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 8, ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ".png", "image/png"),
    (b"\x49\x49\x2a\x00" + b"\x00" * 8, ".tif", "image/tiff"),
    (b"\x4d\x4d\x00\x2a" + b"\x00" * 8, ".tiff", "image/tiff"),
])
def test_sniff_magies_reelles(data, ext, attendu):
    assert _sniff(data, ext) == attendu


# ── PK (zip) : l'extension tranche ───────────────────────────────────────


def test_sniff_pk_par_extension():
    assert _sniff(_PK, ".docx") == _DOCX_MIME
    assert _sniff(_PK, ".zip") == "application/zip"
    # Tout autre couple PK + extension est refusé (fail-closed).
    assert _sniff(_PK, ".xlsx") is None
    assert _sniff(_PK, "") is None


def test_sniff_zip_vide_refuse():
    # Une archive VIDE commence PK\x05\x06 (pas PK\x03\x04) — aucune valeur
    # probante, refus délibéré.
    assert _sniff(b"\x50\x4b\x05\x06" + b"\x00" * 18, ".zip") is None


# ── OLE : l'extension tranche ────────────────────────────────────────────


def test_sniff_ole_par_extension():
    assert _sniff(_OLE, ".doc") == "application/msword"
    assert _sniff(_OLE, ".msg") == "application/vnd.ms-outlook"
    # Un conteneur OLE sous toute autre extension est refusé (fail-closed —
    # avant 2026-08-11 il sniffait msword et échouait plus haut au contrôle
    # d'accord extension ; même issue, chemin plus franc).
    assert _sniff(_OLE, ".pdf") is None


# ── .eml : heuristique d'en-tête, fail-closed ────────────────────────────


@pytest.mark.parametrize("data", [
    b"Return-Path: <a@b.example>\r\nFrom: a@b.example\r\n\r\ncorps",
    b"From: a@b.example\nTo: c@d.example\n\ncorps",
    b"\xef\xbb\xbfDate: Mon, 10 Aug 2026 08:00:00 -0400\r\n\r\nx",  # BOM
])
def test_sniff_eml_accepte(data):
    assert _sniff(data, ".eml") == "message/rfc822"


@pytest.mark.parametrize("data", [
    b"",
    b"\x00\x01\x02\x03 binaire",
    b"bonjour tout le monde, aucun en-tete ici\n",
    b": valeur sans nom\r\n",
    b"From a@b.example Sat Jan  5 09:14:16 2008\n",  # mbox : espace avant ':'
    b"X" * 100 + b": nom d'en-tete trop long (> 77)\n",
])
def test_sniff_eml_fail_closed(data):
    assert _sniff(data, ".eml") is None


def test_sniff_eml_exige_l_extension():
    # La forme d'en-tête seule ne suffit jamais : l'extension .eml est requise.
    assert _sniff(b"From: a@b.example\n", ".txt") is None
    assert _sniff(b"From: a@b.example\n", ".pdf") is None


def test_sniff_magie_reelle_gagne_sur_eml():
    # Un PDF renommé .eml sniffe PDF — le contrôle d'accord extension le
    # refusera ensuite dans upload_document, avec le bon message.
    assert _sniff(b"%PDF-1.4 x", ".eml") == "application/pdf"


def test_sniff_repositionne_le_flux():
    # La sonde est passée de 8 à 512 octets — le flux doit toujours être
    # rendu au début (le même objet part ensuite vers upload_from_file).
    flux = io.BytesIO(b"Return-Path: <a@b>\r\n" + b"y" * 4096)
    assert doc._sniff_content_type(flux, ".eml") == "message/rfc822"
    assert flux.tell() == 0


# ── _validate_file : les nouvelles extensions ────────────────────────────


def test_validate_file_nouvelles_extensions():
    for nom in ("pieces.zip", "courriel.eml", "message.msg"):
        assert doc._validate_file(nom, 100) == []
    for nom in ("archive.7z", "archive.rar"):
        erreurs = doc._validate_file(nom, 100)
        assert erreurs and "courriels (EML/MSG)" in erreurs[0]


# ── upload_document bout-en-bout (stockage + Firestore mockés) ───────────


def _mock_storage(monkeypatch):
    blob = mock.Mock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(doc.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(
        doc, "db",
        mock.Mock(collection=lambda n: mock.Mock(document=lambda i: mock.Mock())),
    )
    return blob


def test_upload_document_zip_bout_en_bout(monkeypatch):
    blob = _mock_storage(monkeypatch)
    contenu = _PK + b"\x00" * 64
    document, erreurs = doc.upload_document(
        "d1", "2026-001", io.BytesIO(contenu), "pieces.zip",
        len(contenu), {"category": "pièce"}, "u1",
    )
    assert erreurs == []
    assert document["file_type"] == "application/zip"
    assert (
        blob.upload_from_file.call_args.kwargs["content_type"]
        == "application/zip"
    )
    # Revue 2026-08-11 : disposition posée sur l'OBJET, si bien qu'un URL
    # signé sans override de requête sert quand même en pièce jointe.
    assert blob.content_disposition == "attachment"


def test_upload_document_eml_bout_en_bout(monkeypatch):
    blob = _mock_storage(monkeypatch)
    contenu = b"From: client@exemple.com\r\nSubject: Piece\r\n\r\nBonjour"
    document, erreurs = doc.upload_document(
        "d1", "2026-001", io.BytesIO(contenu), "courriel.eml",
        len(contenu), {"category": "pièce"}, "u1",
    )
    assert erreurs == []
    assert document["file_type"] == "message/rfc822"
    assert blob.content_disposition == "attachment"


def test_upload_document_refuse_desaccord_extension(monkeypatch):
    blob = _mock_storage(monkeypatch)
    document, erreurs = doc.upload_document(
        "d1", "2026-001", io.BytesIO(_OLE), "piece.pdf",
        len(_OLE), {"category": "pièce"}, "u1",
    )
    assert document is None
    assert erreurs and "aucun format autorisé" in erreurs[0]
    blob.upload_from_file.assert_not_called()
