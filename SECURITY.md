# Security Policy

Pallas Athena is a single-user legal practice management app handling confidential client data subject to professional secrecy obligations under the Code of ethics of advocates of the *Barreau du Québec*. Security is taken seriously.

## Reporting a vulnerability

If you discover a security issue, **do not open a public GitHub issue**. Email the maintainer directly:

**Contact:** Jason Poirier Lavoie — `security@poirierlavoie.ca`

Please include:
- A description of the vulnerability
- Steps to reproduce (or a proof-of-concept)
- The potential impact you've identified
- Any suggested mitigation, if you have one

You'll get an initial acknowledgement within 72 hours. Coordinated disclosure is appreciated — please give a reasonable window to patch before publishing details.

## Scope

In scope:
- The deployed application at `athena.poirierlavoie.ca`
- Code in this repository
- DAV endpoints (`/dav/*`)

Out of scope:
- Social engineering of the maintainer or anyone else
- Physical attacks
- DoS / volumetric attacks
- Issues in third-party dependencies that don't affect this app's specific configuration (report those upstream)
- Findings that require already-compromised credentials or compromised endpoint devices

## How secrets are managed

This is a single-user, single-environment app. Secrets live in two places:

**Production (Google App Engine):**
- Sensitive values (`SECRET_KEY`, `FIREBASE_API_KEY`, `DAV_PASSWORD_HASH`) are stored in [Google Cloud Secret Manager](https://cloud.google.com/secret-manager) and read at startup by `config.py`.
- Non-sensitive identifiers (project ID, storage bucket, App ID, reCAPTCHA site key) live as plain env vars in `app.yaml`.
- The App Engine default service account (`athena-pallas@appspot.gserviceaccount.com`) holds `roles/secretmanager.secretAccessor` on each secret.
- The Firebase Admin SDK uses Application Default Credentials — there is no service-account JSON file deployed.

**Local development:**
- A `.env` file at the repo root (gitignored) supplies env vars for local dev.
- The Firebase Admin SDK JSON file is stored **outside** the repo (e.g., `~/.config/athena/firebase-admin.json`) and referenced via `GOOGLE_APPLICATION_CREDENTIALS`. Alternatively, `gcloud auth application-default login` is used.

**Never committed:**
- `.env` and any `.env.*` files
- Service-account JSON files (matched by `**/service-account*.json`, `**/*-adminsdk-*.json` in `.gitignore`)
- Firebase Admin SDK private keys
- Bcrypt hashes, API keys, session secrets, debug tokens

If you spot any secret in this repository (current files or git history), please report it as a vulnerability.

## Defense-in-depth controls

- **Authentication:** Firebase Auth (email/password) with Phone MFA. Single authorized email enforced server-side.
- **Authorization:** Every state-mutating endpoint requires `@login_required`.
- **CSRF:** `flask-wtf` `CSRFProtect` on all POST/PUT/DELETE.
- **App Check:** Firebase App Check with reCAPTCHA Enterprise on HTMX requests.
- **DAV auth:** Separate HTTP Basic Auth (bcrypt-hashed password) plus Cloudflare Access Zero Trust on `/dav/*`.
- **Edge:** Cloudflare in front of App Engine with Full Strict TLS. Direct App Engine access (`*.appspot.com`) is rejected by a `before_request` hook.
- **Headers:** HSTS (2-year), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, restrictive `Permissions-Policy`. CSP is currently report-only.
- **Rate limiting:** `flask-limiter` on `/auth/login`.
- **Input handling:** All user input is sanitized via `security.sanitize()` and length-capped per field.
- **Storage:** Firebase Storage URLs are signed with 15-minute expiry; raw bucket URLs are never exposed.

## Credential rotation

If you have reason to believe any credential has been exposed:

1. **Firebase API key:** Regenerate in [GCP Console → Credentials](https://console.cloud.google.com/apis/credentials), update the Secret Manager `firebase-api-key` secret.
2. **Firebase Admin SDK key:** Generate a new key in [Firebase Console → Service Accounts](https://console.firebase.google.com/project/_/settings/serviceaccounts/adminsdk) and delete the old one. App Engine uses ADC — no deployed file needs changing. Update local dev path if applicable.
3. **Flask `SECRET_KEY`:** Generate via `python -c "import secrets; print(secrets.token_hex(32))"`. Update Secret Manager `flask-secret-key`. All existing sessions will be invalidated.
4. **DAV password:** Generate new bcrypt hash via `python -c "import bcrypt; print(bcrypt.hashpw(b'NEW_PASSWORD', bcrypt.gensalt(12)).decode())"`. Update Secret Manager `dav-password-hash`. Re-pair DavX5 client.
5. **App Check debug token:** Revoke in Firebase Console → App Check → Manage debug tokens. Generate a new one for local dev.

Redeploy after rotating Secret Manager values:
```
gcloud app deploy athena/app.yaml --project=athena-pallas
```

## Supported versions

This is a single-deployment app — only the currently deployed `main` branch is "supported". Old App Engine versions exist for rollback but receive no fixes; the cleanup step in `cloudbuild.yaml` retains only the three most recent non-serving versions.

## Data handling

- Client data lives in Firestore (native mode) and Firebase Storage, both in `northamerica-northeast1` (Montréal).
- All access is logged via Cloud Logging; PII is **not** logged at the application level.
- **Firestore:** native scheduled backups, daily (7-day retention) and weekly (14-week retention), stored in `northamerica-northeast1`.
- **Firebase Storage:** object versioning enabled, with a 180-day non-current version retention policy. GCS bucket-level soft delete provides an additional 7-day window to undelete objects.
- Data subject rights (access, deletion, rectification) are handled manually by the maintainer.
