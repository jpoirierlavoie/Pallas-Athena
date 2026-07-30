"""Unit tests for utils/validators.py — contact data normalization."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import validators
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


# ── normalize_email: linearity + equivalence (CodeQL py/polynomial-redos) ──
# The shape check used to be `^[^@\s]+@[^@\s]+\.[^@\s]+$`, which is QUADRATIC:
# both classes after the « @ » match the dot, so on « x@ » + « a. »×n the
# engine tried every split point and rescanned the tail for each — 3.8 s at
# 40 KB, ~96 min at the portal's 1 MB body cap, GIL held throughout, and the
# PUBLIC /api/renvoi passes a JSON field straight in. A length bound alone did
# not satisfy CodeQL (it does not model `len() > N → return` as a sanitizer),
# so the pattern itself is gone: the checks are now a linear partition. These
# tests pin BOTH that the language is unchanged and that it stays linear.


def test_email_over_rfc_length_refused():
    """RFC 5321 caps an address at 254 characters. Kept as defence in depth
    even though the shape check no longer backtracks."""
    long_local = "a" * 250
    assert len(f"{long_local}@ex.com") > validators.EMAIL_MAX_LENGTH
    assert normalize_email(f"{long_local}@ex.com") is None
    # A 254-character address is still accepted (the bound is not stricter
    # than the RFC — a real long address must keep working).
    local = "a" * (validators.EMAIL_MAX_LENGTH - len("@example.com"))
    ok = f"{local}@example.com"
    assert len(ok) == validators.EMAIL_MAX_LENGTH
    assert normalize_email(ok) == ok


def test_email_redos_shape_returns_fast():
    """The adversarial shape must be refused in bounded time — now by the
    shape check itself, not merely by the length bound.

    The trailing « @ » is load-bearing and NOT interchangeable: it is what
    made a match impossible, forcing the old engine through every split point.
    A trailing SPACE — the shape CodeQL reports — is stripped before matching,
    after which the string MATCHED in microseconds; a test built on it would
    have passed even with the vulnerable pattern in place, pinning nothing."""
    attack = "x@" + "a." * 100_000 + "@"
    start = time.perf_counter()
    assert normalize_email(attack) is None
    assert validate_email(attack)[1] == "Adresse courriel invalide."
    assert time.perf_counter() - start < 2.0


def test_email_shape_matches_the_historical_regex():
    """The linear checks must recognise EXACTLY the old pattern's language.

    Verified out of band over 3 906 exhaustive strings and 400 000 random ones
    (16 Unicode whitespace classes, accents, CJK) with zero divergence; this is
    the CI-sized replay, so a future "let's just put the regex back, it read
    better" cannot pass silently. `_LEGACY` is the pattern that was removed —
    it lives here as the oracle, never in the module."""
    import itertools
    import re

    _LEGACY = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    accepts = lambda s: normalize_email(s) is not None  # noqa: E731

    for n in range(5):
        for tup in itertools.product("a@. \t", repeat=n):
            s = "".join(tup)
            # normalize_email strips and lowercases first, so compare the
            # oracle against the same normalized string it would see.
            norm = s.strip().lower()
            expected = bool(norm) and _LEGACY.match(norm) is not None
            assert accepts(s) is expected, f"divergence sur {s!r}"

    for s in ("john@example.com", "a@b.c", "a@b..c", "a@b.c.d.e", "a@b.c ",
              "A@B.COM", "no-reply+tag@sub.domain.co.uk", "é@dom.ca"):
        assert accepts(s), f"devrait être accepté : {s!r}"
    for s in ("a@.b", "a@b.", "a@@b.c", "a b@c.de", "a@b c.de", "@b.c", "a@",
              "@", ".", "userexample.com", "user@examplecom"):
        assert not accepts(s), f"devrait être refusé : {s!r}"


def test_email_rejects_embedded_newline_unlike_the_old_regex():
    """The one deliberate tightening: Python's ``$`` also matches just before
    a trailing newline, so the old pattern ACCEPTED « a@b.c\\n » and would have
    returned it verbatim. Unreachable through normalize_email (.strip() runs
    first), but a newline inside a stored address is never wanted — and a
    future refactor that drops the strip must not silently reopen it."""
    import re

    assert re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", "a@b.c\n") is not None
    assert validators._WHITESPACE_RE.search("a@b.c\n") is not None


def test_whitespace_guard_is_linear():
    """Tripwire, mirroring the ones in test_docx_fill: a single character
    class cannot backtrack. A `.` creeping in here would mean someone
    reintroduced a scanning pattern."""
    assert "." not in validators._WHITESPACE_RE.pattern
    assert validators._WHITESPACE_RE.pattern == r"\s"


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

def test_address_defaults_empty_stays_blank():
    # No street → no address at all → NO default is applied (avoid the
    # « (Québec) Canada » stub on addressless contacts).
    data = {}
    result = apply_address_defaults(data, "address")
    assert result.get("address_country", "") == ""
    assert result.get("address_province", "") == ""
    assert result.get("address_city", "") == ""

def test_address_city_defaults_when_street_filled():
    data = {"address_street": "123 rue Principale"}
    result = apply_address_defaults(data, "address")
    # A real street re-enables the speed defaults.
    assert result["address_city"] == "Montréal"
    assert result["address_province"] == "Québec"
    assert result["address_country"] == "Canada"

def test_address_city_no_default_without_street():
    data = {}
    result = apply_address_defaults(data, "address")
    assert result.get("address_city", "") == ""

def test_address_legacy_country_code_migrated():
    # Legacy "CA" is rewritten to the full name ALWAYS (migration runs even
    # without a street) — but with no street, the province is NOT defaulted.
    data = {"address_country": "CA"}
    result = apply_address_defaults(data, "address")
    assert result["address_country"] == "Canada"
    assert result.get("address_province", "") == ""

def test_address_legacy_country_code_migrated_with_street_defaults_province():
    data = {"address_country": "CA", "address_street": "1 rue X"}
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
