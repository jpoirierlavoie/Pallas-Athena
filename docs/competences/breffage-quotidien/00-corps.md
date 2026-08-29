# Breffage quotidien

Cette compétence produit un document unique : la note de breffage, déposée sous « Général ».
Elle s'exécute normalement sans personne devant l'écran. Tout ce qui suit découle de cette
contrainte.

Un breffage n'est pas un tableau de bord. Il ne récite pas ce qui va bien. Il nomme ce qui appelle
une décision aujourd'hui, et il se tait sur le reste.

Deux cadences la partagent. Le **breffage quotidien**, les jours ouvrables, tient les volets 1 à 6.
La **passe hebdomadaire du lundi** tient les volets 7 et 8, qui sont trop chers pour tourner tous
les jours. La consigne de la tâche dit laquelle vous exécutez.

## La règle qui gouverne tout le reste

**Un volet incomplet se déclare incomplet.** Chaque outil signale lui-même ses angles morts —
`checks_skipped`, `data_completeness`, `truncated`, `next_cursor`, `folder_labels_complete`,
`reconciliation_never_performed`. Un breffage qui tait ces signaux se lit comme un breffage complet
et devient un piège : le lecteur en conclut qu'un dossier est en règle alors que la vérification
n'a pas tourné.

Un balayage écourté n'est jamais un balayage propre. La rubrique « Ce qui n'a pas été vérifié » est
la seule rubrique obligatoire de la note, même les jours où elle est vide.

## Le plafond d'appels, et ce qu'il impose

Chaque aller-retour d'outil coûte **un appel de modèle**, et le tour meurt à 24. Un tour mort ne
dépose rien : pas de note, pas de courriel, et l'occurrence du jour est déjà marquée — **rien ne
recommence avant la prochaine date planifiée**.

Le budget nominal est de 13 à 16 appels. Il tient si vous ne paginez pas au-delà du nécessaire.
S'il se resserre, l'ordre d'abandon est celui-ci, du premier sacrifié au dernier :

1. le volet financier détaillé (garder les trois totaux) ;
2. la passe des courriels au-delà du premier appel ;
3. les tâches créées (les décrire dans la note plutôt que les créer).

**La note se dépose toujours.** Elle est le livrable ; tout le reste est son contenu. Un volet
abandonné se déclare dans « Ce qui n'a pas été vérifié », il ne disparaît pas en silence.

## Ce que la compétence n'écrit pas

Deux sortes d'écriture seulement : la note de breffage, et au plus cinq tâches selon
`taches-auto.md`. Rien d'autre.

**Interdits absolus, même si l'outil répond :**

- `mail_draft` — déposer un brouillon dans Outlook n'est pas le travail d'un breffage.
- `mail_file_to_dossier` — il verse des documents **permanents**, sans clé d'idempotence, et aucun
  outil de cette conversation ne peut les retirer.
- toute écriture sur un dossier, un contact, une entrée de temps, une dépense.

Ces deux premiers outils **ne sont pas verrouillés** : ils répondront si vous les appelez. C'est
précisément pourquoi l'interdit est écrit ici.

Aucun calcul de délai à la main. Les échéances rapportées sont celles que les outils retournent. Si
un délai de rigueur paraît en jeu et qu'aucun outil ne le porte, le dire comme question, jamais
comme date.

## Ordre des appels

L'ordre est choisi pour le coût : les trois premiers donnent l'essentiel.

**1. `get_agenda`** — un seul appel, et c'est le cœur. Audiences à venir, tâches urgentes, étapes de
protocole urgentes, alertes de prescription, statistiques. `days_ahead` vaut 14 ; 21 le vendredi,
30 au retour d'une absence.

La fenêtre s'ouvre à **minuit, heure de Montréal** : une audience de ce matin y figure encore. Tous
les drapeaux de retard emploient ce même jour montréalais.

⚠ **Les alertes de prescription ont un horizon FIXE de 60 jours**, indépendant de `days_ahead`.
Porter la fenêtre à 21 ou 30 ne les élargit pas d'un jour. Ne le laissez pas entendre dans la note.

⚠ **`urgent_tasks` n'est pas votre liste de tâches.** Il exclut structurellement les tâches **sans
date d'échéance** et plafonne à 50. Pour la vraie situation, voir l'appel 3.

Dans les alertes, `last_action_date` est le dernier jour juridique **au plus tard** à l'échéance —
pas la date à laquelle une action aurait été prise. Vérifier `last_action_differs` avant de le
rapporter, sans quoi on annonce une date qui n'est celle de rien.

**2. `get_coverage_report`** avec `status: "actif"` et **`limit: 50`**. Le parc du cabinet tient
dans un appel, et à cette limite la réponse porte non seulement `summary` mais les **items dossier
par dossier** — dont `PROTO_ABSENT`, dont le volet 6 a besoin. Ne paginez pas ; si `truncated` est
vrai, dites-le et travaillez sur ce que vous avez.

Lire `scope.checks_skipped`, `scope.dossiers_examined` et `data_completeness` avant toute
conclusion. Un contrôle suspendu parce qu'une source n'a pas pu être lue n'est pas un contrôle
réussi.

**3. `list_tasks`** sans `dossier_id` et avec **`include_completed: true`**. Un seul appel qui sert
deux fins : la situation réelle de vos tâches ouvertes — celle que `get_agenda` sous-déclare — et
le corpus de déduplication du volet 6. Le drapeau `include_completed` n'est pas facultatif : sans
lui, une tâche que vous avez terminée hier renaît demain matin.

**4. `get_billing_snapshot`** sans `dossier_id`, puis **`get_trust_snapshot`**, qui ne prend
**aucun argument** — lui en passer un fait échouer l'appel.

**5. Courriels** — voir `courriels.md`. Un seul balayage par date, sur toute la boîte.

**6. Tâches à créer** — voir `taches-auto.md`. Au plus cinq, après la lecture de l'appel 3. Un seul
déclencheur y coûte un appel de lecture supplémentaire (`list_hearings`, tout le cabinet) ; les trois
autres se calculent sur ce qui est déjà en main.

**7. Hygiène en attente** (lundi) — `get_coverage_report` avec `status: "en_attente"`. Les dossiers
en attente dérivent sans que personne les regarde.

**8. Dossiers dormants** (lundi) — voir `dormance.md`. Le volet le plus cher du lot.

## Les volets

### Audiences et échéances

C'est le volet qui commande le reste : en tête de la note, et le seul qui ne s'abrège jamais.

Trois strates, dans cet ordre. **Ce qui est en retard** — étapes échues, tâches dépassées, alertes
franchies. **Ce qui tombe aujourd'hui et demain.** **Ce qui vient dans la fenêtre**, une ligne par
élément.

Une audience se rapporte avec sa date, son tribunal, son dossier et son intitulé abrégé. Une étape
de protocole se rapporte avec son échéance **dérivée**, jamais avec le mot inscrit au document :
`status` gouverne, `status_stored` n'est que de la provenance, et `status_differs: true` marque
l'écart. Un « en_retard » resté sur le document depuis des mois n'est pas une nouvelle du jour.

**Les alertes de prescription passent en tête, quelle que soit leur date.** Un délai de prescription
ne se rattrape pas, et une alerte à cinquante jours vue aujourd'hui vaut mieux qu'une alerte à cinq
jours vue trop tard.

### Vos tâches

De l'appel 3, pas de `get_agenda`. Trois choses : ce qui est en retard (`is_overdue`), ce qui tombe
aujourd'hui, et le nombre de tâches ouvertes **sans date** — celles-là n'apparaissent nulle part
ailleurs et sont exactement celles qu'on oublie.

Ne recopiez pas la liste. Le breffage donne les retards nommés, le reste en nombre.

### Volet financier

Trois chiffres suffisent : le travail non facturé, l'encours des factures, le total détenu en
fidéicommis. Le détail par dossier ne monte que lorsqu'il porte une décision.

`total_hours` compte **toutes** les heures, y compris non facturables ; les montants non facturés ne
comptent que le facturable non encore porté à une facture. Ne pas confondre les deux dans une même
phrase.

Trois signaux du fidéicommis montent toujours, quel que soit leur âge, parce qu'ils touchent à des
obligations comptables : `reconciliation_never_performed`, `reconciliation_overdue`, et un chèque en
circulation dont la date d'émission commence à dater. Les rapporter comme des faits observés, sans
les qualifier en droit comptable.

Le volet est hebdomadaire par nature. Le produire tous les jours en entier le rend invisible. Les
jours ordinaires : une ligne de totaux, et rien de plus si rien ne bouge.

### Courriels

Méthode complète dans `courriels.md`. Ce qui monte dans la note : les fils qui attendent une
réponse de vous, et les messages du jour qui appellent une décision. Jamais un compte de non-lus.

### Tâches créées

Méthode complète dans `taches-auto.md`. Ce qui monte dans la note : chaque tâche créée, nommée avec
son dossier, dans sa propre rubrique — et « Aucune tâche créée. » les jours où il n'y en a pas. Le
juriste doit pouvoir savoir ce qui est apparu dans sa liste sans avoir à l'ouvrir.

### Hygiène des dossiers

Le `summary` du balayage, en deux nombres et une liste courte : combien de dossiers portent au moins
une constatation, et la ventilation par code.

**La distinction de sévérité est la seule qui compte.** Un `manquement` désigne ce que le dossier est
**tenu** d'avoir — la vérification des conflits et celle de l'identité sont des obligations
déontologiques, non des préférences de saisie. Un `signalement` mérite un coup d'œil et rien de plus.
Les mélanger efface la seule information utile.

Deux constatations méritent d'être nommées dès qu'elles apparaissent : `PROTO_REGIME` — le régime du
C.p.c. dont relève le gabarit ne gouverne pas le forum du dossier, ce qui rend toutes ses échéances
suspectes — et `TACHE_OUVERTE_DOSSIER_FERME`, qui arrive par `cross_scope_findings` et qu'aucun
filtre de statut ne ferait remonter.

### Dossiers dormants — lundi seulement

Un dossier actif sans mouvement depuis des semaines est soit oublié, soit porteur d'un statut qui
ment. Les deux se corrigent, et aucun ne se voit dans l'agenda. Méthode et réserves dans
`dormance.md`.

## La note

Le gabarit exact est dans `gabarit-note.md`. Points structurants :

Le titre porte la date en clair — « Breffage du 27 août 2026 » — de sorte que la liste des notes
« Général » se lise comme un journal.

La clé d'idempotence est **dérivée de la date**, jamais aléatoire : `breffage-AAAA-MM-JJ`. Une
exécution qui repart après une panne retrouve alors le résultat du premier appel au lieu de déposer
une seconde note. Il n'existe aucune déduplication automatique, et la note est indélébile depuis le
clavardage.

Plafond de 20 000 caractères, et l'appel **échoue** au-delà plutôt que de tronquer. Un breffage qui
approche ce plafond est un breffage qui recopie au lieu de trier : abréger, ne pas scinder.

Si l'appel semble échouer, relire avec `list_notes` — sans `dossier_id`, ce qui donne les notes
« Général » — avant de réessayer. Un réessai aveugle crée un doublon permanent.

## Contrôle avant dépôt

Six vérifications sur la note produite, dans l'ordre :

1. **Sévérité** — les manquements sont-ils séparés des signalements ?
2. **Provenance** — chaque échéance vient-elle d'un outil, plutôt que d'un calcul fait ici ?
3. **Tâches créées** — la rubrique les nomme-t-elle toutes, avec leur dossier ?
4. **Angles morts** — `checks_skipped`, `data_completeness`, `truncated`,
   `folder_labels_complete` : la dernière rubrique les porte-t-elle tous ?
5. **Silence utile** — les volets sans nouvelle sont-ils réduits à une ligne ?
6. **Longueur** — la note se lit-elle en deux minutes sur un téléphone ?

## Raccord avec les autres compétences

Une question de droit soulevée par un breffage relève de `recherche-juridique-quebecoise`. Le
breffage ne tranche rien : il signale qu'une question se pose.

L'audit d'un dossier en particulier ne se fait pas ici. Les outils qu'il demande ne sont pas offerts
au clavardage ; ce travail se mène depuis claude.ai.

## Fichiers de référence

Le bloc système en donne la liste et l'identifiant à employer. Les lire au moment utile, pas avant.

- `gabarit-note.md` — la structure de la note, rubrique par rubrique, avec les règles d'abrègement.
  À lire au moment de rédiger.
- `courriels.md` — le balayage par date, l'attribution par dossier de classement, la détection des
  fils sans réponse. À lire avant le volet 5.
- `taches-auto.md` — les quatre déclencheurs, les titres, la déduplication et ses limites. À lire
  avant le volet 6.
- `dormance.md` — la dérivation des dossiers dormants et le piège d'`updated_at`. À lire avant le
  volet 8, le lundi.
