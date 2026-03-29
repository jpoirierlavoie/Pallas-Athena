# PHASE B — Input Validation & Normalization

Read CLAUDE.md for project context. This phase adds a validation and normalization layer for contact data (phone numbers, emails, addresses, postal codes) to ensure uniform, exportable data across the application.

## Context

The current `models/partie.py` only validates that required fields exist and that email contains `@`. Phone numbers are stored as free-text strings with no formatting. Addresses have no validation. This phase creates a `utils/validators.py` module that normalizes data on input, and updates all forms and models that accept these fields.

## Step 1 — Create `utils/validators.py`

```python
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
    ...


def format_phone_display(e164: str) -> str:
    """Format an E.164 phone number for display.

    Rules:
    - Canadian/US (+1): "+1 (514) 555-1234"
    - International: "+33 1 42 68 53 00" (just insert spaces for readability)
    - If not valid E.164, return as-is

    For +1 numbers specifically:
    "+1AAABBBCCCC" → "+1 (AAA) BBB-CCCC"
    """
    ...


def validate_phone(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize a phone number.

    Returns (normalized_value, error_message).
    If valid: ("+15145551234", None)
    If invalid: (None, "Numéro de téléphone invalide.")
    If empty: (None, None) — empty is acceptable for optional fields
    """
    ...


# ── Email ────────────────────────────────────────────────────────────────

def normalize_email(raw: str) -> Optional[str]:
    """Normalize an email address.

    Rules:
    - Strip whitespace
    - Convert to lowercase
    - Basic pattern validation: must match [^@]+@[^@]+\.[^@]+
    - Return None if invalid

    Do NOT attempt full RFC 5322 validation. The pattern check catches
    obvious errors (missing @, missing domain, spaces).
    """
    ...


def validate_email(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize an email.

    Returns (normalized_value, error_message).
    """
    ...


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
    ...


def validate_postal_code(raw: str, country: str = "CA") -> tuple[Optional[str], Optional[str]]:
    """Validate and normalize a postal code.

    Returns (normalized_value, error_message).
    """
    ...


# ── Address Defaults ─────────────────────────────────────────────────────

DEFAULT_COUNTRY = "CA"
DEFAULT_PROVINCE = "QC"
DEFAULT_CITY = "Montréal"


def apply_address_defaults(data: dict, prefix: str = "address") -> dict:
    """Apply sensible defaults to address fields if they are empty.

    Args:
        data: The form data dict.
        prefix: The address field prefix ("address" for personal,
                "work_address" for professional).

    Defaults applied when the field is empty/missing:
    - {prefix}_country → "CA"
    - {prefix}_province → "QC" (only if country is "CA")
    - {prefix}_city → "Montréal" (only if province is "QC" and city is empty)

    City default is ONLY applied if the street is non-empty (i.e., the user
    started filling the address). We don't auto-fill city on a completely
    blank address.
    """
    ...
```

## Step 2 — Create Display Filter in Jinja2

Update `main.py` to register the phone display filter:

```python
from utils.validators import format_phone_display

app.jinja_env.filters["phone"] = format_phone_display
```

This allows templates to use `{{ partie.phone_cell|phone }}` to display formatted phone numbers.

## Step 3 — Update `models/partie.py`

Modify the `_validate` function to use the new validators:

```python
from utils.validators import (
    validate_phone,
    validate_email,
    validate_postal_code,
    apply_address_defaults,
    normalize_phone,
    normalize_email,
    normalize_postal_code,
)

def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    client_type = data.get("type", "individual")

    if client_type == "individual":
        if not data.get("last_name", "").strip():
            errors.append("Le nom de famille est requis.")
    elif client_type == "organization":
        if not data.get("organization_name", "").strip():
            errors.append("Le nom de l'organisation est requis.")
    else:
        errors.append("Type de contact invalide.")

    if data.get("contact_role", "") not in VALID_CONTACT_ROLES:
        errors.append("Rôle de contact invalide.")

    # Phone validation (all phone fields)
    for field, label in [
        ("phone_home", "Téléphone domicile"),
        ("phone_cell", "Cellulaire"),
        ("phone_work", "Téléphone professionnel"),
        ("fax", "Télécopieur"),
    ]:
        raw = data.get(field, "").strip()
        if raw:
            normalized, err = validate_phone(raw)
            if err:
                errors.append(f"{label} : {err}")
            else:
                data[field] = normalized  # Replace with normalized value

    # Email validation
    for field, label in [
        ("email", "Courriel"),
        ("email_work", "Courriel professionnel"),
    ]:
        raw = data.get(field, "").strip()
        if raw:
            normalized, err = validate_email(raw)
            if err:
                errors.append(f"{label} : {err}")
            else:
                data[field] = normalized

    # Postal code validation
    for prefix in ("address", "work_address"):
        country = data.get(f"{prefix}_country", "CA")
        raw_pc = data.get(f"{prefix}_postal_code", "").strip()
        if raw_pc:
            normalized, err = validate_postal_code(raw_pc, country)
            if err:
                errors.append(f"Code postal ({prefix.replace('_', ' ')}) : {err}")
            else:
                data[f"{prefix}_postal_code"] = normalized

    return errors
```

Add a `_normalize` function that runs BEFORE `_validate`, called by both `create_partie` and `update_partie`:

```python
def _normalize(data: dict) -> dict:
    """Normalize input data before validation."""
    # Apply address defaults
    for prefix in ("address", "work_address"):
        data = apply_address_defaults(data, prefix)

    # Normalize phones (even if validation will run again, normalize early)
    for field in ("phone_home", "phone_cell", "phone_work", "fax"):
        raw = data.get(field, "").strip()
        if raw:
            normalized = normalize_phone(raw)
            if normalized:
                data[field] = normalized

    # Normalize emails
    for field in ("email", "email_work"):
        raw = data.get(field, "").strip()
        if raw:
            normalized = normalize_email(raw)
            if normalized:
                data[field] = normalized

    # Normalize postal codes
    for prefix in ("address", "work_address"):
        country = data.get(f"{prefix}_country", "CA")
        raw_pc = data.get(f"{prefix}_postal_code", "").strip()
        if raw_pc:
            normalized = normalize_postal_code(raw_pc, country)
            if normalized:
                data[f"{prefix}_postal_code"] = normalized

    return data
```

Update `create_partie` and `update_partie` to call `_normalize` before `_sanitize_data`:

```python
def create_partie(data: dict) -> tuple[Optional[dict], list[str]]:
    data = _normalize(data)
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    ...
```

## Step 4 — Update Templates for Phone Display

In all templates that display phone numbers, use the new filter:

```jinja2
{# OLD: #}
{{ partie.phone_cell }}

{# NEW: #}
{{ partie.phone_cell|phone }}
```

Update these templates:
- `templates/parties/detail.html` — all phone displays and `tel:` links
- `templates/parties/_partie_rows.html` — phone in list rows
- `templates/parties/form.html` — phone input placeholders

For the `tel:` href, continue using the raw E.164 value (it's already in the correct format for `tel:` links):
```jinja2
<a href="tel:{{ partie.phone_cell }}">{{ partie.phone_cell|phone }}</a>
```

## Step 5 — Update Form Placeholders and Defaults

In `templates/parties/form.html`:

1. Phone field placeholders should show the expected format:
```html
<input type="tel" name="phone_cell" placeholder="+1 (514) 555-1234" ...>
```

2. Address country field should default to "CA":
```html
<input type="text" name="address_country" value="{{ partie.address_country if partie else 'CA' }}" ...>
```

3. Address province should default to "QC":
```html
<input type="text" name="address_province" value="{{ partie.address_province if partie else 'QC' }}" ...>
```

These defaults are already partially in place — verify and ensure consistency.

## Step 6 — Update vCard Serialization

In `models/partie.py`, update `partie_to_vcard` to ensure phone numbers are serialized in E.164 format (they should already be if normalization ran on save). No change needed if the stored value is already E.164.

In `vcard_to_partie`, the incoming phone from DavX5 may be in various formats. Run `normalize_phone` on each parsed phone value:

```python
# In vcard_to_partie, after extracting phone values:
from utils.validators import normalize_phone

# For each phone field extracted:
if phone_value:
    normalized = normalize_phone(phone_value)
    if normalized:
        data[field] = normalized
    else:
        data[field] = phone_value  # Keep raw if normalization fails
```

## Step 7 — Data Migration Consideration

Existing contact records have un-normalized phone numbers and postal codes. Add a one-time migration utility (not a route — a standalone script):

Create `scripts/normalize_existing.py`:
```python
"""One-time migration: normalize phone numbers and postal codes for all parties."""

# This script is run manually: python scripts/normalize_existing.py
# It reads all parties from Firestore, normalizes phone/email/postal fields,
# and writes back only the changed records.

# Usage: cd athena && python -m scripts.normalize_existing
```

The script should:
1. Fetch all documents from the `parties` collection
2. For each, run `normalize_phone` on all phone fields, `normalize_email` on email fields, `normalize_postal_code` on postal code fields, `apply_address_defaults` on address fields
3. If any field changed, update the document (with a new etag and updated_at)
4. Print a summary: "Normalized X of Y records. Z fields changed."
5. Bump the CardDAV ctag after migration so DavX5 picks up changes

## Testing Checklist
- [ ] `normalize_phone("(514) 555-1234")` → `"+15145551234"`
- [ ] `normalize_phone("514-555-1234")` → `"+15145551234"`
- [ ] `normalize_phone("+33 1 42 68 53 00")` → `"+33142685300"`
- [ ] `normalize_phone("555-1234")` → `"+15145551234"` (Montreal default)
- [ ] `normalize_phone("")` → `None`
- [ ] `format_phone_display("+15145551234")` → `"+1 (514) 555-1234"`
- [ ] `normalize_email("  John@Example.COM  ")` → `"john@example.com"`
- [ ] `normalize_postal_code("h2t1s6")` → `"H2T 1S6"`
- [ ] `normalize_postal_code("H2T 1S6")` → `"H2T 1S6"` (already valid)
- [ ] `validate_postal_code("XXXXX", "CA")` → error
- [ ] Address defaults apply correctly (country=CA, province=QC, city=Montréal when street is filled)
- [ ] Creating a new partie with raw phone "514-555-1234" stores "+15145551234"
- [ ] Editing a partie preserves normalized values
- [ ] Phone display in detail page shows formatted "+1 (514) 555-1234"
- [ ] `tel:` links use raw E.164 value
- [ ] vCard export contains E.164 phone numbers
- [ ] vCard import normalizes incoming phone numbers
- [ ] Migration script runs without error on existing data
