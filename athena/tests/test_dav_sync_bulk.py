"""The bulk DAV sync primitives of dav/sync.py — their first tests.

`record_tombstones_bulk`, `bump_ctag_in_batch` and `record_tombstones_in_batch`
carry the closing-dossier drain (services/dossier_dav.py since lot 4a —
routes/dossiers.py before) and the series writes of models/hearing.py, and
later lots build on them (the series delete fix, the dossier-status drain).
They had no test at all. Everything here runs against
the shared fake Firestore (tests/_fake_firestore.py), whose batches are
atomic and whose commit log is the SERVER's view — so « one read », « chunked
commits » and « staged, not committed » are asserted on what the store
actually received, never on a dict handed to a mock.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from google.api_core import exceptions as gexc  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync

from tests._fake_firestore import install  # noqa: E402

NAME = "dossier:d1"
SYNC_DOC = f"dav_sync/{NAME}"
TOMBSTONES = f"{SYNC_DOC}/tombstones"


@pytest.fixture()
def fake(monkeypatch):
    fake = install(monkeypatch, dav_sync)
    fake.seed(SYNC_DOC, {"ctag": "ctag-0", "sync_token": "ctag-0"})
    return fake


@pytest.fixture()
def ctag_calls(monkeypatch):
    """Count get_ctag calls while keeping the REAL implementation."""
    calls = []
    real = dav_sync.get_ctag

    def spy(name):
        calls.append(name)
        return real(name)

    monkeypatch.setattr(dav_sync, "get_ctag", spy)
    return calls


def _tombstone_commits(fake):
    return [c for c in fake.commits if all(p.startswith(TOMBSTONES + "/") for _k, p in c.ops)]


# ── record_tombstones_bulk ─────────────────────────────────────────────────


def test_no_commit_exceeds_the_chunk_even_under_an_enforced_ceiling(monkeypatch):
    """Firestore dropped its 500-writes-per-commit limit on 2023-03-29, so
    the fake enforces none by default and the « 500 » in dav/sync.py's comment
    is the retired limit. The chunk still bounds each commit (request size,
    one failure's blast radius); a store whose ceiling IS the chunk accepts
    the whole drain, which it could not if one commit carried more."""
    from tests._fake_firestore import FakeFirestore

    chunk = dav_sync._BATCH_CHUNK
    fake = install(monkeypatch, dav_sync, fake=FakeFirestore(max_writes_per_commit=chunk))
    fake.seed(SYNC_DOC, {"ctag": "ctag-0", "sync_token": "ctag-0"})
    ids = [f"r{i:04d}" for i in range(2 * chunk + 1)]

    dav_sync.record_tombstones_bulk(NAME, ids)

    assert len(fake.peek_collection(TOMBSTONES)) == len(ids)
    assert max(len(c.ops) for c in fake.commits) == chunk


def test_bulk_reads_the_ctag_once_and_commits_in_chunks(fake, ctag_calls):
    chunk = dav_sync._BATCH_CHUNK
    ids = [f"r{i:04d}" for i in range(2 * chunk + 1)]

    dav_sync.record_tombstones_bulk(NAME, ids)

    assert ctag_calls == [NAME]  # ONE read, not one per resource…
    # …and the store saw exactly that: one lookup of the sync document.
    assert [(r.rpc, r.paths) for r in fake.reads] == [("batch_get_documents", (SYNC_DOC,))]
    sizes = [len(c.ops) for c in _tombstone_commits(fake)]
    assert sizes == [chunk, chunk, 1]
    assert all(kind == "set" for c in fake.commits for kind, _p in c.ops)
    stored = fake.peek_collection(TOMBSTONES)
    assert sorted(stored) == ids
    # Every tombstone carries the token that was current when it was written.
    assert {t["sync_token"] for t in stored.values()} == {"ctag-0"}


def test_bulk_does_not_bump_the_ctag(fake, ctag_calls):
    """A drain records deletions; the bump is the caller's decision (and the
    caller's to stage atomically). The sync document is left untouched."""
    before = fake.stored_update_time(SYNC_DOC)
    dav_sync.record_tombstones_bulk(NAME, ["a", "b"])
    assert fake.peek(SYNC_DOC) == {"ctag": "ctag-0", "sync_token": "ctag-0"}
    assert fake.stored_update_time(SYNC_DOC) == before
    assert not any(p == SYNC_DOC for c in fake.commits for _k, p in c.ops)


def test_bulk_with_no_ids_does_nothing(fake, ctag_calls):
    dav_sync.record_tombstones_bulk(NAME, [])
    assert ctag_calls == []
    assert fake.reads == [] and fake.commits == []


def test_bulk_on_a_never_synced_collection_initialises_the_ctag_once(monkeypatch, ctag_calls):
    """get_ctag's documented lazy initialisation: a collection no client has
    ever synced gets its first token — once — and the tombstones carry it."""
    fake = install(monkeypatch, dav_sync)
    dav_sync.record_tombstones_bulk("dossier:fresh", ["x"])
    sync = fake.peek("dav_sync/dossier:fresh")
    assert sync["ctag"] and sync["ctag"] == sync["sync_token"]
    assert fake.peek("dav_sync/dossier:fresh/tombstones/x")["sync_token"] == sync["ctag"]
    assert ctag_calls == ["dossier:fresh"]


def test_a_failed_chunk_propagates_and_later_chunks_never_run(fake, ctag_calls):
    """Failures PROPAGATE — a caller deleting on the strength of this must not
    mistake a write failure for a completed drain. Chunks are NOT atomic with
    one another: what committed before the failure stays committed, which is
    why the caller must treat the whole drain as failed and re-runnable."""
    chunk = dav_sync._BATCH_CHUNK
    ids = [f"r{i:04d}" for i in range(2 * chunk + 1)]
    tombstone_commits = []

    def fail_second_chunk(info):
        if all(p.startswith(TOMBSTONES + "/") for _k, p in info.ops):
            tombstone_commits.append(info.index)
            if len(tombstone_commits) == 2:
                raise gexc.ServiceUnavailable("injected")

    fake.add_commit_hook(fail_second_chunk)
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.record_tombstones_bulk(NAME, ids)

    assert len(tombstone_commits) == 2  # the third chunk was never attempted
    assert sorted(fake.peek_collection(TOMBSTONES)) == ids[:chunk]


# ── bump_ctag_in_batch / record_tombstones_in_batch ────────────────────────


def test_bump_in_batch_stages_without_committing_or_reading(fake):
    batch = fake.batch()
    token = dav_sync.bump_ctag_in_batch(batch, NAME)

    assert token and token != "ctag-0"
    assert fake.peek(SYNC_DOC)["ctag"] == "ctag-0"  # staged, not applied
    assert fake.reads == [] and fake.commits == []

    batch.commit()
    assert fake.peek(SYNC_DOC)["ctag"] == token
    assert fake.peek(SYNC_DOC)["sync_token"] == token


def test_tombstones_in_batch_stage_under_the_given_token(fake):
    batch = fake.batch()
    dav_sync.record_tombstones_in_batch(batch, NAME, ["a", "b"], "tok-9")

    assert fake.peek_collection(TOMBSTONES) == {}  # nothing before commit
    assert fake.reads == [] and fake.commits == []

    batch.commit()
    stored = fake.peek_collection(TOMBSTONES)
    assert sorted(stored) == ["a", "b"]
    assert {t["sync_token"] for t in stored.values()} == {"tok-9"}
    assert all(t["deleted_at"].tzinfo is not None for t in stored.values())


def test_bump_and_tombstones_commit_together_or_not_at_all(fake):
    """The reason these two exist: a delete that commits its documents but
    not its tombstones and bump leaves the resources on the phone for good.
    Staged with a write that fails its precondition, NOTHING applies."""
    fake.seed("hearings/h1", {"title": "x"})
    batch = fake.batch()
    batch.delete(fake.document("hearings/h1"))
    token = dav_sync.bump_ctag_in_batch(batch, NAME)
    dav_sync.record_tombstones_in_batch(batch, NAME, ["h1"], token)
    batch.update(fake.document("hearings/missing"), {"x": 1})  # NotFound

    with pytest.raises(gexc.NotFound):
        batch.commit()
    assert fake.peek("hearings/h1") == {"title": "x"}
    assert fake.peek(SYNC_DOC)["ctag"] == "ctag-0"
    assert fake.peek_collection(TOMBSTONES) == {}


def test_bump_and_tombstones_share_one_commit_when_it_succeeds(fake):
    fake.seed("hearings/h1", {"title": "x"})
    batch = fake.batch()
    batch.delete(fake.document("hearings/h1"))
    token = dav_sync.bump_ctag_in_batch(batch, NAME)
    dav_sync.record_tombstones_in_batch(batch, NAME, ["h1"], token)
    batch.commit()

    assert len(fake.commits) == 1
    assert fake.peek("hearings/h1") is None
    assert fake.peek(SYNC_DOC)["ctag"] == token
    assert fake.peek(f"{TOMBSTONES}/h1")["sync_token"] == token


# ── bump=True — the drain's shape (lot 4a) ─────────────────────────────────


def test_bulk_with_bump_is_one_atomic_commit_up_to_the_chunk_minus_one(fake, ctag_calls):
    """The tombstones AND the bump in ONE commit, with no token read: the
    tombstones carry the NEW token."""
    ids = [f"r{i:04d}" for i in range(dav_sync._BATCH_CHUNK - 1)]
    token = dav_sync.record_tombstones_bulk(NAME, ids, bump=True)

    assert ctag_calls == [] and fake.reads == []
    assert len(fake.commits) == 1
    assert fake.peek(SYNC_DOC)["ctag"] == token != "ctag-0"
    stored = fake.peek_collection(TOMBSTONES)
    assert sorted(stored) == ids
    assert {t["sync_token"] for t in stored.values()} == {token}


def test_bulk_with_bump_bumps_even_with_no_ids(fake):
    token = dav_sync.record_tombstones_bulk(NAME, [], bump=True)
    assert fake.peek(SYNC_DOC)["ctag"] == token
    assert len(fake.commits) == 1 and fake.peek_collection(TOMBSTONES) == {}


def test_a_failed_chunk_with_bump_leaves_the_token_unchanged(fake):
    """The bump rides in the FINAL chunk: a failure before it means no client
    can record a token covering half a drain — the caller re-runs it all."""
    ids = [f"r{i:04d}" for i in range(dav_sync._BATCH_CHUNK + 10)]
    seen = []

    def fail_final(info):
        seen.append(info.index)
        if any(p == SYNC_DOC for _k, p in info.ops):
            raise gexc.ServiceUnavailable("injected")

    fake.add_commit_hook(fail_final)
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.record_tombstones_bulk(NAME, ids, bump=True)
    assert len(seen) == 2
    assert fake.peek(SYNC_DOC)["ctag"] == "ctag-0"
    assert len(fake.peek_collection(TOMBSTONES)) == dav_sync._BATCH_CHUNK - 1


def test_remove_tombstones_bulk_deletes_only_the_named_ids(fake):
    for rid in ("a", "b", "keep"):
        fake.seed(f"{TOMBSTONES}/{rid}", {"deleted_at": None, "sync_token": "t"})
    assert dav_sync.remove_tombstones_bulk(NAME, ["a", "b", "never"]) is None
    assert sorted(fake.peek_collection(TOMBSTONES)) == ["keep"]
    assert fake.peek(SYNC_DOC)["ctag"] == "ctag-0"  # no bump by default


def test_remove_tombstones_bulk_with_bump_commits_the_bump_with_the_removals(fake):
    fake.seed(f"{TOMBSTONES}/a", {"deleted_at": None, "sync_token": "t"})
    token = dav_sync.remove_tombstones_bulk(NAME, ["a"], bump=True)
    assert len(fake.commits) == 1
    kinds = {k for k, _p in fake.commits[0].ops}
    assert kinds == {"delete", "set"}
    assert fake.peek(SYNC_DOC)["ctag"] == token
    assert fake.peek_collection(TOMBSTONES) == {}


def test_remove_tombstones_bulk_propagates_a_write_failure(fake):
    """Unlike remove_tombstone, which logs and swallows: a bulk caller must
    know."""
    fake.add_commit_hook(lambda info: (_ for _ in ()).throw(
        gexc.ServiceUnavailable("injected")))
    with pytest.raises(gexc.ServiceUnavailable):
        dav_sync.remove_tombstones_bulk(NAME, ["a"])
