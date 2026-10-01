# Comptabilité : lire le fidéicommis ; écrire aux registres exige une autorisation distincte

## Soldes et registre en fidéicommis
{{GEN:charger}}
- **Déclencheur** : « combien en fidéicommis », « solde du client », « chèques en circulation », « conciliation en retard ».
- **Appels** : (1) le cabinet : `get_trust_snapshot()` — les comptes, leur conciliation, les chèques en circulation, `by_dossier` ; (2) un dossier, par client : `get_trust_balance(dossier_id)` ; (3) les mouvements : `list_trust_transactions(dossier_id, client_id)` pour une carte-client, `list_trust_transactions(account_id)` seul pour le journal entier, paginé par `cursor` ; ailleurs, `truncated: true` : resserrer par `date_from` et `date_to`, les plus récents pouvant manquer.
- **Arrêt** : `book_*` est le « Solde aux livres » ; `cleared_*`, le « Disponible (compensé) », ne se présente jamais comme « Solde » ; l'écart est en transit.
- **À éviter** : un `get_trust_balance` par dossier pour le portrait du cabinet : le `by_dossier` du portrait le donne.

## Écrire aux registres : fidéicommis, administration
{{GEN:outils_comptables}}
- **Déclencheur** : « inscris le dépôt », « paie la facture depuis le fidéicommis », « compense ces chèques ».
- **Appels** : (1) ces outils n'existent que sous l'autorisation distincte « Comptabilité » ; l'autorisation d'écriture n'en tient jamais lieu ; (2) absents de votre liste : dire à l'avocat de saisir le mouvement dans l'application, sans chercher d'autre voie ; (3) présents : lire la description de chacun avant son premier appel, et n'inscrire qu'un mouvement survenu à la banque, confirmé par l'avocat, avec une clé.
- **Arrêt** : rien ne se supprime ; une écriture du fidéicommis se corrige par contre-passation seulement, une écriture d'administration par modification tant qu'elle le permet, puis par contre-passation. Conciliation, virement entre dossiers, compte bancaire : l'application seule.
- **À éviter** : consigner un mouvement de fonds dans une note ou une tâche, faute d'outil.
