"""Le déplacement d'une ressource DAV entre collections (règle 6 du plan).

Une tâche, une note ou une audience dont le dossier change QUITTE une
collection DAV et ENTRE dans une autre. Les suppressions ne voyagent que par
pierre tombale : sans elle, l'ancienne collection garde sa copie sur le
téléphone pour toujours ; sans le retrait de la pierre tombale périmée côté
nouvelle, un même REPORT dirait la ressource à la fois vivante et supprimée.

Trois routes portaient cette chorégraphie à la main. L'ORDRE vit désormais à
un seul endroit, ``dav.sync.relocation_plan``, exécuté par
``dav.sync.relocate_resource`` (bruyant, pour les routes) et par
``mcp.handlers._dav_resync`` (chaque étape sous sa propre garde : l'écriture
est déjà engagée). On épingle :

1. le plan — sa table, clé sur les COLLECTIONS et jamais sur les ids bruts ;
2. ``relocate_resource`` sur le faux partagé : l'état du magasin après un
   déplacement, et l'arrêt à la première panne ;
3. la PARITÉ, octet pour octet, avec le code des trois routes qui
   portaient la chorégraphie — transcrit tel qu'il était à 65a6d17 — et
   avec les routes réelles elles-mêmes ; la route des audiences, qui
   comparait les ids BRUTS, a basculé au lot 1a en corrigeant le seul écart
   (None contre « » dans « Général ») ;
4. ``_dav_resync`` — les deux moitiés indépendantes, rien qui lève ;
5. ``_entity_write_result(previous_dossier_id=…)`` et son contrat, et
   l'appariement DÉRIVÉ outil ↔ clé ``previous_collection_cleared``.
"""

import ast
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.output_schemas as output_schemas
    import mcp.tools as tools
    import routes.hearings as hearings_routes
    import routes.notes as notes_routes
    import routes.tasks as tasks_routes
    from mcp.output_schemas import OUTPUT_SCHEMAS

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

GEN = dav_sync.GENERAL_COLLECTION
IDS = (None, "", "d1", "d2")


def _scope(dossier_id):
    return f"dossier:{dossier_id}" if dossier_id else GEN


# ══════════════════════════════════════════════════════════════════════
# 1. Le plan
# ══════════════════════════════════════════════════════════════════════


def test_a_real_move_tombstones_and_bumps_the_old_then_the_new():
    assert dav_sync.relocation_plan(
        "r1", old_dossier_id="d1", new_dossier_id="d2",
    ) == (
        ("record_tombstone", "dossier:d1"),
        ("bump_ctag", "dossier:d1"),
        ("remove_tombstone", "dossier:d2"),
        ("bump_ctag", "dossier:d2"),
    )


@pytest.mark.parametrize("old, new", [("d1", None), ("d1", ""), (None, "d2"), ("", "d2")])
def test_a_move_to_or_from_general_is_a_real_move(old, new):
    plan = dav_sync.relocation_plan("r1", old_dossier_id=old, new_dossier_id=new)
    assert [op for op, _ in plan] == [
        "record_tombstone", "bump_ctag", "remove_tombstone", "bump_ctag",
    ]
    assert plan[0][1] == _scope(old) and plan[-1][1] == _scope(new)


@pytest.mark.parametrize("old, new", [
    ("d1", "d1"), (None, None), ("", ""),
    (None, ""), ("", None),       # « Général » both ways: NOT a move
])
def test_the_same_collection_is_one_bump(old, new):
    assert dav_sync.relocation_plan(
        "r1", old_dossier_id=old, new_dossier_id=new,
    ) == (("bump_ctag", _scope(new)),)


@pytest.mark.parametrize("old", IDS)
def test_a_creation_untombstones_then_bumps_the_new_collection(old):
    assert dav_sync.relocation_plan(
        "r1", old_dossier_id=old, new_dossier_id="d2", created=True,
    ) == (("remove_tombstone", "dossier:d2"), ("bump_ctag", "dossier:d2"))


def test_a_plan_for_no_resource_is_refused():
    with pytest.raises(ValueError):
        dav_sync.relocation_plan("", old_dossier_id="d1", new_dossier_id="d2")


def test_every_planned_operation_is_declared():
    ops = {
        op
        for old in IDS for new in IDS for created in (False, True)
        for op, _ in dav_sync.relocation_plan(
            "r1", old_dossier_id=old, new_dossier_id=new, created=created,
        )
    }
    assert ops == set(dav_sync.RELOCATION_OPERATIONS)


# ══════════════════════════════════════════════════════════════════════
# 2. relocate_resource — sur le vrai client, magasin en mémoire
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def store(monkeypatch):
    fake = install(monkeypatch, dav_sync)
    fake.seed("dav_sync/dossier:d1", {"ctag": "c-old", "sync_token": "c-old"})
    fake.seed("dav_sync/dossier:d2", {"ctag": "c-new", "sync_token": "c-new"})
    # A stale tombstone from a previous delete of the same id.
    fake.seed("dav_sync/dossier:d2/tombstones/r1", {"sync_token": "x"})
    return fake


def test_a_move_leaves_the_store_as_davx5_needs_it(store):
    dav_sync.relocate_resource("r1", old_dossier_id="d1", new_dossier_id="d2")
    tomb = store.peek("dav_sync/dossier:d1/tombstones/r1")
    assert tomb is not None and tomb["sync_token"] == "c-old"
    assert store.peek("dav_sync/dossier:d1")["ctag"] != "c-old"
    assert store.peek("dav_sync/dossier:d2/tombstones/r1") is None
    assert store.peek("dav_sync/dossier:d2")["ctag"] != "c-new"


def test_the_old_collection_is_told_before_the_new(store):
    dav_sync.relocate_resource("r1", old_dossier_id="d1", new_dossier_id="d2")
    ops = [op for commit in store.commits for op in commit.ops]
    assert ops == [
        ("set", "dav_sync/dossier:d1/tombstones/r1"),
        ("set", "dav_sync/dossier:d1"),
        ("delete", "dav_sync/dossier:d2/tombstones/r1"),
        ("set", "dav_sync/dossier:d2"),
    ]


def test_a_creation_removes_the_stale_tombstone(store):
    dav_sync.relocate_resource(
        "r1", old_dossier_id=None, new_dossier_id="d2", created=True,
    )
    assert store.peek("dav_sync/dossier:d2/tombstones/r1") is None
    assert store.peek("dav_sync/dossier:d1/tombstones/r1") is None
    assert store.peek("dav_sync/dossier:d1")["ctag"] == "c-old"


def test_relocate_resource_stops_at_the_first_failure(monkeypatch):
    """The route's loud path: a failure is a 500, and nothing after it runs
    — exactly what the hand-written blocks did."""
    calls = []

    def boom(name):
        calls.append(("bump_ctag", name))
        raise RuntimeError("store down")

    monkeypatch.setattr(dav_sync, "record_tombstone",
                        lambda c, r: calls.append(("record_tombstone", c)))
    monkeypatch.setattr(dav_sync, "bump_ctag", boom)
    monkeypatch.setattr(dav_sync, "remove_tombstone",
                        lambda c, r: calls.append(("remove_tombstone", c)))
    with pytest.raises(RuntimeError):
        dav_sync.relocate_resource("r1", old_dossier_id="d1", new_dossier_id="d2")
    assert calls == [("record_tombstone", "dossier:d1"), ("bump_ctag", "dossier:d1")]


# ══════════════════════════════════════════════════════════════════════
# 3. La parité avec le code qu'il remplace
# ══════════════════════════════════════════════════════════════════════
#
# The three blocks below are the route code as it stood at 65a6d17
# (routes/tasks.py:403-413, routes/notes.py:441-454, routes/hearings.py:
# 597-604), transcribed statement for statement with only the primitives
# injected. They are the reference any switch to relocate_resource is
# measured against: « byte-identical » means the same calls, in the same
# order, for every input. The real-route tests further down drive the
# routes themselves against the same transcripts: while a route still
# carries its own block they prove the transcription faithful; once it
# calls relocate_resource, they prove the switch changed nothing.


def _legacy_task_route(rec, task_id, old_dossier_id, new_dossier_id):
    collection_for, record_tombstone, bump_ctag, remove_tombstone = rec
    old_scope = collection_for(old_dossier_id)
    new_scope = collection_for(new_dossier_id)
    if old_scope != new_scope:
        record_tombstone(old_scope, task_id)
        bump_ctag(old_scope)
        remove_tombstone(new_scope, task_id)
    bump_ctag(new_scope)


def _legacy_note_route(rec, note_id, old_dossier_id, new_dossier_id):
    collection_for, record_tombstone, bump_ctag, remove_tombstone = rec
    old_scope = collection_for(old_dossier_id)
    new_scope = collection_for(new_dossier_id)
    if old_scope != new_scope:
        record_tombstone(old_scope, note_id)
        bump_ctag(old_scope)
        remove_tombstone(new_scope, note_id)
    bump_ctag(new_scope)


def _legacy_hearing_route(rec, hearing_id, old_dossier_id, new_dossier_id):
    collection_for, record_tombstone, bump_ctag, remove_tombstone = rec
    if old_dossier_id != new_dossier_id:
        record_tombstone(collection_for(old_dossier_id), hearing_id)
        bump_ctag(collection_for(old_dossier_id))
        remove_tombstone(collection_for(new_dossier_id), hearing_id)
    bump_ctag(collection_for(new_dossier_id))


def _recorder():
    calls: list[tuple] = []
    prims = (
        dav_sync.collection_for,
        lambda c, r: calls.append(("record_tombstone", c, r)),
        lambda c: calls.append(("bump_ctag", c)),
        lambda c, r: calls.append(("remove_tombstone", c, r)),
    )
    return calls, prims


def _legacy_calls(block, old, new):
    calls, prims = _recorder()
    block(prims, "r1", old, new)
    return calls


def _relocate_calls(monkeypatch, old, new, created=False):
    calls, (_, rec_t, bump, rem_t) = _recorder()
    monkeypatch.setattr(dav_sync, "record_tombstone", rec_t)
    monkeypatch.setattr(dav_sync, "bump_ctag", bump)
    monkeypatch.setattr(dav_sync, "remove_tombstone", rem_t)
    dav_sync.relocate_resource(
        "r1", old_dossier_id=old, new_dossier_id=new, created=created,
    )
    return calls


def _resync_calls(monkeypatch, old, new, created=False):
    calls, (_, rec_t, bump, rem_t) = _recorder()
    monkeypatch.setattr(handlers, "record_tombstone", rec_t)
    monkeypatch.setattr(handlers, "bump_ctag", bump)
    monkeypatch.setattr(handlers, "remove_tombstone", rem_t)
    assert handlers._dav_resync(
        "r1", old_dossier_id=old, new_dossier_id=new, created=created,
    ) == (True, True)
    return calls


@pytest.mark.parametrize("block", [_legacy_task_route, _legacy_note_route])
@pytest.mark.parametrize("old", IDS)
@pytest.mark.parametrize("new", IDS)
def test_relocate_resource_is_the_task_and_note_route_code(monkeypatch, block, old, new):
    assert _relocate_calls(monkeypatch, old, new) == _legacy_calls(block, old, new)


@pytest.mark.parametrize("old", IDS)
@pytest.mark.parametrize("new", IDS)
@pytest.mark.parametrize("created", [False, True])
def test_the_two_executors_issue_the_same_calls(monkeypatch, old, new, created):
    assert (_resync_calls(monkeypatch, old, new, created)
            == _relocate_calls(monkeypatch, old, new, created))


def test_the_legacy_hearing_route_differed_only_on_general_spelled_two_ways(
    monkeypatch,
):
    """The hearing route's former block compared RAW ids, so None vs "" —
    both « Général » — recorded a tombstone there, removed it, and bumped
    twice. That is the ONLY place it disagreed with relocation_plan, and it
    is why the route was left alone in lot 0a (a switch had to change
    nothing). Lot 1a switched it anyway, deliberately, to fix exactly
    those inputs — pinned against the real route below."""
    differs = {
        (old, new) for old in IDS for new in IDS
        if _relocate_calls(monkeypatch, old, new)
        != _legacy_calls(_legacy_hearing_route, old, new)
    }
    assert differs == {(None, ""), ("", None)}


# The routes themselves, driven for real.


def _client(blueprint):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(blueprint)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _route_recorder(monkeypatch, route_module):
    """Record every DAV primitive the route can reach — through its own
    imported names AND through dav.sync (relocate_resource's) — into one
    ordered list, so a call made twice by two paths would show."""
    calls, (_, rec_t, bump, rem_t) = _recorder()
    for module in (route_module, dav_sync):
        for name, fn in (("record_tombstone", rec_t), ("bump_ctag", bump),
                         ("remove_tombstone", rem_t)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, fn)
    return calls


def _dossier(i):
    return {"id": i, "file_number": f"2026-{i}", "title": f"Dossier {i}"}


@pytest.mark.parametrize("old", IDS)
@pytest.mark.parametrize("new", ["", "d1", "d2"])   # the form posts "" for none
def test_the_task_route_moves_exactly_as_before(monkeypatch, old, new):
    calls = _route_recorder(monkeypatch, tasks_routes)
    monkeypatch.setattr(tasks_routes, "get_task",
                        lambda i: {"id": i, "dossier_id": old, "title": "T"})
    monkeypatch.setattr(tasks_routes, "get_dossier", _dossier)
    monkeypatch.setattr(
        tasks_routes, "update_task",
        lambda tid, data, *, expected_etag=None: ({"id": tid, **data}, []),
    )
    resp = _client(tasks_routes.tasks_bp).post(
        "/taches/r1", data={"title": "T", "dossier_id": new},
    )
    assert resp.status_code == 302
    # The form turns "" into the task's « no dossier » value, None.
    assert calls == _legacy_calls(_legacy_task_route, old, new or None)


@pytest.mark.parametrize("old", IDS)
@pytest.mark.parametrize("new", ["", "d1", "d2"])
def test_the_note_route_moves_exactly_as_before(monkeypatch, old, new):
    calls = _route_recorder(monkeypatch, notes_routes)
    monkeypatch.setattr(
        notes_routes, "get_note",
        lambda i: {"id": i, "dossier_id": old, "title": "T", "content": "C",
                   "category": "recherche"},
    )
    monkeypatch.setattr(notes_routes, "get_dossier", _dossier)
    monkeypatch.setattr(
        notes_routes, "update_note",
        lambda nid, data, *, expected_etag=None: ({"id": nid, **data}, []),
    )
    resp = _client(notes_routes.notes_bp).post("/notes/r1", data={
        "title": "T", "content": "C", "category": "recherche",
        "dossier_id": new,
    })
    assert resp.status_code == 302
    assert calls == _legacy_calls(_legacy_note_route, old, new)


@pytest.mark.parametrize("old", IDS)
@pytest.mark.parametrize("new", ["", "d1", "d2"])   # the form posts "" for none
def test_the_hearing_route_moves_like_relocate_resource(monkeypatch, old, new):
    """Lot 1a: routes/hearings.hearing_update runs relocation_plan. Every
    input moves as relocate_resource does — including a hearing stored with
    dossier_id None saved from the form with "" (« Général » both times):
    one bump, and no tombstone recorded then removed in the same collection
    (the old block's double churn)."""
    calls = _route_recorder(monkeypatch, hearings_routes)
    monkeypatch.setattr(hearings_routes, "get_hearing",
                        lambda i: {"id": i, "dossier_id": old, "title": "A"})
    monkeypatch.setattr(hearings_routes, "get_dossier", _dossier)
    monkeypatch.setattr(
        hearings_routes, "update_hearing",
        lambda hid, data, **kw: ({"id": hid, **data}, []),
    )
    resp = _client(hearings_routes.hearings_bp).post("/audiences/r1", data={
        "title": "A", "start_date": "2026-10-15", "start_time": "09:00",
        "end_time": "10:00", "hearing_type": "audience",
        "status": "confirmée", "dossier_id": new,
    })
    assert resp.status_code == 302
    assert calls == _relocate_calls(monkeypatch, old, new)
    if old in (None, "") and new == "":
        assert calls == [("bump_ctag", GEN)]


def test_the_switched_routes_call_the_shared_helper():
    """The parity above is only worth something if the routes really go
    through relocate_resource — never back to a hand-written copy."""
    for module, fn in ((tasks_routes, "task_update"), (notes_routes, "note_update"),
                       (hearings_routes, "hearing_update")):
        tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        called = {c.func.id for c in ast.walk(node)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "relocate_resource" in called, fn
        assert not called & {"record_tombstone", "remove_tombstone", "bump_ctag"}, fn


# ══════════════════════════════════════════════════════════════════════
# 4. _dav_resync — deux moitiés indépendantes, rien qui lève
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def logged(monkeypatch):
    seen = []
    import utils.logging_setup as ls
    monkeypatch.setattr(ls, "log_unexpected", lambda msg, **kw: seen.append((msg, kw)))
    return seen


def _flaky(monkeypatch, *, failing):
    """Primitives that fail on the (operation, collection) pairs named."""
    calls = []

    def run(op, coll, *rest):
        calls.append((op, coll))
        if (op, coll) in failing:
            raise RuntimeError("store down")

    monkeypatch.setattr(handlers, "record_tombstone", lambda c, r: run("record_tombstone", c))
    monkeypatch.setattr(handlers, "bump_ctag", lambda c: run("bump_ctag", c))
    monkeypatch.setattr(handlers, "remove_tombstone", lambda c, r: run("remove_tombstone", c))
    return calls


def test_an_old_half_failure_still_tells_the_new_collection(monkeypatch, logged):
    calls = _flaky(monkeypatch, failing={("record_tombstone", "dossier:d1")})
    assert handlers._dav_resync(
        "r1", old_dossier_id="d1", new_dossier_id="d2", created=False,
    ) == (True, False)
    assert calls == [
        ("record_tombstone", "dossier:d1"), ("bump_ctag", "dossier:d1"),
        ("remove_tombstone", "dossier:d2"), ("bump_ctag", "dossier:d2"),
    ]
    assert logged == [("mcp write: DAV resync step failed",
                       {"step": "record_tombstone", "collection": "dossier:d1"})]


def test_a_new_half_failure_is_reported_as_not_bumped(monkeypatch, logged):
    _flaky(monkeypatch, failing={("bump_ctag", "dossier:d2")})
    assert handlers._dav_resync(
        "r1", old_dossier_id="d1", new_dossier_id="d2", created=False,
    ) == (False, True)
    assert len(logged) == 1


def test_a_failure_on_the_same_collection_counts_as_the_new_half(monkeypatch, logged):
    _flaky(monkeypatch, failing={("bump_ctag", "dossier:d1")})
    assert handlers._dav_resync(
        "r1", old_dossier_id="d1", new_dossier_id="d1", created=False,
    ) == (False, True)


def test_an_unplannable_resync_is_reported_never_raised(monkeypatch, logged):
    calls = _flaky(monkeypatch, failing=set())
    assert handlers._dav_resync(
        "", old_dossier_id="d1", new_dossier_id="d2", created=False,
    ) == (False, False)
    assert calls == [] and logged[0][0] == "mcp write: DAV resync could not be planned"


def test_bump_note_ctag_is_the_resync_of_a_write_that_does_not_move(monkeypatch):
    """The existing tools' path, unchanged in effect: a creation
    un-tombstones then bumps, an edit bumps."""
    for created in (False, True):
        calls = _flaky(monkeypatch, failing=set())
        assert handlers._bump_note_ctag("d1", "r1", created=created) is True
        assert calls == [
            (op, coll) for op, coll in dav_sync.relocation_plan(
                "r1", old_dossier_id="d1", new_dossier_id="d1", created=created,
            )
        ]


def test_bump_note_ctag_still_reports_a_failed_bump(monkeypatch, logged):
    _flaky(monkeypatch, failing={("bump_ctag", GEN)})
    assert handlers._bump_note_ctag("", "r1", created=False) is False


# ══════════════════════════════════════════════════════════════════════
# 5. _entity_write_result(previous_dossier_id=…) et son contrat
# ══════════════════════════════════════════════════════════════════════

_RELOCATING_SCHEMA = output_schemas._entity_write_result(
    {"status": {"type": "string"}}, dav=True, verb="updated", relocates=True,
)


def _moved_payload(monkeypatch, *, failing=frozenset(), wrote=True, previous="d1"):
    """(payload, the DAV calls it made) for a task moved to dossier d2."""
    calls = _flaky(monkeypatch, failing=set(failing))
    payload = handlers._entity_write_result(
        "task",
        {"id": "r1", "dossier_id": "d2", "dossier_file_number": "2026-d2",
         "dossier_title": "Dossier d2", "label": "T", "date": None,
         "status": "à_faire"},
        dossier={"status": "actif"}, dav_exposed=True, verb="updated",
        created=False, wrote=wrote, previous_dossier_id=previous,
    )
    payload["idempotent_replay"] = False     # run_write's key
    return payload, calls


def _conforms(payload):
    errors = tools.validate_args(_RELOCATING_SCHEMA, tools._jsonable(payload))
    assert errors == [], errors


def test_a_clean_move_reports_the_old_collection_cleared(monkeypatch, logged):
    payload, calls = _moved_payload(monkeypatch)
    assert calls[0] == ("record_tombstone", "dossier:d1")
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is True
    assert payload["previous_collection_cleared"] is True
    assert payload["warnings"] == []
    _conforms(payload)


def test_an_old_half_failure_warns_and_says_not_to_retry(monkeypatch, logged):
    payload, _ = _moved_payload(
        monkeypatch, failing={("record_tombstone", "dossier:d1")},
    )
    assert payload["previous_collection_cleared"] is False
    assert payload["ctag_bumped"] is True           # the new half still ran
    assert len(payload["warnings"]) == 1
    warning = payload["warnings"][0]
    assert "ancien dossier" in warning and "Ne pas réessayer" in warning
    _conforms(payload)


def test_a_no_op_moves_nothing_and_claims_nothing(monkeypatch):
    payload, calls = _moved_payload(monkeypatch, wrote=False)
    assert payload["ctag_bumped"] is False
    assert payload["previous_collection_cleared"] is True
    assert payload["warnings"] == [] and calls == []
    _conforms(payload)


def test_a_move_from_general_spelled_none_is_the_general_collection(monkeypatch, logged):
    _, calls = _moved_payload(monkeypatch, previous=None)
    assert calls[:2] == [("record_tombstone", GEN), ("bump_ctag", GEN)]


def test_without_previous_dossier_id_the_payload_is_unchanged(monkeypatch):
    """The existing tools: the same calls as before, and NO new key — their
    declared schemas do not carry it."""
    calls = _flaky(monkeypatch, failing=set())
    payload = handlers._entity_write_result(
        "task", {"id": "r1", "dossier_id": "d2"}, dossier=None,
        dav_exposed=True, created=True,
    )
    assert "previous_collection_cleared" not in payload
    assert calls == [("remove_tombstone", "dossier:d2"), ("bump_ctag", "dossier:d2")]


def test_the_relocating_schema_requires_the_key_and_only_on_dav_entities():
    assert "previous_collection_cleared" in _RELOCATING_SCHEMA["required"]
    plain = output_schemas._entity_write_result({}, dav=True)
    assert "previous_collection_cleared" not in plain["properties"]
    with pytest.raises(ValueError):
        output_schemas._entity_write_result({}, dav=False, relocates=True)


def _tools_passing_previous_dossier_id() -> set[str]:
    """Write tools whose handler — followed through the module-level names
    it references in mcp/handlers.py — calls ``_entity_write_result`` with a
    ``previous_dossier_id``. Derived, never a list."""
    tree = ast.parse(pathlib.Path(handlers.__file__).read_text(encoding="utf-8"))
    top = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            top[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    top[t.id] = node

    def passes(start: str) -> tuple[bool, bool]:
        seen, stack = set(), [start]
        builds = relocates = False
        while stack:
            name = stack.pop()
            if name in seen or name not in top:
                continue
            seen.add(name)
            for sub in ast.walk(top[name]):
                if isinstance(sub, ast.Name) and sub.id in top:
                    stack.append(sub.id)
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_entity_write_result"):
                    builds = True
                    if any(k.arg == "previous_dossier_id" for k in sub.keywords):
                        relocates = True
        return builds, relocates

    reach = {t: passes(tools.TOOLS[t]["handler"]) for t in tools.WRITE_TOOLS}
    # Not vacuous: the traversal does reach the builder from its known users.
    for known in ("create_task", "create_hearing", "complete_task"):
        assert reach[known][0], known
    return {t for t, (_, relocates) in reach.items() if relocates}


def test_a_schema_declares_previous_collection_cleared_iff_its_handler_emits_it():
    """« ONLY on tools that emit it »: ``_obj`` requires every listed key, so
    a schema declaring the key on a tool that never emits it would ship a
    violated contract — and a mover that emits it undeclared would ship an
    undocumented one. Lot 0 has no mover: both sides are empty today, and
    the first Lot 1 mover must move them together."""
    declared = {
        t for t, schema in OUTPUT_SCHEMAS.items()
        if "previous_collection_cleared" in schema.get("properties", {})
    }
    assert declared == _tools_passing_previous_dossier_id()
