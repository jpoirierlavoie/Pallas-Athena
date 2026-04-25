"""Unit tests for utils/deadlines.py — Quebec judicial deadline computation."""

import sys
import os

# Ensure athena/ is on the path when running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from utils.deadlines import (
    compute_deadline,
    is_juridical_day,
    next_juridical_day,
    prev_juridical_day,
    get_quebec_holidays,
    _easter_sunday,
)


# ── Easter calculation ────────────────────────────────────────────────────


def test_easter_2024():
    """Easter Sunday 2024 = March 31."""
    assert _easter_sunday(2024) == date(2024, 3, 31)


def test_easter_2025():
    """Easter Sunday 2025 = April 20. Good Friday = April 18. Easter Monday = April 21."""
    assert _easter_sunday(2025) == date(2025, 4, 20)
    holidays = get_quebec_holidays(2025)
    assert date(2025, 4, 18) in holidays  # Good Friday
    assert date(2025, 4, 21) in holidays  # Easter Monday


def test_easter_2026():
    """Easter Sunday 2026 = April 5."""
    assert _easter_sunday(2026) == date(2026, 4, 5)


# ── Holiday computation ───────────────────────────────────────────────────


def test_patriots_day_2025():
    """Patriots' Day 2025 = Monday May 19 (Monday before May 25)."""
    holidays = get_quebec_holidays(2025)
    assert date(2025, 5, 19) in holidays
    # May 25 itself (Sunday) is not Patriots' Day
    assert date(2025, 5, 25) not in holidays


def test_labour_day_2025():
    """Labour Day 2025 = Monday September 1 (1st Monday of September)."""
    holidays = get_quebec_holidays(2025)
    assert date(2025, 9, 1) in holidays


def test_thanksgiving_2025():
    """Thanksgiving 2025 = Monday October 13 (2nd Monday of October)."""
    holidays = get_quebec_holidays(2025)
    assert date(2025, 10, 13) in holidays


def test_christmas_on_sunday():
    """When Dec 25 is Sunday (2022), Dec 26 (Monday) is also observed."""
    # Dec 25, 2022 = Sunday
    assert date(2022, 12, 25).weekday() == 6  # confirm Sunday
    holidays = get_quebec_holidays(2022)
    assert date(2022, 12, 25) in holidays
    assert date(2022, 12, 26) in holidays


def test_new_years_on_sunday():
    """When Jan 1 is Sunday (2023), Jan 2 (Monday) is also observed."""
    # Jan 1, 2023 = Sunday
    assert date(2023, 1, 1).weekday() == 6  # confirm Sunday
    holidays = get_quebec_holidays(2023)
    assert date(2023, 1, 1) in holidays
    assert date(2023, 1, 2) in holidays


def test_fete_nationale_on_sunday():
    """When June 24 falls on Sunday, June 25 (Monday) is also observed."""
    # Find a year where June 24 is Sunday
    # June 24, 2018 = Sunday
    assert date(2018, 6, 24).weekday() == 6  # confirm Sunday
    holidays = get_quebec_holidays(2018)
    assert date(2018, 6, 24) in holidays
    assert date(2018, 6, 25) in holidays


def test_canada_day_on_sunday():
    """When July 1 falls on Sunday, July 2 (Monday) is also observed."""
    # July 1, 2018 = Sunday
    assert date(2018, 7, 1).weekday() == 6  # confirm Sunday
    holidays = get_quebec_holidays(2018)
    assert date(2018, 7, 1) in holidays
    assert date(2018, 7, 2) in holidays


def test_all_holidays_2025_count():
    """2025 has exactly 9 standard holidays (no Sunday-observation extras)."""
    holidays = get_quebec_holidays(2025)
    # No fixed holiday falls on Sunday in 2025, so exactly 9
    assert len(holidays) == 9


# ── is_juridical_day ─────────────────────────────────────────────────────


def test_is_juridical_day_weekday():
    """A regular Monday is a juridical day."""
    assert is_juridical_day(date(2025, 3, 3)) is True  # Monday


def test_is_juridical_day_saturday():
    """Saturday is not a juridical day."""
    assert is_juridical_day(date(2025, 3, 1)) is False  # Saturday


def test_is_juridical_day_sunday():
    """Sunday is not a juridical day."""
    assert is_juridical_day(date(2025, 3, 2)) is False  # Sunday


def test_is_juridical_day_holiday():
    """A statutory holiday is not a juridical day."""
    assert is_juridical_day(date(2025, 1, 1)) is False  # Jour de l'An


def test_is_juridical_day_good_friday():
    """Good Friday is not a juridical day."""
    assert is_juridical_day(date(2025, 4, 18)) is False


def test_is_juridical_day_easter_monday():
    """Easter Monday is not a juridical day."""
    assert is_juridical_day(date(2025, 4, 21)) is False


# ── next_juridical_day / prev_juridical_day ───────────────────────────────


def test_next_juridical_day_already_juridical():
    """A day that is already juridical returns itself."""
    assert next_juridical_day(date(2025, 3, 3)) == date(2025, 3, 3)  # Monday


def test_next_juridical_day_from_saturday():
    """Saturday → Monday."""
    assert next_juridical_day(date(2025, 3, 1)) == date(2025, 3, 3)


def test_next_juridical_day_from_sunday():
    """Sunday → Monday."""
    assert next_juridical_day(date(2025, 3, 2)) == date(2025, 3, 3)


def test_prev_juridical_day_already_juridical():
    """A day that is already juridical returns itself."""
    assert prev_juridical_day(date(2025, 3, 3)) == date(2025, 3, 3)  # Monday


def test_prev_juridical_day_from_saturday():
    """Saturday → Friday."""
    assert prev_juridical_day(date(2025, 3, 8)) == date(2025, 3, 7)  # Sat → Fri


def test_prev_juridical_day_from_sunday():
    """Sunday → Friday."""
    assert prev_juridical_day(date(2025, 3, 9)) == date(2025, 3, 7)  # Sun → Fri


# ── compute_deadline — forward ────────────────────────────────────────────


def test_basic_forward_deadline():
    """15 days after a date that lands on a weekday stays unchanged."""
    # March 3 (Mon) + 10 = March 13 (Thu) — no adjustment
    result = compute_deadline(date(2025, 3, 3), 10, "after")
    assert result == date(2025, 3, 13)
    assert is_juridical_day(result)


def test_forward_lands_on_saturday():
    """Deadline landing on Saturday moves to Monday."""
    # March 3 (Mon) + 5 = March 8 (Sat) → March 10 (Mon)
    result = compute_deadline(date(2025, 3, 3), 5, "after")
    assert result == date(2025, 3, 10)


def test_forward_lands_on_sunday():
    """Deadline landing on Sunday moves to Monday."""
    # March 3 (Mon) + 6 = March 9 (Sun) → March 10 (Mon)
    result = compute_deadline(date(2025, 3, 3), 6, "after")
    assert result == date(2025, 3, 10)


def test_forward_lands_on_holiday():
    """Deadline landing on Fête nationale (June 24) moves to next juridical day."""
    # June 10 (Tue) + 14 = June 24 (Tue, holiday) → June 25 (Wed)
    result = compute_deadline(date(2025, 6, 10), 14, "after")
    assert result == date(2025, 6, 25)


def test_forward_lands_on_holiday_before_weekend():
    """Deadline landing on Good Friday (Friday holiday) moves past Easter weekend to Tuesday."""
    # April 3 (Thu) + 15 = April 18 (Fri, Good Friday)
    # → skip Good Friday, Sat, Easter Sunday, Easter Monday → April 22 (Tue)
    result = compute_deadline(date(2025, 4, 3), 15, "after")
    assert result == date(2025, 4, 22)


def test_zero_delay_on_holiday():
    """0-day delay on a holiday returns next juridical day."""
    # Jan 1 (holiday) → Jan 2 (Thursday, juridical)
    result = compute_deadline(date(2025, 1, 1), 0, "after")
    assert result == date(2025, 1, 2)


def test_zero_delay_on_weekday():
    """0-day delay on a weekday returns the same day."""
    result = compute_deadline(date(2025, 3, 3), 0, "after")
    assert result == date(2025, 3, 3)


# ── compute_deadline — backward ───────────────────────────────────────────


def test_backward_lands_on_saturday():
    """Backward deadline landing on Saturday moves to Friday."""
    # March 17 (Mon) - 9 = March 8 (Sat) → March 7 (Fri)
    result = compute_deadline(date(2025, 3, 17), 9, "before")
    assert result == date(2025, 3, 7)


def test_backward_lands_on_sunday():
    """Backward deadline landing on Sunday moves to Friday."""
    # March 17 (Mon) - 8 = March 9 (Sun) → March 7 (Fri)
    result = compute_deadline(date(2025, 3, 17), 8, "before")
    assert result == date(2025, 3, 7)


def test_backward_lands_on_holiday():
    """Backward deadline landing on a holiday moves to previous juridical day."""
    # A deadline landing on Easter Monday → prev_juridical_day = Good Friday - 1 = Thursday
    # Easter Monday 2025 = April 21 (Mon)
    # April 21 - 1 days back from April 22: compute April 22 - 1 = April 21
    # Use: April 22 (Tue) - 1 = April 21 (Easter Mon) → prev = April 17 (Thu)
    result = compute_deadline(date(2025, 4, 22), 1, "before")
    assert result == date(2025, 4, 17)


def test_zero_delay_backward():
    """0-day backward delay on a juridical day returns the same day."""
    result = compute_deadline(date(2025, 3, 3), 0, "before")
    assert result == date(2025, 3, 3)


# ── Holiday cluster ───────────────────────────────────────────────────────


def test_holiday_cluster():
    """Good Friday + Easter weekend + Easter Monday creates a 4-day non-juridical window."""
    # Easter 2025: Good Friday Apr 18 (Fri), Sat Apr 19, Sun Apr 20, Easter Mon Apr 21
    # All four days are non-juridical
    assert not is_juridical_day(date(2025, 4, 18))  # Good Friday
    assert not is_juridical_day(date(2025, 4, 19))  # Saturday
    assert not is_juridical_day(date(2025, 4, 20))  # Easter Sunday
    assert not is_juridical_day(date(2025, 4, 21))  # Easter Monday

    # April 22 is the first juridical day after the cluster
    assert is_juridical_day(date(2025, 4, 22))

    # Deadline landing anywhere in the cluster moves to April 22
    assert compute_deadline(date(2025, 4, 14), 4, "after") == date(2025, 4, 22)
    assert compute_deadline(date(2025, 4, 17), 4, "after") == date(2025, 4, 22)
