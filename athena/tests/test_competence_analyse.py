"""La compétence « Analyse documentaire » — ce que son corps doit dire.

Le corps est de la DONNÉE : il vit dans Firestore, s'édite à l'écran, et
`scripts/exporter_competence_analyse.py` n'en est que la source de
référence. Rien ne le compilait, donc rien ne le gardait.

Ce fichier garde le peu qui doit y être, et il existe pour une régression
RÉELLE : le 2026-08-27, en réécrivant la section « en lot », j'ai REMPLACÉ
le paragraphe de marche à suivre au lieu d'écrire à côté. Quatre consignes
sont parties avec lui, dont `indices_protection`. Le corps restait
plausible, la suite restait verte, et le défaut ne se serait vu qu'à
l'usage — sur une carte d'analyse dont la ligne « Indices » aurait cessé
d'apparaître, des semaines plus tard.

Ce qui est épinglé ici n'est donc pas la prose (elle doit rester libre)
mais les TROIS choses que rien ne dérive et que le modèle seul peut
fournir. Tout le reste de l'analyse en découle par le code :

    sous_nature        → la catégorie du document en dérive
                         (`analyse_taxonomies.nature_of`)
    privilèges         → le niveau de protection en dérive
                         (`analyse_protection.appliquer_regime`)
    indices_protection → ne dérive de rien. C'est la SEULE trace du
                         raisonnement, et la carte d'analyse l'affiche.
                         Un régime sans indice n'est qu'une affirmation.

Ces assertions sont volontairement grossières (une occurrence suffit) :
elles doivent survivre à une réécriture de style et ne tomber que sur une
DISPARITION.
"""

import importlib.util
import os

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "exporter_competence_analyse.py",
)


def _corps() -> str:
    spec = importlib.util.spec_from_file_location("_exp_analyse", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CORPS


def test_le_corps_nomme_la_sous_nature():
    # Le modèle ne choisit JAMAIS la catégorie : il arrête la sous-nature,
    # et `record_analyse` en dérive la catégorie. Le corps doit le dire,
    # sans quoi le modèle cherchera une catégorie que le schéma n'offre pas.
    corps = _corps()
    assert "sous_nature" in corps
    assert "DÉRIVE" in corps or "dérive" in corps


def test_le_corps_nomme_les_privileges():
    # Le niveau de protection se calcule des privilèges retenus, cumulés.
    corps = _corps()
    assert "privilège" in corps.lower()
    assert "cumul" in corps.lower()


def test_le_corps_exige_les_indices_de_protection():
    # La consigne perdue le 2026-08-27. Rien ne la dérive, rien ne la
    # remplace : sans elle, le régime devient invérifiable.
    assert "indices_protection" in _corps()


def test_le_corps_distingue_l_essai_a_blanc_du_lot():
    # Un essai à blanc suivi d'un enregistrement DOUBLE le nombre d'appels
    # de modèle, et ce nombre est plafonné par tour
    # (`CHAT_CHAIN_MAX_CALLS`). Sur un lot, chaque doublon coûte donc un
    # document que le tour n'atteindra pas — mesuré le 2026-08-27 : douze
    # appels pour quatre documents sur quarante-cinq.
    corps = _corps()
    assert "dry_run" in corps
    assert "idempotency_key" in corps
    assert "LOT" in corps or "lot" in corps
