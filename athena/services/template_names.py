"""A template RENAME, checked against its source dossiers (fixups of lot 2A).

A gabarit's name prints into the name of every document generated from it,
for every client (« REF - date - Projet {name} »,
``models.document.projet_document_name``). The name of a NEW template was
already checked against the dossier its file came from (the review of lot 2A
T9); a later rename was checked against NOTHING — the connector's
``update_template`` metadata mode and the web edit form alike — so « Mise en
demeure Tremblay » could be typed onto a firm-wide template and print on the
next client's letters (the critic of lot 2A, finding « medium »).

The template now RECORDS where its files came from: ``source_dossier_id`` at
creation, and each version entry's own (``models.doc_template``). A rename is
checked against every one of those dossiers' identifiers
(``services.docx_identifiers.dossier_identifiers`` — parties, numbers,
addresses, the firm's own excluded) with the SAME matcher as the creation
check (``utils.docx_leak_scan.text_residues``: folding, whole words, the
``accept`` escape hatch).

What it cannot do, stated: a template with NO recorded source — uploaded
through the web form, created before these fixups, or through an upload
ticket that declared ``aucun_dossier_source`` — has nothing to be checked
against, and ``performed`` is then False (the connector's result says so);
a recorded source dossier that no longer exists is reported, not checked.
It checks the dossiers the files came from, never the whole firm: a name
typed after ANOTHER client is not caught.

Fail CLOSED: an unreadable version list, dossier or linked party raises
:class:`NameCheckUnavailable` — never « nothing found ». No write, no log
line: its callers (``models.doc_template.update_template``, which enforces
it on every path, and the connector's handler, which names each residue)
refuse and log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from models import doc_template as doc_template_model
from models.dossier import get_dossier_strict
from services import docx_identifiers as identifiers_service
from utils.cabinet import cabinet_dict
from utils.docx_leak_scan import LeakScanError, fold_key, text_residues

__all__ = ["NameCheck", "NameCheckUnavailable", "check_rename"]


class NameCheckUnavailable(Exception):
    """The check could not read everything it needs. French message."""


@dataclass(frozen=True)
class NameCheck:
    """What :func:`check_rename` found.

    * ``performed`` — False when no source dossier is recorded, or none of
      the recorded ones exists any more: NOTHING was checked;
    * ``dossier_ids`` — the dossiers actually checked;
    * ``missing_dossier_ids`` — recorded sources that no longer exist;
    * ``residues`` — identifiers found in the name and NOT accepted: the
      caller refuses unless empty;
    * ``accepted`` — found, and named in ``accept``;
    * ``unused_accept`` — ``accept`` entries matching nothing found.
    """

    performed: bool
    dossier_ids: tuple[str, ...] = ()
    missing_dossier_ids: tuple[str, ...] = ()
    residues: tuple[str, ...] = ()
    accepted: tuple[str, ...] = ()
    unused_accept: tuple[str, ...] = ()


_UNREADABLE = (
    "Le contrôle du nom n'a pas pu lire le dossier source de ce gabarit : "
    "réessayez dans un instant."
)


def check_rename(
    template: Mapping, name: str, *, accept: Iterable[str] = (),
) -> NameCheck:
    """Check *name* — the name *template* would take — against the
    identifiers of every dossier its files came from. Raises
    :class:`NameCheckUnavailable` when anything it needs is unreadable."""
    accept = [a for a in accept if isinstance(a, str)]
    try:
        sources = doc_template_model.template_source_dossier_ids(dict(template))
    except doc_template_model.TemplateReadError as exc:
        raise NameCheckUnavailable(_UNREADABLE) from exc
    if not sources:
        return NameCheck(performed=False)

    checked: list[str] = []
    missing: list[str] = []
    identifiers: list[str] = []
    firm = cabinet_dict()
    for dossier_id in sources:
        try:
            dossier = get_dossier_strict(dossier_id)
        except Exception as exc:
            raise NameCheckUnavailable(_UNREADABLE) from exc
        if dossier is None:
            missing.append(dossier_id)
            continue
        try:
            identifiers += identifiers_service.dossier_identifiers(
                dossier, firm=firm)
        except identifiers_service.IdentifiersUnavailable as exc:
            raise NameCheckUnavailable(str(exc)) from exc
        checked.append(dossier_id)
    if not checked:
        return NameCheck(performed=False, missing_dossier_ids=tuple(missing))

    try:
        found = text_residues(name, identifiers, accept=accept)
    except LeakScanError as exc:
        raise NameCheckUnavailable(str(exc)) from exc
    found_keys = {fold_key(s) for s in found.residues + found.accepted}
    unused = tuple(a for a in accept if fold_key(a) not in found_keys)
    return NameCheck(
        performed=True,
        dossier_ids=tuple(checked),
        missing_dossier_ids=tuple(missing),
        residues=found.residues,
        accepted=found.accepted,
        unused_accept=unused,
    )
