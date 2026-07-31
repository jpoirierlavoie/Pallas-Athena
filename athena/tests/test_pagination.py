"""Tests for pagination helpers (legacy slicing + cursor mode)."""

from datetime import datetime, timezone

from pagination import (
    MAX_TRAIL,
    cursor_pagination,
    decode_cursor,
    encode_cursor,
    keyset_page,
    paginate,
    parse_trail,
)


# ── Legacy page mode ──────────────────────────────────────────────────────


def test_paginate_slices_and_flags():
    items = list(range(40))
    page_items, ctx = paginate(items, page=2, page_size=15)
    assert page_items == list(range(15, 30))
    assert ctx["mode"] == "page"
    assert ctx["has_prev"] is True
    assert ctx["has_next"] is True


def test_paginate_clamps_page_to_one():
    page_items, ctx = paginate([1, 2, 3], page=-5, page_size=2)
    assert page_items == [1, 2]
    assert ctx["page"] == 1
    assert ctx["has_prev"] is False


# ── Cursor encoding ───────────────────────────────────────────────────────


def test_cursor_roundtrip_plain_values():
    values = ["2025-001", "abc-123"]
    assert decode_cursor(encode_cursor(values)) == values


def test_cursor_roundtrip_datetime_preserves_tz():
    dt = datetime(2026, 6, 11, 14, 30, tzinfo=timezone.utc)
    out = decode_cursor(encode_cursor([dt, "id-1"]))
    assert out == [dt, "id-1"]
    assert out[0].tzinfo is not None


def test_decode_cursor_rejects_garbage():
    assert decode_cursor(None) is None
    assert decode_cursor("") is None
    assert decode_cursor("not!!valid@@base64") is None
    assert decode_cursor("Zm9v") is None  # valid b64, not JSON list


def test_cursor_token_is_urlsafe():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    token = encode_cursor([dt, "x/y+z"])
    assert all(c not in token for c in "+/="), token


# ── Trail handling ────────────────────────────────────────────────────────


def test_parse_trail_empty_and_bounded():
    assert parse_trail(None) == []
    assert parse_trail("") == []
    long = ",".join(f"c{i}" for i in range(MAX_TRAIL + 10))
    assert len(parse_trail(long)) == MAX_TRAIL


def test_cursor_pagination_first_page():
    ctx = cursor_pagination(
        cursor=None, trail=[], next_cursor="c1", url="/x", target="#rows"
    )
    assert ctx["mode"] == "cursor"
    assert ctx["page"] == 1
    assert ctx["has_prev"] is False
    assert ctx["has_next"] is True
    assert ctx["next_cursor"] == "c1"
    assert ctx["next_trail"] == ""  # page 1 has no cursor to push


def test_cursor_pagination_forward_then_back():
    # On page 3: cursor=c2, trail=[c1]
    ctx = cursor_pagination(
        cursor="c2", trail=["c1"], next_cursor="c3", url="/x", target="#rows"
    )
    assert ctx["page"] == 3
    # Forward: next page starts after c3, trail grows by current cursor
    assert ctx["next_cursor"] == "c3"
    assert ctx["next_trail"] == "c1,c2"
    # Back: pop the trail
    assert ctx["prev_cursor"] == "c1"
    assert ctx["prev_trail"] == ""
    assert ctx["has_prev"] is True


def test_cursor_pagination_last_page():
    ctx = cursor_pagination(
        cursor="c5", trail=["c1", "c2"], next_cursor=None, url="/x", target="#rows"
    )
    assert ctx["has_next"] is False
    assert ctx["next_cursor"] == ""


# ── keyset_page: paging a Python-materialized list (lot 2) ──────────────


def _rows(*specs):
    """specs are (date_iso, id) pairs."""
    return [{"date": d, "id": i} for d, i in specs]


_KEY = lambda r: (r["date"], r["id"])          # noqa: E731


def _walk(rows, limit, *, mutate=None):
    """Page all the way through, optionally mutating between pages.

    Returns the flattened list of ids served, in order.
    """
    served, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 50, "pagination did not terminate"
        page, cursor, _ = keyset_page(rows, _KEY, cursor, limit)
        served.extend(r["id"] for r in page)
        if cursor is None:
            return served
        if mutate:
            mutate(rows)
            mutate = None                      # once


def test_keyset_page_walks_everything_once():
    """The mandate's acceptance criterion: a full paged walk equals the
    direct count, with no duplicate and no omission."""
    rows = _rows(*[(f"2026-07-{d:02d}", f"id{d:02d}") for d in range(1, 32)])
    served = _walk(rows, 7)
    assert len(served) == 31
    assert len(set(served)) == 31              # no duplicate
    assert set(served) == {r["id"] for r in rows}   # no omission


def test_keyset_page_orders_descending_by_default():
    rows = _rows(("2026-07-01", "a"), ("2026-07-03", "c"), ("2026-07-02", "b"))
    page, _, _ = keyset_page(rows, _KEY, None, 3)
    assert [r["id"] for r in page] == ["c", "b", "a"]


def test_insertion_between_pages_neither_skips_nor_repeats():
    """The offset failure this exists to avoid. A row inserted anywhere
    must not shift rows already served nor hide unserved ones."""
    rows = _rows(*[(f"2026-07-{d:02d}", f"id{d:02d}") for d in range(1, 21)])

    def _insert(current):
        # Lands in the middle of the ordering, after page 1 was served.
        current.append({"date": "2026-07-15", "id": "NEW"})

    served = _walk(rows, 6, mutate=_insert)
    assert len(served) == len(set(served)), "a row was served twice"
    # Every ORIGINAL row is still served — the insertion cost nothing.
    assert {f"id{d:02d}" for d in range(1, 21)} <= set(served)


def test_ties_are_broken_by_the_id_component():
    """Several rows sharing a date is the common case (many time entries on
    one day). Without the id in the key a page boundary inside the tie group
    would drop or repeat rows."""
    rows = _rows(("2026-07-10", "a"), ("2026-07-10", "b"), ("2026-07-10", "c"),
                 ("2026-07-10", "d"))
    served = _walk(rows, 2)
    assert served == ["d", "c", "b", "a"]
    assert len(set(served)) == 4


def test_next_cursor_is_minted_from_the_last_returned_row():
    """Not from the end of the materialized window — otherwise rows between
    `limit` and the window edge are skipped on resume."""
    rows = _rows(("2026-07-03", "c"), ("2026-07-02", "b"), ("2026-07-01", "a"))
    page, cursor, has_more = keyset_page(rows, _KEY, None, 1)
    assert [r["id"] for r in page] == ["c"]
    assert has_more is True
    assert cursor == ["2026-07-03", "c"]       # the row just served
    page2, _, _ = keyset_page(rows, _KEY, cursor, 1)
    assert [r["id"] for r in page2] == ["b"]   # resumes immediately after


def test_last_page_reports_no_cursor():
    rows = _rows(("2026-07-02", "b"), ("2026-07-01", "a"))
    page, cursor, has_more = keyset_page(rows, _KEY, None, 5)
    assert len(page) == 2
    assert cursor is None and has_more is False


def test_empty_input_and_cursor_past_the_end():
    assert keyset_page([], _KEY, None, 10) == ([], None, False)
    rows = _rows(("2026-07-01", "a"))
    page, cursor, has_more = keyset_page(rows, _KEY, ["2026-01-01", "a"], 10)
    assert page == [] and cursor is None and has_more is False


def test_a_foreign_cursor_degrades_to_page_one_and_never_crashes():
    """A cursor minted by another tool decodes fine but has the wrong arity
    or types. It must behave like « no cursor », not raise and not silently
    position the reader somewhere arbitrary."""
    rows = _rows(("2026-07-02", "b"), ("2026-07-01", "a"))
    page, _, _ = keyset_page(rows, _KEY, [12345], 10)            # wrong type
    assert [r["id"] for r in page] == ["b", "a"]
    page, _, _ = keyset_page(rows, _KEY, ["x", "y", "z"], 10)    # wrong arity
    assert len(page) == 2


def test_zero_limit_is_not_a_silent_empty_last_page():
    """A caller passing limit=0 must not be told « that is everything »."""
    rows = _rows(("2026-07-01", "a"))
    page, cursor, has_more = keyset_page(rows, _KEY, None, 0)
    assert page == [] and cursor is None and has_more is True


def test_scalar_keys_work_like_single_element_tuples():
    """The trust register orders on `sequence` alone — a scalar key."""
    rows = [{"sequence": n} for n in (3, 1, 2)]
    page, cursor, _ = keyset_page(rows, lambda r: r["sequence"], None, 2)
    assert [r["sequence"] for r in page] == [3, 2]
    assert cursor == [2]
    page2, _, _ = keyset_page(rows, lambda r: r["sequence"], cursor, 2)
    assert [r["sequence"] for r in page2] == [1]


def test_ascending_mode_walks_the_other_way():
    rows = _rows(("2026-07-03", "c"), ("2026-07-01", "a"), ("2026-07-02", "b"))
    page, cursor, _ = keyset_page(rows, _KEY, None, 2, descending=False)
    assert [r["id"] for r in page] == ["a", "b"]
    page2, _, _ = keyset_page(rows, _KEY, cursor, 2, descending=False)
    assert [r["id"] for r in page2] == ["c"]


def test_cursor_round_trips_through_the_opaque_token():
    """keyset_page hands back raw key values; the wire carries the encoded
    token. Datetimes must survive that round trip or the resume silently
    restarts at page 1."""
    rows = [
        {"date": datetime(2026, 7, d, tzinfo=timezone.utc), "id": f"id{d}"}
        for d in (1, 2, 3)
    ]
    key = lambda r: (r["date"], r["id"])       # noqa: E731
    page, cursor, _ = keyset_page(rows, key, None, 1)
    token = encode_cursor(cursor)
    page2, _, _ = keyset_page(rows, key, decode_cursor(token), 1)
    assert page[0]["id"] == "id3"
    assert page2[0]["id"] == "id2"
