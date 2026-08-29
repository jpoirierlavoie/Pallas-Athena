# Tâches planifiées — le breffage quotidien et la passe du lundi

Document d'exploitation. Il dit comment une tâche planifiée s'exécute réellement dans Athéna, donne
la configuration exacte des deux tâches et les consignes à coller, et nomme les pièges que le
formulaire ne montre pas.

La compétence elle-même vit dans [`docs/competences/breffage-quotidien/`](competences/breffage-quotidien/) :
un corps et quatre fichiers de référence, à recopier dans l'application. **Rien ne se téléverse tout
seul** — le dépôt est la source de vérité, l'application en est une copie saisie à la main.

---

## 1. Ce qui se passe réellement

Un répartiteur cron balaie les définitions **toutes les quinze minutes** sur le service `default`.
Une tâche est due lorsque le jour civil de **Montréal** correspond à sa récurrence et que l'heure
locale a atteint son `hour_local`.

Le premier balayage qui la trouve due écrit, **en une seule transaction Firestore**, une conversation
flottante, le tour de l'utilisateur, le tour de l'assistant et le marqueur d'occurrence du jour. Ce
marqueur est porté par la **date locale**, ce qui rend l'exécution insensible au changement d'heure,
et il est posé **avant** que le modèle ne réponde.

**Conséquence à retenir : une occurrence marquée ne se rejoue pas.** Si le tour meurt — plafond
d'appels atteint, panne de Vertex après épuisement des reprises — il n'y a ni note, ni courriel, et
rien ne recommence avant la prochaine date planifiée. C'est ce qui gouverne tout le
dimensionnement de la compétence.

Une passe de réparation rattrape le cas étroit où l'occurrence a été marquée mais où la chaîne n'a
jamais été mise en file ; elle journalise `chat_scheduled_repair` en ERREUR, parce qu'une réparation
doit se voir.

La conversation apparaît dans « Flottantes » à `/chat`, non lue. Avec `deliver_email`, un courriel
part une fois l'exécution terminée — **au plus une fois**, garanti par un marqueur transactionnel.

**`jours_ouvrables` veut dire jours JURIDIQUES** — les jours de semaine, moins les jours fériés
québécois. Le breffage ne tourne donc pas le 1er janvier ni le lundi de Pâques. `hebdomadaire`, en
revanche, ne consulte pas ce calendrier : **la passe du lundi tourne aussi un lundi férié.**

## 2. Les deux tâches

Saisies à `/chat/taches-planifiees`. Le formulaire n'expose rien d'autre que ces champs.

| Champ | Breffage quotidien | Passe hebdomadaire |
|---|---|---|
| Nom | `Breffage quotidien` | `Hygiène, dormance et courriels anciens` |
| Récurrence | `jours_ouvrables` | `hebdomadaire`, jour = **lundi** |
| Heure locale | **6** | **7** |
| Modèle | `gemini-2.5-pro` | `gemini-2.5-pro` |
| Compétences | ☑ Breffage quotidien | ☑ Breffage quotidien |
| Dossier | *(aucun — flottante)* | *(aucun — flottante)* |
| Livrer par courriel | ☑ | ☑ |

**L'heure est un entier, de 0 à 23.** Six heures trente ne s'exprime pas. Une tâche à 6 s'exécute au
premier balayage à partir de 06 h 00, donc en pratique entre 06 h 00 et 06 h 15.

**Gemini, et non Claude.** Le quota Vertex des modèles Anthropic est à **zéro** sur ce projet et six
demandes d'augmentation ont été refusées ; nommer un modèle `claude-*` livrerait une tâche qui
répond 429 à chaque occurrence. Trois conséquences assumées : pas de `web_search` (elle est de toute
façon éteinte sur un tour non surveillé), la réflexion est facturée mais n'est pas rendue — la
rubrique « Réflexion » du transcript reste vide, ce qui est exact et non un défaut —, et l'inférence
reste **à Montréal**, ce qui est un gain de résidence.

**Les deux tâches partagent la même compétence.** Un corps de compétence est prépendu au bloc système
de **chaque** appel de modèle du tour : deux compétences distinctes doubleraient ce poids pour rien.
C'est la consigne qui choisit les volets.

## 3. Les consignes, à coller

Ni l'une ni l'autre ne contient de chevrons : le nettoyage d'entrée supprime toute séquence entre
`<` et `>`, silencieusement. Le plafond est de 20 000 caractères, et le dépassement **tronque sans
rien dire** — ces deux consignes en font moins de mille.

### Breffage quotidien

```
Produis le breffage quotidien selon la compétence « Breffage quotidien ».

Exécute les volets 1 à 6 : agenda et échéances, hygiène des dossiers, mes tâches,
volet financier (les trois totaux seulement), courriels, et les tâches à créer.

N'exécute PAS les volets 7 et 8 — hygiène des dossiers « en attente » et dossiers
dormants. Ils appartiennent à la passe du lundi ; note leur absence dans la rubrique
« Ce qui n'a pas été vérifié ».

Fenêtre de l'agenda : 14 jours, 21 le vendredi.
Fenêtre des courriels : sept jours. Le courrier nouveau est celui reçu depuis le
breffage ouvrable précédent — la veille du mardi au vendredi, et le vendredi matin
lorsque nous sommes lundi.

Crée au plus cinq tâches selon taches-auto.md, après la lecture de déduplication.

Dépose la note sous « Général », titrée « Breffage du [date en clair] », avec la clé
d'idempotence breffage-AAAA-MM-JJ.
```

### Passe hebdomadaire

```
Produis la passe hebdomadaire selon la compétence « Breffage quotidien ».

Exécute les volets 7 et 8 SEULEMENT : l'hygiène des dossiers au statut « en attente »,
et les dossiers dormants selon dormance.md. N'exécute pas les volets 1 à 6 — le
breffage de 6 h les a tenus ce matin, et les répéter coûterait le tour.

Ajoute une passe de courriels de trente jours, pour la traîne des fils sans réponse que
la fenêtre de sept jours ne voit pas.

Ne crée AUCUNE tâche : cette passe observe, elle ne mint rien.

Dépose ta propre note sous « Général », titrée « Passe hebdomadaire du [date en clair] »,
avec la clé d'idempotence hebdo-AAAA-MM-JJ. Cette clé doit différer de celle du breffage
de 6 h, sinon la note du matin est rejouée et la tienne n'est jamais écrite.
```

## 4. Mettre en place la compétence

À `/chat/competences`, créer une compétence, puis :

| Champ du formulaire | Valeur |
|---|---|
| Nom | `Breffage quotidien` |
| Description | `Le breffage du matin et la passe du lundi : agenda, échéances, courriels, hygiène.` |
| Corps | le contenu de `00-corps.md` |
| Fichiers | `gabarit-note.md`, `courriels.md`, `taches-auto.md`, `dormance.md` |

Quatre fichiers, sous le plafond de six. Le corps fait environ 11 600 caractères, sous le plafond de
30 000 — au-delà duquel il serait **tronqué sans avertissement**. Un fichier de référence, lui, est
refusé net au-delà de 40 000 : jamais tronqué.

**Le corps ne doit contenir aucun chevron** ; le contenu des fichiers de référence, lui, est exempté
du nettoyage et se recopie tel quel. Les quatre fichiers du dépôt respectent déjà les deux règles.

Le corps désigne ses fichiers par leur nom **à plat** — `courriels.md`, jamais un chemin. C'est ainsi
qu'ils se lisent à l'exécution.

## 5. Ce que le formulaire ne montre pas

**Le plafond du tour est de 24 appels de modèle** (`CHAT_CHAIN_MAX_CALLS` dans `chat.yaml` ; la
valeur par défaut du code, 12, ne s'applique pas au service `chat`). Chaque aller-retour d'outil en
consomme un. Le breffage quotidien en dépense 13 à 16, la passe du lundi 9 à 13 : la marge est réelle
mais elle n'est pas énorme, et c'est **la raison d'être de la scission en deux tâches**. Le nombre
d'appels réellement consommés se lit sur la conversation.

**Deux outils sont dangereux et ne sont pas verrouillés.** `mail_draft` et `mail_file_to_dossier`
répondent sur un tour non surveillé — ils ne figurent pas dans `GATED_TOOLS`, et le versement de
courriel n'a **aucune clé d'idempotence** et écrit des documents **permanents**. La compétence les
interdit par écrit, aux deux endroits qui comptent (le corps et `courriels.md`). Si un jour un
breffage dépose un brouillon ou verse un fichier, c'est cette consigne qui a cédé.

**Les tâches créées ne peuvent pas être supprimées depuis le clavardage.** Une tâche créée à tort se
retire à la main dans l'application. Le préfixe `[Auto]` est là pour qu'elle se repère d'un coup
d'œil.

**Renommer une tâche `[Auto]` en fait recréer une le lendemain.** La déduplication se fait par
comparaison de titres, et la clé d'idempotence ne vit que 24 heures. C'est le coût accepté du volet ;
il se paie en désordre visible, jamais en travail perdu.

**La charte est éditable à l'écran** (`/chat/charte`) et son addendum planifié est ce qui dit au
modèle de ne poser aucune question. Un addendum vide livrerait une exécution planifiée qui attend une
réponse que personne ne donnera. Vérifier qu'il est non vide avant de mettre les tâches en service.

## 6. Vérification

**Il n'existe aucune route « exécuter maintenant ».** L'essai se fait donc à la main.

1. Ouvrir une conversation à `/chat`, cocher la compétence, coller la consigne quotidienne. Cela
   exerce tout, sauf l'addendum planifié et le refus automatique des outils sous garde.
2. **Éprouver le volet des courriels contre la réalité** : le balayage rapporte-t-il du courrier
   venant des sous-dossiers portant un numéro de dossier, et le champ `folder` l'attribue-t-il
   correctement ? C'est l'hypothèse sur laquelle repose tout le volet, et elle ne se vérifie qu'en
   vrai.
3. Relancer la même consigne une seconde fois le même jour et confirmer qu'**aucune tâche n'est
   créée en double**. C'est la seule façon simple d'éprouver la garde.
4. Créer les deux tâches. Le lendemain ouvrable, vérifier entre 06 h 00 et 06 h 15 que le courriel est
   arrivé et que la conversation attend, non lue, dans « Flottantes ».
5. Après le premier lundi : la rubrique des dossiers dormants nomme-t-elle de vrais dossiers, et le
   tour est-il resté loin du plafond ?

## 7. Quand quelque chose ne va pas

| Symptôme | Cause probable |
|---|---|
| Aucune note, aucun courriel | Le tour est mort. Ouvrir la conversation : le dernier tour porte l'erreur. Plafond d'appels atteint, ou panne de Vertex après reprises. |
| Deux notes le même jour | Deux clés d'idempotence distinctes, ou plus de 24 h entre les deux dépôts. |
| La note du lundi manque | Clé identique à celle du breffage de 6 h : le dépôt a rejoué la note du matin. |
| Le modèle pose une question | L'addendum planifié de la charte est vide. |
| 429 à chaque occurrence | Un modèle `claude-*` a été choisi. Le quota Anthropic est à zéro sur ce projet. |
| Rien le 1er janvier | Normal : `jours_ouvrables` exclut les fériés québécois. |
| Un breffage tourne un lundi férié | Normal aussi : `hebdomadaire` ne consulte pas ce calendrier. |
| Des tâches en double tous les matins | Une tâche `[Auto]` a été renommée, ou la lecture de déduplication est tronquée. |

Les journaux utiles, par leur nom exact : `chat_scheduler_execute` (le balayage),
`chat_scheduled_dispatch` (l'occurrence partie), `chat_scheduled_repair` (**toujours en ERREUR** — une
réparation doit se voir), `chat_report_emailed` (la livraison), `chat_turn_failed`, `chat_tool_call`
et `chat_tool_refused`. Ils ne portent que des identifiants et des comptes : jamais un sujet de
courriel, une adresse, un nom ni un contenu.
