"""Cohérence régime/forum des protocoles (PA-D03).

Le cas vécu : un protocole « cq_simplifié » (art. 535.x C.p.c., voie
simplifiée de la Cour du Québec) ACTIF sur un dossier de la Cour supérieure
(2026-027) — un échéancier suivi depuis le mauvais régime du Code, sans
qu'aucune couche ne s'en aperçoive. Rien ne validait le gabarit contre le
forum du dossier, et le champ `court` du protocole était vide depuis
toujours (la copie lisait `dossier.court`, une clé qui n'a jamais existé —
c'est `tribunal`).

Épinglé ici : le prédicat pur regime_mismatch (partagé par la porte de
création, l'annotation de l'assistant et le drapeau MCP — trois surfaces,
une vérité) et la porte de création elle-même, avec son message français
actionnable qui NOMME le gabarit attendu.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import protocol as pmod  # noqa: E402


def _dossier(tribunal="Cour supérieure", forum_type="judiciaire"):
    return {"id": "d1", "tribunal": tribunal, "forum_type": forum_type}


# ── Le prédicat pur ──────────────────────────────────────────────────────


def test_cq_sur_cour_superieure_est_incoherent():
    assert pmod.regime_mismatch("cq_simplifié", _dossier("Cour supérieure"))


def test_cs_sur_cour_du_quebec_est_incoherent():
    assert pmod.regime_mismatch("cs_ordinaire", _dossier("Cour du Québec"))


def test_gabarit_assorti_est_coherent():
    assert not pmod.regime_mismatch("cq_simplifié", _dossier("Cour du Québec"))
    assert not pmod.regime_mismatch("cs_ordinaire", _dossier("Cour supérieure"))


def test_conventionnel_est_toujours_admis():
    for d in (_dossier("Cour supérieure"),
              _dossier("Tribunal administratif du logement", "administratif"),
              _dossier("", "prejudiciaire"), None):
        assert not pmod.regime_mismatch("conventionnel", d)


def test_forum_non_judiciaire_refuse_les_deux_gabarits_cpc():
    tal = _dossier("Tribunal administratif du logement", "administratif")
    cf = _dossier("Cour fédérale", "federal")
    for d in (tal, cf):
        assert pmod.regime_mismatch("cq_simplifié", d)
        assert pmod.regime_mismatch("cs_ordinaire", d)


def test_tribunal_inconnu_ne_bloque_rien():
    """Préjudiciaire / non parsé : rien à valider, on ne bloque pas le
    dossier — le juriste sait mieux que le vide."""
    assert not pmod.regime_mismatch("cq_simplifié", _dossier(""))
    assert not pmod.regime_mismatch("cs_ordinaire", _dossier(""))
    assert not pmod.regime_mismatch("cq_simplifié", None)


# ── La porte de création ─────────────────────────────────────────────────


def test_create_refuse_et_nomme_le_gabarit_attendu(monkeypatch):
    import models.dossier as dmod
    monkeypatch.setattr(dmod, "get_dossier",
                        lambda did: _dossier("Cour supérieure"))
    proto, errors = pmod.create_protocol(
        dossier_id="d1",
        protocol_type="cq_simplifié",
        start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        data={},
    )
    assert proto is None
    assert len(errors) == 1
    # Actionnable : le message nomme le tribunal du dossier ET le gabarit
    # qu'il fallait.
    assert "Cour du Québec" in errors[0]
    assert "Cour supérieure" in errors[0]
    assert "CS — Procédure ordinaire" in errors[0]


def test_create_refuse_un_gabarit_cpc_sur_forum_administratif(monkeypatch):
    import models.dossier as dmod
    monkeypatch.setattr(
        dmod, "get_dossier",
        lambda did: _dossier("Tribunal administratif du logement",
                             "administratif"),
    )
    proto, errors = pmod.create_protocol(
        dossier_id="d1",
        protocol_type="cs_ordinaire",
        start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        data={},
    )
    assert proto is None
    assert "Conventionnel" in errors[0]


def test_create_accepte_le_gabarit_assorti(monkeypatch):
    """Le chemin nominal traverse la porte et échoue plus loin ou réussit —
    jamais sur le régime. On intercepte la suite pour isoler la porte."""
    import models.dossier as dmod
    monkeypatch.setattr(dmod, "get_dossier",
                        lambda did: _dossier("Cour du Québec"))
    monkeypatch.setattr(pmod, "_get_active_protocols",
                        lambda did: [{"id": "existing"}])
    proto, errors = pmod.create_protocol(
        dossier_id="d1",
        protocol_type="cq_simplifié",
        start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
        data={},
    )
    # Stopped by the one-active-protocol guard — i.e. PAST the regime gate.
    assert proto is None
    assert "protocole actif" in errors[0]
