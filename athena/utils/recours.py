"""Recourse & prescription domain logic for dossiers (Québec civil law).

Pure functions and reference tables — no Firestore, no Flask — so the
prescription deadline ("date pour agir") and the value class are computed
identically wherever they are needed, and stay fully unit-testable.

Two reference tables live here and are meant to be edited in ONE place:

* ``PRESCRIPTION_PERIODS`` — the extinctive-prescription options offered in
  the dossier form, each mapping to a duration in years (or ``None`` for an
  imprescriptible / to-be-determined recourse). Defaults follow the Code
  civil du Québec; adjust the list to your practice.
* ``VALUE_CLASSES`` / ``TOP_CLASS`` — map the amount in dispute ("valeur")
  to a class (Roman numeral). Each band's upper bound is inclusive at the cent.

Note: ``compute_date_pour_agir`` extends a deadline that lands on a
non-juridical day (weekend or Québec statutory holiday) forward to the next
juridical day. It is still an indicative computation — verify every deadline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from utils.deadlines import next_juridical_day

# ── Prescription periods (extinctive prescription, C.c.Q.) ──────────────
# key -> (French label shown in the dropdown, duration in years | None).
# ``None`` = no automatic deadline (imprescriptible, or "autre" to set by hand).
PRESCRIPTION_PERIODS: dict[str, tuple[str, "int | None"]] = {
    "1_an": ("1 an, art. 2929 C.c.Q.", 1),
    "3_ans": ("3 ans, art. 2925 C.c.Q.", 3),
    "10_ans": ("10 ans, art. 2922 C.c.Q.", 10),
    "30_ans": ("30 ans, art. 2926.1 C.c.Q.", 30),
    "imprescriptible": ("Imprescriptible, art. 2926.1(2) C.c.Q.", None),
    "autre": ("Autre / à déterminer", None),
}

# Valid keys for model-level validation; "" means "non définie".
VALID_PRESCRIPTION_TYPES: tuple[str, ...] = ("",) + tuple(PRESCRIPTION_PERIODS)

# key -> label, for the dropdown and the detail card (includes the empty state).
PRESCRIPTION_LABELS: dict[str, str] = {
    "": "Non définie",
    **{key: label for key, (label, _years) in PRESCRIPTION_PERIODS.items()},
}


def prescription_years(prescription_type: str) -> "int | None":
    """Return the duration in years for a prescription type, else ``None``."""
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


def compute_date_pour_agir(
    droit_action_date: "datetime | None", prescription_type: str
) -> "datetime | None":
    """Compute the deadline to act = ``droit_action_date`` + prescription period.

    Returns ``None`` when the start date is missing or the prescription type
    carries no fixed duration (imprescriptible / autre / non définie). When the
    raw anniversary lands on a non-juridical day (weekend or Québec statutory
    holiday), the deadline is extended forward to the next juridical day. The
    result is indicative — every limitation deadline must still be verified.
    """
    if not droit_action_date:
        return None
    years = prescription_years(prescription_type)
    if years is None:
        return None
    raw = _add_years(droit_action_date, years)
    adjusted = next_juridical_day(raw.date())
    return datetime(
        adjusted.year, adjusted.month, adjusted.day, tzinfo=timezone.utc
    )
