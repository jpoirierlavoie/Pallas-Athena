# Notes, théorie de la cause, documents et gabarits

## N1 Trouver, lire ou modifier une note
{{GEN:charger}}
- **Déclencheur** : « retrouve ma note sur … », « corrige la note ».
- **Appels** : (1) `list_notes(dossier_id, query=…)` ; dossier oublié : `list_notes(scope="cabinet", query=…)` ; (2) `get_note(note_id)`, la seule note retenue ; (3) `update_note(note_id, …, expected_etag)`, ou `append_to_note(note_id, content)` pour ajouter à la fin.
- **Arrêt** : l'aperçu de la rangée suffit parfois.
- **À éviter** : `list_notes` sans portée, qui ne voit que Général — 98 appels, dix fois plus lents qu'avec un `dossier_id`.

## N2 Écrire la théorie de la cause
{{GEN:charger}}
- **Déclencheur** : « verse ce plan dans la théorie », « complète le bloc C ».
- **Appels** : (1) `get_note(note_id=analyse_note_id)` : `structure` et `etag` ; `analyse_note_state` "absent" : `edit_analyse(dossier_id)` seul la crée, une écriture à confirmer ; (2) retirer du texte les titres de niveaux 1 et 2, le découper en corps de blocs ; (3) confirmer, puis `edit_analyse(dossier_id, operations=[…], expected_etag, idempotency_key)`, tous les blocs en un appel.
- **Arrêt** : pas de relecture.
- **À éviter** : `list_notes` pour trouver la théorie : son `analyse_note_id` est dans `get_dossier`.

## Doc1 Ce document est-il déjà versé ?
{{GEN:charger}}
- **Déclencheur** : « as-tu le jugement ? » ; avant tout téléversement.
- **Appels** : (1) `list_documents(dossier_id, category=…)`, ou `query=…` ; dossier inconnu : `list_documents(scope="cabinet", query=…)`.
- **Arrêt** : `query` ne lit que les métadonnées : une absence ne prouve pas que la pièce manque.
- **À éviter** : relire la liste d'un même dossier — 5 relectures de `list_documents` observées.

## Doc2 Lire un document
{{GEN:charger}}
- **Déclencheur** : « que dit la pièce P-3 ».
- **Appels** : (1) `get_document_text(document_id)`, puis `page_range` (`next_page`) seulement si la réponse l'exige.
- **Arrêt** : citer ce que la tâche exige, rien de plus.
- **À éviter** : tout lire pour une date ou un nom.

## Doc3 Qualifier un lot
{{GEN:charger}}
- **Déclencheur** : « qualifie les documents non analysés du 2026-008 ».
- **Appels** : (1) `list_documents(dossier_id, limit=50)`, puis `next_offset`, en gardant `analysee: false` et un `file_type` PDF ou .docx ; (2) une confirmation pour le lot ; (3) par document, `get_document_text` puis `record_document_analysis`, une clé chacun, plusieurs par tour.
- **Arrêt** : rapporter les qualifiés, les illisibles, le reste ; la méthode relève d'analyse-documentaire.
- **À éviter** : réanalyser sans demande : chaque passage ajoute une entrée au journal.

## Doc4 Verser un fichier de l'extérieur
{{GEN:charger}}
- **Déclencheur** : « verse ce PDF au dossier ».
- **Appels** : (1) Doc1 d'abord ; (2) `begin_upload(purpose="document", …)`, PUT des octets à `upload_url` depuis le bac à sable, puis `finalize_upload(ticket_id)`.
- **Arrêt** : sans bac à sable capable du PUT, renvoyer à l'application.
- **À éviter** : un second ticket pour un fichier déjà déposé.

## Doc5 Générer, gabariser, classer
{{GEN:charger}}
- **Déclencheur** : « prépare la lettre sur gabarit », « classe ces pièces ».
- **Appels** : (1) `list_templates(kind="gabarit", query=…)`, puis `list_templates(template_id, dossier_id)` : blocs et champs à fournir ; (2) confirmer, puis `fill_gabarit(template_id, dossier_id, blocs, champs_manuels, idempotency_key)` ; (3) du Markdown sur gabarit d'impression : `create_document(source="markdown", dossier_id, title, markdown)` ; (4) gabariser un document : `preview_templatize(document_id, substitutions)`, puis `create_template(source_document_id, name, category, substitutions)` — `index` compte depuis 0, « n° 1 » depuis 1 ; (5) classer : `move_documents` (50 d'un coup), `manage_folder`, `update_document`.
- **Arrêt** : rapporter le document créé et le dossier de classement que nomme `folder` (d'ordinaire « Interne › Projets »).
- **À éviter** : `list_templates(dossier_id)` sans `template_id`, refusé ; un `update_document` par pièce à ranger.
