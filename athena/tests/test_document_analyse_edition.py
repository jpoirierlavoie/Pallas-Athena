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


# ── Annexe C : les deux axes du droit de la preuve ──────────────────────
#
# Le défaut vu en production le 2026-08-27 : `moyen_preuve` et
# `qualification_ecrit` restaient vides après TOUTE analyse. Ce n'était pas
# le modèle qui les omettait — le schéma d'entrée de l'outil ne les
# déclarait pas, donc il ne pouvait pas les fournir, alors qu'ils étaient
# lus s'ils arrivaient et éditables par le juriste. Quatre champs dans cet
# état, et rien ne le disait.


def test_le_modele_peut_fournir_tout_ce_que_le_modele_lit():
    """L'épingle qui aurait attrapé le défaut, et par DÉRIVATION.

    Tout champ que `_EXTRACTION_FIELDS` accepte doit avoir une propriété
    d'entrée dans l'outil. Sinon il est structurellement condamné à rester
    vide, et rien ne le signale : ni erreur, ni avertissement, seulement
    une carte d'analyse incomplète que personne ne relie à un schéma.
    """
    from mcp import tools

    schema = set(
        tools.TOOLS["record_document_analysis"]["input_schema"]["properties"]
    )
    manquants = sorted(set(doc._EXTRACTION_FIELDS) - schema)
    assert not manquants, (
        f"lus par le modèle mais impossibles à fournir : {manquants}"
    )


def test_les_deux_axes_de_preuve_s_ecrivent(monde):
    store, _ = monde
    champ, erreurs = doc._analyse_derivee(
        {
            "sous_nature": "PREUVE_CONTRAT", "moyen_preuve": "ECRIT",
            "qualification_ecrit": "SOUS_SEING_PRIVE",
            "parait_original": True, "qualite_reconnaissance": "haute",
        },
        document={"id": "doc-1"},
    )
    assert not erreurs, erreurs
    assert champ["moyen_preuve"] == "ECRIT"
    assert champ["qualification_ecrit"] == "SOUS_SEING_PRIVE"
    assert champ["parait_original"] is True
    assert champ["qualite_reconnaissance"] == "haute"


def test_une_qualification_d_ecrit_sur_un_temoignage_est_refusee():
    # Annexe C, axe 2 : « seulement si moyen_preuve == ECRIT ». Un « acte
    # notarié » sur un témoignage ne veut rien dire, et la qualification a
    # des conséquences — un acte authentique fait preuve jusqu'à
    # inscription de faux (art. 2813-2814 C.c.Q.).
    champ, erreurs = doc._analyse_derivee(
        {"sous_nature": "PREUVE_CONTRAT", "moyen_preuve": "TEMOIGNAGE",
         "qualification_ecrit": "ACTE_NOTARIE"},
        document={"id": "doc-1"},
    )
    assert champ == {}
    assert any("ECRIT" in e for e in erreurs), erreurs


def test_non_determine_reste_acceptable_partout():
    # C'est ce qui permet de dire « je ne peux pas trancher » sans mentir.
    champ, erreurs = doc._analyse_derivee(
        {"sous_nature": "PREUVE_CONTRAT", "moyen_preuve": "TEMOIGNAGE",
         "qualification_ecrit": "NON_DETERMINE"},
        document={"id": "doc-1"},
    )
    assert not erreurs, erreurs


def test_le_juriste_est_tenu_par_la_meme_regle_d_axe(monde):
    # La validation porte sur la valeur qui sera STOCKÉE : corriger un axe
    # seul doit rester cohérent avec l'autre tel qu'il est déjà en place.
    store, _ = monde
    store["doc-1"]["analyse"], _ = doc._analyse_derivee(
        {"sous_nature": "PREUVE_CONTRAT", "moyen_preuve": "ECRIT",
         "qualification_ecrit": "SOUS_SEING_PRIVE"},
        document={"id": "doc-1"},
    )
    maj, erreurs = doc.update_analyse(
        "doc-1", {"moyen_preuve": "TEMOIGNAGE"}, par="me@cabinet.ca"
    )
    assert maj is None
    assert any("ECRIT" in e for e in erreurs), erreurs


def test_les_deux_axes_sont_offerts_en_listes_fermees():
    # En texte libre, ils étaient invérifiables — et la moitié de leur
    # valeur tient à ce qu'ils soient comparables d'un document à l'autre.
    from routes.documents import _analyse_form_context
    from utils import analyse_taxonomies as t

    ctx = _analyse_form_context()
    assert {c for c, _, _ in ctx["analyse_moyens_preuve"]} == set(
        t.VALID_MOYENS_PREUVE
    )
    assert {c for c, _, _ in ctx["analyse_qualifications"]} == set(
        t.VALID_QUALIFICATIONS_ECRIT
    )


# ── Deux textes, pas trois (2026-08-31) ─────────────────────────────────


def test_le_juriste_n_a_plus_qu_un_champ_de_texte_a_lui():
    """`description` a été RETIRÉ, et il ne doit pas revenir.

    C'était le troisième champ de texte d'un document, et il était
    redondant par construction : `record_analyse` y recopiait le résumé
    de l'analyse, donc après toute analyse les deux portaient la même
    chaîne. Depuis qu'ils s'éditaient séparément, ils pouvaient en plus
    diverger — pire que la redondance.

    Le partage est désormais net : `notes_internes` est le texte du
    JURISTE (rien ne le réécrit), `analyse.resume` celui du MODÈLE, et
    `genere_depuis` la provenance d'un document produit par la machine —
    qui n'est ni l'un ni l'autre et ne s'édite pas.
    """
    defaut = doc._default_doc()
    assert "description" not in defaut
    assert "notes_internes" in defaut
    assert "genere_depuis" in defaut


def test_le_formulaire_n_ecrit_jamais_une_description(monde):
    """La liste blanche de `update_metadata` est ce qui l'empêche.

    Un formulaire périmé, un appel forgé, une couture oubliée : aucun ne
    doit pouvoir ressusciter le champ.
    """
    store, _ = monde
    maj, erreurs = doc.update_metadata(
        "doc-1", {"description": "revenue par la bande",
                  "notes_internes": "ma note"}
    )
    assert not erreurs, erreurs
    assert maj.get("description", "") == ""
    assert maj["notes_internes"] == "ma note"


def test_la_recherche_libre_couvre_les_deux_textes_et_la_provenance():
    """Elle cherchait dans `description`. Sans ce remplacement, retrouver
    « la note d'honoraires de la facture 2026-003 » par son numéro aurait
    cessé de fonctionner — silencieusement, comme toute recherche qui
    rétrécit."""
    import inspect

    source = inspect.getsource(doc.list_documents)
    assert 'd.get("notes_internes"' in source
    assert 'd.get("genere_depuis"' in source
    assert '"resume"' in source
    assert 'd.get("description"' not in source


def test_le_gestionnaire_ne_jette_aucun_champ_que_le_schema_annonce():
    """L'épingle qui manquait, et le défaut qu'elle attrape.

    Trois listes doivent s'accorder pour qu'un champ d'analyse traverse :
    le SCHÉMA de l'outil (ce que le modèle peut envoyer),
    `_ANALYSE_INPUTS` (ce que le gestionnaire retient) et
    `_EXTRACTION_FIELDS` (ce que la dérivation lit). Le 2026-08-27, la
    première a gagné quatre champs (les deux axes de l'Annexe C,
    `parait_original`, `qualite_reconnaissance`) et la deuxième non : le
    gestionnaire les jetait en silence, donc aucune surface ne pouvait
    les écrire alors que la description de l'outil les annonçait.

    L'épingle voisine (`test_le_modele_peut_fournir_tout_ce_que_le_modele_lit`)
    ne l'a pas vu : elle vérifie `_EXTRACTION_FIELDS ⊆ schéma`, jamais le
    maillon du MILIEU. Et la vérification manuelle de l'époque portait sur
    `_analyse_derivee` en direct — à côté de la frontière où le défaut
    vivait, la faute déjà consignée pour l'identifiant de courriel.
    """
    from mcp import handlers, tools

    schema = set(
        tools.TOOLS["record_document_analysis"]["input_schema"]["properties"]
    )
    lus = set(doc._EXTRACTION_FIELDS)
    retenus = set(handlers._ANALYSE_INPUTS)

    jetes = sorted((schema & lus) - retenus)
    assert not jetes, f"annoncés au schéma mais jetés par le gestionnaire : {jetes}"

    # Et l'inverse : rien de retenu qui ne soit ni déclaré ni lu, sinon la
    # liste porterait un nom mort que personne ne remarquerait.
    orphelins = sorted(retenus - schema - lus)
    assert not orphelins, f"retenus sans schéma ni lecture : {orphelins}"


# ── Les vocabulaires de l'analyse, au connecteur ────────────────────────
#
# Le clavardage interne portait la compétence et ses annexes ; avec son
# retrait, la seule voie est un Skill claude.ai — du texte STATIQUE que
# rien ne relie au code et dont aucun test ne détecte la dérive. Ces
# quatre vocabulaires sont l'assurance : la table elle-même, atteignable
# au moment où le modèle en a besoin.


def _vocab(kind):
    from mcp import handlers

    return handlers.get_reference_vocabulary({"kind": kind})


def test_les_quatre_vocabulaires_d_analyse_sont_servis():
    # Dérivé des tables, jamais recopié : une entrée ajoutée à la table
    # paraît au connecteur sans qu'on y touche.
    assert {r["code"] for r in _vocab("sous_natures")["items"]} == set(
        tax.VALID_SOUS_NATURES
    )
    assert {r["code"] for r in _vocab("privileges")["items"]} == set(
        prot.PRIVILEGES
    )
    assert {r["code"] for r in _vocab("moyens_preuve")["items"]} == set(
        tax.VALID_MOYENS_PREUVE
    )
    assert {r["code"] for r in _vocab("qualifications_ecrit")["items"]} == set(
        tax.VALID_QUALIFICATIONS_ECRIT
    )


def test_le_kind_declare_et_le_kind_servi_ne_peuvent_pas_deriver():
    # Un `kind` déclaré sans constructeur est refusé à l'exécution ; un
    # constructeur sans `kind` déclaré est inatteignable. Les deux échouent
    # en silence du point de vue de l'appelant.
    from mcp import handlers, tools

    declares = set(
        tools.TOOLS["get_reference_vocabulary"]["input_schema"]
        ["properties"]["kind"]["enum"]
    )
    # `actions` est le seul traité hors de la table (il prend un filtre).
    servis = set(handlers._VOCABULARIES) | {"actions"}
    assert declares == servis


def test_chaque_privilege_porte_son_niveau_et_sa_reserve():
    """Le vocabulaire le plus conséquent, et pourquoi la réserve compte.

    La règle asymétrique du domaine veut que sous-estimer une protection
    soit plus grave que la surestimer (art. 60.4 du Code des professions).
    Une réserve dit ce qu'un code ne garantit PAS — que `SECRET_COMMERCIAL`
    n'est pas un privilège de non-divulgation, que l'étiquette `PUBLIC`
    n'est jamais exhaustive. La taire serait pire que l'omettre.
    """
    lignes = {r["code"]: r["note"] for r in _vocab("privileges")["items"]}
    for code, p in prot.PRIVILEGES.items():
        assert f"niveau {p.niveau}" in lignes[code], code
        if p.reserve:
            assert p.reserve in lignes[code], code
        if p.implique:
            for cible in p.implique:
                assert cible in lignes[code], (code, cible)


def test_la_regle_d_axe_voyage_avec_chaque_qualification():
    # Annexe C : la qualification de l'écrit n'a de sens que sur un moyen
    # ECRIT, et le modèle doit l'apprendre AVANT le refus, pas par lui.
    for r in _vocab("qualifications_ecrit")["items"]:
        if r["code"] != "NON_DETERMINE":
            assert "ECRIT" in r["note"], r["code"]


def test_aucun_vocabulaire_d_analyse_n_est_tronque():
    # Le plafond est à 200 et la plus grande table en compte 42 : une
    # troncature silencieuse enseignerait un vocabulaire incomplet comme
    # s'il était complet.
    for kind in ("sous_natures", "privileges", "moyens_preuve",
                 "qualifications_ecrit"):
        assert _vocab(kind)["truncated"] is False, kind
