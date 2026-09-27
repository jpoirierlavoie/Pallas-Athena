"""Generation from a gabarit — the ONE assembly (lot 2A, step T4).

Hoisted out of ``routes/doc_templates.py`` so that the web popup and the
connector's ``fill_gabarit`` (Lot 2A, later step) cannot fill the same
template two ways. The route became a request adapter: it reads the form,
calls these functions in order, and turns a :class:`GenerationRefused`
into its fragment or its redirect. The pure pieces — the field catalog,
the fill engine — stay in ``utils/``; what lives here is what needs the
store: the slots, the values, the save.

The pipeline, in order:

1. :func:`resolve_slots` — the dossier and the four party slots. STRICT by
   default: a slot id that is not on the dossier is REFUSED. It used to be
   swapped, in silence, for the dossier's first party — and a document was
   produced on a choice the lawyer had not made. ``lenient=True`` is the
   popup RENDER's selection repair, and only that: the dossier picker
   re-renders with ``hx-include`` of the slot inputs, so the PREVIOUS
   dossier's selection arrives with the new dossier and must be reset — the
   popup then SHOWS the choice it made, before anything is generated.
2. :func:`resolve_auto_values` — the auto fields, resolved server-side from
   the catalog on those slots. The web prefills the popup with them; the
   connector will fill with them (never with values Claude supplies).
3. :func:`field_inventory` (the connector's view: kinds and RESOLVED FLAGS,
   never a value) or :func:`form_fields` (the popup's: the values to prefill).
4. :func:`values_from_submission` — the web's values, each checked against
   its REAL ceiling (:func:`field_ceiling`) and REFUSED past it. The route
   used to CUT every single-line value at 2 000 characters: a one-paragraph
   ``{{dossier.sommaire}}`` (5 000 allowed by the model) lost its end in
   the letter, with nothing on screen to say so.
5. :func:`fill` — the engine, with the ``report=`` channel (SPEC H.4 B9).
6. :func:`save_into_projets` — into the dossier's « Projets » system folder,
   found by its ROLE (``ensure_system_folder``, lot 2A T2), under the uid
   the caller established (``request_uid()`` on the web, ``owner_uid()`` on
   the connector) — checked by ``require_uid`` BEFORE the folder is touched,
   so nothing at all is written for a request that could not file it.

Every refusal is a :class:`GenerationRefused` carrying a machine-stable
``reason`` (the ``generation_failed`` vocabulary of OBSERVABILITY.md), a
French ``message`` that never quotes a value, and the ``field`` it concerns.
Logging stays with the caller: it knows which surface refused.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Collection, Mapping, Optional

from werkzeug.utils import secure_filename

from models.document import projet_document_name, upload_document
from models.dossier import get_dossier
from models.folder import SYSTEM_ROLE_PROJETS, ensure_system_folder
from models.partie import get_partie, get_parties_bulk
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.docx_fill import DocxFillError, fill_docx
from utils.logging_setup import log_unexpected
from utils.template_fields import (
    classify_placeholders,
    fallback_value,
    manual_options,
    manual_spec,
    manual_value,
    resolve_values,
)
from utils.tracing_setup import span

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
TEMPLATE_FILE_UNAVAILABLE = (
    "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
)
TEMPLATE_INVALID = "Le gabarit est invalide et n'a pas pu être rempli."
FILL_ERROR = "Erreur lors de la génération. Veuillez réessayer."
PROJETS_UNAVAILABLE = "Le dossier « Projets » est indisponible. Réessayez."

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
    fields = []
    for name, kind in _prompted(template):
        if kind == "passthrough":
            fields.append(InventoryField(name, kind, False, 0))
            continue
        if kind == "auto":
            value = resolved.get(name, "")
            fields.append(InventoryField(
                name, kind, bool(value),
                field_ceiling(kind, multiline=_is_multiline(value), resolved=value),
            ))
            continue
        spec = manual_spec(name) or {}
        fields.append(InventoryField(
            name, kind, False, field_ceiling(kind, multiline=False),
            options=tuple(manual_options(name) or ()),
            default=spec.get("default", "") or "",
        ))
    classification = classify_placeholders(template.get("placeholders", []))
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


# ── 5. Fill ──────────────────────────────────────────────────────────────


def fill(
    docx_bytes: bytes,
    values: Mapping[str, str],
    *,
    rich_values: Optional[Mapping[str, str]] = None,
    template_id: str = "",
) -> tuple[bytes, list[str]]:
    """Fill *docx_bytes*; return ``(filled, demoted)``.

    ``demoted`` names the *rich_values* that fell back to the plain fill
    (``fill_docx``'s ``report=`` channel, SPEC H.4 B9) — the Markdown
    sigils then show in the document, which a caller must be able to say.
    Raises :class:`GenerationRefused` (``template_invalid`` for a
    structurally invalid template, ``fill_error`` for anything else).
    """
    report: dict = {}
    try:
        with span("template.fill", template_id=template_id,
                  field_count=len(values)):
            filled = fill_docx(
                docx_bytes, dict(values),
                rich_values=dict(rich_values) if rich_values else None,
                report=report,
            )
    except DocxFillError as exc:
        raise GenerationRefused("template_invalid", TEMPLATE_INVALID) from exc
    except Exception as exc:
        log_unexpected("template fill failed", template_id=template_id)
        raise GenerationRefused("fill_error", FILL_ERROR) from exc
    return filled, list(report.get("demoted", []))


# ── 6. Names and save ────────────────────────────────────────────────────


def output_names(
    template: dict, dossier: Optional[dict], today: date
) -> tuple[str, str]:
    """``(display_name, filename)`` of a generated document:
    ``"REF - YYYY-MM-DD - Projet Nom"`` and its safe ``.docx`` filename."""
    reference = (dossier or {}).get("file_number", "")
    display = projet_document_name(reference, template.get("name", "Gabarit"), today)
    out_name = secure_filename(f"{display}.docx")
    if not out_name.lower().endswith(".docx"):
        out_name = f"projet_{today.isoformat()}.docx"
    return display, out_name


def generated_from(template: dict) -> str:
    """The machine provenance a generated document carries (``genere_depuis``)."""
    return (f"Généré depuis le gabarit «{template.get('name', '')}» "
            f"v{template.get('version', 1)}")


def save_into_projets(
    *,
    template: dict,
    dossier: dict,
    filled: bytes,
    uid: str,
    today: date,
) -> dict:
    """File *filled* in *dossier*'s « Projets » folder; return the document.

    *uid* is the Storage identity the caller established
    (``storage_identity.request_uid()`` on the web, ``owner_uid()`` on the
    connector). It is checked FIRST: nothing is written — not even the
    folder — for a request that could not file the document anyway.

    Raises :class:`GenerationRefused`: ``save_failed`` (an unusable uid, an
    upload the model refused), ``projets_unavailable`` (the system folder
    could not be obtained — the document is NEVER saved at the dossier root
    instead, which the old ``get_or_create_folder`` path did on its None).
    """
    try:
        uid = storage_identity.require_uid(uid)
    except storage_identity.StorageIdentityUnavailable as exc:
        raise GenerationRefused("save_failed", str(exc)) from exc
    dossier_id = dossier.get("id", "")
    folder, folder_errors = ensure_system_folder(dossier_id, SYSTEM_ROLE_PROJETS)
    if folder is None:
        raise GenerationRefused(
            "projets_unavailable",
            folder_errors[0] if folder_errors else PROJETS_UNAVAILABLE,
        )
    display, out_name = output_names(template, dossier, today)
    metadata = {
        "category": template.get("category", "autre"),
        "folder_id": folder["id"],
        "display_name": display,
        "genere_depuis": generated_from(template),
        "tags": ["gabarit"],
    }
    doc, errors = upload_document(
        dossier_id=dossier_id,
        dossier_file_number=dossier.get("file_number", ""),
        file_stream=io.BytesIO(filled),
        filename=out_name,
        file_size=len(filled),
        metadata=metadata,
        user_id=uid,
    )
    if errors or doc is None:
        raise GenerationRefused(
            "save_failed", errors[0] if errors else FILL_ERROR)
    return doc
