"""Le juriste corrige l'analyse — et la règle de non-déclassement.

Deux choses arrêtées le 2026-08-27, et qui se tiennent :

**La règle de non-déclassement était PROMISE et pas implémentée.**
`appliquer_regime` ne monte qu'à l'intérieur d'UN appel : elle ne reçoit
rien de l'analyse antérieure, si bien qu'une réanalyse retenant moins de
privilèges faisait tomber le niveau. Mesuré avant correction : 3 → 1, en
silence, sur un document couvert par le secret professionnel. Pendant ce
temps la description de l'outil MCP, la compétence et CLAUDE.md
affirmaient toutes trois que le code gardait le plus élevé.

**Et le juriste peut tout éditer**, ce qui est ce qui rend la première
règle tenable : un chemin automatique ne descend jamais, mais une
sur-protection fautive ne devient pas définitive pour autant — l'avocat
est la voie de descente, et sa correction part au journal comme le reste.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch.dict(os.environ, {
    "SECRET_KEY": "t", "FIREBASE_PROJECT_ID": "p",
    "FIREBASE_STORAGE_BUCKET": "b", "AUTHORIZED_USER_EMAIL": "a@b.c",
}):
    with mock.patch("firebase_admin.initialize_app"), \
            mock.patch("google.cloud.firestore.Client"):
        import models.document as doc

from utils import analyse_protection as prot  # noqa: E402
from utils import analyse_taxonomies as tax  # noqa: E402


class _FauxDoc:
    def __init__(self, store, key, sub=None):
        self._store, self._key, self._sub = store, key, sub

    def set(self, data):
        self._store[self._key] = dict(data)

    def collection(self, nom):
        assert nom == doc.ANALYSES_SUBCOLLECTION, nom
        return _FauxCollection(self._sub)


class _FauxCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _FauxDoc(self._store, key)


@pytest.fixture()
def monde(monkeypatch):
    store, journaux = {}, {}

    class _Racine:
        def document(self, key):
            return _FauxDoc(store, key, journaux.setdefault(key, {}))

    class _DB:
        def collection(self, nom):
            assert nom == doc.COLLECTION, nom
            return _Racine()

    monkeypatch.setattr(doc, "db", _DB())
    monkeypatch.setattr(
        doc, "get_document", lambda i: dict(store.get(i) or {}) or None
    )
    store["doc-1"] = {
        "id": "doc-1", "dossier_id": "d1", "category": "correspondance",
        "category_source": "juriste", "filename": "x.pdf",
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
    }
    return store, journaux


# ── La règle de non-déclassement (chemins AUTOMATIQUES) ─────────────────


def _derive(sortie, precedente=None):
    document = {"id": "doc-1", "category": "autre"}
    if precedente is not None:
        document["analyse"] = precedente
    champ, erreurs = doc._analyse_derivee(sortie, document=document)
    assert not erreurs, erreurs
    return champ


def test_une_reanalyse_ne_fait_jamais_descendre_le_niveau():
    # Le défaut RÉEL, mesuré le 2026-08-27 : 3 → 1, sans un mot. Une
    # erreur qui sous-estime la protection peut mener à une divulgation
    # par inadvertance (art. 60.4 du Code des professions) ; l'inverse
    # fait perdre du temps. Les deux ne se valent pas.
    a1 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]})
    assert a1["niveau_protection"] == 3

    a2 = _derive({"sous_nature": "CORR_TIERS", "privileges": []}, a1)
    assert a2["niveau_protection"] == 3


def test_le_niveau_tenu_reste_expliqué_par_ses_privileges():
    # Garder un niveau 3 sans le privilège qui le fonde produirait une
    # carte incohérente, où la protection ne s'expliquerait par rien.
    a1 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]})
    a2 = _derive({"sous_nature": "CORR_TIERS", "privileges": []}, a1)
    assert "SECRET_PROFESSIONNEL" in a2["privileges"]


def test_la_divergence_est_signalee_et_le_verdict_de_l_analyse_conserve():
    # Tenir le niveau sans le dire le rendrait invérifiable : on verrait
    # un niveau tenu sans savoir de quoi il a été tenu.
    a1 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]})
    a2 = _derive({"sous_nature": "CORR_TIERS", "privileges": []}, a1)
    assert a2["divergence_protection"] is True
    assert a2["niveau_protection_analyse"] == 1   # ce que CE passage concluait
    assert a2["niveau_protection_precedent"] == 3
    assert any("Niveau tenu" in m for m in a2["motifs_protection"])


def test_remonter_le_niveau_ne_signale_aucune_divergence():
    a1 = _derive({"sous_nature": "CORR_TIERS", "privileges": []})
    a2 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]}, a1)
    assert a2["niveau_protection"] == 3
    assert a2["divergence_protection"] is False


def test_une_premiere_analyse_ne_signale_aucune_divergence():
    # Aucun précédent : il n'y a rien à tenir, et crier au loup sur le
    # premier passage rendrait le drapeau inutilisable.
    a = _derive({"sous_nature": "CORR_TIERS", "privileges": []})
    assert a["divergence_protection"] is False
    assert a["niveau_protection_precedent"] is None


# ── Le juriste édite TOUT ────────────────────────────────────────────────


def test_le_juriste_peut_declasser_ce_que_l_analyse_ne_peut_pas(monde):
    # La contrepartie de la règle : sans cette porte, une sur-protection
    # fautive serait définitive.
    store, _ = monde
    a1 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]})
    store["doc-1"]["analyse"] = a1

    maj, erreurs = doc.update_analyse(
        "doc-1", {"niveau_protection": 0, "privileges": ["PUBLIC"]},
        par="me@cabinet.ca",
    )
    assert not erreurs, erreurs
    assert maj["analyse"]["niveau_protection"] == 0


def test_editer_vaut_confirmer(monde):
    # Le juriste qui corrige a VU la carte. Lui redemander un clic sur
    # « Confirmer » serait lui faire dire deux fois la même chose.
    store, _ = monde
    store["doc-1"]["analyse"] = _derive({"sous_nature": "CORR_TIERS"})
    maj, erreurs = doc.update_analyse(
        "doc-1", {"auteur": "Un Toit en Réserve"}, par="me@cabinet.ca"
    )
    assert not erreurs, erreurs
    assert maj["analyse"]["confirme"] is True
    assert maj["analyse"]["confirme_par"] == "me@cabinet.ca"
    assert maj["category_source"] == "juriste"


def test_la_categorie_DERIVE_du_code_meme_a_la_main(monde):
    # La garantie centrale du dispositif : personne ne choisit une
    # catégorie, ni le modèle ni le juriste. On choisit un CODE.
    store, _ = monde
    store["doc-1"]["analyse"] = _derive({"sous_nature": "CORR_TIERS"})
    maj, erreurs = doc.update_analyse(
        "doc-1", {"sous_nature": "JUG_JUGEMENT"}, par="me@cabinet.ca"
    )
    assert not erreurs, erreurs
    assert maj["analyse"]["nature_detectee"] == tax.nature_of("JUG_JUGEMENT")
    assert maj["category"] == maj["analyse"]["nature_detectee"]
    assert maj["analyse"]["famille"] == tax.famille_of("JUG_JUGEMENT")


def test_les_vocabulaires_restent_fermes_a_la_main(monde):
    store, _ = monde
    store["doc-1"]["analyse"] = _derive({"sous_nature": "CORR_TIERS"})
    for champs, attendu in (
        ({"sous_nature": "INVENTE"}, "Sous-nature inconnue"),
        ({"privileges": ["MAGIQUE"]}, "Privilège inconnu"),
        ({"niveau_protection": 9}, "Niveau de protection invalide"),
    ):
        maj, erreurs = doc.update_analyse("doc-1", champs, par="me@cabinet.ca")
        assert maj is None
        assert any(attendu in e for e in erreurs), (champs, erreurs)


def test_la_correction_du_juriste_part_au_journal(monde):
    # Rien ne s'efface : l'historique doit distinguer ce que le modèle a
    # proposé de ce que l'avocat a arrêté.
    store, journaux = monde
    store["doc-1"]["analyse"] = _derive({"sous_nature": "CORR_TIERS"})
    doc.update_analyse("doc-1", {"auteur": "X"}, par="me@cabinet.ca")
    entrees = list(journaux["doc-1"].values())
    assert len(entrees) == 1
    assert entrees[0]["declenche_par"] == "juriste"
    assert entrees[0]["modifie_par"] == "me@cabinet.ca"


def test_une_cle_absente_laisse_la_valeur_intacte(monde):
    # Le contrat de présence du modèle : sans lui, enregistrer un seul
    # champ effacerait tous les autres.
    store, _ = monde
    store["doc-1"]["analyse"] = _derive(
        {"sous_nature": "CORR_TIERS", "auteur": "Original", "tribunal": "TAL"}
    )
    maj, _ = doc.update_analyse("doc-1", {"auteur": "Corrigé"},
                                par="me@cabinet.ca")
    assert maj["analyse"]["auteur"] == "Corrigé"
    assert maj["analyse"]["tribunal"] == "TAL"


def test_une_cle_presente_et_vide_efface(monde):
    # L'autre moitié du contrat, et elle est nécessaire : sans elle, une
    # mention que l'analyse a inventée ne pourrait jamais être retirée.
    store, _ = monde
    store["doc-1"]["analyse"] = _derive(
        {"sous_nature": "CORR_TIERS", "tribunal": "TAL"}
    )
    maj, _ = doc.update_analyse("doc-1", {"tribunal": ""}, par="me@cabinet.ca")
    assert maj["analyse"]["tribunal"] == ""


def test_le_juriste_pose_le_plancher_des_analyses_suivantes(monde):
    # Il a tranché : la divergence n'est plus en attente, et le niveau
    # qu'il pose devient ce qu'une réanalyse ne pourra plus abaisser.
    store, _ = monde
    a1 = _derive({"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]})
    store["doc-1"]["analyse"] = a1
    maj, _ = doc.update_analyse(
        "doc-1", {"niveau_protection": 2, "privileges": ["LITIGE"]},
        par="me@cabinet.ca",
    )
    assert maj["analyse"]["divergence_protection"] is False

    # Une réanalyse plus basse ne redescend pas sous le plancher qu'il a
    # posé. Le niveau 1 est le plancher NATUREL de la table — la
    # présomption publique cède devant l'absence d'indice —, donc le
    # plancher du juriste doit être testé au-dessus pour prouver quelque
    # chose.
    a3 = _derive({"sous_nature": "JUG_JUGEMENT", "privileges": ["PUBLIC"]},
                 maj["analyse"])
    assert a3["niveau_protection"] == 2
    assert a3["divergence_protection"] is True


# ── La couverture, épinglée par DÉRIVATION ──────────────────────────────


def test_tout_ce_que_l_analyse_produit_est_editable():
    """La décision du praticien, rendue exécutable.

    « Whatever the analysis outputs becomes an editable field. » Ce test
    DÉRIVE la liste au lieu de la recopier : un champ ajouté à
    `_analyse_derivee` sans entrée d'édition fait tomber le test, ce qu'un
    inventaire écrit à la main ne ferait jamais.
    """
    produits = set(_derive({
        "sous_nature": "JUG_JUGEMENT", "privileges": ["PUBLIC"],
        "resume": "r", "tribunal": "t", "auteur": "a",
        "numero_dossier_cour": "1", "parties_mentionnees": ["x"],
        "indices_protection": ["i"], "confiance": "haute",
    }))
    # Ce qui SE DÉRIVE ne se saisit pas : la nature vient du code, l'état
    # de confirmation vient d'un geste, les niveaux témoins sont calculés.
    derives = {
        "statut", "nature_detectee", "famille", "niveau_protection_analyse",
        "niveau_protection_precedent", "divergence_protection",
        "confirme", "confirme_par", "confirme_le",
    }
    manquants = sorted(produits - set(doc.ANALYSE_EDITABLE) - derives)
    assert not manquants, f"produits mais non éditables : {manquants}"


def test_les_vocabulaires_du_formulaire_sont_derives_des_modules_purs():
    # Pas de littéral recopié : une entrée ajoutée à la table paraît au
    # formulaire sans qu'on y touche, et la dérive devient impossible.
    from routes.documents import _analyse_form_context

    ctx = _analyse_form_context()
    assert {r["code"] for r in ctx["analyse_sous_natures"]} == set(
        tax.VALID_SOUS_NATURES
    )
    assert {r["code"] for r in ctx["analyse_privileges"]} == set(prot.PRIVILEGES)
