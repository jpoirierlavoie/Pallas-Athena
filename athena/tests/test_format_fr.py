"""Tests for utils/format_fr.py — French currency/date/number formatting."""

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.format_fr import (
    format_cents_fr,
    format_cents_fr_parens,
    format_date_fr,
    format_hours_fr,
    format_rate_fr,
)

NBSP = " "


# ── Currency ────────────────────────────────────────────────────────────

def test_currency_basic_and_nbsp_grouping():
    assert format_cents_fr(115000) == f"1{NBSP}150,00{NBSP}$"
    assert format_cents_fr(123456789) == f"1{NBSP}234{NBSP}567,89{NBSP}$"
    assert format_cents_fr(5000) == f"50,00{NBSP}$"
    assert format_cents_fr(99) == f"0,99{NBSP}$"


def test_currency_zero_never_bare():
    assert format_cents_fr(0) == f"0,00{NBSP}$"


def test_currency_negative_keeps_sign():
    assert format_cents_fr(-115000) == f"-1{NBSP}150,00{NBSP}$"


def test_currency_uses_real_nbsp_not_space():
    out = format_cents_fr(115000)
    assert " " in out
    assert "1 150" not in out  # not a regular ASCII space


def test_parens_deduction():
    assert format_cents_fr_parens(115000) == f"(1{NBSP}150,00){NBSP}$"
    assert format_cents_fr_parens(0) == f"(0,00){NBSP}$"
    # A stored positive retainer displays parenthesized regardless of sign.
    assert format_cents_fr_parens(-115000) == f"(1{NBSP}150,00){NBSP}$"


# ── Rates ───────────────────────────────────────────────────────────────

def test_rate_gst_and_qst_scales():
    assert format_rate_fr(500, 100) == f"5{NBSP}%"        # GST 5.00 %
    assert format_rate_fr(9975, 1000) == f"9,975{NBSP}%"  # QST 9.975 %


def test_rate_trims_trailing_zeros():
    assert format_rate_fr(5000, 1000) == f"5{NBSP}%"
    assert format_rate_fr(9900, 1000) == f"9,9{NBSP}%"


# ── Hours ───────────────────────────────────────────────────────────────

def test_hours_two_decimals_comma():
    assert format_hours_fr(0.5) == "0,50"
    assert format_hours_fr(2) == "2,00"
    assert format_hours_fr(12.25) == "12,25"


# ── Dates ───────────────────────────────────────────────────────────────

def test_date_long_french_and_premier():
    assert format_date_fr(date(2025, 12, 11)) == "11 décembre 2025"
    assert format_date_fr(date(2026, 5, 1)) == "1er mai 2026"


def test_date_accepts_datetime_uses_utc_calendar_date():
    # Midnight-UTC storage → the UTC calendar date, no Montréal shift.
    dt = datetime(2025, 12, 11, 0, 0, tzinfo=timezone.utc)
    assert format_date_fr(dt) == "11 décembre 2025"
