# Facturation : non facturé, encours, travail d'une période, facture, phases, budget

## F1 Non facturé, encours
{{GEN:charger}}
- **Déclencheur** : « combien de non facturé », « quels dossiers », « qui me doit ».
- **Appels** : (1) le cabinet : `get_billing_snapshot()` — les totaux, `by_dossier` pour « quels dossiers », `outstanding_invoices` pour le détail des factures ; (2) un dossier : `get_billing_snapshot(dossier_id)` pour le non facturé seulement ; ce qu'il doit : `list_invoices(dossier_id, status_group="impayée")`, en additionnant `balance_cents`.
- **Au besoin** : `outstanding_invoices_truncated` vrai : `list_invoices(status_group="impayée")`, toutes les pages ; `by_dossier_truncated` vrai : ventilation partielle, totaux exacts — le dire.
- **Arrêt** : citer `outstanding_display` (cabinet), jamais `total_outstanding` (brouillons compris) ni une somme de `amount_due`, figé à l'émission ; `payment_basis: "none"` : rien d'inscrit, pas rien de payé.
- **À éviter** : `list_time_entries` dossier par dossier — 20 appels, puis les mêmes 20 le lendemain.

## F2 Travail d'une période
{{GEN:charger}}
- **Déclencheur** : « qu'ai-je fait en septembre », « mes heures de la semaine ».
- **Appels** : (1) `list_time_entries(date_from=…, date_to=…, limit=50)` sans `dossier_id` — chaque rangée porte son dossier ; `dossier_id` seulement si un dossier est nommé ; (2) dans le même tour, si les débours sont demandés : `list_expenses(date_from=…, date_to=…, limit=50)` ; (3) `next_cursor` tant que la réponse l'exige.
- **Arrêt** : totaliser les rangées lues ; `truncated: true` = somme partielle, à compléter ou à dire.
- **À éviter** : un appel par dossier — 45 paires consécutives de `list_time_entries`, aucune sur le même dossier.

## F3 Facture
{{GEN:charger}}
- **Déclencheur** : « prépare la facture du 2026-022 », « facture le non facturé ».
- **Appels** : (1) `preview_invoice(dossier_id, all_unbilled=true)`, ou les `time_entry_ids` et `expense_ids` retenus ; `ready: false` : s'arrêter ; (2) montrer lignes, taxes, total, puis confirmer ; (3) `create_invoice`, même sélection, `expected_total_cents` de l'aperçu.
- **Au besoin**, sur demande : `create_document(source="invoice_note", invoice_id)`.
- **Arrêt** : rapporter le numéro ; la facture est un brouillon.
- **À éviter** : `get_dossier` ou `get_billing_snapshot` avant l'aperçu ; `list_invoices` après la création.

## F4 Reclasser les phases
{{GEN:charger}}
- **Déclencheur** : « reclasse les heures sans phase du 2026-003 ».
- **Appels** : (1) `list_time_entries(dossier_id, limit=50)`, puis `next_cursor` jusqu'au bout ; garder les rangées dont `phase` est vide ; (2) les libellés : un `get_reference_vocabulary(kind="phases")` par conversation ; un tableau des codes proposés, puis confirmer ; (3) `set_time_entry_phase_bulk`, 50 rangées au plus par appel.
- **Au besoin**, les débours : `list_expenses(dossier_id, limit=50)` au même tour, puis `set_expense_phase_bulk`.
- **Arrêt** : rapporter les rangées appliquées, inchangées, refusées ; une rangée facturée se reclasse aussi.
- **À éviter** : `get_reference_vocabulary` par rangée, ou pour un code que l'avocat a nommé ; `update_time_entry` sur une rangée facturée.

## F5 Budget
{{GEN:charger}}
- **Déclencheur** : « où en est le budget », « révise le budget ».
- **Appels** : (1) `get_budget(dossier_id, include_history=false)` ; l'historique sur demande ; (2) réviser : un tableau des lignes, une confirmation, puis `create_budget_version(dossier_id, base_version, mode, lines)`.
- **Arrêt** : la lecture répond à « où en est-on ».
- **À éviter** : `get_billing_snapshot` ou `list_time_entries` en plus : le réalisé est déjà dans le budget.
