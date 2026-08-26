"""Dérivations de l'analyse documentaire (pures) — §5.4, §6.3, §6.4, §6.5.

Le modèle OBSERVE (nature, sous-nature, champs extraits, codes de privilège
observés) ; **le code QUALIFIE**. Tout ce que ce module calcule —
``champs_attendus_absents``, ``niveau_protection``, le régime retenu, les
alertes — est une règle de droit déterministe, et n'a donc rien à faire
dans le jugement d'un modèle.

**Le principe cardinal : échouer vers le haut** (§6.3). Une erreur vers le
bas — marquer publique une pièce privilégiée — peut mener à une divulgation
par inadvertance, c'est-à-dire à un manquement sous l'art. 9 de la Charte et
l'art. 60.4 du Code des professions. Une erreur vers le haut est une gêne.
Les deux ne se valent pas, et ce module est asymétrique en conséquence :

1. Un doute retient le régime le plus protecteur plausible, jamais le moins.
2. Aucun déclassement automatique (la règle vit dans la transaction
   d'écriture, pas ici — mais rien ici ne doit la contredire).
3. L'étiquette n'autorise rien : elle aide au triage, la décision de
   communiquer reste celle de l'avocat.
4. **Le module échoue fermé** : l'absence d'étiquette ne vaut PAS
   ``PUBLIC``, elle vaut « non déterminé » — d'où ``niveau_protection``
   qui rend ``None`` et jamais ``0`` sur une liste vide.

PUR : ``typing`` seulement. Ne jamais importer ``models``.
"""

from __future__ import annotations

from typing import Any, Optional

from utils.analyse_taxonomies import (
    DOMAINES_ACCES_RESTREINT,
    JURIDICTIONS_ACCES_RESTREINT,
    PRIVILEGES,
    SOUS_NATURES,
    matrice_champs,
)

# Les motifs sont des jetons ASCII machine-stables : ils sont
# journalisables (une taxonomie fermée ne révèle rien du document),
# contrairement aux « indices de protection », qui décrivent le document et
# ne quittent jamais Firestore.
MOTIF_PROJET_NON_DEPOSE = "projet_non_depose"
MOTIF_ACCES_RESTREINT_DOMAINE = "acces_restreint_domaine"
MOTIF_ACCES_RESTREINT_NUMERO = "acces_restreint_numero"
MOTIF_DOMAINE_INDETERMINE = "domaine_indetermine"
MOTIF_DEFAUT_RESIDUEL = "defaut_residuel"
# Le défaut PUBLIC d'une nature réputée publique a été ÉCARTÉ parce qu'un
# régime protégé a été observé sur ce document précis. Nommé du côté de ce
# qui s'est passé (la présomption cède), pas du côté de la nature.
MOTIF_PRESOMPTION_PUBLIQUE_ECARTEE = "presomption_publique_ecartee"


def _present(valeur: Any) -> bool:
    """UNE définition de « présent », écrite une fois.

    Une chaîne blanche est absente — «&nbsp;» n'est pas un numéro de
    dossier de cour. Une liste vide est absente.
    """
    if valeur is None:
        return False
    if isinstance(valeur, str):
        return bool(valeur.strip())
    if isinstance(valeur, (list, tuple, set, dict)):
        return bool(valeur)
    return True


# ── Champs attendus ─────────────────────────────────────────────────────


def champs_attendus_absents(
    sous_nature: str,
    extrait: dict,
    *,
    contient_dispositif: Optional[bool] = None,
) -> tuple[str, ...]:
    """Les « ✓ » de l'annexe B que l'extraction ne porte pas.

    Les « ○ » ne sont JAMAIS signalés — c'est toute la raison d'être des
    deux symboles. Rend un tuple **dans l'ordre de la matrice** (jamais un
    ensemble) : cet ordre est ce que l'interface affiche et ce qu'un test
    épingle.
    """
    attendus, _possibles, _ancres = matrice_champs(
        sous_nature, contient_dispositif=contient_dispositif
    )
    return tuple(c for c in attendus if not _present(extrait.get(c)))


def champs_penalisants(
    sous_nature: str,
    absents: tuple[str, ...],
    *,
    contient_dispositif: Optional[bool] = None,
) -> tuple[str, ...]:
    """Parmi les champs absents, ceux qui doivent peser sur la confiance.

    Sans cette fonction, l'étiquette « usage — non vérifié » de l'annexe B
    n'aurait aucun effet mécanique : un procès-verbal d'audience sans
    district judiciaire — qu'AUCUNE disposition n'exige — dégraderait la
    confiance d'une analyse pourtant juste. Deux retraits :

    * une ligne non ancrée ne pénalise rien (l'usage n'est pas le texte) ;
    * ``champs_sans_penalite`` retire une attente sur une ligne par
      ailleurs ancrée — le cas de l'art. 107, où une demande introductive
      NE PEUT PAS porter de numéro de cour avant son dépôt.
    """
    _attendus, _possibles, ancres = matrice_champs(
        sous_nature, contient_dispositif=contient_dispositif
    )
    if not ancres:
        return ()
    entry = SOUS_NATURES.get(sous_nature)
    exemptes = set(entry.champs_sans_penalite) if entry else set()
    return tuple(c for c in absents if c not in exemptes)


# ── Le niveau de protection ─────────────────────────────────────────────


def niveau_protection(privileges) -> Optional[int]:
    """Le maximum des niveaux retenus — ``None`` sur une liste vide.

    **``None``, jamais ``0``.** Rendre 0 pour « le modèle n'a rien dit »
    EST le défaut du §6.3 règle 4 : ``PUBLIC`` n'est atteignable que depuis
    un code explicite, dans la taxonomie, que le modèle a affirmé. Un code
    inconnu est ignoré ici (l'appelant l'a déjà refusé en validation) : ce
    n'est pas à cette fonction de normaliser en silence.
    """
    niveaux = [
        PRIVILEGES[c].niveau for c in (privileges or []) if c in PRIVILEGES
    ]
    return max(niveaux) if niveaux else None


# ── Le régime retenu ────────────────────────────────────────────────────


def _acces_restreint(
    *,
    domaine_dossier: str,
    numeros: tuple[str, ...],
) -> Optional[str]:
    """Le document relève-t-il d'un dossier à accès restreint (art. 16) ?

    Rend le motif, ou ``None``. **Trois signaux, et le deuxième referme un
    trou qui échoue OUVERT** : ``models/dossier._MATTER_TYPE_TO_DOMAINE``
    mappe délibérément « familial » → « " " » (le dossier hérité s'affiche
    « — » jusqu'à sa prochaine classification), si bien qu'un prédicat
    lisant seulement ``domaine == "FAM"`` ne se déclenche PAS sur
    exactement la population que la règle protège. Le segment de
    juridiction du numéro de cour, lui, vient du DOCUMENT et du dossier :
    il fonctionne sans aucun domaine, et c'est d'ailleurs la matière — non
    la classification — que l'art. 16 vise.
    """
    if domaine_dossier in DOMAINES_ACCES_RESTREINT:
        return MOTIF_ACCES_RESTREINT_DOMAINE
    for numero in numeros:
        segment = _segment_juridiction(numero)
        if segment and segment in JURIDICTIONS_ACCES_RESTREINT:
            return MOTIF_ACCES_RESTREINT_NUMERO
    return None


def _segment_juridiction(numero: str) -> str:
    """Les deux chiffres de juridiction d'un « NNN-NN-NNNNNN-NN ».

    Volontairement minimal et tolérant : ce module ne refait pas l'analyse
    du numéro de cour (models/reference la fait), il ne lit qu'un segment
    pour un test d'appartenance.
    """
    parts = str(numero or "").strip().split("-")
    if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) == 2:
        return parts[1]
    return ""


def appliquer_regime(
    *,
    nature: str,
    sous_nature: str,
    privileges,
    champs_absents: tuple[str, ...],
    domaine_dossier: str = "",
    numero_dossier_extrait: str = "",
    numero_dossier_du_dossier: str = "",
) -> tuple[tuple[str, ...], Optional[int], tuple[str, ...]]:
    """(codes retenus, niveau, motifs) — le régime que le CODE arrête.

    L'ordre des étapes est porteur : chacune ne peut que MONTER le niveau,
    sauf la première (l'union), qui part du plus protecteur des deux avis.
    """
    motifs: list[str] = []
    retenus: set[str] = {c for c in (privileges or []) if c in PRIVILEGES}

    entry = SOUS_NATURES.get(sous_nature)
    defauts = set(entry.protection_defaut) if entry else set()

    # 1. Semer depuis la taxonomie — en UNION, jamais en remplacement : le
    #    cumul est le principe (§6.1), et une union ne peut que monter.
    #
    #    Exception présomptive : un défaut PUBLIC (pièce cotée, jugement,
    #    procès-verbal) ne s'applique QUE si rien de protégé n'a été
    #    observé sur CE document. §5.6 conclut de la cotation à la
    #    publicité ; §6.5 existe parce que la même cotation peut être une
    #    erreur de classement ou une renonciation par inadvertance. Les
    #    deux ne peuvent pas être vrais à plat : la cotation rend la pièce
    #    PRÉSOMPTIVEMENT publique, et la présomption cède devant un indice
    #    contraire — sinon l'étiquette et l'alerte se contrediraient sur le
    #    même document.
    if defauts == {"PUBLIC"} and _a_du_protege(retenus):
        motifs.append(MOTIF_PRESOMPTION_PUBLIQUE_ECARTEE)
    else:
        retenus |= defauts

    # 2. Fermer sur les implications (ENQUETE_INTERNE entraîne LITIGE).
    for code in list(retenus):
        retenus |= set(PRIVILEGES[code].implique)

    # 3. Un acte de procédure sans numéro de cour n'a jamais accédé au
    #    caractère public de l'art. 11 : c'est un projet, donc du travail
    #    préparatoire. Le test porte sur les champs ABSENTS, pas sur le
    #    numéro : `champs_absents` encode déjà « cette nature était censée
    #    en porter un ». PUBLIC est ÉCARTÉ — le seul endroit où le code
    #    contredit le modèle vers le bas sur un code, et c'est sûr parce
    #    que l'effet net ne peut que monter le niveau.
    if nature == "procédure" and "numero_dossier_cour" in champs_absents:
        retenus.discard("PUBLIC")
        retenus.add("LITIGE")
        motifs.append(MOTIF_PROJET_NON_DEPOSE)

    # 4. Rabattement de l'art. 16 : l'accès restreint retire le caractère
    #    public. Et un domaine INDÉTERMINÉ ne l'autorise pas non plus — on
    #    ne peut pas établir qu'un dossier non classé n'est pas familial.
    numeros = tuple(
        n for n in (numero_dossier_extrait, numero_dossier_du_dossier) if n
    )
    motif_restreint = _acces_restreint(
        domaine_dossier=domaine_dossier, numeros=numeros
    )
    if motif_restreint:
        retenus.discard("PUBLIC")
        retenus.add("CONFIDENTIEL")
        motifs.append(motif_restreint)
    elif "PUBLIC" in retenus and not str(domaine_dossier or "").strip():
        retenus.discard("PUBLIC")
        retenus.add("CONFIDENTIEL")
        motifs.append(MOTIF_DOMAINE_INDETERMINE)

    # 5. Défaut résiduel — JAMAIS `PUBLIC`.
    if not retenus:
        retenus.add("CONFIDENTIEL")
        motifs.append(MOTIF_DEFAUT_RESIDUEL)

    ordonnes = tuple(
        code for code in PRIVILEGES if code in retenus
    )
    return ordonnes, niveau_protection(ordonnes), tuple(motifs)


def _a_du_protege(codes) -> bool:
    return any(
        PRIVILEGES[c].niveau >= 2 for c in codes if c in PRIVILEGES
    )


# ── Les alertes ─────────────────────────────────────────────────────────


def alerte_dispositif_detecte(
    sous_nature: str, contient_dispositif: Optional[bool]
) -> bool:
    """Un procès-verbal d'audience porte-t-il le jugement ? (§5.4)

    ``None`` compte comme vrai — l'asymétrie, ici aussi.

    Ce que cette fonction ne fait PAS, et ne doit jamais faire : calculer
    un délai. Un jugement rendu à l'audience fait courir les délais
    d'appel, et c'est précisément pour ça qu'on le SIGNALE — mais fonder un
    délai de rigueur sur une lecture automatique demanderait une fiabilité
    que rien n'a encore établie. Aucun appel vers ``utils/deadlines`` ni
    ``models/protocol`` n'a sa place ici (§17).
    """
    if sous_nature == "PV_AUDIENCE_JUGEMENT":
        return True
    if sous_nature == "PV_AUDIENCE":
        return contient_dispositif is not False
    return False


def alerte_renonciation_possible(nature: str, privileges) -> bool:
    """Une pièce cotée porte-t-elle les marques d'un régime protégé ? (§6.5)

    C'est probablement la fonction à plus forte valeur de la phase, et il
    faut la nommer honnêtement : ce n'est pas une conclusion juridique,
    c'est un **détecteur de désaccord** entre ce que le modèle a observé et
    la règle par défaut du code (une pièce cotée est présumée publique).
    Elle SIGNALE — une erreur de classement, ou une renonciation au
    privilège dont l'avocat devrait être averti — et ne modifie aucune
    classification.

    Le seuil est le NIVEAU ≥ 2, pas « autre chose que public » :
    « confidentiel » sur une pièce déposée est du bruit (toute pièce l'était
    avant son dépôt), tandis que le secret professionnel, le privilège
    relatif au litige ou celui des règlements sur une pièce COTÉE est la
    question qui mérite d'être posée. Lire l'ordinal plutôt qu'une liste de
    codes fait survivre la règle à une modification de l'annexe D.

    Les « indices de protection » ne sont délibérément PAS un paramètre :
    ce sont des phrases libres du modèle, et un prédicat sur du texte libre
    serait une expression régulière sur de la prose.
    """
    return nature == "pièce" and _a_du_protege(privileges or [])


def divergence_numero_dossier(
    numero_extrait: str, numero_du_dossier: str
) -> bool:
    """Le numéro lu sur le document contredit-il celui du dossier ?

    Absent du spec, gratuit (les deux valeurs sont déjà en main), fréquent
    (document déposé au mauvais dossier, pièce venant d'un dossier
    connexe) — et c'est aussi un signal de PROTECTION : un document portant
    un numéro de chambre familiale dans un dossier non familial est
    exactement là où le rabattement de l'art. 16 compte, et où le domaine
    n'aurait rien dit.

    Comparaison sur les chiffres seulement : les tirets et les espaces d'un
    numéro transcrit à la main ne sont pas une divergence.
    """
    a = "".join(ch for ch in str(numero_extrait or "") if ch.isdigit())
    b = "".join(ch for ch in str(numero_du_dossier or "") if ch.isdigit())
    return bool(a) and bool(b) and a != b
