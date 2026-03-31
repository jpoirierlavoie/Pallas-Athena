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

def normalize_email(raw: str) -> Optional[str]:
    """Normalize an email address.

    Rules:
    - Strip whitespace
    - Convert to lowercase
    - Basic pattern validation: must match [^@]+@[^@]+\\.[^@]+
    - Return None if invalid

    Do NOT attempt full RFC 5322 validation. The pattern check catches
    obvious errors (missing @, missing domain, spaces).
    """
    if not raw:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", normalized):
        return normalized
    return None


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

def normalize_postal_code(raw: str, country: str = "CA") -> Optional[str]:
    """Normalize a postal code.

    Canadian format: "A1A 1A1" (letter-digit-letter space digit-letter-digit)
    - Strip whitespace, uppercase
    - If 6 chars without space, insert space after 3rd char
    - Validate pattern

    US format: "12345" or "12345-6789"

    Other countries: return stripped/uppercased as-is (no validation).
    """
    if not raw:
        return None
    stripped = raw.strip().upper()
    if not stripped:
        return None

    if country == "CA":
        no_space = stripped.replace(" ", "")
        if len(no_space) == 6:
            formatted = f"{no_space[:3]} {no_space[3:]}"
            if re.match(r"^[A-Z]\d[A-Z] \d[A-Z]\d$", formatted):
                return formatted
        return None

    if country == "US":
        if re.match(r"^\d{5}(-\d{4})?$", stripped):
            return stripped
        return None

    # Other countries: return as-is
    return stripped


def validate_postal_code(
    raw: str, country: str = "CA"
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

DEFAULT_COUNTRY = "CA"
DEFAULT_PROVINCE = "QC"
DEFAULT_CITY = "Montréal"


def apply_address_defaults(data: dict, prefix: str = "address") -> dict:
    """Apply sensible defaults to address fields if they are empty.

    Args:
        data: The form data dict (mutated in place).
        prefix: The address field prefix ("address" for personal,
                "work_address" for professional).

    Defaults applied when the field is empty/missing:
    - {prefix}_country → "CA"
    - {prefix}_province → "QC" (only if country is "CA")
    - {prefix}_city → "Montréal" (only if province is "QC" and street is non-empty)
    """
    country_key = f"{prefix}_country"
    province_key = f"{prefix}_province"
    city_key = f"{prefix}_city"
    street_key = f"{prefix}_street"

    if not data.get(country_key):
        data[country_key] = DEFAULT_COUNTRY

    country = data.get(country_key, "")

    if country == DEFAULT_COUNTRY and not data.get(province_key):
        data[province_key] = DEFAULT_PROVINCE

    province = data.get(province_key, "")

    if (
        province == DEFAULT_PROVINCE
        and not data.get(city_key)
        and data.get(street_key, "").strip()
    ):
        data[city_key] = DEFAULT_CITY

    return data
