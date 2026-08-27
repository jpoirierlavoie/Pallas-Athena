# Annexe A — natures et sous-natures

Le vocabulaire de classification. La `nature` est la catégorie du document dans l'application ; la `sous_nature` la raffine. **N'invente jamais un code absent de cette table.**

> Table GÉNÉRÉE depuis `athena/utils/analyse_taxonomies.py`, qui fait foi.
> Ne pas éditer ce fichier à la main : régénère-le (`python -m scripts.exporter_competence_analyse`).

## Famille Judiciaire (`JUDICIAIRE`)

| Sous-nature | Libellé | Nature | Ancrage |
|---|---|---|---|
| `PROC_DEM_INTRO` | Demande introductive d'instance | `procédure` | art. 100, 107 C.p.c. |
| `PROC_AVIS_ASSIGN` | Avis d'assignation | `procédure` | art. 145 C.p.c. |
| `PROC_DEM_INSTANCE` | Demande en cours d'instance | `procédure` | art. 101 C.p.c. |
| `PROC_PROTOCOLE` | Protocole de l'instance | `procédure` | art. 148 C.p.c. |
| `PROC_MOYEN_PRELIM` | Moyen préliminaire | `procédure` | art. 167 C.p.c. |
| `PROC_DEFENSE` | Défense écrite | `procédure` | art. 170 C.p.c. |
| `PROC_EXPOSE_SOMMAIRE` | Exposé sommaire des éléments de contestation | `procédure` | art. 148 al. 2 (5°), 170 al. 2 C.p.c. |
| `PROC_DEM_RECONV` | Demande reconventionnelle | `procédure` | art. 172 C.p.c. |
| `PROC_INTERVENTION` | Intervention volontaire ou forcée | `procédure` | art. 184 C.p.c. |
| `PROC_DECL_SERMENT` | Déclaration sous serment | `procédure` | art. 99, 105 C.p.c. |
| `PROC_DEM_INSCRIPTION` | Demande d'inscription pour instruction et jugement | `procédure` | art. 173 C.p.c. |
| `PROC_DECL_APPEL` | Déclaration d'appel | `procédure` | art. 358 C.p.c. |
| `PROC_AUTRE` | Autre acte de procédure | `procédure` | art. 99 C.p.c. |
| `JUG_JUGEMENT` | Jugement de première instance | `jugement` | — |
| `JUG_ARRET` | Arrêt de la Cour d'appel | `jugement` | art. 389 C.p.c. |
| `JUG_ORDONNANCE` | Ordonnance | `jugement` | — |
| `PV_SIGNIFICATION` | Procès-verbal de signification (huissier) | `procès_verbal_signification` | art. 119 C.p.c. |
| `PV_SIGNIFICATION_DESIGNEE` | Procès-verbal de notification (personne désignée) | `procès_verbal_signification` | art. 120 C.p.c. |
| `PV_AUDIENCE` | Procès-verbal d'audience | `procès_verbal_audience` | — |
| `PV_AUDIENCE_JUGEMENT` | Procès-verbal d'audience portant jugement | `procès_verbal_audience` | — |
| `TRANS_INTERROGATOIRE` | Notes sténographiques d'interrogatoire | `transcription` | — |
| `TRANS_AUDIENCE` | Notes sténographiques d'audience | `transcription` | — |

## Famille Correspondance (`CORRESPONDANCE`)

| Sous-nature | Libellé | Nature | Ancrage |
|---|---|---|---|
| `CORR_MISE_DEMEURE` | Mise en demeure | `correspondance` | art. 1595 C.c.Q. |
| `CORR_CONFRERE` | Lettre au confrère | `correspondance` | — |
| `CORR_CLIENT` | Lettre au client | `correspondance` | — |
| `CORR_TRIBUNAL` | Lettre au tribunal ou au greffe | `correspondance` | — |
| `CORR_EXPERT` | Communication avec un expert | `correspondance` | — |
| `CORR_TIERS` | Lettre à un tiers | `correspondance` | — |
| `CORR_AUTRE` | Autre correspondance | `correspondance` | — |

## Famille Preuve (`PREUVE`)

| Sous-nature | Libellé | Nature | Ancrage |
|---|---|---|---|
| `PIECE_COMMUNIQUEE` | Pièce cotée, notifiée et déposée | `pièce` | — |
| `PREUVE_CONTRAT` | Contrat, entente, quittance | `preuve` | — |
| `PREUVE_COURRIEL` | Courriel ou message | `preuve` | — |
| `PREUVE_FACTURE` | Facture d'un tiers | `preuve` | — |
| `PREUVE_RELEVE` | Relevé, état de compte | `preuve` | — |
| `PREUVE_PHOTO` | Photographie | `preuve` | — |
| `PREUVE_RAPPORT_EXPERT` | Rapport d'expertise | `preuve` | — |
| `PREUVE_AUTRE` | Autre élément de preuve | `preuve` | — |

## Famille Cabinet (`CABINET`)

| Sous-nature | Libellé | Nature | Ancrage |
|---|---|---|---|
| `CAB_MANDAT` | Mandat, convention d'honoraires | `mandat` | — |
| `CAB_FACTURE` | Note d'honoraires du cabinet | `facture` | — |
| `CAB_DEBOURSE` | Pièce justificative de déboursé | `déboursé` | — |
| `CAB_MEMO` | Mémorandum ou note interne | `autre` | — |

## Famille Indéterminée (`INDETERMINE`)

| Sous-nature | Libellé | Nature | Ancrage |
|---|---|---|---|
| `NON_DETERMINE` | Nature indéterminée | `autre` | — |

## Deux pièges de lecture

- `PV_AUDIENCE` est un préfixe de `PV_AUDIENCE_JUGEMENT`, et `PV_SIGNIFICATION` de `PV_SIGNIFICATION_DESIGNEE` : ce sont **quatre codes distincts**, jamais des variantes.
- La catégorie héritée `procès_verbal` (sans suffixe) existe encore sur d'anciens documents de l'application. **Ne la produis jamais** : choisis `procès_verbal_signification` ou `procès_verbal_audience`, et signale la divergence.
