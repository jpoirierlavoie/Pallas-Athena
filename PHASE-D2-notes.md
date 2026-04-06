# PHASE D2 — Dossier Notes + VJOURNAL Serialization

Read CLAUDE.md for project context. **Phase D1 must be completed and tested before starting this phase.** D1 established per-dossier CalDAV collections at `/dav/dossier-{id}/` that serve VTODO resources. This phase adds VJOURNAL resources (notes) to those same collections, plus the web UI for creating and managing notes.

## Context

After D1, each dossier collection advertises `supported-calendar-component-set` with both VTODO and VJOURNAL, but the VJOURNAL handlers return 501. This phase implements the VJOURNAL handling.

A note is a timestamped record: meeting notes, research findings, call summaries, strategy memos, court appearance observations. Each note becomes a VJOURNAL entry within its parent dossier's CalDAV collection.

## Step 1 — Create `models/note.py`

```python
"""Dossier notes — timestamped journal entries linked to case files.

Each note becomes a VJOURNAL resource in the dossier's CalDAV collection
at /dav/dossier-{dossierId}/{noteId}.ics
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

COLLECTION = "notes"

VALID_CATEGORIES = (
    "appel",
    "rencontre",
    "recherche",
    "stratégie",
    "correspondance",
    "audience",
    "autre",
)

CATEGORY_LABELS = {
    "appel": "Appel",
    "rencontre": "Rencontre",
    "recherche": "Recherche",
    "stratégie": "Stratégie",
    "correspondance": "Correspondance",
    "audience": "Audience",
    "autre": "Autre",
}
```

### Schema

```python
def _default_doc() -> dict:
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "content": "",
        "category": "autre",
        "pinned": False,
        # DAV
        "vjournal_uid": "",
        # Metadata
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }
```

### CRUD Functions

Implement these following the exact same pattern as `models/task.py`:

```python
def _sanitize_data(data: dict) -> dict: ...

def _validate(data: dict) -> list[str]:
    """Title and dossier_id are required. Content is required."""
    errors = []
    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette note.")
    if not data.get("title", "").strip():
        errors.append("Le titre de la note est requis.")
    if not data.get("content", "").strip():
        errors.append("Le contenu de la note est requis.")
    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")
    return errors

def create_note(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors).
    Set vjournal_uid = uuid4() on creation.
    """
    ...

def get_note(note_id: str) -> Optional[dict]: ...

def list_notes(
    dossier_id: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    pinned_first: bool = True,
) -> list[dict]:
    """Return notes, pinned first then newest first.
    Search scans title + content (client-side, same as other modules).
    """
    ...

def update_note(note_id: str, data: dict) -> tuple[Optional[dict], list[str]]: ...
def delete_note(note_id: str) -> tuple[bool, str]: ...

def toggle_pin(note_id: str) -> tuple[Optional[dict], list[str]]:
    """Toggle the pinned status of a note."""
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."]
    return update_note(note_id, {"pinned": not existing.get("pinned", False)})

def get_notes_summary(dossier_id: str) -> dict:
    """Return {total, recent_count} for tab display."""
    notes = list_notes(dossier_id=dossier_id)
    return {"total": len(notes)}
```

### VJOURNAL Serialization

```python
def note_to_vjournal(note: dict) -> str:
    """Serialize a note to an RFC-5545 VJOURNAL string wrapped in VCALENDAR.

    Properties:
    - UID: note's vjournal_uid
    - SUMMARY: note title
    - DESCRIPTION: note content
    - DTSTART: note created_at (date only)
    - CATEGORIES: note category label (French)
    - STATUS: FINAL (notes are always finalized records)
    - LAST-MODIFIED: note updated_at
    - SEQUENCE: 0
    - X-PALLAS-NOTE-CATEGORY: category key (for round-trip fidelity)
    - X-PALLAS-DOSSIER-ID: dossier_id
    """
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Note//FR")
    cal.add("version", "2.0")

    journal = icalendar.Journal()
    journal.add("uid", note.get("vjournal_uid", ""))
    journal.add("summary", note.get("title", ""))

    if note.get("content"):
        journal.add("description", note["content"])

    created = note.get("created_at")
    if created and hasattr(created, "date"):
        journal.add("dtstart", created.date())

    journal.add("status", "FINAL")

    if note.get("category"):
        label = CATEGORY_LABELS.get(note["category"], note["category"])
        journal.add("categories", [label])

    updated = note.get("updated_at")
    if updated:
        journal.add("last-modified", updated)

    journal.add("sequence", 0)

    # Custom X- properties
    if note.get("category"):
        journal.add("x-pallas-note-category", note["category"])
    if note.get("dossier_id"):
        journal.add("x-pallas-dossier-id", note["dossier_id"])
    if note.get("pinned"):
        journal.add("x-pallas-pinned", "true")

    cal.add_component(journal)
    return cal.to_ical().decode("utf-8")


def vjournal_to_note(ical_str: str) -> dict:
    """Parse a VJOURNAL string into a note dict (for DAV PUT).

    Extracts standard properties and X-PALLAS-* custom properties.
    """
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VJOURNAL":
            continue

        uid = component.get("uid")
        if uid:
            data["vjournal_uid"] = str(uid)

        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        desc = component.get("description")
        if desc:
            data["content"] = str(desc)

        dtstart = component.get("dtstart")
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                data["created_at"] = dt
            else:
                data["created_at"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # X- properties
        category = component.get("x-pallas-note-category")
        if category:
            cat = str(category)
            if cat in VALID_CATEGORIES:
                data["category"] = cat

        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        pinned = component.get("x-pallas-pinned")
        if pinned and str(pinned).lower() == "true":
            data["pinned"] = True

        break

    return data
```

## Step 2 — Update `dav/dossier_collections.py`

Phase D1 left placeholder comments (`# D2: try note here`) throughout the DAV handlers. Now implement them.

### Add imports at top of file:

```python
from models.note import (
    create_note,
    delete_note,
    get_note,
    list_notes,
    note_to_vjournal,
    update_note,
    vjournal_to_note,
)
```

### Update PROPFIND Depth:1 — include notes:

```python
if depth == "1":
    # Tasks (existing)
    tasks = list_tasks(dossier_id=dossier_id)
    for task in tasks:
        _add_task_resource(multistatus, dossier_id, task, body)

    # Notes (NEW)
    notes = list_notes(dossier_id=dossier_id)
    for note in notes:
        _add_note_resource(multistatus, dossier_id, note, body)
```

### Add `_add_note_resource` helper:

```python
def _add_note_resource(multistatus, dossier_id, note, body):
    """Add a single VJOURNAL resource <D:response>."""
    href = f"/dav/dossier-{dossier_id}/{note['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = "text/calendar; charset=utf-8"

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))

    if body is not None and propfind_requests_prop(body, caldav_tag("calendar-data")):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)
```

### Update PROPFIND Resource — try note after task:

```python
def propfind_resource(dossier_id, resource_id):
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        ...  # existing

    # NEW: try note
    note = get_note(resource_id)
    if note and note.get("dossier_id") == dossier_id:
        body = parse_propfind_body(request.get_data())
        multistatus = make_multistatus()
        _add_note_resource(multistatus, dossier_id, note, body)
        xml = serialize_multistatus(multistatus)
        return Response(xml, status=207, content_type="application/xml; charset=utf-8")

    return Response("Not Found", status=404)
```

### Update all REPORT handlers — include notes:

In `_handle_sync_collection`:
```python
# After tasks loop:
notes = list_notes(dossier_id=dossier_id)
for note in notes:
    resp = add_response(multistatus, f"/dav/dossier-{dossier_id}/{note['id']}.ics")
    prop = add_propstat(resp)
    ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'
```

In `_handle_multiget`:
```python
# After task lookup:
note = get_note(resource_id)
if note and note.get("dossier_id") == dossier_id:
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)
    ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'
    ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)
    continue
```

In `_handle_calendar_query`:
```python
# After tasks loop:
notes = list_notes(dossier_id=dossier_id)
for note in notes:
    href = f"/dav/dossier-{dossier_id}/{note['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)
    ET.SubElement(prop, dav_tag("getetag")).text = f'"{note.get("etag", "")}"'
    ET.SubElement(prop, caldav_tag("calendar-data")).text = note_to_vjournal(note)
```

### Update GET — try note after task:

```python
def get_resource(dossier_id, resource_id):
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        ...  # existing

    # NEW
    note = get_note(resource_id)
    if note and note.get("dossier_id") == dossier_id:
        ical = note_to_vjournal(note)
        resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
        resp.headers["ETag"] = f'"{note.get("etag", "")}"'
        return resp

    return Response("Not Found", status=404)
```

### Update PUT — handle VJOURNAL:

Replace the 501 stub in `put_resource`:

```python
elif component_type == "VJOURNAL":
    return _put_note(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
```

Add `_put_note`:

```python
def _put_note(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match):
    """Handle PUT for a VJOURNAL resource."""
    existing = get_note(resource_id)

    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    try:
        data = vjournal_to_note(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    data["dossier_id"] = dossier_id
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")

    sync_name = f"dossier:{dossier_id}"

    if existing:
        updated, errors = update_note(resource_id, data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_note(data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp
```

### Update DELETE — try note after task:

```python
def delete_resource(dossier_id, resource_id):
    # Try task (existing)
    ...

    # NEW: try note
    existing_note = get_note(resource_id)
    if existing_note and existing_note.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing_note.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_note(resource_id)
        if not success:
            return Response(error, status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    return Response("Not Found", status=404)
```

## Step 3 — Create Routes: `routes/notes.py`

```python
"""Dossier note routes — create, list, detail, edit, delete, pin."""

from flask import Blueprint, redirect, render_template, request, url_for
from auth import login_required
from dav.sync import bump_ctag
from models.note import (
    CATEGORY_LABELS,
    VALID_CATEGORIES,
    create_note,
    delete_note,
    get_note,
    list_notes,
    toggle_pin,
    update_note,
)
from models.dossier import get_dossier, list_dossiers

notes_bp = Blueprint("notes", __name__, url_prefix="/notes")
```

### Routes to implement:

```python
@notes_bp.route("/")                        # List (all dossiers or filtered)
@notes_bp.route("/<note_id>")               # Detail
@notes_bp.route("/new")                     # New note form (GET)
@notes_bp.route("/", methods=["POST"])      # Create (POST)
@notes_bp.route("/<note_id>/edit")          # Edit form (GET)
@notes_bp.route("/<note_id>", methods=["POST"])  # Update (POST)
@notes_bp.route("/<note_id>/delete", methods=["POST"])  # Delete
@notes_bp.route("/<note_id>/pin", methods=["POST"])     # Toggle pin
```

**After every create/update/delete, bump the dossier CTag:**
```python
if note.get("dossier_id"):
    bump_ctag(f"dossier:{note['dossier_id']}")
```

### Form fields:
- Dossier selector (required — autocomplete, same pattern as tasks/hearings)
- Title (required, text input)
- Category (select dropdown from VALID_CATEGORIES)
- Content (required, textarea, min-height ~200px, monospace-optional)
- Pin toggle (checkbox: "Épingler cette note")

### List page features:
- Filter by dossier (dropdown or from query param)
- Filter by category
- Search by title/content
- Pinned notes displayed first with a pin icon
- Each row: title, category badge, first ~100 chars of content truncated, date

### Detail page:
- Full content display with `whitespace-pre-wrap`
- Category badge, pin indicator, created/updated dates
- Link back to dossier
- Edit, Delete, Pin/Unpin buttons

## Step 4 — Register Blueprint

In `main.py`:
```python
from routes.notes import notes_bp
app.register_blueprint(notes_bp)
```

## Step 5 — Integrate Notes into Dossier Detail

In `routes/dossiers.py`, update the `dossier_tab` handler for `tab_name == "documents"`:

```python
if tab_name == "documents":
    # Load notes
    from models.note import list_notes, get_notes_summary, CATEGORY_LABELS as NOTE_CATEGORY_LABELS
    ctx["notes"] = list_notes(dossier_id=dossier_id)
    ctx["notes_summary"] = get_notes_summary(dossier_id)
    ctx["note_category_labels"] = NOTE_CATEGORY_LABELS

    # ... existing folder and document loading (unchanged)
```

In `templates/dossiers/_tab_documents.html`, add a notes section at the top, before the file browser:

```jinja2
{# ── Notes section ──────────────────────────────────────────── #}
<div class="mb-6">
  <div class="flex items-center justify-between mb-3">
    <h3 class="text-sm font-semibold text-gray-900">Notes</h3>
    <a href="{{ url_for('notes.note_new', dossier_id=dossier.id) }}"
       class="text-xs font-medium text-indigo-600 hover:text-indigo-700">
      + Nouvelle note
    </a>
  </div>

  {% if notes %}
  <div class="flex flex-col gap-2">
    {% for n in notes[:5] %}
    <a href="{{ url_for('notes.note_detail', note_id=n.id) }}"
       class="p-3 bg-white rounded-lg border border-gray-200 hover:border-gray-300 transition-colors">
      <div class="flex items-center gap-2 mb-1">
        {% if n.pinned %}
        <svg class="w-3 h-3 text-amber-500 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
          <path d="M9.653 16.915l-.005-.003-.019-.01a20.759 20.759 0 01-1.162-.682 22.045 22.045 0 01-2.582-1.9C4.045 12.733 2 10.352 2 7.5a4.5 4.5 0 018-2.828A4.5 4.5 0 0118 7.5c0 2.852-2.044 5.233-3.885 6.82a22.049 22.049 0 01-3.744 2.582l-.019.01-.005.003h-.002a.723.723 0 01-.692 0h-.002z"/>
        </svg>
        {% endif %}
        <span class="text-sm font-medium text-gray-900 truncate">{{ n.title }}</span>
        <span class="text-[10px] px-1.5 py-0.5 rounded-full font-medium bg-gray-100 text-gray-600">
          {{ note_category_labels.get(n.category, n.category) }}
        </span>
      </div>
      <p class="text-xs text-gray-500 truncate">{{ n.content[:120] }}</p>
      {% if n.created_at and n.created_at.strftime is defined %}
      <p class="text-xs text-gray-400 mt-1">
        {% set month_short = ['', 'janv', 'févr', 'mars', 'avr', 'mai', 'juin', 'juil', 'août', 'sept', 'oct', 'nov', 'déc'] %}
        {{ n.created_at.strftime('%d') }} {{ month_short[n.created_at.month] }} {{ n.created_at.strftime('%Y') }}
      </p>
      {% endif %}
    </a>
    {% endfor %}
    {% if notes|length > 5 %}
    <a href="{{ url_for('notes.note_list', dossier_id=dossier.id) }}"
       class="text-xs text-center text-indigo-600 py-2">
      Voir les {{ notes|length }} notes
    </a>
    {% endif %}
  </div>
  {% else %}
  <p class="text-gray-400 text-sm py-2">Aucune note pour ce dossier.</p>
  {% endif %}
</div>

<hr class="border-gray-200 mb-6">

{# ── Files section (existing, unchanged) ──────────────────── #}
```

## Step 6 — Create Note Templates

Create these templates following the same design patterns as tasks:

- `templates/notes/list.html` — full page with filters (dossier, category, search)
- `templates/notes/detail.html` — full content, metadata, actions
- `templates/notes/form.html` — create/edit form
- `templates/notes/_note_rows.html` — HTMX partial for filtered list

## Step 7 — Add Notes to Sidebar/Navigation

In `templates/base.html`:

Desktop sidebar: add a "Notes" link after "Documents" (or combine conceptually — the dossier tab already shows both). Optional — the primary access path is through the dossier detail tab.

Mobile "Plus" menu: add "Notes" entry if a standalone notes list page exists.

## Step 8 — Firestore Collection Map Update

```
users/{userId}/
├── ...
├── notes/{noteId}               # NEW — dossier journal notes
├── ...
```

## Testing Checklist
- [ ] Can create a note linked to a dossier via web UI
- [ ] Title, content, category, pinned status saved correctly
- [ ] Notes appear in the Documents tab of the dossier detail (above files)
- [ ] Pinned notes display first with pin icon
- [ ] Note search works (title + content)
- [ ] Note category filter works
- [ ] Note detail shows full content with whitespace preserved
- [ ] Note edit and delete work
- [ ] Pin toggle works
- [ ] DAV: PROPFIND Depth:1 on dossier collection returns both tasks AND notes
- [ ] DAV: GET on `/dav/dossier-{id}/{noteId}.ics` returns valid VJOURNAL
- [ ] DAV: PUT VJOURNAL to dossier collection creates a note
- [ ] DAV: PUT VJOURNAL to dossier collection updates an existing note
- [ ] DAV: DELETE VJOURNAL removes the note
- [ ] DAV: sync-collection REPORT includes notes
- [ ] DAV: calendar-multiget REPORT fetches specific notes
- [ ] DAV: PUT correctly distinguishes VTODO from VJOURNAL in the same collection
- [ ] Per-dossier CTag bumps on note create/update/delete
- [ ] DavX5: notes appear as journal entries within the dossier collection
- [ ] DavX5/jtx Board: notes display with title and content
- [ ] Creating a note in jtx Board → syncs to Pallas Athena web UI
- [ ] Web UI unchanged for all non-note functionality (no regression)
