"""Quebec judicial deadline computation (art. 83 C.p.c.).

Two distinct families live here, and conflating them is the bug this module
exists to prevent:

* **Computation** (``compute_deadline``, ``is_juridical_day``,
  ``next_juridical_day``, ``prev_juridical_day``, ``add_jours_ouvrables``) —
  pure calendar arithmetic implementing art. 83: every day counts, and a raw
  deadline landing on a non-juridical day is pushed further in the direction
  of computation. **No clock is ever read here.** These functions are pinned
  by a frozen reference table in ``tests/test_deadlines.py``.
* **Lateness** (``today_mtl``, ``effective_due``, ``is_past_due``,
  ``days_until``) — the single answer to « is this deadline in the past? »,
  on the **Montréal** calendar, evaluated against the PROROGUED deadline
  (lawyer's decision, 2026-08-02): a due date landing on a non-juridical day
  is actionable until the next juridical day, so it is not « late » until
  the day after that. Due Saturday → actionable Monday → late Tuesday.
  Prorogation can only make lateness start LATER, never earlier — and it is
  a no-op on the computed deadlines (steps, prescription), which already
  land on juridical days by construction.

``today_mtl`` is the one place a clock is read. Every surface that needs
"today" must go through it, or two surfaces drift by up to a day: UTC runs
ahead of Montréal by 4-5 hours, so a UTC-based comparison declares a deadline
past from 20:00 (EDT) / 19:00 (EST) the evening BEFORE.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from tz import MTL


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
    if direction == "after":
        raw = start_date + timedelta(days=delay_days)
        if not is_juridical_day(raw):
            return next_juridical_day(raw)
        return raw
    else:
        raw = start_date - timedelta(days=delay_days)
        if not is_juridical_day(raw):
            return prev_juridical_day(raw)
        return raw


def is_juridical_day(d: date) -> bool:
    """Return True if the date is a juridical day (not a weekend or holiday)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in get_quebec_holidays(d.year):
        return False
    return True


def next_juridical_day(d: date) -> date:
    """Return the next juridical day on or after the given date."""
    current = d
    for _ in range(10):
        if is_juridical_day(current):
            return current
        current += timedelta(days=1)
    return current


def prev_juridical_day(d: date) -> date:
    """Return the previous juridical day on or before the given date."""
    current = d
    for _ in range(10):
        if is_juridical_day(current):
            return current
        current -= timedelta(days=1)
    return current


def last_action_day(deadline: date) -> tuple[date, bool]:
    """The real last day to act before *deadline*, and whether it differs.

    ``prev_juridical_day`` is INCLUSIVE (on-or-before), so on a deadline that
    already falls on a juridical day the last action day IS the deadline and
    the boolean is False. Consumers that surface the date should show it only
    when it differs — otherwise it reads as a duplicated (buggy-looking)
    date. Shared by the dashboard and the MCP get_agenda alert row so the
    two surfaces can never drift.
    """
    last = prev_juridical_day(deadline)
    return last, last != deadline


def today_mtl() -> date:
    """The current calendar date in Montréal — the ONE clock read.

    Not ``date.today()`` (server-local, undefined on App Engine) and not
    ``datetime.now(timezone.utc).date()``: UTC crosses midnight 4-5 hours
    before Montréal does, so a UTC "today" declares a deadline past during
    the whole evening preceding it.
    """
    return datetime.now(timezone.utc).astimezone(MTL).date()


def _as_date(value) -> Optional[date]:
    """Coerce a stored value to its own calendar date, or None.

    Date-only fields are stored at midnight UTC, so their UTC calendar date
    IS the intended day — never convert them to Montréal (that would shift
    them to the previous day). Only ``today`` is Montréal-based; the
    deadline keeps its own calendar date. Same rule as ``mcp.tools.date_str``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return None


def effective_due(deadline) -> Optional[date]:
    """The day a deadline is actionable UNTIL: itself, prorogued if needed.

    ``next_juridical_day`` is inclusive, so a deadline already landing on a
    juridical day is returned unchanged — which makes this a NO-OP for every
    computed deadline in the system (protocol steps, prescription dates all
    go through art. 83 at computation time). It only moves hand-typed dates
    that landed on a weekend or a Québec statutory holiday.
    """
    when = _as_date(deadline)
    if when is None:
        return None
    return next_juridical_day(when)


def is_past_due(deadline, *, today: Optional[date] = None) -> bool:
    """True when the PROROGUED deadline fell strictly BEFORE today (Montréal).

    Two rules compose here, both the lawyer's:
    * a deadline falling ON its (effective) day is NOT past due — the day is
      not over and the act can still be posed;
    * a deadline landing on a non-juridical day prorogues to the next
      juridical day before lateness is evaluated (decision 2026-08-02).
      Due Saturday → actionable Monday → past due Tuesday.

    A missing deadline is never past due (an undated task cannot be late).
    ``today`` is injectable so the rule is testable without a clock.
    """
    when = effective_due(deadline)
    if when is None:
        return False
    return when < (today or today_mtl())


def days_until(deadline, *, today: Optional[date] = None) -> Optional[int]:
    """Whole days from today (Montréal) to the PROROGUED deadline.

    None when undated. Evaluated on ``effective_due`` so the countdown and
    ``is_past_due`` can never disagree: the count reaches zero on the last
    actionable day and goes negative only once the deadline is truly past —
    never « -1 » on something that is not yet late (the dashboard's old
    evening artifact).
    """
    when = effective_due(deadline)
    if when is None:
        return None
    return (when - (today or today_mtl())).days


def add_jours_ouvrables(start: date, n: int) -> date:
    """Add *n* business days: each counted day skips Saturdays, Sundays and
    Québec statutory holidays (the same table ``next_juridical_day`` uses via
    ``is_juridical_day``).

    Serves the notice delays expressed in jours ouvrables (art. 3, Loi sur la
    presse — the ``3_jours_ouvrables`` key of ``utils.recours.AVIS_PERIODS``).
    ``n == 0`` returns *start* unchanged, even when *start* itself is not a
    juridical day.
    """
    current = start
    remaining = n
    while remaining > 0:
        current += timedelta(days=1)
        if is_juridical_day(current):
            remaining -= 1
    return current


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
    holidays: list[date] = []

    # Jour de l'An (January 1)
    jan1 = date(year, 1, 1)
    holidays.append(jan1)
    # If Jan 1 falls on Sunday, Monday Jan 2 is also observed
    if jan1.weekday() == 6:  # Sunday
        holidays.append(date(year, 1, 2))

    # Easter-based holidays
    easter = _easter_sunday(year)
    holidays.append(easter - timedelta(days=2))  # Vendredi saint (Good Friday)
    holidays.append(easter + timedelta(days=1))  # Lundi de Pâques (Easter Monday)

    # Journée nationale des patriotes (last Monday on or before May 24)
    # = the Monday immediately preceding May 25
    may24 = date(year, 5, 24)
    days_since_monday = may24.weekday()  # Monday=0, ..., Sunday=6
    patriots_day = may24 - timedelta(days=days_since_monday)
    holidays.append(patriots_day)

    # Fête nationale du Québec (June 24)
    june24 = date(year, 6, 24)
    holidays.append(june24)
    if june24.weekday() == 6:  # Sunday → Monday observed
        holidays.append(date(year, 6, 25))

    # Fête du Canada (July 1)
    july1 = date(year, 7, 1)
    holidays.append(july1)
    if july1.weekday() == 6:  # Sunday → Monday observed
        holidays.append(date(year, 7, 2))

    # Fête du Travail (1st Monday of September)
    sept1 = date(year, 9, 1)
    days_to_monday = (7 - sept1.weekday()) % 7  # 0 if already Monday
    labour_day = sept1 + timedelta(days=days_to_monday)
    holidays.append(labour_day)

    # Action de grâce (2nd Monday of October)
    oct1 = date(year, 10, 1)
    days_to_monday = (7 - oct1.weekday()) % 7
    first_monday_oct = oct1 + timedelta(days=days_to_monday)
    thanksgiving = first_monday_oct + timedelta(weeks=1)
    holidays.append(thanksgiving)

    # Jour de Noël (December 25)
    dec25 = date(year, 12, 25)
    holidays.append(dec25)
    if dec25.weekday() == 6:  # Sunday → Monday observed
        holidays.append(date(year, 12, 26))

    return holidays


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given year using the Anonymous Gregorian algorithm."""
    # Meeus/Jones/Butcher algorithm
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
    return date(year, month, day)
