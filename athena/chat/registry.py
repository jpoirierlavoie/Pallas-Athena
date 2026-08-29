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

The guard-rails for unattended runs are unchanged in KIND, though one
changed in form on 2026-08-27: the charter's write discipline (propose by
DESCRIBING the write, never by calling the tool — `dry_run` was removed
from the protocol), the forced idempotency keys (executors.py), and the
``GATED_TOOLS`` mechanism below.

``GATED_TOOLS`` (SPEC §4.6.3): a ``tool_use`` on a member pauses the turn
into ``awaiting_authorization`` (interactive) or is auto-refused with a
directive to describe the action in the report instead (scheduled). The set ships EMPTY in v1 — the mechanism is
implemented, the policy is the practitioner's to widen, one name at a time.

No capability named « delete » exists, can be registered, or is reachable.
"""

from __future__ import annotations

from typing import Any, Optional

from config import Config
from utils.logging_setup import log_chat_event
from mcp import tools as mcp_tools

from chat import mail_tools
from chat.worker_tools import WORKER_NAME_PREFIXES, WORKER_TOOLS

# Executor identifiers (also the `executor` field of chat_tool_call events).
IN_PROCESS = "in_process"
HTTP_WORKER = "http_worker"
SKILL_FILE = "skill_file"
ANTHROPIC_NATIVE = "anthropic_native"
MAIL = "mail"

# Every mail name, read and write, for execution-time routing. Derived from
# the specs so a new tool cannot be routable-but-unlisted or the reverse.
_MAIL_ALL_NAMES: frozenset[str] = frozenset(mail_tools.all_tool_names())

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
        #
        # get_coverage_report EST REVENU le 2026-08-28. Il avait été coupé
        # avec ce lot au motif du budget de prompt, mais il ne coûte que
        # ~577 jetons — 3 % du tableau — là où les outils de reprise
        # ci-dessus en valent ~1 800 chacun. Et il porte la seule chose que
        # le breffage quotidien ne peut pas dériver autrement : la
        # distinction manquement / signalement, dont deux membres
        # (vérification des conflits, vérification d'identité) sont des
        # obligations déontologiques et non des préférences de saisie.
        # Le retirer revenait à publier chaque matin un parc « propre »
        # sans avoir regardé.
        "list_deletions",
    }
)

# §4.6.3 — requires_authorization. POPULATED 2026-08-28, as the prerequisite
# of the mailbox lot, on the recommendation the 2026-08-26 audit already made
# (« Populate GATED_TOOLS before the first turn that can reach untrusted
# content »). It shipped empty in v1 because the only adversary-authored text
# reaching the loop was a document the lawyer had chosen to upload; a mailbox
# accepts prose from anyone who knows the address, which is a different thing.
#
# DERIVED, not a literal, for the CHAT_WRITE_TOOLS reason: the policy is « a
# tool that REPLACES a stored value, plus the one that is irreversible », and
# a derived set cannot drift from that sentence. EDIT_TOOLS is exactly the
# replacers; import_invoice replaces nothing but flips N sources to
# « facturée », after which both models refuse every modification — the one
# connector gesture no other connector tool can undo.
#
# Note what this does NOT cover, deliberately: the additive creators
# (create_note, create_task…) stay ungated. Gating a creation would put a
# click in front of the assistant's ordinary work for an act the lawyer can
# delete in one gesture.
#
# The audit's second remedy — forcing `dry_run: true` on unattended writes —
# is NOT available: dry_run left the protocol on 2026-08-27, one day after the
# audit was written. This set is therefore the whole of the compensating
# control, which is why it is derived rather than hand-listed.
GATED_TOOLS: frozenset[str] = frozenset(mcp_tools.EDIT_TOOLS) | {"import_invoice"}


def is_gated(name: str) -> bool:
    return name in GATED_TOOLS


# ── La messagerie (lot 2026-08-28) ──────────────────────────────────────────
#
# CHAT-LOCAL: these names never enter mcp.tools.TOOLS, so they are
# structurally unreachable from claude.ai — the Workers' mechanism.
#
# CHAT_LOCAL_WRITE_TOOLS is the sibling CHAT_WRITE_TOOLS cannot be. That one
# is frozenset(mcp_tools.WRITE_TOOLS) and is pinned BY EQUALITY, so no
# chat-local name can ever join it; without this set the kill switch would
# report writes disabled while a mail write kept running.
CHAT_LOCAL_WRITE_TOOLS: frozenset[str] = frozenset(mail_tools.write_tool_names())

_mail_warned = False


def mail_available() -> bool:
    """Whether the mail family is offered at all.

    Says something out loud in exactly ONE case: enabled but unconfigured.
    « Not configured » is the normal, silent state on default and portail;
    « enabled and unconfigured » means the tools vanish from the model's
    array for a reason nobody can see, which is the shape of the
    origin-secret defect this codebase already paid for once.
    """
    global _mail_warned
    if not Config.CHAT_MAIL_ENABLED:
        return False
    if Config.chat_mail_configured():
        return True
    if not _mail_warned:
        _mail_warned = True
        log_chat_event(
            "chat_mail_unavailable",
            "refused",
            reason="mail_enabled_but_unconfigured",
        )
    return False


def mail_writes_enabled() -> bool:
    """Drafting and filing, separably from reading: an incident can withdraw
    the ability to write into Outlook and the dossier while the assistant
    keeps reading."""
    return bool(Config.CHAT_MAIL_DRAFTS_ENABLED) and writes_enabled()


def mail_specs(*, include_writes: Optional[bool] = None) -> tuple[dict, ...]:
    if not mail_available():
        return ()
    writes = mail_writes_enabled() if include_writes is None else (
        include_writes and bool(Config.CHAT_MAIL_DRAFTS_ENABLED)
    )
    return mail_tools.READ_TOOLS + (mail_tools.WRITE_TOOLS if writes else ())


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
    # Execution-time routing, NOT array membership. Absence from the array is
    # never a control: the conversation history replays prior tool_use blocks
    # verbatim, so the model re-names a withheld tool and it would still run
    # (verified on live code — update_dossier is excluded from the array and
    # executor_for still returns in_process for it).
    if name.startswith(mail_tools.MAIL_NAME_PREFIX) and name in _MAIL_ALL_NAMES:
        return MAIL if mail_available() else None
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


def web_search_enabled(*, unattended: bool = False) -> bool:
    """Whether the native web_search block is declared for this turn.

    OFF unconditionally on an unattended turn: a scheduled run has no human
    who could notice a query composed from privileged material, and the
    queries leave the tenant before any code in this repo sees them.
    """
    if unattended:
        return False
    return bool(Config.CHAT_WEB_SEARCH_ENABLED)


def anthropic_tools(
    *, include_writes: Optional[bool] = None, unattended: bool = False
) -> list[dict[str, Any]]:
    """The Messages API ``tools`` array for a turn.

    Internal entries reuse ``TOOLS[name]["input_schema"]`` BY IDENTITY
    (clean subset JSON Schema — no $ref/$schema/format/default, verified);
    the write tools keep their injected ``idempotency_key``
    properties, which ARE the §4.6.2/§12.3 proposal mechanism. Order is
    stable: internal tools in TOOLS order, then Worker tools in declaration
    order, then get_skill_file, then web_search — the trailing
    cache_control breakpoint (chat/vertex.py) covers the whole array only
    because this order never shifts between calls.

    web_search is CONDITIONAL since 2026-08-28 (kill switch, and never on an
    unattended turn). The prefix therefore differs between an interactive and
    a scheduled turn — which costs nothing, because the two never share a
    chain, and within a chain `unattended` is fixed for the turn's life.

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
    for spec in mail_specs(include_writes=include_writes):
        tools.append(
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": spec["input_schema"],
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
    if web_search_enabled(unattended=unattended):
        tools.append(
            {
                "type": WEB_SEARCH_TYPE,
                "name": WEB_SEARCH_NAME,
                "max_uses": Config.CHAT_WEB_SEARCH_MAX_USES,
            }
        )
    return tools
