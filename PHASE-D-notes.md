# PHASE D — Dossier Notes + VJOURNAL Notes Serialization

Read CLAUDE.md for project context. This phase adds a timestamped notes subsystem to dossiers and serializes notes as VJOURNAL components for DavX5 sync.

## Context

A litigation file accumulates notes throughout its life: meeting notes, research notes, call summaries, strategy memos, settlement discussions. Currently the dossier model has a single `notes` text field and a single `internal_notes` field. This phase creates a proper `notes` subcollection under each dossier, with each note as a timestamped, categorized entry.

For DAV sync, RFC 5545 allows multiple VJOURNAL components. Each dossier note will become its own VJOURNAL entry in the `/dav/journals/` collection, linked to the parent dossier's VJOURNAL via the `RELATED-TO` property.

## Step 1 — Firestore Schema: `notes/{noteId}`

Create `models/note.py`:

```python
# Firestore path: notes/{noteId}  (top-level, like other collections)
# Each note belongs to a dossier via dossier_id.
{
    "id": "uuid-v4",
    "dossier_id": "uuid-ref",
    "dossier_file_number": "2025-001",    # Denormalized for display
    "dossier_title": "",                   # Denormalized for display

    "title": "Appel avec Me Tremblay",     # Short title / subject line
    "content": "...",                       # Full note content (plain text, multi-line)
    "category": "appel" | "rencontre" | "recherche" | "stratégie" | "correspondance" | "audience" | "autre",
    "pinned": False,                        # Pinned notes appear first

    # DAV
    "vjournal_uid": "uuid-v4",            # Unique UID for VJOURNAL serialization
    "dav_href": "/dav/journals/{noteId}.ics",

    # Metadata
    "created_at": datetime,
    "updated_at": datetime,
    "etag": "uuid-v4",
}
```

## Step 2 — Model Layer: `models/note.py`

```python
"""Dossier notes — timestamped journal entries linked to case files."""

import uuid
from datetime import datetime, timezone
from typing import Optional

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


def _default_doc() -> dict: ...
def _sanitize_data(data: dict) -> dict: ...
def _validate(data: dict) -> list[str]:
    """Validate note data. Title and dossier_id are required. Content is required."""
    ...


def create_note(data: dict) -> tuple[Optional[dict], list[str]]: ...
def get_note(note_id: str) -> Optional[dict]: ...
def list_notes(
    dossier_id: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    pinned_first: bool = True,
) -> list[dict]:
    """Return notes, newest first. Pinned notes appear before unpinned."""
    ...

def update_note(note_id: str, data: dict) -> tuple[Optional[dict], list[str]]: ...
def delete_note(note_id: str) -> tuple[bool, str]: ...
def toggle_pin(note_id: str) -> tuple[Optional[dict], list[str]]: ...

def get_notes_summary(dossier_id: str) -> dict:
    """Return {total, recent_5} for dashboard/tab display."""
    ...


# ── RFC-5545 VJOURNAL serialization ──────────────────────────────────

def note_to_vjournal(note: dict, parent_vjournal_uid: str = "") -> str:
    """Serialize a note to an RFC-5545 VJOURNAL string.

    The VJOURNAL includes:
    - UID: note's vjournal_uid
    - SUMMARY: note title
    - DESCRIPTION: note content
    - DTSTART: note created_at date
    - CATEGORIES: note category label
    - STATUS: FINAL
    - LAST-MODIFIED: note updated_at
    - RELATED-TO;RELTYPE=PARENT:{parent_vjournal_uid}
    - X-PALLAS-DOSSIER-ID: dossier_id
    - X-PALLAS-NOTE-CATEGORY: category key
    """
    ...

def vjournal_to_note(ical_str: str) -> dict:
    """Parse a VJOURNAL string into a note dict (for DAV PUT).

    Extracts standard properties and X-PALLAS-* custom properties.
    If RELATED-TO is present, extracts the parent dossier linkage.
    """
    ...
```

## Step 3 — Routes: `routes/notes.py`

Create a new blueprint:

```python
notes_bp = Blueprint("notes", __name__, url_prefix="/notes")

# Routes:
# GET  /notes/                           — standalone notes list (all dossiers)
# GET  /notes/?dossier_id=xxx            — notes for a specific dossier (HTMX partial)
# GET  /notes/<note_id>                  — note detail
# GET  /notes/new?dossier_id=xxx         — new note form
# POST /notes/                           — create note
# GET  /notes/<note_id>/edit             — edit form
# POST /notes/<note_id>                  — update note
# POST /notes/<note_id>/delete           — delete note
# POST /notes/<note_id>/pin              — toggle pin
```

## Step 4 — Register Blueprint

In `main.py`, add:
```python
from routes.notes import notes_bp
app.register_blueprint(notes_bp)
```

## Step 5 — Add Notes to Dossier Detail Tabs

**Option A (recommended):** Add notes as a subsection within the existing "Documents" tab, separated by a divider. This keeps the tab count manageable.

**Option B:** Add a dedicated "Notes" tab. This adds a 8th tab which may overflow on mobile.

**Go with Option A.** In the Documents tab, add a section above the file list:

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
  <div class="flex flex-col gap-2 mb-4">
    {% for n in notes[:5] %}
    <a href="{{ url_for('notes.note_detail', note_id=n.id) }}"
       class="p-3 bg-white rounded-lg border border-gray-200 hover:border-gray-300 transition-colors">
      <div class="flex items-center justify-between mb-1">
        <div class="flex items-center gap-2">
          {% if n.pinned %}
          <svg class="w-3 h-3 text-amber-500" fill="currentColor" viewBox="0 0 24 24">...</svg>
          {% endif %}
          <span class="text-sm font-medium text-gray-900 truncate">{{ n.title }}</span>
        </div>
        <span class="text-xs text-gray-400">{{ n.created_at|to_mtl ... }}</span>
      </div>
      <p class="text-xs text-gray-500 truncate">{{ n.content[:120] }}</p>
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

{# ── Files section (existing) ───────────────────────────────── #}
```

Update the dossier tab handler to load notes:
```python
if tab_name == "documents":
    # Load notes
    from models.note import list_notes, get_notes_summary
    ctx["notes"] = list_notes(dossier_id=dossier_id)
    ctx["notes_summary"] = get_notes_summary(dossier_id)
    # ... existing document loading ...
```

## Step 6 — Note Templates

Create:
- `templates/notes/list.html` — full-page note list with search, category filter
- `templates/notes/detail.html` — note detail with full content display
- `templates/notes/form.html` — create/edit form (title, category, content textarea)
- `templates/notes/_note_rows.html` — HTMX partial for filtered list

**Note form should include:**
- Title (required, text input)
- Category (select dropdown)
- Content (required, textarea with generous height — min 10 rows)
- Pin toggle (checkbox: "Épingler cette note")
- Dossier selector (pre-filled if coming from dossier context)

**Note detail should show:**
- Title, category badge, pinned indicator
- Full content (whitespace-pre-wrap for multi-line)
- Created/updated dates
- Link back to dossier
- Edit and Delete buttons

## Step 7 — DAV Integration: Notes as VJOURNAL entries

Each note becomes a separate resource in the `/dav/journals/` collection. The existing journal collection already serves dossiers as VJOURNAL entries. Notes will coexist in the same collection.

**How to distinguish notes from dossiers in the DAV collection:**
The `X-PALLAS-NOTE-CATEGORY` custom property is present on notes but not on dossier journals. When parsing a VJOURNAL PUT, check for this property to route to `note` vs `dossier` processing.

Update `dav/rfc5545.py` — Journals section:

1. **PROPFIND Depth:1** — include both dossier journals AND note journals:
```python
def journals_propfind_collection() -> Response:
    ...
    if depth == "1":
        dossiers = list_dossiers()
        for dossier in dossiers:
            _add_journal_resource_response(multistatus, dossier, body)
        # Also include notes
        from models.note import list_notes, note_to_vjournal
        notes = list_notes()
        for note in notes:
            _add_note_resource_response(multistatus, note, body)
    ...
```

2. **GET** — handle both dossier and note IDs:
```python
@rfc5545_bp.route("/dav/journals/<resource_id>.ics", methods=["GET"])
def journals_get_resource(resource_id: str) -> Response:
    # Try dossier first
    dossier = get_dossier(resource_id)
    if dossier:
        ical = dossier_to_vjournal(dossier)
        ...
    # Try note
    from models.note import get_note, note_to_vjournal
    note = get_note(resource_id)
    if note:
        parent_uid = ""
        if note.get("dossier_id"):
            parent_dossier = get_dossier(note["dossier_id"])
            parent_uid = parent_dossier.get("vjournal_uid", "") if parent_dossier else ""
        ical = note_to_vjournal(note, parent_uid)
        ...
    return Response("Not Found", status=404)
```

3. **PUT** — route based on content:
```python
@rfc5545_bp.route("/dav/journals/<resource_id>.ics", methods=["PUT"])
def journals_put_resource(resource_id: str) -> Response:
    ...
    ical_str = request.get_data(as_text=True)
    # Check if this is a note (has X-PALLAS-NOTE-CATEGORY)
    if "X-PALLAS-NOTE-CATEGORY" in ical_str:
        from models.note import vjournal_to_note, create_note, update_note, get_note
        data = vjournal_to_note(ical_str)
        existing_note = get_note(resource_id)
        if existing_note:
            updated, errors = update_note(resource_id, data)
            ...
        else:
            data["id"] = resource_id
            created, errors = create_note(data)
            ...
    else:
        # Existing dossier VJOURNAL logic
        data = vjournal_to_dossier(ical_str)
        ...
```

4. **DELETE** — try both:
```python
@rfc5545_bp.route("/dav/journals/<resource_id>.ics", methods=["DELETE"])
def journals_delete_resource(resource_id: str) -> Response:
    # Try dossier first
    existing_dossier = get_dossier(resource_id)
    if existing_dossier:
        ...  # existing logic
    # Try note
    from models.note import get_note, delete_note
    existing_note = get_note(resource_id)
    if existing_note:
        ...
    return Response("Not Found", status=404)
```

5. **REPORT handlers** (sync-collection, multiget, calendar-query) — must include notes alongside dossiers in results. Update each to fetch and include notes.

## Step 8 — Update Firestore Collection Map

```
users/{userId}/
├── ...
├── notes/{noteId}               # NEW — dossier journal notes
├── ...
```

## Step 9 — Bump DAV CTag on Note Changes

All note CRUD operations (create, update, delete) should call:
```python
from dav.sync import bump_ctag
bump_ctag("dossiers")  # Notes are in the journals/dossiers DAV collection
```

## Testing Checklist
- [ ] Can create a note linked to a dossier
- [ ] Note title, category, content, and pin status are saved correctly
- [ ] Notes appear in the Documents tab of the dossier detail
- [ ] Pinned notes appear first in the list
- [ ] Notes can be searched by title/content
- [ ] Notes can be filtered by category
- [ ] Note detail page shows full content with correct formatting
- [ ] Note edit and delete work correctly
- [ ] Pin toggle works (HTMX or redirect)
- [ ] DAV: PROPFIND Depth:1 on /dav/journals/ returns both dossier and note VJOURNALs
- [ ] DAV: GET /dav/journals/{noteId}.ics returns valid VJOURNAL with RELATED-TO
- [ ] DAV: PUT creates/updates notes when X-PALLAS-NOTE-CATEGORY is present
- [ ] DAV: DELETE removes notes
- [ ] DAV: sync-collection REPORT includes notes
- [ ] DavX5 sees note journals alongside dossier journals
- [ ] Note CTag bumps propagate correctly — DavX5 picks up new notes on sync
- [ ] Deleting a dossier does NOT automatically delete its notes (they become orphaned but accessible)
