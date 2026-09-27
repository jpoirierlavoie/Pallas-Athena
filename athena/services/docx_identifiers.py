"""The strings that must not survive into a firm-wide template (lot 2A, T5).

:func:`dossier_identifiers` answers one question for the leak scan
(``utils/docx_leak_scan.scan_identifiers``): given the dossier a document
comes from, which names, numbers and addresses would identify that matter if
they rode into a gabarit every future client's letters are filled from?

What it collects:

* from the DOSSIER — its title (« Tremblay c. Lavoie »), our file number,
  the court file number (never the « Préjudiciaire » placeholder, which is a
  word, not an identifier), and the name SNAPSHOTS its party entries carry
  (each party's and each lawyer's name, with and without civility);
* from each PARTY the dossier links — every client and opposing party, the
  lawyer recorded against each, and every mandataire (tutor, curator…) of
  those parties: the name with and without civility, « Nom Prénom » (the
  order of an intitulé), the surname alone when it is at least four letters
  long, an organisation's legal and trade names, the personal and work
  emails, every phone in the forms a letter prints it (E.164, the ten
  national digits, « (514) 555-1234 »), both street addresses, both postal
  codes with and without their space, the NEQ and the bar number.

What it leaves out: the FIRM's own details (``utils.cabinet.cabinet_dict``).
The firm appears on every letter it sends — its name, address, phone and
email are the letterhead — so an identifier whose words all occur inside
one of the firm's own strings is dropped; otherwise the lawyer's own record,
often linked as a party's lawyer, would flag every template ever
registered. The residue, stated: a client whose surname is part of the
firm's name (a « Lavoie » at « Poirier Lavoie, avocat ») loses the
surname-alone check — never the full name, which is not inside the firm's.

Fail-CLOSED. The parties are read with ``partie.get_parties_bulk``, which
fails open to ``{}`` (and simply omits an id that does not exist); here any
id that did not come back — a read error or a record that vanished despite
the foreign-key rule that forbids deleting a linked party — raises
:class:`IdentifiersUnavailable`. A leak scan that is quietly incomplete
would pass a template it should have refused.

No log line, no write: a read-and-derive function. Its caller logs.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from models.dossier import PREJUDICIAIRE_FILE_NUMBER
from models.partie import display_name, get_parties_bulk
from utils.cabinet import cabinet_dict
from utils.docx_leak_scan import fold_key, words_within
from utils.template_fields import _nom_bare, _strip_civility_prefix

__all__ = ["IdentifiersUnavailable", "dossier_identifiers"]

# A surname alone is only worth checking when it is not a common short word.
_SURNAME_MIN_CHARS = 4
_ASCII_DIGITS = frozenset("0123456789")


class IdentifiersUnavailable(Exception):
    """The identifiers could not ALL be built. French message, no content."""

    def __init__(self, message: str, *, missing: int = 0) -> None:
        super().__init__(message)
        self.missing = missing


def _text(value: Any) -> str:
    """One stored value as a string. The CardDAV path can store a LIST in an
    address component (the vobject unescaped-comma case — see
    ``utils.template_fields._address_component``): joined, never dropped."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(str(v).strip() for v in value if isinstance(v, str)).strip()
    return ""


def _name_forms(name: Any) -> list[str]:
    """A snapshot name as stored, and without its civility (Me / Mme / M.)."""
    shown = _text(name)
    if not shown:
        return []
    return [shown, _strip_civility_prefix(shown)]


def _phone_forms(value: Any) -> list[str]:
    """The forms a letter prints a phone number in."""
    raw = _text(value)
    if not raw:
        return []
    digits = "".join(c for c in raw if c in _ASCII_DIGITS)
    national = ""
    if len(digits) == 11 and digits.startswith("1"):
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    forms = [raw]
    if national:
        forms += [
            national,
            "1" + national,
            f"({national[:3]}) {national[3:6]}-{national[6:]}",
        ]
    elif digits:
        forms.append(digits)
    return forms


def _postal_forms(value: Any) -> list[str]:
    raw = _text(value)
    if not raw:
        return []
    compact = "".join(raw.split())
    forms = [raw, compact]
    if len(compact) == 6:
        forms.append(f"{compact[:3]} {compact[3:]}")
    return forms


def _party_forms(partie: Mapping) -> list[str]:
    forms: list[str] = []
    if partie.get("type") == "organization":
        forms += [_text(partie.get("organization_name")),
                  _text(partie.get("trade_name"))]
    else:
        forms += [_text(display_name(dict(partie))), _text(_nom_bare(dict(partie)))]
        first = _text(partie.get("first_name"))
        last = _text(partie.get("last_name"))
        if first and last:
            forms.append(f"{last} {first}")
        if len(last) >= _SURNAME_MIN_CHARS:
            forms.append(last)
    forms += [_text(partie.get("email")), _text(partie.get("email_work"))]
    for key in ("phone_home", "phone_cell", "phone_work", "fax"):
        forms += _phone_forms(partie.get(key))
    for key in ("address_street", "work_address_street"):
        forms.append(_text(partie.get(key)))
    for key in ("address_postal_code", "work_address_postal_code"):
        forms += _postal_forms(partie.get(key))
    forms += [_text(partie.get("company_neq")), _text(partie.get("bar_number"))]
    return forms


def _firm_forms(firm: Mapping) -> list[str]:
    forms: list[str] = []
    forms += _name_forms(firm.get("nom"))
    forms.append(_text(firm.get("organisation")))
    forms.append(_text(firm.get("adresse_civique")))
    forms += _postal_forms(firm.get("code_postal"))
    for key in ("telephone", "telecopieur"):
        forms += _phone_forms(firm.get(key))
    forms.append(_text(firm.get("courriel")))
    return [f for f in forms if f]


def _entries(dossier: Mapping) -> list[Mapping]:
    out: list[Mapping] = []
    for key in ("clients", "opposing_parties"):
        for entry in dossier.get(key) or []:
            if isinstance(entry, Mapping):
                out.append(entry)
    return out


def _linked_ids(dossier: Mapping, entries: list[Mapping]) -> list[str]:
    ids: list[str] = []
    for entry in entries:
        ids += [_text(entry.get("id")), _text(entry.get("avocat_id"))]
    for key in ("client_ids", "opposing_party_ids", "avocat_ids"):
        ids += [_text(i) for i in dossier.get(key) or []]
    return [i for i in dict.fromkeys(ids) if i]


def _load(ids: list[str]) -> dict[str, dict]:
    """``{id: partie}`` for EVERY id, or :class:`IdentifiersUnavailable`."""
    if not ids:
        return {}
    found = get_parties_bulk(ids)
    missing = [i for i in ids if i not in found]
    if missing:
        raise IdentifiersUnavailable(
            f"Les fiches de {len(missing)} partie(s) liée(s) au dossier sont "
            "illisibles ou introuvables : le contrôle des identifiants ne "
            "peut pas conclure. Réessayez dans un instant.",
            missing=len(missing),
        )
    return found


def _mandataire_ids(parties: Iterable[Mapping], known: set[str]) -> list[str]:
    ids: list[str] = []
    for partie in parties:
        for entry in partie.get("mandataires") or []:
            if isinstance(entry, Mapping):
                mid = _text(entry.get("id"))
                if mid and mid not in known:
                    ids.append(mid)
    return list(dict.fromkeys(ids))


def dossier_identifiers(
    dossier: Mapping, *, firm: Optional[Mapping] = None
) -> list[str]:
    """The strings the leak scan must look for in a document of *dossier*.

    *dossier* is a record as ``get_dossier`` returns it (party arrays
    migrated). *firm* is ``cabinet_dict()``'s shape; ``None`` reads it.

    Returns distinct strings (by the scan's own folding — the first
    spelling wins), in a stable order: the dossier's own, then each
    party's. Raises :class:`IdentifiersUnavailable` when a linked party
    cannot be read.
    """
    if not isinstance(dossier, Mapping):
        raise IdentifiersUnavailable(
            "Le dossier est illisible : le contrôle des identifiants ne peut "
            "pas conclure."
        )
    entries = _entries(dossier)
    forms: list[str] = [_text(dossier.get("title")),
                        _text(dossier.get("file_number"))]
    court = _text(dossier.get("court_file_number"))
    if court and court != PREJUDICIAIRE_FILE_NUMBER:
        forms.append(court)
    for entry in entries:
        forms += _name_forms(entry.get("name"))
        forms += _name_forms(entry.get("avocat_name"))

    parties = _load(_linked_ids(dossier, entries))
    mandataires = _load(_mandataire_ids(parties.values(), set(parties)))
    for partie in list(parties.values()) + list(mandataires.values()):
        forms += _party_forms(partie)

    firm_keys = [fold_key(f) for f in _firm_forms(
        firm if firm is not None else cabinet_dict())]
    kept: dict[tuple, str] = {}
    for form in forms:
        key = fold_key(form)
        if not key or key in kept:
            continue
        if any(words_within(key, firm_key) for firm_key in firm_keys):
            continue
        kept[key] = form
    return list(kept.values())
