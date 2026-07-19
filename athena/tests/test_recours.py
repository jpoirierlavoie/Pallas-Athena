"""Unit tests for utils.recours (prescription deadline + value class)."""

from datetime import datetime, timezone

import pytest

from utils.deadlines import is_juridical_day
from utils.recours import (
    PRESCRIPTION_LABELS,
    PRESCRIPTION_PERIODS,
    VALID_PRESCRIPTION_TYPES,
    _add_months,
    _add_period,
    _add_years,
    compute_class,
    compute_date_pour_agir,
    prescription_period,
)


def _d(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _cents(dollars: float) -> int:
    return int(round(dollars * 100))


# ── compute_class ────────────────────────────────────────────────────────
def test_class_none_value_is_none():
    assert compute_class(None) is None


def test_class_one_cent_is_class_I():
    assert compute_class(_cents(0.01)) == "I"


def test_class_I_upper_bound_inclusive():
    # 15 000,00 $ is the top of Classe I
    assert compute_class(_cents(15_000)) == "I"


def test_class_II_starts_one_cent_over():
    assert compute_class(_cents(15_000.01)) == "II"


def test_class_II_upper_bound_inclusive():
    assert compute_class(_cents(85_000)) == "II"


def test_class_III_starts_one_cent_over():
    assert compute_class(_cents(85_000.01)) == "III"


def test_class_III_upper_bound_inclusive():
    assert compute_class(_cents(300_000)) == "III"


def test_class_IV_above_top():
    assert compute_class(_cents(300_000.01)) == "IV"
    assert compute_class(_cents(1_000_000)) == "IV"


# ── prescription_period ──────────────────────────────────────────────────
def test_period_known_types():
    assert prescription_period("1_an") == (1, "ans")
    assert prescription_period("3_ans") == (3, "ans")
    assert prescription_period("10_ans") == (10, "ans")
    assert prescription_period("30_ans") == (30, "ans")


def test_period_carries_sub_year_units():
    """The taxonomy's délais are mostly NOT whole years."""
    assert prescription_period("90_jours") == (90, "jours")
    assert prescription_period("45_jours") == (45, "jours")
    assert prescription_period("6_mois") == (6, "mois")
    assert prescription_period("3_mois") == (3, "mois")


def test_period_none_for_open_ended_or_unknown():
    assert prescription_period("imprescriptible") is None
    assert prescription_period("autre") is None
    assert prescription_period("") is None
    assert prescription_period("bogus") is None


def test_every_period_unit_is_dispatchable():
    """A unit typo would silently raise only for the affected key."""
    for key, (_label, period) in PRESCRIPTION_PERIODS.items():
        if period is None:
            continue
        amount, unit = period
        assert amount > 0, key
        assert unit in ("jours", "mois", "ans"), key
        # Must not raise — proves _add_period dispatches every declared unit.
        assert _add_period(_d(2026, 1, 15), amount, unit) is not None


def test_add_period_rejects_an_unknown_unit():
    with pytest.raises(ValueError):
        _add_period(_d(2026, 1, 15), 3, "semaines")


# ── compute_date_pour_agir ───────────────────────────────────────────────
def test_deadline_none_when_no_start_date():
    assert compute_date_pour_agir(None, "3_ans") is None


def test_deadline_none_when_imprescriptible():
    assert compute_date_pour_agir(_d(2020, 1, 15), "imprescriptible") is None


def test_deadline_none_when_autre_or_unset():
    assert compute_date_pour_agir(_d(2020, 1, 15), "autre") is None
    assert compute_date_pour_agir(_d(2020, 1, 15), "") is None


def test_deadline_extends_forward_off_a_sunday():
    # 15 Jan 2020 + 3 ans → 15 Jan 2023, a Sunday → next juridical Mon 16 Jan.
    assert compute_date_pour_agir(_d(2020, 1, 15), "3_ans") == _d(2023, 1, 16)


def test_deadline_extends_forward_off_a_holiday():
    # 1 Jan 2021 + 3 ans → 1 Jan 2024 (Jour de l'An) → next juridical Tue 2 Jan.
    assert compute_date_pour_agir(_d(2021, 1, 1), "3_ans") == _d(2024, 1, 2)


def test_deadline_on_a_weekday_is_unchanged():
    # 29 Feb 2020 + 3 ans → 28 Feb 2023 (Tuesday, juridical) → unchanged
    # (also exercises the leap-day → 28 Feb clamp).
    assert compute_date_pour_agir(_d(2020, 2, 29), "3_ans") == _d(2023, 2, 28)


def test_deadline_result_is_always_juridical():
    for start, ptype in [
        (_d(2020, 1, 15), "3_ans"),
        (_d(2021, 1, 1), "3_ans"),
        (_d(2024, 6, 1), "1_an"),
        (_d(2020, 3, 10), "10_ans"),
        (_d(1995, 12, 31), "30_ans"),
    ]:
        result = compute_date_pour_agir(start, ptype)
        assert result is not None
        assert is_juridical_day(result.date())


def test_deadline_preserves_utc_tzinfo():
    result = compute_date_pour_agir(_d(2020, 1, 15), "3_ans")
    assert result is not None and result.tzinfo == timezone.utc


# ── _add_years (both leap-year branches) ─────────────────────────────────
def test_add_years_clamps_to_feb_28_in_common_year():
    assert _add_years(_d(2020, 2, 29), 3) == _d(2023, 2, 28)


def test_add_months_clamps_to_the_target_months_last_day():
    # 31 Jan + 1 mois is 28 Feb, NOT 3 March (the month analogue of the
    # 29 Feb → 28 Feb year clamp).
    assert _add_months(_d(2026, 1, 31), 1) == _d(2026, 2, 28)
    assert _add_months(_d(2026, 3, 31), 1) == _d(2026, 4, 30)


def test_add_months_clamps_onto_a_leap_february():
    assert _add_months(_d(2024, 1, 31), 1) == _d(2024, 2, 29)


def test_add_months_rolls_over_the_year():
    assert _add_months(_d(2026, 11, 15), 3) == _d(2027, 2, 15)
    assert _add_months(_d(2026, 12, 1), 12) == _d(2027, 12, 1)
    # 9 mois (FAI-07, libération d'office) across a year boundary.
    assert _add_months(_d(2026, 6, 30), 9) == _d(2027, 3, 30)


def test_add_period_days_crosses_month_and_year_ends():
    # 90 jours is a real count of days, never "3 mois".
    assert _add_period(_d(2026, 1, 1), 90, "jours") == _d(2026, 4, 1)
    assert _add_period(_d(2026, 12, 15), 30, "jours") == _d(2027, 1, 14)


def test_days_and_months_are_not_interchangeable():
    """90 jours from 1 Jan lands on 1 Apr; 3 mois lands on 1 Apr too — but
    from 1 Dec they diverge, which is why the unit is stored, not inferred."""
    assert _add_period(_d(2026, 12, 1), 90, "jours") == _d(2027, 3, 1)
    assert _add_period(_d(2026, 12, 1), 3, "mois") == _d(2027, 3, 1)
    assert _add_period(_d(2026, 1, 1), 90, "jours") == _d(2026, 4, 1)
    assert _add_period(_d(2026, 1, 1), 3, "mois") == _d(2026, 4, 1)
    # A 31-day month run is where they part.
    assert _add_period(_d(2026, 7, 1), 90, "jours") == _d(2026, 9, 29)
    assert _add_period(_d(2026, 7, 1), 3, "mois") == _d(2026, 10, 1)


def test_deadline_for_a_days_period_is_juridical():
    # 45 jours (TRV-01, congédiement) from a Monday → raw 15 Mar 2026 is a
    # Sunday → extended forward to Monday 16 March.
    assert compute_date_pour_agir(_d(2026, 1, 29), "45_jours") == _d(2026, 3, 16)


def test_deadline_for_a_months_period_is_juridical():
    # 6 mois from 25 Jun 2026 → 25 Dec 2026 (Noël) → next juridical day.
    result = compute_date_pour_agir(_d(2026, 6, 25), "6_mois")
    assert result is not None and is_juridical_day(result.date())
    assert result > _d(2026, 12, 25)


def test_add_years_keeps_feb_29_when_target_is_leap():
    assert _add_years(_d(2020, 2, 29), 4) == _d(2024, 2, 29)


# ── reference tables ─────────────────────────────────────────────────────
def test_labels_and_valid_types_include_empty_state():
    assert "" in VALID_PRESCRIPTION_TYPES
    assert PRESCRIPTION_LABELS[""] == "Non définie"
    for key in ("1_an", "3_ans", "10_ans", "30_ans", "imprescriptible", "autre"):
        assert key in VALID_PRESCRIPTION_TYPES
        assert key in PRESCRIPTION_LABELS


# ── § 8 (15) — compute_date_pour_agir is intangible ──────────────────────
def test_compute_date_pour_agir_signature_unchanged():
    """The orchestration layer (compute_echeances) composes this function; its
    signature is part of the intangibility contract (spec § 0.3)."""
    import inspect

    sig = inspect.signature(compute_date_pour_agir)
    assert list(sig.parameters) == ["droit_action_date", "prescription_type"]


def test_compute_date_pour_agir_sample_grid_unchanged():
    """Literal expected dates over month-ends and leap days — a change in any
    of these is a change to the date arithmetic itself."""
    grid = [
        # (start, type) -> expected (incl. the art. 52 forward report)
        ((2026, 7, 18), "3_ans", (2029, 7, 18)),
        ((2025, 1, 31), "3_mois", (2025, 4, 30)),   # month-length clamp
        ((2025, 1, 31), "1_an", (2026, 2, 2)),      # 2026-01-31 Sat → Mon
        ((2024, 2, 29), "1_an", (2025, 2, 28)),     # leap clamp
        ((2024, 2, 29), "3_ans", (2027, 3, 1)),     # 2027-02-28 Sun → Mon
        ((2026, 5, 14), "90_jours", (2026, 8, 12)),
    ]
    for (sy, sm, sd), p_type, (ey, em, ed) in grid:
        assert compute_date_pour_agir(_d(sy, sm, sd), p_type) == _d(ey, em, ed), (
            (sy, sm, sd), p_type
        )
