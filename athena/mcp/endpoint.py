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
    SUPPORTED_PROTOCOL_VERSIONS,
    mcp_bp,
)
from mcp import jsonrpc, tools
from mcp.bearer import (
    ScopeRequired,
    granted_scopes,
    insufficient_scope_response,
    mcp_auth_required,
    revalidate_for_write,
)
from mcp.tools import ToolArgumentError
from security import limiter
from utils.logging_setup import log_mcp_event, log_unexpected, sanitize_log_value
from utils.tracing_setup import span

# Verbatim §9.3 instructions surfaced to the client model at initialize.
INSTRUCTIONS = (
    "Pallas Athena is a single-user Quebec civil litigation practice "
    "manager. Every tool is read-only EXCEPT two that write notes: "
    "`create_note` (new note on a dossier) and `append_to_note` (appends to "
    "an existing note). They appear only when the lawyer granted the "
    "`athena:write` scope. Nothing else can be created, modified or "
    "deleted, and no note can ever be edited or deleted through this "
    "connector. A write is permanent, syncs to the lawyer's phone, and has "
    "no undo here — confirm with the user before writing, never write to a "
    "dossier you have not read first, and never retry a write that appeared "
    "to fail without re-checking with list_notes or get_note (there is no "
    "de-duplication, so a blind retry duplicates the note). Note content is "
    "Markdown, written in French; raw HTML is refused. Domain data (titles, "
    "notes, statuses, categories) is in French. Monetary amounts appear as "
    "integer `*_cents` plus a formatted `*_display` string (CAD). "
    "Datetimes are ISO 8601 in America/Montreal; date-only fields are "
    "`YYYY-MM-DD`. IDs are UUIDv4 strings — pass them between tools "
    "verbatim. Start broad (get_agenda, list_dossiers, search) and narrow "
    "with get_dossier / get_note / list_* filters."
)

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
        "instructions": INSTRUCTIONS,
    }


def _tools_call(params: dict, protocol_version: str) -> dict:
    name = params.get("name")
    if not isinstance(name, str) or name not in tools.TOOLS:
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS,
            f"Unknown tool: {sanitize_log_value(str(name))[:80]}",
        )

    # Authorization BEFORE argument validation and before any handler runs,
    # so a refused write never touches the model layer.
    if not tools.tool_available(name):
        log_mcp_event(
            "mcp_write_refused", "refused", tool=name, reason="write_disabled"
        )
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS,
            "Write tools are disabled on this server (MCP_WRITE_ENABLED).",
        )
    needed = tools.required_scope(name)
    if needed not in granted_scopes():
        raise ScopeRequired(needed, name)
    if name in tools.WRITE_TOOLS:
        # Re-read the live token: the bearer success cache is a read-path
        # optimization and must not let a revoked token mutate the file.
        revalidate_for_write(needed)

    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS, "arguments must be an object"
        )

    schema = tools.TOOLS[name]["input_schema"]
    validation_errors = tools.validate_args(schema, arguments)
    if validation_errors:
        raise jsonrpc.JsonRpcError(
            jsonrpc.INVALID_PARAMS, "; ".join(validation_errors)
        )

    dossier_id = arguments.get("dossier_id")
    span_attrs: dict[str, Any] = {}
    if isinstance(dossier_id, str) and dossier_id:
        span_attrs["dossier_id"] = dossier_id

    handler = tools.get_handler(name)
    started = time.perf_counter()
    argument_error: Optional[str] = None
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

    if argument_error is not None:
        # Raised outside the span, so its (user-derived) text never reaches
        # the exporter. It still reaches the client, which is the point.
        raise jsonrpc.JsonRpcError(jsonrpc.INVALID_PARAMS, argument_error)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if name in tools.WRITE_TOOLS:
        # append_to_note is called with only a note_id, so its audit record
        # would otherwise carry no dossier — the one call that mutates an
        # existing client record would be the least traceable. IDs and
        # counts only; never the title or the content.
        note = (payload or {}).get("note") or {}
        log_mcp_event(
            "mcp_note_written",
            "success",
            tool=name,
            dossier_id=note.get("dossier_id") or None,
            note_id=note.get("id") or None,
            content_chars=note.get("content_length"),
            # The bump itself, NOT dav_synced — a closed dossier bumps
            # correctly but is never advertised to DavX5, and conflating the
            # two would make a healthy write look like a sync failure.
            ctag_bumped=bool((payload or {}).get("ctag_bumped")),
            dav_synced=bool((payload or {}).get("dav_synced")),
        )
    log_mcp_event(
        "mcp_tool_call",
        "success",
        tool=name,
        duration_ms=duration_ms,
        **({"dossier_id": dossier_id} if span_attrs else {}),
    )
    return tools.tool_result(payload, protocol_version)
