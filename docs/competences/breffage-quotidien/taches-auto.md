# Les tâches créées par le breffage

Le breffage crée des tâches. C'est une exception à la discipline d'écriture, elle est étroite, et
elle porte un risque qui ne se supprime pas — seulement se borne. Ce fichier dit quoi créer,
comment ne pas créer deux fois, et où le mécanisme cède.

## Table des matières

1. Le risque, énoncé d'abord
2. La lecture de déduplication
3. Les quatre déclencheurs
4. Les titres
5. Les limites de l'exécution
6. Ce qui se rapporte

---

## 1. Le risque, énoncé d'abord

**Rien ne peut supprimer une tâche depuis cette conversation.** Aucun outil du clavardage n'efface
une tâche. Une tâche créée à tort reste, et c'est le juriste qui la retire à la main dans
l'application.

**La clé d'idempotence ne protège pas d'un jour à l'autre.** Elle vit 24 heures. Une clé stable
comme `tache-prescription-2026-035` expire pendant la nuit : le lendemain, la même clé crée une
**seconde** tâche. L'idempotence protège d'un rejeu du même matin, jamais du matin suivant.

**`list_tasks` n'a pas de recherche par titre.** La déduplication est donc une comparaison faite
ici, sur des titres lus. Elle est exacte tant que les titres le sont.

Il en découle une conséquence à accepter : **si le juriste renomme une tâche créée ici, le breffage
du lendemain en crée une seconde.** C'est le prix du volet. Il se paie en désordre visible, jamais
en travail perdu.

## 2. La lecture de déduplication

Elle a déjà eu lieu : c'est l'appel 3 du corps.

```
list_tasks(include_completed: true, limit: 50)
```

Sans `dossier_id` — donc tout le cabinet, « Général » compris.

**`include_completed: true` n'est pas facultatif.** La vue par défaut omet les tâches `terminée`.
Sans ce drapeau, une tâche que le juriste a terminée hier est absente de la lecture, donc recréée
demain matin, et de nouveau le surlendemain. La règle est qu'**une tâche terminée ne revient
jamais** : la terminer veut dire « je m'en suis occupé », et le breffage le croit sur parole.

Si l'appel rend `truncated`, la lecture est partielle : **ne créer aucune tâche ce jour-là** et le
dire dans « Ce qui n'a pas été vérifié ». Créer sur une lecture partielle, c'est créer des doublons
à l'aveugle.

Un candidat se crée seulement si **aucune** tâche lue — ouverte ou terminée — ne porte déjà son
titre exact.

## 3. Les quatre déclencheurs

Deux d'entre eux nomment ce que rien d'autre ne voit. Les deux autres redoublent l'agenda, et c'est
assumé : une échéance vue tous les matins sans jamais devenir une tâche finit par se fondre dans le
décor.

### a. Un fil qui attend votre réponse

Source : le balayage des courriels, section 4 de `courriels.md`. Le dernier message du fil ne vient
pas du juriste et date de plus de trois jours juridiques.

**Le seul signal qui n'existe nulle part ailleurs dans Athéna.** L'agenda ne voit pas un courriel ;
le dossier ne sait pas qu'une question est restée sans réponse.

Ne pas créer pour un fil non attribué à un dossier : sans numéro de dossier, le titre n'est pas
stable, donc la déduplication ne tient pas. Le rapporter dans la note à la place.

### b. Un dossier sans prochaine étape

Un dossier `actif` qui réunit les trois : aucune tâche ouverte, aucune audience à venir, et
`PROTO_ABSENT` au rapport de couverture.

Rien n'est prévu sur ce dossier. Un agenda ne peut pas le montrer — un agenda montre ce qui est
prévu, et ici il n'y a rien.

Les trois sources sont déjà en main, sauf une : les tâches ouvertes viennent de l'appel 3, le
`PROTO_ABSENT` des `items` du rapport de couverture — qui **omet les dossiers sans constatation**, ce
qui est exactement ce qu'on veut ici. Les audiences demandent **un appel de plus** :

```
list_hearings(limit: 50)
```

Sans dossier ni dates : la fenêtre par défaut couvre d'aujourd'hui à soixante jours, sur tout le
cabinet. **Écarter les lignes de statut `annulée`** — l'outil les rend, et une audience annulée n'est
pas une audience à venir. Un dossier dont la seule audience est annulée est précisément un dossier
sans prochaine étape.

Si l'appel est tronqué ou échoue, **ce déclencheur ne se déclenche pas** : sans la liste des
audiences, on ne peut pas affirmer qu'un dossier n'en a aucune. Le dire dans « Ce qui n'a pas été
vérifié ».

**La limite, et elle est volontaire.** La troisième condition emploie le drapeau du rapport de
couverture, pas une lecture des étapes : `list_protocol_steps` est par dossier, donc vérifier
vraiment coûterait un appel par dossier. Un dossier dont le protocole existe mais dont la prochaine
étape tombe dans trois mois **ne sera pas signalé**. C'est le sens sûr de l'erreur.

### c. Une prescription à moins de 60 jours

Source : `get_agenda.prescription_alerts`. Employer `prescription_date_effective` lorsqu'elle est
présente, et vérifier `last_action_differs` avant d'écrire une date.

Ne rien créer lorsque `prescription_status` vaut `interrompue` ou `imprescriptible` : le délai ne
court pas.

### d. Une étape de protocole échue

Source : `get_agenda.urgent_protocol_steps`, sur `status` **dérivé** — jamais `status_stored`, qui
peut porter un « en_retard » vieux de plusieurs mois.

## 4. Les titres

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
- Le dernier segment est **stable dans le temps** : une date d'échéance, un nom de correspondant,
  un intitulé d'étape. Jamais un compte de jours, jamais « depuis 4 jours » — cela change chaque
  matin et la déduplication ne tiendrait pas une nuit.

Champs à poser avec le titre :

| Champ | Valeur |
|---|---|
| `dossier_id` | celui du dossier concerné, toujours |
| `due_date` | l'échéance quand il y en a une (prescription, étape) ; absente sinon |
| `priority` | `haute` pour une prescription, `normale` pour le reste |
| `category` | `suivi`, sauf `correspondance` pour un fil sans réponse |
| `idempotency_key` | `auto-AAAA-MM-JJ-` suivi d'un identifiant court du candidat |

Le statut n'est pas à poser : une tâche créée est toujours `à_faire`.

## 5. Les limites de l'exécution

**Cinq tâches par exécution, au maximum.** Au-delà, s'arrêter et écrire dans la note combien de
candidats n'ont pas été créés. Cinq créations valent cinq appels de modèle, et le plafond du tour
est à 24.

**Ordre de priorité** si les candidats dépassent cinq : les prescriptions d'abord, puis les étapes
échues, puis les fils sans réponse, puis les dossiers sans prochaine étape.

**Une création qui échoue ne se réessaie pas.** Le noter et passer au candidat suivant. Un réessai
coûte un appel et risque le doublon.

## 6. Ce qui se rapporte

Une rubrique de la note, toujours présente, même vide.

```
### Tâches créées ce matin

- **2026-035** · [Auto] Prescription — 2026-09-12
- **2026-014** · [Auto] Répondre — Me Gagnon, 21 août
Aucune autre : 2 candidats retenus sur 2.
```

Les jours sans création : « Aucune tâche créée. »

Chaque tâche créée est nommée. Le juriste doit pouvoir, en lisant la note, savoir exactement ce qui
est apparu dans sa liste sans avoir à l'ouvrir — et retirer d'un geste ce qui n'avait pas lieu
d'être.
