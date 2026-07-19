# Pallas Athena — Master Reference

Pallas Athena is a single-user legal practice management web application for a Québec civil litigation lawyer (Jason Poirier Lavoie, Barreau du Québec). It manages contacts (parties), dossiers (case files), billable hours, expenses, invoices, hearings, tasks, case protocols, notes, and procedural documents. It synchronizes bidirectionally with DavX5 on Android via CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL).

Deployed at `athena.poirierlavoie.ca`. GCP project: `athena-pallas`. Codebase is on GitHub; deploys via Cloud Build trigger on push to main.

This document supersedes the old `SPEC.md` and phase-specific markdown files as the canonical reference for future work.

---

## Change Impact Assessment (do this before any change)

Several subsystems are **delicate and coupled to external frameworks/services, and they fail SILENTLY** — the change looks fine, tests may pass, and the breakage only surfaces later in production or on a synced device. **Before implementing any change or improvement, run a one-line impact check against each component below.** State explicitly whether the change touches it; if it does, say what you verified. Most changes touch none — the discipline exists so one is never broken silently. (A trivial change — docs, or a pure-logic helper with no runtime surface — may just note "no impact on the four" and proceed.)

1. **MCP connector** (`mcp/`, exposes read-only data to Claude). *Touches it when the change edits:* `auth.py`/session, `security.py` (CSP, CSRF exemptions, rate limits, request-size caps, the App Check HTMX predicate), `config.py`, the OAuth collections, or any `models/*` function the 17 tools read. *Verify:* OAuth/bearer flow + `MCP_ENABLED` kill switch intact; no PII or signed URLs in tool output; date-only fields use `mcp.tools.date_str`, never `to_mtl`; no MCP-only query needs a new composite index; `test_mcp_*` green.

2. **DavX5 sync** (`dav/` + the model DAV serializers). *Touches it when the change edits:* any `X_to_vcard/_to_vevent/_to_vtodo/_to_vjournal` serializer or its inverse parser, a CTag-bump call site, collection structure/naming, a field those serializers read, **or adds any new write path to a DAV-exposed collection** (bulk import, migration, cron, a future write-capable tool). *Verify:* output stays RFC-5545/vCard-valid with the **mandatory `UID`/`DTSTAMP`/`CREATED`** present (the jtx `icalobject.created` NOT-NULL trap); every mutation bumps its CTag — **CTag bumping lives in the route layer, not the models, so a write that bypasses the existing routes never bumps and DavX5 silently never re-syncs**; root Depth:1 PROPFIND discovery unaffected. **DavX5 fails silently — test the endpoint with `curl` before trusting it.**

3. **Template generation / field matching** (`utils/docx_fill.py`, `utils/template_fields.py`). *Touches it when the change edits:* the fill-engine regexes / run-normalization / split-run detection, the field catalog or `FLAT_ALIASES`, or a `models/partie|dossier` field name the catalog resolves. *Verify:* placeholders still fill on every occurrence (normalization + per-occurrence detection intact); output opens in Word **without repair** (never introduce a `docxtpl`/`python-docx` round-trip); `resolve_values` still maps every catalog field; `test_docx_fill`/`test_template_fields` green.

4. **Observability & logging** (`utils/logging_setup.py`, `utils/tracing_setup.py`, `athena/OBSERVABILITY.md`). *Touches it when the change adds a log line, a span, or a new user-facing data path.* *Verify:* emit only through the typed helpers (never raw `logger.*`); no PII (wrap interpolated user values in `sanitize_log_value`; the redaction filter only auto-scrubs emails/phones/postal/court-file — **names, titles, and client strings are NOT auto-redacted**, and it sits on the single root handler, so a second handler bypasses it); new events/spans registered in `OBSERVABILITY.md`; structured fields land in `jsonPayload` (production uses `CloudLoggingHandler`, not the deprecated `AppEngineHandler`); spans carry IDs/counts only, never names/bodies/tokens.

5. **Security & edge defense** (`security.py`, the `main.py` `before_request` chain, `app.yaml`, Cloudflare/GCP config). *Touches it when the change edits:* response headers/CSP, the CSRF exemption list, rate-limit or brute-force keys, request-size caps, the `before_request` path-prefix allowlists (`/_ah/`, `UPLOAD_PATHS`, `_is_template_upload_path`, the App Check exempt prefixes), any `Config` secret name (`cf-origin-secret`, `RECAPTCHA_ENTERPRISE_SITE_KEY`), or adds a route under a prefix those checks special-case. *Verify:* App Check and the origin-secret check **fail open when their key is unset** — a dropped/renamed secret silently disables them (warned once, easy to miss); a new sensitive route isn't accidentally under an exempt prefix; `CF-Connecting-IP` (rate-limit/brake key) is only trustworthy while the App Engine firewall + origin secret hold — don't widen the firewall; CSP is **enforced** with a **per-request nonce** on `script-src` (`'self' 'nonce-…' 'unsafe-eval'` + the Google reCAPTCHA origins — no `'unsafe-inline'`, no `ajax.cloudflare.com`; `build_csp`/`csp_nonce` in `security.py`), so a new inline `<script>` runs **only** if it carries `nonce="{{ csp_nonce }}"` and an injected/un-nonced inline script is **blocked today**; inline `on*` handlers are refused outright (a nonce can't authorize an attribute) — wire events via `addEventListener` on `data-` attributes as `base.html` does. `'unsafe-eval'` stays for Alpine's `new Function()`, `style-src` keeps `'unsafe-inline'` for reCAPTCHA; prefer external/vendored JS; a new browser/session POST goes in its own blueprint, never onto a CSRF-exempt one (`mcp_bp` is blanket-exempt).

6. **Frontend asset pipeline** (`static/vendor/`, `static/src/app.input.css`, `static/sw.js`, `base.html`, the `_EARLY_HINTS_*` lists in `security.py`). *Touches it when the change edits:* a template's Tailwind classes, `app.input.css`, any vendored asset, or the `<script>` block in `base.html`. *Verify:* a new/renamed class went through the **full recompile + rehash fan-out** (new `app.<hash>.css` → the `<link>` in `base.html` + `auth/login.html`, the `PRECACHE` in `sw.js`, the `_EARLY_HINTS_*` in `security.py`; delete the old hash) — a class absent from the compiled artifact silently doesn't apply; **never edit a `static/vendor/` file in place** (immutable 1-year cache → returning devices keep the stale copy) — always a new hash filename plus a bumped `sw.js` cache version; the footer script document-order (App Check boot → htmx → Alpine) is load-bearing — execution follows document order (the Firebase/App Check boot scripts are synchronous, at parse time; the vendored htmx/Alpine `defer` scripts run at `DOMContentLoaded`), so reordering silently breaks App Check on `hx-trigger="load"` requests or Alpine `x-data` evaluation.

7. **Firestore indexes & query invariants** (`firestore.indexes.json`, model queries). *Touches it when the change edits:* any `.where()`+`.order_by()` combo, a new filter value / sort field / cursor-paginated list, or a SUM/AVG aggregation. *Verify:* the composite index exists and **deploys before or with the code** (`firebase deploy --only firestore:indexes`) — until it finishes building the query fails and the view silently degrades to an empty list; a SUM/AVG needs its **own** aggregation index whose trailing fields are the aggregated fields in **alphabetical order** with directions matching the query's last sort — a same-fields index in the wrong tail order is ignored and the total silently reads zero (the June 2026 dashboard "heures non facturées" incident).

**Dependency bumps are a silent trigger too.** The weekly Dependabot minor/patch PRs can change behavior these four depend on with no repo code change: `icalendar`/`vobject` (DAV & vCard serialization shape — note `partie_to_vcard` string-patches vobject's output to vCard 4.0), `google-auth`/`google-cloud-storage` (signed-URL IAM signing), `google-cloud-logging` (whether `json_fields` reaches `jsonPayload`), `opentelemetry-*` + the `setuptools<81` pin (trace ↔ log correlation). When such a PR lands, re-run the relevant check above instead of assuming a version-only diff is safe.

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
  # auth/login.html, the PRECACHE list in static/sw.js, AND the Early Hints
  # lists in security.py (_EARLY_HINTS_*); delete the old hashed file;
  # remove node_modules afterwards
  ```
  The Firebase App Check bootstrap is also a vendored, hash-named asset (`static/vendor/appcheck-boot.<hash>.js`), configured via a non-executable JSON block in `base.html` — same rules apply if it changes (re-hash, update base.html + sw.js + security.py).
  **Script order in `base.html`/`login.html` is load-bearing:** Firebase/App Check boot → page scripts → htmx → Alpine, all at the end of `<body>`. Execution follows *document order* — the Firebase/App Check boot scripts run synchronously at parse time, and the vendored htmx/Alpine `defer` scripts run in document order at `DOMContentLoaded`; position, not a sync/defer phase, is the guarantee. (Cloudflare Rocket Loader, which used to defer every script while preserving that order, was **disabled at the edge on 2026-07-11** and is not returning.) Never move htmx/Alpine above the App Check boot or above inline component definitions.
  Vendored assets are served `Cache-Control: immutable` (1 year) — **a changed asset MUST get a new filename**; never edit one in place. Dynamically-assembled class names get purged at compile time: keep classes as complete string literals in templates / `routes/*.py` / `models/*.py` (all scanned via `@source`), or safelist them in `app.input.css`.
- **DAV libraries:** `icalendar`, `vobject`. Custom CardDAV/CalDAV/RFC-5545 endpoints served directly from Flask.
- **MCP connector (Phase I):** a hand-rolled, stateless **JSON-response-mode Streamable HTTP** MCP server at `POST /mcp` (no SSE, no sessions) plus an **embedded OAuth 2.1 authorization server** (`mcp/` package), exposing 17 read-only tools to Claude as a custom connector (14 original + 3 Phase-K trust). **Zero new Python dependencies** — stdlib (`secrets`, `hashlib`, `base64`) + packages already pinned (Flask, flask-limiter, flask-wtf). Kill switch: `MCP_ENABLED` env var (default `"true"`; `false` → every `/mcp` + `/oauth/*` route 404s).
- **Markdown:** `Markdown` + `bleach` libraries for rendering note content (rendered via Jinja `markdown` filter).
- **PDF:** `reportlab` (pure Python — do NOT use `weasyprint`; it requires cairo/pango system libs unavailable on App Engine Standard).
- **Word templates (Phase H — gabarits):** user-managed `.docx` templates filled by a **stdlib-only engine** (`utils/docx_fill.py`: `zipfile` + `re` + `io` — direct string substitution on the XML zip entries, every other entry copied byte-identical). **`docxtpl`/`python-docx` are explicitly rejected** — their load/save round-trip rewrites enough of the OOXML package that Word refuses to open letterhead templates with multiple headers/footers, `titlePg` sections, and embedded fonts. Zero new Python dependencies. **The full placeholder inventory (all `{{…}}` names + syntax) is in [`GABARITS_PLACEHOLDERS.md`](GABARITS_PLACEHOLDERS.md).**
- **Hosting:** Google App Engine Standard, Python 3.13 runtime, F2 instance class.
- **CDN / edge:** Cloudflare **Pro plan** (Full Strict SSL, Origin Certificate, Access Zero Trust for `/dav/*`, Argo Smart Routing, **Early Hints** — `security.py` emits the `Link` preload headers Cloudflare converts to HTTP 103; **Rocket Loader was disabled at the edge on 2026-07-11**). The App Engine firewall accepts only Cloudflare IP ranges, so the edge is not bypassable (see Security Rules → Edge defense in depth).
- **PWA:** manifest + service worker (`static/sw.js`) for offline fallback + stale-while-revalidate caching of `/static/vendor/` and `/static/icons/` assets (never authenticated HTML); Trusted Web Activity wrapper for Android (assetlinks.json served at `/.well-known/assetlinks.json`).
- **Observability:** structured logging to Cloud Logging (`utils/logging_setup.py` — request-context fields + PII redaction filter) and distributed tracing to Cloud Trace via OpenTelemetry (`utils/tracing_setup.py` — Flask/requests/Jinja2 auto-instrumentation, 10% prod sampling, PII-sanitizing exporter). **`athena/OBSERVABILITY.md` is the event/span registry — read it before adding log events or spans.**
- **CI/CD:** Google Cloud Build trigger on GitHub push to main; `cloudbuild.yaml` runs the pytest suite as a deploy gate (hash-locked install), deploys, and prunes old versions. GitHub Actions provide security scanning (CodeQL, OSV-Scanner, Trivy, Bandit, dependency-review, OpenSSF Scorecard) and Dependabot keeps pins moving (weekly grouped minor/patch PRs).

### Python dependencies (`requirements.in` → `requirements.txt`)

Direct dependencies live in **`athena/requirements.in`** with **exact pins** (`==X.Y.Z` — wildcards break OSV-Scanner version resolution). `athena/requirements.txt` is a **generated, hash-locked lockfile — never edit it by hand**. To change a dependency, edit `requirements.in` and re-lock:

```
uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt
```

(Compiling over the existing output file preserves unrelated transitive pins.) Production installs run with `PIP_REQUIRE_HASHES=1` / `PIP_NO_DEPS=1` (set in `app.yaml`), so an unhashed or out-of-band package cannot deploy. CI/dev-only tools (pytest) live in `athena/requirements-dev.txt`, which is never deployed.

Direct deps beyond the original core set: `google-cloud-logging`, the OpenTelemetry stack (`opentelemetry-api`/`sdk` 1.27.0 + GCP trace exporter/propagator 1.7.0 + Flask/requests/Jinja2 instrumentation 0.48b0 — versions are paired), `Pillow` (transitive via reportlab, pinned explicitly for CVE hygiene), `defusedxml`, and a `setuptools<81` constraint (OTel instrumentation 0.48b0 still imports `pkg_resources`; removable once OTel is bumped to ≥0.50b0).

(`google-cloud-storage` is listed for completeness; storage operations actually go through `firebase-admin.storage`, which uses the same underlying client.)

---

## Architecture Rules

1. **SINGLE USER.** Exactly one authorized email (`AUTHORIZED_USER_EMAIL` env var). No multi-tenancy, no registration, no roles. Every endpoint that mutates state verifies the session via `@login_required` (Firebase Auth + server-side session). DAV endpoints use a separate HTTP Basic auth.
2. **Firestore is flat.** Despite the single-user nature, Firestore **collections are top-level** (`parties`, `dossiers`, `tasks`, `hearings`, `notes`, `protocols`, `invoices`, `timeentries`, `expenses`, `documents`, `doc_templates`, `dav_sync`, `counters`, `ref_greffes`, `ref_juridictions`, `ref_palais`, the Phase-I OAuth collections `oauth_clients`, `oauth_codes`, `oauth_tokens`, plus the Phase-K trust collections `trust_accounts`, `trust_transactions`, `trust_reconciliations`). They are **not** nested under `users/{userId}/...`. Firebase Storage paths, however, **do** use `users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}` (with `userId` from the Firebase Auth `uid` claim).
3. **Bilingual code/UI split.** All user-facing text (labels, buttons, placeholders, errors, toasts, empty states) is in **French**. All code (variable names, function names, comments, docstrings) is in **English**.
4. **Currency in integer cents.** `15000` means $150.00. Never use floats for money. Use `Decimal` only for tax computation intermediates, convert to int cents (with `ROUND_HALF_UP`) before storage.
5. **Timestamps UTC.** Stored as UTC `datetime` with timezone info. Displayed in `America/Montreal` via the `to_mtl` Jinja filter (registered from `tz.py`).
6. **UUIDv4 document IDs.** Generated server-side. Never reuse IDs. **Documented exception (Phase I):** the OAuth collections use the lookup key as the doc ID — `oauth_clients/{client_id}`, `oauth_codes/{sha256(code)}`, `oauth_tokens/{sha256(token)}` — so raw credentials are never stored and validation is one keyed `get()`.
7. **Every Firestore doc has `created_at`, `updated_at`, `etag`** (etag = UUIDv4 regenerated on every write, used for DAV `If-Match` conditional requests). Folders and the three OAuth collections are exceptions: they have `created_at`/`updated_at` but no `etag`.
8. **HTMX first.** Dynamic interactions use HTMX. Flask endpoints check `request.headers.get("HX-Request")`/`HX-Target` and return HTML fragments for HTMX requests, full pages otherwise.
9. **Mobile-first.** Design for 375px viewport first. Breakpoints at 768px (tablet) and 1024px+ (desktop). Touch targets minimum 44px.
10. **Minimalist visual language.** Near-white `#FAFAFA` backgrounds, near-black `gray-900` text, `indigo-600` accent. System font stack. Generous white-space.
11. **DAV-ready schemas.** Parties, hearings, tasks, notes carry stable DAV UIDs (`vcard_uid`, `vevent_uid`, `vtodo_uid`, `vjournal_uid`) set at creation and never changed.

---

## Security Rules

- **Security headers on every response** (via `security.py` `_add_security_headers` `after_request` hook):
  - `Content-Security-Policy` — **enforced** since 2026-07-11 (see `build_csp`/`csp_nonce` in `security.py`; flipped after verifying against 90 days of report-only `/csp-report` data — only `script-src` ever reported violations, then hardened the same day). The policy is **assembled per request** so a fresh **nonce** can be spliced into `script-src`: `'self' 'nonce-<per-request>' 'unsafe-eval'` + the Google origins the App Check SDK loads reCAPTCHA Enterprise from (`gstatic.com`, `apis.google.com`, `google.com`) — **no `'unsafe-inline'`, no `ajax.cloudflare.com`, no script CDN origins** (assets are vendored). The app's own inline `<script>` blocks carry `nonce="{{ csp_nonce }}"` (the `csp_nonce` Jinja global = the header value), so an injected/un-nonced inline script is blocked; inline `on*` handlers were refactored to `data-` attributes wired via `addEventListener` (a nonce cannot authorize a handler attribute). `'unsafe-eval'` is retained for Alpine's `new Function()` (dropping it needs `@alpinejs/csp` + an expression rewrite); `style-src` keeps `'unsafe-inline'` for reCAPTCHA's dynamic inline styles. **Rocket Loader is disabled at the edge (since 2026-07-11).** Violations are posted to `/csp-report` (`report-uri`, still active under enforcement) and logged as `csp_violation` security events.
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload` (2 years)
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()`
  - `Cache-Control: no-store, no-cache, must-revalidate, private`
  - `Pragma: no-cache`
- **CSRF** on every POST/PUT/DELETE via `flask-wtf` `CSRFProtect`. HTMX requests include the token via `hx-headers` on `<body>`. Failures are logged as `csrf_failure` security events and return 400.
- **Rate limiting** on `/auth/login` (configurable via `RATE_LIMIT_LOGIN`, default `5 per minute`) via `flask-limiter` (in-memory store). The rate-limit key is `CF-Connecting-IP` (real client IP behind Cloudflare; falls back to the peer address) — only trustworthy because the firewall guarantees traffic transits Cloudflare.
- **Request size limits:** 25 MB global cap (`MAX_CONTENT_LENGTH` in `config.py`); routes other than `/documents/upload` are capped at 1 MB and DAV/well-known paths at 5 MB by `_enforce_request_size` in `security.py`. **Phase H exemption:** template upload/replace (`POST /gabarits/` and `POST /gabarits/<id>`) get 10 MB (`_is_template_upload_path`); the generation POST (`/gabarits/generer`) and every other gabarit sub-route stay at 1 MB.
- **Secrets live in Google Cloud Secret Manager**, not in `app.yaml`: `flask-secret-key`, `firebase-api-key`, `dav-password-hash`, `cf-origin-secret`. `config.py` resolves them at startup when `ENV=production`; locally they come from `.env` env vars (`SECRET_KEY`, `FIREBASE_API_KEY`, `DAV_PASSWORD_HASH`, `CF_ORIGIN_SECRET`).
- **Firebase Storage URLs:** always signed, 15-minute expiry. Never expose raw bucket URLs to the client. The signing path uses `iam.signBlob` via `google-auth` impersonation when running on App Engine.
- **DAV authentication:** HTTP Basic Auth with bcrypt-hashed password (`DAV_PASSWORD_HASH`, from Secret Manager in prod). Username is the same as `AUTHORIZED_USER_EMAIL`. **Separate** from Firebase Auth.
- **MCP authentication (Phase I):** `POST /mcp` requires an OAuth 2.1 **opaque bearer token** (32 bytes `secrets.token_urlsafe`, stored as SHA-256 hex doc IDs in `oauth_tokens` — no JWTs, no new crypto deps). Access tokens live 60 min, refresh tokens 30 days with **rotation** (a replayed rotated refresh token revokes its whole family; a replayed authorization code does too). The embedded AS (`mcp/oauth.py`) offers **open-but-neutered DCR**: `/oauth/register` accepts only Claude's callback URLs (`https://claude.ai|claude.com/api/mcp/auth_callback`; localhost additionally outside production), and the consent screen sits behind `@login_required` (session + MFA), so no third party can complete a flow. PKCE S256 only; public clients only; `hmac.compare_digest` for PKCE and cache comparisons. Bearer failures feed a **per-IP brake** (20 invalid tokens / 15 min → 429 before Firestore is touched) mirroring the DAV brake, with a 5-min HMAC-keyed success cache (revocation lag ≤ 5 min on a warm instance). CSRF exemptions: `/mcp`, `/oauth/register`, `/oauth/token`, `/oauth/revoke` — **not** the `/oauth/authorize` POST. Rate limits: register 10/h, token + revoke 60/h, `/mcp` 240/min (all keyed by `CF-Connecting-IP`). An `Origin` header on `/mcp` must be `claude.ai`/`claude.com`/the canonical origin (DNS-rebinding defense). Never log tokens, codes, or verifiers. Break-glass: `MCP_ENABLED=false` (kill switch) or `python -m scripts.revoke_mcp_tokens`.
- **Edge defense in depth — all traffic must transit Cloudflare.** Three layers:
  1. **App Engine firewall** allows only Cloudflare's published IP ranges (ops-side; configured in GCP).
  2. **Origin secret** (`_enforce_origin_secret` in `security.py`): when `CF_ORIGIN_SECRET` is set, every request must carry the matching `X-Origin-Auth` header, injected at the edge by a Cloudflare Transform Rule — defeats direct-to-App-Engine access with a spoofed Host. Unset = disabled (local dev).
  3. **Host check** (`block_appspot` in `main.py`): rejects `*.appspot.com` hosts (403). Weakest layer (Host is spoofable) but free.
  App Engine internal paths (`/_ah/` — warmup, cron) never transit Cloudflare and are **exempt from layers 2 and 3**.
- **Cloudflare Access** (Zero Trust) fronts `/dav/*` with a service-token policy for DavX5 and a Google SSO policy for interactive use.
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
│   ├── pagination.py               # Pagination helpers: legacy page mode + cursor mode (encode/decode/trail)
│   ├── manifest.json               # PWA manifest
│   ├── robots.txt
│   ├── OBSERVABILITY.md            # Structured-logging event registry + tracing conventions (source of truth)
│   ├── .gcloudignore               # Keeps tests/venv/dev/non-runtime files out of the deployed bundle
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
│   │   └── trust.py                # /fideicommis/*  (Phase K: journal, carte, comptes, conciliations, exports)
│   │
│   ├── dav/                        # DAV protocol endpoints
│   │   ├── __init__.py             # Principal + calendar/addressbook home-set; root PROPFIND lists collections dynamically
│   │   ├── carddav.py              # /dav/addressbook/ — contacts
│   │   ├── caldav.py               # /dav/calendar/ — hearings (VEVENT)
│   │   ├── rfc5545.py              # /dav/tasks/ — standalone tasks (VTODO) only
│   │   ├── dossier_collections.py  # /dav/dossier-{id}/ — per-dossier VTODO + VJOURNAL
│   │   ├── dav_auth.py             # HTTP Basic Auth decorator
│   │   ├── xml_utils.py            # Namespace tags, multistatus builders, propfind body parser
│   │   └── sync.py                 # CTag / sync-token / tombstone management
│   │
│   ├── mcp/                        # MCP connector (Phase I) — read-only, zero new deps
│   │   ├── __init__.py             # mcp_bp + oauth_bp blueprints, register_mcp(app), constants,
│   │   │                           # MCP_ENABLED kill switch (404s every route when off)
│   │   ├── jsonrpc.py              # JSON-RPC 2.0 parsing, response/error envelopes, error codes
│   │   ├── endpoint.py             # POST /mcp dispatcher: initialize/ping/tools list+call
│   │   ├── bearer.py               # @mcp_auth_required, WWW-Authenticate challenges,
│   │   │                           # per-IP invalid-token brake (mirrors dav_auth brake)
│   │   ├── oauth.py                # RFC 8414/9728 metadata, /oauth/register|authorize|token|revoke
│   │   ├── store.py                # Firestore persistence: oauth_clients / oauth_codes / oauth_tokens
│   │   ├── tools.py                # TOOLS registry, subset JSON-Schema validator, money/date helpers
│   │   └── handlers.py             # 17 read-only tool implementations calling models/* + utils/*
│   │
│   ├── utils/                      # Utility modules
│   │   ├── __init__.py
│   │   ├── deadlines.py            # Quebec art. 83 C.p.c. judicial deadline calc
│   │   ├── recours.py              # Recours & prescription (pure): delay-period table
│   │   │                           # (amount, unit — jours/mois/ans), value-class table,
│   │   │                           # compute_class + compute_date_pour_agir + the
│   │   │                           # type-aware compute_echeances orchestration
│   │   │                           # (AVIS_PERIODS, PA_PERIODS, Echeance)
│   │   ├── taxonomie.py            # Taxonomie des actions (pure, GENERATED): 20 domaines →
│   │   │                           # 162 actions with délai / delai_types (11 jetons) /
│   │   │                           # a_valider / avis structurés / ref_delai + ref_fondement;
│   │   │                           # tooltip_payload; suggests a period, never sets one
│   │   ├── docx_fill.py            # Phase H/H.2: stdlib-only .docx fill engine (zip XML substitution;
│   │   │                           # scalars, blocks, + H.2 repeating rows & conditional regions)
│   │   ├── template_fields.py      # Phase H: field catalog, flat aliases, classification, resolution
│   │   ├── invoice_docx.py         # Phase H.2: invoice → note-d'honoraires context (facture.* + rows + conditions)
│   │   ├── format_fr.py            # Phase H.2: fr-CA currency/date/hours/rate formatting (centralized)
│   │   ├── validators.py           # Phone (E.164), email, postal code normalization, address defaults
│   │   ├── export_csv.py           # CSV export helper (UTF-8 BOM)
│   │   ├── export_pdf.py           # reportlab-based PDF export
│   │   ├── logging_setup.py        # Cloud Logging handler, ContextFilter, RedactionFilter, typed log helpers
│   │   └── tracing_setup.py        # OpenTelemetry → Cloud Trace, PII-sanitizing exporter, span()/@traced
│   │
│   ├── scripts/                    # One-time / manual scripts (run with python -m scripts.X)
│   │   ├── __init__.py
│   │   ├── seed_reference_data.py  # Populate ref_greffes + ref_juridictions (Phase G)
│   │   ├── mint_dev_token.py       # Local-dev MCP bearer minting (refuses ENV=production)
│   │   ├── revoke_mcp_tokens.py    # Break-glass: revoke all MCP tokens (+ optional client purge)
│   │   ├── diagnose_gabarit.py     # Local: list a gabarit's placeholders/classification + fragmentation cause
│   │   └── verify_trust_integrity.py  # Phase K: recompute + cross-check the trust register (read-only)
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
│   │   ├── test_mcp_tools.py
│   │   ├── test_docx_fill.py
│   │   ├── test_template_fields.py
│   │   ├── test_format_fr.py       # Phase H.2
│   │   ├── test_invoice_docx.py    # Phase H.2 (incl. end-to-end note fill)
│   │   ├── test_reference_addresses.py  # Court-location table + greffe→address wiring
│   │   ├── test_reference_forums.py # Non-judicial forum table (admin tribunals + federal courts)
│   │   ├── test_taxonomie.py       # Action taxonomy invariants (incl. the §4 déchéance cross-check)
│   │   ├── test_dossier_taxonomy.py # matter_type/objet → domaine/action migration + validation
│   │   ├── test_dossier_forum.py   # forum_type/forum validation + normalize_forum (CI-only)
│   │   ├── test_folders.py         # Phase H.2 get_or_create_folder (CI-only: imports models)
│   │   ├── test_document_naming.py # Phase H.2 projet_document_name (CI-only: imports models)
│   │   └── test_trust.py           # Phase K: balance arithmetic, control, reversal, clearing, reconciliation, exports
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
│   │   ├── dossiers/               # list, detail, form + _dossier_rows + _tab_temps,
│   │   │                           # _tab_facturation, _tab_agenda, _tab_protocole,
│   │   │                           # _tab_documents, _tab_placeholder
│   │   ├── time_expenses/          # list, time_form, expense_form + _time_rows, _expense_rows
│   │   ├── invoices/               # list, detail, create + _invoice_rows + _unbilled_items
│   │   ├── hearings/               # list, detail, form + _hearing_rows + _month_grid
│   │   ├── tasks/                  # list, detail, form + _task_row, _task_rows
│   │   ├── notes/                  # list, detail, form + _note_rows
│   │   ├── protocols/              # list, detail, form + _protocol_rows
│   │   ├── documents/              # list, detail, upload, edit + _browser, _document_rows, _folder_tree
│   │   ├── gabarits/               # list, detail, form + _template_rows, _generate_modal, _generate_fields
│   │   ├── trust/                   # Phase K: list (journal), _transaction_rows, detail, form, card,
│   │   │                            # transfer_form, reverse_confirm, client_consolidated, accounts_list,
│   │   │                            # account_form/detail, reconciliations_list, reconciliation_form/worksheet
│   │   └── mcp/                    # consent.html (OAuth consent screen, French)
│   │
│   └── static/
│       ├── sw.js                   # Service worker (precache + stale-while-revalidate for vendor assets)
│       ├── src/app.input.css       # Tailwind input (source of the compiled artifact below)
│       ├── vendor/                 # Vendored, version-named, immutable-cached assets:
│       │                           # app.<hash>.css (compiled Tailwind), htmx-2.0.4.min.js,
│       │                           # alpinejs-3.15.12.min.js, firebase-{app,auth,app-check}-compat-10.12.2.js,
│       │                           # appcheck-boot.<hash>.js (App Check bootstrap, was inline)
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

> Note on tab names: the dossier detail uses an HTMX tab loader (`/dossiers/<id>/tab/<tab_name>`). Active tab names are `temps`, `facturation`, `agenda`, `protocole`, `documents`, `fideicommis` (Phase K); **`temps` is the default tab**. Tab keys ≠ labels since July 2026: `temps` is labelled « Temps & Déboursés » and `facturation` « Honoraires » (the keys were kept so bookmarks survive). The `documents` tab lost its counters/indicators in July 2026 (summary stat grid and the per-folder « X éléments » line — dropping the latter also removed a per-folder `_count_items` N+1 and the unused `get_document_summary`/`get_notes_summary` calls from the tab loader). The `audiences` and `taches` tabs were **merged into `agenda` in July 2026** (`_tab_agenda.html`: two sections, Audiences then Tâches, mirroring the temps tab) — the per-tab summary counters were dropped, and the tab is **forward-looking**: items dated strictly before today (Montréal calendar day; today's items stay) are filtered out route-side in Python (no new Firestore index), and a dateless task shows only while active (`à_faire`/`en_cours`). `_LEGACY_TABS` in `routes/dossiers.py` maps the old `audiences`/`taches` names onto `agenda` for pre-merge bookmarks and `return_to` links. The `apercu` (Aperçu) tab was **removed in July 2026** — its prescription block became the « Recours et prescription » card (itself **split in July 2026** into « Recours » — domaine, action (libellé + greyed `(CODE)` from `action_obj`), précision, valeur/classe — and « Prescription » — « Délai (Type) » (the confirmed délai with the taxonomy's nature du délai bracketed after it, amber when a déchéance), droit d'action, date pour agir), its dates became the « Mandat » card (renamed from « Dates clés » in July 2026 — it shows type de mandat, « Honoraires (taux) » (`format_honoraires_parts`: type label + greyed rate in parentheses; gabarits keep `format_honoraires`'s joined « label — taux »), ouverture, fermeture, and a derived « Rétention » = fermeture + 7 ans computed read-only in `dossiers.dossier_detail`; the fermeture/rétention rows are hidden until a closing date is set; « Type de dossier » left it in July 2026 when it became « Domaine » on the Recours card), and its free-text notes were deleted with the fields (below). A « Sommaire » card (free-text `sommaire` field, entered on the create/edit form) sits between the header card and the info-card grid. There is no separate `notes` tab in the dossier hub; notes live at the standalone `/notes` view (filterable by `?dossier_id=`).

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

A dossier holds multiple clients and multiple opposing parties as **arrays of `{id, name}` objects**. Flat ID arrays (`client_ids`, `opposing_party_ids`) are kept in sync for `array_contains` queries (used by `count_dossiers_for_partie` / `list_dossiers_for_partie`). A migration helper (`_migrate_parties`) upgrades older single-client docs on read.

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

    # Parties on the dossier (replaces the legacy single client_id)
    "clients":          [{"id": UUIDv4, "name": str}, ...],
    "client_ids":       [UUIDv4, ...],    # mirrors clients[].id (for array_contains)
    "opposing_parties": [{"id": UUIDv4, "name": str}, ...],
    "opposing_party_ids": [UUIDv4, ...],

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
    # mandate_type ("type de mandat") — nature of the engagement (new July
    # 2026). Absent on legacy dossiers → the UI shows "—" until set on edit.
    # "mediation_arbitrage" was RETIRED July 2026 → migrated to "autre" on read
    # (models.dossier._migrate_mandate_type), mirroring _migrate_domaine.
    "mandate_type": "judiciaire" | "transactionnel" | "consultation" | "autre",
    "role": "demandeur" | "défendeur" | "intervenant" | "mis en cause" | "autre",

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
    "hourly_rate": int,                   # cents (default 25000 = $250/h)
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
    "amount": int,                        # cents
    "taxable": bool,
    "receipt_document_id": UUIDv4 | None, # FK → documents (optional)
    "invoiced": bool, "invoice_id": UUIDv4 | None,
}
```

### `invoices/{invoiceId}` — Invoices

```python
{
    "invoice_number": str,                # "{file_number}-NN" — per-file sequence
                                          # (2-digit padded, rolls to 3+ past 99;
                                          # e.g. "2025-001-03"). Legacy invoices
                                          # keep their "YYYY-F###" numbers.
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
    "hearing_type": "audience" | "conférence_de_gestion"
                  | "conférence_de_règlement" | "interrogatoire"
                  | "médiation" | "procès" | "appel" | "autre",
    "start_datetime": datetime, "end_datetime": datetime,
    "all_day": bool,
    "location": str, "court": str, "judge": str,
    "notes": str,
    "reminder_minutes": int,              # 15|30|60|120|1440|2880|10080, default 1440 (24h)
    "status": "confirmée" | "à_confirmer" | "reportée" | "annulée" | "terminée",

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
    "file_size": int,                     # bytes (max 25 MB)
    "storage_path": "users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}",
    "category": "procédure" | "pièce" | "correspondance" | "preuve"
              | "jugement" | "entente" | "note" | "autre",
    "description": str,
    "tags": list[str],
    "version": int, "parent_document_id": UUIDv4 | None,
}
```

Allowed MIME types: PDF, MS Word (doc/docx), JPEG, PNG, TIFF.

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

### `doc_templates/{templateId}` — Document templates ("gabarits", Phase H)

Top-level collection; standard common fields (`id`, `created_at`, `updated_at`, `etag`). Not DAV-exposed — no DAV UID, no CTag bumping. Template files live in **Storage** at `users/{userId}/templates/{templateId}/{filename}` (signed URLs, 15-min expiry) and are **not** `documents` records; generated outputs saved into a dossier ARE regular `documents` records (independent copies — deleting a gabarit never touches them). No composite index (small collection: single `order_by("name")`, category/search filtered client-side).

```python
{
    "name": str,                       # ≤120 chars, required
    "description": str,
    "category": "procédure" | "correspondance" | "autre",
    "kind": "gabarit" | "note_honoraires",  # Phase H.2 discriminator (default
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
| `/` | GET | Landing dashboard: hearings (next 7 days + 7-60 days), urgent tasks (≤14 days or overdue), urgent protocol steps, prescription alerts (within 60 days, with judicially-adjusted last-action date), quick stats (open dossiers, unbilled hours/amount, outstanding invoices) |

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
| `/dossiers/<id>` | GET | Detail (hub page) |
| `/dossiers/<id>/tab/<tab_name>` | GET | HTMX tab loader (`temps` — default, `facturation`, `agenda`, `protocole`, `documents`, `fideicommis`; legacy `audiences`/`taches` map to `agenda`) |
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
| `/temps/<entry_id>/delete` | POST | Delete |
| `/temps/depenses/new` | GET | Expense form |
| `/temps/depenses` | POST | Expense create |
| `/temps/depenses/<expense_id>/edit` | GET | Edit |
| `/temps/depenses/<expense_id>` | POST | Update |
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
| `/factures/<id>` | GET | Detail view (also serves as print-ready view via `@media print` in CSS) |
| `/factures/<id>/status` | POST | Transition status (envoyée/payée/annulée…) |
| `/factures/<id>/void` | POST | Annul and release linked time entries/expenses |
| `/factures/<id>/delete` | POST | Hard-delete a cancelled invoice |
| `/factures/<id>/note-docx` | POST | **Phase H.2** — generate the Word note d'honoraires from this invoice via the `kind="note_honoraires"` gabarit; save the `.docx` into the dossier's « **Projets** » folder (`GENERATED_FOLDER_NAME`) under the name `"{file_number} - YYYY-MM-DD - Projet {template} {invoice_number}"` (`projet_document_name`); HTMX success partial (`_note_generated.html`). Refuses `annulée`; French message if no note template exists. The reportlab PDF path is unchanged (they coexist) |
| `/factures/export/{csv,pdf}` | GET | Export |

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
| `/audiences/<id>/delete` | POST | Delete |
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
| `/documents/upload` | GET / POST | Upload form / submit (form fields: `dossier_id`, `folder_id`, …) |
| `/documents/<id>/edit` | GET / POST | Edit metadata |
| `/documents/<id>/move` | POST | Move to a folder (Firestore-only, Storage path unchanged) |
| `/documents/move-bulk` | POST | Batch move |
| `/documents/<id>/delete` | POST | Delete (Storage + Firestore) |
| `/documents/folders/create` | POST | Create folder |
| `/documents/folders/<fid>/rename` | POST | Rename |
| `/documents/folders/<fid>/move` | POST | Change parent (with circular-ref check) |
| `/documents/folders/<fid>/delete` | POST | Delete (recursive flag) |
| `/documents/folder-tree` | GET | HTMX folder tree (for move modal) |

### DAV endpoints (`/dav/*`)

| Endpoint | Purpose |
|----------|---------|
| `/.well-known/carddav` | 301 → `/dav/` |
| `/.well-known/caldav` | 301 → `/dav/` |
| `/dav/` | Root: `OPTIONS` + `PROPFIND`. Advertises `addressbook-home-set` and `calendar-home-set`. Depth:1 lists all collections dynamically (addressbook, calendar, tasks, and `/dav/dossier-{id}/` for each `actif`/`en_attente` dossier) |
| `/dav/addressbook/` + `/{id}.vcf` | CardDAV — contacts |
| `/dav/calendar/` + `/{id}.ics` | CalDAV VEVENT — hearings |
| `/dav/tasks/` + `/{id}.ics` | CalDAV VTODO — **standalone tasks only** (`dossier_id is None`) |
| `/dav/dossier-{did}/` + `/{id}.ics` | **Per-dossier CalDAV collection** (Phase D1+D2): VTODO tasks linked to this dossier + VJOURNAL notes of this dossier |

All DAV endpoints support: `OPTIONS`, `PROPFIND` (Depth 0/1), `REPORT` (sync-collection / addressbook-multiget / calendar-multiget), `GET`, `PUT` (with `If-Match` / `If-None-Match`), `DELETE` (with `If-Match`).

### MCP connector endpoints (Phase I)

All served through Cloudflare like every other route; **must not** be behind the Cloudflare Access application that fronts `/dav/*`. The `MCP_ENABLED` kill switch 404s every row of this table.

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

The 17 read-only tools (`get_agenda`, `list_dossiers`, `get_dossier`, `list_tasks`, `list_hearings`, `list_notes`, `get_note`, `list_documents`, `list_parties`, `get_partie`, `get_billing_snapshot`, `list_protocol_steps`, `compute_judicial_deadline`, `parse_court_file_number`, plus the Phase-K trust tools `get_trust_balance`, `list_trust_transactions`, `get_trust_snapshot`) live in `mcp/handlers.py` with schemas in `mcp/tools.py`. **Trust tools never emit the bank transit or account number** (`list_trust_transactions` emits neither transit/last4/institution; `get_trust_snapshot` may emit the account name + institution but never the transit or last4). Conventions: money as `*_cents` + fr-CA `*_display`; date-only fields as `YYYY-MM-DD` (UTC calendar date); true timestamps as ISO-8601 America/Montreal; every list tool capped at 50 items with a `truncated` flag; no signed URLs or storage paths ever in tool output.

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
| `/fideicommis/nouvelle` · `/` | GET · POST | Entry form · create (refuses `purpose="correction"`) |
| `/fideicommis/<id>` | GET | Detail: all fields, both frozen balances, links to reversal pair / other transfer leg / invoice; inline compenser + contrepasser actions |
| `/fideicommis/<id>/compenser` · `/compenser-lot` | POST | Single / bulk clear (all-or-nothing) |
| `/fideicommis/<id>/contrepasser` | GET · POST | Reversal confirmation (mandatory motif) · submit |
| `/fideicommis/virement` | GET · POST | Inter-dossier transfer (two `compensée` legs, one account) |
| `/fideicommis/carte/<dossier_id>/<client_id>` | GET | **Carte-client** — chronological, « Solde aux livres » vs « Disponible (compensé) » |
| `/fideicommis/client/<client_id>` | GET | Consolidated « Vue de gestion » across dossiers — **not a register**, no control, no export |
| `/fideicommis/comptes/` · `/nouveau` · `/<id>` · `/<id>/edit` | GET/POST | Account list / create / detail / edit (metadata only — balances never editable) |
| `/fideicommis/conciliations/` · `/nouvelle` · `/<id>` · `/<id>/completer` | GET/POST | Reconciliation list / start / worksheet (live variance) / complete (refuses variance ≠ 0) |
| `/fideicommis/export/<csv\|pdf>` · `/carte/<did>/<cid>/export/<csv\|pdf>` | GET | Journal / card export — 9 columns, « Recette »/« Crédit » split, `en_circulation` marked `*` |
| `/fideicommis/{dossier,client,counterparty}-search` | GET | HTMX autocompletes (client-search scoped to one dossier; counterparty suggests parties as **text**) |

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
  server-side instead — tasks (per-status groups, 100 each), notes (pinned + 100 recent),
  hearings (two 100-doc windows around now + month-grid range). Cap hits log a warning.
- Validate filter values against the model's `VALID_*` vocabulary in routes before
  choosing a path, so junk query strings cannot force an unbounded fallback scan.

### `models/partie.py`
- `display_name(partie) -> str` — returns `organization_name` (legal name) for personnes morales; trade name (`trade_name`) is surfaced separately in the UI
- `update_kyc_status(partie_id, field, status, notes)` — `field ∈ {"identity_verified", "conflict_check"}`, auto-stamps the corresponding `_date`
- `link_kyc_document(partie_id, document_id)` — appends to `kyc_document_ids`
- `MANDATAIRE_KIND_LABELS` — French display labels for the `kind` field on each mandataires entry (mandataire, tuteur, curateur, représentant_légal, autre)
- `_migrate_mandataires(partie)` — translates legacy single-mandataire fields into the new `mandataires` list on read; pops the legacy keys so the next `set()` purges them from storage. Called from `get_partie`.
- `mandataires` constraint enforcement in `_validate`: each entry's `id` must reference an existing partie that is `type=="individual"`, shares the parent's `contact_role`, and is not the parent itself. `_normalize` deduplicates by id, drops empty entries, and removes any legacy `mandataire_id`/`mandataire_kind`/`mandataire_notes` keys before save.
- `delete_partie` enforces the FK safety check (fails CLOSED): refuses while the partie is referenced by any dossier (`count_dossiers_for_partie_strict`) or listed as a mandataire by another partie — applies to UI and CardDAV DELETE alike
- `partie_to_vcard(partie) -> str` — vCard 4.0 with LANG, GENDER, X-PRONOUN, TITLE, ROLE, ORG, dual ADR/TEL/EMAIL, CATEGORIES (contact_role), NOTE, UID, REV. (Mandataires list, trade name, and governing law are not yet serialized to vCard.)
- `vcard_to_partie(vcard_str) -> dict` — inverse parser; normalizes incoming phones via `normalize_phone`

### `models/dossier.py`
- `suggest_file_number() -> str` — public wrapper around `_suggest_next_file_number`, "YYYY-NNN" sequential
- `count_open() -> int` — COUNT aggregation over `status in (actif, en_attente)` (dashboard stat)
- `list_prescription_alerts(cutoff, limit=50) -> list[dict]` — server-side `status==actif AND prescription_date<=cutoff`, ordered + bounded; logs a warning when the window fills (needs the `dossiers` composite index)
- `count_dossiers_for_partie(partie_id) -> int` (returns 0 on query failure — display only), `count_dossiers_for_partie_strict(partie_id) -> int` (propagates errors — used by FK safety checks), `list_dossiers_for_partie(partie_id) -> list[dict]` — query both `client_ids` and `opposing_party_ids`
- `delete_dossier` REFUSES deletion while child records exist (documents, time entries, expenses, invoices, hearings, tasks, notes, protocols, folders, **trust transactions**) and fails CLOSED when the child check errors — archive instead of deleting. **A dossier that has EVER had a trust entry can never be deleted** even at a zero balance (the register is permanent; `trust_transactions` rows are never hard-deleted, so the `count>0` check enforces "ever existed")
- `dossier_to_vjournal(dossier) -> str`, `vjournal_to_dossier(ical_str) -> dict` — legacy, retained for potential export (not used by DAV post-D1). CATEGORIES now emits the domaine label + the action label; an unknown key resolves to nothing rather than leaking a raw snake_case key as a French category (the old `matter_type` line did).
- **Forum (July 2026, four-way since late July):** `VALID_FORUM_TYPES = ("judiciaire", "administratif", "federal", "prejudiciaire")` + `FORUM_TYPE_LABELS` (« Tribunal de droit commun » / « Tribunal administratif » / « Cour ou tribunal fédéral » / « Préjudiciaire »). `normalize_forum(data)` reconciles the forum fields server-side (authoritative over the JS state), called by the route in `_form_data` before validation. `administratif`/`federal` → the picked `forum` slug's name becomes `tribunal`, the Québec judicial-court fields (greffe/juridiction/district/palais/competence) are cleared, and `is_administrative_tribunal` is True only for `administratif`; a blank/unknown/**cross-category** slug leaves the data untouched (`_validate` rejects it). `prejudiciaire` → everything judicial is cleared EXCEPT the user-entered `district_judiciaire`, and `court_file_number` is forced to `PREJUDICIAIRE_FILE_NUMBER` (« Préjudiciaire ») so gabarits can cite it until the parser crushes it. `judiciaire` → `forum` cleared, parsed metadata stands. `_validate` presence-gates `forum_type` (legacy dossiers default `"judiciaire"` on read); the retired `"autre"` is no longer writable — `_migrate_forum_type` (in the `_migrate_parties` chokepoint) splits stored `"autre"` docs by their slug's category on read (dangling slug → `judiciaire`, forum cleared, tribunal text kept). It lives in the model, not the route, so it is testable without Flask config.
- **Taxonomy (July 2026):** `VALID_DOMAINES` / `VALID_ACTIONS` / `DOMAINE_LABELS` are re-exported from `utils/taxonomie.py` — the vocabulary is **not** redefined here (contrast `MANDATE_TYPE_LABELS` / `FEE_TYPE_LABELS`, which `utils/template_fields.py` must mirror by hand; there is deliberately **no domaine mirror**, since `taxonomie` is Firestore-free and both sides import it). `_migrate_domaine` (called from `_migrate_parties`, the chokepoint covering all six read paths) folds legacy `matter_type` → `domaine` and `objet` → `action_precision`, then `_REMOVED_FIELDS` purges both on the next save. `_validate` presence-gates `domaine`/`action` (like `mandate_type`) and rejects a pair whose code prefix disagrees with the domaine — the cascading picker cannot produce that, but a hand-crafted POST can.

### `models/time_entry.py` (note: file is `time_entry.py`, **not** `timeentry.py`)
- `get_time_summary(dossier_id) -> dict`
- `get_unbilled_time_entries(dossier_id) -> list[dict]`
- `get_unbilled_totals() -> {"hours": float, "amount": int}` — single server-side aggregation (SUM×2 over `billable==True AND invoiced==False`); used by the dashboard instead of streaming the collection. Needs the `timeentries` composite indexes in `firestore.indexes.json`.
- `mark_time_entries_invoiced(entry_ids, invoice_id) -> list[str]` — returns the IDs that failed to flip (no silent swallowing); no longer called by `create_invoice` (flips happen inside its transaction)

### `models/expense.py`
- `get_expense_summary(dossier_id) -> dict`
- `get_unbilled_expenses(dossier_id) -> list[dict]`
- `mark_expenses_invoiced(expense_ids, invoice_id) -> list[str]` — returns failed IDs, mirroring `mark_time_entries_invoiced`

### `models/invoice.py`
- `compute_totals(line_items: list[dict]) -> dict` — pure helper computing subtotal_fees / subtotal_expenses / subtotal / GST (5%) / QST (9.975%, **not compounded on GST**) / total. Uses `Decimal` with `ROUND_HALF_UP`.
- `create_invoice(dossier_id, time_entry_ids, expense_ids, data)` — fully transactional: sources that are missing, already invoiced, or belonging to another dossier are skipped; the invoice doc, line items, and `invoiced=True` flips for ONLY the retained sources commit in a single Firestore transaction that re-reads each source (etag compared) and aborts on concurrent change. `retainer_applied` is validated to `[0, total]`.
- `get_invoice_with_items(invoice_id) -> tuple[Optional[dict], list[dict]]`
- `update_status(invoice_id, new_status)` — enforces allowed transitions (`STATUS_TRANSITIONS`)
- `void_invoice(invoice_id)` — all-or-nothing: source releases + status flip to `annulée` commit in one `db.batch()`; any failure aborts without changing status
- `delete_invoice(invoice_id)` — only allowed on `annulée`; refuses if any time entry/expense still references the invoice
- `get_invoice_summary(dossier_id) -> dict`
- `get_outstanding_total() -> int` — SUM(`amount_due`) aggregation over `status in (envoyée, en_retard)` (dashboard stat; needs the `invoices` composite index)
- Invoice numbers are **per-file**: `"{file_number}-NN"` (2-digit-padded sequence within the dossier; e.g. `2025-001-03`). Allocated by `_generate_invoice_number(dossier_id)` from a **per-dossier** transactional counter `counters/invoice-{dossier_id}` (`seq`), seeded on first use by `_seed_invoice_seq` = max(count of the file's existing invoices, highest existing per-file suffix) — so the sequence counts legacy `YYYY-F###` invoices too and a deleted new-scheme number can never be reused (monotonic; the counter never decrements). A dossier with **no file number** falls back to the legacy year-sequential `YYYY-F###` (`_generate_year_invoice_number` + `counters/invoices-{year}`). **Existing invoices keep whatever number they were issued** — never renumbered (immutable accounting artifact). Allocation failure aborts invoice creation — no guessed fallback number.

### `models/hearing.py`
- `get_hearing_summary(dossier_id) -> dict`, `get_upcoming_hearings(days=30) -> list[dict]`
- `list_hearings_in_range(date_from, date_to, limit=100) -> list[dict]` — server-side range + order on `start_datetime` (single-field index); the dashboard's bounded hearing windows
- `hearing_to_vevent(hearing) -> str` — VEVENT with VALARM (TRIGGER -PT{reminder_minutes}M)
- `vevent_to_hearing(ical_str) -> dict`
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
- `get_notes_summary(dossier_id) -> dict`
- `note_to_vjournal(note) -> str` — VJOURNAL with SUMMARY (title), DESCRIPTION (content), CATEGORIES (category), X-ATHENA-PINNED if pinned
- `vjournal_to_note(ical_str) -> dict`

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
- `list_urgent_steps(cutoff, limit=50) -> list[dict]` — replaces the dashboard N+1: ONE `collection_group("steps")` query (status in active set + deadline ≤ cutoff, 3× over-fetch) + ONE batched `get_all` of distinct parent protocols; only steps of `actif` protocols survive, enriched with `_protocol_title`/`_protocol_id`/`_dossier_file_number`. Needs the `steps` COLLECTION_GROUP index.
- `_sync_task_status(task_id, step_status)` — uses `_SYNCING` guard (separate set from `task.py`)
- `_auto_create_tasks_for_steps(protocol)` — creates a task per step and links it via `linked_task_id`
- `_check_protocol_completion(protocol_id)` — auto-transitions to `complété` when all mandatory steps are done

### `models/document.py` + `models/folder.py`

Document functions:
- `upload_document(dossier_id, dossier_file_number, file_stream, filename, file_size, metadata, user_id)` — uploads to `users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}`, creates Firestore doc with `folder_id`. Validation is magic-byte based (`_sniff_content_type`): the sniffed type must be in `ALLOWED_MIME_TYPES` and agree with the extension; the sniffed type (never `mimetypes.guess_type`) sets the stored `file_type` and GCS Content-Type. The storage-path filename segment goes through `secure_filename` (raw name kept only in `original_filename`/`display_name`).
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
- `rename_folder`, `move_folder` (circular-ref prevention via parent chain walk + subtree depth check), `delete_folder(recursive=False)`
- `get_folder_breadcrumb(dossier_id, folder_id) -> list[{id, name}]`
- `get_folder_tree(dossier_id) -> nested list` — for move modal
- `get_or_create_folder(dossier_id, name, parent_folder_id=None) -> Optional[dict]` — **Phase H.2**: returns the existing root-level folder of that name (case-insensitive) or creates it; idempotent so repeated generations reuse the one « Projets » folder (`document.GENERATED_FOLDER_NAME`) instead of tripping the duplicate-name check. **All** generated documents (gabarit letters/procedures AND notes d'honoraires) save there, named by `document.projet_document_name(reference, template_name, day)` → `"REF - YYYY-MM-DD - Projet Nom"`

### `models/doc_template.py` (Phase H — gabarits)

- `create_template(file_stream, filename, file_size, metadata, user_id) -> tuple[Optional[dict], list[str]]` — validates size (≤10 MB) / `.docx` extension / archive structure (`validate_template`), extracts + classifies placeholders (`classify_placeholders`), uploads to Storage (`users/{userId}/templates/{templateId}/{filename}`), persists the doc with the field inventory; Storage rollback on Firestore failure
- `get_template(template_id)`, `list_templates(category=None, search=None)` — single `order_by("name")`, filters client-side (small bounded collection, no index)
- `update_template(template_id, data, file_stream=None, filename=None, file_size=None)` — with a file: re-validate, re-extract, upload NEW Storage object, `version += 1`, delete the old object only after the doc points at the new one
- `delete_template(template_id)` — Storage object (NotFound tolerated) + Firestore doc
- `get_template_bytes(template_id) -> Optional[bytes]` — for filling
- `get_note_honoraires_template() -> Optional[dict]` — **Phase H.2**: most-recently-updated `kind == "note_honoraires"` template (Python filter over the small collection, no index); the `/factures/<id>/note-docx` route selects it. `VALID_KINDS = ("gabarit", "note_honoraires")` with `kind` set from a checkbox on the upload/edit form; legacy docs without `kind` read as `"gabarit"`
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

Append-only trust register. **Pure §6.1 helpers** (importable without the client, carry the test suite): `compute_deltas(direction, amount, status)` (the §4.4 balance atom — per-entry book/cleared/bank contribution), `check_disbursement_allowed(cleared, amount)` (the overdraft control), `reconciliation_variance(...)`, `to_barreau_row(tx, view)` (the 9 Barreau columns; single source for HTML/CSV/PDF), `recompute_running_balances(txs, view)` (verification only). **Firestore layer** (the only part touching `db`): accounts CRUD (`create_account`/`get_account`/`list_accounts`/`update_account` — metadata only, never balances); the transactional writes `create_transaction` (the core — reads account/counter/last-entry/dossier/invoice, runs the guards + overdraft control + backdating guard INSIDE the transaction, then commits), `clear_transaction`/`clear_transactions_bulk` (all-or-nothing), `reverse_transaction` (mints the opposite `correction` entry; `en_circulation`→both `annulée`, `compensée`→reversal `en_circulation`), `create_inter_dossier_transfer` (two linked `compensée` legs in one account; overdraft applies to the source); reconciliation (`create_reconciliation`/`complete_reconciliation` — variance must be 0, gated then committed atomically; `list_outstanding`/`list_in_transit`); queries (`list_transactions`/`list_transactions_page` cursor-paginated by `sequence` DESC/`list_card_transactions`/`list_dossier_transactions`/`get_transaction`); and summaries (`get_trust_summary` — `in_transit = book − cleared` per client, no query; `get_firm_trust_snapshot`). **No `update_*`/`delete_*` for transactions; no SUM aggregation** (bounded Python sums, sidestepping the June-2026 index trap). Abort reasons are machine-stable strings mapped to French via `_ABORT_MESSAGES`.

### MCP (Phase I)

The Phase-I MCP layer added 14 tools; Phase K adds 3 read-only trust tools (17 total). The tool handlers in `mcp/handlers.py` compose **existing** model/util functions only; filters the model layer lacks are applied in the handler over a bounded fetch (≤ 200 docs), and **no composite index exists for an MCP-only query**. No tool path writes to Firestore (notably: `list_protocol_steps` derives overdue status by date comparison instead of calling `check_overdue_steps`, which writes; the trust tools call `models/trust.py` read paths only). **Trust tools never emit the bank transit or account number.**

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

- Catalog namespaces: `dossier.*` (incl. derived `role_feminin`, capitalized `role_label`, demandeur/défendeur **positions** swapped by `dossier.role` with `autre` → unresolved, the **recours fields** `objet` / `valeur` (fr-CA currency) / `classe` (`compute_class`) / `prescription` (label) / `droit_action` / `date_pour_agir`, and the **« Mandat » card fields** `type_mandat` / `type_dossier` / `type_honoraires` (all label-mapped — the three label dicts are mirrored locally to keep the module Firestore-free, **kept in sync with `models/dossier.py`**) / `honoraires` (fee + rate jointly, via the shared `format_honoraires`) / `taux_horaire` / `forfait` (fr-CA currency) / `ouverture` / `fermeture` / `retention` (= `fermeture` + 7 ans, via the shared `retention_date`; `routes/dossiers.py` imports both helpers so the card and generated docs match)), `client.*`/`adverse.*`/`destinataire.*` (identical field set; **no `civilite` — civilité is passthrough**; work-address preference for `avocat_adverse`/`expert`/`huissier`/`notaire`; phone work → cell → home via `format_phone_display`), `cabinet.*` (FIRM_*), `date.aujourdhui` (French long date, `1er` for the 1st) / `date.aujourdhui_iso`.
- **Person names render BARE (no honorific) by default (July 2026).** The stored snapshot name carries the `Me`/`M.`/`Mme` prefix (`display_name` prepends it), so `{{dossier.demandeur}}`/`{{…defendeur}}` strip it (`_strip_civility_prefix`) and `{{<slot>.nom_complet}}` builds from first+last (`_nom_bare`) — a procedure intitulé cites the party bare. Each has an **`…_avec_civilite` twin** (`{{dossier.demandeur_avec_civilite}}`, `{{<slot>.nom_complet_avec_civilite}}`) that keeps the honorific, for a letter address block. Accented `…_avec_civilité` spellings are auto-registered (`_register_civility_variants`); the positions also get flat aliases. Organizations have no honorific, so both forms equal the legal name.
- `FLAT_ALIASES` maps the existing gabarits' flat French names (`{{district}}`, `{{numero_dossier}}`, …) onto the catalog — one template set serves this module and the user's Claude.ai skills. The `civilité`/`civilité_récipient` aliases were **removed** (civilité is now passthrough — it must appear in letters but never in court procedures, so the user places and fills it).
- `MANUAL_FIELDS` (deliberately data-less letter metadata: `objet_lettre`, `privilège`/`transmission_lettre` selects, `pièces_jointes` default `"Aucune"`, `référence_externe`, …). **`salutations` was removed** — it is passthrough.
- Missing-value strings (exact): auto field left blank → **`[CHAMP MANQUANT : {name}]`**; manual/unknown-but-prompted left blank → **`[À COMPLÉTER : {name}]`** (`fallback_value`). Passthrough fields get neither — the raw `{{name}}` survives. Generation never fails on a missing value.

### `utils/format_fr.py` (Phase H.2 — French formatting)

Pure functions, thoroughly tested; centralized so the note d'honoraires (and, optionally later, the on-screen invoice + reportlab PDF) format money identically. `format_cents_fr(cents) -> "1 150,00 $"` (NBSP thousands, comma decimal, trailing ` $`; `0` → `"0,00 $"`), `format_cents_fr_parens(cents) -> "(1 150,00) $"` (retainer deduction), `format_rate_fr(rate, scale) -> "5 %"`/`"9,975 %"` (**GST stored ×100, QST ×1000** — caller passes the scale; `models.invoice` stores `gst_rate=500`, `qst_rate=9975`, and `compute_totals` uses hardcoded `Decimal("0.05")`/`Decimal("0.09975")`, so stored rates are display-only), `format_hours_fr(h) -> "0,50"`, `format_date_fr(d) -> "11 décembre 2025"` (`1er` for the first; a datetime uses its UTC calendar date — no Montréal shift).

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

Uses `reportlab.platypus` for tabular reports. Column width ratios (3rd tuple element) define relative widths. Same `cents_fields` / `hours_fields` semantics as CSV. Landscape orientation for wide tables; portrait for narrow. Font: Helvetica.

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

OpenTelemetry tracing. `init_app(app)` (called from `create_app` **before** `init_logging` so the OTel middleware wraps the WSGI app first) exports to Cloud Trace in production (10% sampling, `ParentBased(TraceIdRatioBased)`; override via `TRACE_SAMPLE_RATIO`) and to the console in dev. Auto-instruments Flask, `requests`, and Jinja2. Three PII layers keep query strings, storage paths, and emails/phones out of exported spans (instrumentation hooks, `_SanitizingSpanExporter`, manual-span guard). Manual API: `span("name", **attrs)` context manager, `add_attributes(**attrs)` (enrich current request span), `firestore_span(op, collection, doc_id=...)`, `@traced()` decorator. Span-name and attribute conventions are in `athena/OBSERVABILITY.md`.

---

## DAV Protocol Layer

### URL structure (post-Phase D1)

```
/dav/                                # Principal + addressbook/calendar home-set
├── addressbook/                     # CardDAV — contacts
├── calendar/                        # CalDAV — hearings (VEVENT)
├── tasks/                           # CalDAV — standalone tasks ONLY (dossier_id=None)
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
clear_tombstones(collection_name: str) -> None
delete_sync_state(collection_name: str) -> None   # removes the dav_sync doc (run clear_tombstones first)
```

Call sites that must bump CTags:
- All `parties` CRUD → `bump_ctag("parties")`
- All `hearings` CRUD → `bump_ctag("hearings")`
- `tasks` CRUD → `bump_ctag(f"dossier:{dossier_id}")` if linked, else `bump_ctag("tasks")`
- Task dossier reassignment → tombstone + bump for OLD collection (incl. the standalone `tasks` collection), `remove_tombstone` + bump for NEW
- `notes` CRUD → `bump_ctag(f"dossier:{dossier_id}")`
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
- **The taxonomy SUGGESTS a délai; it never sets one.** `taxonomie.Action.prescription_type` prefills the Prescription dropdown **only on a user action-change** — never on load, or opening an existing dossier would silently overwrite the delay the lawyer confirmed. It is `""` wherever the source's delay is not a single clean period, and those `""`s are load-bearing, not gaps: **FAI-01**'s « 6 mois » is a *retrospective eligibility window* (the acte de faillite must fall in the 6 months **preceding** the application), so suggesting it would compute a deadline that means nothing; RCV-05/COR-06 differ by regime; CJP-* are « délai raisonnable ». Never "fill in" a blank `prescription_type` without re-reading the source row.
- **A `-99` « Autre (préciser) » row must never carry a délai.** Every domaine ends with one so no file is unclassifiable; they have no delay of their own, and the domaine's default (e.g. RES's « 3 ans (art. 2925) ») is **not** theirs to inherit — `action_precision` is where the real object goes.
- **`delai_types` is a tuple over a closed 11-token vocabulary; annex asymmetries are deliberate.** Tokens combine (`("PE","A")`), `D` (stricte, rouge) outranks `DR` (relevable, orange) in `niveau_decheance`, and the legal content of `taxonomie.py` changes **only on an approved spec**. Do not "fix" COR-11 (token `A`, `avis == ()`) or RCV-03 (two conditional avis, typed `(PE,)`) — both are binding annex content, pinned by `test_a_token_and_avis_sets_are_pinned`. The six pinned `ref_delai == ""` rows (CST-05, COR-04, COR-09, FAM-01, FAM-02, FAM-06) are equally deliberate — no statutory delay source exists, and inventing one would derive legal content.
- **§ 4's déchéance list is a cross-section claim.** `niveau_decheance` derives from the `delai_types` tokens, but **APP-01** states « déchéance expresse » only in prose — a per-section reader cannot catch that. `tests/test_taxonomie.py::test_section_4_decheances_all_carry_D` pins the § 4 cross-section as `stricte`; extend it when the source changes.
- **`date_avis` is manual, never derived.** Each avis has its own factual starting point (délivrance du bien, cause d'action…), which is NOT `droit_action_date` — deriving the date would silently compute from the wrong start. The form shows the action's structured avis as the suggestion; the lawyer confirms by filling the field. `compute_echeances` dates an avis only from an explicit `date_depart_avis` + a confirmed scenario (`avis_confirmes`).
- **Direct App Engine access is blocked at three layers** (App Engine firewall → Cloudflare IPs only, `X-Origin-Auth` origin secret, appspot Host check). When debugging, hit the Cloudflare hostname — `gcloud app browse` will 403. New App Engine internal endpoints (cron, queues) must be under `/_ah/` or they'll be rejected by the origin checks.
- **`requirements.txt` is generated — never hand-edit it.** Change `requirements.in`, then re-lock with `uv pip compile` (recipe in the Tech Stack section). Production pip runs with `--require-hashes --no-deps`, so an unhashed edit simply won't deploy.
- **Keep `setuptools<81`** until the OTel instrumentation packages are bumped to ≥0.50b0 — 0.48b0 imports `pkg_resources` at runtime and tracing silently disables without it (and the CI test for the trace log field fails).
- **Exact-pin dependencies (`==X.Y.Z`)** in `requirements.in` — wildcard pins (`==X.*`) break OSV-Scanner's version resolution and produce false-positive CVE reports.
- **Composite indexes must be deployed BEFORE (or with) code that queries them** — `firebase deploy --only firestore:indexes --project athena-pallas`. Until an index builds, the affected queries fail and views gracefully degrade to empty lists. Every new `.where()+.order_by()` combo or filtered aggregation needs an entry in `firestore.indexes.json`.
- **An index that serves a paginated list does NOT serve its SUM aggregation.** Firestore matches SUM/AVG queries only against an index whose *trailing* fields are the aggregated fields in **alphabetical order** (`amount` before `hours`), with directions **matching the query's last sort** (ASC for equality-only queries; DESC after `date DESC, id DESC`). A same-fields index in the wrong tail order is ignored — the query 400s ("requires an index") even though the index is READY, and totals silently degrade to zero (June 2026 dashboard "heures non facturées" incident).
- **Never edit a `static/vendor/` file in place** — they're cached `immutable` for a year. A changed asset gets a new version/hash filename, plus updates to the templates that reference it, the precache list in `static/sw.js`, and the Early Hints lists in `security.py`.
- **Script order at the end of `<body>` is load-bearing** (App Check boot → page scripts → htmx → Alpine). Execution follows document order — the Firebase/App Check boot scripts run synchronously at parse time, and the vendored htmx/Alpine `defer` scripts run in document order at `DOMContentLoaded`; position, not a sync/defer phase, is the guarantee. (Rocket Loader, which used to defer all scripts while preserving that order, was disabled at the edge on 2026-07-11.) Moving htmx above the boot reopens a race where `hx-trigger="load"` requests fire without the `X-Firebase-AppCheck` header and 401; moving Alpine above inline component definitions breaks `x-data` evaluation.
- **MCP output: date-only fields must never pass through `to_mtl`.** Fields stored as midnight UTC (`timeentries.date`, `expenses.date`, invoice `date`/`due_date`, task `due_date`, protocol `start_date`/`end_date`/step `deadline_date`, dossier `opened_date`/`closed_date`/`prescription_date`/`droit_action_date`/`date_avis`) are emitted as the **UTC calendar date** via `mcp.tools.date_str` — a Montréal conversion shifts them to the previous day. True timestamps go through `mcp.tools.iso_mtl`.
- **The MCP endpoint is stateless JSON mode — never add SSE** (`GET /mcp` streams) without revisiting the gunicorn `--timeout 60` sizing; long-lived connections would exhaust the 2×4 worker/thread budget.
- **Firestore TTL is lagging garbage collection, not enforcement.** `oauth_codes`/`oauth_tokens` expiry checks stay in code (`expire_at` comparison on every read); deleted-late docs must still be treated as dead.
- **Cloudflare bot mitigations can challenge Anthropic's egress** on `/mcp`/`/oauth/*` (non-browser client). A Configuration Rule disables Browser Integrity Check on those paths; if Super Bot Fight Mode challenges Claude's requests, relax its "Definitely automated" action and verify in Security → Events (same class of fight as the Play-Store/Bubblewrap episode).
- **`athena/mcp/` shadows any installed `mcp` PyPI package** (the app dir is first on `sys.path`). Never add the MCP Python SDK to `requirements.in` without renaming one of them.
- **The consent screen must reuse class strings that already exist in the compiled CSS** — `athena/mcp/` and `templates/mcp/` are covered by the `@source "../../templates"` scan, but adding a genuinely new utility class still requires the full recompile-and-rehash procedure. Note the app's primary buttons are `bg-gray-900`, not `bg-indigo-600` (which is not in the compiled artifact).
- **Never fill gabarits with `docxtpl`/`python-docx`** (Phase H): their load/save round-trip corrupts letterhead templates for Word (repair prompt). `utils/docx_fill.py` substitutes strings in the XML zip entries and copies everything else byte-identical — keep it that way.
- **Word fragments typed placeholders across `<w:r>` runs** (spell/grammar `proofErr` markers, tracked changes, mid-word format or **language** changes — notably at the dot in a namespaced name like `{{dossier.defendeur}}`, where the two halves get a different proofing/`lang` `rPr`). `utils/docx_fill._normalize_runs` heals this **before** matching (in extract/validate/fill alike): it strips `proofErr` markers and coalesces adjacent text runs (each holding one `<w:t>`) when **either** they carry identical `rPr` — Word's own save-time optimization — **or** joining them bridges a placeholder (`_bridges_placeholder`: the first run holds an unclosed `{{`, or the split fell between the braces). In the **bridge** case formatting differences are ignored and the first run's `rPr` wins — the whole `{{name}}` is replaced by one value anyway, so collapsing its fragments is correct, and it beats shipping an unfillable literal `{{…}}` that **retyping never fixes** (Word re-splits at the dot every save — the July 2026 "fragmenté persists" report). The run open tag is matched as `<w:r(?:\s…)?>` so runs carrying `rsid` attributes are healed too (those attributes are dropped on merge; Word reopens fine). Only genuinely **structural** splits stay unmerged and are still reported via `split_run_suspects`: a `<w:br/>`/`<w:drawing>`/field code between the halves (not a plain `<w:t>` run → no match), a bookmark/comment marker between them (breaks adjacency), or a paragraph boundary. Load-bearing details: the `<w:t>` text capture is `[^<]*` (NOT `.*?` — DOTALL would swallow markup and merge runs across paragraph boundaries); split-run detection is **per-occurrence** (`_name_counts`), so a clean copy of a repeated field can't mask a fragmented sibling (the "only the last `{{tribunal}}` fills" bug); and **`fragmenté` ≠ missing data** — a fragmented field ships as literal `{{…}}`, whereas an intact field with no dossier value fills with a visible `[CHAMP MANQUANT : …]`.
- **The docx paragraph scan must cover ALL paragraphs** — a previous implementation passed `count=1` to `re.sub` and silently skipped block placeholders outside the first paragraph (regression-tested in `test_docx_fill.py`).
- **Fill-engine replacement callbacks must be functions**, never bare strings — user content containing `\g<0>` or backslashes would be interpreted as regex group references (regression-tested).
- **Template files are NOT `documents` records** — they live at `users/{uid}/templates/…` and are managed only through `/gabarits`; generated outputs saved into a dossier ARE regular documents (independent copies).
- **Gabarit placeholders are three-way, not "block vs scalar" (July 2026):** `classify_placeholders` returns **auto** (catalog/alias, case-insensitive — an ALL-CAPS name upper-cases its value), **manual** (`MANUAL_FIELDS` letter metadata), and **passthrough** (everything else). Passthrough — the former ALL-CAPS blocks, **`{{civilité}}`, `{{salutations}}`**, and unknown names — is deliberately **not resolved and not prompted**; the route omits it from the fill `values` so the raw `{{name}}` survives into the .docx for Word. Do **not** re-add civilité to the catalog or a `salutations` default: civilité must appear in letters but never in court procedures, so it is the user's to place. The route **re-classifies on every render** (`template_detail`, `_fields_context`, `_collect_values`), so a template uploaded before this change (whose stored `block_fields` still exists) classifies correctly without a re-upload.
- **Trust (Phase K): the register's "Solde" is the *book* balance; the *cleared* balance is a control and must never be displayed under that label.** Book = Σ(receipts) − Σ(disbursements) over ALL statuses (incl. `annulée`); cleared = receipts once `compensée` minus disbursements while `compensée`/`en_circulation`. `to_barreau_row`'s « Solde » is `balance_after_account` (journal) / `balance_after_client` (carte). The card/entry UI shows both, labelled « Solde aux livres » vs « Disponible (compensé) » — the gap is the deposits-in-transit and is the whole point of the two-step model.
- **Trust: `sequence`, not `date`, is the register's order; a disbursement may only draw on the *cleared* balance.** Backdating (a date before the last entry's) is refused — correct a past error with a **reversal dated today**, never by rewriting history. The overdraft control (`check_disbursement_allowed`) lives INSIDE the Firestore transaction on the same read-set as the write (`create_transaction`), and is confirmed REQUIRED (user decision 2026-07-16 — do not relax to book-only). Reversals bypass the control; the create path refuses `purpose="correction"` so reversal stays the only way to mint one.
- **Trust: `annulée` entries count in the book balance** (they net with their reversal — the register is chronological, so entries #5–#11 must still show the balance as it stood) **and are excluded from the cleared balance.** Getting this backwards double-counts funds. `compute_deltas(direction, amount, status)` is the single arithmetic atom (spec §4.4); `in_transit = book − cleared` per client (annulée pairs net out, so no query needed).
- **Trust: `trust_transactions.date` and `.cleared_date` are date-only (midnight UTC)** — emit via `mcp.tools.date_str` in MCP output and render with `.strftime('%Y-%m-%d')` in templates, **never `to_mtl`/`iso_mtl`** (a Montréal shift moves them to the previous day). The register has **9 export columns, not 8** — column 7 « Recette / Crédit » is split into two per-direction columns « Recette » (recettes) / « Crédit » (déboursés) per the Barreau sheet (user decision 2026-07-16), diverging from spec §8; `to_barreau_row` + the export column lists carry this.
- **Trust: the module fails CLOSED.** `create_transaction`/`clear`/`reverse`/`reconcile` abort on any read failure (never a partial write); list views propagate read errors to the route (never a silently-empty register). `update_dossier` re-reads the three trust map fields at the last moment before its full-doc `set()` so a form save can't clobber a concurrent trust write with a stale (possibly overdraft-permitting) cleared balance. A dossier that ever held a trust entry can **never be deleted** (`trust_transactions` in `_CHILD_COLLECTIONS`) — archive it.

---

## Infrastructure & Deployment

### Current deployed configuration

- **Domain:** `athena.poirierlavoie.ca` (Athena app), `poirierlavoie.ca` (firm website, separate)
- **SSL:** Cloudflare Full Strict with 15-year Origin Certificate (RSA, PKCS#1 format after OpenSSL conversion on Windows)
- **Network ingress:** App Engine firewall restricted to **Cloudflare's published IP ranges** — all traffic must transit Cloudflare. Paired with the in-app `X-Origin-Auth` origin-secret check and the appspot Host check (see Security Rules → Edge defense in depth).
- **Edge security:** Cloudflare Access Zero Trust on `/dav/*` (service token policy for DavX5, Google SSO for interactive)
- **Secrets:** Google Cloud Secret Manager — `flask-secret-key`, `firebase-api-key`, `dav-password-hash`, `cf-origin-secret` (resolved by `config.py` at startup in production)
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
  3. Cloudflare Access must match `/dav/*` only — `/mcp`, `/oauth/*`, `/.well-known/oauth*` must **not** be behind Access.
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

Additional firm/tax env vars consumed by `config.py` (set via `--set-env-vars` or `.env` locally): `FIRM_NAME`, `FIRM_STREET`, `FIRM_UNIT`, `FIRM_CITY`, `FIRM_PROVINCE`, `FIRM_POSTAL_CODE`, `FIRM_PHONE`, `FIRM_EMAIL`, `GST_NUMBER`, `QST_NUMBER`, `SESSION_LIFETIME_HOURS`, `RATE_LIMIT_LOGIN`, `TRACE_SAMPLE_RATIO`, `APPCHECK_DEBUG_TOKEN` (local dev).

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

### Phase K — Trust accounting "comptabilité en fidéicommis" (July 2026, ✅ code complete)

- **K** — The two registers required by RLRQ c. B-1, r. 5: the **journal de caisse** (recettes et déboursés — all clients, chronological, running balance) and the **carte-client** (grand livre auxiliaire — the same rows filtered to one `(dossier_id, client_id)` couple). Two views of ONE collection (`trust_transactions`); one write path, one source of truth. Deliberate divergences from the house patterns (spec §2): **append-only, no `update_*`/`delete_*`** — correction is by **reversal only** (`reverse_transaction` mints an opposite `purpose="correction"` entry; the create path refuses `correction`); exactly **three write-once mutable fields** (`status` `en_circulation`→`compensée`|`annulée`, `cleared_date`, `reconciliation_id`); the **overdraft control** (a déboursé may only draw on the client's `compensée`/cleared balance — confirmed required, user decision 2026-07-16) lives INSIDE the Firestore transaction; the module **fails CLOSED** everywhere. Three balances per `compute_deltas` atom: **book** (all statuses incl. `annulée`; the register's « Solde »), **cleared** (the control; never shown as « Solde »), **bank** (compensée only; the reconciliation anchor). Two-step lifecycle: recorded `en_circulation` when made, `compensée` when it clears the bank, `annulée` only via reversal of an uncleared entry. New model `models/trust.py` (pure §6.1 helpers `compute_deltas`/`check_disbursement_allowed`/`reconciliation_variance`/`to_barreau_row`/`recompute_running_balances` + the Firestore layer: accounts CRUD, transactional `create_transaction`/`clear`/`clear_bulk`/`reverse`, `create_inter_dossier_transfer`, reconciliation with variance gating, per-account monotonic counter `counters/trust-{account_id}`). Three new top-level collections (`trust_accounts` — last-4-only, never the full account number; `trust_transactions`; `trust_reconciliations`) + three `dossiers` fields (`trust_balance`, `trust_balance_by_client`, `trust_cleared_by_client`, defaulted on read by `_migrate_trust`). Routes `routes/trust.py` at `/fideicommis` (journal, entry form, detail, compenser/contrepasser, virement inter-dossiers, carte-client, comptes, conciliations worksheet with live variance, CSV/PDF exports) + a `fideicommis` dossier tab + a dashboard « Sommes en fidéicommis » stat. **Exports have 9 columns** — « Recette / Crédit » split into two per-direction columns (Barreau sheet, user decision 2026-07-16). 3 read-only MCP tools (`get_trust_balance`/`list_trust_transactions`/`get_trust_snapshot`, 14→17; never emit transit/account number) + a consent-screen disclosure. `log_trust_event` + `trust.transaction`/`trust.reconcile` spans. 8 composite indexes; `scripts/verify_trust_integrity.py` recomputes and cross-checks. **Zero new Python dependencies.** Spec: `SPEC_PHASE_K_FIDEICOMMIS.md`. **Ops prerequisite: deploy the 8 `firestore.indexes.json` trust indexes before/with the code**, or the paginated/filtered queries degrade until they build.

### Proposed / not yet implemented

- **Microsoft 365 bidirectional sync** — Graph API OAuth2 + webhook (change notifications) for native Outlook calendar/contacts integration, with Athena-tagged extensions for loop prevention
- **Notes tab on dossier detail** — currently notes are accessed only via the standalone `/notes?dossier_id=…` view
- **Dedicated KYC / conflict-check routes** — model helpers exist (`update_kyc_status`, `link_kyc_document`) but are not yet exposed as discrete routes
- **R2 migration** for Firebase Storage (cost optimization, low priority)
- **Turnstile** migration from reCAPTCHA Enterprise (optional)

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
