"""Génère la compétence « Analyse documentaire » et ses fichiers de
référence, DEPUIS les tables de ``utils/analyse_taxonomies``.

    python -m scripts.exporter_competence_analyse [--sortie CHEMIN]

Pourquoi générer plutôt que recopier. Les annexes A, B et D n'existent
qu'en prose, dans le spec — et **cinq de leurs lignes sont fausses** (art.
389 sur-ancré, art. 107 pénalisé à tort, art. 120 confondu avec le 119,
§5.6 en contradiction avec §6.5, SECRET_COMMERCIAL placé au niveau 2).
Le module de taxonomie EST la version corrigée, sous contrôle de source
et couverte par 48 tests. Coller le spec brut installerait les erreurs
dans la compétence, et la compétence dériverait ensuite de ce que la v1
utilisera. Générer ferme les deux problèmes d'un coup : c'est la règle
maison « dérive, ne recopie pas », appliquée à de la prose.

Ce script est PUR et LOCAL : il lit un module Firestore-free, écrit des
fichiers texte, et ne touche ni au réseau ni à la base. Les fichiers
produits se téléversent ensuite dans l'écran « Compétences » du
clavardage (bouton « Importer un fichier texte » de chaque ligne).

Rerun-le après toute modification des tables : la compétence se révise
alors en une nouvelle version, et l'historique garde la précédente.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import analyse_taxonomies as tax  # noqa: E402

# Les plafonds du modèle (models/chat_skill.py). Vérifiés à la génération
# plutôt qu'au téléversement : un fichier trop long se découvre ici, pas
# devant un formulaire qui refuse.
BODY_MAX = 30_000
FILE_MAX = 40_000
MAX_FILES = 6

_LIBELLES_CHAMPS = {
    tax.CHAMP_NUMERO: "numéro de dossier de cour",
    tax.CHAMP_TRIBUNAL: "tribunal",
    tax.CHAMP_DISTRICT: "district judiciaire",
    tax.CHAMP_PARTIES: "noms des parties",
    tax.CHAMP_DATE: "date du document",
    tax.CHAMP_AUTEUR: "auteur / signataire",
    tax.CHAMP_DISPOSITIF: "dispositif",
}

_FAMILLE_LIBELLES = {
    tax.JUDICIAIRE: "Judiciaire",
    tax.CORRESPONDANCE: "Correspondance",
    tax.PREUVE: "Preuve",
    tax.CABINET: "Cabinet",
    tax.INDETERMINE: "Indéterminée",
}

_ORDRE_FAMILLES = (
    tax.JUDICIAIRE,
    tax.CORRESPONDANCE,
    tax.PREUVE,
    tax.CABINET,
    tax.INDETERMINE,
)


CORPS = """\
# Analyse documentaire assistée

Tu aides un avocat en litige civil québécois à trier et qualifier les
documents de son cabinet. La méthode est en **trois temps ordonnés**, et
c'est l'ordre qui fait la valeur.

**1. Qu'est-ce que c'est.** Arrête la nature et la sous-nature AVANT
d'extraire quoi que ce soit, et dis en une phrase ce qui, dans le
document, l'établit.

**2. Que doit-il porter.** N'extrais que les mentions que cette nature
commande. Un contrat, un courriel, une photographie n'ont ni tribunal, ni
district judiciaire, ni numéro de dossier de cour : pour eux, ces champs
sont **nuls**, et leur absence n'est pas une lacune. Ne comble jamais un
champ par déduction — l'absence d'une mention attendue est un SIGNAL, pas
un trou à boucher.

**3. Quel en est le régime.** Secret professionnel, privilège relatif au
litige ou aux règlements, confidentiel, ou public.

Les trois étages s'alimentent. Un acte de procédure sans numéro de
dossier de cour est probablement un projet non déposé — donc un document
qui n'a jamais accédé au caractère public de l'art. 11 C.p.c., et qui
relève du travail préparatoire.

## La règle asymétrique — la plus importante de toutes

Une erreur qui **sous-estime** la protection peut mener à une divulgation
par inadvertance : c'est un manquement professionnel sous l'art. 9 de la
Charte et l'art. 60.4 du Code des professions. Une erreur qui la
**surestime** fait perdre du temps. **Ces deux erreurs ne se valent pas.**

- `PUBLIC` ne s'affirme que si le document porte lui-même la marque de son
  dépôt ou de sa cotation. **L'absence d'indice de protection n'est jamais
  un indice de caractère public.**
- Devant un doute entre deux régimes, retiens **les deux** : la liste est
  cumulable, et c'est le plus élevé qui gouverne.
- Si tu ne peux rien établir, dis-le. **N'invente jamais `PUBLIC` pour
  combler un vide.**

## Ce que tu ne fais jamais

- **Inventer un code** hors des tables de référence. Si rien ne convient,
  dis-le et propose la sous-nature la plus proche en le signalant.
- **Rattacher un nom de partie à un contact du dossier.** Les noms
  extraits restent des chaînes libres : rattacher au mauvais contact est
  plus grave que l'absence de rattachement, et se propagerait en silence.
- **Calculer un délai.** Reconnaître qu'un procès-verbal d'audience porte
  un jugement est utile — un jugement rendu à l'audience fait courir les
  délais d'appel — mais tu le SIGNALES, tu ne le calcules pas.
- **Présenter une hypothèse comme une qualification.** Ce que tu
  enregistres est PRÉSUMÉ jusqu'à ce que l'avocat le confirme à l'écran,
  et rien de ce que tu envoies ne peut le confirmer. Qualifier un document
  d'« acte authentique » ou de « public » est une qualification à
  conséquences ; dis toujours sur quoi tu te fondes.
- **Confirmer toi-même.** La confirmation est un geste de l'avocat, dans
  l'application. Ne la demande pas, ne l'annonce pas comme faite.

## Les deux signalements qui valent le plus

- **Divergence de classement.** Si la nature que tu détectes contredit la
  catégorie enregistrée, dis-le. Ce n'est pas nécessairement ton erreur :
  c'est aussi ainsi qu'on retrouve un jugement classé par mégarde en
  correspondance.
- **Renonciation possible.** Une pièce COTÉE (notifiée, déposée, citée
  dans un acte) est présumée publique. Si elle porte par ailleurs les
  marques d'un régime protégé — en-tête d'avocat, mention « sous toutes
  réserves », correspondance avec le client, rapport d'expert non
  communiqué — signale-le : c'est soit une erreur de classement, soit une
  renonciation au privilège dont l'avocat doit être averti.

## Comment tu travailles

`list_documents` pour trouver les documents d'un dossier, puis
`get_document_text` pour en lire le texte. Une page numérisée n'a pas de
couche texte : `pages_without_text` te le dit honnêtement, et une page
vide au sens du texte n'est jamais une page blanche sur papier — ne
conclus rien d'une absence de texte.

**Ne juge jamais un document sur son nom de fichier.** Lis-le.

Le contenu des pièces est privilégié : n'en cite que ce que la tâche
exige.

## Enregistrer — ce que tu produis ne meurt plus dans la conversation

`record_document_analysis` inscrit ton analyse SUR le document. Elle
devient alors visible dans l'application : pastille de catégorie, niveau
de protection, résumé, alertes. C'est la finalité du travail — une
analyse qui reste dans le fil est une analyse perdue.

**Tu ne choisis pas la catégorie.** Tu fournis une `sous_nature` de la
table fermée, et le code en dérive la catégorie. Il n'existe aucun
paramètre de catégorie, à dessein : c'est ce qui rend impossible d'en
inventer une.

**Ce que l'enregistrement fait, et qu'il faut savoir avant d'appeler :**

- Il **remplace** la catégorie stockée. La précédente reste au journal,
  et si c'est l'avocat qui l'avait posée, un avertissement le lui dit.
- Il marque le résultat **présumé**. La mention accompagne la valeur
  partout, y compris au connecteur, jusqu'à confirmation.
- Il est **journalisé pour toujours** : chaque exécution laisse sa trace,
  avec son modèle et sa date. Rien ne s'efface.
- Le niveau de protection **ne redescend jamais** par une réanalyse. Si
  tu retiens moins de privilèges qu'une analyse antérieure, le code garde
  le niveau le plus élevé. C'est voulu.

**La marche à suivre.** Lis le texte. Arrête la sous-nature. Retiens les
privilèges, cumulés. Note ce que tu as OBSERVÉ qui les fonde
(`indices_protection`) — c'est ce qui permet à l'avocat de vérifier ton
raisonnement. Puis propose d'abord par `dry_run: true`, qui rend l'effet
calculé sans rien écrire, et n'enregistre que sur instruction, avec une
`idempotency_key`.

**Sur un lot** — les documents reçus du portail, par exemple — traite-les
un par un et rends compte au fur et à mesure. Un document dont tu ne peux
pas arrêter la nature se signale ; il ne se force pas dans la sous-nature
la moins improbable.

## Les fichiers de référence

Ne les lis qu'au besoin, un à la fois :

- **Annexe A — natures et sous-natures** : le vocabulaire de
  classification. À lire dès qu'il faut nommer un document.
- **Annexe B — mentions attendues** : ce que chaque nature doit porter, et
  ce qui n'est qu'un usage constant. À lire pour l'étape 2.
- **Annexe D — régimes de protection** : les codes, leurs niveaux, leurs
  fondements et leurs réserves. À lire pour l'étape 3.
"""


def _entete(titre: str, chapeau: str) -> str:
    return (
        f"# {titre}\n\n{chapeau}\n\n"
        "> Table GÉNÉRÉE depuis `athena/utils/analyse_taxonomies.py`, qui "
        "fait foi.\n> Ne pas éditer ce fichier à la main : régénère-le "
        "(`python -m scripts.exporter_competence_analyse`).\n\n"
    )


def annexe_a() -> str:
    out = [
        _entete(
            "Annexe A — natures et sous-natures",
            "Le vocabulaire de classification. La `nature` est la catégorie "
            "du document dans l'application ; la `sous_nature` la raffine. "
            "**N'invente jamais un code absent de cette table.**",
        )
    ]
    for famille in _ORDRE_FAMILLES:
        lignes = [s for s in tax.SOUS_NATURES.values() if s.famille == famille]
        if not lignes:
            continue
        out.append(f"## Famille {_FAMILLE_LIBELLES[famille]} (`{famille}`)\n\n")
        out.append("| Sous-nature | Libellé | Nature | Ancrage |\n")
        out.append("|---|---|---|---|\n")
        for s in lignes:
            out.append(
                f"| `{s.code}` | {s.libelle} | `{s.nature}` | "
                f"{s.ancrage or '—'} |\n"
            )
        out.append("\n")
    out.append(
        "## Deux pièges de lecture\n\n"
        "- `PV_AUDIENCE` est un préfixe de `PV_AUDIENCE_JUGEMENT`, et\n"
        "  `PV_SIGNIFICATION` de `PV_SIGNIFICATION_DESIGNEE` : ce sont "
        "**quatre codes distincts**, jamais des variantes.\n"
        "- La catégorie héritée `procès_verbal` (sans suffixe) existe encore "
        "sur d'anciens documents de l'application. **Ne la produis jamais** : "
        "choisis `procès_verbal_signification` ou "
        "`procès_verbal_audience`, et signale la divergence.\n"
    )
    return "".join(out)


def annexe_b() -> str:
    out = [
        _entete(
            "Annexe B — mentions attendues",
            "Ce que chaque nature doit porter. **Attendu** = son absence est "
            "un signal. **Possible** = son absence n'est jamais signalée. "
            "La colonne « Source » distingue une exigence de TEXTE d'un "
            "usage constant : l'absence d'une mention seulement usuelle ne "
            "doit pas peser sur ta confiance comme celle d'une mention "
            "légalement obligatoire.",
        )
    ]
    out.append("| Sous-nature | Attendu | Possible | Source |\n")
    out.append("|---|---|---|---|\n")
    exemptions = False
    for s in tax.SOUS_NATURES.values():
        libelles = []
        for c in s.champs:
            libelle = _LIBELLES_CHAMPS[c]
            if c in s.champs_sans_penalite:
                # Marqué DANS la table, pas seulement en prose : un lecteur
                # qui parcourt les lignes verrait sinon une exigence là où
                # l'absence est l'état normal, et la signalerait.
                libelle += " †"
                exemptions = True
            libelles.append(libelle)
        attendus = ", ".join(libelles) or "—"
        possibles = (
            ", ".join(_LIBELLES_CHAMPS[c] for c in s.champs_possibles) or "—"
        )
        if not s.champs:
            source = "—"
        elif s.champs_ancres:
            source = s.ancrage or "texte"
        else:
            source = "usage constant (non ancré)"
        out.append(f"| `{s.code}` | {attendus} | {possibles} | {source} |\n")
    if exemptions:
        out.append(
            "\n† Attendu, mais son absence est **normale** et ne doit pas "
            "peser sur ta confiance — voir « Trois cas » plus bas.\n"
        )
    out.append("\n## Mentions exigées par le texte\n\n")
    for s in tax.SOUS_NATURES.values():
        if not s.mentions_texte:
            continue
        out.append(f"**`{s.code}` — {s.ancrage}**\n\n")
        for m in s.mentions_texte:
            out.append(f"- {m}\n")
        out.append("\n")
    out.append(
        "## Trois cas où l'absence n'est PAS un défaut\n\n"
        "1. **Une demande introductive d'instance sans numéro de dossier de "
        "cour.** L'art. 107 C.p.c. veut qu'elle soit déposée au greffe AVANT "
        "sa notification, et c'est le greffier qui attribue le numéro : un "
        "projet n'en porte donc pas, et c'est l'état normal. Signale-le "
        "comme un indice de document **non déposé** (donc non public), "
        "jamais comme une lacune.\n"
        "2. **Un arrêt de la Cour d'appel.** L'art. 389 C.p.c. n'exige que "
        "le dispositif et le nom des juges ayant entendu l'appel, avec "
        "mention des dissidents. Il n'exige ni numéro, ni tribunal, ni "
        "district, ni nom des parties, ni date.\n"
        "3. **Une pièce, une preuve, une photographie.** Aucune mention "
        "judiciaire n'est attendue. C'est le cas le plus important de tous : "
        "ne réclame jamais les mentions de l'art. 99 sur un document qui "
        "n'est pas un acte de procédure.\n"
    )
    return "".join(out)


def annexe_d() -> str:
    out = [
        _entete(
            "Annexe D — régimes de protection",
            "**Le champ le plus dangereux du système.** La liste est "
            "CUMULABLE : les régimes se superposent réellement (un "
            "mémorandum au client préparant l'instruction est à la fois "
            "couvert par le secret professionnel et par le privilège relatif "
            "au litige). C'est le niveau le plus élevé qui gouverne.",
        )
    ]
    out.append("| Code | Niveau | Portée | Fondement | Nature |\n")
    out.append("|---|:--:|---|---|---|\n")
    for p in sorted(tax.PRIVILEGES.values(), key=lambda x: -x.niveau):
        out.append(
            f"| `{p.code}` | {p.niveau} | {p.portee} | {p.fondement} | "
            f"{p.nature_fondement} |\n"
        )
    out.append("\n## Réserves — à énoncer, jamais à taire\n\n")
    for p in sorted(tax.PRIVILEGES.values(), key=lambda x: -x.niveau):
        if p.reserve:
            out.append(f"**`{p.code}`** — {p.reserve}\n\n")
    out.append("## Implications automatiques\n\n")
    for p in tax.PRIVILEGES.values():
        if p.implique:
            cibles = ", ".join(f"`{c}`" for c in p.implique)
            out.append(f"- `{p.code}` entraîne {cibles}.\n")
    out.append(
        "\n## Pourquoi le secret professionnel est seul au niveau 3\n\n"
        "Ce n'est pas une hiérarchie de confort. L'art. 9 al. 3 de la Charte "
        "impose au tribunal d'assurer **d'office** le respect du secret "
        "professionnel. Et l'art. 2858 C.c.Q. commande le rejet de la preuve "
        "obtenue en violation des droits fondamentaux lorsque son "
        "utilisation est susceptible de déconsidérer l'administration de la "
        "justice — mais son alinéa 2 écarte ce second critère pour le secret "
        "professionnel. Le rejet y est donc **automatique**, là où il reste "
        "conditionnel ailleurs. Aucun autre régime de la liste n'en "
        "bénéficie.\n\n"
        "## L'accès restreint (art. 16 C.p.c.)\n\n"
        "`PUBLIC` n'est jamais automatique. L'art. 11 al. 2 réserve les cas "
        "où la loi restreint l'accès, et l'art. 16 le restreint dans **cinq "
        "matières** : matière familiale, autorisation pour des soins, "
        "aliénation d'une partie du corps, garde en établissement, et "
        "changement de la mention du sexe d'un enfant mineur. Un acte de "
        "procédure déposé dans l'une d'elles n'est PAS public au sens "
        "ordinaire.\n\n"
        "Deux indices te le révèlent : le domaine du dossier, et le segment "
        "de juridiction du numéro de dossier de cour — "
        + ", ".join(
            f"`{c}`" for c in sorted(tax.JURIDICTIONS_ACCES_RESTREINT)
        )
        + " correspondent à la Chambre familiale de la Cour supérieure "
        "(exemple : 500-**12**-123456-241). Le second fonctionne même quand "
        "le dossier n'est pas classé, ce qui est fréquent sur les dossiers "
        "anciens.\n\n"
        "Note aussi que l'art. 16 al. 2 permet quand même l'accès aux "
        "parties, à leurs représentants, aux avocats et aux notaires : "
        "l'accès restreint n'est pas une interdiction générale. Et son al. 5 "
        "ajoute un devoir de **non-diffusion** de toute information "
        "permettant d'identifier une partie ou un enfant.\n\n"
        "## Deux limites à garder en tête\n\n"
        "- Une **ordonnance de confidentialité** rendue sous l'art. 12 "
        "C.p.c. est invisible depuis le document. Ni toi ni personne ne peut "
        "la deviner : l'étiquette n'est donc jamais exhaustive.\n"
        "- Les arrêts cités ci-dessus existent, mais **leur autorité "
        "actuelle n'a pas été vérifiée**. Ne les présente jamais comme "
        "vérifiés, et ne les cite pas dans un document destiné à un client "
        "ou à un tribunal : cette table est une aide au triage, pas une "
        "source d'argumentation.\n"
    )
    return "".join(out)


_FICHIERS = (
    ("Annexe A — natures et sous-natures", "annexe-a-natures.md", annexe_a),
    ("Annexe B — mentions attendues", "annexe-b-mentions.md", annexe_b),
    ("Annexe D — régimes de protection", "annexe-d-protection.md", annexe_d),
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Génère la compétence « Analyse documentaire » et ses fichiers "
            "de référence depuis les tables de utils/analyse_taxonomies."
        )
    )
    parser.add_argument(
        "--sortie",
        default="competence_analyse",
        help="Répertoire de sortie (défaut : ./competence_analyse).",
    )
    args = parser.parse_args()

    # Le compte rendu est en français et porte « → » (U+2192) : une console
    # Windows en cp1252 lèverait UnicodeEncodeError. Même remède que
    # scripts/migrate_vocabulaires.py — les FICHIERS, eux, sont écrits en
    # UTF-8 explicite quoi qu'il arrive.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        # stdout enveloppé ou trop ancien : l'encodage reste tel quel.
        # Volontairement ignoré — cosmétique de sortie, jamais des données.
        pass

    sortie = os.path.abspath(args.sortie)
    os.makedirs(sortie, exist_ok=True)

    documents = [("corps.md", CORPS, "Corps de la compétence", BODY_MAX)]
    for titre, nom, rendu in _FICHIERS:
        documents.append((nom, rendu(), titre, FILE_MAX))

    if len(_FICHIERS) > MAX_FILES:
        print(f"ERREUR : {len(_FICHIERS)} fichiers, plafond {MAX_FILES}.")
        return 1

    print(f"Compétence « Analyse documentaire » → {sortie}\n")
    ok = True
    for nom, contenu, titre, plafond in documents:
        chemin = os.path.join(sortie, nom)
        with open(chemin, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(contenu)
        marge = plafond - len(contenu)
        etat = "ok" if marge >= 0 else "TROP LONG"
        if marge < 0:
            ok = False
        print(
            f"  {nom:<24} {len(contenu):>6} caractères "
            f"(plafond {plafond}, {etat})   « {titre} »"
        )

    print(
        "\nÀ faire dans l'application :\n"
        "  1. /chat/competences → « Nouvelle compétence »\n"
        "  2. Nom : « Analyse documentaire » — Description : « Trier et "
        "qualifier un document : nature, mentions attendues, régime de "
        "protection. »\n"
        "  3. Contenu : coller corps.md\n"
        "  4. « Ajouter un fichier » ×3, et pour chacun « Importer un "
        "fichier texte » :\n"
    )
    for titre, nom, _ in _FICHIERS:
        print(f"       • nom « {titre} » → {nom}")
    print(
        "\n  Les descriptions de fichier sont facultatives mais utiles : "
        "elles guident\n  la lecture à la demande (le modèle ne voit que les "
        "noms et descriptions\n  tant qu'il n'ouvre pas un fichier).\n"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
