# Pallas Athena — Observability Event Registry

This document is the source of truth for the structured-logging event vocabulary emitted by Pallas Athena. It is read alongside `CLAUDE.md`.

All structured logs go through `athena/utils/logging_setup.py`, which:

- Attaches a Cloud Logging `CloudLoggingHandler` (log name **`pallas-athena`**) in production, or a stderr stream handler locally. `CloudLoggingHandler` (not the deprecated `AppEngineHandler`, whose `emit` str-formats every record and drops `json_fields`) routes each record's `json_fields` into the LogEntry **`jsonPayload`** — so the event vocabulary below is queryable as `jsonPayload.event`, `jsonPayload.outcome`, etc. The human-readable message lands under `jsonPayload.message`.
- Runs every record through `ContextFilter` (injects request-scoped fields) then `RedactionFilter` (drops sensitive keys; scrubs PII from `json_fields`, the formatted message — including `%`-style args, which are pre-interpolated in the filter — and rendered exception tracebacks).
- Exposes a small set of typed helpers — call those instead of `logger.info(...)` directly so log-based metrics keep working.

## Common fields (every record)

`ContextFilter` adds these to `record.json_fields` for every log emitted inside a Flask request:

| Field | Type | Source |
|---|---|---|
| `request_id` | string | `X-Request-Id` header if present, else a fresh UUID4 hex |
| `trace` | string | `projects/{FIREBASE_PROJECT_ID}/traces/{TRACE_ID}` parsed from `X-Cloud-Trace-Context` (omitted if header absent) |
| `auth_context` | `"session"` \| `"dav_basic"` \| `"mcp_bearer"` \| `"anonymous"` | derived from path + session presence (`/mcp` → `mcp_bearer`) |
| `route` | string | matched URL rule (e.g. `/dossiers/<id>/tab/<tab_name>`) — falls back to `request.path` for 404s |
| `method` | string | HTTP method |
| `is_htmx` | bool | `HX-Request` header presence |

Outside a request (cron jobs, scripts, M365 webhook handlers), call `bind_context(**fields)` to populate these manually.

## PII redaction policy

Enforced by `RedactionFilter` (CLAUDE.md, Security Rules — "Do not log PII"):

- Keys in `SENSITIVE_KEYS` (case-insensitive) are replaced with `"<redacted>"`. Includes `authorization`, `cookie`, `session`, `password`, `password_hash`, `secret`, `api_key`, `token`, `id_token`, `access_token`, `refresh_token`, `private_key`, `dav_password_hash`, `csrf_token`, `firebase_token`.
- Free-text matches are scrubbed: emails → `<email>`, phone numbers → `<phone>`, Canadian postal codes → `<postal>`. The scrub covers:
  - every string inside `record.json_fields` (recursively) and dict messages;
  - the **formatted message** — records carrying `%`-style `args` are pre-interpolated inside the filter (`record.getMessage()`), scrubbed, and their `args` cleared, so `logger.warning("... %s", value)` call sites cannot leak the arg values; plain string messages without args are scrubbed too;
  - **exception tracebacks** — when `exc_info` is set, the filter pre-renders the traceback, scrubs it line by line, caches the result in `record.exc_text`, and clears `exc_info`, so both the Cloud Logging handler and the stderr handler emit only the redacted text. Trade-off: Cloud Error Reporting groups errors by stack trace, so scrubbing PII embedded in exception messages may split or merge some error groups — accepted versus shipping PII.
- Control characters (C0 + DEL + C1, plus U+2028/U+2029 line separators) are escaped to visible sequences (`\n` → `\\n`, others → `\\xNN`/`\\uNNNN`) in messages and json_fields, so user-controlled values cannot forge log entries on plain-text handlers (CWE-117). Neutralization runs **after** the PII pass — the phone/postal regexes need `\s` to match raw control whitespace. Tracebacks are split on `\n` only (not `splitlines()`, whose extra boundary characters would re-emerge as real newlines) and escaped per line — inter-frame newlines survive. Call sites that interpolate user-controlled values (URL path segments, request fields) into log messages should additionally wrap them in `sanitize_log_value(...)` — that cuts the taint where static analyzers (CodeQL) can see it.
- Quebec court file numbers (`NNN-NN-NNNNNN-NNN`) are **preserved by default** — they are public information once filed and useful for correlation. Flip `REDACT_COURT_FILE_NUMBERS = True` in `logging_setup.py` to redact them.
- String values longer than 2048 characters are replaced with `"<truncated, N chars>"` (applied per line for tracebacks, so one oversized frame never swallows the whole stack).

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
| `redirect_rejected` | warning | `safe_internal_redirect` rejected a `return_to` value (open-redirect guard). Fields: `reason` (`"not_internal_path"`, `"backslash_in_path"`, `"scheme_or_netloc_present"`). The rejected URL itself is **not** logged — it could be attacker-controlled. |

### `log_mcp_event(event, outcome, *, client_id=None, tool=None, reason=None, **extra)` — logger `pallas.mcp`

`outcome` ∈ `{"success", "failure", "refused"}` — `success` emits at INFO, `failure`/`refused` at WARNING. Optional fields (`client_id`, `tool`, `reason`) are omitted when `None`. `reason` is a short machine-stable string (`"invalid_token"`, `"code_reused"`, `"kill_switch"`) — **never** a token, authorization code, or PKCE verifier (also covered by `SENSITIVE_KEYS` redaction, but don't rely on it).

| `event` | Typical outcome | Notes |
|---|---|---|
| `mcp_client_registered` | success | Dynamic Client Registration accepted (`client_id`) |
| `mcp_consent` | success / refused | Consent screen decision (« Autoriser » / « Refuser ») |
| `mcp_token_issued` | success | Token endpoint issued a pair; `grant` = `authorization_code` \| `refresh_token` |
| `mcp_token_refused` | refused | Token endpoint rejection; `reason` = `code_unknown`, `code_reused`, `code_expired`, `client_mismatch`, `redirect_uri_mismatch`, `pkce_mismatch`, `refresh_unknown`, `refresh_replayed`, `refresh_expired`, `unsupported_grant_type` |
| `mcp_token_revoked` | success | RFC 7009 revocation of a single access token |
| `mcp_family_revoked` | success | Whole token family revoked (code replay, refresh replay, revocation of a refresh token); `revoked_count` |
| `mcp_auth_failure` | refused | Bearer validation failed on `/mcp`; `reason` = `missing_token` (OAuth discovery path — expected), `invalid_token`, `oversized_token`, `insufficient_scope`, `resource_mismatch`, `origin_forbidden` |
| `mcp_brake_engaged` | refused | Per-IP invalid-token brake returned 429 |
| `mcp_initialize` | success | MCP `initialize` handled; sanitized `client_name`/`client_version`, `protocol_version` |
| `mcp_tool_call` | success / failure | Tool executed; fields: `tool`, `duration_ms`, `dossier_id` (when the call carries one) |
| `mcp_disabled_hit` | refused | Kill switch (`MCP_ENABLED=false`) returned 404 on a Phase-I route |

### `log_template_event(event, *, template_id=None, dossier_id=None, **extra)` — logger `pallas.templates`

INFO, except `generation_failed` (WARNING). **Never pass field values** (client PII) — placeholder names, counts and IDs only; the `RedactionFilter` is a backstop, not the policy.

| `event` | Notes |
|---|---|
| `template_uploaded` | New gabarit; `template_id`, `placeholder_count`, `warning_count` (split-run suspects) |
| `template_updated` | Metadata edit or file replacement; `file_replaced: bool`, `version` |
| `template_deleted` | Gabarit + Storage object removed |
| `document_generated` | `template_id`, `dossier_id` (when saved), `saved_document_id` (when saved), `field_count`, `missing_count` (blanks replaced by the visible French fallback). **Note d'honoraires (Phase H.2):** adds `invoice_id`, `source="facture"`, and the three row counts `rows_honoraire` / `rows_debours_tx` / `rows_debours_ntx` (instead of `field_count`/`missing_count`) |
| `generation_failed` | WARNING; `reason` machine-stable (`template_not_found`, `template_file_unavailable`, `template_invalid`, `fill_error`, `save_failed`; Phase H.2 adds `no_note_template`, `invoice_voided`, `unbalanced_condition`) — never a filename or field value |

### `log_unexpected(message, *, exc_info=True, **extra)` — logger `pallas.unexpected`

Always emitted at ERROR with traceback. This is what `main.py`'s `errorhandler(Exception)` calls — it surfaces to Cloud Error Reporting via the `pallas-athena` log. The traceback text is PII-scrubbed by `RedactionFilter` before emission (see "PII redaction policy" above for the Error Reporting grouping trade-off).

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
- **Warning:** `TRACE_SAMPLE_RATIO=1.0` multiplies trace egress ~10× — every request exports spans to Cloud Trace (more ingestion cost, more BatchSpanProcessor queue pressure on 256MB F2 instances, and a larger exfiltration surface for anything the sanitizing layers below might miss). Use it for short debugging windows only, then revert.

### PII controls in traces

Three layers in `utils/tracing_setup.py` keep PII out of exported spans:

1. **Instrumentation hooks.** The Flask request/response hooks overwrite `http.target` (and `http.url` when present) with the request path only, so query strings (e.g. client-name searches like `/parties/?q=Tremblay`) never persist on request spans. The `requests` hook rewrites outbound `http.url` to `scheme://host/path` — and for `*storage.googleapis.com` hosts keeps `scheme://host` only, because both the object path and the `name=` query param embed uid / dossier / filename.
2. **Sanitizing exporter.** `_SanitizingSpanExporter` wraps the Cloud Trace exporter (and the dev console exporter). Before delegating, it strips query strings from URL-like attribute keys (`http.target`, `http.url`, `http.route`, `url.full`, `url.path`, `url.query`) and applies the same email / phone / postal regex scrub as the logging `RedactionFilter` (the patterns are imported from `logging_setup`, not duplicated) to every string attribute value. This is the defense-in-depth backstop for anything the hooks miss.
3. **Manual-span guard.** `span()`, `add_attributes()` and `firestore_span()` drop any attribute whose key is in the logging layer's `SENSITIVE_KEYS` and scrub string values before setting them.

These layers are a safety net, not an invitation: as with logs, never attach raw vCard / iCalendar bodies, client names, or signed URLs as span attributes.

### Trace ↔ log correlation

`logging_setup.ContextFilter` reads the active OTel span and writes `trace = projects/{FIREBASE_PROJECT_ID}/traces/{trace_id}` onto every record. Cloud Logging UI uses this to render a "View trace" link from each log entry. Because the OTel composite propagator is installed (W3C `traceparent` + GCP `X-Cloud-Trace-Context`), the trace ID seen by logs matches the trace ID Cloud Trace records — they are the same span context.

### Span name conventions

| Prefix | Used for | Examples |
|---|---|---|
| (auto-named, route) | Flask request span (top-level) — auto-instrumented | `GET /dossiers/<id>`, `REPORT /dav/dossier-<id>/` |
| `dav.*` | Application work inside a DAV handler | `dav.parse_sync_token`, `dav.serialize_objects`, `dav.add_tombstones`, `dav.build_multistatus` |
| `firestore.*` | Firestore reads/writes wrapped via `firestore_span` | `firestore.get`, `firestore.query`, `firestore.set` |
| `auth.*` | Reserved — wrap auth verification helpers as needed | (not yet instrumented) |
| `mcp.request` | MCP JSON-RPC dispatch (one per POST /mcp) | `mcp.request` with `method` attribute |
| `mcp.tool.*` | One span per tool execution | `mcp.tool.get_agenda`, `mcp.tool.list_dossiers` |
| `template.fill` | docx fill inside the generation POST (Phase H / H.2) | `template.fill` with `template_id`, `field_count` (gabarits) or `invoice_id` + `rows_honoraire`/`rows_debours_tx`/`rows_debours_ntx` (note d'honoraires) — never values or content, counts and IDs only |
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
| `method` | string | manual (`mcp.request`) | JSON-RPC method (`initialize`, `tools/call`, …) |
| `template_id` | string | manual (`template.fill` + request span) | Gabarit UUID |
| `field_count` | int | manual (`template.fill` + request span) | Placeholders filled in a generation |
| `invoice_id` | string | manual (`template.fill` + request span, Phase H.2) | Invoice UUID for a note d'honoraires — ID only |
| `rows_honoraire` / `rows_debours_tx` / `rows_debours_ntx` | int | manual (Phase H.2) | Note-d'honoraires table row counts — counts only, never figures or descriptions |
| `dossier_id` | string | manual (`mcp.tool.*`) | Set when the tool call carries a `dossier_id` argument — UUIDs only, never names/emails/token material |
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

…then revert by removing the override after debugging. Don't leave 100% sampling on in production: `TRACE_SAMPLE_RATIO=1.0` multiplies trace egress (~10× the default), F2 instances are 256MB and BatchSpanProcessor's queue grows with span volume — and every additional exported span widens the surface the PII-sanitizing layers have to cover.

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
