"""Firm info in the Phase H catalog shape (``cabinet.*`` resolvers).

One authority for the dict the template catalog's ``cabinet.*`` fields
resolve from — previously duplicated verbatim in ``routes/invoices.py`` and
``routes/doc_templates.py`` (and about to grow a third copy for the
note-print flow, which is what forced the hoist).

THE LIVE VALUES NOW COME FROM FIRESTORE (``models/settings.py``, the
``settings/cabinet`` singleton), with ``Config.FIRM_*`` demoted to a seed and
a fallback. ``get_cabinet`` fails open, so this function keeps its old
contract: it always returns a complete dict and never raises.

THE PHONE FORMAT IS DECIDED HERE, and that is deliberate. Values are stored
E.164 (``+15147372525``) so they can be validated, but four surfaces print
the result and two of them do no stripping at all — ``utils/budget_pdf``'s
footer and every gabarit's ``{{cabinet.telephone}}``, both client-facing. So
the local display form (``(514) 737-2525``) is produced once, here, which
keeps every generated document byte-identical to what it printed when the
values lived in ``app.yaml`` as pre-formatted strings.

``organisation`` is the FIRM's name; ``nom`` is the LAWYER's. A consumer
needing a firm designation may fall back from ``organisation`` to ``nom``,
never the reverse — an organisation does not sign a KYC verification.
"""

from utils.validators import format_phone_display

# Keys every consumer may rely on. Exported so a test can pin the set by
# derivation instead of a hand-written literal that decays.
CABINET_KEYS: tuple[str, ...] = (
    "nom",
    "organisation",
    "adresse_civique",
    "ville",
    "province",
    "code_postal",
    "telephone",
    "telecopieur",
    "courriel",
    "gst_number",
    "qst_number",
)


def display_phone(e164: str) -> str:
    """E.164 -> the local display form, tolerating an unnormalized value.

    ``format_phone_display`` returns a non-``+`` input unchanged, so a legacy
    or hand-edited value survives as typed. The ``+1 `` prefix is dropped
    because that is what the firm's own stationery, the budget-PDF footer and
    the accusé bordereau have always shown; ``routes/taches_portail`` used to
    carry two ``.removeprefix("+1 ")`` calls for a case that could never fire
    while the value was a display string, and they are gone with this change.
    """
    if not e164:
        return ""
    try:
        shown = format_phone_display(e164)
    except Exception:
        return e164
    return shown.removeprefix("+1 ")


def cabinet_dict() -> dict:
    # Lazy import: models/__init__.py builds the Firestore client at import,
    # so a module-level import would make this module un-importable by the
    # pure tests that only exercise the formatting above.
    from models.settings import get_cabinet

    cab = get_cabinet()

    street = cab.get("address_street") or ""
    unit = cab.get("address_unit") or ""
    if unit:
        street = f"{street}, {unit}" if street else unit

    return {
        "nom": cab.get("nom") or "",
        "organisation": cab.get("organisation") or "",
        "adresse_civique": street,
        "ville": cab.get("address_city") or "",
        "province": cab.get("address_province") or "",
        "code_postal": cab.get("address_postal_code") or "",
        "telephone": display_phone(cab.get("telephone") or ""),
        "telecopieur": display_phone(cab.get("telecopieur") or ""),
        "courriel": cab.get("courriel") or "",
        "gst_number": cab.get("gst_number") or "",
        "qst_number": cab.get("qst_number") or "",
    }
