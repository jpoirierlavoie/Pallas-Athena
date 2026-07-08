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
Browser / DavX5 / Claude
        │
   Cloudflare  (TLS, WAF, Access on /dav/*, Rocket Loader, Early Hints, origin secret)
        │
  App Engine Standard (Flask + gunicorn, Python 3.13)
        │
  ┌─────┴───────────────┬──────────────────┬───────────────┐
Firestore        Firebase Storage     Firebase Auth    Secret Manager
(native mode)    (documents/gabarits) (+ Phone MFA)    (4 secrets)
```

---

## 2. Prerequisites

**Accounts**
- A **Google Cloud** account with billing enabled.
- A **Cloudflare** account on the **Pro plan** (≈ US$25/mo) — required for
  Access (Zero Trust on `/dav/*`), Rocket Loader, and Early Hints.
- A **domain name** you control (this guide uses `yourdomain.example`).
- *Optional:* a **Google Play Console** account (only for the Android TWA, §12).
- *Optional:* a **claude.ai** account (only for the MCP connector, §11).

**Local tools**
- Python **3.13**
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install)
- [`firebase` CLI](https://firebase.google.com/docs/cli) (`npm i -g firebase-tools`)
- [`uv`](https://github.com/astral-sh/uv) (only if you change dependencies)
- Node.js + npm (only if you recompile the CSS — see §15)
- `git`, and Python's `bcrypt` (`pip install bcrypt`) for generating the DAV hash

---

## 3. Cost expectations (rough, low-traffic single user)

| Component | Ballpark |
|---|---|
| App Engine F2, `min_instances: 0` | a few $/mo (pay-per-request; cold starts) |
| App Engine F2, `min_instances: 1` | ~US$40–50/mo (one always-on instance, no cold starts) |
| Firestore (native) | cents–low $/mo at single-user volume |
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

Set these in [`athena/app.yaml`](athena/app.yaml) (non-secret identifiers) for
production, and in a local `.env` (from [`.env.example`](.env.example)) for
development. **Never** put secrets in `app.yaml`.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `ENV` | ✔ | `development` | `production` switches secret resolution to Secret Manager |
| `SECRET_KEY` → secret `flask-secret-key` | ✔ **hard** | — | Flask session signing; **app won't boot without it** |
| `FIREBASE_PROJECT_ID` | ✔ **hard** | — | Your GCP/Firebase project id |
| `FIREBASE_STORAGE_BUCKET` | ✔ **hard** | — | Your Storage bucket name |
| `AUTHORIZED_USER_EMAIL` | ✔ **hard** | — | The single user; also the DAV username |
| `FIREBASE_APP_ID` | ○ | `""` | Web-app id used by the App Check bootstrap |
| `FIREBASE_API_KEY` → secret `firebase-api-key` | ○ | `""` | Public browser key (safe to expose, kept out of git) |
| `DAV_PASSWORD_HASH` → secret `dav-password-hash` | ○ (needed for DAV) | `""` | **bcrypt** hash of the DAV password |
| `RECAPTCHA_ENTERPRISE_SITE_KEY` | ○ (recommended) | `""` | App Check; **fail-open + loud warning** if unset in prod |
| `APPCHECK_DEBUG_TOKEN` | ○ | `""` | Local dev only |
| `CF_ORIGIN_SECRET` → secret `cf-origin-secret` | ○ | `""` | Edge origin check; **fail-open** if unset |
| `REQUIRE_MFA` | ○ | `true` | Enforce Phone MFA |
| `SESSION_LIFETIME_HOURS` | ○ | `12` | Server-side session lifetime |
| `RATE_LIMIT_LOGIN` | ○ | `5 per minute` | Login rate limit |
| `MCP_ENABLED` | ○ | `true` | `false` → all `/mcp` + `/oauth/*` routes 404 |
| `MCP_CANONICAL_ORIGIN` | ○ | owner domain in [config.py](athena/config.py) | OAuth issuer — **must be your domain** |
| `FIRM_NAME` … `FIRM_EMAIL`, `GST_NUMBER`, `QST_NUMBER` | ○ | mostly `""` | Invoice header + tax numbers |
| `TRACE_SAMPLE_RATIO` | ○ | `0.1` | Cloud Trace sampling (read by `tracing_setup.py`) |
| `PIP_REQUIRE_HASHES`, `PIP_NO_DEPS` | ✔ (prod) | `1`, `1` | Supply-chain: reject unhashed/out-of-band installs |

### 4.2 The four Secret Manager secrets (production)

`flask-secret-key` (required), `firebase-api-key`, `dav-password-hash`,
`cf-origin-secret`. Created in §6.4.

### 4.3 Owner-specific values to replace

Every value below is currently hardcoded to the original deployment. Replace
each one, then run the checker in §4.4.

| Value | Where |
|---|---|
| GCP project id | `app.yaml`, every `gcloud`/`firebase --project` command, `.env` |
| Firebase app id | `app.yaml` |
| Storage bucket | `app.yaml` |
| Authorized email | `app.yaml` |
| `FIRM_NAME` | `app.yaml` |
| **reCAPTCHA site key** | `app.yaml` — replace **and** rotate the old one |
| MCP canonical origin default | [config.py](athena/config.py) |
| Firm name / domain / contact | [static/legal/privacy.html](athena/static/legal/privacy.html), [terms.html](athena/static/legal/terms.html), [README.md](README.md), [SECURITY.md](SECURITY.md), [LICENSE](LICENSE) |
| TWA package + SHA-256 fingerprint | [main.py](athena/main.py) `assetlinks.json` route (only if you build the Android app, §12) |

### 4.4 Verify your configuration

A helper script checks required vars, warns on disabled fail-open controls, and
flags any owner-default literal you forgot to replace:

```bash
cd athena
python -m scripts.check_config          # uses ENV from your shell/.env
python -m scripts.check_config --prod   # force the production ruleset
```

---

## 5. Order of operations (read this before running anything)

Two steps are **irreversible or order-sensitive**:

1. Create GCP project + enable billing
2. Enable required APIs
3. **Create the App Engine app — the region choice is PERMANENT** (§6.2)
4. Create Firestore in native mode (same region)
5. **Deploy indexes + rules — BEFORE the first code deploy** (§6.3). Until an
   index finishes building, the query it serves fails and the view silently
   shows an empty list.
6. Create the 4 secrets + grant IAM (§6.4)
7. Firebase Auth: create the single user + enroll Phone MFA (§6.5)
8. Storage bucket (§6.6)
9. App Check + reCAPTCHA (§6.7)
10. First deploy + smoke test (§8)
11. Seed reference data (§9)
12. Cloudflare edge (§7 — can be prepared in parallel, but DNS cutover comes here)
13. Optional: DavX5 (§10), MCP (§11), Android TWA (§12)

---

## 6. Google Cloud & Firebase provisioning

Throughout, set `PROJECT=your-project-id` and substitute your own values.

### 6.1 Project, billing, APIs

```bash
gcloud projects create $PROJECT --name="Pallas Athena"
gcloud billing projects link $PROJECT --billing-account=XXXXXX-XXXXXX-XXXXXX
firebase projects:addfirebase $PROJECT

gcloud services enable \
  appengine.googleapis.com firestore.googleapis.com firebase.googleapis.com \
  firebaseappcheck.googleapis.com identitytoolkit.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  logging.googleapis.com cloudtrace.googleapis.com \
  recaptchaenterprise.googleapis.com iam.googleapis.com \
  --project=$PROJECT
```

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

# From the repo root (firebase.json points at the root rule/index files):
firebase deploy --only firestore:indexes,firestore:rules,storage --project $PROJECT
```

- **Native mode**, not Datastore mode.
- The rules are intentional **deny-all** — all access is through the Admin SDK
  and signed URLs, which bypass rules. This is also what stops a self-signed-up
  Firebase account from reading your data.
- `firestore.indexes.json` contains the composite indexes (dashboard
  aggregations, cursor-paginated lists, the protocol-steps collection group).
  **They must finish building before you send real traffic.**

### 6.4 Secret Manager + IAM

Create the secrets (only `flask-secret-key` is strictly required):

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))" \
  | gcloud secrets create flask-secret-key --data-file=- --project=$PROJECT

# bcrypt hash for the DAV password (pick your own password):
python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_DAV_PASSWORD', bcrypt.gensalt()).decode())" \
  | gcloud secrets create dav-password-hash --data-file=- --project=$PROJECT

printf '%s' 'YOUR_FIREBASE_WEB_API_KEY' | gcloud secrets create firebase-api-key --data-file=- --project=$PROJECT
python -c "import secrets; print(secrets.token_urlsafe(32))" \
  | gcloud secrets create cf-origin-secret --data-file=- --project=$PROJECT   # also paste into the Cloudflare Transform Rule (§7)
```

Grant IAM. The two service accounts are the **App Engine default SA**
(`$PROJECT@appspot.gserviceaccount.com`) and whatever SA your **Cloud Build
trigger runs as** (see the note below):

```bash
AE_SA="$PROJECT@appspot.gserviceaccount.com"

# App Engine runtime SA:
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$AE_SA" --role="roles/cloudtrace.agent"
for s in flask-secret-key firebase-api-key dav-password-hash cf-origin-secret; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$AE_SA" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

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

Get `FIREBASE_APP_ID` and the browser API key from **Project settings → Your
apps → Web app** (register one if none exists). Put the app id in `app.yaml`
and the API key in the `firebase-api-key` secret.

### 6.6 Firebase Storage

Initialize the default bucket (Firebase console → **Storage → Get started**).
Its name must equal `FIREBASE_STORAGE_BUCKET`. The deny-all `storage.rules` you
deployed in §6.3 already protects it; files are served only via 15-minute signed
URLs.

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

---

## 7. Cloudflare edge

All traffic must reach App Engine **through** Cloudflare; the app actively
rejects direct access. Set up, in order:

1. **DNS:** add your zone in Cloudflare, then a **proxied** (orange-cloud)
   record for `yourdomain.example` pointing at your App Engine custom-domain
   target (map the domain first under App Engine → Settings → Custom domains).
2. **SSL/TLS:** set the mode to **Full (Strict)** and install an **Origin
   Certificate** on App Engine's custom domain.
3. **App Engine firewall:** restrict ingress to **Cloudflare's published IP
   ranges** only, so the origin can't be hit directly.
4. **Origin secret (Transform Rule):** add a request Transform Rule that injects
   header `X-Origin-Auth: <cf-origin-secret value>` on every request, zone-wide.
   The app checks it (`security.py`) when `CF_ORIGIN_SECRET` is set. This is the
   second layer that defeats a spoofed-Host direct hit.
5. **Rocket Loader:** enable it. ⚠️ The end-of-`<body>` script order in
   `base.html` is load-bearing under Rocket Loader — don't reorder those scripts.
6. **Early Hints:** enable it (the app emits the `Link` preload headers).
7. Point `MCP_CANONICAL_ORIGIN` and `AUTHORIZED_USER_EMAIL`'s domain at
   `yourdomain.example`, and update the legal pages / README / SECURITY.

---

## 8. First deploy + smoke test

**Deploy** either by connecting a Cloud Build trigger on push to `main` (runs
the pytest gate → `gcloud app deploy` → prunes old versions), or manually:

```bash
cd athena
gcloud app deploy app.yaml --project=$PROJECT
```

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
- Front `/dav/*` (only) with **Cloudflare Access (Zero Trust)**: a service-token
  policy for DavX5 plus a Google-SSO policy for interactive use.
- In DavX5, add the account by URL (`https://yourdomain.example/`), with HTTP
  Basic credentials: username = `AUTHORIZED_USER_EMAIL`, password = the plaintext
  whose bcrypt hash is in `dav-password-hash`. Also add the Cloudflare Access
  service-token headers (`CF-Access-Client-Id`, `CF-Access-Client-Secret`) as
  custom headers, or the edge blocks the request before Basic auth runs.

---

## 11. Optional — MCP connector for Claude

Skip entirely by setting `MCP_ENABLED=false` (all `/mcp` + `/oauth/*` routes
404). If you keep it:

```bash
gcloud firestore fields ttls update expire_at --collection-group=oauth_codes  --enable-ttl --project=$PROJECT
gcloud firestore fields ttls update expire_at --collection-group=oauth_tokens --enable-ttl --project=$PROJECT
```

(TTL is garbage collection only — expiry is enforced in code regardless.)

- Ensure **Cloudflare Access does NOT cover** `/mcp`, `/oauth/*`, or
  `/.well-known/oauth*` (only `/dav/*`).
- Add a Cloudflare **Configuration Rule** disabling Browser Integrity Check on
  those paths (Claude's server is not a browser); watch Security → Events for
  Super Bot Fight Mode challenging Anthropic's egress.
- In claude.ai: **Settings → Connectors → Add custom connector →**
  `https://yourdomain.example/mcp`, then complete Firebase login + MFA on the
  consent screen and click **« Autoriser »**.

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
python -m scripts.check_config     # sanity-check your .env
flask run --debug                  # http://127.0.0.1:5000
# production-like: gunicorn -b :8080 main:app
python -m pytest tests/ -q
```

Notes:
- **Firestore emulator** (`gcloud emulators firestore start`) requires exporting
  `FIRESTORE_EMULATOR_HOST` before running Flask/scripts — otherwise the Admin
  SDK targets **live** Firestore via Application Default Credentials.
- `scripts/seed_reference_data.py` and `scripts/normalize_existing.py` do **not**
  read `.env`; they need `GOOGLE_APPLICATION_CREDENTIALS` / ADC.
- For local MCP testing, run on `:8080` (or set `MCP_CANONICAL_ORIGIN` to match
  your port) and mint a dev token: `python -m scripts.mint_dev_token`.
- If you have not enrolled Phone MFA locally, set `REQUIRE_MFA=false` in `.env`.

---

## 15. Operations

- **Backups / DR:** see [SECURITY.md](SECURITY.md#data-handling). Configure
  Firestore scheduled backups (or Point-in-Time Recovery) and Firebase Storage
  object versioning for *your* project — they are per-project settings, not code.
- **Monitoring:** Cloud Logging (log name `pallas-athena`) and Cloud Trace. The
  event/span vocabulary is in [OBSERVABILITY.md](athena/OBSERVABILITY.md).
- **CSP:** ships in **Report-Only** mode. Switch to enforcing `Content-Security-
  Policy` only after a clean reporting window (`security.py`).
- **Rollback:** Cloud Build keeps the 3 most-recent non-serving versions.
  `gcloud app versions list`, then migrate traffic:
  `gcloud app services set-traffic default --splits=<VERSION>=1`.
- **Cold starts:** `min_instances: 0` (in `app.yaml`) trades a cold start for
  zero standing cost; set `1` to eliminate it (one always-on F2).
- **Dependencies:** edit `athena/requirements.in`, then re-lock —
  `uv pip compile requirements.in --python-version 3.13 --universal --generate-hashes -o requirements.txt`
  (never hand-edit `requirements.txt`). Dependabot proposes weekly bumps; the
  four delicate subsystems in [CLAUDE.md](CLAUDE.md) should be re-verified after
  any bump to `icalendar`/`vobject`, `google-*`, or the OpenTelemetry stack.
- **Frontend assets:** if you change Tailwind classes, recompile
  `static/src/app.input.css` → `static/vendor/app.<hash>.css` and fan the new
  hash out to `base.html`, `auth/login.html`, `sw.js` PRECACHE, and the Early
  Hints lists in `security.py` (full recipe in [CLAUDE.md](CLAUDE.md) → Tech Stack).

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
| DavX5 silently won't sync | Missing Cloudflare Access service-token headers, or a DAV Basic-Auth mismatch. Test the endpoint with `curl` first. |
| Word shows a "repair" prompt on a generated doc | A template-engine change introduced a `docxtpl`/`python-docx` round-trip — forbidden (see CLAUDE.md). |

---

**See also:** [README.md](README.md) · [SECURITY.md](SECURITY.md) ·
[CLAUDE.md](CLAUDE.md) (developer reference) ·
[OBSERVABILITY.md](athena/OBSERVABILITY.md)
