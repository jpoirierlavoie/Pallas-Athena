"""Assainissement des noms + enveloppe create-only + enfilage (spec L1 §13.b/e/k)."""

import json
import os
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from client.services import stockage, taches  # noqa: E402


# ── §13.b — assainissement des noms ──────────────────────────────────────


def test_assainir_nom_ordinaire_intact():
    assert stockage.assainir_nom("Déposition finale.pdf") == "Déposition finale.pdf"


def test_assainir_traversee_de_chemin():
    resultat = stockage.assainir_nom("../../etc/passwd")
    assert "/" not in resultat and "\\" not in resultat
    assert not resultat.startswith(".")


def test_assainir_separateurs_et_controle():
    assert stockage.assainir_nom("a/b\\c.pdf") == "a_b_c.pdf"
    assert stockage.assainir_nom("mau\x00vais\x1fnom.pdf") == "mauvaisnom.pdf"


def test_assainir_nfc_normalise():
    # e + combining acute (NFD) → é (NFC)
    assert stockage.assainir_nom("dépot.pdf") == "dépot.pdf"


def test_assainir_vide_et_points():
    assert stockage.assainir_nom("") == "document"
    assert stockage.assainir_nom("...") == "document"
    assert stockage.assainir_nom(None) == "document"


def test_assainir_longueur_conserve_extension():
    long_nom = "a" * 300 + ".pdf"
    resultat = stockage.assainir_nom(long_nom)
    assert len(resultat) <= 180
    assert resultat.endswith(".pdf")


def test_horodatage_compact():
    assert re.fullmatch(r"\d{8}T\d{6}", stockage.horodatage_utc_compact())


# ── §13.e — enveloppe create-only ────────────────────────────────────────


def test_ecrire_enveloppe_create_only(monkeypatch):
    blob = mock.Mock()
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(stockage, "_bucket", lambda: bucket)

    enveloppe = {"invitation_id": "inv1", "batch": "b1", "files": []}
    stockage.ecrire_enveloppe("inv1", "b1", enveloppe)

    bucket.blob.assert_called_once_with("submissions/inv1/b1/envelope.json")
    kwargs = blob.upload_from_string.call_args.kwargs
    # Create-only: the double finalization MUST collide (412 → route 409).
    assert kwargs["if_generation_match"] == 0
    assert kwargs["content_type"] == "application/json"
    assert json.loads(blob.upload_from_string.call_args.args[0]) == enveloppe


def test_session_reprenable_origin_et_taille(monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(stockage, "_bucket", lambda: bucket)

    url = stockage.ouvrir_session_reprenable("submissions/i/b/files/001_a.pdf",
                                             "application/pdf", 1234)
    assert url == "https://up.example/s"
    kwargs = blob.create_resumable_upload_session.call_args.kwargs
    # origin → CORS de la session ; size → plafond appliqué par GCS lui-même.
    assert kwargs["origin"].startswith("https://")
    assert kwargs["size"] == 1234


# ── §13.k — charge et routage de la tâche ────────────────────────────────


def test_signaler_charge_et_routage_conformes(monkeypatch):
    fake = mock.Mock()
    fake.queue_path.return_value = "projects/p/locations/l/queues/portail"
    monkeypatch.setattr(taches, "_client", lambda: fake)

    taches.signaler("soumise", "inv1", batch="b1")

    requete = fake.create_task.call_args.kwargs["request"]
    assert requete["parent"] == "projects/p/locations/l/queues/portail"
    aehr = requete["task"]["app_engine_http_request"]
    assert aehr["relative_uri"] == "/taches/portail/evenement"
    # Le gestionnaire vit sur le service PRINCIPAL, jamais sur le portail.
    assert aehr["app_engine_routing"] == {"service": "default"}
    corps = json.loads(aehr["body"].decode())
    assert corps["event"] == "soumise"
    assert corps["invitation_id"] == "inv1"
    assert corps["batch"] == "b1"
    assert corps["emis_at"]  # ISO 8601 UTC


def test_signaler_evenement_inconnu_refuse():
    with pytest.raises(ValueError):
        taches.signaler("purge", "inv1")
