"""Tests for pagination helpers (legacy slicing + cursor mode)."""

from datetime import datetime, timezone

from pagination import (
    MAX_TRAIL,
    cursor_pagination,
    decode_cursor,
    encode_cursor,
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
