# Agenda : tâches, audiences, Bookings, échéances, protocole

## A1 Tâches
{{GEN:charger}}
- **Déclencheur** : « ajoute une tâche », « c'est fait ».
- **Appels** : (1) confirmer, puis `create_task(dossier_id, title, due_date)`, une clé chacune ; (2) fermer : `complete_task(task_id)`, `warnings` relayés.
- **Au besoin** : rouvrir, `reopen_task` ; modifier, `update_task` avec l'`etag` de la rangée ; un doublon à craindre (reprise, « si elle n'y est pas ») : `list_tasks(dossier_id, include_completed=true)`, par dossier seulement.
- **Arrêt** : pas de relecture.
- **À éviter** : `list_tasks(include_completed=true)` sans dossier : les closes les plus anciennes — 109 sur 123, les 14 ouvertes hors page.

## A2 Audiences et rendez-vous
{{GEN:charger}}
- **Déclencheur** : « inscris l'audience du … », « chaque lundi à 9 h ».
- **Appels** : (1) confirmer, puis `create_hearing(dossier_id, title, date, start_time, hearing_type)`, avec `status="confirmée"` si la date est certaine ; (2) récurrent : `create_hearing_series`.
- **Au besoin** : modifier, `list_hearings(dossier_id)` (`date_from` pour le passé), puis `update_hearing(hearing_id, …, expected_etag)`.
- **Arrêt** : pas de relecture ; Outlook le reçoit seul : ne pas le créer aussi par Microsoft 365.
- **À éviter** : un `create_hearing` par occurrence d'une série.

## A3 Demandes Bookings
{{GEN:charger}}
- **Déclencheur** : « des rendez-vous à confirmer ? ».
- **Appels** : (1) `list_hearings(bookings="pending")` ; (2) l'accord de l'avocat pour chaque demande, puis `decide_rendez_vous(hearing_id, action, expected_etag, idempotency_key)`.
- **Arrêt** : `action="refuser"` annule la réunion Outlook et avise le client, sans retour : le redire avant ; rapporter `client_notified`.
- **À éviter** : chercher une demande dans la fenêtre de dates, où elle n'est pas.

## A4 Échéances
{{GEN:charger}}
- **Déclencheur** : « signifié le …, 15 jours : inscris l'échéance ».
- **Appels** : (1) un ancrage donné : `compute_judicial_deadline(start_date, delay_days, direction)` d'emblée, en jours, plusieurs par tour ; (2) confirmer, puis `create_task(dossier_id, title, due_date, description)`, la base du calcul dans `description`.
- **Au besoin** : une échéance déjà inscrite se lit (`get_agenda`, `list_protocol_steps(dossier_id)`, `next_deadline_date`), sans recalcul.
- **Arrêt** : rapporter l'échéance et la tâche ; un délai en mois et sa qualification relèvent de calendrier-procedural.
- **À éviter** : recalculer l'agenda — 45 `compute_judicial_deadline`, dont 29 dans les breffages de 06:07.

## A5 Protocole de l'instance
{{GEN:charger}}
- **Déclencheur** : « crée le protocole », « ajoute une étape ».
- **Appels** : (1) `list_protocol_steps(dossier_id)` : le statut dérivé gouverne ; (2) CQ ou CS : UN `create_protocol(dossier_id, protocol_type, start_date, create_linked_tasks=true)`, confirmé (`false` sans tâches) ; (3) étape sur mesure : `add_protocol_step(protocol_id, title, deadline_date, create_linked_task=true)` ; modifier : `update_protocol_step(protocol_id, step_id, …, expected_etag)`.
- **Arrêt** : pas de relecture.
- **À éviter** : un `create_task` par étape ; `list_protocol_steps` par élément du breffage — 18 fois à 06:07.
