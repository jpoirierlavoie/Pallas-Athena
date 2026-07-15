# Taxonomie des actions en justice — Droit québécois

> Référentiel de classification des dossiers pour application de gestion de pratique
> Litige civil et commercial — Québec · Version 1.0 · Juillet 2026

---

## 1. Objet et description

Ce document définit une **taxonomie à deux niveaux** (catégorie → sous-catégorie) des actions en justice en droit québécois, conçue pour servir de liste de choix dans une application de gestion de pratique. Elle poursuit trois objectifs :

1. **Classer les dossiers** de façon uniforme et lisible d'un coup d'œil;
2. **Assister le calcul de la prescription** en attachant à chaque sous-catégorie un délai indicatif, son type juridique et son point de départ;
3. **Permettre le filtrage et les statistiques** de pratique par famille de recours.

La liste ne prétend pas à l'exhaustivité : chaque catégorie se termine par un item « Autre (préciser) ».

### Principe de conception

Depuis le Code de procédure civile de 2016, il n'existe qu'un seul véhicule procédural — la demande introductive d'instance (art. 141 et s. C.p.c.). La « typologie des actions » est donc une classification **doctrinale et substantielle**, non procédurale. Elle croise plusieurs axes (nature du droit exercé, fondement, résultat recherché) qu'il ne faut **pas mélanger dans la hiérarchie de la liste** : la liste déroulante suit un seul axe pratique (domaine → action nommée), et les axes doctrinaux deviennent des **champs dérivés**, remplis automatiquement lorsque l'utilisateur choisit une sous-catégorie.

### Modèle de données suggéré

Chaque sous-catégorie porte les attributs suivants :

| Champ | Valeurs | Utilité |
|---|---|---|
| `code` | ex. `RCV-01` | Clé stable pour la base de données |
| `nature` | personnelle · réelle · mixte | Qualification doctrinale; règles de compétence territoriale |
| `fondement` | contractuel · extracontractuel · légal-statutaire | Régime de responsabilité; non-cumul (art. 1458 al. 2 C.c.Q.) |
| `resultat` | condamnation · déclaratoire · constitutif-extinctif | Nature des conclusions |
| `delai_duree` | ex. « 3 ans », « 90 jours », « imprescriptible » | Suggestion de calcul |
| `delai_type` | **P** (prescription) · **D** (déchéance) · **A** (avis préalable) | Voir conventions ci-dessous |
| `point_depart` | règle textuelle (ex. « manifestation du préjudice ») | Le point de départ est presque toujours **factuel** |
| `references` | articles de loi | Vérification par l'avocat |

### Conventions

- **P — Prescription** : délai interruptible et suspensible (art. 2889 et s. C.c.Q.).
- **D — Déchéance** : délai de rigueur, en principe ni interruptible ni suspensible; à signaler visuellement dans l'application.
- **A — Avis préalable** : dénonciation ou avis requis avant ou en sus du recours (ex. dénonciation d'un vice caché, avis municipal).
- Les références sont au **Code civil du Québec** sauf indication contraire (C.p.c., LFI, LSAQ, LCSA, LCV, LMC, LACC).

### Avertissement d'implémentation

Le délai attaché à chaque sous-catégorie est une **suggestion calculée, jamais une valeur ferme**. Le point de départ dépend des faits (manifestation, connaissance, fin des travaux), et l'interruption ou la suspension de la prescription échappe à tout calcul automatique. Recommandation : l'application propose une date, mais un champ « date de prescription confirmée par l'avocat » demeure obligatoire avant d'alimenter les rappels et l'agenda.

---

## 2. La taxonomie

### 1. REC — Recouvrement de créances

*Personnelle · contractuelle · condamnation — prescription 3 ans (art. 2925) sauf indication*

| Code | Sous-catégorie | Délai | Point de départ / pièges | Réf. |
|---|---|---|---|---|
| REC-01 | Action sur compte (biens vendus, services rendus) | 3 ans (P) | Exigibilité de **chaque facture** | 2925, 2931 |
| REC-02 | Prêt, reconnaissance de dette | 3 ans (P) | Terme; prêt à demande : nuances jurisprudentielles | 2925, 2880 |
| REC-03 | Effets de commerce (chèque, billet) | 3 ans (P) | Exigibilité de l'effet | 2925 |
| REC-04 | Cautionnement | 3 ans (P) | Défaut du débiteur principal; caractère accessoire | 2333 s. |
| REC-05 | Loyers et charges (bail commercial) | 3 ans (P) | Chaque échéance | 2931 |
| REC-06 | Honoraires professionnels | 3 ans (P) | Exigibilité / fin du mandat | 2925 |
| REC-99 | Autre (préciser) | — | — | — |

### 2. CON — Contrats : exécution, anéantissement, garanties

*Personnelle (mixte pour CON-06) · contractuelle · condamnation ou constitutif*

| Code | Sous-catégorie | Délai | Point de départ / pièges | Réf. |
|---|---|---|---|---|
| CON-01 | Exécution en nature | 3 ans (P) | Défaut | 1601 |
| CON-02 | Dommages-intérêts contractuels | 3 ans (P) | Manifestation du préjudice | 1458, 2926 |
| CON-03 | Résolution / résiliation | 3 ans (P) | Défaut | 1604 s. |
| CON-04 | Nullité (vices de consentement, capacité) | 3 ans (P) | Connaissance de la cause; crainte : sa cessation. Imprescriptible **par exception** (moyen de défense) | 2927, 2882 |
| CON-05 | Réduction de l'obligation / du prix | 3 ans (P) | Idem | 1604 al. 3 |
| CON-06 | Passation de titre (action mixte) | 3 ans (P) | Refus de signer | 1712 |
| CON-07 | Vices cachés (garantie de qualité) | 3 ans (P) **+ A** | Découverte; **dénonciation écrite dans un délai raisonnable** | 1726, 1739 |
| CON-08 | Bail commercial (éviction, résiliation, renouvellement) | 3 ans (P) | Selon le droit invoqué | 1851 s. |
| CON-09 | Assurance — réclamation d'indemnité | 3 ans (P) | Naissance du droit; avis de sinistre : délais de police | 2425 s. |
| CON-10 | Action directe contre l'assureur du responsable | 3 ans (P) | Suit le recours contre l'assuré | 2501 |
| CON-99 | Autre (préciser) | — | — | — |

### 3. RCV — Responsabilité civile extracontractuelle

*Personnelle · extracontractuelle · condamnation*

| Code | Sous-catégorie | Délai | Point de départ / pièges | Réf. |
|---|---|---|---|---|
| RCV-01 | Préjudice corporel | 3 ans (P) | 1re manifestation; **avis et courts délais inopposables** | 2926, 2930 |
| RCV-02 | Préjudice corporel — acte criminel / violences | 10 ans; **imprescriptible** (violence sexuelle, violence subie pendant l'enfance, violence conjugale); décès de la victime ou de l'auteur : 3 ans du décès | Manifestation | 2926.1 |
| RCV-03 | Préjudice matériel | 3 ans (P) | **Municipalités : avis 15 jours + prescription 6 mois** | 2925; 585-586 LCV |
| RCV-04 | Préjudice moral / psychologique | 3 ans (P) | Manifestation | 2926 |
| RCV-05 | Diffamation | **1 an** (P) | Connaissance de l'atteinte | 2929 |
| RCV-06 | Vie privée, renseignements personnels | 3 ans (P) | 1 an si l'atteinte est à la réputation | 35-41; 2929 |
| RCV-07 | Responsabilité professionnelle | 3 ans (P) | Manifestation; médical → souvent RCV-01/02 | 2926 |
| RCV-08 | Produits — fabricant / vendeur spécialisé | 3 ans (P) | Découverte | 1468, 1730 |
| RCV-09 | Fait d'autrui, fait des biens, animaux, ruine du bâtiment | 3 ans (P) | — | 1459-1467 |
| RCV-10 | Troubles de voisinage | 3 ans (P) | Préjudice continu : renaissance au jour le jour | 976 |
| RCV-11 | Abus de procédure | 3 ans (P) | Fin de l'instance abusive (généralement) | 51 C.p.c. |
| RCV-99 | Autre (préciser) | — | — | — |

### 4. RES — Restitutions et quasi-contrats

*Personnelle · ni contractuelle ni délictuelle · condamnation — 3 ans (art. 2925)*

| Code | Sous-catégorie | Particularités | Réf. |
|---|---|---|---|
| RES-01 | Réception de l'indu | Départ : paiement / sa découverte | 1491 |
| RES-02 | Enrichissement injustifié | Caractère subsidiaire; conjoints de fait : fin de la vie commune | 1493 |
| RES-03 | Gestion d'affaires | — | 1482 |
| RES-04 | Restitution des prestations | Accessoire à l'anéantissement de l'acte | 1699 s. |
| RES-99 | Autre (préciser) | — | — |

### 5. GAG — Protection du gage commun du créancier

| Code | Sous-catégorie | Délai | Point de départ | Réf. |
|---|---|---|---|---|
| GAG-01 | Action oblique | Délai du droit du débiteur exercé | — | 1627 |
| GAG-02 | Action en inopposabilité (paulienne) | **1 an (D — déchéance)** | Connaissance du préjudice | 1631, 1635 |
| GAG-03 | Simulation / contre-lettre | 3 ans (P) | Connaissance | 1451-1452 |
| GAG-99 | Autre (préciser) | — | — | — |

### 6. IMM — Réel et immobilier

*Réelle (sauf indication) · résultat variable*

| Code | Sous-catégorie | Délai | Particularités | Réf. |
|---|---|---|---|---|
| IMM-01 | Revendication | Imprescriptible (propriété) | Limite pratique : prescription acquisitive d'autrui | 953, 2918 |
| IMM-02 | Servitudes (confessoire, négatoire, extinction) | 10 ans (P) | Extinction par non-usage : 10 ans | 2923; 1191 |
| IMM-03 | Bornage | Imprescriptible | — | 978 |
| IMM-04 | Action du possesseur troublé | **1 an (D)** | Possession paisible > 1 an requise | 929 |
| IMM-05 | Empiètement / accession | Variable | — | 992 s. |
| IMM-06 | Prescription acquisitive (demande en acquisition) | Possession 10 ans | Jugement requis pour l'immeuble | 2918 |
| IMM-07 | Copropriété — annulation de décision d'assemblée | **90 jours (D)** | Date de l'assemblée | 1103 |
| IMM-08 | Fin d'indivision — partage, licitation | Imprescriptible durant l'indivision | — | 1030 |
| IMM-09 | Expropriation déguisée | Régime en évolution (nouvelle Loi sur l'expropriation, 2023) | Atteinte continue | 952 |
| IMM-10 | Radiation d'inscription (registre foncier) | — | — | 3057 s. |
| IMM-99 | Autre (préciser) | — | — | — |

### 7. CST — Construction

| Code | Sous-catégorie | Délai | Particularités | Réf. |
|---|---|---|---|---|
| CST-01 | Perte de l'ouvrage (solidité) | Garantie **5 ans** (couverture) + action 3 ans (P) | Fin des travaux; manifestation | 2118-2119 |
| CST-02 | Malfaçons | Garantie **1 an** de la réception + action 3 ans (P) | Réception avec/sans réserve | 2120 |
| CST-03 | Réclamations de chantier (extras, retards) | 3 ans (P) | Avis contractuels souvent stricts (A) | 2098 s. |
| CST-04 | Hypothèque légale de la construction | Inscription : **30 jours** de la fin des travaux; action/préavis : **6 mois (D)** | Fin des travaux | 2726-2727 |
| CST-05 | Cautionnements de chantier | Délais de la police (A/D) | Avis à la caution | — |
| CST-99 | Autre (préciser) | — | — | — |

### 8. COR — Corporatif et commercial

| Code | Sous-catégorie | Délai | Particularités | Réf. |
|---|---|---|---|---|
| COR-01 | Oppression (recours pour abus) | Aucun délai fixe; 3 ans par analogie (controverse) | — | 450 LSAQ; 241 LCSA |
| COR-02 | Action dérivée (pour le compte de la société) | — | Autorisation préalable du tribunal | 445 LSAQ; 239 LCSA |
| COR-03 | Conventions d'actionnaires (rachat, évaluation) | 3 ans (P) | — | 2925 |
| COR-04 | Nullité de résolutions; rectification de registres | Diligence | — | — |
| COR-05 | Liquidation / dissolution judiciaire | — | — | 463 s. LSAQ |
| COR-06 | Responsabilité des administrateurs (salaires, etc.) | Délais stricts (6 mois / 2 ans LCSA) | — | 154 LSAQ; 119 LCSA |
| COR-07 | Non-concurrence, non-sollicitation, secrets commerciaux | 3 ans (P) + injonction | — | 2088-2089 |
| COR-08 | Concurrence déloyale | 3 ans (P) | — | 1457; 7 LMC |
| COR-09 | Vente d'entreprise (garanties, ajustements de prix) | 3 ans (P) ou clause de survie (qualification débattue, 2884) | — | — |
| COR-99 | Autre (préciser) | — | — | — |

### 9. HYP — Sûretés et recours hypothécaires

*Réelle (accessoire) · condamnation / délaissement*

| Code | Sous-catégorie | Particularités | Réf. |
|---|---|---|---|
| HYP-01 | Préavis d'exercice et délaissement | Délais de délaissement 10-60 jours selon le recours | 2757 s. |
| HYP-02 | Prise en paiement | Autorisation judiciaire si ≥ 50 % payé | 2778 |
| HYP-03 | Vente sous contrôle de justice / par le créancier | — | 2791; 2784 |
| HYP-04 | Prise de possession à des fins d'administration | — | 2773 |
| HYP-05 | Action personnelle sur la créance garantie | 3 ans (P); l'hypothèque s'éteint avec la créance | 2797 |
| HYP-99 | Autre (préciser) | — | — |

### 10. FAI — Faillite et insolvabilité (fédéral)

| Code | Sous-catégorie | Délai clé | Réf. |
|---|---|---|---|
| FAI-01 | Requête en ordonnance de faillite | Acte de faillite dans les **6 mois** précédents | 43 LFI |
| FAI-02 | Proposition / avis d'intention | Délais LFI stricts | 50, 50.4 LFI |
| FAI-03 | Arrangement LACC | — | LACC |
| FAI-04 | Nomination d'un séquestre | — | 243 LFI |
| FAI-05 | Recours du syndic (préférences, opérations sous-évaluées) | Périodes suspectes : 3/12 mois; 1 an / 5 ans | 95-96 LFI |
| FAI-06 | Réclamations, libération, dettes exclues | — | 121 s., 178 LFI |
| FAI-99 | Autre (préciser) | — | — |

### 11. FAM — Familial

*Majoritairement constitutif d'état — imprescriptible sauf indication*

| Code | Sous-catégorie | Délai / piège | Réf. |
|---|---|---|---|
| FAM-01 | Divorce, séparation de corps, dissolution d'union civile | Aucun | — |
| FAM-02 | Autorité parentale, temps parental | En tout temps (intérêt de l'enfant) | — |
| FAM-03 | Aliments | En tout temps; **arrérages : 3 ans** | 2931 |
| FAM-04 | Patrimoine familial, régimes matrimoniaux | Accessoire à la demande principale | 414 s. |
| FAM-05 | Prestation compensatoire (décès) | **1 an du décès (P)** | 427; 2928 |
| FAM-06 | Union parentale (régime en vigueur depuis le 30 juin 2025) | Régime nouveau — à paramétrer | — |
| FAM-07 | Filiation | Délais spéciaux stricts | 530 s. |
| FAM-08 | Conjoints de fait — enrichissement injustifié | 3 ans de la fin de la vie commune | 1493 |
| FAM-99 | Autre (préciser) | — | — |

### 12. SUC — Successions et personnes

| Code | Sous-catégorie | Délai | Réf. |
|---|---|---|---|
| SUC-01 | Vérification de testament (non contentieux) | — | 302 s. C.p.c. |
| SUC-02 | Contestation de testament (captation, incapacité) | 3 ans (P), connaissance | 2927 |
| SUC-03 | Pétition d'hérédité | **10 ans** de l'ouverture | 626 |
| SUC-04 | Option de l'héritier (délibération) | 6 mois | 632 |
| SUC-05 | Partage successoral | Imprescriptible durant l'indivision | 836 s. |
| SUC-06 | Reddition de compte / destitution du liquidateur | 3 ans (P) | 806 s. |
| SUC-07 | Survie de l'obligation alimentaire | **6 mois du décès (D)** | 684-685 |
| SUC-08 | Tutelle au majeur, mandat de protection | Non contentieux | 268 s. |
| SUC-09 | Jugement déclaratif de décès | 7 ans d'absence | 92 s. |
| SUC-99 | Autre (préciser) | — | — |

### 13. DEC — Déclaratoire, homologation, reconnaissance

*Déclaratoire — le délai suit généralement le droit sous-jacent*

| Code | Sous-catégorie | Délai | Réf. |
|---|---|---|---|
| DEC-01 | Jugement déclaratoire | Suit le droit sous-jacent; le moyen de défense est imprescriptible | 142 C.p.c.; 2882 |
| DEC-02 | Homologation de transaction | — | 2631 s. |
| DEC-03 | Homologation / **annulation** de sentence arbitrale | Annulation : **3 mois (D)** | 645-648 C.p.c. |
| DEC-04 | Reconnaissance de décision étrangère | 10 ans (P) | 2924; 3155 s.; 507 s. C.p.c. |
| DEC-99 | Autre (préciser) | — | — |

### 14. CJP — Contrôle judiciaire et pourvois (anciens recours extraordinaires)

*Légal-statutaire · annulation / ordonnance — « délai raisonnable » (≈ 30 jours en jurisprudence)*

| Code | Sous-catégorie | Délai | Réf. |
|---|---|---|---|
| CJP-01 | Annulation de décision (évocation, certiorari) | Délai raisonnable | 529 C.p.c. |
| CJP-02 | Mandamus (accomplissement d'un devoir) | Délai raisonnable | 529 C.p.c. |
| CJP-03 | Quo warranto (usurpation de fonction) | Délai raisonnable; délais spéciaux en matière municipale | 529 C.p.c. |
| CJP-04 | Habeas corpus | En tout temps | 398 C.p.c. |
| CJP-05 | Nullité / invalidité de règlements ou d'actes de l'administration | Délai raisonnable; recours en cassation municipaux : délais courts spécifiques | 529 C.p.c. |
| CJP-06 | Déclaration d'inconstitutionnalité / d'inopérabilité | — | 529, 76-78 C.p.c. |
| CJP-99 | Autre (préciser) | — | — |

### 15. INJ — Injonctions et mesures provisionnelles (objet principal du dossier)

*Le délai suit le droit substantiel protégé; l'urgence est la vraie contrainte*

| Code | Sous-catégorie | Réf. |
|---|---|---|
| INJ-01 | Injonction permanente | 509 s. C.p.c. |
| INJ-02 | Injonction interlocutoire / provisoire (10 jours) | 510-511 C.p.c. |
| INJ-03 | Ordonnances Anton Piller, Mareva, Norwich | Jurisprudence |
| INJ-04 | Saisie avant jugement | 516 s. C.p.c. |
| INJ-05 | Séquestre judiciaire | 523 s. C.p.c. |
| INJ-06 | Ordonnance de sauvegarde | 49, 158 C.p.c. |
| INJ-99 | Autre (préciser) | — |

### 16. EXE — Exécution et post-jugement

| Code | Sous-catégorie | Délai | Réf. |
|---|---|---|---|
| EXE-01 | Exécution forcée (saisies) | Le jugement se prescrit par **10 ans** | 2924 |
| EXE-02 | Opposition à saisie ou à vente | Délais courts d'exécution | 735 s. C.p.c. |
| EXE-03 | Outrage au tribunal | — | 57 s. C.p.c. |
| EXE-04 | Pourvoi en rétractation de jugement | **30 jours** de la connaissance / max 6 mois (D) | 345 s. C.p.c. |
| EXE-99 | Autre (préciser) | — | — |

---

## 3. Étiquettes transversales

À implémenter comme **champs booléens** (drapeaux) sur le dossier, et non comme catégories, car elles peuvent se combiner à n'importe quelle sous-catégorie :

| Étiquette | Note |
|---|---|
| Action collective | 571 s. C.p.c. — la demande d'autorisation **suspend** la prescription (2908) |
| Arbitrage / renvoi à l'arbitrage | 622 s. C.p.c. |
| Matière non contentieuse | 302 s. C.p.c. |
| Demande reconventionnelle | Le dossier est classé selon la demande principale |
| Petites créances | ≤ 15 000 $ (536 C.p.c.) |
| Dommages punitifs réclamés | 1621 C.c.Q.; Charte, L.p.c., etc. |
| Compétence TAL (logement) | Compétence exclusive du Tribunal administratif du logement |
| Question constitutionnelle | Avis au procureur général (76-78 C.p.c.) |

## 4. Rappels sur les déchéances

Les délais de **déchéance (D)** ne se suspendent ni ne s'interrompent en principe et pardonnent moins que la prescription. À faire ressortir visuellement dans l'application :

| Code | Recours | Délai |
|---|---|---|
| GAG-02 | Inopposabilité (paulienne) | 1 an (1635 C.c.Q.) |
| IMM-04 | Action du possesseur troublé | 1 an (929 C.c.Q.) |
| IMM-07 | Annulation de décision d'assemblée de copropriété | 90 jours (1103 C.c.Q.) |
| CST-04 | Hypothèque légale de la construction | 30 jours (inscription) / 6 mois (action ou préavis) |
| SUC-07 | Survie de l'obligation alimentaire | 6 mois du décès (684-685 C.c.Q.) |
| DEC-03 | Annulation de sentence arbitrale | 3 mois (648 C.p.c.) |
| EXE-04 | Rétractation de jugement | 30 jours / 6 mois (345 s. C.p.c.) |

---

*Ce référentiel est un outil de classification et d'aide-mémoire. Les délais indiqués sont indicatifs : le point de départ est une question de fait, et l'interruption, la suspension ou la renonciation peuvent modifier le calcul. La date de prescription retenue pour un dossier doit toujours être confirmée par l'avocat responsable.*
