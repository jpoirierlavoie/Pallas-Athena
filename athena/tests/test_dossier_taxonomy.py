"""Tests for the matter_type/objet → domaine/action migration in models/dossier.py.

Importing ``models.dossier`` pulls in ``models/__init__``, which constructs a
Firestore client, so this runs in the Cloud Build deploy-gate install (same
constraint as test_folders.py). No Firestore call is made — the migration and
validation helpers under test are pure dict functions.

These guard a ONE-WAY data change: the legacy keys are popped on read and
purged by the next full-document set(), so a migration that drops something
drops it for good.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models.dossier as dossier


def _read(doc: dict) -> dict:
    """Reproduce exactly what get_dossier does to a raw Firestore dict."""
    return dossier._strip_removed_fields(dossier._migrate_parties(doc))


# ── matter_type → domaine ─────────────────────────────────────────────


def test_unambiguous_matter_types_map_to_their_domaine():
    for matter_type, expected in (
        ("recouvrement", "REC"),
        ("injonction", "INJ"),
        ("recours_extraordinaire", "CJP"),
        ("vice_cache", "CON"),
    ):
        out = _read({"matter_type": matter_type})
        assert out["domaine"] == expected, matter_type


def test_action_dommages_is_left_unclassified_rather_than_guessed():
    """Damages can be contractual (CON) or extracontractual (RCV). Guessing
    would silently mislabel the file's whole liability regime."""
    assert _read({"matter_type": "action_dommages"})["domaine"] == ""


def test_autre_and_legacy_subject_matter_keys_are_left_unclassified():
    for matter_type in ("autre", "litige_civil", "litige_commercial", "familial"):
        assert _read({"matter_type": matter_type})["domaine"] == "", matter_type


def test_an_unknown_matter_type_does_not_invent_a_domaine():
    out = _read({"matter_type": "quelque_chose_dautre"})
    assert out.get("domaine", "") == ""


def test_migration_never_overwrites_a_domaine_already_set():
    """A taxonomy-era dossier that still carries a stale matter_type must not
    be reclassified by it."""
    out = _read({"domaine": "TRV", "matter_type": "recouvrement"})
    assert out["domaine"] == "TRV"


# ── objet → action_precision ──────────────────────────────────────────


def test_legacy_objet_text_is_preserved_as_the_precision():
    """objet was free text and cannot map onto an action code, so it is kept
    rather than discarded."""
    out = _read({"objet": "vente de matériel, factures 2024-03"})
    assert out["action_precision"] == "vente de matériel, factures 2024-03"


def test_objet_never_overwrites_a_precision_already_set():
    out = _read({"objet": "ancien texte", "action_precision": "texte courant"})
    assert out["action_precision"] == "texte courant"


def test_an_empty_objet_does_not_create_a_precision():
    assert _read({"objet": ""}).get("action_precision", "") == ""


# ── Purge-on-save ─────────────────────────────────────────────────────


def test_the_legacy_keys_are_popped_so_the_next_save_purges_them():
    out = _read({"matter_type": "recouvrement", "objet": "des factures"})
    assert "matter_type" not in out
    assert "objet" not in out


def test_migration_reads_the_legacy_keys_before_they_are_stripped():
    """ORDERING IS LOAD-BEARING. get_dossier nests the calls as
    _strip_removed_fields(_migrate_parties(...)); reverse them and the legacy
    data is destroyed unread. This pins the composition, not the nesting."""
    out = _read({"matter_type": "injonction", "objet": "cesser les travaux"})
    assert out["domaine"] == "INJ"
    assert out["action_precision"] == "cesser les travaux"
    assert "matter_type" not in out and "objet" not in out


def test_a_dossier_with_neither_legacy_key_is_untouched():
    out = _read({"domaine": "REC", "action": "REC-01"})
    assert out["domaine"] == "REC"
    assert out["action"] == "REC-01"


# ── Validation ────────────────────────────────────────────────────────


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


def test_a_matching_domaine_action_pair_validates():
    assert dossier._validate(_valid(domaine="REC", action="REC-01")) == []


def test_an_action_from_another_domaine_is_rejected():
    """The cascading picker cannot produce this, but a hand-crafted POST can."""
    errors = dossier._validate(_valid(domaine="REC", action="TRV-01"))
    assert any("n'appartient pas au domaine" in e for e in errors)


def test_an_unknown_domaine_or_action_is_rejected():
    assert any("Domaine invalide" in e for e in dossier._validate(_valid(domaine="ZZZ")))
    assert any("Action invalide" in e for e in dossier._validate(_valid(action="ZZZ-01")))


def test_the_unset_state_validates():
    """A dossier need not be classified."""
    assert dossier._validate(_valid(domaine="", action="")) == []


def test_a_domaine_without_an_action_validates():
    """Partial classification is allowed — the domaine narrows, the action refines."""
    assert dossier._validate(_valid(domaine="REC", action="")) == []


def test_a_legacy_dossier_lacking_the_fields_entirely_still_validates():
    """domaine/action are presence-gated like mandate_type: an unconditional
    check would lock every legacy dossier out of editing."""
    assert dossier._validate(_valid()) == []


def test_removed_fields_covers_the_legacy_taxonomy_keys():
    assert "matter_type" in dossier._REMOVED_FIELDS
    assert "objet" in dossier._REMOVED_FIELDS


def test_new_dossiers_are_born_unclassified():
    """The old matter_type defaulted to "action_dommages", silently classifying
    every new dossier as an unrelated recourse."""
    defaults = dossier._default_doc()
    assert defaults["domaine"] == ""
    assert defaults["action"] == ""
    assert defaults["action_precision"] == ""
    assert "matter_type" not in defaults
    assert "objet" not in defaults
