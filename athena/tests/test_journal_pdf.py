"""« Journal des honoraires » — the Barreau-model fee journal export.

Pins the sheet's shape (legal landscape, 13 columns, no grouping band), the
disbursement split the invoice document does not store, and the one property
the practitioner asked for explicitly: a row never folds onto itself and
never runs into its neighbour.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from reportlab.lib.pagesizes import LEGAL, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics

from models.invoice import expense_split
from utils import journal_pdf
from utils.format_fr import format_cents_fr


def _row(**over) -> dict:
    row = {
        "date": "2026-08-01",
        "reference": "2026-001",
        "client": "M. Jean Tremblay",
        "numero": "2026-001-03",
        "honoraires": 150000,
        "debours_tx": 10500,
        "debours_ntx": 5000,
        "sous_total": 165500,
        "tps": 8025,
        "tvq": 16010,
        "total": 189535,
        "recu": 0,
        "solde": 189535,
        "annulee": False,
    }
    row.update(over)
    return row


def _widths() -> list[float]:
    usable = landscape(LEGAL)[0] - 2 * 10 * mm
    return [ratio * usable for _, ratio, _ in journal_pdf.COLUMNS]


# ── The sheet's shape ───────────────────────────────────────────────────────


def test_thirteen_columns_in_the_requested_order():
    assert [label for label, _, _ in journal_pdf.COLUMNS] == [
        "Date", "N/Réf", "Client", "N° de note", "Honoraires",
        "Débours\ntaxables", "Débours non\ntaxables", "Sous-total",
        "TPS", "TVQ", "Total", "Sommes\nreçues", "Solde",
    ]


def test_column_ratios_spend_the_whole_sheet():
    # The widths must fill the legal sheet — that is what keeps rows on one
    # line (« ajusté la largeur afin de remplir la grandeur de la feuille »).
    assert abs(sum(r for _, r, _ in journal_pdf.COLUMNS) - 1.0) < 1e-9


def test_money_columns_are_the_nine_summed_ones():
    money = [label for label, _, is_money in journal_pdf.COLUMNS if is_money]
    assert len(money) == len(journal_pdf.MONEY_KEYS) == 9


def test_pdf_is_legal_landscape_and_font_pure():
    resp = journal_pdf.build_journal_pdf(
        [_row()], subtitle="Toutes les factures", filename="j.pdf"
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    assert b"NotoSerif" in resp.data
    assert b"Helvetica" not in resp.data
    # Legal landscape: 1008 × 612 pt, as /MediaBox in the page dictionary.
    assert b"1008" in resp.data and b"612" in resp.data


def test_title_and_no_grouping_band():
    resp = journal_pdf.build_journal_pdf(
        [_row()], subtitle="", filename="j.pdf"
    )
    assert resp.status_code == 200
    # The Barreau sheet's top band is deliberately dropped.
    labels = [label for label, _, _ in journal_pdf.COLUMNS]
    assert "Facturation" not in labels
    assert "DÉTAIL DE LA FACTURE" not in labels
    assert journal_pdf.TITLE == "JOURNAL DES HONORAIRES"


def test_empty_journal_still_renders():
    resp = journal_pdf.build_journal_pdf([], subtitle="", filename="j.pdf")
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


# ── No row folds onto itself, none overflows ────────────────────────────────


def test_every_cell_fits_its_column_even_with_extreme_values():
    widths = _widths()
    rows = [
        _row(),
        _row(  # a very long client name and seven-figure amounts
            client="Les Entreprises Internationales Tremblay, Gagnon, "
                   "Lavoie & Associés (Québec) Limitée",
            honoraires=987_654_321, debours_tx=123_456_789,
            debours_ntx=98_765_432, sous_total=1_209_876_542,
            tps=60_493_827, tvq=120_684_784, total=1_391_055_153,
            recu=1_000_000_000, solde=391_055_153,
        ),
        _row(annulee=True),
    ]
    for row in rows:
        for cell, width in zip(journal_pdf._row_cells(row, widths), widths):
            assert "\n" not in cell, cell          # never wraps
            drawn = pdfmetrics.stringWidth(
                cell, journal_pdf._FONT, journal_pdf._SIZE
            )
            assert drawn <= width - 2 * journal_pdf._PAD, (cell, drawn, width)


def test_long_client_name_is_clipped_not_wrapped():
    widths = _widths()
    cells = journal_pdf._row_cells(
        _row(client="X" * 400), widths
    )
    assert cells[2].endswith("…")
    assert len(cells[2]) < 400


def test_voided_invoice_is_marked_in_the_note_number():
    widths = _widths()
    live = journal_pdf._row_cells(_row(), widths)[3]
    void = journal_pdf._row_cells(_row(annulee=True), widths)[3]
    assert "(ann.)" in void and "(ann.)" not in live


# ── Arithmetic ──────────────────────────────────────────────────────────────


def test_totals_row_sums_exactly_what_is_displayed():
    widths = _widths()
    rows = [_row(), _row(honoraires=50000, sous_total=50000, total=50000)]
    cells = journal_pdf._totals_cells(rows, widths)
    assert cells[0].startswith("TOTAL — 2 factures")
    assert cells[4] == format_cents_fr(150000 + 50000)   # honoraires
    assert cells[7] == format_cents_fr(165500 + 50000)   # sous-total


def test_totals_label_is_singular_for_one_invoice():
    assert journal_pdf._totals_cells([_row()], _widths())[0].startswith(
        "TOTAL — 1 facture"
    )


def test_expense_split_always_ties_back_to_the_stored_subtotal():
    invoice = {"subtotal_expenses": 15500}
    items = [
        {"type": "expense", "amount": 10500, "taxable": True},
        {"type": "expense", "amount": 5000, "taxable": False},
        {"type": "fee", "amount": 150000, "taxable": True},   # excluded
    ]
    tx, ntx = expense_split(invoice, items)
    assert (tx, ntx) == (10500, 5000)
    assert tx + ntx == invoice["subtotal_expenses"]


def test_expense_split_falls_back_to_taxable_when_items_are_unreadable():
    # list_line_items fails open to [] — the row must still tie to the
    # invoice's own subtotal rather than silently under-report it.
    invoice = {"subtotal_expenses": 15500}
    tx, ntx = expense_split(invoice, [])
    assert (tx, ntx) == (15500, 0)
    assert tx + ntx == invoice["subtotal_expenses"]
