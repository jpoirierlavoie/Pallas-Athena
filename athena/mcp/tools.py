"""MCP tool registry, subset JSON-Schema validator, and output helpers.

The registry maps tool names to their metadata and handler name (resolved
lazily against :mod:`mcp.handlers` to avoid a circular import). Every tool
is read-only (``readOnlyHint``) **except the members of** :data:`WRITE_TOOLS`,
which require the ``athena:write`` scope — or, for the subset
:data:`ACCOUNTING_TOOLS`, the separate ``athena:comptabilite`` scope (empty
until plan lot 5). Every schema sets ``additionalProperties: false``.
"""

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from mcp import (
    SCOPE_COMPTABILITE,
    SCOPE_READ,
    SCOPE_WRITE,
    comptabilite_enabled,
    write_enabled,
)
from mcp import coverage, disclosure
from mcp.output_schemas import OUTPUT_SCHEMAS
from tz import to_mtl
# Pure module (no Firestore at import) — safe to derive enums from, unlike
# models.* (see the literal-enum comment below).
from utils import analyse_taxonomies as _tax
from utils import phases
from utils import recurrence

# ── Money / date formatting (§10.1 conventions) ─────────────────────────

_NBSP = " "


#: The reason a refusal is logged under when its raiser names none.
DEFAULT_REFUSAL_REASON = "argument_refused"


class ToolArgumentError(Exception):
    """Argument-level failure a handler detects beyond the schema
    (bad date string, mutually exclusive params). Maps to JSON-RPC -32602.

    The MESSAGE goes to the client and nowhere else: it describes
    user-supplied content (a note's text, a party's name), so it is kept
    out of spans and logs. ``reason`` is what the refusal is LOGGED under
    (``mcp_write_refused``) — a machine-stable snake_case code, never text.
    Keyword-only, so every existing one-message raise is unchanged and
    logs as :data:`DEFAULT_REFUSAL_REASON`.
    """

    def __init__(
        self, message: str, *, reason: str = DEFAULT_REFUSAL_REASON
    ) -> None:
        super().__init__(message)
        self.reason = reason


class CommittedWriteError(Exception):
    """A write COMMITTED, then a later step of the same call failed.

    Raised by :func:`mcp.write_support.run_write` when ``execute`` fails
    AFTER a model noted a commit (``models.provenance.note_commit``) — a
    CTag bump, the re-read of a cascaded step, the payload builder. Before
    it existed that exception reached the endpoint's blanket ``except`` and
    came back as a retryable « internal error », so a caller retried and the
    write happened twice. This one says the opposite, loudly: the write is
    there, do NOT retry, re-read it.

    Deliberately NOT a :class:`ToolArgumentError`: a refusal is a -32602
    that promises nothing was written; this is a tool RESULT (``isError``)
    saying something WAS. Every field is an id, a collection name or a
    count — never content — because the endpoint logs them
    (``mcp_write_partial``) and the message reaches the client verbatim.
    ``replay`` is true when the error is re-raised from a stored ``partial``
    idempotency record rather than from this call's own failure.
    """

    def __init__(
        self,
        tool: str,
        entity_id: Optional[str],
        dossier_id: Optional[str],
        *,
        collection: str = "",
        rows: int = 1,
        replay: bool = False,
    ) -> None:
        self.tool = tool
        self.entity_id = entity_id or None
        self.dossier_id = dossier_id or None
        self.collection = collection or ""
        self.rows = int(rows or 0)
        self.replay = bool(replay)
        super().__init__(self._message())

    def _message(self) -> str:
        parts = []
        if self.entity_id:
            parts.append(
                f"{self.collection}/{self.entity_id}" if self.collection
                else f"id {self.entity_id}"
            )
        if self.dossier_id:
            parts.append(f"dossier {self.dossier_id}")
        if self.rows > 1:
            parts.append(f"{self.rows} éléments écrits")
        where = f" ({', '.join(parts)})" if parts else ""
        return (
            f"L'écriture est ENREGISTRÉE{where} mais une étape qui la suit a "
            "échoué. NE PAS RÉESSAYER : relisez l'élément pour constater son "
            "état — tout nouvel appel qui ne reprend pas la même "
            "idempotency_key (un appel sans clé compris) l'écrirait une "
            "seconde fois."
        )


# The shape of a server-minted document id (Architecture Rule 6: UUIDv4).
# Fixed-count character classes only — linear, nothing to backtrack over.
_ID_SHAPE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def loggable_id(value: Any) -> Optional[str]:
    """*value* when it is shaped like an id, else None.

    For any identifier that is about to reach a log line or a stored record
    without having been proven to be one: a refused call's ``dossier_id`` is
    still the caller's string (the schema bounds it to 64 characters and
    nothing more), and a title or a party name pasted into it must not reach
    a log the RedactionFilter does not scrub for names. ``fullmatch``, never
    ``match`` + ``$``: ``$`` also matches before a trailing newline.
    """
    if isinstance(value, str) and _ID_SHAPE.fullmatch(value):
        return value
    return None


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
    ``minLength``, ``minItems``, ``maxItems``, ``items`` (a single schema,
    applied to every element — recursively, so an object inside an array is
    validated in full), ``anyOf``, ``additionalProperties: false``. Empty
    list = valid.

    Despite the name, this validates OUTPUT payloads too: the conformance
    tests run every handler and check its real payload against the declared
    ``outputSchema`` with this same validator, so a schema the validator
    cannot express cannot be declared — the contract and its enforcement
    use one grammar.

    ⚠ An UNLISTED keyword is not an error: it is silently ignored. That is
    how ``minItems``/``maxItems`` sat declared on the bulk phase tools,
    advertised to the client and enforced by nothing but the handler,
    until 2026-09-25. Implement a keyword here before a schema relies on it.
    The same holds for three FORMS of listed keywords: an unknown ``type``
    name accepts any value, an ``additionalProperties`` SCHEMA (anything
    but ``false``) is ignored, and a tuple-form ``items`` list crashes the
    walk. ``tests/test_mcp_tools.py`` refuses all of these in every
    declared schema.
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


def _nearest_argument(key: str, properties: dict) -> str:
    """The supported name a bad one was probably meant to be, or "".

    Deliberately narrow — a prefix relation either way, case-folded. That
    covers the shapes actually observed (a spurious suffix, a truncation,
    a case slip) without the false confidence of an edit-distance guess:
    naming the WRONG neighbour would send the caller off to fix a field it
    never meant.
    """
    plie = key.casefold()
    candidats = [
        nom for nom in properties
        if plie.startswith(nom.casefold()) or nom.casefold().startswith(plie)
    ]
    if not candidats:
        return ""
    # Ne suggérer QUE si les candidats forment une chaîne — le plus long
    # étend tous les autres. Sinon ils divergent, et le plus long n'est
    # qu'un frère parmi d'autres : « dossier » propose alors
    # `dossier_status` là où l'appelant voulait `dossier_id`, et suivre la
    # suggestion pose un filtre qui RÉTRÉCIT la portée en silence. Sur les
    # 2 755 cas de suffixe parasite du corpus cette règle ne perd rien, et
    # elle éteint les 24 cas divergents.
    long = max(candidats, key=len)
    plie_long = long.casefold()
    if not all(plie_long.startswith(c.casefold()) for c in candidats):
        return ""
    # Et, quand c'est la CLÉ qui est plus courte, n'accepter que si le
    # candidat la prolonge à une frontière de mot. Sans cela `id` propose
    # `idempotency_key` — un nom sans rapport dont `id` n'est le préfixe que
    # par accident de lettres, et que la garde de sécurité épinglée refuse
    # précisément (un `id` fourni sur un outil d'écriture est une tentative
    # d'écraser un document, pas une faute de frappe).
    if len(plie) < len(plie_long) and plie_long[len(plie)] != "_":
        return ""
    return long


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

    if isinstance(value, list):
        # Counted BEFORE the items are walked, and a count violation ends
        # the check: an oversized array would otherwise be validated item by
        # item and its per-item errors echoed back in one refusal — work and
        # a response both proportional to what the caller chose to send.
        count_errors: list[str] = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            count_errors.append(
                f"`{name}` must contain at least {schema['minItems']} "
                f"item(s) ({len(value)} given)"
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            count_errors.append(
                f"`{name}` must contain at most {schema['maxItems']} "
                f"items ({len(value)} given)"
            )
        if count_errors:
            errors.extend(count_errors)
            return errors

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
                    # NAME the supported arguments, and the near-miss when
                    # one is unambiguous. A refusal that says only what is
                    # wrong leaves the caller guessing; read_from_manifest
                    # already answered « Fichier inconnu. Fichiers
                    # disponibles : … » and the convention had simply never
                    # reached here.
                    #
                    # ⚠ This does NOT explain the 2026-08-31 `date_from1`
                    # incident, and an earlier draft of this comment claimed
                    # it did. Replayed against the live model, a refusal
                    # NAMING `date_from` gets « je dois utiliser date_from »
                    # in reply — and `date_from1` emitted again. That
                    # corruption is downstream of the model's intent and no
                    # wording can reach it; its cause was a keyword property
                    # name in a tool declaration. This is a
                    # plain improvement for ordinary wrong argument names,
                    # nothing more.
                    proche = _nearest_argument(key, properties)
                    detail = f"`{key}` is not a supported argument"
                    if proche:
                        detail += f" — did you mean `{proche}`?"
                    if properties:
                        detail += (
                            " (supported: "
                            + ", ".join(f"`{k}`" for k in sorted(properties))
                            + ")"
                        )
                    errors.append(detail)
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
    """idempotency_key — the shared write protocol (WP15).

    Fresh dict per usage (module rule). Every write tool splats this into
    its input schema; enforcement lives in mcp/write_support.run_write.

    ``dry_run`` used to sit here too and was REMOVED on 2026-08-27 (user
    decision). It had never been a control — nothing required it, nothing
    checked it, and a caller that omitted it simply wrote. It was a
    courtesy the model extended or withheld, and the first real batch
    showed both failure directions at once: the model previewed forty-five
    documents nobody asked it to preview, while no mechanism could have
    obliged it to preview anything else.

    Its cost was measured, not supposed. It doubled every write into two
    model calls, which is what killed a document-analysis batch on
    `chain_ceiling` after six documents out of forty-five. And it carried
    its own trap class: `run_write` short-circuited the dry branch WITHOUT
    calling the model, so every model-side guard had to be repeated in the
    handler ahead of it or a preview would promise a success the real call
    refused. Those guards remain — they refuse early, in French, naming the
    field — but the obligation that created them is gone.

    Proposing without performing is now expressed the honest way: do not
    call the tool, and describe the intended write. The charter and the
    scheduled addendum say exactly that. A proposal that runs no code
    cannot half-run.

    Removal is fail-CLOSED, which is what makes it safe: every write input
    schema carries ``additionalProperties: False``, so a caller still
    sending `dry_run` is REFUSED by :func:`validate_args` rather than
    silently written for — which would be the dangerous outcome.
    """
    return {
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


# The CONCURRENCY policy of an edit tool (plan rule 3) — declared per tool as
# the ``"concurrency"`` spec key, beside ``"idempotency"``, and read by the
# guards in tests/test_mcp_framework_guards.py. Every member of EDIT_TOOLS
# declares one; a read tool declares none.
#
# * ``optional`` — ``expected_etag`` is accepted, not demanded. When the
#   caller omits it, the handler still compare-and-sets against the etag it
#   has just read, so a write landing between that read and the commit is
#   refused rather than overwritten. The tool names, in ``"etag_readers"``,
#   the read tools whose rows carry the etag it expects.
# * ``required`` — ``expected_etag`` is demanded (content replacement,
#   outbound actions — plan rule 3), and listed in the input schema's
#   ``required``. ``decide_rendez_vous`` (the outbound one) declares it since
#   lot 1b. ``update_note`` and ``edit_analyse`` stay ``optional`` at the
#   spec level because only SOME of their calls replace content: their
#   handlers demand the etag exactly when they do (a note's ``content``,
#   the théorie's ``operations``/``full``) and refuse its absence.
# * ``exempt`` — the tool accepts no ``expected_etag``; ``"concurrency_reason"``
#   says why, in one sentence a reviewer can check.
CONCURRENCY_OPTIONAL = "optional"
CONCURRENCY_REQUIRED = "required"
CONCURRENCY_EXEMPT = "exempt"
CONCURRENCY_POLICIES: tuple[str, ...] = (
    CONCURRENCY_OPTIONAL, CONCURRENCY_REQUIRED, CONCURRENCY_EXEMPT,
)


def _expected_etag_prop(readers: tuple[str, ...]) -> dict:
    """``expected_etag`` — the optimistic-concurrency argument of an edit.

    Named ``expected_etag``, never ``etag``: a property literally called
    ``etag`` is forbidden on every write (test_mcp_tools), since it reads as
    « set the stored etag ». No ``minLength``: ``''`` is what the rows carry
    for a legacy record that never had an etag, and it must be sendable
    back. *readers* are the tools whose rows expose the etag — the same
    tuple the spec declares as ``"etag_readers"``, so the text a model reads
    and the contract the guards check cannot disagree.
    """
    return {
        "expected_etag": {
            "type": "string",
            "maxLength": 64,
            "description": (
                "The `etag` from your latest read of this record ("
                + " / ".join(readers)
                + ", or the last write result). If the record changed "
                "since — in the application, on the phone or through "
                "another call — the write is REFUSED and nothing is "
                "written: re-read, then retry."
            ),
        },
    }


# The read tools whose rows carry the etag an edit tool expects — declared
# on the edit as ``"etag_readers"`` and named in its ``expected_etag`` text.
_PARTIE_ETAG_READERS = ("get_partie", "list_parties")
_DOSSIER_ETAG_READERS = ("get_dossier", "list_dossiers")
_TIME_ENTRY_ETAG_READERS = ("list_time_entries",)
_EXPENSE_ETAG_READERS = ("list_expenses",)
_TASK_ETAG_READERS = ("list_tasks",)
_NOTE_ETAG_READERS = ("get_note", "list_notes")
# A protocol's etag is on its list_protocol_steps object; a step's on its
# row there, and on the get_agenda urgent-step rows.
_PROTOCOL_ETAG_READERS = ("list_protocol_steps",)
_STEP_ETAG_READERS = ("list_protocol_steps", "get_agenda")
# A calendar event's etag is on its list_hearings row and on the get_agenda
# hearing rows; a pending Bookings request is listed only by
# list_hearings (bookings: "pending").
_HEARING_ETAG_READERS = ("list_hearings", "get_agenda")
_RENDEZ_VOUS_ETAG_READERS = ("list_hearings",)


def _expected_etag_required_when(readers: tuple[str, ...], when: str) -> dict:
    """``expected_etag`` for a tool that DEMANDS it on some calls only.

    Declared ``optional`` in the registry — the other calls of the tool
    (a title change, the théorie's idempotent init) have no version to
    present, or cannot have one yet — and demanded by the HANDLER when
    *when* holds: a content replacement (plan rule 3, D8). The text says
    so first, so a model reads the condition before the generic remedy.
    """
    prop = _expected_etag_prop(readers)
    prop["expected_etag"]["description"] = (
        f"REQUIRED {when}. " + prop["expected_etag"]["description"]
    )
    return prop


def _expected_etag_only_for(readers: tuple[str, ...], which: str) -> dict:
    """``expected_etag`` for a tool whose OTHER calls have no version to
    present (manage_folder's create): the text names the calls it applies
    to first, and the handler refuses it on the others."""
    prop = _expected_etag_prop(readers)
    prop["expected_etag"]["description"] = (
        f"{which} only. " + prop["expected_etag"]["description"]
    )
    return prop


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

# Which tools mutate. DERIVED from the disclosure registry
# (mcp/disclosure.FAMILIES): a write tool belongs to exactly one family, and
# the family is what describes it — in INSTRUCTIONS and on the consent
# screen — so a new write tool cannot ship without declaring itself AND
# being disclosed. Enforcement (mcp/endpoint.py) and advertisement
# (list_tool_descriptors) both derive from this set. The literal pin in
# tests/test_mcp_tools.test_write_tools_set_is_pinned stays as a second
# tripwire, and tests/test_mcp_framework_guards (b) checks that membership
# and each tool's declared scope are the same fact.
WRITE_TOOLS: frozenset[str] = disclosure.write_tools()

# Writes that REPLACE a stored value rather than adding one. Lot Q ended the
# era where destructiveHint could be a family constant: « destructive » in
# the MCP spec means « may perform destructive updates », which is exactly
# what an edit does. Under-warning here is worse than over-warning — a
# client uses the hint to decide whether to confirm with the user first.
# Derived into the annotations below, never restated per tool.
EDIT_TOOLS: frozenset[str] = frozenset({
    # Il REMPLACE la catégorie stockée du document — et cette catégorie
    # est ce que l'explorateur affiche et ce sur quoi le juriste filtre.
    # Le journal garde la précédente, mais un remplacement reste un
    # remplacement : sous-avertir est le mauvais côté de l'erreur.
    "record_document_analysis",
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
    # complete_task REPLACES a task's stored status — and, through the
    # model's cascade, the linked protocol step's, up to closing the whole
    # protocol. It was left out while it was « the only status change »,
    # which under-warned: a client uses the hint to decide whether to
    # confirm with the user first (plan lot 0a, disclosure step).
    "complete_task",
    # Lot 1b — the agenda edits. Each REPLACES a stored value: a task's
    # fields, a task's status (and, by the cascade, its step's and
    # protocol's), a note's fields or whole body, blocs of the théorie de la
    # cause. The replaced prose of a note is kept (revisions); the rest is
    # not — which is why the hint matters.
    "update_task", "reopen_task", "update_note", "edit_analyse",
    # Lot 1b (L6) — the protocol edits. Each REPLACES a stored value: a
    # protocol's fields or status (and, by a new start date, the template
    # deadlines and the linked tasks' due dates), a step's fields or
    # status (and, by the cascade, its task's and its protocol's). The
    # two creators (create_protocol, add_protocol_step) replace nothing.
    "update_protocol", "update_protocol_step",
    # Lot 1b (L7) — the calendar. update_hearing REPLACES an event's fields
    # (its slot, its status — annulée removes the Outlook copy —, its
    # notes, which are not kept) and can move it or take it out of its
    # series — on a CONFIRMED Bookings rendez-vous only its dossier and its
    # notes (D10, 2026-09-27). decide_rendez_vous REPLACES a Bookings
    # request's decision gate, and its refusal cancels the client's Outlook
    # meeting: the one write here whose effect leaves the building
    # (OUTBOUND_TOOLS below).
    "update_hearing", "decide_rendez_vous",
    # Lot 2A (T7) — FILES. update_document REPLACES a document's filing
    # fields (its name, date, tags, folder, a presumed category) — none kept;
    # move_documents REPLACES each row's folder; manage_folder REPLACES a
    # folder's name or parent (its create replaces nothing, but one tool
    # carries one hint, and under-warning is the wrong side).
    "update_document", "move_documents", "manage_folder",
    # Lot 2A (T9) — finalize_upload's gabarit « replace » mode REPLACES a
    # stored template's file (the version in force: every future letter is
    # printed from it — the previous one is KEPT, restorable in the
    # application). Its document branch replaces nothing; one tool carries
    # one hint, and under-warning is the wrong side (review of lot 2).
    "finalize_upload",
    # Lot 2A (T10) — update_template REPLACES a template's name,
    # description, category or kind (none kept), or its file in force (the
    # previous version KEPT, restorable). create_template replaces nothing.
    "update_template",
})

# Writes with an effect OUTSIDE the practice's own records — a message a
# third party receives. MCP's openWorldHint is DERIVED from this set in
# list_tool_descriptors, never restated per tool (the override is refused
# by tests/test_mcp_framework_guards (d)). decide_rendez_vous's refusal
# cancels the Outlook meeting of a « Bookings with me » request, which
# notifies the client: the connector's first outbound effect (plan D10).
# A member must also declare idempotency AND concurrency « required »
# (plan rules 3 and 7), and the guards derive membership from the code: a
# tool whose closure reaches an outbound call is a member, and a member
# that reaches none is refused as a false alarm.
OUTBOUND_TOOLS: frozenset[str] = frozenset({"decide_rendez_vous"})

# Names that PROMISE an edit. A tool whose name starts with one of these must
# be in EDIT_TOOLS (destructiveHint) unless exempted with a reason in
# tests/test_mcp_framework_guards (c); an EDIT_TOOLS member without such a
# name (record_document_analysis, import_invoice, complete_task) is declared
# there with the reason it replaces or irreversibly flips a stored value.
# Deliberately NOT « complete_ » — complete_dossier fills empty fields only.
EDIT_NAME_PREFIXES: tuple[str, ...] = (
    "update_", "set_", "replace_", "rewrite_", "move_", "void_", "reopen_",
    "remove_", "reverse_", "clear_", "cancel_", "refuse_", "confirm_",
    "reschedule_", "unlink_", "detach_", "edit_",
)

# The idempotency POLICY of a write tool — declared per tool as the
# ``"idempotency"`` spec key and read by mcp.write_support.run_write, never
# passed by the handler, so the registry and the protocol cannot disagree.
#
# * ``optional`` — an ``idempotency_key`` is accepted, not demanded. The
#   store fails OPEN: a Firestore blip on the claim must not block a
#   legitimate first write. Every write tool but the two below is
#   ``optional``.
# * ``required`` — the key is demanded (a call without one is refused), and
#   the store fails CLOSED: an unreadable claim refuses the call rather than
#   risk a second write, and a failure before any commit keeps the claim
#   ``pending`` so a same-key retry is refused until the caller re-reads.
#   Reserved for money, outbound and series tools (plan rule 7): since lot
#   1b ``create_hearing_series`` (a series) and ``decide_rendez_vous`` (the
#   outbound one) declare it. Such a tool must also list ``idempotency_key``
#   in its input schema's ``required`` (pinned by test_mcp_framework_guards).
IDEMPOTENCY_OPTIONAL = "optional"
IDEMPOTENCY_REQUIRED = "required"
IDEMPOTENCY_POLICIES: tuple[str, ...] = (IDEMPOTENCY_OPTIONAL, IDEMPOTENCY_REQUIRED)


def idempotency_policy(name: str) -> str:
    """The declared idempotency policy of write tool *name*.

    Absent → ``optional`` (the historical posture). A value outside
    :data:`IDEMPOTENCY_POLICIES` — a typo in the registry — reads as
    ``required``: when the declaration cannot be trusted, the protocol
    takes the side that can never write twice. (The registry guard fails
    the build on such a typo long before it could matter.) An unregistered
    *name* raises ``KeyError``: run_write is keyed by the tool's own
    registered name, and a handler that is not is a bug to surface, not a
    default to guess.
    """
    value = TOOLS[name].get("idempotency", IDEMPOTENCY_OPTIONAL)
    return value if value in IDEMPOTENCY_POLICIES else IDEMPOTENCY_REQUIRED


# Per-call content ceiling, deliberately far below models.note's
# CONTENT_MAX_LENGTH (100_000). Two reasons: an oversized write is refused
# LOUDLY here (-32602) instead of being silently truncated by
# security.sanitize, and the gap leaves room for several appends before a
# note is full. ~20 000 chars ≈ a 3 500-word memo.
CONTENT_MAX_CHARS = 20_000
NOTE_TITLE_MAX_CHARS = 200

# A full REPLACEMENT of a note body (update_note) or of the théorie de la
# cause (edit_analyse `full`) must carry the whole note, so it gets the
# note's own scale — but strictly BELOW models.note.CONTENT_MAX_LENGTH
# (100 000): the handler prepends a one-line dated provenance stamp, and the
# assembled string is what must fit, never a truncated one (pinned by
# tests/test_mcp_agenda_writes.py). 99 000 characters at the worst JSON
# escaping (6 bytes) is ~594 KB — under the 1 MB /mcp request cap.
NOTE_REPLACE_MAX_CHARS = 99_000
# At most one operation per zone of the théorie (the entête + blocs A to H).
ANALYSE_OPERATIONS_MAX = 9

# Phase N — get_document_text: per-call character ceiling. The model PAGES
# through a long document (page_range + next_page) instead of receiving
# megabytes; 40 000 chars ≈ 15-20 dense pages per call.
DOCUMENT_TEXT_MAX_CHARS = 40_000
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
    "procès_verbal_signification", "procès_verbal_audience",
    "transcription", "mandat", "autre",
]
# Copied exactly from models.doc_template.VALID_KINDS / VALID_CATEGORIES
# (French). A gabarit's category is its OWN narrow taxonomy — never the
# documents one above. tests/test_mcp_template_reads.py pins both pairs; a
# literal, not an import, because importing models.* runs firestore.Client()
# at load.
_TEMPLATE_KINDS = ["gabarit", "note_honoraires", "note"]
_TEMPLATE_CATEGORIES = ["procédure", "correspondance", "autre"]
# How many folders list_documents(include_folders) returns — a dossier holds
# tens; the cap only bounds a pathological tree (folders_truncated says so).
FOLDER_TREE_MAX = 200
# Lot 2A (T7) — the document and folder edits. Literals copied from the
# models (an import would run firestore.Client() at load);
# tests/test_mcp_file_writes.py pins each against its model constant.
DOCUMENT_NAME_MAX_CHARS = 300      # models.document.DISPLAY_NAME_MAX
DOCUMENT_TAG_MAX_CHARS = 200       # models.document.TAG_MAX
# BELOW models.document.TAGS_MAX_ITEMS (30): the connector's list is the
# lawyer's filing vocabulary, and twenty is already more than a screen shows.
DOCUMENT_TAGS_MAX = 20
DOCUMENT_MOVE_MAX = 50             # models.document.MOVE_BULK_MAX
FOLDER_NAME_MAX_CHARS = 100        # models.folder.MAX_NAME_LENGTH
# The categories an EDIT may set: the vocabulary minus the legacy
# « procès_verbal », still readable and filterable but no longer offered at
# entry (models.document.CATEGORY_CHOICES).
_DOCUMENT_CATEGORY_CHOICES = [c for c in _DOCUMENT_CATEGORIES if c != "procès_verbal"]
# A document's etag is on its list_documents row; a folder's on the
# list_documents `folders` tree (include_folders).
_DOCUMENT_ETAG_READERS = ("list_documents",)
_FOLDER_ETAG_READERS = ("list_documents",)
# Lot 2A (T8) — the generations. Literals copied from their sources
# (services/gabarit_champs and models/document import the Firestore client
# at load); tests/test_mcp_generation.py pins each against its source.
BLOCS_MAX_ITEMS = 12                  # services.gabarit_champs.BLOCS_MAX_ITEMS
CHAMPS_MANUELS_MAX_ITEMS = 12         # …CHAMPS_MANUELS_MAX_ITEMS
BLOC_MAX_CHARS = 20_000               # …BLOC_MAX_CHARS
BLOCS_TOTAL_MAX_CHARS = 60_000        # …BLOCS_TOTAL_MAX_CHARS
CHAMP_MANUEL_MAX_CHARS = 2_000        # …CHAMP_MANUEL_MAX_CHARS
PLACEHOLDER_NAME_MAX_CHARS = 64
# A Markdown document is a whole text, not a bloc — bounded, like the blocs
# of one call, well under the 1 MB /mcp body cap.
MARKDOWN_DOCUMENT_MAX_CHARS = 60_000
DOCUMENT_TITLE_MAX_CHARS = 200
_CREATE_DOCUMENT_SOURCES = ["markdown", "copy"]
# Lot 2A (T9) — the upload ticket. Literals copied from the models (an
# import would run firestore.Client() at load); tests/test_mcp_upload.py
# pins each against its source.
UPLOAD_FILENAME_MAX_CHARS = 200                    # models.upload_ticket.MAX_FILENAME_CHARS
UPLOAD_DOCUMENT_MAX_BYTES = 200 * 1024 * 1024      # models.document.MAX_FILE_SIZE
UPLOAD_TEMPLATE_MAX_BYTES = 10 * 1024 * 1024       # models.doc_template.MAX_TEMPLATE_SIZE
UPLOAD_ACCEPT_MAX_ITEMS = 50                       # models.upload_ticket.MAX_LIST_ITEMS
UPLOAD_ACCEPT_MAX_CHARS = 200                      # its per-item ceiling
TEMPLATE_NAME_MAX_CHARS = 120                      # models.doc_template.NAME_MAX
TEMPLATE_DESCRIPTION_MAX_CHARS = 2_000             # models.doc_template.DESCRIPTION_MAX
_UPLOAD_PURPOSES = ["document", "gabarit"]
_TEMPLATE_MODES = ["create", "replace"]
# Lot 2A (T10) — a template's etag is on its list_templates row (list mode
# and detail mode alike: both render _template_summary).
_TEMPLATE_ETAG_READERS = ("list_templates",)
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
# Lot 1b (L6) — the protocol vocabularies, copied from models.protocol
# (VALID_PROTOCOL_TYPES, VALID_STATUSES, STEP_STATUS_TARGETS) for the same
# firestore-at-import reason; pinned against it by
# tests/test_mcp_protocol_writes.py.
_PROTOCOL_TYPES = ["cq_simplifié", "cs_ordinaire", "conventionnel"]
_PROTOCOL_STATUSES = ["actif", "suspendu", "complété"]
_STEP_STATUS_TARGETS = ["complété", "à_venir"]

# Lot 1b (L7) — the calendar vocabularies, copied from models.hearing
# (VALID_MODALITES, VALID_STATUSES, VALID_REMINDER_MINUTES) for the same
# firestore-at-import reason; pinned against it by
# tests/test_mcp_hearing_writes.py. The series frequencies and ceiling are
# DERIVED from utils/recurrence (pure), the phase precedent.
_HEARING_MODALITES = ["présentiel", "visioconférence", "téléphonique"]
_HEARING_STATUSES = [
    "confirmée", "à_confirmer", "reportée", "annulée", "terminée",
]
# A creation records a date that is either certain or still to confirm —
# never a postponement, a cancellation or an event already held.
_HEARING_CREATE_STATUSES = ["à_confirmer", "confirmée"]
_HEARING_REMINDERS = [15, 30, 60, 120, 1440, 2880, 10080]
_SERIES_FREQUENCIES = list(recurrence.VALID_FREQUENCIES)
_SERIES_MAX = recurrence.MAX_SERIE_OCCURRENCES
_RENDEZ_VOUS_ACTIONS = ["confirmer", "refuser"]

# Analyse documentaire — DÉRIVÉS eux aussi, du même précédent.
# `utils/analyse_taxonomies` est pur (aucun import de modèle, aucun client
# Firestore au chargement), donc l'enum ne peut pas dériver du vocabulaire
# que le modèle valide. ⚠ La CATÉGORIE n'est PAS un paramètre : elle est
# dérivée de la sous-nature par `nature_of()`. L'ajouter ici rouvrirait
# exactement ce que l'écart assumé avec la §5.3 rend supportable.
_SOUS_NATURE_CODES = sorted(_tax.VALID_SOUS_NATURES)
_PRIVILEGE_CODES = sorted(_tax.VALID_PRIVILEGES)
# Annexe C — les deux axes du droit de la preuve. L'ORDRE de la table est
# conservé (art. 2811 d'abord) : il se lit comme la disposition, et un tri
# alphabétique mettrait « AVEU » avant « ECRIT » sans raison.
_MOYEN_PREUVE_CODES = list(_tax.MOYENS_PREUVE)
_QUALIFICATION_ECRIT_CODES = list(_tax.QUALIFICATIONS_ECRIT)
_QUALITE_RECONNAISSANCE = list(_tax.QUALITES_RECONNAISSANCE)

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



def _hearing_modality_props() -> dict:
    """modalite / conference_uri / reminder_minutes — shared, fresh per
    usage, by create_hearing, create_hearing_series and update_hearing."""
    return {
        "modalite": {
            "type": "string",
            "enum": _HEARING_MODALITES,
            "description": (
                "présentiel (default at creation), visioconférence or "
                "téléphonique."
            ),
        },
        "conference_uri": {
            "type": "string",
            "maxLength": 2000,
            "description": (
                "Video link — http:// or https:// only (anything else is "
                "refused, naming this field). Shown on the phone and in "
                "Outlook for a visioconférence."
            ),
        },
        "reminder_minutes": {
            "type": "integer",
            "enum": _HEARING_REMINDERS,
            "description": "Reminder before the start, in minutes (1440 = 24 h, the default).",
        },
    }


def _hearing_create_props() -> dict:
    """The create_hearing arguments — shared, fresh per usage, by
    create_hearing and create_hearing_series (a series is N copies of ONE
    create_hearing prototype, so the two cannot describe it two ways)."""
    return {
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
            "description": "HH:MM Montréal. Omit for an all-day event.",
        },
        "end_time": {
            "type": "string",
            "maxLength": 5,
            "description": "HH:MM Montréal. Defaults to start + 1 h.",
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
        **_hearing_modality_props(),
        "status": {
            "type": "string",
            "enum": _HEARING_CREATE_STATUSES,
            "description": "à_confirmer (default) or confirmée when the date is certain.",
        },
    }

# Items per bulk reclassification call. Sized in the MAX_ZIP_FILES tradition
# — against gunicorn's 60 s SIGKILL, not against a round number: one batched
# read plus at most 50 serialized single-key updates is a few seconds. It
# also equals one `list_time_entries` page, so the read and write cadences
# line up. `minItems`/`maxItems` below are enforced by `validate_args` on the
# endpoint path since 2026-09-25 (they were declared and ignored until then).
# The handler still repeats both bounds, in French: a direct call skips the
# schema, and the house rule is that a handler never relies on it.
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
            "(judiciaire/extrajudiciaire), the modalité, a conference_uri for "
            "video events, and its etag (update_hearing). Two other modes, "
            "exclusive of the date window and of each other: `serie_id` lists "
            "one recurring chain whole (past included); `bookings` \"pending\" "
            "lists the « Bookings with me » requests awaiting a decision "
            "(decide_rendez_vous), with the requester's name and email."
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
                "serie_id": _id(
                    "List this recurring chain whole, past occurrences "
                    "included (from a row's serie_id). Not with the date "
                    "window, dossier_id or bookings."
                ),
                "bookings": {
                    "type": "string",
                    "enum": ["pending"],
                    "description": (
                        "\"pending\": the Bookings requests awaiting a "
                        "decision — à_confirmer, or annulée_client (the "
                        "client cancelled; refuser removes it). Not with the "
                        "date window, dossier_id or serie_id."
                    ),
                },
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
            "de la cause » (the lawyer's structured case analysis) — write "
            "it only with edit_analyse, never append_to_note/update_note."
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
            "Fetch one note with its full raw Markdown content and etag. For "
            "a note flagged is_analyse (the dossier's « Théorie de la "
            "cause »), `structure` maps its entête and blocs A–H; write it "
            "with edit_analyse only."
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
            "ONLY (display name, file name, the analysis summary, "
            "notes_internes, genere_depuis, tags), NEVER the text inside the "
            "file — read that with get_document_text (content SEARCH across "
            "files is still not available). Scope: one dossier by default "
            "(dossier_id required), or scope=\"cabinet\" across every "
            "dossier (folder_path is then \"\"). Filter by folder, "
            "category, `query` or a date window. Each row carries its "
            "dossier, folder_path (\"\" = dossier root), document_date — "
            "the document's OWN date when the lawyer entered one (null "
            "otherwise; created_at is only the upload instant) —, its etag, "
            "category_source (« mcp »: set by Claude, PRESUMED until the "
            "lawyer confirms) and folder_system_role. include_folders adds "
            "the dossier's whole folder tree."
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
                    "description": (
                        "Free-text match on the metadata the description "
                        "lists — never the file's text."
                    ),
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
                "include_folders": {
                    "type": "boolean",
                    "description": (
                        "Dossier scope only: also return the dossier's "
                        "COMPLETE folder tree in `folders` — empty folders "
                        "included, whatever folder_id, query or page. An "
                        "unreadable folder store or an unknown dossier_id "
                        "is REFUSED, never reported as « no folder ». "
                        "Default false."
                    ),
                },
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
    "list_templates": {
        "title": "Gabarits (modèles Word)",
        "description": (
            "The practice's Word templates (« gabarits ») — metadata only: "
            "never the file, its filename or a link. Without template_id: "
            "the list. With template_id: that template's placeholders, "
            "split three ways — AUTO fields the application fills itself "
            "(dossier, parties, firm, today's date), never yours to supply; "
            "MANUAL fields, short letter metadata with their options; BLOCS, "
            "the free content the template leaves to be written (names "
            "exact). Pass dossier_id (and the slot ids) to see which auto "
            "fields resolve on that file: an unresolved one prints "
            "« [CHAMP MANQUANT : name] », a data gap to report, not to "
            "write around. Values are NEVER returned — read get_dossier or "
            "get_partie for the data. Kinds note_honoraires and note are "
            "filled by their own flow (their fields: flow_fields); ONE of "
            "each is active, designated by the lawyer in the application — "
            "nothing here changes it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "Inspect ONE template (UUIDv4), from a row of the list. "
                    "Omit to list."
                ),
                "kind": {
                    "type": "string",
                    "enum": _TEMPLATE_KINDS,
                    "description": (
                        "List only: « gabarit » (letters, procedures), "
                        "« note_honoraires » (the invoice note), « note » "
                        "(the note print)."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": _TEMPLATE_CATEGORIES,
                    "description": "List only: the template's own category.",
                },
                "query": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "List only: matches name and description.",
                },
                "dossier_id": _id(
                    "With template_id: resolve the auto fields on this "
                    "dossier. Omitted, every dossier and party field reads "
                    "unresolved."
                ),
                "client_id": _id(
                    "With template_id: the dossier's client filling the "
                    "« client » slot. Needed when the dossier has several "
                    "and the template uses the slot. Refused for kinds "
                    "note / note_honoraires (their flow never fills it)."
                ),
                "adverse_id": _id(
                    "With template_id: the dossier's opposing party filling "
                    "the « adverse » slot. Needed when it has several. "
                    "Refused for kinds note / note_honoraires."
                ),
                "destinataire_id": _id(
                    "With template_id: the addressee contact of the "
                    "« destinataire » slot. No default. note_honoraires: "
                    "the invoice's client. Refused for kind note."
                ),
                "offset": _offset(),
                "limit": _limit(20),
            },
            "additionalProperties": False,
        },
        "handler": "list_templates",
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
            "Reconciliation: summing `balance` over status envoyée + "
            "en_retard (i.e. status_group=\"impayée\") equals "
            "get_billing_snapshot's outstanding_cents to the cent — the two "
            "share one definition, the LIVE balance. Never sum "
            "`amount_due` for this: it is frozen at issuance and stays at "
            "full value on a settled invoice, so it overstates what is "
            "owed by everything already collected. Reconciles only ACROSS "
            "ALL PAGES. While "
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
            "EVERY FINDING IS AN OBSERVATION, never an instruction: each "
            "`detail` says what to do in the application — and this "
            "connector never verifies an identity or a conflict. "
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
                        # Les vocabulaires de l'ANALYSE documentaire. Sans
                        # eux, les codes de `record_document_analysis` sont
                        # des énums nues : pas de libellé, pas d'ancrage
                        # légal, pas de niveau, pas de réserve.
                        "sous_natures", "privileges",
                        "moyens_preuve", "qualifications_ecrit",
                    ],
                    "description": (
                        "Which vocabulary to enumerate. The four analysis "
                        "ones feed `record_document_analysis`: "
                        "`sous_natures` (the 42 codes — the analysis "
                        "DERIVES the category from the one you pick), "
                        "`privileges` (7 codes, CUMULATIVE, each with its "
                        "protection LEVEL, its legal basis and its "
                        "reserve — what the code does NOT guarantee), and "
                        "the two Annexe C axes `moyens_preuve` / "
                        "`qualifications_ecrit`."
                    ),
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
            "are suspect and must be raised, not relied on. Each protocol "
            "carries the `etag` update_protocol expects and `closed_by` "
            "(« auto » = closed by its last step's completion); each step "
            "the `etag` update_protocol_step expects."
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
            "in French. The note syncs to the lawyer's phone; this "
            "connector can later edit it (update_note), never delete it. "
            "Confirm with the user before calling, and never call it on a "
            "dossier you have not read with get_dossier first. If the call "
            "appears to fail, check list_notes before retrying — there is no "
            "de-duplication and a retry creates a second note. Raw HTML tags "
            "are rejected (Markdown autolinks like <https://…> are converted "
            "automatically); write plain Markdown. Defaults to category "
            "'recherche'. Every note opens with a dated « Note rédigée par "
            "Claude le … » provenance line."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "append_to_note": {
        "title": "Ajouter du texte à une note existante",
        "description": (
            "WRITE. Append Markdown to the END of an existing note, under a "
            "dated « Ajouté par Claude » separator. Purely additive: existing "
            "content is never modified or removed here (replacing text is "
            "update_note). Like every change of a note's content, the note "
            "as it stood is kept in its revision history. Use get_note "
            "first to read what is already there. If the call appears to "
            "fail, re-read the note with get_note before retrying — a retry "
            "appends a second copy. "
            "Fails explicitly (rather than truncating) when the note would "
            "exceed its storage ceiling. Refuses the « Théorie de la cause » "
            "note (is_analyse true in list_notes/get_note) — write it with "
            "edit_analyse (mode append completes one bloc)."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_note": {
        "title": "Modifier une note",
        "annotations": {
            # Values already stored write nothing; a content replacement
            # demands the current etag, so a repeat is refused, not doubled.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — REPLACES the fields you name (a field you omit is "
            "untouched): title, category, pinned, content, dossier_id "
            "(MOVES the note, \"\" = « Général »; its phone copy follows). "
            "`content` replaces the WHOLE body: read it with get_note, edit, "
            "send it back complete, with `expected_etag`. The replaced text "
            "is kept in the note's revision history (as for any content "
            "change, in the app or on the phone too) and a dated « Révisée "
            "par Claude » line opens the new body. Text between angle "
            "brackets (raw HTML) is refused, never stripped; an over-long "
            "body is refused, never truncated. Refuses the « Théorie de la "
            "cause » (is_analyse): use edit_analyse. To add at the end, "
            "prefer append_to_note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "note_id": _id("The note to modify (UUIDv4), from list_notes."),
                "title": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_TITLE_MAX_CHARS,
                    "description": "New title, in French.",
                },
                "category": {
                    "type": "string", "enum": _NOTE_CATEGORIES,
                    "description": "New category.",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Pin (true) or unpin (false) the note.",
                },
                "content": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_REPLACE_MAX_CHARS,
                    "description": (
                        "The complete new Markdown body, in French (max "
                        f"{NOTE_REPLACE_MAX_CHARS} characters)."
                    ),
                },
                "dossier_id": _id(
                    "MOVE the note to this dossier (UUIDv4), or \"\" for "
                    "« Général ». An id that does not resolve is refused, "
                    "never downgraded."
                ),
                **_expected_etag_required_when(
                    _NOTE_ETAG_READERS, "when `content` is sent"),
                **_write_protocol_props(),
            },
            "required": ["note_id"],
            "additionalProperties": False,
        },
        "handler": "update_note",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _NOTE_ETAG_READERS,
    },
    "edit_analyse": {
        "title": "Rédiger la théorie de la cause",
        "annotations": {
            # The init finds and writes nothing twice; an edit demands the
            # current etag, so its repeat is refused, never applied twice.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — the dossier's « Théorie de la cause » (an entête, then "
            "8 blocs « ## Bloc A » … « ## Bloc H »). With neither "
            "`operations` nor `full`: creates it pre-seeded if absent "
            "(idempotent) and returns its structure and etag. `operations`: "
            "replace a bloc's body or append at its end, each bloc once; "
            "headings are kept, and the text may hold no level-1/2 heading. "
            "`full`: rewrites everything, keeping the eight headings once "
            "each, in order. Writing REQUIRES `expected_etag`; each write "
            "keeps the replaced text in the note's revision history and "
            "adds a dated « par Claude » line. Refuses (never guesses) when "
            "a heading is missing or doubled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier whose théorie de la cause to write (UUIDv4)."
                ),
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": ANALYSE_OPERATIONS_MAX,
                    "description": (
                        f"1 to {ANALYSE_OPERATIONS_MAX} bloc edits, applied "
                        "in order — whole list or nothing. Not with `full`."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "bloc": {
                                "type": "string",
                                "enum": ["entete", "A", "B", "C", "D", "E",
                                         "F", "G", "H"],
                                "description": (
                                    "« entete » (the text before bloc A) "
                                    "or a bloc letter."
                                ),
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["replace", "append"],
                                "description": (
                                    "replace = the bloc's whole body (\"\" "
                                    "empties it, bar the dated revision "
                                    "line); append = add at its end."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "maxLength": CONTENT_MAX_CHARS,
                                "description": "Markdown, in French.",
                            },
                        },
                        "required": ["bloc", "mode", "content"],
                        "additionalProperties": False,
                    },
                },
                "full": {
                    "type": "string", "minLength": 1,
                    "maxLength": NOTE_REPLACE_MAX_CHARS,
                    "description": (
                        "The whole new note (Markdown). Not with "
                        "`operations`."
                    ),
                },
                **_expected_etag_required_when(
                    _NOTE_ETAG_READERS,
                    "with `operations` or `full` (the note's id is "
                    "get_dossier's analyse_note_id)"),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "edit_analyse",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _NOTE_ETAG_READERS,
    },
    "complete_task": {
        "title": "Clore une tâche",
        "annotations": {
            # A second call with the same status writes nothing at all.
            "idempotentHint": True,
        },
        "description": (
            "Close a task: « terminée », « annulée », or move an OPEN task "
            "to « en_cours ». It never reopens a closed task (neither to "
            "« à_faire » nor to « en_cours ») — that is reopen_task — and "
            "never edits its fields (update_task) or deletes it. "
            "CASCADE, and read this before calling: completing a task that "
            "a protocol step is linked to ALSO completes that step, exactly "
            "as ticking the box in the application does — and if it was the "
            "last open step, THE WHOLE PROTOCOL closes and its deadlines "
            "stop appearing in get_agenda (reopen_task on the task reopens "
            "both). `protocol_step_effect` reports what actually happened "
            "to the step linked in the active protocol of the task's "
            "dossier, re-read after the write, never predicted. "
            "One asymmetry worth knowing: « annulée » triggers NO cascade, "
            "so the linked step stays open and keeps appearing in the "
            "agenda. « en_cours » on a task already « terminée » or "
            "« annulée » is REFUSED — that would reopen it. Rare: "
            "« en_cours » on an OPEN task whose linked step is still "
            "« complété » reopens that step, and a protocol its last step "
            "had closed — which `protocol_step_effect`, reading the active "
            "protocol only, does not show. Calling it on a task that already carries the requested status "
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
                        "absent, and « en_cours » is refused on a task "
                        "already closed: reopening is reopen_task."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Accepts no expected_etag (update_task and reopen_task do); the "
            "handler compare-and-sets against the task it has just read, so "
            "a change landing during the call is refused, never overwritten "
            "(a same-state call writes nothing)."
        ),
        "handler": "complete_task",
    },
    "update_task": {
        "title": "Modifier une tâche",
        "annotations": {
            # Values already stored write nothing: a replay is a no-op.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — REPLACES the fields you name; a field you omit is "
            "untouched, and values already stored write nothing. Never the "
            "status: close with complete_task, reopen with reopen_task. "
            "`description` replaces the WHOLE text and the old one is NOT "
            "kept — read it with list_tasks and send it back complete. "
            "`dossier_id` MOVES the task (\"\" = « Général ») and its phone "
            "copy follows; a task a protocol step links never moves "
            "(refused). Omitting both phase keys leaves the classification "
            "alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("The task to modify (UUIDv4), from list_tasks."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "New title, in French.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "New description — replaces the whole text; \"\" "
                        "empties it. Refused, never truncated, past 2000 "
                        "characters."
                    ),
                },
                "priority": {
                    "type": "string", "enum": _TASK_PRIORITIES,
                    "description": "New priority.",
                },
                "category": {
                    "type": "string", "enum": _TASK_CATEGORIES,
                    "description": "New category.",
                },
                "due_date": _date(
                    "New deadline, YYYY-MM-DD; \"\" removes it (an undated "
                    "task never reaches the urgent lists)."
                ),
                **_phase_props(),
                "dossier_id": _id(
                    "MOVE the task to this dossier (UUIDv4), or \"\" for "
                    "« Général ». An id that does not resolve is refused, "
                    "never downgraded."
                ),
                **_expected_etag_prop(_TASK_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "handler": "update_task",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TASK_ETAG_READERS,
    },
    "reopen_task": {
        "title": "Rouvrir une tâche",
        "annotations": {
            # The same target twice writes nothing at all.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — reopen a « terminée » task, or an « annulée » one with "
            "reopen_cancelled true (a cancellation is a decision: confirm "
            "with the user), to « à_faire » (default) or « en_cours »; it "
            "also puts an « en_cours » task back to « à_faire ». CASCADE: a "
            "linked protocol step that was « complété » reopens too, and "
            "so does its protocol when the cascade had closed it by itself. "
            "When the step cannot follow (protocol suspended or closed by "
            "hand, another protocol active) the call is REFUSED and nothing "
            "is written. `protocol_step_effect` reports what happened, "
            "re-read after the write. A task already in the requested state "
            "is a no-op."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": _id("The task to reopen (UUIDv4), from list_tasks."),
                "status": {
                    "type": "string",
                    "enum": ["à_faire", "en_cours"],
                    "description": (
                        "Default « à_faire ». To put an OPEN task "
                        "« en_cours », use complete_task."
                    ),
                },
                "reopen_cancelled": {
                    "type": "boolean",
                    "description": (
                        "Must be true to reopen an « annulée » task — "
                        "refused otherwise, naming this flag."
                    ),
                },
                **_expected_etag_prop(_TASK_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "handler": "reopen_task",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TASK_ETAG_READERS,
    },
    "create_protocol": {
        "title": "Créer un protocole",
        "description": (
            "WRITE — create a dossier's case protocol from a template: "
            "cq_simplifié (Cour du Québec simplified track: C.p.c. "
            "deadlines, locked), cs_ordinaire (Cour supérieure: suggested "
            "dates to confirm) or conventionnel (no steps — add them with "
            "add_protocol_step). A dossier has at most ONE actif protocol: "
            "refused while one exists (suspend or complete it first with "
            "update_protocol). A template whose C.p.c. regime does not "
            "govern the dossier's court is refused, naming the right one. "
            "Deadlines are computed from start_date (art. 83 C.p.c.). No "
            "task is created unless create_linked_tasks is true (one per "
            "step, synced to the phone, linked so that completing one "
            "completes the other). Returns each step's id, deadline, phase "
            "and etag, and the protocol's etag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier (UUIDv4). An id that does not resolve is "
                    "refused."),
                "protocol_type": {
                    "type": "string", "enum": _PROTOCOL_TYPES,
                    "description": (
                        "The template. Its regime must govern the "
                        "dossier's court (see regime_mismatch in "
                        "list_protocol_steps)."),
                },
                "start_date": _date(
                    "YYYY-MM-DD — the date the template's deadlines run "
                    "from (e.g. service of the originating application)."),
                "title": {
                    "type": "string", "maxLength": 200,
                    "description": (
                        "Default « Protocole de l'instance »."),
                },
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "Free notes on the protocol, in French.",
                },
                "create_linked_tasks": {
                    "type": "boolean",
                    "description": (
                        "Default false. true creates one task per step "
                        "(title, deadline, phase copied) on the dossier."),
                },
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "protocol_type", "start_date"],
            "additionalProperties": False,
        },
        "handler": "create_protocol",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_protocol": {
        "title": "Modifier un protocole",
        "annotations": {
            # Values already stored write nothing: a replay is a no-op.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — REPLACES the protocol fields you name; values already "
            "stored write nothing. A new start_date RECOMPUTES the "
            "template deadlines in the same write — except completed "
            "steps and CS dates the lawyer truly confirmed, which stay "
            "(`recompute`); a linked task still on its step's old date "
            "follows, one moved by hand stays (`linked_tasks`). status "
            "« suspendu » or « complété » takes the steps out of "
            "get_agenda and stops the task↔step cascade; « actif » is "
            "refused while another protocol of the dossier is actif. "
            "Never the type, the dossier or the steps "
            "(update_protocol_step). `notes` replaces without history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "The protocol (UUIDv4), from list_protocol_steps."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                    "description": "New title.",
                },
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "New notes — replaces the whole text.",
                },
                "court": {
                    "type": "string", "maxLength": 200,
                    "description": "The court the protocol runs before.",
                },
                "start_date": _date(
                    "New start date, YYYY-MM-DD — recomputes the template "
                    "deadlines (see description)."),
                "status": {
                    "type": "string", "enum": _PROTOCOL_STATUSES,
                    "description": (
                        "« suspendu » / « complété » close it (stamped "
                        "closed_by « mcp »); « actif » reactivates it."),
                },
                **_expected_etag_prop(_PROTOCOL_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["protocol_id"],
            "additionalProperties": False,
        },
        "handler": "update_protocol",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PROTOCOL_ETAG_READERS,
    },
    "add_protocol_step": {
        "title": "Ajouter une étape de protocole",
        "description": (
            "WRITE — add a custom step at the end of an ACTIVE protocol "
            "(refused on a suspended or completed one: reactivate it with "
            "update_protocol first). A custom step is never mandatory nor "
            "locked, and its deadline is yours to set — compute it with "
            "compute_judicial_deadline. create_linked_task true also "
            "creates a task with the step's title, deadline and phase, "
            "synced to the phone and linked to the step."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "The protocol (UUIDv4), from list_protocol_steps."),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "Step title, in French.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": "What the step requires.",
                },
                "cpc_reference": {
                    "type": "string", "maxLength": 200,
                    "description": "E.g. « art. 246 C.p.c. ».",
                },
                "deadline_date": _date("YYYY-MM-DD. Omit for no deadline."),
                **_phase_props(),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": "Free notes on the step.",
                },
                "create_linked_task": {
                    "type": "boolean",
                    "description": "Default false — see description.",
                },
                **_write_protocol_props(),
            },
            "required": ["protocol_id", "title"],
            "additionalProperties": False,
        },
        "handler": "add_protocol_step",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_protocol_step": {
        "title": "Modifier une étape de protocole",
        "annotations": {
            # Same values, or the same target status, write nothing.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — either REPLACE fields of a step (deadline_date, "
            "notes, phase; title, description and cpc_reference on a "
            "custom step only — a template step's C.p.c. text and a CQ "
            "locked deadline are refused, naming the field) or set its "
            "`status` — never both in one call (two writes, not atomic). "
            "`status` is a TARGET, never a toggle: « complété » or "
            "« à_venir »; the state it already has writes nothing. "
            "CASCADE: the linked task follows (an annulée one is left "
            "alone); completing the last open step closes the WHOLE "
            "protocol; reopening a step of a protocol that closed that way "
            "reactivates it (refused if another protocol is actif). "
            "Otherwise refused on a protocol that is not actif. A changed "
            "deadline carries along a linked task still on the old date "
            "(`linked_task`), and confirms a CS date — one sent back "
            "unchanged stays a suggestion (confirm it in the application). "
            "`status_change` is re-read after the write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "protocol_id": _id(
                    "The step's protocol (UUIDv4), from list_protocol_steps."),
                "step_id": _id("The step (UUIDv4)."),
                "deadline_date": _date(
                    "New deadline, YYYY-MM-DD; \"\" clears it (refused on "
                    "a template step)."),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "New notes — replaces the whole text, no history."),
                },
                **_phase_props(),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "New title — custom steps only.",
                },
                "description": {
                    "type": "string", "maxLength": 2000,
                    "description": "New description — custom steps only.",
                },
                "cpc_reference": {
                    "type": "string", "maxLength": 200,
                    "description": "New reference — custom steps only.",
                },
                "status": {
                    "type": "string", "enum": _STEP_STATUS_TARGETS,
                    "description": (
                        "The state wanted — alone in its call (see "
                        "description)."),
                },
                **_expected_etag_prop(_STEP_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["protocol_id", "step_id"],
            "additionalProperties": False,
        },
        "handler": "update_protocol_step",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _STEP_ETAG_READERS,
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
            "can later edit (update_task), close (complete_task) or reopen "
            "(reopen_task) it but never delete it, and it syncs to the "
            "lawyer's phone. Confirm with the user before calling; use "
            "an idempotency_key on every "
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
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
            "event is created à_confirmer unless you pass status confirmée, "
            "and syncs to the lawyer's phone. Correct or reschedule it "
            "later with update_hearing; it cannot be deleted here. For a "
            "recurring event use create_hearing_series. Confirm with the "
            "user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_hearing_create_props(),
                **_write_protocol_props(),
            },
            "required": ["title", "date"],
            "additionalProperties": False,
        },
        "handler": "create_hearing",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_hearing_series": {
        "title": "Créer une série d'événements",
        "description": (
            "WRITE — create a RECURRING series: N ordinary events sharing "
            "a serie_id, written in ONE atomic batch (all or nothing) and "
            "synced to the phone. Same arguments as create_hearing, plus "
            "`frequency` and EXACTLY one bound: `count` (1 to "
            f"{_SERIES_MAX}) or `until` (YYYY-MM-DD, inclusive — refused, "
            f"never truncated, past {_SERIES_MAX} occurrences). Each "
            "occurrence keeps the Montréal wall-clock hour across "
            "daylight-saving changes and is never moved off a weekend or "
            "holiday. `idempotency_key` is REQUIRED. Change one occurrence "
            "with update_hearing (detach_from_series takes it out of the "
            "chain); list a chain with list_hearings(serie_id). Confirm "
            "with the user before calling."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_hearing_create_props(),
                "frequency": {
                    "type": "string",
                    "enum": _SERIES_FREQUENCIES,
                    "description": "How often the event repeats.",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _SERIES_MAX,
                    "description": (
                        "Number of occurrences, the first one included. "
                        "Not with `until`."
                    ),
                },
                "until": _date(
                    "Last possible day, YYYY-MM-DD (inclusive). Not with "
                    "`count`."
                ),
                **_write_protocol_props(),
            },
            "required": ["title", "date", "frequency", "idempotency_key"],
            "additionalProperties": False,
        },
        "handler": "create_hearing_series",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
    },
    "update_hearing": {
        "title": "Modifier un événement du calendrier",
        # NOT idempotentHint, unlike update_task / update_note: values
        # already stored write nothing, but `notes_append` appends AGAIN on
        # every identical call — so « calling repeatedly with the same
        # arguments has no additional effect » would be false for one of its
        # arguments, and the hint is what a client trusts before a blind
        # retry. The family default (false) is the honest one here.
        "description": (
            "WRITE — REPLACES the fields you name; a field you omit is "
            "untouched, and values already stored write nothing. "
            "Reschedule with date / start_time / end_time (Montréal): an "
            "omitted hour keeps the stored wall-clock hour, an omitted end "
            "keeps the duration. `status` annulée drops the event from "
            "get_agenda and removes its Outlook copy at the next 10-minute "
            "cycle (the phone shows it cancelled). `dossier_id` MOVES it "
            "(\"\" = « Général ») — a series occurrence moves only with "
            "detach_from_series true. A pending or refused Bookings request "
            "is refused here (decide_rendez_vous). On a CONFIRMED Bookings "
            "rendez-vous ONLY dossier_id, notes and notes_append are "
            "accepted: rescheduling, cancelling or any other change is "
            "REFUSED, because the Outlook meeting the client holds is the "
            "reference — a reschedule or a cancellation is made in Outlook "
            "(the Bookings sync then flags it in Réception); anything else "
            "the lawyer edits in the application, whose form is left free: "
            "only this connector is restricted. Replaced notes are NOT kept."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "The event to modify (UUIDv4), from list_hearings or "
                    "get_agenda."
                ),
                "title": {
                    "type": "string", "minLength": 1, "maxLength": 300,
                    "description": "New title, in French.",
                },
                "hearing_type": {
                    "type": "string", "enum": _HEARING_TYPES,
                    "description": "New type (the forum derives from it).",
                },
                "date": _date(
                    "New day, YYYY-MM-DD (Montréal). Omitted: the stored day."
                ),
                "start_time": {
                    "type": "string", "maxLength": 5,
                    "description": (
                        "New start, HH:MM Montréal; makes the event timed. "
                        "Required to turn an all-day event into a timed one."
                    ),
                },
                "end_time": {
                    "type": "string", "maxLength": 5,
                    "description": (
                        "New end, HH:MM Montréal, same day, after the start. "
                        "Omitted: the stored duration."
                    ),
                },
                "all_day": {
                    "type": "boolean",
                    "description": (
                        "true: an all-day event (then no start_time or "
                        "end_time); false: a timed one (needs start_time if "
                        "it was all-day)."
                    ),
                },
                "location": {
                    "type": "string", "maxLength": 300,
                    "description": "New location; \"\" empties it.",
                },
                "court": {
                    "type": "string", "maxLength": 200,
                    "description": "New court; \"\" empties it.",
                },
                "judge": {
                    "type": "string", "maxLength": 200,
                    "description": "New judge; \"\" empties it.",
                },
                **_hearing_modality_props(),
                "notes": {
                    "type": "string", "maxLength": 2000,
                    "description": (
                        "REPLACES the whole notes (\"\" empties them); the "
                        "old text is NOT kept. Refused, never truncated, "
                        "past 2000 characters. Not with notes_append."
                    ),
                },
                "notes_append": {
                    "type": "string", "minLength": 1, "maxLength": 1500,
                    "description": (
                        "Appended under a dated « Ajouté par Claude » line; "
                        "the notes as stored must still fit 2000 characters "
                        "(refused otherwise). A repeated call appends again "
                        "— retry only with the same idempotency_key. Not "
                        "with notes."
                    ),
                },
                "status": {
                    "type": "string", "enum": _HEARING_STATUSES,
                    "description": (
                        "New status. terminée is refused on a future day."
                    ),
                },
                "detach_from_series": {
                    "type": "boolean",
                    "description": (
                        "true: this occurrence leaves its series and becomes "
                        "a standalone event, in the same write as the other "
                        "fields (a no-op when it is in no series)."
                    ),
                },
                "dossier_id": _id(
                    "MOVE the event to this dossier (UUIDv4), or \"\" for "
                    "« Général ». An id that does not resolve is refused, "
                    "never downgraded."
                ),
                **_expected_etag_prop(_HEARING_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["hearing_id"],
            "additionalProperties": False,
        },
        "handler": "update_hearing",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _HEARING_ETAG_READERS,
    },
    "decide_rendez_vous": {
        "title": "Confirmer ou refuser un rendez-vous Bookings",
        "annotations": {
            # The same decision twice writes nothing and never contacts the
            # client again.
            "idempotentHint": True,
        },
        "description": (
            "WRITE, OUTBOUND — decide a pending « Bookings with me » "
            "request (list them with list_hearings, bookings \"pending\"). "
            "`confirmer`: the event enters the calendar and the phone; with "
            "lier_partie (default true) the contact whose email is EXACTLY "
            "the requester's is linked, and the call is refused when none "
            "matches (resend with lier_partie false). A request the client "
            "cancelled cannot be confirmed. `refuser` CANCELS THE OUTLOOK "
            "MEETING, WHICH NOTIFIES THE CLIENT, with a fixed text you "
            "cannot change; a request the client cancelled is only removed. "
            "An already-CONFIRMED rendez-vous is not refused here: it is "
            "cancelled in Outlook. "
            "`expected_etag` (from that listing) and `idempotency_key` are "
            "REQUIRED; the same decision twice writes nothing and never "
            "contacts the client again. Always confirm with the user first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hearing_id": _id(
                    "The pending request (UUIDv4), from list_hearings with "
                    "bookings \"pending\"."
                ),
                "action": {
                    "type": "string", "enum": _RENDEZ_VOUS_ACTIONS,
                    "description": (
                        "confirmer (accept) or refuser (decline — notifies "
                        "the client)."
                    ),
                },
                "lier_partie": {
                    "type": "boolean",
                    "description": (
                        "confirmer only (default true): link the contact "
                        "matched server-side on the requester's exact email."
                    ),
                },
                **_expected_etag_prop(_RENDEZ_VOUS_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": [
                "hearing_id", "action", "expected_etag", "idempotency_key",
            ],
            "additionalProperties": False,
        },
        "handler": "decide_rendez_vous",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_REQUIRED,
        "concurrency": CONCURRENCY_REQUIRED,
        "etag_readers": _RENDEZ_VOUS_ETAG_READERS,
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
            "defaults to the dossier's hourly rate. It can be corrected "
            "with update_time_entry only while not yet invoiced (its "
            "litigation phase with set_time_entry_phase, even after); this "
            "connector can never delete it. Confirm with the user before "
            "calling."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_expense": {
        "title": "Créer un déboursé",
        "description": (
            "WRITE. Record a disbursement (débours) on a dossier — "
            "bailiff, expert, transcript, filing stamp… dossier_id is "
            "REQUIRED. The description prints verbatim on the client's "
            "invoice: billing narrative in French, no internal notes. "
            "Amount in integer cents. It can be corrected with "
            "update_expense only while not yet invoiced (its litigation "
            "phase with set_expense_phase, even after); this connector can "
            "never delete it. Confirm with the user before calling."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "complete_dossier": {
        "title": "Compléter les champs vides d'un dossier",
        "description": (
            "WRITE, fill-only-if-empty. Fill dossier fields that are EMPTY "
            "or still at their model default — a computed classification, "
            "value in dispute, prescription starting point… A field that "
            "already carries a different value is NEVER overwritten: the "
            "whole call is refused listing the conflicting fields, and "
            "nothing is written (changing a set value is update_dossier's "
            "job). Filling court_file_number also derives the "
            "judicial metadata exactly as the web form does. Money in "
            "integer cents; contingency_percent in basis points "
            "(2500 = 25 %); dates YYYY-MM-DD. Confirm with the user "
            "before calling; the response carries the resulting "
            "prescription picture."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
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
            "edit or remove a recorded event. Confirm with the user "
            "before calling."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_time_entry": {
        "title": "Corriger une entrée de temps",
        "description": (
            "WRITE — REPLACES the values you name; a field you omit is "
            "untouched. Correct a transcription error BEFORE the entry is "
            "invoiced: once it is, neither this connector nor the "
            "application can correct it — only its litigation phase stays "
            "reclassifiable (set_time_entry_phase) — and the only way back "
            "is voiding the invoice in the application (which releases "
            "every source). "
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
                **_expected_etag_prop(_TIME_ENTRY_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["time_entry_id"],
            "additionalProperties": False,
        },
        "handler": "update_time_entry",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TIME_ENTRY_ETAG_READERS,
    },
    "update_expense": {
        "title": "Corriger un déboursé",
        "description": (
            "WRITE — REPLACES the values you name; a field you omit is "
            "untouched. Same wall as update_time_entry: once the "
            "disbursement is invoiced nothing here can correct it — only "
            "its litigation phase stays reclassifiable (set_expense_phase). "
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
                **_expected_etag_prop(_EXPENSE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
        "handler": "update_expense",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _EXPENSE_ETAG_READERS,
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
                **_expected_etag_prop(_TIME_ENTRY_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["time_entry_id"],
            "additionalProperties": False,
        },
        "handler": "set_time_entry_phase",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TIME_ENTRY_ETAG_READERS,
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
                **_expected_etag_prop(_EXPENSE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["expense_id"],
            "additionalProperties": False,
        },
        "handler": "set_expense_phase",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _EXPENSE_ETAG_READERS,
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "A batch carries no per-row token yet (an item-level "
            "expected_etag can come later); each row is still compared "
            "and set against the etag the handler has just read."
        ),
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Same as set_time_entry_phase_bulk: each row is compared and "
            "set against the etag the handler has just read."
        ),
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
            "Compare the returned subtotal, GST and "
            "QST against the PDF: the totals are computed over the real "
            "sources, never estimated. "
            "The invoice lands in BROUILLON and stays there — this connector "
            "never sets an invoice status and never records a payment. "
            "Billing the sources freezes them: nothing here can modify them "
            "afterwards except their litigation phase (set_time_entry_phase "
            "/ set_expense_phase), and the only way back is voiding the "
            "invoice in the application, which releases every source — the "
            "number itself stays on the voided invoice until the lawyer "
            "deletes that invoice there."
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Creates an invoice and replaces no value it names; the sources "
            "it flips are re-read and etag-compared inside the invoice's own "
            "transaction."
        ),
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
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
                **_expected_etag_prop(_DOSSIER_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["dossier_id"],
            "additionalProperties": False,
        },
        "handler": "update_dossier",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _DOSSIER_ETAG_READERS,
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
        "idempotency": IDEMPOTENCY_OPTIONAL,
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
                **_expected_etag_prop(_PARTIE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["partie_id"],
            "additionalProperties": False,
        },
        "handler": "update_partie",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _PARTIE_ETAG_READERS,
    },
    # ════════════════════════════════════════════════════════════════════
    # Lecture du CONTENU d'un document (2026-08). L'écran de consentement
    # nomme le fait que le contenu intégral d'une pièce transite par
    # claude.ai — c'est la divulgation qui rend cet outil acceptable.
    # ════════════════════════════════════════════════════════════════════
    "get_document_text": {
        "title": "Texte d'un document",
        "description": (
            "Read a stored document's TEXT LAYER — the reading companion of "
            "list_documents (take document_id from there, or from the entity "
            "a file write returned; a template is not a document). PDF and "
            ".docx "
            "only; a scanned or image-only page has NO text layer and is "
            "reported honestly (has_text false, listed in "
            "pages_without_text) — nothing is OCR'd and empty never means "
            "the page is blank on paper. Output is bounded per call "
            f"({DOCUMENT_TEXT_MAX_CHARS} characters): follow next_page with "
            "page_range to continue (pages for a PDF, computed segments for "
            "a .docx). Files over the extraction ceiling, encrypted PDFs, "
            ".doc/images/zip and other formats are refused with a French "
            "message — consult those in the application. The content is "
            "privileged legal material: quote only what the task requires, "
            "and NEVER put any of it into a web_search query."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "The document to read (UUIDv4), from list_documents."
                ),
                "page_range": {
                    "type": "string",
                    "maxLength": 12,
                    "description": (
                        "\"4\" = from page 4 onward; \"2-6\" = that range "
                        "(1-based, inclusive). Omit for page 1 onward. To "
                        "continue, pass the previous response's next_page "
                        "as a single number."
                    ),
                },
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "handler": "get_document_text",
    },
    "record_document_analysis": {
        "title": "Enregistrer l'analyse d'un document",
        "description": (
            "WRITE — RECORDS a document analysis you have already performed, "
            "and REPLACES the document's stored category. Read the text with "
            "get_document_text FIRST; never analyse from a filename. You "
            "supply a `sous_nature` from the CLOSED table and the CODE "
            "derives the category — you cannot choose or invent one here, "
            "and it replaces a PRESUMED category a FILES tool set. The "
            "result becomes visible in the application: category badge, "
            "protection level, summary. It is marked PRESUMED until the "
            "lawyer confirms it on screen; nothing you send here can confirm "
            "it. Every run is journalled for ever and the previous category "
            "is kept, so a replacement stays observable. Privileges are "
            "CUMULATIVE and the code fails UPWARD: a protection level is "
            "never lowered by a re-analysis. Never claim PUBLIC to fill a "
            "gap — absence of a protection marker is not a marker of public "
            "character. Names stay free strings; never resolve one to a "
            "contact. A retry without idempotency_key records a SECOND "
            "journal entry (nothing is lost, but it is noise)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "The document analysed (UUIDv4), from list_documents."
                ),
                "sous_nature": {
                    "type": "string",
                    "enum": _SOUS_NATURE_CODES,
                    "description": (
                        "Closed sub-nature code (Annexe A). The stored "
                        "category is DERIVED from it — there is no category "
                        "parameter, by design. Never invent a code: if none "
                        "fits, say so instead of calling."
                    ),
                },
                "privileges": {
                    "type": "array",
                    "items": {"type": "string", "enum": _PRIVILEGE_CODES},
                    "description": (
                        "Protection regimes, CUMULATIVE (Annexe D). Under "
                        "doubt between two, send BOTH — the highest governs. "
                        "An empty list means « nothing established », which "
                        "the code treats as confidential by default, never "
                        "as public."
                    ),
                },
                "resume": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "One short paragraph, in French, of what the "
                        "document IS and says. Shown in the application."
                    ),
                },
                "numero_dossier_cour": {
                    "type": "string",
                    "description": (
                        "Court file number as it appears ON the document, "
                        "verbatim. Omit when absent — an absence is a SIGNAL "
                        "(a possibly undeposited draft), never a gap to fill "
                        "by deduction."
                    ),
                },
                "tribunal": {
                    "type": "string",
                    "description": (
                        "Court named on the document. Omit if absent."
                    ),
                },
                "district_judiciaire": {
                    "type": "string",
                    "description": (
                        "Judicial district on the document. Omit if absent."
                    ),
                },
                "auteur": {
                    "type": "string",
                    "description": (
                        "Signatory, bailiff, judge, clerk or stenographer "
                        "named on the document. Omit if absent."
                    ),
                },
                "parties_mentionnees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Party names as WRITTEN. FREE STRINGS — never "
                        "resolve one to a contact of the dossier: linking "
                        "the wrong contact is worse than not linking, and "
                        "would propagate in silence."
                    ),
                },
                "date_document_str": {
                    "type": "string",
                    "description": (
                        "Date ON the document, YYYY-MM-DD. Omit if absent."
                    ),
                },
                "date_signature_str": {
                    "type": "string",
                    "description": (
                        "Signature date, YYYY-MM-DD. Omit if absent."
                    ),
                },
                "contient_dispositif": {
                    "type": "boolean",
                    "description": (
                        "True when a procès-verbal d'audience carries the "
                        "judgment itself. SIGNAL it — a judgment rendered at "
                        "the hearing starts appeal delays — but never "
                        "compute the delay."
                    ),
                },
                "dispositif": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "The operative wording, verbatim, when present."
                    ),
                },
                "indices_protection": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "What you OBSERVED that grounds the privileges, in "
                        "plain words (« en-tête d'avocat », « mention sous "
                        "toutes réserves »). This is what lets the lawyer "
                        "check your reasoning."
                    ),
                },
                "langue_detectee": {
                    "type": "string",
                    "enum": ["fr", "en", "autre"],
                    "description": "Main language of the document.",
                },
                "confiance": {
                    "type": "string",
                    "enum": ["haute", "moyenne", "faible"],
                    "description": (
                        "Your confidence in this classification. Say "
                        "« faible » rather than guessing well."
                    ),
                },
                "extraction_tronquee": {
                    "type": "boolean",
                    "description": (
                        "True when you read only part of the text "
                        "(pagination not followed to the end)."
                    ),
                },
                # ── Annexe C — the two axes of evidence law ──────────
                # These four were readable and editable but had NO input
                # property, so the model could not supply them and they
                # stayed empty on every analysis. Spotted in production
                # 2026-08-27 on a TAL decision.
                "moyen_preuve": {
                    "type": "string",
                    "enum": _MOYEN_PREUVE_CODES,
                    "description": (
                        "By WHICH MEANS this document could prove a fact "
                        "(art. 2811 C.c.Q.). A procedural act proves "
                        "nothing by itself — it carries the burden, it is "
                        "not evidence — so leave this out for a "
                        "`procédure`. NON_DETERMINE when you cannot tell; "
                        "never guess."
                    ),
                },
                "qualification_ecrit": {
                    "type": "string",
                    "enum": _QUALIFICATION_ECRIT_CODES,
                    "description": (
                        "WHAT KIND of writing, and ONLY when moyen_preuve "
                        "is ECRIT — the call is refused otherwise. This is "
                        "a qualification with consequences (an acte "
                        "authentique proves itself until inscription de "
                        "faux, art. 2813-2814): say NON_DETERMINE unless "
                        "the document itself establishes it."
                    ),
                },
                "parait_original": {
                    "type": "boolean",
                    "description": (
                        "Whether the document APPEARS to be the original "
                        "rather than a copy (art. 2860 C.c.Q.). An "
                        "observation, never a conclusion — a wet "
                        "signature, a seal, an original stamp. Omit when "
                        "nothing in the document says."
                    ),
                },
                "qualite_reconnaissance": {
                    "type": "string",
                    "enum": _QUALITE_RECONNAISSANCE,
                    "description": (
                        "How well the text came through, for a scan or a "
                        "photograph. This MEASURES a need rather than "
                        "qualifying the document: accumulated « faible » "
                        "is what would justify a dedicated OCR pass."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["document_id", "sous_nature"],
            "additionalProperties": False,
        },
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Adopts expected_etag with the document edit tools (plan lot "
            "2); the analysis never lowers a protection level, so an "
            "overwrite cannot under-protect."
        ),
        "handler": "record_document_analysis",
    },
    # ── Lot 2A (T7) — FILES: the document and folder edits ─────────────
    "update_document": {
        "title": "Classer un document",
        "annotations": {
            # Values already stored write nothing: a repeat is a no-op.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — REPLACES the filing fields you name on one document (an "
            "omitted field is untouched): display_name, document_date, tags, "
            "folder_id (refiles it), category. Never its file, and never "
            "notes_internes — the lawyer's own text. A category you set is "
            "stored PRESUMED until the lawyer confirms it in the "
            "application; on a document that carries an analysis the "
            "category derives from it and is refused here (use "
            "record_document_analysis). Values already stored write nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": _id(
                    "The document (UUIDv4), from list_documents."
                ),
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "New display name, in French. Text between angle "
                        "brackets is refused, never stripped."
                    ),
                },
                "document_date": _date(
                    "The document's OWN date, YYYY-MM-DD; \"\" clears it. A "
                    "malformed date is refused, never read as a clearing."
                ),
                "tags": {
                    "type": "array",
                    "maxItems": DOCUMENT_TAGS_MAX,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": DOCUMENT_TAG_MAX_CHARS,
                        "description": "One tag, without a comma.",
                    },
                    "description": (
                        "The COMPLETE new tag list — it replaces the stored "
                        "one; [] clears it. No comma in a tag (the "
                        "application splits tags on commas), none twice."
                    ),
                },
                "folder_id": _id(
                    "Refile into this folder of the SAME dossier (id from "
                    "list_documents with include_folders), or \"\" for the "
                    "dossier root."
                ),
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "The category, stored PRESUMED (category_source "
                        "« mcp »). Refused on an analysed document."
                    ),
                },
                **_expected_etag_prop(_DOCUMENT_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "handler": "update_document",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _DOCUMENT_ETAG_READERS,
    },
    "move_documents": {
        "title": "Déplacer des documents",
        "annotations": {
            # A second identical call finds every row already filed there:
            # « unchanged », nothing written.
            "idempotentHint": True,
        },
        "description": (
            f"WRITE — refile up to {DOCUMENT_MOVE_MAX} documents of ONE "
            "dossier into one folder, or its root, in a single atomic write. "
            "`results` answers each id IN REQUEST ORDER: moved, unchanged "
            "(already there — nothing written) or refused with its reason (an "
            "unknown id, another dossier's document); a refused row blocks "
            "none of the others. An unknown target folder, an id named twice "
            "or an unreadable store refuses the whole call — nothing moves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dossier_id": _id(
                    "The dossier the documents belong to (UUIDv4)."
                ),
                "document_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": DOCUMENT_MOVE_MAX,
                    "items": {
                        "type": "string", "minLength": 1, "maxLength": 64,
                        "description": "A document id, from list_documents.",
                    },
                    "description": (
                        "The documents to refile, each once. The report "
                        "follows this order."
                    ),
                },
                "folder_id": _id(
                    "The target folder of this dossier (list_documents with "
                    "include_folders), or \"\" for the dossier root."
                ),
                **_write_protocol_props(),
            },
            "required": ["dossier_id", "document_ids", "folder_id"],
            "additionalProperties": False,
        },
        "handler": "move_documents",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "A batch carries no per-row token; ONE transaction reads every "
            "row and the target folder, so a write landing meanwhile re-runs "
            "it on fresh data — and it writes folder_id alone."
        ),
    },
    "manage_folder": {
        "title": "Organiser les dossiers de classement",
        "description": (
            "WRITE — one folder of a dossier's filing tree. action \"create\": "
            "name, and parent_folder_id (\"\" or omitted = dossier root); a "
            "name already taken there is refused — or, with if_exists "
            "\"reuse\", that folder is returned. \"rename\": folder_id + name. "
            "\"move\": folder_id + parent_folder_id (\"\" = root); never into "
            "itself or a subfolder, 5 levels deep at most. The system folders "
            "« Projets » and « Reçus du portail » are the application's: never "
            "renamed, moved or recreated here, and their names are reserved "
            "at the root. A name is refused, never altered (no « / », "
            "« \\ », angle brackets or control character). Nothing is ever "
            "deleted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["create", "rename", "move"],
                    "description": "What to do.",
                },
                "dossier_id": _id(
                    "The dossier whose filing tree changes (UUIDv4)."
                ),
                "folder_id": _id(
                    "rename / move: the folder, from list_documents with "
                    "include_folders. Refused with create."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": FOLDER_NAME_MAX_CHARS,
                    "description": "create / rename: the name, in French.",
                },
                "parent_folder_id": _id(
                    "create: where (\"\" or omitted = the dossier root). "
                    "move: the new parent, REQUIRED (\"\" = the root)."
                ),
                "if_exists": {
                    "type": "string", "enum": ["refuse", "reuse"],
                    "description": (
                        "create only: when that name is already taken there, "
                        "\"refuse\" (default) or \"reuse\" — the existing "
                        "folder is returned, untouched."
                    ),
                },
                **_expected_etag_only_for(
                    _FOLDER_ETAG_READERS, "rename / move"),
                **_write_protocol_props(),
            },
            "required": ["action", "dossier_id"],
            "additionalProperties": False,
        },
        "handler": "manage_folder",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _FOLDER_ETAG_READERS,
    },
    # ── Lot 2A (T8) — FILES: new Word documents, never a changed one ────
    "fill_gabarit": {
        "title": "Remplir un gabarit",
        "description": (
            "WRITE — fill a gabarit (kind « gabarit ») for ONE dossier and "
            "save the .docx as a NEW document in its « Projets » folder. "
            "Call list_templates with template_id (and this dossier_id) "
            "first: supply only the blocs and manual fields it reports, "
            "names exact; every other field is filled by the application "
            "from the dossier, its parties and the firm — never by you. "
            "Write allegations as plain paragraphs separated by a BLANK "
            "LINE, unnumbered: each inherits the gabarit's own Word "
            "numbering (a single newline becomes a space). markdown true "
            "only for internal formatting (headings, bold, a table). A text "
            "holding « {{ » or « }} » is refused. The result is a draft, "
            "never sent; a retry without idempotency_key saves a second one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "The gabarit (UUIDv4), from list_templates — kind "
                    "« gabarit » only."
                ),
                "dossier_id": _id(
                    "REQUIRED: the dossier whose data fills the gabarit and "
                    "whose « Projets » receives the document (no download "
                    "path here)."
                ),
                "client_id": _id(
                    "The « client » slot: one of THIS dossier's clients "
                    "(get_dossier). Needed when it has several and the "
                    "gabarit reads client.*."
                ),
                "adverse_id": _id(
                    "The « adverse » slot: one of THIS dossier's opposing "
                    "parties. Same rule as client_id."
                ),
                "destinataire_id": _id(
                    "The addressee (any contact, list_parties). No default."
                ),
                "blocs": {
                    "type": "array",
                    "maxItems": BLOCS_MAX_ITEMS,
                    "description": (
                        "The content blocks, one per bloc name. A bloc you "
                        "omit stays « {{name}} » for the lawyer to write in "
                        "Word."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "nom": {
                                "type": "string", "minLength": 1,
                                "maxLength": PLACEHOLDER_NAME_MAX_CHARS,
                                "description": (
                                    "The bloc's name without braces, exactly "
                                    "as list_templates reports it (case "
                                    "counts)."
                                ),
                            },
                            "contenu": {
                                "type": "string", "minLength": 1,
                                "maxLength": BLOC_MAX_CHARS,
                                "description": (
                                    "The text, in French. Paragraphs "
                                    "separated by a blank line."
                                ),
                            },
                            "markdown": {
                                "type": "boolean",
                                "description": (
                                    "true = Word formatting from Markdown "
                                    "(headings, bold, lists, tables). "
                                    "Default false; never for numbered "
                                    "allegations."
                                ),
                            },
                        },
                        "required": ["nom", "contenu"],
                        "additionalProperties": False,
                    },
                },
                "champs_manuels": {
                    "type": "array",
                    "maxItems": CHAMPS_MANUELS_MAX_ITEMS,
                    "description": (
                        "The manual fields list_templates reports. Omitted "
                        "→ its default, else « [À COMPLÉTER : name] »."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "nom": {
                                "type": "string", "minLength": 1,
                                "maxLength": PLACEHOLDER_NAME_MAX_CHARS,
                                "description": (
                                    "The field's name without braces, exact."
                                ),
                            },
                            "valeur": {
                                "type": "string",
                                "maxLength": CHAMP_MANUEL_MAX_CHARS,
                                "description": (
                                    "Its value — one of its option values "
                                    "when list_templates lists options."
                                ),
                            },
                        },
                        "required": ["nom", "valeur"],
                        "additionalProperties": False,
                    },
                },
                **_write_protocol_props(),
            },
            "required": ["template_id", "dossier_id"],
            "additionalProperties": False,
        },
        "handler": "fill_gabarit",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "create_document": {
        "title": "Créer un document Word",
        "description": (
            "WRITE — add a NEW Word document to a dossier; an existing "
            "document is never changed. source \"markdown\": your Markdown "
            "(title + markdown), printed on the note-print template the "
            "lawyer designated ACTIVE — refused when none is; a `category` "
            "you give is stored PRESUMED. source \"copy\": a copy of a "
            "stored .docx (document_id), in ITS OWN dossier only — reuse "
            "across dossiers goes through a gabarit; the copy keeps the "
            "source's category and protection level. Both land in "
            "« Projets » unless folder_id names another folder (\"\" = the "
            "dossier root). A retry without idempotency_key creates a "
            "second document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string", "enum": _CREATE_DOCUMENT_SOURCES,
                    "description": "What the document is made from.",
                },
                "dossier_id": _id(
                    "markdown: REQUIRED, the dossier. copy: optional — if "
                    "given, it must be the source's own dossier."
                ),
                "title": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_TITLE_MAX_CHARS,
                    "description": (
                        "markdown: REQUIRED, the title (French) — the "
                        "template's {{note.titre}} and the document's name."
                    ),
                },
                "markdown": {
                    "type": "string", "minLength": 1,
                    "maxLength": MARKDOWN_DOCUMENT_MAX_CHARS,
                    "description": (
                        "markdown: REQUIRED, the body. Raw HTML and angle "
                        "brackets are refused; write links as [text](url)."
                    ),
                },
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "markdown: stored PRESUMED. Omitted: the template's "
                        "own category."
                    ),
                },
                "document_date": _date(
                    "markdown: the document's OWN date, YYYY-MM-DD."
                ),
                "document_id": _id(
                    "copy: REQUIRED, the stored .docx to copy "
                    "(list_documents)."
                ),
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "copy: the copy's name. Default « Copie de … »."
                    ),
                },
                "folder_id": _id(
                    "A folder of the dossier (list_documents with "
                    "include_folders); \"\" = the root. Omitted: « Projets »."
                ),
                **_write_protocol_props(),
            },
            "required": ["source"],
            "additionalProperties": False,
        },
        "handler": "create_document",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    # ── Lot 2A (T9) — FILES: the upload ticket (plan D4) ────────────────
    # begin_upload's `upload_url` is the ONE capability URL a tool output
    # carries (the documented exception to « no signed URL in output »):
    # its persist/rehydrate hooks (mcp/handlers.py) keep it out of
    # mcp_idempotency, and a replay re-opens a session for the SAME ticket.
    "begin_upload": {
        "title": "Ouvrir un téléversement",
        "description": (
            "WRITE — step 1 of bringing an outside file in: a one-hour, "
            "WRITE-ONLY upload ticket. purpose \"document\": a new document "
            "of dossier_id, its filing given NOW (folder_id — omitted = the "
            "dossier root —, category stored PRESUMED, display_name, "
            "document_date, tags). purpose \"gabarit\": template_mode "
            "\"create\" (name, category, kind, description — a special kind "
            "is never designated active) or \"replace\" (template_id + "
            "expected_version from list_templates; the version in force is "
            "kept). Declare the file's exact size_bytes and md5_base64. "
            "upload_url is a capability: PUT the exact bytes to it from your "
            "code sandbox in ONE request with its headers, use it only in "
            "that code — never repeat it to the user —, then call "
            "finalize_upload. Needs sandbox egress to storage.googleapis.com. "
            "A template taken from a dossier: name that dossier_id, and its "
            "names and numbers are refused (accept_residual on the lawyer's "
            "word only)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "purpose": {
                    "type": "string", "enum": _UPLOAD_PURPOSES,
                    "description": "What the file becomes.",
                },
                "filename": {
                    "type": "string", "minLength": 1,
                    "maxLength": UPLOAD_FILENAME_MAX_CHARS,
                    "description": (
                        "The file's name with its extension (PDF, Word, "
                        "Excel, JPG, PNG, TIFF, ZIP, EML, MSG; .docx for a "
                        "gabarit). No slash."
                    ),
                },
                "size_bytes": {
                    "type": "integer", "minimum": 1,
                    "maximum": UPLOAD_DOCUMENT_MAX_BYTES,
                    "description": (
                        "The EXACT size in bytes (a gabarit: 10 MB at most)."
                    ),
                },
                "md5_base64": {
                    "type": "string", "minLength": 24, "maxLength": 24,
                    "description": (
                        "base64 of the raw 16-byte MD5 digest of the bytes "
                        "(base64.b64encode(hashlib.md5(data).digest())) — "
                        "never the hex form."
                    ),
                },
                "dossier_id": _id(
                    "document: REQUIRED, the dossier. gabarit: the dossier "
                    "the file comes from, whose identifiers are then refused."
                ),
                "folder_id": _id(
                    "document: a folder of the dossier (list_documents with "
                    "include_folders); omitted or \"\" = the root."
                ),
                "category": {
                    "type": "string", "enum": _DOCUMENT_CATEGORY_CHOICES,
                    "description": (
                        "document: stored PRESUMED. gabarit create: "
                        "procédure, correspondance or autre (default)."
                    ),
                },
                "display_name": {
                    "type": "string", "minLength": 1,
                    "maxLength": DOCUMENT_NAME_MAX_CHARS,
                    "description": (
                        "document: its name. Default: the file name."
                    ),
                },
                "document_date": _date(
                    "document: its OWN date, YYYY-MM-DD."
                ),
                "tags": {
                    "type": "array",
                    "maxItems": DOCUMENT_TAGS_MAX,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": DOCUMENT_TAG_MAX_CHARS,
                        "description": "One tag, without a comma.",
                    },
                    "description": "document: its tags, none twice.",
                },
                "template_mode": {
                    "type": "string", "enum": _TEMPLATE_MODES,
                    "description": (
                        "gabarit: REQUIRED — a new template, or a new file "
                        "for an existing one."
                    ),
                },
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "gabarit create: REQUIRED, its name — printed in "
                        "every generated document's name, so checked like "
                        "the file against dossier_id."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "gabarit create: what it is for.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "gabarit create: default « gabarit ». A special kind "
                        "is created NOT active."
                    ),
                },
                "template_id": _id(
                    "gabarit replace: REQUIRED, the template (list_templates)."
                ),
                "expected_version": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "gabarit replace: REQUIRED, its version as read — "
                        "refused if another version landed since."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "One identifier exactly as a refusal named it."
                        ),
                    },
                    "description": (
                        "gabarit: residues the LAWYER accepts to keep in a "
                        "firm-wide template."
                    ),
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "gabarit: empty the file's title, subject, author, "
                        "last editor and description first (default false)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["purpose", "filename", "size_bytes", "md5_base64"],
            "additionalProperties": False,
        },
        "handler": "begin_upload",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "finalize_upload": {
        "title": "Verser un fichier téléversé",
        "annotations": {
            # A ticket already filed answers its stored result again and
            # writes nothing — even without an idempotency_key.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — step 2: files the bytes PUT under ticket_id. They enter "
            "only if their size and MD5 are exactly what begin_upload bound; "
            "other bytes are refused and discarded. document → a NEW "
            "document. gabarit → checked against the named dossier's "
            "identifiers, then a new template or a NEW version of the one "
            "named (the replaced version is kept). Nothing received yet → "
            "refused, the ticket stays open: PUT, then call again. A ticket "
            "already filed answers its result again — never upload that "
            "file anew. A refused or expired ticket never reopens: begin "
            "anew with a NEW idempotency_key."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": _id("The ticket_id begin_upload returned."),
                **_write_protocol_props(),
            },
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
        "handler": "finalize_upload",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_EXEMPT,
        "concurrency_reason": (
            "Everything is bound at begin_upload — a replacement's "
            "expected_version included, re-checked in the template's own "
            "transaction — and a ticket is claimed transactionally: one "
            "finalizer at a time, with no etag to pass."
        ),
    },
    # ── Lot 2A (T10) — TEMPLATES from a document already in a dossier ───
    # Interpretation A (plan D5): the stored .docx is registered UNCHANGED
    # (bar the opt-in core-properties scrub). Turning its literals into
    # {{…}} fields is lot 2B's preview_templatize / substitutions.
    "create_template": {
        "title": "Enregistrer un document comme gabarit",
        "description": (
            "WRITE — registers a Word document ALREADY in a dossier (.docx, "
            "10 MB at most, id from list_documents) as a NEW firm-wide "
            "template, its bytes unchanged — scrub_properties first empties "
            "the file's title, subject, author, last editor and description. "
            "Refused while the file or `name` still carries the source "
            "dossier's names, numbers or addresses: each residue is named; "
            "list it in accept_residual ONLY on the lawyer's word, and every "
            "accepted one is echoed back. A special kind (note_honoraires, "
            "note) is created NOT active: only the lawyer designates the "
            "active one, in the application. The source document is never "
            "modified. Fields Word fragmented come back in "
            "entity.validation_warnings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_document_id": _id(
                    "The stored .docx (UUIDv4), from list_documents. Its "
                    "OWN dossier is the one checked."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "Its name, in French — printed in the name of every "
                        "document generated from it, so checked like the "
                        "file. Never a client's name."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "What it is for, in French.",
                },
                "category": {
                    "type": "string", "enum": _TEMPLATE_CATEGORIES,
                    "description": "The template's OWN category.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "Default « gabarit ». A special kind is created NOT "
                        "active."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "One identifier exactly as a refusal named it."
                        ),
                    },
                    "description": (
                        "Residues the LAWYER accepts to keep in a firm-wide "
                        "template."
                    ),
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "Empty the file's document properties first "
                        "(default false)."
                    ),
                },
                **_write_protocol_props(),
            },
            "required": ["source_document_id", "name", "category"],
            "additionalProperties": False,
        },
        "handler": "create_template",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
    },
    "update_template": {
        "title": "Corriger un gabarit",
        "annotations": {
            # Values already stored — or a file identical to the version in
            # force — write nothing: a repeat is a no-op.
            "idempotentHint": True,
        },
        "description": (
            "WRITE — corrects ONE template, in one of two calls. METADATA: "
            "name, description, category, kind — an omitted field is "
            "untouched; the kind of the ACTIVE template of a special kind "
            "cannot change. Or a new FILE: source_document_id (a .docx "
            "already in a dossier) + expected_version from list_templates — "
            "installed as a NEW version, the one in force KEPT and "
            "restorable in the application; refused while the file names "
            "the source dossier (accept_residual on the lawyer's word only). "
            "A new file for the active template prints at once on every "
            "document of its kind. Never changes which template is active. "
            "Values or a file already stored write nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_id": _id(
                    "The template (UUIDv4), from list_templates."
                ),
                "name": {
                    "type": "string", "minLength": 1,
                    "maxLength": TEMPLATE_NAME_MAX_CHARS,
                    "description": (
                        "Metadata: its new name, in French — printed in the "
                        "name of every generated document: never a client's."
                    ),
                },
                "description": {
                    "type": "string",
                    "maxLength": TEMPLATE_DESCRIPTION_MAX_CHARS,
                    "description": "Metadata: what it is for; \"\" clears it.",
                },
                "category": {
                    "type": "string", "enum": _TEMPLATE_CATEGORIES,
                    "description": "Metadata: the template's OWN category.",
                },
                "kind": {
                    "type": "string", "enum": _TEMPLATE_KINDS,
                    "description": (
                        "Metadata: a special kind is never made active here."
                    ),
                },
                "source_document_id": _id(
                    "File: the stored .docx (UUIDv4, list_documents) whose "
                    "bytes become the new version."
                ),
                "expected_version": {
                    "type": "integer", "minimum": 1,
                    "description": (
                        "File: REQUIRED — the version as read; refused if "
                        "another version landed since."
                    ),
                },
                "accept_residual": {
                    "type": "array",
                    "maxItems": UPLOAD_ACCEPT_MAX_ITEMS,
                    "items": {
                        "type": "string", "minLength": 1,
                        "maxLength": UPLOAD_ACCEPT_MAX_CHARS,
                        "description": (
                            "One identifier exactly as a refusal named it."
                        ),
                    },
                    "description": "File: residues the LAWYER accepts to keep.",
                },
                "scrub_properties": {
                    "type": "boolean",
                    "description": (
                        "File: empty the file's document properties first."
                    ),
                },
                **_expected_etag_prop(_TEMPLATE_ETAG_READERS),
                **_write_protocol_props(),
            },
            "required": ["template_id"],
            "additionalProperties": False,
        },
        "handler": "update_template",
        "scope": SCOPE_WRITE,
        "idempotency": IDEMPOTENCY_OPTIONAL,
        "concurrency": CONCURRENCY_OPTIONAL,
        "etag_readers": _TEMPLATE_ETAG_READERS,
    },
}


# The accounting tools (plan decision D1): trust and administration register
# entries, behind their OWN scope and their own kill switch. DERIVED from the
# declared scope, never listed by hand — a tool joins by declaring
# ``"scope": SCOPE_COMPTABILITE``, which is also what gates it at
# tools/call. Every member is ALSO a member of WRITE_TOOLS (pinned by
# tests/test_mcp_framework_guards: a non-read scope IS the write gate), so the
# write protocol, the write audit, the write-time token revalidation and the
# master switch MCP_WRITE_ENABLED all reach it by construction.
#
# EMPTY until plan lot 5: the scope, the switch and the consent box ship
# dormant, and nothing lands under them before the model fixes they rely on.
ACCOUNTING_TOOLS: frozenset[str] = frozenset(
    name for name, spec in TOOLS.items()
    if spec.get("scope") == SCOPE_COMPTABILITE
)

# The kill switches a tool can be off under, by their environment names —
# what the tools/call refusal says, so the operator reads the variable to
# flip rather than a paraphrase of it.
WRITE_SWITCH = "MCP_WRITE_ENABLED"
COMPTABILITE_SWITCH = "MCP_COMPTABILITE_ENABLED"


def required_scope(name: str) -> str:
    """Scope a tool needs. Unlisted tools default to read — never to write.

    An accounting tool needs ``athena:comptabilite`` and ONLY that (plus the
    read baseline every /mcp call demands): ``athena:write`` does not stand
    in for it, so a token granted writes alone never reaches one.
    """
    return TOOLS[name].get("scope", SCOPE_READ)


def unavailable_reason(name: str) -> Optional[str]:
    """The kill switch that keeps *name* off, or ``None`` when it is live.

    Two switches, nested. ``MCP_WRITE_ENABLED`` is the master: it governs
    every write, accounting included, and it is the one NAMED when both are
    off — it is off, and no other change will bring the tool back while it
    stays off. ``MCP_COMPTABILITE_ENABLED`` governs the accounting subset
    alone. A read tool is never switched off here (``MCP_ENABLED`` 404s the
    whole endpoint instead, upstream of any tool).
    """
    if name in WRITE_TOOLS and not write_enabled():
        return WRITE_SWITCH
    if name in ACCOUNTING_TOOLS and not comptabilite_enabled():
        return COMPTABILITE_SWITCH
    return None


def tool_available(name: str) -> bool:
    """False when a kill switch keeps *name* off (see :func:`unavailable_reason`).

    The tools/list filter reads this, and the tools/call gate reads
    :func:`unavailable_reason` — the same decision, so a tool is never
    advertised yet refused, nor hidden yet callable.
    """
    return unavailable_reason(name) is None


def list_tool_descriptors(granted: Optional[frozenset[str]] = None) -> list[dict]:
    """Registry entries in MCP tools/list wire format, filtered by scope.

    A read-only connection must not see the write tools, nor a write
    connection the accounting tools: advertising them would have the client
    model call one and take a 403 on every attempt, and ``_forbidden`` does
    not feed the failure brake — an unthrottled refusal loop. The filter is
    by each tool's OWN scope, so the two grants are independent: read +
    comptabilite shows the reads and the accounting tools, and nothing of
    athena:write. ``granted=None`` means "no scope filtering" (tests, docs)
    — the kill switches still apply.
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
        # DERIVED from OUTBOUND_TOOLS: a tool whose effect reaches a third
        # party (the Outlook cancellation a refused Bookings request sends
        # the client) interacts with an « open world » in the spec's sense;
        # every other tool touches the practice's own records only.
        annotations["openWorldHint"] = name in OUTBOUND_TOOLS
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
