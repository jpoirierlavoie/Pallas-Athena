# Pallas Athena — Master Reference

Pallas Athena is a single-user legal practice management web application for a Québec civil litigation lawyer (Jason Poirier Lavoie, Barreau du Québec). It manages contacts (parties), dossiers (case files), billable hours, expenses, invoices, hearings, tasks, case protocols, notes, and procedural documents. It synchronizes bidirectionally with DavX5 on Android via CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL).

Deployed at `athena.poirierlavoie.ca`. GCP project: `athena-pallas`. Codebase is on GitHub; deploys via Cloud Build trigger on push to main.

This document supersedes the old `SPEC.md` and phase-specific markdown files as the canonical reference for future work.

---

## Change Impact Assessment (do this before any change)

Several subsystems are **delicate and coupled to external frameworks/services, and they fail SILENTLY** — the change looks fine, tests may pass, and the breakage only surfaces later in production or on a synced device. **Before implementing any change or improvement, run a one-line impact check against each component below.** State explicitly whether the change touches it; if it does, say what you verified. Most changes touch none — the discipline exists so one is never broken silently. (A trivial change — docs, or a pure-logic helper with no runtime surface — may just note "no impact on the four" and proceed.)

1. **MCP connector** (`mcp/`, exposes data to Claude — 26 read tools + **21 write tools** in four families (CREATE, CORRECT, RECLASSIFY, IMPORT — see the Tech Stack bullet), each with a **declared `outputSchema` contract**). *Touches it when the change edits:* `auth.py`/session, `security.py` (CSP, CSRF exemptions, rate limits, request-size caps, the App Check HTMX predicate, **`sanitize`/`TAG_RE`**), `config.py`, the OAuth collections, any `models/*` function the tools read **or write**, **or the SHAPE of any handler payload** (a key added/removed/renamed, a type changed, a new branch) — `mcp/output_schemas.py` is a MUST-conform contract on `structuredContent`, so a payload change without its schema+fixture change ships a violated contract that a strict client rejects with no error on our side. *Verify:* OAuth/bearer flow + both kill switches (`MCP_ENABLED`, `MCP_WRITE_ENABLED`) intact; no PII or signed URLs in tool output; date-only fields use `mcp.tools.date_str`, never `to_mtl`; no MCP-only query needs a new composite index; **schema and handler moved together** — every input property keeps a `description`, every output root keeps `type: "object"` (wire-mandated even beside `anyOf`; one bare root kills all 47 tools on SDK clients), no output schema gains `additionalProperties: false`, `required` gains only always-present keys, and a value flowing verbatim from a model matches its declared type on EVERY write path (web, DAV, MCP — the vobject list-in-address case); `test_mcp_*` green, `test_mcp_output_schemas.py` in particular. **The write tools make item #2 apply to this component** — a DAV-exposed write (note, task, hearing) that skips `bump_ctag(collection_for(dossier_id))` desyncs DavX5 silently, and since lot Q **a contact write must call `bump_ctag("parties")`**, which extends item #2 to a SECOND collection (the CardDAV addressbook). `mcp.tools.WRITE_TOOLS` is the pinned list and every member runs through `mcp/write_support.run_write` (`dry_run` + `idempotency_key`). Two further verifications since lot Q: **a tool that MODIFIES a stored value must carry `destructiveHint: true`**, which is DERIVED from `mcp.tools.EDIT_TOOLS` and no longer a family constant — under-warning is worse than over-warning, since a client uses the hint to decide whether to confirm; and **a `dry_run` must refuse everything the live call refuses**, because `run_write` short-circuits the dry branch WITHOUT calling the model, so every model-side guard has to be repeated in the handler ahead of it. `complete_task` changes a task's STATUS and, through `models/task.update_task`, cascades into the linked protocol step (see the Known Gotcha), and the scope gate lives in `endpoint._tools_call` (never in a handler — `except Exception` there would turn a 403 into a 200).

2. **DavX5 sync** (`dav/` + the model DAV serializers). *Touches it when the change edits:* any `X_to_vcard/_to_vevent/_to_vtodo/_to_vjournal` serializer or its inverse parser, a CTag-bump call site, collection structure/naming, a field those serializers read, **or adds any new write path to a DAV-exposed collection** (bulk import, migration, cron, a future write-capable tool). *Verify:* output stays RFC-5545/vCard-valid with the **mandatory `UID`/`DTSTAMP`/`CREATED`** present (the jtx `icalobject.created` NOT-NULL trap); every mutation bumps its CTag — **CTag bumping lives in the route layer, not the models, so a write that bypasses the existing routes never bumps and DavX5 silently never re-syncs**; root Depth:1 PROPFIND discovery unaffected. **DavX5 fails silently — test the endpoint with `curl` before trusting it.**

3. **Template generation / field matching** (`utils/docx_fill.py`, `utils/template_fields.py`). *Touches it when the change edits:* the fill-engine regexes / run-normalization / split-run detection, the field catalog or `FLAT_ALIASES`, or a `models/partie|dossier` field name the catalog resolves. *Verify:* placeholders still fill on every occurrence (normalization + per-occurrence detection intact); output opens in Word **without repair** (never introduce a `docxtpl`/`python-docx` round-trip); `resolve_values` still maps every catalog field; `test_docx_fill`/`test_template_fields` green.

4. **Observability & logging** (`utils/logging_setup.py`, `utils/tracing_setup.py`, `athena/OBSERVABILITY.md`). *Touches it when the change adds a log line, a span, or a new user-facing data path.* *Verify:* emit only through the typed helpers (never raw `logger.*`); no PII (wrap interpolated user values in `sanitize_log_value`; the redaction filter only auto-scrubs emails/phones/postal/court-file — **names, titles, and client strings are NOT auto-redacted**, and it sits on the single root handler, so a second handler bypasses it); new events/spans registered in `OBSERVABILITY.md`; structured fields land in `jsonPayload` (production uses `CloudLoggingHandler`, not the deprecated `AppEngineHandler`); spans carry IDs/counts only, never names/bodies/tokens.

5. **Security & edge defense** (`security.py`, the `main.py` `before_request` chain, `app.yaml`, Cloudflare/GCP config). *Touches it when the change edits:* response headers/CSP, the CSRF exemption list, rate-limit or brute-force keys, request-size caps, the `before_request` path-prefix allowlists (`/_ah/`, `UPLOAD_PATHS`, `_is_template_upload_path`, the App Check exempt prefixes), any `Config` secret name (`cf-origin-secret`, `RECAPTCHA_ENTERPRISE_SITE_KEY`), or adds a route under a prefix those checks special-case. *Verify:* App Check and the origin-secret check **fail open when their key is unset** — and they are NOT equally loud about it: App Check logs a warning in production (`security.py`, « App Check site key not configured »), while **`_enforce_origin_secret` says nothing at all** — `if not secret: return None`, no log, no metric. (This entry claimed both were « warned once » until 2026-08-11, when the August audit found `cf-origin-secret` had never existed in Secret Manager and nothing had ever signalled it.) `scripts/check_config.py` is the only thing that reports it, and nothing runs that automatically. A dropped or renamed secret therefore disables the check in perfect silence; a new sensitive route isn't accidentally under an exempt prefix; `CF-Connecting-IP` (rate-limit/brake key) is only trustworthy while the App Engine firewall + origin secret hold — don't widen the firewall; CSP is **enforced** with a **per-request nonce** on `script-src` (`'self' 'nonce-…' 'unsafe-eval'` + the Google reCAPTCHA origins — no `'unsafe-inline'`, no `ajax.cloudflare.com`; `build_csp`/`csp_nonce` in `security.py`), so a new inline `<script>` runs **only** if it carries `nonce="{{ csp_nonce }}"` and an injected/un-nonced inline script is **blocked today**; inline `on*` handlers are refused outright (a nonce can't authorize an attribute) — wire events via `addEventListener` on `data-` attributes as `base.html` does. `'unsafe-eval'` stays for Alpine's `new Function()`, `style-src` keeps `'unsafe-inline'` for reCAPTCHA; prefer external/vendored JS; a new browser/session POST goes in its own blueprint, never onto a CSRF-exempt one (`mcp_bp` is blanket-exempt).

6. **Frontend asset pipeline** (`static/vendor/`, `static/src/app.input.css`, `static/sw.js`, `base.html`, the `_EARLY_HINTS_*` lists in `security.py`). *Touches it when the change edits:* a template's Tailwind classes, `app.input.css`, any vendored asset, or the `<script>` block in `base.html`. *Verify:* a new/renamed class went through the **full recompile + rehash fan-out** (new `app.<hash>.css` → the `<link>` in `base.html` + `auth/login.html`, the `PRECACHE` in `sw.js`, the `_EARLY_HINTS_*` in `security.py`; delete the old hash) — a class absent from the compiled artifact silently doesn't apply; **never edit a `static/vendor/` file in place** (immutable 1-year cache → returning devices keep the stale copy) — always a new hash filename plus a bumped `sw.js` cache version; the footer script document-order (App Check boot → htmx → Alpine) is load-bearing — execution follows document order (the Firebase/App Check boot scripts are synchronous, at parse time; the vendored htmx/Alpine `defer` scripts run at `DOMContentLoaded`), so reordering silently breaks App Check on `hx-trigger="load"` requests or Alpine `x-data` evaluation.

7. **Firestore indexes & query invariants** (`firestore.indexes.json`, model queries). *Touches it when the change edits:* any `.where()`+`.order_by()` combo, a new filter value / sort field / cursor-paginated list, or a SUM/AVG aggregation. *Verify:* the composite index exists and **deploys before or with the code** (`firebase deploy --only firestore:indexes`) — until it finishes building the query fails and the view silently degrades to an empty list; an existing index is **not** enough just because it names the same fields — a backwards scan flips EVERY field, so an ASC equality combined with a DESC sort is a third ordering that needs its own index (the August 2026 `opening_book_balance` 500); a SUM/AVG needs its **own** aggregation index whose trailing fields are the aggregated fields in **alphabetical order** with directions matching the query's last sort — a same-fields index in the wrong tail order is ignored and the total silently reads zero (the June 2026 dashboard "heures non facturées" incident).

**Dependency bumps are a silent trigger too.** The weekly Dependabot minor/patch PRs can change behavior these four depend on with no repo code change: `icalendar`/`vobject` (DAV & vCard serialization shape — note `partie_to_vcard` string-patches vobject's output to vCard 4.0), `google-auth`/`google-cloud-storage` (signed-URL IAM signing), `google-cloud-logging` (whether `json_fields` reaches `jsonPayload`), `opentelemetry-*` (trace ↔ log correlation — and the OTel packages can only move as one atomic set, never individually). When such a PR lands, re-run the relevant check above instead of assuming a version-only diff is safe.

---

## Tech Stack

- **Backend:** Python 3.13 (App Engine Standard)
- **Framework:** Flask 3.1 with Blueprints
- **Database:** Google Cloud Firestore (native mode — not Datastore mode)
- **File Storage:** Firebase Storage (via `firebase-admin` SDK)
- **Authentication:** Firebase Auth (email/password, single-user enforced server-side) + Firebase Phone MFA + Firebase App Check (reCAPTCHA Enterprise provider)
- **Frontend:** Jinja2 templates + HTMX + Alpine.js + Tailwind CSS. No React, no SPA, no deploy-time build pipeline. All frontend JS (htmx, Alpine, Firebase compat SDKs) is **vendored at exact versions in `static/vendor/`** and served same-origin — never load scripts from CDNs (supply-chain rule; CSP excludes CDN origins). **CSS is precompiled, never compiled in the browser**: `static/src/app.input.css` → `static/vendor/app.<hash>.css` (a committed ~40 KB artifact). When class names change, regenerate:
  ```
  npm install --no-save --no-package-lock @tailwindcss/cli@4.3.0 tailwindcss@4.3.0
  npx @tailwindcss/cli@4.3.0 -i athena/static/src/app.input.css -o athena/static/vendor/app.css --minify
  # rename to app.<first-8-of-sha256>.css; update the <link> in base.html +
  # auth/login.html + client/templates/base.html (portal), the PRECACHE list
  # in static/sw.js (and bump STATIC_CACHE), AND the Early Hints lists in
  # security.py (_EARLY_HINTS_*); delete the old hashed file; remove
  # node_modules afterwards. app.input.css scans templates/, routes/,
  # models/ AND client/templates/ (@source lines — source(none) disables
  # auto-detection, so a missing @source silently drops classes).
  ```
  The Firebase App Check bootstrap is also a vendored, hash-named asset (`static/vendor/appcheck-boot.<hash>.js`), configured via a non-executable JSON block in `base.html` — same rules apply if it changes (re-hash, update base.html + sw.js + security.py).
  **Script order in `base.html`/`login.html` is load-bearing:** Firebase/App Check boot → page scripts → htmx → Alpine, all at the end of `<body>`. Execution follows *document order* — the Firebase/App Check boot scripts run synchronously at parse time, and the vendored htmx/Alpine `defer` scripts run in document order at `DOMContentLoaded`; position, not a sync/defer phase, is the guarantee. (Cloudflare Rocket Loader, which used to defer every script while preserving that order, was **disabled at the edge on 2026-07-11** and is not returning.) Never move htmx/Alpine above the App Check boot or above inline component definitions.
  Vendored assets are served `Cache-Control: immutable` (1 year) — **a changed asset MUST get a new filename**; never edit one in place. Dynamically-assembled class names get purged at compile time: keep classes as complete string literals in templates / `routes/*.py` / `models/*.py` (all scanned via `@source`), or safelist them in `app.input.css`.
- **DAV libraries:** `icalendar`, `vobject`. Custom CardDAV/CalDAV/RFC-5545 endpoints served directly from Flask.
- **MCP connector (Phase I, extended by Phase L, the July 2026 audit remediation and the August 2026 mandate):** a hand-rolled, stateless **JSON-response-mode Streamable HTTP** MCP server at `POST /mcp` (no SSE, no sessions) plus an **embedded OAuth 2.1 authorization server** (`mcp/` package), exposing **47 tools** to Claude as a custom connector — **26 read-only** (14 original + 3 Phase-K trust + `list_time_entries`/`list_expenses`/`list_deletions` + the August additions `list_invoices`/`get_invoice`/`get_coverage_report` + the lot-Q additions `get_reference_vocabulary`/`find_imported`/`get_import_audit`) and **21 writes in four families**. **CREATE:** notes (`create_note`/`append_to_note`), entities (`create_task`/`create_hearing`/`create_time_entry`/`create_expense`), lot Q's `create_partie` and `create_dossier`, and the dossier recorders (`complete_dossier` fill-only-if-empty, `record_signification`, `record_prescription_event`). **CORRECT** — these REPLACE the value they name, an omitted field being left alone: `update_partie`, `update_dossier`, `update_time_entry` and `update_expense` (the last two only while the entry is un-invoiced — the models refuse otherwise), plus `complete_task`, the ONE status change (terminée/annulée/en_cours — never `à_faire`). **RECLASSIFY** (août 2026) — `set_time_entry_phase`, `set_expense_phase` and their `_bulk` twins (≤ 50 rows a call): the ONLY writes that reach a row already carried to an invoice, and they can touch NOTHING but `phase`/`sous_phase`. Safe there because that pair is on no invoice — the line items are independent copies without it — so the billing freeze, which exists to protect the money figures, never covered it; it feeds the dossier's budget-vs-actuals view, which counts billed work. The models write it through a four-key partial `update()`, never the merged `set()` of `update_*`, and `update_time_entry`/`update_expense` keep their refusal INTACT. **IMPORT:** `import_invoice`, which recreates an invoice the practice's previous system issued, under its own number and date, **without ever reading or advancing the year counter**, from real un-invoiced sources only, always into `brouillon`. **No tool can delete anything, set an invoice status, or record a payment.** `mcp.tools.EDIT_TOOLS` ⊆ `WRITE_TOOLS` is the subset that replaces a stored value and is what DERIVES `destructiveHint` — the annotation stopped being a family constant the day an edit shipped. Every write runs through `mcp/write_support.run_write` (`dry_run` preview + 24 h `idempotency_key` replay via the keyed `mcp_idempotency` collection). **Zero new Python dependencies** — stdlib (`secrets`, `hashlib`, `base64`) + packages already pinned (Flask, flask-limiter, flask-wtf). Two kill switches: `MCP_ENABLED` (default `"true"`; `false` → every `/mcp` + `/oauth/*` route 404s) and `MCP_WRITE_ENABLED` (default `"true"`; `false` → the write tools vanish from `tools/list`, are refused at `tools/call`, and the consent checkbox disappears — reads unaffected).
- **Portail client (spec « L1 », July 2026 — naming collision: the CLAUDE.md "Phase L" is the MCP note writes; this is the separate portail series L1→L2→L3):** a **second App Engine service `portail`** (host `portail.poirierlavoie.ca`, public — NO Cloudflare Access) through which an invited client transmits documents into a **quarantine GCS bucket**. Source package **`athena/client/`** (user decision 2026-07-25 — the lawyer-facing service may later move under `juriste/`), deployed from the SAME `athena/` source dir via `athena/portail.yaml`; `client.wsgi:app` registers ONLY the portal blueprint (route-map isolation pinned by test). Client auth = Firebase **email-link** (single-use, bound to the invited email) + a per-request re-read of the invitation document in the **named Firestore database `portail`** (single-writer: only the main service writes; the portal reads and signals via **Cloud Tasks** queue `portail`). Outbound email = **Microsoft Graph** client-credentials (`utils/graph.py` + `utils/courriel.py` — no msal; the phase-J foundation). Reviews happen in the main service's « **Réception** » page; nothing enters Firestore/canonical storage without an explicit « Verser » click, restricted to the documents vocabulary (11 MIME types — 2026-08-11 widened the original 6 with ZIP/.eml/.msg, 2026-08-13 added Excel .xls/.xlsx — ≤ 200 MB since 2026-08-12, the versement being a GCS-side copy). One new Python dependency: `google-cloud-tasks` (+ `requests` promoted to a direct pin).
- **Markdown:** `Markdown` + `bleach` libraries for rendering note content (rendered via Jinja `markdown` filter).
- **PDF:** `reportlab` (pure Python — do NOT use `weasyprint`; it requires cairo/pango system libs unavailable on App Engine Standard).
- **Word templates (Phase H — gabarits):** user-managed `.docx` templates filled by a **stdlib-only engine** (`utils/docx_fill.py`: `zipfile` + `re` + `io` — direct string substitution on the XML zip entries, every other entry copied byte-identical). **`docxtpl`/`python-docx` are explicitly rejected** — their load/save round-trip rewrites enough of the OOXML package that Word refuses to open letterhead templates with multiple headers/footers, `titlePg` sections, and embedded fonts. Zero new Python dependencies. **Phase H.3 (August 2026)** adds the `rich_values` hook: a note's **Markdown body converts to real Word formatting** (headings/bold/italic/lists/blockquotes/**tables**) via `utils/markdown_docx.py` (markdown→HTML with the SAME pipeline as the screen → stdlib HTMLParser → OOXML block writer; direct formatting only, never named styles, never new zip parts) — the host paragraph carrying `{{note.contenu}}` ALONE is replaced by the converted blocks; any unsafe host or converter failure **degrades to the plain fill** (sigils visible, document valid, never corrupt). **The full placeholder inventory (all `{{…}}` names + syntax) is in [`GABARITS_PLACEHOLDERS.md`](GABARITS_PLACEHOLDERS.md).**
- **Hosting:** Google App Engine Standard, Python 3.13 runtime, F2 instance class.
- **CDN / edge:** Cloudflare **Pro plan** (Full Strict SSL, Origin Certificate, Argo Smart Routing, **Early Hints** — `security.py` emits the `Link` preload headers Cloudflare converts to HTTP 103; **Rocket Loader was disabled at the edge on 2026-07-11**). The App Engine firewall accepts only Cloudflare IP ranges, so the edge is not bypassable (see Security Rules → Edge defense in depth).
- **PWA:** manifest + service worker (`static/sw.js`) for offline fallback + stale-while-revalidate caching of `/static/vendor/` and `/static/icons/` assets (never authenticated HTML); Trusted Web Activity wrapper for Android (assetlinks.json served at `/.well-known/assetlinks.json`).
- **Observability:** structured logging to Cloud Logging (`utils/logging_setup.py` — request-context fields + PII redaction filter) and distributed tracing via OpenTelemetry, exported **over OTLP/gRPC to `telemetry.googleapis.com`** (`utils/tracing_setup.py` — Flask/requests/Jinja2 auto-instrumentation, 10% prod sampling, PII-sanitizing exporter). **`athena/OBSERVABILITY.md` is the event/span registry — read it before adding log events or spans.**
- **CI/CD:** Google Cloud Build trigger on GitHub push to main; `cloudbuild.yaml` runs the pytest suite as a deploy gate (hash-locked install), deploys, and prunes old versions. GitHub Actions provide security scanning (CodeQL, OSV-Scanner, Trivy, Bandit, dependency-review, OpenSSF Scorecard) and Dependabot keeps pins moving (weekly grouped minor/patch PRs).

### Python dependencies (`requirements.in` → `requirements.txt`)

Direct dependencies live in **`athena/requirements.in`** with **exact pins** (`==X.Y.Z` — wildcards break OSV-Scanner version resolution). `athena/requirements.txt` is a **generated, hash-locked lockfile — never edit it by hand**. To change a dependency, edit `requirements.in` and re-lock:

```
uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt
```

(Compiling over the existing output file preserves unrelated transitive pins.) Production installs run with `PIP_REQUIRE_HASHES=1` / `PIP_NO_DEPS=1` (set in `app.yaml`), so an unhashed or out-of-band package cannot deploy. CI/dev-only tools (pytest) live in `athena/requirements-dev.txt`, which is never deployed.

Direct deps beyond the original core set: `google-cloud-logging`, the OpenTelemetry stack (`opentelemetry-api`/`sdk` 1.44.0 + **OTLP/gRPC exporter 1.44.0** + `opentelemetry-exporter-credential-provider-gcp` 0.65b0 + GCP propagator 1.12.0 + Flask/requests/Jinja2 instrumentation 0.65b0 + an explicit `opentelemetry-resourcedetector-gcp` 1.14.0 pin — **all paired, they move as ONE atomic edit**: each instrumentation package hard-pins its siblings at `==`, so a single-package bump is unresolvable and Dependabot cannot automate it), `Pillow` (transitive via reportlab, pinned explicitly for CVE hygiene), and `defusedxml`. The GCP trace exporter (and with it `google-cloud-trace`) **left the graph on 2026-07-30** — export goes to `telemetry.googleapis.com` over OTLP now; see OBSERVABILITY.md for why gRPC and not HTTP. **There is no longer a `setuptools` pin** — contrib dropped `pkg_resources` at 0.49b0, so setuptools left the graph entirely (2026-07-30), which is what closed CVE-2026-59890 on all three scanning surfaces at once instead of suppressing it on each.

(`google-cloud-storage` is listed for completeness; storage operations actually go through `firebase-admin.storage`, which uses the same underlying client.)

---

## Architecture Rules

1. **SINGLE USER.** Exactly one authorized email (`AUTHORIZED_USER_EMAIL` env var). No multi-tenancy, no registration, no roles. Every endpoint that mutates state verifies the session via `@login_required` (Firebase Auth + server-side session). DAV endpoints use a separate HTTP Basic auth.
2. **Firestore is flat.** Despite the single-user nature, Firestore **collections are top-level** (`parties`, `dossiers`, `tasks`, `hearings`, `notes`, `protocols`, `invoices`, `timeentries`, `expenses`, `documents`, `doc_templates`, `dav_sync`, `counters`, `ref_greffes`, `ref_juridictions`, `ref_palais`, the Phase-I OAuth collections `oauth_clients`, `oauth_codes`, `oauth_tokens`, the Phase-K trust collections `trust_accounts`, `trust_transactions`, `trust_reconciliations`, plus the July 2026 additions `audit_events` (append-only deletion journal) and `mcp_idempotency` (MCP write-replay cache), and `budgets` (August 2026 — per-dossier phase budgets, append-only versioned)). They are **not** nested under `users/{userId}/...`. Firebase Storage paths, however, **do** use `users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}` (with `userId` from the Firebase Auth `uid` claim).
3. **Bilingual code/UI split.** All user-facing text (labels, buttons, placeholders, errors, toasts, empty states) is in **French**. All code (variable names, function names, comments, docstrings) is in **English**.
4. **Currency in integer cents.** `15000` means $150.00. Never use floats for money. Use `Decimal` only for tax computation intermediates, convert to int cents (with `ROUND_HALF_UP`) before storage.
5. **Timestamps UTC.** Stored as UTC `datetime` with timezone info. Displayed in `America/Montreal` via the `to_mtl` Jinja filter (registered from `tz.py`).
6. **UUIDv4 document IDs.** Generated server-side. Never reuse IDs. **Documented exceptions:** the OAuth collections use the lookup key as the doc ID — `oauth_clients/{client_id}`, `oauth_codes/{sha256(code)}`, `oauth_tokens/{sha256(token)}` — so raw credentials are never stored and validation is one keyed `get()`; `mcp_idempotency/{sha256(tool:key)}` follows the same keyed-`get()` pattern (the raw idempotency key is never stored).
7. **Every Firestore doc has `created_at`, `updated_at`, `etag`** (etag = UUIDv4 regenerated on every write, used for DAV `If-Match` conditional requests). Folders, the three OAuth collections, `audit_events` and `mcp_idempotency` are exceptions: no `etag` (and the last two are write-once — never updated after creation).
8. **HTMX first.** Dynamic interactions use HTMX. Flask endpoints check `request.headers.get("HX-Request")`/`HX-Target` and return HTML fragments for HTMX requests, full pages otherwise.
9. **Mobile-first.** Design for 375px viewport first. Breakpoints at 768px (tablet) and 1024px+ (desktop). Touch targets minimum 44px.
10. **Minimalist visual language.** Near-white `#FAFAFA` backgrounds, near-black `gray-900` text, `indigo-600` accent. Generous white-space. **Typography (August 2026, revised 2026-08-07): Noto Sans for the ENTIRE UI; Noto Serif ONLY for document-reading surfaces** — the rendered note content (`.note-content` — note detail + the Analyse tab) and the reportlab PDF exports. The boundary is pure CSS in `static/src/app.input.css`: `body { font-family: var(--font-sans) }` + a single `.note-content { font-family: var(--font-serif) }` rule — no per-template font classes (an earlier serif-body design needed ~70 `font-sans` chrome edits; all removed when the boundary inverted). `font-sans`/`font-serif` via the `@theme` block remain the per-element escape hatch (first use needs a recompile). The fonts are vendored (SIL OFL — no CDN): variable woff2 ×4 in `static/vendor/` (Noto Sans v42 + Noto Serif v33, roman + italic, latin subset) for the web, static Noto Serif TTF ×2 in `utils/fonts/` for reportlab (provenance + sha256 in `utils/fonts/README.md`). The `.woff2` MIME type needs the dedicated `static_files` handler ABOVE `/static/vendor` in BOTH yaml files (nosniff would reject the default octet-stream). Early Hints/portal preload the SANS roman only (serif loads on demand on note pages). Emails keep their client-safe stacks (webfonts don't load in mail clients); generated `.docx` take their fonts from the user's own gabarit templates (the fill engine never writes `rFonts`). **Icons (August 2026): Material Symbols Outlined as a vendored subsetted variable icon font** (ligatures — `material-symbols-outlined-v364-*.woff2`, ~24 KB, 40 glyphs, Apache-2.0 licence file beside it) rendered ONLY through the `ms()` Jinja global (`utils/icons.py` — validates each name against the canonical `MATERIAL_ICONS` set, emits `aria-hidden`/`translate="no"`, sizes via the hand-written `.ms-N` classes in `app.input.css`); `tests/test_icons.py` pins template usage == the vendored subset both ways and forbids stray inline SVG (only the 8 SVG/CSS spinners remain — animated arcs, kept on purpose). Adding an icon = add the name to `MATERIAL_ICONS`, regenerate the subset (css2 `icon_names=` URL in `utils/fonts/README.md`), NEW hashed filename + full asset fan-out. Never use icon-font ligatures in emails.
11. **DAV-ready schemas.** Parties, hearings, tasks, notes carry stable DAV UIDs (`vcard_uid`, `vevent_uid`, `vtodo_uid`, `vjournal_uid`) set at creation and never changed.

---

## Security Rules

- **Security headers on every response** (via `security.py` `_add_security_headers` `after_request` hook):
  - `Content-Security-Policy` — **enforced** since 2026-07-11 (see `build_csp`/`csp_nonce` in `security.py`; flipped after verifying against 90 days of report-only `/csp-report` data — only `script-src` ever reported violations, then hardened the same day). The policy is **assembled per request** so a fresh **nonce** can be spliced into `script-src`: `'self' 'nonce-<per-request>' 'unsafe-eval'` + the Google origins the App Check SDK loads reCAPTCHA Enterprise from (`gstatic.com`, `apis.google.com`, `google.com`) — **no `'unsafe-inline'`, no `ajax.cloudflare.com`, no script CDN origins** (assets are vendored). The app's own inline `<script>` blocks carry `nonce="{{ csp_nonce }}"` (the `csp_nonce` Jinja global = the header value), so an injected/un-nonced inline script is blocked; inline `on*` handlers were refactored to `data-` attributes wired via `addEventListener` (a nonce cannot authorize a handler attribute). `'unsafe-eval'` is retained for Alpine's `new Function()` (dropping it needs `@alpinejs/csp` + an expression rewrite); `style-src` keeps `'unsafe-inline'` for reCAPTCHA's dynamic inline styles. **Rocket Loader is disabled at the edge (since 2026-07-11).** Violations are posted to `/csp-report` (`report-uri`, still active under enforcement) and logged as `csp_violation` security events. **`form-action` is `'self'` everywhere except `/oauth/authorize`**, where `_form_action_for` adds `https://claude.ai https://claude.com` (and loopback outside production): `form-action` governs the entire redirect chain of a submission, and the consent POST 302s to Claude's callback, so `'self'` alone silently breaks connector authorization.
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload` (2 years)
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
  - `Cache-Control: no-store, no-cache, must-revalidate, private`
  - `Pragma: no-cache`
- **CSRF** on every POST/PUT/DELETE via `flask-wtf` `CSRFProtect`. HTMX requests include the token via `hx-headers` on `<body>`. Failures are logged as `csrf_failure` security events and return 400.
- **Rate limiting** on `/auth/login` (configurable via `RATE_LIMIT_LOGIN`, default `5 per minute`) via `flask-limiter` (in-memory store). The rate-limit key is `CF-Connecting-IP` (real client IP behind Cloudflare; falls back to the peer address) — only trustworthy because the firewall guarantees traffic transits Cloudflare.
- **Request size limits:** 25 MB global cap (`MAX_CONTENT_LENGTH` in `config.py` — now a pure backstop); EVERY route is capped at 1 MB and DAV/well-known paths at 5 MB by `_enforce_request_size` in `security.py` (`UPLOAD_PATHS` is empty since 2026-08-12 — the document upload went direct-to-GCS, so no multipart route remains). **Phase H exemption:** template upload/replace (`POST /gabarits/` and `POST /gabarits/<id>`) get 10 MB (`_is_template_upload_path`); the generation POST (`/gabarits/generer`) and every other gabarit sub-route stay at 1 MB.
- **Secrets live in Google Cloud Secret Manager**, not in `app.yaml`: `flask-secret-key`, `firebase-api-key`, `dav-password-hash`, `cf-origin-secret`, plus (portail L1) `portail-secret-key` (portal session key — DISTINCT from the main one; resolved lazily by `client/config.py`, never at import) and `graph-client-secret` (Graph outbound email — main service only, optional/fail-open). `config.py` resolves the main set at startup when `ENV=production`; locally they come from `.env` env vars (`SECRET_KEY`, `FIREBASE_API_KEY`, `DAV_PASSWORD_HASH`, `CF_ORIGIN_SECRET`, `PORTAIL_SECRET_KEY`, `GRAPH_CLIENT_SECRET`).
- **Firebase Storage URLs:** always signed, 15-minute expiry. Never expose raw bucket URLs to the client. The signing path uses `iam.signBlob` via `google-auth` impersonation when running on App Engine.
- **DAV authentication:** HTTP Basic Auth with bcrypt-hashed password (`DAV_PASSWORD_HASH`, from Secret Manager in prod). Username is the same as `AUTHORIZED_USER_EMAIL`. **Separate** from Firebase Auth.
- **MCP authentication (Phase I):** `POST /mcp` requires an OAuth 2.1 **opaque bearer token** (32 bytes `secrets.token_urlsafe`, stored as SHA-256 hex doc IDs in `oauth_tokens` — no JWTs, no new crypto deps). Access tokens live 60 min, refresh tokens 30 days with **rotation** (a replayed rotated refresh token revokes its whole family; a replayed authorization code does too). The embedded AS (`mcp/oauth.py`) offers **open-but-neutered DCR**: `/oauth/register` accepts only Claude's callback URLs (`https://claude.ai|claude.com/api/mcp/auth_callback`; localhost additionally outside production), and the consent screen sits behind `@login_required` (session + MFA), so no third party can complete a flow. PKCE S256 only; public clients only; `hmac.compare_digest` for PKCE and cache comparisons. Bearer failures feed a **per-IP brake** (20 invalid tokens / 15 min → 429 before Firestore is touched) mirroring the DAV brake, with a 5-min HMAC-keyed success cache (revocation lag ≤ 5 min on a warm instance). CSRF exemptions: `/mcp`, `/oauth/register`, `/oauth/token`, `/oauth/revoke` — **not** the `/oauth/authorize` POST. Rate limits: register 10/h, token + revoke 60/h, `/mcp` 240/min (all keyed by `CF-Connecting-IP`). An `Origin` header on `/mcp` must be `claude.ai`/`claude.com`/the canonical origin (DNS-rebinding defense). Never log tokens, codes, or verifiers. Break-glass: `MCP_ENABLED=false` (kill switch), `MCP_WRITE_ENABLED=false` (writes only), or `python -m scripts.revoke_mcp_tokens`.
- **MCP write scope (Phase L, élargie juillet 2026):** `athena:write` is in `SCOPES_SUPPORTED` (so it is advertised in the RFC 8414/9728 metadata and a client may request it), but **it is granted only when the user ticks « Autoriser les écritures (création seulement) » on the consent screen** (the label enumerates every write family — the screen is the only human-readable description of what the token can do). `_validate_authorize_request` deliberately strips it — the value it returns is always the read baseline — and `authorize_decision` re-adds it solely from `request.form["grant_write"]`, so the hidden (attacker-modifiable) `scope` input can never escalate and the page the user read always matches the grant that is minted. `SCOPE_READ` is force-included in every grant, because `bearer.py` demands it on **every** `/mcp` call and a write-only token would be a permanently dead connector. Enforcement is per tool in `endpoint._tools_call`, **before** argument validation, raising `bearer.ScopeRequired`, which is caught **ahead of** the generic `except Exception` (caught there it would degrade to a 200 "internal error" with no 403 and no `WWW-Authenticate`). The bearer **success cache carries the granted scope** and both the warm and cold paths publish `g.mcp_scopes`; `granted_scopes()` raises rather than defaulting when it is unset. Write calls additionally run `bearer.revalidate_for_write`, one keyed Firestore read that bypasses the cache so a revoked token cannot mutate for the remaining ≤5 min of its cache window. **Scope is frozen at issuance and copied verbatim across refresh rotation** — adding or removing write always requires revoke + re-consent, never a code change.
- **Edge defense in depth — all traffic must transit Cloudflare.** Three layers:
  1. **App Engine firewall** allows only Cloudflare's published IP ranges (ops-side; configured in GCP).
  2. **Origin secret** (`_enforce_origin_secret` in `security.py`): when `CF_ORIGIN_SECRET` is set, every request must carry the matching `X-Origin-Auth` header, injected at the edge by a Cloudflare Transform Rule — defeats direct-to-App-Engine access with a spoofed Host. Unset = disabled (local dev). **ARMED 2026-08-11** — it had never run before that date (`cf-origin-secret` did not exist; the zone had no `http_request_late_transform` ruleset), which is why `CF-Connecting-IP` was forgeable and all three brute-force brakes (login 5/min, DAV 10/15 min, MCP bearer 20/15 min) were bypassable by anyone fronting `athena-pallas.nn.r.appspot.com` with *their own* Cloudflare zone. The edge half is a zone-wide Transform Rule (`expression: true`, ruleset `3b9fcca30bb54b2cb9d239127de4270f`) whose firing was proven with Cloudflare's request tracer on 14 paths across both hosts **before** the secret was created — arming first would have 403'd the whole site (see the Known Gotcha for the order and the trailing-newline trap).
  3. **Host check** (`block_appspot` in `main.py`): rejects `*.appspot.com` hosts (403). Weakest layer (Host is spoofable) but free.
  App Engine internal paths (`/_ah/` — warmup, cron) never transit Cloudflare and are **exempt from layers 2 and 3**. **Cloud Tasks / cron dispatches (portail L1)** arrive from the internal address `0.1.0.2` with `X-AppEngine-QueueName` / `X-Appengine-Cron` headers that **App Engine strips from ALL external traffic** — `security.is_appengine_internal_request()` gates a bypass of layers 2 and 3 on their presence (spoof-proof), and each machine handler re-checks its own header value (which queue, cron true). The App Engine firewall must ALLOW `0.1.0.2/32` (ops; without it tasks/cron are silently blocked at layer 1). The `portail` service shares the app-level firewall (Cloudflare ranges) but has **no** Cloudflare Access application (public host — WAF + rate limiting at the edge instead).
- **Cloudflare Access is deliberately NOT used** (decision 2026-08-11). Every doc in this repo — including the public privacy policy — claimed until that date that Zero Trust fronted `/dav/*`; it never did, and no commit ever provisioned it. The claim was removed rather than made true, for reasons worth keeping: **DavX5 has no custom-header setting** (verified on device), so an Access service token cannot be presented at all. Its one mTLS-capable path is a **client certificate** (Advanced login → Android KeyChain), and Access mTLS *is* available on this account (probed: the API refuses on the certificate, `12130`, never on the plan — a widely-repeated claim that it is Enterprise-only is wrong here). But **Cloudflare enables mTLS per HOSTNAME, not per path**, and `athena.poirierlavoie.ca` also serves the web UI and `/mcp` — so the isolation would require giving DAV its own hostname. Weighed against that, `/dav/*` keeps: App Engine firewall + **the origin secret armed the same day** + bcrypt Basic auth + the per-IP brake. That ordering matters — arming layer 2 is what made `CF-Connecting-IP` trustworthy again, and therefore made the DAV brake mean something. **No code reads `CF-Access-*`** (zero occurrences outside prose), so adding Access later remains an edge-only change requiring no deploy.
- **Firebase App Check** verifies attestation tokens on HTMX requests (`X-Firebase-AppCheck` header) when `RECAPTCHA_ENTERPRISE_SITE_KEY` is configured. Static, DAV, well-known, and `/auth/*` paths are exempt; non-HTMX (full page) requests are protected by session + CSRF. Fail-open when unconfigured, but logs a loud warning in production.
- **Session establishment hardening** (`auth.py`): ID tokens are verified with `check_revoked=True`, and only tokens minted by an interactive sign-in **within the last 10 minutes** (`auth_time` replay guard) may create a session. Sessions are server-side with a `SESSION_LIFETIME_HOURS` expiry (default 12 h); cookies are `HttpOnly`, `SameSite=Lax`, `Secure` in prod.
- **Phone MFA** required for the single authorized email when `REQUIRE_MFA=true` (default **true**, and set `"true"` in production `app.yaml`; the verifier in `auth.py` checks for `sign_in_second_factor` in the decoded token).
- **Input sanitization** via `security.sanitize()` — strips HTML tags, enforces max lengths. Called from `_sanitize_data()` in every model.
- **Open-redirect guard:** `security.safe_internal_redirect(target, fallback)` validates every `return_to` value (same-origin path only; blocks `//host`, schemes, backslash tricks). Rejections are logged without the URL.
- **Do not log PII.** Enforced in code, not just by convention: all logging goes through `utils/logging_setup.py`, whose `RedactionFilter` drops sensitive keys, scrubs emails/phones/postal codes, and escapes control characters (log-injection defense, CWE-117) in messages, `json_fields`, and tracebacks; `utils/tracing_setup.py` applies the same scrubbing to exported spans. Use the typed helpers (`log_auth_event`, `log_security_event`, `log_dav_operation`, `log_dossier_event`, `log_mcp_event`, `log_template_event`, `log_unexpected`) instead of raw `logger.*` calls — the event vocabulary is documented in `athena/OBSERVABILITY.md`. When interpolating a user-controlled value (URL path segment, request field) into a log message, wrap it in `sanitize_log_value(...)`.

---

## Code Style & Conventions

- **Python type hints** on all function signatures.
- **Flask blueprints** — one per module (`parties.py`, `dossiers.py`, etc.), registered in `main.py` via `create_app()`.
- **Firestore errors** wrapped in `try/except`. Return user-friendly French error messages; log the raw exception.
- **Consistent CRUD pattern** across models:
  ```python
  def create_X(data: dict) -> tuple[Optional[dict], list[str]]
  def get_X(x_id: str) -> Optional[dict]
  def list_X(...) -> list[dict]
  def update_X(x_id: str, data: dict) -> tuple[Optional[dict], list[str]]
  def delete_X(x_id: str) -> tuple[bool, str]
  ```
- **DAV serialization** (`X_to_vcard`, `X_to_vevent`, etc.) lives in the model alongside CRUD.
- **Validation and normalization** are separate model-level concerns. The pipeline in `create_*`/`update_*` is `_normalize` (where applicable) → `_sanitize_data` → `_validate`.
- **CTag bumping** is explicit at every call site that mutates DAV-exposed data. See the DAV section below.
- **Module-level `_SYNCING` set** prevents infinite bidirectional sync loops (task ↔ protocol step). Both `models/task.py` and `models/protocol.py` declare their own.

---

## Directory Structure

```
.
├── athena/
│   ├── app.yaml                    # App Engine config (python313, F2, gunicorn sizing, warmup, static handlers)
│   ├── requirements.in             # Direct deps, exact pins — edit THIS, then re-lock
│   ├── requirements.txt            # GENERATED hash-locked lockfile (uv pip compile) — never hand-edit
│   ├── requirements-dev.txt        # CI/dev-only deps (pytest) — never deployed
│   ├── main.py                     # Flask app factory, blueprints, Jinja filters (to_mtl, phone, jsattr,
│   │                               # markdown), error handlers, /_ah/warmup, /csp-report, appspot block
│   ├── config.py                   # Env + Secret Manager configuration class (incl. firm info + tax numbers)
│   ├── auth.py                     # Firebase Auth verification, @login_required, MFA gate, token replay guard
│   ├── security.py                 # Security headers, CSRF, rate limiting, App Check, origin secret,
│   │                               # request size caps, sanitize(), safe_internal_redirect(),
│   │                               # Early Hints Link headers (_EARLY_HINTS_*)
│   ├── tz.py                       # UTC ↔ America/Montreal helpers
│   ├── pagination.py               # Pagination helpers: legacy page mode, cursor mode
│   │                               # (encode/decode/trail), and keyset_page — paging a
│   │                               # Python-materialized list by ORDER KEY, never offset
│   ├── manifest.json               # PWA manifest
│   ├── robots.txt
│   ├── OBSERVABILITY.md            # Structured-logging event registry + tracing conventions (source of truth)
│   ├── .gcloudignore               # Keeps tests/venv/dev/non-runtime files out of the deployed bundle
│   │
│   ├── portail.yaml                # App Engine config of the SECOND service « portail » (F1, SA
│   │                               # portail-svc, entrypoint client.wsgi:app; deployed from athena/)
│   │
│   ├── client/                     # Portail client (spec L1) — the portal service's own package.
│   │   ├── __init__.py             # portail_bp + its own Limiter (never the main service's)
│   │   ├── wsgi.py                 # app = create_portail_app() — ONLY the portal blueprint
│   │   ├── app.py                  # Factory: pa_portail cookie + portail-secret-key, 1 MB cap,
│   │   │                           # CSRF (session-lifetime tokens), guard wiring
│   │   ├── config.py               # Annexe C constants (importable by BOTH services) + LAZY
│   │   │                           # secret functions (never resolved at import)
│   │   ├── security.py             # Portal CSP (§10, no 'unsafe-eval' — no Alpine) + headers +
│   │   │                           # fail-open App Check on POSTs
│   │   ├── routes.py               # /entree /session /api/renvoi /documents /api/televersement
│   │   │                           # /api/finaliser /confirmation /sante + the §6.5 guard
│   │   ├── services/               # invitations.py (read-only named DB), stockage.py (sanitize,
│   │   │                           # resumable sessions, create-only envelope), taches.py (enqueue)
│   │   └── templates/              # base/entree/documents/ouverture/confirmation/erreur
│   │                               # (français, vanilla JS — aucun Alpine : CSP sans 'unsafe-eval')
│   │
│   ├── services/                   # Main-service orchestration (multi-subsystem operations)
│   │   └── portail_emission.py     # Invitation émission/renvoi: Firebase user + claim merge +
│   │                               # email-link + Graph email (manual-link fallback)
│   │
│   ├── models/                     # Firestore data access layer
│   │   ├── __init__.py             # Exposes `db` (Firestore client singleton) + aggregation_values() helper
│   │   ├── partie.py               # Contacts (clients, opposing parties, counsel, experts…)
│   │   ├── dossier.py              # Case files
│   │   ├── time_entry.py           # Billable hours
│   │   ├── expense.py              # Expenses
│   │   ├── invoice.py              # Invoices + line items subcollection
│   │   ├── hearing.py              # Court dates
│   │   ├── task.py                 # Tasks (VTODO)
│   │   ├── note.py                 # Dossier notes (VJOURNAL)
│   │   ├── protocol.py             # Case protocols + steps subcollection (incl. CQ/CS templates)
│   │   ├── document.py             # Document metadata (Firebase Storage files)
│   │   ├── folder.py               # Document folders (nested, Firestore-only)
│   │   ├── doc_template.py         # Gabarits .docx (Phase H): CRUD + Storage + placeholder extraction
│   │   ├── reference.py            # Read-only: ref_greffes, ref_juridictions
│   │   ├── audit_event.py          # Append-only deletion journal (July 2026): record_deletion
│   │   │                           # (best-effort, AFTER the committed delete) + list_recent
│   │   ├── portail_invitation.py   # Invitations (NAMED database « portail », lazy client, single
│   │   │                           # writer = main service; poser_accuse transactional test-and-set)
│   │   ├── budget.py               # Budgets par phase (août 2026): append-only VERSIONNÉ (jamais
│   │   │                           # d'update/delete — preuve déontologique §10 Phase O), lignes
│   │   │                           # par sous-code, agrégation du réalisé + vue budget-vs-réalisé
│   │   ├── admin_ledger.py         # Comptabilité d'administration (août 2026): compte d'opérations +
│   │   │                           # carte de crédit — dates libres, MODIFIABLE jusqu'au verrou de
│   │   │                           # conciliation, soldes calculés À LA LECTURE (aucun solde gelé),
│   │   │                           # ventilation TPS/TVQ, paiement de carte 2 jambes, reçus
│   │   └── trust.py                # Fidéicommis (Phase K): accounts + append-only register + reconciliation
│   │
│   ├── routes/                     # Flask blueprints (web UI)
│   │   ├── __init__.py
│   │   ├── auth_routes.py          # /auth/*
│   │   ├── dashboard.py            # /
│   │   ├── parties.py              # /parties/*
│   │   ├── dossiers.py             # /dossiers/*  (incl. /dossiers/parse-court-file)
│   │   ├── time_expenses.py        # /temps/*  (heures + dépenses)
│   │   ├── invoices.py             # /factures/*
│   │   ├── hearings.py             # /audiences/*
│   │   ├── tasks.py                # /taches/*
│   │   ├── notes.py                # /notes/*
│   │   ├── protocols.py            # /protocoles/*
│   │   ├── documents.py            # /documents/*  (independent of dossier URL; dossier_id passed as query/form arg)
│   │   ├── doc_templates.py        # /gabarits/*  (Phase H: lifecycle + HTMX generation popup)
│   │   ├── trust.py                # /fideicommis/*  (Phase K: journal, carte, comptes, conciliations, exports)
│   │   ├── budgets.py              # /budgets/*  (août 2026: formulaire versionné, historique,
│   │   │                           # exports PDF estimation/suivi)
│   │   ├── admin_ledger.py         # /administration/*  (août 2026: journal à solde courant,
│   │   │                           # écritures modifiables jusqu'au verrou, encaissement de
│   │   │                           # factures → record_payment — UNIQUE écrivain d'un
│   │   │                           # paiement depuis le 2026-08-17 —, reçus direct-à-GCS,
│   │   │                           # conciliations banque ET carte, exports CSV + PDF légal)
│   │   ├── comptabilite.py         # /comptabilite/  (août 2026: le hub « Comptabilité » —
│   │   │                           # composeur LECTURE SEULE des deux listes de comptes,
│   │   │                           # fail-closed par section ; l'entrée de nav comptable)
│   │   ├── reception.py            # /reception/*  (portail L1: revue des lots, versement restreint,
│   │   │                           # invitations, pastille de nav en cache 60 s fail-open ;
│   │   │                           # L3: onglet Ouvertures — création / fusion champ par champ)
│   │   ├── taches_portail.py       # /taches/portail/*  (MACHINE, CSRF-exempt: gestionnaire Cloud
│   │   │                           # Tasks + réconciliation cron — gardes X-AppEngine-*)
│   │   ├── taches_bookings.py      # /taches/bookings/sync  (L2, MACHINE, cron: synchro
│   │   │                           # « Bookings with me » → hearings à_confirmer — garde X-Appengine-Cron)
│   │   └── taches_outlook.py       # /taches/outlook/sync  (MACHINE, cron 10 min: miroir des
│   │                               # audiences confirmées → calendrier Outlook du juriste ;
│   │                               # Firestore-lecture-seule, garde X-Appengine-Cron)
│   │
│   ├── dav/                        # DAV protocol endpoints
│   │   ├── __init__.py             # Principal + calendar/addressbook home-set; root PROPFIND lists collections dynamically
│   │   ├── carddav.py              # /dav/addressbook/ — contacts
│   │   ├── dossier_collections.py  # /dav/dossier-{id}/ AND /dav/general/ — one scoped
│   │   │                           #   implementation: VEVENT + VTODO + VJOURNAL
│   │   ├── dav_auth.py             # HTTP Basic Auth decorator
│   │   ├── xml_utils.py            # Namespace tags, multistatus builders, propfind body parser
│   │   └── sync.py                 # CTag / sync-token / tombstone management
│   │
│   ├── mcp/                        # MCP connector — 47 tools: 26 read + 21 writes
│   │   ├── __init__.py             # mcp_bp + oauth_bp blueprints, register_mcp(app), constants,
│   │   │                           # MCP_ENABLED kill switch (404s every route when off)
│   │   ├── jsonrpc.py              # JSON-RPC 2.0 parsing, response/error envelopes, error codes
│   │   ├── endpoint.py             # POST /mcp dispatcher: initialize/ping/tools list+call
│   │   ├── bearer.py               # @mcp_auth_required, WWW-Authenticate challenges,
│   │   │                           # per-IP invalid-token brake (mirrors dav_auth brake)
│   │   ├── oauth.py                # RFC 8414/9728 metadata, /oauth/register|authorize|token|revoke
│   │   ├── store.py                # Firestore persistence: oauth_clients / oauth_codes / oauth_tokens
│   │   ├── tools.py                # TOOLS registry, subset JSON-Schema validator (incl. anyOf +
│   │   │                           #   union types for outputs), money/date helpers
│   │   ├── output_schemas.py       # Declared outputSchema per tool — a MUST-conform contract,
│   │   │                           #   enforced by tests/test_mcp_output_schemas.py
│   │   ├── write_support.py        # Write protocol (July 2026): run_write — dry_run preview +
│   │   │                           #   24 h idempotency replay (keyed mcp_idempotency collection)
│   │   ├── coverage.py             # Coverage-check registry + PURE predicates (imports no
│   │   │                           #   model, so the suite tests without Firestore)
│   │   ├── import_audit.py         # Lot Q: les 7 contrôles IMP-01..07 de get_import_audit,
│   │   │                           #   PURS comme coverage.py (aucun import de modèle)
│   │   └── handlers.py             # 47 tool implementations (26 read + 21 writes) over models/* + utils/*
│   │
│   ├── utils/                      # Utility modules
│   │   ├── __init__.py
│   │   ├── fonts/                  # Noto Serif static TTFs (Regular/Bold) for reportlab +
│   │   │                           # OFL.txt + README.md (sources, release tag, sha256) —
│   │   │                           # outside static/ on purpose (never publicly served)
│   │   ├── deadlines.py            # Quebec art. 83 C.p.c. judicial deadline calc
│   │   ├── recurrence.py           # Séries récurrentes (pure): 4 fréquences sur le
│   │   │                           # vocabulaire Period de recours.py, ANCRAGE
│   │   │                           # (jamais de chaînage), dates seules (le fuseau
│   │   │                           # vit chez l'appelant), fin obligatoire,
│   │   │                           # plafond 60 ; n'importe JAMAIS utils/deadlines
│   │   ├── recours.py              # Recours & prescription (pure): delay-period table
│   │   │                           # (amount, unit — jours/mois/ans), value-class table,
│   │   │                           # compute_class + compute_date_pour_agir + the
│   │   │                           # type-aware compute_echeances orchestration
│   │   │                           # (AVIS_PERIODS, PA_PERIODS, Echeance)
│   │   ├── taxonomie.py            # Taxonomie des actions (pure, GENERATED): 20 domaines →
│   │   │                           # 162 actions with délai / delai_types (11 jetons) /
│   │   │                           # a_valider / avis structurés / ref_delai + ref_fondement;
│   │   │                           # tooltip_payload; suggests a period, never sets one
│   │   ├── phases.py               # Taxonomie des PHASES du litige (pure, Phase O) :
│   │   │                           # 18 phases (tronc ordonné 1-9 + modules + ADM/HOR) →
│   │   │                           # ~60 sous-codes (-00/-99 synthétisés, HOR-00 seul) ;
│   │   │                           # VALID_PHASES/SOUS_PHASES ("" inclus), phase_of,
│   │   │                           # default_sous_phase, validate_pair, form_payload ;
│   │   │                           # importé DIRECTEMENT par mcp/tools.py (enums dérivés)
│   │   ├── docx_fill.py            # Phase H/H.2: stdlib-only .docx fill engine (zip XML substitution;
│   │   │                           # scalars, blocks, + H.2 repeating rows & conditional regions)
│   │   ├── template_fields.py      # Phase H: field catalog, flat aliases, classification, resolution
│   │   ├── invoice_docx.py         # Phase H.2: invoice → note-d'honoraires context (facture.* + rows + conditions)
│   │   ├── markdown_docx.py        # Phase H.3: markdown → OOXML blocks (note printing; shares the
│   │   │                           # screen's markdown pipeline constants — main.py imports them)
│   │   ├── note_docx.py            # Phase H.3: note → note-print context (note.* + the rich contenu)
│   │   ├── cabinet.py              # cabinet.* firm dict — ONE authority (was duplicated in 2 routes)
│   │   ├── icons.py                # Material Symbols: MATERIAL_ICONS canonical subset + the ms()
│   │   │                           # Jinja global (aria-hidden/translate=no/size classes)
│   │   ├── format_fr.py            # Phase H.2: fr-CA currency/date/hours/rate formatting (centralized)
│   │   ├── graph.py                # Portail L1 / phase J: Microsoft Graph client-credentials token
│   │   │                           # (process cache, no msal) + graph_get (nextLink) + graph_post
│   │   ├── graph_calendrier.py     # Bookings L2: calendarView reads (UTC), est_reservation
│   │   │                           # predicate, extraire, annuler_reservation (Calendars.ReadWrite)
│   │   │                           # + le marqueur anti-boucle du miroir (MIROIR_PROP_ID,
│   │   │                           # MIROIR_CATEGORIE, porte_marqueur_miroir)
│   │   ├── graph_miroir.py         # Miroir Outlook (pur, sans Firestore) : charge d'événement
│   │   │                           # Graph, marqueur hearing_id|etag, diff, POST/PATCH/DELETE
│   │   ├── rapprochement.py        # L3 (pur): candidats de rapprochement de noms pour l'aide au
│   │   │                           # contrôle des conflits — propose, ne tranche JAMAIS
│   │   ├── courriel.py             # Outbound email via Graph sendMail (saveToSentItems: true)
│   │   ├── validators.py           # Phone (E.164), email, postal code normalization, address defaults
│   │   ├── export_csv.py           # CSV export helper (UTF-8 BOM)
│   │   ├── export_pdf.py           # reportlab-based PDF export
│   │   ├── admin_journal_pdf.py    # « Journal de caisse — compte d'administration » (août 2026):
│   │   │                           # frère de trust_journal_pdf (11 colonnes dont Net/TPS/TVQ,
│   │   │                           # légal paysage, SOLDE REPORTÉ, ligne de taxes en clôture)
│   │   ├── trust_journal_pdf.py    # « Journal de caisse des recettes et déboursés » (août
│   │   │                           # 2026): le registre de l'art. 38 RLRQ c. B-1, r. 5 —
│   │   │                           # légal paysage, bloc de titre CENTRÉ, 10 colonnes,
│   │   │                           # ligne de SOLDE REPORTÉ + totaux qui se rapprochent
│   │   ├── journal_pdf.py          # « Journal des honoraires » (août 2026): la feuille du
│   │   │                           # Barreau — papier LÉGAL paysage, 13 colonnes pilotées
│   │   │                           # par CLÉ (l'ordre vit dans une seule table), cellules en
│   │   │                           # chaînes (jamais de repli), fr-CA ; SEUL le nom du client
│   │   │                           # s'écrête — un identifiant ou un montant tronqué serait FAUX
│   │   ├── budget_pdf.py           # Budget PDF builder (août 2026): les 2 variantes client
│   │   │                           # (estimation portrait / suivi paysage), sous-totaux par
│   │   │                           # phase, pied de page cabinet paginé, montants fr-CA
│   │   ├── logging_setup.py        # Cloud Logging handler, ContextFilter, RedactionFilter, typed log helpers
│   │   └── tracing_setup.py        # OpenTelemetry → OTLP/gRPC (telemetry.googleapis.com),
│   │                               # PII-sanitizing exporter, span()/@traced
│   │
│   ├── scripts/                    # One-time / manual scripts (run with python -m scripts.X)
│   │   ├── __init__.py
│   │   ├── seed_reference_data.py  # Populate ref_greffes + ref_juridictions (Phase G)
│   │   ├── mint_dev_token.py       # Local-dev MCP bearer minting (refuses ENV=production)
│   │   ├── revoke_mcp_tokens.py    # Break-glass: revoke all MCP tokens (+ optional client purge)
│   │   ├── diagnose_gabarit.py     # Local: list a gabarit's placeholders/classification + fragmentation cause
│   │   ├── verify_trust_integrity.py  # Phase K: recompute + cross-check the trust register (read-only)
│   │   ├── verify_admin_integrity.py  # Août 2026: recompute + cross-check du registre d'administration
│   │   │                           # (Σ deltas, ventilation, paires, re-preuve des conciliations)
│   │   ├── purge_encaissements_factures.py  # Août 2026: remet à zéro tout montant
│   │   │                           # encaissé que le grand livre n'adosse pas (--dry-run
│   │   │                           # par défaut ; appelle record_payment(id, 0), dont
│   │   │                           # l'annulation étroite rouvre « payée » → « envoyée »)
│   │   ├── reprise_encaissements.py  # Août 2026: l'ASSISTANT de rapprochement des 40
│   │   │                           # virements d'honoraires sortis du fidéicommis —
│   │   │                           # --proposer (CSV) / --verifier / --appliquer,
│   │   │                           # --seulement pour la 1re exécution. Un virement
│   │   │                           # peut acquitter PLUSIEURS factures (clé
│   │   │                           # d'idempotence = le couple virement-facture) ;
│   │   │                           # jamais d'écriture au fidéicommis
│   │   ├── corriger_provisions_factures.py  # Août 2026: retire d'une facture
│   │   │                           # reprise la `retainer_applied` que son virement
│   │   │                           # d'honoraires acquitte (règle prouvée
│   │   │                           # `provision ≤ Σ virements ≤ total`) — écriture
│   │   │                           # hors modèle, faute d'`update_invoice`
│   │   ├── migrate_vocabulaires.py  # One-shot: rewrite removed hearing/note/document keys (--dry-run default, --apply writes + bumps CTags)
│   │   └── backfill_protocol_court.py  # One-shot (July 2026): fill the always-empty protocol
│   │                               # court field from the dossier's tribunal (--dry-run default)
│   │
│   ├── tests/                      # pytest unit tests (run by Cloud Build as a deploy gate)
│   │   ├── __init__.py
│   │   ├── test_deadlines.py
│   │   ├── test_validators.py
│   │   ├── test_exports.py
│   │   ├── test_logging_setup.py
│   │   ├── test_tracing_setup.py
│   │   ├── test_pagination.py
│   │   ├── test_dashboard_aggregation.py
│   │   ├── test_security_headers.py
│   │   ├── test_mcp_jsonrpc.py
│   │   ├── test_mcp_oauth.py
│   │   ├── test_dav_hearings.py    # DAV: per-dossier + « Général », comp-filter, stamps
│   │   ├── test_notes_general.py   # Notes without a dossier: CTag bump + unknown-id guard
│   │   ├── test_analyse_note.py    # Théorie de la cause: dateless VJOURNAL, include_analyse
│   │   │                           #   contract, idempotent seed, edit-merge safety
│   │   ├── test_mcp_output_schemas.py  # Conformance: real handlers vs declared outputSchema
│   │   ├── test_mcp_tools.py
│   │   ├── test_docx_fill.py
│   │   ├── test_template_fields.py
│   │   ├── test_format_fr.py       # Phase H.2
│   │   ├── test_invoice_docx.py    # Phase H.2 (incl. end-to-end note fill)
│   │   ├── test_markdown_docx.py   # Phase H.3: markdown→OOXML converter (constructs, tables, bounds)
│   │   ├── test_note_docx.py       # Phase H.3: note-print context builder + assembly seam + E2E
│   │   ├── test_template_kind.py   # Phase H.3: _kind_from_form matrix + VALID_KINDS/KIND_LABELS
│   │   ├── test_reference_addresses.py  # Court-location table + greffe→address wiring
│   │   ├── test_reference_forums.py # Non-judicial forum table (admin tribunals + federal courts)
│   │   ├── test_taxonomie.py       # Action taxonomy invariants (incl. the §4 déchéance cross-check)
│   │   ├── test_phases.py          # Phase-O taxonomy invariants (Annexe A pinned, ASCII,
│   │   │                           # -00/-99 synthesis, tronc order, prefix invariant)
│   │   ├── test_budget.py          # Budget: validation (ADM/HOR refusés), versionnement,
│   │   │                           # agrégation du réalisé, seuils 80 %, PDF ×2 (NotoSerif
│   │   │                           # présent / Helvetica absent, fr-CA au niveau story)
│   │   ├── test_phase_fields.py    # Phase-O fields on the 3 models + protocol mapping
│   │   │                           # (pinned) + VTODO round-trip/non-effacement (CI-only)
│   │   ├── test_dossier_taxonomy.py # matter_type/objet → domaine/action migration + validation
│   │   ├── test_dossier_forum.py   # forum_type/forum validation + normalize_forum (CI-only)
│   │   ├── test_folders.py         # Phase H.2 get_or_create_folder (CI-only: imports models)
│   │   ├── test_document_naming.py # Phase H.2 projet_document_name (CI-only: imports models)
│   │   ├── test_trust.py           # Phase K: balance arithmetic, control, reversal, clearing, reconciliation, exports
│   │   ├── test_hearing_vocab.py   # Two-tier hearing vocab, forum, modalité, CONFERENCE (roundtrip/non-effacement/non-escaping), URI whitelist
│   │   ├── test_note_vocab.py      # Note category vocab + read-time migration
│   │   ├── test_document_vocab.py  # Document category vocab + migration + MCP enum parity
│   │   ├── test_hearing_confirmation.py  # L2: include_unconfirmed contract (DAV/MCP exclude by default)
│   │   ├── test_graph_calendrier.py      # L2: Bookings predicate, UTC parse, cancel call
│   │   │                                 # + le garde anti-boucle du miroir (marqueur → "")
│   │   ├── test_bookings_sync.py         # L2: reconciliation upsert + idempotence + cron guard
│   │   ├── test_graph_miroir.py          # Miroir Outlook: charge (jamais d'attendees), marqueur,
│   │   │                                 # diff all-day en dates, graph_patch/graph_delete
│   │   ├── test_taches_outlook.py        # Miroir Outlook: diff créer/corriger/supprimer, fenêtre
│   │   │                                 # pleine désarme les suppressions, jamais d'écriture Firestore
│   │   ├── test_reception_rdv.py         # L2: confirm/refuse/divergence routes + partie linkage + badge
│   │   ├── test_partie_kyc.py            # Audit fix: KYC dates stamped only on transitions
│   │   ├── test_protocol_summary.py      # Audit fix: 7-day window + calendar-date overdue
│   │   ├── test_protocol_regime.py       # Audit fix: regime gate at create + regime_mismatch
│   │   ├── test_audit_events.py          # audit_events journal: best-effort write, bounded read
│   │   ├── test_prescription_events.py   # derive_prescription: depot/reconnaissance/suspension,
│   │   │                                 # legacy prise_action_date fold, three-surface parity
│   │   ├── test_mcp_write_support.py     # run_write: dry_run, idempotency replay/conflict, fail-open
│   │   ├── test_invoice_payments.py      # Lot P: balance arithmetic, the auto-flip and its
│   │   │                                 # narrow undo, the cap on what is OWED
│   │   ├── test_trust_journal_pdf.py     # août 2026: le Journal de caisse (art. 38) — 10
│   │   │                                 # colonnes, report + rapprochement, carte-client
│   │   │                                 # laissée intacte (les 9 BARREAU_COLUMNS épinglées)
│   │   ├── test_journal_pdf.py           # août 2026: le Journal des honoraires — 13 colonnes,
│   │   │                                 # légal paysage, et la mesure qu'AUCUNE cellule ne
│   │   │                                 # plie ni ne déborde (stringWidth vs largeur)
│   │   ├── test_invoice_detail.py        # août 2026: la page détail est une FICHE DE DONNÉES —
│   │   │                                 # rend le gabarit (bloc content seul) et épingle les
│   │   │                                 # deux moitiés : données présentes, fac-similé absent
│   │   ├── test_admin_ledger.py          # août 2026: registre d'administration — couche pure
│   │   │                                 # (deltas, ventilation, brut→net/TPS/TVQ), verrou de
│   │   │                                 # conciliation, antidatage PERMIS, conciliation carte
│   │   │                                 # (harnais fake-Firestore, _match étendu aux inégalités)
│   │   ├── test_admin_integration.py     # août 2026: sélecteur global de factures, coexistence
│   │   │                                 # record_payment (courant+delta), gardes des reçus,
│   │   │                                 # orchestration fidéicommis, épingles OOB/nav
│   │   ├── test_admin_journal_pdf.py     # août 2026: la feuille 11 colonnes — ratios, blancs
│   │   │                                 # jamais « 0,00 $ », NotoSerif, budget de largeurs
│   │   ├── test_recurrence.py            # Séries: les 4 fréquences, ANCRAGE (31 janv.
│   │   │                                 # → 28 févr., 31 mars), fin obligatoire,
│   │   │                                 # refus (jamais de troncature), et le DST
│   │   │                                 # que recours.add_period n'avait jamais vu
│   │   ├── test_hearing_series.py        # Séries (modèle): le piège serie_id == "",
│   │   │                                 # l'id fourni écrasé, UN lot atomique bump
│   │   │                                 # inclus, passé protégé, sortie DAV sans RRULE
│   │   ├── test_hearing_series_routes.py # Séries (routes): portée relue du STOCKÉ,
│   │   │                                 # refus en 2xx (htmx), UNE ligne au journal
│   │   └── test_coverage.py              # Lot 5: the 13 checks as PURE predicates, no Firestore
│   │
│   ├── templates/
│   │   ├── base.html
│   │   ├── offline.html            # Service-worker offline fallback
│   │   ├── components/             # Reusable partials (modal, toast, empty_state, confirm_dialog,
│   │   │                           # pagination, loading_skeleton, _export_dropdown[_oob])
│   │   ├── auth/                   # login, mfa_setup, mfa_manage
│   │   ├── dashboard/index.html
│   │   ├── errors/                 # 404, 500
│   │   ├── parties/                # list, detail, form + _partie_rows, _search_results,
│   │   │                           # _mandataire_search_results, _address_letter
│   │   ├── dossiers/               # list, detail, form + _dossier_rows + _tab_nav
│   │   │                           # (two-level tab bar) + _tab_apercu, _tab_analyse,
│   │   │                           # _tab_temps,
│   │   │                           # _tab_facturation, _tab_fideicommis, _tab_audiences,
│   │   │                           # _tab_taches, _tab_protocole, _tab_documents
│   │   │                           # (Fichiers), _tab_notes, _tab_placeholder
│   │   ├── time_expenses/          # list, time_form, expense_form + _time_rows, _expense_rows
│   │   ├── invoices/               # list, detail, create + _invoice_rows + _unbilled_items
│   │   ├── hearings/               # list, detail, form + _hearing_rows + _month_grid
│   │   ├── tasks/                  # list, detail, form + _task_row, _task_rows
│   │   ├── notes/                  # list, detail, form + _note_rows
│   │   ├── protocols/              # list, detail, form + _protocol_rows
│   │   ├── documents/              # list, detail, upload, edit + _browser, _document_rows, _folder_tree
│   │   ├── gabarits/               # list, detail, form + _template_rows, _generate_modal, _generate_fields
│   │   ├── trust/                   # Phase K: list (journal), _transaction_rows, detail, form,
│   │   │                            # _facture_options (sélecteur du virement, 2026-08-12), card,
│   │   │                            # transfer_form, reverse_confirm, client_consolidated, accounts_list,
│   │   │                            # account_form/detail, reconciliations_list, reconciliation_form/worksheet
│   │   ├── budgets/                 # août 2026: form (répéteur versionné par phase), history
│   │   ├── administration/          # août 2026: list (journal + filtres + OOB exports), form
│   │   │                            # (create+edit, ventilation HTMX, sélecteur global de factures),
│   │   │                            # detail (reçu direct-à-GCS, facture liée, révisions),
│   │   │                            # reverse_confirm (date choisissable), card_payment_form,
│   │   │                            # comptes ×3, conciliations ×3 (feuille banque/carte)
│   │   ├── comptabilite/            # août 2026: index.html — le hub (deux sections, soldes
│   │   │                            # étiquetés par ligne, jamais de total combiné)
│   │   ├── reception/              # Portail L1: index (onglets + lots + invitations), inviter, lien,
│   │   │                           # _rdv (L2), _ouvertures + _confirmation_intake (L3),
│   │   │                           # _invitation_{documents,ouverture,pied}.html (corps des
│   │   │                           # deux courriels d'invitation ; le pied est commun)
│   │   └── mcp/                    # consent.html (OAuth consent screen, French)
│   │
│   └── static/
│       ├── sw.js                   # Service worker (precache + stale-while-revalidate for vendor assets)
│       ├── src/app.input.css       # Tailwind input (source of the compiled artifact below)
│       ├── vendor/                 # Vendored, version-named, immutable-cached assets:
│       │                           # app.<hash>.css (compiled Tailwind), htmx-2.0.4.min.js,
│       │                           # alpinejs-3.15.12.min.js, firebase-{app,auth,app-check}-compat-10.12.2.js,
│       │                           # appcheck-boot.<hash>.js (App Check bootstrap, was inline),
│       │                           # noto-sans-v42-latin-wght[-italic].woff2 (UI font) +
│       │                           # noto-serif-v33-latin-wght[-italic].woff2 (note content) +
│       │                           # material-symbols-outlined-v364-<sha8>.woff2 (icon ligature
│       │                           # subset + its *-Apache-2.0.txt licence — the subset MUTATES
│       │                           # when icons are added: new hashed name each time) +
│       │                           # the two *-OFL.txt licenses (August 2026 — .woff2 has its own
│       │                           # mime_type handler in both yaml files; the SANS roman is
│       │                           # preloaded via Early Hints with `crossorigin` + a
│       │                           # <link rel=preload> in the portal base)
│       ├── icons/                  # PWA + favicon assets
│       └── legal/                  # privacy.html, terms.html (served at /privacy, /terms)
│
├── cloudbuild.yaml                 # Cloud Build pipeline (pytest gate, deploy, prune old versions)
├── firebase.json                   # Firebase CLI targets (points at the rules/index files below)
├── firestore.rules                 # Firestore security rules (deploy via firebase CLI; not an App Engine file)
├── firestore.indexes.json          # Composite indexes — deploy with `firebase deploy --only firestore:indexes`
├── storage.rules                   # Firebase Storage rules
├── .env.example                    # Template for local-dev env vars
└── .github/                        # dependabot.yml + workflows: codeql, osv-scanner, trivy, bandit,
                                    # dependency-review, scorecard
```

> The Firestore/Storage rules + index files live at the **repo root** (next to `firebase.json`, which references them by bare filename) — they are Firebase-CLI deploy config, **not** part of the App Engine app, so they deliberately sit outside `athena/` and never ship in the deployed bundle.

> Note on tab names: the dossier detail uses an HTMX tab loader (`/dossiers/<id>/tab/<tab_name>`). Since late July 2026 the hub uses a **two-level tab nav** (`templates/dossiers/_tab_nav.html`): four top-level **groups** — **Aperçu**, **Finances**, **Agenda**, **Documents** — over horizontally-scrollable **leaf** sub-rows (Finances → `temps`/`facturation`/`fideicommis`; Agenda → `audiences`/`taches`/`protocole`; Documents → `documents` (« Fichiers »)/`notes`/`analyse`; Aperçu is single-leaf, no sub-row). The eleven leaf slugs the HTMX loader accepts are `apercu, temps, facturation, fideicommis, budget, audiences, taches, protocole, documents, notes, analyse` (Finances → `temps`/`facturation`/`fideicommis`/`budget` since August 2026); **`temps` is still the default leaf** (a habit kept from the era `apercu` was empty; the tab now has content, so switching the default is a one-line UX decision, not a technical one). `GET /dossiers/<id>?tab=<leaf>` selects the initial leaf and the server derives its group (`_LEAF_GROUP`, mirrored by the `groups` list in `_tab_nav.html`); selection is client-side Alpine (`activeGroup`/`activeLeaf`), while each leaf still HTMX-loads `/dossiers/<id>/tab/<leaf>` into `#tab-content` and pushes the `?tab=` hub URL. The scrollable sub-rows use the `.dossier-subnav`/`.dossier-subnav-wrap` classes in `static/src/app.input.css` (hidden scrollbar + snap + right-edge fade — a rehash accompanied their introduction). Tab keys ≠ labels since July 2026: `temps` is labelled « Temps & Déboursés », `facturation` « Honoraires », `audiences` « Calendrier » (see below), `documents` « Fichiers » (under a **Documents** group with a new `notes` « Notes » leaf); the keys were kept so bookmarks survive. The `documents` tab lost its counters/indicators in July 2026 (summary stat grid and the per-folder « X éléments » line — dropping the latter also removed a per-folder `_count_items` N+1 and the unused `get_document_summary`/`get_notes_summary` calls from the tab loader). `audiences` and `taches` were merged into one `agenda` tab in July 2026, then **re-split in late July 2026** into separate **Calendrier** (`audiences`, `_tab_audiences.html`) and **Tâches** (`taches`, `_tab_taches.html`) leaves under the **Agenda** group (the two-level nav gave back the horizontal room the merge worked around). Each stays **forward-looking**: items dated strictly before today (Montréal calendar day; today's items stay) are filtered out route-side in Python (no new Firestore index), and a dateless task shows only while active (`à_faire`/`en_cours`). The merged `_tab_agenda.html` is gone; `_LEGACY_TABS` in `routes/dossiers.py` now maps a merged-era `?tab=agenda` bookmark onto `audiences` (Calendrier), and the pre-merge `audiences`/`taches` slugs are valid again. **« Agenda » is now the group name; the hearings feature itself is labelled « Calendrier » everywhere user-facing** (main nav + the standalone `/audiences` page) — the `/audiences` route prefix, the `hearings` blueprint, and the DAV `/dav/calendar/` layer are unchanged (the rename is label-only). The `apercu` (Aperçu) tab was removed in July 2026, **re-added as an intentionally-empty leaf in late July 2026**, and since **2026-07-23 hosts the four legal-info cards** — Juridiction · Recours · Prescription · Mandat — in the same 2×2 grid style (`_tab_apercu.html`; derived values from `routes/dossiers._apercu_card_context`, label maps from `_template_context`). Above the tab bar remain only the header card, the « Sommaire » card and the Clients / Parties adverses pair. Its original prescription block had become the « Recours et prescription » card (itself **split in July 2026** into « Recours » — domaine, action (libellé + greyed `(CODE)` from `action_obj`), précision, valeur/classe — and « Prescription » — « Délai (Type) » (the confirmed délai with the taxonomy's nature du délai bracketed after it, amber when a déchéance), droit d'action, date pour agir), its dates became the « Mandat » card (renamed from « Dates clés » in July 2026 — it shows type de mandat, « Honoraires (taux) » (`format_honoraires_parts`: type label + greyed rate in parentheses; gabarits keep `format_honoraires`'s joined « label — taux »), ouverture, fermeture, and a derived « Rétention » = fermeture + 7 ans computed read-only in `dossiers.dossier_detail`; the fermeture/rétention rows are hidden until a closing date is set; « Type de dossier » left it in July 2026 when it became « Domaine » on the Recours card), and its free-text notes were deleted with the fields (below). A « Sommaire » card (free-text `sommaire` field, entered on the create/edit form) sits between the header card and the info-card grid. Notes are a **Notes** leaf under the Documents group (`notes` slug, `_tab_notes.html`, split off the Fichiers tab in late July 2026 — mirroring the audiences/tâches split); they also remain at the standalone `/notes` view (filterable by `?dossier_id=`). An **Analyse** leaf (`analyse`, `_tab_analyse.html`) joined the **Documents** group in July 2026 (the spec drafted it under Aperçu; user decision 2026-07-23 placed it beside Fichiers/Notes): it hosts the dossier's single « **Théorie de la cause** » note (8 blocs A→H, `models/note.py` `ANALYSE_TITLE`/`_ANALYSE_SEED`), lazily created by `POST /dossiers/<id>/analyse/init` (idempotent; the route bumps the CTag only on actual creation) and edited through the standard `/notes/<id>/edit` form by id. The note is `dateless=True` (VJOURNAL **without DTSTART** → a jtx Board *Note*) and `is_analyse=True` — hidden from every Notes view but INCLUDED on the DAV and MCP read paths (see the `include_analyse` Known Gotcha).

---

## Firestore Data Model

All collections are top-level (see Architecture Rule 2). The two reference collections (`ref_greffes`, `ref_juridictions`) are read-only.

### Common fields (every document)

```
id:          UUIDv4 (also the document ID)
created_at:  UTC datetime
updated_at:  UTC datetime
etag:        UUIDv4, regenerated on every write
```

### `parties/{partieId}` — Contacts

```python
{
    "type": "individual" | "organization",     # personne physique / personne morale (UI labels)
    "contact_role": "client" | "partie_adverse" | "avocat_adverse"
                  | "témoin" | "expert" | "huissier" | "notaire" | "autre",

    # Individual (personne physique)
    "prefix": "Me" | "M." | "Mme" | "",
    "first_name": str, "last_name": str,
    "birth_date": datetime | None,       # DATE SEULE à minuit UTC (convention
                                         # dossier.opened_date) — rendre par
                                         # strftime, JAMAIS to_mtl. Sérialisée
                                         # en vCard BDAY (personnes physiques
                                         # seulement) ET relue : l'absence de
                                         # BDAY OMET la clé, jamais None, sinon
                                         # un PUT DavX5 l'effacerait. Non
                                         # exposée par MCP.

    # Organization (personne morale)
    "organization_name": str,                   # Legal name (nom légal) — required when type=="organization"
    "trade_name": str,                          # Trade name / "doing business as" (nom d'emprunt)
    "governing_law": str,                       # Constituting statute (loi constitutive)

    # vCard 4.0 demographics (LANG, GENDER, X-PRONOUN)
    "language": "fr" | "en" | "es" | "",
    "gender": "M" | "F" | "O" | "N" | "U" | "",
    "pronouns": "il/lui" | "elle" | "iel" | "he/him" | "she/her" | "they/them" | "",

    # Employment (vCard TITLE, ROLE, ORG)
    "job_title": str, "job_role": str, "organization": str,

    # Personal contact
    "email": str,                        # lowercase normalized
    "phone_home": str, "phone_cell": str,  # E.164 (+15145551234)
    "address_street": str, "address_unit": str,  # street stores number + name (e.g. "450 rue Sainte-Catherine Ouest")
    "address_city": str,                 # default "Montréal" (full name)
    "address_province": str,             # default "Québec" (full name; legacy "QC" migrated on save)
    "address_postal_code": str,          # "A1A 1A1" format
    "address_country": str,              # default "Canada" (full name; legacy "CA" migrated on save)

    # Professional contact
    "email_work": str,
    "phone_work": str, "fax": str,
    "work_address_street": str, "work_address_unit": str,
    "work_address_city": str, "work_address_province": str,
    "work_address_postal_code": str,
    "work_address_country": str,         # default "Canada"

    # Legal identifiers
    "bar_number": str,                    # For lawyers
    "company_neq": str,                   # Quebec NEQ for organizations

    # KYC (only relevant when contact_role == "client")
    "identity_verified": "non_vérifié" | "vérifié" | "exempté",
    "identity_verified_date": datetime | None,
    "identity_verified_notes": str,
    "kyc_document_ids": list[str],        # References to documents collection
    "conflict_check": "non_vérifié" | "vérifié" | "conflit_détecté",
    "conflict_check_date": datetime | None,
    "conflict_check_notes": str,

    # Mandataires (representations: mandate, tutorship, curatorship, …)
    # A partie may have any number of mandataires. Each entry's `id` must
    # reference a partie that exists, is type=="individual", shares this
    # partie's contact_role, and is not this partie itself. Duplicates
    # (same id appearing twice in the list) are deduped on save.
    # Legacy single-mandataire fields ("mandataire_id" / "mandataire_kind"
    # / "mandataire_notes") are migrated into this list on read by
    # `_migrate_mandataires` and purged from storage on the next save.
    "mandataires": [
        {
            "id": UUIDv4,
            "kind": "mandataire" | "tuteur" | "curateur"
                  | "représentant_légal" | "autre",
            "notes": str,
        },
        ...
    ],

    "notes": str,

    # DAV
    "vcard_uid": UUIDv4,                  # set once at creation
    "dav_href": "/dav/addressbook/{id}.vcf",
}
```

### `dossiers/{dossierId}` — Case files

A dossier holds multiple clients and multiple opposing parties as **arrays of `{id, name, roles, avocat_id, avocat_name}` objects** (July 2026 rework): `roles` ⊆ `PARTY_ROLES` (10-value procedural vocabulary — demandeur, défendeur, demandeur/défendeur reconventionnel, mis en cause, intervenant, appelant, intimé, requérant, autre; a party may hold several), and `avocat_id`/`avocat_name` link+snapshot the party's lawyer (any individual contact; the form's picker boosts `avocat_adverse` first). Flat ID arrays (`client_ids`, `opposing_party_ids`, **`avocat_ids`**) are kept in sync for `array_contains` queries (`count_dossiers_for_partie` / `list_dossiers_for_partie` / the partie-deletion FK check — all three arrays). **`role` is DERIVED on save** (`_derive_role`: first role of the first client that has one) and survives only for the gabarits; the standalone selector is gone from the form. A migration helper (`_migrate_parties`) upgrades older single-client docs on read, normalizes every entry to the full shape, and **seeds the legacy dossier-level `role` into `clients[0].roles`** (once, when no client carries a role) so existing dossiers keep their meaning untouched.

```python
{
    "file_number": str,                   # User-assigned, e.g., "2025-001"
    "title": str,                         # "Tremblay c. Lavoie"
    "sommaire": str,                      # Free-text case summary (≤ 5000 chars —
                                          # _SOMMAIRE_MAX_LENGTH; other string
                                          # fields keep the 2000 cap). Shown in
                                          # its own card on the detail page;
                                          # exposed by the MCP get_dossier tool
                                          # and as the {{dossier.sommaire}} /
                                          # {{sommaire}} gabarit placeholder.

    # Parties on the dossier (replaces the legacy single client_id).
    # roles ⊆ PARTY_ROLES (multi); avocat_* = the party's lawyer (link + snapshot).
    "clients":          [{"id": UUIDv4, "name": str, "roles": [str, ...],
                          "avocat_id": UUIDv4 | "", "avocat_name": str}, ...],
    "client_ids":       [UUIDv4, ...],    # mirrors clients[].id (for array_contains)
    "opposing_parties": [...],            # same shape as clients
    "opposing_party_ids": [UUIDv4, ...],
    "avocat_ids":       [UUIDv4, ...],    # every avocat_id, deduped/sorted (FK check)

    # Classification — the two-level ACTION TAXONOMY (July 2026), replacing
    # the old free-form matter_type ("type de dossier") + objet (free text).
    # Vocabulary lives in utils/taxonomie.py, NOT in models/dossier.py.
    # Both default to "" — a dossier need not be classified — and both are
    # presence-gated in _validate, so a legacy doc stays editable.
    "domaine": "" | "REC" | "CON" | "RCV" | ...,   # 20 families
    "action": "" | "REC-01" | ...,                 # 162 named recourses;
                                                   # the code prefix MUST equal
                                                   # `domaine` (_validate checks)
    "action_precision": str,   # free text; required by the « Autre (préciser) »
                               # (-99) rows, and where the pre-taxonomy `objet`
                               # text lands on migration
    # mandate_type ("type de mandat") — nature of the engagement. Vocabulary
    # reworked July 2026 (user decision): consultation→service_conseils,
    # transactionnel→special, autre/mediation_arbitrage→general, all on read
    # via models.dossier._migrate_mandate_type. Absent on legacy dossiers →
    # the UI shows "—" until set on edit.
    "mandate_type": "judiciaire" | "service_conseils" | "general" | "special",
    # DERIVED on save from clients[].roles (first client that has one) —
    # never user-entered since July 2026; kept for the gabarits.
    "role": str,                          # ∈ PARTY_ROLES | ""

    # Phase G — Court file number + parsed judicial metadata
    "court_file_number": str,             # Raw, e.g., "500-05-123456-241"
    "greffe_number": str,                 # 3-digit parsed code
    "juridiction_number": str,            # 2-digit parsed code
    "tribunal": str,                      # Court/forum name (parsed from
                                          # ref_juridictions, OR the forum name
                                          # when forum_type=="autre")
    "competence": str,                    # Auto-populated (judicial only)
    "palais_de_justice": str,             # Auto-populated from ref_greffes
    "district_judiciaire": str,           # Auto-populated (judicial only)
    "is_administrative_tribunal": bool,   # True for a Québec admin tribunal
                                          # (letters-prefix parse OR an "autre"
                                          # forum of category "administratif")

    # Forum (July 2026; four-way since late July — the binary "autre" was
    # split and a pre-litigation state added; legacy "autre" docs migrate on
    # read via _migrate_forum_type, slug category → administratif/federal).
    "forum_type": "judiciaire" | "administratif" | "federal" | "prejudiciaire",
                                          # default "judiciaire" (parser active).
                                          # "prejudiciaire" = nothing filed yet:
                                          # only district_judiciaire is entered,
                                          # and court_file_number is FORCED to
                                          # "Préjudiciaire" (PREJUDICIAIRE_FILE_NUMBER)
                                          # so {{dossier.numero_cour}} cites it —
                                          # crushed by the parser once a real
                                          # number is entered under "judiciaire".
    "forum": str,                         # reference._FORUMS slug when
                                          # administratif/federal (e.g. "taq",
                                          # "cour_federale"); "" otherwise. Its
                                          # name is written into `tribunal`, and
                                          # the court file number is stored
                                          # UNPARSED. models/dossier.normalize_forum
                                          # reconciles this server-side and
                                          # rejects a cross-category slug.

    # Financial (cents)
    # "pro_bono"/"aide_juridique" are RATE-LESS: no taux/forfait/pourcentage
    # applies, so format_honoraires renders the label alone.
    "fee_type": "hourly" | "flat" | "contingency" | "mixed"
              | "pro_bono" | "aide_juridique",
    "hourly_rate": int,                   # cents (default 30000 = $300/h)
    "flat_fee": int | None,
    "contingency_percent": int | None,    # BASIS POINTS (2500 = 25,00 %), not
                                          # cents — mirrors invoice gst_rate.
                                          # Applies to "contingency" AND "mixed".
    "fee_notes": str,                     # free text on the fee arrangement

    # Status
    "status": "actif" | "en_attente" | "fermé" | "archivé",
    "opened_date": datetime, "closed_date": datetime | None,

    # Recours & prescription (see utils/recours.py + utils/taxonomie.py)
    # `domaine`/`action`/`action_precision` are up in Classification above.
    "valeur": int | None,                 # amount in dispute, integer cents
    "prescription_type": str,             # dropdown key → period (recours.PRESCRIPTION_PERIODS).
                                          # The delay the LAWYER CONFIRMED — the
                                          # taxonomy only suggests it on an
                                          # action change, and may differ.
    "droit_action_date": datetime | None, # "droit d'action" — start of the prescription
    # "date pour agir": DERIVED on save (models/dossier._apply_prescription_deadline)
    # from droit_action_date + prescription_type (via compute_echeances since
    # July 2026 — behaviors unchanged); remains the field the
    # dashboard/index/alerts read.
    "prescription_date": datetime | None,
    # Confirmed avis préalable date (July 2026, additive — absent on legacy
    # docs, no migration). MANUAL: entered on the form, never auto-derived
    # (each avis has its own factual starting point — délivrance du bien,
    # cause d'action… — not droit_action_date). The form shows the action's
    # structured avis (délai/point de départ/sanction) as the suggestion.
    "date_avis": datetime | None,
    # Acte interruptif posé — la demande déposée (art. 2892 C.c.Q.).
    # MANUEL, jamais dérivé, additif (juillet 2026, aucune migration :
    # absent sur un dossier hérité = aucune action prise). LEGACY depuis
    # le modèle d'événements ci-dessous : derive_prescription le replie À
    # LA LECTURE en événement interruption_depot implicite (aucune
    # migration de stockage), donc sa présence TAIT l'alerte partout —
    # list_prescription_alerts (tableau de bord ET MCP get_agenda) et
    # _attach_prescription_warnings (pastille de liste + couleur de
    # carte). Ne recalcule PAS prescription_date.
    "prise_action_date": datetime | None,
    # ÉVÉNEMENTS DE PRESCRIPTION (juillet 2026 — remédiation de l'audit
    # MCP, décision « modèle d'événements complet »). Journal append-only
    # sur le document ; la date brute `prescription_date` n'est JAMAIS
    # recalculée à partir d'eux (provenance — test épinglé) :
    # models/dossier.derive_prescription(doc) projette À CÔTÉ un statut
    # dérivé (courante | interrompue | echue | imprescriptible |
    # a_verifier) et une `date_effective`. interruption_depot (art.
    # 2892/2896) → interrompue, date None (l'interruption dure jusqu'au
    # jugement — calculer une date serait l'inventer) ;
    # interruption_reconnaissance / renonciation → un nouveau délai de la
    # même durée confirmée court de la date de l'événement
    # (compute_date_pour_agir — report art. 52 inclus) ; suspension →
    # décale l'échéance effective de sa durée, puis au prochain jour
    # juridique. Les événements ne peuvent que REPOUSSER la date, donc la
    # requête serveur sur la date brute SUR-capture, jamais l'inverse —
    # aucun index nouveau, filtre Python sur la fenêtre bornée.
    "prescription_events": [
        {
            "id": UUIDv4,
            "type": "interruption_depot" | "interruption_reconnaissance"
                  | "suspension" | "renonciation",
            "date": datetime,             # date seule à minuit UTC
            "end_date": datetime | None,  # suspension seulement (requise)
            "reference": str,             # ≤ 300 — « signification DII »…
            "document_id": UUIDv4 | "",   # FK documents, facultative
        },
        ...
    ],
    # SIGNIFICATIONS (juillet 2026, même lot). Registre append-only ;
    # partie_id doit référencer une partie AU dossier (les délais des
    # arts. 145/147 C.p.c. courent par partie). superseded_by pointe la
    # signification qui REMPLACE celle-ci (le cas du second PV corrigé) ;
    # la dérivation des délais de réponse est différée (couture posée).
    "significations": [
        {
            "id": UUIDv4,
            "partie_id": UUIDv4,          # partie au dossier (validé)
            "date": datetime,             # date seule à minuit UTC
            "mode": "personnelle" | "domicile" | "huissier" | "notification"
                  | "avocat" | "publication",
            "huissier_id": UUIDv4 | "",   # FK parties, facultative
            "pv_document_id": UUIDv4 | "",
            "superseded_by": UUIDv4 | "", # id d'une signification SŒUR
            "confirmee": bool,
        },
        ...
    ],
    "prescription_notes": str,

    # REMOVED FIELDS — popped on read by models/dossier._strip_removed_fields
    # (get_dossier only), so the next full-document set() purges them:
    #   notes / internal_notes — July 2026, superseded by the standalone
    #     `notes` collection.
    #   matter_type / objet — July 2026, superseded by the domaine/action
    #     taxonomy. _migrate_domaine folds them into domaine/action_precision
    #     FIRST — get_dossier nests the calls as
    #     _strip_removed_fields(_migrate_parties(doc)), and reversing that
    #     nesting destroys the legacy data unread.

    # DAV (retained for potential export; not used by the DAV layer post-D1)
    "vjournal_uid": UUIDv4, "dav_href": "/dav/journals/{id}.ics",
}
```

> The schema does **not** currently include dedicated `opposing_counsel`, `court`, or `retainer_amount` / `retainer_balance` fields. Opposing counsel today is captured by adding a partie with `contact_role="avocat_adverse"` to `opposing_parties`. Court is derived from the parsed `tribunal` / `palais_de_justice`. There is no retainer-tracking subsystem yet.

### `dossiers/{dossierId}/folders/{folderId}` — Document folders

Subcollection under dossiers. Folders are Firestore-only; actual files stay at flat Storage paths regardless of folder moves.

```python
{
    "dossier_id": str,
    "name": str,                          # Max 100 chars, no / or \
    "parent_folder_id": UUIDv4 | None,    # None = root of dossier
    "order": int,                         # Display order among siblings
    # Standard created_at/updated_at (no etag on folders)
}
```

**Constraints:** max nesting depth 5, no duplicate names within same parent (case-insensitive), circular reference prevention on move.

### `timeentries/{entryId}` — Billable hours

```python
{
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "date": datetime,                     # Date only, stored as midnight UTC
    "description": str,
    # Phase O (août 2026) — codes de phase du litige (utils/phases.py).
    # "" = non renseignée (docs hérités, jamais rétro-remplis). sous_phase
    # défaut "<phase>-00" à l'écriture. ORTHOGONAL à toute `category`.
    "phase": str,                         # "" | "ADM" | "PRE" | ... (18 codes)
    "sous_phase": str,                    # "" | "CTS-02" | ... (préfixe = phase)
    "hours": float,                       # 0.1 increments
    "rate": int,                          # cents
    "amount": int,                        # cents, computed: hours * rate,
                                          # FORCED to 0 when billable is False
                                          # (unbillable time has no calculated
                                          # cost — models.time_entry
                                          # ._compute_entry_amount); the
                                          # dashboard's unbilled tracker already
                                          # excludes it via the billable filter
    "billable": bool, "invoiced": bool,
    "invoice_id": UUIDv4 | None,
}
```

### `expenses/{expenseId}` — Expenses

```python
{
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "date": datetime,
    "description": str,
    "category": "signification" | "expertise" | "transcription"
              | "deplacement" | "photocopie" | "timbre_judiciaire" | "autre",
    "phase": str,                         # Phase O — même contrat que timeentries
    "sous_phase": str,
    "amount": int,                        # cents
    "taxable": bool,
    "receipt_document_id": UUIDv4 | None, # FK → documents (optional)
    "invoiced": bool, "invoice_id": UUIDv4 | None,
}
```

### `invoices/{invoiceId}` — Invoices

```python
{
    "invoice_number": str,                # "YYYY-F###" — Montréal-year sequence
                                          # (3-digit padded, rolls to 4+ past
                                          # 999; e.g. "2026-F031"). Canonical
                                          # again since 2026-08-12; the SIX
                                          # per-file "{file_number}-NN" invoices
                                          # of the 2026-07-17→08-12 parenthesis
                                          # keep their numbers for ever.
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "client_id": str, "client_name": str,

    # Billing address snapshot at invoice creation
    "billing_address": {"name", "street", "unit", "city", "province", "postal_code"},

    "date": datetime, "due_date": datetime,
    "status": "brouillon" | "envoyée" | "payée" | "en_retard" | "annulée",

    # All cents
    "subtotal_fees": int, "subtotal_expenses": int, "subtotal": int,
    "gst_rate": 500,                      # basis points (5.00%)
    "gst_amount": int,
    "qst_rate": 9975,                     # basis points (9.975%)
    "qst_amount": int,
    "total": int,
    "retainer_applied": int, "amount_due": int,

    "gst_number": str,                    # Snapshotted from config at creation
    "qst_number": str,

    "notes": str,
    "payment_terms": str,                 # default: "Payable dans les 30 jours…"
}
```

### `invoices/{invoiceId}/lineitems/{itemId}` — Invoice line items (subcollection)

```python
{
    "type": "fee" | "expense",
    "source_id": UUIDv4,                  # FK → timeentry or expense
    "date": datetime,
    "description": str,
    "hours": float | None,                # fees only
    "rate": int | None, "amount": int,    # cents
    "taxable": bool,
}
```

### `hearings/{hearingId}` — Court dates

```python
{
    # Optional — standalone agenda events have no dossier (all fields "" when unset)
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "title": str,
    # Two-tier vocabulary (2026-07-24). The forum (judiciaire /
    # extrajudiciaire) is DERIVED from the type (models.hearing.forum_of),
    # NEVER stored — the two lists are disjoint. Judiciaire:
    # conférence_de_gestion/_de_règlement/_préparatoire, audience, instruction.
    # Extrajudiciaire: consultation, rencontre, conférence, interrogatoire,
    # autre. Removed keys migrated ON READ (_migrate_hearing): procès→
    # instruction, appel→audience, médiation→autre. « conférence » is a
    # strict PREFIX of the three « conférence_… » keys — strict equality /
    # dict access ONLY, never startswith.
    "hearing_type": "conférence_de_gestion" | ... | "autre",
    "start_datetime": datetime, "end_datetime": datetime,
    "all_day": bool,
    "location": str, "court": str, "judge": str,
    # Modality (2026-07-24). conference_uri is KEPT even when modalite leaves
    # visioconférence (round-trip); it is rendered as an <a href> so a
    # http/https whitelist (is_safe_conference_uri) guards every write path.
    "modalite": "présentiel" | "visioconférence" | "téléphonique",
    "conference_uri": str,                # http/https only; "" unless video
    "notes": str,
    "reminder_minutes": int,              # 15|30|60|120|1440|2880|10080, default 1440 (24h)
    "status": "confirmée" | "à_confirmer" | "reportée" | "annulée" | "terminée",

    # Bookings sync (Phase L2, 2026-07-25). All default ""/None; absent on
    # legacy docs (defaulted on read by _migrate_hearing, NEVER back-filled).
    # confirmation is a SEPARATE concept from status — note "à_confirmer" is
    # ALSO a status value (a court date pending scheduling), which confirmation
    # must never be conflated with.
    "source": "" | "bookings",            # "bookings" = a « Bookings with me » import
    "confirmation": "" | "à_confirmer"    # "" (or absent) = confirmed → visible everywhere;
                  | "annulée_client"      # gates it out of DAV+MCP (and, except à_confirmer,
                  | "refusée",            # the Calendar). See list_hearings include_unconfirmed.
    "graph_event_id": str, "graph_ical_uid": str, "graph_last_modified": str,
    "client_email": str, "client_nom": str,   # from the Bookings attendee (≠ juriste)
    "bookings_divergence": dict | None,   # {motif, detail, nouveau_debut/_fin, vu} — §5.4
    "partie_id": str,                     # recognized partie linked at confirmation (§5.1)

    # Séries récurrentes (août 2026). Additifs, sans migration : un doc
    # hérité lit serie_id == "" (« autonome »), ce qui est vrai. Les DEUX
    # champs appartiennent au SERVEUR — jamais lus d'une charge DAV, jamais
    # émis dans un VEVENT, ce qui fait que le lien survit gratuitement à un
    # aller-retour depuis le téléphone (une clé absente survit à la fusion
    # de update_hearing).
    #
    # ⚠ "" est une VALEUR STOCKÉE, pas une sentinelle : une égalité
    # Firestore dessus ramène TOUTE audience autonome du cabinet. Voir le
    # Known Gotcha — le déclencheur ne demande aucun attaquant.
    "serie_id": str,                      # UUIDv4 partagé par la chaîne; "" = autonome.
                                          # Toutes les occurrences sont ÉGALES :
                                          # pas de maître, pas d'index (un index
                                          # se périmerait au 1er détachement,
                                          # un maître ferait du détachement une
                                          # promotion au lieu d'une écriture).
    "serie_rule": dict | None,            # {freq, start, count|until} — dates ISO.
                                          # Le motif TEL QU'ENGENDRÉ, jamais
                                          # réétendu à la lecture : après le
                                          # premier détachement ou la première
                                          # suppression, les dates ne
                                          # déterminent plus la règle.

    # DAV
    "vevent_uid": UUIDv4, "dav_href": "/dav/calendar/{id}.ics",
}
```

### `tasks/{taskId}` — Tasks

```python
{
    "dossier_id": UUIDv4 | None,          # Optional (standalone tasks live at /dav/tasks/)
    "dossier_file_number": str, "dossier_title": str,
    "title": str, "description": str,
    "priority": "haute" | "normale" | "basse",
    "status": "à_faire" | "en_cours" | "terminée" | "annulée",
    "due_date": datetime | None,
    "completed_date": datetime | None,
    "category": "rédaction" | "recherche" | "correspondance" | "dépôt"
              | "signification" | "suivi" | "admin" | "autre",
    "phase": str,                         # Phase O — même contrat que timeentries ;
    "sous_phase": str,                    # sérialisé en CATEGORIES (le CODE) +
                                          # X-PALLAS-PHASE/-SOUS-PHASE (VTODO)

    # Phase D3: link to parent note via RFC 5545 RELATED-TO;RELTYPE=PARENT
    "related_note_id": UUIDv4 | None,

    # DAV
    "vtodo_uid": UUIDv4,
    "dav_href": str,                      # STALE post-D1 — tasks are served from per-dossier collections
}
```

### `notes/{noteId}` — Dossier notes

```python
{
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "title": str,
    "content": str,                       # Markdown — rendered via the `markdown` Jinja filter
    "category": "appel" | "rencontre" | "recherche" | "stratégie"
              | "correspondance" | "audience" | "autre",
    "pinned": bool,

    # Analyse (July 2026 — both default False; absent on legacy docs)
    "dateless": bool,                     # True → note_to_vjournal OMITS DTSTART
                                          # (a pure jtx Board *Note*, not a dated
                                          # journal); CREATED/DTSTAMP stay emitted
    "is_analyse": bool,                   # True → the dossier's single « Théorie
                                          # de la cause » note (Analyse leaf) —
                                          # excluded from Notes views by default,
                                          # included on DAV/MCP paths via
                                          # list_notes(include_analyse=True)

    # DAV
    "vjournal_uid": UUIDv4,
}
```

Notes live in `/dav/dossier-{id}/{noteId}.ics` as VJOURNAL resources alongside that dossier's VTODOs.

### `protocols/{protocolId}` — Case protocols

```python
{
    "dossier_id": str, "dossier_file_number": str, "dossier_title": str,
    "title": str,                         # default "Protocole de l'instance"
    "protocol_type": "cq_simplifié" | "cs_ordinaire" | "conventionnel",
    "start_date": datetime, "end_date": datetime,
    "court": str,
    "notes": str,
    "status": "actif" | "complété" | "suspendu",
}
```

A dossier may have **multiple protocols** over its lifetime, but **at most one `actif`** at any time.

### `protocols/{protocolId}/steps/{stepId}` — Protocol steps (subcollection)

```python
{
    "order": int,
    "title": str, "description": str,
    "cpc_reference": str,                 # e.g., "art. 246 C.p.c."
    "deadline_date": datetime,
    "deadline_offset_days": int | None,   # null for conventionnel / custom-added steps
    "mandatory": bool,                    # True for CQ/CS template steps
    "deadline_locked": bool,              # True for CQ mandatory steps
    "status": "à_venir" | "en_cours" | "complété" | "en_retard",
    "completed_date": datetime | None,
    "linked_task_id": UUIDv4 | None,
    "linked_hearing_id": UUIDv4 | None,
    "notes": str,
    "date_confirmed": bool,               # CS suggested-date acknowledgement
    "phase": str,                         # Phase O — annotation de phase du litige :
    "sous_phase": str,                    # les gabarits CQ/CS la portent (mapping
                                          # approuvé 2026-08-10, épinglé par test) ;
                                          # la tâche auto-créée de l'étape l'hérite ;
                                          # "" sur une étape custom non classée
}
```

### `documents/{documentId}` — Document metadata

```python
{
    "dossier_id": str, "dossier_file_number": str,
    "folder_id": UUIDv4 | None,           # None = dossier root
    "filename": str,                      # Sanitized
    "original_filename": str,
    "display_name": str,                  # User-friendly
    "file_type": str,                     # MIME type
    "file_size": int,                     # bytes (max 200 MB since 2026-08-12)
    "storage_path": "users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}",
    "category": "procédure" | "pièce" | "correspondance" | "preuve"
              | "jugement" | "entente" | "note" | "autre",
    "description": str,
    "tags": list[str],
    "document_date": datetime | None,     # July 2026 — the DOCUMENT's own
                                          # date (a judgment's date, a
                                          # letter's date), MANUAL on the
                                          # upload/edit forms, date-only at
                                          # midnight UTC (date_str, never
                                          # to_mtl). NO backfill (worthless
                                          # to guess); list filters fall
                                          # back to created_at when absent
    "version": int, "parent_document_id": UUIDv4 | None,
}
```

Allowed MIME types: PDF, MS Word (doc/docx), Excel (.xls `application/vnd.ms-excel` / .xlsx `…spreadsheetml.sheet` — 2026-08-13), JPEG, PNG, TIFF, ZIP, courriels (.eml `message/rfc822` / .msg `application/vnd.ms-outlook`) — 11 types; **≤ 200 MB since 2026-08-12** (the byte paths are GCS-side: browser→GCS resumable upload + rewrite-copy ingestion — App Engine caps any request at 32 MB, so a through-app upload could never carry more). The five non-previewable additions (ZIP, .eml, .msg, .xls, .xlsx) are stored with `Content-Disposition: attachment` on the blob and never receive an inline signed URL (`routes/documents._PREVIEWABLE_TYPES`).

### `dav_sync/{collectionName}` — DAV sync state

```python
{
    "ctag": UUIDv4,                       # Regenerated on every collection change
    "sync_token": str,                    # Currently mirrors ctag (string, not a counter)
    "updated_at": datetime,
    # Subcollection: tombstones/{resourceId}
}
```

Collection names used:
- `"parties"` — addressbook
- `"hearings"` — shared calendar
- `"tasks"` — standalone tasks only
- `"dossier:{dossierId}"` — per-dossier collections (Phase D1+). The colon is valid in Firestore document IDs.

### OAuth collections (Phase I — MCP connector)

Three top-level collections backing the embedded OAuth 2.1 authorization server. **Documented exception to Architecture Rule 6:** document IDs are the lookup keys (client_id, or SHA-256 hex of the code/token), never UUIDv4 — raw credentials are never stored. No `etag` (not DAV-exposed). **Expiry is enforced in code on every read** (`expire_at` comparison in `mcp/store.py` callers); the Firestore TTL policies on `oauth_codes.expire_at` / `oauth_tokens.expire_at` are only garbage collection (deletion can lag by days), never a security control. No composite indexes needed (keyed `get()`s; family/client queries are single-field equality, auto-indexed).

#### `oauth_clients/{client_id}`

```python
{
    "client_id": str,                 # secrets.token_urlsafe(24); doc ID
    "client_name": str,               # sanitize()d at write, autoescaped at render
    "redirect_uris": list[str],       # validated against the Claude-callback allowlist at registration
    "token_endpoint_auth_method": "none",   # public clients only in v1
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "last_used_at": datetime | None,  # stamped at each successful token issuance
}
```

#### `oauth_codes/{sha256(code)}`

```python
{
    "client_id": str,
    "redirect_uri": str,              # exact URI used at /oauth/authorize
    "scope": str,                     # space-delimited; v1 always "athena:read"
    "code_challenge": str,            # PKCE, S256 only
    "code_challenge_method": "S256",
    "resource": str | None,           # RFC 8707 value received, if any
    "used": bool,                     # single-use guard (flipped transactionally)
    "family_id": str | None,          # stamped at consumption; enables replay → family revocation
    "expire_at": datetime,            # now + 300 s
}
```

#### `oauth_tokens/{sha256(token)}`

```python
{
    "token_type": "access" | "refresh",
    "client_id": str,
    "scope": str,
    "resource": str | None,
    "family_id": str,                 # uuid4 hex; shared by all tokens from one auth-code
    "revoked": bool,
    "rotated_to": str | None,         # hash of successor refresh token (audit trail)
    "expire_at": datetime,            # access: +60 min; refresh: +30 days
    "last_used_at": datetime | None,
}
```

### Trust collections (Phase K — fidéicommis)

Three new top-level collections (standard `id`/`created_at`/`updated_at`/`etag`; not DAV-exposed). **The exhaustive field list is in `SPEC_PHASE_K_FIDEICOMMIS.md` §3** — this is the shape summary.

- **`trust_accounts/{accountId}`** — a trust bank account. `account_type` ∈ `général`|`spécial` (only `général` exercised; `spécial` schema-only). **`account_number_last4` — last 4 digits ONLY, never the full number** (a payment credential); `transit` (5 digits). Denormalized balances maintained transactionally: `book_balance` (en_circulation + compensée) and `bank_balance` (compensée only). `status="fermé"` requires `book_balance == 0`.
- **`trust_transactions/{transactionId}`** — the register (both views). Field names map 1:1 onto the Barreau columns. `sequence` (continuous per account, never reused — from `counters/trust-{account_id}`), `date` (**date-only, midnight UTC**), `direction` ∈ `recette`|`déboursé`, `amount` (cents, **always positive** — direction carries the sign), `purpose` (`VALID_PURPOSES`; `correction` reserved for reversals), `method` (`VALID_METHODS`), the two **frozen** running balances `balance_after_account`/`balance_after_client`, `status` ∈ `en_circulation`|`compensée`|`annulée`, `cleared_date`, `reconciliation_id`, and the links `invoice_id`/`invoice_external_ref`/`reverses_id`/`reversed_by_id`/`related_transaction_id`. `counterparty`/`client_name`/`dossier_*` are **text snapshots, never FKs** (the register must show what was on the cheque). Nullable dossier/client (bank interest/fees have none). **A `virement_honoraires` must be backed by an invoice — EITHER a linked Athéna `invoice_id` (verified: issued, same dossier, amount ≤ solde dû) OR a free-text `invoice_external_ref` for a pre-Athéna paper invoice (recorded, NOT amount-verified; user decision 2026-07-17). Never both, never neither. The route resolves the Athéna invoice by NUMBER within the dossier (a typo hard-errors, never a silent downgrade to external).**
- **`trust_reconciliations/{reconciliationId}`** — a bank reconciliation. `period_end` (date-only), `statement_balance`, snapshot totals, `variance` (**must be 0 to complete**: `statement + deposits_in_transit − outstanding_cheques − book`), `status` ∈ `brouillon`|`complétée`, `cleared_transaction_ids`. One `brouillon` per account at a time.
- **`counters/trust-{account_id}`** — `{seq}`, same transactional mechanic as `counters/invoices-{year}`; never resets.

**`dossiers` gains three fields (Phase K):** `trust_balance` (cents — book, all clients), `trust_balance_by_client` (`{client_id: cents}`, book), `trust_cleared_by_client` (`{client_id: cents}`, cleared — the control). Absent on legacy docs → defaulted to `0`/`{}` on read by `_migrate_trust` (in the `_migrate_parties` chokepoint). Written only by `models/trust.py` (transactionally); `update_dossier` re-reads them just before its `set()` so a form save can't clobber a concurrent trust write.

### Administration collections (August 2026 — comptabilité d'administration)

Three top-level collections (standard `id`/`created_at`/`updated_at`/`etag`; not DAV-exposed), the firm-side sibling of Phase K for the **operations bank account and the corporate credit card**. Deliberate divergences from trust (user decisions 2026-08-13): **no backdating guard and no frozen per-row balances** — `date` is the free economic date (future refused, Montréal clock), `sequence` stays the insertion-order audit cursor, and running balances are **computed at read** in `(date, sequence)` order; **entries are EDITABLE/DELETABLE until the reconciliation lock** (a completed reconciliation freezes its whole period — after that, contre-passation only; edits keep a bounded on-document `revisions` trail, deletions go through `audit_events` with `entity_type="admin_transaction"`); **no overdraft control** and no `cleared`/`bank` balances — ONE denormalized figure, `admin_accounts.ledger_balance` (Σ of the status-blind `admin_delta` over every row).

- **`admin_accounts/{accountId}`** — `account_type` ∈ `opérations`|`carte_crédit` (immutable after creation — sign semantics and reconciliation wording hang off it), `institution`, `transit` (blank for a card), `account_number_last4` (never the full number), `ledger_balance` (cents; a card's runs NEGATIVE when money is owed — `display_balance` shows the positive « Solde dû »), `status` `actif`|`fermé` (**no zero-balance close rule** — an ops account may close overdrawn; `fermé` blocks creates only).
- **`admin_transactions/{transactionId}`** — `sequence` (counter `counters/admin-{account_id}`), `date` (date-only midnight UTC, FREE in the past down to the lock floor), `direction` (`recette`|`déboursé`, positive `amount`), `kind` ∈ `encaissement_facture`|`recette_autre`|`dépense`|`paiement_carte`|`correction` (the last two minted only by their own paths), `category` (17-value firm-expense vocabulary, required on a dépense — a THIRD taxonomy, never shared with `expense.VALID_CATEGORIES`), the **ventilation** `net_amount`/`gst_amount`/`qst_amount` (blank déboursé → net=amount, taxes 0 = no ITC/ITR claimed; otherwise `net+tps+tvq == amount` enforced), `counterparty` (payer/supplier snapshot), `supplier_invoice_ref` (supplier's OWN invoice number — distinct from `reference`, and never named `invoice_external_ref`, trust's pre-Athéna FEE invoice field), optional `dossier_id` + label snapshots (no client dimension), `invoice_id`/`invoice_number` (encaissement — bank accounts only), `trust_transaction_id` (auto-recette linkage, single-field auto-indexed), the four `receipt_*` fields, `status` `en_circulation`→`compensée`|`annulée`, `cleared_date`, `reconciliation_id`, `reverses_id`/`reversed_by_id`/`related_transaction_id` (card-payment pair), `revisions` (cap 25).
- **`admin_reconciliations/{reconciliationId}`** — the trust shape (≤1 brouillon/account, period_end after the last complétée and never future, variance must be 0, abandon-a-draft). A card's `statement_balance` is entered AS THE STATEMENT STATES IT (positive solde dû); `statement_to_ledger` converts once at variance time. **Completion LOCKS the period** and re-verifies each ticked entry's status AND etag in-transaction (entries are editable here until this very lock).
- **`counters/admin-{account_id}`** — `{seq}`, the trust counter convention.

### `audit_events/{eventId}` — Append-only deletion journal (July 2026)

Answers « qu'est-ce qui a disparu ? » for a sync-aware client (the MCP `list_deletions` tool + `updated_since` filters): DAV tombstones are pruned at 30 days, scoped per collection, and were verified misleading as a deletion feed — this journal is the durable, cross-entity record. **Rule-7 exception:** `created_at` only, no `etag`, never updated after creation. UUIDv4 doc IDs (Rule 6 holds). Written by `models/audit_event.record_deletion(...)` — called from every web delete route and DAV DELETE branch, **best-effort AFTER the committed delete** (its own `try/except` + `log_unexpected`; a journal failure must never fail or roll back the delete). Read by `list_recent(limit≤200)` — `at` DESC single-field index, Python filters, fails open to `[]`.

```python
{
    "id": UUIDv4,
    "at": datetime,                   # deletion instant (UTC)
    "entity_type": "task" | "hearing" | "note" | "partie" | "document"
                 | "folder" | "time_entry" | "expense" | "invoice"
                 | "protocol" | "protocol_step" | "doc_template" | "dossier",
    "entity_id": UUIDv4,
    "dossier_id": str,                # "" when none
    "snapshot_min": {"title": str, "status": str},   # labels only — enough
                                      # to say WHAT vanished, never a body
    "created_at": datetime,
}
```

### `mcp_idempotency/{sha256(tool:key)}` — MCP write-replay cache (July 2026)

The WP15 write protocol's storage: a successful write called with an `idempotency_key` records its result here; retrying the SAME tool+key within 24 h returns the stored result (`idempotent_replay: true`) instead of duplicating, and the same key with DIFFERENT args is refused (`args_hash` fingerprint, protocol args excluded). **Rule-6/7 exception:** the doc ID is the keyed hash (raw keys never stored — the OAuth-collections pattern), no `etag`, write-once. Expiry (`expire_at`, +24 h) is enforced **in code on every read**; the Firestore TTL fieldOverride in `firestore.indexes.json` is garbage collection only (same doctrine as `oauth_codes`/`oauth_tokens`). The cache **fails open in both directions** (a Firestore hiccup must never block a write, nor a replay lookup invent one), and a REFUSED call records nothing — only committed results are replayable.

```python
{
    "tool": str,                      # WRITE_TOOLS member
    "args_hash": str,                 # sha256 fingerprint (idempotency_key
                                      # and dry_run excluded)
    "result": dict,                   # the stored tool payload, replayed verbatim
    "created_at": datetime,
    "expire_at": datetime,            # +24 h; enforced in code, TTL = GC only
}
```

### `budgets/{budgetId}` — Per-dossier phase budgets (August 2026)

**Append-only, VERSIONED — no `update_*`/`delete_*` ever** (the trust register doctrine, for a deontological reason: the duty to inform the client of the foreseeable cost requires proving WHEN the information was given — Phase O spec §10 — and an overwritten budget destroys that proof). Every save mints a NEW immutable doc; the newest `(version, created_at)` is authoritative (`get_latest_budget`), older versions stay readable (`/budgets/historique`) and each one's « Estimation » PDF is its reproducible rendering. Versioning is Python-side (`_next_version` = 1+max, no transaction — single-user; a double-submit's worst case is a duplicate version number, disambiguated by the `created_at` tie-break, never a data loss). No composite index (`where dossier_id ==` + Python sort).

```python
{
    "dossier_id": str,
    "version": int,                   # ≥ 1, per-dossier, minted at create
    "hourly_rate": int,               # cents — FROZEN into the version
    "note": str,                      # free-text assumptions
    "lines": [                        # phase derived from the prefix, never stored
        {"sous_phase": "PRE-01",      # ∈ phases.SOUS_CODES, REQUIRED non-empty
                                      # (no DAV/MCP/legacy path — hard requirement
                                      # is safe here, unlike D-6); ADM/HOR REFUSED
                                      # (D-14: withdrawn from the client quote)
         "hours": float, "frais_cents": int},
        ...
    ],
    # created_at/updated_at/etag present (Rule 7) though never updated.
}
```

### Named database « portail » — `invitations/{invitationId}` (portail client L1)

A **separate named Firestore database** (`gcloud firestore databases create --database=portail`), NOT a collection of the default DB. Accessed through dedicated lazy `firestore.Client(database="portail")` instances — never `models.db`, never `firebase_admin.firestore`. **Single-writer principle:** the main service (`models/portail_invitation.py`) writes everything; the portal service (`client/services/invitations.py`) only reads and signals via Cloud Tasks (IAM backstop: `datastore.viewer` conditioned to this database). No `etag` (not DAV-exposed); no composite index (single-field order/equality only — filters applied in Python over a bounded read).

```python
{
    "id": UUIDv4, "type": "documents" | "intake",   # « intake » réservé à L3
    "email": str,                    # minuscules — l'adresse invitée
    "partie_id": str | None, "dossier_id": str | None,
    "display_label": str,            # LA SEULE désignation vue du client —
                                     # jamais un intitulé révélant la partie
                                     # adverse ni un mémo interne (§5: tout ce
                                     # document est lisible par le service
                                     # PUBLIC)
    "statut": "envoyée" | "ouverte" | "soumise" | "traitée" | "refusée" | "révoquée",
    "created_at": ts, "updated_at": ts,
    "expires_at": ts,                # expiration LOGIQUE (vérifiée à chaque
                                     # requête) — aucun statut « expirée »
    "resend_count": int,
    "quota_files": int, "quota_mb": int,
    "soumissions": [ {"batch": str, "files_count": int,
                      "total_bytes": int, "recu_at": ts} ],
    "accuses": { "<batch>": True },  # test-and-set transactionnel — l'unique
                                     # garde de l'unique effet non idempotent
                                     # (l'accusé courriel, au plus une fois)
    "prefill": None | dict,          # L3 : instantané NON SENSIBLE d'une
                                     # partie (models.portail_invitation
                                     # .prefill_depuis_partie — LISTE BLANCHE :
                                     # ni notes, ni conformité, ni liaison de
                                     # dossier, ni date de naissance) pour
                                     # préremplir le formulaire d'ouverture
}
```

The quarantine bucket (`athena-pallas-portail-quarantaine`) carries the durable truth: `submissions/{inv}/{batch}/files/{seq:03d}_{nom_assaini}` + `envelope.json` (portal, create-only `if_generation_match=0`) + `manifeste.json` (main service: SHA-512 hashes, per-file `etat` ∈ reçu/versé/refusé/manquant, copied `http`/`submitted_at`); processed lots move envelope+manifest under `archive/` (lifecycle: submissions 90 d, archive 365 d).

### `doc_templates/{templateId}` — Document templates ("gabarits", Phase H)

Top-level collection; standard common fields (`id`, `created_at`, `updated_at`, `etag`). Not DAV-exposed — no DAV UID, no CTag bumping. Template files live in **Storage** at `users/{userId}/templates/{templateId}/{filename}` (signed URLs, 15-min expiry) and are **not** `documents` records; generated outputs saved into a dossier ARE regular `documents` records (independent copies — deleting a gabarit never touches them). No composite index (small collection: single `order_by("name")`, category/search filtered client-side).

```python
{
    "name": str,                       # ≤120 chars, required
    "description": str,
    "category": "procédure" | "correspondance" | "autre",
    "kind": "gabarit" | "note_honoraires" | "note",  # Phase H.2/H.3 discriminator (default
                                       # "gabarit"); "note_honoraires" flags the
                                       # invoice template /factures fills. Kept
                                       # separate from category. Legacy docs
                                       # (no kind) read as "gabarit".
    "filename": str,                   # secure_filename()d, .docx
    "original_filename": str,
    "file_size": int,                  # bytes (≤ 10 MB)
    "storage_path": "users/{userId}/templates/{templateId}/{filename}",
    "version": int,                    # starts at 1, +1 on each file replacement

    # Extracted at upload / file replacement (utils/docx_fill + utils/template_fields).
    # Classification is also recomputed on every render (route re-classifies),
    # so these stored lists are informational; stale ones on older docs never
    # drive behavior. (Legacy docs may still carry a `block_fields` list — it
    # is ignored; the ALL-CAPS→block concept was removed July 2026.)
    "placeholders": list[str],         # distinct {{...}} names, document order
    "auto_fields": list[str],          # resolvable from the field catalog (case-insensitive)
    "manual_fields": list[str],        # known letter-metadata inputs (MANUAL_FIELDS)
    "passthrough_fields": list[str],   # left verbatim in the .docx for Word (blocks,
                                       # civilité, salutations, unknown names)
    "slots_required": list[str],       # ⊆ {"dossier","client","adverse","destinataire"}
    "validation_warnings": list[str],  # French split-run warnings at last upload
}
```

> **The reference tables are read from memory, not Firestore.** `models/reference.py` embeds `_PALAIS` / `_GREFFES` / `_JURIDICTIONS` as module-level dicts and every lookup hits those; the three `ref_*` collections below are a **mirror seeded for a future admin UI that nothing reads today**. `scripts/seed_reference_data.py` imports the in-memory tables rather than re-listing them (they were duplicated literals and had already drifted — `other_locations` existed only in the script, so `get_greffe()` never returned it). **Edit `models/reference.py`; re-seed only to refresh the mirror.**

### `ref_greffes/{greffeNumber}` — Quebec courthouse reference (top-level, read-only)

Document ID is the 3-digit greffe number (string). Seeded from `scripts/seed_reference_data.py`.

```python
{
    "greffe_number": "500",
    "palais_de_justice": "Montréal",
    "district_judiciaire": "Montréal",
    "point_de_service": bool,             # True = itinerant circuit greffe.
                                          # NOT the MJQ "point de service de
                                          # justice" notion — see ref_palais.
    "palais_key": "montreal" | None,      # → ref_palais / _PALAIS; None = no
                                          # published civic address (the 4
                                          # itinerant greffes + 525 + 715)
    "other_locations": list[str],         # For shared greffes (614, 635, 640, 652);
                                          # seed-script-only, absent in-memory
    "updated_at": datetime,
}
```

### `ref_palais/{palaisKey}` — Court locations & addresses (top-level, read-only)

Document ID is a stable ASCII slug (`montreal`, `saint-jerome`, `val-dor`). **51 entries: 43 palais de justice + 8 points de service de justice** (MJQ, « Trouver un palais de justice », extracted 2026-07-15). Addresses mirror the `parties` address convention (street = civic number + name, unit separate, full province/country names) so a resolved address drops into the existing address shape.

A location is a **building**; a greffe is a **registry** sitting in one. The relationship is neither 1:1 nor total, which is why addresses are keyed separately rather than stored on the greffe: **6 greffes have no published address** (the 4 itinerant circuit greffes 614/635/640/652, plus 525 « Montréal - Chambre de la jeunesse » and 715 Sainte-Agathe-des-Monts, absent from the extraction), and **Kuujjuaq is a published courthouse no greffe number names** — it is kept unreferenced rather than guessed onto a Nunavik circuit greffe.

```python
{
    "palais_key": "chicoutimi",
    "name": "Chicoutimi",                 # MJQ courthouse name…
    "city": "Saguenay",                   # …which may differ from the city
    "location_type": "palais" | "point_de_service",
    "street": "227, rue Racine Est",
    "unit": "1er étage",                  # "" when none
    "province": "Québec", "country": "Canada",
    "postal_code": "G7H 7B4",             # "A1A 1A1" normalized form
    "mailing_address": str,               # "" unless the MJQ publishes a
                                          # distinct one (Percé, Forestville)
    "updated_at": datetime,
}
```

### `ref_juridictions/{juridictionNumber}` — Tribunal/competence reference (top-level, read-only)

Document ID is the 2-digit juridiction number, zero-padded (string).

```python
{
    "juridiction_number": "05",
    "tribunal": "Cour supérieure",
    "competence": "Division générale",
    "greffe_type": "GC" | "GP" | "GI",   # civil / criminal-penal / provincial statutory
    "updated_at": datetime,
}
```

---

## Routes Reference

All UI routes require `@login_required` (in `auth.py`). DAV routes use `@dav_auth_required` (Basic Auth) and the DAV blueprints are CSRF-exempt.

### `auth_routes.py` — `/auth/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/auth/login` | GET | Login page |
| `/auth/verify-token` | POST | Receive Firebase ID token, create session |
| `/auth/mfa-setup` | GET | MFA enrolment page |
| `/auth/mfa-manage` | GET | MFA management page |
| `/auth/logout` | POST | Clear session |

### `dashboard.py` — `/`

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Landing dashboard: hearings (next 7 days + 7-60 days), urgent tasks (≤14 days or overdue), urgent protocol steps, prescription alerts (within 60 days, based on the EFFECTIVE date from `derive_prescription`, with judicially-adjusted last-action date, **excluding interrupted dossiers** — depot event or legacy `prise_action_date`), quick stats (open dossiers, unbilled hours/amount, outstanding invoices) |

### `parties.py` — `/parties/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/parties/` | GET | List view with role + type filters, search |
| `/parties/search` | GET | HTMX autocomplete |
| `/parties/mandataire-search` | GET | HTMX picker for the mandataires list — filtered to `type=individual`, matching `contact_role`. Accepts `exclude` as a comma-separated list of ids (typically `<self>` plus every already-picked mandataire) |
| `/parties/<id>` | GET | Detail view |
| `/parties/new` | GET | Create form |
| `/parties/` | POST | Create submit |
| `/parties/<id>/edit` | GET | Edit form |
| `/parties/<id>` | POST | Edit submit |
| `/parties/<id>/delete` | POST | Delete (with FK safety check) |
| `/parties/export/csv` | GET | CSV export |
| `/parties/export/pdf` | GET | PDF export |

> KYC fields (`identity_verified`, `conflict_check`, KYC notes) are edited **inline through the regular party form** today. The model exposes `update_kyc_status` and `link_kyc_document` helpers, but no dedicated `/parties/<id>/kyc/*` routes are wired up yet.

### `dossiers.py` — `/dossiers/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/dossiers/` | GET | List with status tabs (actif / en_attente / fermé / archivé / tous) |
| `/dossiers/<id>` | GET | Detail (hub page). Reads `?tab=<leaf>` to pick the initial leaf (fallback `temps`) and derives the initial group |
| `/dossiers/<id>/tab/<tab_name>` | GET | HTMX tab loader. Leaf slugs: `apercu` (cartes Juridiction/Recours/Prescription/Mandat), `temps` — default, `facturation`, `fideicommis`, `budget` (budget-vs-réalisé par phase), `audiences` (« Calendrier »), `taches`, `protocole`, `documents` (« Fichiers »), `notes`, `analyse` (théorie de la cause); legacy `agenda` maps to `audiences` |
| `/dossiers/<id>/analyse/init` | POST | Create the dossier's « Théorie de la cause » note (idempotent — returns the existing one untouched; bumps `dossier:{id}` only on actual creation); responds with the re-rendered `_tab_analyse.html` fragment |
| `/dossiers/new` | GET | Create form |
| `/dossiers/` | POST | Create submit |
| `/dossiers/<id>/edit` | GET | Edit form |
| `/dossiers/<id>` | POST | Edit submit |
| `/dossiers/<id>/delete` | POST | Delete |
| `/dossiers/parse-court-file` | POST | **Phase G** — JSON endpoint returning judicial metadata from a court file number |
| `/dossiers/export/csv` | GET | CSV export |
| `/dossiers/export/pdf` | GET | PDF export |

### `time_expenses.py` — `/temps/*`

Time entries live at the prefix root; expenses live under `/depenses`. No `/heures` segment.

| Route | Method | Purpose |
|-------|--------|---------|
| `/temps/dossier-search` | GET | HTMX dossier autocomplete |
| `/temps/` | GET | Standalone view with Heures/Dépenses tabs |
| `/temps/new` | GET | Time entry form |
| `/temps/` | POST | Time entry create |
| `/temps/<entry_id>/edit` | GET | Edit |
| `/temps/<entry_id>` | POST | Update |
| `/temps/<entry_id>/phase` | GET / POST | **Reclassement de phase** (August 2026) — the phase-ONLY form, reachable on a billed entry (which the edit form still refuses). Renders a read-only recap plus the shared `components/_phase_selector.html`; the form posts nothing but `phase`/`sous_phase`, and a test pins that input inventory |
| `/temps/<entry_id>/delete` | POST | Delete |
| `/temps/depenses/new` | GET | Expense form |
| `/temps/depenses` | POST | Expense create |
| `/temps/depenses/<expense_id>/edit` | GET | Edit |
| `/temps/depenses/<expense_id>` | POST | Update |
| `/temps/depenses/<expense_id>/phase` | GET / POST | Same, for a disbursement (shared template `time_expenses/phase_form.html`) |
| `/temps/depenses/<expense_id>/delete` | POST | Delete |
| `/temps/export/{csv,pdf}` | GET | Time entries export |
| `/temps/depenses/export/{csv,pdf}` | GET | Expenses export |

### `invoices.py` — `/factures/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/factures/` | GET | List with status filter |
| `/factures/new` | GET | Creation flow (select dossier → pick unbilled items) |
| `/factures/unbilled/<dossier_id>` | GET | HTMX list of unbilled time entries + expenses for selection |
| `/factures/` | POST | Create invoice |
| `/factures/<id>` | GET | **Data sheet** (August 2026) — a structured reading of what is stored: identification (+ links to the dossier and the client), the frozen billing snapshot (address, tax numbers, terms), the fee/disbursement line items, the totals cascade and the live **Solde**, the read-only « **Paiements** » block (the register entries imputed on it — the payment FORM was removed 2026-08-17: it predated the accounting module and was a second, ledger-blind writer of `amount_paid`), the notes. **Not a facsimile of the invoice**: no firm letterhead, no « FACTURE » title, no `@media print` — the client-facing document is the Word note d'honoraires below |
| `/factures/<id>/status` | POST | Transition status (envoyée/payée/annulée…) |
| `/factures/<id>/void` | POST | Annul and release linked time entries/expenses |
| `/factures/<id>/delete` | POST | Hard-delete a cancelled invoice |
| `/factures/<id>/note-docx` | POST | **Phase H.2** — generate the Word note d'honoraires from this invoice via the `kind="note_honoraires"` gabarit; save the `.docx` into the dossier's « **Projets** » folder (`GENERATED_FOLDER_NAME`) under the name `"{file_number} - YYYY-MM-DD - Projet {template} {invoice_number}"` (`projet_document_name`); HTMX success partial (`_note_generated.html`). Refuses `annulée`; French message if no note template exists. **This is the ONE client-facing rendering of an invoice** — the detail page became a data sheet in August 2026 precisely so there is only one document to keep in step. (The list-level `/factures/export/pdf` is a different animal: the Barreau's « Journal des honoraires », a book of account, never a client document) |
| `/factures/export/csv` | GET | CSV export of the filtered list (9 columns, unchanged) |
| `/factures/export/pdf` | GET | **« Journal des honoraires »** (August 2026) — the Barreau du Québec's fee-journal sheet: **legal paper, landscape**, 13 columns in the model's own order (Date · Client · N/Réf · N° de note · Honoraires · Débours TX · Débours NTX · Sous-total · TPS · TVQ · Total · Sommes reçues · Solde), chronological (oldest first — the screen list reads newest first), a totals row, and the active filters spelled out as a subtitle. The **N° de note drops the file-number prefix** the N/Réf column already carries (`2026-001-03` → `03`) — HISTORICAL rows only since the 2026-08-12 numbering revert (the six per-file invoices of the 2026-07-17→08-12 parenthesis); it fires only when that prefix is literally this row's reference AND the remainder is all digits, so a `YYYY-FNNN` (every invoice since), a row with no N/Réf, and a free-form file number like « 2026 » all stay whole (`journal_pdf._short_note_number`). Honours the same filters as the list. Built by `utils/journal_pdf.py`, never `utils/export_pdf.py` |

### `hearings.py` — `/audiences/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/audiences/dossier-search` | GET | HTMX dossier autocomplete |
| `/audiences/` | GET | Upcoming hearings list + monthly grid toggle |
| `/audiences/new` | GET | Hearing form |
| `/audiences/` | POST | Create |
| `/audiences/<id>` | GET | Detail |
| `/audiences/<id>/edit` | GET | Edit form |
| `/audiences/<id>` | POST | Update |
| `/audiences/<id>/delete` | POST | Delete. `scope` ∈ `occurrence` (défaut) \| `suivantes` : « et les suivantes » supprime la chaîne à partir de cette occurrence via `delete_series(from_date=…)`. Le `serie_id` est **relu de l'audience STOCKÉE**, jamais du formulaire (voir le Known Gotcha `serie_id == ""`), et le pivot est le plus TARDIF entre le jour de l'occurrence et aujourd'hui — une occurrence passée n'est jamais touchée. Un refus voyage sur une **redirection 2xx** avec `?erreur=` (htmx n'échange que les 2xx) |
| `/audiences/<id>/detacher` | POST | **Séries** — détache une occurrence : `serie_id`/`serie_rule` remis à vide, elle devient une audience ordinaire. Un seul champ de part et d'autre : ni maître à promouvoir, ni index à renuméroter. Bumpe le CTag (l'appartenance a changé) |
| `/audiences/?serie=<id>` | GET | **Séries** — la vue d'UNE chaîne (passé compris, sans filtres). Sans elle il n'y a aucun moyen de vérifier qu'une action de série a fait ce qu'on lui a demandé, et la liste ordinaire se coupe à 100 lignes sans commande pour aller plus loin. La création y redirige |
| `/audiences/export/{csv,pdf}` | GET | Export |

### `tasks.py` — `/taches/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/taches/dossier-search` | GET | HTMX dossier autocomplete |
| `/taches/` | GET | List grouped by status |
| `/taches/new` | GET | Form (accepts `?related_note_id=` prefill) |
| `/taches/` | POST | Create |
| `/taches/<id>` | GET | Detail (shows linked note if any) |
| `/taches/<id>/edit` | GET | Edit |
| `/taches/<id>` | POST | Update |
| `/taches/<id>/toggle` | POST | HTMX checkbox complete/reopen |
| `/taches/<id>/delete` | POST | Delete |
| `/taches/export/{csv,pdf}` | GET | Export |

### `notes.py` — `/notes/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/notes/dossier-search` | GET | HTMX dossier autocomplete |
| `/notes/` | GET | List (optionally `?dossier_id=`) |
| `/notes/new` | GET | Form (requires dossier_id) |
| `/notes/` | POST | Create |
| `/notes/<id>` | GET | Detail (markdown rendered, shows linked tasks) |
| `/notes/<id>/edit` | GET | Edit |
| `/notes/<id>` | POST | Update |
| `/notes/<id>/pin` | POST | Toggle pinned |
| `/notes/<id>/gabarit-docx` | POST | **Phase H.3** — fill the note-print gabarit (kind `"note"`) from this note → **direct .docx download** (never saved to the dossier); markdown body → real Word formatting via `rich_values`; Analyse notes included; errors → redirect with `?gabarit_erreur=` banner |
| `/notes/<id>/delete` | POST | Delete |
| `/notes/export/{csv,pdf}` | GET | Export |

### `protocols.py` — `/protocoles/*`

| Route | Method | Purpose |
|-------|--------|---------|
| `/protocoles/` | GET | List |
| `/protocoles/new` | GET | Creation wizard (dossier → type → start date) |
| `/protocoles/` | POST | Create |
| `/protocoles/<id>` | GET | Detail (timeline view) |
| `/protocoles/<id>/edit` | GET | Edit form |
| `/protocoles/<id>` | POST | Update protocol metadata (incl. start-date recompute) |
| `/protocoles/<id>/delete` | POST | Delete |
| `/protocoles/<id>/steps` | POST | Add a custom step |
| `/protocoles/<id>/steps/<step_id>` | POST | Update step (deadline, notes, status) |
| `/protocoles/<id>/steps/<step_id>/complete` | POST | Toggle step completion (syncs linked task) |
| `/protocoles/<id>/steps/<step_id>/delete` | POST | Delete (blocked when `mandatory`) |

> There are no separate `/protocoles/<id>/complete` or `/protocoles/<id>/suspend` routes today — protocol completion happens automatically via `_check_protocol_completion`, and status changes go through the regular update form.

### `documents.py` — `/documents/*`

The documents blueprint is **mounted at `/documents`** (not nested under `/dossiers/<id>/`). The dossier context is passed via query string (`?dossier_id=…`) on GETs and via form fields on POSTs.

| Route | Method | Purpose |
|-------|--------|---------|
| `/documents/` | GET | Folder-aware browser (filterable by `?dossier_id=`, `?folder_id=`, `?category=`, `?q=`, `?sort=`) |
| `/documents/<id>` | GET | Viewer (signed URL) |
| `/documents/<id>/download` | GET | Signed URL redirect |
| `/documents/upload` | GET | Upload form — the submit is DIRECT-to-GCS since 2026-08-12 (no multipart POST any more); on success the JS falls back to the browser SCOPED to the upload's dossier/folder (2026-08-13 — the old multipart semantics restored; an explicit `return_to`, validated at render, still wins) |
| `/documents/zip` | GET | « Télécharger (zip) » (2026-08-13) : composes the folder subtree's archive IN GCS (`build_folder_zip_url` — streaming, ZIP_STORED, all-or-nothing) then 302s to a 15-min signed URL; refusal → bounce to the scoped browser with `?erreur=`. No `folder_id` = the whole dossier |
| `/documents/api/televersement` | POST | JSON: opens a resumable GCS session under `staging/{uid}/` (extension + ≤ 200 MB gate; `size=` enforced by GCS itself; CORS via the session's `origin=`) |
| `/documents/api/finaliser` | POST | JSON: sniffs a 512-byte probe of the staging object, ingests it by GCS-side copy (`ingest_blob_as_document`), then CONSUMES the staging blob (success and refusal alike; an unfinalized orphan is swept by the bucket's `staging/` lifecycle rule) |
| `/documents/<id>/edit` | GET / POST | Edit metadata |
| `/documents/<id>/move` | POST | Move to a folder (Firestore-only, Storage path unchanged) |
| `/documents/move-bulk` | POST | Batch move |
| `/documents/<id>/delete` | POST | Delete (Storage + Firestore) |
| `/documents/folders/create` | POST | Create folder |
| `/documents/folders/<fid>/rename` | POST | Rename |
| `/documents/folders/<fid>/move` | POST | Change parent (with circular-ref check) |
| `/documents/folders/<fid>/delete` | POST | Delete the folder AND its subtree. `contents` (from the dialog's two distinct forms) decides the files: `move` — they go to the deleted folder's parent, one write each (the default, and the fallback for ANY unrecognised value) — or `delete`, the app's ONE destructive cascade (GCS bytes included, ≤ `MAX_FOLDER_DELETE_DOCUMENTS`). Mints **one `audit_events` row per entity** — every document, every sub-folder. `?message=` reports what disappeared; `?erreur=` bounces a failure (the non-HTMX branch used to drop it) |
| `/documents/folder-tree` | GET | HTMX folder tree (for move modal) |

### DAV endpoints (`/dav/*`)

| Endpoint | Purpose |
|----------|---------|
| `/.well-known/carddav` | 301 → `/dav/` |
| `/.well-known/caldav` | 301 → `/dav/` |
| `/dav/` | Root: `OPTIONS` + `PROPFIND`. Advertises `addressbook-home-set` and `calendar-home-set`. Depth:1 lists all collections dynamically (addressbook, « Général », and `/dav/dossier-{id}/` for each `actif`/`en_attente` dossier) |
| `/dav/addressbook/` + `/{id}.vcf` | CardDAV — contacts |
| `/dav/general/` + `/{id}.ics` | **« Général »** (July 2026): VEVENT + VTODO + VJOURNAL for every item with **no dossier** — hearings, tasks AND notes. Replaced `/dav/calendar/` and `/dav/tasks/`, which are **gone** |
| `/dav/dossier-{did}/` + `/{id}.ics` | **Per-dossier CalDAV collection** (Phase D1+D2, extended July 2026): VEVENT hearings + VTODO tasks + VJOURNAL notes of this dossier |

All DAV endpoints support: `OPTIONS`, `PROPFIND` (Depth 0/1), `REPORT` (sync-collection / addressbook-multiget / calendar-multiget), `GET`, `PUT` (with `If-Match` / `If-None-Match`), `DELETE` (with `If-Match`).

### MCP connector endpoints (Phase I)

All served through Cloudflare like every other route. (No Cloudflare Access application exists on this zone — see Security Rules; if one is ever added for `/dav/*`, these paths must stay outside it.) The `MCP_ENABLED` kill switch 404s every row of this table.

| Route | Methods | Auth | CSRF | Purpose |
|---|---|---|---|---|
| `/mcp` | POST | Bearer token | exempt | MCP Streamable HTTP endpoint (stateless JSON mode; one JSON-RPC message per POST) |
| `/mcp` | GET, DELETE | — | — | `405` (no SSE stream, no sessions — `Mcp-Session-Id` is never issued) |
| `/.well-known/oauth-protected-resource/mcp` | GET | none | — | RFC 9728 protected-resource metadata |
| `/.well-known/oauth-protected-resource` | GET | none | — | Same document (fallback) |
| `/.well-known/oauth-authorization-server` | GET | none | — | RFC 8414 AS metadata |
| `/oauth/register` | POST | none (10/h per IP) | exempt | RFC 7591 DCR — redirect URIs restricted to Claude's callbacks |
| `/oauth/authorize` | GET | `@login_required` | n/a | French consent screen (`templates/mcp/consent.html`) |
| `/oauth/authorize` | POST | `@login_required` | **enforced** | Consent decision → 302 with code (+state) |
| `/oauth/token` | POST | public client + PKCE (60/h per IP) | exempt | Code exchange + refresh rotation |
| `/oauth/revoke` | POST | token self-auth (60/h per IP) | exempt | RFC 7009 revocation (refresh token → whole family) |

The **26 read-only tools** (`get_agenda`, `list_dossiers`, `get_dossier`, `list_tasks`, `list_hearings`, `list_notes`, `get_note`, `list_documents`, `list_parties`, `get_partie`, `get_billing_snapshot`, `list_protocol_steps`, `compute_judicial_deadline`, `parse_court_file_number`, the Phase-K trust tools `get_trust_balance`, `list_trust_transactions`, `get_trust_snapshot`, the July 2026 additions `list_time_entries`, `list_expenses`, `list_deletions`, the August 2026 additions `list_invoices`, `get_invoice`, `get_coverage_report`, and the lot-Q additions `get_reference_vocabulary` (the controlled vocabularies the models validate but never enumerate in a refusal), `find_imported` (the durable anti-duplicate lookup by `legacy_ref`) and `get_import_audit` (IMP-01..07 over one reprised dossier)) and the **21 write tools** (`create_note`, `append_to_note`, `create_task`, `create_hearing`, `create_time_entry`, `create_expense`, `complete_dossier`, `record_signification`, `record_prescription_event`, `complete_task`, lot Q's `create_partie`, `update_partie`, `create_dossier`, `update_dossier`, `update_time_entry`, `update_expense`, `import_invoice`, and the August 2026 reclassifiers `set_time_entry_phase`, `set_expense_phase`, `set_time_entry_phase_bulk`, `set_expense_phase_bulk` — pinned in `mcp.tools.WRITE_TOOLS`, requiring `athena:write`; every one runs through `mcp/write_support.run_write` for `dry_run` + `idempotency_key`; **none can delete anything, set an invoice status, or record a payment**) live in `mcp/handlers.py` with schemas in `mcp/tools.py`. `tools/list` is **filtered by granted scope**, so a read-only connection never sees the write tools. Descriptors are **fully specified** (July 2026): French `title` + `annotations.title` mirror (2025-03-26 compat), per-usage `description` on every input property, and a declared `outputSchema` per tool (`mcp/output_schemas.py`) with `structuredContent` emitted on protocol ≥ 2025-06-18 — see the Known Gotchas entry for the three schema rules. **Trust tools never emit the bank transit or account number** (`list_trust_transactions` emits neither transit/last4/institution; `get_trust_snapshot` may emit the account name + institution but never the transit or last4). Conventions: money as `*_cents` + fr-CA `*_display`; date-only fields as `YYYY-MM-DD` (UTC calendar date); true timestamps as ISO-8601 America/Montreal; every list tool capped at 50 items with a `truncated` flag; no signed URLs or storage paths ever in tool output. **Phase O (août 2026) :** `create_task`/`create_time_entry`/`create_expense` acceptent `phase`/`sous_phase` **optionnels** — enums **DÉRIVÉS** de `utils/phases.py` (module pur importé directement par `mcp/tools.py`, le précédent `_COVERAGE_CODES` — jamais des littéraux copiés) ; `sous_phase` seule → phase déduite du préfixe ; phase seule → `-00` imputé ; couple contradictoire → refus français AVANT écriture (`handlers._resolve_phase_pair`) ; l'écho `entity` porte les deux champs (contrat outputSchema).

### `doc_templates.py` — `/gabarits/*` (Phase H)

All `@login_required`. Template upload/replace POSTs carry multipart `.docx` (10 MB cap — see Security Rules). The generation popup is an HTMX modal whose selection state is **server-owned**: slot changes re-render the field form; clicked search results carry their selection as `set_*` query params (which win over the `hx-include`-carried current state).

| Route | Method | Purpose |
|-------|--------|---------|
| `/gabarits/` | GET | List (name, category badge, version, placeholder count, warnings badge); FAB "+"; empty state invites first upload |
| `/gabarits/new` | GET | Upload form (multipart: file + name + description + category) |
| `/gabarits/` | POST | Create → redirect to detail (which shows the extracted field inventory + split-run warnings) |
| `/gabarits/<id>` | GET | Detail: metadata, auto/manual/passthrough field chips, warnings, « Générer », « Télécharger le gabarit », « Modifier », « Supprimer » |
| `/gabarits/<id>/edit` | GET | Edit form (metadata + optional replacement file) |
| `/gabarits/<id>` | POST | Update (file replacement → re-validate, re-extract, version += 1, old Storage object deleted) |
| `/gabarits/<id>/delete` | POST | Delete (Firestore doc + Storage object; generated documents untouched) |
| `/gabarits/<id>/download` | GET | Redirect to signed URL (15 min) |
| `/gabarits/dossier-search` | GET | HTMX dossier autocomplete (rows reload the modal with `set_dossier_id`) |
| `/gabarits/partie-search` | GET | HTMX partie autocomplete (rows reload the field form with `set_destinataire_id`; optional `?role=`) |
| `/gabarits/generer` | GET | Popup step 1 (modal partial): template select (or fixed via `?template_id=&fixed=1`), dossier picker (locked via `?locked=1`), prefills from `?dossier_id=` / `?partie_id=` (→ destinataire slot) |
| `/gabarits/generer/champs` | GET | Popup step 2 (field-form partial): slot selects + one editable input per **auto/manual** placeholder (prefilled via `resolve_values`, manual defaults applied); **passthrough** placeholders are listed read-only as « À compléter dans Word » (left verbatim in the output) |
| `/gabarits/generer` | POST | Generate: fill → dossier present → save via `document.upload_document` into the « **Projets** » folder (`GENERATED_FOLDER_NAME`), display_name `"{file_number} - YYYY-MM-DD - Projet {name}"` (`projet_document_name`), category from template + HTMX success partial; no dossier → direct `.docx` attachment (plain POST, `target="_blank"`) |

**Entry points:** dossier detail header + Documents-tab toolbar (« Générer depuis un gabarit », dossier locked), partie detail header (« Générer un document », partie → destinataire), gabarit list rows/detail (« Générer »). Each host page carries a `<div id="gabarit-modal">` mount point.

### `trust.py` — `/fideicommis/*` (Phase K — fidéicommis)

All `@login_required`, French UI, standard CSRF (no exemption), default 1 MB request cap (no exemption). Standard POST+redirect with inline error boxes; HTMX only for the autocompletes + the reconciliation live variance.

| Route | Method | Purpose |
|---|---|---|
| `/fideicommis/` | GET | **Journal de caisse** — per-account, cursor pagination (`sequence` DESC); filters (compte/statut/sens/période) use a bounded 200-row fallback. Header: book/bank/outstanding/in-transit + overdue-reconciliation badge |
| `/fideicommis/nouvelle` · `/` | GET · POST | Entry form · create (refuses `purpose="correction"`). Since 2026-08-12 the paiement-d'honoraires invoice is picked from a **SELECT of the dossier's issued invoices** (number + solde dû — exactly what the transactional verification accepts/caps) instead of hand-transcribed; `_resolve_invoice_number` (exact string, dossier-scoped) remains the final verdict, and `invoice_external_ref` stays the free-text path for pre-Athéna paper invoices. **Since 2026-08-13** the virement block also carries a « Compte d'administration (recette automatique) » select — after the committed transfer the route mints the matching admin recette (fail-open, banner on failure; see the admin gotcha) — and the label reads « Paiement d'honoraires » (key `virement_honoraires` unchanged) |
| `/fideicommis/factures-du-dossier` | GET | HTMX partial (`trust/_facture_options.html`): the issued-invoice select, reloaded via the « rafraichir-factures » custom event when the entry form's dossier changes ($nextTick AFTER setting the hidden dossier_id, so hx-include reads the fresh value; a `function () {}` expression on purpose — an arrow's `>` inside an attribute breaks naive `<input[^>]*>` tag parsing, see test_dossier_search_inputs_send_a_query_param) |
| `/fideicommis/<id>` | GET | Detail: all fields, both frozen balances, links to reversal pair / other transfer leg / invoice; inline compenser + contrepasser actions |
| `/fideicommis/<id>/compenser` · `/compenser-lot` | POST | Single / bulk clear (all-or-nothing) |
| `/fideicommis/<id>/contrepasser` | GET · POST | Reversal confirmation (mandatory motif) · submit |
| `/fideicommis/virement` | GET · POST | Inter-dossier transfer (two `compensée` legs, one account) |
| `/fideicommis/carte/<dossier_id>/<client_id>` | GET | **Carte-client** — chronological, « Solde aux livres » vs « Disponible (compensé) » |
| `/fideicommis/client/<client_id>` | GET | Consolidated « Vue de gestion » across dossiers — **not a register**, no control, no export |
| `/fideicommis/comptes/` · `/nouveau` · `/<id>` · `/<id>/edit` | GET/POST | Account list / create / detail / edit (metadata only — balances never editable) |
| `/fideicommis/conciliations/` · `/nouvelle` · `/<id>` · `/<id>/completer` | GET/POST | Reconciliation list / start (refuses a FUTURE period_end and a blank statement amount — literal 0 stays legitimate) / worksheet (live variance **as of period_end**) / complete (refuses variance ≠ 0) |
| `/fideicommis/conciliations/<id>/abandonner` | POST | Delete a DRAFT reconciliation (brouillon only — a complétée is the audit trail); the escape hatch that unblocks the one-brouillon guard without console surgery |
| `/fideicommis/export/pdf` | GET | **« Journal de caisse des recettes et déboursés »** (August 2026) — the register **art. 38 RLRQ c. B-1, r. 5** requires, on **legal paper, landscape**, centred title block (title · account name + institution + ••••last4 · the article cited · the period). Its 10 columns are that article's own list: Date · Client · N/Réf · Somme reçue de / Bénéficiaire · Objet · Mode · N° de chèque · Recette · Débours · Solde (« Mode » carries both 2°g *mode de retrait* and the 1°g *espèces* indication; « N° de chèque » reads the entry's `reference`). Opens on a **SOLDE REPORTÉ** row and closes on a totals row, so *report + Σ recettes − Σ déboursés = solde de clôture* verifies. **Deliberately ignores the statut/sens filters** — a book of account is complete, and only a complete one reconciles; the period IS honoured. Built by `utils/trust_journal_pdf.py`, never `utils/export_pdf.py` |
| `/fideicommis/export/csv` · `/carte/<did>/<cid>/export/<csv\|pdf>` | GET | Journal CSV + carte-client (both formats) — the 9 `BARREAU_COLUMNS`, « Recette »/« Crédit » split, `en_circulation` marked `*`, via `to_barreau_row` |
| `/fideicommis/{dossier,client,counterparty}-search` | GET | HTMX autocompletes (client-search scoped to one dossier; counterparty suggests parties as **text**) |

### `budgets.py` — `/budgets/*` (August 2026 — budgets par phase)

All `@login_required`, French UI, standard CSRF, POST+redirect with inline error boxes. The dossier context travels as `?dossier_id=` (the documents/trust pattern); the hub leaf is `/dossiers/<id>/tab/budget`.

| Route | Method | Purpose |
|---|---|---|
| `/budgets/nouveau?dossier_id=` | GET | Versioned form — seed: the 9 tronc phases with their full sub-code grid at 0 (zero rows dropped at save); editing seeds from the latest version; modules addable (« Procédures spéciales ») |
| `/budgets/` | POST | Mint a NEW version (never overwrite) → redirect to the Budget tab |
| `/budgets/historique?dossier_id=` | GET | Version history (totals per version + per-version Estimation PDF) |
| `/budgets/<id>/export/<variante>` | GET | PDF — `estimation` (portrait, client document, no actuals) or `suivi` (landscape, budget vs actuals + écarts); firm footer from `cabinet_dict()` |

### `admin_ledger.py` — `/administration/*` (August 2026 — comptabilité d'administration)

All `@login_required`, French UI, standard CSRF, POST+redirect with inline error boxes + 400. The receipt API endpoints exchange small JSON control messages (bytes go browser→GCS). Was the first accounting module with a nav entry; **since 2026-08-15 the nav entry is « Comptabilité » → the `/comptabilite` hub** (see `comptabilite.py` below), which links here.

| Route | Method | Purpose |
|---|---|---|
| `/administration/` | GET | **Journal** per account, `(date, sequence)` order, filters (compte/type/statut/catégorie/période) via HTMX `#admin-rows` + OOB `#admin-export` re-swap. Full-period fetch (`list_register`, `truncated` surfaced). **The Solde column renders ONLY without content filters** — a running balance over a filtered subset is a false figure, so it is hidden rather than lied about; a `date_from` shows the « Solde reporté » row |
| `/administration/nouvelle` · `/` | GET · POST | Entry form · create. The Sens is IMPLIED by the kind (no Sens select); « Déjà compensée » = route-composed create-then-clear; an `encaissement_facture` then projects the payment onto the invoice (`record_payment(current + delta)` — failure leaves the ENTRY standing + `?avertissement=facture` banner) |
| `/administration/ventilation` | GET | HTMX partial: `extract_taxes_from_gross` prefills the three ventilation fields (ONE Python implementation, no JS rounding) — fields stay editable |
| `/administration/<tx_id>` | GET | Detail: facts + ventilation + revisions + linked invoice with LIVE balance + receipt card + lock state |
| `/administration/<tx_id>/modifier` | GET·POST | Edit an UNLOCKED entry (account immutable, kind within recette_autre/dépense, invoice linkage create-only); lock re-verified in the model transaction |
| `/administration/<tx_id>/supprimer` | POST | Delete an unlocked entry (a `paiement_carte` leg deletes BOTH legs); route records `audit_events` after the committed delete |
| `/administration/<tx_id>/compenser` | POST | Single clear (the worksheet is the bulk path — no `/compenser-lot` route by design) |
| `/administration/<tx_id>/contrepasser` | GET·POST | Reversal with a CHOOSABLE date ∈ [date originale, aujourd'hui], above the lock floor; a card-payment leg reverses both; an invoice-linked entry reduces the recorded payment |
| `/administration/paiement-carte` | GET·POST | One economic event, two linked legs (bank déboursé + card recette) |
| `/administration/api/televersement` · `/<tx_id>/api/recu` · `/<tx_id>/recu` | POST·POST·GET | Receipts: resumable GCS session on the RECEIPT whitelist (PDF/JPG/PNG/TIFF ≤ 10 Mo — unambiguous magics), sniff + rewrite to `users/{uid}/administration/{tx_id}/`, staging consumed both outcomes, replace deletes the old blob; serving = 302 to a signed URL, attachment forced |
| `/administration/dossier-search` | GET | HTMX autocomplete (optional dossier linkage) |
| `/administration/comptes/…` | GET/POST | Account list/create/detail/edit (type immutable; balances never form-editable) |
| `/administration/conciliations/…` | GET/POST | List / new (card statement entered as stated — positive solde dû) / worksheet (live Alpine variance on `statement_ledger`, resurrection sets read-only) / completer (**locks the period**) / abandonner (draft only) |
| `/administration/export/<fmt>` | GET | CSV (14 columns incl. Net/TPS/TVQ; Solde BLANK when filtered) · PDF legal landscape via `utils/admin_journal_pdf.py` (ignores content filters — a book of account is complete; honours the period; Σ TPS/TVQ closing line) |

### `comptabilite.py` — `/comptabilite/` (August 2026 — hub « Comptabilité »)

ONE route, `GET /comptabilite/` (`@login_required`) — the unified account listing the nav's « Comptabilité » entry opens (2026-08-15 consolidation, user decisions: entry→hub page, two sections, trust parity retrofits included). A **presentation-only composer**: it reads the two EXISTING firm snapshots (`trust.get_firm_trust_snapshot()` — also under the MCP `get_trust_snapshot` outputSchema contract, so its shape is read and never altered — and `al.get_firm_admin_snapshot()`) and routes every action (Journal / Détail / Concilier `?account_id=` / Nouveau compte) to the modules' own screens. Doctrine, pinned by `tests/test_comptabilite.py` + the `test_comptabilite_parity.py` watchdog: **READ-ONLY forever** (no POST route, no model write verb — a write path here would bypass the modules' guards); **fail-closed PER SECTION** (a snapshot read failure renders an « indisponibles » panel with NO creation CTA — an empty state on an outage would invite a duplicate account — while the other section renders normally; never a page-level 500); **two sections, never a merged list** (the balance label travels PER ROW — « Solde aux livres » / « Solde » / « Solde dû » sign-flipped for a card — and **no combined total, ever**: summing client funds with firm cash is a legally misleading figure, pinned ABSENT); zero hardcoded module path, zero HTMX/script/arrow-function, zero new Firestore query, zero new Tailwind class. `/fideicommis/*` and `/administration/*` stay canonical and unchanged — the hub added a nav entry the trust module never had.

| Route | Method | Purpose |
|---|---|---|
| `/comptabilite/` | GET | The hub: both account lists with per-row typed badges (type, Fermé, Conciliation en retard / Jamais concilié), per-row labelled balance, actions into each module's existing screens |

### `reception.py` — `/reception/*` (portail client L1)

All `@login_required`, French. POST+redirect with `?message=`/`?erreur=` (no flash). Fail-open display: a missing « portail » database or bucket renders empty states + a warning, never a 500.

| Route | Method | Purpose |
|---|---|---|
| `/reception/` | GET | Tabs `?onglet=documents|rdv|ouvertures` (**rdv = L2**, **ouvertures = L3**). Documents: submitted lots with the manifest table (nom d'origine, taille, type, SHA-512 abrégé + complet au survol, divergences, IP/UA), active invitations (renvoyer/révoquer), recent history. **rdv** (`_contexte_rdv` → `_rdv.html`): à_confirmer + annulée_client cards + unseen-divergence alerts, partie exact-email linkage. **ouvertures** (`_contexte_ouvertures` → `_ouvertures.html`): la DERNIÈRE enveloppe intake de chaque invitation `soumise`, la vue côte à côte pré-calculée (`_comparaison`) et les candidats de conflit (`_candidats_adverses`) |
| `/reception/inviter` | GET·POST | Émission (courriel prérempli depuis la partie cliente, désignation défaut `Dossier {n°}`, durée) ; sans Graph → page « lien à transmettre ». **L3** : sélecteur `type` documents/intake — c'était le SEUL endroit où le type était écrit en dur ; `?type=intake&partie_id=` (déclencheur (b)) préremplit le contact et joint l'instantané `prefill_depuis_partie` |
| `/reception/invitations/<id>/renvoyer` · `/revoquer` | POST | Renvoi du lien / révocation instantanée |
| `/reception/rdv/<hid>/confirmer` | POST | **L2** — `confirmation → ""` (+ `partie_id` si coché) → `bump_ctag(collection_for(dossier_id))` : le rendez-vous entre au Calendrier + DavX5. **L3** : la case `intake` (cochée par défaut, seulement si AUCUNE partie reconnue, derrière `FEATURE_INTAKE`) émet le formulaire d'ouverture — une panne n'annule jamais la confirmation déjà commise, elle produit un bandeau |
| `/reception/rdv/<hid>/refuser` | POST | **L2** — annule la réunion Outlook via Graph (best-effort, seulement pour un à_confirmer actif) + `confirmation → "refusée"` ; **pas de bump** (jamais en DAV). Bandeau si l'annulation Graph échoue |
| `/reception/rdv/<hid>/divergence/<action>` | POST | **L2** — `appliquer` (applique le créneau stocké + bump) / `annuler` (`annulée_client` + bump) / `ignorer`·`conserver` (`vu=True`, pas de bump) |
| `/reception/lots/<inv>/<batch>/fichiers/<seq>` | GET | Redirection vers un URL signé V4 (15 min) — `Content-Disposition: attachment` FORCÉ, content_type déclaré (§7.5, jamais inline) ; les octets ne transitent JAMAIS par l'app (plafond App Engine de 32 Mo par réponse — incident 2026-08-12) |
| `.../fichiers/<seq>/verser` | POST | Ingestion via `get_or_create_folder(« Reçus du portail »)` + **copie côté serveur** (`ingest_blob_as_document`, 2026-08-12 — les octets ne transitent jamais par l'app) — restreinte au vocabulaire documents (11 types, ≤ 200 Mo) ; garde de fraîcheur (`blob.reload()` + refus > 200 Mo AVANT toute lecture) + revérification SHA-512 **en flux** contre le manifeste (divergence → refus + `versement_divergence` ERROR) ; provenance en `description` + tag `portail` |
| `.../fichiers/<seq>/refuser` · `.../traiter` | POST | Refus explicite / lot traité (chaque fichier « reçu » exige une décision AVANT la purge ; enveloppe+manifeste → `archive/`) |
| `/reception/ouvertures/<inv>/<batch>/creer` | POST | **L3** — crée la partie (`contact_role="client"`, Conformité INTACTE) + les contacts adverses cochés → **`bump_ctag("parties")`** (un seul) → `traitée` + archivage → redirection vers la fiche avec `?message=` |
| `.../appliquer` | POST | **L3** — mise à jour partielle des SEULS champs cochés (un champ soumis vide n'efface jamais) + adverses → bump si écriture → `traitée` + archivage |
| `.../refuser` | POST | **L3** — `refusée` + archivage ; **aucun courriel** au client (D-L3-3) |
| `/reception/invitations/<inv>/archive` | GET | Modale HTMX du détail conservé. Branche sur le **type** : documents → fichiers + SHA-512 + décision ; **intake** → récapitulatif des réponses, parties adverses déclarées, consentement (version + horodatage), IP/UA — une section par version transmise. Lit `archive/` d'abord, `submissions/` en repli |

### `taches_portail.py` — `/taches/portail/*` (MACHINE, portail L1)

CSRF-exempt blueprint; no `@login_required`. Origin proof = the `X-AppEngine-*` headers (stripped from ALL external traffic).

| Route | Method | Guard | Purpose |
|---|---|---|---|
| `/taches/portail/evenement` | POST | `X-AppEngine-QueueName == "portail"` sinon 403 | Handler §8.3: `ouverte` / `renvoi` / `soumise` (rapprochement + SHA-512 en flux + manifeste + accusé transactionnel). **L3** : `soumise` aiguille sur le TYPE de l'invitation — jamais sur l'enveloppe, dont la lecture vit dans le court-circuit d'idempotence — vers `_traiter_intake` (aucune empreinte ; enveloppe illisible → les deux marqueurs posés quand même, sinon réconciliation en boucle). Exception → 5xx (reprise) ; no-op/malformé → 200 |
| `/taches/portail/reconciliation` | GET | `X-Appengine-Cron == "true"` sinon 403 | §8.4: toute enveloppe sans soumission/accusé enregistrés → ré-enfilée + ERROR `reconciliation_reparation` |

### `taches_bookings.py` — `/taches/bookings/*` (MACHINE, Bookings L2)

CSRF-exempt blueprint; no `@login_required`. Same origin proof as `taches_portail` (`X-Appengine-*` stripped from external traffic).

| Route | Method | Guard | Purpose |
|---|---|---|---|
| `/taches/bookings/sync` | GET | `X-Appengine-Cron == "true"` sinon 403 | §4 — lit `calendarView`, rapproche par `graph_ical_uid` (upsert : nouveau → à_confirmer **sans bump CTag** ; modifié+à_confirmer → maj silencieuse ; modifié/annulé + confirmé → divergence sans écraser). Court-circuit si `not BOOKINGS_SYNC_ACTIVE` / `not bookings_configured()` ; GraphError → 200 (pas de tempête de reprise). **La recherche de ses propres imports passe `include_unconfirmed=True`** — sinon un doublon serait recréé à chaque cycle |

### `taches_outlook.py` — `/taches/outlook/*` (MACHINE, miroir Outlook)

CSRF-exempt blueprint; no `@login_required`. Same origin proof (`X-Appengine-Cron`).

| Route | Method | Guard | Purpose |
|---|---|---|---|
| `/taches/outlook/sync` | GET | `X-Appengine-Cron == "true"` sinon 403 | Miroir unidirectionnel (cron 10 min) : les audiences **confirmées** (`confirmation == ""`, `status != "annulée"`, **jamais `source == "bookings"`** — elles SONT déjà des événements Outlook) sont réconciliées par diff (POST/PATCH/DELETE) dans le calendrier PRINCIPAL du juriste, fenêtre UNIQUE [-30 j, +365 j] partagée Athéna/Outlook. Athéna écrase les éditions Outlook (comparaison etag stampé + champs visibles). **Firestore-lecture-seule** — le mappage vit dans la propriété étendue de l'événement. `fenetre_pleine` (≥ 500 audiences) désarme les suppressions + ERROR. Kill switch `MIROIR_OUTLOOK_ACTIF` (gèle les miroirs en place) ; GraphError → 200 |

### Portal service routes (`client/routes.py`, service « portail » — separate process)

Public host `portail.poirierlavoie.ca`; guard §6.5 re-reads the invitation each request (instant revocation) and checks the **required type per endpoint** (`_TYPE_REQUIS`; an endpoint absent from the table is refused). `/entree` (email-link landing + renvoi form, anti-enumeration; reuses a live session ONLY for its own `?i=`), `POST /session` (verify_id_token WITHOUT check_revoked; requires `portail: True` + `email_verified` + email match; 10/min; `suivant` routed by type), `POST /api/renvoi` (5/h — always the identical response), `/confirmation` (both flows, type-aware), `/sante`.

**Documents flow:** `GET /documents`, `POST /api/televersement` (whitelist/quotas → GCS resumable session with `origin=`+`size=`), `POST /api/finaliser` (envelope create-only; a 409 means the lot is already acquired → purge the session and answer SUCCESS).

**Ouverture flow (L3):** `GET /ouverture` (4-step wizard, vanilla JS — no Alpine, the CSP has no `'unsafe-eval'`), `POST /api/intake/etape` (whitelist + per-field bounds + hard byte guard, merged into `session["intake"]`), `POST /api/intake/finaliser` (mints its own batch — intake never goes through `api_televersement`, the only other batch minter — writes a file-less `type: "intake"` envelope, then `signaler("soumise")`). Re-entry allowed until the lawyer marks the ouverture traitée.

### Top-level miscellaneous routes (defined in `main.py`)

| Route | Purpose |
|-------|---------|
| `/offline` | Service-worker offline fallback page |
| `/.well-known/assetlinks.json` | Android TWA Digital Asset Links (SHA-256 fingerprint of the signing key) |
| `/manifest.json` | PWA manifest (served as static file) |
| `/sw.js` | Service worker (served as static file with `Service-Worker-Allowed: /`) |
| `/privacy`, `/terms` | Static legal pages |
| `/csp-report` | POST, CSRF-exempt — receives browser CSP violation reports (the `report-uri` of the enforced CSP); logs a `csp_violation` security event, returns 204 |
| `/_ah/warmup` | App Engine warmup (`inbound_services: warmup`) — primes the Firestore channel before an instance takes live traffic; exempt from the appspot block and origin-secret check |

---

## Model Layer Reference

Every model exports the standard CRUD set. Module-specific additions:

### Pagination (two modes — `pagination.py`)

- **Cursor mode (preferred for list views):** model functions named `list_X_page(...,
  limit=PAGE_SIZE, cursor=None) -> (rows, next_cursor)` push `order_by(primary,
  "id").limit(limit+1).start_after(...)` into Firestore (~15 reads/page regardless of
  collection size). Routes thread the opaque `cursor` + a bounded `trail` of prior
  cursors (for « Précédent ») through hx-vals; `components/pagination.html` renders both
  modes. Every filter+order combo needs a composite index in `firestore.indexes.json`
  (the `id` field is the tiebreaker — its direction must match the index).
  Implemented for: timeentries, expenses, parties, dossiers, invoices.
- **Legacy page mode:** `paginate(items, page)` slices a fully materialized list. Kept
  for search paths (Python full-text filter), rare filter combos that would each need
  their own composite index, and dossier-scoped deep links (already server-narrowed).
- **Bounded-group mode (no pagination UI):** views whose UX is not paginated cap reads
  server-side instead — tasks (per-status groups, 100 each; since 2026-07-23 the default
  list view fetches only the active groups — terminée/annulée are fetched and shown,
  expanded, solely under their status filter, the collapsed disclosures being gone),
  notes (pinned + 100 recent), hearings (a 100-doc upcoming window + month-grid range;
  the past window is fetched and shown only under an active type/status filter —
  same 2026-07-23 decision, « Passées » disclosure removed). Cap hits log a warning.
- Validate filter values against the model's `VALID_*` vocabulary in routes before
  choosing a path, so junk query strings cannot force an unbounded fallback scan.

### `models/partie.py`
- `display_name(partie) -> str` — returns `organization_name` (legal name) for personnes morales; trade name (`trade_name`) is surfaced separately in the UI
- `update_kyc_status(partie_id, field, status, notes)` — `field ∈ {"identity_verified", "conflict_check"}`, auto-stamps the corresponding `_date`
- `link_kyc_document(partie_id, document_id)` — appends to `kyc_document_ids`
- `get_parties_bulk(ids) -> {id: doc}` (August 2026) — one `db.get_all` round-trip, no index; mirrors `dossier.get_dossiers_bulk` and **fails open to `{}`**. Written for the MCP coverage report's two deontological checks: the alternative (`list_parties(role_filter="client")`) **silently under-reports**, because `contact_role` belongs to the CONTACT, not to the dossier link, so a client recorded under another role vanishes from a regulatory check.
- `MANDATAIRE_KIND_LABELS` — French display labels for the `kind` field on each mandataires entry (mandataire, tuteur, curateur, représentant_légal, autre)
- `_migrate_mandataires(partie)` — translates legacy single-mandataire fields into the new `mandataires` list on read; pops the legacy keys so the next `set()` purges them from storage. Called from `get_partie`.
- `mandataires` constraint enforcement in `_validate`: each entry's `id` must reference an existing partie that is `type=="individual"`, shares the parent's `contact_role`, and is not the parent itself. `_normalize` deduplicates by id, drops empty entries, and removes any legacy `mandataire_id`/`mandataire_kind`/`mandataire_notes` keys before save.
- `delete_partie` enforces the FK safety check (fails CLOSED): refuses while the partie is referenced by any dossier (`count_dossiers_for_partie_strict`) or listed as a mandataire by another partie — applies to UI and CardDAV DELETE alike
- `partie_to_vcard(partie) -> str` — vCard 4.0 with LANG, GENDER, X-PRONOUN, TITLE, ROLE, ORG, dual ADR/TEL/EMAIL, CATEGORIES (contact_role), NOTE, UID, REV. (Mandataires list, trade name, and governing law are not yet serialized to vCard.)
- `vcard_to_partie(vcard_str) -> dict` — inverse parser; normalizes incoming phones via `normalize_phone`

### `models/dossier.py`
- `suggest_file_number() -> str` — public wrapper around `_suggest_next_file_number`, "YYYY-NNN" sequential
- `count_open() -> int` — COUNT aggregation over `status in (actif, en_attente)` (dashboard stat)
- `list_prescription_alerts(cutoff, limit=50) -> list[dict]` — server-side `status==actif AND prescription_date<=cutoff`, ordered + bounded; logs a warning when the window fills, on the RAW count (needs the `dossiers` composite index). Each row is then re-read through `derive_prescription` **in Python**: an interrupted dossier (depot event or legacy `prise_action_date`) is dropped, and the surviving rows carry `prescription_status`/`prescription_date_effective` beside the raw date — see the Known Gotcha; this one seam serves the dashboard and the MCP `get_agenda` alike
- `derive_prescription(doc) -> {status, date_effective}` — the ONE derivation seam over `prescription_events` (+ the legacy `prise_action_date` folded as an implicit depot at read); never writes, never touches the raw `prescription_date`. Consumed by the alerts above, `_attach_prescription_warnings`, and the MCP `get_dossier`/`record_prescription_event`
- `count_dossiers_for_partie(partie_id) -> int` (returns 0 on query failure — display only), `count_dossiers_for_partie_strict(partie_id) -> int` (propagates errors — used by FK safety checks), `list_dossiers_for_partie(partie_id) -> list[dict]` — query `client_ids`, `opposing_party_ids` **and `avocat_ids`** (a contact linked as a party's lawyer counts as referenced: it blocks deletion, and `get_partie` reports the dossier with `relation: "avocat"`)
- `delete_dossier` REFUSES deletion while child records exist (documents, time entries, expenses, invoices, hearings, tasks, notes, protocols, folders, **trust transactions**) and fails CLOSED when the child check errors — archive instead of deleting. **A dossier that has EVER had a trust entry can never be deleted** even at a zero balance (the register is permanent; `trust_transactions` rows are never hard-deleted, so the `count>0` check enforces "ever existed")
- `dossier_to_vjournal(dossier) -> str`, `vjournal_to_dossier(ical_str) -> dict` — legacy, retained for potential export (not used by DAV post-D1). CATEGORIES now emits the domaine label + the action label; an unknown key resolves to nothing rather than leaking a raw snake_case key as a French category (the old `matter_type` line did).
- **Forum (July 2026, four-way since late July):** `VALID_FORUM_TYPES = ("judiciaire", "administratif", "federal", "prejudiciaire")` + `FORUM_TYPE_LABELS` (« Tribunal de droit commun » / « Tribunal administratif » / « Cour ou tribunal fédéral » / « Préjudiciaire »). `normalize_forum(data)` reconciles the forum fields server-side (authoritative over the JS state), called by the route in `_form_data` before validation. `administratif`/`federal` → the picked `forum` slug's name becomes `tribunal`, the Québec judicial-court fields (greffe/juridiction/district/palais/competence) are cleared, and `is_administrative_tribunal` is True only for `administratif`; a blank/unknown/**cross-category** slug leaves the data untouched (`_validate` rejects it). `prejudiciaire` → everything judicial is cleared EXCEPT the user-entered `district_judiciaire`, and `court_file_number` is forced to `PREJUDICIAIRE_FILE_NUMBER` (« Préjudiciaire ») so gabarits can cite it until the parser crushes it. `judiciaire` → `forum` cleared, parsed metadata stands. `_validate` presence-gates `forum_type` (legacy dossiers default `"judiciaire"` on read); the retired `"autre"` is no longer writable — `_migrate_forum_type` (in the `_migrate_parties` chokepoint) splits stored `"autre"` docs by their slug's category on read (dangling slug → `judiciaire`, forum cleared, tribunal text kept). It lives in the model, not the route, so it is testable without Flask config.
- **Taxonomy (July 2026):** `VALID_DOMAINES` / `VALID_ACTIONS` / `DOMAINE_LABELS` are re-exported from `utils/taxonomie.py` — the vocabulary is **not** redefined here (contrast `MANDATE_TYPE_LABELS` / `FEE_TYPE_LABELS`, which `utils/template_fields.py` must mirror by hand; there is deliberately **no domaine mirror**, since `taxonomie` is Firestore-free and both sides import it). `_migrate_domaine` (called from `_migrate_parties`, the chokepoint covering all six read paths) folds legacy `matter_type` → `domaine` and `objet` → `action_precision`, then `_REMOVED_FIELDS` purges both on the next save. `_validate` presence-gates `domaine`/`action` (like `mandate_type`) and rejects a pair whose code prefix disagrees with the domaine — the cascading picker cannot produce that, but a hand-crafted POST can.

### `models/time_entry.py` (note: file is `time_entry.py`, **not** `timeentry.py`)
- `get_time_summary(dossier_id) -> dict`
- `get_unbilled_time_entries(dossier_id) -> list[dict]`
- `get_unbilled_totals() -> {"hours": float, "amount": int}` — single server-side aggregation (SUM×2 over `billable==True AND invoiced==False`); used by the dashboard instead of streaming the collection. Needs the `timeentries` composite indexes in `firestore.indexes.json`.
- `set_time_entry_phase(entry_id, phase, sous_phase) -> (doc, errors, changed)` (August 2026) — the ONE writer allowed past the `invoiced` wall, and the only field it can reach is the Phase-O pair. It writes a **partial `update()` of exactly four keys** (`phase`, `sous_phase`, `updated_at`, `etag`), never the merged full-document `set()` that `update_time_entry` performs — that shape, not a promise, is what makes it incapable of moving hours/rate/amount/description, and a test pins the key set. Validates with `phases.validate_pair` ALONE (the module's `_validate` would lock out a legacy row with a blank description); a blank phase is refused (« Hors phase » HOR is the vocabulary's answer for unclassifiable work); **an unchanged pair writes nothing at all** — no `updated_at` churn — which is what makes a reclassification pass replayable. The third return member is a documented deviation from the `(doc, errors)` convention (the `delete_folder` precedent): the bulk MCP caller must report « applied » apart from « already classified », and deriving that in two callers is how two callers drift
- `get_time_entries_bulk(ids) -> {id: doc}` (August 2026) — one `db.get_all`, no index; mirrors `dossier.get_dossiers_bulk` but **fails CLOSED (it propagates)**, unlike `partie.get_parties_bulk`. Its caller is a write path: degraded to `{}` it would report every row « introuvable » and hand back a batch of fabricated refusals (the `subtree_members` vs `list_documents` distinction)
- `mark_time_entries_invoiced(entry_ids, invoice_id) -> list[str]` — returns the IDs that failed to flip (no silent swallowing); no longer called by `create_invoice` (flips happen inside its transaction)

### `models/expense.py`
- `get_expense_summary(dossier_id) -> dict`
- `get_unbilled_expenses(dossier_id) -> list[dict]`
- `set_expense_phase(expense_id, phase, sous_phase) -> (doc, errors, changed)` / `get_expenses_bulk(ids)` (August 2026) — the twins of the time-entry pair above, same four-key write and same fail-closed read. Both halves matter: `budget.aggregate_actuals` buckets time entries AND disbursements by sub-code, so a phase's « réalisé » is wrong while either side is unclassified
- `mark_expenses_invoiced(expense_ids, invoice_id) -> list[str]` — returns failed IDs, mirroring `mark_time_entries_invoiced`

### `models/invoice.py`
- `compute_totals(line_items: list[dict]) -> dict` — pure helper computing subtotal_fees / subtotal_expenses / subtotal / GST (5%) / QST (9.975%, **not compounded on GST**) / total. Uses `Decimal` with `ROUND_HALF_UP`.
- `create_invoice(dossier_id, time_entry_ids, expense_ids, data)` — fully transactional: sources that are missing, already invoiced, or belonging to another dossier are skipped; the invoice doc, line items, and `invoiced=True` flips for ONLY the retained sources commit in a single Firestore transaction that re-reads each source (etag compared) and aborts on concurrent change. `retainer_applied` is validated to `[0, total]`.
- `get_invoice_with_items(invoice_id) -> tuple[Optional[dict], list[dict]]`
- `update_status(invoice_id, new_status)` — enforces the transitions **`available_transitions(invoice)`** allows, never `STATUS_TRANSITIONS` directly (see the gotcha: the table is keyed on status alone and cannot tell a hand-set `payée` from a ledger-backed one)
- `available_transitions(invoice) -> tuple[str, ...]` (2026-08-17) — the ONE authority, read by `update_status` **and** by the detail route so a button never renders only to be refused. Returns the table's row, minus `envoyée` when the invoice is `payée` **with a recorded payment**: such an invoice corrects by contre-passation (which reduces the payment and reopens it by itself), never by hand
- `void_invoice(invoice_id)` — all-or-nothing: source releases + status flip to `annulée` commit in one `db.batch()`; any failure aborts without changing status
- `delete_invoice(invoice_id)` — only allowed on `annulée`; refuses if any time entry/expense still references the invoice
- `get_invoice_summary(dossier_id) -> dict`
- `get_outstanding_total() -> int` — SUM(`amount_due`) aggregation over `status in (envoyée, en_retard)` (dashboard stat; needs the `invoices` composite index)
- `list_line_items(invoice_id) -> list[dict]` (August 2026) — the subcollection alone, without the extra document read `get_invoice_with_items` costs; fails open to `[]`. Feeds the fee journal, which already holds every invoice document.
- `expense_split(invoice, line_items) -> (taxable, non_taxable)` (August 2026, pure) — the Barreau journal needs the disbursement split, which **the invoice document does not store** (only `subtotal_expenses`). So the STORED total stays authoritative and the items only carve out the non-taxable part: the two columns always add back to `subtotal_expenses`, and a row whose items are missing still ties (everything under taxable — the `taxable: True` default the tax was computed under) instead of silently under-reporting the sheet's own subtotal. Fees are excluded: `create_invoice` always writes them `taxable: True`, so they are the journal's « Honoraires » column whole.
- `balance_of(invoice) -> int` (August 2026) — the LIVE balance in cents, `amount_due − amount_paid`, **derived and never stored**. Note the trap it replaces: **`amount_due` is frozen at issuance and stays non-zero on a fully paid invoice**, so it has never been a balance despite reading like one.
- `record_payment(invoice_id, amount_paid, paid_date=None)` (August 2026) — the only writer of `amount_paid`/`paid_date`, and since **2026-08-17** its only request-served caller is `routes/admin_ledger` (pinned by a source sweep in `tests/test_invoice_detail.py`): the invoice's own payment form was removed, so **the accounting module is the single writer of a payment**. Transactional; owns BOTH fields **and** the status flip: a zero balance flips the invoice to `payée`, and a CORRECTION that reopens a balance **undoes that flip**. The undo is deliberately narrow (status `payée` **with** a recorded payment that no longer covers the invoice) — a `payée` set by hand is the lawyer's statement and is never touched. Without it an erroneous amount would strand the invoice for ever — `payée` stopped being terminal on 2026-08-17, but `available_transitions` closes the manual exit the moment a payment is RECORDED, so for the ONE status this function can set the undo is still the only way back. The caller that needs it is `_reduire_paiement` (a reversed encaissement); `scripts/purge_encaissements_factures.py` and `scripts/reprise_encaissements.py` lean on it to make pre-module payments re-enterable. **Caps on `amount_due`, not `total`**: with a retainer applied `amount_due < total`, and capping on the total let a payment land between the two and produce a NEGATIVE balance with nothing to explain it.
- Invoice numbers are **year-sequential**: `"YYYY-F###"` (3-digit-padded, rolling to 4+ past 999; e.g. `2026-F031`) — the canonical scheme AGAIN since the 2026-08-12 user decision, reverting the per-file `"{file_number}-NN"` scheme of 2026-07-17 after four weeks. The **year is the MONTRÉAL calendar year** (`today_mtl` — no longer the UTC year, which stamped a Dec 31 evening invoice with the next millésime). Allocated by `_generate_invoice_number()` from the transactional counter `counters/invoices-{year}` (`seq`), seeded on a year's first use by `_scan_max_invoice_seq` (full-collection scan on the `"YYYY-F"` prefix — per-file numbers never match it and are ignored); monotonic, never reused — the production counter stood at seq=30 at the revert, so numbering resumed at `2026-F031`, no reseeding. **Existing invoices keep whatever number they were issued** — never renumbered (immutable accounting artifact); the SIX per-file invoices of the parenthesis stay as issued, and the orphaned `counters/invoice-{dossier_id}` docs sit inert in Firestore. Allocation failure aborts invoice creation — no guessed fallback number.

### `models/hearing.py`
- `get_hearing_summary(dossier_id) -> dict`, `get_upcoming_hearings(days=30) -> list[dict]`
- `list_hearings_in_range(date_from, date_to, limit=100) -> list[dict]` — server-side range + order on `start_datetime` (single-field index); the dashboard's bounded hearing windows
- `forum_of(hearing_type) -> str` — « judiciaire »/« extrajudiciaire », derived from the type (default extrajudiciaire); the forum is never stored
- `is_safe_conference_uri(uri) -> bool` — http/https scheme whitelist (empty allowed); called by `_validate` (form → error) AND `vevent_to_hearing` (DAV → drop the bad value). The only guard on the `<a href>` render (stored-XSS)
- `_migrate_hearing(doc)` — read-time net on every raw-read path (get/list/window/range): folds removed hearing_type keys (procès→instruction, appel→audience, médiation→autre), defaults `modalite`/`conference_uri`, and (L2) defaults the Bookings fields incl. `confirmation=""`/`source=""` (get_hearing returns `to_dict()` with no `_default_doc` merge; legacy docs are NEVER back-filled to a non-empty confirmation)
- **`list_hearings` / `list_hearings_in_range` / `list_hearings_window` take `include_unconfirmed=False` (L2)** — `_filter_confirmation` drops unconfirmed Bookings imports by default (only confirmed reach DAV/MCP/dashboard); `True` keeps confirmed + à_confirmer + annulée_client but always drops `refusée`. `get_hearing` does NOT filter. `_UNCONFIRMED_ALL`/`_UNCONFIRMED_REFUSED` are the two drop sets.
- **Séries récurrentes (août 2026)** — `create_hearing_series(data, frequency, *, count=None, until=None) -> (occurrences, errors)` matérialise N audiences ordinaires partageant un `serie_id`, écrites en **UN SEUL `db.batch()` incluant le bump de CTag** (voir la dérogation documentée à la section DAV). Elle **retire** tout `id`/`vevent_uid` fourni avant d'étendre : `create_hearing` les honore (l'affordance CalDAV), donc les laisser passer ferait N `batch.set()` sur LA MÊME référence — Firestore garde le dernier, en silence, et N−1 occurrences disparaissent avec un retour de succès. `list_series(serie_id)` **PROPAGE** une erreur de lecture, contrairement à `list_hearings` qui rend `[]` : un dialogue destructeur ne doit jamais sous-estimer ce qu'il détruira (doctrine `subtree_members` contre `list_folders`). `delete_series(serie_id, *, from_date=None)` met les N suppressions, les N pierres tombales et le bump dans un seul lot (`2N+1` opérations, atomique au plafond de 60). `unlink_hearing(hearing_id)` détache. `occurrence_day(hearing)` rend le jour civil dans le BON référentiel — la date UTC pour un all-day (minuit UTC par convention), la date de **Montréal** pour une audience horodatée, faute de quoi un rendez-vous de 21 h serait rattaché au lendemain.
- `list_hearings_in_range_state(...) -> HearingWindow(rows, window_full, ok)` — la variante que doit employer tout appelant qui **SUPPRIME sur la foi d'une absence** (le miroir Outlook). `window_full` est mesuré sur la fenêtre **BRUTE**, avant que `_filter_confirmation` ne la rétrécisse ; `ok` distingue « rien ne correspond » de « la requête a échoué ». `list_hearings_in_range` reste l'enveloppe simple pour les lecteurs d'affichage.
- `hearing_to_vevent(hearing) -> str` — VEVENT with VALARM (TRIGGER -PT{reminder_minutes}M); emits `CONFERENCE;VALUE=URI;FEATURE=VIDEO` only for a video event with a link (icalendar 7.0.3 serializes it raw), `X-PALLAS-MODALITE`, and a « Modalité: … » DESCRIPTION line — never a second `CATEGORIES` value
- `vevent_to_hearing(ical_str) -> dict` — **NON-EFFACEMENT (§4.3):** OMITS `conference_uri`/`modalite` keys when the property is absent from the incoming VEVENT (never `""`), so a client that drops them on a plain edit can't wipe the stored link (`update_hearing` merges `{**existing, **data}`); an incoming URI re-runs the scheme whitelist
- `create_hearing`/`update_hearing`/`_validate` treat `dossier_id` as **optional** — a hearing may be a standalone agenda event with no dossier (like standalone tasks); `_validate` requires only a title + start datetime. All hearings, linked or standalone, live in the single shared `hearings` / `/dav/calendar/` collection, so standalone events sync to DavX5 with no extra DAV routing (contrast tasks, which split per-dossier). `hearing_to_vevent` omits the `Dossier:` DESCRIPTION line when there is no dossier.

### `models/task.py`
- `list_urgent_tasks(cutoff, limit=50) -> list[dict]` — server-side `status in (à_faire, en_cours) AND due_date <= cutoff`, ordered + bounded (dashboard; needs the `tasks` composite index)
- `toggle_task_complete(task_id) -> tuple[Optional[dict], list[str]]` — flips `à_faire` ↔ `terminée`; fires `_sync_protocol_step`
- `_sync_protocol_step(task_id, new_status)` — bidirectional sync; uses module-level `_SYNCING` set to prevent loops
- `get_task_summary(dossier_id) -> dict`
- `task_to_vtodo(task) -> str` — VTODO with PRIORITY, STATUS, DUE, COMPLETED, CATEGORIES, and `RELATED-TO;RELTYPE=PARENT:{note_vjournal_uid}` when `related_note_id` is set
- `vtodo_to_task(ical_str) -> dict` — inverse, resolves RELATED-TO via `note._find_note_by_vjournal_uid`

### `models/note.py`
- `toggle_pin(note_id) -> tuple[Optional[dict], list[str]]`
- `_find_note_by_vjournal_uid(uid) -> note | None` — used for RELATED-TO resolution
- `get_notes_summary(dossier_id) -> dict` — includes the analyse note (its only caller is the MCP `get_dossier`, whose read paths expose it)
- `list_notes(..., include_analyse=False)` / `list_notes_recent(..., include_analyse=False)` — the « Théorie de la cause » note is EXCLUDED by default (Notes views); the DAV collection paths, `_sync_dossier_dav_visibility` and the MCP note tools pass `True` (Python filter, no index — see Known Gotchas)
- `ANALYSE_TITLE` / `_ANALYSE_SEED` — title + 8-block seed of the analyse note (verbatim from `SPEC_Analyse_theorie_de_la_cause.md` Annexe A)
- `get_analyse_note(dossier_id)` / `has_analyse(dossier_id)` / `create_analyse_note(dossier_id)` — the Analyse leaf's single note; creation is idempotent, `category="stratégie"`, `dateless=True`, `is_analyse=True`; the CTag bump belongs to the route
- `note_to_vjournal(note) -> str` — VJOURNAL with SUMMARY (title), DESCRIPTION (content), CATEGORIES (category), X-ATHENA-PINNED if pinned; omits DTSTART when `dateless` (CREATED/DTSTAMP stay — the jtx NOT-NULL trap) and adds `X-PALLAS-ANALYSE:true` when `is_analyse`
- `vjournal_to_note(ical_str) -> dict` — sets `dateless` from DTSTART's absence; sets `is_analyse` only when the X-prop is present (never an explicit False — `update_note`'s merge keeps the stored flag when a client strips unknown X- properties)

### `models/protocol.py`
- `get_template(protocol_type) -> list[dict]` — returns CQ/CS template (hardcoded `CQ_TEMPLATE_STEPS` / `CS_TEMPLATE_STEPS`) or `[]` for `conventionnel`
- `create_protocol(dossier_id, protocol_type, start_date, data)` — rejects if active protocol exists; auto-generates steps from the template; optionally calls `_auto_create_tasks_for_steps`
- `get_protocol(protocol_id)` — returns protocol with `steps` attached
- `get_protocol_for_dossier(dossier_id, active_only=True)`
- `list_protocols_for_dossier(dossier_id) -> list[dict]` — newest first, without steps
- `list_protocols(status_filter=None, ...)`
- `add_step`, `update_step` (blocked when `deadline_locked`), `delete_step` (blocked when `mandatory`)
- `complete_step(protocol_id, step_id)` / `uncomplete_step(...)` — fires `_sync_task_status` and may trigger `_check_protocol_completion`
- `recompute_deadlines(protocol_id, new_start_date)` — for offset-based steps; uses `utils.deadlines.compute_deadline`
- `check_overdue_steps(protocol_id) -> int` — flips `status → en_retard` on past-due, non-completed steps
- `get_protocol_summary(dossier_id) -> {has_protocol, has_history, total, completed, overdue, upcoming, ...}`
- `get_current_phase_for_dossier(dossier_id) -> (phase, sous_phase)` — Phase O : « l'étape courante » = première étape non complétée dans l'ordre du protocole actif ; son annotation est le défaut suggéré des formulaires temps/dépense/tâche. `("","")` sans protocole/annotation. **~10 lectures — payé UNIQUEMENT au GET d'un formulaire qui connaît déjà son dossier**, jamais sur DAV ni sur un formulaire vierge (règle `_linked_step`). Fail-open (une suggestion ne casse jamais un rendu).
- `list_urgent_steps(cutoff, limit=50) -> list[dict]` — replaces the dashboard N+1: ONE `collection_group("steps")` query (status in active set + deadline ≤ cutoff, 3× over-fetch) + ONE batched `get_all` of distinct parent protocols; only steps of `actif` protocols survive, enriched with `_protocol_title`/`_protocol_id`/`_dossier_file_number`. Needs the `steps` COLLECTION_GROUP index.
- `_sync_task_status(task_id, step_status)` — uses `_SYNCING` guard (separate set from `task.py`)
- `_auto_create_tasks_for_steps(protocol)` — creates a task per step and links it via `linked_task_id`
- `_check_protocol_completion(protocol_id)` — auto-transitions to `complété` when all mandatory steps are done

### `models/document.py` + `models/folder.py`

Document functions:
- `upload_document(dossier_id, dossier_file_number, file_stream, filename, file_size, metadata, user_id)` — the THROUGH-APP stream path, since 2026-08-12 used only by the GENERATED documents (gabarits, notes d'honoraires — small .docx; the browser upload form went direct-to-GCS). Uploads to `users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}`, creates Firestore doc with `folder_id`. Validation is magic-byte based (`_sniff_content_type`, 512-byte bounded probe + `seek(0)`): the sniffed type must be in `ALLOWED_MIME_TYPES` and agree with the extension; the sniffed type (never `mimetypes.guess_type`) sets the stored `file_type` and GCS Content-Type. The two container signatures are extension-disambiguated, fail-closed on any other pairing (PK → `.docx`/`.xlsx`/`.zip`; OLE2 → `.doc`/`.msg`/`.xls` — Excel since 2026-08-13), and `.eml` — which has NO magic — is recognized LAST by a regex-free RFC 5322 first-header-field byte check (`_looks_like_eml`; CWE-1333 doctrine), so a real magic always wins. ZIP/.eml/.msg/.xls/.xlsx blobs additionally get `Content-Disposition: attachment` at upload (`_ATTACHMENT_ONLY_TYPES`). The storage-path filename segment goes through `secure_filename` (raw name kept only in `original_filename`/`display_name`).
- `ingest_blob_as_document(source_blob, dossier_id, dossier_file_number, filename, metadata, user_id)` (2026-08-12) — ingestion by GCS-side COPY: same validation contract as `upload_document` (size ≤ 200 MB from the blob's own metadata, sniff on a 512-byte ranged probe via `_sniff_header`, extension agreement), then a rewrite loop to the canonical path, the destination patched with the SNIFFED type (never the source's declared one) + the attachment discipline. The bytes never transit the application (32 MB platform cap). Callers: Réception's verser (quarantine source) and `/documents/api/finaliser` (staging source). The CALLER must have `reload()`ed the source and owns its cleanup. Shares `_prepare_document_record` with `upload_document` (one record shape, one filename sanitation).
- `build_folder_zip_url(dossier_id, folder_id, user_id)` (2026-08-13) — « Télécharger (zip) » : composes a folder subtree's archive IN GCS (streamed `zipfile` over `blob.open("wb", ignore_flush=True, chunk_size=8 MiB)` — **`ignore_flush` is load-bearing**: zipfile calls `flush()` and `BlobWriter.flush()` raises without it; the 40 MiB default buffer is also why `chunk_size` is explicit) then returns a 15-min signed URL. `folder_id=None` = the whole dossier. TWO caps checked on Firestore metadata BEFORE any byte moves (`MAX_ZIP_TOTAL_BYTES` 400 MB ≈ 21 s at the 20 MB/s floor, `MAX_ZIP_FILES` 150 ≈ 15 s of GET initiations — the byte cap alone protects nothing against many small files; gunicorn SIGKILLs at 60 s). ZIP_STORED (the corpus doesn't recompress; DEFLATE would turn an I/O route CPU-bound). ALL-OR-NOTHING: any failure aborts and `BlobWriter.__exit__` TERMINATES the resumable session — no partial object ever exists; the only possible staging residue is a complete zip whose signing failed, swept by the `staging/` 7-day lifecycle. Entry names: Windows-safe sanitizer (hostile chars, DOS reserved stems, trailing dots), display_name-first with a known-extension guarantee, per-directory case-folded dedup (folders claim first), dangling `folder_id` docs land at the archive root — never silently dropped; empty subfolders preserved via `mkdir`.
- `list_documents(dossier_id=None, folder_id=None, category=None, search=None, sort_by="created_at")`
- `update_metadata`, `delete_document`
- `get_signed_url(document_id, expires_in_minutes=15) -> str`
- `move_document(dossier_id, document_id, target_folder_id)` — Firestore-only update
- `move_documents_bulk(dossier_id, document_ids, target_folder_id) -> int`
- `get_document_summary(dossier_id) -> dict`
- `format_file_size(size_bytes) -> str`, `get_file_icon(file_type) -> str`

Folder functions:
- `create_folder(dossier_id, name, parent_folder_id=None)` — max depth 5, no dupes in parent, sanitize name
- `get_folder`, `list_folders(dossier_id, parent_folder_id=None)`
- `rename_folder`, `move_folder` (circular-ref prevention via parent chain walk + subtree depth check), `delete_folder(dossier_id, folder_id, *, contents="move"|"delete") -> (ok, msg, rapport)` — the subtree in one shot; `rapport` carries the folders and documents ACTUALLY destroyed so the route can journal one event per entity
- `subtree_index(dossier_id) -> {folder_id: {direct, documents, folders}}` and `subtree_members(dossier_id, folder_id) -> (folder_ids, documents)` (August 2026) — the TWO-query idiom `build_folder_zip_url` proved, replacing the per-folder `_count_items` N+1 in the browser (that helper — one query per folder, and fail-OPEN — was **deleted** with its last caller; do not reinstate it). Both **fail CLOSED** (they propagate), unlike `list_folders`/`list_documents` which fail open: a destructive dialog must never under-report what it will destroy
- `get_folder_breadcrumb(dossier_id, folder_id) -> list[{id, name}]`
- `get_folder_tree(dossier_id) -> nested list` — for move modal
- `get_or_create_folder(dossier_id, name, parent_folder_id=None) -> Optional[dict]` — **Phase H.2**: returns the existing root-level folder of that name (case-insensitive) or creates it; idempotent so repeated generations reuse the one « Projets » folder (`document.GENERATED_FOLDER_NAME`) instead of tripping the duplicate-name check. **All** generated documents (gabarit letters/procedures AND notes d'honoraires) save there, named by `document.projet_document_name(reference, template_name, day)` → `"REF - YYYY-MM-DD - Projet Nom"`

### `models/doc_template.py` (Phase H — gabarits)

- `create_template(file_stream, filename, file_size, metadata, user_id) -> tuple[Optional[dict], list[str]]` — validates size (≤10 MB) / `.docx` extension / archive structure (`validate_template`), extracts + classifies placeholders (`classify_placeholders`), uploads to Storage (`users/{userId}/templates/{templateId}/{filename}`), persists the doc with the field inventory; Storage rollback on Firestore failure
- `get_template(template_id)`, `list_templates(category=None, search=None)` — single `order_by("name")`, filters client-side (small bounded collection, no index)
- `update_template(template_id, data, file_stream=None, filename=None, file_size=None)` — with a file: re-validate, re-extract, upload NEW Storage object, `version += 1`, delete the old object only after the doc points at the new one
- `delete_template(template_id)` — Storage object (NotFound tolerated) + Firestore doc
- `get_template_bytes(template_id) -> Optional[bytes]` — for filling
- `get_note_honoraires_template()` / `get_note_template() -> Optional[dict]` — **Phase H.2/H.3**: most-recently-updated template of the kind (shared `_latest_template_of_kind`, Python filter over the small collection, no index); the `/factures/<id>/note-docx` and `/notes/<id>/gabarit-docx` routes select them. `VALID_KINDS = ("gabarit", "note_honoraires", "note")` with `kind` set from a **radio group** on the upload/edit form (`routes/doc_templates._kind_from_form` — the legacy `is_note_honoraires` checkbox still maps as fallback); legacy docs without `kind` read as `"gabarit"`
- `get_signed_url(template_id, expires_in_minutes=15)` — IAM signBlob signing, attachment disposition
- `VALID_CATEGORIES = ("procédure", "correspondance", "autre")`, `MAX_TEMPLATE_SIZE`, `DOCX_MIME`
- Split-run suspects become French `validation_warnings` strings; the upload proceeds (the field simply won't fill until retyped in Word and re-uploaded)

### `models/reference.py` (read-only)

All lookups are **in-memory** (`_PALAIS` / `_GREFFES` / `_JURIDICTIONS` / `_FORUMS` module dicts) — the `ref_*` Firestore collections are an unread mirror. The module imports only `typing`: keep it Firestore-free so it stays a pure, unit-testable table.

- `get_greffe(greffe_number) -> dict | None`
- `get_juridiction(juridiction_number) -> dict | None`
- `get_palais(palais_key) -> dict | None` — court location by slug, with `palais_key` attached; returns a **copy** (callers can't corrupt the shared table — note `get_greffe`/`get_juridiction` still hand back the live dict)
- `get_greffe_address(greffe_number) -> dict | None` — resolves a greffe to its location via `palais_key`. `None` means **"no published address"** (itinerant greffe, or unknown greffe number), never "no address exists" — resolve before relying on it for a filing
- `format_palais_address(palais, multiline=False) -> str` — MJQ-style rendering: `"227, rue Racine Est, 1er étage, Saguenay (Québec) G7H 7B4"`; `multiline=True` breaks before the city for a letter address block; tolerates `None`
- `list_greffes()`, `list_juridictions()`, `list_palais(location_type=None)` — `location_type` filters to `"palais"` or `"point_de_service"`
- `parse_court_file_number(court_file_number) -> dict` — returns `{greffe_number, juridiction_number, greffe, juridiction, is_administrative, parse_error}`. Letters prefix → `is_administrative=True`, no parsing. Format `NNN-NN-...` required, else `parse_error`.

**Non-judicial forums (`_FORUMS`, July 2026).** The forums the court-file parser can't handle — the **16 Québec administrative tribunals** (CJAQ list: TAQ, TAT, TAL, TAMF, TADP, CAI, CFP, CPTAQ, CTQ, CMQ, CQLC, BPCD, RE, RACJ, RMAAQ, RBQ) + the **4 federal courts** (Cour fédérale, Cour d'appel fédérale, Cour canadienne de l'impôt, Cour suprême du Canada). Slug → `{name, abbr, category}` where `category` ∈ {`"administratif"`, `"federal"`}. Tribunaux spécialisés of the Cour du Québec (droits de la personne, professions) are **deliberately absent** — they run through the judicial stream (juridiction codes 53/07). The dossier's `forum` field holds a slug; `forum` drives the `tribunal` name and `is_administrative_tribunal` (True only for `"administratif"`).
- `get_forum(forum_key) -> dict | None` — with slug attached; returns a **copy**
- `forum_tribunal_name(forum_key) -> str` — the display name written into the dossier's `tribunal`
- `list_forums(category=None) -> list[dict]` — name-sorted; `category` filters `"administratif"`/`"federal"`
- `forums_by_category() -> [(category_key, label, [forum…]), …]` — the form's optgroup picker, admin-then-federal order

### `models/trust.py` (Phase K — fidéicommis)

Append-only trust register. **Pure §6.1 helpers** (importable without the client, carry the test suite): `compute_deltas(direction, amount, status)` (the §4.4 balance atom — per-entry book/cleared/bank contribution), `check_disbursement_allowed(cleared, amount)` (the overdraft control), `reconciliation_variance(...)`, `to_barreau_row(tx, view)` (the 9 Barreau columns — the CSVs and the carte-client PDF; **not** the journal PDF, which is the art. 38 sheet of `utils/trust_journal_pdf.py`, nor the HTML tables, which read raw fields), `recompute_running_balances(txs, view)` (verification only). **Firestore layer** (the only part touching `db`): accounts CRUD (`create_account`/`get_account`/`list_accounts`/`update_account` — metadata only, never balances); the transactional writes `create_transaction` (the core — reads account/counter/last-entry/dossier/invoice, runs the guards + overdraft control + backdating guard INSIDE the transaction, then commits), `clear_transaction`/`clear_transactions_bulk` (all-or-nothing), `reverse_transaction` (mints the opposite `correction` entry; `en_circulation`→both `annulée`, `compensée`→reversal `en_circulation`), `create_inter_dossier_transfer` (two linked `compensée` legs in one account; overdraft applies to the source); reconciliation (`create_reconciliation` — refuses a future `period_end` and a `None` statement / `complete_reconciliation` — variance must be 0 **computed as of `period_end`, never as of now**, gated then committed atomically with an account-**etag sentinel** for concurrency and `bank_balance` **incremented** by the ticked deltas / `delete_reconciliation` — abandon a brouillon, transactional, refuses complétée; `list_outstanding`/`list_in_transit` with the `as_of` cutoff, plus the as-of layer `book_balance_as_of` (frozen `balance_after_account` of the last entry dated ≤ D, streamed `sequence DESC` on the existing index), `_list_cleared_after`/`_list_annulled_after` (the resurrection sets) and **`reconciliation_as_of_context`** — the ONE seam the worksheet render and the completion gate both consume); queries (`list_transactions`/`list_transactions_page` cursor-paginated by `sequence` DESC/`list_card_transactions`/`list_dossier_transactions`/`get_transaction`, plus **`list_register(account_id, date_from, date_to)`** — the art. 38 period sheet's reader, date bounds pushed to Firestore on the `(account_id, date, sequence)` index and returning a `truncated` flag rather than swallowing it, and **`opening_book_balance(account_id, as_of_exclusive)`** — the « solde reporté », streamed `sequence DESC` on index #1 exactly like `book_balance_as_of` (it briefly ordered `date DESC` on the `(account_id, date, sequence)` index instead and 500'd the export — see the Known Gotcha on index inverses), returning `(cents, had_prior_entry)` so a register can tell « reporté 0,00 $ » from « nothing precedes this period »; `implied_opening_balance(first_tx)` is its pure cross-check); and summaries (`get_trust_summary` — `in_transit = book − cleared` per client, no query; `get_firm_trust_snapshot`). **No `update_*`/`delete_*` for transactions; no SUM aggregation** (bounded Python sums, sidestepping the June-2026 index trap). Abort reasons are machine-stable strings mapped to French via `_ABORT_MESSAGES`.

### `models/admin_ledger.py` (August 2026 — comptabilité d'administration)

The trust harness with three deliberate divergences (module docstring — see the Administration collections above). **Pure layer** (carries `tests/test_admin_ledger.py`): `admin_delta(direction, amount)` (signed, STATUS-BLIND — annulée rows count, netting only with their reversal), `display_balance`/`statement_to_ledger` (the card sign conversions — one compute path, the TYPE decides display), `extract_taxes_from_gross(gross) -> (net, tps, tvq)` (Decimal ROUND_HALF_UP, remainder imputed to the NET so the sum is EXACT; constants LOCAL and test-pinned to invoice's GST/QST BPS — never imported, vocabulary doctrine), `validate_ventilation` (recette → zeros silently; blank déboursé → net=amount; otherwise exact sum), `running_balances(txs, opening)` (the RENDER path — trust's verification-only helper promoted). `reconciliation_variance` is IMPORTED from `models.trust` (pure, generic). **Firestore layer**: accounts CRUD (no zero-balance close rule); `_read_lock_floor` (latest complétée period_end, streamable inside a txn) + `_entry_lock_reason` (the four lock clauses + the two linkage clauses — reversal is NOT gated); `create_transaction` (lock + future-date guards, `encaissement_facture` verified against the invoice's **LIVE balance** `amount_due − amount_paid` — stricter than trust's frozen check — on `opérations` accounts only, dossier labels snapshotted off the invoice); `update_transaction`/`delete_transaction` (in-txn lock re-read, `delta_new − delta_old` ledger adjust, `revisions` append / route-recorded deletion); `clear_transaction(+bulk)` (regenerates the account etag — the reconciliation sentinel must see a clear even though no balance moves); `reverse_transaction(tx_id, reason, reversal_date=None, *, allow_linked=False)` (trust status algebra; date choosable in [original, today] above the floor; a `paiement_carte` leg reverses BOTH legs; `allow_linked` reserved for the trust orchestration); `create_card_payment`/`delete_card_payment` (two legs, two counters, one txn); `attach_receipt` (the one post-create mutation outside the lock — returns `_previous_receipt_path` for blob cleanup); reads (`list_register` (date, sequence)-ordered with date bounds pushed to Firestore, `opening_ledger_balance` = full-pre-history Σ returning `(cents, had_prior)` — RAISES on truncation, a partial sum is a wrong balance; `book_balance_as_of` likewise; `find_by_trust_transaction` (fails OPEN — a display/orchestration aid) and son frère **`list_by_trust_transaction`** (2026-08-17) qui **PROPAGE** et rend TOUTES les lignes : la reprise s'en sert de clé d'idempotence, usage que l'aîné ne peut pas servir — il échoue ouvert, donc un hoquet de lecture ferait doubler une écriture qu'`_entry_lock_reason` rend ensuite incorrigible, et sa `.limit(2)` détecte le doublon sans jamais le dire ; `sum_invoice_receipts` — the recomputable Lot P cumulative — et `list_invoice_receipts`, son pendant de lecture qui garde les contre-passées et échoue OUVERT); the reconciliation subsystem (trust shape; per-entry status+ETAG re-verify at completion — dates are editable until this very lock); `get_firm_admin_snapshot` + the copied `_reconciliation_overdue` predicate (PA-D06 doctrine — mirror fixes to trust's).

### MCP (Phase I)

The Phase-I MCP layer added 14 tools; Phase K adds 3 read-only trust tools (17); Phase L adds 2 note-write tools (19); the July 2026 audit remediation adds 3 read tools and 7 create-only writes (29); **the August 2026 mandate adds 3 read tools (`list_invoices`, `get_invoice`, `get_coverage_report`) and `complete_task` (33)**; **lot Q — la reprise de données historiques — adds 3 read tools (`get_reference_vocabulary`, `find_imported`, `get_import_audit`) and 7 writes (`create_partie`, `update_partie`, `create_dossier`, `update_dossier`, `update_time_entry`, `update_expense`, `import_invoice`) — 43 (26 read + 17 write)**; **the August 2026 phase reclassifiers add 4 writes (`set_time_entry_phase`, `set_expense_phase`, and their `_bulk` twins) — 47 total (26 read + 21 write)**. The tool handlers in `mcp/handlers.py` compose **existing** model/util functions only; filters the model layer lacks are applied in the handler over a bounded fetch (≤ 200 docs), and **no composite index exists for an MCP-only query**. **Only the handlers named in `mcp.tools.WRITE_TOOLS` write to Firestore, and the writable surface is exactly: `notes`, `tasks`, `hearings`, `timeentries`, `expenses`, `parties`, `invoices` (create-by-import only, with its `lineitems` subcollection and the `invoiced` flips it causes on the two billing collections) and `dossiers` — the two append-only arrays (`significations`, `prescription_events`), the fill-only-if-empty fields of `complete_dossier` (« empty » ≡ absent/blank/still equal to the `field_defaults()` default; one conflicting field → atomic French refusal, nothing written), and since lot Q a full create plus a field-by-field correction that REPLACES what it names. A dossier's `status` is settable at CREATION and refused afterwards; party arrays are append-only on the edit path. The `invoiced` wall has exactly ONE opening: `phase`/`sous_phase` on `timeentries`/`expenses`, through `set_*_phase` — a four-key partial `update()` that cannot reach a money figure, every other field of a billed row staying frozen and `update_time_entry`/`update_expense` still refusing one outright** — every other handler stays strictly read-only (notably: `list_protocol_steps` derives overdue status by date comparison instead of calling `check_overdue_steps`, which writes; the trust tools call `models/trust.py` read paths only). Every write runs through `mcp/write_support.run_write`: `dry_run: true` validates fully and persists nothing; `idempotency_key` stores the result under `mcp_idempotency/{sha256(tool:key)}` for 24 h (TTL fieldOverride + code-enforced expiry; replay returns the stored result with `idempotent_replay: true`; same key + different args → refusal; the cache **fails open** both ways and a refused call records nothing). Note that the *request* path writes outside the tool path regardless (`bearer.stamp_token_last_used`, `oauth.touch_client`, the idempotency records). **Trust tools never emit the bank transit or account number.**

**Phase-L write invariants** (each pinned by a test in `tests/test_mcp_tools.py`): the handler resolves the dossier **first** and refuses an unknown id (never blanks it as `routes/notes.py` does — that path writes an orphan note reachable from nowhere); the model payload is an **explicit whitelist**, never `**args` (`models.note.create_note` honours a caller-supplied `id` and then does a full-document `set()`, so a forwarded `id` would silently overwrite an existing note); the write **bumps `dossier:{dossier_id}`** (plus `remove_tombstone` on create) with the bump wrapped in its own `try/except`, because a raise after the commit would reach `endpoint._tools_call`'s blanket `except Exception` and report a committed write as a failure — the model would retry and duplicate the note; `append_to_note` **refuses** rather than letting `security.sanitize` truncate at `CONTENT_MAX_LENGTH`; and Markdown autolinks are normalized (`<https://…>` → `[url](url)`) while any residual `<…>` run is **refused loudly** — `sanitize` would delete it silently, so `« si a < b et b > c »` would otherwise lose « < b et b > » with a success envelope. **The July 2026 creators inherit every one of these invariants** (dossier resolved first, explicit whitelist — `create_task` additionally PINS `status: "à_faire"` and strips a caller-supplied `id`/`vevent_uid` —, bump via `collection_for(dossier_id)` wrapped in its own `try/except`, closed-dossier `dav_synced=false` + French warning, refusals never quoting content), with one cap difference: task/hearing `_sanitize_data` truncates at **2000** chars, not the notes' 100k, so `_clean_entity_text` refuses at that ceiling.

---

## Utility Modules

### `utils/docx_fill.py` (Phase H — pure stdlib fill engine)

No Firestore, no Flask — fully unit-testable. Operates by direct string substitution on `word/document.xml` + `word/header*.xml` + `word/footer*.xml` inside the zip; every other entry is copied byte-identical (Word must reopen the output without repair — the reason `docxtpl` is rejected).

```python
PLACEHOLDER_RE                                  # {{name}} — accents, dots, optional inner spaces
extract_placeholders(docx_bytes) -> list[str]    # distinct names, document order (tag-stripped scan)
validate_template(docx_bytes) -> TemplateValidation  # .placeholders, .split_run_suspects, .errors (French)
fill_docx(docx_bytes, values, *, rows_by_region=None, conditions=None) -> bytes
                                                 # raises DocxFillError on structural problems
```

- **Phase H.2 extensions** (`rows_by_region`/`conditions`, `word/document.xml` only; both `None` → identical to Phase H). Order inside a target (§4.3): normalize runs → **conditional regions** → **repeating rows** → block paragraphs → scalars. **Conditional regions** `{{?cond}}` … `{{/cond}}` (markers in their own paragraphs bracketing a table): when the flag is false the whole marker-paragraph→marker-paragraph span is deleted (removing the table cleanly — never a partial table, which Word rejects), when true the marker *paragraphs* are removed entirely (`_remove_marker_paragraph` — leaving **no blank line**; a marker sharing its paragraph with other text keeps the paragraph, marker stripped). If that leaves two `<w:tbl>` directly adjacent (which Word would merge), `_ensure_table_separation` inserts a **minimal ~1pt paragraph** between them — distinct tables, no visible gap (put a section heading inside the `{{?cond}}` to avoid even that); an unbalanced open/close raises `DocxFillError`. **Repeating rows** `{{#region}}` in a row's first cell: the innermost `<w:tr>` is cloned once per row dict (row-scoped `{{h.date}}`/`{{d.cout}}` fields resolved per item), preserving cell borders/shading; an empty list removes the row. Split-run detection now also flags fragmented `{{#…}}`/`{{?…}}`/`{{/…}}` markers (§3.4). Used by the note-d'honoraires generation (`utils/invoice_docx.py`); the four Phase H callers pass neither extra and are untouched.
- **Block expansion first** (values containing blank-line separators): the host `<w:p>` is cloned once per chunk with the placeholder substituted — numbered-list `<w:pPr>` XML is preserved, so chunks continue the list numbering. The paragraph scan covers ALL paragraphs (regression: a previous implementation passed `count=1`). This is **value-driven**, not classification-driven: the engine expands any multi-paragraph value it is *given*. (Since July 2026 the gabarit UI no longer classifies anything as a "block" and leaves such content for Word — see `template_fields.py` passthrough — so this path is dormant for gabarit generation, but the capability is retained and still tested in `test_docx_fill.py`.)
- **Scalars second**; single `\n` → one space; XML escaping (`& < >`) + C0 control stripping (except `\t`); **function replacement callbacks only** (a bare replacement string would interpret `\g<0>`/backslashes in user content).
- Safety caps: compressed ≤ 10 MB, single XML target ≤ 25 MB, total decompressed ≤ 100 MB, ≤ 2000 entries, no absolute/`..` entry names, magic `PK\x03\x04` + `[Content_Types].xml` + `word/document.xml` required.
- **Split-run detection:** names visible in tag-stripped text but not matchable in raw XML were fragmented across `<w:r>` runs by Word → reported as suspects at upload (user retypes the field in Word in one stroke); never silently rewritten.

### `utils/template_fields.py` (Phase H — field catalog)

> **The complete placeholder inventory** — every `{{…}}` name across the catalog, flat aliases, manual fields, passthrough, plus the note-d'honoraires `facture.*` / repeating rows / `si_*` conditions — is documented in [`GABARITS_PLACEHOLDERS.md`](GABARITS_PLACEHOLDERS.md) (repo root). **Keep it in sync** when you change the catalog / aliases / manual fields here, or the regions / conditions in `utils/invoice_docx.py`.

Pure functions (mirrors `display_name` locally — must stay importable without the Firestore client). `classify_placeholders(names) -> Classification` (`.auto` map, `.manual` list, `.passthrough` list, `.slots_required` ⊆ {dossier, client, adverse, destinataire}) and `resolve_values(names, *, dossier, client, adverse, destinataire, firm, today) -> dict[str, str]` (only non-empty resolutions; absent = popup shows an empty input).

**Three kinds** (the ALL-CAPS→"block" concept was removed July 2026): **auto** — matches the catalog/aliases **case-insensitively** (`{{TRIBUNAL}}` resolves like `{{tribunal}}`; an ALL-CAPS placeholder gets its value upper-cased — `{{TRIBUNAL}}` → `COUR SUPÉRIEURE` — via `is_uppercase_name`); **manual** — the short `MANUAL_FIELDS` letter-metadata, prompted in the popup; **passthrough** — everything else (former ALL-CAPS blocks like `{{FAITS}}`, the `{{civilité}}`/`{{salutations}}` fields, unknown names): **not resolved and not prompted**, left verbatim as `{{name}}` in the output for the user to complete in Word. The route omits passthrough names from the fill `values`, and `fill_docx` leaves any unlisted placeholder untouched.

- Catalog namespaces: `dossier.*` (incl. derived `role_feminin`, capitalized `role_label`, demandeur/défendeur **positions** swapped by `dossier.role` with `autre` → unresolved, the **recours fields** `objet` / `valeur` (fr-CA currency) / `classe` (`compute_class`) / `prescription` (label) / `droit_action` / `date_pour_agir`, and the **« Mandat » card fields** `type_mandat` / `type_dossier` / `type_honoraires` (all label-mapped — the three label dicts are mirrored locally to keep the module Firestore-free, **kept in sync with `models/dossier.py`**) / `honoraires` (fee + rate jointly, via the shared `format_honoraires`) / `taux_horaire` / `forfait` (fr-CA currency) / `ouverture` / `fermeture` / `retention` (= `fermeture` + 7 ans, via the shared `retention_date`; `routes/dossiers.py` imports both helpers so the card and generated docs match)), `client.*`/`adverse.*`/`destinataire.*` (identical field set; **no `civilite` — civilité is passthrough**; work-address preference for `avocat_adverse`/`expert`/`huissier`/`notaire` **with a fallback to the other block** — `selected_address` is the shared authority, see the Known Gotcha; phone work → cell → home via `format_phone_display`), `cabinet.*` (FIRM_*), `date.aujourdhui` (French long date, `1er` for the 1st) / `date.aujourdhui_iso`.
- **Person names render BARE (no honorific) by default (July 2026).** The stored snapshot name carries the `Me`/`M.`/`Mme` prefix (`display_name` prepends it), so `{{dossier.demandeur}}`/`{{…defendeur}}` strip it (`_strip_civility_prefix`) and `{{<slot>.nom_complet}}` builds from first+last (`_nom_bare`) — a procedure intitulé cites the party bare. Each has an **`…_avec_civilite` twin** (`{{dossier.demandeur_avec_civilite}}`, `{{<slot>.nom_complet_avec_civilite}}`) that keeps the honorific, for a letter address block. Accented `…_avec_civilité` spellings are auto-registered (`_register_civility_variants`); the positions also get flat aliases. Organizations have no honorific, so both forms equal the legal name.
- `FLAT_ALIASES` maps the existing gabarits' flat French names (`{{district}}`, `{{numero_dossier}}`, …) onto the catalog — one template set serves this module and the user's Claude.ai skills. The `civilité`/`civilité_récipient` aliases were **removed** (civilité is now passthrough — it must appear in letters but never in court procedures, so the user places and fills it).
- `MANUAL_FIELDS` (deliberately data-less letter metadata: `objet_lettre`, `privilège`/`transmission_lettre` selects, `pièces_jointes` default `"Aucune"`, `référence_externe`, …). **`salutations` was removed** — it is passthrough.
- Missing-value strings (exact): auto field left blank → **`[CHAMP MANQUANT : {name}]`**; manual/unknown-but-prompted left blank → **`[À COMPLÉTER : {name}]`** (`fallback_value`). Passthrough fields get neither — the raw `{{name}}` survives. Generation never fails on a missing value.

### `utils/format_fr.py` (Phase H.2 — French formatting)

Pure functions, thoroughly tested; centralized so the note d'honoraires (and, optionally later, the on-screen invoice + reportlab PDF) format money identically. `format_cents_fr(cents) -> "1 150,00 $"` (NBSP thousands, comma decimal, trailing ` $`; `0` → `"0,00 $"`), `format_cents_fr_parens(cents) -> "(1 150,00) $"` (retainer deduction), `format_rate_fr(rate, scale) -> "5 %"`/`"9,975 %"` (**GST stored ×100, QST ×1000** — caller passes the scale; `models.invoice` stores `gst_rate=500`, `qst_rate=9975`, and `compute_totals` uses hardcoded `Decimal("0.05")`/`Decimal("0.09975")`, so stored rates are display-only), `format_hours_fr(h) -> "0,50"`, `parse_cents_fr("1 150,00") -> 115000` (the inverse of `format_cents_fr`: accepts a comma decimal, ordinary/non-breaking/narrow spaces and a trailing `$`; ROUND_HALF_UP to the cent; **raises rather than returning 0** — a silent zero would record an invoice as unpaid while reporting the payment saved), `format_date_fr(d) -> "11 décembre 2025"` (`1er` for the first; a datetime uses its UTC calendar date — no Montréal shift).

### `utils/invoice_docx.py` (Phase H.2 — note-d'honoraires context builder)

Pure function `build_invoice_context(invoice, line_items, *, firm, destinataire, dossier, today) -> InvoiceContext` (`.values` scalars, `.rows` region→row-dicts, `.conditions` si_* bools). Maps a stored invoice to the `facture.*` scalar fields (§6.2) **read, never recomputed** — every figure formatted via `format_fr`, the only arithmetic being integer-cent addition of the two derived disbursement subtotals (**invariant: `sous_total_debours_tx + sous_total_debours_ntx == subtotal_expenses`**). Header namespaces (`destinataire.*`/`dossier.*`/`cabinet.*`/`date.*`) resolve through the Phase H catalog by **canonical name AND flat alias** (`resolve_values(list(CATALOG) + list(FLAT_ALIASES), …)`), so a note template can use the identical placeholders as the procedures/letters gabarits (`{{numero_dossier}}` as well as `{{dossier.numero_cour}}`); `destinataire` falls back to a synthetic partie from the invoice's `billing_address` snapshot when the client partie was deleted, so generation never fails. Row-scoped fields are prefixed `h.`/`d.` so they never collide with global scalars. `facture.taux_horaire` is the uniform billed rate when all fee lines share one, else the dossier's `hourly_rate` fallback, else blank.

### `utils/deadlines.py`

Implements Quebec judicial delay rules under **art. 83 C.p.c.**: all calendar days count; if the raw deadline lands on a non-juridical day (weekend or statutory holiday), extend further in the direction of computation until a juridical day is reached.

```python
compute_deadline(start_date: date, delay_days: int, direction: "after"|"before") -> date
is_juridical_day(d: date) -> bool
next_juridical_day(d: date) -> date
prev_juridical_day(d: date) -> date
add_jours_ouvrables(start: date, n: int) -> date   # n business days (skips weekends +
                                                   # Québec holidays); serves the
                                                   # 3_jours_ouvrables avis delay (Loi sur
                                                   # la presse) via recours.AVIS_PERIODS;
                                                   # n=0 → start unchanged. July 2026, additive.
get_quebec_holidays(year: int) -> list[date]
last_action_day(deadline: date) -> (date, bool)  # July 2026: the last juridical
                                           # day ON OR BEFORE the deadline +
                                           # whether it differs — the shared
                                           # last-action helper (dashboard AND
                                           # MCP get_agenda; prev_juridical_day
                                           # is INCLUSIVE by design)
# ── Lateness (August 2026) — a DIFFERENT question from computation ──────
today_mtl() -> date                        # the ONE clock read; Montréal
effective_due(deadline) -> date | None     # next_juridical_day(d), INCLUSIVE —
                                           # a no-op on computed deadlines
                                           # (already juridical); moves only
                                           # hand-typed weekend/holiday dates
is_past_due(deadline, *, today=None) -> bool   # effective_due strictly BEFORE
                                           # today; due TODAY is not late,
                                           # an undated deadline never is
days_until(deadline, *, today=None) -> int | None   # signed, on effective_due —
                                           # never negative without being late
# Lateness evaluates the PROROGUED deadline (decision 2026-08-02): due
# Saturday → actionable Monday → late Tuesday. Prorogation only ever makes
# lateness start LATER, never earlier.
_easter_sunday(year: int) -> date          # Meeus/Jones/Butcher algorithm
```

Quebec statutory holidays handled: Jour de l'An (+ Jan 2 if Jan 1 is Sunday), Vendredi saint, Lundi de Pâques, Journée nationale des patriotes (Monday before May 25), Fête nationale (June 24), Fête du Canada (July 1), Fête du Travail (1st Monday September), Action de grâce (2nd Monday October), Noël (Dec 25). Sunday→Monday observation rule applies to fixed holidays.

Integration points:
- `models/protocol.py`: `_compute_deadline` (CQ/CS template offsets and `recompute_deadlines`)
- `routes/dashboard.py`: `_get_prescription_alerts` computes `last_action_date = prev_juridical_day(prescription_date)` for display

### `utils/recours.py` (recours & prescription — pure)

No Firestore, no Flask — mirrors `deadlines.py` in style so the dossier's recourse fields compute identically wherever needed and stay unit-testable (`tests/test_recours.py`).

```python
Period = tuple[int, str]                               # (amount, "jours"|"mois"|"ans")
PRESCRIPTION_PERIODS: dict[str, (label, Period|None)]  # delay options, ascending
PRESCRIPTION_LABELS / VALID_PRESCRIPTION_TYPES         # derived (incl. "" = non définie)
prescription_period(prescription_type) -> Period | None
VALUE_CLASSES / TOP_CLASS                              # montant en litige → classe I–IV (inclusive cent bounds)
compute_class(valeur_cents) -> str | None              # Roman numeral "I"–"IV", else None
compute_date_pour_agir(droit_action_date, prescription_type) -> datetime | None
_add_years / _add_months / _add_period                 # calendar arithmetic per unit
# Échéancier par type de délai (July 2026 — spec « échéancier », § 5-6):
AVIS_PERIODS                                           # notice delays (Annexe B); reuses the
                                                       # PRESCRIPTION_PERIODS entries + the ONE new
                                                       # key "3_jours_ouvrables" (unit JOURS_OUVRABLES,
                                                       # via deadlines.add_jours_ouvrables) — NEVER in
                                                       # the prescription dropdown
PA_PERIODS = {"IMM-06": (10, YEARS)}                   # prescription acquisitive maturity per action
Echeance = NamedTuple(role, date, niveau, libelle, note)  # role ∈ principale|avis|defensive;
                                                       # niveau ∈ rouge|orange|normal|info|aucun
compute_echeances(action_code, date_depart, prescription_type="", *,
                  date_depart_avis=None, avis_confirmes=(),
                  inclure_suggestion_raisonnable=False) -> tuple[Echeance, ...]
```

- **Periods carry a UNIT (July 2026).** They used to be bare years (`_add_years` via `datetime.replace(year=…)`), which cannot express the taxonomy's *90 jours*, *45 jours*, *6 mois*, *3 mois*. `_add_period` dispatches: days → `timedelta`, months → `_add_months` (clamps the day to the target month's last — 31 janvier + 1 mois = 28/29 février, the month analogue of the 29 Feb → 28 Feb year clamp), years → `_add_years`. **`prescription_years` was renamed `prescription_period`** and returns `(amount, unit)`.
- **Labels are generic** (« 3 ans », not « 3 ans, art. 2925 C.c.Q. »). One period serves many articles — 1 an alone covers art. 1635 (paulienne), 929 (possesseur troublé), 2929 (diffamation) and 115 LNT — so an article in the label mislabels every other use. The article now travels with the taxonomy action (`utils.taxonomie` `references`).
- **The list is not only prescription.** It also carries *déchéance* and *avis* delays the taxonomy needs; `taxonomie.Action.delai_types` records which (the 11-token vocabulary). The field keeps the name `prescription_type` for continuity.
- The dossier form drives everything through `prescription_type` (dropdown) + `droit_action_date` (« droit d'action »). `models/dossier._apply_prescription_deadline` computes the « date pour agir » into **`prescription_date`** on save (imprescriptible → `None`; unset/`autre` → any existing value preserved) — since July 2026 it consumes `compute_echeances`' principale (with a `compute_date_pour_agir` fallback for actions with no dated principale, e.g. PA), behaviors byte-identical and pinned by test — so the dashboard/index/alerts keep reading the same field. The detail page shows domaine, action, `valeur` + `compute_class` (« Valeur (Classe) ») and « Fondement du recours » (`ref_fondement`) on the **« Recours »** card, and the prescription label, the délai nature, « Fondement du délai » (`ref_delai`), `droit_action_date`, `prescription_date` and the optional `date_avis` on the **« Prescription »** card — the rest of the taxonomy guidance (délai, point de départ, avis, avertissement) lives on the add/edit form only.
- **`compute_echeances` is orchestration, never new arithmetic** (spec § 0.3 — `_add_years`/`_add_months`/`_add_period`/`compute_date_pour_agir`/`next_juridical_day` are **intangible**): every dated échéance goes through `compute_date_pour_agir`/`_add_period` + `next_juridical_day` (art. 52 L.i. forward report), the sole additive unit being `jours_ouvrables` via `deadlines.add_jours_ouvrables`. Dispatch: PA → one *defensive* échéance (« interrompre avant » la maturité, `PA_PERIODS`); a lawyer-confirmed period is **authoritative** → principale identical to `compute_date_pour_agir` (niveau rouge `D` / orange `DR` — relief text from `taxonomie.DR_RELIEF_NOTES`); R → no firm date (indicative 30-jours suggestion on request); N/I/S/V/F → dateless message; unclassified/`-99`/unknown → PE-like default (pre-rework behavior verbatim). **Avis échéances are driven by `action.avis`, never the `A` token** (COR-11 has the token, no avis; RCV-03 the inverse — both binding annex content): a `conditionnel` avis needs its index in `avis_confirmes`, dates from its **own** `date_depart_avis` (never `droit_action_date`), and degrades to a dateless checklist item otherwise; an avis échéance never replaces the principale.
- `compute_date_pour_agir` extends a deadline that lands on a weekend / Québec statutory holiday **forward to the next juridical day** (`utils.deadlines.next_juridical_day`); it stays indicative — every limitation deadline must still be verified.
- `VALUE_CLASSES` holds the confirmed value table — Classe **I** (≤ 15 000 $), **II** (≤ 85 000 $), **III** (≤ 300 000 $), **IV** (> 300 000 $), each bound inclusive at the cent.

### `utils/taxonomie.py` (taxonomie des actions en justice — pure)

The two-level classification of Québec civil/commercial recourses: **20 domaines → 162 actions**, generated from « Taxonomie des actions en justice — Droit québécois » **v1.2** (16 juillet 2026 — a copy sits in `docs/` at the repo root; itself aligned on the FARBQ table « Prescriptions extinctives et autres délais », avril 2026), with `delai_types`/`avis`/`ref_fondement` transcribed from the **binding annexes** of « SPEC — échéancier par type de délai et avis » (18 juillet 2026, rév. 2). **The legal content of `taxonomie.py` changes only on an approved spec — never edit a row by hand**, and every displayed échéance is indicative. Pure (typing + functools only) — **keep it Firestore-free**: both `models/dossier.py` and `utils/template_fields.py` import it, and the latter must not pull in the Firestore client; `utils/recours.py` imports it too (no cycle — taxonomie imports nothing).

```python
Avis    = NamedTuple(libelle, delai_key, point_depart, reference, sanction="", conditionnel=False)
Action  = NamedTuple(code, libelle, delai="", delai_types=(), a_valider=False, point_depart="",
                     ref_delai="", ref_fondement="", avis=(), prescription_type="")
Domaine = NamedTuple(code, libelle, note, actions)
DOMAINES / ACTIONS / VALID_DOMAINES / VALID_ACTIONS / DOMAINE_LABELS
DELAI_TYPE_LABELS / VALID_DELAI_TYPES     # the closed 11-token § 4 vocabulary (one label per token)
DR_RELIEF_NOTES                           # relief text per déchéance relevable (Annexe A notes DR)
get_domaine(code) / get_action(code) / actions_for(domaine) / domaine_of(action)
action_label(code) -> "Libellé [CODE]"    # what the UI shows and a procedure cites
delai_types_label(code) -> str            # joined labels + « (qualification à valider) » suffix
niveau_decheance(code) -> "stricte"|"relevable"|None   # D outranks DR; is_decheance = deprecated alias
action_choices(domaine) / requires_precision(code) / avis_delai_display(key)
tooltip_payload(code) -> dict             # the § 7 standardized tooltip (lru_cached)
form_payload() -> dict                    # lru_cached; the form's embedded JSON (embeds tooltip per action)
```

- **`delai` is a suggestion, never a firm value.** The starting point is a question of fact; interruption/suspension escape any computation. `prescription_type` on an action is only a **suggested** key into `recours.PRESCRIPTION_PERIODS`, applied **only on a user action-change** (never on load, so it cannot clobber a confirmed value), and deliberately `""` where the delay is regime-dependent (RCV-05, COR-06), merely « raisonnable » (CJP-*), retrospective rather than running (**FAI-01** — the 6 months is an eligibility window *preceding* the application), a PA action (IMM-06 — the extinctive dropdown must not prefill a defensive maturity), or an « Autre (préciser) » row.
- **`delai_types` vocabulary is closed (11 tokens, § 4):** `PE` prescription extinctive · `PA` prescription acquisitive (defensive — point de départ = début de la possession adverse, « interrompre avant ») · `D` déchéance stricte (rouge) · `DR` déchéance relevable (orange, relief in `DR_RELIEF_NOTES`) · `A` avis préalable · `R` délai raisonnable · `N` aucun délai · `I` imprescriptible · `S` suit le droit sous-jacent · `V` variable · `F` fenêtre rétrospective. A tuple may combine tokens (`("PE","A")`); the `-99` rows carry `()`. `a_valider` replaced the source's embedded asterisks (24 pinned codes: Annexe A `a_val` ∪ Annexe C asterisk rows — union rule, user decision 2026-07-18); `references` split into `ref_delai` (source of the delay) + `ref_fondement` (seat of the right of action, Annexe C; C.c.Q. implicit). Six pinned rows keep `ref_delai=""` (CST-05, COR-04, COR-09, FAM-01, FAM-02, FAM-06 — no statutory delay source exists; user decision 2026-07-19).
- **The A token and the `avis` tuple are deliberately asymmetric** (binding annex content — do not "fix"): **COR-11** carries `A` for display but `avis == ()` (the 30-day delay IS the recourse), and **RCV-03** carries two `conditionnel` municipal avis (LCV 15 jours / CM 60 jours) while typed `(PE,)`. `test_a_token_and_avis_sets_are_pinned` pins both sets.
- **`niveau_decheance` must cover § 4 of the source.** A déchéance stricte is a délai de rigueur (neither suspends nor interrupts) and § 4 asks it be shown visually (red; relevable amber). **APP-01** states « déchéance expresse » in prose only. `tests/test_taxonomie.py::test_section_4_decheances_all_carry_D` pins the § 4 cross-section as `stricte` — extend it if the source changes.
- **The cascade ships the whole table to the browser**, embedded in `form.html` as a **non-executable `<script type="application/json">`** block (the pattern `base.html` uses for the App Check config; now larger with the per-action `tooltip`). It needs no CSP nonce (a data block is never executed, so `script-src` does not apply) and no round trip — a raw `fetch()` would carry no `X-Firebase-AppCheck` header, since App Check only gates `HX-Request` traffic. The § 7 tooltip renders **only on the add/edit form** (the Alpine block over `currentAction.tooltip`, fixed order: Délai · Type(s) · Point de départ · Avis requis · Réf. délai · Fondement · avertissement) — the detail page deliberately does NOT repeat it (user decision 2026-07-19): its cards cite only « Fondement du recours » (`ref_fondement`, Recours card) and « Fondement du délai » (`ref_delai`, Prescription card).
- Legacy `matter_type` → `domaine` mapping lives in `models/dossier._MATTER_TYPE_TO_DOMAINE`. Only the unambiguous keys map (recouvrement→REC, injonction→INJ, recours_extraordinaire→CJP, vice_cache→CON); **`action_dommages` deliberately maps to `""`** — damages can be contractual (CON) or extracontractual (RCV), and guessing would silently mislabel the file's liability regime (art. 1458 al. 2 C.c.Q. non-cumul).

### `utils/validators.py`

```python
normalize_phone(raw, default_country="+1") -> str | None      # → E.164
format_phone_display(e164) -> str                              # → "+1 (514) 555-1234"
validate_phone(raw) -> (normalized, error)
normalize_email(raw) -> str | None                             # lowercase + pattern check
validate_email(raw) -> (normalized, error)
normalize_postal_code(raw, country="CA") -> str | None        # "A1A 1A1"
validate_postal_code(raw, country="CA") -> (normalized, error)
apply_address_defaults(data, prefix="address") -> dict        # Canada/Québec/Montréal (full names); also migrates legacy "CA"→"Canada", "QC"→"Québec", etc.
```

`format_phone_display` is registered as the Jinja `phone` filter in `main.py`; templates use `{{ partie.phone_cell|phone }}`.

Call sequence in model layer: `_normalize` → `_sanitize_data` → `_validate`. All three happen in `create_*` and `update_*` for parties (the only model that uses these validators today).

### `utils/export_csv.py`

```python
export_csv(rows, columns: list[(key, label)], filename,
           date_format="%Y-%m-%d", cents_fields=None, hours_fields=None) -> Response
```

Outputs UTF-8 with BOM (`﻿`) for Excel compatibility with French accents. Cents fields divided by 100 with 2 decimals. Hours fields rounded to 1 decimal. Booleans become "Oui"/"Non". Datetimes formatted per `date_format`.

### `utils/export_pdf.py`

Uses `reportlab.platypus` for tabular reports. Column width ratios (3rd tuple element) define relative widths. Same `cents_fields` / `hours_fields` semantics as CSV. Landscape orientation for wide tables; portrait for narrow. Font: **Noto Serif** (August 2026) — static Regular/Bold TTFs vendored in `utils/fonts/`, registered at **module import** (`pdfmetrics.registerFont` + `registerFontFamily`), deliberately loud: `doc.build()` swallows exceptions into a French 500, so a missing/corrupt TTF must fail at import, where the test suite (which imports this module) turns it into a CI deploy-gate failure instead of a silent Helvetica fallback. reportlab does not support variable-font weight instances — keep the static TTFs, never substitute the web woff2 files. `tests/test_exports.py` pins `b"NotoSerif" in resp.data` (and the absence of `b"Helvetica"`) on both export functions.

### `utils/logging_setup.py`

Structured logging. `init_app(app)` (called from `create_app`) attaches a Cloud Logging `CloudLoggingHandler` (log name `pallas-athena`; routes `json_fields` into the LogEntry `jsonPayload` — the deprecated `AppEngineHandler` dropped them to `textPayload`) in production or stderr locally, plus two filters on every record: `ContextFilter` (injects `request_id`, `trace`, `auth_context`, `route`, `method`, `is_htmx`) and `RedactionFilter` (drops `SENSITIVE_KEYS`, scrubs emails/phones/postal codes from messages, `json_fields`, and tracebacks). Emit through the typed helpers, never raw `logger.*`:

```python
log_auth_event(event, outcome, *, reason=None, **extra)      # logger pallas.auth
log_dossier_event(event, dossier_id, **extra)                # logger pallas.dossier
log_dav_operation(operation, collection_type, ...)           # logger pallas.dav
log_security_event(event, severity, **extra)                 # logger pallas.security
log_unexpected(message, *, exc_info=True, **extra)           # logger pallas.unexpected (ERROR + traceback)
bind_context(**fields)                                        # outside a request (scripts, cron)
```

**The full event vocabulary, severities, and field conventions live in `athena/OBSERVABILITY.md`** — extend that registry when adding events.

### `utils/tracing_setup.py`

OpenTelemetry tracing (api/sdk 1.44.0, instrumentation 0.65b0). `init_app(app)` (called from `create_app` **before** `init_logging` so the OTel middleware wraps the WSGI app first — the **portail** service does the same in `client/app.py`) exports **over OTLP/gRPC to `telemetry.googleapis.com`** in production (10% sampling, `ParentBased(TraceIdRatioBased)`; override via `TRACE_SAMPLE_RATIO`) and to the console in dev. Auto-instruments Flask, `requests`, and Jinja2. Three PII layers keep query strings, storage paths, and emails/phones out of exported spans (instrumentation hooks, `_SanitizingSpanExporter`, manual-span guard). **Every failure in this module is fail-open** — init errors become a `logger.warning` and the app boots with tracing silently off — so `tests/test_tracing_setup.py` asserts the *absence of warnings*, not the absence of exceptions. Since 2026-07-30 it also carries the suite's first test of a real export path (`test_otlp_export_reaches_a_grpc_server`: a live local gRPC server, asserting on the received OTLP payload). Manual API: `span("name", **attrs)` context manager, `add_attributes(**attrs)` (enrich current request span), `firestore_span(op, collection, doc_id=...)`, `@traced()` decorator. Span-name and attribute conventions are in `athena/OBSERVABILITY.md`.

---

## DAV Protocol Layer

### URL structure (post-Phase D1)

```
/dav/                                # Principal + addressbook/calendar home-set
├── addressbook/                     # CardDAV — contacts
├── general/                         # « Général » — VEVENT + VTODO + VJOURNAL
│                                    #   for every item with NO dossier
├── dossier-{dossier1Id}/            # Per-dossier: VTODO + VJOURNAL
│   ├── {taskId}.ics                # VTODO
│   └── {noteId}.ics                # VJOURNAL
├── dossier-{dossier2Id}/
└── ...
```

Only dossiers with status `actif` or `en_attente` appear in PROPFIND Depth:1 at root. Closed/archived dossiers are hidden — DavX5 stops syncing them. Reopening a dossier makes it reappear on next sync discovery.

### Why per-dossier collections (Phase D1 rationale)

Pre-D1, dossiers were exposed as VJOURNAL entries at `/dav/journals/`. This misused RFC 5545: VJOURNAL is for timestamped records (meeting notes, research), not for containers. Dossiers are naturally CalDAV **collections** that group related tasks and notes. Per-dossier collections also enable jtx Board to render RFC 5545 RELATED-TO relationships as visual parent-child hierarchies (Phase D3).

### CTag/ETag sync model

- Each collection has a `ctag` stored at `dav_sync/{collectionName}/` — changes whenever any resource in it is created, updated, or deleted.
- Each resource has an `etag` (UUIDv4) regenerated on every write — used for `If-Match` conditional updates.
- Collection names: `"parties"`, `"hearings"`, `"tasks"` (standalone), `"dossier:{dossierId}"`.
- `sync_token` currently mirrors `ctag` (same UUID string). `dav_sync/{collectionName}/tombstones/{resourceId}` stores deletion records consumed by sync-collection REPORT responses.

### `dav/sync.py` API

```python
get_ctag(collection_name: str) -> str
get_ctags_bulk(names: list[str]) -> dict[str, str]   # single db.get_all read (root PROPFIND)
get_sync_token(collection_name: str) -> str   # currently returns the ctag
bump_ctag(collection_name: str) -> str
record_tombstone(collection_name: str, resource_id: str) -> None
remove_tombstone(collection_name: str, resource_id: str) -> None  # call when a resource (re)enters a collection
get_tombstones(collection_name: str, since_token: Optional[str] = None) -> list[dict]  # prunes tombstones older than TOMBSTONE_TTL_DAYS (30) while streaming
record_tombstones_bulk(collection_name: str, resource_ids: list[str]) -> None  # ONE get_ctag + chunked db.batch() at _BATCH_CHUNK=450
bump_ctag_in_batch(batch, collection_name: str) -> str                         # stage a bump into a CALLER-owned batch
record_tombstones_in_batch(batch, collection_name, resource_ids, sync_token) -> None
clear_tombstones(collection_name: str) -> None
delete_sync_state(collection_name: str) -> None   # removes the dav_sync doc (run clear_tombstones first)
```

> **Bulk primitives (août 2026).** `record_tombstone` appelle `get_ctag` en ligne, soit **DEUX allers-retours sérialisés par ressource** : drainer un dossier chargé — ou supprimer une série — de cette façon marche droit dans le délai de 60 s de gunicorn, et un SIGKILL là est irrécupérable (les ressources ont quitté la collection, le CTag n'a jamais été bumpé, et plus rien ne dit à DavX5 d'aller voir). `record_tombstones_bulk` fait une lecture et `ceil(N/450)` commits ; il **propage** ses échecs.
>
> **Dérogation documentée à la règle « le bump vit dans la route ».** Une écriture en lot met son bump **DANS le lot** (`bump_ctag_in_batch`), parce que commiter puis bumper laisse N audiences vivantes et visibles dans l'application que **DavX5 ne resynchronise JAMAIS** (`_handle_sync_collection` court-circuite sur un jeton inchangé). Les lots Firestore traversent les collections, donc création = `N+1` opérations et suppression = `2N+1`. C'est la règle que la doctrine protège, servie autrement — et non son contournement.

Call sites that must bump CTags:
- All `parties` CRUD → `bump_ctag("parties")`
- All `hearings` CRUD → `bump_ctag("hearings")`
- `tasks` CRUD → `bump_ctag(f"dossier:{dossier_id}")` if linked, else `bump_ctag("tasks")`
- Task dossier reassignment → tombstone + bump for OLD collection (incl. the standalone `tasks` collection), `remove_tombstone` + bump for NEW
- `notes` CRUD → `bump_ctag(f"dossier:{dossier_id}")`; delete → `record_tombstone` + bump; dossier reassignment → tombstone + bump for OLD collection, `remove_tombstone` + bump for NEW (same shape as tasks — added July 2026; a bare bump on delete left the note on the phone forever)
- `protocol._auto_create_tasks_for_steps` → bump per task created
- All DAV PUT/DELETE handlers already bump their own CTag
- Dossier deletion → `clear_tombstones(f"dossier:{id}")` + `delete_sync_state(f"dossier:{id}")` (no `"dossiers"` sync collection exists post-D1)

Sync hygiene rules:
- Tombstones are pruned after 30 days (`TOMBSTONE_TTL_DAYS`); sync-collection REPORT builders skip any tombstone whose id matches a live resource (RFC 6578: one response per href).
- `/dav/tasks/` per-resource handlers 404 on dossier-linked tasks, and PUT forces `dossier_id=None` (the payload's `X-PALLAS-DOSSIER-ID` is ignored — the URL determines the collection).
- DAV XML bodies (PROPFIND/REPORT) are capped at 512 KB (`xml_utils`), DAV request bodies at 5 MB (`security.py`).

### Authentication

`dav/dav_auth.py` implements `@dav_auth_required`:
- Accepts HTTP Basic Auth
- Compares username against `DAV_USERNAME` (defaults to `AUTHORIZED_USER_EMAIL`)
- Compares password against `DAV_PASSWORD_HASH` (bcrypt)
- On failure: `401` + `WWW-Authenticate: Basic realm="Pallas Athena"`
- Brute-force brake: per-IP failure tracker (10 failures / 15 min → 429 + Retry-After, before bcrypt runs), fail-fast on malformed/oversized credentials, and a 5-minute success cache so DavX5 polls don't re-run bcrypt (keyed by HMAC-SHA-256 of the credentials under an ephemeral per-process random key — never a plain hash of the password). In-memory per instance — a brake, not a guarantee.
- All DAV blueprints are CSRF-exempt (`csrf.exempt(...)` in `main.py`)

### DavX5 compatibility notes

- DavX5 discovers via `/.well-known/carddav` and `/.well-known/caldav` — both 301 to `/dav/`.
- PROPFIND Depth:1 on `/dav/` lists every advertised collection (static + per-dossier) — DavX5 won't find nested URLs.
- Must handle `If-Match` and `If-None-Match` headers on PUT/DELETE.
- `Content-Type` must be exact: `text/vcard; charset=utf-8` for CardDAV, `text/calendar; charset=utf-8` for CalDAV.
- Honor `Prefer: return=minimal` by omitting bodies on successful writes.
- Harmless error: DavX5 SQLite foreign-key errors when a dossier collection disappears (closed/archived dossier) — client-side race, safe to ignore.
- Post-D1 migration required: users must **remove and re-add** the DavX5 account after deploying D1.
- **Same for the July 2026 hearings split AND the « Général » collection** (ship together, ONE account reset) (dossier-linked hearings moved out of `/dav/calendar/` into `/dav/dossier-{id}/`): **remove and re-add the DavX5 account.** A relocation cannot be expressed implicitly in this sync model — sync tokens are non-monotonic UUIDs, and a client that never receives a tombstone keeps its local copy — so the account reset is the guaranteed convergence path (user decision 2026-07-23; deliberately no migration script). Each active dossier then appears in DavX5 **both** as a calendar (VEVENT) and as a jtx list (VTODO/VJOURNAL); tick each where you want it.

---

## Scripts

Run with `python -m scripts.<name>` from the `athena/` directory.

### `scripts/seed_reference_data.py` (Phase G)

Mirrors `ref_greffes` (56), `ref_juridictions` (27), `ref_palais` (51) and `ref_forums` (20) into Firestore **from the in-memory tables in `models/reference.py`** (imported, not re-listed — the old duplicated literals had already drifted). Idempotent — overwrites documents. **Nothing reads these collections**; the app reads the in-memory tables, so a data fix means editing `models/reference.py`, and re-seeding is optional housekeeping for the eventual admin UI.

---

## Domain Logic Reference

### Quebec taxes

- **GST (TPS)**: 5% on taxable amounts
- **QST (TVQ)**: 9.975% on taxable amounts — **NOT compounded on GST** (this changed in 2013; an older implementation computed QST on (subtotal + GST), that's incorrect)
- Use `Decimal` for tax intermediates, convert to int cents with `ROUND_HALF_UP` before storage

### Quebec judicial deadlines

See `utils/deadlines.py` above. Key rule: direction of computation matters. Forward deadlines (e.g., "15 days after service") landing on weekend/holiday → push forward. Backward deadlines (e.g., "10 days before hearing") landing on weekend/holiday → push backward (earlier).

### Protocol types

**Cour du Québec — Procédure simplifiée (`cq_simplifié`)**: prescriptive, deadlines fixed by C.p.c. Steps auto-generated with `mandatory=True, deadline_locked=True`. User can add supplementary steps but can't delete/edit mandatory ones. Start-date change auto-recomputes all deadlines.

**Cour supérieure — Procédure ordinaire (`cs_ordinaire`)**: suggestive template. `mandatory=True, deadline_locked=False`. Default offsets pre-fill suggested dates (displayed with "À modifier" badge until `date_confirmed=True`) but user sets real dates. Full editability.

**Conventionnel (`conventionnel`)**: blank template. User creates all steps. `mandatory=False, deadline_locked=False`. For appeals, tribunals, arbitration, mediation, any non-standard context.

A dossier may have **multiple protocols sequentially** but only one `actif` at a time. Completed/suspended protocols appear in a collapsible "Protocoles antérieurs" section.

### Bidirectional task-protocol sync

Both directions sync status changes between a task and its linked protocol step:
- Step completed → task marked `terminée`
- Task completed → step marked `complété`
- Task reopened → step reverted to `à_venir`
- Step reopened → task reverted to `à_faire`

Implemented via two helpers: `_sync_task_status` in `protocol.py`, `_sync_protocol_step` in `task.py`. Both use a module-level `_SYNCING: set[str]` guard to prevent infinite recursion. Cross-protocol search iterates active protocols (tractable for single-user dataset size).

### Court file number parsing

Format: `NNN-NN-NNNNNN-NN` (e.g., `500-05-123456-241`)
- Positions 1–3: greffe number (courthouse + judicial district)
- Positions 5–6: jurisdiction number (tribunal + competence)
- Remaining: sequence number + check digits (not parsed)

Letters prefix (e.g., `TAL-...`, `TAQ-...`) → administrative tribunal, no parsing, `is_administrative_tribunal=True`.

Shared greffe numbers (multiple locations): `614`, `635`, `640`, `652` — stored with `other_locations` array. The `point_de_service=True` flag marks itinerant points of service.

Auto-populated fields on dossier (`tribunal`, `competence`, `palais_de_justice`, `district_judiciaire`) remain user-editable after parsing.

**Forum droplist (July 2026; replaced the checkbox late July).** The parser only resolves Québec judicial-court numbers (`NNN-NN-…`). The dossier form's « Dossier judiciaire » section has a four-way « Forum » select (`forum_type`): **Tribunal de droit commun** — parser active on blur, parsed-fields grid shown; **Tribunal administratif** / **Cour ou tribunal fédéral** — a per-category picker of the `reference._FORUMS` bodies appears (two `<select name="forum">`, only the active one enabled so exactly one submits); picking one writes its name into `tribunal`, clears the judicial-only fields, and the court file number is stored **verbatim, unparsed**; **Préjudiciaire** — nothing filed yet: the file-number input is hidden/disabled, a manual « District judiciaire » input is the only entry, and the server forces `court_file_number = "Préjudiciaire"` so `{{dossier.numero_cour}}` fills — switching back to droit commun auto-clears the placeholder string so the real number can be parsed (which crushes the préjudiciaire values). Server-side reconciliation is `models/dossier.normalize_forum` (authoritative over the JS state). The detail page's « Juridiction » card shows a « Forum » row (the `FORUM_TYPE_LABELS` label) whenever `forum_type != "judiciaire"`. See the reference `_FORUMS` table above.

### Court locations & addresses (July 2026)

`models/reference._PALAIS` holds the MJQ civic address of each of the **43 palais de justice + 8 points de service de justice**, keyed by slug; each greffe carries a `palais_key` into it, so a parsed court file number resolves to a street address via `get_greffe_address(greffe_number)`. Data only for now — the two consumers are **planned, not built**: (i) auto-filling a hearing's address when it sits at a courthouse, (ii) filling the clerk's address on a notice of presentation. **No gabarit placeholder and no MCP field exposes this yet.**

Two traps in this data:
- **`point_de_service` means two different things.** The greffe-level flag marks the four **itinerant circuit greffes** (614/635/640/652); `ref_palais.location_type == "point_de_service"` marks the eight **MJQ points de service de justice** (Amqui, Carleton-sur-Mer, Dolbeau-Mistassini, Forestville, Gaspé, La Sarre, Matane, Sainte-Anne-des-Monts) — all of which the greffe table flags `False`. The two disagree **by design**; don't conflate or "fix" one to match the other.
- **A courthouse name is not its city.** Chicoutimi is in Saguenay; Havre-Aubert is in Les Îles-de-la-Madeleine. Address the `city` field, title the `name` field.

### Contact roles and the "everyone is a partie" model

All contacts live in a single `parties` collection with a `contact_role` discriminator: clients, opposing parties, opposing counsel, experts, witnesses, bailiffs, notaries, others. KYC/compliance fields are only shown when `contact_role == "client"`.

Dossiers reference parties via `clients[]` and `opposing_parties[]` arrays of `{id, name}`. Opposing counsel today is captured by adding a partie with `contact_role="avocat_adverse"` to `opposing_parties`.

### Phone number handling

- Stored: E.164 (e.g., `+15145551234`)
- Displayed: `{{ phone|phone }}` filter → `+1 (514) 555-1234`
- `tel:` links: use raw E.164 value
- 7-digit input defaults to +1514 (Montreal area code)
- International numbers preserved as-is if prefixed with `+`

### Markdown in notes

Note content is stored as Markdown. Rendered via `markdown.markdown(content, extensions=["tables", "fenced_code", "nl2br"])` then sanitized via `bleach.clean()` against a fixed allowlist (`_ALLOWED_TAGS`, `_ALLOWED_ATTRS` in `main.py`). Registered as a Jinja filter: `{{ note.content | markdown | safe }}`. Truncated preview snippets on list pages do NOT render Markdown (shown as plain text).

---

## Known Gotchas

- **Firestore `!=` + `orderBy`**: cannot combine `!=` with `order_by` on a different field. Design queries accordingly; fall back to client-side filtering for small result sets.
- **Firestore is flat, not user-scoped.** Despite the single-user design, collections live at the root (`/parties`, `/dossiers`, …), not under `/users/{userId}/...`. Queries that assume nesting will fail. Storage paths, by contrast, **do** use `/users/{userId}/...`.
- **App Engine Standard filesystem**: read-only except `/tmp`. All persistent file storage goes through Firebase Storage.
- **DavX5 strictness**: partial DAV compliance causes silent sync failures. Test every endpoint with `curl` before testing with DavX5.
- **Canadian postal code format**: `A1A 1A1` (letter-digit-letter **space** digit-letter-digit) — normalize always.
- **`normalize_email` has NO regex any more — that was the ReDoS fix, and a length bound alone did not close it.** The old `^[^@\s]+@[^@\s]+\.[^@\s]+$` was **quadratic**: both classes after the `@` match the dot, so on `x@` + `a.`×n + `@` the engine tried every split point and rescanned the tail — 3.8 s at 40 KB, ~96 min at the portal's 1 MB body cap, and `re` holds the GIL, so ONE request froze the whole gunicorn worker (all 4 threads) until the 60 s timeout. The portal's **public, unauthenticated** `POST /api/renvoi` passes a JSON field straight in (App Check + 5/h per IP are the only other gates). A `len() > 254` guard was added first and **CodeQL kept the alert open** — it does not model a length check as a sanitizer, and it simply re-anchored `py/polynomial-redos` onto the `match()` line (alert 312 → 318). The pattern is therefore **gone**: `normalize_email` now partitions on `@` and checks `"." in domain[1:-1]`, which recognises the identical language in one linear pass (equivalence pinned by `test_email_shape_matches_the_historical_regex`, verified out of band over 400 k random strings across 16 Unicode whitespace classes). `EMAIL_MAX_LENGTH = 254` stays as RFC-correct defence in depth. One deliberate tightening: Python's `$` also matches **just before a trailing newline**, so the old pattern accepted `a@b.c\n` — unreachable through `.strip()`, but pinned refused now. Lesson worth generalising: **a guard CodeQL cannot see leaves the alert open forever; removing the vulnerable construct is what closes it.** The same trap sits latent in `logging_setup.EMAIL_RE` (still quadratic, and unanchored), safe **only** because `_redact_string`'s `MAX_LEN = 2048` early return precedes it — it is now the last of its family in the repo, so keep that ordering.
- **Tests parse XML with `defusedxml`, not `xml.etree.ElementTree`.** Bandit flags every `ET.fromstring` as B314, and eight such alerts had accumulated as one-by-one dismissals before the tests were switched (2026-07-30) — each new well-formedness assertion filed a fresh one. `defusedxml` is already a pinned direct dependency and `dav/xml_utils.py` already uses it, so `from defusedxml import ElementTree as ET` costs nothing and ends the recurring triage tax.
- **Easter is floating**: implement Meeus/Jones/Butcher algorithm; do not hardcode dates.
- **`_SYNCING` guard required** for bidirectional task↔protocol sync to terminate. Each module has its own set; never skip the guard.
- **Per-dossier CTag naming**: use `dossier:{id}` (with colon — valid in Firestore document IDs).
- **DAV collections must be direct children of `/dav/`**: nested URLs (`/dav/dossiers/{id}/`) won't be discovered by DavX5's Depth:1 PROPFIND.
- **`/dav/journals/` is gone** (post-D1). DavX5 accounts must be removed and re-added.
- **VTODO→VJOURNAL RELATED-TO works** in jtx Board only when both components are in the **same** CalDAV collection — which they are after D1.
- **CSV BOM**: prepend `﻿` to CSV output or Excel mangles French accents.
- **`reportlab` only for PDFs** (pure Python). `weasyprint` requires cairo/pango system libs unavailable on App Engine Standard.
- **Task `dav_href` field is stale post-D1** — tasks with a dossier are served from per-dossier collections, computed dynamically. Ignore the stored `dav_href` on those.
- **Closed dossiers' DAV collections disappear** — DavX5 may throw a harmless SQLite FK error. Safe to ignore (client-side race).
- **`markdown` filter applied twice** renders nothing — only apply it on the full detail view, never on preview snippets.
- **QST is NOT compounded on GST** (since 2013). Apply both to taxable subtotal independently.
- **Decimal for money math, int cents for storage** — never mix Decimal and float in tax calculations.
- **App Check + Phone MFA** can lock out the user if the phone is lost. Keep Firebase console access as a fallback.
- **CSP is ENFORCED with a per-request nonce** (since 2026-07-11; flipped after a 90-day report-only `/csp-report` window where only `script-src` reported, then hardened the same day). `script-src` is `'self' 'nonce-<per-request>' 'unsafe-eval'` + the Google reCAPTCHA origins — **no `'unsafe-inline'`, no `ajax.cloudflare.com`** (see `build_csp` in `security.py`); the app's inline `<script>`s carry `nonce="{{ csp_nonce }}"` and an un-nonced/injected inline script is **blocked**, while inline `on*` handlers were moved to `data-` attributes + `addEventListener`. `'unsafe-eval'` (Alpine `new Function()`) and `style-src 'unsafe-inline'` (reCAPTCHA) remain as documented necessities. `report-uri` stays active, so violations are still collected under enforcement. Rocket Loader is disabled at the edge.
- **Documents blueprint isn't nested under dossiers.** Routes live at `/documents/...` and the dossier scope is passed as `?dossier_id=…` (GET) or as a form field (POST). When linking from a dossier tab, always include `dossier_id` in the URL.
- **Hearings prefix is `/audiences`**, not `/agenda`. Internal `url_for()` calls must use the `hearings.*` blueprint.
- **Dossier `clients` and `opposing_parties` are arrays**, not single FKs. Code reading legacy `client_id` must go through `_migrate_parties` (already applied in `get_dossier`/`list_dossiers`).
- **`dossier.role` is DERIVED — never edit it by hand** (July 2026). The source of truth is `clients[].roles`; `_derive_role` recomputes the dossier-level field on every save (first role of the first client that has one), so a hand edit is silently overwritten. It exists only for the gabarits: `{{dossier.role}}`, `role_feminin`, `role_label`, and the demandeur/défendeur intitulé positions. Those positions resolve ONLY for plain demandeur/défendeur — a « demandeur reconventionnel » leaves `{{dossier.demandeur}}`/`{{…defendeur}}` unresolved, which is the documented §6.2 semantics for non-binary roles, not a bug. The legacy dossier-level role is seeded into `clients[0].roles` on read (once); `utils/template_fields.py`'s `_ROLE_LABEL`/`_ROLE_FEMININ` mirror `PARTY_ROLE_LABELS` by hand and are pinned equal by test — « autre » deliberately has no feminine form.
- **The taxonomy SUGGESTS a délai; it never sets one.** `taxonomie.Action.prescription_type` prefills the Prescription dropdown **only on a user action-change** — never on load, or opening an existing dossier would silently overwrite the delay the lawyer confirmed. It is `""` wherever the source's delay is not a single clean period, and those `""`s are load-bearing, not gaps: **FAI-01**'s « 6 mois » is a *retrospective eligibility window* (the acte de faillite must fall in the 6 months **preceding** the application), so suggesting it would compute a deadline that means nothing; RCV-05/COR-06 differ by regime; CJP-* are « délai raisonnable ». Never "fill in" a blank `prescription_type` without re-reading the source row.
- **A `-99` « Autre (préciser) » row must never carry a délai.** Every domaine ends with one so no file is unclassifiable; they have no delay of their own, and the domaine's default (e.g. RES's « 3 ans (art. 2925) ») is **not** theirs to inherit — `action_precision` is where the real object goes.
- **`delai_types` is a tuple over a closed 11-token vocabulary; annex asymmetries are deliberate.** Tokens combine (`("PE","A")`), `D` (stricte, rouge) outranks `DR` (relevable, orange) in `niveau_decheance`, and the legal content of `taxonomie.py` changes **only on an approved spec**. Do not "fix" COR-11 (token `A`, `avis == ()`) or RCV-03 (two conditional avis, typed `(PE,)`) — both are binding annex content, pinned by `test_a_token_and_avis_sets_are_pinned`. The six pinned `ref_delai == ""` rows (CST-05, COR-04, COR-09, FAM-01, FAM-02, FAM-06) are equally deliberate — no statutory delay source exists, and inventing one would derive legal content.
- **§ 4's déchéance list is a cross-section claim.** `niveau_decheance` derives from the `delai_types` tokens, but **APP-01** states « déchéance expresse » only in prose — a per-section reader cannot catch that. `tests/test_taxonomie.py::test_section_4_decheances_all_carry_D` pins the § 4 cross-section as `stricte`; extend it when the source changes.
- **Prescription has TWO layers: the raw `prescription_date` (provenance, NEVER recomputed from events) and the derived projection from `derive_prescription(doc)` — and every consumer must read the SAME seam.** `models/dossier.derive_prescription` is the one derivation (status ∈ courante | interrompue | echue | imprescriptible | a_verifier + `date_effective`), consumed by `list_prescription_alerts` (dashboard **and** MCP `get_agenda`), `routes/dossiers._attach_prescription_warnings` (list pastille + card colour) and the MCP `get_dossier` — the three-surface parity rule: a dossier silenced or shifted on one surface must read identically on all, or Claude warns about a limitation period that no longer runs (advice that is actively wrong) while the dashboard stays quiet, or vice versa. An `interruption_depot` event (or the legacy `prise_action_date`, folded in at READ as an implicit depot — no storage migration) silences the alert with `date_effective: None` (art. 2896 — the interruption lasts until judgment; computing a date would be inventing one). Alert filtering stays **in PYTHON over the bounded raw-date query**, for three reasons that each fail SILENTLY: a Firestore equality on `None` does not match ABSENT keys (every pre-existing dossier — the fields are additive, no migration); a new `.where()` would need a composite index whose build window degrades the query to an **empty list**, the worst failure mode for a limitation deadline; and events only push the effective date LATER, so the server query on the raw date **over-fetches, never under-fetches** — re-basing the window server-side on a derived date would require materializing it, which the raw-date provenance rule forbids. The « result window full » warning is computed on the RAW count, before filtering: a silenced dossier still consumes one of the 50 slots, so a full window still means real alerts are hidden beyond it.
- **`date_avis` is manual, never derived.** Each avis has its own factual starting point (délivrance du bien, cause d'action…), which is NOT `droit_action_date` — deriving the date would silently compute from the wrong start. The form shows the action's structured avis as the suggestion; the lawyer confirms by filling the field. `compute_echeances` dates an avis only from an explicit `date_depart_avis` + a confirmed scenario (`avis_confirmes`).
- **Direct App Engine access is blocked at three layers** (App Engine firewall → Cloudflare IPs only, `X-Origin-Auth` origin secret, appspot Host check). When debugging, hit the Cloudflare hostname — `gcloud app browse` will 403. New App Engine internal endpoints (cron, queues) must be under `/_ah/` or they'll be rejected by the origin checks. **Layer 2 is off in production** (see the Security Rules entry) — treat the count as two until it is armed.
- **Arming the origin secret is EDGE-FIRST, and a trailing newline in the secret is site-wide downtime.** Two traps, both silent, both discovered 2026-08-11. **(a) Order.** Creating `cf-origin-secret` before the Cloudflare Transform Rule exists arms the app against a header nobody sends: with `min_instances: 0` the instances recycle on their own, so the site starts answering **403 everywhere** without any deploy. The reverse order is harmless — no secret means `security.py`'s `if not secret: return None`, and the injected header is simply ignored. Prove the rule fires before creating the secret with Cloudflare's request tracer (`POST /accounts/{id}/request-tracer/trace`); do not infer it. **(b) Bytes.** `gcloud secrets create --data-file=-` stores the payload verbatim and **nothing in `config.py` strips it**, so `python -c "…print(…)"` bakes in a `\n` that `hmac.compare_digest` will never match — and a Transform Rule cannot emit a newline, so the edge cannot be corrected to compensate. `DEPLOYMENT.md`'s recipe carried exactly that defect until it was fixed with `| tr -d '\n'`; `dav-password-hash` has the same trap (a 61-byte bcrypt hash never matches the 60 `checkpw` recomputes, and DavX5 then fails silently). Verify with `gcloud secrets versions access latest --secret=<id> | xxd | tail -1` — no trailing `0a`. Rollback is **not** a config flip: the value is read once at class-body evaluation and `lru_cache`d per gunicorn worker, so disabling means a new EMPTY secret version **plus** a redeploy. Also note the Transform Rule must be **zone-wide**: `_enforce_origin_secret` exempts only `/_ah/*` and the `X-AppEngine-*` dispatch headers, so a path-scoped rule would 403 `/dav/`, `/mcp`, `/oauth/*`, `/auth/*`, `/csp-report` and `/.well-known/*`, and unrouted URLs answer 403 instead of 404.
- **`requirements.txt` is generated — never hand-edit it.** Change `requirements.in`, then re-lock with `uv pip compile` (recipe in the Tech Stack section). Production pip runs with `--require-hashes --no-deps`, so an unhashed edit simply won't deploy.
- **The OTel packages move as ONE atomic edit, and `opentelemetry-resourcedetector-gcp` 1.13.0 is YANKED.** api/sdk (1.44.0) and contrib instrumentation (0.65b0) are paired lines, and each instrumentation package hard-pins its siblings at `==0.65b0` — bumping one alone is unresolvable, so Dependabot cannot do it and it must be a hand-authored commit. The resource detector moved repositories at 1.13.0 with a wrong namespace ("breaks imports", yanked); 1.14.0 restored it, hence the explicit pin. Landing on 1.13.0 would fail an import that `tracing_setup`'s `except Exception` swallows — **trace export would silently stop**. The old `setuptools<81` constraint is gone (contrib dropped `pkg_resources` at 0.49b0); do not reintroduce one without re-reading why it existed.
- **The OTLP exporter must be authenticated with `credentials=`, NEVER `headers=` — the difference is a one-hour cliff.** `OTLPSpanExporter` freezes `headers=` at construction (`self._headers = tuple(...)`, reused verbatim on every `Export`), so a Bearer token placed there expires in ~1 h; `UNAUTHENTICATED` is **not** in the exporter's retryable codes, so export stops dead while the app keeps serving normally — the silent-failure mode this component collects. `credentials=` (built by `create_google_grpc_credentials()`) wires an `AuthMetadataPlugin` that gRPC invokes on **every** RPC, re-minting the token. It is also why the transport is gRPC and not HTTP: `credentials=` does not exist on the HTTP exporter. Two companions: **`gcp.project_id` is the routing key** and is set by hand in `_build_resource()` (the GCP detector emits `cloud.account.id`, never `gcp.project_id` — `MIGRATION.md` is wrong, and the behaviour when it is missing is documented nowhere); and **`cloudtrace.googleapis.com` must stay enabled** beside `telemetry.googleapis.com`, or Observability discards the spans silently.
- **A guard the scanner cannot see leaves an alert open forever — and CodeQL taught us that twice.** A `len() > N → return` in front of a quadratic regex is a real mitigation but not a *sanitizer* CodeQL models, so `py/polynomial-redos` simply re-anchored onto the next line (alert 312 → 318). Removing the vulnerable construct is what closes an alert; suppressing is for advisories whose code path is genuinely unreachable (and then on every surface separately — `osv-scanner.toml` silences OSV-Scanner ONLY; Dependabot never reads it and Trivy files its own alert regardless of its severity filter).
- **Exact-pin dependencies (`==X.Y.Z`)** in `requirements.in` — wildcard pins (`==X.*`) break OSV-Scanner's version resolution and produce false-positive CVE reports.
- **Composite indexes must be deployed BEFORE (or with) code that queries them** — `firebase deploy --only firestore:indexes --project athena-pallas`. Until an index builds, the affected queries fail and views gracefully degrade to empty lists. Every new `.where()+.order_by()` combo or filtered aggregation needs an entry in `firestore.indexes.json`.
- **An index that serves a paginated list does NOT serve its SUM aggregation.** Firestore matches SUM/AVG queries only against an index whose *trailing* fields are the aggregated fields in **alphabetical order** (`amount` before `hours`), with directions **matching the query's last sort** (ASC for equality-only queries; DESC after `date DESC, id DESC`). A same-fields index in the wrong tail order is ignored — the query 400s ("requires an index") even though the index is READY, and totals silently degrade to zero (June 2026 dashboard "heures non facturées" incident).
- **Never edit a `static/vendor/` file in place** — they're cached `immutable` for a year. A changed asset gets a new version/hash filename, plus updates to the templates that reference it, the precache list in `static/sw.js`, and the Early Hints lists in `security.py`.
- **Script order at the end of `<body>` is load-bearing** (App Check boot → page scripts → htmx → Alpine). Execution follows document order — the Firebase/App Check boot scripts run synchronously at parse time, and the vendored htmx/Alpine `defer` scripts run in document order at `DOMContentLoaded`; position, not a sync/defer phase, is the guarantee. (Rocket Loader, which used to defer all scripts while preserving that order, was disabled at the edge on 2026-07-11.) Moving htmx above the boot reopens a race where `hx-trigger="load"` requests fire without the `X-Firebase-AppCheck` header and 401; moving Alpine above inline component definitions breaks `x-data` evaluation.
- **MCP output: date-only fields must never pass through `to_mtl`.** Fields stored as midnight UTC (`timeentries.date`, `expenses.date`, invoice `date`/`due_date`, task `due_date`, protocol `start_date`/`end_date`/step `deadline_date`, dossier `opened_date`/`closed_date`/`prescription_date`/`droit_action_date`/`date_avis`) are emitted as the **UTC calendar date** via `mcp.tools.date_str` — a Montréal conversion shifts them to the previous day. True timestamps go through `mcp.tools.iso_mtl`.
- **Collection display names are short, and built in ONE place.** `dav/dossier_collections.collection_display_name()` is the single source of truth, called by both the root Depth:1 listing (`dav/__init__.py`) and the collection's own PROPFIND — they used to build the label separately and had drifted (the root prefixed « Pallas Athena — », the collection did not), so what a client showed depended on which response it read last. A dossier is **« N/R : 2026-001 »** (*notre référence*; the title is dropped — DavX5 truncates mid-word in its collection list and in the Android calendar name), falling back to the title when there is no file number. The addressbook is **« Clients et parties impliqués »** (`carddav.ADDRESSBOOK_DISPLAY_NAME`) and the general collection **« Général »**. No « Pallas Athena — » prefix anywhere: DavX5 shows the account name above the list already. Renaming a collection does NOT need an account re-add — DavX5 picks it up on « Refresh collection list ».
- **« Général » is a real DAV collection, not a UI label** (July 2026). `/dav/general/` carries every hearing, task **and note** with no dossier, and is served by the SAME code as a dossier collection — `dav/dossier_collections.py` is written against a *scope*, a dossier id where `""` means Général (`_href_prefix`, `_resolve_scope`). Sharing the implementation is the point: comp-filter, tombstones and the "the URL decides the dossier" rule cannot drift. It replaced `/dav/calendar/` + `/dav/tasks/`, both **removed**. It has no lifecycle, so it is never drained the way a closed dossier is.
- **`dav.sync.collection_for(dossier_id)` is the ONE routing rule for a CTag bump.** Tasks store `None` for "no dossier", notes and hearings `""` — both falsy, so no migration was needed. Every write path goes through it (`routes/notes.py`, `routes/tasks.py`, `routes/hearings.py`, `models/protocol.py`, `dav/dossier_collections.py`, `mcp/handlers.py`). The old `routes/notes.py` bumped only `if note.get("dossier_id")`; a dossier-less note would have been written, shown in the app, and **never synced**.
- **A note's `dossier_id` is optional since July 2026, which removed a guard.** `models/note._validate` no longer requires one, so blanking an *unresolvable* id — which `routes/notes._enrich_dossier_info` used to do — now silently files a dossier note under « Général » instead of erroring. That helper returns `(data, errors)` and **refuses** an id that does not resolve; `mcp/handlers.create_note` does the same, and its tool description tells the model never to omit `dossier_id` as a fallback for "I couldn't find the dossier".
- **A dossier-linked hearing lives in ONE collection — `dossier:{id}`, never `hearings`** (July 2026). `/dav/calendar/` now serves only hearings with an empty `dossier_id`, exactly as `/dav/tasks/` serves only dossier-less tasks; `dav/caldav.py` funnels every listing through `_standalone_hearings()` and every per-resource lookup through `_standalone_or_404()`. Serving the same hearing from both would make DavX5 import the court date twice, and a PUT/DELETE through the wrong collection bumps a CTag the other never watches. `routes/hearings.py._sync_name()` picks the collection for every web-UI write, and the update path tombstones the OLD collection + `remove_tombstone`s the NEW one when the dossier changes (the shape `routes/tasks.py` uses). **Closing a dossier drains its hearings too** — `_sync_dossier_dav_visibility` tombstones tasks, notes *and* hearings, or stale court dates sit on the phone forever once the collection stops being advertised.
- **The per-dossier collection is mixed-component, so `calendar-query` MUST honor `comp-filter`.** `DOSSIER_COMPONENTS` (in `dav/dossier_collections.py`) is the single source of truth, imported by `dav/__init__.py` for the root Depth:1 listing — two hard-coded literals that disagree mean discovery promises a capability the collection then denies. `requested_components()` parses the RFC 4791 §9.7.1 nesting and **degrades to "return everything" on an absent or malformed filter, never to empty** (an empty collection reads to the client as "all deleted"). `sync-collection` is structurally component-blind (RFC 6578 defines no filter), so it reports every member and the client routes by component after fetching bodies — that is expected, not a bug.
- **`hearing_to_vevent` emitted neither `DTSTAMP` nor `CREATED` until July 2026.** DTSTAMP is mandatory (RFC 5545 §3.6.1); the omission slid because the Android calendar provider tolerates it. `CREATED` is the same jtx Board `icalobject.created` NOT-NULL trap documented for VJOURNAL, and it becomes reachable the moment a VEVENT enters a per-dossier collection jtx also subscribes to. Likewise `create_hearing` **ignored a caller-supplied `id`/`vevent_uid`** and minted fresh ones, so a CalDAV PUT stored the event under an id the client never learns — every later GET of that href 404s while a duplicate syncs down. Both fixed; `tests/test_dav_hearings.py` pins them.
- **A hearing's video link is dropped by the Android calendar unless it is in the DESCRIPTION.** VEVENTs sync to the device **calendar** (Google Calendar via DavX5), NOT jtx Board (jtx only imports VTODO/VJOURNAL). Android's `CalendarContract` has no conferencing field, so DavX5/ical4android **discards the RFC 7986 `CONFERENCE` property** — Google Calendar never shows it (confirmed on a Pixel 10 Pro, 2026-07-24). `hearing_to_vevent` therefore also emits the URL as a `Visioconférence: <url>` line in DESCRIPTION (Google Calendar renders a bare URL as a tappable link); `CONFERENCE` is kept only for standards-aware clients. In DESCRIPTION (a TEXT property) the URL's `,`/`;` are wire-escaped and the client unescapes them — that's correct, distinct from the `CONFERENCE` URI property which stays raw.
- **`serie_id == ""` est une VALEUR STOCKÉE, pas une sentinelle — et le déclencheur ne demande aucun attaquant.** Une égalité Firestore `where("serie_id", "==", "")` ramène **toute audience autonome du cabinet**. « Détacher » pose précisément `serie_id = ""`, et un onglet resté ouvert — un second onglet, le bouton Précédent — affiche encore « Cette occurrence et les suivantes ». Un clic, avec un jeton CSRF valide et la session du juriste, supprimerait le calendrier entier en annonçant « 47 occurrences supprimées ». **Deux gardes, les deux requises** : `list_series`/`delete_series` refusent un identifiant vide **en première instruction** (fail closed au MODÈLE, pas à la route), et la route **relit le `serie_id` de l'audience STOCKÉE** au lieu de faire confiance à la portée postée. Épinglé des deux côtés (`tests/test_hearing_series.py`, `tests/test_hearing_series_routes.py`).
- **Une série s'étend par ANCRAGE, jamais en chaînant — et en heure civile de Montréal.** L'occurrence *k* est `add_period(start, amount * k, unit)` mesurée depuis le départ ORIGINAL : `_add_months` écrête le jour au dernier du mois cible, donc chaîner depuis l'occurrence précédente épinglerait une série mensuelle du 31 janvier au 28 pour toujours (l'ancrage donne 28 février, **31** mars, **30** avril). Et `utils/recurrence` rend des **dates** : l'appelant compose chaque date avec l'heure murale et convertit **séparément** par `mtl_to_utc`, ce qui tient « 9 h » à 9 h de part et d'autre d'un changement d'heure — ajouter des `timedelta` à la valeur UTC stockée décalerait en silence toute occurrence postérieure à la bascule de mars ou de novembre. Le all-day garde sa convention minuit UTC : **ne pas « unifier » les deux**, trois modules en dépendent.
- **Une occurrence n'est JAMAIS déplacée hors d'un week-end ou d'un jour férié.** `utils/recurrence` ne doit jamais importer `utils/deadlines`. La prorogation de l'art. 83 C.p.c. régit les délais de PROCÉDURE — le dernier jour pour déposer ou signifier ; une rencontre récurrente ne perd rien à tomber un samedi, et proroger détruirait l'ancrage (« le 15… le 17… le 15 »). L'aperçu de création peut *signaler* un jour non juridique ; il ne le déplace pas.
- **Le plafond de 60 occurrences est fixé par les FENÊTRES DE LECTURE, pas par le lot Firestore.** Quatre contraintes le bornent simultanément, et elles cassent dans cet ordre : une suppression atomique est `2N+1` opérations contre le plafond de 500 (et le `_BATCH_CHUNK = 450` du dépôt) → N ≤ 224 ; la liste `/audiences` est une fenêtre de **100** lignes **sans aucune commande de pagination** ; le miroir Outlook lit **1500** lignes et, une fois pleine, **désarme la suppression des orphelins** ; le journal des suppressions est une fenêtre de **200** filtrée en Python après coup. Relever le plafond exige de relever ces fenêtres d'abord — la première à céder est l'écran du juriste, pas Firestore.
- **Une série supprimée s'inscrit en UNE ligne `audit_events`, jamais N.** `list_recent` lit une fenêtre dure de 200 et applique tous ses filtres EN PYTHON après la lecture : 60 lignes par chaîne évinceraient l'historique de suppression du cabinet, après quoi `list_deletions(entity_type="invoice")` répondrait vide avec `truncated: false` — une affirmation de complétude qui serait fausse. C'est légitime **uniquement parce que la suppression est atomique** au plafond de 60 ; au-delà de ~224 l'opération pourrait réussir partiellement et le détail par occurrence redeviendrait obligatoire. Les pierres tombales DAV restent, elles, par occurrence — c'est le mécanisme de synchro, pas le journal. `VALID_ENTITY_TYPES` et l'enum d'entrée MCP de `list_deletions` sont recopiés à la main : ils bougent dans le MÊME commit (parité épinglée par `tests/test_audit_events.py`).
- **La garde de troncature du miroir Outlook se mesure sur la fenêtre BRUTE.** `list_hearings_in_range` rend `_filter_confirmation(raw, …)`, qui retire les imports `refusée` **dans les deux modes** : tant que la route calculait `len(rows) >= _LIMITE_FENETRE` sur la liste filtrée, une seule réservation refusée dans la fenêtre faisait passer une lecture tronquée pour complète — ce qui **RÉARMAIT** la suppression des miroirs au-delà de la coupe, effaçant de vraies dates de cour d'Outlook sans les recréer avant des mois. Le miroir consomme désormais `list_hearings_in_range_state`, dont `window_full` vient du brut et dont `ok` distingue « rien ne correspond » d'« la requête a échoué » (une lecture en échec rendait `[]`, donc `0 >= limite` était faux et la garde ne protégeait pas). Relever `_LIMITE_FENETRE` seul ne corrige rien : cela déplace le seuil et laisse la garde cassée au nouveau.
- **`DTEND` est EXCLUSIF pour une valeur DATE (RFC 5545 §3.8.2.2).** `create_hearing` pose `end = start + 1 h` à défaut, ce qui pour un all-day à minuit UTC donne 01 h **le même jour** : `hearing_to_vevent` émettait donc `DTEND == DTSTART`, un événement all-day de longueur nulle que la norme interdit — et une série all-day en aurait expédié un par occurrence. La règle appliquée est celle que `utils/graph_miroir._dates_journee` applique côté Outlook depuis toujours (fin stockée au-delà du jour de début = déjà exclusive, sinon +1 jour), de sorte que le téléphone et Exchange s'accordent sur la durée.
- **A phone-created VEVENT defaults to `hearing_type="rencontre"`, never « audience » (July 2026 audit fix).** `dav/dossier_collections._put_hearing`'s CREATE branch used to fall through to `_default_doc()`'s `"audience"`, so every event typed on the phone without an `X-PALLAS-HEARING-TYPE` was silently stamped a court hearing — and since the forum is DERIVED from the type (`forum_of`), a psychologist appointment read `forum="judiciaire"` on every surface (the audit's « psychologist defect » was a mechanism, not data entry). The create branch now does `data.setdefault("hearing_type", "rencontre")` (extrajudiciaire); the UPDATE path stays untouched — non-effacement: an absent property on a round-trip must never rewrite the stored type. Mistyped historical events are corrected by hand in the app (user decision 2026-07-30). `vevent_to_hearing` must **OMIT** `conference_uri`/`modalite` from its returned dict when the incoming VEVENT lacks `CONFERENCE`/`X-PALLAS-MODALITE` — never return `""`. `update_hearing` merges `{**existing, **data}`, so a present-but-empty key overwrites while an absent key survives; a calendar client (Google Calendar via DavX5) that drops these on a plain time edit would otherwise wipe the stored visioconférence link server-side. Pinned by `tests/test_hearing_vocab.py`. Verify on a real device after any change to that parser: edit the time in the **device calendar**, resync, `GET …/<id>.ics`, confirm the `Visioconférence:` DESCRIPTION line (and `CONFERENCE`) are intact.
- **`icalendar` 7.0.3 serializes `CONFERENCE` as a URI natively — do NOT "fix" it to a TEXT encoding.** `event.add("conference", uri, parameters={"VALUE":"URI","FEATURE":"VIDEO"})` emits the raw comma/semicolon a Teams/Meet link carries (`?a=1,2;b=3`), no `\,`/`\;` escaping. The `vUri`/`encode=0` workaround the vocabularies spec anticipated was for older 5.x/6.x where the property registry didn't know `CONFERENCE`; on the pinned 7.0.3 it is unnecessary and would double-handle. `tests/test_hearing_vocab.py::test_conference_serialized_as_uri_without_escaping` locks it. (A future `icalendar` bump is a Dependabot silent-trigger — re-run that test.)
- **`hearing_type` uses strict equality / dict access only — `conférence` is a PREFIX of three keys.** The extrajudicial `conférence` key is a strict prefix of `conférence_de_gestion`/`_de_règlement`/`_préparatoire`; any `startswith("conférence")` or substring `in` test misclassifies. Every color dict and label lookup uses `.get(hearing_type, …)`; keep it that way.
- **A hearing's `confirmation` field (Bookings L2) is NOT its `status` — and `à_confirmer` collides across both.** `confirmation` gates DAV/MCP/Calendar visibility (`""`/absent = confirmed, `à_confirmer`/`annulée_client`/`refusée` = hidden to varying degrees); `status` is the court-date state (`confirmée`/`à_confirmer`/…). **`à_confirmer` is a value of BOTH**, meaning entirely different things — an imported Bookings reservation is `status="confirmée"` (the client booked it) AND `confirmation="à_confirmer"` (the lawyer hasn't reviewed it). Never gate visibility on `status`, never let a status filter touch `confirmation`.
- **The `include_unconfirmed` contract mirrors `include_analyse`, and a caller on the wrong default fails silently.** `list_hearings`/`list_hearings_in_range`/`list_hearings_window` default `include_unconfirmed=False` (confirmed only) — DAV (`_collection_members`), MCP agenda (`get_agenda`/`list_hearings` → `list_hearings_in_range`), the dashboard and exports MUST stay on the default, or a pending Bookings reservation leaks into DavX5/Claude as a real event. `True` keeps confirmed + à_confirmer + annulée_client but **always drops `refusée`**; only the Réception rdv tab, the Calendar view (`routes/hearings._keep_calendar` then drops annulée_client, badges à_confirmer) and the **Outlook mirror sweep** (`routes/taches_outlook` — sync-internal, to keep its truncation signal honest; `_retenir` re-applies `confirmation == ""` explicitly) pass it. `get_hearing` (single fetch) does **NOT** filter — Réception and the confirm/refuse routes rely on reaching an unconfirmed import.
- **The Bookings subject keyword is ANCHORED on the end of the subject, and that anchor is the only thing standing between an internal meeting and a cancelled one.** The polled mailbox is the juriste's OWN, so every event he creates himself carries the same `organizer` as a genuine reservation — `mot_cle_correspondant` therefore has exactly one discriminant, the subject. It used to be a substring match anywhere, which was survivable while the only keyword was « Consultation » (a rare word in a calendar) and became untenable with « Réunion ». The predicate now requires the subject to END with « {séparateur} {mot-clé} », which is precisely the `{Customer} - {Service}` shape Bookings emits; the three dash forms are accepted (Outlook substitutes an em dash), and the separator is REQUIRED so an event titled with the bare service name does not match either. Why it matters more than it looks: a false positive lands in Réception where **both** available actions are destructive — « Refuser » calls Graph `/cancel` on a real meeting and notifies its attendees, and « Confirmer » may email a client-onboarding invitation to the first external attendee (the checkbox is pre-checked when no partie matches, and `FEATURE_INTAKE` is on). Deleting the hearing does not help: the next 10-minute cron re-creates it.
- **Keyword matching folds case AND accents — and that is not cosmetic.** `« é »` precomposed (NFC, U+00E9) and decomposed (NFD, e + U+0301) are different strings to Python, and nothing guarantees which form Bookings stored when the service was named. Without folding, an accented keyword can simply never match, with no error anywhere — a failure mode that did not exist while « Consultation » was the only keyword. `graph_calendrier._plier` mirrors `utils/rapprochement._plier` (NFD decompose, drop `Mn` marks, casefold). Happy side effect: a keyword typed without its accent in `app.yaml` still matches.
- **An EMPTY `BOOKINGS_SUBJECT_KEYWORDS` is a total, silent outage.** An empty value (or a lone comma) yields an empty tuple, the predicate never matches, and the sync runs every 10 minutes importing nothing — then the **absence loop** flags every already-imported reservation as `annulée_client`, because their UIDs stop appearing in `detected_uids`. The sync now refuses to run and logs `bookings_sync_erreur_graph`/`failure` with `reason="aucun_mot_cle"` rather than proceeding. The same absence loop is why **tightening the predicate is a deploy-time event**: any previously-imported event whose subject does not satisfy the new anchor flips to `annulée_client` (harmless for genuine reservations, which end with the service name; it is in fact how pre-existing false positives get cleaned up, and removing such a card does NOT call Graph).
- **A test that does not override `BOOKINGS_SUBJECT_KEYWORDS` proves nothing about production.** The predicate's behaviour is decided by an env var set in `app.yaml`, outside pytest's reach: `tests/test_graph_calendrier.py` pins `est_reservation("Réunion interne") is False` under an autouse fixture that freezes the keywords to `("Consultation",)`, so it would stay green while production behaved the opposite way. Any test asserting what the predicate does with a given service MUST monkeypatch the real keyword tuple.
- **`BOOKINGS_DEBUG_PAYLOAD` emits at INFO, deliberately.** It used to log at DEBUG while the root logger sits at INFO in production — the one tool meant for tuning the predicate was mute exactly where it is needed. The flag is itself the gate (default `false`), and the line still carries only `organizer_match`, `keyword_match`, the detected keyword, `subject_len` and email DOMAINS — never the subject (it embeds the client's name) nor a full address.
- **The Bookings sync's OWN lookup MUST pass `include_unconfirmed=True` — the duplicate trap.** `routes/taches_bookings._synchroniser` reconciles by `graph_ical_uid` against `list_hearings(include_unconfirmed=True)`; on the default it would not see the `à_confirmer` imports it created last cycle and would **recreate a duplicate hearing every 10 minutes**. Pinned by `tests/test_bookings_sync.py`. A Bookings import **never bumps a CTag** (invisible until confirmed); the bump lives in `routes/reception.rdv_confirmer` (`collection_for(dossier_id)`, `""` → « Général »), never in the sync.
- **Refusing a Bookings rendez-vous cancels the Outlook meeting (Calendars.ReadWrite) — best-effort, never blocking.** `routes/reception.rdv_refuser` calls `graph_calendrier.annuler_reservation` **only for a still-active `à_confirmer` import** (an `annulée_client` one is already cancelled — re-calling Graph would 404), catches `GraphError`/`GraphNotConfigured`, applies `confirmation="refusée"` regardless, and shows a « annulez manuellement » banner on failure. A refusal never bumps a CTag (a pending import was never in DAV).
- **`cron.yaml` is the WHOLE project cron table — `gcloud app deploy cron.yaml` REPLACES it.** It now carries THREE entries: the portail L1 reconciliation (15 min), the Bookings L2 sync (10 min) and the Outlook mirror (10 min). Any future cron entry MERGES into this one file and redeploys the complete file — replacing it with a single entry silently stops the other jobs.
- **The Outlook mirror's loop-prevention marker is deterministic — and its GUID is FROZEN FOREVER.** The mirror (`routes/taches_outlook` + `utils/graph_miroir`) writes confirmed hearings into the SAME default calendar the Bookings sync polls, with the juriste as organizer — exactly the shape of a reservation. Every mirrored event therefore carries the extended property `MIROIR_PROP_ID` (value `"{hearing_id}|{etag}"`) plus the « Pallas Athéna » category, and `mot_cle_correspondant` refuses any marked event **before** its keyword logic (so `est_reservation`, `extraire` and `_debug_payload` are all covered). The guard is WIDE (property OR category refuses import) while destructive mirror operations are NARROW (the property alone qualifies — a real event hand-categorized « Pallas Athéna » is never deleted). Changing `MIROIR_PROP_GUID` orphans every mirrored event (invisible to the diff, transparent to the guard); the `$expand` in `lister_reservations`/`lister_miroirs` is what makes the property visible — dropping it silently blinds the guard down to the category alone.
- **The Outlook mirror is Firestore-READ-ONLY by design, and its two sides share ONE window.** Storing an `outlook_event_id` on the hearing would regenerate `etag`/`updated_at` on every sweep (DavX5 re-sync churn) and entangle with the Bookings import's `graph_*` fields (whose `list_bookings_all` absence loop must never see mirror bookkeeping) — the mapping lives in the mirrored event's extended property instead; `test_miroir_jamais_d_ecriture_firestore` pins it. The Athéna fetch and the Outlook calendarView use the SAME [-30 j, +365 j] window: a mirror can only exit it through the past edge (its date is fixed; the future edge advances with the clock), so a hearing postponed beyond the horizon leaves its mirror inside the window where the diff deletes it as an orphan. Bookings-sourced hearings are NEVER mirrored (`_retenir` — they already ARE Outlook events; mirroring would duplicate every client meeting).
- **A truncated mirror window disarms deletions — never "fix" that.** When `list_hearings_in_range` returns `_LIMITE_FENETRE` (500) rows, the desired set is truncated: driving orphan deletion with it would mass-delete valid mirrors of every hearing beyond the cut (and never recreate them). The sweep skips the DELETE phase entirely and logs `miroir_outlook_erreur_graph`/`failure` with `reason="fenetre_pleine"` at ERROR — creations and corrections stay armed (a truncated set still tells the truth about what it contains). The remedy is raising `_LIMITE_FENETRE`, not re-enabling deletes.
- **MCP category/type enums in `mcp/tools.py` are hand-kept LITERALS, not derived.** `_NOTE_CATEGORIES`/`_DOCUMENT_CATEGORIES` copy the model `VALID_CATEGORIES` by hand because `mcp/tools.py` is imported at startup and importing `models.*` runs `firestore.Client()` at module load (`models/__init__.py`). `tests/test_mcp_tools.py`/`test_document_vocab.py` pin the literals against the models — update the literal when the model changes, don't switch to an import. **The rule bans `models/*` imports, not pure `utils/*` ones**: `_COVERAGE_CODES` and the Phase-O `_PHASE_CODES`/`_SOUS_PHASE_CODES` are DERIVED (`mcp.coverage`, `utils.phases` — both Firestore-free), which is preferable wherever possible because drift becomes structurally impossible.
- **`phase`/`sous_phase` are presence-gated with `""` valid — a hard requirement at the MODEL breaks three write paths at once** (Phase O). A non-empty requirement in `_validate` would 422 every DavX5 task PUT (no third-party client emits a phase), break `_auto_create_tasks_for_steps`, and contradict the optional MCP parameters — the D-6 « requis à l'écriture » lives at the WEB FORM only (selector without an empty option at creation, « Non renseignée » offered on a legacy edit). The shared validation is `phases.validate_pair` (presence-gated, prefix cross-check), consumed by the three models AND `protocol._validate_step`; `phases.apply_sous_phase_default` imputes the `-00` at every create/update merge. The taxonomy itself changes **only on an approved spec** (like `taxonomie.py`); the CQ/CS step→phase mapping is pinned by `test_template_step_mapping_is_pinned` — legal content, never a refactor side-effect.
- **A budget is never edited — it is re-minted.** `models/budget.py` has NO `update_*`/`delete_*` by design (deontological proof of WHEN the client was informed — Phase O spec §10). « Modifier le budget » seeds the form from the latest version and SAVES A NEW ONE; the version counter is Python-side (no transaction — a double-submit's worst case is a duplicate number, resolved by the `created_at` tie-break, never a loss). Three content rules that fail loudly, not silently: **ADM/HOR are refused** in budget lines (D-14 — withdrawn from the client quote; the form never offers them, the model blocks a forged POST); the consumption percentage is computed **in dollars** (fees + frais — the figure quoted to the client), never in hours; and the « réalisé » counts **billable time only** (worked, not invoiced — the practitioner's 2026-08-10 decision: the 80 % alert must fire BEFORE invoicing reveals the overrun). Legacy unphased time lands in a « Non renseignée » row — displayed apart, never silently dropped.
- **VTODO `CATEGORIES` carries the phase CODE, never its label** (D-7: renaming a phase must touch no VTODO — ASCII survives the Android round-trip where NFC/NFD is not guaranteed). `task_to_vtodo` emits the code beside the category label in ONE multi-value CATEGORIES plus `X-PALLAS-PHASE`/`X-PALLAS-SOUS-PHASE`; `vtodo_to_task` applies the strict NON-EFFACEMENT rule (absent property → absent key; unknown/contradictory value → ignored; CATEGORIES fallback accepts only members of the phase vocabulary), and `update_task` carries the coherence repair: a phase supplied WITHOUT a sub-code that contradicts the stored one re-imputes to the `-00` instead of 422-ing a phone PUT that DavX5 would swallow silently.
- **`form-action` covers a form's whole REDIRECT CHAIN, not just its action URL.** The OAuth consent POST is same-origin, but it answers `302` to `https://claude.ai/api/mcp/auth_callback`, and the browser refuses that hop under `form-action 'self'` — the authorization code never reaches Claude and the connector cannot be added. Chrome reports the *original* same-origin action URL in the console message, which reads like a nonsensical "self violates 'self'"; the redirect is the actual violation. `security._form_action_for` therefore widens `form-action` to `'self' https://claude.ai https://claude.com` **on `/oauth/authorize` only** (plus loopback outside production, mirroring `mcp.oauth.redirect_uri_allowed`), and `tests/test_security_headers.py` pins the source list against `mcp.ALLOWED_REDIRECT_URIS` so the two cannot drift. **This is exercised only when a connector is added or re-authorized**, so it stayed latent from the 2026-07-11 CSP enforcement flip until the first re-consent.
- **An MCP note write MUST bump the dossier CTag — nothing else will.** `models/note.py` never bumps (it does not even import `dav/`); bumping lives in the caller (`routes/notes.py`, `dav/dossier_collections.py`). A tool path that calls `create_note`/`update_note` and stops leaves the note in Firestore and visible in the web UI while **DavX5 silently never re-syncs it** — `_handle_sync_collection` short-circuits when the client's token equals the current one. The bump is wrapped in its own `try/except` in `mcp/handlers._bump_note_ctag` and surfaced as `dav_synced` + a French warning: letting it raise would hit `endpoint._tools_call`'s blanket `except Exception`, reporting an already-committed write as a failure, and the model would retry into a **duplicate note** (notes carry no idempotency key and `create_note` mints a fresh UUID each call).
- **A note written to a `fermé`/`archivé` dossier never reaches the phone.** The root Depth:1 PROPFIND only advertises `actif`/`en_attente` dossiers, and `_dossier_is_active` drains the collection for the rest. The write is allowed (the register is legitimate post-closure) but the payload returns `dav_synced: false` plus an explicit French warning — never silence.
- **The `include_analyse` contract is a per-CALLER decision, and getting it wrong fails silently in both directions.** The « Théorie de la cause » note (`is_analyse`, Analyse leaf) is excluded by `list_notes`/`list_notes_recent` **by default** so the Notes views and `/notes` never show it; the **DAV collection paths (`dav/dossier_collections.py`), `_sync_dossier_dav_visibility` and the MCP note reads (`mcp/handlers.list_notes`, `get_notes_summary`) MUST pass `include_analyse=True`** — a DAV caller left on the default makes the note vanish from DavX5 with no error anywhere (the collection simply stops listing it), and a forgotten exclusion on a new Notes view breaks the isolation the sheet exists for. MCP exposes it **read-only**: `list_notes`/`get_note` emit `is_analyse` (an outputSchema-contract field) and `append_to_note` refuses the note with a French message that never quotes its content. The note is `dateless` — `note_to_vjournal` omits DTSTART (jtx Board *Note*) while CREATED/DTSTAMP stay (the NOT-NULL trap) — and `vjournal_to_note` never writes an explicit `is_analyse=False`, so a client stripping unknown X- properties can't demote the stored flag through a PUT. Three guards protect the one-per-dossier singleton: the note edit form **locks the dossier picker** on an `is_analyse` note and `note_update` refuses a changed `dossier_id` (a moved/cleared analyse note would be invisible in every app view); `create_analyse_note`'s existence check queries Firestore **directly and fails CLOSED** (via `list_notes` it would swallow a read error into « no note yet » and seed a duplicate over the filled analysis); and the DAV PUT create branch **drops `is_analyse`** on the « Général » scope or when the target dossier already has its analyse note (a jtx move to an empty dossier keeps it).
- **A note write must be validated on the string that is STORED, not on each field.** `TAG_RE`'s `[^<>]` body class includes `\n`, so a match spans arbitrarily many lines — and `append_to_note` sanitizes `existing + block` as one string. An unpaired `<` already sitting in the note (which the web form and DAV PUT both accept happily) plus any `>` in the addition (a Markdown blockquote is the obvious one) makes the regex **span the join** and delete the note's tail, the separator and the provenance stamp. Both halves pass a per-field check individually. The handler therefore asserts the post-condition `sanitize(combined) == combined` before writing, and the length check runs **first** (sanitize also truncates, so an over-long note would otherwise report the chevron reason).
- **Tool refusal messages must not quote note content.** `utils/tracing_setup.span()` calls `record_exception` + `set_status(str(exc))` on anything crossing its boundary, and `_SanitizingSpanExporter` scrubs **attributes, not exception events** — an excerpt in a `ToolArgumentError` would ship privileged research to Cloud Trace. `endpoint._tools_call` catches `ToolArgumentError` **inside** the span and re-raises it outside; the messages describe the problem instead of sampling it.
- **`security.sanitize` eats Markdown, data-dependently.** `_TAG_RE = <[^<>]*>` deletes every angle-bracket run: `<https://canlii.ca/t/abc>` vanishes with the citation, and `« si a < b et b > c »` loses « < b et b > », while `« a < b < c »` survives (the `[^<>]` body fails to match). The MCP write path normalizes autolinks to `[url](url)` and then **refuses** anything still matching `security.TAG_RE` — that public alias exists precisely so the handler's prediction cannot drift from what `sanitize` actually removes. Truncation is the same class of trap: `sanitize` cuts at `CONTENT_MAX_LENGTH` with no exception and no flag, so `append_to_note` checks the projected length **before** calling `update_note`.
- **Lateness and computation are two different questions, and only ONE clock may answer either.** `utils/deadlines.py` holds both families and keeps them apart: `compute_deadline`/`is_juridical_day`/`next_juridical_day` implement art. 83 C.p.c. and **never read a clock**; `today_mtl`/`effective_due`/`is_past_due`/`days_until` answer « is this past? » on the **Montréal** calendar, evaluated against the **prorogued** deadline (lawyer's decision 2026-08-02, reversing the earlier « juridical days play no part in lateness » doctrine): `effective_due = next_juridical_day(d)` (inclusive — a no-op on every computed deadline, which already lands juridical by construction), so a deadline due Saturday is actionable Monday and late only Tuesday, on **every** surface (web, MCP, 07:00 briefing). Prorogation can only make lateness start LATER — never earlier. UTC crosses midnight 4 h (EDT) or **5 h** (EST) before Montréal, so a UTC-based comparison declares a deadline past for the whole evening preceding it (the 2026-08-02 report: tasks due Monday read « en retard » Sunday from 20:00); a hard-coded offset would be wrong half the year. **Templates never compare datetimes** — routes compute `_overdue`/`_days_left`/`_days_remaining` via the predicate and templates read the flags (the in-template `due < now` comparisons were the drift vector). **A TEST that derives a date from the clock and then asserts lateness is the same bug in test clothing.** It passes all day and fails only in the 00:00–04:00 UTC band — where `today_mtl()` is still the PREVIOUS Montréal day, so a « 3 days ago » UTC offset is really 2 Montréal days — and then only when that lands on a weekend, which prorogation carries forward to today. That combination broke the 2026-08-11 00:03 UTC build (`test_get_agenda_marks_overdue_tasks`, green for months). **Freeze the day (`_freeze_mtl_today` in `tests/test_mcp_tools.py`) and use a fixed deadline; never widen the offset** — widening is probabilistic, and it is exactly what left that landmine armed after a sibling test hit the same wall and went from 2 days to 7. A **frozen 34-entry reference table** (`tests/test_deadlines.py::test_compute_deadline_frozen_reference_table`) pins art. 83 against every future change — it caught a hand-computed error the day it was written: a 15-day delay computed BACKWARD from 16 March lands on Friday 27 February, not Monday 2 March, because a backward computation **adjusts backward**.

- **A protocol step's stored `status` is a FOSSIL; the connector derives instead.** `check_overdue_steps` is the only writer of `en_retard`, it has **no branch that ever clears one**, and it runs only when the lawyer opens the protocol page in a browser — so a step stamped by the pre-2026-07-30 wall-clock rule carries `en_retard` for ever, and a read handler is forbidden from repairing it. `mcp/handlers.derive_step_status` therefore recomputes: `complété` is authoritative and never re-derived, everything else follows the deadline against `today_mtl()`. `_step_row` emits the derived value as `status` (what the 07:00 briefing already reads), the raw word as `status_stored`, and `status_differs` to make the fossil visible; **`is_overdue` is `status == "en_retard"` BY CONSTRUCTION**, so the contradictory pair the audit found can no longer be emitted. (`protocols/detail.html` reads `_days_remaining < 0` alone — never the stored word.) The web-keeps-its-own-rule split (mandate decision D3) was **reversed on 2026-08-02**: `get_task_summary`/`get_protocol_summary` still take an injectable `today`, but its DEFAULT is now `today_mtl()` — the historical wall-clock/UTC defaults are dead, and all three surfaces read the same prorogued predicate.

- **`pagination.keyset_page` pages a Python-materialized list by ORDER KEY — and its key must be IMMUTABLE and TOTAL.** These lists are re-derived on every call (Firestore cannot order them), so an offset walk silently skips or repeats when a row is inserted between pages; a keyset resumes from a position in the ordering instead. Two rules are load-bearing: the key must end with the document **id** (unique, never rewritten) so ties break deterministically, and the next cursor is minted **from the last returned row** — not from the model's window cursor, which in a Python-filtered branch points past rows the handler dropped. **It cannot serve every ordering:** `list_invoices` deliberately does NOT use it, because the model orders `date DESC, id ASC` — MIXED directions — while `keyset_page` has a single `descending` flag; using it would skip or repeat rows inside a same-date group, the normal case at month end.

- **A tool that emits `next_cursor` must accept a `cursor`, and the test that checks it is DERIVED, not a list.** `test_every_paged_tool_declares_a_cursor_input` sweeps `OUTPUT_SCHEMAS` for `next_cursor` and demands the matching input property. It was first written with a hand-kept tuple and immediately stopped proving anything about the next tool added — `list_invoices` slipped past it, then `list_notes`. Any pinned inventory of tools has the same decay: derive it.

- **`list_notes`' default scope is NARROW, and that was the whole bug.** With no `dossier_id` and no `scope` it returns only the « Général » notes — never the firm. A caller that searched a term and got nothing concluded « no such note exists » while the note sat in a dossier. `scope="cabinet"` opens it, `dossier` is implicit whenever `dossier_id` is present, and **every contradictory combination is refused rather than resolved by precedence** (`cabinet`+`dossier_id`, `general`+`dossier_id`, `dossier` without one, `folder_id` in cabinet, `cursor`+`offset`, `cursor` outside cabinet, **`offset` INSIDE cabinet**). That last one is the mirror the first draft forgot: `offset` was declared, validated, then dropped, so every page returned the first — the very failure the change existed to remove, reproduced by its own new path. Cabinet scope orders on `(created_at, id)` (immutable) rather than the model's pinned-first order, because a mutable key component moves rows across a page boundary; the other scopes keep the model's ordering and their offset paging **byte-identical**, because the two scheduled jobs read them.

- **`query` on `list_documents` matches METADATA ONLY, and the description says so on purpose.** Reserving the meaning now is what keeps a future content-search tool from having to contradict it. For the same reason accent/case folding was deliberately NOT added to `list_notes`' `query`: it would WIDEN the match set, and the 17:00 job chains `list_notes` → `append_to_note`, so a widened search can retarget the append to a different note. A silently retargeted append is worse than a missed accent.

- **One client-facing rendering per document — the invoice detail page is a DATA sheet.** Since August 2026 it shows only what is stored (identification, frozen billing snapshot, line items, totals, live Solde, payment form, notes); the firm letterhead, the « FACTURE » title, the paper layout and the whole `@media print` block are gone, and `_firm_info()` left `routes/invoices.py` with them. The client document is the **Word note d'honoraires** (`/factures/<id>/note-docx`), whose letterhead lives in the gabarit. Re-adding a screen facsimile would recreate two renderings of one invoice that must agree for ever — and they would drift, because only one of them is what the client actually receives. `tests/test_invoice_detail.py` renders the template and pins both halves (data present, facsimile markers absent). Two details worth keeping: the tax RATES come from the invoice's own `gst_rate`/`qst_rate` (an invoice issued under another rate must read back under it — never hardcode « 5 % »/« 9,975 % » in markup again), and the disbursement bucket is `type != "fee"` (the `invoice_docx` rule), so a line item of an unexpected type can never vanish from the one page whose job is to show everything the invoice holds.
- **`invoice.amount_due` is NOT a balance.** It is `total − retainer_applied` frozen at issuance and never updated, so it stays non-zero on a fully paid invoice; `balance_of` is the live figure. **`payment_basis` distinguishes silence from fact**: `"none"` means nothing was RECORDED, not that nothing was paid — the 21 pre-August invoices were deliberately not backfilled, so they all read that way until the lawyer enters them. And the two firm-wide « outstanding » definitions **deliberately disagree**: `get_outstanding_total` sums `amount_due` over `envoyée`+`en_retard`, while `get_dossier.summaries.invoices.total_outstanding` sums `total`, counts `brouillon` and treats `payée` as settled. « Fixing » the latter would change a money value the 07:00 briefing already reads; both definitions are stated verbatim in the tool descriptions and pinned by a test that explains why.

- **`complete_task` cascades into the protocol, and that is the app's own behaviour — not an invention.** It goes through `models/task.update_task`, so `_sync_protocol_step` completes the linked step and `_check_protocol_completion` can close the WHOLE protocol; `list_urgent_steps` keeps only `actif` protocols, so a closure silently empties that dossier's deadline feed in the briefing. The lawyer accepted this (2026-08-02). Three properties make it tenable: `dry_run` previews the cascade; the step is **RE-READ after the write** and its real state reported, because `_sync_protocol_step` swallows every exception and a predicted « complété » could be a lie; and a French warning names the closure. **`toggle_task_complete` must NEVER be called** — it is a four-state toggle that sends `annulée` *and* `terminée` back to `à_faire`, silently un-cancelling a cancelled task. `à_faire` is refused as a status (reopening clears `completed_date` and de-completes the step); asking for the other terminal state on an already-closed task is refused rather than rewriting the lawyer's decision; and the same state twice writes **nothing at all**, which is what makes a scheduled job replayable. `idempotentHint` is therefore **per tool**, not per family.

- **A compliance report must never build a manquement out of a failed read.** `list_protocols` and `get_parties_bulk` both fail open to empty. Unguarded, `PROTO_ABSENT` would fire on EVERY dossier at once (a false-manquement storm), and a client would be reported unverified — a regulatory accusation founded on an error. `get_coverage_report` therefore SUPPRESSES the affected checks, lists them in `scope.checks_skipped`, and sets `data_completeness.protocol_index_complete` / `kyc_checked` to false. **A shortened report must never pass for a clean one.** The same reasoning gives `list_notes`/`list_documents` their `dossier_status_matched` count: zero rows with zero matched dossiers explains WHY the answer is empty instead of asserting the firm holds no such record.

- **Une adresse de contact se fournit en BLOC de six clés, ou pas du tout.** `models/partie._normalize` appelle `utils/validators.apply_address_defaults`, qui écrit **Canada / Québec / Montréal dans le dictionnaire de l'appelant** dès qu'un `<prefix>_street` est présent et que ces clés sont vides — et `update_partie` fusionne `{**existing, **data}` avant un `set()` de document complet. Un contact torontois envoyé avec une rue et une ville vide est donc **silencieusement déménagé**, sur une facture que le client recevra. Le connecteur exige les six clés ensemble (`unit` et `postal_code` peuvent être vides) ; la règle est délibérément plus grossière que la logique de l'injecteur, pour rester correcte si celle-ci change. Corollaire général sur ce modèle : **une clé présente et vide EFFACE, une clé absente survit** — toute charge de correction se construit PAR PRÉSENCE, jamais par `args.get(k, défaut)`, et un `id` fourni corromprait le CHAMP `id` sans changer le chemin du document (ce qui casse la pagination par curseur et les scans de mandataires).
- **`create_invoice` SAUTE EN SILENCE une source manquante, déjà facturée ou d'un autre dossier — et `_SourceConflictError` ne peut PAS l'attraper.** L'escamotage a lieu dans la boucle de PRÉ-LECTURE, donc l'id n'entre jamais dans `source_refs` et la relecture transactionnelle ne le voit pas. Un contrôle côté appelant ne vaut donc rien : il valide un instantané que le modèle peut ne pas honorer, puis le modèle écrit une facture courte en rapportant un succès. C'est pourquoi `require_all_sources` **et** `expected_total` vivent dans le MODÈLE (lot Q), la pré-lecture du gestionnaire ne servant qu'à nommer chaque fautif par motif. Note connexe : `get_time_entry`/`get_expense` avalent une erreur de lecture en `None`, donc le motif dit « introuvable **ou illisible** » — le modèle ne peut pas distinguer les deux, et affirmer le mauvais serait pire que nommer les deux.
- **Un `dry_run` qui prédit un succès que l'appel réel refuse est un mensonge.** `mcp/write_support.run_write` court-circuite la branche sèche **sans jamais appeler le modèle**, donc chaque garde du modèle qu'un appelant peut déclencher (une entrée déjà facturée, un nom requis manquant, un id inconnu) doit être **répétée dans le gestionnaire AVANT cette branche**. Le cas le plus coûteux est l'entrée facturée : le modèle la refuse, mais une simulation aurait annoncé un succès et la reprise aurait continué sur une hypothèse fausse.
- **`_rebuild_party_mirrors` lève un `KeyError` NON RATTRAPÉ sur une entrée de partie sans « id ».** L'indiçage est brut (`c["id"]`), `_validate` ne vérifie pas la présence de la clé, et le résultat est un HTTP 500 plutôt qu'une erreur de validation. Tout chemin d'écriture de dossier doit donc RÉSOUDRE chaque `partie_id` avant que le modèle ne voie la donnée — ce qui sert aussi à poser `name`/`avocat_name` côté serveur, ces instantanés étant ce qu'une procédure générée cite. `tests/test_dossier_parties.py` épingle le `KeyError` pour que la garde ne soit jamais retirée comme superflue.
- **Un numéro de facture importé ne peut jamais emprunter le préfixe « AAAA-F » du millésime COURANT.** Le compteur de cette année existe déjà et `_scan_max_invoice_seq` ne ressème une année qu'à sa première utilisation : un numéro planté là serait réattribué plus tard à une vraie facture. Une année **passée** est sûre (son compteur ne peut plus naître), une année **future** aussi (son premier usage sèmera au-dessus du numéro importé). `models/invoice._is_live_sequence_number` teste le préfixe ENTIER, pas « préfixe + chiffres » : `int()` mange les espaces, donc « 2026-F 12 » échoue `isdigit()` et se lit pourtant 12 au réamorçage.
- **Les heures MCP acceptent deux décimales, et plus fin est refusé — jamais arrondi.** `round(0.25, 1) == 0.2` : le quart d'heure hérité, à 300 $/h, stockait 60,00 $ là où la facture papier imprime 75,00 $, en silence, et l'écart faisait ensuite échouer la réconciliation de `import_invoice` par une différence que l'appelant ne pouvait pas combler. Le validateur de schéma n'ayant pas `multipleOf`, c'est un contrôle de gestionnaire (`handlers._clean_hours`).
- **Facturer n'est PAS irréversible, et le dire faussement prive le juriste de sa seule réparation.** `models/invoice.void_invoice` remet chaque source `invoiced: False, invoice_id: None` en un lot atomique, après quoi `delete_invoice` retire la facture annulée et le numéro se libère. Les refus et les avertissements du connecteur nomment donc cette voie ; l'ancienne formule « ni le connecteur ni l'application ne pourront plus les modifier » était fausse.
- **La phase d'une ligne FACTURÉE reste corrigeable — et c'est un écrivain SÉPARÉ qui le permet, jamais une garde relâchée.** Le mur `invoiced` protège les CHIFFRES de la facture ; `phase`/`sous_phase` n'en sont pas : balayage fait, ce couple ne figure sur AUCUNE facture (les postes de `invoices/{id}/lineitems` sont des copies indépendantes qui n'ont pas le champ), aucun `{{…}}` de gabarit ne le résout, aucun sérialiseur DAV ne l'émet, et le « Journal des honoraires » n'en a pas de colonne. Il ne nourrit que `budget.aggregate_actuals`, laquelle compte le travail FACTURÉ — d'où le défaut qu'on répare : une ligne facturée non classée gonfle la rangée « Non renseignée » de chaque vue budget-vs-réalisé et fausse le seuil déontologique de 80 %. `update_time_entry`/`update_expense` gardent donc leur refus **INTACT** (leur refus est ce qui rend vrai le « invoiced : toujours faux » de LEUR schéma de sortie), et le reclassement passe par `set_time_entry_phase`/`set_expense_phase`, dont la garantie est une FORME : un `update()` partiel de QUATRE clés, jamais le `set()` de document fusionné. Ne jamais « simplifier » en relâchant la garde des `update_*` : ce serait mettre un montant à une garde de distance d'un appelant qui ne voulait que reclasser. Côté web, même discipline — un formulaire dédié dont le seul contrôle est le sélecteur de phase (`templates/time_expenses/phase_form.html`), et un test épingle l'inventaire de ses `name=` : y glisser un champ ne serait pas refusé, il serait ignoré, et la main suivante le « brancherait » sans qu'aucun test ne tombe. Corollaire d'ordre : le modèle **valide AVANT** d'exiger une phase non vide — un sous-code inconnu laisse le parent non dérivé, et rapporter cela comme « phase requise » enverrait l'appelant réparer la mauvaise moitié (`phases.resolve_pair` complète, `phases.validate_pair` refuse ; les deux vivent dans le module pur, partagés par les modèles ET `handlers._resolve_phase_pair`).
- **The MCP tool schema is fully specified, and `outputSchema` is a CONTRACT** (July 2026). Every input property carries a per-usage `description`; every tool declares an `outputSchema` (`mcp/output_schemas.py`) that `structuredContent` MUST conform to (MCP 2025-06-18). Three rules keep it honest: **never `additionalProperties: false` in an output schema** (adding one payload field would make strict clients reject valid responses — correct on inputs, poison on outputs); **`required` lists only always-present keys** (`list_documents.folder_path` is conditional, typed but never required); and **every schema root MUST carry `type: "object"`, even beside an `anyOf`** — the wire schema for `Tool.outputSchema` requires it, and the official SDK zod-parses the whole ListToolsResult, so ONE bare-anyOf descriptor kills all 47 tools at once. `tests/test_mcp_output_schemas.py` runs every REAL handler and validates the exact `structuredContent` payload against the declared schema, one test per `anyOf` branch — extend it whenever a handler's payload shape changes, or the deploy gate ships a violated contract.
- **A stored value can violate a declared output type via the DAV write paths.** vobject parses an ADR component with an unescaped comma (a known non-DavX5 CardDAV client bug) as a Python LIST, and `models/partie` sanitizes only str values — the list is committed silently. `mcp/handlers._addr_str` coerces on the way out so `get_partie` honours its schema regardless of stored shape. The general lesson: an output schema is a claim about EVERY write path's data (web, DAV, MCP), not just the fixtures.
- **The MCP endpoint is stateless JSON mode — never add SSE** (`GET /mcp` streams) without revisiting the gunicorn `--timeout 60` sizing; long-lived connections would exhaust the 2×4 worker/thread budget.
- **Firestore TTL is lagging garbage collection, not enforcement.** `oauth_codes`/`oauth_tokens` expiry checks stay in code (`expire_at` comparison on every read); deleted-late docs must still be treated as dead.
- **Cloudflare bot mitigations can challenge Anthropic's egress** on `/mcp`/`/oauth/*` (non-browser client). A Configuration Rule disables Browser Integrity Check on those paths; if Super Bot Fight Mode challenges Claude's requests, relax its "Definitely automated" action and verify in Security → Events (same class of fight as the Play-Store/Bubblewrap episode).
- **`athena/mcp/` shadows any installed `mcp` PyPI package** (the app dir is first on `sys.path`). Never add the MCP Python SDK to `requirements.in` without renaming one of them.
- **The consent screen must reuse class strings that already exist in the compiled CSS** — `athena/mcp/` and `templates/mcp/` are covered by the `@source "../../templates"` scan, but adding a genuinely new utility class still requires the full recompile-and-rehash procedure. Note the app's primary buttons are `bg-gray-900` (the indigo utilities — `bg-indigo-600`, `text-indigo-600`, `border-indigo-600` — ARE in the compiled artifact and drive accents/active-tab underlines, so reuse those freely; earlier notes calling `bg-indigo-600` absent were stale).
- **Never fill gabarits with `docxtpl`/`python-docx`** (Phase H): their load/save round-trip corrupts letterhead templates for Word (repair prompt). `utils/docx_fill.py` substitutes strings in the XML zip entries and copies everything else byte-identical — keep it that way.
- **Word fragments typed placeholders across `<w:r>` runs** (spell/grammar `proofErr` markers, tracked changes, mid-word format or **language** changes — notably at the dot in a namespaced name like `{{dossier.defendeur}}`, where the two halves get a different proofing/`lang` `rPr`). `utils/docx_fill._normalize_runs` heals this **before** matching (in extract/validate/fill alike): it strips `proofErr` markers, then **folds each maximal CHAIN of adjacent text runs in ONE linear pass** (`_fold_run_chain`), absorbing a neighbour into the accumulator when **either** they carry identical `rPr` — Word's own save-time optimization — **or** joining them bridges a placeholder (`_bridges_placeholder`: the accumulator holds an unclosed `{{`, or the split fell between the braces). In the **bridge** case formatting differences are ignored and the accumulator's `rPr` wins — the whole `{{name}}` is replaced by one value anyway, so collapsing its fragments is correct, and it beats shipping an unfillable literal `{{…}}` that **retyping never fixes** (Word re-splits at the dot every save — the July 2026 "fragmenté persists" report). The run open tag is matched as `<w:r(?:\s…)?>` so runs carrying `rsid` attributes are healed too (those attributes are dropped on merge; Word reopens fine). Only genuinely **structural** splits stay unmerged and are still reported via `split_run_suspects`: a `<w:br/>`/`<w:drawing>`/field code between the halves (not a plain `<w:t>` run → no match), a bookmark/comment marker between them (breaks adjacency), or a paragraph boundary. Load-bearing details: **the LINEARITY INVARIANT (CWE-1333) — none of `_PROOF_ERR_RE`, `_TEXT_RUN_RE` or `_RUN_CHAIN_RE` contains a `.`, and none carries `re.DOTALL`** (the `<w:t>` capture is `[^<]*`, the `<w:rPr>` body is tempered, the paired-`proofErr` body is `[^<]*`). This is not style: a `.*?` body under DOTALL rescans to end-of-string once per unmatched opening tag, and a **1.2 KB** `.docx` whose `document.xml` held 465 KB of unclosed `<w:proofErr …>` cost **45 SECONDS** inside `validate_template` (2026-07-30 — repetitive XML deflates ~350:1, so `MAX_COMPRESSED_BYTES` never bounded the CPU; `MAX_SINGLE_XML_BYTES` is the real ceiling), while the unclosed-`<w:rPr>` variant multiplied with the old fixpoint loop into **O(n³)**. `tests/test_docx_fill.py` pins the invariant with `"." not in pattern` + `not flags & re.DOTALL` tripwires. The paired-`proofErr` body is also a **correctness** fix: `.*?` DELETED intervening `<w:r>` runs and their placeholders, which vanished from the document AND the inventory. **The fold replaced a `while True` fixpoint loop** — `re.sub` consumed BOTH runs of a REFUSED pair, so the second was never offered to its right neighbour and a split healed or not depending on the **PARITY** of its alignment (the residual half of the "fragmenté persists" report); folding also removed the need for any pass budget. A run the fold declines to merge is re-emitted **byte-for-byte**, which is what keeps normalization a no-op on a clean template. Split-run detection is **per-occurrence** (`_all_token_counts`, over names AND `{{#…}}`/`{{?…}}`/`{{/…}}` markers), so a clean copy of a repeated field can't mask a fragmented sibling (the "only the last `{{tribunal}}` fills" bug); and **`fragmenté` ≠ missing data** — a fragmented field ships as literal `{{…}}`, whereas an intact field with no dossier value fills with a visible `[CHAMP MANQUANT : …]`. One deliberate narrowing: an `rPr` that NESTS another (`<w:rPrChange>`, a tracked formatting change) no longer matches, so such a run ends the chain and is reported as a suspect rather than silently reformatted.
- **The docx paragraph scan must cover ALL paragraphs** — a previous implementation passed `count=1` to `re.sub` and silently skipped block placeholders outside the first paragraph (regression-tested in `test_docx_fill.py`).
- **Fill-engine replacement callbacks must be functions**, never bare strings — user content containing `\g<0>` or backslashes would be interpreted as regex group references (regression-tested).
- **Template files are NOT `documents` records** — they live at `users/{uid}/templates/…` and are managed only through `/gabarits`; generated outputs saved into a dossier ARE regular documents (independent copies).
- **Supprimer un dossier de classement : les documents D'ABORD, les enregistrements de dossiers ENSUITE — et un échec sur les documents ne touche à AUCUN dossier.** L'ordre est load-bearing. L'ancienne `delete_folder` avalait l'échec du re-parentage (`logger.warning`) et supprimait le dossier quand même : les documents gardaient un `folder_id` mort et devenaient **invisibles** — `list_documents` filtre par égalité exacte, donc ils n'apparaissaient ni à la racine ni dans aucun dossier navigable, seulement en recherche libre ou dans le ZIP du dossier complet. Le fail CLOSED conserve l'arborescence et rend l'opération rejouable ; `subtree_members` retrouve d'ailleurs un document ainsi échoué, en s'appuyant sur l'ensemble d'identifiants plutôt que sur la navigation. **« Supprimer le contenu » est la SEULE cascade destructive de l'application** — la doctrine (`delete_dossier` refuse tant qu'un enfant existe, pour ne pas « orpheliner des blobs confidentiels ») vise les cascades SILENCIEUSES : celle-ci est demandée, décomptée sur tout le sous-arbre avant le clic, plafonnée (`MAX_FOLDER_DELETE_DOCUMENTS` — le temps de gunicorn, comme les plafonds du ZIP) et journalisée entité par entité. Toute valeur de `contents` non reconnue retombe sur `move` : un formulaire périmé ou forgé ne doit jamais détruire. **Le sous-arbre se lit par `_all_folders`/`_all_documents`, JAMAIS par `list_folders`/`list_documents`** : ces deux-là finissent par `except Exception: return []` — le bon réflexe pour une liste à l'écran, le pire pour une suppression. Une collection illisible se lirait « ce dossier ne contient rien », le dialogue afficherait « Ce dossier est vide » et les enregistrements de dossiers partiraient par-dessus des documents qui pointent encore dessus : le bogue du `folder_id` mort, réintroduit par la porte de derrière (piège trouvé en revue, le jour même). Les deux lecteurs dédiés propagent, et `delete_folder` transforme la propagation en refus net. **`record_deletion` se pose HORS du test de succès** — deux des retours d'échec ne sont pas atomiques et portent, à dessein, ce qu'ils ont déjà détruit (une suppression de blob qui casse au 13e fichier sur 40 en laisse douze définitivement partis, GCS et Firestore compris). Journaliser sous `if success:` jetait ce compte-rendu, et `list_deletions` — dont la fonction ENTIÈRE est de répondre « qu'est-ce qui a disparu ? » — répondait que rien n'avait disparu ; le trou était permanent, une reprise ne pouvant pas ré-énumérer des lignes détruites. Le compte-rendu ne liste QUE des suppressions commises, donc une exécution qui n'a rien détruit ne journalise toujours rien. Corollaire du même raisonnement sur le plafond : un échec *géré* se raconte, un **SIGKILL de gunicorn ne raconte rien** — d'où `MAX_FOLDER_DELETE_DOCUMENTS = 150` et non 200, `delete_document` coûtant TROIS allers-retours sérialisés par fichier au taux que `MAX_ZIP_FILES` (150 ≈ 15 s) établit, soit ~60 s pile pour 200 fichiers.
- **htmx 2.0.4 n'échange QUE les réponses 2xx : un fragment d'erreur rendu en 4xx ne paraît JAMAIS.** Son `responseHandling` par défaut est `[{code:"204",swap:false},{code:"[23]..",swap:true},{code:"[45]..",swap:false,error:true}]`. Le dépôt connaît la règle et la contourne au moins une fois (`routes/doc_templates.py:88` rend son fragment en 200 « precisely because a 422 fragment would silently never render »), mais elle ne vivait qu'en commentaire, et un lot neuf a remarché dessus le 2026-08-14 : chaque refus de la suppression de dossier de classement — le plafond « 342 fichiers, au-delà de la limite », l'aveu d'une destruction partielle — mourait à l'écran, bouton mort, rien qui bouge, l'utilisateur re-cliquant. La forme retenue là-bas est la plus simple : **l'échec emprunte la voie du succès** (redirection que le XHR suit, `document_list` rendant alors le fragment `_browser.html` avec `?erreur=` dans sa bannière — le message paraît ET la liste survit), plutôt qu'un fragment qui écraserait la zone d'échange par une seule ligne rouge. **Il reste ~28 branches `', 422'` dans `routes/*.py`** : la plupart sont des gardes inatteignables (un champ caché toujours posté), aucune n'a été touchée par ce lot — les vérifier une par une est un travail en soi, mais tout NOUVEAU message d'erreur destiné à être lu doit sortir en 2xx.
- **Le hub « Comptabilité » est un composeur de PRÉSENTATION — il ne gagnera jamais un chemin d'écriture, une requête Firestore nouvelle, ni une fusion des deux modules.** La consolidation du 2026-08-15 (décisions utilisateur : entrée de nav → page hub, deux sections, rétroports de parité inclus) a explicitement RÉFUTÉ l'unification profonde : ~15 chemins relatifs homonymes entre les deux blueprints (`/<tx_id>` attrape-tout compris) rendent une fusion à plat impossible, et les divergences de fond sont VOULUES (fermeture solde-nul vs libre, soldes gelés vs calculés à la lecture, horloge UTC vs Montréal, `VALID_METHODS` incompatibles, `_reconciliation_overdue` copié « free to diverge » — admin ayant `account_floor` en plus). **Les 12 paires de gabarits jumeaux restent jumelles** : un partial partagé entre `templates/trust/` et `templates/administration/` casserait les 5 smoke `web_rendu` mono-blueprint (`test_admin_integration.py:440+` n'enregistre que `admin_bp` — un `url_for('trust.…')` inconditionnel y lève `BuildError` ; le seul lien croisé existant, `administration/detail.html`, survit par son garde `{% if %}`) et le pin anti-fonctions-fléchées balaie tout `templates/administration/` alors que le worksheet trust utilise `el =>` légitimement. La parité de CÂBLAGE (OOB export+header, `hx-include` des selects de compte, type figé en édition, présélection de conciliation) est verrouillée par `tests/test_comptabilite_parity.py` — le watchdog qui rend exécutable la consigne « a fix on one side should be mirrored on the other » (un TEST, pas un script : la porte pytest l'exécute à chaque déploiement, la leçon `check_config.py`). Deux régimes de fixtures coexistent donc : les rendus du hub et des listes de comptes exigent les TROIS blueprints (`test_comptabilite.py`), les smoke admin restent mono-blueprint. Pièges connexes : la ceinture négative du portail (`test_portail_app.py`) inclut `/comptabilite/` ; le pin de nav exige exactement 2 `href="/comptabilite"` et interdit `/administration`/`/fideicommis` en dur dans `base.html` ; la tuile « Sommes en fidéicommis » du tableau de bord pointe toujours `trust.journal` (là où son chiffre vit — ne pas la « corriger » vers le hub) ; et une lacune préexistante documentée : le formulaire de compte trust ne collecte ni `dossier_id` ni `client_id`, donc un compte « spécial » ne peut pas se créer depuis l'UI (`models/trust.py:424-428` le refuse) — à combler seulement le jour où un compte spécial est réellement requis. **La revue adversariale du lot (7 confirmés/13) a ajouté quatre invariants, épinglés** : (1) `_reconciliation_overdue` lit le jour civil de **Montréal**, jamais UTC (la bande du soir allumait « Conciliation en retard » 4-5 h trop tôt quand `now − 30 j` enjambait une fin de mois — miroir des deux modèles, horloge gelée au test) ; (2) **un compte `fermé` n'est jamais « en retard de conciliation »** (aucune conciliation mensuelle n'est due après clôture — le prédicat ne connaît que des dates, le garde vit aux 4 points d'appel : les 2 instantanés + les 2 `_account_header`) ; (3) **« Concilier »/« Nouvelle conciliation » ne s'offrent pas sur un compte fermé** (les deux `reconciliation_new` ne listent que les actifs, et un `<select>` sans option correspondante retombe sur la PREMIÈRE — l'utilisateur concilierait le mauvais compte sans le voir) ; (4) le re-rendu 400 de la **création** de compte préserve le type soumis (`selected` sur la branche création des deux `account_form` — le rétroport « type figé » l'avait perdu, et son épingle littérale interdisait la réparation : les épingles sont désormais STRUCTURELLES). Pièges de test payés par la même revue : une assertion négative sur un montant se compose par `format_cents_fr`, jamais en littéral (le millier est un NBSP — un littéral à espace ASCII ne matche aucun rendu) ; une épingle OOB lie `hx-swap-oob` **au même tag** que l'id (quatre sous-chaînes indépendantes laissaient passer un header ré-émis sans l'attribut).
- **L'adresse d'un contact se choisit par `template_fields.selected_address`, JAMAIS par une lecture directe de `address_*`.** Les deux blocs d'adresse d'une partie sont peuplés par des chemins OPPOSÉS selon le type : le formulaire juriste masque tout le bloc « Coordonnées personnelles » pour une **personne morale** (`x-show="partieType === 'individual'"`), donc l'adresse d'une entreprise saisie côté juriste ne peut vivre que dans `work_address_*` — tandis que le **portail** écrit l'adresse d'une entreprise dans `address_*` (`routes/reception._CORRESPONDANCE`). Les deux sont légitimes. `_selected_address` essayait UN seul bloc selon le rôle et ne relisait jamais l'autre : une note d'honoraires pour un client personne morale imprimait « [CHAMP MANQUANT : destinataire.adresse_complete] » sur toute la famille d'adresse (le nom, lui, se résolvait — d'où un défaut discret). Le rôle décide désormais du bloc essayé EN PREMIER, l'autre sert de repli (critère : la rue, puis la ville) ; `_courriel` suivant le bloc retenu, le courriel professionnel d'une entreprise se répare du même coup. **Trois lecteurs partagent cette autorité** — les gabarits, l'accusé de réception du portail (`routes/taches_portail._adresse_lignes`) et le MCP `list_parties` — et le module reste PUR (aucun import Firestore), ce qui est précisément ce qui permet aux deux autres de l'importer. Deux asymétries à connaître : l'instantané `billing_address` d'une facture, lui, a toujours fait « travail sinon personnelle » (`routes/invoices._build_billing_address`), si bien que la fiche de facture affichait la bonne adresse pendant que le Word n'en avait aucune — et une entreprise **supprimée** s'imprimait correctement, le repli sur l'instantané recopiant l'adresse de travail dans les clés personnelles. Cas résiduel assumé : une entreprise créée au portail puis éditée par le juriste porte les DEUX adresses, et la préférence par rôle garde celle du portail. **Deux pièges que la revue du même jour a payés** : (a) `_courriel` suit le drapeau `is_work` du choix d'adresse — or ce drapeau ne signifie plus « ce rôle préfère le professionnel » mais « voici le bloc qui portait une adresse », si bien qu'un client n'ayant enregistré que son adresse de bureau perdait son courriel personnel ; le courriel a donc désormais SON repli ordonné (le champ préféré, puis l'autre — la forme de `_telephone`), et les deux surfaces publiques sont `selected_address` / `selected_email` ; (b) une composante d'adresse peut être une LISTE (vobject parse une ADR à virgule non échappée ainsi — le motif de `mcp.handlers._addr_str`), et un `.strip()` nu levait `AttributeError` : sur le chemin de l'accusé du portail, la levée arrivait APRÈS `poser_accuse`, donc le rejeu Cloud Tasks trouvait le marqueur posé et n'envoyait jamais le bordereau — l'au-plus-une-fois dégénérait en zéro-fois. `_address_block` coerce (`_address_component`) et le rendu de l'accusé est désormais enveloppé comme son envoi. Cinq lecteurs partagent l'autorité : gabarits, accusé du portail, MCP `list_parties`, et les exports CSV/PDF des contacts.
- **Gabarit placeholders are three-way, not "block vs scalar" (July 2026):** `classify_placeholders` returns **auto** (catalog/alias, case-insensitive — an ALL-CAPS name upper-cases its value), **manual** (`MANUAL_FIELDS` letter metadata), and **passthrough** (everything else). Passthrough — the former ALL-CAPS blocks, **`{{civilité}}`, `{{salutations}}`**, and unknown names — is deliberately **not resolved and not prompted**; the route omits it from the fill `values` so the raw `{{name}}` survives into the .docx for Word. Do **not** re-add civilité to the catalog or a `salutations` default: civilité must appear in letters but never in court procedures, so it is the user's to place. The route **re-classifies on every render** (`template_detail`, `_fields_context`, `_collect_values`), so a template uploaded before this change (whose stored `block_fields` still exists) classifies correctly without a re-upload.
- **Trust (Phase K): the register's "Solde" is the *book* balance; the *cleared* balance is a control and must never be displayed under that label.** Book = Σ(receipts) − Σ(disbursements) over ALL statuses (incl. `annulée`); cleared = receipts once `compensée` minus disbursements while `compensée`/`en_circulation`. `to_barreau_row`'s « Solde » is `balance_after_account` (journal) / `balance_after_client` (carte). The card/entry UI shows both, labelled « Solde aux livres » vs « Disponible (compensé) » — the gap is the deposits-in-transit and is the whole point of the two-step model.
- **An export link that sits OUTSIDE the HTMX swap target goes stale, and the export then silently covers the wrong thing.** A filter change swaps only its `hx-target` (`#trust-rows`, `#invoice-rows`, …); anything else on the page keeps the values of the initial render — so an « Exporter » URL built from `filters.*` in the parent template still carries the period the page loaded with. The export succeeds, the file looks right, and it covers the wrong range: exactly what shipped in the trust journal on 2026-08-11, where a filtered month exported the whole register. The fix is the house OOB pattern — the links live in their own partial, the parent hosts them in an id'd container, and the ROWS partial re-emits that container with `hx-swap-oob="true"` (guarded on `HX-Request`, and placed OUTSIDE the rows/no-rows branches so an empty period refreshes them too). `components/_export_dropdown_oob.html` is the generic one; `trust/_export_links.html` the trust journal's. **Whenever a list gains a filter that an export must honour, check which side of the swap boundary the link is on.**
- **A composite index's "complete inverse" flips EVERY field, the equality ones included — mixing an ASC equality with a DESC sort needs its own index.** Firestore can scan a composite index backwards, so `(account_id ASC, date ASC, sequence ASC)` also serves `(account_id DESC, date DESC, sequence DESC)`. It does **not** serve `account_id ==` + `order_by date DESC, sequence DESC`: an equality filter looks direction-free from the query side (you never write a direction for it) and is not, in the index, so that combination is a *third* ordering neither the index nor its inverse provides. `opening_book_balance` was written that way on 2026-08-11 to read a single document; every `/fideicommis/export/pdf` with a period answered **FAILED_PRECONDITION → generic 500**, and the local suite could not see it (mocked queries never consult an index). Two lessons: prefer an **already-exercised query shape** over a new one that saves a read — this one went back to `(account_id ==, sequence DESC)`, index #1, and pays the entries booked since the period, which reconciliation already reads; and when a new query shape *is* worth an index, the index deploys **before** the code (the standing rule), because the failure surfaces only in production. An export route should also **degrade** — `_journal_pdf` now catches both model reads and prints the register with a French notice about what is missing, since a book of account the lawyer needs is worth more shortened-and-labelled than replaced by an error page.
- **Trust: the « solde reporté » is exact ONLY because dates are non-decreasing in `sequence` order.** `opening_book_balance` reads back one frozen `balance_after_account` — it never re-sums — and that is sound only while the backdating guard (`antidatage_refusé`, inside the create transaction) and today-dated reversals hold. **An entry written outside `create_transaction`** (an import, a backfill, a hand edit, a reset `counters/trust-{account_id}`) breaks the invariant and makes the carried-forward balance wrong **in silence**. That is why the journal PDF cross-checks it against `implied_opening_balance(first_tx)` — the first entry's own frozen balance minus its own `compute_deltas` contribution — and prints a warning on the sheet when the two disagree, rather than shipping a register that looks authoritative and is not. Note also that the report **includes** an `annulée` entry whose reversal falls after the period (the register is chronological, so both legs count, §4.2) — correct, but it is why the report will not match a « net » reconstruction.
- **Trust: `sequence`, not `date`, is the register's order; a disbursement may only draw on the *cleared* balance.** Backdating (a date before the last entry's) is refused — correct a past error with a **reversal dated today**, never by rewriting history. The overdraft control (`check_disbursement_allowed`) lives INSIDE the Firestore transaction on the same read-set as the write (`create_transaction`), and is confirmed REQUIRED (user decision 2026-07-16 — do not relax to book-only). Reversals bypass the control; the create path refuses `purpose="correction"` so reversal stays the only way to mint one.
- **Trust: `annulée` entries count in the book balance** (they net with their reversal — the register is chronological, so entries #5–#11 must still show the balance as it stood) **and are excluded from the cleared balance.** Getting this backwards double-counts funds. `compute_deltas(direction, amount, status)` is the single arithmetic atom (spec §4.4); `in_transit = book − cleared` per client (annulée pairs net out, so no query needed).
- **Trust: `trust_transactions.date` and `.cleared_date` are date-only (midnight UTC)** — emit via `mcp.tools.date_str` in MCP output and render with `.strftime('%Y-%m-%d')` in templates, **never `to_mtl`/`iso_mtl`** (a Montréal shift moves them to the previous day). The register has **9 export columns, not 8** — column 7 « Recette / Crédit » is split into two per-direction columns « Recette » (recettes) / « Crédit » (déboursés) per the Barreau sheet (user decision 2026-07-16), diverging from spec §8; `to_barreau_row` + the export column lists carry this.
- **Portail (L1): a Cloud Tasks `app_engine_http_request` enqueue needs `appengine.applications.get`, not just `cloudtasks.enqueuer`.** The enqueuing SA must ALSO hold `appengine.applications.get` (via `roles/appengine.appViewer`) or `create_task` fails `PERMISSION_DENIED` ("App Engine targets require appengine.applications.get"). This failed SILENTLY end-to-end at first launch: the portal's direct enqueue threw, `api_finaliser` swallowed it (by design — the envelope is durable, §7.4), the client got the confirmation page, but no manifest/accusé/status-change happened. The **15-min reconciliation cron rescued every stuck lot** (it runs on the default service as the main SA, whose `editor` role already carries the permission) — the R-4 safety net working exactly as designed, just with a ≤15-min delay instead of seconds. Fix = grant `portail-svc` `appengine.appViewer`; then the direct path processes in seconds. Symptom to recognize: submissions appear in Réception ~15 min late and `tache_enfilage_echec` fires on the `portail` module while `reconciliation_reparation` fires on `default`.
- **Portail (L1): the portal process must NEVER import `models`/`security`/`config`.** `models/__init__.py` constructs the default-database Firestore client at import and `config.py` resolves the MAIN service's required secrets at import — either would make the portal service depend on permissions its least-privilege SA deliberately lacks. The portal imports only `client/*` + `utils/logging_setup`/`tracing_setup`/`validators`; `client/config.py` holds the shared constants with **lazy** secret functions, importable by BOTH services. The reverse direction is fine (the main service imports `client.config`, `client.services.taches`).
- **Portail (L1): machine endpoints get their OWN CSRF-exempt blueprint and header-gated edge bypasses.** A Cloud Tasks POST to the main service is blocked three times by default (CSRF 400 → origin-secret 403 → appspot-host 403). The fix is `csrf.exempt(taches_portail_bp)` (a dedicated blueprint — never widen a browser blueprint) plus `is_appengine_internal_request()` bypasses in `_enforce_origin_secret`/`block_appspot`, safe because App Engine strips `X-AppEngine-*` from all external traffic. Never bypass on anything else; the handlers re-check the exact header value.
- **Portail (L1): the invitation document is readable by the PUBLIC service.** `display_label` is the only designation the client ever sees — no dossier title revealing the opposing party, no internal memo, nothing beyond the necessary goes into an invitation (spec §5). Same discipline in logs: `pallas.portail` events carry IDs and counts only (a client email or file name is never auto-redacted for names). The log name `pallas-athena` is SHARED by both services — filter by `resource.labels.module_id` (`default` vs `portail`).
- **Chaque file du portail a SON courriel d'invitation, et le renvoi doit suivre le type.** `services/portail_emission._corps_invitation` prend l'invitation ENTIÈRE et choisit son gabarit (`_COURRIELS`) : une invitation « intake » recevait sinon le texte « documents » — objet « Transmission de documents », corps énumérant les formats de fichiers admis — alors que le lien mène à un formulaire. Le piège n'était pas à l'émission mais au **renvoi** : `renvoyer_invitation` était aveugle au type, et ce chemin est atteignable depuis le bouton « Renvoyer » de Réception **et** depuis le « Le lien ne fonctionne pas ? » que le CLIENT actionne. Corriger cette seule fonction couvre les quatre points d'appel de `routes/`.
- **Le pied des courriels d'invitation est un `{% include %}` partagé, à dessein.** `templates/reception/_invitation_pied.html` porte la distinction entre la durée du **lien** (heures, usage unique) et celle de l'**invitation** (14 jours), plus l'**URL de secours** — les lignes qu'un lot de correctifs entier a values (le courriel promettait que *le lien* durait 14 jours, ce qui était faux et condamnait des clients). C'est le premier `include` de la famille des courriels, dont les deux bordereaux dupliquent au contraire leur pied : la dérogation est assumée parce que ce qui est partagé ici est un invariant de correction, et que le dupliquer accepterait qu'une retouche future le rétablisse dans une seule des deux files. Les annexes A.1 des specs L1/L3 portent encore la phrase fautive — un commentaire Jinja interdit de la « restaurer ».
- **Portail (L1): the accusé email is at-most-once BY DESIGN.** `poser_accuse` (transactional test-and-set on `accuses[batch]`) is the single guard of the single non-idempotent effect. A send failure after the marker is set is logged (`courriel_echec`, ERROR) and NOT retried — a retry could never resend (the marker is set) and would only burn queue attempts. Every reconciliation repair logs `reconciliation_reparation` at ERROR on purpose: it means the queue lost work.
- **Portail (L1): the envelope is the durable truth; an enqueue failure never fails a finalization.** `envelope.json` is written create-only (`if_generation_match=0` — replayed finalization → 409) BEFORE the task is enqueued; if the enqueue fails the client still gets the confirmation and the 15-min reconciliation cron replays the batch. Never "fix" the ordering.
- **App Engine Standard PLAFONNE toute réponse ET toute requête HTTP à 32 Mo — un fichier ne doit JAMAIS transiter par l'application, dans AUCUN sens.** Sortant : la route de téléchargement de la quarantaine faisait `send_file(blob.open("rb"))` — correct en local, coupé net en production dès que l'objet dépasse le plafond (2026-08-12, un lot de 68 et 128 Mo — des ZIP renommés `.docx` pour contourner l'ancien filtre — répondait **500 sans AUCUNE ligne applicative** ; le seul indice était le WARNING plateforme « Response size was too large »). Entrant, le miroir exact : le POST multipart de `/documents/upload` ne pouvait physiquement pas porter plus de 32 Mo. Les DEUX sens sont GCS-directs depuis le 2026-08-12 : remise = **URL signé V4** (`models/document.sign_blob_url` + `build_attachment_disposition`, réutilisés par `get_signed_url` ET `routes/reception.telecharger`) ; téléversement du juriste = session reprenable sous `staging/{uid}/` + finalisation (`/documents/api/televersement|finaliser`) ; ingestion (versement de Réception ET finalisation du formulaire) = **copie par rewrite** (`ingest_blob_as_document` — sonde de 512 octets pour le sniff, jamais le corps ; SHA-512 de Réception recalculé EN FLUX par tranches de 8 Mio), plafond documents 200 Mo (pure politique — décision utilisateur 2026-08-12) ; et depuis le 2026-08-13, l'archive ZIP d'un dossier de classement = **composition EN FLUX dans GCS** (`build_folder_zip_url` — voir son bullet pour les pièges `ignore_flush`/`chunk_size` et les deux plafonds anti-SIGKILL). Les `send_file` restants (gabarits, impression de note) servent des `.docx` générés ≤ 10 Mo — sous le plafond par construction. Ops : une règle de cycle de vie du bucket canonique balaie `staging/` (âge 7 j) — un staging jamais finalisé est un orphelin inerte. Le symptôme à reconnaître : des 500 sur une route de fichier sans traceback nulle part.
- **Portail : le LOT (`batch`) est frappé à la CRÉATION DE SESSION — jamais au premier téléversement.** La session est un cookie signé : la première vague de `/api/televersement` part en PARALLÈLE avec le même témoin initial sans lot, et chaque requête frappait alors LE SIEN (`horodatage_utc_compact`, résolution à la seconde). À cheval sur un changement de seconde, les objets GCS se répartissaient sur DEUX lots, le cookie final n'en gardait qu'un, et la finalisation répondait « Requête invalide. » sans issue (2026-08-11, journaux de production : lot de 18 h 12 coupé 14/6 — le client s'en est tiré en rechargeant la page et en retransmettant —, lot de 19 h 26 resté coincé). Depuis le correctif : `creer_session` frappe le lot (type documents SEULEMENT — l'intake frappe le sien à sa propre finalisation), `_purger_lot` en refrappe un NEUF aussitôt (ré-entrée D-2), et le minting dans `api_televersement` n'est plus qu'un repli pour les sessions antérieures. Le JS de `documents.html` SÉRIALISE de surcroît les POST `/api/televersement` (file de promesses `reserver`) : des POST parallèles portent tous le même cookie, donc ils lisaient le même `seq` (fichiers homonymes en collision d'objet) et le même `files_count` (une vague de 20 fichiers ne consommait ~1 du quota). Les PUT vers GCS restent parallèles — seule l'ouverture de session (quelques ms) fait la file. Règle générale : toute route du portail qui MUTE la session doit supposer des appels parallèles porteurs du même témoin.
- **Portail (L1): versement restricted to the documents vocabulary — 11 MIME types since 2026-08-13, ≤ 200 MB since 2026-08-12** (magic-byte sniff). The 2026-08-11 user decision widened the original 6 (2026-07-25) with **ZIP, .eml and .msg** — versable AND accepted at the portal (`PORTAIL_EXTENSIONS` gained `zip`, ending the v1 « décision D-4 » archive refusal; `.eml`/`.msg` were already portal-accepted, just never named in the texts); 2026-08-12 raised the size ceiling to 200 MB by making the versement a **GCS-side copy** (`ingest_blob_as_document`); 2026-08-13 added **Excel** (.xls via the OLE arm, .xlsx via the PK arm of `_sniff_header` — already portal-accepted under « documents Office », whose portal texts therefore did NOT change). A HEIC/PPTX/MP4 (or anything past 200 MB, the portal's own per-file cap) remains downloadable from Réception (attachment forced) but not versable; **do not widen `ALLOWED_MIME_TYPES` further without a new user decision.** « Verser » keeps its guards : `blob.reload()` + size refusal BEFORE any read (the manifest's `size_gcs` is frozen at hashing time), the SHA-512 recomputed IN STREAM (8 MiB slices — the object never sits whole in RAM) against the manifest (mismatch → French refusal + `versement_divergence` ERROR; the document description only ever cites a hash the app itself verified). Marking a lot processed PURGES the quarantine files, so every « reçu » file requires an explicit verser/refuser decision first.
- **Trust: the module fails CLOSED.** `create_transaction`/`clear`/`reverse`/`reconcile` abort on any read failure (never a partial write); list views propagate read errors to the route (never a silently-empty register). `update_dossier` re-reads the three trust map fields at the last moment before its full-doc `set()` so a form save can't clobber a concurrent trust write with a stale (possibly overdraft-permitting) cleared balance. A dossier that ever held a trust entry can **never be deleted** (`trust_transactions` in `_CHILD_COLLECTIONS`) — archive it.
- **Trust: a reconciliation is anchored to `period_end`, NEVER to now** (2026-07-29 — the fix that made RETROACTIVE reconciliation possible). The variance gate and the worksheet both consume `reconciliation_as_of_context`: book = the frozen `balance_after_account` of the last entry dated ≤ period_end (exact because dates are non-decreasing in sequence order — the backdating guard + reversals dated now); outstanding/in-transit = en_circulation dated ≤ period_end **plus the resurrection sets** (entries compensées with `cleared_date` > period_end, and annulée originals whose reversal postdates it) — those are counted as outstanding-as-of but are **never tickable** (already terminal). `complete_reconciliation` **increments** `bank_balance` by the ticked deltas (never sets it to the statement — false for a retro period) and uses an account-**etag sentinel** as the concurrency check; `rec.book_balance` snapshots the **as-of** figure. An unticked cheque stays `en_circulation` and carries to the next period's worksheet (cross-statement clearing, pinned by `test_cross_period_outstanding_cheque`). A brouillon is deletable via « Abandonner » (`delete_reconciliation`, brouillon only) — before that, an unbalanceable draft blocked the account forever. `period_end` refuses the future; a blank statement amount errors while the **literal 0 stays legitimate** (an emptied account reads exactly 0,00 $ — never re-add the `or 0` coalescing).
- **Administration ≠ fidéicommis : les trois mécanismes retirés le sont PAR DESSEIN — ne pas les « réparer ».** `models/admin_ledger.py` n'a NI garde d'antidatage, NI soldes gelés par ligne, NI contrôle de découvert (décisions utilisateur 2026-08-13) : la date économique est libre dans le passé, les soldes se calculent à la lecture en ordre `(date, sequence)` (`running_balances` + `opening_ledger_balance` — qui LÈVE sur troncature : une somme partielle est un solde faux), et le verrou est la **conciliation complétée** (période entière gelée ; ensuite contre-passation seulement). Corollaires qui échouent en silence si on les oublie : **la colonne Solde ne s'affiche/exporte JAMAIS sur une vue filtrée par type/statut/catégorie** (un solde courant sur un sous-ensemble est un chiffre faux — on le cache plutôt que mentir) ; **compenser une écriture régénère l'etag du COMPTE** même si aucun solde ne bouge (la sentinelle de complétion doit le voir) ; la complétion re-vérifie **statut ET etag** de chaque écriture cochée (les dates sont modifiables jusqu'à ce verrou précis — trust pouvait s'en dispenser, pas ici) ; **une compensation datée DANS une période conciliée est refusée** (`compensation_période_verrouillée` — elle ferait mentir l'ensemble de résurrection et la re-preuve de la conciliation) ; **une écriture de correction ne se contre-passe JAMAIS** (`correction_non_contre_passable` — la ventilation copiée double-compterait aux rapports et une facture ne peut pas se re-lier ; l'annulation d'une contre-passation erronée est une NOUVELLE écriture) ; **toutes les gardes de dates lisent l'horloge de MONTRÉAL — ET les VALEURS PAR DÉFAUT qu'elles jugent aussi** (`today_mtl` — création, compensation, `period_end` de conciliation, le « today » des formulaires, et la date par défaut de la contre-passation : un `datetime.now(utc)` refuserait ou estampillerait demain chaque soir après 20 h, la classe du 2026-08-02. Le piège s'est refermé le 2026-08-14 : la garde de `reverse_transaction` lisait Montréal pendant que sa date par défaut venait d'UTC, donc **toute contre-passation sans date explicite se refusait elle-même quatre heures par jour** — attrapé par le harnais à horloge gelée le lendemain de la livraison, ce qui est précisément à quoi sert un `today_mtl` monkeypatché) ; et **la ventilation TPS/TVQ des rapports est SIGNÉE** (`routes/admin_ledger._ventilation_signed` — une correction-recette, contre-passation d'une dépense, porte sa ventilation en NÉGATIF pour que Σ TPS/Σ TVQ nette une dépense contre-passée à zéro : sans le signe, le bloc CTI/RTI réclamerait des crédits de taxe sur un achat annulé).
- **« payée » ne se POSE plus à la main, et la sortie qu'on lui a rendue est CONDITIONNELLE — sans quoi elle ouvrait pire qu'elle ne fermait** (2026-08-17). Dix-neuf factures sur quarante-trois portaient ce statut sans le moindre montant : le bouton « Marquer comme payée » était la porte que le retrait du formulaire d'encaissement venait de fermer. Il a disparu de `STATUS_TRANSITIONS` ; **la bascule automatique de `record_payment` n'est PAS touchée**, elle écrit `status` directement dans sa transaction (`invoice.py:1086-1092`) sans passer par la table — c'est la moitié qui aurait cassé en silence, et elle est épinglée. En retour `payée` cesse d'être un cul-de-sac, sans quoi ces dix-neuf resteraient hors d'atteinte de tout encaissement (`create_transaction` refuse `facture_non_émise` hors `envoyée`/`en_retard`). **Mais la réouverture nue aurait été un trou** : `void_invoice` (`invoice.py:1149`) ne regarde QUE le statut, jamais `amount_paid`, donc `payée → envoyée → annulée` aurait libéré en deux clics les heures et dépenses d'une facture réellement encaissée — sans que rien ne le voie, l'annulation ne touchant pas `amount_paid` —, et le `amount_due` FIGÉ aurait reparu à pleine valeur dans `get_outstanding_total`, au tableau de bord comme au connecteur. D'où **`available_transitions`**, lue par le modèle ET par la fiche. `en_retard` a gagné au passage une sortie vers `envoyée` : rien n'écrit ce statut automatiquement, et sans elle une erreur ne se corrigeait plus que par l'annulation. **Conséquence assumée (décision 2026-08-17) : il n'existe aucune radiation de créance** — une facture irrécouvrable reste « envoyée » avec son solde, ce qui est comptablement vrai jusqu'à radiation formelle ; la marquer « payée » affirmait qu'on avait reçu l'argent, et c'était déjà faux.
- **La provision de l'ancien système est un piège à double comptage, et la garde de saturation ne la voit pas** (2026-08-17). L'ancien logiciel imputait la provision du client **à l'émission** : le `amount_due` d'une facture reprise portant une `retainer_applied` est déjà NET de cet argent, et cet argent est le `virement_honoraires` qui a quitté le fidéicommis. Y porter un encaissement le compterait deux fois. Le piège n'est pas seulement le refus `encaissement_excède_solde` — **il est silencieux quand le dû résiduel dépasse le virement** : `256401-01` (provision 500,00 $, dû 600,68 $, virement 500,00 $) passait toutes les gardes. D'où un refus explicite sur `retainer_applied > 0`, aux DEUX étages de `reprise_encaissements` (la proposition ne l'offre pas, la vérification le refuse — le CSV est éditable). Le déblocage passe par `scripts/corriger_provisions_factures.py`, qui retire la provision et rend le dû égal au total ; le solde final est identique au cent près, seule la forme change (`256501-01` : 1 149,75 − 750,00 = 399,75 $ dans les deux idiomes). **Sa règle est `provision ≤ Σ virements ≤ total`, jamais l'égalité stricte** — sur `2026-012-01` la provision ne couvre que le PREMIER des deux virements, le second acquittant le solde. Et c'est une **écriture hors modèle assumée** : il n'existe aucun `update_invoice`, `retainer_applied`/`amount_due` ne s'écrivant qu'à la création — ce qui rend le script acceptable n'est donc pas la prudence de l'écriture (deux champs) mais l'exactitude prouvée du choix.
- **Un virement d'honoraires peut acquitter PLUSIEURS factures — la clé d'idempotence est le couple, jamais le virement seul** (2026-08-17). Celui de 1 505,92 $ de M. Duon-Sauvé couvre deux factures au cent près ; celui de 527,30 $ de M. Fuchs se répartit sur les deux siennes. `reprise_encaissements` cherche donc par `(trust_transaction_id, invoice_id, montant)` via `list_by_trust_transaction` — chercher par virement seul ferait passer la seconde jambe pour déjà faite. La somme des lignes d'un virement doit égaler son montant **au cent près**, sans quoi il entrerait moins d'argent que le fidéicommis n'en a sorti et l'écart n'apparaîtrait qu'à la conciliation. Dans le CSV, une cellule **vide** vaut le virement entier (le cas nominal) et une cellule **illisible** est REFUSÉE : retomber sur le montant entier imputerait en silence bien plus que voulu. **Limite consignée** : un virement partagé un jour contre-passé au fidéicommis ne verrait qu'UNE de ses écritures annulée, `_contrepasser_recette_administration` rendant un succès sans bannière.
- **Le compte d'opérations est un livre PARTIEL tant que ses dépenses ne sont pas saisies** (2026-08-17). La reprise des honoraires (`scripts/reprise_encaissements.py`) n'inscrit que le **côté crédit** : 34 343,39 $ de recettes sur onze mois, en regard de ~27 $ de dépenses — aucun loyer, aucune assurance, aucune cotisation. Le solde affiché est donc **nettement surestimé**, et `complete_reconciliation` exigeant une variance exactement nulle, la première conciliation postérieure au 2025-08-31 **échouera** — proprement, jamais en silence — tant que le côté débit manque. Aucune conciliation déjà complétée n'est affectée (elles s'arrêtent au 2025-08-31, le premier virement est du 2025-09-12), donc le contrôle nº 6 de `verify_admin_integrity` reste vert. Ne pas lire ce solde comme la trésorerie du cabinet.
- **Le registre d'administration est le SEUL écrivain d'`amount_paid` (depuis le 2026-08-17) — et il écrit toujours « courant + delta », jamais un SET aveugle.** `record_payment` ÉCRASE (`invoice.py:702`) ; l'orchestration (`routes/admin_ledger._projeter_paiement`/`_reduire_paiement`, réutilisée par le fidéicommis) relit `amount_paid` juste avant l'appel et passe le cumul, si bien qu'une correction survit aux encaissements suivants — c'est ce qui fait que deux encaissements sur une même facture s'ADDITIONNENT au lieu de s'écraser, propriété devenue essentielle maintenant que tout paiement passe par là. Une réduction (contre-passation) repasse le `paid_date` EXISTANT (record_payment le nullifie lui-même à zéro). En cas d'échec de la projection, **l'écriture TIENT** (le registre est le livre de référence) et une bannière `?avertissement=facture` demande de contre-passer puis de ressaisir — jamais de blocage, jamais d'écrêtage silencieux (elle renvoyait au formulaire de la facture, qui n'existe plus). Le cumul reste recomputable (`sum_invoice_receipts`) et `verify_admin_integrity` signale désormais la dérive en **erreur** : plus aucun second écrivain ne l'explique, et un écart signifie que le compte d'opérations est faux d'autant. Son contrôle nº 8 balaie TOUTE facture portant un montant encaissé — il n'examinait que celles déjà citées par une écriture, donc un paiement saisi hors comptabilité (zéro écriture, zéro comparaison) lui échappait : c'est ainsi que 5 397,36 $ ont manqué au registre pendant un mois.
- **Le virement d'honoraires du fidéicommis crée sa recette d'administration au niveau ROUTE, fail-open, et la recette se contre-passe DEPUIS le fidéicommis.** `routes/trust.entry_create` appelle `_creer_recette_administration` APRÈS le commit du virement (kind `encaissement_facture` si facture Athéna — ce qui enchaîne la projection Lot P — sinon `recette_autre`) ; un échec = bannière, jamais un blocage du virement. Côté administration, contre-passer une recette portant `trust_transaction_id` est REFUSÉ sans le drapeau `allow_linked` (le fidéicommis est la source de vérité du mouvement) — c'est `routes/trust.entry_reverse` qui contre-passe les deux côtés. **Depuis la revue 2026-08-13, trust plafonne lui aussi sur le solde VIVANT** (`amount_due − amount_paid` — le plafond figé sur `amount_due` datait d'avant Lot P et aurait laissé un virement prendre PLUS des fonds du client que la facture ne doit encore ; `_factures_emises` affiche le même solde vivant et n'offre plus une facture soldée), si bien que le virement et l'encaissement automatique passent ou échouent ENSEMBLE.
- **`_normalize` must never inject a key the caller did not supply — it did, and every PARTIAL update of a partie was destructive.** `models/partie.update_partie` merges `{**existing, **data}` then writes the FULL document, so a key present-but-empty **erases** while an absent key survives. `_normalize` set `data["mandataires"] = cleaned` unconditionally, so `update_partie(pid, {"email": …})` silently wiped the stored mandataires list — latent in `update_kyc_status`/`link_kyc_document`, and the nominal path for L3's field-by-field apply. The normalization is now gated on `if "mandataires" in data`. **Apply the same reasoning to any future normalizer**: on a full-document-set model, injecting a default IS a deletion.
- **A vCard property that is written but never READ gets erased by the first DAV PUT.** Same non-effacement rule as the hearing `CONFERENCE` property: `vcard_to_partie` **omits** `birth_date` when `BDAY` is absent rather than returning `None`, because the merge treats a present key as an instruction to overwrite. A CardDAV client that does not carry BDAY would otherwise delete the stored date server-side on a plain contact edit. `birth_date` is a **date-only value at midnight UTC** — render with `strftime`, **never `to_mtl`** (Montréal moves it to the previous day), and it is deliberately **not** exposed through MCP (`get_partie` builds a whitelist payload, so the outputSchema is untouched).
- **The portal session IS a signed cookie, and overflowing it fails SILENTLY.** Browsers cap a cookie at ~4096 bytes and simply drop anything larger — the client loses their session mid-form while their single-use link is already spent, so the loss is unrecoverable. The intake draft therefore carries BOTH per-field character bounds (`INTAKE_*` in `client/config.py`) AND a hard **byte** guard (`INTAKE_BROUILLON_MAX`, refused with a French message). The two are not redundant: bounds count characters, the cookie counts bytes, and « é » is two — a fully-accented saturated form clears every bound and still overflows. Two traps when re-measuring: a repeated character compresses to nothing (itsdangerous zlib-compresses, so a naive test measured 195 bytes and proved nothing — use incompressible filler), and the cookie also carries the identity keys and the CSRF secret.
- **Portal statut vocabulary lives in `client/config.py` because BOTH services depend on it.** `STATUTS_SESSION` (upload/session allowed — includes `soumise` since D-2) and `STATUTS_FERMES` (`révoquée`/`refusée`/**`traitée`**) are imported by the portal AND the main service. A per-service copy drifts, and the drift **reopens a closed invitation**: `ajouter_soumission` promotes the statut to `soumise`, which is upload-capable, so a late task or a reconciliation replay would undo a revocation. That promotion is now gated on `statut not in STATUTS_FERMES`.
- **`_garde`'s type gate is a table, not an equality — and a route missing from it is refused.** `client/routes._TYPE_REQUIS` maps endpoint → required invitation type (`None` = both, as for `/confirmation`); `_TYPE_REQUIS.get(endpoint, "")` yields `""`, which no invitation type equals, so a new guarded route left out of the table fails closed rather than opening to both flows. Two other places assume a type and must move together: `creer_session`'s `suivant` and `entree()`'s redirect, both routed through `_PAGE_DU_TYPE`. **Any new session key must also join `_refus`'s pop tuple** (`intake` did) — otherwise a refusal leaves one client's draft behind for the next visitor on that device.
- **`/entree` may only reuse a live session for ITS OWN invitation.** Every invitation email points at `/entree?i={id}` (the Firebase link and the fallback URL both), so a `?i=` naming a different invitation is the ordinary arrival of a second client on a shared browser. Reusing the cookie there dropped that visitor inside the previous holder's session — files written under the wrong prefix, and the accusé (names, sizes, SHA-512) mailed to the wrong client. `_garde` proves the INVITATION is live, never that the VISITOR is its invitee. Reuse is gated on `?i=` being empty or equal; the session is still never cleared (a foreign URL must not kill an upload in progress).
- **A 409 on finalisation is a SUCCESS from the client's side, and must purge the session.** The envelope already exists, so the lot is acquired. This is the recovery path of the most ordinary failure — the response lost on a flaky mobile link, the browser re-arming the button — and answering with an error left `session["batch"]` naming an already-manifested lot: later uploads landed there unhashed, unlisted and purged at « traiter », every submit re-409'd forever, and the quota counted the lot twice.
- **An intake task must dispatch on the INVITATION, never on the envelope.** In `routes/taches_portail`, the `envelope.json` read sits inside the idempotence short-circuit (`if manifeste.exists(): …`), so a branch keyed off the envelope runs only on the first attempt — never on a queue replay nor a reconciliation repair. And an unreadable intake envelope must still set **both** `soumissions[]` and `accuses[batch]`: those two are the reconciliation's only completion criteria, so a lot that sets one is re-enqueued every 15 minutes forever.
- **Creating a partie from Réception must `bump_ctag("parties")` — nothing else will.** CTag bumping lives in the route layer (`routes/parties.py` does it at its three write sites); `models/partie.py` never bumps. An ouverture that creates the client fiche or the declared adverse contacts without bumping leaves them in Firestore, visible in the app, and **never in the DavX5 address book**, with no error anywhere. One bump per request covers all the contacts it created.
- **`utils/rapprochement.candidats` proposes, it never decides.** Legal forms and civilities are stripped from BOTH sides (otherwise « Béton Nord » ↔ « Béton Nord inc. » is missed and « Me Jean Tremblay » ↔ « Me Paul Gagnon » is a false positive), and a single common token only counts when it constitutes one of the two names. Do not grow this into a conflict detector: a missed match is not a green light, a proposed match is not a conflict, and the ethical check remains the lawyer's. A test pins even the absence of an `est_en_conflit()`.
- **A field submitted EMPTY never erases the stored value.** In Réception's field-by-field apply, only ticked boxes with a non-empty submitted value enter the payload — a client's silence is not a retraction, and an unticked box is not an instruction to delete. This works only because `update_partie` merges; combined with the `_normalize` trap above, an injected empty key would delete instead.

---

## Infrastructure & Deployment

### Current deployed configuration

- **Domain:** `athena.poirierlavoie.ca` (Athena app), `poirierlavoie.ca` (firm website, separate)
- **SSL:** Cloudflare Full Strict with 15-year Origin Certificate (RSA, PKCS#1 format after OpenSSL conversion on Windows)
- **Network ingress:** App Engine firewall restricted to **Cloudflare's published IP ranges** — all traffic must transit Cloudflare. Paired with the in-app `X-Origin-Auth` origin-secret check and the appspot Host check (see Security Rules → Edge defense in depth).
- **Edge security (state verified 2026-08-11, not aspiration):** App Engine firewall = the 22 published Cloudflare ranges + `0.1.0.2/32`, default `DENY` ✓ · Configuration Rule « MCP API » disabling Browser Integrity Check on `/mcp` + `/oauth/*` ✓ · portail rate limit ✓ · Rocket Loader `off`, Early Hints `on`, SSL `strict`, min TLS 1.3 ✓ · **Cloudflare Access: none, by decision 2026-08-11** · **origin secret: ARMED 2026-08-11** · **edge HSTS aligned to the origin's 2 years, 2026-08-11** · apex `poirierlavoie.ca` has no address record, so HSTS preload reads `rejected`
- **Secrets:** Google Cloud Secret Manager — `flask-secret-key`, `firebase-api-key`, `dav-password-hash`, `portail-secret-key`, `graph-client-secret` (resolved by `config.py` at startup in production). `cf-origin-secret` was created 2026-08-11 (43 ASCII chars, byte-identical to the Transform Rule). All five existing payloads verified free of stray whitespace (the trailing-newline trap below).
- **Email:** MTA-STS policy setup for `poirierlavoie.ca` via Cloudflare Worker
- **MCP edge/GCP prerequisites (Phase I — perform before connecting Claude):**
  1. Firestore TTL policies (garbage collection only; expiry stays enforced in code):
     ```bash
     gcloud firestore fields ttls update expire_at \
       --collection-group=oauth_codes --enable-ttl --project=athena-pallas
     gcloud firestore fields ttls update expire_at \
       --collection-group=oauth_tokens --enable-ttl --project=athena-pallas
     ```
  2. `firebase deploy --only firestore:rules --project athena-pallas` (deny-all covers the new collections).
  3. No Cloudflare Access application exists (decision 2026-08-11). If one is ever added for `/dav/*`, it must match that prefix ONLY — `/mcp`, `/oauth/*` and `/.well-known/oauth*` must never be behind Access.
  4. Cloudflare **Configuration Rule** on `(starts_with(http.request.uri.path, "/mcp") or starts_with(http.request.uri.path, "/oauth/") or starts_with(http.request.uri.path, "/.well-known/oauth"))` disabling Browser Integrity Check; watch Security → Events for Super Bot Fight Mode challenging Anthropic's egress and relax "Definitely automated" if needed.
  5. Verify the `X-Origin-Auth` Transform Rule is zone-wide (it must cover the new paths).
  6. Connect from claude.ai: Settings → Connectors → Add custom connector → `https://athena.poirierlavoie.ca/mcp` → Firebase login + MFA → « Autoriser ».

### CI/CD — `cloudbuild.yaml`

```yaml
steps:
  # Step 1: Install dependencies and run tests — a failing test aborts the
  # build before the deploy step runs.
  - name: 'python:3.13-slim'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        set -euo pipefail
        pip install --require-hashes --no-deps -r athena/requirements.txt
        pip install -r athena/requirements-dev.txt
        cd athena && python -m pytest tests/ -q

  # Step 2: Deploy to App Engine
  - name: 'gcr.io/cloud-builders/gcloud'
    args: ['app', 'deploy', 'app.yaml', '--quiet', '--version=$SHORT_SHA']
    dir: 'athena'

  # Step 3: Conditional cleanup (keeps the 3 most recent versions)
  - name: 'gcr.io/cloud-builders/gcloud'
    entrypoint: 'bash'
    args:
      - '-c'
      - |
        OLD_VERSIONS=$$(gcloud app versions list \
          --service=default \
          --filter="traffic_split=0" \
          --format="value(version.id)" \
          --sort-by="~version.createTime" | tail -n +4)
        if [ -n "$$OLD_VERSIONS" ]; then
          gcloud app versions delete $$OLD_VERSIONS --quiet
        else
          echo "Cleanup skipped: No versions beyond the safety buffer found."
        fi

timeout: '1200s'
options:
  logging: CLOUD_LOGGING_ONLY
substitutions:
  _SERVICE_NAME: default
```

Deploys are tagged with `$SHORT_SHA` and the cleanup keeps the 3 most-recently-created non-serving versions. Triggered by GitHub push to main. **The pytest suite is a hard deploy gate** — the hash-locked install reproduces the production dependency set exactly.

### GitHub-side CI (`.github/`)

Security scanning runs on GitHub, independent of Cloud Build:

- `codeql.yml` — CodeQL static analysis (push/PR + weekly)
- `osv-scanner.yml` — OSV vulnerability scan of the lockfile (push/PR/merge-group + weekly); requires exact pins in `requirements.in`
- `trivy.yml`, `bandit.yml` — repo/config and Python-security scans (push/PR + weekly)
- `dependency-review.yml` — blocks PRs introducing known-vulnerable deps
- `scorecard.yml` — OpenSSF Scorecard supply-chain posture
- `dependabot.yml` — weekly grouped minor/patch PRs for pip (`/athena`) and GitHub Actions

### IAM requirements

The Cloud Build service account (`firebase-adminsdk-fbsvc@athena-pallas.iam.gserviceaccount.com`) needs:
- `roles/iam.serviceAccountUser` on `athena-pallas@appspot.gserviceaccount.com`
- `roles/appengine.appAdmin` on the project

The App Engine default service account (`athena-pallas@appspot.gserviceaccount.com`) needs:
- `roles/logging.logWriter` (Cloud Logging) and `roles/cloudtrace.agent` (Cloud Trace)
- `roles/secretmanager.secretAccessor` on the four application secrets
- `roles/iam.serviceAccountTokenCreator` **on itself** (member and resource are both `athena-pallas@appspot.gserviceaccount.com`) — required for the `iam.signBlob` self-impersonation that signs Firebase Storage URLs (`models/document.py`, `models/doc_template.py`). Without it, every document/gabarit upload and download **silently fails to produce a signed URL** on App Engine (local dev using a service-account JSON key signs locally and never hits this path, so the gap only surfaces in production).

### `app.yaml` (current, abridged)

```yaml
runtime: python313
instance_class: F2
automatic_scaling:
  min_instances: 0          # cold start after idle (several seconds of imports);
                            # warmup softens it; set 1 to eliminate at the cost
                            # of one always-on F2 instance
  max_instances: 2
  target_cpu_utilization: 0.65

inbound_services:
  - warmup                  # App Engine sends /_ah/warmup before routing live traffic

# Explicit sizing — gunicorn's defaults (1 sync worker, 30 s timeout) cap the
# app at ~2 concurrent requests and SIGKILL slow DAV/dashboard requests.
entrypoint: gunicorn -b :$PORT --workers 2 --threads 4 --timeout 60 --graceful-timeout 30 main:app

env_variables:
  ENV: "production"
  FIREBASE_PROJECT_ID: "athena-pallas"
  FIREBASE_APP_ID: "..."
  FIREBASE_STORAGE_BUCKET: "athena-pallas.firebasestorage.app"
  AUTHORIZED_USER_EMAIL: "<authorized-email>"
  RECAPTCHA_ENTERPRISE_SITE_KEY: "..."
  REQUIRE_MFA: "true"
  PIP_REQUIRE_HASHES: "1"
  PIP_NO_DEPS: "1"
  # NO secrets here — SECRET_KEY, FIREBASE_API_KEY, DAV_PASSWORD_HASH and
  # CF_ORIGIN_SECRET come from Secret Manager at runtime (config.py).

handlers:
  - url: /manifest.json     # PWA manifest
  - url: /sw.js             # Service worker (Cache-Control: no-cache, Service-Worker-Allowed: /)
  - url: /favicon.ico
  - url: /robots.txt
  - url: /apple-touch-icon{,-precomposed}.png
  - url: /privacy           # static/legal/privacy.html (+ explicit security headers —
  - url: /terms             #  static handlers bypass Flask's after_request hook)
  - url: /static/vendor     # version-named assets: Cache-Control public, max-age=31536000, immutable
  - url: /static            # everything else: Cache-Control public, max-age=86400
  - url: /.*                # script: auto, secure: always
```

Additional firm/tax env vars consumed by `config.py` (set via `--set-env-vars` or `.env` locally): `FIRM_NAME`, `FIRM_STREET`, `FIRM_UNIT`, `FIRM_CITY`, `FIRM_PROVINCE`, `FIRM_POSTAL_CODE`, `FIRM_PHONE`, `FIRM_EMAIL`, `GST_NUMBER`, `QST_NUMBER`, `SESSION_LIFETIME_HOURS`, `RATE_LIMIT_LOGIN`, `TRACE_SAMPLE_RATIO`, `APPCHECK_DEBUG_TOKEN` (local dev). **Bookings L2:** `BOOKINGS_JURISTE_UPN` (the polled mailbox — empty disables the sync), `BOOKINGS_SYNC_ACTIVE` (default `true`), `BOOKINGS_SUBJECT_KEYWORDS` (comma-separated, default `Consultation`; the subject must **END** with « {séparateur} {mot-clé} » — case- and accent-folded. One keyword per published service, each needing an entry in `Config.BOOKINGS_TYPE_PAR_MOT_CLE` (`consultation`→`consultation`, `rencontre`→`rencontre` — the service was renamed from « Réunion » on 2026-07-30, clean swap, no bookings carried the old name) or the import falls back to `consultation` **with a warning**), `BOOKINGS_SYNC_LOOKAHEAD_DAYS`/`BOOKINGS_SYNC_LOOKBACK_DAYS` (90/1), `BOOKINGS_DEBUG_PAYLOAD` (predicate tuning), `FEATURE_INTAKE` (L3 trigger, default `false`). **Miroir Outlook:** `MIROIR_OUTLOOK_ACTIF` (default `true` — `false` freezes the mirrors in place, no cleanup), `MIROIR_OUTLOOK_LOOKAHEAD_DAYS`/`MIROIR_OUTLOOK_LOOKBACK_DAYS` (365/30 — the shared diff window; same mailbox and Graph credentials as Bookings via `bookings_configured()`).

### Local development

```bash
# Install deps (runtime + dev/test)
pip install -r athena/requirements.txt
pip install -r athena/requirements-dev.txt

# Change a dependency: edit requirements.in, then re-lock (from athena/)
uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt

# Firestore emulator
gcloud emulators firestore start

# Run Flask
flask run --debug

# Run with gunicorn (production-like)
gunicorn -b :8080 main:app

# Deploy manually (normally CI handles this)
gcloud app deploy --project=athena-pallas

# Seed reference data (one-time after first deploy)
python -m scripts.seed_reference_data

# Mint a local MCP bearer token (dev only; refuses ENV=production), then
# point MCP Inspector at http://localhost:8080/mcp with it
python -m scripts.mint_dev_token

# Break-glass: revoke every MCP token (Claude must re-authorize)
python -m scripts.revoke_mcp_tokens

# Run unit tests
python -m pytest tests/ -v

# Deploy Firestore composite indexes (REQUIRED after adding any
# .where().order_by() combo or filtered aggregation — queries fail,
# gracefully degraded, until the index exists)
firebase deploy --only firestore:indexes --project athena-pallas

# Deploy Firestore + Storage security rules (firebase CLI, not gcloud;
# targets defined in firebase.json at the repo root)
firebase deploy --only firestore:rules,storage --project athena-pallas
```

Environment variables for local dev are read from `.env` via `python-dotenv` (see `.env.example` at the repo root).

---

## Phase History

All foundation phases (1–12) and improvement phases (A–G) are completed. This reference document consolidates their specifications.

### Foundation (Phases 1–12, all ✅ completed)

1. Project scaffolding, Firebase Auth, security hardening
2. Client/contact management + CardDAV foundation (vCard 4.0)
3. Dossier management with tabbed detail hub
4. Time tracking + expenses
5. Invoicing with GST/QST (on-screen + print-friendly via `@media print`)
6. Hearings/calendar + CalDAV foundation (VEVENT)
7. Tasks + VTODO foundation
8. Case protocols (three types: CQ simplifié / CS ordinaire / Conventionnel)
9. Document storage (Firebase Storage + folder hierarchy)
10. DAV protocol layer (CardDAV, CalDAV, RFC-5545)
11. Dashboard, polish, App Engine deployment
12. Firebase App Check + Phone MFA

### Improvements (Phases A–G, all ✅ completed)

- **A** — Judicial deadline calculator (art. 83 C.p.c. + Quebec holidays + Meeus/Jones/Butcher Easter)
- **B** — Input validation & normalization (E.164 phones, lowercase emails, Canadian postal codes, address defaults)
- **C** — Multiple sequential protocols per dossier + bidirectional task↔protocol step sync
- **D1** — DAV collection restructuring (per-dossier collections, removed `/dav/journals/`)
- **D2** — Dossier notes as VJOURNAL in per-dossier collections (markdown content)
- **D3** — RFC 5545 RELATED-TO linking between tasks and notes
- **F** — Data export (CSV with UTF-8 BOM + PDF via reportlab)
- **G** — Court file number parsing + reference data (greffes, juridictions)

### Hardening & performance (June 2026, all ✅ completed)

- **Security remediation** (commit `17269d4` + follow-ups) — 65-finding audit fixed in code: Secret Manager migration, Cloudflare origin-secret + firewall defense stack, auth replay guard + `check_revoked`, DAV brute-force brake, transactional invoicing, fail-closed FK checks, magic-byte upload validation, open-redirect guard, CSP cleanup + `/csp-report`, structured logging with PII redaction, OTel tracing with PII-sanitized spans, GitHub security workflows + Dependabot, hash-locked dependency pipeline.
- **Performance overhaul** (commit `13951c9`) — Tailwind precompiled to a committed `app.<hash>.css` (in-browser compiler removed); dashboard moved to Firestore aggregation queries; cursor pagination (timeentries/expenses/parties/dossiers/invoices) and bounded-group queries (tasks/notes/hearings); 30 composite indexes in `firestore.indexes.json`; immutable vendor caching; gunicorn sizing; `/_ah/warmup`; service-worker vendor caching.

### Phase I — MCP connector (July 2026, ✅ code complete)

- **I** — MCP server for Claude custom connectors: stateless JSON-mode Streamable HTTP endpoint (`POST /mcp`) with 14 read-only tools; embedded OAuth 2.1 AS (DCR restricted to Claude callbacks, PKCE S256, refresh rotation + family revocation, French consent screen behind session + MFA); opaque SHA-256-at-rest bearer tokens with a per-IP brake; `MCP_ENABLED` kill switch; `log_mcp_event` + `mcp.request`/`mcp.tool.*` spans; zero new dependencies. Side fix: `login_required` now preserves the query string in the login `next` redirect (needed for `/oauth/authorize?...`, also fixes filtered-list deep links). Ops prerequisites before connecting Claude: Firestore TTL policies on `oauth_codes.expire_at`/`oauth_tokens.expire_at`, rules deploy, Cloudflare Configuration Rule for bot mitigations on `/mcp`+`/oauth/*`, verify Cloudflare Access stays scoped to `/dav/*` (§16 of `PHASE_I_MCP.md`).

### Phase H — Document template generation "gabarits" (July 2026, ✅ code complete)

- **H** — User-managed `.docx` templates at `/gabarits` (upload / metadata edit / file replacement with version bump + re-extraction / delete / signed-URL download — templates are data, never a deploy). Stdlib-only fill engine (`utils/docx_fill.py`: XML substitution inside the zip, byte-identical pass-through of everything else; `docxtpl`/`python-docx` rejected — Word repair-prompt issue with letterhead templates). Field catalog + flat-alias table for the existing gabarits (`utils/template_fields.py`); split-run placeholders detected at upload and reported in French; blanks become visible `[CHAMP MANQUANT : x]` / `[À COMPLÉTER : x]` strings. HTMX generation popup from three entry points (gabarits, dossier detail — locked dossier, partie detail — destinataire prefill); output saved into the dossier's documents or downloaded directly. 10 MB upload size exemption in `security.py`; `log_template_event` + `template.fill` span; zero new dependencies. Spec: `SPEC_PHASE_H_GABARITS.md`.
  - **Refinement (July 2026 — placeholder handling):** catalog/alias matching is now **case-insensitive** and an ALL-CAPS placeholder upper-cases its resolved value (`{{TRIBUNAL}}` → `COUR SUPÉRIEURE`), fixing headings that read as "blocks" before. The ALL-CAPS→**block** concept was **removed**: the app fills only auto (case data) + a few manual letter-metadata fields, and leaves everything else — the former blocks (`{{FAITS}}` …), **civilité, and salutations** — verbatim as `{{name}}` for the user to complete in Word (per the user's instruction: civilité belongs in letters, never in court procedures). Added `{{dossier.role_label}}` (capitalized client role). Engine and its tests unchanged; `template_fields.py` classification + route/popup only.
  - **Refinement (July 2026 — split-run healing):** `docx_fill._normalize_runs` now **bridges** a placeholder Word fragmented across runs with *different* formatting/language (the frequent split at the dot in `{{dossier.defendeur}}`), and tolerates run-level `rsid` attributes — retyping no longer needed. Only genuinely structural splits (a `<w:br/>`/field/bookmark/image inside the braces) stay flagged. `scripts/diagnose_gabarit.py` reports a template's placeholders, classification, and any residual fragmentation with its cause.
  - **Refinement (July 2026 — bare names + civility twin):** person names render **bare by default** (`{{dossier.demandeur}}` → `Jean Tremblay`, `{{<slot>.nom_complet}}` without the `Me`/`M.`/`Mme` prefix), so a procedure cites the party without a honorific; each name field has an **`…_avec_civilite` twin** (`{{dossier.demandeur_avec_civilite}}`, `{{<slot>.nom_complet_avec_civilite}}`) that keeps it, for letter address blocks. Accented `…_avec_civilité` spellings auto-registered.

### Phase H.2 — Invoice document generation "note d'honoraires" (July 2026, ✅ code complete)

- **H.2** — Word note-d'honoraires generation from a stored invoice, reusing the Phase H fill engine, Storage, field catalog, and generation-into-documents flow. Two additive engine capabilities in `utils/docx_fill.py` (Phase H callers untouched — `fill_docx` gains `*, rows_by_region=None, conditions=None`, both `None` = Phase H behavior): **repeating table rows** (`{{#region}}` clones the innermost `<w:tr>` per row dict, preserving cell formatting) and **conditional regions** (`{{?cond}}`…`{{/cond}}` bracketing a table — false removes the whole marker-paragraph→marker-paragraph span, so an empty table disappears entirely; unbalanced → `DocxFillError`). Ordering per target: conditionals → rows → blocks → scalars (`word/document.xml` only). New pure modules: `utils/format_fr.py` (fr-CA currency/date/hours/rate — NBSP thousands, comma decimal; GST ×100 / QST ×1000 scales) and `utils/invoice_docx.py` (`build_invoice_context` → `facture.*` scalars + three region row-lists + `si_*` conditions; **figures read from the invoice, never recomputed**; `sous_total_debours_tx + sous_total_debours_ntx == subtotal_expenses` invariant; billing_address fallback when the client partie is deleted). `models/folder.get_or_create_folder` (idempotent folder); `doc_templates.kind` discriminator (`"note_honoraires"` via a checkbox; `get_note_honoraires_template`). Route `POST /factures/<id>/note-docx` (refuses `annulée`; French message when no note template) + a « Note d'honoraires (Word) » button on the invoice detail. `document_generated` gains `source="facture"` + row counts; `template.fill` span gains `invoice_id`. **The reportlab PDF is unchanged — the two coexist.** Trust accounting stays out of scope (the note only displays the stored `retainer_applied` as a parenthesized deduction and `amount_due` as the balance). Zero new dependencies. Spec: `SPEC_PHASE_H2_NOTE_HONORAIRES.md`.
  - **Refinement (July 2026 — H.2 polish):** (1) a kept conditional's marker paragraphs are removed entirely (`_remove_marker_paragraph`) and any two tables it leaves adjacent get a **minimal ~1pt separator paragraph** (`_ensure_table_separation`) so stacked tables don't merge and show no visible gap; (2) `build_invoice_context` resolves both canonical names AND flat aliases (`list(CATALOG) + list(FLAT_ALIASES)`) so a note reuses the identical placeholders as procedures/letters, and `facture.taux_horaire` falls back to the dossier's `hourly_rate` when line-item rates are mixed/absent; (3) **all** generated documents (gabarits + notes) now save into a per-dossier « **Projets** » folder (`document.GENERATED_FOLDER_NAME`) named `"REF - YYYY-MM-DD - Projet Nom"` (`document.projet_document_name`).

### Phase H.3 — Impression d'une note via gabarit (August 2026, ✅ code complete)

- **H.3** — « Imprimer (Word) » sur la page d'une note (et l'onglet Analyse — les notes Théorie de la cause incluses) : remplit le gabarit de type **« Note (impression) »** (`kind="note"`, sélection = le plus récent du type, motif H.2) et **télécharge directement** le .docx (jamais versé aux documents du dossier — décision utilisateur 2026-08-10). Le corps Markdown devient de la **vraie mise en forme Word** : nouveau module pur `utils/markdown_docx.py` (markdown→HTML avec le MÊME pipeline que l'écran — constantes partagées importées par `main.py`, qui gagne au passage `use_align_attribute=True` réparant l'alignement des tableaux à l'écran que bleach supprimait — puis HTMLParser stdlib → écrivain d'éléments OOXML : titres en pas de taille relatifs à la police du gabarit, gras/italique/barré, code Consolas, listes en puces texte/numéros calculés (jamais numbering.xml), citations à bordure gauche, filets, liens soulignés + URL, **tableaux Word réels** avec alignements `:---:`, largeurs égales sur la page du sectPr). Accroché au moteur par `fill_docx(..., rich_values=)` (H.2-style, additif — `None` = octet-identique) : le paragraphe hôte portant `{{note.contenu}}` SEUL est remplacé par les blocs convertis, ses pPr/rPr semant le corps ; tout hôte non sûr (texte partagé, sectPr, zone de texte) ou toute panne du convertisseur **dégrade vers le remplissage plat** (sigils visibles, document valide — jamais corrompu) ; en-têtes/pieds : verbatim. Contexte `note.*` (`utils/note_docx.py`, pur — titre, catégorie, dates MONTRÉAL, dossier avec repli « Général », + tout le catalogue) ; case à cocher → **groupe radio** 3 types (aide legacy conservée) ; `utils/cabinet.py` (hissage du dict cabinet dupliqué). 63 tests nouveaux (constructions, bornes, invariant de linéarité, E2E graine d'analyse — 5 tableaux, défusedxml). **Zéro dépendance nouvelle, aucun index, aucune classe Tailwind nouvelle.** Placeholders documentés : `GABARITS_PLACEHOLDERS.md` §7.
- **Ops prerequisite :** composer le gabarit « note » sur papier à en-tête (`{{note.contenu}}` SEUL dans son paragraphe) et le téléverser avec le type « Note (impression) » ; premier essai = **vérification Word manuelle** (ouverture sans réparation, tableaux distincts, échelle des titres) sur une note Théorie de la cause.

### Phase K — Trust accounting "comptabilité en fidéicommis" (July 2026, ✅ code complete)

- **K** — The two registers required by RLRQ c. B-1, r. 5: the **journal de caisse** (recettes et déboursés — all clients, chronological, running balance) and the **carte-client** (grand livre auxiliaire — the same rows filtered to one `(dossier_id, client_id)` couple). Two views of ONE collection (`trust_transactions`); one write path, one source of truth. Deliberate divergences from the house patterns (spec §2): **append-only, no `update_*`/`delete_*`** — correction is by **reversal only** (`reverse_transaction` mints an opposite `purpose="correction"` entry; the create path refuses `correction`); exactly **three write-once mutable fields** (`status` `en_circulation`→`compensée`|`annulée`, `cleared_date`, `reconciliation_id`); the **overdraft control** (a déboursé may only draw on the client's `compensée`/cleared balance — confirmed required, user decision 2026-07-16) lives INSIDE the Firestore transaction; the module **fails CLOSED** everywhere. Three balances per `compute_deltas` atom: **book** (all statuses incl. `annulée`; the register's « Solde »), **cleared** (the control; never shown as « Solde »), **bank** (compensée only; the reconciliation anchor). Two-step lifecycle: recorded `en_circulation` when made, `compensée` when it clears the bank, `annulée` only via reversal of an uncleared entry. New model `models/trust.py` (pure §6.1 helpers `compute_deltas`/`check_disbursement_allowed`/`reconciliation_variance`/`to_barreau_row`/`recompute_running_balances` + the Firestore layer: accounts CRUD, transactional `create_transaction`/`clear`/`clear_bulk`/`reverse`, `create_inter_dossier_transfer`, reconciliation with variance gating, per-account monotonic counter `counters/trust-{account_id}`). Three new top-level collections (`trust_accounts` — last-4-only, never the full account number; `trust_transactions`; `trust_reconciliations`) + three `dossiers` fields (`trust_balance`, `trust_balance_by_client`, `trust_cleared_by_client`, defaulted on read by `_migrate_trust`). Routes `routes/trust.py` at `/fideicommis` (journal, entry form, detail, compenser/contrepasser, virement inter-dossiers, carte-client, comptes, conciliations worksheet with live variance, CSV/PDF exports) + a `fideicommis` dossier tab + a dashboard « Sommes en fidéicommis » stat. **The CSVs and the carte-client PDF have 9 columns** — « Recette / Crédit » split into two per-direction columns (Barreau sheet, user decision 2026-07-16); the **journal PDF became the art. 38 register in August 2026** (10 columns, legal landscape, carried-forward balance — see its route row above). 3 read-only MCP tools (`get_trust_balance`/`list_trust_transactions`/`get_trust_snapshot`, 14→17; never emit transit/account number) + a consent-screen disclosure. `log_trust_event` + `trust.transaction`/`trust.reconcile` spans. 8 composite indexes; `scripts/verify_trust_integrity.py` recomputes and cross-checks. **Zero new Python dependencies.** Spec: `SPEC_PHASE_K_FIDEICOMMIS.md`. **Ops prerequisite: deploy the 8 `firestore.indexes.json` trust indexes before/with the code**, or the paginated/filtered queries degrade until they build.

### Phase L — MCP note writes (July 2026, ✅ code complete)

- **L** — The MCP connector gains its first write capability: **2 tools** (17 → 19), `create_note` (new note on a dossier) and `append_to_note` (appends under a dated separator). Deliberately **additive only** — no tool can edit or delete a note, and no other collection is writable. New scope **`athena:write`**, granted *only* by a default-unchecked « Autoriser l'écriture des notes » checkbox on the French consent screen (the hidden `scope` field can never escalate; `SCOPE_READ` is always force-included so a write-only grant can't brick the connector). Per-tool enforcement in `endpoint._tools_call` before argument validation via `bearer.ScopeRequired`, caught ahead of the generic `except Exception`; `tools/list` filtered by granted scope so a read-only connection never sees a write tool. The bearer **success cache now carries the scope** (both paths publish `g.mcp_scopes`) and writes additionally run `bearer.revalidate_for_write` — one keyed Firestore read bypassing the cache, so revocation stops a mutation immediately instead of ≤5 min later. Handlers resolve the dossier first (refusing an unknown id rather than blanking it), build the model payload from an **explicit whitelist** (a forwarded `id` would full-document-overwrite an existing note), **bump `dossier:{id}`** (+ `remove_tombstone` on create) inside their own `try/except` surfaced as `dav_synced`, normalize Markdown autolinks and **refuse** any residual `security.TAG_RE` match, refuse an append that would truncate at `CONTENT_MAX_LENGTH`, and stamp every write with a dated « Ajouté/rédigée par Claude » provenance line. Second kill switch **`MCP_WRITE_ENABLED`** (writes off, reads untouched). New events `mcp_note_written` / `mcp_write_refused` (IDs and counts only — never a note's title or body) + `scope` on `mcp_consent`/`mcp_token_issued`; `security.TAG_RE` public alias added. **Zero new Python dependencies; no new Tailwind class (verified against `app.af95b30d.css`), so no recompile/rehash.** **Ops prerequisite: the scope is frozen at issuance and copied across refresh rotation — the connector must be removed in claude.ai, `python -m scripts.revoke_mcp_tokens` run, and re-added with the box ticked.**

### Portail client — socle + transmission de documents (spec « L1 », July 2026, ✅ code complete — **infrastructure pending**)

- **Portail L1** — second App Engine service `portail` (package `athena/client/`, `athena/portail.yaml`, F1, SA `portail-svc` à moindre privilège) : client invité → connexion Firebase par **lien courriel** (usage unique, lié à l'adresse) → téléversement **directement vers GCS** (sessions reprenables, `origin=` pour le CORS, `size=` appliqué par GCS) dans le bucket de quarantaine → enveloppe create-only → tâche Cloud Tasks vers le gestionnaire du service principal (SHA-512 en flux, manifeste, accusé A.2 derrière un test-and-set transactionnel) → revue dans « **Réception** » (versement restreint au vocabulaire documents — élargi à 9 types le 2026-08-11 : + ZIP/.eml/.msg, puis à 11 le 2026-08-13 : + Excel —, refus, lot traité → archive 365 j). Réconciliation cron 15 min (« toute enveloppe finit traitée » — Cloud Tasks n'a pas de file de rebut). Fondations phase J introduites : `utils/graph.py` + `utils/courriel.py` (Graph client credentials, sans msal), la file + le motif de gestionnaire, `cron.yaml`. Garde-fous §1 : claim `portail: True` refusé à la session du principal ; auto-invitation du juriste refusée ; repli « lien manuel » quand Graph n'est pas configuré. Dépendances : + `google-cloud-tasks`, `requests` promu direct. Tailwind : `@source client/templates` + re-hachage (`app.0821ad87.css`). Specs : `SPEC_PHASE_L1_PORTAIL_SOCLE_DOCUMENTS.md` (déviations documentées : paquet `athena/client/` au lieu de `portail/` racine ; versement restreint ; événements snake_case sous `pallas.portail`).
- **Ops prerequisites (ordered — BEFORE pushing the CI-wiring commit):**
  1. `gcloud services enable cloudtasks.googleapis.com cloudscheduler.googleapis.com` (⚠️ **Scheduler est requis par `gcloud app deploy cron.yaml`** — s'il manque, l'étape cron du build échoue avec `SERVICE_DISABLED` alors que default/portail/dispatch sont déjà déployés)
  2. `gcloud iam service-accounts create portail-svc`
  3. Bucket `athena-pallas-portail-quarantaine` — **région = région App Engine (`gcloud app describe`)**, UBLA, non public + cycle de vie (submissions 90 j / archive 365 j, JSON au §12.3 de la spec)
  4. IAM : `portail-svc` → `storage.objectCreator` (bucket), `datastore.viewer` (condition `resource.name.startsWith(".../databases/portail")`), `cloudtasks.enqueuer` (file), **`appengine.appViewer` (projet — porte `appengine.applications.get`, EXIGÉ pour enfiler une tâche `app_engine_http_request` ; `cloudtasks.enqueuer` seul → `PERMISSION_DENIED`, grant omis par la spec §3)**, `logging.logWriter` + `cloudtrace.agent`, `secretmanager.secretAccessor` sur `portail-secret-key` + `firebase-api-key` ; SA principal → `storage.objectAdmin` (bucket) + `secretAccessor` sur `graph-client-secret` + **`cloudtasks.enqueuer` sur la file** (la réconciliation ré-enfile ; le SA principal a `editor` qui porte déjà `appengine.applications.get`) ; SA Cloud Build → `iam.serviceAccountUser` sur `portail-svc`
  5. `gcloud firestore databases create --database=portail --location=<région>` ; `gcloud tasks queues create portail --location=<région>` puis update `--max-attempts=10 --min-backoff=10s --max-backoff=600s --max-concurrent-dispatches=3 --max-dispatches-per-second=5`
  6. **Pare-feu App Engine : Autoriser `0.1.0.2/32` en priorité haute** (Cloud Tasks + cron — vérifier l'adresse dans la doc)
  7. Entra ID : app « Pallas-Athena-Graph », permission d'application `Mail.Send` + consentement admin ; secret → `graph-client-secret` ; `GRAPH_TENANT_ID`/`GRAPH_CLIENT_ID`/`GRAPH_SENDER_UPN` dans `app.yaml` (recommandé : `ApplicationAccessPolicy` restreignant à la boîte du juriste)
  8. Console Firebase → Authentication : activer la connexion par **lien courriel** ; ajouter `portail.poirierlavoie.ca` aux domaines autorisés
  9. Clé reCAPTCHA Enterprise : ajouter le domaine du portail (App Check D-2)
  10. Mappage `portail.poirierlavoie.ca` + DNS Cloudflare (CNAME proxied, Full Strict) ; **aucune** application Access sur cet hôte ; WAF + limite de débit (~120 req/min/IP) ; Transform Rule `X-Origin-Auth` zone-wide
  11. Secrets `portail-secret-key` + `graph-client-secret`
  12. Pousser le commit CI (portail.yaml + dispatch.yaml + cron.yaml) ; essai bout en bout avec un **alias** (jamais le courriel d'autorisation — §1.3), y compris l'accusé + sa copie « Éléments envoyés » et le test de panne (`gcloud tasks queues pause portail`)

### Phase L2 — Synchronisation « Bookings with me » → rendez-vous à confirmer (July 2026, ✅ code complete — **infrastructure pending**)

- **L2** — Importe les réservations Microsoft « Bookings with me » de la boîte du juriste dans Athéna comme **hearings** (le dépôt n'a pas de `models/event.py` — les événements de calendrier SONT les hearings) verrouillés derrière un **nouveau champ `confirmation`** (`""`/absent = confirmé, `à_confirmer`, `annulée_client`, `refusée`) — **distinct du champ `status`**, où `à_confirmer` est déjà une valeur (audience à planifier). **Interrogation cron toutes les 10 min** (`GET /taches/bookings/sync`, blueprint machine `routes/taches_bookings.py`, gardé par `X-Appengine-Cron`), pas de webhook. Réutilise l'infra Graph + cron de L1 : `utils/graph_calendrier.py` (lecture `calendarView` paginée UTC via `graph_get`, prédicat mot-clé-d'objet `mot_cle_correspondant` — organizer == UPN ET le sujet **SE TERMINE** par « {séparateur} {mot-clé} », Bookings nommant l'événement `{Client} - {Service}` ; casse et accents pliés ; `extraire`, `annuler_reservation`). **Permission Graph `Calendars.ReadWrite`** (décision 2026-07-25) : refuser un rendez-vous **annule réellement** la réunion Outlook (best-effort + bandeau si échec). **Contrat `include_unconfirmed`** (miroir de `include_analyse`) sur `list_hearings`/`list_hearings_in_range`/`list_hearings_window` : défaut `False` = confirmés seulement (DAV, MCP, tableau de bord, exports) ; `True` = + à_confirmer + annulée_client (jamais `refusée`). Le **Calendrier** montre les à_confirmer avec un **badge** (décision D-L2-2 — `routes/hearings._keep_calendar` retire annulée_client), DavX5/MCP jamais avant confirmation. Onglet **Rendez-vous** de Réception (`templates/reception/_rdv.html`) : cartes confirmer/refuser + alertes de divergence (modifié/annulé côté client — jamais d'écrasement d'un confirmé, §5.4), liaison partie par courriel exact ; la confirmation `bump_ctag(collection_for(dossier_id))` (`""` → « Général »). `log_bookings_event` (logger `pallas.bookings`) + config `BOOKINGS_*`/`FEATURE_INTAKE` (déclencheur intake L3 inerte). **Zéro nouvelle dépendance ; aucune classe Tailwind nouvelle** (badge `bg-amber-100 text-amber-700` déjà compilé) → pas de recompilation. Spec : `SPEC_PHASE_L2_BOOKINGS_SYNC.md` (déviations documentées : `models/event.py` = `models/hearing.py` ; événements snake_case sous `pallas.bookings` ; décisions ReadWrite + badge Calendrier).
- **Ops prerequisites (ordered):**
  1. **Entra ID** : ajouter la permission d'application **`Calendars.ReadWrite`** à « Pallas-Athena-Graph » (créée en L1) + **consentement admin** (recommandé : `ApplicationAccessPolicy` restreignant l'accès calendrier à la seule boîte du juriste).
  2. `app.yaml` env : `BOOKINGS_JURISTE_UPN=<UPN de la boîte Bookings>`, `BOOKINGS_SUBJECT_KEYWORDS=<nom du type de rencontre, p. ex. Consultation>`, `BOOKINGS_SYNC_ACTIVE=true`, `FEATURE_INTAKE=false` (+ lookahead / lookback si autres que 90 / 1).
  3. **Bookings with me** : le mot-clé doit être une **sous-chaîne** du sujet — Bookings nomme l'événement `{Client} - {Service}` (p. ex. « Jason Poirier Lavoie - Consultation »), donc le nom du service est un **suffixe**. Régler `BOOKINGS_SUBJECT_KEYWORDS` sur ce nom de service ; lien Teams activé, publier la page.
  4. **Déployer `cron.yaml` COMPLET** (⚠️ `gcloud app deploy cron.yaml` REMPLACE toute la table cron — le fichier contient DÉJÀ la réconciliation portail L1 + la synchro Bookings ; ne jamais le remplacer par la seule entrée Bookings).
  5. Réservation d'essai + réglage du prédicat (`BOOKINGS_DEBUG_PAYLOAD=true` → journalise `organizer_match`/`keyword_match` + domaines tronqués, jamais le sujet → ajuster `BOOKINGS_SUBJECT_KEYWORDS` → remettre `false`). Le pare-feu `0.1.0.2/32` est déjà autorisé (L1).

### Portail — survivabilité du lien (2026-07-27, ✅ livré)

Deux lots successifs sur le socle L1, motivés par des essais réels où « une faute de frappe, un onglet fermé, un second clic sur le courriel » suffisaient à condamner un client. **Le lien Firebase est à USAGE UNIQUE et il est consommé AVANT que la session du portail existe** : tout refus survenant après ce point ne refuse pas une requête, il détruit l'invitation, sans recours. Tout ce qui suit découle de là.

- **Le client n'est jamais sans issue** — passerelle « Le lien ne fonctionne pas ? » en permanence sous le formulaire ; tout échec révèle le formulaire de renvoi sans masquer le champ d'adresse (une faute de frappe se corrige en retapant ; seul un lien consommé exige un nouveau courriel). Le statut du renvoi est HONNÊTE : un 429/401/5xx affichait auparavant le message de succès.
- **App Check ne bloque plus `POST /session`** (`client/security.verify_app_check`, dérogation unique et documentée). C'est ce qui expliquait « moi ça marche, mes amis non » : le code à usage unique est dépensé une ligne avant ce POST, donc un score reCAPTCHA faible — routine sur un téléphone neuf, en navigation privée ou derrière un VPN — ne refusait pas la requête, il **détruisait l'invitation**. La route garde toutes ses vraies gardes (jeton Firebase avec la revendication `portail`, `email_verified`, invitation active, courriel identique, 10/min). App Check reste APPLIQUÉ sur `/api/renvoi` (non authentifié, envoie du courriel) et sur les API de téléversement.
- **`invitations.LectureIndisponible`** — une panne de lecture Firestore était indiscernable d'une révocation et effaçait la session. Elle rend désormais 503 **sans toucher la session** ; `/api/renvoi` reste octet pour octet identique même pendant la panne (l'invariant anti-énumération doit survivre à la panne qu'il sert à traverser).
- **`STATUTS_SESSION = ("envoyée","ouverte","soumise")`** (décision D-2) : un lot soumis reste ouvert jusqu'à ce que le JURISTE le marque traité, pour que « j'ai oublié une page » soit récupérable. **`STATUTS_FERMES = ("révoquée","refusée","traitée")`** : les deux vivent dans `client/config.py` parce que les DEUX services en dépendent — une copie par service dériverait, et la dérive ROUVRIRAIT une invitation close.
- **`lot_abandonne`** (ERROR) — un préfixe de quarantaine avec des fichiers mais **sans enveloppe**, immobile depuis plus de 2 h : le client a téléversé sans finaliser. Rien ne le référence, la réconciliation l'ignore, et le cycle de vie 90 j l'effacerait en silence.
- **Le courriel dit la vérité** — il promettait que *le lien* restait valide 14 jours, alors que le lien dure quelques heures et que c'est l'*invitation* qui dure 14 jours. Les deux faits sont désormais distincts, et le courriel porte une **URL de secours** (`https://{PORTAIL_HOST}/entree?i={id}`) : c'est la ligne la plus utile du gabarit, celle qui donne une sortie à tout échec sans appel téléphonique.

**Revue adversariale (2026-07-27) — 8 défauts confirmés, corrigés dans le même lot.** Le fil commun : `_garde` prouve qu'une INVITATION est vivante, jamais que le VISITEUR en est le destinataire. (i) `entree()` réutilisait la session sans comparer `?i=`, donc un 2e client sur un navigateur partagé atterrissait dans la session du premier — fichiers sous le mauvais préfixe, accusé (noms, tailles, SHA-512) expédié au mauvais client ; (ii) `ajouter_soumission` réécrivait `statut="soumise"` sans condition, ce qui ANNULAIT une révocation depuis que « soumise » ouvre la session ; (iii) la branche 409 de `api_finaliser` ne purgeait pas la session, coinçant définitivement le client sur le chemin de reprise le plus banal (réponse perdue sur un lien mobile) ; (iv) « traitée » manquait aux statuts terminaux ; (v) une invitation expirée était envoyée sur `/confirmation`, faux accusé et boucle fermée ; (vi) le commentaire du quota surestimait son étanchéité ; (vii-viii) le bloc de reprise restait cliquable après déconnexion et affichait l'adresse d'une identité persistée sans vérifier qu'elle appartenait à l'invitation de `?i=` (un marqueur local les lie désormais).

### Phase L3 — Portail client : formulaire d'ouverture « intake » (July 2026, ✅ code complete)

- **L3** — Le portail gagne une **seconde file** : un formulaire d'ouverture de dossier en 4 étapes (`type="intake"`), ouvert par la même invitation à lien courriel. La soumission produit une **enveloppe JSON en quarantaine, sans aucun fichier** ; le juriste l'examine dans **Réception → onglet « Ouvertures »** et **crée** la partie, ou **applique champ par champ** une mise à jour. **Aucune écriture Firestore avant son clic**, et la section Conformité n'est JAMAIS renseignée : recueillir n'est pas vérifier (le KYC reste hors périmètre, `pieces_identite: null` est un emplacement réservé). Trois déclencheurs : (a) confirmation d'un rendez-vous Bookings dont le courriel n'est lié à aucune partie (case cochée par défaut, derrière `FEATURE_INTAKE`), (b) bouton sur la fiche d'un contact (avec préremplissage), (c) sélecteur sur le formulaire d'invitation de Réception.
  - **La porte par type** remplace l'égalité globale de `_garde` (`type != "documents"` → refus) par une table **endpoint → type requis** (`client/routes._TYPE_REQUIS`) : la porte reste au même endroit, et un endpoint absent de la table est refusé **par défaut**. Les deux autres endroits qui supposaient « documents » — le `suivant` de `creer_session` et le test de type de `entree()` — passent par `_PAGE_DU_TYPE`.
  - **JS vanilla, pas d'Alpine** (la spec supposait Alpine) : la CSP du portail n'a pas `'unsafe-eval'`. Bascules d'étape par classe comme les panneaux de `entree.html`, lignes de parties adverses rendues intégralement en DOM (`textContent`, jamais `innerHTML`) comme la liste de `documents.html`. Chaque « Suivant » est un `fetch` vers `/api/intake/etape`, ce qui laisse **App Check pleinement appliqué** — un POST de formulaire HTML classique ne porterait pas l'en-tête et prendrait un 401.
  - **Brouillon ceinture + bretelles (D-L3-1)** : `session["intake"]` est l'autorité (validée et bornée à chaque étape) ; un miroir `localStorage` (clé portant l'id de l'invitation) est proposé par un bouton explicite et **jamais fusionné d'office**.
  - **Ré-entrée** : corriger et re-soumettre tant que le juriste n'a pas traité l'ouverture ; chaque envoi crée un nouveau lot, et Réception affiche **le plus récent**. À la clôture (traitée OU refusée), **toutes** les enveloppes de l'invitation passent sous `archive/` — pas seulement celle qui a été examinée — et se reconsultent par la même modale que les lots de documents traités.
  - `models/partie.py` gagne **`birth_date`** (date seule à minuit UTC, `BDAY` vCard émis ET relu), demandée au client par l'assistant (`<input type="date">`, facultative, bornée à aujourd'hui) et proposée au versement comme tout autre champ — le portail la valide LOCALEMENT (`_date_valide`), n'important jamais `models`, et la **refuse** plutôt que de l'écarter en silence et un correctif de `_normalize` (voir Known Gotchas). `models/portail_invitation.prefill_depuis_partie` est une **liste blanche** — le document d'invitation est lu par le service PUBLIC. `utils/rapprochement.py` (pur) propose des candidats de conflit, **sans jamais rendre de verdict**.
  - **Zéro nouvelle dépendance ; aucune classe Tailwind nouvelle** (168 classes vérifiées contre `app.0821ad87.css` ; 7 absentes ont été remplacées par des utilitaires existants, dont 3 qui traînaient depuis L2 dans `_rdv.html`) → **pas de recompilation**. Spec : `SPEC_PHASE_L3_PORTAIL_INTAKE.md` (déviations documentées : pas d'Alpine ; noms de champs du modèle vivant — `organization_name`/`company_neq`, adresse structurée ; `INTAKE_PRECISION_MAX` 200 → 160 pour tenir dans le témoin ; événements snake_case).
- **Ops prerequisite :** `FEATURE_INTAKE: "true"` dans `app.yaml` (déclencheur (a) — suppose L2 en service). **Rien d'autre** : file, seau, base nommée, cron et permissions Graph sont ceux de L1 — et surtout **ne pas redéployer `cron.yaml`**, aucune entrée n'a changé.

### Miroir Outlook — audiences Athéna → calendrier du juriste (2026-07-29, ✅ code complete)

- **Miroir** — un **cron 10 min** (`GET /taches/outlook/sync`, blueprint MACHINE `routes/taches_outlook.py`) réconcilie par diff les audiences **confirmées** (`confirmation == ""`, `status != "annulée"`, jamais `source == "bookings"` — elles SONT déjà des événements Outlook) dans le **calendrier principal** du juriste via Graph (`Calendars.ReadWrite` de L2, aucun consentement nouveau). Bénéfice du calendrier principal (décision utilisateur 2026-07-29) : les audiences comptent dans le libre/occupé Exchange, donc « Bookings with me » n'offre plus de créneau par-dessus une date de cour. Unidirectionnel : **Athéna écrase** une édition Outlook au cycle suivant (etag stampé + comparaison des champs visibles ; le corps n'est pas comparé — Outlook réécrit texte→HTML). **Anti-boucle déterministe** : propriété étendue `MIROIR_PROP_ID` (GUID gelé, valeur `"{hearing_id}|{etag}"`) + catégorie « Pallas Athéna » sur chaque miroir, refusées par `mot_cle_correspondant` AVANT sa logique de mot-clé (garde large propriété OU catégorie ; suppression étroite, propriété seule). **Firestore-lecture-seule** — le mappage vit dans l'événement Outlook, jamais sur l'audience (churn etag/DavX5). Fenêtre UNIQUE [-30 j, +365 j] partagée des deux côtés (l'invariant anti-orphelin) ; `fenetre_pleine` (≥ 500) désarme les suppressions + ERROR. Charge d'événement : `subject`/`start`/`end` UTC (all-day à minuit `America/Toronto`, fin exclusive), `showAs: busy`, rappel, corps minimal N/R + visio, **jamais de clé `attendees`** (Exchange enverrait des invitations), `transactionId` à la création seulement. Nouveaux `graph_patch`/`graph_delete` dans `utils/graph.py` ; nouveaux événements `miroir_outlook_execute`/`miroir_outlook_erreur_graph` (logger `pallas.bookings`). **Zéro nouvelle dépendance, aucune UI, aucun index.**
- **Ops :** rien à provisionner (permission Entra, pare-feu et secrets déjà en place). Le push déploie code + `cron.yaml` COMPLET (trois entrées) via le CI. Vérifs premier cycle : `miroir_outlook_execute` avec compteurs plausibles ; audiences dans Outlook avec la catégorie ; `detectes` stable côté `bookings_sync_execute` (aucun auto-import) ; une édition Outlook d'un miroir rétablie au cycle suivant ; un créneau Bookings par-dessus une audience refusé. Cosmétique : créer la catégorie maîtresse « Pallas Athéna » (couleur) dans Outlook.

### Remédiation de l'audit MCP + surface d'écriture (2026-07-30, ✅ code complete)

- **Contexte** — Claude (claude.ai) a audité le connecteur en lecture seule (`pallas-athena-mcp-audit.md` : 9 défauts, 12 lacunes) ; chaque constat a été vérifié contre la source avant remède (verdicts fichier:ligne). Plusieurs mécanismes différaient de l'hypothèse de l'audit : `prev_juridical_day` est *inclusif* (problème de nommage, pas d'écho) ; la fenêtre de grâce de conciliation masquait la branche « jamais conciliée » ; le `court` des protocoles a toujours été vide (`dossier.get("court")` — la clé s'appelle `tribunal`) ; les événements créés au téléphone étaient estampillés « audience » ; `list_trust_transactions` rendait les plus VIEILLES lignes ; le curseur de `list_dossiers` était jeté.
- **Correctifs lecture (phases A-B)** — alertes de prescription honnêtes (`last_action_differs` partagé via `utils/deadlines.last_action_day`) ; résolution des préfixes alphabétiques du numéro de cour (fédéral → `is_administrative=False`) ; dates KYC estampillées seulement aux transitions ; fenêtre 7 jours documentée + définition unique du retard (date civile) côté protocoles ; instantané fidéicommis refait (jamais-conciliée AVANT la grâce, état par compte, chèques en circulation LISTÉS, `by_dossier[]`) ; `court` copié depuis `tribunal` + garde de régime au create + drapeau `regime_mismatch` ; défaut DAV `rencontre` ; facturation véridique (`get_billing_snapshot` global + `list_time_entries`/`list_expenses`) ; filtres notes/documents (`document_date` manuel) ; `created_at`/`updated_at` + `updated_since` + `offset`/`has_more` sur les outils matérialisés ; jointure lecture des libellés de dossier (instantané en repli) ; registre fidéicommis DESC.
- **Modèles (phase C)** — `audit_events` (journal de suppression append-only + outil `list_deletions`) ; `prescription_events[]` + `derive_prescription` (la date brute JAMAIS recalculée — projection `prescription_status`/`prescription_date_effective` à côté ; parité trois surfaces) ; `significations[]` (registre par partie, chaîne superseded_by ; dérivation des délais de réponse différée).
- **Écritures (phase D)** — `WRITE_TOOLS` 2 → 9, **création seulement** : `create_task` (statut épinglé `à_faire`), `create_hearing` (heures Montréal → UTC ; défaut `rencontre` ; `conference_uri` filtré), `create_time_entry`/`create_expense` (taux du dossier par défaut ; `created_via: "mcp"` stocké, JAMAIS de texte de provenance — leurs descriptions s'impriment sur les factures), `complete_dossier` (remplir-si-vide strict, refus atomique), `record_signification`, `record_prescription_event`. Protocole d'écriture commun `mcp/write_support.run_write` : `dry_run` + `idempotency_key` (collection à clé `mcp_idempotency`, TTL 24 h, fail-open). Consentement + INSTRUCTIONS réécrits pour énumérer les familles ET l'impossible ; `mcp_write` généralisé (l'événement `mcp_note_written` conservé pour la continuité). **29 outils (20 lecture + 9 écriture) ; zéro dépendance nouvelle ; zéro index composite nouveau** (un seul fieldOverride TTL).
- **Ops (ordonné)** : (1) déployer `firestore.indexes.json` (le fieldOverride TTL `mcp_idempotency.expire_at`) avant/avec le code ; (2) `python -m scripts.backfill_protocol_court` (--dry-run puis --apply) ; (3) après le push de la phase D — le scope étant **gelé à l'émission**, retirer le connecteur dans claude.ai, `python -m scripts.revoke_mcp_tokens`, re-ajouter et cocher « Autoriser les écritures » ; vérifier `tools/list` → 29, un aller-retour `create_task` en `dry_run`, et le coupe-circuit `MCP_WRITE_ENABLED=false` ; (4) corrections manuelles : recréer le protocole 2026-027 en `cs_ordinaire`, retyper les 4 événements mal classés.

### Mandat du 31 juillet 2026 — six lots sur le connecteur MCP (2026-08-02, ✅ code complete)

- **Contexte** — mandat écrit de l'avocat (`mandat-claude-code-pallas-athena_1.md`), six évolutions du connecteur. Le mandat supposait une pile Cloudflare Workers / D1 / Drizzle : **corrigé avant tout travail** (Flask + Firestore + App Engine, aucune préproduction). Contrainte dominante : **additif seulement**, deux tâches planifiées non surveillées (briefing 7 h, veille 17 h) lisant `get_agenda`, `list_tasks`, `list_hearings`, `get_dossier`, `list_notes`, `create_note`, `append_to_note`. Ordre exécuté : **6 → 2 → P → 4 → 1 → 5 → 3**. **29 → 33 outils** (23 lecture, 10 écriture) ; 1457 → 1613 tests ; **zéro dépendance nouvelle, zéro index composite nouveau**.
- **Lot 6 — cohérence des échéances** (`2143fdc`). Deux constats de l'audit corrigés *après vérification* : `get_agenda` ne se trompait PAS de date (sa fenêtre était déjà en heure de Montréal — l'audit du 31 juillet lisait le 30 parce que l'appel tombait dans la bande 00 h-04 h UTC), mais la même réponse calculait tous ses `is_overdue` en UTC ; et la contradiction `en_retard`/`is_overdue: false` était un **statut fossilisé**, pas un problème de fuseau. `utils/deadlines.py` gagne le prédicat unique (`today_mtl`/`is_past_due`/`days_until`) + une table de référence figée protégeant l'art. 83. `_step_row` dérive le statut ; `list_tasks` gagne `is_overdue` ; `get_agenda` ouvre sa fenêtre à minuit Montréal (ce lot AJOUTE donc des rangées). Le web n'était pas modifié (décision D3 : `today` facultatif dont le défaut reproduisait l'existant) — **décision RENVERSÉE le 2026-08-02** après le signalement de la bande du soir (des tâches dues lundi lues « en retard » le dimanche dès 20 h) : le web est aligné sur le même prédicat, la prorogation entre dans l'évaluation du retard (`effective_due` — due samedi → agissable lundi → en retard mardi, toutes surfaces), les défauts muraux/UTC sont morts, et les gabarits lisent des drapeaux calculés côté route au lieu de comparer des datetimes.
- **Lot 2 — pagination par curseur** (`fae40f9`). `truncated` avertissait qu'il y avait une suite et rien ne permettait de l'atteindre. `pagination.keyset_page` + curseur sur `list_time_entries`, `list_expenses`, `list_hearings` (sans index : la borne basse monte sur le champ même du tri), `list_trust_transactions` (chemin exact seulement — les autres formes n'ont que des index ASC et le disent franchement).
- **Lot P — encaissement** (`8c2a1a2`). Le registre demandé exigeait « montant encaissé » et « solde », dont **aucun n'existait**. `amount_paid` + `paid_date` sur la facture, `balance_of` dérivé, `record_payment` transactionnelle avec bascule à `payée` et **annulation étroite de sa propre bascule** (`payée` étant terminal, une saisie erronée aurait immobilisé la facture). `utils/format_fr.parse_cents_fr`. Formulaire web — **supprimé le 2026-08-17**, la comptabilité devenant l'unique écrivain d'un paiement ; **aucun outil MCP n'écrit un paiement** (inchangé). Pas de reprise des 21 factures (décision de l'avocat).
- **Lot 4 — registre des factures** (`94e6d54`). `list_invoices` (routage par argument : index automatique + pagination Python avec `dossier_id`, curseur serveur sans) et `get_invoice` (postes VERBATIM, `subtotal_matches_line_items`, avertissement quand des postes vides sur une facture non nulle trahissent une panne de lecture). `status_group="impayée"` = deux requêtes fusionnées, jamais un `in` + `order_by` + `start_after`.
- **Lot 1 — recherche cabinet** (`ec34059`). Paramètre `scope` sur `list_notes`/`list_documents` ; les rangées gagnent les trois champs de dossier dans **tous** les modes ; `list_documents` relâche son `required` au schéma et le réimpose au gestionnaire.
- **Lot 5 — rapport de couverture** (`1d94cc0`). `mcp/coverage.py` (registre + prédicats purs, aucun import de modèle) + `get_coverage_report` : 13 contrôles, deux sévérités, constats hors portée pour les dossiers fermés, et les deux garde-fous qui suppriment un contrôle plutôt que d'en tirer un manquement. `models/partie.get_parties_bulk`.
- **Lot 3 — `complete_task`** (`eff3ce4`). La seule modification du connecteur. **Cascade complète assumée** (décision de l'avocat) : l'étape liée se complète et le protocole entier peut se clore — divulgué, prévisualisable en `dry_run`, et **relu après l'écriture** plutôt que prédit. Écran de consentement et INSTRUCTIONS réécrits : ils promettaient « rien ne peut être modifié ».
- **Revues adversariales.** Les lots 4 et 1 ont été soumis à une revue en trois dimensions avec réfutation sur preuves. Le lot 4 : 21 constats, 0 confirmé par les réfutateurs — **verdict non retenu**, six étaient exacts (dont `taxable` faisant lire « non taxable » une clé absente, et le plafond de `record_payment` sur le total plutôt que sur le solde dû, qui rendait un solde négatif). Le lot 1 : 23 constats, **13 confirmés avec reproduction**, dont `offset` accepté puis jeté en portée cabinet — le défaut même que le lot supprimait, reproduit par son propre chemin neuf.
- **Ops (ordonné, après le push)** : (1) rien à provisionner — aucun index, aucune dépendance, aucune classe Tailwind nouvelle ; (2) **le scope est gelé à l'émission et le jeton en vigueur a été accordé sous un texte promettant « rien ne peut être modifié »** → retirer le connecteur dans claude.ai, `python -m scripts.revoke_mcp_tokens`, re-ajouter en cochant la case ; (3) vérifier `tools/list` → 33, un `complete_task` en `dry_run` sur une tâche liée à une étape (la cascade doit s'annoncer), et le coupe-circuit `MCP_WRITE_ENABLED=false` ; (4) saisir les encaissements des 21 factures existantes — sans quoi un âge des comptes les lit toutes comme impayées.

### Phase O — Codes de phase du litige (axe 1) (2026-08-10, ✅ code complete)

- **O** — La taxonomie de **phases du litige** (spec `SPEC_PHASE_O_PHASAGE.md`) : `utils/phases.py` (PUR, calqué sur `taxonomie.py`) — **18 phases** (tronc ordonné PRE→JUG 1-9, 7 modules PRL/PRV/INC/EXP/EXE/APP/CJU, ADM transversal, HOR résiduel) → **~60 sous-codes** (`-00` « Général »/`-99` « Autre (préciser) » **synthétisés** partout sauf HOR ; Annexe A épinglée par test). Champs `phase`/`sous_phase` **additifs, sans migration** (patron `date_avis`) sur **quatre surfaces** : `timeentries`, `expenses`, `tasks` et les **étapes de gabarits de protocole** (mapping CQ/CS approuvé 2026-08-10, contenu juridique épinglé ; la tâche auto-créée hérite l'annotation de son étape). Validation **presence-gated, `""` valide, croisement par préfixe** (`phases.validate_pair`, partagée par les 4 surfaces) — l'exigence D-6 vit **au formulaire web seulement** (décision praticien : une exigence modèle casserait DavX5/MCP/auto-tâches). **DAV** : `CATEGORIES` reçoit le **CODE** (jamais le libellé — D-7 ; tuiles jtx en prime) + `X-PALLAS-PHASE`/`-SOUS-PHASE`, relecture en non-effacement strict + repli CATEGORIES + réparation de cohérence dans `update_task`. **MCP** : `create_task`/`create_time_entry`/`create_expense` gagnent `phase`/`sous_phase` optionnels — enums **dérivés** de `utils/phases.py` (import direct, précédent `_COVERAGE_CODES` ; zéro littéral), ergonomie `sous_phase` seule → parent déduit, écho dans `entity` (outputSchema) ; **toujours 33 outils, aucun re-consentement** (paramètres optionnels ≠ scope). **Formulaires** : sélecteur en cascade partagé (`components/_phase_selector.html`, patron domaine→action : JSON non-exécutable, `$nextTick`, remise au `-00`), bande « codes récents » du dossier, **défaut déduit du protocole** (`get_current_phase_for_dossier` — première étape non complétée ; payé seulement au GET avec `?dossier_id=`), décochage auto de « Facturable » sur ADM/HOR (D-8, événement `phase-changed`) ; badges libellés sur les listes, ligne au détail de tâche/étape, colonne « Phase » (libellé, D-13) sur les 6 exports CSV/PDF. **Zéro dépendance, zéro index, zéro classe Tailwind nouvelle (vérifié contre `app.0346c0c3.css` — pas de recompilation), aucun événement d'observabilité nouveau.** 1754 tests (+63).

### Budget par phase — estimation + suivi + PDF client (2026-08-10, ✅ code complete)

- **Budget** — le premier sequel de la Phase O (spec §10 : la jointure protocole ↔ budget ↔ temps, le retrait du classeur Excel « Estimation des frais et honoraires »). Nouvelle feuille **Budget** sous Finances : lignes d'estimation **par sous-code de phase** (heures × taux figé + frais), groupées par phase avec sous-totaux, modules ajoutables (« Procédures spéciales ») ; suivi budget-vs-réalisé (réalisé = **heures travaillées facturables** + toutes dépenses, agrégé en Python sur le scan borné au dossier — aucun index) avec jauges par phase et **seuil 80 % en dollars** (le déclencheur déontologique) ; **versions immuables** (`models/budget.py` append-only — voir le gotcha) avec historique ; **deux PDF reportlab** (`utils/budget_pdf.py`) : « Estimation des frais et honoraires » (portrait, document client, note d'avertissement verbatim du modèle) et « Suivi budgétaire » (paysage, écarts, rangée « Non renseignée ») — sous-totaux par phase, grand total, bloc taux horaire, **pied de page cabinet paginé** (adresse/téléphone/télécopieur/courriel — les FIRM_* complétés dans app.yaml + nouveau `FIRM_FAX`/`cabinet_dict()["telecopieur"]`), montants fr-CA via `format_fr` (jamais `_format_value_pdf`). Événements `budget_saved`/`budget_exported` (IDs/version/comptes — jamais de montants). **Zéro dépendance, zéro index, zéro classe Tailwind nouvelle (pas de recompilation) ; MCP/DAV/gabarits intouchés.**

### Comptabilité d'administration — compte d'opérations + carte de crédit (2026-08-13, ✅ code complete)

- **Admin** — le frère firm-side de la Phase K : un registre pour le **compte d'opérations** et la **carte de crédit corporative** (recettes/dépenses du cabinet, rattachement dossier FACULTATIF, jamais de dimension client). Module PARALLÈLE (`models/admin_ledger.py` + `routes/admin_ledger.py` + `templates/administration/` + `utils/admin_journal_pdf.py`) copiant le harnais trust et abandonnant TROIS mécanismes par décision utilisateur : antidatage libre (le passé toujours inscriptible tant que la période n'est pas conciliée ; le futur refusé sur l'horloge de Montréal), **écritures modifiables/supprimables jusqu'au verrou de conciliation** (piste `revisions` bornée + registre de suppressions ; ensuite contre-passation seulement, à date choisissable), soldes **calculés à la lecture** en ordre `(date, sequence)` (un seul dénormalisé : `ledger_balance`). Dépenses ventilées **Net/TPS/TVQ** (`extract_taxes_from_gross` — reliquat au net, somme exacte ; bouton HTMX « Ventiler ») avec catégories d'exploitation (3e vocabulaire, 17 valeurs) et n° de facture fournisseur ; **pièce justificative** par transaction (direct-à-GCS, whitelist PDF/JPG/PNG/TIFF ≤ 10 Mo, chemin firm-level `users/{uid}/administration/{tx_id}/`) ; **encaissement de facture** (sélecteur GLOBAL des factures impayées, vérifié contre le solde VIVANT, compte d'opérations seulement) qui projette le paiement sur la facture via `record_payment(courant + delta)` — et le **paiement d'honoraires du fidéicommis crée automatiquement sa recette** (orchestration route fail-open, `trust_transaction_id`, contre-passation croisée depuis le fidéicommis) ; **paiement de carte** en deux jambes liées ; **conciliations banque ET carte** (relevé de carte saisi tel qu'énoncé, `statement_to_ledger` convertit le signe ; la complétion VERROUILLE la période, re-vérification statut+etag par écriture) ; exports CSV (Net/TPS/TVQ) + **PDF légal paysage** (11 colonnes, SOLDE REPORTÉ, ligne Σ TPS/TVQ — le dividende CTI/RTI). Première entrée de navigation comptable (base.html ×2, icône `payments` déjà vendorisée). 5 index composites (`admin_transactions` ×4 + `admin_reconciliations`) déployés AVANT le code ; `AdminLedgerEvent` (logger `pallas.admin_ledger`) + spans `admin.transaction`/`admin.reconcile` ; `scripts/verify_admin_integrity.py` (Σ deltas, ventilation, paires, re-preuve des conciliations complétées — la garantie continue du verrou). **Renommage fidéicommis livré dans le même lot : « Virement d'honoraires » → « Paiement d'honoraires »** (libellé seul, clé intouchée ; « Déboursé à un tiers » conservé tel quel — décision utilisateur). Zéro dépendance nouvelle, zéro classe Tailwind nouvelle, DAV intouché ; MCP hors périmètre v1 (seul l'enum d'ENTRÉE de `list_deletions` gagne `admin_transaction` — additif, la sortie type déjà en chaîne libre). **Revue adversariale (48 agents, 4 dimensions, réfutation à 2 sceptiques) : 19 constats confirmés, tous corrigés le jour même** — dont la ventilation SIGNÉE des rapports (le bloc CTI/RTI nette une dépense contre-passée), le refus de contre-passer une correction, le verrou sur la date de compensation, l'horloge Montréal partout, `|jsattr` sur `dossierDisplay` (admin ET trust), le re-swap OOB de l'en-tête au changement de compte, et le plafond trust remis sur le solde VIVANT. 111 tests nouveaux (modèle 70, intégration 30, PDF 11) + 1 test trust.

### Lot Q — Reprise de données historiques par le connecteur MCP (2026-08-16, ✅ code complete)

- **Lot Q** — le juriste transcrit dans Athéna un fonds de dossiers tenu manuellement dans un ancien système (feuille Excel de dossiers, PDF de notes d'honoraires détaillées). Le travail d'interprétation est celui d'un modèle, pas d'un script : d'où le connecteur plutôt qu'un `scripts/import_*.py`. **33 → 43 outils (26 lecture + 17 écriture)** ; zéro dépendance, **zéro index composite**, zéro classe Tailwind, aucun changement de schéma Firestore hors un champ additif.
- **Douze décisions du juriste**, arrêtées avant l'écriture d'une ligne : (D-1) les contacts sont dans le périmètre ; (D-2) les factures gardent **numéro et date d'origine**, le compteur annuel n'étant jamais lu ni avancé ; (D-3) **modifications oui, suppressions non** ; (D-4) les factures importées restent **brouillon** — ni statut ni paiement par MCP, l'exclusion du lot P tient ; (D-5) composition **source d'abord** ; (D-6) les PDF sont détaillés ; (D-7) tout à TPS 5 % / TVQ 9,975 % ; (D-8) `status` à la création d'un dossier, refusé ensuite ; (D-9) **un seul scope** `athena:write`, révocation + reconsentement ; (D-10) chaque enregistrement importé porte un **`legacy_ref`** ; (D-11) les heures acceptent **deux décimales** ; (D-12) un **ajustement nommé** débloque une facture irréconciliable.
- **Modèle.** `create_invoice` gagne quatre arguments par mot-clé, inatteignables depuis `request.form` : `invoice_number` (le compteur n'est alors ni lu ni avancé ; le millésime courant est refusé), `expected_total` (aucune tolérance — l'écart, la ventilation et « N lignes retenues pour M sources fournies »), `require_all_sources` (l'escamotage silencieux devient un refus nommant chaque fautif — **dans le modèle**, la pré-lecture du gestionnaire étant un instantané TOCTOU), et `adjustment` (UN poste nommé, typé `fee`, le seul du système sans `source_id`). La lecture d'unicité du numéro vit DANS la transaction et échoue **fermée**. `get_dossier_by_file_number` remplace un filtre Python sur les 200 dossiers les plus récemment OUVERTS — exactement la fenêtre hors de laquelle vit une reprise. `billing_address_from` remonte de `routes/invoices.py` au modèle, comportement inchangé. `legacy_ref` sur cinq modèles + `models.find_by_legacy_ref`, générique et fail-closed.
- **Lecture.** Le connecteur ÉCRIVAIT `phase`/`sous_phase` sans pouvoir les relire : les deux relevés de facturation et `_task_row` les portent désormais, avec leurs libellés, `created_via` et les horodatages. `get_reference_vocabulary` expose les six vocabulaires que les modèles valident sans jamais les énumérer dans un refus — la classification était sinon indevinable. `find_imported` et `get_import_audit` (7 contrôles purs, `mcp/import_audit.py`) complètent la vérification.
- **Doctrine.** `destructiveHint` cesse d'être une constante de famille : `EDIT_TOOLS` le dérive. `INSTRUCTIONS` et l'écran de consentement sont réécrits — « CREATE-ONLY », « une seule modification » et « les factures … intouchables » sont devenus faux le même jour. `_PHASED_TOOLS`, le tuple que le dépôt nommait lui-même comme un risque de péremption, devient un balayage dérivé.
- **Prérequis d'exploitation, ordonnés :** (1) rien à provisionner — les trois requêtes ajoutées sont des égalités simple-champ servies par l'index automatique ; (2) vérifier la récupération Firestore à un instant donné (ce lot n'a pas d'annulation) ; (3) déployer les commits 1 à 6 (inertes ou lecture seule) et confirmer `tools/list` → 36 ; (4) confirmer `GST_NUMBER`/`QST_NUMBER` ; (5) **poser `MCP_WRITE_ENABLED: "false"` AVANT le commit 7** — c'est la mitigation de la fenêtre d'escalade que D-9 laisse ouverte : avec un seul scope, le jeton en vigueur verrait les nouveaux outils dès leur déploiement, sous un écran de consentement qui disait les factures et les contacts intouchables ; (6) déployer les commits 7 à 11 ; (7) **révoquer et reconsentir** (`python -m scripts.revoke_mcp_tokens`) ; (8) remettre `"true"` et vérifier `tools/list` → 43 ; (9) essais à blanc (le refus en branche SÈCHE d'une entrée facturée, le bloc d'adresse partiel, un `expected_total_cents` volontairement faux) ; (10) conventions d'`idempotency_key` et de `legacy_ref` ; (11) **pilote sur UN dossier complet**, réconcilié à la main contre le PDF — c'est là qu'on tranche le `taxable` du poste d'ajustement ; (12) vérifier que `counters/invoices-{année}`.`seq` est **inchangé** après chaque lot ; (13) attendre la secousse DavX5 (N contacts = N resynchronisations du carnet) ; (14) après la reprise : `get_import_audit` par dossier, **promouvoir les factures à la main** (§ IMP-07 — deux gestes chacune, sans quoi le « Journal des honoraires » les imprime à 0 $ reçu), rattacher les PDF d'origine, puis révoquer.

### Encaissements — la comptabilité devient l'unique écrivain d'un paiement (2026-08-17, ✅ code complete)

- **Contexte.** Le formulaire d'encaissement de la fiche de facture (`POST /factures/<id>/paiement`) datait du **lot P (2 août 2026)** et précédait de onze jours le module de comptabilité (13 août) : c'était un second écrivain d'`amount_paid`, indépendant du grand livre — ce que `verify_admin_integrity` assumait en dégradant tout écart en simple note, « the invoice's manual payment form is a legitimate second writer ». La production a montré le prix de cette dualité : **5 factures portaient 5 397,36 $ de paiements pour ZÉRO écriture comptable** (3 venaient d'un paiement d'honoraires du fidéicommis saisi sans compte d'administration, 2 d'un encaissement bancaire présumé), le compte d'opérations était sous-évalué d'autant, et le contrôle d'intégrité **ne pouvait pas le voir** — il n'examinait que les factures déjà citées par une écriture.
- **Décisions du juriste :** (D-1) les cinq paiements sont **effacés** et ressaisis à la main en comptabilité ; (D-2) `amount_paid` **reste un champ stocké**, écrit uniquement par la comptabilité — le calculer à la lecture coûterait une requête par ligne dans cinq chemins déjà chauds (`journal_pdf._journal_rows` délibérément non plafonné, `balance_of`, `_factures_impayees`, le plafond du virement, le contrat `outputSchema`), et le garde-fou recomputable existe déjà (`sum_invoice_receipts`) ; (D-3) les 19 factures « payée » **sans** montant sont hors périmètre (c'est la parole de l'avocat) ; (D-4) sur un **paiement d'honoraires** au fidéicommis, le compte d'administration devient **obligatoire**.
- **Ce qui disparaît :** la route `invoice_record_payment`, la carte « Encaissement » du gabarit, les imports devenus morts (`parse_cents_fr`, `record_payment` — `balance_of` RESTE), et le contrat de query-string `?erreur=montant|date|paiement`. **La cascade des totaux (« Encaissé », « Solde ») ne bouge pas** : c'est de l'affichage d'argent, pas le formulaire.
- **Ce qui la remplace :** un bloc « **Paiements** » en LECTURE SEULE (`models/admin_ledger.list_invoice_receipts` — la requête d'égalité simple-champ de `sum_invoice_receipts`, servie par l'index automatique, tri `(date, sequence)` en Python), qui **garde les contre-passées** (les cacher laisserait le mouvement du solde sans explication) et **échoue OUVERT** (aide à l'affichage, pas le registre — `sum_invoice_receipts` garde son fail-closed, elle projette de l'argent). L'état vide dit OÙ se saisit un paiement, mais seulement sur une facture émise.
- **D-4, la brèche du fidéicommis :** l'option « Aucune (saisie manuelle) » du sélecteur de compte laissait sortir les fonds sans écriture ni imputation. La garde est dans la **route**, avant `create_transaction` (le formulaire n'est pas une garde ; un refus postérieur laisserait au registre un mouvement que rien ne compense) et ne vise que `virement_honoraires`. `_comptes_administration` rend désormais `(comptes, lisible)` : sans ce second membre, une panne de lecture s'afficherait comme une absence de compte et inviterait à en ouvrir un **en double** — la doctrine que le hub Comptabilité a déjà payée.
- **Le contrôle nº 8 passe de note à ERREUR**, balaie TOUTE facture portant un montant encaissé, et lit le cumul depuis le **modèle** : sa copie locale omettait `reversed_by_id` et aurait signalé un faux écart après toute contre-passation d'un encaissement compensé.
- **Migration** (`scripts/purge_encaissements_factures.py`, `--dry-run` par défaut) : il n'écrit pas Firestore, il appelle `record_payment(id, 0)`. Le détail load-bearing n'est pas l'effacement mais la **bascule inverse « payée → envoyée »** — `_ISSUED_INVOICE_STATUSES = ("envoyée", "en_retard")`, donc une facture restée « payée » n'apparaîtrait dans aucun sélecteur **et** `create_transaction` la refuserait (`facture_non_émise`) : la ressaisie serait impossible. L'annulation étant étroite (une « payée » PORTANT un paiement), les 19 autres ne sont pas touchées. La sélection est testée — une facture déjà adossée est épargnée (rejouable), un cumul illisible **épargne** la facture et le dit.
- **Zéro dépendance, zéro index, zéro classe Tailwind, aucun changement de schéma.** Le contrat MCP ne bouge pas (`payment_basis`, `amount_paid`, `balance_cents` restent des champs stockés de même forme) ; seules leurs descriptions cessent de dire « entered in the application ». 2381 tests (+17).
- **Exploitation, ordonné :** (1) déployer ; (2) `python -m scripts.purge_encaissements_factures` en simulation, LIRE la liste, puis `--apply` ; (3) ressaisir les paiements qui ne viennent pas du fidéicommis directement au registre ; (4) `python -m scripts.verify_admin_integrity` : aucun écart.
- **Suite immédiate (2026-08-17) — la reprise des 40 virements.** La production a montré que les cinq factures n'étaient que 16 % du trou : **40 virements d'honoraires** ont quitté le fidéicommis depuis 2025-09-12 pour **34 343,39 $**, en regard de **4 recettes totalisant 265,00 $** au compte d'opérations. Le juriste ayant refusé de contre-passer les virements (ils sont couverts par des conciliations complétées, et une contre-passation ferait dire au registre du fidéicommis un mouvement qui n'a jamais eu lieu), la réparation se fait **entièrement du côté administration** — ce que le logiciel permet, son chemin automatisé n'écrivant JAMAIS vers le fidéicommis (`routes/trust.py:370-413`). Décisions : portée = les 40 ; le sens du statut s'inverse (le gotcha « payée » ci-dessus) ; l'écriture porte `trust_transaction_id` **en connaissance du prix** (elle devient incorrigible depuis Administration — choix réaffirmé après qu'une troisième voie ait été offerte) ; et **on attend la fin de la reprise du lot Q** avant d'exécuter. Le rapprochement n'étant PAS dérivable des données (3 factures reçoivent plus que leur dû, 1 référence en nomme deux, une bonne part cite un numéro pas encore importé), l'outil est un **assistant**, pas une migration : `--proposer` écrit un CSV que le juriste corrige, `--verifier` rejoue toutes les gardes sans écrire, `--appliquer` exécute le fichier signé.
- **Ce que la reprise a réellement appris (2026-08-17).** Le juriste proposait d'écrire les 40 recettes puis de leur reporter la facture ; **c'est impossible** — `update_transaction` ignore `invoice_id` EN SILENCE et rend un succès, le changement de `kind` vers `encaissement_facture` est refusé, et un lien posé de force serait inerte (`sum_invoice_receipts` exige le `kind`, `list_invoice_receipts` non : la facture afficherait un paiement qu'aucun total ne compte). Le repli « autre recette maintenant, lien plus tard » échoue aussi, `_entry_lock_reason` verrouillant sur `trust_transaction_id` SEUL. Mais l'exercice a mis au jour ce qui bloquait vraiment : **huit factures portant une provision** (gotcha ci-dessus), **des virements couvrant plusieurs factures**, une garde « autre dossier » de mon invention qui refusait des rapprochements justes, et **cinq factures jamais importées** — dont deux découvertes en posant les questions d'arbitrage une par une, et importées séance tenante. Le rapprochement automatique n'est possible qu'en **normalisant les tirets** ET en cherchant sur le `legacy_ref` du lot Q : le virement cite « 251601-01 » quand la facture importée s'appelle « 25160101 ».

### Séries récurrentes au Calendrier (2026-08-18, ✅ code complete)

- **Contexte.** Le Calendrier ne savait enregistrer qu'une audience isolée : une rencontre hebdomadaire ou un suivi mensuel se saisissait N fois, et se corrigeait N fois. Demande du juriste : une fréquence (hebdomadaire / mensuelle / trimestrielle / annuelle), une date de départ, une **fin obligatoire** (date de fin **ou** nombre d'occurrences), tout-la-journée ou horodatée, et des occurrences **liées** — supprimer ou modifier la chaîne, ou en détacher une pour la traiter seule.
- **MATÉRIALISATION**, décision structurante. Une série s'étend en N audiences ordinaires partageant un `serie_id` ; **aucun lecteur existant ne change** — tableau de bord, grille du mois, onglet du dossier, collection DAV, MCP, exports, miroir Outlook. L'autre voie (un document porteur d'une `RRULE`, étendu à la lecture) a été écartée sur preuves : la collection `hearings` n'a **aucun index composite** et toutes ses requêtes bornées filtrent ET trient sur `start_datetime`, dont un document à règle n'a qu'un exemplaire — la série disparaîtrait de chaque fenêtre dès sa première occurrence passée ; et le miroir Outlook **purge les doublons** (plus d'un événement par `hearing_id` → il garde le plus petit id et SUPPRIME le reste, toutes les 10 minutes).
- **Décisions du juriste :** portées « cette occurrence » + « cette occurrence et les suivantes » (pas de portée englobant le passé) ; **occurrences passées protégées** de toute action de chaîne — un écart assumé, `hearing_delete` supprimant aujourd'hui n'importe quelle audience quelle que soit sa date ; **plafond 60** ; lot 1 = créer / supprimer la chaîne / détacher, l'édition propagée à toute la chaîne étant remise à un second lot (modifier UNE occurrence marche déjà — c'est une audience ordinaire).
- **Livré :** `utils/recurrence.py` (pur, réutilise `recours._add_months`/`_add_years` via le nouvel alias public `add_period` — **aucune arithmétique de date nouvelle**) ; `models/hearing.create_hearing_series`/`list_series`/`delete_series`/`unlink_hearing`/`occurrence_day` + les champs additifs `serie_id`/`serie_rule` ; `dav/sync.record_tombstones_bulk`/`bump_ctag_in_batch`/`record_tombstones_in_batch` ; la route `/audiences/<id>/detacher`, la portée `scope` sur la suppression, la vue `?serie=` ; le bloc de répétition au formulaire (création seulement) et le dialogue à deux portées ; la famille d'événements `pallas.hearing` (`routes/hearings.py` n'émettait **aucune** ligne de journal).
- **Trois bogues PRÉEXISTANTS corrigés dans le même lot** — aucun n'était atteignable, tous le deviennent dès qu'un calendrier peut porter 60 événements liés : (1) la garde de troncature du miroir Outlook se mesurait sur la liste FILTRÉE, si bien qu'un seul import `refusée` réarmait la suppression de vraies dates de cour dans Outlook ; (2) une lecture Firestore en échec rendait `[]`, indiscernable de « rien ne correspond », ce qui faisait passer TOUS les miroirs pour orphelins ; (3) fermer un dossier chargé drainait ses pierres tombales une par une (deux allers-retours chacune, bump en dernier) et dépassait le délai de 60 s de gunicorn — un SIGKILL y est irrécupérable. `_LIMITE_FENETRE` relevé de 500 à 1500 **après** la réparation de (1). Corrigé aussi : `DTEND` d'un all-day valait `DTSTART` (RFC 5545 §3.8.2.2), qu'une série aurait multiplié par N.
- **Zéro dépendance, zéro index composite, zéro classe Tailwind nouvelle** (vérifié contre `app.0346c0c3.css` — pas de recompilation), **aucune régénération d'icône** (`tests/test_icons.py` est une porte de déploiement dans trois directions : la puce « Série » est un chip TEXTE), **aucun changement de `cron.yaml`**, **aucun re-consentement MCP** (`serie_id` n'est pas émis, `create_hearing` reste unitaire ; seul l'enum d'ENTRÉE de `list_deletions` gagne `hearing_series`, additif), **aucune migration**, **aucune ré-addition du compte DavX5** (ni chemin de collection, ni nom d'affichage, ni jeu de composants ne changent). 2563 tests (+97).

### Reclassement de phase sur une ligne facturée (2026-08-18, ✅ code complete)

- **Contexte.** Phase O a donné aux entrées de temps et aux déboursés une annotation `phase`/`sous_phase` que le budget lit — `budget.aggregate_actuals` compte TOUT le temps facturable et TOUS les déboursés, facturés compris. Le travail antérieur à Phase O, ou mal étiqueté, tombait donc dans la rangée « Non renseignée » de chaque vue budget-vs-réalisé, et rien ne pouvait le corriger : les deux `update_*` refusent une ligne facturée, les deux formulaires masquent leur bouton, et le gestionnaire MCP répète le refus avant la branche sèche.
- **Ce qui a été tranché** (décisions du juriste, 2026-08-18) : capacité **permanente**, pas de commutateur (le gel de ce couple n'a jamais été une décision — c'était le dommage collatéral d'une garde visant les montants) ; **temps ET déboursés** (les deux moitiés de `aggregate_actuals`) ; **outils dédiés** plutôt qu'un relâchement des `update_*` ; **lot plafonné à 50** pour une reprise d'un an d'historique.
- **Modèle.** `set_time_entry_phase` / `set_expense_phase` + `get_time_entries_bulk` / `get_expenses_bulk` (fail CLOSED — voir le gotcha). La garantie est la forme de l'écriture : `update()` de quatre clés. Saut du non-changement, donc une passe est rejouable. Nouveau helper PUR `phases.resolve_pair`, partagé par les deux modèles et `handlers._resolve_phase_pair`, qui cesse ainsi de dupliquer l'ergonomie.
- **Connecteur.** 43 → **47 outils** (26 lecture + 21 écriture) : `set_time_entry_phase`, `set_expense_phase` et leurs jumeaux `_bulk`. Membres de `WRITE_TOOLS` ET d'`EDIT_TOOLS` (`destructiveHint`), et les premiers écrivains **idempotents** après `complete_task` — le test qui affirmait que `complete_task` était le seul a été renommé et son ensemble épinglé. Le lot rend un rapport **ligne par ligne, dans l'ordre de la demande** (applied / unchanged / refused + `reason`) ; un id en double refuse TOUT l'appel (deux codes pour une ligne n'ont pas de résultat unique à rapporter) ; `dossier_id`/`invoiced` sont **null** sur un refus plutôt que défaultés, parce qu'affirmer « non facturée » d'une ligne jamais lue serait inventer un fait sur elle.
- **Application.** Formulaire dédié `/temps/<id>/phase` et `/temps/depenses/<id>/phase` (gabarit partagé), atteint depuis la bannière ambre du formulaire d'édition. Événements `phase_reclassified` (web) et `mcp_phase_bulk` (comptes seuls — `mcp_write` ne porte pas d'`entity_id` pour un lot).
- **Zéro dépendance, zéro index, zéro classe Tailwind nouvelle** (les 99 classes des trois gabarits vérifiées contre `app.0346c0c3.css` — pas de recompilation), DAV intouché (`timeentries`/`expenses` ne sont pas exposés). 2627 tests (+58).
- **Exploitation, ordonné :** (1) `MCP_WRITE_ENABLED: "false"` déployé D'ABORD — le scope est gelé à l'émission, donc le jeton en vigueur verrait les quatre outils dès leur déploiement, sous un écran de consentement qui disait une ligne facturée figée à jamais (la mitigation du lot Q, pour la même raison) ; (2) déployer ; (3) `python -m scripts.revoke_mcp_tokens`, retirer et re-ajouter le connecteur en cochant « Autoriser les écritures » ; (4) remettre `"true"`, vérifier `tools/list` → 47 ; (5) reprendre dossier par dossier : `list_time_entries` → proposer les codes → `_bulk` en `dry_run` → relire → commettre avec une `idempotency_key`, puis vérifier l'onglet Budget.

### Proposed / not yet implemented

- **Microsoft 365 bidirectional sync** — Graph API OAuth2 + webhook (change notifications) for native Outlook calendar/contacts integration. The **push half is done** (the 2026-07-29 Outlook mirror above, with its extended-property loop prevention); what remains proposed is the inbound half (webhook change notifications honoring Outlook-side edits) and contacts.
- **Dedicated KYC / conflict-check routes** — model helpers exist (`update_kyc_status`, `link_kyc_document`) but are not yet exposed as discrete routes
- **R2 migration** for Firebase Storage (cost optimization, low priority)
- **Turnstile** migration from reCAPTCHA Enterprise (optional)
- **`models/vocab.py` consolidation** — the controlled vocabularies (hearing type, note/document category, statuses…) are each mirrored across model + route + form + list + tab + MCP schema. The 2026-07-24 vocabularies rework, which touched three of them in ~15 files, is the strongest argument yet; a single source with derived label/color maps would remove the mirror-drift class of bug. Not yet done.

---

## Conventions for Adding New Features

When building a new module or feature, follow the existing patterns:

1. **Schema first**: define the Firestore document shape in this document, including the DAV UID field if the resource is DAV-exposed.
2. **Model second**: implement standard CRUD (`create_X`, `get_X`, `list_X`, `update_X`, `delete_X`) with `_normalize` (where applicable) + `_sanitize_data` + `_validate` pipeline. Add DAV serializers if needed.
3. **CTag bumping**: every mutation of a DAV-exposed collection bumps the CTag. Track every call site.
4. **Routes third**: one blueprint, French UI labels, HTMX for dynamic interactions, FAB "+" on list views, confirmation dialogs on delete.
5. **Templates fourth**: mobile-first, extend `base.html`, use `components/` partials, ensure 44px touch targets.
6. **Export support**: if the module has a list view, add CSV + PDF export routes using `utils/export_csv.py` and `utils/export_pdf.py`.
7. **Testing checklist**: add a testing checklist documenting expected behavior; add `tests/test_<feature>.py` if there is non-trivial pure logic.
8. **Update this document** to reflect the new module in every relevant section (Directory Structure, Data Model, Routes, Model Layer Reference).

For improvements/patches to existing modules, the same applies incrementally — prefer editing existing models over creating parallel ones, and always maintain backward compatibility with existing Firestore documents (use a `_migrate_*` helper on read where necessary, as `dossier._migrate_parties` does).
