"""Unit tests for utils.recurrence (recurring calendar series expansion).

Pure — no Firestore, no Flask. Three families of invariant are pinned here:

* the ANCHOR rule (occurrence k is measured from the start, never from its
  sibling), which is what keeps a monthly series on the 31st;
* the mandatory, bounded end — a series that would exceed the cap is REFUSED,
  never silently truncated;
* the DST property the caller depends on. ``utils.recours.add_period`` had
  never been exercised on a wall-clock composition before this module existed.
"""

from datetime import date, datetime, time

import pytest

from tz import mtl_to_utc, to_mtl
from utils import recurrence as rec
from utils.deadlines import is_juridical_day


# ── Vocabulary ──────────────────────────────────────────────────────────
def test_the_four_frequencies_the_lawyer_asked_for_exist():
    assert set(rec.VALID_FREQUENCIES) == {
        "hebdomadaire",
        "mensuelle",
        "trimestrielle",
        "annuelle",
    }


def test_every_frequency_has_a_label_and_a_dispatchable_period():
    from utils.recours import add_period

    for key in rec.VALID_FREQUENCIES:
        assert rec.FREQUENCY_LABELS[key]
        amount, unit = rec.frequency_period(key)
        assert amount > 0
        # Must not raise — the unit has to be one recours can dispatch.
        add_period(datetime(2026, 1, 15), amount, unit)


def test_labels_carry_the_empty_key_for_the_form_select():
    assert rec.FREQUENCY_LABELS[""] == "Ne se répète pas"


def test_unknown_frequency_has_no_period():
    assert rec.frequency_period("quotidienne") is None
    assert rec.frequency_period("") is None


def test_the_cap_is_sixty_and_is_a_module_constant():
    # Pinned deliberately: the value is derived from four read-window
    # constraints (atomic delete, /audiences 100, mirror 500, journal 200).
    # Raising it is a decision, never a side effect.
    assert rec.MAX_SERIE_OCCURRENCES == 60


# ── The anchor rule ─────────────────────────────────────────────────────
def test_monthly_from_the_31st_anchors_and_does_not_drift():
    # Chaining from the previous occurrence would pin the tail to the 28th
    # for ever. Anchoring restores the 31st whenever the month allows it.
    assert rec.occurrence_dates(date(2026, 1, 31), "mensuelle", count=5) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
    ]


def test_monthly_from_the_31st_hits_29_february_in_a_leap_year():
    assert rec.occurrence_dates(date(2028, 1, 31), "mensuelle", count=2) == [
        date(2028, 1, 31),
        date(2028, 2, 29),
    ]


def test_annual_from_29_february_returns_to_the_29th_at_the_next_leap_year():
    assert rec.occurrence_dates(date(2028, 2, 29), "annuelle", count=5) == [
        date(2028, 2, 29),
        date(2029, 2, 28),
        date(2030, 2, 28),
        date(2031, 2, 28),
        date(2032, 2, 29),
    ]


def test_quarterly_is_three_months_anchored():
    assert rec.occurrence_dates(date(2026, 1, 31), "trimestrielle", count=4) == [
        date(2026, 1, 31),
        date(2026, 4, 30),
        date(2026, 7, 31),
        date(2026, 10, 31),
    ]


def test_weekly_is_seven_days():
    dates = rec.occurrence_dates(date(2026, 9, 15), "hebdomadaire", count=4)
    assert dates == [
        date(2026, 9, 15),
        date(2026, 9, 22),
        date(2026, 9, 29),
        date(2026, 10, 6),
    ]


def test_the_start_date_is_always_the_first_occurrence():
    for freq in rec.VALID_FREQUENCIES:
        assert rec.occurrence_dates(date(2026, 3, 17), freq, count=3)[0] == date(
            2026, 3, 17
        )


# ── Bounds: count / until ───────────────────────────────────────────────
def test_count_yields_exactly_that_many():
    assert len(rec.occurrence_dates(date(2026, 1, 5), "hebdomadaire", count=12)) == 12


def test_until_is_inclusive():
    dates = rec.occurrence_dates(
        date(2026, 9, 15), "hebdomadaire", until=date(2026, 10, 13)
    )
    assert dates[-1] == date(2026, 10, 13)
    assert len(dates) == 5


def test_until_one_day_before_an_occurrence_excludes_it():
    dates = rec.occurrence_dates(
        date(2026, 9, 15), "hebdomadaire", until=date(2026, 10, 12)
    )
    assert dates[-1] == date(2026, 10, 6)


def test_until_equal_to_start_yields_a_single_occurrence():
    assert rec.occurrence_dates(
        date(2026, 9, 15), "mensuelle", until=date(2026, 9, 15)
    ) == [date(2026, 9, 15)]


def test_count_of_one_is_legal():
    assert rec.occurrence_dates(date(2026, 9, 15), "mensuelle", count=1) == [
        date(2026, 9, 15)
    ]


def test_the_cap_is_reachable_exactly():
    dates = rec.occurrence_dates(
        date(2026, 1, 5), "hebdomadaire", count=rec.MAX_SERIE_OCCURRENCES
    )
    assert len(dates) == rec.MAX_SERIE_OCCURRENCES


# ── Validation — an end is mandatory, and overflow REFUSES ──────────────
def test_no_end_is_refused():
    errors = rec.validate_rule("mensuelle")
    assert errors and "date de fin" in errors[0]


def test_both_ends_are_refused():
    errors = rec.validate_rule(
        "mensuelle", count=3, until=date(2027, 1, 1), start=date(2026, 1, 1)
    )
    assert errors and "pas les deux" in errors[0]


def test_unknown_frequency_is_refused():
    assert rec.validate_rule("quotidienne", count=3)


def test_count_over_the_cap_is_refused():
    errors = rec.validate_rule("mensuelle", count=rec.MAX_SERIE_OCCURRENCES + 1)
    assert errors and "60" in errors[0]


def test_count_below_one_is_refused():
    assert rec.validate_rule("mensuelle", count=0)
    assert rec.validate_rule("mensuelle", count=-5)


def test_until_before_start_is_refused():
    assert rec.validate_rule(
        "mensuelle", until=date(2025, 1, 1), start=date(2026, 1, 1)
    )


def test_an_until_that_would_overflow_the_cap_is_REFUSED_not_truncated():
    # A silently shortened series is a calendar the lawyer believes is
    # complete and is not. Three years of weekly is ~157 occurrences.
    errors = rec.validate_rule(
        "hebdomadaire", until=date(2029, 1, 1), start=date(2026, 1, 1)
    )
    assert errors and "dépasserait" in errors[0]

    with pytest.raises(ValueError):
        rec.occurrence_dates(
            date(2026, 1, 1), "hebdomadaire", until=date(2029, 1, 1)
        )


def test_an_until_landing_exactly_on_the_cap_boundary_is_accepted():
    start = date(2026, 1, 5)
    last = rec.occurrence_dates(
        start, "hebdomadaire", count=rec.MAX_SERIE_OCCURRENCES
    )[-1]
    assert rec.validate_rule("hebdomadaire", until=last, start=start) == []
    assert (
        len(rec.occurrence_dates(start, "hebdomadaire", until=last))
        == rec.MAX_SERIE_OCCURRENCES
    )


def test_occurrence_dates_raises_on_an_invalid_rule():
    with pytest.raises(ValueError):
        rec.occurrence_dates(date(2026, 1, 1), "mensuelle")


# ── DST: the property the caller depends on ─────────────────────────────
# utils.recurrence returns DATES; the caller composes each with the event's
# wall-clock time and converts it individually through mtl_to_utc. These two
# tests pin that this composition holds the civil time constant across both
# Montréal transitions. Adding timedeltas to the stored UTC value instead
# would shift every occurrence after the switch by an hour, silently.
def _civil_times(start: date, freq: str, count: int, wall: time) -> set[time]:
    stored = [
        mtl_to_utc(datetime.combine(d, wall))
        for d in rec.occurrence_dates(start, freq, count=count)
    ]
    return {to_mtl(dt).time() for dt in stored}


def test_weekly_series_keeps_its_civil_time_across_the_march_dst_switch():
    # 2026: spring forward on Sunday 8 March.
    assert _civil_times(date(2026, 2, 18), "hebdomadaire", 6, time(9, 0)) == {
        time(9, 0)
    }


def test_weekly_series_keeps_its_civil_time_across_the_november_dst_switch():
    # 2026: fall back on Sunday 1 November.
    assert _civil_times(date(2026, 10, 14), "hebdomadaire", 6, time(9, 0)) == {
        time(9, 0)
    }


def test_monthly_series_keeps_its_civil_time_across_a_full_year():
    assert _civil_times(date(2026, 1, 15), "mensuelle", 12, time(14, 30)) == {
        time(14, 30)
    }


def test_the_stored_utc_offset_really_does_change_across_the_switch():
    # Guards the two tests above from passing vacuously: if the fixture never
    # crossed a transition, holding the civil time constant would prove
    # nothing. EDT is UTC-4, EST is UTC-5.
    dates = rec.occurrence_dates(date(2026, 10, 14), "hebdomadaire", count=6)
    hours = {mtl_to_utc(datetime.combine(d, time(9, 0))).hour for d in dates}
    assert hours == {13, 14}


# ── No prorogation: occurrences land where the pattern puts them ────────
def test_an_occurrence_landing_on_a_weekend_is_not_moved():
    # 2026-09-05 is a Saturday. A standing meeting forfeits nothing by
    # falling on one, and moving it would destroy the anchor.
    dates = rec.occurrence_dates(date(2026, 9, 5), "hebdomadaire", count=3)
    assert dates == [date(2026, 9, 5), date(2026, 9, 12), date(2026, 9, 19)]
    assert all(d.weekday() == 5 for d in dates)
    assert not is_juridical_day(dates[0])


def test_the_module_does_not_import_deadlines():
    # Structural: prorogation must never leak into expansion. utils.deadlines
    # answers a different question (the last day to file or serve).
    import inspect

    source = inspect.getsource(rec)
    body = source.split('"""', 2)[-1]  # skip the module docstring
    assert "utils.deadlines" not in body
    assert "next_juridical_day" not in body


# ── The stored rule record ──────────────────────────────────────────────
def test_build_rule_uses_iso_strings_not_datetimes():
    rule = rec.build_rule("mensuelle", date(2026, 9, 15), count=12)
    assert rule == {"freq": "mensuelle", "start": "2026-09-15", "count": 12}
    assert all(isinstance(v, (str, int)) for v in rule.values())


def test_build_rule_with_until():
    rule = rec.build_rule("hebdomadaire", date(2026, 9, 15), until=date(2027, 5, 3))
    assert rule["until"] == "2027-05-03"
    assert "count" not in rule


def test_describe_count_and_until():
    assert (
        rec.describe(rec.build_rule("mensuelle", date(2026, 9, 15), count=12))
        == "Chaque mois — 12 occurrences"
    )
    assert (
        rec.describe(rec.build_rule("mensuelle", date(2026, 9, 15), count=1))
        == "Chaque mois — 1 occurrence"
    )
    assert rec.describe(
        rec.build_rule("hebdomadaire", date(2026, 9, 15), until=date(2027, 5, 3))
    ) == "Chaque semaine — jusqu'au 3 mai 2027"


def test_describe_tolerates_a_missing_or_corrupt_rule():
    assert rec.describe(None) == ""
    assert rec.describe({}) == ""
    assert rec.describe({"freq": "inconnue", "count": 3}) == ""
    # A corrupt until must not raise — the banner degrades to the label.
    assert rec.describe({"freq": "mensuelle", "until": "pas-une-date"}) == "Chaque mois"
