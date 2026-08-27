"""chat/turn_engine.py — the claim → work → commit chain (Phase N).

Integration-style: the REAL models/chat_conversation runs against the fake
Firestore store (the test_chat_conversation harness), Vertex is a scripted
double, Storage a dict. This is where the SPEC's acceptance tests 1-4, 8
and 13 live: duplicate suppression with ZERO model calls, pause_turn
replayed verbatim, the chain ceiling failing loud, byte-exact rehydration
through a storage_ref, and the authorization pause/resume.
"""

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name, module):
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
    from chat import turn_engine
    from chat.vertex import ChatVertexFatal, ChatVertexRetryable
    from config import Config


# ── Fakes (harness of test_chat_conversation + Storage + Vertex) ────────────


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
        _refuser_tableaux_imbriques(data)
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def update(self, fields):
        _refuser_tableaux_imbriques(fields)
        doc = self._store.setdefault(self._coll, {}).get(self.id)
        if doc is None:
            raise KeyError(f"update on missing {self._coll}/{self.id}")
        doc.update(copy.deepcopy(fields))


def _refuser_tableaux_imbriques(valeur, chemin=""):
    """Firestore REFUSE un tableau qui contient un tableau.

    Copié de tests/test_chat_draft.py — ce fichier porte sa PROPRE copie du
    harnais, et c'est exactement pourquoi le défaut du 2026-08-26 est passé :
    la contrainte ajoutée à un seul des deux harnais n'aurait rien gardé ici.
    Les deux doivent la modéliser, sinon le premier vrai tour la découvre en
    production (INVALID_ARGUMENT: Nested arrays are not allowed).
    """
    if isinstance(valeur, dict):
        for cle, v in valeur.items():
            _refuser_tableaux_imbriques(v, f"{chemin}.{cle}" if chemin else str(cle))
    elif isinstance(valeur, (list, tuple)):
        for i, v in enumerate(valeur):
            if isinstance(v, (list, tuple)):
                raise ValueError(
                    f"Nested arrays are not allowed: {chemin}[{i}]"
                )
            _refuser_tableaux_imbriques(v, f"{chemin}[{i}]")


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


class _FakeBlob:
    def __init__(self, files, path):
        self._files = files
        self._path = path

    def upload_from_string(self, data, content_type=""):
        self._files[self._path] = data

    def download_as_bytes(self):
        return self._files[self._path]


class _FakeStorage:
    def __init__(self, files):
        self._files = files

    def bucket(self):
        files = self._files

        class _Bucket:
            def blob(self, path):
                return _FakeBlob(files, path)

        return _Bucket()


class _ScriptedVertex:
    """Queue of canned responses (or exceptions); records every request."""

    def __init__(self):
        self.responses = []
        self.calls = []

    def __call__(self, model_key, *, system, messages, tools):
        self.calls.append(
            {
                "model": model_key,
                "system": copy.deepcopy(system),
                "messages": copy.deepcopy(messages),
                "tools": tools,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected Vertex call")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


def _usage(n=1):
    return {
        "input_tokens": 100 * n,
        "output_tokens": 10 * n,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def _response(blocks, stop_reason="end_turn"):
    return {"content": blocks, "stop_reason": stop_reason, "usage": _usage()}


@pytest.fixture()
def world(monkeypatch):
    store: dict = {}
    files: dict = {}
    events: list = []
    enqueued: list = []
    scripted = _ScriptedVertex()

    monkeypatch.setattr(cc, "db", _FakeDB(store))
    monkeypatch.setattr(cc, "firestore", _FakeFirestore)
    monkeypatch.setattr(turn_engine, "storage", _FakeStorage(files))
    monkeypatch.setattr(turn_engine.vertex, "call_model", scripted)
    monkeypatch.setattr(
        turn_engine.skill_model, "get_heads", lambda ids: []
    )
    monkeypatch.setattr(
        turn_engine,
        "log_chat_event",
        lambda event, outcome="success", **kw: events.append(
            {"event": event, "outcome": outcome, **kw}
        ),
    )
    monkeypatch.setattr(
        turn_engine.taches,
        "enfiler_tour",
        lambda cid, tid, token: enqueued.append((cid, tid, token)),
    )

    conv, errors = cc.create_conversation(
        {
            "title": "Analyse",
            "model": "claude-sonnet-5",
            "dossier_id": "d1",
            "owner_uid": "u1",
        }
    )
    assert errors == []
    turn, errors = cc.start_turn(conv["id"], "Bonjour")
    assert errors == []
    return SimpleNamespace(
        store=store,
        files=files,
        events=events,
        enqueued=enqueued,
        vertex=scripted,
        conv=conv,
        turn=turn,
    )


def _payload(world, token=None):
    return {
        "conversation_id": world.conv["id"],
        "turn_id": world.turn["id"],
        "step_token": token or world.turn["step_token"],
    }


def _stored_turn(world):
    return world.store[f"chat_conversations/{world.conv['id']}/turns"][
        world.turn["id"]
    ]


# ── Acceptance #2 — duplicate suppression, zero model calls ────────────────

def test_terminal_duplicate_is_skipped_without_a_model_call(world):
    world.vertex.responses = [_response([{"type": "text", "text": "Réponse."}])]
    assert turn_engine.process_task(_payload(world), 0) == "final"
    calls_after_first = len(world.vertex.calls)
    # The duplicate delivery of the SAME task observes the terminal state.
    assert turn_engine.process_task(_payload(world), 1) == "skip"
    assert len(world.vertex.calls) == calls_after_first
    assert any(e["event"] == "chat_duplicate_delivery" for e in world.events)


# ── The plain end_turn chain ────────────────────────────────────────────────

def test_end_turn_finalizes_with_counters_and_stamps(world):
    world.vertex.responses = [_response([{"type": "text", "text": "Voici."}])]
    assert turn_engine.process_task(_payload(world), 0) == "final"
    stored = _stored_turn(world)
    assert stored["state"] == "final"
    assert stored["charter_version"] == 1
    assert stored["segments"][0]["pricing"]["usd_micros"] > 0
    conv_doc = world.store["chat_conversations"][world.conv["id"]]
    assert conv_doc["token_totals"]["model_calls"] == 1
    assert conv_doc["active_turn_id"] == ""
    assert world.enqueued == []
    assert any(e["event"] == "chat_turn_finalized" for e in world.events)
    # The request itself: charter first in system, tools carry the trailing
    # cache breakpoint, the user message closes the array.
    request = world.vertex.calls[0]
    assert request["system"][0]["text"].startswith("Tu es l'assistant")
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "Bonjour"}],
    }


# ── Tool chain: execute → continue → assemble verbatim ─────────────────────

def test_tool_use_executes_continues_and_replays_results(world, monkeypatch):
    executed = []

    def _fake_execute(name, args, **kw):
        executed.append((name, args, kw["unattended"]))
        return SimpleNamespace(content='{"ok": true}', is_error=False)

    monkeypatch.setattr(turn_engine.executors, "execute_tool", _fake_execute)
    tool_use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_dossier",
        "input": {"dossier_id": "d1"},
    }
    world.vertex.responses = [
        _response(
            [{"type": "text", "text": "Je consulte."}, tool_use], "tool_use"
        ),
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    assert executed == [("get_dossier", {"dossier_id": "d1"}, False)]
    assert len(world.enqueued) == 1
    stored = _stored_turn(world)
    assert stored["segments"][0]["tool_results"][0]["tool_use_id"] == "toolu_1"
    assert stored["continuation"]["enqueued"] is True

    # Continuation task: the assembled request replays the tool_use and its
    # result VERBATIM before the next call.
    world.vertex.responses = [_response([{"type": "text", "text": "Conclu."}])]
    next_token = stored["continuation"]["token"]
    assert turn_engine.process_task(_payload(world, next_token), 0) == "final"
    request = world.vertex.calls[1]
    roles = [m["role"] for m in request["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert request["messages"][1]["content"][1] == tool_use
    tool_result = request["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"


# ── pause_turn (the review's finding) ───────────────────────────────────────

def test_pause_turn_replays_the_paused_message_unchanged(world):
    paused_blocks = [
        {"type": "text", "text": "Recherche en cours"},
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "art. 2925"},
        },
    ]
    world.vertex.responses = [_response(paused_blocks, "pause_turn")]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    stored = _stored_turn(world)
    assert stored["state"] == "running"
    next_token = stored["continuation"]["token"]

    world.vertex.responses = [_response([{"type": "text", "text": "Fini."}])]
    assert turn_engine.process_task(_payload(world, next_token), 0) == "final"
    request = world.vertex.calls[1]
    # The paused assistant message is the LAST message, byte-identical.
    assert request["messages"][-1] == {
        "role": "assistant",
        "content": paused_blocks,
    }


# ── Acceptance #4 — the chain ceiling fails loud ────────────────────────────

def test_chain_ceiling_fails_loud(world, monkeypatch):
    monkeypatch.setattr(Config, "CHAT_CHAIN_MAX_CALLS", 1)
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda *a, **k: SimpleNamespace(content="{}", is_error=False),
    )
    world.vertex.responses = [
        _response(
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "get_dossier",
                    "input": {},
                }
            ],
            "tool_use",
        )
    ]
    assert turn_engine.process_task(_payload(world), 0) == "failed"
    stored = _stored_turn(world)
    assert stored["state"] == "failed"
    assert stored["error"]["code"] == "chain_ceiling"
    # The spent call and its tool results are still recorded — the registre
    # never loses paid work.
    assert stored["segments"][0]["tool_results"] is not None
    assert world.enqueued == []
    failure = [e for e in world.events if e["event"] == "chat_turn_failed"]
    assert failure and failure[0]["reason"] == "chain_ceiling"


# ── Retry exhaustion terminalizes ───────────────────────────────────────────

def test_retry_exhaustion_terminalizes_without_a_model_call(world, monkeypatch):
    monkeypatch.setattr(Config, "CHAT_TASK_RETRY_TERMINAL", 5)
    assert turn_engine.process_task(_payload(world), 5) == "terminalized"
    assert _stored_turn(world)["state"] == "failed"
    assert _stored_turn(world)["error"]["code"] == "retry_exhausted"
    assert world.vertex.calls == []


# ── Acceptance #3/#10 — offload + BYTE-EXACT rehydration ────────────────────

def test_offload_and_byte_exact_rehydration(world, monkeypatch):
    monkeypatch.setattr(Config, "CHAT_BLOCK_OFFLOAD_BYTES", 50)
    thinking = {
        "type": "thinking",
        "thinking": "Réflexion longue " * 20,
        "signature": "SIG-BYTES-EXACTS==",
    }
    world.vertex.responses = [
        _response([thinking, {"type": "text", "text": "suite"}], "pause_turn")
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    stored = _stored_turn(world)
    pointer = stored["segments"][0]["blocks"][0]
    assert pointer["type"] == "storage_ref"
    assert pointer["original_type"] == "thinking"
    assert pointer["path"] in world.files
    assert any(e["event"] == "chat_block_offloaded" for e in world.events)

    world.vertex.responses = [_response([{"type": "text", "text": "ok"}])]
    next_token = stored["continuation"]["token"]
    assert turn_engine.process_task(_payload(world, next_token), 0) == "final"
    request = world.vertex.calls[1]
    # The signature came back BYTE-EXACT — never the preview.
    assert request["messages"][-1]["content"][0] == thinking


def test_corrupt_storage_ref_fails_the_turn_loudly(world, monkeypatch):
    monkeypatch.setattr(Config, "CHAT_BLOCK_OFFLOAD_BYTES", 50)
    world.vertex.responses = [
        _response(
            [{"type": "thinking", "thinking": "x" * 200, "signature": "s"}],
            "pause_turn",
        )
    ]
    turn_engine.process_task(_payload(world), 0)
    stored = _stored_turn(world)
    path = stored["segments"][0]["blocks"][0]["path"]
    world.files[path] = b"contenu corrompu"
    next_token = stored["continuation"]["token"]
    assert turn_engine.process_task(_payload(world, next_token), 0) == "failed"
    assert _stored_turn(world)["error"]["code"] == "storage_ref_corrupt"
    assert len(world.vertex.calls) == 1  # the corrupt replay never called Vertex


# ── Enqueue failure → retry → REPAIR (the crash-gap closure) ───────────────

def test_enqueue_failure_retries_into_the_repair_branch(world, monkeypatch):
    def _boom(cid, tid, token):
        raise RuntimeError("queue indisponible")

    monkeypatch.setattr(turn_engine.taches, "enfiler_tour", _boom)
    world.vertex.responses = [
        _response([{"type": "text", "text": "…"}], "pause_turn")
    ]
    with pytest.raises(ChatVertexRetryable):
        turn_engine.process_task(_payload(world), 0)
    assert any(e["event"] == "chat_enqueue_failed" for e in world.events)
    stored = _stored_turn(world)
    assert stored["continuation"]["enqueued"] is False

    # The queue redelivers the SAME task (old token): the claim repairs by
    # re-enqueueing the rotated token — no Vertex call.
    repaired: list = []
    monkeypatch.setattr(
        turn_engine.taches,
        "enfiler_tour",
        lambda cid, tid, token: repaired.append(token),
    )
    assert turn_engine.process_task(_payload(world), 1) == "repair"
    assert repaired == [stored["continuation"]["token"]]
    assert len(world.vertex.calls) == 1


# ── Fatal Vertex errors ─────────────────────────────────────────────────────

def test_fatal_vertex_error_finalizes_failed_with_excerpt_in_doc_only(world):
    world.vertex.responses = [
        ChatVertexFatal("vertex_invalid_request", 400, "corps d'erreur détaillé")
    ]
    assert turn_engine.process_task(_payload(world), 0) == "failed"
    stored = _stored_turn(world)
    assert stored["error"]["code"] == "vertex_invalid_request"
    assert stored["error"]["excerpt"] == "corps d'erreur détaillé"
    failure = [e for e in world.events if e["event"] == "chat_turn_failed"]
    # The excerpt never reaches the log line.
    assert "excerpt" not in failure[0]
    assert "corps" not in json.dumps(failure)


def test_unexpected_engine_error_terminalizes_not_retries(world, monkeypatch):
    monkeypatch.setattr(
        turn_engine,
        "_assemble_messages",
        mock.Mock(side_effect=RuntimeError("bogue déterministe")),
    )
    monkeypatch.setattr(turn_engine, "log_unexpected", lambda *a, **k: None)
    assert turn_engine.process_task(_payload(world), 0) == "failed"
    assert _stored_turn(world)["error"]["code"] == "internal_error"
    assert world.vertex.calls == []


# ── Acceptance #13 — the authorization seam ─────────────────────────────────

def test_gated_tool_pauses_then_resume_executes_per_decision(world, monkeypatch):
    monkeypatch.setattr(
        turn_engine.registry, "GATED_TOOLS", frozenset({"create_note"})
    )
    executed = []
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: (
            executed.append(name),
            SimpleNamespace(content='{"fait": true}', is_error=False),
        )[1],
    )
    calls = [
        {"type": "tool_use", "id": "t-note", "name": "create_note", "input": {}},
        {"type": "tool_use", "id": "t-read", "name": "get_dossier", "input": {}},
    ]
    world.vertex.responses = [_response(calls, "tool_use")]
    assert turn_engine.process_task(_payload(world), 0) == "paused"
    stored = _stored_turn(world)
    assert stored["state"] == "awaiting_authorization"
    # A gated call in a parallel batch holds the WHOLE batch (§4.6.3).
    assert executed == []
    assert world.enqueued == []
    assert [c["name"] for c in stored["authorization"]["calls"]] == [
        "create_note",
        "get_dossier",
    ]

    # The lawyer refuses the gated call; the batch resumes: refused →
    # error tool_result, the rest executes, then the next model call.
    status, token = cc.decide_authorization(
        world.conv["id"], world.turn["id"],
        approved=["t-read"], refused=["t-note"],
    )
    assert status == "ok"
    world.vertex.responses = [_response([{"type": "text", "text": "Adapté."}])]
    assert turn_engine.process_task(_payload(world, token), 0) == "final"
    assert executed == ["get_dossier"]
    stored = _stored_turn(world)
    results = stored["segments"][0]["tool_results"]
    by_id = {r["tool_use_id"]: r for r in results}
    assert by_id["t-note"]["is_error"] is True
    assert "Refusé par l'avocat" in by_id["t-note"]["content"][0]["text"]
    assert by_id["t-read"]["is_error"] is False
    # The model's adapted continuation is recorded (acceptance #13).
    assert stored["segments"][1]["blocks"][0]["text"] == "Adapté."


def test_gated_tool_is_not_paused_in_unattended_context(world, monkeypatch):
    monkeypatch.setattr(
        turn_engine.registry, "GATED_TOOLS", frozenset({"create_note"})
    )
    seen = []
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: (
            seen.append(kw["unattended"]),
            SimpleNamespace(content="refus", is_error=True),
        )[1],
    )
    # Make the pending turn an unattended one.
    _stored_turn(world)["addendum"] = "unattended"
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t1", "name": "create_note",
              "input": {}}],
            "tool_use",
        ),
        _response([{"type": "text", "text": "Rapport."}]),
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    assert seen == [True]  # executed (auto-refused inside), never paused
    assert _stored_turn(world)["state"] == "running"


# ── Reference files: get_skill_file through the whole engine ───────────────

_HEAD_WITH_FILES = {
    "id": "s-doc",
    "name": "Rédaction",
    "current_version": 4,
    "active": True,
    "body": "corps",
    "files": [
        {"name": "Guide", "description": "Style.", "sha256": "a" * 64,
         "chars": 5}
    ],
}


def test_get_skill_file_end_to_end_pins_this_turns_version(world, monkeypatch):
    # First engine test with NON-empty skill heads: the REAL execute_tool
    # runs (only the Firestore reader is faked), so the step-1 in-memory
    # pairs → executor → stamped skill_versions chain is exercised whole.
    monkeypatch.setattr(
        turn_engine.skill_model,
        "get_heads",
        lambda ids: [copy.deepcopy(_HEAD_WITH_FILES)],
    )
    monkeypatch.setattr(
        turn_engine.executors, "log_chat_event", lambda *a, **k: None
    )
    reads = []

    def _fake_read(skill_id, version, filename):
        reads.append((skill_id, version, filename))
        return "Contenu du guide.", None

    monkeypatch.setattr(turn_engine.executors, "_read_skill_file", _fake_read)
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t1", "name": "get_skill_file",
              "input": {"skill_id": "s-doc", "filename": "Guide"}}],
            "tool_use",
        ),
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    stored = _stored_turn(world)
    # Step 1: the turn doc was NOT yet stamped when the tool ran — the
    # in-memory pairs served the read, and the SAME pairs were stamped.
    assert reads == [("s-doc", 4, "Guide")]
    assert stored["skill_versions"] == [{"skill_id": "s-doc", "version": 4}]
    result = stored["segments"][0]["tool_results"][0]
    assert result["is_error"] is False
    assert result["content"][0]["text"] == "Contenu du guide."
    # The system prompt carried the listing INSIDE the COMPÉTENCE block.
    system_1 = world.vertex.calls[0]["system"]
    assert len(system_1) == 2
    assert "FICHIERS DE RÉFÉRENCE" in system_1[1]["text"]
    assert "skill_id : s-doc" in system_1[1]["text"]
    assert "- Guide — Style. (5 caractères)" in system_1[1]["text"]

    world.vertex.responses = [_response([{"type": "text", "text": "Fini."}])]
    token = stored["continuation"]["token"]
    assert turn_engine.process_task(_payload(world, token), 0) == "final"
    # Cross-step byte stability of the cached prefix (no edit in between).
    assert world.vertex.calls[1]["system"] == system_1


def test_get_skill_file_resume_from_authorization_uses_stamped_pairs(
    world, monkeypatch
):
    # Risk-1 pin: a skill REVISED during the human pause must not move the
    # file read — the pause commit stamped the pairs, and resume resolves
    # through the STAMPED version, not the fresh head.
    monkeypatch.setattr(
        turn_engine.registry, "GATED_TOOLS", frozenset({"create_note"})
    )
    monkeypatch.setattr(
        turn_engine.skill_model,
        "get_heads",
        lambda ids: [copy.deepcopy(_HEAD_WITH_FILES)],
    )
    monkeypatch.setattr(
        turn_engine.executors, "log_chat_event", lambda *a, **k: None
    )
    reads = []

    def _fake_read(skill_id, version, filename):
        reads.append((skill_id, version, filename))
        return "Contenu.", None

    monkeypatch.setattr(turn_engine.executors, "_read_skill_file", _fake_read)
    calls = [
        {"type": "tool_use", "id": "t-gated", "name": "create_note",
         "input": {}},
        {"type": "tool_use", "id": "t-file", "name": "get_skill_file",
         "input": {"skill_id": "s-doc", "filename": "Guide"}},
    ]
    world.vertex.responses = [_response(calls, "tool_use")]
    assert turn_engine.process_task(_payload(world), 0) == "paused"
    assert _stored_turn(world)["skill_versions"] == [
        {"skill_id": "s-doc", "version": 4}]

    # The compétence is revised while the lawyer deliberates…
    revised = {**copy.deepcopy(_HEAD_WITH_FILES), "current_version": 5}
    monkeypatch.setattr(
        turn_engine.skill_model, "get_heads", lambda ids: [revised]
    )
    status, token = cc.decide_authorization(
        world.conv["id"], world.turn["id"],
        approved=["t-file"], refused=["t-gated"],
    )
    assert status == "ok"
    world.vertex.responses = [_response([{"type": "text", "text": "Fini."}])]
    assert turn_engine.process_task(_payload(world, token), 0) == "final"
    # …and the file still resolved at the version stamped at the pause.
    assert reads == [("s-doc", 4, "Guide")]


# ── The native-PDF fallback (D2) ────────────────────────────────────────────

def test_scanned_pdf_result_attaches_the_native_document(world, monkeypatch):
    payload = {
        "found": True, "readable": True, "pagination_unit": "page",
        "page_count": 3,
        "pages": [{"page": 1, "text": "", "has_text": False,
                   "page_truncated": False}],
        "pages_without_text": [1], "truncated": False, "next_page": 2,
        "warnings": [],
    }
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: SimpleNamespace(
            content=json.dumps(payload), is_error=False
        ),
    )
    monkeypatch.setattr(
        turn_engine.document_model,
        "get_document_bytes",
        lambda i, **kw: (b"%PDF-fake", ""),
    )
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t1", "name": "get_document_text",
              "input": {"document_id": "doc9"}}],
            "tool_use",
        ),
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    results = _stored_turn(world)["segments"][0]["tool_results"]
    kinds = [r.get("type") for r in results]
    assert kinds[0] == "tool_result"
    assert "document" in kinds
    document = next(r for r in results if r.get("type") == "document")
    assert document["source"]["media_type"] == "application/pdf"


# ── Le socle du versionnement de la charte (lot 0) ──────────────────────────
#
# Trois défauts latents, invisibles tant que la charte est une constante et
# fatals dès qu'elle varie. Ils sont réparés AVANT que la charte devienne
# éditable, précisément pour qu'aucun d'eux ne soit imputé au lot suivant.


def test_stamp_is_written_once_even_with_zero_skills(world, monkeypatch):
    """La garde d'estampille lit `charter_version`, jamais `skill_versions`.

    Une conversation sans aucune compétence a `skill_versions == []`, qui
    est falsy : l'ancienne garde ne gardait donc RIEN, et l'estampille se
    réécrivait à chaque pas de la chaîne. Inoffensif tant que la charte
    est une constante ; un déplacement silencieux de la provenance du
    registre en milieu de chaîne dès qu'elle ne l'est plus.
    """
    # Zéro compétence — c'est le défaut de la fixture, et c'est le cas.
    assert turn_engine.skill_model.get_heads([]) == []
    versions_vues = []
    vrai_commit = cc.commit_step

    def _espion(cid, tid, token, **kw):
        versions_vues.append(("stamps" in kw and kw["stamps"] is not None))
        return vrai_commit(cid, tid, token, **kw)

    monkeypatch.setattr(turn_engine.conv_model, "commit_step", _espion)
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t1", "name": "get_dossier",
              "input": {"dossier_id": "d1"}}],
            "tool_use",
        ),
        _response([{"type": "text", "text": "Voici."}]),
    ]
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: SimpleNamespace(content="{}", is_error=False),
    )
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    jeton = _stored_turn(world)["step_token"]
    assert turn_engine.process_task(_payload(world, jeton), 0) == "final"
    # Deux commits, UNE seule estampille — la première.
    assert versions_vues == [True, False]
    assert _stored_turn(world)["charter_version"] == turn_engine.charter.CHARTER_VERSION


def test_draft_written_at_step_one_records_the_charter_version(world, monkeypatch):
    """Le cas le plus fréquent de tous : un save_draft au premier lot.

    L'estampille arrive sur le commit qui SUIT les outils, donc au pas 1 le
    document de tour lit encore None. Les compétences avaient déjà leur
    repli en mémoire ; la charte non — sa provenance partait vide.
    """
    vus = {}
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: (
            vus.update(kw.get("provenance_extra") or {}),
            SimpleNamespace(content="{}", is_error=False),
        )[1],
    )
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t1", "name": "save_draft", "input": {}}],
            "tool_use",
        ),
    ]
    assert turn_engine.process_task(_payload(world), 0) == "continue"
    # Le document de tour lisait None à cet instant précis…
    assert vus["charter_version"] == turn_engine.charter.CHARTER_VERSION


def test_charter_resolution_precedes_pending_tool_execution(world, monkeypatch):
    """L'ordre dans `_advance` est porteur, pas cosmétique.

    La résolution de la charte est la seule partie d'`_advance` qui pourra
    lever un RETRYABLE, et le bloc des outils en attente exécute des appels
    APPROUVÉS — sans clé d'idempotence en interactif. Une levée après eux
    ferait rejouer chaque écriture à la redélivrance : notes en double,
    brouillons en double. La mutation qui remet la résolution sous le bloc
    fait tomber ce test.
    """
    monkeypatch.setattr(
        turn_engine.registry, "GATED_TOOLS", frozenset({"create_note"})
    )
    executes = []
    monkeypatch.setattr(
        turn_engine.executors,
        "execute_tool",
        lambda name, args, **kw: (
            executes.append(name),
            SimpleNamespace(content="{}", is_error=False),
        )[1],
    )
    world.vertex.responses = [
        _response(
            [{"type": "tool_use", "id": "t-note", "name": "create_note",
              "input": {}}],
            "tool_use",
        )
    ]
    assert turn_engine.process_task(_payload(world), 0) == "paused"
    assert executes == []

    status, token = cc.decide_authorization(
        world.conv["id"], world.turn["id"], approved=["t-note"], refused=[]
    )
    assert status == "ok"

    # L'assemblage échoue à la reprise, comme le fera une lecture Firestore
    # de la charte en panne.
    def _panne(*a, **kw):
        raise turn_engine.vertex.ChatVertexRetryable("charter_unreadable")

    monkeypatch.setattr(turn_engine.charter, "system_blocks", _panne)
    with pytest.raises(turn_engine.vertex.ChatVertexRetryable):
        turn_engine.process_task(_payload(world, token), 0)
    # L'écriture approuvée n'a PAS eu lieu : la redélivrance ne la doublera pas.
    assert executes == []
