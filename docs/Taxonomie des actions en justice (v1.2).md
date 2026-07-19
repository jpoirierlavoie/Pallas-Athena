# Taxonomie des actions en justice — Droit québécois

> Référentiel de classification des dossiers pour application de gestion de pratique
> Litige civil et commercial — Québec · **Version 1.2 · 16 juillet 2026**

---

## 1. Objet et description

Ce document définit une **taxonomie à deux niveaux** (catégorie → sous-catégorie) des actions en justice en droit québécois, conçue pour servir de liste de choix dans une application de gestion de pratique. Elle poursuit trois objectifs :

1. **Classer les dossiers** de façon uniforme et lisible d'un coup d'œil;
2. **Assister le calcul de la prescription** en attachant à chaque sous-catégorie un délai indicatif, son type juridique et son point de départ;
3. **Permettre le filtrage et les statistiques** de pratique par famille de recours.

La liste ne prétend pas à l'exhaustivité : chaque catégorie se termine par un item « Autre (préciser) ».

### Principe de conception

Depuis le Code de procédure civile de 2016, il n'existe qu'un seul véhicule procédural — la demande introductive d'instance (art. 141 et s. C.p.c.). La « typologie des actions » est donc une classification **doctrinale et substantielle**, non procédurale. Elle croise plusieurs axes (nature du droit exercé, fondement, résultat recherché) qu'il ne faut **pas mélanger dans la hiérarchie de la liste** : la liste déroulante suit un seul axe pratique (domaine → action nommée), et les axes doctrinaux deviennent des **champs dérivés**, remplis automatiquement lorsque l'utilisateur choisit une sous-catégorie.

### La colonne « Fondement du recours » (v1.2)

Chaque sous-catégorie indique désormais le **siège central du droit d'action** — la ou les dispositions qui *créent* le droit invoqué —, à distinguer des dispositions qui en fixent le **délai** (colonne « Réf. délai »). La colonne retient volontairement 1 à 3 sources « pivots » plutôt qu'un relevé exhaustif : elle sert de point d'entrée pour la rédaction des procédures (l'allégation du fondement) et pour la vérification, non de substitut à la recherche. Lorsque le siège du droit est jurisprudentiel ou réparti dans plusieurs régimes, la cellule l'indique.

### Modèle de données suggéré

Chaque sous-catégorie porte les attributs suivants :

| Champ | Valeurs | Utilité |
|---|---|---|
| `code` | ex. `RCV-01` | Clé stable pour la base de données |
| `nature` | personnelle · réelle · mixte | Qualification doctrinale; règles de compétence territoriale |
| `fondement` | contractuel · extracontractuel · légal-statutaire | Régime de responsabilité; non-cumul (art. 1458 al. 2 C.c.Q.) |
| `resultat` | condamnation · déclaratoire · constitutif-extinctif | Nature des conclusions |
| `ref_fondement` *(v1.2)* | articles fondant le droit d'action (siège du droit) | Rédaction des allégations; vérification |
| `delai_duree` | ex. « 3 ans », « 90 jours », « imprescriptible » | Suggestion de calcul |
| `delai_type` | **P** (prescription) · **D** (déchéance) · **A** (avis préalable) | Voir conventions ci-dessous |
| `point_depart` | règle textuelle (ex. « manifestation du préjudice ») | Le point de départ est presque toujours **factuel** |
| `ref_delai` *(v1.2, anciennement `references`)* | articles fixant le délai | Calcul de la prescription; vérification |

### Conventions

- **P — Prescription** : délai interruptible et suspensible (art. 2889 et s. C.c.Q.).
- **D — Déchéance** : délai de rigueur, en principe ni interruptible ni suspensible; à signaler visuellement dans l'application.
- **A — Avis préalable** : dénonciation ou avis requis avant ou en sus du recours (ex. dénonciation d'un vice caché, avis municipal).
- **Nuance (v1.1)** : plusieurs délais statutaires de type « D » demeurent **relevables** sur démonstration prévue par leur loi (impossibilité d'agir, motifs sérieux et légitimes, motif raisonnable — p. ex. art. 11 LAA, art. 25 Loi P-9.2.1, art. 43 LPRP, art. 363 C.p.c.). L'application devrait distinguer « déchéance stricte » et « déchéance relevable », et la qualification retenue doit être validée par l'avocat.
- Les références sont au **Code civil du Québec** sauf indication contraire (lois citées par leur sigle usuel : C.p.c., C.p.p., CT, CM, LCV, LNT, LATMP, LJA, LAF, LAI, LPRP, LFM, LAA, LAJ, LSAQ, LTAL, LFI, LCSA, LIR, LCF, LRMM, LTA, CCT, LMC, LACC, L.p.c., LLC).

### Avertissement d'implémentation

Le délai attaché à chaque sous-catégorie est une **suggestion calculée, jamais une valeur ferme**. Le point de départ dépend des faits (manifestation, connaissance, fin des travaux), et l'interruption ou la suspension de la prescription échappe à tout calcul automatique. Recommandation : l'application propose une date, mais un champ « date de prescription confirmée par l'avocat » demeure obligatoire avant d'alimenter les rappels et l'agenda. La colonne « Fondement du recours » est pareillement indicative : **les numéros d'articles doivent être vérifiés avant toute allégation dans une procédure.**

**Règles à coder dans le moteur d'échéances (v1.1)** — indépendantes de la taxonomie, ces règles du C.p.c. et du C.c.Q. conditionnent la protection effective du recours :

- **Interruption civile** : la demande en justice n'interrompt la prescription que si elle est **signifiée dans les 60 jours** de l'échéance de la prescription (art. 2892 C.c.Q.) — le seul dépôt au greffe ne suffit pas si la signification est tardive.
- **Notification de la DII** : la demande introductive d'instance doit être notifiée aux parties dans les **3 mois** du dépôt au greffe (art. 107 al. 3 C.p.c.).
- **Mise en état** : l'inscription pour instruction et jugement dans les **6 mois** (1 an en matière familiale) est un **délai de rigueur** (art. 173 C.p.c.), prorogeable seulement aux conditions qui y sont prévues.

---

## 2. La taxonomie

### 1. REC — Recouvrement de créances

*Personnelle · contractuelle · condamnation — prescription 3 ans (art. 2925) sauf indication. Fondement transversal : le droit du créancier à l'exécution de l'obligation (art. 1590, 1594). Voir l'étiquette « Recouvrement simplifié (535.1 s. C.p.c.) ».*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| REC-01 | Action sur compte (biens vendus, services rendus) | 1590; vente : 1708, 1734 (paiement du prix); services : 2098, 2106-2108 | 3 ans (P) | Exigibilité de **chaque facture** | 2925, 2931 |
| REC-02 | Prêt, reconnaissance de dette | 2314, 2327 s. (obligations de l'emprunteur); 1590 | 3 ans (P) | Terme; prêt à demande : nuances jurisprudentielles | 2925, 2880 |
| REC-03 | Effets de commerce (chèque, billet) | Loi sur les lettres de change, L.R.C. 1985, ch. B-4 (LLC) | 3 ans (P) | Exigibilité de l'effet | 2925 |
| REC-04 | Cautionnement | 2333, 2346 s. (engagement de la caution) | 3 ans (P) | Défaut du débiteur principal; caractère accessoire | 2925 |
| REC-05 | Loyers et charges (bail commercial) | 1851, 1855 (obligation de payer le loyer); sanctions : 1863 | 3 ans (P) | Chaque échéance | 2931 |
| REC-06 | Honoraires professionnels | 2098, 2106-2108 (contrat de service); mandat : 2130, 2134 | 3 ans (P) | Exigibilité / fin du mandat | 2925 |
| REC-99 | Autre (préciser) | — | — | — | — |

### 2. CON — Contrats : exécution, anéantissement, garanties

*Personnelle (mixte pour CON-06) · contractuelle · condamnation ou constitutif — fondement transversal : force obligatoire du contrat (1434) et droit à l'exécution (1590)*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| CON-01 | Exécution en nature | 1590, 1601 | 3 ans (P) | Défaut | 2925 |
| CON-02 | Dommages-intérêts contractuels | 1458; 1607 s. (évaluation : 1611-1613) | 3 ans (P) | Manifestation du préjudice | 2925, 2926 |
| CON-03 | Résolution / résiliation | 1604-1606 | 3 ans (P) | Défaut | 2925 |
| CON-04 | Nullité (vices de consentement, capacité) | 1398-1408 (erreur, dol, crainte, lésion); 1416-1422 (régime des nullités) | 3 ans (P) | Connaissance de la cause; crainte : sa cessation. Imprescriptible **par exception** (moyen de défense) | 2927, 2882 |
| CON-05 | Réduction de l'obligation / du prix | 1604 al. 3; dol : 1407-1408 | 3 ans (P) | Idem | 2925 |
| CON-06 | Passation de titre (action mixte) | 1710-1712 (promesse de vente) | 3 ans (P) | Refus de signer | 2925 |
| CON-07 | Vices cachés (garantie de qualité) | 1726-1731; dommages : 1728; consommation : 38, 53 L.p.c. | 3 ans (P) **+ A** | Découverte; **dénonciation écrite dans un délai raisonnable** | 1739; 2925 |
| CON-08 | Bail commercial (éviction, résiliation, renouvellement) | 1851, 1854-1863 (obligations et sanctions) | 3 ans (P) | Selon le droit invoqué | 2925 |
| CON-09 | Assurance — réclamation d'indemnité | 2389 (contrat); 2463-2464 (obligation de payer l'indemnité) | 3 ans (P) | Naissance du droit; avis de sinistre : délais de police | 2925 |
| CON-10 | Action directe contre l'assureur du responsable | 2500-2501 | 3 ans (P) | Suit le recours contre l'assuré | 2925 |
| CON-99 | Autre (préciser) | — | — | — | — |

### 3. RCV — Responsabilité civile extracontractuelle

*Personnelle · extracontractuelle · condamnation — fondement transversal : 1457. Régimes particuliers : accident d'automobile → ADM-09 (SAAQ); transport → TRN.*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| RCV-01 | Préjudice corporel | 1457; évaluation : 1607, 1611 s., 1614-1615 | 3 ans (P) | 1re manifestation; **avis et courts délais inopposables** | 2926, 2930 |
| RCV-02 | Préjudice corporel — acte criminel / violences | 1457 | 10 ans; **imprescriptible** (violence sexuelle, violence subie pendant l'enfance, violence conjugale); décès de la victime ou de l'auteur : 3 ans du décès | Manifestation; aide financière étatique : voir ADM-10 (IVAC) | 2926.1 |
| RCV-03 | Préjudice matériel | 1457 | 3 ans (P) | **Municipalités — deux régimes** : LCV : avis 15 jours (A) + action 6 mois (585); Code municipal : avis 60 jours (A) + action 6 mois (1112.1); fautes ou illégalités : 6 mois (586 LCV) | 2925; 585-586 LCV; 1112.1 CM |
| RCV-04 | Préjudice moral / psychologique | 1457; 49 Charte québécoise | 3 ans (P) | Manifestation | 2926 |
| RCV-05 | Diffamation | 1457; 3 et 35 C.c.Q.; 4-5 et 49 Charte québécoise | **1 an** (P); **média/journal : 3 mois** + avis préalable **3 jours ouvrables (A)** | Connaissance de l'atteinte; média : publication ou sa connaissance (max 1 an de la publication); la courte prescription suppose le respect des formalités par le journal | 2929; art. 2-3 Loi sur la presse |
| RCV-06 | Vie privée, renseignements personnels | 3, 35-41 C.c.Q.; 5 Charte québécoise; 1457; recours statutaires : LPRP | 3 ans (P) | 1 an si l'atteinte est à la réputation | 2925; 2929 |
| RCV-07 | Responsabilité professionnelle | 1458 (client) / 1457 (tiers); service : 2100; mandat : 2138 s. | 3 ans (P) | Manifestation; médical → souvent RCV-01/02 | 2926 |
| RCV-08 | Produits — fabricant / vendeur spécialisé | 1468-1469, 1473; chaîne contractuelle : 1730; consommation : 53 L.p.c. | 3 ans (P) | Découverte | 2925, 2926 |
| RCV-09 | Fait d'autrui, fait des biens, animaux, ruine du bâtiment | 1459-1467 (sièges spécifiques : 1459-1460, 1463, 1465-1467) | 3 ans (P) | — | 2925 |
| RCV-10 | Troubles de voisinage | 976 (sans faute); 1457 (avec faute) | 3 ans (P) | Préjudice continu : renaissance au jour le jour | 2925 |
| RCV-11 | Abus de procédure | 51-54 C.p.c.; 6-7 et 1457 C.c.Q. | 3 ans (P) | Fin de l'instance abusive (généralement) | 2925 |
| RCV-99 | Autre (préciser) | — | — | — | — |

### 4. RES — Restitutions et quasi-contrats

*Personnelle · ni contractuelle ni délictuelle · condamnation — 3 ans (art. 2925)*

| Code | Sous-catégorie | Fondement du recours | Particularités | Réf. délai |
|---|---|---|---|---|
| RES-01 | Réception de l'indu | 1491-1492 | Départ : paiement / sa découverte | 2925 |
| RES-02 | Enrichissement injustifié | 1493-1496 | Caractère subsidiaire; conjoints de fait : fin de la vie commune | 2925 |
| RES-03 | Gestion d'affaires | 1482-1490 | — | 2925 |
| RES-04 | Restitution des prestations | 1699-1707 | Accessoire à l'anéantissement de l'acte | Suit le recours principal |
| RES-99 | Autre (préciser) | — | — | — |

### 5. GAG — Protection du gage commun du créancier

*Fondement transversal : le patrimoine du débiteur, gage commun des créanciers (2644-2646)*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ | Réf. délai |
|---|---|---|---|---|---|
| GAG-01 | Action oblique | 1627-1630 | Délai du droit du débiteur exercé | — | Suit le droit exercé |
| GAG-02 | Action en inopposabilité (paulienne) | 1631-1634 | **1 an (D — déchéance)** | Connaissance du préjudice; **syndic de faillite (pour la masse) : nomination du syndic** | 1635 |
| GAG-03 | Simulation / contre-lettre | 1451-1452 | 3 ans (P) | Connaissance | 2925 |
| GAG-99 | Autre (préciser) | — | — | — | — |

### 6. IMM — Réel et immobilier

*Réelle (sauf indication) · résultat variable*

| Code | Sous-catégorie | Fondement du recours | Délai | Particularités | Réf. délai |
|---|---|---|---|---|---|
| IMM-01 | Revendication | 912, 953 | Imprescriptible (propriété) | Limite pratique : prescription acquisitive d'autrui | 2918 |
| IMM-02 | Servitudes (confessoire, négatoire, extinction) | 1177 s.; extinction : 1191 | 10 ans (P) | Extinction par non-usage : 10 ans | 2923; 1191 |
| IMM-03 | Bornage | 978; 466 s. C.p.c. | Imprescriptible | — | — |
| IMM-04 | Action du possesseur troublé | 912, 928-929 | **1 an (D)** | Possession paisible > 1 an requise | 929 |
| IMM-05 | Empiètement / accession | 954-964 (accession); 992 (empiètement) | Variable | — | — |
| IMM-06 | Prescription acquisitive (demande en acquisition) | 2910-2920 | Possession 10 ans | Jugement requis pour l'immeuble (2918) | 2918 |
| IMM-07 | Copropriété — annulation de décision d'assemblée | 1103 | **90 jours (D)** | Date de l'assemblée | 1103 |
| IMM-08 | Fin d'indivision — partage, licitation | 1030, 1032; succession : 836 s. | Imprescriptible durant l'indivision | — | 1030 |
| IMM-09 | Expropriation (contestation) et expropriation déguisée | 952; 17 Loi concernant l'expropriation | **Contestation du droit d'exproprier et radiation de l'avis : 30 jours (D*)** de la date de l'expropriation; expropriation déguisée : atteinte continue | Nouvelle Loi concernant l'expropriation (2023); qualification du délai à valider (*voir § 4) | 17 Loi concernant l'expropriation |
| IMM-10 | Radiation d'inscription (registre foncier) | 3057, 3063 | — | — | — |
| IMM-99 | Autre (préciser) | — | — | — | — |

### 7. CST — Construction

| Code | Sous-catégorie | Fondement du recours | Délai | Particularités | Réf. délai |
|---|---|---|---|---|---|
| CST-01 | Perte de l'ouvrage (solidité) | 2118 (responsabilité légale; exonérations : 2119) | Garantie **5 ans** (couverture) + action 3 ans (P) | Fin des travaux; manifestation | 2118; 2925-2926 |
| CST-02 | Malfaçons | 2120 | Garantie **1 an** de la réception + action 3 ans (P) | Réception avec/sans réserve | 2120; 2925 |
| CST-03 | Réclamations de chantier (extras, retards) | 2098, 2106-2109 | 3 ans (P) | Avis contractuels souvent stricts (A) | 2925 |
| CST-04 | Hypothèque légale de la construction | 2724(2°), 2726-2728; exercice : 2748 s. | Inscription : **30 jours** de la fin des travaux; action/préavis : **6 mois (D)** | Fin des travaux | 2727 |
| CST-05 | Cautionnements de chantier | 2333 s.; termes de la police (stipulation pour autrui : 1444 s.) | Délais de la police (A/D) | Avis à la caution | Selon la police |
| CST-99 | Autre (préciser) | — | — | — | — |

### 8. COR — Corporatif et commercial

| Code | Sous-catégorie | Fondement du recours | Délai | Particularités | Réf. délai |
|---|---|---|---|---|---|
| COR-01 | Oppression / redressement (recours pour abus) | 450-453 LSAQ; 241 LCSA | **3 ans (P) (2925)** | **Exception : imprescriptible** si le recours vise la reconnaissance du droit de propriété sur les actions (position du tableau FARBQ, avril 2026) | 2925 |
| COR-02 | Action dérivée (pour le compte de la société) | 445-449 LSAQ; 239-240 LCSA | — | Autorisation préalable du tribunal | Suit le droit exercé |
| COR-03 | Conventions d'actionnaires (rachat, évaluation) | 1434; convention unanime : 213-214 LSAQ; 146 LCSA | 3 ans (P) | — | 2925 |
| COR-04 | Nullité de résolutions; rectification de registres | 450 s. LSAQ (redressement); rectification : 455 s. LSAQ*; 243 LCSA | Diligence | — | — |
| COR-05 | Liquidation / dissolution judiciaire | 463 s. LSAQ; 214 LCSA | — | — | — |
| COR-06 | Responsabilité des administrateurs — salaires impayés | 154 LSAQ; 119 LCSA | **QC : 3 ans (P)**, mais poursuite préalable de la société dans **1 an** de l'exigibilité; **Féd. : durant le mandat ou 2 ans de la cessation (D)**, mais poursuite préalable de la société dans **6 mois** de l'échéance | Deux conditions préalables distinctes — piège fréquent | 154 LSAQ + 2925; 119(2)-(3) LCSA |
| COR-07 | Non-concurrence, non-sollicitation, secrets commerciaux | 2088-2089, 2095 (emploi); 1434 (clauses); secret commercial : 1612 | 3 ans (P) + injonction | — | 2925 |
| COR-08 | Concurrence déloyale | 1457; 7 LMC | 3 ans (P) | — | 2925 |
| COR-09 | Vente d'entreprise (garanties, ajustements de prix) | 1434; garanties du vendeur : 1716-1733 | 3 ans (P) ou clause de survie (qualification débattue, 2884) | — | 2925 |
| COR-10 | Responsabilité des administrateurs — résolutions illicites (émission d'actions, commissions, dividendes, rachats, indemnités) | 155-156 LSAQ; 118 LCSA | **QC : 3 ans** de la résolution (2925); **Féd. : 2 ans** de la résolution | Divergence QC/féd. à signaler visuellement | 2925; 118(7) LCSA |
| COR-11 | Dissidence — droit de rachat de l'actionnaire | 372 s. LSAQ; 190 LCSA | Confirmation auprès de la société : **30 jours (D/A*)** de la réception de l'avis de rachat | Qualification à valider (*voir § 4) | 380 LSAQ |
| COR-99 | Autre (préciser) | — | — | — | — |

### 9. HYP — Sûretés et recours hypothécaires

*Réelle (accessoire) · condamnation / délaissement*

| Code | Sous-catégorie | Fondement du recours | Particularités | Réf. délai |
|---|---|---|---|---|
| HYP-01 | Préavis d'exercice et délaissement | 2748, 2757 s.; délaissement : 2763 s. | Délais de délaissement 10-60 jours selon le recours | 2758 |
| HYP-02 | Prise en paiement | 2778-2783 | Autorisation judiciaire si ≥ 50 % payé | — |
| HYP-03 | Vente sous contrôle de justice / par le créancier | 2784-2790 (par le créancier); 2791-2794 (contrôle de justice) | — | — |
| HYP-04 | Prise de possession à des fins d'administration | 2773-2777 | — | — |
| HYP-05 | Action personnelle sur la créance garantie | 1590 + titre de créance (ex. prêt : 2314) | 3 ans (P); l'hypothèque s'éteint avec la créance (2797) | 2925 |
| HYP-99 | Autre (préciser) | — | — | — |

### 10. FAI — Faillite et insolvabilité (fédéral)

*Appels en matière de faillite : 10 jours — voir APP-05.*

| Code | Sous-catégorie | Fondement du recours | Délai clé | Réf. délai |
|---|---|---|---|---|
| FAI-01 | Requête en ordonnance de faillite | 42-43 LFI | Acte de faillite dans les **6 mois** précédents | 43(1) LFI |
| FAI-02 | Proposition / avis d'intention | 50, 50.4, 62 LFI | Délais LFI stricts | 50 s. LFI |
| FAI-03 | Arrangement LACC | 4-6, 11 LACC | — | — |
| FAI-04 | Nomination d'un séquestre | 243 LFI | — | — |
| FAI-05 | Recours du syndic (préférences, opérations sous-évaluées) | 95-96 LFI; alternative provinciale : 1631 C.c.Q. | Périodes suspectes : 3/12 mois; 1 an / 5 ans | 95-96 LFI |
| FAI-06 | Réclamations, libération, dettes exclues | 121-124, 135 (réclamations); 178 (dettes exclues) LFI | — | — |
| FAI-07 | Libération d'office du failli / opposition à libération | 168.1, 170, 173 LFI (motifs d'opposition) | 1re faillite : **9 mois** (21 mois si versements art. 68); récidive : **24 mois** (36 mois) — l'opposition du créancier doit précéder ces échéances | 168.1 LFI |
| FAI-99 | Autre (préciser) | — | — | — |

### 11. FAM — Familial

*Majoritairement constitutif d'état — imprescriptible sauf indication. Appel en matière de divorce : voir APP-06.*

| Code | Sous-catégorie | Fondement du recours | Délai / piège | Réf. délai |
|---|---|---|---|---|
| FAM-01 | Divorce, séparation de corps, dissolution d'union civile | 8 Loi sur le divorce; 493 s.; 521.12 s. | Aucun | — |
| FAM-02 | Autorité parentale, temps parental | 597-612; ordonnances parentales : 16 s. Loi sur le divorce | En tout temps (intérêt de l'enfant : 33) | — |
| FAM-03 | Aliments | 585 s.; époux : 15.1-15.2 Loi sur le divorce | En tout temps; **arrérages : 3 ans** | 2931 |
| FAM-04 | Patrimoine familial, régimes matrimoniaux | 414-426 (patrimoine); 431 s. (régimes); 448 s. (société d'acquêts) | Accessoire à la demande principale | — |
| FAM-05 | Prestation compensatoire (décès) | 427-430 | **1 an du décès (P)** | 2928 |
| FAM-06 | Union parentale (régime en vigueur depuis le 30 juin 2025) | 521.20 s.* (patrimoine d'union parentale) | Régime nouveau — à paramétrer | — |
| FAM-07 | Filiation | 523 s.; actions : 532 s.; procréation assistée et GPA : 538 s., 541 s. | **Imprescriptible entre vifs; 3 ans du décès** (de l'enfant ou du parent) | 542.32 |
| FAM-08 | Conjoints de fait — enrichissement injustifié | 1493-1496 | 3 ans de la fin de la vie commune | 2925; 2880 |
| FAM-99 | Autre (préciser) | — | — | — |

### 12. SUC — Successions et personnes

| Code | Sous-catégorie | Fondement du recours | Délai | Réf. délai |
|---|---|---|---|---|
| SUC-01 | Vérification de testament (non contentieux) | 772-773; 302 s. C.p.c. | — | — |
| SUC-02 | Contestation de testament (captation, incapacité) | 703-711 (capacité de tester); vices : 1398 s. (application jurisprudentielle) | 3 ans (P), connaissance | 2927 |
| SUC-03 | Pétition d'hérédité | 626-629 | **10 ans** de l'ouverture | 626 |
| SUC-04 | Option de l'héritier (délibération) | 630-632 | 6 mois | 632 |
| SUC-05 | Partage successoral | 836-848; 1030 | Imprescriptible durant l'indivision | — |
| SUC-06 | Reddition de compte / destitution du liquidateur | 783 s.; remplacement : 791*; comptes : 806, 819-822; administration du bien d'autrui : 1360 s. | 3 ans (P) | 2925 |
| SUC-07 | Survie de l'obligation alimentaire | 684-695 | **6 mois du décès (D)** | 684-685 |
| SUC-08 | Tutelle au majeur, mandat de protection | 268 s. (tutelle); 2166 s. (homologation du mandat) | Non contentieux | — |
| SUC-09 | Jugement déclaratif de décès | 92-96 | 7 ans d'absence | 92 |
| SUC-99 | Autre (préciser) | — | — | — |

### 13. DEC — Déclaratoire, homologation, reconnaissance

*Déclaratoire — le délai suit généralement le droit sous-jacent*

| Code | Sous-catégorie | Fondement du recours | Délai | Réf. délai |
|---|---|---|---|---|
| DEC-01 | Jugement déclaratoire | 142 C.p.c. | Suit le droit sous-jacent; le moyen de défense est imprescriptible | 2882 |
| DEC-02 | Homologation de transaction | 2631-2637; 527 s. C.p.c. | — | — |
| DEC-03 | Homologation / **annulation** de sentence arbitrale | 2638-2643 (convention d'arbitrage); 645-648 C.p.c. | Annulation : **3 mois (D)** | 648 C.p.c. |
| DEC-04 | Reconnaissance de décision étrangère | 3155-3163; 507 s. C.p.c. | 10 ans (P) | 2924 |
| DEC-99 | Autre (préciser) | — | — | — |

### 14. CJP — Contrôle judiciaire et pourvois (anciens recours extraordinaires)

*Légal-statutaire · annulation / ordonnance — « délai raisonnable » (≈ 30 jours en jurisprudence). Fondement transversal : pouvoir général de contrôle de la Cour supérieure (34 C.p.c.).*

| Code | Sous-catégorie | Fondement du recours | Délai | Réf. délai |
|---|---|---|---|---|
| CJP-01 | Annulation de décision (évocation, certiorari) | 34, 529 al. 1 (2°) C.p.c. | Délai raisonnable | 529 al. 3 C.p.c. |
| CJP-02 | Mandamus (accomplissement d'un devoir) | 34, 529 al. 1 (3°) C.p.c. | Délai raisonnable | 529 al. 3 C.p.c. |
| CJP-03 | Quo warranto (usurpation de fonction) | 34, 529 al. 1 (4°) C.p.c. | Délai raisonnable; délais spéciaux en matière municipale | 529 al. 3 C.p.c. |
| CJP-04 | Habeas corpus | 398 C.p.c.; 24 Charte canadienne | En tout temps | — |
| CJP-05 | Nullité / invalidité de règlements ou d'actes de l'administration | 34, 529 al. 1 (1°) C.p.c. | Délai raisonnable; **cassation municipale : 3 mois** (692 CM; 407 LCV); **nullité du rôle d'évaluation : 1 an** (172 LFM); **annulation de vente d'immeuble pour taxes : 1 an** (1050 CM) | 529 al. 3 C.p.c.; 692 CM; 407 LCV; 172 LFM; 1050 CM |
| CJP-06 | Déclaration d'inconstitutionnalité / d'inopérabilité | 529 al. 1 (1°), 76-78 C.p.c.; 52 Loi constitutionnelle de 1982; 52 Charte québécoise | — | — |
| CJP-99 | Autre (préciser) | — | — | — |

### 15. INJ — Injonctions et mesures provisionnelles (objet principal du dossier)

*Le délai suit le droit substantiel protégé; l'urgence est la vraie contrainte*

| Code | Sous-catégorie | Fondement du recours | Notes |
|---|---|---|---|
| INJ-01 | Injonction permanente | 509, 513 C.p.c. | — |
| INJ-02 | Injonction interlocutoire / provisoire (10 jours) | 510-512 C.p.c. | — |
| INJ-03 | Ordonnances Anton Piller, Mareva, Norwich | 49 C.p.c. (pouvoirs généraux); pouvoirs inhérents — jurisprudence | — |
| INJ-04 | Saisie avant jugement | 516-522 C.p.c. | — |
| INJ-05 | Séquestre judiciaire | 523-524 C.p.c.; 2305 s. C.c.Q. | — |
| INJ-06 | Ordonnance de sauvegarde | 49, 158 C.p.c. | — |
| INJ-99 | Autre (préciser) | — | — |

### 16. EXE — Exécution et post-jugement

| Code | Sous-catégorie | Fondement du recours | Délai | Réf. délai |
|---|---|---|---|---|
| EXE-01 | Exécution forcée (saisies) | Le jugement (titre exécutoire); 656 s. C.p.c. | Le jugement se prescrit par **10 ans**; **exception : jugement contre le responsable d'un préjudice issu d'une infraction criminelle (Loi P-9.2.1) : imprescriptible — 3 ans du décès du responsable, le cas échéant** | 2924 al. 1 et 2 |
| EXE-02 | Opposition à saisie ou à vente | 735 s. C.p.c. | Délais courts d'exécution | 735 s. C.p.c. |
| EXE-03 | Outrage au tribunal | 57-62 C.p.c. | — | — |
| EXE-04 | Pourvoi en rétractation de jugement | 345-346 C.p.c. (motifs) | **Deux étapes de rigueur (D)** : signification **30 jours** (disparition de l'empêchement / connaissance du jugement, de la preuve ou du fait), puis présentation **30 jours** de la signification; **plafond : 6 mois du jugement** | 347 C.p.c. |
| EXE-99 | Autre (préciser) | — | — | — |

### 17. TRN — Transport et cargaison *(nouvelle, v1.1)*

*Personnelle · principalement contractuelle · condamnation — courts délais hétérogènes et avis préalables : vigilance particulière en réclamations de cargaison et subrogation d'assureurs*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| TRN-01 | Transporteur interne de biens | 2030 s. (contrat de transport); responsabilité : 2049-2053 | **Avis (A)** : 60 jours de la délivrance (bien délivré) ou **9 mois** de l'expédition (bien non délivré), **sous peine d'irrecevabilité**; action : 3 ans (P) | Délivrance ou date à laquelle le bien aurait dû être délivré | 2050, 2925 |
| TRN-02 | Transport maritime de biens | 2059 s.; responsabilité : 2067 s. | **1 an** | Délivrance ou, en cas de perte totale, date prévue de délivrance | 2079 |
| TRN-03 | Passagers et bagages — maritime | 37 LRMM; ann. 2, art. 3 (Conv. d'Athènes — responsabilité du transporteur) | **2 ans**; plafond absolu de **3 ans** (suspension et interruption comprises) | Débarquement réel ou prévu; décès : nuances (ann. 2, art. 16) | 37 LRMM; ann. 2 |
| TRN-04 | Abordage — cargaison, décès, blessures | 15-17 LRMM (répartition de la responsabilité) | **2 ans** (prorogeable dans certaines circonstances) | Perte, décès ou blessures | 23 LRMM |
| TRN-05 | Transport aérien | LTA, ann. I, art. 17-19 (Varsovie) / ann. VI (Montréal), selon le trajet | **2 ans (D*)** — généralement traité en déchéance | Arrivée à destination, date prévue d'arrivée ou arrêt du transport | 29 LTA, ann. I |
| TRN-06 | Personnes à charge de la victime (maritime) | 6(1)-(2) LRMM | 2 ans | Fait générateur (blessures) / décès | 14 LRMM |
| TRN-07 | Droit maritime canadien — recours résiduel | 22 LCF (compétence); droit maritime canadien (common law) | 3 ans (P) | Fait générateur | 140 LRMM |
| TRN-99 | Autre (préciser) | — | — | — | — |

### 18. ADM — Recours administratifs et statutaires *(nouvelle, v1.1)*

*Légal-statutaire · contestation, révision ou réclamation — délais courts, souvent de rigueur mais **fréquemment relevables** selon la loi applicable : vérifier chaque régime*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| ADM-01 | TAQ — recours principal | La loi sectorielle attributive du recours; institution du TAQ : 14 LJA | **30 jours** (affaires sociales : **60 jours**) | Notification de la décision ou faits d'ouverture; aucun délai si l'administration a fait défaut de statuer en révision | 110 LJA |
| ADM-02 | TAQ — révision ou révocation | 154-155 LJA | Délai raisonnable | Décision visée ou fait nouveau | 155 LJA |
| ADM-03 | Fiscal (Québec) — opposition | 93.1.1 LAF; loi fiscale créant la cotisation (ex. Loi sur les impôts) | **90 jours** | Envoi de l'avis de cotisation | 93.1.1 LAF |
| ADM-04 | Fiscal (Québec) — contestation (Cour du Québec) | 93.1.10 LAF | Ouverture : après ratification/nouvelle cotisation, ou expiration de 90/180 jours sans décision; **échéance : 90 jours** de l'envoi de la décision sur opposition (prorogeable ≤ 1 an : impossibilité d'agir) | Décision du ministre | 93.1.10, 93.1.13 LAF |
| ADM-05 | Fiscal (fédéral) — opposition | 165 LIR | **90 jours**; particuliers et successions à taux progressifs : au plus tard le dernier de (i) 1 an de l'échéance de production et (ii) 90 jours de l'envoi de la cotisation | Envoi de l'avis de cotisation | 165(1) LIR |
| ADM-06 | Fiscal (fédéral) — appel (Cour canadienne de l'impôt) | 169 LIR | Ouverture : après ratification ou 90 jours sans réponse; **échéance : 90 jours** de l'avis de ratification ou de nouvelle cotisation | — | 169(1) LIR |
| ADM-07 | Fiscalité municipale — rôle d'évaluation | 124 s. (révision), 138.5 (TAQ), 171-174 (cassation) LFM | Révision : **avant le 1er mai** suivant l'entrée en vigueur du rôle; recours au TAQ : avant le **31e jour** (138.5); cassation : 1er mai / **61e jour** de l'avis; nullité du rôle : 1 an | Force majeure : 60 jours de la fin de la situation; voir aussi CJP-05 | 124-138.5, 171-172 LFM |
| ADM-08 | Accès à l'information et renseignements personnels (CAI) | 135 LAI (révision); 42 LPRP (examen de mésentente) | Révision (public) / examen de mésentente (privé) : **30 jours** (secteur privé : relevable pour motif raisonnable) | Décision, refus ou expiration du délai de réponse | 135 LAI; 43 LPRP |
| ADM-09 | SAAQ — indemnisation (automobile) | 5 s. LAA (droit à l'indemnité sans égard à la faute) | Demande d'indemnité : **3 ans** (relevable : motifs sérieux et légitimes); révision : **60 jours**; contestation au TAQ : **60 jours** | Accident, manifestation du préjudice ou décès; victime non-résidente : 180 jours (art. 9) | 9, 11, 83.45, 83.49 LAA |
| ADM-10 | IVAC — demande de qualification | 8 s., 25 Loi P-9.2.1 (droit à l'aide financière) | **3 ans** (présomption de renonciation réfragable : motif raisonnable); violences (enfance, sexuelle, conjugale) : **en tout temps**; infractions antérieures au 13 octobre 2021 : **2 ans** | Connaissance du préjudice ou décès de la victime | 25 Loi P-9.2.1 |
| ADM-11 | Aide juridique — révisions | 4.1 s. LAJ (droit à l'aide); révisions : 74-75 LAJ | Refus, retrait, remboursement : **30 jours**; admissibilité financière (comité de révision) : **15 jours** | Décision du directeur général | 74-75 LAJ |
| ADM-99 | Autre (préciser) | — | — | — | — |

### 19. TRV — Travail et emploi *(nouvelle, v1.1)*

*Légal-statutaire (contractuel pour le recours civil) — délais très courts : pièges fréquents au moment de l'ouverture du mandat*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| TRV-01 | Congédiement sans cause juste et suffisante | 124 LNT | **45 jours (D*)** | Congédiement | 124 LNT |
| TRV-02 | Pratiques interdites | 122, 122.1 LNT (interdictions); recours : 123 | **45 jours**; congédiement, suspension ou mise à la retraite pour le motif de l'art. 122.1 : **90 jours** | Pratique reprochée | 123, 123.1 LNT |
| TRV-03 | Harcèlement psychologique | 81.18-81.20 LNT (droit à un milieu exempt de harcèlement); recours : 123.6 s. | **2 ans**; renvoi au TAT sur refus de la CNESST : 30 jours | Dernière manifestation de la conduite | 123.7, 123.9 LNT |
| TRV-04 | Réclamation civile sous la LNT | Normes (40 s.); recours : 98-99, 113 LNT | **1 an** | Chaque échéance | 115 LNT |
| TRV-05 | Code du travail — plaintes et rapports collectifs | 12-15 CT (interdictions); 47.2 (juste représentation); 100 s. (grief) | Plaintes (art. 12-15) : **30 jours**; devoir de juste représentation : **6 mois**; droits issus d'une convention collective : **6 mois** | Connaissance, sanction ou naissance de la cause d'action | 14.0.1, 16, 47.5, 71 CT |
| TRV-06 | LATMP — volet travailleur | 32 (interdiction de représailles); 44 s. (droit à l'indemnité); réclamations : 267 s. LATMP | Plainte (art. 32) : **30 jours**; réclamation : **6 mois** (violence à caractère sexuel : **2 ans**); révision : **30 jours**; contestation au TAT : **60 jours** | Lésion, décès ou connaissance | 32, 253, 270-272, 358-359.1 LATMP |
| TRV-07 | LATMP — volet employeur (imputation) | 326, 329 LATMP | Transfert de coûts : **1 an** de l'accident; partage (travailleur déjà handicapé) : avant l'expiration de la **3e année** suivant l'année de la lésion | — | 326, 329 LATMP |
| TRV-08 | Code canadien du travail (entreprises fédérales) | 97 (pratiques déloyales), 133 et 147 (représailles), 240 (congédiement injustifié) CCT | Plaintes au CCRI et congédiement injustifié : **90 jours** | Connaissance des circonstances / congédiement | 97, 133, 240 CCT |
| TRV-99 | Autre (préciser) | — | — | — | — |

### 20. APP — Appels et pourvois *(nouvelle, v1.1)*

*Mandats post-jugement — délais de rigueur emportant généralement déchéance; à ouvrir comme dossiers distincts dès la réception du jugement. La rétractation demeure à EXE-04.*

| Code | Sous-catégorie | Fondement du recours | Délai | Point de départ / pièges | Réf. délai |
|---|---|---|---|---|---|
| APP-01 | Appel civil — Cour d'appel du Québec | 30-31 C.p.c. (droit d'appel — plein droit ou permission) | **30 jours** (déclaration d'appel ± permission); appel incident : **10 jours**; jugements visés à l'art. 361 : **10 jours** (fin d'injonction interlocutoire, libération refusée, saisie avant jugement) ou **5 jours** (intégrité de la personne, garde/évaluation psychiatrique) | Avis du jugement ou jugement rendu à l'audience; **rigueur et déchéance** — la C.A. peut relever la partie (≤ 6 mois du jugement, chances raisonnables + impossibilité d'agir) | 360-363 C.p.c. |
| APP-02 | Cour suprême du Canada | 40 Loi sur la Cour suprême (autorisation) | Autorisation d'appel : **60 jours**; avis d'appel : **30 jours** | Jugement porté en appel / jugement accordant l'autorisation | 58 Loi sur la Cour suprême |
| APP-03 | Cours fédérales | 18.1 (contrôle judiciaire), 27 (appel), 28 (contrôle en C.A.F.) LCF | Contrôle judiciaire (C.F.) : **30 jours** (prorogeable); appel à la C.A.F. : **10 jours** (interlocutoire) / **30 jours** (final — **juillet et août exclus du calcul**) | Première communication de la décision / prononcé du jugement | 18.1(2), 27(2) LCF |
| APP-04 | Appels statutaires — Cour du Québec | 91 LTAL; 147 LAI; 61 LPRP; 159 LJA (droits d'appel) | TAL : **30 jours** (permission, de la connaissance); CAI : interlocutoire **10 jours**, final **30 jours** + signification 10 jours du dépôt; TAQ (affaires immobilières, territoire agricole) : **30 jours** (permission) | Décision, notification ou connaissance selon le régime | 92 LTAL; 147.1, 149, 151 LAI; 61.1, 63, 65 LPRP; 160 LJA |
| APP-05 | Faillite — appels | 193 LFI (droit d'appel) | **10 jours** (décision du registraire; décision du tribunal → cour d'appel), ou autre délai fixé par le juge | Ordonnance ou décision | 30(2), 31(1) Règles générales sur la faillite et l'insolvabilité |
| APP-06 | Divorce et ordonnances accessoires | 21 Loi sur le divorce (droit d'appel) | **30 jours** (prorogeable pour motifs particuliers, même après expiration) | Prononcé du jugement ou de l'ordonnance | 12(1), 21 Loi sur le divorce |
| APP-07 | Pénal / réglementaire (C.p.p.) | 266 s. (appel à la C.S.); 291 s. (permission — C.A.) C.p.p.* | Appel à la Cour supérieure : **30 jours**; permission d'appeler à la C.A. : **30 jours**; rétractation (jugement par défaut) : **15 jours** de la connaissance | Jugement / connaissance | 252, 271, 296 C.p.p. |
| APP-99 | Autre (préciser) | — | — | — | — |

---

## 3. Étiquettes transversales

À implémenter comme **champs booléens** (drapeaux) sur le dossier, et non comme catégories, car elles peuvent se combiner à n'importe quelle sous-catégorie :

| Étiquette | Note |
|---|---|
| Action collective | 571 s. C.p.c. — la demande d'autorisation **suspend** la prescription (2908) |
| Arbitrage / renvoi à l'arbitrage | 2638 s. C.c.Q.; 622 s. C.p.c. |
| Matière non contentieuse | 302 s. C.p.c. |
| Demande reconventionnelle | Le dossier est classé selon la demande principale |
| Petites créances | ≤ 15 000 $ (536 C.p.c.) |
| Dommages punitifs réclamés | 1621 C.c.Q.; 49 al. 2 Charte québécoise; 272 L.p.c., etc. |
| Compétence TAL (logement) | Compétence exclusive du Tribunal administratif du logement; rétractation 10 jours, révision 1 mois, appel : voir APP-04 |
| Question constitutionnelle | Avis au procureur général (76-78 C.p.c.) — au plus tard 30 jours avant la mise en état |
| **Recouvrement simplifié de certaines créances** *(v1.1)* | 535.1 s. C.p.c. — calendrier propre : pièces du demandeur **20 jours**; dénonciation des moyens préliminaires **45 jours** (+ observations 10 jours); contestation (exposé sommaire, avis et pièces) **95 jours** de la signification de l'avis d'assignation (535.4-535.7) |
| **Partie publique ou municipale** *(v1.1)* | Déclenche les avis préalables (**15 jours** LCV 585; **60 jours** CM 1112.1) et les prescriptions abrégées (**6 mois**, 585-586 LCV; 1112.1 CM); rappel : le préjudice corporel demeure régi par le C.c.Q. (2930) |

## 4. Rappels sur les déchéances

Les délais de **déchéance (D)** ne se suspendent ni ne s'interrompent en principe et pardonnent moins que la prescription. À faire ressortir visuellement dans l'application :

| Code | Recours | Délai |
|---|---|---|
| GAG-02 | Inopposabilité (paulienne) | 1 an (1635 C.c.Q.) |
| IMM-04 | Action du possesseur troublé | 1 an (929 C.c.Q.) |
| IMM-07 | Annulation de décision d'assemblée de copropriété | 90 jours (1103 C.c.Q.) |
| IMM-09 | Contestation du droit d'exproprier | 30 jours (17 Loi concernant l'expropriation)* |
| CST-04 | Hypothèque légale de la construction | 30 jours (inscription) / 6 mois (action ou préavis) |
| SUC-07 | Survie de l'obligation alimentaire | 6 mois du décès (684-685 C.c.Q.) |
| DEC-03 | Annulation de sentence arbitrale | 3 mois (648 C.p.c.) |
| EXE-04 | Rétractation de jugement | 30 jours + 30 jours / plafond 6 mois (347 C.p.c.) |
| APP-01 | Délais d'appel civils | 30 / 10 / 5 jours — déchéance expresse (363 C.p.c.), relief possible ≤ 6 mois |
| COR-11 | Dissidence — confirmation du rachat | 30 jours (380 LSAQ)* |
| TRN-05 | Transport aérien | 2 ans (29 LTA)* |
| TRV-01 | Congédiement sans cause juste et suffisante | 45 jours (124 LNT)* |

\* **Qualification prudente** : le tableau FARBQ ne qualifie pas ces délais et la jurisprudence n'est pas uniforme (déchéance stricte c. délai relevable). Avant de conclure qu'un délai expiré est fatal — ou, à l'inverse, qu'il peut être relevé —, vérifier l'état du droit propre au régime. Par ailleurs, plusieurs délais statutaires listés aux catégories ADM et TRV sont expressément relevables (p. ex. 11 LAA : motifs sérieux et légitimes; 25 Loi P-9.2.1 : motif raisonnable; 43 LPRP : motif raisonnable).

---

## Historique des versions

| Version | Date | Modifications |
|---|---|---|
| 1.0 | Juillet 2026 | Version initiale (16 catégories). |
| 1.1 | 15 juillet 2026 | Alignement sur le tableau **« Prescriptions extinctives et autres délais » de la FARBQ (mise à jour avril 2026)**. Corrections et enrichissements : COR-01 (oppression : 3 ans; exception imprescriptible), COR-06 (scission QC/féd. et conditions préalables), RCV-03 (régimes LCV et CM distingués; 586 LCV), RCV-05 (régime média — Loi sur la presse), EXE-01 (exception 2924 al. 2), EXE-04 (art. 347 C.p.c., deux étapes), CJP-05 (délais municipaux chiffrés), GAG-02 (point de départ — syndic), FAM-07 (542.32), IMM-09 (contestation 30 jours, Loi concernant l'expropriation), FAI-07 (libération d'office). Nouvelles sous-catégories COR-10 et COR-11. **Quatre nouvelles catégories : TRN (17), ADM (18), TRV (19), APP (20)** — codes existants inchangés, ajouts en fin de liste. Nouvelles étiquettes « Recouvrement simplifié » et « Partie publique ou municipale ». Règles du moteur d'échéances (2892 C.c.Q.; 107 al. 3 et 173 C.p.c.). Tableau des déchéances enrichi, avec réserve de qualification. |
| 1.2 | 16 juillet 2026 | **Ajout de la colonne « Fondement du recours »** à chacune des 20 catégories : siège central du droit d'action (1 à 3 sources pivots — C.c.Q. ou loi particulière), distinct des dispositions fixant le délai. Modèle de données : scission du champ `references` en `ref_fondement` et `ref_delai`; renommage de la colonne « Réf. » en « Réf. délai ». Fondements transversaux ajoutés aux intros de catégories (1590/1594 — REC; 1434/1590 — CON; 1457 — RCV; 2644-2646 — GAG; 34 C.p.c. — CJP). Les fondements marqués d'un astérisque (rectification LSAQ, remplacement du liquidateur, union parentale, C.p.p.) portent une numérotation à confirmer. Aucun changement aux délais ni aux codes. |

---

*Ce référentiel est un outil de classification et d'aide-mémoire. Les délais indiqués sont indicatifs : le point de départ est une question de fait, et l'interruption, la suspension ou la renonciation peuvent modifier le calcul. Les délais marqués « v1.1 » ont été recoupés avec le tableau « Prescriptions extinctives et autres délais » de la FARBQ (avril 2026), lui-même indicatif et non exhaustif. La colonne « Fondement du recours » (v1.2) identifie le siège central du droit d'action à titre indicatif : les numéros d'articles doivent être vérifiés avant toute allégation dans une procédure. La date de prescription retenue pour un dossier doit toujours être confirmée par l'avocat responsable.*
