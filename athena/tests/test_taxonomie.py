"""Tests for utils/taxonomie.py — the Québec action taxonomy.

This is legal reference data a limitation deadline is suggested from, so the
structural invariants are asserted rather than eyeballed. The table was
generated from the source document and verified row-by-row against it; these
tests guard the properties a later hand-edit could silently break.

``utils/taxonomie.py`` is pure (typing + functools only), so this runs
standalone — no Firestore, no import-order dependency.
"""

import re

from utils import recours, taxonomie

# § 4 « Rappels sur les déchéances » — the source's own authoritative list.
# Held here as well as in the generator because it is a CROSS-SECTION claim:
# a row can state its déchéance in prose and never carry a "(D)" marker, which
# is exactly how APP-01 slipped through the per-row transcription.
DECHEANCE_S4 = {
    "GAG-02", "IMM-04", "IMM-07", "IMM-09", "CST-04", "SUC-07",
    "DEC-03", "EXE-04", "APP-01", "COR-11", "TRN-05", "TRV-01",
}

# The source's 20 families, in its own order.
EXPECTED_DOMAINES = [
    "REC", "CON", "RCV", "RES", "GAG", "IMM", "CST", "COR", "HYP", "FAI",
    "FAM", "SUC", "DEC", "CJP", "INJ", "EXE", "TRN", "ADM", "TRV", "APP",
]


# ── Table shape ───────────────────────────────────────────────────────


def test_the_twenty_domaines_are_present_in_source_order():
    """Order is the dropdown order — not incidental."""
    assert list(taxonomie.DOMAINES) == EXPECTED_DOMAINES


def test_every_action_code_is_prefixed_by_its_domaine():
    """domaine_of() derives the relationship from the prefix, so the prefix
    IS the relationship — a mismatch would break _validate's pair check."""
    for code, domaine in taxonomie.DOMAINES.items():
        for action in domaine.actions:
            assert action.code.startswith(f"{code}-"), action.code
            assert taxonomie.domaine_of(action.code) == code


def test_action_codes_are_globally_unique_and_well_formed():
    seen = set()
    for action in taxonomie.ACTIONS.values():
        assert re.fullmatch(r"[A-Z]{3}-\d{2}", action.code), action.code
        assert action.code not in seen
        seen.add(action.code)


def test_every_domaine_ends_with_an_autre_preciser_row():
    """The source guarantees a catch-all per family, so no file is unclassifiable."""
    for code, domaine in taxonomie.DOMAINES.items():
        assert domaine.actions[-1].code == f"{code}-99", code
        assert domaine.actions[-1].libelle == "Autre (préciser)"


def test_every_action_has_a_libelle():
    for action in taxonomie.ACTIONS.values():
        assert action.libelle.strip(), action.code


# ── The delay contract ────────────────────────────────────────────────


def test_delai_types_are_tuples_from_the_closed_vocabulary():
    """§ 8 (2) — every token of every action ∈ the 11-token § 4 vocabulary."""
    for action in taxonomie.ACTIONS.values():
        assert isinstance(action.delai_types, tuple), action.code
        for token in action.delai_types:
            assert token in taxonomie.VALID_DELAI_TYPES, (
                f"{action.code}: {token!r}"
            )
            assert token in taxonomie.DELAI_TYPE_LABELS, action.code


def test_autre_rows_carry_nothing():
    """§ 8 (4) — the -99 rows have no types, no avis, no flags, no refs."""
    for action in taxonomie.ACTIONS.values():
        if action.code.endswith("-99"):
            assert action.delai_types == (), action.code
            assert action.avis == (), action.code
            assert action.a_valider is False, action.code
            assert action.ref_delai == "", action.code
            assert action.ref_fondement == "", action.code


def test_every_suggested_period_exists_in_the_recours_table():
    """A suggestion naming a key PRESCRIPTION_PERIODS lacks would silently
    fail to prefill, and would fail _validate if it ever reached the form."""
    for action in taxonomie.ACTIONS.values():
        assert action.prescription_type in recours.VALID_PRESCRIPTION_TYPES, (
            f"{action.code}: {action.prescription_type!r}"
        )


def test_an_autre_preciser_row_never_suggests_a_delay():
    """It has no delay of its own — suggesting one would invent law."""
    for action in taxonomie.ACTIONS.values():
        if action.code.endswith("-99"):
            assert action.prescription_type == "", action.code
            assert action.delai == "", action.code


def test_no_period_is_suggested_where_the_delay_is_not_a_single_period():
    """These rows' delays are regime-dependent, merely « raisonnable », or
    retrospective. A suggestion would compute a deadline that means nothing.
    """
    for code in (
        "RCV-05",  # 1 an general vs. 3 mois média — different regimes
        "COR-06",  # QC 3 ans (P) vs. féd. 2 ans (D)
        "CJP-01", "CJP-02", "CJP-03",  # « délai raisonnable »
        "IMM-05",  # « Variable »
        "GAG-01",  # follows the debtor's own right
        "FAI-01",  # 6 months RETROSPECTIVE eligibility, not a running delay
    ):
        assert taxonomie.ACTIONS[code].prescription_type == "", code


def test_a_compound_delay_suggests_the_action_period_not_the_notice():
    """CON-07 « 3 ans + avis »: the avis is a trap, the ACTION is 3 ans.
    TRN-01 leads with the avis; the action is still 3 ans."""
    assert taxonomie.ACTIONS["CON-07"].prescription_type == "3_ans"
    assert "PE" in taxonomie.ACTIONS["CON-07"].delai_types
    assert "A" in taxonomie.ACTIONS["CON-07"].delai_types
    assert taxonomie.ACTIONS["TRN-01"].prescription_type == "3_ans"
    # CST-01: « Garantie 5 ans (couverture) + action 3 ans (P) » — the
    # guarantee is coverage, not the limitation period.
    assert taxonomie.ACTIONS["CST-01"].prescription_type == "3_ans"


def test_section_4_decheances_all_carry_D():
    """The cross-section check that caught APP-01, whose own cell says
    « déchéance » in prose and never carries a (D) marker."""
    for code in DECHEANCE_S4:
        assert code in taxonomie.ACTIONS, code
        assert taxonomie.niveau_decheance(code) == "stricte", (
            f"§4 lists {code} as déchéance, delai_types is "
            f"{taxonomie.ACTIONS[code].delai_types!r}"
        )


def test_niveau_decheance_levels():
    assert taxonomie.niveau_decheance("GAG-02") == "stricte"     # (D,)
    assert taxonomie.niveau_decheance("REC-01") is None          # (PE,)
    assert taxonomie.niveau_decheance("CON-07") is None          # (PE, A)
    assert taxonomie.niveau_decheance("") is None
    assert taxonomie.niveau_decheance("ZZZ-01") is None


def test_is_decheance_is_a_deprecated_alias():
    for code in taxonomie.ACTIONS:
        assert taxonomie.is_decheance(code) == (
            taxonomie.niveau_decheance(code) is not None
        ), code


def test_is_decheance_matches_whole_tokens():
    """"D" is a whole token, never a substring of another token."""
    assert taxonomie.is_decheance("COR-11")  # (D, A)
    assert taxonomie.is_decheance("COR-06")  # (PE, D)
    assert not taxonomie.is_decheance("TRN-04") or (
        taxonomie.niveau_decheance("TRN-04") == "relevable"
    )  # DR must not read as D (stage 1: still (), stage 2: relevable)


# ── Lookups ───────────────────────────────────────────────────────────


def test_action_label_is_libelle_plus_bracketed_code():
    assert taxonomie.action_label("REC-01") == "Action sur compte [REC-01]"


def test_lookups_tolerate_unknown_and_empty_codes():
    assert taxonomie.get_action("ZZZ-99") is None
    assert taxonomie.get_action("") is None
    assert taxonomie.get_domaine("ZZZ") is None
    assert taxonomie.action_label("ZZZ-99") == ""
    assert taxonomie.action_label("") == ""
    assert taxonomie.domaine_of("ZZZ-99") == ""
    assert taxonomie.actions_for("ZZZ") == ()
    assert taxonomie.actions_for("") == ()


def test_valid_vocabularies_include_the_unset_state():
    """A dossier need not be classified — "" must pass _validate."""
    assert "" in taxonomie.VALID_DOMAINES
    assert "" in taxonomie.VALID_ACTIONS
    assert "REC" in taxonomie.VALID_DOMAINES
    assert "REC-01" in taxonomie.VALID_ACTIONS
    assert taxonomie.DOMAINE_LABELS[""] == "Non défini"


def test_actions_for_returns_source_order():
    codes = [a.code for a in taxonomie.actions_for("REC")]
    assert codes == ["REC-01", "REC-02", "REC-03", "REC-04", "REC-05",
                     "REC-06", "REC-99"]


def test_requires_precision_flags_only_the_catch_all_rows():
    assert taxonomie.requires_precision("REC-99")
    assert not taxonomie.requires_precision("REC-01")
    assert not taxonomie.requires_precision("")


# ── Form payload ──────────────────────────────────────────────────────


def test_form_payload_covers_every_domaine_and_action():
    payload = taxonomie.form_payload()
    assert set(payload) == set(taxonomie.DOMAINES)
    total = sum(len(d["actions"]) for d in payload.values())
    assert total == len(taxonomie.ACTIONS)


def test_form_payload_carries_the_guidance_the_form_shows():
    # Verifies form_payload copies each field through faithfully — asserted
    # against the Action's own values, not hardcoded prose, so an editorial
    # rewording of a delai/point_depart cell does not break the plumbing test.
    payload = taxonomie.form_payload()
    rec01 = payload["REC"]["actions"][0]
    src = taxonomie.ACTIONS["REC-01"]
    assert rec01["code"] == "REC-01"
    assert rec01["label"] == taxonomie.action_label("REC-01")
    assert rec01["delai"] == src.delai
    assert rec01["delai_types"] == list(src.delai_types)
    assert rec01["delai_types_label"] == taxonomie.delai_types_label("REC-01")
    assert rec01["a_valider"] == src.a_valider
    assert rec01["niveau_decheance"] == taxonomie.niveau_decheance("REC-01")
    assert rec01["point_depart"] == src.point_depart
    assert rec01["ref_delai"] == src.ref_delai
    assert rec01["ref_fondement"] == src.ref_fondement
    assert rec01["avis"] == [
        {
            "libelle": v.libelle,
            "delai": taxonomie.avis_delai_display(v.delai_key),
            "delai_key": v.delai_key,
            "point_depart": v.point_depart,
            "reference": v.reference,
            "sanction": v.sanction,
            "conditionnel": v.conditionnel,
        }
        for v in src.avis
    ]
    assert rec01["prescription_type"] == src.prescription_type == "3_ans"


def test_form_payload_schema_snapshot():
    """§ 8 (16) — the payload is JSON-serializable and its per-action key set
    is pinned: it is the form JS's API contract."""
    import json

    payload = taxonomie.form_payload()
    json.dumps(payload)  # must not raise
    expected_keys = {
        "code", "label", "delai", "delai_types", "delai_types_label",
        "a_valider", "niveau_decheance", "point_depart", "ref_delai",
        "ref_fondement", "avis", "prescription_type",
    }
    for domaine in payload.values():
        for action in domaine["actions"]:
            assert set(action) == expected_keys, action["code"]


def test_form_payload_is_cached():
    """It is handed to every dossier view, not just the form."""
    assert taxonomie.form_payload() is taxonomie.form_payload()
