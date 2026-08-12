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
| `budget_saved` | A NEW budget version was minted (append-only — never an overwrite). Fields: `budget_id`, `version`, `line_count` — never amounts (the trust « never amounts » rule) |
| `budget_exported` | A budget PDF was generated. Fields: `budget_id`, `version`, `variant` ∈ `estimation`\|`suivi` — never amounts |

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
| `mcp_write` | success | Any write tool completed (the generalized audit line, July 2026 — one per `tools/call` on a member of `WRITE_TOOLS`); fields: `tool`, `dossier_id`, `entity_id`, `dry_run`, `idempotent_replay` (both mean « nothing new was written » — the line says which kind of nothing), `ctag_bumped`, `dav_synced` (distinct: a closed dossier bumps correctly but is never advertised to DavX5). **IDs and flags only — never a title, description or body.** Since August 2026 this also covers `complete_task`, the connector's ONE status change: an already-in-that-state call logs `dry_run: false` with `ctag_bumped: false`, which reads correctly as « a real call that changed nothing » rather than as a simulation |
| `mcp_note_written` | success | A write tool committed a note (Phase L; kept beside `mcp_write` for log-metric continuity — note writes emit BOTH); fields: `tool` (`create_note`/`append_to_note`), `dossier_id`, `note_id`, `content_chars`, `ctag_bumped`. **IDs and counts only — never the note title or body** (privileged work product; the `RedactionFilter` does not auto-scrub titles or free text) |
| `mcp_write_refused` | refused | A write tool was refused before execution; `reason` = `insufficient_scope` (token lacks `athena:write`) or `write_disabled` (`MCP_WRITE_ENABLED=false`); `tool` when known |

> `mcp_consent` and `mcp_token_issued` also carry the granted `scope` string (and `write_granted` on consent). A scope is not a credential — it is the only way to answer « pourquoi le connecteur ne peut-il pas écrire ? » after the fact.
> `mcp_auth_failure` gained one `reason`: `write_revalidation_failed` — a write tool re-read its token (bypassing the bearer success cache) and found it revoked, expired, or no longer write-scoped.

### `log_template_event(event, *, template_id=None, dossier_id=None, **extra)` — logger `pallas.templates`

INFO, except `generation_failed` (WARNING). **Never pass field values** (client PII) — placeholder names, counts and IDs only; the `RedactionFilter` is a backstop, not the policy.

| `event` | Notes |
|---|---|
| `template_uploaded` | New gabarit; `template_id`, `placeholder_count`, `warning_count` (split-run suspects) |
| `template_updated` | Metadata edit or file replacement; `file_replaced: bool`, `version` |
| `template_deleted` | Gabarit + Storage object removed |
| `document_generated` | `template_id`, `dossier_id` (when saved), `saved_document_id` (when saved), `field_count`, `missing_count` (blanks replaced by the visible French fallback). **Note d'honoraires (Phase H.2):** adds `invoice_id`, `source="facture"`, and the three row counts `rows_honoraire` / `rows_debours_tx` / `rows_debours_ntx` (instead of `field_count`/`missing_count`). **Impression de note (Phase H.3):** adds `note_id`, `source="note"`, `field_count` — never the note's title or content |
| `generation_failed` | WARNING; `reason` machine-stable (`template_not_found`, `template_file_unavailable`, `template_invalid`, `fill_error`, `save_failed`; Phase H.2 adds `no_note_template`, `invoice_voided`, `unbalanced_condition`; Phase H.3 adds `no_note_print_template`) — never a filename or field value |

### `log_trust_event(event, outcome='success', *, transaction_id=None, dossier_id=None, account_id=None, reconciliation_id=None, reason=None, **extra)` — logger `pallas.trust`

Trust accounting (« comptabilité en fidéicommis », Phase K). `outcome` ∈ `{"success", "refused"}` — `success` emits at INFO, `refused` at WARNING. Optional fields are omitted when `None`. `reason` is a short machine-stable string (`"insufficient_cleared_balance"`, or a §5 abort string such as `antidatage_refusé` / `facture_non_émise`) — **never** an account-holder name, client string, or dollar amount. The `RedactionFilter` does NOT auto-scrub names or amounts (only emails/phones/postal/court-file), so keep them out of the fields entirely. `variance_cents` (on `trust_reconciliation_variance`) is the single number ever logged — a control failure with no client attached, useless without it.

| `event` | Typical outcome | Notes |
|---|---|---|
| `trust_transaction_created` | success | An écriture (or an inter-dossier transfer leg) was appended; `transaction_id`, `account_id`, `dossier_id`, `direction`, `purpose`, `sequence` |
| `trust_transaction_cleared` | success | An entry moved `en_circulation` → `compensée`; `transaction_id` |
| `trust_transaction_reversed` | success | A contre-passation was recorded; `transaction_id` (the reversal), `reverses_id`, `annulled: bool` (true when both entries became `annulée`) |
| `trust_overdraft_refused` | refused | The cleared-funds control blocked a déboursé; `dossier_id`, `account_id`, `reason` = `insufficient_cleared_balance`. **The module's most important WARNING.** |
| `trust_transaction_refused` | refused | Any other create abort; `reason` = a §5 abort string (`compte_fermé`, `client_hors_dossier`, `antidatage_refusé`, `facture_non_émise`, …) |
| `trust_reconciliation_completed` | success | A reconciliation was balanced and completed; `reconciliation_id`, `account_id`, `cleared_count` |
| `trust_reconciliation_variance` | refused | Completion refused because the variance was non-zero; `reconciliation_id`, `account_id`, `variance_cents` |
| `trust_reconciliation_abandoned` | success | A DRAFT reconciliation was deleted (« Abandonner » — a brouillon never mutated any transaction); `reconciliation_id`, `account_id` |
| `trust_export` | success | Journal / carte-client CSV or PDF export; `format`, `view`, `row_count` |

### `log_portail_event(event, outcome='success', *, invitation_id=None, batch=None, dossier_id=None, document_id=None, reason=None, **extra)` — logger `pallas.portail`

Portail client (spec L1). One vocabulary for **both services**: the portal process emits the client-facing events, the main service emits the task/reconciliation/courriel/Réception ones — Cloud Logging separates them by `resource.labels.module_id` (`portail` vs `default`; the log name `pallas-athena` is shared, so any alert filtering only on `logName` now also matches portal traffic). `outcome` ∈ `{"success", "refused", "failure"}` → INFO / WARNING / **ERROR** — a `failure` means work could be lost (enqueue failures, reconciliation repairs) and must reach error dashboards. **IDs and counts only**: a client's email, a file name, or a display label must NEVER appear in any field — the `RedactionFilter` auto-scrubs emails but not names/filenames, and portal identity is exactly what this boundary protects.

| `event` | Typical outcome | Notes |
|---|---|---|
| `session_creee` | success | Portal session established after email-link sign-in; `invitation_id` |
| `session_refusee` | refused | Session creation or the per-request guard refused; `reason` machine-stable (`token_invalid`, `claim_missing`, `email_mismatch`, `expired`, `inactive`, `no_session`, …) — the CLIENT always sees the same generic French message |
| `televersement_ouvert` | success | Resumable GCS session opened; `invitation_id`, `batch`, `taille` |
| `televersement_rejete` | refused | Upload refused at validation; `reason` ∈ `extension` / `taille` / `quota_files` / `quota_volume` |
| `soumission_finalisee` | success | Envelope written (the submission is ACQUIRED); `invitation_id`, `batch`, `files_count` |
| `renvoi_demande` | success | A sign-in link was re-generated (main service); `invitation_id`, `emailed: bool` |
| `tache_enfilee` | success | Cloud Tasks enqueue; `invitation_id`, `batch`, `evenement` |
| `tache_enfilage_echec` | **failure** | Enqueue failed. At finalization this is NOT fatal (the envelope exists; reconciliation replays); for `renvoi` it just means no email |
| `tache_recue` | success / refused | Handler entry; `evenement`, `retry_count`; `refused` + `reason` (`malformed`, `no_batch`, `envelope_missing`) for the 200-no-op branches |
| `manifeste_ecrit` | success | SHA-512 hashes computed + manifeste.json written; `invitation_id`, `batch`, `files_count` |
| `accuse_envoye` | success | Accusé de réception emailed (behind the transactional `poser_accuse` test-and-set — at most once per lot) |
| `courriel_envoye` / `courriel_echec` | success / refused ou failure | Graph sendMail outcome; `reason` = `graph_not_configured` (refused) or `graph_error` (failure). A failure AFTER the accusé marker is set is logged here and never retried — the marker already guarantees at-most-once |
| `reconciliation_execute` | success | Cron sweep done; `lots_vus`, `lots_repares` |
| `reconciliation_reparation` | **failure** | An envelope existed with no recorded submission/accusé → re-enqueued. **Every repair means the queue lost work — a symptom to watch** (§8.4) |
| `lot_abandonne` | **failure** | A quarantine prefix holds files but **no envelope**, and stopped moving >2 h ago: the client uploaded but never completed « Soumettre » (guard refusal mid-upload, expired session, closed tab). Nothing references it — Réception cannot see it and the 90-day lifecycle would delete it silently; `invitation_id`, `batch`. **No accusé is ever emitted for such a lot** — it would attest reception of files the client never confirmed |
| `document_verse` | success | A quarantine file was ingested into the dossier; `invitation_id`, `batch`, `dossier_id`, `document_id` |
| `document_refuse` | success | A file was explicitly refused in Réception |
| `versement_divergence` | **failure** | The live quarantine blob no longer matches the manifest at « Verser » time — `reason` = `taille` (blob grew past 25 Mo since hashing; refused BEFORE any download so the oversized object never reaches RAM) or `sha512` (downloaded bytes ≠ the manifest fingerprint the accusé attested). Integrity anomaly, deliberately ERROR: the reviewed file is not the file about to enter the dossier. IDs only (`invitation_id`, `batch`, `seq`) — never a filename |
| `lot_traite` | success | Lot archived (envelope+manifeste → `archive/`, files purged), invitation → `traitée` |
| `invitation_emise` | success | Invitation created (+ claim stamped); `invitation_id`, `dossier_id`, `emailed: bool` |
| `invitation_revoquee` | success | Instant revocation from Réception |
| `intake_etape` | success | Wizard step merged into the server-side draft (L3); `invitation_id`, `etape` (a digit). **Never a field value** — the draft is the client's identity |
| `intake_soumis` | success / refused / **failure** | Intake envelope written; `invitation_id`, `batch`, `adverses` (count). `refused` + `reason=deja_soumis` on a replay within the same second (the batch id is second-resolution). **`failure` + `reason=enveloppe_malformee`** means the envelope could not be parsed main-side — both convergence markers are set anyway, or reconciliation would re-enqueue that lot every 15 min forever, and Réception shows it with a banner |
| `intake_confirmation_envoyee` | success / refused | Gabarit A.3 emailed, behind the same `poser_accuse` test-and-set (at most once per lot). `refused` + `reason=enveloppe_malformee` when nothing was confirmed |
| `intake_partie_creee` | success | Réception created a contact from an ouverture; `invitation_id`, `batch`, `adverses_crees`. Conformité is untouched — collecting is not verifying |
| `intake_partie_mise_a_jour` | success | Field-by-field apply; `champs` (count applied), `adverses_crees` |
| `intake_adverse_cree` | success | A declared adverse party was created as a contact (D-L3-2); `invitation_id` only — **never the name** |
| `intake_refuse` | refused | An ouverture was refused; no email is sent to the client (D-L3-3) |

> `log_auth_event` gained one `reason`: `portail_claim` — a Firebase token carrying the portal custom claim tried to open a session on the main service (spec L1 §1.2 defense in depth).

### `log_bookings_event(event, outcome='success', *, hearing_id=None, reason=None, **extra)` — logger `pallas.bookings`

Bookings sync (spec L2) — the « Bookings with me » → rendez-vous à confirmer pipeline — plus the **miroir Outlook** (2026-07-29, same mailbox, same 10-min cron cadence). `outcome` ∈ `{"success", "refused", "failure"}` → INFO / WARNING / **ERROR**. Same PII discipline as `pallas.portail`: **IDs, opaque Graph identifiers and counts only** — a client's name or a meeting subject must NEVER appear (the `RedactionFilter` scrubs full email addresses but not names/subjects). Counters travel in `**extra`.

| `event` | Typical outcome | Notes |
|---|---|---|
| `bookings_sync_execute` | success | Cron sweep done; counters `vus`, `detectes`, `crees`, `modifies`, `annules`, `divergences` |
| `bookings_sync_erreur_graph` | refused ou **failure** | `refused` + `reason="not_configured"` (Graph creds / mailbox absent — fail-open, no-op); `failure` + `reason="graph_error"` (a Graph outage — the cycle was missed, the next 10-min run retries) |
| `reception_rdv_confirme` | success | A rendez-vous was confirmed in Réception; `hearing_id`, `partie_liee: bool` — the event now enters DAV/Calendar (CTag bumped) |
| `reception_rdv_refuse` | success ou refused | A rendez-vous was refused; `hearing_id`, `graph_annule: bool`. `refused` + `reason="graph_error"` when the Outlook cancellation failed (the Athéna refusal still stands — the juriste is told to cancel manually) |
| `reception_rdv_divergence_traitee` | success | A `bookings_divergence` alert was applied/ignored/cancelled; `hearing_id`, `action` |
| `miroir_outlook_execute` | success | Outlook-mirror cron sweep done; counters `vus`, `miroirs`, `crees`, `corriges`, `supprimes`, `ignores`, `erreurs` (per-event Graph failures — the sweep continues) |
| `miroir_outlook_erreur_graph` | refused ou **failure** | `refused` + `reason="not_configured"` (fail-open, no-op); `failure` + `reason="graph_error"` (Graph outage, cycle missed) or `reason="fenetre_pleine"` (the 500-hearing fetch window is full: the desired set is truncated, so the DELETE phase is disarmed until `_LIMITE_FENETRE` is raised — loud, never silent) |

### `log_unexpected(message, *, exc_info=True, **extra)` — logger `pallas.unexpected`

Always emitted at ERROR with traceback. This is what `main.py`'s `errorhandler(Exception)` calls — it surfaces to Cloud Error Reporting via the `pallas-athena` log. The traceback text is PII-scrubbed by `RedactionFilter` before emission (see "PII redaction policy" above for the Error Reporting grouping trade-off).

## Adding a new event type

1. Extend the relevant `Literal` in `utils/logging_setup.py` (or add a new helper for a new domain).
2. Document the event in this file: name, severity, helper, fields.
3. If the event will drive an alert: add a log-based metric in GCP filtering on `logName="projects/athena-pallas/logs/pallas-athena"` and `jsonPayload.event="..."`.

## Tracing

Distributed tracing is configured by `athena/utils/tracing_setup.py`. It runs OpenTelemetry, exports over **OTLP/gRPC to `telemetry.googleapis.com`** in production, and emits spans to the console in dev. Auto-instrumentation covers Flask, `requests` (so `firebase-admin` outbound calls are captured), and Jinja2.

**Versions (2026-07-30):** api/sdk **1.44.0** with contrib instrumentation **0.65b0** — the stable and beta lines are paired and each instrumentation package hard-pins its siblings at `==`, so they move as one atomic edit. The claims in this section are pinned by `tests/test_tracing_setup.py`, which was written against the *previous* versions (1.27.0/0.48b0) and re-run against these — a deliberate order, so the tests measure a bump rather than record its outcome.

### OTLP export (2026-07-30 — replaced `CloudTraceSpanExporter`)

Google deprecated **all** its exporters on 2026-07-29, but deprecation was not the motive: no end date is published, and `cloudtrace.googleapis.com` cannot disappear since the OTLP path requires it to stay enabled. The motive is **silent data loss, today** — the legacy API caps a span at **32 attributes and 256 bytes per value**, and truncates without an error ("the Cloud Trace API uses a non-deterministic algorithm to select 32 attributes to ingest. The remaining attributes are discarded"). Between the Flask auto-instrumentation (12 attributes measured), `requests`, `firestore_span` and `add_attributes`, a loaded span plausibly crosses that line. OTLP allows **1024 attributes / 64 KiB per value** with unrestricted daily ingestion, at the same price.

**PII corollary, easy to miss:** values that the legacy API truncated at 256 bytes server-side now survive intact up to 64 KiB. The scrubber below became *more* load-bearing with this migration, not less.

Three things about this path are load-bearing:

- **Auth goes through `credentials=`, never `headers=`.** `headers=` is frozen at construction (`self._headers = tuple(...)`, reused verbatim on every `Export`), so a Bearer token placed there expires in ~1 h — and `UNAUTHENTICATED` is **not** in the exporter's retryable codes, so export would stop dead while the app kept serving. `create_google_grpc_credentials()` (from `opentelemetry-exporter-credential-provider-gcp`) builds `composite_channel_credentials(ssl, metadata_call_credentials(AuthMetadataPlugin(...)))`, whose `__call__` gRPC invokes on **every** RPC, re-minting the token. This is also why the transport is **gRPC and not HTTP**: `credentials=` does not exist on the HTTP exporter.
- **`gcp.project_id` is the routing key**, set by hand in `_build_resource()` from `FIREBASE_PROJECT_ID`. The GCP resource detector does **not** supply it (it emits `cloud.account.id`, `cloud.platform`, `faas.*`, `cloud.region`); Google's `MIGRATION.md` claims otherwise and is wrong. What happens when it is absent is documented nowhere, so it is not a bet worth taking — `tests/test_tracing_setup.py::test_otlp_export_reaches_a_grpc_server` asserts it on the payload a real gRPC server receives.
- **Attribute keys keep their OTel names.** The legacy exporter remapped them (`http.method` → `/http/method`); OTLP passes them verbatim. **Any saved Cloud Trace filter targeting `/http/*` stops matching** after this migration — rewrite it against the bare key.

`OTEL_EXPORTER_OTLP_TIMEOUT: "5"` (seconds) is set in both `app.yaml` and `portail.yaml`: the default is 10 s and the exporter retries up to 6 times with exponential backoff, which could hold the batch thread past gunicorn's 30 s `--graceful-timeout` during a shutdown. `OTEL_EXPORTER_OTLP_PROTOCOL` is deliberately **not** set (the SDK reads it only under `opentelemetry-instrument`), and no `x-goog-user-project` header is sent (Google forbids it — duplicate values fail the request).

**Resource detectors need an env var.** Since SDK 1.42 they are loaded **only** when `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` is set (`gcp` in `app.yaml` and `portail.yaml`). Google's migration guide still claims the GCP detector is automatic — that is false on 1.42+, and the failure is silent: no GCP resource labels, no error.

### Sampling

- Production: 10% of traces, `ParentBased(TraceIdRatioBased(0.1))` — child spans inherit the parent's decision so cross-service traces stay coherent.
- Dev: 100% by default, `AlwaysOn`. Console exporter prints every span.
- Override via env var **`TRACE_SAMPLE_RATIO`** (clamped to `[0.0, 1.0]`). Set to `1.0` for a debugging session, `0.0` to disable.
- **Warning:** `TRACE_SAMPLE_RATIO=1.0` multiplies trace egress ~10× — every request exports spans to Cloud Trace (more ingestion cost, more BatchSpanProcessor queue pressure on 256MB F2 instances, and a larger exfiltration surface for anything the sanitizing layers below might miss). Use it for short debugging windows only, then revert.

### PII controls in traces

Three layers in `utils/tracing_setup.py` keep PII out of exported spans:

1. **Instrumentation hooks.** The Flask request/response hooks overwrite `http.target` (and `http.url` when present) with the request path only, so query strings (e.g. client-name searches like `/parties/?q=Tremblay`) never persist on request spans. The `requests` hook rewrites outbound `http.url` to `scheme://host/path` — and for `*storage.googleapis.com` hosts keeps `scheme://host` only, because both the object path and the `name=` query param embed uid / dossier / filename.
2. **Sanitizing exporter.** `_SanitizingSpanExporter` wraps the OTLP exporter (and the dev console exporter). Before delegating, it strips query strings from URL-like attribute keys (`http.target`, `http.url`, `http.route`, `url.full`, `url.path`, `url.query`) and applies the same email / phone / postal regex scrub as the logging `RedactionFilter` (the patterns are imported from `logging_setup`, not duplicated) to every string attribute value. This is the defense-in-depth backstop for anything the hooks miss. **It works by replacing the private `ReadableSpan._attributes` slot** — the SDK exposes no public setter — so a failure there is caught and logged at **ERROR** (it was DEBUG until 2026-07-30, i.e. invisible under the production INFO root level, which is precisely how a leak would have gone unnoticed). The export proceeds regardless: tracing must never break the app.
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
| `mcp.tool.*` | One span per tool execution | `mcp.tool.get_agenda`, `mcp.tool.list_dossiers`; the write spans `mcp.tool.create_note` / `mcp.tool.append_to_note` carry `dossier_id` only — never the note title or content |
| `template.fill` | docx fill inside the generation POST (Phase H / H.2 / H.3) | `template.fill` with `template_id`, `field_count` (gabarits) or `invoice_id` + `rows_honoraire`/`rows_debours_tx`/`rows_debours_ntx` (note d'honoraires) or `note_id` (impression de note) — never values or content, counts and IDs only |
| `trust.transaction` | One trust write — create / reversal / inter-dossier transfer (Phase K) | `trust.transaction` with `direction`, `purpose`, `dossier_id` — **never amounts** |
| `trust.reconcile` | Reconciliation completion (Phase K) | `trust.reconcile` with `account_id`, `cleared_count` |
| `pallas.<module>.<qualname>` | Default name produced by the `@traced()` decorator | `models.dossier.create_dossier` |

### Standard attributes

| Attribute | Type | Set by | Purpose |
|---|---|---|---|
| `service.name` | string | resource | Always `pallas-athena` |
| `service.version` | string | resource | App Engine `GAE_VERSION` (or `local`) |
| `deployment.environment.name` | string | resource | `production` / `development` — the STABLE semconv key (the bare `deployment.environment` it replaced is deprecated upstream) |
| `service.instance.id` | string | resource | Random UUID per process, added automatically by the SDK since 1.43 — new cardinality on the resource, not PII |
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
| `dossier_id` | string | manual (`mcp.tool.*`, `trust.*`) | Set when the call carries a dossier — UUIDs only, never names/emails/token material |
| `account_id` | string | manual (`trust.*`) | Trust account UUID — ID only, never the account-holder name |
| `transaction_id` | string | manual (`trust.*`) | Trust transaction UUID |
| `reconciliation_id` | string | manual (`trust.*`) | Reconciliation run UUID |
| `cleared_count` | int | manual (`trust.reconcile`) | Entries cleared in a reconciliation |
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
- **`roles/cloudtrace.agent`** — push spans. The OTLP migration (2026-07-30) needed **no IAM change**: `roles/telemetry.tracesWriter` grants only `telemetry.traces.write`, which `cloudtrace.agent` already contains. The same holds for the portail service account (`portail-svc`). If spans stop arriving with `PERMISSION_DENIED`, this is the first assumption to re-verify.

Both APIs must stay enabled — `telemetry.googleapis.com` receives the spans, and disabling `cloudtrace.googleapis.com` makes Observability **discard** them silently (it is also what serves trace reads and the log-entry « View trace » link).

Verify with:

```bash
gcloud projects get-iam-policy athena-pallas \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:athena-pallas@appspot.gserviceaccount.com" \
  --format="value(bindings.role)"
```
