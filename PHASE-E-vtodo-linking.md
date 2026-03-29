# PHASE E — VTODO ↔ VJOURNAL Linking via RELATED-TO

Read CLAUDE.md for project context. This phase adds RFC 5545 `RELATED-TO` properties to VTODO serialization so that tasks are formally linked to their parent dossier's VJOURNAL in the iCalendar data.

## Context

Currently, tasks store `dossier_id` and include dossier info in the VTODO `DESCRIPTION` and as `X-PALLAS-DOSSIER-ID`. However, there is no standard RFC 5545 linkage between the VTODO and the dossier's VJOURNAL. The `RELATED-TO` property (RFC 5545 §3.8.4.5) provides this linkage.

**Important limitation (acknowledged by the user):** DavX5 and tasks.org support `RELATED-TO` for task-to-task parent-child relationships (VTODO → VTODO), but they do NOT render cross-component-type relationships (VTODO → VJOURNAL) as visual parent-child in their UI. The `RELATED-TO` property will round-trip correctly (DavX5 preserves it on sync), but the visual linkage only appears in the Pallas Athena web UI.

This is a small, focused change — primarily serialization/deserialization updates.

## Step 1 — Update `task_to_vtodo` in `models/task.py`

Add the `RELATED-TO` property pointing to the dossier's VJOURNAL UID:

```python
def task_to_vtodo(task: dict) -> str:
    """Serialize a task dict to an RFC-5545 VTODO string."""
    ...
    # After existing properties, add RELATED-TO if task has a dossier_id
    if task.get("dossier_id"):
        # Look up the dossier's vjournal_uid
        from models.dossier import get_dossier
        dossier = get_dossier(task["dossier_id"])
        if dossier and dossier.get("vjournal_uid"):
            # RFC 5545 RELATED-TO with RELTYPE=PARENT
            # The icalendar library supports this:
            related = icalendar.vText(dossier["vjournal_uid"])
            todo.add("related-to", related, parameters={"RELTYPE": "PARENT"})

    ...
    cal.add_component(todo)
    return cal.to_ical().decode("utf-8")
```

**Note on the `icalendar` library:** The `icalendar` library handles `RELATED-TO` with parameters. Verify that the output looks like:
```
RELATED-TO;RELTYPE=PARENT:uuid-of-dossier-vjournal
```

If the library doesn't emit the `RELTYPE` parameter cleanly, manually append the line before `END:VTODO`:
```python
# Fallback: manual line insertion if library doesn't cooperate
vtodo_str = cal.to_ical().decode("utf-8")
if dossier_vjournal_uid:
    related_line = f"RELATED-TO;RELTYPE=PARENT:{dossier_vjournal_uid}"
    vtodo_str = vtodo_str.replace(
        "END:VTODO",
        f"{related_line}\r\nEND:VTODO"
    )
return vtodo_str
```

Test the library approach first. Only use the manual fallback if the library output is malformed.

## Step 2 — Update `vtodo_to_task` in `models/task.py`

Parse the `RELATED-TO` property on incoming VTODOs. If present and `RELTYPE=PARENT`, look up the dossier by vjournal_uid:

```python
def vtodo_to_task(ical_str: str) -> dict:
    ...
    for component in cal.walk():
        if component.name != "VTODO":
            continue

        ...  # existing property extraction

        # RELATED-TO → dossier linkage
        related_tos = component.get("related-to")
        if related_tos:
            # Can be a single value or a list
            if not isinstance(related_tos, list):
                related_tos = [related_tos]
            for rt in related_tos:
                rt_str = str(rt)
                params = getattr(rt, "params", {})
                reltype = params.get("RELTYPE", "PARENT")
                if reltype == "PARENT" and rt_str:
                    # Look up dossier by vjournal_uid
                    from models.dossier import _find_dossier_by_vjournal_uid
                    dossier = _find_dossier_by_vjournal_uid(rt_str)
                    if dossier:
                        data["dossier_id"] = dossier["id"]
                        data["dossier_file_number"] = dossier.get("file_number", "")
                        data["dossier_title"] = dossier.get("title", "")
                    break

        break  # Only process first VTODO

    return data
```

## Step 3 — Add `_find_dossier_by_vjournal_uid` to `models/dossier.py`

```python
def _find_dossier_by_vjournal_uid(vjournal_uid: str) -> Optional[dict]:
    """Find a dossier by its VJOURNAL UID. Used for RELATED-TO resolution."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("vjournal_uid", "==", vjournal_uid)
        ).limit(1)
        for doc in query.stream():
            return _migrate_parties(doc.to_dict())
    except Exception:
        pass
    return None
```

## Step 4 — Update Note VJOURNAL Serialization (Phase D integration)

If Phase D (Notes) is already implemented, also add `RELATED-TO` to note VJOURNALs. Each note's VJOURNAL should include:
```
RELATED-TO;RELTYPE=PARENT:{dossier_vjournal_uid}
```

This is already specified in Phase D's `note_to_vjournal` function, but verify it's implemented correctly with the `icalendar` library.

## Step 5 — Web UI: Display Linked Dossier on Task Detail

In `templates/tasks/detail.html`, the dossier link is already displayed. No UI change needed — the linkage is data-level (DAV serialization).

In `templates/tasks/list.html` and `_task_rows.html`, the `dossier_file_number` already shows. No change needed.

## Step 6 — Verify Round-Trip Fidelity

The key test: create a task linked to a dossier via the web UI → sync to DavX5 → verify the VTODO contains `RELATED-TO;RELTYPE=PARENT:{uid}` → modify the task in DavX5 → sync back → verify the dossier linkage is preserved.

DavX5/tasks.org should preserve `RELATED-TO` properties they don't understand — they pass them through unchanged on PUT. Verify this.

## Testing Checklist
- [ ] `task_to_vtodo` includes `RELATED-TO;RELTYPE=PARENT:{vjournal_uid}` when task has a dossier
- [ ] `task_to_vtodo` omits `RELATED-TO` when task has no dossier
- [ ] The RELATED-TO line is well-formed RFC 5545 (property;parameter:value)
- [ ] `vtodo_to_task` parses `RELATED-TO;RELTYPE=PARENT:{uid}` and resolves to correct dossier_id
- [ ] `vtodo_to_task` handles missing RELATED-TO gracefully (no crash)
- [ ] `vtodo_to_task` handles unknown RELTYPE values gracefully
- [ ] `_find_dossier_by_vjournal_uid` returns correct dossier
- [ ] `_find_dossier_by_vjournal_uid` returns None for unknown UID
- [ ] DavX5 sync: task created in web UI appears in DavX5 with RELATED-TO preserved
- [ ] DavX5 sync: task modified in DavX5 syncs back with dossier linkage intact
- [ ] DavX5 sync: task created in DavX5 without RELATED-TO → dossier_id stays None
- [ ] Existing tasks without RELATED-TO continue to work (backward compatible)
