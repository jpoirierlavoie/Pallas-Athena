# Reprise de données historiques, un dossier à la fois

## Reprise d'un dossier
{{GEN:charger}}
- **Déclencheur** : « reprends ce fonds ».
- **Appels** : (1) `find_imported(legacy_ref)` par contact, dossier et facture, en parallèle ; (2) ce qui manque : `create_partie`, `create_dossier`, avec `legacy_ref` ; (3) les sources, un tableau confirmé : `create_time_entries_bulk`, `create_expenses_bulk`, une `legacy_ref` par rangée ; une rangée déjà importée se retire (son id : `find_imported`) ; (4) `import_invoice(dossier_id, invoice_number, date, expected_total_cents, time_entry_ids, expense_ids)`, ids tirés de `entities` ; (5) `get_import_audit(dossier_id)`.
- **Arrêt** : la facture reste brouillon ; `envoyée` (`update_invoice`) sur la parole de l'avocat seulement.
- **À éviter** : 61 `create_time_entry` un à un, dont 57 sur 11 dossiers en dix minutes, après 40 `list_time_entries`.

Plafonds qui diffèrent entre outils :
{{GEN:limites_reprise}}
