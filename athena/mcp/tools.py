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
from mcp import coverage
from mcp.output_schemas import OUTPUT_SCHEMAS
from tz import to_mtl
# Pure module (no Firestore at import) — safe to derive enums from, unlike
# models.* (see the literal-enum comment below).
from utils import phases

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


def _write_protocol_props() -> dict:
    """dry_run + idempotency_key — the shared write protocol (WP15).

    Fresh dicts per usage (module rule). Every write tool splats these
    into its input schema; enforcement lives in mcp/write_support.run_write.
    """
    return {
        "dry_run": {
            "type": "boolean",
            "description": (
                "true = full validation and the computed effect returned, "
                "but NOTHING is written (no note, no sync, no idempotency "
                "record). Use to propose an entry for approval before "
                "committing it."
            ),
        },
        "idempotency_key": {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
            "description": (
                "Caller-chosen key identifying THIS write. Retrying with "
                "the same key returns the first call's stored result "
                "instead of writing twice (kept 24 h); the same key with "
                "different arguments is refused. Recommended on every "
                "unattended/scheduled write."
            ),
        },
    }


def _offset() -> dict:
    """Offset paging for the fully-materialized list tools (G07)."""
    return {
        "type": "integer",
        "minimum": 0,
        "maximum": 5000,
        "description": (
            "Skip this many rows before returning `limit` rows (default "
            "0). Follow next_offset from the previous response. Not "
            "snapshot-stable: the list is re-derived per call, so a row "
            "changed between pages shifts the following ones."
        ),
    }


def _cursor(note: str = "") -> dict:
    """Keyset paging — the recoverable alternative to `offset`.

    Unlike an offset, a cursor names a POSITION IN THE ORDERING, so a row
    inserted between two pages neither skips nor repeats anything.
    """
    return {
        "type": "string",
        "maxLength": 400,
        "description": (
            "Opaque next_cursor from the previous response — resumes right "
            "after the last returned row. Omit for the first page; a "
            "malformed value restarts at page 1. Unlike `offset`, a cursor "
            "is stable across insertions between pages." + (" " + note if note else "")
        ),
    }


def _updated_since() -> dict:
    """The change-window argument of the fully-materialized list tools.

    Deliberately absent from the windowed tools (list_dossiers,
    list_hearings): a filter inside a 200-doc window would silently miss
    older rows touched recently.
    """
    return {
        "type": "string",
        "maxLength": 35,
        "description": (
            "Only rows modified on/after this moment — YYYY-MM-DD "
            "(Montréal calendar day) or a full ISO-8601 timestamp. "
            "Beware: updated_at is noisy (phone syncs and internal "
            "bookkeeping re-stamp it without visible changes), so treat "
            "matches as candidates, not confirmed edits."
        ),
    }

_READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}
# Per the MCP spec, destructiveHint defaults to TRUE and idempotentHint to
# FALSE once readOnlyHint is false — both must be stated explicitly or the
# client over-warns on a purely additive call.
_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,   # additive by default: see EDIT_TOOLS below
    "idempotentHint": False,    # a second call creates/appends again
    "openWorldHint": False,
}

# The single source of truth for which tools mutate. Enforcement
# (mcp/endpoint.py) and advertisement (list_tool_descriptors) both derive
# from it, so a new write tool cannot ship without declaring itself.
WRITE_TOOLS: frozenset[str] = frozenset({
    "create_note", "append_to_note",
    # WP16 — the entity creators (create-only; no delete, no modify).
    "create_task", "create_hearing", "create_time_entry", "create_expense",
    # Lot 3 — the ONLY status change in the connector. Still no delete and
    # no free-form edit: it closes a task, and that is all.
    "complete_task",
    # WP17 — dossier mutators: fill-only-if-empty + append-only recorders.
    "complete_dossier", "record_signification", "record_prescription_event",
    # Lot Q — la reprise de données historiques. Les créateurs restent
    # additifs ; les update_* REMPLACENT une valeur nommée, ce qui les rend
    # destructifs au sens de la spec MCP (voir EDIT_TOOLS).
    "create_partie", "update_partie",
    "create_dossier", "update_dossier",
    "update_time_entry", "update_expense",
    "import_invoice",
    # Reclassement de phase (août 2026) — les SEULES écritures qui passent
    # le mur `invoiced`, et elles ne peuvent toucher que phase/sous_phase :
    # ce couple ne figure sur aucune facture, aucun gabarit, aucun
    # sérialiseur DAV. Les update_* ci-dessus gardent leur refus intact.
    "set_time_entry_phase", "set_expense_phase",
    "set_time_entry_phase_bulk", "set_expense_phase_bulk",
})

# Writes that REPLACE a stored value rather than adding one. Lot Q ended the
# era where destructiveHint could be a family constant: « destructive » in
# the MCP spec means « may perform destructive updates », which is exactly
# what an edit does. Under-warning here is worse than over-warning — a
# client uses the hint to decide whether to confirm with the user first.
# Derived into the annotations below, never restated per tool.
EDIT_TOOLS: frozenset[str] = frozenset({
    "update_partie", "update_dossier",
    "update_time_entry", "update_expense",
    # import_invoice ne remplace aucune valeur, mais il BASCULE N sources à
    # « facturée » — après quoi les deux modèles refusent toute modification
    # ET toute suppression. C'est le seul geste du connecteur qu'aucun autre
    # outil du connecteur ne peut défaire (seule l'application le peut, en
    # annulant la facture), donc sous-avertir ici serait le pire endroit.
    "import_invoice",
    # Un reclassement REMPLACE le code stocké. Qu'il ne puisse pas déplacer
    # un montant ne le rend pas additif : le client se sert de l'indice pour
    # décider s'il confirme, et sous-avertir reste le mauvais côté de
    # l'erreur.
    "set_time_entry_phase", "set_expense_phase",
    "set_time_entry_phase_bulk", "set_expense_phase_bulk",
})

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
# Derived, not hand-copied: mcp.coverage is pure (it imports no model), so
# importing it here costs nothing and the enum can never drift from the
# checks that actually run.
_COVERAGE_CODES = coverage.ALL_CODES
_INVOICE_STATUSES = ["brouillon", "envoyée", "payée", "en_retard", "annulée"]
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

# Lot Q. These four are declared in models/partie.py and NEVER checked by its
# _validate — the web form constrains them with a <select>, the model does
# not. On the connector's path the schema enum is therefore the ONLY guard:
# without it « gender: banana » persists silently onto a vCard.
_PARTIE_PREFIXES = ["Me", "M.", "Mme"]
_PARTIE_LANGUAGES = ["fr", "en", "es"]
_PARTIE_GENDERS = ["M", "F", "O", "N", "U"]
_PARTIE_PRONOUNS = ["il/lui", "elle", "iel", "he/him", "she/her", "they/them"]

# The six keys of ONE address block. They travel together or not at all —
# see _require_address_bloc in the handlers for why a partial block silently
# relocates a contact.
_ADDRESS_KEYS = ("street", "unit", "city", "province", "postal_code", "country")


def _address_props(prefix: str, which: str) -> dict:
    """The six flat address keys of one block, as fresh dicts (module rule)."""
    return {
        f"{prefix}_{key}": {
            "type": "string",
            "maxLength": 200,
            "description": (
                f"{which} address — {key}. The SIX keys of a block must be "
                "supplied together (unit and postal_code may be empty "
                "strings); a partial block would be completed with "
                "Montréal / Québec / Canada defaults."
            ),
        }
        for key in _ADDRESS_KEYS
    }


def _partie_identity_props() -> dict:
    """Identity and contact fields shared by create_partie and update_partie."""
    return {
        "prefix": {
            "type": "string", "enum": _PARTIE_PREFIXES,
            "description": "Civility of a natural person.",
        },
        "first_name": {"type": "string", "maxLength": 200,
                       "description": "Given name (natural person)."},
        "last_name": {"type": "string", "maxLength": 200,
                      "description": "Family name — REQUIRED on an individual."},
        "organization_name": {
            "type": "string", "maxLength": 300,
            "description": (
                "Legal name — REQUIRED on an organization. This is what "
                "display_name returns for a company, never the trade name."
            ),
        },
        "trade_name": {"type": "string", "maxLength": 300,
                       "description": "Trade name / « doing business as »."},
        "governing_law": {"type": "string", "maxLength": 300,
                          "description": "Constituting statute."},
        "language": {"type": "string", "enum": _PARTIE_LANGUAGES,
                     "description": "Correspondence language (vCard LANG)."},
        "gender": {"type": "string", "enum": _PARTIE_GENDERS,
                   "description": "vCard GENDER."},
        "pronouns": {"type": "string", "enum": _PARTIE_PRONOUNS,
                     "description": "vCard X-PRONOUN."},
        "job_title": {"type": "string", "maxLength": 200,
                      "description": "vCard TITLE."},
        "job_role": {"type": "string", "maxLength": 200,
                     "description": "vCard ROLE."},
        "organization": {"type": "string", "maxLength": 300,
                         "description": "Employer (vCard ORG)."},
        "email": {"type": "string", "maxLength": 254,
                  "description": "Personal email; normalised to lowercase."},
        "email_work": {"type": "string", "maxLength": 254,
                       "description": "Work email."},
        "phone_home": {"type": "string", "maxLength": 40,
                       "description": "Normalised to E.164."},
        "phone_cell": {"type": "string", "maxLength": 40,
                       "description": "Normalised to E.164."},
        "phone_work": {"type": "string", "maxLength": 40,
                       "description": "Normalised to E.164."},
        "fax": {"type": "string", "maxLength": 40,
                "description": "Normalised to E.164."},
        "bar_number": {"type": "string", "maxLength": 60,
                       "description": "Barreau number, for a lawyer."},
        "company_neq": {"type": "string", "maxLength": 60,
                        "description": "Québec NEQ, for an organization."},
        "notes": {"type": "string", "maxLength": 2000,
                  "description": "Free-text notes on the contact."},
        **_address_props("address", "Personal"),
        **_address_props("work_address", "Work"),
    }


_PARTY_ROLES = [
    "demandeur", "défendeur", "demandeur reconventionnel",
    "défendeur reconventionnel", "mis en cause", "intervenant",
    "appelant", "intimé", "requérant", "autre",
]


def _party_entry_props() -> dict:
    """One party on a dossier. The connector RESOLVES every id and snapshots
    the names itself — they are what a generated procedure cites."""
    return {
        "type": "object",
        "properties": {
            "partie_id": _id(
                "An EXISTING contact's id. Refused if it does not resolve — "
                "never silently blanked."
            ),
            "roles": {
                "type": "array",
                "items": {"type": "string", "enum": _PARTY_ROLES},
                "description": (
                    "Procedural roles; a party may hold several. An unknown "
                    "role is REFUSED here (the web form drops it silently)."
                ),
            },
            "avocat_partie_id": _id(
                "This party's lawyer, as another contact's id. Optional."
            ),
        },
        "required": ["partie_id"],
        "additionalProperties": False,
    }


def _forum_props() -> dict:
    """Forum fields. The model's normalize_forum reconciles them server-side
    and DISCARDS what does not apply — the handler reports what it dropped."""
    return {
        "forum_type": {
            "type": "string",
            "enum": ["judiciaire", "administratif", "federal", "prejudiciaire"],
            "description": (
                "judiciaire = ordinary court, the file number is parsed. "
                "administratif / federal = the body named by `forum`; the "
                "number is stored unparsed and the district is cleared. "
                "prejudiciaire = nothing filed; the file number is FORCED to "
                "« Préjudiciaire »."
            ),
        },
        "forum": {
            "type": "string", "maxLength": 40,
            "description": (
                "Body slug for administratif/federal — from "
                "get_reference_vocabulary(kind=\"forums\"). A slug from the "
                "wrong category is refused."
            ),
        },
        "district_judiciaire": {
            "type": "string", "maxLength": 60,
            "description": (
                "Judicial district. Discarded for an administrative or "
                "federal forum, with a warning saying so."
            ),
        },
    }


def _dossier_field_props() -> dict:
    """The classification/financial block shared by complete_dossier,
    create_dossier and update_dossier — one definition, three tools."""
    return {
        "domaine": {"type": "string", "maxLength": 10,
                    "description": "Taxonomy family — get_reference_vocabulary."},
        "action": {"type": "string", "maxLength": 10,
                   "description": "Named recourse; its prefix MUST equal domaine."},
        "action_precision": {"type": "string", "maxLength": 2000,
                             "description": "Free text; required by « Autre » rows."},
        "sommaire": {"type": "string", "maxLength": 5000,
                     "description": "Free-text case summary (stored up to 5000)."},
        "mandate_type": {"type": "string", "maxLength": 40,
                         "description": "judiciaire | service_conseils | general | special."},
        "court_file_number": {
            "type": "string", "maxLength": 40,
            "description": (
                "e.g. « 500-05-123456-241 ». On a judiciaire forum the "
                "greffe, juridiction, tribunal and district are DERIVED from "
                "it."
            ),
        },
        "prescription_type": {"type": "string", "maxLength": 40,
                              "description": "Delay key — get_reference_vocabulary."},
        "fee_type": {"type": "string", "maxLength": 30,
                     "description": "hourly | flat | contingency | mixed | pro_bono | aide_juridique."},
        "fee_notes": {"type": "string", "maxLength": 2000,
                      "description": "Free text on the fee arrangement."},
        "valeur": {"type": "integer", "minimum": 1, "maximum": 100000000000,
                   "description": "Amount in dispute, integer cents."},
        "hourly_rate": {
            "type": "integer", "minimum": 0, "maximum": 100000000,
            "description": (
                "Integer cents. 0 is REAL (pro bono, aide juridique) — set it "
                "on a historical file, because create_time_entry defaults "
                "each entry's rate to this value."
            ),
        },
        "flat_fee": {"type": "integer", "minimum": 1, "maximum": 100000000000,
                     "description": "Flat fee, integer cents."},
        "contingency_percent": {
            "type": "integer", "minimum": 1, "maximum": 10000,
            "description": "BASIS POINTS: 2500 = 25,00 %.",
        },
        "droit_action_date": _date("« Droit d'action » start, YYYY-MM-DD."),
        "date_avis": _date("Confirmed avis préalable date, YYYY-MM-DD."),
        "prise_action_date": _date("Interruptive act filed, YYYY-MM-DD."),
        "prescription_notes": {
            "type": "string", "maxLength": 2000,
            "description": (
                "Free-text notes on the limitation analysis. A real dossier "
                "field the connector could not reach before this lot."
            ),
        },
    }


def _legacy_ref_prop() -> dict:
    return {
        "legacy_ref": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "This record's identifier in the PREVIOUS system. Stored so "
                "find_imported can retrieve it later: the idempotency window "
                "is 24 h and an import runs for days. Refused if another "
                "record of the same kind already bears it."
            ),
        }
    }

# WP16 write-tool enums — literals for the same firestore-at-import reason,
# each pinned against its model by tests/test_mcp_tools.py.
_TASK_PRIORITIES = ["haute", "normale", "basse"]
_TASK_CATEGORIES = [
    "rédaction", "recherche", "correspondance", "dépôt",
    "signification", "suivi", "admin", "autre",
]
_HEARING_TYPES = [
    # judiciaire tier…
    "conférence_de_gestion", "conférence_de_règlement",
    "conférence_préparatoire", "audience", "instruction",
    # …extrajudiciaire tier (forum derives from the type)
    "consultation", "rencontre", "conférence", "interrogatoire", "autre",
]
_EXPENSE_CATEGORIES = [
    "signification", "expertise", "transcription", "deplacement",
    "photocopie", "timbre_judiciaire", "autre",
]

# Phase O — DERIVED, not hand-copied (the _COVERAGE_CODES precedent):
# utils/phases.py is pure (no model import, no Firestore at load), so the
# enum can never drift from the vocabulary the models validate against.
# "" is deliberately EXCLUDED from the input enums: an MCP caller either
# phases the entry or omits the parameter — passing "" would be noise.
_PHASE_CODES = [c for c in phases.VALID_PHASES if c]
_SOUS_PHASE_CODES = [c for c in phases.VALID_SOUS_PHASES if c]

# The optional phase pair shared by the three phased write tools.
_PHASE_DESCRIPTION = (
    "Code de phase du litige (axe 1 — ex. « CTS » Contestation, « PRE » "
    "Préjudiciaire, « ADM » Administration). Optionnel : omis = non "
    "renseignée. Indépendant de `category` (nature du travail). Si seul "
    "`sous_phase` est fourni, la phase parente est déduite du préfixe."
)
_SOUS_PHASE_DESCRIPTION = (
    "Sous-code complet de la phase (ex. « CTS-02 » Demande "
    "reconventionnelle). Optionnel : une phase sans sous-code impute au "
    "« -00 » (Général) de cette phase. Le préfixe doit concorder avec "
    "`phase` si les deux sont fournis."
)


def _phase_props() -> dict:
    return {
        "phase": {
            "type": "string",
            "enum": _PHASE_CODES,
            "description": _PHASE_DESCRIPTION,
        },
        "sous_phase": {
            "type": "string",
            "enum": _SOUS_PHASE_CODES,
            "description": _SOUS_PHASE_DESCRIPTION,
        },
    }


# Items per bulk reclassification call. Sized in the MAX_ZIP_FILES tradition
# — against gunicorn's 60 s SIGKILL, not against a round number: one batched
# read plus at most 50 serialized single-key updates is a few seconds. It
# also equals one `list_time_entries` page, so the read and write cadences
# line up. `minItems`/`maxItems` below are declared for the CLIENT's benefit;
# this module's subset validator does not implement them (as with
# `multipleOf` on hours), so the real enforcement is in the handler.
PHASE_BULK_MAX = 50


def _phase_bulk_items(id_key: str, id_description: str) -> dict:
    """The `entries` array of a bulk phase reclassification."""
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": PHASE_BULK_MAX,
        "description": (
            f"The rows to reclassify, 1 to {PHASE_BULK_MAX} per call. Each "
            "item carries its own phase code; an item naming neither `phase` "
            "nor `sous_phase` is refused (unlike the correction tools, "
            "omitting a code here would mean nothing)."
        ),
        "items": {
            "type": "object",
            "properties": {
                id_key: _id(id_description),
                **_phase_props(),
            },
            "required": [id_key],
            "additionalProperties": False,
        },
    }


# ── Registry ────────────────────────────────────────────────────────────

TOOLS: dict[str, dict] = {
    "get_agenda": {
        "title": "Agenda et priorités",
        "description": (
            "Daily briefing: upcoming hearings, urgent tasks, urgent protocol "
            "steps, prescription alerts within 60 days, and practice-wide stats "
            "(open dossiers, unbilled work, outstanding invoices). Prefer this "
            "as the first call for any \"what's coming up\" question. The "
            "window opens at MIDNIGHT, Montréal time, so a hearing earlier "
            "today is still listed; every overdue flag in the response uses "
            "that same Montréal day, so window.from and is_overdue can never "
            "disagree. In "
            "prescription alerts, last_action_date is the last juridical day "
            "ON OR BEFORE the deadline (inclusive — it equals "
            "prescription_date on a business-day deadline; check "
            "last_action_differs), never the date an action was taken."
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
            "free-text query matching title, file number, court file number "
            "and the sommaire (case summary). Returns summary rows, newest "
            "opened first; use get_dossier for full detail. Paginate with "
            "next_cursor: pass it back as `cursor` until it comes back "
            "null."
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
                    "description": ("Free-text match on title, file number, "
                                    "court file number and sommaire."),
                },
                "cursor": {
                    "type": "string",
                    "maxLength": 400,
                    "description": (
                        "Opaque next_cursor from the previous response — "
                        "resumes right after the last returned row. Omit "
                        "for the first page; a malformed value restarts "
                        "at page 1."
                    ),
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
            "protocol). In summaries.protocol, `upcoming` counts open steps "
            "due within `upcoming_window_days` (7) calendar days — NOT all "
            "future steps; `next_deadline_date` is the nearest open deadline "
            "regardless of window, and a step due today is upcoming, never "
            "overdue. forum_type is 'judiciaire' (a Québec judicial court, "
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
                    "description": (
                        "The user-assigned file number, e.g. « 2026-001 ». "
                        "Alternative to dossier_id — provide exactly one. "
                        "Matched EXACTLY (whitespace trimmed), so « 2026-1 » "
                        "does not find « 2026-001 »: found: false means no "
                        "dossier bears this exact number, not that the file "
                        "is absent under some other spelling. Unlike the "
                        "dossier_id branch, a failed lookup reports an error "
                        "rather than found: false — so « not found » here is "
                        "a fact you can act on."
                    ),
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
            "status or include_completed=true to see the rest. Every row "
            "carries is_overdue, computed against the MONTRÉAL calendar day: "
            "a task due TODAY is not overdue, an undated one never is, and a "
            "terminée/annulée one never is whatever its due date says."
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
                "updated_since": _updated_since(),
                "offset": _offset(),
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
            "status field. Each row carries the derived forum "
            "(judiciaire/extrajudiciaire), the modalité, and a conference_uri "
            "for video events."
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
                "cursor": _cursor(
                    "Hearings page OLDEST-first (agenda order), so the "
                    "cursor advances forward in time."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_hearings",
    },
    "list_notes": {
        "title": "Notes (dossier ou cabinet)",
        "description": (
            "List notes with a 280-character plain-text preview. "
            "CHOOSE THE CORPUS FIRST — the default is NARROW: with no "
            "dossier_id and no scope you get ONLY the « Général » notes "
            "(entries attached to no file), NOT the firm. To search every "
            "dossier — the way to find a note whose file you have forgotten "
            "— pass scope=\"cabinet\". With dossier_id you get that one "
            "file. Every row carries its dossier_id/file number/title, so a "
            "cabinet hit is attributable without a second call. "
            "`query` searches the FULL title and "
            "content, so a match may sit past the preview — fetch the note "
            "before concluding it is irrelevant. Use get_note for the full "
            "Markdown. A note flagged is_analyse is the dossier's « Théorie "
            "de la cause » (the lawyer's structured case analysis) — "
            "readable but READ-ONLY: never target it with append_to_note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["general", "dossier", "cabinet"],
                    "description": (
                        "WHICH CORPUS to search. \"general\" (default) = only "
                        "notes attached to NO dossier. \"dossier\" = one file "
                        "(implicit whenever dossier_id is given). "
                        "\"cabinet\" = EVERY note in the firm. Contradictory "
                        "combinations are refused, never silently resolved."
                    ),
                },
                "dossier_id": _id(
                    "The dossier whose notes to list (UUIDv4). Implies "
                    "scope=\"dossier\". Omit for the « Général » notes, or "
                    "pass scope=\"cabinet\" to search every dossier."
                ),
                "dossier_status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Cabinet scope ONLY: keep notes whose dossier carries "
                        "this status (research usually targets open files)."
                    ),
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": (
                        "Case-insensitive substring over each note's title "
                        "AND full content (not just the preview)."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _NOTE_CATEGORIES,
                    "description": "Filter to one note category.",
                },
                "pinned": {
                    "type": "boolean",
                    "description": (
                        "true = pinned notes only; false = unpinned only. "
                        "Omit for both."
                    ),
                },
                "date_from": _date(
                    "Earliest creation date (Montréal calendar), "
                    "YYYY-MM-DD inclusive."
                ),
                "date_to": _date(
                    "Latest creation date (Montréal calendar), "
                    "YYYY-MM-DD inclusive."
                ),
                "updated_since": _updated_since(),
                "offset": _offset(),
                "cursor": _cursor(
                    "Cabinet scope ONLY (other scopes page with offset). "
                    "Cabinet orders by creation date + id — immutable fields, "
                    "so pinning a note between pages cannot shift it."
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
        "title": "Documents (dossier ou cabinet)",
        "description": (
            "List document METADATA — names, categories, sizes, versions; "
            "never file contents or download links. `query` matches METADATA "
            "ONLY (display name, filename, description, tags) and NEVER the "
            "text inside the file; searching document contents is not "
            "available through this connector. "
            "Scope: one dossier by default (dossier_id required), or "
            "scope=\"cabinet\" to search document metadata across every "
            "dossier — each row then carries its dossier_id/file "
            "number/title, and folder_path is \"\" (resolving folder "
            "breadcrumbs firm-wide would cost one query per dossier). "
            "Optionally filter "
            "by folder, category, a free-text query over names, description "
            "and tags, or a date window. Each row carries folder_path "
            "(resolved; \"\" = dossier root) and document_date — the "
            "document's OWN date when the lawyer entered one (null "
            "otherwise; created_at is only the upload instant, often days "
            "after the event on scanned papers)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["dossier", "cabinet"],
                    "description": (
                        "\"dossier\" (default) = one file, and dossier_id is "
                        "REQUIRED. \"cabinet\" = every dossier; dossier_id "
                        "and folder_id are then refused as contradictory."
                    ),
                },
                "dossier_id": _id(
                    "The dossier whose document metadata to list (UUIDv4). "
                    "Required unless scope=\"cabinet\"."
                ),
                "dossier_status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Cabinet scope ONLY: keep documents whose dossier "
                        "carries this status."
                    ),
                },
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
                "date_from": _date(
                    "Earliest EFFECTIVE date, YYYY-MM-DD inclusive — the "
                    "document's own document_date when set, else its "
                    "upload date."
                ),
                "date_to": _date(
                    "Latest effective date, YYYY-MM-DD inclusive."
                ),
                "updated_since": _updated_since(),
                "offset": _offset(),
                "cursor": _cursor(
                    "Cabinet scope ONLY (dossier scope pages with offset)."
                ),
                "limit": _limit(25),
            },
            # `dossier_id` is deliberately NOT in `required` any more: cabinet
            # scope has no dossier. Relaxing `required` is additive on the
            # wire (a schema that demands less accepts strictly more), and the
            # HANDLER re-imposes it outside cabinet scope — so an omitted
            # dossier_id still fails loudly instead of silently becoming a
            # firm-wide scan.
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
                "updated_since": _updated_since(),
                "offset": _offset(),
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
            "Billing posture. Without dossier_id: firm-wide unbilled totals "
            "— fees AND disbursements (unbilled_expenses) — the outstanding "
            "amount and invoices, plus by_dossier: which files hold the "
            "work in progress (hours, fees, disbursements per dossier). "
            "With dossier_id: that dossier's time/expense/invoice summaries "
            "plus unbilled line detail (up to 50 rows each). Note "
            "total_hours counts ALL time incl. non-billable; unbilled "
            "figures are billable-and-not-yet-invoiced only."
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
    "list_time_entries": {
        "title": "Entrées de temps",
        "description": (
            "Time entries firm-wide or per dossier — billed AND unbilled "
            "(the billing snapshot lists unbilled rows only; this is the "
            "work-history view, and the only way to see invoiced time). "
            "Sorted newest date first. billable_filter='non_facture' means "
            "NOT YET INVOICED — it includes non-billable rows, whose amount "
            "is always 0; 'billable' filters to billable time regardless of "
            "invoicing. Combine dossier_id + date range to answer « what "
            "was done on this file in July »."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Restrict to one dossier (UUIDv4). Omit for firm-wide."
                ),
                "date_from": _date(
                    "Earliest entry date, YYYY-MM-DD inclusive."
                ),
                "date_to": _date(
                    "Latest entry date, YYYY-MM-DD inclusive."
                ),
                "billable_filter": {
                    "type": "string",
                    "enum": ["billable", "non_facture"],
                    "description": (
                        "'billable' = billable time only; 'non_facture' = "
                        "not yet invoiced (includes non-billable rows). "
                        "Omit for everything."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Required to walk a full month or exercise: `truncated` "
                    "warns there is more, and only this resumes it."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_time_entries",
    },
    "list_expenses": {
        "title": "Déboursés",
        "description": (
            "Disbursements (débours) firm-wide or per dossier — billed AND "
            "unbilled, sorted newest date first. "
            "billable_filter='non_facture' keeps only those not yet "
            "invoiced. Categories are French keys (signification, "
            "expertise, transcription, deplacement, photocopie, "
            "timbre_judiciaire, autre)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Restrict to one dossier (UUIDv4). Omit for firm-wide."
                ),
                "date_from": _date(
                    "Earliest expense date, YYYY-MM-DD inclusive."
                ),
                "date_to": _date(
                    "Latest expense date, YYYY-MM-DD inclusive."
                ),
                "billable_filter": {
                    "type": "string",
                    "enum": ["non_facture"],
                    "description": (
                        "'non_facture' = not yet invoiced. Omit for "
                        "everything."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor(
                    "Required to walk a full month or exercise: `truncated` "
                    "warns there is more, and only this resumes it."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_expenses",
    },
    "list_invoices": {
        "title": "Registre des factures",
        "description": (
            "The invoice register, newest date first. "
            "WITHOUT a status filter this returns EVERY status, including "
            "`brouillon` (drafted, never sent to the client) and `annulée` "
            "(void) — neither is money owed, and neither may be presented as "
            "an issued invoice. Filter with `status`, or with "
            "`status_group=\"impayée\"` for what is actually outstanding. "
            "Resolves the invoice_id carried by list_time_entries and "
            "list_expenses rows. "
            "PAYMENT IS ONLY AS RECORDED: `amount_paid` is the sum posted in "
            "the accounting module (the only writer of a payment), "
            "`balance` is amount_due − "
            "amount_paid, and `payment_basis: \"none\"` means nothing was "
            "recorded — NOT that nothing was paid. Older invoices predate "
            "the payment field and read that way. "
            "`amount_due` is the balance AT ISSUANCE and is never updated: "
            "it stays non-zero on a paid invoice, so never read it as what "
            "is still owed. "
            "Reconciliation: summing `amount_due` over status envoyée + "
            "en_retard (i.e. status_group=\"impayée\") equals "
            "get_billing_snapshot's outstanding_cents to the cent — the two "
            "share one definition — but only ACROSS ALL PAGES. While "
            "`truncated` is true the sum you hold is partial; page to the "
            "end with `cursor`, or quote get_billing_snapshot's figure "
            "instead of adding these up.Beware the DIFFERENT one in "
            "get_dossier.summaries.invoices.total_outstanding, which sums "
            "`total`, counts brouillons and treats payée as settled; the two "
            "figures will not agree, by design. "
            "Exact at any size firm-wide and for any single dossier; page "
            "with `cursor`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "Restrict to one dossier (UUIDv4). Omit for firm-wide."
                ),
                "status": {
                    "type": "string",
                    "enum": _INVOICE_STATUSES,
                    "description": (
                        "One exact status. Mutually exclusive with "
                        "status_group."
                    ),
                },
                "status_group": {
                    "type": "string",
                    "enum": ["impayée"],
                    "description": (
                        "\"impayée\" = envoyée + en_retard, the same pair "
                        "get_billing_snapshot sums. Mutually exclusive with "
                        "status."
                    ),
                },
                "date_from": _date("Earliest invoice date, YYYY-MM-DD inclusive."),
                "date_to": _date("Latest invoice date, YYYY-MM-DD inclusive."),
                "limit": _limit(25),
                "cursor": _cursor(),
            },
            "additionalProperties": False,
        },
        "handler": "list_invoices",
    },
    "get_invoice": {
        "title": "Facture",
        "description": (
            "One invoice: its parties, its full money block (fees, "
            "disbursements, GST, QST, retainer, total, recorded payment and "
            "live balance) and its line items. "
            "Line descriptions are what PRINTED on the client's invoice — "
            "quote them verbatim, never paraphrase. "
            "`subtotal_matches_line_items: false` means the stored subtotal "
            "and the sum of the lines disagree: raise it, never silently "
            "re-add. A non-empty `warnings` array means the line items could "
            "not be read — the invoice is not empty, the read failed. "
            "Line items are readable ONE INVOICE AT A TIME; there is no way "
            "to search them across invoices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": _id("The invoice to read (UUIDv4)."),
            },
            "required": ["invoice_id"],
            "additionalProperties": False,
        },
        "handler": "get_invoice",
    },
    "get_coverage_report": {
        "title": "Rapport de couverture",
        "description": (
            "Hygiene sweep across the open files, in ONE call instead of one "
            "get_dossier per dossier: which files are missing a protocol, a "
            "signification, a court file number, a conflict check. "
            "Two severities: `manquement` = something the file is REQUIRED "
            "to have (the conflict-of-interest and identity checks are "
            "regulatory obligations, not data-entry preferences); "
            "`signalement` = worth a look, not a breach. Codes are stable "
            "across runs, so a file can be tracked from one sweep to the "
            "next. "
            "EVERY FINDING IS AN OBSERVATION, never an instruction: this "
            "connector cannot create a protocol, verify an identity or file "
            "a signification, and each `detail` says what to do in the "
            "application. "
            "ALWAYS read `scope.checks_skipped` and `data_completeness` "
            "before reporting a file as clean: when the protocol index or "
            "the client contacts cannot be read, those checks are SUPPRESSED "
            "rather than fired — a client is never called unverified because "
            "a read failed, and a shortened report is not a clean one. "
            "`cross_scope_findings` carries findings on CLOSED dossiers "
            "(a task still open on a closed file), which the status filter "
            "could never surface."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Dossier status to sweep; default \"actif\"."
                    ),
                },
                "checks": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_COVERAGE_CODES)},
                    "description": (
                        "Restrict the sweep to these codes. Omit for all — "
                        "anything left out is listed in checks_skipped."
                    ),
                },
                "limit": _limit(25),
                "cursor": _cursor("Items page by file number."),
            },
            "additionalProperties": False,
        },
        "handler": "get_coverage_report",
    },
    "get_reference_vocabulary": {
        "title": "Vocabulaires de référence",
        "description": (
            "Enumerate a controlled vocabulary the models VALIDATE but never "
            "spell out when they refuse — « Domaine invalide. » names no "
            "valid domaine. Call this BEFORE writing a dossier's "
            "classification rather than guessing a code. "
            "kind='domaines' (20 families), 'actions' (162 named recourses; "
            "pass `domaine` to narrow — the code prefix MUST equal the "
            "domaine), 'prescription_types' (the delay dropdown), 'forums' "
            "(non-judicial bodies: Québec administrative tribunals and "
            "federal courts, for forum_type administratif/federal), "
            "'districts' (judicial districts), 'phases' (litigation phase "
            "codes and their sub-codes). "
            "`note` carries whatever qualifies that row: for an action it is "
            "the taxonomy's INDICATIVE delay — a suggestion the lawyer "
            "confirms, never a computed deadline, and often empty because "
            "the source has no single clean period. Pure reference data: no "
            "dossier, no client, nothing personal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "domaines", "actions", "prescription_types",
                        "forums", "districts", "phases",
                    ],
                    "description": "Which vocabulary to enumerate.",
                },
                "domaine": {
                    "type": "string",
                    "maxLength": 10,
                    "description": (
                        "Narrow `actions` to one family, e.g. « REC ». "
                        "Refused with any other kind."
                    ),
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        "handler": "get_reference_vocabulary",
    },
    "find_imported": {
        "title": "Retrouver un enregistrement importé",
        "description": (
            "Find what a historical import already wrote, by the identifier "
            "it carried in the PREVIOUS system (`legacy_ref`). This is the "
            "durable duplicate guard: an idempotency_key expires after 24 h "
            "and an import runs for days, so before creating anything for a "
            "spreadsheet row, look the row's own reference up here. "
            "Searches contacts, dossiers, time entries, disbursements and "
            "invoices at once; pass `entity_type` to narrow. An empty "
            "`matches` means nothing bearing that reference exists — a fact "
            "you can act on, because a failed lookup reports an error "
            "instead. Nothing in this connector can delete a duplicate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "legacy_ref": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "The previous system's own identifier for this "
                        "record — a row number, a file reference, an invoice "
                        "number. Whatever convention you adopt, keep it "
                        "stable for the whole import."
                    ),
                },
                "entity_type": {
                    "type": "string",
                    "enum": [
                        "partie", "dossier", "time_entry", "expense", "invoice",
                    ],
                    "description": "Restrict the search to one kind of record.",
                },
            },
            "required": ["legacy_ref"],
            "additionalProperties": False,
        },
        "handler": "find_imported",
    },
    "get_import_audit": {
        "title": "Vérifier l'import d'un dossier",
        "description": (
            "Reconcile ONE dossier after a historical import: its work, its "
            "disbursements and its invoices checked against each other. Run "
            "it after every file, before moving to the next spreadsheet row. "
            "Findings are OBSERVATIONS, never instructions — this connector "
            "cannot delete a duplicate entry, cannot void an invoice and "
            "cannot move one out of brouillon; every detail says what to do "
            "in the application. "
            "IMP-01 unbilled work on a closed dossier (the signature of an "
            "interrupted import). IMP-02 stored subtotal ≠ sum of line "
            "items. IMP-03 a line item citing a source that no longer "
            "exists. IMP-04 possible duplicate entries — same date, "
            "description and amount, which is exactly what re-running an "
            "import after the 24 h idempotency window produces. IMP-05 a "
            "closed dossier with no closing date. IMP-06 an entry marked "
            "invoiced whose invoice is missing. IMP-07 an imported invoice "
            "still in brouillon. "
            "`checks_skipped` names checks NOT run because the sources could "
            "not be read completely — a shortened report must never pass for "
            "a clean one, and a paging boundary must never be reported as a "
            "missing source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier to reconcile. Provide exactly one of "
                    "dossier_id or file_number."
                ),
                "file_number": {
                    "type": "string",
                    "maxLength": 20,
                    "description": (
                        "The file number, e.g. « 2019-014 ». Matched exactly. "
                        "Alternative to dossier_id — provide exactly one."
                    ),
                },
            },
            "additionalProperties": False,
        },
        "handler": "get_import_audit",
    },
    "list_deletions": {
        "title": "Journal des suppressions",
        "description": (
            "The append-only deletion trail, newest first: what was "
            "deleted, when, and the minimal snapshot (title + status) it "
            "carried. Use it when something that used to appear has "
            "vanished — it distinguishes « deleted » from « never "
            "existed ». Two honest limits: the trail starts at its own "
            "deployment (silence about anything earlier), and the read "
            "window is the 200 most recent events — an empty answer past "
            "that means « not in the recent window », never « never "
            "deleted »."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    # Kept in step with models/audit_event.VALID_ENTITY_TYPES
                    # (hand-mirrored — the models/* import ban). Additive
                    # input-enum growth is safe; the OUTPUT schema types
                    # entity_type as a plain string, so new values never
                    # violate the structuredContent contract.
                    "enum": [
                        "task", "hearing", "note", "document", "expense",
                        "time_entry", "invoice", "partie", "protocol",
                        "protocol_step", "folder", "doc_template",
                        "dossier", "admin_transaction", "hearing_series",
                    ],
                    "description": "Filter to one entity type.",
                },
                "dossier_id": _id(
                    "Only deletions on this dossier (UUIDv4)."
                ),
                "date_from": _date(
                    "Earliest deletion date (Montréal calendar), "
                    "YYYY-MM-DD inclusive."
                ),
                "limit": _limit(25),
            },
            "additionalProperties": False,
        },
        "handler": "list_deletions",
    },
    "list_protocol_steps": {
        "title": "Étapes du protocole",
        "description": (
            "Case-protocol timeline for a dossier: the active protocol's "
            "ordered steps with deadlines. A step's `status` is DERIVED here "
            "from its deadline against today (Montréal) and is the value that "
            "governs; `status_stored` is the word on the document, kept for "
            "provenance only. The stored word is written solely when the "
            "lawyer opens the protocol page in a browser and an « en_retard » "
            "there is never cleared, so it can lag reality indefinitely — "
            "`status_differs: true` marks exactly that. `is_overdue` is "
            "equivalent to `status == \"en_retard\"`; both come from one "
            "predicate and can never contradict each other. Set "
            "include_history=true to also include prior (completed/suspended) "
            "protocols. Check regime_mismatch on every protocol: true means "
            "the template's C.p.c. regime does not govern the dossier's "
            "forum (e.g. a Cour du Québec simplified-track template — arts. "
            "535.x — on a Superior Court file), so its tracked deadlines "
            "are suspect and must be raised, not relied on."
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
            "The trust register (journal de caisse), NEWEST movements "
            "first. Pass dossier_id AND "
            "client_id together for a carte-client (one beneficiary); pass "
            "neither for the full journal. Optional date range and status. "
            "PAGING IS ASYMMETRIC, and the difference matters. A bare "
            "account_id (no dossier_id/client_id/status/date filter) rides "
            "the newest-first index: it is exact at any register size and "
            "`cursor` walks the whole register. EVERY OTHER SHAPE reads a "
            "bounded window ordered OLDEST-first and re-sorts it here, and "
            "returns next_cursor: null — so on a register longer than that "
            "window the rows shown are NOT the most recent ones, even though "
            "they are displayed newest-first. When `truncated` is true on a "
            "filtered call, narrow with date_from/date_to, or drop the "
            "filters and pass account_id alone. "
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
                "cursor": _cursor(
                    "Honoured ONLY with a bare account_id; every other "
                    "shape returns next_cursor: null (see the description)."
                ),
            },
            "additionalProperties": False,
        },
        "handler": "list_trust_transactions",
    },
    "get_trust_snapshot": {
        "title": "Aperçu des fonds en fidéicommis",
        "description": (
            "Firm-wide trust picture: each account's book and bank balance "
            "with its OWN reconciliation state (last completed period, "
            "never_reconciled, overdue), total held, the outstanding cheques "
            "LISTED with their issue dates (stale-cheque monitoring), "
            "deposits in transit, and by_dossier — which files hold trust "
            "money (book vs cleared per dossier; per-client detail via "
            "get_trust_balance). reconciliation_overdue means a month-end "
            "past its 30-day grace has no completed reconciliation covering "
            "it; reconciliation_never_performed flags a firm that has never "
            "reconciled at all. Amounts in cents + fr-CA display. "
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
                **_write_protocol_props(),
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
                **_write_protocol_props(),
            },
            "required": ["note_id", "content"],
            "additionalProperties": False,
        },
        "handler": "append_to_note",
        "scope": SCOPE_WRITE,
    },
    "complete_task": {
        "title": "Clore une tâche",
        "annotations": {
            # A second call with the same status writes nothing at all.
            "idempotentHint": True,
        },
        "description": (
            "Close a task: « terminée », « annulée », or move it to "
            "« en_cours ». The ONLY status change this connector can make — "
            "it cannot reopen a task to « à_faire », edit its title or "
            "delete it; those are done in the application. "
            "CASCADE, and read this before calling: completing a task that "
            "a protocol step is linked to ALSO completes that step, exactly "
            "as ticking the box in the application does — and if it was the "
            "last open step, THE WHOLE PROTOCOL closes and its deadlines "
            "stop appearing in get_agenda. `protocol_step_effect` reports "
            "what actually happened, re-read from the document after the "
            "write, never predicted. Preview it with `dry_run: true` first "
            "whenever the task belongs to a dossier under an active "
            "protocol. "
            "Two asymmetries worth knowing: « annulée » triggers NO cascade, "
            "so the linked step stays open and keeps appearing in the "
            "briefing; and « en_cours » on an already-completed task "
            "RE-OPENS the linked step. "
            "Calling it on a task that already carries the requested status "
            "is a safe no-op (`already_completed: true`, nothing written) — "
            "which is what makes a scheduled job replayable. Asking for the "
            "other terminal status on an already-closed task is REFUSED "
            "rather than silently rewriting the lawyer's decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("The task to close (UUIDv4)."),
                "status": {
                    "type": "string",
                    "enum": ["terminée", "annulée", "en_cours"],
                    "description": (
                        "Default « terminée ». « à_faire » is deliberately "
                        "absent: reopening is done in the application."
                    ),
                },
                "completion_note": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": (
                        "Optional French note appended to the task's "
                        "description under a dated « par Claude » stamp. "
                        "Refused rather than truncated if the combined text "
                        "would pass the 2000-character field ceiling."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "scope": SCOPE_WRITE,
        "handler": "complete_task",
    },
    "create_task": {
        "title": "Créer une tâche",
        "description": (
            "WRITE. Create a task — the deadline-custody entry point: a "
            "deadline you computed belongs HERE, in the agenda that raises "
            "alarms, not in the prose of a note. With dossier_id it is "
            "filed on that dossier (an unresolvable id is refused, never "
            "downgraded); omit it only for practice-wide to-dos "
            "(« Général »). The task is created à_faire — this connector "
            "can close it with complete_task but can never edit or delete "
            "it, and it syncs to the "
            "lawyer's phone. Confirm with the user before calling; use "
            "dry_run to propose first, and an idempotency_key on every "
            "scheduled call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier to file the task on (UUIDv4). Omit for a "
                    "standalone (« Général ») task."
                ),
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Task title, in French — the actionable.",
                },
                "description": {
                    "type": "string",
                    "maxLength": 1500,
                    "description": (
                        "Optional detail (basis of the deadline, article, "
                        "computation), in French. A provenance stamp is "
                        "appended automatically."
                    ),
                },
                "due_date": _date(
                    "Deadline, YYYY-MM-DD. Omit for an undated task — but "
                    "an undated task never appears in the urgent lists, "
                    "so a computed deadline should always carry its date."
                ),
                "priority": {
                    "type": "string",
                    "enum": _TASK_PRIORITIES,
                    "description": "Defaults to 'normale'.",
                },
                "category": {
                    "type": "string",
                    "enum": _TASK_CATEGORIES,
                    "description": "Defaults to 'autre'.",
                },
                **_phase_props(),
                **_write_protocol_props(),
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        "handler": "create_task",
        "scope": SCOPE_WRITE,
    },
    "create_hearing": {
        "title": "Créer un événement au calendrier",
        "description": (
            "WRITE. Create a calendar event (hearing, meeting, "
            "examination…) on the shared agenda. hearing_type drives the "
            "derived forum: the five judicial types (audience, "
            "instruction, conférence_de_gestion/_de_règlement/"
            "_préparatoire) read as court events; it defaults to "
            "« rencontre » (extrajudiciaire). Times are Montréal local "
            "(HH:MM); omitting start_time makes it an all-day event. The "
            "event is created with status à_confirmer and syncs to the "
            "lawyer's phone; this connector can never edit or delete it. "
            "Confirm with the user before calling; dry_run to propose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier this event belongs to (UUIDv4). Omit for "
                    "a standalone (« Général ») event."
                ),
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                    "description": "Event title, in French.",
                },
                "hearing_type": {
                    "type": "string",
                    "enum": _HEARING_TYPES,
                    "description": (
                        "Two-tier vocabulary; the FORUM derives from it. "
                        "Defaults to 'rencontre' (extrajudiciaire) — pick "
                        "a judicial type only for a real court event."
                    ),
                },
                "date": _date("Event date, YYYY-MM-DD (Montréal). Required."),
                "start_time": {
                    "type": "string",
                    "maxLength": 5,
                    "description": (
                        "HH:MM Montréal. Omit for an all-day event."
                    ),
                },
                "end_time": {
                    "type": "string",
                    "maxLength": 5,
                    "description": (
                        "HH:MM Montréal. Defaults to start + 1 h."
                    ),
                },
                "all_day": {
                    "type": "boolean",
                    "description": "true forces an all-day event.",
                },
                "location": {
                    "type": "string",
                    "maxLength": 300,
                    "description": "Room, address or palais de justice.",
                },
                "court": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Court name, for judicial events.",
                },
                "judge": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Presiding judge, when known.",
                },
                "notes": {
                    "type": "string",
                    "maxLength": 1500,
                    "description": (
                        "Free notes, in French. A provenance stamp is "
                        "appended automatically."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["title", "date"],
            "additionalProperties": False,
        },
        "handler": "create_hearing",
        "scope": SCOPE_WRITE,
    },
    "create_time_entry": {
        "title": "Créer une entrée de temps",
        "description": (
            "WRITE. Record billable (or non-billable) time on a dossier — "
            "capture work at the moment it happens instead of "
            "reconstructing at billing time. dossier_id is REQUIRED (time "
            "always belongs to a file). The description prints VERBATIM "
            "on the client's invoice: write it as a billing narrative, in "
            "French, and never include provenance or internal notes — the "
            "entry is marked machine-created internally. rate_cents "
            "defaults to the dossier's hourly rate. This connector can "
            "never edit or delete the entry. Confirm with the user before "
            "calling; dry_run shows the computed amount first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier the time belongs to (UUIDv4). Required."
                ),
                "date": _date("Work date, YYYY-MM-DD. Required."),
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": (
                        "Billing narrative, in French — prints verbatim "
                        "on the invoice."
                    ),
                },
                "hours": {
                    "type": "number",
                    "minimum": 0.01,
                    "maximum": 24,
                    "description": (
                        "Hours worked, at most TWO decimals — so a legacy "
                        "quarter-hour (0.25) imports exactly. Anything finer "
                        "is refused rather than rounded: 0.25 h silently "
                        "rounded to 0.2 h bills 60,00 $ where the paper "
                        "invoice printed 75,00 $."
                    ),
                },
                "rate_cents": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000000,
                    "description": (
                        "Hourly rate in cents. Omit to use the dossier's "
                        "rate."
                    ),
                },
                "billable": {
                    "type": "boolean",
                    "description": (
                        "Defaults to true. Non-billable time is recorded "
                        "with amount 0."
                    ),
                },
                **_phase_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "date", "description", "hours"],
            "additionalProperties": False,
        },
        "handler": "create_time_entry",
        "scope": SCOPE_WRITE,
    },
    "create_expense": {
        "title": "Créer un déboursé",
        "description": (
            "WRITE. Record a disbursement (débours) on a dossier — "
            "bailiff, expert, transcript, filing stamp… dossier_id is "
            "REQUIRED. The description prints verbatim on the client's "
            "invoice: billing narrative in French, no internal notes. "
            "Amount in integer cents. This connector can never edit or "
            "delete the entry. Confirm with the user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier the expense belongs to (UUIDv4). Required."
                ),
                "date": _date("Expense date, YYYY-MM-DD. Required."),
                "description": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": (
                        "Billing narrative, in French — prints verbatim "
                        "on the invoice."
                    ),
                },
                "amount_cents": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100000000,
                    "description": "Amount in integer cents (15000 = 150,00 $).",
                },
                "category": {
                    "type": "string",
                    "enum": _EXPENSE_CATEGORIES,
                    "description": "Defaults to 'autre'.",
                },
                "taxable": {
                    "type": "boolean",
                    "description": "Defaults to true.",
                },
                **_phase_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "date", "description", "amount_cents"],
            "additionalProperties": False,
        },
        "handler": "create_expense",
        "scope": SCOPE_WRITE,
    },
    "complete_dossier": {
        "title": "Compléter les champs vides d'un dossier",
        "description": (
            "WRITE, fill-only-if-empty. Fill dossier fields that are EMPTY "
            "or still at their model default — a computed classification, "
            "value in dispute, prescription starting point… A field that "
            "already carries a different value is NEVER overwritten: the "
            "whole call is refused listing the conflicting fields, and "
            "nothing is written (changing a set value is the lawyer's act, "
            "in the app). Filling court_file_number also derives the "
            "judicial metadata exactly as the web form does. Money in "
            "integer cents; contingency_percent in basis points "
            "(2500 = 25 %); dates YYYY-MM-DD. Confirm with the user "
            "before calling; dry_run shows the resulting prescription "
            "picture first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("The dossier to complete (UUIDv4). Required."),
                "domaine": {
                    "type": "string", "maxLength": 10,
                    "description": ("Taxonomy family code (e.g. REC) — "
                                    "validated by the model."),
                },
                "action": {
                    "type": "string", "maxLength": 10,
                    "description": ("Taxonomy action code (e.g. REC-01); "
                                    "must belong to the domaine."),
                },
                "action_precision": {
                    "type": "string", "maxLength": 500,
                    "description": "Free-text precision (the -99 rows require it).",
                },
                "sommaire": {
                    "type": "string", "maxLength": 5000,
                    "description": (
                        "Case summary, French — only when empty. The model "
                        "stores it up to 5000, unlike every other string "
                        "field, which caps at 2000."
                    ),
                },
                "mandate_type": {
                    "type": "string",
                    "enum": ["judiciaire", "service_conseils", "general", "special"],
                    "description": "Nature of the engagement.",
                },
                "court_file_number": {
                    "type": "string", "maxLength": 40,
                    "description": ("Court file number; judicial metadata "
                                    "derives from it when parseable."),
                },
                "prescription_type": {
                    "type": "string", "maxLength": 40,
                    "description": ("Confirmed delay key (e.g. 3_ans) — "
                                    "validated against the model "
                                    "vocabulary; drives the computed date "
                                    "pour agir."),
                },
                "fee_type": {
                    "type": "string",
                    "enum": ["hourly", "flat", "contingency", "mixed",
                             "pro_bono", "aide_juridique"],
                    "description": "Fee arrangement.",
                },
                "fee_notes": {
                    "type": "string", "maxLength": 1000,
                    "description": "Free text on the fee arrangement.",
                },
                "valeur": {
                    "type": "integer", "minimum": 1,
                    "description": "Amount in dispute, integer cents.",
                },
                "hourly_rate": {
                    "type": "integer", "minimum": 1,
                    "description": ("Hourly rate in cents — fills only if "
                                    "still at the 30000 default."),
                },
                "flat_fee": {
                    "type": "integer", "minimum": 1,
                    "description": "Flat fee in cents.",
                },
                "contingency_percent": {
                    "type": "integer", "minimum": 1, "maximum": 10000,
                    "description": "Basis points (2500 = 25,00 %).",
                },
                "droit_action_date": _date(
                    "Start of the prescription period, YYYY-MM-DD."),
                "date_avis": _date(
                    "Confirmed avis préalable date, YYYY-MM-DD."),
                "prise_action_date": _date(
                    "Date the recourse was filed (art. 2892) — silences "
                    "the prescription alert. Prefer "
                    "record_prescription_event for new entries."),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "complete_dossier",
        "scope": SCOPE_WRITE,
    },
    "record_signification": {
        "title": "Consigner une signification",
        "description": (
            "WRITE, append-only. Record service of process on a party OF "
            "the dossier — one entry per party served (arts. 145/147 "
            "C.p.c. delays run per party). A party not on the dossier is "
            "refused. Use `supersedes` when a corrected procès-verbal "
            "replaces an earlier one (the prior entry is marked "
            "superseded, never deleted). This connector can never edit or "
            "remove a recorded signification. Confirm with the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("The dossier (UUIDv4). Required."),
                "partie_id": _id(
                    "The served party's contact id — must be a party ON "
                    "the dossier (see get_dossier clients/opposing_parties)."
                ),
                "date": _date("Service date, YYYY-MM-DD. Required."),
                "mode": {
                    "type": "string",
                    "enum": ["personnelle", "domicile", "huissier",
                             "notification", "avocat", "publication"],
                    "description": "Mode of service. Defaults to 'huissier'.",
                },
                "huissier_id": _id(
                    "Optional contact id of the bailiff."
                ),
                "pv_document_id": _id(
                    "Optional documents record of the procès-verbal."
                ),
                "supersedes": _id(
                    "Id of the EARLIER signification this one replaces "
                    "(from get_dossier.significations) — the corrected-"
                    "second-PV case."
                ),
                "confirmee": {
                    "type": "boolean",
                    "description": "true once the procès-verbal is in hand.",
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "partie_id", "date"],
            "additionalProperties": False,
        },
        "handler": "record_signification",
        "scope": SCOPE_WRITE,
    },
    "record_prescription_event": {
        "title": "Consigner un événement de prescription",
        "description": (
            "WRITE, append-only. Record a C.c.Q. prescription event on the "
            "dossier: interruption_depot (art. 2892 — a demande filed; "
            "silences the alert, effective date becomes null per art. "
            "2896), interruption_reconnaissance (art. 2898 — restarts the "
            "confirmed period from the event date), suspension (art. 2904 "
            "— requires end_date, shifts the effective deadline), "
            "renonciation (art. 2883). The RAW prescription_date is never "
            "recomputed — the derived prescription_status and "
            "prescription_date_effective returned by this call (and by "
            "get_dossier) carry the picture. This connector can never "
            "edit or remove a recorded event. Confirm with the user; "
            "dry_run previews the resulting status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("The dossier (UUIDv4). Required."),
                "type": {
                    "type": "string",
                    "enum": ["interruption_depot",
                             "interruption_reconnaissance",
                             "suspension", "renonciation"],
                    "description": "The C.c.Q. event kind.",
                },
                "date": _date("Event date, YYYY-MM-DD. Required."),
                "end_date": _date(
                    "Suspension end, YYYY-MM-DD — required for (and only "
                    "meaningful on) type=suspension."
                ),
                "reference": {
                    "type": "string", "maxLength": 300,
                    "description": ("Free text: article, document, "
                                    "circumstance (French)."),
                },
                "document_id": _id(
                    "Optional documents record supporting the event."
                ),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "type", "date"],
            "additionalProperties": False,
        },
        "handler": "record_prescription_event",
        "scope": SCOPE_WRITE,
    },
    "update_time_entry": {
        "title": "Corriger une entrée de temps",
        "description": (
            "WRITE — REPLACES the values you name; a field you omit is "
            "untouched. Correct a transcription error BEFORE the entry is "
            "invoiced: once it is, neither this connector nor the "
            "application can modify it, and the only way back is voiding the "
            "invoice in the application (which releases every source). "
            "`amount` is never yours to set — the model recomputes it as "
            "hours × rate, and forces 0 on a non-billable entry. "
            "Omitting `billable` leaves it as it is: never send it « just in "
            "case », because flipping a deliberately non-billable entry back "
            "on rematerialises its amount. "
            "Omitting BOTH phase keys leaves the classification alone; "
            "naming either rewrites both."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_entry_id": _id("The entry to correct (UUIDv4). Required."),
                "date": _date("Correct the date, YYYY-MM-DD."),
                "description": {
                    "type": "string", "minLength": 1, "maxLength": 2000,
                    "description": (
                        "Billing narrative, French — prints VERBATIM on the "
                        "client's invoice. No provenance note is ever added."
                    ),
                },
                "hours": {
                    "type": "number", "minimum": 0.01, "maximum": 24,
                    "description": (
                        "At most two decimals (0.25 for a quarter hour); "
                        "anything finer is refused rather than rounded."
                    ),
                },
                "rate_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000,
                    "description": "Hourly rate in cents; 0 is legitimate.",
                },
                "billable": {
                    "type": "boolean",
                    "description": (
                        "Send ONLY to change it. A non-billable entry always "
                        "carries amount 0."
                    ),
                },
                **_phase_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["time_entry_id"],
            "additionalProperties": False,
        },
        "handler": "update_time_entry",
        "scope": SCOPE_WRITE,
    },
    "update_expense": {
        "title": "Corriger un déboursé",
        "description": (
            "WRITE — REPLACES the values you name; a field you omit is "
            "untouched. Same wall as update_time_entry: once the "
            "disbursement is invoiced nothing here can touch it. "
            "Unlike a time entry, `amount_cents` IS yours — the model never "
            "recomputes a disbursement, so a historical amount survives "
            "exactly. "
            "Omitting `taxable` leaves it as it is: sending it « just in "
            "case » would add QST to a non-taxable disbursement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expense_id": _id("The disbursement to correct. Required."),
                "date": _date("Correct the date, YYYY-MM-DD."),
                "description": {
                    "type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "Prints VERBATIM on the client's invoice.",
                },
                "amount_cents": {
                    "type": "integer", "minimum": 1, "maximum": 100000000,
                    "description": "Amount in integer cents.",
                },
                "category": {
                    "type": "string", "enum": _EXPENSE_CATEGORIES,
                    "description": "Disbursement category.",
                },
                "taxable": {
                    "type": "boolean",
                    "description": "Send ONLY to change it.",
                },
                **_phase_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
        "handler": "update_expense",
        "scope": SCOPE_WRITE,
    },
    "set_time_entry_phase": {
        "title": "Reclasser la phase d'une entrée de temps",
        "description": (
            "WRITE — sets ONLY the litigation phase (`phase`/`sous_phase`) "
            "of a time entry, and it is the only tool that can do so once "
            "the entry has been carried to an invoice. That is safe because "
            "the phase appears on NO invoice: line items are independent "
            "copies with no phase field, and nothing on the client's note "
            "d'honoraires reads it. It feeds the dossier's budget-vs-actuals "
            "view, which counts billed work too. Hours, rate, amount, "
            "description, billable and invoiced are not addressable here and "
            "cannot move. When the entry is NOT yet invoiced, "
            "`update_time_entry` can set the phase as well, alongside other "
            "corrections — prefer it there. Give `sous_phase` alone and the "
            "parent phase is derived from its prefix; give `phase` alone and "
            "it imputes to that phase's « -00 » (Général). Re-sending the "
            "code the entry already carries writes nothing at all."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_entry_id": _id(
                    "The entry to reclassify (UUIDv4). Required."
                ),
                **_phase_props(),
                **_write_protocol_props(),
            },
            "required": ["time_entry_id"],
            "additionalProperties": False,
        },
        "handler": "set_time_entry_phase",
        "scope": SCOPE_WRITE,
        # A second identical call writes nothing: the model compares the
        # stored pair first. Declared per tool, like complete_task.
        "annotations": {"idempotentHint": True},
    },
    "set_expense_phase": {
        "title": "Reclasser la phase d'un déboursé",
        "description": (
            "WRITE — the disbursement twin of `set_time_entry_phase`: sets "
            "ONLY `phase`/`sous_phase`, invoiced or not, because the phase "
            "appears on no invoice. Amount, category, taxable, description "
            "and invoiced cannot move here. Disbursements carry the "
            "« frais » half of a phase's actuals, so a budget is only right "
            "when both halves are classified. Re-sending the stored code "
            "writes nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expense_id": _id(
                    "The disbursement to reclassify. Required."
                ),
                **_phase_props(),
                **_write_protocol_props(),
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
        "handler": "set_expense_phase",
        "scope": SCOPE_WRITE,
        "annotations": {"idempotentHint": True},
    },
    "set_time_entry_phase_bulk": {
        "title": "Reclasser la phase de plusieurs entrées de temps",
        "description": (
            "WRITE — the batch form of `set_time_entry_phase`: reclassify a "
            "whole page of `list_time_entries` in one call instead of one "
            "call per row. `results` comes back in the SAME ORDER as "
            "`entries`, one row each, saying whether the entry was applied, "
            "left alone because it already carried that code, or refused and "
            "why — a refused item never blocks its neighbours, and nothing "
            "is ever changed without being named. Naming the same id twice "
            "refuses the WHOLE call: two codes for one row means the plan is "
            "ambiguous. A retry with the same `idempotency_key` replays the "
            "stored report rather than re-attempting the refusals — fix them "
            "and send a NEW key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _phase_bulk_items(
                    "time_entry_id", "A time entry to reclassify."
                ),
                **_write_protocol_props(),
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
        "handler": "set_time_entry_phase_bulk",
        "scope": SCOPE_WRITE,
        "annotations": {"idempotentHint": True},
    },
    "set_expense_phase_bulk": {
        "title": "Reclasser la phase de plusieurs déboursés",
        "description": (
            "WRITE — the batch form of `set_expense_phase`, same contract as "
            "`set_time_entry_phase_bulk`: ordered per-item results, a refusal "
            "that stops nothing else, a duplicated id that refuses the whole "
            "call, and a replayed `idempotency_key` that returns the stored "
            "report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entries": _phase_bulk_items(
                    "expense_id", "A disbursement to reclassify."
                ),
                **_write_protocol_props(),
            },
            "required": ["entries"],
            "additionalProperties": False,
        },
        "handler": "set_expense_phase_bulk",
        "scope": SCOPE_WRITE,
        "annotations": {"idempotentHint": True},
    },
    "import_invoice": {
        "title": "Importer une facture du système précédent",
        "description": (
            "WRITE. Recreate an invoice the previous system already issued, "
            "under ITS OWN number and date — the year counter is never read "
            "and never advanced, so the live numbering is untouched. "
            "SOURCE-FIRST: create the historical time entries and "
            "disbursements first, then bill them here. Line items can only "
            "come from real, uninvoiced sources of this dossier; there is no "
            "literal-line-item path, which is what keeps the budget, the "
            "phase reporting and the fee journal truthful. "
            "`expected_total_cents` is REQUIRED — the grand total printed on "
            "the paper invoice, BEFORE any retainer is applied. Any "
            "difference refuses the creation with the gap and the breakdown; "
            "there is no tolerance, because one cent of silent drift is how "
            "a book of account starts lying. When the paper total genuinely "
            "cannot be rebuilt from the lines (a courtesy write-down, a "
            "rounding), name the difference with `adjustment` so it is "
            "written ON the invoice instead of hidden. "
            "ALWAYS dry_run first and compare the previewed subtotal, GST and "
            "QST against the PDF: the preview runs the real computation over "
            "the real sources, it does not estimate. "
            "The invoice lands in BROUILLON and stays there — this connector "
            "never sets an invoice status and never records a payment. "
            "Billing the sources freezes them: nothing here can modify them "
            "afterwards, and the only way back is voiding the invoice in the "
            "application, which releases every source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("The dossier this invoice belongs to."),
                "invoice_number": {
                    "type": "string", "minLength": 1, "maxLength": 32,
                    "description": (
                        "The number the invoice ALREADY bears. Refused if it "
                        "belongs to the current year's live « YYYY-F… » "
                        "series (that counter would hand it out again), if "
                        "another invoice already carries it, or if it is over "
                        "length — never truncated."
                    ),
                },
                "date": _date("The ORIGINAL invoice date, YYYY-MM-DD."),
                "due_date": _date("Defaults to date + 30 days."),
                "expected_total_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000000,
                    "description": (
                        "The grand total printed on the paper invoice, in "
                        "cents, BEFORE any retainer is deducted — not the "
                        "balance due."
                    ),
                },
                "time_entry_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "description": (
                        "Time entries to bill. Every one must exist, be "
                        "uninvoiced and belong to this dossier — otherwise "
                        "the whole call is refused, naming each offender."
                    ),
                },
                "expense_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "description": "Disbursements to bill, same rules.",
                },
                "retainer_applied_cents": {
                    "type": "integer", "minimum": 0, "maximum": 100000000000,
                    "description": (
                        "A provision deducted on the original invoice. "
                        "Without it the recorded balance stays overstated and "
                        "the invoice can never settle."
                    ),
                },
                "adjustment": {
                    "type": "object",
                    "description": (
                        "The escape hatch when the printed total cannot be "
                        "rebuilt from the lines. Becomes ONE named fee line."
                    ),
                    "properties": {
                        "amount_cents": {
                            "type": "integer",
                            "description": (
                                "May be negative (a write-down). Never 0."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "French, printed on the invoice — « Remise de "
                                "courtoisie », « Arrondi ». Required: an "
                                "unexplained amount on a client's invoice is "
                                "worse than a refusal."
                            ),
                        },
                        "taxable": {
                            "type": "boolean",
                            "description": (
                                "true (default) reproduces an invoice whose "
                                "GST/QST were computed on the reduced amount; "
                                "false reproduces one discounted after tax."
                            ),
                        },
                    },
                    "required": ["amount_cents", "description"],
                    "additionalProperties": False,
                },
                "notes": {
                    "type": "string", "maxLength": 1500,
                    "description": "Notes carried on the invoice.",
                },
                "payment_terms": {
                    "type": "string", "maxLength": 500,
                    "description": "Payment terms as originally printed.",
                },
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": [
                "dossier_id", "invoice_number", "date", "expected_total_cents",
            ],
            "additionalProperties": False,
        },
        "handler": "import_invoice",
        "scope": SCOPE_WRITE,
    },
    "create_dossier": {
        "title": "Créer un dossier",
        "description": (
            "WRITE. Open a dossier — built for transcribing a historical "
            "file, so `status` may be « fermé » or « archivé » from the "
            "start and `opened_date` / `closed_date` are yours to set. A "
            "dossier created closed is never advertised to DavX5, which is "
            "deliberate: there is no collection to drain because none ever "
            "existed. "
            "Every partie_id is RESOLVED before anything is written and the "
            "names are snapshotted server-side — they are what a generated "
            "procedure will cite. An unknown id is refused, never blanked. "
            "Call get_reference_vocabulary for domaine / action / "
            "prescription_type / forum / district codes instead of guessing: "
            "the model refuses an invalid one without naming a valid one. "
            "`hourly_rate` accepts 0 (pro bono, aide juridique) — set it, "
            "because create_time_entry defaults each entry's rate to it. "
            "Check find_imported first and pass `legacy_ref`: nothing here "
            "can delete a duplicate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_number": {
                    "type": "string", "minLength": 1, "maxLength": 40,
                    "description": (
                        "The file number as the practice wrote it, e.g. "
                        "« 2019-014 ». Refused if a dossier already bears it."
                    ),
                },
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "e.g. « Tremblay c. Lavoie ».",
                },
                "clients": {
                    "type": "array",
                    "description": (
                        "At least one. Each entry names an EXISTING contact."
                    ),
                    "items": _party_entry_props(),
                },
                "opposing_parties": {
                    "type": "array",
                    "description": "Opposing parties, same shape as clients.",
                    "items": _party_entry_props(),
                },
                "status": {
                    "type": "string", "enum": _DOSSIER_STATUSES,
                    "description": (
                        "Defaults to « actif ». A historical file usually "
                        "arrives « fermé » or « archivé »; it can NEVER be "
                        "changed afterwards through this connector."
                    ),
                },
                "opened_date": _date("Opening date, YYYY-MM-DD."),
                "closed_date": _date(
                    "Closing date, YYYY-MM-DD. Auto-stamped when the status "
                    "is fermé/archivé and none is given."
                ),
                **_forum_props(),
                **_dossier_field_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["file_number", "title", "clients"],
            "additionalProperties": False,
        },
        "handler": "create_dossier",
        "scope": SCOPE_WRITE,
    },
    "update_dossier": {
        "title": "Corriger un dossier",
        "description": (
            "WRITE — REPLACES the values you name; a field you omit is "
            "untouched. Use complete_dossier instead when you only want to "
            "FILL fields that are still empty: it refuses to overwrite, which "
            "is the safer tool for an unattended job. "
            "`status` is deliberately NOT accepted: closing a dossier must "
            "drain its DavX5 collection, which only the application does — "
            "one closed here would leave its tasks, notes and hearings on the "
            "phone for ever. `file_number` is not accepted either (every "
            "invoice froze a snapshot of it), nor is `closed_date`. "
            "Party arrays are APPEND-only via add_clients / "
            "add_opposing_parties: passing a whole array would silently drop "
            "the parties you left out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id("The dossier to correct (UUIDv4). Required."),
                "title": {"type": "string", "maxLength": 300,
                          "description": "New title."},
                "sommaire": {"type": "string", "maxLength": 5000,
                             "description": "Free-text case summary."},
                "opened_date": _date("Correct the opening date, YYYY-MM-DD."),
                "add_clients": {
                    "type": "array",
                    "description": (
                        "Parties to ADD as clients. Refused if one is already "
                        "on the dossier."
                    ),
                    "items": _party_entry_props(),
                },
                "add_opposing_parties": {
                    "type": "array",
                    "description": "Parties to ADD as opposing parties.",
                    "items": _party_entry_props(),
                },
                **_forum_props(),
                **_dossier_field_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "update_dossier",
        "scope": SCOPE_WRITE,
    },
    "create_partie": {
        "title": "Créer un contact",
        "description": (
            "WRITE. Create a contact (partie): a client, an opposing party, "
            "opposing counsel, an expert, a bailiff… Built for transcribing "
            "a historical file, so pass `legacy_ref` and check "
            "find_imported first — this connector can never delete a "
            "duplicate. "
            "An individual REQUIRES last_name; an organization REQUIRES "
            "organization_name; never mix the two families. "
            "An ADDRESS TRAVELS AS A BLOCK of six keys (street, unit, city, "
            "province, postal_code, country) or not at all: the model "
            "completes a partial block with Montréal / Québec / Canada, so "
            "a Toronto contact sent with a street and no city is silently "
            "relocated — onto an invoice the client will receive. "
            "Identity verification and conflict-of-interest checks are NOT "
            "writable here and never will be: a machine must not attest "
            "that a client's identity was verified."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string", "enum": _PARTIE_TYPES,
                    "description": "individual (natural person) or organization.",
                },
                "contact_role": {
                    "type": "string", "enum": _CONTACT_ROLES,
                    "description": "The contact's role. Defaults to « client ».",
                },
                **_partie_identity_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["type"],
            "additionalProperties": False,
        },
        "handler": "create_partie",
        "scope": SCOPE_WRITE,
    },
    "update_partie": {
        "title": "Corriger un contact",
        "description": (
            "WRITE — REPLACES the values you name. Send ONLY the fields that "
            "change: a field you omit is untouched, but a field sent EMPTY is "
            "ERASED (the model writes the whole document). Never rebuild the "
            "payload from a full get_partie card. "
            "The same six-key ADDRESS BLOCK rule as create_partie: read the "
            "current block from get_partie and send it back complete. "
            "`type` is not changeable here — flipping individual ↔ "
            "organization strands the required-name rule and every display "
            "name built from it. Identity verification, conflict checks and "
            "mandataires are not writable. "
            "Note the model re-validates the WHOLE merged record: a legacy "
            "contact carrying an unparseable phone number will refuse every "
            "edit, naming a field you did not touch — fix that field first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "partie_id": _id("The contact to correct (UUIDv4). Required."),
                "contact_role": {
                    "type": "string", "enum": _CONTACT_ROLES,
                    "description": "Change the contact's role.",
                },
                **_partie_identity_props(),
                **_legacy_ref_prop(),
                **_write_protocol_props(),
            },
            "required": ["partie_id"],
            "additionalProperties": False,
        },
        "handler": "update_partie",
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
        annotations = dict(
            _WRITE_ANNOTATIONS if name in WRITE_TOOLS else _READ_ONLY_ANNOTATIONS
        )
        if name in EDIT_TOOLS:
            # DERIVED from EDIT_TOOLS, never restated per tool: an edit that
            # replaces a stored value IS a destructive update in the spec's
            # sense, and a future editor gets the honest hint by membership
            # alone rather than by someone remembering to add an override.
            annotations["destructiveHint"] = True
        # A tool may correct a hint the family default gets wrong for it.
        # complete_task is genuinely idempotent — a second call with the
        # same status is a no-op that writes nothing — while every creator
        # would append again.
        annotations.update(spec.get("annotations") or {})
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
