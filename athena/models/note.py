"""Dossier notes — timestamped journal entries linked to case files.

Each note becomes a VJOURNAL resource in the dossier's CalDAV collection
at /dav/dossier-{dossierId}/{noteId}.ics
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

logger = logging.getLogger(__name__)

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


def _default_doc() -> dict:
    """Return a dict with every note field set to its default value."""
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


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=5000)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

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


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_note(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    note_id = merged.get("id") or str(uuid.uuid4())
    vjournal_uid = merged.get("vjournal_uid") or str(uuid.uuid4())

    merged.update({
        "id": note_id,
        "created_at": merged.get("created_at") or now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "vjournal_uid": vjournal_uid,
    })

    try:
        db.collection(COLLECTION).document(note_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_note(note_id: str) -> Optional[dict]:
    """Fetch a single note by ID."""
    try:
        doc = db.collection(COLLECTION).document(note_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_note failed for %s: %s", note_id, exc)
    return None


def list_notes(
    dossier_id: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    pinned_first: bool = True,
) -> list[dict]:
    """Return notes, pinned first then newest first.

    Search scans title + content (client-side, same as other modules).
    """
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if category and category in VALID_CATEGORIES:
            results = [r for r in results if r.get("category") == category]

        if search:
            q = search.lower()
            results = [
                r for r in results
                if q in (r.get("title", "") or "").lower()
                or q in (r.get("content", "") or "").lower()
            ]

        # Sort: pinned first (if requested), then newest first
        results.sort(
            key=lambda n: (
                0 if pinned_first and n.get("pinned") else 1,
                -(n.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            ),
        )

        return results
    except Exception:
        return []


def update_note(
    note_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing note. Returns (updated_doc, errors)."""
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."]

    merged = {**existing, **_sanitize_data(data)}

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(note_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_note(note_id: str) -> tuple[bool, str]:
    """Delete a note. Returns (success, error_message)."""
    existing = get_note(note_id)
    if not existing:
        return False, "Note introuvable."

    try:
        db.collection(COLLECTION).document(note_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def toggle_pin(note_id: str) -> tuple[Optional[dict], list[str]]:
    """Toggle the pinned status of a note."""
    existing = get_note(note_id)
    if not existing:
        return None, ["Note introuvable."]
    return update_note(note_id, {"pinned": not existing.get("pinned", False)})


# ── Summary ──────────────────────────────────────────────────────────────


def _find_note_by_vjournal_uid(vjournal_uid: str) -> Optional[dict]:
    """Find a note by its VJOURNAL UID. Used for RELATED-TO resolution."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("vjournal_uid", "==", vjournal_uid)
        ).limit(1)
        for doc in query.stream():
            return doc.to_dict()
    except Exception as exc:
        logger.warning("_find_note_by_vjournal_uid failed for %s: %s", vjournal_uid, exc)
    return None


def get_notes_summary(dossier_id: str) -> dict:
    """Return {total} for tab display."""
    notes = list_notes(dossier_id=dossier_id)
    return {"total": len(notes)}


# ── RFC-5545 VJOURNAL serialization ─────────────────────────────────────


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

        break  # Only process first VJOURNAL

    return data
