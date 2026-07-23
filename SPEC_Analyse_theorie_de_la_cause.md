# Spécification — Feuille « Analyse » (Théorie de la cause) — Pallas Athéna

**Destinataire :** Claude Code
**Cible :** dépôt `jpoirierlavoie/Athena-Pallas`, branche `main`
**Statut :** prêt à implémenter — lire d'abord la section 1 (structure de navigation réelle)
**Révision :** (1) **note unique** ; (2) feuille **« Analyse »** sous le groupe **« Aperçu »** (l'ancienne « Élaboré », **renommée** ; plus de feuille « Élaboré ») ; (3) note **partiellement isolée** — masquée de la feuille « Notes » (et de la vue `/notes`), **lisible en lecture seule via le connecteur MCP**, et **toujours synchronisée dans DavX5**. *(L'entrée-lien « Analyse » autrefois envisagée dans « Fichiers » est retirée.)*

---

## 0. Résumé exécutif

Ajouter, au niveau de chaque dossier, une **feuille « Analyse »** sous le **groupe « Aperçu »** du hub. Elle héberge une **« théorie de la cause »** structurée en **8 blocs (A → H)** — le *Gabarit B, version complète et stratégique* de la méthode d'élaboration de la théorie d'une cause (École du Barreau du Québec).

Contraintes fixées par le praticien :

1. **Une seule note** contient **tous les blocs** (les 8 blocs A→H sont des sections Markdown dans le champ `content`).
2. Cette note est exposée à DavX5 comme une **note pure sans date** (un `VJOURNAL` **sans `DTSTART`**) → une *Note*, non un *Journal*, dans jtx Board.
3. Création **paresseuse**, à la première ouverture de la feuille, via un bouton **« Ajouter une théorie de la cause »** (idempotent, préremplie du gabarit complet).
4. **Isolement partiel** : la note **n'apparaît pas** dans la feuille « Notes » ni dans la vue `/notes` ; côté application, elle n'est atteignable **que par la feuille « Analyse »**. En revanche, elle est **lisible en lecture seule par le connecteur MCP** (`list_notes`/`get_note` la renvoient ; les outils d'écriture MCP ne peuvent pas la modifier) et **reste exposée à DavX5** (note sans date).
5. La feuille « Analyse » **remplace** l'ancienne « Élaboré » (même emplacement, sous « Aperçu »). Aucune entrée dans « Fichiers ».

Approche : **réutiliser la collection `notes`** (donc le pipeline `VJOURNAL` / collection DAV par dossier / CTag), en l'étendant de deux booléens (`dateless`, `is_analyse`). L'exclusion des vues « Notes » se fait par un paramètre `include_analyse` sur les fonctions de liste (défaut : exclut) ; **exceptions impératives : les chemins DAV et l'outil MCP `list_notes` doivent inclure la note** (le MCP en lecture seule).

---

## 1. Structure de navigation réelle (À LIRE EN PREMIER)

Le hub du dossier utilise, depuis **fin juillet 2026**, une **navigation à deux paliers** (`athena/templates/dossiers/_tab_nav.html`) :

- **4 groupes** : **Aperçu**, **Finances**, **Agenda**, **Documents**.
- Feuilles (sous-onglets) par groupe :
  - **Aperçu** → `apercu` (feuille volontairement vide ; les cartes Sommaire/Recours/Mandat sont au-dessus de la barre, dans `detail.html`). **← « Analyse » s'ajoute ici.**
  - **Finances** → `temps` (**défaut**) · `facturation` · `fideicommis`.
  - **Agenda** → `audiences` (« Calendrier ») · `taches` · `protocole`.
  - **Documents** → `documents` (« Fichiers ») · `notes` (« Notes »).
- Chargeur HTMX : `/dossiers/<id>/tab/<leaf>` → `#tab-content`, URL poussée `?tab=<leaf>`. Sélection Alpine (`activeGroup`/`activeLeaf`).
- `routes/dossiers.py` : `_VALID_TABS`, `_LEAF_GROUP` (feuille → groupe, **miroir** de `groups` dans `_tab_nav.html`), `_LEGACY_TABS`.

**Conséquence :** **« Analyse » = une feuille sous le groupe « Aperçu »**. Le groupe « Aperçu » gagne une sous-rangée `[Aperçu, Analyse]`. (C'est l'ancienne feuille « Élaboré », simplement renommée « Analyse ».)

⚠️ **S'aligner sur le code vivant.** Lire `_tab_nav.html`, `routes/dossiers.py` (`_VALID_TABS`, `_LEAF_GROUP`, `dossier_tab`), `models/note.py`, `routes/notes.py`, `dav/dossier_collections.py`, `dav/sync.py`, ainsi que la couche `mcp/` (outils de notes) avant d'éditer.

---

## 2. Architecture existante (points d'appui)

**Pile.** Python 3.13 / Flask 3.1 / Firestore natif / Firebase Storage. Frontend Jinja2 + HTMX + Alpine.js + Tailwind **précompilé et vendu**. **Aucun CDN.** CSP **appliquée** (nonce par requête ; pas de `<script>` inline sans `nonce="{{ csp_nonce }}"`).

**Notes (`models/note.py`).** Collection `notes`. Champs : `id`, `dossier_id`, `dossier_file_number`, `dossier_title`, `title`, `content` (Markdown, `CONTENT_MAX_LENGTH = 100_000`), `category`, `pinned`, `vjournal_uid`, `created_at`, `updated_at`, `etag`. `note_to_vjournal` **ajoute toujours `DTSTART`** (→ *Journal daté*). `CREATED`/`DTSTAMP` **obligatoires** (piège `icalobject.created` NOT NULL de jtx). `vjournal_to_note` fait l'inverse. `content` : 100 000 caractères — le gabarit complet y tient largement.

**Fonctions de liste (à étendre, §3.4) :** `list_notes(dossier_id=…, category=…, search=…)` et le chemin borné `list_notes_recent(dossier_id=…)`.

**Feuille « Notes » (`_tab_notes.html`) + chargeur.** `dossier_tab` (`tab_name=="notes"`) fait `ctx["notes"] = list_notes(dossier_id=dossier_id)`. → **doit désormais exclure la note « Analyse »** (§3.4).

**Collection DAV par dossier (`dav/dossier_collections.py`).** `_add_note_resource` sérialise via `note_to_vjournal` ; la liste vient de `list_notes(dossier_id=…)`. → **doit désormais inclure la note « Analyse »** (§4.3). Collections enfants directs de `/dav/` (découverte PROPFIND Depth:1).

**CTag (`dav/sync.py`).** Collection dossier = `"dossier:{dossierId}"`. **Bump au niveau des ROUTES.** `notes` CRUD → `bump_ctag(f"dossier:{dossier_id}")`.

**Connecteur MCP (`mcp/`).** Outils de notes en lecture (`list_notes`, `get_note`) — et, sous le scope `athena:write`, `create_note`/`append_to_note`. → la note « Analyse » est **lisible (lecture seule)** : `list_notes`/`get_note` **l'incluent** ; `append_to_note` **la refuse** (aucune écriture) — voir §3.4, §8.

---

## 3. Modèle de données — extensions de `note`

Fichier : `athena/models/note.py`.

### 3.1 Nouveaux champs (dans `_default_doc()`)

```python
"dateless": False,     # True → note pure : note_to_vjournal OMET DTSTART
"is_analyse": False,   # True → cette note EST la théorie de la cause (feuille « Analyse »)
```

- `dateless` — pilote **uniquement** l'émission de `DTSTART`. `created_at` reste renseigné.
- `is_analyse` — marque **l'unique** note « Analyse » d'un dossier : sert à la retrouver, à l'isoler des listes, et à garantir l'idempotence.

### 3.2 Contenu-semence et titre

```python
ANALYSE_TITLE = "Théorie de la cause"   # SUMMARY de la note (affiché tel quel dans jtx Board)
_ANALYSE_SEED = """…"""                  # cf. Annexe A — Markdown des 8 blocs A→H
```
*(Le libellé de l'onglet est « Analyse » ; le titre de la note reste « Théorie de la cause ». Changer `ANALYSE_TITLE` si un autre intitulé est voulu.)*

**Catégorie :** réutiliser **`"stratégie"`** (déjà dans `VALID_CATEGORIES`).

### 3.3 Fonctions dédiées

```python
def has_analyse(dossier_id: str) -> bool: ...

def get_analyse_note(dossier_id: str) -> dict | None:
    """L'unique note is_analyse du dossier, sinon None.
    RECOMMANDÉ : list_notes(dossier_id=..., include_analyse=True) puis retenir
    en Python la note is_analyse — aucun index composite (§7)."""

def create_analyse_note(dossier_id: str) -> tuple[dict | None, list[str]]:
    """Crée l'unique note préremplie (IDEMPOTENT).
    - Si has_analyse(dossier_id) → renvoyer l'existante, ne rien créer.
    - Sinon : create_note({
          "dossier_id": dossier_id, "title": ANALYSE_TITLE,
          "content": _ANALYSE_SEED, "category": "stratégie", "pinned": False,
          "dateless": True, "is_analyse": True,
      })
    - NE PAS bumper le CTag ici : le bump appartient à la ROUTE (§5)."""
```

> **Piège d'édition (`update_note`).** Le formulaire de note standard (§5.3) ignore `dateless`/`is_analyse`. `update_note` **doit faire une mise à jour partielle** (merge) : `dateless`, `is_analyse` **et** `created_at` **survivent**. Ne jamais écraser `created_at` à `None`, ni remettre `dateless`/`is_analyse` à `False`.

### 3.4 Visibilité — paramètre `include_analyse` (vues « Notes », MCP, DAV)

Étendre les fonctions de liste d'un paramètre **`include_analyse: bool = False`**. **Défaut = `False` = exclut** la note « Analyse » (pour les vues « Notes »). Implémentation **en Python** (filtrer `n.get("is_analyse")` après lecture) → **aucun index Firestore** (§7). Les appelants DAV et l'outil MCP `list_notes` passent **`True`**.

```python
def list_notes(dossier_id=None, category=None, search=None,
               include_analyse=False, ...):
    notes = [ ... ]  # requête existante
    if not include_analyse:
        notes = [n for n in notes if not n.get("is_analyse")]
    return notes

def list_notes_recent(dossier_id=None, include_analyse=False):
    ...  # même exclusion
```

**Contrat d'appel — à respecter partout (zone à échec silencieux) :**

| Appelant | `include_analyse` | Effet attendu |
|---|:--:|---|
| Liste `/notes/` + `list_notes_recent` (`routes/notes.py`) | `False` (défaut) | La note « Analyse » **n'apparaît pas** dans les notes. |
| Feuille « Notes » (`dossier_tab`, `tab_name=="notes"`) | `False` | Idem. |
| Outil MCP `list_notes` | **`True`** | **Incluse** dans la sortie MCP (lecture seule). |
| Outil MCP `get_note` | — | **Renvoie** la note (lecture seule). |
| Outil MCP `append_to_note` | — | **Refuse** l'écriture sur une note `is_analyse` (lecture seule). |
| **Collection DAV par dossier** (`dav/dossier_collections.py`) | **`True`** | **La note EST exposée à DavX5** — sinon elle cesse silencieusement de se synchroniser. |
| **`_sync_dossier_dav_visibility`** (fermeture/réouverture de dossier, `routes/dossiers.py`) | **`True`** | La note est **tombstonée/restaurée** proprement côté DavX5. |
| `get_analyse_note` (feuille « Analyse ») | `True` | Ciblage volontaire de la note. |

⚠️ **Le piège :** tout chemin **DAV** qui listerait les notes avec le défaut (`False`) ferait **disparaître** la note « Analyse » de DavX5 sans erreur. Les deux appels DAV ci-dessus — **et** l'outil MCP `list_notes` — **doivent** passer `include_analyse=True`. Inversement, les vues « Notes » / `/notes` gardent le défaut (exclusion).

---

## 4. Sérialisation DAV — la note « sans date »

Fichier : `athena/models/note.py`.

### 4.1 `note_to_vjournal` — omettre `DTSTART` quand `dateless`

```python
created = note.get("created_at")
if created and hasattr(created, "date") and not note.get("dateless"):
    journal.add("dtstart", created.date())
# CREATED + DTSTAMP restent émis inconditionnellement (piège jtx NOT NULL).
```
`dateless=True` → `VJOURNAL` **sans `DTSTART`** → **jtx Board = *Note***. Notes ordinaires inchangées (**non-régression**).

### 4.2 Fidélité de round-trip

```python
# note_to_vjournal :
if note.get("is_analyse"):
    journal.add("x-pallas-analyse", "true")
# vjournal_to_note :
a = component.get("x-pallas-analyse")
if a and str(a).lower() == "true":
    data["is_analyse"] = True
data["dateless"] = component.get("dtstart") is None
```
⚠️ **PUT depuis jtx** : préserver `created_at` et `is_analyse` déjà stockés (merge, §3.3).

### 4.3 Exposition DAV (inclusion impérative) + CTag

- La collection DAV par dossier **doit** appeler `list_notes(dossier_id=..., include_analyse=True)` (modifier l'appel dans `dav/dossier_collections.py`). De même pour `_sync_dossier_dav_visibility`. Voir le contrat en §3.4.
- **CTag** : création (route §5.2) et édition DOIVENT `bump_ctag(f"dossier:{dossier_id}")` **au niveau route**. `/notes/<id>` bumpe déjà ; la route d'initialisation bumpe **une fois** après création.

### 4.4 Vérification DAV (obligatoire — échec silencieux)

**DavX5 échoue silencieusement — tester au `curl` avant le client.**
1. `PROPFIND` `/dav/dossier-{id}/` (Depth:1) → la ressource `.ics` de la note **apparaît bien** (piège d'exclusion écarté).
2. `GET` du `.ics` → **pas de `DTSTART`** ; `UID`, `DTSTAMP`, `CREATED`, `SUMMARY` (« Théorie de la cause »), `DESCRIPTION`, `X-PALLAS-ANALYSE` présents.
3. CTag `dossier:{id}` **modifié** après init.
4. **jtx Board** : la note sous **Notes** (sans date).

---

## 5. Backend — routes

### 5.1 Rendu de la feuille (chargeur standard)

La feuille `analyse` se charge via `GET /dossiers/<id>/tab/analyse`. Dans `dossier_tab` :
```python
if tab_name == "analyse":
    ctx["analyse_note"] = get_analyse_note(dossier_id)   # None ⇒ état vide (bouton)
```
et `templates["analyse"] = "dossiers/_tab_analyse.html"`.

### 5.2 Initialisation (le bouton)
```
POST /dossiers/<dossier_id>/analyse/init
```
- `@login_required`. **Idempotent** : `create_analyse_note(dossier_id)`.
- **Après création réelle : `bump_ctag(f"dossier:{dossier_id}")`** (pas sur double-clic).
- Réponse HTMX : renvoyer `dossiers/_tab_analyse.html` re-rendu → `hx-target="#tab-content"`, `hx-swap="innerHTML"`.
- CSRF : couvert par l'entête global `hx-headers` de `base.html`.

### 5.3 Édition — réutiliser les routes `notes`

La note **est** une note : réutiliser `GET /notes/<id>` (détail Markdown) et `GET/POST /notes/<id>/edit` (formulaire standard, une seule grande textarea). Ces routes fonctionnent **par identifiant** — l'exclusion des listes ne les gêne pas. `return_to` validé vers `?tab=analyse`. Rappels : `POST /notes/<id>` bumpe déjà le CTag ; **merge partiel de `update_note`** requis (§3.3).

> Détail d'ergonomie : le lien « Modifier » de la feuille « Analyse » pointe vers `/notes/<id>/edit`. La page d'édition standard reste accessible par identifiant même si la note est masquée des listes (souhaité).

### 5.4 Câblage de la feuille « Analyse » sous « Aperçu »

1. `routes/dossiers.py` — ajouter `"analyse"` à **`_VALID_TABS`**, mapper **`_LEAF_GROUP["analyse"] = "apercu"`**, et `templates["analyse"] = "dossiers/_tab_analyse.html"` ; charger `ctx["analyse_note"]` (§5.1).
2. `athena/templates/dossiers/_tab_nav.html` — ajouter la feuille `analyse` (libellé **« Analyse »**) **sous le groupe « Aperçu »**, dans `groups` (miroir de `_LEAF_GROUP`). Réutiliser les classes `.dossier-subnav`/`.dossier-subnav-wrap` (**pas de nouvelle classe → pas de recompilation CSS**).
3. Pas de route de rendu dédiée : passage par le chargeur `/dossiers/<id>/tab/analyse`.

*(Si une feuille `elabore` avait déjà été amorcée, la remplacer entièrement par `analyse` : slug, libellé, template, route d'init, champ `is_analyse`.)*

---

## 6. Frontend — `dossiers/_tab_analyse.html`

**État vide** (`analyse_note is None`) :

```html
<div class="bg-white rounded-xl border border-gray-200 p-8 text-center">
  <p class="text-sm text-gray-500 mb-4">Aucune théorie de la cause pour ce dossier.</p>
  <button hx-post="{{ url_for('dossiers.dossier_analyse_init', dossier_id=dossier.id) }}"
          hx-target="#tab-content" hx-swap="innerHTML"
          class="<classes de bouton primaire existantes>">
    Ajouter une théorie de la cause
  </button>
  <p class="text-xs text-gray-400 mt-3">
    Crée une note unique (les 8 blocs A→H), synchronisée dans DavX5 comme une note sans date.
  </p>
</div>
```

**État peuplé** :

```html
<div class="bg-white rounded-xl border border-gray-200 p-5">
  <div class="flex items-center justify-between mb-3">
    <h3 class="text-sm font-semibold text-gray-900">{{ analyse_note.title }}</h3>
    <a href="{{ url_for('notes.note_edit', note_id=analyse_note.id,
                        return_to=url_for('dossiers.dossier_detail', dossier_id=dossier.id, tab='analyse')) }}"
       class="text-xs text-indigo-600 hover:underline">Modifier</a>
  </div>
  {# Markdown rendu SUR LE CONTENU COMPLET seulement (jamais sur un aperçu) #}
  <div class="prose prose-sm max-w-none">{{ analyse_note.content | markdown }}</div>
</div>
```

- **Rappel `CLAUDE.md` : filtre `markdown` deux fois = rien** — l'appliquer une seule fois, sur le contenu complet.
- **Prérequis** : le contenu-semence (Annexe A) utilise **tableaux Markdown** + cases `☐` → vérifier l'extension **`tables`** du filtre `markdown` (sinon l'activer ou remplacer les tableaux par des listes).
- **CSP** : aucun script inline.

---

## 7. Index Firestore

- **Aucun index.** `get_analyse_note` et l'exclusion `include_analyse` opèrent **en Python** sur la liste par dossier déjà indexée. Éviter toute requête `.where("is_analyse", ...)` (risque d'index composite à déployer avant le code, sinon dégradation silencieuse).

---

## 8. Évaluation d'impact (« Change Impact Assessment » du CLAUDE.md)

| Sous-système | Touché ? | À vérifier |
|---|---|---|
| **1. Connecteur MCP** (`mcp/`) | **Oui.** La note « Analyse » est **lisible (lecture seule)** : `list_notes` l'inclut (`include_analyse=True`), `get_note` la renvoie ; `append_to_note` **refuse** l'écriture sur une note `is_analyse`. | Aucun secret/PII ; date-only via `mcp.tools.date_str` (la note est `dateless`) ; aucun nouvel index ; `test_mcp_*` au vert **et** des tests confirmant que la note est **lue** mais **non modifiable** via MCP. |
| **2. Sync DavX5** (`dav/` + sérialiseurs) | **Oui.** `note_to_vjournal` conditionne `DTSTART` ; `X-PALLAS-ANALYSE` ; **inclusion `include_analyse=True`** dans les 2 chemins DAV ; route d'init ; bump CTag. | `UID`/`DTSTAMP`/`CREATED` présents ; `DTSTART` absent ; notes datées inchangées ; **les 2 appels DAV passent `include_analyse=True`** ; bump CTag en route ; **test `curl`** (§4.4). |
| **3. Génération de gabarits** | **Non.** | — |
| **7. Index Firestore & invariants** | **Non** (tout en Python, §7). | — |
| **Documents / Firebase Storage** | **Non** (aucune entrée « Analyse » dans « Fichiers » ; le lien précédemment envisagé est retiré). | — |

**Zone d'attention n°1 :** le paramètre `include_analyse`. Vues « Notes » / `/notes` = **exclut** ; **DAV et MCP `list_notes` = inclut**. Un oubli d'`include_analyse=True` côté DAV retire silencieusement la note de la synchro ; un oubli d'exclusion côté vues « Notes » casse l'isolement visuel.
**Zone d'attention n°2 :** bump du CTag **au niveau route**.

---

## 9. Plan de test

- **Modèle** : `note_to_vjournal` omet `DTSTART` ssi `dateless` ; émet `X-PALLAS-ANALYSE` ; `CREATED`/`DTSTAMP`/`UID` présents. `vjournal_to_note` relit `is_analyse`, déduit `dateless`. `create_analyse_note` **idempotent** (une seule note `is_analyse`/dossier). Semence : titre « Théorie de la cause », 8 blocs A→H, catégorie « stratégie ».
- **Isolement (vues Notes)** : `list_notes`/`list_notes_recent` **excluent** la note par défaut, **l'incluent** avec `include_analyse=True`. La feuille « Notes » et la vue `/notes` ne la montrent pas.
- **MCP (lecture seule)** : `list_notes` (via `include_analyse=True`) et `get_note` **renvoient** la note ; `append_to_note` **refuse** l'écriture sur une note `is_analyse`.
- **Inclusion DAV** : la collection par dossier et `_sync_dossier_dav_visibility` **la listent** (via `include_analyse=True`).
- **Non-régression** : une note ordinaire porte **toujours** `DTSTART` et **apparaît** dans « Notes »/MCP.
- **Merge d'édition** : `update_note` préserve `dateless`/`is_analyse`/`created_at`.
- **Routes / nav** : `POST …/analyse/init` crée une note **et** bumpe le CTag ; 2e POST ne crée rien. `GET /dossiers/<id>/tab/analyse` rend le bouton si absente, la note sinon. La feuille « Analyse » est **sous le groupe Aperçu** (`_LEAF_GROUP["analyse"]=="apercu"`).
- **Édition par id** : `/notes/<id>/edit` fonctionne bien que la note soit masquée des listes.
- **DAV (`curl`)** : PROPFIND → ressource **présente** ; GET → pas de `DTSTART`, `UID/DTSTAMP/CREATED` présents ; CTag modifié après init.
- **jtx Board (manuel)** : la note sous **Notes** (sans date).

---

## 10. Déploiement / rollout

- **Aucune migration** : création paresseuse ; dossiers existants intacts tant que le bouton n'est pas pressé.
- **DavX5** : **pas** de retrait/re-ajout de compte (structure de collection inchangée).
- **CSS** : réutiliser les classes existantes → **aucune régénération** ; sinon suivre la recette du `CLAUDE.md`.

---

## 11. Récapitulatif des livrables de code

1. `athena/models/note.py` — champs `dateless`/`is_analyse` + défauts ; `ANALYSE_TITLE` + `_ANALYSE_SEED` (Annexe A) ; `has_analyse`, `get_analyse_note`, `create_analyse_note` ; **paramètre `include_analyse` sur `list_notes` et `list_notes_recent`** (exclusion Python) ; `note_to_vjournal` (DTSTART conditionnel + `X-PALLAS-ANALYSE`) ; `vjournal_to_note` ; merge partiel de `update_note`.
2. `athena/dav/dossier_collections.py` — l'appel de liste des notes passe **`include_analyse=True`**.
3. `athena/routes/dossiers.py` — `dossier_analyse_init` (POST, bump CTag) ; `dossier_tab` : branche `analyse` (charge `analyse_note`) ; `_VALID_TABS` + `_LEAF_GROUP["analyse"]="apercu"` + `templates["analyse"]` ; **`_sync_dossier_dav_visibility` passe `include_analyse=True`**.
4. `athena/routes/notes.py` — la liste et `list_notes_recent` gardent le défaut (exclusion) ; rien d'autre.
5. `athena/mcp/…` — outils de notes : `list_notes` **inclut** (`include_analyse=True`) et `get_note` renvoie la note (lecture seule) ; `append_to_note` **refuse** l'écriture sur une note `is_analyse`.
6. `athena/templates/dossiers/_tab_nav.html` — feuille « Analyse » sous le groupe « Aperçu ».
7. `athena/templates/dossiers/_tab_analyse.html` — état vide (bouton) + état peuplé (note rendue).
8. Tests : `tests/` — modèle, isolement (web + MCP), inclusion DAV, routes/nav, DAV (curl), non-régression.

---

## Annexe A — Contenu-semence de la note (`_ANALYSE_SEED`, Markdown)

> `content` de la note ; `title` = « Théorie de la cause » ; `dateless=True`, `is_analyse=True`. Zones à remplir = « … » ; *questions-repères* en italique. Cases `☐` et tableaux → extension `tables` (§6).

```markdown
# Théorie de la cause

*Dossier : … | Partie représentée : ☐ Demandeur ☐ Défendeur ☐ Mis en cause | Rédigé par : … | Date de l'analyse : …*

Outil de travail interne (méthode d'élaboration de la théorie d'une cause, version complète et stratégique). Les blocs F et G — forces/faiblesses et théorie adverse — n'ont pas vocation à être versés au dossier de la Cour.

---

## Bloc A — Identification et cadre procédural

### Parties et leur qualité

| Partie | Rôle | Qualité / capacité / intérêt (art. 85 C.p.c.) |
|---|---|---|
| … | … | … |

### Cadre procédural

Tribunal et compétence d'attribution : …
District (compétence territoriale) : …
Montant ou valeur en jeu : …
Voie procédurale envisagée : …

### Verrous préliminaires

- ☐ **Prescription** — délai applicable : … *(à défaut de délai particulier, 3 ans : art. 2925 C.c.Q.)* — point de départ : … — date pour agir : …
- ☐ Intérêt et qualité pour agir (art. 85 C.p.c.)
- ☐ Compétence (matière et territoire)
- ☐ Mise en demeure / avis préalable requis ou envoyé
- ☐ Autres conditions de recevabilité : …

*Questions-repères : le client a-t-il l'intérêt et la qualité requis ? Le recours est-il encore dans les délais ? Le bon tribunal est-il saisi ? Une démarche préalable est-elle exigée ?*

---

## Bloc B — Les faits

### Récit chronologique

…

### Cartographie des faits

| Fait | Générateur du droit ? | Admis / non contesté | Contesté (à prouver) | Défavorable |
|------|:---:|:---:|:---:|:---:|
| … | ☐ | ☐ | ☐ | ☐ |

### Faits défavorables à gérer

(comment les neutraliser ou les expliquer) …

### Faits manquants ou à investiguer

(documents, témoins, expertises à obtenir) …

*Questions-repères : quels faits font naître le droit invoqué ? Lesquels l'autre partie admettra-t-elle ? Quels faits me nuisent, et comment les aborder de front ? Que dois-je encore aller chercher ?*

---

## Bloc C — Le fondement juridique et ses éléments constitutifs

### Fondement(s) invoqué(s)

Cause d'action (ou, en défense, moyens opposés) : …
Sources : ☐ législation … ☐ jurisprudence … ☐ doctrine …

### Éléments constitutifs à réunir

*Exemple — responsabilité civile : faute, préjudice, lien de causalité (art. 1457 C.c.Q. extracontractuel ; art. 1458 C.c.Q. contractuel).*

| Élément constitutif | Fait(s) qui l'établit | Preuve disponible | Solide ? |
|---|---|---|:--:|
| … | … | … | ☐ |
| … | … | … | ☐ |
| … | … | … | ☐ |

### Moyens de défense / d'exception envisageables

(les miens et ceux de l'adversaire) …

*Questions-repères : ai-je isolé chacune des conditions que la loi exige ? Chaque condition est-elle appuyée par un fait et par une preuve ? Une seule condition non établie fait-elle échouer le recours ?*

---

## Bloc D — Qualification et syllogisme

**Majeure (la règle) :** …

**Mineure (les faits qualifiés) :** …

**Conclusion (l'application) :** …

### Qualification juridique retenue

(nature exacte du rapport ou de l'acte) …

*Questions-repères : chaque condition de la règle trouve-t-elle appui dans un fait ? Un fait vient-il contredire l'application de la règle ?*

---

## Bloc E — La stratégie de preuve

### Fardeau et norme

Fardeau de preuve — qui doit prouver quoi (art. 2803 C.c.Q.) : …
Norme applicable : prépondérance des probabilités (art. 2804 C.c.Q.), sauf exigence légale plus stricte : …

### Moyens de preuve

*Art. 2811 C.c.Q. : écrit, témoignage, présomption, aveu, présentation d'un élément matériel.*

| Élément / fait à prouver | Sur qui repose le fardeau | Moyen de preuve prévu | Source / pièce / témoin | Lacune |
|---|---|---|---|---|
| … | … | … | … | … |

*Questions-repères : pour chaque fait contesté, ai-je un moyen de preuve ? La preuve est-elle admissible et disponible ? Où sont mes trous de preuve, et comment les combler ? Quelle preuve l'adversaire opposera-t-il ?*

---

## Bloc F — Analyse critique

### Forces de ma position

- …

### Faiblesses et risques

- …

### Théorie adverse anticipée

(prétentions probables de la partie adverse — faits, fondement, preuve — et ma réponse à chacune)

| Prétention adverse anticipée | Ma réponse / parade |
|---|---|
| … | … |

*Questions-repères : si j'étais l'avocat de l'autre partie, quelle serait ma meilleure théorie ? Quel est le maillon le plus faible de ma cause ? Résiste-t-elle au contre-interrogatoire et au scénario adverse le plus favorable ?*

---

## Bloc G — La théorie de la cause (synthèse persuasive)

### Théorie factuelle

(le récit, cohérent et favorable, de ce qui s'est passé) …

### Théorie juridique

(le fondement de droit qui commande le résultat recherché) …

### Le thème

(l'idée-force, l'angle d'équité ou de bon sens qui donne au tribunal une raison de trancher en ma faveur) …

### Énoncé de la théorie (une à deux phrases)

> « … »

*Test de solidité : la théorie est-elle cohérente (sans contradiction interne), crédible (conforme au bon sens et à l'expérience), complète (elle absorbe même les faits défavorables) et simple (mémorable, exprimable en une phrase) ?*

---

## Bloc H — Conclusions recherchées et suites

### Conclusions recherchées

(remèdes précis, tels qu'ils devront être formulés à l'acte de procédure — clarté, précision, concision, ordre logique et numérotation : art. 99 C.p.c.)

1. …
2. …

### Objectifs réels du client

(et scénarios de règlement acceptables) …

### Prochaines étapes et échéancier

…

### Éléments encore à obtenir

(preuve, expertise, mandat, provision) …
```

---

## Annexe B — Ancrages législatifs cités (vérifiés)

Renvois figurant dans le contenu-semence, confirmés au texte officiel (Légis Québec) :

- **Art. 85 C.p.c.** — intérêt suffisant pour former une demande en justice.
- **Art. 99 C.p.c.** — contenu de l'acte de procédure : nature, objet, faits, conclusions ; clarté, précision, concision, ordre logique, numérotation.
- **Art. 2803 C.c.Q.** — fardeau de la preuve.
- **Art. 2804 C.c.Q.** — prépondérance des probabilités.
- **Art. 2811 C.c.Q.** — moyens de preuve (écrit, témoignage, présomption, aveu, élément matériel).
- **Art. 1457 / 1458 C.c.Q.** — responsabilité extracontractuelle / contractuelle (exemple d'éléments constitutifs).
- **Art. 2925 C.c.Q.** — prescription de droit commun de 3 ans (à défaut de délai particulier).

> Ancrages **par défaut** du gabarit (p. ex. la prescription de 3 ans cède devant tout délai particulier) : ils illustrent la méthode et doivent être validés selon la matière de chaque dossier.
