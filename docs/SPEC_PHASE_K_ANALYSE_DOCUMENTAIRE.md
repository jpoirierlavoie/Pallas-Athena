# Spécification — Phase K : Analyse documentaire assistée (Claude sur Agent Platform)

**Destinataire :** Claude Code
**Cible :** dépôt `jpoirierlavoie/Athena-Pallas`, branche `main`
**Statut :** projet à réviser — arbitrage de la section 17 requis avant implémentation
**Révision :** v3 — ajout de **l'étiquette de privilège** (§6), **scission de `procès_verbal`** (§5.2), sémantique de la pièce confirmée (§5.6). Remplace intégralement la v2.

> **Lettre de phase.** « K » est provisoire ; confirmer contre le tableau *Phase History* de `CLAUDE.md`.

---

## 0. Résumé exécutif

Analyse documentaire à la demande, exécutée par **Claude sur la plateforme d'agents de Google Cloud** dans le projet `athena-pallas`, en **trois temps ordonnés** :

1. **Classer la nature du document** — acte de procédure, jugement, procès-verbal de signification, procès-verbal d'audience, transcription, correspondance, preuve, pièce, document du cabinet.
2. **N'extraire que les métadonnées que cette nature commande** — chaque nature judiciaire a sa propre liste légale de mentions obligatoires (art. 99, 105, 119, 389 C.p.c.), et ces listes diffèrent.
3. **Déterminer le régime de protection** — secret professionnel, privilèges, confidentialité, ou caractère public.

**L'ordre fait la valeur.** Demander à un modèle le numéro de dossier de cour d'un courriel versé en pièce, c'est l'inviter à en fabriquer un. Lui demander d'abord *ce qu'est* le document transforme l'absence d'un champ en **signal** : un acte de procédure sans numéro de dossier est probablement un projet non déposé — donc, précisément, un document **non public et couvert par le privilège relatif au litige**. Les trois étages s'alimentent (§6.4).

**Deux déclencheurs explicites** : bouton dans l'application, outil MCP. Aucun déclenchement automatique au téléversement.

**Quatre contraintes non négociables :** l'analyse est une hypothèse que l'avocat confirme, jamais une qualification arrêtée (§7) ; **le régime de protection échoue toujours vers le haut** (§6.3) ; rien n'est supprimé (§8.2) ; aucun contenu de document ne transite par les journaux (§12).

---

## 1. Ce qui change par rapport à la v2

| Axe | v2 | v3 |
|---|---|---|
| **Régime de protection** | Absent | **Étiquette de privilège cumulable + niveau dérivé** (§6) |
| **`procès_verbal`** | Une seule catégorie | **Scindée** : signification / audience — champs attendus entièrement différents (§5.2) |
| **PV d'audience** | Simple procès-verbal | **Peut contenir le jugement** (chambre de pratique) — extraction du dispositif (§5.4) |
| **`pièce` / `preuve`** | Frontière à confirmer | **Confirmée** : la pièce est cotée, donc publique et non privilégiée (§5.6) |
| **Vocabulaire `category`** | Réutilisé tel quel | **Élargi** — travail préalable de migration (§5.3) |
| **Renonciation au privilège** | — | **Détection d'une divulgation possiblement par inadvertance** (§6.5) |

Inchangé : fournisseur, déclencheurs, types de fichiers, sortie par outil forcé, journal append-only, garde-fou de confirmation, décisions de région et de journalisation.

---

## 2. Architecture

```
┌──────────────────────────┐        ┌───────────────────────────────┐
│  Application Flask       │        │  Connecteur MCP (mcp/)        │
│  bouton « Analyser »     │        │  outil analyser_document      │
└───────────┬──────────────┘        └───────────┬───────────────────┘
             └──────────────┬─────────────────────┘
                             ▼
                 services/analyse_queue.py  (Cloud Task, jeton OIDC)
                             ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  Cloud Function 2e gén. (Python 3.13)                        │
   │  functions/analyse_documentaire/                             │
   │                                                               │
   │   1. lire documents/{id} + le dossier parent (domaine, §6.4) │
   │   2. renifler le type réel (_sniff_content_type)              │
   │   3. préparer le contenu selon le type (§4.2)                 │
   │   4. APPEL UNIQUE — outil forcé, schéma ordonné :             │
   │        a) nature + sous_nature                                │
   │        b) champs conditionnés par (a)                         │
   │        c) indices de protection observés                      │
   │   5. dériver EN CODE : famille, champs_attendus_absents,      │
   │                        niveau_protection, alertes             │
   │   6. valider contre les taxonomies fermées                    │
   │   7. écrire documents/{id}.analyse                            │
   │      + documents/{id}/analyses/{analyseId}   (append-only)    │
   └───────────┬─────────────────────────────┬───────────────────┘
                ▼                              ▼
       ┌────────────────┐            ┌──────────────────────┐
       │ Cloud Storage  │            │ Firestore            │
       └────────────────┘            └──────────────────────┘
```

**Un seul appel.** Le schéma place `nature` et `sous_nature` en tête ; l'invite ordonne de les arrêter avant tout le reste. Si les tests (§14) révèlent une contamination — extraction de champs incompatibles avec la nature annoncée —, la bascule vers deux appels est la remédiation prévue.

**Le partage des rôles est net : le modèle observe, le code qualifie.** `famille`, `champs_attendus_absents`, `niveau_protection` et les alertes sont **calculés en Python** depuis les tables des annexes. Ce sont des règles de droit déterministes ; elles n'ont rien à faire dans le jugement d'un modèle.

**Nouveauté v3 : la fonction lit aussi le dossier parent.** Le `domaine` du dossier conditionne le caractère public (art. 16 C.p.c. — accès restreint en matière familiale, §6.4). Le compte de service doit donc pouvoir lire `dossiers/{id}`, en lecture seule.

**Arborescence :**

```
functions/analyse_documentaire/
├── main.py                 # point d'entrée HTTP (Cloud Tasks)
├── analyseur.py            # appel, validation, dérivations
├── extraction.py           # DOCX + XLSX, bibliothèque standard
├── claude_client.py        # client AnthropicVertex, outil forcé
├── schema.py               # schéma de l'outil + validation
├── taxonomies.py           # Annexes A à D
├── protection.py           # dérivation du niveau + alertes (§6)
├── journalisation.py       # filtre de rédaction
├── requirements.txt
└── tests/
```

**Pourquoi une fonction séparée.** Contrainte permanente « aucune nouvelle dépendance Python dans le monolithe » : `anthropic[vertex]` élargirait `requirements.in` et l'installation verrouillée par empreintes d'App Engine. Accessoirement, une analyse multipages frôle ou excède le délai de 60 s d'App Engine Standard.

---

## 3. Le fournisseur : Claude sur Agent Platform

### 3.1 Région — préférence forte, non bloquante

| Rang | Cible | Conséquence |
|---|---|---|
| 1 | `northamerica-northeast1` (Montréal) | Traitement en province ; analyse de transfert sans objet. |
| 2 | Point de terminaison **multirégional** couvrant le Canada, s'il existe | Aire définie, pas nécessairement le Québec. Prime d'environ 10 % sur le point global. |
| 3 | Global ou É.-U. | **Déclenche l'analyse de transfert hors Québec (art. 17 Loi 25)** — à consigner. |

Région en **variable d'environnement explicite**, jamais un défaut implicite du SDK ; **journalisée à chaque analyse et stockée dans l'enregistrement** (`region`, §8.1). Si le repli au rang 3 est utilisé, il doit être possible d'établir, document par document, où chaque pièce a été traitée.

> Vérifier au Model Garden au déploiement : la disponibilité des modèles Claude varie selon la région, et la présence de Gemini à Montréal n'emporte rien pour Claude.

### 3.2 Client et SDK

Client `AnthropicVertex` (`anthropic[vertex]`), identifiants par défaut de l'application, `project_id` et `region` explicites. Modèle en variable d'environnement — les identifiants portent des dates de retrait.

> Confirmer nom de paquet, constructeur et identifiants contre la documentation vivante avant d'épingler. Cette spécification n'en fixe aucun.

### 3.3 Sortie structurée — outil forcé

Claude n'a pas d'équivalent au `response_schema` de Gemini : déclarer un outil dont le schéma d'entrée est la structure voulue, imposer son emploi par `tool_choice`, lire le bloc `tool_use`.

- **Filtrer `content` par `type`**, jamais par position.
- Le schéma est une contrainte forte, **non une garantie** : valider, et écrire `statut: "echec"` plutôt qu'un enregistrement partiel.

### 3.4 Deux plafonds, dont un piège arithmétique

Charge utile Agent Platform : **30 Mo**. Téléversement Athéna : 25 Mo. **L'encodage base64 gonfle d'un tiers** — un fichier de 25 Mo pèse près de 33 Mo encodé, donc au-delà du plafond, avant l'invite et le schéma. Un fichier parfaitement téléversable fera échouer l'analyse.

Appliquer un plafond propre **calculé sur la taille encodée** (marge franche, de l'ordre de 20 Mo de source, configurable), refuser en amont avec un message français, ne facturer aucun appel. Vérifier séparément les limites propres au traitement des PDF.

### 3.5 Journalisation requête-réponse — activée

Décision prise : activée, fenêtre glissante de 30 jours.

Trois conséquences à assumer : il existera dans `athena-pallas` **une seconde copie du contenu intégral des documents analysés**, dont des pièces couvertes par le secret professionnel ; ce magasin doit figurer **à l'inventaire des supports** de l'ÉFVP avec sa propre politique de conservation (vérifier la fenêtre après activation) ; il doit être couvert par les mêmes protections que le reste — une copie de documents privilégiés moins bien protégée que l'original est une régression silencieuse.

---

## 4. Types de fichiers, extraction et reconnaissance

### 4.1 Matrice de traitement

| Type | MIME | Voie | Reconnaissance du contenu non structuré |
|---|---|---|---|
| PDF | `application/pdf` | Bloc `document` natif | **Oui** — pages numérisées lues dans le même appel |
| JPEG | `image/jpeg` | Bloc `image` natif | **Oui** |
| PNG | `image/png` | Bloc `image` natif | **Oui** |
| DOCX | `…wordprocessingml.document` | Extraction texte (bibliothèque standard) → bloc `text` | Sans objet |
| XLSX | `…spreadsheetml.sheet` | Extraction texte (bibliothèque standard) → bloc `text` | Sans objet |
| autre | — | `statut: "non_applicable"` | — |

**TIFF est retiré** — Claude accepte JPEG, PNG, GIF, WebP. Refus explicite en v1 avec message clair ; la conversion exigerait une dépendance d'imagerie (§18).

**XLSX doit probablement être ajouté à `ALLOWED_MIME_TYPES`** — vérifier `models/document.py`. Prérequis, pas effet de bord.

### 4.2 Extraction DOCX et XLSX — bibliothèque standard uniquement

`zipfile` + `re`, selon la doctrine de `utils/docx_fill.py` : les bibliothèques tierces corrompent les paquets OOXML à en-têtes multiples ou polices incorporées.

**DOCX** — lire `word/document.xml`, retirer le balisage, joindre les paragraphes. La phase H écrit, celle-ci lit : même technique, **jamais le même module**.

**XLSX** — les chaînes sont mutualisées dans `xl/sharedStrings.xml` et référencées par index ; sans résolution des renvois, on n'extrait que des nombres. Sérialiser en **Markdown tabulaire, une table par feuille, en-têtes conservés**. **Une formule n'est pas une valeur** : privilégier la valeur en cache. Plafonner le nombre de cellules et **le déclarer** (`extraction_tronquee: true`) — un tableau financier tronqué sans mention est pire qu'une absence d'analyse.

### 4.3 Pas d'OCR distinct en v1

Claude lit les PDF numérisés et les photographies dans l'appel principal. `qualite_reconnaissance` sert à **mesurer** le besoin : si les mentions `faible` s'accumulent, l'escalade Document AI se justifiera sur des données plutôt que sur une intuition.

---

## 5. Classification — natures et familles

### 5.1 Trois axes, deux produits par le modèle, un dérivé en code

| Axe | Produit par | Rôle |
|---|---|---|
| `nature` | **Modèle** | Catégorie principale, alignée sur le vocabulaire `category` **élargi** |
| `sous_nature` | **Modèle** | Raffinement, code fermé (Annexe A) |
| `famille` | **Code** | Regroupement dérivé, qui commande l'extraction |

Motif à deux étages repris du phasage (`phase` / `sous_phase`) : cohérence interne, et un raffinement absent retombe sur sa catégorie parente.

### 5.2 Élargissement du vocabulaire `category` — travail préalable

Le vocabulaire actuel — `procédure`, `pièce`, `jugement`, `correspondance`, `déboursé`, `facture`, `preuve`, `procès_verbal`, `transcription`, `mandat`, `autre` — confond deux documents qui n'ont **rien en commun** :

| | Procès-verbal de signification | Procès-verbal d'audience |
|---|---|---|
| Auteur | Huissier, sous serment professionnel | Greffier |
| Mentions obligatoires | **art. 119 C.p.c.** — liste fermée | Aucune disposition repérée |
| Fonction | Preuve de la notification (art. 107 al. 3 C.p.c.) | Consignation du déroulement |
| Peut contenir un jugement | Non | **Oui** (§5.4) |
| Régime de protection | Public | Public |

Les champs attendus sont entièrement disjoints ; les garder sous un même code rend la matrice de l'Annexe B inapplicable.

**Modification demandée :**

| Retiré | Remplacé par |
|---|---|
| `procès_verbal` | `proces_verbal_signification` · `proces_verbal_audience` |

> **Codes ASCII sans diacritiques pour les nouvelles valeurs.** Le vocabulaire existant en porte (`procédure`, `pièce`), mais la discipline retenue ailleurs — phasage, iCalendar — est l'ASCII. Uniformiser tout le vocabulaire serait une migration bien plus lourde ; introduire les nouveaux codes en ASCII et laisser les anciens tels quels est le compromis proposé. **À arbitrer** (§17), car un vocabulaire mixte est une dette.

**Migration — à traiter avec soin, dans un ordre imposé :**

1. Ajouter les deux nouvelles valeurs **sans retirer** `procès_verbal`.
2. Retirer `procès_verbal` du **sélecteur de saisie** uniquement : plus offert à la création, toujours lisible et filtrable.
3. Reclasser les documents existants **par un geste humain**, éventuellement assisté par l'analyse elle-même — jamais par un lot automatique. Le principe « aucune capacité de suppression » interdit d'écraser une classification faite par l'avocat.
4. `procès_verbal` demeure une valeur **héritée** tant qu'un document la porte. Ne pas la supprimer du modèle.

Portée du changement à vérifier au code vivant : `models/document.py`, formulaire de téléversement, filtres de la feuille « Fichiers », énumération de l'outil MCP `list_documents`, et tout gabarit affichant le libellé.

> **Lire `models/document.py` avant d'écrire `taxonomies.py`.** La liste ci-dessus provient de la description de l'outil MCP ; c'est la liste du modèle qui fait foi.

### 5.3 `category` n'est jamais écrasé

Le modèle écrit `nature_detectee`, **jamais** `category`. En cas de divergence : `divergence_categorie: true`, signalée dans l'interface, et une **route dédiée** permet d'adopter la nature détectée. Sans cette règle, une analyse en série réorganiserait le dossier en silence.

Une divergence n'est d'ailleurs pas nécessairement une erreur du modèle : c'est aussi ainsi qu'on retrouve un jugement classé par mégarde en correspondance.

### 5.4 Le procès-verbal d'audience peut contenir le jugement

En chambre de pratique, le jugement n'existe souvent que dans le procès-verbal d'audience. Le traiter comme un simple compte rendu ferait manquer le dispositif lui-même.

**Deux champs :** `contient_dispositif: bool | None` et `dispositif: str | None`.

**Trois conséquences :**

- Quand `contient_dispositif` est vrai, la `sous_nature` reste `PV_AUDIENCE` mais **la matrice des champs attendus bascule sur celle du jugement** (Annexe B) — on attend alors le tribunal, les parties, la date et le nom du juge.
- La portée dépasse le classement. **Un jugement rendu à l'audience fait courir les délais d'appel.** Identifier qu'un procès-verbal en contient un est un fait opérationnel, pas documentaire — et c'est probablement le rattachement le plus utile à envisager plus tard vers le module d'échéances (§18).
- `alerte_dispositif_detecte` remonte à l'interface pour que l'avocat vérifie. La spécification **ne calcule aucun délai** à partir de cette détection : ce serait fonder un délai de rigueur sur une lecture automatique, ce que rien ici ne justifie.

### 5.5 Le sous-axe procédural

Le code de type de procédure (`PROC_DEM_INTRO`, `PROC_PROTOCOLE`, `PROC_DEFENSE`, `PROC_DECL_APPEL`…) est la `sous_nature` de la seule nature `procédure`. Il n'est plus demandé ailleurs — un courriel n'a pas de type procédural. Annexe A.

`MISE_DEMEURE` relève de l'art. 1595 C.c.Q. et est un **acte extrajudiciaire** : classé sous `correspondance`, ce qui est juridiquement exact et pratiquement commode, la mise en demeure étant de fait une lettre.

### 5.6 Pièce et preuve — frontière confirmée

**Sémantique arrêtée :** la **pièce** est une preuve **cotée** — notifiée, déposée, citée dans un acte de procédure. Elle devient de ce fait **publique et non privilégiée**. La **preuve** non cotée conserve le régime de protection qui lui est propre.

Cette distinction n'est donc pas seulement documentaire : **elle est le pivot du régime de protection** (§6.5). Le passage de `preuve` à `pièce` est un changement d'état qui emporte perte du privilège — ce qui en fait l'endroit exact où une renonciation par inadvertance peut se produire.

---

## 6. Le régime de protection — nouveauté v3

### 6.1 Deux champs, dont un dérivé

| Champ | Produit par | Rôle |
|---|---|---|
| `privileges` | **Modèle** | Liste de codes observés — **cumulable** |
| `niveau_protection` | **Code** | Ordinal dérivé du plus élevé des codes retenus |

**La liste est cumulable parce que les régimes se superposent réellement.** Un mémorandum de l'avocat au client préparant l'instruction est à la fois couvert par le secret professionnel et par le privilège relatif au litige ; une offre de règlement transmise au client relève du privilège relatif aux règlements et du secret professionnel. Forcer un choix unique appauvrirait l'étiquette et masquerait le régime le plus protecteur.

### 6.2 Taxonomie et ancrages

Table complète à l'**Annexe D**. Quatre niveaux :

| Niveau | Code | Fondement |
|:--:|---|---|
| 3 | `SECRET_PROFESSIONNEL` | art. 9 Charte (c-12) ; art. 60.4 Code des professions ; **art. 2858 al. 2 C.c.Q.** |
| 2 | `LITIGE`, `REGLEMENT`, `INTERET_COMMUN`, `ENQUETE_INTERNE`, `INFORMATEUR`, `SECRET_COMMERCIAL` | mixte — voir Annexe D |
| 1 | `CONFIDENTIEL` | défaut résiduel |
| 0 | `PUBLIC` | art. 11 C.p.c. |

**Pourquoi le secret professionnel occupe seul le niveau 3.** Ce n'est pas une hiérarchie de confort mais une différence de régime, vérifiée au texte. L'art. 9 al. 3 de la Charte impose au tribunal d'assurer **d'office** le respect du secret professionnel. Surtout, l'art. 2858 C.c.Q. commande le rejet de la preuve obtenue en violation des droits fondamentaux **lorsque son utilisation est susceptible de déconsidérer l'administration de la justice** — mais son alinéa 2 écarte ce second critère « lorsqu'il s'agit d'une violation du droit au respect du secret professionnel ». Le rejet y est donc automatique, là où il reste conditionnel ailleurs. Aucun autre régime de la liste ne bénéficie de cela.

**Trois réserves à porter dans le code et dans l'interface :**

- **`SECRET_COMMERCIAL` n'est pas un privilège de non-divulgation.** Les art. 1472 et 1612 C.c.Q. fondent une responsabilité et une indemnisation, non une immunité de production. La protection en instance passe par une ordonnance de confidentialité (art. 12 C.p.c.), pas par le secret lui-même. Le placer au niveau 2 est un choix **pragmatique de manipulation**, non un énoncé de droit — l'interface doit le dire.
- **`INFORMATEUR` est d'abord un régime pénal**, d'application civile étroite. À retenir avec parcimonie.
- **`ENQUETE_INTERNE` est le plus souvent une espèce du privilège relatif au litige**, non un régime autonome. Conservé comme code distinct pour sa commodité pratique, sans prétendre à l'autonomie.

### 6.3 Le principe cardinal : échouer vers le haut

**C'est le champ le plus dangereux du système.** Une erreur vers le bas — marquer publique une pièce privilégiée — peut mener à une divulgation par inadvertance, c'est-à-dire à un manquement professionnel sous l'art. 9 de la Charte et l'art. 60.4 du Code des professions. Une erreur vers le haut est une gêne. **Les deux erreurs ne se valent pas, et le système doit être asymétrique en conséquence.**

Quatre règles :

1. **En cas de doute, retenir le régime le plus protecteur plausible**, jamais le moins. L'invite doit le poser explicitement.
2. **Aucun déclassement automatique.** Une réanalyse qui abaisse le niveau **ne l'écrit pas** : elle inscrit `divergence_protection: true` et laisse l'ancienne valeur en place jusqu'à décision humaine.
3. **L'étiquette n'autorise rien.** Elle aide au triage ; la décision de communiquer un document reste celle de l'avocat, prise en lisant le document. Aucun automatisme ne doit jamais s'appuyer sur cette valeur pour transmettre quoi que ce soit.
4. **Le module échoue fermé.** En cas d'échec d'analyse, l'absence d'étiquette **ne vaut pas `PUBLIC`** : elle vaut « non déterminé », affiché comme tel. C'est la même posture que le module de fidéicommis.

### 6.4 Ce que la nature et le dossier apportent déjà

Le régime se déduit largement des deux étages précédents, ce qui réduit la part laissée au jugement du modèle :

| Situation | Régime par défaut | Motif |
|---|---|---|
| `procédure` **déposée** (n° de dossier présent) | `PUBLIC` | art. 11 C.p.c. |
| `procédure` **sans n° de dossier** | `LITIGE` | Probable projet non déposé — donc non public |
| `jugement`, `PV_SIGNIFICATION`, `PV_AUDIENCE` | `PUBLIC` | art. 11 C.p.c. |
| `pièce` (cotée) | `PUBLIC` | §5.6 |
| `preuve` non cotée | selon la source | — |
| `CORR_CLIENT` | `SECRET_PROFESSIONNEL` | art. 9 Charte |
| `CORR_CONFRERE` portant « sous toutes réserves » | `REGLEMENT` | art. 4 C.p.c. ; Union Carbide |
| Communication avec un expert | `LITIGE` | Lizotte c. Aviva |
| `CAB_MANDAT`, `CAB_FACTURE` | `SECRET_PROFESSIONNEL` | Relation professionnelle |
| `CAB_MEMO` | `LITIGE` + `SECRET_PROFESSIONNEL` | Travail préparatoire |
| Défaut résiduel | `CONFIDENTIEL` | — |

**Deux interactions découvertes en rédigeant, qui valent d'être soulignées.**

La première : **le mécanisme de détection des projets alimente le régime de protection.** `champs_attendus_absents` contenant `numero_dossier_cour` sur une nature `procédure` signale un acte non déposé — donc un document qui n'a jamais accédé au caractère public de l'art. 11 C.p.c., et qui relève du travail préparatoire. Le dispositif introduit en v2 pour diagnostiquer le classement sert ici à protéger. Les deux étages ne sont pas juxtaposés : ils se renforcent.

La seconde : **`PUBLIC` n'est pas automatique.** L'art. 11 al. 2 C.p.c. réserve les cas où la loi restreint l'accès, et l'art. 16 C.p.c. restreint l'accès aux dossiers **en matière familiale** ainsi qu'à divers documents de santé ou psychosociaux déposés sous pli cacheté. En matière familiale, un acte de procédure déposé **n'est pas public** au sens ordinaire. D'où l'exigence, en §2, que la fonction lise le `domaine` du dossier parent : si le domaine est familial, `PUBLIC` est rabattu sur `CONFIDENTIEL`, en code et non par le modèle.

**Limite à assumer :** une ordonnance de confidentialité rendue sous l'art. 12 C.p.c. est invisible depuis le document. Le modèle ne peut pas la connaître, le code non plus. L'étiquette ne peut donc jamais être tenue pour exhaustive — ce qui est une raison de plus pour la règle 3 du §6.3.

### 6.5 Détection d'une renonciation possiblement par inadvertance

Puisque la cotation rend la pièce publique (§5.6), un document classé `pièce` qui présente par ailleurs les marques d'un régime protégé — en-tête d'avocat, mention « sous toutes réserves », correspondance avec le client, rapport d'expert non communiqué — signale l'une de deux choses : une erreur de classement, ou **une renonciation au privilège dont l'avocat devrait être averti**.

`alerte_renonciation_possible: true` remonte alors à l'interface, avec les indices observés.

C'est probablement la fonction à plus forte valeur de toute la phase, et elle ne coûte rien de plus : elle naît de la rencontre entre la sémantique de la pièce et l'étiquette de privilège. Elle **signale** ; elle ne conclut pas, et surtout ne modifie aucune classification.

---

## 7. Le garde-fou déontologique

Qualifier un document d'« acte authentique » ou de « public » n'est pas une étiquette mais une **qualification à conséquences** : l'acte authentique bénéficie d'une présomption d'authenticité (art. 2813 al. 2 C.c.Q.) ; le caractère public commande ce qui peut être transmis. Une supposition de modèle, écrite en base puis ressortie par le connecteur MCP, se présenterait avec la même autorité apparente qu'une détermination de l'avocat.

**Trois exigences, non négociables :**

1. **Tout champ de qualification porte `confirme: false` par défaut** — nature, preuve **et privilège**. Seul un geste explicite le passe à `true`, horodaté. Aucun chemin automatique.
2. **La sortie MCP marque l'état.** Une valeur non confirmée sort avec sa mention d'incertitude, jamais nue.
3. **L'interface distingue le présumé du confirmé.** Pour les champs de qualification, l'écusson ne suffit pas : la mention accompagne la valeur.

**Corollaire sur les parties.** Les noms extraits restent des **chaînes libres**, jamais résolus vers un `partie_id`. Rattacher un nom au mauvais contact est plus grave que l'absence de rattachement, et se propagerait en silence.

---

## 8. Schéma Firestore

### 8.1 Champ courant sur `documents/{documentId}`

```python
{
    # … champs existants — `category` N'EST JAMAIS ÉCRASÉ …

    "analyse": {
        "statut": "en_attente" | "en_cours" | "prete" | "echec" | "non_applicable",

        # — Étage 1 : classification —
        "nature_detectee": str,              # vocabulaire `category` élargi — Annexe A
        "sous_nature": str,                  # code fermé — Annexe A
        "famille": str,                      # DÉRIVÉ — JUDICIAIRE | CORRESPONDANCE
                                             #          | PREUVE | CABINET | INDETERMINE
        "divergence_categorie": bool,

        # — Étage 2 : contenu —
        "resume": str,
        "langue_detectee": "fr" | "en" | "autre",
        "qualite_reconnaissance": "haute" | "moyenne" | "faible" | None,
        "extraction_tronquee": bool,

        # — Bloc art. 99 / 105 / 119 / 389 C.p.c. — famille JUDICIAIRE —
        "numero_dossier_cour": str | None,
        "tribunal": str | None,
        "district_judiciaire": str | None,
        "auteur": str | None,                # signataire, huissier, juge, greffier, sténographe
        "parties_mentionnees": list[str],    # chaînes libres — JAMAIS de partie_id
        "date_signature_str": str | None,    # AAAA-MM-JJ — cf. piège §13
        "date_document_str": str | None,     # AAAA-MM-JJ
        "contient_dispositif": bool | None,  # §5.4 — PV d'audience portant jugement
        "dispositif": str | None,

        # — Bloc preuve C.c.Q. — familles PREUVE et CORRESPONDANCE —
        "moyen_preuve": str,                 # Annexe C, ou NON_DETERMINE
        "qualification_ecrit": str | None,
        "parait_original": bool | None,      # apparence seule — art. 2860 C.c.Q.

        # — Étage 3 : régime de protection (§6) —
        "privileges": list[str],             # cumulable — Annexe D
        "niveau_protection": int,            # DÉRIVÉ — 0 public … 3 secret professionnel
        "indices_protection": list[str],     # ce que le modèle a observé, en clair
        "divergence_protection": bool,       # réanalyse abaissant le niveau — §6.3 règle 2

        # — Diagnostic et alertes —
        "champs_attendus_absents": list[str],       # DÉRIVÉ — §5.x
        "alerte_dispositif_detecte": bool,          # §5.4
        "alerte_renonciation_possible": bool,       # §6.5
        "confiance": "haute" | "moyenne" | "faible",

        # — Confirmation (§7) —
        "confirme": False,
        "confirme_par": str | None,
        "confirme_le": datetime | None,

        # — Provenance —
        "modele": str,
        "region": str,                       # §3.1
        "declenche_par": "application" | "mcp",
        "genere_le": datetime,               # UTC
        "analyse_id": str,
        "message_erreur": str | None,
    }
}
```

### 8.2 Journal des analyses — sous-collection append-only

`documents/{documentId}/analyses/{analyseId}` : **une entrée par exécution**, jamais modifiée ni supprimée. `analyse` n'est qu'un cache de la dernière.

Ce n'est pas du zèle. Le principe « aucune capacité de suppression » vaut partout, et une analyse relancée après changement d'invite ou de modèle produira des classifications différentes. Savoir **quelle version a dit quoi, quand, avec quel modèle et dans quelle région** est la condition d'auditabilité. C'est aussi ce qui rend applicable la règle de non-déclassement du §6.3 : sans historique, on ne peut pas constater qu'un niveau a baissé.

Chaque entrée porte au minimum : `genere_le`, `modele`, `region`, `declenche_par`, la sortie complète, le motif d'échec le cas échéant.

### 8.3 Index et synchronisation

- **Aucun index composite** pour le fonctionnement de base : `set()` sur un `documentId` connu.
- Une recherche transversale ultérieure — « tous les documents au niveau 3 », « tous les actes de procédure sans numéro de dossier » — exigerait un index. **Ne pas le créer par anticipation.**
- **Aucun bump de CTag** : `documents` n'est pas exposée en DAV. Ne pas appeler `dav/sync.py`.
- `updated_at` et `etag` bumpés selon la règle d'architecture usuelle.

---

## 9. Déclencheurs

### 9.1 Application — routes Flask

| Route | Méthode | Rôle |
|---|---|---|
| `/documents/<id>/analyse` | POST | Enfile la tâche ; écrit `statut: "en_attente"` |
| `/documents/<id>/analyse` | GET | Fragment HTMX — état courant, pour la scrutation |
| `/documents/<id>/analyse/confirmer` | POST | **Seul** chemin passant `confirme` à `true` (§7) |
| `/documents/<id>/analyse/adopter-nature` | POST | **Seul** chemin écrivant `nature_detectee` dans `category` (§5.3) |
| `/documents/<id>/protection` | POST | **Seul** chemin abaissant `niveau_protection` (§6.3 règle 2) |

Interface : écusson d'état ; **niveau de protection affiché en tête**, avant la nature — c'est ce qui commande la manipulation du document ; nature détectée ensuite ; bandeau de divergence si `divergence_categorie` ou `divergence_protection` ; **`champs_attendus_absents`, `alerte_dispositif_detecte` et `alerte_renonciation_possible` affichés comme avertissements**, non enfouis dans un repli ; valeurs de qualification portant leur mention d'incertitude tant que `confirme` est faux ; scrutation `hx-trigger="every 3s"` tant que le statut est `en_attente` ou `en_cours`, selon le motif de squelette déjà en place.

### 9.2 Connecteur MCP

**Nouvel outil `analyser_document`**, scope `athena:write`.

**Asynchrone, et il doit le dire.** Un appel MCP ne peut attendre des dizaines de secondes : il enfile la tâche et retourne `statut: "en_attente"` et `analyse_id` ; le résultat se récupère par `list_documents`. La description de l'outil énonce ce contrat, faute de quoi l'appelant conclura à un échec devant une réponse vide.

**Extension de `list_documents`** : ajouter `nature_detectee`, `sous_nature`, `famille`, `resume`, `numero_dossier_cour`, `date_signature_str`, `champs_attendus_absents`, **`niveau_protection`**, **`privileges`**, `confirme`, `statut`. Aucun n'est une URL ni un chemin de stockage. Conserver le plafond de 50 éléments.

> **Le niveau de protection doit sortir par MCP, et c'est un gain, non un risque.** Le connecteur passe par l'infrastructure d'Anthropic ; l'analyse reste dans `athena-pallas`. Un agent qui sait qu'un document est au niveau 3 peut se comporter en conséquence ; un agent qui l'ignore ne le peut pas. La question de savoir si l'étiquette doit **restreindre** ce que l'outil renvoie est laissée ouverte (§17).

---

## 10. Sécurité et IAM

**Compte de service dédié** — jamais le compte par défaut d'App Engine. Rôles strictement nécessaires : appel des modèles sur Agent Platform ; lecture seule sur le seau des documents ; **lecture seule sur `dossiers`** (nouveau en v3, pour le `domaine` — §6.4) ; écriture Firestore limitée à `documents` et à sa sous-collection `analyses`.

**Point d'entrée HTTP non invocable publiquement** : n'accepter que Cloud Tasks avec jeton OIDC lié au compte de service attendu ; 403 pour tout le reste.

**Autorisation applicative** : la route Flask et l'outil MCP vérifient l'accès de l'appelant au dossier **avant** d'enfiler la tâche. La fonction ne refait pas ce contrôle — elle n'a pas le contexte utilisateur —, ce qui rend la vérification en amont impérative.

---

## 11. Confidentialité et résidence — liste préalable

**Technique (Claude Code) :**

- [ ] Région vérifiée au Model Garden ; ordre de préférence respecté ; **région effective journalisée et stockée**.
- [ ] Journalisation requête-réponse activée ; fenêtre de conservation vérifiée ; magasin inventorié.
- [ ] Journaux de la fonction inspectés manuellement : aucun contenu, aucun texte extrait, aucun résumé, **aucun indice de protection en clair** — chemins d'erreur et DEBUG compris.
- [ ] Alerte budgétaire Cloud Billing cadrée sur cette fonction.

**Non technique (à trancher par M. Poirier-Lavoie) :**

- [ ] ÉFVP au titre de la Loi 25, incluant le magasin de journalisation requête-réponse comme support distinct.
- [ ] Transfert hors Québec (art. 17 Loi 25) si le repli au rang 3 est utilisé.
- [ ] Mention de l'usage de l'IA dans les conventions d'honoraires et lettres de mandat.
- [ ] Confirmation des conditions de traitement des données de Google Cloud, à la source.

---

## 12. Observabilité

Enregistreur `pallas.analyse_documentaire`.

| Événement | Issue | Attributs |
|---|---|---|
| `analyse_demandee` | succès | `document_id`, `declenche_par` |
| `analyse_terminee` | succès | `duree_ms`, `modele`, `region`, `confiance`, `nature_detectee`, `sous_nature`, `famille`, `niveau_protection`, `nb_champs_absents` |
| `analyse_divergence_categorie` | succès | `category`, `nature_detectee` |
| `analyse_divergence_protection` | succès | `niveau_anterieur`, `niveau_propose` |
| `analyse_alerte_renonciation` | succès | `document_id` — **jamais les indices** |
| `analyse_echouee` | échec | `motif` : `trop_volumineux`, `type_non_supporte`, `erreur_modele`, `sortie_invalide`, `taxonomie_inconnue`, `delai_depasse` |
| `qualification_confirmee` | succès | `document_id` |

**Jamais journalisés :** résumé, texte extrait, dispositif, noms de parties, nom de fichier, numéro de dossier de cour, **contenu de `indices_protection`**. La discipline de `utils/logging_setup.py` doit être reproduite — un `RedactionFilter` équivalent, pas un simple soin à l'écriture.

`nature_detectee`, `sous_nature` et `niveau_protection` sont journalisables : taxonomies fermées, aucune révélation de contenu. `nb_champs_absents` l'est aussi — le **nombre**, jamais la liste, qui nommerait indirectement ce que le document ne contient pas. `indices_protection` ne l'est pas : « en-tête du cabinet, mention *sous toutes réserves* » décrit le document.

---

## 13. Pièges connus

**Déclassement silencieux du privilège.** Une réanalyse ne baisse **jamais** `niveau_protection` : elle inscrit `divergence_protection` et laisse l'ancienne valeur. C'est le piège le plus grave de la phase — un déclassement automatique transformerait une amélioration d'invite en risque de divulgation.

**Absence d'étiquette lue comme `PUBLIC`.** En cas d'échec, `niveau_protection` est **non déterminé**, jamais 0. Le module échoue fermé.

**Écrasement de `category`.** Le modèle écrit `nature_detectee`, jamais `category` ; une seule route peut adopter la nature détectée.

**Dérivations à la mauvaise place.** `famille`, `champs_attendus_absents` et `niveau_protection` sont **calculés en Python** depuis les annexes. Les demander au modèle réintroduirait dans son jugement des règles de droit déterministes.

**`PUBLIC` en matière familiale.** L'art. 16 C.p.c. restreint l'accès : le rabattement sur `CONFIDENTIEL` se fait en code, à partir du `domaine` du dossier parent (§6.4).

**Dates — piège maison.** `date_signature_str` et `date_document_str` sont des **dates sans heure** et suivent la convention `date_str` (`AAAA-MM-JJ`), jamais un horodatage ISO complet : un champ date-seule sérialisé en horodatage se décale d'un jour au passage de fuseau, et une date décalée d'un jour peut faire basculer un calcul de délai.

**Sortie par blocs.** Lire le bloc `tool_use` en filtrant par `type`, jamais par position.

**Gonflement base64.** Le plafond utile se calcule sur la taille encodée.

**XLSX — chaînes mutualisées.** Sans résolution de `sharedStrings.xml`, l'analyse porte sur du bruit numérique.

**Taxonomies ouvertes.** Le schéma énumère les valeurs ; `analyseur.py` rejette ce qui n'y figure pas. Un code inventé rend les valeurs non requêtables et fausse toute agrégation.

**Migration de `procès_verbal`.** Ne pas retirer la valeur du modèle tant qu'un document la porte ; ne pas reclasser en lot.

**Cloud Tasks et pare-feu.** La tâche vise une Cloud Function, hors du pare-feu App Engine. Mais un rappel de la fonction vers l'application arriverait de `0.1.0.2`, hors des plages Cloudflare, et serait bloqué en silence sans règle dédiée.

---

## 14. Plan de tests

**Protection — priorité absolue, c'est le champ le plus dangereux :**

- **Correspondance avec le client** : `SECRET_PROFESSIONNEL`, niveau 3.
- **Lettre au confrère « sous toutes réserves »** : `REGLEMENT`, niveau 2.
- **Rapport d'expert non communiqué** : `LITIGE`, niveau 2.
- **Mémorandum interne préparatoire** : `LITIGE` **et** `SECRET_PROFESSIONNEL` — vérifier le **cumul**, et que le niveau retenu est 3.
- **Acte de procédure déposé** : `PUBLIC`, niveau 0.
- **Projet d'acte non déposé** : `LITIGE` — vérifier que l'absence de n° de dossier a bien empêché `PUBLIC` (§6.4).
- **Acte de procédure en matière familiale** : rabattu sur `CONFIDENTIEL` par le `domaine` du dossier (art. 16 C.p.c.).
- **Non-déclassement** : réanalyser un document étiqueté niveau 3 avec une invite dégradée → `divergence_protection: true`, **`niveau_protection` inchangé**.
- **Échec** : `niveau_protection` non déterminé, **jamais 0**.
- **Renonciation** : document classé `pièce` portant en-tête d'avocat et mention « sous toutes réserves » → `alerte_renonciation_possible: true` (§6.5).

**Classification :**

- **Pièce non procédurale** (contrat, courriel, photo) : champs de l'art. 99 **nuls**, sans invention, `champs_attendus_absents` **vide** — l'absence est normale pour cette famille. Test le plus important de la série.
- **Acte de procédure complet** : tous les champs de l'art. 99, `champs_attendus_absents` vide, `confiance: haute`.
- **Projet d'acte sans n° de dossier** : classé `procédure`, `champs_attendus_absents` contenant `numero_dossier_cour`.
- **Procès-verbal de signification** : champs de l'art. 119, **les deux blocs** renseignés, `qualification_ecrit: NON_DETERMINE`.
- **Procès-verbal d'audience portant jugement** (chambre de pratique) : `contient_dispositif: true`, `dispositif` extrait, **matrice basculée sur celle du jugement**, `alerte_dispositif_detecte: true`.
- **Procès-verbal d'audience sans jugement** : `contient_dispositif: false`, aucune alerte.
- **Note d'honoraires du cabinet vs facture de tiers** : `facture`/`CABINET` et `pièce` ou `preuve`/`PREUVE`.
- **Divergence** : document classé `correspondance`, détecté `jugement` → `divergence_categorie: true`, `category` **inchangé**.
- **Contamination** : aucune extraction du bloc art. 99 sur famille `PREUVE`, ni du bloc C.c.Q. sur famille `JUDICIAIRE` hors PV de signification.
- **Valeur héritée** : un document portant encore `procès_verbal` reste lisible et filtrable.

**Extraction et formats :**

- **DOCX** : fidélité sur un document issu d'un gabarit de la phase H, en-têtes multiples et polices incorporées comprises.
- **XLSX** : plusieurs feuilles, chaînes mutualisées, formules avec valeur en cache, dépassement du plafond → `extraction_tronquee: true`.
- **PDF numérisé** : reconnaissance sans OCR distinct ; `qualite_reconnaissance` cohérente.
- **JPEG / PNG** : voie image directe. **TIFF** : refus explicite.
- **Fichier de 24 Mo** : refusé en amont, message français, aucun appel facturé.

**Robustesse :**

- **Sortie hors taxonomie** : `statut: "echec"`, rien d'écrit dans `analyse`.
- **Journal** : deux exécutions → deux entrées, la première intacte.
- **`confirme`, `category`, `niveau_protection`** : aucun chemin autre que leurs routes dédiées ne les modifie.
- **Journaux** : inspection manuelle, chemins d'erreur compris, **absence d'`indices_protection`** vérifiée.
- **MCP** : nouveaux champs présents, aucune URL signée, plafond de 50 respecté, contrat asynchrone conforme.

---

## 15. Ordre des travaux

Le travail se scinde en deux lots, le premier étant un prérequis autonome.

**Lot 1 — élargissement du vocabulaire (§5.2), sans IA :**

1. Lire `models/document.py` et relever le vocabulaire `category` réel.
2. Ajouter `proces_verbal_signification` et `proces_verbal_audience` ; retirer `procès_verbal` du sélecteur seulement.
3. Propager : formulaire, filtres, énumération MCP, gabarits.
4. Livrer et laisser tourner. **Le reclassement des documents existants se fait au fil de l'eau, par geste humain.**

**Lot 2 — analyse documentaire :**

5. Vérifier la disponibilité régionale au Model Garden ; fixer la variable d'environnement.
6. Créer le compte de service, attribuer les rôles (§10), **y compris la lecture de `dossiers`**.
7. Étendre `ALLOWED_MIME_TYPES` pour XLSX si nécessaire.
8. Activer la journalisation requête-réponse ; vérifier la fenêtre de conservation.
9. Déployer `functions/analyse_documentaire/` — déploiement manuel en v1 ; envisager ensuite une étape Cloud Build conditionnée par la suite de tests, sur le modèle du verrou pytest déjà en place.
10. Livrer routes Flask et gabarits.
11. Livrer l'outil MCP et l'extension de `list_documents`.
12. Compléter la liste de la section 11 avant tout usage sur un dossier réel.

Aucune migration de données : les documents antérieurs n'ont pas de champ `analyse`, ce que l'interface traite comme « non analysé ».

---

## 16. Décisions ouvertes

1. **Lettre de phase** à confirmer contre `CLAUDE.md`.
2. **ASCII ou diacritiques** pour les nouvelles valeurs de `category` (§5.2) — introduire du mixte, ou uniformiser tout le vocabulaire par une migration plus lourde ?
3. **L'étiquette de privilège restreint-elle quoi que ce soit en v1** — sortie MCP, export, partage — ou reste-t-elle purement informative et visuelle ? Recommandation : informative en v1, restrictive dans une phase ultérieure, une fois la fiabilité mesurée.
4. **`SECRET_COMMERCIAL` au niveau 2** (§6.2) — choix pragmatique assumé, ou faut-il l'isoler puisqu'il ne fonde aucune immunité de production ?
5. **`INFORMATEUR`** — conservé malgré son application civile étroite, ou retiré de la taxonomie ?
6. **TIFF** — refus en v1, ou conversion dans un module isolé ?
7. **Journal append-only** — retenu tel quel, ou champ simple écrasable pour limiter le volume ?
8. **Contrôle de coût** — plafond d'analyses par dossier ou par mois, ou surveillance budgétaire seule ?
9. **Qualification du procès-verbal de signification en preuve** — laissée à `NON_DETERMINE`, ou tranchée en amont ?
10. **ÉFVP et mention client** — préalables à la mise en service, ou peuvent-ils suivre de peu ?

---

## 17. Hors périmètre

Synthèse à l'échelle du dossier ; analyse de la collection `notes` ; recherche vectorielle et sémantique ; rattachement automatique des parties ; escalade OCR par Document AI ; conversion TIFF ; outil MCP `get_document` ; restriction d'accès fondée sur le niveau de protection ; **rattachement du dispositif détecté au module d'échéances**.

Ces deux derniers points sont les suites naturelles. Le second en particulier : un jugement rendu à l'audience fait courir les délais d'appel, et la phase K sait désormais le détecter — mais fonder un délai de rigueur sur une lecture automatique demanderait une fiabilité que rien n'a encore établie. À reprendre quand le taux de confirmation sera mesurable.

Reste enfin l'alimentation de la feuille « Analyse » (théorie de la cause) depuis les documents analysés. La v3 la rapproche : une cartographie des faits préremplie suppose de savoir quel document est une preuve, de quelle sorte, **et ce qu'on a le droit d'en faire**. Les trois étages y pourvoient.

---

## Annexe A — Natures et sous-natures

`nature` reprend le vocabulaire `category` **élargi** (§5.2). `sous_nature` la raffine. `famille` est **dérivée en code**.

### Famille `JUDICIAIRE`

| `nature` | `sous_nature` | Libellé | Ancrage |
|---|---|---|---|
| `procédure` | `PROC_DEM_INTRO` | Demande introductive d'instance | art. 100, 107 C.p.c. |
| `procédure` | `PROC_AVIS_ASSIGN` | Avis d'assignation | art. 145 C.p.c. |
| `procédure` | `PROC_DEM_INSTANCE` | Demande en cours d'instance | art. 101 C.p.c. |
| `procédure` | `PROC_PROTOCOLE` | Protocole de l'instance | art. 148 C.p.c. |
| `procédure` | `PROC_MOYEN_PRELIM` | Moyen préliminaire | Livre II, t. I, ch. V, s. I ; art. 167 C.p.c. |
| `procédure` | `PROC_DEFENSE` | Défense écrite | art. 170 C.p.c. |
| `procédure` | `PROC_EXPOSE_SOMMAIRE` | Exposé sommaire des éléments de contestation | art. 148 al. 2 (5°), 170 al. 2 C.p.c. |
| `procédure` | `PROC_DEM_RECONV` | Demande reconventionnelle | art. 172 C.p.c. |
| `procédure` | `PROC_INTERVENTION` | Intervention volontaire ou forcée | art. 184 C.p.c. |
| `procédure` | `PROC_DECL_SERMENT` | Déclaration sous serment | art. 105, 106 C.p.c. |
| `procédure` | `PROC_DEM_INSCRIPTION` | Demande d'inscription pour instruction et jugement | art. 173 C.p.c. |
| `procédure` | `PROC_DECL_APPEL` | Déclaration d'appel | art. 358 C.p.c. |
| `procédure` | `PROC_AUTRE` | Autre acte de procédure | — |
| `jugement` | `JUG_JUGEMENT` | Jugement de première instance | — |
| `jugement` | `JUG_ARRET` | Arrêt de la Cour d'appel | art. 389 C.p.c. |
| `jugement` | `JUG_ORDONNANCE` | Ordonnance | — |
| **`proces_verbal_signification`** | `PV_SIGNIFICATION` | Procès-verbal de signification | art. 119, 120 C.p.c. |
| **`proces_verbal_audience`** | `PV_AUDIENCE` | Procès-verbal d'audience | — |
| **`proces_verbal_audience`** | `PV_AUDIENCE_JUGEMENT` | Procès-verbal d'audience **portant jugement** (§5.4) | — |
| `transcription` | `TRANS_INTERROGATOIRE` | Notes sténographiques d'interrogatoire | — |
| `transcription` | `TRANS_AUDIENCE` | Notes sténographiques d'audience | — |
| `procès_verbal` | *(héritée)* | Valeur antérieure à la scission — lisible, non offerte | — |

### Famille `CORRESPONDANCE`

| `nature` | `sous_nature` | Libellé | Ancrage |
|---|---|---|---|
| `correspondance` | `CORR_MISE_DEMEURE` | Mise en demeure | art. 1595 C.c.Q. |
| `correspondance` | `CORR_CONFRERE` | Lettre au confrère | — |
| `correspondance` | `CORR_CLIENT` | Lettre au client | — |
| `correspondance` | `CORR_TRIBUNAL` | Lettre au tribunal ou au greffe | — |
| `correspondance` | `CORR_EXPERT` | Communication avec un expert | — |
| `correspondance` | `CORR_TIERS` | Lettre à un tiers | — |
| `correspondance` | `CORR_AUTRE` | Autre correspondance | — |

### Famille `PREUVE`

| `nature` | `sous_nature` | Libellé |
|---|---|---|
| `pièce` | `PIECE_COMMUNIQUEE` | Pièce cotée, notifiée et déposée (§5.6) |
| `preuve` | `PREUVE_CONTRAT` | Contrat, entente, quittance |
| `preuve` | `PREUVE_COURRIEL` | Courriel ou message |
| `preuve` | `PREUVE_FACTURE` | Facture d'un tiers |
| `preuve` | `PREUVE_RELEVE` | Relevé, état de compte |
| `preuve` | `PREUVE_PHOTO` | Photographie |
| `preuve` | `PREUVE_RAPPORT_EXPERT` | Rapport d'expertise |
| `preuve` | `PREUVE_AUTRE` | Autre élément de preuve |

### Famille `CABINET`

| `nature` | `sous_nature` | Libellé |
|---|---|---|
| `mandat` | `CAB_MANDAT` | Mandat, convention d'honoraires |
| `facture` | `CAB_FACTURE` | Note d'honoraires du cabinet |
| `déboursé` | `CAB_DEBOURSE` | Pièce justificative de déboursé |
| `autre` | `CAB_MEMO` | Mémorandum ou note téléversé comme fichier |

### Famille `INDETERMINE`

| `nature` | `sous_nature` |
|---|---|
| `autre` | `NON_DETERMINE` |

> **Le modèle ne crée jamais de code.** Chaque ajout est un geste délibéré portant son ancrage vérifié.

---

## Annexe B — Matrice des champs attendus

Consommée **en code** pour dériver `champs_attendus_absents`. `✓` = attendu ; `○` = possible, absence non signalée ; vide = non attendu.

| `sous_nature` | n° cour | tribunal | district | parties | date | auteur | Ancrage |
|---|:--:|:--:|:--:|:--:|:--:|:--:|---|
| `PROC_*` (sauf `DECL_SERMENT`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | art. 99 al. 2-3 C.p.c. |
| `PROC_DECL_SERMENT` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | art. 99 + 105 al. 2 C.p.c. |
| `PV_SIGNIFICATION` | ✓ | ○ | | ✓ | ✓ | ✓ | art. 119 C.p.c. |
| `JUG_ARRET` | ✓ | ✓ | ○ | ✓ | ✓ | ✓ | art. 389 C.p.c. |
| `JUG_*`, `PV_AUDIENCE_JUGEMENT` | ✓ | ✓ | ○ | ✓ | ✓ | ✓ | **usage — non vérifié** |
| `PV_AUDIENCE`, `TRANS_*` | ✓ | ✓ | ○ | ✓ | ✓ | ○ | **usage — non vérifié** |
| `CORR_*` | ○ | | | ○ | ✓ | ✓ | — |
| `PIECE_*`, `PREUVE_*` | | | | | ○ | ○ | — |
| `CAB_*` | | | | | ✓ | | — |
| `NON_DETERMINE` | | | | | | | — |

**Champs supplémentaires exigés par le texte :**

- `PROC_DECL_SERMENT` — jour et lieu du serment ; nom et adresse de celui qui le prête ; nom et qualité de celui qui le reçoit (art. 105 al. 2 C.p.c.).
- `PV_SIGNIFICATION` — nature du document signifié ; lieu, date **et heure** ; nom et, s'il y a lieu, qualité de la personne à qui le document a été remis ; refus ou échec de la tentative ; état des honoraires et frais (art. 119 C.p.c.).
- `JUG_ARRET` — dispositif ; nom des juges ayant entendu l'appel, avec mention des dissidents (art. 389 C.p.c.).
- `PV_AUDIENCE_JUGEMENT` — dispositif (§5.4).

> Les lignes **« usage — non vérifié »** reposent sur la pratique constante, non sur une disposition repérée. Les traiter comme indicatives : leur absence ne doit pas peser autant sur `confiance` que celle d'une mention légalement obligatoire.

---

## Annexe C — Taxonomie « preuve » (C.c.Q.)

Renseignée pour les familles `PREUVE` et `CORRESPONDANCE`, ainsi que pour `PV_SIGNIFICATION`. `NON_DETERMINE` ailleurs.

**Axe 1 — moyen de preuve.** Art. 2811 C.c.Q. : écrit, témoignage, présomption, aveu, présentation d'un élément matériel.

| Code | Libellé | Ancrage |
|---|---|---|
| `ECRIT` | Écrit | art. 2811, 2837 C.c.Q. |
| `TEMOIGNAGE` | Témoignage | art. 2811, 2843 C.c.Q. |
| `PRESOMPTION` | Présomption | art. 2811 C.c.Q. |
| `AVEU` | Aveu | art. 2811 C.c.Q. |
| `ELEMENT_MATERIEL` | Présentation d'un élément matériel | art. 2811 C.c.Q. |
| `NON_DETERMINE` | Indéterminable | — |

**Axe 2 — qualification de l'écrit** (seulement si `moyen_preuve == ECRIT`).

| Code | Libellé | Ancrage |
|---|---|---|
| `ACTE_AUTHENTIQUE` | Acte authentique | art. 2813, 2814 C.c.Q. |
| `ACTE_NOTARIE` | Acte notarié | art. 2814 (6°), 2819 C.c.Q. |
| `ACTE_SEMI_AUTHENTIQUE` | Acte émanant d'un officier public étranger | art. 2822 C.c.Q. |
| `SOUS_SEING_PRIVE` | Acte sous seing privé | art. 2826, 2827 C.c.Q. |
| `ECRIT_ENTREPRISE` | Écrit non signé utilisé dans le cours des activités d'une entreprise | art. 2831 C.c.Q. |
| `PAPIER_DOMESTIQUE` | Papier domestique | art. 2833 C.c.Q. |
| `AUTRE_ECRIT` | Autre écrit rapportant un fait | art. 2832 C.c.Q. |
| `NON_DETERMINE` | Indéterminable | — |

**Notions connexes, non stockées comme codes :** support et neutralité technologique (art. 2837 C.c.Q.) ; intégrité du document technologique (art. 2838, 2839 C.c.Q.) ; original et copie (art. 2860 C.c.Q. — d'où `parait_original`, qui **observe** sans conclure) ; copie certifiée et transfert (art. 2841, 2842 C.c.Q.) ; fardeau (art. 2803 C.c.Q. — fondement de la règle excluant les actes de procédure de la qualification en preuve).

---

## Annexe D — Régime de protection

Champ `privileges` — **cumulable**. `niveau_protection` est le maximum des niveaux retenus, **dérivé en code**.

| Niveau | Code | Portée | Fondement | Nature du fondement |
|:--:|---|---|---|---|
| 3 | `SECRET_PROFESSIONNEL` | Communication avec le client | art. 9 Charte (c-12) ; art. 60.4 Code des professions (c-26) ; art. 2858 al. 2 C.c.Q. | **Statutaire et quasi constitutionnel** |
| 2 | `LITIGE` | Communication avec un expert ou un collaborateur, travail préparatoire | *Lizotte c. Aviva*, 2016 CSC 52 ; *Blank c. Canada*, 2006 CSC 39 | **Jurisprudentiel** — privilège générique |
| 2 | `REGLEMENT` | Offres et pourparlers transactionnels | art. 4, 606 C.p.c. (modes privés de PRD) ; *Union Carbide c. Bombardier*, 2014 CSC 35 | **Mixte** |
| 2 | `INTERET_COMMUN` | Parties actuelles ou potentielles partageant un intérêt | — | **Jurisprudentiel** |
| 2 | `ENQUETE_INTERNE` | Documents internes constitués en vue du litige | — | **Jurisprudentiel** — le plus souvent une espèce de `LITIGE` |
| 2 | `INFORMATEUR` | Identité d'un client dénonciateur | — | **Jurisprudentiel** — d'abord pénal, application civile étroite |
| 2 | `SECRET_COMMERCIAL` | Données industrielles et commerciales | art. 1472, 1612 C.c.Q. ; art. 22-23 Loi sur l'accès (a-2.1) pour les organismes publics | **Statutaire, mais ⚠ voir ci-dessous** |
| 1 | `CONFIDENTIEL` | Correspondance externe, défaut résiduel | — | — |
| 0 | `PUBLIC` | Acte de procédure déposé, pièce cotée | art. 11 C.p.c. | **Statutaire** |

**Trois réserves à porter dans l'interface, non seulement dans le code :**

> **`SECRET_COMMERCIAL` n'est pas un privilège de non-divulgation.** Les art. 1472 et 1612 C.c.Q. fondent une responsabilité et une indemnisation — exonération lorsque l'intérêt général l'emporte, calcul du préjudice —, non une immunité de production. En instance, la protection passe par une ordonnance rendue sous l'art. 12 C.p.c. Le classer au niveau 2 est un choix **de manipulation**, non un énoncé de droit.

> **`PUBLIC` n'est pas automatique.** L'art. 11 al. 2 C.p.c. réserve les exceptions légales, et l'art. 16 C.p.c. restreint l'accès en matière familiale ainsi qu'aux documents de santé ou psychosociaux déposés sous pli cacheté. Le rabattement se fait en code depuis le `domaine` du dossier parent (§6.4).

> **Une ordonnance de confidentialité (art. 12 C.p.c.) est invisible depuis le document.** Ni le modèle ni le code ne peuvent la connaître. L'étiquette n'est jamais exhaustive.

**Arrêts vérifiés à CanLII :** *Lizotte c. Aviva, Compagnie d'assurance du Canada*, 2016 CSC 52, [2016] 2 R.C.S. 521 ; *Union Carbide Canada Inc. c. Bombardier Inc.*, 2014 CSC 35, [2014] 1 R.C.S. 800 ; *Blank c. Canada (Ministre de la Justice)*, 2006 CSC 39, [2006] 2 R.C.S. 319. L'existence et l'intitulé de ces décisions sont confirmés ; **leur autorité actuelle et la portée exacte de leur dispositif n'ont pas été vérifiées** et doivent l'être avant toute utilisation en argumentation.

> **Rappel — le champ le plus dangereux du système.** Ces codes sont des hypothèses produites par un modèle. Une erreur vers le bas peut mener à une divulgation par inadvertance, donc à un manquement sous l'art. 9 de la Charte et l'art. 60.4 du Code des professions ; une erreur vers le haut est une gêne. Le système est asymétrique en conséquence (§6.3) : il échoue vers le haut, ne déclasse jamais seul, et n'autorise rien.
