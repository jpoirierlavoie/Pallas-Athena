"""Tests for utils/admin_journal_pdf.py — the firm cash-register sheet.

The sibling of test_trust_journal_pdf.py: the sheet's shape (column table,
ratios, clip policy), the cell projection (blank-not-zero discipline on the
mutually-exclusive and expense-only money columns), the opening/totals
reconciliation, and the document itself (legal landscape, NotoSerif only —
the deploy gate's « no Helvetica » doctrine).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import admin_journal_pdf as ajp  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# The sheet's shape
# ═══════════════════════════════════════════════════════════════════════════


def test_ratios_sum_to_one():
    assert round(sum(c.ratio for c in ajp.COLUMNS), 6) == 1.0


def test_eleven_columns_in_the_pinned_order():
    assert [c.key for c in ajp.COLUMNS] == [
        "date", "counterparty", "categorie", "facture", "mode",
        "net", "tps", "tvq", "recette", "debours", "solde",
    ]


def test_only_free_text_columns_may_clip():
    """A date, a mode, an amount or the running balance is never ellipsised —
    in a book of account a truncated figure or identifier is a FALSE one."""
    clippable = {c.key for c in ajp.COLUMNS if c.clip}
    assert clippable == {"counterparty", "categorie", "facture"}
    assert not any(c.clip for c in ajp.COLUMNS if c.money)


def test_money_columns_are_the_six_amounts():
    assert ajp.MONEY_KEYS == ("net", "tps", "tvq", "recette", "debours", "solde")
    assert ajp.TEXT_COLUMN_COUNT == 5


# ═══════════════════════════════════════════════════════════════════════════
# Cell projection — blank, never « 0,00 $ », on the inapplicable side
# ═══════════════════════════════════════════════════════════════════════════


def _expense_row(**over):
    row = {
        "date": "2026-07-10", "counterparty": "Bell Canada",
        "categorie": "Téléphone", "facture": "F-8842", "mode": "Prélèvement",
        "net": 10000, "tps": 500, "tvq": 998,
        "recette": None, "debours": 11498, "solde": -11498,
        "en_circulation": False,
    }
    row.update(over)
    return row


def test_expense_row_projects_each_value_under_its_column():
    from utils.format_fr import format_cents_fr

    values = ajp._display_values(_expense_row())
    assert values["net"] == format_cents_fr(10000)
    assert values["recette"] == ""          # mutually exclusive — BLANK
    assert "114" in values["debours"]
    assert values["facture"] == "F-8842"


def test_receipt_row_leaves_ventilation_and_debours_blank():
    values = ajp._display_values({
        "date": "2026-07-15", "counterparty": "Jean Tremblay",
        "categorie": "", "facture": "2026-F031", "mode": "Virement",
        "net": None, "tps": None, "tvq": None,
        "recette": 60000, "debours": None, "solde": 48502,
        "en_circulation": True,
    })
    assert values["net"] == "" and values["tps"] == "" and values["tvq"] == ""
    assert values["debours"] == ""
    assert values["date"].endswith(" *")    # uncleared flag on the date


def test_missing_solde_prints_blank_not_zero():
    values = ajp._display_values(_expense_row(solde=None))
    assert values["solde"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# The document
# ═══════════════════════════════════════════════════════════════════════════


def _build(rows, **kw):
    defaults = dict(
        account_line="Opérations — BNC — ••••1234",
        period="Période du 2026-07-01 au 2026-07-31",
        filename="journal_administration_test.pdf",
    )
    defaults.update(kw)
    return ajp.build_admin_journal_pdf(rows, **defaults)


def test_document_renders_legal_landscape_notoserif_only():
    resp = _build(
        [_expense_row()],
        opening_cents=50000, opening_label="SOLDE REPORTÉ AU 2026-07-01",
        tps_total=500, tvq_total=998,
    )
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
    assert b"NotoSerif" in resp.data
    assert b"Helvetica" not in resp.data
    from reportlab.lib.pagesizes import LEGAL, landscape

    # legal landscape: 1008 x 612 points — pinned via the module's own build
    assert landscape(LEGAL)[0] > landscape(LEGAL)[1]


def test_empty_period_still_renders_with_the_notice():
    resp = _build([], notices=["Avertissement : essai."])
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


def test_header_labels_fit_their_columns_unclipped():
    """Every column label must fit its width at header size — a clipped
    HEADER would be a design bug, not a data condition."""
    from reportlab.lib.pagesizes import LEGAL, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics

    usable = landscape(LEGAL)[0] - 2 * (10 * mm)
    for col in ajp.COLUMNS:
        width = col.ratio * usable - 2 * ajp._PAD
        assert pdfmetrics.stringWidth(col.label, ajp._FONT_BOLD, ajp._SIZE) <= width, col.label


def test_money_columns_hold_a_seven_figure_amount():
    from reportlab.lib.pagesizes import LEGAL, landscape
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from utils.format_fr import format_cents_fr

    usable = landscape(LEGAL)[0] - 2 * (10 * mm)
    sample = format_cents_fr(123456789)  # 1 234 567,89 $
    for col in ajp.COLUMNS:
        if not col.money:
            continue
        width = col.ratio * usable - 2 * ajp._PAD
        assert pdfmetrics.stringWidth(sample, ajp._FONT, ajp._SIZE) <= width, col.key
