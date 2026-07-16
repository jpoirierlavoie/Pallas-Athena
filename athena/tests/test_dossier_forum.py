"""Tests for the four-way forum feature: dossier validation, the server-side
forum reconciliation (`normalize_forum`), and the legacy "autre" migration.

Importing ``models.dossier`` pulls in ``models/__init__`` (Firestore client),
so this runs in the Cloud Build deploy-gate install (same constraint as
test_folders.py). No Firestore call is made — the functions under test are
pure dict operations. (`normalize_forum` lives in the model, not the route,
precisely so it can be tested without Flask's config/SECRET_KEY.)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models.dossier as dossier
from models.dossier import (
    PREJUDICIAIRE_FILE_NUMBER,
    _migrate_forum_type,
    normalize_forum,
)


# ── Model validation ──────────────────────────────────────────────────


def _valid(**over) -> dict:
    base = {
        "title": "Tremblay c. Lavoie",
        "file_number": "2026-001",
        "clients": [{"id": "p1", "name": "Jean Tremblay"}],
        "status": "actif",
        "prescription_type": "",
    }
    base.update(over)
    return base


def test_forum_type_judiciaire_validates():
    assert dossier._validate(_valid(forum_type="judiciaire", forum="")) == []


def test_forum_type_prejudiciaire_validates_without_a_forum():
    assert dossier._validate(_valid(forum_type="prejudiciaire", forum="")) == []


def test_forum_type_administratif_with_an_admin_tribunal_validates():
    assert dossier._validate(_valid(forum_type="administratif", forum="taq")) == []


def test_forum_type_federal_with_a_federal_court_validates():
    assert dossier._validate(
        _valid(forum_type="federal", forum="cour_federale")
    ) == []


def test_forum_type_administratif_without_a_forum_is_rejected():
    errors = dossier._validate(_valid(forum_type="administratif", forum=""))
    assert any("sélectionner le tribunal" in e for e in errors)


def test_cross_category_forum_is_rejected():
    """A federal court under « administratif » (or vice versa) — only a
    hand-crafted POST can produce it, but it must not slip through."""
    errors = dossier._validate(
        _valid(forum_type="administratif", forum="cour_federale")
    )
    assert any("sélectionner le tribunal" in e for e in errors)
    errors = dossier._validate(_valid(forum_type="federal", forum="taq"))
    assert any("sélectionner le tribunal" in e for e in errors)


def test_an_invalid_forum_type_is_rejected():
    errors = dossier._validate(_valid(forum_type="quelconque", forum=""))
    assert any("Type de forum" in e for e in errors)


def test_retired_autre_is_no_longer_a_valid_submission():
    """"autre" is migrated on read, never accepted on write."""
    errors = dossier._validate(_valid(forum_type="autre", forum="taq"))
    assert any("Type de forum" in e for e in errors)


def test_forum_is_presence_gated_for_legacy_dossiers():
    """A dossier predating the field (no forum_type key) must stay editable."""
    assert dossier._validate(_valid()) == []


def test_new_dossiers_default_to_a_judicial_forum():
    defaults = dossier._default_doc()
    assert defaults["forum_type"] == "judiciaire"
    assert defaults["forum"] == ""


# ── Server-side forum resolution ──────────────────────────────────────


def test_judiciaire_clears_the_forum_and_keeps_parsed_metadata():
    data = {
        "forum_type": "judiciaire", "forum": "taq",  # stale slug
        "tribunal": "Cour supérieure", "competence": "Division générale",
        "district_judiciaire": "Montréal", "greffe_number": "500",
    }
    normalize_forum(data)
    assert data["forum"] == ""
    # The parsed judicial metadata is left untouched.
    assert data["tribunal"] == "Cour supérieure"
    assert data["greffe_number"] == "500"


def test_admin_tribunal_sets_tribunal_and_clears_judicial_fields():
    data = {
        "forum_type": "administratif", "forum": "taq",
        "tribunal": "Cour supérieure",  # stale JS value
        "competence": "Division générale", "district_judiciaire": "Montréal",
        "palais_de_justice": "Montréal", "greffe_number": "500",
        "juridiction_number": "05", "is_administrative_tribunal": False,
    }
    normalize_forum(data)
    assert data["tribunal"] == "Tribunal administratif du Québec"
    assert data["competence"] == ""
    assert data["district_judiciaire"] == ""
    assert data["palais_de_justice"] == ""
    assert data["greffe_number"] == ""
    assert data["juridiction_number"] == ""
    assert data["is_administrative_tribunal"] is True


def test_federal_court_is_not_flagged_administrative():
    data = {"forum_type": "federal", "forum": "cour_federale",
            "tribunal": "", "is_administrative_tribunal": True}
    normalize_forum(data)
    assert data["tribunal"] == "Cour fédérale"
    assert data["is_administrative_tribunal"] is False


def test_a_bad_or_cross_category_slug_does_not_wipe_the_judicial_fields():
    """The submission will fail _validate; don't destroy data pre-validation."""
    for slug in ("bogus", "cour_federale"):  # unknown, wrong category
        data = {"forum_type": "administratif", "forum": slug,
                "tribunal": "Cour supérieure", "greffe_number": "500"}
        normalize_forum(data)
        assert data["tribunal"] == "Cour supérieure"
        assert data["greffe_number"] == "500"


def test_the_court_file_number_is_stored_verbatim_for_admin_and_federal():
    data = {"forum_type": "administratif", "forum": "tal",
            "court_file_number": "31 000000 000 G"}
    normalize_forum(data)
    assert data["court_file_number"] == "31 000000 000 G"


def test_prejudiciaire_forces_the_placeholder_number_and_keeps_the_district():
    data = {
        "forum_type": "prejudiciaire", "forum": "taq",  # stale slug
        "court_file_number": "500-05-123456-241",       # stale number
        "tribunal": "Cour supérieure", "competence": "Division générale",
        "district_judiciaire": "Terrebonne",            # user-entered — kept
        "palais_de_justice": "Montréal", "greffe_number": "500",
        "juridiction_number": "05", "is_administrative_tribunal": True,
    }
    normalize_forum(data)
    assert data["court_file_number"] == PREJUDICIAIRE_FILE_NUMBER
    assert data["district_judiciaire"] == "Terrebonne"
    assert data["forum"] == ""
    assert data["tribunal"] == ""
    assert data["competence"] == ""
    assert data["palais_de_justice"] == ""
    assert data["greffe_number"] == ""
    assert data["juridiction_number"] == ""
    assert data["is_administrative_tribunal"] is False


# ── Legacy "autre" migration (read path) ──────────────────────────────


def test_migrate_autre_with_an_admin_slug_becomes_administratif():
    doc = {"forum_type": "autre", "forum": "taq"}
    _migrate_forum_type(doc)
    assert doc["forum_type"] == "administratif"
    assert doc["forum"] == "taq"


def test_migrate_autre_with_a_federal_slug_becomes_federal():
    doc = {"forum_type": "autre", "forum": "cour_supreme_canada"}
    _migrate_forum_type(doc)
    assert doc["forum_type"] == "federal"


def test_migrate_autre_with_a_dangling_slug_falls_back_to_judiciaire():
    doc = {"forum_type": "autre", "forum": "gone", "tribunal": "Ancien forum"}
    _migrate_forum_type(doc)
    assert doc["forum_type"] == "judiciaire"
    assert doc["forum"] == ""
    # The tribunal name written at save time survives as plain text.
    assert doc["tribunal"] == "Ancien forum"


def test_migrate_leaves_current_values_alone():
    for ftype in ("judiciaire", "administratif", "federal", "prejudiciaire"):
        doc = {"forum_type": ftype, "forum": "taq"}
        _migrate_forum_type(doc)
        assert doc["forum_type"] == ftype
