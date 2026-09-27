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

- Keys in `SENSITIVE_KEYS` are replaced with `"<redacted>"`. Matching is **exact whole-key membership** after `.lower()`, never a substring test — so `secret` is dropped while `client_secret` would not be. The full set (17): `authorization`, `cookie`, `set-cookie`, `session`, `password`, `password_hash`, `secret`, `secret_value`, `api_key`, `token`, `id_token`, `access_token`, `refresh_token`, `private_key`, `dav_password_hash`, `csrf_token`, `firebase_token`. When adding a field that could carry a credential, add its EXACT key here — a resemblance buys nothing.
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
| `password_changed` | INFO / WARNING | « Paramètres → Sécurité ». **Reported by the BROWSER**, not observed by the server: `firebase-admin` 7.4.0 has no MFA surface at all — not enrol, not unenrol, not even a count — so without this line the server would learn nothing of a password or second-factor change. Posted to `/parametres/securite/journal` (204, fire-and-forget, its own `30 per hour` bucket). The event vocabulary there is **closed**: an unknown name is a 400 and logs nothing |
| `mfa_enrolled` | INFO / WARNING | A second factor was added. Changing a number is **additive** — enrol the new one, confirm two, then remove the old — so this fires before `mfa_unenrolled` in a number change, never instead of it |
| `mfa_unenrolled` | INFO / WARNING | A factor was removed. Carries `factor_count_client`, clamped 0–10 and named `_client` on purpose: the provenance stays honest, because the server cannot verify it |
| `mfa_unenroll_failed_zero_factors` | WARNING (always) | The refusal that matters. Removing the LAST factor is an unrecoverable lockout — with `REQUIRE_MFA=true` the app refuses every session while `/parametres/securite` sits behind `@login_required`, so the application cannot repair its own lock. The guard lives in the JS FUNCTION, never in a `:disabled` attribute, which DevTools re-enables |
| `mfa_enroll_failed` | WARNING (always) | Enrolling a SECOND phone factor failed — either the SMS could not be sent or the assertion was refused. Fields: `error_code_client`, the Firebase code **as the browser saw it**, bounded by FORM (`^auth/[a-z0-9-]{1,48}$`) rather than by a closed list: the codes move with the SDK, and a closed list would silence exactly the unexpected code one wants to learn. Added 2026-09-13, after an enrolment failed with a code outside the page's 20-entry table and left **no trace anywhere** — the route was called on successes only, the catch discarded `err.code`, and the browser console was drowned in extension noise. That matters here more than elsewhere: this is the page whose mishandling can leave a zero-factor account |
| `reauth_failed` | WARNING (always) | Re-authentication before a privileged change (password, factor) did not succeed. Fields: `reason` from the closed allowlist — never the submitted value |

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
| `phase_reclassified` | A time entry's or disbursement's litigation phase was changed from the application (`routes/time_expenses`). Fields: `entity_type` ∈ `time_entry`\|`expense`, `entity_id`, `from_sous_phase`, `to_sous_phase`, `invoiced`. Emitted ONLY when the code actually changed — re-saving the same code writes nothing and logs nothing. **Codes and ids only**: never the description (which prints on the client's invoice) and never an amount. The connector's own path is covered by `mcp_write` / `mcp_phase_bulk` instead. `invoiced: true` is the interesting line — it is the one write the application allows past the billing freeze, and the phase is the only field it can touch |

### `log_dav_operation(operation, collection_type, *, dossier_id=None, object_count=None, duration_ms=None, status_code=None, ctag_bumped=None, **extra)` — logger `pallas.dav`

All emitted at INFO. Optional fields are omitted from the record when `None` so log-based metrics filtering on (e.g.) `ctag_bumped` don't pick up structurally-empty records.

| `operation` | Notes |
|---|---|
| `propfind` | Collection / resource discovery |
| `report` | `addressbook-multiget`, `calendar-multiget`, etc. |
| `get` | Single resource fetch |
| `put` | Create / update. A REFUSED create in a dossier / « Général » collection (`collection_type: dossier` — VTODO since lot 0b, VEVENT and VJOURNAL since lot 1a) or a vCard create (lot 0b B7, `collection_type: addressbook` — a CardDAV PUT whose URL name cannot be an id is refused before any read, create or update alike) carries `status_code` and `reason` ∈ `nom_invalide` (400 — the URL's resource name cannot become a document id: reserved, over-long, or holding a character a URI path segment cannot carry as-is — a space, `%`, `?`, `#`, an accent), `id_pris` (412 — `create()` found a document the read did not see, a racing PUT: refused, never overwritten), `id_autre_composant` (412 — another component already holds that id in the shared href space: a note or an event for a VTODO, a task or a note for a VEVENT, a task or an event for a VJOURNAL), `lecture_indisponible` (503 with `Retry-After`, `collection_type: dossier`, lot 1a — the PUT path's STRICT read failed, so whether the resource exists is unknown; before lot 1a the fail-open read called it absent and routed a phone EDIT into the create branch, where the hearing and note creators overwrote the stored document). Never the resource name itself: a client may name a resource after a UID that embeds an address. A vCard the MODEL refuses (422, or 503 when the mandataire reverse check could not run) logs a WARNING on the module logger `dav.carddav` carrying the error COUNT only — never the text, which can name a contact |
| `delete` | Resource removal. A REFUSED vCard delete (lot 0b review, `collection_type: addressbook`) carries `status_code` and `reason` ∈ `references` (409 — the contact is still linked to a dossier, or is the mandataire of another contact; the French reason travels in the response BODY only), `verification_indisponible` (503 + `Retry-After` — the reference check could not run), `ecriture_echouee` (500 — the store refused the delete; an `unexpected` « partie delete failed » ERROR rides beside it), `introuvable` (404 — the contact vanished between the route's read and the model's). Never the refusal's text: it NAMES the represented contacts, and until that review it was logged at ERROR on `dav.carddav` behind a 500 « Erreur serveur. » |
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
| `appcheck_disabled` | warning | **A CONFIGURATION event, not a request event** — `RECAPTCHA_ENTERPRISE_SITE_KEY` is unset in production, so App Check verification is fail-open. Fields: `reason="recaptcha_site_key_unset"`. Warns **once per process**: it describes a deployment. Note the ordering it depends on — the test sits ABOVE the `HX-Request` gate in `_verify_app_check` on purpose, because below it a freshly deployed instance nobody had clicked around never emitted it at all. It therefore also fires on App-Check-exempt paths, which is correct. Was a bare `current_app.logger.warning` until 2026-09-07, carrying no `jsonPayload.event` and so invisible to a log-based metric. |
| `origin_secret_disabled` | warning | Same class: `CF_ORIGIN_SECRET` is unset in production, so `_enforce_origin_secret` waves every request through and the App Engine firewall is the only remaining edge layer. Fields: `reason="cf_origin_secret_unset"`. Warns once per process. **Until 2026-09-07 this guard logged NOTHING** — `if not secret: return None`, no log, no metric, not even DEBUG. The August 2026 audit found the secret had never existed in Secret Manager, meaning the whole origin check had never run and `CF-Connecting-IP` was forgeable, and nothing had ever signalled it. This row exists so that cannot recur silently. |

> **These two are worth a log-based metric.** They are the only events in this registry that
> report a security control having *switched itself off*, and both are silent-by-default
> failure modes with a documented history. Filter on
> `logName="projects/<project>/logs/pallas-athena"` plus
> `jsonPayload.event=("appcheck_disabled" OR "origin_secret_disabled")` and alert on
> **count > 0** — there is no acceptable rate. Because both warn once per process and
> `min_instances: 0` recycles instances, expect at most a handful per day while a control is
> genuinely off, and exactly zero once it is configured.

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
| `mcp_tool_call` | success / failure | Tool executed; fields: `tool`, `duration_ms`, `dossier_id` (when the call carries one **in id form** — UUID-shaped, like the `mcp.tool.*` span attribute: a successful read can carry a pasted title too, since an unknown dossier yields an empty list, not a refusal. Raw until 2026-09-25) |
| `mcp_disabled_hit` | refused | Kill switch (`MCP_ENABLED=false`) returned 404 on a Phase-I route |
| `mcp_write` | success | Any write tool completed (the generalized audit line, July 2026 — one per `tools/call` on a member of `WRITE_TOOLS`); fields: `tool`, `dossier_id`, `entity_id`, `idempotent_replay` (the ONE flag meaning « nothing new was written »; `dry_run` sat beside it until 2026-08-27, when the preview left the write protocol — every `mcp_write` line is now a real call), `ctag_bumped`, `dav_synced` (distinct: a closed dossier bumps correctly but is never advertised to DavX5). **IDs and flags only — never a title, description or body.** Since August 2026 this also covers `complete_task`, the connector's ONE status change: an already-in-that-state call logs `idempotent_replay: false` with `ctag_bumped: false` — a real call that changed nothing, which is exactly what a no-op looks like here (the handler passes `wrote=False`, so no CTag is bumped). **On an idempotent replay `ctag_bumped` is logged `false`**: the replay hands back the FIRST call's stored payload — its `ctag_bumped: true` included — while bumping nothing, and until 2026-09-25 every replay logged a bump that never happened. `dav_synced` is left as stored: it describes the dossier's DavX5 visibility, not an action of this call. A call that COMMITTED and then failed emits no `mcp_write` at all — see `mcp_write_partial` |
| `mcp_note_written` | success | A write tool committed a note (Phase L; kept beside `mcp_write` for log-metric continuity — note writes emit BOTH); fields: `tool` (`create_note`/`append_to_note`), `dossier_id`, `note_id`, `content_chars`, `ctag_bumped`. **IDs and counts only — never the note title or body** (privileged work product; the `RedactionFilter` does not auto-scrub titles or free text). A replayed `idempotency_key` emits this line too — nothing was written; read the `mcp_write` beside it, whose `idempotent_replay` says so — and its `ctag_bumped` is then `false`, for the reason given under `mcp_write` |
| `mcp_write_refused` | refused | A write tool call was refused, and nothing was committed on its account (the write protocol's contract: a handler refuses BEFORE its write, and `run_write` RELEASES the idempotency claim of a refused call, so it leaves no entry behind. A refusal raised AFTER a model noted a commit is a handler bug and is NOT logged here: it becomes `mcp_write_partial`) — with ONE known residue: `record_document_analysis` appends its `analyses` journal entry BEFORE the document cache, so a cache write that fails leaves an inert journal entry behind the refusal. Fields: `tool` (when known) and `reason` — a machine-stable code, **never the refusal's text**, which quotes argument names the caller chose or describes user-supplied content and reaches the client only: `insufficient_scope` (the token lacks the tool's scope — including a write-time revalidation that found it revoked or expired, see `mcp_auth_failure`, AND one whose token LOOKUP failed: refused fail-closed, with an `unexpected` line naming the `tool` beside it — the token itself may be sound), `write_disabled` (`MCP_WRITE_ENABLED=false` — the MASTER switch: an accounting tool called while writes are off is refused under this reason too), `comptabilite_disabled` (`MCP_COMPTABILITE_ENABLED=false` with writes on: an accounting tool — scope `athena:comptabilite` — was called while its own switch is off. No tool carries that scope before plan lot 5, so until then this reason cannot occur), `schema_invalid` (the arguments failed the tool's input schema, or were not an object; `error_count` = the number of violations), `argument_refused` (the handler refused the call — the default reason of `ToolArgumentError`. ⚠ It is ALSO what a failed SAVE reads as: the models turn a Firestore write exception into a French error list (« Erreur lors de la sauvegarde… », after their own `log_unexpected`), which the handler raises like any other refusal. Such a line normally follows an `unexpected` ERROR in the same request (`request_id`); without one, it was the arguments that were refused), `idempotency_conflict` (an `idempotency_key` reused with different arguments), `idempotency_in_flight` (the key is CLAIMED by a call younger than the in-flight window — the platform's 10-minute request deadline plus 30 s of clock-skew margin — which may still be running — or which ENDED without its result being stored (a failed `finalize`, a `required` claim kept after a failure), which the refusal's wording allows for rather than promising a result; also emitted when the claim was lost to another same-key call on every attempt, beside an `mcp_idempotency_store_failure` with `op: claim`, `error_type: ClaimContention`: the caller is told to wait and retry with the SAME key, never a new one, which is how the race the claim closes would reopen), `idempotency_interrupted` (the key's claim is older than that window and was never finalized: the first call died between its claim and its record, and its write MAY have landed — the caller is told to re-read, and to use a new key only if nothing was written. Worth a look: it means a worker died mid-write), `idempotency_required` (a tool whose declared policy is `required` was called without a key — no tool declares that policy before Lot 5), `idempotency_store_unavailable` (policy `required` and the claim could not be established: refused fail-CLOSED, nothing executed, an `mcp_idempotency_store_failure` beside it), `stale_etag` (optimistic concurrency, plan rule 3: the record changed since it was read — the caller's `expected_etag` no longer matches, or, when the caller sent none, a write landed between the handler's own read and the model's transactional commit (`models.concurrency`). Nothing was written; the caller is told to re-read and retry, and the refusal names only WHEN the last write happened, never what changed nor by which path. A burst on one record is two writers racing on it — the phone and the connector, or two connector calls. Emitted by the edit tools that declare a `concurrency` policy of `optional`/`required`, AND — since the lot 0a critique — by the five writes that take no etag but rewrite what they read (`append_to_note`, `complete_task`, `complete_dossier`, `record_signification`, `record_prescription_event`: two parallel calls to one of them on the same record produce exactly one such refusal, where the second used to erase the first's write); a BULK reclassification reports a stale row inside its own result instead of refusing the call). A handler refusal also carries `dossier_id` when the call names one **in id form** (UUID-shaped): the argument is still the caller's string there — the schema bounds it to 64 characters and nothing more, and the commonest refusal is « dossier introuvable » — so a title or a name pasted into it is never logged. **Until 2026-09-25 only the first two reasons were ever logged**: schema and handler refusals left no trace at all, so the burst the Lot Q note below calls the stop-the-import signal could not be seen |
| `mcp_idempotency_store_failure` | failure | The idempotency store (`mcp_idempotency`) could not be read or written. Under the `optional` policy — every write tool today — the write went ahead regardless: the store fails OPEN (idempotency is retry armour, not an authorization gate). Under `required` it fails CLOSED: a claim that cannot be established refuses the call (`mcp_write_refused`, `idempotency_store_unavailable`). Fields: `tool`, `op` ∈ `lookup` (the claim's keyed read failed, or the stored entry could not be interpreted — `error_type: MalformedEntry` — so under `optional` the call ran UNCLAIMED: if it was in fact a retry, it duplicated) \| `claim` (the atomic `create()` of the pending claim — or the precondition-guarded delete of an expired entry — failed; same consequence) — EXCEPT `error_type: ClaimContention`, the entry kept changing for every attempt: that is another call live on the same key, not a store blip, so the call is REFUSED `idempotency_in_flight` under both policies and never runs unclaimed) \| `finalize` (the committed result could not be stored on the call's own claim: the entry STAYS `pending`, so a same-key retry is REFUSED as in flight, then as interrupted — never duplicated) \| `release` (a refused or failed call could not delete its own pending claim: same refusal of a same-key retry, on the safe side) \| `record` (a call that ran unclaimed could not store its result afterwards — an error on a write does not prove it did not land, a timeout can follow a commit — so a retry with the same key normally writes again) \| `record_partial` (a call that committed and then failed could not mark its entry `partial`: a same-key retry then reads it as in flight/interrupted rather than re-raising the « ENREGISTRÉE » message), `error_type` (the exception's class name — never its message, which can carry the stored result). Replaced (2026-09-25) two raw `logger.warning` lines that sat outside this registry and named no tool. A `lookup`/`claim`/`record` failure followed by a same-key `mcp_write` with `idempotent_replay: false` is a duplicated write to go and look at |
| `mcp_write_partial` | failure | A write COMMITTED, then a later step of the SAME call failed (a CTag bump, the re-read of a cascaded protocol step, the payload builder). The commit point is structural: the model mutator noted it (`models.provenance.note_commit`) and `run_write` read it, marked the idempotency entry `partial`, and raised `CommittedWriteError`; the client received an `isError` result reading « L'écriture est ENREGISTRÉE (…) mais une étape qui la suit a échoué. NE PAS RÉESSAYER : relisez l'élément » — never the retryable « internal error » that used to invite a duplicate. Fields: `tool`, `entity_id` (the first committed document), `collection` (its Firestore collection), `dossier_id` (a committed dossier, else the call's own — ids only when UUID-shaped), `rows` (distinct documents committed), `idempotent_replay` (true when a same-key retry re-raised the stored partial instead of executing). WARNING, because `log_mcp_event` cannot emit ERROR: the ERROR — with the traceback, which is the whole diagnosis — is the `unexpected` line « mcp write failed after its commit point » (`tool`, `commits`, `error_type`) that `run_write` logs beside it (no traceback when the late failure was a refusal, whose text describes user content). No `mcp_write` line accompanies it. **Ids and counts only.** Every one of these lines is a write to re-read: the record exists, and whatever followed it (the phone's copy, the cascade) may not |
| `mcp_phase_bulk` | success | A BULK phase reclassification ran (`set_time_entry_phase_bulk` / `set_expense_phase_bulk`). Fields: `entity_type`, `requested`, `applied`, `unchanged`, `refused` (`dry_run` was emitted here until 2026-08-27, when the preview left the write protocol). It exists because `mcp_write` fires for these calls with `entity_id: null` — honest, a batch has no single entity — which would otherwise leave a 50-row write with no measurable trace. **Counts only, never an id list.** A replayed `idempotency_key` short-circuits before the handler, so it emits no line here (the endpoint's `mcp_write` still does, flagged `idempotent_replay`) |
| `mcp_document_analysed` | A document analysis was RECORDED on the document (`record_document_analysis`). Fields: `document_id`, `sous_nature`, `niveau_protection`, `categorie_remplacee`. **Codes and ids only** — never the résumé, never an extracted party name, never a line of the document: the content is privileged, and the whole point of the tool is that it read it. `categorie_remplacee: true` is the interesting line — it means a stored classification was overwritten, and the previous one lives in the `analyses` journal |

> **Reclassement de phase (août 2026).** The four `set_*_phase` tools are members of `WRITE_TOOLS`, so `mcp_write` covers them by construction; the single-entry ones carry a real `entity_id` and `dossier_id`, the bulk ones do not (see `mcp_phase_bulk` above). None of them emits a CTag key: `timeentries` and `expenses` are not DAV-exposed, and this module never declares a sync that does not exist. Reading the pair together is the point — `mcp_write` says a batch ran (and whether it was a replay), and the `mcp_phase_bulk` line beside it says how much of it landed. Since the preview left the write protocol (2026-08-27) there is no flag distinguishing a proposal from a commit — every line is a real call — so `applied` against `unchanged` is what tells you whether the batch moved anything.
>
> **Disclosure (lot 0a, 2026-09-25).** No event was added: what the connector says it can do moved into `mcp/disclosure.py`, a text change. One refusal is new — `complete_task` putting a closed task (`terminée`/`annulée`) back `en_cours` is REFUSED (a reopen; plan lot 1 gives it its own tool) — and it logs as `mcp_write_refused` with `reason: argument_refused`, like every handler refusal: no write, no claim left behind, no CTag bump.
>
> **Lot Q (août 2026 — reprise de données historiques).** `mcp_write` covers the seven new write tools by WRITE_TOOLS membership; no new event was registered, because one audit line per write is the rule. Two readings to know: `create_partie` / `update_partie` log `dossier_id: null` (a contact belongs to no dossier — honest, not a defect), and they are the first writes whose `ctag_bumped` refers to the ADDRESSBOOK (`bump_ctag("parties")`) rather than a per-dossier collection. `import_invoice` logs `entity_id` = the invoice id and no CTag keys at all: invoices are not DAV-exposed, and this module never declares a sync that does not exist. **Still IDs, counts and flags only** — never an invoice number's narrative, a party's name, or a billing description, none of which the `RedactionFilter` auto-scrubs. A burst of `mcp_write_refused` during an import means Claude is mis-reading the source data: it is the signal to stop the batch, not to retry it. (If `unexpected` ERROR lines ride along with `argument_refused`, it is the STORE that is failing rather than the reading — see that reason above; stopping is right either way.)

> `mcp_consent` and `mcp_token_issued` also carry the granted `scope` string (and, on consent, `write_granted` and `comptabilite_granted` — each true only when its OWN box was ticked AND honoured: the accounting box is offered, and a tick on it honoured, only while `MCP_WRITE_ENABLED` and `MCP_COMPTABILITE_ENABLED` are both on and at least one tool carries the scope). A scope is not a credential — it is the only way to answer « pourquoi le connecteur ne peut-il pas écrire ? » after the fact.
> `mcp_auth_failure` gained one `reason`: `write_revalidation_failed` — a write tool re-read its token (bypassing the bearer success cache) and found it revoked, expired, or no longer carrying the tool's OWN scope (`athena:write`, or `athena:comptabilite` for an accounting tool — the write grant never stands in for it). Since 2026-09-25 that line carries `tool` — the write being revalidated — and so does the `mcp_write_refused` the endpoint emits after it; a revalidation whose token LOOKUP fails (the store unreachable — the write is refused, fail-closed) logs `unexpected` with `tool`.

### `log_settings_event(event, *, fields_changed=None, **extra)` — logger `pallas.settings`

INFO, except `cabinet_refused` and `integrations_refused` (WARNING). The firm profile (`settings/cabinet`) and the integration settings (`settings/integrations`) are edited from « Paramètres ». **Pass field NAMES only, never their values** — the `RedactionFilter` scrubs emails and phone numbers but NOT names of people, and `nom` is one. Compose the list with `models.settings.changed_field_names`.

| `event` | Notes |
|---|---|
| `cabinet_updated` | `fields_changed` (sorted field NAMES) + `fields_changed_count`. A save silently changes the letterhead of every generated document, the tax numbers snapshotted onto every FUTURE invoice, and the fax on every procedure — `updated_at` says when, never what |
| `cabinet_refused` | WARNING; `error_count`. Validation refused the save (missing `nom`, invalid phone/courriel/postal code) — never the submitted value |
| `integrations_updated` | `fields_changed` (sorted field NAMES) + `fields_changed_count`, over `settings/integrations`. Same rule, plus one reason of its own: a mis-set subject keyword stops the Bookings sync **silently**, and worse, an EMPTY keyword set makes the absence loop flag already-imported reservations `annulée_client`. « When did these fields last move? » is therefore the first question when diagnosing « nothing comes in any more » |
| `integrations_refused` | WARNING; `error_count`. The store refused — an empty keyword set, a judicial `hearing_type` in the keyword→type map, a day count outside its bounds. Never the submitted value |

### `log_template_event(event, *, template_id=None, dossier_id=None, **extra)` — logger `pallas.templates`

INFO, except `generation_failed` (WARNING). **Never pass field values** (client PII) — placeholder names, counts and IDs only; the `RedactionFilter` is a backstop, not the policy.

| `event` | Notes |
|---|---|
| `template_uploaded` | New gabarit; `template_id`, `placeholder_count`, `warning_count` (split-run suspects) |
| `template_updated` | Metadata edit or file replacement; `file_replaced: bool`, `version` |
| `template_deleted` | Gabarit + Storage object removed |
| `document_generated` | `template_id`, `dossier_id` (when saved), `saved_document_id` (when saved), `field_count`, `missing_count` (blanks replaced by the visible French fallback). **Note d'honoraires (Phase H.2):** adds `invoice_id`, `source="facture"`, and the three row counts `rows_honoraire` / `rows_debours_tx` / `rows_debours_ntx` (instead of `field_count`/`missing_count`). **Impression de note (Phase H.3):** adds `note_id`, `source="note"`, `field_count` — never the note's title or content |
| `generation_failed` | WARNING; `reason` machine-stable (`template_not_found`, `template_file_unavailable`, `template_invalid`, `fill_error`, `save_failed`; Phase H.2 adds `no_note_template`, `invoice_voided`, `unbalanced_condition`; Phase H.3 adds `no_note_print_template`; Sept. 2026 adds `manual_option_invalid` — carries the FIELD NAME only, never the submitted value) — never a filename or field value |

### `log_trust_event(event, outcome='success', *, transaction_id=None, dossier_id=None, account_id=None, reconciliation_id=None, reason=None, **extra)` — logger `pallas.trust`

Trust accounting (« comptabilité en fidéicommis », Phase K). `outcome` ∈ `{"success", "refused"}` — `success` emits at INFO, `refused` at WARNING. Optional fields are omitted when `None`. `reason` is a short machine-stable string (`"insufficient_cleared_balance"`, or a §5 abort string such as `antidatage_refusé` / `facture_non_émise`) — **never** an account-holder name, client string, or dollar amount. The `RedactionFilter` does NOT auto-scrub names or amounts (only emails/phones/postal/court-file), so keep them out of the fields entirely. `variance_cents` (on `trust_reconciliation_variance`) is the single number ever logged — a control failure with no client attached, useless without it.

| `event` | Typical outcome | Notes |
|---|---|---|
| `trust_transaction_created` | success | An écriture (or an inter-dossier transfer leg) was appended; `transaction_id`, `account_id`, `dossier_id`, `direction`, `purpose`, `sequence` |
| `trust_transaction_cleared` | success | An entry moved `en_circulation` → `compensée`; `transaction_id` |
| `trust_transaction_reversed` | success | A contre-passation was recorded; `transaction_id` (the reversal), `reverses_id`, `annulled: bool` (true when both entries became `annulée`). Reversing a leg of a two-leg inter-dossier transfer reverses BOTH legs in one transaction (lot 0b) and emits this event ONCE PER LEG |
| `trust_overdraft_refused` | refused | The cleared-funds control blocked a déboursé; `dossier_id`, `account_id`, `reason` = `insufficient_cleared_balance`. **The module's most important WARNING.** |
| `trust_transaction_refused` | refused | Any other create abort — the in-transaction guards AND, since lot 0b (2026-09-26), the read-free prechecks, which used to refuse without a trace; `reason` = a §5 abort string (`compte_fermé`, `client_hors_dossier`, `antidatage_refusé`, `facture_non_émise`, `date_future`, `objet_invalide`, …). `account_id`/`dossier_id` are the ids the caller submitted (omitted when blank). Since the same lot, a refused clear / reversal / inter-dossier transfer logs here too — its read-free refusals included (`motif_requis`; `transfert_identique`, `montant_invalide`, `mode_invalide`) — with `operation` ∈ `clear`·`reverse`·`transfer` (absent = create) and `transaction_id` (clear, reverse) or `count` (bulk clear); the lock-floor reasons are `période_verrouillée` (`période_verrouillée_jour` when the floor IS today — no date is open before tomorrow), `compensation_période_verrouillée`, `contre_passation_période_verrouillée`, `virement_période_verrouillée` |
| `trust_reconciliation_completed` | success | A reconciliation was balanced and completed; `reconciliation_id`, `account_id`, `cleared_count` |
| `trust_reconciliation_variance` | refused | Completion refused because the variance was non-zero; `reconciliation_id`, `account_id`, `variance_cents` |
| `trust_reconciliation_abandoned` | success | A DRAFT reconciliation was deleted (« Abandonner » — a brouillon never mutated any transaction); `reconciliation_id`, `account_id` |
| `trust_export` | success | Journal / carte-client CSV or PDF export; `format`, `view`, `row_count` |

### `log_admin_ledger_event(event, outcome='success', *, transaction_id=None, account_id=None, invoice_id=None, reconciliation_id=None, reason=None, **extra)` — logger `pallas.admin_ledger`

Administration accounting (« comptabilité d'administration », August 2026) — the operations-account / corporate-card sibling of `pallas.trust`. `outcome` ∈ `{"success", "refused", "failure"}` → INFO / WARNING / WARNING. Same PII discipline as trust: `reason` is a machine-stable abort string, **never** a supplier name, a description, or a dollar amount (`variance_cents` on `admin_reconciliation_variance` keeps the trust exception).

| `event` | Typical outcome | Notes |
|---|---|---|
| `admin_transaction_created` | success | An entry was appended; `transaction_id`, `account_id`, `invoice_id` (encaissement), `direction`, `kind`, `sequence` |
| `admin_transaction_updated` | success | An UNLOCKED entry was edited in place (the deliberate divergence from trust); `transaction_id`, `fields` (names only) — the change itself lands in the entry's on-document `revisions` trail |
| `admin_transaction_deleted` | success | An unlocked entry (or both card-payment legs) was hard-deleted; the route records it in `audit_events` (`entity_type="admin_transaction"`) |
| `admin_transaction_cleared` | success | `en_circulation` → `compensée`; `transaction_id` |
| `admin_transaction_reversed` | success | A contre-passation was recorded; `transaction_id` (the reversal), `reverses_id` |
| `admin_transaction_refused` | refused | Any create/update abort — the no-read guards included since lot 0b (a create refused before any read used to leave no line); `reason` = an abort string (`période_verrouillée`, `date_future`, `ventilation_invalide`, `sens_incohérent` — a direction that contradicts the kind, which the model now DERIVES —, `lien_fideicommis_réservé` — a `trust_transaction_id` inside the data dict, which only the trust fee payment and the reprise script may set, as a keyword —, `encaissement_carte_interdit`, `encaissement_excède_solde`, …) |
| `admin_card_payment_created` | success | Two linked legs (bank déboursé + card recette); `transaction_id` (bank leg), `account_id`, `card_account_id` |
| `admin_receipt_attached` | success | A pièce justificative's metadata was set; `transaction_id`, `size` — never the filename |
| `admin_invoice_payment_projected` | success ou **failure** | The Lot P projection: an encaissement recorded (or a reversal reduced — `reduced: true`) the payment on its invoice via `record_payment`; `failure` means the ENTRY STANDS and the UI showed the correction banner. **Since 2026-08-17 this is the ONLY event a REQUEST-SERVED payment can leave**: the invoice's own payment form was removed, so the accounting module is the single writer — an `amount_paid` that moved without this event, during normal operation, means someone wrote outside the module. **The two migration scripts are the named exception**: `purge_encaissements_factures` and `reprise_encaissements` call `record_payment` directly and emit nothing, so a batch of payments clearing or appearing with no event is the expected trace of a hand-run reprise, not an intrusion |
| `admin_reconciliation_completed` | success | Variance 0, entries stamped, **the period is now LOCKED**; `reconciliation_id`, `account_id`, `cleared_count` |
| `admin_reconciliation_variance` | refused | Completion refused; `variance_cents` |
| `admin_reconciliation_abandoned` | success | A draft was deleted |
| `admin_export` | success | Journal CSV or PDF; `format`, `account_id`, `row_count` |

### `log_invoice_event(event, invoice_id, *, outcome='success', reason=None, **extra)` — logger `pallas.invoice`

Invoice lifecycle (lot 0b, 2026-09-26) — emitted by `models/invoice.py` itself, so every surface that changes a status or voids an invoice leaves the same trace. Before this family those two operations — the ones that decide whether an invoice's hours can be billed again — left **no log line at all**. `outcome` ∈ `{"success", "refused"}` → INFO / WARNING. `reason` is a machine-stable code, **never** the French refusal (it can quote an amount); never an invoice number, an amount, or a client name — the `RedactionFilter` scrubs neither.

| `event` | Typical outcome | Notes |
|---|---|---|
| `invoice_status_changed` | success | `update_status` committed; `invoice_id`, `from_status`, `to_status` |
| `invoice_voided` | success | `void_invoice` committed; `invoice_id`, `dossier_id`, `released_count`, `foreign_count` (sources that name ANOTHER invoice — billed again since, left untouched: a non-zero value is the trace of the read-then-set race on the time-entry form, worth investigating), `missing_count` (line-item sources whose document no longer exists) |
| `invoice_refused` | refused | `operation` ∈ `status` \| `void`; `reason` ∈ `annulation_par_statut` (« annulée » asked of `update_status`), `transition_refusee`, `stale_etag`, `introuvable`, `deja_annulee`, `payee`, `paiement_fideicommis`, `paiement_inscrit`, `encaissement_administration`, `lignes_illisibles` |

### `log_protocol_event(event, protocol_id, *, outcome='success', reason=None, **extra)` — logger `pallas.protocol`

Protocols, their steps and the task cascade they drive (lot 0b, 2026-09-26; creation, closure and reopening since lot 1a) — emitted by `models/protocol.py` itself, so every caller (web route, the task cascade, the connector from lot 1b) leaves the same trace. A step status change rewrites a DAV-exposed task behind the caller's back, and before this family neither the change nor a cascade that deliberately LEFT the task alone left any line. `outcome` ∈ `{"success", "refused"}` → INFO / WARNING. **IDs, statuses and machine reasons only** — never a step or task title (it names the case, and the `RedactionFilter` scrubs neither names nor free text). A store failure of the step write itself, cascade exceptions and a failed CTag bump stay `unexpected` ERRORs (`protocol step status write failed`, `protocol cascade: task status sync failed`, `protocol cascade: task CTag bump failed`, `protocol cascade: completion check failed`, `protocol one-actif check failed` — the fail-closed read of the one-actif rule raised, and the create or reopen was refused —, `protocol write failed`, `protocol linked task creation failed`, `protocol linked task CTag bump failed`, `protocol linked task: step link failed` — an ORPHAN task: created, never linked).

| `event` | Typical outcome | Notes |
|---|---|---|
| `step_status_set` | success | `set_step_status` committed a change; `protocol_id`, `step_id`, `from_status`, `to_status`, `task_sync` (`synced` / `noop` / `skipped_cancelled` / `skipped` / `missing` / `failed` / `none`), `protocol_closed`, `protocol_reopened` (a reopened step reactivated a protocol the cascade had closed — lot 1a). A same-state request writes nothing and emits nothing |
| `step_status_refused` | refused | `reason` ∈ `statut_invalide`, `protocole_introuvable`, `etape_introuvable`, `stale_etag` (the caller's `expected_etag` is no longer the step's), `protocole_non_actif` (the protocol is suspended, or completed by a deliberate change or before `closed_by` existed — only an auto-closed one reopens by itself), `autre_protocole_actif` (reopening would reactivate the protocol while another one is actif in the dossier), `lecture_impossible` (the one-actif check could not read — fail closed); `step_id`. Also emitted when the TASK cascade asks a step to follow its task and the step refuses: the task has committed, the step has not, and this line is the trace |
| `protocol_created` | success | `create_protocol` committed the protocol and its template steps in one transaction; `dossier_id`, `protocol_type`, `step_count` |
| `protocol_refused` | refused | `operation` ∈ `create` (lot 1a; `update` joins with the model's update rules); `reason` ∈ `champ_refuse` (a key outside the model's whitelist), `validation`, `regime` (the C.p.c. template does not govern the dossier's court), `protocole_actif_existant` (the one-actif rule), `lecture_impossible` (that rule's read failed — refused, never « none found »); `dossier_id`. `protocol_id` is `""` on a refused creation |
| `linked_tasks_created` | success ou refused | `create_linked_tasks` finished; `created`, `linked`, `failed` counts. `refused` (WARNING) when a task failed to be created or created-but-unlinked (an orphan — see the `unexpected` messages above); each step is its own attempt, so the others still get their task |
| `task_move_refused` | refused | `models/task.update_task` refused to move a task to another dossier because a protocol step links it (`reason=tache_liee`); `protocol_id`, `task_id`, `step_id`. Web form, jtx move (DAV PUT → 422) and connector alike. When the link lookup itself cannot read, the move is refused too and the trace is the `unexpected` ERROR `task move: step link lookup failed` (task_id only) |
| `cascade_task_skipped` | success ou refused | The linked task was NOT carried along. `success` + `reason` ∈ `tache_annulee` (a cancelled task is never un-cancelled nor turned « terminée »), `tache_terminee` (an `en_cours` step never downgrades a done task) — deliberate, the step and the task now disagree on purpose. `refused` + `reason` ∈ `tache_introuvable` (the link points at no task — or `get_task` failed open), `ecriture_refusee` (the task write returned errors), `concurrence` (two compare-and-set conflicts in a row) — the step committed and its task is out of step: the lawyer is told on the page. `task_id`, `step_status`, `task_status` |

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
| `versement_divergence` | **failure** | The live quarantine blob no longer matches the manifest at « Verser » time — `reason` = `taille` (blob grew past 200 Mo since hashing; refused BEFORE any read) or `sha512` (the stream-computed hash ≠ the manifest fingerprint the accusé attested; 8 MiB slices — the copy-based versement never holds the object in RAM). Integrity anomaly, deliberately ERROR: the reviewed file is not the file about to enter the dossier. IDs only (`invitation_id`, `batch`, `seq`) — never a filename |
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
| `bookings_sync_execute` | success | Cron sweep done; counters `vus`, `detectes`, `crees`, `modifies`, `annules`, `divergences`, plus the flag `annulations_desarmees` — true when the absence-cancellation loop was DISARMED because the integration settings came from the deploy-time fallback rather than the store (see `reglages_replies` below). One line therefore tells the whole cycle |
| `bookings_sync_erreur_graph` | refused ou **failure** | `refused` + `reason="not_configured"` (Graph creds / mailbox absent — fail-open, no-op); `failure` + `reason="graph_error"` (a Graph outage — the cycle was missed, the next 10-min run retries); `failure` + `reason="aucun_mot_cle"` (`BOOKINGS_SUBJECT_KEYWORDS` empty — the predicate can never match, so the sync imports NOTHING and the absence loop would flag every already-imported reservation `annulée_client`. The route refuses to run and answers **200** — deliberately, so a cron retry storm cannot follow. ⚠ This guard sits BEFORE `bookings_configured()`, so on a doubly-unconfigured deployment the reason you see is this one, not `not_configured`. The event NAME says « erreur Graph » but this condition never touches Graph); `failure` + `reason="reglages_replies"` (the `settings/integrations` store was UNREADABLE, so the sync ran on the deploy-time values and **disarmed the absence-cancellation loop**. Creation stays armed — the `fenetre_pleine` precedent. Emitted every 10-minute cycle for as long as the store is unreadable, deliberately: the fallback is safe but it is not what the lawyer configured, so a keyword added in the app is silently not matched until it clears) |
| `bookings_mot_cle_non_mappe` | refused | A detected keyword has no entry in `bookings_type_par_mot_cle`, so the import falls back to `bookings_type_defaut`; fields `mot_cle`, `type_defaut`. Legitimate (mapping every service is not required) but it became PROBABLE when the map became editable, which is why it stopped being a bare `logger.warning` invisible to the `pallas.bookings` stream. ⚠ The keyword is a SERVICE NAME the lawyer chose — configuration, never client data; the meeting SUBJECT embeds the client's name and is logged nowhere |
| `reception_rdv_confirme` | success | A rendez-vous was confirmed in Réception; `hearing_id`, `partie_liee: bool` — the event now enters DAV/Calendar (CTag bumped) |
| `reception_rdv_refuse` | success ou refused | A rendez-vous was refused; `hearing_id`, `graph_annule: bool`. `refused` + `reason="graph_error"` when the Outlook cancellation failed (the Athéna refusal still stands — the juriste is told to cancel manually) |
| `reception_rdv_divergence_traitee` | success | A `bookings_divergence` alert was applied/ignored/cancelled; `hearing_id`, `action` |
| `miroir_outlook_execute` | success | Outlook-mirror cron sweep done; counters `vus`, `miroirs`, `crees`, `corriges`, `supprimes`, `ignores`, `erreurs` (per-event Graph failures — the sweep continues), `restants` (mutations deferred past the per-sweep cap `_PLAFOND_MUTATIONS` — the next 10-min cycle resumes; a `restants` that never drops to 0 across cycles means the backlog is not converging) |
| `miroir_outlook_erreur_graph` | refused ou **failure** | `refused` + `reason="not_configured"` (fail-open, no-op); `failure` + `reason="graph_error"` (Graph outage, cycle missed) or `reason="fenetre_pleine"` (the 1500-hearing fetch window is full: the desired set is truncated, so the DELETE phase is disarmed until `_LIMITE_FENETRE` is raised — loud, never silent) or `reason="lecture_firestore"` (the Athéna read FAILED: an empty list is indistinguishable from "nothing matched", and a caller that conflated them would treat every mirror as an orphan, so the sweep abstains entirely) |

### `log_hearing_series_event(event, serie_id, **extra)` — logger `pallas.hearing`

Recurring calendar series (« séries »). One click here creates or destroys up to 60 documents, mints as many DAV tombstones and pushes as many VEVENTs to the phone — before this family, `routes/hearings.py` emitted **no log line at all**, so the Calendrier's most consequential operation left no trace. Always INFO.

**IDs and COUNTS only — never the title.** The `RedactionFilter` scrubs emails, phones, postal codes and court file numbers, but NOT names or free text, and a hearing title routinely carries a client's name.

| `event` | Notes |
|---|---|
| `series_created` | A series was materialised; `serie_id`, `occurrences` (count), `dossier_id`, `frequence`, `ctag_bumped` (always true — the bump rides inside the write batch) |
| `series_deleted` | A chain was deleted from an occurrence onward; `serie_id`, `occurrences` (how many were ACTUALLY destroyed, never how many were asked for), `dossier_id`, `ctag_bumped`. The deletion journal takes ONE `audit_events` row per chain — see the entry in CLAUDE.md for why N rows would evict the practice's whole deletion history |
| `series_unlinked` | One occurrence was detached and became standalone; `serie_id` (the chain it LEFT), `hearing_id`, `dossier_id`, `ctag_bumped` |

### `log_chat_event` — RETIRÉ le 2026-09-02

Le logger `pallas.chat` et ses 24 événements ont été retirés avec le client
de clavardage interne (Phase N, 2026-08-26 → 2026-09-02) : le cabinet est
passé à un compte Claude for Work couvert par une entente de traitement des
données, ce qui retire au clavardage sa seule raison d'être. Les journaux
déjà émis restent dans Cloud Logging sous `logName=pallas-athena` et
`resource.labels.module_id="chat"` jusqu'à l'expiration de leur rétention ;
une alerte log-based posée sur `chat_charter_repli` ne se déclenchera plus et
peut être retirée.

### `log_unexpected(message, *, exc_info=True, **extra)` — logger `pallas.unexpected`

Always emitted at ERROR with traceback. This is what `main.py`'s `errorhandler(Exception)` calls — it surfaces to Cloud Error Reporting via the `pallas-athena` log. The traceback text is PII-scrubbed by `RedactionFilter` before emission (see "PII redaction policy" above for the Error Reporting grouping trade-off).

Message added in lot 1a (2026-09-27, the review of step L1), a READ whose failure is answered rather than guessed: « dav put strict read failed » (`component` ∈ `VTODO`/`VJOURNAL`/`VEVENT` — the component the PUT carries —, `check` ∈ `existing` (the read that decides edit vs create) / `other_component` (the create's check that no other component holds the id), `dossier_id` — never the resource name). The dossier-collection PUT could not tell whether the resource exists and answered **503** (`Retry-After`), beside the INFO `dav_operation` put line with `reason: lecture_indisponible`. A burst is a Firestore outage; a steady stream on one component is a stored document the read path cannot migrate — every phone edit of it will keep answering 503 until it is repaired. Before lot 1a the fail-open `get_*` read called it absent (a WARNING) and routed a phone EDIT into the create branch.

Messages added in lot 0b B7 (2026-09-27), each a DEGRADED path that keeps working: « dav tombstone token read failed » (`collection` — `dav.sync` could not read a collection's token to stamp a tombstone; the tombstone is written anyway, stamped `""`, and the stored token is NOT reset — before, the same blip rewrote it and forced every DAV client into a full resync); « partie mandataire reference check failed » (the reverse mandataire scan of `update_partie` could not run: the role/type change is refused, fail closed — CardDAV answers 503); « list_prescription_alerts: derivation failed » (one dossier `derive_prescription` cannot read: alerted `a_verifier` on its raw date instead of emptying the whole alert list)); « list_prescription_alerts: row migration failed » (`dossier_id` — one dossier the party migration `_migrate_parties` cannot read, typically a legacy client entry with no `id`: alerted as stored instead of dropping every alert of its status, as it did until the lot's completeness review); « list_prescription_alerts: status query failed » (`status` — one status query of the alert list could not run: the other status still alerts. It was a WARNING carrying the exception text; a limitation-deadline list silently missing a status belongs in Error Reporting).

Messages added earlier in lot 0b (B5/B6, 2026-09-26/27), each a READ whose failure is answered rather than guessed: « trust list_invoice_fee_payments failed » (the invoice page could not read the trust fee payments that name the invoice — display only, fails OPEN: « Annuler » may stay offered, and the void, which re-reads everything in its own transaction, refuses); « trust: cash receipt lookup failed » (`routes/trust` could not resolve the art. 72 cash receipt the lawyer named by its register number — the entry is refused with « Veuillez réessayer », never recorded without the check); « budget version read failed » (`dossier_id` — `models/budget.create_budget` could not read the existing versions: the save is REFUSED rather than minting a duplicate version 1 that would sort below the real latest and never show). The ordinary write/read failures of the lot's model paths (« invoice status update failed », « invoice void failed », « create_invoice: transaction failed », « create_invoice: invoice number seeding failed », « protocol step status write failed ») follow the house rule of every « … write failed » line: the operation failed, nothing was written, the user saw « Veuillez réessayer ».

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
| `admin.transaction` | One administration write — create / reversal / card payment (August 2026) | `admin.transaction` with `direction`, `kind` — **never amounts, never supplier names** |
| `admin.reconcile` | Administration reconciliation completion | `admin.reconcile` with `account_id`, `cleared_count` |
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
