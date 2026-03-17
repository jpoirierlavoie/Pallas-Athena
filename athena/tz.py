"""Timezone helpers for Pallas Athena.

All Firestore timestamps are stored as UTC.  The single user works from
Montréal, so every datetime shown in the UI or serialised to CalDAV must
be converted to America/Montreal first.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

MTL = ZoneInfo("America/Montreal")


def to_mtl(dt: datetime | None) -> datetime | None:
    """Convert a UTC (or naive) datetime to America/Montreal.

    Returns *None* unchanged so Jinja2 filters can be applied
    unconditionally.
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return dt  # date-only objects — nothing to shift
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MTL)


def mtl_to_utc(dt: datetime | None) -> datetime | None:
    """Interpret a naive datetime as Montreal local time and convert to UTC.

    Used when parsing HTML form inputs, which carry no timezone but are
    entered by the user in Montreal time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MTL)
    return dt.astimezone(timezone.utc)
