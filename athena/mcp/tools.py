"""MCP tool registry, subset JSON-Schema validator, and output helpers.

The registry maps tool names to their metadata and handler name (resolved
lazily against :mod:`mcp.handlers` to avoid a circular import). Every tool
is read-only (``readOnlyHint``) and every schema sets
``additionalProperties: false``.
"""

import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

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
    if protocol_version == "2025-06-18":
        result["structuredContent"] = clean
    return result


def error_result(message: str) -> dict:
    """Tool execution error as an MCP result (not a JSON-RPC error)."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ── Subset JSON-Schema validator (§10.2) ────────────────────────────────

def validate_args(schema: dict, args: Any) -> list[str]:
    """Validate *args* against a subset JSON Schema; return error strings.

    Supported keywords: ``type`` (object, string, integer, number, boolean,
    array), ``properties``, ``required``, ``enum``, ``minimum``,
    ``maximum``, ``maxLength``, ``items`` (one level),
    ``additionalProperties: false``. Empty list = valid.
    """
    return _validate_value(schema, args, "arguments")


def _type_ok(expected: str, value: Any) -> bool:
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

    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(expected_type, value):
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


_DATE = {"type": "string", "maxLength": 10, "description": "Date as YYYY-MM-DD."}
_ID = {"type": "string", "maxLength": 64}

_READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}

# Enum values copied exactly from the data model (they are French).
_DOSSIER_STATUSES = ["actif", "en_attente", "fermé", "archivé"]
_TASK_STATUSES = ["à_faire", "en_cours", "terminée", "annulée"]
_DOCUMENT_CATEGORIES = [
    "procédure", "pièce", "correspondance", "preuve",
    "jugement", "entente", "note", "autre",
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
                "status": {"type": "string", "enum": _DOSSIER_STATUSES},
                "query": {"type": "string", "maxLength": 120},
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
            "file number parsed into greffe/juridiction/tribunal) or 'autre' "
            "(an administrative tribunal or federal court, whose name is in "
            "`tribunal` and whose file number is stored unparsed). The recourse "
            "is classified by the Québec action "
            "taxonomy: domaine/domaine_label (the family) and action/"
            "action_label/action_precision (the named recourse, e.g. REC-01). "
            "delai is the taxonomy's INDICATIVE delay for that action and "
            "delai_type says what kind it is — P prescription, D déchéance "
            "(neither suspends nor interrupts), A avis préalable; "
            "delai_point_depart and action_references carry its starting point "
            "and statutory references. Also valeur + valeur_classe, "
            "prescription_type/prescription_label (the delay the lawyer "
            "confirmed, which may differ from the taxonomy suggestion), "
            "droit_action_date, and prescription_date = the computed « date "
            "pour agir ». Every delay is indicative — the starting point is a "
            "question of fact and interruption/suspension are not computed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _ID,
                "file_number": {"type": "string", "maxLength": 20},
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
                "dossier_id": _ID,
                "status": {"type": "string", "enum": _TASK_STATUSES},
                "include_completed": {"type": "boolean"},
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
                "date_from": _DATE,
                "date_to": _DATE,
                "dossier_id": _ID,
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_hearings",
    },
    "list_notes": {
        "title": "Notes d'un dossier",
        "description": (
            "List the notes of a dossier (pinned first, then newest) with a "
            "280-character plain-text preview. Use get_note for the full "
            "Markdown content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _ID,
                "limit": _limit(20),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "list_notes",
    },
    "get_note": {
        "title": "Détail d'une note",
        "description": "Fetch one note with its full raw Markdown content.",
        "input_schema": {
            "type": "object",
            "properties": {"note_id": _ID},
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
                "dossier_id": _ID,
                "folder_id": _ID,
                "category": {"type": "string", "enum": _DOCUMENT_CATEGORIES},
                "query": {"type": "string", "maxLength": 120},
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
                "contact_role": {"type": "string", "enum": _CONTACT_ROLES},
                "type": {"type": "string", "enum": _PARTIE_TYPES},
                "query": {"type": "string", "maxLength": 120},
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
            "properties": {"partie_id": _ID},
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
            "properties": {"dossier_id": _ID},
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
                "dossier_id": _ID,
                "include_history": {"type": "boolean"},
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
                "start_date": _DATE,
                "delay_days": {"type": "integer", "minimum": 0, "maximum": 3650},
                "direction": {"type": "string", "enum": ["after", "before"]},
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
                "court_file_number": {"type": "string", "maxLength": 30},
            },
            "required": ["court_file_number"],
            "additionalProperties": False,
        },
        "handler": "parse_court_file_number",
    },
}


def list_tool_descriptors() -> list[dict]:
    """Registry entries in MCP tools/list wire format."""
    return [
        {
            "name": name,
            "title": spec["title"],
            "description": spec["description"],
            "inputSchema": spec["input_schema"],
            "annotations": dict(_READ_ONLY_ANNOTATIONS),
        }
        for name, spec in TOOLS.items()
    ]


def get_handler(name: str) -> Callable[[dict], Any]:
    """Resolve a tool's handler function (lazy import breaks the cycle)."""
    from mcp import handlers

    return getattr(handlers, TOOLS[name]["handler"])
