# PALLAS ATHENA — Legal Practice Management Application
## Master Specification — Phased Implementation Guide

---

## GLOBAL CONTEXT (Include with every phase)

You are building **Pallas Athena**, a single-user legal practice management web application for a Québec civil litigation lawyer. The application manages clients, case files (dossiers), billable hours, expenses, invoices, hearing dates, tasks, and case protocols. It also provides document storage for procedural files.

### Technology Stack
- **Backend:** Python 3.11+ with Flask
- **Database:** Google Cloud Firestore (native mode — Datastore mode is NOT used)
- **File Storage:** Firebase Storage (via Google Cloud Storage SDK)
- **Authentication:** Firebase Authentication (email/password, single-user only)
- **Hosting:** Google App Engine (Standard Environment, Python 3 runtime)
- **Frontend:** Flask/Jinja2 templates + HTMX for dynamic interactions + Alpine.js for lightweight client-side state
- **CSS:** Tailwind CSS via CDN (mobile-first, minimalist, generous white-space)
- **DAV Sync:** CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL) endpoints served directly from Flask for DavX5 synchronization
- **PDF/vCard/iCal Libraries:** `icalendar`, `vobject`, `weasyprint` (only if needed later)

### Core Design Principles
1. **Single-user system.** There is exactly one authorized user. Every endpoint — web UI and DAV — must verify this identity. No multi-tenancy, no user registration, no role system.
2. **Mobile-first, minimalist UI.** Generous white-space, clean typography, touch-friendly targets (min 44px). The interface should feel like a premium productivity tool, not enterprise software. Default viewport is a phone screen; desktop is a bonus layout.
3. **Security-hardened.** This application handles privileged legal data. Every response must include strict security headers (CSP, HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy). All inputs must be validated and sanitized. CSRF protection on every state-changing request. Rate limiting on authentication endpoints.
4. **DAV-first data model.** Clients, hearings, tasks, and dossiers are stored in Firestore but must be serializable to vCard (CardDAV), iCalendar VEVENT (CalDAV), and VTODO/VJOURNAL (RFC-5545) at all times. The Firestore schema must accommodate all standard DAV properties from the start.
5. **Bilingual readiness.** The UI is in French. All labels, buttons, placeholders, error messages, and headings are in French. Backend code, comments, variable names, and documentation remain in English.

### Project Structure
```
athena/
├── app.yaml                    # App Engine configuration
├── requirements.txt
├── main.py                     # Flask app factory and entrypoint
├── config.py                   # Configuration (env-based)
├── auth.py                     # Firebase Auth verification middleware
├── security.py                 # Security headers, CSP, rate limiting
├── models/                     # Firestore data access layer
│   ├── __init__.py
│   ├── client.py               # Client/contact CRUD
│   ├── dossier.py              # Case file/dossier CRUD
│   ├── timeentry.py            # Billable hours CRUD
│   ├── expense.py              # Expense CRUD
│   ├── invoice.py              # Invoice CRUD + line item computation
│   ├── hearing.py              # Hearing/court date CRUD
│   ├── task.py                 # Task CRUD
│   ├── protocol.py             # Case protocol CRUD
│   └── document.py             # Document metadata CRUD
├── routes/                     # Flask blueprints (web UI)
│   ├── __init__.py
│   ├── auth_routes.py          # Login/logout
│   ├── dashboard.py            # Home dashboard
│   ├── clients.py              # Client management views
│   ├── dossiers.py             # Dossier management views
│   ├── time_expenses.py        # Time + expense entry views
│   ├── invoices.py             # Invoice views
│   ├── calendar_routes.py      # Hearing calendar views
│   ├── tasks.py                # Task management views
│   ├── protocols.py            # Protocol management views
│   └── documents.py            # Document browser views
├── dav/                        # DAV protocol endpoints
│   ├── __init__.py
│   ├── carddav.py              # CardDAV server (clients)
│   ├── caldav.py               # CalDAV server (hearings)
│   ├── rfc5545.py              # VTODO + VJOURNAL endpoints (tasks, dossiers)
│   └── dav_auth.py             # HTTP Basic Auth for DAV clients
├── templates/                  # Jinja2 templates
│   ├── base.html               # Base layout with nav, meta, security
│   ├── components/             # Reusable HTMX partials
│   │   ├── modal.html
│   │   ├── toast.html
│   │   ├── empty_state.html
│   │   └── confirm_dialog.html
│   ├── auth/
│   │   └── login.html
│   ├── dashboard/
│   │   └── index.html
│   ├── clients/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   ├── dossiers/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   ├── time_expenses/
│   │   ├── list.html
│   │   └── form.html
│   ├── invoices/
│   │   ├── list.html
│   │   ├── detail.html
│   │   ├── form.html
│   │   └── print.html
│   ├── calendar/
│   │   ├── index.html
│   │   └── form.html
│   ├── tasks/
│   │   ├── list.html
│   │   └── form.html
│   ├── protocols/
│   │   ├── list.html
│   │   ├── detail.html
│   │   └── form.html
│   └── documents/
│       ├── browser.html
│       └── viewer.html
└── static/
    └── icons/                  # SVG icons (inline preferred)
```

### Firestore Collection Map
```
users/{userId}/
├── clients/{clientId}
├── dossiers/{dossierId}
├── timeentries/{entryId}
├── expenses/{expenseId}
├── invoices/{invoiceId}
│   └── lineitems/{itemId}      # Subcollection
├── hearings/{hearingId}
├── tasks/{taskId}
├── protocols/{protocolId}
│   └── steps/{stepId}          # Subcollection
├── documents/{documentId}      # Metadata only; file in Storage
└── dav_sync/{resourceType}     # Sync tokens and ctags
```

### Naming Conventions
- Firestore document IDs: UUID v4 (generated server-side)
- Timestamps: Always stored as UTC `datetime` objects; displayed in `America/Montreal` timezone
- Currency: Stored as integer cents (e.g., `15000` = $150.00) to avoid floating-point errors
- All Firestore documents include `created_at`, `updated_at` (UTC datetime), and `etag` (UUID v4, regenerated on every write — used for DAV sync)

---

## PHASE 1 — Project Scaffolding, Authentication, and Security Hardening

### Objective
Set up the Flask project skeleton, Firebase integration, single-user authentication with session management, and comprehensive security headers. After this phase, you should have a running Flask app that serves a login page, authenticates against Firebase Auth, establishes a secure session, and protects all routes behind authentication. Every response must include hardened security headers.

### Detailed Requirements

**1.1 — Flask App Factory (`main.py`)**
Create a Flask application factory pattern. The app must:
- Load configuration from `config.py` (which reads from environment variables)
- Initialize the Firebase Admin SDK using a service account JSON (path from env var `GOOGLE_APPLICATION_CREDENTIALS`) or Application Default Credentials when running on App Engine
- Initialize Firestore client as a global singleton accessible throughout the app
- Register all blueprints (start with `auth_routes` only; others come in later phases)
- Register the `security.py` `after_request` hook for security headers
- Set `SECRET_KEY` from environment variable (minimum 32 bytes, generated via `secrets.token_hex(32)`)
- Configure Flask sessions as server-side (use `flask-session` with Firestore backend, or signed cookies encrypted with the secret key — signed cookies are simpler for single-user)

**1.2 — Configuration (`config.py`)**
Use a Config class pattern with environment variable loading:
- `SECRET_KEY`: Required, no default
- `FIREBASE_PROJECT_ID`: Required
- `FIREBASE_STORAGE_BUCKET`: Required (format: `{project-id}.appspot.com`)
- `AUTHORIZED_USER_EMAIL`: The single authorized email address — hardcoded as an env var for maximum security. No other email can ever authenticate.
- `SESSION_LIFETIME_HOURS`: Default 12
- `DAV_USERNAME`: Username for DAV Basic Auth (separate from Firebase Auth)
- `DAV_PASSWORD_HASH`: Bcrypt hash of the DAV password (never store plaintext)
- `RATE_LIMIT_LOGIN`: Default "5 per minute"
- `ENV`: `development` or `production`

**1.3 — Authentication (`auth.py`)**
Implement Firebase Authentication verification:
- The login flow works as follows: the user enters email + password on the login page. A client-side Firebase Auth SDK call (`signInWithEmailAndPassword`) returns an ID token. This token is sent to the Flask backend via POST. The backend verifies the ID token using `firebase_admin.auth.verify_id_token()`, confirms the email matches `AUTHORIZED_USER_EMAIL`, and sets a session cookie.
- Create a `@login_required` decorator that checks the session on every request. If no valid session, redirect to login with a `next` URL parameter.
- Session must store: `user_id` (Firebase UID), `email`, `login_time`, `expires_at`.
- On each request, verify the session has not expired. If expired, clear session and redirect to login.
- Implement logout that clears the session and redirects to login.
- **Critical:** Never trust client-side state. Always verify server-side.

**1.4 — Security Middleware (`security.py`)**
Create an `after_request` handler that attaches the following headers to EVERY response:

```python
# Content Security Policy — strict, no inline scripts (HTMX is loaded from CDN)
Content-Security-Policy: default-src 'self'; script-src 'self' https://cdn.jsdelivr.net https://unpkg.com https://www.gstatic.com https://apis.google.com; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://*.googleapis.com; connect-src 'self' https://*.googleapis.com https://*.firebaseio.com https://identitytoolkit.googleapis.com; font-src 'self' https://cdn.jsdelivr.net; frame-src https://*.firebaseapp.com; base-uri 'self'; form-action 'self'; frame-ancestors 'none'

# Transport security
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload

# Prevent MIME sniffing
X-Content-Type-Options: nosniff

# Clickjacking protection (redundant with CSP frame-ancestors but defense-in-depth)
X-Frame-Options: DENY

# Control referrer information
Referrer-Policy: strict-origin-when-cross-origin

# Disable browser features not needed
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()

# Prevent caching of sensitive data
Cache-Control: no-store, no-cache, must-revalidate, private
Pragma: no-cache
```

Also implement:
- CSRF protection using `flask-wtf` or a custom double-submit cookie pattern. Every POST/PUT/DELETE request must include a valid CSRF token. HTMX requests must include the token via `hx-headers`.
- Rate limiting on `/auth/login` endpoint (5 attempts per minute per IP) using `flask-limiter` with Firestore or in-memory backend.
- Request size limiting (max 16MB for document uploads, 1MB for all other requests).
- Input sanitization utility function that strips HTML tags and validates string lengths.

**1.5 — Login Page Template (`templates/auth/login.html`)**
Create a minimal, centered login form:
- White background, vertically and horizontally centered card
- App name "Pallas Athena" in a clean sans-serif font (Inter or system font stack) at the top, with a subtle owl icon or shield motif (optional, keep minimal)
- Email and password fields with French labels ("Courriel", "Mot de passe")
- "Connexion" submit button, full-width, dark (near-black) background with white text
- Error messages displayed inline in red below the form
- Loading state: button shows a spinner and is disabled during authentication
- The Firebase Auth JS SDK is loaded and handles the `signInWithEmailAndPassword` call client-side, then POSTs the ID token to `/auth/verify-token`
- Must work flawlessly on mobile (viewport meta tag, responsive padding)

**1.6 — Base Template (`templates/base.html`)**
Create the base layout that all authenticated pages extend:
- Mobile-first responsive layout
- Bottom navigation bar (mobile) with 5 icons: Tableau de bord (dashboard), Dossiers, Temps (time tracking), Agenda (calendar/hearings), Plus (overflow menu for clients, invoices, tasks, protocols, documents)
- On desktop (≥768px), navigation becomes a left sidebar
- Viewport meta tag: `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`
- Tailwind CSS via CDN
- HTMX via CDN
- Alpine.js via CDN
- CSRF token injected into a meta tag and configured as a default HTMX header:
  ```html
  <meta name="csrf-token" content="{{ csrf_token() }}">
  <body hx-headers='{"X-CSRFToken": "{{ csrf_token() }}"}'>
  ```
- Toast notification container (for success/error feedback, animated via Alpine.js)
- Offline indicator (subtle banner when navigator.onLine is false)
- Color scheme: near-white backgrounds (`#FAFAFA` or `gray-50`), near-black text (`gray-900`), accent color for interactive elements: `indigo-600`. Minimal use of color — mostly grayscale with the accent for CTAs and active nav states.
- Typography: system font stack (`-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`), generous line-height (`1.6`), body text `16px` on mobile.

**1.7 — App Engine Configuration (`app.yaml`)**
```yaml
runtime: python311
instance_class: F2
automatic_scaling:
  min_instances: 0
  max_instances: 2
  target_cpu_utilization: 0.65
env_variables:
  ENV: "production"
  # Other env vars set via gcloud app deploy or Secret Manager
handlers:
  - url: /static
    static_dir: static
    secure: always
  - url: /.*
    script: auto
    secure: always
```

**1.8 — Dependencies (`requirements.txt`)**
```
Flask==3.1.*
firebase-admin==6.*
flask-wtf==1.*
flask-limiter==3.*
gunicorn==22.*
google-cloud-firestore==2.*
google-cloud-storage==2.*
icalendar==6.*
vobject==0.9.*
bcrypt==4.*
pytz==2024.*
```

### Testing Criteria for Phase 1
- [*] `flask run` starts the app without errors
- [*] Visiting any route redirects to `/auth/login`
- [*] Login with the correct Firebase Auth email succeeds and redirects to `/` (dashboard placeholder)
- [*] Login with an incorrect email (even if valid Firebase Auth) is rejected
- [*] After login, session persists across page reloads
- [*] Logout clears the session and redirects to login
- [*] All responses include the full set of security headers (verify with `curl -I`)
- [ ] Rate limiting blocks login after 5 rapid attempts
- [*] CSRF token is present and POST without it returns 400
- [*] Mobile viewport renders correctly (test at 375px width)

---

## PHASE 2 — Client Management + CardDAV Foundation

### Objective
Build the client and contact management module with full CRUD in the web UI, and lay the groundwork for CardDAV synchronization. This module manages all contacts — actual clients, opposing parties, opposing counsel, experts, witnesses, and other intervenants. Contacts are the foundational entity — dossiers, invoices, and time entries all reference a client. The schema uses vCard 4.0 (RFC 6350) to support language, gender, and extended properties required for CardDAV sync with DavX5.

### Firestore Schema: `clients/{clientId}`

```python
{
    "id": "uuid-v4",                     # Also the document ID
    "type": "individual" | "organization",
    
    # Contact role — distinguishes actual clients from other parties
    # Maps to vCard CATEGORIES property
    "contact_role": "client" | "partie_adverse" | "avocat_adverse" | "témoin" | "expert" | "huissier" | "notaire" | "autre",
    
    # For individuals
    "first_name": "Jean",
    "last_name": "Tremblay",
    "prefix": "Me" | "M." | "Mme" | "",  # Honorific
    
    # For organizations
    "organization_name": "ABC Inc.",
    "contact_person": "Marie Lavoie",     # Primary contact at org
    
    # Demographics (vCard 4.0 properties: LANG, GENDER, GRAMGENDER/X-PRONOUN)
    "language": "fr" | "en" | "es" | "",  # BCP 47 language tag — maps to vCard LANG
    "gender": "M" | "F" | "O" | "N" | "U" | "",  # M=male, F=female, O=other, N=none/not applicable, U=unknown — maps to vCard GENDER
    "pronouns": "il/lui" | "elle" | "iel" | "he/him" | "she/her" | "they/them" | "",  # Maps to vCard X-PRONOUN (no RFC standard yet)
    
    # Professional coordinates (vCard TITLE, ROLE, ORG)
    "job_title": "",                      # e.g., "Associé principal" — maps to vCard TITLE
    "job_role": "",                       # e.g., "Directeur des finances" — maps to vCard ROLE
    "organization": "",                   # Employer/firm name for individuals — maps to vCard ORG
                                          # (For type=organization, use organization_name instead)
    
    # Personal contact info
    "email": "jean@example.com",
    "phone_home": "",
    "phone_cell": "+15145551234",
    
    # Professional contact info
    "email_work": "",
    "phone_work": "",
    "fax": "",
    
    # Personal address
    "address_street": "1234 rue Sherbrooke Ouest",
    "address_unit": "App. 4",
    "address_city": "Montréal",
    "address_province": "QC",
    "address_postal_code": "H3A 1B2",
    "address_country": "CA",
    
    # Professional address
    "work_address_street": "",
    "work_address_unit": "",
    "work_address_city": "",
    "work_address_province": "",
    "work_address_postal_code": "",
    "work_address_country": "CA",
    
    # Legal identifiers
    "bar_number": "",                     # If lawyer (own or opposing counsel)
    "company_neq": "",                    # Numéro d'entreprise du Québec
    
    # KYC / Compliance (only relevant when contact_role == "client")
    "identity_verified": "non_vérifié" | "vérifié" | "exempté",
    "identity_verified_date": None | datetime,    # Date of last verification or exemption
    "identity_verified_notes": "",                # Method, exemption reason, etc.
    "kyc_document_ids": [],                       # List of document IDs (from Phase 9 document storage)
    "conflict_check": "non_vérifié" | "vérifié" | "conflit_détecté",
    "conflict_check_date": None | datetime,
    "conflict_check_notes": "",                   # Details of conflict check or detected conflict
    
    # Notes
    "notes": "",
    
    # Metadata
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4",                   # Regenerated on every write
    
    # DAV-specific
    "vcard_uid": "uuid-v4",              # Stable UID for vCard, set once at creation
    "dav_href": "/dav/addressbook/uuid-v4.vcf"  # CardDAV resource path
}
```

### Web UI Requirements

**Client List View (`/clients`)**
- Search bar at top (filters by name, email, phone — client-side with Alpine.js for speed, or HTMX for server-side search)
- Toggle between "Tous" (all), "Particuliers" (individuals), "Organisations" (organizations)
- Secondary filter by contact role: "Clients", "Avocats", "Autres intervenants" — useful for quickly finding opposing counsel or experts
- Each client shown as a card/row: name, contact_role badge (colored: client=indigo, avocat_adverse=amber, others=gray), phone, email. Tap to go to detail.
- Clients with `identity_verified == "non_vérifié"` show a small warning indicator (orange dot) on their card
- Floating action button (FAB) "+" bottom-right on mobile to add new client
- Empty state: friendly message "Aucun contact pour le moment. Ajoutez votre premier contact."

**Client Detail View (`/clients/<id>`)**
- Display all client fields in clean, readable layout
- Contact role badge prominently displayed next to name
- Section groupings:
  - Coordonnées personnelles (personal contact: email, phone_home, phone_cell, personal address)
  - Coordonnées professionnelles (work contact: email_work, phone_work, fax, job_title, job_role, organization, work address)
  - Profil (language, gender, pronouns — displayed subtly, not prominently)
  - Adresse (personal and professional addresses, side by side on desktop)
  - Identifiants (bar_number, company_neq)
  - Notes
  - **Conformité** (only shown when contact_role == "client"):
    - Vérification d'identité: status badge (Non vérifié = orange, Vérifié = green, Exempté = blue), date, notes
    - Conflit d'intérêts: status badge (Non vérifié = orange, Vérifié = green, Conflit détecté = red), date, notes
    - Documents KYC: list of linked documents (from Phase 9) with view/download links. Upload button to add new KYC documents directly from this section.
- Quick-action buttons: Appeler (tel: link), Courriel (mailto: link)
- List of associated dossiers (linked from Phase 3)
- Edit button (top-right), Delete button (with confirmation dialog)

**Client Form (`/clients/new`, `/clients/<id>/edit`)**
- Full-screen form on mobile, modal or side-panel on desktop
- **Section 1 — Type et rôle:**
  - Radio toggle: "Particulier" / "Organisation" — conditionally shows relevant fields
  - Contact role dropdown: "Client", "Partie adverse", "Avocat(e) adverse", "Témoin", "Expert(e)", "Huissier(ère)", "Notaire", "Autre"
- **Section 2 — Identité** (for individuals):
  - Prefix, first name, last name
  - Language dropdown (Français, English, Español, Autre)
  - Gender dropdown (Homme, Femme, Autre, Ne s'applique pas, Non précisé)
  - Pronouns dropdown (il/lui, elle, iel, he/him, she/her, they/them, Autre) — optional
- **Section 3 — Coordonnées personnelles:**
  - Email, phone_home, phone_cell
  - Personal address fields
- **Section 4 — Coordonnées professionnelles:**
  - Job title, role, organization (employer/firm)
  - Email (work), phone (work), fax
  - Work address fields
- **Section 5 — Identifiants:**
  - Bar number (shown when contact_role is avocat_adverse or client with "Me" prefix)
  - NEQ (shown when type is organization)
- **Section 6 — Conformité** (shown only when contact_role == "client"):
  - Vérification d'identité: status dropdown (Non vérifié / Vérifié / Exempté), date picker (auto-fills to today on status change), notes textarea
  - Conflit d'intérêts: status dropdown (Non vérifié / Vérifié / Conflit détecté), date picker, notes textarea
  - KYC documents: file upload area (links to document storage in Phase 9; before Phase 9, store as a placeholder list of document references)
- **Section 7 — Notes:**
  - General notes textarea
- All fields with French labels
- Postal code auto-formats to Canadian format (A1A 1A1) with input mask
- Phone fields auto-format to (514) 555-1234
- Validation: at minimum, a name is required. Email validated if provided. Postal code pattern validated. If contact_role is "client", show a non-blocking reminder if identity is not verified.
- Save via HTMX POST, returns to list with success toast

**Model Layer (`models/client.py`)**
- `create_client(data: dict) -> dict` — validates, generates UUID + etag + vcard_uid, writes to Firestore, returns document
- `get_client(client_id: str) -> dict | None`
- `list_clients(type_filter: str = None, role_filter: str = None, search: str = None) -> list[dict]`
- `update_client(client_id: str, data: dict) -> dict` — regenerates etag + updated_at. If identity_verified or conflict_check status changes, auto-set the corresponding date to now.
- `delete_client(client_id: str) -> bool` — verify no linked dossiers before deleting (or soft-delete)
- `update_kyc_status(client_id: str, field: str, status: str, notes: str) -> dict` — updates identity_verified or conflict_check with auto-dated timestamp
- `link_kyc_document(client_id: str, document_id: str) -> dict` — appends document_id to kyc_document_ids list
- `client_to_vcard(client: dict) -> str` — serialize client to vCard 4.0 string using `vobject`
- `vcard_to_client(vcard_str: str) -> dict` — parse a vCard string into a client dict (for CardDAV PUT)

**vCard Serialization (vCard 4.0 — RFC 6350)**
Use vCard version 4.0 (not 3.0) to support LANG, GENDER, and extended properties. Map Firestore fields to vCard properties:
```
VERSION → 4.0
FN → display name (computed: "prefix first_name last_name" or "organization_name")
N → last_name;first_name;;;prefix
ORG → organization (employer) for individuals, or organization_name for type=organization
TITLE → job_title
ROLE → job_role

EMAIL;TYPE=HOME → email
EMAIL;TYPE=WORK → email_work
TEL;TYPE=HOME → phone_home
TEL;TYPE=WORK → phone_work
TEL;TYPE=CELL → phone_cell
TEL;TYPE=FAX → fax

ADR;TYPE=HOME → ;;address_street;address_city;address_province;address_postal_code;address_country
ADR;TYPE=WORK → ;;work_address_street;work_address_city;work_address_province;work_address_postal_code;work_address_country

LANG → language (BCP 47 tag: "fr", "en", etc.)
GENDER → gender (single character: M, F, O, N, U)
X-PRONOUN → pronouns (non-standard; no RFC yet — use X- property for DavX5 compatibility)

CATEGORIES → contact_role (mapped to French label: "Client", "Partie adverse", "Avocat(e) adverse", etc.)
NOTE → notes
UID → vcard_uid
REV → updated_at (formatted as ISO datetime)
```

**DavX5 Note on vCard 4.0:** DavX5 fully supports vCard 4.0. The `vobject` library supports both 3.0 and 4.0; ensure you set `VERSION:4.0` in the serialized output. If `vobject` has limitations with 4.0 properties, fall back to manually appending LANG, GENDER, and X-PRONOUN lines to the serialized string before returning.

### Testing Criteria for Phase 2
- [*] Can create, view, edit, and delete clients (both individual and organization)
- [*] Contact role selection works and persists correctly
- [*] Contact role filter on list view works
- [*] Search filters work correctly
- [*] Type toggle filters correctly
- [ ] Phone and postal code formatting works
- [*] Language, gender, and pronoun fields save and display correctly
- [*] Professional coordinates (title, role, org) save and display correctly
- [*] Work address and work contact fields save and display correctly
- [*] Conformité section only appears when contact_role is "client"
- [*] Identity verification status change auto-sets the date
- [*] Conflict check status change auto-sets the date
- [ ] KYC document linking works (placeholder until Phase 9 document storage is built)
- [*] Unverified client indicator shows on list view
- [ ] `client_to_vcard()` produces valid vCard 4.0 output including LANG, GENDER, X-PRONOUN, TITLE, ROLE, ORG, CATEGORIES, and dual ADR/TEL/EMAIL entries
- [ ] `vcard_to_client()` correctly parses a vCard 4.0 string back into a client dict
- [ ] Form validation prevents submission of invalid data
- [*] Mobile layout is clean with proper spacing and touch targets

---

## PHASE 3 — Dossier (Case File) Management

### Objective
Build the dossier module. A dossier represents a legal matter/case. It is the central entity linking clients, time entries, expenses, invoices, hearings, tasks, protocols, and documents. Dossiers will also be serializable as RFC-5545 VJOURNALs for DavX5 sync.

### Firestore Schema: `dossiers/{dossierId}`

```python
{
    "id": "uuid-v4",
    "file_number": "2025-001",            # User-assigned, unique, auto-incrementing suggestion
    "title": "Tremblay c. Lavoie",        # Case title
    "client_id": "uuid-ref",              # Reference to client
    "client_name": "Jean Tremblay",       # Denormalized for display
    
    # Case classification
    "matter_type": "litige_civil" | "litige_commercial" | "recouvrement" | "injonction" | "familial" | "autre",
    "court": "Cour supérieure" | "Cour du Québec" | "Tribunal administratif" | "Cour d'appel" | "Cour des petites créances" | "autre",
    "district": "Montréal" | "Québec" | "Laval" | "Longueuil" | ... ,
    "court_file_number": "500-17-123456-789",  # Judicial file number
    
    # Parties
    "role": "demandeur" | "défendeur" | "intervenant" | "mis en cause" | "autre",
    "opposing_party": "Marie Lavoie",
    "opposing_counsel": "Me Pierre Gagnon",
    "opposing_counsel_firm": "Gagnon Avocats",
    "opposing_counsel_phone": "",
    "opposing_counsel_email": "",
    
    # Financial
    "hourly_rate": 25000,                  # In cents ($250.00/hr) — default rate for this dossier
    "flat_fee": None,                      # If flat fee arrangement, in cents
    "fee_type": "hourly" | "flat" | "contingency" | "mixed",
    "retainer_amount": 0,                  # Provision pour frais, in cents
    "retainer_balance": 0,                 # Current retainer balance, in cents
    
    # Status
    "status": "actif" | "en_attente" | "fermé" | "archivé",
    "opened_date": datetime,
    "closed_date": None,
    
    # Prescription/Limitation
    "prescription_date": None,             # Date de prescription — critical deadline
    "prescription_notes": "",
    
    # Notes
    "notes": "",
    "internal_notes": "",                  # Private notes never shown externally
    
    # Metadata
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4",
    
    # DAV-specific
    "vjournal_uid": "uuid-v4",
    "dav_href": "/dav/journals/uuid-v4.ics"
}
```

### Web UI Requirements

**Dossier List View (`/dossiers`)**
- Default view shows only `actif` dossiers, with filter tabs: "Actifs", "En attente", "Fermés", "Tous"
- Each dossier card shows: file_number, title, client_name, matter_type badge, status badge
- Prescription date warning: if `prescription_date` is within 60 days, show an orange warning badge; if within 30 days, show red
- Search by file number, title, client name, court file number
- Sort by: date opened (default, newest first), file number, client name
- FAB "+" to create new dossier

**Dossier Detail View (`/dossiers/<id>`)**
This is the "hub" page for a case. It should display:
- Header: file_number, title, status badge
- Client card (linked — tap to go to client)
- Opposing party and counsel info section
- Court and jurisdiction info section
- Financial summary: fee type, hourly rate or flat fee, retainer balance
- **Tabbed sub-sections** (HTMX-loaded):
  - "Aperçu" (overview — notes, prescription date, key dates)
  - "Temps & Dépenses" (time entries + expenses for this dossier — from Phase 4)
  - "Facturation" (invoices for this dossier — from Phase 5)
  - "Audiences" (hearings for this dossier — from Phase 6)
  - "Tâches" (tasks for this dossier — from Phase 7)
  - "Protocole" (case protocol — from Phase 8)
  - "Documents" (uploaded files — from Phase 9)
- Edit and delete (with confirmation) actions

**Dossier Form**
- Client selector: searchable dropdown (HTMX autocomplete from `/clients/search?q=`)
- File number: auto-suggest next sequential number based on year (e.g., `2025-042`), but user can override
- All fields with French labels
- Court file number field with placeholder showing format: `500-17-______-___`
- Matter type and court as dropdowns
- Fee type selection conditionally shows hourly_rate or flat_fee field
- Prescription date with date picker + optional notes field

### Model Layer (`models/dossier.py`)
Standard CRUD + `dossier_to_vjournal()` and `vjournal_to_dossier()` for RFC-5545 sync.

### Testing Criteria for Phase 3
- [ ] Full CRUD for dossiers
- [*] Client selector autocomplete works
- [*] File number auto-suggestion works
- [*] Status filtering works
- [*] Prescription date warnings display correctly
- [*] Dossier detail "hub" page renders all sections
- [ ] Tabs load content via HTMX without full page reload
- [*] Mobile layout is clean and all sections are accessible

---

## PHASE 4 — Time Tracking and Expense Management

### Objective
Build the billable hours and expense modules. Time entries and expenses are always associated with a dossier. This phase also builds the unified time/expense list view that appears both as a standalone page and within a dossier's detail view.

### Firestore Schema: `timeentries/{entryId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",    # Denormalized
    "dossier_title": "Tremblay c. Lavoie", # Denormalized
    "date": datetime,                      # Date of work (date only, stored as midnight UTC)
    "description": "Rédaction de la requête introductive d'instance",
    "hours": 2.5,                          # Decimal hours (supports 0.1 increments)
    "rate": 25000,                         # Rate in cents — defaults from dossier but can be overridden
    "amount": 62500,                       # Computed: hours * rate, in cents
    "billable": True,                      # Toggle for non-billable work tracking
    "invoiced": False,                     # Set to True when included in an invoice
    "invoice_id": None,                    # Reference to invoice if invoiced
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4"
}
```

### Firestore Schema: `expenses/{expenseId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",
    "dossier_title": "Tremblay c. Lavoie",
    "date": datetime,
    "description": "Frais de signification — huissier Côté",
    "category": "signification" | "expertise" | "transcription" | "deplacement" | "photocopie" | "timbre_judiciaire" | "autre",
    "amount": 15000,                       # In cents ($150.00)
    "taxable": True,                       # Whether taxes apply to this expense
    "receipt_document_id": None,           # Optional link to an uploaded receipt (Phase 9)
    "invoiced": False,
    "invoice_id": None,
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4"
}
```

### Web UI Requirements

**Standalone Time & Expense View (`/temps`)**
- Two tabs at top: "Heures" and "Dépenses"
- Date-based grouping: entries grouped by date, most recent first
- Each time entry row: date, dossier file number, description (truncated), hours, amount
- Each expense row: date, dossier file number, description, category badge, amount
- Quick filter: date range picker (this week / this month / custom)
- Filter by dossier (searchable dropdown)
- Toggle: "Tout" / "Facturable seulement" / "Non facturé seulement"
- Running totals at the bottom: total hours, total amount
- FAB "+" opens a form with pre-selected type (hour or expense)

**Time Entry Form**
- Dossier selector (required) — searchable, shows file_number + title
- Date picker (defaults to today)
- Description: multi-line text area with common descriptions as quick-select chips (e.g., "Appel téléphonique", "Correspondance", "Rédaction", "Recherche juridique", "Audience", "Révision")
- Hours: number input with 0.1 step, or a duration picker (e.g., 1h30 = 1.5)
- Rate: pre-filled from dossier's hourly_rate, editable
- Billable toggle (default on)
- Save via HTMX, return to list with toast

**Expense Form**
- Similar structure to time entry
- Category dropdown
- Amount in dollars (converted to cents on save)
- Taxable toggle
- Optional receipt upload (file picker — stored in Phase 9)

**Within Dossier Detail View**
The "Temps & Dépenses" tab on the dossier detail page shows a filtered version of the same list (filtered to that dossier only), with summary stats at the top: total hours, total billable amount, total expenses, grand total.

### Model Layer
Standard CRUD for both `timeentry.py` and `expense.py`, plus:
- `get_unbilled_entries(dossier_id)` — returns time entries and expenses not yet invoiced
- `mark_as_invoiced(entry_ids, invoice_id)` — batch update
- `get_summary(dossier_id)` — returns totals

### Testing Criteria for Phase 4
- [*] Can create, edit, delete time entries and expenses
- [*] Amount auto-computes from hours × rate for time entries
- [*] Dossier selector correctly links entries
- [*] Date grouping displays correctly
- [ ] Filters (date range, dossier, billable status) work
- [*] Running totals are accurate
- [*] Quick-select description chips work
- [ ] Dossier detail tab shows filtered entries with summary
- [ ] Mobile layout handles long descriptions gracefully (truncation)

---

## PHASE 5 — Invoicing

### Objective
Build the invoicing module. Invoices pull from unbilled time entries and expenses for a given dossier, compute Québec GST (5%) and QST (9.975%), and provide a print-friendly view for export. Invoices are on-screen with a print/export function — no PDF generation library needed.

### Firestore Schema: `invoices/{invoiceId}`

```python
{
    "id": "uuid-v4",
    "invoice_number": "2025-F001",         # Auto-generated sequential, prefix F for facture
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",
    "dossier_title": "Tremblay c. Lavoie",
    "client_id": "uuid-ref",
    "client_name": "Jean Tremblay",
    
    # Billing address (snapshot from client at invoice creation)
    "billing_address": {
        "name": "Jean Tremblay",
        "street": "1234 rue Sherbrooke Ouest",
        "unit": "Bureau 200",
        "city": "Montréal",
        "province": "QC",
        "postal_code": "H3A 1B2"
    },
    
    "date": datetime,                      # Invoice date
    "due_date": datetime,                  # Default: 30 days from date
    "status": "brouillon" | "envoyée" | "payée" | "en_retard" | "annulée",
    
    # Financials (all in cents)
    "subtotal_fees": 0,                    # Sum of time entry amounts
    "subtotal_expenses": 0,                # Sum of expense amounts
    "subtotal": 0,                         # fees + expenses
    "gst_rate": 500,                       # 5.00% stored as basis points
    "gst_amount": 0,                       # Computed
    "qst_rate": 9975,                      # 9.975% stored as basis points (×10 for precision)
    "qst_amount": 0,                       # Computed
    "total": 0,                            # subtotal + gst + qst
    "retainer_applied": 0,                 # Amount applied from retainer
    "amount_due": 0,                       # total - retainer_applied
    
    # Tax numbers (your firm's — set from config)
    "gst_number": "123456789 RT0001",
    "qst_number": "1234567890 TQ0001",
    
    "notes": "",                           # Terms, notes printed on invoice
    "payment_terms": "Payable dans les 30 jours suivant la date de facturation.",
    
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4"
}
```

### Firestore Schema: `invoices/{invoiceId}/lineitems/{itemId}` (subcollection)

```python
{
    "id": "uuid-v4",
    "type": "fee" | "expense",
    "source_id": "uuid-ref",              # timeentry or expense ID
    "date": datetime,
    "description": "Rédaction de la requête introductive d'instance",
    "hours": 2.5,                          # Only for fees
    "rate": 25000,                         # Only for fees, in cents
    "amount": 62500,                       # In cents
    "taxable": True
}
```

### Web UI Requirements

**Invoice List View (`/factures`)**
- Shows all invoices, most recent first
- Each row: invoice_number, client_name, dossier file_number, date, total (formatted), status badge
- Status badges: "Brouillon" (gray), "Envoyée" (blue), "Payée" (green), "En retard" (red), "Annulée" (strikethrough gray)
- Filters: status, date range, dossier
- FAB "+" to create new invoice

**Invoice Creation Flow**
1. Select dossier (required) — searchable dropdown
2. System fetches all unbilled time entries and expenses for that dossier
3. Display them as a checklist — user can select/deselect individual items
4. Show running subtotal, tax computation, and total as items are selected
5. Invoice date defaults to today, due date to +30 days
6. User can edit notes and payment terms
7. Save creates the invoice in `brouillon` status and marks selected entries as invoiced

**Invoice Detail View (`/factures/<id>`)**
- Professional invoice layout
- Header: your firm name/info (from config), invoice number, date, due date
- Client billing address block
- Dossier reference (file number, court file number, case title)
- Line items table: date, description, hours, rate, amount — grouped by fees then expenses
- Subtotals section: fees subtotal, expenses subtotal, subtotal, TPS/GST, TVQ/QST, total, retainer applied, amount due
- Status badge + action buttons: "Marquer comme envoyée", "Marquer comme payée", "Annuler"
- Notes and payment terms at the bottom
- **Print button**: triggers `window.print()`. The print.html template (or `@media print` CSS) should hide the nav, header, and action buttons, and render a clean, professional invoice suitable for sending to clients. This IS the print/export function — the user can "Print to PDF" from their browser.

**Print Stylesheet**
- `@media print` rules that hide navigation, buttons, and non-essential UI
- Clean black-and-white layout suitable for professional correspondence
- Your firm's name, address, phone, email, tax registration numbers in the header
- Page break rules for long invoices

### Tax Computation Logic
```python
# GST (TPS): 5% on taxable amounts
gst = taxable_subtotal * 0.05

# QST (TVQ): 9.975% on taxable amounts (NOT compounding on GST since 2013)
qst = taxable_subtotal * 0.09975

# Round to nearest cent
# Use Python's Decimal for all calculations to avoid floating-point issues
```

### Model Layer (`models/invoice.py`)
- `create_invoice(dossier_id, selected_entry_ids, selected_expense_ids, data)` — creates invoice + line items, marks sources as invoiced
- `compute_totals(invoice_id)` — recalculates all amounts
- `update_status(invoice_id, new_status)` — with appropriate validations
- `get_invoice_with_items(invoice_id)` — returns invoice + all line items
- `void_invoice(invoice_id)` — sets status to `annulée`, un-marks time entries and expenses as invoiced

### Testing Criteria for Phase 5
- [ ] Invoice creation flow correctly pulls unbilled entries
- [ ] Selecting/deselecting items updates totals in real-time
- [ ] GST and QST compute correctly (verify with known amounts)
- [ ] Invoice detail view renders professionally
- [ ] Print view produces clean output suitable for clients
- [ ] Status transitions work (brouillon → envoyée → payée)
- [ ] Voiding an invoice releases the time entries and expenses
- [ ] Invoice numbers auto-increment correctly
- [ ] Currency displays as "$1,234.56" (Canadian format)

---

## PHASE 6 — Hearing Dates and Calendar + CalDAV Foundation

### Objective
Build the hearing/court date module with a calendar view and lay the groundwork for CalDAV synchronization. Hearings are always associated with a dossier and need to serialize as VEVENT components for DavX5 sync.

### Firestore Schema: `hearings/{hearingId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",
    "dossier_title": "Tremblay c. Lavoie",
    
    "title": "Audience sur requête en irrecevabilité",
    "hearing_type": "audience" | "conférence_de_gestion" | "conférence_de_règlement" | "interrogatoire" | "médiation" | "procès" | "appel" | "autre",
    
    "start_datetime": datetime,            # Full datetime in UTC
    "end_datetime": datetime,              # Full datetime in UTC (default: start + 1 hour)
    "all_day": False,
    
    "location": "Palais de justice de Montréal, salle 2.14",
    "court": "Cour supérieure",
    "judge": "L'honorable Pierre Lefebvre",
    
    "notes": "",
    "reminder_minutes": 1440,              # Default: 24 hours before (1440 min)
    
    # Status
    "status": "confirmée" | "à_confirmer" | "reportée" | "annulée" | "terminée",
    
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4",
    
    # DAV-specific
    "vevent_uid": "uuid-v4",
    "dav_href": "/dav/calendar/uuid-v4.ics"
}
```

### Web UI Requirements

**Calendar View (`/agenda`)**
- Default view: upcoming hearings list (next 30 days), chronologically
- Toggle to monthly calendar grid view (simple — just dots/indicators on days with hearings; tap a day to see its hearings)
- Each hearing card: date + time, title, dossier file_number, hearing_type badge, location, status badge
- Past hearings grayed out
- Color-coded by hearing_type
- FAB "+" to add new hearing

**Hearing Form**
- Dossier selector (required)
- Title (with suggested prefills based on hearing_type selection)
- Hearing type dropdown
- Date and time pickers (separate for start date, start time, end time)
- All-day toggle
- Location (with common courthouses as quick-select options for Montréal, Québec, Laval, Longueuil)
- Court, Judge fields
- Reminder: dropdown (15 min, 30 min, 1h, 2h, 24h, 48h, 1 semaine)
- Status dropdown
- Notes

**VEVENT Serialization**
Map Firestore fields to iCalendar VEVENT:
```
BEGIN:VEVENT
UID:{vevent_uid}
DTSTART:{start_datetime in UTC format}
DTEND:{end_datetime in UTC format}
SUMMARY:{title}
LOCATION:{location}
DESCRIPTION:{notes}\n\nDossier: {dossier_file_number} - {dossier_title}\nType: {hearing_type}\nCour: {court}\nJuge: {judge}
STATUS:{CONFIRMED|TENTATIVE|CANCELLED}
CATEGORIES:{hearing_type}
VALARM: TRIGGER:-PT{reminder_minutes}M
END:VEVENT
```

### Model Layer (`models/hearing.py`)
Standard CRUD + `hearing_to_vevent()` and `vevent_to_hearing()`.

### Testing Criteria for Phase 6
- [ ] Full CRUD for hearings
- [ ] Calendar list view shows upcoming hearings correctly
- [ ] Monthly grid view shows indicators and is tappable
- [ ] Hearing type filter works
- [ ] Quick-select courthouses populate location correctly
- [ ] `hearing_to_vevent()` produces valid iCalendar output (validate with `icalendar` parser)
- [ ] Dossier detail "Audiences" tab shows linked hearings
- [ ] Timezone handling is correct (stored UTC, displayed Eastern)

---

## PHASE 7 — Task Management + RFC-5545 VTODO Foundation

### Objective
Build the task management module. Tasks can be standalone or associated with a dossier. Tasks will serialize as VTODOs for DavX5 sync.

### Firestore Schema: `tasks/{taskId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": None | "uuid-ref",      # Optional dossier link
    "dossier_file_number": "",
    "dossier_title": "",
    
    "title": "Déposer la déclaration sous serment",
    "description": "",
    "priority": "haute" | "normale" | "basse",
    "status": "à_faire" | "en_cours" | "terminée" | "annulée",
    
    "due_date": None | datetime,           # Date d'échéance
    "completed_date": None | datetime,
    
    "category": "rédaction" | "recherche" | "correspondance" | "dépôt" | "signification" | "suivi" | "admin" | "autre",
    
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4",
    
    # DAV-specific
    "vtodo_uid": "uuid-v4",
    "dav_href": "/dav/tasks/uuid-v4.ics"
}
```

### Web UI Requirements

**Task List View (`/taches`)**
- Grouped by status: "À faire" and "En cours" shown first, "Terminées" collapsed at bottom
- Within each group, sorted by due_date (soonest first), then priority
- Each task row: checkbox (tap to mark complete), title, dossier file_number (if linked), due_date, priority indicator (colored dot: red=haute, orange=normale, gray=basse)
- Overdue tasks: due_date highlighted in red
- Filter by: dossier, priority, category
- FAB "+" to create new task
- Swipe right to complete (mobile gesture, with HTMX)

**Task Form**
- Title (required)
- Description (optional, multi-line)
- Dossier selector (optional)
- Priority dropdown
- Category dropdown
- Due date picker (optional)
- Status dropdown

**VTODO Serialization**
```
BEGIN:VTODO
UID:{vtodo_uid}
SUMMARY:{title}
DESCRIPTION:{description}\n\nDossier: {dossier_file_number} - {dossier_title}
PRIORITY:{1=haute, 5=normale, 9=basse}
STATUS:{NEEDS-ACTION|IN-PROCESS|COMPLETED|CANCELLED}
DUE:{due_date}
COMPLETED:{completed_date}
CATEGORIES:{category}
END:VTODO
```

### Testing Criteria for Phase 7
- [ ] Full CRUD for tasks
- [ ] Checkbox completion updates status via HTMX
- [ ] Grouping and sorting work correctly
- [ ] Overdue highlighting works
- [ ] `task_to_vtodo()` produces valid output
- [ ] Dossier detail "Tâches" tab shows linked tasks
- [ ] Mobile swipe-to-complete works

---

## PHASE 8 — Case Protocols

### Objective
Build the case protocol module. A protocol (protocole de l'instance) is the procedural roadmap for a case, defining the steps and deadlines that govern how the matter progresses to trial or resolution. In Québec civil litigation, there are three fundamentally different types of protocols, each with distinct behavior in the application:

1. **Cour du Québec — Procédure simplifiée** (Small claims excluded; this covers contested matters under the CQ's jurisdiction). This protocol is **mandated by law** and follows a **strict, fixed timeline** set out in the *Code de procédure civile*. The steps and their deadlines are prescribed — they cannot be freely modified by the parties. The application should auto-generate all steps with fixed offsets from the start date, and the user should not be able to delete or reorder mandatory steps (though they can add supplementary notes or custom sub-steps).

2. **Cour supérieure — Procédure ordinaire**. This protocol is **more flexible**. The *C.p.c.* requires the parties to file a case protocol (*protocole de l'instance*), but the specific deadlines within it are **proposed by the parties and approved by the court**. The protocol provides a framework of common procedural milestones (communication of exhibits, examinations, expert reports, inscription for trial, etc.), but the user sets the actual deadline dates for each. The application should present the standard milestones as a suggested template, but every deadline is **user-editable** from the start.

3. **Conventionnel — Protocole personnalisé**. This covers cases in appeal courts, administrative tribunals, arbitration, mediation frameworks, or any situation where the parties establish a **fully custom timeline** by agreement. There is no prescribed template — the user creates all steps and deadlines from scratch. The application presents a blank protocol with an "add step" interface.

### Firestore Schema: `protocols/{protocolId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",
    "dossier_title": "Tremblay c. Lavoie",
    
    "title": "Protocole de l'instance",    # Auto-generated from type, user can override
    "protocol_type": "cq_simplifié" | "cs_ordinaire" | "conventionnel",
    
    # Start date: the date from which all deadlines are computed
    # For CQ: typically date of service of the originating application
    # For CS: typically date the protocol is filed or approved
    # For Conventionnel: user-defined reference date
    "start_date": datetime,
    "end_date": datetime,                  # Final deadline (computed for CQ, user-set for CS/Conv.)
    
    # Applicable court (denormalized from dossier for display)
    "court": "Cour du Québec" | "Cour supérieure" | "Cour d'appel" | "Tribunal administratif" | "Arbitrage" | "autre",
    
    "notes": "",
    "status": "actif" | "complété" | "suspendu",
    
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4"
}
```

### Firestore Schema: `protocols/{protocolId}/steps/{stepId}` (subcollection)

```python
{
    "id": "uuid-v4",
    "order": 1,                            # Sequential display order
    "title": "Communication des pièces",
    "description": "Conformément à l'article 246 C.p.c.",
    "cpc_reference": "art. 246 C.p.c.",   # Optional: Code de procédure civile reference
    
    "deadline_date": datetime,             # The actual deadline date
    "deadline_offset_days": 60,            # Days from protocol start_date (used for template computation; null for conventionnel)
    
    # Whether this step is mandatory (prescribed by law) or supplementary (added by user)
    "mandatory": True,                     # True for CQ prescribed steps; False for user-added steps
    # Whether the deadline can be edited by the user
    "deadline_locked": False,              # True for CQ mandatory steps; False for CS and Conventionnel
    
    "status": "à_venir" | "en_cours" | "complété" | "en_retard",
    "completed_date": None | datetime,
    "linked_task_id": None,                # Optional: auto-create a task for this step
    "linked_hearing_id": None,             # Optional: link to a hearing (e.g., conférence de gestion)
    "notes": "",
    
    "created_at": datetime,
    "updated_at": datetime
}
```

### Protocol Templates

Each protocol type has a different template behavior:

#### Template A — Cour du Québec, Procédure simplifiée

This template is **prescriptive**. Steps are auto-generated with fixed offsets from `start_date`. The `mandatory` flag is `True` and `deadline_locked` is `True` for all prescribed steps. The user may add supplementary steps (with `mandatory: False` and `deadline_locked: False`) but cannot delete, reorder, or change deadlines of the mandatory steps.

**Steps (approximate — verify against current C.p.c. provisions):**

| # | Step | Offset | C.p.c. Ref. |
|---|------|--------|-------------|
| 1 | Signification de l'avis d'assignation | Jour 0 | art. 145 — reference date |
| 2 | Avis de la partie demanderesse | Jour 20 | art. 535.4 |
| 3 | Dénonciation des moyens préliminaires | Jour 45 | art. 535.5 |
| 4 | Avis de la partie défenderesse | Jour 95 | art. 535.6 |
| 5 | Conférence de gestion | Jour 110 | art. 535.8 |
| 6 | Conférence de règlement à l'amiable | Jour 130 à 160 | art. 535.12 |
| 7 | Inscription pour instruction et jugement | Jour 180 | art. 535.13 |

**Important:** These offsets are illustrative. The exact delays and articles should be verified against the current *Code de procédure civile* provisions for the Cour du Québec simplified procedure. The user should be able to adjust these templates in a future settings/admin page, but at launch, the above is a reasonable default. A note should be displayed in the UI: *"Les délais sont fournis à titre indicatif. Vérifiez les dispositions applicables du Code de procédure civile."*

#### Template B — Cour supérieure, Procédure ordinaire

This template is **suggestive**. It pre-populates the standard milestones that a typical Superior Court case protocol would include, but all deadlines are **editable** by the user. `mandatory` is `True` (these are standard milestones, not optional), but `deadline_locked` is `False`. The user sets the actual dates based on what the parties agreed or what the court ordered.

**Steps (standard milestones — all dates user-editable):**

| # | Step | Default Offset (suggestion only) | Notes |
|---|------|----------------------------------|-------|
| 1 | Signification de l'avis d'assignation | Jour 0 | art. 145(1) C.p.c. — reference date |
| 2 | Réponse | Jour 15 | art. 145(2) C.p.c. |
| 3 | Premier protocole de l'instance | Jour 45 | art. 149(2) C.p.c. |
| 4 | Interrogatoires préalables | Jour 120 | User sets actual date |
| 5 | Expertises (rapports d'experts) | Jour 150 | User sets actual date |
| 6 | Conférence de règlement à l'amiable | Jour 180 | User sets actual date |
| 7 | Conférence de gestion | Jour 180 | User sets actual date |
| 8 | Inscription pour instruction et jugement | Jour 180 | art. 173(1) C.p.c. |

When this template is selected, the "Default Offset" column pre-fills the deadline dates as suggestions. The user is expected to replace these with the actual agreed-upon or court-ordered dates. A note should be displayed: *"Modifiez les dates selon le protocole convenu entre les parties ou ordonné par le tribunal."*

The user can freely add, remove, or reorder steps.

#### Template C — Conventionnel (personnalisé)

No template. The protocol starts with **zero steps**. The user adds each step manually with a title, description, and deadline. This is used for:
- Cour d'appel (mémoires, cahiers de sources, audition)
- Tribunaux administratifs (various procedures)
- Arbitrage (agreed timeline between parties and arbitrator)
- Médiation (session dates, document exchange)
- Any other non-standard procedural context

All steps have `mandatory: False` and `deadline_locked: False`.

### Web UI Requirements

**Protocol List is accessed through the dossier detail "Protocole" tab** (not a standalone page). One dossier may have at most one active protocol.

**Protocol View (within dossier detail)**
- Header showing: protocol type badge (CQ Simplifié = blue, CS Ordinaire = indigo, Conventionnel = gray), title, status, start date → end date range
- Disclaimer text for CQ and CS templates (as described above)
- Visual timeline: vertical step list with connecting lines
- Each step card:
  - Title + C.p.c. reference (if any) in subtle gray
  - Deadline date prominently displayed
  - Status badge: "À venir" (gray), "En cours" (blue), "Complété" (green), "En retard" (red with pulsing dot)
  - Days remaining (or days overdue in red)
  - Lock icon on CQ mandatory steps to indicate the deadline is prescribed by law
  - "Compléter" button (marks as completed, records date)
  - Linked task indicator (if a task was created for this step) — tap to navigate to the task
  - Linked hearing indicator (if linked) — tap to navigate to the hearing
- Progress bar at top showing completed steps / total steps
- Warning indicators for upcoming deadlines (within 7 days) and overdue steps

**Protocol Creation**
- Step 1: Select protocol type via three clearly described cards:
  - **Cour du Québec — Simplifié**: "Protocole prescrit par le C.p.c. avec délais fixes. Les étapes obligatoires ne peuvent pas être modifiées."
  - **Cour supérieure — Ordinaire**: "Protocole standard avec jalons habituels. Les dates sont à adapter selon l'entente des parties ou l'ordonnance du tribunal."
  - **Conventionnel**: "Protocole entièrement personnalisé. Pour les appels, l'arbitrage, les tribunaux administratifs, ou toute procédure non standard."
- Step 2: Set start date (with explanation of what start date means for each type)
- Step 3: Review auto-populated steps (CQ and CS) or start adding steps (Conventionnel)
  - For CQ: steps are shown as read-only with computed deadlines. User can add supplementary steps but cannot modify or delete mandatory steps.
  - For CS: steps are shown as editable. Default offset dates are pre-filled but highlighted with an "À modifier" badge until the user explicitly sets them.
  - For Conventionnel: empty state with "Ajouter une étape" button.
- Step 4: Optionally toggle "Créer les tâches automatiquement" (auto-creates a linked task for each step)
- Save creates the protocol and all steps in a batch write.

**Editing Protocols**
- CQ: Cannot delete mandatory steps. Can edit notes on any step. Can add supplementary steps. Cannot change mandatory deadlines. If the start date changes, all mandatory deadlines auto-recompute.
- CS: Full editing of all steps — change dates, add, remove, reorder. If the user changes the start date, offer to recompute suggested dates (with confirmation, since the user may have already set custom dates).
- Conventionnel: Full editing — add, remove, reorder, change everything.

**Automatic Task Creation**
When a protocol step is created and the "auto-create tasks" toggle is on, automatically create a linked task (Phase 7) with the same title and deadline. Bidirectional sync: when the task is completed, the protocol step is also marked complete (and vice versa). When a step has a `linked_hearing_id`, completing the hearing also marks the step complete.

### Model Layer (`models/protocol.py`)
- `create_protocol(dossier_id, protocol_type, start_date, data)` — creates protocol + auto-generates steps from template (CQ/CS) or empty (Conventionnel)
- `get_protocol(protocol_id)` — returns protocol with all steps
- `get_protocol_for_dossier(dossier_id)` — returns the active protocol for a dossier (max one active)
- `update_protocol(protocol_id, data)` — updates protocol metadata
- `add_step(protocol_id, step_data)` — adds a custom step
- `update_step(protocol_id, step_id, data)` — updates a step (validates: cannot change deadline on locked steps)
- `delete_step(protocol_id, step_id)` — deletes a step (validates: cannot delete mandatory steps)
- `complete_step(protocol_id, step_id)` — marks step complete, sets completed_date, syncs linked task/hearing
- `recompute_deadlines(protocol_id, new_start_date)` — recalculates all offset-based deadlines from a new start date (only for steps with deadline_offset_days set)
- `check_overdue_steps(protocol_id)` — scans steps and updates status to "en_retard" where deadline_date < today and status is not "complété"
- `get_template(protocol_type)` — returns the step template for a given protocol type

### Testing Criteria for Phase 8
- [ ] CQ protocol auto-generates all mandatory steps with correct offsets
- [ ] CQ mandatory steps cannot be deleted or have their deadlines changed
- [ ] CQ supplementary steps can be added freely
- [ ] CQ start date change recomputes all mandatory deadlines
- [ ] CS protocol auto-generates suggested milestones with editable deadlines
- [ ] CS "À modifier" badge appears on steps with default (unconfirmed) dates
- [ ] CS steps can be freely added, removed, reordered, and re-dated
- [ ] Conventionnel protocol starts empty and allows full manual step creation
- [ ] Deadline computation from start_date + offset is correct for all types
- [ ] Step status updates work (including automatic overdue detection)
- [ ] Lock icon appears on CQ mandatory steps
- [ ] Disclaimer text displays correctly for CQ and CS types
- [ ] Timeline visualization renders correctly on mobile
- [ ] Progress tracking is accurate
- [ ] Linked task creation and bidirectional status sync works
- [ ] Linked hearing completion syncs to protocol step
- [ ] Protocol type selection cards clearly explain each option

---

## PHASE 9 — Document Storage

### Objective
Build the document storage module — a file management system for procedural documents with hierarchical folder organization. Uses Firebase Storage (Google Cloud Storage) for file storage and Firestore for metadata. Folders are a Firestore-only concept — files remain at flat Storage paths regardless of folder placement. The UI should feel like a lightweight file browser (similar to Google Drive but much simpler).

### Firestore Schema: `dossiers/{dossierId}/folders/{folderId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "name": "Pièces du demandeur",           # Folder display name (max 100 chars, no / or \)
    "parent_folder_id": None | "uuid-ref",   # None = root of dossier, uuid = nested inside another folder
    "order": 0,                               # Display order among siblings
    "created_at": datetime,
    "updated_at": datetime
}
```

**Folder constraints:** Max nesting depth of 5 levels. No duplicate names within the same parent (case-insensitive). Circular reference prevention on move operations.

### Firestore Schema: `documents/{documentId}`

```python
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",
    "folder_id": None | "uuid-ref",        # None = dossier root level, uuid = inside a folder
    
    "filename": "requête_introductive.pdf",
    "original_filename": "requête_introductive.pdf",
    "display_name": "Requête introductive d'instance",  # User-friendly name
    "file_type": "application/pdf",
    "file_size": 245678,                   # Bytes
    "storage_path": "users/{userId}/dossiers/{dossierId}/documents/{documentId}/{filename}",
    
    "category": "procédure" | "pièce" | "correspondance" | "preuve" | "jugement" | "entente" | "note" | "autre",
    "description": "",
    "tags": ["requête", "introductive"],
    
    # Versioning (simple)
    "version": 1,
    "parent_document_id": None,            # If this is a version of another document
    
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4"
}
```

### Firebase Storage Structure
```
users/{userId}/
└── dossiers/{dossierId}/
    └── documents/{documentId}/
        └── {filename}
```

### Web UI Requirements

**Document Browser (within dossier detail "Documents" tab)**
- **Breadcrumb bar** at top: "Documents > Pièces > Expert Tremblay" — each segment is clickable (HTMX navigation). Root shows "Documents".
- **Toolbar:** "Nouveau dossier" button, "Téléverser" button, bulk actions (Déplacer, Supprimer) when items are selected via checkboxes
- **Folders displayed first** as a group, before documents:
  - Each folder row: folder icon, name, item count, date modified. Tap to navigate into folder.
- **Document rows** below folders: icon (by type: PDF, Word, image, other), display_name, category badge, file_size (formatted), upload date
- Category filter tabs
- Search by name/description — when searching, searches across ALL folders and shows folder path in results
- Sort by date (default), name, size
- Upload button: opens file picker (accept common legal file types: PDF, DOCX, DOC, JPG, PNG, TIFF). Files uploaded while inside a folder are automatically assigned to that folder.
- Multi-file upload support with progress indicator
- Drag-and-drop upload area on desktop
- **Create folder inline:** clicking "Nouveau dossier" inserts an editable text input row at the top of the folder list. Enter to save, Escape to cancel.
- **Move-to-folder modal:** when selecting items and clicking "Déplacer", a modal shows the full folder tree with a "Déplacer ici" button. Supports moving both documents and folders.
- Empty folder state: "Ce dossier est vide."
- Empty root state: "Aucun document pour le moment. Téléversez votre premier fichier ou créez un dossier."

**File Upload Flow**
1. User selects files (or drags and drops)
2. For each file: show upload progress bar
3. On upload completion: show a form to set display_name, category, description, tags
4. File is uploaded to Firebase Storage, metadata saved to Firestore
5. Success toast

**Document Viewer**
- For PDFs: embed using `<iframe>` or `<object>` with the Storage download URL (signed URL, short-lived)
- For images: display inline with zoom support
- For other types: download link only
- Document metadata displayed alongside the viewer
- Download button (always available)
- Delete button (with confirmation)
- Edit metadata button

**Security for Storage URLs**
- Generate signed URLs with 15-minute expiry for viewing/downloading
- Never expose raw Storage URLs to the client
- Storage security rules: only authenticated user with matching userId can read/write

### Model Layer (`models/document.py`)
- `upload_document(dossier_id, file_stream, filename, metadata, folder_id=None)` — uploads to Storage, creates Firestore record with folder_id
- `get_document(document_id)` — returns metadata
- `get_signed_url(document_id)` — generates a short-lived signed URL
- `list_documents(dossier_id, folder_id=None, category=None, search=None)` — lists with optional filters. When folder_id is passed, returns only documents in that folder. When search is active, ignores folder filter and searches all folders.
- `delete_document(document_id)` — deletes both Storage file and Firestore record
- `update_metadata(document_id, data)` — updates display_name, category, tags, description
- `move_document(dossier_id, document_id, target_folder_id)` — updates folder_id (Firestore only, no Storage change)
- `move_documents_bulk(dossier_id, document_ids, target_folder_id)` — batch move via Firestore batch write

### Model Layer (`models/folder.py`)
- `create_folder(dossier_id, name, parent_folder_id=None)` — validates name uniqueness within parent, max depth, creates Firestore doc
- `get_folder(dossier_id, folder_id)` — returns folder or None
- `list_folders(dossier_id, parent_folder_id=None)` — returns folders at the given level, alphabetically
- `rename_folder(dossier_id, folder_id, new_name)` — validates no duplicate name in same parent
- `move_folder(dossier_id, folder_id, new_parent_folder_id)` — validates no circular references, no duplicate names
- `delete_folder(dossier_id, folder_id, recursive=False)` — if not recursive, rejects non-empty folders; if recursive, reassigns contents to parent and deletes
- `get_folder_breadcrumb(dossier_id, folder_id)` — returns path from root to current folder for breadcrumb display
- `get_folder_tree(dossier_id)` — returns full nested tree structure for the move-to-folder modal

### Testing Criteria for Phase 9
- [ ] Single and multi-file upload works with progress indication
- [ ] Files are correctly stored in Firebase Storage at the right path
- [ ] Metadata is correctly saved to Firestore
- [ ] PDF viewer works inline
- [ ] Image viewer works with zoom
- [ ] Download produces the correct file
- [ ] Signed URLs expire correctly (verify a URL stops working after 15 min)
- [ ] Delete removes both Storage file and Firestore metadata
- [ ] Category filtering works
- [ ] File size is displayed in human-readable format (KB/MB)
- [ ] Upload rejects files over 25MB with a friendly error
- [ ] Can create folders at dossier root and nested inside other folders
- [ ] Max folder nesting depth (5 levels) is enforced
- [ ] Duplicate folder names within same parent are rejected
- [ ] Can rename and move folders
- [ ] Circular folder moves are prevented
- [ ] Breadcrumb navigation works correctly at all levels
- [ ] Uploading inside a folder assigns the document to that folder
- [ ] Moving documents between folders works (single and bulk)
- [ ] Search across all folders works and shows folder context
- [ ] Deleting an empty folder works; non-empty folder without recursive flag is rejected
- [ ] Existing documents without folder_id display at root (backward compatible)
- [ ] Folder tree in move modal renders correctly

---

## PHASE 10 — DAV Protocol Layer (CardDAV, CalDAV, RFC-5545)

### Objective
This is the most technically demanding phase. Implement CardDAV, CalDAV, and RFC-5545 (VTODO/VJOURNAL) endpoints directly in Flask so that DavX5 can synchronize contacts (clients), calendar events (hearings), tasks, and case file journals. All DAV endpoints share a common authentication mechanism (HTTP Basic Auth over TLS) and follow the WebDAV protocol extensions.

### DAV Authentication (`dav/dav_auth.py`)
- DAV clients (DavX5) use HTTP Basic Authentication
- Credentials are separate from Firebase Auth: `DAV_USERNAME` and `DAV_PASSWORD_HASH` from config
- Verify credentials using bcrypt comparison on every DAV request
- Return `401 Unauthorized` with `WWW-Authenticate: Basic realm="Pallas Athena"` on failure
- Create a `@dav_auth_required` decorator for all DAV routes

### Well-Known Endpoints
DavX5 discovers services via well-known URLs. Implement:
```
GET /.well-known/carddav → 301 redirect to /dav/addressbook/
GET /.well-known/caldav → 301 redirect to /dav/calendar/
```

### Shared DAV Concepts
All DAV endpoints must support these HTTP methods:
- `OPTIONS` — return supported methods and DAV compliance class
- `PROPFIND` — XML query for resource properties (Depth: 0 or 1)
- `REPORT` — for sync-collection reports (efficient sync)
- `GET` — retrieve individual resources (vCard, iCal)
- `PUT` — create or update a resource
- `DELETE` — remove a resource

**Sync mechanism:** Use Firestore `updated_at` timestamps and `etag` fields to implement CTag (collection tag — changes when any resource in the collection changes) and ETag (individual resource versioning). DavX5 uses these to determine what has changed since last sync.

**CTag implementation:** Store a `ctag` value in `dav_sync/{resourceType}` document. Increment (or regenerate as a UUID) whenever any resource in the collection is created, updated, or deleted.

### 10A — CardDAV (Contacts / Clients)

**Endpoints:**
```
/dav/addressbook/                  # Address book collection
/dav/addressbook/{clientId}.vcf    # Individual contact resource
```

**PROPFIND on collection (`/dav/addressbook/`, Depth: 0)**
Return collection properties:
- `{DAV:}resourcetype` → `{DAV:}collection` + `{urn:ietf:params:xml:ns:carddav}addressbook`
- `{DAV:}displayname` → "Pallas Athena — Clients"
- `{http://calendarserver.org/ns/}getctag` → current ctag
- `{DAV:}sync-token` → sync token

**PROPFIND on collection (Depth: 1)**
Return the collection properties (as Depth: 0) plus a `{DAV:}response` element for each client containing:
- `{DAV:}href` → `/dav/addressbook/{clientId}.vcf`
- `{DAV:}getetag` → client's etag
- `{urn:ietf:params:xml:ns:carddav}address-data` → full vCard (only if requested in PROPFIND body)

**REPORT (addressbook-query or sync-collection)**
For `sync-collection`: return all resources changed since the provided sync-token. The sync-token can be a timestamp or a counter. Return `{DAV:}response` elements for changed/new resources and `{DAV:}status` 404 for deleted resources.

**GET `/dav/addressbook/{clientId}.vcf`**
Return the vCard 4.0 representation of the client. Content-Type: `text/vcard; charset=utf-8`. Include `ETag` header.

**PUT `/dav/addressbook/{clientId}.vcf`**
Parse the incoming vCard, convert to client dict via `vcard_to_client()`, create or update in Firestore. Support `If-Match` header for conditional updates (compare with stored etag). Return `201 Created` or `204 No Content`.

**DELETE `/dav/addressbook/{clientId}.vcf`**
Delete the client from Firestore. Support `If-Match`. Return `204 No Content`.

### 10B — CalDAV (Hearings)

**Endpoints:**
```
/dav/calendar/                     # Calendar collection
/dav/calendar/{hearingId}.ics      # Individual event resource
```

Follow the same PROPFIND/REPORT/GET/PUT/DELETE pattern as CardDAV, but with:
- `{urn:ietf:params:xml:ns:caldav}calendar` resource type
- `{urn:ietf:params:xml:ns:caldav}calendar-data` for iCalendar data
- `{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set` → VEVENT
- Content-Type for GET/PUT: `text/calendar; charset=utf-8`

Each `.ics` resource wraps a single VEVENT in a VCALENDAR:
```
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Pallas Athena//NONSGML v1.0//EN
BEGIN:VEVENT
...
END:VEVENT
END:VCALENDAR
```

### 10C — RFC-5545 Tasks (VTODO)

**Endpoints:**
```
/dav/tasks/                        # Task collection (CalDAV calendar with VTODO support)
/dav/tasks/{taskId}.ics            # Individual task resource
```

Same CalDAV pattern but `supported-calendar-component-set` → VTODO. Each `.ics` wraps a VTODO in a VCALENDAR.

### 10D — RFC-5545 Journals (VJOURNAL for Dossiers)

**Endpoints:**
```
/dav/journals/                     # Journal collection
/dav/journals/{dossierId}.ics      # Individual journal resource
```

Same CalDAV pattern but `supported-calendar-component-set` → VJOURNAL. Dossier information is serialized as a VJOURNAL entry with:
```
BEGIN:VJOURNAL
UID:{vjournal_uid}
SUMMARY:{file_number} — {title}
DESCRIPTION:{notes}\n\nClient: {client_name}\nPartie adverse: {opposing_party}\nCour: {court}\nN° dossier judiciaire: {court_file_number}\nStatut: {status}
DTSTART:{opened_date}
STATUS:{FINAL|DRAFT|CANCELLED}
CATEGORIES:{matter_type}
END:VJOURNAL
```

### XML Response Format
All PROPFIND and REPORT responses use the `{DAV:}multistatus` XML format. Use Python's `xml.etree.ElementTree` to construct responses. Register DAV namespaces:
```python
DAV_NS = "DAV:"
CARDDAV_NS = "urn:ietf:params:xml:ns:carddav"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CS_NS = "http://calendarserver.org/ns/"
```

### DavX5 Compatibility Notes
- DavX5 first queries `/.well-known/carddav` and `/.well-known/caldav`
- It then issues `PROPFIND` on the discovered collections
- It uses `sync-collection` REPORT for subsequent syncs
- It sends `If-Match` and `If-None-Match` headers — you MUST handle these correctly
- It expects `Content-Type` headers to be exact (`text/vcard`, `text/calendar`)
- It may send `Prefer: return=minimal` — if so, omit the body on successful PUT/DELETE

### Testing Criteria for Phase 10
- [ ] Well-known redirects work
- [ ] Basic Auth correctly authenticates/rejects
- [ ] PROPFIND Depth:0 returns correct collection properties
- [ ] PROPFIND Depth:1 returns all resources in collection
- [ ] GET returns valid vCard / iCalendar data
- [ ] PUT creates new resources and updates existing ones
- [ ] DELETE removes resources
- [ ] ETags are correctly generated and compared
- [ ] CTag changes when collection is modified
- [ ] `If-Match` conditional requests work correctly
- [ ] DavX5 can discover, sync, and modify contacts (manual test with DavX5 on Android)
- [ ] DavX5 can discover, sync, and modify calendar events
- [ ] DavX5 can discover and sync tasks
- [ ] Two-way sync works: changes in web UI appear in DavX5 and vice versa
- [ ] Conflict resolution: last-write-wins with etag validation

---

## PHASE 11 — Dashboard, Polish, and Deployment

### Objective
Build the home dashboard, refine the overall UI, and deploy to Google App Engine with production-grade configuration.

### Dashboard (`/`)
The dashboard is the landing page after login. It should provide an at-a-glance summary of:

**Short-term Schedule**
- Meetings over the course of the next 7 days, chronologically
- If none: "Aucune audience prévue à court terme."

**Urgent Items**
- Tasks due within 14 days or overdue
- Protocol steps due within 14 days or overdue
- Prescription dates within 60 days

**Long-term Planning**
- Hearings over the course of the next two months, chronologically

**Quick Stats**
- Open dossiers count
- Unbilled hours (total) and unbilled amount
- Outstanding invoices (total amount of `envoyée` invoices)

**Quick Actions**
- "Nouvelle entrée de temps" (link to time entry form)
- "Nouveau dossier" (link to dossier form)
- "Nouvelle tâche" (link to task form)

### UI Polish
- Consistent empty states across all modules
- Consistent error handling (network errors, Firestore errors) with user-friendly French messages
- Loading skeletons for HTMX requests (not spinners — use animated placeholder blocks matching content layout)
- Smooth page transitions
- Consistent touch targets (min 44px height) on all interactive elements
- Test and fix any layout issues at 320px, 375px, 414px, 768px, 1024px, 1440px widths
- Ensure all date displays use `dd MMMM yyyy` format in French (e.g., "15 mars 2025")
- Ensure all currency displays use Canadian format with dollar sign

### Deployment Configuration

**Firebase Security Rules (Firestore)**
```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId}/{document=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
    // Deny everything else
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

**Firebase Storage Security Rules**
```
rules_version = '2';
service firebase.storage {
  match /b/{bucket}/o {
    match /users/{userId}/{allPaths=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

**App Engine Deployment**
- Ensure `app.yaml` is production-ready
- Set all environment variables via `gcloud` or Secret Manager
- Configure custom domain with SSL (managed by App Engine)
- Set up Cloud Logging for error monitoring
- Configure daily Firestore backups (export to Cloud Storage)

**Pre-Launch Checklist**
- [ ] All security headers verified in production
- [ ] Firebase Auth email/password only — disable all other sign-in providers (phone MFA is added in Phase 12, but phone is a second factor, not a sign-in provider)
- [ ] No debug mode in production
- [ ] Secret key is truly random and stored securely
- [ ] DAV password is strong and bcrypt-hashed
- [ ] CORS is not enabled (single-origin app, no need)
- [ ] Rate limiting is active
- [ ] File upload size limits enforced
- [ ] All Firestore queries have appropriate indexes (check logs for index creation prompts)
- [ ] Signed URLs for document access have short expiry
- [ ] No sensitive data in client-side JavaScript or HTML source
- [ ] Custom 404 and 500 error pages in French

### Testing Criteria for Phase 11
- [ ] Dashboard loads quickly and displays all sections correctly
- [ ] Dashboard shows accurate counts and summaries
- [ ] All empty states are friendly and informative
- [ ] App deploys to App Engine without errors
- [ ] Custom domain with HTTPS works
- [ ] All features work in production environment
- [ ] DavX5 sync works against the production URL
- [ ] Mobile experience is smooth and fast (test on actual phone)
- [ ] Browser "Add to Home Screen" works as a pseudo-PWA (optional: add a manifest.json)

---

## PHASE 12 — Firebase App Check and Multi-Factor Authentication

### Objective
Harden the application's security posture with two critical Firebase features: **App Check**, which ensures that only your legitimate application can access Firebase backend services (Firestore, Storage, Authentication), and **phone-based Multi-Factor Authentication (MFA)**, which adds a second factor to the login process. After this phase, every Firebase API call from the client is attested, and login requires both the password and a phone verification code.

This phase is intentionally placed last because it modifies the authentication flow established in Phase 1, the Firestore and Storage access patterns used throughout Phases 2–9, and the client-side Firebase SDK initialization. Implementing it on a fully working application minimizes the risk of breaking existing functionality.

### 12A — Firebase App Check

**What App Check does:** App Check adds an attestation layer between your application and Firebase services. Before the client can read/write Firestore, upload/download from Storage, or call Authentication APIs, it must present a valid App Check token. This token proves the request originates from your legitimate app, not from a script, a stolen API key, or a malicious client. Without a valid token, Firebase rejects the request.

**Attestation Provider:**
Since Pallas Athena is a web application hosted on App Engine, use **reCAPTCHA Enterprise** as the attestation provider. This is Google's recommended provider for web apps. It runs invisibly in the background (no user-facing CAPTCHA puzzles) and issues attestation tokens that App Check validates.

**Server-Side Configuration:**

1. **Enable App Check in Firebase Console:**
   - Navigate to Firebase Console → App Check
   - Register your web app with reCAPTCHA Enterprise
   - Obtain the reCAPTCHA Enterprise site key
   - Add the site key to `config.py` as `RECAPTCHA_ENTERPRISE_SITE_KEY`

2. **Enforce App Check on Firebase services:**
   - In the Firebase Console, enable enforcement for:
     - Cloud Firestore
     - Cloud Storage
     - Firebase Authentication
   - **Critical:** Do NOT enable enforcement until the client-side integration is fully working and tested. Premature enforcement will lock you out of your own app. Enable enforcement only after verifying that all client-side requests include valid App Check tokens.

3. **Backend token verification (`auth.py` modifications):**
   - For any server-side Firebase Admin SDK operations (which bypass App Check by default since they use service account credentials), no changes are needed — Admin SDK calls are trusted.
   - For the DAV endpoints (Phase 10), which use HTTP Basic Auth and server-side Firestore calls via the Admin SDK, no App Check changes are needed — these are server-to-server calls.
   - The protection is primarily on the **client-side path**: the Firebase JS SDK calls from the browser (Authentication sign-in, and any direct client-side Firestore/Storage calls if they exist).

**Client-Side Integration (`templates/base.html` and `templates/auth/login.html`):**

1. **Initialize App Check in the Firebase JS SDK:**
   ```javascript
   import { initializeAppCheck, ReCaptchaEnterpriseProvider } from "firebase/app-check";
   
   const appCheck = initializeAppCheck(app, {
     provider: new ReCaptchaEnterpriseProvider('{{ config.RECAPTCHA_ENTERPRISE_SITE_KEY }}'),
     isTokenAutoRefreshEnabled: true  // Automatically refresh tokens before expiry
   });
   ```
   This must be called **before** any Firebase Auth, Firestore, or Storage calls.

2. **Placement:** Initialize App Check immediately after `initializeApp()` in every page that uses the Firebase JS SDK. In Pallas Athena, the primary client-side Firebase usage is:
   - `login.html`: Firebase Auth `signInWithEmailAndPassword()` and MFA enrollment/verification (Phase 12B)
   - Any page that makes direct client-side Firestore or Storage calls (if any — the architecture primarily uses server-side Flask calls, so this may be limited to the auth flow)

3. **Debug token for local development:**
   When running locally (`ENV=development`), App Check will fail because `localhost` is not registered with reCAPTCHA Enterprise. Use a debug provider:
   ```javascript
   if (location.hostname === 'localhost') {
     self.FIREBASE_APPCHECK_DEBUG_TOKEN = true;
   }
   ```
   Register the debug token in the Firebase Console under App Check → Apps → Manage debug tokens. Add `APPCHECK_DEBUG_TOKEN` to `config.py` for local development.

**CSP Header Update (`security.py`):**
reCAPTCHA Enterprise requires loading scripts from Google. Update the Content-Security-Policy header:
```
script-src: add https://www.google.com https://www.gstatic.com
frame-src: add https://www.google.com https://recaptcha.google.com
connect-src: add https://www.google.com https://recaptchaenterprise.googleapis.com
```

### 12B — Phone-Based Multi-Factor Authentication (MFA)

**What phone MFA does:** After the user successfully authenticates with email + password, Firebase Auth requires a second verification step: a 6-digit code sent via SMS to the user's registered phone number. The user must enter this code to complete the login. This protects against password compromise — an attacker who obtains the password still cannot log in without access to the phone.

**Firebase MFA Architecture:**
Firebase Auth's MFA is handled entirely through the client-side JS SDK. The flow is:
1. User signs in with email + password → Firebase returns a `MultiFactorError` if MFA is enrolled
2. Client-side code catches this error, extracts the MFA resolver
3. Client requests SMS verification via the resolver → Firebase sends the SMS code
4. User enters the 6-digit code → client verifies it via the resolver
5. On success, Firebase returns the fully authenticated ID token (with MFA claims)
6. Client POSTs this token to Flask backend (`/auth/verify-token`) as in Phase 1

**MFA Enrollment (first-time setup):**
Since this is a single-user application, MFA enrollment happens once. Build an enrollment page at `/auth/mfa-setup` (accessible only when authenticated):
1. User navigates to MFA setup (or is redirected there on first login if MFA is not yet enrolled)
2. Page shows a phone number input field with French label ("Numéro de téléphone pour la vérification en deux étapes")
3. User enters their phone number (pre-formatted for Canadian numbers: +1XXXXXXXXXX)
4. Client calls `multiFactor.getSession()` then `PhoneAuthProvider.verifyPhoneNumber()` with a `RecaptchaVerifier` (invisible reCAPTCHA, separate from App Check)
5. Firebase sends an SMS code to the phone
6. User enters the 6-digit code on the page
7. Client calls `PhoneMultiFactorGenerator.assertion()` then `multiFactor.enroll()` with a display name ("Téléphone principal")
8. Success toast: "Vérification en deux étapes activée."
9. Redirect to dashboard

**Modified Login Flow (`templates/auth/login.html`):**
Update the Phase 1 login flow to handle MFA:

```javascript
// Pseudocode — implement in the login.html Firebase JS SDK logic
try {
  const userCredential = await signInWithEmailAndPassword(auth, email, password);
  // MFA not enrolled or not required — proceed as before
  await postIdTokenToBackend(userCredential.user);
} catch (error) {
  if (error.code === 'auth/multi-factor-auth-required') {
    // MFA is enrolled — need second factor
    const resolver = getMultiFactorResolver(auth, error);
    
    // Show the MFA verification UI (hide login form, show code input)
    showMfaCodeInput();
    
    // Send SMS verification
    const phoneInfoOptions = {
      multiFactorHint: resolver.hints[0],  // First enrolled phone factor
      session: resolver.session
    };
    const phoneAuthProvider = new PhoneAuthProvider(auth);
    const verificationId = await phoneAuthProvider.verifyPhoneNumber(
      phoneInfoOptions, 
      recaptchaVerifier  // Invisible reCAPTCHA widget
    );
    
    // User enters the code...
    const code = await waitForUserCode();  // Your UI logic
    const cred = PhoneAuthProvider.credential(verificationId, code);
    const assertion = PhoneMultiFactorGenerator.assertion(cred);
    const userCredential = await resolver.resolveSignIn(assertion);
    
    // MFA verified — proceed to backend verification
    await postIdTokenToBackend(userCredential.user);
  } else {
    // Other auth error — display to user
    showError(error.message);
  }
}
```

**Login Page UI Changes:**
The login page now has two states:
1. **Initial state:** Email + password fields, "Connexion" button (unchanged from Phase 1)
2. **MFA state:** Revealed after successful password authentication when MFA is enrolled. Shows:
   - A message: "Un code de vérification a été envoyé au numéro se terminant par ••••XX." (last 2 digits shown)
   - A 6-digit code input (individual boxes for each digit, auto-advance on entry)
   - "Vérifier" button
   - "Renvoyer le code" link (with cooldown timer: "Renvoyer dans 30s")
   - "Annuler" link (returns to initial state)
   - Transition between states should be smooth (HTMX swap or Alpine.js toggle, no full page reload)

**Backend Verification (`auth.py` modifications):**
The `verify_id_token()` call in Phase 1 does not need modification — Firebase ID tokens issued after MFA verification automatically contain MFA claims. However, add an optional strictness check:
```python
# Optional: verify that the token was issued after MFA verification
decoded_token = firebase_admin.auth.verify_id_token(id_token)
# The token's 'firebase' claim includes 'sign_in_second_factor' if MFA was used
if config.REQUIRE_MFA:
    firebase_claim = decoded_token.get('firebase', {})
    if 'sign_in_second_factor' not in firebase_claim:
        # MFA was not completed — reject
        return jsonify({"error": "Vérification en deux étapes requise."}), 403
```

Add to `config.py`:
- `REQUIRE_MFA`: Boolean, default `True` in production. Set to `False` in development if MFA is not yet enrolled. Once enrolled, set to `True` permanently.
- `MFA_PHONE_NUMBER`: Not stored in config — Firebase manages this. The phone number is registered through the enrollment flow and stored by Firebase Auth, not in your Firestore.

**MFA Management Page (`/auth/mfa-manage`):**
A simple settings page (accessible from the "Plus" menu) that shows:
- Current MFA status: Enrolled / Not enrolled
- Registered phone number (masked: +1 514 •••-••34)
- "Désinscrire" button (unenroll MFA — requires re-authentication, confirm dialog warning about security implications)
- "Changer le numéro" button (unenroll + re-enroll flow)

**reCAPTCHA for SMS Verification:**
Firebase requires a `RecaptchaVerifier` for phone auth to prevent SMS abuse. Use an invisible reCAPTCHA:
```javascript
const recaptchaVerifier = new RecaptchaVerifier(auth, 'mfa-recaptcha-container', {
  size: 'invisible'
});
```
Add a `<div id="mfa-recaptcha-container"></div>` to both the login page and the MFA setup page. This is separate from the App Check reCAPTCHA Enterprise provider — they serve different purposes and coexist without conflict.

**CSP Header Update (`security.py`):**
The phone auth reCAPTCHA may require additional CSP entries (these likely overlap with App Check's entries, but verify):
```
frame-src: https://www.google.com (already added for App Check)
script-src: https://www.google.com https://www.gstatic.com (already added for App Check)
```

### Configuration Summary

Add the following to `config.py`:
```python
# App Check
RECAPTCHA_ENTERPRISE_SITE_KEY = os.environ.get('RECAPTCHA_ENTERPRISE_SITE_KEY', '')
APPCHECK_DEBUG_TOKEN = os.environ.get('APPCHECK_DEBUG_TOKEN', '')  # Local dev only

# MFA
REQUIRE_MFA = os.environ.get('REQUIRE_MFA', 'true').lower() == 'true'
```

Add the following environment variables to `app.yaml` (or Secret Manager):
```yaml
env_variables:
  RECAPTCHA_ENTERPRISE_SITE_KEY: "your-site-key-here"
  REQUIRE_MFA: "true"
```

### Deployment Order

This phase must be deployed carefully to avoid locking yourself out:

1. **Deploy client-side App Check integration** with enforcement **disabled** in Firebase Console. Verify that all client-side Firebase calls include App Check tokens (check browser network tab for `X-Firebase-AppCheck` headers).
2. **Enroll MFA** through the `/auth/mfa-setup` page with `REQUIRE_MFA=false`. Verify enrollment works and the SMS code is received.
3. **Set `REQUIRE_MFA=true`** and redeploy. Verify login requires both password and SMS code.
4. **Enable App Check enforcement** in Firebase Console for Firestore, Storage, and Authentication. Verify the app still works. If anything breaks, disable enforcement immediately (it takes effect within minutes).
5. **Verify DAV endpoints still work** — they use server-side Admin SDK calls and should not be affected by App Check, but confirm explicitly.

### Testing Criteria for Phase 12
- [ ] App Check is initialized before any Firebase JS SDK calls
- [ ] reCAPTCHA Enterprise runs invisibly (no user-facing puzzle)
- [ ] Debug token works for local development
- [ ] With App Check enforcement disabled, all features still work identically
- [ ] With App Check enforcement enabled, all features still work (client-side calls include valid tokens)
- [ ] With App Check enforcement enabled, requests without valid tokens are rejected by Firebase
- [ ] MFA enrollment flow works: phone number entry → SMS received → code verified → enrollment confirmed
- [ ] Login flow correctly handles MFA: password → SMS code → verified → session established
- [ ] Login rejects authentication when MFA code is incorrect
- [ ] Login rejects authentication when MFA is enrolled but `REQUIRE_MFA=true` and MFA step is skipped
- [ ] "Renvoyer le code" works with appropriate cooldown
- [ ] MFA management page shows enrollment status and allows unenrollment
- [ ] Phone number change (unenroll + re-enroll) works
- [ ] CSP headers updated correctly — no console errors for blocked resources
- [ ] DAV endpoints (Phase 10) are unaffected by App Check (server-side Admin SDK calls)
- [ ] Full login flow on mobile: email → password → MFA code → dashboard (smooth, no layout issues)

---

## IMPLEMENTATION NOTES FOR THE AI ASSISTANT

When implementing each phase, follow these rules:

1. **Implement completely.** Each phase should result in fully working code. Do not leave placeholders like `# TODO` or `pass` unless explicitly noted. Every function should have a real implementation.

2. **French UI, English code.** All user-facing strings (labels, buttons, placeholders, error messages, toasts, headings, empty states) must be in French. All code (variable names, comments, function names, docstrings) must be in English.

3. **Security first.** Never skip CSRF, never skip auth checks, never expose raw Firestore paths or Storage URLs. Validate all inputs on the server side even if client-side validation exists.

4. **Mobile first.** Always design for a 375px screen first, then add responsive breakpoints for larger screens. Touch targets minimum 44px. Generous padding and margins.

5. **Minimalist aesthetics.** White-space is a design element, not wasted space. Limit the color palette. No decorative elements. Clean typography. Thin borders or shadows for card separation. The app should feel calm and professional.

6. **HTMX patterns.** Use HTMX for all dynamic interactions: form submissions, partial page updates, infinite scroll, search-as-you-type, modal loading, tab switching. Minimize custom JavaScript. Return HTML fragments from Flask endpoints (not JSON) for HTMX requests.

7. **Error handling.** Every Firestore operation should be wrapped in try/except. Every form submission should handle and display validation errors. Network errors should show a toast, not crash the page.

8. **Consistent patterns.** Once you establish a pattern for CRUD (e.g., in Phase 2 for clients), replicate the exact same pattern for subsequent modules. Consistent code is maintainable code.

9. **Test incrementally.** After each phase, the app should be runnable and all new features should work end-to-end. Do not introduce dependencies on future phases.

10. **DAV compliance.** In Phase 10, strict compliance with RFC 6352 (CardDAV), RFC 4791 (CalDAV), and RFC 5545 (iCalendar) is essential. DavX5 is a strict client — partial compliance will cause sync failures. When in doubt, consult the RFCs. Use the `icalendar` and `vobject` libraries for serialization rather than building strings manually.
