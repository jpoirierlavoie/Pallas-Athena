# Pallas Athena — Observability Event Registry

This document is the source of truth for the structured-logging event vocabulary emitted by Pallas Athena. It is read alongside `CLAUDE.md`.

All structured logs go through `athena/utils/logging_setup.py`, which:

- Attaches a Cloud Logging `AppEngineHandler` (log name **`pallas-athena`**) in production, or a stderr stream handler locally.
- Runs every record through `ContextFilter` (injects request-scoped fields) then `RedactionFilter` (drops sensitive keys, scrubs PII from string values).
- Exposes a small set of typed helpers — call those instead of `logger.info(...)` directly so log-based metrics keep working.

## Common fields (every record)

`ContextFilter` adds these to `record.json_fields` for every log emitted inside a Flask request:

| Field | Type | Source |
|---|---|---|
| `request_id` | string | `X-Request-Id` header if present, else a fresh UUID4 hex |
| `trace` | string | `projects/{FIREBASE_PROJECT_ID}/traces/{TRACE_ID}` parsed from `X-Cloud-Trace-Context` (omitted if header absent) |
| `auth_context` | `"session"` \| `"dav_basic"` \| `"anonymous"` | derived from path + session presence |
| `route` | string | matched URL rule (e.g. `/dossiers/<id>/tab/<tab_name>`) — falls back to `request.path` for 404s |
| `method` | string | HTTP method |
| `is_htmx` | bool | `HX-Request` header presence |

Outside a request (cron jobs, scripts, M365 webhook handlers), call `bind_context(**fields)` to populate these manually.

## PII redaction policy

Enforced by `RedactionFilter` (CLAUDE.md, Security Rules — "Do not log PII"):

- Keys in `SENSITIVE_KEYS` (case-insensitive) are replaced with `"<redacted>"`. Includes `authorization`, `cookie`, `session`, `password`, `password_hash`, `secret`, `api_key`, `token`, `id_token`, `access_token`, `refresh_token`, `private_key`, `dav_password_hash`, `csrf_token`, `firebase_token`.
- Free-text matches in string values are scrubbed: emails → `<email>`, phone numbers → `<phone>`, Canadian postal codes → `<postal>`.
- Quebec court file numbers (`NNN-NN-NNNNNN-NNN`) are **preserved by default** — they are public information once filed and useful for correlation. Flip `REDACT_COURT_FILE_NUMBERS = True` in `logging_setup.py` to redact them.
- String values longer than 2048 characters are replaced with `"<truncated, N chars>"`.

To extend redaction: add the key to `SENSITIVE_KEYS` (a module-level set) — no other change needed.

## Event taxonomy

Each helper emits through a dedicated logger so log-based metrics can filter by `logName`.

### `log_auth_event(event, outcome, *, reason=None, **extra)` — logger `pallas.auth`

| `event` | Default severity (success / failure) | Notes |
|---|---|---|
| `login` | INFO / WARNING | Firebase Auth session establishment |
| `logout` | INFO / WARNING | Session cleared |
| `mfa_challenge` | INFO / WARNING | Phone MFA prompt presented |
| `mfa_success` | INFO / WARNING | Second factor verified |
| `auth_failure` | WARNING (always) | Token verification, unauthorized email, etc. |
| `appcheck_failure` | WARNING (always) | App Check verification failed on HTMX request |
| `rate_limit_hit` | WARNING (always) | `flask-limiter` rejected the request |

`reason` should be a short machine-stable string (`"token_invalid"`, `"mfa_missing"`, `"unauthorized_email"`, `"rate_limit_exceeded"`) — never an email or token.

### `log_dossier_event(event, dossier_id, **extra)` — logger `pallas.dossier`

All emitted at INFO.

| `event` | Notes |
|---|---|
| `created` | New dossier saved |
| `updated` | Mutation other than archive/delete |
| `archived` | Status transitioned to `archivé` |
| `viewed` | Detail page loaded (use sparingly — high-volume) |
| `deleted` | Hard delete |
| `court_file_parsed` | `/dossiers/parse-court-file` returned a successful parse |

### `log_dav_operation(operation, collection_type, *, dossier_id=None, object_count=None, duration_ms=None, status_code=None, ctag_bumped=None, **extra)` — logger `pallas.dav`

All emitted at INFO. Optional fields are omitted from the record when `None` so log-based metrics filtering on (e.g.) `ctag_bumped` don't pick up structurally-empty records.

| `operation` | Notes |
|---|---|
| `propfind` | Collection / resource discovery |
| `report` | `addressbook-multiget`, `calendar-multiget`, etc. |
| `get` | Single resource fetch |
| `put` | Create / update |
| `delete` | Resource removal |
| `mkcol` | Collection creation (rare — DavX5 doesn't issue MKCOL today) |
| `sync_collection` | Sync REPORT |

| `collection_type` | Maps to URL prefix |
|---|---|
| `addressbook` | `/dav/addressbook/...` |
| `calendar` | `/dav/calendar/...` |
| `tasks` | `/dav/tasks/...` (standalone tasks) |
| `dossier` | `/dav/dossier-{id}/...` (per-dossier collection — `dossier_id` should be set) |

### `log_security_event(event, severity, **extra)` — logger `pallas.security`

`severity` ∈ `{"warning", "error", "critical"}` maps to Python WARNING / ERROR / CRITICAL.

| `event` | Typical severity | Notes |
|---|---|---|
| `csrf_failure` | warning | `flask-wtf` rejected a POST/PUT/DELETE |
| `request_too_large` | warning | `_enforce_request_size` returned 413 |
| `appspot_blocked` | warning | Direct `*.appspot.com` traffic rejected |
| `csp_violation` | warning | CSP report endpoint received a violation report |
| `appcheck_failure` | warning | Same surface as `log_auth_event("appcheck_failure", ...)` — emit one or the other, not both |
| `session_lookup_failure` | warning | `_derive_auth_context` raised while reading `session["user_id"]` (corrupted cookie payload, `SECRET_KEY` rotation mid-flight, etc.). Request is downgraded to `auth_context="anonymous"` for logging only — authorization is still enforced by `@login_required`. Fields: `reason` (exception class name), `path` (request path). |

### `log_unexpected(message, *, exc_info=True, **extra)` — logger `pallas.unexpected`

Always emitted at ERROR with traceback. This is what `main.py`'s `errorhandler(Exception)` calls — it surfaces to Cloud Error Reporting via the `pallas-athena` log.

## Adding a new event type

1. Extend the relevant `Literal` in `utils/logging_setup.py` (or add a new helper for a new domain).
2. Document the event in this file: name, severity, helper, fields.
3. If the event will drive an alert: add a log-based metric in GCP filtering on `logName="projects/athena-pallas/logs/pallas-athena"` and `jsonPayload.event="..."`.

## IAM requirement

The App Engine default service account (`athena-pallas@appspot.gserviceaccount.com`) needs **`roles/logging.logWriter`** to push records to Cloud Logging. Verify with:

```bash
gcloud projects get-iam-policy athena-pallas \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:athena-pallas@appspot.gserviceaccount.com" \
  --format="value(bindings.role)"
```
