# Pallas Athena — Legal Practice Management

## Project Overview
Single-user legal practice management application for a Québec civil litigation lawyer. Manages clients, dossiers (case files), billable hours, expenses, invoices, hearing dates, tasks, case protocols, and procedural documents. Synchronizes with DavX5 via CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL) endpoints.

Full specification: see SPEC.md

## Tech Stack
- Python 3.11+ / Flask
- Google Cloud Firestore (native mode, NOT Datastore mode)
- Firebase Storage (via google-cloud-storage SDK)
- Firebase Auth (email/password, single-user only)
- Jinja2 templates + HTMX + Alpine.js (no React, no SPA, no separate frontend build)
- Tailwind CSS via CDN
- DAV libraries: `icalendar`, `vobject`
- Deployment target: Google App Engine Standard (Python 3 runtime)

## Architecture Rules
- SINGLE USER. One authorized email. No multi-tenancy, no registration, no roles.
- All user-facing text (labels, buttons, placeholders, errors, toasts, headings, empty states) MUST be in French.
- All code (variable names, function names, comments, docstrings) MUST be in English.
- Currency: stored as integer cents (e.g., 15000 = $150.00). NEVER use floats for money. Use Python `Decimal` for tax computations only, then convert to int cents.
- Timestamps: stored as UTC `datetime`. Displayed in `America/Montreal` timezone.
- Firestore document IDs: UUID v4, generated server-side.
- Every Firestore document includes `created_at`, `updated_at` (UTC datetime), and `etag` (UUID v4, regenerated on every write).
- HTMX for all dynamic interactions. Flask endpoints return HTML fragments for HTMX requests, full pages for normal requests.
- Mobile-first design: build for 375px viewport first, add breakpoints for tablet (768px) and desktop (1024px+).
- Minimalist UI: generous white-space, near-white backgrounds (#FAFAFA), near-black text (gray-900), indigo-600 accent. System font stack.

## Security Rules
- Every response includes security headers (CSP, HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, Cache-Control).
- CSRF protection on every POST/PUT/DELETE. HTMX requests include CSRF token via `hx-headers`.
- Rate limiting on auth endpoints (5/min).
- Firebase Storage URLs: always signed, 15-minute expiry. Never expose raw URLs.
- DAV endpoints: HTTP Basic Auth (bcrypt), separate credentials from Firebase Auth.
- Validate and sanitize all inputs server-side, even if client-side validation exists.

## Code Style
- Python type hints on all function signatures.
- Flask blueprints for route organization (one blueprint per module).
- Wrap all Firestore operations in try/except with user-friendly error handling.
- Consistent CRUD pattern across all modules: create, get, list, update, delete in model layer.
- DAV serialization functions (e.g., `client_to_vcard()`, `hearing_to_vevent()`) live in the model layer alongside CRUD.

## Project Structure
```
athena/
├── app.yaml
├── requirements.txt
├── main.py                  # Flask app factory + entrypoint
├── config.py                # Env-based configuration
├── auth.py                  # Firebase Auth verification + @login_required
├── security.py              # Security headers, CSRF, rate limiting
├── models/                  # Firestore data access layer
├── routes/                  # Flask blueprints (web UI)
├── dav/                     # CardDAV, CalDAV, RFC-5545 endpoints
├── templates/               # Jinja2 templates
│   ├── base.html
│   ├── components/          # Reusable HTMX partials
│   └── {module}/            # Per-module templates
└── static/
```

## Commands
- Run locally: `flask run --debug`
- Run with gunicorn: `gunicorn -b :8080 main:app`
- Deploy: `gcloud app deploy`
- Firestore emulator: `gcloud emulators firestore start`

## Current Phase
Phase 12 — Firebase App Check + Phone MFA

## Phase Checklist
- [*] Phase 1: Scaffolding, Auth, Security
- [*] Phase 2: Client Management + CardDAV Foundation
- [*] Phase 3: Dossier (Case File) Management
- [*] Phase 4: Time Tracking + Expense Management
- [*] Phase 5: Invoicing (GST/QST)
- [*] Phase 6: Hearing Dates + CalDAV Foundation
- [*] Phase 7: Task Management + VTODO Foundation
- [*] Phase 8: Case Protocols
- [*] Phase 9: Document Storage
- [*] Phase 10: DAV Protocol Layer (CardDAV, CalDAV, RFC-5545)
- [*] Phase 11: Dashboard, Polish, Deployment
- [ ] Phase 12: Firebase App Check + Phone MFA

## Known Gotchas
- Firestore does not support `!=` combined with `orderBy` on a different field. Design queries accordingly.
- App Engine Standard does not allow writing to the filesystem (except /tmp). All file storage goes through Firebase Storage.
- DavX5 is strict about DAV compliance. Partial implementation will cause silent sync failures. Test each DAV endpoint with `curl` before testing with DavX5.
- QST (9.975%) is applied on the taxable amount directly, NOT compounded on GST (changed in 2013).
- Canadian postal code format: A1A 1A1 (letter-digit-letter space digit-letter-digit).
