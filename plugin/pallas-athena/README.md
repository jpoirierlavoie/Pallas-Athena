# Pallas Athéna — plugin 2.0.0

Une compétence qui apprend à Claude à se servir du connecteur Pallas Athena **avec le moins
d'appels possible** : d'où vient chaque identifiant, la suite d'appels la plus courte pour
chaque demande courante, ce qu'aucun outil ne fait. Elle ne recopie pas les descriptions des
outils : le serveur les envoie, et la tête de ses INSTRUCTIONS porte le noyau de sécurité.

- `skills/pallas-athena/SKILL.md` — le noyau et quatre recettes; moins de 10 Ko.
- `recettes/*.md` — six domaines, lus seulement quand la demande y mène.
- `references/index-outils.md` — **généré**; lu quand aucune recette ne convient.
- `.mcp.json` — déclare le connecteur **par son nom**. Jamais par une URL : dans Claude Code,
  elle masquerait le connecteur claude.ai sans pouvoir s'authentifier.

## Réglages par surface

- **claude.ai** : activer « Exécution de code et création de fichiers », sans quoi aucune
  compétence ne se charge; connecteur en mode « Sur demande ». Envisager « Bloqué » pour
  `preview_templatize`, `create_template` et `import_invoice` : un outil bloqué ne coûte plus
  sa description — à débloquer le temps d'une gabarisation ou d'une reprise.
- **Claude Code** : le plugin s'y synchronise depuis le compte. Vérifier qu'il affiche 2.0.0,
  puis supprimer les deux copies 1.2.0 périmées (`pallas-athena`, `pallas-athena~g3`).
- **Complément Excel et Toolbox** : désactiver le connecteur — en 30 jours, 149 de leurs 168
  chargements n'ont été suivis d'aucun appel.

## Reconstruire et téléverser

La source vit ici, hors d'`athena/` : elle n'est jamais déployée. Les lignes `{{GEN:…}}` se
remplissent à la construction; ne jamais les écrire à la main.

```
cd athena
python -m pytest tests/test_plugin_pallas.py -q   # la porte, d'abord
python -m scripts.exporter_plugin_pallas           # → ../dist/pallas-athena-2.0.0.plugin (racine du dépôt)
python -m scripts.exporter_plugin_pallas --check   # diffère-t-il de la dernière construction ?
python -m scripts.exporter_plugin_pallas --deplie ../dist/deplie   # et les fichiers, à relire
```

`dist/` n'est pas versionné. Téléverser le `.plugin` dans la page des plugins de
l'organisation, sur claude.ai (compte propriétaire), à la place de la version précédente.

## Ce qui le protège

`athena/tests/test_plugin_pallas.py`, dans la porte de déploiement, échoue quand une citation
se périme (outil ou paramètre renommé, valeur retirée) ou qu'un budget casse ; il vérifie aussi
l'en-tête, les termes retirés, `.mcp.json` et les champs de chaque recette. `--check` compare une
reconstruction à l'archive de `dist/` : s'il échoue (ou qu'aucune archive n'existe), la source ou
le connecteur ont changé — reconstruire et téléverser. Un changement du registre qui ne touche
que les parties générées laisse le test vert : `--check` le voit.
