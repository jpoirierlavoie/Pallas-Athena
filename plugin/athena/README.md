# Athena — plugin 1.2.0

Une compétence qui apprend à Claude à se servir du connecteur Athena **avec le moins
d'appels possible** : d'où vient chaque identifiant, la suite d'appels la plus courte pour
chaque demande courante, ce qu'aucun outil ne fait. Elle ne recopie pas les descriptions des
outils : le serveur les envoie, et la tête de ses INSTRUCTIONS porte le noyau de sécurité.

- `skills/athena/SKILL.md` — le noyau et quatre recettes; moins de 10 Ko.
- `recettes/*.md` — six domaines, lus seulement quand la demande y mène.
- `references/index-outils.md` — **généré**; lu quand aucune recette ne convient.
- `.mcp.json` — désigne le connecteur **par son nom, « Athena »**, jamais par une URL : dans
  Claude Code, une URL masquerait le connecteur claude.ai sans pouvoir s'authentifier. Le
  plugin ne crée donc pas le connecteur : il s'ajoute une fois dans claude.ai.

## Réglages par surface

- **claude.ai** : le connecteur se nomme **« Athena »** (`https://athena.poirierlavoie.ca/mcp`),
  en mode « Sur demande »; « Exécution de code et création de fichiers » activée, sans quoi
  aucune compétence ne se charge. Envisager « Bloqué » pour `preview_templatize`,
  `create_template` et `import_invoice` — à débloquer le temps d'une gabarisation ou d'une reprise.
- **Remplace `pallas-athena`** : retirer cet ancien plugin (bibliothèque de l'organisation et
  « Mes téléversements »), sinon ses copies restent chargées, Claude Code compris.
- **Complément Excel et Toolbox** : désactiver le connecteur — en 30 jours, 149 de leurs 168
  chargements n'ont été suivis d'aucun appel.

## Reconstruire et livrer

La source vit ici, hors d'`athena/` : elle n'est jamais déployée. Les lignes `{{GEN:…}}` se
remplissent à la construction; ne jamais les écrire à la main.

```
cd athena
python -m pytest tests/test_plugin_athena.py -q   # la porte, d'abord
python -m scripts.exporter_plugin_athena           # → ../dist/athena-1.2.0.plugin (non versionné)
rm -rf ../../athena-plugin/plugins/athena
python -m scripts.exporter_plugin_athena --deplie ../../athena-plugin/plugins/athena
```

Puis, dans ce miroir (GitHub `jpoirierlavoie/athena-plugin`) : reporter la version dans
`.claude-plugin/marketplace.json`, valider, pousser. claude.ai s'y synchronise (réglage de
l'organisation), puis Claude Code : rien à téléverser. Une version GitHub portant l'archive
reste facultative (point de téléchargement).

## Ce qui le protège

`athena/tests/test_plugin_athena.py`, dans la porte de déploiement, échoue quand une citation
se périme (outil ou paramètre renommé, valeur retirée) ou qu'un budget casse ; il vérifie aussi
l'en-tête, les termes retirés, `.mcp.json` et les champs de chaque recette. `--check` compare une
reconstruction à l'archive de `dist/` : s'il échoue (ou qu'aucune archive n'existe), la source ou
le connecteur ont changé — reconstruire et livrer. Un changement du registre qui ne touche
que les parties générées laisse le test vert : `--check` le voit.
