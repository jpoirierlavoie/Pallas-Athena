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
import unicodedata
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

# Le séparateur que Bookings insère entre le client et le service. Les trois
# formes de tiret sont admises : le trait d'union ordinaire, mais aussi les
# cadratins qu'Outlook substitue parfois à la saisie.
_SEPARATEUR = r"[-–—]\s*"


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


def _plier(texte: str) -> str:
    """Casse et diacritiques neutralisées, pour comparer des sujets français.

    Même patron que ``utils/rapprochement._plier`` : décomposition NFD puis
    rejet des marques combinantes. Il est ici LOAD-BEARING, pas cosmétique —
    « é » précomposé (NFC, U+00E9) et « é » décomposé (NFD, e + U+0301) sont
    des chaînes différentes pour Python, et rien ne garantit la forme que
    Bookings a stockée. Sans ce pliage, un mot-clé accentué peut ne jamais
    mordre, sans le moindre message. « Consultation » n'avait pas d'accent —
    le risque naît avec « Réunion ».
    """
    decompose = unicodedata.normalize("NFD", texte or "")
    return "".join(
        c for c in decompose if unicodedata.category(c) != "Mn"
    ).casefold()


def mot_cle_correspondant(ev: dict) -> str:
    """Le mot-clé Bookings détecté dans *ev*, ou « » (spec §4.4).

    Prédicat déterministe : le juriste est l'organisateur ET le sujet SE
    TERMINE par « {séparateur} {mot-clé} ». Bookings nomme l'événement
    « {Client} - {Service} », donc le nom du service est un SUFFIXE.

    L'ancrage n'est pas un détail. Le prédicat ne peut PAS distinguer une
    réservation Bookings d'un événement que le juriste crée lui-même : dans
    les deux cas il est l'organisateur. Le mot-clé est donc le seul
    discriminant, et une simple sous-chaîne capturerait « Réunion d'équipe »
    ou « Préparation réunion CA » — importés comme des rendez-vous clients,
    avec les conséquences que l'on sait (un refus annule la vraie réunion
    Outlook). Le séparateur est exigé pour qu'un événement intitulé
    simplement « Réunion » ne morde pas non plus.

    Rend le mot-clé plutôt qu'un booléen : l'appelant en dérive le type
    d'audience (``Config.BOOKINGS_TYPE_PAR_MOT_CLE``).
    """
    org = (
        ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address")
        or ""
    ).lower()
    upn = Config.BOOKINGS_JURISTE_UPN.lower()
    if not upn or org != upn:
        return ""
    sujet = _plier(ev.get("subject") or "")
    for k in Config.BOOKINGS_SUBJECT_KEYWORDS:
        plie = _plier(k)
        # Un mot-clé vide serait « contenu » partout : la garde reste, comme
        # dans la version sous-chaîne.
        if not plie:
            continue
        if re.search(_SEPARATEUR + re.escape(plie) + r"\s*$", sujet):
            return k
    return ""


def est_reservation(ev: dict) -> bool:
    """True when *ev* is a « Bookings with me » reservation (spec §4.4)."""
    return bool(mot_cle_correspondant(ev))


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
        # Le service Bookings détecté — l'appelant en dérive le hearing_type.
        # Recalculé plutôt que passé en argument : extraire reste appelable
        # seule, et le mot-clé ne peut pas diverger du prédicat qui l'a admis.
        "mot_cle": mot_cle_correspondant(ev),
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
