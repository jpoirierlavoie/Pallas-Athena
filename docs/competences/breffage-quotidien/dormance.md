# Les dossiers dormants

**Volet hebdomadaire — le lundi seulement.** C'est le volet le plus cher du lot : quatre à dix
appels. Il n'a pas sa place dans un breffage quotidien, et la consigne de la tâche dit lequel des
deux vous exécutez.

Un dossier actif sans mouvement depuis des semaines est soit oublié, soit porteur d'un statut qui
ment — un dossier réglé qu'on n'a jamais fermé. Les deux se corrigent, et ni l'un ni l'autre
n'apparaît dans un agenda : un agenda montre ce qui est prévu, et sur un dossier dormant il n'y a
rien de prévu.

## Table des matières

1. Pourquoi `updated_at` ne répond pas à la question
2. La méthode : le mouvement se lit sur les enfants
3. Les trois lectures
4. Le seuil et les exceptions
5. Ce qui se rapporte
6. Le coût

---

## 1. Pourquoi `updated_at` ne répond pas à la question

Chaque dossier porte un `updated_at`. Il est tentant de le lire comme « dernière activité ». **Il ne
l'est pas**, et il se trompe dans les deux sens.

**Il bouge sans qu'aucun travail n'ait eu lieu.** Une écriture au fidéicommis met à jour les soldes
inscrits sur le document du dossier, donc son `updated_at` et son `etag` — un encaissement de
provision suffit. Une simple sauvegarde du formulaire aussi. Le dossier paraît vivant ; il dort.

**Il ne bouge pas quand du travail a lieu.** Une note, une entrée de temps, un document, une tâche
et une audience vivent dans des collections **distinctes** du document du dossier. Une heure inscrite
ce matin ne touche pas le `updated_at` du dossier. Le dossier paraît dormant depuis six mois ; on y
travaille tous les jours.

C'est le piège central de ce volet. `updated_at` répond à « quand ce document a-t-il été réécrit »,
jamais à « quand a-t-on travaillé sur ce dossier ».

## 2. La méthode : le mouvement se lit sur les enfants

Le mouvement d'un dossier est la date la plus récente parmi ses enregistrements enfants. Trois
familles suffisent, et elles se lisent **à l'échelle du cabinet**, pas dossier par dossier — une
lecture par dossier coûterait un appel par dossier et ferait mourir le tour.

| Famille | Ce qu'elle prouve |
|---|---|
| Entrées de temps | Du travail a réellement été fait. Le signal le plus fort. |
| Notes | Un appel, une rencontre, une décision consignée. |
| Documents | Une pièce reçue ou produite. |

Tâches et audiences sont volontairement exclues : elles portent des dates **à venir**, et une
audience fixée dans trois mois ne dit rien du travail des six dernières semaines.

D'abord le parc :

```
list_dossiers(status: "actif", limit: 50)
```

Une page suffit pour ce cabinet. Si `next_cursor` n'est pas nul, paginer une fois — un dossier absent
du parc ne peut pas être déclaré dormant, et un parc partiel produirait un silence, pas une erreur.

## 3. Les trois lectures

Chaque lecture est bornée par une date de départ — la borne de dormance de la section 4, reculée
d'une marge — et rend des lignes qui portent **`dossier_id`**. Ce champ est déclaré au contrat de
sortie de chacun des trois outils : il est toujours là, il n'y a rien à sonder à l'exécution, et
c'est lui qui fait la jointure.

**⚠ Grouper par `dossier_id`, jamais par `dossier_file_number`.** Les lignes portent aussi le numéro
et l'intitulé du dossier — mais les entrées de temps les portent en **instantané**, figé à la saisie
et non rafraîchi si le dossier est renommé depuis, là où les notes et les documents sont relus du
dossier vivant. Grouper sur le numéro mélangerait deux conventions et scinderait un dossier renommé
en deux, dont l'une paraîtrait dormante. L'identifiant, lui, ne change jamais.

```
list_time_entries(date_from: "AAAA-MM-JJ", limit: 50)
list_notes(scope: "cabinet", dossier_status: "actif", date_from: "AAAA-MM-JJ", limit: 50)
list_documents(scope: "cabinet", dossier_status: "actif", date_from: "AAAA-MM-JJ", limit: 50)
```

Trois particularités, chacune capable de faire échouer l'appel ou de fausser le résultat.

**La pagination du cabinet se fait au curseur, jamais au décalage.** En portée `cabinet`, `list_notes`
et `list_documents` **refusent** `offset` — c'est un refus net, pas un paramètre ignoré. Reprendre
avec `cursor`, en repassant le `next_cursor` de la réponse précédente jusqu'à ce qu'il soit nul.
`list_time_entries` n'a de toute façon que le curseur.

**`dossier_status` n'existe qu'en portée cabinet.** Le passer ailleurs est refusé. Il vaut la peine
ici : il retire d'emblée le mouvement des dossiers fermés, qui ne nous intéresse pas.

**`date_from` ne désigne pas la même chose partout.** Pour une entrée de temps, c'est la date du
travail. Pour une note, sa date de création. Pour un document, sa date **effective** — la date propre
du document quand le juriste en a saisi une, la date de versement sinon. C'est le bon choix pour ce
volet, mais il en découle qu'un jugement daté du mois dernier versé ce matin compte comme un
mouvement du mois dernier.

Deux appels au plus par famille. Si `truncated` reste vrai après le second, s'arrêter : le volet est
partiel, il se déclare partiel, et il ne vaut pas six appels de plus.

## 4. Le seuil et les exceptions

**Quarante-cinq jours sans mouvement**, sur un dossier `actif`. En deçà, un dossier qui attend une
date de cour n'est pas dormant, il attend.

Trois dossiers ne se rapportent pas comme dormants même sans mouvement :

- **Une audience est fixée à venir.** Le dossier est en attente d'une date, pas oublié. `get_agenda`
  du breffage du matin l'a déjà nommée ; si le volet tourne seul, `list_hearings` sur le dossier
  répond, au prix d'un appel.
- **Une alerte de prescription court dessus.** Il figure déjà, plus haut et plus fort, dans la
  rubrique des prescriptions.
- **Le dossier a été ouvert il y a moins de quarante-cinq jours.** Il n'a jamais eu le temps de
  bouger ; le déclarer dormant serait un contresens.

## 5. Ce qui se rapporte

Une ligne par dossier, la plus ancienne d'abord, cinq lignes au plus.

```
### Dossiers dormants

- **2026-008** · Aucun mouvement depuis le 12 juin (77 jours). Tremblay c. Lavoie.
- **2026-021** · Aucun mouvement depuis le 3 juillet (56 jours). Statut « actif ».
- et 3 autres dossiers sans mouvement depuis plus de 45 jours.
```

Ne rien conclure. Le breffage dit qu'un dossier ne bouge pas ; il ne dit pas qu'il est abandonné, ni
qu'il devrait être fermé. Ce sont deux jugements qui appellent la lecture du dossier.

Ce volet ne crée **aucune tâche**. Un dossier dormant est une observation à confirmer, et
`taches-auto.md` ne le compte pas parmi ses déclencheurs — « sans prochaine étape » est une
condition différente et mieux fondée.

Si une famille de lecture a été tronquée, la ligne le dit : « lecture partielle des entrées de
temps — des dossiers actifs peuvent manquer à cette liste », et le fait remonte aussi dans « Ce qui
n'a pas été vérifié ».

## 6. Le coût

| Appel | Nombre |
|---|---|
| `list_dossiers` | 1 à 2 |
| `list_time_entries` | 1 à 2 |
| `list_notes` cabinet | 1 à 2 |
| `list_documents` cabinet | 1 à 2 |
| **Total** | **4 à 8** |

Le plafond du tour est de 24 appels de modèle, tout compris. Ce volet en consomme jusqu'au tiers, et
c'est la raison pour laquelle il est hebdomadaire. S'il se resserre, l'ordre d'abandon est :
documents, puis notes. **Les entrées de temps ne s'abandonnent jamais** — sans elles, il ne reste
aucun signal de travail réel, et le volet dirait n'importe quoi.
