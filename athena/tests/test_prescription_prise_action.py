"""Prise d'action : taire l'alerte de prescription (2026-07-28).

Quand la demande a été déposée, le délai ne court plus (art. 2892 C.c.Q.) et
l'alerte n'a plus lieu d'être. « Taire » doit valoir PARTOUT : le tableau de
bord, Claude (MCP get_agenda), la pastille de la liste des dossiers et la
couleur de « Date pour agir » sur la carte. Laisser une seule de ces surfaces
vive apprendrait au juriste à se méfier de la couleur — ce qui coûte plus cher
que l'alerte elle-même.

Les tests du filtre côté modèle vivent dans test_dashboard_aggregation.py (là
où sont déjà ceux de list_prescription_alerts) ; ce fichier couvre la couche
route, non testée jusqu'ici.

Importer ``routes.dossiers`` tire ``models/__init__`` (client Firestore), d'où
le mock à l'import — aucun appel Firestore n'est fait, la fonction sous test
étant une pure opération sur des dictionnaires.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.dossiers as rd

NOW = datetime.now(timezone.utc)


def _dossier(**over) -> dict:
    base = {"id": "d1", "prescription_date": NOW + timedelta(days=10)}
    base.update(over)
    return base


# ── Pastille de la liste + couleur de la carte ───────────────────────────


def test_sans_prise_d_action_l_avertissement_reste():
    dossiers = [_dossier()]
    rd._attach_prescription_warnings(dossiers)
    assert dossiers[0]["_prescription_warning"] == "red"


def test_une_prise_d_action_eteint_la_pastille():
    dossiers = [_dossier(prise_action_date=NOW - timedelta(days=1))]
    rd._attach_prescription_warnings(dossiers)
    assert dossiers[0]["_prescription_warning"] == ""


def test_elle_eteint_aussi_l_orange():
    dossiers = [_dossier(prescription_date=NOW + timedelta(days=45),
                         prise_action_date=NOW)]
    rd._attach_prescription_warnings(dossiers)
    assert dossiers[0]["_prescription_warning"] == ""


def test_un_dossier_herite_sans_la_cle_garde_son_avertissement():
    """Le champ est additif, sans migration : sur un dossier existant la clé
    est ABSENTE, ce qui doit se lire « aucune action prise »."""
    herite = {"id": "d1", "prescription_date": NOW + timedelta(days=10)}
    assert "prise_action_date" not in herite
    rd._attach_prescription_warnings([herite])
    assert herite["_prescription_warning"] == "red"


def test_une_date_effacee_ramene_l_avertissement():
    """Vider le champ au formulaire produit None, pas une clé absente."""
    dossiers = [_dossier(prise_action_date=None)]
    rd._attach_prescription_warnings(dossiers)
    assert dossiers[0]["_prescription_warning"] == "red"


def test_sans_date_pour_agir_rien_ne_change():
    dossiers = [_dossier(prescription_date=None)]
    rd._attach_prescription_warnings(dossiers)
    assert dossiers[0]["_prescription_warning"] == ""


# ── Saisie du formulaire ─────────────────────────────────────────────────


def test_le_formulaire_lit_la_date_a_minuit_utc():
    """_parse_date est le seul point de coercition : « AAAA-MM-JJ » → minuit
    UTC, vide/illisible → None."""
    assert rd._parse_date("2026-07-28") == datetime(
        2026, 7, 28, tzinfo=timezone.utc
    )
    assert rd._parse_date("") is None
    assert rd._parse_date("28/07/2026") is None
