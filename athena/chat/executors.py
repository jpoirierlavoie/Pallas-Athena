"""Tool execution for the turn worker (Phase N).

The in-process branch calls the MCP handlers directly — which BYPASSES
everything ``mcp/endpoint._tools_call`` does (scope, kill switch, argument
validation, audit logging). This module reproduces the load-bearing subset
itself:

* argument validation via ``mcp.tools.validate_args`` (the free function),
  errors returned to the MODEL as an error tool_result so it can
  self-correct — never an exception;
* the write kill switch (``Config.CHAT_WRITE_ENABLED``) and the
  ``GATED_TOOLS`` unattended auto-refusal (SPEC §12.3);
* the audit line — ``log_chat_event("chat_tool_call"/"chat_tool_refused")``
  with tool name, executor, duration and machine-stable reasons only;
* the catch-all: ``ToolArgumentError`` surfaces its French message
  (handlers never quote content in refusals — the house doctrine), any
  other exception is logged via ``log_unexpected`` and becomes a GENERIC
  French error that quotes nothing.

NOTHING here ever raises out of a turn: every failure is an error
tool_result, visible in the transcript (SPEC §4.5 — fail loud, degrade
never). CTag bumps ride inside the handlers themselves, so DAV correctness
is inherited by construction.

Scope note: authorization here is the lawyer's session at POST time plus
the task context — there is no bearer token, no scope, and
``bearer.revalidate_for_write`` has no meaning in-process (revoking the
CONNECTOR must not disable the firm's own chat).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from mcp import tools as mcp_tools
from utils.logging_setup import log_chat_event, log_unexpected

from chat import registry
from chat.worker_client import call_worker

_GENERIC_ERROR_FR = (
    "L'outil a échoué pour une raison inattendue. L'erreur est journalisée ; "
    "réessayez ou reformulez la demande."
)

_GATED_UNATTENDED_FR = (
    "Refusé : cet outil exige une autorisation humaine, et cette exécution "
    "planifiée tourne sans surveillance. Proposez l'action dans votre "
    "rapport en exécutant l'appel en dry_run: true, pour que l'avocat la "
    "commette lui-même."
)


@dataclass(frozen=True)
class ToolExecution:
    """What the turn engine folds into a tool_result content block."""

    content: str
    is_error: bool


def _serialize(payload: Any) -> str:
    """Mirror of ``mcp.tools.tool_result``'s text serialization."""
    return json.dumps(
        mcp_tools._jsonable(payload), ensure_ascii=False, indent=2
    )


def _unattended_key(seed: str, name: str, tool_use_id: str) -> str:
    """Deterministic idempotency key for an unattended write (SPEC §12.3):
    derived from (task, occurrence, step) — the seed — plus the tool_use id,
    so a queue redelivery replays instead of duplicating."""
    digest = hashlib.sha256(
        f"{seed}|{name}|{tool_use_id}".encode("utf-8")
    ).hexdigest()
    return f"planifie-{digest[:32]}"


def execute_tool(
    name: str,
    args: Any,
    *,
    conversation_id: str,
    turn_id: str,
    step: int,
    unattended: bool = False,
    idempotency_seed: str = "",
    tool_use_id: str = "",
    provenance_extra: Optional[dict] = None,
    skill_pairs: Optional[list] = None,
) -> ToolExecution:
    """Execute one tool call and return its tool_result material.

    ``provenance_extra`` lets the turn engine enrich the draft-provenance
    seam with what only it knows (model id, skill versions, charter
    version); it never overrides the identity keys set here.
    ``skill_pairs`` is THIS turn's resolved version list — entries are
    DICTS ``{skill_id, version}``, never pairs (Firestore refuses nested
    arrays; see turn_engine._resolve_skills) —
    the only route through which ``get_skill_file`` resolves a file (a
    skill outside the pairs was outside the prompt too).

    The INTERACTIVE authorization pause (``awaiting_authorization``) is the
    turn engine's decision, taken BEFORE this function; the unattended
    auto-refusal of a gated tool lives here because it produces an ordinary
    error tool_result and the chain continues (SPEC §12.3 — no run ever
    stalls waiting for a human who is not there).
    """
    executor = registry.executor_for(name)
    arguments = args if isinstance(args, dict) else {}

    if executor is None:
        log_chat_event(
            "chat_tool_refused",
            "refused",
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool=name,
            reason="unknown_tool",
            step=step,
        )
        return ToolExecution(
            content=f"Unknown tool: {name}.",
            is_error=True,
        )

    if registry.is_gated(name) and unattended:
        log_chat_event(
            "chat_tool_refused",
            "refused",
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool=name,
            reason="gated_unattended",
            step=step,
        )
        return ToolExecution(content=_GATED_UNATTENDED_FR, is_error=True)

    if name in registry.CHAT_WRITE_TOOLS and not registry.writes_enabled():
        log_chat_event(
            "chat_tool_refused",
            "refused",
            conversation_id=conversation_id,
            turn_id=turn_id,
            tool=name,
            reason="write_disabled",
            step=step,
        )
        return ToolExecution(
            content=(
                "Les écritures du clavardage sont désactivées "
                "(CHAT_WRITE_ENABLED). Lecture seulement."
            ),
            is_error=True,
        )

    started = time.monotonic()
    if executor == registry.HTTP_WORKER:
        outcome = _execute_worker(name, arguments)
    elif executor == registry.SKILL_FILE:
        outcome = _execute_skill_file(arguments, skill_pairs=skill_pairs or [])
    else:
        outcome = _execute_in_process(
            name,
            arguments,
            unattended=unattended,
            idempotency_seed=idempotency_seed,
            tool_use_id=tool_use_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            step=step,
            provenance_extra=provenance_extra,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    log_chat_event(
        "chat_tool_call",
        "failure" if outcome.is_error else "success",
        conversation_id=conversation_id,
        turn_id=turn_id,
        tool=name,
        step=step,
        executor=executor,
        duration_ms=duration_ms,
    )
    return outcome


def _execute_in_process(
    name: str,
    arguments: dict,
    *,
    unattended: bool,
    idempotency_seed: str,
    tool_use_id: str,
    conversation_id: str,
    turn_id: str,
    step: int,
    provenance_extra: Optional[dict] = None,
) -> ToolExecution:
    schema = mcp_tools.TOOLS[name]["input_schema"]
    errors = mcp_tools.validate_args(schema, arguments)
    if errors:
        # The model self-corrects on a listed refusal; validator strings are
        # schema-derived and quote no content.
        return ToolExecution(
            content=f"Invalid arguments for {name}: " + "; ".join(errors),
            is_error=True,
        )

    if (
        unattended
        and name in mcp_tools.WRITE_TOOLS
        and idempotency_seed
        and "idempotency_key" not in arguments
    ):
        # SPEC §12.3 — mandatory in scheduled runs: a queue redelivery must
        # replay the stored result, never write twice.
        arguments = {
            **arguments,
            "idempotency_key": _unattended_key(idempotency_seed, name, tool_use_id),
        }

    provenance_token = _set_draft_provenance(
        name, conversation_id, turn_id, unattended, provenance_extra
    )
    try:
        handler = mcp_tools.get_handler(name)
        payload = handler(arguments)
    except mcp_tools.ToolArgumentError as exc:
        # Handler refusals are French and never quote content (doctrine).
        return ToolExecution(content=str(exc), is_error=True)
    except Exception:
        log_unexpected(
            "chat tool execution failed",
            tool=name,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        return ToolExecution(content=_GENERIC_ERROR_FR, is_error=True)
    finally:
        _reset_draft_provenance(name, provenance_token)

    if name in _DRAFT_TOOLS and isinstance(payload, dict):
        draft = payload.get("draft") or {}
        if not payload.get("dry_run"):
            log_chat_event(
                "chat_draft_written",
                conversation_id=conversation_id,
                turn_id=turn_id,
                draft_id=str(draft.get("id", "")),
                version=int(draft.get("current_version") or 0),
                dossier_id=str(draft.get("dossier_id", "")) or None,
                content_chars=int(draft.get("content_length") or 0),
            )
    return ToolExecution(content=_serialize(payload), is_error=False)


def _execute_worker(name: str, arguments: dict) -> ToolExecution:
    spec = registry.find_worker_spec(name)
    result = call_worker(spec or {}, arguments)
    if not result.get("ok"):
        return ToolExecution(
            content=str(result.get("message", _GENERIC_ERROR_FR)),
            is_error=True,
        )
    return ToolExecution(content=_serialize(result.get("payload")), is_error=False)


# ── get_skill_file (executor `skill_file`) ──────────────────────────────────
#
# A READ, and a sibling of _execute_in_process on purpose: none of the
# in-process machinery applies — no idempotency-key injection (WRITE_TOOLS-
# gated), no PROVENANCE ContextVar (_DRAFT_TOOLS-gated), no handler
# resolution (the name is not in TOOLS — mcp_tools.TOOLS[name] would
# KeyError). Version consistency is the point: the file is resolved at the
# (skill_id, version) pair THIS turn's assembly used, never a re-read head.

_SKILL_NOT_SELECTED_FR = (
    "Compétence non sélectionnée pour ce tour : seuls les fichiers des "
    "compétences listées dans le bloc système sont lisibles."
)


def _read_skill_file(skill_id: str, version: int, filename: str):
    """Lazy-import seam (the _set_draft_provenance motif): models/__init__
    builds the Firestore client at import, and test_chat_registry imports
    this module without patching it."""
    from models import chat_skill

    return chat_skill.get_version_file(skill_id, version, filename)


def _execute_skill_file(
    arguments: dict, *, skill_pairs: list
) -> ToolExecution:
    errors = mcp_tools.validate_args(
        registry.GET_SKILL_FILE_SPEC["input_schema"], arguments
    )
    if errors:
        return ToolExecution(
            content=(
                f"Invalid arguments for {registry.GET_SKILL_FILE_NAME}: "
                + "; ".join(errors)
            ),
            is_error=True,
        )
    skill_id = str(arguments.get("skill_id", ""))
    version = next(
        (
            int(entree.get("version") or 0)
            for entree in skill_pairs
            if isinstance(entree, dict)
            and str(entree.get("skill_id", "")) == skill_id
        ),
        None,
    )
    if version is None:
        # Never echoes the requested id — refusals quote nothing.
        return ToolExecution(content=_SKILL_NOT_SELECTED_FR, is_error=True)
    try:
        content, reason = _read_skill_file(
            skill_id, version, str(arguments.get("filename", ""))
        )
    except Exception:
        log_unexpected(
            "chat skill file read failed",
            tool=registry.GET_SKILL_FILE_NAME,
        )
        return ToolExecution(content=_GENERIC_ERROR_FR, is_error=True)
    if reason is not None:
        return ToolExecution(content=reason, is_error=True)
    # RAW text, no JSON envelope — reference material reads best as itself,
    # and the indent=2 serialization would only inflate the block toward
    # the offload threshold.
    return ToolExecution(content=content or "", is_error=False)


# ── Draft provenance (ContextVar seam — models/chat_draft.py) ───────────────
#
# The draft model reads its provenance from a ContextVar, never from
# forgeable schema arguments. The token-based reset in `finally` above is
# load-bearing: a leaked value would stamp the NEXT task's draft with this
# turn's identity.

_DRAFT_TOOLS = frozenset({"save_draft", "revise_draft"})


def _set_draft_provenance(
    name: str,
    conversation_id: str,
    turn_id: str,
    unattended: bool,
    extra: Optional[dict] = None,
) -> Optional[object]:
    if name not in _DRAFT_TOOLS:
        return None
    try:
        from models import chat_draft

        return chat_draft.PROVENANCE.set(
            {
                **(extra or {}),
                "created_via": "scheduled" if unattended else "chat",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
            }
        )
    except Exception:  # pragma: no cover — provenance must never block a write
        return None


def _reset_draft_provenance(name: str, token: Optional[object]) -> None:
    if token is None or name not in _DRAFT_TOOLS:
        return
    try:
        from models import chat_draft

        chat_draft.PROVENANCE.reset(token)
    except Exception:  # pragma: no cover
        pass
