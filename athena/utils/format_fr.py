"""French currency, date, number, and rate formatting (Phase H.2).

Pure functions — no Firestore, no Flask, no new dependencies. Centralized so
the invoice note-d'honoraires builder and (optionally, as a follow-up) the
on-screen invoice and the reportlab PDF all format money identically, to the
cent.

Conventions (Québec / fr-CA):
- comma decimal separator, non-breaking-space (U+00A0) thousands grouping;
- currency suffixed with a non-breaking space then ``$`` (``"1 150,00 $"``);
- long French dates (``"11 décembre 2025"``, ``1er`` for the first).
"""

from datetime import date, datetime
from decimal import Decimal

_NBSP = " "

_FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
)


def _group_thousands(digits: str) -> str:
    """Group an unsigned integer digit-string in threes with a NBSP."""
    groups: list[str] = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return _NBSP.join(groups)


def format_cents_fr(cents: int) -> str:
    """``115000`` → ``"1 150,00 $"`` (NBSP thousands, comma decimal).

    Zero → ``"0,00 $"`` (never a bare ``0``). Negative amounts keep a leading
    ``-`` (parenthesized display is :func:`format_cents_fr_parens`).
    """
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{_group_thousands(str(whole))},{frac:02d}{_NBSP}$"


def format_cents_fr_parens(cents: int) -> str:
    """Parenthesized deduction: ``115000`` → ``"(1 150,00) $"``; ``0`` → ``"(0,00) $"``.

    Used for « Avances en fidéicommis » — a retainer shown as a subtraction.
    """
    # Drop the trailing NBSP + "$" from the magnitude, wrap in parens, re-add.
    amount = format_cents_fr(abs(int(cents)))[:-2]
    return f"({amount}){_NBSP}$"


def format_rate_fr(rate: int, scale: int) -> str:
    """A stored tax-rate integer as a French percentage.

    The invoice stores GST ×100 (``500`` → ``"5 %"``) and QST ×1000
    (``9975`` → ``"9,975 %"``) — different scales for QST's third decimal —
    so the caller passes the matching ``scale``. Trailing zeros are trimmed.
    """
    pct = Decimal(int(rate)) / Decimal(int(scale))
    text = f"{pct:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')}{_NBSP}%"


def format_hours_fr(hours: float) -> str:
    """``0.5`` → ``"0,50"`` (two decimals, comma separator, no unit)."""
    return f"{float(hours):.2f}".replace(".", ",")


def format_date_fr(value: date | datetime) -> str:
    """``date(2025, 12, 11)`` → ``"11 décembre 2025"`` (``1er`` for the first).

    Accepts a ``datetime`` too and uses its own calendar date (invoice
    date-only fields are stored as midnight UTC — take the UTC date, never a
    Montréal conversion that would shift to the previous day).
    """
    if isinstance(value, datetime):
        value = value.date()
    day = "1er" if value.day == 1 else str(value.day)
    return f"{day} {_FRENCH_MONTHS[value.month - 1]} {value.year}"
