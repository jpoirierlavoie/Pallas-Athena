"""Taxonomies de l'analyse documentaire (pures) — annexes A, B et D.

Le modèle OBSERVE, le code QUALIFIE. Ce module porte les tables ; les
jugements qu'on en tire vivent dans ``utils/analyse_protection.py``.

PUR : ``typing`` + ``functools`` seulement, comme ``utils/taxonomie.py`` et
``utils/phases.py``. **Ne jamais importer ``models``** — ``models/__init__``
construit le client Firestore à l'import, et ce module doit rester
importable par les schémas MCP, les gabarits et la suite de tests sans
Firestore. Les valeurs qui doivent s'accorder avec un modèle (les
``nature``, les codes de juridiction) sont recopiées ici et **épinglées par
test** contre leur source — le précédent ``mcp.tools._DOCUMENT_CATEGORIES``.

**Le contenu juridique de ce module ne change que sur un spec approuvé —
jamais une ligne à la main**, et chaque échéance affichée reste indicative.

Discipline de casse, arrêtée le 2026-08-26 :

* ``nature`` suit ``models.document.VALID_CATEGORIES`` et porte donc ses
  ACCENTS (``procédure``, ``pièce``, ``procès_verbal_signification``) — le
  vocabulaire français d'un champ Firestore, jamais exposé en DAV.
* **Tout code que ce module FRAPPE est ASCII SCREAMING_SNAKE**
  (``PROC_DEM_INTRO``, ``JUDICIAIRE``, ``SECRET_PROFESSIONNEL``) : ce sont
  des membres d'``enum`` JSON sous ``strict``, ils sont journalisables, et
  le code s'appuie sur leurs préfixes.
* Tout ``libelle`` est du français accentué.

⚠ **Préfixes stricts, à l'intérieur même de l'annexe A** : ``PV_AUDIENCE``
est un préfixe de ``PV_AUDIENCE_JUGEMENT``, et ``PV_SIGNIFICATION`` de
``PV_SIGNIFICATION_DESIGNEE``. Accès par dict / égalité stricte SEULEMENT,
jamais ``startswith`` (le piège ``conférence`` / ``conférence_de_gestion``
de ``hearing_type``, transposé).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, NamedTuple, Optional

# ── Familles (dérivées en code, jamais demandées au modèle) ──────────────

JUDICIAIRE = "JUDICIAIRE"
CORRESPONDANCE = "CORRESPONDANCE"
PREUVE = "PREUVE"
CABINET = "CABINET"
INDETERMINE = "INDETERMINE"

VALID_FAMILLES: frozenset = frozenset(
    {JUDICIAIRE, CORRESPONDANCE, PREUVE, CABINET, INDETERMINE}
)

# Les champs de l'annexe B. Noms ASCII, et « parties_nommees_texte » porte
# sa contrainte dans son nom (§7) : ce sont des CHAÎNES LIBRES, jamais
# résolues vers un partie_id — rattacher un nom au mauvais contact est plus
# grave que l'absence de rattachement, et se propagerait en silence.
CHAMP_NUMERO = "numero_dossier_cour"
CHAMP_TRIBUNAL = "tribunal"
CHAMP_DISTRICT = "district_judiciaire"
CHAMP_PARTIES = "parties_nommees_texte"
CHAMP_DATE = "date_document_str"
CHAMP_AUTEUR = "auteur"
CHAMP_DISPOSITIF = "dispositif"


class SousNature(NamedTuple):
    """Une ligne d'annexe A, fusionnée avec sa ligne d'annexe B.

    ``champs`` = les « ✓ » (attendus, leur absence est signalée) ;
    ``champs_possibles`` = les « ○ » (leur absence n'est JAMAIS signalée —
    c'est toute la raison d'être des deux symboles).

    ``champs_ancres`` distingue une exigence de TEXTE d'un usage constant.
    L'annexe B étiquette « usage — non vérifié » plusieurs lignes, mais
    l'étiquette n'avait aucun effet mécanique : ici elle en a un — une
    attente non ancrée s'affiche et ne pèse PAS sur la confiance.

    ``champs_sans_penalite`` retire une attente du calcul de confiance sur
    une ligne par ailleurs ancrée (le cas de l'art. 107, voir la table).
    """

    code: str
    libelle: str
    nature: str
    famille: str
    ancrage: str = ""
    champs: tuple[str, ...] = ()
    champs_possibles: tuple[str, ...] = ()
    champs_ancres: bool = True
    champs_sans_penalite: tuple[str, ...] = ()
    mentions_texte: tuple[str, ...] = ()
    protection_defaut: tuple[str, ...] = ()


# Les six colonnes de l'annexe B pour un acte de procédure (art. 99 al. 2-3).
_ART_99 = (
    CHAMP_NUMERO,
    CHAMP_TRIBUNAL,
    CHAMP_DISTRICT,
    CHAMP_PARTIES,
    CHAMP_DATE,
    CHAMP_AUTEUR,
)
# Les lignes de jugement / d'audience : le district n'y est qu'un « ○ ».
_JUG_ATTENDUS = (
    CHAMP_NUMERO,
    CHAMP_TRIBUNAL,
    CHAMP_PARTIES,
    CHAMP_DATE,
    CHAMP_AUTEUR,
)


def _proc(code: str, libelle: str, ancrage: str, **kw: Any) -> SousNature:
    """Une ligne « acte de procédure » — art. 99 al. 2-3 C.p.c."""
    return SousNature(
        code=code,
        libelle=libelle,
        nature="procédure",
        famille=JUDICIAIRE,
        ancrage=ancrage,
        champs=_ART_99,
        **kw,
    )


_SOUS_NATURES: tuple[SousNature, ...] = (
    # ── Famille JUDICIAIRE — actes de procédure ─────────────────────────
    _proc(
        "PROC_DEM_INTRO",
        "Demande introductive d'instance",
        "art. 100, 107 C.p.c.",
        # Art. 107 al. 1 : la demande introductive « doit être déposée au
        # greffe avant sa notification », et c'est le GREFFIER qui ouvre le
        # dossier et lui attribue son numéro. Un projet n'en porte donc pas
        # — c'est l'état normal, pas une lacune. L'inférence de protection
        # (pas de numéro ⇒ pas déposé ⇒ non public) reste juste et vit dans
        # analyse_protection ; ce qui serait faux, c'est d'en faire un
        # défaut qui pèse sur la confiance.
        champs_sans_penalite=(CHAMP_NUMERO,),
    ),
    _proc("PROC_AVIS_ASSIGN", "Avis d'assignation", "art. 145 C.p.c."),
    _proc("PROC_DEM_INSTANCE", "Demande en cours d'instance", "art. 101 C.p.c."),
    _proc("PROC_PROTOCOLE", "Protocole de l'instance", "art. 148 C.p.c."),
    _proc(
        "PROC_MOYEN_PRELIM",
        "Moyen préliminaire",
        "art. 167 C.p.c.",
    ),
    _proc("PROC_DEFENSE", "Défense écrite", "art. 170 C.p.c."),
    _proc(
        "PROC_EXPOSE_SOMMAIRE",
        "Exposé sommaire des éléments de contestation",
        "art. 148 al. 2 (5°), 170 al. 2 C.p.c.",
    ),
    _proc("PROC_DEM_RECONV", "Demande reconventionnelle", "art. 172 C.p.c."),
    _proc(
        "PROC_INTERVENTION",
        "Intervention volontaire ou forcée",
        "art. 184 C.p.c.",
    ),
    _proc(
        "PROC_DECL_SERMENT",
        "Déclaration sous serment",
        "art. 99, 105 C.p.c.",
        mentions_texte=(
            "le jour et le lieu du serment",
            "les nom et adresse de celui qui le prête",
            "les nom et qualité de celui qui le reçoit",
        ),
    ),
    _proc(
        "PROC_DEM_INSCRIPTION",
        "Demande d'inscription pour instruction et jugement",
        "art. 173 C.p.c.",
    ),
    _proc("PROC_DECL_APPEL", "Déclaration d'appel", "art. 358 C.p.c."),
    _proc("PROC_AUTRE", "Autre acte de procédure", "art. 99 C.p.c."),
    # ── Famille JUDICIAIRE — jugements ──────────────────────────────────
    SousNature(
        code="JUG_JUGEMENT",
        libelle="Jugement de première instance",
        nature="jugement",
        famille=JUDICIAIRE,
        champs=_JUG_ATTENDUS,
        champs_possibles=(CHAMP_DISTRICT,),
        # Aucune disposition repérée n'énumère ces mentions : c'est l'usage
        # constant. L'étiquette a ici un effet — voir SousNature.
        champs_ancres=False,
        protection_defaut=("PUBLIC",),
    ),
    SousNature(
        code="JUG_ARRET",
        libelle="Arrêt de la Cour d'appel",
        nature="jugement",
        famille=JUDICIAIRE,
        # L'art. 389 n'ancre QUE deux choses : « Tout arrêt contient, outre
        # le dispositif, le nom des juges qui ont entendu l'appel, avec
        # mention de celui ou de ceux qui ne partagent pas l'opinion de la
        # majorité. » Il n'exige ni numéro de dossier, ni tribunal, ni
        # district, ni nom des parties, ni date. L'annexe B en marquait
        # cinq « ✓ art. 389 » — une mésattribution qui faisait peser sur la
        # confiance les mauvaises colonnes, précisément à l'envers de la
        # règle « une attente non ancrée pèse moins ».
        ancrage="art. 389 C.p.c.",
        champs=(CHAMP_DISPOSITIF, CHAMP_AUTEUR),
        champs_possibles=(
            CHAMP_NUMERO,
            CHAMP_TRIBUNAL,
            CHAMP_DISTRICT,
            CHAMP_PARTIES,
            CHAMP_DATE,
        ),
        mentions_texte=(
            "le dispositif",
            "le nom des juges ayant entendu l'appel, avec mention des "
            "dissidents",
        ),
        protection_defaut=("PUBLIC",),
    ),
    SousNature(
        code="JUG_ORDONNANCE",
        libelle="Ordonnance",
        nature="jugement",
        famille=JUDICIAIRE,
        champs=_JUG_ATTENDUS,
        champs_possibles=(CHAMP_DISTRICT,),
        champs_ancres=False,
        protection_defaut=("PUBLIC",),
    ),
    # ── Famille JUDICIAIRE — procès-verbaux de signification ────────────
    SousNature(
        code="PV_SIGNIFICATION",
        libelle="Procès-verbal de signification (huissier)",
        nature="procès_verbal_signification",
        famille=JUDICIAIRE,
        ancrage="art. 119 C.p.c.",
        champs=(CHAMP_NUMERO, CHAMP_PARTIES, CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_TRIBUNAL,),
        mentions_texte=(
            "le numéro du dossier du tribunal et le nom des parties",
            "la nature du document signifié",
            "le lieu, la date et l'heure",
            "les nom et, s'il y a lieu, qualité de la personne à qui le "
            "document a été remis — ou, le cas échéant, le lieu où il a "
            "été laissé",
            "le refus ou l'échec de la tentative",
            "l'état des honoraires et frais",
        ),
        protection_defaut=("PUBLIC",),
    ),
    SousNature(
        code="PV_SIGNIFICATION_DESIGNEE",
        libelle="Procès-verbal de notification (personne désignée)",
        nature="procès_verbal_signification",
        famille=JUDICIAIRE,
        # L'art. 120 régit le PV dressé par « une personne désignée par
        # l'huissier » et sa liste est DIFFÉRENTE de celle de l'art. 119 :
        # les nom, qualité et adresse de cette personne, et un récépissé du
        # destinataire (ou la mention de son refus). Une seule ligne ne
        # pouvait pas porter deux listes disjointes — d'où celle-ci.
        ancrage="art. 120 C.p.c.",
        champs=(CHAMP_PARTIES, CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_TRIBUNAL),
        mentions_texte=(
            "les nom, qualité et adresse de la personne désignée",
            "le récépissé du destinataire ou la mention de son refus",
        ),
        protection_defaut=("PUBLIC",),
    ),
    # ── Famille JUDICIAIRE — procès-verbaux d'audience ──────────────────
    SousNature(
        code="PV_AUDIENCE",
        libelle="Procès-verbal d'audience",
        nature="procès_verbal_audience",
        famille=JUDICIAIRE,
        champs=_JUG_ATTENDUS,
        champs_possibles=(CHAMP_DISTRICT,),
        champs_ancres=False,
        protection_defaut=("PUBLIC",),
    ),
    SousNature(
        code="PV_AUDIENCE_JUGEMENT",
        libelle="Procès-verbal d'audience portant jugement",
        nature="procès_verbal_audience",
        famille=JUDICIAIRE,
        # En chambre de pratique, le jugement n'existe souvent QUE dans le
        # procès-verbal. La matrice bascule alors sur celle du jugement, et
        # le dispositif devient une attente.
        champs=_JUG_ATTENDUS + (CHAMP_DISPOSITIF,),
        champs_possibles=(CHAMP_DISTRICT,),
        champs_ancres=False,
        protection_defaut=("PUBLIC",),
    ),
    # ── Famille JUDICIAIRE — transcriptions ─────────────────────────────
    SousNature(
        code="TRANS_INTERROGATOIRE",
        libelle="Notes sténographiques d'interrogatoire",
        nature="transcription",
        famille=JUDICIAIRE,
        champs=(CHAMP_NUMERO, CHAMP_TRIBUNAL, CHAMP_PARTIES, CHAMP_DATE),
        champs_possibles=(CHAMP_DISTRICT, CHAMP_AUTEUR),
        champs_ancres=False,
    ),
    SousNature(
        code="TRANS_AUDIENCE",
        libelle="Notes sténographiques d'audience",
        nature="transcription",
        famille=JUDICIAIRE,
        champs=(CHAMP_NUMERO, CHAMP_TRIBUNAL, CHAMP_PARTIES, CHAMP_DATE),
        champs_possibles=(CHAMP_DISTRICT, CHAMP_AUTEUR),
        champs_ancres=False,
        protection_defaut=("PUBLIC",),
    ),
    # ── Famille CORRESPONDANCE ──────────────────────────────────────────
    SousNature(
        code="CORR_MISE_DEMEURE",
        libelle="Mise en demeure",
        nature="correspondance",
        famille=CORRESPONDANCE,
        # Acte extrajudiciaire (art. 1595 C.c.Q.) : juridiquement exact
        # sous « correspondance », et pratiquement commode — une mise en
        # demeure EST une lettre.
        ancrage="art. 1595 C.c.Q.",
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
    ),
    SousNature(
        code="CORR_CONFRERE",
        libelle="Lettre au confrère",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
        # « sous toutes réserves » fait basculer vers REGLEMENT — mais
        # c'est une OBSERVATION du modèle, pas un défaut de la nature : une
        # lettre au confrère n'en porte pas nécessairement la mention.
        protection_defaut=("CONFIDENTIEL",),
    ),
    SousNature(
        code="CORR_CLIENT",
        libelle="Lettre au client",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
        protection_defaut=("SECRET_PROFESSIONNEL",),
    ),
    SousNature(
        code="CORR_TRIBUNAL",
        libelle="Lettre au tribunal ou au greffe",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
    ),
    SousNature(
        code="CORR_EXPERT",
        libelle="Communication avec un expert",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
        protection_defaut=("LITIGE",),
    ),
    SousNature(
        code="CORR_TIERS",
        libelle="Lettre à un tiers",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
    ),
    SousNature(
        code="CORR_AUTRE",
        libelle="Autre correspondance",
        nature="correspondance",
        famille=CORRESPONDANCE,
        champs=(CHAMP_DATE, CHAMP_AUTEUR),
        champs_possibles=(CHAMP_NUMERO, CHAMP_PARTIES),
        champs_ancres=False,
    ),
    # ── Famille PREUVE ──────────────────────────────────────────────────
    SousNature(
        code="PIECE_COMMUNIQUEE",
        libelle="Pièce cotée, notifiée et déposée",
        nature="pièce",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
        # ⚠ PRÉSOMPTIF, pas absolu. La cotation rend la pièce publique
        # (§5.6) — mais §6.5 existe parce que la même cotation peut être
        # une erreur de classement ou une renonciation par inadvertance.
        # analyse_protection n'applique donc ce défaut QUE si aucun code de
        # niveau ≥ 2 n'a été observé ; sinon l'étiquette et l'alerte se
        # contrediraient sur le même document.
        protection_defaut=("PUBLIC",),
    ),
    SousNature(
        code="PREUVE_CONTRAT",
        libelle="Contrat, entente, quittance",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    SousNature(
        code="PREUVE_COURRIEL",
        libelle="Courriel ou message",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    SousNature(
        code="PREUVE_FACTURE",
        libelle="Facture d'un tiers",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    SousNature(
        code="PREUVE_RELEVE",
        libelle="Relevé, état de compte",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    SousNature(
        code="PREUVE_PHOTO",
        libelle="Photographie",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    SousNature(
        code="PREUVE_RAPPORT_EXPERT",
        libelle="Rapport d'expertise",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
        protection_defaut=("LITIGE",),
    ),
    SousNature(
        code="PREUVE_AUTRE",
        libelle="Autre élément de preuve",
        nature="preuve",
        famille=PREUVE,
        champs_possibles=(CHAMP_DATE, CHAMP_AUTEUR),
    ),
    # ── Famille CABINET ─────────────────────────────────────────────────
    SousNature(
        code="CAB_MANDAT",
        libelle="Mandat, convention d'honoraires",
        nature="mandat",
        famille=CABINET,
        champs=(CHAMP_DATE,),
        champs_ancres=False,
        protection_defaut=("SECRET_PROFESSIONNEL",),
    ),
    SousNature(
        code="CAB_FACTURE",
        libelle="Note d'honoraires du cabinet",
        nature="facture",
        famille=CABINET,
        champs=(CHAMP_DATE,),
        champs_ancres=False,
        protection_defaut=("SECRET_PROFESSIONNEL",),
    ),
    SousNature(
        code="CAB_DEBOURSE",
        libelle="Pièce justificative de déboursé",
        nature="déboursé",
        famille=CABINET,
        champs=(CHAMP_DATE,),
        champs_ancres=False,
    ),
    SousNature(
        code="CAB_MEMO",
        libelle="Mémorandum ou note interne",
        nature="autre",
        famille=CABINET,
        champs=(CHAMP_DATE,),
        champs_ancres=False,
        # Travail préparatoire : les deux régimes se superposent réellement
        # — c'est le cas d'école du cumul (§6.1).
        protection_defaut=("LITIGE", "SECRET_PROFESSIONNEL"),
    ),
    # ── Famille INDETERMINE ─────────────────────────────────────────────
    SousNature(
        code="NON_DETERMINE",
        libelle="Nature indéterminée",
        nature="autre",
        famille=INDETERMINE,
    ),
)

SOUS_NATURES: dict[str, SousNature] = {s.code: s for s in _SOUS_NATURES}
VALID_SOUS_NATURES: frozenset = frozenset(SOUS_NATURES)

# nature → ses sous-natures, dans l'ordre de la table (l'ordre de l'enum).
NATURES: dict[str, tuple[str, ...]] = {}
for _s in _SOUS_NATURES:
    NATURES[_s.nature] = NATURES.get(_s.nature, ()) + (_s.code,)

# Les natures que le modèle peut CHOISIR. La valeur héritée
# « procès_verbal » n'y est pas : elle reste lisible et filtrable dans
# l'application, mais aucune analyse ne doit la produire — c'est la
# scission du 2026-08-26 qui existe pour ça.
VALID_NATURES: frozenset = frozenset(NATURES)


# ── Annexe D — le régime de protection ──────────────────────────────────


class Privilege(NamedTuple):
    """Un code de l'annexe D.

    ``reserve`` n'est pas un commentaire : c'est le texte que l'INTERFACE
    doit rendre à côté du code. Une réserve qui ne vit qu'en commentaire de
    code n'atteint jamais la personne qui décide.

    ``implique`` ferme la liste vers le haut : un code qui est le plus
    souvent une espèce d'un autre l'entraîne, de sorte que la liste dise
    vrai et qu'une recherche ultérieure le retrouve.
    """

    code: str
    niveau: int
    portee: str
    fondement: str
    nature_fondement: str
    reserve: str = ""
    implique: tuple[str, ...] = ()


_PRIVILEGES: tuple[Privilege, ...] = (
    Privilege(
        code="SECRET_PROFESSIONNEL",
        niveau=3,
        portee="Communication avec le client",
        fondement=(
            "art. 9 Charte des droits et libertés de la personne (c-12) ; "
            "art. 60.4 Code des professions (c-26) ; art. 2858 al. 2 C.c.Q."
        ),
        nature_fondement="statutaire",
        # Seul au niveau 3, et ce n'est pas une hiérarchie de confort :
        # l'art. 9 al. 3 impose au tribunal d'assurer d'OFFICE le respect du
        # secret professionnel, et l'art. 2858 écarte pour lui le second
        # critère du rejet de la preuve — le rejet y est automatique, là où
        # il reste conditionnel partout ailleurs.
    ),
    Privilege(
        code="LITIGE",
        niveau=2,
        portee=(
            "Communication avec un expert ou un collaborateur, travail "
            "préparatoire"
        ),
        fondement="Lizotte c. Aviva, 2016 CSC 52 ; Blank c. Canada, 2006 CSC 39",
        nature_fondement="jurisprudentiel",
        reserve=(
            "Fondement jurisprudentiel : l'existence de ces arrêts est "
            "confirmée, leur autorité actuelle ne l'est pas. À vérifier "
            "avant toute utilisation en argumentation."
        ),
    ),
    Privilege(
        code="REGLEMENT",
        niveau=2,
        portee="Offres et pourparlers transactionnels",
        fondement=(
            "art. 4, 606 C.p.c. ; Union Carbide c. Bombardier, 2014 CSC 35"
        ),
        nature_fondement="mixte",
    ),
    Privilege(
        code="ENQUETE_INTERNE",
        niveau=2,
        portee="Documents internes constitués en vue du litige",
        fondement="—",
        nature_fondement="jurisprudentiel",
        reserve=(
            "Le plus souvent une espèce du privilège relatif au litige, "
            "conservée pour sa commodité pratique — sans prétendre à "
            "l'autonomie."
        ),
        implique=("LITIGE",),
    ),
    Privilege(
        code="SECRET_COMMERCIAL",
        niveau=1,
        portee="Données industrielles et commerciales",
        fondement="art. 1472, 1612 C.c.Q.",
        nature_fondement="statutaire",
        # NIVEAU 1, pas 2 (arbitrage 2026-08-26, contre l'annexe D). Les
        # art. 1472/1612 fondent une responsabilité et un calcul du
        # préjudice, JAMAIS une immunité de production : en instance, la
        # protection passe par une ordonnance de confidentialité rendue
        # sous l'art. 12 C.p.c. Le placer au niveau 2 le ferait primer
        # CONFIDENTIEL sur une échelle décrite comme juridique — et le
        # consommateur MCP, qui ne voit que l'entier, le lirait comme une
        # affirmation de droit.
        reserve=(
            "N'est PAS un privilège de non-divulgation : aucune immunité "
            "de production n'en découle. En instance, la protection passe "
            "par une ordonnance rendue sous l'art. 12 C.p.c."
        ),
    ),
    Privilege(
        code="CONFIDENTIEL",
        niveau=1,
        portee="Correspondance externe ; défaut résiduel",
        fondement="—",
        nature_fondement="residuel",
    ),
    Privilege(
        code="PUBLIC",
        niveau=0,
        portee="Acte de procédure déposé, pièce cotée",
        fondement="art. 11 C.p.c.",
        nature_fondement="statutaire",
        reserve=(
            "L'art. 11 al. 2 réserve les cas où la loi restreint l'accès, "
            "et une ordonnance de confidentialité (art. 12 C.p.c.) est "
            "invisible depuis le document : l'étiquette n'est jamais "
            "exhaustive."
        ),
    ),
)

PRIVILEGES: dict[str, Privilege] = {p.code: p for p in _PRIVILEGES}
VALID_PRIVILEGES: frozenset = frozenset(PRIVILEGES)

# INFORMATEUR et INTERET_COMMUN sont VOLONTAIREMENT absents de la v1 :
# application civile étroite pour le premier, fondement purement
# jurisprudentiel et usage rare pour le second. Chaque membre d'un enum
# fermé est une façon pour le modèle de se tromper ; les rajouter est une
# ligne de table, les retirer après coup obligerait à ré-analyser tout ce
# qui les porterait.

# Le libellé de chaque niveau. `None` y figure EXPLICITEMENT : l'absence
# d'étiquette ne vaut pas « public », elle vaut « non déterminé », et un
# rendu qui ferait `.get(x, "Public")` inverserait exactement l'asymétrie
# que tout ce module protège (§6.3 règle 4).
NIVEAU_LABELS: dict[Optional[int], str] = {
    None: "Non déterminé",
    0: "Public",
    1: "Confidentiel",
    2: "Privilégié",
    3: "Secret professionnel",
}


# ── Accès restreint (art. 16 C.p.c.) ────────────────────────────────────

# Les codes de juridiction dont la compétence est « Chambre familiale ».
# RECOPIÉS de models/reference._JURIDICTIONS parce que ce module doit
# rester Firestore-free (importer models construirait le client) ; un test
# les RE-DÉRIVE de la source, le motif _DOCUMENT_CATEGORIES.
JURIDICTIONS_ACCES_RESTREINT: frozenset = frozenset({"04", "12", "13"})

# Le domaine familial de utils/taxonomie.
DOMAINES_ACCES_RESTREINT: frozenset = frozenset({"FAM"})

# ⚠ L'art. 16 C.p.c. vise CINQ matières, pas seulement la famille :
# matière familiale, autorisation pour des soins, aliénation d'une partie
# du corps, garde en établissement, changement de la mention du sexe d'un
# enfant mineur — plus le régime distinct des documents de santé ou
# psychosociaux déposés sous pli cacheté. La taxonomie des domaines n'a de
# code que pour la première ; les quatre autres ne sont donc PAS couvertes
# par les deux ensembles ci-dessus. Le trou est écrit ici plutôt que tu.
# (La juridiction « 14 — Procédures non contentieuses » est l'endroit où
# plusieurs de ces demandes se déposent, mais elle couvre aussi les
# successions et les tutelles ordinaires : l'y inclure en bloc rendrait
# l'étiquette bruyante sur une large population non visée.)
MATIERES_ART_16_NON_COUVERTES: tuple[str, ...] = (
    "autorisation pour des soins",
    "aliénation d'une partie du corps",
    "garde en établissement",
    "changement de la mention du sexe d'un enfant mineur",
)


# ── Accès ───────────────────────────────────────────────────────────────


def get_sous_nature(code: str) -> Optional[SousNature]:
    """Accès STRICT par dict — jamais par préfixe (voir le docstring)."""
    return SOUS_NATURES.get(code)


def famille_of(sous_nature: str) -> str:
    """La famille d'une sous-nature, ``INDETERMINE`` si inconnue.

    Ne lit PAS ``nature`` : l'annexe A place « autre » dans deux familles
    (CAB_MEMO → CABINET, NON_DETERMINE → INDETERMINE) et « preuve » dans
    PREUVE alors que « facture » est dans CABINET. La famille est une
    colonne de la sous-nature, jamais une fonction de la nature.
    """
    entry = SOUS_NATURES.get(sous_nature)
    return entry.famille if entry else INDETERMINE


def nature_of(sous_nature: str) -> str:
    entry = SOUS_NATURES.get(sous_nature)
    return entry.nature if entry else ""


def validate_pair(nature: str, sous_nature: str) -> list[str]:
    """Le couple (nature, sous_nature) est-il cohérent ? Erreurs FR.

    Une paire en désaccord n'est PAS une question de famille — c'est un
    échec de validation, et l'appelant écrit ``statut: "echec"`` plutôt
    que d'enregistrer une classification à moitié vraie. Deux fonctions,
    deux métiers (le motif ``phases.validate_pair``).
    """
    errors: list[str] = []
    entry = SOUS_NATURES.get(sous_nature)
    if entry is None:
        errors.append(f"Sous-nature inconnue : « {sous_nature} ».")
        return errors
    if nature != entry.nature:
        errors.append(
            f"La sous-nature « {sous_nature} » relève de la nature "
            f"« {entry.nature} », pas de « {nature} »."
        )
    return errors


def matrice_champs(
    sous_nature: str, *, contient_dispositif: Optional[bool] = None
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """(attendus, possibles, ancrés) — la ligne d'annexe B qui s'applique.

    **La bascule du §5.4 vit ici**, pas dans le calcul des champs absents :
    un procès-verbal d'audience qui PORTE le jugement lit la ligne
    ``PV_AUDIENCE_JUGEMENT``. La ``sous_nature`` n'est pas réécrite pour
    autant — elle reste ``PV_AUDIENCE``.

    ``contient_dispositif is None`` se comporte comme **True** : la même
    asymétrie qu'au régime de protection, pour une raison plus concrète
    encore — ce qu'on tairait en traitant l'inconnu comme « pas de
    jugement », c'est un délai d'appel qui court.
    """
    if sous_nature == "PV_AUDIENCE" and contient_dispositif is not False:
        sous_nature = "PV_AUDIENCE_JUGEMENT"
    entry = SOUS_NATURES.get(sous_nature)
    if entry is None:
        return (), (), True
    return entry.champs, entry.champs_possibles, entry.champs_ancres


@lru_cache(maxsize=1)
def schema_enums() -> dict[str, list[str]]:
    """Les énumérations que le schéma de l'outil consomme.

    Le module de schéma ne contient AUCUNE liste littérale : il lit celle-ci,
    de sorte qu'une dérive entre la table et le schéma soit structurellement
    impossible. L'ordre est celui de la table (stable — le schéma compilé et
    le préfixe de cache de prompt en dépendent).
    """
    natures: list[str] = []
    for s in _SOUS_NATURES:
        if s.nature not in natures:
            natures.append(s.nature)
    return {
        "nature": natures,
        "sous_nature": [s.code for s in _SOUS_NATURES],
        "privileges": [p.code for p in _PRIVILEGES],
        "famille": [JUDICIAIRE, CORRESPONDANCE, PREUVE, CABINET, INDETERMINE],
    }
