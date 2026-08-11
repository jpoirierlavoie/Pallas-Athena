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
    return [col.ratio * usable for col in journal_pdf.COLUMNS]


# ── The sheet's shape ───────────────────────────────────────────────────────


def test_thirteen_columns_in_the_requested_order():
    # Date · Client · N/Réf · N° de note — the Barreau model's own order.
    assert [c.label for c in journal_pdf.COLUMNS] == [
        "Date", "Client", "N/Réf", "N° de note", "Honoraires",
        "Débours TX", "Débours NTX", "Sous-total",
        "TPS", "TVQ", "Total", "Sommes reçues", "Solde",
    ]


def test_headers_are_single_line():
    # The shortened labels all fit on one line now — a uniform header band.
    for col in journal_pdf.COLUMNS:
        assert "\n" not in col.label, col.label


def test_column_ratios_spend_the_whole_sheet():
    # The widths must fill the legal sheet — that is what keeps rows on one
    # line (« ajusté la largeur afin de remplir la grandeur de la feuille »).
    assert abs(sum(c.ratio for c in journal_pdf.COLUMNS) - 1.0) < 1e-9


def test_money_columns_are_the_nine_summed_ones():
    money = [c for c in journal_pdf.COLUMNS if c.money]
    assert len(money) == len(journal_pdf.MONEY_KEYS) == 9
    # MONEY_KEYS is DERIVED from COLUMNS — the totals row can only ever sum
    # the money columns that are actually printed.
    assert journal_pdf.MONEY_KEYS == tuple(c.key for c in money)
    assert journal_pdf.TEXT_COLUMN_COUNT == 13 - 9


def test_each_value_lands_under_its_own_column():
    """The net that was missing: cells are built from each column's KEY, so
    re-ordering COLUMNS can never shift a row's content sideways. Feed a row
    whose every field is distinctive and check where each one lands."""
    widths = _widths()
    row = _row(
        date="1111-11-11", client="CLIENT-MARKER", reference="REF-MARKER",
        numero="NUM-MARKER", honoraires=101, debours_tx=202, debours_ntx=303,
        sous_total=404, tps=505, tvq=606, total=707, recu=808, solde=909,
    )
    cells = journal_pdf._row_cells(row, widths)
    by_label = {
        col.label: cell for col, cell in zip(journal_pdf.COLUMNS, cells)
    }
    assert by_label["Date"] == "1111-11-11"
    assert by_label["Client"] == "CLIENT-MARKER"
    assert by_label["N/Réf"] == "REF-MARKER"
    assert by_label["N° de note"] == "NUM-MARKER"
    assert by_label["Honoraires"] == format_cents_fr(101)
    assert by_label["Débours TX"] == format_cents_fr(202)
    assert by_label["Débours NTX"] == format_cents_fr(303)
    assert by_label["Solde"] == format_cents_fr(909)


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
    labels = [col.label for col in journal_pdf.COLUMNS]
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
    cells = journal_pdf._row_cells(_row(client="X" * 400), widths)
    client = cells[1]          # Client is the second column now
    assert client.endswith("…")
    assert len(client) < 400


def test_client_is_the_only_clippable_column():
    """Clipping a client name is cosmetic; clipping an identifier or an
    AMOUNT in a book of account makes it false."""
    clippable = [c.label for c in journal_pdf.COLUMNS if c.clip]
    assert clippable == ["Client"]


def test_identifiers_and_amounts_are_never_clipped():
    widths = _widths()
    row = _row(
        reference="UNE-RÉFÉRENCE-DE-DOSSIER-ABSURDEMENT-LONGUE",
        numero="2026-F0001 (une note héritée très longue)",
        **{k: 999_999_999_99 for k in journal_pdf.MONEY_KEYS},
    )
    cells = journal_pdf._row_cells(row, widths)
    for col, cell in zip(journal_pdf.COLUMNS, cells):
        if col.clip:
            continue
        assert not cell.endswith("…"), (col.label, cell)
        if col.money:
            assert cell == format_cents_fr(999_999_999_99)


def test_a_legacy_voided_note_number_still_fits_its_column():
    # « 2026-F001 (ann.) » is the widest note number the sheet can hold —
    # the column is budgeted for it rather than clipping an identifier.
    widths = _widths()
    cells = journal_pdf._row_cells(
        _row(numero="2026-F001", reference="", annulee=True), widths
    )
    idx = [c.key for c in journal_pdf.COLUMNS].index("numero")
    assert cells[idx] == "2026-F001 (ann.)"
    drawn = pdfmetrics.stringWidth(
        cells[idx], journal_pdf._FONT, journal_pdf._SIZE
    )
    assert drawn <= widths[idx] - 2 * journal_pdf._PAD, (drawn, widths[idx])


def test_voided_invoice_is_marked_in_the_note_number():
    widths = _widths()
    live = journal_pdf._row_cells(_row(), widths)[3]
    void = journal_pdf._row_cells(_row(annulee=True), widths)[3]
    assert "(ann.)" in void and "(ann.)" not in live
    # Marked AFTER the prefix is dropped — « 03 (ann.) », not the whole number.
    assert void == "03 (ann.)"


# ── The note number drops the file-number prefix ────────────────────────────


def test_note_number_drops_the_dossier_prefix():
    # The N/Réf column already carries « 2026-001 ».
    assert journal_pdf._short_note_number("2026-001-03", "2026-001") == "03"
    # Past 99 the suffix widens — still all digits, still dropped.
    assert journal_pdf._short_note_number("2026-001-100", "2026-001") == "100"


def test_note_number_is_left_whole_when_the_prefix_is_not_the_file():
    # A legacy YYYY-FNNN number: the prefix is the YEAR, deducible from no
    # other column.
    assert journal_pdf._short_note_number("2026-F001", "2026-001") == "2026-F001"
    # No N/Réf: the note number is the row's only identification.
    assert journal_pdf._short_note_number("2026-F001", "") == "2026-F001"
    # The free-form file-number trap: « 2026 » is a legal file number, and a
    # naive strip would read « 2026-F001 » as « F001 ».
    assert journal_pdf._short_note_number("2026-F001", "2026") == "2026-F001"
    # A prefix/reference divergence (rename race, DAV import stray space).
    assert (
        journal_pdf._short_note_number("2025-014-01", "2026-001")
        == "2025-014-01"
    )


def test_note_number_survives_dashes_inside_the_file_number():
    # A rsplit/split("-") would fall apart here; startswith does not.
    assert journal_pdf._short_note_number("A-B-C-07", "A-B-C") == "07"


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
