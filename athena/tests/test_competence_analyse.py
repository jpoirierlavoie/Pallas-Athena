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


def _module():
    spec = importlib.util.spec_from_file_location("_exp_analyse", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corps() -> str:
    return _module().CORPS


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


def test_le_corps_impose_un_seul_appel_par_document():
    # L'essai à blanc a été RETIRÉ du protocole d'écriture MCP le
    # 2026-08-27 : `dry_run` n'est plus un paramètre, et les schémas
    # portent `additionalProperties: False` — l'envoyer se fait donc
    # REFUSER, jamais ignorer. Le corps ne doit pas l'enseigner.
    #
    # Ce qui reste vrai, et que ce contrôle garde : un appel par document,
    # avec sa propre clé d'idempotence, parce que le nombre d'appels de
    # modèle par tour est plafonné (`CHAT_CHAIN_MAX_CALLS`) — mesuré le
    # 2026-08-27 : douze appels pour quatre documents sur quarante-cinq.
    corps = _corps()
    assert "dry_run" not in corps
    assert "idempotency_key" in corps
    assert "LOT" in corps or "lot" in corps


# ── Le dépliage ────────────────────────────────────────────────────────
#
# Les littéraux de l'exportateur sont coupés à ~72 colonnes, ce qui est la
# bonne forme pour de la SOURCE : ça se relit, et un diff montre la phrase
# modifiée plutôt que le paragraphe entier. Ce n'est PAS la bonne forme
# pour la destination — un champ de texte que le juriste édite lui-même,
# où du texte pré-coupé oblige à recouper tout le paragraphe pour ajouter
# un mot. Le Markdown se replie seul sur la fenêtre.
#
# `deplier` fait cette conversion, et le seul contrôle qui vaille est
# qu'elle ne PERDE ni ne RÉORDONNE rien.

_NL = chr(10)


def test_deplier_ne_perd_ni_ne_reordonne_aucun_mot():
    # L'invariant fort, et le seul qui protège du dégât silencieux : une
    # jointure fautive avalerait une ligne sans que rien ne le dise.
    # Appliqué au corps ET aux trois annexes générées.
    module = _module()
    textes = [module.CORPS] + [rendu() for _, _, rendu in module._FICHIERS]
    for avant in textes:
        assert module.deplier(avant).split() == avant.split()


def test_deplier_garde_les_retours_d_un_bloc_indente():
    # Un bloc de 4 espaces est du code au sens Markdown : chaque retour y
    # est significatif. L'exemple de compte rendu en lot en est un — joint,
    # ses trois lignes deviendraient une seule phrase illisible.
    module = _module()
    source = _NL.join([
        "Un exemple :",
        "",
        "    premiere ligne",
        "    seconde ligne",
        "",
        "La suite du",
        "texte courant.",
        "",
    ])
    deplie = module.deplier(source)
    assert "    premiere ligne" + _NL + "    seconde ligne" in deplie
    assert "La suite du texte courant." in deplie


def test_deplier_laisse_une_rangee_de_tableau_par_ligne():
    # Une rangée de tableau EST une ligne ; jointes, les annexes A, B et D
    # ne rendraient plus aucun tableau.
    module = _module()
    source = _NL.join(["| a | b |", "| - | - |", "| 1 | 2 |", ""])
    deplie = module.deplier(source)
    assert "| a | b |" in deplie and "| 1 | 2 |" in deplie
    assert "| a | b | | - | - |" not in deplie


def test_deplier_ouvre_une_ligne_par_item_de_liste():
    # Un item continue sur sa propre ligne, mais l'item SUIVANT en ouvre
    # une nouvelle — sans quoi la liste devient un paragraphe.
    module = _module()
    source = _NL.join(
        ["- premier item qui", "  continue ici", "- second item", ""]
    )
    deplie = module.deplier(source)
    assert "- premier item qui continue ici" in deplie
    assert deplie.count("- second item") == 1
    assert "continue ici - second" not in deplie


def test_deplier_ne_colle_pas_deux_lignes_de_citation():
    # Le défaut que le contrôle « mêmes mots » ne peut PAS voir, et qui a
    # bel et bien été livré : joindre deux lignes de citation laisse un
    # « > » littéral au milieu de la phrase, que le rendu affiche tel
    # quel. Les mots y sont tous, dans l'ordre — c'est la mise en forme
    # qui casse. L'en-tête généré des trois annexes est une citation de
    # deux lignes, donc les trois étaient touchées.
    module = _module()
    source = _NL.join(["> Première ligne.", "> Seconde ligne.", ""])
    deplie = module.deplier(source)
    assert "> Première ligne." + _NL + "> Seconde ligne." in deplie
    assert "Première ligne. > Seconde" not in deplie


def test_les_annexes_exportees_gardent_leur_citation_d_en_tete():
    # Le même contrôle de bout en bout, sur le texte réellement produit.
    module = _module()
    for _, _, rendu in module._FICHIERS:
        for ligne in module.deplier(rendu()).split(_NL):
            if ligne.startswith(">"):
                assert " > " not in ligne


def test_deplier_conserve_les_titres_et_les_lignes_vides():
    # La ligne vide est la SEULE frontière de paragraphe du Markdown ; un
    # titre est une ligne, jamais le début d'un paragraphe.
    module = _module()
    source = _NL.join(
        ["## Titre", "Une phrase", "coupée.", "", "Une autre.", ""]
    )
    deplie = module.deplier(source)
    assert deplie.startswith("## Titre" + _NL)
    assert "Une phrase coupée." in deplie
    assert _NL + _NL in deplie


def test_le_corps_exporte_est_deplie():
    # Le contrôle de bout en bout : c'est le FICHIER qui doit être déplié,
    # pas seulement la fonction qui sait le faire. Une ligne de plus de 90
    # colonnes ne peut exister que si le pliage a bien sauté.
    module = _module()
    corps = module.deplier(module.CORPS)
    longues = [ligne for ligne in corps.split(_NL) if len(ligne) > 90]
    assert longues, "aucune ligne longue : le corps n'a pas été déplié"
