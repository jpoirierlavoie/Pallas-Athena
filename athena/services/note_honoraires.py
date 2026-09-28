"""The note d'honoraires — ONE generation, for the web and the connector.

Hoisted out of ``routes/invoices.invoice_note_docx`` (lot 3a, step 2) so
that the « Note d'honoraires (Word) » button and the connector's future
generation tool (lot 3b) cannot print an invoice two ways. The route became
a request adapter: it calls :func:`generer_note_honoraires` and turns a
:class:`NoteRefusee` into its fragment. The pure pieces stay where they
were — ``utils/invoice_docx.build_invoice_context`` (the invoice → fill
context), the fill engine — and the save is ``services.gabarits.
save_generated``, the ONE uploader of every generated .docx.

The pipeline, in order — every refusal raises :class:`NoteRefusee` BEFORE
anything is written, and logs its ``generation_failed`` line here, so both
surfaces log identically:

1. the invoice and its line items through the STRICT reader
   (``get_invoice_with_items_strict``): a read failure refuses
   (``invoice_unreadable``) — the route used the fail-open reader, whose
   swallowed line-item failure printed the stored totals over EMPTY tables,
   a client-facing document that looked complete;
2. an ``annulée`` invoice is refused (``invoice_voided``);
3. line items absent under a non-zero subtotal refuse
   (``line_items_unreadable`` — ``models.invoice.line_items_missing``, the
   void's own rule: one rule for the web and the connector);
4. the template the lawyer DESIGNATED (``get_active_template
   ("note_honoraires")`` — never « the most recent », D11): none designated
   (``no_note_template``) and an unreadable store (``template_read_failed``)
   are two different answers;
5. the fill context (the dossier and the client read as before — a client
   gone falls back to the invoice's frozen billing snapshot), the values,
   and their FINGERPRINT (:func:`fill_fingerprint` — the template's id and
   version plus a SHA-256 of everything the fill prints);
6. unless *regenerate*: a note already generated from THIS invoice with the
   SAME fingerprint is returned instead of a duplicate (``reused=True``,
   nothing written, no log line — the connector's own ``mcp_write`` says
   what its call did). The lookup is strict (``find_generated_for_invoice``
   raises): « none found » on a transient error would file a duplicate, so
   it refuses (``generated_lookup_failed``);
7. the bytes of the file THIS template record names
   (``template_file_bytes`` — never a re-read that could print version N+1
   under the name of N), then the fill (``template.fill`` span);
8. the Storage uid — obtained only now, so nothing is written for a
   request that could not file the note — through *resolve_uid*
   (``storage_identity.owner_uid`` by default: a caller with no browser
   session; the web passes ``request_uid``), and the save into the
   dossier's « Projets » system folder, by its ROLE, the document carrying
   ``source_invoice_id`` + ``generation_fingerprint``.

The web passes ``regenerate=True``: its button has always filed a new note
on every click, and keeps doing so (the note it files is fingerprinted, so
the connector finds it). Never reads ``flask.session`` — a sweep pins it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from models.doc_template import (
    TemplateReadError,
    get_active_template,
    template_file_bytes,
)
from models.document import find_generated_for_invoice
from models.dossier import get_dossier
from models.invoice import get_invoice_with_items_strict, line_items_missing
from models.partie import get_partie
from services.gabarits import GenerationRefused, projet_names, save_generated
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.deadlines import today_mtl
from utils.docx_fill import DocxFillError, fill_docx
from utils.invoice_docx import build_invoice_context
from utils.logging_setup import log_template_event, log_unexpected
from utils.template_fields import classify_placeholders, fallback_value, manual_value
from utils.tracing_setup import add_attributes, span

NOTE_KIND = "note_honoraires"

# French messages — the route's own wording, kept VERBATIM where it existed.
INVOICE_NOT_FOUND = "Facture introuvable."
INVOICE_UNREADABLE = (
    "La facture ou ses lignes n'ont pas pu être lues — lecture impossible. "
    "Rien n'a été généré : réessayez dans un instant."
)
INVOICE_VOIDED = (
    "Impossible de générer une note d'honoraires pour une facture annulée."
)
LINE_ITEMS_UNREADABLE = (
    "Les lignes de cette facture sont introuvables alors que son sous-total "
    "n'est pas nul : la note imprimerait des totaux au-dessus de tableaux "
    "vides. Rien n'a été généré."
)
NO_ACTIVE_NOTE_HONORAIRES = (
    "Aucun gabarit « Note d'honoraires » n'est désigné comme actif : "
    "désignez-en un dans Gabarits."
)
NOTE_TEMPLATE_UNREADABLE = (
    "Le gabarit actif des notes d'honoraires n'a pas pu être lu — lecture "
    "impossible. Rien n'a été généré : réessayez dans un instant."
)
GENERATED_LOOKUP_FAILED = (
    "Les notes d'honoraires déjà générées pour cette facture n'ont pas pu "
    "être lues. Rien n'a été généré, pour ne pas en créer une en double : "
    "réessayez dans un instant."
)
TEMPLATE_FILE_UNAVAILABLE = (
    "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
)
TEMPLATE_INVALID = "Le gabarit est invalide et n'a pas pu être rempli."
FILL_ERROR = "Erreur lors de la génération. Veuillez réessayer."
SAVE_FAILED = "Erreur lors de l'enregistrement."


class NoteRefusee(Exception):
    """A note d'honoraires that must not be generated — nothing was written.

    ``reason`` is machine-stable (the ``generation_failed`` vocabulary of
    OBSERVABILITY.md); ``message`` is French and never quotes a value.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class NoteGeneree:
    """A note d'honoraires the call produced — or found already filed."""

    document: dict
    template: dict
    fingerprint: str
    reused: bool = False
    counts: dict = field(default_factory=dict)


def assemble_note_values(template: dict, ctx) -> dict[str, str]:
    """One value per template placeholder: ctx (facture.* + resolved header
    fields) → auto fallback → manual default. Row-scoped (h./d.) and
    passthrough names are omitted (rows fill the former; the latter stay
    literal). Moved verbatim from ``routes/invoices._assemble_note_values``.
    """
    placeholders = template.get("placeholders", [])
    classification = classify_placeholders(placeholders)
    values: dict[str, str] = {}
    for name in placeholders:
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True)
        elif name in classification.manual:
            # manual_value(), never MANUAL_FIELDS[name]: manual names match
            # case-insensitively since Sept. 2026, so a {{PRIVILÈGE}} in a note
            # template would KeyError on the bare index — on a path that never
            # prompts, so nothing would hint at the cause.
            values[name] = manual_value(name)
        # else: passthrough / row-scoped → omit
    return values


def fill_fingerprint(template: dict, values: dict, ctx) -> str:
    """The SHA-256 of what a note prints: the template's id and version,
    and every value, row and condition the fill receives.

    Two generations with the same fingerprint produce the same document:
    the same bytes of the same template (a replaced file is a new version),
    filled with the same values. What the invoice's ``updated_at`` would
    miss (a replaced letterhead, an edited client address) and what it
    would over-report (a status change the note does not print) are both
    decided by the CONTENT here.
    """
    payload = {
        "template_id": str(template.get("id") or ""),
        "template_version": template.get("version", 1),
        "values": values,
        "rows": ctx.rows,
        "conditions": ctx.conditions,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _refuse(reason: str, message: str, **fields) -> NoteRefusee:
    log_template_event("generation_failed", reason=reason, **fields)
    return NoteRefusee(reason, message)


def generer_note_honoraires(
    invoice_id: str,
    *,
    regenerate: bool = False,
    resolve_uid: Optional[Callable[[], str]] = None,
    today: Optional[date] = None,
) -> NoteGeneree:
    """Generate *invoice_id*'s note d'honoraires into « Projets ».

    Raises :class:`NoteRefusee` (nothing written). See the module
    docstring for the order of the checks. *regenerate* files a new note
    even when an identical one exists; *resolve_uid* — how the Storage uid
    is obtained, called once the note is filled (step 8) — defaults to
    ``storage_identity.owner_uid``; *today* defaults to Montréal's.
    """
    try:
        invoice, items = get_invoice_with_items_strict(invoice_id)
    except Exception:
        log_unexpected("note d'honoraires: invoice read failed",
                       invoice_id=invoice_id)
        raise _refuse("invoice_unreadable", INVOICE_UNREADABLE,
                      invoice_id=invoice_id)
    if not invoice:
        raise _refuse("invoice_not_found", INVOICE_NOT_FOUND)
    if invoice.get("status") == "annulée":
        raise _refuse("invoice_voided", INVOICE_VOIDED)
    if line_items_missing(invoice, items):
        raise _refuse("line_items_unreadable", LINE_ITEMS_UNREADABLE,
                      invoice_id=invoice_id)

    # The DESIGNATED template (D11, lot 2A T3) — never « the most recent »:
    # an edit of another template can no longer switch the letterhead of
    # every client's note. None designated and an unreadable store are two
    # different answers: the first names the fix, the second says retry.
    try:
        template = get_active_template(NOTE_KIND)
    except TemplateReadError:
        raise _refuse("template_read_failed", NOTE_TEMPLATE_UNREADABLE)
    if not template:
        raise _refuse("no_note_template", NO_ACTIVE_NOTE_HONORAIRES)
    template_id = template["id"]

    dossier_id = invoice.get("dossier_id", "")
    dossier = get_dossier(dossier_id) if dossier_id else None
    client_id = invoice.get("client_id", "")
    client = get_partie(client_id) if client_id else None
    day = today or today_mtl()

    ctx = build_invoice_context(
        invoice, items,
        firm=cabinet_dict(), destinataire=client, dossier=dossier, today=day,
    )
    values = assemble_note_values(template, ctx)
    counts = {
        "rows_honoraire": len(ctx.rows["ligne_honoraire"]),
        "rows_debours_tx": len(ctx.rows["ligne_debours_tx"]),
        "rows_debours_ntx": len(ctx.rows["ligne_debours_ntx"]),
    }
    add_attributes(template_id=template_id, invoice_id=invoice_id, **counts)
    fingerprint = fill_fingerprint(template, values, ctx)

    if not regenerate:
        try:
            filed = find_generated_for_invoice(invoice_id)
        except Exception:
            log_unexpected("note d'honoraires: generated notes lookup failed",
                           invoice_id=invoice_id)
            raise _refuse("generated_lookup_failed", GENERATED_LOOKUP_FAILED,
                          template_id=template_id, invoice_id=invoice_id)
        for existing in filed:
            if (existing.get("generation_fingerprint") == fingerprint
                    and existing.get("dossier_id") == dossier_id):
                return NoteGeneree(document=existing, template=template,
                                   fingerprint=fingerprint, reused=True,
                                   counts=counts)

    docx_bytes = template_file_bytes(template)
    if docx_bytes is None:
        raise _refuse("template_file_unavailable", TEMPLATE_FILE_UNAVAILABLE,
                      template_id=template_id)

    try:
        with span("template.fill", template_id=template_id, invoice_id=invoice_id):
            filled = fill_docx(
                docx_bytes, values,
                rows_by_region=ctx.rows, conditions=ctx.conditions,
            )
    except DocxFillError as exc:
        reason = "unbalanced_condition" if "conditionnelle" in str(exc) else "fill_error"
        raise _refuse(reason, TEMPLATE_INVALID, template_id=template_id)
    except Exception:
        log_unexpected("note-honoraires fill failed", template_id=template_id)
        raise _refuse("fill_error", FILL_ERROR, template_id=template_id)

    save_fields = {"template_id": template_id, "dossier_id": dossier_id,
                   "invoice_id": invoice_id}
    # The uid first, then « Projets »: nothing is written — not even the
    # folder — for a request that could not file the note anyway.
    try:
        uid = (resolve_uid or storage_identity.owner_uid)()
    except storage_identity.StorageIdentityUnavailable as exc:
        raise _refuse("save_failed", str(exc), **save_fields)

    invoice_number = invoice.get("invoice_number", "")
    tmpl_base = template.get("name") or "Note d'honoraires"
    display, out_name = projet_names(
        dossier, f"{tmpl_base} {invoice_number}".strip(), day)
    try:
        # Found by its ROLE, at its deterministic id (lot 2A, T2): a failure
        # REFUSES, never a save at the dossier root.
        doc, _folder = save_generated(
            dossier={"id": dossier_id,
                     "file_number": invoice.get("dossier_file_number", "")},
            filled=filled,
            uid=uid,
            display_name=display,
            filename=out_name,
            category="correspondance",
            genere_depuis=f"Générée depuis la facture {invoice_number}".strip(),
            tags=("note_honoraires",),
            generated_from_invoice={"invoice_id": invoice_id,
                                    "fingerprint": fingerprint},
        )
    except GenerationRefused as refusal:
        raise _refuse(refusal.reason, refusal.message or SAVE_FAILED,
                      **save_fields)

    log_template_event("document_generated", template_id=template_id,
                       dossier_id=dossier_id, saved_document_id=doc["id"],
                       invoice_id=invoice_id, source="facture", **counts)
    return NoteGeneree(document=doc, template=template,
                       fingerprint=fingerprint, reused=False, counts=counts)
