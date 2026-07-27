# Spécification — Phase L3 : Portail client — formulaire d'ouverture (intake) — Pallas Athéna

**Destinataire :** Claude Code
**Cible :** dépôt `jpoirierlavoie/Athena-Pallas`, branche `main`
**Statut :** prêt à implémenter — dépend du socle L1 (`SPEC_PHASE_L1_PORTAIL_SOCLE_DOCUMENTS.md`) ; le déclencheur post-rendez-vous dépend de L2 (`SPEC_PHASE_L2_BOOKINGS_SYNC.md`, §5.2) mais les déclencheurs manuels vivent sans L2
**Ordre de la série L :** L1 → L2 → **L3 (présente)**

---

## 0. Résumé exécutif

Ajouter au portail un **formulaire d'ouverture de dossier client** (« intake ») en quatre étapes, accessible par la même invitation Firebase à lien courriel que la phase L1 (`type="intake"`). La soumission produit une **enveloppe JSON en quarantaine** (aucun fichier), signalée par Cloud Tasks au gestionnaire du service principal (L1 §8) ; le juriste l'examine dans **Réception → onglet « Ouvertures »**, où il **crée** la partie (ou **applique champ par champ** une mise à jour à une partie existante) — **jamais d'ingestion automatique** dans Firestore.

Décisions entérinées et bornes :

1. **Trois déclencheurs d'émission** : (a) à la **confirmation d'un rendez-vous** dont le courriel ne correspond à aucune partie (case cochée par défaut, L2 §5.2 — `FEATURE_INTAKE` passe à `True` dans la présente phase) ; (b) bouton **« Inviter à compléter le dossier client »** sur la fiche d'une partie existante (mise à jour) ; (c) **« Nouvelle invitation »** dans Réception (courriel libre).
2. **KYC mis de côté** : aucune pièce d'identité demandée, aucun téléversement dans ce formulaire ; le statut de conformité de la partie demeure `non_vérifié`. Le schéma d'enveloppe **réserve** l'emplacement (`"pieces_identite": null`) pour la phase future.
3. **Aide au contrôle des conflits, sans automatisme** : le formulaire recueille le ou les **noms des parties adverses** (noms seulement — l'avertissement à l'écran enjoint de **ne pas exposer la situation**) ; Réception affiche les **candidats de correspondance** trouvés dans la base des contacts, à titre d'aide visuelle ; toute décision demeure humaine.

---

## 1. À LIRE EN PREMIER — code vivant

Avant d'éditer : `athena/models/partie.py` (ou le nom réel du modèle des contacts — champs exacts : type personne physique/morale, prénom/nom, dénomination, courriel, téléphones, adresse structurée ou texte, langue, `contact_role`, section Conformité), le formulaire des parties (`templates/parties/…`) pour calquer l'ordre et les libellés des champs, la fonction de liste des parties (recherche), le socle L1 (invitations §5–6, courriel et tâches §8, Réception §9), et L2 §5.2 (point d'accrochage du déclencheur).

⚠️ La **table de correspondance** du §4.2 utilise des noms de champs canoniques : **adapter chaque nom au modèle vivant** ; si un champ n'existe pas (ex. NEQ), l'omettre du formulaire plutôt que de l'ajouter au modèle.

---

## 2. Émission des invitations `type="intake"` (service principal)

Réutiliser `emettre_invitation` (L1 §6.2) avec :

- `type="intake"`, durée `INVITATION_INTAKE_JOURS` (14 jours) ;
- `display_label` : par défaut « Ouverture de votre dossier client » (générique — **ne pas** y révéler de numéro ni de partie adverse) ;
- `prefill` : **uniquement** pour le déclencheur (b) — instantané des champs **non sensibles** de la partie : type, prénom/nom ou dénomination, courriel, téléphones, adresse, langue. **Jamais** : notes internes, conformité/KYC, mémos, liaisons de dossiers. ⚠️ Rappel du piège L1 §5 : tout le document d'invitation est lisible par le service public.
- Gabarit de courriel : Annexe A.1 (adapter A.1 de L1 — objet « Ouverture de votre dossier client »).

Déclencheur (a) : dans L2 §5.2, passer `FEATURE_INTAKE = True` et brancher la case sur cette émission (courriel = `client_email` du rendez-vous). Déclencheur (b) : bouton sur la fiche partie ; refuser si la partie n'a pas de courriel. Déclencheur (c) : formulaire minimal (courriel + libellé) dans Réception.

---

## 3. Formulaire portail (`portail/routes.py`, gabarits `portail/templates/intake_*.html`)

Garde de route : `inv["type"] == "intake"` (le socle L1 §6.5 fournit déjà session, relecture d'invitation, CSRF, CSP). Assistant en **quatre étapes**, français, mobile d'abord :

- **É1 — Identité** : nature (`personne physique` / `personne morale` — bascule Alpine) ; physique : prénom, nom, (facultatif) date de naissance **si le modèle vivant la porte** ; morale : dénomination sociale, (facultatif) NEQ **si le modèle le porte** ; langue de communication (français / anglais).
- **É2 — Coordonnées** : courriel **prérempli et en lecture seule** (= courriel de l'invitation — cohérence d'identité), téléphone principal (+ secondaire facultatif), adresse complète (calquer la structure du modèle vivant : soit champs distincts, soit bloc texte).
- **É3 — Partie(s) adverse(s)** : lignes répétables (Alpine ; max `INTAKE_MAX_ADVERSES = 5`) — `{nom complet ou dénomination (≤ 120), précision facultative (≤ 200, une ligne — ex. « mon ancien employeur »)}`. Encadré d'avertissement : « Indiquez uniquement les **noms**. **N'exposez pas votre situation** ni les faits de l'affaire dans ce formulaire ; ils seront abordés avec votre avocat. »
- **É4 — Révision et consentement** : récapitulatif ; case obligatoire de consentement à la collecte (texte : Annexe A.2) ; bouton « Soumettre ».

**Sauvegarde de progression — en session (cookie), pas en quarantaine.** Le portail ne peut **pas relire** le bucket (créateur d'objets seulement, L1 §3). Chaque « Suivant » fait `POST /api/intake/etape` qui valide et fusionne `session["intake"]` ; « Précédent » recharge depuis la session. Bornes de champs ci-dessus **obligatoires** : la charge sérialisée doit rester ≤ ~2 Ko pour tenir dans le cookie signé (limite pratique ~4 Ko). Conséquence assumée : la reprise vaut **pour l'appareil et la session en cours** (24 h) ; la reprise inter-appareils n'existe pas en v1 (décision D-L3-1).

**Prérempli** : au premier `GET`, si `inv["prefill"]`, initialiser `session["intake"]` avec.

---

## 4. Soumission et enveloppe

### 4.1 `POST /api/intake/finaliser`

Validation finale complète (champs requis selon la nature, consentement coché), puis écriture de `submissions/{inv}/{batch}/envelope.json` avec **précondition `if_generation_match=0`** (double soumission → 409), `batch` généré à la finalisation :

```json
{"type": "intake", "invitation_id": "…", "batch": "…", "partie_id": "…|null",
 "submitted_at": "<ISO 8601 UTC>",
 "client": {"email": "…", "uid": "…"},
 "http": {"ip": "…", "user_agent": "…"},
 "donnees": {"nature": "physique|morale", "prenom": "…", "nom": "…",
             "denomination": "…", "neq": "…", "date_naissance": "…",
             "langue": "fr|en", "courriel": "…",
             "telephone": "…", "telephone2": "…", "adresse": {…}},
 "parties_adverses": [{"nom": "…", "precision": "…"}],
 "consentement": {"accepte": true, "horodatage": "<ISO>", "version_texte": "1"},
 "pieces_identite": null}
```

Puis `taches.signaler("soumise", inv_id, batch=batch)`, purge de `session["intake"]`, page de confirmation : « Votre formulaire a été transmis. Nous vous confirmerons la suite par courriel. »

### 4.2 Gestionnaire de tâches (extension du traitement `soumise` de L1 §8.3)

Aiguiller sur `envelope["type"]` : pour `intake` — pas d'empreintes de fichiers ; valider le schéma (rejet consigné si malformé), `statut → soumise`, append `soumissions[]`, puis **courriel de confirmation simple** (Annexe A.3 ; garde d'idempotence `accuses[batch]` identique à L1). La **réconciliation** de L1 §8.4 couvre les enveloppes `intake` sans modification (mêmes critères de réparation : présence dans `soumissions[]` et `accuses[batch]`).

---

## 5. Réception — onglet « Ouvertures »

Remplace le réservoir « Livré en phase L3 » (L1 §9.2). Liste des invitations `type="intake"` au statut `soumise` (puis `traitée` récentes). Carte par soumission :

### 5.1 Corps de la fiche

Rendu lisible de `donnees` (libellés français, ordre du formulaire) + horodatage, IP/UA.

### 5.2 Volet « Parties adverses déclarées » — aide au contrôle des conflits

Pour **chaque** nom déclaré : recherche de candidats dans les contacts — charger la liste des parties (fonction existante) et apparier **en Python** : `casefold()` des deux côtés, correspondance si l'un contient l'autre ou si l'écart de jetons est faible (comparaison simple par ensembles de mots ; **aucune** dépendance de fuzzy matching). Affichage : « Candidats existants : {liens vers fiches} » ou « Aucun contact correspondant ». Sous chaque nom, case **« Créer comme contact (rôle : partie adverse) »**, **cochée par défaut** (décision D-L3-2) — à l'acceptation (§5.3), création d'un contact minimal `{nom, contact_role="partie_adverse"}` avec note de provenance « Déclaré par le client via le portail, invitation {id} » **si et seulement si** la case reste cochée et qu'aucun candidat n'a été retenu comme doublon.

⚠️ Aide **visuelle** seulement : aucun blocage, aucun verdict de conflit automatisé. La vérification déontologique demeure celle du juriste.

### 5.3 Actions

- **Sans `partie_id`** (nouveau client) — bouton **« Créer la partie »** : création via le chemin de modèle existant avec la table de correspondance ci-dessous ; `contact_role = "client"` (valeur vivante du vocabulaire) ; **la section Conformité n'est pas touchée** (`identity_verified = "non_vérifié"`) ; redirection vers la fiche créée avec bandeau « Vérifiez et complétez la fiche ». Puis `statut → traitée`, enveloppe déplacée sous `archive/` (motif L1 §9.2).
- **Avec `partie_id`** (mise à jour) — vue **côte à côte** : pour chaque champ, valeur actuelle ↔ valeur soumise, différences surlignées, **case par champ** « Appliquer » (cochées par défaut sur les seuls champs qui diffèrent) ; bouton « Appliquer la sélection » → mise à jour **partielle** de la partie (merge — ne jamais écraser les champs non cochés ni les champs internes), puis `traitée` + archivage.
- **« Refuser »** : `statut → refusée`, enveloppe archivée ; aucun courriel automatique (suivi humain).

**Table de correspondance** (canonique → **à adapter au modèle vivant**, §1) :

| Formulaire (`donnees`) | Champ partie |
|---|---|
| `nature` | type personne physique / morale (énumération vivante) |
| `prenom`, `nom` / `denomination` | prénom, nom / dénomination |
| `neq` | NEQ (si le champ existe ; sinon omis du formulaire) |
| `date_naissance` | date de naissance (si existante ; **patron `date_str`**, jamais horodatage) |
| `langue` | langue de communication |
| `courriel` | courriel principal |
| `telephone`, `telephone2` | téléphones |
| `adresse` | adresse (structure vivante) |
| — | `contact_role="client"`, provenance « portail », Conformité **intacte** |

---

## 6. KYC — réservé, non implémenté

Aucune collecte de pièce d'identité ; `pieces_identite` demeure `null` dans l'enveloppe (emplacement de schéma réservé) ; aucun champ de conformité n'est écrit à l'ingestion. Le formulaire n'y fait **aucune** allusion. (Mémo d'architecture, hors périmètre : la photo d'une pièce = **collecte**, non **vérification** ; toute phase KYC future exigera son propre cadrage.)

---

## 7. Configuration

```python
FEATURE_INTAKE = True                  # bascule le déclencheur de L2 §5.2
INTAKE_MAX_ADVERSES = 5
INTAKE_PRECISION_MAX = 200
INTAKE_NOM_MAX = 120
INVITATION_INTAKE_JOURS = 14           # déjà déclaré en L1, Annexe C
```

---

## 8. Observabilité

`portail.intake.etape` (numéro d'étape), `portail.intake.soumis`, `principal.intake.confirmation_envoyee`, `reception.intake.partie_creee`, `reception.intake.partie_mise_a_jour` (nombre de champs appliqués), `reception.intake.adverse_cree`, `reception.intake.refuse`. Redaction : jamais de noms ni de courriels en clair dans les journaux — identifiants d'invitation et compteurs.

---

## 9. Critères d'acceptation

1. Rendez-vous d'essai confirmé (L2) avec courriel inconnu → case cochée → invitation intake expédiée ; le même formulaire s'ouvre aussi depuis la fiche d'une partie (prérempli) et depuis Réception (courriel libre).
2. Le lien exige la confirmation du courriel invité (propriété L1) ; le formulaire refuse une invitation `type="documents"` et réciproquement.
3. Progression sauvegardée entre les étapes (retour arrière sans perte) sur le même appareil ; les bornes de longueur sont appliquées côté serveur.
4. Double soumission → 409 ; le client reçoit **un seul** courriel de confirmation (idempotence de la tâche).
5. Réception → Ouvertures : nouveau client → « Créer la partie » produit une fiche conforme à la table §5.3, Conformité intacte, rôle client ; partie existante → la vue côte à côte n'applique **que** les champs cochés.
6. Les candidats de conflit s'affichent pour un nom adverse proche d'un contact existant ; la création du contact adverse n'a lieu que si la case demeure cochée.
7. Enveloppe archivée après traitement ou refus ; **aucune** écriture Firestore avant le clic du juriste.

---

## 10. Décisions ouvertes

- **D-L3-1** — reprise inter-appareils du brouillon (imposerait un stockage serveur relisible par le portail : préfixe lisible dédié + condition IAM, ou service de session). Défaut v1 : session-appareil, 24 h.
- **D-L3-2** — création des contacts « partie adverse » cochée par défaut à l'acceptation. Défaut : **cochée** (enrichit le contrôle des conflits futurs) ; décocher si le praticien préfère une base de contacts strictement délibérée.
- **D-L3-3** — courriel au client au refus d'une soumission (défaut : aucun ; suivi humain).
- **D-L3-4** — champs additionnels d'ouverture (référence du renvoi, comment nous avez-vous connu, etc.). Défaut : hors v1 pour préserver la limite du cookie de session.

---

## Annexe A — Gabarits (français)

### A.1 Courriel d'invitation (intake)

> **Objet :** Ouverture de votre dossier client — {cabinet}
>
> Bonjour,
>
> Afin de préparer l'ouverture de votre dossier, nous vous invitons à compléter le formulaire sécurisé suivant : {lien}
>
> Ce lien est **strictement personnel** et lié à la présente adresse courriel ; il ne doit pas être transféré. Il demeure valide jusqu'au {date_expiration}. Le formulaire requiert environ cinq minutes ; vos renseignements demeurent confidentiels et ne seront versés à votre dossier qu'après examen.
>
> {signature}

### A.2 Texte de consentement (É4, version 1)

> Je consens à ce que {cabinet} recueille les renseignements fournis au présent formulaire aux fins de l'ouverture et de la gestion de mon dossier, de la vérification des conflits d'intérêts et des communications avec moi. Ces renseignements sont conservés de façon sécurisée au Québec/Canada et traités conformément aux obligations professionnelles de l'avocat et à la législation applicable sur la protection des renseignements personnels. Je peux retirer mon consentement en communiquant avec le cabinet, sous réserve des obligations légales de conservation.

### A.3 Courriel de confirmation de soumission

> **Objet :** Confirmation de réception — formulaire d'ouverture
>
> Bonjour,
>
> Nous confirmons la réception de votre formulaire d'ouverture le {date et heure, America/Toronto}. Son contenu sera examiné et nous vous reviendrons pour la suite. La présente confirmation atteste uniquement de la **réception** du formulaire ; elle ne constitue ni l'ouverture d'un dossier ni la formation d'un mandat.
>
> {signature}
