"""Firm info in the Phase H catalog shape (``cabinet.*`` resolvers).

One authority for the dict the template catalog's ``cabinet.*`` fields
resolve from — previously duplicated verbatim in ``routes/invoices.py``
and ``routes/doc_templates.py`` (and about to grow a third copy for the
note-print flow, which is what forced the hoist).
"""

from config import Config
from utils.validators import format_phone_display


def cabinet_dict() -> dict:
    street = Config.FIRM_STREET
    if Config.FIRM_UNIT:
        street = f"{street}, {Config.FIRM_UNIT}" if street else Config.FIRM_UNIT
    try:
        telephone = (
            format_phone_display(Config.FIRM_PHONE) if Config.FIRM_PHONE else ""
        )
    except Exception:
        telephone = Config.FIRM_PHONE or ""
    try:
        telecopieur = (
            format_phone_display(Config.FIRM_FAX) if Config.FIRM_FAX else ""
        )
    except Exception:
        telecopieur = Config.FIRM_FAX or ""
    return {
        "nom": Config.FIRM_NAME,
        "adresse_civique": street,
        "ville": Config.FIRM_CITY,
        "province": Config.FIRM_PROVINCE,
        "code_postal": Config.FIRM_POSTAL_CODE,
        "telephone": telephone,
        "telecopieur": telecopieur,
        "courriel": Config.FIRM_EMAIL,
    }
