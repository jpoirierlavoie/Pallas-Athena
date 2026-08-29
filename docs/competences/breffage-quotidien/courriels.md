# Le volet des courriels

Un seul balayage, par date, sur toute la boîte. Ce fichier dit pourquoi la date est le bon
instrument, comment un courriel se rattache à un dossier, et comment se repère un fil qui attend une
réponse.

## Table des matières

1. Pourquoi la date, et jamais le dossier de classement
2. Le balayage
3. Lire `truncated` correctement
4. L'attribution à un dossier
5. Les fils qui attendent une réponse
6. Ce qui se rapporte
7. Ce qui est interdit

---

## 1. Pourquoi la date, et jamais le dossier de classement

Le juriste **classe ses courriels dès leur arrivée** dans des sous-dossiers qui portent le numéro de
dossier. Conséquence directe : la boîte de réception et les éléments envoyés sont presque toujours
**vides**. Une passe qui viserait ces deux emplacements ne rapporterait rien, tous les matins, sans la
moindre erreur — le pire des silences.

`mail_search` n'a d'ailleurs **aucun paramètre de dossier de classement**. Il interroge toute la
boîte, sous-dossiers compris, en un seul appel. L'instrument de sélection est la **date**, et il l'a
toujours été.

## 2. Le balayage

Un appel, sans `query`, sans `dossier_id`, sans `extra_participants` :

```
mail_search(received_from: "AAAA-MM-JJ", limit: 50)
```

La date de départ se calcule à partir de `window.from` de l'appel 1 — c'est la seule date dont vous
disposez.

Ne rien ajouter d'autre. La requête vide est ce qui envoie l'appel sur le chemin exact (filtre sur la
date, tri décroissant) plutôt que sur la recherche plein texte, qui est à la journée près et
plafonnée.

**La fenêtre quotidienne : sept jours.** Elle sert deux fins d'un seul appel.

- Les lignes **depuis le breffage ouvrable précédent** sont le courrier nouveau. Du mardi au vendredi,
  cela veut dire la veille ; **le lundi, cela remonte au vendredi matin**, sans quoi tout le week-end
  tombe dans un trou.
- Les lignes plus anciennes donnent l'historique nécessaire à la section 5. Elles ne se rapportent pas
  comme des nouvelles.

⚠ **L'appel rend les 50 messages les plus RÉCENTS de la fenêtre.** Ce sont donc les plus anciens qui
tombent en premier — exactement ceux dont la section 5 a besoin. Sur une semaine chargée, la détection
des fils sans réponse travaille sur une fenêtre amputée par le haut. C'est pourquoi `truncated`
suspend cette détection (section 3), et pourquoi le lundi lit une tranche séparée plutôt qu'une
fenêtre élargie.

**La passe du lundi lit la tranche ANCIENNE, jamais la même en plus large.**

```
mail_search(received_from: "AAAA-MM-JJ (il y a 30 jours)", received_to: "AAAA-MM-JJ (il y a 8 jours)", limit: 50)
```

Élargir seulement `received_from` ne servirait à rien : le tri est décroissant et le plafond de 50
rendrait les mêmes messages récents que le balayage quotidien. Poser la borne haute juste avant la
fenêtre quotidienne est ce qui fait apparaître la traîne — les fils sans réponse depuis trois semaines
que sept jours ne voient pas.

**La corbeille est exclue par défaut.** Si `deleted_items_excluded` vaut `null`, l'exclusion n'a pas pu
être vérifiée : des courriels supprimés peuvent figurer dans les résultats, et cela se dit.

**Le balayage voit aussi ce que le juriste a envoyé, et ses brouillons.** L'appel porte sur toute la
boîte, éléments envoyés compris — c'est précisément ce qui rend possible la détection de la section 5,
qui a besoin de savoir si la dernière parole du fil est la sienne. Deux conséquences :

- Une ligne dont l'expéditeur est le juriste n'est **jamais** du courrier nouveau à rapporter. Elle ne
  sert qu'à l'historique du fil.
- Une ligne portant `is_draft: true` est un **brouillon**, pas un message envoyé. Elle ne se rapporte
  pas, et surtout **elle ne compte pas comme une réponse** : un brouillon commencé et laissé là est
  exactement le cas où le fil attend toujours.

## 3. Lire `truncated` correctement

`truncated` n'est **pas** un synonyme de « la fenêtre déborde les 50 lignes ». Il est vrai dès que
l'une de ces trois choses est vraie :

- la fenêtre déborde le plafond ; **ou**
- le serveur avait d'autres pages ; **ou**
- **au moins une ligne a été écartée** — un message de la corbeille, ou du courrier machine émis par
  l'application elle-même.

Le troisième cas est fréquent et anodin. Avant de conclure que le volet est partiel, lire `count` et
`deleted_items_excluded` : si `count` est nettement sous 50 et que des lignes ont simplement été
écartées, la fenêtre a bien été vue en entier — le dire ainsi plutôt que d'annoncer un débordement qui
n'a pas eu lieu.

En revanche, dès que `count` atteint 50, la fenêtre est réellement amputée : la détection de la
section 5 n'a pas vraiment tourné, elle se déclare partielle, et le déclencheur (a) de
`taches-auto.md` ne se déclenche pas ce jour-là.

## 4. L'attribution à un dossier

Chaque ligne porte un champ `folder` : le chemin de classement du message. Comme les sous-dossiers
portent le numéro de dossier, c'est une attribution **directe et exacte** — meilleure que tout
rapprochement par nom ou par adresse.

Trois cas, et chacun se rapporte différemment.

**Le chemin porte un numéro de dossier.** C'est l'attribution. Elle ne se discute pas. Pour créer une
tâche, ce numéro doit encore être traduit en identifiant : voir `taches-auto.md` §4.

**Le chemin n'en porte pas** — le message est encore dans la boîte de réception, ou dans un dossier de
classement général. Le message **n'est pas classé**, et c'est en soi l'information : c'est du courrier
que le juriste n'a pas encore trié. Le rapporter comme non attribué, sans deviner.

**`folder_labels_complete` est faux.** Un ou plusieurs chemins n'ont pas pu être résolus. Les lignes
touchées se rapportent sans attribution, et le fait se déclare. Ne jamais inventer un rattachement à
partir du sujet.

En dernier recours seulement, et jamais pour contredire un chemin : un rapprochement par participants,
en repassant `mail_search` avec `dossier_id`. Cela coûte un appel de plus et ne se fait que si une
décision en dépend.

## 5. Les fils qui attendent une réponse

Se calcule **sur les lignes déjà obtenues**, sans appel supplémentaire.

Grouper par `conversation_id`. Un fil attend une réponse lorsque :

- le message le plus récent du fil **ne vient pas du juriste** — comparer le champ `from` à
  **`jason@poirierlavoie.ca`**, son adresse ; et
- ce message a **plus de trois jours juridiques**.

Trois jours, pas trois jours civils : un message du jeudi après-midi n'attend pas depuis trop
longtemps le lundi matin.

Un fil dont le dernier message vient du juriste n'attend rien de lui : il n'a pas sa place ici, même
si le correspondant n'a pas répondu. Le breffage suit ce que le juriste doit faire, pas ce qu'il
attend des autres.

**La limite, et elle se déclare quand elle mord.** Le balayage quotidien ne voit que sept jours, et
seulement si `count` est resté sous 50 (section 3). Un message sans réponse depuis trois semaines
apparaît dans la tranche ancienne du lundi, pas avant. Les jours ordinaires, la rubrique porte la
mention que la détection couvre sept jours.

## 6. Ce qui se rapporte

Deux listes courtes, cinq lignes chacune au plus.

```
### Courriels

**En attente de votre réponse**
- **2026-014** · Me Gagnon, 21 août — mise en demeure, sans réponse depuis 4 jours.
- *(non classé)* · adjoint@assureur-xyz.ca, 22 août — demande de documents.

**Depuis vendredi**
- **2026-035** · Greffe, 25 août — avis de présentation.
- et 6 autres messages classés, sans décision apparente.
```

Le sujet du message se cite **abrégé**, jamais son contenu. Un corps de courriel est couvert par le
secret professionnel ; la note vit dans Athéna, mais elle se synchronise sur un téléphone.

**Ne jamais compter les non-lus.** Le nombre de messages non lus n'est pas une information : il mesure
une habitude de lecture, pas une charge de travail. La question est « lequel appelle une décision
aujourd'hui ».

## 7. Ce qui est interdit

**`mail_draft`** — un breffage ne dépose pas de brouillon dans Outlook. Si une réponse s'impose, la
nommer dans la note et laisser le juriste la rédiger.

**`mail_file_to_dossier`** — il verse des documents **permanents** dans un dossier, sans clé
d'idempotence, et **aucun outil de cette conversation ne peut les retirer**. Un classement n'est jamais
une urgence du matin.

Ces deux outils **répondront** si vous les appelez : rien dans le système ne les verrouille pour une
exécution planifiée. L'interdit est ici, et il tient tout seul.

**Ne jamais citer un lien de connexion.** Le retrait automatique des liens à usage unique existe, mais
il ne dispense pas de la règle : on ne recopie pas une URL d'authentification dans une note.
