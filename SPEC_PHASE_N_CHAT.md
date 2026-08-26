# SPEC — Phase N : Client de clavardage IA (Claude sur Vertex)

Target: Claude Code. Read `CLAUDE.md` first — this spec assumes every convention in it
(vendored assets, no new Python dependencies in the monolith, HTMX + Alpine + Jinja2,
Firestore conventions, `date_str` for date-only MCP output, ASCII-only codes, Cloudflare
firewall posture, Cloud Tasks `0.1.0.2` allow rule). Where this spec and `CLAUDE.md`
conflict, **this spec wins for the `chat_*` collections and routes only**.

Status: **implemented (2026-08-26)** — the decisions below were taken during the
implementation review and OVERRIDE the flagged defaults where they differ.
CLAUDE.md's Phase-N history entry is the canonical record.

> **DÉCISIONS D1–D10 (Jason, 2026-08-26), consignées :**
> **D1** worker = 3e service App Engine « chat » (SA par défaut, gunicorn 570 s,
> file « chat-turns ») — l'exécution §2 sur le service default contredisait son
> propre gunicorn --timeout 60. **D2** lecture des pièces = pypdf (dépendance
> NOUVELLE, la dérogation assumée au « zero new dependencies » de ce spec) +
> repli bloc « document » natif pour les pages numérisées. **D3** les nouveaux
> outils sont exposés sur le chat ET le connecteur claude.ai, get_document_text
> inclus (confirmé explicitement : le contenu intégral des pièces transite alors
> par claude.ai — l'écran de consentement le dit). **D4** brouillons =
> collection `chat_drafts` versionnée + « Verser en Word » (pipeline H.3).
> **D5** Workers legislation/jurisprudence = REST simple à jeton (specs fournis
> en cours de route ; stub à liaison tardive d'ici là). **D6** modèles =
> Sonnet 5 + Opus 5 (classe de rétention à vérifier au Model Garden ; repli
> documenté Opus 4.8). **D7** seeds (skills + tâches planifiées) créés via
> l'UI après déploiement. **D8** + get_draft/list_drafts : TOOLS 47 → 52.
> **D9** toolset d'écriture du chat = parité complète avec le connecteur
> (dérivée de WRITE_TOOLS). **D10** deux jetons Workers distincts.
>
> **CORRECTION à §4.4 :** la « Phase K extraction pipeline » citée plus bas
> N'A JAMAIS EXISTÉ dans ce dépôt — Phase K y est le fidéicommis. Le socle
> réel est `models/document.get_document_bytes` (nouveau seam borné à 40 Mo)
> + `utils/pdf_text.py` (pypdf). La carte des lettres du FLAG ci-dessous était
> également fausse (la réservation est L2) ; la lettre N, elle, tient.

> **FLAG — phase letter.** "N" assumes L = portail client and M = service de réservation.
> If the booking service was assigned a different letter, renumber this file accordingly.

This spec is deliberately **high-level**: it fixes architecture, contracts, and invariants,
and leaves implementation detail to Claude Code within existing project conventions. Every
deliberate divergence from `CLAUDE.md` is called out. Interpretation defaults are marked
**FLAG** and may be overridden by Jason in one line each.

---

## 1. Purpose and scope

A single-user chat client, inside Pallas Athéna, wired to Anthropic Claude models on
**Google Vertex AI**. It exists so that privileged material never transits a consumer
AI product: inference runs under the firm's GCP project, the full exchange is recorded
in the firm's own append-only registre, and every tool action is attributed and versioned.

**In scope (v1):**

- Conversations with Claude Sonnet 5 (quotidien / administration) and Claude Opus 4.8
  (recherche / rédaction), selectable per conversation.
- Extended thinking enabled; the thinking text is preserved and displayed.
- Agentic tool loop: internal Athéna tools called **in-process**, the two legal-research
  Cloudflare Workers (`legislation`, `jurisprudence`) called over HTTP, and Anthropic's
  native `web_search` tool for doctrine.
- Read, write, and modify capabilities. **No delete capability exists anywhere.** All
  writes are versioned and carry provenance.
- Runtime-manageable skills, selectable per conversation.
- Conversations attached to a dossier, or floating.
- Per-turn token accounting, aggregated per conversation and per dossier.
- Append-only conversation registre in Firestore; conversations are resumable.
- **Scheduled tasks**: recurring unattended runs (successors to the three scheduled
  tasks currently configured in the Claude.ai project), almost always floating.

**Out of scope (v1):**

- Streaming (SSE/WebSocket) — deliberately excluded; see §2.
- Any delete route, tool, or UI affordance.
- Fable-class / Mythos-class models (Covered Models: mandatory 30-day retention under the
  Advanced AI Safety Addendum — disqualifying). The model allowlist is closed; see §9.
- Automatic billing of AI costs to client invoices (accounting is captured; posting a
  débours remains a human act — FLAG, revisit in a later phase).
- Context compaction / summarization of long histories (v2; v1 sends full history with
  prompt caching).
- File attachment upload in the composer. Pièces are read **via MCP tools only** (§4.4).

---

## 2. Execution model — the load-bearing decision

**"No streaming" does not mean "synchronous."** An Opus turn with extended thinking and
several tool calls runs for minutes; the gunicorn `--timeout 60` and App Engine request
limits forbid holding the user's HTTP request open. The turn therefore executes as an
**asynchronous chain of Cloud Tasks**, and the browser polls.

### 2.1 Turn lifecycle

1. `POST /chat/<conversation_id>/message` — validates, appends the user turn to the
   registre (§6), creates an assistant turn in state `pending`, enqueues the first task
   on a dedicated queue (`chat-turns`), returns `202` with the turn id. The request
   returns in milliseconds.
2. **Worker task = at most one Vertex model call.** The task loads conversation state
   from Firestore, assembles the request (§3), calls Vertex. On `stop_reason: tool_use`,
   it executes the requested tools (§4), appends the assistant content blocks and tool
   results to the turn record, and **enqueues the continuation task**. On terminal stop,
   it finalizes the turn (markdown rendered server-side, usage recorded, counters updated).
3. The browser polls a fragment endpoint (HTMX, `every 2s`) that renders the turn's
   current state: `réflexion…` / `outil : <nom>` / `rédaction…` / final content. Polling
   stops on terminal state (HTTP 286 or equivalent convention).

### 2.2 Invariants

- **One model call per task.** The agentic loop is unbounded across the chain, but each
  task stays far inside worker limits. A hard ceiling on chain length per turn
  (configurable, e.g. 12 model calls) prevents runaway loops; hitting it finalizes the
  turn with an explicit error state, never silently.
- **Idempotency.** Cloud Tasks delivers at-least-once. Each step carries a step token;
  the worker advances state inside a Firestore transaction guarded by that token. A
  duplicate delivery observes the advanced state and exits without calling Vertex.
- **Durability.** The turn completes and is recorded even if the browser disconnects.
  Tokens spent are tokens recorded, always.
- **Turn states.** `pending → running → awaiting_authorization → final | failed`.
  The `awaiting_authorization` state serves tool gating (§4.6); the v1 registry gates
  no tool, but the state machine implements the state from day one.
- **Failure is loud.** Retry exhaustion marks the turn `failed` with the error surfaced
  in the transcript. Recovery is a new user turn; nothing is retried invisibly.
- **Firewall.** The `chat-turns` queue targets App Engine; requests arrive from
  `0.1.0.2`. The existing allow rule (Phase J piège) must cover the new handler route —
  verify, do not assume.

### 2.3 Consequence for rendering

No streaming means **no client-side markdown**. Finalized assistant text passes through
the existing server-side `markdown` + `bleach` pipeline, exactly like notes. Nothing new
is vendored. Model output is markdown only, by charter (§8).

---

## 3. Vertex integration

- Endpoint: Vertex AI, **multi-region `us`** (FLAG — global is ~10 % cheaper but routes
  worldwide; the default preserves the residency posture already accepted).
- Request shape: standard Messages API body; the model goes in the URL;
  `anthropic_version: "vertex-2023-10-16"` in the body.
- Auth: Application Default Credentials of the App Engine service account. Grant the
  Vertex AI User role; **no API key exists anywhere**.
- Dependencies: `requests` + `google-auth` (already pinned). **Zero new dependencies**
  in the monolith — this is why no Anthropic SDK is used.
- Extended thinking: enabled on both models with per-model default budgets in config.
  **Thinking blocks (including signatures) are stored verbatim and passed back unmodified
  on every continuation** — both within a tool-use chain and when a conversation is
  resumed. This is an API correctness requirement, not a style choice.
- Prompt caching: enabled. Prompt assembly order is stable — system charter, then
  selected skills, then tool schemas, then history — with a `cache_control` breakpoint
  after the stable prefix. (Caching is a transient server-side KV cache measured in
  minutes; it is distinct from data retention and stays on.)
- Vertex request-response logging (BigQuery) stays **OFF**. The registre (§6) is the
  record; no second copy.

---

## 4. Tool surface

### 4.1 Registry

One registry maps tool name → `{schema, executor, capability}`.

| Executor | Tools | Transport |
|---|---|---|
| `in-process` | All existing Athéna read tools + new write/modify tools | Direct calls into `mcp/handlers.py`; `mcp/tools.py` remains the schema source of truth |
| `http-worker` | `legislation` and `jurisprudence` Workers | HTTPS with a service credential (path token / header); no OAuth dance — this is server-to-server on the firm's own infrastructure |
| `anthropic-native` | `web_search` | Declared in the request; executed by Anthropic server-side (supported on Vertex) |

`capability ∈ {read, write, modify}`. **No delete verb is registered, importable, or
reachable.** The Phase I OAuth/PKCE machinery is not in this path at all — authorization
is the user's Firebase session at POST time plus the task context; Vertex has no MCP
connector, so client-side orchestration is the only possible design anyway.

### 4.2 In-process read tools

Reuse the existing read handlers as-is (dossiers, parties, tasks, hearings, notes,
documents metadata, billing, fidéicommis, protocol, deadlines, etc.). The MCP output
conventions hold — notably `date_str` for date-only fields.

### 4.3 New write/modify tools (minimal v1 set — FLAG, extensible)

- `create_note` / `append_to_note` — as the existing MCP write path, same stamps.
- `create_task` — deadline custody, as existing.
- `save_draft` — creates a **versioned markdown draft** attached to the dossier (the
  core "rédaction" deliverable).
- `revise_draft` — appends a **new version** of an existing draft.

**Versioning invariant (applies to every write/modify tool):** modification never
overwrites. Each write appends a new version (subcollection + head pointer, or the
documents module's existing versioning where applicable) and records provenance:
`{conversation_id, turn_id, model, skill_versions}`. Deletion of versions does not exist.
Human-facing provenance keeps the existing « Ajouté par Claude » stamp convention.

### 4.4 The missing read tool — document content

**No existing tool returns file contents** (`list_documents` is metadata-only). Reading
pièces is the central use case, so v1 adds:

- `get_document_text(document_id, page_range?)` — returns extracted text, bounded per
  call (config cap), paginable. Backed by Phase K extractions (`ai_summary` /
  extracted-text artifacts) where they exist; otherwise a bounded extraction path
  (PDF text layer via existing stdlib-compatible means; Document AI escalation is
  Phase K's job, not this tool's). Binary content is never returned. Oversized results
  are truncated with an explicit continuation marker — the model pages, it does not
  receive megabytes.

> **FLAG.** The exact extraction fallback (reuse Phase K pipeline output only vs. on-demand
> extraction) is Claude Code's call within the no-new-dependencies rule; if a document has
> no extractable text, the tool says so honestly rather than OCR-ing inline.

### 4.5 External Workers and web_search

- Worker tools are namespaced (`legislation_*`, `jurisprudence_*`) and forwarded with a
  service credential from Secret Manager/env — never hardcoded.
- Any tool failure (Worker HTTP error, timeout) returns an **error tool_result** to the
  model; the loop continues and the error is visible in the transcript. Fail loud,
  degrade never.
- `web_search` is enabled for doctrine. Per-search cost is real; it is counted in the
  turn's usage record (§7).

---

### 4.6 Authorization — three mechanisms, one seam

Human authorization happens at exactly one seam: **between a `tool_use` block and its
`tool_result`.** The Messages API is stateless, so the seam can stay open for
milliseconds or for days — the conversation is data at rest; nothing waits in memory,
and the async chain (§2) makes "pause" mean simply "no continuation task enqueued yet."
Three mechanisms use the seam, lightest first:

1. **Ask-and-end-turn (interactive default).** When the model judges an action
   consequential or ambiguous, the charter instructs it **not** to call the tool and to
   finalize its turn with the question instead. The answer is the next user turn. This
   is what Claude.ai itself does — there is no mid-turn pause there either. Zero
   machinery.
2. **The `dry_run` proposal idiom.** The Athéna handlers already implement two-phase
   writes: `dry_run: true` returns the fully validated computed effect without writing
   anything. House idiom: propose via dry_run, present the computed effect, commit only
   on explicit instruction, with the idempotency key. This is the **mandatory** posture
   for consequential writes in scheduled runs, which cannot ask (§12.3).
3. **Structural gating (`requires_authorization`).** The registry accepts a per-tool
   boolean, set in config. A gated `tool_use` pauses tool_result assembly and finalizes
   the turn into `awaiting_authorization`, rendering the pending call(s) as an
   Approuver / Refuser card. Approval — a session + MFA-authenticated POST — executes
   the tool and re-enqueues the chain; refusal appends an error tool_result
   (« refusé par l'avocat ») and re-enqueues, letting the model adapt. All tool_results
   of one model turn are assembled before the continuation call, so a gated call in a
   parallel batch holds the whole batch. The pause is unbounded; the only cost of a
   long one is prompt-cache expiry (rewritten on resume) — never correctness. **The
   mechanism is implemented in v1; the gated set is empty** (FLAG 3).

---

## 5. Skills — runtime-managed, not hardcoded

Skills change and will keep changing; they are **data, not code**.

- Collection `chat_skills`: `{name, description, active}` with an append-only `versions`
  subcollection holding the markdown body. Editing a skill = appending a version and
  moving the head. Deactivation exists; deletion does not.
- Seed content: the current user skills (`redaction-juridique-quebecoise`,
  `recherche-juridique-quebecoise`) imported as version 1 of each.
- UI: a management screen (list, view, edit-as-new-version, activate/deactivate) and a
  **per-conversation multi-select** in the composer area.
- Injection: selected skills are concatenated into the system prompt after the base
  charter. **FLAG (default chosen):** the version used is the head at the moment of each
  turn, and the exact `(skill_id, version)` pairs are recorded on that turn in the
  registre — so a mid-conversation skill edit takes effect on the next turn and the
  record shows precisely which text governed which output.
- Changing the skill selection or content invalidates the prompt cache prefix; accepted
  cost, no special handling.

---

## 6. Conversations, registre, and resumption

### 6.1 Data model

- `chat_conversations/{id}`: `{dossier_id (nullable), title, model, created_at,
  skill_selection, status, token_totals, cost_snapshot}`. A null `dossier_id` is a
  conversation flottante.
- `chat_conversations/{id}/turns/{seq}`: **append-only**, one document per message event
  (user message; assistant message with its thinking, text, and tool_use blocks; grouped
  tool_results). Monotonic sequence. Never mutated after finalization; the only mutable
  window is the pending assistant turn being built by its own task chain (§2), guarded
  by the step token.
- **1 MB/doc guard:** large tool_results are stored in full in Firebase Storage
  (`chat_artifacts/…`) with a pointer + inline truncation in the turn doc. The registre
  remains complete via the Storage object; the Firestore doc stays small. Turn content
  is never an array on the conversation doc.

### 6.2 The registre

The `turns` subcollection **is** the registre. Each finalized turn records: content
blocks verbatim (thinking included, signatures included), tool calls with arguments and
results (or Storage pointers), model id, skill versions, pricing snapshot, token usage,
timestamps. Writes the model performs elsewhere (notes, drafts, tasks) carry the
provenance backlink (§4.3), so the registre and the version chains cross-reference.

- **Append-only is a code convention, not a storage control** — accepted deliberately:
  Loi 25 erasure must remain technically possible. Two consequences are binding:
  1. **No delete route, handler, tool, or UI element exists in the application.**
     Erasure is an out-of-band, documented manual procedure (console/script), performed
     by the lawyer, logged by the platform.
  2. **Firestore Data Access audit logging is a deployment prerequisite** — the same
     project-level flag already required for the fidéicommis module. It is the
     compensating control that distinguishes a registre from an ordinary collection.
     One flag covers both modules; enable before first real use.

### 6.3 Resumption

Opening a conversation rebuilds the Messages array from `turns` in sequence and
continues. Thinking blocks are replayed verbatim (§3). v1 sends the full history
(1 M context + caching absorb this); compaction is explicitly v2.

---

## 7. Token accounting

- Every Vertex response's `usage` block (input, output — thinking included — cache reads,
  cache writes, web searches) is recorded on the turn, with the model id and a pricing
  snapshot taken from **config** (not code): Vertex list prices, the +10 % multi-region
  premium, the Sonnet 5 introductory pricing end date (2026-08-31), per-search cost.
- Aggregates are **maintained counters, never SUM aggregations** (known index piège):
  the turn-finalizing transaction increments `token_totals` on the conversation doc and,
  when `dossier_id` is set, a `chat_usage_dossier/{dossier_id}` counter doc.
- UI: a cumulative indicator per conversation (tokens + estimated cost, **USD** — GCP
  bills in USD; no invented FX conversion — FLAG) and a per-dossier roll-up view.
  **No alarms, no budget gates** — indication only, per decision.

---

## 8. System charter (high level)

A single base charter, in French, versioned in config, stating at minimum:

- Context: outil interne d'un avocat québécois ; droit civil et commercial ; sortie en
  **français** et en **markdown uniquement** (aucun autre format de livrable).
- Epistemic duties: no invented citations, no invented statutory text; legislation is
  read through the `legislation` tools, jurisprudence through the `jurisprudence` tools.
- **Citation rule:** any jurisprudential citation appearing in a draft or analysis must
  have been passed through `jurisprudence`'s citation-verification tool during the
  conversation before delivery.
- Privileged-data posture: the model reads pièces via tools only, quotes only what the
  task requires.
- Tool-use norms: prefer internal tools over web_search for anything the firm's systems
  know; web_search is for doctrine and open sources.

---

## 9. Model policy and data governance

- **Allowlist (closed): `claude-sonnet-5`, `claude-opus-4-8`** on Vertex. Sonnet is the
  default for new conversations; Opus is selected per conversation for recherche and
  rédaction. The model is a per-conversation setting (a URL segment at call time).
- **Exclusion by policy:** Covered Models (Fable-class, Mythos-class) are excluded —
  mandatory 30-day prompt/response retention under the Advanced AI Safety Addendum.
  Adding **any** future model requires verifying its retention class first; the
  allowlist lives in config with a comment saying exactly this.
- Neither allowed model trains on customer data under Vertex terms; neither carries
  forced retention. No Vertex features that persist prompts are enabled (§3).
- Nothing model-facing ever includes secrets; tool results are the only data channel.

---

## 10. UI (high level, existing stack only)

- **Conversation list**: grouped by dossier + a « Flottantes » section; new-conversation
  action asks dossier (or floating), model, skills.
- **Conversation view**: transcript of turns; thinking rendered as a collapsed
  « Réflexion » block per assistant turn (expandable); tool calls rendered as chips
  (`outil : get_dossier`) expandable to arguments/results; failed turns clearly marked;
  the token/cost indicator; the composer with model + skills visible.
- **Pending turn**: the polled fragment shows the live phase (réflexion / outil /
  rédaction). No optimistic rendering of unfinalized model text.
- **Skills screen**: as §5.
- Jinja2 + HTMX + Alpine, vendored assets only, existing auth (Firebase session + MFA),
  existing CSP. Nothing new client-side.

---

## 11. Security posture

- All chat routes behind the existing session/MFA middleware; task handler route
  restricted to Cloud Tasks (header validation + firewall rule).
- Worker service credentials in Secret Manager/env; least-privilege IAM for Vertex.
- The chat writes only through the registry's declared tools — no generic "execute"
  capability exists.

---

## 12. Scheduled tasks (tâches planifiées)

A scheduled run is an **ordinary conversation whose first turn is initiated by cron
instead of a POST**. The §2 task chain executes it unchanged; everything in §§3–7
(thinking, tools, registre, accounting) applies verbatim.

### 12.1 Definitions

- Collection `chat_scheduled_tasks`: `{name, prompt, model (default Sonnet),
  skill_selection, recurrence, hour_local, dossier_id (nullable — default null,
  i.e. flottante), deliver_email (bool), active, last_occurrence, next_occurrence}`.
- Runtime-editable UI, same philosophy as skills (§5): create, edit, activate /
  deactivate. **Deletion does not exist.** Edits take effect on the next occurrence.
- Recurrence is a small vocabulary — `quotidien` / `jours_ouvrables` /
  `hebdomadaire` (+ day) — **not** cron-expression parsing. Times are
  America/Montreal via stdlib `zoneinfo`; occurrence identity is keyed on the
  **local** date to stay DST-safe.

### 12.2 Dispatch

- **One static `cron.yaml` entry, forever** (e.g. every 15 minutes), hitting a
  dispatcher route restricted to App Engine cron (header validation + firewall —
  cron traffic arrives from `0.1.0.2` like Cloud Tasks; the existing allow rule
  must cover this route — verify, do not assume).
- The dispatcher reads due tasks and, per occurrence: creates a **new** conversation
  titled `« {name} — {date_str} »`, appends the stored prompt as the user turn, and
  enqueues the normal chain. Task definitions are therefore editable at runtime with
  no redeployment.
- **Occurrence idempotency**: dispatch is guarded by a Firestore transaction on
  `(task_id, local occurrence)`; duplicate cron delivery dispatches nothing twice.

### 12.3 Unattended discipline

- A charter addendum is injected for scheduled runs: the model runs unattended, asks
  no questions, and produces a self-contained markdown report.
- Every write/modify tool call in a scheduled run **must** carry an idempotency key
  derived from `(task_id, occurrence, step)` — the Athéna handlers already support
  idempotency keys and recommend them for unattended writes; this makes it mandatory.
- Chain ceiling and loud failure (§2.2) apply unchanged; a failed run is visible in
  the conversation list, never silent.
- Scheduled runs **never** enter `awaiting_authorization`: if a tool is gated
  (§4.6.3), a call to it in unattended context is auto-answered with a refusal
  tool_result directing the model to propose the action via `dry_run` in its report
  instead. No run ever stalls waiting for a human who is not there.

### 12.4 Delivery and accounting

- The finished run lands in « Flottantes » with an unread marker. Because it is an
  ordinary conversation, it is **resumable**: the morning briefing can be opened and
  interrogated.
- `deliver_email: true` sends the finalized markdown through the Phase J Graph
  `sendMail` infrastructure (notification@ mailbox). Default off.
- Token usage aggregates on the task record (floating runs have no dossier roll-up).

### 12.5 Seeding

The three scheduled tasks currently configured in the Claude.ai project are recreated
as seed rows. **Their prompts and cadences must be supplied by Jason** — this spec
cannot read the Claude.ai scheduling configuration — and should be re-expressed
against the registry tool names, since the same tools now run in-process.

---

## 13. Tests and acceptance (high level)

1. Turn lifecycle: user POST → chained tasks with a simulated `tool_use` sequence →
   finalized turn; transcript shows thinking, tool chips, final markdown.
2. Idempotency: duplicate task delivery of the same step produces exactly one Vertex
   call's worth of state advancement.
3. Resumption: a conversation with prior thinking + tool turns resumes and continues
   correctly (blocks replayed verbatim).
4. Chain ceiling: a forced loop hits the ceiling and fails loud.
5. No-delete: static check that no delete verb exists in registry, routes, or UI.
6. Versioning: `revise_draft` produces a new version with provenance; head moves; prior
   versions intact.
7. Counters: token totals on conversation and dossier docs match the sum of turn usage
   records (computed in Python in the test).
8. Registre completeness: every Vertex call maps to exactly one recorded turn segment;
   a mid-chain failure leaves a `failed` turn, never an unrecorded call.
9. Skills: mid-conversation skill edit → next turn records the new version id.
10. Oversized tool_result: stored to Storage, pointer in turn doc, doc < 1 MB.
11. Dispatch idempotency: duplicate cron delivery for the same occurrence creates
    exactly one conversation.
12. Scheduled run end-to-end: due task → new floating conversation carrying the
    unattended charter addendum → finalized report with usage recorded on the task;
    `deliver_email` path exercised when enabled; a mid-run failure leaves a visible
    `failed` conversation.
13. Gating: a test-only tool flagged `requires_authorization` pauses the turn into
    `awaiting_authorization`; the approve path executes the tool and the chain
    continues; the refuse path appends the error tool_result and the model's adapted
    continuation is recorded. The same tool called in a scheduled run is auto-refused
    with the dry_run directive, and the run completes.

---

## 14. Flagged defaults (overridable in one line each)

| # | Decision | Default chosen |
|---|---|---|
| 1 | Phase letter | N |
| 2 | Write-tool set v1 | Notes, tasks, versioned drafts only |
| 3 | Approval gate on writes | Mechanism implemented (§4.6); `requires_authorization` set is **empty** — no tool gated in v1 |
| 4 | Skill version binding | Head-at-each-turn, version recorded per turn |
| 5 | Vertex endpoint | Multi-region `us` (+10 %) |
| 6 | Cost display currency | USD estimate, no FX |
| 7 | `get_document_text` fallback | Phase K artifacts first; honest failure otherwise |
| 8 | Chain ceiling | 12 model calls per turn (config) |
| 9 | Conversation title | First user message truncated; auto-titling is v2 |
| 10 | Débours posting of AI costs | Not in v1; accounting captured for later |
| 11 | Scheduled run container | New conversation per occurrence, not a rolling thread |
| 12 | Scheduled default model | Sonnet (quotidien / administration) |
| 13 | Email delivery of runs | Per-task boolean, default off (Phase J Graph infra) |
| 14 | Recurrence vocabulary | quotidien / jours_ouvrables / hebdomadaire — no cron expressions |
