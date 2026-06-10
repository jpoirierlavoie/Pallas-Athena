"""Unit tests for utils/export_csv.py and utils/export_pdf.py — output-injection hardening."""

import sys
import os

# Ensure athena/ is on the path when running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.export_csv import export_csv, _format_value, _neutralize_formula
from utils.export_pdf import export_pdf, export_pdf_grouped


def _fmt(val, key="field", cents=None, hours=None):
    """Shortcut around _format_value with empty special-field sets."""
    return _format_value(val, key, "%Y-%m-%d", cents or set(), hours or set())


# ── CSV formula neutralization (_format_value) ────────────────────────────


def test_csv_equals_prefix_neutralized():
    assert _fmt('=HYPERLINK("http://evil")') == '\'=HYPERLINK("http://evil")'


def test_csv_plus_prefix_neutralized():
    assert _fmt("+1+1") == "'+1+1"


def test_csv_at_prefix_neutralized():
    assert _fmt("@SUM(1+1)") == "'@SUM(1+1)"


def test_csv_minus_prefix_neutralized():
    assert _fmt("-2+3") == "'-2+3"


def test_csv_tab_prefix_neutralized():
    assert _fmt("\t=1+1") == "'\t=1+1"


def test_csv_cr_prefix_neutralized():
    assert _fmt("\r=1+1") == "'\r=1+1"


def test_csv_negative_cents_not_prefixed():
    # Negative amounts rendered through cents_fields legitimately start
    # with '-' and must NOT receive the quote prefix.
    assert _fmt(-15000, key="amount", cents={"amount"}) == "-150.00"


def test_csv_negative_hours_not_prefixed():
    assert _fmt(-1.5, key="hours", hours={"hours"}) == "-1.5"


def test_csv_plain_string_untouched():
    assert _fmt("Tremblay c. Lavoie") == "Tremblay c. Lavoie"


def test_csv_list_with_formula_first_item_neutralized():
    # Tag lists are user-controlled strings — the joined cell is neutralized.
    assert _fmt(["=cmd|' /C calc'!A0", "ok"]) == "'=cmd|' /C calc'!A0, ok"


def test_neutralize_formula_passthrough():
    assert _neutralize_formula("Honoraires") == "Honoraires"


# ── CSV end-to-end ────────────────────────────────────────────────────────


def test_export_csv_neutralizes_cell_but_not_amounts():
    rows = [{"description": '=HYPERLINK("http://evil")', "amount": -15000}]
    columns = [("description", "Description"), ("amount", "Montant")]
    resp = export_csv(rows, columns, cents_fields=["amount"])
    body = resp.get_data(as_text=True)
    assert "'=HYPERLINK" in body
    assert "-150.00" in body
    assert "'-150.00" not in body


# ── PDF escaping (dangling '<' must not raise) ────────────────────────────


def test_export_pdf_dangling_angle_bracket_does_not_raise():
    rows = [
        {"title": "Réunion c. <Gagnon le 5"},
        {"title": "Vigneault & Fils <inc>"},
    ]
    columns = [("title", "Titre", 1.0)]
    resp = export_pdf(rows, columns, title="Rapport", filename="test.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")


def test_export_pdf_grouped_dangling_angle_bracket_does_not_raise():
    groups = [
        ("Dossier <2025-001 & autres", [{"title": "Réunion c. <Gagnon le 5"}]),
    ]
    columns = [("title", "Titre", 1.0)]
    resp = export_pdf_grouped(groups, columns, title="Rapport", filename="test.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
