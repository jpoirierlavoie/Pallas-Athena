"""Input validation and normalization for contact data."""

import re
from typing import Optional


# ── Phone Numbers ────────────────────────────────────────────────────────

def normalize_phone(raw: str, default_country: str = "+1") -> Optional[str]:
    """Normalize a phone number to E.164 format.

    Rules:
    - Strip all non-digit characters except leading +
    - If starts with +, keep as-is (international number)
    - If starts with 1 and is 11 digits, prepend +
    - If 10 digits (North American), prepend +1
    - If 7 digits (local), prepend +1514 (Montreal area code)
    - Return None if the result doesn't match a valid pattern

    Examples:
        "(514) 555-1234"  → "+15145551234"
        "514-555-1234"    → "+15145551234"
        "5145551234"      → "+15145551234"
        "+33 1 42 68 53 00" → "+33142685300"
        "555-1234"        → "+15145551234"
        "1-800-555-1234"  → "+18005551234"
        ""                → None
        "abc"             → None

    Returns:
        E.164 formatted string (e.g., "+15145551234") or None if invalid.
    """
    if not raw or not raw.strip():
        return None

    stripped = raw.strip()

    if stripped.startswith("+"):
        # International — strip all non-digits after the +
        digits = re.sub(r"\D", "", stripped[1:])
        e164 = f"+{digits}"
    else:
        digits = re.sub(r"\D", "", stripped)
        if len(digits) == 11 and digits.startswith("1"):
            e164 = f"+{digits}"
        elif len(digits) == 10:
            e164 = f"+1{digits}"
        elif len(digits) == 7:
            e164 = f"+1514{digits}"
        else:
            return None

    # E.164: + followed by 8–15 digits
    if re.match(r"^\+\d{8,15}$", e164):
        return e164
    return None


def format_phone_display(e164: str) -> str:
    """Format an E.164 phone number for display.

    Rules:
    - Canadian/US (+1AAABBBCCCC): "+1 (AAA) BBB-CCCC"
    - Other: return as-is
    - If not a valid E.164 string, return as-is
    """
    if not e164 or not e164.startswith("+"):
        return e164

    if e164.startswith("+1") and len(e164) == 12:
        area = e164[2:5]
        exchange = e164[5:8]
        number = e164[8:12]
        return f"+1 ({area}) {exchange}-{number}"

    return e164


def validate_phone(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize a phone number.

    Returns (normalized_value, error_message).
    If valid: ("+15145551234", None)
    If invalid: (None, "Numéro de téléphone invalide.")
    If empty: (None, None) — empty is acceptable for optional fields
    """
    if not raw or not raw.strip():
        return None, None
    normalized = normalize_phone(raw)
    if normalized:
        return normalized, None
    return None, "Numéro de téléphone invalide."


# ── Email ────────────────────────────────────────────────────────────────

# RFC 5321 §4.5.3.1.3 caps a forward-path (the whole address) at 256 octets
# including the angle brackets — 254 usable characters. Nothing legitimate
# exceeds it. Kept as defence in depth: the shape check below is now linear,
# so this bound is no longer the only thing standing between a public JSON
# body and a quadratic scan — but an unbounded address is invalid anyway, and
# the portal's /api/renvoi feeds this function straight from an unauthenticated
# request. Never "fix" an over-long value by truncating (that would turn a
# 300-character string into a plausible-looking address).
EMAIL_MAX_LENGTH = 254

# A single character class with no quantifier — it cannot backtrack, and it
# keeps the EXACT `\s` semantics of the pattern this replaced (str.isspace()
# is not identical). The tripwire in tests/test_validators.py asserts it stays
# free of `.`.
_WHITESPACE_RE = re.compile(r"\s")


def normalize_email(raw: str) -> Optional[str]:
    """Normalize an email address.

    Rules:
    - Strip whitespace
    - Convert to lowercase
    - Reject anything longer than EMAIL_MAX_LENGTH (RFC 5321)
    - Basic shape validation: ``local@domain`` where the domain carries a dot
      that is neither its first nor its last character, and no whitespace or
      second « @ » appears anywhere
    - Return None if invalid

    Do NOT attempt full RFC 5322 validation. The shape check catches obvious
    errors (missing @, missing domain, spaces).

    This used to be the regex ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$``, which was
    POLYNOMIAL (CodeQL py/polynomial-redos, high): both classes after the
    « @ » match the dot, so on « x@ » + « a. »×n the engine tried every split
    point and rescanned the tail for each — 3.8 s at 40 KB, ~96 min at the
    portal's 1 MB body cap, with the GIL held the whole time. The checks below
    recognise the IDENTICAL language in one linear pass (equivalence pinned by
    test_email_shape_matches_the_historical_regex), with one deliberate
    tightening: Python's ``$`` also matches just before a trailing newline, so
    the old pattern accepted « a@b.c\\n ». Unreachable here because .strip()
    runs first, but a newline in a stored address is never wanted.
    """
    if not raw:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    if len(normalized) > EMAIL_MAX_LENGTH:
        return None
    if _WHITESPACE_RE.search(normalized):
        return None
    local, at, domain = normalized.partition("@")
    if not at or not local or "@" in domain:
        return None
    # `domain[1:-1]` is exactly `[^@\s]+\.[^@\s]+`: a dot with at least one
    # character before it and at least one after.
    if "." not in domain[1:-1]:
        return None
    return normalized


def validate_email(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize an email.

    Returns (normalized_value, error_message).
    If valid: ("user@example.com", None)
    If invalid: (None, "Adresse courriel invalide.")
    If empty: (None, None)
    """
    if not raw or not raw.strip():
        return None, None
    normalized = normalize_email(raw)
    if normalized:
        return normalized, None
    return None, "Adresse courriel invalide."


# ── Postal Code ──────────────────────────────────────────────────────────

_CANADIAN_COUNTRY_VALUES = {"ca", "can", "canada"}
_AMERICAN_COUNTRY_VALUES = {"us", "usa", "états-unis", "etats-unis", "united states"}


def normalize_postal_code(raw: str, country: str = "Canada") -> Optional[str]:
    """Normalize a postal code.

    Canadian format: "A1A 1A1" (letter-digit-letter space digit-letter-digit)
    - Strip whitespace, uppercase
    - If 6 chars without space, insert space after 3rd char
    - Validate pattern

    US format: "12345" or "12345-6789"

    Other countries: return stripped/uppercased as-is (no validation).

    Recognizes both legacy two-letter codes ("CA"/"US") and full names
    ("Canada", "États-Unis", …) for the *country* argument.
    """
    if not raw:
        return None
    stripped = raw.strip().upper()
    if not stripped:
        return None

    country_key = (country or "").strip().lower()

    if country_key in _CANADIAN_COUNTRY_VALUES:
        no_space = stripped.replace(" ", "")
        if len(no_space) == 6:
            formatted = f"{no_space[:3]} {no_space[3:]}"
            if re.match(r"^[A-Z]\d[A-Z] \d[A-Z]\d$", formatted):
                return formatted
        return None

    if country_key in _AMERICAN_COUNTRY_VALUES:
        if re.match(r"^\d{5}(-\d{4})?$", stripped):
            return stripped
        return None

    # Other countries: return as-is
    return stripped


def validate_postal_code(
    raw: str, country: str = "Canada"
) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize a postal code.

    Returns (normalized_value, error_message).
    If valid: ("H2T 1S6", None)
    If invalid: (None, "Code postal invalide.")
    If empty: (None, None)
    """
    if not raw or not raw.strip():
        return None, None
    normalized = normalize_postal_code(raw, country)
    if normalized:
        return normalized, None
    return None, "Code postal invalide."


# ── Address Defaults ─────────────────────────────────────────────────────

DEFAULT_COUNTRY = "Canada"
DEFAULT_PROVINCE = "Québec"
DEFAULT_CITY = "Montréal"

# Legacy two-letter codes are migrated to full names on the next save so the
# stored value matches the rest of the address (which is already long-form).
_LEGACY_COUNTRY_MAP = {"CA": "Canada", "US": "États-Unis"}
_LEGACY_PROVINCE_MAP = {
    "QC": "Québec",
    "ON": "Ontario",
    "BC": "Colombie-Britannique",
    "AB": "Alberta",
    "MB": "Manitoba",
    "SK": "Saskatchewan",
    "NB": "Nouveau-Brunswick",
    "NS": "Nouvelle-Écosse",
    "PE": "Île-du-Prince-Édouard",
    "NL": "Terre-Neuve-et-Labrador",
    "YT": "Yukon",
    "NT": "Territoires du Nord-Ouest",
    "NU": "Nunavut",
}


def apply_address_defaults(data: dict, prefix: str = "address") -> dict:
    """Apply sensible defaults to address fields if they are empty.

    Args:
        data: The form data dict (mutated in place).
        prefix: The address field prefix ("address" for personal,
                "work_address" for professional).

    Behavior:
    - Migrates legacy 2-letter codes to full names ("CA" → "Canada",
      "QC" → "Québec", …) so storage stays consistent with the new defaults.
    - Migrates legacy 2-letter codes ALWAYS (so stored data stays consistent).
    - Applies the city/province/country defaults ONLY when a street is
      provided. An addressless contact therefore stays fully blank instead
      of collecting a « (Québec) Canada » stub that would then surface
      everywhere (invoices, the accusé bordereau, letter address blocks).
    """
    country_key = f"{prefix}_country"
    province_key = f"{prefix}_province"
    city_key = f"{prefix}_city"
    street_key = f"{prefix}_street"

    # Migrate legacy abbreviations to full names
    raw_country = (data.get(country_key) or "").strip()
    if raw_country.upper() in _LEGACY_COUNTRY_MAP:
        data[country_key] = _LEGACY_COUNTRY_MAP[raw_country.upper()]

    raw_province = (data.get(province_key) or "").strip()
    if raw_province.upper() in _LEGACY_PROVINCE_MAP:
        data[province_key] = _LEGACY_PROVINCE_MAP[raw_province.upper()]

    # No street → no address at all: apply NO default (avoid the stub).
    if not (data.get(street_key) or "").strip():
        return data

    # Apply defaults when fields are still empty
    if not (data.get(country_key) or "").strip():
        data[country_key] = DEFAULT_COUNTRY

    country = (data.get(country_key) or "").strip()

    if country == DEFAULT_COUNTRY and not (data.get(province_key) or "").strip():
        data[province_key] = DEFAULT_PROVINCE

    province = (data.get(province_key) or "").strip()

    if province == DEFAULT_PROVINCE and not (data.get(city_key) or "").strip():
        data[city_key] = DEFAULT_CITY

    return data
