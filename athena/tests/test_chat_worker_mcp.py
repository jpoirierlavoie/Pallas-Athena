"""chat/worker_client.py — the MCP transport to the legal-research Workers.

D5 said « REST simple à jeton »; the Workers turned out to be MCP servers,
and D5 was amended 2026-08-26. What this file pins is the seam that
amendment created:

* the JSON-RPC envelope, and above all that the REMOTE tool name goes on
  the wire — the model sees ``jurisprudence_canlii_verify_citations``, the
  Worker answers to ``canlii_verify_citations``, and conflating them turns
  every call into « outil inconnu »;
* that a tool's French text arrives VERBATIM, refusals included. The
  connectors carry their reliability warnings inside that prose; a client
  that re-wrapped, summarised or dropped it would hand the model a verdict
  stripped of its reserve, and nothing else in the suite would notice;
* the two framings (JSON and SSE) and the session handshake, so the twin
  Worker drops in without this file being rewritten under it.

Import preamble mirrors tests/test_chat_registry.py.
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from chat import executors, registry, worker_client  # noqa: E402

SPEC = {
    "name": "jurisprudence_canlii_verify_citations",
    "worker": "jurisprudence",
    "tool": "canlii_verify_citations",
    "path": "/mcp",
    "transport": "mcp",
}

PROSE = (
    "CONFIRMÉE — Québec (Procureur général) c. Untel, 2020 QCCA 495. "
    "Établit l'existence et l'identité, jamais l'autorité actuelle."
)


class FakeResponse:
    """Enough of a `requests` response for the client's bounded read."""

    def __init__(self, status_code=200, body=b"", content_type="application/json",
                 session=None):
        self.status_code = status_code
        self._body = body
        self.headers = {"Content-Type": content_type}
        if session:
            self.headers["Mcp-Session-Id"] = session

    def iter_content(self, chunk_size=65536):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def rpc_result(text, is_error=False):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "JURISPRUDENCE_WORKER_URL", "https://j.example")
    monkeypatch.setattr(Config, "JURISPRUDENCE_WORKER_TOKEN", "jeton-juris")
    monkeypatch.setattr(worker_client, "_SESSIONS", {})
    monkeypatch.setattr(worker_client, "_REQUIRES_HANDSHAKE", {})


def _capture(monkeypatch, response):
    """Patch requests.post and hand back the recorded calls."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, stream=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return response() if callable(response) else response

    monkeypatch.setattr(worker_client.requests, "post", fake_post)
    return calls


# ── The envelope ────────────────────────────────────────────────────────────


def test_the_wire_carries_the_remote_name_not_the_namespaced_one(monkeypatch):
    calls = _capture(monkeypatch, FakeResponse(200, rpc_result(PROSE)))
    worker_client.call_worker(SPEC, {"citations": [{"citation": "2020 QCCA 495"}]})

    assert len(calls) == 1
    body = calls[0]["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    # THE point of this test: the Worker never heard of the prefixed name.
    assert body["params"]["name"] == "canlii_verify_citations"
    assert body["params"]["name"] != SPEC["name"]
    assert body["params"]["arguments"] == {
        "citations": [{"citation": "2020 QCCA 495"}]
    }
    assert calls[0]["url"] == "https://j.example/mcp"


def test_headers_declare_both_framings_and_carry_the_bearer(monkeypatch):
    calls = _capture(monkeypatch, FakeResponse(200, rpc_result(PROSE)))
    worker_client.call_worker(SPEC, {})

    headers = calls[0]["headers"]
    assert headers["Authorization"] == "Bearer jeton-juris"
    # A server that answers SSE refuses a client that did not say it reads it.
    assert "application/json" in headers["Accept"]
    assert "text/event-stream" in headers["Accept"]
    assert headers["MCP-Protocol-Version"] == worker_client.PROTOCOL_VERSION


# ── The text, verbatim ──────────────────────────────────────────────────────


def test_the_tools_text_comes_back_untouched(monkeypatch):
    _capture(monkeypatch, FakeResponse(200, rpc_result(PROSE)))
    assert worker_client.call_worker(SPEC, {}) == {"ok": True, "text": PROSE}


def test_a_refusal_keeps_its_french_reason(monkeypatch):
    """`isError` is the tool refusing, not the service breaking. The model
    corrects itself on the reason — a generic message would erase it."""
    refusal = "« citations » doit contenir au moins 1 élément."
    _capture(monkeypatch, FakeResponse(200, rpc_result(refusal, is_error=True)))

    result = worker_client.call_worker(SPEC, {"citations": []})
    assert result["ok"] is False
    assert result["reason"] == "tool_error"
    assert result["message"] == refusal


def test_several_text_blocks_are_joined_in_order(monkeypatch):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "premier"},
                    {"type": "image", "data": "ignoré"},
                    {"type": "text", "text": "second"},
                ],
                "isError": False,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    _capture(monkeypatch, FakeResponse(200, body))
    assert worker_client.call_worker(SPEC, {})["text"] == "premier\nsecond"


def test_the_executor_hands_the_prose_over_without_a_json_envelope(monkeypatch):
    """The regression this guards: `_serialize` would wrap the verdict and
    its reserve in quotes and escapes, for no reader's benefit."""
    monkeypatch.setattr(registry, "find_worker_spec", lambda name: SPEC)
    monkeypatch.setattr(registry, "executor_for", lambda name: registry.HTTP_WORKER)
    monkeypatch.setattr(
        executors, "call_worker", lambda spec, args: {"ok": True, "text": PROSE}
    )
    monkeypatch.setattr(executors, "log_chat_event", lambda *a, **k: None)

    outcome = executors.execute_tool(
        SPEC["name"], {}, conversation_id="c", turn_id="t", step=1
    )
    assert outcome.is_error is False
    assert outcome.content == PROSE
    assert not outcome.content.startswith('"')


# ── Protocol faults ─────────────────────────────────────────────────────────


def test_a_jsonrpc_error_is_a_protocol_fault_not_an_empty_result(monkeypatch):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
    ).encode("utf-8")
    _capture(monkeypatch, FakeResponse(200, body))

    result = worker_client.call_worker(SPEC, {})
    assert result["ok"] is False
    assert result["reason"] == "mcp_error"
    # HTTP 200 with an `error` member: reading only the status would have
    # reported success on a call that never ran.


def test_a_result_that_is_not_an_object_is_refused(monkeypatch):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "texte nu"}).encode("utf-8")
    _capture(monkeypatch, FakeResponse(200, body))
    assert worker_client.call_worker(SPEC, {})["reason"] == "bad_envelope"


def test_an_unknown_transport_is_refused_rather_than_guessed(monkeypatch):
    _capture(monkeypatch, FakeResponse(200, rpc_result(PROSE)))
    spec = {**SPEC, "transport": "rest"}
    assert worker_client.call_worker(spec, {})["reason"] == "unsupported_transport"


def test_an_unconfigured_worker_never_reaches_the_network(monkeypatch):
    monkeypatch.setattr(Config, "JURISPRUDENCE_WORKER_TOKEN", "")
    monkeypatch.setattr(
        worker_client.requests, "post", mock.Mock(side_effect=AssertionError("appelé"))
    )
    assert worker_client.call_worker(SPEC, {})["reason"] == "not_configured"


# ── The twin's framing: SSE, and sessions ───────────────────────────────────


def test_an_sse_framed_response_is_read(monkeypatch):
    """The twin Worker answers text/event-stream to the same request."""
    frame = b"event: message\ndata: " + rpc_result(PROSE) + b"\n\n"
    _capture(monkeypatch, FakeResponse(200, frame, content_type="text/event-stream"))
    assert worker_client.call_worker(SPEC, {}) == {"ok": True, "text": PROSE}


def test_a_session_id_is_captured_and_replayed(monkeypatch):
    calls = _capture(
        monkeypatch, FakeResponse(200, rpc_result(PROSE), session="sess-1")
    )
    worker_client.call_worker(SPEC, {})
    assert worker_client._SESSIONS["jurisprudence"] == "sess-1"

    worker_client.call_worker(SPEC, {})
    assert calls[1]["headers"]["Mcp-Session-Id"] == "sess-1"


def test_an_expired_session_is_dropped_but_not_retried_behind_the_model(monkeypatch):
    """A 404 on a stale session must not become a silent second call: the
    no-retry rule exists because a turn has a latency budget."""
    worker_client._SESSIONS["jurisprudence"] = "sess-vieille"
    calls = _capture(monkeypatch, FakeResponse(404, b""))

    result = worker_client.call_worker(SPEC, {})
    assert result["reason"] == "http_404"
    assert len(calls) == 1
    assert "jurisprudence" not in worker_client._SESSIONS


def test_the_handshake_is_inert_for_a_stateless_worker(monkeypatch):
    calls = _capture(monkeypatch, FakeResponse(200, rpc_result(PROSE)))
    worker_client.call_worker(SPEC, {})
    assert len(calls) == 1  # tools/call only — no initialize
    assert calls[0]["body"]["method"] == "tools/call"


def test_the_handshake_runs_when_a_worker_declares_it(monkeypatch):
    monkeypatch.setattr(
        worker_client, "_REQUIRES_HANDSHAKE", {"jurisprudence": True}
    )
    responses = iter(
        [
            FakeResponse(
                200,
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}}
                ).encode("utf-8"),
                session="sess-neuve",
            ),
            FakeResponse(202, b""),
            FakeResponse(200, rpc_result(PROSE)),
        ]
    )
    calls = _capture(monkeypatch, lambda: next(responses))

    assert worker_client.call_worker(SPEC, {}) == {"ok": True, "text": PROSE}
    assert [c["body"]["method"] for c in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert calls[2]["headers"]["Mcp-Session-Id"] == "sess-neuve"
