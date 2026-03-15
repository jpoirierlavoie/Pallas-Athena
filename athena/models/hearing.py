"""Hearing (court date) Firestore CRUD and RFC-5545 VEVENT serialization."""

import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import icalendar

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

# Firestore collection path
COLLECTION = "hearings"

# Valid enum values
VALID_HEARING_TYPES = (
    "audience",
    "conférence_de_gestion",
    "conférence_de_règlement",
    "interrogatoire",
    "médiation",
    "procès",
    "appel",
    "autre",
)
VALID_STATUSES = (
    "confirmée",
    "à_confirmer",
    "reportée",
    "annulée",
    "terminée",
)
VALID_REMINDER_MINUTES = (15, 30, 60, 120, 1440, 2880, 10080)

# Display labels (French)
HEARING_TYPE_LABELS = {
    "audience": "Audience",
    "conférence_de_gestion": "Conférence de gestion",
    "conférence_de_règlement": "Conférence de règlement",
    "interrogatoire": "Interrogatoire",
    "médiation": "Médiation",
    "procès": "Procès",
    "appel": "Appel",
    "autre": "Autre",
}
STATUS_LABELS = {
    "confirmée": "Confirmée",
    "à_confirmer": "À confirmer",
    "reportée": "Reportée",
    "annulée": "Annulée",
    "terminée": "Terminée",
}
REMINDER_LABELS = {
    15: "15 minutes",
    30: "30 minutes",
    60: "1 heure",
    120: "2 heures",
    1440: "24 heures",
    2880: "48 heures",
    10080: "1 semaine",
}

# Hearing type → suggested color for calendar display
HEARING_TYPE_COLORS = {
    "audience": "indigo",
    "conférence_de_gestion": "blue",
    "conférence_de_règlement": "teal",
    "interrogatoire": "amber",
    "médiation": "green",
    "procès": "red",
    "appel": "purple",
    "autre": "gray",
}

# Quick-select courthouse locations
QUICK_LOCATIONS = (
    "Palais de justice de Montréal, 1 rue Notre-Dame Est",
    "Palais de justice de Québec, 300 boulevard Jean-Lesage",
    "Palais de justice de Laval, 2800 boulevard Saint-Martin Ouest",
    "Palais de justice de Longueuil, 1111 boulevard Jacques-Cartier Est",
)

# Suggested hearing titles per type
HEARING_TITLE_SUGGESTIONS = {
    "audience": "Audience sur requête",
    "conférence_de_gestion": "Conférence de gestion",
    "conférence_de_règlement": "Conférence de règlement à l'amiable",
    "interrogatoire": "Interrogatoire préalable",
    "médiation": "Séance de médiation",
    "procès": "Procès — instruction au mérite",
    "appel": "Audience en appel",
    "autre": "",
}


def _default_doc() -> dict:
    """Return a dict with every hearing field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "",
        "hearing_type": "audience",
        "start_datetime": None,
        "end_datetime": None,
        "all_day": False,
        "location": "",
        "court": "",
        "judge": "",
        "notes": "",
        "reminder_minutes": 1440,
        "status": "à_confirmer",
        "created_at": None,
        "updated_at": None,
        "etag": "",
        # DAV-specific
        "vevent_uid": "",
        "dav_href": "",
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

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette audience.")

    if not data.get("title", "").strip():
        errors.append("Le titre de l'audience est requis.")

    if not data.get("start_datetime"):
        errors.append("La date et l'heure de début sont requises.")

    ht = data.get("hearing_type", "")
    if ht and ht not in VALID_HEARING_TYPES:
        errors.append("Type d'audience invalide.")

    st = data.get("status", "")
    if st and st not in VALID_STATUSES:
        errors.append("Statut invalide.")

    # End must be after start
    start = data.get("start_datetime")
    end = data.get("end_datetime")
    if start and end and end <= start:
        errors.append("L'heure de fin doit être après l'heure de début.")

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_hearing(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}

    # Auto-set end_datetime if not provided (start + 1 hour)
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    hearing_id = str(uuid.uuid4())
    vevent_uid = str(uuid.uuid4())

    merged.update({
        "id": hearing_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "vevent_uid": vevent_uid,
        "dav_href": f"/dav/calendar/{hearing_id}.ics",
    })

    try:
        db.collection(COLLECTION).document(hearing_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_hearing(hearing_id: str) -> Optional[dict]:
    """Fetch a single hearing by ID."""
    try:
        doc = db.collection(COLLECTION).document(hearing_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


def list_hearings(
    dossier_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    hearing_type_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return hearings, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters (Firestore single-field index limitation)
        if status_filter and status_filter in VALID_STATUSES:
            results = [r for r in results if r.get("status") == status_filter]

        if hearing_type_filter and hearing_type_filter in VALID_HEARING_TYPES:
            results = [r for r in results if r.get("hearing_type") == hearing_type_filter]

        if date_from:
            results = [r for r in results if r.get("start_datetime") and r["start_datetime"] >= date_from]
        if date_to:
            results = [r for r in results if r.get("start_datetime") and r["start_datetime"] <= date_to]

        # Sort by start_datetime ascending (chronological)
        results.sort(
            key=lambda h: h.get("start_datetime") or datetime.min.replace(tzinfo=timezone.utc),
        )

        return results
    except Exception:
        return []


def update_hearing(
    hearing_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing hearing. Returns (updated_doc, errors)."""
    existing = get_hearing(hearing_id)
    if not existing:
        return None, ["Audience introuvable."]

    merged = {**existing, **_sanitize_data(data)}

    # Auto-set end_datetime if not provided
    if merged.get("start_datetime") and not merged.get("end_datetime"):
        merged["end_datetime"] = merged["start_datetime"] + timedelta(hours=1)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(hearing_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_hearing(hearing_id: str) -> tuple[bool, str]:
    """Delete a hearing. Returns (success, error_message)."""
    existing = get_hearing(hearing_id)
    if not existing:
        return False, "Audience introuvable."

    try:
        db.collection(COLLECTION).document(hearing_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


# ── Summary ──────────────────────────────────────────────────────────────


def get_hearing_summary(dossier_id: str) -> dict:
    """Return hearing counts for a dossier."""
    hearings = list_hearings(dossier_id=dossier_id)
    now = datetime.now(timezone.utc)
    upcoming = [h for h in hearings if h.get("start_datetime") and h["start_datetime"] > now and h.get("status") not in ("annulée", "terminée")]
    past = [h for h in hearings if h.get("start_datetime") and h["start_datetime"] <= now or h.get("status") in ("terminée",)]
    return {
        "total": len(hearings),
        "upcoming": len(upcoming),
        "past": len(past),
    }


def get_upcoming_hearings(days: int = 30) -> list[dict]:
    """Return hearings within the next N days, across all dossiers."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    hearings = list_hearings(date_from=now, date_to=cutoff)
    # Exclude cancelled
    return [h for h in hearings if h.get("status") not in ("annulée",)]


# ── RFC-5545 VEVENT serialization ─────────────────────────────────────────


def hearing_to_vevent(hearing: dict) -> str:
    """Serialize a hearing dict to an RFC-5545 VEVENT string wrapped in VCALENDAR."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Audience//FR")
    cal.add("version", "2.0")

    event = icalendar.Event()
    event.add("uid", hearing.get("vevent_uid", ""))
    event.add("summary", hearing.get("title", ""))

    # DTSTART / DTEND
    start = hearing.get("start_datetime")
    end = hearing.get("end_datetime")
    if hearing.get("all_day"):
        if start and hasattr(start, "date"):
            event.add("dtstart", start.date())
        if end and hasattr(end, "date"):
            event.add("dtend", end.date())
    else:
        if start:
            event.add("dtstart", start)
        if end:
            event.add("dtend", end)

    # LOCATION
    if hearing.get("location"):
        event.add("location", hearing["location"])

    # DESCRIPTION — combine notes with dossier info
    desc_parts = []
    if hearing.get("notes"):
        desc_parts.append(hearing["notes"])
    desc_parts.append(
        f"Dossier: {hearing.get('dossier_file_number', '')} - {hearing.get('dossier_title', '')}"
    )
    if hearing.get("hearing_type"):
        label = HEARING_TYPE_LABELS.get(hearing["hearing_type"], hearing["hearing_type"])
        desc_parts.append(f"Type: {label}")
    if hearing.get("court"):
        desc_parts.append(f"Cour: {hearing['court']}")
    if hearing.get("judge"):
        desc_parts.append(f"Juge: {hearing['judge']}")
    event.add("description", "\n".join(desc_parts))

    # STATUS mapping
    status_map = {
        "confirmée": "CONFIRMED",
        "à_confirmer": "TENTATIVE",
        "reportée": "TENTATIVE",
        "annulée": "CANCELLED",
        "terminée": "CONFIRMED",
    }
    event.add("status", status_map.get(hearing.get("status", ""), "TENTATIVE"))

    # CATEGORIES
    if hearing.get("hearing_type"):
        label = HEARING_TYPE_LABELS.get(hearing["hearing_type"], hearing["hearing_type"])
        event.add("categories", [label])

    # VALARM — reminder
    reminder_min = hearing.get("reminder_minutes", 1440)
    if reminder_min and reminder_min > 0:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", hearing.get("title", "Audience"))
        alarm.add("trigger", timedelta(minutes=-reminder_min))
        event.add_component(alarm)

    # LAST-MODIFIED
    updated = hearing.get("updated_at")
    if updated:
        event.add("last-modified", updated)

    event.add("sequence", 0)

    # Custom X- properties for round-trip fidelity
    if hearing.get("dossier_id"):
        event.add("x-pallas-dossier-id", hearing["dossier_id"])
    if hearing.get("court"):
        event.add("x-pallas-court", hearing["court"])
    if hearing.get("judge"):
        event.add("x-pallas-judge", hearing["judge"])
    if hearing.get("hearing_type"):
        event.add("x-pallas-hearing-type", hearing["hearing_type"])

    cal.add_component(event)
    return cal.to_ical().decode("utf-8")


def vevent_to_hearing(ical_str: str) -> dict:
    """Parse an RFC-5545 VEVENT string into a hearing dict (for CalDAV PUT)."""
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        # UID
        uid = component.get("uid")
        if uid:
            data["vevent_uid"] = str(uid)

        # SUMMARY → title
        summary = component.get("summary")
        if summary:
            data["title"] = str(summary)

        # DTSTART → start_datetime
        dtstart = component.get("dtstart")
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                data["start_datetime"] = dt
                data["all_day"] = False
            else:
                data["start_datetime"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )
                data["all_day"] = True

        # DTEND → end_datetime
        dtend = component.get("dtend")
        if dtend:
            dt = dtend.dt
            if hasattr(dt, "hour"):
                data["end_datetime"] = dt
            else:
                data["end_datetime"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # LOCATION
        location = component.get("location")
        if location:
            data["location"] = str(location)

        # DESCRIPTION → notes (just the first line; rest is metadata)
        desc = component.get("description")
        if desc:
            data["notes"] = str(desc)

        # STATUS
        status = component.get("status")
        if status:
            status_str = str(status).upper()
            reverse_map = {
                "CONFIRMED": "confirmée",
                "TENTATIVE": "à_confirmer",
                "CANCELLED": "annulée",
            }
            data["status"] = reverse_map.get(status_str, "à_confirmer")

        # Custom X- properties
        dossier_id = component.get("x-pallas-dossier-id")
        if dossier_id:
            data["dossier_id"] = str(dossier_id)

        court = component.get("x-pallas-court")
        if court:
            data["court"] = str(court)

        judge = component.get("x-pallas-judge")
        if judge:
            data["judge"] = str(judge)

        hearing_type = component.get("x-pallas-hearing-type")
        if hearing_type:
            ht = str(hearing_type)
            if ht in VALID_HEARING_TYPES:
                data["hearing_type"] = ht

        # VALARM → reminder_minutes
        for sub in component.subcomponents:
            if sub.name == "VALARM":
                trigger = sub.get("trigger")
                if trigger and hasattr(trigger, "dt"):
                    td = trigger.dt
                    if isinstance(td, timedelta):
                        data["reminder_minutes"] = abs(int(td.total_seconds() / 60))

        break  # Only process first VEVENT

    return data
