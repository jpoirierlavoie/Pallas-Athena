"""Réception — versement restreint, traitement de lot, pastille (spec L1 §9).

CI-only style (imports models under the mocked Firestore client). The GET
views render the full base template and are exercised end-to-end manually;
these tests pin the LOGIC: the versement pre-check (restriction au
vocabulaire documents — décision 2026-07-25), the explicit-decision guard
before purging a lot, the provenance fields, and the badge cache fail-open.
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
    import routes.reception as rc

from flask import Flask  # noqa: E402


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rc.reception_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _entree(**over) -> dict:
    base = {
        "objet": "submissions/inv1/b1/files/001_piece.pdf",
        "name": "piece.pdf", "size_declared": 100, "size_gcs": 100,
        "content_type": "application/pdf",
        "sha512": "cafe" * 32, "etat": "reçu", "divergence": None,
    }
    base.update(over)
    return base


def _manifeste(*fichiers) -> dict:
    return {"batch": "b1", "invitation_id": "inv1",
            "files": list(fichiers), "etat_lot": "soumis"}


# ── Pré-contrôle du versement (restriction au vocabulaire actuel) ────────


def test_versable_pdf_recu():
    assert rc._versable(_entree()) is True


@pytest.mark.parametrize("entree", [
    _entree(name="photo.heic", content_type="image/heic"),
    _entree(name="video.mp4"),
    _entree(name="classeur.xlsx"),
    _entree(size_gcs=26 * 1024 * 1024),          # > 25 Mo
    _entree(size_gcs=0),
    _entree(etat="versé"),
    _entree(etat="refusé"),
    _entree(etat="manquant"),
])
def test_versable_refuse(entree):
    assert rc._versable(entree) is False


# ── Verser ───────────────────────────────────────────────────────────────


def _dossier():
    return {"id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie"}


def test_verser_ingere_avec_provenance(web, monkeypatch):
    manifeste = _manifeste(_entree())
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    ecrits = []
    monkeypatch.setattr(rc, "_ecrire_manifeste",
                        lambda i, b, m: ecrits.append(m))
    blob = mock.Mock()
    blob.download_as_bytes.return_value = b"%PDF-1.4 data"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    monkeypatch.setattr(rc, "get_dossier",
                        lambda d: _dossier() if d == "d1" else None)
    monkeypatch.setattr(rc, "get_or_create_folder",
                        lambda d, nom: {"id": "f-portail", "name": nom})
    upload = mock.Mock(return_value=({"id": "doc9"}, []))
    monkeypatch.setattr(rc, "upload_document", upload)

    reponse = web.post(
        "/reception/lots/inv1/b1/fichiers/0/verser",
        data={"dossier_id": "d1", "category": "pièce", "display_name": ""},
    )
    assert reponse.status_code == 302
    assert "message=" in reponse.headers["Location"]

    args = upload.call_args.args
    assert args[0] == "d1" and args[1] == "2026-001"
    assert args[3] == "piece.pdf" and args[6] == "u1"
    metadata = args[5]
    assert metadata["tags"] == ["portail"]
    assert "invitation inv1" in metadata["description"]
    assert ("cafe" * 32) in metadata["description"]
    assert metadata["folder_id"] == "f-portail"
    # Manifest entry flipped to « versé » and persisted.
    assert manifeste["files"][0]["etat"] == "versé"
    assert ecrits


def test_verser_non_versable_refuse_sans_ingestion(web, monkeypatch):
    manifeste = _manifeste(_entree(name="photo.heic"))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    upload = mock.Mock()
    monkeypatch.setattr(rc, "upload_document", upload)

    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert reponse.status_code == 302
    assert "erreur=" in reponse.headers["Location"]
    upload.assert_not_called()
    assert manifeste["files"][0]["etat"] == "reçu"  # untouched


def test_verser_deja_traite_refuse(web, monkeypatch):
    monkeypatch.setattr(rc, "_lire_manifeste",
                        lambda i, b: _manifeste(_entree(etat="versé")))
    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/verser",
                       data={"dossier_id": "d1"})
    assert "erreur=" in reponse.headers["Location"]


# ── Refuser + traiter le lot ─────────────────────────────────────────────


def test_refuser_marque_le_fichier(web, monkeypatch):
    manifeste = _manifeste(_entree())
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste", lambda i, b, m: None)
    reponse = web.post("/reception/lots/inv1/b1/fichiers/0/refuser")
    assert "message=" in reponse.headers["Location"]
    assert manifeste["files"][0]["etat"] == "refusé"


def test_traiter_bloque_si_fichier_recu(web, monkeypatch):
    monkeypatch.setattr(
        rc, "_lire_manifeste",
        lambda i, b: _manifeste(_entree(etat="versé"), _entree()),
    )
    bucket = mock.Mock()
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "erreur=" in reponse.headers["Location"]
    bucket.copy_blob.assert_not_called()  # never purge unexamined files


def test_traiter_archive_et_purge(web, monkeypatch):
    manifeste = _manifeste(_entree(etat="versé"), _entree(etat="refusé"))
    monkeypatch.setattr(rc, "_lire_manifeste", lambda i, b: manifeste)
    monkeypatch.setattr(rc, "_ecrire_manifeste", lambda i, b, m: None)

    fichiers_restants = [mock.Mock(), mock.Mock()]
    sources = {}

    def _blob(nom):
        src = sources.setdefault(nom, mock.Mock())
        src.exists.return_value = True
        return src

    bucket = mock.Mock()
    bucket.blob.side_effect = _blob
    bucket.list_blobs.return_value = fichiers_restants
    monkeypatch.setattr(rc, "_bucket", lambda: bucket)
    statuts = []
    monkeypatch.setattr(rc.pi, "maj_statut",
                        lambda i, s: statuts.append((i, s)) or True)

    reponse = web.post("/reception/lots/inv1/b1/traiter")
    assert "message=" in reponse.headers["Location"]
    assert manifeste["etat_lot"] == "traité"
    # envelope + manifeste copiés sous archive/ puis supprimés.
    copies = [c.args for c in bucket.copy_blob.call_args_list]
    assert {c[2] for c in copies} == {
        "archive/inv1/b1/envelope.json", "archive/inv1/b1/manifeste.json",
    }
    for src in sources.values():
        src.delete.assert_called_once()
    for restant in fichiers_restants:
        restant.delete.assert_called_once()
    assert statuts == [("inv1", "traitée")]


# ── Pastille (cache + fail-open) ─────────────────────────────────────────


def test_compteur_reception_cache_et_fail_open(monkeypatch):
    rc._badge_cache.update(at=0.0, n=None)
    compte = mock.Mock(return_value=3)
    monkeypatch.setattr(rc.pi, "compter_soumises", compte)
    assert rc.compteur_reception() == 3
    assert rc.compteur_reception() == 3
    assert compte.call_count == 1  # cached within the TTL

    rc._badge_cache.update(at=0.0)
    compte.return_value = None  # base « portail » absente → fail-open
    assert rc.compteur_reception() is None
    rc._badge_cache.update(at=0.0, n=None)
