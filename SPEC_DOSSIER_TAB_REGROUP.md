# SPEC — Dossier detail: two-level tab navigation + « Agenda » → « Calendrier » rename

Working name: **Phase K (tentative — assign the real letter at merge)**
Target: **Claude Code**, executing against the live `athena/` repo.
Reference: `CLAUDE.md` is the architectural source of truth. **The dossier tab set has evolved since `CLAUDE.md` was last written** — inventory the live templates/routes first and reconcile names against the taxonomy below rather than assuming exact current filenames.

---

## 1. Goal

The dossier detail hub (`/dossiers/<id>`) currently renders a single horizontal row of tabs that overflows on a 375 px viewport. Restructure it into a **two-level navigation**:

- **Top row** — 4 group tabs, always visible, fixed (non-scrolling).
- **Sub-row** — the leaf tabs of the active group, shown only for multi-leaf groups, **horizontally scrollable** (handles long labels like « Temps & Déboursés »).

Alongside the restructure: surface the (already-built) trust-accounting module as a **Fidéicommis** leaf under Finances, empty the **Aperçu** leaf, and rename the events/hearings feature from **« Agenda »** to **« Calendrier »** — freeing the word « Agenda » to name the new top-level group.

---

## 2. Assumptions & decisions — confirm before building

If any of these is wrong, stop and flag it; each is reversible but shapes the whole spec.

1. **Fidéicommis surfaces the existing trust-accounting module.** The trust-accounting subsystem is already built (post-dating `CLAUDE.md`, which still says it's deferred — that line is stale). This phase adds a **Fidéicommis** leaf under Finances that renders the module's **dossier-scoped** view; it does **not** modify the trust-accounting data model or business logic. Claude Code must discover the module's current wiring — its model, its per-dossier query, and whether a dossier-scoped fragment already exists or only a standalone view — and integrate accordingly (see §7.1 and §8).
2. **Aperçu becomes empty.** The tab stays as a navigation entry but renders a neutral empty state. The previous overview/summary content is retired (git preserves it). If the overview partial computed data server-side in the dossier route solely to feed itself, that computation can be dropped.
3. **Agenda → Calendrier is a label-only rename.** The route prefix `/audiences`, the `hearings` blueprint, all `url_for('hearings.*')`, and the DAV `/dav/calendar/` layer are **unchanged**. Only user-facing French strings change.
4. **« Honoraires » is a dossier-tab label change only.** The standalone invoicing page (`/factures`) keeps its current name; the user did not ask to rename it.
5. **Internal tab slugs stay stable.** Labels change; slugs do not. `facturation` still backs « Honoraires », `audiences` still backs « Calendrier ». The only new slug is `fideicommis`. This keeps existing `?tab=` deep links and internal references intact.
6. **No Notes tab.** Notes remain at the standalone `/notes` view (the previously-proposed dossier Notes tab is **not** part of this taxonomy).

Optional, not in scope unless requested: default landing leaf. This spec keeps **Aperçu** as the default landing tab (current behavior). If a blank first screen is undesirable, changing the default to `temps` (Finances) is a one-line change — noted, not done.

---

## 3. Target tab taxonomy

| Top group | Leaf label (French) | Leaf slug (URL, unchanged unless noted) | Status | Backing content |
|---|---|---|---|---|
| **Aperçu** | *(none — direct leaf)* | `apercu` | Emptied | New empty-state placeholder |
| **Finances** | Temps & Déboursés | `temps` | Relabel | Existing time+expenses tab |
| | Honoraires | `facturation` | Relabel | Existing invoicing tab |
| | Fidéicommis | `fideicommis` | **New leaf, existing content** | Existing trust-accounting module (dossier-scoped view) |
| **Agenda** | Calendrier | `audiences` | Relabel (was « Agenda ») | Existing hearings/events tab |
| | Tâches | `taches` | Unchanged | Existing tasks tab |
| | Protocole | `protocole` | Unchanged | Existing protocol tab |
| **Documents** | *(none — direct leaf)* | `documents` | Unchanged | Existing documents tab |

Leaf slug set after this change: `{apercu, temps, facturation, fideicommis, audiences, taches, protocole, documents}` (8 leaves; net +1 = `fideicommis`).

The `fideicommis` tab slug is the HTMX fragment slug for this nav, independent of whatever standalone route/path the trust module already exposes. The `fideicommis` dispatch case (§7.1) renders the module's dossier-scoped view — the tab slug and the module's internal route need not match, so no trust-module route needs renaming.

Group membership:
- `apercu` → **Aperçu** (single leaf, no sub-row)
- `temps`, `facturation`, `fideicommis` → **Finances** (default leaf: `temps`)
- `audiences`, `taches`, `protocole` → **Agenda** (default leaf: `audiences`)
- `documents` → **Documents** (single leaf, no sub-row)

---

## 4. Non-goals

- No change to the trust-accounting module's data model, collections, or business logic — this phase only surfaces its existing dossier-scoped view as the Fidéicommis leaf.
- No Firestore schema or data migration of any kind.
- No change to the `hearings` blueprint name or the `/audiences` route prefix.
- No change to the standalone invoicing page label (`/factures`).
- No Notes tab.
- No new Python dependency (this is a template/route/CSS change only).

---

## 5. Two-level nav architecture (frontend)

Keep the existing HTMX fragment loader. **The content of each leaf still loads via the current `/dossiers/<id>/tab/<slug>` endpoint into the existing tab target** — the grouping is a presentation layer over that, so the backend barely changes.

### 5.1 Structure

- **Top row**: static markup, 4 group tabs.
- **Sub-rows**: static markup, one per multi-leaf group, toggled by `x-show`. Aperçu and Documents have no sub-row.
- **Content region**: reuse the existing container that the current tab loader swaps into (do **not** invent a new id — read the current `hx-target` off the existing tab bar and reuse it).

Prefer extracting the nav into a partial, e.g. `dossiers/_tab_nav.html`, included by the detail page — cleaner than inlining two rows of markup.

### 5.2 Alpine state

Drive selection client-side with Alpine (no server state needed for tab selection — contrast the gabarit modal, which is server-owned because selection re-renders a form):

```js
{
  activeGroup: '<initial from server>',
  activeLeaf:  '<initial from server>',
  groups: {
    apercu:    { label:'Aperçu',    leaves:[{slug:'apercu',      label:'Aperçu'}] },
    finances:  { label:'Finances',  leaves:[
                   {slug:'temps',       label:'Temps & Déboursés'},
                   {slug:'facturation', label:'Honoraires'},
                   {slug:'fideicommis', label:'Fidéicommis'} ] },
    agenda:    { label:'Agenda',    leaves:[
                   {slug:'audiences',   label:'Calendrier'},
                   {slug:'taches',      label:'Tâches'},
                   {slug:'protocole',   label:'Protocole'} ] },
    documents: { label:'Documents', leaves:[{slug:'documents',   label:'Documents'}] }
  }
}
```

Also maintain a leaf→group reverse map (derive it from `groups` or hardcode) for URL restore.

### 5.3 Interaction rules

- **Click a group** → set `activeGroup`; show that group's sub-row (hidden for single-leaf groups); load the group's **first** leaf (v1: always first; remembering last-viewed-per-group is an optional later enhancement).
- **Click a leaf** → set `activeLeaf`; HTMX-load `/dossiers/<id>/tab/<slug>` into the content region.
- **Single-leaf groups** (Aperçu, Documents) → clicking loads the leaf directly, no sub-row.

### 5.4 URL sync (refresh-stable, deep-linkable)

- On leaf navigation, push the **hub** URL with the leaf as a query param — not the fragment path (refreshing a fragment path would return only the fragment). Use HTMX's explicit push URL on each leaf trigger:
  ```
  hx-get="/dossiers/{{ dossier.id }}/tab/{{ slug }}"
  hx-target="<existing target>"
  hx-push-url="/dossiers/{{ dossier.id }}?tab={{ slug }}"
  ```
- On full page load, the **server** reads `?tab=`, validates it against the leaf set (fallback `apercu`), and passes `initial_leaf` + `initial_group` (derived) to the template so the correct group is highlighted, the correct sub-row is shown, and the correct leaf content is requested on `load`.

### 5.5 Mobile behavior

- **Top row**: fixed, non-scrolling. Verify it fits at **320 px** (labels: Aperçu / Finances / Agenda / Documents). If it clips at 320 px, apply the same scroll treatment as the sub-row; otherwise leave fixed.
- **Sub-row**: horizontally scrollable — this is where the two-level pattern and the scrollable-strip pattern combine. Apply:
  - `overflow-x: auto`, `flex-nowrap`, hidden scrollbar (cross-browser), `scroll-snap-type: x proximity`.
  - A right-edge fade affordance so off-screen leaves are discoverable.
  - On group switch / initial render, scroll the **active** leaf into view.
  The Finances sub-row (« Temps & Déboursés » + « Honoraires » + « Fidéicommis ») overflows 375 px and depends on this; the Agenda sub-row is borderline — test both.
- **Touch targets**: every group tab and leaf tab ≥ 44 px (Architecture Rule 9).

### 5.6 Accessibility

- Top row `role="tablist"`; each group `role="tab"` with `aria-selected` and `aria-controls` referencing the sub-nav / panel region. Sub-row `role="tablist"`; each leaf `role="tab"` with `aria-selected`, `aria-controls` referencing the content region (`role="tabpanel"`). Keyboard: left/right arrows move within a row.
- If full nested-tab ARIA proves fussy with the screen reader, a pragmatic fallback is `<nav>` + `aria-current="page"` on the active group/leaf — acceptable, but keep `aria-selected`/`aria-current` accurate either way.

### 5.7 Script-order caution

Alpine and htmx stay at the end of `<body>`, after the App Check boot and any inline component definitions (`CLAUDE.md` — load-bearing under Rocket Loader). Do not move them. If the nav's Alpine component is defined inline, it must appear **before** htmx/Alpine execute in document order.

---

## 6. CSS — compiled-artifact recompile/rehash (MANDATORY if any new class is introduced)

The scroll-strip affordances (hidden scrollbar, edge fade, snap) will likely introduce classes not present in the committed `static/vendor/app.<hash>.css`. **Vendored CSS is served `immutable` for a year — never edit a hashed file in place; a changed asset gets a new filename.**

Recommended: put the bespoke sub-row bits in `static/src/app.input.css` under `@layer components` (e.g. `.dossier-subnav`, hidden-scrollbar rules, the fade) so they compile into the artifact and stay same-origin (avoids inline-style / CSP-`style-src` concerns; CSP is report-only today but keep it clean). Keep dynamically-referenced class names as complete string literals in templates so the `@source` purge doesn't drop them.

If (and only if) class names change, run the full procedure from `CLAUDE.md`:

```
npm install --no-save --no-package-lock @tailwindcss/cli@4.3.0 tailwindcss@4.3.0
npx @tailwindcss/cli@4.3.0 -i athena/static/src/app.input.css -o athena/static/vendor/app.css --minify
# rename to app.<first-8-of-sha256>.css
```

Then update **all** references to the new hash filename and remove the old one:
- `<link>` in `templates/base.html` **and** `templates/auth/login.html`
- the `PRECACHE` list in `static/sw.js`
- the Early Hints lists in `security.py` (`_EARLY_HINTS_*`)
- delete the old hashed file; remove `node_modules` afterwards

Note the app's primary buttons are `bg-gray-900`, not `bg-indigo-600` (indigo-600 is not in the compiled artifact). Use active-tab styling that exists in the current artifact (the current tab bar's active treatment) — don't introduce an indigo utility that isn't compiled without going through the rehash.

---

## 7. Backend changes

Minimal — no model changes.

### 7.1 Tab dispatch — `/dossiers/<id>/tab/<tab_name>`

- Add a `fideicommis` case → render the trust-accounting module's **dossier-scoped** view (that dossier's trust position/ledger). Discover how the module currently exposes per-dossier data: if it already ships a dossier-scoped fragment/partial, reuse it; if only a standalone page exists, render its per-dossier query through a new fragment (§8) following the `temps`/`facturation` tab pattern. Do not duplicate the module's query logic — call its existing per-dossier model function.
- Change the `apercu` case → render the empty-state placeholder (see §8).
- Leave `temps`, `facturation`, `audiences`, `taches`, `protocole`, `documents` cases as-is.
- Update the allowed-tab-name validation set to exactly `{apercu, temps, facturation, fideicommis, audiences, taches, protocole, documents}`; unknown slugs keep the current behavior (404 / graceful empty).

### 7.2 Dossier detail — `GET /dossiers/<id>`

- Parse and validate `?tab=` (fallback `apercu`).
- Compute `initial_group` from the leaf via a **server-side leaf→group map** mirroring the Alpine one, and pass `initial_leaf` + `initial_group` to the template so the first paint highlights the right group, shows the right sub-row, and requests the right leaf on `load`.

No changes to `models/*`, DAV, or `firestore.indexes.json`.

---

## 8. Templates — new & changed

Reconcile the exact current filenames against the live repo (they have drifted from `CLAUDE.md`, which lists `_tab_overview`, `_tab_temps`, `_tab_facturation`, `_tab_audiences`, `_tab_taches`, `_tab_protocole`, `_tab_documents`, and an existing `_tab_placeholder`).

- **`dossiers/_tab_nav.html`** (new, recommended): the two-level nav (top group row + per-group sub-rows) with the Alpine state, ARIA, and scroll affordances from §5. Included by the detail page in place of the current single tab row.
- **Aperçu partial** (currently `_tab_overview`): replace its body with a neutral French empty state. Reuse the existing `_tab_placeholder` partial if its copy can be parameterized; otherwise a minimal empty state. Suggested copy: « Aucun aperçu pour le moment. » Retire the old overview markup.
- **Fidéicommis fragment**: render the trust-accounting module's dossier-scoped view. First check whether the module already ships a dossier-scoped partial — if so, include it from the `fideicommis` dispatch case and do **not** create a parallel one. Only if none exists, create `dossiers/_tab_fideicommis.html` that renders the dossier's trust data via the module's existing per-dossier model function (mirroring how the `temps`/`facturation` fragments query and render), matching the visual language of the other financial tabs. No placeholder — this shows real trust-account content for the dossier.
- **Calendrier relabel**: inside the existing hearings/events tab partial and the nav, change the visible label « Agenda » → « Calendrier » (see §9 for the full rename).

---

## 9. App-level « Agenda » → « Calendrier » rename — do this FIRST

**Ordering matters.** Rename the existing hearings/events feature label from « Agenda » to « Calendrier » **before** introducing the new « Agenda » group, so the two never coexist ambiguously. A blind global find-replace of « Agenda » is unsafe — after this change the word « Agenda » must still exist, but only as the **new group** label.

Rename these user-facing occurrences of « Agenda » (the events feature) → « Calendrier »:
- The main navigation item in `templates/base.html` (and any mobile nav / menu).
- Hearings templates — list, detail, form, month-grid: page `<title>`, headers, empty states, toasts, any breadcrumb.
- The dossier sub-tab label (covered by §3/§8).
- `manifest.json` app shortcut label, if one points at the hearings page.

Keep unchanged (internal, not user-facing):
- Route prefix `/audiences`, blueprint name `hearings`, all `url_for('hearings.*')`.
- DAV `/dav/calendar/` and everything in the DAV layer — it is keyed by collection/protocol, not by UI label, and is unaffected.

Do **not** rename the new top-level group « Agenda ».

Grep guidance: search French UI strings (templates + any label constants) for the word « Agenda », review each hit, and confirm it's the events feature before replacing. Exclude code identifiers.

---

## 10. Backward-compat & migration

- **Deep links**: existing `?tab=<slug>` links keep working (no slug removed; `fideicommis` added). No label-only change breaks a URL.
- **`/audiences` bookmarks**: unaffected (prefix unchanged). *(Optional future cleanup: rename the prefix to `/calendrier` with a 301 from `/audiences` — separate spec.)*
- **DAV / Firestore**: no change, no data migration.
- **Service worker**: if §6 produced a new CSS hash, the `PRECACHE` bump ships the new asset on the next SW update — expected.

---

## 11. Testing checklist

- `GET /dossiers/<id>/tab/fideicommis` → 200, the dossier's trust-accounting view rendered (real content, scoped to that dossier — not a placeholder).
- `GET /dossiers/<id>/tab/apercu` → 200, empty state rendered, no reference to retired overview data.
- All 8 leaf slugs load; an unknown slug is handled as before.
- `GET /dossiers/<id>?tab=facturation` → Finances group active + sub-row shown + Honoraires leaf selected.
- `GET /dossiers/<id>?tab=protocole` → Agenda group active + sub-row shown + Protocole leaf selected.
- `GET /dossiers/<id>?tab=documents` → Documents active, no sub-row.
- Rename: the main nav shows « Calendrier » (not « Agenda ») for the events entry; the dossier detail shows the new « Agenda » group label; the hearings page title reads « Calendrier ».
- `tests/test_security_headers.py` still passes (headers unchanged).
- Update any existing test asserting old tab labels / the « Agenda » nav string.
- Manual mobile QA: top row fits at 320 px and 375 px; Finances sub-row scrolls without clipping and the fade shows; active leaf scrolls into view on group switch; all targets ≥ 44 px.
- ARIA smoke: `role="tablist"`/`role="tab"` present; `aria-selected` / `aria-current` reflects the active group and leaf.

Add `tests/` coverage for the two new/changed dispatch cases (`fideicommis`, empty `apercu`) and the `?tab=` initial-group derivation.

---

## 12. Recommended commit sequence

1. **Rename** « Agenda » → « Calendrier » (app-level + existing dossier sub-tab label) + update affected tests. Isolated, low-risk, independently shippable.
2. **Fidéicommis + Aperçu**: wire the `fideicommis` dispatch case to the trust module's dossier-scoped view (reuse an existing fragment or add one per §8); empty the Aperçu partial + dispatch case.
3. **Two-level nav**: build `_tab_nav.html` (top row + scrollable sub-rows + Alpine + `hx-push-url` + `?tab=` server handling).
4. **CSS**: recompile/rehash if any class changed; update `base.html`, `login.html`, `sw.js`, `security.py`; delete the old hashed file.
5. **Tests + manual mobile QA.**
6. **Update `CLAUDE.md`** (see §14).

---

## 13. Acceptance criteria

- Dossier detail shows 4 top-level groups; Finances and Agenda reveal scrollable sub-rows; Aperçu and Documents load directly with no sub-row.
- Every existing tab's content loads under its new label/position; nothing is lost except the intentionally-emptied Aperçu.
- Fidéicommis renders the selected dossier's trust-accounting view (real content), under Finances.
- The events feature reads « Calendrier » everywhere user-facing (dossier sub-tab + standalone page); `/audiences` still resolves; the new group is « Agenda ».
- Top row fits at 375 px (and 320 px, or scrolls); the sub-row scrolls without clipping; targets ≥ 44 px; a page refresh preserves the active tab via `?tab=`.
- Zero new Python dependencies; CSS served from a new immutable hash **iff** classes changed, with every reference updated; script order preserved; no new inline-style or CDN origin (CSP report-only stays clean).
- `pytest` suite green (Cloud Build deploy gate).

---

## 14. Update `CLAUDE.md`

Reflect the change in:
- **Directory Structure** — note `_tab_nav.html` (and `_tab_fideicommis.html`, only if one was created rather than reusing an existing trust-module fragment) under `templates/dossiers/`; update the tab-partial list.
- **Routes Reference → `dossiers.py`** — the tab loader's active tab-name list becomes `apercu, temps, facturation, fideicommis, audiences, taches, protocole, documents`; note the `?tab=` param on `GET /dossiers/<id>`.
- The note on dossier tab names (currently "there is no separate `notes` tab") — keep the no-Notes note; add the two-level grouping and the Fidéicommis leaf (surfacing the trust-accounting module).
- **Correct the stale trust-accounting references** — `CLAUDE.md` states trust-accounting figures are deferred, but the module is now built. Update those references and make sure the trust module (its model, routes/templates, and the new Fidéicommis dossier tab) is documented; if it isn't captured elsewhere, at minimum flag the doc gap for a follow-up.
- Anywhere the hearings feature is described as user-facing « Agenda » — update to « Calendrier », while keeping the `/audiences` prefix / `hearings` blueprint notes intact.
