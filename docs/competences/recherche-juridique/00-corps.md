# Recherche juridique québécoise

Cette compétence établit le fondement d'une question de droit québécois : la règle applicable, les conditions d'ouverture, les délais et les pièges procéduraux. Elle sert pour une opinion, une fiche, une simple question de droit, et pour préparer le terrain avant toute rédaction.

Elle vise le droit civil et commercial québécois, en matière contentieuse. Ce n'est pas une compétence de droit criminel, fiscal ou administratif fédéral, même si elle peut y toucher incidemment.

## Le principe qui gouverne tout le reste

Une proposition de droit n'est affirmée que si sa source a été rapportée par l'outil de législation ou par l'outil de jurisprudence. Le web sert à comprendre et à repérer; il n'autorise jamais. Une piste trouvée sur le web existe dans un seul état tant qu'elle n'est pas confirmée : celui de piste, inscrite comme telle dans le livrable sous « à vérifier ».

Ce n'est pas une règle de prudence. C'est une nécessité mécanique, expliquée à la section suivante.

Le corollaire vaut pour votre propre mémoire. Un numéro d'article récité est indistinguable d'un numéro inventé, et le lecteur ne peut pas faire la différence. Aucun article ne s'énonce sans avoir été récupéré à l'outil, même quand il paraît certain.

## Ce dont vous disposez

Quatre familles d'outils, et rien d'autre.

| Famille | Ce qu'elle donne |
|---|---|
| **Le dossier** | Accès natif aux dossiers, parties, notes, tâches, documents, brouillons et échéances. Ce sont les données de l'application elle-même |
| **Législation du Québec** (`legislation_qclaw_*`) | Le texte officiel des lois et règlements québécois |
| **Jurisprudence** (`jurisprudence_canlii_*`) | Des métadonnées de décisions — jamais leur texte |
| **Recherche web** (`web_search`) | Le repérage et la doctrine. N'autorise rien, et **ne vous est pas toujours offerte** |

Les noms d'outils sont **préfixés**. C'est `legislation_qclaw_get_article`, non `qclaw_get_article`; `jurisprudence_canlii_verify_citations`, non `canlii_verify_citations`. Un nom sans préfixe n'existe pas.

**La recherche web dépend du modèle du tour.** Elle n'est offerte que sur un tour Claude. Sur un tour Gemini, l'outil n'est pas dans votre tableau : vérifiez-y avant de planifier une étape doctrinale, et s'il manque, **déclarez l'étape indisponible dans le livrable** plutôt que de la combler de mémoire. Voir `sources-doctrinales.md`.

Vous n'avez **pas** accès à la messagerie ni au stockage documentaire du cabinet. Ce qui ne se trouve ni au dossier ni chez les autres outils se demande à l'utilisateur.

## La limite qui détermine la méthode

L'outil de **législation** rend le texte officiel verbatim et se cherche par le contenu. Il porte l'essentiel du poids probatoire du livrable.

L'outil de **jurisprudence** ne rend **aucun texte de décision** et ne permet **aucune recherche par sujet**. On ne peut pas lui demander « des arrêts sur le vice caché et la prescription ». Il sait vérifier une citation qu'on lui donne, retrouver une décision par le nom des parties, lister ce qu'un tribunal a rendu entre deux dates, et parcourir le graphe des citations depuis un ancrage connu. **Le repérage par sujet vient donc nécessairement d'ailleurs** : de la doctrine, du dossier, de ses notes, de la connaissance de la matière. C'est ce qui impose la méthode de la boule de neige (voir `methode-sources.md`), et l'ignorer produit le sinistre que cette compétence existe pour prévenir — une note élégante, des citations réelles, et un ratio inventé.

Deux routes seulement mènent au texte d'une décision : **celle qui est déjà versée au dossier**, lue par `get_document_text`, et **le téléversement demandé à l'utilisateur**. Vérifier la première avant d'employer la seconde. Ne pas contourner en recopiant du texte de décision dans une note : l'usage autorisé de l'API porte sur les citations et les hyperliens, non sur la constitution d'une copie du corpus.

## Ordre des opérations

**0. Si la question se rattache à un dossier**, l'ouvrir avant tout. La marche à suivre vit dans le fichier `dossier-athena.md` de la CHARTE — `get_skill_file(skill_id="charte", filename="dossier-athena.md")`. Quand la conversation y est déjà rattachée, son `dossier_id` figure au prompt système : ne le cherchez pas. `get_dossier` livre une qualification déjà faite, des délais déjà saisis et un drapeau `a_valider` qui est un mandat de vérification explicite. Ces données orientent; elles ne prouvent rien.

**1. Trancher l'urgence avant le fond.** Si le dossier porte une échéance rapprochée — délai de réponse, dépôt du protocole, délai de rigueur de mise en état, alerte de prescription —, la question du délai se traite **en premier et se répond en premier**, quitte à livrer une réponse partielle sur le fond. Une analyse impeccable remise après l'échéance ne vaut rien. Le dire à l'utilisateur dès que l'échéance apparaît, sans attendre la fin de la recherche.

**2. Reformuler la question en questions de droit recherchables.** Une demande de praticien (« est-ce qu'on peut poursuivre ? ») se décompose : quelle est la qualification du rapport de droit, quelle norme le régit, quelles sont les conditions d'ouverture, quels moyens de défense existent, quelle réparation est possible, devant quel forum, dans quel délai. Sans cette décomposition, la recherche part dans le vide.

**3. Passe des préalables — obligatoire, même non demandée.** Voir `preliminaires.md`. Produire la grille même quand elle est vide, et l'écrire.

**4. Législation.** Cadrer, situer, puis extraire le texte — la marche détaillée est dans `methode-sources.md`, section 1. **Toujours relever la date de consolidation** retournée et la reporter dans le livrable.

**5. Doctrine.** Recherche web restreinte aux sources de `sources-doctrinales.md`, **si l'outil vous est offert**. La doctrine sert à deux choses : comprendre la règle, et repérer les autorités à vérifier ensuite. Elle est citable comme doctrine, jamais comme substitut au texte.

**6. Jurisprudence.** Les candidats viennent de l'étape 5, du dossier ou de l'utilisateur — jamais d'une recherche par sujet, qui n'existe pas. Authentifier, lire la fiche, récolter les autorités que ses mots-clés nomment, puis étendre par le citateur : c'est la boule de neige, détaillée dans `methode-sources.md`, section 3. **Grouper les vérifications** — vingt-cinq citations coûtent un seul aller-retour.

**7. Postérité.** L'outil d'historique donne un indice, pas une réponse : il ne détecte ni l'infirmation, ni les pourvois pendants, ni les refus de permission d'appeler, ni les désistements. Le signaler dans le livrable plutôt que de le présenter comme une vérification.

**8. Lecture.** Si le sens exact d'une décision détermine la réponse, ne pas deviner : la lire si elle est déjà versée au dossier, sinon en demander le téléversement (`methode-sources.md`, section 6).

**9. Attaque adverse** — systématique, jamais optionnelle.

**10. Livrable structuré**, selon `livrables.md`.

## Le statut de lecture

Chaque autorité porte un statut, et ce statut détermine ce que le livrable peut en tirer. La règle existe parce que le mode d'échec le plus dangereux consiste à lire un intitulé, une liste de mots-clés et un résumé, puis à écrire « la Cour d'appel a jugé que… ». La citation est réelle, la vérification superficielle passe, et le ratio est fabriqué.

| Statut | Ce qui est établi | Ce que le livrable peut en dire |
|---|---|---|
| **Citée** | `jurisprudence_canlii_verify_citations` — CONFIRMÉE | Nommer la décision, son tribunal, sa date; signaler qu'elle porte sur la question |
| **Résumée** | En sus, la fiche et les mots-clés de `jurisprudence_canlii_get_case` | Indiquer l'objet et le sens du dispositif tel que le résumé l'annonce, en l'attribuant au résumé |
| **Rapportée** | Ce qu'une source doctrinale identifiée en dit | Rapporter la lecture de l'auteur, en la lui attribuant nommément — jamais comme l'énoncé de la cour |
| **Lue** | Texte intégral — document du dossier lu par `get_document_text`, ou fichier fourni par l'utilisateur | Énoncer le ratio, citer un paragraphe, fonder une argumentation dessus |

**Aucune argumentation ne repose sur une autorité qui n'est pas au statut « Lue ».** Les autres soutiennent, contextualisent, orientent — et figurent au registre des sources avec leur statut apparent.

**Le statut voyage avec l'autorité.** Il ne s'arrête pas au livrable de recherche : il suit la citation dans tout ce qu'on en fera ensuite. C'est au plan d'argumentation qu'il compte le plus, puisqu'une décision non lue y serait citée à un juge. Toute autorité transmise à la rédaction l'est donc avec son statut, jamais nue.

Le verdict **DISCORDANTE** de `jurisprudence_canlii_verify_citations` mérite une vigilance particulière : la citation existe mais renvoie à une autre décision que celle annoncée par la source. C'est la signature d'une référence produite par une IA ou recopiée sans vérification. Le signaler explicitement à l'utilisateur, et écarter la source qui l'a produite.

## Écarter la France

Le web francophone juridique est dominé par la France, et **votre propre mémoire l'est aussi** : le vocabulaire est presque identique et le droit ne l'est pas. Une source française sur le vice caché, la prescription ou la responsabilité est activement dangereuse, parce qu'elle se lit comme du droit applicable.

**Le test le plus rapide est le numéro d'article.** Responsabilité extracontractuelle sous 1240 ou 1382, prescription de droit commun sous 2224, vices cachés sous 1641 : c'est français. Au Québec, ce sont 1457, 2925 et 1726 et suivants du C.c.Q. Un seul marqueur suffit : **la source entière est écartée**, pas seulement le passage. Une source qui se trompe de système ne devient pas fiable sur le reste.

Le droit canadien de common law n'est pas à écarter mais à qualifier : une discussion ontarienne d'une *limitation period* ne transpose pas. Les arrêts de la Cour suprême et le droit fédéral demeurent pertinents.

Les tables complètes de marqueurs et d'exclusions sont dans `sources-doctrinales.md`.

## Le budget : l'autorité idéale, pas le ratissage

Une recherche québécoise réussie tient en peu de sources bien choisies : le fondement législatif au bon article et au bon alinéa; un énoncé de principe, de préférence de la Cour d'appel; une ou deux applications proches sur les faits; l'autorité contraire la plus embêtante. Au-delà, on n'ajoute pas — on retire. Trois à cinq autorités vérifiées valent mieux que quinze citées : une longue liste de décisions non lues n'est pas de la rigueur, c'est du remplissage, et elle donne au lecteur une fausse impression de solidité.

**Zéro décision est parfois le bon résultat.** Faute de repérage par sujet, il arrive qu'aucun ancrage ne se présente. Une fiche qui appuie sa réponse sur le texte de loi et la doctrine, en disant qu'aucune décision n'a pu être repérée et par quels moyens on a cherché, est honnête et utile. Une fiche qui comble ce vide par des citations approximatives ne l'est pas.

Écrire au fur et à mesure plutôt que d'accumuler en mémoire de travail. Ce qui compte doit survivre à la conversation.

## L'attaque adverse

Systématique. Une recherche qui ne trouve que ce qui appuie la position du client est un piège à retardement.

Chercher activement l'autorité contraire, formuler le meilleur argument de la partie adverse dans sa version la plus forte, et signaler quand l'état du droit est incertain plutôt que de trancher. Signaler aussi l'autorité défavorable qui lie le tribunal, même — surtout — quand elle dérange.

## Les livrables

Deux formats, décrits dans `livrables.md` : **la fiche** pour une question ponctuelle, **la note de recherche** pour une analyse. Tous deux suivent la même structure en rubriques : Question, Réponse courte, Préalables, Législation, Jurisprudence, Doctrine, Application aux faits, Attaque adverse, Réserves et vérifications à faire, Registre des sources. Un troisième format, **la vérification de citations**, répond au cas d'un texte d'origine douteuse à éprouver.

**Destination.** Si la recherche se rattache à un dossier, elle vit dans Athéna comme note de catégorie `recherche`; au-delà de 20 000 caractères, elle va dans un brouillon versionné. Les mécanismes d'écriture — recherche des doublons, essai à blanc, clé d'idempotence, plafonds — sont à la section 9 du `dossier-athena.md` de la charte. Si la recherche ne se rattache à aucun dossier, produire le texte dans la conversation, sans créer de note.

## Raccord avec la compétence de rédaction

La compétence de rédaction supprime les citations du corps des procédures et des lettres : les articles s'argumentent à l'audience, pas dans la demande. Les citations vivent donc ici, et c'est ici qu'on les retrouvera.

Le partage est net. Cette compétence produit la fiche et la note de recherche. La compétence de rédaction produit la note d'analyse, la théorie de la cause et le plan d'argumentation — ce dernier étant le document où les citations réapparaissent au grand jour, devant le tribunal. Ne pas produire ici un document destiné au tribunal, ni là-bas une recherche non vérifiée.

Deux disciplines se répondent d'une compétence à l'autre : le **statut de lecture** d'une autorité, défini ici, et le **statut du fait** d'un élément du dossier, défini là-bas. Aucune des deux ne se contente de ce qu'une fiche affiche.

⚠ **Une compétence non cochée n'existe pas pour vous.** Les fichiers d'une autre compétence ne se lisent que si elle est sélectionnée sur le tour en cours. Si la rédaction ne l'est pas, ne prétendez pas consulter ses tables : dites qu'elles sont hors d'atteinte, ou demandez à l'utilisateur de cocher la case.

**Les chiffres figés dans les autres compétences sont des hypothèses, pas des acquis.** Seuils de compétence, délais, montants : les revérifier à l'outil de législation avant de les employer. Ils dérivent, et une allégation de compétence fausse se paie cher.

## Fichiers de référence

Quatre fichiers appartiennent à cette compétence. Un cinquième, **`dossier-athena.md`, appartient à la charte** — comment lire et écrire les données de l'application, la taxonomie des délais, les champs qui trompent, le protocole et ses pièges. Il se lit avec `skill_id="charte"` et il est disponible même hors de cette compétence.

- `preliminaires.md` — la grille des préalables et ses ancrages vérifiés. À lire à chaque question de fond, c'est-à-dire presque toujours.
- `methode-sources.md` — formulation des requêtes, usage détaillé des outils de source, vérification et parcours du graphe. À lire à la première recherche de la session.
- `sources-doctrinales.md` — la disponibilité de la recherche web, la liste blanche des sources québécoises et la procédure d'exclusion. À lire avant toute recherche web.
- `livrables.md` — les trois gabarits de sortie. À lire au moment de rédiger.
