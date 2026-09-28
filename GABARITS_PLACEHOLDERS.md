# Gabarit placeholder reference

Every placeholder string you can use inside a `.docx` **gabarit** (Phase H), a
**note d'honoraires** (Phase H.2) or a **note-print** (Phase H.3) template,
and the syntax rules that govern them.

> **Source of truth.** This document is a human-readable index of what the fill
> engine actually supports. The authoritative definitions live in the code:
> - **Syntax / structural tokens** → [`athena/utils/docx_fill.py`](athena/utils/docx_fill.py)
> - **Field catalog, flat aliases, manual & passthrough fields** → [`athena/utils/template_fields.py`](athena/utils/template_fields.py)
> - **Note-d'honoraires context (`facture.*`, rows, conditions)** → [`athena/utils/invoice_docx.py`](athena/utils/invoice_docx.py)
> - **Note-print context (`note.*`) + markdown→Word conversion** → [`athena/utils/note_docx.py`](athena/utils/note_docx.py) / [`athena/utils/markdown_docx.py`](athena/utils/markdown_docx.py)
>
> If you add or rename a catalog field, alias, manual field, region, or
> condition in those files, **update this document to match.**

---

## 1. Syntax — the token forms

| Token | What it does | Where it works |
|---|---|---|
| `{{name}}` | **Scalar** — replaced by its resolved value (XML-escaped). | body, headers, footers |
| `{{#region}}` | **Repeating table row** — placed in the row's *first cell*; the innermost `<w:tr>` is cloned once per item. **No closing marker** — the table-row boundary ends the region. An empty list removes the marked row. | note d'honoraires (document body only) |
| `{{?cond}}` … `{{/cond}}` | **Conditional region** — put the two markers in their *own paragraphs* bracketing a table. If the flag is false, the whole span (markers + table) is deleted; if true, only the marker paragraphs are removed. Unbalanced open/close raises an error. | note d'honoraires (document body only) |

### Rules that bite

- **Name charset:** letters (including accents `À–ÿ`), digits `0–9`, underscore
  `_`, and dot `.` — **no spaces inside the name, no hyphens**. Whitespace
  *around* the name is allowed: `{{ name }}` matches `{{name}}`.
- **Matching is case-insensitive** — for auto fields *and*, since September
  2026, for the manual fields of §4: `{{tribunal}}`, `{{Tribunal}}`,
  `{{TRIBUNAL}}` and likewise `{{privilège}}` / `{{PRIVILÈGE}}` all resolve to
  the same field. **Case only, never accents**: `{{privilege}}` unaccented is an
  unknown name and stays passthrough.
- **ALL-CAPS uppercases the value**, auto and manual alike: `{{TRIBUNAL}}` →
  `COUR SUPÉRIEURE`, `{{TRANSMISSION_LETTRE}}` → `COURRIEL` while
  `{{transmission_lettre}}` → `courriel`. One option list therefore serves a
  capitalised letterhead heading and an inline sentence.
- **Unknown names are left verbatim.** Any placeholder that isn't a known field
  survives as literal `{{name}}` in the output for you to complete in Word —
  generation never fails on it (see [§5 Passthrough](#5-passthrough--left-verbatim)).
- **Multi-paragraph values auto-expand.** A value containing a blank line is
  split into multiple paragraphs, cloning the host paragraph (list numbering
  continues). The generation popup shows such a field as a **text area** — a
  single-line input would strip the newlines and the expansion would silently
  never fire. (This is what `{{dossier.sommaire}}` and the `_avec_adresse`
  party blocks rely on.)
- **Each value has a ceiling, and past it the generation is REFUSED — never
  cut** (since lot 2A, 2026-09-27): a manual field 2 000 characters, an auto
  field 5 000 (the longest stored field, `dossier.sommaire`) or its own
  resolved length when the server builds a longer one, a multi-paragraph
  value 20 000. The popup's input stops you at the same number. Until then
  every single-line value was silently CUT at 2 000 — a one-paragraph
  sommaire lost its end in the letter.
- **The popup's choices are checked when you generate.** A client or an
  opposing party that is no longer on the dossier, or a destinataire that no
  longer exists, refuses the generation with a message — it is never
  replaced by the dossier's first party.
- **Missing value → visible marker.** An auto field left blank renders as
  `[CHAMP MANQUANT : name]`; a prompted (manual) field left blank renders as
  `[À COMPLÉTER : name]`. Passthrough names get neither — the raw `{{name}}`
  stays.
- **Split runs ("fragmenté").** Word sometimes fragments a typed placeholder
  across internal runs (most often at the dot in `{{dossier.defendeur}}`). The
  engine heals most of these automatically; a genuinely structural split (a line
  break, tab, image, field code, or bookmark *inside* the braces) is reported as
  a warning at upload, and that field ships as literal `{{…}}` until you retype
  it in Word in one stroke.

---

## 2. Case-data fields (auto-filled)

Filled automatically from the dossier and the selected parties.

### `dossier.*`

| Placeholder | Value |
|---|---|
| `{{dossier.titre}}` | Dossier title |
| `{{dossier.sommaire}}` | Free-text case summary (the detail page's « Sommaire » card). Multi-paragraph: blank-line-separated chunks expand into cloned paragraphs; single line breaks become spaces |
| `{{dossier.numero_cour}}` | Court file number (« Préjudiciaire » while the dossier's forum is préjudiciaire — no proceedings filed yet) |
| `{{dossier.reference_interne}}` | Internal reference (`file_number`) |
| `{{dossier.tribunal}}` | Tribunal |
| `{{dossier.chambre}}` | Chamber / competence |
| `{{dossier.district}}` | Judicial district |
| `{{dossier.palais}}` | Courthouse (palais de justice) |
| `{{dossier.role}}` | Client's litigation role, raw (e.g. `demandeur`) |
| `{{dossier.role_feminin}}` | Feminine role (demanderesse, défenderesse, …; `autre` → unresolved) |
| `{{dossier.role_label}}` | Capitalized role label (Demandeur, Défendeur, …) |
| `{{dossier.demandeur}}` | Demandeur name(s), **bare** (no honorific) — alias of `{{dossier.demandeurs}}` below |
| `{{dossier.defendeur}}` | Défendeur name(s), **bare** — alias of `{{dossier.defendeurs}}` below |
| `{{dossier.demandeur_avec_civilite}}` | Demandeur name(s) **with** Me/M./Mme |
| `{{dossier.defendeur_avec_civilite}}` | Défendeur name(s) **with** honorific |
| `{{dossier.adresse_demandeur}}` | One-line address of the demandeur side |
| `{{dossier.adresse_defendeur}}` | One-line address of the défendeur side |
| `{{dossier.domaine}}` | Domaine label — the taxonomy family (« Recouvrement de créances », …) |
| `{{dossier.action}}` | The action as cited: « Libellé [CODE] » (« Action sur compte [REC-01] ») |
| `{{dossier.action_libelle}}` | The action's name alone, without the bracketed code |
| `{{dossier.action_code}}` | The bare code (« REC-01 ») |
| `{{dossier.precision}}` | Free-text précision on the action — required by the « Autre (préciser) » (`-99`) rows; also holds the pre-taxonomy « Objet » text |
| `{{dossier.delai}}` | The taxonomy's **indicative** delay for the action, short style since July 2026 (« 3 ans ») |
| `{{dossier.point_depart}}` | The action's starting point / traps (« Exigibilité de chaque facture ») |
| `{{dossier.reference}}` | The statutory source of the **delay** (`ref_delai`, « Arts. 2925 et 2931, C.c.Q. »). Split July 2026: six actions with no statutory delay source resolve empty (CST-05, COR-04, COR-09, FAM-01, FAM-02, FAM-06) |
| `{{dossier.fondement}}` | **New (July 2026)** — the seat of the right of action (`ref_fondement`, Annexe C of the taxonomy v1.2; « C.c.Q. » implicit: « 1590; 1708, 1734; 2098, 2106-2108 »). Verify article numbers before alleging them in a procedure |
| `{{dossier.objet}}` | **Renamed « Action » (July 2026)** — kept as an alias, now resolves to the action label, not the old free text |
| `{{dossier.valeur}}` | Amount in dispute, fr-CA currency (« 85 000,00 $ ») |
| `{{dossier.classe}}` | Value class (Roman numeral I–IV), derived from the value |
| `{{dossier.prescription}}` | The confirmed delay label (« 3 ans », « 90 jours », « Imprescriptible »). Generic since July 2026 — the delay's article now travels with `{{dossier.reference}}` (and the right of action's with `{{dossier.fondement}}`), because one period serves many articles |
| `{{dossier.droit_action}}` | Droit d'action — start of prescription (French long date) |
| `{{dossier.date_pour_agir}}` | Date pour agir — computed limitation deadline (French long date) |
| `{{dossier.prise_action}}` | Prise d'action — date the recourse was filed / the limitation period interrupted (art. 2892 C.c.Q.). Manual, never computed; when set it silences the prescription alert |
| `{{dossier.type_mandat}}` | Type de mandat label (« Judiciaire (ad litem) », « Service-conseils », « Général », « Spécial »). **Reworked July 2026** — the old « Transactionnel » / « Consultatif » / « Autre » labels are gone; a dossier saved before the rework shows « — » until re-edited |
| `{{dossier.type_dossier}}` | **Renamed « Domaine » (July 2026)** — kept as an alias of `{{dossier.domaine}}` |
| `{{dossier.type_honoraires}}` | Fee-type label (« Horaire », « Forfaitaire », « Mixte », « Contingence », « Pro bono », « Aide juridique ») |
| `{{dossier.honoraires}}` | Fee type + rate jointly (« Horaire — 250,00 $/h », « Contingence — 25 % », « Mixte — 250,00 $/h + 5 000,00 $ + 25 % ») |
| `{{dossier.taux_horaire}}` | Hourly rate, fr-CA currency (« 250,00 $ ») |
| `{{dossier.forfait}}` | Flat fee, fr-CA currency |
| `{{dossier.pourcentage}}` | Contingency percentage, fr-CA (« 25 % ») — set for `contingency` and `mixed` |
| `{{dossier.notes_honoraires}}` | Free-text notes on the fee arrangement |
| `{{dossier.ouverture}}` | Opening date (French long date) |
| `{{dossier.fermeture}}` | Closing date (French long date; unresolved while open) |
| `{{dossier.retention}}` | Document-retention date = closing date + 7 years (French long date) |

Accented spellings `{{dossier.demandeur_avec_civilité}}` /
`{{dossier.defendeur_avec_civilité}}` also resolve (auto-registered).

#### Several parties — the role-scoped families (September 2026)

A dossier holds **any number** of parties per side, each with its own `roles`.
A « mis en cause » is not a defendant, so these read **each party's own role**
rather than the side it sits on — which also makes them resolve on a dossier
whose overall role is blank, « intervenant » or « autre » (the
demandeur/défendeur *positions* above cannot).

Two forms per role. The **inline** form enumerates in French — `A, B et C`. The
**`_avec_adresse`** form emits *one paragraph per party*, `Nom, adresse`, and
because the chunks are blank-line separated the fill engine clones the host
paragraph once each, **numbering, indent and style included**. Put it alone in
one paragraph of your intitulé; there is no marker syntax to learn.

| Role | Inline | One paragraph per party (name + address) |
|---|---|---|
| demandeur | `{{dossier.demandeurs}}` | `{{dossier.demandeurs_avec_adresse}}` |
| défendeur | `{{dossier.defendeurs}}` | `{{dossier.defendeurs_avec_adresse}}` |
| demandeur reconventionnel | `{{dossier.demandeurs_reconventionnels}}` | `{{dossier.demandeurs_reconventionnels_avec_adresse}}` |
| défendeur reconventionnel | `{{dossier.defendeurs_reconventionnels}}` | `{{dossier.defendeurs_reconventionnels_avec_adresse}}` |
| mis en cause | `{{dossier.mis_en_cause}}` | `{{dossier.mis_en_cause_avec_adresse}}` |
| intervenant | `{{dossier.intervenants}}` | `{{dossier.intervenants_avec_adresse}}` |
| appelant | `{{dossier.appelants}}` | `{{dossier.appelants_avec_adresse}}` |
| intimé | `{{dossier.intimes}}` | `{{dossier.intimes_avec_adresse}}` |
| requérant | `{{dossier.requerants}}` | `{{dossier.requerants_avec_adresse}}` |

Rules that bite here:

- **Nothing is invented.** If no party carries the role, the placeholder is
  unresolved and prints `[CHAMP MANQUANT : …]`. A bankruptcy whose adverse
  parties are `intimé` / `mis en cause` / `requérant` has **no** défendeur, and
  saying otherwise in an intitulé would be wrong — use the matching role.
- **Legacy dossiers keep their meaning.** Party roles were added in July 2026
  and never back-filled. When *no* party on a side carries any role,
  `{{dossier.demandeur}}` / `{{dossier.defendeur}}` fall back to naming that
  whole side, exactly as before. A side where *some* party is tagged is taken at
  its word — an untagged co-client there is a confrère, not a second defendant.
- **The address comes from the contact record**, through the same
  personal-vs-professional arbitration as `{{<slot>.adresse_complete}}`. A party
  with no usable address contributes its name alone — degraded, never wrong.
- **It works inside a table cell**, which is how most intitulés are laid out:
  put `{{dossier.defendeurs_avec_adresse}}` alone in the left cell's paragraph
  and « Défendeurs » in the right one. The *paragraph* is cloned, not the row,
  so the quality label opposite stays put. (This is why the party block does not
  use the `{{#region}}` row repeat: that clones the whole row and flattens
  newlines, so an address could never take its own line.)
- **A party holding two roles appears under each**, by design: a défenderesse
  who is also demanderesse reconventionnelle answers both questions. Put only
  the placeholders your intitulé should name.
- `{{dossier.adresse_demandeur}}` / `{{dossier.adresse_defendeur}}` are
  unchanged: they still give **one** address, that of the party picked in the
  popup. For every party's address, use the `_avec_adresse` form.

### `client.*`, `adverse.*`, `destinataire.*` (partie slots)

Each of the three slots exposes the **same 14 fields**. Replace `<slot>` with
`client`, `adverse`, or `destinataire`:

| Placeholder | Value |
|---|---|
| `{{<slot>.nom_complet}}` | Full name, **bare** (no honorific); organizations → legal name |
| `{{<slot>.nom_complet_avec_civilite}}` | Full name **with** honorific (accented `…_civilité` also works) |
| `{{<slot>.prenom}}` | First name (individuals only) |
| `{{<slot>.nom}}` | Last name (individuals only) |
| `{{<slot>.organisation}}` | Organization name |
| `{{<slot>.adresse_civique}}` | Civic address (street, or "street, unit") |
| `{{<slot>.ville}}` | City |
| `{{<slot>.province}}` | Province |
| `{{<slot>.code_postal}}` | Postal code |
| `{{<slot>.pays}}` | Country |
| `{{<slot>.adresse_complete}}` | One-line full address |
| `{{<slot>.courriel}}` | Email (work vs. personal per selected address) |
| `{{<slot>.telephone}}` | Phone, formatted (work → cell → home) |
| `{{<slot>.numero_barreau}}` | Bar number |

> **Address selection (preference + fallback):** the contact's role decides
> which address block is tried **first** — the *work* block for
> `avocat_adverse`, `expert`, `huissier` and `notaire`, the *personal* one for
> everybody else (clients included). **The other block is the fallback** when
> the preferred one carries no address, so a **personne morale** always prints:
> the contact form hides the personal block for an organization (its address
> can only be entered under « Adresse »/professional), while the client portal
> writes a company's address into the personal one. Both are legitimate.
> The email follows the block that was selected; the phone does not (it has its
> own work → cell → home order). Affects every address/email field on the slot.

### `cabinet.*` (your firm)

`{{cabinet.nom}}` · `{{cabinet.organisation}}` · `{{cabinet.adresse_civique}}` ·
`{{cabinet.ville}}` · `{{cabinet.province}}` · `{{cabinet.code_postal}}` ·
`{{cabinet.telephone}}` · `{{cabinet.telecopieur}}` · `{{cabinet.courriel}}`

> **`nom` is YOU, `organisation` is the firm.** `{{cabinet.nom}}` is the
> lawyer (« Me Jason Poirier Lavoie ») and is what signs — a procedure, a
> letter, an identity verification. `{{cabinet.organisation}}` is the firm's
> trade name (« Poirier Lavoie, avocat »), for a letterhead or a firm
> designation. A blank `organisation` falls back to `nom`; never the reverse.
>
> All of these are edited in **Paramètres → Profil du cabinet**. Note that
> the name shown in the *client portal* comes from the deployment
> configuration (`portail.yaml`) and does not change there.

### `date.*`

| Placeholder | Value |
|---|---|
| `{{date.aujourdhui}}` | Today, French long date (« 25 avril 2026 »; `1er` for the 1st) |
| `{{date.aujourdhui_iso}}` | Today, ISO `YYYY-MM-DD` |

---

## 3. Flat aliases (shorthand)

Short, un-namespaced names that map onto the catalog — so one template set can
serve both this app and external skills. A flat alias **wins** over a
same-spelled namespaced field.

| Alias | Resolves to |
|---|---|
| `{{district}}` | `dossier.district` |
| `{{numero_dossier}}` | `dossier.numero_cour` |
| `{{tribunal}}` | `dossier.tribunal` |
| `{{chambre}}` | `dossier.chambre` |
| `{{référence_interne}}` | `dossier.reference_interne` |
| `{{intitulé_dossier}}` | `dossier.titre` |
| `{{sommaire}}` | `dossier.sommaire` |
| `{{rôle}}` | `dossier.role_feminin` (**feminine** role, not the raw role) |
| `{{demandeur}}` / `{{défendeur}}` | `dossier.demandeur` / `dossier.defendeur` (bare) |
| `{{demandeur_avec_civilité}}` / `{{demandeur_avec_civilite}}` | `dossier.demandeur_avec_civilite` |
| `{{défendeur_avec_civilité}}` / `{{défendeur_avec_civilite}}` | `dossier.defendeur_avec_civilite` |
| `{{adresse_demandeur}}` / `{{adresse_défendeur}}` | `dossier.adresse_demandeur` / `dossier.adresse_defendeur` |
| `{{valeur}}` | `dossier.valeur` |
| `{{classe}}` | `dossier.classe` |
| `{{prescription}}` | `dossier.prescription` |
| `{{droit_action}}` | `dossier.droit_action` |
| `{{date_pour_agir}}` | `dossier.date_pour_agir` |
| `{{prise_action}}` | `dossier.prise_action` |
| `{{domaine}}` | `dossier.domaine` |
| `{{action}}` | `dossier.action` |
| `{{objet}}` | `dossier.objet` (→ the action label; **new alias** — `{{objet}}` used to fall silently into passthrough) |
| `{{précision}}` / `{{precision}}` | `dossier.precision` |
| `{{délai}}` / `{{delai}}` | `dossier.delai` |
| `{{point_départ}}` / `{{point_depart}}` | `dossier.point_depart` |
| `{{référence_action}}` / `{{reference_action}}` | `dossier.reference` |
| `{{fondement}}` / `{{référence_fondement}}` / `{{reference_fondement}}` | `dossier.fondement` |
| `{{type_mandat}}` | `dossier.type_mandat` |
| `{{type_dossier}}` | `dossier.type_dossier` (→ the domaine label) |
| `{{date_ouverture}}` / `{{date_fermeture}}` | `dossier.ouverture` / `dossier.fermeture` |
| `{{rétention}}` / `{{retention}}` | `dossier.retention` |
| `{{ville_procédure}}` / `{{ville_lettre}}` | `cabinet.ville` |
| `{{date_procédure}}` / `{{date_lettre}}` | `date.aujourdhui` |
| `{{prénom_récipient}}` | `destinataire.prenom` |
| `{{nom_récipient}}` | `destinataire.nom` |
| `{{cabinet_récipient}}` | `destinataire.organisation` |
| `{{adresse_civique_récipient}}` | `destinataire.adresse_civique` |
| `{{ville_récipient}}` | `destinataire.ville` |
| `{{province_récipient}}` | `destinataire.province` |
| `{{code_postal_récipient}}` | `destinataire.code_postal` |
| `{{pays_récipient}}` | `destinataire.pays` |

---

## 4. Manual fields (prompted, no data source)

Short letter-metadata inputs offered in the generation popup. Left blank →
`[À COMPLÉTER : name]`.

| Placeholder | Default / options |
|---|---|
| `{{procédure}}` | free text (empty) |
| `{{disposition}}` | free text (empty) |
| `{{objet_lettre}}` | free text (empty) |
| `{{référence_externe}}` | free text (empty) |
| `{{pièces_jointes}}` | defaults to **`Aucune`** |
| `{{privilège}}` | select: `SOUS TOUTES RÉSERVES` · `SOUS TOUTES RÉSERVES ET SANS PRÉJUDICE` · `SANS PRÉJUDICE` · `PERSONNEL ET CONFIDENTIEL` · `CONFIDENTIEL` · `PRIVILÉGIÉ ET CONFIDENTIEL` · `—` · **`(aucune mention)`** |
| `{{transmission_lettre}}` | select: `courriel` · `huissier` · `poste recommandée` · `télécopieur` |

**Two ways of saying nothing, and they differ.** Choosing **« (aucune mention) »**
prints *nothing at all*; leaving the select untouched prints the loud
`[À COMPLÉTER : privilège]`. The `—` option prints a literal em dash, for a
letterhead that reserves a visible line for the mention.

A submitted value outside a field's option list is now **refused** with a French
message naming the field — the `<select>` used to be the only constraint.

---

## 5. Passthrough — left verbatim

Deliberately **not resolved and not prompted** — these survive as literal
`{{name}}` in the output so you place and fill them in Word:

- `{{civilité}}` — recipient's title/civility. (Belongs in letters, never in
  court procedures — hence yours to place.)
- `{{salutations}}` — closing salutation formula.
- **Any ALL-CAPS block** — e.g. `{{FAITS}}`, `{{CONCLUSIONS}}`, `{{MOYENS}}` —
  free-form legal content. ⚠ **Capitals alone no longer make a name
  passthrough** (September 2026): matching folds case on *both* families, so the
  seven §4 names are prompted whatever their case — `{{PRIVILÈGE}}` gets its
  select, and `{{DISPOSITION}}` / `{{PROCÉDURE}}` are prompted as manual fields
  rather than left for Word. Pick a block name that is not one of those seven.
- **Any unknown name** — anything not matching the catalog *or the manual
  fields* (both case-insensitively, the catalog also via a flat alias).

### Through the connector (`fill_gabarit`, lot 2A, September 2026)

A passthrough name is also what **Claude** may write, through the connector's
`fill_gabarit` — a *bloc*. Everything else stays the application's.

**What a call can supply — and nothing else:**

| Argument | What it is | Limits |
|---|---|---|
| `template_id` | The gabarit. **Kind « gabarit » only** — a « Note d'honoraires » or « Note (impression) » template is refused: they have their own flows (the invoice's « Note d'honoraires (Word) » in the application; the connector's `create_document` for a note print, §7) | — |
| `dossier_id` | **Required.** Its data fills every auto field, and the document is saved as a NEW document in ITS « Projets » folder — **always there**, never another folder, never a download | — |
| `client_id` / `adverse_id` | The « client » / « adverse » slots — a party **on that dossier**, needed when the dossier has several and the gabarit reads the slot (never guessed, never the first one by default) | — |
| `destinataire_id` | The « destinataire » slot — any contact; no default | — |
| `blocs` | `[{nom, contenu, markdown?}]` — the passthrough names only | ≤ 12 blocs, ≤ 20 000 characters each, 60 000 in all |
| `champs_manuels` | `[{nom, valeur}]` — the §4 fields; an option list is enforced, and the « (aucune mention) » option prints nothing | ≤ 12, ≤ 2 000 characters each |

**Read the template first.** `list_templates` with `template_id` (and the
`dossier_id`, plus the slot ids) lists the gabarit's AUTO fields with a
« resolved » flag — **never a value** — its MANUAL fields with their options,
and its BLOCS (names exact). An auto field that does not resolve prints
`[CHAMP MANQUANT : name]`: it is a gap in the dossier to report or fix there,
not something to write around in a bloc.

- **Only the passthrough names are Claude's.** The auto fields are resolved by
  the server from the dossier and its parties (a bloc named like one is
  refused, never overridden); the manual fields are set through their own
  list, their options enforced. Names are compared **exactly** — the case
  counts here, as `list_templates` reports them.
- **A bloc's paragraphs are separated by a BLANK LINE**; a single newline
  becomes a space. Each paragraph is a clone of the host paragraph, so a
  `{{FAITS}}` alone in a **numbered** paragraph yields real, continuous Word
  numbers — which is why a bloc should carry **no numbering of its own**.
- **`markdown: true`** sends a bloc through the formatted path of §7 (the host
  paragraph's own numbering neutralized). It needs the same host as
  `{{note.contenu}}`: alone in its paragraph, in the body. Otherwise it prints
  as plain text, Markdown sigils visible, and the tool says so
  (`blocs_demoted`).

**Word's numbers or computed numbers — pick by the path, never both:**

| | Plain bloc (the default) | `markdown: true` |
|---|---|---|
| Who numbers | **Word** — the host paragraph's numbering applies to each paragraph the blank lines separate | **The converter** — a Markdown ordered list (`1.`, `2.`) is numbered as TEXT, computed at generation |
| The host's numbering | Inherited, once per paragraph | Neutralized (« numbering removed » — never inherited, so nothing is numbered twice) |
| After the lawyer edits in Word | The numbers renumber themselves, and continue the list the host paragraph belongs to | They are plain text: an inserted or removed item does NOT renumber, and each Markdown list is numbered on its own |
| Use it for | Numbered allegations, conclusions — anything the lawyer will renumber | Internal structure: headings, bold, a bullet list, a table |

Writing « 1. » yourself in a plain bloc placed in a numbered paragraph prints
the number TWICE (Word's, then yours). One limit, shared with §7: numbering
carried by the paragraph's Word STYLE alone (« Liste numérotée » / « List
Number », with no number applied on the paragraph itself) is still inherited
by a `markdown: true` bloc — give that paragraph an ordinary style.
- **A bloc or a manual value can never contain `{{` or `}}`** — the engine
  would read `{{dossier.demandeur}}` inside it as a field and print the
  dossier's data there. It is refused, not escaped. A **Markdown** bloc (and
  `create_document`'s Markdown) is judged once FORMATTED too: a `\{` escape or
  a `&#123;` / `&lbrace;` character reference prints a brace, so a text that
  would print `{{…}}` is refused as well.
- A bloc Claude does not write stays `{{name}}` for Word, and the result says
  which (`blocs_left_verbatim`, read back from the produced file — a field
  Word fragmented is listed there too).

---

## 6. Note d'honoraires only (`kind="note_honoraires"`)

The invoice's « Note d'honoraires (Word) » fills the template of this type
that is **designated as active** on its page (« Désigner comme gabarit
actif ») — never simply the most recent one (lot 2A, 2026-09-27); with none
designated, generation refuses and says so.

A note-d'honoraires template can use **everything above** for its header
(`dossier.*`, `destinataire.*`, `cabinet.*`, `date.*`, and their flat aliases —
the destinataire slot is the invoice's client), **plus** the following.

All `facture.*` money / rate / date / hours values arrive **pre-formatted**
fr-CA (NBSP thousands, comma decimals, trailing ` $`). Figures are read from the
stored invoice — never recomputed.

### `facture.*` scalars

| Placeholder | Value |
|---|---|
| `{{facture.numero}}` | Invoice number (raw string) |
| `{{facture.date}}` | Invoice date (French long date) |
| `{{facture.date_echeance}}` | Due date |
| `{{facture.sous_total_honoraires}}` | Fees subtotal |
| `{{facture.sous_total_debours_tx}}` | Taxable disbursements subtotal |
| `{{facture.sous_total_debours_ntx}}` | Non-taxable disbursements subtotal |
| `{{facture.total_honoraires}}` | Total fees (= `sous_total_honoraires`) |
| `{{facture.total_debours_tx}}` | Total taxable disbursements (= `sous_total_debours_tx`) |
| `{{facture.total_debours_ntx}}` | Total non-taxable disbursements (= `sous_total_debours_ntx`) |
| `{{facture.total_avant_taxes}}` | Subtotal before taxes |
| `{{facture.tps_taux}}` | GST/TPS rate (« 5 % ») |
| `{{facture.tps_numero}}` | GST registration number |
| `{{facture.tps_montant}}` | GST amount |
| `{{facture.tvq_taux}}` | QST/TVQ rate (« 9,975 % ») |
| `{{facture.tvq_numero}}` | QST registration number |
| `{{facture.tvq_montant}}` | QST amount |
| `{{facture.total_apres_taxes}}` | Total after taxes |
| `{{facture.avances_fideicommis}}` | Retainer applied, **parenthesized** deduction (« (1 150,00) $ ») |
| `{{facture.solde}}` | Balance due |
| `{{facture.nombre_heures}}` | Total billed hours (« 0,50 ») |
| `{{facture.taux_horaire}}` | Hourly rate (uniform billed rate; else dossier fallback; else blank) |

> `sous_total_debours_tx + sous_total_debours_ntx == subtotal_expenses`.

### Repeating rows

| Region marker | Row-scoped fields |
|---|---|
| `{{#ligne_honoraire}}` | `{{h.date}}` · `{{h.description}}` · `{{h.temps}}` |
| `{{#ligne_debours_tx}}` (taxable) | `{{d.date}}` · `{{d.description}}` · `{{d.cout}}` |
| `{{#ligne_debours_ntx}}` (non-taxable) | `{{d.date}}` · `{{d.description}}` · `{{d.cout}}` |

The two disbursement regions share the identical `d.*` field set — only which
line items populate each differs (taxable vs. non-taxable). Row-scoped fields
are prefixed `h.` / `d.` so they never collide with the global scalars.

### Conditional flags

| Flag | True when |
|---|---|
| `{{?si_honoraires}}` … `{{/si_honoraires}}` | there is ≥ 1 fee line |
| `{{?si_debours_tx}}` … `{{/si_debours_tx}}` | there is ≥ 1 taxable disbursement |
| `{{?si_debours_ntx}}` … `{{/si_debours_ntx}}` | there is ≥ 1 non-taxable disbursement |

Wrap each section's table in its flag so an empty section disappears cleanly.

---

## 7. Impression d'une note only (`kind="note"`)

The **note-print** template — the .docx the « Imprimer (Word) » button on a
note's page (and on the Analyse tab) fills, streamed as a **direct download**
(never saved into the dossier's documents). Upload it in « Gabarits » with the
type **« Note (impression) »**, then **designate it** on its page
(« Désigner comme gabarit actif »): the DESIGNATED template of that kind is
the one used — since lot 2A (2026-09-27) there is no « most recently
updated » fallback, and with none designated the print refuses, naming the
fix. It can use everything above (`dossier.*`, `cabinet.*`,
`date.*`, flat aliases — no destinataire slot in this flow) **plus**:

### `note.*` scalars

| Placeholder | Value |
|---|---|
| `{{note.titre}}` | The note's title, verbatim. |
| `{{note.categorie}}` | The category's French label (« Stratégie », « Rencontre », …). |
| `{{note.date}}` | Creation date — **Montréal** calendar date, French long form (« 1er août 2026 »). |
| `{{note.date_maj}}` | Last-modified date, same form — **empty when same day as creation** (mirrors the on-screen « Modifiée » rule). |
| `{{note.dossier}}` | `N° — Titre` of the note's dossier; **« Général »** for a dossier-less note (always resolves — prefer it over `dossier.*` if your template must serve general notes too, whose `dossier.*` fields render `[CHAMP MANQUANT : …]`). |
| `{{note.contenu}}` | **The rich field** — the note's Markdown body converted to real Word formatting. |

### What `{{note.contenu}}` renders

The conversion targets the SCREEN rendering (same markdown pipeline):
headings (sized steps off your template's font size, bold), **bold** /
*italic*, inline `code` and fenced blocks (Consolas, shaded), bullet and
numbered lists (text bullets / computed numbers — printable, not
Word-restyleable), `>` blockquotes (left border), `---` rules, single line
breaks as real line breaks, links as underlined text with the URL appended,
and **markdown tables as real Word tables** (bordered, header row shaded,
`:---:` alignment honoured, equal column widths across the page).

### Rules that bite (note-print)

- **`{{note.contenu}}` must sit ALONE in its own paragraph** in the template.
  The engine replaces that whole paragraph with the converted content; the
  paragraph's own formatting (font, size, justification) seeds the body text.
  If the placeholder shares its line with other text, the content degrades to
  plain text (markdown sigils visible) — the document still generates.
- **Everything of that paragraph's formatting seeds the content EXCEPT its
  Word numbering** (since lot 2A, 2026-09-27): a `{{note.contenu}}` placed in
  a numbered list paragraph no longer numbers every heading, item and table
  cell a second time in front of the converter's own numbers. Want Word's
  numbering on each paragraph instead? That is the plain fill of an ordinary
  field in a numbered paragraph, separated by blank lines (§1). One limit:
  numbering that comes from the paragraph's Word STYLE alone (« Liste
  numérotée » / « List Number », with no number applied on the paragraph
  itself) is still inherited — give that paragraph an ordinary style.
- Never put `{{note.contenu}}` in a header/footer — it is left verbatim there.
- The other `note.*` fields are ordinary scalars and work anywhere.
- **The connector prints on this same template** (`create_document`, source
  « markdown », lot 2A): Claude's Markdown as `{{note.contenu}}`, its title as
  `{{note.titre}}`, saved as a NEW document in « Projets » unless Claude names
  another folder of the dossier (the web print stays a download). Only the template the lawyer designated **active** is used —
  none designated, it refuses; and where `{{note.contenu}}` cannot take
  formatting it REFUSES rather than store a document full of Markdown sigils;
  where it is not in the BODY at all (only in a header or footer, or split by
  Word), it refuses too — read back from the produced file — rather than store
  a document without the text.
  A drafted text has no note category nor modification date:
  `{{note.categorie}}` and `{{note.date_maj}}` print empty there.

---

## Quick behavioral recap

- Person names render **bare by default**; use the `…_avec_civilite` twin when
  you want the honorific (a letter address block, not a court intitulé).
- Everything is **case-insensitive**; ALL-CAPS **uppercases the value**.
- Unlisted placeholders are **safe** — they stay verbatim, generation never
  fails.
- Blank auto field → `[CHAMP MANQUANT : …]`; blank manual field →
  `[À COMPLÉTER : …]`; passthrough → raw `{{name}}`.
