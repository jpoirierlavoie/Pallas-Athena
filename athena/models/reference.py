"""Quebec court reference data — greffes and juridictions.

Read-only model for querying Firestore reference collections.
Data is seeded by scripts/seed_reference_data.py.
"""

from typing import Optional

from models import db

REF_GREFFES = "ref_greffes"
REF_JURIDICTIONS = "ref_juridictions"


def get_greffe(greffe_number: str) -> Optional[dict]:
    """Look up a greffe by its 3-digit number."""
    doc = db.collection(REF_GREFFES).document(greffe_number).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_juridiction(juridiction_number: str) -> Optional[dict]:
    """Look up a juridiction by its 2-digit number."""
    doc = db.collection(REF_JURIDICTIONS).document(juridiction_number).get()
    if doc.exists:
        return doc.to_dict()
    return None


def list_greffes() -> list[dict]:
    """Return all greffes, sorted by palais_de_justice name."""
    docs = db.collection(REF_GREFFES).order_by("palais_de_justice").stream()
    return [d.to_dict() for d in docs]


def list_juridictions() -> list[dict]:
    """Return all juridictions, sorted by juridiction_number."""
    docs = db.collection(REF_JURIDICTIONS).order_by("juridiction_number").stream()
    return [d.to_dict() for d in docs]


def parse_court_file_number(court_file_number: str) -> dict:
    """Parse a Quebec court file number and return resolved metadata.

    Args:
        court_file_number: e.g., "500-05-123456-241"

    Returns:
        {
            "greffe_number": "500" or None,
            "juridiction_number": "05" or None,
            "greffe": { ... } or None,
            "juridiction": { ... } or None,
            "is_administrative": False,
            "parse_error": None or "error message"
        }
    """
    result = {
        "greffe_number": None,
        "juridiction_number": None,
        "greffe": None,
        "juridiction": None,
        "is_administrative": False,
        "parse_error": None,
    }

    if not court_file_number or not court_file_number.strip():
        result["parse_error"] = "Numéro de dossier judiciaire requis."
        return result

    cleaned = court_file_number.strip()

    # Check for administrative tribunal prefix (starts with letters)
    if cleaned[0].isalpha():
        result["is_administrative"] = True
        return result

    # Parse: expect NNN-NN-...
    parts = cleaned.split("-")
    if len(parts) < 2:
        result["parse_error"] = (
            "Format invalide. Attendu : NNN-NN-NNNNNN-NN "
            "(ex. : 500-05-123456-241)"
        )
        return result

    greffe_str = parts[0]
    juridiction_str = parts[1]

    # Validate greffe: exactly 3 digits
    if len(greffe_str) != 3 or not greffe_str.isdigit():
        result["parse_error"] = (
            f"Le numéro de greffe « {greffe_str} » doit être composé "
            "de 3 chiffres."
        )
        return result

    # Validate juridiction: exactly 2 digits
    if len(juridiction_str) != 2 or not juridiction_str.isdigit():
        result["parse_error"] = (
            f"Le numéro de juridiction « {juridiction_str} » doit être "
            "composé de 2 chiffres."
        )
        return result

    result["greffe_number"] = greffe_str
    result["juridiction_number"] = juridiction_str

    # Look up in Firestore
    result["greffe"] = get_greffe(greffe_str)
    result["juridiction"] = get_juridiction(juridiction_str)

    if not result["greffe"]:
        result["parse_error"] = (
            f"Greffe « {greffe_str} » introuvable dans les données "
            "de référence."
        )

    if not result["juridiction"]:
        existing_error = result["parse_error"] or ""
        sep = " " if existing_error else ""
        result["parse_error"] = (
            f"{existing_error}{sep}Juridiction « {juridiction_str} » "
            "introuvable dans les données de référence."
        )

    return result
