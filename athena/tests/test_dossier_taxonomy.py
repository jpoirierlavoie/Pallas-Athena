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


# ── _apply_prescription_deadline: behaviors preserved (spec § 6.4) ────
# The July 2026 échéancier rework routed the computation through
# compute_echeances; these pin that every pre-existing behavior survived.

from datetime import datetime, timezone

from utils.recours import compute_date_pour_agir


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_deadline_imprescriptible_clears_the_date():
    doc = {"prescription_type": "imprescriptible",
           "prescription_date": _dt(2027, 1, 1)}
    dossier._apply_prescription_deadline(doc)
    assert doc["prescription_date"] is None


def test_deadline_unset_or_autre_never_overwrites_a_manual_date():
    for p_type in ("", "autre"):
        doc = {"prescription_type": p_type,
               "droit_action_date": _dt(2026, 7, 18),
               "prescription_date": _dt(2027, 1, 1)}
        dossier._apply_prescription_deadline(doc)
        assert doc["prescription_date"] == _dt(2027, 1, 1), p_type


def test_deadline_computes_for_a_classified_action():
    doc = {"action": "REC-01", "prescription_type": "3_ans",
           "droit_action_date": _dt(2026, 7, 18)}
    dossier._apply_prescription_deadline(doc)
    assert doc["prescription_date"] == compute_date_pour_agir(
        _dt(2026, 7, 18), "3_ans"
    )


def test_deadline_computes_for_an_unclassified_dossier():
    """No action code — the pre-rework behavior verbatim."""
    doc = {"prescription_type": "3_ans", "droit_action_date": _dt(2026, 7, 18)}
    dossier._apply_prescription_deadline(doc)
    assert doc["prescription_date"] == compute_date_pour_agir(
        _dt(2026, 7, 18), "3_ans"
    )


def test_deadline_manual_period_still_computes_on_special_regimes():
    """FAM-07 (I,PE) and IMM-06 (PA) produce no dated principale on their
    own, but a lawyer-confirmed period must keep computing exactly as before
    (the compute_date_pour_agir fallback)."""
    for action in ("FAM-07", "IMM-06"):
        doc = {"action": action, "prescription_type": "10_ans",
               "droit_action_date": _dt(2026, 7, 18)}
        dossier._apply_prescription_deadline(doc)
        assert doc["prescription_date"] == compute_date_pour_agir(
            _dt(2026, 7, 18), "10_ans"
        ), action


def test_deadline_never_touches_date_avis():
    doc = {"action": "TRN-01", "prescription_type": "3_ans",
           "droit_action_date": _dt(2026, 7, 18),
           "date_avis": _dt(2026, 8, 1)}
    dossier._apply_prescription_deadline(doc)
    assert doc["date_avis"] == _dt(2026, 8, 1)


def test_date_avis_defaults_to_none():
    assert dossier._default_doc()["date_avis"] is None


# ── mandate_type vocabulary rework (July 2026) ────────────────────────


def test_retired_mandate_types_migrate_on_read():
    """The vocabulary became judiciaire/service_conseils/general/special.
    A dossier carrying a retired key must read as a current one, or editing
    it trips _validate's mandate_type check (the mediation_arbitrage lesson)."""
    for old, expected in (
        ("consultation", "service_conseils"),
        ("transactionnel", "special"),
        ("autre", "general"),
        ("mediation_arbitrage", "general"),
    ):
        assert _read({"mandate_type": old})["mandate_type"] == expected


def test_current_mandate_types_survive_the_read_unchanged():
    for key in dossier.VALID_MANDATE_TYPES:
        assert _read({"mandate_type": key})["mandate_type"] == key


def test_mandate_labels_cover_exactly_the_valid_keys():
    """The form dropdown is driven by MANDATE_TYPE_LABELS; a key without a
    label would render blank, a label without a key is dead."""
    assert set(dossier.MANDATE_TYPE_LABELS) == set(dossier.VALID_MANDATE_TYPES)


def test_template_fields_mandate_label_mirror_stays_in_sync():
    """utils/template_fields mirrors MANDATE_TYPE_LABELS by hand (it must
    stay importable without the Firestore client)."""
    from utils.template_fields import _MANDATE_TYPE_LABEL

    assert _MANDATE_TYPE_LABEL == dossier.MANDATE_TYPE_LABELS


# ── Per-party roles + avocat (July 2026 rework) ───────────────────────


def test_party_entries_are_normalized_on_read():
    doc = _read({"clients": [{"id": "p1", "name": "Jean"}],
                 "opposing_parties": [{"id": "p2", "name": "Paul"}]})
    for entry in doc["clients"] + doc["opposing_parties"]:
        assert entry["roles"] == []
        assert entry["avocat_id"] == ""
        assert entry["avocat_name"] == ""


def test_legacy_global_role_seeds_the_first_client():
    """Pre-rework dossiers carried ONE dossier-level role describing the
    clients' side. It must become the first client's per-party role, or
    every existing dossier silently loses its role at the first edit."""
    doc = _read({
        "role": "défendeur",
        "clients": [{"id": "p1", "name": "Jean"}, {"id": "p2", "name": "Anne"}],
    })
    assert doc["clients"][0]["roles"] == ["défendeur"]
    assert doc["clients"][1]["roles"] == []


def test_seeding_never_overwrites_an_existing_per_party_role():
    doc = _read({
        "role": "défendeur",
        "clients": [{"id": "p1", "name": "Jean", "roles": ["appelant"]}],
    })
    assert doc["clients"][0]["roles"] == ["appelant"]


def test_derive_role_takes_the_first_client_that_has_one():
    data = {"clients": [
        {"id": "p1", "name": "Jean", "roles": []},
        {"id": "p2", "name": "Anne", "roles": ["intimé", "demandeur reconventionnel"]},
    ]}
    dossier._derive_role(data)
    assert data["role"] == "intimé"

    dossier._derive_role({"clients": []})  # must not raise
    empty = {"clients": [{"id": "p1", "name": "J", "roles": []}]}
    dossier._derive_role(empty)
    assert empty["role"] == ""


def test_seeded_dossier_derives_back_to_its_old_role():
    """Round-trip stability: read (seeds) then save (derives) must keep the
    legacy role byte-identical — otherwise every touch of an old dossier
    would rewrite its gabarit-visible role."""
    doc = _read({"role": "demandeur",
                 "clients": [{"id": "p1", "name": "Jean"}]})
    dossier._derive_role(doc)
    assert doc["role"] == "demandeur"


def test_rebuild_party_mirrors_includes_avocat_ids():
    data = {
        "clients": [{"id": "p1", "name": "J", "roles": [],
                     "avocat_id": "av1", "avocat_name": "Roy"}],
        "opposing_parties": [{"id": "p2", "name": "P", "roles": [],
                              "avocat_id": "av2", "avocat_name": "Côté"},
                             {"id": "p3", "name": "Q", "roles": [],
                              "avocat_id": "av1", "avocat_name": "Roy"}],
    }
    dossier._rebuild_party_mirrors(data)
    assert data["client_ids"] == ["p1"]
    assert data["opposing_party_ids"] == ["p2", "p3"]
    assert data["avocat_ids"] == ["av1", "av2"]     # deduped, sorted


def test_validate_rejects_junk_party_role_and_self_lawyer():
    base = {"title": "T", "file_number": "1", "status": "actif",
            "prescription_type": ""}
    bad_role = dict(base, clients=[
        {"id": "p1", "name": "J", "roles": ["capitaine"]}])
    assert any("Rôle" in e for e in dossier._validate(bad_role))

    self_lawyer = dict(base, clients=[
        {"id": "p1", "name": "J", "roles": [], "avocat_id": "p1"}])
    assert any("propre avocat" in e for e in dossier._validate(self_lawyer))


def test_party_role_labels_cover_exactly_the_vocabulary():
    assert set(dossier.PARTY_ROLE_LABELS) == set(dossier.PARTY_ROLES)


def test_template_fields_role_maps_track_the_vocabulary():
    """Gabarit mirrors ({{dossier.role_label}} / role_feminin). A role
    without a feminine form leaves the placeholder unresolved — only
    « autre » is deliberately uninflected."""
    from utils.template_fields import _ROLE_FEMININ, _ROLE_LABEL

    assert _ROLE_LABEL == dossier.PARTY_ROLE_LABELS
    assert set(_ROLE_FEMININ) == set(dossier.PARTY_ROLES) - {"autre"}
