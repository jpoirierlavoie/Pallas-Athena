"""Tests for the court-location (palais de justice) address table in
models/reference.py.

A filing address depends on this data, so the structural invariants are
asserted rather than eyeballed.

``models/reference.py`` is pure — it imports only ``typing``. Plain
``import models.reference`` would nonetheless run ``models/__init__``, which
constructs a Firestore client at import time; that only resolves locally
because another test module happens to stub ``google.cloud.firestore`` into
``sys.modules`` first, which makes it an alphabetical-collection-order
dependency and leaves this file unrunnable on its own. Loading the module
straight off disk keeps these tests honest about that purity and runnable
standalone.
"""

import importlib.util
import os
import re

_REFERENCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
    "reference.py",
)
_spec = importlib.util.spec_from_file_location("athena_reference", _REFERENCE_PATH)
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)

# Greffes the MJQ publishes no civic address for: the four itinerant circuit
# greffes, plus two absent from the July 2026 extraction.
GREFFES_WITHOUT_ADDRESS = {"525", "614", "635", "640", "652", "715"}

# A published courthouse that no greffe number in _GREFFES names. Kept in the
# table unreferenced rather than guessed onto a Nunavik circuit greffe.
ORPHAN_LOCATIONS = {"kuujjuaq"}


# ── Table shape ───────────────────────────────────────────────────────


def test_location_counts_match_the_mjq_source():
    """43 palais de justice + 8 points de service de justice."""
    by_type: dict[str, int] = {}
    for palais in reference._PALAIS.values():
        by_type[palais["location_type"]] = by_type.get(palais["location_type"], 0) + 1
    assert by_type == {"palais": 43, "point_de_service": 8}


def test_every_location_has_a_complete_address():
    postal = re.compile(r"^[A-Z]\d[A-Z] \d[A-Z]\d$")
    for key, palais in reference._PALAIS.items():
        assert palais["name"], key
        assert palais["street"], key
        assert palais["city"], key
        assert palais["location_type"] in ("palais", "point_de_service"), key
        # "A1A 1A1" — the app-wide normalized form.
        assert postal.match(palais["postal_code"]), f"{key}: {palais['postal_code']}"
        # Full names, matching the `parties` address convention.
        assert palais["province"] == "Québec", key
        assert palais["country"] == "Canada", key


def test_only_two_locations_carry_a_distinct_mailing_address():
    with_mail = {k for k, v in reference._PALAIS.items() if v["mailing_address"]}
    assert with_mail == {"perce", "forestville"}
    assert reference._PALAIS["perce"]["mailing_address"].startswith("Case postale 188")


# ── Greffe → location wiring ──────────────────────────────────────────


def test_every_greffe_palais_key_resolves():
    for number, greffe in reference._GREFFES.items():
        key = greffe["palais_key"]
        assert "palais_key" in greffe, number
        if key is not None:
            assert key in reference._PALAIS, f"greffe {number} -> {key!r}"


def test_greffes_without_an_address_are_the_known_set():
    """Guards against a future greffe silently losing its address."""
    missing = {n for n, g in reference._GREFFES.items() if not g["palais_key"]}
    assert missing == GREFFES_WITHOUT_ADDRESS


def test_no_two_greffes_claim_the_same_location():
    """A duplicate would mean a bad name match, not a real shared building."""
    seen: dict[str, str] = {}
    for number, greffe in reference._GREFFES.items():
        key = greffe["palais_key"]
        if key:
            assert key not in seen, f"greffes {seen[key]} and {number} share {key!r}"
            seen[key] = number


def test_orphan_locations_are_the_known_set():
    referenced = {
        g["palais_key"] for g in reference._GREFFES.values() if g["palais_key"]
    }
    assert set(reference._PALAIS) - referenced == ORPHAN_LOCATIONS


# ── Lookup helpers ────────────────────────────────────────────────────


def test_get_greffe_address_resolves_through_the_greffe_number():
    palais = reference.get_greffe_address("500")
    assert palais["name"] == "Montréal"
    assert palais["street"] == "1, rue Notre-Dame Est"
    assert palais["palais_key"] == "montreal"


def test_get_greffe_address_is_none_for_an_itinerant_greffe():
    assert reference.get_greffe_address("614") is None


def test_get_greffe_address_is_none_for_an_unknown_greffe():
    assert reference.get_greffe_address("999") is None


def test_get_palais_returns_a_copy():
    """Callers must not be able to corrupt the shared table."""
    reference.get_palais("montreal")["street"] = "CORRUPTED"
    assert reference._PALAIS["montreal"]["street"] == "1, rue Notre-Dame Est"


def test_get_palais_is_none_for_an_unknown_key():
    assert reference.get_palais("nope") is None


def test_list_palais_filters_by_location_type():
    names = [p["name"] for p in reference.list_palais("point_de_service")]
    assert names == [
        "Amqui", "Carleton-sur-Mer", "Dolbeau-Mistassini", "Forestville",
        "Gaspé", "La Sarre", "Matane", "Sainte-Anne-des-Monts",
    ]
    assert len(reference.list_palais()) == 51


# ── Address formatting ────────────────────────────────────────────────


def test_format_palais_address_single_line():
    palais = reference.get_greffe_address("500")
    assert reference.format_palais_address(palais) == (
        "1, rue Notre-Dame Est, Montréal (Québec) H2Y 1B6"
    )


def test_format_palais_address_multiline_breaks_before_the_city():
    palais = reference.get_greffe_address("500")
    assert reference.format_palais_address(palais, multiline=True) == (
        "1, rue Notre-Dame Est\nMontréal (Québec) H2Y 1B6"
    )


def test_format_palais_address_includes_the_unit():
    # Chicoutimi: a unit, and a city that differs from the courthouse name.
    palais = reference.get_greffe_address("150")
    assert reference.format_palais_address(palais) == (
        "227, rue Racine Est, 1er étage, Saguenay (Québec) G7H 7B4"
    )


def test_format_palais_address_tolerates_none():
    assert reference.format_palais_address(None) == ""


# ── Existing contract ─────────────────────────────────────────────────


def test_parse_court_file_number_still_resolves():
    """The address columns must not disturb the parser."""
    result = reference.parse_court_file_number("500-05-123456-241")
    assert result["parse_error"] is None
    assert result["greffe"]["palais_de_justice"] == "Montréal"
    assert result["juridiction"]["tribunal"] == "Cour supérieure"
