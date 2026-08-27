# Annexe D — régimes de protection

**Le champ le plus dangereux du système.** La liste est CUMULABLE : les régimes se superposent réellement (un mémorandum au client préparant l'instruction est à la fois couvert par le secret professionnel et par le privilège relatif au litige). C'est le niveau le plus élevé qui gouverne.

> Table GÉNÉRÉE depuis `athena/utils/analyse_taxonomies.py`, qui fait foi.
> Ne pas éditer ce fichier à la main : régénère-le (`python -m scripts.exporter_competence_analyse`).

| Code | Niveau | Portée | Fondement | Nature |
|---|:--:|---|---|---|
| `SECRET_PROFESSIONNEL` | 3 | Communication avec le client | art. 9 Charte des droits et libertés de la personne (c-12) ; art. 60.4 Code des professions (c-26) ; art. 2858 al. 2 C.c.Q. | statutaire |
| `LITIGE` | 2 | Communication avec un expert ou un collaborateur, travail préparatoire | Lizotte c. Aviva, 2016 CSC 52 ; Blank c. Canada, 2006 CSC 39 | jurisprudentiel |
| `REGLEMENT` | 2 | Offres et pourparlers transactionnels | art. 4, 606 C.p.c. ; Union Carbide c. Bombardier, 2014 CSC 35 | mixte |
| `ENQUETE_INTERNE` | 2 | Documents internes constitués en vue du litige | — | jurisprudentiel |
| `SECRET_COMMERCIAL` | 1 | Données industrielles et commerciales | art. 1472, 1612 C.c.Q. | statutaire |
| `CONFIDENTIEL` | 1 | Correspondance externe ; défaut résiduel | — | residuel |
| `PUBLIC` | 0 | Acte de procédure déposé, pièce cotée | art. 11 C.p.c. | statutaire |

## Réserves — à énoncer, jamais à taire

**`LITIGE`** — Fondement jurisprudentiel : l'existence de ces arrêts est confirmée, leur autorité actuelle ne l'est pas. À vérifier avant toute utilisation en argumentation.

**`ENQUETE_INTERNE`** — Le plus souvent une espèce du privilège relatif au litige, conservée pour sa commodité pratique — sans prétendre à l'autonomie.

**`SECRET_COMMERCIAL`** — N'est PAS un privilège de non-divulgation : aucune immunité de production n'en découle. En instance, la protection passe par une ordonnance rendue sous l'art. 12 C.p.c.

**`PUBLIC`** — L'art. 11 al. 2 réserve les cas où la loi restreint l'accès, et une ordonnance de confidentialité (art. 12 C.p.c.) est invisible depuis le document : l'étiquette n'est jamais exhaustive.

## Implications automatiques

- `ENQUETE_INTERNE` entraîne `LITIGE`.

## Pourquoi le secret professionnel est seul au niveau 3

Ce n'est pas une hiérarchie de confort. L'art. 9 al. 3 de la Charte impose au tribunal d'assurer **d'office** le respect du secret professionnel. Et l'art. 2858 C.c.Q. commande le rejet de la preuve obtenue en violation des droits fondamentaux lorsque son utilisation est susceptible de déconsidérer l'administration de la justice — mais son alinéa 2 écarte ce second critère pour le secret professionnel. Le rejet y est donc **automatique**, là où il reste conditionnel ailleurs. Aucun autre régime de la liste n'en bénéficie.

## L'accès restreint (art. 16 C.p.c.)

`PUBLIC` n'est jamais automatique. L'art. 11 al. 2 réserve les cas où la loi restreint l'accès, et l'art. 16 le restreint dans **cinq matières** : matière familiale, autorisation pour des soins, aliénation d'une partie du corps, garde en établissement, et changement de la mention du sexe d'un enfant mineur. Un acte de procédure déposé dans l'une d'elles n'est PAS public au sens ordinaire.

Deux indices te le révèlent : le domaine du dossier, et le segment de juridiction du numéro de dossier de cour — `04`, `12`, `13` correspondent à la Chambre familiale de la Cour supérieure (exemple : 500-**12**-123456-241). Le second fonctionne même quand le dossier n'est pas classé, ce qui est fréquent sur les dossiers anciens.

Note aussi que l'art. 16 al. 2 permet quand même l'accès aux parties, à leurs représentants, aux avocats et aux notaires : l'accès restreint n'est pas une interdiction générale. Et son al. 5 ajoute un devoir de **non-diffusion** de toute information permettant d'identifier une partie ou un enfant.

## Deux limites à garder en tête

- Une **ordonnance de confidentialité** rendue sous l'art. 12 C.p.c. est invisible depuis le document. Ni toi ni personne ne peut la deviner : l'étiquette n'est donc jamais exhaustive.
- Les arrêts cités ci-dessus existent, mais **leur autorité actuelle n'a pas été vérifiée**. Ne les présente jamais comme vérifiés, et ne les cite pas dans un document destiné à un client ou à un tribunal : cette table est une aide au triage, pas une source d'argumentation.
