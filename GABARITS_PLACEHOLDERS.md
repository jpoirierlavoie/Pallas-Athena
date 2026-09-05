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

`{{cabinet.nom}}` · `{{cabinet.adresse_civique}}` · `{{cabinet.ville}}` ·
`{{cabinet.province}}` · `{{cabinet.code_postal}}` · `{{cabinet.telephone}}` ·
`{{cabinet.courriel}}`

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

---

## 6. Note d'honoraires only (`kind="note_honoraires"`)

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
type **« Note (impression) »**; the most recently updated template of that
kind is the one used. It can use everything above (`dossier.*`, `cabinet.*`,
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
- Never put `{{note.contenu}}` in a header/footer — it is left verbatim there.
- The other `note.*` fields are ordinary scalars and work anywhere.

---

## Quick behavioral recap

- Person names render **bare by default**; use the `…_avec_civilite` twin when
  you want the honorific (a letter address block, not a court intitulé).
- Everything is **case-insensitive**; ALL-CAPS **uppercases the value**.
- Unlisted placeholders are **safe** — they stay verbatim, generation never
  fails.
- Blank auto field → `[CHAMP MANQUANT : …]`; blank manual field →
  `[À COMPLÉTER : …]`; passthrough → raw `{{name}}`.
