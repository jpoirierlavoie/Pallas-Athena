"""Tests for the type-aware deadline orchestration (utils/recours.py § 6).

Pure — no Firestore, no Flask: ``compute_echeances`` dispatches on the
taxonomy's ``delai_types`` and composes the EXISTING date arithmetic only.
The § 8-10 identity tests are the lock on that promise: for every dated
principale the result must equal ``compute_date_pour_agir`` verbatim
(art. 52 Loi d'interprétation forward report included).
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import taxonomie
from utils.recours import (
    AVIS_PERIODS,
    PA_PERIODS,
    PRESCRIPTION_LABELS,
    PRESCRIPTION_PERIODS,
    VALID_PRESCRIPTION_TYPES,
    compute_date_pour_agir,
    compute_echeances,
)


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


STARTS = (
    _dt(2026, 7, 18),
    _dt(2025, 1, 31),   # month-end clamp
    _dt(2024, 2, 29),   # leap day
    _dt(2026, 5, 14),   # Thursday before the patriots-Monday window
)


def _principale(echeances):
    return next(e for e in echeances if e.role == "principale")


# ── § 8 (10) — strict backwards compatibility of the dated principale ────


@pytest.mark.parametrize("code,p_type", [
    ("REC-01", "3_ans"),    # (PE,)
    ("TRV-01", "45_jours"), # (D,)
    ("ADM-01", "30_jours"), # (DR,)
    ("COR-06", "3_ans"),    # (PE, D)
])
@pytest.mark.parametrize("start", STARTS)
def test_pe_d_dr_principale_identical_to_compute_date_pour_agir(code, p_type, start):
    e = _principale(compute_echeances(code, start, p_type))
    assert e.date == compute_date_pour_agir(start, p_type)
    assert e.date is not None
    assert e.libelle == "Date pour agir"


def test_unclassified_defaults_to_pe():
    """""/-99/unknown codes: the current unclassified behavior verbatim."""
    start = _dt(2026, 7, 18)
    for code in ("", "REC-99", "ZZZ-01"):
        echeances = compute_echeances(code, start, "3_ans")
        assert len(echeances) == 1
        e = echeances[0]
        assert e.role == "principale"
        assert e.niveau == "normal"
        assert e.date == compute_date_pour_agir(start, "3_ans")


def test_no_period_no_date_but_niveau_still_colors():
    """A D action with no confirmed period still flags rouge, date None."""
    e = _principale(compute_echeances("TRV-01", None, ""))
    assert e.date is None
    assert e.niveau == "rouge"


# ── § 8 (11) — niveaux ───────────────────────────────────────────────────


def test_niveaux():
    start = _dt(2026, 7, 18)
    rouge = _principale(compute_echeances("TRV-01", start, "45_jours"))
    assert rouge.niveau == "rouge"
    assert "rigueur" in rouge.note
    orange = _principale(compute_echeances("ADM-01", start, "30_jours"))
    assert orange.niveau == "orange"
    assert "106" in orange.note          # DR_RELIEF_NOTES["ADM-01"]
    normal = _principale(compute_echeances("REC-01", start, "3_ans"))
    assert normal.niveau == "normal"


def test_dr_without_specific_relief_gets_generic_note():
    # ADM-03 is (DR,) and absent from DR_RELIEF_NOTES → generic relief note.
    e = _principale(compute_echeances("ADM-03", _dt(2026, 7, 18), "90_jours"))
    assert e.niveau == "orange"
    assert "relief" in e.note or "relev" in e.note


# ── § 8 (12) — avis ──────────────────────────────────────────────────────


def test_trn01_two_conditional_avis():
    start = _dt(2026, 7, 18)
    depart_avis = _dt(2026, 3, 2)
    # Not confirmed → no avis échéance at all.
    none_confirmed = compute_echeances(
        "TRN-01", start, "3_ans", date_depart_avis=depart_avis
    )
    assert [e for e in none_confirmed if e.role == "avis"] == []
    # First scenario (bien délivré, 60 jours) confirmed.
    one = compute_echeances(
        "TRN-01", start, "3_ans",
        date_depart_avis=depart_avis, avis_confirmes=(0,),
    )
    avis = [e for e in one if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].date == compute_date_pour_agir(depart_avis, "60_jours")
    assert "irrecevabilité" in avis[0].note
    # Both scenarios confirmed → 60 jours + 9 mois.
    both = compute_echeances(
        "TRN-01", start, "3_ans",
        date_depart_avis=depart_avis, avis_confirmes=(0, 1),
    )
    avis = [e for e in both if e.role == "avis"]
    assert len(avis) == 2
    assert avis[1].date == compute_date_pour_agir(depart_avis, "9_mois")
    # The principale is never replaced by an avis échéance.
    assert _principale(both).date == compute_date_pour_agir(start, "3_ans")


def test_rcv05_avis_3_jours_ouvrables_golden():
    """Cas d'or § 8 (12): Thursday start, patriots Monday inside the window.
    Thu 2026-05-14 + 3 jours ouvrables = Fri 15, [Sat/Sun/Mon-férié skipped],
    Tue 19, Wed 20."""
    echeances = compute_echeances(
        "RCV-05", None, "",
        date_depart_avis=_dt(2026, 5, 14), avis_confirmes=(0,),
    )
    avis = [e for e in echeances if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].date == _dt(2026, 5, 20)
    assert "3_jours_ouvrables" not in PRESCRIPTION_PERIODS


def test_checklist_avis_without_computable_key():
    """CON-07's dénonciation has no fixed delay — a dateless checklist item
    carrying the point de départ, the reference and the sanction."""
    echeances = compute_echeances(
        "CON-07", _dt(2026, 7, 18), "3_ans",
        date_depart_avis=_dt(2026, 7, 18),
    )
    avis = [e for e in echeances if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].date is None
    assert avis[0].niveau == "info"
    assert "1739" in avis[0].note
    assert "fatale" in avis[0].note


def test_dated_avis_needs_its_own_start_date():
    """No date_depart_avis → even a computable conditional avis degrades to a
    checklist item (its starting point is not droit_action_date)."""
    echeances = compute_echeances(
        "TRN-01", _dt(2026, 7, 18), "3_ans", avis_confirmes=(0,),
    )
    avis = [e for e in echeances if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].date is None
    assert "2050" in avis[0].note


# ── § 8 (13) — PA defensive ──────────────────────────────────────────────


def test_pa_imm06_defensive_only():
    start = _dt(2026, 7, 18)
    echeances = compute_echeances("IMM-06", start, "")
    assert len(echeances) == 1
    e = echeances[0]
    assert e.role == "defensive"
    assert "interrompre avant" in e.libelle
    # Same period arithmetic as a 10-year prescription — identical mechanics.
    assert e.date == compute_date_pour_agir(start, "10_ans")
    assert not [x for x in echeances if x.role == "principale"]
    # § 8 (9b): the PA period comes from PA_PERIODS, never the dropdown.
    assert set(PA_PERIODS) == {"IMM-06"}
    assert taxonomie.ACTIONS["IMM-06"].prescription_type == ""


def test_pa_without_start_date_still_defensive():
    echeances = compute_echeances("IMM-06", None, "")
    assert len(echeances) == 1
    assert echeances[0].role == "defensive"
    assert echeances[0].date is None


# ── § 8 (14) — R (délai raisonnable) ─────────────────────────────────────


def test_r_no_date_by_default():
    e = _principale(compute_echeances("CJP-01", _dt(2026, 7, 18), ""))
    assert e.date is None
    assert e.niveau == "info"
    assert "raisonnable" in e.libelle or "raisonnable" in e.note


def test_r_suggestion_on_request():
    start = _dt(2026, 7, 18)
    e = _principale(compute_echeances(
        "CJP-01", start, "", inclure_suggestion_raisonnable=True
    ))
    assert e.date == compute_date_pour_agir(start, "30_jours")
    assert e.niveau == "info"
    assert "ndicatif" in e.note


# ── Token combinations ───────────────────────────────────────────────────


def test_r_d_cjp05_rouge_no_date():
    e = _principale(compute_echeances("CJP-05", _dt(2026, 7, 18), ""))
    assert e.date is None
    assert e.niveau == "rouge"      # D outranks R's info
    assert "à valider" in e.note    # CJP-05 is a_valider


def test_n_pe_fam03():
    e = _principale(compute_echeances("FAM-03", None, ""))
    assert e.date is None
    assert e.niveau == "aucun"
    assert "tout temps" in e.note
    assert "Prescription extinctive" in e.note   # secondary-token mention
    # With a lawyer-confirmed period (arrérages), it computes normally.
    start = _dt(2026, 7, 18)
    dated = _principale(compute_echeances("FAM-03", start, "3_ans"))
    assert dated.date == compute_date_pour_agir(start, "3_ans")


def test_i_pe_fam07_message_only():
    e = _principale(compute_echeances("FAM-07", None, ""))
    assert e.date is None
    assert e.niveau == "aucun"
    assert "Imprescriptible" in e.libelle or "Imprescriptible" in e.note


def test_pe_i_rcv02_computes_when_period_set():
    start = _dt(2026, 7, 18)
    e = _principale(compute_echeances("RCV-02", start, "3_ans"))
    assert e.date == compute_date_pour_agir(start, "3_ans")
    assert e.niveau == "normal"
    # Without a period: PE primary → PE-like, date None.
    empty = _principale(compute_echeances("RCV-02", start, ""))
    assert empty.date is None
    assert empty.niveau == "normal"


def test_s_a_cjp06_checklist_avis():
    echeances = compute_echeances("CJP-06", None, "")
    principale = _principale(echeances)
    assert principale.date is None
    assert principale.niveau == "aucun"
    assert "substantiel" in principale.note
    avis = [e for e in echeances if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].date is None
    assert "76-78" in avis[0].note
    assert "recevabilité" in avis[0].note


def test_a_v_cst05_manual_plus_checklist():
    echeances = compute_echeances("CST-05", None, "")
    principale = _principale(echeances)
    assert principale.date is None
    assert principale.niveau == "aucun"
    assert "manuelle" in principale.note
    assert "à valider" in principale.note        # CST-05 is a_valider
    avis = [e for e in echeances if e.role == "avis"]
    assert len(avis) == 1
    assert avis[0].libelle == "Avis à la caution"


def test_d_a_cor11_rouge_no_avis():
    start = _dt(2026, 7, 18)
    echeances = compute_echeances(
        "COR-11", start, "30_jours", date_depart_avis=start
    )
    e = _principale(echeances)
    assert e.date == compute_date_pour_agir(start, "30_jours")
    assert e.niveau == "rouge"
    # Annexe B: the 30-day delay IS the recourse — no avis échéance.
    assert [x for x in echeances if x.role == "avis"] == []


# ── AVIS_PERIODS / dropdown invariants ───────────────────────────────────


def test_avis_delai_keys_all_resolve():
    for action in taxonomie.ACTIONS.values():
        for avis in action.avis:
            if avis.delai_key is not None:
                assert avis.delai_key in AVIS_PERIODS, (
                    f"{action.code}: {avis.delai_key}"
                )


def test_prescription_dropdown_is_untouched():
    """3_jours_ouvrables lives ONLY in AVIS_PERIODS; the prescription
    vocabulary and labels are byte-identical to their pre-rework content."""
    assert "3_jours_ouvrables" not in PRESCRIPTION_PERIODS
    assert "3_jours_ouvrables" not in VALID_PRESCRIPTION_TYPES
    assert VALID_PRESCRIPTION_TYPES == ("",) + tuple(PRESCRIPTION_PERIODS)
    assert list(PRESCRIPTION_PERIODS) == [
        "5_jours", "10_jours", "15_jours", "30_jours", "45_jours", "60_jours",
        "90_jours", "3_mois", "6_mois", "9_mois", "1_an", "2_ans", "3_ans",
        "5_ans", "7_ans", "10_ans", "30_ans", "imprescriptible", "autre",
    ]
    assert PRESCRIPTION_LABELS[""] == "Non définie"
    # The calendar AVIS keys reuse the PRESCRIPTION_PERIODS entries.
    for key in ("15_jours", "30_jours", "60_jours", "9_mois"):
        assert AVIS_PERIODS[key] is PRESCRIPTION_PERIODS[key]
