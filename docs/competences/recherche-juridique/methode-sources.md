# Méthode : interroger les deux sources officielles

⚠ **Les noms d'outils sont préfixés.** `legislation_qclaw_*` et `jurisprudence_canlii_*`. Un nom sans préfixe n'existe pas et produira un refus.

## Table des matières

1. Législation Québec — le socle
2. Jurisprudence Canada — l'authentification et le graphe
3. La boule de neige, en pratique
4. Reconstituer un ordre chronologique
5. Ce que chaque outil n'établit pas
6. Demander les jugements à l'utilisateur
7. Modes d'échec et conduite à tenir

## 1. Législation Québec — le socle

C'est le seul des deux outils qui rende du texte et qui se cherche par le contenu. Il porte donc l'essentiel du poids probatoire du livrable.

**Entrer dans le corpus.** Quand la loi applicable est inconnue, `legislation_qclaw_find_relevant` avec une description du problème en langue courante (« vice caché maison », « congédiement », « bail commercial »). C'est un classement heuristique : il propose des candidats, il ne détermine pas le droit applicable. Enchaîner systématiquement avec la lecture du texte.

**Se situer.** `legislation_qclaw_get_structure` donne l'arbre des divisions sans le texte — c'est l'outil d'exploration à privilégier avant d'extraire, parce qu'il coûte peu en contexte. `legislation_qclaw_get_division` descend ensuite dans une division précise, avec `include_text=false` quand seuls les numéros d'articles importent.

**Extraire.** `legislation_qclaw_get_article` pour un article, `legislation_qclaw_get_articles` pour une plage ou une liste. Préférer une liste explicite à une plage large : on ne charge que ce qu'on va lire.

**Chercher par le contenu.** `legislation_qclaw_search_text` fait de la recherche plein texte sur les articles. Ne pas restreindre à une loi sans raison — une recherche restreinte qui échoue est élargie automatiquement, mais une recherche mal restreinte donne un faux négatif silencieux.

**Résoudre une citation.** `legislation_qclaw_resolve_reference` accepte les formes libres (« art. 1457 C.c.Q. », « RLRQ, c. T-16, art. 12 »).

**Cartographier.** `legislation_qclaw_list_laws` donne toutes les lois du corpus avec leur identifiant, leurs intitulés français et anglais et leur citation RLRQ — c'est le moyen de savoir si une loi est couverte avant de conclure qu'elle n'existe pas. `legislation_qclaw_list_subjects` liste les matières de la taxonomie, utile pour cadrer une question mal définie.

**Dater.** `legislation_qclaw_get_article` retourne une date de consolidation. **La relever et la reporter dans le livrable.** Pour toute disposition récemment modifiée ou sensible, confirmer à LégisQuébec, car le corpus accuse un retard de consolidation.

Vérifier aussi quelle version s'applique : celle d'aujourd'hui pour la procédure, celle du fait générateur pour le droit substantiel. Ce n'est pas la même question et la réponse diffère souvent.

**Le graphe législatif.** `legislation_qclaw_related_laws` donne les règlements pris sous l'autorité d'une loi, la loi habilitante et les renvois. Utile pour ne pas manquer un règlement d'application, et il signale les cibles absentes du corpus.

## 2. Jurisprudence Canada — l'authentification et le graphe

Cet outil n'est **pas** un moteur de recherche jurisprudentielle. Il ne rend aucun texte de décision et ne permet aucune recherche par mots du texte. Il fait quatre choses.

**Analyser une citation sans appeler CanLII.** `jurisprudence_canlii_parse_citation` dit si la forme est reconnue et constructible. À employer avant une vérification en lot pour écarter d'emblée les formes qui ne se résoudront pas — cela évite de gaspiller des places dans le lot.

**Authentifier.** `jurisprudence_canlii_verify_citations` accepte jusqu'à vingt-cinq citations par appel, avec un intitulé et une année attendus facultatifs. Verdicts : CONFIRMÉE, DISCORDANTE, INTROUVABLE, NON CONSTRUCTIBLE, ILLISIBLE. **Grouper les vérifications** : un lot de vingt-cinq coûte le même aller-retour qu'une citation isolée, et la vérification en lot est ce qui rend praticable la méthode de la boule de neige.

- **DISCORDANTE** est le verdict le plus instructif : la citation existe mais désigne une autre décision que celle annoncée. Signature d'une référence fabriquée ou recopiée sans contrôle. Le signaler et écarter la source qui l'a produite.
- **NON CONSTRUCTIBLE** vise les citations de recueils (R.C.S., R.J.Q., C.A.) et les identifiants d'éditeurs (J.E., REJB, EYB, AZ), qui ne se résolvent pas directement. Enchaîner avec `jurisprudence_canlii_find_case` sur le nom des parties.
- **INTROUVABLE** n'établit pas l'inexistence : la couverture a des bornes historiques et la diffusion connaît un délai.

**Décrire.** `jurisprudence_canlii_get_case` rend la fiche officielle — intitulé, citation, date, numéro de dossier de cour, mots-clés, hyperlien. Le champ des mots-clés est un digest indexé, souvent riche : il nomme les articles en jeu et les arrêts de principe discutés. C'est une source de candidats à part entière, et le meilleur substitut disponible au texte.

**Retrouver.** `jurisprudence_canlii_find_case` cherche sur l'intitulé et les mots-clés, avec tribunal et bornes d'années facultatifs. À utiliser quand on connaît les parties et l'année, ou quand la citation n'est pas constructible. **Ne pas compter dessus pour du repérage par sujet** : il n'y a pas de recherche par mots du texte, et le balayage en direct est faillible.

**Parcourir le graphe.** `jurisprudence_canlii_citator` avec `rel='citing'` (ce qui cite la décision), `rel='cited'` (ce qu'elle cite) ou `rel='legislation'` (les dispositions qu'elle cite). Les listes sont brutes : **aucun sens de traitement** n'y figure — ni suivi, ni distingué, ni infirmé.

**Une lacune à connaître** : le graphe va de la décision vers les dispositions, jamais l'inverse. On ne peut pas demander quelles décisions citent un article donné, ce qui ferme la porte d'entrée la plus naturelle en méthode civiliste.

**Veiller.** `jurisprudence_canlii_browse_cases` liste les décisions d'un tribunal avec filtres de date. Trois dates distinctes s'y trouvent : la date de la décision, la date de diffusion sur CanLII et la date de dernière modification. Pour la veille, c'est la diffusion qui compte; pour la recherche, c'est la date du jugement. L'écart peut atteindre plusieurs mois.

**Cadrer.** `jurisprudence_canlii_list_databases` recense les bases. La couverture québécoise est large — Cour d'appel, Cour supérieure, Cour du Québec, Tribunal administratif du logement, TAQ, TAT, Tribunal des droits de la personne, Tribunal des professions, arbitrages, conseils de discipline. Les identifiants les plus employés : `csc-scc` (Cour suprême), `csc-scc-al` (demandes d'autorisation d'appel), `qcca`, `qccs`.

**Situer un dossier de cour.** `jurisprudence_greffe_parse_court_file_number` tire d'un numéro québécois le greffe — palais de justice et district — puis le tribunal et le type de greffe. `jurisprudence_palais_list` et `jurisprudence_palais_get` renseignent sur les palais de justice et points de service. Ces outils lisent des données de référence locales : ils n'établissent **pas** qu'un dossier existe ou qu'il est actif, et leurs adresses vieillissent. L'application porte son propre `parse_court_file_number`, sans préfixe, au même usage; employer celui des deux qui est disponible.

**Législation.** `jurisprudence_canlii_browse_legislation` et `jurisprudence_canlii_get_legislation` existent, mais pour le texte d'une loi québécoise c'est `legislation_qclaw_*` qu'il faut employer : lui seul rend le texte officiel verbatim. Réserver les outils CanLII à la datation et à la vérification d'abrogation.

## 3. La boule de neige, en pratique

Faute de recherche par sujet, le corpus se construit par expansion depuis des ancrages. C'est la méthode classique d'avant le plein texte, et elle fonctionne bien.

1. **Obtenir un premier ancrage** hors outil de jurisprudence : doctrine, note au dossier, mémoire de l'utilisateur, arrêt de principe connu de la matière.
2. **L'authentifier** par `jurisprudence_canlii_verify_citations`.
3. **Lire sa fiche** par `jurisprudence_canlii_get_case` et récolter les autorités nommées dans les mots-clés.
4. **Authentifier cette récolte**, en une seule passe groupée.
5. **Étendre** par `jurisprudence_canlii_citator` — `cited` pour remonter vers les arrêts de principe, `citing` pour redescendre vers les applications récentes.
6. **Arrêter tôt.** Quand deux ou trois itérations ramènent les mêmes noms, le corpus est stabilisé. Continuer ne fait qu'allonger la liste.

## 4. Reconstituer un ordre chronologique

`jurisprudence_canlii_browse_cases` trie par **date de diffusion sur CanLII**, non par date de jugement. Les deux divergent, parfois de plusieurs mois, et le retard de diffusion de la Cour supérieure peut dépasser deux ans. Les numéros de citation neutre ne sont pas non plus un substitut fiable : ils suivent l'ordre de diffusion, pas celui des jugements.

Quand l'ordre chronologique réel importe — pour situer un revirement, pour savoir laquelle de deux décisions est postérieure —, procéder en deux temps : parcourir pour récolter les numéros de citation, puis vérifier en lots de vingt-cinq avec `jurisprudence_canlii_verify_citations`, dont la fiche porte la date du jugement. Reconstituer l'ordre à partir de ces dates, jamais des numéros.

Pour la veille, c'est l'inverse : c'est la diffusion qui compte, puisque c'est elle qui décide de ce qui est nouveau depuis hier.

## 5. Ce que chaque outil n'établit pas

| Outil | N'établit pas |
|---|---|
| `jurisprudence_canlii_verify_citations` | L'autorité actuelle, le contenu du dispositif |
| `jurisprudence_canlii_get_case` | Le texte, le ratio, la portée réelle du résumé |
| `jurisprudence_canlii_citator` | Le sens du traitement — suivi, distingué, infirmé |
| `jurisprudence_canlii_subsequent_history` | L'infirmation, les pourvois pendants, les refus de permission d'appeler, les désistements |
| `legislation_qclaw_get_article` | La version en vigueur postérieure à la consolidation |
| `legislation_qclaw_find_relevant` | Le droit applicable — c'est un classement heuristique |

`jurisprudence_canlii_subsequent_history` mérite une mention particulière : il repère, parmi les décisions citantes, celles d'une juridiction supérieure dont l'intitulé ressemble. C'est un indice utile et rien de plus. **Ne jamais le présenter comme une vérification d'historique d'appel.** En l'absence de citateur professionnel, l'état d'une décision reste à confirmer à la source, et le livrable doit le dire.

## 6. Demander les jugements à l'utilisateur

C'est la seconde route vers le statut « Lue », après le document déjà versé au dossier, et elle est explicitement ouverte : l'utilisateur téléverse les décisions qu'on lui demande.

Demander de façon directe et parcimonieuse. Nommer chaque décision par sa citation vérifiée, en dire une phrase, et préciser ce qu'on y cherche. Demander une à trois décisions, pas dix. Le faire au moment où le besoin apparaît, sans attendre la fin de la recherche.

> Trois décisions changeraient la réponse. Pourriez-vous téléverser 2026 QCCA 412 — j'y cherche l'énoncé du critère — et 2025 QCCS 3187, pour voir comment le tribunal a traité le point de départ du délai sur des faits voisins ?

Avant de demander, vérifier avec `list_documents` si la décision n'est pas déjà au dossier. Si elle y est, la lire avec `get_document_text` plutôt que de la redemander. Réserve : un document numérisé n'a pas de couche de texte et ne rend rien — l'outil le signale par `pages_without_text`, et il faut alors demander une version lisible. Un retour vide ne veut jamais dire une page blanche sur papier.

Si l'utilisateur ne peut pas fournir le texte, le dire dans le livrable : la question reste ouverte au niveau du ratio, et l'analyse s'appuie alors sur la législation et la doctrine.

## 7. Modes d'échec et conduite à tenir

**Un outil ne répond pas.** Le dire et s'arrêter sur ce volet. Ne pas basculer silencieusement sur le web pour combler.

**Un outil n'est pas dans votre tableau.** Ce n'est pas une panne : les Workers non configurés sont simplement absents, et la recherche web dépend du modèle du tour. Le constater, le dire, et dégrader honnêtement — jamais faire semblant de l'avoir appelé.

**Une recherche ne donne rien.** L'absence de résultat est un résultat, et il se rapporte : quelles bases, quels termes, quelles bornes de dates. Ne pas conclure à l'inexistence, ne pas combler par la mémoire.

**Une citation ne se vérifie pas.** Elle ne sert pas. Elle figure au registre des sources avec son verdict, dans la rubrique des vérifications à faire.

**Une source web contredit l'outil de source.** L'outil l'emporte, sans discussion. Signaler la contradiction, parce qu'elle révèle souvent une source périmée ou étrangère.

**Le texte d'une décision manque et il est déterminant.** Vérifier d'abord avec `list_documents` si elle n'est pas déjà versée au dossier : si elle y est, `get_document_text` en rend la couche de texte et la décision passe au statut « Lue ». Sinon, demander le téléversement à l'utilisateur (section 6). À défaut, laisser la question ouverte au niveau du ratio et le dire.

**Tentation de contourner l'absence de texte.** Ne pas reconstituer un jugement par accumulation de commentaires doctrinaux, et ne pas recopier du texte de décision dans une note. L'usage autorisé de l'API porte sur les citations, les fiches et les hyperliens; la note conserve le lien, non le corpus.
