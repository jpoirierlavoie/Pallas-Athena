# Pallas Athéna

**A single-user practice manager for Québec civil & commercial litigation.**
*Application de gestion de cabinet pour le litige civil et commercial au Québec.*

Pallas Athéna centralizes everything a solo litigator needs: case files
(*dossiers*), contacts (*parties*), billable time and expenses, invoices with
GST/QST, hearings, tasks, case protocols, notes, procedural documents, and
`.docx` template generation. It syncs contacts, calendars, and tasks to Android
(DavX5) over CardDAV/CalDAV/RFC-5545, and exposes an MCP connector for Claude
— read-only, except for two opt-in tools that add notes to a dossier.

> ⚠️ **Not open-source, and not a clone-and-run project.** See
> [Using this software](#using-this-software) before doing anything.

---

## Features

- **Dossiers** with Québec court-file-number parsing (greffe / juridiction)
- **Parties** — one contact model for clients, opposing parties, counsel,
  experts, witnesses, bailiffs, notaries (with KYC/conflict fields for clients)
- **Billing** — time & expenses → invoices with GST (5 %) and QST (9.975 %)
- **Hearings & calendar**, **tasks**, and **case protocols** (CQ simplifié /
  CS ordinaire / conventionnel) with art. 83 C.p.c. deadline calculation
- **Notes** (Markdown) and a **document store** with folders and signed URLs
- **Gabarits** — user-managed Word templates filled from case data
- **DavX5 sync** (CardDAV / CalDAV / VTODO / VJOURNAL) and an **MCP connector**
  exposing data to Claude — read-only by default; note creation and appending
  require an explicit `athena:write` grant ticked on the consent screen

## Tech stack

Python 3.13 · Flask · Jinja2 + HTMX + Alpine.js + precompiled Tailwind
(no SPA, no build step) · Google App Engine Standard · Firestore (native mode) ·
Firebase Auth (+ Phone MFA) + App Check · Firebase Storage · Cloudflare (Pro).

```
Browser / DavX5 / Claude → Cloudflare → App Engine (Flask)
                                          → Firestore / Storage / Auth / Secret Manager
```

## Design assumptions (read before adopting)

This app is **intentionally narrow**:

- **Single user** — exactly one authorized email, enforced server-side. No
  multi-tenancy, no roles.
- **French-only UI** — no internationalization layer.
- **Québec-specific** — taxes, judicial deadlines, court-file parsing, and
  reference data assume Québec practice. Deploying elsewhere requires code
  changes (see [DEPLOYMENT.md §13](DEPLOYMENT.md)).

Adopting Pallas Athéna means **re-provisioning your own cloud stack and adapting
the domain logic** — not just running a binary.

## Using this software

The source is published for reference. The [LICENSE](LICENSE) is
**all-rights-reserved**: no right to use, copy, modify, or deploy is granted
automatically.

You may run your own instance **only with prior written permission** from the
copyright holder, and — because this software handles data covered by
professional secrecy — permission is granted **only to practising lawyers**.

**To request permission**, email `jason@poirierlavoie.ca` with your name, bar/
order membership and number, your jurisdiction, and how you intend to use it.

## Documentation

| Doc | For |
|---|---|
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | Standing up your own instance, end to end (once permitted) |
| **[SECURITY.md](SECURITY.md)** | Security posture, secret management, vulnerability disclosure |
| **[CLAUDE.md](CLAUDE.md)** | Full developer/architecture reference |
| **[athena/OBSERVABILITY.md](athena/OBSERVABILITY.md)** | Logging events + tracing conventions |

Quick config sanity check for a redeployment: `cd athena && python -m scripts.check_config`.

## Glossary (French domain terms)

**dossier** case file · **partie** contact/party · **greffe** court registry ·
**juridiction** tribunal/competence · **audience** hearing · **protocole**
case-management protocol · **gabarit** document template · **temps/dépenses**
time/expenses · **facture** invoice.

---

*Cette application n'est pas destinée au grand public. Son accès est réservé aux
personnes autorisées. Aucune donnée n'est recueillie à des fins publicitaires ou
de suivi.*

© 2026 Jason Poirier Lavoie. Tous droits réservés / All rights reserved.
