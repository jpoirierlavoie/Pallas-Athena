"""The chat-side tool registry (Phase N).

One registry maps tool name → executor. Four executors (SPEC §4.1 + the
2026-08-26 reference-files lot):

* ``in_process``       — the MCP tool set, called directly into
                         ``mcp/handlers.py`` (Flask-free, proven). Schemas
                         are referenced from ``mcp.tools.TOOLS`` BY
                         IDENTITY — never copied, so the two surfaces
                         cannot drift.
* ``http_worker``      — the legislation/jurisprudence Workers, which are
                         MCP servers reached by JSON-RPC ``tools/call``
                         (chat/worker_tools.py + worker_client.py). Their
                         specs are GENERATED from the Workers' own
                         ``tools/list``, so a description or a schema here
                         cannot drift from the tool it describes.
                         CHAT-ONLY: these names never enter ``TOOLS`` and
                         never reach the external connector.
* ``skill_file``       — ``get_skill_file``, the progressive-disclosure
                         read of a compétence's reference files
                         (models/chat_skill.py). CHAT-ONLY like the
                         Workers: absent from ``TOOLS`` → structurally
                         unreachable from claude.ai. Resolution goes
                         through THIS turn's pinned ``(skill_id, version)``
                         pairs, never a re-read head.
* ``anthropic_native`` — ``web_search``, declared in the request body and
                         executed by Anthropic server-side (basic version
                         only on Vertex — no dynamic filtering).

Write parity (user decision D9, 2026-08-26), AMENDED 2026-08-27:
``CHAT_WRITE_TOOLS`` is still DERIVED from ``mcp.tools.WRITE_TOOLS``, so
the POLICY cannot drift — but ``CHAT_EXCLUDED_TOOLS`` now withholds part
of that set from the chat's tool array, for prompt budget. The chat can
therefore no longer create a dossier or a contact, while the external
connector still can. This is exactly the « one-line edit, made
consciously » the original note anticipated, and the divergence is
pinned name by name rather than left to be discovered.

The guard-rails for unattended runs are unchanged: the charter's dry_run
discipline, the forced idempotency keys (executors.py), and the
``GATED_TOOLS`` mechanism below.

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
SKILL_FILE = "skill_file"
ANTHROPIC_NATIVE = "anthropic_native"

# The one Anthropic-native tool. Basic web search only on Vertex (verified
# 2026-08-26); its per-search cost lands in usage.server_tool_use.
WEB_SEARCH_NAME = "web_search"
WEB_SEARCH_TYPE = "web_search_20250305"

# The chat-local reference-file read (never in mcp.tools.TOOLS — the
# Workers' isolation mechanism). Offered UNCONDITIONALLY so the tools-array
# prefix never flaps with the skill selection (prompt-cache stability); a
# call with no selected files earns a French refusal, not an error.
GET_SKILL_FILE_NAME = "get_skill_file"
GET_SKILL_FILE_SPEC: dict[str, Any] = {
    "name": GET_SKILL_FILE_NAME,
    "description": (
        "Read ONE reference file of a SELECTED compétence, or of the "
        "charte. The available files are listed in a FICHIERS DE RÉFÉRENCE "
        "section of the system prompt — in the charter's own first block, "
        "and in each COMPÉTENCE block — each giving the skill_id to use. "
        "Read a file only when the current task needs it — the listing "
        "alone is enough to know it exists. Returns the file's raw text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": (
                    "The carrier's id, exactly as given in its FICHIERS DE "
                    "RÉFÉRENCE listing — a compétence's id, or « charte »."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "The file's name as listed (case-insensitive match)."
                ),
            },
        },
        "required": ["skill_id", "filename"],
        "additionalProperties": False,
    },
}

# D9 — full parity with the connector, BY DERIVATION (drift structurally
# impossible; a conscious divergence edits this line).
CHAT_WRITE_TOOLS: frozenset[str] = frozenset(mcp_tools.WRITE_TOOLS)

# Outils que le CLAVARDAGE n'expose pas (2026-08-27, décision du
# praticien). Le connecteur externe les garde tous : cette liste ne
# touche que le tableau d'outils du chat.
#
# Le motif est le budget de prompt, et il est massif : les schémas
# d'outils font ~29 500 jetons, soit 98 % du prompt, RENVOYÉS À CHAQUE
# APPEL DE MODÈLE (pas par tour ni par conversation). Ces quinze-là en
# valent 11 100, et aucun n'est de nature conversationnelle.
#
# Le critère est STRUCTUREL, jamais statistique : au moment de la coupe le
# registre comptait 26 tours et 11 appels d'outil, ce qui ne prouve rien
# sur ce qui sert. On retire ce dont la raison d'être n'est pas une
# conversation — une migration, une passe de nettoyage, un audit — pas ce
# qui n'a pas encore été appelé.
#
# ⚠ Cela DIVERGE de la parité d'écriture D9, à dessein : dix de ces noms
# sont des écritures, et le clavardage ne peut donc plus créer un dossier
# ni un contact. C'est la « édition d'une ligne, faite consciemment » que
# la note D9 ci-dessus prévoyait. La parité subsiste sur le reste, et un
# test l'épingle avec l'écart, nom par nom.
CHAT_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        # Reprise de données historiques (lot Q) — un travail de migration
        # mené depuis claude.ai, pas depuis une conversation. Ce sont
        # aussi les quatre outils les plus coûteux du système.
        "create_partie",
        "update_partie",
        "create_dossier",
        "update_dossier",
        "complete_dossier",
        "import_invoice",
        "find_imported",
        "get_import_audit",
        "get_reference_vocabulary",
        # Reclassement de phase — une passe sur des dizaines de lignes,
        # dont les variantes _bulk (50 par appel) n'ont aucun sens dans un
        # échange.
        "set_time_entry_phase",
        "set_expense_phase",
        "set_time_entry_phase_bulk",
        "set_expense_phase_bulk",
        # Audit et vérification — des rapports qu'on lit à froid.
        "get_coverage_report",
        "list_deletions",
    }
)

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
    if name == GET_SKILL_FILE_NAME:
        return SKILL_FILE
    if name.startswith(WORKER_NAME_PREFIXES) and find_worker_spec(name):
        return HTTP_WORKER
    return None


def chat_tool_names(*, include_writes: Optional[bool] = None) -> list[str]:
    """The in-process tool names the chat offers, in TOOLS order (stable —
    the prompt-cache prefix depends on it)."""
    writes = writes_enabled() if include_writes is None else include_writes
    names = []
    for name in mcp_tools.TOOLS:
        if name in CHAT_EXCLUDED_TOOLS:
            continue
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
    order, then get_skill_file, then web_search — the trailing
    cache_control breakpoint (chat/vertex.py) covers the whole array only
    because this order never shifts between calls.

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
    # get_skill_file sits between the workers and web_search — a FRESH
    # wrapper dict (the schema shared by identity, like the internal
    # entries) so the trailing cache_control stamp never mutates the spec.
    tools.append(
        {
            "name": GET_SKILL_FILE_NAME,
            "description": GET_SKILL_FILE_SPEC["description"],
            "input_schema": GET_SKILL_FILE_SPEC["input_schema"],
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
