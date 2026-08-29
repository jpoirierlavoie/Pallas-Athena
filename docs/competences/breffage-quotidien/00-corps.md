# Breffage quotidien

Cette compétence produit un document unique : la note de breffage, déposée sous « Général ». Elle
s'exécute normalement sans personne devant l'écran. Tout ce qui suit découle de cette contrainte.

Un breffage n'est pas un tableau de bord. Il ne récite pas ce qui va bien. Il nomme ce qui appelle
une décision aujourd'hui, et il se tait sur le reste.

Deux cadences la partagent. Le **breffage quotidien**, les jours ouvrables, tient les volets 1 à 7.
La **passe hebdomadaire du lundi** tient les volets 8 à 10, trop chers pour tourner tous les jours.
La consigne de la tâche dit laquelle vous exécutez.

## La règle qui gouverne tout le reste

**Un volet incomplet se déclare incomplet.** Chaque outil signale lui-même ses angles morts —
`checks_skipped`, `data_completeness`, `truncated`, `next_cursor`, `dossier_status_matched`,
`folder_labels_complete`, `deleted_items_excluded`, `reconciliation_never_performed`. Un breffage qui
tait ces signaux se lit comme un breffage complet et devient un piège : le lecteur en conclut qu'un
dossier est en règle alors que la vérification n'a pas tourné.

Un balayage écourté n'est jamais un balayage propre. La rubrique « Ce qui n'a pas été vérifié » est la
seule rubrique obligatoire de la note, même les jours où elle est vide.

**Un corollaire qui vaut pour toutes les listes : zéro ligne peut vouloir dire « la lecture a
échoué ».** Plusieurs lectures avalent une panne en liste vide et rendent alors `count: 0`,
`truncated: false` — indiscernable d'un succès. Quand le zéro est invraisemblable (le cabinet a
toujours des tâches ouvertes, toujours des dossiers actifs), le traiter comme une panne et le dire,
jamais comme un fait.

## D'où vient la date

**Vous ne connaissez pas la date.** Rien dans votre contexte ne la porte : ni la charte, ni la
consigne, ni le titre de la conversation. La seule source est la réponse du premier appel :
**`get_agenda` rend `window.from`, qui EST le jour civil de Montréal.**

Tout ce qui s'écrit ensuite en dépend — le titre de la note, sa clé d'idempotence, les clés des
tâches créées, le calcul de « depuis quatre jours ». Aucune date ne s'écrit avant cet appel, et aucune
ne s'invente.

C'est aussi pourquoi `days_ahead` vaut **14, toujours**. Faire varier la fenêtre selon le jour de la
semaine demanderait de connaître le jour AVANT l'appel qui l'apprend.

## Le plafond d'appels, et ce qu'il impose

Chaque aller-retour d'outil coûte **un appel de modèle**, et le tour meurt à 24. Un tour mort ne
dépose rien : pas de note, pas de courriel, et l'occurrence du jour est déjà marquée — **rien ne
recommence avant la prochaine date planifiée**.

Le budget nominal est de 9 appels sans création de tâche, 15 au pire. S'il se resserre, l'ordre
d'abandon est celui-ci, du premier sacrifié au dernier :

1. **les tâches à créer** — les décrire dans la note plutôt que les créer ;
2. le volet financier détaillé (garder les trois totaux) ;
3. la passe des courriels au-delà du premier appel.

**La note se dépose toujours, et elle passe avant toute écriture.** Elle est le livrable ; tout le
reste est son contenu. Jamais l'inverse : jusqu'à trois tâches permanentes créées sans note ni
courriel serait le pire résultat possible. Un volet abandonné se déclare dans « Ce qui n'a pas été
vérifié », il ne disparaît pas en silence.

## Ce que la compétence n'écrit pas

Deux sortes d'écriture seulement : la note de breffage, et au plus trois tâches selon
`taches-auto.md`. Rien d'autre.

**Quatorze outils d'écriture répondent sur ce tour ; onze demandent une autorisation, et l'auto-refus
protège ceux-là seulement.** Les autres s'exécutent sans rien demander. Les voici, tous interdits ici
sauf les deux nommés au paragraphe précédent :

- `mail_draft` — déposer un brouillon dans Outlook n'est pas le travail d'un breffage.
- `mail_file_to_dossier` — il verse des documents **permanents**, sans clé d'idempotence, et aucun
  outil de cette conversation ne peut les retirer.
- `complete_task` — il ne ferme pas qu'une tâche : il complète l'étape de protocole liée et peut
  clore le protocole entier, sans réouverture possible d'ici.
- `append_to_note`, `create_hearing`, `create_time_entry`, `create_expense`, `save_draft`,
  `record_signification`, `record_prescription_event` — hors sujet, et irréversibles d'ici.

Aucun de ces outils n'est verrouillé : ils **répondront** si vous les appelez. C'est précisément
pourquoi l'interdit est écrit ici.

Aucun calcul de délai à la main. Les échéances rapportées sont celles que les outils retournent. Si un
délai de rigueur paraît en jeu et qu'aucun outil ne le porte, le dire comme question, jamais comme
date.

## Ordre des appels

L'ordre est choisi pour le coût : les trois premiers donnent l'essentiel.

**1. `get_agenda`** avec `days_ahead: 14` — un seul appel, et c'est le cœur. Audiences à venir, tâches
urgentes, étapes de protocole urgentes, alertes de prescription, statistiques. **Et la date** (voir
plus haut).

La fenêtre s'ouvre à **minuit, heure de Montréal** : une audience de ce matin y figure encore. Tous
les drapeaux de retard emploient ce même jour montréalais.

⚠ **Les alertes de prescription ont un horizon FIXE de 60 jours**, indépendant de `days_ahead`. Ne le
laissez pas entendre autrement dans la note.

⚠ **`urgent_tasks` n'est pas votre liste de tâches.** Il exclut structurellement les tâches **sans
date d'échéance** et plafonne à 50. Pour la vraie situation, voir l'appel 3.

Dans les alertes, `last_action_date` est le dernier jour juridique **au plus tard** à l'échéance — pas
la date à laquelle une action aurait été prise. Vérifier `last_action_differs` avant de le rapporter,
sans quoi on annonce une date qui n'est celle de rien.

**2. `get_coverage_report`** avec `status: "actif"` et **`limit: 50`**. Le parc du cabinet tient dans
un appel, et à cette limite la réponse porte non seulement `summary` mais les **items dossier par
dossier** — dont `dossier_id` et `PROTO_ABSENT`, dont le volet 7 a besoin. Ne paginez pas ; si
`truncated` est vrai, dites-le et travaillez sur ce que vous avez.

Lire `scope.checks_skipped`, `scope.dossiers_examined` et `data_completeness` avant toute conclusion.
Un contrôle suspendu parce qu'une source n'a pas pu être lue n'est pas un contrôle réussi. Les
`items` **omettent les dossiers sans constatation** : un dossier absent est un dossier propre, pas un
dossier non examiné.

**3. `list_tasks`** avec `limit: 50`, sans `dossier_id` et **sans `include_completed`**. Un seul appel
qui sert deux fins : la situation réelle de vos tâches ouvertes — celle que `get_agenda` sous-déclare
— et le premier tri de la déduplication du volet 7. ⚠ Le drapeau `include_completed` est un piège
ici : il rendrait 50 tâches **terminées** et pas une seule ouverte. `taches-auto.md` §2 explique
pourquoi.

**4. `list_dossiers`** avec `status: "actif"` et `limit: 50`. Le parc, et la seule carte qui traduit
un **numéro** de dossier en **identifiant** — ce dont le volet 7 a besoin pour rattacher un courriel.

**5. `get_billing_snapshot`** sans `dossier_id`, puis **`get_trust_snapshot`**, qui ne prend **aucun
argument** — lui en passer un fait échouer l'appel.

**6. Courriels** — voir `courriels.md`. Un seul balayage par date, sur toute la boîte.

**7. Tâches à créer** — voir `taches-auto.md`. Au plus trois, chacune précédée de sa vérification par
dossier. Un appel de lecture supplémentaire est nécessaire (`list_hearings`, tout le cabinet).

**8. Hygiène en attente** (lundi) — `get_coverage_report` avec `status: "en_attente"`. Les dossiers en
attente dérivent sans que personne les regarde.

**9. Dossiers dormants** (lundi) — voir `dormance.md`. Le volet le plus cher du lot.

**10. Courriels anciens** (lundi) — le même outil que le volet 6, sur la tranche **plus ancienne** de
la fenêtre. Voir `courriels.md` §2.

## Les volets

### Audiences et échéances

C'est le volet qui commande le reste : en tête de la note, et le seul qui ne s'abrège jamais.

Trois strates, dans cet ordre. **Ce qui est en retard** — étapes échues, tâches dépassées, alertes
franchies. **Ce qui tombe aujourd'hui et demain.** **Ce qui vient dans la fenêtre**, une ligne par
élément.

Une audience se rapporte avec sa date, son tribunal, son dossier et son intitulé abrégé. Une étape de
protocole se rapporte avec son échéance **dérivée**, jamais avec le mot inscrit au document :
`status` gouverne, `status_stored` n'est que de la provenance, et `status_differs: true` marque
l'écart. Un « en_retard » resté sur le document depuis des mois n'est pas une nouvelle du jour.

**Les alertes de prescription passent en tête, quelle que soit leur date.** Un délai de prescription
ne se rattrape pas, et une alerte à cinquante jours vue aujourd'hui vaut mieux qu'une alerte à cinq
jours vue trop tard. Elles sont déjà filtrées des dossiers interrompus et imprescriptibles : une
alerte présente est une alerte qui court. `prescription_status: "a_verifier"` veut dire que le délai
n'a pas pu être calculé — le dire, sans citer de date.

### Vos tâches

De l'appel 3, pas de `get_agenda`. Trois choses : ce qui est en retard (`is_overdue`), ce qui tombe
aujourd'hui, et le nombre de tâches ouvertes **sans date** — celles-là n'apparaissent nulle part
ailleurs et sont exactement celles qu'on oublie.

Ne recopiez pas la liste. Le breffage donne les retards nommés, le reste en nombre. Et si l'appel a
rendu zéro ligne, dites que la lecture n'a pas abouti — pas « 0 tâche ».

### Volet financier

Trois chiffres suffisent : le travail non facturé, l'encours des factures, le total détenu en
fidéicommis. Le détail par dossier ne monte que lorsqu'il porte une décision.

`unbilled_hours` compte les heures **facturables non encore portées à une facture** — ce n'est pas un
total d'heures travaillées, et la réponse à l'échelle du cabinet n'en porte pas. Ne pas présenter
l'une pour l'autre.

Trois signaux du fidéicommis montent toujours, quel que soit leur âge, parce qu'ils touchent à des
obligations comptables : `reconciliation_never_performed`, `reconciliation_overdue`, et un chèque en
circulation dont la date d'émission commence à dater. Les rapporter comme des faits observés, sans les
qualifier en droit comptable.

Le volet est hebdomadaire par nature. Le produire tous les jours en entier le rend invisible. Les
jours ordinaires : une ligne de totaux, et rien de plus si rien ne bouge.

### Courriels

Méthode complète dans `courriels.md`. Ce qui monte dans la note : les fils qui attendent une réponse
de vous, et les messages du jour qui appellent une décision. Jamais un compte de non-lus.

### Tâches créées

Méthode complète dans `taches-auto.md`. Ce qui monte dans la note : chaque tâche créée, nommée avec
son dossier, dans sa propre rubrique — et « Aucune tâche créée. » les jours où il n'y en a pas, avec
la raison quand c'est une lecture qui a manqué. Le juriste doit pouvoir savoir ce qui est apparu dans
sa liste sans avoir à l'ouvrir.

### Hygiène des dossiers

Le `summary` du balayage, en deux nombres et une liste courte : combien de dossiers portent au moins
une constatation, et la ventilation par code.

**La distinction de sévérité est la seule qui compte.** Un `manquement` désigne ce que le dossier est
**tenu** d'avoir — la vérification des conflits et celle de l'identité sont des obligations
déontologiques, non des préférences de saisie. Un `signalement` mérite un coup d'œil et rien de plus.
Les mélanger efface la seule information utile.

Deux constatations méritent d'être nommées dès qu'elles apparaissent : `PROTO_REGIME` — le régime du
C.p.c. dont relève le gabarit ne gouverne pas le forum du dossier, ce qui rend toutes ses échéances
suspectes — et `TACHE_OUVERTE_DOSSIER_FERME`, qui arrive par `cross_scope_findings` et qu'aucun filtre
de statut ne ferait remonter.

### Dossiers dormants — lundi seulement

Un dossier actif sans mouvement depuis des semaines est soit oublié, soit porteur d'un statut qui
ment. Les deux se corrigent, et aucun ne se voit dans l'agenda. Méthode et réserves dans
`dormance.md`. Cette rubrique **n'apparaît jamais dans la note quotidienne** : elle appartient à la
note du lundi.

## La note

Le gabarit exact est dans `gabarit-note.md`. Points structurants :

Le titre porte la date en clair — « Breffage du 27 août 2026 » — de sorte que la liste des notes
« Général » se lise comme un journal.

La clé d'idempotence est **dérivée de la date**, jamais aléatoire : `breffage-AAAA-MM-JJ`. La passe du
lundi dépose sa propre note sous une clé distincte.

**Le mécanisme d'idempotence, une fois pour toutes.** Même clé + arguments **identiques** → le résultat
du premier appel est rendu, sans seconde écriture : c'est ce qui protège d'une reprise après panne.
Même clé + arguments **différents** → l'appel est **REFUSÉ**, explicitement et en français. Il n'existe
aucun cas où une clé réutilisée écrase ou remplace en silence. Une reprise dont le texte aura changé
tombe donc dans le second cas : relire avec `list_notes` — sans `dossier_id`, ce qui donne les notes
« Général » — plutôt que de conclure d'un refus que rien n'a été déposé.

Plafond de 20 000 caractères, et l'appel **échoue** au-delà plutôt que de tronquer. Un breffage qui
approche ce plafond est un breffage qui recopie au lieu de trier : abréger, ne pas scinder.

## Contrôle avant dépôt

Six vérifications sur la note produite, dans l'ordre :

1. **Sévérité** — les manquements sont-ils séparés des signalements ?
2. **Provenance** — chaque date vient-elle d'un outil, plutôt que d'un calcul fait ici ?
3. **Tâches créées** — la rubrique les nomme-t-elle toutes, avec leur dossier ?
4. **Angles morts** — `checks_skipped`, `data_completeness`, `truncated`, `dossier_status_matched`,
   `folder_labels_complete`, `deleted_items_excluded` : la dernière rubrique les porte-t-elle tous ?
5. **Silence utile** — les volets sans nouvelle sont-ils réduits à une ligne ?
6. **Longueur** — la note se lit-elle en deux minutes sur un téléphone ?

## Raccord avec les autres compétences

Une question de droit soulevée par un breffage relève de `recherche-juridique-quebecoise`. Le
breffage ne tranche rien : il signale qu'une question se pose.

L'audit d'un dossier en particulier ne se fait pas ici. Les outils qu'il demande ne sont pas offerts
au clavardage ; ce travail se mène depuis claude.ai.

## Fichiers de référence

Le bloc système en donne la liste et l'identifiant à employer. Les lire au moment utile, pas avant.

- `gabarit-note.md` — la structure des deux notes, rubrique par rubrique, avec les règles
  d'abrègement. À lire au moment de rédiger.
- `courriels.md` — le balayage par date, l'attribution par dossier de classement, la détection des
  fils sans réponse. À lire avant le volet 6 (et avant le volet 10, le lundi).
- `taches-auto.md` — la déduplication en deux temps, les quatre déclencheurs, les titres. À lire avant
  le volet 7.
- `dormance.md` — la dérivation des dossiers dormants et le piège d'`updated_at`. À lire avant le
  volet 9, le lundi.
