# Les livrables

Deux formats principaux — la fiche et la note de recherche — qui partagent une seule structure, et un format court réservé à la vérification de citations. La présentation est cartésienne : on sépare les sources par nature avant de les appliquer aux faits, de sorte que le lecteur voie d'où vient chaque proposition et puisse en contester une sans démonter le reste.

Le plan d'argumentation ne figure pas ici : il appartient à la compétence de rédaction, qui en fait le mode 2 de sa note de stratégie. Ce que la recherche lui transmet, ce sont des autorités vérifiées **avec leur statut de lecture**, jamais des citations nues.

## 1. La structure commune

Suivre cet ordre. Il est civiliste : le texte fonde, la jurisprudence interprète, la doctrine explique, et l'application vient après.

```
## Question
## Réponse courte
## Préalables
## Législation
## Jurisprudence
## Doctrine
## Application aux faits
## Attaque adverse
## Réserves et vérifications à faire
## Registre des sources
```

**Question** — reformulée en question de droit, pas la demande telle que reçue. S'il y en a plusieurs, les numéroter et garder l'ordre dans tout le document.

**Réponse courte** — trois à cinq lignes. Ce qu'on répondrait au téléphone. Si la réponse est incertaine, le dire ici plutôt que de le réserver aux réserves.

**Préalables** — la grille de `preliminaires.md`, y compris les rubriques vides. Le délai et sa nature y figurent toujours, même quand la question ne portait pas dessus.

**Législation** — les articles, avec leur numéro, leur objet et la date de consolidation. Citer le texte lorsqu'il est court et déterminant; le paraphraser sinon. Indiquer quelle version s'applique quand la question est temporelle.

**Jurisprudence** — chaque décision avec sa citation vérifiée, son tribunal, sa date et **son statut de lecture**. Ne jamais énoncer un ratio pour une décision qui n'est pas au statut « Lue ». Une décision seulement « Résumée » se présente comme telle : « la fiche annonce que le tribunal a rejeté le moyen d'irrecevabilité », et non « le tribunal a jugé que ».

**Doctrine** — auteur, titre, source, et ce que l'auteur en dit. Distinguer la doctrine savante du bulletin de cabinet. **Si l'outil de recherche web n'était pas disponible sur ce tour, l'écrire ici** — « recherche doctrinale non effectuée, outil indisponible » — et reporter la vérification aux réserves. Une rubrique vide qui s'explique vaut mieux qu'une rubrique remplie de mémoire.

**Application aux faits** — c'est ici, et seulement ici, que les sources rencontrent le dossier. Si les faits sont incertains, énoncer l'hypothèse retenue et signaler comment la réponse changerait sous une autre hypothèse.

**Attaque adverse** — le meilleur argument contraire, dans sa version la plus forte, avec les autorités qui le portent. Systématique.

**Réserves et vérifications à faire** — les pistes non confirmées, les décisions à lire, les vérifications doctrinales suggérées, les points où l'état du droit est incertain, et ce que le retard de consolidation laisse en suspens.

**Registre des sources** — tableau final : autorité, statut, provenance, vérification.

| Autorité | Statut | Provenance | Vérifiée |
|---|---|---|---|
| art. 2925 C.c.Q. | Texte officiel | `legislation_qclaw_get_article`, consolidé au AAAA-MM-JJ | ✓ |
| 2026 QCCS 2659 | Résumée | `jurisprudence_canlii_verify_citations` — CONFIRMÉE | oui |
| Bulletin [cabinet], [date] | Doctrine praticienne | Web, liste blanche | s.o. |

## 2. La fiche

Une question, une page. Question, réponse courte, préalables, fondement avec les articles, autorités avec leur statut, réserves. On saute l'application aux faits quand la question est abstraite, jamais l'attaque adverse ni le registre.

C'est le format par défaut d'une question posée en passant.

## 3. La note de recherche

La structure complète, développée. C'est le format d'une opinion, d'une analyse de dossier ou d'une question à plusieurs volets.

Prose plutôt que listes à puces, sauf pour les données structurées, qui vont en tableau.

## 4. La vérification de citations

Format court, pour le cas particulier d'un texte d'origine douteuse — un mémoire reçu, une recherche produite par une IA, un bulletin recopié — dont il faut éprouver les autorités avant de s'y fier.

Analyser d'abord les formes avec `jurisprudence_canlii_parse_citation` pour écarter celles qui ne se résoudront pas, puis vérifier le reste par lots de vingt-cinq avec `jurisprudence_canlii_verify_citations`. Rendre un tableau, et rien d'autre :

| Citation telle qu'annoncée | Intitulé annoncé | Verdict | Ce que la fiche donne réellement |
|---|---|---|---|

Faire suivre de trois lignes au plus : combien de citations ont été éprouvées, combien sont confirmées, et ce qu'il faut en conclure sur la source.

**Le verdict DISCORDANTE commande une conclusion, pas une note de bas de page.** Une citation qui existe mais désigne une autre décision que celle annoncée est la signature d'une référence fabriquée. Une seule suffit à retirer sa fiabilité à l'ensemble du texte : le dire explicitement et recommander de tout revérifier plutôt que de corriger la ligne fautive.

NON CONSTRUCTIBLE n'est pas un échec — les citations de recueils (R.C.S., R.J.Q., C.A.) et les identifiants d'éditeurs (J.E., REJB, EYB, AZ) ne se résolvent pas directement. Enchaîner avec `jurisprudence_canlii_find_case` sur le nom des parties avant de conclure. INTROUVABLE n'établit pas l'inexistence : la couverture a des bornes historiques et la diffusion connaît un délai.

## 5. Destination

### Recherche rattachée à un dossier

L'ordre est imposé : `get_dossier` d'abord — on n'écrit jamais sur un dossier qu'on n'a pas lu; `list_notes` ensuite, avec son paramètre `query`, pour vérifier qu'aucune note ne porte déjà sur la même question; confirmation de l'utilisateur; puis l'écriture.

**Sous 20 000 caractères : une note de catégorie `recherche`** (`create_note`, ou `append_to_note` si une note de recherche existe déjà sur le point). C'est la destination canonique — la note se classe au dossier, se synchronise sur le téléphone et ne s'efface jamais.

**Au-delà : un brouillon versionné** (`save_draft`, 100 000 caractères). Ne pas découper une longue note en trois : elle se lirait mal et se corrigerait plus mal encore. Le brouillon garde chaque version, se révise par `revise_draft` — qui **déplace la tête**, donc relire d'abord avec `get_draft` et envoyer le document complet — et ne se synchronise pas sur le téléphone.

**Toute la mécanique d'écriture est décrite à la section 9 du `dossier-athena.md` de la charte** (`skill_id="charte"`) : les plafonds et le débordement, l'essai à blanc, la clé d'idempotence, l'absence totale de déduplication, et le refus — jamais la rétrogradation — d'un `dossier_id` qui ne se résout pas. La lire avant le premier appel d'écriture de la session.

**Ne jamais viser la note marquée `is_analyse`** — la théorie de la cause — qui est en lecture seule.

**Vous ne pouvez pas corriger le dossier lui-même.** La seule écriture au dossier qui vous soit ouverte est `record_prescription_event`, et seulement sur demande explicite. Une correction de qualification, de délai ou de forum se **propose** : c'est l'utilisateur qui la saisit dans l'application.

### Recherche générale

Rattachée à aucun dossier — produire le texte dans la conversation. Ne pas créer de note « Général ».

## 6. Trois règles de rédaction

**Calibrer l'affirmation sur la preuve.** « La Cour d'appel a jugé que » suppose une lecture. « La fiche annonce », « selon tel auteur », « sous réserve de lecture » sont des formulations honnêtes, pas des faiblesses. Le lecteur est un avocat : il sait lire un degré de certitude, et il en a besoin.

**Ne pas remplir.** Une note courte et sûre vaut mieux qu'une note longue et molle. Quand une rubrique est vide, écrire qu'elle est vide et pourquoi — c'est une information, et souvent la plus utile.

**Remonter ce qui contredit le dossier.** Lorsque la recherche établit que la qualification, le délai, le point de départ ou le forum inscrits au dossier sont inexacts, cela va en tête du livrable, sous la réponse courte — pas dans les réserves. Une erreur de qualification produit presque toujours une erreur de délai, et c'est souvent la seule chose de la note qui appelle une action le jour même.

Nommer le champ, ce qu'il porte, ce qu'il devrait porter, et la conséquence pratique. **La correction passe par l'utilisateur** : aucun outil du clavardage n'écrit un dossier ni un contact. La formuler assez précisément pour qu'elle soit applicable en un geste.
