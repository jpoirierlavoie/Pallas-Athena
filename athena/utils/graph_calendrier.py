"""Microsoft Graph calendar reads for the Bookings sync (spec L2).

Pure Graph glue over ``utils/graph`` (token + paginated GET/POST from L1) — no
Firestore, fully unit-testable. Reads « Bookings with me » reservations from
the juriste's mailbox via ``calendarView`` and, on a refusal, cancels the
underlying Outlook event (decision 2026-07-25: Calendars.ReadWrite).

``calendarView`` returns start/end in **UTC by default** (no ``Prefer``
header needed), so the conversion to the model's UTC storage is a plain parse.
MAIN SERVICE ONLY — a call from the portal process raises GraphNotConfigured.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import Config
from utils import graph

logger = logging.getLogger(__name__)

# The fields the reconciliation needs; nothing more (spec §4.3).
_SELECT = (
    "id,iCalUId,subject,start,end,location,isOnlineMeeting,onlineMeeting,"
    "attendees,organizer,isCancelled,lastModifiedDateTime"
)

# Graph emits 7 fractional-second digits ("…:00.0000000"); trim to ≤6 so
# datetime.fromisoformat parses across versions. Also tolerate a trailing Z.
_FRAC_RE = re.compile(r"\.(\d{1,})")


def _parse_graph_dt(obj: Optional[dict]) -> Optional[datetime]:
    """Parse a Graph ``{dateTime, timeZone}`` pair to timezone-aware UTC."""
    if not obj:
        return None
    raw = (obj.get("dateTime") or "").strip()
    if not raw:
        return None
    raw = raw.rstrip("Zz")
    raw = _FRAC_RE.sub(lambda m: "." + m.group(1)[:6], raw)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("graph_calendrier: unparseable dateTime")
        return None
    if dt.tzinfo is None:
        tz = (obj.get("timeZone") or "UTC").strip()
        if tz in ("UTC", ""):
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            # calendarView defaults to UTC, so this is defensive only.
            try:
                dt = dt.replace(tzinfo=ZoneInfo(tz))
            except Exception:
                dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def est_reservation(ev: dict) -> bool:
    """True when *ev* is a « Bookings with me » reservation (spec §4.4).

    Deterministic predicate: the juriste is the organizer AND the subject
    CONTAINS a configured keyword (case-insensitive). Bookings names the event
    « {Customer} - {Service} », so the service name is a SUFFIX — a substring
    match, not a prefix, is what detects it. No archaeology of undocumented
    Graph properties.
    """
    org = (
        ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address")
        or ""
    ).lower()
    subj = (ev.get("subject") or "").lower()
    upn = Config.BOOKINGS_JURISTE_UPN.lower()
    if not upn or org != upn:
        return False
    return any(k.lower() in subj for k in Config.BOOKINGS_SUBJECT_KEYWORDS)


def extraire(ev: dict) -> dict:
    """Normalize a Graph event to the fields the reconciliation upserts."""
    upn = Config.BOOKINGS_JURISTE_UPN.lower()
    online = ev.get("onlineMeeting") or {}
    join = (online.get("joinUrl") or "") if ev.get("isOnlineMeeting") else ""

    client_email, client_nom = "", ""
    for att in ev.get("attendees") or []:
        addr = (((att.get("emailAddress") or {}).get("address")) or "").lower()
        if addr and addr != upn:
            client_email = addr
            client_nom = ((att.get("emailAddress") or {}).get("name")) or ""
            break

    return {
        "graph_event_id": ev.get("id") or "",
        "graph_ical_uid": ev.get("iCalUId") or "",
        "title": ev.get("subject") or "",
        "start_datetime": _parse_graph_dt(ev.get("start")),
        "end_datetime": _parse_graph_dt(ev.get("end")),
        # A Teams link maps straight onto the native hearing fields, so a
        # confirmed booking is already a well-formed visio hearing.
        "conference_uri": join,
        "modalite": "visioconférence" if join else "présentiel",
        "location": ((ev.get("location") or {}).get("displayName")) or "",
        "client_email": client_email,
        "client_nom": client_nom,
        "graph_last_modified": ev.get("lastModifiedDateTime") or "",
        "is_cancelled": bool(ev.get("isCancelled")),
    }


def lister_reservations(debut: datetime, fin: datetime) -> list[dict]:
    """Return the raw Graph events in [debut, fin] (paginated, UTC times).

    Raises GraphNotConfigured / GraphError (the caller decides how to degrade).
    """
    params: dict[str, Any] = {
        "startDateTime": debut.astimezone(timezone.utc).isoformat(),
        "endDateTime": fin.astimezone(timezone.utc).isoformat(),
        "$select": _SELECT,
        "$top": 100,
    }
    data = graph.graph_get(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/calendarView", params=params
    )
    return data.get("value") or []


def annuler_reservation(graph_event_id: str, motif: str = "") -> None:
    """Cancel the underlying Outlook event (notifies the client via Bookings).

    Requires Calendars.ReadWrite. Raises GraphError on failure so the caller
    can surface a « annulez manuellement » warning without blocking the
    refusal itself.
    """
    graph.graph_post(
        f"/users/{Config.BOOKINGS_JURISTE_UPN}/events/{graph_event_id}/cancel",
        {"Comment": motif or "Rendez-vous annulé."},
    )
