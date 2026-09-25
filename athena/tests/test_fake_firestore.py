"""Self-tests of the shared fake Firestore (tests/_fake_firestore.py).

A fake is only worth what it REFUSES. Every behaviour below is one the ad-hoc
fakes of this suite got wrong in the permissive direction — accepting what
production rejects — and each test pins the production answer so a later
edit of the fake cannot quietly relax it.

The client side is the real google-cloud-firestore client; several tests
therefore pin behaviour that comes from the real library (ReadAfterWriteError,
the transactional retry loop, TypeError at staging). They are here anyway:
they fail loudly the day a dependency bump changes that behaviour, which is
exactly when a test resting on the fake needs to know.
"""

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.api_core import exceptions as gexc  # noqa: E402
from google.cloud import firestore  # noqa: E402
from google.cloud.firestore_v1 import ReadAfterWriteError  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter, Or  # noqa: E402

from tests import _fake_firestore as ff  # noqa: E402
from tests._fake_firestore import FakeFirestore, install  # noqa: E402

UTC = timezone.utc


@pytest.fixture()
def db():
    return FakeFirestore()


def _doc(db, path):
    return db.document(path)


# ── The client is the REAL one ─────────────────────────────────────────────


def test_the_fake_is_a_real_client(db):
    """Model code needs no patching beyond `db`: isinstance checks and the
    whole client API are the production ones."""
    assert isinstance(db, firestore.Client)
    assert ff.transactional is firestore.transactional


def test_an_unmodelled_rpc_fails_loudly(db):
    server = db._firestore_api
    with pytest.raises(NotImplementedError, match="list_collection_ids"):
        server.list_collection_ids
    # …yet still reads as « absent » to a hasattr probe.
    assert not hasattr(server, "partition_query")


def test_install_patches_db_and_refuses_a_module_without_one(monkeypatch):
    import types

    mod = types.ModuleType("m")
    mod.db = object()
    fake = install(monkeypatch, mod)
    assert mod.db is fake
    with pytest.raises(AttributeError):
        install(monkeypatch, types.ModuleType("no_db"))


# ── Documents: set / get / codec ───────────────────────────────────────────


def test_set_then_get_round_trips_through_the_real_codec(db):
    wr = _doc(db, "a/b").set({
        "n": 1, "when": datetime(2026, 3, 1, 12, 0), "tags": ("x", "y"),
        "nested": {"k": [1, {"deep": [2, 3]}]},
    })
    snap = _doc(db, "a/b").get()
    assert snap.exists
    data = snap.to_dict()
    # A naive datetime is stored as UTC and comes back AWARE — the trap a
    # naive-vs-aware comparison falls into in production.
    assert data["when"] == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    assert data["when"].tzinfo is not None
    assert data["tags"] == ["x", "y"]  # a tuple is an array
    assert data["nested"] == {"k": [1, {"deep": [2, 3]}]}
    assert snap.update_time == wr.update_time
    assert snap.create_time == wr.update_time


def test_a_value_the_client_cannot_encode_raises_at_staging(db):
    for bad in (date(2026, 1, 1), object()):
        with pytest.raises(TypeError, match="Cannot convert to a Firestore Value"):
            _doc(db, "a/b").set({"d": bad})
    assert db.peek("a/b") is None


def test_a_missing_document_reads_as_not_existing(db):
    snap = _doc(db, "a/nope").get()
    assert not snap.exists
    assert snap.to_dict() is None


def test_set_replaces_and_merge_merges_deeply(db):
    _doc(db, "a/b").set({"x": 1, "m": {"p": 1, "q": 2}})
    _doc(db, "a/b").set({"m": {"p": 9}}, merge=True)
    assert db.peek("a/b") == {"x": 1, "m": {"p": 9, "q": 2}}
    _doc(db, "a/b").set({"only": True})
    assert db.peek("a/b") == {"only": True}


def test_update_dotted_path_and_map_replacement(db):
    _doc(db, "a/b").set({"m": {"p": 1, "q": 2}, "z": 0})
    _doc(db, "a/b").update({"m.p": 5})
    assert db.peek("a/b")["m"] == {"p": 5, "q": 2}
    _doc(db, "a/b").update({"m": {"r": 1}})  # a map value REPLACES the map
    assert db.peek("a/b")["m"] == {"r": 1}


def test_update_on_a_missing_document_is_not_found(db):
    with pytest.raises(gexc.NotFound):
        _doc(db, "a/nope").update({"x": 1})
    assert db.peek("a/nope") is None


def test_client_side_refusals_of_the_real_library(db):
    with pytest.raises(ValueError, match="empty document"):
        _doc(db, "a/b").update({})
    with pytest.raises(ValueError, match="DELETE_FIELD"):
        _doc(db, "a/b").set({"x": firestore.DELETE_FIELD})
    with pytest.raises(ValueError):
        db.collection("a").document("b/c")  # an odd path is a collection


def test_sentinels_delete_field_server_timestamp_and_transforms(db):
    _doc(db, "a/b").set({"x": 1, "n": 2, "arr": [1, 2], "gone": "?"})
    wr = _doc(db, "a/b").update({
        "gone": firestore.DELETE_FIELD,
        "stamp": firestore.SERVER_TIMESTAMP,
        "n": firestore.Increment(3),
        "fresh": firestore.Increment(4),
        "arr": firestore.ArrayUnion([2, 3]),
    })
    data = db.peek("a/b")
    assert "gone" not in data
    assert data["stamp"] == wr.update_time  # the COMMIT time
    assert data["n"] == 5 and data["fresh"] == 4
    assert data["arr"] == [1, 2, 3]
    _doc(db, "a/b").update({"arr": firestore.ArrayRemove([1])})
    assert db.peek("a/b")["arr"] == [2, 3]


# ── create() and preconditions ─────────────────────────────────────────────


def test_create_refuses_an_existing_document_with_already_exists(db):
    """AlreadyExists — which IS a Conflict — never a silent overwrite."""
    _doc(db, "a/b").create({"v": 1})
    with pytest.raises(gexc.AlreadyExists) as exc:
        _doc(db, "a/b").create({"v": 2})
    assert isinstance(exc.value, gexc.Conflict)
    assert db.peek("a/b") == {"v": 1}


def test_update_times_are_monotonic_and_create_time_survives(db):
    first = _doc(db, "a/b").set({"v": 1})
    second = _doc(db, "a/b").update({"v": 2})
    assert second.update_time > first.update_time
    snap = _doc(db, "a/b").get()
    assert snap.create_time == first.update_time
    assert snap.update_time == second.update_time


def test_a_stale_last_update_time_refuses_delete_and_update(db):
    stale = _doc(db, "a/b").set({"v": 1}).update_time
    _doc(db, "a/b").update({"v": 2})
    option = db.write_option(last_update_time=stale)
    with pytest.raises(gexc.FailedPrecondition):
        _doc(db, "a/b").delete(option=option)
    with pytest.raises(gexc.FailedPrecondition):
        _doc(db, "a/b").update({"v": 3}, option=option)
    assert db.peek("a/b") == {"v": 2}

    fresh = db.write_option(last_update_time=_doc(db, "a/b").get().update_time)
    _doc(db, "a/b").delete(option=fresh)
    assert db.peek("a/b") is None


def test_last_update_time_on_a_missing_document_fails_the_precondition(db):
    ts = _doc(db, "a/b").set({"v": 1}).update_time
    db.external_delete("a/b")
    with pytest.raises(gexc.FailedPrecondition):
        _doc(db, "a/b").delete(option=db.write_option(last_update_time=ts))


def test_exists_precondition_and_plain_delete_of_a_missing_document(db):
    _doc(db, "a/nope").delete()  # no precondition: succeeds silently
    with pytest.raises(gexc.NotFound):
        _doc(db, "a/nope").delete(option=db.write_option(exists=True))


def test_a_write_that_changes_nothing_keeps_the_previous_update_time(db):
    """The documented WriteResult contract: « If the write did not actually
    change the document, this will be the previous update_time. » A fake
    that bumped it anyway would make a `last_update_time` precondition — the
    idempotency claim's release, a conditional delete — refuse where
    production accepts, and a test would pin a guarantee production lacks."""
    first = _doc(db, "a/b").set({"v": 1, "m": {"p": [1, 2]}})
    again = _doc(db, "a/b").set({"m": {"p": [1, 2]}, "v": 1})  # key order is irrelevant
    assert again.update_time == first.update_time
    assert db.stored_update_time("a/b") == first.update_time
    same_field = _doc(db, "a/b").update({"v": 1})
    assert same_field.update_time == first.update_time
    # …so a precondition captured before those writes still holds.
    _doc(db, "a/b").update({"v": 2}, option=db.write_option(last_update_time=first.update_time))
    assert db.peek("a/b")["v"] == 2


def test_a_type_change_is_a_change_even_where_python_sees_equality(db):
    """`True == 1` in Python; a stored bool and a stored int are different
    Firestore values, so the write DOES change the document."""
    first = _doc(db, "a/b").set({"flag": True})
    second = _doc(db, "a/b").set({"flag": 1})
    assert second.update_time > first.update_time


def test_a_server_timestamp_always_changes_the_document(db):
    first = _doc(db, "a/b").set({"at": firestore.SERVER_TIMESTAMP})
    second = _doc(db, "a/b").set({"at": firestore.SERVER_TIMESTAMP})
    assert second.update_time > first.update_time


def test_an_identical_external_write_neither_races_nor_invalidates(db):
    ts = _doc(db, "a/b").set({"v": 1}).update_time
    db.external_write("a/b", {"v": 1})
    assert db.stored_update_time("a/b") == ts
    _doc(db, "a/b").delete(option=db.write_option(last_update_time=ts))
    assert db.peek("a/b") is None


def test_an_external_writer_invalidates_a_captured_update_time(db):
    ts = _doc(db, "a/b").set({"v": 1}).update_time
    db.external_write("a/b", {"v": "other process"})
    with pytest.raises(gexc.FailedPrecondition):
        _doc(db, "a/b").update({"v": 2}, option=db.write_option(last_update_time=ts))


# ── Invalid ids and nested arrays ──────────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["", ".", "..", "__reserved__"])
def test_an_invalid_document_id_is_refused_at_rpc_time(db, bad_id):
    """The client builds these references without complaint (an empty
    `serie_id` would); the server refuses them — reads and writes alike."""
    ref = db.collection("a").document(bad_id)
    with pytest.raises(gexc.InvalidArgument):
        ref.set({"v": 1})
    with pytest.raises(gexc.InvalidArgument):
        ref.get()


def test_nested_arrays_are_refused_and_nothing_is_written(db):
    with pytest.raises(gexc.InvalidArgument, match="[Nn]ested arrays"):
        _doc(db, "a/b").set({"rows": [[1, 2], [3]]})
    assert db.peek("a/b") is None
    with pytest.raises(gexc.InvalidArgument):
        _doc(db, "a/b").set({"m": {"deep": [1, [2]]}})
    with pytest.raises(gexc.InvalidArgument):
        db.seed("a/b", {"rows": [[1]]})
    _doc(db, "a/b").set({"v": 0})
    with pytest.raises(gexc.InvalidArgument):
        _doc(db, "a/b").update({"arr": firestore.ArrayUnion([[1]])})
    assert db.peek("a/b") == {"v": 0}


def test_an_array_of_maps_holding_arrays_is_legal(db):
    _doc(db, "a/b").set({"rows": [{"cells": [1, 2]}, {"cells": []}]})
    assert db.peek("a/b") == {"rows": [{"cells": [1, 2]}, {"cells": []}]}


# ── Batches ────────────────────────────────────────────────────────────────


def test_a_batch_is_atomic(db):
    _doc(db, "a/keep").set({"v": 0})
    batch = db.batch()
    batch.set(_doc(db, "a/x"), {"v": 1})
    batch.update(_doc(db, "a/keep"), {"v": 2})
    batch.update(_doc(db, "a/missing"), {"v": 3})  # fails the precondition
    with pytest.raises(gexc.NotFound):
        batch.commit()
    assert db.peek("a/x") is None
    assert db.peek("a/keep") == {"v": 0}


def test_a_batch_commits_every_write_at_one_update_time(db):
    batch = db.batch()
    batch.set(_doc(db, "a/x"), {"v": 1})
    batch.create(_doc(db, "a/y"), {"v": 2})
    results = batch.commit()
    assert len(results) == 2
    assert results[0].update_time == results[1].update_time
    assert db.stored_update_time("a/x") == db.stored_update_time("a/y")
    assert db.commits[-1].ops == (("set", "a/x"), ("create", "a/y"))


def test_nothing_staged_in_a_batch_is_visible_before_commit(db):
    batch = db.batch()
    batch.set(_doc(db, "a/x"), {"v": 1})
    assert not _doc(db, "a/x").get().exists
    batch.commit()
    assert _doc(db, "a/x").get().exists


def test_a_batch_over_the_write_cap_is_refused_whole():
    db = FakeFirestore(max_writes_per_commit=3)
    batch = db.batch()
    for i in range(4):
        batch.set(_doc(db, f"a/d{i}"), {"i": i})
    with pytest.raises(gexc.InvalidArgument, match="maximum 3 writes"):
        batch.commit()
    assert db.peek_collection("a") == {}


def test_there_is_no_write_count_cap_by_default():
    """Firestore release notes, 2023-03-29: « Firestore no longer limits the
    number of writes that can be passed to a Commit operation or performed
    in a transaction. Previously, the limit was 500. » A fake that still
    refused the 501st write would let a test assert a refusal production
    never gives."""
    db = FakeFirestore()
    assert db.max_writes_per_commit is None
    batch = db.batch()
    for i in range(501):
        batch.set(_doc(db, f"a/d{i}"), {"i": i})
    batch.commit()
    assert len(db.peek_collection("a")) == 501


def test_a_commit_hook_can_fail_a_commit_and_nothing_applies(db):
    def boom(info):
        raise gexc.ServiceUnavailable("injected")

    remove = db.add_commit_hook(boom)
    with pytest.raises(gexc.ServiceUnavailable):
        _doc(db, "a/x").set({"v": 1})
    assert db.peek("a/x") is None
    remove()
    _doc(db, "a/x").set({"v": 1})
    assert db.peek("a/x") == {"v": 1}


# ── Transactions ───────────────────────────────────────────────────────────


def test_transaction_writes_are_buffered_until_commit(db):
    _doc(db, "a/x").set({"n": 1})
    seen_outside = {}

    @firestore.transactional
    def run(txn):
        snap = _doc(db, "a/x").get(transaction=txn)
        txn.update(snap.reference, {"n": snap.get("n") + 1})
        txn.set(_doc(db, "a/y"), {"new": True})
        # Another reader (no transaction) still sees the committed state.
        seen_outside["x"] = db.peek("a/x")
        seen_outside["y"] = db.peek("a/y")
        return "done"

    assert run(db.transaction()) == "done"
    assert seen_outside == {"x": {"n": 1}, "y": None}
    assert db.peek("a/x") == {"n": 2}
    assert db.peek("a/y") == {"new": True}


def test_a_transactional_read_after_a_staged_write_raises(db):
    _doc(db, "a/x").set({"n": 1})

    @firestore.transactional
    def run(txn):
        txn.set(_doc(db, "a/y"), {"v": 1})
        _doc(db, "a/x").get(transaction=txn)

    with pytest.raises(ReadAfterWriteError):
        run(db.transaction())
    assert db.peek("a/y") is None  # rolled back


def test_an_exception_inside_the_transaction_rolls_everything_back(db):
    _doc(db, "a/x").set({"n": 1})

    @firestore.transactional
    def run(txn):
        txn.update(_doc(db, "a/x"), {"n": 99})
        raise RuntimeError("guard refused")

    with pytest.raises(RuntimeError):
        run(db.transaction())
    assert db.peek("a/x") == {"n": 1}


def test_a_read_through_a_transaction_not_in_progress_is_refused(db):
    with pytest.raises(ValueError, match="not in progress"):
        _doc(db, "a/x").get(transaction=db.transaction())


def test_reads_record_whether_they_went_through_the_transaction(db):
    _doc(db, "a/x").set({"n": 1})
    _doc(db, "a/y").set({"n": 1})
    db.reset_logs()

    @firestore.transactional
    def run(txn):
        _doc(db, "a/x").get(transaction=txn)
        _doc(db, "a/y").get()  # the bug class: a read OUTSIDE the transaction
        list(db.collection("a").where(filter=FieldFilter("n", "==", 1)).stream(transaction=txn))

    run(db.transaction())
    by_rpc = [(r.rpc, r.paths, r.transactional) for r in db.reads]
    assert ("batch_get_documents", ("a/x",), True) in by_rpc
    assert ("batch_get_documents", ("a/y",), False) in by_rpc
    assert ("run_query", ("a/x", "a/y"), True) in by_rpc
    assert [r.paths for r in db.reads_outside_transactions()] == [("a/y",)]


def test_a_stale_read_aborts_and_the_real_decorator_retries_on_fresh_data(db):
    """The lost-update case: another writer lands between the read and the
    commit. The commit aborts, `transactional` re-runs the function, and the
    second attempt computes from the fresh value."""
    db.seed("a/counter", {"n": 1})
    attempts = []

    def concurrent_writer(info):
        if info.index == 0:  # the transaction's FIRST commit attempt only
            db.external_write("a/counter", {"n": 10})

    db.add_commit_hook(concurrent_writer)

    @firestore.transactional
    def increment(txn):
        snap = _doc(db, "a/counter").get(transaction=txn)
        attempts.append(snap.get("n"))
        txn.update(snap.reference, {"n": snap.get("n") + 1})

    increment(db.transaction())
    assert attempts == [1, 10]
    assert db.peek("a/counter") == {"n": 11}


def test_a_transaction_that_keeps_losing_the_race_gives_up(db):
    db.seed("a/counter", {"n": 0})
    # A value the counter never held before: an identical write is a no-op
    # that races nothing (see the no-op tests below).
    db.add_commit_hook(
        lambda info: info.transaction and db.external_write("a/counter", {"n": 1000 + info.index})
    )

    @firestore.transactional
    def increment(txn):
        snap = _doc(db, "a/counter").get(transaction=txn)
        txn.update(snap.reference, {"n": snap.get("n") + 1})

    with pytest.raises(ValueError, match="attempts"):
        increment(db.transaction(max_attempts=2))


def test_a_phantom_in_a_transactional_query_aborts(db):
    db.seed("a/x", {"open": True})
    calls = []

    def insert_phantom(info):
        if info.index == 0:
            db.external_write("a/phantom", {"open": True})

    db.add_commit_hook(insert_phantom)

    @firestore.transactional
    def count_open(txn):
        rows = list(
            db.collection("a").where(filter=FieldFilter("open", "==", True)).stream(transaction=txn)
        )
        calls.append(len(rows))
        txn.set(_doc(db, "a/summary"), {"open": len(rows)})

    count_open(db.transaction())
    assert calls == [1, 2]
    assert db.peek("a/summary") == {"open": 2}


def test_a_read_only_transaction_refuses_writes(db):
    @firestore.transactional
    def run(txn):
        txn.set(_doc(db, "a/x"), {"v": 1})

    with pytest.raises(ValueError):
        run(db.transaction(read_only=True))
    assert db.peek("a/x") is None


# ── Queries ────────────────────────────────────────────────────────────────


@pytest.fixture()
def rows(db):
    db.seed_collection("t", {
        "a": {"n": 3, "s": "x", "tags": ["p", "q"], "v": None},
        "b": {"n": 1, "s": "y", "tags": ["q"]},
        "c": {"n": 2, "s": "x", "tags": [], "v": 5},
        "d": {"n": "not-a-number", "s": "x"},
        "e": {"s": "z"},  # no `n` at all
    })
    return db


def _ids(query, **kw):
    return [s.id for s in query.stream(**kw)]


def test_unordered_queries_return_document_id_order(rows):
    assert _ids(rows.collection("t")) == ["a", "b", "c", "d", "e"]


def test_equality_range_and_type_semantics(rows):
    t = rows.collection("t")
    assert _ids(t.where(filter=FieldFilter("s", "==", "x"))) == ["a", "c", "d"]
    # A range filter matches only the operand's TYPE: « d » (a string) is out.
    assert _ids(t.where(filter=FieldFilter("n", ">=", 2))) == ["c", "a"]
    assert _ids(t.where(filter=FieldFilter("n", "in", [1, 2]))) == ["b", "c"]
    assert _ids(t.where(filter=FieldFilter("tags", "array_contains", "q"))) == ["a", "b"]
    assert _ids(t.where(filter=FieldFilter("tags", "array_contains_any", ["p", "zz"]))) == ["a"]
    assert _ids(t.where(filter=Or([
        FieldFilter("s", "==", "y"), FieldFilter("s", "==", "z"),
    ]))) == ["b", "e"]


def test_equality_on_none_matches_only_a_stored_null(rows):
    """CLAUDE.md: « a Firestore equality on None does not match ABSENT
    keys » — every legacy document lacking an additive field."""
    t = rows.collection("t")
    assert _ids(t.where(filter=FieldFilter("v", "==", None))) == ["a"]
    assert _ids(t.where(filter=FieldFilter("v", "!=", None))) == ["c"]


def test_not_equal_and_not_in_exclude_missing_fields(rows):
    t = rows.collection("t")
    assert set(_ids(t.where(filter=FieldFilter("s", "!=", "x")))) == {"b", "e"}
    assert set(_ids(t.where(filter=FieldFilter("s", "not-in", ["x", "y"])))) == {"e"}


def test_order_by_excludes_documents_missing_the_field(rows):
    t = rows.collection("t")
    # Numbers order before strings (Firestore type order); « e » has no n.
    assert _ids(t.order_by("n")) == ["b", "c", "a", "d"]
    assert _ids(t.order_by("n", direction=firestore.Query.DESCENDING)) == ["d", "a", "c", "b"]


def test_ties_break_on_the_document_name_in_the_last_direction(db):
    db.seed_collection("t", {"a": {"k": 1}, "b": {"k": 1}, "c": {"k": 0}})
    t = db.collection("t")
    assert _ids(t.order_by("k")) == ["c", "a", "b"]
    assert _ids(t.order_by("k", direction=firestore.Query.DESCENDING)) == ["b", "a", "c"]


def test_limit_offset_and_cursors(rows):
    by_n = rows.collection("t").where(filter=FieldFilter("n", ">", 0)).order_by("n")
    assert _ids(by_n.limit(2)) == ["b", "c"]
    assert _ids(by_n.offset(1).limit(1)) == ["c"]
    assert _ids(by_n.start_after({"n": 1})) == ["c", "a"]
    assert _ids(by_n.start_at({"n": 2})) == ["c", "a"]
    assert _ids(by_n.end_before({"n": 3})) == ["b", "c"]
    last = next(iter(by_n.limit(1).stream()))
    assert _ids(by_n.start_after(last)) == ["c", "a"]  # snapshot cursor


def test_stream_is_an_iterator_not_a_list(rows):
    """Production `stream()` is a generator: `len()` on it is a bug."""
    stream = rows.collection("t").stream()
    with pytest.raises(TypeError):
        len(stream)
    assert isinstance(rows.collection("t").get(), list)


def test_disjunction_limits_are_enforced(rows):
    t = rows.collection("t")
    with pytest.raises(gexc.InvalidArgument):
        list(t.where(filter=FieldFilter("n", "in", list(range(31)))).stream())
    with pytest.raises(gexc.InvalidArgument):
        list(t.where(filter=FieldFilter("n", "not-in", list(range(11)))).stream())


def test_subcollections_are_independent_of_their_parent(db):
    _doc(db, "p/1").set({"v": 1})
    _doc(db, "p/1/kids/k1").set({"v": 2})
    _doc(db, "p/2/kids/k2").set({"v": 3})  # parent never created
    assert _ids(db.collection("p")) == ["1"]
    assert _ids(db.collection("p/1/kids")) == ["k1"]
    assert sorted(s.id for s in db.collection_group("kids").stream()) == ["k1", "k2"]
    _doc(db, "p/1").delete()
    assert db.peek("p/1/kids/k1") == {"v": 2}  # NOT cascaded
    snap = next(iter(db.collection_group("kids").where(filter=FieldFilter("v", "==", 2)).stream()))
    assert snap.reference.parent.parent.id == "1"


def test_projection_returns_only_the_selected_fields(rows):
    snap = next(iter(rows.collection("t").select(["s"]).limit(1).stream()))
    assert snap.to_dict() == {"s": "x"}


def test_count_sum_and_avg_aggregations(rows):
    t = rows.collection("t")
    values = {}
    for item in t.count(alias="n").get():
        for agg in item:
            values[agg.alias] = agg.value
    assert values == {"n": 5}
    result = t.sum("n", alias="total").get()[0][0]
    assert result.value == 6  # the string « d » and the absent « e » do not count
    assert t.where(filter=FieldFilter("s", "==", "x")).avg("n", alias="m").get()[0][0].value == 2.5


def test_get_all_returns_missing_documents_and_no_guaranteed_order(db):
    db.seed("a/x", {"v": 1})
    refs = [_doc(db, "a/x"), _doc(db, "a/missing")]
    snaps = list(db.get_all(refs))
    assert {s.id: s.exists for s in snaps} == {"x": True, "missing": False}
    # The service documents NO order: the fake answers in reverse so a caller
    # that zips results against its request list fails here, not in prod.
    assert [s.id for s in snaps] == ["missing", "x"]


# ── Setup / assertion helpers ──────────────────────────────────────────────


def test_seed_and_peek_bypass_the_logs(db):
    db.seed("a/x", {"v": 1})
    assert db.peek("a/x") == {"v": 1}
    assert db.reads == [] and db.commits == []
    got = db.peek("a/x")
    got["v"] = 2
    assert db.peek("a/x") == {"v": 1}  # peek hands back a copy


def test_seed_goes_through_the_real_codec(db):
    with pytest.raises(TypeError):
        db.seed("a/x", {"d": date(2026, 1, 1)})
    db.seed("a/x", {"when": datetime(2026, 1, 1)})
    assert db.peek("a/x")["when"].tzinfo is not None
