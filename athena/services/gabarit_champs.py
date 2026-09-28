"""The fields of a generation from a gabarit — the READ half (lot 2A, T6).

What a generation needs before it writes anything: the dossier and the
party slots (:func:`resolve_slots`), the auto values resolved server-side
(:func:`resolve_auto_values`), the placeholder inventory
(:func:`field_inventory` — kinds and RESOLVED FLAGS, never a value) or the
popup's prefilled fields (:func:`form_fields`), the check of a
submitted form (:func:`values_from_submission`), and the connector's fill
values (:func:`values_for_connector`, lot 2A T8 — the server's auto values
plus the blocs and manual fields Claude wrote). Nothing here writes: the
fill and the save into « Projets » live in :mod:`services.gabarits`, which
re-exports every name of this module so the web route keeps ONE import.

Split out of ``services/gabarits.py`` for the connector's ``list_templates``
(the preview of what a fill would resolve). The « never » sweep of
``tests/test_mcp_disclosure.py`` reads every service a connector module
imports WHOLE — a module that names ``upload_document`` or
``ensure_system_folder`` breaks the « document » promise even if the tool
only calls its readers. So the read half is its own module, and a read
tool that imports it provably reaches no writer. ``fill_gabarit`` (a
later step) imports the write half and changes the promise with it.

See :mod:`services.gabarits` for the pipeline and the refusal contract:
every refusal is a :class:`GenerationRefused` with a machine-stable
``reason``, a French ``message`` that never quotes a value, and the
``field`` it concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Collection, Mapping, Optional

from models.dossier import get_dossier
from models.partie import get_partie, get_parties_bulk
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.template_fields import (
    CATALOG,
    classify_placeholders,
    fallback_value,
    manual_options,
    manual_spec,
    manual_value,
    resolve_values,
)

# ── Ceilings ─────────────────────────────────────────────────────────────
#
# The longest value a submitted field may carry, per kind. A value past its
# ceiling is REFUSED (reason ``value_too_long``), never cut.
#
# * A MANUAL field is short letter metadata (objet, référence, pièces
#   jointes): the historical 2 000.
# * An AUTO field prints stored case data, and the longest single stored
#   field the catalog reads is ``dossier.sommaire`` (5 000 —
#   ``models.dossier._SOMMAIRE_MAX_LENGTH``; every other dossier and partie
#   string is capped at 2 000). A composite the catalog BUILDS (an address
#   line, a list of party names) can exceed any one field: its ceiling is
#   then its own resolved length — what the server itself would print is
#   never refused. ``tests/test_gabarit_service.py`` pins the 5 000 to the
#   model's cap.
# * A MULTI-LINE value (a party block, a multi-paragraph sommaire): 20 000
#   — five parties with their addresses already exceed 2 000 — or, for an
#   auto field, its resolved length when longer.
MANUAL_MAX_CHARS = 2000
AUTO_MAX_CHARS = 5000
MULTILINE_MAX_CHARS = 20000

# The prefix the popup names its value inputs with (``champ__{name}``).
FIELD_PREFIX = "champ__"

# French messages of the pre-existing refusals — kept VERBATIM from the
# route they come from (the web says exactly what it said before the hoist).
DOSSIER_NOT_FOUND = (
    "Le dossier sélectionné est introuvable. Fermez la fenêtre et réessayez."
)

_SLOT_LABELS = {
    "client": "Le client choisi",
    "adverse": "La partie adverse choisie",
    "destinataire": "Le destinataire choisi",
}
_SLOTS = ("client", "adverse", "destinataire")


class GenerationRefused(Exception):
    """A generation that must not proceed — nothing was written.

    ``reason`` is machine-stable (the ``generation_failed`` vocabulary),
    ``message`` is French and never quotes a submitted value, ``field``
    names what the refusal concerns: a slot parameter (``client_id`` …),
    ``dossier_id``, or a placeholder name.

    The messages are the POPUP's wording (« rouvrez la fenêtre », kept
    verbatim for the refusals that predate the hoist); a surface without a
    window — the connector — phrases its own from ``reason`` and ``field``.
    """

    def __init__(self, reason: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.field = field


# ── 1. Slots ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotResolution:
    """The dossier and the party slots a generation fills from."""

    dossier: Optional[dict] = None
    dossier_id: str = ""
    client: Optional[dict] = None
    client_id: str = ""
    adverse: Optional[dict] = None
    adverse_id: str = ""
    destinataire: Optional[dict] = None
    destinataire_id: str = ""
    # The dossier's own entries (name snapshots) — what the popup's selects
    # offer — and every party document, for the role-scoped blocks.
    clients: list = dc_field(default_factory=list)
    opposing_parties: list = dc_field(default_factory=list)
    parties: dict = dc_field(default_factory=dict)


def _first_id(entries: Optional[list]) -> str:
    if entries:
        return entries[0].get("id", "") or ""
    return ""


def _entry_ids(entries: Optional[list]) -> list[str]:
    return [e.get("id") for e in (entries or []) if e.get("id")]


def dossier_parties(dossier: Optional[dict]) -> dict:
    """Every party of *dossier*, loaded in ONE round-trip: {id: partie doc}.

    The dossier's clients[]/opposing_parties[] entries are name snapshots
    with no address, so the role-scoped blocks need the documents
    themselves. ``get_parties_bulk`` is a keyed ``db.get_all`` (no index)
    and fails OPEN to ``{}`` — which degrades a block to names alone, never
    to an error: this is a document-rendering aid, not a register.
    """
    if not dossier:
        return {}
    ids = _entry_ids(dossier.get("clients")) + _entry_ids(
        dossier.get("opposing_parties"))
    return get_parties_bulk(ids) if ids else {}


def _refuse_slot(slot: str, reason: str, *, detail: str) -> GenerationRefused:
    return GenerationRefused(
        reason, f"{_SLOT_LABELS[slot]} {detail}", field=f"{slot}_id")


def resolve_slots(
    dossier_id: str = "",
    client_id: str = "",
    adverse_id: str = "",
    destinataire_id: str = "",
    *,
    lenient: bool = False,
    required_slots: Collection[str] = (),
    refuse_ambiguous: bool = False,
) -> SlotResolution:
    """Resolve the generation's dossier and party slots.

    STRICT (the default — every path that produces a document) raises
    :class:`GenerationRefused`:

    * ``dossier_not_found`` — *dossier_id* names no dossier;
    * ``slot_foreign`` — a *client_id* or *adverse_id* that is not among the
      dossier's clients / opposing parties (an adverse slot without a
      dossier included: there is nothing it could be on);
    * ``slot_unknown`` — a *destinataire_id* (any contact) or, without a
      dossier, a *client_id* (the free pick) that names no contact;
    * ``slot_ambiguous`` — only when *refuse_ambiguous*: a slot of
      *required_slots* omitted while the dossier offers several candidates
      (the connector must name its choice; the popup shows its default).

    An omitted client or adverse slot defaults to the dossier's first entry.
    A slot the DOSSIER vouches for whose contact document fails to load is
    not refused: its fields resolve to nothing and print the visible
    « [CHAMP MANQUANT : …] » marker, as they always did.

    ``lenient=True`` is the popup render's selection repair (see the module
    docstring) and reproduces the pre-hoist render: an unknown
    dossier reads as none, a foreign slot is reset to the dossier's first
    entry, an unknown contact is dropped.
    """
    dossier_id = (dossier_id or "").strip()
    client_id = (client_id or "").strip()
    adverse_id = (adverse_id or "").strip()
    destinataire_id = (destinataire_id or "").strip()

    dossier = get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        if dossier_id and not lenient:
            raise GenerationRefused("dossier_not_found", DOSSIER_NOT_FOUND,
                                    field="dossier_id")
        dossier_id = ""

    clients = (dossier or {}).get("clients", [])
    opposing = (dossier or {}).get("opposing_parties", [])
    if dossier:
        client_id = _dossier_slot(
            "client", client_id, clients, lenient=lenient,
            ambiguous=refuse_ambiguous and "client" in required_slots,
        )
        adverse_id = _dossier_slot(
            "adverse", adverse_id, opposing, lenient=lenient,
            ambiguous=refuse_ambiguous and "adverse" in required_slots,
        )
    elif adverse_id and not lenient:
        raise _refuse_slot("adverse", "slot_foreign", detail=(
            "ne peut pas être retenue sans dossier : choisissez d'abord le "
            "dossier. Rien n'a été généré."))
    else:
        # No dossier: the client slot is a free contact pick (spec §5); the
        # adverse slot has no fallback.
        adverse_id = ""

    client = get_partie(client_id) if client_id else None
    if client is None:
        if client_id and not dossier and not lenient:
            raise _refuse_slot("client", "slot_unknown", detail=(
                "est introuvable ou n'a pas pu être lu : rouvrez la fenêtre "
                "et choisissez-le de nouveau. Rien n'a été généré."))
        client_id = ""
    adverse = get_partie(adverse_id) if adverse_id else None
    destinataire = get_partie(destinataire_id) if destinataire_id else None
    if destinataire is None:
        if destinataire_id and not lenient:
            raise _refuse_slot("destinataire", "slot_unknown", detail=(
                "est introuvable ou n'a pas pu être lu : rouvrez la fenêtre "
                "et choisissez-le de nouveau. Rien n'a été généré."))
        destinataire_id = ""

    return SlotResolution(
        dossier=dossier,
        dossier_id=dossier_id,
        client=client,
        client_id=client_id,
        adverse=adverse,
        adverse_id=adverse_id,
        destinataire=destinataire,
        destinataire_id=destinataire_id,
        clients=list(clients),
        opposing_parties=list(opposing),
        parties=dossier_parties(dossier),
    )


def _dossier_slot(
    slot: str,
    chosen: str,
    entries: list,
    *,
    lenient: bool,
    ambiguous: bool,
) -> str:
    """The id a client/adverse slot takes on a dossier (see resolve_slots)."""
    valid = {e.get("id") for e in entries}
    if chosen:
        if chosen in valid:
            return chosen
        if lenient:
            return _first_id(entries)
        raise _refuse_slot(slot, "slot_foreign", detail=(
            "ne figure pas (ou plus) au dossier : rouvrez la fenêtre et "
            "choisissez de nouveau. Rien n'a été généré."))
    if ambiguous and len(_entry_ids(entries)) > 1:
        raise _refuse_slot(slot, "slot_ambiguous", detail=(
            "doit être précisé : le dossier en compte plusieurs. Rien n'a "
            "été généré."))
    return _first_id(entries)


PARTIES_UNREADABLE = (
    "La fiche d'une partie du dossier n'a pas pu être lue : réessayez dans "
    "un instant. Si le refus persiste, une partie inscrite au dossier est "
    "introuvable — le juriste doit vérifier les parties du dossier. Rien "
    "n'a été généré."
)


def require_parties_read(slots: SlotResolution) -> None:
    """Refuse (``parties_unreadable``) when a party the DOSSIER lists did
    not load — the bulk read behind the role-scoped blocks, or a client /
    opposing-party slot's own read.

    Both readers fail OPEN (``get_parties_bulk`` to ``{}``, ``get_partie``
    to ``None``), which :func:`resolve_slots` accepts on purpose: the web
    popup SHOWS every resolved value before anything is generated, and a
    field that resolves to nothing prints the visible « [CHAMP MANQUANT] »
    marker. A surface that files a document it never shows — the connector
    — must call this instead: without it, one transient read error filed a
    procedure whose intitulé lost its parties' names and addresses, and the
    result called that « données manquantes au dossier » (reviews of lot 2A
    T4 and T8; the completeness critic). The same fail-closed rule
    ``services.docx_identifiers.dossier_identifiers`` applies to the same
    bulk read. A party a dossier lists cannot be deleted (the partie FK
    check fails closed), so a refusal that persists means a record the
    lawyer must look at, never a routine case.
    """
    if not slots.dossier:
        return
    listed = _entry_ids(slots.clients) + _entry_ids(slots.opposing_parties)
    loaded = slots.parties or {}
    unread = any(pid not in loaded for pid in listed)
    # A slot the dossier vouches for resolves to its first entry (or the
    # named one): an empty record there is a read that failed, not a choice.
    unread = unread or (
        bool(_first_id(slots.clients)) and slots.client is None
    ) or (
        bool(_first_id(slots.opposing_parties)) and slots.adverse is None
    )
    if unread:
        raise GenerationRefused(
            "parties_unreadable", PARTIES_UNREADABLE, field="dossier_id")


# ── 2. Auto values ───────────────────────────────────────────────────────


def resolve_auto_values(
    template: dict,
    slots: SlotResolution,
    *,
    firm: Optional[dict] = None,
    today: Optional[date] = None,
) -> dict[str, str]:
    """The auto fields of *template* resolved server-side on *slots*.

    Only non-empty resolutions are present (a name absent from the result is
    unresolved). Pure over what *slots* already loaded, plus the firm
    profile — one keyed read of ``settings/cabinet``, fail-open.
    """
    return resolve_values(
        template.get("placeholders", []),
        dossier=slots.dossier,
        client=slots.client,
        adverse=slots.adverse,
        destinataire=slots.destinataire,
        firm=firm if firm is not None else cabinet_dict(),
        today=today or today_mtl(),
        parties=slots.parties,
    )


# ── 3. The fields ────────────────────────────────────────────────────────


def _is_multiline(value: str) -> bool:
    """A value carrying a blank line is a BLOCK: ``docx_fill`` clones the
    host paragraph once per chunk. It must round-trip through a
    ``<textarea>`` — an ``<input type=text>`` strips newlines by the HTML
    value-sanitization algorithm, which would flatten the block into one
    line and the expansion would silently never fire."""
    return "\n\n" in value.replace("\r\n", "\n")


def field_ceiling(kind: str, *, multiline: bool, resolved: str = "") -> int:
    """The longest value a field of *kind* may carry (see the Ceilings)."""
    own = len((resolved or "").replace("\r\n", "\n"))
    if kind == "auto":
        base = MULTILINE_MAX_CHARS if multiline else AUTO_MAX_CHARS
        return max(base, own)
    return MULTILINE_MAX_CHARS if multiline else MANUAL_MAX_CHARS


@dataclass(frozen=True)
class InventoryField:
    """One placeholder, as the connector may see it — never its value."""

    name: str
    kind: str                         # "auto" | "manual" | "passthrough"
    resolved: bool                    # auto: the server has a value on the slots
    ceiling: int                      # 0 for passthrough (never submitted)
    options: tuple = ()               # manual selects: (label, value) couples
    default: str = ""                 # manual fields' default, if any
    # auto: the party slot the value comes from (« dossier », « client »,
    # « adverse », « destinataire »), or "" for the firm and today's date —
    # which resolve whatever the slots.
    slot: str = ""


@dataclass(frozen=True)
class Inventory:
    fields: tuple = ()
    slots_required: tuple = ()


def _prompted(template: dict):
    """(name, kind) for every placeholder, in document order."""
    placeholders = template.get("placeholders", [])
    # Re-classified on every call so the field set is always correct — even
    # for templates uploaded before this taxonomy (their stored *_fields
    # lists may be stale).
    classification = classify_placeholders(placeholders)
    auto_set = set(classification.auto)
    manual_set = set(classification.manual)
    for name in placeholders:
        if name in auto_set:
            yield name, "auto"
        elif name in manual_set:
            yield name, "manual"
        else:
            yield name, "passthrough"


def field_inventory(
    template: dict,
    slots: SlotResolution,
    *,
    resolved: Optional[Mapping[str, str]] = None,
) -> Inventory:
    """Every placeholder of *template* — its kind, whether the server
    resolves it on *slots*, its ceiling — and NO value (SPEC H.4 D5: the
    connector needs to know a field is the server's, not what the client's
    address is). *resolved* avoids a second resolution when the caller
    already holds one."""
    if resolved is None:
        resolved = resolve_auto_values(template, slots)
    classification = classify_placeholders(template.get("placeholders", []))
    fields = []
    for name, kind in _prompted(template):
        if kind == "passthrough":
            fields.append(InventoryField(name, kind, False, 0))
            continue
        if kind == "auto":
            value = resolved.get(name, "")
            canonical = classification.auto.get(name, "")
            fields.append(InventoryField(
                name, kind, bool(value),
                field_ceiling(kind, multiline=_is_multiline(value), resolved=value),
                slot=(CATALOG.get(canonical) or ("",))[0] or "",
            ))
            continue
        spec = manual_spec(name) or {}
        fields.append(InventoryField(
            name, kind, False, field_ceiling(kind, multiline=False),
            options=tuple(manual_options(name) or ()),
            default=spec.get("default", "") or "",
        ))
    return Inventory(fields=tuple(fields),
                     slots_required=tuple(sorted(classification.slots_required)))


def form_fields(template: dict, resolved: Mapping[str, str]) -> list[dict]:
    """The popup's field list: one dict per AUTO or MANUAL placeholder, with
    the value to prefill; passthrough placeholders are not form fields."""
    fields = []
    for name, kind in _prompted(template):
        if kind == "passthrough":
            continue
        value = resolved.get(name, "")
        options = None
        if kind == "manual":
            options = manual_options(name)
            if not value:
                value = (manual_spec(name) or {}).get("default", "") or ""
        multiline = _is_multiline(value)
        fields.append({
            "name": name, "kind": kind, "value": value,
            "options": options, "multiline": multiline,
            # The input's maxlength: the browser refuses a user edit past
            # it (the server refuses past the same number).
            "ceiling": field_ceiling(
                kind, multiline=multiline,
                resolved=value if kind == "auto" else ""),
        })
    return fields


def passthrough_fields(template: dict) -> list[str]:
    return classify_placeholders(template.get("placeholders", [])).passthrough


# ── 4. Submitted values ──────────────────────────────────────────────────


def values_from_submission(
    template: dict,
    submitted: Mapping[str, str],
    *,
    resolved: Optional[Mapping[str, str]] = None,
) -> tuple[dict[str, str], int]:
    """One value per prompted placeholder from *submitted* (``{name: raw}``);
    blanks become the visible French fallback strings (§6.7).

    Passthrough placeholders (former blocks, civilité, salutations, unknown
    names) are omitted from the result, so :func:`fill_docx` leaves each
    ``{{name}}`` verbatim in the output for the user to complete in Word.

    Each raw value is checked against :func:`field_ceiling` — *resolved*
    (the server's own auto values, when the caller has them) lifting an
    auto field's ceiling to its own length — and REFUSED past it
    (``value_too_long``); a manual value outside its option list is refused
    too (``manual_option_invalid``: the ``<select>`` is the only constraint
    on the browser side, and a crafted or stale POST must not write an
    arbitrary mention into a letter). Returns ``(values, missing)``.
    """
    resolved = resolved or {}
    values: dict[str, str] = {}
    missing = 0
    for name, kind in _prompted(template):
        if kind == "passthrough":
            continue  # leave {{name}} in the output for Word
        raw = submitted.get(name, "") or ""
        measured = raw.replace("\r\n", "\n")
        ceiling = field_ceiling(
            kind, multiline="\n" in measured,
            resolved=resolved.get(name, "") if kind == "auto" else "",
        )
        if len(measured) > ceiling:
            raise GenerationRefused("value_too_long", (
                f"Le champ « {name} » dépasse la longueur permise "
                f"({ceiling} caractères) : raccourcissez-le. Rien n'a été "
                "généré."), field=name)
        if kind == "auto":
            value = raw.strip()
            if not value:
                value = fallback_value(name, is_auto=True)
                missing += 1
        else:
            options = manual_options(name)
            if options and raw.strip() and raw.strip() not in {v for _, v in options}:
                raise GenerationRefused("manual_option_invalid", (
                    f"La valeur du champ « {name} » ne figure pas dans la "
                    "liste proposée. Rouvrez la fenêtre et choisissez une "
                    "option."), field=name)
            value = manual_value(name, raw)
            # Compare against the marker itself rather than sniffing its
            # prefix: a legitimate value could one day begin the same way.
            if value == fallback_value(name, is_auto=False):
                missing += 1
        values[name] = value
    return values, missing


# ── 5. The connector's values (lot 2A, T8 — ``fill_gabarit``) ────────────
#
# The web fills what the lawyer SUBMITTED in the popup (step 4); the
# connector fills what the SERVER resolves, plus the two things only Claude
# can write: the gabarit's BLOCS (passthrough names — « {{FAITS}} ») and its
# MANUAL fields (letter metadata). Nothing else is Claude's: an auto field
# named as a bloc is refused, never « overridden » — the application, not
# the model, prints the parties and the file number (SPEC H.4 D1).
#
# Ceilings (SPEC H.4 §4.3; the connector's schema copies them as literals,
# pinned against these by tests/test_gabarit_mcp.py): a bloc is a SECTION
# of a procedure, 20 000 characters; all the blocs of one call, 60 000 —
# the 1 MB /mcp body cap stays the hard bound; a manual field is short
# letter metadata, the form's own 2 000.
BLOCS_MAX_ITEMS = 12
CHAMPS_MANUELS_MAX_ITEMS = 12
BLOC_MAX_CHARS = 20_000
BLOCS_TOTAL_MAX_CHARS = 60_000
CHAMP_MANUEL_MAX_CHARS = MANUAL_MAX_CHARS

# What the fill engine strips in silence (utils/docx_fill._CONTROL_RE): a
# C0 control character other than tab, newline and carriage return. Refused
# rather than stripped — nothing supplied is altered without a word.
_CONTROL_CHARS = frozenset(
    chr(c) for c in range(32) if chr(c) not in ("\t", "\n", "\r"))
_NOTHING = "Rien n'a été généré."


def markdown_problem(text: str) -> str:
    """The reason code a MARKDOWN text is refused for once FORMATTED, or
    ``""`` — judged on the very OOXML the fill engine would splice.

    * ``sigils`` — the formatted XML holds « {{ » or « }} » although the
      source may not: a Markdown backslash escape (``\\{``) and an HTML
      character reference (``&#123;``, ``&lbrace;`` — in the text or in a
      link's URL, which the formatter prints after the link) both render as
      a brace. The engine's passes after the rich one rescan that XML and
      would read ``{{dossier.titre}}`` there as a field to fill with the
      dossier's data (review of lot 2A T8 — the raw-text check alone was
      bypassed). The engine matches a placeholder on CONTIGUOUS raw XML
      (``docx_fill._name_pattern``), and no OOXML markup ever carries a
      brace, so a brace pair anywhere in this XML is exactly the danger.
    * ``unformattable`` — the formatter refuses the text itself (nesting
      past its ceiling). Filled anyway, the text would be demoted to raw
      Markdown under a message blaming the TEMPLATE's paragraph.

    The seed paragraph/run properties are the host's in the real fill; they
    carry no text, so the default seed judges the same characters.
    """
    from utils.markdown_docx import markdown_to_ooxml  # lazy — pure module

    try:
        ooxml = markdown_to_ooxml(text)
    except Exception:
        return "unformattable"
    if "{{" in ooxml or "}}" in ooxml:
        return "sigils"
    return ""


def _refuse_markdown(problem: str, where: str, *, field: str) -> GenerationRefused:
    if problem == "unformattable":
        return GenerationRefused("markdown_unformattable", (
            f"{where} ne peut pas être mis en forme (imbrication de listes "
            "ou de citations trop profonde) : simplifiez-le, ou envoyez-le "
            f"sans markdown. {_NOTHING}"), field=field)
    return GenerationRefused("value_refused", (
        f"{where} produirait « {{{{ » ou « }}}} » une fois mis en forme (une "
        "accolade échappée « \\{ » ou une entité « &#123; » donne une "
        "accolade) : l'application le lirait comme un champ à remplir et y "
        f"imprimerait des données du dossier. Retirez-les. {_NOTHING}"),
        field=field)


@dataclass(frozen=True)
class ConnectorValues:
    """What a connector fill hands the engine, and what it can report —
    names and counts, never a value."""

    values: dict = dc_field(default_factory=dict)
    rich_values: dict = dc_field(default_factory=dict)
    auto_resolved: tuple = ()          # auto names the server resolved
    auto_missing: tuple = ()           # → « [CHAMP MANQUANT : …] »
    manual_missing: tuple = ()         # → « [À COMPLÉTER : …] »
    blocs_plain: tuple = ()            # supplied, filled as paragraphs
    blocs_markdown: tuple = ()         # supplied, sent to the rich path
    blocs_open: tuple = ()             # passthrough names nobody supplied


def _names_fr(names) -> str:
    names = [f"« {n} »" for n in names]
    return ", ".join(names) if names else "aucun"


def _text_problem(text: str, ceiling: int) -> str:
    """The reason code a supplied text is refused for, or ``""``."""
    if not text.strip():
        return "blank"
    if len(text.replace("\r\n", "\n")) > ceiling:
        return "too_long"
    if "{{" in text or "}}" in text:
        return "sigils"
    if any(ch in _CONTROL_CHARS for ch in text):
        return "control"
    return ""


def _refuse_text(problem: str, where: str, ceiling: int, *, field: str) -> GenerationRefused:
    if problem == "too_long":
        return GenerationRefused("value_too_long", (
            f"{where} dépasse {ceiling} caractères : découpez-le, ou "
            f"rédigez la suite dans Word. {_NOTHING}"), field=field)
    if problem == "sigils":
        # The engine would read « {{nom}} » inside the text as a field to
        # fill (its later passes rescan what an earlier one inserted) and
        # print dossier data there — the review's « re-substitution ».
        return GenerationRefused("value_refused", (
            f"{where} contient « {{{{ » ou « }}}} » : l'application les "
            "lirait comme un champ à remplir et y imprimerait des données du "
            f"dossier. Retirez-les. {_NOTHING}"), field=field)
    if problem == "control":
        return GenerationRefused("value_refused", (
            f"{where} contient un caractère de contrôle invisible, que le "
            f"document ne peut pas porter : retirez-le. {_NOTHING}"),
            field=field)
    return GenerationRefused("value_refused", (
        f"{where} est vide : omettez-le plutôt — il restera tel quel pour "
        f"Word. {_NOTHING}"), field=field)


def values_for_connector(
    template: dict,
    resolved: Mapping[str, str],
    *,
    blocs: list,
    manuels: list,
) -> ConnectorValues:
    """The fill of *template* on the server's *resolved* auto values plus
    the blocs and manual fields Claude supplied — or a refusal.

    *blocs* are ``{nom, contenu, markdown?}`` dicts, *manuels* ``{nom,
    valeur}`` dicts, in request order; names are compared EXACTLY with the
    template's placeholders, as ``list_templates`` reports them. Raises
    :class:`GenerationRefused` — nothing is filled — for: too many entries
    (``too_many_items``); a bloc that names no passthrough placeholder of
    the template, or names an auto or manual field (``bloc_unknown``); a
    manual entry that names no manual field (``manual_unknown``); a name
    given twice, or both as a bloc and as a manual field (``bloc_conflict``);
    a manual value outside its option list (``manual_option_invalid``); a
    text past its ceiling (``value_too_long``); an empty text, a text
    carrying ``{{``/``}}`` or a control character, or a Markdown bloc whose
    FORMATTED text would carry them (``value_refused`` —
    :func:`markdown_problem`); a Markdown bloc the formatter cannot convert
    (``markdown_unformattable``).
    Positions in the messages are 1-based; a supplied name is never quoted
    unless it IS a placeholder of the template.

    Markdown hygiene (autolinks, the chevrons ``bleach`` would strip) is the
    caller's, BEFORE this: it owns the normalization the note writes share.
    """
    placeholders = [p for p in (template.get("placeholders") or [])
                    if isinstance(p, str)]
    classification = classify_placeholders(placeholders)
    auto_names = set(classification.auto)
    manual_names = set(classification.manual)
    passthrough = [n for n in placeholders
                   if n not in auto_names and n not in manual_names]

    if len(blocs) > BLOCS_MAX_ITEMS:
        raise GenerationRefused("too_many_items", (
            f"`blocs` : {BLOCS_MAX_ITEMS} blocs au plus par appel "
            f"({len(blocs)} reçus). {_NOTHING}"), field="blocs")
    if len(manuels) > CHAMPS_MANUELS_MAX_ITEMS:
        raise GenerationRefused("too_many_items", (
            f"`champs_manuels` : {CHAMPS_MANUELS_MAX_ITEMS} champs au plus "
            f"par appel ({len(manuels)} reçus). {_NOTHING}"),
            field="champs_manuels")

    supplied: dict[str, tuple[str, bool]] = {}      # bloc name → (text, md)
    seen_at: dict[str, str] = {}                    # name → where first named
    total = 0
    for position, entry in enumerate(blocs, start=1):
        where = f"Le bloc n° {position}"
        name = str((entry or {}).get("nom") or "")
        if name in auto_names:
            raise GenerationRefused("bloc_unknown", (
                f"{where} désigne « {name} », un champ que l'application "
                f"remplit elle-même depuis le dossier : retirez-le. {_NOTHING}"),
                field=name)
        if name in manual_names:
            raise GenerationRefused("bloc_unknown", (
                f"{where} désigne « {name} », un champ manuel : passez-le "
                f"dans `champs_manuels`. {_NOTHING}"), field=name)
        if name not in passthrough:
            raise GenerationRefused("bloc_unknown", (
                f"{where} ne désigne aucun bloc de ce gabarit. Blocs à "
                f"rédiger : {_names_fr(passthrough)}. Écrivez le nom "
                "exactement comme list_templates (avec template_id) le "
                f"rapporte — la casse compte. {_NOTHING}"), field="blocs")
        if name in seen_at:
            raise GenerationRefused("bloc_conflict", (
                f"{where} et {seen_at[name]} désignent le même bloc "
                f"« {name} » : un bloc se rédige une seule fois. {_NOTHING}"),
                field=name)
        seen_at[name] = where.replace("Le bloc", "le bloc")
        text = (entry or {}).get("contenu")
        text = text if isinstance(text, str) else ""
        problem = _text_problem(text, BLOC_MAX_CHARS)
        if problem:
            raise _refuse_text(problem, where, BLOC_MAX_CHARS, field=name)
        markdown = bool((entry or {}).get("markdown", False))
        if markdown:
            problem = markdown_problem(text)
            if problem:
                raise _refuse_markdown(problem, where, field=name)
        total += len(text.replace("\r\n", "\n"))
        supplied[name] = (text, markdown)
    if total > BLOCS_TOTAL_MAX_CHARS:
        raise GenerationRefused("value_too_long", (
            f"`blocs` : {total} caractères au total, au-delà de "
            f"{BLOCS_TOTAL_MAX_CHARS} par appel. Rédigez le reste dans Word, "
            f"ou générez en deux documents. {_NOTHING}"), field="blocs")

    manual_supplied: dict[str, str] = {}
    for position, entry in enumerate(manuels, start=1):
        where = f"Le champ manuel n° {position}"
        name = str((entry or {}).get("nom") or "")
        if name not in manual_names:
            if name in passthrough:
                detail = (f"désigne « {name} », un bloc : passez-le dans "
                          "`blocs`.")
            elif name in auto_names:
                detail = (f"désigne « {name} », un champ que l'application "
                          "remplit elle-même : retirez-le.")
            else:
                detail = ("ne désigne aucun champ manuel de ce gabarit. "
                          f"Champs manuels : {_names_fr(sorted(manual_names))}.")
            raise GenerationRefused("manual_unknown",
                                    f"{where} {detail} {_NOTHING}",
                                    field="champs_manuels")
        if name in seen_at:
            raise GenerationRefused("bloc_conflict", (
                f"{where} et {seen_at[name]} désignent le même champ "
                f"« {name} » : nommez-le une seule fois. {_NOTHING}"),
                field=name)
        seen_at[name] = where.replace("Le champ", "le champ")
        raw = (entry or {}).get("valeur")
        raw = raw if isinstance(raw, str) else ""
        if raw.strip():
            problem = _text_problem(raw, CHAMP_MANUEL_MAX_CHARS)
            if problem:
                raise _refuse_text(problem, where, CHAMP_MANUEL_MAX_CHARS,
                                   field=name)
        options = manual_options(name)
        if options and raw.strip() and raw.strip() not in {v for _, v in options}:
            raise GenerationRefused("manual_option_invalid", (
                f"La valeur du champ manuel « {name} » ne figure pas dans sa "
                f"liste. Valeurs admises : {_names_fr(v for _, v in options)} "
                f"(list_templates les rapporte). {_NOTHING}"), field=name)
        manual_supplied[name] = raw

    values: dict[str, str] = {}
    rich_values: dict[str, str] = {}
    auto_resolved, auto_missing, manual_missing = [], [], []
    for name in placeholders:
        if name in auto_names:
            value = str(resolved.get(name) or "")
            if value:
                auto_resolved.append(name)
            else:
                value = fallback_value(name, is_auto=True)
                auto_missing.append(name)
            values[name] = value
        elif name in manual_names:
            value = manual_value(name, manual_supplied.get(name, ""))
            if value == fallback_value(name, is_auto=False):
                manual_missing.append(name)
            values[name] = value
        elif name in supplied:
            text, markdown = supplied[name]
            if markdown:
                rich_values[name] = text
            else:
                values[name] = text
        # else: passthrough nobody supplied — left verbatim for Word.
    return ConnectorValues(
        values=values,
        rich_values=rich_values,
        auto_resolved=tuple(auto_resolved),
        auto_missing=tuple(auto_missing),
        manual_missing=tuple(manual_missing),
        blocs_plain=tuple(n for n in passthrough
                          if n in supplied and not supplied[n][1]),
        blocs_markdown=tuple(n for n in passthrough
                             if n in supplied and supplied[n][1]),
        blocs_open=tuple(n for n in passthrough if n not in supplied),
    )
