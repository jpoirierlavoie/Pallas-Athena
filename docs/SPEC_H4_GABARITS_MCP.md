# Phase H.4 — Surface MCP des gabarits

> **Statut :** devis, non implémenté. Rédigé le 2026-09-02 par lecture du dépôt,
> puis vérifié affirmation par affirmation contre le code (`athena/mcp/`,
> `athena/routes/doc_templates.py`, `athena/utils/docx_fill.py`,
> `athena/utils/template_fields.py`, `athena/utils/markdown_docx.py`,
> `athena/models/doc_template.py`, `athena/models/document.py`, `CLAUDE.md`,
> `GABARITS_PLACEHOLDERS.md`). Les affirmations que la lecture n'a pas pu
> éprouver sont listées au §14.
>
> **Étiquette.** *H.4* plutôt qu'une phase nouvelle : ceci n'ouvre aucun
> sous-système, cela prolonge celui des gabarits (H → H.2 note d'honoraires →
> H.3 note imprimée → **H.4 surface MCP**) d'une troisième voie d'appel.

---

## 1. Objet

Permettre à un modèle — le connecteur Claude, et par parité D9 le clavardage
interne — de produire un `.docx` mis en forme à partir d'un gabarit existant en ne
fournissant **que le contenu juridique**. Le modèle rédige les allégations ; le
serveur résout l'intitulé, les parties, le district, les dates, les montants, et
appelle le moteur de remplissage. Aucun `.docx` ne quitte l'application, aucun
catalogue de champs n'est réimplémenté ailleurs.

Trois outils : `list_gabarits` (lecture), `get_gabarit` (lecture),
`fill_gabarit` (écriture).

**Compte d'outils — compté dans le code, pas repris de la documentation.**
`TOOLS` porte aujourd'hui **53** entrées, dont **24** en `SCOPE_WRITE`
(`WRITE_TOOLS` a bien 24 membres) : donc **29 lectures + 24 écritures = 53**.
La phase porte le connecteur à **56 (31 lectures, 25 écritures)**.

> ⚠️ `CLAUDE.md` et la docstring de `mcp/handlers.py` annoncent encore
> « 52 outils — 29 lectures, 23 écritures ». Le chiffre est **déjà périmé** :
> `record_document_analysis` est absent de leur énumération. Voir §13, L3.

### Hors périmètre

Téléversement, modification et suppression de gabarits restent exclusivement à
l'écran. Les deux `kind` liés à leur flux — `note_honoraires` (rempli par
`/factures/<id>/note-docx`) et `note` (rempli par `/notes/<id>/gabarit-docx`) —
ne sont pas remplissables par cette voie (§3, D3).

---

## 2. Constat : le moteur existe, la surface manque

Tout ce qu'il faut pour remplir un gabarit depuis un appel non-HTTP est déjà écrit
et déjà éprouvé, et **rien de tout cela n'exige un contexte Flask** :

| Brique | Rôle |
|---|---|
| `models.doc_template.list_templates(category, search)` | inventaire, tri par nom, filtrage en Python (collection bornée, aucun index) |
| `models.doc_template.get_template(id)` / `get_template_bytes(id)` | métadonnées / octets du `.docx` depuis Storage |
| `utils.template_fields.classify_placeholders(names)` | `Classification(auto: dict, manual: list, passthrough: list, slots_required: set)` |
| `utils.template_fields.resolve_values(names, *, dossier, client, adverse, destinataire, firm, today)` | valeurs auto |
| `utils.template_fields.fallback_value(name, is_auto)` | `[CHAMP MANQUANT : …]` / `[À COMPLÉTER : …]` |
| `utils.docx_fill.fill_docx(bytes, values, *, rows_by_region, conditions, rich_values)` | le remplissage stdlib |
| `utils.cabinet.cabinet_dict()` | le dict `cabinet.*` |
| `models.folder.get_or_create_folder(dossier_id, name)` | le dossier « Projets », idempotent |
| `models.document.projet_document_name(ref, nom, jour)` | `"REF - AAAA-MM-JJ - Projet Nom"` |
| `models.document.upload_document(..., user_id)` | Storage + enregistrement Firestore — **voir §7, B1** |

Trois constats rendent l'affaire plus légère qu'elle n'en a l'air.

**Les blocs sont déjà réservés.** `classify_placeholders` verse en `passthrough`
tout nom inconnu du catalogue et absent de `MANUAL_FIELDS` — donc tout bloc en
capitales (`{{FAITS}}`, `{{CONCLUSIONS}}`, `{{MOYENS}}`), plus `{{civilité}}` et
`{{salutations}}`. Ces noms survivent verbatim dans la sortie. C'est exactement
l'emplacement que cette phase remplit ; rien à reclasser.

**La voie riche existe.** `fill_docx` accepte depuis H.3 un paramètre
`rich_values` (markdown brut → mise en forme Word via `utils/markdown_docx.py`),
aujourd'hui utilisé par le seul flux d'impression de note. Rien dans le moteur ne
le lie à `{{note.contenu}}`.

**La numérotation est déjà bonne par la voie ordinaire.** Dans `fill_docx`, la
voie `values` se scinde **d'elle-même, par la valeur** : `_BLANK_LINE_RE`
(`\n\s*\n`) départage. Une valeur *sans* ligne blanche est substituée sur place
(*scalar substitution*) ; une valeur *avec* déclenche l'*expansion de bloc* — le
`<w:p>` hôte est cloné **verbatim** par `_PARAGRAPH_RE.sub`, une fois par tronçon,
donc tout son `<w:pPr>` — `numPr` compris — est dupliqué tel quel. Aucune fusion,
aucun `_merge_ppr` : cette fonction vit dans `markdown_docx.py` et n'est atteinte
que par la voie riche. Un `{{FAITS}}` seul dans un paragraphe de liste numérotée
du gabarit donne donc de vraies allégations numérotées par Word.

Il n'y a **aucun drapeau à poser** pour cela : c'est le contenu que le modèle
envoie qui choisit. D'où l'importance de la description de l'outil.

> Piège à connaître : dans un bloc, un **retour de ligne isolé devient une
> espace** (`chunk.replace("\n", " ")`). Seule une **ligne blanche** crée un
> paragraphe. Sans cela, un exposé de faits arrive en un seul paragraphe-fleuve,
> portant un seul numéro.
>
> Second repli à connaître : un placeholder de bloc qu'aucun paragraphe ne peut
> accueillir (hôte imbriqué dans une zone de texte) est substitué **en ligne**,
> tronçons joints par des espaces — la séparation en paragraphes est perdue, le
> contenu ne l'est pas. Silencieux aujourd'hui ; même famille de problème que B9.

Ce qui manque : `routes/doc_templates.generate()` est le seul assemblage pour
`kind="gabarit"`, et il lit `request.form`. Le connecteur MCP n'expose aucun outil
de gabarit.

---

## 3. Décisions de conception

**D1 — Le contenu va au moteur, jamais le moteur au contenu.**
L'alternative — exporter les `.docx` et le catalogue vers le modèle — obligerait à
réimplémenter hors du dépôt le catalogue de `utils/template_fields.py` (39 ko :
`CATALOG`, `FLAT_ALIASES`, `MANUAL_FIELDS`, formatage fr-CA, permutation des
rôles, préférence d'adresse selon le rôle du contact, taxonomie des actions ; la
fonction `resolve_values` elle-même n'est qu'un itérateur de quarante lignes
au-dessus de tout cela). Deux sources de vérité pour un même vocabulaire dérivent
toujours, et la dérive serait invisible : le document se génère quand même.

**D2 — `dossier_id` obligatoire sur `fill_gabarit`.**
Le flux web autorise une génération sans dossier en diffusant le `.docx` en
téléchargement. Un outil MCP n'a pas cette sortie : renvoyer les octets encodés
ferait entrer un document entier dans le contexte du modèle, pour un coût élevé et
un bénéfice nul (le modèle vient d'en écrire le contenu). Sans dossier, il n'y a
nulle part où déposer le résultat — donc refus, en français, nommant le champ.

**D3 — `kind` restreint à `"gabarit"`.**
`note_honoraires` a besoin du contexte `facture.*`, des régions répétées et des
drapeaux conditionnels que seul `utils/invoice_docx.py` construit ; `note` a
besoin de `note.*` et de `utils/note_docx.py`. Les exposer ici dupliquerait deux
constructeurs de contexte pour un gain nul : ces deux documents n'ont pas de
contenu libre à rédiger.

**D4 — Les blocs sont un TABLEAU d'objets, pas un dictionnaire.**
`mcp.tools.validate_args` est un validateur JSON-Schema **de sous-ensemble** : il
connaît `additionalProperties: false` mais pas `additionalProperties: <schéma>`,
ni `propertyNames`, ni `maxProperties`. Sa docstring pose la règle — *« a schema
the validator cannot express cannot be declared »* — parce que le même validateur
éprouve les `outputSchema` contre les charges réelles. `items` récursant sur
`_validate_value`, un tableau d'objets typés est parfaitement exprimable.

**D5 — `get_gabarit` ne renvoie pas les valeurs résolues.**
Le modèle n'a pas besoin de savoir que `{{dossier.demandeur}}` vaut tel nom ; il a
besoin de savoir que ce champ est rempli par le serveur et n'est donc pas le sien.
Renvoyer les valeurs ferait sortir adresses, courriels et téléphones du client
pour un bénéfice nul — `get_dossier` et `get_partie` fournissent déjà ces données
quand elles servent vraiment. La sortie porte `resolved: true|false`, jamais la
valeur. Un paramètre `include_values` est délibérément écarté : une option à
usage rare finit par être passée par défaut.

**D6 — Voie `values` par défaut ; voie riche sur demande explicite, par bloc.**
`markdown: false` par défaut. `false` → `values` (paragraphes séparés par une
ligne blanche, numérotation héritée du gabarit) ; `true` → `rich_values`. Un même
nom ne peut figurer qu'une fois : `fill_docx` lève si un nom est à la fois dans
`values` et `rich_values` (*« no silent precedence »*), et le handler doit refuser
avant, en nommant le doublon. **Attention** : la voie riche a un défaut latent
(§13, L1) qui doit être corrigé avant qu'elle serve ici.

**D7 — Plafonds propres à la voie MCP.**
`routes/doc_templates._SCALAR_MAX_CHARS = 2000` est le plafond du formulaire,
taillé pour des champs de métadonnée. Un bloc d'allégations le dépasse.

**D8 — Aucune URL signée, aucun `storage_path` en sortie.**
Règle de sécurité du dépôt, sans exception. La sortie porte `document_id` et
`display_name` ; la relecture passe par `get_document_text`, qui lit déjà le
`.docx`.

**D9 — Idempotence par le protocole d'écriture existant.**
`fill_gabarit` passe par `mcp.write_support.run_write`. Sans clé, un réessai crée
un second document dans « Projets » — bruit visible, jamais perte. Avec clé, le
rejeu renvoie le résultat stocké.

---

## 4. Schémas d'entrée

Conventions respectées : `additionalProperties: False` partout, `description` par
usage sur chaque propriété, `title` français.

**Les deux énumérations sont des littéraux, pas des dérivations.** Elles vivent
dans `models/doc_template.py`, et `mcp/tools.py` ne peut pas importer `models.*` :
`models/__init__.py` instancie `firestore.Client()` au chargement. La convention
du dépôt est explicite (`_NOTE_CATEGORIES`, `_DOSSIER_STATUSES`) — recopier le
littéral et **épingler l'égalité dans `tests/test_mcp_tools.py`** :

```python
# Copiées exactement de models.doc_template.VALID_CATEGORIES / VALID_KINDS
# (elles sont en français). tests/test_mcp_tools.py épingle les deux paires
# l'une contre l'autre. Littéral et non dérivé : importer models.* exécute
# firestore.Client() au chargement — voir models/__init__.py.
_GABARIT_CATEGORIES = ["procédure", "correspondance", "autre"]
_GABARIT_KINDS = ["gabarit", "note_honoraires", "note"]
```

(La dérivation reste la bonne voie quand le module source est pur —
`_COVERAGE_CODES`, `utils/phases.py` — mais ce n'est pas le cas ici.)

**`maxItems` est déclaratif, jamais suffisant.** Le validateur ne l'implémente pas
(il est déclaré « for the CLIENT's benefit »). Le précédent est `_phase_bulk_items`,
dont le `maxItems: PHASE_BULK_MAX` est doublé d'une garde côté handler. Les gardes
de §6.2 comptent donc les entrées elles-mêmes.

### 4.1 `list_gabarits` — lecture

```python
"list_gabarits": {
    "title": "Lister les gabarits",
    "description": (
        "List the .docx templates (« gabarits ») available for filling. "
        "Returns metadata only — never the file, never a URL. Take a "
        "gabarit_id from here and pass it to get_gabarit to see which "
        "placeholders the application fills by itself and which blocks are "
        "yours to write. Only kind « gabarit » can be filled through this "
        "connector: the note-d'honoraires and note-print templates are "
        "bound to their own flows in the application."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": _GABARIT_CATEGORIES,
                "description": "Filter by category. Omit for all.",
            },
            "query": {
                "type": "string", "maxLength": 120,
                "description": "Free-text match on name and description.",
            },
            "include_other_kinds": {
                "type": "boolean",
                "description": (
                    "true also lists the note-d'honoraires and note-print "
                    "templates, which this connector cannot fill — for "
                    "inventory questions only. Default false."
                ),
            },
            "limit": _limit(20),
        },
        "additionalProperties": False,
    },
    "handler": "list_gabarits",
},
```

Aucun index Firestore nouveau : `list_templates` fait un seul `order_by("name")`
sur une collection bornée et filtre en Python. Le filtre `kind` s'ajoute dans le
handler, pas dans le modèle — surface de changement minimale.

### 4.2 `get_gabarit` — lecture

L'outil charnière. Il répond à une seule question : *de ce gabarit, qu'est-ce qui
est à moi ?*

```python
"get_gabarit": {
    "title": "Champs d'un gabarit",
    "description": (
        "Inventory of one gabarit's placeholders, split three ways. AUTO "
        "fields the application resolves by itself from the dossier, the "
        "selected parties, the firm and today's date — never yours to "
        "supply. MANUAL fields are short letter metadata you may set. "
        "BLOCKS are the free-form legal content the gabarit reserves for "
        "you ({{FAITS}}, {{CONCLUSIONS}}, {{MOYENS}}, and any other name "
        "the catalog does not know). Pass dossier_id to see which auto "
        "fields actually resolve on that file — an unresolved one prints "
        "as « [CHAMP MANQUANT : name] » in the output, which is a data gap "
        "to report to the lawyer, not something to write around. Values "
        "are never returned: use get_dossier / get_partie when you need "
        "the underlying data. Block names are returned EXACTLY as the "
        "gabarit spells them; fill_gabarit matches them case-sensitively."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "gabarit_id": _id("The gabarit to inspect (UUIDv4), from list_gabarits."),
            "dossier_id": _id(
                "Resolve the auto fields against this dossier (UUIDv4). Omit "
                "for the inventory alone, with every auto field reported "
                "unresolved."
            ),
            "client_id": _id(
                "Which of the dossier's clients fills the « client » slot "
                "(UUIDv4). Defaults to the dossier's first client."
            ),
            "adverse_id": _id(
                "Which opposing party fills the « adverse » slot (UUIDv4). "
                "Defaults to the first opposing party."
            ),
            "destinataire_id": _id(
                "Which contact fills the « destinataire » slot (UUIDv4) — the "
                "addressee of a letter. No default: a gabarit that needs the "
                "slot and does not get it renders « [CHAMP MANQUANT : …] »."
            ),
        },
        "required": ["gabarit_id"],
        "additionalProperties": False,
    },
    "handler": "get_gabarit",
},
```

### 4.3 `fill_gabarit` — écriture (`athena:write`)

```python
"fill_gabarit": {
    "title": "Remplir un gabarit",
    "description": (
        "WRITE — fill a gabarit and SAVE the .docx into the dossier's "
        "« Projets » folder. Call get_gabarit FIRST: you may only supply "
        "the names it reports as blocks and as manual fields, spelled "
        "EXACTLY as reported (matching is case-sensitive). Every other "
        "placeholder is resolved by the application; supplying one is "
        "refused, not silently ignored. "
        "WRITE ALLEGATIONS AS PLAIN PARAGRAPHS SEPARATED BY A BLANK LINE, "
        "with no numbering of your own and no markdown list: the gabarit's "
        "own paragraph carries the Word numbering and each of your "
        "paragraphs inherits it as a real, continuous, restyleable number. "
        "A single newline is NOT a paragraph break — it collapses to a "
        "space. Leave markdown false unless a block genuinely needs "
        "internal formatting (sub-headings, bold, a table). "
        "The result is a draft (« projet »): saved, never sent, never "
        "signed — the lawyer opens it in Word. A retry without "
        "idempotency_key saves a SECOND document — use one on every "
        "unattended write."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "gabarit_id": _id("The gabarit to fill (UUIDv4), from list_gabarits."),
            "dossier_id": _id(
                "The dossier to file the result in (UUIDv4). REQUIRED — the "
                ".docx is saved in this dossier's « Projets » folder, and "
                "there is no download path on this connector."
            ),
            "client_id": _id("« client » slot (UUIDv4). Defaults to the dossier's first client."),
            "adverse_id": _id("« adverse » slot (UUIDv4). Defaults to the first opposing party."),
            "destinataire_id": _id("« destinataire » slot (UUIDv4). No default."),
            "blocs": {
                "type": "array",
                "maxItems": BLOCS_MAX_ITEMS,          # 12 — déclaratif ; garde §6.2.9
                "description": (
                    "The free-form content blocks, one entry per placeholder "
                    "name reported as a block by get_gabarit. A block you "
                    "omit stays literal « {{name}} » in the output for the "
                    "lawyer to complete in Word."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "nom": {
                            "type": "string", "minLength": 1, "maxLength": 64,
                            "description": (
                                "The placeholder name WITHOUT the braces, "
                                "spelled exactly as get_gabarit reported it. "
                                "Matching is case-sensitive."
                            ),
                        },
                        "contenu": {
                            "type": "string", "minLength": 1,
                            "maxLength": BLOC_MAX_CHARS,       # 20_000
                            "description": (
                                "The text, in French. Plain paragraphs "
                                "separated by a BLANK LINE. No numbering of "
                                "your own."
                            ),
                        },
                        "markdown": {
                            "type": "boolean",
                            "description": (
                                "true converts the content to real Word "
                                "formatting (headings, bold, lists, tables) "
                                "instead of plain paragraphs. Default false. "
                                "Never use it for numbered allegations."
                            ),
                        },
                    },
                    "required": ["nom", "contenu"],
                    "additionalProperties": False,
                },
            },
            "champs_manuels": {
                "type": "array",
                "maxItems": CHAMPS_MANUELS_MAX_ITEMS,  # 12 — déclaratif ; garde §6.2.9
                "description": (
                    "Values for the short manual fields get_gabarit reports "
                    "(objet_lettre, privilège, pièces_jointes, …). An "
                    "omitted one falls back to its default, then to "
                    "« [À COMPLÉTER : name] »."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "nom": {"type": "string", "minLength": 1, "maxLength": 64,
                                "description": "The manual field's name, without braces."},
                        "valeur": {"type": "string",
                                   "maxLength": CHAMP_MANUEL_MAX_CHARS,   # 2_000
                                   "description": "Its value. Respect the field's options when it has any."},
                    },
                    "required": ["nom", "valeur"],
                    "additionalProperties": False,
                },
            },
            **_write_protocol_props(),
        },
        "required": ["gabarit_id", "dossier_id"],
        "additionalProperties": False,
    },
    "handler": "fill_gabarit",
    "scope": SCOPE_WRITE,
},
```

**Constantes nouvelles** (dans `mcp/tools.py`, à côté de `DRAFT_*_MAX_CHARS`) :

| Constante | Valeur | Justification |
|---|---|---|
| `BLOC_MAX_CHARS` | 20 000 | Un exposé de faits long tient largement ; `DRAFT_CONTENT_MAX_CHARS` vaut 100 000, mais un brouillon est un document entier, un bloc en est une section |
| `BLOCS_TOTAL_MAX_CHARS` | 60 000 | Plafond agrégé, vérifié dans le handler. Le cap de requête de 1 Mo sur `/mcp` reste le garde-fou dur ; 60 000 caractères d'UTF-8 français pèsent ≈ 65 ko |
| `BLOCS_MAX_ITEMS` / `CHAMPS_MANUELS_MAX_ITEMS` | 12 | Déclarés au schéma pour le client, **appliqués dans le handler** — le validateur ignore `maxItems` |
| `CHAMP_MANUEL_MAX_CHARS` | 2 000 | Aligné sur `_SCALAR_MAX_CHARS` du formulaire — même nature de champ |

`WRITE_TOOLS` gagne `"fill_gabarit"`. `EDIT_TOOLS` **non** : l'outil crée un
document, il n'en remplace aucun. `_WRITE_ANNOTATIONS` pose déjà
`destructiveHint: False` et `idempotentHint: False` — aucune annotation à
surcharger.

---

## 5. Schémas de sortie (`mcp/output_schemas.py`)

Trois règles du dépôt : jamais `additionalProperties: false` en sortie ;
`required` ne porte que les clés **toujours** présentes ; racine toujours
`type: "object"`. Rappel de mécanique : **`_obj(required=None)` exige TOUTES les
clés déclarées** — tout champ conditionnel oblige à passer `required=[…]`
explicitement.

`Classification.slots_required` est un **`set`** : il faut le `sorted()` avant
sérialisation, comme le font déjà `routes/doc_templates` et
`models/doc_template._extraction_fields`.

### `list_gabarits` — `_list_envelope(_gabarit_summary())`

```python
def _gabarit_summary() -> dict:
    return _obj({
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "category": {"type": "string"},
        "kind": {"type": "string"},
        "version": {"type": "integer"},
        "placeholder_count": {"type": "integer"},
        "bloc_count": {"type": "integer"},
        "slots_required": _arr({"type": "string"}),
        "fillable": {"type": "boolean"},          # kind == "gabarit"
        "has_warnings": {"type": "boolean"},
        "updated_at": {"type": ["string", "null"]},
    })
```

### `get_gabarit`

```python
"get_gabarit": _obj({
    "gabarit": _obj({
        "id": {"type": "string"}, "name": {"type": "string"},
        "kind": {"type": "string"}, "category": {"type": "string"},
        "version": {"type": "integer"},
        "fillable": {"type": "boolean"},
        "slots_required": _arr({"type": "string"}),
        "validation_warnings": _arr({"type": "string"}),
    }),
    "dossier": {"type": ["object", "null"]},        # {id, file_number, title} ou null
    "slots": _obj({                                 # ce qui a été RETENU, pas ce qui a été demandé
        "client_id": {"type": ["string", "null"]},
        "adverse_id": {"type": ["string", "null"]},
        "destinataire_id": {"type": ["string", "null"]},
    }),
    "auto_fields": _arr(_obj({
        "name": {"type": "string"},
        "resolved": {"type": "boolean"},
        "slot": {"type": ["string", "null"]},
    })),
    "manual_fields": _arr(_obj({
        "name": {"type": "string"},
        "default": {"type": "string"},
        "options": {"type": ["array", "null"]},
    })),
    "blocs": _arr(_obj({
        "name": {"type": "string"},
        "uppercase": {"type": "boolean"},
    })),
    "unresolved_auto_fields": _arr({"type": "string"}),
    "warnings": _arr({"type": "string"}),
}),
```

`unresolved_auto_fields` duplique délibérément l'information portée par
`auto_fields[].resolved` : c'est la seule chose que le modèle doit **rapporter à
l'avocat**, et une liste courte a plus de chances d'être lue qu'un drapeau noyé
dans quarante lignes.

### `fill_gabarit`

```python
"fill_gabarit": _obj(
    {
        "filled": {"type": "boolean"},
        "document_id": {"type": "string"},
        "display_name": {"type": "string"},
        "folder": {"type": "string"},               # « Projets »
        "dossier_id": {"type": "string"},
        "gabarit": _obj({"id": {"type": "string"}, "name": {"type": "string"},
                         "version": {"type": "integer"}}),
        "fields": _obj(
            {
                "auto_resolved": {"type": "integer"},
                "auto_missing": _arr({"type": "string"}),     # → [CHAMP MANQUANT : …]
                "manual_missing": _arr({"type": "string"}),   # → [À COMPLÉTER : …]
                "blocs_filled": _arr({"type": "string"}),
                "blocs_left_verbatim": _arr({"type": "string"}),
                "blocs_demoted": _arr({"type": "string"}),    # cf. §7, B9
            },
            required=["auto_resolved", "auto_missing", "manual_missing",
                      "blocs_filled", "blocs_left_verbatim"],
        ),
        "warnings": _arr({"type": "string"}),
        "idempotent_replay": {"type": "boolean"},
    },
    required=["filled", "document_id", "display_name", "folder", "dossier_id",
              "gabarit", "fields", "warnings", "idempotent_replay"],
),
```

`blocs_demoted` est **conditionnel** — il n'existe que si l'option B9(ii) est
retenue. D'où son absence de `required`, qui laisse les deux issues valides sans
retoucher le contrat.

---

## 6. Handlers (`mcp/handlers.py`)

### 6.1 Helper partagé

Le contexte de remplissage est le même pour `get_gabarit` et `fill_gabarit` :

```python
def _gabarit_context(args: dict, *, require_dossier: bool) -> _GabaritCtx:
    """Résout gabarit + dossier + les trois créneaux, exactement comme
    routes/doc_templates._fields_context, mais sans `request`.

    Reproduit la MÊME logique de repli : un client_id absent ou étranger au
    dossier retombe sur le premier client ; idem adverse ; destinataire sans
    défaut. La divergence des deux voies serait invisible et se lirait comme
    un bogue du gabarit."""
```

Deux options :

- **(a) Hoister.** Extraire de `routes/doc_templates.py` un
  `utils/gabarit_context.py` — résolution des créneaux, classification,
  résolution des valeurs — que la route et le handler appellent tous deux.
  *Recommandé*, et cohérent avec le précédent `utils/cabinet.py`, hoisté pour
  exactement cette raison (*« about to grow a third copy »*).
- **(b) Dupliquer** dans le handler et épingler l'équivalence par un test. Moins
  de mouvement, mais la troisième copie arrive un jour.

### 6.2 `fill_gabarit` — garde-fous, dans l'ordre

Doctrine du dépôt : *les gardes tournent dans le handler, en amont du modèle*, en
français, en nommant le champ fautif.

1. `gabarit_id` introuvable → refus nommant `list_gabarits`.
2. `template["kind"] != "gabarit"` → refus nommant le flux compétent
   (« *les notes d'honoraires se génèrent depuis la facture* »).
3. `dossier_id` introuvable → refus, **sans repli** vers une génération sans
   dossier (posture de `save_draft` : « *n'omettez pas dossier_id pour contourner
   cette erreur* »).
4. Chaque créneau fourni doit résoudre **et**, pour `client` et `adverse`,
   appartenir au dossier ; sinon refus. La route retombe silencieusement sur le
   premier — acceptable pour un humain qui voit le résultat à l'écran, pas pour un
   appel non surveillé.
5. Chaque `blocs[].nom` ∈ `classification.passthrough`, **comparaison exacte**
   (`_name_pattern` n'a pas `re.IGNORECASE` : l'insensibilité à la casse du
   sous-système est celle de `_canonical_for`, donc des seuls champs auto). Un nom
   `auto` → refus (« *ce champ est rempli par l'application* ») ; un nom `manual`
   → refus renvoyant vers `champs_manuels` ; un nom absent du gabarit → refus
   énumérant les blocs disponibles.
6. Doublon de `nom` dans `blocs`, ou nom présent dans `blocs` **et**
   `champs_manuels` → refus. (`fill_docx` lèverait de toute façon sur le
   croisement `values`/`rich_values` : mieux vaut un message qui nomme le nom.)
7. Chaque `champs_manuels[].nom` ∈ `MANUAL_FIELDS`, et sa valeur ∈ `options`
   quand le champ en a. La route ne le vérifie pas — le `<select>` s'en charge.
8. `sum(len(contenu))` ≤ `BLOCS_TOTAL_MAX_CHARS` → refus chiffré sinon.
9. `len(blocs)` ≤ `BLOCS_MAX_ITEMS` et `len(champs_manuels)` ≤
   `CHAMPS_MANUELS_MAX_ITEMS` — **le validateur n'applique pas `maxItems`**
   (§4).
10. `get_template_bytes` renvoyant `None` → refus (« *téléversez le gabarit à
    nouveau* »).

### 6.3 Assemblage

```python
def fill_gabarit(args: dict) -> dict:
    return run_write("fill_gabarit", args, lambda: _fill_gabarit_impl(args))


def _fill_gabarit_impl(args: dict) -> dict:
    ctx = _gabarit_context(args, require_dossier=True)      # §6.1 + gardes §6.2

    values = dict(ctx.resolved_auto)                        # resolve_values
    for name in ctx.classification.auto:
        values.setdefault(name, fallback_value(name, is_auto=True))
    for name in ctx.classification.manual:
        values[name] = (
            fournis.get(name)
            or MANUAL_FIELDS[name]["default"]
            or fallback_value(name, is_auto=False)
        )

    rich_values = {b["nom"]: b["contenu"] for b in blocs if b.get("markdown")}
    for b in blocs:
        if not b.get("markdown"):
            values[b["nom"]] = b["contenu"]                 # cf. note ci-dessous

    filled = fill_docx(                                     # DocxFillError → ToolArgumentError
        get_template_bytes(gabarit_id),
        values,
        rich_values=rich_values or None,
    )

    folder = get_or_create_folder(dossier_id, GENERATED_FOLDER_NAME)
    display = projet_document_name(dossier["file_number"], template["name"], _today_mtl())
    out_name = secure_filename(f"{display}.docx")
    if not out_name.lower().endswith(".docx"):              # repli de la route —
        out_name = f"projet_{_today_mtl().isoformat()}.docx"  # _validate_file refuserait sinon

    doc, errors = upload_document(
        dossier_id=dossier_id,
        dossier_file_number=dossier["file_number"],
        file_stream=io.BytesIO(filled),
        filename=out_name,
        file_size=len(filled),
        metadata={
            "category": template.get("category", "autre"),
            "folder_id": (folder or {}).get("id"),
            "display_name": display,
            "genere_depuis": (
                f"Généré depuis le gabarit «{template['name']}» "
                f"v{template.get('version', 1)} — connecteur MCP"
            ),
            "tags": ["gabarit", "mcp"],
        },
        user_id=_storage_user_id(),                         # §7, B1
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    ...
```

Aucune normalisation du contenu n'est nécessaire :
`docx_fill._normalize_newlines` (fonction de module, pas attribut de `fill_docx`)
s'en charge à l'intérieur. En revanche, **avertir** — sans corriger — quand un
bloc de voie `values` commence par un motif de numérotation (`^\s*\d+[.)]\s`).
C'est l'erreur que le modèle commettra le plus souvent : numéroter lui-même
par-dessus la numérotation Word du gabarit. Avertir plutôt que réécrire : un
gabarit sans numérotation propre peut légitimement attendre des numéros dans le
texte.

**Pas de `bump_ctag`** : `documents` n'est pas exposée en DAV — le dépôt le dit
déjà à `models/document.py` (`record_analyse` : « *Aucun `bump_ctag` :
`documents` n'est pas exposée en DAV* »).

---

## 7. Points de blocage vérifiés

### B1 — L'identifiant Storage. *Le seul vrai blocage.*

`upload_document(..., user_id)` construit
`users/{user_id}/dossiers/{dossier_id}/documents/{document_id}/{filename assaini}`.
Les routes le passent par `session.get("user_id", "unknown")`, la session étant
posée par `auth.py` (`session["user_id"] = decoded["uid"]`). **Aucun handler MCP
n'a de session** — et depuis la phase N les mêmes handlers tournent aussi
in-process dans le service `chat`.

| Voie | Mécanique | Réserve |
|---|---|---|
| **(a) Résolution mémoïsée** *(recommandée)* | `firebase_admin.auth.get_user_by_email(Config.AUTHORIZED_USER_EMAIL).uid`, mis en cache au niveau module | Un appel Auth au premier remplissage par instance ; échec → refus français explicite |
| (b) Variable de configuration | `STORAGE_USER_ID` dans `app.yaml` | Un uid recopié à la main ; s'il diverge, les documents partent dans un préfixe orphelin que rien ne signale |
| (c) Dérivation depuis un document existant du dossier | lire le `storage_path` d'un document du même dossier | Échoue sur un dossier vide, c'est-à-dire précisément le cas d'un premier projet |

La voie (a) tient à l'architecture : *un seul utilisateur autorisé* (règle 1),
donc l'uid est déterministe. **Deux réserves.** D'abord, vérifier que le SDK Admin
est initialisé dans le service `chat` comme dans `default`. Ensuite, noter que le
précédent de la route a la posture **opposée** à celle qu'on recommande ici : son
`"unknown"` par défaut écrit sous `users/unknown/…` plutôt que d'échouer. Voir
§13, L2 — c'est un défaut latent, pas un modèle à suivre.

### B2 — Le validateur ne sait pas exprimer un dictionnaire à clés libres

Vérifié dans `mcp/tools.validate_args` : mots-clés reconnus = `type`,
`properties`, `required`, `enum`, `minimum`, `maximum`, `maxLength`, `minLength`,
`items` (récursif sur `_validate_value`, donc un objet dans un tableau est bien
validé), `anyOf`, `additionalProperties: false`. **`maxItems` / `minItems` ne sont
pas implémentés.** Fonde D4 et la garde §6.2.9.

### B3 — Plafonds et cap de requête

`_enforce_request_size` cape toute route à 1 Mo ; l'exemption de 10 Mo vise le
téléversement de gabarits, pas `/mcp`. `BLOCS_TOTAL_MAX_CHARS = 60 000` laisse une
marge d'un ordre de grandeur. *(Cap non vérifié dans le code — §14.)*

### B4 — Numérotation : la voie `values` est la bonne

En expansion de bloc, la voie `values` clone le `<w:p>` hôte verbatim ; son `numPr`
survit sans qu'aucune fusion n'intervienne. La voie riche, elle, passe par
`markdown_docx`, qui rend les listes ordonnées en **numéros calculés** —
« *printable, not Word-restyleable* ». Pour des allégations on veut la numérotation
de Word : donc voie `values`, paragraphes séparés par une ligne blanche. C'est pour
cela que `markdown` est faux par défaut. **Et voir §13, L1** : dans un hôte
numéroté, la voie riche fait bien pire que des numéros calculés.

### B5 — `rich_values` : contraintes d'hôte

Le placeholder doit être **seul** dans son paragraphe ; jamais en en-tête ni en
pied (il y reste verbatim) ; un hôte portant `<w:sectPr>` est refusé pour ne pas
détruire le saut de section du papier à en-tête. Toute violation dégrade
silencieusement vers la voie `values` — d'où B9.

### B6 — `documents` : collection déjà écrite, docstring périmée

`record_document_analysis` (∈ `WRITE_TOOLS`) écrit **déjà** la collection
`documents` via `models.document.record_analyse`. La liste des collections
mutables de la docstring de `handlers.py` ne la mentionne pas : c'est une
péremption **existante**, que H.4 doit corriger au passage — mais H.4 n'inaugure
rien ici. Ce qu'elle inaugure vraiment : la **première écriture MCP qui touche
Firebase Storage** (tout le reste est Firestore). C'est ce qui fait de B1 un
blocage.

### B7 — Dérive `_firm_dict` : hygiène, pas correctif

`routes/doc_templates._firm_dict()` duplique `utils/cabinet.cabinet_dict()` et lui
manque `telecopieur`. La duplication est réelle et vaut d'être supprimée. Mais
**sans effet observable aujourd'hui** : le catalogue ne comporte que sept entrées
`cabinet.*` (`nom`, `adresse_civique`, `ville`, `province`, `code_postal`,
`telephone`, `courriel`) et **pas** `cabinet.telecopieur`. Un gabarit qui
l'écrirait tomberait en passthrough et resterait littéralement
`{{cabinet.telecopieur}}` — sur les deux voies. Une clé supplémentaire dans le
dict `firm` est inerte : `resolve_values` n'itère que sur des noms ayant une
entrée au `CATALOG`. Voir §13, L4 pour le manque réel.

### B8 — Le libellé de consentement

`athena:write` n'est accordé que si l'utilisateur coche « Autoriser les écritures
(création seulement) », dont le libellé **énumère les familles d'écriture** :
l'écran est la seule description humainement lisible de ce que le jeton peut
faire. Or la portée est gelée à l'émission et recopiée verbatim aux
rafraîchissements. Conséquence : un jeton déjà accordé gagnerait `fill_gabarit`
sans nouvel écran. Décision requise — mettre à jour le libellé et révoquer les
jetons pour forcer un nouveau consentement, ou l'assumer. La création d'un projet
dans un dossier reste additive et non destructive, donc l'assumer est défendable ;
mais c'est un choix, pas un détail d'implémentation. *(Mécanique non vérifiée dans
le code — §14.)*

### B9 — `blocs_demoted` exige de toucher le moteur

`_apply_rich` rend bien `(xml, demoted)`, mais `_fill_target_xml` **consomme
`demoted` en interne** (il refile les noms dégradés par la voie `values`) et
`fill_docx` ne rend que des `bytes`. Il n'existe donc **aucun canal** vers
l'appelant. Deux issues, à trancher :

- **(i) Renoncer** à `blocs_demoted`. La sortie ne dit rien de la dégradation ; le
  modèle peut annoncer une mise en forme que le document n'a pas. Coût nul,
  honnêteté moindre.
- **(ii) Ouvrir un canal** *(recommandé)* : ajouter à `fill_docx` un paramètre
  facultatif `report: dict | None = None`, rempli en place, ignoré par tous les
  appelants existants. Une dizaine de lignes, aucun changement de signature de
  retour, `test_docx_fill` gagne un cas au lieu d'en changer. **Mais §12 doit
  alors reconnaître que le sous-système 3 est touché** — le devis ne peut pas à la
  fois promettre `blocs_demoted` et jurer de ne pas toucher `docx_fill.py`.

---

## 8. Sécurité et confidentialité

Rien de nouveau **en nature**, deux points à poser consciemment.

Le contenu rédigé transite par claude.ai sur la voie du connecteur externe — même
arbitrage que celui déjà accepté pour `get_document_text`, dont l'écran de
consentement nomme le fait, à la différence près que le sens est inversé : ici le
texte *part* vers le service au lieu d'en revenir. Sur la voie interne (clavardage
sur Vertex, projet GCP du cabinet), la question ne se pose pas.

Le résultat est un **projet** : déposé dans « Projets », jamais signifié, jamais
transmis, jamais signé. La description de l'outil le dit, et le nom généré le porte
déjà (`… - Projet Nom du gabarit`).

Aucune donnée nouvelle ne sort : `get_gabarit` ne renvoie pas de valeurs (D5), et
aucune sortie ne porte d'URL signée ni de `storage_path` (D8).

---

## 9. Observabilité

Le vocabulaire existe : `log_template_event("document_generated" |
"generation_failed", …)`. Y ajouter `source="mcp"` et le `dossier_id`. **Jamais de
valeur de champ** — la règle §11 de la spec H tient telle quelle, et les blocs sont
du contenu privilégié : noms de placeholders, compteurs et identifiants seulement.

Span `template.fill` déjà posé par la route ; le handler pose le sien avec
`add_attributes(template_id=…, bloc_count=…, total_chars=…)` — des compteurs, pas
des textes.

`athena/OBSERVABILITY.md` : enregistrer le champ `source` et les nouveaux motifs
d'échec (`kind_refused`, `bloc_unknown`, `bloc_conflict`,
`storage_user_unresolved`).

---

## 10. Tests

| Fichier | Contenu |
|---|---|
| `tests/test_mcp_output_schemas.py` | Fixtures pour les trois outils — obligatoire : le test fait tourner les **vrais** handlers et éprouve la charge réelle contre l'`outputSchema`. Couvrir la branche `dossier: null` de `get_gabarit`, et l'absence de `blocs_demoted` si B9(i). |
| `tests/test_mcp_tools.py` | Registre, `outputSchema` déclaré, `fill_gabarit ∈ WRITE_TOOLS` et `∉ EDIT_TOOLS`, `description` sur chaque propriété. **Épinglage des littéraux** : `_GABARIT_CATEGORIES == list(models.doc_template.VALID_CATEGORIES)`, idem `kind` — seul garde-fou contre la dérive imposée par l'interdiction d'importer `models.*`. |
| `tests/test_gabarit_mcp.py` *(nouveau)* | Les dix gardes de §6.2, une par test, message français vérifié. Équivalence des créneaux route/handler si §6.1(b). Bout en bout : gabarit de test → `fill_gabarit` → l'archive s'ouvre, les blocs sont en place, les numéros sont hérités. |
| `tests/test_docx_fill.py` | Étendre : (1) une valeur multi-paragraphes dans un hôte portant `numPr` produit *n* paragraphes numérotés — le comportement existe, il n'est pas épinglé ; (2) le `report` de B9(ii) si retenu ; (3) **la régression de L1** (voie riche dans un hôte numéroté). |

---

## 11. Documentation à mettre à jour

- **`CLAUDE.md`** — le compte d'outils figure aux lignes **15, 56, 298, 318, 1840,
  2224 et 3209** (sept endroits, pas trois), et **la docstring de
  `mcp/handlers.py` en porte un huitième**. Il est déjà faux (§1) : les corriger
  toutes à 56 / 31 / 25, pas seulement celles qu'on croise. Puis : familles
  d'écriture ; collections mutables + `documents` ; Directory Structure si
  `utils/gabarit_context.py` naît ; fiche `doc_templates.py` du Routes Reference.
- **`GABARITS_PLACEHOLDERS.md`** — une section « voie MCP » : quels noms sont
  fournissables, la règle de la ligne blanche (et le retour de ligne isolé qui
  devient une espace), le contraste numérotation Word / numéros calculés. Le
  document se déclare index lisible de ce que le moteur supporte ; il doit rester
  vrai.
- **`athena/OBSERVABILITY.md`** — §9.
- **`SECURITY.md` / `DEPLOYMENT.md`** — seulement si B8 conclut à un changement de
  libellé de consentement.

---

## 12. Change Impact Assessment

| Sous-système | Touché | Ce qu'il faut vérifier |
|---|---|---|
| 1. Connecteur MCP | **oui, centralement** | Schémas et handlers déplacés ensemble ; `outputSchema` déclaré et conforme sur charge réelle ; `description` par propriété ; `required` limité aux clés toujours présentes ; jamais `additionalProperties: false` en sortie ; `WRITE_TOOLS` gagne le membre ; `run_write` sur le chemin ; portée éprouvée dans `endpoint._tools_call` et non dans le handler ; les deux coupe-circuits intacts ; **parité clavardage** — `chat/registry.py` référence `TOOLS` par identité, donc l'outil arrive aussi côté clavardage sans code : le vouloir, ou l'exclure explicitement |
| 2. Synchronisation DavX5 | **non** | `documents` n'est pas exposée en DAV (déjà écrit dans `models/document.py`) ; aucun CTag |
| 3. Génération / appariement des champs | **oui si B9(ii) ou si L1 est corrigé** | Le devis n'ajoute qu'un appelant, **sauf** sur deux points : le canal `report` de `fill_docx` (B9) et le `numPr` de `_para_ppr` (L1). Les deux sont chirurgicaux, les deux doivent être assumés ici plutôt que découverts en revue. `test_docx_fill` / `test_template_fields` gagnent des cas ; aucun cas existant ne doit changer |
| 4. Observabilité | **oui** | Helpers typés seulement ; aucune valeur de champ ; nouveaux événements enregistrés |
| 5. Sécurité / défense de bordure | **oui, à la marge** | B8 (consentement) ; aucune exemption CSRF nouvelle ; aucune exemption de taille nouvelle |
| 6. Chaîne d'actifs front | non | Aucune vue |
| 7. Index Firestore | **non** | `list_templates` : un seul `order_by("name")` sur collection bornée ; filtre `kind` en Python |

---

## 13. Défauts latents découverts en rédigeant ce devis

Aucun n'est causé par H.4 ; tous la concernent.

**L1 — Double numérotation par la voie riche.** `_apply_rich` sème `base_ppr` avec
le `<w:pPr>` **de l'hôte**, et `_HtmlToOoxml._para_ppr` ne retire que `spacing`,
`ind` et `pBdr` — **jamais `numPr`**, qui figure pourtant dans `_PPR_ORDER` et est
donc réémis. Conséquence : si `{{FAITS}}` est seul dans un paragraphe de liste
numérotée et que le bloc part en `markdown: true`, **chaque** paragraphe produit
(titres, items, citations, jusqu'aux paragraphes de cellules) hérite du `numPr` de
l'hôte et est numéroté par Word — *en plus* du glyphe calculé inséré comme texte.
Le défaut n'a jamais surfacé parce que `{{note.contenu}}`, seul consommateur
actuel, vit dans un paragraphe ordinaire. H.4 le rendrait atteignable. Correctif
proposé : ajouter `numPr` à la liste `strip` de `_para_ppr` (la voie riche gère sa
propre numérotation ; hériter de celle de l'hôte n'a de sens dans aucun cas).

**L2 — `users/unknown/…`.** Les deux appels de `upload_document` dans
`routes/doc_templates.py` passent `session.get("user_id", "unknown")`. Une session
amputée écrit sous un préfixe Storage orphelin plutôt que d'échouer, et rien ne le
signale. À corriger en même temps que B1, faute de quoi les deux voies auront des
postures opposées sur la même erreur.

**L3 — Comptes d'outils périmés.** `CLAUDE.md` (sept occurrences) et la docstring
de `handlers.py` annoncent 52 / 29 / 23 ; le code porte 53 / 29 / 24 depuis
`record_document_analysis`. La docstring de `handlers.py` omet aussi `documents`
de sa liste de collections mutables (B6).

**L4 — `cabinet.telecopieur` inatteignable.** `Config.FIRM_FAX` existe,
`cabinet_dict()` expose `telecopieur`, mais le `CATALOG` n'a pas l'entrée : un
gabarit ne peut pas imprimer le numéro de télécopieur du cabinet. Une ligne à
ajouter au catalogue si le besoin existe — sinon, retirer `telecopieur` de
`cabinet_dict()` pour que la clé morte cesse de suggérer le contraire.

---

## 14. Ce qui n'a pas pu être vérifié

Faute d'avoir lu ces fichiers, les affirmations suivantes reposent sur `CLAUDE.md`
ou sur des indices indirects, et doivent être re-vérifiées avant de coder :

- **B3** — cap de 1 Mo sur `/mcp`, exemption de 10 Mo au téléversement
  (`security.py`, `_enforce_request_size`).
- **§4** — « `models/__init__.py` instancie `firestore.Client()` au chargement »
  (corroboré par le commentaire de `tools.py`, pas lu à la source).
- **B8** — libellé de consentement, gel de la portée à l'émission, recopie aux
  rafraîchissements (`mcp/oauth.py` et gabarits de consentement).
- **D3** — `/factures/<id>/note-docx`, `/notes/<id>/gabarit-docx`,
  `utils/invoice_docx.py`, `utils/note_docx.py`.
- **§12** — « `chat/registry.py` référence `TOOLS` par identité » (`chat/`).
- **§6.3** — `get_or_create_folder` idempotent, rendant un dict à clé `id`
  (`models/folder.py`).
- **B1** — SDK Admin initialisé dans le service `chat` (`chat.yaml`, `chat/`).
- **§9** — signatures de `log_template_event`, `add_attributes`, `span`.
- **§10** — contenu réel des trois fichiers de test cités.

---

## 15. Questions ouvertes — à trancher avant de coder

1. **§6.1 (a) ou (b)** — hoister le contexte de gabarit en `utils/`, ou dupliquer
   avec un test d'équivalence. Recommandation : (a).
2. **B1** — voie de résolution de l'uid Storage. Recommandation : (a), mémoïsée,
   et corriger L2 dans le même mouvement.
3. **B9 (i) ou (ii)** — renoncer à signaler la dégradation markdown, ou ouvrir un
   canal `report` dans `fill_docx`. Recommandation : (ii).
4. **L1** — corriger le `numPr` de la voie riche maintenant (recommandé : le
   défaut devient atteignable avec H.4) ou séparément.
5. **B8** — consentement : révoquer et re-consentir, ou assumer l'élargissement
   silencieux d'un jeton déjà émis.
6. **Parité clavardage** — l'outil doit-il arriver sur les deux surfaces
   (comportement par défaut, décision D3/D8 de la phase N) ou être réservé au
   connecteur externe ? Le clavardage interne a d'autres moyens de produire un
   document ; le connecteur externe n'en a aucun.
7. **Nom de l'outil d'écriture** — `fill_gabarit` (retenu ici, aligné sur
   `fill_docx`) ou `generate_document` (plus explicite pour un modèle, mais
   collision de vocabulaire avec `list_documents` / `get_document_text`).
8. **Un quatrième outil ?** — `fill_gabarit_from_draft(draft_id, gabarit_id, …)`
   fermerait la boucle `save_draft` → révision → document. Hors périmètre ici : il
   faudrait décider comment un brouillon markdown se découpe en blocs, ce qui est
   une question de conception à part entière.
