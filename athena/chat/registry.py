"""The chat-side tool registry (Phase N).

One registry maps tool name → executor. Three executors (SPEC §4.1):

* ``in_process``       — the MCP tool set, called directly into
                         ``mcp/handlers.py`` (Flask-free, proven). Schemas
                         are referenced from ``mcp.tools.TOOLS`` BY
                         IDENTITY — never copied, so the two surfaces
                         cannot drift.
* ``http_worker``      — the legislation/jurisprudence Workers
                         (chat/worker_tools.py + worker_client.py).
                         CHAT-ONLY: these names never enter ``TOOLS`` and
                         never reach the external connector.
* ``anthropic_native`` — ``web_search``, declared in the request body and
                         executed by Anthropic server-side (basic version
                         only on Vertex — no dynamic filtering).

Write parity (user decision D9, 2026-08-26): the chat exposes the SAME
write set as the external connector — ``CHAT_WRITE_TOOLS`` is DERIVED from
``mcp.tools.WRITE_TOOLS``, so parity holds by construction and a future
divergence is a one-line edit of this module, made consciously. The
guard-rails for unattended runs are the charter's dry_run discipline, the
forced idempotency keys (executors.py), and the ``GATED_TOOLS`` mechanism
below.

``GATED_TOOLS`` (SPEC §4.6.3): a ``tool_use`` on a member pauses the turn
into ``awaiting_authorization`` (interactive) or is auto-refused with the
dry_run directive (scheduled). The set ships EMPTY in v1 — the mechanism is
implemented, the policy is the practitioner's to widen, one name at a time.

No capability named « delete » exists, can be registered, or is reachable.
"""

from __future__ import annotations

from typing import Any, Optional

from config import Config
from mcp import tools as mcp_tools

from chat.worker_tools import WORKER_NAME_PREFIXES, WORKER_TOOLS

# Executor identifiers (also the `executor` field of chat_tool_call events).
IN_PROCESS = "in_process"
HTTP_WORKER = "http_worker"
ANTHROPIC_NATIVE = "anthropic_native"

# The one Anthropic-native tool. Basic web search only on Vertex (verified
# 2026-08-26); its per-search cost lands in usage.server_tool_use.
WEB_SEARCH_NAME = "web_search"
WEB_SEARCH_TYPE = "web_search_20250305"

# D9 — full parity with the connector, BY DERIVATION (drift structurally
# impossible; a conscious divergence edits this line).
CHAT_WRITE_TOOLS: frozenset[str] = frozenset(mcp_tools.WRITE_TOOLS)

# §4.6.3 — requires_authorization. EMPTY in v1 (FLAG 3), pinned by test;
# widening it is config-by-code, one name per line, with a reason.
GATED_TOOLS: frozenset[str] = frozenset()


def is_gated(name: str) -> bool:
    return name in GATED_TOOLS


def writes_enabled() -> bool:
    return bool(Config.CHAT_WRITE_ENABLED)


def find_worker_spec(name: str) -> Optional[dict]:
    for spec in WORKER_TOOLS:
        if spec.get("name") == name:
            return spec
    return None


def available_worker_specs() -> list[dict]:
    """Worker tools whose Worker is actually configured (URL + token).

    An unconfigured Worker's tools are simply ABSENT from the model's tool
    array — the charter's citation rule then degrades honestly (« non
    vérifiée ») instead of the model calling into a wall.
    """
    return [
        spec
        for spec in WORKER_TOOLS
        if Config.worker_configured(str(spec.get("worker", "")))
    ]


def executor_for(name: str) -> Optional[str]:
    """The executor a tool name runs under, or None for an unknown name."""
    if name == WEB_SEARCH_NAME:
        return ANTHROPIC_NATIVE
    if name in mcp_tools.TOOLS:
        return IN_PROCESS
    if name.startswith(WORKER_NAME_PREFIXES) and find_worker_spec(name):
        return HTTP_WORKER
    return None


def chat_tool_names(*, include_writes: Optional[bool] = None) -> list[str]:
    """The in-process tool names the chat offers, in TOOLS order (stable —
    the prompt-cache prefix depends on it)."""
    writes = writes_enabled() if include_writes is None else include_writes
    names = []
    for name in mcp_tools.TOOLS:
        if name in mcp_tools.WRITE_TOOLS:
            if not writes or name not in CHAT_WRITE_TOOLS:
                continue
        names.append(name)
    return names


def anthropic_tools(*, include_writes: Optional[bool] = None) -> list[dict[str, Any]]:
    """The Messages API ``tools`` array for a turn.

    Internal entries reuse ``TOOLS[name]["input_schema"]`` BY IDENTITY
    (clean subset JSON Schema — no $ref/$schema/format/default, verified);
    the write tools keep their injected ``dry_run``/``idempotency_key``
    properties, which ARE the §4.6.2/§12.3 proposal mechanism. Order is
    stable: internal tools in TOOLS order, then Worker tools in declaration
    order, then web_search — the trailing cache_control breakpoint
    (chat/vertex.py) covers the whole array only because this order never
    shifts between calls.

    Nothing model-facing contains a secret: the array is built from schemas
    and descriptions only (pinned by test).
    """
    tools: list[dict[str, Any]] = []
    for name in chat_tool_names(include_writes=include_writes):
        spec = mcp_tools.TOOLS[name]
        tools.append(
            {
                "name": name,
                "description": spec["description"],
                "input_schema": spec["input_schema"],
            }
        )
    for worker_spec in available_worker_specs():
        tools.append(
            {
                "name": worker_spec["name"],
                "description": worker_spec["description"],
                "input_schema": worker_spec["input_schema"],
            }
        )
    tools.append(
        {
            "type": WEB_SEARCH_TYPE,
            "name": WEB_SEARCH_NAME,
            "max_uses": Config.CHAT_WEB_SEARCH_MAX_USES,
        }
    )
    return tools
