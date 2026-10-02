"""Construit le greffon claude.ai « pallas-athena » (compétence 2.0.0)
depuis sa source manuscrite et le registre des outils du connecteur.

    python -m scripts.exporter_plugin_pallas [--sortie CHEMIN.plugin]
                                             [--deplie DOSSIER] [--check]

La source vit à la racine du dépôt, dans ``plugin/pallas-athena/``, HORS
d'``athena/`` : elle n'est jamais déployée. Le produit est une archive
``dist/pallas-athena-<version>.plugin`` (``dist/`` n'est pas versionné) de
même disposition que la 1.3.0 — ``.claude-plugin/plugin.json``,
``.mcp.json``, ``README.md``, ``skills/pallas-athena/…`` — qu'on téléverse
dans la page des plugins de l'organisation, sur claude.ai.

Pourquoi générer une partie du texte. Tout ce qui peut dériver du code est
DÉRIVÉ du registre (``mcp.tools``, ``mcp.disclosure``) plutôt que recopié :
la 1.3.0 recopiait des comptes d'outils, un préfixe et des vocabulaires, et
les trois étaient faux au moment de la refonte. La source porte donc des
lignes ``{{GEN:…}}``, seules sur leur ligne, que ce script remplit :

* ``{{GEN:version}}`` — « Compétence 2.0.0 — registre ‹12 hex› », empreinte
  des noms d'outils et de leurs schémas d'entrée : une compétence
  construite contre un autre registre le dit dès sa deuxième ligne ;
* ``{{GEN:charger}}`` — sous le titre d'une recette, la ligne « Charger »
  que Claude Code passe en UN ToolSearch ``select:``. Elle se lit sur les
  lignes Déclencheur et Appels SEULEMENT, jamais Arrêt ni À éviter : ces
  deux-là nomment précisément les outils qu'il ne faut pas appeler, et une
  ligne bâtie sur tout le bloc ferait charger à R1 les descentes qu'il
  interdit. Une recette sœur du même fichier que ces lignes citent
  (« D1 pour les parties ») y ajoute ses outils ;
* ``{{GEN:seule_application}}`` — les promesses du registre
  (``disclosure.general_nevers()``) que le noyau de sécurité ne porte PAS
  (``in_core`` faux) : ce sont celles qu'un client qui coupe les
  INSTRUCTIONS à 2 048 caractères perd ;
* ``{{GEN:limites_reprise}}`` — les propriétés homonymes dont le
  ``maxLength`` diffère d'un outil à l'autre, limitées à celles qu'un outil
  de la recette déclare ;
* ``{{GEN:outils_comptables}}`` — la ligne « Charger » des outils qui ne
  paraissent que sous l'autorisation distincte « Comptabilité ».

``references/index-outils.md`` est généré en entier : chaque outil visible
sous l'autorisation d'écriture, par famille du registre, avec ses entrées
requises et ses indicateurs ; les outils comptables dans une section à part.

Ce script est PUR et LOCAL : il importe ``mcp.tools`` et ``mcp.disclosure``
(qui n'importent aucun modèle), jamais ``models`` ; il ne touche ni au
réseau ni à la base, et refuse de tourner sous ``ENV=production``, où la
configuration irait chercher ses secrets. Il refuse aussi de produire une
archive hors budget ou dont un gabarit reste non rempli : la porte de
déploiement (``tests/test_plugin_pallas.py``) vérifie le même produit.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Iterable, Optional

ATHENA = Path(__file__).resolve().parent.parent
RACINE = ATHENA.parent
SOURCE = RACINE / "plugin" / "pallas-athena"
DIST = RACINE / "dist"

sys.path.insert(0, str(ATHENA))

NOM = "pallas-athena"
VERSION = "2.0.0"

SKILL = "skills/pallas-athena/SKILL.md"
INDEX = "skills/pallas-athena/references/index-outils.md"
RECETTES = tuple(
    f"skills/pallas-athena/recettes/{nom}.md"
    for nom in ("facturation", "dossiers", "notes-documents", "agenda",
                "reprise", "comptabilite")
)
COMPTABILITE = "skills/pallas-athena/recettes/comptabilite.md"
REPRISE = "skills/pallas-athena/recettes/reprise.md"
PLUGIN_JSON = ".claude-plugin/plugin.json"
MCP_JSON = ".mcp.json"
README = "README.md"

#: Les fichiers de la SOURCE, et rien d'autre : un fichier de trop (une
#: copie de travail, une sauvegarde d'éditeur) partirait dans l'archive.
SOURCES: tuple[str, ...] = (PLUGIN_JSON, MCP_JSON, README, SKILL, *RECETTES)
#: Ce que le script ajoute.
GENERES: tuple[str, ...] = (INDEX,)

#: ``.mcp.json`` déclare le connecteur PAR SON NOM, octet pour octet celui de
#: la 1.3.0. Une URL masquerait, dans Claude Code, le connecteur claude.ai
#: sans pouvoir s'authentifier (l'OAuth de production refuse un rappel en
#: boucle locale).
MCP_JSON_ATTENDU = b'{\n  "mcpServers": {\n    "Pallas Athena": {}\n  }\n}\n'

#: Plafonds en OCTETS (UTF-8), fichier livré. SKILL.md se charge en entier
#: quand la compétence se déclenche : à ~3 k jetons, il survit entier à une
#: compaction de Claude Code. Les recettes se lisent une par une.
BUDGETS: dict[str, int] = {
    PLUGIN_JSON: 512,
    README: 3_072,
    SKILL: 10_240,
    "skills/pallas-athena/recettes/facturation.md": 4_096,
    "skills/pallas-athena/recettes/dossiers.md": 4_096,
    "skills/pallas-athena/recettes/notes-documents.md": 4_608,
    "skills/pallas-athena/recettes/agenda.md": 3_584,
    REPRISE: 2_048,
    COMPTABILITE: 2_560,
    INDEX: 5_120,
}
#: Un fichier LIÉ depuis SKILL.md (recette, référence) de plus de 100
#: lignes demande une table des matières (guide des compétences) ; aucun
#: ici n'en a besoin. SKILL.md, lui, se charge en entier : son plafond est
#: celui des octets.
LIGNES_MAX = 100
#: La DESCRIPTION de la compétence : plafond de claude.ai.
DESCRIPTION_MAX = 1_024

#: La fin du NOYAU (Noyau + Conventions) : ce titre, le premier qui les
#: suit, doit commencer avant cet octet de SKILL.md. Une troncature et une
#: compaction gardent toutes deux le DÉBUT du fichier : ce qui gouverne une
#: écriture doit y être.
FIN_DU_NOYAU = "## L'application en une minute"
FIN_DU_NOYAU_MAX = 3_500

_GABARIT = re.compile(r"^\{\{GEN:([a-z_]+)\}\}$")
_RECETTE_ID = re.compile(r"^#{2,3} ((?:Doc|[RFDNA])\d+)\b")
_CHAMP = re.compile(r"^- \*\*([^*]+)\*\*")
_SPAN = re.compile(r"`([^`\n]+)`")
_IDENT = re.compile(r"([a-z][a-z0-9_]*)(?=\(|$)")

# Les variables sans lesquelles ``config.Config`` refuse de s'évaluer. Le
# registre n'en lit aucune : on les pose (sans jamais écraser une valeur
# présente) pour importer ``mcp`` hors de l'application, comme le font les
# tests. Sous ENV=production, la configuration irait au Secret Manager : le
# script refuse plutôt (voir _registre).
_ENV_HORS_LIGNE: tuple[tuple[str, str], ...] = (
    ("SECRET_KEY", "exporteur-hors-ligne"),
    ("FIREBASE_PROJECT_ID", "exporteur-hors-ligne"),
    ("FIREBASE_STORAGE_BUCKET", "exporteur-hors-ligne"),
    ("AUTHORIZED_USER_EMAIL", "exporteur@hors-ligne.invalid"),
)

# Horodatage FIXE des membres de l'archive : deux constructions d'une même
# source doivent donner les mêmes octets.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class ErreurDeConstruction(Exception):
    """La source ne se construit pas : gabarit inconnu, recette sans outil,
    fichier en trop ou manquant. Le message nomme le fichier."""


# ── Le registre ─────────────────────────────────────────────────────────

_REGISTRE: Optional[tuple] = None


def _registre():
    """``(mcp.tools, mcp.disclosure)``, importés une fois."""
    global _REGISTRE
    if _REGISTRE is None:
        if os.environ.get("ENV") == "production":
            raise ErreurDeConstruction(
                "ENV=production : la configuration irait chercher ses secrets "
                "au Secret Manager. Construire hors production."
            )
        for nom, valeur in _ENV_HORS_LIGNE:
            os.environ.setdefault(nom, valeur)
        from mcp import disclosure, tools  # noqa: E402 — après l'environnement
        _REGISTRE = (tools, disclosure)
    return _REGISTRE


def outils_ecriture_visibles() -> list[str]:
    """Les outils visibles sous l'autorisation d'écriture — lectures et
    écritures, les comptables exclus —, dans l'ordre du registre."""
    tools, _ = _registre()
    return [n for n in tools.TOOLS if n not in tools.ACCOUNTING_TOOLS]


def outils_comptables() -> list[str]:
    """Les outils de l'autorisation distincte « Comptabilité », dans l'ordre
    du registre."""
    tools, _ = _registre()
    return [n for n in tools.TOOLS if n in tools.ACCOUNTING_TOOLS]


def empreinte_registre() -> str:
    """12 hex : l'empreinte des noms d'outils et de leurs schémas d'entrée
    (tous les outils, comptables compris). Elle change dès qu'un outil est
    ajouté, retiré, renommé, ou qu'un de ses paramètres change."""
    tools, _ = _registre()
    corps = [[nom, tools.TOOLS[nom]["input_schema"]] for nom in sorted(tools.TOOLS)]
    brut = json.dumps(corps, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()[:12]


def ligne_version() -> str:
    return f"Compétence {VERSION} — registre {empreinte_registre()}"


# ── Les blocs générés ───────────────────────────────────────────────────

def _texte_sans_balisage(fragment: str) -> str:
    """Un ``fr`` du registre (balisage statique) en texte simple : balises
    retirées, entités décodées, espaces insécables rendues ordinaires."""
    texte = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    texte = texte.replace("\xa0", " ")
    return re.sub(r" {2,}", " ", texte).strip()


# Les promesses hors du noyau qui nomment une capacité de l'APPLICATION —
# celles où l'avocat doit être renvoyé — et leur rendu pour Claude : la
# phrase coupée à son premier « — » (``coupe``), puis ``suite``. Le texte
# du registre s'adresse à l'avocat sur l'écran de consentement (« ci-dessus »,
# « vous seul ») ; il ne se recopie pas tel quel dans un texte lu par Claude.
SEULE_APPLICATION = {
    "trust": (False, " sans l'autorisation distincte « Comptabilité »"),
    "document": (True, ""),
    "template_version": (True, " : chaque version se rétablit dans l'application"),
    "link": (True, ""),
    "active_template": (True, " : l'avocat seul, dans l'application"),
}
# Les autres décrivent un comportement d'outil que sa description porte déjà.
HORS_SEULE_APPLICATION = ("invoice_sources", "uncancel")


def bloc_seule_application() -> list[str]:
    """Les capacités que seule l'application offre, tirées du registre."""
    _, disclosure = _registre()
    hors_noyau = {n.key: n for n in disclosure.general_nevers() if not n.in_core}
    connues = set(SEULE_APPLICATION) | set(HORS_SEULE_APPLICATION)
    if set(hors_noyau) != connues:
        raise ErreurDeConstruction(
            "promesses hors noyau à classer dans SEULE_APPLICATION ou "
            "HORS_SEULE_APPLICATION : "
            + ", ".join(sorted(set(hors_noyau) ^ connues)))
    lignes = []
    for cle, (coupe, suite) in SEULE_APPLICATION.items():
        texte = _texte_sans_balisage(hors_noyau[cle].fr)
        if coupe:
            texte = texte.split(" — ", 1)[0]
        lignes.append(f"- {texte}{suite}")
    return lignes


def _ligne_charger(noms: Iterable[str], note: str = "") -> str:
    liste = ", ".join(f"`{n}`" for n in noms)
    return f"- **Charger**{note} : {liste}"


def bloc_outils_comptables() -> list[str]:
    return [_ligne_charger(outils_comptables(),
                           " (autorisation Comptabilité seulement)")]


def _parcourir(proprietes: dict, parent: str = ""):
    """``(chemin_parent, nom, schéma)`` de chaque propriété, objets et
    tableaux d'objets imbriqués compris."""
    for nom, schema in proprietes.items():
        yield parent, nom, schema
        if not isinstance(schema, dict):
            continue
        if isinstance(schema.get("properties"), dict):
            yield from _parcourir(schema["properties"],
                                  f"{parent}.{nom}" if parent else nom)
        items = schema.get("items")
        if isinstance(items, dict) and isinstance(items.get("properties"), dict):
            yield from _parcourir(items["properties"],
                                  f"{parent}.{nom}" if parent else nom)


def _plafonds_par_propriete() -> dict[str, list[tuple[str, str, int]]]:
    """``nom → [(outil, chemin_parent, maxLength)]`` sur les outils visibles
    sous l'autorisation d'écriture."""
    tools, _ = _registre()
    table: dict[str, list[tuple[str, str, int]]] = {}
    for outil in outils_ecriture_visibles():
        props = tools.TOOLS[outil]["input_schema"].get("properties", {})
        for parent, nom, schema in _parcourir(props):
            if isinstance(schema, dict) and isinstance(schema.get("maxLength"), int):
                table.setdefault(nom, []).append((outil, parent, schema["maxLength"]))
    return table


def _occurrence(outil: str, parent: str) -> str:
    return f"`{outil}` ({parent})" if parent else f"`{outil}`"


def bloc_limites(outils_recette: Iterable[str]) -> list[str]:
    """Les propriétés homonymes dont le ``maxLength`` diffère d'un outil à
    l'autre, pour celles qu'un outil de la recette déclare : d'abord les
    plafonds des outils de la recette, du plus grand au plus petit ; puis
    « ailleurs », les autres outils dont le plafond diffère du plus grand
    de la recette — nommés s'ils sont deux au plus, en fourchette sinon."""
    retenus = set(outils_recette)
    lignes = []
    for nom, occurrences in sorted(_plafonds_par_propriete().items()):
        if len({v for _, _, v in occurrences}) < 2:
            continue
        dans = [o for o in occurrences if o[0] in retenus]
        if not dans:
            continue
        # « Ailleurs » : les autres outils dont le plafond n'est pas le plus
        # grand de la recette — ceux qui refuseraient ce qu'elle écrit.
        plus_grand = max(v for _, _, v in dans)
        hors = [o for o in occurrences
                if o[0] not in retenus and o[2] != plus_grand]
        parts = []
        for valeur in sorted({v for _, _, v in dans}, reverse=True):
            qui = ", ".join(_occurrence(o, p) for o, p, v in dans if v == valeur)
            parts.append(f"{valeur} {qui}")
        if hors:
            valeurs = sorted({v for _, _, v in hors})
            fourchette = (str(valeurs[0]) if len(valeurs) == 1
                          else f"{valeurs[0]} à {valeurs[-1]}")
            if len(hors) <= 2:
                qui = ", ".join(_occurrence(o, p) for o, p, _ in hors)
                parts.append(f"ailleurs {fourchette} ({qui})")
            else:
                parts.append(f"ailleurs {fourchette}")
        lignes.append(f"- `{nom}` : " + " ; ".join(parts))
    return lignes


# ── L'index des outils ──────────────────────────────────────────────────

def _jeton_outil(nom: str) -> str:
    """``outil(entrées requises)`` et ses indicateurs, pour l'index.

    ``idempotency_key`` ne figure pas dans les entrées : l'indicateur C dit
    qu'elle est exigée (le registre l'impose alors dans ``required``)."""
    tools, _ = _registre()
    schema = tools.TOOLS[nom]["input_schema"]
    props = schema.get("properties", {})
    requis = [r for r in schema.get("required", []) if r != "idempotency_key"]
    jeton = f"`{nom}({', '.join(requis)})`" if requis else f"`{nom}`"
    marques = []
    if nom in tools.EDIT_TOOLS:
        marques.append("R")
    if (nom in tools.WRITE_TOOLS
            and tools.idempotency_policy(nom) == tools.IDEMPOTENCY_REQUIRED):
        marques.append("C")
    plafond = props.get("limit", {}).get("maximum") if isinstance(
        props.get("limit"), dict) else None
    if isinstance(plafond, int):
        marques.append(f"≤{plafond}")
    pagination = [mot for cle, mot in (("cursor", "curseur"),
                                        ("offset", "offset"),
                                        ("page_range", "pages"))
                  if cle in props]
    if pagination:
        marques.append("/".join(pagination))
    return f"{jeton} {' '.join(marques)}".rstrip()


def index_outils() -> str:
    tools, disclosure = _registre()
    visibles = outils_ecriture_visibles()
    lectures = [n for n in visibles if n not in tools.WRITE_TOOLS]
    lignes = [
        "# Index des outils",
        "",
        f"{ligne_version()}. Généré depuis le registre : ne pas modifier.",
        ("À lire seulement quand aucune recette ne convient ; la description "
         "de l'outil, lue avant son premier appel, fait foi."),
        ("Entre parenthèses : les entrées requises. R : remplace une valeur "
         "stockée (`destructiveHint`). C : `idempotency_key` exigée. ≤N : "
         "plafond de `limit`. Pagination : curseur (`cursor`), offset "
         "(`offset`), pages (`page_range`)."),
        "",
        "## Lecture",
        "",
    ]
    lignes += [f"- {_jeton_outil(n)}" for n in lectures]
    lignes += ["", "## Écriture, par famille", ""]
    for famille in disclosure.families_for(disclosure.SCOPE_WRITE):
        membres = " · ".join(_jeton_outil(n) for n in famille.tools)
        lignes.append(f"- **{famille.label}** : {membres}")
    lignes += [
        "",
        "## Grant Comptabilité",
        "",
        ("Visibles seulement sous l'autorisation distincte « Comptabilité », "
         "que l'autorisation d'écriture ne remplace jamais ; absents, "
         "renvoyer à l'application."),
        "",
    ]
    lignes += [f"- {_jeton_outil(n)}" for n in outils_comptables()]
    return "\n".join(lignes) + "\n"


# ── Le remplissage de la source ─────────────────────────────────────────

def _lire_texte(chemin: Path) -> str:
    """Le texte d'un fichier source, normalisé : UTF-8 sans BOM, fins de
    ligne LF, un seul saut final. Un dépôt extrait sous Windows
    (``core.autocrlf``) rendrait sinon une autre archive."""
    brut = chemin.read_bytes()
    if brut.startswith(b"\xef\xbb\xbf"):
        brut = brut[3:]
    texte = brut.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return texte.rstrip("\n") + "\n"


def _blocs_recettes(lignes: list[str]) -> list[tuple[int, int]]:
    """``(début, fin)`` de chaque bloc portant un gabarit « Charger » : du
    titre qui le précède au titre suivant."""
    blocs = []
    for i, ligne in enumerate(lignes):
        if ligne in ("{{GEN:charger}}", "{{GEN:outils_comptables}}"):
            debut = next((j for j in range(i - 1, -1, -1)
                          if lignes[j].startswith("#")), None)
            if debut is None:
                raise ErreurDeConstruction(
                    f"{ligne} hors d'une recette (aucun titre au-dessus)")
            fin = next((j for j in range(i + 1, len(lignes))
                        if lignes[j].startswith("#")), len(lignes))
            blocs.append((debut, fin))
    return blocs


def _outils_appeles(lignes: list[str]) -> list[str]:
    """Les outils nommés (entre accents graves) sur les lignes Déclencheur
    et Appels d'un bloc, dans l'ordre de première mention."""
    tools, _ = _registre()
    vus: list[str] = []
    for ligne in lignes:
        champ = _CHAMP.match(ligne)
        if not champ or champ.group(1) not in ("Déclencheur", "Appels"):
            continue
        for span in _SPAN.findall(ligne):
            m = _IDENT.match(span)
            if m and m.group(1) in tools.TOOLS and m.group(1) not in vus:
                vus.append(m.group(1))
    return vus


def remplir(chemin: str, texte: str) -> str:
    """Remplit les gabarits ``{{GEN:…}}`` d'un fichier Markdown."""
    lignes = texte.split("\n")
    for i, ligne in enumerate(lignes):
        if "{{GEN:" in ligne and not _GABARIT.match(ligne):
            raise ErreurDeConstruction(
                f"{chemin}:{i + 1} : un gabarit doit être seul sur sa ligne")

    # Les outils de chaque recette, puis ceux des recettes sœurs que ses
    # Appels citent (« D1 pour les parties », « Doc1 d'abord »).
    blocs = _blocs_recettes(lignes)
    propres: dict[int, list[str]] = {}
    ids: dict[str, int] = {}
    for debut, fin in blocs:
        propres[debut] = _outils_appeles(lignes[debut:fin])
        m = _RECETTE_ID.match(lignes[debut])
        if m:
            ids[m.group(1)] = debut
    charger: dict[int, list[str]] = {}
    for debut, fin in blocs:
        noms = list(propres[debut])
        appels = " ".join(l for l in lignes[debut:fin]
                          if l.startswith(("- **Déclencheur**", "- **Appels**")))
        for rid, autre in ids.items():
            cite = re.search(rf"(?<![\w-]){re.escape(rid)}(?![\w-])", appels)
            if autre != debut and cite:
                noms += [n for n in propres[autre] if n not in noms]
        charger[debut] = noms

    sortie: list[str] = []
    debut_courant = None
    for i, ligne in enumerate(lignes):
        if ligne.startswith("#"):
            debut_courant = i
        m = _GABARIT.match(ligne)
        if not m:
            sortie.append(ligne)
            continue
        cle = m.group(1)
        if cle == "version":
            sortie.append(ligne_version())
        elif cle == "charger":
            noms = charger[debut_courant]
            if not noms:
                raise ErreurDeConstruction(
                    f"{chemin}:{i + 1} : la recette « {lignes[debut_courant]} » "
                    "ne nomme aucun outil sur ses lignes Déclencheur et Appels")
            sortie.append(_ligne_charger(noms))
        elif cle == "outils_comptables":
            sortie += bloc_outils_comptables()
        elif cle == "seule_application":
            sortie += bloc_seule_application()
        elif cle == "limites_reprise":
            outils = [n for d in charger.values() for n in d]
            limites = bloc_limites(outils)
            if not limites:
                raise ErreurDeConstruction(
                    f"{chemin}:{i + 1} : aucune limite divergente à citer")
            sortie += limites
        else:
            raise ErreurDeConstruction(
                f"{chemin}:{i + 1} : gabarit inconnu {{{{GEN:{cle}}}}}")
    return "\n".join(sortie)


def _plugin_json(texte: str) -> str:
    donnees = json.loads(texte)
    if donnees.get("name") != NOM:
        raise ErreurDeConstruction(
            f"{PLUGIN_JSON} : « name » doit valoir « {NOM} »")
    donnees.pop("version", None)
    ordonne = {"name": donnees.pop("name"), "version": VERSION, **donnees}
    return json.dumps(ordonne, indent=2, ensure_ascii=False) + "\n"


def construire(source: Optional[Path] = None) -> dict[str, bytes]:
    """Les fichiers livrés, ``chemin POSIX → octets``, triés. *source* :
    la source manuscrite (défaut : ``plugin/pallas-athena`` du dépôt)."""
    source = Path(source if source is not None else SOURCE)
    presents = sorted(
        p.relative_to(source).as_posix()
        for p in source.rglob("*") if p.is_file()
    )
    en_trop = [p for p in presents if p not in SOURCES]
    manquants = [p for p in SOURCES if p not in presents]
    if en_trop or manquants:
        raise ErreurDeConstruction(
            f"source {source} : en trop {en_trop or '—'}, manquants "
            f"{manquants or '—'}")

    fichiers: dict[str, bytes] = {}
    for chemin in SOURCES:
        texte = _lire_texte(source / chemin)
        if chemin == PLUGIN_JSON:
            texte = _plugin_json(texte)
        elif chemin.startswith("skills/"):
            # Le README est pour l'avocat, jamais lu par le modèle : il peut
            # NOMMER la syntaxe des gabarits sans qu'on la remplisse.
            texte = remplir(chemin, texte)
        fichiers[chemin] = texte.encode("utf-8")
    fichiers[INDEX] = index_outils().encode("utf-8")
    return dict(sorted(fichiers.items()))


def problemes(fichiers: dict[str, bytes]) -> list[str]:
    """Ce qui interdit de livrer : budgets d'octets et de lignes, gabarit
    resté non rempli, fin du noyau trop tardive, ``.mcp.json`` changé."""
    erreurs = []
    for chemin, donnees in fichiers.items():
        plafond = BUDGETS.get(chemin)
        if plafond is not None and len(donnees) > plafond:
            erreurs.append(f"{chemin} : {len(donnees)} octets, plafond {plafond}")
        if chemin.endswith(".md"):
            texte = donnees.decode("utf-8")
            if chemin.startswith("skills/") and "{{GEN:" in texte:
                erreurs.append(f"{chemin} : gabarit non rempli")
            lie = chemin.startswith(("skills/pallas-athena/recettes/",
                                     "skills/pallas-athena/references/"))
            if lie and texte.count("\n") >= LIGNES_MAX:
                erreurs.append(
                    f"{chemin} : {texte.count(chr(10))} lignes, plafond "
                    f"{LIGNES_MAX - 1}")
    if fichiers.get(MCP_JSON) != MCP_JSON_ATTENDU:
        erreurs.append(f"{MCP_JSON} : différent de celui de la 1.3.0")
    skill = fichiers.get(SKILL, b"")
    position = skill.find(("\n" + FIN_DU_NOYAU + "\n").encode("utf-8"))
    if position < 0:
        erreurs.append(f"{SKILL} : titre « {FIN_DU_NOYAU} » introuvable")
    elif position + 1 >= FIN_DU_NOYAU_MAX:
        erreurs.append(
            f"{SKILL} : le noyau finit à l'octet {position + 1}, plafond "
            f"{FIN_DU_NOYAU_MAX}")
    return erreurs


def assembler(fichiers: dict[str, bytes]) -> bytes:
    """L'archive ``.plugin`` : un zip, membres triés, horodatage et droits
    fixes — deux constructions d'une même source donnent les mêmes octets."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as zf:
        for chemin in sorted(fichiers):
            info = zipfile.ZipInfo(chemin, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zf.writestr(info, fichiers[chemin], compresslevel=9)
    return tampon.getvalue()


def lire_archive(donnees: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(donnees)) as zf:
        return {nom: zf.read(nom) for nom in sorted(zf.namelist())}


def nom_archive() -> str:
    return f"{NOM}-{VERSION}.plugin"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Construit le greffon claude.ai « pallas-athena » depuis "
            "plugin/pallas-athena et le registre des outils."
        )
    )
    parser.add_argument(
        "--sortie",
        default=str(DIST / nom_archive()),
        help=f"Archive à écrire (défaut : dist/{nom_archive()} à la racine).",
    )
    parser.add_argument(
        "--deplie",
        default="",
        help="Dossier où écrire aussi les fichiers livrés, pour les relire.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=("N'écrit rien : compare une reconstruction à l'archive "
              "--sortie existante ; code 1 si elles diffèrent."),
    )
    args = parser.parse_args(argv)

    # Le compte rendu est en français et porte « — » : une console Windows
    # en cp1252 lèverait UnicodeEncodeError. Même remède que
    # scripts/exporter_competence_analyse.py — les FICHIERS, eux, sont écrits
    # en UTF-8 explicite quoi qu'il arrive.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        # stdout enveloppé ou trop ancien : l'encodage reste tel quel.
        # Volontairement ignoré — cosmétique de sortie, jamais des données.
        pass

    try:
        fichiers = construire()
    except ErreurDeConstruction as exc:
        print(f"ERREUR : {exc}")
        return 1

    print(f"Greffon {NOM} {VERSION} — {ligne_version()}\n")
    for chemin, donnees in fichiers.items():
        plafond = BUDGETS.get(chemin)
        lignes = donnees.count(b"\n")
        etat = "" if plafond is None else (
            f"(plafond {plafond}, {'ok' if len(donnees) <= plafond else 'TROP LONG'})")
        print(f"  {chemin:<52} {len(donnees):>6} octets {lignes:>3} lignes {etat}")

    erreurs = problemes(fichiers)
    if erreurs:
        print("\nREFUSÉ — rien n'est écrit :")
        for erreur in erreurs:
            print(f"  • {erreur}")
        return 1

    archive = assembler(fichiers)
    sortie = Path(args.sortie)

    if args.check:
        if not sortie.is_file():
            print(f"\nAucune archive à comparer : {sortie}")
            return 1
        existante = lire_archive(sortie.read_bytes())
        if existante == fichiers:
            print(f"\nÀ jour : {sortie} est identique à une reconstruction.")
            return 0
        differents = sorted(
            set(existante) ^ set(fichiers)
            | {n for n in set(existante) & set(fichiers) if existante[n] != fichiers[n]}
        )
        print(f"\nPÉRIMÉ : {sortie} diffère d'une reconstruction — "
              "reconstruire et téléverser. Membres en cause :")
        for nom in differents:
            print(f"  • {nom}")
        return 1

    sortie.parent.mkdir(parents=True, exist_ok=True)
    provisoire = sortie.with_name(sortie.name + ".tmp")
    provisoire.write_bytes(archive)
    os.replace(provisoire, sortie)
    print(f"\nArchive : {sortie} ({len(archive)} octets, "
          f"sha256 {hashlib.sha256(archive).hexdigest()[:12]})")

    if args.deplie:
        racine = Path(args.deplie)
        for chemin, donnees in fichiers.items():
            cible = racine / chemin
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(donnees)
        print(f"Fichiers dépliés : {racine}")

    print(
        "\nÀ faire sur claude.ai (compte propriétaire) : téléverser l'archive "
        "dans la page\ndes plugins de l'organisation, à la place de la "
        "version précédente."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
