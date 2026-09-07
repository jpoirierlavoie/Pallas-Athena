"""Tests for pagination helpers (legacy slicing + cursor mode)."""

from datetime import datetime, timezone

from pagination import (
    MAX_PAGE,
    MAX_TRAIL,
    PAGE_SIZE,
    cursor_pagination,
    decode_cursor,
    encode_cursor,
    keyset_page,
    paginate,
    parse_trail,
    resolve_page,
    total_pages_of,
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


# ── Le total, les sauts, la position explicite (lot « navigation ») ────────


def test_total_pages_of_distinguishes_unknown_from_empty():
    """La garde de l'item 3 de la doctrine.

    Un compte qui a ÉCHOUÉ ne doit jamais se lire comme zéro : « Page 7 / 0 »
    est un mensonge assuré, et l'incident de juin 2026 est exactement une
    agrégation en échec dégradée en 0 plausible. Un ensemble vide — un vrai
    zéro, connu — vaut une page.
    """
    assert total_pages_of(None) is None
    assert total_pages_of(0) == 1
    assert total_pages_of(1) == 1
    assert total_pages_of(PAGE_SIZE) == 1
    assert total_pages_of(PAGE_SIZE + 1) == 2


def test_cursor_pagination_honours_an_explicit_page_past_max_trail():
    """La régression nommée : le libellé gelait à MAX_TRAIL + 2.

    Une fois la traîne saturée, len(trail) cessait de croître, donc les pages
    23, 24, 40 se lisaient toutes « Page 22 ».
    """
    trail = [f"c{i}" for i in range(MAX_TRAIL)]
    ctx = cursor_pagination(cursor="cX", trail=trail, next_cursor="cY",
                            url="/x", target="#r", page=25)
    assert ctx["page"] == 25
    assert ctx["page_exact"] is True
    # Sans `page` (URL en vol, forgée avant ce lot), l'ancien calcul survit —
    # et s'annonce comme approximatif au lieu d'affirmer un numéro.
    vieux = cursor_pagination(cursor="cX", trail=trail, next_cursor="cY",
                              url="/x", target="#r")
    assert vieux["page"] == MAX_TRAIL + 2
    assert vieux["page_exact"] is False


def test_cursor_pagination_never_clamps_the_label_to_a_stale_total():
    """À NE JAMAIS « corriger ».

    Sur le chemin curseur ce sont les lignes qui commandent, pas le total. Un
    compte périmé (trop bas) ne doit ni figer le libellé ni épingler Suivant,
    sinon il échoue le lecteur au milieu de la liste.
    """
    ctx = cursor_pagination(cursor="c1", trail=["c0"], next_cursor="c2",
                            url="/x", target="#r", page=7, total=45)
    assert ctx["page"] == 7            # 45 lignes = 3 pages, et pourtant 7
    assert ctx["next_page"] == 8
    assert ctx["has_next"] is True     # tiré du fetch limit+1, jamais du total


def test_cursor_pagination_has_prev_after_a_jump_emptied_the_trail():
    """Un saut n'a pas de chemin de retour dans la traîne — mais « Précédent »
    doit rester offert : il relit la page N-1 par déplacement absolu."""
    ctx = cursor_pagination(cursor=None, trail=[], next_cursor="c9",
                            url="/x", target="#r", page=40, total=900)
    assert ctx["has_prev"] is True
    assert ctx["prev_page"] == 39
    assert ctx["prev_cursor"] == ""


def test_a_missing_total_hides_every_jump_control():
    """L'épingle du repli ouvert : compte illisible → aucun saut proposé,
    donc aucun déplacement absolu non borné ne peut être demandé."""
    ctx = cursor_pagination(cursor="c1", trail=["c0"], next_cursor="c2",
                            url="/x", target="#r", page=7, total=None)
    assert ctx["has_total"] is False
    assert ctx["total_pages"] is None
    assert ctx["show_end"] is False
    assert ctx["show_jump"] is False
    assert ctx["jump_next_page"] is None
    # ⚠ Et le saut ARRIÈRE aussi : à la page 7 il vaut None de toute façon
    # (7 − 10 < 1), ce qui masquait le défaut. Une page PROFONDE le révèle —
    # un « −10 » offert sans total serait un déplacement absolu que
    # resolve_page refuse, donc un contrôle qui atterrit page 1 en mentant.
    profond = cursor_pagination(cursor="c1", trail=["c0"], next_cursor="c2",
                                url="/x", target="#r", page=15, total=None)
    assert profond["jump_prev_page"] is None
    assert profond["show_jump"] is False
    # « Début » ne demande AUCUN total (la page 1 est l'offset 0).
    assert profond["show_first"] is True
    # Un vrai zéro reste distinguable d'un compte absent.
    vide = cursor_pagination(cursor=None, trail=[], next_cursor=None,
                             url="/x", target="#r", page=1, total=0)
    assert vide["has_total"] is True and vide["total_pages"] == 1


def test_resolve_page_refuses_an_offset_read_without_a_total():
    """Le garde de coût. ?page=100000 sans total émettrait offset(1_499_985),
    soit ~1,5 M de lectures facturées et un SIGKILL de gunicorn à 60 s."""
    assert resolve_page(40, None, has_cursor=False) == (1, 0)
    assert resolve_page(None, None, has_cursor=False) == (1, 0)


def test_resolve_page_clamps_and_never_offsets_a_cursor_read():
    assert resolve_page(999, 5, has_cursor=False) == (5, 4 * PAGE_SIZE)
    assert resolve_page(3, 78, has_cursor=False) == (3, 2 * PAGE_SIZE)
    # Le curseur commande les lignes : le numéro est un libellé, sans offset,
    # et il n'est PAS ramené au total.
    assert resolve_page(7, 3, has_cursor=True) == (7, 0)
    # Plafond dur, indépendamment du total.
    assert resolve_page(999999, 100000, has_cursor=False)[0] == MAX_PAGE
    # Une entrée absurde retombe sur la page 1 plutôt que de lever.
    assert resolve_page(-3, 9, has_cursor=False) == (1, 0)
    assert resolve_page(None, 9, has_cursor=False) == (1, 0)


def test_jump_and_end_never_duplicate_their_neighbours():
    """Un ±10 écrêté serait un doublon sans étiquette de son voisin."""
    def nav(page, total_pages):
        return cursor_pagination(cursor="c", trail=[], next_cursor="c2",
                                 url="/x", target="#r", page=page,
                                 total=total_pages * PAGE_SIZE)
    # Page 5 sur 12 : reculer de 10 tomberait sur 1, ce que « Début » fait déjà.
    assert nav(5, 12)["jump_prev_page"] is None
    # …et avancer de 10 tomberait sur 15 → écrêté à 12, ce que « Fin » fait déjà.
    assert nav(5, 12)["jump_next_page"] is None
    # Assez loin des deux bords, les deux sauts se justifient.
    milieu = nav(15, 40)
    assert (milieu["jump_prev_page"], milieu["jump_next_page"]) == (5, 25)
    # « Début » duplique « Précédent » à la page 2 ; « Fin » duplique
    # « Suivant » à l'avant-dernière.
    assert nav(2, 40)["show_first"] is False and nav(3, 40)["show_first"] is True
    assert nav(39, 40)["show_end"] is False and nav(38, 40)["show_end"] is True


def test_paginate_clamps_a_page_beyond_the_total():
    """?page=999 sur une liste de 3 pages échouait le lecteur sur un tableau
    vide sous « Page 999 » — le seul paramètre de pagination qu'une main peut
    modifier dans la barre d'adresse."""
    items, ctx = paginate(list(range(40)), page=99, page_size=15)
    assert ctx["page"] == 3
    assert items == list(range(30, 40))
    assert ctx["has_next"] is False


def test_paginate_now_surfaces_the_total_it_always_had():
    _, ctx = paginate(list(range(40)), page=1, page_size=15)
    assert ctx["total"] == 40
    assert ctx["total_pages"] == 3
    assert ctx["has_total"] is True
    assert ctx["page_exact"] is True
