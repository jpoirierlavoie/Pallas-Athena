# PHASE A — Judicial Deadline Calculator

Read CLAUDE.md for project context. This phase adds a foundational utility module for computing legal deadlines according to Quebec civil procedure rules.

## Context

Quebec judicial delays follow art. 83 C.p.c.: all calendar days count in the delay, but if the computed deadline falls on a non-juridical day (Saturday, Sunday, or a statutory holiday), the deadline extends further **in the direction of computation** until it lands on a juridical (permissible) day.

"Direction of computation" means:
- If computing a deadline **after** a start date (e.g., "15 days after service"), and the result lands on a Saturday, the deadline moves to Monday.
- If computing a deadline **before** a target date (e.g., "10 days before the hearing"), and the result lands on a Sunday, the deadline moves to the preceding Friday.

## Step 1 — Create `utils/deadlines.py`

```python
"""Quebec judicial deadline computation (art. 83 C.p.c.)."""

from datetime import date, timedelta
from typing import Literal


def compute_deadline(
    start_date: date,
    delay_days: int,
    direction: Literal["after", "before"] = "after",
) -> date:
    """Compute a judicial deadline from a start date and delay.

    Args:
        start_date: The reference date (e.g., date of service, hearing date).
        delay_days: Number of calendar days in the delay (positive integer).
        direction: "after" = deadline is start_date + delay_days (forward).
                   "before" = deadline is start_date - delay_days (backward).

    Returns:
        The adjusted deadline date. If the raw deadline falls on a
        non-juridical day, it is pushed further in the direction of
        computation until it lands on a juridical day.

    Examples:
        # 15 days after March 1, 2025 = March 16 (Sunday) → March 17 (Monday)
        compute_deadline(date(2025, 3, 1), 15, "after")

        # 10 days before March 14, 2025 = March 4 (Tuesday) → March 4 (no change)
        compute_deadline(date(2025, 3, 14), 10, "before")
    """
    ...


def is_juridical_day(d: date) -> bool:
    """Return True if the date is a juridical day (not a weekend or holiday)."""
    ...


def next_juridical_day(d: date) -> date:
    """Return the next juridical day on or after the given date."""
    ...


def prev_juridical_day(d: date) -> date:
    """Return the previous juridical day on or before the given date."""
    ...


def get_quebec_holidays(year: int) -> list[date]:
    """Return all Quebec statutory holidays for a given year.

    Must include ALL of the following:
    - Jour de l'An (January 1)
    - Vendredi saint (Good Friday — floating, based on Easter)
    - Lundi de Pâques (Easter Monday — floating, based on Easter)
    - Journée nationale des patriotes (Monday preceding May 25)
    - Fête nationale du Québec (June 24)
    - Fête du Canada (July 1)
    - Fête du Travail (1st Monday of September)
    - Action de grâce (2nd Monday of October)
    - Jour de Noël (December 25)

    Also include the January 2 rule: if January 1 falls on a Sunday,
    January 2 is also a non-juridical day (observed holiday).

    Similarly, if June 24 or July 1 or December 25 falls on a Sunday,
    the following Monday is observed.

    For the Easter calculation, implement the Anonymous Gregorian algorithm
    (Meeus/Jones/Butcher) to compute Easter Sunday, then derive Good Friday
    (Easter - 2) and Easter Monday (Easter + 1).
    """
    ...


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given year using the Anonymous Gregorian algorithm."""
    # Implement the Meeus/Jones/Butcher algorithm
    # This is a well-known algorithm — implement it directly, do not use a library
    ...
```

## Step 2 — Implementation Details

**Easter calculation (Anonymous Gregorian / Meeus-Jones-Butcher):**
```
a = year % 19
b = year // 100
c = year % 100
d = b // 4
e = b % 4
f = (b + 8) // 25
g = (b - f + 1) // 3
h = (19 * a + b - d - g + 15) % 30
i = c // 4
k = c % 4
l = (32 + 2 * e + 2 * i - h - k) % 7
m = (a + 11 * h + 22 * l) // 451
month = (h + l - 7 * m + 114) // 31
day = ((h + l - 7 * m + 114) % 31) + 1
Easter Sunday = date(year, month, day)
```

**Holiday observation rules for Quebec:**
- When a fixed holiday (Jan 1, June 24, July 1, Dec 25) falls on Sunday, the Monday is observed as the statutory holiday. The Sunday itself is already non-juridical (weekend), and the Monday becomes non-juridical too.
- When a fixed holiday falls on Saturday, it is NOT moved to Friday. Saturday is already non-juridical. No additional day is added.
- Floating holidays (Patriots' Day, Labour Day, Thanksgiving) always land on Monday by definition.
- Good Friday is always Friday. Easter Monday is always Monday.

**The `compute_deadline` function logic:**
1. Compute raw deadline: `start_date + timedelta(days=delay_days)` or `start_date - timedelta(days=delay_days)`
2. Check if raw deadline is juridical
3. If not, push further in the direction:
   - direction="after" → call `next_juridical_day(raw)`
   - direction="before" → call `prev_juridical_day(raw)`
4. Return adjusted date

**`next_juridical_day` and `prev_juridical_day`:**
- Loop day-by-day in the appropriate direction until `is_juridical_day()` returns True
- Safety cap: max 10 iterations (no holiday cluster is longer than ~4 days)

**`is_juridical_day`:**
- Return False if Saturday or Sunday
- Return False if date is in `get_quebec_holidays(date.year)`
- Return True otherwise

## Step 3 — Integrate into Protocol Module

Update `models/protocol.py`:

1. Replace the existing `_compute_deadline` function:
```python
# OLD:
def _compute_deadline(start_date: datetime, offset_days: int) -> datetime:
    return start_date + timedelta(days=offset_days)

# NEW:
from utils.deadlines import compute_deadline as _judicial_deadline

def _compute_deadline(start_date: datetime, offset_days: int) -> datetime:
    """Compute a protocol step deadline using judicial delay rules."""
    result_date = _judicial_deadline(start_date.date(), offset_days, direction="after")
    return datetime.combine(result_date, datetime.min.time(), tzinfo=timezone.utc)
```

2. This change automatically propagates to `create_protocol` and `recompute_deadlines` since they both call `_compute_deadline`.

## Step 4 — Integrate into Dashboard Prescription Alerts

Update `routes/dashboard.py` — the `_get_prescription_alerts` function should display the judicially-adjusted prescription date alongside the raw one if they differ. No change to stored data — the adjustment is display-only for prescriptions (the stored date is the actual prescription date; the judicial adjustment tells the user when the last juridical day to act falls).

Add to the dashboard template context: for each prescription alert, compute `last_action_date = prev_juridical_day(prescription_date)` and include it in the display if it differs from the prescription date.

## Step 5 — Create `utils/__init__.py`

Empty file to make `utils` a Python package:
```python
"""Utility modules for Pallas Athena."""
```

## Step 6 — Add Unit Tests

Create `tests/test_deadlines.py` with these test cases:

```python
def test_basic_forward_deadline():
    """15 days after a date that lands on a weekday stays unchanged."""

def test_forward_lands_on_saturday():
    """Deadline landing on Saturday moves to Monday."""

def test_forward_lands_on_sunday():
    """Deadline landing on Sunday moves to Monday."""

def test_backward_lands_on_saturday():
    """Backward deadline landing on Saturday moves to Friday."""

def test_backward_lands_on_sunday():
    """Backward deadline landing on Sunday moves to Friday."""

def test_forward_lands_on_holiday():
    """Deadline landing on Fête nationale (June 24) moves to next juridical day."""

def test_forward_lands_on_holiday_before_weekend():
    """Deadline landing on Friday holiday moves to Monday."""

def test_easter_2025():
    """Easter Sunday 2025 = April 20. Good Friday = April 18. Easter Monday = April 21."""

def test_patriots_day_2025():
    """Patriots' Day 2025 = Monday May 19 (Monday before May 25)."""

def test_thanksgiving_2025():
    """Thanksgiving 2025 = Monday October 13 (2nd Monday of October)."""

def test_labour_day_2025():
    """Labour Day 2025 = Monday September 1."""

def test_christmas_on_sunday():
    """When Dec 25 is Sunday, Dec 26 (Monday) is observed."""

def test_new_years_on_sunday():
    """When Jan 1 is Sunday, Jan 2 (Monday) is observed."""

def test_zero_delay():
    """0-day delay returns the start date itself (adjusted if non-juridical)."""

def test_holiday_cluster():
    """Good Friday + Easter Monday creates a 4-day non-juridical window."""
```

Run tests with: `python -m pytest tests/test_deadlines.py -v`

## Testing Checklist
- [ ] `compute_deadline` with direction="after" works for weekdays, weekends, holidays
- [ ] `compute_deadline` with direction="before" works for weekdays, weekends, holidays
- [ ] `get_quebec_holidays` returns correct holidays for 2024, 2025, 2026
- [ ] Easter calculation is correct for 2024 (March 31), 2025 (April 20), 2026 (April 5)
- [ ] Holiday observation rules (Sunday → Monday) work correctly
- [ ] Protocol deadline computation now uses judicial rules
- [ ] Existing protocols still display correctly (no regression)
- [ ] Dashboard prescription alerts show last juridical action date
- [ ] All unit tests pass
