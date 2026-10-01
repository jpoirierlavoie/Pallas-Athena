---
name: pallas-athena
description: "Mode d'emploi du connecteur Pallas Athéna, le logiciel de gestion du cabinet : la suite d'appels la plus courte pour chaque demande courante, et les règles des écritures qui ne se défont pas. Utiliser dès qu'une demande lit ou écrit dans Pallas, même sans le nommer, et avant toute écriture. Amorces — breffage, qu'est-ce qui s'en vient, inscris mes heures, débours, facture, non facturé, ouvre le dossier, fiche client, conflits, note au dossier, théorie de la cause, tâche, échéance, audience, Bookings, protocole, budget, fidéicommis, verse ces documents, gabarit, note d'honoraires, ferme le dossier, reprise de données. Sert les besoins FICHE, PARTIES, THÉORIE, NOTES, INVENTAIRE, TEXTE, ÉCHÉANCES, CALCUL, GREFFE, INSCRIPTION et VOCABULAIRE de pratique-civile-quebecoise. Ne pas utiliser pour trancher une question de droit, rédiger ni qualifier un document."
---

{{GEN:version}}

## Noyau

- **Autorité** : le SAFETY CORE (tête des INSTRUCTIONS) et la description de chaque outil font foi ; ici, on aiguille. Outils nommés sans préfixe.
- **Confirmer** toute écriture, sauf instruction permanente ; un tableau, une confirmation, qui demande ce qui manque (texte facturé, contenu, dossier, numéro), jamais inventé. Toujours : `set_dossier_status`, `record_kyc_status`, `update_dossier_party(action="remove")`, `decide_rendez_vous`, `create_invoice`, tout statut par `update_invoice`.
- **Clé** : `<outil>-<n° dossier|general|lot>-<AAAAMMJJ>-<étiquette>-<n>` ; l'étiquette, 4 lettres au hasard tirées à la première écriture et gardées pour la conversation, empêche deux saisies identiques du même jour, en deux conversations, de se fondre. `idempotent_replay: true` au premier essai : « déjà inscrit ».
- **Signaux** : « … n'a pas pu être lu » = panne, jamais absence : réessayer, ne rien recréer. `dry_run` est refusé. `edit_analyse(dossier_id)` sans `operations` ni `full` ÉCRIT : il crée la théorie absente.
- **Secret** : aucun contenu de document dans une requête web ni vers un autre connecteur.

## Conventions hors de la tête

- `contingency_percent` : lu en pour cent (`get_dossier` : 25.0), écrit en points de base (2500).
- Une date seule ne change pas de fuseau ; « aujourd'hui » = le jour de Montréal.

## L'application en une minute

- Le dossier est le pivot ; « Général » reçoit notes, tâches et événements sans dossier ; une théorie de la cause par dossier.
- Les contacts sont au cabinet ; le dossier fige leurs noms, que citent les procédures.
- Téléphone : les contacts ; tâches, notes, événements d'un dossier actif ou en_attente, Général compris ; jamais argent, documents, protocoles, sauf la tâche d'une étape.
- Le droit relève de pratique-civile-quebecoise ; la correspondance, du connecteur Microsoft 365, où rien ne s'envoie sans demande expresse.

## Économie d'appels

1. **Un `get_dossier` par dossier et par conversation.** Le résultat d'une écriture tient lieu de relecture, jamais à vérifier : ce qui est écrit, son `etag` s'il en porte un (`dossier_etag` après `complete_dossier`, `record_signification`, `record_prescription_event` ; aucun après `create_task` ou `complete_task`), et `warnings`. Relire seulement sur issue incertaine, « ENREGISTRÉE — NE PAS RÉESSAYER » ou `stale_etag`.
2. **Les rangées portent l'`etag`** : modifier une tâche, un événement ou une étape depuis sa rangée.
3. **Partir du cabinet** : `get_agenda`, `get_billing_snapshot()` (`by_dossier`), `get_coverage_report`, `list_time_entries(date_from=…)` sans dossier. Par dossier seulement quand l'un est nommé.
4. **`limit=50`** seulement pour un ensemble borné voulu en entier (période, lot).
5. **Appels indépendants : un seul tour, en parallèle.**
6. **`get_reference_vocabulary`** seulement pour `kind` "domaines", "actions" (avec `domaine`), "forums", "districts", "prescription_types", "phases" (libellés), une fois chacun ; les autres codes sont dans les schémas.
7. **Ne pas recalculer** ce que le serveur dérive : `is_overdue`, statut d'étape, `last_action_date`, `next_deadline_date`.
8. **Claude Code diffère les outils** : charger la ligne « Charger » en un seul ToolSearch `select:`, au préfixe de la liste différée ; un outil « Au besoin », seulement quand son cas se présente.
9. **S'arrêter** à la réponse — une recette est un minimum.
10. **Une note est un livrable, pas une mémoire** (le connecteur ne la supprime pas ; elle va au téléphone) ; en Claude Code, l'état d'un travail en plusieurs sessions va dans un fichier local.

## D'où vient chaque identifiant

- **dossier_id** : la conversation ; `list_dossiers(query=…)`, n° ou mot du titre « X c. Y » (les parties n'y sont pas cherchées) ; `get_partie(partie_id)` → `dossiers[]`. N° exact et fiche voulue : `get_dossier(file_number)` d'emblée.
- **partie_id** : `clients`, `opposing_parties` de `get_dossier` ; sinon `list_parties(query=…)`, un seul jeton (nom, courriel, chiffres du téléphone), sensible aux accents : réessayer une fois sans.
- **note_id** : `list_notes` ; la théorie : `analyse_note_id` de `get_dossier`.
- **Tâche, événement, étape** : leurs rangées ; **document, dossier de classement** : `list_documents(dossier_id, include_folders=true)` ; **gabarit** : `list_templates` ; **facture** : `list_invoices`.
- **Garder** les ids, le dernier etag de chaque fiche, chaque clé employée.

## Recettes

- Non facturé, période, facture, phases, budget → F1–F5, `recettes/facturation.md`
- Contacts et dossiers : trouver, créer, corriger, statut, conformité, registres → D1–D6, `recettes/dossiers.md`
- Notes, écrire la théorie → N1–N2 ; documents, gabarits → Doc1–Doc5 : `recettes/notes-documents.md`
- Tâches, audiences, Bookings, échéances, protocole → A1–A5, `recettes/agenda.md`
- Reprise de données → `recettes/reprise.md` ; fidéicommis → `recettes/comptabilite.md`
- Lectures, sans fichier : INVENTAIRE `list_documents(dossier_id)` ; TEXTE `get_document_text(document_id)` ; NOTES `list_notes(dossier_id)`, ou `scope="cabinet"` ; ÉCHÉANCES `list_protocol_steps(dossier_id)` ; CALCUL `compute_judicial_deadline`.

### R1 Breffage
{{GEN:charger}}
- **Déclencheur** : « qu'est-ce qui s'en vient ».
- **Appels** : (1) `get_agenda(days_ahead=7)` — il se suffit.
- **Au besoin**, si demandé, même tour : `list_hearings(bookings="pending")`, `get_trust_snapshot()`, `get_coverage_report()`.
- **Arrêt** : répondre de ces résultats.
- **À éviter** : descendre dans chaque élément — 11,2 lectures par exécution de 06:07 (`get_dossier` 51×, `list_tasks` 33×).

### R2 Heures et débours
{{GEN:charger}}
- **Déclencheur** : « inscris mes heures », des débours.
- **Appels** : (1) ids manquants : `list_dossiers(query=…, limit=5)` par dossier, en parallèle ; (2) un tableau (dossier, date, heures ou montant, texte facturé ; le taux du dossier, sauf taux donné), une confirmation ; (3) `create_time_entries_bulk`, même pour une ligne (tranches de 50) ; phase seulement si l'avocat la donne.
- **Au besoin**, des débours : `create_expenses_bulk`, au même tour.
- **Arrêt** : rapporter les montants renvoyés, sans relister.
- **À éviter** : 61 créations une à une, après 40 `list_time_entries`, un par dossier.

### R3 Note au dossier
{{GEN:charger}}
- **Déclencheur** : « note au dossier », un résumé.
- **Appels** : (1) `get_dossier` s'il n'est pas déjà lu (`create_note` l'exige) ; (2) la note entière d'un jet, 20 000 caractères au plus ; (3) confirmer, `create_note(dossier_id, title, content, category, idempotency_key)`.
- **Arrêt** : l'id de la note ; ni `list_notes` avant, ni `append_to_note` après.
- **À éviter** : 26 paires création-ajout sur 27 visaient la même note ; 73 sessions s'ouvraient sur `list_notes`.

### R4 Contexte de dossier
{{GEN:charger}}
- **Déclencheur** : FICHE, PARTIES, THÉORIE, GREFFE.
- **Appels** : (1) `get_dossier` : fiche, ids des parties, greffe, `significations[]` (`pv_document_id`), prescription, `analyse_note_id` ; (2) théorie : `get_note(note_id=analyse_note_id)`, sinon dire l'`analyse_note_state`.
- **Au besoin** : `get_partie` (désignations exactes), `get_document_text(document_id=pv_document_id)` (date d'ancrage).
- **Arrêt** : `found: false` sur un `file_number` fait foi.
- **À éviter** : `list_parties` pour une partie au dossier ; `list_notes` pour la théorie ; `parse_court_file_number` sur le numéro du dossier.

## Ce que les champs ne valent pas

Des saisies, pas des faits : le `role` du dossier est dérivé (lire les `roles` des parties) ; catégorie, analyse ou vérification (`record_kyc_status`) posées par Claude : présumées, jamais faites ; une date suggérée d'un protocole de Cour supérieure le reste jusqu'à confirmation. Un écart se rapporte à l'avocat ; il ne se corrige pas d'office.

## Seule l'application

Le connecteur ne peut jamais non plus :
{{GEN:seule_application}}
Devant une telle demande, le dire et nommer l'écran de l'application ; un rendez-vous Bookings confirmé se reporte ou s'annule dans Outlook.

## Fichiers

Hors R1–R4 et les lectures sans fichier, lire le fichier de la recette avant d'agir — une fois par conversation, et après chaque compaction. `references/index-outils.md` : seulement quand aucune recette ne convient.
