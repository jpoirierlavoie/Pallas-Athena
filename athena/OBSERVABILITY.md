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

## Tracing

Distributed tracing is configured by `athena/utils/tracing_setup.py`. It runs OpenTelemetry, exports to **Cloud Trace** in production, and emits spans to the console in dev. Auto-instrumentation covers Flask, `requests` (so `firebase-admin` outbound calls are captured), and Jinja2.

### Sampling

- Production: 10% of traces, `ParentBased(TraceIdRatioBased(0.1))` — child spans inherit the parent's decision so cross-service traces stay coherent.
- Dev: 100% by default, `AlwaysOn`. Console exporter prints every span.
- Override via env var **`TRACE_SAMPLE_RATIO`** (clamped to `[0.0, 1.0]`). Set to `1.0` for a debugging session, `0.0` to disable.

### Trace ↔ log correlation

`logging_setup.ContextFilter` reads the active OTel span and writes `trace = projects/{FIREBASE_PROJECT_ID}/traces/{trace_id}` onto every record. Cloud Logging UI uses this to render a "View trace" link from each log entry. Because the OTel composite propagator is installed (W3C `traceparent` + GCP `X-Cloud-Trace-Context`), the trace ID seen by logs matches the trace ID Cloud Trace records — they are the same span context.

### Span name conventions

| Prefix | Used for | Examples |
|---|---|---|
| (auto-named, route) | Flask request span (top-level) — auto-instrumented | `GET /dossiers/<id>`, `REPORT /dav/dossier-<id>/` |
| `dav.*` | Application work inside a DAV handler | `dav.parse_sync_token`, `dav.serialize_objects`, `dav.add_tombstones`, `dav.build_multistatus` |
| `firestore.*` | Firestore reads/writes wrapped via `firestore_span` | `firestore.get`, `firestore.query`, `firestore.set` |
| `auth.*` | Reserved — wrap auth verification helpers as needed | (not yet instrumented) |
| `pallas.<module>.<qualname>` | Default name produced by the `@traced()` decorator | `models.dossier.create_dossier` |

### Standard attributes

| Attribute | Type | Set by | Purpose |
|---|---|---|---|
| `service.name` | string | resource | Always `pallas-athena` |
| `service.version` | string | resource | App Engine `GAE_VERSION` (or `local`) |
| `deployment.environment` | string | resource | `production` / `development` |
| `dav.collection_type` | string | manual | `addressbook` / `calendar` / `tasks` / `dossier` / `root` |
| `dav.operation` | string | manual | `propfind` / `report` / `get` / `put` / `delete` / `sync_collection` |
| `dav.dossier_id` | string | manual | Per-dossier collection ID |
| `dav.depth` | string | manual | DAV `Depth` request header (`0` / `1` / `infinity`) |
| `dav.report_type` | string | manual | `sync-collection` / `calendar-multiget` / `calendar-query` |
| `dav.component_type` | string | manual | `VTODO` / `VJOURNAL` |
| `dav.object_count` | int | manual | Total resources serialized |
| `dav.task_count`, `dav.note_count` | int | manual | Per-component breakdown for sync_collection |
| `dav.tombstone_count` | int | manual | Tombstones included in a sync response |
| `dav.changed_count` | int | manual | Resources actually changed (sync_collection) |
| `dav.sync_token` | string | manual | Client-provided token (or `initial`) |
| `dav.body_size` | int | manual | Inbound iCalendar / vCard body length on PUT |
| `dav.conditional` | bool | manual | Whether the request used `If-Match` / `If-None-Match` |
| `dav.response_status` | int | manual | HTTP status (only set on outcomes worth highlighting) |
| `db.system` | string | `firestore_span` | Always `firestore` |
| `db.collection` | string | `firestore_span` | Firestore collection name |
| `db.document_id` | string | `firestore_span` | Firestore doc ID (omitted for queries) |

Memory note: never attach raw vCalendar / vCard bodies — log size, not content. `dav.body_size` is the canonical handle.

### Adding instrumentation

1. **Top-level enrichment.** Inside a Flask handler, call `add_attributes(...)` once at the top of the function. The Flask auto-instrumentation already opened a span for the request; this enriches it without nesting. Cheap and high-signal.
2. **Sub-spans for measurable work.** Use `with span("phase.name", attr=val):` around discrete phases (parse, serialize, build response). Aim 3–6 spans per request total — more makes the waterfall harder to read.
3. **Firestore reads.** Wrap with `firestore_span("get"|"query"|"set", "<collection>", doc_id="...", **extra)`. Reserve for hot paths (DAV layer + future heavy aggregations); don't migrate every model call.
4. **Function-scoped spans.** Use `@traced("name", attr=val)` to wrap an entire function. Convenient when the same work runs from multiple call sites.

The canonical example is `_handle_sync_collection` in [dav/dossier_collections.py](athena/dav/dossier_collections.py): top-level `add_attributes`, sub-spans for `dav.parse_sync_token` / `dav.serialize_objects` / `dav.build_multistatus`, and `firestore_span` calls for the dav_sync read, tasks query, notes query, and tombstones query.

### Bumping sampling for a debugging session

Production runs at 10% — fine for normal monitoring, sparse for debugging. To get 100% sampling on a hot deploy without a full redeploy:

```bash
gcloud app deploy app.yaml --set-env-vars=TRACE_SAMPLE_RATIO=1.0
```

…then revert by removing the override after debugging. Don't leave 100% sampling on in production: F2 instances are 256MB and BatchSpanProcessor's queue grows with span volume.

## IAM requirement

The App Engine default service account (`athena-pallas@appspot.gserviceaccount.com`) needs:

- **`roles/logging.logWriter`** — push records to Cloud Logging.
- **`roles/cloudtrace.agent`** — push spans to Cloud Trace.

Verify with:

```bash
gcloud projects get-iam-policy athena-pallas \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:athena-pallas@appspot.gserviceaccount.com" \
  --format="value(bindings.role)"
```
