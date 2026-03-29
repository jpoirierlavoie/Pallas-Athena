# PHASE C — Multiple Protocols + Bidirectional Task Sync

Read CLAUDE.md for project context. This phase modifies the protocol module to allow multiple sequential protocols per dossier, and fixes the bidirectional synchronization between protocol steps and their linked tasks.

## Context

**Current behavior (broken):** The `create_protocol` function rejects creation if the dossier already has an active protocol. This means once a protocol is created, the dossier can never have another — even if the first protocol is completed or suspended.

**Desired behavior:** A dossier may have multiple protocols over its lifetime, but only one can be `actif` at any time. A completed or suspended protocol is archived and remains viewable. Creating a new protocol requires that no existing protocol has status `actif`.

**Current sync gap:** When a protocol step is completed via `complete_step()`, it calls `_sync_task_status()` to update the linked task. However, when a task is completed via `toggle_task_complete()` or `update_task()`, the linked protocol step is NOT updated. This is a one-way sync that causes confusion.

## Step 1 — Modify Protocol Creation Logic

In `models/protocol.py`, update `create_protocol`:

```python
# CURRENT (too restrictive):
existing = get_protocol_for_dossier(dossier_id)
if existing and existing.get("status") == "actif":
    return None, ["Ce dossier a déjà un protocole actif."]

# NEW (allows creation when no active protocol exists):
active_protocols = _get_active_protocols(dossier_id)
if active_protocols:
    return None, ["Ce dossier a déjà un protocole actif. Complétez ou suspendez le protocole existant avant d'en créer un nouveau."]
```

Add helper:
```python
def _get_active_protocols(dossier_id: str) -> list[dict]:
    """Return all protocols with status 'actif' for a dossier."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        ).where(
            filter=FieldFilter("status", "==", "actif")
        )
        return [doc.to_dict() for doc in query.stream()]
    except Exception:
        return []
```

## Step 2 — Update `get_protocol_for_dossier`

The current function returns the active protocol, falling back to the most recent. Update it to clearly separate "active" from "historical":

```python
def get_protocol_for_dossier(dossier_id: str, active_only: bool = True) -> Optional[dict]:
    """Return the active protocol for a dossier.

    If active_only is True (default), returns only the 'actif' protocol.
    If active_only is False, returns the most recent protocol regardless of status.
    """
    ...
```

Add a new function:
```python
def list_protocols_for_dossier(dossier_id: str) -> list[dict]:
    """Return all protocols for a dossier, newest first. Steps are NOT loaded."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        results = [doc.to_dict() for doc in query.stream()]
        results.sort(
            key=lambda p: p.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return results
    except Exception:
        return []
```

## Step 3 — Update Protocol Tab in Dossier Detail

In `routes/dossiers.py`, the `dossier_tab` handler for `tab_name == "protocole"`:

```python
if tab_name == "protocole":
    # Active protocol (if any)
    active_protocol = get_protocol_for_dossier(dossier_id, active_only=True)
    if active_protocol:
        check_overdue_steps(active_protocol["id"])
        active_protocol = get_protocol(active_protocol["id"])

    # Historical protocols (completed/suspended)
    all_protocols = list_protocols_for_dossier(dossier_id)
    historical_protocols = [
        p for p in all_protocols
        if p.get("status") in ("complété", "suspendu")
    ]

    ctx["protocol"] = active_protocol
    ctx["historical_protocols"] = historical_protocols
    ctx["protocol_summary"] = get_protocol_summary(dossier_id)
    ...
```

## Step 4 — Update Protocol Tab Template

In `templates/dossiers/_tab_protocole.html`, add a section below the active protocol display:

```jinja2
{# ── Historical protocols (collapsed) ──────────────────────── #}
{% if historical_protocols %}
<div class="mt-6" x-data="{ showHistory: false }">
  <button @click="showHistory = !showHistory"
          class="flex items-center gap-2 text-sm font-medium text-gray-500 hover:text-gray-700">
    <svg :class="showHistory ? 'rotate-90' : ''" class="w-4 h-4 transition-transform" ...>
    Protocoles antérieurs ({{ historical_protocols|length }})
  </button>
  <div x-show="showHistory" x-cloak class="mt-3 space-y-2">
    {% for hp in historical_protocols %}
    <a href="{{ url_for('protocols.protocol_detail', protocol_id=hp.id) }}"
       class="block p-3 bg-gray-50 rounded-lg border border-gray-200 hover:border-gray-300 opacity-80">
      <div class="flex items-center justify-between">
        <div>
          <span class="text-xs font-medium px-2 py-0.5 rounded-full {{ protocol_type_colors.get(hp.protocol_type, '') }}">
            {{ protocol_type_short_labels.get(hp.protocol_type, hp.protocol_type) }}
          </span>
          <span class="text-sm text-gray-700 ml-2">{{ hp.title }}</span>
        </div>
        <span class="text-xs text-gray-400">
          {{ 'Complété' if hp.status == 'complété' else 'Suspendu' }}
        </span>
      </div>
    </a>
    {% endfor %}
  </div>
</div>
{% endif %}

{# ── "Create new protocol" button (only when no active protocol) ── #}
{% if not protocol %}
  {# ... existing creation prompt ... #}
{% endif %}
```

The "Créer un protocole" button should now appear whenever there's no active protocol — even if historical protocols exist.

## Step 5 — Bidirectional Task-Protocol Sync

This is the critical fix. Currently:
- Step → Task sync: `complete_step()` calls `_sync_task_status()` ✓
- Task → Step sync: `toggle_task_complete()` and `update_task()` do NOT sync back ✗

**Add reverse sync in `models/task.py`:**

After any status change in `update_task` or `toggle_task_complete`, check if the task is linked to a protocol step and sync:

```python
def _sync_protocol_step(task_id: str, new_task_status: str) -> None:
    """Sync a protocol step when its linked task status changes."""
    try:
        from models.protocol import COLLECTION as PROTO_COLLECTION
        from models.protocol import STEPS_SUBCOLLECTION

        # Find the protocol step that links to this task
        # We need to search across all protocols' steps subcollections.
        # Since this is a single-user app, the dataset is small — iterate protocols.
        protocols = db.collection(PROTO_COLLECTION).stream()
        for proto_doc in protocols:
            proto = proto_doc.to_dict()
            if proto.get("status") != "actif":
                continue
            steps_ref = db.collection(PROTO_COLLECTION).document(
                proto_doc.id
            ).collection(STEPS_SUBCOLLECTION)
            for step_doc in steps_ref.stream():
                step = step_doc.to_dict()
                if step.get("linked_task_id") == task_id:
                    # Found the linked step — sync status
                    now = datetime.now(timezone.utc)
                    if new_task_status == "terminée" and step.get("status") != "complété":
                        step_doc.reference.update({
                            "status": "complété",
                            "completed_date": now,
                            "updated_at": now,
                        })
                        # Update protocol etag
                        db.collection(PROTO_COLLECTION).document(proto_doc.id).update({
                            "updated_at": now,
                            "etag": str(uuid.uuid4()),
                        })
                        # Check protocol completion
                        from models.protocol import _check_protocol_completion
                        _check_protocol_completion(proto_doc.id)
                    elif new_task_status in ("à_faire", "en_cours") and step.get("status") == "complété":
                        step_doc.reference.update({
                            "status": "à_venir",
                            "completed_date": None,
                            "updated_at": now,
                        })
                        db.collection(PROTO_COLLECTION).document(proto_doc.id).update({
                            "updated_at": now,
                            "etag": str(uuid.uuid4()),
                        })
                    return  # Found and synced — done
    except Exception:
        pass  # Sync failure should not break the task update
```

**Integrate into `update_task`:**

```python
def update_task(task_id: str, data: dict) -> tuple[Optional[dict], list[str]]:
    existing = get_task(task_id)
    ...
    # After successful Firestore write:
    try:
        db.collection(COLLECTION).document(task_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    # Sync to protocol step if status changed
    old_status = existing.get("status", "")
    new_status = merged.get("status", "")
    if old_status != new_status:
        _sync_protocol_step(task_id, new_status)

    return merged, []
```

**Integrate into `toggle_task_complete`:**

The `toggle_task_complete` function calls `update_task`, so the sync will propagate automatically. No additional change needed.

## Step 6 — Prevent Circular Sync

The bidirectional sync creates a potential infinite loop: step completion syncs to task, which triggers task sync back to step. Prevent this with a simple guard:

In `models/protocol.py`, modify `_sync_task_status`:
```python
_SYNCING = set()  # Module-level guard

def _sync_task_status(task_id: str, step_status: str) -> None:
    if task_id in _SYNCING:
        return  # Prevent circular sync
    _SYNCING.add(task_id)
    try:
        from models.task import update_task
        if step_status == "complété":
            update_task(task_id, {"status": "terminée"})
        elif step_status in ("à_venir", "en_cours"):
            update_task(task_id, {"status": "à_faire"})
    except Exception:
        pass
    finally:
        _SYNCING.discard(task_id)
```

In `models/task.py`, apply the same guard:
```python
_SYNCING = set()  # Module-level guard

def _sync_protocol_step(task_id: str, new_task_status: str) -> None:
    if task_id in _SYNCING:
        return
    _SYNCING.add(task_id)
    try:
        ...  # existing sync logic
    finally:
        _SYNCING.discard(task_id)
```

## Step 7 — Update Protocol Summary

Update `get_protocol_summary` to only count the active protocol:

```python
def get_protocol_summary(dossier_id: str) -> dict:
    protocol = get_protocol_for_dossier(dossier_id, active_only=True)
    if not protocol:
        # Check if there are any historical protocols
        all_protos = list_protocols_for_dossier(dossier_id)
        return {
            "has_protocol": False,
            "has_history": len(all_protos) > 0,
            "total": 0,
            "completed": 0,
            "overdue": 0,
            "upcoming": 0,
        }
    ...  # existing logic, add "has_history" field
```

## Step 8 — Update Protocol Creation Form

In `routes/protocols.py`, update `protocol_new`:

```python
@protocols_bp.route("/new")
@login_required
def protocol_new() -> str:
    dossier_id = request.args.get("dossier_id", "")
    ...
    # CHANGE: only redirect if there's an ACTIVE protocol
    existing = get_protocol_for_dossier(dossier_id, active_only=True)
    if existing:
        return redirect(
            url_for("protocols.protocol_detail", protocol_id=existing["id"])
        )
    # Allow creation even if historical protocols exist
    ...
```

## Testing Checklist
- [ ] Can create first protocol on a dossier — works as before
- [ ] Cannot create second protocol while first is active — error message displayed
- [ ] Can complete a protocol (all steps completed → status changes to "complété")
- [ ] After completing first protocol, can create a second protocol
- [ ] Historical protocols appear in collapsed "Protocoles antérieurs" section
- [ ] Historical protocol detail page still accessible and read-only
- [ ] Completing a protocol step updates the linked task to "terminée"
- [ ] Completing a linked task updates the protocol step to "complété"
- [ ] Reopening a task (toggle) reverts the protocol step to "à_venir"
- [ ] Reopening a protocol step reverts the linked task to "à_faire"
- [ ] No infinite sync loop (step→task→step cycle terminates)
- [ ] Protocol auto-completion (all steps done) works with bidirectional sync
- [ ] Dashboard protocol step alerts only show active protocol steps
- [ ] Suspending a protocol allows creating a new one
