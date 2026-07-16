"""Tests for the forum feature: dossier validation + the server-side forum
reconciliation (`normalize_forum`).

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
from models.dossier import normalize_forum


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


def test_forum_type_autre_with_a_known_forum_validates():
    assert dossier._validate(_valid(forum_type="autre", forum="taq")) == []


def test_forum_type_autre_without_a_forum_is_rejected():
    errors = dossier._validate(_valid(forum_type="autre", forum=""))
    assert any("sélectionner le tribunal" in e for e in errors)


def test_forum_type_autre_with_an_unknown_forum_is_rejected():
    errors = dossier._validate(_valid(forum_type="autre", forum="bogus"))
    assert any("sélectionner le tribunal" in e for e in errors)


def test_an_invalid_forum_type_is_rejected():
    errors = dossier._validate(_valid(forum_type="quelconque", forum=""))
    assert any("Type de forum" in e for e in errors)


def test_forum_is_presence_gated_for_legacy_dossiers():
    """A dossier predating the field (no forum_type key) must stay editable."""
    assert dossier._validate(_valid()) == []


def test_new_dossiers_default_to_a_judicial_forum():
    defaults = dossier._default_doc()
    assert defaults["forum_type"] == "judiciaire"
    assert defaults["forum"] == ""


# ── Route: server-side forum resolution ───────────────────────────────


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


def test_autre_admin_tribunal_sets_tribunal_and_clears_judicial_fields():
    data = {
        "forum_type": "autre", "forum": "taq",
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


def test_autre_federal_court_is_not_flagged_administrative():
    data = {"forum_type": "autre", "forum": "cour_federale",
            "tribunal": "", "is_administrative_tribunal": True}
    normalize_forum(data)
    assert data["tribunal"] == "Cour fédérale"
    assert data["is_administrative_tribunal"] is False


def test_autre_with_a_bad_slug_does_not_wipe_the_judicial_fields():
    """The submission will fail _validate; don't destroy data pre-validation."""
    data = {"forum_type": "autre", "forum": "bogus",
            "tribunal": "Cour supérieure", "greffe_number": "500"}
    normalize_forum(data)
    assert data["tribunal"] == "Cour supérieure"
    assert data["greffe_number"] == "500"


def test_the_court_file_number_is_never_touched_by_resolution():
    """The whole point: an « autre » file number is stored verbatim, unparsed."""
    data = {"forum_type": "autre", "forum": "tal",
            "court_file_number": "31 000000 000 G"}
    normalize_forum(data)
    assert data["court_file_number"] == "31 000000 000 G"
