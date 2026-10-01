# Dossiers et contacts : trouver, créer, corriger, conformité, registres
`find_imported` et `legacy_ref` : en reprise seulement.

## D1 Trouver un contact, ou ses dossiers
{{GEN:charger}}
- **Déclencheur** : « trouve le contact », « ses dossiers ? ».
- **Appels** : (1) `list_parties(query=…)`, un mot, si le dossier lu n'a pas l'id ; (2) `get_partie(partie_id)` pour les coordonnées (la rangée n'a que nom, rôle, ville) ou `dossiers[]`.
- **Arrêt** : `dossiers[]` répond à « quels dossiers ».
- **À éviter** : 31 `list_parties` en rafales de 3 à 8, souvent avant la lecture d'un dossier qui avait les ids.

## D2 Nouveau contact
{{GEN:charger}}
- **Déclencheur** : « crée la fiche de … ».
- **Appels** : (1) le doublon : `list_parties(query=…)`, un mot par appel ; vide : un second mot ; (2) confirmer ; (3) `create_partie(type, …, idempotency_key)`.
- **Arrêt** : rapporter l'id.
- **À éviter** : créer sans chercher : un doublon ne s'efface pas.

## D3 Nouveau dossier
{{GEN:charger}}
- **Déclencheur** : « ouvre un dossier, Béton Nord c. X ».
- **Appels** : (1) le numéro : le demander s'il manque (aucun outil ne le propose ; `create_dossier` refuse un numéro pris) ; même tour, D1 pour les parties ; (2) D2 pour les manquantes ; (3) `get_reference_vocabulary(kind="actions", domaine=…)` (recouvrement : REC) ; `"domaines"`, `"forums"` ou `"districts"` pour un code inconnu seulement ; (4) confirmer, puis `create_dossier(file_number, title, clients, …)`.
- **Arrêt** : l'id, le n°, les `warnings` (sans arborescence, le dossier existe : ne pas le recréer) ; `get_dossier` si la fiche est demandée.
- **À éviter** : tous les vocabulaires d'avance — 11 `get_reference_vocabulary` en 25 s, le 09-15.

## D4 Corriger un dossier ou un contact
{{GEN:charger}}
- **Déclencheur** : « corrige le titre », « ferme le dossier ».
- **Appels** : (1) champ vide : `complete_dossier` ; valeur à remplacer : `update_dossier`, seuls les champs changés, avec `expected_etag` ; (2) ajouter une partie : `update_dossier(add_clients=[…])` ou `add_opposing_parties` ; rôles, avocat, détachement : `update_dossier_party(action=…)` ; (3) statut : `set_dossier_status` seul, ses `warnings` relayés ; (4) contact : `update_partie`, seuls les champs changés ; un nom corrigé ne gagne les dossiers que par `update_dossier_party(action="refresh_names", partie_id=…)` ; mandataire : `update_partie_mandataire`.
- **Arrêt** : pas de relecture.
- **À éviter** : relire le dossier avant et après chaque correction.

## D5 Conformité : identité, conflits
{{GEN:charger}}
- **Déclencheur** : « vérifie les conflits ».
- **Appels** : (1) les dossiers actifs : `get_coverage_report(checks=["CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE"])` ; un client : `get_partie(partie_id)` ; (2) confirmer, puis `record_kyc_status(partie_id, check, status, notes, expected_etag)`.
- **Arrêt** : dire « inscrit, présumé », jamais « vérifié » ; l'obligation relève de deontologie-professionnelle.
- **À éviter** : un `get_partie` par client pour un balayage.

## D6 Signification, événement de prescription
{{GEN:charger}}
- **Déclencheur** : « signifié le … », « la demande est déposée ».
- **Appels** : (1) du dossier lu, sinon `get_dossier` : le `partie_id` et `significations[]` ; (2) confirmer, puis `record_signification(dossier_id, partie_id, date, mode)`, un appel par partie, en parallèle ; (3) `record_prescription_event(dossier_id, type, date)`.
- **Arrêt** : le délai qui en découle : `agenda.md`, A4.
- **À éviter** : réinscrire pour « corriger » : tout s'ajoute, rien ne se retire.
