"""Tests for the MCP JSON-RPC endpoint (transport rules + dispatch)."""

import ast
import json
import logging
import os
import pathlib
import re
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
    import mcp as mcp_pkg
    import mcp.bearer as bearer
    import mcp.endpoint as endpoint
    import mcp.handlers as handlers
    import mcp.store as store
    import mcp.tools as tools
    import mcp.write_support as write_support

from tests._fake_firestore import install as install_fake  # noqa: E402
from tests import _dummy_accounting  # noqa: E402

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
    instructions = result["instructions"]
    # A bare `"read-only" in instructions` would keep passing against
    # "20 tools read; 9 write…" while proving nothing. Assert the write
    # disclosure the client model actually needs: the families, the
    # fill-only rule, the WP15 retry protocol, and — since lot Q — the
    # boundaries that replaced the create-only ceiling.
    assert "CORRECT" in instructions
    assert "create_note" in instructions
    assert "complete_dossier" in instructions
    assert "athena:write" in instructions
    assert "refuses to overwrite" in instructions
    assert "idempotency_key" in instructions
    # `dry_run` was removed on 2026-08-27 and every write schema carries
    # additionalProperties: False, so a lingering mention here would be
    # worse than useless: it would instruct the model to send an argument
    # that is now REFUSED, and the write it was asked for would never
    # happen. The retry advice that replaced it must be present instead.
    assert "dry_run" not in instructions
    assert "re-read" in instructions
    # What the connector still cannot do. « CREATE-ONLY » and « never
    # writable » died with lot Q — asserting them would now pin a lie.
    assert "NEVER allocates" in instructions
    assert "brouillon" in instructions
    assert "never records a payment" in instructions
    assert "DELETED" in instructions
    assert "trust accounting" in instructions
    # The repair path must be stated: voiding the invoice in the application
    # releases every source. Saying nothing would leave the model believing
    # an import is irreversible.
    assert "void the invoice IN THE APPLICATION" in instructions


# ── tools/list & tools/call ─────────────────────────────────────────────

def test_tools_list_hides_write_tools_from_a_read_only_token(client):
    """Advertising a write tool to a read-only connection makes the model
    call it and take a 403 every time — and _forbidden does NOT feed the
    failure brake, so that is an unthrottled refusal loop."""
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_list = resp.get_json()["result"]["tools"]
    assert len(tools_list) == len(tools.TOOLS) - len(tools.WRITE_TOOLS)
    names = {t["name"] for t in tools_list}
    assert not (names & {"create_note", "append_to_note"})
    for tool in tools_list:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["openWorldHint"] is False
        assert tool["inputSchema"]["additionalProperties"] is False
    assert "nextCursor" not in resp.get_json()["result"]


# ── Write-scope enforcement ─────────────────────────────────────────────

@pytest.fixture()
def write_client(monkeypatch):
    """A client whose token carries athena:read + athena:write."""
    bearer.reset_brake_state()
    doc = {
        "token_type": "access",
        "client_id": "client-1",
        "scope": "athena:read athena:write",
        "resource": None,
        "family_id": "fam-1",
        "revoked": False,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    monkeypatch.setattr(store, "get_token", lambda h: dict(doc))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    app = _make_app()
    yield app.test_client()
    bearer.reset_brake_state()


def _call_write(cl, rid=1):
    return _rpc(cl, {
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {
            "name": "create_note",
            "arguments": {"dossier_id": "d1", "title": "T", "content": "C"},
        },
    })


def test_tools_list_advertises_write_tools_to_a_write_token(write_client):
    tools_list = _rpc(
        write_client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).get_json()["result"]["tools"]
    # Every tool but the accounting ones: those need athena:comptabilite,
    # which a write grant never implies (empty set until plan lot 5 — the
    # dummy-tool tests below prove the subtraction is not vacuous).
    assert len(tools_list) == len(tools.TOOLS) - len(tools.ACCOUNTING_TOOLS)
    by_name = {t["name"]: t for t in tools_list}
    for name in ("create_note", "append_to_note"):
        assert by_name[name]["annotations"]["readOnlyHint"] is False
        assert by_name[name]["annotations"]["destructiveHint"] is False


def test_write_refusal_is_403_with_a_challenge_not_a_200_internal_error(client):
    """endpoint's blanket `except Exception` would turn the refusal into a
    200 isError result, losing the status, the WWW-Authenticate step-up
    signal, and the ability to tell a refusal from a Firestore outage."""
    resp = _call_write(client)
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "insufficient_scope"
    challenge = resp.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="athena:write"' in challenge


def test_write_still_refused_on_the_second_call_when_the_cache_is_warm(client):
    """THE regression test for the bearer success cache. The first call
    populates it; the hit path returns before the Firestore read, so a scope
    gate that only reads the cold path would authorize every later write."""
    assert _call_write(client, rid=1).status_code == 403
    # Second call goes through _check_success_cache, not Firestore.
    second = _call_write(client, rid=2)
    assert second.status_code == 403
    assert second.get_json()["error"] == "insufficient_scope"


def test_write_tool_is_dispatched_when_the_scope_is_granted(
    write_client, monkeypatch
):
    seen = {}

    def _stub(args):
        seen.update(args)
        return {"created": True, "note": {"id": "n1", "dossier_id": "d1",
                                          "content_length": 1}, "dav_synced": True}

    monkeypatch.setattr(handlers, "create_note", _stub)
    body = _call_write(write_client).get_json()
    assert seen["dossier_id"] == "d1"
    assert body["result"]["isError"] is False


def test_revoked_token_cannot_write_even_while_the_cache_is_warm(
    write_client, monkeypatch
):
    """Break-glass revocation must stop a mutation immediately, not after the
    5-minute success-cache window."""
    monkeypatch.setattr(
        handlers, "create_note",
        lambda args: {"created": True, "note": {"id": "n1"}, "dav_synced": True},
    )
    assert _call_write(write_client, rid=1).get_json()["result"]["isError"] is False

    revoked = {
        "token_type": "access", "client_id": "client-1",
        "scope": "athena:read athena:write", "resource": None,
        "family_id": "fam-1", "revoked": True,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    monkeypatch.setattr(store, "get_token", lambda h: dict(revoked))
    second = _call_write(write_client, rid=2)
    assert second.status_code == 403
    assert second.get_json()["error"] == "insufficient_scope"


def test_write_kill_switch_refuses_the_call(monkeypatch):
    bearer.reset_brake_state()
    doc = {
        "token_type": "access", "client_id": "client-1",
        "scope": "athena:read athena:write", "resource": None,
        "family_id": "fam-1", "revoked": False,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    monkeypatch.setattr(store, "get_token", lambda h: dict(doc))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    cl = _make_app(MCP_WRITE_ENABLED=False).test_client()
    listed = _rpc(cl, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    # Le coupe-circuit retire TOUTE la famille d'ecriture, et rien
    # d'autre : la surface de lecture reste entiere.
    assert (len(listed.get_json()["result"]["tools"])
            == len(tools.TOOLS) - len(tools.WRITE_TOOLS))
    body = _call_write(cl, rid=2).get_json()
    assert body["error"]["code"] == -32602
    assert "MCP_WRITE_ENABLED" in body["error"]["message"]
    bearer.reset_brake_state()


# ── The accounting scope (athena:comptabilite) — dormant until lot 5 ─────
#
# No real tool carries the scope yet, so every gate below is exercised on
# the DUMMY accounting tool of tests/_dummy_accounting.py, registered the
# way a lot-5 tool will be (declared scope, WRITE_TOOLS, ACCOUNTING_TOOLS,
# a handler resolved by name). The gates themselves are the real ones.

_ALL_SCOPES = "athena:read athena:write athena:comptabilite"


def _token_doc(scope: str, **over) -> dict:
    doc = {
        "token_type": "access", "client_id": "client-1",
        "scope": scope, "resource": None,
        "family_id": "fam-1", "revoked": False,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    doc.update(over)
    return doc


@pytest.fixture()
def accounting(monkeypatch):
    """The dummy accounting tool, and the list of calls its handler saw."""
    bearer.reset_brake_state()
    calls: list = []

    def recorder(args):
        calls.append(dict(args))
        return {"recorded": True}

    name = _dummy_accounting.register(monkeypatch, handler=recorder)
    yield name, calls
    bearer.reset_brake_state()


def _client_for(monkeypatch, scope: str, **config):
    monkeypatch.setattr(store, "get_token", lambda h: _token_doc(scope))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    return _make_app(**config).test_client()


def _listed(cl) -> set:
    body = _rpc(cl, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).get_json()
    return {t["name"] for t in body["result"]["tools"]}


def test_a_write_token_neither_sees_nor_reaches_an_accounting_tool(
    accounting, monkeypatch, caplog
):
    """By construction: athena:write never stands in for athena:comptabilite.
    Hidden from tools/list, and a direct call is a 403 whose step-up
    challenge names the scope it lacks — never the write scope it holds."""
    name, calls = accounting
    cl = _client_for(
        monkeypatch, "athena:read athena:write", MCP_COMPTABILITE_ENABLED=True)
    listed = _listed(cl)
    assert name not in listed
    assert listed == set(tools.TOOLS) - tools.ACCOUNTING_TOOLS

    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        resp = _call(cl, name, _dummy_accounting.call_args())
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "insufficient_scope"
    assert 'scope="athena:comptabilite"' in resp.headers["WWW-Authenticate"]
    assert calls == []
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "insufficient_scope"
    assert refused["tool"] == name


def test_the_accounting_switch_hides_and_refuses_even_with_every_scope(
    accounting, monkeypatch, caplog
):
    """MCP_COMPTABILITE_ENABLED=false (its default — pinned in
    test_mcp_tools): gone from tools/list, refused at tools/call BEFORE the
    handler, and the refusal names ITS switch — not the write one, which is
    on. Nothing else moves."""
    name, calls = accounting
    cl = _client_for(monkeypatch, _ALL_SCOPES, MCP_COMPTABILITE_ENABLED=False)
    listed = _listed(cl)
    assert name not in listed
    assert listed == set(tools.TOOLS) - {name}

    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(cl, name, _dummy_accounting.call_args()).get_json()
    assert body["error"]["code"] == -32602
    assert "MCP_COMPTABILITE_ENABLED" in body["error"]["message"]
    assert "MCP_WRITE_ENABLED" not in body["error"]["message"]
    assert calls == []
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "comptabilite_disabled"
    assert refused["tool"] == name


def test_the_write_switch_stops_an_accounting_tool_too(
    accounting, monkeypatch, caplog
):
    """The master switch: writes off → the accounting tool is off even with
    its own switch on, and the refusal names MCP_WRITE_ENABLED."""
    name, calls = accounting
    cl = _client_for(
        monkeypatch, _ALL_SCOPES,
        MCP_WRITE_ENABLED=False, MCP_COMPTABILITE_ENABLED=True)
    assert _listed(cl) == set(tools.TOOLS) - tools.WRITE_TOOLS
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(cl, name, _dummy_accounting.call_args()).get_json()
    assert body["error"]["code"] == -32602
    assert "MCP_WRITE_ENABLED" in body["error"]["message"]
    assert calls == []
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "write_disabled"


def test_an_accounting_grant_reaches_its_tool_and_nothing_else(
    accounting, monkeypatch
):
    """read + comptabilite, both switches on: the accounting tool is listed
    and dispatched, and NOT ONE general write tool is visible — the two
    grants are independent in both directions."""
    name, calls = accounting
    cl = _client_for(
        monkeypatch, "athena:read athena:comptabilite",
        MCP_COMPTABILITE_ENABLED=True)
    listed = _listed(cl)
    assert listed == (set(tools.TOOLS) - tools.WRITE_TOOLS) | {name}
    body = _call(cl, name, _dummy_accounting.call_args()).get_json()
    assert body["result"]["isError"] is False
    assert calls == [_dummy_accounting.call_args()]
    # And a general write stays out of reach of this grant.
    resp = _call_write(cl, rid=3)
    assert resp.status_code == 403
    assert 'scope="athena:write"' in resp.headers["WWW-Authenticate"]


def test_revalidation_demands_the_accounting_scope_not_the_write_one(
    accounting, monkeypatch, caplog
):
    """The write-time revalidation re-reads the LIVE token and demands the
    tool's OWN scope. Warm the success cache with an accounting grant, then
    narrow the stored token to read + write: the cached scope would still
    let the call through the gate, so only the revalidation can stop it —
    and it must, because athena:write does not stand in for comptabilite."""
    name, calls = accounting
    cl = _client_for(
        monkeypatch, "athena:read athena:comptabilite",
        MCP_COMPTABILITE_ENABLED=True)
    first = _call(cl, name, _dummy_accounting.call_args(), rid=1).get_json()
    assert first["result"]["isError"] is False
    assert len(calls) == 1

    monkeypatch.setattr(
        store, "get_token", lambda h: _token_doc("athena:read athena:write"))
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        resp = _call(cl, name, _dummy_accounting.call_args(), rid=2)
    assert resp.status_code == 403
    assert 'scope="athena:comptabilite"' in resp.headers["WWW-Authenticate"]
    assert len(calls) == 1                       # the second never ran
    (auth,) = _events(caplog, "mcp_auth_failure")
    assert auth["reason"] == "write_revalidation_failed"
    assert auth["tool"] == name


def test_revalidate_for_write_checks_the_scope_it_is_given(monkeypatch):
    """Unit view of the same rule: the function is handed the tool's scope
    and checks THAT one against the live document — a token carrying every
    other scope is refused."""
    from flask import g

    app = _make_app()
    monkeypatch.setattr(
        store, "get_token", lambda h: _token_doc("athena:read athena:write"))
    with app.test_request_context("/mcp"):
        g.mcp_token_hash = "h" * 64
        with pytest.raises(bearer.ScopeRequired) as exc:
            bearer.revalidate_for_write("athena:comptabilite", "zz_test")
        assert exc.value.scope == "athena:comptabilite"
        assert exc.value.tool == "zz_test"
        bearer.revalidate_for_write("athena:write", "create_note")  # passes

    monkeypatch.setattr(
        store, "get_token", lambda h: _token_doc(_ALL_SCOPES))
    with app.test_request_context("/mcp"):
        g.mcp_token_hash = "h" * 64
        bearer.revalidate_for_write("athena:comptabilite", "zz_test")  # passes


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


# ── Refusal logging, replays and revalidation (lot 0a, step 2) ─────────
#
# OBSERVABILITY.md has promised since lot Q that « a burst of
# mcp_write_refused during an import » is the stop-the-batch signal. Until
# 2026-09-25 only the kill switch and the scope gate ever emitted one: a
# schema refusal and a handler refusal left no trace at all. These tests pin
# the lines that now exist AND what they must never carry — the refusal's
# text, which quotes argument names the caller chose or describes privileged
# content the RedactionFilter does not scrub.


def _events(caplog, event):
    return [
        r.json_fields for r in caplog.records
        if r.name == "pallas.mcp"
        and getattr(r, "json_fields", {}).get("event") == event
    ]


def _logged_text(caplog) -> str:
    """Everything the records would ship: message, fields, traceback."""
    return "\n".join(
        f"{r.getMessage()} {getattr(r, 'json_fields', '')} {r.exc_text or ''}"
        for r in caplog.records
    )


def _call(cl, name, arguments, rid=1):
    return _rpc(cl, {
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def _forbid_handler(monkeypatch, name="create_note"):
    def forbidden(args):
        pytest.fail("a schema refusal must not reach the handler")

    monkeypatch.setattr(handlers, name, forbidden)


def test_a_schema_refused_write_is_logged_by_count_never_by_text(
    write_client, monkeypatch, caplog
):
    _forbid_handler(monkeypatch)
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(write_client, "create_note", {
            "dossier_id": "d1", "title": "T",           # `content` missing
            "champ_SECRET_inconnu": "valeur SECRÈTE",   # not a property
        }).get_json()

    assert body["error"]["code"] == -32602
    # The client is told exactly what is wrong — that is its only channel.
    assert "champ_SECRET_inconnu" in body["error"]["message"]
    assert _events(caplog, "mcp_write_refused") == [{
        "event": "mcp_write_refused", "outcome": "refused",
        "tool": "create_note", "reason": "schema_invalid", "error_count": 2,
    }]
    assert not _events(caplog, "mcp_write")
    assert not _events(caplog, "mcp_tool_call")
    assert "SECRET" not in _logged_text(caplog)


def test_non_object_arguments_to_a_write_are_a_schema_refusal(
    write_client, monkeypatch, caplog
):
    _forbid_handler(monkeypatch)
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(write_client, "create_note", ["SECRET"]).get_json()
    assert body["error"]["message"] == "arguments must be an object"
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "schema_invalid"
    assert refused["error_count"] == 1
    assert "SECRET" not in _logged_text(caplog)


def test_a_read_tool_schema_refusal_logs_no_write_refusal(client, caplog):
    """`mcp_write_refused` is the write audit's counterpart; a bad READ
    argument changes nothing and must not inflate the import signal."""
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(client, "get_agenda", {"days_ahead": 900}).get_json()
    assert body["error"]["code"] == -32602
    assert not _events(caplog, "mcp_write_refused")


def test_a_handler_refused_write_is_logged_by_reason_never_by_text(
    write_client, monkeypatch, caplog
):
    def refuse(args):
        raise tools.ToolArgumentError(
            "La note « Stratégie SECRÈTE contre Tremblay » est introuvable."
        )

    monkeypatch.setattr(handlers, "create_note", refuse)
    dossier_id = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f"
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(write_client, "create_note", {
            "dossier_id": dossier_id, "title": "T", "content": "C",
        }).get_json()

    assert body["error"]["code"] == -32602
    assert "SECRÈTE" in body["error"]["message"]      # the client, only
    assert _events(caplog, "mcp_write_refused") == [{
        "event": "mcp_write_refused", "outcome": "refused",
        "tool": "create_note", "reason": "argument_refused",
        "dossier_id": dossier_id,
    }]
    assert not _events(caplog, "mcp_write")
    assert "SECRÈTE" not in _logged_text(caplog)
    assert "Tremblay" not in _logged_text(caplog)


@pytest.mark.parametrize("dossier_id", [
    "Tremblay c. Lavoie",                                # a title, pasted
    "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f\n",            # `$` would pass it
    "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f Tremblay",     # an id, then a name
])
def test_a_refused_dossier_id_that_is_not_id_shaped_is_not_logged(
    write_client, monkeypatch, caplog, dossier_id
):
    """The commonest handler refusal is « dossier introuvable », so a
    refused call's `dossier_id` is exactly the one least likely to be an id:
    the schema bounds it to 64 characters, nothing more. A name pasted into
    it must not reach the log, which does not scrub names."""
    def refuse(args):
        raise tools.ToolArgumentError("Dossier introuvable.")

    monkeypatch.setattr(handlers, "create_note", refuse)
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        _call(write_client, "create_note", {
            "dossier_id": dossier_id, "title": "T", "content": "C",
        })
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "argument_refused"
    assert "dossier_id" not in refused
    assert "Tremblay" not in str(refused)


def test_a_read_tool_handler_refusal_logs_no_write_refusal(client, caplog):
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(client, "compute_judicial_deadline", {
            "start_date": "not-a-date", "delay_days": 10,
            "direction": "after",
        }).get_json()
    assert body["error"]["code"] == -32602
    assert not _events(caplog, "mcp_write_refused")


@pytest.fixture()
def run_write_note(monkeypatch):
    """`create_note` replaced by a stub that goes through the REAL
    run_write, over the shared fake Firestore — so replays and key
    conflicts are the protocol's own, not a hand-set payload flag."""
    install_fake(monkeypatch, write_support)
    executed = []

    def create_note(args):
        def execute():
            executed.append(args["title"])
            return {
                "created": True, "ctag_bumped": True, "dav_synced": True,
                "note": {"id": "n1", "dossier_id": args["dossier_id"],
                         "content_length": len(args["content"])},
            }
        return write_support.run_write("create_note", args, execute)

    monkeypatch.setattr(handlers, "create_note", create_note)
    return executed


def _note_args(title="T", key="cle-de-replay-1"):
    return {"dossier_id": "d1", "title": title, "content": "C",
            "idempotency_key": key}


def test_a_replay_logs_no_ctag_bump(write_client, run_write_note, caplog):
    """The replay hands back the FIRST call's stored payload, `ctag_bumped:
    true` included, while bumping nothing. Logged as-is, every replay
    claimed a bump that never happened."""
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        first = _call(write_client, "create_note", _note_args(), rid=1)
        second = _call(write_client, "create_note", _note_args(), rid=2)

    assert run_write_note == ["T"]                      # one real write
    writes = _events(caplog, "mcp_write")
    assert [(w["idempotent_replay"], w["ctag_bumped"]) for w in writes] == [
        (False, True), (True, False),
    ]
    notes = _events(caplog, "mcp_note_written")
    assert [n["ctag_bumped"] for n in notes] == [True, False]
    # The TOOL RESULT is the stored contract and replays verbatim; only the
    # audit line had to stop lying.
    assert first.get_json()["result"]["isError"] is False
    replayed = json.loads(second.get_json()["result"]["content"][0]["text"])
    assert replayed["idempotent_replay"] is True
    assert replayed["ctag_bumped"] is True


def test_a_key_conflict_is_logged_under_its_own_reason(
    write_client, run_write_note, caplog
):
    _call(write_client, "create_note", _note_args(title="A"), rid=1)
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(
            write_client, "create_note", _note_args(title="B SECRET"), rid=2
        ).get_json()

    assert body["error"]["code"] == -32602
    assert "idempotency_key" in body["error"]["message"]
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "idempotency_conflict"
    assert refused["tool"] == "create_note"
    assert run_write_note == ["A"]                      # never executed
    assert "SECRET" not in _logged_text(caplog)


# ── A write that COMMITTED, then failed (lot 0a, step 4) ───────────────

_NOTE_ID = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f"
_DOSSIER_ID = "9d1c2b3a-4e5f-4a6b-8c7d-0e1f2a3b4c5d"


@pytest.fixture()
def partial_note(monkeypatch):
    """`create_note` replaced by a stub that goes through the REAL run_write
    over the shared fake: its execute NOTES a commit exactly as a model
    does, then fails — the CTag bump, a builder, anything after the write."""
    install_fake(monkeypatch, write_support)
    executed = []

    def create_note(args):
        def execute():
            executed.append(args["title"])
            from models import provenance
            provenance.note_commit("notes", _NOTE_ID)
            raise RuntimeError("bump exploded — titre SECRET")
        return write_support.run_write("create_note", args, execute)

    monkeypatch.setattr(handlers, "create_note", create_note)
    return executed


def _partial_args(key="cle-partielle-1"):
    return {"dossier_id": _DOSSIER_ID, "title": "T", "content": "C",
            "idempotency_key": key}


def test_a_committed_then_failed_write_says_so_and_is_never_retryable(
    write_client, partial_note, caplog
):
    """It used to reach the blanket `except` and come back « Tool execution
    failed due to an internal error » — retryable, so the caller retried and
    wrote twice. It is now an isError RESULT (not a -32602, which promises
    nothing was written) saying ENREGISTRÉE — NE PAS RÉESSAYER."""
    with caplog.at_level(logging.INFO):
        body = _call(write_client, "create_note", _partial_args()).get_json()

    assert "error" not in body
    result = body["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "ENREGISTRÉE" in text and "NE PAS RÉESSAYER" in text
    assert "internal error" not in text.lower()
    assert _NOTE_ID in text

    (partial,) = _events(caplog, "mcp_write_partial")
    assert partial == {
        "event": "mcp_write_partial", "outcome": "failure",
        "tool": "create_note", "entity_id": _NOTE_ID,
        "dossier_id": _DOSSIER_ID, "collection": "notes", "rows": 1,
        "idempotent_replay": False,
    }
    (call,) = _events(caplog, "mcp_tool_call")
    assert call["outcome"] == "failure"
    assert not _events(caplog, "mcp_write")        # it did not « succeed »
    assert not _events(caplog, "mcp_write_refused")  # nor was it refused
    # The ERROR with the traceback comes from run_write, not the endpoint's
    # generic « mcp tool execution failed » line.
    unexpected = [r for r in caplog.records if r.name == "pallas.unexpected"]
    assert [r.getMessage() for r in unexpected] == [
        "mcp write failed after its commit point"]
    # The span/audit lines carry ids; the caught exception's text rides only
    # on the traceback of the ERROR line, as for any unexpected failure.
    assert "SECRET" not in "\n".join(
        f"{r.getMessage()} {getattr(r, 'json_fields', '')}"
        for r in caplog.records if r.name == "pallas.mcp")


def test_a_same_key_retry_of_a_committed_write_repeats_the_warning(
    write_client, partial_note, caplog
):
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        _call(write_client, "create_note", _partial_args(), rid=1)
        body = _call(write_client, "create_note", _partial_args(), rid=2).get_json()

    assert partial_note == ["T"]                   # executed ONCE
    assert body["result"]["isError"] is True
    assert "NE PAS RÉESSAYER" in body["result"]["content"][0]["text"]
    partials = _events(caplog, "mcp_write_partial")
    assert [p["idempotent_replay"] for p in partials] == [False, True]
    assert partials[1]["entity_id"] == _NOTE_ID


def test_an_in_flight_key_is_refused_under_its_own_reason(
    write_client, run_write_note, caplog, monkeypatch
):
    """A pending claim young enough to still be running — the second call
    of a client that retried too early — is refused with « same key »."""
    args = _note_args()
    fingerprint = write_support.args_fingerprint(args)
    now = datetime.now(UTC)
    fake = write_support.db
    fake.seed(
        f"{write_support.COLLECTION}/"
        f"{write_support._doc_id('create_note', args['idempotency_key'])}",
        {"tool": "create_note", "args_fingerprint": fingerprint,
         "status": "pending", "claim_id": "c1", "claimed_at": now,
         "created_at": now, "expire_at": now + write_support.IDEMPOTENCY_TTL},
    )
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(write_client, "create_note", args).get_json()

    assert body["error"]["code"] == -32602
    assert "MÊME clé" in body["error"]["message"]
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "idempotency_in_flight"
    assert run_write_note == []                    # never executed


def _accept_note(monkeypatch):
    monkeypatch.setattr(
        handlers, "create_note",
        lambda args: {"created": True, "note": {"id": "n1"}, "dav_synced": True},
    )


def test_a_revalidation_refusal_names_the_tool(write_client, monkeypatch, caplog):
    """Without the tool name, a revocation that stopped an import mid-batch
    left a refusal saying nothing about WHICH write it stopped."""
    _accept_note(monkeypatch)
    assert _call_write(write_client, rid=1).get_json()["result"]["isError"] is False

    revoked = {
        "token_type": "access", "client_id": "client-1",
        "scope": "athena:read athena:write", "resource": None,
        "family_id": "fam-1", "revoked": True,
        "expire_at": datetime.now(UTC) + timedelta(hours=1),
    }
    monkeypatch.setattr(store, "get_token", lambda h: dict(revoked))
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        assert _call_write(write_client, rid=2).status_code == 403

    (auth,) = _events(caplog, "mcp_auth_failure")
    assert auth["reason"] == "write_revalidation_failed"
    assert auth["tool"] == "create_note"
    assert auth["client_id"] == "client-1"
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "insufficient_scope"
    assert refused["tool"] == "create_note"


def test_an_unreachable_revalidation_is_refused_and_names_the_tool(
    write_client, monkeypatch, caplog
):
    """Fail CLOSED on a store outage — and the unexpected-error line says
    which write it refused, not merely that a lookup failed somewhere."""
    _accept_note(monkeypatch)
    assert _call_write(write_client, rid=1).get_json()["result"]["isError"] is False

    def outage(_hash):
        raise RuntimeError("firestore down")

    def must_not_write(args):
        pytest.fail("a refused revalidation must not write")

    monkeypatch.setattr(store, "get_token", outage)   # the cache stays warm
    monkeypatch.setattr(handlers, "create_note", must_not_write)
    with caplog.at_level(logging.INFO):
        assert _call_write(write_client, rid=2).status_code == 403

    unexpected = [
        r.json_fields for r in caplog.records if r.name == "pallas.unexpected"
    ]
    assert [u.get("tool") for u in unexpected] == ["create_note"]
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["tool"] == "create_note"


# ── Every refusal reason is a stable code, and documented ──────────────

_MCP_DIR = pathlib.Path(ATHENA_DIR) / "mcp"
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


def _refusal_reasons_in_source() -> dict:
    """Every reason a write refusal can be LOGGED under, from the AST.

    Three sources: a `reason=` given to ToolArgumentError, a literal
    `reason=` on `log_mcp_event("mcp_write_refused", …)`, and the literal
    second argument of the endpoint's `_log_write_refused`. The default
    reason joins them from the class itself.
    """
    raised, logged, non_literal = set(), set(), []
    for path in sorted(_MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                fname = func.id
            elif isinstance(func, ast.Attribute):
                fname = func.attr
            else:
                continue
            reason_kw = next(
                (k.value for k in node.keywords if k.arg == "reason"), None
            )
            if fname == "ToolArgumentError" and reason_kw is not None:
                if isinstance(reason_kw, ast.Constant):
                    raised.add(reason_kw.value)
                else:
                    non_literal.append(f"{path.name}:{node.lineno}")
            elif (
                fname == "log_mcp_event" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "mcp_write_refused"
                and isinstance(reason_kw, ast.Constant)
            ):
                logged.add(reason_kw.value)
            elif (
                fname == "_log_write_refused" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
            ):
                logged.add(node.args[1].value)
    return {"raised": raised, "logged": logged, "non_literal": non_literal}


def test_every_refusal_reason_is_a_stable_code_and_documented():
    found = _refusal_reasons_in_source()
    # A reason handed to ToolArgumentError must be a LITERAL code: a computed
    # one is how a message fragment ends up in a log field.
    assert not found["non_literal"], found["non_literal"]
    reasons = found["raised"] | found["logged"] | {tools.DEFAULT_REFUSAL_REASON}
    # Non-vacuous: the sweep must see every path this commit wires.
    assert reasons >= {
        "insufficient_scope", "write_disabled", "comptabilite_disabled",
        "schema_invalid", "argument_refused", "idempotency_conflict",
    }, reasons
    bad = sorted(r for r in reasons if not _REASON_CODE.match(str(r)))
    assert not bad, bad

    doc = (pathlib.Path(ATHENA_DIR) / "OBSERVABILITY.md").read_text(
        encoding="utf-8"
    )
    (row,) = [
        line for line in doc.splitlines()
        if line.startswith("| `mcp_write_refused` |")
    ]
    undocumented = sorted(r for r in reasons if f"`{r}`" not in row)
    assert not undocumented, (
        f"mcp_write_refused reasons missing from OBSERVABILITY.md: {undocumented}"
    )


def test_the_refusal_helper_logs_write_tools_only():
    """Pinned directly: the gate is WRITE_TOOLS membership, not the caller's
    good intentions."""
    seen = []
    with mock.patch.object(
        endpoint, "log_mcp_event", lambda *a, **k: seen.append((a, k))
    ):
        endpoint._log_write_refused("get_agenda", "schema_invalid", error_count=1)
        assert seen == []
        endpoint._log_write_refused("create_note", "schema_invalid", error_count=3)
    assert seen == [(
        ("mcp_write_refused", "refused"),
        {"tool": "create_note", "reason": "schema_invalid", "error_count": 3},
    )]
