"""Tests for utils/phases.py — the litigation-phase taxonomy (Phase O, axis 1).

Pure module: no Firestore, no Flask. The reference data is transcribed from
SPEC_PHASE_O_PHASAGE.md Annexe A — these tests pin that transcription, the
D-9 synthesis, the ASCII invariant (D-3), and the derived constants (Annexe B).
"""

import json
import re

from utils import phases

_PHASE_RE = re.compile(r"^[A-Z]{3}$")
_SOUS_RE = re.compile(r"^[A-Z]{3}-\d{2}$")


# ── Annexe A pinned ─────────────────────────────────────────────────────────


def test_annexe_a_phase_inventory_is_pinned():
    # 18 phases, A.1 order preserved (ADM first, tronc 1-9, modules, HOR last).
    assert list(phases.PHASES) == [
        "ADM", "PRE", "PRD", "INT", "CTS", "INR", "MEE", "INS", "AUD", "JUG",
        "PRL", "PRV", "INC", "EXP", "EXE", "APP", "CJU", "HOR",
    ]


def test_annexe_a_samples():
    assert phases.PHASES["CTS"].libelle == "Contestation"
    assert phases.PHASES["AUD"].libelle == "Instruction"
    assert phases.SOUS_CODES["CTS-02"].libelle == "Demande reconventionnelle"
    assert phases.SOUS_CODES["PRE-04"].libelle == "Mise en demeure et avis préalables"
    assert phases.SOUS_CODES["ADM-05"].libelle == "Fermeture et conservation du dossier"
    # D-19: the one sanctioned nature-of-work split.
    assert phases.SOUS_CODES["AUD-01"].libelle == "Préparation de l'instruction"
    assert phases.SOUS_CODES["AUD-02"].libelle == "Audience"


def test_explicit_sous_code_counts_match_annexe_a():
    # Explicit rows only (synthesized -00/-99 excluded): A.2 = 27, A.3 = 21,
    # A.4 = 6 (5 ADM + HOR-00).
    explicit = {
        code: [
            sc for sc in p.sous_codes
            if not (code != "HOR" and sc.code.endswith(("-00", "-99")))
        ]
        for code, p in phases.PHASES.items()
    }
    counts = {code: len(rows) for code, rows in explicit.items()}
    assert counts == {
        "ADM": 5, "PRE": 4, "PRD": 4, "INT": 3, "CTS": 3, "INR": 3,
        "MEE": 3, "INS": 2, "AUD": 2, "JUG": 3, "PRL": 3, "PRV": 3,
        "INC": 3, "EXP": 4, "EXE": 2, "APP": 3, "CJU": 3, "HOR": 1,
    }


# ── D-9 synthesis ───────────────────────────────────────────────────────────


def test_every_phase_but_hor_gets_synthesized_00_and_99():
    for code, p in phases.PHASES.items():
        codes = [sc.code for sc in p.sous_codes]
        if code == "HOR":
            assert codes == ["HOR-00"]
            # A.4 label, never the synthesized « Général ».
            assert p.sous_codes[0].libelle == "Hors phase (résiduel — aucune ventilation)"
            continue
        assert codes[0] == f"{code}-00", code
        assert codes[-1] == f"{code}-99", code
        assert phases.SOUS_CODES[f"{code}-00"].libelle == "Général"
        assert phases.SOUS_CODES[f"{code}-99"].libelle == "Autre (préciser)"


# ── D-3 ASCII invariant (protects the DAV CATEGORIES round-trip) ────────────


def test_codes_are_strict_ascii():
    for code in phases.PHASES:
        assert _PHASE_RE.match(code), code
    for code in phases.SOUS_CODES:
        assert _SOUS_RE.match(code), code
        assert code.isascii(), code


# ── Prefix invariant + hierarchy ────────────────────────────────────────────


def test_prefix_is_the_relationship():
    for code, p in phases.PHASES.items():
        for sc in p.sous_codes:
            assert sc.code.split("-", 1)[0] == code, sc.code
            assert phases.phase_of(sc.code) == code, sc.code


def test_phase_of_unknown_and_empty():
    assert phases.phase_of("") == ""
    assert phases.phase_of("ZZZ-01") == ""
    assert phases.phase_of("CTS") == ""  # a phase code is not a sub-code


def test_default_sous_phase():
    for code in phases.PHASES:
        assert phases.default_sous_phase(code) == f"{code}-00"
        assert phases.default_sous_phase(code) in phases.SOUS_CODES
    assert phases.default_sous_phase("") == ""
    assert phases.default_sous_phase("ZZZ") == ""


# ── resolve_pair — l'ergonomie partagee des chemins d'ecriture ───────────


def test_resolve_pair_derives_the_parent_from_the_prefix():
    assert phases.resolve_pair("", "CTS-02") == ("CTS", "CTS-02")


def test_resolve_pair_imputes_the_00_from_a_phase_alone():
    assert phases.resolve_pair("MEE", "") == ("MEE", "MEE-00")


def test_resolve_pair_leaves_a_complete_pair_alone():
    assert phases.resolve_pair("EXP", "EXP-03") == ("EXP", "EXP-03")
    # …y compris un couple INCOHERENT : completer n'est pas valider, et
    # c'est validate_pair qui doit pouvoir le refuser en le voyant tel quel.
    assert phases.resolve_pair("INS", "CTS-02") == ("INS", "CTS-02")


def test_resolve_pair_ne_valide_jamais():
    """Un sous-code inconnu ressort TEL QUEL, parent non derive.

    Deriver ici et valider la-bas est ce qui empeche « sous-phase
    invalide » d'etre rapporte comme « phase requise » : le modele
    validerait un couple vide et enverrait l'appelant reparer la mauvaise
    moitie."""
    assert phases.resolve_pair("", "XXX-99") == ("", "XXX-99")
    assert phases.validate_pair(
        {"phase": "", "sous_phase": "XXX-99"}
    ) == ["Sous-phase invalide."]
    assert phases.resolve_pair("ZZZ", "") == ("ZZZ", "")
    assert phases.resolve_pair("", "") == ("", "")


def test_resolve_pair_trims_and_tolerates_blanks():
    assert phases.resolve_pair("  CTS  ", "  CTS-02 ") == ("CTS", "CTS-02")


# ── Derived constants (Annexe B) ────────────────────────────────────────────


def test_valid_vocabularies_include_empty():
    # "" is load-bearing: the D-6 requirement lives at the web form, never in
    # the model — a hard model requirement would 422 every DavX5 task PUT.
    assert "" in phases.VALID_PHASES
    assert "" in phases.VALID_SOUS_PHASES
    assert phases.PHASE_LABELS[""] == "Non renseignée"
    assert phases.SOUS_PHASE_LABELS[""] == "Non renseignée"


def test_label_parity_bidirectional():
    for code in phases.VALID_PHASES:
        assert code in phases.PHASE_LABELS, code
    for code in phases.PHASE_LABELS:
        assert code in phases.VALID_PHASES, code
    for code in phases.VALID_SOUS_PHASES:
        assert code in phases.SOUS_PHASE_LABELS, code
    for code in phases.SOUS_PHASE_LABELS:
        assert code in phases.VALID_SOUS_PHASES, code


def test_non_facturables_derived():
    assert phases.PHASES_NON_FACTURABLES == frozenset({"ADM", "HOR"})
    for code in phases.PHASES_NON_FACTURABLES:
        assert phases.PHASES[code].facturable_defaut is False


def test_tronc_ordonne():
    # 9 tronc phases, ordre 1-9, no gap, no module among them (spec §10:
    # calendar-duration analytics may only run over the ordered tronc).
    assert phases.TRONC_ORDONNE == (
        "PRE", "PRD", "INT", "CTS", "INR", "MEE", "INS", "AUD", "JUG",
    )
    ordres = [phases.PHASES[c].ordre for c in phases.TRONC_ORDONNE]
    assert ordres == list(range(1, 10))
    for code, p in phases.PHASES.items():
        if p.categorie == "tronc":
            assert code in phases.TRONC_ORDONNE
        else:
            assert p.ordre is None, code


def test_categories_closed_and_covering():
    cats = {p.categorie for p in phases.PHASES.values()}
    assert cats == {"tronc", "module", "transversal", "residuel"}
    assert [c for c, p in phases.PHASES.items() if p.categorie == "transversal"] == ["ADM"]
    assert [c for c, p in phases.PHASES.items() if p.categorie == "residuel"] == ["HOR"]


# ── Helpers + form payload ──────────────────────────────────────────────────


def test_sous_codes_for_and_labels():
    assert [sc.code for sc in phases.sous_codes_for("INS")] == [
        "INS-00", "INS-01", "INS-02", "INS-99",
    ]
    assert phases.sous_codes_for("") == ()
    assert phases.sous_phase_label("CTS-02") == "Demande reconventionnelle [CTS-02]"
    assert phases.sous_phase_label("ZZZ-01") == ""


def test_form_payload_cached_json_ready():
    p1 = phases.form_payload()
    assert p1 is phases.form_payload()  # lru_cached, shared, read-only
    text = json.dumps(p1, ensure_ascii=False)
    assert '"CTS"' in text and "Demande reconventionnelle" in text
    assert p1["ADM"]["facturable_defaut"] is False
    assert p1["CTS"]["facturable_defaut"] is True
    assert p1["HOR"]["sous_codes"] == [
        {"code": "HOR-00", "label": "Hors phase (résiduel — aucune ventilation) [HOR-00]"}
    ]


def test_module_stays_pure():
    # Importable without Firestore/Flask — mcp/tools.py and the models both
    # depend on that (the taxonomie.py purity rule). Inspect the actual
    # import statements, not prose (the docstring legitimately SAYS
    # « no Firestore »).
    import sys

    mod = sys.modules["utils.phases"]
    src = open(mod.__file__, encoding="utf-8").read()
    imports = [
        line.strip()
        for line in src.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    for line in imports:
        assert "models" not in line, line
        assert "firestore" not in line.lower(), line
        assert "flask" not in line.lower(), line
