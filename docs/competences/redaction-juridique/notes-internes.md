# Les notes internes

Notes d'analyse et notes de stratégie. C'est la famille où **le régime s'inverse** : ici, les citations ne sont pas seulement permises, elles sont exigées. Une note qui affirme une règle sans sa source ne vaut rien, parce qu'on ne pourra ni la vérifier, ni la défendre, ni la réutiliser dans six mois.

**C'est aussi la seule famille où le Markdown s'écrit**, parce que ces notes vivent dans l'application et y sont rendues comme du Markdown — titres, tableaux, cases à cocher. L'interdit de balisage du corps de la compétence vise les documents destinés à un gabarit Word; il ne s'applique pas ici.

**La note de recherche ne relève pas de cette compétence.** Elle appartient entièrement à la compétence de recherche, qui en fixe la méthode, les rubriques et la destination. Une demande de note de recherche s'y renvoie plutôt que de se traiter ici.

Ces notes vivent dans le dossier. Elles sont couvertes par le secret professionnel et, le cas échéant, par le privilège relatif au litige. Elles sont écrites en sachant qu'un adversaire pourrait un jour tenter d'y accéder : ne jamais y écrire ce qu'on ne pourrait pas expliquer.

## Table des matières

0. Ce qu'il faut tirer du dossier avant d'écrire
1. Le raccord avec la compétence de recherche
2. La discipline de la source
3. La note d'analyse
4. La note de stratégie — mode 1 : la théorie de la cause
5. La note de stratégie — mode 2 : le plan d'argumentation
6. Choisir le mode, et passer de l'un à l'autre
7. Ce qui distingue une note interne d'une lettre au client
8. Marqueurs de contamination

---

## 0. Ce qu'il faut tirer du dossier avant d'écrire

Une note interne qui ignore ce qui est déjà au dossier crée un doublon, et un doublon divergent est pire que pas de note du tout.

**Chercher les notes existantes d'abord, systématiquement.** Vérifier si une note porte déjà sur la même question : le cas échéant, la lire et la compléter plutôt que d'en écrire une seconde. Lire la note marquée `is_analyse` — la théorie de la cause — à laquelle toute note d'analyse ou de stratégie doit se raccorder plutôt que de la contredire en silence. Lorsqu'une divergence apparaît, la nommer explicitement : c'est souvent l'information la plus utile de la note.

Obtenir aussi la qualification, les délais et le drapeau `a_valider`, ainsi que les échéances du protocole et les audiences quand elles commandent une note de stratégie.

Les appels et leurs pièges — dont le fait que `list_notes` ne balaie par défaut que les notes « Général », et qu'il faut `scope: "cabinet"` pour chercher au-delà du dossier — sont dans le fichier `dossier-athena.md` de la charte (`skill_id="charte"`), section 7.

L'écriture au dossier suit les mêmes règles que partout : lecture, puis confirmation de l'utilisateur, puis `create_note` ou `append_to_note`. **Jamais la note `is_analyse`.**

---

## 1. Le raccord avec la compétence de recherche

La compétence de recherche gouverne **ce qui peut être affirmé** : la méthode de recherche, la vérification des citations, le statut de lecture de chaque autorité, et la structure de ses propres livrables — la fiche et la note de recherche. Ces deux documents lui appartiennent et se produisent chez elle.

Celle-ci gouverne **la manière d'écrire** les trois documents qui restent : la note d'analyse, la théorie de la cause et le plan d'argumentation. Aucun n'est un livrable de recherche; tous la présupposent.

**Le plan d'argumentation relève de cette compétence.** C'est le mode 2 de la note de stratégie, traité à la section 5 : un document sommaire remis au juge, où les renvois de droit sont explicites et cités.

Le travail préparatoire qu'il suppose — découper le recours élément par élément, et associer à chacun sa norme, le fait à alléguer, la pièce qui le prouve et le témoin qui peut en parler — ne disparaît pas pour autant : il vit dans la théorie de la cause, dont le bloc C réunit les éléments constitutifs et le bloc E la stratégie de preuve. Ces deux blocs font ce travail plus complètement qu'un tableau isolé, et ils le font au bon moment, c'est-à-dire avant la rédaction des allégations plutôt qu'à la veille de l'audience.

Lorsque la recherche n'a pas été faite, ne pas produire une note d'apparence achevée sur des propositions de droit non vérifiées. Rédiger en marquant chaque assise manquante, ou faire la recherche d'abord.

⚠ **Si la compétence de recherche n'est pas cochée sur le tour**, ses fichiers ne vous sont pas lisibles. Le dire plutôt que d'en inventer le contenu.

## 2. La discipline de la source

**Chaque proposition de droit porte sa source.** Article avec son numéro et sa date de consolidation; décision avec sa citation neutre vérifiée, son tribunal et sa date; doctrine avec son auteur, son titre et sa provenance.

**Calibrer l'affirmation sur ce qu'on a réellement lu.** « La Cour d'appel a jugé que » suppose la lecture du texte de la décision. Quand on n'a que la fiche ou le résumé, écrire « la fiche annonce que le tribunal a rejeté le moyen ». Quand on tient l'information d'un auteur, écrire « selon X, la Cour aurait retenu que ». Ce ne sont pas des faiblesses de rédaction : c'est l'information dont le lecteur a besoin pour décider s'il doit aller voir lui-même.

**Ce qui n'est pas vérifié est marqué comme tel**, dans une rubrique dédiée en fin de note plutôt que dilué dans le corps. Une note honnête sur ses trous est plus utile qu'une note lisse.

**Ne jamais citer un traité de mémoire.** Nommer l'ouvrage et la question dans la rubrique des vérifications à faire, pour consultation à une source payante.

## 3. La note d'analyse

Elle applique un droit déjà établi à une question précise du dossier. Elle ne refait pas la recherche : elle la suppose et y renvoie. Elle se distingue de la théorie de la cause par sa portée — celle-ci couvre le dossier entier, la note d'analyse couvre une question.

```
OBJET ET QUESTION POSÉE
LES FAITS TENUS POUR ACQUIS
   [pour chacun, sa source : pièce, déclaration du client, document reçu]
   [et ce qui reste incertain]
LE CADRE JURIDIQUE APPLICABLE
   [renvoi à la recherche, sans la refaire]
L'APPLICATION AUX FAITS
LES POINTS FAIBLES
CONCLUSION
CE QUI RESTE À VÉRIFIER
```

**La rubrique des faits tenus pour acquis est la plus importante et la plus négligée.** Une analyse vaut ce que valent les faits qu'elle suppose. Distinguer ce qui est documenté de ce que le client a raconté. Lorsqu'un fait déterminant est incertain, énoncer l'hypothèse retenue et indiquer comment la conclusion changerait sous l'hypothèse contraire.

**Les points faibles ne sont pas optionnels.** Une analyse qui ne trouve que ce qui appuie la position du client est un piège à retardement, et le retard tombe toujours au mauvais moment.

## 4. La note de stratégie — mode 1 : la théorie de la cause

Document exhaustif, strictement interne, qui couvre le dossier entier. C'est la note marquée `is_analyse`, de catégorie `stratégie`, une seule par dossier.

**Contrainte d'écriture avant toute autre.** Le chat ne peut pas écrire cette note : `append_to_note` la refuse et il n'existe aucun mécanisme d'édition. La compétence produit donc le texte dans la conversation — ou le dépose en brouillon avec `save_draft` si l'utilisateur le demande —, et c'est l'utilisateur qui le porte dans la note du dossier. En conséquence : produire le document complet plutôt qu'un fragment, et signaler qu'il remplace ou complète la note existante. **Lire d'abord la note en place avec `get_note`**; une théorie de la cause réécrite sans avoir lu la précédente perd le travail déjà fait.

### L'ossature exacte

⚠ **Reproduire l'ossature telle qu'elle existe dans la note du dossier**, ci-dessous. Le texte produit y est collé : une structure inventée, si bonne soit-elle, oblige à tout reformater à la main.

Trois conventions à respecter au caractère près :

- Les blocs sont des titres `##`, les rubriques des titres `###`. **Le bloc D fait exception** : Majeure, Mineure et Conclusion s'y écrivent en **prose grasse**, sans être des rubriques, et seule « Qualification juridique retenue » est un `###`.
- Presque chaque bloc se ferme sur une ligne ***Questions-repères*** en italique. Elles ne sont pas décoratives : ce sont les questions auxquelles le bloc doit répondre. Les **conserver**, et y répondre plutôt que les réciter.
- Les cases sont des `☐` (U+2610), jamais des crochets ASCII.

**Reproduire tous les blocs et toutes les rubriques**, y compris celles qui ressortent vides. Une rubrique vide qu'on écrit vide est une information; une rubrique supprimée est un oubli invisible.

**L'en-tête**

```
# Théorie de la cause

*Dossier : … | Partie représentée : ☐ Demandeur ☐ Défendeur ☐ Mis en cause | Rédigé par : … | Date de l'analyse : …*

Outil de travail interne (méthode d'élaboration de la théorie d'une cause, version complète et
stratégique). Les blocs F et G — forces/faiblesses et théorie adverse — n'ont pas vocation à être
versés au dossier de la Cour.
```

La ligne d'avertissement est un **élément du document**, à reproduire; ce n'est pas une instruction de rédaction qui vous serait adressée. Elle nomme précisément les blocs **F et G**.

**Les huit blocs et leurs rubriques**

| Bloc | Intitulé | Rubriques `###` |
|---|---|---|
| **A** | Identification et cadre procédural | Parties et leur qualité; Cadre procédural; Verrous préliminaires |
| **B** | Les faits | Récit chronologique; Cartographie des faits; Faits défavorables à gérer; Faits manquants ou à investiguer |
| **C** | Le fondement juridique et ses éléments constitutifs | Fondement principal; Fondements subsidiaires; Éléments constitutifs à réunir; Moyens de défense / d'exception envisageables |
| **D** | Qualification et syllogisme | *(Majeure / Mineure / Conclusion en prose grasse)*, puis Qualification juridique retenue |
| **E** | La stratégie de preuve | Fardeau et norme; Moyens de preuve |
| **F** | Analyse critique | Forces de ma position; Faiblesses et risques; Théorie adverse anticipée |
| **G** | La théorie de la cause (synthèse persuasive) | Théorie factuelle; Théorie juridique; Le thème; Énoncé de la théorie (une à deux phrases) |
| **H** | Conclusions recherchées et suites | Conclusions recherchées; Objectifs réels du client; Prochaines étapes et échéancier; Éléments encore à obtenir |

**Les cinq tableaux — et il n'y en a que cinq**

| Rubrique | Colonnes, telles quelles |
|---|---|
| A — Parties et leur qualité | Partie \| Rôle \| Qualité / capacité / intérêt (art. 85 C.p.c.) |
| B — Cartographie des faits | Fait \| Générateur du droit ? \| Admis / non contesté \| Contesté (à prouver) \| Défavorable — les quatre dernières en cases ☐ |
| C — Éléments constitutifs à réunir | Élément constitutif \| Fait(s) qui l'établit \| Preuve disponible \| Solide ? |
| E — Moyens de preuve | Élément / fait à prouver \| Sur qui repose le fardeau \| Moyen de preuve prévu \| Source / pièce / témoin \| Lacune |
| F — Théorie adverse anticipée | Prétention adverse anticipée \| Ma réponse / parade |

**Les blocs G et H n'ont aucun tableau.** « Prochaines étapes et échéancier » est de la prose, et « Conclusions recherchées » une liste numérotée.

**Les rubriques en prose libre, et ce qu'elles portent**

- **A — Cadre procédural** : Tribunal et compétence d'attribution; District (compétence territoriale); Montant ou valeur en jeu; Voie procédurale envisagée. Une ligne chacun.
- **A — Verrous préliminaires** : cinq cases ☐ — **Prescription** (délai applicable, point de départ, date pour agir), Intérêt et qualité pour agir (art. 85 C.p.c.), Compétence (matière et territoire), Mise en demeure / avis préalable requis ou envoyé, **Autres conditions de recevabilité**.
- **C — Fondement principal** : la cause d'action (ou, en défense, le moyen principal opposé), suivie de la ligne « Sources : ☐ législation … ☐ jurisprudence … ☐ doctrine … ». **Fondements subsidiaires** est une rubrique distincte, qui dit lesquels et pourquoi chacun est subsidiaire.
- **E — Fardeau et norme** : qui doit prouver quoi (art. 2803 C.c.Q.); la norme de la prépondérance des probabilités (art. 2804 C.c.Q.), sauf exigence légale plus stricte.
- **G — Énoncé de la théorie** : une citation en bloc (`> « … »`).
- **G** se ferme sur *Test de solidité* — une ligne italique, **non une rubrique** : la théorie est-elle cohérente, crédible, complète, simple?

### Ce qui fait la qualité de chaque bloc

**Bloc A.** Les verrous préliminaires se cochent, tous, même acquis. Un verrou coché sans vérification est pire qu'un verrou non coché : il éteint la question pour la suite du dossier. Le cadre procédural porte aussi la valeur en litige, parce que la stratégie se décide en fonction de ce qu'elle coûte.

**Bloc B.** Le récit chronologique se rédige en prose, dates et chiffres exacts, sans qualificatif. La rubrique des faits défavorables est celle qui justifie l'existence de la note : un fait défavorable non écrit ressurgit à l'interrogatoire. Consigner aussi, quand il y en a, les incohérences relevées dans les procédures déjà déposées.

**Bloc C.** Le principal et les subsidiaires ont chacun leur rubrique — le partage est structurel, pas une convention de prose. Dire pourquoi chacun est subsidiaire, et le remplir même quand il n'y en a pas : « aucun » est une réponse, l'absence de rubrique est un oubli. Un fondement subsidiaire qui survit à la chute du principal est un actif; c'est ce que la question-repère du bloc demande — si le fondement principal tombe, que reste-t-il?

**Bloc D.** Le syllogisme en trois temps, en prose grasse, puis la qualification retenue en rubrique. Lorsqu'une requalification est possible et à double tranchant, l'écrire : dire qu'elle simplifierait la démonstration, dire ce qu'elle exposerait, et dire si on la plaide ou si on se contente d'y être prêt.

**Bloc E.** La colonne « Sur qui repose le fardeau » n'est pas décorative : elle révèle les faits que l'adversaire doit prouver, et c'est souvent là que se trouve la force de la position.

**Bloc F.** Les faiblesses s'écrivent au long, avec la parade quand elle existe et l'aveu qu'il n'y en a pas quand il n'y en a pas. La théorie adverse se rédige dans sa version la plus forte.

**Bloc G.** La théorie factuelle en un paragraphe, la théorie juridique en un paragraphe, puis le thème en une phrase et l'énoncé en une citation. **Si la théorie demande trois paragraphes, elle n'est pas encore trouvée.**

**Bloc H.** Les conclusions recherchées se reproduisent telles qu'actuellement plaidées — en visant la clarté, la précision, la concision, l'ordre logique et la numérotation de l'article 99 C.p.c. —, en marquant les corrections recommandées. Les objectifs réels du client se distinguent de ce que la procédure demande, et les scénarios de règlement s'ordonnent par préférence, avec un plancher. Terminer par ce qui reste à obtenir : preuve, expertise, mandat, provision.

## 5. La note de stratégie — mode 2 : le plan d'argumentation

Document **sommaire**, destiné au **juge**, avec renvois de droit explicites et cités. C'est l'inverse exact du mode 1 par le destinataire, la longueur et la retenue, alors que la matière est la même.

**Ce qu'il n'est pas.** Le plan d'argumentation n'est pas un acte de procédure. Le Code de procédure civile ne lui donne aucune forme : il connaît la plaidoirie comme phase des débats (art. 265 C.p.c.) et le temps qui y est alloué (art. 385 C.p.c.), non un écrit qui la porte. Il en découle deux règles fermes. D'abord, **une conclusion qui figure au plan sans figurer à la procédure n'a aucun effet** : si la conclusion manque, il faut amender, pas plaider. Ensuite, les limites de longueur, les délais de remise et les usages de dépôt relèvent des règlements de procédure et des directives du district — les vérifier plutôt que les supposer.

### Structure par défaut

```
RÉSUMÉ
CADRE JURIDIQUE
APPLICATION AUX FAITS
CONCLUSIONS RECHERCHÉES
```

**Résumé.** Le litige et la position, en un ou deux paragraphes. C'est ici que la théorie de la cause du bloc G devient plaidable : la théorie juridique et la théorie factuelle s'y transposent, dépouillées de leur appareil interne. Le juge doit savoir, après le premier paragraphe, ce qu'on lui demande et pourquoi.

**Cadre juridique.** La règle applicable et ses sources, dans l'ordre civiliste : le texte, puis la jurisprudence qui l'interprète, puis la doctrine s'il y a lieu. Chaque proposition porte sa référence complète, avec le point précis lorsqu'il s'agit d'une décision. C'est la section où cette compétence lève son interdit habituel de citer : ici la citation est la substance.

**Application aux faits.** Chaque élément constitutif, confronté aux faits mis en preuve, avec le renvoi à la pièce ou au témoignage. C'est du bloc C et du bloc E que cette section se tire.

**Conclusions recherchées.** Reprises **mot pour mot** de la procédure. Toute divergence entre le plan et la procédure sera relevée et coûtera plus qu'elle ne rapporte.

### Variantes

| Contexte | Structure |
|---|---|
| **Au fond, après enquête** | La structure par défaut, l'application aux faits renvoyant à la preuve administrée |
| **Sur moyen préliminaire** (déclinatoire, irrecevabilité, abus) | Le moyen soulevé; le critère applicable et sa source; pourquoi il est ou n'est pas rencontré; conclusions. Pas de récit factuel étendu — sur un moyen préliminaire, les faits allégués sont tenus pour avérés |
| **Sur demande interlocutoire** (injonction, sauvegarde, provision) | Les critères, un par section, dans l'ordre où le tribunal les examine; l'urgence et la balance des inconvénients en dernier; conclusions |
| **En défense** | Résumé de la position; ce qui est admis; ce qui est contesté et pourquoi; cadre juridique; conclusions en rejet |

Le mémoire d'appel obéit à un régime distinct et codifié, avec ses propres exigences de forme et de délai. Il ne se rédige pas sur ce gabarit; vérifier le régime applicable avant d'y toucher.

### Discipline de citation

Les références suivent le *Manuel canadien de la référence juridique*, avec citation neutre et renvoi au paragraphe précis lorsqu'il s'agit d'une décision.

**Aucune décision ne se cite au juge sans avoir été lue.** C'est ici que la règle du statut de lecture porte le plus : « la Cour d'appel a jugé que » adressé à un juge, sur la foi d'un résumé, est une faute professionnelle en attente d'être découverte. Une décision qui n'est connue que par sa fiche ou par un auteur ne va pas au plan d'argumentation — ou y va avec l'attribution exacte de ce qu'on en sait. Lorsque le texte d'une décision déterminante n'a pas pu être obtenu, le dire à l'utilisateur avant qu'il ne dépose.

Prévoir le cahier d'autorités correspondant : chaque source citée au plan doit s'y retrouver, et rien d'autre.

### Ce qui ne franchit jamais la porte

La règle ne tient pas au découpage en blocs de la théorie de la cause, mais au destinataire. Tout ce qui a été écrit pour se prémunir contre une difficulté cesse d'être utile dès lors que c'est le juge qui lit — et devient nuisible.

Ne jamais laisser passer dans un document remis au tribunal : l'aveu d'une faiblesse formulé comme tel, l'évaluation du risque de recouvrement, les objectifs réels du client, les scénarios de règlement et leur plancher, l'état de la provision et les honoraires, l'appréciation de la crédibilité d'un témoin du client, les incohérences relevées dans nos propres procédures et non encore corrigées, et le thème dans sa formulation rhétorique interne.

Anticiper l'argument adverse est légitime et souvent habile; exposer ses propres faiblesses ne l'est pas. La distinction se fait à la formulation : « la partie défenderesse soutiendra que…, mais » se plaide; « notre point faible est… » ne se plaide pas.

**Un dernier contrôle avant remise du bloc :** relire en se demandant, de chaque phrase, si elle serait encore écrite ainsi en sachant que la partie adverse la lira. Elle la lira.

## 6. Choisir le mode, et passer de l'un à l'autre

| Signal | Mode |
|---|---|
| Ouverture d'un dossier, réception d'un mandat, préparation d'une procédure, réévaluation après un fait nouveau | **Mode 1** — théorie de la cause |
| Audience fixée, moyen préliminaire à présenter ou à contester, demande interlocutoire, plaidoirie à préparer | **Mode 2** — plan d'argumentation |
| Une question de droit précise appliquée au dossier | **Note d'analyse**, section 3 |
| Une question de droit à établir | **La compétence de recherche**, pas celle-ci |

Le mode 1 alimente le mode 2, jamais l'inverse. Quand la théorie de la cause existe, la lire avant d'écrire un plan d'argumentation : le plan en est la version publiable, et il doit être cohérent avec elle. Quand elle n'existe pas et que le dossier a de l'ampleur, le signaler — un plan d'argumentation écrit sans théorie de la cause préalable est un plan qui découvre ses faiblesses à l'audience.

Lorsque l'utilisateur demande simplement « une note de stratégie », déterminer le mode par le contexte du dossier plutôt que de choisir au hasard, et si le contexte ne tranche pas, demander.

## 7. Ce qui distingue une note interne d'une lettre au client

Elles se confondent souvent, à tort. La note interne est un instrument de travail : elle consigne les incertitudes, les hypothèses, les faiblesses, les questions à poser. La lettre au client est un instrument de communication : elle explique, elle situe, elle demande une décision.

Une note interne transformée en lettre par simple copie inquiète inutilement le client ou lui livre des considérations tactiques qu'il n'a pas les moyens de pondérer. Une lettre au client transformée en note interne perd tout ce qui rend la note utile — précisément les réserves qu'on avait adoucies.

Lorsque l'utilisateur demande les deux, produire deux textes distincts.

## 8. Marqueurs de contamination

Dans cette famille, la contamination passe surtout par les autorités citées.

**Doctrine ou jurisprudence française** invoquée pour éclairer une notion québécoise : le vocabulaire coïncide, le droit ne coïncide pas. Le test du numéro d'article s'applique intégralement — 1240, 1382, 2224, 1641, 1134 signalent la France.

**Autorités de common law canadien** transposées sans qualification : une analyse ontarienne d'une *limitation period* ne transpose pas à la prescription du C.c.Q., et une discussion de *duty of care* n'éclaire pas l'article 1457 C.c.Q. Les arrêts de la Cour suprême et le droit fédéral demeurent pleinement pertinents, y compris en anglais.

**Le syllogisme à la française** — majeure, mineure, conclusion, avec « en l'espèce » à chaque paragraphe — n'est pas faux, et le bloc D de la théorie de la cause le demande explicitement. Ce qui contamine, c'est de l'étendre à toute la note : la structure québécoise est plus proche du raisonnement de l'avocat — la règle, son application, ce qui résiste.

**Le format de mémo américain** — IRAC, *bluebook*, notes de bas de page abondantes — importe une mise en forme sans importer de contenu. Les citations québécoises suivent le *Manuel canadien de la référence juridique*.

Les tables complètes sont dans `contamination.md` de la charte (`skill_id="charte"`).
