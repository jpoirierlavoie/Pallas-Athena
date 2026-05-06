"""Unit tests for utils/validators.py — contact data normalization."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.validators import (
    normalize_phone,
    format_phone_display,
    validate_phone,
    normalize_email,
    validate_email,
    normalize_postal_code,
    validate_postal_code,
    apply_address_defaults,
)


# ── normalize_phone ───────────────────────────────────────────────────────

def test_phone_parentheses_format():
    assert normalize_phone("(514) 555-1234") == "+15145551234"

def test_phone_dashes():
    assert normalize_phone("514-555-1234") == "+15145551234"

def test_phone_10_digits():
    assert normalize_phone("5145551234") == "+15145551234"

def test_phone_international():
    assert normalize_phone("+33 1 42 68 53 00") == "+33142685300"

def test_phone_7_digits_local():
    assert normalize_phone("555-1234") == "+15145551234"

def test_phone_1_800():
    assert normalize_phone("1-800-555-1234") == "+18005551234"

def test_phone_empty():
    assert normalize_phone("") is None

def test_phone_whitespace_only():
    assert normalize_phone("   ") is None

def test_phone_letters_only():
    assert normalize_phone("abc") is None

def test_phone_already_e164():
    assert normalize_phone("+15145551234") == "+15145551234"

def test_phone_11_digits_with_1():
    assert normalize_phone("15145551234") == "+15145551234"

def test_phone_too_short():
    assert normalize_phone("123") is None

def test_phone_strips_spaces():
    assert normalize_phone("  (514) 555-1234  ") == "+15145551234"


# ── format_phone_display ──────────────────────────────────────────────────

def test_display_north_american():
    assert format_phone_display("+15145551234") == "+1 (514) 555-1234"

def test_display_toll_free():
    assert format_phone_display("+18005551234") == "+1 (800) 555-1234"

def test_display_international_passthrough():
    # International numbers are returned as-is
    assert format_phone_display("+33142685300") == "+33142685300"

def test_display_empty():
    assert format_phone_display("") == ""

def test_display_not_e164():
    assert format_phone_display("514-555-1234") == "514-555-1234"


# ── validate_phone ────────────────────────────────────────────────────────

def test_validate_phone_valid():
    val, err = validate_phone("(514) 555-1234")
    assert val == "+15145551234"
    assert err is None

def test_validate_phone_invalid():
    val, err = validate_phone("abc")
    assert val is None
    assert err == "Numéro de téléphone invalide."

def test_validate_phone_empty():
    val, err = validate_phone("")
    assert val is None
    assert err is None


# ── normalize_email ───────────────────────────────────────────────────────

def test_email_normalizes_case():
    assert normalize_email("  John@Example.COM  ") == "john@example.com"

def test_email_strips_whitespace():
    assert normalize_email("  user@example.com  ") == "user@example.com"

def test_email_missing_at():
    assert normalize_email("userexample.com") is None

def test_email_missing_dot():
    assert normalize_email("user@examplecom") is None

def test_email_empty():
    assert normalize_email("") is None

def test_email_valid():
    assert normalize_email("avocat@barreau.qc.ca") == "avocat@barreau.qc.ca"


# ── validate_email ────────────────────────────────────────────────────────

def test_validate_email_valid():
    val, err = validate_email("User@Example.COM")
    assert val == "user@example.com"
    assert err is None

def test_validate_email_invalid():
    val, err = validate_email("notanemail")
    assert val is None
    assert err == "Adresse courriel invalide."

def test_validate_email_empty():
    val, err = validate_email("")
    assert val is None
    assert err is None


# ── normalize_postal_code ─────────────────────────────────────────────────

def test_postal_lowercase_no_space():
    assert normalize_postal_code("h2t1s6") == "H2T 1S6"

def test_postal_already_valid():
    assert normalize_postal_code("H2T 1S6") == "H2T 1S6"

def test_postal_uppercase_no_space():
    assert normalize_postal_code("H2T1S6") == "H2T 1S6"

def test_postal_invalid_canadian():
    assert normalize_postal_code("XXXXX", "CA") is None

def test_postal_us_5_digit():
    assert normalize_postal_code("90210", "US") == "90210"

def test_postal_us_9_digit():
    assert normalize_postal_code("90210-1234", "US") == "90210-1234"

def test_postal_us_invalid():
    assert normalize_postal_code("ABCDE", "US") is None

def test_postal_other_country():
    # Non-CA/US returns as-is
    assert normalize_postal_code("75001", "FR") == "75001"

def test_postal_empty():
    assert normalize_postal_code("") is None


# ── validate_postal_code ──────────────────────────────────────────────────

def test_validate_postal_valid():
    val, err = validate_postal_code("h2t1s6")
    assert val == "H2T 1S6"
    assert err is None

def test_validate_postal_invalid():
    val, err = validate_postal_code("XXXXX", "CA")
    assert val is None
    assert err == "Code postal invalide."

def test_validate_postal_empty():
    val, err = validate_postal_code("")
    assert val is None
    assert err is None


# ── apply_address_defaults ────────────────────────────────────────────────

def test_address_defaults_empty():
    data = {}
    result = apply_address_defaults(data, "address")
    assert result["address_country"] == "Canada"
    assert result["address_province"] == "Québec"
    # No street → no city default
    assert result.get("address_city", "") == ""

def test_address_city_defaults_when_street_filled():
    data = {"address_street": "123 rue Principale"}
    result = apply_address_defaults(data, "address")
    assert result["address_city"] == "Montréal"

def test_address_city_no_default_without_street():
    data = {}
    result = apply_address_defaults(data, "address")
    assert result.get("address_city", "") == ""

def test_address_legacy_country_code_migrated():
    # Legacy "CA" should be rewritten to the full name on save.
    data = {"address_country": "CA"}
    result = apply_address_defaults(data, "address")
    assert result["address_country"] == "Canada"
    assert result["address_province"] == "Québec"

def test_address_legacy_us_code_migrated():
    data = {"address_country": "US"}
    result = apply_address_defaults(data, "address")
    assert result["address_country"] == "États-Unis"
    # Province not defaulted for non-Canadian addresses
    assert result.get("address_province", "") == ""

def test_address_legacy_province_code_migrated():
    data = {
        "address_country": "Canada",
        "address_province": "ON",
        "address_city": "Toronto",
        "address_street": "1 Bay Street",
    }
    result = apply_address_defaults(data, "address")
    assert result["address_province"] == "Ontario"
    assert result["address_city"] == "Toronto"

def test_work_address_prefix():
    data = {"work_address_street": "1000 De La Gauchetière"}
    result = apply_address_defaults(data, "work_address")
    assert result["work_address_country"] == "Canada"
    assert result["work_address_province"] == "Québec"
    assert result["work_address_city"] == "Montréal"
