"""Pagination helpers for list views.

Two modes coexist:

- **Cursor mode (preferred):** Firestore-native ``order_by().limit().start_after()``
  pagination. Reads ~PAGE_SIZE docs per page regardless of collection size.
  Model functions return ``(rows, next_cursor)``; routes thread an opaque
  cursor token plus a bounded "trail" of prior cursors (so « Précédent » can
  pop back) through the query string / hx-vals.

- **Legacy page mode:** in-memory slicing of a fully materialized list via
  :func:`paginate`. Kept for the search path (Python-side full-text filter)
  and for routes not yet migrated.
"""

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

PAGE_SIZE = 15

# « Précédent » works by popping a trail of prior cursors carried in the URL.
# Bound it so URLs stay short; beyond the cap the oldest entries drop and
# going back that far lands on page 1 (acceptable: cap × PAGE_SIZE records).
MAX_TRAIL = 20

_DT_KEY = "__dt__"


def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, dict]:
    """Slice a fully materialized list for the current page (legacy mode).

    Returns (page_items, pagination_dict).
    """
    page = max(1, page)
    offset = (page - 1) * page_size
    page_items = items[offset:offset + page_size]
    return page_items, {
        "mode": "page",
        "page": page,
        "has_prev": page > 1,
        "has_next": len(items) > offset + page_size,
    }


def encode_cursor(values: list[Any]) -> str:
    """Encode order-key values into an opaque URL-safe token.

    Values are the ``order_by`` field values of the last row on the current
    page (e.g. ``[date, id]``). Datetimes are tagged so they round-trip as
    timezone-aware datetimes.
    """
    def _enc(v: Any) -> Any:
        if isinstance(v, datetime):
            return {_DT_KEY: v.isoformat()}
        return v

    raw = json.dumps([_enc(v) for v in values], ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: Optional[str]) -> Optional[list[Any]]:
    """Decode a cursor token back into order-key values.

    Returns None for empty/malformed tokens — callers treat that as page 1.
    """
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        out: list[Any] = []
        for v in values:
            if isinstance(v, dict) and _DT_KEY in v:
                dt = datetime.fromisoformat(v[_DT_KEY])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                out.append(dt)
            else:
                out.append(v)
        return out
    except Exception:
        # Malformed/foreign token: degrade to the first page rather than 500.
        logger.warning("decode_cursor: malformed cursor token ignored")
        return None


def parse_trail(raw: Optional[str]) -> list[str]:
    """Parse the comma-separated cursor trail from the query string."""
    if not raw:
        return []
    return [t for t in raw.split(",") if t][-MAX_TRAIL:]


def cursor_pagination(
    *,
    cursor: Optional[str],
    trail: list[str],
    next_cursor: Optional[str],
    url: str,
    target: str,
    extra_vals: Optional[dict] = None,
) -> dict:
    """Build the pagination context for components/pagination.html (cursor mode).

    ``cursor`` is the token that produced the CURRENT page ("" / None = first
    page); ``trail`` holds the cursors of the pages before it; ``next_cursor``
    comes from the model's ``(rows, next_cursor)`` return.
    """
    has_prev = bool(cursor)
    prev_cursor = trail[-1] if trail else ""
    prev_trail = ",".join(trail[:-1])
    next_trail = ",".join(([*trail, cursor] if cursor else trail)[-MAX_TRAIL:])
    return {
        "mode": "cursor",
        "page": len(trail) + (2 if cursor else 1),  # display only
        "has_prev": has_prev,
        "has_next": bool(next_cursor),
        "next_cursor": next_cursor or "",
        "next_trail": next_trail,
        "prev_cursor": prev_cursor,
        "prev_trail": prev_trail,
        "url": url,
        "target": target,
        "extra_vals": extra_vals,
    }
