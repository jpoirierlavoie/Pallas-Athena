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


def keyset_page(
    rows: list[Any],
    key_of,
    cursor_values: Optional[list[Any]],
    limit: int,
    *,
    descending: bool = True,
) -> tuple[list[Any], Optional[list[Any]], bool]:
    """Page a Python-materialized list by ORDER KEY, never by offset.

    Returns ``(page, next_cursor_values, has_more)``; ``next_cursor_values``
    is None on the last page.

    Why not an offset: the lists this serves are re-derived on every call
    (Firestore cannot filter or order them server-side), so one row inserted
    between two requests shifts every subsequent page — an offset walk then
    silently skips or repeats rows. A keyset resumes from a POSITION IN THE
    ORDERING instead, so an insertion before the cursor changes nothing and
    an insertion after it simply appears where it belongs.

    Two rules make it sound, and both are load-bearing:

    * ``key_of(row)`` must be a TOTAL order over IMMUTABLE fields. A key
      that can change between pages (a `pinned` toggle, an `updated_at`)
      can move one row across the boundary and cost a skip or a duplicate —
      bounded to the rows whose key actually changed, where an offset's
      failure is unbounded. Always end the key with the document id: it is
      unique and never rewritten, so ties are broken deterministically
      rather than by stream order.
    * The next cursor is minted FROM THE LAST RETURNED ROW, so nothing
      between ``limit`` and the end of the materialized window is skipped
      when the caller resumes (the ``list_dossiers`` rule).

    ``descending`` matches the display order: True keeps the largest key
    first and advances toward smaller keys.
    """
    if limit <= 0:
        return [], None, bool(rows)

    ordered = sorted(rows, key=key_of, reverse=descending)
    if cursor_values is not None:
        marker = tuple(cursor_values)

        def _after(row: Any) -> bool:
            key = tuple(_as_key_tuple(key_of(row)))
            try:
                return key < marker if descending else key > marker
            except TypeError:
                # A foreign cursor (wrong arity or wrong types) must degrade
                # to "keep everything" — the documented page-1 behaviour —
                # never crash and never silently mis-position the reader.
                return True

        ordered = [r for r in ordered if _after(r)]

    page = ordered[:limit]
    has_more = len(ordered) > limit
    next_values = _as_key_tuple(key_of(page[-1])) if (has_more and page) else None
    return page, (list(next_values) if next_values is not None else None), has_more


def _as_key_tuple(key: Any) -> tuple:
    """Normalize a sort key to a tuple so scalars and tuples compare alike."""
    return tuple(key) if isinstance(key, (tuple, list)) else (key,)


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
