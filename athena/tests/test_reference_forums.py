"""Tests for the non-judicial forum table in models/reference.py.

A filing forum name goes onto legal documents, so the structural invariants
are asserted rather than eyeballed. ``models/reference.py`` is pure (imports
only ``typing``); it is loaded straight off disk so these run standalone,
without ``models/__init__``'s Firestore client or an import-order dependency.
"""

import importlib.util
import os

_REFERENCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
    "reference.py",
)
_spec = importlib.util.spec_from_file_location("athena_reference_forums", _REFERENCE_PATH)
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)

# The 16 Québec administrative tribunals (CJAQ) + 4 federal courts.
EXPECTED_ADMIN_ABBRS = {
    "TAQ", "TAT", "TAL", "TAMF", "TADP", "CAI", "CFP", "CPTAQ", "CTQ", "CMQ",
    "CQLC", "BPCD", "RE", "RACJ", "RMAAQ", "RBQ",
}
EXPECTED_FEDERAL_ABBRS = {"C.F.", "C.A.F.", "C.C.I.", "C.S.C."}


# ── Table shape ───────────────────────────────────────────────────────


def test_forum_counts_match_the_source_lists():
    admin = reference.list_forums("administratif")
    federal = reference.list_forums("federal")
    assert len(admin) == 16
    assert len(federal) == 4
    assert len(reference.list_forums()) == 20


def test_every_forum_has_the_expected_fields_and_category():
    for key, forum in reference._FORUMS.items():
        assert set(forum) == {"name", "abbr", "category"}, key
        assert forum["name"], key
        assert forum["abbr"], key
        assert forum["category"] in (reference.ADMINISTRATIF, reference.FEDERAL), key


def test_the_abbreviations_are_the_known_sets():
    admin = {f["abbr"] for f in reference.list_forums("administratif")}
    federal = {f["abbr"] for f in reference.list_forums("federal")}
    assert admin == EXPECTED_ADMIN_ABBRS
    assert federal == EXPECTED_FEDERAL_ABBRS


def test_forum_slugs_are_ascii_and_unique():
    for key in reference._FORUMS:
        assert key == key.lower(), key
        assert key.isascii(), key
        assert " " not in key, key
    assert len(set(reference._FORUMS)) == len(reference._FORUMS)


def test_the_judicial_stream_tribunals_are_deliberately_absent():
    """Tribunal des droits de la personne / des professions run through the
    Cour du Québec and are covered by the parser (juridiction 53 / 07)."""
    names = {f["name"] for f in reference.list_forums()}
    assert "Tribunal des droits de la personne" not in names
    assert "Tribunal des professions" not in names


# ── Lookup helpers ────────────────────────────────────────────────────


def test_get_forum_resolves_and_attaches_the_slug():
    forum = reference.get_forum("taq")
    assert forum["name"] == "Tribunal administratif du Québec"
    assert forum["abbr"] == "TAQ"
    assert forum["category"] == reference.ADMINISTRATIF
    assert forum["forum_key"] == "taq"


def test_get_forum_is_none_for_unknown_or_empty():
    assert reference.get_forum("nope") is None
    assert reference.get_forum("") is None


def test_get_forum_returns_a_copy():
    reference.get_forum("taq")["name"] = "CORRUPTED"
    assert reference._FORUMS["taq"]["name"] == "Tribunal administratif du Québec"


def test_forum_tribunal_name_gives_the_display_name():
    assert reference.forum_tribunal_name("cour_federale") == "Cour fédérale"
    assert reference.forum_tribunal_name("bogus") == ""


def test_list_forums_is_name_sorted():
    admin = reference.list_forums("administratif")
    assert [f["name"] for f in admin] == sorted(f["name"] for f in admin)


def test_forums_by_category_groups_in_display_order():
    groups = reference.forums_by_category()
    assert [g[0] for g in groups] == [reference.ADMINISTRATIF, reference.FEDERAL]
    assert groups[0][1] == "Tribunaux administratifs du Québec"
    assert groups[1][1] == "Cours et tribunaux fédéraux"
    assert len(groups[0][2]) == 16
    assert len(groups[1][2]) == 4


def test_a_federal_court_is_not_flagged_administrative():
    """The category drives is_administrative_tribunal downstream."""
    assert reference.get_forum("cour_federale")["category"] == reference.FEDERAL
    assert reference.get_forum("taq")["category"] == reference.ADMINISTRATIF
