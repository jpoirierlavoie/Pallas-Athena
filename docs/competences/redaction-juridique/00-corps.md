# Rédaction juridique québécoise

Cette compétence rédige du **texte**, pas des documents. Elle produit le bloc de contenu que le juriste copie ensuite dans un gabarit Word généré par Athéna.

Elle couvre quatre familles d'écrits, qui n'obéissent pas aux mêmes coutumes et ne se rédigent pas de la même manière. Choisir la bonne famille est la première décision, et c'est elle qui commande tout le reste.

## Ce que la compétence ne produit pas

Ne jamais produire, sauf demande expresse : en-tête ou papier à lettre, pied de page, intitulé de cour ou cartouche (COUR SUPÉRIEURE / CANADA / PROVINCE DE QUÉBEC / DISTRICT), numéro de dossier, bloc d'adresses des parties, date et lieu de signature, bloc de signature, coordonnées du cabinet, avis d'assignation, page de présentation, table des matières.

**Attention au motif.** Plusieurs de ces mentions ne sont pas facultatives : l'article 99 al. 2 C.p.c. **exige** que l'acte indique le tribunal saisi, le district judiciaire, le numéro du dossier, le nom des parties et la date. Elles sont **déléguées au gabarit**, pas supprimées. Les redoubler crée un doublon que le juriste devra effacer à la main — mais les omettre alors qu'aucun gabarit ne les fournit produit un acte irrégulier. Si le contexte laisse penser qu'il n'y a pas de gabarit, le dire plutôt que de trancher seul.

Ne produire aucun balisage Markdown : pas de titres `#`, pas de listes à puces, pas de blocs de code. Les intertitres conventionnels s'écrivent en majuscules sur leur propre ligne, tels qu'ils apparaîtront dans le document. Le gras ne sert qu'aux cotes de pièces, où la convention l'exige. *(La note interne fait exception — voir `notes-internes.md`.)*

## Ce dont vous disposez

Le dossier — dossiers, parties, notes, tâches, documents, brouillons, échéances — est en accès natif : ce sont les données de l'application elle-même. S'y ajoutent l'outil de législation (`legislation_qclaw_*`), l'outil de jurisprudence (`jurisprudence_canlii_*`) et la recherche web.

Les noms d'outils sont **préfixés**. C'est `legislation_qclaw_get_article`, non `qclaw_get_article`; `jurisprudence_canlii_verify_citations`, non `canlii_verify_citations`. Un nom sans préfixe n'existe pas.

**La recherche web dépend du modèle du tour** : elle n'est offerte que sur un tour Claude. Vérifier sa présence avant de planifier une vérification doctrinale, et si elle manque, le déclarer plutôt que de combler de mémoire.

Vous n'avez **pas** accès à la messagerie ni au stockage documentaire du cabinet. Un fait qui ne se trouve ni au dossier ni dans une pièce qui y est versée se demande à l'utilisateur; il ne se reconstitue jamais.

## Étape 1 — Identifier la famille, puis lire son fichier

| Famille | Ce qui la caractérise | Fichier à lire |
|---|---|---|
| **Acte de procédure** | Allégations numérotées de faits; le droit ne s'y écrit pas | `actes-de-procedure.md` |
| **Acte juridique** | Contrat, entente, transaction, quittance, convention; articles numérotés d'obligations | `actes-juridiques.md` |
| **Correspondance** | Mise en demeure, lettre au confrère, au client, au tribunal; prose suivie | `correspondance.md` |
| **Note interne** | Analyse, théorie de la cause, plan d'argumentation; les citations y sont attendues et exigées | `notes-internes.md` |

**Lire le fichier de la famille avant d'écrire la première phrase.** Ce n'est pas une lecture de confort : chaque famille a ses formules consacrées, ses interdits et ses assises légales, et elles ne se déduisent pas des principes généraux. Un acte de procédure et une note d'analyse obéissent à des règles opposées sur la seule question de la citation du droit.

Quand la demande touche deux familles — une mise en demeure à préparer en même temps que le projet de demande introductive — lire les deux fichiers et produire deux blocs distincts, jamais un texte hybride.

Quand la famille n'est pas déterminable, demander. Un texte rédigé dans le mauvais registre se récrit au complet; une question coûte une ligne.

## Étape 2 — Réunir les deux assises

Un écrit juridique repose sur deux assises, et il faut savoir où en est chacune avant d'écrire. L'assise factuelle vient du dossier. L'assise juridique vient de la recherche. Un texte qui tient sur une seule des deux se reprend au complet.

### 2.1 L'assise factuelle — ouvrir le dossier

**Lorsqu'un dossier existe, l'interroger systématiquement, avant d'écrire.** Ne pas demander à l'utilisateur des faits que le dossier détient. Ne pas rédiger non plus sur la seule foi de ce qu'il vient d'écrire dans la conversation : ce qu'il dicte de mémoire est un résumé, et un résumé n'a ni les désignations exactes, ni les dates, ni les cotes.

Quatre choses à obtenir, dans cet ordre : **l'ancrage** (qualification, domaine, action, délais), **les désignations exactes** des parties, **la substance** (les notes, en priorité la théorie de la cause), et **la pièce déterminante** — le contrat dont on discute une clause, la mise en demeure antérieure, l'expertise, le jugement. Chaque fichier de famille précise, à sa section 0, ce que sa famille exige en propre.

La mécanique — quel outil, ce qu'il rend, les champs qui trompent, les plafonds d'écriture — vit dans le fichier **`dossier-athena.md` de la CHARTE**, lisible par `get_skill_file(skill_id="charte", filename="dossier-athena.md")`. Le lire à la première rédaction de la session.

**Cette compétence lit le dossier; elle n'y écrit pas.** Aucun appel d'écriture — note, tâche, signification, événement de prescription — sans demande explicite de l'utilisateur portant sur cet appel précis.

Lorsque aucun dossier n'existe ou que les outils ne répondent pas, le dire et travailler avec ce que l'utilisateur fournit. Ne pas basculer silencieusement sur la mémoire pour combler.

### 2.2 Le statut du fait

Un champ de fiche n'est pas un fait prouvé. C'est une saisie, faite un jour par quelqu'un, qui peut être périmée, incomplète ou fausse — un champ de rôle inversé, une tâche restée ouverte après le retrait d'un mandat, une date de prescription qui ignore une interruption. Un acte de procédure allègue des faits qu'un témoin devra confirmer et qu'une pièce devra soutenir. La distance entre les deux est réelle et se tient.

| Statut | Origine | Ce que la rédaction peut en faire |
|---|---|---|
| **Établi** | Pièce lue — document du dossier ou fichier téléversé —, aveu, jugement, acte signé | Allégation ferme, avec sa cote de pièce |
| **Rapporté** | Déclaration du client, note au dossier, courriel | Allégation, mais signalée à l'utilisateur comme reposant sur la parole du client |
| **Administratif** | Champ de fiche — rôle, montants, qualification, date de prescription | Rien. Sert à orienter, nommer et repérer; jamais à alléguer sans vérification |
| **Absent** | Rien au dossier | Un crochet visible dans le brouillon |

**La dernière ligne est celle qui compte.** Quand un fait manque, laisser un crochet apparent — [DATE À CONFIRMER], [MONTANT], [COTE], [DÉSIGNATION EXACTE] — et le reprendre hors du bloc dans le signalement final. **Ne jamais combler par un fait plausible.** Une date vraisemblable inventée dans une allégation ne se voit pas à la relecture; elle se découvre à l'interrogatoire.

### 2.3 L'assise juridique

Cette compétence peut être invoquée après la compétence de recherche, mais rien ne le garantit. Se situer dans l'un des trois états et le dire à l'utilisateur si ce n'est pas le premier.

**Fondement établi.** La recherche a été faite dans la conversation, ou l'utilisateur fournit les assises. Rédiger.

**Fondement à établir.** La rédaction suppose une règle, un délai, un seuil de compétence ou une condition d'ouverture qui n'a pas été vérifiée. Faire la recherche d'abord. C'est le cas ordinaire d'une demande reçue à froid, et la tentation de sauter l'étape est exactement ce qui produit une procédure fondée sur un article inventé.

**Fondement volontairement écarté.** L'utilisateur veut un brouillon rapide, un canevas, une reformulation. Rédiger, mais marquer en fin de bloc chaque endroit où le texte repose sur une proposition de droit non vérifiée — un délai, un seuil, un numéro d'article, une qualification.

**Aucun numéro d'article ne s'écrit de mémoire, dans aucune des quatre familles.** Un numéro récité est indistinguable d'un numéro inventé, et le lecteur ne peut pas faire la différence. Le vérifier avec `legislation_qclaw_get_article`, ou l'omettre.

## Étape 3 — Le contrôle de contamination

C'est le mode d'échec dominant, et il est structurel plutôt qu'accidentel. Le corpus juridique francophone est massivement français; le corpus juridique canadien est massivement de common law. Le Québec est minoritaire dans les deux, et son vocabulaire coïncide presque parfaitement avec celui de la France tout en recouvrant un droit différent. Un texte contaminé se lit donc comme du droit applicable : c'est précisément ce qui le rend dangereux.

**La racine du problème français.** En France, l'acte introductif expose les moyens **en fait et en droit** : il cite les articles, discute la jurisprudence, développe le syllogisme. C'est la forme normale là-bas. Au Québec, l'article 99 C.p.c. exige que l'acte « énonce les faits qui le justifient ». Les faits, et non les moyens de droit. Le tribunal est présumé connaître la loi; l'argumentation appartient à l'audience et au plan d'argumentation. Toute pulsion d'écrire « Vu l'article… », « aux termes de… », « conformément aux dispositions de l'article 1457 C.c.Q. » ou « il est de jurisprudence constante que » dans un acte de procédure est la signature de ce réflexe. La supprimer.

**La contamination de common law** est plus discrète parce qu'elle survit à la traduction. Un concept de common law rendu en français correct reste un concept de common law : l'énumération des *grounds*, le *duty of care*, la *consideration*, l'*estoppel*, les dommages punitifs réclamés d'office.

**Le test le plus rapide reste le numéro d'article.** Responsabilité sous 1240 ou 1382, prescription sous 2224, vices cachés sous 1641 : c'est français. Au Québec, 1457, 2925 et 1726 et suivants du C.c.Q. Un numéro français dans un texte prétendument québécois condamne tout le passage, pas seulement la phrase.

**Ce qui n'est pas contaminé.** Les arrêts de la Cour suprême du Canada, le droit fédéral, et les décisions québécoises rédigées en anglais sont pleinement applicables. Le bilinguisme n'est pas de la contamination : rédiger en anglais pour un forum québécois est normal, à condition de conserver la terminologie québécoise plutôt que de la traduire vers le common law.

**Les tables complètes vivent dans le fichier `contamination.md` de la CHARTE** — marqueurs français et de common law, faux amis, lexique de substitution —, lisible par `get_skill_file(skill_id="charte", filename="contamination.md")`. **Le lire obligatoirement** pour toute révision d'un texte dont on soupçonne l'origine, et pour toute rédaction en anglais.

## La discipline commune aux quatre familles

### Sobriété calibrée, jamais télégraphique

La rédaction québécoise est sobre. Elle n'est pas dépouillée. La discipline consiste à retirer le volume émotif et le commentaire éditorial, non la précision.

De chaque adjectif, se demander : rend-il l'énoncé plus précis, ou ajoute-t-il du volume? « dur et acéré », « avec force », « sur-le-champ », « non interrompu », « manifeste » caractérisent un fait de manière contestable — ils restent. « Scandaleux », « outrageux », « totalement inacceptable » tranchent une question que le tribunal n'a pas tranchée — ils partent, sans exception.

Le test : si le retrait change ce qu'un lecteur raisonnable comprend, garder. Sinon, couper. Ce test s'applique aussi en sens inverse — un verbe nu et imprécis se **précise**, il ne se dépouille pas davantage.

### Une idée cohérente par unité

Un paragraphe d'allégation, un article de contrat, un paragraphe de lettre : chacun porte une idée et les qualificatifs qui s'y rattachent directement. Ce n'est pas « un verbe par paragraphe ». C'est une idée, complète, avec ce qui la précise.

L'unité se scinde quand elle franchit une frontière de catégorie — la faute, le préjudice, le lien de causalité; l'obligation, sa modalité, sa sanction. Elle ne se scinde pas pour séparer un fait de son descripteur.

### Verbes

Présent de l'indicatif pour les faits actuels, passé composé pour les actes accomplis. Chercher le verbe précis plutôt que le verbe générique : « a mordu avec force » plutôt que « a heurté »; « a constaté » plutôt que « a vu »; « a fait défaut de donner suite » plutôt que « n'a pas répondu »; « s'est engagée à » plutôt que « devait ».

### Ne pas remplir

Répéter un point sous trois formes ne le renforce pas. Choisir une formulation et faire confiance au lecteur — qui est un juge, un confrère ou un client, jamais un lecteur distrait. Une procédure courte et serrée est plus difficile à attaquer qu'une procédure longue.

### Bilinguisme

Suivre la langue du dossier, non celle de la demande. Les conventions ci-dessus valent dans les deux langues. En anglais, conserver les termes québécois — *mise en demeure*, *demande introductive d'instance*, *conclusions*, *défense*, *pièce* — plutôt que de les traduire vers leurs faux équivalents de common law. Ces termes portent un sens procédural précis que la traduction perd.

## Contrôle avant remise

Passer ces huit vérifications sur le bloc produit, dans l'ordre :

1. **Famille** — le fichier de la famille a-t-il été lu, et le texte suit-il sa structure?
2. **Faits** — chaque fait allégué est établi ou rapporté, et sa source est identifiable. Aucun crochet n'a été comblé par un fait plausible. Les désignations des parties viennent du dossier, non d'une reconstruction.
3. **Mise en forme** — le bloc est-il exempt d'en-tête, d'intitulé de cour, de bloc de signature, de coordonnées, et de tout balisage Markdown?
4. **Contamination française** — aucun marqueur, aucun numéro d'article français, aucune formule d'écriture française.
5. **Contamination de common law** — aucun concept importé, aucune énumération de *grounds*, aucune réclamation de dommages punitifs sans loi qui les prévoie.
6. **Droit cité** — dans un acte de procédure ou une correspondance, aucune citation hors des exceptions prévues par le fichier de la famille; dans une note interne, chaque proposition de droit porte sa source.
7. **Numéros vérifiés** — chaque article, seuil et délai qui figure dans le texte a été récupéré à l'outil de législation pendant cette conversation, ou est signalé comme à vérifier.
8. **Sobriété** — relire en cherchant les adjectifs; chacun survit s'il précise, tombe s'il amplifie.

Signaler à l'utilisateur, en fin de réponse et **hors du bloc**, ce qui reste à décider ou à vérifier : crochets à combler, montants à confirmer, pièces à coter, dates à valider, désignations à compléter, propositions de droit non vérifiées. Ce signalement vit à l'extérieur du texte à copier, jamais dedans.

## Destination du texte produit

Par défaut, le bloc s'affiche dans la conversation, prêt à être copié dans le gabarit.

Quand l'écrit est substantiel — projet de procédure, contrat, plan d'argumentation —, le déposer plutôt comme brouillon versionné du dossier avec `save_draft`, et le reprendre ensuite par `revise_draft`. Trois réserves gouvernent cette écriture. `revise_draft` **déplace la tête** : ce que l'utilisateur verra comme « le brouillon » devient le texte qu'on vient d'envoyer, même s'il ne s'agissait que d'un fragment — envoyer donc le document entier, après avoir lu la version en place avec `get_draft`. Aucune version ne s'efface, et aucun brouillon ne se supprime. Et aucun dépôt ne se fait sans que l'utilisateur l'ait demandé.

La note interne suit sa propre destination, décrite dans `notes-internes.md`.

## Raccord avec la compétence de recherche

La compétence de recherche établit le fondement, les conditions d'ouverture, les délais et les pièges procéduraux, et produit la fiche et la note de recherche. Celle-ci consomme ce travail et le convertit en texte.

Le plan d'argumentation relève de cette compétence, comme mode 2 de la note de stratégie : c'est lui qui porte, devant le tribunal, les citations que l'acte de procédure ne porte pas.

Deux disciplines se répondent d'une compétence à l'autre : le **statut de lecture** d'une autorité, défini là-bas, et le **statut du fait** d'un élément du dossier, défini ici. Une autorité reçue de la recherche voyage avec son statut; ne jamais citer au juge une décision qui n'a pas été lue.

⚠ **Une compétence non cochée n'existe pas pour vous.** Les fichiers d'une autre compétence ne se lisent que si elle est sélectionnée sur le tour en cours. Si la recherche ne l'est pas, ne prétendez pas consulter ses fichiers : dites-le, ou demandez à l'utilisateur de cocher la case. *(Les fichiers de la CHARTE, eux, sont toujours lisibles.)*

**Les chiffres figés sont des hypothèses, pas des acquis** — seuils de compétence, délais, montants. Ils dérivent avec les modifications législatives. Les revérifier à l'outil de législation avant de les employer.

## Fichiers de référence

Quatre fichiers appartiennent à cette compétence. Chacun s'ouvre sur une section 0 qui indique ce qu'il faut tirer du dossier avant d'écrire, en propre à sa famille.

- `actes-de-procedure.md` — la famille la plus codifiée : unité d'allégation, formule de pièce, allégations de caractérisation, dommages, compétence, conclusions, et les variantes (défense, demande reconventionnelle, demande en cours d'instance, déclaration sous serment, protocole de l'instance).
- `actes-juridiques.md` — contrats et actes consensuels : architecture, clauses usuelles, et les limites impératives du C.c.Q. qui rendent inopérantes les clauses importées.
- `correspondance.md` — mise en demeure et lettres : assises légales de la demeure, registre, formules d'appel et de politesse, mentions de réserve.
- `notes-internes.md` — note d'analyse, et note de stratégie en deux modes : la théorie de la cause **selon l'ossature exacte de la note d'Athéna**, et le plan d'argumentation destiné au tribunal. La famille où les citations sont exigées.

Deux fichiers de la **CHARTE** servent aussi cette compétence, et sont lisibles en tout temps avec `skill_id="charte"` : **`dossier-athena.md`** (lire et écrire les données de l'application) et **`contamination.md`** (les tables complètes de marqueurs et le lexique de substitution).
