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
   The connector's twin is :func:`values_for_connector` (lot 2A, T8): the
   SERVER's auto values, plus the blocs and manual fields Claude supplied,
   each checked against the template — never a value for a field the
   application resolves.
5. :func:`fill` — the engine, with the ``report=`` channel (SPEC H.4 B9).
6. :func:`save_into_projets` — into the dossier's « Projets » system folder,
   found by its ROLE (``ensure_system_folder``, lot 2A T2), under the uid
   the caller established (``request_uid()`` on the web, ``owner_uid()`` on
   the connector) — checked by ``require_uid`` BEFORE the folder is touched,
   so nothing at all is written for a request that could not file it.
   :func:`save_generated` is its general form (lot 2A, T8): any generated
   .docx — a filled gabarit, a Markdown document printed on the note
   template —, its own name, category and provenance, into « Projets » or
   a folder the caller resolved.

Steps 1-4 READ only and live in :mod:`services.gabarit_champs` (lot 2A,
T6), re-exported here so the route keeps one import: a connector READ
(``list_templates``) imports that half alone, and the « never » sweep
(tests/test_mcp_disclosure.py), which reads a reached service WHOLE,
then proves it reaches no writer. Steps 5-6 — the fill and the save —
stay here.

Every refusal is a :class:`GenerationRefused` carrying a machine-stable
``reason`` (the ``generation_failed`` vocabulary of OBSERVABILITY.md), a
French ``message`` that never quotes a value, and the ``field`` it concerns.
Logging stays with the caller: it knows which surface refused.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Mapping, Optional

from werkzeug.utils import secure_filename

from models.document import projet_document_name, upload_document
from models.folder import SYSTEM_ROLE_PROJETS, ensure_system_folder
from services.gabarit_champs import (  # noqa: F401 — re-exported, see above
    AUTO_MAX_CHARS,
    BLOC_MAX_CHARS,
    BLOCS_MAX_ITEMS,
    BLOCS_TOTAL_MAX_CHARS,
    CHAMP_MANUEL_MAX_CHARS,
    CHAMPS_MANUELS_MAX_ITEMS,
    DOSSIER_NOT_FOUND,
    FIELD_PREFIX,
    MANUAL_MAX_CHARS,
    MULTILINE_MAX_CHARS,
    ConnectorValues,
    GenerationRefused,
    Inventory,
    InventoryField,
    SlotResolution,
    dossier_parties,
    field_ceiling,
    field_inventory,
    form_fields,
    passthrough_fields,
    resolve_auto_values,
    resolve_slots,
    values_for_connector,
    values_from_submission,
)
from utils import storage_identity
from utils.docx_fill import DocxFillError, fill_docx
from utils.logging_setup import log_unexpected
from utils.tracing_setup import span

# French messages of the write half — kept VERBATIM from the route they
# come from (the web says exactly what it said before the hoist).
TEMPLATE_FILE_UNAVAILABLE = (
    "Le fichier du gabarit est introuvable. Téléversez-le à nouveau."
)
TEMPLATE_INVALID = "Le gabarit est invalide et n'a pas pu être rempli."
FILL_ERROR = "Erreur lors de la génération. Veuillez réessayer."
PROJETS_UNAVAILABLE = "Le dossier « Projets » est indisponible. Réessayez."


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
    return projet_names(dossier, template.get("name", "Gabarit"), today)


def projet_names(
    dossier: Optional[dict], name: str, today: date
) -> tuple[str, str]:
    """``(display_name, filename)`` of a « projet » named *name*:
    ``"REF - YYYY-MM-DD - Projet Nom"`` and its safe ``.docx`` filename
    (``projet_{date}.docx`` when the name sanitizes to nothing — an
    accent-only title)."""
    reference = (dossier or {}).get("file_number", "")
    display = projet_document_name(reference, name, today)
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
    display, out_name = output_names(template, dossier, today)
    doc, _folder = save_generated(
        dossier=dossier, filled=filled, uid=uid,
        display_name=display, filename=out_name,
        category=template.get("category", "autre"),
        genere_depuis=generated_from(template),
        tags=("gabarit",),
    )
    return doc


# The « no folder named » default of save_generated: the « Projets » system
# folder, found by its role. ``None`` means the dossier root.
PROJETS = object()


def ensure_projets(dossier_id: str) -> dict:
    """*dossier_id*'s « Projets » system folder — by its ROLE, created on
    first use — or :class:`GenerationRefused` (``projets_unavailable``):
    never a silent fall back to the dossier root."""
    folder, errors = ensure_system_folder(dossier_id, SYSTEM_ROLE_PROJETS)
    if folder is None:
        raise GenerationRefused(
            "projets_unavailable", errors[0] if errors else PROJETS_UNAVAILABLE)
    return folder


def save_generated(
    *,
    dossier: dict,
    filled: bytes,
    uid: str,
    display_name: str,
    filename: str,
    category: str,
    genere_depuis: str,
    category_source: str = "juriste",
    tags: tuple = (),
    document_date: Optional[object] = None,
    folder: object = PROJETS,
    generated_from_invoice: Optional[dict] = None,
) -> tuple[dict, Optional[dict]]:
    """File a generated .docx as a NEW document of *dossier*; return
    ``(document, folder)`` — *folder* the one it landed in, ``None`` for the
    dossier root.

    *folder*: :data:`PROJETS` (the default) → the « Projets » system folder,
    ensured by its role; ``None`` → the dossier root; a folder dict the
    CALLER resolved in this dossier (the model re-checks it). The uid is
    checked FIRST, before « Projets » is touched. *category_source* says who
    chose *category*: « juriste » for a template's own category, « mcp »
    when Claude chose it (shown « présumée »). *generated_from_invoice*
    (``{"invoice_id", "fingerprint"}``) links a note d'honoraires to the
    invoice it renders (``services.note_honoraires``, lot 3a).

    Raises :class:`GenerationRefused` as :func:`save_into_projets` does.
    """
    try:
        uid = storage_identity.require_uid(uid)
    except storage_identity.StorageIdentityUnavailable as exc:
        raise GenerationRefused("save_failed", str(exc)) from exc
    dossier_id = dossier.get("id", "")
    if folder is PROJETS:
        target = ensure_projets(dossier_id)
    else:
        target = folder if isinstance(folder, dict) else None
    metadata = {
        "category": category or "autre",
        "folder_id": (target or {}).get("id"),
        "display_name": display_name,
        "genere_depuis": genere_depuis,
        "tags": list(tags),
    }
    if document_date is not None:
        metadata["document_date"] = document_date
    # Only when given: the writers that never link an invoice keep the call
    # they have always made.
    link = ({"generated_from_invoice": generated_from_invoice}
            if generated_from_invoice is not None else {})
    doc, errors = upload_document(
        dossier_id=dossier_id,
        dossier_file_number=dossier.get("file_number", ""),
        file_stream=io.BytesIO(filled),
        filename=filename,
        file_size=len(filled),
        metadata=metadata,
        user_id=uid,
        category_source=category_source,
        **link,
    )
    if errors or doc is None:
        raise GenerationRefused(
            "save_failed", errors[0] if errors else FILL_ERROR)
    return doc, target
