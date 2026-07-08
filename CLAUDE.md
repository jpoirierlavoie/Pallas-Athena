# Pallas Athena — Master Reference

Pallas Athena is a single-user legal practice management web application for a Québec civil litigation lawyer (Jason Poirier Lavoie, Barreau du Québec). It manages contacts (parties), dossiers (case files), billable hours, expenses, invoices, hearings, tasks, case protocols, notes, and procedural documents. It synchronizes bidirectionally with DavX5 on Android via CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL).

Deployed at `athena.poirierlavoie.ca`. GCP project: `athena-pallas`. Codebase is on GitHub; deploys via Cloud Build trigger on push to main.

This document supersedes the old `SPEC.md` and phase-specific markdown files as the canonical reference for future work.

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
  **Script order in `base.html`/`login.html` is load-bearing:** Firebase/App Check boot → page scripts → htmx → Alpine, all at the end of `<body>`. Cloudflare Rocket Loader (enabled at the edge) defers every script but preserves *document order* — position, not sync/defer phases, is the only execution-order guarantee. Never move htmx/Alpine above the App Check boot or above inline component definitions.
  Vendored assets are served `Cache-Control: immutable` (1 year) — **a changed asset MUST get a new filename**; never edit one in place. Dynamically-assembled class names get purged at compile time: keep classes as complete string literals in templates / `routes/*.py` / `models/*.py` (all scanned via `@source`), or safelist them in `app.input.css`.
- **DAV libraries:** `icalendar`, `vobject`. Custom CardDAV/CalDAV/RFC-5545 endpoints served directly from Flask.
- **MCP connector (Phase I):** a hand-rolled, stateless **JSON-response-mode Streamable HTTP** MCP server at `POST /mcp` (no SSE, no sessions) plus an **embedded OAuth 2.1 authorization server** (`mcp/` package), exposing 14 read-only tools to Claude as a custom connector. **Zero new Python dependencies** — stdlib (`secrets`, `hashlib`, `base64`) + packages already pinned (Flask, flask-limiter, flask-wtf). Kill switch: `MCP_ENABLED` env var (default `"true"`; `false` → every `/mcp` + `/oauth/*` route 404s).
- **Markdown:** `Markdown` + `bleach` libraries for rendering note content (rendered via Jinja `markdown` filter).
- **PDF:** `reportlab` (pure Python — do NOT use `weasyprint`; it requires cairo/pango system libs unavailable on App Engine Standard).
- **Word templates (Phase H — gabarits):** user-managed `.docx` templates filled by a **stdlib-only engine** (`utils/docx_fill.py`: `zipfile` + `re` + `io` — direct string substitution on the XML zip entries, every other entry copied byte-identical). **`docxtpl`/`python-docx` are explicitly rejected** — their load/save round-trip rewrites enough of the OOXML package that Word refuses to open letterhead templates with multiple headers/footers, `titlePg` sections, and embedded fonts. Zero new Python dependencies.
- **Hosting:** Google App Engine Standard, Python 3.13 runtime, F2 instance class.
- **CDN / edge:** Cloudflare **Pro plan** (Full Strict SSL, Origin Certificate, Access Zero Trust for `/dav/*`, Argo Smart Routing, **Rocket Loader** — see the script-order rule above, **Early Hints** — `security.py` emits the `Link` preload headers Cloudflare converts to HTTP 103). The App Engine firewall accepts only Cloudflare IP ranges, so the edge is not bypassable (see Security Rules → Edge defense in depth).
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
2. **Firestore is flat.** Despite the single-user nature, Firestore **collections are top-level** (`parties`, `dossiers`, `tasks`, `hearings`, `notes`, `protocols`, `invoices`, `timeentries`, `expenses`, `documents`, `doc_templates`, `dav_sync`, `counters`, `ref_greffes`, `ref_juridictions`, plus the Phase-I OAuth collections `oauth_clients`, `oauth_codes`, `oauth_tokens`). They are **not** nested under `users/{userId}/...`. Firebase Storage paths, however, **do** use `users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}` (with `userId` from the Firebase Auth `uid` claim).
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
  - `Content-Security-Policy-Report-Only` (currently report-only — see `CSP` constant in `security.py`). `script-src` is `'self'` + `ajax.cloudflare.com` (Rocket Loader — remove if it's ever disabled at the edge) + the Google origins the App Check SDK loads reCAPTCHA Enterprise from (`gstatic.com`, `apis.google.com`, `google.com`) — **no script CDN origins** (assets are vendored). Violations are posted to `/csp-report` (`report-uri`) and logged as `csp_violation` security events.
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
│   ├── firestore.rules             # (security rules — Firestore client SDK is not used; rules are defensive)
│   ├── firestore.indexes.json      # Composite indexes — deploy with `firebase deploy --only firestore:indexes`
│   ├── storage.rules               # Firebase Storage rules
│   ├── OBSERVABILITY.md            # Structured-logging event registry + tracing conventions (source of truth)
│   ├── .gcloudignore               # Keeps tests/venv/dev files out of the deployed bundle
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
│   │   └── reference.py            # Read-only: ref_greffes, ref_juridictions
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
│   │   └── doc_templates.py        # /gabarits/*  (Phase H: lifecycle + HTMX generation popup)
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
│   │   └── handlers.py             # 14 read-only tool implementations calling models/* + utils/*
│   │
│   ├── utils/                      # Utility modules
│   │   ├── __init__.py
│   │   ├── deadlines.py            # Quebec art. 83 C.p.c. judicial deadline calc
│   │   ├── docx_fill.py            # Phase H: stdlib-only .docx placeholder fill engine (zip XML substitution)
│   │   ├── template_fields.py      # Phase H: field catalog, flat aliases, classification, resolution
│   │   ├── validators.py           # Phone (E.164), email, postal code normalization, address defaults
│   │   ├── export_csv.py           # CSV export helper (UTF-8 BOM)
│   │   ├── export_pdf.py           # reportlab-based PDF export
│   │   ├── logging_setup.py        # Cloud Logging handler, ContextFilter, RedactionFilter, typed log helpers
│   │   └── tracing_setup.py        # OpenTelemetry → Cloud Trace, PII-sanitizing exporter, span()/@traced
│   │
│   ├── scripts/                    # One-time / manual scripts (run with python -m scripts.X)
│   │   ├── __init__.py
│   │   ├── normalize_existing.py   # Backfill E.164 phones + normalized postal codes (Phase B)
│   │   ├── seed_reference_data.py  # Populate ref_greffes + ref_juridictions (Phase G)
│   │   ├── mint_dev_token.py       # Local-dev MCP bearer minting (refuses ENV=production)
│   │   └── revoke_mcp_tokens.py    # Break-glass: revoke all MCP tokens (+ optional client purge)
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
│   │   └── test_template_fields.py
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
│   │   ├── dossiers/               # list, detail, form + _dossier_rows + _tab_overview, _tab_temps,
│   │   │                           # _tab_facturation, _tab_audiences, _tab_taches, _tab_protocole,
│   │   │                           # _tab_documents, _tab_placeholder
│   │   ├── time_expenses/          # list, time_form, expense_form + _time_rows, _expense_rows
│   │   ├── invoices/               # list, detail, create + _invoice_rows + _unbilled_items
│   │   ├── hearings/               # list, detail, form + _hearing_rows + _month_grid
│   │   ├── tasks/                  # list, detail, form + _task_row, _task_rows
│   │   ├── notes/                  # list, detail, form + _note_rows
│   │   ├── protocols/              # list, detail, form + _protocol_rows
│   │   ├── documents/              # list, detail, upload, edit + _browser, _document_rows, _folder_tree
│   │   ├── gabarits/               # list, detail, form + _template_rows, _generate_modal, _generate_fields
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
├── firebase.json                   # Firebase CLI targets (firestore rules+indexes, storage rules)
├── .env.example                    # Template for local-dev env vars
└── .github/                        # dependabot.yml + workflows: codeql, osv-scanner, trivy, bandit,
                                    # dependency-review, scorecard
```

> Note on tab names: the dossier detail uses an HTMX tab loader (`/dossiers/<id>/tab/<tab_name>`). Active tab names are `apercu`, `temps`, `facturation`, `audiences`, `taches`, `protocole`, `documents` — there is no separate `notes` tab in the dossier hub today. Notes live at the standalone `/notes` view (filterable by `?dossier_id=`).

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

    # Parties on the dossier (replaces the legacy single client_id)
    "clients":          [{"id": UUIDv4, "name": str}, ...],
    "client_ids":       [UUIDv4, ...],    # mirrors clients[].id (for array_contains)
    "opposing_parties": [{"id": UUIDv4, "name": str}, ...],
    "opposing_party_ids": [UUIDv4, ...],

    # Classification
    "matter_type": "litige_civil" | "litige_commercial" | "recouvrement"
                 | "injonction" | "familial" | "autre",
    "role": "demandeur" | "défendeur" | "intervenant" | "mis en cause" | "autre",

    # Phase G — Court file number + parsed judicial metadata
    "court_file_number": str,             # Raw, e.g., "500-05-123456-241"
    "greffe_number": str,                 # 3-digit parsed code
    "juridiction_number": str,            # 2-digit parsed code
    "tribunal": str,                      # Auto-populated from ref_juridictions
    "competence": str,                    # Auto-populated
    "palais_de_justice": str,             # Auto-populated from ref_greffes
    "district_judiciaire": str,           # Auto-populated
    "is_administrative_tribunal": bool,   # True if letters prefix (TAL, TAQ…)

    # Financial (cents)
    "fee_type": "hourly" | "flat" | "contingency" | "mixed",
    "hourly_rate": int,                   # cents (default 25000 = $250/h)
    "flat_fee": int | None,

    # Status
    "status": "actif" | "en_attente" | "fermé" | "archivé",
    "opened_date": datetime, "closed_date": datetime | None,

    # Prescription
    "prescription_date": datetime | None, "prescription_notes": str,

    "notes": str,
    "internal_notes": str,                # Never shown externally

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
    "amount": int,                        # cents, computed: hours * rate
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
    "invoice_number": str,                # "YYYY-F###" sequential
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

### `doc_templates/{templateId}` — Document templates ("gabarits", Phase H)

Top-level collection; standard common fields (`id`, `created_at`, `updated_at`, `etag`). Not DAV-exposed — no DAV UID, no CTag bumping. Template files live in **Storage** at `users/{userId}/templates/{templateId}/{filename}` (signed URLs, 15-min expiry) and are **not** `documents` records; generated outputs saved into a dossier ARE regular `documents` records (independent copies — deleting a gabarit never touches them). No composite index (small collection: single `order_by("name")`, category/search filtered client-side).

```python
{
    "name": str,                       # ≤120 chars, required
    "description": str,
    "category": "procédure" | "correspondance" | "autre",
    "filename": str,                   # secure_filename()d, .docx
    "original_filename": str,
    "file_size": int,                  # bytes (≤ 10 MB)
    "storage_path": "users/{userId}/templates/{templateId}/{filename}",
    "version": int,                    # starts at 1, +1 on each file replacement

    # Extracted at upload / file replacement (utils/docx_fill + utils/template_fields)
    "placeholders": list[str],         # distinct {{...}} names, document order
    "auto_fields": list[str],          # resolvable from the field catalog
    "manual_fields": list[str],        # scalar user inputs (known manual + unknown)
    "block_fields": list[str],         # ALL-CAPS names → multi-paragraph textareas
    "slots_required": list[str],       # ⊆ {"dossier","client","adverse","destinataire"}
    "validation_warnings": list[str],  # French split-run warnings at last upload
}
```

### `ref_greffes/{greffeNumber}` — Quebec courthouse reference (top-level, read-only)

Document ID is the 3-digit greffe number (string). Seeded from `scripts/seed_reference_data.py`.

```python
{
    "greffe_number": "500",
    "palais_de_justice": "Montréal",
    "district_judiciaire": "Montréal",
    "point_de_service": bool,             # True = itinerant
    "other_locations": list[str],         # For shared greffes (614, 635, 640, 652)
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
| `/dossiers/<id>/tab/<tab_name>` | GET | HTMX tab loader (`apercu`, `temps`, `facturation`, `audiences`, `taches`, `protocole`, `documents`) |
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

The 14 read-only tools (`get_agenda`, `list_dossiers`, `get_dossier`, `list_tasks`, `list_hearings`, `list_notes`, `get_note`, `list_documents`, `list_parties`, `get_partie`, `get_billing_snapshot`, `list_protocol_steps`, `compute_judicial_deadline`, `parse_court_file_number`) live in `mcp/handlers.py` with schemas in `mcp/tools.py`. Conventions: money as `*_cents` + fr-CA `*_display`; date-only fields as `YYYY-MM-DD` (UTC calendar date); true timestamps as ISO-8601 America/Montreal; every list tool capped at 50 items with a `truncated` flag; no signed URLs or storage paths ever in tool output.

### `doc_templates.py` — `/gabarits/*` (Phase H)

All `@login_required`. Template upload/replace POSTs carry multipart `.docx` (10 MB cap — see Security Rules). The generation popup is an HTMX modal whose selection state is **server-owned**: slot changes re-render the field form; clicked search results carry their selection as `set_*` query params (which win over the `hx-include`-carried current state).

| Route | Method | Purpose |
|-------|--------|---------|
| `/gabarits/` | GET | List (name, category badge, version, placeholder count, warnings badge); FAB "+"; empty state invites first upload |
| `/gabarits/new` | GET | Upload form (multipart: file + name + description + category) |
| `/gabarits/` | POST | Create → redirect to detail (which shows the extracted field inventory + split-run warnings) |
| `/gabarits/<id>` | GET | Detail: metadata, auto/manual/block field chips, warnings, « Générer », « Télécharger le gabarit », « Modifier », « Supprimer » |
| `/gabarits/<id>/edit` | GET | Edit form (metadata + optional replacement file) |
| `/gabarits/<id>` | POST | Update (file replacement → re-validate, re-extract, version += 1, old Storage object deleted) |
| `/gabarits/<id>/delete` | POST | Delete (Firestore doc + Storage object; generated documents untouched) |
| `/gabarits/<id>/download` | GET | Redirect to signed URL (15 min) |
| `/gabarits/dossier-search` | GET | HTMX dossier autocomplete (rows reload the modal with `set_dossier_id`) |
| `/gabarits/partie-search` | GET | HTMX partie autocomplete (rows reload the field form with `set_destinataire_id`; optional `?role=`) |
| `/gabarits/generer` | GET | Popup step 1 (modal partial): template select (or fixed via `?template_id=&fixed=1`), dossier picker (locked via `?locked=1`), prefills from `?dossier_id=` / `?partie_id=` (→ destinataire slot) |
| `/gabarits/generer/champs` | GET | Popup step 2 (field-form partial): slot selects + every placeholder as editable input/textarea, prefilled via `resolve_values`, manual defaults applied |
| `/gabarits/generer` | POST | Generate: fill → dossier present → save via `document.upload_document` (category from template; display_name `"{name} — {date}"`) + HTMX success partial; no dossier → direct `.docx` attachment (plain POST, `target="_blank"`) |

**Entry points:** dossier detail header + Documents-tab toolbar (« Générer depuis un gabarit », dossier locked), partie detail header (« Générer un document », partie → destinataire), gabarit list rows/detail (« Générer »). Each host page carries a `<div id="gabarit-modal">` mount point.

### Top-level miscellaneous routes (defined in `main.py`)

| Route | Purpose |
|-------|---------|
| `/offline` | Service-worker offline fallback page |
| `/.well-known/assetlinks.json` | Android TWA Digital Asset Links (SHA-256 fingerprint of the signing key) |
| `/manifest.json` | PWA manifest (served as static file) |
| `/sw.js` | Service worker (served as static file with `Service-Worker-Allowed: /`) |
| `/privacy`, `/terms` | Static legal pages |
| `/csp-report` | POST, CSRF-exempt — receives browser CSP violation reports (the `report-uri` of the report-only policy); logs a `csp_violation` security event, returns 204 |
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
- `delete_dossier` REFUSES deletion while child records exist (documents, time entries, expenses, invoices, hearings, tasks, notes, protocols, folders) and fails CLOSED when the child check errors — archive instead of deleting
- `dossier_to_vjournal(dossier) -> str`, `vjournal_to_dossier(ical_str) -> dict` — legacy, retained for potential export (not used by DAV post-D1)

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
- Invoice numbers come from a transactional counter at `counters/invoices-{year}` (`seq` field, seeded from a max-scan on first use; monotonic, so numbers are never reused). Allocation failure aborts invoice creation — no fallback number.

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

### `models/doc_template.py` (Phase H — gabarits)

- `create_template(file_stream, filename, file_size, metadata, user_id) -> tuple[Optional[dict], list[str]]` — validates size (≤10 MB) / `.docx` extension / archive structure (`validate_template`), extracts + classifies placeholders (`classify_placeholders`), uploads to Storage (`users/{userId}/templates/{templateId}/{filename}`), persists the doc with the field inventory; Storage rollback on Firestore failure
- `get_template(template_id)`, `list_templates(category=None, search=None)` — single `order_by("name")`, filters client-side (small bounded collection, no index)
- `update_template(template_id, data, file_stream=None, filename=None, file_size=None)` — with a file: re-validate, re-extract, upload NEW Storage object, `version += 1`, delete the old object only after the doc points at the new one
- `delete_template(template_id)` — Storage object (NotFound tolerated) + Firestore doc
- `get_template_bytes(template_id) -> Optional[bytes]` — for filling
- `get_signed_url(template_id, expires_in_minutes=15)` — IAM signBlob signing, attachment disposition
- `VALID_CATEGORIES = ("procédure", "correspondance", "autre")`, `MAX_TEMPLATE_SIZE`, `DOCX_MIME`
- Split-run suspects become French `validation_warnings` strings; the upload proceeds (the field simply won't fill until retyped in Word and re-uploaded)

### `models/reference.py` (read-only)

- `get_greffe(greffe_number) -> dict | None`
- `get_juridiction(juridiction_number) -> dict | None`
- `list_greffes()`, `list_juridictions()`
- `parse_court_file_number(court_file_number) -> dict` — returns `{greffe_number, juridiction_number, greffe, juridiction, is_administrative, parse_error}`. Letters prefix → `is_administrative=True`, no parsing. Format `NNN-NN-...` required, else `parse_error`.

### MCP (Phase I)

No model-layer changes. The 14 tool handlers in `mcp/handlers.py` compose **existing** model/util functions only; filters the model layer lacks are applied in the handler over a bounded fetch (≤ 200 docs), and **no composite index exists for an MCP-only query**. No tool path writes to Firestore (notably: `list_protocol_steps` derives overdue status by date comparison instead of calling `check_overdue_steps`, which writes).

---

## Utility Modules

### `utils/docx_fill.py` (Phase H — pure stdlib fill engine)

No Firestore, no Flask — fully unit-testable. Operates by direct string substitution on `word/document.xml` + `word/header*.xml` + `word/footer*.xml` inside the zip; every other entry is copied byte-identical (Word must reopen the output without repair — the reason `docxtpl` is rejected).

```python
PLACEHOLDER_RE                                  # {{name}} — accents, dots, optional inner spaces
extract_placeholders(docx_bytes) -> list[str]    # distinct names, document order (tag-stripped scan)
validate_template(docx_bytes) -> TemplateValidation  # .placeholders, .split_run_suspects, .errors (French)
fill_docx(docx_bytes, values) -> bytes           # raises DocxFillError on structural problems
```

- **Block expansion first** (values containing blank-line separators): the host `<w:p>` is cloned once per chunk with the placeholder substituted — numbered-list `<w:pPr>` XML is preserved, so chunks continue the list numbering. The paragraph scan covers ALL paragraphs (regression: a previous implementation passed `count=1`).
- **Scalars second**; single `\n` → one space; XML escaping (`& < >`) + C0 control stripping (except `\t`); **function replacement callbacks only** (a bare replacement string would interpret `\g<0>`/backslashes in user content).
- Safety caps: compressed ≤ 10 MB, single XML target ≤ 25 MB, total decompressed ≤ 100 MB, ≤ 2000 entries, no absolute/`..` entry names, magic `PK\x03\x04` + `[Content_Types].xml` + `word/document.xml` required.
- **Split-run detection:** names visible in tag-stripped text but not matchable in raw XML were fragmented across `<w:r>` runs by Word → reported as suspects at upload (user retypes the field in Word in one stroke); never silently rewritten.

### `utils/template_fields.py` (Phase H — field catalog)

Pure functions (mirrors `display_name` locally — must stay importable without the Firestore client). `classify_placeholders(names) -> Classification` (auto map, manual scalars, ALL-CAPS blocks, `slots_required` ⊆ {dossier, client, adverse, destinataire}, unknowns) and `resolve_values(names, *, dossier, client, adverse, destinataire, firm, today) -> dict[str, str]` (only non-empty resolutions; absent = popup shows an empty input).

- Catalog namespaces: `dossier.*` (incl. derived `role_feminin` and demandeur/défendeur **positions** swapped by `dossier.role`; `autre` → unresolved), `client.*`/`adverse.*`/`destinataire.*` (identical field set: civilité from prefix then gender; work-address preference for `avocat_adverse`/`expert`/`huissier`/`notaire`; phone work → cell → home via `format_phone_display`), `cabinet.*` (FIRM_*), `date.aujourdhui` (French long date, `1er` for the 1st) / `date.aujourdhui_iso`.
- `FLAT_ALIASES` maps the four existing gabarits' flat French names (`{{district}}`, `{{civilité_récipient}}`, …) onto the catalog — one template set serves this module and the user's Claude.ai skills.
- `MANUAL_FIELDS` (deliberately data-less: `objet_lettre`, `privilège`/`transmission_lettre` selects, `salutations` via `salutations_default(civilité)`, `pièces_jointes` default `"Aucune"`, …).
- Missing-value strings (exact): auto field left blank → **`[CHAMP MANQUANT : {name}]`**; manual/block/unknown left blank → **`[À COMPLÉTER : {name}]`** (`fallback_value`). Generation never fails on a missing value.

### `utils/deadlines.py`

Implements Quebec judicial delay rules under **art. 83 C.p.c.**: all calendar days count; if the raw deadline lands on a non-juridical day (weekend or statutory holiday), extend further in the direction of computation until a juridical day is reached.

```python
compute_deadline(start_date: date, delay_days: int, direction: "after"|"before") -> date
is_juridical_day(d: date) -> bool
next_juridical_day(d: date) -> date
prev_juridical_day(d: date) -> date
get_quebec_holidays(year: int) -> list[date]
_easter_sunday(year: int) -> date          # Meeus/Jones/Butcher algorithm
```

Quebec statutory holidays handled: Jour de l'An (+ Jan 2 if Jan 1 is Sunday), Vendredi saint, Lundi de Pâques, Journée nationale des patriotes (Monday before May 25), Fête nationale (June 24), Fête du Canada (July 1), Fête du Travail (1st Monday September), Action de grâce (2nd Monday October), Noël (Dec 25). Sunday→Monday observation rule applies to fixed holidays.

Integration points:
- `models/protocol.py`: `_compute_deadline` (CQ/CS template offsets and `recompute_deadlines`)
- `routes/dashboard.py`: `_get_prescription_alerts` computes `last_action_date = prev_juridical_day(prescription_date)` for display

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

Populates `ref_greffes` (~55 entries) and `ref_juridictions` (~15 entries) from hardcoded Python lists. Idempotent — overwrites documents. Run once after initial deploy, or when reference data changes.

### `scripts/normalize_existing.py` (Phase B)

One-time backfill: reads all `parties` documents, runs `normalize_phone` / `normalize_email` / `normalize_postal_code` / `apply_address_defaults` on each, writes back only changed records (with new etag + `updated_at`), bumps the `parties` CTag. Prints a summary. Safe to re-run.

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
- **CSP is in Report-Only mode today** — violations are reported but not blocked. Switch to enforcing `Content-Security-Policy` only after a clean reporting window.
- **Documents blueprint isn't nested under dossiers.** Routes live at `/documents/...` and the dossier scope is passed as `?dossier_id=…` (GET) or as a form field (POST). When linking from a dossier tab, always include `dossier_id` in the URL.
- **Hearings prefix is `/audiences`**, not `/agenda`. Internal `url_for()` calls must use the `hearings.*` blueprint.
- **Dossier `clients` and `opposing_parties` are arrays**, not single FKs. Code reading legacy `client_id` must go through `_migrate_parties` (already applied in `get_dossier`/`list_dossiers`).
- **Direct App Engine access is blocked at three layers** (App Engine firewall → Cloudflare IPs only, `X-Origin-Auth` origin secret, appspot Host check). When debugging, hit the Cloudflare hostname — `gcloud app browse` will 403. New App Engine internal endpoints (cron, queues) must be under `/_ah/` or they'll be rejected by the origin checks.
- **`requirements.txt` is generated — never hand-edit it.** Change `requirements.in`, then re-lock with `uv pip compile` (recipe in the Tech Stack section). Production pip runs with `--require-hashes --no-deps`, so an unhashed edit simply won't deploy.
- **Keep `setuptools<81`** until the OTel instrumentation packages are bumped to ≥0.50b0 — 0.48b0 imports `pkg_resources` at runtime and tracing silently disables without it (and the CI test for the trace log field fails).
- **Exact-pin dependencies (`==X.Y.Z`)** in `requirements.in` — wildcard pins (`==X.*`) break OSV-Scanner's version resolution and produce false-positive CVE reports.
- **Composite indexes must be deployed BEFORE (or with) code that queries them** — `firebase deploy --only firestore:indexes --project athena-pallas`. Until an index builds, the affected queries fail and views gracefully degrade to empty lists. Every new `.where()+.order_by()` combo or filtered aggregation needs an entry in `firestore.indexes.json`.
- **An index that serves a paginated list does NOT serve its SUM aggregation.** Firestore matches SUM/AVG queries only against an index whose *trailing* fields are the aggregated fields in **alphabetical order** (`amount` before `hours`), with directions **matching the query's last sort** (ASC for equality-only queries; DESC after `date DESC, id DESC`). A same-fields index in the wrong tail order is ignored — the query 400s ("requires an index") even though the index is READY, and totals silently degrade to zero (June 2026 dashboard "heures non facturées" incident).
- **Never edit a `static/vendor/` file in place** — they're cached `immutable` for a year. A changed asset gets a new version/hash filename, plus updates to the templates that reference it, the precache list in `static/sw.js`, and the Early Hints lists in `security.py`.
- **Script order at the end of `<body>` is load-bearing under Rocket Loader** (App Check boot → page scripts → htmx → Alpine). Rocket Loader defers all scripts but preserves document order, so position is the only cross-regime execution-order guarantee. Moving htmx above the boot reopens a race where `hx-trigger="load"` requests fire without the `X-Firebase-AppCheck` header and 401; moving Alpine above inline component definitions breaks `x-data` evaluation.
- **MCP output: date-only fields must never pass through `to_mtl`.** Fields stored as midnight UTC (`timeentries.date`, `expenses.date`, invoice `date`/`due_date`, task `due_date`, protocol `start_date`/`end_date`/step `deadline_date`, dossier `opened_date`/`closed_date`/`prescription_date`) are emitted as the **UTC calendar date** via `mcp.tools.date_str` — a Montréal conversion shifts them to the previous day. True timestamps go through `mcp.tools.iso_mtl`.
- **The MCP endpoint is stateless JSON mode — never add SSE** (`GET /mcp` streams) without revisiting the gunicorn `--timeout 60` sizing; long-lived connections would exhaust the 2×4 worker/thread budget.
- **Firestore TTL is lagging garbage collection, not enforcement.** `oauth_codes`/`oauth_tokens` expiry checks stay in code (`expire_at` comparison on every read); deleted-late docs must still be treated as dead.
- **Cloudflare bot mitigations can challenge Anthropic's egress** on `/mcp`/`/oauth/*` (non-browser client). A Configuration Rule disables Browser Integrity Check on those paths; if Super Bot Fight Mode challenges Claude's requests, relax its "Definitely automated" action and verify in Security → Events (same class of fight as the Play-Store/Bubblewrap episode).
- **`athena/mcp/` shadows any installed `mcp` PyPI package** (the app dir is first on `sys.path`). Never add the MCP Python SDK to `requirements.in` without renaming one of them.
- **The consent screen must reuse class strings that already exist in the compiled CSS** — `athena/mcp/` and `templates/mcp/` are covered by the `@source "../../templates"` scan, but adding a genuinely new utility class still requires the full recompile-and-rehash procedure. Note the app's primary buttons are `bg-gray-900`, not `bg-indigo-600` (which is not in the compiled artifact).
- **Never fill gabarits with `docxtpl`/`python-docx`** (Phase H): their load/save round-trip corrupts letterhead templates for Word (repair prompt). `utils/docx_fill.py` substitutes strings in the XML zip entries and copies everything else byte-identical — keep it that way.
- **Word fragments typed placeholders across `<w:r>` runs** (spell/grammar `proofErr` markers, tracked changes, mid-word formatting). `utils/docx_fill._normalize_runs` heals this **before** matching (in extract/validate/fill alike): it strips `proofErr` markers and coalesces adjacent runs with **identical** `rPr` whose sole content is one `<w:t>` — the same run-optimization Word does on save, so the output still opens without repair. Only genuinely un-mergeable fragments remain (halves with *different* formatting) and are still reported via `split_run_suspects`. Two load-bearing details: the `<w:t>` text capture is `[^<]*` (NOT `.*?` — DOTALL would swallow markup and merge runs across paragraph boundaries), and split-run detection is **per-occurrence** (`_name_counts`), so a clean copy of a repeated field can't mask a fragmented sibling (that masking was the "only the last `{{tribunal}}` fills" bug).
- **The docx paragraph scan must cover ALL paragraphs** — a previous implementation passed `count=1` to `re.sub` and silently skipped block placeholders outside the first paragraph (regression-tested in `test_docx_fill.py`).
- **Fill-engine replacement callbacks must be functions**, never bare strings — user content containing `\g<0>` or backslashes would be interpreted as regex group references (regression-tested).
- **Template files are NOT `documents` records** — they live at `users/{uid}/templates/…` and are managed only through `/gabarits`; generated outputs saved into a dossier ARE regular documents (independent copies).

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
- Secret Manager accessor on the four application secrets

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

- **H** — User-managed `.docx` templates at `/gabarits` (upload / metadata edit / file replacement with version bump + re-extraction / delete / signed-URL download — templates are data, never a deploy). Stdlib-only fill engine (`utils/docx_fill.py`: XML substitution inside the zip, byte-identical pass-through of everything else; `docxtpl`/`python-docx` rejected — Word repair-prompt issue with letterhead templates). Field catalog + flat-alias table for the four existing gabarits (`utils/template_fields.py`); ALL-CAPS block fields expand by paragraph cloning (numbered lists continue); split-run placeholders detected at upload and reported in French; blanks become visible `[CHAMP MANQUANT : x]` / `[À COMPLÉTER : x]` strings. HTMX generation popup from three entry points (gabarits, dossier detail — locked dossier, partie detail — destinataire prefill); output saved into the dossier's documents or downloaded directly. 10 MB upload size exemption in `security.py`; `log_template_event` + `template.fill` span; zero new dependencies. Spec: `SPEC_PHASE_H_GABARITS.md`.

### Proposed / not yet implemented

- **Microsoft 365 bidirectional sync** — Graph API OAuth2 + webhook (change notifications) for native Outlook calendar/contacts integration, with Athena-tagged extensions for loop prevention
- **Notes tab on dossier detail** — currently notes are accessed only via the standalone `/notes?dossier_id=…` view
- **Dedicated KYC / conflict-check routes** — model helpers exist (`update_kyc_status`, `link_kyc_document`) but are not yet exposed as discrete routes
- **R2 migration** for Firebase Storage (cost optimization, low priority)
- **Turnstile** migration from reCAPTCHA Enterprise (optional)
- **Switch CSP to enforcing mode** once the report-only window is clean

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
