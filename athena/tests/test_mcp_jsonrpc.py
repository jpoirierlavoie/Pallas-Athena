"""Tests for the MCP JSON-RPC endpoint (transport rules + dispatch)."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# config.py resolves env vars at import time — set the minimum before any
# app module import.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask

# models/__init__.py instantiates the Firestore client at import time —
# patch the constructor so no credentials are required (same pattern as
# test_dashboard_aggregation.py).
with mock.patch("google.cloud.firestore.Client"):
    import models  # noqa: F401
    import mcp as mcp_pkg
    import mcp.bearer as bearer
    import mcp.endpoint as endpoint_module  # noqa: F401 — attaches routes
    import mcp.handlers as handlers
    import mcp.oauth as oauth_module  # noqa: F401 — attaches routes
    import mcp.store as store

UTC = timezone.utc
ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUTH = {"Authorization": "Bearer test-token"}


def _make_app(**config) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(ATHENA_DIR, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["MCP_ENABLED"] = True
    app.config["MCP_CANONICAL_ORIGIN"] = "https://athena.poirierlavoie.ca"
    app.config["ENV"] = "development"
    app.config["RATELIMIT_ENABLED"] = False
    app.config.update(config)

    from security import csrf, limiter

    csrf.init_app(app)
    limiter.init_app(app)
    mcp_pkg.register_mcp(app)
    csrf.exempt(mcp_pkg.mcp_bp)
    return app


@pytest.fixture()
def client(monkeypatch):
    bearer.reset_brake_state()
    valid_doc = {
        "token_type": "access",
        "client_id": "client-1",
        "scope": "athena:read",
        "resource": None,
        "family_id": "fam-1",
        "revoked": False,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    monkeypatch.setattr(store, "get_token", lambda h: dict(valid_doc))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    app = _make_app()
    yield app.test_client()
    bearer.reset_brake_state()


def _rpc(client, body, headers=None, raw=None):
    payload = raw if raw is not None else json.dumps(body)
    return client.post(
        "/mcp",
        data=payload,
        content_type="application/json",
        headers={**AUTH, **(headers or {})},
    )


# ── Transport rules ─────────────────────────────────────────────────────

def test_parse_error_returns_minus_32700(client):
    resp = _rpc(client, None, raw="{not json")
    assert resp.status_code == 200
    assert resp.get_json()["error"]["code"] == -32700


def test_batch_array_rejected(client):
    resp = _rpc(client, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert resp.get_json()["error"]["code"] == -32600


def test_invalid_envelope_rejected(client):
    resp = _rpc(client, {"id": 1, "method": "ping"})  # missing jsonrpc
    assert resp.get_json()["error"]["code"] == -32600


def test_unknown_method_returns_minus_32601(client):
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 7, "method": "resources/list"})
    body = resp.get_json()
    assert body["error"]["code"] == -32601
    assert body["id"] == 7


def test_notification_returns_202_empty(client):
    resp = _rpc(
        client, {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp.status_code == 202
    assert resp.data == b""


def test_ping(client):
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.get_json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_get_and_delete_are_405(client):
    assert client.get("/mcp", headers=AUTH).status_code == 405
    assert client.delete("/mcp", headers=AUTH).status_code == 405


def test_kill_switch_404(monkeypatch):
    bearer.reset_brake_state()
    app = _make_app(MCP_ENABLED=False)
    c = app.test_client()
    resp = c.post("/mcp", data="{}", content_type="application/json", headers=AUTH)
    assert resp.status_code == 404
    # The kill switch covers GET/DELETE too (explicit route, not Flask 405).
    assert c.get("/mcp", headers=AUTH).status_code == 404
    assert c.delete("/mcp", headers=AUTH).status_code == 404
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404


# ── Protocol-version header ─────────────────────────────────────────────

def test_unsupported_protocol_version_header_400(client):
    resp = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"MCP-Protocol-Version": "2020-01-01"},
    )
    assert resp.status_code == 400


def test_supported_protocol_version_headers_accepted(client):
    for version in ("2025-06-18", "2025-03-26"):
        resp = _rpc(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": version},
        )
        assert resp.status_code == 200


# ── initialize ──────────────────────────────────────────────────────────

def _initialize(client, protocol_version):
    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    if protocol_version is not None:
        params["protocolVersion"] = protocol_version
    resp = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
    )
    return resp.get_json()["result"]


def test_initialize_echoes_supported_version(client):
    assert _initialize(client, "2025-03-26")["protocolVersion"] == "2025-03-26"


def test_initialize_unsupported_version_returns_newest(client):
    assert _initialize(client, "1999-01-01")["protocolVersion"] == "2025-06-18"


def test_initialize_missing_version_returns_newest(client):
    assert _initialize(client, None)["protocolVersion"] == "2025-06-18"


def test_initialize_shape(client):
    result = _initialize(client, "2025-06-18")
    assert result["serverInfo"]["name"] == "pallas-athena"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert "read-only" in result["instructions"]


# ── tools/list & tools/call ─────────────────────────────────────────────

def test_tools_list_has_14_read_only_tools(client):
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_list = resp.get_json()["result"]["tools"]
    assert len(tools_list) == 14
    for tool in tools_list:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["openWorldHint"] is False
        assert tool["inputSchema"]["additionalProperties"] is False
    assert "nextCursor" not in resp.get_json()["result"]


def test_tools_call_unknown_tool(client):
    resp = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "drop_tables", "arguments": {}},
        },
    )
    assert resp.get_json()["error"]["code"] == -32602


def test_tools_call_invalid_arguments(client):
    resp = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_agenda", "arguments": {"days_ahead": 900}},
        },
    )
    body = resp.get_json()
    assert body["error"]["code"] == -32602
    assert "days_ahead" in body["error"]["message"]


def test_tools_call_unknown_argument_rejected(client):
    resp = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_agenda", "arguments": {"bogus": 1}},
        },
    )
    assert resp.get_json()["error"]["code"] == -32602


def _call_parse_tool(client, headers=None):
    return _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "parse_court_file_number",
                "arguments": {"court_file_number": "500-05-123456-241"},
            },
        },
        headers=headers,
    )


def test_tools_call_success_envelope(client):
    result = _call_parse_tool(client).get_json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["tribunal"] == "Cour supérieure"
    assert payload["palais_de_justice"] == "Montréal"
    # Default protocol (2025-03-26): no structuredContent.
    assert "structuredContent" not in result


def test_tools_call_structured_content_on_2025_06_18(client):
    result = _call_parse_tool(
        client, headers={"MCP-Protocol-Version": "2025-06-18"}
    ).get_json()["result"]
    assert result["structuredContent"]["tribunal"] == "Cour supérieure"


def test_tool_exception_is_error_result_not_protocol_error(client, monkeypatch):
    def boom(args):
        raise RuntimeError("firestore exploded")

    monkeypatch.setattr(handlers, "parse_court_file_number", boom)
    body = _call_parse_tool(client).get_json()
    assert "error" not in body
    assert body["result"]["isError"] is True


def test_tool_argument_error_maps_to_minus_32602(client):
    resp = _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "compute_judicial_deadline",
                "arguments": {
                    "start_date": "not-a-date",
                    "delay_days": 10,
                    "direction": "after",
                },
            },
        },
    )
    body = resp.get_json()
    assert body["error"]["code"] == -32602
    assert "start_date" in body["error"]["message"]
