# PHASE D3 — RELATED-TO Linking (VTODO ↔ VJOURNAL)

Read CLAUDE.md for project context. **Phases D1 and D2 must be completed before starting this phase.** Tasks and notes now live in the same per-dossier CalDAV collection. This phase adds RFC 5545 `RELATED-TO` properties so that tasks and notes can reference each other.

## Context

Since D1 and D2, each dossier's CalDAV collection contains both VTODO (tasks) and VJOURNAL (notes) resources. RFC 5545 §3.8.4.5 defines the `RELATED-TO` property for expressing relationships between calendar components. Within a single CalDAV collection, clients like jtx Board can render `RELATED-TO` with `RELTYPE=PARENT` as visual parent-child hierarchies.

This means: if a task carries `RELATED-TO;RELTYPE=PARENT:{note-vjournal-uid}`, jtx Board will display that task as a sub-item of the note. This is the correct use of the property — linking actionable items (tasks) to the context (notes) that generated them.

**Practical examples:**
- After a client meeting (note), you create follow-up tasks. Each task `RELATED-TO` → the meeting note.
- After research (note), you create tasks for filing. Each task `RELATED-TO` → the research note.
- A task may also relate to another task (parent-child task hierarchy): `RELATED-TO;RELTYPE=PARENT:{other-task-vtodo-uid}`.

## Step 1 — Add `related_note_id` to Task Schema

In `models/task.py`, update `_default_doc`:

```python
def _default_doc() -> dict:
    return {
        ...
        # Existing fields unchanged
        "vtodo_uid": "",
        "dav_href": "",
        # NEW: optional link to a parent note
        "related_note_id": None,    # ID of a note in the same dossier
        ...
    }
```

This field is optional. Most tasks won't have a related note. It's set via the web UI (when creating a task from a note's detail page) or via DAV sync (when `RELATED-TO` is present in the incoming VTODO).

## Step 2 — Update `task_to_vtodo` Serialization

In `models/task.py`, add `RELATED-TO` to the VTODO output:

```python
def task_to_vtodo(task: dict) -> str:
    ...
    # After existing custom X- properties, before cal.add_component(todo):

    # RELATED-TO: link to parent note's VJOURNAL UID
    if task.get("related_note_id"):
        from models.note import get_note
        related_note = get_note(task["related_note_id"])
        if related_note and related_note.get("vjournal_uid"):
            # Use the icalendar library to add RELATED-TO with RELTYPE param
            related_prop = icalendar.vText(related_note["vjournal_uid"])
            todo.add("related-to", related_prop, parameters={"RELTYPE": "PARENT"})

    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")
```

**Verify the output format.** The generated iCalendar should contain a line like:
```
RELATED-TO;RELTYPE=PARENT:a1b2c3d4-uuid-of-note-vjournal
```

If the `icalendar` library doesn't emit `RELTYPE` correctly, use manual line insertion as a fallback:
```python
# Fallback if library doesn't cooperate:
ical_str = cal.to_ical().decode("utf-8")
if related_note and related_note.get("vjournal_uid"):
    line = f"RELATED-TO;RELTYPE=PARENT:{related_note['vjournal_uid']}"
    ical_str = ical_str.replace("END:VTODO", f"{line}\r\nEND:VTODO")
return ical_str
```

Test the library approach first.

## Step 3 — Update `vtodo_to_task` Deserialization

In `models/task.py`, parse `RELATED-TO` from incoming VTODOs:

```python
def vtodo_to_task(ical_str: str) -> dict:
    ...
    for component in cal.walk():
        if component.name != "VTODO":
            continue

        ...  # existing property extraction

        # RELATED-TO → look for parent note link
        # The icalendar library may return RELATED-TO as a single value or list
        related_tos = component.get("related-to")
        if related_tos:
            if not isinstance(related_tos, list):
                related_tos = [related_tos]
            for rt in related_tos:
                rt_str = str(rt)
                params = getattr(rt, "params", {})
                reltype = params.get("RELTYPE", "PARENT")
                if reltype == "PARENT" and rt_str:
                    # Look up note by vjournal_uid
                    from models.note import _find_note_by_vjournal_uid
                    note = _find_note_by_vjournal_uid(rt_str)
                    if note:
                        data["related_note_id"] = note["id"]
                    break

        break

    return data
```

**Important:** If `RELATED-TO` points to an unknown UID (note doesn't exist in our system), silently ignore it. The property will still round-trip in the iCalendar data — we just don't resolve it to a Firestore relationship.

## Step 4 — Add `_find_note_by_vjournal_uid` to `models/note.py`

```python
def _find_note_by_vjournal_uid(vjournal_uid: str) -> Optional[dict]:
    """Find a note by its VJOURNAL UID. Used for RELATED-TO resolution."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("vjournal_uid", "==", vjournal_uid)
        ).limit(1)
        for doc in query.stream():
            return doc.to_dict()
    except Exception:
        pass
    return None
```

## Step 5 — Web UI: Create Task from Note

On the note detail page (`templates/notes/detail.html`), add a button:

```jinja2
<a href="{{ url_for('tasks.task_new', dossier_id=note.dossier_id, related_note_id=note.id) }}"
   class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50">
  <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/>
  </svg>
  Créer une tâche liée
</a>
```

In `routes/tasks.py`, update `task_new` to accept `related_note_id`:

```python
@tasks_bp.route("/new")
@login_required
def task_new() -> str:
    ...
    related_note_id = request.args.get("related_note_id", "")
    prefilled = ...

    if related_note_id:
        from models.note import get_note
        note = get_note(related_note_id)
        if note:
            prefilled = prefilled or {}
            prefilled["related_note_id"] = related_note_id
            prefilled["dossier_id"] = note.get("dossier_id", "")
            # Pre-fill dossier info
            ...
    ...
```

In the task form (`templates/tasks/form.html`), add a hidden field:
```html
<input type="hidden" name="related_note_id" value="{{ task.related_note_id if task else '' }}">
```

And if `related_note_id` is set, show a visual indicator:
```jinja2
{% if task and task.related_note_id %}
<div class="mb-4 p-3 bg-indigo-50 border border-indigo-200 rounded-xl text-sm text-indigo-700">
  Tâche liée à une note du dossier.
</div>
{% endif %}
```

In `routes/tasks.py`, update `_form_data` to include the field:
```python
def _form_data() -> dict:
    ...
    return {
        ...
        "related_note_id": f.get("related_note_id", "").strip() or None,
    }
```

## Step 6 — Web UI: Display Linked Tasks on Note Detail

On the note detail page, show tasks that reference this note:

```python
# In routes/notes.py, note_detail handler:
from models.task import list_tasks

# Find tasks linked to this note
all_dossier_tasks = list_tasks(dossier_id=note["dossier_id"])
linked_tasks = [t for t in all_dossier_tasks if t.get("related_note_id") == note_id]
ctx["linked_tasks"] = linked_tasks
```

```jinja2
{# In templates/notes/detail.html #}
{% if linked_tasks %}
<div class="bg-white rounded-xl border border-gray-200 p-5 mt-4">
  <h2 class="text-sm font-semibold text-gray-900 mb-3">Tâches liées</h2>
  <div class="flex flex-col gap-2">
    {% for t in linked_tasks %}
    <a href="{{ url_for('tasks.task_detail', task_id=t.id) }}"
       class="flex items-center gap-2 p-2 rounded-lg hover:bg-gray-50">
      {% if t.status == 'terminée' %}
      <div class="w-4 h-4 rounded border-2 border-green-500 bg-green-500 flex-shrink-0 flex items-center justify-center">
        <svg class="w-2.5 h-2.5 text-white" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>
      </div>
      {% else %}
      <div class="w-4 h-4 rounded border-2 border-gray-300 flex-shrink-0"></div>
      {% endif %}
      <span class="text-sm {{ 'text-gray-500 line-through' if t.status == 'terminée' else 'text-gray-900' }}">{{ t.title }}</span>
    </a>
    {% endfor %}
  </div>
</div>
{% endif %}
```

## Step 7 — Display Related Note on Task Detail

On the task detail page (`templates/tasks/detail.html`), if the task has a `related_note_id`, show a link:

```python
# In routes/tasks.py, task_detail handler:
related_note = None
if task.get("related_note_id"):
    from models.note import get_note
    related_note = get_note(task["related_note_id"])
ctx["related_note"] = related_note
```

```jinja2
{# In templates/tasks/detail.html, within the info cards #}
{% if related_note %}
<div class="bg-white rounded-xl border border-gray-200 p-5">
  <h2 class="text-sm font-semibold text-gray-900 mb-2">Note liée</h2>
  <a href="{{ url_for('notes.note_detail', note_id=related_note.id) }}"
     class="flex items-center gap-2 text-sm text-indigo-600 hover:text-indigo-700">
    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"/>
    </svg>
    {{ related_note.title }}
  </a>
</div>
{% endif %}
```

## Testing Checklist
- [ ] `task_to_vtodo` includes `RELATED-TO;RELTYPE=PARENT:{uid}` when `related_note_id` is set
- [ ] `task_to_vtodo` omits `RELATED-TO` when `related_note_id` is None
- [ ] RELATED-TO line is well-formed RFC 5545 (property;parameter:value)
- [ ] `vtodo_to_task` parses `RELATED-TO;RELTYPE=PARENT:{uid}` and resolves to correct note
- [ ] `vtodo_to_task` handles missing RELATED-TO gracefully
- [ ] `vtodo_to_task` handles RELATED-TO pointing to unknown UID gracefully
- [ ] `_find_note_by_vjournal_uid` returns correct note
- [ ] `_find_note_by_vjournal_uid` returns None for unknown UID
- [ ] Web UI: "Créer une tâche liée" from note detail pre-fills dossier and sets related_note_id
- [ ] Web UI: task detail shows "Note liée" with clickable link
- [ ] Web UI: note detail shows "Tâches liées" with linked task list
- [ ] DavX5/jtx Board: task with RELATED-TO appears as child of the note
- [ ] DavX5/jtx Board: modifying a task preserves the RELATED-TO property on sync
- [ ] Round-trip: create task with RELATED-TO in web UI → sync to DavX5 → modify in DavX5 → sync back → relationship preserved
- [ ] Tasks without related_note_id are unaffected (backward compatible)
