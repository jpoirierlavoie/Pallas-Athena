"""Bounded MCP client for the legal-research Workers (Phase N).

Server-to-server, with a bearer token per Worker (D5/D10). The Workers are
MCP servers, not plain REST services — D5 said "REST simple à jeton" and
was AMENDED 2026-08-26 when the endpoints turned out to speak JSON-RPC:
one ``tools/call`` per invocation, over ``POST /mcp``. Nothing had to be
added on the Worker side; ``Authorization: Bearer`` and the stateless JSON
mode were already part of its contract.

The contract with the turn engine is unchanged: :func:`call_worker` NEVER
raises — every failure becomes ``{"ok": False, reason, message}`` that the
executor turns into an error tool_result, visible in the transcript (fail
loud, degrade never, SPEC §4.5). No automatic retry: a retry doubles
latency inside a budgeted turn chain, and the model retries visibly if it
judges it worthwhile.

WHAT COMES BACK IS TEXT, AND IT IS PASSED ON VERBATIM. An MCP tool result
is ``content: [{type: "text", text: …}]`` — French prose written for a
model to read. The connectors put their reliability warnings IN that prose
(« établit l'existence, jamais l'autorité actuelle »), so re-wrapping it
in JSON, summarising it, or reporting only its verdict would strip the
reserve off an assurance. Hence ``{"ok": True, "text": …}`` and an
executor that hands the string over untouched.

A tool that REFUSES (``isError: true``) is not a breakdown: it is a French
refusal the model must read and correct — so it comes back as
``ok: False`` carrying that same text, not a generic message.

Bounds: connect 5 s, read per-Worker (see ``_READ_TIMEOUT_S``); the
response body is read in bounded chunks and REFUSED past
``RESPONSE_CAP_BYTES`` (a truncated JSON document would parse into a
silently wrong answer — refusing is honest). ``reason`` values are
machine-stable and are the ONLY thing logged — a Worker response may quote
legal content and never reaches the logs.

Two response framings are handled, because the two Workers do not agree:
``jurisprudence`` answers ``application/json`` (stateless JSON mode), while
an MCP server built on the official SDK answers ``text/event-stream`` and
issues an ``Mcp-Session-Id``. Both are legal Streamable HTTP, and the twin
``legislation`` Worker is of the second kind — hence the SSE decoding and
the handshake below, which are inert for a Worker that needs neither.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import requests

from config import Config

CONNECT_TIMEOUT_S = 5
RESPONSE_CAP_BYTES = 2 * 1024 * 1024

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pallas-athena-chat", "version": "1"}

# Read timeout per Worker. `jurisprudence` gets more than the default
# because one of its tools is a CHAIN: canlii_verify_citations takes up to
# 25 citations and makes a throttled outbound call for each, so a lawful
# slow answer is not the same thing as a hung service.
_DEFAULT_READ_TIMEOUT_S = 30
_READ_TIMEOUT_S: dict[str, int] = {"jurisprudence": 60}

# Workers whose transport requires an `initialize` handshake and a session
# header before `tools/call` (the official-SDK framing). EMPTY in v1:
# `jurisprudence` is stateless and needs none. The `legislation` lot flips
# its entry to True — the mechanism below is already written and tested.
_REQUIRES_HANDSHAKE: dict[str, bool] = {}

# Session ids handed out by a stateful Worker, one per worker name. Kept
# per PROCESS: a Cloud Run instance reuses its session across turns, and a
# new instance simply initialises its own.
_SESSIONS: dict[str, str] = {}


def _base_url(worker: str) -> str:
    if worker == "legislation":
        return Config.LEGISLATION_WORKER_URL.rstrip("/")
    if worker == "jurisprudence":
        return Config.JURISPRUDENCE_WORKER_URL.rstrip("/")
    return ""


def _token(worker: str) -> str:
    if worker == "legislation":
        return Config.LEGISLATION_WORKER_TOKEN
    if worker == "jurisprudence":
        return Config.JURISPRUDENCE_WORKER_TOKEN
    return ""


def _fail(reason: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "message": message}


# ── Transport ───────────────────────────────────────────────────────────────


def _headers(worker: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_token(worker)}",
        "Content-Type": "application/json",
        # Both framings declared: a stateless Worker answers JSON, an
        # SDK-built one answers SSE, and each refuses a request that does
        # not say it can read what it is about to send.
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    session = _SESSIONS.get(worker)
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


def _post(worker: str, url: str, message: dict) -> dict[str, Any]:
    """One JSON-RPC message. Returns ``{"ok": True, "message": <parsed>}``
    (``None`` for an accepted notification) or a machine-stable failure."""
    try:
        response = requests.post(
            url,
            json=message,
            headers=_headers(worker),
            timeout=(
                CONNECT_TIMEOUT_S,
                _READ_TIMEOUT_S.get(worker, _DEFAULT_READ_TIMEOUT_S),
            ),
            stream=True,
        )
    except requests.Timeout:
        return _fail(
            "timeout",
            f"Le service « {worker} » n'a pas répondu à temps. "
            "Réessayez si le résultat est nécessaire.",
        )
    except requests.RequestException:
        return _fail("connection_error", f"Le service « {worker} » est injoignable.")

    with response:
        session = _header(response, "Mcp-Session-Id")
        if session:
            _SESSIONS[worker] = session
        status = response.status_code
        if status == 404 and _SESSIONS.get(worker):
            # A stateful Worker answers 404 to a session it no longer
            # holds. Drop it so the NEXT call re-initialises; this one
            # still fails, because a silent retry inside a budgeted turn
            # is exactly what the no-retry rule forbids.
            _SESSIONS.pop(worker, None)
        if status == 202:
            # Notification accepted; there is no body to read.
            return {"ok": True, "message": None}
        if status != 200:
            return _fail(
                f"http_{status}",
                f"Le service « {worker} » a répondu avec l'erreur HTTP {status}.",
            )
        content_type = _header(response, "Content-Type") or ""
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > RESPONSE_CAP_BYTES:
                    # A truncated JSON body would parse into a silently
                    # wrong answer — refuse the oversized response outright.
                    return _fail(
                        "response_too_large",
                        f"La réponse du service « {worker} » dépasse la taille "
                        "admise (2 Mo). Restreignez la requête.",
                    )
                chunks.append(chunk)
        except requests.RequestException:
            return _fail(
                "connection_error", f"Le service « {worker} » est injoignable."
            )

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    parsed = _decode(raw, content_type)
    if parsed is None:
        return _fail(
            "invalid_json", f"Le service « {worker} » a répondu un corps illisible."
        )
    return {"ok": True, "message": parsed}


def _header(response: Any, name: str) -> Optional[str]:
    """Header lookup that survives a response object without headers (the
    test doubles), because a missing header is never worth an exception."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        return headers.get(name)
    except Exception:  # pragma: no cover - defensive
        return None


def _decode(raw: str, content_type: str) -> Optional[dict]:
    """A JSON-RPC message, whether framed as JSON or as SSE."""
    if "text/event-stream" in content_type.lower():
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                candidate = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(candidate, dict) and "jsonrpc" in candidate:
                return candidate
        return None
    try:
        candidate = json.loads(raw)
    except ValueError:
        return None
    return candidate if isinstance(candidate, dict) else None


def _handshake(worker: str, url: str) -> Optional[dict[str, Any]]:
    """`initialize` + `notifications/initialized` for a stateful Worker.

    Returns None on success (the session id, if any, is now cached), or a
    failure dict. Inert while ``_REQUIRES_HANDSHAKE`` has no entry for the
    worker — which is the case for every Worker wired today.
    """
    if not _REQUIRES_HANDSHAKE.get(worker) or _SESSIONS.get(worker):
        return None
    outcome = _post(
        worker,
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        },
    )
    if not outcome.get("ok"):
        return outcome
    message = outcome.get("message") or {}
    if "error" in message:
        return _fail(
            "mcp_error",
            f"Le service « {worker} » a refusé l'initialisation du protocole.",
        )
    _post(
        worker,
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    return None


# ── The one public entry point ──────────────────────────────────────────────


def call_worker(spec: dict, args: dict) -> dict[str, Any]:
    """Execute one Worker tool call.

    Returns ``{"ok": True, "text": …}`` — the tool's own French prose,
    untouched — or ``{"ok": False, "reason": …, "message": …}``.
    """
    worker = str(spec.get("worker", ""))
    if not Config.worker_configured(worker):
        return _fail(
            "not_configured",
            f"L'outil {spec.get('name', '?')} est indisponible : le service "
            f"« {worker} » n'est pas configuré.",
        )
    transport = str(spec.get("transport", "mcp"))
    if transport != "mcp":
        # No second transport exists. Refusing beats inventing a fallback
        # that no Worker would answer.
        return _fail(
            "unsupported_transport",
            f"L'outil {spec.get('name', '?')} déclare un transport inconnu.",
        )

    url = _base_url(worker) + str(spec.get("path", ""))
    failed = _handshake(worker, url)
    if failed is not None:
        return failed

    outcome = _post(
        worker,
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            # The REMOTE name, never the namespaced one the model sees.
            "params": {"name": str(spec.get("tool", "")), "arguments": args},
        },
    )
    if not outcome.get("ok"):
        return outcome
    return _unwrap(worker, outcome.get("message") or {})


def _unwrap(worker: str, message: dict) -> dict[str, Any]:
    """A JSON-RPC response → the executor's contract.

    Three outcomes, and the middle one is the subtle one. A JSON-RPC
    ``error`` is a PROTOCOL fault (unknown method, malformed request); a
    ``result`` with ``isError`` is the tool REFUSING, in French, with a
    reason the model can act on. Collapsing the second into a generic
    message would throw away the only thing that lets the model correct
    itself — so its text travels verbatim.
    """
    if "error" in message:
        code = (message.get("error") or {}).get("code")
        return _fail(
            "mcp_error",
            f"Le service « {worker} » a refusé l'appel (erreur de protocole "
            f"{code}).",
        )
    result = message.get("result")
    if not isinstance(result, dict):
        return _fail(
            "bad_envelope",
            f"Le service « {worker} » a répondu hors contrat.",
        )
    text = _text_of(result)
    if result.get("isError"):
        return _fail(
            "tool_error",
            text
            or f"L'outil du service « {worker} » a échoué sans expliquer pourquoi.",
        )
    return {"ok": True, "text": text}


def _text_of(result: dict) -> str:
    """The text blocks of an MCP tool result, joined and unaltered.

    Non-text blocks (an image, an embedded resource) are skipped rather
    than described: none of these tools emits one, and inventing a
    placeholder would put words in the tool's mouth.
    """
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return ""
    parts = [
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)
