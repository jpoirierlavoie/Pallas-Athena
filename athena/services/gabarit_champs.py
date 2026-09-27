"""The fields of a generation from a gabarit — the READ half (lot 2A, T6).

What a generation needs before it writes anything: the dossier and the
party slots (:func:`resolve_slots`), the auto values resolved server-side
(:func:`resolve_auto_values`), the placeholder inventory
(:func:`field_inventory` — kinds and RESOLVED FLAGS, never a value) or the
popup's prefilled fields (:func:`form_fields`), and the check of a
submitted form (:func:`values_from_submission`). Nothing here writes: the
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
