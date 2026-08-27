# Annexe B — mentions attendues

Ce que chaque nature doit porter. **Attendu** = son absence est un signal. **Possible** = son absence n'est jamais signalée. La colonne « Source » distingue une exigence de TEXTE d'un usage constant : l'absence d'une mention seulement usuelle ne doit pas peser sur ta confiance comme celle d'une mention légalement obligatoire.

> Table GÉNÉRÉE depuis `athena/utils/analyse_taxonomies.py`, qui fait foi.
> Ne pas éditer ce fichier à la main : régénère-le (`python -m scripts.exporter_competence_analyse`).

| Sous-nature | Attendu | Possible | Source |
|---|---|---|---|
| `PROC_DEM_INTRO` | numéro de dossier de cour †, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 100, 107 C.p.c. |
| `PROC_AVIS_ASSIGN` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 145 C.p.c. |
| `PROC_DEM_INSTANCE` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 101 C.p.c. |
| `PROC_PROTOCOLE` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 148 C.p.c. |
| `PROC_MOYEN_PRELIM` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 167 C.p.c. |
| `PROC_DEFENSE` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 170 C.p.c. |
| `PROC_EXPOSE_SOMMAIRE` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 148 al. 2 (5°), 170 al. 2 C.p.c. |
| `PROC_DEM_RECONV` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 172 C.p.c. |
| `PROC_INTERVENTION` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 184 C.p.c. |
| `PROC_DECL_SERMENT` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 99, 105 C.p.c. |
| `PROC_DEM_INSCRIPTION` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 173 C.p.c. |
| `PROC_DECL_APPEL` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 358 C.p.c. |
| `PROC_AUTRE` | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document, auteur / signataire | — | art. 99 C.p.c. |
| `JUG_JUGEMENT` | numéro de dossier de cour, tribunal, noms des parties, date du document, auteur / signataire | district judiciaire | usage constant (non ancré) |
| `JUG_ARRET` | dispositif, auteur / signataire | numéro de dossier de cour, tribunal, district judiciaire, noms des parties, date du document | art. 389 C.p.c. |
| `JUG_ORDONNANCE` | numéro de dossier de cour, tribunal, noms des parties, date du document, auteur / signataire | district judiciaire | usage constant (non ancré) |
| `PV_SIGNIFICATION` | numéro de dossier de cour, noms des parties, date du document, auteur / signataire | tribunal | art. 119 C.p.c. |
| `PV_SIGNIFICATION_DESIGNEE` | noms des parties, date du document, auteur / signataire | numéro de dossier de cour, tribunal | art. 120 C.p.c. |
| `PV_AUDIENCE` | numéro de dossier de cour, tribunal, noms des parties, date du document, auteur / signataire | district judiciaire | usage constant (non ancré) |
| `PV_AUDIENCE_JUGEMENT` | numéro de dossier de cour, tribunal, noms des parties, date du document, auteur / signataire, dispositif | district judiciaire | usage constant (non ancré) |
| `TRANS_INTERROGATOIRE` | numéro de dossier de cour, tribunal, noms des parties, date du document | district judiciaire, auteur / signataire | usage constant (non ancré) |
| `TRANS_AUDIENCE` | numéro de dossier de cour, tribunal, noms des parties, date du document | district judiciaire, auteur / signataire | usage constant (non ancré) |
| `CORR_MISE_DEMEURE` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_CONFRERE` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_CLIENT` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_TRIBUNAL` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_EXPERT` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_TIERS` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `CORR_AUTRE` | date du document, auteur / signataire | numéro de dossier de cour, noms des parties | usage constant (non ancré) |
| `PIECE_COMMUNIQUEE` | — | date du document, auteur / signataire | — |
| `PREUVE_CONTRAT` | — | date du document, auteur / signataire | — |
| `PREUVE_COURRIEL` | — | date du document, auteur / signataire | — |
| `PREUVE_FACTURE` | — | date du document, auteur / signataire | — |
| `PREUVE_RELEVE` | — | date du document, auteur / signataire | — |
| `PREUVE_PHOTO` | — | date du document, auteur / signataire | — |
| `PREUVE_RAPPORT_EXPERT` | — | date du document, auteur / signataire | — |
| `PREUVE_AUTRE` | — | date du document, auteur / signataire | — |
| `CAB_MANDAT` | date du document | — | usage constant (non ancré) |
| `CAB_FACTURE` | date du document | — | usage constant (non ancré) |
| `CAB_DEBOURSE` | date du document | — | usage constant (non ancré) |
| `CAB_MEMO` | date du document | — | usage constant (non ancré) |
| `NON_DETERMINE` | — | — | — |

† Attendu, mais son absence est **normale** et ne doit pas peser sur ta confiance — voir « Trois cas » plus bas.

## Mentions exigées par le texte

**`PROC_DECL_SERMENT` — art. 99, 105 C.p.c.**

- le jour et le lieu du serment
- les nom et adresse de celui qui le prête
- les nom et qualité de celui qui le reçoit

**`JUG_ARRET` — art. 389 C.p.c.**

- le dispositif
- le nom des juges ayant entendu l'appel, avec mention des dissidents

**`PV_SIGNIFICATION` — art. 119 C.p.c.**

- le numéro du dossier du tribunal et le nom des parties
- la nature du document signifié
- le lieu, la date et l'heure
- les nom et, s'il y a lieu, qualité de la personne à qui le document a été remis — ou, le cas échéant, le lieu où il a été laissé
- le refus ou l'échec de la tentative
- l'état des honoraires et frais

**`PV_SIGNIFICATION_DESIGNEE` — art. 120 C.p.c.**

- les nom, qualité et adresse de la personne désignée
- le récépissé du destinataire ou la mention de son refus

## Trois cas où l'absence n'est PAS un défaut

1. **Une demande introductive d'instance sans numéro de dossier de cour.** L'art. 107 C.p.c. veut qu'elle soit déposée au greffe AVANT sa notification, et c'est le greffier qui attribue le numéro : un projet n'en porte donc pas, et c'est l'état normal. Signale-le comme un indice de document **non déposé** (donc non public), jamais comme une lacune.
2. **Un arrêt de la Cour d'appel.** L'art. 389 C.p.c. n'exige que le dispositif et le nom des juges ayant entendu l'appel, avec mention des dissidents. Il n'exige ni numéro, ni tribunal, ni district, ni nom des parties, ni date.
3. **Une pièce, une preuve, une photographie.** Aucune mention judiciaire n'est attendue. C'est le cas le plus important de tous : ne réclame jamais les mentions de l'art. 99 sur un document qui n'est pas un acte de procédure.
