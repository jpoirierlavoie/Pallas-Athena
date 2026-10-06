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
    import mcp.disclosure as disclosure
    import mcp.endpoint as endpoint
    import mcp.handlers as handlers
    import mcp.store as store
    import mcp.tools as tools
    import mcp.write_support as write_support

from tests._fake_firestore import install as install_fake  # noqa: E402

UTC = timezone.utc
ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUTH = {"Authorization": "Bearer test-token"}


def _make_app(**config) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(ATHENA_DIR, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
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
    # The explicit route, not Flask's automatic 405 (an HTML body, and an
    # Allow header naming OPTIONS too): no SSE stream, no session to delete.
    for resp in (client.get("/mcp", headers=AUTH),
                 client.delete("/mcp", headers=AUTH)):
        assert resp.status_code == 405
        assert resp.headers["Allow"] == "POST"
        assert resp.get_json() == {"error": "method_not_allowed"}


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
    # REWRITTEN deliberately (finitions, contracts-1 part 2): INSTRUCTIONS
    # open on a SAFETY CORE whose « NEVER, whatever the tool: » list states
    # the promises as clauses, then ONE index line per family — the family
    # prose these assertions pinned moved into the tool descriptions, where
    # they are pinned now (tools.TOOLS below).
    import mcp.tools as _tools
    desc = {name: spec["description"] for name, spec in _tools.TOOLS.items()}
    assert instructions.startswith(disclosure.safety_core_en())
    assert len(instructions.encode("utf-8")) <= 8_000
    assert "NEVER allocates" in instructions
    assert "brouillon" in instructions
    assert "record a payment outside the accounting registers" in instructions
    assert "DELETE anything" in instructions
    # What the accounting tools never do, told to EVERY token since
    # 2026-10-05 — until then only to a token holding the separate
    # athena:comptabilite grant, and the « trust » promise (« without the
    # separate grant it never writes to trust accounting ») left with it.
    assert "It never deletes a register entry" in instructions
    assert "It never transfers trust funds between dossiers" in instructions
    assert "It never withdraws trust funds in cash" in instructions
    assert "It never shows a bank transit or account number" in instructions
    assert "trust accounting" not in instructions
    # The repair path must be stated: voiding the invoice releases every
    # source. Saying nothing would leave the model believing an import is
    # irreversible. Rewritten deliberately (lot 3b): the connector voids too
    # (update_invoice), and the promises that replaced « never changes an
    # invoice's status » / « never allocates an invoice number » are stated.
    assert "voided (update_invoice, status annulée" in desc["import_invoice"]
    assert "SEND an invoice to anyone (marking one envoyée sends nothing)" in (
        instructions)
    assert "mark an invoice payée by a status change" in instructions
    assert "never changes an invoice's status" not in instructions
    assert "It never allocates an invoice number" not in instructions
    # Lot 3b (the text step): the BILL paragraph states what the consent
    # screen states — only a brouillon is corrected (an issued invoice is
    # voided and reissued), the sources an invoice bills freeze until a
    # void, and each budget version is kept as the proof of what the client
    # was told.
    assert "ONLY a brouillon" in instructions
    assert "frozen, their phase aside, until update_invoice voids it" in (
        desc["create_invoice"])
    assert "the proof of what the client was told, and when" in (
        desc["create_budget_version"])
    # Lot 0a (disclosure step): the text is ASSEMBLED from mcp/disclosure,
    # and three things it used to say were false. Voiding does NOT free the
    # number; complete_task does not reopen; the family count is derived.
    # Lot 1b: reopening became its own tool, so « never reopens a closed
    # task » became false as a CONNECTOR promise — what stays true is that
    # complete_task never does it, and that no tool undoes a cancellation
    # without being told to in so many words.
    assert "frees the number" not in instructions
    assert "the number itself stays on the voided invoice" in (
        desc["import_invoice"])
    assert "never reopens a closed one — that is `reopen_task`" in instructions
    assert "never silently undoes a cancellation" in instructions
    assert "five families" not in instructions
    # ONE text for every token since 2026-10-05: every tool counted, every
    # family indexed — ACCOUNTING and its read included —, and no word of
    # the separate accounting grant the lawyer removed (a model told of a
    # scope that no longer exists would send the lawyer looking for it).
    families = [f for f in disclosure.FAMILIES if f.tools]
    reads = len(set(tools.TOOLS) - tools.WRITE_TOOLS)
    assert (f"TOOLS: {reads} tools read; {len(tools.WRITE_TOOLS)} write, "
            f"in {len(families)} families:") in instructions
    assert "ACCOUNTING: `record_trust_entry`" in instructions
    assert "`get_admin_ledger`" in instructions
    assert "athena:comptabilite" not in instructions
    assert "Accounting tools appear only under" not in instructions
    # The lot 0a write-protocol rules the client model must follow.
    assert "`expected_etag`" in instructions
    assert "do NOT retry" in instructions
    assert "`updated_via`" in instructions
    # RECLASSIFY names the bulk tools literally, not as « their _bulk forms ».
    assert "`set_time_entry_phase_bulk`" in instructions
    assert "`set_expense_phase_bulk`" in instructions
    # Lot 4b: the initialize text a client model reads says what the two
    # new families do to the PHONE and to a compliance record — and the two
    # promises the lot falsified are gone from it, while « nothing can be
    # deleted » carries the precision that keeps it true.
    assert "DOSSIERS: " in instructions and "CONTACTS: " in instructions
    assert "fermé / archivé DRAINS its DavX5 collection" in instructions
    # Reworded by the finitions (contracts-8): the repair is the first of
    # three ordered retry cases.
    assert "the SAME status under the SAME idempotency_key" in (
        desc["set_dossier_status"])
    assert "counts as NOT done" in instructions
    assert "removes a LINK — the contact stays" in instructions
    assert "CONFIRM an identity or conflict check" in instructions
    assert "can never be changed here" not in instructions
    assert "identity verification or conflict-of-interest checks" not in (
        instructions)


# ── tools/list & tools/call ─────────────────────────────────────────────

def test_tools_list_hides_write_tools_from_a_read_only_token(client):
    """Advertising a write tool to a read-only connection makes the model
    call it and take a 403 every time — and _forbidden does NOT feed the
    failure brake, so that is an unthrottled refusal loop."""
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_list = resp.get_json()["result"]["tools"]
    # Exactly the reads — the administration ledger's among them since
    # 2026-10-05, an ordinary read like the trust reads — and not one write,
    # the five accounting writes included.
    names = {t["name"] for t in tools_list}
    assert len(tools_list) == len(names)
    assert names == set(tools.TOOLS) - tools.WRITE_TOOLS
    assert "get_admin_ledger" in names
    assert not (names & {"create_note", "append_to_note"})
    assert not (names & tools.ACCOUNTING_WRITE_TOOLS)
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
    # Every tool: since 2026-10-05 the accounting writes are write tools
    # like the others (athena:write), their separate scope gone.
    by_name = {t["name"]: t for t in tools_list}
    assert len(tools_list) == len(by_name) == len(tools.TOOLS)
    assert set(by_name) == set(tools.TOOLS)
    for name in ("create_note", "append_to_note"):
        assert by_name[name]["annotations"]["readOnlyHint"] is False
        assert by_name[name]["annotations"]["destructiveHint"] is False
    for name in tools.ACCOUNTING_WRITE_TOOLS:
        assert by_name[name]["annotations"]["readOnlyHint"] is False
    assert by_name["get_admin_ledger"]["annotations"]["readOnlyHint"] is True


def test_write_refusal_is_403_with_a_challenge_not_a_200_internal_error(
    client, monkeypatch
):
    """endpoint's blanket `except Exception` would turn the refusal into a
    200 isError result, losing the status, the WWW-Authenticate step-up
    signal, and the ability to tell a refusal from a Firestore outage."""
    def must_not_run(args):
        pytest.fail("a scope refusal must not reach the handler")

    monkeypatch.setattr(handlers, "create_note", must_not_run)
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


# ── The accounting tools: write tools like the others ───────────────────
#
# The five ACCOUNTING writes (trust and administration register entries)
# sat behind their own scope, athena:comptabilite, and their own kill switch
# until 2026-10-05, when the lawyer removed both: they are write tools like
# the others now (athena:write), and the administration ledger's read,
# get_admin_ledger, an ordinary read (athena:read). Every gate below runs on
# the REAL tools; a recorder stands in for each handler, so a dispatch is
# observed without a register behind it.

# A token still carrying the retired scope string (none was live at the
# removal) simply holds an unknown scope: it must reach nothing more.
_RETIRED_SCOPE = "athena:comptabilite"

# Arguments each accounting write's input schema accepts — the schema runs
# before the handler, so a dispatched call must clear it. Keyed by tool, and
# checked against ACCOUNTING_WRITE_TOOLS below: a sixth accounting write
# must come here before the checklist passes.
_ACCOUNTING_CALLS = {
    "record_trust_entry": {
        "account_id": "acc-1", "direction": "recette",
        "purpose": "dépôt_client", "amount_cents": 100,
        "date": "2026-10-01", "method": "chèque",
        "idempotency_key": "cle-comptable-fid-1",
    },
    "record_admin_entry": {
        "account_id": "acc-1", "kind": "dépense", "amount_cents": 100,
        "date": "2026-10-01", "method": "chèque",
        "idempotency_key": "cle-comptable-adm-1",
    },
    "update_admin_entry": {
        "tx_id": "tx-1", "expected_etag": "etag-1",
        "idempotency_key": "cle-comptable-maj-1",
    },
    "clear_register_entries": {
        "register": "admin", "tx_ids": ["tx-1"], "cleared_date": "2026-10-01",
        "idempotency_key": "cle-comptable-cmp-1",
    },
    "reverse_register_entry": {
        "register": "admin", "tx_id": "tx-1", "reason": "Erreur de saisie",
        "idempotency_key": "cle-comptable-cp-1",
    },
}


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
def accounting_handlers(monkeypatch):
    """The five accounting writes' handlers replaced by recorders: the
    (tool, arguments) pairs prove a call reached its handler, and their
    absence that a refusal came first."""
    bearer.reset_brake_state()
    calls: list = []
    for name in tools.ACCOUNTING_WRITE_TOOLS:
        def recorder(args, _name=name):
            calls.append((_name, dict(args)))
            return {"recorded": True}

        monkeypatch.setattr(handlers, tools.TOOLS[name]["handler"], recorder)
    yield calls
    bearer.reset_brake_state()


def _client_for(monkeypatch, scope: str):
    monkeypatch.setattr(store, "get_token", lambda h: _token_doc(scope))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    return _make_app().test_client()


def _listed(cl) -> set:
    body = _rpc(cl, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).get_json()
    return {t["name"] for t in body["result"]["tools"]}


@pytest.mark.parametrize("scope", ["athena:read", f"athena:read {_RETIRED_SCOPE}"])
def test_a_read_only_token_neither_sees_nor_reaches_an_accounting_write(
    accounting_handlers, monkeypatch, caplog, scope
):
    """Hidden from tools/list, and a direct call is a 403 whose step-up
    challenge names athena:write — refused before any handler runs, each
    refusal logged under its tool. The retired accounting string grants
    nothing: a token carrying it is a read-only token."""
    cl = _client_for(monkeypatch, scope)
    listed = _listed(cl)
    assert not listed & tools.WRITE_TOOLS
    assert "get_admin_ledger" in listed

    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        for rid, name in enumerate(sorted(_ACCOUNTING_CALLS), 1):
            resp = _call(cl, name, _ACCOUNTING_CALLS[name], rid=rid)
            assert resp.status_code == 403, name
            assert resp.get_json()["error"] == "insufficient_scope"
            assert 'scope="athena:write"' in resp.headers["WWW-Authenticate"]
    assert accounting_handlers == []
    refused = _events(caplog, "mcp_write_refused")
    assert [(r["reason"], r["tool"]) for r in refused] == [
        ("insufficient_scope", name) for name in sorted(_ACCOUNTING_CALLS)]


@pytest.mark.parametrize("name", sorted(_ACCOUNTING_CALLS))
def test_a_write_token_reaches_every_accounting_write(
    accounting_handlers, monkeypatch, caplog, name
):
    """A read + write token lists each accounting write and its call is
    dispatched to the handler, audited as a write — no other grant
    needed."""
    cl = _client_for(monkeypatch, "athena:read athena:write")
    assert name in _listed(cl)
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(cl, name, _ACCOUNTING_CALLS[name]).get_json()
    assert body["result"]["isError"] is False
    assert accounting_handlers == [(name, _ACCOUNTING_CALLS[name])]
    (write,) = _events(caplog, "mcp_write")
    assert write["tool"] == name
    assert not _events(caplog, "mcp_write_refused")


@pytest.mark.parametrize("narrowed", ["athena:read", f"athena:read {_RETIRED_SCOPE}"])
@pytest.mark.parametrize("name", sorted(_ACCOUNTING_CALLS))
def test_revalidation_stops_an_accounting_write_once_the_live_token_lost_write(
    accounting_handlers, monkeypatch, caplog, name, narrowed
):
    """The write-time revalidation re-reads the LIVE token and demands the
    tool's scope — athena:write, for the accounting writes as for every
    write. Warm the success cache with a write grant, then narrow the stored
    token: the cached scope still lets the call through the gate, so only
    the revalidation can stop it — and it must, the retired accounting
    string standing in for nothing."""
    cl = _client_for(monkeypatch, "athena:read athena:write")
    first = _call(cl, name, _ACCOUNTING_CALLS[name], rid=1).get_json()
    assert first["result"]["isError"] is False
    assert len(accounting_handlers) == 1

    monkeypatch.setattr(store, "get_token", lambda h: _token_doc(narrowed))
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        resp = _call(cl, name, _ACCOUNTING_CALLS[name], rid=2)
    assert resp.status_code == 403
    assert 'scope="athena:write"' in resp.headers["WWW-Authenticate"]
    assert len(accounting_handlers) == 1                 # the second never ran
    (auth,) = _events(caplog, "mcp_auth_failure")
    assert auth["reason"] == "write_revalidation_failed"
    assert auth["tool"] == name
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "insufficient_scope"
    assert refused["tool"] == name


def test_revalidate_for_write_checks_the_scope_it_is_given(monkeypatch):
    """Unit view of the same rule: the function is handed the tool's scope
    and checks THAT one against the live document — a token carrying every
    other scope is refused. Every write's scope is athena:write since
    2026-10-05, and the retired accounting string never stands in for it."""
    from flask import g

    app = _make_app()
    monkeypatch.setattr(
        store, "get_token", lambda h: _token_doc("athena:read athena:write"))
    with app.test_request_context("/mcp"):
        g.mcp_token_hash = "h" * 64
        with pytest.raises(bearer.ScopeRequired) as exc:
            bearer.revalidate_for_write("test:other", "zz_test")
        assert exc.value.scope == "test:other"
        assert exc.value.tool == "zz_test"
        bearer.revalidate_for_write("athena:write", "record_trust_entry")  # passes
        bearer.revalidate_for_write("athena:write", "create_note")  # passes

    monkeypatch.setattr(
        store, "get_token", lambda h: _token_doc(f"athena:read {_RETIRED_SCOPE}"))
    with app.test_request_context("/mcp"):
        g.mcp_token_hash = "h" * 64
        with pytest.raises(bearer.ScopeRequired) as exc:
            bearer.revalidate_for_write("athena:write", "record_trust_entry")
        assert exc.value.scope == "athena:write"
        assert exc.value.tool == "record_trust_entry"


@pytest.mark.parametrize("scope", [
    "athena:read", "athena:read athena:write", f"athena:read {_RETIRED_SCOPE}",
])
def test_initialize_tells_every_token_the_same_instructions(monkeypatch, scope):
    """ONE text for every token since 2026-10-05: the per-token accounting
    variant left with the athena:comptabilite scope. Every token is told of
    the ACCOUNTING family and the administration ledger's read — a
    read-only one too, as it always was of every other write family, under
    the sentence that says when write tools appear."""
    bearer.reset_brake_state()
    cl = _client_for(monkeypatch, scope)
    instructions = _initialize(cl, "2025-06-18")["instructions"]
    assert instructions == endpoint.INSTRUCTIONS
    assert "ACCOUNTING: `record_trust_entry`" in instructions
    assert "`get_admin_ledger`" in instructions
    assert ("Write tools appear only when the lawyer granted the "
            "`athena:write` scope.") in instructions
    bearer.reset_brake_state()


# The numbers the lawyer reads off `tools/list` in the deploy train
# (DEPLOYMENT.md §15), LITERAL on purpose: every other assertion of this
# module derives its set from the registry, and a derived check passes
# whatever the registry holds. 88 = 32 reads + 56 writes (86 → 88 on
# 2026-09-30, deliberately: the two bulk creators create_time_entries_bulk /
# create_expenses_bulk). Since 2026-10-05 — the separate accounting scope
# and every MCP kill switch removed — there are two token shapes: a
# read-only token lists the 32 reads, get_admin_ledger among them; a
# read + write token lists all 88, the five ACCOUNTING writes among them. A
# token still carrying the retired accounting string lists no more.
@pytest.mark.parametrize("scope, expected", [
    ("athena:read", 32),
    ("athena:read athena:write", 88),
    (f"athena:read {_RETIRED_SCOPE}", 32),
    (f"athena:read athena:write {_RETIRED_SCOPE}", 88),
])
def test_tools_list_counts_per_token_are_the_train_s_checklist(
    monkeypatch, scope, expected
):
    bearer.reset_brake_state()
    assert len(tools.TOOLS) == 88 and len(tools.WRITE_TOOLS) == 56
    # The five accounting writes, by name — the set the recorder tests above
    # dispatch, every one of them.
    assert tools.ACCOUNTING_WRITE_TOOLS == set(_ACCOUNTING_CALLS) == {
        "record_trust_entry", "record_admin_entry", "update_admin_entry",
        "clear_register_entries", "reverse_register_entry",
    }
    listed = _listed(_client_for(monkeypatch, scope))
    assert len(listed) == expected
    assert "get_admin_ledger" in listed
    writes = "athena:write" in scope.split()
    assert (listed >= tools.ACCOUNTING_WRITE_TOOLS) is writes
    if not writes:
        assert not listed & tools.WRITE_TOOLS
    bearer.reset_brake_state()


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


def test_a_bulk_creator_s_schema_refusal_names_each_row_through_the_endpoint(
    write_client, monkeypatch, caplog
):
    """tools/call runs validate_args BEFORE the handler, so the row index
    the handler adds never reaches the commonest faults. The endpoint's own
    message must therefore locate each one — `entries[12].hours`, never a
    bare `hours` the caller cannot map to one of 50 new, id-less rows."""
    _forbid_handler(monkeypatch, "create_time_entries_bulk")
    rows = [{"dossier_id": "d1", "date": "2026-09-28", "hours": 1.5,
             "description": "Rédaction"} for _ in range(50)]
    rows[12]["hours"] = 30
    del rows[37]["hours"]
    rows[44]["phase"] = "XYZ"
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(write_client, "create_time_entries_bulk", {
            "entries": rows, "idempotency_key": "cle-lot-endpoint",
        }).get_json()
    assert body["error"]["code"] == -32602
    message = body["error"]["message"]
    assert "`entries[12].hours` must be <= 24" in message
    assert "`entries[37].hours` is required" in message
    assert "`entries[44].phase` must be one of" in message
    assert "; `hours`" not in message and not message.startswith("`hours`")
    (refused,) = _events(caplog, "mcp_write_refused")
    assert refused["reason"] == "schema_invalid"
    assert refused["error_count"] == 3


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


@pytest.mark.parametrize("given, logged", [
    ("Tremblay c. Lavoie", None),                        # a title, pasted
    ("0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f\n", None),     # `$` would pass it
    ("0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f",
     "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f"),              # an id: kept
])
def test_the_span_and_the_tool_call_line_carry_only_an_id_shaped_dossier(
    client, monkeypatch, caplog, given, logged
):
    """A SUCCESSFUL call can carry a name too: a read tool answers an
    unknown dossier with an empty list, not a refusal. Until 2026-09-25 the
    raw argument reached the `mcp.tool.*` span attribute and every
    `mcp_tool_call` line — while OBSERVABILITY.md promised « UUIDs only »
    there — although the refusal line had already learnt the shape check."""
    spans = []

    class _Span:
        def __init__(self, name, **attrs):
            spans.append((name, attrs))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(endpoint, "span", _Span)
    monkeypatch.setattr(handlers, "list_notes", lambda args: {"notes": []})
    with caplog.at_level(logging.INFO, logger="pallas.mcp"):
        body = _call(client, "list_notes", {"dossier_id": given}).get_json()
    assert "error" not in body
    ((name, attrs),) = [(n, a) for n, a in spans if n.startswith("mcp.tool.")]
    assert name == "mcp.tool.list_notes"
    (call,) = _events(caplog, "mcp_tool_call")
    if logged is None:
        assert "dossier_id" not in attrs and "dossier_id" not in call
        assert "Tremblay" not in _logged_text(caplog)
    else:
        assert attrs == {"dossier_id": logged}
        assert call["dossier_id"] == logged


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
    assert "SECRET" not in text             # the cause never reaches the client

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


def test_the_client_reads_the_committed_sentence_never_the_error_rendered(
    write_client, monkeypatch
):
    """The endpoint answers with `client_message` — the sentence the error
    built from its ids — never `str(exc)` (py/stack-trace-exposure,
    2026-09-30). An error whose rendering carried anything else (a future
    subclass, a cause folded into its string) still shows only that
    sentence."""
    class _Rendered(tools.CommittedWriteError):
        def __str__(self) -> str:
            return "Traceback (most recent call last): titre SECRET"

    def create_note(args):
        raise _Rendered("create_note", _NOTE_ID, _DOSSIER_ID, collection="notes")

    monkeypatch.setattr(handlers, "create_note", create_note)
    body = _call(write_client, "create_note", _partial_args()).get_json()

    assert body["result"]["isError"] is True
    text = body["result"]["content"][0]["text"]
    assert "SECRET" not in text and "Traceback" not in text
    assert text == tools.CommittedWriteError(
        "create_note", _NOTE_ID, _DOSSIER_ID, collection="notes"
    ).client_message


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
    # Non-vacuous: the sweep must see every path this commit wires — each of
    # its three sources at least once (the endpoint's literal log line, its
    # `_log_write_refused`, a ToolArgumentError). The two switch refusals
    # (`write_disabled`, `comptabilite_disabled`) left with the switches on
    # 2026-10-05; two refusals of the write protocol stand in — the
    # in-flight key and the stale etag INSTRUCTIONS tell the model about.
    assert found["logged"] >= {"insufficient_scope", "schema_invalid"}
    assert reasons >= {
        "insufficient_scope", "schema_invalid", "argument_refused",
        "idempotency_conflict", "idempotency_in_flight", "stale_etag",
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
