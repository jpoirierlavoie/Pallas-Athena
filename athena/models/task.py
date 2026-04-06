"""Task Firestore CRUD and RFC-5545 VTODO serialization."""

import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Circular sync guard — prevents infinite task↔protocol sync loops
_SYNCING: set[str] = set()

import icalendar

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

# Firestore collection path
COLLECTION = "tasks"

# Valid enum values
VALID_PRIORITIES = ("haute", "normale", "basse")
VALID_STATUSES = ("à_faire", "en_cours", "terminée", "annulée")
VALID_CATEGORIES = (
    "rédaction",
    "recherche",
    "correspondance",
    "dépôt",
    "signification",
    "suivi",
    "admin",
    "autre",
)

# Display labels (French)
PRIORITY_LABELS = {
    "haute": "Haute",
    "normale": "Normale",
    "basse": "Basse",
}
STATUS_LABELS = {
    "à_faire": "À faire",
    "en_cours": "En cours",
    "terminée": "Terminée",
    "annulée": "Annulée",
}
CATEGORY_LABELS = {
    "rédaction": "Rédaction",
    "recherche": "Recherche",
    "correspondance": "Correspondance",
    "dépôt": "Dépôt",
    "signification": "Signification",
    "suivi": "Suivi",
    "admin": "Administration",
    "autre": "Autre",
}

# Priority color mapping for UI
PRIORITY_COLORS = {
    "haute": "red",
    "normale": "orange",
    "basse": "gray",
}


def _default_doc() -> dict:
    """Return a dict with every task field set to its default value."""
    return {
        "id": "",
        "dossier_id": None,
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "description": "",
        "priority": "normale",
        "status": "à_faire",
        "due_date": None,
        "completed_date": None,
        "category": "autre",
        "created_at": None,
        "updated_at": None,
        "etag": "",
        # DAV-specific
        "vtodo_uid": "",
        "dav_href": "",
        # Optional link to a parent note (RELATED-TO)
        "related_note_id": None,
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre de la tâche est requis.")

    priority = data.get("priority", "")
    if priority and priority not in VALID_PRIORITIES:
        errors.append("Priorité invalide.")

    status = data.get("status", "")
    if status and status not in VALID_STATUSES:
        errors.append("Statut invalide.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_task(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    task_id = str(uuid.uuid4())
    vtodo_uid = str(uuid.uuid4())

    merged.update({
        "id": task_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "vtodo_uid": vtodo_uid,
        "dav_href": f"/dav/tasks/{task_id}.ics",
    })

    # Auto-set completed_date if status is terminée
    if merged["status"] == "terminée" and not merged.get("completed_date"):
        merged["completed_date"] = now

    try:
        db.collection(COLLECTION).document(task_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_task(task_id: str) -> Optional[dict]:
    """Fetch a single task by ID."""
    try:
        doc = db.collection(COLLECTION).document(task_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


def list_tasks(
    dossier_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    priority_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
) -> list[dict]:
    """Return tasks, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters (Firestore single-field index limitation)
        if status_filter and status_filter in VALID_STATUSES:
            results = [r for r in results if r.get("status") == status_filter]

        if priority_filter and priority_filter in VALID_PRIORITIES:
            results = [r for r in results if r.get("priority") == priority_filter]

        if category_filter and category_filter in VALID_CATEGORIES:
            results = [r for r in results if r.get("category") == category_filter]

        # Sort by due_date (soonest first, None last), then by priority
        priority_order = {"haute": 0, "normale": 1, "basse": 2}
        results.sort(
            key=lambda t: (
                0 if t.get("due_date") else 1,
                t.get("due_date") or datetime.max.replace(tzinfo=timezone.utc),
                priority_order.get(t.get("priority", "normale"), 1),
            ),
        )

        return results
    except Exception:
        return []


def update_task(
    task_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing task. Returns (updated_doc, errors)."""
    existing = get_task(task_id)
    if not existing:
        return None, ["Tâche introuvable."]

    merged = {**existing, **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    # Auto-set completed_date when completing
    if merged["status"] == "terminée" and not merged.get("completed_date"):
        merged["completed_date"] = now
    # Clear completed_date if reopened
    if merged["status"] in ("à_faire", "en_cours"):
        merged["completed_date"] = None

    old_status = existing.get("status", "")

    try:
        db.collection(COLLECTION).document(task_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    # Sync to protocol step if status changed
    new_status = merged.get("status", "")
    if old_status != new_status:
        _sync_protocol_step(task_id, new_status)

    return merged, []


def delete_task(task_id: str) -> tuple[bool, str]:
    """Delete a task. Returns (success, error_message)."""
    existing = get_task(task_id)
    if not existing:
        return False, "Tâche introuvable."

    try:
        db.collection(COLLECTION).document(task_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def toggle_task_complete(task_id: str) -> tuple[Optional[dict], list[str]]:
    """Toggle a task between à_faire and terminée. Returns (updated_doc, errors)."""
    existing = get_task(task_id)
    if not existing:
        return None, ["Tâche introuvable."]

    if existing["status"] in ("terminée", "annulée"):
        new_status = "à_faire"
    else:
        new_status = "terminée"

    return update_task(task_id, {"status": new_status})


# ── Protocol sync ────────────────────────────────────────────────────────


def _sync_protocol_step(task_id: str, new_task_status: str) -> None:
    """Sync a protocol step when its linked task status changes."""
    if task_id in _SYNCING:
        return
    _SYNCING.add(task_id)
    try:
        from models.protocol import (
            COLLECTION as PROTO_COLLECTION,
            STEPS_SUBCOLLECTION,
            _check_protocol_completion,
        )

        # Search active protocols for a step linked to this task
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
                    now = datetime.now(timezone.utc)
                    if new_task_status == "terminée" and step.get("status") != "complété":
                        step_doc.reference.update({
                            "status": "complété",
                            "completed_date": now,
                            "updated_at": now,
                        })
                        db.collection(PROTO_COLLECTION).document(proto_doc.id).update({
                            "updated_at": now,
                            "etag": str(uuid.uuid4()),
                        })
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
    finally:
        _SYNCING.discard(task_id)


# ── Summary ──────────────────────────────────────────────────────────────


def get_task_summary(dossier_id: str) -> dict:
    """Return task counts for a dossier."""
    tasks = list_tasks(dossier_id=dossier_id)
    active = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]
    completed = [t for t in tasks if t.get("status") == "terminée"]
    now = datetime.now(timezone.utc)
    overdue = [
        t for t in active
        if t.get("due_date") and t["due_date"] < now
    ]
    return {
        "total": len(tasks),
        "active": len(active),
        "completed": len(completed),
        "overdue": len(overdue),
    }


# ── RFC-5545 VTODO serialization ─────────────────────────────────────────


def task_to_vtodo(task: dict) -> str:
    """Serialize a task dict to an RFC-5545 VTODO string wrapped in VCALENDAR."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Tâche//FR")
    cal.add("version", "2.0")

    todo = icalendar.Todo()
    todo.add("uid", task.get("vtodo_uid", ""))
    todo.add("summary", task.get("title", ""))

    # DESCRIPTION — combine description with dossier info
    desc_parts = []
    if task.get("description"):
        desc_parts.append(task["description"])
    if task.get("dossier_file_number"):
        desc_parts.append(
            f"Dossier: {task.get('dossier_file_number', '')} - {task.get('dossier_title', '')}"
        )
    if desc_parts:
        todo.add("description", "\n\n".join(desc_parts))

    # PRIORITY mapping: haute=1, normale=5, basse=9
    priority_map = {"haute": 1, "normale": 5, "basse": 9}
    todo.add("priority", priority_map.get(task.get("priority", "normale"), 5))

    # STATUS mapping
    status_map = {
        "à_faire": "NEEDS-ACTION",
        "en_cours": "IN-PROCESS",
        "terminée": "COMPLETED",
        "annulée": "CANCELLED",
    }
    todo.add("status", status_map.get(task.get("status", ""), "NEEDS-ACTION"))

    # DUE — emit in America/Montreal for datetime values
    mtl = ZoneInfo("America/Montreal")
    due = task.get("due_date")
    if due:
        if hasattr(due, "hour") and due.hour == 0 and due.minute == 0:
            # Date-only stored as midnight — emit as date
            todo.add("due", due.date())
        elif hasattr(due, "hour"):
            if due.tzinfo is None or due.tzinfo == timezone.utc:
                due = due.replace(tzinfo=timezone.utc).astimezone(mtl)
            todo.add("due", due)
        else:
            todo.add("due", due)

    # COMPLETED
    completed = task.get("completed_date")
    if completed:
        if hasattr(completed, "hour"):
            if completed.tzinfo is None or completed.tzinfo == timezone.utc:
                completed = completed.replace(tzinfo=timezone.utc).astimezone(mtl)
        todo.add("completed", completed)

    # CATEGORIES
    if task.get("category"):
        label = CATEGORY_LABELS.get(task["category"], task["category"])
        todo.add("categories", [label])

    # LAST-MODIFIED
    updated = task.get("updated_at")
    if updated:
        todo.add("last-modified", updated)

    todo.add("sequence", 0)

    # Custom X- properties for round-trip fidelity
    if task.get("dossier_id"):
        todo.add("x-pallas-dossier-id", task["dossier_id"])
    if task.get("category"):
        todo.add("x-pallas-category", task["category"])

    # RELATED-TO: link to parent note's VJOURNAL UID
    related_note_uid = None
    if task.get("related_note_id"):
        from models.note import get_note
        related_note = get_note(task["related_note_id"])
        if related_note and related_note.get("vjournal_uid"):
            related_note_uid = related_note["vjournal_uid"]
            related_prop = icalendar.vText(related_note_uid)
            todo.add("related-to", related_prop, parameters={"RELTYPE": "PARENT"})

    cal.add_component(todo)
    ical_str = cal.to_ical().decode("utf-8")

    # Fallback: if library didn't emit RELTYPE correctly, insert manually
    if related_note_uid and f"RELTYPE=PARENT" not in ical_str and related_note_uid in ical_str:
        line = f"RELATED-TO;RELTYPE=PARENT:{related_note_uid}"
        ical_str = ical_str.replace("END:VTODO", f"{line}\r\nEND:VTODO")

    return ical_str


def vtodo_to_task(ical_str: str) -> dict:
    """Parse an RFC-5545 VTODO string into a task dict (for DAV PUT)."""
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VTODO":
            continue

        # UID
        uid = component.get("uid")
        if uid:
            data["vtodo_uid"] = str(uid)

        # SUMMARY → title
        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        # DESCRIPTION → description (first part before dossier info)
        desc = component.get("description")
        if desc:
            data["description"] = str(desc)

        # PRIORITY → priority
        priority = component.get("priority")
        if priority:
            pval = int(priority)
            if pval <= 1:
                data["priority"] = "haute"
            elif pval <= 5:
                data["priority"] = "normale"
            else:
                data["priority"] = "basse"

        # STATUS → status
        status = component.get("status")
        if status:
            status_str = str(status).upper()
            reverse_map = {
                "NEEDS-ACTION": "à_faire",
                "IN-PROCESS": "en_cours",
                "COMPLETED": "terminée",
                "CANCELLED": "annulée",
            }
            data["status"] = reverse_map.get(status_str, "à_faire")

        # DUE → due_date (normalize to UTC)
        due = component.get("due")
        if due:
            dt = due.dt
            if hasattr(dt, "hour"):
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                data["due_date"] = dt
            else:
                data["due_date"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # COMPLETED → completed_date (normalize to UTC)
        completed = component.get("completed")
        if completed:
            dt = completed.dt
            if hasattr(dt, "hour"):
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                data["completed_date"] = dt
            else:
                data["completed_date"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # Custom X- properties
        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        category = component.get("x-pallas-category")
        if category:
            cat = str(category)
            if cat in VALID_CATEGORIES:
                data["category"] = cat

        # RELATED-TO → resolve parent note link
        related_tos = component.get("related-to")
        if related_tos:
            if not isinstance(related_tos, list):
                related_tos = [related_tos]
            for rt in related_tos:
                rt_str = str(rt)
                params = getattr(rt, "params", {})
                reltype = params.get("RELTYPE", "PARENT")
                if reltype == "PARENT" and rt_str:
                    from models.note import _find_note_by_vjournal_uid
                    note = _find_note_by_vjournal_uid(rt_str)
                    if note:
                        data["related_note_id"] = note["id"]
                    break

        break  # Only process first VTODO

    return data
