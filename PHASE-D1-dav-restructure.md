# PHASE D1 — DAV Collection Restructuring

Read CLAUDE.md for project context. This phase restructures the DAV layer so that each active dossier becomes its own CalDAV collection containing VTODO (tasks) and eventually VJOURNAL (notes, added in D2). The `/dav/journals/` collection is removed.

## Why This Change

The current `/dav/journals/` collection serializes dossiers as VJOURNAL entries. This is a misuse of VJOURNAL — a dossier is a container for notes and tasks, not a journal entry. Under RFC 5545, VJOURNAL entries are timestamped records of something that happened (meeting notes, research findings, call summaries). The dossier itself maps to a CalDAV *collection* — a named container that groups related VTODO and VJOURNAL resources.

## New URL Structure

```
/dav/                                    # Principal + calendar-home-set (PROPFIND discovery)
├── addressbook/                         # CardDAV (UNCHANGED)
├── calendar/                            # CalDAV VEVENT hearings (UNCHANGED)
├── tasks/                               # CalDAV VTODO — standalone tasks only (no dossier_id)
├── dossier-{dossier1Id}/               # CalDAV collection: VTODO + VJOURNAL
│   ├── {task1Id}.ics                   # VTODO for a task linked to this dossier
│   └── {task2Id}.ics                   # VTODO for another task
├── dossier-{dossier2Id}/               # Another dossier's collection
│   └── ...
└── ...
```

Key decisions:
- Per-dossier collections are **direct children of `/dav/`** with a `dossier-` prefix. This is required because DavX5 discovers collections via PROPFIND Depth:1 on the calendar-home-set. Nested URLs (e.g., `/dav/dossiers/{id}/`) would be invisible at Depth:1.
- Each dossier collection advertises `supported-calendar-component-set` with both VTODO and VJOURNAL. In this phase only VTODO is served. D2 adds VJOURNAL (notes).
- Only dossiers with status `actif` or `en_attente` are exposed as DAV collections. Closed/archived dossiers are omitted from discovery — DavX5 stops syncing them. Reopening a dossier makes it reappear.
- The `dossier-` prefix prevents URL collision with other DAV paths.

## What Changes

| Component | Before | After |
|-----------|--------|-------|
| `/dav/journals/` | Dossiers as VJOURNAL entries | **Removed entirely** |
| `/dav/tasks/` | All tasks (with and without dossier) | Standalone tasks only (`dossier_id is None`) |
| `/dav/dossier-{id}/` | Does not exist | New: per-dossier CalDAV collection with VTODO |
| Root PROPFIND Depth:1 | 4 static children | Dynamic: calendar + tasks + N dossier collections |
| CTag system | Per-type (`tasks`, `dossiers`) | Per-dossier (`dossier:{id}`) + standalone `tasks` |
| Task `dav_href` field | Always `/dav/tasks/{id}.ics` | Computed dynamically; stored field becomes stale (ignore it) |

## What Does NOT Change

- `/dav/addressbook/` — completely unchanged
- `/dav/calendar/` — completely unchanged (hearings stay in the shared calendar)
- Web UI — no template or route changes
- Firestore schemas for tasks, dossiers — no field additions or removals
- Firebase Security Rules, App Check, authentication

## Step 1 — Create `dav/dossier_collections.py`

This is a new Flask blueprint handling all DAV operations on per-dossier collections.

```python
"""Per-dossier CalDAV collections — VTODO tasks (and VJOURNAL notes in D2).

Each active dossier is exposed as a CalDAV collection at:
    /dav/dossier-{dossierId}/

Resources within the collection:
    /dav/dossier-{dossierId}/{resourceId}.ics

This phase serves VTODO resources only. VJOURNAL support is added in Phase D2.
"""

from flask import Blueprint

dossier_dav_bp = Blueprint("dossier_dav", __name__)
```

### URL Registration Pattern

All routes use the pattern `/dav/dossier-<dossier_id>/` for the collection
and `/dav/dossier-<dossier_id>/<resource_id>.ics` for individual resources.

### Endpoints to Implement

```python
# ── OPTIONS ──────────────────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["OPTIONS"])
@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["OPTIONS"])
@dav_auth_required
def options(dossier_id: str, resource_id: str = None) -> Response:
    resp = Response("", status=200)
    resp.headers["Allow"] = "OPTIONS, GET, PUT, DELETE, PROPFIND, REPORT"
    resp.headers["DAV"] = "1, 2, 3, calendar-access"
    return resp


# ── PROPFIND (Collection) ────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["PROPFIND"])
@dav_auth_required
def propfind_collection(dossier_id: str) -> Response:
    """PROPFIND on a dossier collection.

    Depth:0 → return collection properties only.
    Depth:1 → return collection properties + all resources in the collection.
    """
    # Verify dossier exists and is active
    dossier = get_dossier(dossier_id)
    if not dossier or dossier.get("status") not in ("actif", "en_attente"):
        return Response("Not Found", status=404)

    depth = request.headers.get("Depth", "0")
    body = parse_propfind_body(request.get_data())
    multistatus = make_multistatus()

    # Collection response
    _add_collection_props(multistatus, dossier, body)

    if depth == "1":
        # List all tasks linked to this dossier
        tasks = list_tasks(dossier_id=dossier_id)
        for task in tasks:
            _add_task_resource(multistatus, dossier_id, task, body)

        # In D2, also list notes here:
        # notes = list_notes(dossier_id=dossier_id)
        # for note in notes:
        #     _add_note_resource(multistatus, dossier_id, note, body)

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _add_collection_props(multistatus, dossier, body):
    """Add the dossier collection's own <D:response>."""
    href = f"/dav/dossier-{dossier['id']}/"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        rt = ET.SubElement(prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, caldav_tag("calendar"))

    if propfind_requests_prop(body, dav_tag("displayname")):
        display = f"{dossier.get('file_number', '')} — {dossier.get('title', '')}"
        ET.SubElement(prop, dav_tag("displayname")).text = display

    if propfind_requests_prop(body, cs_tag("getctag")):
        ET.SubElement(prop, cs_tag("getctag")).text = get_ctag(f"dossier:{dossier['id']}")

    if propfind_requests_prop(body, dav_tag("sync-token")):
        ET.SubElement(prop, dav_tag("sync-token")).text = (
            f"data:,{get_sync_token(f'dossier:{dossier[\"id\"]}')}"
        )

    if propfind_requests_prop(body, caldav_tag("supported-calendar-component-set")):
        sccs = ET.SubElement(prop, caldav_tag("supported-calendar-component-set"))
        # Advertise both VTODO and VJOURNAL from the start
        comp_todo = ET.SubElement(sccs, caldav_tag("comp"))
        comp_todo.set("name", "VTODO")
        comp_journal = ET.SubElement(sccs, caldav_tag("comp"))
        comp_journal.set("name", "VJOURNAL")

    if propfind_requests_prop(body, dav_tag("supported-report-set")):
        srs = ET.SubElement(prop, dav_tag("supported-report-set"))
        sr = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr, dav_tag("report")).append(
            ET.Element(dav_tag("sync-collection"))
        )
        sr2 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr2, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-multiget"))
        )
        sr3 = ET.SubElement(srs, dav_tag("supported-report"))
        ET.SubElement(sr3, dav_tag("report")).append(
            ET.Element(caldav_tag("calendar-query"))
        )


def _add_task_resource(multistatus, dossier_id, task, body):
    """Add a single VTODO resource <D:response>."""
    href = f"/dav/dossier-{dossier_id}/{task['id']}.ics"
    resp = add_response(multistatus, href)
    prop = add_propstat(resp)

    if propfind_requests_prop(body, dav_tag("getetag")):
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'

    if propfind_requests_prop(body, dav_tag("getcontenttype")):
        ET.SubElement(prop, dav_tag("getcontenttype")).text = "text/calendar; charset=utf-8"

    if propfind_requests_prop(body, dav_tag("resourcetype")):
        ET.SubElement(prop, dav_tag("resourcetype"))  # Empty for non-collection

    if body is not None and propfind_requests_prop(body, caldav_tag("calendar-data")):
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)


# ── PROPFIND (Resource) ──────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PROPFIND"])
@dav_auth_required
def propfind_resource(dossier_id: str, resource_id: str) -> Response:
    """PROPFIND on a single resource within a dossier collection."""
    # Try task first
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        body = parse_propfind_body(request.get_data())
        multistatus = make_multistatus()
        _add_task_resource(multistatus, dossier_id, task, body)
        xml = serialize_multistatus(multistatus)
        return Response(xml, status=207, content_type="application/xml; charset=utf-8")

    # D2: try note here
    # note = get_note(resource_id)
    # if note and note.get("dossier_id") == dossier_id: ...

    return Response("Not Found", status=404)


# ── REPORT ───────────────────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/", methods=["REPORT"])
@dav_auth_required
def report_collection(dossier_id: str) -> Response:
    """Handle REPORT requests on a dossier collection.

    Supported reports: sync-collection, calendar-multiget, calendar-query.
    """
    dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)

    body_root = parse_report_body(request.get_data())
    if body_root is None:
        return Response("Bad Request", status=400)

    local = body_root.tag.split("}")[-1] if "}" in body_root.tag else body_root.tag

    if local == "sync-collection":
        return _handle_sync_collection(dossier_id, body_root)
    elif local == "calendar-multiget":
        return _handle_multiget(dossier_id, body_root)
    elif local == "calendar-query":
        return _handle_calendar_query(dossier_id, body_root)

    return Response("Report type not supported", status=501)


def _handle_sync_collection(dossier_id, body_root):
    """sync-collection REPORT — return all resources + tombstones."""
    sync_name = f"dossier:{dossier_id}"
    token_el = body_root.find(dav_tag("sync-token"))
    client_token = ""
    if token_el is not None and token_el.text:
        client_token = token_el.text.replace("data:,", "")

    multistatus = make_multistatus()
    current_token = get_sync_token(sync_name)

    if not client_token or client_token != current_token:
        # Full sync: return all tasks (and notes in D2) for this dossier
        tasks = list_tasks(dossier_id=dossier_id)
        for task in tasks:
            resp = add_response(multistatus, f"/dav/dossier-{dossier_id}/{task['id']}.ics")
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'

        # D2: also include notes
        # notes = list_notes(dossier_id=dossier_id)
        # for note in notes: ...

        # Tombstones
        tombstones = get_tombstones(sync_name)
        for ts in tombstones:
            add_status_response(
                multistatus,
                f"/dav/dossier-{dossier_id}/{ts['id']}.ics",
                404, "Not Found",
            )

    ET.SubElement(multistatus, dav_tag("sync-token")).text = f"data:,{current_token}"
    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_multiget(dossier_id, body_root):
    """calendar-multiget REPORT — return specific resources by href."""
    multistatus = make_multistatus()

    for href_el in body_root.findall(dav_tag("href")):
        href = href_el.text or ""
        resource_id = _extract_resource_id(href)
        if not resource_id:
            add_status_response(multistatus, href, 404, "Not Found")
            continue

        # Try task
        task = get_task(resource_id)
        if task and task.get("dossier_id") == dossier_id:
            resp = add_response(multistatus, href)
            prop = add_propstat(resp)
            ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'
            ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)
            continue

        # D2: try note
        # note = get_note(resource_id)
        # ...

        add_status_response(multistatus, href, 404, "Not Found")

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


def _handle_calendar_query(dossier_id, body_root):
    """calendar-query REPORT — return all matching resources."""
    multistatus = make_multistatus()

    tasks = list_tasks(dossier_id=dossier_id)
    for task in tasks:
        href = f"/dav/dossier-{dossier_id}/{task['id']}.ics"
        resp = add_response(multistatus, href)
        prop = add_propstat(resp)
        ET.SubElement(prop, dav_tag("getetag")).text = f'"{task.get("etag", "")}"'
        ET.SubElement(prop, caldav_tag("calendar-data")).text = task_to_vtodo(task)

    # D2: also include notes

    xml = serialize_multistatus(multistatus)
    return Response(xml, status=207, content_type="application/xml; charset=utf-8")


# ── GET ──────────────────────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["GET"])
@dav_auth_required
def get_resource(dossier_id: str, resource_id: str) -> Response:
    # Try task
    task = get_task(resource_id)
    if task and task.get("dossier_id") == dossier_id:
        ical = task_to_vtodo(task)
        resp = Response(ical, status=200, content_type="text/calendar; charset=utf-8")
        resp.headers["ETag"] = f'"{task.get("etag", "")}"'
        return resp

    # D2: try note

    return Response("Not Found", status=404)


# ── PUT ──────────────────────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["PUT"])
@dav_auth_required
def put_resource(dossier_id: str, resource_id: str) -> Response:
    """Create or update a resource in a dossier collection.

    Parses the iCalendar body to determine component type (VTODO or VJOURNAL).
    """
    # Verify dossier exists
    dossier = get_dossier(dossier_id)
    if not dossier:
        return Response("Not Found", status=404)

    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")

    ical_str = request.get_data(as_text=True)
    if not ical_str:
        return Response("Bad Request", status=400)

    # Determine component type by parsing
    component_type = _detect_component_type(ical_str)

    if component_type == "VTODO":
        return _put_task(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match)
    elif component_type == "VJOURNAL":
        # D2 will handle this
        return Response("VJOURNAL support coming soon", status=501)
    else:
        return Response("Unsupported component type", status=400)


def _put_task(dossier_id, dossier, resource_id, ical_str, if_match, if_none_match):
    """Handle PUT for a VTODO resource."""
    existing = get_task(resource_id)

    # Precondition checks
    if if_none_match == "*" and existing:
        return Response("Precondition Failed", status=412)
    if if_match and existing:
        if if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)
    if if_match and not existing:
        return Response("Precondition Failed", status=412)

    try:
        data = vtodo_to_task(ical_str)
    except Exception:
        return Response("Bad Request — invalid iCalendar", status=400)

    # Force the dossier_id from the URL (the collection implies the dossier)
    data["dossier_id"] = dossier_id
    data["dossier_file_number"] = dossier.get("file_number", "")
    data["dossier_title"] = dossier.get("title", "")

    sync_name = f"dossier:{dossier_id}"

    if existing:
        # If task was previously in a different dossier, record tombstone there
        old_dossier = existing.get("dossier_id")
        if old_dossier and old_dossier != dossier_id:
            record_tombstone(f"dossier:{old_dossier}", resource_id)
            bump_ctag(f"dossier:{old_dossier}")

        updated, errors = update_task(resource_id, data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=204)
        resp.headers["ETag"] = f'"{updated.get("etag", "")}"'
    else:
        data["id"] = resource_id
        created, errors = create_task(data)
        if errors:
            return Response("\n".join(errors), status=422)
        bump_ctag(sync_name)
        resp = Response("", status=201)
        resp.headers["ETag"] = f'"{created.get("etag", "")}"'

    if "return=minimal" in request.headers.get("Prefer", ""):
        resp.headers["Preference-Applied"] = "return=minimal"

    return resp


# ── DELETE ───────────────────────────────────────────────────────

@dossier_dav_bp.route("/dav/dossier-<dossier_id>/<resource_id>.ics", methods=["DELETE"])
@dav_auth_required
def delete_resource(dossier_id: str, resource_id: str) -> Response:
    # Try task
    existing = get_task(resource_id)
    if existing and existing.get("dossier_id") == dossier_id:
        if_match = request.headers.get("If-Match")
        if if_match and if_match != f'"{existing.get("etag", "")}"':
            return Response("Precondition Failed", status=412)

        success, error = delete_task(resource_id)
        if not success:
            return Response(error, status=500)

        sync_name = f"dossier:{dossier_id}"
        record_tombstone(sync_name, resource_id)
        bump_ctag(sync_name)
        return Response("", status=204)

    # D2: try note

    return Response("Not Found", status=404)


# ── Helpers ──────────────────────────────────────────────────────

def _detect_component_type(ical_str: str) -> str | None:
    """Detect whether the iCalendar body contains VTODO or VJOURNAL."""
    # Simple string scan — faster than full parsing for routing
    if "BEGIN:VTODO" in ical_str:
        return "VTODO"
    elif "BEGIN:VJOURNAL" in ical_str:
        return "VJOURNAL"
    return None


def _extract_resource_id(href: str) -> str | None:
    """Extract the resource ID from a dossier collection href."""
    href = href.rstrip("/")
    if not href.endswith(".ics"):
        return None
    segment = href.rsplit("/", 1)[-1]
    return segment.replace(".ics", "")
```

## Step 2 — Modify `dav/__init__.py` (Root PROPFIND)

The root PROPFIND at Depth:1 must now dynamically list per-dossier collections alongside the static collections.

**Changes to the Depth:1 section:**

Replace the static `collections` list with dynamic enumeration:

```python
if depth == "1":
    from dav.xml_utils import carddav_tag
    from dav.sync import get_ctag
    from models.dossier import list_dossiers

    # ── Static collections (unchanged) ────────────────────────
    static_collections = [
        ("/dav/addressbook/", "Clients", "addressbook", None),
        ("/dav/calendar/", "Audiences", "calendar", "VEVENT"),
        ("/dav/tasks/", "Tâches (sans dossier)", "calendar", "VTODO"),
    ]
    # NOTE: /dav/journals/ is REMOVED — no longer listed

    ctag_names = {
        "/dav/addressbook/": "parties",
        "/dav/calendar/": "hearings",
        "/dav/tasks/": "tasks",
    }

    for coll_path, coll_name, coll_type, component in static_collections:
        # ... existing logic for building child responses (unchanged)
        ...

    # ── Dynamic per-dossier collections ───────────────────────
    active_dossiers = list_dossiers(status_filter="actif") + list_dossiers(status_filter="en_attente")
    # Deduplicate (in case a dossier appears in both queries — shouldn't happen but be safe)
    seen_ids = set()
    for dossier in active_dossiers:
        if dossier["id"] in seen_ids:
            continue
        seen_ids.add(dossier["id"])

        coll_path = f"/dav/dossier-{dossier['id']}/"
        display_name = f"Pallas Athena — {dossier.get('file_number', '')} — {dossier.get('title', '')}"
        sync_name = f"dossier:{dossier['id']}"

        child = add_response(multistatus, coll_path)
        child_prop = add_propstat(child)

        # resourcetype: collection + calendar
        rt = ET.SubElement(child_prop, dav_tag("resourcetype"))
        ET.SubElement(rt, dav_tag("collection"))
        ET.SubElement(rt, caldav_tag("calendar"))

        # displayname
        ET.SubElement(child_prop, dav_tag("displayname")).text = display_name

        # supported-calendar-component-set: VTODO + VJOURNAL
        sccs = ET.SubElement(child_prop, caldav_tag("supported-calendar-component-set"))
        comp_todo = ET.SubElement(sccs, caldav_tag("comp"))
        comp_todo.set("name", "VTODO")
        comp_journal = ET.SubElement(sccs, caldav_tag("comp"))
        comp_journal.set("name", "VJOURNAL")

        # getctag
        ET.SubElement(child_prop, cs_tag("getctag")).text = get_ctag(sync_name)
```

**Also update the calendar-home-set response:** remove the reference to `/dav/journals/` if it's being advertised anywhere.

## Step 3 — Modify `dav/rfc5545.py` (Standalone Tasks Only)

The VTODO section of `rfc5545.py` currently serves ALL tasks. Modify it to serve **only standalone tasks** (where `dossier_id is None`).

**Changes to the tasks PROPFIND handler:**

```python
# In tasks_propfind_collection, at Depth:1:
if depth == "1":
    # CHANGED: only list tasks without a dossier
    tasks = list_tasks()
    standalone_tasks = [t for t in tasks if not t.get("dossier_id")]
    for task in standalone_tasks:
        _add_task_resource_response(multistatus, task, body)
```

**Apply the same filter to all task REPORT handlers:**

- `_tasks_sync_collection`: filter `list_tasks()` to standalone only
- `_tasks_calendar_query`: filter `list_tasks()` to standalone only
- `_tasks_multiget`: no filter needed (it fetches by specific ID, so the task's dossier_id doesn't matter for multiget)

**For the standalone tasks CTag:** continue using `bump_ctag("tasks")` for standalone tasks. Per-dossier tasks use `bump_ctag(f"dossier:{dossier_id}")`.

**Remove the entire VJOURNAL / Journals section.** Delete everything from the `# VJOURNAL — Dossiers` header through the end of the journals handlers:
- `journals_options`
- `journals_propfind_collection`
- `journals_propfind_resource`
- `_add_journals_collection_response`
- `_add_journal_resource_response`
- `journals_report_collection`
- `_journals_sync_collection`
- `_journals_multiget`
- `_journals_calendar_query`
- `journals_get_resource`
- `journals_put_resource`
- `journals_delete_resource`

Also remove the imports of dossier model functions that were only used by the journal handlers:
```python
# REMOVE these imports:
from models.dossier import (
    create_dossier,
    delete_dossier,
    dossier_to_vjournal,
    get_dossier,
    list_dossiers,
    update_dossier,
    vjournal_to_dossier,
)
```

## Step 4 — Update CTag Bumping Across the Application

When a task with a `dossier_id` is created, updated, or deleted via the **web UI**, the per-dossier CTag must be bumped so DavX5 picks up the change.

**In `routes/tasks.py`:**

Currently the routes call `bump_ctag("tasks")` after task CRUD. Update this logic:

```python
# After creating/updating/deleting a task:
from dav.sync import bump_ctag

if task.get("dossier_id"):
    bump_ctag(f"dossier:{task['dossier_id']}")
else:
    bump_ctag("tasks")  # Standalone tasks collection
```

Apply this change in:
- `task_create`
- `task_update`
- `task_delete`
- `task_toggle`

**In `models/protocol.py`:**

The `_auto_create_tasks_for_steps` function creates dossier-linked tasks. After creating each task, bump the dossier CTag:
```python
if task and protocol.get("dossier_id"):
    bump_ctag(f"dossier:{protocol['dossier_id']}")
```

**In `dav/dossier_collections.py`:**

The PUT and DELETE handlers already call `bump_ctag(f"dossier:{dossier_id}")`. No additional change needed.

## Step 5 — Register the New Blueprint

In `main.py`:

```python
# Add after existing DAV blueprint registrations:
from dav.dossier_collections import dossier_dav_bp
app.register_blueprint(dossier_dav_bp)
csrf.exempt(dossier_dav_bp)
```

## Step 6 — Update `dav/sync.py` for Per-Dossier Sync

No changes to the sync module itself. The existing `get_ctag`, `bump_ctag`, `record_tombstone`, and `get_tombstones` functions all take a `collection_name` string parameter. Passing `f"dossier:{dossier_id}"` works directly — it creates a Firestore document at `dav_sync/dossier:{dossierId}` with its own ctag, sync_token, and tombstones subcollection. The colon in the document ID is valid in Firestore.

## Step 7 — Handle Task Dossier Reassignment

When a task is moved from one dossier to another via the web UI (`update_task` with a changed `dossier_id`), both the old and new dossier CTags must be bumped:

**In `routes/tasks.py`, in `task_update`:**

```python
# After successful update:
old_dossier_id = existing_task.get("dossier_id")  # before update
new_dossier_id = updated_task.get("dossier_id")   # after update

if old_dossier_id != new_dossier_id:
    # Task moved between dossiers (or to/from standalone)
    if old_dossier_id:
        record_tombstone(f"dossier:{old_dossier_id}", task_id)
        bump_ctag(f"dossier:{old_dossier_id}")
    else:
        record_tombstone("tasks", task_id)
        bump_ctag("tasks")

    if new_dossier_id:
        bump_ctag(f"dossier:{new_dossier_id}")
    else:
        bump_ctag("tasks")
elif new_dossier_id:
    bump_ctag(f"dossier:{new_dossier_id}")
else:
    bump_ctag("tasks")
```

## Step 8 — Clean Up Dossier Model DAV Fields

The `dossier_to_vjournal` and `vjournal_to_dossier` functions in `models/dossier.py` are no longer called from any DAV handler. Leave them in place for now — they're harmless and might be useful for future export. Do NOT delete them.

The `vjournal_uid` and `dav_href` fields on dossiers are now unused by the DAV layer (the dossier itself is a collection, not a resource). Leave them in the schema — removing fields from existing Firestore documents requires a migration, which isn't worth the effort for unused fields.

## Migration Note for DavX5 Users

After deploying this phase, the DAV collection structure changes fundamentally. DavX5 must be reconfigured:

1. Remove the existing Pallas Athena account from DavX5 (Settings → Accounts → Pallas Athena → Remove)
2. Re-add the account (same URL, same credentials)
3. DavX5 will discover the new collection structure: shared calendar, standalone tasks, plus one collection per active dossier
4. Select which collections to sync

The old `/dav/journals/` data (dossier metadata as VJOURNAL) will no longer appear — this is expected and desired.

## Testing Checklist
- [ ] PROPFIND Depth:0 on `/dav/` returns principal properties (unchanged)
- [ ] PROPFIND Depth:1 on `/dav/` lists: addressbook, calendar, tasks, and one entry per active dossier
- [ ] Closed/archived dossiers are NOT listed in Depth:1
- [ ] PROPFIND Depth:1 on `/dav/` does NOT list `/dav/journals/`
- [ ] PROPFIND Depth:0 on `/dav/dossier-{id}/` returns collection properties with correct displayname
- [ ] PROPFIND Depth:1 on `/dav/dossier-{id}/` lists all tasks linked to that dossier
- [ ] `/dav/tasks/` PROPFIND Depth:1 lists ONLY standalone tasks (no dossier_id)
- [ ] GET on `/dav/dossier-{id}/{taskId}.ics` returns valid VTODO
- [ ] PUT VTODO to `/dav/dossier-{id}/{taskId}.ics` creates/updates task with correct dossier_id
- [ ] DELETE on `/dav/dossier-{id}/{taskId}.ics` removes task and records tombstone
- [ ] ETag/If-Match preconditions work on dossier collection resources
- [ ] sync-collection REPORT on dossier collection returns tasks + tombstones
- [ ] Per-dossier CTag bumps when a dossier's task is created/updated/deleted via web UI
- [ ] Standalone tasks CTag bumps only for tasks without dossier_id
- [ ] Moving a task between dossiers: tombstone in old, bump both CTags
- [ ] `/dav/journals/` endpoints return 404 (removed)
- [ ] DavX5: remove account, re-add → discovers new collection structure
- [ ] DavX5: dossier collections appear with readable names ("2025-001 — Tremblay c. Lavoie")
- [ ] DavX5: tasks in a dossier collection sync correctly
- [ ] DavX5: standalone tasks sync correctly
- [ ] DavX5: hearings in `/dav/calendar/` are unaffected
- [ ] DavX5: contacts in `/dav/addressbook/` are unaffected
- [ ] Web UI: all task and dossier pages work identically (no regression)
