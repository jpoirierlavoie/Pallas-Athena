# Gabarit placeholder reference

Every placeholder string you can use inside a `.docx` **gabarit** (Phase H) or
**note d'honoraires** (Phase H.2) template, and the syntax rules that govern
them.

> **Source of truth.** This document is a human-readable index of what the fill
> engine actually supports. The authoritative definitions live in the code:
> - **Syntax / structural tokens** → [`athena/utils/docx_fill.py`](athena/utils/docx_fill.py)
> - **Field catalog, flat aliases, manual & passthrough fields** → [`athena/utils/template_fields.py`](athena/utils/template_fields.py)
> - **Note-d'honoraires context (`facture.*`, rows, conditions)** → [`athena/utils/invoice_docx.py`](athena/utils/invoice_docx.py)
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
- **Matching is case-insensitive.** `{{tribunal}}`, `{{Tribunal}}`, `{{TRIBUNAL}}`
  all resolve to the same field.
- **ALL-CAPS uppercases the value.** A placeholder written in all capitals gets
  its resolved value upper-cased: `{{TRIBUNAL}}` → `COUR SUPÉRIEURE`.
- **Unknown names are left verbatim.** Any placeholder that isn't a known field
  survives as literal `{{name}}` in the output for you to complete in Word —
  generation never fails on it (see [§5 Passthrough](#5-passthrough--left-verbatim)).
- **Multi-paragraph values auto-expand.** A value containing a blank line is
  split into multiple paragraphs, cloning the host paragraph (list numbering
  continues).
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
| `{{dossier.numero_cour}}` | Court file number |
| `{{dossier.reference_interne}}` | Internal reference (`file_number`) |
| `{{dossier.tribunal}}` | Tribunal |
| `{{dossier.chambre}}` | Chamber / competence |
| `{{dossier.district}}` | Judicial district |
| `{{dossier.palais}}` | Courthouse (palais de justice) |
| `{{dossier.role}}` | Client's litigation role, raw (e.g. `demandeur`) |
| `{{dossier.role_feminin}}` | Feminine role (demanderesse, défenderesse, …; `autre` → unresolved) |
| `{{dossier.role_label}}` | Capitalized role label (Demandeur, Défendeur, …) |
| `{{dossier.demandeur}}` | Demandeur name(s), **bare** (no honorific), swapped by role |
| `{{dossier.defendeur}}` | Défendeur name(s), **bare**, swapped by role |
| `{{dossier.demandeur_avec_civilite}}` | Demandeur name(s) **with** Me/M./Mme |
| `{{dossier.defendeur_avec_civilite}}` | Défendeur name(s) **with** honorific |
| `{{dossier.adresse_demandeur}}` | One-line address of the demandeur side |
| `{{dossier.adresse_defendeur}}` | One-line address of the défendeur side |
| `{{dossier.objet}}` | Objet du recours |
| `{{dossier.valeur}}` | Amount in dispute, fr-CA currency (« 85 000,00 $ ») |
| `{{dossier.classe}}` | Value class (Roman numeral I–IV), derived from the value |
| `{{dossier.prescription}}` | Prescription rule label (« 3 ans — art. 2925 C.c.Q. … ») |
| `{{dossier.droit_action}}` | Droit d'action — start of prescription (French long date) |
| `{{dossier.date_pour_agir}}` | Date pour agir — computed limitation deadline (French long date) |

Accented spellings `{{dossier.demandeur_avec_civilité}}` /
`{{dossier.defendeur_avec_civilité}}` also resolve (auto-registered).

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

> **Address selection:** for a partie whose role is `avocat_adverse`, `expert`,
> `huissier`, or `notaire` **and** who has a work address, the *work* address /
> email are used; otherwise the personal ones. Affects every address/email
> field on that slot.

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
| `{{privilège}}` | select: `SOUS TOUTES RÉSERVES` · `PERSONNEL ET CONFIDENTIEL` · `—` |
| `{{transmission_lettre}}` | select: `courriel` · `huissier` · `poste recommandée` · `télécopieur` |

---

## 5. Passthrough — left verbatim

Deliberately **not resolved and not prompted** — these survive as literal
`{{name}}` in the output so you place and fill them in Word:

- `{{civilité}}` — recipient's title/civility. (Belongs in letters, never in
  court procedures — hence yours to place.)
- `{{salutations}}` — closing salutation formula.
- **Any ALL-CAPS block** — e.g. `{{FAITS}}`, `{{CONCLUSIONS}}`, `{{MOYENS}}` —
  free-form legal content.
- **Any unknown name** — anything not matching the catalog (case-insensitively,
  incl. via a flat alias) and not a manual field.

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

## Quick behavioral recap

- Person names render **bare by default**; use the `…_avec_civilite` twin when
  you want the honorific (a letter address block, not a court intitulé).
- Everything is **case-insensitive**; ALL-CAPS **uppercases the value**.
- Unlisted placeholders are **safe** — they stay verbatim, generation never
  fails.
- Blank auto field → `[CHAMP MANQUANT : …]`; blank manual field →
  `[À COMPLÉTER : …]`; passthrough → raw `{{name}}`.
