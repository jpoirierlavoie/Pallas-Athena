# Deploying your own Pallas Athena instance

This guide takes you from an empty Google Cloud project to a running,
Cloudflare-fronted deployment of Pallas Athena. It is written for someone who
has **not** worked with App Engine, Firebase, or Cloudflare before, so it errs
on the side of explaining *why* a step exists.

> **New to the project?** Read [README.md](README.md) first for what the app is,
> then come back here.

---

## 0. Before you begin — permission & eligibility

**Pallas Athena is not open-source.** The [LICENSE](LICENSE) is
all-rights-reserved: the source is published for reference, and *no right to
use, copy, modify, or deploy it is granted automatically.*

You may run your own instance **only after** obtaining **prior written
permission** from the copyright holder, Jason Poirier Lavoie. Because the
application is purpose-built for legal practice and handles data subject to
professional-secrecy obligations, permission is granted **only to practising
lawyers** (e.g. members of the Barreau du Québec or another bar).

**To request permission**, email `jason@poirierlavoie.ca` with:
- your name and bar / order membership (with member number),
- the jurisdiction you practise in,
- a brief description of how you intend to use the app.

Do not deploy until you have that written consent. The rest of this guide
assumes you have it.

---

## 1. What you are deploying (and what you're signing up for)

Pallas Athena is a **deliberately single-tenant** practice manager. Adopting it
is closer to *re-provisioning and adapting* than *clone-and-run*. Three
assumptions are baked deep into the code — accept them before you start:

| Assumption | What it means for you |
|---|---|
| **One authorized user** | Exactly one email is allowed, enforced server-side ([auth.py](athena/auth.py)). There is no registration, no roles, no multi-tenancy. Supporting more than one user is a rewrite, not a setting. |
| **French-only UI** | Every label, button, and message is hardcoded French. There is no i18n layer. |
| **Québec legal domain** | Taxes (GST 5 % / QST 9.975 %), judicial deadlines (art. 83 C.p.c. + Québec holidays), court-file-number parsing, courthouse/tribunal reference data, and the CQ/CS protocol templates are Québec-specific. **Deploying as-is outside Québec produces wrong deadlines and wrong taxes.** See §13 to adapt. |

Architecture at a glance:

```
Browser / DavX5 / Claude                     Invited client (public)
        │                                              │
   Cloudflare  (TLS, WAF, Early Hints, origin secret)  │
        │                                              │
  App Engine « default »                    App Engine « portail »
  (Flask + gunicorn, F2)                    (F1, own least-privilege SA)
        │                                              │
        │                           ┌──────────────────┤
        │                           │                  │
        │                    Cloud Tasks queue   Quarantine bucket
        │                     « portail »        (client uploads)
        │                           │                  │
        └──────────┬────────────────┴──────────────────┘
                   │
  ┌────────────────┼──────────────┬──────────────┬────────────────┐
Firestore     Firestore      Firebase Storage  Firebase Auth  Secret Manager
(default)     « portail »    (documents/       (+ Phone MFA)  (6 secrets)
              (named DB)      gabarits)
```

**There are TWO App Engine services, not one**, plus a `dispatch.yaml` that
routes the portal host to the second and a `cron.yaml` carrying three jobs.
This diagram showed one until 2026-09-12, which is how an adopter learns the
`portail` service exists only when their first CI build fails deploying it.
Its infrastructure — the least-privilege service account, the quarantine
bucket, the named Firestore database, the Cloud Tasks queue and nine IAM
grants — is **not yet written up in this document**; `CLAUDE.md` « Portail
client » is the authority until it is.

---

## 2. Prerequisites

**Accounts**
- A **Google Cloud** account with billing enabled.
- A **Cloudflare** account on the **Pro plan** (≈ US$25/mo) — required for
  Early Hints. (Zero Trust / Access is optional — this deployment does not use it.)
- A **domain name** you control (this guide uses `yourdomain.example`).
- *Optional:* a **Google Play Console** account (only for the Android TWA, §12).
- *Optional:* a **claude.ai** account (only for the MCP connector, §11).

**Local tools**
- Python **3.13**
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install)
- [`firebase` CLI](https://firebase.google.com/docs/cli) (`npm i -g firebase-tools`)
- [`uv`](https://github.com/astral-sh/uv) (only if you change dependencies)
- Node.js + npm (only if you recompile the CSS — see §15)
- `git`. **Not** a separate `pip install bcrypt`: `bcrypt` is a pinned direct
  dependency, so `pip install -r athena/requirements.txt` (§14) already brings
  it — and a `.venv` created with `--without-pip` has no `pip` to run the
  command with anyway (« No module named pip », measured on this repository's
  own venv). §6.4's recipe therefore *checks* for bcrypt instead of installing
  it blind.

---

## 3. Cost expectations (rough, low-traffic single user)

| Component | Ballpark |
|---|---|
| App Engine F2, `min_instances: 0` | a few $/mo (pay-per-request; cold starts) |
| App Engine F2, `min_instances: 1` | ~US$40–50/mo (one always-on instance, no cold starts) |
| App Engine F1 — the **`portail` service** | a few $/mo (also `min_instances: 0`; it only moves JSON, never file bytes) |
| Firestore (native) — **two databases** | cents–low $/mo at single-user volume |
| Cloud Tasks + Cloud Scheduler | free tier covers one queue and three cron jobs |
| Quarantine bucket (client uploads) | pennies/mo + egress; lifecycle purges at 90 / 365 days |
| Firebase Storage | pennies/mo + egress |
| Secret Manager | negligible |
| reCAPTCHA Enterprise | free tier is generous for one user |
| **Cloudflare Pro** | **~US$25/mo (required)** |
| Domain | ~US$10–20/yr |

Cloudflare Pro and (optionally) an always-on App Engine instance dominate the
bill. Everything Google-side is small at single-user scale.

---

## 4. Configuration reference

### 4.1 Environment variables

Set these in [`athena/app.yaml`](athena/app.yaml) (main service) and
[`athena/portail.yaml`](athena/portail.yaml) (portal service) for production,
and in a local `.env` (from [`.env.example`](.env.example)) for development.
**Never** put a secret in either yaml.

**Complete by derivation.** A test parses `config.py`, `client/config.py` and
`client/app.py` for every environment variable they actually read and fails if
one is absent from the tables below. **Twenty-five were missing until
2026-09-12** — the whole Microsoft Graph, Bookings and Outlook-mirror surface,
the portal's own four, and `MCP_WRITE_ENABLED`. Five of those govern
integrations an adopter cannot otherwise discover without reading `config.py`.

#### Core — boot, authentication, edge

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `ENV` | ✔ | `development` | `production` switches secret resolution to Secret Manager. Read by **both** services |
| `SECRET_KEY` → secret `flask-secret-key` | ✔ **hard** | — | Flask session signing; **app won't boot without it** |
| `FIREBASE_PROJECT_ID` | ✔ **hard** | — | Your GCP/Firebase project id. It also *builds the Secret Manager resource path*, so **nothing** — not one secret — resolves without it |
| `FIREBASE_STORAGE_BUCKET` | ✔ **hard** | — | Your Storage bucket name |
| `AUTHORIZED_USER_EMAIL` | ✔ **hard** | — | The single user; also the DAV username |
| `FIREBASE_APP_ID` | ○ | `""` | Web-app id used by the App Check bootstrap |
| `FIREBASE_API_KEY` → secret `firebase-api-key` | ○ | `""` | Public browser key (safe to expose, kept out of git) |
| `DAV_PASSWORD_HASH` → secret `dav-password-hash` | ○ (needed for DAV) | `""` | **bcrypt** hash of the DAV password |
| `RECAPTCHA_ENTERPRISE_SITE_KEY` | ○ (recommended) | `""` | App Check; **fail-open + loud warning** if unset in prod |
| `APPCHECK_DEBUG_TOKEN` | ○ | `""` | Local dev only |
| `CF_ORIGIN_SECRET` → secret `cf-origin-secret` | ○ | `""` | Edge origin check; **fail-open and completely SILENT** if unset — no log, no metric. §4.4 and « Paramètres → Configuration » are the only things that will tell you |
| `REQUIRE_MFA` | ○ | `true` | Enforce Phone MFA. Read by « Paramètres → Sécurité » to decide whether the last enrolled factor may be removed |
| `SESSION_LIFETIME_HOURS` | ○ | `12` | Server-side session lifetime |
| `RATE_LIMIT_LOGIN` | ○ | `5 per minute` | Login rate limit |
| `MCP_ENABLED` | ○ | `true` | `false` → all `/mcp` + `/oauth/*` routes 404 |
| `MCP_WRITE_ENABLED` | ○ | `true` | `false` → every write tool vanishes from `tools/list` and is refused at `tools/call`, and the consent checkboxes disappear; reads are untouched. It is the **master** write switch: the accounting tools below are writes, so `false` stops them too, whatever `MCP_COMPTABILITE_ENABLED` says. Its arm/disarm procedure is **deploy-ordered** — see the comment in `app.yaml` — which is why it is deliberately not editable at runtime |
| `MCP_COMPTABILITE_ENABLED` | ○ | **`false`** | The accounting switch — the only MCP switch that defaults to **off** (money is fail-closed: forgetting the variable leaves accounting off). `false` → the accounting tools (scope `athena:comptabilite`, a separate consent box « Autoriser la comptabilité ») vanish from `tools/list`, are refused at `tools/call`, and their box is not offered. Since plan lot 5b six tools carry the scope — `get_admin_ledger` (read) and the ACCOUNTING family (`record_trust_entry`, `record_admin_entry`, `update_admin_entry`, `clear_register_entries`, `reverse_register_entry`) — so `true` offers the box, and a token granted it sees them. Same double duty and same deploy order as `MCP_WRITE_ENABLED` — re-consenting while it is `false` silently yields a grant without accounting; see `app.yaml` |
| `MCP_CANONICAL_ORIGIN` | ○ | owner domain in [config.py](athena/config.py) | OAuth issuer — **must be your domain** |
| `TRACE_SAMPLE_RATIO` | ○ | `0.1` | Trace sampling (read by `utils/tracing_setup.py`, not by `config.py`) |
| `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` | ✔ (prod) | — | `gcp`. Read by the **OpenTelemetry SDK itself**: since SDK 1.42 the resource detectors load *only* when it is set. Needed in **both** yamls — each service runs its own `tracing_setup` in its own process |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | ○ (prod) | SDK default | OTLP export timeout **in seconds**. Both yamls set `5` |
| `PIP_REQUIRE_HASHES`, `PIP_NO_DEPS` | ✔ (prod) | `1`, `1` | Supply-chain: reject unhashed/out-of-band installs |

#### Firm profile — SEED and FALLBACK only

The live firm profile is edited in « Paramètres » and stored at
`settings/cabinet`. These eleven bootstrap a fresh deploy on an empty database
and are what the app falls back to if Firestore is unreadable — they are **not**
the running values once the profile has been saved once. The `portail` service
is the exception: it reads `FIRM_NAME`/`FIRM_PHONE` straight from its own yaml
and cannot reach the singleton.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `FIRM_NAME` | ○ | `""` | **The lawyer**, not the firm — the compliance signer and the letterhead |
| `FIRM_STREET`, `FIRM_UNIT`, `FIRM_CITY`, `FIRM_POSTAL_CODE` | ○ | `""` | Address, in the app's own six-suffix shape |
| `FIRM_PROVINCE` | ○ | `QC` | ⚠ The only non-empty default here, and it disagrees with the convention every contact address in the app follows (`Québec`, spelled out). `_seed_from_env` migrates it through `apply_address_defaults`, so the stored profile is correct — but a reader of `app.yaml` sees `QC` |
| `FIRM_PHONE`, `FIRM_FAX`, `FIRM_EMAIL` | ○ | `""` | Phones are stored **E.164**; `cabinet_dict()` renders the local form |
| `GST_NUMBER`, `QST_NUMBER` | ○ | `""` | ⚠ Read at invoice creation and **snapshotted per invoice** — nothing rewrites them on an existing invoice (a brouillon's correction, lot 3a, reaches its notes, terms, due date and billing address only), so setting them here (or in « Paramètres ») affects **future invoices only**. **Since lot 3a a NEW invoice charging GST or QST under an EMPTY number is REFUSED**, on the web form as through the connector — fill both in « Paramètres → Profil du cabinet » before deploying it (§15 « Lot 3 », step 1) |

#### Integrations — Microsoft Graph, Bookings, Outlook mirror

Main service only. The `portail` service has **no** `GRAPH_*` variable by
design (spec L1 §8.1 — it cannot call Graph at all).

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `GRAPH_TENANT_ID` | ○ | `""` | Entra tenant. Outbound email is off unless all four Graph values are set (`Config.graph_configured()`) |
| `GRAPH_CLIENT_ID` | ○ | `""` | The app registration |
| `GRAPH_SENDER_UPN` | ○ | `""` | The mailbox invitations and accusés are sent **from** |
| `GRAPH_SENDER_NAME` | ○ | `""` | Display name on those messages |
| `GRAPH_CLIENT_SECRET` → secret `graph-client-secret` | ○ | `""` | Resolved with `required=False`, which **swallows a PERMISSION_DENIED into `""`** — a missing accessor binding therefore turns outbound email off in silence. §6.4's grant loop covers it |
| `BOOKINGS_JURISTE_UPN` | ○ | `""` | The mailbox whose calendar is polled. **Empty short-circuits the sync** |
| `BOOKINGS_SYNC_ACTIVE` | ○ | `true` | Kill switch for the 10-minute Bookings import |
| `BOOKINGS_SUBJECT_KEYWORDS` | ○ | `Consultation` | Comma-separated. A subject matches when it **ends** with « {separator} {keyword} », case- and accent-folded — Bookings names the event `{Customer} - {Service}`, so the service name is a suffix. ⚠ An **empty** value is a total, silent outage: nothing matches, and the absence loop then flags every already-imported reservation `annulée_client`. The sync refuses to run rather than proceed |
| `BOOKINGS_SYNC_LOOKAHEAD_DAYS` | ○ | `90` | Forward window of the poll |
| `BOOKINGS_SYNC_LOOKBACK_DAYS` | ○ | `1` | Backward window of the poll |
| `BOOKINGS_DEBUG_PAYLOAD` | ○ | `false` | Predicate tuning: logs the first detected and first undetected event **at INFO** (the root logger sits at INFO in production, so DEBUG would be mute exactly where it is needed). Domains and predicate booleans only — never the subject, which embeds the client's name |
| `MIROIR_OUTLOOK_ACTIF` | ○ | `true` | Kill switch for the Athéna → Outlook mirror. `false` freezes existing mirrors in place; it does not clean them up |
| `MIROIR_OUTLOOK_LOOKAHEAD_DAYS` | ○ | `365` | The mirror's forward window — **shared** by the Athéna read and the Outlook read, which is what keeps a mirror from becoming an invisible orphan |
| `MIROIR_OUTLOOK_LOOKBACK_DAYS` | ○ | `30` | Its backward window |
| `FEATURE_INTAKE` | ○ | `false` | Offers the portal's intake (dossier-opening) form when confirming a Bookings rendez-vous whose email matches no contact |

⚠ **Two Bookings settings have no environment variable at all.**
`BOOKINGS_TYPE_PAR_MOT_CLE` (keyword → hearing type) and
`BOOKINGS_TYPE_DEFAUT` are literals in
[`config.py`](athena/config.py). Publishing a new Bookings service and naming
it in `BOOKINGS_SUBJECT_KEYWORDS` **without** adding it to that map falls back
to « consultation » — logged, but only in the logs. Editing `config.py` is the
only way to change it today.

#### The `portail` service — set in `portail.yaml`

The portal runs in its own process under its own least-privilege service
account, so it re-reads what it needs from its own yaml. `ENV`,
`FIREBASE_PROJECT_ID`, `FIREBASE_APP_ID`, `RECAPTCHA_ENTERPRISE_SITE_KEY`,
`APPCHECK_DEBUG_TOKEN`, `FIRM_NAME` and `FIRM_PHONE` are the same names as
above and must be set **again** there.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `PORTAIL_SECRET_KEY` → secret `portail-secret-key` | ✔ **hard** (portal) | — | The portal's session key, **distinct** from the main one. The service does not boot without it |
| `PORTAIL_HOST` | ○ | ⚠ `portail.poirierlavoie.ca` — the **original owner's** host | Used to build the fallback link in every invitation email |
| `PORTAIL_BUCKET` | ○ | `{FIREBASE_PROJECT_ID}-portail-quarantaine` | Derived, so it follows your project id by itself |
| `TASKS_LOCATION` | ○ | ⚠ `northamerica-northeast1` (Montréal) | **Must equal your App Engine region** (`gcloud app describe`). It is not derived: deploy elsewhere and leave it, and every `signaler()` raises `NOT_FOUND` at finalization — a failure the portal swallows **by design** (the envelope is the durable truth), so the only symptom is lots arriving in Réception ~15 minutes late via the reconciliation cron |

### 4.2 The six Secret Manager secrets (production)

**Six, not four.** This section said "the four" from the day
`portail-secret-key` shipped as a fifth and `graph-client-secret` as a sixth —
which is the predicted behaviour of a hand-kept list, and the reason the table
below is now GENERATED from
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against it by a test.

| Secret | Required for | Where the value comes from | Expected length | If absent or wrong |
|---|---|---|---|---|
| `flask-secret-key` | `default` | you mint it | entre 32 et 256 chars | l'application NE DÉMARRE PAS |
| `portail-secret-key` | `portail` | you mint it | entre 32 et 256 chars | le service « portail » NE DÉMARRE PAS |
| `firebase-api-key` | optional | a console hands it to you | entre 30 et 60 chars | la page de connexion ne peut pas initialiser Firebase |
| `dav-password-hash` | optional | computed from a password you choose | exactement 60 chars | l'authentification DAV ne peut pas réussir — DavX5 cesse de synchroniser SANS message d'erreur |
| `cf-origin-secret` | optional | you mint it | entre 32 et 128 chars | le contrôle d'origine est DÉSACTIVÉ en silence (l'accès direct à App Engine n'est plus bloqué) |
| `graph-client-secret` | optional | a console hands it to you | entre 20 et 256 chars | le courriel sortant est désactivé |

Created in §6.4. `portail-secret-key` is **required for the `portail` service
to boot** — it is not optional in the way the other five non-`flask` entries
are; the portal simply does not start without it.

The live verdict on all six — does each resolve, is its length right, does it
carry stray whitespace or a character that cannot survive an HTTP header — is
on the **« Paramètres → Configuration »** page, and in
`python -m scripts.check_config --prod`. Both read the same table and the same
predicates as this section.

### 4.3 Owner-specific values to replace

Every value below is hardcoded to the original deployment. Replace each one,
then run the checker in §4.4 — **which names the exact value and the exact
file**, so this table deliberately does not repeat the owner's identifiers a
third time.

The table is GENERATED from `OWNER_LITERALS` and `SCAN_FILES` in
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against them by a test: the « Where » column is not a claim, it is
the result of searching each scanned file for that value. It listed nine rows
until 2026-09-12 and the checker already knew of fifteen.

| Value | Where the checker finds it |
|---|---|
| GCP project id | `athena/app.yaml`, `athena/portail.yaml` |
| Storage bucket | `athena/app.yaml` |
| Firebase app id | `athena/app.yaml`, `athena/portail.yaml` |
| Authorized user email | `athena/app.yaml` |
| Firm name | `athena/app.yaml`, `athena/portail.yaml` |
| reCAPTCHA site key | `athena/app.yaml`, `athena/portail.yaml` |
| MCP canonical origin | `athena/app.yaml`, `athena/config.py` |
| Domain | `athena/app.yaml`, `dispatch.yaml`, `athena/config.py` |
| TWA package | `athena/main.py` |
| Portal host | `dispatch.yaml`, `athena/client/config.py` |
| Portal service account | `athena/portail.yaml` |
| Firm phone | `athena/app.yaml`, `athena/portail.yaml` |
| Firm email | `athena/app.yaml` |
| Graph tenant id | `athena/app.yaml` |
| Graph client id | `athena/app.yaml` |
| App Engine region | `athena/client/config.py` |

**Also carrying the owner's identity, outside the scan.** The checker looks at
deployment config only, so these are on you: the firm name, domain and contact
in [`static/legal/privacy.html`](athena/static/legal/privacy.html) and
[`terms.html`](athena/static/legal/terms.html), [README.md](README.md),
[SECURITY.md](SECURITY.md) and [LICENSE](LICENSE); the TWA package and its
SHA-256 signing fingerprint in [main.py](athena/main.py)'s `assetlinks.json`
route (only if you build the Android app, §12); and the placeholder values in
[`.env.example`](.env.example).

⚠ **The reCAPTCHA site key must be replaced *and* the old one rotated** — a
site key is public, so forking the repo hands it to everyone; and a fork that
keeps it is sending App Check attestations against someone else's key.

### 4.4 Verify your configuration

A helper script checks required vars, warns on disabled fail-open controls, and
flags any owner-default literal you forgot to replace:

```bash
cd athena
python -m scripts.check_config          # uses ENV from your shell/.env
python -m scripts.check_config --prod   # force the production ruleset
```

**And a second one checks the INFRASTRUCTURE**, which the first never
looked at — it reads env vars, secrets and committed files, but knows nothing
about whether the queue exists or the firewall lets your own cron through:

```bash
cd athena
python -m scripts.provision --project $PROJECT                  # état + plan
python -m scripts.provision --project $PROJECT --plan-seulement # le plan seul
python -m scripts.provision --project $PROJECT --json           # pour une machine
```

It **never mutates anything**, and that is structural rather than promised:
every command it runs comes from a table whose verbs are restricted, by
allowlist, to `describe` / `list` / `get-iam-policy` — a test enforces it.
There is deliberately no `--apply`.

`--project` is **required and never inherited** from `gcloud config`: a stale
active project is how you inspect — or provision — inside someone else's.
The region is not asked for; it is read from the App Engine application,
which is authoritative because its region is permanent.

Exit codes, and the distinct `2` matters — a CI that conflates « go click in a
console » with « something is broken » ends up ignoring both:

| Code | Meaning |
|---|---|
| `0` | everything detectable is in place |
| `1` | **drift** — a resource exists and contradicts what the code expects |
| `2` | work remains: not yet provisioned, console-only, or not verifiable |

⚠ **A failed probe reports `inconnu`, never « absent ».** `gcloud` missing,
stale credentials, a disabled API — none of those say anything about the
resource, and reporting an absence would send you to provision what is
already there. The first real run proved the rule before it was ever tested:
`subprocess` on Windows resolves only `.exe`, never the SDK's `gcloud.CMD`,
so ten present resources came back « not verified » instead of « missing ».

---

## 5. Order of operations (read this before running anything)

**Three of these are irreversible**, and no later step can undo them: the App
Engine region (3), the default Firestore database's location (4), and — once
the portal is provisioned — the named `portail` database's. Everything else
can be re-run.

**Four are order-sensitive**, and each fails in its own way: indexes before the
first deploy (5), or a query silently returns an empty list while it builds;
the firewall's `0.1.0.2/32` allow **before** the default-deny (12), or you cut
off your own cron and queue; the Cloudflare Transform Rule (12) **before** the
origin secret gets a VALUE (13), or the site answers 403 everywhere with no
deploy to explain it; and the storage bucket before its rules (8).

⚠ **That third one was wrong in this very list until 2026-09-13** — the
preamble warned about the order while the numbered steps put the secrets at 6
and Cloudflare at 12, which is the trap itself. It is split now, and the split
is the honest shape: an empty *container* is inert, a *version* is what arms
the check.

0. **Authenticate** — `gcloud auth login`, `gcloud auth
   application-default login` (the ADC the scripts of §9 use), and
   `firebase login`. This step did not exist in this document until
   2026-09-12: §6.1 opened straight on `gcloud projects create`, and §9 later
   referenced credentials nothing had told you to obtain. Note that
   `.firebaserc` is gitignored and absent, so **every `firebase` command needs
   an explicit `--project`**.
1. Create GCP project + enable billing, then `gcloud config set project
   $PROJECT` (it cannot be selected before it exists — every command below
   also takes `--project=$PROJECT` explicitly, which is the safer habit: a
   stale active project is how you provision into someone else's)
2. Enable required APIs
3. **Create the App Engine app — the region choice is PERMANENT** (§6.2)
4. Create Firestore in native mode (same region)
5. **Deploy indexes + rules — BEFORE the first code deploy** (§6.3). Until an
   index finishes building, the query it serves fails and the view silently
   shows an empty list.
6. Create the six empty secret containers + grant IAM, then write **five** of
   the six VALUES (§6.4). The origin secret's value is deliberately NOT one of
   them — it waits for 13. An empty container is harmless: `config._secret`
   resolves an optional secret with no version to `""`, and
   `_enforce_origin_secret` reads that as « disabled »
7. Firebase Auth: create the single user + enroll Phone MFA (§6.5)
8. Storage bucket **then its rules** (§6.6) — the bucket must exist first
9. App Check + reCAPTCHA (§6.7)
10. First deploy + smoke test (§8)
11. Seed reference data (§9)
12. Cloudflare edge (§7) — DNS cutover, Full (Strict), the firewall, and the
    zone-wide **Transform Rule** that injects `X-Origin-Auth`. Can be prepared
    in parallel, but it lands here. **Prove the rule fires** with Cloudflare's
    request tracer before going on; do not infer it
13. **Arm the origin secret** — only NOW write `cf-origin-secret`'s first
    version (§6.4). Earlier, you arm the application against a header nobody
    sends, and with `min_instances: 0` the instances recycle by themselves —
    so the site starts answering **403 on every path with no deploy to
    blame**, including the login page you would use to investigate. The value
    takes effect as instances recycle; redeploy if you want it immediate
14. Optional: DavX5 (§10), MCP (§11), Android TWA (§12)

---

## 6. Google Cloud & Firebase provisioning

Throughout, set `PROJECT=your-project-id` and substitute your own values.

### 6.1 Project, billing, APIs

```bash
gcloud projects create $PROJECT --name="Pallas Athena"
gcloud billing projects link $PROJECT --billing-account=XXXXXX-XXXXXX-XXXXXX
firebase projects:addfirebase $PROJECT

gcloud services enable \
  appengine.googleapis.com firestore.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  iam.googleapis.com iamcredentials.googleapis.com \
  firebase.googleapis.com firebaseappcheck.googleapis.com \
  firebaserules.googleapis.com firebasestorage.googleapis.com \
  storage.googleapis.com identitytoolkit.googleapis.com \
  recaptchaenterprise.googleapis.com logging.googleapis.com \
  cloudtrace.googleapis.com telemetry.googleapis.com \
  cloudtasks.googleapis.com cloudscheduler.googleapis.com \
  --project=$PROJECT
```

**Eighteen, not twelve.** This list said twelve until 2026-09-12 and
omitted six, of which `cloudscheduler` breaks an adopter's very first CI
build — `cloudbuild.yaml` deploys `cron.yaml` unconditionally, as its
**fourth** step, so the failure lands after `default`, `portail` and
`dispatch` have already gone out. The block above and the table below are
both GENERATED from
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against it by a test, so an API added to the code announces
itself here or the suite goes red.

| API | Why it is required | Who calls it |
|---|---|---|
| `appengine.googleapis.com` | Les deux services App Engine y tournent. | app.yaml, portail.yaml |
| `firestore.googleapis.com` | La base par défaut ET la base NOMMÉE « portail » — ce sont deux bases du même service. | models/, client/services/invitations.py |
| `secretmanager.googleapis.com` | Les six secrets de §4.2. Sans elle, `config.py` lève à l'import et le service ne démarre pas du tout. | config.py, client/config.py |
| `cloudbuild.googleapis.com` | Le déclencheur qui exécute la suite puis déploie. | cloudbuild.yaml |
| `iam.googleapis.com` | Les liaisons de rôles que §6.4 pose. | les commandes de §6.4 |
| `iamcredentials.googleapis.com` | L'API `signBlob`. Sans elle l'auto-impersonation échoue et AUCUN URL signé n'est produit — en silence, et en PRODUCTION seulement : en local une clé de compte de service signe sur place, donc ce chemin n'est jamais emprunté avant le déploiement. | models/document.sign_blob_url, models/doc_template |
| `firebase.googleapis.com` | La gestion du projet Firebase. | la CLI firebase |
| `firebaseappcheck.googleapis.com` | App Check, qui vérifie l'attestation des requêtes HTMX. | security.py |
| `firebaserules.googleapis.com` | Le déploiement des règles Firestore et Storage. Sans elle `firebase deploy --only firestore:rules,storage` échoue — et ces règles SONT le refus par défaut qui couvre chaque collection. | firestore.rules, storage.rules |
| `firebasestorage.googleapis.com` | Le seau par défaut de Firebase Storage et ses règles. | firebase-admin.storage |
| `storage.googleapis.com` | L'API JSON de GCS elle-même : URL signés, sessions reprenables, ingestion par rewrite, composition du ZIP d'un dossier de classement. `firebasestorage` gère le seau ; c'est celle-ci qui déplace les octets. | google-cloud-storage |
| `identitytoolkit.googleapis.com` | Firebase Auth — la session, la MFA, et le lien courriel du portail. | auth.py, client/routes.py |
| `recaptchaenterprise.googleapis.com` | Le fournisseur d'attestation d'App Check. | security.py, base.html |
| `logging.googleapis.com` | Le journal structuré, par `CloudLoggingHandler`. Sans elle, il ne reste aucune trace agrégée de ce que la production a fait. | utils/logging_setup.py |
| `cloudtrace.googleapis.com` | Doit rester activée À CÔTÉ de `telemetry` : la note de migration de Google est explicite — désactiver Cloud Trace fait JETER les traces envoyées à l'API Telemetry, en silence. C'est aussi elle qui sert la LECTURE des traces. | utils/tracing_setup.py, la console |
| `telemetry.googleapis.com` | La destination OTLP des spans depuis le 2026-07-30. | utils/tracing_setup.py |
| `cloudtasks.googleapis.com` | La file « portail » par laquelle le service public signale. | client/services/taches.py |
| `cloudscheduler.googleapis.com` | EXIGÉE par `gcloud app deploy cron.yaml`. Son absence fait échouer cette étape sur `SERVICE_DISABLED`, et `cloudbuild.yaml` la place en QUATRIÈME : l'échec arrive donc APRÈS que `default`, `portail` et `dispatch` sont déployés. La première construction d'un adoptant se termine rouge sur un déploiement à moitié fait. | cloudbuild.yaml, cron.yaml |

**`telemetry.googleapis.com` is where spans are now sent** (OTLP, since
2026-07-30 — see `athena/OBSERVABILITY.md`). **`cloudtrace.googleapis.com` must
stay enabled alongside it**, and that is not redundancy: Google's own migration
note is explicit that "if you disable the Cloud Trace API, then Google Cloud
Observability discards trace data you send to the Telemetry API" — silently.
Trace *reading* (the console, the log-entry « View trace » link) is also still
served by the Cloud Trace API.

No IAM change accompanied the migration: `roles/telemetry.tracesWriter` grants
only `telemetry.traces.write`, which `roles/cloudtrace.agent` already carries —
and both service accounts (`$PROJECT@appspot` and `portail-svc`) hold it. If
spans stop arriving with `PERMISSION_DENIED`, that assumption is the first
thing to re-verify.

### 6.2 App Engine app — choose the region carefully (permanent)

```bash
gcloud app create --region=northamerica-northeast1 --project=$PROJECT
```

The original deploys to `northamerica-northeast1` (Montréal). **You cannot
change an App Engine app's region later** — pick the region closest to you and
your data-residency obligations before running this. Firestore should use the
same region (next step).

### 6.3 Firestore (native mode) + indexes + rules

```bash
gcloud firestore databases create \
  --location=northamerica-northeast1 --type=firestore-native --project=$PROJECT

# From the repo root (firebase.json points at the root rule/index files).
# NOTE: storage rules are NOT deployed here — the bucket does not exist yet.
# They go in §6.6, right after you create it.
firebase deploy --only firestore:indexes,firestore:rules --project $PROJECT
```

- **Native mode**, not Datastore mode.
- The rules are intentional **deny-all** — all access is through the Admin SDK
  and signed URLs, which bypass rules. This is also what stops a self-signed-up
  Firebase account from reading your data.
- `firestore.indexes.json` contains the composite indexes (dashboard
  aggregations, cursor-paginated lists, the protocol-steps collection group).
  **They must finish building before you send real traffic.** There is no
  « done » signal from the CLI; watch Firebase console → Firestore → Indexes
  until every one reads **Enabled**.
- ⚠ **The storage rules moved to §6.6.** Until 2026-09-12 this command carried
  `,storage`, which asks the Firebase CLI to deploy rules for a bucket §6.6 has
  not created yet. Doing it in the documented order is strictly safer;
  *whether* the combined form hard-errors or silently no-ops on a project with
  no default bucket has not been tested here, and would need a fresh project to
  settle.

### 6.4 Secret Manager + IAM

**Everything in this section below the IAM grants is GENERATED** from
`athena/utils/deployment_inventory.gcloud_recipe()` and pinned against it by
`athena/tests/test_deployment_inventory.py`. It is therefore the *same*
recipe the « Paramètres → Configuration » page renders beside each secret for
every later rotation — the doc and the page cannot drift, which is the whole
reason the shared table exists. Do not hand-edit the command blocks; change
the function.

The comments inside the blocks are in **French**. That is not an oversight:
they are the very strings the French settings page renders, generated rather
than translated. A second, English copy is exactly the thing that would drift.

> **What the previous version of this section got wrong**, kept here because
> each defect is instructive. It ran `hashpw(b'YOUR_DAV_PASSWORD', …)`, which
> deposits the **plaintext DAV password** in `~/.bash_history`. Every line
> said `gcloud secrets create --data-file=-` — correct on a first deploy, and
> wrong for every rotation since, where the secret already exists and `create`
> fails in a way that reads like a platform fault. And every line ended in
> `| tr -d '\n'` to mop up a newline `sys.stdout.write` never emits, which
> teaches a superstition instead of the rule.

> **The rule itself is one sentence: nothing in the pipeline may emit a
> trailing newline.** `gcloud secrets versions add --data-file=-` stores the
> payload **byte for byte**, and nothing in `config.py` strips it (`_secret` →
> `_from_secret_manager` decodes and returns as-is). So `printf '%s'` and
> `sys.stdout.write` are safe; `echo`, `print()` and a heredoc are not. For
> `cf-origin-secret` one stray byte is site-wide downtime — `security.py`'s
> `hmac.compare_digest` compares the stored value against a header a
> Cloudflare Transform Rule injects, and **a rule cannot emit a newline**, so
> every request answers 403. `dav-password-hash` has the same trap for a
> different reason: a 61-byte hash never matches the 60 bytes `checkpw`
> recomputes, and DavX5 then fails **silently**.

> ⚠ **bash only — and on Windows that is a measurement, not a preference.**
> Measured on the maintainer's Windows 11 machine, 2026-09-11.
>
> **Git Bash works.** `printf '%s' "$V" | <native exe>` delivers exactly the
> bytes held in `$V` — 3 for `abc`, 43 for a 43-character token. The negative
> control is what makes that mean something: `echo`, a heredoc and a
> here-string each deliver one byte MORE, and the extra byte is a bare `\n`,
> so the MSYS→native pipe performs no LF↔CRLF translation either. Verified
> across all 256 byte values, at 200 KB, and into gcloud's own
> `files.ReadStdinBytes()`. On that machine `gcloud` resolves to a POSIX
> launcher, never to `gcloud.cmd`, so `cmd.exe` is not in the chain — check
> yours with `file $(command -v gcloud)`.
>
> ⚠ **But `python3` is a trap on Windows, and it fails SILENTLY.** It is
> present on PATH as a Microsoft Store App Execution Alias that exits 49 and
> writes nothing, so `command -v python3` SUCCEEDS while the interpreter is
> broken. Found by running this section for real against a throwaway secret on
> 2026-09-11: the generated value came out EMPTY, and only the length
> comparison stopped an empty `cf-origin-secret` from being written — which
> would have silently disabled the entire origin check. Every block below
> therefore resolves the interpreter by TRYING it, never by looking for it.
>
> **PowerShell does not work, and it is worse than a stray newline.** `abc`
> arrives at a NATIVE executable as **8 bytes** — `ef bb bf 61 62 63 0d 0a`.
> But on Windows `gcloud` is itself a `.ps1` that re-pipes (`gcloud.ps1:118`,
> `$input | & "$exe_path"`), so the payload is terminated TWICE. Measured end
> to end through the real `gcloud` into a real Secret Manager on 2026-09-11:
> ten ASCII characters were stored as **16 bytes**,
> `efbbbf 6162636465666768696a 0d 0d 0a` — the BOM, the payload, then the CR
> left over from the first hop followed by the second hop's CRLF. Three
> mechanisms, not one: a CRLF appended once per pipeline OBJECT (unconditional, in every
> idiom tested, including native→native and including when the upstream
> program emitted nothing); a 3-byte BOM prepended, which comes from
> `[Console]::InputEncoding`; and `$OutputEncoding` re-encoding the payload —
> Windows PowerShell 5.1 defaults it to **ASCII** (code page 20127), so an
> accented character silently becomes `?`. That last one corrupts the
> CONTENT, not just the tail. PowerShell 7 was not installed and therefore
> not measured; do not assume it is safe.
>
> **Google Cloud Shell remains a good second venue** — a clean Linux
> userland, no trace on your own disk, reachable from a phone. What it does
> NOT buy is better audit attribution: a local `gcloud` authenticated as you
> already writes as you. This document claimed otherwise until 2026-09-11.
> The audit log settles it — `cf-origin-secret` version 1 was written from a
> Windows laptop by the local CLI and Cloud Audit attributed it to the
> practitioner, as it did for 26 of 26 human secret writes over 400 days.

**First, create the six empty containers.** `gcloud secrets create` without
`--data-file` makes the container and no version; the first version is written
by the per-secret blocks below.

```bash
REGION=northamerica-northeast1   # the SAME region as §6.2

for s in flask-secret-key portail-secret-key firebase-api-key dav-password-hash cf-origin-secret graph-client-secret; do
  gcloud secrets create $s --project=$PROJECT \
    --replication-policy=user-managed --locations=$REGION
done
```

> **Replication is permanent per secret.** `user-managed` pinned to the App
> Engine region keeps the payloads in one jurisdiction, which for a law
> practice is a data-residency question rather than a preference.
> `--replication-policy=automatic` is the alternative and cannot be changed
> afterwards — you would have to create a new secret under a new name.

**Then write the first version of each.** These are the same commands the
Configuration page renders for a rotation; on a rotation you run only these,
never the `create` above.

#### `flask-secret-key` — Clé de signature des sessions Flask

If this value is wrong or absent: l'application NE DÉMARRE PAS.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add flask-secret-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=flask-secret-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `portail-secret-key` — Clé de session du service « portail »

If this value is wrong or absent: le service « portail » NE DÉMARRE PAS.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add portail-secret-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=portail-secret-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `firebase-api-key` — Clé d'API navigateur Firebase

If this value is wrong or absent: la page de connexion ne peut pas initialiser Firebase.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR, pas les perdre en silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six secrets sont d'une seule ligne par construction, donc un collage multiligne signifie qu'on a collé la mauvaise chose — et le collage tronqué a l'air complet à l'écran. C'est la mesure qui l'attrape, pas l'œil.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 3. Mesurer, puis écrire SEULEMENT si la longueur est entre 30 et 60 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add firebase-api-key \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=firebase-api-key \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `dav-password-hash` — Empreinte bcrypt du mot de passe DAV

If this value is wrong or absent: l'authentification DAV ne peut pas réussir — DavX5 cesse de synchroniser SANS message d'erreur.

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Vérifier que bcrypt est présent DANS cet interpréteur — et non ailleurs. L'ancienne ligne `-m pip install` ne marche pas partout : le venv de ce dépôt n'a pas `pip` du tout (« No module named pip », mesuré), et bcrypt y est déjà puisque c'est une dépendance épinglée. On constate donc au lieu d'installer à l'aveugle.
"$PY" -c 'import bcrypt' 2>/dev/null && printf 'bcrypt : présent\n' || printf 'bcrypt ABSENT de cet interpréteur — à installer avant de continuer\n'

# 3. Calculer l'empreinte. Le mot de passe n'est jamais un argument ni une variable — `getpass` le lit du terminal. L'EMPREINTE, elle, peut vivre dans une variable : c'est un condensé, et c'est ce qui permet de la mesurer AVANT de l'écrire, contrôle que la forme fusionnée précédente ne pouvait pas offrir.
EMPREINTE=$("$PY" -c 'import bcrypt, getpass, sys; sys.stdout.write(bcrypt.hashpw(getpass.getpass("Mot de passe DAV : ").encode(), bcrypt.gensalt()).decode())')

# 4. Mesurer, puis écrire SEULEMENT si la longueur est exactement 60. Une empreinte de 61 octets n'égalera jamais les 60 que `bcrypt.checkpw` recalcule, et DavX5 cesse alors de synchroniser sans un mot.
N=$(printf '%s' "$EMPREINTE" | wc -c | tr -d ' ')
if [ "$N" -eq 60 ]; then
  VERSION=$(printf '%s' "$EMPREINTE" | gcloud secrets versions add dav-password-hash \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer l'empreinte de la session.
unset EMPREINTE

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`, qui désignerait la version précédente si l'écriture avait échoué et rassurerait donc à tort.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=dav-password-hash \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -eq 60 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `cf-origin-secret` — Secret d'origine Cloudflare

If this value is wrong or absent: le contrôle d'origine est DÉSACTIVÉ en silence (l'accès direct à App Engine n'est plus bloqué).

```bash
# 1. Résoudre l'interpréteur en l'ESSAYANT, jamais en le cherchant. Sur Windows, `python3` est un raccourci du Microsoft Store : il EXISTE sur le PATH et sort en code 49 sans rien produire, si bien qu'un `command -v` le choisirait et que la valeur engendrée serait vide.
PY=""; for c in python3 python py; do "$c" -c '' >/dev/null 2>&1 && { PY=$c; break; }; done
if [ -n "$PY" ]; then
  printf 'interpréteur : %s\n' "$PY"
else
  printf 'aucun interpréteur Python utilisable — REFUSER\n'
fi

# 2. Frapper une valeur neuve. `sys.stdout.write` plutôt que `print` par discipline : aucune ligne de cette recette n'émet de saut de ligne. Et `secrets` plutôt qu'un tirage depuis `/dev/urandom` : c'est le générateur cryptographique de la bibliothèque standard, ce qui vaut de dépendre d'un interpréteur.
VALEUR=$("$PY" -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(32))')

# 3. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 4. Mesurer, puis écrire SEULEMENT si la longueur est entre 32 et 128 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add cf-origin-secret \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=cf-origin-secret \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

#### `graph-client-secret` — Secret client Microsoft Graph

If this value is wrong or absent: le courriel sortant est désactivé.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR, pas les perdre en silence. ⚠ `read` ne retient que la PREMIÈRE LIGNE : les six secrets sont d'une seule ligne par construction, donc un collage multiligne signifie qu'on a collé la mauvaise chose — et le collage tronqué a l'air complet à l'écran. C'est la mesure qui l'attrape, pas l'œil.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement.
printf '[%s]\n' "$VALEUR"

# 3. Mesurer, puis écrire SEULEMENT si la longueur est entre 20 et 256 octets. La mesure est une GARDE, pas un avis : la commande d'écriture vit DANS le `if`, donc coller le bloc entier ne peut pas écrire une valeur que la mesure vient de refuser. `printf` est une primitive du shell, donc la valeur ne devient l'argument d'aucun processus ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps` ; et `versions add`, jamais `secrets create` — le secret existe déjà.
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
  VERSION=$(printf '%s' "$VALEUR" | gcloud secrets versions add graph-client-secret \
    --project=$PROJECT --data-file=- --format='value(name)')
  printf 'écrit : %s octets, version %s\n' "$N" "${VERSION##*/}"
else
  VERSION=""
  printf "longueur %s octets — ÉCRITURE ANNULÉE, rien n'a été écrit\n" "$N"
fi

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire LA VERSION QU'ON VIENT D'ÉCRIRE — jamais `latest`. Si l'écriture avait échoué, `latest` désignerait la version précédente, qui a toutes les chances d'être de bonne longueur puisqu'elle fonctionnait : l'après-vol rassurerait alors sur une écriture qui n'a pas eu lieu.
if [ -z "$VERSION" ]; then
  printf 'aucune version écrite — rien à relire\n'
else
  N=$(gcloud secrets versions access "${VERSION##*/}" --secret=graph-client-secret \
    --project=$PROJECT | wc -c | tr -d ' ')
  if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
    printf 'relu : %s octets — conforme\n' "$N"
  else
    printf 'relu : %s octets — NE CORRESPOND PAS\n' "$N"
  fi
fi
```

Each block's last command does the verification itself — it reads the stored
value back and lets the SHELL compare the byte count, rather than printing a
number for you to check by eye.

**The byte count is the complete check, and `xxd | tail -1` is not.** This
document used to advise inspecting the last line of a hex dump for a trailing
`0a`. That only looks at the END. PowerShell's worst artefact is a 3-byte BOM
at the FRONT, which a tail inspection cannot see; a byte count sees both (3
added in front, 2 at the back). It also never prints the secret, which a hex
dump does.

The count is also the only thing that catches the paste failure an eye cannot:
`IFS= read -rs` keeps only the **first line** of a multi-line paste, and the
bracketed echo then displays a truncated value that looks perfectly complete.
Measured: 63 bytes pasted, 20 stored, brackets clean. All six secrets here are
single-line by construction, so a multi-line paste means the wrong thing was
copied.

Grant IAM. The two service accounts are the **App Engine default SA**
(`$PROJECT@appspot.gserviceaccount.com`) and whatever SA your **Cloud Build
trigger runs as** (see the note below):

```bash
AE_SA="$PROJECT@appspot.gserviceaccount.com"

# App Engine runtime SA:
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/cloudtrace.agent"
# `graph-client-secret` was MISSING from this loop. `Config.GRAPH_CLIENT_SECRET`
# is read with required=False, which swallows a PERMISSION_DENIED into "" — so
# a missing binding here turns outbound email (invitations, accusés, intake
# confirmations) off SILENTLY. `portail-secret-key` is deliberately absent: it
# belongs to the `portail` service account, not to this one.
for s in flask-secret-key firebase-api-key dav-password-hash cf-origin-secret graph-client-secret; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$AE_SA" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# `portail-secret-key` is granted to the PORTAL service account instead — it
# belongs to the second App Engine service, whose least-privilege SA is the
# whole point of keeping the two session keys apart:
#   gcloud secrets add-iam-policy-binding portail-secret-key \
#     --member="serviceAccount:portail-svc@$PROJECT.iam.gserviceaccount.com" \
#     --role="roles/secretmanager.secretAccessor" --project=$PROJECT
# The portal's full infrastructure (bucket, named database, queue, nine IAM
# grants) is not yet in this document — see CLAUDE.md « Portail client » until
# it is.

# REQUIRED for signed Storage URLs: the runtime SA must be able to sign as ITSELF
# (iam.signBlob self-impersonation). Without this, document & gabarit uploads and
# downloads silently fail to produce a signed URL in production.
gcloud iam service-accounts add-iam-policy-binding $AE_SA \
  --member="serviceAccount:$AE_SA" --role="roles/iam.serviceAccountTokenCreator" --project=$PROJECT
```

**Cloud Build SA note:** the original grants deploy rights to the Firebase Admin
SDK SA and configures the trigger to *run as* that SA. If you instead use the
default build identity, grant these to whichever SA your build actually runs as:

```bash
BUILD_SA="<the SA your Cloud Build trigger runs as>"
gcloud iam service-accounts add-iam-policy-binding $AE_SA \
  --member="serviceAccount:$BUILD_SA" --role="roles/iam.serviceAccountUser" --project=$PROJECT
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$BUILD_SA" --role="roles/appengine.appAdmin"
```

### 6.5 Firebase Auth — the single user + Phone MFA

In the Firebase console (there is no clean gcloud path for these):
1. **Authentication → Sign-in method:** enable **Email/Password**.
2. **Authentication → Sign-in method → Advanced:** enable **SMS Multi-factor**.
3. **Authentication → Users:** create **one** user whose email is exactly your
   `AUTHORIZED_USER_EMAIL`. Any other email is rejected at login with *"Accès
   non autorisé"*.
4. Log in once to enrol a phone as the second factor. With `REQUIRE_MFA=true`,
   any ID token lacking a second factor is refused.

> **Lockout warning:** losing the enrolled phone locks you out. Keep Firebase
> console access as your recovery path.

#### 6.5.1 Recovering from a lockout

With `REQUIRE_MFA=true`, `auth.py` refuses any ID token lacking
`sign_in_second_factor`. So with zero enrolled factors, sign-in succeeds at
Firebase and then `/auth/verify-token` refuses the session — and because
`/parametres/securite` is behind `@login_required`, **the application cannot
repair its own lockout.** Recovery is always out of band, in this order:

1. **Fastest, and the only path that needs nothing but deploy access.** Set
   `REQUIRE_MFA: "false"` in `app.yaml`, deploy, log in with the password
   alone, open **Paramètres → Sécurité**, enrol a number, then set it back to
   `"true"` and deploy again. The account is password-only for the duration
   of that window.
2. **Firebase console** → Authentication → Users → the account → manage
   second factors.
3. **Both paths run through the Google account**, whose own 2FA and recovery
   codes are the real single point of failure. Print those codes and store
   them off this machine. Athena's lockout risk is downstream of that, not
   independent of it.

The application is built so it can only ever *refuse* to create this state:
the last enrolled factor cannot be removed while `REQUIRE_MFA` is on, and
changing a number is additive (enrol the new one, verify by logging in, only
then remove the old).

#### 6.5.2 Manual verification of « Paramètres → Sécurité »

Nearly all of this page is client-side JavaScript, which the pytest suite
**cannot execute** (there is no jest/jsdom/Playwright in this repo). The
automated tests cover routing, CSRF, nonce discipline, config plumbing and
the journal contract — **not one line** of the re-auth dance, the enrolment
ordering, or the guards. This list is what covers the rest.

Run it on a quiet day, with the Firebase console open in another tab and
§6.5.1 printed on paper. **Never exercise the destructive paths against the
real `AUTHORIZED_USER_EMAIL`** — mint a throwaway Firebase user and point
`AUTHORIZED_USER_EMAIL` at it on a `gcloud app deploy --no-promote` version.

| # | Check |
|---|---|
| **M0** | **GATE — run before the removal branch is trusted.** Enrol a **second** phone factor while the first is enrolled. Record `enrolledFactors.length` and whether the next login offers a choice of hints. If enrolling a second factor fails on this project, the in-app number change must not be used at all — correct from the console instead. |
| M1 | Measure the recent-login window: unlock, wait 6 min, attempt an enrol. The page re-locks after 5 min by design; confirm the message is legible rather than a raw error. |
| M2 | Local dev with `RECAPTCHA_ENTERPRISE_SITE_KEY` **unset**: the page loads, the console shows no `firebase is not defined`, and one of the four state panels renders. |
| M3 | Key **set**: the page loads **and** App Check still works elsewhere — load `/dossiers` and confirm an htmx request still carries `X-Firebase-AppCheck`. Proves the guarded `initializeApp` did not create a second app and orphan `appcheck-boot`'s App Check instance. |
| M4 | Delete IndexedDB `firebaseLocalStorageDb` in DevTools **without** touching cookies, reload → must show « Reconnexion nécessaire », never « aucun facteur ». |
| M5 | Tamper with the rendered `athenaUid` → « Identité différente », every mutation refused. |
| M6 | Password change happy path, then **log out and log back in with the new password** — the only real proof. |
| M7 | Wrong current password → « Mot de passe actuel incorrect. » and **no SMS is sent** (a wasted SMS costs money and is an abuse vector). |
| M8 | Three wrong SMS codes then the right one → succeeds, **without** needing a resend. |
| M9 | Let a code expire → resend becomes immediately available → the resent code works. Confirm in the Network tab that a **new** reCAPTCHA token was minted. |
| **M10** | **Full number change:** unlock → add new → confirm the echo → code → **two factors visible** → log out → log in with the **NEW** number → return → remove the old → log out → log in again. Two complete login cycles. |
| M11 | Typo'd new number (a valid number you also control): the code never arrives, abandon → **the old factor is still enrolled and login still works.** |
| M12 | With one factor and `REQUIRE_MFA=true`: the Retirer control is disabled with a visible reason — then **re-enable it in DevTools and click it**; the JS function itself must refuse. |
| M13 | Six SMS in a few minutes → « Trop de demandes de code » and the cooldown **re-arms**. |
| M14 | Cloud Logging: one `pallas.auth` entry per event; no phone number, email or token in `jsonPayload`. |
| M15 | Zero CSP violations in the console; nothing arrives at `/csp-report`. |
| M16 | 375 px: the six code inputs fit and every touch target clears 44 px. |
| M17 | Profile: change the fax, then generate a budget PDF **and** a gabarit containing `{{cabinet.telecopieur}}` — both must show it. Confirm the accusé bordereau reads « Montréal (Québec) » and the phone still reads `(514) 737-2525` with no `+1 `. |
| M18 | `python -m scripts.check_config` still passes. |
| **M19** | **Look at the sidebar in a browser and confirm a cog, not the literal word « settings ».** No test can catch this: `tests/test_icons.py` compares templates against the Python set and never reads the font's glyph table. |

Get `FIREBASE_APP_ID` and the browser API key from **Project settings → Your
apps → Web app** (register one if none exists). Put the app id in `app.yaml`
and the API key in the `firebase-api-key` secret.

### 6.6 Firebase Storage

Initialize the default bucket (Firebase console → **Storage → Get started**).
Its name must equal `FIREBASE_STORAGE_BUCKET`.

**Then, and only then, deploy its rules** — the bucket has to exist first:

```bash
# From the repo root:
firebase deploy --only storage --project $PROJECT
```

The rules are deny-all; files are served only via 15-minute signed URLs, which
bypass rules because they are produced by the Admin SDK. Nothing in the
application reads Storage through a client SDK.

**Then give the bucket its ONE lifecycle rule: delete objects under
`staging/` after 7 days.** Every direct-to-GCS upload lands under
`staging/{uid}/…` first — the web upload form, a zip export, and (lot 2A) the
connector's upload tickets under `staging/{uid}/mcp/…` — and anything never
finalized stays there until this rule sweeps it. The connector's ticket
depends on it outright: a file PUT after its ticket's hour is never filed, and
this rule is what erases it. Check it (read-only — it changes nothing):

```bash
# The value of FIREBASE_STORAGE_BUCKET in athena/app.yaml.
FIREBASE_STORAGE_BUCKET=your-bucket-name
gcloud storage buckets describe "gs://${FIREBASE_STORAGE_BUCKET:?}" \
  --project=$PROJECT --format="json(lifecycle_config)"
```

The answer must hold a `Delete` rule with `"age": 7` and
`"matchesPrefix": ["staging/"]` — and **no other `Delete` rule that can reach
a live object**: this bucket holds the clients' documents under `users/`, so a
rule without a prefix, or on any other prefix, deletes them. (A rule limited to
NONCURRENT versions — `daysSinceNoncurrentTime`, `numNewerVersions`,
`isLive: false` — touches no live object and is fine.) If the staging rule is
missing, add it in the console (Cloud Storage → the bucket → *Lifecycle* →
*Add a rule*: delete, age 7 days, object name prefix `staging/`) rather than by
`gcloud storage buckets update --lifecycle-file`, which REPLACES the whole
configuration. `python -m scripts.provision --project=$PROJECT` reports it as
`cycle-de-vie-staging`, with the same two checks.

### 6.7 App Check + reCAPTCHA Enterprise (recommended)

```bash
gcloud recaptcha keys create --web --display-name=athena-appcheck \
  --domains=yourdomain.example --integration-type=score --project=$PROJECT
```

Register the Web App in **Firebase console → App Check** with the reCAPTCHA
Enterprise provider, and put the site key in `RECAPTCHA_ENTERPRISE_SITE_KEY`
(`app.yaml`). App Check is **fail-open** if unset — the app still runs, but logs
a loud warning in production and does not verify HTMX requests. For local dev,
register an App Check **debug token** and set `APPCHECK_DEBUG_TOKEN`.

### 6.8 Data residency — keep application logs in your region (optional)

After §6.2/§6.3, your **primary data stores are already regional**: App Engine,
Firestore, and (from §6.6) the Storage bucket all live in
`northamerica-northeast1` (Montréal), and those regions are **permanent**. Two
Google-managed telemetry sinks, however, default to **`global`**:

| Sink | Default location | Movable? |
|---|---|---|
| Cloud Logging `_Default` bucket (your `pallas-athena` logs) | `global` | ✅ redirect to a regional bucket (below) |
| Cloud Logging `_Required` bucket (Admin Activity / System Event audit logs) | `global` | ❌ **permanent** — Google-imposed |
| Cloud Trace | Google-managed (global) | ❌ no regional-storage control |

A log bucket's location is fixed at creation, so the existing `global` buckets
can't be *moved* — you create a **new regional bucket** and route future logs to
it. Application logs are PII-redacted at source (`RedactionFilter`), so this is a
residency-hygiene step, not a leak fix.

```bash
# 1. Create a Montréal log bucket:
gcloud logging buckets create athena-mtl \
  --location=northamerica-northeast1 --retention-days=30 --project=$PROJECT

# 2. Repoint the _Default sink at it (new logs land in Montréal):
gcloud logging sinks update _Default \
  logging.googleapis.com/projects/$PROJECT/locations/northamerica-northeast1/buckets/athena-mtl \
  --project=$PROJECT

# 3. Verify:
gcloud logging sinks describe _Default --project=$PROJECT
gcloud logging buckets list --project=$PROJECT
```

- Only logs written **after** the change go to Montréal; the old `global`
  `_Default` contents age out on their retention window.
- The `_Required` audit bucket stays `global` — it holds project-admin metadata
  (who called which API), never client data. There is no supported way to
  regionalize it on an existing project.
- Cloud Trace has no equivalent regional-bucket control; traces are
  PII-sanitized (`tracing_setup.py`) and carry IDs/counts only.

---

## 7. Cloudflare edge

All traffic must reach App Engine **through** Cloudflare; the app actively
rejects direct access. Set up, in order:

1. **DNS:** add your zone in Cloudflare, then a **proxied** (orange-cloud)
   record for `yourdomain.example` pointing at your App Engine custom-domain
   target (map the domain first under App Engine → Settings → Custom domains).
2. **SSL/TLS:** set the mode to **Full (Strict)** and install an **Origin
   Certificate** on App Engine's custom domain.
3. **App Engine firewall:** allow **Cloudflare's published IP ranges**, and
   **`0.1.0.2/32` at a higher priority**, then set the `default` rule to DENY —
   in that order. ⚠ This step said « Cloudflare ranges **only** » until
   2026-09-12, and following it literally **kills all three cron jobs and the
   Cloud Tasks queue**: App Engine dispatches them from the internal address
   `0.1.0.2`, which is not a Cloudflare range. The live deployment carries 24
   rules — `0.1.0.2/32` at priority 10, 22 Cloudflare ranges at 100–360, and
   the implicit `default` DENY (read 2026-09-12). Set the deny LAST: flipping
   it before the allows locks you out of your own origin. Cloudflare publishes
   new ranges over time and this repository carries no copy of the list — take
   it from `cloudflare.com/ips` at provisioning time, and never delete a range
   you do not recognise, since it may be your own office IP.
4. **Origin secret (Transform Rule):** add a request Transform Rule that injects
   header `X-Origin-Auth: <cf-origin-secret value>` on every request, zone-wide.
   The app checks it (`security.py`) when `CF_ORIGIN_SECRET` is set. This is the
   second layer that defeats a spoofed-Host direct hit.
   ⚠ **Do this while the secret is still an EMPTY container, and prove the rule
   fires before giving it a value** — Cloudflare's request tracer
   (`POST /accounts/{id}/request-tracer/trace`), never inference. The reverse
   order 403s the entire zone with no deploy to explain it; see §5 step 13.
5. **Rocket Loader:** leave it **off** — the original enabled it, then disabled
   it at the edge on 2026-07-11, and it is not returning. ⚠️ The end-of-`<body>`
   script order in `base.html` is still load-bearing: the App Check boot runs synchronously and htmx/Alpine
   are `defer`, but all resolve in document order — don't reorder them.
6. **Early Hints:** enable it (the app emits the `Link` preload headers).
7. Point `MCP_CANONICAL_ORIGIN` and `AUTHORIZED_USER_EMAIL`'s domain at
   `yourdomain.example`, and update the legal pages / README / SECURITY.

---

## 8. First deploy + smoke test

**Deploy** either manually, or by connecting a Cloud Build trigger on push to
`main`.

```bash
cd athena
gcloud app deploy app.yaml --project=$PROJECT
```

⚠ **The trigger does not do one deploy, it does four**, and this section said
one until 2026-09-12. `cloudbuild.yaml` runs the pytest gate, then deploys
`app.yaml`, `portail.yaml`, `dispatch.yaml` and `cron.yaml` in that order, then
prunes old versions **per service**. Three consequences for a first build:

- **Step 2b fails if `portail-svc` does not exist.** The portal's service
  account and its IAM must be provisioned before the trigger is ever fired —
  and that checklist is in `CLAUDE.md` « Portail client », not yet here.
- **Step 2d needs `cloudscheduler.googleapis.com`** (§6.1). Without it the
  build fails `SERVICE_DISABLED` *after* three services are already deployed.
- **`dispatch.yaml` and `cron.yaml` each REPLACE their whole table.** The two
  files at the repo root are the single complete source; a partial file
  silently removes the rules or jobs it omits.

Deploying `app.yaml` by hand, as above, is the safe way to get the first
service up before any of that exists.

**Smoke test:**
- Visit `https://yourdomain.example` — the login page loads through Cloudflare.
- `gcloud app browse` (direct `*.appspot.com`) should be **403** — proof the
  edge defenses work.
- Log in as the single user; complete Phone MFA.
- Create a dossier, upload a document (verifies signed URLs / the signBlob role
  from §6.4), and confirm it downloads.
- Check **Cloud Logging** for the log name `pallas-athena`.

---

## 9. Seed Québec reference data

Populates courthouse (`ref_greffes`) and tribunal (`ref_juridictions`)
collections used by court-file-number parsing. Idempotent.

```bash
cd athena
# Needs Application Default Credentials — this script does NOT read .env:
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
python -m scripts.seed_reference_data
```

> If you are adapting to another jurisdiction (§13), edit the data in
> `scripts/seed_reference_data.py` first.

---

## 10. Optional — DavX5 calendar/contacts sync

Skip this if you don't need Android sync. If you keep it:
- In DavX5, add the account by URL (`https://yourdomain.example/`), with HTTP
  Basic credentials: username = `AUTHORIZED_USER_EMAIL`, password = the plaintext
  whose bcrypt hash is in `dav-password-hash`.
- `/dav/*` is defended by the App Engine firewall, the origin-secret check, the
  bcrypt Basic auth and a per-IP brute-force brake. **Do not put Cloudflare
  Access in front of it** without reading this first: DavX5 has no
  custom-header setting, so a service token cannot be presented (verified on
  device, 2026-08-11). Its only mTLS-capable path is a *client certificate*
  (Advanced login → select a certificate from the Android KeyChain) — and
  Cloudflare enables mTLS **per hostname, not per path**, so switching it on
  for a host that also serves the web UI and the MCP endpoint affects every
  client of that host. If you want Access on DAV, give DAV its own hostname.

---

## 11. Optional — MCP connector for Claude

Skip entirely by setting `MCP_ENABLED=false` (all `/mcp` + `/oauth/*` routes
404). If you keep it:

```bash
gcloud firestore fields ttls update expire_at --collection-group=oauth_codes  --enable-ttl --project=$PROJECT
gcloud firestore fields ttls update expire_at --collection-group=oauth_tokens --enable-ttl --project=$PROJECT
```

(TTL is garbage collection only — expiry is enforced in code regardless.)

- If you use Cloudflare Access at all, ensure it **does NOT cover** `/mcp`, `/oauth/*`, or
  `/.well-known/oauth*` (only `/dav/*`).
- Add a Cloudflare **Configuration Rule** disabling Browser Integrity Check on
  those paths (Claude's server is not a browser); watch Security → Events for
  Super Bot Fight Mode challenging Anthropic's egress.
- In claude.ai: **Settings → Connectors → Add custom connector →**
  `https://yourdomain.example/mcp`, then complete Firebase login + MFA on the
  consent screen and click **« Autoriser »**.
- **Read the consent screen before ticking.** Its write block — one
  paragraph per write family, then the list of what the connector can
  NEVER do, then the box's summary — is assembled from
  `athena/mcp/disclosure.py`, the same registry INSTRUCTIONS come from, and
  each « never » is backed by a sweep of the connector's code. The scope is
  frozen when you click « Autoriser »: a later release that changes what a
  write tool can do (a new family, a lifted « never », or a behaviour change
  such as lot 0a's `complete_task` refusing to reopen a closed task) reaches
  the token you already hold **silently**. So for such a release:
  `python -m scripts.revoke_mcp_tokens` and remove the connector in
  claude.ai BEFORE pushing, deploy, then re-add it and tick the boxes under
  the new text. **That manual revoke → deploy → re-consent sequence is the
  ONLY control that keeps a token granted before the MCP write-expansion
  program (lots 0a to 5) from reaching its new write tools**: there is no
  code gate — no version stamped on a token, no check at `tools/call` that
  a grant predates the tool it calls — by the lawyer's decision D19
  (2026-09-29). Skip it, and every new `athena:write` tool is live for the
  token in force the moment the deploy lands. (The accounting tools are
  the exception by construction: they need `athena:comptabilite`, a scope
  no token held before lot 5b.) §15 « Déploiement unique (D22) », step 5,
  is where this release runs it. Keep `MCP_WRITE_ENABLED` at `"true"` for that re-consent —
  consenting while it is `"false"` offers no box and yields a read-only
  grant, without a word. This is the single-deploy form of the arm/disarm
  procedure in the `MCP_WRITE_ENABLED` comment of `app.yaml` (§4.1's
  pointer): with every token revoked BEFORE the push, nothing holds a grant
  the new surface could reach, so the switch need not be armed. Either order
  works; the one that fails is re-consenting while the switch is armed.
- **The upload ticket needs an organisation setting of claude.ai** (lot 2A,
  plan D4): `begin_upload` hands Claude a WRITE-only upload link whose
  ticket files bytes for one hour only (the GCS session itself outlives it;
  a late PUT is never filed and the `staging/` rule sweeps it), and only the
  code sandbox can PUT the bytes to it — so the claude.ai
  organisation must allow code-execution network egress to
  `storage.googleapis.com` (§15 « Lot 2A », step 7). Without it the tickets
  open, every PUT fails, and they expire unused: nothing is ever filed.
- **An invoice the connector issues consumes a REAL number** (lot 3b,
  plan D3): `create_invoice` takes the year's next `YYYY-F###` inside the
  invoice's own transaction — a refusal consumes none, a success one for
  ever, voided or not. Never smoke-test it: §15 « Lot 3 », step 7, verifies
  a deploy with `preview_invoice` and no-op calls, and says how to check
  that `counters/invoices-{year}.seq` advances by exactly one per invoice
  really issued.
- **A dossier status set through the connector moves your PHONE, and a
  compliance check it writes is only PRESUMED** (lot 4b, plan D6 and D7):
  `set_dossier_status` runs the same DavX5 drain as the dossier form's save
  — closing or archiving takes the dossier's tasks, notes and events off
  the phone; when that drain does not finish the result says so, and asking
  the same status again (or « Resynchroniser le téléphone » on the dossier's
  page) repairs it. `record_kyc_status` stores an identity or conflict check
  as « inscrit par Claude — à confirmer »: it counts as NOT done — the
  coverage report keeps it open — until you click « Confirmer » on the
  contact's fiche, and it is refused over a check you decided yourself.
  §15 « Lot 4 » verifies both on test data.
- The consent screen has room for a **second, separate box**, « Autoriser la
  comptabilité » (scope `athena:comptabilite`). It appears only when
  `MCP_WRITE_ENABLED` and `MCP_COMPTABILITE_ENABLED` are both `true` **and**
  at least one tool carries that scope — six do since plan lot 5b (the
  ACCOUNTING family and `get_admin_ledger`). A write grant never reaches an
  accounting tool: each box grants its own scope, and only its own. The
  box lists what it grants — inscribing trust and administration entries
  (a fee payment, an encaissement: each RECORDS A PAYMENT on the invoice),
  correcting an editable administration entry, clearing at the statement
  date, reversing — and, in its own « jamais » list, what it still never
  does: delete an entry (a trust entry is corrected only by a reversal,
  an administration entry while it stays editable, then by a reversal),
  start, complete or abandon a reconciliation, create or modify an
  account, transfer funds between dossiers (or reverse one leg of such a
  transfer), withdraw trust funds in cash, back a fee payment with a
  paper invoice, an invoice not yet sent, one that imputes a provision,
  another client's invoice or — in a dossier of several clients — one
  naming none (D21), make a fee payment to anyone but you or
  your firm as « Paramètres » names you (D23), show a bank number. Arming
  it is its own train: §15 « Lot 5 », run as step 16 of « Déploiement
  unique (D22) ».
- **Run no MCP write during a deploy window — and none after a rollback
  past the idempotency claim** (plan lot 0a: `mcp/write_support.py` with its
  `pending` status). Since that release a write CLAIMS its `idempotency_key`
  before it executes: the `mcp_idempotency` entry is created `pending`, then
  finalized `committed` (with the result) or `partial` (the write committed,
  a later step failed — the caller was told « NE PAS RÉESSAYER »). Code from
  before that release knows neither state: it reads a `pending` or `partial`
  entry as « no stored result », executes the write again, and overwrites
  the entry. So while two versions serve traffic side by side (a deploy in
  progress, a traffic split) or after migrating traffic back to an older
  version, a same-key retry that the new code would refuse or re-report can
  be DUPLICATED by the old one. The window is short and needs a retry to
  land on the other version, but a scheduled Claude job retries by design:
  pause those jobs for the deploy, and after a rollback past this release let
  the 24 h `expire_at` run out (or revoke the connector) before writing
  again. Nothing to migrate in the other direction — the new code replays a
  legacy entry (no `status`) unchanged.

---

## 11b. Retiré — Assistant IA interne (Vertex, 2026-08-26 → 2026-09-02)

Cette section décrivait la mise en service d'un client de conversation
Claude/Gemini sur **Vertex AI** sous le projet du cabinet, sur un TROISIÈME
service App Engine, avec sa file Cloud Tasks `chat-turns`, son entrée cron,
ses deux secrets de Workers juridiques et son `roles/aiplatform.user`.

**Il a été retiré le 2026-09-02** : le cabinet est passé à un compte **Claude
for Work couvert par une entente de traitement des données**, ce qui retire à
ce clavardage sa seule raison d'être (que le matériel privilégié ne transite
pas par un produit grand public). Le service, la file, l'entrée cron et les
données ont été supprimés ; il n'y a plus rien à provisionner ici.

**Ce qui SURVIT de cette section, et qu'il faut garder :** le **Firestore
Data Access audit logging** activé à son étape 1. Il était le contrôle
compensatoire du registre ; il est désormais la trace de son effacement, et
il ne coûte rien. Ne pas le désactiver.

L'analyse documentaire, elle, ne dépend plus de rien ici : la compétence se
colle dans un Skill claude.ai, et les outils MCP `get_document_text` +
`record_document_analysis` (§11) suffisent à la boucle complète.

---

## 12. Optional — Android TWA

Only if you want an installable Android app. Build the TWA (e.g. with
[Bubblewrap](https://github.com/GoogleChromeLabs/bubblewrap)), enrol it in Play
App Signing, then replace the `package_name` and `sha256_cert_fingerprints` in
the `assetlinks.json` route in [main.py](athena/main.py) with **your** package
name and Play App Signing SHA-256, and redeploy.

---

## 13. Adapting to another jurisdiction / language / user model

This app is Québec-, French-, and single-user-specific. Honest scope:

- **Taxes:** `models/invoice.py` hardcodes GST 5 % / QST 9.975 % (non-compounded).
  Replace with your jurisdiction's rates/rules.
- **Judicial deadlines:** `utils/deadlines.py` implements art. 83 C.p.c. + Québec
  statutory holidays. Replace with your rules.
- **Court files & reference data:** `models/reference.py` +
  `scripts/seed_reference_data.py` (courthouse/tribunal parsing and data).
- **Protocol templates:** `models/protocol.py` (CQ/CS templates).
- **Language:** all UI text is hardcoded French with no i18n framework —
  translating is a template-wide effort, not a config toggle.
- **Multi-user:** the single-user model is enforced server-side and the Firestore
  collections are flat (not user-scoped). Multi-tenancy is a substantial rewrite.

---

## 14. Local development

```bash
cp .env.example .env        # then fill it in (see §4.1); PowerShell: Copy-Item
pip install -r athena/requirements.txt
pip install -r athena/requirements-dev.txt

cd athena
python -m scripts.check_config       # sanity-check your .env
flask --app main run --debug         # http://127.0.0.1:5000
python -m pytest tests/ -q
```

⚠ **`flask run --debug` alone does not work here, and its error message is
the giveaway.** There is no `app.py`, no `wsgi.py` and no `FLASK_APP`, so
Flask answers *« Could not locate a Flask application »*. The app factory
lives in `main.py`, hence `--app main`. Measured 2026-09-12, both forms.

⚠ **`gunicorn` does not run on Windows** — it imports `fcntl`, which is
POSIX-only (`ModuleNotFoundError: No module named 'fcntl'`, measured). The
production-like check is a Linux/macOS/WSL step; on Windows the deploy
itself is the first place the `app.yaml` entrypoint is exercised. On a POSIX
box it is `gunicorn -b :8080 main:app`, run from `athena/`.

Notes:
- **Firestore emulator** (`gcloud emulators firestore start`) requires exporting
  `FIRESTORE_EMULATOR_HOST` before running Flask/scripts — otherwise the Admin
  SDK targets **live** Firestore via Application Default Credentials.
  ⚠ And point it at something that is actually **listening**: `main.py`
  registers a context processor for the Réception badge that runs a Firestore
  aggregation on **every template render**, and its fail-open catches
  exceptions, not an unreachable host. Aimed at a dead port, every page of the
  application therefore **hangs** instead of erroring — measured 2026-09-12,
  stack captured at `models/portail_invitation.compter_soumises`.
- `scripts/seed_reference_data.py` does **not** read `.env`; it needs
  `GOOGLE_APPLICATION_CREDENTIALS` / ADC.
- For local MCP testing, run on `:8080` (or set `MCP_CANONICAL_ORIGIN` to match
  your port) and mint a dev token: `python -m scripts.mint_dev_token`.
- If you have not enrolled Phone MFA locally, set `REQUIRE_MFA=false` in `.env`.

---

## 15. Operations

- **Backups / DR:** see [SECURITY.md](SECURITY.md#data-handling). Configure
  Firestore scheduled backups (or Point-in-Time Recovery) and Firebase Storage
  object versioning for *your* project — they are per-project settings, not code.
- **Monitoring:** Cloud Logging (log name `pallas-athena`) and Cloud Trace. The
  event/span vocabulary is in [OBSERVABILITY.md](athena/OBSERVABILITY.md). Logs
  default to the `global` region — see §6.8 to keep them in Montréal.
- **CSP:** **enforced** (since 2026-07-11, after a report-only window showed
  only `script-src` violations). `script-src` uses a per-request nonce
  (`build_csp` in `security.py`, no `'unsafe-inline'`); violations still post to
  `/csp-report` (`report-uri`) and log as `csp_violation`.
- **Rollback:** Cloud Build keeps the 3 most-recent non-serving versions.
  `gcloud app versions list`, then migrate traffic:
  `gcloud app services set-traffic default --splits=<VERSION>=1`.
  ⚠ Rolling back past the idempotency claim (lot 0a) hands the MCP entries to code
  that does not know the `pending`/`partial` states and will execute a
  same-key retry again — see the last bullet of §11 before writing through
  the connector on the older version.
- **Déploiement unique (D22) — the whole MCP write-expansion program in ONE
  release (branch `mcp-ecriture-finitions`: lots 0a to 5 and the finitions,
  none of them deployed yet).** The lawyer's decision D22 (2026-09-29): the
  final branch is pushed ONCE, under ONE consent train, and the accounting
  switch is flipped LATER, on its own, after a supervised pilot. The per-lot
  bullets below keep their detail and their recipes; each step here names
  the one it runs. Where they differ — each lot's own « push the lot as ONE
  deploy », its own revocation and its own re-consent — **this order
  wins**: the revocations of lots 1b to 4 collapse into step 5, every
  lot's push into step 6, their re-consents into step 7 — lot 5's arming,
  revocation and re-consent are steps 16 and 17. What the release ships: **80** tools under the
  write grant (31 read, 49 write) and six accounting tools hidden behind
  `MCP_COMPTABILITE_ENABLED: "false"`; one TTL `fieldOverride`
  (`mcp_upload_tickets.expire_at`) and no composite index; no dependency;
  no `cron.yaml` or `firestore.rules` change; no DavX5 account re-add.
  Already done, nothing to redo (plan « Ops », 2026-09-29): the claude.ai
  organisation allows code-execution egress to `storage.googleapis.com`
  (« Lot 2A », step 7), and the canonical bucket's `staging/` 7-day rule was
  verified on 2026-09-28 (« Lot 2A », step 3). Left as they are, by the
  lawyer's decision: the two fee payments written before the D-4 rule
  (trust sequences 28 and 42, 1 000,00 $) — `verify_trust_integrity`
  reports them as NOTES, never repaired. Deferred by the lawyer, not a
  blocker: the lot 2B pilot on one real letter (« Lot 2B », step 4) — until
  it has run, do not rely on `create_template` with `substitutions` for a
  gabarit the practice will send from.

  **Before the push** — read-only first, then the two prerequisites that
  must precede the code:
  1. *Measure, read-only*, the day of the push (the environment of the T3
     recipe below: ADC, inline variables, never `ENV=production`). Each was
     clean on 2026-09-28/29; the registers have moved since, so run them
     again:
     - `python -m scripts.verify_trust_integrity` — exit `0`, or `2` with
       every note read with the lawyer (the two pre-D-4 fee payments above
       among them; the notes of check 8 describe history — « Finitions »,
       item 5);
     - `python -m scripts.verify_admin_integrity` — exit `0`; check 8
       (`amount_paid` == Σ receipts, over EVERY invoice carrying a payment)
       and check 9 (a kind stored against its direction) above all
       (« Administration direction (lot 0b) », « Lot 3 » step 2, « Lot 5 »
       step 0);
     - the lot 4 measurement (« Lot 4 », step 1: a contact on both sides of
       a dossier, a representation the forward rule refuses) — no output is
       the expected answer;
     - the storage-identity listing (« Storage identity (lot 0a) »:
       `users/unknown/`, `staging/unknown/`) — « matched no objects »;
     - « Paramètres → Profil du cabinet » carries both tax numbers
       (« Lot 3 », step 1) and the lawyer's and the firm's names (D23: a
       profile naming neither refuses every fee payment — « Finitions »,
       item 5).
     Any écart: stop, and decide it with the lawyer before going further.
  2. *The index file*, from the repo root of THIS branch:
     `firebase deploy --only firestore:indexes --project $PROJECT`, then the
     read-only TTL listing until it reads `ACTIVE` (« Lot 2A », step 1).
     Garbage collection only — first so it is never forgotten.
  3. *The designation of the active gabarits, against production, from a
     checkout of THIS branch* — the script ships with the change it
     prepares, so `main` does not have it until the push:
     `python -m scripts.designer_gabarits_actifs` (simulation), `--apply`,
     then the simulation again — every kind « déjà désigné », no « [!] »
     line (the recipe: « Active gabarits (lot 2A, step T3) »). The old code
     ignores the field, so this opens no outage; pushing FIRST would — every
     note d'honoraires and note print refuses until it runs. From here to
     the push, **nobody edits or uploads a note-d'honoraires or note-print
     template** (the old code still picks by recency, and the new one will
     print the designated one); re-run the simulation right before step 6.
  4. *Merge `main` into the branch.* `main` carries one commit the branch
     lacks, `f69663e` — the same change as the branch's first commit
     `861c6b8` (the same parent `21012c0`, identical trees), so the merge
     brings no content and cannot conflict. Run the suite on the merge
     result (`python -m pytest tests/ -q -p no:cacheprovider`, from
     `athena/`) BEFORE step 5: Cloud Build runs it again as the gate, but a
     red build after the revocation leaves the connector down.
  5. *Revoke and disconnect — BEFORE the push*:
     `python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai. **This step is the ONLY control that keeps a token granted
     before the program from reaching its new write tools** — there is no
     code gate (the lawyer's decision D19, 2026-09-29; §11): skipped, every
     tool of lots 1b to 4 is live for the token in force the moment the
     deploy lands, under a consent screen that never described it. Pause
     any scheduled Claude job for the deploy window (§11, last bullet).
     `MCP_WRITE_ENABLED` stays `"true"`.

  **The push:**
  6. *ONE push of the merged branch to `main`*, `app.yaml` reading
     `MCP_ENABLED: "true"`, `MCP_WRITE_ENABLED: "true"` and
     `MCP_COMPTABILITE_ENABLED: "false"` (the branch ships that way — check
     it). Cloud Build runs the suite as the gate. Never lot by lot: each
     push would put new tools under the text consented to before it.
  7. *Re-add the connector and READ the new screen before ticking
     « Autoriser les écritures »*: the blocks each lot's train lists
     (« Lot 1b » step 3, « Lot 2A » step 6, « Lot 2B » step 3, « Lot 3 »
     step 6, « Lot 4 » step 5). The accounting box is NOT offered — its
     switch is off — and that is expected. Re-consenting while
     `MCP_WRITE_ENABLED` is `"false"` would yield a read-only grant without
     a word.
  8. *Verify `tools/list`*: **80** tools (31 read, 49 write), no accounting
     tool, `decide_rendez_vous` alone carrying `openWorldHint: true`; and
     the `initialize` text opens on the SAFETY CORE (« Pallas Athena is a
     single-user … SAFETY CORE — read it before ANY write. NEVER, whatever
     the tool: … »), 8 000 bytes at most. The deploy gate pins both, and
     the descriptor budget (`tests/test_mcp_descriptor_budget.py`: about
     257 KB of its 280 KB cap).

  **After the push** — checks that write nothing, or only on test data,
  but for ONE deletable draft (step 12). They share their test data, so
  keep it until step 14 says to delete it:
  9. *DavX5, on the wire then on the device* (Change Impact item 2 — it
     fails silently): on TWO test dossiers, both `actif`, the first with
     one task, one note and one confirmed event and a TEST contact of role
     « client » as its only client (the set-up of « Lot 4 », step 2) — the
     lot 4 curl checks (« Lot 4 », step 6: `root` — 207, as many dossier
     collections as actif + en_attente dossiers —, the Depth:0 PROPFIND —
     207 —, and the `report` counts); a PROPFIND Depth:1 on one active
     dossier collection and on `/dav/addressbook/` (« Finitions », item 1);
     and the wire checks of lot 0a/0b — a test task moved from the first
     test dossier to the second (« DAV relocation »), a contact and a task
     created by PUT (« Contacts created on the phone », « Tasks,
     hearings… » item 1).
  10. *A phone edit*, on the test dossiers' events and tasks only:
      « Finitions » item 3 (a line typed after the metadata block of an
      event and of a task survives, and no block is re-imported; the event
      moved from one test dossier's calendar to the other's), and « Tasks,
      hearings… » item 3 (a « Reportée » test hearing moved on the phone
      stays « Reportée »).
  11. *The Outlook mirror*: « Lot 1b », C1, on a scratch event of a test
      dossier — a reschedule through `update_hearing` moves its Outlook
      copy within 10 minutes (`outlook_mirror: "follows"`), a cancellation
      removes it.
  12. *Word opens every generated document WITHOUT repair* (Change Impact
      item 3 — no test sees Word's prompt): the « Lot 2A » step 9 list, on
      a test dossier; a document drawn from a SCRATCH template whose first
      version was restored on its page (« Active gabarits »); and the note
      d'honoraires of « Lot 3 » step 3 (the web) and step 7e (the
      connector). That note needs a REAL invoice — a test invoice burns a
      number for ever (step 14) — so it files ONE draft into that invoice's
      dossier, in « Projets » (the connector's call returns the same note
      when it is identical, `reused: true`): the one write outside the test
      data. Delete it in the application once opened.
  13. *The upload ticket, once*, on a test dossier (« Lot 2A », step 8):
      one PDF through `begin_upload` → PUT → `finalize_upload`, and one
      deliberately wrong `md5_base64` that files nothing — note which of the
      two refusals happened.
  14. *No invoice number burned*: read `counters/invoices-{year}` before
      and after the « Lot 3 » step 7 smoke test (`preview_invoice`, the
      same-status `update_invoice` no-op, `get_budget` → an unchanged
      `create_budget_version`) — never `create_invoice`; the counter must
      read exactly what it read. Then delete the test data of steps 9 to 13
      in the application, in the order the deletion guards allow: the
      tasks, notes and events, the documents and their folders, then the
      two test dossiers (a dossier with a child record is refused), then
      the test contacts (a contact a dossier still names is refused), and
      the scratch template.
  15. *Tell the lawyer* what changed on the web (each lot's « Changements
      web à annoncer » in CLAUDE.md's Phase History; « Finitions », items
      4-6), and that the next REAL fee payment is the pilot of the one-
      transaction fee payment (« The fee payment is ONE transaction », its
      last paragraph — never a test at trust). The other per-lot device
      checks (lot 0b items 2-5, lot 1a L2-L4, lot 1b A-D) stay available:
      run first the ones he relies on. Watch the first week: the
      finitions' `unexpected` messages and the first void (« Invoice void
      (lot 0b) »). Update BOTH copies of the claude.ai skill `pallas-athena`
      the same day, with the lists of « Lot 1b », « Lot 2A », « Lot 2B »,
      « Lot 3 », « Lot 4 » and « Finitions » at once — every count reads 80
      (31 + 49); the accounting disciplines (« Lot 5 », step 9) wait for
      step 17. The same day, re-export and re-paste the claude.ai skill
      « Analyse documentaire » (`python -m
      scripts.exporter_competence_analyse` — « Finitions », its D25
      paragraph): it is generated, and any copy exported before the review
      of D25 says the analysis « remplace » the stored category without
      exception.

  **Later — the accounting switch, its own train, once the release has run
  clean:**
  16. First both integrity scripts again, ON the deployed version, before
      anything is armed (« Lot 5 », step 2 — the connector will write into
      these registers, and an écart there first could not later be told
      from its own). Then `MCP_COMPTABILITE_ENABLED: "true"` in `app.yaml`,
      and deploy (« Lot 5 », step 3). Nothing changes for the token in
      force: it lacks the scope, and sees neither the tools nor the box's
      text.
  17. Revoke and re-consent, ticking « Autoriser les écritures » AND
      « Autoriser la comptabilité » after reading its block (« Lot 5 »,
      step 4); verify **86** tools (32 read, 54 write) and the other token
      shapes (« Lot 5 », step 5). Then the skill's accounting lists
      (« Lot 5 », step 9).
  18. The supervised pilot on a TEST administration account — never the
      trust register, never the real operations account (« Lot 5 »,
      step 6) —, then both integrity scripts again (« Lot 5 », step 7).
      Only then may the first real bank movement be recorded through the
      connector.

  **Emergency switches** — each an `app.yaml` value read at startup, so a
  deploy: `MCP_COMPTABILITE_ENABLED: "false"` stops the six accounting
  tools only; `MCP_WRITE_ENABLED: "false"` stops every write, accounting
  included, the reads staying; `MCP_ENABLED: "false"` answers 404 on every
  `/mcp` and `/oauth/*` route. Faster, and without a deploy:
  `python -m scripts.revoke_mcp_tokens` — a write is refused at once (it
  re-reads its token, bypassing the cache), a read within the 5-minute
  success cache. A rollback: « Rollback » above — past lot 0a an older
  version executes a same-key retry again (§11, last bullet), past T3 it
  picks templates by recency again (« Active gabarits », last paragraph).
- **Storage identity (lot 0a, 2026-09-25):** every Storage path is now built
  under a uid that `utils/storage_identity.py` has validated, and nothing can
  write under `users/unknown/` or `staging/unknown/` any more — the routes used
  to read the uid as `session.get("user_id", "unknown")`, and
  `doc_template.update_template` fell back to `"unknown"` on a malformed stored
  path. Before deploying that change, and whenever you want to confirm nothing
  was ever written there, run this **read-only** check (it lists, it never
  deletes; « One or more URLs matched no objects » is the GOOD answer):

  ```bash
  # The value of FIREBASE_STORAGE_BUCKET in athena/app.yaml — this shell does
  # not read app.yaml. Left unset, the ":?" stops the command instead of
  # listing "gs:///users/unknown/", whose error is NOT the good answer.
  FIREBASE_STORAGE_BUCKET=your-bucket-name
  gcloud storage ls --recursive "gs://${FIREBASE_STORAGE_BUCKET:?}/users/unknown/" --project=$PROJECT
  gcloud storage ls --recursive "gs://${FIREBASE_STORAGE_BUCKET:?}/staging/unknown/" --project=$PROJECT
  ```

  If either lists objects, **stop and inventory them before deploying**: their
  Firestore `storage_path` still resolves (downloads keep working), but a
  template stored there can no longer have its FILE replaced — the model now
  refuses, rather than writing the new file under the same fallback prefix.
  Nothing here moves or deletes them for you.
- **DAV relocation (lot 0a, 2026-09-25):** moving a task or a note to another
  dossier in the web app now runs through `dav.sync.relocate_resource` (the
  same calls in the same order as the hand-written blocks it replaced — pinned
  by `tests/test_mcp_dav_resync.py`, but DavX5 fails silently, so check the
  wire once after the deploy). Move a test task from dossier `D1` to `D2` in
  the app (both `actif` or `en_attente` — a closed dossier's collection is
  drained, not listed), then ask both collections for a full `sync-collection` REPORT (curl
  prompts for the DAV password; it never goes on the command line):

  ```bash
  D1=first-dossier-id D2=second-dossier-id TASK=the-task-id
  DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
  BODY='<?xml version="1.0" encoding="utf-8"?><d:sync-collection xmlns:d="DAV:"><d:sync-token/><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
  for D in "$D1" "$D2"; do
    echo "== dossier-$D"
    # The multistatus is ONE line: -o keeps just this task's <D:response>.
    curl -s -u "${DAV_USER:?}" -X REPORT \
      -H "Content-Type: application/xml; charset=utf-8" --data "$BODY" \
      "https://yourdomain.example/dav/dossier-$D/" \
      | grep -o "<D:href>[^<]*$TASK\.ics</D:href>.\{0,120\}"
  done
  ```

  Expected: under `D1` the task's href is followed by
  `<D:status>HTTP/1.1 404 Not Found</D:status>` (its tombstone); under `D2` by
  a `<D:propstat>` carrying its `getetag` (live). A task still live under `D1`,
  or absent from `D2`, means the phone will keep the stale copy — stop and
  investigate before relying on DavX5.
- **Administration direction (lot 0b):** the model now DERIVES an
  administration entry's direction from its kind, and a web edit — which
  always names the kind — re-derives it. An « Autre recette » stored as a
  déboursé by an older direct model call would therefore have its sign
  flipped, and the operations balance moved by twice its amount, the first
  time anyone edits it. Before deploying lot 0b, run the **read-only**
  integrity script and read its check n° 9:

  ```bash
  python -m scripts.verify_admin_integrity
  ```

  A line « type recette_autre inscrit en déboursé » (or any kind/direction
  mismatch) names an entry to decide on by hand before the deploy; the script
  repairs nothing. No such line is the expected answer — every writer so far
  derived or passed the right direction.
- **Contacts created on the phone (lot 0b, B7):** a vCard created in DavX5
  is now stored under the id its URL names and the UID it carries (before,
  it got a server id: its own href 404'd and a duplicate synced back). The
  suite pins it through the real routes, but DavX5 fails silently, so check
  the wire once after the deploy with a throw-away contact (curl prompts for
  the DAV password):

  ```bash
  DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
  RID=$(python -c "import uuid; print(uuid.uuid4())")
  printf 'BEGIN:VCARD\r\nVERSION:4.0\r\nUID:curl-%s\r\nFN:Test Curl\r\nN:Curl;Test;;;\r\nEND:VCARD\r\n' "$RID" > /tmp/test.vcf
  curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
    -H "Content-Type: text/vcard; charset=utf-8" -H "If-None-Match: *" \
    --data-binary @/tmp/test.vcf "https://yourdomain.example/dav/addressbook/$RID.vcf"
  curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/addressbook/$RID.vcf" | grep UID
  ```

  Expected: `201`, then `UID:curl-<the same id>`. A 404 on the GET means the
  fix is not live. Delete the test contact in the app afterwards. Contacts
  created on the phone BEFORE this deploy keep their server id — their
  phone-side copy is the orphan. Then, on the device, create one contact in
  DavX5's address book, sync twice, and confirm it appears ONCE.
- **Prescription alerts include « en attente » dossiers (lot 0b, B7):** the
  dashboard and the MCP `get_agenda` briefing (the 07:00 scheduled run too)
  now list every dossier `actif` OR `en_attente` whose prescription falls
  within 60 days. Expect NEW rows the first morning if any dossier on hold
  carries a prescription date — they were silenced before, not absent. No
  index to deploy: the same `(status, prescription_date)` index serves both
  statuses.
- **DAV sync tokens (lot 0b, B7):** a failed read of a collection's CTag no
  longer rewrites it with a fresh token (which forced every client into a
  full resync, silently). During a Firestore blip a PROPFIND/REPORT now
  answers 500 and DavX5 retries; tombstones are still written. Nothing to do
  at deploy time.
- **Trust register (lot 0b, B5):** before deploying, run the **read-only**
  integrity script:

  ```bash
  python -m scripts.verify_trust_integrity
  ```

  Until lot 0b a « Virement inter-dossiers » between two clients of the SAME
  dossier credited the recipient without debiting the source: the dossier's
  stored per-client balances then disagree with the register. A line
  `dossier …/…: trust_balance_by_client stocké … ≠ recalculé …` (or
  `trust_cleared_by_client`) names that damage — already in the data, not
  caused by the deploy. The script repairs nothing, and the overdraft control
  reads the stored cleared balance, so decide each one by hand before relying
  on it. After the deploy the model refuses, on the web as it will on the
  connector: an entry, a clearing, a reversal or a transfer that falls in a
  period a completed reconciliation covers (the message names its end date);
  a future date; a cash (« comptant ») disbursement other than the art. 72
  refund of a sum of 7 500 $ or more received in cash; a fee payment by any
  method other than cheque or transfer (art. 58); a fee payment on an invoice
  that imputes a provision. The two new queries ride existing indexes
  (`trust_transactions(account_id ASC, sequence ASC)` for the art. 72 receipt
  lookup, `trust_reconciliations(account_id ASC, period_end DESC)` for the
  lock floor) — nothing to deploy.
- **Trust register, checks 5-10 (lot 5a):** the same **read-only** script now
  also re-proves every completed reconciliation at its period end (5), checks
  every clearing date — never before its entry, never in the future, never
  inside a period already closed when the clear was made (6) —, checks that
  dates never go backwards in sequence order (7), lists the entries the lot 0b
  rules would refuse today, every single-leg « virement inter-dossiers »
  and every entry whose objet contradicts its sens — « Dépôt du client »
  paid out, « Remise au client » paid in: the connector refused that pair
  from lot 5b, and since the lawyer's decision D24 (2026-09-29) the model
  refuses it — and the single leg — for every caller, the web form
  included, so these notes describe history —, plus, since the same day, a
  fee payment drawn for ANOTHER client's invoice (D21, ids only) and one
  made to a payee other than the lawyer or his firm as « Paramètres »
  names them (D23, without the name — an unreadable profile is an écart,
  the check not run)
  (8), reads every dossier's stored per-client
  balances, so a balance no entry backs and a client in shortfall both show
  (9), and checks that every fee payment (« paiement d'honoraires ») is backed
  in the administration register by standing recettes adding up to exactly
  its amount — and by none once it was reversed (10: the D-4 linkage; until
  lot 5a step 3 the web wrote that recette after the trust commit and failed
  open, and the reversal cascade reversed the recettes one commit at a time
  — the first only, before step 2 — so both directions drifted in silence;
  since step 3 both are ONE transaction, and check 10 measures the history). A fee payment with no linked recette at all is a note when it was
  written before the D-4 rule (commit e719588, 2026-08-17 11:23 HAE), or when
  an UNLINKED recette of the same amount exists — the manual entry the trust
  page's banner asks for when the automatic one fails, which can never carry
  the link; every other mismatch is an écart. A linked recette dated BEFORE
  the fee payment it carries is a note (D16 on history: the new fee-payment
  model will refuse it). Step 8 of lot 5 (`models/fee_payment`) relies on this measure: its
  reversal treats a fee payment with zero linked recettes as legacy. Run it in
  production, BEFORE the model steps of lot 5 deploy. Its exit
  code says what it found: `0` clean, `1` at least one **écart** (a figure or
  an invariant the register no longer stands on — fix or explain each before
  going further), `2` **notes** only. Its reads are not one snapshot: a write
  committed WHILE it reads (another tab, or Claude through the connector)
  would make a balance read before it disagree with the entries read after
  it — an écart no data carries. So it compares every trust and
  administration account's last-write time around each pass (every register
  write rewrites its account) and re-reads, up to three passes; when every
  pass was crossed, the FIRST écart says so and the others are not
  established — re-run it when nobody is writing. A note is history the register keeps
  (it is append-only): an entry written before a rule existed, or a
  reconciliation completed before the as-of rework (commit 945572a,
  2026-07-29 22:37 HAE), which ran under the old « tickable = en circulation
  now » code and may fail a re-proof it was never held to. Read each note with
  the lawyer; nothing is repaired automatically. One note is known and is
  not a balance error: an inter-dossier transfer written BEFORE lot 5 has a
  FIRST leg whose frozen running balance is high by exactly the amount
  (check 1, reported as a note since the lot-5 completeness review, an écart
  before) — the model stored the pair's net balance on both legs where the
  running balance after the debit leg is lower by the amount. The account,
  dossier and client balances are right, and so is every reconciliation (it
  reads the SECOND leg's figure); what is wrong is the frozen running balance
  of that one leg, which the art. 38 journal PDF prints in its « Solde »
  column (and which trips the PDF's carried-forward cross-check when that leg
  opens a period). The register is append-only, so the historical legs keep
  it; a transfer made since lot 5 stores the exact figure on both legs, and
  check 1 recognizes the old defect only by its whole signature (a
  transfer's debit leg, its recette leg the next sequence, a difference of
  exactly the amount) — any other mismatch stays an écart.
- **The fee payment is ONE transaction (lot 5a, step 3):** « Paiement
  d'honoraires » on the trust entry form now writes the trust withdrawal, its
  recette in the operations account and the invoice's payment in ONE
  Firestore transaction (`models/fee_payment`), and its « Contre-passer »
  reverses the withdrawal, EVERY linked recette and their invoices' payments
  the same way. No index, no dependency, no Tailwind class, no MCP surface.
  **Before deploying it**, the two read-only integrity scripts must be clean
  of écarts — `verify_admin_integrity` check n° 8 above all (an invoice whose
  `amount_paid` is below what a linked recette took makes its fee payment's
  reversal REFUSE: the model never clamps), and `verify_trust_integrity`
  check n° 10 (a fee payment with no linked recette reverses at trust alone —
  legacy — and says so). **Web behaviour changes to tell the lawyer:**
  1. a fee payment the administration side refuses (a closed account, a
     credit card, a date inside a reconciled period of the operations
     account, a payment the invoice refuses) is now refused WHOLE, on the
     form, and nothing is written at trust either — the old
     « la recette n'a pas pu suivre — inscrivez-la manuellement » banner, and
     the withdrawal it followed, are gone;
  2. a new optional field « Date du dépôt au compte d'administration » dates
     the recette (D16) — blank = the withdrawal's date; it may be LATER (a
     cheque of 30 August deposited on 2 September, even once August is
     reconciled on the operations account), never earlier, never future;
  3. the reversal of a fee payment is all-or-nothing too: a recette that
     cannot follow refuses the whole reversal, with its reason (the old
     « administration_contrepassation » banner is gone), and the confirmation
     page says the recette is reversed in the same operation;
  4. « Déjà compensée » on the administration form is ONE create born
     « compensée » (it could half-fail, leaving the entry « en circulation »
     under a banner);
  5. a bulk clear listing the same entry twice is refused (it cleared it
     twice and doubled the client's cleared balance — latent: no web form
     posts a bulk clear today);
  6. the entry's page now SAYS, in an amber banner, what a successful fee
     payment or reversal still needs the lawyer to read (the route used to
     drop it): an invoice addressed to ANOTHER client of the dossier than
     the one whose funds leave trust (check that this client authorised
     it), a fee payment reversed at trust alone (no linked recette — with
     what to do about a recette typed by hand — or recettes already
     reversed), and a client's cleared balance gone negative after a
     reversal (a shortfall the firm must cover). And a deposit date refused
     because the operations account is reconciled THROUGH TODAY now says to
     retry tomorrow, instead of asking for a later date the form refuses;
  7. a save whose OUTCOME IS UNKNOWN — the commit's answer lost (a timeout,
     or the commit retried after it landed) — now says so: « … a échoué
     d'une façon qui ne permet pas de savoir si elle a été inscrite :
     vérifiez le journal … avant de réessayer » (fee payment and its
     reversal, trust entry, inter-dossier transfer, administration entry,
     card payment). The fee payment used to answer « Rien n'a été inscrit.
     Veuillez réessayer » over a withdrawal that HAD landed — the retry
     withdrew the fees a second time — and a retried commit re-ran the body
     over its own landed writes, applying the amount TWICE to the balances
     and the invoice under a success (review of step 3). Rare by
     construction; each occurrence logs an `unexpected` line (see
     OBSERVABILITY.md) and is worth a look at the journal.
  **Pilot on REAL movements only** — the registers cannot be cleaned up, and
  there is no preproduction: the next real fee payment on an issued invoice,
  then check the trust entry, the operations account's encaissement (dated
  as the form said) and the invoice's « Paiements » block and status. Never
  « test » a reversal on the trust register. Re-run both integrity scripts
  afterwards.
- **Invoice void (lot 0b, B1):** voiding now reads, inside ONE transaction,
  the `trust_transactions`, `admin_transactions`, `timeentries` and
  `expenses` rows whose `invoice_id` names the invoice — single-field
  equalities the automatic index serves (no `fieldOverride` exempts
  `invoice_id`), and the invoice page reads the trust rows too. The suite's
  fake store does not model indexes, so watch the FIRST void in production:
  a red « Erreur lors de l'annulation » banner beside an `invoice void
  failed` ERROR carrying `FAILED_PRECONDITION` would mean an index is
  missing (nothing is written — the void fails closed).
- **Tasks, hearings and the step button on the phone (lot 0b, B3/B4):** five
  DAV fixes the suite pins through the real routes, on a store that is not
  DavX5. The plan requires the hearing STATUS round trip to be checked on the
  device, so do all five once after the deploy:
  1. *A task created on the phone keeps its URL.* PUT a throw-away VTODO,
     then GET the same href:

     ```bash
     D=an-active-dossier-id
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     RID=$(python -c "import uuid; print(uuid.uuid4())")
     printf 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//curl//test//FR\r\nBEGIN:VTODO\r\nUID:curl-%s\r\nDTSTAMP:20260101T000000Z\r\nCREATED:20260101T000000Z\r\nSUMMARY:Test curl\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\nEND:VCALENDAR\r\n' "$RID" > /tmp/test.ics
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
       -H "Content-Type: text/calendar; charset=utf-8" -H "If-None-Match: *" \
       --data-binary @/tmp/test.ics "https://yourdomain.example/dav/dossier-$D/$RID.ics"
     curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D/$RID.ics" | grep UID
     ```

     Expected: `201`, then `UID:curl-<the same id>`; a 404 on the GET means
     the fix is not live. Delete the test task in the app afterwards. Tasks
     created on the phone BEFORE the deploy keep their server id — their
     phone-side copy is the orphan.
  2. *The step button reaches the phone.* In the app, click « Compléter » on
     a protocol step that has a linked task; sync jtx: the task shows
     completed. Click « Rouvrir »: it comes back open.
  3. *A hearing keeps « Reportée ».* Set a test hearing to « Reportée » in the
     app, sync, move it by an hour in the Android calendar, sync again, and
     open it in the app: still « Reportée ». The served `.ics` carries
     `STATUS:TENTATIVE` and `X-PALLAS-STATUS:reportée`. If DavX5 turns out to
     drop the X-property, the status reads back as before the fix (degraded,
     never corrupted) — note it and report it.
  4. *Notes stop growing.* Edit that hearing's time three times on the
     phone, and a task's title three times in jtx: neither the hearing's
     notes nor the task's description gains a « Dossier: … » block, and the
     hearing's `Visioconférence:` line (if it has a link) is still served.
  5. *A deleted series leaves the phone.* Only if you use series: delete a
     series one of whose occurrences was moved to another dossier; that
     occurrence disappears from the phone too.
- **Protocols and their tasks (lot 1a, L2):** no index, no data migration —
  legacy protocols read `closed_by` as « not recorded » and are therefore
  never reactivated automatically; legacy steps read an empty etag. What
  changes on screen, to announce: a protocol or step form left open while
  something changed elsewhere is refused with the amber banner; the step
  button refuses on a suspended protocol or one completed by hand
  (« réactivez-le… »); a new start date no longer moves completed steps nor
  confirmed CS dates, and moves the linked tasks still on the old date; a
  note saved on a CS step no longer confirms its date (new « Confirmer cette
  date » box) — and a CS date « confirmed » before this deploy only by such
  a note (still equal to the template's suggestion) now shows « À modifier »
  again, with the box: tick it to keep the date through a start-date
  change; a task linked from a step can no longer change dossier (except
  back into its protocol's dossier). Check
  the phone once after the deploy:
  1. *A reopened task reactivates its protocol.* On a test dossier whose
     tribunal is left blank (so the regime gate accepts either C.p.c.
     template), create a « CS — Procédure ordinaire » protocol
     with « Créer les tâches automatiquement » ticked — the web « Ajouter
     une étape » form creates no task, and a « Conventionnel » protocol has
     no step to link. In the app, complete every step but ONE with its
     « Compléter » button; then complete that last step's linked task in
     jtx, sync: the step is completed and the protocol reads « Complété ».
     Reopen the task in jtx, sync: the step reads « À venir » and the
     protocol « Actif » again.
  2. *A linked task stays in its dossier.* Move that task to another
     dossier's list in jtx: the sync reports an error (422) and the task
     stays in its list — delete the test protocol and task afterwards.
  3. *A moved deadline reaches the phone.* Edit that step's deadline in the
     app (a CS step's deadline is editable; a CQ one is locked by law): the
     linked task's due date follows on the next sync (a `REPORT
     sync-collection` of `/dav/dossier-<id>/` returns a new token).
- **Notes and the théorie de la cause (lot 1a, L3):** no index, no data
  migration (the new `notes/{id}/revisions/` documents are only written, on
  the automatic index; deny-all `firestore.rules` already covers them). What
  changes on screen, to announce: « Ajouter une théorie de la cause » now
  refuses with a red banner when the dossier cannot be read (« Erreur de
  lecture ») or already holds TWO théories (it used to go quiet, or show an
  empty sheet); deleting a note that was modified in the meantime comes back
  to the note with a red banner instead of returning silently to the list.
  Deleting the théorie de la cause now leaves a write-once copy of its text in
  Firestore (`notes/<id>/revisions/`, field `content:delete`) — nothing in
  the app lists it yet; recover it from the Firestore console if ever needed.
  Check once after the deploy, with curl (DavX5 fails silently):
  1. *The théorie never changes dossier through DAV.* Take a dossier D1
     whose théorie de la cause exists (its note id `NID` is in the URL of
     « Modifier » on the Analyse tab) and a second active dossier D2. Fetch
     it, then PUT the very same body into D2's collection:

     ```bash
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D1/$NID.ics" > /tmp/theorie.ics
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" -X PUT \
       -H "Content-Type: text/calendar; charset=utf-8" \
       --data-binary @/tmp/theorie.ics "https://yourdomain.example/dav/dossier-$D2/$NID.ics"
     curl -s -o /dev/null -w '%{http_code}\n' -u "${DAV_USER:?}" \
       "https://yourdomain.example/dav/dossier-$D1/$NID.ics"
     ```

     Expected: `422`, then `200` — the théorie is still served from D1 and
     the Analyse tab of D1 still shows it. A `204` on the PUT means the fix
     is not live (and the théorie just moved to D2: move it back by
     repeating the PUT towards D1).
  2. *jtx Board still moves a théorie the way it always did.* jtx moves an
     entry by creating a copy in the target list and deleting the original:
     the copy becomes the target dossier's théorie only if that dossier has
     none (otherwise an ordinary undated note), and the original's deletion
     now leaves its snapshot. Nothing to do unless you move one.
- **Bookings rendez-vous, the hearing form and the task form (lot 1a, L4):**
  no index (the Bookings reads are `source == "bookings"` equalities, served
  by the automatic index), no data migration, no cron change. What changes on
  screen, to announce: the Réception « Rendez-vous » tab shows « Lecture des
  rendez-vous impossible » instead of « Aucun rendez-vous à confirmer » when
  Firestore cannot be read; « Confirmer » and « Refuser » from a page older
  than the rendez-vous (the Bookings sync moves a pending slot silently) are
  refused with a red banner — and a stale « Refuser » no longer touches
  Outlook; a rendez-vous the client cancelled can no longer be confirmed;
  « Lier à … » links the contact whose address matches the requester's
  exactly, whatever the page posted; the hearing edit form refuses a save
  over a change made elsewhere (amber banner); the task form refuses an
  unknown dossier instead of filing the task under « Général »; and
  refusing a pending rendez-vous while Outlook cannot be reached at all
  (Graph unconfigured, or no event id) now shows a RED banner — « la
  réunion Outlook n'a PAS été annulée … annulez-la manuellement » —
  where it used to print a green « Rendez-vous refusé. » with the client
  still booked. The
  connector gains nothing yet (`decide_rendez_vous` is lot 1b). Check once
  after the deploy, with a Bookings reservation booked under an ALIAS
  address (never a real client — a refusal emails the booker):
  1. *A refusal still cancels the meeting, once.* Wait for the reservation
     to appear in Réception (≤ 10 min), click « Refuser »: the green banner
     says the Outlook meeting was cancelled and the client notified, the
     alias receives the cancellation (the comment Graph forwards is the
     fixed « Rendez-vous refusé par le juriste. »), and the card is gone.
     Click « Refuser » again from a second tab left open on the old page:
     « Ce rendez-vous était déjà refusé. », and no second cancellation
     email.
  2. *A stale page never reaches Outlook.* Book a second reservation, open
     Réception, then MOVE that reservation in Bookings (or in Outlook) and
     wait for the next sync (the card's time changes on a reload). From the
     tab still showing the OLD time, click « Refuser »: red banner
     « Ce rendez-vous a changé… Outlook n'a pas été touché », no email.
     Reload and confirm it: it enters the calendar and the phone (a
     `REPORT sync-collection` of `/dav/general/` returns a new token).
- **Lot 1b — the agenda through the connector (ONE consent train, steps
  L5–L8):** eleven new `athena:write` tools — `update_task`, `reopen_task`,
  `update_note`, `edit_analyse` (L5); `create_protocol`, `update_protocol`,
  `add_protocol_step`, `update_protocol_step` (L6); `update_hearing`,
  `create_hearing_series`, `decide_rendez_vous` (L7) — plus new arguments on
  `create_hearing` (modality, link, reminder, status) and two new READ modes
  of `list_hearings` (`serie_id`; `bookings: "pending"`, which shows the
  requester's name and email to ANY token, a read-only one included).
  `decide_rendez_vous` is the connector's FIRST OUTBOUND effect: refusing a
  Bookings request cancels the client's Outlook meeting, which notifies him.
  The scope is frozen at issuance and a new read mode reaches the token in
  force the moment it deploys, so this is a CONSENT TRAIN, never an ordinary
  deploy — and the four steps ship in ONE push:
  1. `python -m scripts.revoke_mcp_tokens` and remove the connector in
     claude.ai BEFORE pushing.
  2. Push the lot 1b commits as one deploy. `MCP_WRITE_ENABLED` stays
     `"true"` — re-consenting while it is `"false"` yields a read-only grant
     without a word.
  3. Re-add the connector and READ the new screen before ticking
     « Autoriser les écritures »: a « Tenir l'agenda, le calendrier, les
     notes et le protocole » block (it says that reopening a task reopens
     its protocol step and a protocol the last step had closed); a
     « Décider des demandes de rendez-vous Bookings » block (it quotes the
     fixed text « Rendez-vous refusé par le juriste. » the client receives);
     the READ paragraph naming the pending Bookings requests; and, in the
     « jamais » list, « défaire une annulation en silence » and « rédiger un
     message destiné à un client ou à un tiers » where « rouvrir une tâche »
     used to be, « supprimer quoi que ce soit » now reading « … dans Athéna
     — annuler une tâche ou un événement les conserve ».
  4. `tools/list` counts **60** (27 read, 33 write); `decide_rendez_vous`
     alone carries `openWorldHint: true`, and it and `create_hearing_series`
     list `idempotency_key` as required. The descriptor byte budget (plan
     train step 4) is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` — about 175 KB of its 280 KB
     total cap at the end of lot 1b, every tool under its 8 KB cap; a red
     build there means a description to TRIM, never a cap to raise.

  No index, no data migration, no cron change, no Tailwind class. Two WEB
  changes ship with it (D17, 2026-09-27), to announce: (a) a protocol that
  is not « actif » can no longer take a new step nor a step edit on ANY
  path — the model refuses them for the web as for the connector (lot 1b
  had the connector alone ask, through a `require_active` flag now gone) —
  and its page hides those controls behind an amber banner, « Ce protocole
  n'est pas actif : réactivez-le (Modifier le protocole → Statut
  « Actif ») avant de modifier ses étapes. », keeping only the « Rouvrir »
  of a completed step when the protocol was closed by its last step (which
  reactivates it); (b) EVERY change of a note's content — the web form,
  the phone, the connector — now keeps the replaced text in
  `notes/{id}/revisions/` (lot 1b kept it for the connector's writes
  only), and the note page says « Versions précédentes : N » once there is
  one. A phone PUT that changes a note's text now commits in a transaction
  (guarded on the version just read, re-read on a lost race —
  last-write-wins is unchanged; one that loses every attempt answers 503
  with `Retry-After`, so the phone keeps its edit and re-sends it — never
  the 422 that would read as a refused body); one that changes only its title or
  category is the same bare write as before. Then, on scratch dossiers —
  DavX5 fails silently, so the `REPORT sync-collection` is the proof and
  the phone the confirmation:

  **A. Tasks, notes, the théorie (L5).**
  1. *A move reaches the phone.* `update_task` with another `dossier_id`: a
     `REPORT sync-collection` of the OLD `/dav/dossier-{id}/` (with its last
     token) lists the task as deleted, the new collection lists it; sync jtx
     on the phone — the task has left the old dossier's list and sits in the
     new one, once. The same with `update_note` on an ordinary note.
  2. *A reopen follows its step, or does nothing.* On a task whose protocol
     step the cascade closed, `reopen_task`: the step reopens and the
     protocol is « actif » again. With a second protocol made actif first,
     the call is refused and the task stays « terminée ».
  3. *Replaced prose is kept.* `update_note` with `content` (and the etag
     from `get_note`) — the note opens with « Révisée par Claude le … »; the
     old text sits in Firestore under `notes/{id}/revisions/`.
  4. *The théorie is edited by bloc, never guessed.* `edit_analyse` without
     operations creates it (`mode: created`), then one `append` on bloc F
     lands at the end of F only. Delete the « ## Bloc F » heading in the app
     and try again: refused, naming F, nothing written.

  **B. The protocol (L6)**, on a dossier with no protocol:
  1. *Creation makes no task unless asked.* `create_protocol`
     (`cq_simplifié`, a start date): every step comes back with its etag,
     `tasks_created: 0`, and a `REPORT sync-collection` of
     `/dav/dossier-{id}/` returns the SAME token as before (nothing to sync).
     A second `create_protocol` on the dossier is refused, naming the first.
  2. *The cascade is reported, re-read.* Suspend that protocol, create a
     `conventionnel` one, `add_protocol_step` twice (the second with
     `create_linked_task: true` — the task appears on the phone after a
     sync), then `update_protocol_step` `status: complété` on the first and
     then the second: `status_change.protocol_closed: true`, the protocol
     reads `closed_by: auto` in `list_protocol_steps`, the task is
     « terminée » on the phone. `status: à_venir` on the second step reopens
     the protocol (`protocol_reopened: true`); while it is actif,
     `update_protocol` `status: actif` on the suspended CQ protocol is
     refused, naming it. Complete that second step again: the conventionnel
     protocol closes by itself once more.
  3. *The law's text stays the law's.* Now reactivate the CQ protocol
     (`update_protocol`, `status: actif` — accepted, the other is closed);
     `status: à_venir` on the conventionnel's second step is then REFUSED
     (reopening it would make two protocols actif) and nothing moves.
     `update_protocol_step` with a `title` on one of the CQ template steps:
     refused, the message opens with « `title` refusé » ; with a
     `deadline_date` on it: « `deadline_date` refusé ». A `notes` edit on
     the same step lands.
  4. *A new start date keeps what is done.* Complete a CQ step, then
     `update_protocol` with another `start_date`: `recompute.preserved`
     names that step (`completed`), every other one is in `recompute.moved`
     with its new etag.
  5. *A CS suggestion is not confirmed by sending it back.* On a Cour
     supérieure dossier, `create_protocol` `cs_ordinaire`, then
     `update_protocol_step` with a step's OWN `deadline_date`: nothing is
     written and a warning says the date stays a suggestion (the connector
     has no confirmation flag; « Confirmer cette date » in the application
     does it). A DIFFERENT date is confirmed (`date_confirmed_now: true`).

  **C. The calendar and Bookings (L7).**
  1. *A reschedule keeps the Montréal hour and the duration — and Outlook
     follows.* `create_hearing` timed on a late-October day (09:00–10:30),
     then `update_hearing` with only a November `date` (after the
     daylight-saving change): `list_hearings` reads 09:00–10:30 `-05:00`, a
     `REPORT sync-collection` of `/dav/dossier-{id}/` lists the event, the
     phone shows 09:00, and the Outlook copy moves to the new day within 10
     minutes (`outlook_mirror: "follows"`). `status: annulée`:
     `outlook_mirror: "removed"`, the Outlook copy is gone within 10 minutes,
     the phone shows it cancelled.
  2. *A status set here reaches the phone exactly.* `update_hearing`
     `status: reportée` on another scratch event (`D` its dossier, `HID` its
     id, `DAV_USER` as in the lot 0b checks), then
     `curl -s -u "${DAV_USER:?}" "https://yourdomain.example/dav/dossier-$D/$HID.ics" | grep STATUS`:
     `STATUS:TENTATIVE` and `X-PALLAS-STATUS:reportée`. Move it by an hour
     in the Android calendar, sync, and `list_hearings` still reads
     `reportée` — if it reads `à_confirmer`, DavX5 dropped the X-property
     (the lot 0b device check): degraded, never corrupted; report it.
  3. *A series is one write, and an occurrence moves only detached.*
     `create_hearing_series` without a key: refused, nothing created. With
     one (`hebdomadaire`, `count: 3`): the phone receives the three events in
     ONE sync, and `list_hearings` with `serie_id` lists them. `update_hearing`
     with another `dossier_id` on one occurrence: refused, naming
     `detach_from_series`; with `detach_from_series: true` in the same call:
     `detached: true`, and a `REPORT sync-collection` of the OLD collection
     lists it as deleted. The same series call with the SAME key again:
     `idempotent_replay: true`, still three events.
  4. *A refusal reaches the client once, with the fixed text.* Book a
     « Bookings with me » slot from an ALIAS mailbox (never a real client —
     a refusal emails the booker). `list_hearings` with
     `bookings: "pending"` lists it with its etag and the alias.
     `decide_rendez_vous` `refuser` with a wrong `expected_etag`: refused,
     and NO cancellation reaches the alias. With the listed etag: the alias
     receives Outlook's cancellation carrying « Rendez-vous refusé par le
     juriste. », and the result reads `graph_cancelled: true`,
     `client_notified: true`. The same call with the SAME key:
     `idempotent_replay: true` and no second mail.
  5. *A confirmed Bookings rendez-vous changes only by its dossier and
     notes (D10).* Book a second slot, `decide_rendez_vous` `confirmer`
     with `lier_partie: false` (or true when a contact carries the alias):
     it appears in the calendar and on the phone. `update_hearing` with
     another `start_time` on it: REFUSED, naming `start_time` and pointing
     to Outlook for the reschedule — `list_hearings` still reads the old
     hour and the Outlook meeting is unchanged; with another
     `hearing_type`, REFUSED too, and the refusal sends that one to the
     application, not to Outlook. `update_hearing` with a `dossier_id` (and a
     `notes_append`): written, `moved: true`, `outlook_mirror:
     "not_mirrored"`, the event leaves « Général » for the dossier's
     collection on the phone — and the Outlook meeting is still
     unchanged. Then move the meeting by an hour IN OUTLOOK: within 10
     minutes Réception (« Rendez-vous ») shows the divergence, and
     « Appliquer » carries the new hour into Athéna.

  **D. The web follows the same rules (D17).**
  1. *A protocol that is not « actif » takes no step change, anywhere.*
     Suspend a scratch protocol (Modifier le protocole → Statut
     « Suspendu »): its page shows the amber banner, no « + Ajouter une
     étape », no « Modifier » / « Ajouter une note » on a step, no
     « Compléter ». `add_protocol_step` on it is refused too. Reactivate it:
     every control is back. On a protocol its last step closed
     (`closed_by: auto` in `list_protocol_steps`), the page still offers
     « Rouvrir » on a completed step, and clicking it makes the protocol
     « actif » again.
  2. *No edit of a note's text is lost, whoever makes it.* On a scratch
     note, change its text in the app, then in jtx Board on the phone and
     sync: the note page reads « Versions précédentes : 2 », and Firestore
     holds two documents under `notes/{id}/revisions/` — the original
     text, replaced `via: web`, and the app's version, replaced `via: dav`
     (`via` names the path that REPLACED it). Change only the
     note's category on the phone: still 2. The `PUT` answers 204 with the
     new `ETag`, and a `REPORT sync-collection` of `/dav/dossier-{id}/`
     lists the note.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 1 made false there: « 49 outils : 27 en lecture, 22 en
  écriture » (now 60: 27 + 33); the théorie de la cause « lecture seule, ne
  s'édite que dans l'application » (it is `edit_analyse`'s, by bloc, with
  the etag); « une note créée ne peut être ni éditée » and « tâches,
  audiences … créés définitivement » (`update_note`, `update_task`,
  `update_hearing`); « rouvrir une tâche … impossible ici » and « créer un
  protocole … impossible, par conception » (`reopen_task`,
  `create_protocol`); « `complete_task` — seul changement de statut »;
  « `create_hearing` … jamais modifiable ni supprimable ici » (modifiable
  now, never deletable) — and, false since lot 0a already, « `en_cours` sur
  une tâche déjà terminée rouvre l'étape liée » (refused; that is
  `reopen_task` — the one rare exception: `en_cours` on an OPEN task whose
  step was left « complété » reopens it, unreported by
  `protocol_step_effect`); « Aucun des 49 outils » and « les 49 outils »
  of the references list (60); the INSCRIPTION row and the « Remplace ce
  qu'on nomme » / « Ajoute » rows (`update_task`, `update_note`,
  `update_hearing` and its `notes_append`, `update_protocol`,
  `update_protocol_step`, `edit_analyse` and its append mode); the
  « confirmer avec l'utilisateur » list (`create_hearing_series`,
  `decide_rendez_vous`); in `references/outils.md`, family C « aucune de
  ces écritures n'est annulable », the tables without AGENDA/BOOKINGS, and
  `create_hearing` « créé `à_confirmer` » (it now takes `status`); and add
  the `expected_etag` workflow (never retry blindly after `stale_etag`),
  the two cascades (a completion can close a protocol, a reopen
  reactivates it), `list_hearings` `bookings: "pending"` and the Bookings
  warnings (a refusal notifies the client with a fixed text; a
  confirmed rendez-vous changes here only by its dossier and notes —
  rescheduling or cancelling it is refused and done in Outlook, any
  other change by the lawyer in the application); and, from D17, that a
  note's replaced text is kept whoever replaced it (the app, the phone,
  Claude — an append included), and that no step of a suspended or
  completed protocol changes anywhere until it is reactivated (the one
  exception: reopening a step of a protocol its last step closed).
- **Active gabarits (lot 2A, step T3, 2026-09-27) — run the designation
  script against production BEFORE deploying this release.** Until T3 the
  invoice note d'honoraires and the note print were filled from « the most
  recently updated template of the kind », so any edit of another template
  of that kind silently switched the letterhead printed on every client's
  note. Since T3 the code reads ONLY the template the lawyer DESIGNATED
  (`active_for`, set by « Désigner comme gabarit actif » on the template's
  page) and has **no recency fallback**: a kind with no designation refuses
  to generate (« Aucun gabarit … n'est désigné comme actif : désignez-en un
  dans Gabarits. »). The script designates, for each kind that has none
  yet, the template production is printing TODAY — the exact old rule —
  and the OLD code ignores the new field, so running it first means there
  is no outage window at all. Deploying first opens one: every note
  d'honoraires and every note print refuses until the script runs.

  Run it from a checkout of THIS release's branch — the script ships with
  the change it prepares, so `main` does not have it until the push that
  deploys, which is exactly one step too late. With Application Default
  Credentials (the script does NOT read `.env`, and `config.py` resolves its
  required variables at import — pass them inline, and never `ENV=production`,
  which would make it resolve the service's secrets; `GOOGLE_CLOUD_PROJECT`
  stops the client from inferring another project from ADC):

  ```bash
  cd athena
  export GOOGLE_CLOUD_PROJECT=$PROJECT FIREBASE_PROJECT_ID=$PROJECT \
    FIREBASE_STORAGE_BUCKET=your-bucket-name \
    AUTHORIZED_USER_EMAIL=you@example.com SECRET_KEY=unused-by-the-script
  # 1. Simulation (the default): per kind, what it WOULD designate.
  python -m scripts.designer_gabarits_actifs
  # 2. Designate. Each designation runs in the model's own transaction,
  #    against the etag just read — a template edited in between is
  #    refused, never designated blind (re-run).
  python -m scripts.designer_gabarits_actifs --apply
  # 3. Re-run the simulation: every kind must read « déjà désigné » with NO
  #    « [!] » line. Exit code 1 flags an anomaly (for instance a designation
  #    that is not the template the old code prints).
  python -m scripts.designer_gabarits_actifs
  ```

  Then deploy promptly: between the script and the deploy, the old code
  still selects by recency, so editing or uploading a note d'honoraires /
  note-print template in that window makes it print a template the new
  code will not use (step 3's « [!] » line catches that — re-run it right
  before pushing). After the deploy, open « Gabarits »: the designated template of
  each kind carries the « Actif » badge. A kind with no template at all
  (« aucun gabarit de ce type ») stays undesignated; upload one and
  designate it on its page.

  The same release keeps template FILE VERSIONS (D11): a replacement writes
  a new object `users/{uid}/templates/{id}/v{N}/{file}` and records a
  write-once `doc_templates/{id}/versions/{N}` entry — the previous object
  is never overwritten nor deleted, and the template's page lists every
  version with « Télécharger » and « Rétablir » (a restore becomes version
  N+1). Templates created before T3 have no entry until their first
  replacement records the file they had. **Manual Word check** (Change
  Impact item 3): replace a scratch template's file, « Rétablir » the
  first version, and open a document generated from the restored template
  — it must open without repair. No index to deploy (single-field queries
  only), no new secret, no new Tailwind class.

  ⚠ Rolling back past T3 is safe for the data but brings the old rules
  back: the older code selects by recency again (it ignores `active_for`),
  and its file replacement deletes the previous object — the `versions/*`
  entries stay in place, inert, and a version whose object that code
  deleted is then no longer restorable once T3 is redeployed (« Rétablir »
  refuses it: « Le fichier de cette version est introuvable »).
- **Upload tickets and the leak scan (lot 2A, step T5, 2026-09-27) — deploy
  the index file FIRST.** This release adds the `mcp_upload_tickets`
  collection (the record of the connector's future `begin_upload` /
  `finalize_upload` exchange) and its TTL policy, declared as a
  `fieldOverrides` entry in `firestore.indexes.json` beside the
  `mcp_idempotency` one — no composite index. Nothing calls the collection
  yet (the tools arrive with the lot's MCP release), so the order is about
  never having to remember it later:

  ```bash
  # From the repo root. Declares the TTL on mcp_upload_tickets.expire_at.
  firebase deploy --only firestore:indexes --project $PROJECT
  # Read-only check: the state must read ACTIVE (it takes a few minutes).
  gcloud firestore fields ttls list --collection-group=mcp_upload_tickets \
    --project=$PROJECT --format="value(ttlConfig.state)"
  ```

  The TTL is garbage collection only — a ticket's one-hour window is
  enforced in code, on every claim — so a missing policy costs storage,
  never security. Then verify the canonical bucket's `staging/` 7-day
  lifecycle rule with the read-only command of §6.6 (or
  `python -m scripts.provision --project=$PROJECT`, row
  `cycle-de-vie-staging`): the upload ticket relies on it to erase a file
  sent after its ticket closed. That row reads « inconnu » in the test
  corpus until its real output is recorded (`tests/fixtures/gcloud/
  production.json`, then remove it from `_CORPUS_EN_ATTENTE` in
  `tests/test_deployment_probe.py`). The same release ships the pure leak
  scan (`utils/docx_leak_scan.py`) and the identifier builder
  (`services/docx_identifiers.py`) the template writers will call, and the
  `run_write` persistence hooks that keep the ticket's upload URL out of
  `mcp_idempotency` — all dormant until the MCP release: no route, form or
  tool changes, no Tailwind class, no new secret.
- **The upload ticket's tools (lot 2A, step T9 — plan D4): `begin_upload` /
  `finalize_upload`.** An MCP-visible change, so it ships inside the lot's
  consent train (revoke the connector BEFORE pushing, re-add it after, check
  `tools/list` and the byte budget — CLAUDE.md, plan « Consent train »).
  Nothing to provision beyond T5 (the `mcp_upload_tickets` TTL and the
  `staging/` lifecycle rule above — verify both are in place first): no
  index, no secret, no Tailwind class. Two prerequisites only the lawyer can
  do:

  1. **claude.ai organisation settings → code execution → network egress:**
     allow `storage.googleapis.com`. Without it `begin_upload` still opens
     tickets, every PUT from the sandbox fails, and the tickets expire
     harmlessly after their hour (their bytes never arrive; nothing is
     filed). Optional hardening, if a firm-wide egress to that host is
     unwanted: a Cloudflare Worker (for instance `depot.poirierlavoie.ca`)
     forwarding ONLY `PUT`s to this bucket's resumable-session URIs — the
     tool contract would not change, only the host `upload_url` names.
  2. **Pilot on a scratch dossier** before relying on it: ask Claude to
     compute the size and MD5 of an attached PDF, `begin_upload`, PUT,
     `finalize_upload` — the document appears in the dossier, its category
     « présumée » if one was given. Then check the refusals: with a deliberately
     WRONG `md5_base64`, NO file may enter — either the service itself
     refuses the PUT (the declared MD5 travels in the session's initiation
     metadata; the repo cannot prove GCS enforces it, so this is what the
     pilot establishes) and `finalize_upload` then answers « Aucun fichier
     n'a encore été reçu », or the PUT lands and `finalize_upload` refuses
     it (« empreinte MD5 ») with the staging object gone
     (`gcloud storage ls gs://<bucket>/staging/<uid>/mcp/` — read-only).
     Both are a pass; a document in the dossier is a failure. Note which
     one happened. A ticket left more than an hour must answer « expiré ».
     For a gabarit, upload a letter FROM a dossier with `dossier_id`: its
     parties' names must be refused by name until accepted
     (`accept_residual`, a new ticket). **Manual Word check** (Change Impact
     item 3): open a document generated from an uploaded template — and from
     one uploaded with `scrub_properties` — without repair.

  The upload URL is the ONE capability a tool output carries (CLAUDE.md,
  Security Rules): it is never stored (`mcp_idempotency` keeps the result
  without it) and never logged; `mcp_upload_opened` / `mcp_upload_finalized`
  carry ids, codes and counts (OBSERVABILITY.md). A burst of
  `mcp_upload_finalized` refusals with `reason: empreinte_differente` means
  bytes other than the declared ones reached a session — look at it.
- **Lot 2A — files, templates and the upload ticket through the connector
  (ONE consent train: the lot's commits — branch `mcp-ecriture-lot2`, with
  any earlier lot of that stack not yet deployed — in ONE push).**
  Ten new tools — the FILES family (`update_document`, `move_documents`,
  `manage_folder`, `fill_gabarit`, `create_document`, `begin_upload`,
  `finalize_upload`), the TEMPLATES family (`create_template`,
  `update_template`) and one read, `list_templates` — plus the model
  hardening they rely on (the T1-T5 items above: documents, folders, active
  templates and versions, the generation service, the ticket store). The
  scope is frozen at issuance and a new READ tool reaches the token in force
  the moment it deploys, so this is a consent train, never an ordinary
  deploy. **In this order** — each step is a precondition of the next:
  1. **The index file first** (the TTL fieldOverride of
     `mcp_upload_tickets.expire_at`, garbage collection only — no composite
     index in this lot):
     `firebase deploy --only firestore:indexes --project $PROJECT`, then the
     read-only
     `gcloud firestore fields ttls list --collection-group=mcp_upload_tickets --project=$PROJECT --format="value(ttlConfig.state)"`
     until it reads `ACTIVE`.
  2. **The designation of the active gabarits, against production, from a
     checkout of THIS branch** (the script ships with the change it
     prepares — `main` only has it after the push, one step too late):
     `python -m scripts.designer_gabarits_actifs` (simulation), then
     `--apply`, then the simulation again — every kind « déjà désigné », no
     « [!] » line (the exact recipe, ADC and inline variables, never
     `ENV=production`, is the T3 item above). The old code ignores the
     field, so there is no outage window; deploying BEFORE it opens one —
     every note d'honoraires and note print refuses until it runs. Re-run
     the simulation right before step 5 (an edit of a note template in
     between moves the old code's pick).
  3. **Verify the canonical bucket's `staging/` 7-day lifecycle rule —
     read-only** (§6.6's command, or `python -m scripts.provision
     --project=$PROJECT`, row `cycle-de-vie-staging`). The ticket relies on
     it to erase a file PUT after its hour closed; a missing rule costs
     storage, never a wrongly filed document.
     **Verified on the live `athena-pallas` bucket on 2026-09-28**
     (`staging/` age 7, plus the versioning rule deleting non-current
     objects after 180 days); the output is in the probe corpus
     (`tests/fixtures/gcloud/production.json`). Re-check only if the
     bucket's lifecycle is ever edited.
  4. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing. `MCP_WRITE_ENABLED` stays `"true"`: no
     token exists to abuse, and re-consenting while it is `"false"` yields a
     read-only grant without a word.
  5. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate). Pushing its commits one by one would put new tools under the old
     consent text, and the T3 reader switch ahead of step 2's designation.
  6. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: a « Classer vos documents » block (a
     category Claude sets stays « présumée » until you confirm it; never
     « Projets » nor « Reçus du portail »), « Produire des projets Word » (a
     filled gabarit ALWAYS in « Projets »; a copy's protection level
     presumed, and its category presumed unless you had set it),
     « Téléverser un fichier » (a write-only link that transits the
     conversation, a file filed only within the hour, and the
     `storage.googleapis.com` prerequisite), a separate « Gérer vos
     gabarits » block (a replaced file kept and restorable; a NEW name given
     to an existing gabarit checked against the dossiers its files came
     from, when recorded — `name_check` says whether it was; the active note
     templates are designated by you alone — though a new file for the
     active one prints at once); and, in the « jamais » list, « modifier le
     fichier d'un document existant », « remplacer le fichier d'un gabarit
     sans en garder la version précédente », « obtenir un lien de lecture
     ou de téléchargement d'un fichier d'Athéna — le seul lien … celui d'un
     dépôt »,
     « confirmer une catégorie ou une analyse présumées » and « désigner le
     gabarit actif ». Then check `tools/list`: **70** tools (28 read, 42
     write), the ten new ones present, the write tools absent on a
     read-only grant, and `MCP_WRITE_ENABLED=false` hiding them. The byte
     budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (about 204 KB of its 280 KB cap; `update_partie` the largest at
     7.8 KB of 8) — a red build there means a description to TRIM, never a
     cap to raise.
  7. **claude.ai organisation settings → code execution → network egress:
     allow `storage.googleapis.com`** (an organisation admin's step). It is
     not per-bucket: any sandbox run could then PUT to ANY Cloud Storage
     bucket. If that is unwanted, the optional hardening is a Cloudflare
     Worker (for instance `depot.poirierlavoie.ca`) forwarding ONLY `PUT`s
     to this bucket's resumable-session URIs — the tool contract does not
     change, only the host `upload_url` names (and the allowlist then names
     only that host). Without either, every PUT fails and the tickets
     expire unused.
  8. **Pilot the upload ticket on a SCRATCH dossier** — the T9 item's
     recipe: a PDF whose size and MD5 the sandbox computes, `begin_upload`,
     PUT, `finalize_upload` (the document appears, « présumée » if a
     category was given); a deliberately WRONG `md5_base64` never files
     anything (either GCS refuses the PUT and `finalize_upload` answers
     « Aucun fichier n'a encore été reçu », or the PUT lands and is refused
     « empreinte MD5 » with the staging object gone — note which); a ticket
     left more than an hour answers « expiré »; a gabarit uploaded from a
     dossier with `dossier_id` is refused on its parties' names until
     accepted.
  9. **Word checks** (Change Impact item 3 — the fill engine's outputs must
     open WITHOUT repair; no test can see Word's repair prompt): open a
     `fill_gabarit` result whose blocs sit in a NUMBERED paragraph (Word's
     numbers, once each, renumbering when a paragraph is inserted), one with
     a `markdown: true` bloc (no double numbering), a `create_document`
     Markdown result (headings, a table), a `create_document` copy, a
     template installed by `update_template` and one « Rétabli » on its
     page, and a document generated from each of those two.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 2A makes false there (on top of the lot 1 list above):
  « 49 outils : 27 en lecture, 22 en écriture » / « Aucun des 49 outils » /
  « Les 49 outils » (now 70: 28 + 42); « la catégorie d'un document et son
  niveau de protection sont dérivés, jamais choisis » (règle 2) and, in
  `references/vocabulaires.md`, « Catégorie de document … en écriture elle
  est dérivée » — a category `update_document`, `create_document` or
  `begin_upload` sets is PRESUMED and only for a document WITHOUT an
  analysis, an analysis replacing it; « Il n'existe aucun paramètre de
  catégorie, à dessein » (le cliquet de protection) — true of
  `record_document_analysis` only; the INSCRIPTION row « pour un document :
  `record_document_analysis` » (a document's filing is `update_document`
  and `move_documents`, a new one `fill_gabarit`, `create_document` or the
  upload ticket, a gabarit `create_template` / `update_template` or the
  ticket); in `references/outils.md`, the family table without FILES and
  TEMPLATES, family B without `list_templates`, family C « aucune de ces
  écritures n'est annulable » and family D without `update_document` /
  `move_documents` / `manage_folder` / `update_template`; and — false since
  2026-08-31, which this lot's text sweep caught — « `query` ne touche que
  … nom affiché, nom de fichier, description, étiquettes » (SKILL.md
  INVENTAIRE row and `references/outils.md` twice): the fields are the
  analysis summary, `notes_internes` and `genere_depuis`, never a
  « description ». Incomplete rather than false, to extend the same day:
  SKILL.md « Les trois sémantiques d'écriture » (« Remplace ce qu'on
  nomme » lacks `update_document`, `update_template` and `manage_folder`'s
  rename and move, and no row says that `fill_gabarit`, `create_document`
  and `finalize_upload` always make a NEW document); `references/outils.md`
  — its pagination table (`list_templates` pages by offset) and its
  provenance bullet (a generated or copied document says « par Claude
  (connecteur) » in `genere_depuis`); `references/vocabulaires.md` (the
  template kinds and categories, `manage_folder`'s actions, `begin_upload`'s
  purposes and template modes). Add: the upload ticket (compute size and MD5 in the
  sandbox, one PUT, never show the link, egress to storage.googleapis.com,
  `finalize_upload` answering a filed ticket again rather than a new
  ticket); `fill_gabarit`'s discipline (read `list_templates` with
  `template_id` and the `dossier_id` first, write only the blocs and manual
  fields, plain paragraphs separated by a blank line with no numbering of
  your own, `markdown: true` only for internal structure, never « {{ » or
  « }} », always into « Projets », and a refusal saying a party of the
  dossier « n'a pas pu être lue » means retry — never write around it);
  `create_document`'s two sources and its
  refusal when no note-print template is designated; the presumed category
  and its « Confirmer » in the application (a copy's category presumed
  only when the lawyer had not set it; its protection level presumed, to
  verify by qualifying the copy); the leak scan of a template taken from a
  dossier (`accept_residual` on the lawyer's word only) — and, since the
  fixups of lot 2A, that a RENAME is checked against the template's source
  dossiers when they are recorded (`name_check` says whether it was; a
  party's name still never goes into a template's name), that an uploaded
  gabarit must name its source `dossier_id` or declare
  `aucun_dossier_source: true` — only for a file from no dossier, never as a
  way round the check; and that the active note templates are the lawyer's
  to designate AND to undesignate. D18: a category the lawyer chose or
  confirmed (`category_set_by_lawyer: true` on `list_documents`) is never
  replaced — report the disagreement to him instead; a document filed
  before that marker counts as his unless its category is « autre » (fixups
  of lot 3).
- **Lot 2B — turning a finished letter into a gabarit through the connector
  (ONE consent train: branch `mcp-ecriture-lot2b` — with lot 2A and any
  earlier lot of that stack not yet deployed — in ONE push, then the PILOT
  on ONE real letter).** One new READ tool, `preview_templatize`, and one new
  argument, `create_template`'s `substitutions` — no new family, no index, no
  dependency, no Tailwind class, no `cron.yaml` change, nothing DAV-exposed.
  A new read reaches the token in force the moment it deploys, and the
  consent text changes (« transformé en gabarit »), so this is a consent
  train, never an ordinary deploy. **In this order:**
  1. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing. `MCP_WRITE_ENABLED` stays `"true"` (no
     token exists to abuse; re-consenting while it is `"false"` yields a
     read-only grant without a word).
  2. **Push the lot as ONE deploy** (Cloud Build runs the suite as the gate).
  3. **Re-add the connector and READ the « Gérer vos gabarits » block before
     ticking** « Autoriser les écritures »: a document « tel quel, ou
     transformé en gabarit » (a COPY, its document properties always emptied,
     all or nothing, each replacement counted in advance), checked « tel qu'il sera enregistré, les textes que Claude
     a demandé de remplacer compris », and « relisez un gabarit transformé
     avant de vous en servir » — the check reads names, numbers and
     addresses, never the rest of the text. Then `tools/list`: **71** tools
     (29 read, 42 write), `preview_templatize` present on a read-only grant
     too (it writes nothing), `create_template` listing `substitutions`. The
     byte budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (about 209 KB of its 280 KB cap).
  4. **The pilot — ONE real letter, in a real dossier** (Change Impact item
     3: no test can see Word's repair prompt, and only a real letter has a
     real letterhead). Pick a finished `.docx` the practice wrote — no
     tracked changes, no comments (accept, reject and delete them in Word
     first, then file it again) — with the client's name in the body AND in
     a header or footer if possible.
     a. **Preview.** Ask Claude to templatize it: it calls
        `preview_templatize` with the pairs it proposes (the client's name,
        the file number, the court number, the addresses…). Read the rows:
        each `substituted` count must match what the letter really holds,
        `in_non_target_parts` names a footnote (the title, subject, author,
        last editor and description are ALWAYS emptied on this path, and
        `scrubbed_properties` says which were), and `leak_scan.residues`
        lists what would stay — an ALL-CAPS heading the pairs missed shows
        up here as the literal itself (`origin: substitution`).
     b. **Adjust**, preview again, until `ready_to_create: true`.
     c. **Create** (`create_template`, the same pairs, each
        `expected_occurrences` = its `substituted`). Try one deliberately
        WRONG count first: it must be refused « rien n'a été écrit » and
        « Gabarits » must show nothing new.
     d. **Open the template in Word** (Gabarits → the new template →
        « Télécharger le gabarit »): it opens WITHOUT a repair prompt, the
        letterhead, fonts and headers are intact, and each replaced text now
        reads `{{…}}` — in the body, the header and the footer.
     e. **Fill it for ANOTHER dossier** (« Générer depuis un gabarit » on
        that dossier, or `fill_gabarit`), open the result in Word WITHOUT repair, and read
        it whole: the other client's name, file number and address are where
        the first ones were — and **nothing of the source client remains**:
        not in the body, not in a header or footer, not in a footnote, not
        in Fichier › Informations (the document properties).
     f. The source letter is unchanged in its dossier (same size, same
        date). If anything is wrong, delete the template in the application
        (the connector cannot delete) and note what the preview missed.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 2B makes false there: every « 70 outils » (now 71: 29 + 42),
  and any sentence saying a document becomes a gabarit only « tel quel » or
  that turning a letter into a gabarit must be done in Word. Add: the
  workflow — `preview_templatize` FIRST, adjust, then `create_template` with
  each `expected_occurrences` = `substituted`; matching is case-SENSITIVE
  (an ALL-CAPS variant is its own pair, with an ALL-CAPS field name); the
  field names are those `list_templates` shows for an existing gabarit (a
  misspelt one classifies « passthrough »); what stays in footnotes and
  field results is never replaced, and the title, subject, author and
  description are always emptied; a residue is accepted
  only on the lawyer's word; an OUTSIDE letter is filed first as a document
  of its dossier (`begin_upload` purpose document) and templatized there —
  the upload ticket never templatizes; the preview's rows count from 0 while
  its French messages number from 1; and the lawyer reads the new gabarit
  before it serves — the check never reads the rest of the letter's text.
- **Lot 3 — billing through the connector (3a: an ordinary release; 3b:
  ONE consent train — branch `mcp-ecriture-lot3`, with lot 2B and any
  earlier lot of that stack not yet deployed, in ONE push).** 3a is the
  model, service and web half and changes no MCP surface: one plan for
  issuing an invoice (`plan_invoice`) with its issuance rules, the
  brouillon correction (`/factures/<id>/brouillon`), the move of an
  un-invoiced time entry or disbursement to another dossier, the note
  d'honoraires as ONE generation (`services/note_honoraires.py`), and the
  budget's transactional version counter. 3b adds five tools — two reads,
  `preview_invoice` and `get_budget`, and the BILL family, `create_invoice`,
  `update_invoice`, `create_budget_version` —, `dossier_id` (the move) on
  `update_time_entry` / `update_expense`, and `source: invoice_note` on
  `create_document`. No index to deploy (single-field equalities and keyed
  reads only), no dependency, no Tailwind class, no `cron.yaml` change, no
  `firestore.rules` change (`counters/budget-{dossier_id}` falls under the
  deny-all), nothing DAV-exposed. The scope is frozen at issuance and the
  two new READ tools reach the token in force the moment they deploy, so 3b
  is a consent train, never an ordinary deploy. **In this order:**
  1. **The firm's GST and QST registration numbers — BEFORE deploying 3a.**
     Open « Paramètres → Profil du cabinet » and check both are filled.
     From 3a on the MODEL refuses a NEW invoice that charges GST or QST
     under an EMPTY number — on the web « Nouvelle facture » form as through
     the connector (« Le numéro d'inscription TPS du cabinet est vide… »).
     Invoices already issued are untouched: their numbers were snapshotted
     at issuance and nothing rewrites them.
  2. **`python -m scripts.verify_admin_integrity` is clean** (read-only —
     the environment of the T3 designation recipe above: ADC, inline
     variables, never `ENV=production`). Check n° 8 above all: every
     invoice's recorded `amount_paid` is backed by the register. The void
     (web « Annuler » and, from 3b, the connector's `update_invoice` with
     status « annulée ») refuses an invoice on which a payment stands, read
     from `amount_paid` AND the standing admin and trust entries; a drift
     the report names is an invoice whose void would be judged on figures
     the lawyer does not see — decide it by hand first.
  3. **3a may go alone as an ordinary release** once every earlier lot of
     the stack is deployed; otherwise its commits ride in step 5's push —
     they change no MCP surface, so the order below is unchanged. Web
     checks after it, none of which burns a number: on any brouillon,
     « Modifier le brouillon » then « Enregistrer » WITHOUT changing a field
     returns to the sheet and writes nothing (the no-op); « Note
     d'honoraires (Word) » on an existing, non-annulée invoice files its note
     in « Projets », and it must open in Word WITHOUT repair (Change Impact
     item 3 — the fill now runs in `services/note_honoraires.py`). Never
     test « Créer » on « Nouvelle facture »: a success takes a real number.
  4. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing 3b. `MCP_WRITE_ENABLED` stays `"true"`: no
     token exists to abuse, and re-consenting while it is `"false"` yields a
     read-only grant without a word.
  5. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate). Pushing 3b's commits one by one would put the new tools under
     the old consent text.
  6. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: the read paragraph now names the budgets;
     a « Facturer » paragraph (a NEW invoice, always a brouillon, only
     billable un-invoiced entries of one dossier, after a preview whose
     total must match exactly — and **it consumes the year's next number
     for ever, even if voided**; its entries are frozen, their phase aside,
     until voided), « Tenir une facture » (**only a brouillon is
     corrected**; marking « envoyée » **sends nothing** and is undone only
     by a void; a void needs a reason and is refused while a payment is
     recorded), « Budgets » (a NEW version, refused when a newer one was
     saved; every version kept, the proof of what the client was told);
     the CORRECT paragraph (an entry may be moved to another dossier), the
     IMPORT paragraph (« annulez la facture — ici ou dans l'application »)
     and the FILES paragraph (the Word note d'honoraires). In the « jamais »
     list: « envoyer une facture à qui que ce soit », « marquer une facture
     payée », « écarter en silence une entrée … nommée », and still
     « supprimer » and « inscrire ou encaisser un paiement » — while
     « changer le statut d'une facture ou l'annuler » and « émettre un
     nouveau numéro de facture » are GONE. Then check `tools/list`: **76**
     tools (31 read, 45 write), `preview_invoice` and `get_budget` present
     on a read-only grant too, and `MCP_WRITE_ENABLED=false` hiding the 45
     writes — the three BILL tools among them — while keeping those two
     reads. The byte budget is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` (about 221 KB of its 280 KB cap;
     `update_partie` still the largest, 7.8 KB of 8).
  7. **The smoke test burns NO number** (Change Impact item 1 — and the
     lawyer's books: every successful `create_invoice` takes the year's
     next `YYYY-F###` for ever, and a void leaves a visible hole in the
     « Journal des honoraires »). First read the year's counter —
     read-only, ADC, the year being TODAY's in Montréal; « absent » means
     no invoice was numbered this year yet:

     ```bash
     cd athena
     export GOOGLE_CLOUD_PROJECT=$PROJECT
     python -c "import sys; from google.cloud import firestore; d = firestore.Client(project='$PROJECT').document('counters/invoices-' + sys.argv[1]).get(); print(d.to_dict() if d.exists else 'absent')" 2026
     ```

     Then, through Claude:
     a. `preview_invoice` on a dossier with unbilled work: `ready: true`
        and a total, or `refusals` saying exactly why (a blank tax number
        here means step 1 was skipped). Nothing is written.
     b. **Never call `create_invoice` here — not even with a total
        deliberately wrong.** That refusal is judged in `plan_invoice`,
        BEFORE the counter is read at all, so it proves nothing about the
        allocation's place inside the transaction; and the same call with
        the preview's TRUE total — one slip, one « helpful » correction —
        issues a real invoice under a permanent number. That the allocation
        commits with the invoice or not at all is the deploy gate's to
        prove (`tests/test_mcp_billing_writes.py`
        `test_a_source_changed_during_the_call_burns_no_number`,
        `test_a_create_that_would_burn_a_number_never_does`, and
        `tests/test_invoice_numbering.py`).
     c. `get_invoice` on an existing `envoyée` invoice: an `etag` and its
        `connector_transitions`; then `update_invoice` with `status:
        "envoyée"` — its CURRENT status — and that etag: `outcome:
        "unchanged"`, nothing written.
     d. `get_budget` on a dossier that has a budget, then
        `create_budget_version` with the `base_version` returned, `mode:
        "merge"` and one line equal to a stored one: `outcome: "unchanged"`,
        nothing written (a new version is permanent — the budget is
        append-only).
     e. `create_document` with `source: "invoice_note"` on an existing,
        non-annulée invoice: ONE note filed in « Projets » — its object in
        the bucket under `users/<the practice's uid>/…`, never
        `users/unknown/` (the Cloud Storage console shows it; no tool output
        carries a path) — which opens in Word WITHOUT repair; the same call
        again returns it (`reused: true`, `created: false`) instead of
        filing a second. (This
        one does write a document — delete it in the application if it is
        not wanted.)
     f. Re-read the counter: it must be exactly what it was.

     The promotion `brouillon → envoyée` (`update_invoice`) is undone only
     by a void: run it ONLY on an invoice the lawyer has chosen to promote
     anyway — an imported brouillon of the IMP-07 backlog, typically — and
     name it before the call. And when the lawyer really issues an invoice
     through `create_invoice`, re-read the counter after it: `seq` must have
     advanced by **exactly one** (a refusal never moves it; a success always
     does, once).

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 3 makes false there (on top of the lot 1, 2A and 2B lists
  above): every tool count — « 49 outils » in the copies as they stand
  today, « 71 » once the lot 2B list is applied — now 76 (31 + 45); SKILL.md « Aucun des … outils
  n'expose de `dry_run`, de `preview` ni d'équivalent » — still no `dry_run`,
  but `preview_invoice` (like `preview_templatize`) is a READ that computes
  exactly what its write would do; « Les écritures qu'aucun outil ne fait :
  … changer le statut d'une facture, enregistrer un paiement ou annuler une
  facture … Tout cela passe par l'application » — the status and the void
  are `update_invoice` now, the payment alone stays in the application;
  `references/comptabilite.md` §1 « le seul retour est l'annulation de la
  facture dans l'application » and §7 rule 5 « La facture atterrit en
  brouillon et y reste. Le connecteur ne change aucun statut de facture »;
  `references/outils.md` — `complete_task` « seul changement de statut »
  (false since lot 1 already), family C « aucune de ces écritures n'est
  annulable par le connecteur » (an invoice it issues, it voids), family G
  listing reads only, `import_invoice` « atterrit en brouillon et y
  reste », and the row « Changer le statut d'une facture, enregistrer un
  paiement, annuler une facture — Impossible ici »; README « ne change
  aucun statut de facture »; and the four `set_*_phase` tools « sont les
  seuls à atteindre une ligne déjà portée à une facture » — SKILL.md « Le
  mur de la facturation », `references/comptabilite.md` §1 (its heading
  « la seule porte qui le traverse » too) and `references/outils.md`
  family E: the void (`update_invoice`, status « annulée », refused while
  a payment stands) reaches every such line, to RELEASE it — what stays
  true is that they alone CHANGE a line while it stays billed (the
  connector's own INSTRUCTIONS said the same until the review of this
  step). Incomplete rather than false, to extend the same day: « Remplace
  ce qu'on nomme » lacks `update_invoice` (a brouillon only) and the
  moves; `references/vocabulaires.md`'s
  invoice statuses should say which ones `update_invoice` sets (envoyée,
  en_retard, annulée — never payée, never back to brouillon) and that
  `create_budget_version` refuses ADM and HOR. Add: the two-step
  `preview_invoice` → `create_invoice` (the SAME selection, its total as
  `expected_total_cents`, an `idempotency_key` always, the lawyer's
  confirmation first — the number is consumed for ever); `update_invoice`'s
  discipline (one change a call, the etag from `get_invoice`, `en_retard`
  only past the due date, a `void_reason` on every void, a payment reversed
  first — in « Fidéicommis » or « Administration », as the refusal says);
  the note d'honoraires by `create_document` source `invoice_note`
  (`regenerate` only on the lawyer's word); `get_budget` →
  `create_budget_version` (`base_version`, `merge` keeping what a line does
  not name, `replace` making the lines the whole budget); and the move of
  an un-invoiced entry by `update_time_entry` / `update_expense` with
  `dossier_id` (its amount and phase kept; its share of the budget actuals
  follows it — a non-billable time entry counts in no budget). Since the
  fixups of lot 3, also: a `create_invoice` answer « Issue INCERTAINE »
  (reason `invoice_outcome_uncertain`) means the invoice MAY exist — re-read
  `list_invoices`, and retry only with the SAME key, never a new one before
  that re-read; the counter never reissues a number, while an IMPORTED
  invoice's number can be imported again once the voided invoice is deleted
  in the application; and IMP-07 names imported brouillons only.
- **Lot 4 — dossiers and contacts through the connector (4a: an ordinary
  release; 4b: ONE consent train — branch `mcp-ecriture-lot4`, with lot 3
  and any earlier lot of that stack not yet deployed, in ONE push).** 4a is
  the model, service and web half: the DavX5 drain of a dossier leaves the
  route for `services/dossier_dav.py` (its members read STRICTLY before the
  status is written — unreadable, the change is refused and nothing is
  saved —, re-runnable, with the amber banner and « Resynchroniser le
  téléphone »), `opened_date` / `closed_date` stamped as the Montréal day,
  the party-link rules in the model (a client who has EVER had trust funds
  on the dossier, a party a signification names and the last client never
  leave; a contact is never client AND adverse — grow-only on a legacy
  duplicate; every detached link journaled in `audit_events`), one
  representation at a time for mandataires (their notes sanitized and
  capped), and the D7 provenance of a compliance check (a presumed
  inscription AMBER on the fiche, « Confirmer » the only way to the
  lawyer's attestation, the coverage report keeping it open). Its
  MCP-visible part is read-only text: `list_deletions` accepts two new
  `entity_type` values and says what their rows name, and
  `get_coverage_report` counts a presumed check as open (none exists before
  4b). 4b adds four writes in two new families — DOSSIERS
  (`set_dossier_status`, `update_dossier_party`) and CONTACTS
  (`update_partie_mandataire`, `record_kyc_status`) —, `get_partie`'s
  provenance fields, `_no_replay` in the write protocol, and the texts. No
  index to deploy (the strict readers reuse the same single-field
  equalities, the trust check the existing `(dossier_id, client_id,
  sequence)` index), no dependency, no Tailwind class, no icon, no
  `cron.yaml` or `firestore.rules` change, and no DavX5 account re-add (no
  collection path, name or component set changes). **In this order:**
  1. **Measure, read-only, BEFORE deploying 4a** — the environment of the
     T3 designation recipe above (ADC, inline variables, never
     `ENV=production`). Two answers decide what to repair by hand:

     ```bash
     cd athena
     export GOOGLE_CLOUD_PROJECT=$PROJECT
     python - <<'PY'
     from google.cloud import firestore
     db = firestore.Client()
     # (a) A contact on BOTH sides of a dossier. From 4a the model lets such
     # a legacy duplicate stand (the rule is grow-only) — removing one side
     # in the dossier's form is its repair.
     for s in db.collection("dossiers").stream():
         d = s.to_dict() or {}
         both = set(d.get("client_ids") or []) & set(d.get("opposing_party_ids") or [])
         if both:
             print("des deux côtés :", d.get("file_number"), len(both))
     # (b) A representation the forward rule already refuses (an unknown
     # contact, itself, not an individual, another role). Such a contact
     # refuses EVERY save today, the web form's included — repair its
     # mandataires in the form before relying on it.
     parties = {s.id: (s.to_dict() or {}) for s in db.collection("parties").stream()}
     for pid, p in parties.items():
         for e in p.get("mandataires") or []:
             mid = e.get("id") if isinstance(e, dict) else None
             m = parties.get(mid)
             why = ("introuvable" if m is None else "soi-même" if mid == pid
                    else "pas une personne physique" if m.get("type") != "individual"
                    else "autre rôle" if m.get("contact_role") != p.get("contact_role")
                    else "")
             if why:
                 print("mandataire :", pid, "→", mid, why)
     PY
     ```

     No output is the expected answer. The identifiers it prints are for
     the lawyer's console only — paste them nowhere else. (A legacy
     single-mandataire contact — `mandataire_id`, migrated on read — is
     not scanned; it is repaired by its next save.)
  2. **4a may go alone as an ordinary release** once every earlier lot of
     the stack is deployed; otherwise its commits ride in step 4's push.
     Web checks after it: open a contact's fiche — its Conformité reads as
     before for the lawyer's own checks (« le … par Me … »), and the
     section now also shows for a CLIENT of a dossier whatever the contact's
     role. On a TEST dossier with one task, one note and one confirmed
     event — its ONLY client a TEST contact of role « client », whose
     identity step 7 inscribes —, close it in the form: no amber banner (the drain completed);
     then reopen it. Worth telling the lawyer, though it dates from lot 0a
     (the form's version check): a trust entry recorded while a dossier's
     edit form is open in another tab refuses that tab's save with the
     conflict banner — a trust write changes the dossier record: reload,
     then redo.
  3. **`python -m scripts.revoke_mcp_tokens`, and remove the connector in
     claude.ai** — BEFORE pushing 4b. `MCP_WRITE_ENABLED` stays `"true"`.
  4. **Push the lot as ONE deploy** (Cloud Build runs the suite as the
     gate).
  5. **Re-add the connector and READ the new screen before ticking**
     « Autoriser les écritures »: a « Dossiers » paragraph (a status changed
     as the application does — closing or archiving takes the dossier's
     tasks, notes and events off your phone and out of the prescription
     alerts, reopening puts them back and erases the closing date; a drain
     that could not finish is REPORTED and repaired by asking the same
     status again or by « Resynchroniser le téléphone »; a party's roles
     or lawyer corrected, a party DETACHED — a link, never the contact,
     refused for the last client, a served party and a client with trust
     history —, names refreshed) and a « Contacts » paragraph (mandataires
     added, corrected or detached; an identity or conflict check INSCRIBED
     as presumed, « inscrit par Claude le … — à confirmer », counting as NOT
     done until you confirm it, never written over a check you decided). In
     the « jamais » list: « confirmer une vérification d'identité ou de
     conflits d'intérêts, ni modifier une vérification que vous avez décidée
     ou confirmée », and « supprimer » now says that detaching a party or a
     mandataire removes a link — while « changer le statut d'un dossier » and
     « … à la vérification d'identité ou à la vérification des conflits
     d'intérêts » (beside the trust promise — « toucher au fidéicommis »,
     which reads « écrire au fidéicommis ou au registre d'administration »
     when lot 5 rides in the same push) are GONE. Then check
     `tools/list`: **80** tools (31 read, 49 write), and
     `MCP_WRITE_ENABLED=false` hiding the 49 writes — the four new ones
     among them. The byte budget is the deploy gate's
     `tests/test_mcp_descriptor_budget.py` (about 232 KB of its 280 KB cap;
     `update_partie` still the largest, 7.9 KB of 8).
  6. **Check the phone on the wire, THEN on the device** (Change Impact
     item 2 — DavX5 fails silently). On the TEST dossier of step 2 (one
     task, one note, one confirmed event), with curl prompting for the DAV
     password:

     ```bash
     D=the-test-dossier-id
     DAV_USER=you@yourdomain.example   # the AUTHORIZED_USER_EMAIL of app.yaml
     BODY='<?xml version="1.0" encoding="utf-8"?><d:sync-collection xmlns:d="DAV:"><d:sync-token/><d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>'
     report() {
       curl -s -u "${DAV_USER:?}" -X REPORT \
         -H "Content-Type: application/xml; charset=utf-8" --data "$BODY" \
         "https://yourdomain.example/dav/dossier-$D/" > /tmp/report.xml
       echo "live: $(grep -o '<D:getetag>' /tmp/report.xml | wc -l)  removed: $(grep -o '404 Not Found' /tmp/report.xml | wc -l)"
     }
     report   # before: live 3
     ```

     First the DISCOVERY itself (fixes of lot 4 — the root Depth:1 PROPFIND
     reads its dossiers STRICTLY and answers 503 + `Retry-After` on a read
     failure instead of a 207 advertising zero dossier collections; a single
     dossier document it cannot read is skipped and logged, the others
     listed):

     ```bash
     root() {
       curl -s -u "${DAV_USER:?}" -X PROPFIND -H "Depth: 1" \
         -o /tmp/root.xml -w '%{http_code}\n' "https://yourdomain.example/dav/"
       echo "dossier collections: $(grep -o '/dav/dossier-' /tmp/root.xml | wc -l)"
     }
     root   # 207, and as many collections as actif + en_attente dossiers
     ```

     The count must equal the number of `actif` and `en_attente` dossiers in
     the application's list. A 503 (with `Retry-After: 30`) means a read
     failed — the `unexpected` line « dav root propfind read failed » says
     which (`check`); retry, and never read it as « no dossier ». A count
     BELOW the application's is a skipped document: the logs then carry
     `list_dossiers_by_status_strict: document skipped` or `dav root
     propfind: dossier skipped`, each naming the `dossier_id` that left
     discovery. Repair that stored document by hand (a legacy client entry
     without an `id`, a stored `id` that is not the document's, a file
     number or title that is not text); its own collection stays out of
     DavX5 until then.

     Then the probe DavX5 itself makes. When discovery fails — its 503
     included — DavX5 does NOT keep its list on the strength of that answer:
     it re-probes every collection at its own URL (Depth:0) and deletes from
     the phone each one that answers 403, 404 or 410; any other error aborts
     the refresh, retried later (davx5-ose, read 2026-09-29). So the
     collection's own answer is the load-bearing one (review of the fixes of
     lot 4 — it read its dossier fail-open and answered 404 on an outage):

     ```bash
     curl -s -u "${DAV_USER:?}" -X PROPFIND -H "Depth: 0" -o /dev/null \
       -w '%{http_code}\n' "https://yourdomain.example/dav/dossier-$D/"   # 207
     ```

     207 in service. During a Firestore incident it must read **503** (with
     `Retry-After: 30` and an `unexpected` « dav dossier scope read failed »
     naming the id) — never 404, which is a deletion on every phone; a 404
     outside an incident is a dossier that does not exist or a document
     logged « get_dossier_for_dav: document unusable ».

     Then, through Claude: `set_dossier_status` « fermé » on it — the
     result reads `dav.complete: true`, `dav.direction: "drain"`,
     `dav.resources: 3`, with warnings naming what closing does —; `report`
     again: live 0, removed 3; a PROPFIND Depth:1 on `/dav/` no longer lists
     `dossier-$D`. The same call again (same status): `outcome:
     "unchanged"`, nothing written, the phone's view re-applied.
     `set_dossier_status` « actif »: `dav.direction: "restore"`; `report`:
     live 3, removed 0. On the device: refresh DavX5's collection list —
     the dossier's items left after the close and came back after the
     reopening (re-tick the collection if DavX5 dropped it).
  7. **The compliance check, on a TEST client** (never a real one — the
     inscription is a real record on a regulatory field): the test
     dossier's ONLY client, the test contact of step 2 — another client
     with an unverified identity would keep `IDENTITE_NON_VERIFIEE` listed
     after the confirmation, and the check below would read as a failure.
     `record_kyc_status` `check: "identity"`, `status: "vérifié"`: its fiche
     shows the AMBER « Vérifié (présumé) » and « inscrit par Claude le … — à
     confirmer », with NO « par Me … »; `get_coverage_report` (the test
     dossier is actif) still lists `IDENTITE_NON_VERIFIEE`, « Dont 1
     inscrite(s) par Claude ». Click « Confirmer » on the fiche: it reads
     « confirmé le … par Me … (inscrit par Claude le …) », and the finding
     is gone. A second `record_kyc_status` on that check is REFUSED
     (`kyc_lawyer_attestation`).
  8. **The links, on the same test data:** a second test contact added as a
     client (`update_dossier` `add_clients`), its roles set
     (`update_dossier_party` `update`), then detached (`remove`):
     `list_deletions` shows a `dossier_party` row. `update_partie_mandataire`
     `add` then `remove` of a third test contact (an individual of the same
     role) on the first one: a `mandataire` row, whose `title` names the
     contact it REPRESENTED. Then delete the test data in the application —
     the dossier's task, note and event first (a dossier with children
     cannot be deleted), then the dossier, then the contacts (a contact
     still on a dossier cannot be deleted); the journal rows stay, by
     design.

  Then update BOTH copies of the claude.ai skill `pallas-athena` the same
  day. What lot 4 makes false there (on top of the lot 1, 2A, 2B and 3
  lists above) — and one NAME to write as shipped: `update_dossier_party`
  names a party's lawyer `avocat_partie_id`, the key of the party entries
  of `create_dossier` / `update_dossier` — never `avocat_id`, the stored
  field's name, which its schema refuses (the RESULT keeps that name:
  `avocat_id_before` / `avocat_id_after`): every tool count (now 80: 31 + 49 — the synced copies still
  say 49: 27 + 22); SKILL.md « Le `status` choisi à la création d'un dossier
  ne peut plus jamais être changé ici » (`set_dossier_status`); « Les
  écritures qu'aucun outil ne fait : Fermer un dossier; … vérifier une
  identité; vérifier les conflits; gérer les mandataires; … La vérification
  d'identité et la vérification de conflits ne sont inscriptibles par aucun
  outil et ne le seront jamais » — closing a dossier is
  `set_dossier_status`, the mandataires `update_partie_mandataire`, and
  `record_kyc_status` INSCRIBES a check as presumed; what stays true is that
  only the lawyer CONFIRMS one (« une machine ne doit pas attester »
  survives in that form); README « Il ne ferme pas un dossier » and
  « n'atteste aucune vérification d'identité ou de conflits » (true only as
  « it inscribes a presumed one, which only the lawyer confirms »);
  `references/outils.md` — the `create_dossier` row « `status` jamais
  modifiable ensuite », the `create_partie` row « vérif. identité/conflits
  **jamais** inscriptibles », the `update_dossier` warning « `status` est
  délibérément absent — fermer un dossier doit vider sa collection DavX5,
  ce que seule l'application fait », « Vérification d'identité, vérification
  de conflits et mandataires ne sont inscriptibles par aucun outil, et ne
  le seront jamais », the `get_coverage_report` warning « le connecteur ne
  peut créer un protocole, vérifier une identité ni déposer une
  signification » (all three false now), the `list_deletions` row « 15
  `entity_type` » (17 — `dossier_party`, `mandataire`), and the table rows
  « Fermer un dossier — Impossible ici » and « Créer un protocole, vérifier
  une identité, vérifier les conflits, gérer les mandataires — Impossible
  ici, par conception »; `references/vocabulaires.md`
  « `list_deletions.entity_type` (15) »; and, in SKILL.md's « Ajoute »
  row, « Les tableaux de parties sont en ajout seul » (a party link now
  leaves its array through `update_dossier_party` remove — what stays true
  is that `update_dossier` only ADDS). Incomplete rather than false, to
  extend the same day: « Ajoute » names `update_dossier.add_clients` alone —
  say that one party link is corrected or DETACHED through
  `update_dossier_party` (refused for the last client, a served party, a
  client with trust history) and names refreshed with its `refresh_names`;
  family D and the families table lack DOSSIERS and CONTACTS; the
  `get_partie` row should name `*_source` / `*_presumed` /
  `*_confirmed_at`; « Les suppressions » and the row « Supprimer quoi que
  ce soit — Aucun outil ne supprime » should say that a detached party or
  mandataire is a LINK, the contact staying, and that `list_deletions`
  lists those detaches (a `mandataire` row's `title` names the REPRESENTED
  contact); `references/vocabulaires.md` should list the five kinds of
  representation (`mandataire`, `tuteur`, `curateur`, `représentant_légal`,
  `autre`) and the two compliance vocabularies (identity: `non_vérifié`,
  `vérifié`, `exempté`; conflict: `non_vérifié`, `vérifié`,
  `conflit_détecté`). Add: `set_dossier_status`'s discipline (the lawyer's
  confirmation first — closing takes the dossier off his phone and out of
  the prescription alerts; read every warning; `dav.complete: false` → the
  SAME status again, same key — and if that retry is refused « encore en
  cours », wait, then the same key again; if « interrompu », re-read the
  dossier, then the status read under a NEW key — unless the warnings say
  the status moved during the call: then re-read first, and ask for the
  status read under a NEW key; a `refresh_names` that refused a dossier
  is retried the same way — never « the same key is fine » without these
  two exceptions); `record_kyc_status`'s (only on the
  lawyer's instruction, only for a client, never over his own decision —
  `*_presumed` false on a decided status means « tell him »; a detected
  conflict reported to him AT ONCE; notes are appended, never replaced);
  and `update_partie_mandataire`'s (an individual of the same role;
  `update` for a representation already listed); and what « Le dossier
  n'a pas pu être lu — réessayez. » means (fixes of lot 4 — every write
  that names a dossier): the store did not answer and NOTHING was
  written — send the same call again in a moment; never read it as
  « introuvable », and never create a dossier on the strength of it.
- **Lot 5 — accounting through the connector (5a: the model, service and
  web half, an ordinary release; 5b: six tools behind their OWN switch —
  branch `mcp-ecriture-lot5`, with lot 4 and any earlier lot of that stack
  not yet deployed, in ONE push).** 5b adds the READ `get_admin_ledger` and
  the ACCOUNTING family — `record_trust_entry` (a trust entry; its
  `virement_honoraires` is the fee payment, trust withdrawal + operations
  recette + the invoice's payment in ONE transaction), `record_admin_entry`
  (a dépense split TPS/TVQ, an other recette, an encaissement that pays the
  invoice in the same transaction, a card payment's two legs),
  `update_admin_entry` (an editable entry, against its etag),
  `clear_register_entries` (≤ 50 entries of one account, at the STATEMENT
  date) and `reverse_register_entry` (the only correction of a trust
  entry, and of an administration entry no longer editable) — all under the
  scope `athena:comptabilite` and its switch `MCP_COMPTABILITE_ENABLED`,
  every rule the MODELS' (the lock floor, the Montréal clock, arts. 57, 58
  and 59, the provision refusal). No index to deploy (the reads reuse the
  existing registers' indexes), no dependency, no Tailwind class, no icon,
  no `cron.yaml` or `firestore.rules` change, and no DavX5 account re-add
  (neither register nor an invoice is DAV-exposed). **The lot SHIPS with
  `MCP_COMPTABILITE_ENABLED: "false"`**, and in that state nothing is
  reachable: the six tools are hidden from every `tools/list` and refused at
  `tools/call`, the box is not offered — and no token in force holds the
  scope (it was never offered before 5b), so the push needs no revocation.
  **In this order** (the lot's own train; if an earlier lot of the stack
  rides in the same push, ITS train's revocation comes first — §15
  « Lot 4 » — since those lots widen `athena:write`):
  0. **Baseline, before the push** (read-only; the environment of the T3
     recipe above): `python -m scripts.verify_trust_integrity` and
     `python -m scripts.verify_admin_integrity`. Exit `0` — or, for the
     trust script only, `2` with every note read with the lawyer (the
     administration script has no notes: every finding it prints is an
     écart, exit `1`) —, admin check nº 8 (`amount_paid` ==
     Σ receipts, over EVERY invoice carrying a payment) clean above all: an
     encaissement the old regime never projected onto its invoice reads
     there as an écart, and 5a's atomic reversal REFUSES when an invoice's
     recorded payment is below the amount it reduces, so such an
     inconsistency must be repaired by hand first.
  1. **Push the lot as ONE deploy with `MCP_COMPTABILITE_ENABLED: "false"`.**
     5a's web half goes live (the fee payment in ONE transaction, its
     « Date du dépôt au compte d'administration », the stale-page refusals
     of the administration edit, « Contre-passer » and « Compenser » — tell
     the lawyer, CLAUDE.md Phase History « Lot 5 »), and nothing of 5b is
     reachable: the connector in force still lists **80** tools (31 read,
     49 write), and its `initialize` text says only that accounting tools
     « appear only under the SEPARATE `athena:comptabilite` grant ». The
     byte budget is the deploy gate's `tests/test_mcp_descriptor_budget.py`
     (251 KB of its 280 KB cap, the six counted).
  2. **Run both integrity scripts again, on the deployed version**, before
     anything is armed — `verify_trust_integrity` and
     `verify_admin_integrity`, read-only: exit `0` (the trust script `2`
     with every note read with the lawyer). The connector will write into
     these registers;
     an écart there first is one nobody could later tell from the
     connector's.
  3. **Arm the switch**: `MCP_COMPTABILITE_ENABLED: "true"` in `app.yaml`,
     and deploy. Nothing changes for the token in force: it lacks the scope,
     so it sees no accounting tool and reads the same `initialize` text (the
     box is merely OFFERED from now on).
  4. **Revoke and re-consent, READING the accounting box**:
     `python -m scripts.revoke_mcp_tokens`, remove the connector in
     claude.ai, re-add it, and read the accounting block before ticking
     « Autoriser la comptabilité » (with « Autoriser les écritures »). It
     says what it grants (reading the administration ledger; trust entries
     — their objet agreeing with their sens, a disbursement drawing only on
     CLEARED funds, a fee payment by cheque or transfer only, to the lawyer
     or his firm as « Paramètres » names them, on an Athéna invoice the
     lawyer SENT, addressed to the client whose funds leave, that imputes no
     provision, writing the withdrawal, the recette and
     the invoice's payment in one operation; administration entries, an
     encaissement that pays its invoice; corrections of an EDITABLE
     administration entry; clearing at the statement date; reversal — the
     only correction of a trust entry), the lock/future rule, and its
     « jamais » list — delete an entry, start / complete / abandon a
     reconciliation, create or modify an account or attach a receipt,
     transfer between dossiers or reverse a leg of such a transfer,
     withdraw cash, back a fee payment with a paper, unsent or
     provision-imputing invoice or with another client's invoice (or one
     naming no client, in a dossier of several), make a fee
     payment to anyone but the lawyer or his firm, show a bank number (the
     last two joined on the lawyer's decisions of 2026-09-29 — D21, D23).
     The write block's list
     now says a payment exists only as a register entry, and « écrire au
     fidéicommis » only with that box. Re-consenting while the switch is
     still `"false"` silently yields a grant WITHOUT accounting — step 3
     comes first.
  5. **Verify `tools/list` per scope.** The new token (all three scopes):
     **86** tools (32 read, 54 write), and its `initialize` text carries an
     « ACCOUNTING: » index line and the seven accounting « never » sentences
     (six until D23 added « fee_payee », 2026-09-29).
     The other token shapes are the deploy gate's literal pins
     (`tests/test_mcp_jsonrpc.py::test_tools_list_counts_per_token_are_the_train_s_checklist`):
     **31** read-only, **80** read + write (or any token while the switch is
     off), **37** read + comptabilité, **32** with `MCP_WRITE_ENABLED` off
     (the reads and `get_admin_ledger`). To see one on the wire, a second
     authorization without the accounting box must list 80. Then call
     `get_admin_ledger` and `get_trust_snapshot`: no transit, no account
     number, no last 4 digits anywhere in either payload, and the balances
     match « Comptabilité ».
  6. **A supervised pilot on a TEST administration account — never on the
     trust register, never on the real operations account.** Every entry is
     PERMANENT (an administration entry is deletable in the application
     only while unlocked, and a reversal pair never), so the pilot needs an
     account of its own: in the application, « Comptabilité » → new
     administration account, type opérations, named « Essai — connecteur »
     (no transit, no digits). First the refusals, which write nothing: a
     trust disbursement « comptant » (art. 57), a fee payment on a
     brouillon invoice (its refusal names the lawyer as the one who attests
     the sending, and says no way around it — D20), a fee payment naming
     another payee than the lawyer or his firm (D23), an entry dated
     tomorrow. Then, the lawyer watching,
     through the connector and each with its own `idempotency_key` — and
     telling Claude it is a pilot on the test account, since its
     instructions record only movements that happened at the bank:
     (a) `record_admin_entry` — a `dépense` of 1,00 $ on the test account,
     today, any `category` of its list, `ventilation` « sans_taxe » (no tax
     figure: nothing reaches a TPS/TVQ total), a `method` and a
     `counterparty` (both required) — the account's balance reads
     **−1,00 $**, the entry « en circulation »; (b) `clear_register_entries`
     (`register: admin`) on it at today's date, with the `etag` its
     `get_admin_ledger` row shows in `expected_etags` (required at admin
     since the finitions: an entry edited after that read is refused, never
     cleared at its new amount) — still **−1,00 $**, the
     entry « compensée » (clearing moves no balance: the ledger balance
     counts every status); (c) `reverse_register_entry` on it, with a
     `reason` — a cleared entry's reversal enters « en circulation », a
     real movement to come, and the balance reads **0,00 $** AT ONCE (the
     reversal counts from its creation); (d) `clear_register_entries` on
     the reversal — still **0,00 $**, now with nothing outstanding. A
     balance other than these after any step is an écart: stop there.
     After each step, open the entry in the application (its status, the
     test account's balance) and read it back with `get_admin_ledger` —
     its `created_via` / `cleared_via` read `"mcp"`, and the entry's page
     in the application shows « inscrite par Claude » / « compensée par
     Claude » beside its status (and « · par Claude » on a revision the
     connector made). Then close the test account
     (« Fermé ») in the application; its TWO entries (the dépense and its
     reversal) stay in the ledger for good, netting to zero.
  7. **Re-run both integrity scripts** (read-only): exit `0` (the trust
     script `2` with the notes read) — the test account's two entries
     among what the administration script checks (Σ = 0, the pair
     symmetric). Only then may the first REAL bank movement be recorded
     through the connector — the lawyer's, at its statement date.
  8. **Incident**: `MCP_COMPTABILITE_ENABLED: "false"` + deploy stops the
     six tools ONLY; `MCP_WRITE_ENABLED: "false"` stops every write,
     accounting included (the read `get_admin_ledger` answers to the
     accounting switch alone).
  9. **Then update BOTH copies of the claude.ai skill `pallas-athena` the
     same day.** What lot 5 makes false there (the synced copy of
     2026-09-17, `references/comptabilite.md` and `SKILL.md`):
     « Le fidéicommis est en lecture seule intégrale. Aucun outil n'y
     écrit. » (comptabilite.md §6); « Le connecteur ne change aucun statut
     de facture et n'enregistre aucun paiement » (§7, rule 5 — its status
     half false since lot 3 already); « enregistrer un paiement » among
     « Les écritures qu'aucun outil ne fait » (SKILL.md); the family « F.
     Comptabilité / fidéicommis » listing three READ tools only
     (`references/outils.md`); and every tool count (« 49 outils »,
     « 27 en lecture, 22 en écriture » — 86 = 32 + 54 under the accounting
     grant, 80 without it). Each is now true WITHOUT the accounting grant
     only, and the connector's own texts refuse those phrasings
     (`tests/test_mcp_disclosure.py` `KNOWN_FALSE_CLAIMS`). Add the
     discipline: record only a movement that HAPPENED at the bank, at the
     date it happened — the tools' own rule; a cheque just written is one,
     recorded en circulation before any statement shows it; one
     `idempotency_key` per movement, kept on a retry; an uncertain outcome
     (`accounting_outcome_uncertain`) is re-read (`list_trust_transactions`,
     `get_admin_ledger`) before anything else and retried only with the
     SAME key — and so is a reversal, a clearing or a correction answered
     « Erreur lors de … » (or refused as already reversed, already
     cleared, or changed since the read, right after such an answer): its
     commit may have landed with the answer lost, so it is never reported
     to the lawyer as failed before the register says so (a retry cannot
     apply it twice — OBSERVABILITY.md, `accounting_refused`); clear only
     at the statement date; a trust mistake is
     REVERSED, never re-entered on top, and an administration mistake is
     corrected with `update_admin_entry` while the entry stays editable;
     never a paper, unsent or provision-imputing invoice for a fee payment
     — nor another client's invoice, nor one naming no client in a
     dossier of several (D21), nor a payee other than the
     lawyer or his firm as « Paramètres » names them (D23, art. 58 — either
     name for either method; omitted, the lawyer on a cheque — art. 58
     draws a fee cheque « à l'ordre de l'avocat » —, the firm on a
     transfer); an invoice is marked « envoyée »
     only on the lawyer's word
     (D20 — the fee payment trusts that status); and an objet always agrees
     with its sens (D24 — the model refuses otherwise).
- **Finitions — the adversarial review of lots 0a-5 (branch
  `mcp-ecriture-finitions`, on top of `mcp-ecriture-lot5`, in the SAME push
  as the stack it sits on).** No train of its own: no scope, no switch, no
  tool (still **86**), no index, no dependency, no Tailwind class, no icon,
  no `cron.yaml` or `firestore.rules` change, no DavX5 account re-add. Its
  connector changes ride the ONE revocation of §15 « Déploiement unique
  (D22) » (step 5; the accounting ones reach a token only at its step
  17): INSTRUCTIONS open on a SAFETY CORE that a client cutting at
  2 048 characters reads whole (the « never » list, the one outbound
  effect, confirm-before-writing, idempotency and etag, re-read before a
  retry), then ONE index line per family naming its tools — the family
  prose moved into the tool descriptions, and the text weighs at most
  8 000 bytes for every token where it weighed 22-26 KB;
  `clear_register_entries` takes
  `expected_etags` (REQUIRED at the administration register);
  `record_document_analysis` takes an optional `expected_etag` and returns
  `entity` {id, etag}; `list_trust_transactions` rows gain ten optional
  keys (account, dossier, client, reference, description, invoice, reversal
  links, provenance); four write outputs require LESS than before (a
  same-key replay stored by the previous release still conforms); a write
  whose record could not be read answers `read_unavailable`, never
  « introuvable »; a create whose commit raised and whose read-back failed
  too answers `write_outcome_uncertain`, the key kept; and — the lawyer's
  decisions of 2026-09-29 — `record_trust_entry` refuses a fee payment's
  `counterparty` other than the lawyer or his firm (D23; omitted, the
  lawyer on a cheque, the firm on a transfer)
  and another client's invoice — or, in a dossier of several clients, one
  naming none (D21) —, its refusal of an unsent invoice
  names the lawyer as the one who attests the sending (D20), and the
  accounting INSTRUCTIONS carry a seventh « never » (`fee_payee`) — all
  under the accounting grant, which no token holds before §15 « Lot 5 »
  step 4. And D25 (the same day, under the ordinary write grant):
  `record_document_analysis` KEEPS a category the lawyer chose or
  confirmed — a confirmed analysis included —, the new analysis itself
  always PRESUMED (his confirmation of the previous one covered that one
  only, and is recorded, never carried over), and
  records the category the sub-nature derives beside a
  `divergence_categorie` flag and a warning; `list_documents` rows gain
  `divergence_categorie`; `update_document` judges the lawyer's rule
  before the analysis rule (« dites-le au juriste »). When BOTH copies of
  the claude.ai skill `pallas-athena` are updated on the day of the push
  (§15 « Déploiement unique (D22) », step 15 — D25 rides the ordinary
  write grant, so it does NOT wait for the accounting lists of « Lot 5 »
  step 9, deferred to step 17), add: an analysis no longer replaces the lawyer's category — a
  phrasing « l'analyse remplace la catégorie » (or « an analysis
  replacing it », the lot 2A wording above) is true of a PRESUMED
  category only; a gap is reported to him, never settled by a new
  analysis. The claude.ai skill « Analyse documentaire » is GENERATED, not
  edited: re-export it at that same step 15 (`python -m
  scripts.exporter_competence_analyse`) and re-paste it — its « Il
  **remplace** la catégorie stockée » bullet was unconditional until the
  review of D25. **Device-visible — tell the lawyer, then check after the
  deploy:**
  1. A DAV read that FAILS answers **503 + `Retry-After`** — a collection
     listing, a sync report, a contact, an event, a task, a note, a
     DELETE —, where it answered an empty 207, a 404, a 412, a 422 or a
     500. DavX5 retries; it deletes nothing. No outage can be staged in
     production, so check the ordinary path: `curl` a PROPFIND Depth:1 on
     one active dossier collection and on `/dav/addressbook/` — 207, every
     member listed —, then a phone sync that pulls one edit made in the
     application.
  2. A CONFIRMED Bookings rendez-vous: « ignorer » / « conserver » a
     divergence in Réception, then edit that event on the phone — it
     uploads (before, the phone's next edit answered 412 and was lost).
  3. On the phone, type a line AFTER the « Dossier: / Type: / Modalité: »
     block of an event's description, and after a task's « Dossier: »
     line; sync, then open both in the application: the notes and the
     description show the typed line and NO metadata block. Move an event
     from one dossier calendar to another on the phone: the new event's
     notes carry no block either.
  4. The web, on a read that fails: a note edit re-renders the form with
     the typed text and « n'a pas pu être lu — réessayez » (it used to
     return to the list, the edit lost); a task or hearing edit refuses
     the same way; a deletion deletes nothing and never tombstones
     « Général » (a task's or hearing's refused deletion still returns to
     the list without a banner, as before — the item is simply there).
  5. The trust entry form (« Fidéicommis » → « Nouvelle écriture »),
     the lawyer's decisions of 2026-09-29: the « Objet » list no longer
     offers « Virement inter-dossiers » (the « Virement inter-dossiers »
     screen stays the way); choosing « Dépôt du client » sets the sens to
     « Recette » (and « Remise au client » to « Déboursé »); on « Paiement
     d'honoraires » the « Bénéficiaire » becomes a choice between the two
     names « Paramètres » holds, the LAWYER preselected (the form opens on
     « chèque », and art. 58 draws a fee cheque « à l'ordre de l'avocat »;
     the firm stays selectable on either method — the rule the lawyer
     settled on 2026-09-29 (D23, clarified the same day): EITHER name is
     accepted for EITHER method, cheque or transfer, the lawyer's name
     being only the DEFAULT on a cheque) — check the names there FIRST: a
     profile naming neither refuses every fee payment. No real entry is
     needed to check it: an incoherent pair or another client's invoice is
     refused and writes nothing. Then `python -m scripts.verify_trust_integrity`
     (read-only): check 8's new NOTES (a single transfer leg, an incoherent
     objet/sens, a fee payment for another client's invoice, a payee
     outside the profile) describe what the register already holds — read
     them with the lawyer; nothing is repaired.
  6. A document whose category the lawyer chose or confirmed, analysed
     again through the connector (D25): its category stays. When the
     analysis derives another category, the fiche's « Analyse » card
     shows, in amber, « L'analyse suggère la catégorie X ; la vôtre est
     conservée ». The analysis itself reads « Présumée », its alerts shown
     and « Confirmer » offered, even when he had confirmed the previous
     one — the card's footer says « Analyse précédente confirmée le … »:
     tell him that his confirmation covered the previous analysis, not
     this one, and that he re-reads it, then confirms it. And in the edit form, correcting an analysis
     field other than the sous-nature no longer replaces the category
     (changing the sous-nature still re-derives it).
  Watch, the first week: the finitions' `unexpected` messages
  (OBSERVABILITY.md, « Messages of the finitions ») — a burst is an outage,
  a steady line on one id a stored document to repair.
- **Cold starts:** `min_instances: 0` (in `app.yaml`) trades a cold start for
  zero standing cost; set `1` to eliminate it (one always-on F2).
- **Dependencies:** edit `athena/requirements.in`, then re-lock —
  `uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt`
  (never hand-edit `requirements.txt`). Dependabot proposes weekly bumps; the
  four delicate subsystems in [CLAUDE.md](CLAUDE.md) should be re-verified after
  any bump to `icalendar`/`vobject`, `google-*`, or the OpenTelemetry stack.
- **Frontend assets:** if you change Tailwind classes, recompile
  `static/src/app.input.css` → `static/vendor/app.<hash>.css`, then fan the new
  hash out to **five** sites — `templates/base.html`,
  `templates/auth/login.html`, **`client/templates/base.html`**,
  `static/sw.js`'s PRECACHE (bumping `STATIC_CACHE` with it), and the Early
  Hints lists in `security.py` — and delete the old hashed file. Full recipe in
  [CLAUDE.md](CLAUDE.md) → Tech Stack.
  ⚠ **This list named four until 2026-09-13, and the one it omitted was
  `client/templates/base.html` — the PORTAL's own base.** Following it left the
  public, client-facing service pointing at a deleted filename served
  `Cache-Control: immutable` for a year: unstyled, for everyone, with no error
  anywhere. A test now pins this list against the files that actually carry the
  hash, so it cannot fall behind again.
- **Effacement Loi 25 — le clavardage interne est parti (2026-09-02).** Ce
  runbook visait le registre des conversations d'assistant, append-only par
  convention précisément pour que l'effacement reste TECHNIQUEMENT possible
  quand la loi l'exige. La fonctionnalité a été retirée, et ses données
  effacées puis vérifiées à zéro (15 conversations, 173 enfants de
  sous-collection, 11 objets Storage) par `scripts/effacer_clavardage.py`,
  l'acte étant consigné par les journaux d'audit Data Access activés à la
  §11b. Le principe reste bon pour toute collection append-only future :
  **une sous-collection ne cascade pas** (les enfants avant la tête), les
  compteurs agrégés sont de la comptabilité et non des renseignements
  personnels (on les laisse), et on ne script JAMAIS un effacement dans
  l'application.

---

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| A list view is unexpectedly empty | A composite index hasn't finished building (§6.3). Check Firestore → Indexes. |
| `403` on every request | You're hitting App Engine directly — use the Cloudflare hostname. `*.appspot.com` is blocked by design. |
| Document upload/download fails silently in prod | Missing `roles/iam.serviceAccountTokenCreator` (self) on the App Engine SA (§6.4). |
| Login rejected with *"Accès non autorisé"* | The Firebase user's email ≠ `AUTHORIZED_USER_EMAIL`. |
| Login rejected after password entry | `REQUIRE_MFA=true` but no second factor enrolled. |
| Warning about App Check in prod logs | `RECAPTCHA_ENTERPRISE_SITE_KEY` unset — App Check is fail-open. |
| DavX5 silently won't sync | A DAV Basic-Auth mismatch, or the account was not re-added after a DAV collection layout change. Test the endpoint with `curl` first — an anonymous `PROPFIND /dav/` must answer `401 WWW-Authenticate: Basic`. |
| Word shows a "repair" prompt on a generated doc | A template-engine change introduced a `docxtpl`/`python-docx` round-trip — forbidden (see CLAUDE.md). |

---

## 17. The steps no command can verify

`python -m scripts.provision` reports everything a read-only `gcloud` call can
establish. These are the rest — console work, edge configuration, a directory
you do not own. They carry **stable ids**, the same ones the script prints
under « à la main », so a checklist and a report can refer to the same thing.

This table is GENERATED from the `manual` rows of
[`athena/utils/deployment_inventory.py`](athena/utils/deployment_inventory.py)
and pinned against them by a test.

| Id | Step | What « done » looks like |
|---|---|---|
| `firebase-auth` | Firebase Auth : fournisseur mot de passe + lien courriel | activés dans la console, domaine du portail autorisé |
| `utilisateur-unique` | L'utilisateur unique, et son second facteur | un seul compte, l'adresse d'AUTHORIZED_USER_EMAIL, MFA inscrite |
| `seau-firebase-storage` | Seau Firebase Storage par défaut | initialisé dans la console, nom == FIREBASE_STORAGE_BUCKET |
| `app-check` | App Check + clé reCAPTCHA Enterprise | application web enregistrée, clé du domaine du portail incluse |
| `cloudflare` | Cloudflare : DNS, Full (Strict), Transform Rule, Configuration Rule | la Transform Rule zone-wide PROUVÉE par le traceur de requêtes AVANT que cf-origin-secret n'existe |
| `entra` | Entra ID : inscription d'application + permissions Graph | Mail.Send et Calendars.ReadWrite, consentement administrateur |
| `declencheur-cloud-build` | Déclencheur Cloud Build sur push vers main | connecté au dépôt, exécutant cloudbuild.yaml |

Each one fails in its own way when skipped, and the script prints that
consequence beside it. Three are worth repeating here because their failure is
**silent**: App Check fails OPEN, so the application works and the protection
simply does not exist; a Cloudflare Transform Rule created *after*
`cf-origin-secret` makes the site answer 403 everywhere with no deploy to
explain it; and an Entra permission change takes 30 minutes to 2 hours to
leave the Exchange cache, so « it still works » proves nothing about a
revocation you just made.

---

**See also:** [README.md](README.md) · [SECURITY.md](SECURITY.md) ·
[CLAUDE.md](CLAUDE.md) (developer reference) ·
[OBSERVABILITY.md](athena/OBSERVABILITY.md)
