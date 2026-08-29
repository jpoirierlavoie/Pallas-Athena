# Le gabarit de la note de breffage

La structure exacte, rubrique par rubrique, et les règles qui décident de ce qui monte et de ce qui
reste dehors.

## Table des matières

1. L'ossature
2. Règles d'abrègement
3. Comment s'écrit une ligne
4. La rubrique obligatoire
5. Le dépôt

---

## 1. L'ossature

Reproduire les rubriques dans cet ordre. Une rubrique sans nouvelle s'écrit quand même, en une
ligne : une rubrique absente ne se distingue pas d'un volet qui n'a pas tourné.

```
# Breffage du [date en clair]

*Fenêtre : [n] jours · Balayage : [n] dossiers actifs examinés · [horodatage]*

## En retard

## Aujourd'hui et demain

## Prescription

## La fenêtre

## Vos tâches

## Courriels

## Cabinet

## Hygiène des dossiers

## Dossiers dormants

## Tâches créées ce matin

## Ce qui n'a pas été vérifié
```

**En retard** vient en premier même les jours où il est vide, parce que c'est la rubrique que le
lecteur cherche des yeux. « Rien en retard. » est une phrase complète et une bonne nouvelle.

**Prescription** est détachée de « La fenêtre » et remonte au-dessus d'elle, quelle que soit la date
des alertes. Un délai de prescription ne se rattrape pas ; une étape de protocole manquée se plaide.

**Vos tâches** ne répète pas ce que les trois premières rubriques ont déjà nommé. Elle porte ce
qu'elles ne peuvent pas montrer : le nombre de tâches ouvertes **sans date d'échéance** — celles-là
n'apparaissent dans aucun agenda, et ce sont exactement celles qu'on oublie — et le total ouvert.
Deux lignes, jamais la liste.

**Courriels** se loge après l'agenda et avant le financier. Deux sous-listes seulement : les fils
qui attendent une réponse, et le courrier de la fenêtre qui appelle une décision. Voir
`courriels.md`.

**Cabinet** porte le volet financier. Les jours ordinaires, trois nombres sur une ligne.

**Dossiers dormants** ne paraît que les jours où le volet a tourné — le lundi. Les autres jours, la
rubrique est retirée, et son absence est notée dans la dernière rubrique.

**Tâches créées ce matin** est toujours présente, même vide. Le juriste doit pouvoir savoir, en
lisant la note, exactement ce qui est apparu dans sa liste de tâches sans avoir à l'ouvrir. Voir
`taches-auto.md`.

## 2. Règles d'abrègement

Le breffage se lit sur un téléphone, à jeun, avant le premier café. Deux minutes, plafond.

**Une ligne par élément.** Pas de sous-listes, pas de paragraphes explicatifs. Ce qui demande un
paragraphe demande une note distincte, pas une rubrique gonflée.

**Cinq lignes par rubrique**, au-delà desquelles on agrège : « et 7 autres tâches échues ».
L'exception est « En retard », qui ne s'agrège jamais — un élément en retard vaut d'être nommé,
même le douzième.

**Aucune répétition d'une rubrique à l'autre.** Une audience de demain figure dans « Aujourd'hui et
demain », pas aussi dans « La fenêtre ». Une tâche échue figure dans « En retard », pas aussi
ailleurs. Un fil sans réponse qui a produit une tâche figure dans « Courriels » **et** dans « Tâches
créées » — c'est la seule répétition admise, parce que les deux disent des choses différentes : ce
qui attend, et ce qui a été fait à ce sujet.

**Rien de ce qui n'appelle pas de décision.** Le nombre de dossiers ouverts ne bouge pas d'un jour à
l'autre et n'a rien à faire dans un breffage quotidien ; il monte le lundi, ou quand il change.

## 3. Comment s'écrit une ligne

Chaque ligne porte quatre choses et pas davantage : **quand**, **quoi**, **quel dossier**, **quelle
suite**. Le dossier se désigne par son numéro et un intitulé abrégé, jamais par son identifiant.

```
- **2026-035** · Prescription au 12 sept. — dernier jour juridique le 11 (jour ouvrable
  précédent). Williams c. Doidge.
- **2026-012** · Protocole : dénonciation des moyens préliminaires échue depuis le 19 août.
- **2026-045** · Audience le 3 sept., 9 h, Cour supérieure, Montréal.
- **2026-014** · Me Gagnon, 21 août — mise en demeure, sans réponse depuis 4 jours.
```

Quatre interdits de rédaction.

**Ne jamais écrire une date qu'un outil n'a pas rendue.** Si l'échéance se déduit, elle s'écrit
comme déduction et porte sa base : « six mois de la signification du 4 mars, sous réserve de
l'article 173 al. 3 C.p.c. » Une date nue est lue comme une date vérifiée.

**Ne jamais qualifier en droit.** Le breffage dit qu'une étape est échue ; il ne dit pas qu'un droit
est éteint. La qualification appelle une lecture du dossier et, souvent, une recherche.

**Ne jamais présenter `last_action_date` comme la date d'une action.** C'est le dernier jour
juridique au plus tard à l'échéance. Sur une échéance tombant un jour ouvrable, il égale
`prescription_date` — et `last_action_differs` dit s'il y a écart.

**Ne jamais citer le corps d'un courriel.** Le sujet, abrégé, suffit. Un courriel est couvert par le
secret professionnel, et la note se synchronise sur un téléphone.

## 4. La rubrique obligatoire

« Ce qui n'a pas été vérifié » se rédige à partir de ce que les outils ont eux-mêmes déclaré, et non
de ce dont on se souvient.

| Source du signal | Ce qu'il faut écrire |
|---|---|
| `scope.checks_skipped` non vide | Les contrôles suspendus, nommés par leur code, et le fait qu'un dossier sans constatation n'est donc pas un dossier propre |
| `data_completeness` avec un faux | Le pan de données illisible — index des protocoles, contacts clients — et `kyc_reason` lorsqu'il est fourni |
| `truncated: true` non paginé | Le volet est partiel, et de combien |
| `folder_labels_complete: false` | Des courriels n'ont pas pu être rattachés à leur dossier de classement |
| `deleted_items_excluded: null` | La corbeille n'a pas pu être identifiée : des courriels supprimés peuvent figurer |
| `list_tasks` tronqué | **Aucune tâche n'a été créée ce jour** — la déduplication était partielle |
| Un appel en échec | L'outil, le volet perdu. Ne pas réessayer trois fois en silence |
| Volet non exécuté ce jour | Les dormants les jours sans passe, le financier les jours ordinaires |
| Détection des fils sans réponse | Qu'elle couvre sept jours, et que la traîne est vue le lundi |

Cette rubrique n'est jamais retirée. Les jours où tout a tourné, elle porte une ligne : « Tous les
volets ont tourné ; aucun contrôle suspendu. »

## 5. Le dépôt

`create_note`, sans `dossier_id` — la note se classe alors sous « Général ».

| Paramètre | Valeur |
|---|---|
| `title` | `Breffage du [date en clair]` |
| `category` | `autre` |
| `idempotency_key` | `breffage-AAAA-MM-JJ` — dérivée de la date, jamais aléatoire |
| `content` | Markdown, français, sous 20 000 caractères |

**La passe du lundi dépose sa PROPRE note**, titrée « Passe hebdomadaire du [date] », avec la clé
`hebdo-AAAA-MM-JJ`. La clé doit différer de celle du breffage de 6 h : la déduplication porte sur le
couple outil + clé, donc réutiliser `breffage-AAAA-MM-JJ` le même jour ferait rejouer la note du
matin et **la passe hebdomadaire ne serait jamais écrite**, sur un compte rendu de succès. Sa note ne
porte que ses propres rubriques — hygiène des dossiers en attente, dossiers dormants, courriels
anciens, et la rubrique obligatoire.

Aucune balise HTML brute : elles sont rejetées. Markdown simple, et les tableaux seulement
lorsqu'ils portent des colonnes réelles.

Une ligne de provenance « Ajouté par Claude » est apposée automatiquement. Ne pas en écrire une
seconde.

**Avant de réessayer un dépôt qui semble avoir échoué**, appeler `list_notes` sans `dossier_id` et
chercher la note du jour. La clé d'idempotence couvre vingt-quatre heures et protège contre le
doublon ; passé ce délai, rien ne le fait. La note est permanente : le clavardage ne peut ni
l'éditer ni l'effacer, et elle se synchronise sur le téléphone.

**Si le dépôt échoue pour dépassement du plafond**, ne pas scinder en deux notes. Reprendre
l'abrègement de la section 2 : un breffage de 20 000 caractères a cessé de trier.
