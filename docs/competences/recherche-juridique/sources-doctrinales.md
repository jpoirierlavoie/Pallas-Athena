# Sources doctrinales : disponibilité, liste blanche et exclusions

## 0. D'abord : l'outil est-il là ?

**La recherche web n'est pas toujours dans votre tableau d'outils.** Elle est offerte sur un tour Claude et **absente sur un tour Gemini** — le modèle du tour décide, et vous ne choisissez pas le modèle. Vérifiez la présence de `web_search` avant de planifier une étape doctrinale.

**S'il manque, l'étape doctrine se déclare indisponible.** Écrivez-le dans le livrable, sous la rubrique Doctrine et dans les réserves : « recherche doctrinale non effectuée — l'outil de recherche web n'était pas disponible sur ce tour ». C'est une information utile pour le lecteur, qui saura quoi faire vérifier.

**Ne comblez pas de mémoire.** C'est précisément le cas où la tentation est forte et la faute la plus coûteuse : une doctrine récitée est indistinguable d'une doctrine inventée, et le nom d'un auteur québécois accolé à une proposition fausse fait plus de dégâts qu'un silence. Sans l'outil, l'analyse s'appuie sur le texte de loi et la jurisprudence vérifiée, et le dit.

**Le reste de ce fichier vaut quand même**, et pour une raison qui n'a rien à voir avec le web : la section 3 est un contrôle sur **votre propre mémoire**. Votre connaissance du droit civil est massivement contaminée par la France, dont le vocabulaire est presque identique et le droit ne l'est pas. Le test du numéro d'article s'applique à ce que vous croyez savoir, pas seulement à ce que vous lisez.

## 1. Pourquoi la doctrine porte ici une charge inhabituelle

Le texte des jugements étant inaccessible, elle devient le principal moyen de savoir ce qu'une décision a réellement jugé — et le principal moyen de repérer les décisions à vérifier.

Cela impose deux disciplines opposées : chercher étroitement, dans des sources identifiées; et ne jamais confondre ce qu'un auteur dit d'un arrêt avec ce que l'arrêt dit.

**Chercher étroitement.** Ne pas lancer de recherche web généraliste sur une notion juridique. Le bruit est massif, et il est majoritairement français. Formuler les requêtes en nommant la source ou en restreignant au domaine, et croiser toujours avec un marqueur québécois — « Québec », « C.c.Q. », « Cour d'appel du Québec », le numéro d'article québécois.

Deux ou trois recherches ciblées valent mieux que dix larges.

## 2. La liste blanche

Les adresses changent; l'institution demeure. Vérifier que le site atteint est bien québécois avant de s'y fier.

### Officiel et institutionnel

| Source | Usage |
|---|---|
| LégisQuébec | Vérifier la version en vigueur quand la consolidation de l'outil de législation est en retard |
| Publications du Québec | Textes officiels, *Gazette officielle* |
| Barreau du Québec | Prises de position, formations, *Développements récents* |
| Chambre des notaires | Doctrine notariale |
| Ministère de la Justice du Québec | Commentaires du ministre, notes explicatives des réformes |
| Tribunaux du Québec | Directives, avis, règlements de procédure |
| Éducaloi | Vulgarisation fiable — orientation seulement, jamais citée en note |

### Doctrine savante

| Source | Usage |
|---|---|
| Érudit | Accès aux revues : *Les Cahiers de droit*, *Revue juridique Thémis*, *Revue du notariat*, *Revue générale de droit*. Moissonnable par OAI-PMH |
| Revue du Barreau | Doctrine praticienne de référence, en PDF libres |
| *McGill Law Journal* | Libre accès immédiat |
| Dépôts universitaires (Papyrus, Corpus UL, eScholarship McGill) | Thèses et mémoires, souvent les seules synthèses sur les questions pointues |
| CanLII Connects | Commentaires d'arrêts, de qualité inégale mais parfois le seul commentaire existant |

**Ce qui est réellement atteignable, et quand.** Le libre accès des revues savantes québécoises fonctionne à barrière mobile : les *Cahiers de droit*, la *Revue juridique Thémis* et la *Revue générale de droit* s'ouvrent après un embargo d'environ douze mois. La doctrine des douze derniers mois est donc largement hors d'atteinte, et c'est précisément celle qui commente les développements récents. En conséquence : ne jamais conclure qu'une question n'a pas été traitée en doctrine du seul fait qu'on n'a rien trouvé; dire que la fenêtre récente n'est pas couverte, et renvoyer la vérification au CAIJ.

### Doctrine praticienne et bulletins de cabinets

Utiles pour l'actualité et pour repérer les arrêts récents. Ils sont **secondaires** : on les cite comme bulletin, jamais comme autorité.

Langlois, Lavery, Fasken, BLG, McCarthy Tétrault, Norton Rose Fulbright Canada, Stikeman Elliott, Osler, Gowling WLG, BCF, Therrien Couture Joli-Cœur, Cain Lamarre, Dunton Rainville, De Grandpré Chait, Miller Thomson, Robic. Les agrégateurs Lexology et Mondaq reprennent la plupart de ces bulletins et permettent de les balayer d'un coup.

Le blogue de SOQUIJ et celui du CRL (Barreau de Longueuil) commentent régulièrement la jurisprudence civile québécoise.

### Payant et hors d'atteinte

Ni SOQUIJ ni le CAIJ ne sont accessibles par API. SOQUIJ est désormais le portail unifié, ayant remplacé Azimut et Juris.doc; son blogue demeure librement consultable. Les traités de référence — Baudouin sur la responsabilité, Jobin et Vézina sur la vente, Lluelles et Moore sur les obligations, les *JurisClasseur Québec*, les *Développements récents* du Barreau — ne sont pas consultables.

**Ne jamais citer un traité de mémoire.** Le risque de citation fabriquée y est élevé et particulièrement embarrassant. La conduite correcte est de nommer l'ouvrage et la question dans une rubrique « vérifications doctrinales suggérées », pour consultation au CAIJ.

## 3. Exclure la France, et les autres droits civils étrangers

Le vocabulaire est presque identique et le droit ne l'est pas. Une source française sur le vice caché, la prescription ou la responsabilité se lit comme du droit applicable : c'est précisément ce qui la rend dangereuse. **Et ce que vous croyez savoir de mémoire est exposé au même défaut** — appliquez ce test à vos propres énoncés avant de les écrire.

### Domaines et éditeurs à écarter

Tout domaine `.fr`. Légifrance, Dalloz, Village de la Justice, doctrine.fr, actu-juridique, LexisNexis France, Éditions Législatives, service-public.fr, les sites de la Cour de cassation et du Conseil d'État. Écarter de même la Belgique, la Suisse et le Luxembourg.

### Le test du numéro d'article

Le plus rapide et le plus fiable. Si la source traite :

| de… | sous l'article… | elle est |
|---|---|---|
| responsabilité extracontractuelle | 1240 ou 1382 | française |
| responsabilité extracontractuelle | 1457 C.c.Q. | québécoise |
| prescription de droit commun | 2224 (cinq ans) | française |
| prescription de droit commun | 2925 C.c.Q. (trois ans) | québécoise |
| vices cachés | 1641 et suivants | française |
| vices cachés | 1726 et suivants C.c.Q. | québécoise |

Un seul marqueur suffit : **la source entière est écartée, pas seulement le passage.** Une source qui se trompe de système ne devient pas fiable sur le reste.

*La compétence de rédaction porte ses propres tables complètes de marqueurs et un lexique de substitution, dans un fichier qui lui appartient. La règle y est différente : en rédaction, un marqueur déclenche une réécriture; ici, il écarte la source. ⚠ Ces tables ne vous sont lisibles que si cette compétence est **cochée sur le tour en cours** — sinon, dites qu'elles sont hors d'atteinte plutôt que d'en inventer le contenu.*

### Autres marqueurs français

Pourvoi en cassation, référé, tribunal judiciaire, tribunal de grande instance, prud'hommes, SARL, assignation, Conseil d'État, ordonnance de non-conciliation.

Attention aux faux amis : *huissier* existe au Québec, et il existe un *JurisClasseur Québec* distinct du JurisClasseur français.

### Le droit canadien de common law

À qualifier, non à écarter. Une discussion ontarienne d'une *limitation period* ne transpose pas au droit québécois de la prescription, et une analyse de *duty of care* n'éclaire pas l'article 1457 C.c.Q. Les arrêts de la Cour suprême et le droit fédéral demeurent pleinement pertinents, y compris lorsqu'ils sont rédigés en anglais.

## 4. Ce que la doctrine autorise, et ce qu'elle n'autorise pas

**Elle autorise** : comprendre la règle; repérer des autorités à vérifier; connaître l'état d'un débat; être citée comme doctrine, avec l'auteur, le titre et la source.

**Elle n'autorise pas** : remplacer le texte de loi; établir qu'une décision existe ou dit ce qu'on lui prête. Toute jurisprudence invoquée par une source doctrinale passe par `jurisprudence_canlii_verify_citations` avant d'être répétée.

Lorsqu'un auteur rapporte le sens d'un arrêt, le livrable l'attribue à l'auteur — « selon X, la Cour aurait retenu que… » — et non à la cour. La différence n'est pas cosmétique : elle signale au lecteur que le ratio n'a pas été lu, et lui permet de décider s'il doit aller voir.
