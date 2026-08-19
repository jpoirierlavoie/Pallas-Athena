"""Recurring calendar series ("séries") — pure expansion of a repeat pattern.

No Firestore, no Flask — like ``utils/recours.py`` and ``utils/deadlines.py``,
so a series expands identically wherever it is needed and stays fully
unit-testable.

The four frequencies map onto the ``(amount, unit)`` ``Period`` vocabulary
already defined in ``utils/recours.py``, and the arithmetic is that module's:
this one writes NO new date maths. ``_add_months`` clamps the day to the target
month's last, so a monthly series started on a 31st behaves the way a human
means it.

THREE RULES THAT ARE LOAD-BEARING
---------------------------------
1. **Anchor, never chain.** Occurrence *k* is ``add_period(start, amount * k,
   unit)`` — always measured from the ORIGINAL start, never from the previous
   occurrence. Chaining would let the month clamp accumulate: a monthly series
   from 31 janvier would give 28 février, then 28 mars, 28 avril… pinned to the
   28th for ever. Anchoring gives 28 février, 31 mars, 30 avril, which is what
   « le 31 de chaque mois » means.

2. **Dates only — no timezone here.** This module returns ``date`` objects. The
   caller composes each date with the event's wall-clock time and converts it
   through ``tz.mtl_to_utc`` INDIVIDUALLY, which is what keeps a series at the
   same civil time across a DST switch. Adding ``timedelta``s to a stored UTC
   value would silently shift every occurrence after the March or November
   transition by an hour. Keeping this module date-only makes that mistake
   impossible to make here.

3. **No weekend / holiday prorogation.** This module must NEVER import
   ``utils/deadlines``. Art. 83 C.p.c. prorogation governs procedural deadlines
   — the last day to file or serve. A standing meeting forfeits nothing by
   falling on a Saturday, and prorogating would destroy the anchor (« le 15…
   le 17… le 15 »). The caller may FLAG a non-juridical occurrence for the
   lawyer to see; it must never move it.
"""

from __future__ import annotations

from datetime import date, datetime

from utils.format_fr import format_date_fr
from utils.recours import DAYS, MONTHS, YEARS, Period, add_period

# Hard ceiling on how many occurrences one series may hold.
#
# 60 is not a round number picked for comfort — it is the largest value that
# satisfies four independent constraints at once, and raising it breaks them in
# the order listed:
#
#   * an atomic chain delete is ``2N + 1`` Firestore ops (N deletes + N
#     tombstones + 1 CTag bump) against the 500-op batch cap, and the repo's
#     own safety chunk is 450 (``models/folder.py`` ``_BATCH_CHUNK``) → N ≤ 224;
#   * the ``/audiences`` list window is 100 rows with NO pagination control, so
#     two series at this cap already fill the lawyer's own calendar screen;
#   * the Outlook mirror window is 500 rows, and once full it DISARMS orphan
#     deletion for every mirror;
#   * the deletion journal is a 200-row window that every filter is applied to
#     in Python AFTER the fetch.
#
# It buys: hebdomadaire ≈ 14 mois, mensuelle = 5 ans, trimestrielle = 15 ans,
# annuelle = 60 ans. Deliberately a module constant, not a ``Config`` value, so
# a test can pin it.
MAX_SERIE_OCCURRENCES = 60

# key -> (French label, Period). The Period feeds ``recours.add_period``.
FREQUENCIES: dict[str, tuple[str, Period]] = {
    "hebdomadaire": ("Chaque semaine", (7, DAYS)),
    "mensuelle": ("Chaque mois", (1, MONTHS)),
    "trimestrielle": ("Chaque trimestre", (3, MONTHS)),
    "annuelle": ("Chaque année", (1, YEARS)),
}

VALID_FREQUENCIES: tuple[str, ...] = tuple(FREQUENCIES)

# Includes "" so a form select can offer « Ne se répète pas » from one map.
FREQUENCY_LABELS: dict[str, str] = {"": "Ne se répète pas"}
FREQUENCY_LABELS.update({key: label for key, (label, _p) in FREQUENCIES.items()})


def frequency_period(frequency: str) -> Period | None:
    """The ``(amount, unit)`` period of a frequency, or ``None`` if unknown."""
    entry = FREQUENCIES.get(frequency or "")
    return entry[1] if entry else None


def _anchor(start: date, amount: int, unit: str, index: int) -> date:
    """Occurrence ``index`` measured from ``start`` — never from its sibling."""
    moment = datetime(start.year, start.month, start.day)
    return add_period(moment, amount * index, unit).date()


def validate_rule(
    frequency: str,
    *,
    count: int | None = None,
    until: date | None = None,
    start: date | None = None,
) -> list[str]:
    """French validation of a recurrence rule. Empty list = valid.

    An end is MANDATORY and must be given exactly one way — a count or an end
    date, never both, never neither. An unbounded series has no honest
    materialised representation, and the lawyer asked for the bound.
    """
    errors: list[str] = []

    if frequency not in FREQUENCIES:
        errors.append("La fréquence de répétition est invalide.")
        return errors

    if count is None and until is None:
        errors.append(
            "Une série doit se terminer : indiquez une date de fin ou un "
            "nombre d'occurrences."
        )
        return errors
    if count is not None and until is not None:
        errors.append(
            "Indiquez une date de fin OU un nombre d'occurrences, pas les deux."
        )
        return errors

    if count is not None:
        if count < 1:
            errors.append("Le nombre d'occurrences doit être d'au moins 1.")
        elif count > MAX_SERIE_OCCURRENCES:
            errors.append(
                f"Une série ne peut pas dépasser {MAX_SERIE_OCCURRENCES} "
                f"occurrences ({count} demandées)."
            )
        return errors

    # ``until`` is not None from here.
    if start is None:
        errors.append("La date de début est requise pour calculer la série.")
        return errors
    if until < start:
        errors.append("La date de fin doit être après la date de début.")
        return errors

    # Refuse LOUDLY rather than truncate: a silently shortened series is a
    # calendar the lawyer believes is complete and is not.
    amount, unit = FREQUENCIES[frequency][1]
    if _anchor(start, amount, unit, MAX_SERIE_OCCURRENCES) <= until:
        errors.append(
            f"Cette série dépasserait {MAX_SERIE_OCCURRENCES} occurrences. "
            "Rapprochez la date de fin ou choisissez une fréquence plus espacée."
        )
    return errors


def occurrence_dates(
    start: date,
    frequency: str,
    *,
    count: int | None = None,
    until: date | None = None,
) -> list[date]:
    """Expand a rule into its occurrence dates, ``start`` first.

    ``until`` is INCLUSIVE — an occurrence landing exactly on it is kept.
    Raises ``ValueError`` if the rule is invalid; call ``validate_rule`` first
    to surface a French message instead.
    """
    errors = validate_rule(frequency, count=count, until=until, start=start)
    if errors:
        raise ValueError(errors[0])

    amount, unit = FREQUENCIES[frequency][1]

    if count is not None:
        return [_anchor(start, amount, unit, k) for k in range(count)]

    dates: list[date] = []
    for k in range(MAX_SERIE_OCCURRENCES):
        moment = _anchor(start, amount, unit, k)
        if until is not None and moment > until:
            break
        dates.append(moment)
    return dates


def build_rule(
    frequency: str,
    start: date,
    *,
    count: int | None = None,
    until: date | None = None,
) -> dict:
    """The ``serie_rule`` record stored on every occurrence.

    ISO date strings on purpose: this is display / prefill data and must not
    acquire a second timezone convention alongside the stored datetimes.
    """
    rule: dict = {"freq": frequency, "start": start.isoformat()}
    if count is not None:
        rule["count"] = count
    if until is not None:
        rule["until"] = until.isoformat()
    return rule


def describe(rule: dict | None) -> str:
    """« Chaque mois — 12 occurrences » / « Chaque semaine — jusqu'au 3 mai 2027 »."""
    if not rule:
        return ""
    label = FREQUENCY_LABELS.get(rule.get("freq") or "", "")
    if not label or not rule.get("freq"):
        return ""

    count = rule.get("count")
    if isinstance(count, int) and count > 0:
        plural = "s" if count > 1 else ""
        return f"{label} — {count} occurrence{plural}"

    until = rule.get("until")
    if isinstance(until, str) and until:
        try:
            parsed = date.fromisoformat(until)
        except ValueError:
            return label
        return f"{label} — jusqu'au {format_date_fr(parsed)}"
    return label
