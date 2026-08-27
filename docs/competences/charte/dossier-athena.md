# Le dossier dans Athéna

Ce fichier appartient à la CHARTE : il vaut pour toute conversation, quelles que soient les compétences cochées. Il dit comment trouver un dossier, quoi y lire, quels champs trompent, et comment y écrire sans rien casser.

Le lire dès que la conversation touche un dossier — une question de fond, une échéance, une note à rédiger, un document à consulter, une heure à saisir. Ne pas le lire pour une conversation qui ne touche à aucune donnée du cabinet.

## Table des matières

1. Trouver le dossier
2. Lire le dossier
3. La taxonomie des délais
4. Les champs qui trompent
5. Le protocole de l'instance et son piège de régime
6. Le calcul des délais
7. Les notes déjà au dossier
8. Les documents
9. Écrire dans Athéna
10. Ce qu'Athéna ne contient pas

---

## 1. Trouver le dossier

**D'abord : il est peut-être déjà nommé.** Quand la conversation est rattachée à un dossier, un bloc « DOSSIER DE CETTE CONVERSATION » figure au prompt système avec son numéro, son intitulé et son `dossier_id`. Employez cet identifiant tel quel et **ne demandez pas de quel dossier il s'agit** — la question a déjà sa réponse sous vos yeux. Ce bloc identifie le dossier; il ne dit rien de son contenu, qui se lit avec `get_dossier`.

Le reste de cette section vaut pour une conversation flottante, ou lorsque la question porte sur un autre dossier que celui de la conversation.

`list_dossiers` accepte une recherche libre (`query`) sur l'intitulé, le numéro de dossier interne, le **numéro de dossier de cour** et le sommaire. C'est la porte d'entrée quand l'utilisateur ne nomme qu'une partie, un numéro de greffe ou un objet.

Le filtre `status` prend `actif`, `en_attente`, `fermé` ou `archivé`. L'omettre balaie tout — utile, parce qu'une question de prescription porte souvent sur un dossier fermé ou en attente. La pagination se fait par `next_cursor`, à repasser en `cursor` jusqu'à ce qu'il revienne nul.

Ne jamais deviner un identifiant. `get_dossier` accepte soit `dossier_id`, soit `file_number`, et un seul des deux à la fois.

## 2. Lire le dossier

`get_dossier` livre une qualification déjà faite. **Utiliser son vocabulaire** plutôt que d'en inventer un autre : ce que vous écrivez se reverse ainsi directement au dossier.

Les champs qui portent la qualification juridique : `domaine` et `domaine_label` (la matière), `action` (le recours), `role` (la position du client), `tribunal` et `court_file_number`, `delai` et `delai_types` (la nature du délai, selon la taxonomie de la section 3), `delai_point_depart`, `avis` (l'existence d'un avis préalable), `ref_delai` et `ref_fondement` (les renvois déjà consignés), `prescription_date`, `prescription_status`, `prescription_date_effective`, et le drapeau `a_valider`.

**`a_valider` à vrai signale une qualification NON confirmée.** Elle reste à vérifier aux sources. Ne jamais la présenter comme acquise, et le signaler à l'utilisateur quand elle porte la réponse.

## 3. La taxonomie des délais

| Code | Sens |
|---|---|
| PE | Prescription extinctive |
| PA | Prescription acquisitive (défensive) |
| D | Déchéance stricte — ne se suspend ni ne s'interrompt |
| DR | Déchéance relevable — un relèvement légal existe |
| A | Avis préalable |
| R | Délai raisonnable |
| N | Aucun délai |
| S | Suit le droit sous-jacent |
| I | Imprescriptible |
| V | Variable |
| F | Fenêtre rétrospective |

La distinction PE / D est celle de l'article 2878 C.c.Q. et elle est la première à trancher : le tribunal ne peut pas suppléer d'office la prescription, mais il **doit** déclarer d'office la déchéance lorsque la loi la prévoit. Une déchéance manquée n'est pas rattrapable par le silence de l'adversaire; une prescription non plaidée l'est.

## 4. Les champs qui trompent

Aucun champ n'est un fait établi. Ce sont des saisies, faites un jour par quelqu'un.

**`prescription_date` n'est pas la date pour agir.** Athéna enregistre un `prescription_status` — par exemple `interrompue` — et un `prescription_date_effective`, mais il **ne calcule** ni l'interruption ni la suspension : il consigne ce qu'on lui a dit. Le point de départ y est une saisie, pas un fait prouvé, et il est parfois marqué inconnu tout en produisant néanmoins une date affichée. Cette date est un signal; l'analyse au fond est le travail. Un faux positif — une date alarmante sur un dossier dont le délai est en réalité interrompu — coûte autant qu'un faux négatif.

**`role` s'inverse.** Le champ indique la position du client et il arrive qu'il soit à l'envers, ou qu'un tiers y soit inscrit comme partie adverse. Croiser avec l'intitulé, les parties et la correspondance avant de bâtir une analyse sur cette base.

**La qualification vieillit.** Un dossier ouvert sous une qualification et mené sous une autre garde la première. Quand le `domaine` décrit un recours différent de celui qu'on exerce, c'est une source d'erreur de délai, et c'est à signaler (voir la section 9).

**Les tâches et leurs statuts.** `list_tasks` exige `include_completed: true` pour donner le tableau complet; une tâche supprimée ne se distingue pas d'une tâche close par son absence.

## 5. Le protocole de l'instance et son piège de régime

`list_protocol_steps` retourne les étapes du protocole actif avec leurs échéances. `include_history: true` ajoute les protocoles antérieurs.

Trois propriétés à connaître :

**Le `status` est dérivé, pas stocké.** Il se recalcule à partir de l'échéance et de la date du jour, et c'est lui qui gouverne. `status_stored` est le mot inscrit sur le document, conservé pour la provenance : il n'est écrit que lorsque l'avocat ouvre la page du protocole dans un navigateur, et un « en_retard » qui s'y trouve n'est jamais effacé. `status_differs: true` marque exactement cet écart.

**`regime_mismatch` est le piège sérieux.** À vrai, il signifie que le régime du C.p.c. dont relève le gabarit du protocole ne gouverne pas le forum du dossier — par exemple un gabarit de procédure allégée de la Cour du Québec appliqué à un dossier de Cour supérieure. Les échéances suivies sont alors **suspectes** et doivent être soulevées, jamais utilisées. Le vérifier sur chaque protocole.

**Deux délais de rigueur encadrent tout le reste**, et ils s'articulent :

| Disposition | Contenu |
|---|---|
| Art. 149 C.p.c. | Le protocole se dépose au greffe **dans les 45 jours** de la signification de l'avis d'assignation (trois mois en matière familiale) |
| Art. 173 C.p.c. | Mise en état et demande d'inscription **dans les six mois** (un an en matière familiale) de l'acceptation présumée ou de l'établissement du protocole. **Délai de rigueur.** Si le protocole n'a pas été déposé dans le délai de l'article 149, les six mois se calculent **depuis la signification de la demande**, et le tribunal ne peut alors prolonger qu'en cas d'impossibilité en fait d'agir |

Le troisième alinéa de l'article 173 est le plus dangereux : un protocole non déposé ne repousse pas l'horloge, il l'avance. Vérifier la date de signification avant de rassurer qui que ce soit sur un délai de mise en état.

## 6. Le calcul des délais

L'article 83 C.p.c. gouverne : le jour du point de départ n'est pas compté, celui de l'échéance l'est; un délai en mois expire au même quantième, ou au dernier jour du mois à défaut de quantième identique; l'échéance est à 24 h; celle qui tomberait un samedi ou un jour férié est reportée au premier jour ouvrable suivant.

Utiliser `compute_judicial_deadline` plutôt que de calculer à la main, en gardant deux réserves : l'outil applique sa propre notion de jour non juridique, à rapprocher du texte de l'article 83; et il calcule des délais **de procédure**, non des délais de **prescription**, dont le point de départ relève du droit substantiel et se discute.

`parse_court_file_number` analyse un numéro de dossier de cour québécois et en tire le greffe — palais de justice et district — puis le tribunal et le type de greffe. Utile pour vérifier la cohérence entre le forum inscrit au dossier et le numéro réel. Il n'établit pas que le dossier existe ni qu'il est actif.

⚠ **Deux outils portent ce nom.** `compute_judicial_deadline` et `parse_court_file_number` sont ceux de l'application, sans préfixe. Le Worker de jurisprudence porte son propre `jurisprudence_greffe_parse_court_file_number`, au même usage. Employer celui qui est disponible; ne pas s'étonner qu'ils coexistent.

## 7. Les notes déjà au dossier

**Chercher avant d'écrire, toujours.** `list_notes` avec `dossier_id` liste les notes du dossier, épinglées d'abord puis les plus récentes, avec un aperçu de 280 caractères. Son paramètre `query` cherche dans **l'intitulé et le contenu complet**, et pas seulement dans l'aperçu : une correspondance peut donc se trouver au-delà de ce qu'on voit, et il faut ouvrir la note avec `get_note` avant de conclure qu'elle est hors sujet.

C'est le moyen d'éviter le doublon. Un travail déjà fait sur la même question se complète; il ne se refait pas.

**Le paramètre `scope` décide de la portée**, et son défaut est étroit :

| `scope` | Ce qui est balayé |
|---|---|
| absent, sans `dossier_id` | Les seules notes « Général », rattachées à aucun dossier |
| `dossier` (implicite avec `dossier_id`) | Les notes de ce dossier |
| `cabinet` | **Tout le cabinet** — la recherche transversale |

Ne pas conclure « aucune note n'existe » sur un balayage étroit. Une question déjà traitée dans un autre dossier ne se trouve qu'en `cabinet`.

Le filtre `category` prend `rencontre`, `consultation`, `analyse`, `recherche`, `stratégie`, `vacation`, `autre`. Une note de recherche juridique va en `recherche`; un compte rendu d'appel en `appel`; une analyse en `analyse`.

**La note marquée `is_analyse` est la théorie de la cause du dossier.** Elle est en lecture seule : `append_to_note` la refuse. La lire est souvent le meilleur investissement de la session — elle porte la qualification retenue, les faits défavorables et l'analyse déjà faite. Ne jamais la viser en écriture.

## 8. Les documents

`list_documents` donne les noms, catégories, tailles et versions des documents d'un dossier, ainsi que les identifiants à passer à `get_document_text`. Il porte le même paramètre `scope` que `list_notes`.

`get_document_text` rend la **couche de texte** d'un document — PDF et .docx —, par tranches. La mécanique de pagination a deux noms : on demande une tranche avec **`page_range`** (`"4"` = à partir de la page 4; `"2-6"` = cet intervalle, bornes comprises), et la réponse indique la suite dans **`next_page`**, qu'on repasse en `page_range` au tour suivant. Suivre jusqu'au bout plutôt que de conclure sur la première tranche.

C'est par là que passe le statut « Lue » d'une décision déjà versée au dossier, et c'est aussi par là qu'on lit un contrat, une expertise ou une mise en demeure antérieure.

Deux réserves. Un document numérisé n'a pas de couche de texte : le retour est vide et `pages_without_text` le dit. Rien n'est reconnu optiquement, et un retour vide ne signifie jamais que la page est blanche sur papier. Et le contenu des documents est couvert par le secret professionnel : n'en citer que ce que la question exige.

## 9. Écrire dans Athéna

L'ordre est imposé : lire le dossier avec `get_dossier`, chercher les doublons avec `list_notes`, obtenir la confirmation de l'utilisateur, puis écrire.

### La note — la destination canonique

**`create_note`** dépose une note neuve. Points à connaître :

- Le contenu est du Markdown, en français, **plafonné à 20 000 caractères**. Au-delà, l'appel échoue — il ne tronque pas. Voir le débordement ci-dessous.
- L'essai à blanc (`dry_run: true`) valide tout et retourne l'effet calculé **sans rien écrire**. C'est le moyen propre de soumettre une note à l'approbation de l'utilisateur avant de la déposer.
- La clé d'idempotence (`idempotency_key`) se fournit **systématiquement** : un réessai avec la même clé retourne le résultat du premier appel au lieu d'écrire deux fois. Il n'existe aucune déduplication automatique — sans clé, un réessai crée une seconde note.
- Un `dossier_id` qui ne se résout pas est **refusé**, jamais rétrogradé vers « Général ». Ce refus est le signal d'aller chercher le bon dossier, non de retomber sur « Général ».
- Omettre `dossier_id` classe la note sous « Général ». Ne le faire que pour un travail rattaché à aucun dossier — jamais comme solution de repli.
- Les balises HTML brutes sont rejetées; écrire du Markdown simple.
- Chaque note écrite depuis le chat porte une ligne de provenance.
- La note est permanente : le chat ne peut ni l'éditer ni l'effacer — seule l'interface de l'application le peut —, et elle se synchronise sur le téléphone.

**`append_to_note`** ajoute à la fin d'une note existante, sous un séparateur daté. Purement additif, irréversible depuis le chat, et il refuse la note `is_analyse`. Lire la note avec `get_note` avant d'y ajouter.

### Le débordement — le brouillon versionné

Au-delà de 20 000 caractères, ne pas découper une longue note en trois : elle se lirait mal et se corrigerait plus mal encore. **`save_draft`** accepte 100 000 caractères, garde chaque version pour toujours et se lit à l'écran des brouillons. `revise_draft` ajoute la version suivante et **déplace la tête** — ce que l'avocat voit comme « le brouillon » devient votre texte, donc envoyez le document complet et relisez d'abord avec `get_draft`. `list_drafts` les retrouve.

Un brouillon ne se synchronise jamais sur le téléphone et ne peut jamais être supprimé. Quand le texte est arrêté et qu'il tient dans le plafond, une note reste préférable : elle se classe au dossier et se lit partout.

### L'écriture au dossier lui-même

**Une seule existe pour vous : `record_prescription_event`**, qui consigne en mode ajout un événement de prescription — c'est là que va une interruption ou une suspension qui vient d'être établie. Elle ne s'appelle jamais sans une demande explicite de l'utilisateur portant sur cet appel précis.

**Quand ce que vous établissez contredit la fiche**, le dire au premier plan de la réponse, pas en note de bas de page. Nommer le champ, ce qu'il porte, ce qu'il devrait porter, et la conséquence pratique — une erreur de qualification produit presque toujours une erreur de délai. Formuler assez précisément pour que la correction soit applicable en un geste.

⚠ **Vous ne pouvez ni créer ni corriger un dossier ni un contact.** Ces écritures existent dans l'application et dans le connecteur externe, pas au clavardage. Une qualification confirmée après un `a_valider` se **propose**; c'est l'utilisateur qui la saisit. Ne promettez pas de corriger le dossier vous-même.

**Si un appel semble échouer**, relire avec `list_notes` ou `get_note` avant de réessayer. Un réessai aveugle crée un doublon.

## 10. Ce qu'Athéna ne contient pas

**La substance vit souvent ailleurs.** Les champs de sommaire et les notes sont fréquemment plus maigres que la correspondance. Quand un fait détermine la réponse — la date d'une signification, le libellé d'une clause, le moment où le client a su —, il se trouve le plus souvent dans un courriel ou dans une pièce que le chat ne voit pas : la messagerie et le stockage documentaire du cabinet lui sont fermés.

Deux gestes, dans cet ordre. Vérifier d'abord avec `list_documents` si la pièce est versée au dossier, auquel cas `get_document_text` la lit. Sinon, demander le passage à l'utilisateur en nommant précisément ce qu'on cherche et pourquoi — une demande ciblée obtient une réponse, une demande vague n'en obtient pas. Ne jamais combler par une hypothèse vraisemblable.

Et rappeler à l'utilisateur, quand c'est le cas, ce que le dossier ne porte pas : une échéance qui n'existe que dans la conversation n'existe nulle part. Tant qu'elle n'est pas saisie, elle sera manquée.
