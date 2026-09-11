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
   Cloudflare  (TLS, WAF, Early Hints, origin secret)
        │
  App Engine Standard (Flask + gunicorn, Python 3.13)
        │
  ┌─────┴───────────────┬──────────────────┬───────────────┐
Firestore        Firebase Storage     Firebase Auth    Secret Manager
(native mode)    (documents/gabarits) (+ Phone MFA)    (6 secrets)
```

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
| `REQUIRE_MFA` | ○ | `true` | Enforce Phone MFA. Read by « Paramètres → Sécurité » to decide whether the last enrolled factor may be removed |
| `SESSION_LIFETIME_HOURS` | ○ | `12` | Server-side session lifetime |
| `RATE_LIMIT_LOGIN` | ○ | `5 per minute` | Login rate limit |
| `MCP_ENABLED` | ○ | `true` | `false` → all `/mcp` + `/oauth/*` routes 404 |
| `MCP_CANONICAL_ORIGIN` | ○ | owner domain in [config.py](athena/config.py) | OAuth issuer — **must be your domain** |
| `FIRM_NAME` … `FIRM_EMAIL`, `FIRM_FAX`, `GST_NUMBER`, `QST_NUMBER` | ○ | mostly `""` | **Seed and fallback only.** The live firm profile is edited in « Paramètres » and stored at `settings/cabinet`; these bootstrap a fresh deploy and are what the app falls back to if Firestore is unreadable. The `portail` service is the exception — it reads `FIRM_NAME`/`FIRM_PHONE` from `portail.yaml` and cannot reach the singleton |
| `TRACE_SAMPLE_RATIO` | ○ | `0.1` | Cloud Trace sampling (read by `tracing_setup.py`) |
| `PIP_REQUIRE_HASHES`, `PIP_NO_DEPS` | ✔ (prod) | `1`, `1` | Supply-chain: reject unhashed/out-of-band installs |

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
6. Create the 6 secrets + grant IAM (§6.4)
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
  telemetry.googleapis.com \
  recaptchaenterprise.googleapis.com iam.googleapis.com \
  --project=$PROJECT
```

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

# 3. Voir ce qui a réellement été saisi, puis laisser le SHELL juger. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement ; le compte, lui, attrape ce que l'œil ne peut pas voir — un collage tronqué à sa première ligne (entre 32 et 256 octets attendus).
printf '[%s]\n' "$VALEUR"
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi

# 4. Écrire la NOUVELLE VERSION — `versions add`, jamais `secrets create` : le secret existe déjà, et `create` échouerait en laissant croire à une panne. `printf` est une primitive du shell, donc la valeur ne passe pas par `/proc/*/cmdline` ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps`.
printf '%s' "$VALEUR" | gcloud secrets versions add flask-secret-key \
  --project=$PROJECT --data-file=-

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire ce qui est STOCKÉ : entre 32 et 256 octets, et le même compte qu'au le contrôle d'avant-vol. C'est l'après-vol, et il est plus fort n'importe quel contrôle d'avant-vol — il interroge la valeur réellement enregistrée.
N=$(gcloud secrets versions access latest --secret=flask-secret-key \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
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

# 3. Voir ce qui a réellement été saisi, puis laisser le SHELL juger. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement ; le compte, lui, attrape ce que l'œil ne peut pas voir — un collage tronqué à sa première ligne (entre 32 et 256 octets attendus).
printf '[%s]\n' "$VALEUR"
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi

# 4. Écrire la NOUVELLE VERSION — `versions add`, jamais `secrets create` : le secret existe déjà, et `create` échouerait en laissant croire à une panne. `printf` est une primitive du shell, donc la valeur ne passe pas par `/proc/*/cmdline` ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps`.
printf '%s' "$VALEUR" | gcloud secrets versions add portail-secret-key \
  --project=$PROJECT --data-file=-

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire ce qui est STOCKÉ : entre 32 et 256 octets, et le même compte qu'au le contrôle d'avant-vol. C'est l'après-vol, et il est plus fort n'importe quel contrôle d'avant-vol — il interroge la valeur réellement enregistrée.
N=$(gcloud secrets versions access latest --secret=portail-secret-key \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi
```

#### `firebase-api-key` — Clé d'API navigateur Firebase

If this value is wrong or absent: la page de connexion ne peut pas initialiser Firebase.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR à l'étape suivante, pas les perdre en silence.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi, puis laisser le SHELL juger. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement ; le compte, lui, attrape ce que l'œil ne peut pas voir — un collage tronqué à sa première ligne (entre 30 et 60 octets attendus).
printf '[%s]\n' "$VALEUR"
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi

# 3. Écrire la NOUVELLE VERSION — `versions add`, jamais `secrets create` : le secret existe déjà, et `create` échouerait en laissant croire à une panne. `printf` est une primitive du shell, donc la valeur ne passe pas par `/proc/*/cmdline` ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps`.
printf '%s' "$VALEUR" | gcloud secrets versions add firebase-api-key \
  --project=$PROJECT --data-file=-

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire ce qui est STOCKÉ : entre 30 et 60 octets, et le même compte qu'au le contrôle d'avant-vol. C'est l'après-vol, et il est plus fort n'importe quel contrôle d'avant-vol — il interroge la valeur réellement enregistrée.
N=$(gcloud secrets versions access latest --secret=firebase-api-key \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -ge 30 ] && [ "$N" -le 60 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
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

# 2. En local, bcrypt est déjà dans l'environnement du dépôt (c'est une dépendance épinglée) : sauter cette ligne. Elle sert dans Cloud Shell, où il n'est pas préinstallé.
"$PY" -m pip install --quiet --user bcrypt

# 3. Calculer l'empreinte ET l'écrire en UNE commande : le mot de passe n'est jamais un argument, jamais une variable, jamais dans l'historique. `sys.stdout.write` n'ajoute pas de saut de ligne — c'est pourquoi aucun `tr -d` n'éponge la CHARGE. (Le contrôle de l'étape suivante en emploie un, mais sur la sortie de `wc -c` : il nettoie un compte, il ne touche pas au secret.)
"$PY" -c 'import bcrypt, getpass, sys; sys.stdout.write(bcrypt.hashpw(getpass.getpass("Mot de passe DAV : ").encode(), bcrypt.gensalt()).decode())' \
  | gcloud secrets versions add dav-password-hash \
      --project=$PROJECT --data-file=-

# 4. Relire ce qui est STOCKÉ — le compte doit valoir exactement 60, et c'est le SHELL qui compare. Une empreinte de 61 octets n'égalera jamais les 60 que `bcrypt.checkpw` recalcule, et DavX5 cesse alors de synchroniser sans un mot.
N=$(gcloud secrets versions access latest --secret=dav-password-hash \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -eq 60 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
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

# 3. Voir ce qui a réellement été saisi, puis laisser le SHELL juger. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement ; le compte, lui, attrape ce que l'œil ne peut pas voir — un collage tronqué à sa première ligne (entre 32 et 128 octets attendus).
printf '[%s]\n' "$VALEUR"
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi

# 4. Écrire la NOUVELLE VERSION — `versions add`, jamais `secrets create` : le secret existe déjà, et `create` échouerait en laissant croire à une panne. `printf` est une primitive du shell, donc la valeur ne passe pas par `/proc/*/cmdline` ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps`.
printf '%s' "$VALEUR" | gcloud secrets versions add cf-origin-secret \
  --project=$PROJECT --data-file=-

# 5. Effacer la variable de la session.
unset VALEUR

# 6. Relire ce qui est STOCKÉ : entre 32 et 128 octets, et le même compte qu'au le contrôle d'avant-vol. C'est l'après-vol, et il est plus fort n'importe quel contrôle d'avant-vol — il interroge la valeur réellement enregistrée.
N=$(gcloud secrets versions access latest --secret=cf-origin-secret \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -ge 32 ] && [ "$N" -le 128 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi
```

#### `graph-client-secret` — Secret client Microsoft Graph

If this value is wrong or absent: le courriel sortant est désactivé.

```bash
# 1. Coller la valeur remise par la console, puis Entrée. Rien ne s'affiche : `-s` la tait, et `IFS=` empêche le shell de manger les blancs — s'il y en a, on veut les VOIR à l'étape suivante, pas les perdre en silence.
IFS= read -rs VALEUR

# 2. Voir ce qui a réellement été saisi, puis laisser le SHELL juger. Les crochets rendent visible une espace de tête ou de queue, qu'aucune console ne montre autrement ; le compte, lui, attrape ce que l'œil ne peut pas voir — un collage tronqué à sa première ligne (entre 20 et 256 octets attendus).
printf '[%s]\n' "$VALEUR"
N=$(printf '%s' "$VALEUR" | wc -c | tr -d ' ')
if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
fi

# 3. Écrire la NOUVELLE VERSION — `versions add`, jamais `secrets create` : le secret existe déjà, et `create` échouerait en laissant croire à une panne. `printf` est une primitive du shell, donc la valeur ne passe pas par `/proc/*/cmdline` ; `--data-file=-` plutôt que `--data=`, qui la déposerait dans `ps`.
printf '%s' "$VALEUR" | gcloud secrets versions add graph-client-secret \
  --project=$PROJECT --data-file=-

# 4. Effacer la variable de la session.
unset VALEUR

# 5. Relire ce qui est STOCKÉ : entre 20 et 256 octets, et le même compte qu'au le contrôle d'avant-vol. C'est l'après-vol, et il est plus fort n'importe quel contrôle d'avant-vol — il interroge la valeur réellement enregistrée.
N=$(gcloud secrets versions access latest --secret=graph-client-secret \
  --project=$PROJECT | wc -c | tr -d ' ')
if [ "$N" -ge 20 ] && [ "$N" -le 256 ]; then
  printf 'longueur %s octets — conforme\n' "$N"
else
  printf 'longueur %s octets — REFUSER, ne rien écrire\n' "$N"
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
#   gcloud secrets add-iam-policy-binding portail-secret-key #     --member="serviceAccount:portail-svc@$PROJECT.iam.gserviceaccount.com" #     --role="roles/secretmanager.secretAccessor" --project=$PROJECT
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
3. **App Engine firewall:** restrict ingress to **Cloudflare's published IP
   ranges** only, so the origin can't be hit directly.
4. **Origin secret (Transform Rule):** add a request Transform Rule that injects
   header `X-Origin-Auth: <cf-origin-secret value>` on every request, zone-wide.
   The app checks it (`security.py`) when `CF_ORIGIN_SECRET` is set. This is the
   second layer that defeats a spoofed-Host direct hit.
5. **Rocket Loader:** leave it **off** — the original enabled it, then disabled
   it at the edge on 2026-07-11, and it is not returning. ⚠️ The end-of-`<body>`
   script order in `base.html` is still load-bearing: the App Check boot runs synchronously and htmx/Alpine
   are `defer`, but all resolve in document order — don't reorder them.
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
python -m scripts.check_config     # sanity-check your .env
flask run --debug                  # http://127.0.0.1:5000
# production-like: gunicorn -b :8080 main:app
python -m pytest tests/ -q
```

Notes:
- **Firestore emulator** (`gcloud emulators firestore start`) requires exporting
  `FIRESTORE_EMULATOR_HOST` before running Flask/scripts — otherwise the Admin
  SDK targets **live** Firestore via Application Default Credentials.
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

**See also:** [README.md](README.md) · [SECURITY.md](SECURITY.md) ·
[CLAUDE.md](CLAUDE.md) (developer reference) ·
[OBSERVABILITY.md](athena/OBSERVABILITY.md)
