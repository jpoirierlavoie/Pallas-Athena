"""Rapprochement de noms — aide VISUELLE au contrôle des conflits (L3 §5.2).

Pur : ni Firestore, ni Flask, ni dépendance de *fuzzy matching* (mêmes règles
que ``utils/recours.py`` — le module doit rester unitairement testable).

⚠️ Ce module ne rend AUCUN verdict. Il propose des candidats à l'œil du
juriste, qui décide seul. Un rapprochement manqué n'est pas un feu vert, et un
rapprochement proposé n'est pas un conflit : la vérification déontologique
demeure entière. C'est pourquoi rien ici ne bloque, ne classe ni ne note.

La comparaison est volontairement grossière et explicable — casse et accents
neutralisés, ponctuation et formes juridiques écartées, puis inclusion ou
recouvrement de jetons. Un score sophistiqué serait plus difficile à
justifier qu'utile, et donnerait l'illusion d'une autorité que l'outil n'a
pas.
"""

import re
import unicodedata
from typing import Iterable, NamedTuple

# Formes juridiques et civilités : présentes ou non selon qui saisit, elles
# feraient rater « Béton Nord » ↔ « Béton Nord inc. ». Écartées des DEUX côtés.
_MOTS_VIDES = {
    "inc", "ltee", "ltd", "limitee", "limited", "cie", "compagnie", "corp",
    "corporation", "enr", "senc", "sencrl", "srl", "sec", "co",
    "les", "le", "la", "l", "de", "du", "des", "d", "et", "the", "of",
    "me", "m", "mme", "dr", "dre",
}

# En deçà, un jeton ne discrimine rien (« Le », « St »).
_LONGUEUR_MIN = 2


class Candidat(NamedTuple):
    """Un rapprochement proposé. ``motif`` est destiné à l'affichage."""

    cle: str            # identifiant opaque fourni par l'appelant
    nom: str            # nom tel qu'il est stocké, pour l'affichage
    motif: str          # « nom identique » | « nom très proche » | « jetons communs »


def _plier(texte: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces."""
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", texte or "")
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", sans_accent.lower()).strip()


def jetons(nom: str) -> set[str]:
    return {
        mot for mot in _plier(nom).split()
        if len(mot) >= _LONGUEUR_MIN and mot not in _MOTS_VIDES
    }


def candidats(
    nom: str, existants: Iterable[tuple[str, str]]
) -> list[Candidat]:
    """Rapprocher *nom* d'une liste de ``(clé, nom_stocké)``.

    Trois degrés, du plus au moins sûr — l'ordre du retour les respecte, pour
    que l'œil tombe d'abord sur le plus probant :

    1. **nom identique** — les formes pliées coïncident ;
    2. **nom très proche** — l'une des formes pliées contient l'autre
       (« Béton Nord » ↔ « Béton Nord inc. ») ;
    3. **jetons communs** — au moins deux jetons significatifs partagés, ou un
       seul s'il constitue à lui seul l'un des deux noms.
    """
    cible_plie = _plier(nom)
    cible_jetons = jetons(nom)
    if not cible_plie:
        return []

    exacts: list[Candidat] = []
    proches: list[Candidat] = []
    partiels: list[Candidat] = []

    for cle, autre in existants:
        autre_plie = _plier(autre)
        if not autre_plie:
            continue
        if autre_plie == cible_plie:
            exacts.append(Candidat(cle, autre, "nom identique"))
            continue
        if cible_plie in autre_plie or autre_plie in cible_plie:
            proches.append(Candidat(cle, autre, "nom très proche"))
            continue
        communs = cible_jetons & jetons(autre)
        if not communs:
            continue
        autre_jetons = jetons(autre)
        if (
            len(communs) >= 2
            or communs == cible_jetons
            or communs == autre_jetons
        ):
            partiels.append(Candidat(cle, autre, "jetons communs"))

    return exacts + proches + partiels
