# Le volet des courriels

Un seul balayage, par date, sur toute la boîte. Ce fichier dit pourquoi la date est le bon
instrument, comment un courriel se rattache à un dossier, et comment se repère un fil qui attend
une réponse.

## Table des matières

1. Pourquoi la date, et jamais le dossier de classement
2. Le balayage
3. L'attribution à un dossier
4. Les fils qui attendent une réponse
5. Ce qui se rapporte
6. Ce qui est interdit

---

## 1. Pourquoi la date, et jamais le dossier de classement

Le juriste **classe ses courriels dès leur arrivée** dans des sous-dossiers qui portent le numéro de
dossier. Conséquence directe : la boîte de réception et les éléments envoyés sont presque toujours
**vides**. Une passe qui viserait ces deux emplacements ne rapporterait rien, tous les matins, sans
la moindre erreur — le pire des silences.

`mail_search` n'a d'ailleurs **aucun paramètre de dossier de classement**. Il interroge toute la
boîte, sous-dossiers compris, en un seul appel. L'instrument de sélection est la **date**, et il l'a
toujours été.

## 2. Le balayage

Un appel, sans `query`, sans `dossier_id`, sans `extra_participants` :

```
mail_search(received_from: "AAAA-MM-JJ", limit: 50)
```

Ne rien ajouter d'autre. La requête vide est ce qui envoie l'appel sur le chemin exact
(`$filter` sur la date, tri décroissant) plutôt que sur la recherche plein texte, qui est à la
journée près et plafonnée.

**La fenêtre : sept jours.** Elle sert deux fins d'un seul appel.

- Les lignes **depuis le breffage précédent** sont le courrier nouveau. Du mardi au vendredi, cela
  veut dire la veille ; **le lundi, cela remonte au vendredi matin**, sans quoi tout le week-end
  tombe dans un trou.
- Les lignes plus anciennes donnent l'historique nécessaire à la section 4. Elles ne se rapportent
  pas comme des nouvelles.

Si `truncated` est vrai, la fenêtre déborde les 50 lignes : le dire dans « Ce qui n'a pas été
vérifié », et ne pas paginer — un second appel coûte un appel de modèle pour du courrier ancien.

**La corbeille est exclue par défaut.** Si `deleted_items_excluded` vaut `null`, l'exclusion n'a pas
pu être vérifiée : des courriels supprimés peuvent figurer dans les résultats, et cela se dit.

Le lundi, la passe hebdomadaire refait le même appel avec une fenêtre de **trente jours**, pour la
traîne des fils sans réponse que sept jours ne voient pas.

**Le balayage voit aussi ce que le juriste a envoyé, et ses brouillons.** L'appel porte sur toute la
boîte, éléments envoyés compris — c'est précisément ce qui rend possible la détection de la section 4,
qui a besoin de savoir si la dernière parole du fil est la sienne. Deux conséquences :

- Une ligne dont l'expéditeur est le juriste n'est **jamais** du courrier nouveau à rapporter. Elle ne
  sert qu'à l'historique du fil.
- Une ligne portant `is_draft: true` est un **brouillon**, pas un message envoyé. Elle ne se rapporte
  pas, et surtout **elle ne compte pas comme une réponse** : un brouillon commencé et laissé là est
  exactement le cas où le fil attend toujours.

## 3. L'attribution à un dossier

Chaque ligne porte un champ `folder` : le chemin de classement du message. Comme les sous-dossiers
portent le numéro de dossier, c'est une attribution **directe et exacte** — meilleure que tout
rapprochement par nom ou par adresse.

Trois cas, et chacun se rapporte différemment.

**Le chemin porte un numéro de dossier.** C'est l'attribution. Elle ne se discute pas.

**Le chemin n'en porte pas** — le message est encore dans la boîte de réception, ou dans un dossier
de classement général. Le message **n'est pas classé**, et c'est en soi l'information : c'est du
courrier que le juriste n'a pas encore trié. Le rapporter comme non attribué, sans deviner.

**`folder_labels_complete` est faux.** Un ou plusieurs chemins n'ont pas pu être résolus. Les lignes
touchées se rapportent sans attribution, et le fait se déclare. Ne jamais inventer un rattachement
à partir du sujet.

En dernier recours seulement, et jamais pour contredire un chemin : un rapprochement par
participants, en repassant `mail_search` avec `dossier_id`. Cela coûte un appel de plus et ne se
fait que si une décision en dépend.

## 4. Les fils qui attendent une réponse

Se calcule **sur les lignes déjà obtenues**, sans appel supplémentaire.

Grouper par `conversation_id`. Un fil attend une réponse lorsque :

- le message le plus récent du fil **ne vient pas du juriste** — comparer `from` à son adresse ; et
- ce message a **plus de trois jours juridiques**.

Trois jours, pas trois jours civils : un message du jeudi après-midi n'attend pas depuis trop
longtemps le lundi matin.

**La limite, et elle se déclare quand elle mord.** Le balayage ne voit que sept jours. Un message
sans réponse depuis trois semaines n'apparaît pas dans le balayage quotidien — il apparaît dans la
passe de trente jours du lundi. Les jours ordinaires, la rubrique porte la mention que la détection
couvre sept jours.

Un fil dont le dernier message vient du juriste n'attend rien de lui : il n'a pas sa place ici, même
si le correspondant n'a pas répondu. Le breffage suit ce que le juriste doit faire, pas ce qu'il
attend des autres.

## 5. Ce qui se rapporte

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

**Ne jamais compter les non-lus.** Le nombre de messages non lus n'est pas une information : il
mesure une habitude de lecture, pas une charge de travail. La question est « lequel appelle une
décision aujourd'hui ».

## 6. Ce qui est interdit

**`mail_draft`** — un breffage ne dépose pas de brouillon dans Outlook. Si une réponse s'impose,
la nommer dans la note et laisser le juriste la rédiger.

**`mail_file_to_dossier`** — il verse des documents **permanents** dans un dossier, sans clé
d'idempotence, et **aucun outil de cette conversation ne peut les retirer**. Un classement n'est
jamais une urgence du matin.

Ces deux outils **répondront** si vous les appelez : rien dans le système ne les verrouille pour une
exécution planifiée. L'interdit est ici, et il tient tout seul.

**Ne jamais citer un lien de connexion.** Le retrait automatique des liens à usage unique existe,
mais il ne dispense pas de la règle : on ne recopie pas une URL d'authentification dans une note.
