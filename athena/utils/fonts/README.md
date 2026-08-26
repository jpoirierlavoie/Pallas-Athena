# Noto Serif — vendored font files (SIL Open Font License 1.1)

The app's content font, pinned and vendored — never fetched at runtime (supply-chain
rule: no CDN, strict CSP). Two independent sets serve two independent renderers:

## PDF (reportlab) — this directory

Static instances registered at import time by `utils/export_pdf.py`
(reportlab does not support variable-font weight instances):

| File | Source | SHA-256 |
|---|---|---|
| `NotoSerif-Regular.ttf` | notofonts/latin-greek-cyrillic release **NotoSerif-v2.015**, `NotoSerif/hinted/ttf/` | `19e72cd8d595fae5bd74a5206f5d938512e1183d4fed7abb1ec1be1d7efa5f88` |
| `NotoSerif-Bold.ttf` | same release | `96656aa5cec8f1d6fd0e804c1fad397e1a1cfa082e6642124e0bda68cd8363ce` |

Release: https://github.com/notofonts/latin-greek-cyrillic/releases/tag/NotoSerif-v2.015

This directory sits deliberately OUTSIDE `static/` — the TTFs are read by
reportlab only and are not publicly served.

## Web (`static/vendor/`) — for reference

Variable-weight (100–900) latin-subset woff2, referenced by `@font-face` in
`static/src/app.input.css`. Downloaded 2026-08-07 from the Google Fonts CSS API
(`family=Noto+Serif…` gstatic **v33**; `family=Noto+Sans…` gstatic **v42**).
Noto Sans is the UI font; Noto Serif renders note content (and these TTFs
render the PDFs):

| File | SHA-256 |
|---|---|
| `noto-sans-v42-latin-wght.woff2` | `51ca196f49a33e79e7870ff88ebd2829a3f627a51e7d690986618f0e7ad2b52d` |
| `noto-sans-v42-latin-wght-italic.woff2` | `b21ff49befe2af5b88a3b622b3446e83199229f15ea7571a9313b15ed706298b` |
| `noto-serif-v33-latin-wght.woff2` | `46281456234014ceb2a79bff447245de0f76b8d803be0738972ed374c3206c5b` |
| `noto-serif-v33-latin-wght-italic.woff2` | `a1a6ebee6d69b2628ac102f55cb447135643a5401ae92eb77f4e2c6888dfce22` |

The `unicode-range` in `app.input.css` is copied verbatim from the Google CSS
responses (latin blocks — cover French incl. œ/Œ, « », ’, —; identical range
for both families).

## Material Symbols Outlined (`static/vendor/`) — icônes en ligatures

Sous-ensemble variable (axes `opsz 20..48`, `wght 100..700`, `FILL 0..1` —
GRAD exclu, app exclusivement claire) contenant EXACTEMENT les glyphes de
`utils/icons.MATERIAL_ICONS`, épinglé par `tests/test_icons.py`. Téléchargé
2026-08-10 via l'API css2 (User-Agent navigateur obligatoire), version
gstatic **v364** :

| Fichier | SHA-256 |
|---|---|
| `material-symbols-outlined-v368-390acc0f.woff2` | `390acc0f2b3dde6e99813e50f60e067013412caddac7e37304da7766db5a05b2` |

URL de régénération (liste `icon_names=` TRIÉE = `sorted(MATERIAL_ICONS)`) :

```
https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL@20..48,100..700,0..1&icon_names=<liste-triée-virgules>&display=block
```

**Procédure « ajouter une icône »** : 1) ajouter le nom à `MATERIAL_ICONS`
(`utils/icons.py`) ; 2) reconstruire l'URL ci-dessus et re-télécharger le
woff2 pointé par le `@font-face` de la réponse ; 3) sha256 → **NOUVEAU nom**
`material-symbols-outlined-vNNN-<sha8>.woff2` + suppression de l'ancien
(actif immuable — jamais d'édition en place) ; 4) `url()` dans
`app.input.css` → recompilation + re-hachage CSS → fan-out complet
(gabarits ×3, Early Hints ×2, PRECACHE + bump `STATIC_CACHE`, test des
en-têtes) ; 5) mettre à jour ce tableau ; 6) `pytest`.

Licence **Apache-2.0** (pas OFL) — `static/vendor/material-symbols-outlined-v368-Apache-2.0.txt`.

## License (Noto)

SIL OFL 1.1 — `OFL.txt` here and `static/vendor/noto-serif-v33-OFL.txt` accompany
the fonts as the license requires. Self-hosting and PDF embedding are permitted.
