"""Recourse & prescription domain logic for dossiers (Québec civil law).

Pure functions and reference tables — no Firestore, no Flask — so the
prescription deadline ("date pour agir") and the value class are computed
identically wherever they are needed, and stay fully unit-testable.

Two reference tables live here and are meant to be edited in ONE place:

* ``PRESCRIPTION_PERIODS`` — the delay options offered in the dossier form,
  each mapping to an ``(amount, unit)`` period (or ``None`` for an
  imprescriptible / to-be-determined recourse).
* ``VALUE_CLASSES`` / ``TOP_CLASS`` — map the amount in dispute ("valeur")
  to a class (Roman numeral). Each band's upper bound is inclusive at the cent.

Note: ``compute_date_pour_agir`` extends a deadline that lands on a
non-juridical day (weekend or Québec statutory holiday) forward to the next
juridical day. It is still an indicative computation — verify every deadline.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone

from utils.deadlines import next_juridical_day

# A period is (amount, unit); the unit drives which calendar arithmetic runs.
Period = tuple[int, str]

DAYS, MONTHS, YEARS = "jours", "mois", "ans"

# ── Delay periods ───────────────────────────────────────────────────────
# key -> (French label shown in the dropdown, Period | None).
# ``None`` = no automatic deadline (imprescriptible, or "autre" to set by hand).
#
# NOT ALL OF THESE ARE PRESCRIPTION. The field is named ``prescription_type``
# for continuity, but the list also carries *déchéance* and *avis* delays the
# taxonomy needs (utils/taxonomie.py `delai_type` records which is which — a
# déchéance neither suspends nor interrupts, so the distinction is not
# cosmetic). Sorted by ascending duration: this is the dropdown order.
#
# LABELS ARE DELIBERATELY GENERIC ("3 ans", not "3 ans, art. 2925 C.c.Q.").
# One period serves many articles — the 1-year period alone covers art. 1635
# (paulienne), 929 (possesseur troublé), 2929 (diffamation) and 115 LNT — so
# baking a single article into the label mislabels every other use. The article
# now travels with the taxonomy action (``utils.taxonomie`` `references`),
# which is where it is actually specific.
PRESCRIPTION_PERIODS: dict[str, tuple[str, "Period | None"]] = {
    "5_jours": ("5 jours", (5, DAYS)),
    "10_jours": ("10 jours", (10, DAYS)),
    "15_jours": ("15 jours", (15, DAYS)),
    "30_jours": ("30 jours", (30, DAYS)),
    "45_jours": ("45 jours", (45, DAYS)),
    "60_jours": ("60 jours", (60, DAYS)),
    "90_jours": ("90 jours", (90, DAYS)),
    "3_mois": ("3 mois", (3, MONTHS)),
    "6_mois": ("6 mois", (6, MONTHS)),
    "9_mois": ("9 mois", (9, MONTHS)),
    "1_an": ("1 an", (1, YEARS)),
    "2_ans": ("2 ans", (2, YEARS)),
    "3_ans": ("3 ans", (3, YEARS)),
    "5_ans": ("5 ans", (5, YEARS)),
    "7_ans": ("7 ans", (7, YEARS)),
    "10_ans": ("10 ans", (10, YEARS)),
    "30_ans": ("30 ans", (30, YEARS)),
    "imprescriptible": ("Imprescriptible", None),
    "autre": ("Autre / à déterminer", None),
}

# Valid keys for model-level validation; "" means "non définie".
VALID_PRESCRIPTION_TYPES: tuple[str, ...] = ("",) + tuple(PRESCRIPTION_PERIODS)

# key -> label, for the dropdown and the detail card (includes the empty state).
PRESCRIPTION_LABELS: dict[str, str] = {
    "": "Non définie",
    **{key: label for key, (label, _period) in PRESCRIPTION_PERIODS.items()},
}


def prescription_period(prescription_type: str) -> "Period | None":
    """Return the ``(amount, unit)`` period for a delay type, else ``None``.

    ``None`` covers four distinct cases the caller cannot tell apart — unknown
    key, "" (non définie), "autre", and "imprescriptible" — all of which mean
    "no automatic deadline". ``models.dossier._apply_prescription_deadline``
    is what disambiguates imprescriptible (clears the date) from unset/autre
    (leaves any hand-entered date alone).
    """
    entry = PRESCRIPTION_PERIODS.get(prescription_type or "")
    return entry[1] if entry else None


# ── Value classes (montant en litige → classe) ──────────────────────────
# Amount in dispute → class (Roman numeral). Each band's upper bound is
# INCLUSIVE at the cent (15 000,00 $ → Classe I; 15 000,01 $ → Classe II), so
# thresholds are held in integer cents to avoid float ambiguity at the edge.
VALUE_CLASSES: tuple[tuple[int, str], ...] = (
    (1_500_000, "I"),      # 0,01 $ – 15 000,00 $
    (8_500_000, "II"),     # 15 000,01 $ – 85 000,00 $
    (30_000_000, "III"),   # 85 000,01 $ – 300 000,00 $
)
TOP_CLASS = "IV"           # 300 000,01 $ et plus


def compute_class(valeur_cents: "int | None") -> "str | None":
    """Return the class (Roman numeral) for an amount in dispute, or ``None``.

    ``valeur`` is stored in integer cents (the app-wide money convention);
    each band's upper bound is inclusive at the cent.
    """
    if valeur_cents is None:
        return None
    for upper_cents, label in VALUE_CLASSES:
        if valeur_cents <= upper_cents:
            return label
    return TOP_CLASS


# ── Date pour agir (limitation deadline) ────────────────────────────────
def _add_years(moment: datetime, years: int) -> datetime:
    """Add whole calendar years, clamping 29 Feb → 28 Feb in common years."""
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, day=28)


def _add_months(moment: datetime, months: int) -> datetime:
    """Add whole calendar months, clamping the day to the target month's last.

    The clamp is the month analogue of ``_add_years``' 29 Feb rule: 31 January
    + 1 mois is 28/29 February, not 3 March. ``calendar.monthrange`` supplies
    the target month's length, so leap years need no special case.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _add_period(moment: datetime, amount: int, unit: str) -> datetime:
    """Add a period, dispatching on its unit."""
    if unit == DAYS:
        return moment + timedelta(days=amount)
    if unit == MONTHS:
        return _add_months(moment, amount)
    if unit == YEARS:
        return _add_years(moment, amount)
    raise ValueError(f"Unité de délai inconnue : {unit!r}")


def compute_date_pour_agir(
    droit_action_date: "datetime | None", prescription_type: str
) -> "datetime | None":
    """Compute the deadline to act = ``droit_action_date`` + the delay period.

    Returns ``None`` when the start date is missing or the delay type carries
    no fixed duration (imprescriptible / autre / non définie). When the raw
    deadline lands on a non-juridical day (weekend or Québec statutory
    holiday), it is extended forward to the next juridical day. The result is
    indicative — every limitation deadline must still be verified.
    """
    if not droit_action_date:
        return None
    period = prescription_period(prescription_type)
    if period is None:
        return None
    raw = _add_period(droit_action_date, *period)
    adjusted = next_juridical_day(raw.date())
    return datetime(
        adjusted.year, adjusted.month, adjusted.day, tzinfo=timezone.utc
    )
