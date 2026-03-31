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
