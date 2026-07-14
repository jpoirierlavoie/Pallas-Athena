"""Unit tests for utils.recours (prescription deadline + value class)."""

from datetime import datetime, timezone

from utils.deadlines import is_juridical_day
from utils.recours import (
    PRESCRIPTION_LABELS,
    VALID_PRESCRIPTION_TYPES,
    _add_years,
    compute_class,
    compute_date_pour_agir,
    prescription_years,
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


# ── prescription_years ───────────────────────────────────────────────────
def test_years_known_types():
    assert prescription_years("1_an") == 1
    assert prescription_years("3_ans") == 3
    assert prescription_years("10_ans") == 10
    assert prescription_years("30_ans") == 30


def test_years_none_for_open_ended_or_unknown():
    assert prescription_years("imprescriptible") is None
    assert prescription_years("autre") is None
    assert prescription_years("") is None
    assert prescription_years("bogus") is None


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


def test_add_years_keeps_feb_29_when_target_is_leap():
    assert _add_years(_d(2020, 2, 29), 4) == _d(2024, 2, 29)


# ── reference tables ─────────────────────────────────────────────────────
def test_labels_and_valid_types_include_empty_state():
    assert "" in VALID_PRESCRIPTION_TYPES
    assert PRESCRIPTION_LABELS[""] == "Non définie"
    for key in ("1_an", "3_ans", "10_ans", "30_ans", "imprescriptible", "autre"):
        assert key in VALID_PRESCRIPTION_TYPES
        assert key in PRESCRIPTION_LABELS
