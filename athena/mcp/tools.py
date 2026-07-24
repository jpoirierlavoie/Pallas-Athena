"""MCP tool registry, subset JSON-Schema validator, and output helpers.

The registry maps tool names to their metadata and handler name (resolved
lazily against :mod:`mcp.handlers` to avoid a circular import). Every tool
is read-only (``readOnlyHint``) **except the members of** :data:`WRITE_TOOLS`,
which require the ``athena:write`` scope. Every schema sets
``additionalProperties: false``.
"""

import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from mcp import SCOPE_READ, SCOPE_WRITE, write_enabled
from mcp.output_schemas import OUTPUT_SCHEMAS
from tz import to_mtl

# ── Money / date formatting (§10.1 conventions) ─────────────────────────

_NBSP = " "


class ToolArgumentError(Exception):
    """Argument-level failure a handler detects beyond the schema
    (bad date string, mutually exclusive params). Maps to JSON-RPC -32602."""


def format_cents(cents: int) -> str:
    """Integer cents → fr-CA display string, e.g. 1234567 → "12 345,67 $".

    Group separator and the space before ``$`` are U+00A0 (no-break
    space). No locale dependency.
    """
    value = int(cents)
    sign = "-" if value < 0 else ""
    dollars, rem = divmod(abs(value), 100)
    grouped = f"{dollars:,}".replace(",", _NBSP)
    return f"{sign}{grouped},{rem:02d}{_NBSP}$"


def date_str(value: Any) -> Optional[str]:
    """Date-only field (stored midnight UTC) → its UTC calendar date.

    Never route these through ``to_mtl`` — a Montréal conversion shifts a
    midnight-UTC date to the previous day.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def iso_mtl(value: Any) -> Optional[str]:
    """True timestamp → ISO 8601 with offset in America/Montreal."""
    if value is None:
        return None
    if isinstance(value, datetime):
        converted = to_mtl(value)
        return converted.isoformat() if converted else None
    return str(value)


def _jsonable(value: Any) -> Any:
    """Deep-convert a payload to JSON-native types (defensive sweep).

    Handlers pre-serialize their date fields explicitly; any stray
    datetime is a true timestamp and rendered ISO-Montreal.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return iso_mtl(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def tool_result(payload: Any, protocol_version: str) -> dict:
    """Wrap a handler payload in the MCP tools/call result envelope."""
    clean = _jsonable(payload)
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(clean, ensure_ascii=False, indent=2),
            }
        ],
        "isError": False,
    }
    # Lexicographic >= is exact for ISO-dated protocol revisions: when a
    # NEWER revision joins SUPPORTED_PROTOCOL_VERSIONS, its clients must
    # keep receiving structuredContent (an equality gate would silently
    # drop it while outputSchema stays declared — the inverted contract).
    if protocol_version >= "2025-06-18":
        result["structuredContent"] = clean
    return result


def error_result(message: str) -> dict:
    """Tool execution error as an MCP result (not a JSON-RPC error)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ── Subset JSON-Schema validator (§10.2) ────────────────────────────────

def validate_args(schema: dict, args: Any) -> list[str]:
    """Validate a value against a subset JSON Schema; return error strings.

    Supported keywords: ``type`` (object, string, integer, number, boolean,
    array, null — or a LIST of those for nullable fields), ``properties``,
    ``required``, ``enum``, ``minimum``, ``maximum``, ``maxLength``,
    ``minLength``, ``items`` (one level), ``anyOf``,
    ``additionalProperties: false``. Empty list = valid.

    Despite the name, this validates OUTPUT payloads too: the conformance
    tests run every handler and check its real payload against the declared
    ``outputSchema`` with this same validator, so a schema the validator
    cannot express cannot be declared — the contract and its enforcement
    use one grammar.
    """
    return _validate_value(schema, args, "arguments")


def _type_ok(expected: Any, value: Any) -> bool:
    if isinstance(expected, (list, tuple)):
        # JSON Schema union types — used by output schemas for nullable
        # fields (e.g. ["string", "null"]); input schemas stay single-typed.
        return any(_type_ok(e, value) for e in expected)
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    return True


def _validate_value(schema: dict, value: Any, name: str) -> list[str]:
    errors: list[str] = []

    if "anyOf" in schema:
        # Valid when ANY branch accepts the value. Used by output schemas
        # whose payload has several shapes (found/not-found, global/dossier);
        # branches discriminate on an `enum` so a wrong-shape payload cannot
        # accidentally satisfy the other branch.
        #
        # OUTPUT schemas only. Sibling keywords are deliberately ignored on
        # a match (standard JSON Schema applies them conjunctively), so an
        # INPUT schema combining anyOf with `additionalProperties: false`
        # would silently skip that security control — never write one.
        for branch in schema["anyOf"]:
            if not _validate_value(branch, value, name):
                return errors
        errors.append(f"`{name}` matches none of the allowed variants")
        return errors

    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(expected_type, value):
        if isinstance(expected_type, (list, tuple)):
            errors.append(
                f"`{name}` must be one of the types: "
                + ", ".join(str(e) for e in expected_type)
            )
            return errors
        article = "an" if expected_type[0] in "aeiou" else "a"
        if (
            expected_type == "integer"
            and "minimum" in schema
            and "maximum" in schema
        ):
            errors.append(
                f"`{name}` must be an integer between "
                f"{schema['minimum']} and {schema['maximum']}"
            )
        else:
            errors.append(f"`{name}` must be {article} {expected_type}")
        return errors

    if value is None:
        # A null that passed the type gate has nothing further to satisfy
        # (never combined with enum/bounds in this codebase's schemas).
        return errors

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        errors.append(f"`{name}` must be one of: {allowed}")
        return errors

    if isinstance(value, bool):
        return errors

    if isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"`{name}` must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"`{name}` must be <= {schema['maximum']}")

    if isinstance(value, str) and "maxLength" in schema:
        if len(value) > schema["maxLength"]:
            errors.append(
                f"`{name}` must be at most {schema['maxLength']} characters"
            )

    if isinstance(value, str) and "minLength" in schema:
        # Needed by the write tools: an empty title/content otherwise passes
        # the schema and fails deep in the model with a French string that
        # reads to the client model like a server fault.
        if len(value.strip()) < schema["minLength"]:
            errors.append(
                f"`{name}` must be at least {schema['minLength']} "
                "non-whitespace characters"
            )

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(
                _validate_value(schema["items"], item, f"{name}[{index}]")
            )

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"`{key}` is not a supported argument")
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"`{key}` is required")
        for key, subschema in properties.items():
            if key in value:
                errors.extend(_validate_value(subschema, value[key], key))

    return errors


# ── Schema fragments ────────────────────────────────────────────────────

def _limit(default: int) -> dict:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": 50,
        "description": f"Maximum items to return (default {default}, hard max 50).",
    }


def _id(description: str) -> dict:
    """A UUIDv4-id argument with a PER-USAGE description.

    One fresh dict per call. The old shared `_ID` fragment carried one
    description slot for sixteen different usages — which is how all
    sixteen ended up with none at all.
    """
    return {"type": "string", "maxLength": 64, "description": description}


def _date(description: str) -> dict:
    """A YYYY-MM-DD date argument with a per-usage description."""
    return {"type": "string", "maxLength": 10, "description": description}

_READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}
# Per the MCP spec, destructiveHint defaults to TRUE and idempotentHint to
# FALSE once readOnlyHint is false — both must be stated explicitly or the
# client over-warns on a purely additive call.
_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,   # additive only: never overwrites, never deletes
    "idempotentHint": False,    # a second call creates/appends again
    "openWorldHint": False,
}

# The single source of truth for which tools mutate. Enforcement
# (mcp/endpoint.py) and advertisement (list_tool_descriptors) both derive
# from it, so a new write tool cannot ship without declaring itself.
WRITE_TOOLS: frozenset[str] = frozenset({"create_note", "append_to_note"})

# Per-call content ceiling, deliberately far below models.note's
# CONTENT_MAX_LENGTH (100_000). Two reasons: an oversized write is refused
# LOUDLY here (-32602) instead of being silently truncated by
# security.sanitize, and the gap leaves room for several appends before a
# note is full. ~20 000 chars ≈ a 3 500-word memo.
CONTENT_MAX_CHARS = 20_000
NOTE_TITLE_MAX_CHARS = 200

# Copied exactly from models.note.VALID_CATEGORIES (they are French).
# tests/test_mcp_tools.py pins the two lists against each other. Kept as a
# literal (not derived) because importing models.* runs firestore.Client()
# at module load — see models/__init__.py.
_NOTE_CATEGORIES = [
    "rencontre", "consultation", "analyse", "recherche",
    "stratégie", "vacation", "autre",
]

# Enum values copied exactly from the data model (they are French).
_DOSSIER_STATUSES = ["actif", "en_attente", "fermé", "archivé"]
_TASK_STATUSES = ["à_faire", "en_cours", "terminée", "annulée"]
_DOCUMENT_CATEGORIES = [
    "procédure", "pièce", "jugement", "correspondance",
    "déboursé", "facture", "preuve", "procès_verbal",
    "transcription", "mandat", "autre",
]
_CONTACT_ROLES = [
    "client", "partie_adverse", "avocat_adverse", "témoin",
    "expert", "huissier", "notaire", "autre",
]
_PARTIE_TYPES = ["individual", "organization"]


# ── Registry ────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    "get_agenda": {
        "title": "Agenda et priorités",
        "description": (
            "Daily briefing: upcoming hearings, urgent tasks, urgent protocol "
            "steps, prescription alerts within 60 days, and practice-wide stats "
            "(open dossiers, unbilled work, outstanding invoices). Prefer this "
            "as the first call for any \"what's coming up\" question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "description": "Look-ahead window in days (default 14).",
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_agenda",
    },
    "list_dossiers": {
        "title": "Liste des dossiers",
        "description": (
            "List case files (dossiers), optionally filtered by status or a "
            "free-text query matching title, file number, or court file number. "
            "Returns summary rows; use get_dossier for full detail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": "Filter by dossier status. Omit for all.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": ("Free-text match on title, file number "
                                    "and court file number."),
                },
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_dossiers",
    },
    "get_dossier": {
        "title": "Détail d'un dossier",
        "description": (
            "Fetch one dossier by dossier_id or by file_number (provide exactly "
            "one), with the full record — including the free-text `sommaire` "
            "(case summary), court metadata and "
            "the recourse & prescription fields — plus per-module summaries "
            "(tasks, hearings, notes, documents, time, expenses, invoices, "
            "protocol). forum_type is 'judiciaire' (a Québec judicial court, "
            "file number parsed into greffe/juridiction/tribunal), "
            "'administratif' or 'federal' (the body's name is in `tribunal`, "
            "file number stored unparsed), or 'prejudiciaire' (no proceedings "
            "filed yet — only district_judiciaire is set and "
            "court_file_number reads 'Préjudiciaire'). The recourse "
            "is classified by the Québec action "
            "taxonomy: domaine/domaine_label (the family) and action/"
            "action_label/action_precision (the named recourse, e.g. REC-01). "
            "delai is the taxonomy's INDICATIVE delay for that action and "
            "delai_types lists what kind(s) it is — PE prescription "
            "extinctive, PA prescription acquisitive (defensive), D déchéance "
            "stricte (neither suspends nor interrupts), DR déchéance "
            "relevable (statutory relief exists), A avis préalable, R délai "
            "raisonnable, N no delay, I imprescriptible, S follows the "
            "underlying right, V variable, F retrospective window — with "
            "delai_types_label as the joined French label and a_valider "
            "flagging qualifications still to confirm at the sources. avis "
            "lists structured prior-notice obligations (libelle/delai/"
            "sanction/conditionnel); delai_point_depart, ref_delai (source of "
            "the delay) and ref_fondement (seat of the right of action) carry "
            "its starting point and statutory references. Also valeur + "
            "valeur_classe, "
            "prescription_type/prescription_label (the delay the lawyer "
            "confirmed, which may differ from the taxonomy suggestion), "
            "droit_action_date, and prescription_date = the computed « date "
            "pour agir ». Every delay is indicative — the starting point is a "
            "question of fact and interruption/suspension are not computed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier's UUIDv4 id, e.g. from list_dossiers. Provide exactly one of dossier_id or file_number."
                ),
                "file_number": {
                    "type": "string",
                    "maxLength": 20,
                    "description": ("The user-assigned file number, "
                                    "e.g. « 2026-001 ». Alternative to "
                                    "dossier_id — provide exactly one."),
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_dossier",
    },
    "list_tasks": {
        "title": "Liste des tâches",
        "description": (
            "List tasks ordered by due date (undated last). By default only "
            "active tasks (à_faire, en_cours) are returned; pass an explicit "
            "status or include_completed=true to see the rest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Only tasks of this dossier (UUIDv4). Omit for all tasks."
                ),
                "status": {
                    "type": "string",
                    "enum": _TASK_STATUSES,
                    "description": ("Filter to one status (French "
                                    "vocabulary); overrides the default "
                                    "active-only view."),
                },
                "include_completed": {
                    "type": "boolean",
                    "description": ("true also returns terminée and annulée "
                                    "tasks in the default (no-status) view."),
                },
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_tasks",
    },
    "list_hearings": {
        "title": "Liste des audiences",
        "description": (
            "List court hearings and agenda events between two dates (default: "
            "today to +60 days, max span 366 days), optionally scoped to one "
            "dossier. Includes cancelled hearings (status annulée) — check the "
            "status field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": _date(
                    "Window start, YYYY-MM-DD (Montréal calendar date). Default: today."
                ),
                "date_to": _date(
                    "Window end, YYYY-MM-DD inclusive. Default: date_from + 60 days."
                ),
                "dossier_id": _id(
                    "Only hearings of this dossier (UUIDv4). Omit for all."
                ),
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_hearings",
    },
    "list_notes": {
        "title": "Notes d'un dossier",
        "description": (
            "List notes (pinned first, then newest) with a 280-character "
            "plain-text preview. With dossier_id: that dossier's notes. "
            "WITHOUT dossier_id: the « Général » notes — free journal entries "
            "attached to no file. Use get_note for the full Markdown. A note "
            "flagged is_analyse is the dossier's « Théorie de la cause » "
            "(the lawyer's structured case analysis) — readable but "
            "READ-ONLY: never target it with append_to_note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier whose notes to list (UUIDv4). OMIT to list the « Général » notes — journal entries attached to no dossier."
                ),
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_notes",
    },
    "get_note": {
        "title": "Détail d'une note",
        "description": (
            "Fetch one note with its full raw Markdown content. A note "
            "flagged is_analyse (the dossier's « Théorie de la cause ») is "
            "read-only: append_to_note refuses it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id("The note's UUIDv4 id, from list_notes."),
            },
            "required": ["note_id"],
            "additionalProperties": False,
        },
        "handler": "get_note",
    },
    "list_documents": {
        "title": "Documents d'un dossier",
        "description": (
            "List document metadata for a dossier — names, categories, sizes, "
            "versions; never file contents or download links. Optionally filter "
            "by folder, category, or a free-text query over names, description, "
            "and tags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier whose document metadata to list (UUIDv4)."
                ),
                "folder_id": _id(
                    "Restrict to one folder (UUIDv4). Omit to span every folder."
                ),
                "category": {
                    "type": "string",
                    "enum": _DOCUMENT_CATEGORIES,
                    "description": "Filter by document category.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": ("Free-text match on names, description "
                                    "and tags."),
                },
                "limit": _limit(25),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "list_documents",
    },
    "list_parties": {
        "title": "Liste des contacts",
        "description": (
            "List contacts (parties), optionally filtered by contact_role, "
            "type, or a name/email/phone query. Returns summary rows; use "
            "get_partie for the full card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_role": {
                    "type": "string",
                    "enum": _CONTACT_ROLES,
                    "description": ("Filter by the contact's role in "
                                    "the practice."),
                },
                "type": {
                    "type": "string",
                    "enum": _PARTIE_TYPES,
                    "description": ("individual = personne physique; "
                                    "organization = personne morale."),
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Free-text match on name, email and phone.",
                },
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_parties",
    },
    "get_partie": {
        "title": "Fiche d'un contact",
        "description": (
            "Fetch one contact's full card: personal and professional "
            "coordinates, legal identifiers, KYC / conflict-check status, "
            "mandataires, and the dossiers referencing them. KYC and "
            "conflict-check notes may be sensitive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "partie_id": _id(
                    "The contact's UUIDv4 id, from list_parties."
                ),
            },
            "required": ["partie_id"],
            "additionalProperties": False,
        },
        "handler": "get_partie",
    },
    "get_billing_snapshot": {
        "title": "Portrait de facturation",
        "description": (
            "Billing posture. Without dossier_id: firm-wide unbilled totals, "
            "outstanding amount, and the outstanding invoices. With dossier_id: "
            "that dossier's time/expense/invoice summaries plus unbilled line "
            "detail (up to 50 rows each)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Scope to one dossier (UUIDv4). Omit for the "
                    "firm-wide picture."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "get_billing_snapshot",
    },
    "list_protocol_steps": {
        "title": "Étapes du protocole",
        "description": (
            "Case-protocol timeline for a dossier: the active protocol's "
            "ordered steps with deadlines and a derived is_overdue flag. Set "
            "include_history=true to also include prior (completed/suspended) "
            "protocols."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier whose case protocol to read (UUIDv4)."
                ),
                "include_history": {
                    "type": "boolean",
                    "description": ("true also includes completed/suspended past "
                                    "protocols (up to 10)."),
                },
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "list_protocol_steps",
    },
    "compute_judicial_deadline": {
        "title": "Calcul de délai judiciaire",
        "description": (
            "Compute a Quebec judicial deadline under art. 83 C.p.c.: all "
            "calendar days count; when the raw deadline lands on a "
            "non-juridical day (weekend or Quebec statutory holiday) it is "
            "extended in the direction of computation — 'after' pushes later, "
            "'before' pushes earlier — to the nearest juridical day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": _date(
                    "The starting date of the computation, YYYY-MM-DD."
                ),
                "delay_days": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3650,
                    "description": ("Calendar days in the delay — art. 83 C.p.c. "
                                    "counts every day."),
                },
                "direction": {
                    "type": "string",
                    "enum": ["after", "before"],
                    "description": ("'after' counts forward from start_date; "
                                    "'before' counts backward. A non-juridical "
                                    "landing extends in the SAME direction."),
                },
            },
            "required": ["start_date", "delay_days", "direction"],
            "additionalProperties": False,
        },
        "handler": "compute_judicial_deadline",
    },
    "parse_court_file_number": {
        "title": "Analyse d'un numéro de dossier judiciaire",
        "description": (
            "Parse a Quebec court file number (NNN-NN-NNNNNN-NN) into "
            "courthouse (greffe) and jurisdiction metadata: tribunal, "
            "competence, palais de justice, judicial district. Letter-prefixed "
            "numbers (TAL, TAQ…) are flagged administrative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "court_file_number": {
                    "type": "string",
                    "maxLength": 30,
                    "description": ("The raw number, e.g. « 500-05-123456-241 »; a "
                                    "letters prefix (TAL, TAQ…) flags an "
                                    "administrative tribunal."),
                },
            },
            "required": ["court_file_number"],
            "additionalProperties": False,
        },
        "handler": "parse_court_file_number",
    },
    "get_trust_balance": {
        "title": "Solde en fidéicommis d'un dossier",
        "description": (
            "Trust (fidéicommis) balances held for a dossier, per client: book "
            "(the register's balance), cleared (available for disbursement), and "
            "deposits in transit. Amounts in cents plus fr-CA display. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier whose trust balances to read (UUIDv4)."
                ),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "get_trust_balance",
    },
    "list_trust_transactions": {
        "title": "Registre des opérations en fidéicommis",
        "description": (
            "The trust register (journal de caisse). Pass dossier_id AND "
            "client_id together for a carte-client (one beneficiary); pass "
            "neither for the full journal. Optional date range and status. "
            "Amounts in cents; date and cleared_date are date-only (YYYY-MM-DD). "
            "Read-only; never exposes the bank transit or account number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": _id(
                    "Restrict to one trust account (UUIDv4). Omit for all."
                ),
                "dossier_id": _id(
                    "With client_id, selects a carte-client (UUIDv4)."
                ),
                "client_id": _id(
                    "With dossier_id, selects a carte-client — one beneficiary (UUIDv4)."
                ),
                "date_from": _date(
                    "Entries dated on/after this date, YYYY-MM-DD."
                ),
                "date_to": _date(
                    "Entries dated on/before this date, YYYY-MM-DD."
                ),
                "status": {
                    "type": "string",
                    "enum": ["en_circulation", "compensée", "annulée"],
                    "description": ("en_circulation = recorded, not yet cleared; "
                                    "compensée = cleared at the bank; annulée = "
                                    "reversed."),
                },
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_trust_transactions",
    },
    "get_trust_snapshot": {
        "title": "Aperçu des fonds en fidéicommis",
        "description": (
            "Firm-wide trust picture: each account's book and bank balance, "
            "total held, outstanding cheques, deposits in transit, and whether a "
            "bank reconciliation is overdue. Amounts in cents + fr-CA display. "
            "Read-only; never exposes the transit or account number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": "get_trust_snapshot",
    },
    # ── Write tools (require athena:write) ──────────────────────────────
    "create_note": {
        "title": "Créer une note dans un dossier",
        "description": (
            "WRITE. Create a new note — the intended home for research "
            "results, summaries and analyses. With dossier_id it is filed on "
            "that dossier; OMIT dossier_id only for work attached to no file "
            "at all (legal watch, general research), which files it under "
            "« Général ». Never omit it as a fallback because you could not "
            "find the right dossier — an id you supply that does not exist is "
            "refused outright, and that refusal is the signal to go look. "
            "Content is Markdown "
            "in French. The note is permanent: this connector cannot edit or "
            "delete it afterwards, and it syncs to the lawyer's phone. "
            "Confirm with the user before calling, and never call it on a "
            "dossier you have not read with get_dossier first. If the call "
            "appears to fail, check list_notes before retrying — there is no "
            "de-duplication and a retry creates a second note. Raw HTML tags "
            "are rejected (Markdown autolinks like <https://…> are converted "
            "automatically); write plain Markdown. Defaults to category "
            "'recherche'. Every note is stamped with a « Ajouté par Claude » "
            "provenance line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier to file the note on (UUIDv4). OMIT only when the research belongs to no dossier — it is then filed under « Général ». An id that does not resolve is refused, never downgraded."
                ),
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": NOTE_TITLE_MAX_CHARS,
                    "description": "Note title, in French.",
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": CONTENT_MAX_CHARS,
                    "description": (
                        f"Markdown body, in French (max {CONTENT_MAX_CHARS} "
                        "characters)."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _NOTE_CATEGORIES,
                    "description": "Defaults to 'recherche'.",
                },
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
        "handler": "create_note",
        "scope": SCOPE_WRITE,
    },
    "append_to_note": {
        "title": "Ajouter du texte à une note existante",
        "description": (
            "WRITE. Append Markdown to the END of an existing note, under a "
            "dated « Ajouté par Claude » separator. Purely additive: existing "
            "content is never modified or removed, and the append cannot be "
            "undone through this connector. Use get_note first to read what "
            "is already there. If the call appears to fail, re-read the note "
            "with get_note before retrying — a retry appends a second copy. "
            "Fails explicitly (rather than truncating) when the note would "
            "exceed its storage ceiling. Refuses the « Théorie de la cause » "
            "note (is_analyse true in list_notes/get_note) — that analysis "
            "is edited only in the app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id(
                    "The note to append to (UUIDv4), from list_notes."
                ),
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": CONTENT_MAX_CHARS,
                    "description": (
                        f"Markdown to append, in French (max "
                        f"{CONTENT_MAX_CHARS} characters)."
                    ),
                },
            },
            "required": ["note_id", "content"],
            "additionalProperties": False,
        },
        "handler": "append_to_note",
        "scope": SCOPE_WRITE,
    },
}


def required_scope(name: str) -> str:
    """Scope a tool needs. Unlisted tools default to read — never to write."""
    return TOOLS[name].get("scope", SCOPE_READ)


def tool_available(name: str) -> bool:
    """False when a write tool is off via the MCP_WRITE_ENABLED kill switch."""
    return name not in WRITE_TOOLS or write_enabled()


def list_tool_descriptors(granted: Optional[frozenset[str]] = None) -> list[dict]:
    """Registry entries in MCP tools/list wire format, filtered by scope.

    A read-only connection must not see the write tools: advertising them
    would have the client model call one and take a 403 on every attempt,
    and ``_forbidden`` does not feed the failure brake — an unthrottled
    refusal loop. ``granted=None`` means "no filtering" (tests, docs).
    """
    scopes = granted if granted is not None else None
    out = []
    for name, spec in TOOLS.items():
        if not tool_available(name):
            continue
        if scopes is not None and required_scope(name) not in scopes:
            continue
        annotations = (
            _WRITE_ANNOTATIONS if name in WRITE_TOOLS else _READ_ONLY_ANNOTATIONS
        )
        out.append(
            {
                "name": name,
                "title": spec["title"],
                "description": spec["description"],
                "inputSchema": spec["input_schema"],
                # A declared outputSchema is a CONTRACT: structuredContent
                # MUST conform (MCP 2025-06-18). Direct indexing, no .get —
                # a tool without one must fail the registry test, not ship
                # schema-less. Conformance is pinned by
                # tests/test_mcp_output_schemas.py against the REAL handlers.
                "outputSchema": OUTPUT_SCHEMAS[name],
                # `title` moved to the descriptor top level in 2025-06-18;
                # 2025-03-26 clients read the display name from
                # annotations.title. Mirror it so the French titles survive
                # on both protocol revisions.
                "annotations": {**annotations, "title": spec["title"]},
            }
        )
    return out


def get_handler(name: str) -> Callable[[dict], Any]:
    """Resolve a tool's handler function (lazy import breaks the cycle)."""
    from mcp import handlers

    return getattr(handlers, TOOLS[name]["handler"])
