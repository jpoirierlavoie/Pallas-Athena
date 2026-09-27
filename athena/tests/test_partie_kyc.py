"""Sémantique des dates KYC (PA-D07).

``identity_verified_date`` / ``conflict_check_date`` répondent à « quand la
décision a-t-elle été prise », jamais à « quand le champ a-t-il bougé ». Le
défaut d'origine : l'estampille se posait sur TOUT changement de statut, dans
les deux sens — une rétrogradation vers « non_vérifié » laissait un statut
non vérifié à côté d'un horodatage frais, l'incohérence exacte relevée par
l'audit MCP.

Invariant épinglé ici : date présente ⇔ statut décidé. L'estampille ne se pose
que sur une transition VERS un statut décidé (vérifié / exempté, et pour le
contrôle des conflits vérifié / conflit_détecté — le contrôle A eu lieu) ; un
statut soumis « non_vérifié » force la date à None, ce qui auto-répare les
documents antérieurs au correctif à leur prochaine édition KYC. Le tout est
conditionné à la PRÉSENCE de la clé : une mise à jour partielle qui ne porte
pas le champ ne touche jamais la date (sur un set plein-document, injecter un
défaut EST une suppression).

Tests purs : modèle importé, Firestore bouchonné (motif de
test_partie_naissance.py).
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

from models import partie as pm  # noqa: E402

ANCIEN = datetime(2026, 6, 19, 14, 51, tzinfo=timezone.utc)


def _partie(**over) -> dict:
    base = pm._default_doc()
    base.update({
        "id": "p1", "type": "individual", "contact_role": "client",
        "first_name": "Marco", "last_name": "Andreoli",
    })
    base.update(over)
    return base


def _maj(monkeypatch, stocke: dict, data: dict) -> dict:
    """Run update_partie against a stubbed store; return the written doc."""
    monkeypatch.setattr(pm, "get_partie", lambda pid: dict(stocke))
    ecrit: dict = {}

    class _Doc:
        def set(self, payload):
            ecrit.update(payload)

    class _Col:
        def document(self, _pid):
            return _Doc()

    monkeypatch.setattr(pm, "db", mock.Mock(collection=lambda _n: _Col()))
    _, erreurs = pm.update_partie("p1", data)
    assert erreurs == []
    return ecrit


def test_transition_vers_verifie_estampille(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié", identity_verified_date=None),
        {"identity_verified": "vérifié"},
    )
    assert ecrit["identity_verified_date"] is not None
    assert ecrit["identity_verified_date"] != ANCIEN


def test_transition_vers_exempte_estampille(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié"),
        {"identity_verified": "exempté"},
    )
    assert ecrit["identity_verified_date"] is not None


def test_retrogradation_efface_la_date(monkeypatch):
    """LE bogue PA-D07 : vérifié → non_vérifié RE-estampillait, produisant
    « non_vérifié » à côté d'un horodatage frais."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "non_vérifié"},
    )
    assert ecrit["identity_verified"] == "non_vérifié"
    assert ecrit["identity_verified_date"] is None


def test_meme_statut_ne_reestampille_pas(monkeypatch):
    """Re-sauvegarder un contact déjà vérifié garde la date d'origine —
    « quand a-t-il été vérifié », pas « quand a-t-on sauvegardé »."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "vérifié", "email": "a@b.ca"},
    )
    assert ecrit["identity_verified_date"] == ANCIEN


def test_conflit_detecte_est_une_decision(monkeypatch):
    """Un contrôle qui DÉTECTE un conflit a bien eu lieu : sa date est la
    date du contrôle."""
    ecrit = _maj(
        monkeypatch,
        _partie(conflict_check="non_vérifié", conflict_check_date=None),
        {"conflict_check": "conflit_détecté"},
    )
    assert ecrit["conflict_check_date"] is not None


def test_retrogradation_du_controle_efface_aussi(monkeypatch):
    ecrit = _maj(
        monkeypatch,
        _partie(conflict_check="vérifié", conflict_check_date=ANCIEN),
        {"conflict_check": "non_vérifié"},
    )
    assert ecrit["conflict_check_date"] is None


def test_mise_a_jour_partielle_ne_touche_pas_les_dates(monkeypatch):
    """Une clé absente ne touche rien — le portail L3 et les PUT CardDAV
    passent par ici sans jamais porter les champs KYC."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="vérifié", identity_verified_date=ANCIEN,
                conflict_check="vérifié", conflict_check_date=ANCIEN),
        {"email": "nouveau@exemple.com"},
    )
    assert ecrit["identity_verified_date"] == ANCIEN
    assert ecrit["conflict_check_date"] == ANCIEN


def test_document_incoherent_pre_correctif_s_autorepare(monkeypatch):
    """Un document d'avant le correctif (non_vérifié + date) se répare à la
    prochaine édition KYC qui re-soumet non_vérifié — l'invariant
    date-présente ⇔ statut-décidé redevient vrai sans migration."""
    ecrit = _maj(
        monkeypatch,
        _partie(identity_verified="non_vérifié", identity_verified_date=ANCIEN),
        {"identity_verified": "non_vérifié"},
    )
    assert ecrit["identity_verified_date"] is None


# ── update_kyc_status ne vide plus les notes de conformité (lot 0b) ──────
#
# Le défaut : ``notes: str = ""`` par défaut, toujours écrit. Comme
# update_partie fusionne {**existing, **data} puis fait un set() du document
# entier, un changement de statut SANS notes effaçait les notes du juriste.
# Ces tests passent par le VRAI client Firestore (seul le serveur est faux) :
# c'est le document STOCKÉ qui est relu, jamais un dict remis à un bouchon.

import pytest  # noqa: E402

from tests._fake_firestore import install  # noqa: E402


@pytest.fixture
def magasin(monkeypatch):
    fake = install(monkeypatch, pm)
    fake.seed("parties/p1", _partie(
        identity_verified="non_vérifié",
        identity_verified_notes="Pièce vue le 3 mars — permis de conduire.",
        conflict_check="non_vérifié",
        conflict_check_notes="Recherche au registre : aucun homonyme.",
        etag="e0",
    ))
    return fake


def test_un_statut_sans_notes_garde_les_notes(magasin):
    """LE défaut : sur l'ancien code, ce changement de statut vidait les
    notes (default ``""`` toujours écrit)."""
    doc, erreurs = pm.update_kyc_status("p1", "identity_verified", "vérifié")
    assert erreurs == []
    stocke = magasin.peek("parties/p1")
    assert stocke["identity_verified"] == "vérifié"
    assert stocke["identity_verified_notes"] == (
        "Pièce vue le 3 mars — permis de conduire."
    )
    # L'autre bloc n'est pas touché non plus.
    assert stocke["conflict_check_notes"] == (
        "Recherche au registre : aucun homonyme."
    )
    assert doc["identity_verified_notes"] == stocke["identity_verified_notes"]


def test_des_notes_fournies_remplacent(magasin):
    pm.update_kyc_status(
        "p1", "conflict_check", "vérifié", notes="Aucun conflit (3 dossiers)."
    )
    assert magasin.peek("parties/p1")["conflict_check_notes"] == (
        "Aucun conflit (3 dossiers)."
    )


def test_une_chaine_vide_efface_explicitement(magasin):
    """Effacer reste possible — mais seulement en le DEMANDANT."""
    pm.update_kyc_status("p1", "identity_verified", "vérifié", notes="")
    assert magasin.peek("parties/p1")["identity_verified_notes"] == ""


def test_les_notes_fournies_sont_assainies(magasin):
    pm.update_kyc_status(
        "p1", "identity_verified", "vérifié", notes="<b>vu</b> " + "x" * 3000
    )
    notes = magasin.peek("parties/p1")["identity_verified_notes"]
    assert "<b>" not in notes and len(notes) <= 2000
