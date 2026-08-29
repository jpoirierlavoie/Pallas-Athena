# Les tâches créées par le breffage

Le breffage crée des tâches. C'est une exception à la discipline d'écriture, elle est étroite, et
elle porte un risque qui ne se supprime pas — seulement se borne. Ce fichier dit quoi créer, comment
ne pas créer deux fois, et où le mécanisme cède.

**Volet quotidien seulement.** La passe du lundi ne crée jamais rien.

## Table des matières

1. Le risque, énoncé d'abord
2. La déduplication, en deux temps
3. Les quatre déclencheurs
4. D'où vient le `dossier_id`
5. Les titres et les champs
6. Les limites de l'exécution
7. Ce qui se rapporte

---

## 1. Le risque, énoncé d'abord

**Rien ne peut supprimer une tâche depuis cette conversation.** Aucun outil du clavardage n'efface
une tâche. Une tâche créée à tort reste, et c'est le juriste qui la retire à la main dans
l'application.

**La clé d'idempotence ne protège pas d'un jour à l'autre.** Elle vit 24 heures. Une clé stable comme
`tache-prescription-2026-035` expire pendant la nuit : le lendemain, la même clé est libre et crée
une **seconde** tâche. L'idempotence protège d'un rejeu du même matin, jamais du matin suivant.

**`list_tasks` n'a pas de recherche par titre.** La déduplication est donc une comparaison faite ici,
sur des titres lus. Elle vaut ce que valent les titres lus — d'où la section 2, qui est la partie
délicate de ce volet.

Il en découle une conséquence à accepter : **si le juriste renomme une tâche créée ici, le breffage
du lendemain en crée une seconde.** C'est le prix du volet. Il se paie en désordre visible, jamais en
travail perdu.

## 2. La déduplication, en deux temps

### Premier temps — la lecture d'ensemble (appel 3 du corps)

```
list_tasks(limit: 50)
```

Sans `dossier_id` et **sans `include_completed`**. Ce défaut est ce qui rend l'appel utilisable : le
filtre serveur ne garde alors que `à_faire` et `en_cours`, soit une quinzaine de lignes, et la
réponse n'est **pas tronquée**.

⚠ **Ne jamais poser `include_completed: true` sur cet appel.** Le drapeau retire le filtre de statut,
et l'outil renvoie alors les 50 premières lignes d'un tri **par échéance croissante** portant sur
toute l'histoire du cabinet — plusieurs centaines de tâches, dont l'écrasante majorité sont terminées
depuis des mois. Les tâches ouvertes tombent hors de la page, la réponse revient `truncated`, et la
lecture ne contient **ni une seule tâche ouverte, ni un seul titre `[Auto]`**. Elle serait pire
qu'inutile : elle aurait l'air d'une lecture réussie.

Cette lecture sert deux fins : la rubrique « Vos tâches » de la note, et un premier tri des candidats
— la plupart des doublons sont des tâches encore ouvertes.

**Une lecture qui rend ZÉRO ligne est une lecture EN ÉCHEC, pas un cabinet sans tâches.** Le modèle
sous-jacent avale une panne Firestore en liste vide, et la réponse est alors indiscernable d'un
succès : `count: 0`, `truncated: false`. Le cabinet a toujours des tâches ouvertes. Donc : **zéro
ligne, on ne crée aucune tâche ce matin**, on l'écrit dans « Ce qui n'a pas été vérifié », et « Vos
tâches » dit que la lecture n'a pas abouti — jamais « 0 tâche ouverte », qui serait une affirmation
fausse.

Même règle si l'appel revient `truncated` : la vue d'ensemble est partielle, on ne crée rien.

### Second temps — la vérification par dossier, avant CHAQUE création

Un candidat qui survit au premier temps n'est pas encore sûr : une tâche **terminée** portant le même
titre n'est pas dans la lecture d'ensemble, et la règle est qu'une tâche terminée **ne revient
jamais** — la terminer veut dire « je m'en suis occupé », et le breffage le croit sur parole.

Avant de créer, et seulement pour les candidats retenus :

```
list_tasks(dossier_id: "…", include_completed: true, limit: 50)
```

Ici `include_completed` est correct et nécessaire : la portée est **un seul dossier**, qui porte une
poignée de tâches, donc la lecture est complète et non tronquée. C'est la seule forme qui répond
exactement à « ce titre existe-t-il déjà, ouvert ou terminé ? ».

Un candidat se crée seulement si **aucune** tâche lue — ouverte ou terminée — ne porte déjà son titre
exact. Si cette lecture est tronquée ou vide, **ne pas créer ce candidat** et le dire.

Un candidat sans dossier ne peut pas être vérifié ainsi : il ne se crée pas (section 4).

## 3. Les quatre déclencheurs

Deux d'entre eux nomment ce que rien d'autre ne voit. Les deux autres redoublent l'agenda, et c'est
assumé : une échéance vue tous les matins sans jamais devenir une tâche finit par se fondre dans le
décor.

### a. Un fil qui attend votre réponse

Source : le balayage des courriels, section 4 de `courriels.md`. Le dernier message du fil ne vient
pas du juriste et date de plus de trois jours juridiques.

**Le seul signal qui n'existe nulle part ailleurs dans Athéna.** L'agenda ne voit pas un courriel ;
le dossier ne sait pas qu'une question est restée sans réponse.

Ne rien créer pour un fil non attribué à un dossier : sans dossier, le titre n'est pas stable et la
vérification du second temps est impossible. Le rapporter dans la note à la place.

Ne rien créer non plus si le balayage est revenu `truncated` : la détection des fils sans réponse n'a
alors pas vraiment tourné (voir `courriels.md` §4).

### b. Un dossier sans prochaine étape

Un dossier `actif` qui réunit les trois : aucune tâche ouverte, aucune audience à venir, et
`PROTO_ABSENT` au rapport de couverture.

Rien n'est prévu sur ce dossier. Un agenda ne peut pas le montrer — un agenda montre ce qui est
prévu, et ici il n'y a rien.

Les trois sources : les tâches ouvertes viennent du premier temps ci-dessus ; le `PROTO_ABSENT` des
`items` du rapport de couverture — qui **omet les dossiers sans constatation**, ce qui est exactement
ce qu'on veut ; les audiences demandent **un appel de plus** :

```
list_hearings(limit: 50)
```

Sans dossier ni dates : la fenêtre par défaut couvre d'aujourd'hui à soixante jours, sur tout le
cabinet, en un appel quel que soit le nombre de dossiers. **Écarter les lignes de statut `annulée`**
— l'outil les rend, et une audience annulée n'est pas une audience à venir. Un dossier dont la seule
audience est annulée est précisément un dossier sans prochaine étape.

Si cet appel est tronqué ou échoue, **ce déclencheur ne se déclenche pas** : sans la liste des
audiences, on ne peut pas affirmer qu'un dossier n'en a aucune.

**La limite, et elle est volontaire.** La troisième condition emploie le drapeau du rapport de
couverture, pas une lecture des étapes : `list_protocol_steps` est par dossier, donc vérifier vraiment
coûterait un appel par dossier. Un dossier dont le protocole existe mais dont la prochaine étape tombe
dans trois mois **ne sera pas signalé**. C'est le sens sûr de l'erreur.

### c. Une prescription à moins de 60 jours

Source : `get_agenda.prescription_alerts`. Employer `prescription_date_effective` lorsqu'elle est
présente, et vérifier `last_action_differs` avant d'écrire une date.

Inutile d'écarter les dossiers interrompus ou imprescriptibles : **le modèle les a déjà retirés des
alertes** — une alerte présente est une alerte qui court. Ce qui mérite attention est
`prescription_status: "a_verifier"`, qui veut dire « alerté, mais le délai n'a pas pu être calculé » :
créer la tâche, et écrire dans la note que la date est à vérifier à la source plutôt que de citer une
échéance.

### d. Une étape de protocole échue

Source : `get_agenda.urgent_protocol_steps`, sur `status` **dérivé** — jamais `status_stored`, qui
peut porter un « en_retard » vieux de plusieurs mois. `status_differs: true` marque l'écart.

## 4. D'où vient le `dossier_id`

`create_task` veut un **identifiant** (UUID), pas un numéro de dossier ; un numéro passé là est
**refusé**, jamais deviné. Et un `dossier_id` omis classe la tâche sous « Général », détachée du
dossier qu'elle concerne. Il faut donc le vrai identifiant, et savoir d'où il vient :

| Déclencheur | Source de l'identifiant |
|---|---|
| c. prescription | `dossier_id` sur la ligne d'alerte |
| d. étape échue | `dossier_id` sur la ligne d'étape |
| b. sans prochaine étape | `dossier_id` sur l'item du rapport de couverture |
| a. fil sans réponse | **aucun** — le balayage ne rend qu'un chemin de classement |

Pour le seul cas (a), la correspondance numéro vers identifiant vient de l'appel 4 du corps :

```
list_dossiers(status: "actif", limit: 50)
```

Un appel, une quinzaine de lignes, et il sert aussi de parc de référence au déclencheur (b). Si le
numéro lu dans le chemin de classement n'y figure pas — dossier fermé, chemin sans numéro — **ne pas
créer** : le rapporter dans la rubrique des courriels.

## 5. Les titres et les champs

Le titre est le mécanisme de déduplication. Il doit être **déterministe** : les mêmes faits doivent
produire exactement la même chaîne demain.

```
[Auto] Répondre — 2026-014 — Me Gagnon, 21 août
[Auto] Sans prochaine étape — 2026-014
[Auto] Prescription — 2026-035 — 2026-09-12
[Auto] Étape échue — 2026-012 — dénonciation des moyens préliminaires
```

Règles, sans exception :

- Le préfixe `[Auto]` est toujours là. Il distingue à l'œil ce que le breffage a créé de ce que le
  juriste a écrit, et il rend la liste filtrable.
- Le **numéro de dossier** suit immédiatement, jamais l'identifiant technique.
- Le dernier segment est **stable dans le temps** : une date d'échéance, un nom de correspondant, un
  intitulé d'étape. Jamais un compte de jours, jamais « depuis 4 jours » — cela change chaque matin et
  la déduplication ne tiendrait pas une nuit.

Champs à poser avec le titre :

| Champ | Valeur |
|---|---|
| `dossier_id` | l'identifiant, selon la table de la section 4. Jamais un numéro |
| `due_date` | l'échéance quand il y en a une (prescription, étape) ; absente sinon |
| `priority` | `haute` pour une prescription, `normale` pour le reste |
| `category` | `suivi`, sauf `correspondance` pour un fil sans réponse |
| `idempotency_key` | `auto-AAAA-MM-JJ-` suivi d'un identifiant court du candidat |

Le statut n'est pas à poser : une tâche créée est toujours `à_faire`.

⚠ **Une clé d'idempotence déjà servie avec des arguments DIFFÉRENTS fait REFUSER l'appel**, en
français et explicitement — elle ne rejoue rien en silence. Deux candidats distincts doivent donc
porter deux clés distinctes, sans quoi le second est refusé.

## 6. Les limites de l'exécution

**Trois tâches par exécution, au maximum.** Chaque création coûte deux appels de modèle — la
vérification par dossier, puis l'écriture — et le plafond du tour est à 24. Au-delà de trois,
s'arrêter et écrire dans la note combien de candidats n'ont pas été créés.

**Ordre de priorité** si les candidats dépassent trois : les prescriptions d'abord, puis les étapes
échues, puis les fils sans réponse, puis les dossiers sans prochaine étape.

**La note passe avant les tâches.** Si le budget d'appels se resserre, on abandonne les créations et
on décrit les candidats dans la note. Des tâches permanentes créées sans note ni courriel sont
exactement l'inverse de ce que ce volet sert.

**Une création qui échoue ne se réessaie pas.** Le noter et passer au candidat suivant. Un réessai
coûte un appel et risque le doublon.

## 7. Ce qui se rapporte

Une rubrique de la note, toujours présente, même vide.

```
### Tâches créées ce matin

- **2026-035** · [Auto] Prescription — 2026-09-12
- **2026-014** · [Auto] Répondre — Me Gagnon, 21 août
Deux créées sur 2 candidats retenus.
```

Les jours sans création : « Aucune tâche créée. » — et, si c'est parce qu'une lecture a échoué ou
qu'un plafond a mordu, le dire, sans quoi le silence se lit comme « aucun candidat ».

Chaque tâche créée est nommée. Le juriste doit pouvoir, en lisant la note, savoir exactement ce qui
est apparu dans sa liste sans avoir à l'ouvrir — et retirer d'un geste ce qui n'avait pas lieu d'être.
