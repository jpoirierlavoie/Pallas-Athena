"""La visibilité DavX5 d'un dossier — la purge à la fermeture, le
rétablissement à la réouverture, la resynchronisation entre les deux
(lot 4a, ``services/dossier_dav.py``).

Ce sont les PREMIERS tests que la purge ait jamais eus. Elle vivait dans
``routes/dossiers._sync_dossier_dav_visibility``, appelée APRÈS l'écriture du
statut, et elle échouait OUVERT à chaque pas : ses trois lecteurs rendaient
``[]`` sur une panne Firestore — la purge ne tombstonait alors RIEN, bumpait
le CTag et laissait la collection quitter la découverte : tout restait sur
le téléphone, pour toujours, sans une erreur nulle part — et elle revenait
tôt dès que le statut n'avait pas changé, si bien qu'on ne pouvait pas la
rejouer.

Tout passe ici par les VRAIS modèles au-dessus du faux Firestore partagé
(``tests/_fake_firestore.py`` : le client est le vrai, seul le serveur est
faux — ses lots sont atomiques et son journal de commits est la vue du
SERVEUR). On relit ce qui est STOCKÉ — les pierres tombales, le CTag, le
statut —, jamais un dictionnaire remis à un faux.
"""

import ast
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav as dav_pkg
    import dav.dossier_collections as dc
    import dav.sync as dav_sync
    from models import dossier as dossier_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import task as task_model
    from services import dossier_dav

from tests._fake_firestore import FakeFirestore, install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc : le magasin, un dossier et ses membres DAV
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model the
    service starts to read tomorrow cannot reach the mocked client."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _dossier(db, file_number="2026-001", status="actif") -> str:
    doc, errors = dossier_model.create_dossier({
        "file_number": file_number, "title": "Tremblay c. Lavoie",
        "status": status,
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return doc["id"]


def _task(dossier_id, title="Préparer la requête") -> str:
    doc, errors = task_model.create_task(
        {"title": title, "dossier_id": dossier_id})
    assert errors == [], errors
    return doc["id"]


def _note(dossier_id, title="Recherche") -> str:
    doc, errors = note_model.create_note(
        {"title": title, "content": "Premier jet.", "category": "recherche",
         "dossier_id": dossier_id})
    assert errors == [], errors
    return doc["id"]


def _hearing(dossier_id, title="Audience") -> str:
    doc, errors = hearing_model.create_hearing(
        {"title": title, "start_datetime": DT, "dossier_id": dossier_id})
    assert errors == [], errors
    return doc["id"]


def _pending_booking(db, dossier_id) -> str:
    """An unconfirmed « Bookings with me » import linked to the dossier —
    NEVER on the phone, so never drained."""
    rid = "b0000000-0000-4000-8000-000000000001"
    db.seed(f"hearings/{rid}", {
        "id": rid, "dossier_id": dossier_id, "title": "Consultation",
        "start_datetime": DT, "source": "bookings",
        "confirmation": "à_confirmer", "etag": "e",
    })
    return rid


@pytest.fixture
def loaded(db):
    """A dossier holding one of each DAV member (the analyse note
    included), an unconfirmed Bookings import, and a second dossier whose
    task must never be touched."""
    did = _dossier(db)
    members = {
        "task": _task(did),
        "note": _note(did),
        "hearing": _hearing(did),
    }
    analyse, errors = note_model.create_analyse_note(did)
    assert errors == [], errors
    members["analyse"] = analyse["id"]
    pending = _pending_booking(db, did)
    other = _dossier(db, file_number="2026-002")
    other_task = _task(other, title="Autre dossier")
    return {"id": did, "members": members, "pending": pending,
            "other": other, "other_task": other_task}


def _tombstones(db, dossier_id) -> set[str]:
    return set(db.peek_collection(f"dav_sync/dossier:{dossier_id}/tombstones"))


def _ctag(db, dossier_id):
    doc = db.peek(f"dav_sync/dossier:{dossier_id}")
    return (doc or {}).get("ctag")


def _set_status(dossier_id, status):
    return lambda: dossier_model.update_dossier(dossier_id, {"status": status})


def _transition(dossier_id, old, new, *, reconcile_always=False, commit=None):
    return dossier_dav.run_status_transition(
        dossier_id, old, new,
        commit if commit is not None else _set_status(dossier_id, new),
        reconcile_always=reconcile_always,
    )


def _fail_queries_on(monkeypatch, db, collection: str):
    """Every QUERY on *collection* fails at the transport; keyed reads,
    other collections and writes stay healthy — the blip that made the old
    readers answer « nothing here »."""
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


def _dav_commits(db, dossier_id):
    """Commits that touched this dossier's sync state (tombstones or CTag)."""
    prefix = f"dav_sync/dossier:{dossier_id}"
    return [c for c in db.commits
            if any(p == prefix or p.startswith(prefix + "/") for _k, p in c.ops)]


# ══════════════════════════════════════════════════════════════════════
# 1. La purge et le rétablissement — les allers-retours
# ══════════════════════════════════════════════════════════════════════


def test_closing_tombstones_every_member_and_bumps_in_the_same_commit(db, loaded):
    did = loaded["id"]
    before = _ctag(db, did)
    db.reset_logs()

    result = _transition(did, "actif", "fermé")

    assert result.errors == []
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"
    members = set(loaded["members"].values())
    # Every task, note (the analyse note INCLUDED) and confirmed hearing —
    # never the pending Bookings import, never the other dossier's task.
    assert _tombstones(db, did) == members
    assert loaded["pending"] not in _tombstones(db, did)
    assert _tombstones(db, loaded["other"]) == set()
    assert _ctag(db, did) not in (None, before)
    # ONE atomic commit: the tombstones AND the bump (N ≤ 449).
    dav_commits = _dav_commits(db, did)
    assert len(dav_commits) == 1
    ops = {p for _k, p in dav_commits[0].ops}
    assert f"dav_sync/dossier:{did}" in ops
    assert {f"dav_sync/dossier:{did}/tombstones/{m}" for m in members} <= ops
    assert result.dav.direction == dossier_dav.DRAIN
    assert result.dav.complete and result.dav.ctag_bumped
    assert result.dav.resources == len(members)
    # The records themselves are untouched — tombstones are DAV markers.
    for rid in (loaded["members"]["task"], loaded["members"]["note"]):
        assert db.peek(f"tasks/{rid}") or db.peek(f"notes/{rid}")


def test_reopening_removes_the_members_tombstones_and_keeps_a_deletion(db, loaded):
    did = loaded["id"]
    _transition(did, "actif", "fermé")
    # A resource deleted WHILE the dossier was closed: its tombstone still
    # has a deletion to report and must survive the restore.
    gone = "d0000000-0000-4000-8000-00000000dead"
    db.seed(f"dav_sync/dossier:{did}/tombstones/{gone}",
            {"deleted_at": DT, "sync_token": "t"})
    closed_ctag = _ctag(db, did)

    result = _transition(did, "fermé", "actif")

    assert result.errors == [] and result.dav.complete
    assert result.dav.direction == dossier_dav.RESTORE
    assert _tombstones(db, did) == {gone}
    assert _ctag(db, did) != closed_ctag
    assert db.peek(f"dossiers/{did}")["closed_date"] is None


@pytest.mark.parametrize("path", [
    ("actif", "archivé"),
    ("en_attente", "fermé"),
    ("en_attente", "archivé"),
])
def test_every_active_to_inactive_crossing_drains(db, loaded, path):
    did = loaded["id"]
    old, new = path
    db.external_write(f"dossiers/{did}",
                      {**db.peek(f"dossiers/{did}"), "status": old,
                       "etag": "x-" + old})
    result = _transition(did, old, new)
    assert result.dav.direction == dossier_dav.DRAIN and result.dav.complete
    assert _tombstones(db, did) == set(loaded["members"].values())


@pytest.mark.parametrize("path", [
    ("actif", "en_attente"), ("en_attente", "actif"),
    ("fermé", "archivé"), ("archivé", "fermé"),
])
def test_a_transition_that_does_not_cross_writes_no_dav_marker(db, loaded, path):
    """The web form saves every field on every edit: without a crossing of
    the active boundary, nothing is tombstoned, removed or bumped."""
    did = loaded["id"]
    old, new = path
    db.external_write(f"dossiers/{did}",
                      {**db.peek(f"dossiers/{did}"), "status": old,
                       "etag": "x-" + old})
    before = _ctag(db, did)
    db.reset_logs()
    result = _transition(did, old, new)
    assert result.errors == []
    assert db.peek(f"dossiers/{did}")["status"] == new
    assert result.dav.direction == dossier_dav.NONE and result.dav.complete
    assert _dav_commits(db, did) == []
    assert _ctag(db, did) == before


def test_en_attente_keeps_the_collection_and_restores_from_fermé(db, loaded):
    """« En attente » is ACTIVE for DAV: reopening a closed dossier INTO
    en_attente restores it (and it stays in the prescription alerts —
    models.dossier.PRESCRIPTION_ALERT_STATUSES, since lot 0b)."""
    did = loaded["id"]
    _transition(did, "actif", "fermé")
    result = _transition(did, "fermé", "en_attente")
    assert result.dav.direction == dossier_dav.RESTORE
    assert _tombstones(db, did) == set()
    assert "en_attente" in dossier_model.PRESCRIPTION_ALERT_STATUSES
    assert "en_attente" in dav_sync.ACTIVE_DOSSIER_STATUSES


def test_a_refused_commit_writes_no_dav_marker(db, loaded):
    """The members are read before the commit, but NOTHING is written when
    the commit itself refuses (a stale etag here)."""
    did = loaded["id"]
    before = _ctag(db, did)
    db.reset_logs()
    result = _transition(did, "actif", "fermé", commit=lambda: (
        dossier_model.update_dossier(did, {"status": "fermé"},
                                     expected_etag="périmé-0000")))
    assert result.errors and result.doc is None
    assert db.peek(f"dossiers/{did}")["status"] == "actif"
    assert _dav_commits(db, did) == [] and _ctag(db, did) == before


# ══════════════════════════════════════════════════════════════════════
# 2. Fail-closed avant l'écriture, honnête après
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("collection", ["tasks", "notes", "hearings"])
def test_an_unreadable_membership_refuses_before_the_commit(
        db, loaded, monkeypatch, collection):
    """Régression — l'ancien code écrivait le statut, puis purgeait sur des
    listes vides : rien tombstoné, CTag bumpé, collection disparue."""
    did = loaded["id"]
    before = _ctag(db, did)
    _fail_queries_on(monkeypatch, db, collection)
    calls = []

    def commit():
        calls.append(1)
        return dossier_model.update_dossier(did, {"status": "fermé"})

    db.reset_logs()
    result = _transition(did, "actif", "fermé", commit=commit)

    assert calls == []  # the commit callable was NEVER called
    assert result.errors == [dossier_dav.MEMBERS_UNREADABLE_ERROR]
    assert db.peek(f"dossiers/{did}")["status"] == "actif"
    assert _tombstones(db, did) == set() and _ctag(db, did) == before
    assert db.commits == []


def test_a_resource_created_between_the_read_and_the_commit_is_drained(db, loaded):
    """The phone can PUT a task into the still-active collection between
    the pre-read and the status commit; the post-commit re-read catches it."""
    did = loaded["id"]
    late = {}

    def commit():
        late["id"] = _task(did, title="Créée au téléphone")
        return dossier_model.update_dossier(did, {"status": "fermé"})

    result = _transition(did, "actif", "fermé", commit=commit)
    assert result.dav.complete
    assert late["id"] in _tombstones(db, did)


def test_a_failed_reread_after_the_commit_drains_what_it_knows_and_says_incomplete(
        db, loaded, monkeypatch):
    did = loaded["id"]

    def commit():
        doc = dossier_model.update_dossier(did, {"status": "fermé"})
        _fail_queries_on(monkeypatch, db, "notes")  # the blip hits AFTER
        return doc

    result = _transition(did, "actif", "fermé", commit=commit)
    assert result.errors == []
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"
    assert _tombstones(db, did) == set(loaded["members"].values())
    assert not result.dav.complete
    assert result.dav.error == dossier_dav.ERR_MEMBERS_REREAD_FAILED


def test_a_failed_drain_write_is_reported_and_the_resync_repairs_it(db, loaded):
    """The status write cannot be undone: a failed drain is REPORTED
    (never raised, never claimed a success), and the separate repair path
    re-applies the visibility for the CURRENT status."""
    did = loaded["id"]
    before = _ctag(db, did)
    prefix = f"dav_sync/dossier:{did}"

    def fail_dav(info):
        if any(p.startswith(prefix) for _k, p in info.ops):
            raise gexc.ServiceUnavailable("injected commit failure")

    remove = db.add_commit_hook(fail_dav)
    result = _transition(did, "actif", "fermé")
    remove()

    assert result.errors == [] and result.doc is not None
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"
    assert not result.dav.complete and not result.dav.ctag_bumped
    assert result.dav.error == dossier_dav.ERR_WRITE_FAILED
    assert _tombstones(db, did) == set() and _ctag(db, did) == before

    repaired = dossier_dav.resync_dossier_dav_visibility(did)
    assert repaired.complete and repaired.direction == dossier_dav.DRAIN
    assert _tombstones(db, did) == set(loaded["members"].values())
    assert _ctag(db, did) != before


def test_the_same_status_with_reconcile_always_re_runs_the_drain(db, loaded):
    """No early return on old == new: the connector (lot 4b) repairs an
    incomplete drain by re-asking for the status the dossier has."""
    did = loaded["id"]
    _transition(did, "actif", "fermé")
    db.external_delete(
        f"dav_sync/dossier:{did}/tombstones/{loaded['members']['note']}")
    ctag = _ctag(db, did)

    result = _transition(did, "fermé", "fermé", reconcile_always=True)

    assert result.dav.direction == dossier_dav.DRAIN and result.dav.complete
    assert _tombstones(db, did) == set(loaded["members"].values())
    assert _ctag(db, did) != ctag


def test_a_pure_resync_with_no_commit(db, loaded):
    did = loaded["id"]
    result = dossier_dav.run_status_transition(
        did, "actif", "actif", None, reconcile_always=True)
    assert result.errors == [] and result.doc is None
    assert result.dav.direction == dossier_dav.RESTORE and result.dav.complete


def test_an_unknown_old_status_applies_the_target_visibility(db, loaded):
    """A failed pre-read (old_status None) is never guessed « actif »: a
    guessed active old status would skip the drain of a dossier the other
    tab had just closed."""
    did = loaded["id"]
    db.external_write(f"dossiers/{did}",
                      {**db.peek(f"dossiers/{did}"), "status": "fermé",
                       "etag": "x"})
    result = _transition(did, None, "fermé")
    assert result.dav.direction == dossier_dav.DRAIN
    assert _tombstones(db, did) == set(loaded["members"].values())


def test_a_concurrent_close_landing_during_a_restore_ends_drained(db, loaded):
    """Another transition can commit between ours and our DAV writes. A
    restore that removed a concurrent drain's tombstones would strand the
    phone: the stored status is re-read after the write, and the visibility
    of the STORED status applied once more."""
    did = loaded["id"]
    _transition(did, "actif", "fermé")
    prefix = f"dav_sync/dossier:{did}"
    fired = []

    def close_meanwhile(info):
        removes = any(k == "delete" and p.startswith(prefix + "/tombstones/")
                      for k, p in info.ops)
        if removes and not fired:
            fired.append(1)
            doc = db.peek(f"dossiers/{did}")
            db.external_write(f"dossiers/{did}",
                              {**doc, "status": "fermé", "etag": "concurrent"})

    db.add_commit_hook(close_meanwhile)
    result = _transition(did, "fermé", "actif")

    assert fired == [1]
    assert db.peek(f"dossiers/{did}")["status"] == "fermé"
    assert result.dav.complete and result.dav.direction == dossier_dav.DRAIN
    assert _tombstones(db, did) == set(loaded["members"].values())


def test_resync_of_an_unreadable_dossier_writes_nothing(db, loaded, monkeypatch):
    did = loaded["id"]
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        if f"dossiers/{did}" in [server.doc_rel(n) for n in request["documents"]]:
            raise gexc.ServiceUnavailable("injected")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    db.reset_logs()
    dav = dossier_dav.resync_dossier_dav_visibility(did)
    assert not dav.complete and dav.error == dossier_dav.ERR_DOSSIER_UNREADABLE
    assert db.commits == []


def test_resync_of_a_missing_dossier(db):
    dav = dossier_dav.resync_dossier_dav_visibility(
        "a0000000-0000-4000-8000-000000000000")
    assert dav.error == dossier_dav.ERR_DOSSIER_NOT_FOUND and db.commits == []


def test_resync_with_unreadable_members_writes_nothing(db, loaded, monkeypatch):
    did = loaded["id"]
    _fail_queries_on(monkeypatch, db, "hearings")
    db.reset_logs()
    dav = dossier_dav.resync_dossier_dav_visibility(did)
    assert not dav.complete and dav.error == dossier_dav.ERR_MEMBERS_UNREADABLE
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. Le découpage : jamais plus d'un lot, le bump dans le DERNIER
# ══════════════════════════════════════════════════════════════════════


def test_a_large_drain_is_chunked_with_the_bump_in_the_final_chunk(monkeypatch):
    chunk = dav_sync._BATCH_CHUNK
    db = install(monkeypatch, *_fake_modules(),
                 fake=FakeFirestore(max_writes_per_commit=chunk))
    did = _dossier(db)
    n = chunk + 60
    ids = [f"t{i:04d}" for i in range(n)]
    for rid in ids:
        db.seed(f"tasks/{rid}", {"id": rid, "dossier_id": did,
                                 "title": "T", "status": "à_faire"})
    db.reset_logs()

    result = _transition(did, "actif", "fermé")

    assert result.dav.complete and result.dav.resources == n
    commits = _dav_commits(db, did)
    assert len(commits) == 2
    assert all(len(c.ops) <= chunk for c in commits)
    sync_doc = f"dav_sync/dossier:{did}"
    assert [any(p == sync_doc for _k, p in c.ops) for c in commits] == [False, True]
    assert _tombstones(db, did) == set(ids)
    # Every tombstone carries the NEW token, read from nowhere.
    stored = db.peek_collection(f"{sync_doc}/tombstones")
    assert {t["sync_token"] for t in stored.values()} == {_ctag(db, did)}


def test_an_empty_dossier_still_bumps(db):
    did = _dossier(db)
    before = _ctag(db, did)
    result = _transition(did, "actif", "fermé")
    assert result.dav.complete and result.dav.resources == 0
    assert _ctag(db, did) not in (None, before)


# ══════════════════════════════════════════════════════════════════════
# 4. Les lecteurs STRICTS
# ══════════════════════════════════════════════════════════════════════


_STRICT = [
    ("tasks", lambda d: task_model.list_tasks_strict(d)),
    ("notes", lambda d: note_model.list_notes_strict(d, include_analyse=True)),
    ("hearings", lambda d: hearing_model.list_hearings_strict(
        d, include_unconfirmed=False)),
]


@pytest.mark.parametrize("collection,reader", _STRICT)
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_strict_reader_refuses_a_blank_id_before_any_query(
        db, collection, reader, blank):
    """The shared query body reads the WHOLE collection for a falsy id; a
    drain handed "" would tombstone every record of the firm."""
    db.reset_logs()
    with pytest.raises(ValueError):
        reader(blank)
    assert db.reads == []


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_the_service_refuses_a_blank_dossier_id(db, blank):
    for call in (
        lambda: dossier_dav.dav_member_ids(blank),
        lambda: dossier_dav.apply_dav_visibility(blank, "fermé", []),
        lambda: dossier_dav.run_status_transition(
            blank, "actif", "fermé", None, reconcile_always=False),
        lambda: dossier_dav.resync_dossier_dav_visibility(blank),
    ):
        with pytest.raises(ValueError):
            call()
    assert db.reads == [] and db.commits == []


@pytest.mark.parametrize("collection,reader", _STRICT)
def test_a_strict_reader_propagates_where_the_display_reader_answers_empty(
        db, loaded, monkeypatch, collection, reader):
    did = loaded["id"]
    _fail_queries_on(monkeypatch, db, collection)
    with pytest.raises(gexc.ServiceUnavailable):
        reader(did)
    display = {"tasks": lambda: task_model.list_tasks(dossier_id=did),
               "notes": lambda: note_model.list_notes(dossier_id=did),
               "hearings": lambda: hearing_model.list_hearings(dossier_id=did)}
    assert display[collection]() == []  # the fail-open sibling, unchanged


def test_the_strict_flags_have_no_default():
    """A DAV caller must decide consciously (the include_analyse lesson)."""
    import inspect
    for fn, flag in ((note_model.list_notes_strict, "include_analyse"),
                     (hearing_model.list_hearings_strict, "include_unconfirmed")):
        param = inspect.signature(fn).parameters[flag]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


def test_membership_parity_with_the_collection_listing(db, loaded):
    """dav_member_ids is EXACTLY what /dav/dossier-{id}/ lists — derived
    from the collection's own enumeration on the same store (the analyse
    note in, the pending Bookings import out)."""
    did = loaded["id"]
    hearings, tasks, notes = dc._collection_members(did)
    listed = sorted(o["id"] for o in (*hearings, *tasks, *notes))
    assert sorted(dossier_dav.dav_member_ids(did)) == listed
    assert loaded["members"]["analyse"] in listed
    assert loaded["pending"] not in listed


# ══════════════════════════════════════════════════════════════════════
# 5. UNE liste des statuts actifs — dérivée, sur chaque statut
# ══════════════════════════════════════════════════════════════════════


def _root_listing_ids(monkeypatch, dossiers: list[dict]) -> set[str]:
    """The dossier ids the root Depth:1 PROPFIND advertises, over a patched
    list_dossiers (the root reads nothing else about dossiers)."""
    from flask import Flask

    def fake_list(status_filter=None, **_kw):
        return [d for d in dossiers if d["status"] == status_filter]

    monkeypatch.setattr(dossier_model, "list_dossiers", fake_list)
    monkeypatch.setattr(dav_sync, "get_ctags_bulk",
                        lambda names: {n: "c" for n in names})
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "t"
    app.register_blueprint(dav_pkg.dav_bp)
    resp = app.test_client().open(
        "/dav/", method="PROPFIND",
        headers={"Depth": "1",
                 "Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="})
    assert resp.status_code == 207, resp.data
    body = resp.get_data(as_text=True)
    return {d["id"] for d in dossiers if f"/dav/dossier-{d['id']}/" in body}


def test_every_status_is_classified_the_same_by_discovery_the_collection_and_the_drain(
        monkeypatch):
    dossiers = [{"id": f"d{i}", "status": s, "file_number": f"2026-00{i}",
                 "title": "T"}
                for i, s in enumerate(dossier_model.VALID_STATUSES)]
    advertised = _root_listing_ids(monkeypatch, dossiers)
    for d in dossiers:
        status = d["status"]
        drained_on_entry = dossier_dav.planned_direction(
            None, status, reconcile_always=True) == dossier_dav.DRAIN
        collection_live = dc._dossier_is_active(d)
        assert (d["id"] in advertised) == collection_live == (not drained_on_entry), status
    # Non-vacuous: both sides of the boundary are exercised.
    assert advertised and advertised != {d["id"] for d in dossiers}


def test_no_hand_typed_active_status_tuple_survives_on_the_dav_paths():
    """The literal (« actif », « en_attente ») lived in the route, the DAV
    reader and six handler sites; it must now be read from dav.sync. AST
    sweep over the DAV-visibility consumers (the prescription alerts and
    the open-dossier count are a different question and live in
    models/dossier.py, outside this sweep)."""
    files = [*sorted((_ATHENA / "dav").glob("*.py")),
             _ATHENA / "services" / "dossier_dav.py",
             _ATHENA / "routes" / "dossiers.py",
             _ATHENA / "mcp" / "handlers.py"]
    target = {"actif", "en_attente"}
    hits = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                values = {e.value for e in node.elts
                          if isinstance(e, ast.Constant)}
                if values == target and len(node.elts) == 2:
                    hits.append(f"{path.name}:{node.lineno}")
    assert hits == [f"sync.py:{_constant_line()}"], hits


def _constant_line() -> int:
    tree = ast.parse((_ATHENA / "dav" / "sync.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", "") == "ACTIVE_DOSSIER_STATUSES"):
            return node.value.lineno
    raise AssertionError("ACTIVE_DOSSIER_STATUSES not found in dav/sync.py")


def test_the_route_no_longer_carries_its_own_drain():
    source = (_ATHENA / "routes" / "dossiers.py").read_text(encoding="utf-8")
    assert "_sync_dossier_dav_visibility" not in source
    assert "record_tombstones_bulk" not in source
    assert "dossier_dav.run_status_transition" in source
