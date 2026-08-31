# Tâches planifiées — le breffage quotidien et la passe du lundi

Document d'exploitation. Il dit comment une tâche planifiée s'exécute réellement dans Athéna, donne
**deux recettes indépendantes** — une par tâche, champs et consigne ensemble — et nomme les pièges que
le formulaire ne montre pas.

**Une tâche planifiée porte UNE consigne.** Les deux recettes ci-dessous se saisissent séparément, dans
deux définitions distinctes. Rien ne se combine.

La compétence, elle, est **unique et partagée** par les deux tâches : elle vit dans
[`docs/competences/breffage-quotidien/`](competences/breffage-quotidien/) — un corps et quatre fichiers
de référence, à recopier dans l'application (§4). **Rien ne se téléverse tout seul** : le dépôt est la
source de vérité, l'application en est une copie saisie à la main.

---

## 1. Ce qui se passe réellement

Un répartiteur cron balaie les définitions **toutes les quinze minutes** sur le service `default`. Une
tâche est due lorsque le jour civil de **Montréal** correspond à sa récurrence et que l'heure locale a
atteint son `hour_local`.

Le premier balayage qui la trouve due écrit, **en une seule transaction Firestore**, une conversation
flottante, le tour de l'utilisateur, le tour de l'assistant et le marqueur d'occurrence du jour. Ce
marqueur est porté par la **date locale**, ce qui rend l'exécution insensible au changement d'heure, et
il est posé **avant** que le modèle ne réponde.

**Conséquence à retenir : une occurrence marquée ne se rejoue pas.** Si le tour meurt — plafond
d'appels atteint, panne de Vertex après épuisement des reprises — il n'y a ni note, ni courriel, et rien
ne recommence avant la prochaine date planifiée. C'est ce qui gouverne tout le dimensionnement de la
compétence.

Une passe de réparation rattrape le cas étroit où l'occurrence a été marquée mais où la chaîne n'a
jamais été mise en file ; elle journalise `chat_scheduled_repair` en ERREUR, parce qu'une réparation
doit se voir.

La conversation apparaît dans « Flottantes » à `/chat`, non lue, **dès le dispatch**. Le courriel, lui,
part **à la fin de la chaîne** — après le dernier appel de modèle, donc une trentaine de minutes plus
tard — et au plus une fois, garanti par un marqueur transactionnel.

**Chaque occurrence tourne dans une conversation NEUVE.** La passe de 7 h n'a aucun accès aux résultats
du breffage de 6 h : elle ne peut ni les lire, ni les citer, ni s'appuyer dessus.

**`jours_ouvrables` veut dire jours JURIDIQUES** — les jours de semaine, moins les jours fériés
québécois. Le breffage ne tourne donc pas le 1er janvier ni le lundi de Pâques. `hebdomadaire`, en
revanche, ne consulte pas ce calendrier : **la passe du lundi tourne aussi un lundi férié** (voir §7).

**Le modèle ne connaît pas la date.** Ni la charte, ni la consigne, ni le titre de la conversation ne la
portent. La seule source est `window.from` de `get_agenda` — ce qui explique pourquoi les deux consignes
commencent par cet appel.

---

## 2. Tâche 1 — Breffage quotidien

Saisie à `/chat/taches-planifiees`.

| Champ | Valeur |
|---|---|
| Nom | `Breffage quotidien` |
| Consigne | le bloc ci-dessous, collé tel quel |
| Récurrence | `jours_ouvrables` |
| Heure locale | **6** |
| Modèle | `gemini-2.5-pro` |
| Compétences | ☑ Breffage quotidien |
| Dossier | *(aucun — conversation flottante)* |
| Livrer par courriel | ☑ |
| Active | ☑ |

**L'heure est un entier, de 0 à 23.** Six heures trente ne s'exprime pas. Une tâche à 6 est dispatchée
au premier balayage à partir de 06 h 00, donc en pratique entre 06 h 00 et 06 h 15.

### La consigne

```
Produisez le breffage quotidien selon la compétence « Breffage quotidien ».

Commencez par get_agenda avec days_ahead 14. Sa réponse porte window.from, qui EST la
date du jour à Montréal : vous n'avez aucune autre source pour la date, et rien ne
s'écrit avant cet appel.

Exécutez les volets 1 à 7 : agenda et échéances, hygiène des dossiers, les tâches
ouvertes du juriste, le parc des dossiers actifs, le volet financier (les trois totaux
seulement), les courriels sur sept jours, puis les tâches à créer.

N'exécutez pas les volets 8 à 10 — dossiers en attente, dossiers dormants, courriels
anciens. Ils appartiennent à la passe hebdomadaire du lundi ; notez leur absence dans la
rubrique « Ce qui n'a pas été vérifié ».

Le courrier nouveau est celui reçu depuis le breffage ouvrable précédent : la veille du
mardi au vendredi, et le vendredi matin lorsque nous sommes lundi.

Créez au plus trois tâches selon taches-auto.md, chacune précédée de sa vérification par
dossier. Si le budget d'appels se resserre, abandonnez les créations et décrivez les
candidats dans la note : la note passe avant toute écriture.

Déposez la note sous « Général », titrée « Breffage du [date en clair] », avec la clé
d'idempotence breffage-AAAA-MM-JJ.
```

---

## 3. Tâche 2 — Passe hebdomadaire du lundi

Saisie séparément, dans sa propre définition.

| Champ | Valeur |
|---|---|
| Nom | `Hygiène, dormance et courriels anciens` |
| Consigne | le bloc ci-dessous, collé tel quel |
| Récurrence | `hebdomadaire`, jour = **lundi** |
| Heure locale | **7** |
| Modèle | `gemini-2.5-pro` |
| Compétences | ☑ Breffage quotidien *(la même)* |
| Dossier | *(aucun — conversation flottante)* |
| Livrer par courriel | ☑ |
| Active | ☑ |

### La consigne

```
Produisez la passe hebdomadaire selon la compétence « Breffage quotidien ».

Commencez par get_agenda avec days_ahead 1. Cet appel ne sert qu'à obtenir window.from,
la date du jour à Montréal : ne rapportez rien de son contenu.

Exécutez ensuite les volets 8 à 10 : l'hygiène des dossiers au statut « en attente », les
dossiers dormants selon dormance.md, et une passe de courriels sur la tranche ANCIENNE
— received_from il y a trente jours, received_to il y a huit jours — pour la traîne des
fils sans réponse que la fenêtre quotidienne ne voit pas.

N'exécutez pas les volets 1 à 7 : ils appartiennent au breffage quotidien.

Ne créez AUCUNE tâche : cette passe observe, elle n'écrit que sa note.

Déposez votre propre note sous « Général », titrée « Passe hebdomadaire du [date en
clair] », avec la clé d'idempotence hebdo-AAAA-MM-JJ. Cette clé doit différer de celle du
breffage quotidien : une clé déjà servie avec des arguments différents fait REFUSER
l'appel, et votre note ne serait pas écrite.
```

---

## 4. Mettre en place la compétence

À `/chat/competences`, créer **une seule** compétence, que les deux tâches sélectionnent :

| Champ du formulaire | Valeur |
|---|---|
| Nom | `Breffage quotidien` |
| Description | `Le breffage du matin et la passe du lundi : agenda, échéances, courriels, hygiène.` |
| Corps | le contenu de `00-corps.md` |
| Fichiers | `gabarit-note.md`, `courriels.md`, `taches-auto.md`, `dormance.md` |

Quatre fichiers, sous le plafond de six. Le corps est sous le plafond de 30 000 caractères — au-delà
duquel il serait **tronqué sans avertissement**. Un fichier de référence, lui, est refusé net au-delà de
40 000 : jamais tronqué.

**Le corps ne doit contenir aucun chevron** (`<` … `>`) ; le nettoyage d'entrée supprime toute séquence
entre les deux, silencieusement. Le contenu des fichiers de référence, lui, est exempté de ce nettoyage
et se recopie tel quel. Les cinq fichiers du dépôt respectent déjà les deux règles.

Le corps désigne ses fichiers par leur nom **à plat** — `courriels.md`, jamais un chemin. C'est ainsi
qu'ils se lisent à l'exécution.

**Une seule compétence pour les deux tâches**, à dessein : un corps de compétence est prépendu au bloc
système de **chaque** appel de modèle du tour, donc deux compétences distinctes doubleraient ce poids
pour rien. C'est la consigne qui choisit les volets.

## 5. Ce que le formulaire ne montre pas

**Le plafond du tour est de 24 appels de modèle** (`CHAT_CHAIN_MAX_CALLS` dans `chat.yaml` ; la valeur
par défaut du code, 12, ne s'applique pas au service `chat`). Chaque aller-retour d'outil en consomme
un. Le breffage quotidien en dépense 9 sans création de tâche et 15 au pire ; la passe du lundi, 9 à 12.
La marge est réelle mais pas énorme, et c'est **la raison d'être de la scission en deux tâches**. Le
nombre d'appels réellement consommés se lit sur la conversation.

**Gemini, et non Claude.** Le quota Vertex des modèles Anthropic est à **zéro** sur ce projet et six
demandes d'augmentation ont été refusées ; nommer un modèle `claude-*` livrerait une tâche qui répond
429 à chaque occurrence. Trois conséquences assumées : pas de `web_search` (elle est de toute façon
éteinte sur un tour non surveillé), la réflexion est facturée mais n'est pas rendue — la rubrique
« Réflexion » du transcript reste vide, ce qui est exact et non un défaut —, et l'inférence reste **à
Montréal**, ce qui est un gain de résidence.

**Quatorze outils d'écriture répondent sur un tour planifié ; onze demandent une autorisation et sont
auto-refusés en exécution non surveillée. Les dix autres s'exécutent sans rien demander** — dont
`complete_task`, qui complète l'étape de protocole liée et peut clore un protocole entier, et les deux
outils de messagerie (`mail_draft`, `mail_file_to_dossier`), ce dernier versant des documents
**permanents** sans clé d'idempotence. La compétence les interdit par écrit, au corps et dans
`courriels.md`. Si un jour un breffage dépose un brouillon, verse un fichier ou ferme une tâche, c'est
cette consigne qui a cédé — pas un verrou.

**Les tâches créées ne peuvent pas être supprimées depuis le clavardage.** Une tâche créée à tort se
retire à la main dans l'application. Le préfixe `[Auto]` est là pour qu'elle se repère d'un coup d'œil.

**Renommer une tâche `[Auto]` en fait recréer une le lendemain.** La déduplication se fait par
comparaison de titres, et la clé d'idempotence ne vit que 24 heures. C'est le coût accepté du volet ; il
se paie en désordre visible, jamais en travail perdu.

**La charte est éditable à l'écran** (`/chat/charte`) et son addendum planifié est ce qui dit au modèle
de ne poser aucune question. Un addendum vide livrerait une exécution planifiée qui attend une réponse
que personne ne donnera. Vérifier qu'il est non vide avant de mettre les tâches en service.

## 6. Vérification

**Il n'existe aucune route « exécuter maintenant ».** L'essai se fait donc à la main.

1. Ouvrir une conversation à `/chat`, cocher la compétence, coller la consigne quotidienne (§2). Cela
   exerce tout, sauf l'addendum planifié et le refus automatique des outils sous autorisation.
2. **Éprouver le volet des courriels contre la réalité** : le balayage rapporte-t-il du courrier venant
   des sous-dossiers portant un numéro de dossier, et le champ `folder` l'attribue-t-il correctement ?
   C'est l'hypothèse sur laquelle repose tout le volet, et elle ne se vérifie qu'en vrai.
3. Relancer la même consigne une seconde fois le même jour et confirmer qu'**aucune tâche n'est créée
   en double**. C'est la seule façon simple d'éprouver la garde.
4. Créer les deux tâches. Le lendemain ouvrable : vers **06 h 15**, la conversation doit être apparue,
   non lue, dans « Flottantes » — c'est la preuve que l'occurrence est partie. Le **courriel** arrive à
   la fin de la chaîne, donc plus tard : le relever vers **06 h 45**. Une conversation présente sans
   courriel signifie que le tour court encore, ou qu'il est mort : ouvrir son dernier tour.
5. Après le premier lundi : la rubrique des dossiers dormants nomme-t-elle de vrais dossiers, et le tour
   est-il resté loin du plafond ?

## 7. Quand quelque chose ne va pas

| Symptôme | Cause probable |
|---|---|
| Aucune note, aucun courriel | Le tour est mort. Ouvrir la conversation : le dernier tour porte l'erreur. Plafond d'appels atteint, ou panne de Vertex après reprises. |
| La conversation est là, pas le courriel | Normal avant ~06 h 45 : le courriel part à la fin de la chaîne. Passé cela, le tour est mort en cours de route. |
| Deux notes de **titres différents** le lundi | Normal et voulu : « Breffage du … » à 6 h, « Passe hebdomadaire du … » à 7 h. |
| Deux notes de **même titre** le même jour | Un second dépôt sous une clé différente, ou plus de 24 h après le premier (la clé d'idempotence a expiré). |
| La note du lundi manque | Le tour est mort, ou l'appel `create_note` a été **refusé** — le refus est visible dans la conversation. Une clé déjà servie avec un contenu différent est refusée, jamais rejouée. |
| Le modèle pose une question | L'addendum planifié de la charte est vide. |
| Le modèle écrit une date fausse ou n'en écrit aucune | `get_agenda` a échoué : c'est la seule source de la date. |
| 429 à chaque occurrence | Un modèle `claude-*` a été choisi. Le quota Anthropic est à zéro sur ce projet. |
| Rien le 1er janvier | Normal : `jours_ouvrables` exclut les fériés québécois. |
| Une passe hebdomadaire sans breffage le même matin | Normal quatre fois l'an — lundi de Pâques, Journée nationale des patriotes, fête du Travail, Action de grâce. `hebdomadaire` ne consulte pas le calendrier férié ; `jours_ouvrables` si. Ces jours-là, aucun agenda n'est produit. Déplacer la passe au mardi le fermerait, au prix du cadrage « lundi ». |
| Aucune tâche créée, tous les jours | Lire la dernière rubrique de la note : une lecture des tâches vide ou tronquée suspend le volet par conception. |
| Des tâches en double tous les matins | Une tâche `[Auto]` a été renommée, ou la vérification par dossier n'a pas pu tourner. |

Les journaux utiles, par leur nom exact : `chat_scheduler_execute` (le balayage),
`chat_scheduled_dispatch` (l'occurrence partie), `chat_scheduled_repair` (**toujours en ERREUR** — une
réparation doit se voir), `chat_report_emailed` (la livraison), `chat_turn_failed`, `chat_tool_call` et
`chat_tool_refused`. Ils ne portent que des identifiants et des comptes : jamais un sujet de courriel,
une adresse, un nom ni un contenu.
