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
* many delays here are NOT prescription at all. ``delai_type`` records which:
  ``P`` prescription (interruptible/suspendable), ``D`` déchéance (a délai de
  rigueur that in principle is neither), ``A`` avis préalable. ``P+A`` means
  both apply; ``D/A`` means the source itself leaves the qualification open.
  Several "D" delays remain *relevables* under their own statute — the source
  flags these with an asterisk and § 4 reserves their qualification.

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


class Action(NamedTuple):
    """One named recourse under a domaine."""

    code: str              # "REC-01" — stable key, never reused
    libelle: str           # French name, as the source states it
    delai: str             # the delay VERBATIM ("" when the source states none)
    delai_type: str        # "P" | "D" | "A" | "P+A" | "D/A" | "A/D" | ""
    point_depart: str      # starting point + traps ("" when none stated)
    references: str        # statutory references (C.c.Q. unless noted)
    prescription_type: str  # SUGGESTED utils.recours period key, or ""


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
        "Personnelle · contractuelle · condamnation — prescription 3 ans (art. 2925) sauf indication. Voir l'étiquette transversale « Recouvrement simplifié (535.1 s. C.p.c.) ».",
        (
            Action("REC-01", "Action sur compte (biens vendus, services rendus)",
                   "3 ans (P)",
                   "P", "Exigibilité de chaque facture",
                   "2925, 2931", "3_ans"),
            Action("REC-02", "Prêt, reconnaissance de dette",
                   "3 ans (P)",
                   "P", "Terme; prêt à demande : nuances jurisprudentielles",
                   "2925, 2880", "3_ans"),
            Action("REC-03", "Effets de commerce (chèque, billet)",
                   "3 ans (P)",
                   "P", "Exigibilité de l'effet",
                   "2925", "3_ans"),
            Action("REC-04", "Cautionnement",
                   "3 ans (P)",
                   "P", "Défaut du débiteur principal; caractère accessoire",
                   "2333 s.", "3_ans"),
            Action("REC-05", "Loyers et charges (bail commercial)",
                   "3 ans (P)",
                   "P", "Chaque échéance",
                   "2931", "3_ans"),
            Action("REC-06", "Honoraires professionnels",
                   "3 ans (P)",
                   "P", "Exigibilité / fin du mandat",
                   "2925", "3_ans"),
            Action("REC-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "CON": Domaine(
        "CON",
        "Contrats : exécution, anéantissement, garanties",
        "Personnelle (mixte pour CON-06) · contractuelle · condamnation ou constitutif",
        (
            Action("CON-01", "Exécution en nature",
                   "3 ans (P)",
                   "P", "Défaut",
                   "1601", "3_ans"),
            Action("CON-02", "Dommages-intérêts contractuels",
                   "3 ans (P)",
                   "P", "Manifestation du préjudice",
                   "1458, 2926", "3_ans"),
            Action("CON-03", "Résolution / résiliation",
                   "3 ans (P)",
                   "P", "Défaut",
                   "1604 s.", "3_ans"),
            Action("CON-04", "Nullité (vices de consentement, capacité)",
                   "3 ans (P)",
                   "P", "Connaissance de la cause; crainte : sa cessation. Imprescriptible par exception (moyen de défense)",
                   "2927, 2882", "3_ans"),
            Action("CON-05", "Réduction de l'obligation / du prix",
                   "3 ans (P)",
                   "P", "Idem",
                   "1604 al. 3", "3_ans"),
            Action("CON-06", "Passation de titre (action mixte)",
                   "3 ans (P)",
                   "P", "Refus de signer",
                   "1712", "3_ans"),
            Action("CON-07", "Vices cachés (garantie de qualité)",
                   "3 ans (P) + A",
                   "P+A", "Découverte; dénonciation écrite dans un délai raisonnable; voir aussi la L.p.c.",
                   "1726, 1739", "3_ans"),
            Action("CON-08", "Bail commercial (éviction, résiliation, renouvellement)",
                   "3 ans (P)",
                   "P", "Selon le droit invoqué",
                   "1851 s.", "3_ans"),
            Action("CON-09", "Assurance — réclamation d'indemnité",
                   "3 ans (P)",
                   "P", "Naissance du droit; avis de sinistre : délais de police",
                   "2425 s.", "3_ans"),
            Action("CON-10", "Action directe contre l'assureur du responsable",
                   "3 ans (P)",
                   "P", "Suit le recours contre l'assuré",
                   "2501", "3_ans"),
            Action("CON-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "RCV": Domaine(
        "RCV",
        "Responsabilité civile extracontractuelle",
        "Personnelle · extracontractuelle · condamnation. Régimes particuliers : accident d'automobile → ADM-09 (SAAQ); transport → TRN.",
        (
            Action("RCV-01", "Préjudice corporel",
                   "3 ans (P)",
                   "P", "1re manifestation; avis et courts délais inopposables",
                   "2926, 2930", "3_ans"),
            Action("RCV-02", "Préjudice corporel — acte criminel / violences",
                   "10 ans; imprescriptible (violence sexuelle, violence subie pendant l'enfance, violence conjugale); décès de la victime ou de l'auteur : 3 ans du décès",
                   "", "Manifestation; aide financière étatique : voir ADM-10 (IVAC)",
                   "2926.1", ""),
            Action("RCV-03", "Préjudice matériel",
                   "3 ans (P)",
                   "P", "Municipalités — deux régimes : LCV : avis 15 jours (A) + action 6 mois (585); Code municipal : avis 60 jours (A) + action 6 mois (1112.1); fautes ou illégalités : 6 mois (586 LCV)",
                   "2925; 585-586 LCV; 1112.1 CM", "3_ans"),
            Action("RCV-04", "Préjudice moral / psychologique",
                   "3 ans (P)",
                   "P", "Manifestation",
                   "2926", "3_ans"),
            Action("RCV-05", "Diffamation",
                   "1 an (P); média/journal : 3 mois + avis préalable 3 jours ouvrables (A)",
                   "P+A", "Connaissance de l'atteinte; média : publication ou sa connaissance (max 1 an de la publication); la courte prescription suppose le respect des formalités par le journal",
                   "2929; art. 2-3 Loi sur la presse", ""),
            Action("RCV-06", "Vie privée, renseignements personnels",
                   "3 ans (P)",
                   "P", "1 an si l'atteinte est à la réputation",
                   "35-41; 2929", "3_ans"),
            Action("RCV-07", "Responsabilité professionnelle",
                   "3 ans (P)",
                   "P", "Manifestation; médical → souvent RCV-01/02",
                   "2926", "3_ans"),
            Action("RCV-08", "Produits — fabricant / vendeur spécialisé",
                   "3 ans (P)",
                   "P", "Découverte",
                   "1468, 1730", "3_ans"),
            Action("RCV-09", "Fait d'autrui, fait des biens, animaux, ruine du bâtiment",
                   "3 ans (P)",
                   "P", "",
                   "1459-1467", "3_ans"),
            Action("RCV-10", "Troubles de voisinage",
                   "3 ans (P)",
                   "P", "Préjudice continu : renaissance au jour le jour",
                   "976", "3_ans"),
            Action("RCV-11", "Abus de procédure",
                   "3 ans (P)",
                   "P", "Fin de l'instance abusive (généralement)",
                   "51 C.p.c.; 2925", "3_ans"),
            Action("RCV-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "RES": Domaine(
        "RES",
        "Restitutions et quasi-contrats",
        "Personnelle · ni contractuelle ni délictuelle · condamnation — 3 ans (art. 2925)",
        (
            Action("RES-01", "Réception de l'indu",
                   "3 ans (art. 2925)",
                   "", "Départ : paiement / sa découverte",
                   "1491", "3_ans"),
            Action("RES-02", "Enrichissement injustifié",
                   "3 ans (art. 2925)",
                   "", "Caractère subsidiaire; conjoints de fait : fin de la vie commune",
                   "1493", "3_ans"),
            Action("RES-03", "Gestion d'affaires",
                   "3 ans (art. 2925)",
                   "", "",
                   "1482", "3_ans"),
            Action("RES-04", "Restitution des prestations",
                   "3 ans (art. 2925)",
                   "", "Accessoire à l'anéantissement de l'acte",
                   "1699 s.", "3_ans"),
            Action("RES-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "GAG": Domaine(
        "GAG",
        "Protection du gage commun du créancier",
        "",
        (
            Action("GAG-01", "Action oblique",
                   "Délai du droit du débiteur exercé",
                   "", "",
                   "1627", ""),
            Action("GAG-02", "Action en inopposabilité (paulienne)",
                   "1 an (D — déchéance)",
                   "D", "Connaissance du préjudice; syndic de faillite (pour la masse) : nomination du syndic",
                   "1631, 1635", "1_an"),
            Action("GAG-03", "Simulation / contre-lettre",
                   "3 ans (P)",
                   "P", "Connaissance",
                   "1451-1452", "3_ans"),
            Action("GAG-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "IMM": Domaine(
        "IMM",
        "Réel et immobilier",
        "Réelle (sauf indication) · résultat variable",
        (
            Action("IMM-01", "Revendication",
                   "Imprescriptible (propriété)",
                   "", "Limite pratique : prescription acquisitive d'autrui",
                   "953, 2918", "imprescriptible"),
            Action("IMM-02", "Servitudes (confessoire, négatoire, extinction)",
                   "10 ans (P)",
                   "P", "Extinction par non-usage : 10 ans",
                   "2923; 1191", "10_ans"),
            Action("IMM-03", "Bornage",
                   "Imprescriptible",
                   "", "",
                   "978", "imprescriptible"),
            Action("IMM-04", "Action du possesseur troublé",
                   "1 an (D)",
                   "D", "Possession paisible > 1 an requise",
                   "929", "1_an"),
            Action("IMM-05", "Empiètement / accession",
                   "Variable",
                   "", "",
                   "992 s.", ""),
            Action("IMM-06", "Prescription acquisitive (demande en acquisition)",
                   "Possession 10 ans",
                   "", "Jugement requis pour l'immeuble",
                   "2918", ""),
            Action("IMM-07", "Copropriété — annulation de décision d'assemblée",
                   "90 jours (D)",
                   "D", "Date de l'assemblée",
                   "1103", "90_jours"),
            Action("IMM-08", "Fin d'indivision — partage, licitation",
                   "Imprescriptible durant l'indivision",
                   "", "",
                   "1030", "imprescriptible"),
            Action("IMM-09", "Expropriation (contestation) et expropriation déguisée",
                   "Contestation du droit d'exproprier et radiation de l'avis : 30 jours (D*) de la date de l'expropriation; expropriation déguisée : atteinte continue",
                   "D", "Nouvelle Loi concernant l'expropriation (2023); qualification du délai à valider (*voir § 4)",
                   "17 Loi concernant l'expropriation; 952", ""),
            Action("IMM-10", "Radiation d'inscription (registre foncier)",
                   "",
                   "", "",
                   "3057 s.", ""),
            Action("IMM-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "CST": Domaine(
        "CST",
        "Construction",
        "",
        (
            Action("CST-01", "Perte de l'ouvrage (solidité)",
                   "Garantie 5 ans (couverture) + action 3 ans (P)",
                   "P", "Fin des travaux; manifestation",
                   "2118-2119", "3_ans"),
            Action("CST-02", "Malfaçons",
                   "Garantie 1 an de la réception + action 3 ans (P)",
                   "P", "Réception avec/sans réserve",
                   "2120", "3_ans"),
            Action("CST-03", "Réclamations de chantier (extras, retards)",
                   "3 ans (P)",
                   "P", "Avis contractuels souvent stricts (A)",
                   "2098 s.", "3_ans"),
            Action("CST-04", "Hypothèque légale de la construction",
                   "Inscription : 30 jours de la fin des travaux; action/préavis : 6 mois (D)",
                   "D", "Fin des travaux",
                   "2726-2727", "6_mois"),
            Action("CST-05", "Cautionnements de chantier",
                   "Délais de la police (A/D)",
                   "D/A", "Avis à la caution",
                   "", ""),
            Action("CST-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "COR": Domaine(
        "COR",
        "Corporatif et commercial",
        "",
        (
            Action("COR-01", "Oppression / redressement (recours pour abus)",
                   "3 ans (P) (2925)",
                   "P", "Exception : imprescriptible si le recours vise la reconnaissance du droit de propriété sur les actions (position du tableau FARBQ, avril 2026)",
                   "450-453 LSAQ; 241 LCSA; 2925", "3_ans"),
            Action("COR-02", "Action dérivée (pour le compte de la société)",
                   "",
                   "", "Autorisation préalable du tribunal",
                   "445 LSAQ; 239 LCSA", ""),
            Action("COR-03", "Conventions d'actionnaires (rachat, évaluation)",
                   "3 ans (P)",
                   "P", "",
                   "2925", "3_ans"),
            Action("COR-04", "Nullité de résolutions; rectification de registres",
                   "Diligence",
                   "", "",
                   "", ""),
            Action("COR-05", "Liquidation / dissolution judiciaire",
                   "",
                   "", "",
                   "463 s. LSAQ", ""),
            Action("COR-06", "Responsabilité des administrateurs — salaires impayés",
                   "QC : 3 ans (P), mais poursuite préalable de la société dans 1 an de l'exigibilité; Féd. : durant le mandat ou 2 ans de la cessation (D), mais poursuite préalable de la société dans 6 mois de l'échéance",
                   "P+D", "Deux conditions préalables distinctes — piège fréquent",
                   "154 LSAQ + 2925; 119(2)-(3) LCSA", ""),
            Action("COR-07", "Non-concurrence, non-sollicitation, secrets commerciaux",
                   "3 ans (P) + injonction",
                   "P", "",
                   "2088-2089", "3_ans"),
            Action("COR-08", "Concurrence déloyale",
                   "3 ans (P)",
                   "P", "",
                   "1457; 7 LMC", "3_ans"),
            Action("COR-09", "Vente d'entreprise (garanties, ajustements de prix)",
                   "3 ans (P) ou clause de survie (qualification débattue, 2884)",
                   "P", "",
                   "", ""),
            Action("COR-10", "Responsabilité des administrateurs — résolutions illicites (émission d'actions, commissions, dividendes, rachats, indemnités)",
                   "QC : 3 ans de la résolution (2925); Féd. : 2 ans de la résolution",
                   "", "Divergence QC/féd. à signaler visuellement",
                   "155-156 LSAQ + 2925; 118(7) LCSA", ""),
            Action("COR-11", "Dissidence — droit de rachat de l'actionnaire",
                   "Confirmation auprès de la société : 30 jours (D/A*) de la réception de l'avis de rachat",
                   "D/A", "Qualification à valider (*voir § 4)",
                   "380 LSAQ", "30_jours"),
            Action("COR-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "HYP": Domaine(
        "HYP",
        "Sûretés et recours hypothécaires",
        "Réelle (accessoire) · condamnation / délaissement",
        (
            Action("HYP-01", "Préavis d'exercice et délaissement",
                   "",
                   "", "Délais de délaissement 10-60 jours selon le recours",
                   "2757 s.", ""),
            Action("HYP-02", "Prise en paiement",
                   "",
                   "", "Autorisation judiciaire si ≥ 50 % payé",
                   "2778", ""),
            Action("HYP-03", "Vente sous contrôle de justice / par le créancier",
                   "",
                   "", "",
                   "2791; 2784", ""),
            Action("HYP-04", "Prise de possession à des fins d'administration",
                   "",
                   "", "",
                   "2773", ""),
            Action("HYP-05", "Action personnelle sur la créance garantie",
                   "",
                   "P", "3 ans (P); l'hypothèque s'éteint avec la créance",
                   "2797", "3_ans"),
            Action("HYP-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "FAI": Domaine(
        "FAI",
        "Faillite et insolvabilité (fédéral)",
        "Appels en matière de faillite : 10 jours — voir APP-05.",
        (
            Action("FAI-01", "Requête en ordonnance de faillite",
                   "Acte de faillite dans les 6 mois précédents",
                   "", "",
                   "43 LFI", ""),
            Action("FAI-02", "Proposition / avis d'intention",
                   "Délais LFI stricts",
                   "", "",
                   "50, 50.4 LFI", ""),
            Action("FAI-03", "Arrangement LACC",
                   "",
                   "", "",
                   "LACC", ""),
            Action("FAI-04", "Nomination d'un séquestre",
                   "",
                   "", "",
                   "243 LFI", ""),
            Action("FAI-05", "Recours du syndic (préférences, opérations sous-évaluées)",
                   "Périodes suspectes : 3/12 mois; 1 an / 5 ans",
                   "", "",
                   "95-96 LFI", ""),
            Action("FAI-06", "Réclamations, libération, dettes exclues",
                   "",
                   "", "",
                   "121 s., 178 LFI", ""),
            Action("FAI-07", "Libération d'office du failli / opposition à libération",
                   "1re faillite : 9 mois (21 mois si versements art. 68); récidive : 24 mois (36 mois) — l'opposition du créancier doit précéder ces échéances",
                   "", "",
                   "168.1 LFI", ""),
            Action("FAI-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "FAM": Domaine(
        "FAM",
        "Familial",
        "Majoritairement constitutif d'état — imprescriptible sauf indication. Appel en matière de divorce : voir APP-06.",
        (
            Action("FAM-01", "Divorce, séparation de corps, dissolution d'union civile",
                   "Aucun",
                   "", "",
                   "", ""),
            Action("FAM-02", "Autorité parentale, temps parental",
                   "En tout temps (intérêt de l'enfant)",
                   "", "",
                   "", ""),
            Action("FAM-03", "Aliments",
                   "En tout temps; arrérages : 3 ans",
                   "", "",
                   "2931", ""),
            Action("FAM-04", "Patrimoine familial, régimes matrimoniaux",
                   "Accessoire à la demande principale",
                   "", "",
                   "414 s.", ""),
            Action("FAM-05", "Prestation compensatoire (décès)",
                   "1 an du décès (P)",
                   "P", "",
                   "427; 2928", "1_an"),
            Action("FAM-06", "Union parentale (régime en vigueur depuis le 30 juin 2025)",
                   "Régime nouveau — à paramétrer",
                   "", "",
                   "", ""),
            Action("FAM-07", "Filiation",
                   "Imprescriptible entre vifs; 3 ans du décès (de l'enfant ou du parent)",
                   "", "",
                   "530 s.; 542.32", ""),
            Action("FAM-08", "Conjoints de fait — enrichissement injustifié",
                   "3 ans de la fin de la vie commune",
                   "", "",
                   "1493", "3_ans"),
            Action("FAM-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "SUC": Domaine(
        "SUC",
        "Successions et personnes",
        "",
        (
            Action("SUC-01", "Vérification de testament (non contentieux)",
                   "",
                   "", "",
                   "302 s. C.p.c.", ""),
            Action("SUC-02", "Contestation de testament (captation, incapacité)",
                   "3 ans (P), connaissance",
                   "P", "",
                   "2927", "3_ans"),
            Action("SUC-03", "Pétition d'hérédité",
                   "10 ans de l'ouverture",
                   "", "",
                   "626", "10_ans"),
            Action("SUC-04", "Option de l'héritier (délibération)",
                   "6 mois",
                   "", "",
                   "632", "6_mois"),
            Action("SUC-05", "Partage successoral",
                   "Imprescriptible durant l'indivision",
                   "", "",
                   "836 s.", "imprescriptible"),
            Action("SUC-06", "Reddition de compte / destitution du liquidateur",
                   "3 ans (P)",
                   "P", "",
                   "806 s.", "3_ans"),
            Action("SUC-07", "Survie de l'obligation alimentaire",
                   "6 mois du décès (D)",
                   "D", "",
                   "684-685", "6_mois"),
            Action("SUC-08", "Tutelle au majeur, mandat de protection",
                   "Non contentieux",
                   "", "",
                   "268 s.", ""),
            Action("SUC-09", "Jugement déclaratif de décès",
                   "7 ans d'absence",
                   "", "",
                   "92 s.", ""),
            Action("SUC-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "DEC": Domaine(
        "DEC",
        "Déclaratoire, homologation, reconnaissance",
        "Déclaratoire — le délai suit généralement le droit sous-jacent",
        (
            Action("DEC-01", "Jugement déclaratoire",
                   "Suit le droit sous-jacent; le moyen de défense est imprescriptible",
                   "", "",
                   "142 C.p.c.; 2882", ""),
            Action("DEC-02", "Homologation de transaction",
                   "",
                   "", "",
                   "2631 s.", ""),
            Action("DEC-03", "Homologation / annulation de sentence arbitrale",
                   "Annulation : 3 mois (D)",
                   "D", "",
                   "645-648 C.p.c.", "3_mois"),
            Action("DEC-04", "Reconnaissance de décision étrangère",
                   "10 ans (P)",
                   "P", "",
                   "2924; 3155 s.; 507 s. C.p.c.", "10_ans"),
            Action("DEC-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "CJP": Domaine(
        "CJP",
        "Contrôle judiciaire et pourvois (anciens recours extraordinaires)",
        "Légal-statutaire · annulation / ordonnance — « délai raisonnable » (≈ 30 jours en jurisprudence)",
        (
            Action("CJP-01", "Annulation de décision (évocation, certiorari)",
                   "Délai raisonnable",
                   "", "",
                   "529 C.p.c.", ""),
            Action("CJP-02", "Mandamus (accomplissement d'un devoir)",
                   "Délai raisonnable",
                   "", "",
                   "529 C.p.c.", ""),
            Action("CJP-03", "Quo warranto (usurpation de fonction)",
                   "Délai raisonnable; délais spéciaux en matière municipale",
                   "", "",
                   "529 C.p.c.", ""),
            Action("CJP-04", "Habeas corpus",
                   "En tout temps",
                   "", "",
                   "398 C.p.c.", ""),
            Action("CJP-05", "Nullité / invalidité de règlements ou d'actes de l'administration",
                   "Délai raisonnable; cassation municipale : 3 mois (692 CM; 407 LCV); nullité du rôle d'évaluation : 1 an (172 LFM); annulation de vente d'immeuble pour taxes : 1 an (1050 CM)",
                   "", "",
                   "529 C.p.c.; 692 CM; 407 LCV; 172 LFM; 1050 CM", ""),
            Action("CJP-06", "Déclaration d'inconstitutionnalité / d'inopérabilité",
                   "",
                   "", "",
                   "529, 76-78 C.p.c.", ""),
            Action("CJP-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "INJ": Domaine(
        "INJ",
        "Injonctions et mesures provisionnelles (objet principal du dossier)",
        "Le délai suit le droit substantiel protégé; l'urgence est la vraie contrainte",
        (
            Action("INJ-01", "Injonction permanente",
                   "",
                   "", "",
                   "509 s. C.p.c.", ""),
            Action("INJ-02", "Injonction interlocutoire / provisoire (10 jours)",
                   "",
                   "", "",
                   "510-511 C.p.c.", ""),
            Action("INJ-03", "Ordonnances Anton Piller, Mareva, Norwich",
                   "",
                   "", "",
                   "Jurisprudence", ""),
            Action("INJ-04", "Saisie avant jugement",
                   "",
                   "", "",
                   "516 s. C.p.c.", ""),
            Action("INJ-05", "Séquestre judiciaire",
                   "",
                   "", "",
                   "523 s. C.p.c.", ""),
            Action("INJ-06", "Ordonnance de sauvegarde",
                   "",
                   "", "",
                   "49, 158 C.p.c.", ""),
            Action("INJ-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "EXE": Domaine(
        "EXE",
        "Exécution et post-jugement",
        "",
        (
            Action("EXE-01", "Exécution forcée (saisies)",
                   "Le jugement se prescrit par 10 ans; exception : jugement contre le responsable d'un préjudice issu d'une infraction criminelle (Loi P-9.2.1) : imprescriptible — 3 ans du décès du responsable, le cas échéant",
                   "", "",
                   "2924 al. 1 et 2", ""),
            Action("EXE-02", "Opposition à saisie ou à vente",
                   "Délais courts d'exécution",
                   "", "",
                   "735 s. C.p.c.", ""),
            Action("EXE-03", "Outrage au tribunal",
                   "",
                   "", "",
                   "57 s. C.p.c.", ""),
            Action("EXE-04", "Pourvoi en rétractation de jugement",
                   "Deux étapes de rigueur (D) : signification 30 jours (disparition de l'empêchement / connaissance du jugement, de la preuve ou du fait), puis présentation 30 jours de la signification; plafond : 6 mois du jugement",
                   "D", "",
                   "347 C.p.c.", ""),
            Action("EXE-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "TRN": Domaine(
        "TRN",
        "Transport et cargaison",
        "Personnelle · principalement contractuelle · condamnation — courts délais hétérogènes et avis préalables : vigilance particulière en réclamations de cargaison et subrogation d'assureurs",
        (
            Action("TRN-01", "Transporteur interne de biens",
                   "Avis (A) : 60 jours de la délivrance (bien délivré) ou 9 mois de l'expédition (bien non délivré), sous peine d'irrecevabilité; action : 3 ans (P)",
                   "P+A", "Délivrance ou date à laquelle le bien aurait dû être délivré",
                   "2050, 2925", "3_ans"),
            Action("TRN-02", "Transport maritime de biens",
                   "1 an",
                   "", "Délivrance ou, en cas de perte totale, date prévue de délivrance",
                   "2079", "1_an"),
            Action("TRN-03", "Passagers et bagages — maritime",
                   "2 ans; plafond absolu de 3 ans (suspension et interruption comprises)",
                   "", "Débarquement réel ou prévu; décès : nuances (ann. 2, art. 16)",
                   "37 LRMM; ann. 2 (Conv. d'Athènes)", "2_ans"),
            Action("TRN-04", "Abordage — cargaison, décès, blessures",
                   "2 ans (prorogeable dans certaines circonstances)",
                   "", "Perte, décès ou blessures",
                   "23 LRMM", "2_ans"),
            Action("TRN-05", "Transport aérien",
                   "2 ans (D*) — généralement traité en déchéance",
                   "D", "Arrivée à destination, date prévue d'arrivée ou arrêt du transport",
                   "29 LTA, ann. I", "2_ans"),
            Action("TRN-06", "Personnes à charge de la victime (maritime)",
                   "2 ans",
                   "", "Fait générateur (blessures) / décès",
                   "6, 14 LRMM", "2_ans"),
            Action("TRN-07", "Droit maritime canadien — recours résiduel",
                   "3 ans (P)",
                   "P", "Fait générateur",
                   "140 LRMM", "3_ans"),
            Action("TRN-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "ADM": Domaine(
        "ADM",
        "Recours administratifs et statutaires",
        "Légal-statutaire · contestation, révision ou réclamation — délais courts, souvent de rigueur mais fréquemment relevables selon la loi applicable : vérifier chaque régime",
        (
            Action("ADM-01", "TAQ — recours principal",
                   "30 jours (affaires sociales : 60 jours)",
                   "", "Notification de la décision ou faits d'ouverture; aucun délai si l'administration a fait défaut de statuer en révision",
                   "110 LJA", ""),
            Action("ADM-02", "TAQ — révision ou révocation",
                   "Délai raisonnable",
                   "", "Décision visée ou fait nouveau",
                   "155 LJA", ""),
            Action("ADM-03", "Fiscal (Québec) — opposition",
                   "90 jours",
                   "", "Envoi de l'avis de cotisation",
                   "93.1.1 LAF", "90_jours"),
            Action("ADM-04", "Fiscal (Québec) — contestation (Cour du Québec)",
                   "Ouverture : après ratification/nouvelle cotisation, ou expiration de 90/180 jours sans décision; échéance : 90 jours de l'envoi de la décision sur opposition (prorogeable ≤ 1 an : impossibilité d'agir)",
                   "", "Décision du ministre",
                   "93.1.10, 93.1.13 LAF", "90_jours"),
            Action("ADM-05", "Fiscal (fédéral) — opposition",
                   "90 jours; particuliers et successions à taux progressifs : au plus tard le dernier de (i) 1 an de l'échéance de production et (ii) 90 jours de l'envoi de la cotisation",
                   "", "Envoi de l'avis de cotisation",
                   "165 LIR", ""),
            Action("ADM-06", "Fiscal (fédéral) — appel (Cour canadienne de l'impôt)",
                   "Ouverture : après ratification ou 90 jours sans réponse; échéance : 90 jours de l'avis de ratification ou de nouvelle cotisation",
                   "", "",
                   "169 LIR", "90_jours"),
            Action("ADM-07", "Fiscalité municipale — rôle d'évaluation",
                   "Révision : avant le 1er mai suivant l'entrée en vigueur du rôle; recours au TAQ : avant le 31e jour (138.5); cassation : 1er mai / 61e jour de l'avis; nullité du rôle : 1 an",
                   "", "Force majeure : 60 jours de la fin de la situation; voir aussi CJP-05",
                   "124-138.5, 171-172 LFM", ""),
            Action("ADM-08", "Accès à l'information et renseignements personnels (CAI)",
                   "Révision (public) / examen de mésentente (privé) : 30 jours (secteur privé : relevable pour motif raisonnable)",
                   "", "Décision, refus ou expiration du délai de réponse",
                   "135 LAI; 43 LPRP", "30_jours"),
            Action("ADM-09", "SAAQ — indemnisation (automobile)",
                   "Demande d'indemnité : 3 ans (relevable : motifs sérieux et légitimes); révision : 60 jours; contestation au TAQ : 60 jours",
                   "", "Accident, manifestation du préjudice ou décès; victime non-résidente : 180 jours (art. 9)",
                   "9, 11, 83.45, 83.49 LAA", ""),
            Action("ADM-10", "IVAC — demande de qualification",
                   "3 ans (présomption de renonciation réfragable : motif raisonnable); violences (enfance, sexuelle, conjugale) : en tout temps; infractions antérieures au 13 octobre 2021 : 2 ans",
                   "", "Connaissance du préjudice ou décès de la victime",
                   "25 Loi P-9.2.1", ""),
            Action("ADM-11", "Aide juridique — révisions",
                   "Refus, retrait, remboursement : 30 jours; admissibilité financière (comité de révision) : 15 jours",
                   "", "Décision du directeur général",
                   "74-75 LAJ", ""),
            Action("ADM-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "TRV": Domaine(
        "TRV",
        "Travail et emploi",
        "Légal-statutaire (contractuel pour le recours civil) — délais très courts : pièges fréquents au moment de l'ouverture du mandat",
        (
            Action("TRV-01", "Congédiement sans cause juste et suffisante",
                   "45 jours (D*)",
                   "D", "Congédiement",
                   "124 LNT", "45_jours"),
            Action("TRV-02", "Pratiques interdites",
                   "45 jours; congédiement, suspension ou mise à la retraite pour le motif de l'art. 122.1 : 90 jours",
                   "", "Pratique reprochée",
                   "123, 123.1 LNT", ""),
            Action("TRV-03", "Harcèlement psychologique",
                   "2 ans; renvoi au TAT sur refus de la CNESST : 30 jours",
                   "", "Dernière manifestation de la conduite",
                   "123.7, 123.9 LNT", "2_ans"),
            Action("TRV-04", "Réclamation civile sous la LNT",
                   "1 an",
                   "", "Chaque échéance",
                   "115 LNT", "1_an"),
            Action("TRV-05", "Code du travail — plaintes et rapports collectifs",
                   "Plaintes (art. 12-15) : 30 jours; devoir de juste représentation : 6 mois; droits issus d'une convention collective : 6 mois",
                   "", "Connaissance, sanction ou naissance de la cause d'action",
                   "14.0.1, 16, 47.5, 71 CT", ""),
            Action("TRV-06", "LATMP — volet travailleur",
                   "Plainte (art. 32) : 30 jours; réclamation : 6 mois (violence à caractère sexuel : 2 ans); révision : 30 jours; contestation au TAT : 60 jours",
                   "", "Lésion, décès ou connaissance",
                   "32, 253, 270-272, 358-359.1 LATMP", ""),
            Action("TRV-07", "LATMP — volet employeur (imputation)",
                   "Transfert de coûts : 1 an de l'accident; partage (travailleur déjà handicapé) : avant l'expiration de la 3e année suivant l'année de la lésion",
                   "", "",
                   "326, 329 LATMP", ""),
            Action("TRV-08", "Code canadien du travail (entreprises fédérales)",
                   "Plaintes au CCRI et congédiement injustifié : 90 jours",
                   "", "Connaissance des circonstances / congédiement",
                   "97, 133, 240 CCT", "90_jours"),
            Action("TRV-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
        ),
    ),
    "APP": Domaine(
        "APP",
        "Appels et pourvois",
        "Mandats post-jugement — délais de rigueur emportant généralement déchéance; à ouvrir comme dossiers distincts dès la réception du jugement. La rétractation demeure à EXE-04.",
        (
            Action("APP-01", "Appel civil — Cour d'appel du Québec",
                   "30 jours (déclaration d'appel ± permission); appel incident : 10 jours; jugements visés à l'art. 361 : 10 jours (fin d'injonction interlocutoire, libération refusée, saisie avant jugement) ou 5 jours (intégrité de la personne, garde/évaluation psychiatrique)",
                   "D", "Avis du jugement ou jugement rendu à l'audience; rigueur et déchéance — la C.A. peut relever la partie (≤ 6 mois du jugement, chances raisonnables + impossibilité d'agir)",
                   "360-363 C.p.c.", ""),
            Action("APP-02", "Cour suprême du Canada",
                   "Autorisation d'appel : 60 jours; avis d'appel : 30 jours",
                   "", "Jugement porté en appel / jugement accordant l'autorisation",
                   "58 Loi sur la Cour suprême", ""),
            Action("APP-03", "Cours fédérales",
                   "Contrôle judiciaire (C.F.) : 30 jours (prorogeable); appel à la C.A.F. : 10 jours (interlocutoire) / 30 jours (final — juillet et août exclus du calcul)",
                   "", "Première communication de la décision / prononcé du jugement",
                   "18.1, 27(2), 28 LCF", ""),
            Action("APP-04", "Appels statutaires — Cour du Québec",
                   "TAL : 30 jours (permission, de la connaissance); CAI : interlocutoire 10 jours, final 30 jours + signification 10 jours du dépôt; TAQ (affaires immobilières, territoire agricole) : 30 jours (permission)",
                   "", "Décision, notification ou connaissance selon le régime",
                   "92 LTAL; 147.1, 149, 151 LAI; 61.1, 63, 65 LPRP; 160 LJA", ""),
            Action("APP-05", "Faillite — appels",
                   "10 jours (décision du registraire; décision du tribunal → cour d'appel), ou autre délai fixé par le juge",
                   "", "Ordonnance ou décision",
                   "30(2), 31(1) Règles générales sur la faillite et l'insolvabilité", ""),
            Action("APP-06", "Divorce et ordonnances accessoires",
                   "30 jours (prorogeable pour motifs particuliers, même après expiration)",
                   "", "Prononcé du jugement ou de l'ordonnance",
                   "12(1), 21 Loi sur le divorce", "30_jours"),
            Action("APP-07", "Pénal / réglementaire (C.p.p.)",
                   "Appel à la Cour supérieure : 30 jours; permission d'appeler à la C.A. : 30 jours; rétractation (jugement par défaut) : 15 jours de la connaissance",
                   "", "Jugement / connaissance",
                   "252, 271, 296 C.p.p.", ""),
            Action("APP-99", "Autre (préciser)",
                   "",
                   "", "",
                   "", ""),
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

# The closed delai_type vocabulary. "+" = both apply; "/" = the source leaves
# the qualification open. Ordering is canonical (P, then D, then A).
DELAI_TYPE_LABELS: dict[str, str] = {
    "": "",
    "P": "Prescription",
    "D": "Déchéance",
    "A": "Avis préalable",
    "P+A": "Prescription + avis préalable",
    "P+D": "Prescription ou déchéance selon le régime",
    "D/A": "Déchéance ou avis préalable — qualification à valider",
}


def is_decheance(action_code: str) -> bool:
    """True when a déchéance delay is in play — flag it visually.

    A déchéance is a délai de rigueur: in principle it neither suspends nor
    interrupts, so it forgives far less than a prescription. § 4 of the source
    asks that these be made to stand out. Note several remain *relevables*
    under their own statute, so this is a warning, not a verdict.
    """
    action = ACTIONS.get(action_code or "")
    return bool(action) and "D" in action.delai_type.replace("/", "+").split("+")


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
                    "delai_type": a.delai_type,
                    "point_depart": a.point_depart,
                    "references": a.references,
                    "prescription_type": a.prescription_type,
                }
                for a in d.actions
            ],
        }
        for code, d in DOMAINES.items()
    }
