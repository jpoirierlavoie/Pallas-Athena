"""chat/vertex.py — transport, taxonomy, pricing (Phase N)."""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from chat import vertex  # noqa: E402
from chat.vertex import ChatVertexFatal, ChatVertexRetryable  # noqa: E402
from config import Config  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None, text="corps"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


_VALID = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture()
def transport(monkeypatch):
    recorded = {}
    monkeypatch.setattr(vertex, "_bearer_token", lambda: "jeton-adc")

    def _post(url, json=None, headers=None, timeout=None):
        recorded.update(url=url, body=json, headers=headers, timeout=timeout)
        return recorded.get("response", _Response(payload=_VALID))

    monkeypatch.setattr(vertex.requests, "post", _post)
    return recorded


def _call(**kwargs):
    return vertex.call_model(
        "claude-sonnet-5",
        system=kwargs.get("system", [{"type": "text", "text": "charte"}]),
        messages=kwargs.get("messages", [{"role": "user", "content": []}]),
        tools=kwargs.get("tools", []),
    )


def test_url_and_body_shape(transport):
    _call(tools=[{"name": "t", "description": "d", "input_schema": {}}])
    assert transport["url"] == (
        "https://aiplatform.googleapis.com/v1/projects/test-project/"
        "locations/us/publishers/anthropic/models/claude-sonnet-5:rawPredict"
    )
    body = transport["body"]
    # The model goes in the URL, NEVER the body; the version in the body.
    assert "model" not in body
    assert body["anthropic_version"] == "vertex-2023-10-16"
    assert body["temperature"] == 1
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] == (
        Config.CHAT_MODELS["claude-sonnet-5"]["thinking_budget_tokens"]
    )
    assert body["max_tokens"] > body["thinking"]["budget_tokens"]
    assert transport["headers"]["Authorization"] == "Bearer jeton-adc"
    assert transport["timeout"] == (
        Config.CHAT_VERTEX_CONNECT_TIMEOUT_S,
        Config.CHAT_VERTEX_READ_TIMEOUT_S,
    )


def test_unknown_model_and_bad_budget_fail_preflight(monkeypatch):
    with pytest.raises(ChatVertexFatal) as excinfo:
        vertex.model_config("claude-fable-5")
    assert excinfo.value.reason == "unknown_model"
    monkeypatch.setitem(
        Config.CHAT_MODELS,
        "claude-sonnet-5",
        {**Config.CHAT_MODELS["claude-sonnet-5"],
         "max_tokens": 100, "thinking_budget_tokens": 200},
    )
    with pytest.raises(ChatVertexFatal) as excinfo:
        vertex.model_config("claude-sonnet-5")
    assert excinfo.value.reason == "config_thinking_budget"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_retryable_statuses(transport, status):
    transport["response"] = _Response(status)
    with pytest.raises(ChatVertexRetryable) as excinfo:
        _call()
    assert excinfo.value.reason == f"vertex_http_{status}"


@pytest.mark.parametrize(
    "status,reason",
    [(400, "vertex_invalid_request"), (401, "vertex_permission"),
     (403, "vertex_permission"), (404, "vertex_endpoint_absent")],
)
def test_fatal_statuses_carry_bounded_excerpt(transport, status, reason):
    transport["response"] = _Response(status, text="e" * 5000)
    with pytest.raises(ChatVertexFatal) as excinfo:
        _call()
    assert excinfo.value.reason == reason
    assert len(excinfo.value.excerpt) == 2000  # bounded, doc-only
    # str(exc) is the machine-stable reason — safe to cross a span.
    assert str(excinfo.value) == reason


def test_timeouts_and_connection_errors_are_retryable(monkeypatch):
    import requests as _requests

    monkeypatch.setattr(vertex, "_bearer_token", lambda: "t")
    monkeypatch.setattr(
        vertex.requests, "post", mock.Mock(side_effect=_requests.Timeout())
    )
    with pytest.raises(ChatVertexRetryable) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_timeout"


def test_malformed_success_body_is_fatal(transport):
    transport["response"] = _Response(200, {"pas": "une réponse"})
    with pytest.raises(ChatVertexFatal) as excinfo:
        _call()
    assert excinfo.value.reason == "vertex_bad_response"


def test_pricing_math_in_usd_micros():
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": {"web_search_requests": 100},
    }
    micros = vertex.segment_cost_usd_micros(usage, "claude-sonnet-5")
    # (3 + 15) USD × 1.10 + 100 × 10/1000 USD × … searches are unmultiplied.
    expected = int(round(((3.00 + 15.00) * 1.10 + 1.00) * 1_000_000))
    assert micros == expected
    # Unknown model → 0, honestly under-reporting rather than inventing.
    assert vertex.segment_cost_usd_micros(usage, "modele-inconnu") == 0
