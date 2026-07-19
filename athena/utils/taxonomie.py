"""Taxonomie des actions en justice — Québec (litige civil et commercial).

Pure reference data and pure helpers — no Firestore, no Flask — so the
classification stays unit-testable and importable from ``utils.template_fields``
(which must not pull in the Firestore client).

A TWO-LEVEL classification, mirroring how a file is actually opened:

    DOMAINE (20 families, e.g. "REC")  →  ACTION (a named recourse, "REC-01")

Each action carries the delay the source states, its legal *type*, the
factual starting point, the traps, and the statutory references. The dossier
form uses it as a cascading picker (domaine → action), then shows the
action's ``point_depart``/``delai`` as guidance while the user picks the
« droit d'action » date.

WHAT THIS IS NOT
----------------
``delai`` is a **suggestion, never a firm value** — the source document is
explicit about this, and so is art. 2879 s. C.c.Q. reality:

* the starting point is a question of FACT (manifestation, connaissance, fin
  des travaux) that no table can settle;
* interruption and suspension (art. 2889 s.) escape any automatic computation;
* many delays here are NOT prescription at all. ``delai_types`` records which,
  as a tuple of tokens from the closed 11-token vocabulary (spec « échéancier
  par type de délai et avis », § 4): ``PE`` prescription extinctive, ``PA``
  prescription acquisitive (defensive), ``D`` déchéance stricte, ``DR``
  déchéance relevable, ``A`` avis préalable requis, ``R`` délai raisonnable,
  ``N`` aucun délai, ``I`` imprescriptible, ``S`` suit le droit sous-jacent,
  ``V`` variable / à qualifier, ``F`` fenêtre rétrospective. A tuple may carry
  several tokens (``("PE", "A")`` = prescription + avis). ``a_valider`` flags
  a qualification still to confirm at the sources (it replaces the asterisks
  the source used to embed in the delay strings), and ``avis`` carries the
  structured prior-notice obligations (Annexe B).

So ``prescription_type`` is only ever a **suggested** period key into
``utils.recours.PRESCRIPTION_PERIODS``, and it is deliberately ``""`` wherever
the delay is regime-dependent (RCV-05 média vs. general, COR-06 QC vs. féd.),
merely "raisonnable" (CJP-*), retrospective rather than running (FAI-01), or a
catch-all « Autre (préciser) » row. The lawyer confirms every deadline.

Source: « Taxonomie des actions en justice — Droit québécois », v1.1
(15 juillet 2026), itself aligned on the FARBQ table « Prescriptions
extinctives et autres délais » (avril 2026). Both are indicative and
non-exhaustive. GENERATED from that document and verified row-by-row against
it; re-verify against the source before editing a row by hand.
"""

from __future__ import annotations

import functools
from typing import NamedTuple, Optional


class Avis(NamedTuple):
    """One structured prior-notice obligation (Annexe B of the spec).

    ``delai_key`` is a key into ``utils.recours.AVIS_PERIODS`` when the notice
    delay is computable, or ``None`` for checklist items (« délai
    raisonnable », « selon la police »). ``conditionnel`` marks notices that
    apply only in a scenario (défendeur média, défendeur municipal, bien
    délivré…) — the engine only dates them once the user confirms the
    scenario.
    """

    libelle: str                 # « Avis écrit au transporteur (bien délivré) »
    delai_key: Optional[str]     # AVIS_PERIODS key, or None
    point_depart: str            # « à compter de la délivrance du bien »
    reference: str               # « art. 2050 al. 2, C.c.Q. »
    sanction: str = ""           # "" when the source states none
    conditionnel: bool = False   # True = scenario-gated


class Action(NamedTuple):
    """One named recourse under a domaine (spec § 3 field order).

    Data rows are constructed with keyword arguments (only non-defaults
    written) — the table is GENERATED; see the module docstring.
    """

    code: str                          # "REC-01" — stable key, never reused
    libelle: str                       # French name, as the source states it
    delai: str = ""                    # short normalized delay text ("3 ans")
    delai_types: tuple[str, ...] = ()  # § 4 tokens; () for the -99 rows
    a_valider: bool = False            # qualification to confirm at the sources
    point_depart: str = ""             # starting point + traps
    ref_delai: str = ""                # source of the DELAY (ex-references)
    ref_fondement: str = ""            # seat of the right of action (Annexe C)
    avis: tuple[Avis, ...] = ()        # structured notices; () when none
    prescription_type: str = ""        # SUGGESTED utils.recours period key


class Domaine(NamedTuple):
    """A family of actions."""

    code: str              # "REC"
    libelle: str           # "Recouvrement de créances"
    note: str              # the source's doctrinal note for the family
    actions: tuple[Action, ...]


# ── The table ───────────────────────────────────────────────────────────
# Order is the source's own: it is the dropdown order.

DOMAINES: dict[str, Domaine] = {
    "REC": Domaine(
        "REC",
        "Recouvrement de créances",
        "Personnelle · contractuelle · condamnation",
        (
            Action("REC-01", "Action sur compte",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Exigibilité de chaque facture",
                   ref_delai="Arts. 2925 et 2931, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-02", "Prêt, reconnaissance de dette",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Terme; prêt à demande : nuances jurisprudentielles",
                   ref_delai="Arts. 2925 et 2880, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-03", "Effets de commerce",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Exigibilité de l'effet",
                   ref_delai="Art. 2925, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-04", "Cautionnement",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Défaut du débiteur principal; caractère accessoire",
                   ref_delai="Art. 2333, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-05", "Loyers et charges",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Chaque échéance",
                   ref_delai="Art. 2931, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-06", "Honoraires professionnels",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Exigibilité / fin du mandat",
                   ref_delai="Art. 2925, C.c.Q.",
                   prescription_type="3_ans"),
            Action("REC-99", "Autre (préciser)"),
        ),
    ),
    "CON": Domaine(
        "CON",
        "Responsabilité contractuelle",
        "Personnelle · contractuelle · condamnation ou constitutif",
        (
            Action("CON-01", "Exécution en nature",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Défaut",
                   ref_delai="1601",
                   prescription_type="3_ans"),
            Action("CON-02", "Dommages-intérêts",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Manifestation du préjudice",
                   ref_delai="1458, 2926",
                   prescription_type="3_ans"),
            Action("CON-03", "Résolution / résiliation",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Défaut",
                   ref_delai="1604 s.",
                   prescription_type="3_ans"),
            Action("CON-04", "Nullité",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Connaissance de la cause; crainte : sa cessation. Imprescriptible par exception (moyen de défense)",
                   ref_delai="2927, 2882",
                   prescription_type="3_ans"),
            Action("CON-05", "Réduction de l'obligation / du prix",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Idem",
                   ref_delai="1604 al. 3",
                   prescription_type="3_ans"),
            Action("CON-06", "Passation de titre",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Refus de signer",
                   ref_delai="1712",
                   prescription_type="3_ans"),
            Action("CON-07", "Vices cachés",
                   delai="Prescription de 3 ans + Avis dans un délai raisonnable",
                   delai_types=("PE", "A"),
                   point_depart="Découverte; dénonciation écrite dans un délai raisonnable; voir aussi la L.p.c.",
                   ref_delai="Arts. 1726 et 1739, C.c.Q.",
                   prescription_type="3_ans"),
            Action("CON-08", "Bail commercial",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Selon le droit invoqué",
                   ref_delai="1851 s.",
                   prescription_type="3_ans"),
            Action("CON-09", "Assurance — réclamation d'indemnité",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Naissance du droit; avis de sinistre : délais de police",
                   ref_delai="2425 s.",
                   prescription_type="3_ans"),
            Action("CON-10", "Action directe contre l'assureur du responsable",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Suit le recours contre l'assuré",
                   ref_delai="2501",
                   prescription_type="3_ans"),
            Action("CON-99", "Autre (préciser)"),
        ),
    ),
    "RCV": Domaine(
        "RCV",
        "Responsabilité civile",
        "Personnelle · extracontractuelle · condamnation",
        (
            Action("RCV-01", "Préjudice corporel",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="1re manifestation; avis et courts délais inopposables",
                   ref_delai="Arts. 2925, 2926, et 2930, C.c.Q.",
                   prescription_type="3_ans"),
            Action("RCV-02", "Préjudice corporel — acte criminel / violences",
                   delai="10 ans; imprescriptible (violence sexuelle, violence subie pendant l'enfance, violence conjugale); décès de la victime ou de l'auteur : 3 ans du décès",
                   point_depart="Manifestation; aide financière étatique : voir ADM-10 (IVAC)",
                   ref_delai="Art. 2926.1, C.c.Q."),
            Action("RCV-03", "Préjudice matériel",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Municipalités — deux régimes : Loi sur les villes : avis 15 jours (A) + action 6 mois (585); Code municipal : avis 60 jours (A) + action 6 mois (1112.1); fautes ou illégalités : 6 mois (586 LCV)",
                   ref_delai="Art. 2925, C.c.Q.; arts. 585 et 586, Loi sur les villes; art. 1112.1 Code municipale",
                   prescription_type="3_ans"),
            Action("RCV-04", "Préjudice moral",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Manifestation",
                   ref_delai="2926",
                   prescription_type="3_ans"),
            Action("RCV-05", "Diffamation",
                   delai="1 an (P); média/journal : 3 mois + avis préalable 3 jours ouvrables (A)",
                   delai_types=("PE", "A"),
                   point_depart="Connaissance de l'atteinte; média : publication ou sa connaissance (max 1 an de la publication); la courte prescription suppose le respect des formalités par le journal",
                   ref_delai="Art. 2929, C.c.Q.; arts. 2 et 3, Loi sur la presse"),
            Action("RCV-06", "Vie privée, renseignements personnels",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="1 an si l'atteinte est à la réputation",
                   ref_delai="35-41; 2929",
                   prescription_type="3_ans"),
            Action("RCV-07", "Responsabilité professionnelle",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Manifestation; médical → souvent RCV-01/02",
                   ref_delai="Arts. 2925, 2926, C.c.Q.",
                   prescription_type="3_ans"),
            Action("RCV-08", "Produits — fabricant / vendeur spécialisé",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Découverte",
                   ref_delai="1468, 1730",
                   prescription_type="3_ans"),
            Action("RCV-09", "Fait d'autrui, fait des biens, animaux, ruine du bâtiment",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   ref_delai="1459-1467",
                   prescription_type="3_ans"),
            Action("RCV-10", "Troubles de voisinage",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Préjudice continu : renaissance au jour le jour",
                   ref_delai="976",
                   prescription_type="3_ans"),
            Action("RCV-11", "Abus de procédure",
                   delai="3 ans (Prescription)",
                   delai_types=("PE",),
                   point_depart="Fin de l'instance abusive (généralement)",
                   ref_delai="Art. 51, C.p.c.; Art. 2925, C.c.Q.",
                   prescription_type="3_ans"),
            Action("RCV-99", "Autre (préciser)"),
        ),
    ),
    "RES": Domaine(
        "RES",
        "Restitutions et quasi-contrats",
        "Personnelle · ni contractuelle ni délictuelle · condamnation",
        (
            Action("RES-01", "Réception de l'indu",
                   delai="3 ans (art. 2925)",
                   point_depart="Départ : paiement / sa découverte",
                   ref_delai="1491",
                   prescription_type="3_ans"),
            Action("RES-02", "Enrichissement injustifié",
                   delai="3 ans (art. 2925)",
                   point_depart="Caractère subsidiaire; conjoints de fait : fin de la vie commune",
                   ref_delai="1493",
                   prescription_type="3_ans"),
            Action("RES-03", "Gestion d'affaires",
                   delai="3 ans (art. 2925)",
                   ref_delai="1482",
                   prescription_type="3_ans"),
            Action("RES-04", "Restitution des prestations",
                   delai="3 ans (art. 2925)",
                   point_depart="Accessoire à l'anéantissement de l'acte",
                   ref_delai="1699 s.",
                   prescription_type="3_ans"),
            Action("RES-99", "Autre (préciser)"),
        ),
    ),
    "GAG": Domaine(
        "GAG",
        "Protection du gage commun du créancier",
        "",
        (
            Action("GAG-01", "Action oblique",
                   delai="Délai du droit du débiteur exercé",
                   ref_delai="1627"),
            Action("GAG-02", "Action en inopposabilité (paulienne)",
                   delai="1 an (D — déchéance)",
                   delai_types=("D",),
                   point_depart="Connaissance du préjudice; syndic de faillite (pour la masse) : nomination du syndic",
                   ref_delai="Arts. 1631 et 1635, C.c.Q.",
                   prescription_type="1_an"),
            Action("GAG-03", "Simulation / contre-lettre",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Connaissance",
                   ref_delai="1451-1452",
                   prescription_type="3_ans"),
            Action("GAG-99", "Autre (préciser)"),
        ),
    ),
    "IMM": Domaine(
        "IMM",
        "Réel et immobilier",
        "Réelle (sauf indication) · résultat variable",
        (
            Action("IMM-01", "Revendication",
                   delai="Imprescriptible (propriété)",
                   point_depart="Limite pratique : prescription acquisitive d'autrui",
                   ref_delai="953, 2918",
                   prescription_type="imprescriptible"),
            Action("IMM-02", "Servitudes (confessoire, négatoire, extinction)",
                   delai="10 ans (P)",
                   delai_types=("PE",),
                   point_depart="Extinction par non-usage : 10 ans",
                   ref_delai="2923; 1191",
                   prescription_type="10_ans"),
            Action("IMM-03", "Bornage",
                   delai="Imprescriptible",
                   ref_delai="978",
                   prescription_type="imprescriptible"),
            Action("IMM-04", "Action du possesseur troublé",
                   delai="1 an (D)",
                   delai_types=("D",),
                   point_depart="Possession paisible > 1 an requise",
                   ref_delai="929",
                   prescription_type="1_an"),
            Action("IMM-05", "Empiètement / accession",
                   delai="Variable",
                   ref_delai="992 s."),
            Action("IMM-06", "Prescription acquisitive (demande en acquisition)",
                   delai="Possession 10 ans",
                   point_depart="Jugement requis pour l'immeuble",
                   ref_delai="2918"),
            Action("IMM-07", "Copropriété — annulation de décision d'assemblée",
                   delai="90 jours (D)",
                   delai_types=("D",),
                   point_depart="Date de l'assemblée",
                   ref_delai="1103",
                   prescription_type="90_jours"),
            Action("IMM-08", "Fin d'indivision — partage, licitation",
                   delai="Imprescriptible durant l'indivision",
                   ref_delai="1030",
                   prescription_type="imprescriptible"),
            Action("IMM-09", "Expropriation (contestation) et expropriation déguisée",
                   delai="Contestation du droit d'exproprier et radiation de l'avis : 30 jours (D*) de la date de l'expropriation; expropriation déguisée : atteinte continue",
                   delai_types=("D",),
                   point_depart="Nouvelle Loi concernant l'expropriation (2023); qualification du délai à valider (*voir § 4)",
                   ref_delai="Art. 17(1), Loi concernant l'expropriation; Art. 952, C.c.Q."),
            Action("IMM-10", "Radiation d'inscription (registre foncier)",
                   ref_delai="3057 s."),
            Action("IMM-99", "Autre (préciser)"),
        ),
    ),
    "CST": Domaine(
        "CST",
        "Construction",
        "",
        (
            Action("CST-01", "Perte de l'ouvrage (solidité)",
                   delai="Garantie 5 ans (couverture) + action 3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Fin des travaux; manifestation",
                   ref_delai="2118-2119",
                   prescription_type="3_ans"),
            Action("CST-02", "Malfaçons",
                   delai="Garantie 1 an de la réception + action 3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Réception avec/sans réserve",
                   ref_delai="2120",
                   prescription_type="3_ans"),
            Action("CST-03", "Réclamations de chantier (extras, retards)",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Avis contractuels souvent stricts (A)",
                   ref_delai="2098 s.",
                   prescription_type="3_ans"),
            Action("CST-04", "Hypothèque légale de la construction",
                   delai="Inscription : 30 jours de la fin des travaux; action/préavis : 6 mois (Déchéance)",
                   delai_types=("D",),
                   point_depart="Fin des travaux",
                   ref_delai="Art. 2727, C.c.Q.",
                   prescription_type="6_mois"),
            Action("CST-05", "Cautionnements de chantier",
                   delai="Délais de la police (A/D)",
                   delai_types=("D", "A"),
                   point_depart="Avis à la caution"),
            Action("CST-99", "Autre (préciser)"),
        ),
    ),
    "COR": Domaine(
        "COR",
        "Corporatif et commercial",
        "",
        (
            Action("COR-01", "Oppression / redressement (recours pour abus)",
                   delai="3 ans (P) (2925)",
                   delai_types=("PE",),
                   point_depart="Exception : imprescriptible si le recours vise la reconnaissance du droit de propriété sur les actions (position du tableau FARBQ, avril 2026)",
                   ref_delai="450-453 LSAQ; 241 LCSA; 2925",
                   prescription_type="3_ans"),
            Action("COR-02", "Action dérivée (pour le compte de la société)",
                   point_depart="Autorisation préalable du tribunal",
                   ref_delai="445 LSAQ; 239 LCSA"),
            Action("COR-03", "Conventions d'actionnaires (rachat, évaluation)",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   ref_delai="2925",
                   prescription_type="3_ans"),
            Action("COR-04", "Nullité de résolutions; rectification de registres",
                   delai="Diligence"),
            Action("COR-05", "Liquidation / dissolution judiciaire",
                   ref_delai="463 s. LSAQ"),
            Action("COR-06", "Responsabilité des administrateurs — salaires impayés",
                   delai="QC : 3 ans (P), mais poursuite préalable de la société dans 1 an de l'exigibilité; Féd. : durant le mandat ou 2 ans de la cessation (D), mais poursuite préalable de la société dans 6 mois de l'échéance",
                   delai_types=("PE", "D"),
                   point_depart="Deux conditions préalables distinctes — piège fréquent",
                   ref_delai="154 LSAQ + 2925; 119(2)-(3) LCSA"),
            Action("COR-07", "Non-concurrence, non-sollicitation, secrets commerciaux",
                   delai="3 ans (P) + injonction",
                   delai_types=("PE",),
                   ref_delai="2088-2089",
                   prescription_type="3_ans"),
            Action("COR-08", "Concurrence déloyale",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   ref_delai="1457; 7 LMC",
                   prescription_type="3_ans"),
            Action("COR-09", "Vente d'entreprise (garanties, ajustements de prix)",
                   delai="3 ans (P) ou clause de survie (qualification débattue, 2884)",
                   delai_types=("PE",)),
            Action("COR-10", "Responsabilité des administrateurs — résolutions illicites (émission d'actions, commissions, dividendes, rachats, indemnités)",
                   delai="QC : 3 ans de la résolution (2925); Féd. : 2 ans de la résolution",
                   point_depart="Divergence QC/féd. à signaler visuellement",
                   ref_delai="155-156 LSAQ + 2925; 118(7) LCSA"),
            Action("COR-11", "Dissidence — droit de rachat de l'actionnaire",
                   delai="Confirmation auprès de la société : 30 jours (D/A*) de la réception de l'avis de rachat",
                   delai_types=("D", "A"),
                   point_depart="Qualification à valider (*voir § 4)",
                   ref_delai="380 LSAQ",
                   prescription_type="30_jours"),
            Action("COR-99", "Autre (préciser)"),
        ),
    ),
    "HYP": Domaine(
        "HYP",
        "Sûretés et recours hypothécaires",
        "Réelle (accessoire) · condamnation / délaissement",
        (
            Action("HYP-01", "Préavis d'exercice et délaissement",
                   point_depart="Délais de délaissement 10-60 jours selon le recours",
                   ref_delai="2757 s."),
            Action("HYP-02", "Prise en paiement",
                   point_depart="Autorisation judiciaire si ≥ 50 % payé",
                   ref_delai="2778"),
            Action("HYP-03", "Vente sous contrôle de justice / par le créancier",
                   ref_delai="2791; 2784"),
            Action("HYP-04", "Prise de possession à des fins d'administration",
                   ref_delai="2773"),
            Action("HYP-05", "Action personnelle sur la créance garantie",
                   delai_types=("PE",),
                   point_depart="3 ans (P); l'hypothèque s'éteint avec la créance",
                   ref_delai="2797",
                   prescription_type="3_ans"),
            Action("HYP-99", "Autre (préciser)"),
        ),
    ),
    "FAI": Domaine(
        "FAI",
        "Faillite et insolvabilité",
        "Appels en matière de faillite : 10 jours — voir APP-05.",
        (
            Action("FAI-01", "Requête en ordonnance de faillite",
                   delai="Acte de faillite dans les 6 mois précédents",
                   ref_delai="43 LFI"),
            Action("FAI-02", "Proposition / avis d'intention",
                   delai="Délais LFI stricts",
                   ref_delai="50, 50.4 LFI"),
            Action("FAI-03", "Arrangement LACC",
                   ref_delai="LACC"),
            Action("FAI-04", "Nomination d'un séquestre",
                   ref_delai="243 LFI"),
            Action("FAI-05", "Recours du syndic (préférences, opérations sous-évaluées)",
                   delai="Périodes suspectes : 3/12 mois; 1 an / 5 ans",
                   ref_delai="95-96 LFI"),
            Action("FAI-06", "Réclamations, libération, dettes exclues",
                   ref_delai="121 s., 178 LFI"),
            Action("FAI-07", "Libération d'office du failli / opposition à libération",
                   delai="1re faillite : 9 mois (21 mois si versements art. 68); récidive : 24 mois (36 mois) — l'opposition du créancier doit précéder ces échéances",
                   ref_delai="168.1 LFI"),
            Action("FAI-99", "Autre (préciser)"),
        ),
    ),
    "FAM": Domaine(
        "FAM",
        "Familial",
        "Majoritairement constitutif d'état — imprescriptible sauf indication. Appel en matière de divorce : voir APP-06.",
        (
            Action("FAM-01", "Divorce, séparation de corps, dissolution d'union civile",
                   delai="Aucun"),
            Action("FAM-02", "Autorité parentale, temps parental",
                   delai="En tout temps (intérêt de l'enfant)"),
            Action("FAM-03", "Aliments",
                   delai="En tout temps; arrérages : 3 ans",
                   ref_delai="2931"),
            Action("FAM-04", "Patrimoine familial, régimes matrimoniaux",
                   delai="Accessoire à la demande principale",
                   ref_delai="414 s."),
            Action("FAM-05", "Prestation compensatoire (décès)",
                   delai="1 an du décès (P)",
                   delai_types=("PE",),
                   ref_delai="Arts. 427 et 2928, C.c.Q.",
                   prescription_type="1_an"),
            Action("FAM-06", "Union parentale (régime en vigueur depuis le 30 juin 2025)",
                   delai="Régime nouveau — à paramétrer"),
            Action("FAM-07", "Filiation",
                   delai="Imprescriptible entre vifs; 3 ans du décès (de l'enfant ou du parent)",
                   ref_delai="Art. 542.32, C.c.Q."),
            Action("FAM-08", "Conjoints de fait — enrichissement injustifié",
                   delai="3 ans de la fin de la vie commune",
                   ref_delai="1493",
                   prescription_type="3_ans"),
            Action("FAM-99", "Autre (préciser)"),
        ),
    ),
    "SUC": Domaine(
        "SUC",
        "Successions et personnes",
        "",
        (
            Action("SUC-01", "Vérification de testament (non contentieux)",
                   ref_delai="302 s. C.p.c."),
            Action("SUC-02", "Contestation de testament (captation, incapacité)",
                   delai="3 ans (P), connaissance",
                   delai_types=("PE",),
                   ref_delai="2927",
                   prescription_type="3_ans"),
            Action("SUC-03", "Pétition d'hérédité",
                   delai="10 ans de l'ouverture",
                   ref_delai="626",
                   prescription_type="10_ans"),
            Action("SUC-04", "Option de l'héritier (délibération)",
                   delai="6 mois",
                   ref_delai="632",
                   prescription_type="6_mois"),
            Action("SUC-05", "Partage successoral",
                   delai="Imprescriptible durant l'indivision",
                   ref_delai="836 s.",
                   prescription_type="imprescriptible"),
            Action("SUC-06", "Reddition de compte / destitution du liquidateur",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   ref_delai="806 s.",
                   prescription_type="3_ans"),
            Action("SUC-07", "Survie de l'obligation alimentaire",
                   delai="6 mois du décès (D)",
                   delai_types=("D",),
                   ref_delai="Art. 684, C.c.Q.",
                   prescription_type="6_mois"),
            Action("SUC-08", "Tutelle au majeur, mandat de protection",
                   delai="Non contentieux",
                   ref_delai="268 s."),
            Action("SUC-09", "Jugement déclaratif de décès",
                   delai="7 ans d'absence",
                   ref_delai="92 s."),
            Action("SUC-99", "Autre (préciser)"),
        ),
    ),
    "DEC": Domaine(
        "DEC",
        "Déclaratoire, homologation, reconnaissance",
        "Déclaratoire — le délai suit généralement le droit sous-jacent",
        (
            Action("DEC-01", "Jugement déclaratoire",
                   delai="Suit le droit sous-jacent; le moyen de défense est imprescriptible",
                   ref_delai="142 C.p.c.; 2882"),
            Action("DEC-02", "Homologation de transaction",
                   ref_delai="2631 s."),
            Action("DEC-03", "Homologation / annulation de sentence arbitrale",
                   delai="Annulation : 3 mois (D)",
                   delai_types=("D",),
                   ref_delai="645-648 C.p.c.",
                   prescription_type="3_mois"),
            Action("DEC-04", "Reconnaissance de décision étrangère",
                   delai="10 ans (P)",
                   delai_types=("PE",),
                   ref_delai="2924; 3155 s.; 507 s. C.p.c.",
                   prescription_type="10_ans"),
            Action("DEC-99", "Autre (préciser)"),
        ),
    ),
    "CJP": Domaine(
        "CJP",
        "Contrôle judiciaire et pourvois",
        "Légal-statutaire · annulation / ordonnance — « délai raisonnable » (≈ 30 jours en jurisprudence)",
        (
            Action("CJP-01", "Annulation de décision (évocation, certiorari)",
                   delai="Délai raisonnable",
                   ref_delai="529 C.p.c."),
            Action("CJP-02", "Mandamus (accomplissement d'un devoir)",
                   delai="Délai raisonnable",
                   ref_delai="529 C.p.c."),
            Action("CJP-03", "Quo warranto (usurpation de fonction)",
                   delai="Délai raisonnable; délais spéciaux en matière municipale",
                   ref_delai="529 C.p.c."),
            Action("CJP-04", "Habeas corpus",
                   delai="En tout temps",
                   ref_delai="398 C.p.c."),
            Action("CJP-05", "Nullité / invalidité de règlements ou d'actes de l'administration",
                   delai="Délai raisonnable; cassation municipale : 3 mois (692 CM; 407 LCV); nullité du rôle d'évaluation : 1 an (172 LFM); annulation de vente d'immeuble pour taxes : 1 an (1050 CM)",
                   ref_delai="529 C.p.c.; 692 CM; 407 LCV; 172 LFM; 1050 CM"),
            Action("CJP-06", "Déclaration d'inconstitutionnalité / d'inopérabilité",
                   ref_delai="529, 76-78 C.p.c."),
            Action("CJP-99", "Autre (préciser)"),
        ),
    ),
    "INJ": Domaine(
        "INJ",
        "Injonctions et mesures provisionnelles (objet principal du dossier)",
        "Le délai suit le droit substantiel protégé; l'urgence est la vraie contrainte",
        (
            Action("INJ-01", "Injonction permanente",
                   ref_delai="509 s. C.p.c."),
            Action("INJ-02", "Injonction interlocutoire / provisoire (10 jours)",
                   ref_delai="510-511 C.p.c."),
            Action("INJ-03", "Ordonnances Anton Piller, Mareva, Norwich",
                   ref_delai="Jurisprudence"),
            Action("INJ-04", "Saisie avant jugement",
                   ref_delai="516 s. C.p.c."),
            Action("INJ-05", "Séquestre judiciaire",
                   ref_delai="523 s. C.p.c."),
            Action("INJ-06", "Ordonnance de sauvegarde",
                   ref_delai="49, 158 C.p.c."),
            Action("INJ-99", "Autre (préciser)"),
        ),
    ),
    "EXE": Domaine(
        "EXE",
        "Exécution et post-jugement",
        "",
        (
            Action("EXE-01", "Exécution forcée (saisies)",
                   delai="Le jugement se prescrit par 10 ans; exception : jugement contre le responsable d'un préjudice issu d'une infraction criminelle (Loi P-9.2.1) : imprescriptible — 3 ans du décès du responsable, le cas échéant",
                   ref_delai="Art. 2924, C.c.Q."),
            Action("EXE-02", "Opposition à saisie ou à vente",
                   delai="Délais courts d'exécution",
                   ref_delai="735 s. C.p.c."),
            Action("EXE-03", "Outrage au tribunal",
                   ref_delai="57 s. C.p.c."),
            Action("EXE-04", "Pourvoi en rétractation de jugement",
                   delai="Deux étapes de rigueur (D) : signification 30 jours (disparition de l'empêchement / connaissance du jugement, de la preuve ou du fait), puis présentation 30 jours de la signification; plafond : 6 mois du jugement",
                   delai_types=("D",),
                   ref_delai="347 C.p.c."),
            Action("EXE-99", "Autre (préciser)"),
        ),
    ),
    "TRN": Domaine(
        "TRN",
        "Transport et cargaison",
        "Personnelle · principalement contractuelle · condamnation — courts délais hétérogènes et avis préalables : vigilance particulière en réclamations de cargaison et subrogation d'assureurs",
        (
            Action("TRN-01", "Transporteur interne de biens",
                   delai="Avis (A) : 60 jours de la délivrance (bien délivré) ou 9 mois de l'expédition (bien non délivré), sous peine d'irrecevabilité; action : 3 ans (Prescription)",
                   delai_types=("PE", "A"),
                   point_depart="Délivrance ou date à laquelle le bien aurait dû être délivré",
                   ref_delai="Arts. 2050 et 2925, C.c.Q.",
                   prescription_type="3_ans"),
            Action("TRN-02", "Transport maritime de biens",
                   delai="1 an",
                   point_depart="Délivrance ou, en cas de perte totale, date prévue de délivrance",
                   ref_delai="Art. 2079, C.c.Q.",
                   prescription_type="1_an"),
            Action("TRN-03", "Passagers et bagages — maritime",
                   delai="2 ans; plafond absolu de 3 ans (suspension et interruption comprises)",
                   point_depart="Débarquement réel ou prévu; décès : nuances (ann. 2, art. 16)",
                   ref_delai="37 LRMM; ann. 2 (Conv. d'Athènes)",
                   prescription_type="2_ans"),
            Action("TRN-04", "Abordage — cargaison, décès, blessures",
                   delai="2 ans (prorogeable dans certaines circonstances)",
                   point_depart="Perte, décès ou blessures",
                   ref_delai="23 LRMM",
                   prescription_type="2_ans"),
            Action("TRN-05", "Transport aérien",
                   delai="2 ans (D*) — généralement traité en déchéance",
                   delai_types=("D",),
                   point_depart="Arrivée à destination, date prévue d'arrivée ou arrêt du transport",
                   ref_delai="29 LTA, ann. I",
                   prescription_type="2_ans"),
            Action("TRN-06", "Personnes à charge de la victime (maritime)",
                   delai="2 ans",
                   point_depart="Fait générateur (blessures) / décès",
                   ref_delai="6, 14 LRMM",
                   prescription_type="2_ans"),
            Action("TRN-07", "Droit maritime canadien — recours résiduel",
                   delai="3 ans (P)",
                   delai_types=("PE",),
                   point_depart="Fait générateur",
                   ref_delai="140 LRMM",
                   prescription_type="3_ans"),
            Action("TRN-99", "Autre (préciser)"),
        ),
    ),
    "ADM": Domaine(
        "ADM",
        "Recours administratifs et statutaires",
        "Légal-statutaire · contestation, révision ou réclamation — délais courts, souvent de rigueur mais fréquemment relevables selon la loi applicable : vérifier chaque régime",
        (
            Action("ADM-01", "TAQ — recours principal",
                   delai="30 jours (affaires sociales : 60 jours)",
                   point_depart="Notification de la décision ou faits d'ouverture; aucun délai si l'administration a fait défaut de statuer en révision",
                   ref_delai="Art. 110, Loi sur la justice administrative"),
            Action("ADM-02", "TAQ — révision ou révocation",
                   delai="Délai raisonnable",
                   point_depart="Décision visée ou fait nouveau",
                   ref_delai="Art. 155, Loi sur la justice administrative"),
            Action("ADM-03", "Fiscal (Québec) — opposition",
                   delai="90 jours",
                   point_depart="Envoi de l'avis de cotisation",
                   ref_delai="Art. 93.1.1, Loi sur l'administration fiscale",
                   prescription_type="90_jours"),
            Action("ADM-04", "Fiscal (Québec) — contestation (Cour du Québec)",
                   delai="Ouverture : après ratification/nouvelle cotisation, ou expiration de 90/180 jours sans décision; échéance : 90 jours de l'envoi de la décision sur opposition (prorogeable ≤ 1 an : impossibilité d'agir)",
                   point_depart="Décision du ministre",
                   ref_delai="Arts. 93.1.10 et 93.1.13, Loi sur l'administration fiscale",
                   prescription_type="90_jours"),
            Action("ADM-05", "Fiscal (fédéral) — opposition",
                   delai="90 jours; particuliers et successions à taux progressifs : au plus tard le dernier de (i) 1 an de l'échéance de production et (ii) 90 jours de l'envoi de la cotisation",
                   point_depart="Envoi de l'avis de cotisation",
                   ref_delai="165 LIR"),
            Action("ADM-06", "Fiscal (fédéral) — appel (Cour canadienne de l'impôt)",
                   delai="Ouverture : après ratification ou 90 jours sans réponse; échéance : 90 jours de l'avis de ratification ou de nouvelle cotisation",
                   ref_delai="169 LIR",
                   prescription_type="90_jours"),
            Action("ADM-07", "Fiscalité municipale — rôle d'évaluation",
                   delai="Révision : avant le 1er mai suivant l'entrée en vigueur du rôle; recours au TAQ : avant le 31e jour (138.5); cassation : 1er mai / 61e jour de l'avis; nullité du rôle : 1 an",
                   point_depart="Force majeure : 60 jours de la fin de la situation; voir aussi CJP-05",
                   ref_delai="124-138.5, 171-172 LFM"),
            Action("ADM-08", "Accès à l'information et renseignements personnels (CAI)",
                   delai="Révision (public) / examen de mésentente (privé) : 30 jours (secteur privé : relevable pour motif raisonnable)",
                   point_depart="Décision, refus ou expiration du délai de réponse",
                   ref_delai="Art. 135, LAI; art. 43, LPRP",
                   prescription_type="30_jours"),
            Action("ADM-09", "SAAQ — indemnisation (automobile)",
                   delai="Demande d'indemnité : 3 ans (relevable : motifs sérieux et légitimes); révision : 60 jours; contestation au TAQ : 60 jours",
                   point_depart="Accident, manifestation du préjudice ou décès; victime non-résidente : 180 jours (art. 9)",
                   ref_delai="Arts. 9, 11, 83.45, et 83.49, Loi sur l'assurance automobile"),
            Action("ADM-10", "IVAC — demande de qualification",
                   delai="3 ans (présomption de renonciation réfragable : motif raisonnable); violences (enfance, sexuelle, conjugale) : en tout temps; infractions antérieures au 13 octobre 2021 : 2 ans",
                   point_depart="Connaissance du préjudice ou décès de la victime",
                   ref_delai="25 Loi P-9.2.1"),
            Action("ADM-11", "Aide juridique — révisions",
                   delai="Refus, retrait, remboursement : 30 jours; admissibilité financière (comité de révision) : 15 jours",
                   point_depart="Décision du directeur général",
                   ref_delai="74-75 LAJ"),
            Action("ADM-99", "Autre (préciser)"),
        ),
    ),
    "TRV": Domaine(
        "TRV",
        "Travail et emploi",
        "Légal-statutaire (contractuel pour le recours civil) — délais très courts : pièges fréquents au moment de l'ouverture du mandat",
        (
            Action("TRV-01", "Congédiement sans cause juste et suffisante",
                   delai="45 jours (D*)",
                   delai_types=("D",),
                   point_depart="Congédiement",
                   ref_delai="124 LNT",
                   prescription_type="45_jours"),
            Action("TRV-02", "Pratiques interdites",
                   delai="45 jours; congédiement, suspension ou mise à la retraite pour le motif de l'art. 122.1 : 90 jours",
                   point_depart="Pratique reprochée",
                   ref_delai="123, 123.1 LNT"),
            Action("TRV-03", "Harcèlement psychologique",
                   delai="2 ans; renvoi au TAT sur refus de la CNESST : 30 jours",
                   point_depart="Dernière manifestation de la conduite",
                   ref_delai="123.7, 123.9 LNT",
                   prescription_type="2_ans"),
            Action("TRV-04", "Réclamation civile sous la LNT",
                   delai="1 an",
                   point_depart="Chaque échéance",
                   ref_delai="115 LNT",
                   prescription_type="1_an"),
            Action("TRV-05", "Code du travail — plaintes et rapports collectifs",
                   delai="Plaintes (art. 12-15) : 30 jours; devoir de juste représentation : 6 mois; droits issus d'une convention collective : 6 mois",
                   point_depart="Connaissance, sanction ou naissance de la cause d'action",
                   ref_delai="Arts. 14.0.1, 16, 47.5, et 71, Code du travail"),
            Action("TRV-06", "LATMP — volet travailleur",
                   delai="Plainte (art. 32) : 30 jours; réclamation : 6 mois (violence à caractère sexuel : 2 ans); révision : 30 jours; contestation au TAT : 60 jours",
                   point_depart="Lésion, décès ou connaissance",
                   ref_delai="32, 253, 270-272, 358-359.1 LATMP"),
            Action("TRV-07", "LATMP — volet employeur (imputation)",
                   delai="Transfert de coûts : 1 an de l'accident; partage (travailleur déjà handicapé) : avant l'expiration de la 3e année suivant l'année de la lésion",
                   ref_delai="326, 329 LATMP"),
            Action("TRV-08", "Code canadien du travail (entreprises fédérales)",
                   delai="Plaintes au CCRI et congédiement injustifié : 90 jours",
                   point_depart="Connaissance des circonstances / congédiement",
                   ref_delai="97, 133, 240 CCT",
                   prescription_type="90_jours"),
            Action("TRV-99", "Autre (préciser)"),
        ),
    ),
    "APP": Domaine(
        "APP",
        "Appels et pourvois",
        "Mandats post-jugement — délais de rigueur emportant généralement déchéance; à ouvrir comme dossiers distincts dès la réception du jugement. La rétractation demeure à EXE-04.",
        (
            Action("APP-01", "Appel civil — Cour d'appel du Québec",
                   delai="30 jours (déclaration d'appel ± permission); appel incident : 10 jours; jugements visés à l'art. 361 : 10 jours (fin d'injonction interlocutoire, libération refusée, saisie avant jugement) ou 5 jours (intégrité de la personne, garde/évaluation psychiatrique)",
                   delai_types=("D",),
                   point_depart="Avis du jugement ou jugement rendu à l'audience; rigueur et déchéance — la C.A. peut relever la partie (≤ 6 mois du jugement, chances raisonnables + impossibilité d'agir)",
                   ref_delai="360-363 C.p.c."),
            Action("APP-02", "Cour suprême du Canada",
                   delai="Autorisation d'appel : 60 jours; avis d'appel : 30 jours",
                   delai_types=("D",),
                   point_depart="Jugement porté en appel / jugement accordant l'autorisation",
                   ref_delai="58 Loi sur la Cour suprême"),
            Action("APP-03", "Cours fédérales",
                   delai="Contrôle judiciaire (C.F.) : 30 jours (prorogeable); appel à la C.A.F. : 10 jours (interlocutoire) / 30 jours (final — juillet et août exclus du calcul)",
                   delai_types=("D",),
                   point_depart="Première communication de la décision / prononcé du jugement",
                   ref_delai="18.1, 27(2), 28 LCF"),
            Action("APP-04", "Appels statutaires — Cour du Québec",
                   delai="TAL : 30 jours (permission, de la connaissance); CAI : interlocutoire 10 jours, final 30 jours + signification 10 jours du dépôt; TAQ (affaires immobilières, territoire agricole) : 30 jours (permission)",
                   point_depart="Décision, notification ou connaissance selon le régime",
                   ref_delai="Art. 92, LTAL; arts. 147.1, 149, 151, LAI; arts. 61.1, 63, 65, LPRP; art. 160, LJA"),
            Action("APP-05", "Faillite — appels",
                   delai="10 jours (décision du registraire; décision du tribunal → cour d'appel), ou autre délai fixé par le juge",
                   point_depart="Ordonnance ou décision",
                   ref_delai="30(2), 31(1) Règles générales sur la faillite et l'insolvabilité"),
            Action("APP-06", "Divorce et ordonnances accessoires",
                   delai="30 jours (prorogeable pour motifs particuliers, même après expiration)",
                   point_depart="Prononcé du jugement ou de l'ordonnance",
                   ref_delai="12(1), 21 Loi sur le divorce",
                   prescription_type="30_jours"),
            Action("APP-07", "Pénal / réglementaire (C.p.p.)",
                   delai="Appel à la Cour supérieure : 30 jours; permission d'appeler à la C.A. : 30 jours; rétractation (jugement par défaut) : 15 jours de la connaissance",
                   point_depart="Jugement / connaissance",
                   ref_delai="252, 271, 296 C.p.p."),
            Action("APP-99", "Autre (préciser)"),
        ),
    ),
}


# ── Derived indexes ─────────────────────────────────────────────────────

# Flat code → Action, across every domaine. Codes are globally unique.
ACTIONS: dict[str, Action] = {
    action.code: action
    for domaine in DOMAINES.values()
    for action in domaine.actions
}

# "" is the unset state: a dossier need not be classified.
VALID_DOMAINES: tuple[str, ...] = ("",) + tuple(DOMAINES)
VALID_ACTIONS: tuple[str, ...] = ("",) + tuple(ACTIONS)

# key → label for the form select and the detail card (includes the empty state).
DOMAINE_LABELS: dict[str, str] = {
    "": "Non défini",
    **{code: d.libelle for code, d in DOMAINES.items()},
}

# The closed 11-token delai_types vocabulary (spec § 4) — one label per token.
# A tuple may carry several tokens; ``delai_types_label`` renders combinations.
DELAI_TYPE_LABELS: dict[str, str] = {
    "PE": "Prescription extinctive",
    "PA": "Prescription acquisitive",
    "D": "Déchéance stricte",
    "DR": "Déchéance relevable",
    "A": "Avis préalable",
    "R": "Délai raisonnable",
    "N": "Aucun délai / en tout temps",
    "I": "Imprescriptible",
    "S": "Suit le droit sous-jacent ou exercé",
    "V": "Variable / à qualifier",
    "F": "Fenêtre rétrospective",
}

VALID_DELAI_TYPES: frozenset[str] = frozenset(DELAI_TYPE_LABELS)


def delai_types_label(action_code: str) -> str:
    """Render an action's delai_types as one French label.

    « Prescription extinctive + Avis préalable », with the suffix
    « (qualification à valider) » when the action carries ``a_valider``.
    "" for an unknown/unset code or an untyped (``-99``) row.
    """
    action = ACTIONS.get(action_code or "")
    if not action or not action.delai_types:
        return ""
    label = " + ".join(DELAI_TYPE_LABELS[t] for t in action.delai_types)
    return f"{label} (qualification à valider)" if action.a_valider else label


def niveau_decheance(action_code: str) -> Optional[str]:
    """« stricte » (D), « relevable » (DR), or None — the déchéance level.

    A déchéance stricte is a délai de rigueur (art. 2878 al. 2 C.c.Q.): in
    principle it neither suspends nor interrupts. A déchéance relevable keeps
    a statutory relief mechanism. « D » outranks « DR » when both appear.
    """
    action = ACTIONS.get(action_code or "")
    if not action:
        return None
    if "D" in action.delai_types:
        return "stricte"
    if "DR" in action.delai_types:
        return "relevable"
    return None


def is_decheance(action_code: str) -> bool:
    """Deprecated alias — use :func:`niveau_decheance`."""
    return niveau_decheance(action_code) is not None


def avis_delai_display(delai_key: Optional[str]) -> str:
    """"3_jours_ouvrables" → "3 jours ouvrables"; None → "".

    Purely typographic (the keys are self-describing) so this module keeps
    importing nothing beyond typing/functools — the real period lives in
    ``utils.recours.AVIS_PERIODS``.
    """
    return (delai_key or "").replace("_", " ")


def get_domaine(code: str) -> Optional[Domaine]:
    """Look up a domaine by code."""
    return DOMAINES.get(code or "")


def get_action(code: str) -> Optional[Action]:
    """Look up an action by its code, across every domaine."""
    return ACTIONS.get(code or "")


def actions_for(domaine_code: str) -> tuple[Action, ...]:
    """Return a domaine's actions in source order, or () for an unknown code."""
    domaine = DOMAINES.get(domaine_code or "")
    return domaine.actions if domaine else ()


def domaine_of(action_code: str) -> str:
    """Return the domaine code an action belongs to, or "".

    Derived from the code prefix rather than a reverse index — the prefix IS
    the relationship, and ``_validate`` relies on that to reject an
    action/domaine pair that disagrees.
    """
    action = ACTIONS.get(action_code or "")
    return action.code.split("-", 1)[0] if action else ""


def action_label(action_code: str) -> str:
    """Render an action as « Libellé [CODE] », or "" for an unknown code.

    The bracketed code is what makes two similarly-worded recourses
    distinguishable at a glance (CON-04 nullité vs. SUC-02 contestation), and
    it is what the user cites.
    """
    action = ACTIONS.get(action_code or "")
    return f"{action.libelle} [{action.code}]" if action else ""


def action_choices(domaine_code: str) -> list[tuple[str, str]]:
    """Return [(code, « Libellé [CODE] »)] for a domaine's select options."""
    return [(a.code, f"{a.libelle} [{a.code}]") for a in actions_for(domaine_code)]


def requires_precision(action_code: str) -> bool:
    """True for the « Autre (préciser) » rows, which carry no delay of their own.

    Those rows exist so a file that fits no named recourse is still classified
    by domaine; the précision field is where the actual object is recorded.
    """
    return bool(action_code) and action_code.endswith("-99")


@functools.lru_cache(maxsize=1)
def form_payload() -> dict:
    """The whole table as JSON-ready data, for the form's cascading picker.

    Embedded as a non-executable ``<script type="application/json">`` block
    (the pattern base.html already uses for the App Check config) rather than
    fetched: it keeps the cascade working with no round trip, no CSRF, and no
    App Check gap on a raw ``fetch``.

    Cached: the table is static, this builds ~43 KB of nested dicts, and
    ``routes.dossiers._template_context`` runs on every dossier list and tab
    render, not just the form. Treat the result as READ-ONLY — every caller
    shares it. It is only ever fed to Jinja's ``|tojson``.
    """
    return {
        code: {
            "libelle": d.libelle,
            "note": d.note,
            "actions": [
                {
                    "code": a.code,
                    "label": f"{a.libelle} [{a.code}]",
                    "delai": a.delai,
                    "delai_types": list(a.delai_types),
                    "delai_types_label": delai_types_label(a.code),
                    "a_valider": a.a_valider,
                    "niveau_decheance": niveau_decheance(a.code),
                    "point_depart": a.point_depart,
                    "ref_delai": a.ref_delai,
                    "ref_fondement": a.ref_fondement,
                    "avis": [
                        {
                            "libelle": v.libelle,
                            "delai": avis_delai_display(v.delai_key),
                            "delai_key": v.delai_key,
                            "point_depart": v.point_depart,
                            "reference": v.reference,
                            "sanction": v.sanction,
                            "conditionnel": v.conditionnel,
                        }
                        for v in a.avis
                    ],
                    "prescription_type": a.prescription_type,
                }
                for a in d.actions
            ],
        }
        for code, d in DOMAINES.items()
    }
