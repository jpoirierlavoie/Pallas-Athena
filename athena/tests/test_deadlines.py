"""Unit tests for utils/deadlines.py — Quebec judicial deadline computation."""

import sys
import os

# Ensure athena/ is on the path when running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta, timezone

import pytest

from utils.deadlines import (
    CIVIL,
    PROCEDURAL,
    add_jours_ouvrables,
    compute_deadline,
    days_until,
    effective_due,
    is_art_82_day,
    is_juridical_day,
    is_past_due,
    last_action_day,
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
    """Dec 25, 2022 = Sunday. Dec 26 (Monday) is a holiday in civil
    procedure — but because art. 82 C.p.c. makes it one EVERY year, not as
    an « observed » Christmas: art. 61(23) h) L.i. names no substitute, so
    for the civil calendar (prescription) Monday Dec 26, 2022 is an
    ordinary jour ouvrable. (Rewritten 2026-09-30: the old test pinned a
    Sunday→Monday observance no text provides.)"""
    assert date(2022, 12, 25).weekday() == 6  # confirm Sunday
    assert date(2022, 12, 25) in get_quebec_holidays(2022)
    assert date(2022, 12, 26) in get_quebec_holidays(2022, regime=PROCEDURAL)
    assert date(2022, 12, 26) not in get_quebec_holidays(2022, regime=CIVIL)
    assert is_juridical_day(date(2022, 12, 26), regime=CIVIL) is True


def test_new_years_on_sunday():
    """Jan 1, 2023 = Sunday. Same reasoning as Christmas: Jan 2 is a
    procedural holiday every year (art. 82 C.p.c.), and no substitute day
    exists for 1 January in art. 61(23) b) L.i. — civil Jan 2, 2023 is a
    jour ouvrable."""
    assert date(2023, 1, 1).weekday() == 6  # confirm Sunday
    assert date(2023, 1, 1) in get_quebec_holidays(2023)
    assert date(2023, 1, 2) in get_quebec_holidays(2023, regime=PROCEDURAL)
    assert date(2023, 1, 2) not in get_quebec_holidays(2023, regime=CIVIL)
    assert is_juridical_day(date(2023, 1, 2), regime=CIVIL) is True


def test_fete_nationale_on_sunday_has_no_substitute():
    """June 24, 2018 = Sunday. THE RULE REVERSED (2026-09-30): the old test
    pinned June 25 as « observed ». Art. 61(23) e) L.i. lists « le 24 juin,
    jour de la fête nationale » with NO substitute — unlike f), which gives
    1 July its « ou le 2 juillet si le 1er tombe un dimanche ». So Monday
    June 25 is a juridical day on BOTH calendars."""
    assert date(2018, 6, 24).weekday() == 6  # confirm Sunday
    for regime in (PROCEDURAL, CIVIL):
        holidays = get_quebec_holidays(2018, regime=regime)
        assert date(2018, 6, 24) in holidays
        assert date(2018, 6, 25) not in holidays
        assert is_juridical_day(date(2018, 6, 25), regime=regime) is True


def test_canada_day_on_sunday():
    """When July 1 falls on Sunday, July 2 (Monday) is the holiday — art.
    61(23) f) L.i., the ONE substitute day the article provides. Both
    calendars."""
    # July 1, 2018 = Sunday
    assert date(2018, 7, 1).weekday() == 6  # confirm Sunday
    for regime in (PROCEDURAL, CIVIL):
        holidays = get_quebec_holidays(2018, regime=regime)
        assert date(2018, 7, 1) in holidays
        assert date(2018, 7, 2) in holidays
        assert is_juridical_day(date(2018, 7, 2), regime=regime) is False


def test_all_holidays_2025_count():
    """2025: the nine dated jours fériés of art. 61(23) L.i. on the civil
    calendar; eleven in procedure, art. 82 C.p.c. adding Thu Jan 2 and Fri
    Dec 26. (Was « exactly 9 » for the single old calendar.)"""
    assert len(get_quebec_holidays(2025, regime=CIVIL)) == 9
    procedural = get_quebec_holidays(2025, regime=PROCEDURAL)
    assert len(procedural) == 11
    assert set(procedural) - set(get_quebec_holidays(2025, regime=CIVIL)) == {
        date(2025, 1, 2), date(2025, 12, 26),
    }
    # The default is the procedural calendar.
    assert get_quebec_holidays(2025) == procedural


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
    """0-day delay on a holiday returns next juridical day.

    Jan 1, 2025 (holiday) → Thu Jan 2 is ALSO a holiday in civil procedure
    (art. 82 C.p.c.) → Fri Jan 3. The old expectation (Jan 2) was the
    defect: art. 82 applies every year, not only when Jan 1 is a Sunday."""
    result = compute_deadline(date(2025, 1, 1), 0, "after")
    assert result == date(2025, 1, 3)


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


def test_lateness_prorogues_to_the_next_juridical_day():
    """THE RULE REVERSED (lawyer's decision, 2026-08-02): a deadline landing
    on a non-juridical day prorogues before lateness is evaluated. Due
    Saturday → actionable Monday → past due Tuesday. The earlier pin (« art.
    83 governs computation, not lateness ») died with that decision, in the
    same commit as the code that reverses it."""
    saturday = date(2026, 7, 18)
    assert not is_juridical_day(saturday)            # guard
    assert effective_due(saturday) == date(2026, 7, 20)   # → Monday
    assert is_past_due(saturday, today=date(2026, 7, 17)) is False
    assert is_past_due(saturday, today=date(2026, 7, 19)) is False  # Sunday
    assert is_past_due(saturday, today=date(2026, 7, 20)) is False  # actionable
    assert is_past_due(saturday, today=date(2026, 7, 21)) is True   # Tuesday
    # A Québec statutory holiday (Fête du Canada 2026 = Wed Jul 1): due on
    # the holiday → actionable Thursday → past due Friday.
    holiday = date(2026, 7, 1)
    assert not is_juridical_day(holiday)             # guard
    assert effective_due(holiday) == date(2026, 7, 2)
    assert is_past_due(holiday, today=date(2026, 7, 2)) is False
    assert is_past_due(holiday, today=date(2026, 7, 3)) is True
    # A JURIDICAL deadline is untouched by prorogation: due Friday, past due
    # Saturday — it WAS actionable Friday.
    friday = date(2026, 7, 17)
    assert effective_due(friday) == friday
    assert is_past_due(friday, today=date(2026, 7, 18)) is True


def test_days_until_and_is_past_due_can_never_disagree():
    """The countdown reaches zero on the last ACTIONABLE day and goes
    negative only once truly late — never « -1 » on something not yet late
    (the dashboard's old evening artifact)."""
    saturday = date(2026, 7, 18)
    for today, expected_days, expected_late in (
        (date(2026, 7, 17), 3, False),   # Friday: 3 days to the Monday
        (date(2026, 7, 19), 1, False),   # Sunday
        (date(2026, 7, 20), 0, False),   # the actionable Monday itself
        (date(2026, 7, 21), -1, True),   # Tuesday: late, and only now < 0
    ):
        assert days_until(saturday, today=today) == expected_days, today
        assert is_past_due(saturday, today=today) is expected_late, today
        assert (days_until(saturday, today=today) < 0) == is_past_due(
            saturday, today=today
        )


def test_days_until_is_signed():
    """Negative once past — callers floor it for display, so the distinction
    between « due today » and « three days late » survives here."""
    today = date(2026, 7, 31)
    assert days_until(date(2026, 8, 5), today=today) == 5
    assert days_until(date(2026, 7, 31), today=today) == 0
    assert days_until(date(2026, 7, 28), today=today) == -3


def _freeze_utc(monkeypatch, iso: str) -> None:
    """Pin datetime.now(timezone.utc) inside utils.deadlines."""
    from utils import deadlines as dl

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
    # These three rows CROSS 26 Dec 2026 and 2 Jan 2027 — art. 82 C.p.c.
    # days (2026-09-30) — but both are Saturdays that year, so every
    # expected value is unchanged. The weekday cases live in
    # ART_82_PROCEDURAL_GOLDEN below.
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


# ── Art. 82 C.p.c. and the two calendars (2026-09-30) ────────────────────
#
# Art. 82 C.p.c.: « Les tribunaux ne siègent pas les samedis et les jours
# fériés au sens de l'article 61 de la Loi d'interprétation (chapitre I-16),
# non plus que les 26 décembre et 2 janvier qui sont, en matière de
# procédure civile, considérés jours fériés. » EVERY year, and only « en
# matière de procédure civile ». Art. 2879 al. 2 C.c.Q. (prescription):
# « Lorsque le dernier jour est un samedi ou un jour férié, la prescription
# n'est acquise qu'au premier jour ouvrable qui suit » — jour férié in the
# sense of art. 61(23) L.i., which names neither 26 December nor 2 January.


def test_art_82_days_are_procedural_holidays_every_year_on_every_weekday():
    """26 Dec and 2 Jan are non-juridical in procedure in EVERY year — the
    range is chosen so each date lands on all seven weekdays at least once,
    which is exactly what the old « only when the holiday is a Sunday »
    rule got wrong four years out of seven (a Tuesday-to-Friday date). On the civil calendar they are
    ordinary days: juridical exactly when they are a weekday."""
    seen = {(12, 26): set(), (1, 2): set()}
    for year in range(2020, 2034):
        for month, day in seen:
            d = date(year, month, day)
            seen[(month, day)].add(d.weekday())
            assert is_art_82_day(d)
            assert is_juridical_day(d, regime=PROCEDURAL) is False, d
            assert is_juridical_day(d) is False, d   # the default
            assert d in get_quebec_holidays(year, regime=PROCEDURAL), d
            assert d not in get_quebec_holidays(year, regime=CIVIL), d
            assert is_juridical_day(d, regime=CIVIL) is (d.weekday() < 5), d
    assert seen[(12, 26)] == set(range(7))
    assert seen[(1, 2)] == set(range(7))


def test_is_art_82_day_is_exactly_the_two_dates():
    assert is_art_82_day(date(2025, 12, 26))
    assert is_art_82_day(date(2026, 1, 2))
    assert not is_art_82_day(date(2025, 12, 25))
    assert not is_art_82_day(date(2025, 12, 27))
    assert not is_art_82_day(date(2026, 1, 1))
    assert not is_art_82_day(date(2026, 1, 3))


def test_june_25_is_juridical_when_june_24_is_a_sunday():
    """2029: June 24 is a Sunday again. Monday June 25 is a working court
    day in procedure AND a jour ouvrable for prescription — art. 61(23) e)
    L.i. gives the Fête nationale no substitute."""
    assert date(2029, 6, 24).weekday() == 6  # guard
    for regime in (PROCEDURAL, CIVIL):
        assert is_juridical_day(date(2029, 6, 25), regime=regime) is True
        assert next_juridical_day(date(2029, 6, 24), regime=regime) == date(
            2029, 6, 25
        )


def test_both_calendars_agree_everywhere_but_the_art_82_days():
    """The two calendars differ ONLY on 26 Dec and 2 Jan — every other day
    of a decade is judged identically (weekends, L.i. holidays, the July 2
    substitute). A second divergence would be a bug in one of them."""
    d = date(2020, 1, 1)
    while d < date(2031, 1, 1):
        if not is_art_82_day(d):
            assert is_juridical_day(d, regime=PROCEDURAL) == is_juridical_day(
                d, regime=CIVIL
            ), d
        d += timedelta(days=1)


def test_unknown_regime_is_refused_loudly():
    """A typo must never fall back silently to one of the two calendars."""
    with pytest.raises(ValueError):
        is_juridical_day(date(2025, 12, 26), regime="prescription")
    with pytest.raises(ValueError):
        is_juridical_day(date(2025, 12, 27), regime="procedure")  # a Saturday
    with pytest.raises(ValueError):
        get_quebec_holidays(2025, regime="")
    # Also on the paths that return early without consulting a calendar:
    # a typo must fail on every call, not only on those that count a day.
    with pytest.raises(ValueError):
        add_jours_ouvrables(date(2025, 12, 23), 0, regime="civile")
    with pytest.raises(ValueError):
        effective_due(None, regime="civile")
    with pytest.raises(ValueError):
        is_past_due(None, today=date(2025, 12, 23), regime="civile")
    with pytest.raises(ValueError):
        days_until(None, today=date(2025, 12, 23), regime="civile")


# Hand-computed from the texts, weekday by weekday (art. 83 C.p.c.: forward
# lands move forward, backward lands move backward; art. 82 C.p.c. for the
# year-end days). Kept APART from the frozen table above, whose values
# stay unchanged: it crosses 26 Dec 2026 / 2 Jan 2027 only on Saturdays.
#   2025-12: Wed 24 · Thu 25 (Noël) · Fri 26 (art. 82) · Sat 27 · Sun 28 · Mon 29
#   2026-01: Wed Dec 31 · Thu 1 (Jour de l'An) · Fri 2 (art. 82) · Sat 3 · Sun 4 · Mon 5
#   2029-01: Mon 1 · Tue 2 (art. 82) · Wed 3
#   2029-12: Mon 24 · Tue 25 (Noël) · Wed 26 (art. 82) · Thu 27
#   2029-06: Sun 24 (Fête nationale) · Mon 25 (juridical — no substitute)
#   2018-06: Sun 24 · Mon 25 (juridical)
#   2023-01: Sun 1 · Mon 2 (art. 82) · Tue 3
ART_82_PROCEDURAL_GOLDEN = [
    ((2025, 12, 24), 2, "after", (2025, 12, 29)),    # Fri 26 → Mon 29
    ((2025, 12, 11), 15, "after", (2025, 12, 29)),   # Fri 26 → Mon 29
    ((2025, 12, 29), 3, "before", (2025, 12, 24)),   # Fri 26 → back over Noël
    ((2025, 12, 31), 2, "after", (2026, 1, 5)),      # Fri Jan 2 → Mon 5
    ((2026, 1, 5), 3, "before", (2025, 12, 31)),     # Fri Jan 2 → back over Jan 1
    ((2028, 12, 19), 14, "after", (2029, 1, 3)),     # Tue Jan 2 → Wed 3
    ((2029, 12, 16), 10, "after", (2029, 12, 27)),   # Wed Dec 26 → Thu 27
    ((2029, 12, 28), 2, "before", (2029, 12, 24)),   # Wed 26, Tue 25 → Mon 24
    ((2029, 6, 22), 3, "after", (2029, 6, 25)),      # Mon Jun 25 juridical
    ((2029, 6, 20), 4, "after", (2029, 6, 25)),      # Sun 24 → Mon 25
    ((2018, 6, 15), 10, "after", (2018, 6, 25)),     # Mon Jun 25 juridical
    ((2022, 12, 30), 3, "after", (2023, 1, 3)),      # Mon Jan 2 (art. 82) → Tue 3
]


def test_compute_deadline_art_82_reference_table():
    for start, delay, direction, expected in ART_82_PROCEDURAL_GOLDEN:
        got = compute_deadline(date(*start), delay, direction)
        assert got == date(*expected), (
            f"compute_deadline({start}, {delay}, {direction!r}) "
            f"= {got}, expected {date(*expected)}"
        )


def test_civil_calendar_reports_prescription_to_the_next_jour_ouvrable():
    """Art. 2879 al. 2 C.c.Q.: only a Saturday or a jour férié (art. 61(23)
    L.i.) moves the last day. 26 Dec / 2 Jan are jours ouvrables for it."""
    # Fri Dec 26, 2025: a jour ouvrable for prescription → itself.
    assert next_juridical_day(date(2025, 12, 26), regime=CIVIL) == date(2025, 12, 26)
    # Thu Dec 25 → Fri Dec 26 (civil), Mon Dec 29 (procedure).
    assert next_juridical_day(date(2025, 12, 25), regime=CIVIL) == date(2025, 12, 26)
    assert next_juridical_day(date(2025, 12, 25), regime=PROCEDURAL) == date(2025, 12, 29)
    # Thu Jan 1, 2026 → Fri Jan 2 (civil), Mon Jan 5 (procedure).
    assert next_juridical_day(date(2026, 1, 1), regime=CIVIL) == date(2026, 1, 2)
    assert next_juridical_day(date(2026, 1, 1), regime=PROCEDURAL) == date(2026, 1, 5)
    # Mon Dec 26, 2033 after a Sunday Christmas, Mon Jan 2, 2034 after a
    # Sunday New Year: no substitute day → ouvrables (the old code moved both
    # to the Tuesday, one day LATER than the law).
    assert date(2033, 12, 25).weekday() == 6 and date(2034, 1, 1).weekday() == 6
    assert is_juridical_day(date(2033, 12, 26), regime=CIVIL) is True
    assert is_juridical_day(date(2034, 1, 2), regime=CIVIL) is True


def test_business_days_follow_the_calendar_they_are_given():
    """Tue Dec 23, 2025 + 3 jours ouvrables. Civil (the presse avis):
    Wed 24 (1), [Thu 25 Noël], Fri 26 (2), [Sat, Sun], Mon 29 (3).
    Procedural: Wed 24 (1), [Thu 25, Fri 26 art. 82, Sat, Sun], Mon 29 (2),
    Tue 30 (3)."""
    assert add_jours_ouvrables(date(2025, 12, 23), 3, regime=CIVIL) == date(2025, 12, 29)
    assert add_jours_ouvrables(date(2025, 12, 23), 3, regime=PROCEDURAL) == date(2025, 12, 30)


def test_lateness_follows_the_calendar_of_the_deadline():
    """A task due Fri Dec 26, 2025 (procedural) is actionable until Mon 29
    and late Tue 30. A PRESCRIPTION ending Fri Dec 26 is acquired at the end
    of that day (art. 2879 — a jour ouvrable): échue on Sat 27 already."""
    due = date(2025, 12, 26)
    assert effective_due(due) == date(2025, 12, 29)
    assert is_past_due(due, today=date(2025, 12, 29)) is False
    assert is_past_due(due, today=date(2025, 12, 30)) is True
    assert days_until(due, today=date(2025, 12, 24)) == 5

    assert effective_due(due, regime=CIVIL) == due
    assert is_past_due(due, today=date(2025, 12, 26), regime=CIVIL) is False
    assert is_past_due(due, today=date(2025, 12, 27), regime=CIVIL) is True
    assert days_until(due, today=date(2025, 12, 24), regime=CIVIL) == 2


def test_last_action_day_is_the_last_day_the_greffe_can_receive_a_filing():
    """last_action_day is PROCEDURAL on purpose: a prescription running out
    on Fri Dec 26, 2025 (a jour ouvrable under art. 2879) cannot be
    interrupted by a dépôt that day — the courts do not sit (art. 82 C.p.c.)
    — so the last day to FILE is Wed Dec 24, and it differs."""
    assert last_action_day(date(2025, 12, 26)) == (date(2025, 12, 24), True)
    assert last_action_day(date(2026, 1, 2)) == (date(2025, 12, 31), True)
    # A plain juridical day stays itself (inclusive).
    assert last_action_day(date(2026, 6, 17)) == (date(2026, 6, 17), False)
    # Mon June 25, 2029 is juridical — no pull-back any more.
    assert last_action_day(date(2029, 6, 25)) == (date(2029, 6, 25), False)


class _FrozenDatetime(datetime):
    """datetime whose now() is pinned — freezes today_mtl without touching
    the injectable ``today`` path (this test exercises the DEFAULT)."""

    _instant = None

    @classmethod
    def now(cls, tz=None):
        return cls._instant.astimezone(tz) if tz else cls._instant


def test_bande_du_soir_une_tache_due_demain_n_est_pas_en_retard(monkeypatch):
    """Le bogue du 2026-08-02 : dimanche 21 h 24 à Montréal = lundi 01 h 24
    UTC. Une tâche due lundi (minuit UTC) satisfaisait « due < now » sur le
    web toute la soirée. Sous today_mtl + le prédicat, elle n'est ni en
    retard ni « -1j » — l'horloge est GELÉE au moment exact du diagnostic."""
    from utils import deadlines as dl

    _FrozenDatetime._instant = datetime(
        2026, 8, 3, 1, 24, tzinfo=timezone.utc
    )
    monkeypatch.setattr(dl, "datetime", _FrozenDatetime)

    assert dl.today_mtl() == date(2026, 8, 2)  # encore dimanche à Montréal
    # Une instance du datetime PATCHÉ, sinon l'isinstance de _as_date ne la
    # reconnaît pas (une vraie datetime n'est pas une instance de la
    # sous-classe qui remplace le nom du module).
    due_monday = _FrozenDatetime(2026, 8, 3, tzinfo=timezone.utc)
    assert dl.is_past_due(due_monday) is False
    assert dl.days_until(due_monday) == 1  # « Demain », jamais « -1j »


# ── Calendar sweep: every PRESCRIPTION site names CIVIL (revue 2026-09-30) ──
#
# Every helper defaults to PROCEDURAL, so a prescription-side call that
# forgets ``regime=`` silently prorogues past a weekday 26 Dec / 2 Jan — a
# LATER prescription date, the unsafe direction. Prose cannot guard that; a
# sweep over the source can. ``last_action_day`` is deliberately absent from
# the helper list: it is procedural BY DESIGN (the last day to FILE).

import ast
import pathlib

_ATHENA = pathlib.Path(__file__).resolve().parent.parent

_CALENDAR_HELPERS = frozenset({
    "is_juridical_day", "next_juridical_day", "prev_juridical_day",
    "add_jours_ouvrables", "effective_due", "is_past_due", "days_until",
    "get_quebec_holidays",
})

# (file, function) — None = the whole module.
_PRESCRIPTION_SITES = (
    ("utils/recours.py", None),
    ("models/dossier.py", "derive_prescription"),
    ("routes/dossiers.py", "_attach_prescription_warnings"),
    ("routes/dashboard.py", "_get_prescription_alerts"),
    ("mcp/handlers.py", "_prescription_row"),
)


def _calendar_calls(node: ast.AST) -> list[tuple[str, ast.Call]]:
    calls = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None))
            if name in _CALENDAR_HELPERS:
                calls.append((name, sub))
    return calls


def _regime_name(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "regime":
            value = kw.value
            return (value.attr if isinstance(value, ast.Attribute)
                    else getattr(value, "id", None))
    return None


@pytest.mark.parametrize("path, function", _PRESCRIPTION_SITES)
def test_every_prescription_site_passes_the_civil_calendar(path, function):
    tree = ast.parse((_ATHENA / path).read_text(encoding="utf-8"))
    if function is not None:
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == function]
        assert len(found) == 1, f"{path}: {function} not found — renamed?"
        tree = found[0]
    calls = _calendar_calls(tree)
    # Not vacuous: each site really does consult a calendar.
    assert calls, f"{path}:{function}: no calendar helper call left to check"
    wrong = [(name, call.lineno, _regime_name(call))
             for name, call in calls if _regime_name(call) != "CIVIL"]
    assert not wrong, (
        f"{path}:{function} calls a calendar helper without regime=CIVIL "
        f"(a prescription date — art. 2879 C.c.Q.): {wrong}"
    )
