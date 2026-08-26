"""models/chat_conversation.py — the registre's transactional guards.

Fake-Firestore harness: the test_chat_draft.py extension of the
test_admin_ledger canon (subcollections + batch), plus multi-collection
transactional reads (conversation + usage roll-up + scheduled task).
"""

import copy
import importlib
import importlib.util
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name: str, module: types.ModuleType) -> None:
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg in sys.modules:
            continue
        if _module_available(pkg):
            importlib.import_module(pkg)
            continue
        pkg_module = types.ModuleType(pkg)
        pkg_module.__path__ = []
        sys.modules[pkg] = pkg_module
        if i > 1:
            setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], pkg_module)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _module_available("google.cloud.firestore"):
    _firestore_stub = types.ModuleType("google.cloud.firestore")

    class _StubQuery:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"

    _firestore_stub.Client = mock.MagicMock(name="firestore.Client")
    _firestore_stub.Query = _StubQuery
    _firestore_stub.Transaction = type("Transaction", (), {})
    _firestore_stub.transactional = lambda func: func
    _install_stub("google.cloud.firestore", _firestore_stub)


with mock.patch("google.cloud.firestore.Client"):
    import models.chat_conversation as cc


# ── Fake Firestore (test_chat_draft harness) ────────────────────────────────


class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def collection(self, name):
        return _FakeCollectionRef(self._store, f"{self._coll}/{self.id}/{name}")

    def get(self, transaction=None):
        return _FakeSnapshot(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def update(self, fields):
        doc = self._store.setdefault(self._coll, {}).get(self.id)
        if doc is None:
            raise KeyError(f"update on missing {self._coll}/{self.id}")
        doc.update(copy.deepcopy(fields))


class _FakeQuery:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._orders = []
        self._limit = None

    def order_by(self, field, direction="ASCENDING"):
        self._orders.append((field, direction))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _rows(self):
        rows = list(self._store.get(self._coll, {}).values())
        for field, direction in reversed(self._orders):
            rows.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction == "DESCENDING"),
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def stream(self, transaction=None):
        return [_FakeSnapshot(d.get("id"), d) for d in self._rows()]


class _FakeCollectionRef(_FakeQuery):
    def document(self, doc_id):
        return _FakeDocRef(self._store, self._coll, doc_id)


class _FakeTransaction:
    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, fields):
        ref.update(fields)


class _FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCollectionRef(self._store, name)

    def transaction(self):
        return _FakeTransaction()


class _FakeFirestore:
    transactional = staticmethod(lambda fn: fn)

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(cc, "db", _FakeDB(data))
    monkeypatch.setattr(cc, "firestore", _FakeFirestore)
    return data


def _new_conversation(store, **overrides):
    doc, errors = cc.create_conversation(
        {
            "title": "Analyse du dossier",
            "model": "claude-sonnet-5",
            "dossier_id": overrides.pop("dossier_id", "d1"),
            "owner_uid": "u1",
            **overrides,
        }
    )
    assert errors == []
    return doc


def _turns(store, conversation_id):
    return store.get(f"chat_conversations/{conversation_id}/turns", {})


_SEGMENT = {
    "step": 1,
    "model": "claude-sonnet-5",
    "blocks": [{"type": "text", "text": "Réponse."}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
        "server_tool_use": {"web_search_requests": 2},
    },
    "pricing": {"version": "2026-08-26", "usd_micros": 1234},
    "tool_results": None,
}


# ── Conversation + start_turn ───────────────────────────────────────────────

def test_create_conversation_validates_model_against_the_closed_allowlist(store):
    doc, errors = cc.create_conversation(
        {"title": "X", "model": "claude-fable-5"}
    )
    assert doc is None
    assert any("allowlist" in e for e in errors)


def test_start_turn_appends_pair_and_marks_in_flight(store):
    conv = _new_conversation(store)
    turn, errors = cc.start_turn(conv["id"], "Bonjour")
    assert errors == []
    assert turn["state"] == "pending"
    assert turn["continuation"] == {"token": turn["step_token"], "enqueued": False}
    turns = _turns(store, conv["id"])
    assert set(turns) == {"000001", "000002"}
    assert turns["000001"]["role"] == "user"
    assert turns["000001"]["content"] == [{"type": "text", "text": "Bonjour"}]
    conv_doc = store["chat_conversations"][conv["id"]]
    assert conv_doc["active_turn_id"] == "000002"
    assert conv_doc["turn_count"] == 2


def test_start_turn_refuses_while_a_turn_is_in_flight(store):
    conv = _new_conversation(store)
    cc.start_turn(conv["id"], "Premier")
    turn, errors = cc.start_turn(conv["id"], "Deuxième")
    assert turn is None
    assert any("déjà en cours" in e for e in errors)
    assert len(_turns(store, conv["id"])) == 2  # nothing appended


# ── claim_step ──────────────────────────────────────────────────────────────

def test_claim_proceeds_and_counts_the_started_call(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    status, claimed, repair = cc.claim_step(
        conv["id"], turn["id"], turn["step_token"]
    )
    assert (status, repair) == ("proceed", None)
    assert claimed["state"] == "running"
    assert claimed["vertex_calls_started"] == 1
    stored = _turns(store, conv["id"])["000002"]
    assert stored["state"] == "running"


def test_claim_skips_terminal_and_stale_tokens(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    # Terminal → skip.
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final", segment=dict(_SEGMENT),
    )
    status, _t, _r = cc.claim_step(conv["id"], turn["id"], turn["step_token"])
    assert status == "skip"


def test_claim_repairs_a_rotated_but_never_enqueued_token(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    status, new_token = cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="running", segment=dict(_SEGMENT),
    )
    assert status == "committed"
    # The OLD task redelivers with the OLD token; the continuation was
    # never marked enqueued → REPAIR with the current token.
    status, _t, repair = cc.claim_step(conv["id"], turn["id"], turn["step_token"])
    assert (status, repair) == ("repair", new_token)
    # Once marked enqueued, the stale delivery is a plain skip.
    cc.mark_enqueued(conv["id"], turn["id"], new_token)
    status, _t, repair = cc.claim_step(conv["id"], turn["id"], turn["step_token"])
    assert (status, repair) == ("skip", None)


def test_mark_enqueued_ignores_a_stale_token(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    cc.mark_enqueued(conv["id"], turn["id"], "jeton-perime")
    stored = _turns(store, conv["id"])["000002"]
    assert stored["continuation"]["enqueued"] is False


# ── commit_step ─────────────────────────────────────────────────────────────

def test_commit_wrong_token_is_a_lost_race_nothing_written(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    status, token = cc.commit_step(
        conv["id"], turn["id"], "jeton-perime",
        next_state="running", segment=dict(_SEGMENT),
    )
    assert (status, token) == ("lost_race", None)
    assert _turns(store, conv["id"])["000002"]["segments"] == []


def test_commit_terminal_folds_the_accounting_in_one_transaction(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    status, _tok = cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final", segment=dict(_SEGMENT),
    )
    assert status == "committed"
    stored = _turns(store, conv["id"])["000002"]
    assert stored["state"] == "final"
    assert stored["finalized_at"] is not None
    assert stored["continuation"] is None
    assert stored["vertex_calls_recorded"] == 1
    conv_doc = store["chat_conversations"][conv["id"]]
    assert conv_doc["active_turn_id"] == ""
    assert conv_doc["token_totals"]["input_tokens"] == 100
    assert conv_doc["token_totals"]["web_search_requests"] == 2
    assert conv_doc["token_totals"]["model_calls"] == 1
    assert conv_doc["cost_snapshot"]["usd_micros_total"] == 1234
    usage = store["chat_usage_dossier"]["d1"]
    assert usage["token_totals"]["output_tokens"] == 50
    assert usage["usd_micros_total"] == 1234


def test_commit_terminal_increments_an_existing_usage_rollup(store):
    conv = _new_conversation(store)
    for _round in range(2):
        turn, _ = cc.start_turn(conv["id"], "Bonjour")
        cc.commit_step(
            conv["id"], turn["id"], turn["step_token"],
            next_state="final", segment=dict(_SEGMENT),
        )
    usage = store["chat_usage_dossier"]["d1"]
    assert usage["token_totals"]["input_tokens"] == 200
    assert usage["usd_micros_total"] == 2468


def test_commit_terminal_updates_the_scheduled_task_totals(store):
    store["chat_scheduled_tasks"] = {
        "t1": {"id": "t1", "usage_totals": {}, "usd_micros_total": 0}
    }
    conv = _new_conversation(
        store, dossier_id="", origin="planifiee", scheduled_task_id="t1"
    )
    turn, _ = cc.start_turn(conv["id"], "Rapport", by="planificateur")
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final", segment=dict(_SEGMENT),
    )
    task = store["chat_scheduled_tasks"]["t1"]
    assert task["usage_totals"]["input_tokens"] == 100
    assert task["usd_micros_total"] == 1234
    # Floating run: no dossier roll-up was minted.
    assert "chat_usage_dossier" not in store


def test_commit_attaches_tool_results_to_the_previous_segment(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    first = {**_SEGMENT, "stop_reason": "tool_use"}
    _status, token = cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="awaiting_authorization", segment=first,
        authorization={"calls": [], "decision": None},
    )
    results = [{"type": "tool_result", "tool_use_id": "x", "content": []}]
    decision_status, new_token = cc.decide_authorization(
        conv["id"], turn["id"], approved=["x"], refused=[]
    )
    assert decision_status == "ok"
    status, _tok = cc.commit_step(
        conv["id"], turn["id"], new_token,
        next_state="final",
        segment=dict(_SEGMENT),
        last_segment_tool_results=results,
    )
    assert status == "committed"
    segments = _turns(store, conv["id"])["000002"]["segments"]
    assert segments[0]["tool_results"] == results
    assert len(segments) == 2


# ── fail_turn / decide_authorization ────────────────────────────────────────

def test_fail_turn_terminalizes_regardless_of_token_and_is_idempotent(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    assert cc.fail_turn(conv["id"], turn["id"], reason="retry_exhausted")
    stored = _turns(store, conv["id"])["000002"]
    assert stored["state"] == "failed"
    assert stored["error"]["code"] == "retry_exhausted"
    assert store["chat_conversations"][conv["id"]]["active_turn_id"] == ""
    # Idempotent: a second call leaves the terminal state untouched.
    assert cc.fail_turn(conv["id"], turn["id"], reason="autre") is True
    assert _turns(store, conv["id"])["000002"]["error"]["code"] == "retry_exhausted"


def test_decide_authorization_only_from_the_pause_and_only_once(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    status, _tok = cc.decide_authorization(
        conv["id"], turn["id"], approved=[], refused=[]
    )
    assert status == "invalid"  # not awaiting
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="awaiting_authorization",
        segment={**_SEGMENT, "stop_reason": "tool_use"},
        authorization={"calls": [], "decision": None},
    )
    status, token = cc.decide_authorization(
        conv["id"], turn["id"], approved=["a"], refused=["b"]
    )
    assert status == "ok" and token
    stored = _turns(store, conv["id"])["000002"]
    assert stored["state"] == "running"
    assert stored["authorization"]["decision"]["approved"] == ["a"]
    # A second decision (double-click, stale tab) is refused.
    status, _tok = cc.decide_authorization(
        conv["id"], turn["id"], approved=[], refused=[]
    )
    assert status == "invalid"


# ── Pure helpers + the no-delete pin ────────────────────────────────────────

def test_sum_segments_is_plain_python_arithmetic():
    totals = cc.sum_segments([dict(_SEGMENT), dict(_SEGMENT)])
    assert totals["input_tokens"] == 200
    assert totals["output_tokens"] == 100
    assert totals["cache_creation_input_tokens"] == 20
    assert totals["cache_read_input_tokens"] == 10
    assert totals["web_search_requests"] == 4
    assert totals["model_calls"] == 2
    assert totals["usd_micros"] == 2468
    assert cc.sum_segments([]) == {**cc._ZERO_TOTALS, "usd_micros": 0}


def test_list_turns_is_chronological(store):
    conv = _new_conversation(store)
    turn, _ = cc.start_turn(conv["id"], "Bonjour")
    cc.commit_step(
        conv["id"], turn["id"], turn["step_token"],
        next_state="final", segment=dict(_SEGMENT),
    )
    cc.start_turn(conv["id"], "Suite")
    turns = cc.list_turns(conv["id"])
    assert [t["seq"] for t in turns] == [1, 2, 3, 4]


def test_no_delete_exists_in_the_module():
    for attr in dir(cc):
        assert not attr.startswith("delete"), attr
    with open(cc.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "def delete" not in source
    assert ".delete(" not in source
