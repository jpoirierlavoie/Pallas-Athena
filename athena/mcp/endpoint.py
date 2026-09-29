"""POST /mcp — stateless, JSON-response-mode MCP Streamable HTTP endpoint.

One JSON-RPC 2.0 message per POST; every response is a single
``application/json`` body (no SSE streams, no ``Mcp-Session-Id``, no
server-initiated messages). Notifications are acknowledged with an empty
202. ``GET``/``DELETE`` fall through to Flask's automatic 405.
"""

import time
from typing import Any, Optional

from flask import Response, jsonify, request

from mcp import (
    DEFAULT_PROTOCOL_VERSION,
    SCOPE_COMPTABILITE,
    SUPPORTED_PROTOCOL_VERSIONS,
    comptabilite_enabled,
    mcp_bp,
)
from mcp import disclosure, jsonrpc, tools
from mcp.bearer import (
    ScopeRequired,
    granted_scopes,
    insufficient_scope_response,
    mcp_auth_required,
    revalidate_for_write,
)
from mcp.tools import CommittedWriteError, ToolArgumentError
from security import limiter
from utils.logging_setup import log_mcp_event, log_unexpected, sanitize_log_value
from utils.tracing_setup import span

# Instructions surfaced to the client model at initialize — ASSEMBLED from
# the disclosure registry (mcp/disclosure.py): the counts, the families and
# the « never » statements are derived, so a lot that adds a family or lifts
# a promise changes the registry and this text follows. Recopied by hand,
# the counts went stale twice and the undo path stated a falsehood (« frees
# the number »).
INSTRUCTIONS = disclosure.build_instructions()
# The variant a token holding athena:comptabilite reads (plan lot 5b): the
# ACCOUNTING family, its read, and the promises the accounting grant keeps.
# Every other token — the accounting switch off included — reads
# INSTRUCTIONS, which describes no tool it cannot see.
INSTRUCTIONS_COMPTABILITE = disclosure.build_instructions(accounting=True)


def instructions_for(scopes) -> str:
    """The INSTRUCTIONS of a token holding *scopes*: the accounting variant
    only while the token holds the scope AND the accounting switch is on —
    the one state in which it can see an accounting tool. With
    ``MCP_WRITE_ENABLED`` off that tool is the READ ``get_admin_ledger``
    alone, and the variant's ACCOUNTING paragraph still describes the five
    writes the master switch hides — as both variants describe every other
    write family whatever the write switch says (the texts are chosen by
    scope, never re-assembled per switch)."""
    if SCOPE_COMPTABILITE in (scopes or ()) and comptabilite_enabled():
        return INSTRUCTIONS_COMPTABILITE
    return INSTRUCTIONS


SERVER_INFO = {
    "name": "pallas-athena",
    "title": "Pallas Athéna",
    "version": "1.0.0",
}


def _protocol_version() -> tuple[Optional[str], Optional[Response]]:
    """Resolve the MCP-Protocol-Version header (absent → 2025-03-26)."""
    header = request.headers.get("MCP-Protocol-Version")
    if header is None:
        return DEFAULT_PROTOCOL_VERSION, None
    if header in SUPPORTED_PROTOCOL_VERSIONS:
        return header, None
    resp = jsonify(
        jsonrpc.error_response(
            None,
            jsonrpc.INVALID_REQUEST,
            f"Unsupported MCP-Protocol-Version; supported: "
            f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
        )
    )
    resp.status_code = 400
    return None, resp


@mcp_bp.route("/mcp", methods=["GET", "DELETE"])
def mcp_method_not_allowed() -> Response:
    """No SSE stream (GET), no sessions to delete (DELETE) — §9.1.

    Registered explicitly (rather than relying on Flask's automatic 405)
    so the blueprint's kill-switch before_request also covers these
    methods with a 404 when MCP_ENABLED is off.
    """
    resp = jsonify({"error": "method_not_allowed"})
    resp.status_code = 405
    resp.headers["Allow"] = "POST"
    return resp


@mcp_bp.route("/mcp", methods=["POST"])
@limiter.limit("240 per minute")
@mcp_auth_required
def mcp_endpoint() -> Any:
    protocol_version, version_error = _protocol_version()
    if version_error is not None:
        return version_error

    try:
        message = jsonrpc.parse_message(request.get_data())
    except jsonrpc.JsonRpcError as exc:
        return jsonify(jsonrpc.error_response(exc.request_id, exc.code, exc.message))

    if jsonrpc.is_notification(message):
        # notifications/initialized, notifications/cancelled, …
        return "", 202

    request_id = message["id"]
    method = message["method"]
    params = message.get("params") or {}

    with span("mcp.request", method=method):
        try:
            result = _dispatch(method, params, request_id, protocol_version)
        except jsonrpc.JsonRpcError as exc:
            return jsonify(
                jsonrpc.error_response(request_id, exc.code, exc.message)
            )
        except ScopeRequired as exc:
            # MUST precede `except Exception` below: caught there, an
            # authorization refusal would become a 200 "internal error" with
            # no 403 and no WWW-Authenticate step-up signal — and would be
            # indistinguishable from a Firestore outage in the logs.
            log_mcp_event(
                "mcp_write_refused",
                "refused",
                tool=exc.tool or None,
                reason="insufficient_scope",
            )
            return insufficient_scope_response(exc.scope)
        except Exception:
            log_unexpected("mcp request dispatch failed")
            return jsonify(
                jsonrpc.error_response(
                    request_id, jsonrpc.INTERNAL_ERROR, "Internal error"
                )
            )
    return jsonify(jsonrpc.result_response(request_id, result))


def _dispatch(
    method: str,
    params: dict,
    request_id: jsonrpc.RequestId,
    protocol_version: str,
) -> dict:
    if method == "initialize":
        return _initialize(params)
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tools.list_tool_descriptors(granted_scopes())}
    if method == "tools/call":
        return _tools_call(params, protocol_version)
    raise jsonrpc.JsonRpcError(
        jsonrpc.METHOD_NOT_FOUND, f"Method not found: {method}", request_id
    )


def _initialize(params: dict) -> dict:
    requested = params.get("protocolVersion")
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        negotiated = requested
    else:
        negotiated = SUPPORTED_PROTOCOL_VERSIONS[0]
    client_info = params.get("clientInfo") or {}
    log_mcp_event(
        "mcp_initialize",
        "success",
        client_name=sanitize_log_value(str(client_info.get("name", ""))[:80]),
        client_version=sanitize_log_value(str(client_info.get("version", ""))[:40]),
        protocol_version=negotiated,
    )
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(SERVER_INFO),
        "instructions": instructions_for(granted_scopes()),
    }


# A REFUSED call's `dossier_id` is still the caller's string: the schema
# bounds it to 64 characters and nothing more, and the commonest handler
# refusal is precisely « dossier introuvable ». A title or a party name
# pasted into the argument must not reach the refusal log, where the
# RedactionFilter does not scrub names. The shape check lives in mcp.tools
# since the write protocol needs the same one for its stored records.
_loggable_id = tools.loggable_id


def _log_write_refused(name: str, reason: str, **fields: Any) -> None:
    """Log a refused WRITE call: the tool, a reason CODE, ids and counts.

    Never the refusal's text — schema errors quote argument names the
    caller chose, and handler refusals describe user-supplied content (a
    note's text, a party's name) that the RedactionFilter does not scrub.
    A read tool's refusal is not logged here: `mcp_write_refused` is the
    write audit's counterpart, and a bad read argument changes nothing.
    """
    if name in tools.WRITE_TOOLS:
        log_mcp_event(
            "mcp_write_refused", "refused", tool=name, reason=reason, **fields
        )


def _tools_call(params: dict, protocol_version: str) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or name not in tools.TOOLS:
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS,
            f"Unknown tool: {sanitize_log_value(str(name))[:80]}",
        )

    # Authorization BEFORE argument validation and before any handler runs,
    # so a refused write never touches the model layer.
    # The refusal names the switch that is actually OFF — the master write
    # switch, or the accounting one — so the operator reading it flips the
    # right variable. Two literal reason codes, never a computed one (the
    # refusal-reason sweep in test_mcp_jsonrpc reads them from the source).
    switch = tools.unavailable_reason(name)
    if switch == tools.COMPTABILITE_SWITCH:
        log_mcp_event(
            "mcp_write_refused", "refused", tool=name,
            reason="comptabilite_disabled",
        )
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS,
            "Accounting tools are disabled on this server "
            f"({tools.COMPTABILITE_SWITCH}).",
        )
    if switch is not None:
        log_mcp_event(
            "mcp_write_refused", "refused", tool=name, reason="write_disabled"
        )
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS,
            f"Write tools are disabled on this server ({switch}).",
        )
    needed = tools.required_scope(name)
    if needed not in granted_scopes():
        raise ScopeRequired(needed, name)
    if name in tools.WRITE_TOOLS:
        # Re-read the live token: the bearer success cache is a read-path
        # optimization and must not let a revoked token mutate the file.
        # `needed` is the tool's OWN scope — athena:comptabilite for an
        # accounting tool — so the live token must still carry THAT one;
        # athena:write never stands in for it.
        revalidate_for_write(needed, name)

    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        _log_write_refused(name, "schema_invalid", error_count=1)
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS, "arguments must be an object"
        )

    schema = tools.TOOLS[name]["input_schema"]
    validation_errors = tools.validate_args(schema, arguments)
    if validation_errors:
        # The COUNT only: the messages quote argument names the caller
        # chose, and an unvalidated `dossier_id` is not an id yet.
        _log_write_refused(
            name, "schema_invalid", error_count=len(validation_errors)
        )
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS, "; ".join(validation_errors)
        )

    # The span attribute and every `mcp_tool_call` line carry the dossier
    # only when it is SHAPED like an id. On any path — success included (a
    # read tool answers an unknown dossier with an empty list, not a
    # refusal) — the argument is still the caller's string, bounded to 64
    # characters by the schema and nothing more: a title or a party name
    # pasted into it would reach Cloud Trace and the tool-call log, which
    # the RedactionFilter does not scrub for names (OBSERVABILITY.md:
    # `dossier_id` span attributes are « UUIDs only »).
    dossier_id = _loggable_id(arguments.get("dossier_id"))
    span_attrs: dict[str, Any] = {}
    if dossier_id:
        span_attrs["dossier_id"] = dossier_id

    handler = tools.get_handler(name)
    started = time.perf_counter()
    argument_error: Optional[str] = None
    argument_reason = tools.DEFAULT_REFUSAL_REASON
    committed_error: Optional[CommittedWriteError] = None
    try:
        with span(f"mcp.tool.{name}", **span_attrs):
            # ToolArgumentError is caught INSIDE the span. `span()` calls
            # record_exception + set_status(str(exc)) on anything crossing
            # its boundary, and these messages describe user-supplied
            # content — letting one through would ship a fragment of a
            # privileged note to Cloud Trace, which the exporter's
            # attribute scrubbing does not cover (it sanitizes attributes,
            # not exception events).
            try:
                payload = handler(arguments)
            except ToolArgumentError as exc:
                argument_error = str(exc)
                argument_reason = exc.reason
                payload = None
            except CommittedWriteError as exc:
                # A write that COMMITTED, then failed. Caught here and not
                # by the blanket `except` below, which would answer
                # « internal error » — a retryable failure, so the caller
                # would retry and write twice. Inside the span for the same
                # reason as above: its own fields are ids, but its
                # `__cause__` is the original exception, whose text may
                # describe content — and record_exception would export the
                # chain. run_write has already logged the traceback
                # (`unexpected`) and recorded the entry `partial`.
                committed_error = exc
                payload = None
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        log_unexpected("mcp tool execution failed", tool=name)
        log_mcp_event(
            "mcp_tool_call",
            "failure",
            tool=name,
            duration_ms=duration_ms,
            **({"dossier_id": dossier_id} if span_attrs else {}),
        )
        # Execution errors are tool RESULTS, not protocol errors (MCP spec).
        return tools.error_result(
            "Tool execution failed due to an internal error."
        )

    if committed_error is not None:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        # Ids, a collection name and counts only — never content. No
        # `mcp_write` success line: the call did not succeed, and the audit
        # line that says « written » would mislead a reader counting writes.
        partial_fields = {
            "entity_id": _loggable_id(committed_error.entity_id),
            "dossier_id": _loggable_id(committed_error.dossier_id),
            "collection": committed_error.collection or None,
        }
        log_mcp_event(
            "mcp_write_partial",
            "failure",
            tool=name,
            rows=committed_error.rows,
            idempotent_replay=committed_error.replay,
            **{k: v for k, v in partial_fields.items() if v is not None},
        )
        log_mcp_event(
            "mcp_tool_call",
            "failure",
            tool=name,
            duration_ms=duration_ms,
            **({"dossier_id": dossier_id} if span_attrs else {}),
        )
        # A tool RESULT with isError — not a -32602, which promises that
        # nothing was written. The message says the opposite, loudly.
        return tools.error_result(str(committed_error))

    if argument_error is not None:
        # The refusal is LOGGED by its reason code — the text below reaches
        # the client only. OBSERVABILITY.md promised an `mcp_write_refused`
        # burst as the stop-the-import signal long before either of these
        # refusal paths emitted one. The dossier id only when id-shaped
        # (`dossier_id` above is already the shape-checked value): a
        # refused call's argument has been checked for length, not for
        # being an id.
        _log_write_refused(
            name,
            argument_reason,
            **({"dossier_id": dossier_id} if dossier_id else {}),
        )
        # Raised outside the span, so its (user-derived) text never reaches
        # the exporter. It still reaches the client, which is the point.
        raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, argument_error)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if name in tools.WRITE_TOOLS:
        # Generalized write audit (WP15): entity ids and counts only, never
        # a title or content. `entity` is the WP16+ shape; the two note
        # tools keep their historical `note` key and ALSO keep emitting the
        # original mcp_note_written event for log-metric continuity.
        entity = (payload or {}).get("entity") or (payload or {}).get("note") or {}
        replay = bool((payload or {}).get("idempotent_replay"))
        common = {
            "tool": name,
            "dossier_id": entity.get("dossier_id") or None,
            "entity_id": entity.get("id") or None,
            # A replay means « nothing new was written » — the audit line
            # must say so. (`dry_run` sat beside it until 2026-08-27, when
            # the preview left the write protocol.)
            "idempotent_replay": replay,
            # The bump itself, NOT dav_synced — a closed dossier bumps
            # correctly but is never advertised to DavX5, and conflating the
            # two would make a healthy write look like a sync failure.
            # A replay hands back the FIRST call's stored payload, `true`
            # included, while bumping nothing: logged as-is, every replay
            # claimed a CTag bump that never happened.
            "ctag_bumped": bool((payload or {}).get("ctag_bumped")) and not replay,
            "dav_synced": bool((payload or {}).get("dav_synced")),
        }
        log_mcp_event("mcp_write", "success", **common)
        if (payload or {}).get("note"):
            note = (payload or {}).get("note") or {}
            log_mcp_event(
                "mcp_note_written",
                "success",
                tool=name,
                dossier_id=note.get("dossier_id") or None,
                note_id=note.get("id") or None,
                content_chars=note.get("content_length"),
                ctag_bumped=common["ctag_bumped"],
                dav_synced=common["dav_synced"],
            )
    log_mcp_event(
        "mcp_tool_call",
        "success",
        tool=name,
        duration_ms=duration_ms,
        **({"dossier_id": dossier_id} if span_attrs else {}),
    )
    return tools.tool_result(payload, protocol_version)
