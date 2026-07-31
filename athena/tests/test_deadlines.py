"""Unit tests for utils/deadlines.py — Quebec judicial deadline computation."""

import sys
import os

# Ensure athena/ is on the path when running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone
from utils.deadlines import (
    add_jours_ouvrables,
    compute_deadline,
    days_until,
    is_juridical_day,
    is_past_due,
    next_juridical_day,
    prev_juridical_day,
    get_quebec_holidays,
    today_mtl,
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


# ── add_jours_ouvrables (business days — avis de la Loi sur la presse) ────


def test_add_jours_ouvrables_plain_week():
    # Mon 2026-07-13 + 2 business days = Wed 2026-07-15 (no weekend, no holiday)
    assert add_jours_ouvrables(date(2026, 7, 13), 2) == date(2026, 7, 15)


def test_add_jours_ouvrables_skips_weekend():
    # Thu 2026-07-16 + 3 → Fri 17, [Sat/Sun], Mon 20, Tue 21
    assert add_jours_ouvrables(date(2026, 7, 16), 3) == date(2026, 7, 21)


def test_add_jours_ouvrables_golden_thursday_with_holiday_monday():
    """§ 8 (12) cas d'or: a statutory-holiday Monday inside the window.
    Journée nationale des patriotes 2026 = Mon May 18 (the Monday preceding
    May 25; 2026-05-24 is a Sunday). Thu 2026-05-14 + 3 jours ouvrables →
    Fri 15, [Sat 16 / Sun 17 / Mon 18 férié], Tue 19, Wed 20."""
    assert not is_juridical_day(date(2026, 5, 18))   # guard: the holiday holds
    assert add_jours_ouvrables(date(2026, 5, 14), 3) == date(2026, 5, 20)


def test_add_jours_ouvrables_zero_is_identity():
    assert add_jours_ouvrables(date(2026, 7, 13), 0) == date(2026, 7, 13)
    # Even from a non-juridical start (a Saturday): 0 adds nothing.
    assert add_jours_ouvrables(date(2026, 7, 18), 0) == date(2026, 7, 18)


# ── Lateness: today_mtl / is_past_due / days_until (lot 6) ───────────────
#
# These answer « is this deadline in the past? ». Art. 83's juridical-day
# machinery above answers a DIFFERENT question (how a deadline is computed)
# and must never leak into these — a deadline falling on a Sunday is not
# "late" on the Friday before.


def test_is_past_due_yesterday_today_tomorrow():
    today = date(2026, 7, 31)
    assert is_past_due(date(2026, 7, 30), today=today) is True
    # The rule the whole application states: due TODAY is not late yet.
    assert is_past_due(date(2026, 7, 31), today=today) is False
    assert is_past_due(date(2026, 8, 1), today=today) is False


def test_is_past_due_undated_is_never_late():
    """An undated task cannot be overdue — never coerce None to a date."""
    assert is_past_due(None, today=date(2026, 7, 31)) is False
    assert days_until(None, today=date(2026, 7, 31)) is None


def test_is_past_due_accepts_a_midnight_utc_datetime():
    """Date-only fields are stored at midnight UTC; their UTC calendar date
    IS the intended day. Converting them to Montréal would shift them back
    one day — the trap mcp.tools.date_str exists to avoid."""
    stored = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    assert is_past_due(stored, today=date(2026, 7, 31)) is True
    assert is_past_due(stored, today=date(2026, 7, 30)) is False
    # Naive datetimes (legacy docs) are read as UTC, not as local time.
    assert is_past_due(datetime(2026, 7, 30, 0, 0), today=date(2026, 7, 31)) is True


def test_lateness_ignores_juridical_days_entirely():
    """A deadline on a Saturday is late on the following Monday, and NOT
    late on the Friday before. Art. 83 governs computation, not lateness."""
    saturday = date(2026, 7, 18)
    assert not is_juridical_day(saturday)            # guard
    assert is_past_due(saturday, today=date(2026, 7, 17)) is False
    assert is_past_due(saturday, today=date(2026, 7, 20)) is True
    # Same for a Québec statutory holiday (Fête du Canada 2026 = Wed Jul 1).
    holiday = date(2026, 7, 1)
    assert not is_juridical_day(holiday)             # guard
    assert is_past_due(holiday, today=date(2026, 7, 1)) is False
    assert is_past_due(holiday, today=date(2026, 7, 2)) is True


def test_days_until_is_signed():
    """Negative once past — callers floor it for display, so the distinction
    between « due today » and « three days late » survives here."""
    today = date(2026, 7, 31)
    assert days_until(date(2026, 8, 5), today=today) == 5
    assert days_until(date(2026, 7, 31), today=today) == 0
    assert days_until(date(2026, 7, 28), today=today) == -3


def _freeze_utc(monkeypatch, iso: str) -> None:
    """Pin datetime.now(timezone.utc) inside utils.deadlines."""
    import utils.deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


def test_today_mtl_crosses_midnight_on_montreal_time_edt(monkeypatch):
    """EDT is UTC-4: the Montréal day turns over at 04:00 UTC. This is the
    exact 4-hour band in which a UTC-based « today » ran a day ahead."""
    _freeze_utc(monkeypatch, "2026-07-31T03:59:00+00:00")
    assert today_mtl() == date(2026, 7, 30)          # still the 30th here
    _freeze_utc(monkeypatch, "2026-07-31T04:01:00+00:00")
    assert today_mtl() == date(2026, 7, 31)


def test_today_mtl_crosses_midnight_on_montreal_time_est(monkeypatch):
    """EST is UTC-5: after the November fallback the band is FIVE hours,
    not four. A hard-coded offset would be wrong half the year."""
    _freeze_utc(monkeypatch, "2026-11-15T04:59:00+00:00")
    assert today_mtl() == date(2026, 11, 14)
    _freeze_utc(monkeypatch, "2026-11-15T05:01:00+00:00")
    assert today_mtl() == date(2026, 11, 15)


def test_today_mtl_across_the_spring_forward(monkeypatch):
    """2026-03-08 02:00 local: clocks jump to 03:00. The evening before the
    change runs on EST (-5), the day after on EDT (-4)."""
    _freeze_utc(monkeypatch, "2026-03-08T04:30:00+00:00")   # 23:30 EST Mar 7
    assert today_mtl() == date(2026, 3, 7)
    _freeze_utc(monkeypatch, "2026-03-09T04:30:00+00:00")   # 00:30 EDT Mar 9
    assert today_mtl() == date(2026, 3, 9)


def test_today_mtl_across_the_fall_back(monkeypatch):
    """2026-11-01 02:00 local: clocks repeat 01:00-02:00. Either pass of the
    ambiguous hour still lands on November 1st."""
    _freeze_utc(monkeypatch, "2026-11-01T03:30:00+00:00")   # 23:30 EDT Oct 31
    assert today_mtl() == date(2026, 10, 31)
    _freeze_utc(monkeypatch, "2026-11-01T05:30:00+00:00")
    assert today_mtl() == date(2026, 11, 1)


def test_is_past_due_defaults_to_today_mtl(monkeypatch):
    """Without an injected today, the predicate reads the Montréal clock —
    not UTC. Pinned because the default is what every caller uses."""
    _freeze_utc(monkeypatch, "2026-07-31T03:00:00+00:00")   # 23:00 EDT Jul 30
    assert is_past_due(date(2026, 7, 30)) is False          # still the 30th
    assert days_until(date(2026, 7, 30)) == 0


# ── Frozen reference table: art. 83 must survive every lot ───────────────


COMPUTE_DEADLINE_GOLDEN = [
    # (start, delay, direction, expected)
    # Plain weekdays, no adjustment.
    ((2026, 7, 13), 1, "after", (2026, 7, 14)),
    ((2026, 7, 13), 2, "after", (2026, 7, 15)),
    ((2026, 7, 13), 4, "after", (2026, 7, 17)),
    ((2026, 7, 15), 1, "before", (2026, 7, 14)),
    ((2026, 7, 17), 4, "before", (2026, 7, 13)),
    # Forward onto a weekend → next juridical day.
    ((2026, 7, 13), 5, "after", (2026, 7, 20)),      # Sat 18 → Mon 20
    ((2026, 7, 13), 6, "after", (2026, 7, 20)),      # Sun 19 → Mon 20
    ((2026, 7, 17), 1, "after", (2026, 7, 20)),      # Sat 18 → Mon 20
    # Backward onto a weekend → previous juridical day.
    ((2026, 7, 20), 2, "before", (2026, 7, 17)),     # Sat 18 → Fri 17
    ((2026, 7, 20), 1, "before", (2026, 7, 17)),     # Sun 19 → Fri 17
    # Zero delay keeps art. 83's adjustment.
    ((2026, 7, 18), 0, "after", (2026, 7, 20)),      # Sat → Mon
    ((2026, 7, 18), 0, "before", (2026, 7, 17)),     # Sat → Fri
    ((2026, 7, 13), 0, "after", (2026, 7, 13)),
    # Statutory holidays.
    ((2026, 6, 30), 1, "after", (2026, 7, 2)),       # Canada Day Wed Jul 1
    ((2026, 7, 2), 1, "before", (2026, 6, 30)),      # back over Jul 1
    ((2026, 6, 23), 1, "after", (2026, 6, 25)),      # Fête nationale Jun 24
    ((2026, 5, 15), 3, "after", (2026, 5, 19)),      # patriotes Mon May 18
    ((2026, 5, 19), 1, "before", (2026, 5, 15)),     # back over May 18
    ((2026, 9, 4), 1, "after", (2026, 9, 8)),        # Labour Day Mon Sep 7
    ((2026, 10, 9), 1, "after", (2026, 10, 13)),     # Thanksgiving Mon Oct 12
    # Easter cluster 2026: Good Friday Apr 3, Easter Monday Apr 6.
    ((2026, 4, 2), 1, "after", (2026, 4, 7)),
    ((2026, 4, 7), 1, "before", (2026, 4, 2)),
    ((2026, 4, 2), 2, "after", (2026, 4, 7)),
    # Year-end cluster: Christmas Fri Dec 25, New Year Fri Jan 1 2027.
    ((2026, 12, 24), 1, "after", (2026, 12, 28)),
    ((2026, 12, 28), 2, "before", (2026, 12, 24)),
    ((2026, 12, 31), 1, "after", (2027, 1, 4)),
    # Long delays crossing several adjustments.
    ((2026, 1, 15), 30, "after", (2026, 2, 16)),
    ((2026, 3, 1), 15, "after", (2026, 3, 16)),
    # Backward ADJUSTS BACKWARD: Mar 16 − 15 = Sun Mar 1 → Fri Feb 27, not
    # forward to Mon Mar 2. The directional rule is the point of art. 83.
    ((2026, 3, 16), 15, "before", (2026, 2, 27)),
    ((2025, 3, 1), 15, "after", (2025, 3, 17)),      # the docstring example
    ((2025, 3, 14), 10, "before", (2025, 3, 4)),     # the docstring example
    # Leap year.
    ((2024, 2, 28), 1, "after", (2024, 2, 29)),
    ((2024, 2, 28), 2, "after", (2024, 3, 1)),
]


def test_compute_deadline_frozen_reference_table():
    """Art. 83 C.p.c. is out of scope for every lot of this mandate — this
    table proves no change degraded it. A failure here means the judicial
    computation moved, which is a legal defect, not a test to update."""
    for start, delay, direction, expected in COMPUTE_DEADLINE_GOLDEN:
        got = compute_deadline(date(*start), delay, direction)
        assert got == date(*expected), (
            f"compute_deadline({start}, {delay}, {direction!r}) "
            f"= {got}, expected {date(*expected)}"
        )
