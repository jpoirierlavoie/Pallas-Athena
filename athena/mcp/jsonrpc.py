"""JSON-RPC 2.0 message parsing and response envelopes for the MCP endpoint.

The MCP Streamable HTTP transport (JSON response mode) carries exactly one
JSON-RPC message per POST. Batch arrays are rejected (batching was removed
in protocol revision 2025-06-18 and Claude never batches).
"""

import json
from typing import Any, Optional, Union

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RequestId = Union[str, int, None]


class JsonRpcError(Exception):
    """Protocol-level JSON-RPC error, converted to an error response."""

    def __init__(self, code: int, message: str, request_id: RequestId = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


def result_response(request_id: RequestId, result: Any) -> dict:
    """Build a JSON-RPC success response object."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: RequestId,
    code: int,
    message: str,
    data: Optional[Any] = None,
) -> dict:
    """Build a JSON-RPC error response object."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_message(raw: bytes) -> dict:
    """Parse and structurally validate a single JSON-RPC 2.0 message.

    Returns the message dict. Raises :class:`JsonRpcError` with the
    appropriate protocol code on any failure. Notification detection
    (absent ``id``) is the caller's job — this only validates shape.
    """
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise JsonRpcError(PARSE_ERROR, "Parse error")

    if isinstance(message, list):
        # Batching is not supported on either protocol revision we serve.
        raise JsonRpcError(INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, "Invalid request")
    if message.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: jsonrpc must be \"2.0\"")

    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: method must be a string")

    if "id" in message and not isinstance(message["id"], (str, int, type(None))):
        raise JsonRpcError(INVALID_REQUEST, "Invalid request: id must be a string or number")

    params = message.get("params")
    if params is not None and not isinstance(params, dict):
        raise JsonRpcError(
            INVALID_REQUEST,
            "Invalid request: params must be an object",
            request_id=message.get("id"),
        )

    return message


def is_notification(message: dict) -> bool:
    """A message without an ``id`` (or with a null id) is a notification."""
    return message.get("id") is None
