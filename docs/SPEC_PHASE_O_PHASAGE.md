# Spécification — Codes de phase du litige (axe 1) — Pallas Athéna

**Destinataire :** Claude Code
**Cible :** dépôt `Athena-Pallas`, branche `main`
**Statut :** prêt à implémenter — lire d'abord la section 1 (terminologie : deux sens du mot « phase »)
**Lettre de phase de développement :** **« O » proposée** — à confirmer par le praticien, qui tient le registre des phases.
**Portée volontairement étroite :** la présente phase pose **la taxonomie** et **le champ** sur quatre collections, plus le câblage DAV et MCP. Elle **ne construit ni** l'estimateur, **ni** la jauge budget-réalisé, **ni** l'analytique de durées (voir §10, sequels).

---

## 0. Résumé exécutif

Introduire une **taxonomie de phases du litige** (l'« axe 1 ») : une liste fermée, hiérarchique (phase → sous-code), destinée à **apparier le temps et les frais aux budgets convenus avec le client** et à **constituer un jeu de données** sur la durée typique des phases. L'**axe 2** (nature du travail — analyse, rédaction, préparation, représentation…) **n'est pas modélisé par un code** : il vit dans le **libellé narratif** de l'entrée de temps. Le praticien a tranché : **pour la communication au client et l'établissement des budgets, l'axe 1 est privilégié**, et les sous-codes découpent donc par **sous-événement procédural, livrable ou type de moyen** — jamais par nature de travail, sous peine de redire ce que la description dit déjà.

Cette taxonomie est **orthogonale** à la taxonomie d'**actions** existante (`utils/taxonomie.py` : `REC`, `CON`, `CST`…). Un dossier `REC-01` traverse quand même `PRE → INT → CTS → … → JUG`. Ce sont **deux colonnes distinctes** du modèle, à ne jamais confondre.

Le code de phase doit vivre sur **quatre** surfaces : `time_entries`, `expenses`, `tasks`, et **les étapes des gabarits de protocole**. Cette quatrième cible est la clé de voûte architecturale : si chaque étape de gabarit porte un code de phase, alors **protocole, budget et temps partagent une même colonne vertébrale** — le protocole donne l'échéance, le budget l'enveloppe, les entrées la consommation.

Deux garde-fous non négociables, hérités des principes du projet :

1. **Codes ASCII stricts** (`PRE-04`, jamais `PRÉ-04`). Le libellé porte les accents ; le code, jamais. Raison : stabilité des clés Firestore, normalisation Unicode des `CATEGORIES` iCalendar (aller-retour DavX5 / jtx Board), encodage d'URL, collation de tri, exports CSV.
2. **Aucune suppression** : aucune route, aucun outil, aucun élément d'interface de suppression. Le déploiement est **additif** ; les documents anciens sans code de phase sont tolérés en lecture (voir §8).

---

## 1. Terminologie (À LIRE EN PREMIER)

Le mot « phase » est ambigu dans ce projet. Le présent document distingue rigoureusement :

- **Phase de développement** — l'unité du registre de développement de Pallas Athéna (Phase K, Phase N, la présente **Phase O**). Toujours écrite « Phase » avec majuscule et lettre.
- **Phase du litige** / **code de phase** — l'objet métier introduit ici (`PRE`, `INT`, `CTS`…). Toujours écrite « phase du litige », « code de phase », ou simplement le code.

Partout dans le code livré, nommer l'objet métier **`phase`** (au sens litige). Ne jamais réutiliser le terme pour désigner une phase de développement dans un identifiant, un commentaire ou un libellé d'interface.

**Collision d'anagramme à surveiller.** Le code de phase `CTS` (Contestation) est l'anagramme du domaine d'action `CST` (existant dans `taxonomie.py`). Les deux **n'occupent jamais le même champ**, donc aucune ambiguïté machine. Mais dans un CSV ou une ligne de journal où les deux coexistent, l'œil humain les confond. **Décision par défaut retenue :** conserver `CTS`, à la condition que toute sortie destinée à l'humain affiche le **libellé**, le code restant réservé aux champs techniques. Variante surchargeable en §9 (`DEF`).

---

## 2. Objectifs et hors-portée

**Objectifs de la présente phase.**

1. Définir la taxonomie de phases (données de référence — Annexe A) dans un module dédié, sur le modèle de `taxonomie.py`.
2. Ajouter le champ de phase à `time_entries`, `expenses`, `tasks`, additif et nullable en lecture, requis à l'écriture nouvelle (§4).
3. Annoter chaque **étape de gabarit de protocole** d'un code de phase (§5).
4. Exposer le **code** de phase dans `CATEGORIES` des VTODO (tâches) — jamais le libellé (§6).
5. Étendre les outils d'écriture MCP (`create_time_entry`, `create_expense`, `create_task`) d'un paramètre de phase optionnel, et **étendre le test de conformité** schémas MCP ↔ constantes (§7).

**Hors-portée — nommé explicitement pour éviter le débordement (voir §10 pour le détail).**

- L'**estimateur de devis** dans l'application (qui remplacerait à terme le classeur Excel).
- La **jauge budget-vs-réalisé** et le seuil d'alerte de dépassement.
- L'**analytique de durées** (médianes, IQR par phase × action × classe × contesté).
- Le **versionnement des budgets** (horodaté, jamais écrasé) — prérequis déontologique de la jauge, mais objet d'une phase distincte.

Ces quatre chantiers **présupposent** la présente phase et n'ont de sens qu'après elle. La présente spec est leur socle.

---

## 3. Décisions liantes

| # | Décision |
|---|---|
| D-1 | La taxonomie de phases est **orthogonale** à `taxonomie.py`. Deux colonnes distinctes ; ne pas fusionner, ne pas dériver l'une de l'autre. |
| D-2 | Les phases **ne portent aucune référence législative** dans les données (pas de `ref_delai`, pas de `ref_fondement`). Une phase est un construit d'**organisation et de facturation**, non la source d'un délai. Les articles cités en discussion sont indicatifs, non stockés, et restent à vérifier au texte officiel. |
| D-3 | Codes **ASCII stricts**. Libellés accentués en base et à l'écran. |
| D-4 | **Modèle à deux champs**, en miroir de `domaine` / `action` : un champ **`phase`** portant le code parent (`CTS`) et un champ **`sous_phase`** portant le sous-code complet (`CTS-02`). `sous_phase` par défaut au `-00` de la phase. Motif Firestore : le parent stocké permet un `where('phase','==','CTS')` sans découpage de chaîne. |
| D-5 | Champ présent sur **quatre** surfaces : `time_entries`, `expenses`, `tasks`, **étapes de gabarits de protocole**. |
| D-6 | **Requis à l'écriture nouvelle** de `time_entries` / `expenses` / `tasks`, avec **valeur par défaut déduite de l'état du protocole** lorsqu'elle existe (§10). **Nullable / absent toléré** sur les documents anciens — additif, **sans migration** (patron `date_avis`). |
| D-7 | `CATEGORIES` des VTODO reçoit le **code** de phase (jamais le libellé), pour que renommer une phase n'orpheline aucun tag DAV. |
| D-8 | **`facturable = false` par défaut** sur les entrées de phase **`ADM`**. Idem `HOR`. Ces deux phases sont **retirées du devis client** mais **conservées dans le dataset** (voir §9, D-14). |
| D-9 | Convention de sous-codes, en miroir de `taxonomie.py` : chaque phase reçoit un **`-00` « Général »** (imputation à la phase sans ventilation) et un **`-99` « Autre (préciser) »**. Exception : `HOR` ne reçoit que `-00`. Le `-00` permet de **déployer les phases immédiatement** et de n'activer les sous-codes que plus tard, sans rupture de schéma. |
| D-10 | **Aucune suppression** nulle part. |
| D-11 | Les champs `category` existants de `tasks` (≈ nature de travail) et de `expenses` (type de déboursé) sont **orthogonaux** au nouveau champ `phase`. **Ne pas les surcharger, ne pas les réutiliser, ne pas les confondre** avec l'axe 1. |

---

## 4. Modèle de données

### 4.1 Module de taxonomie (données de référence)

Créer un module dédié — recommandation : **`utils/phases.py`** — qui **calque la structure de `utils/taxonomie.py`** (`Domaine` contenant des `Action`). La hiérarchie devient `Phase` contenant des `SousCode` :

```python
# utils/phases.py  — SHAPE indicative ; Claude Code arrête la forme définitive.

Categorie = Literal["tronc", "module", "transversal", "residuel"]

@dataclass(frozen=True)
class SousCode:
    code: str          # ASCII, ex. "CTS-02"
    libelle: str       # accentué, ex. "Défense (écrite ou orale)"

@dataclass(frozen=True)
class Phase:
    code: str                    # ASCII, ex. "CTS"
    libelle: str                 # ex. "Contestation"
    categorie: Categorie         # tronc | module | transversal | residuel
    ordre: int | None            # rang dans le tronc ordonné ; None hors tronc
    facturable_defaut: bool      # False pour ADM et HOR ; True sinon
    portee: str                  # courte description (une ligne)
    sous_codes: tuple[SousCode, ...]

PHASES: dict[str, Phase] = { ... }   # cf. Annexe A
```

Les constantes plates dérivées (`VALID_PHASE_CODES`, `PHASE_LABELS`, `VALID_SOUS_PHASE_CODES`, `SOUS_PHASE_LABELS`, l'ensemble des phases `facturable_defaut = False`, la liste ordonnée du tronc) sont **calculées à partir de `PHASES`** puis **exposées dans `models/vocab.py`** de la même manière que le sont déjà les constantes dérivées de `taxonomie.py`. *(La mécanique exacte de remontée vers `vocab.py` appartient au praticien : suivre le précédent de `taxonomie.py`.)*

**Donnée, pas configuration d'exécution.** Comme la taxonomie d'actions, la taxonomie de phases est une **constante de module** (elle change par déploiement, pas par l'utilisateur en session). Elle ne relève **pas** du patron « skills / tâches planifiées = données éditables à l'exécution ».

### 4.2 Champ sur les collections

Ajouter aux `_default_doc()` (ou équivalent) de `time_entries`, `expenses`, `tasks` :

```python
"phase": "",         # code parent ASCII, ex. "CTS" ; "" = non renseigné (docs anciens)
"sous_phase": "",    # sous-code complet ASCII, ex. "CTS-02" ; défaut = "<phase>-00"
```

- **Validation** : `phase ∈ VALID_PHASE_CODES` ; `sous_phase ∈ VALID_SOUS_PHASE_CODES` **et** `sous_phase` préfixé par `phase`. Un `sous_phase` dont le préfixe contredit `phase` est **rejeté** (zone à incohérence silencieuse).
- **Écriture nouvelle** : `phase` requis (défaut déduit du protocole si disponible — §10 — sinon choix forcé côté formulaire). `sous_phase` par défaut au `-00`.
- **Lecture d'ancien document** : `phase == ""` toléré. Aucune migration, aucun rétro-remplissage automatique au jugé (§8).

---

## 5. Points de contact (fichiers)

Table indicative — **s'aligner sur le code vivant** avant d'éditer.

| Fichier / zone | Nature de l'intervention |
|---|---|
| `utils/phases.py` *(nouveau)* | Taxonomie `Phase`/`SousCode` + `PHASES` (Annexe A). |
| `models/vocab.py` | Remontée des constantes dérivées, sur le modèle des constantes d'actions. |
| `models/time_entry.py` | Champs `phase`/`sous_phase` + validation + défaut à l'écriture. |
| `models/expense.py` | Idem. |
| `models/task.py` | Idem. **Ne pas toucher `category`** (orthogonal). |
| `models/protocol.py` + graine des gabarits | **Chaque étape de gabarit** (CQ simplifié / CS ordinaire / conventionnel) reçoit un attribut `phase`. Voir §10 pour la dérivation du défaut. |
| `routes/…` (temps, dépenses, tâches) | Formulaires : sélecteur de phase (bande « codes récents » + liste complète au 2ᵉ niveau, filtrée par phase — §10) ; préremplissage du défaut. |
| `models/task.py` → sérialisation VTODO | `CATEGORIES` reçoit le **code** de phase (§6). |
| Couche `mcp/` (outils temps/dépenses/tâches) | Paramètre `phase`/`sous_phase` optionnel (§7). |
| `tests/` | Non-régression + **test de conformité** MCP ↔ constantes étendu (§7) ; test de cohérence préfixe `sous_phase`/`phase`. |
| `firestore.indexes.json` | **Seulement si** une requête filtrée par `phase` avec tri est réellement introduite. Sinon, ne rien ajouter. |

---

## 6. Sérialisation DAV — `CATEGORIES`

Lorsqu'une **tâche** porte une phase, sa sérialisation VTODO ajoute le **code** de phase à `CATEGORIES` (aux côtés des catégories déjà émises, sans les remplacer) :

```
CATEGORIES:CTS
```

Jamais le libellé. Motif (déjà acté au projet) : le code est ASCII et stable ; renommer « Contestation » en « Défense » ne touche alors **aucun** VTODO et évite la migration côté client que le praticien a signalée comme le coût des étiquettes DAV. Bénéfice collatéral : tuiles colorées par phase dans jtx Board.

**Round-trip.** Au retour (`vtodo_to_task`), n'accepter comme `phase` qu'une valeur de `VALID_PHASE_CODES` ; ignorer toute autre catégorie. La normalisation Unicode NFC/NFD n'étant pas garantie identique entre le serveur (NFC) et l'aller-retour Android, **c'est précisément la contrainte ASCII qui protège** ce chemin. VJOURNAL / notes : hors portée pour l'instant (les notes ne figurent pas parmi les quatre surfaces).

---

## 7. Connecteur MCP + test de conformité

Les outils d'écriture `create_time_entry`, `create_expense`, `create_task` gagnent deux paramètres **optionnels** : `phase` et `sous_phase`, validés comme en §4.2. Les autres outils sont inchangés. **Aucun outil de suppression** (rappel D-10).

Le **test de conformité** existant (schémas MCP ↔ constantes de `vocab.py`) doit être **étendu** pour couvrir le nouveau couple : l'énumération de `phase`/`sous_phase` exposée par les schémas MCP doit rester synchrone avec `VALID_PHASE_CODES` / `VALID_SOUS_PHASE_CODES`. Ajouter en outre un test vérifiant l'invariant de préfixe (`sous_phase` commence par `phase`).

---

## 8. Migration

**Additif, sans migration destructive ni rétro-remplissage au jugé.** Les documents anciens restent valides avec `phase == ""`.

Si le praticien souhaite récupérer l'historique, la dérivation se fait **hors de la présente phase** et doit être **traçable** : croiser les dates des étapes du protocole avec des mots-clés de description, mais alors **estampiller** `phase_source: "inferred"` par opposition à `"entered"`. Sans ce drapeau, il deviendra impossible, dans deux ans, de distinguer ce qui a été **observé** de ce qui a été **deviné** — et toute analytique honnête devra pouvoir exclure les lignes inférées. Ce champ `phase_source` n'est **pas** requis par la présente phase ; il est nommé ici pour que, s'il est ajouté un jour, il le soit dès le premier rétro-remplissage et jamais après coup.

---

## 9. Défauts surchargeables (décisions du praticien)

Points laissés ouverts. Défaut retenu à gauche ; à confirmer ou renverser.

| # | Défaut retenu | Variante possible |
|---|---|---|
| D-12 | Lettre de phase de développement **« O »**. | Toute autre lettre libre du registre. |
| D-13 | Code de contestation **`CTS`** (affichage par libellé pour éviter la collision visuelle avec le domaine `CST`). | **`DEF`** (léger sacrifice de précision : la phase englobe aussi la réponse du demandeur). |
| D-14 | `ADM` et `HOR` **retirés du devis client** via `facturable = false` par défaut, **conservés dans le dataset**. | `ADM` refondu dans un taux général au devis ; ou `ADM` entièrement masqué. |
| D-15 | Sous-codes **livrés mais facultatifs** : le `-00` suffit à déployer ; on active les sous-codes phase par phase. | Rendre `sous_phase` requis dès le départ. |
| D-16 | Noms de champs **`phase`** / **`sous_phase`**. | `code_phase` / `sous_code`, ou tout autre couple cohérent avec la convention maison. |
| D-17 | Module **`utils/phases.py`** distinct de `taxonomie.py`. | Ajout des classes `Phase`/`SousCode` **dans** `taxonomie.py`. |
| D-18 | Paramètre `phase` **ajouté maintenant** aux trois outils d'écriture MCP. | Différé à une phase ultérieure (l'axe 1 reste alors saisi seulement à l'écran). |
| D-19 | `AUD-01` / `AUD-02` (préparation / audience) **conservés** comme deux enveloppes de facturation distinctes (préparation à l'heure, jours d'audience souvent au forfait). C'est la **seule** exception au principe « pas de découpage par nature de travail ». | Fusion en un `AUD` unique. |
| D-20 | Distinction `PRL`/`PRV`/`INC` maintenue (moyens préliminaires / mesures provisionnelles / incidents). | Regroupement, si jugé trop fin pour la pratique. |

---

## 10. Conséquences non nommées (guidage prospectif)

Points qui débordent la présente phase mais qu'elle rend possibles, et qu'il faut connaître pour ne pas mal câbler ce qui suit.

**Jointure protocole ↔ budget ↔ temps.** C'est l'implication décisive de D-5. Une fois chaque étape de gabarit annotée d'un `phase`, un même écran peut afficher, par phase : l'**échéance** (du protocole), l'**enveloppe** (du budget), la **consommation** (des entrées de temps et de frais). Cette même jointure rend calculable le **défaut de phase à la saisie** : la phase suggérée d'une nouvelle entrée de temps se déduit de la position courante dans le protocole du dossier. Prévoir le sélecteur en conséquence — bande de « codes récents » en tête, liste complète au deuxième niveau filtrée par phase — car 18 phases et ~9 activités sont à la limite de ce qui se saisit au pouce, et 8 à 10 combinaisons couvriront 80 % des entrées.

**Deux quantités distinctes : heures et durée calendaire.** Le tronc est **ordonné** (`categorie == "tronc"`, champ `ordre`), les modules ne le sont pas. On ne calcule une **durée calendaire phase-à-phase que sur le tronc ordonné** ; mélanger les modules fausserait tout intervalle. Les deux mesures — heures consommées et jours écoulés — sont l'une et l'autre utiles pour promettre un échéancier au client, et à ne pas confondre.

**Le champ FRAIS de l'estimateur exige `expenses`.** Sans code de phase sur `expenses`, l'appariement au budget ne couvre que la moitié de l'estimation (la colonne FRAIS du classeur n'a aucun répondant). D'où D-5. Rappel corollaire : dans le classeur actuel, l'honoraire de l'expert est facturé au taux horaire de l'avocat — c'est un **déboursé**, pas un honoraire ; la refonte de l'estimateur (sequel) devra le corriger.

**Versionnement des budgets (sequel, mais à cadrer maintenant).** La jauge de dépassement présuppose un budget **horodaté et jamais écrasé**. Le motif est déontologique autant que pratique : l'obligation d'informer le client du coût prévisible et de tout élément susceptible de le faire varier suppose de pouvoir établir **quand** l'information a été donnée. Un budget écrasé détruit cette preuve. Le seuil d'alerte (par ex. 80 % d'une phase consommée) n'est donc pas un gadget : c'est le **déclencheur** de cette obligation.

**Avertissement statistique (sequel analytique).** En pratique solo, le nombre d'observations par cellule (phase × action × classe de valeur × rôle) restera très faible pendant des années. Une médiane sur trois dossiers ne vaut rien — et sera d'autant plus crédible qu'elle sortira d'un système que le praticien a lui-même bâti. Lorsque l'analytique viendra : **ne jamais afficher une statistique sans son *n*** ; **médiane et intervalle interquartile, jamais la moyenne** (distribution fortement asymétrique à droite) ; et prévoir les covariables de découpage déjà détenues — `action`, `valeur_classe`, `role`, `forum_type`, `district_judiciaire` — **plus une qui manque et qui est probablement la plus explicative : le dossier a-t-il été réellement contesté**. Un `REC-01` contesté et un `REC-01` non contesté n'appartiennent pas à la même population.

**Retrait du classeur Excel (sequel).** À terme, l'estimation ne devrait plus être un fichier : l'application connaît déjà l'action, la valeur, la classe, le forum et le rôle, et peut générer le devis à partir de coefficients stockés (d'abord *a priori*, ensuite les médianes observées), livré au client via le moteur de gabarits ou ReportLab. La présente taxonomie est le vocabulaire commun de cet estimateur futur.

---

## Annexe A — Taxonomie de référence (source de vérité)

Codes ASCII ; libellés accentués. Chaque phase reçoit en outre, par la convention D-9, un `-00` « Général » et un `-99` « Autre (préciser) » **non listés ci-dessous** (sauf `HOR`, qui ne reçoit que `-00`).

### A.1 — Phases (axe 1)

| Code | Libellé | Catégorie | Ordre | `facturable_defaut` |
|---|---|---|:--:|:--:|
| `ADM` | Administration du dossier | transversal | — | **false** |
| `PRE` | Préjudiciaire | tronc | 1 | true |
| `PRD` | Prévention et règlement | tronc | 2 | true |
| `INT` | Introduction de l'instance | tronc | 3 | true |
| `CTS` | Contestation | tronc | 4 | true |
| `INR` | Interrogatoires et engagements | tronc | 5 | true |
| `MEE` | Mise en état et gestion | tronc | 6 | true |
| `INS` | Inscription | tronc | 7 | true |
| `AUD` | Instruction | tronc | 8 | true |
| `JUG` | Jugement et suites | tronc | 9 | true |
| `PRL` | Moyens préliminaires | module | — | true |
| `PRV` | Mesures provisionnelles | module | — | true |
| `INC` | Demandes en cours d'instance | module | — | true |
| `EXP` | Expertise | module | — | true |
| `EXE` | Exécution | module | — | true |
| `APP` | Appel | module | — | true |
| `CJU` | Contrôle judiciaire | module | — | true |
| `HOR` | Hors phase | residuel | — | **false** |

### A.2 — Sous-codes, tronc commun

| Phase | Sous-code | Libellé |
|---|---|---|
| `PRE` | `PRE-01` | Consultation initiale |
| `PRE` | `PRE-02` | Étude du dossier |
| `PRE` | `PRE-03` | Recherche et avis juridique |
| `PRE` | `PRE-04` | Mise en demeure et avis préalables |
| `PRD` | `PRD-01` | Négociation et pourparlers |
| `PRD` | `PRD-02` | Médiation |
| `PRD` | `PRD-03` | Conférence de règlement à l'amiable |
| `PRD` | `PRD-04` | Transaction et quittance |
| `INT` | `INT-01` | Demande introductive d'instance |
| `INT` | `INT-02` | Signification et dépôt |
| `INT` | `INT-03` | Protocole de l'instance |
| `CTS` | `CTS-01` | Défense (écrite ou orale) |
| `CTS` | `CTS-02` | Demande reconventionnelle |
| `CTS` | `CTS-03` | Réponse et défense reconventionnelle |
| `INR` | `INR-01` | Interrogatoire de la partie adverse |
| `INR` | `INR-02` | Interrogatoire du client ou d'un témoin |
| `INR` | `INR-03` | Engagements et suivis |
| `MEE` | `MEE-01` | Communication de la preuve et des pièces |
| `MEE` | `MEE-02` | Correspondance avec la partie adverse |
| `MEE` | `MEE-03` | Gestion de l'instance et prolongation des délais |
| `INS` | `INS-01` | Déclaration de mise en état et inscription |
| `INS` | `INS-02` | Appel du rôle provisoire et fixation |
| `AUD` | `AUD-01` | Préparation de l'instruction |
| `AUD` | `AUD-02` | Audience |
| `JUG` | `JUG-01` | Analyse du jugement et rapport au client |
| `JUG` | `JUG-02` | Frais de justice et mémoire de frais |
| `JUG` | `JUG-03` | Rectification et rétractation |

### A.3 — Sous-codes, modules conditionnels

| Phase | Sous-code | Libellé |
|---|---|---|
| `PRL` | `PRL-01` | Moyen déclinatoire |
| `PRL` | `PRL-02` | Moyen d'irrecevabilité |
| `PRL` | `PRL-03` | Autre moyen préliminaire |
| `PRV` | `PRV-01` | Injonction interlocutoire |
| `PRV` | `PRV-02` | Saisie avant jugement |
| `PRV` | `PRV-03` | Ordonnance de sauvegarde |
| `INC` | `INC-01` | Demande en cours d'instance (incident) |
| `INC` | `INC-02` | Mise en cause, intervention ou appel en garantie |
| `INC` | `INC-03` | Modification d'un acte de procédure |
| `EXP` | `EXP-01` | Sélection et mandat de l'expert |
| `EXP` | `EXP-02` | Rapport d'expertise et suivi |
| `EXP` | `EXP-03` | Contre-expertise |
| `EXP` | `EXP-04` | Expert à l'instruction |
| `EXE` | `EXE-01` | Formalités postérieures au jugement |
| `EXE` | `EXE-02` | Mesures d'exécution |
| `APP` | `APP-01` | Permission ou déclaration d'appel |
| `APP` | `APP-02` | Mémoire d'appel |
| `APP` | `APP-03` | Audition en appel |
| `CJU` | `CJU-01` | Pourvoi en contrôle judiciaire |
| `CJU` | `CJU-02` | Mémoire en contrôle judiciaire |
| `CJU` | `CJU-03` | Audition en contrôle judiciaire |

### A.4 — Sous-codes, transversal et résiduel

| Phase | Sous-code | Libellé |
|---|---|---|
| `ADM` | `ADM-01` | Ouverture, vérification d'identité et de conflits |
| `ADM` | `ADM-02` | Mandat et convention d'honoraires |
| `ADM` | `ADM-03` | Rapports et gestion de la relation client |
| `ADM` | `ADM-04` | Facturation et suivi des comptes |
| `ADM` | `ADM-05` | Fermeture et conservation du dossier |
| `HOR` | `HOR-00` | Hors phase (résiduel — aucune ventilation) |

---

## Annexe B — Constantes dérivées à exposer dans `vocab.py`

Toutes **calculées** depuis `PHASES` (ne rien saisir en dur en double) :

- `VALID_PHASE_CODES` — l'ensemble des codes de phase.
- `PHASE_LABELS` — code → libellé.
- `VALID_SOUS_PHASE_CODES` — l'ensemble des sous-codes (y compris les `-00` / `-99` synthétisés).
- `SOUS_PHASE_LABELS` — sous-code → libellé.
- `PHASES_NON_FACTURABLES` — phases à `facturable_defaut = false` (`ADM`, `HOR`).
- `TRONC_ORDONNE` — la séquence du tronc, triée par `ordre` (pour l'analytique de durée calendaire).

Le test de conformité (§7) vérifie que ces constantes restent synchrones avec les énumérations des schémas MCP, et l'invariant de préfixe `sous_phase` ⊂ `phase`.
