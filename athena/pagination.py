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

# The coarse « saut de plusieurs pages » step, in pages.
JUMP_PAGES = 10

# Hard ceiling on a client-supplied page number. `page` reaches Firestore's
# offset(), which BILLS every skipped document, so an unclamped ?page=100000
# would issue offset(1_499_985) — ~1.5M reads and a gunicorn SIGKILL at 60 s.
MAX_PAGE = 2000

_DT_KEY = "__dt__"


def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, dict]:
    """Slice a fully materialized list for the current page (legacy mode).

    Returns (page_items, pagination_dict).
    """
    total = len(items)
    # Clamp UP as well as down: ?page=999 on a 3-page list used to render an
    # empty table under « Page 999 » — a stranded reader on the one pagination
    # param a user can hand-edit.
    page = min(max(1, page), total_pages_of(total, page_size) or 1)
    offset = (page - 1) * page_size
    page_items = items[offset:offset + page_size]
    return page_items, {
        "mode": "page",
        "page": page,
        "has_prev": page > 1,
        "has_next": total > offset + page_size,
        # The total was always in hand here (every legacy caller materializes
        # the full filtered list before slicing) and was simply thrown away.
        "page_exact": True,
        **_nav(page, total, page_size),
    }


def total_pages_of(total: Optional[int], page_size: int = PAGE_SIZE) -> Optional[int]:
    """Ceiling division, or None when the total is UNKNOWN.

    The None is load-bearing: a count that FAILED must never read as zero.
    « Page 7 / 0 » is a confident lie, and the June-2026 incident is exactly
    a failed aggregation degrading to a plausible 0. An empty set — a real,
    known zero — maps to 1 page.
    """
    if total is None:
        return None
    return max(1, -(-total // page_size))


def resolve_page(
    page_arg: Optional[int],
    total_pages: Optional[int],
    *,
    has_cursor: bool,
    page_size: int = PAGE_SIZE,
) -> tuple[int, int]:
    """Return ``(page, offset)`` for one list read.

    Three branches, and the middle one is a refusal:

    * ``has_cursor`` — the cursor drives the rows, so the number is a LABEL
      only and gets no offset. It is deliberately **not** clamped to the
      total: a stale-low count must not freeze a label whose rows are
      still advancing.
    * ``total_pages is None`` — the offset read is REFUSED rather than billed
      against an unbounded skip. There is nothing to clamp against, and
      offset() bills every document it skips.
    * otherwise — clamped to ``[1, min(total_pages, MAX_PAGE)]``.
    """
    wanted = page_arg if isinstance(page_arg, int) and page_arg > 0 else 1
    if has_cursor:
        return min(wanted, MAX_PAGE), 0
    if total_pages is None:
        if wanted > 1:
            logger.warning(
                "resolve_page: offset read refused — total unknown (page=%d)", wanted
            )
        return 1, 0
    page = min(wanted, total_pages, MAX_PAGE)
    return page, (page - 1) * page_size


def _nav(page: int, total: Optional[int], page_size: int = PAGE_SIZE) -> dict:
    """The jump targets and their display flags — ONE authority, both modes.

    Emitting an identical key set in page mode and cursor mode is what lets a
    single block of markup serve both without arithmetic in the template.
    """
    last_page = total_pages_of(total, page_size)
    has_total = last_page is not None
    # A ±JUMP_PAGES target is offered only when it lands STRICTLY BETWEEN
    # « Début » and « Fin ». Clamping it instead would render an unlabelled
    # duplicate of a neighbour — « −10 » pointing at page 1 next to a
    # « Début » that already does. This strict rule is also what makes the
    # request's « for larger sets » condition emerge from the data instead of
    # from a guessed threshold: below ~12 pages neither target can qualify.
    #
    # `in_range` gates BOTH directions, and it carries two terms for the same
    # fault — a control lying about itself. `has_total`: a leap is an
    # absolute-offset read and resolve_page REFUSES one without a total, so
    # ungated, a failed count at page 15 would render a « −10 » that silently
    # landed on page 1. `page <= last_page`: `page` is deliberately NOT
    # clamped to the total on the cursor path (see resolve_page), so page 40
    # of a 3-page list is reachable — and there a « −10 » targets page 30,
    # which resolve_page clamps to the stale total: the reader goes back 37
    # pages, not 10, and the label resets to « Page 3 / 3 ». Redundant on
    # the forward side (page + JUMP < L ⟹ page < L), stated on both so the
    # implication is read off the line instead of re-derived.
    # « Début » needs no total (page 1 is offset 0), so show_first stays free.
    in_range = has_total and page <= last_page
    jump_prev = page - JUMP_PAGES if in_range and page - JUMP_PAGES > 1 else None
    jump_next = (
        page + JUMP_PAGES if in_range and page + JUMP_PAGES < last_page else None
    )
    show_first = page > 2
    return {
        "total": total,
        "total_pages": last_page,
        "last_page": last_page,
        "has_total": has_total,
        "prev_page": max(1, page - 1),
        # NOT clamped: see resolve_page. On the cursor path the rows may be
        # ahead of a stale total, and pinning this would strand the reader.
        "next_page": page + 1,
        "jump_prev_page": jump_prev,
        "jump_next_page": jump_next,
        # At page 2, « Début » duplicates « Précédent ».
        "show_first": show_first,
        # At the penultimate page, « Suivant » IS the end.
        "show_end": has_total and last_page > page + 1,
        # Derived, never a second threshold that could drift from the rule.
        "show_jump": jump_prev is not None or jump_next is not None,
        "jump_size": JUMP_PAGES,
        # ── Per-LIST capability ───────────────────────────────────────────
        # The flags above answer « does this control apply on THIS page? »;
        # these answer « could it EVER apply on this list? ». The component
        # RENDERS on the second and DISABLES on the first, so the row keeps a
        # constant shape for a whole walk of one list while no permanently
        # dead control is ever drawn. Each is the existential closure of its
        # per-page rule over page ∈ [1..last_page]:
        #   show_first ⟺ page > 2            → ∃ ⟺ L ≥ 3
        #   show_end   ⟺ page ≤ L − 2        → ∃ ⟺ L ≥ 3
        #   jump_prev  ⟺ page ≥ JUMP + 2     → ∃ ⟺ L ≥ JUMP + 2
        #   jump_next  ⟺ page ≤ L − JUMP − 1 → ∃ ⟺ L ≥ JUMP + 2
        # The two ends coincide at L ≥ 3 by arithmetic accident — each rule
        # happens to cost two pages — NOT by derivation, so they stay two
        # expressions: one shared flag would drift the day either rule moved.
        #
        # `enabled ⟹ rendered` must hold, or a control vanishes exactly when
        # it would have worked. It holds by arithmetic for show_end and
        # jump_next (each bounds L from below on its own), by `in_range` for
        # jump_prev, and by the explicit `or show_first` here — page 1 is
        # offset 0, so « Début » is valid under ANY total and must never be
        # hidden while it applies. `not has_total` keeps it on a countless
        # list too, where it is the ONLY way home: « Précédent » moves one
        # page, the trail caps at MAX_TRAIL, and a failed count is the DEEP
        # case by nature (a count fails on big collections, not small ones).
        "first_possible": (not has_total) or last_page >= 3 or show_first,
        "last_possible": has_total and last_page >= 3,
        "jump_possible": has_total and last_page >= JUMP_PAGES + 2,
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
    page: Optional[int] = None,
    total: Optional[int] = None,
    page_size: int = PAGE_SIZE,
) -> dict:
    """Build the pagination context for components/pagination.html (cursor mode).

    ``cursor`` is the token that produced the CURRENT page ("" / None = first
    page); ``trail`` holds the cursors of the pages before it; ``next_cursor``
    comes from the model's ``(rows, next_cursor)`` return.

    ``page`` is the position carried explicitly by every control, and passing
    it fixes two silent defects of deriving the number from the trail:

    * the label used to FREEZE at ``MAX_TRAIL + 2``. Once the trail saturated,
      ``len(trail)`` stopped growing, so page 23, 24, 40 all read « Page 22 ».
    * worse, a deep walk BACK drained the trail until ``prev_cursor`` became
      ``""``, which re-served **page 1's rows** under a page-1 label. The
      number was not merely stale, the data was wrong.

    ``total`` (None when the count could not be read) is what unlocks the
    « Fin » and « ±10 » controls; without it they are not rendered at all,
    so no unclamped offset can ever be requested.
    """
    resolved = (
        min(page, MAX_PAGE)
        if isinstance(page, int) and page > 0
        else len(trail) + (2 if cursor else 1)
    )
    # Widened: on a jumped-to page the trail is empty and there is no cursor,
    # yet « Précédent » must still be offered — it offset-reads page - 1.
    has_prev = bool(cursor) or resolved > 1
    prev_cursor = trail[-1] if trail else ""
    prev_trail = ",".join(trail[:-1])
    next_trail = ",".join(([*trail, cursor] if cursor else trail)[-MAX_TRAIL:])
    return {
        "mode": "cursor",
        "page": resolved,
        # False for an in-flight URL minted before `page` was carried: the
        # component then labels the position as approximate rather than
        # asserting a number it derived from a saturating trail.
        "page_exact": isinstance(page, int) and page > 0,
        "has_prev": has_prev,
        # NEVER derived from the total. This is the invariant that stops a
        # stale or failed count from stranding the reader mid-list: the
        # limit+1 fetch is the only thing that knows there is a next page.
        "has_next": bool(next_cursor),
        "next_cursor": next_cursor or "",
        "next_trail": next_trail,
        "prev_cursor": prev_cursor,
        "prev_trail": prev_trail,
        "url": url,
        "target": target,
        "extra_vals": extra_vals,
        **_nav(resolved, total, page_size),
    }
