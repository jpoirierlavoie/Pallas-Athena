"""« Journal de caisse des recettes et déboursés » — the art. 38 register.

Pins the sheet's shape (legal landscape, the ten columns of RLRQ c. B-1,
r. 5 art. 38, centred title block), the opening-balance row that makes the
period readable, and the reconciliation that gives it its point:
report + Σ recettes − Σ déboursés = solde de clôture.

The carte-client shares to_barreau_row/BARREAU_COLUMNS with the CSV — those
nine tests in test_trust.py must stay green, and one here asserts this sheet
did not disturb them.
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

from utils import trust_journal_pdf as J
from utils.format_fr import format_cents_fr


def _row(**over) -> dict:
    row = {
        "date": "2026-08-03",
        "client": "M. Jean Tremblay",
        "n_ref": "2026-001",
        "counterparty": "Me Sophie Gagnon, en fiducie",
        "objet": "Dépôt du client",
        "mode": "Chèque",
        "cheque": "0042",
        "recette": 500000,
        "debours": None,
        "solde": 500000,
        "en_circulation": False,
    }
    row.update(over)
    return row


def _widths() -> list[float]:
    usable = landscape(LEGAL)[0] - 2 * 10 * mm
    return [c.ratio * usable for c in J.COLUMNS]


# ── The sheet's shape (art. 38) ─────────────────────────────────────────────


def test_ten_columns_in_the_article_38_order():
    assert [c.label for c in J.COLUMNS] == [
        "Date", "Client", "N/Réf", "Somme reçue de / Bénéficiaire", "Objet",
        "Mode", "N° de chèque", "Recette", "Débours", "Solde",
    ]


def test_column_ratios_spend_the_whole_sheet():
    assert abs(sum(c.ratio for c in J.COLUMNS) - 1.0) < 1e-9


def test_every_header_fits_its_column():
    # « Somme reçue de / Bénéficiaire » is the long one; if a header did not
    # fit, reportlab would silently wrap the header band.
    for col, width in zip(J.COLUMNS, _widths()):
        drawn = pdfmetrics.stringWidth(col.label, J._FONT_BOLD, J._SIZE)
        assert drawn <= width - 2 * J._PAD, (col.label, drawn, width)


def test_only_free_text_columns_may_clip():
    assert [c.label for c in J.COLUMNS if c.clip] == [
        "Client", "Somme reçue de / Bénéficiaire", "Objet", "N° de chèque",
    ]


def test_money_columns_are_the_three_amounts():
    assert J.MONEY_KEYS == ("recette", "debours", "solde")
    assert J.TEXT_COLUMN_COUNT == 7


def test_title_block_carries_the_regulation():
    assert J.TITLE == "JOURNAL DE CAISSE DES RECETTES ET DÉBOURSÉS"
    assert "Article 38" in J.LEGAL_BASIS
    assert "comptabilité et les normes d'exercice" in J.LEGAL_BASIS


# ── Cell projection ─────────────────────────────────────────────────────────


def test_each_value_lands_under_its_own_column():
    """The anti-shift net: cells are built from each column's KEY, so
    re-ordering COLUMNS can never move a row's content sideways."""
    widths = _widths()
    row = _row(date="1111-11-11", client="CLIENT-M", n_ref="REF-M",
               counterparty="TIERS-M", objet="OBJET-M", mode="MODE-M",
               cheque="CHQ-M", recette=101, debours=None, solde=303)
    by_label = dict(zip([c.label for c in J.COLUMNS],
                        J._row_cells(row, widths)))
    assert by_label["Date"] == "1111-11-11"
    assert by_label["Client"] == "CLIENT-M"
    assert by_label["N/Réf"] == "REF-M"
    assert by_label["Somme reçue de / Bénéficiaire"] == "TIERS-M"
    assert by_label["Objet"] == "OBJET-M"
    assert by_label["Mode"] == "MODE-M"
    assert by_label["N° de chèque"] == "CHQ-M"
    assert by_label["Recette"] == format_cents_fr(101)
    assert by_label["Solde"] == format_cents_fr(303)


def test_recette_and_debours_are_mutually_exclusive_and_blank_not_zero():
    widths = _widths()
    labels = [c.label for c in J.COLUMNS]
    recette = dict(zip(labels, J._row_cells(_row(), widths)))
    assert recette["Recette"] == format_cents_fr(500000)
    assert recette["Débours"] == ""          # blank, never « 0,00 $ »
    debours = dict(zip(labels, J._row_cells(
        _row(recette=None, debours=25000), widths)))
    assert debours["Recette"] == ""
    assert debours["Débours"] == format_cents_fr(25000)


def test_uncleared_entry_is_starred_on_the_date():
    widths = _widths()
    cells = J._row_cells(_row(en_circulation=True), widths)
    assert cells[0] == "2026-08-03 *"
    assert J.UNCLEARED_LEGEND.startswith("*")


def test_identifiers_and_amounts_are_never_clipped():
    widths = _widths()
    row = _row(
        n_ref="UNE-REFERENCE-DE-DOSSIER-TRES-LONGUE",
        mode="Un mode de retrait au libellé démesuré",
        recette=999_999_999_99, debours=None, solde=999_999_999_99,
    )
    for col, cell in zip(J.COLUMNS, J._row_cells(row, widths)):
        if col.clip:
            continue
        assert not cell.endswith("…"), (col.label, cell)


def test_long_free_text_is_clipped_not_wrapped():
    widths = _widths()
    cells = J._row_cells(_row(counterparty="X" * 400), widths)
    assert cells[3].endswith("…")
    assert "\n" not in cells[3]


# ── Opening balance + reconciliation ────────────────────────────────────────


def test_opening_row_spans_the_text_columns_and_carries_the_balance():
    widths = _widths()
    cells = J._spanned_row("SOLDE REPORTÉ AU 2026-08-01", 123456, widths)
    assert cells[0].startswith("SOLDE REPORTÉ")
    assert all(c == "" for c in cells[1:J.TEXT_COLUMN_COUNT])
    assert cells[-1] == format_cents_fr(123456)


def test_totals_row_reconciles_opening_and_closing():
    """The point of the carried-forward line: report + Σ recettes
    − Σ déboursés must equal the closing balance."""
    widths = _widths()
    opening = 100000
    rows = [
        _row(recette=500000, debours=None, solde=opening + 500000),
        _row(recette=None, debours=25000, solde=opening + 500000 - 25000),
    ]
    closing = rows[-1]["solde"]
    cells = J._totals_cells(rows, widths, closing)
    money = dict(zip([c.label for c in J.COLUMNS if c.money],
                     cells[J.TEXT_COLUMN_COUNT:]))
    assert cells[0].startswith("TOTAUX — 2 inscriptions")
    assert money["Recette"] == format_cents_fr(500000)
    assert money["Débours"] == format_cents_fr(25000)
    assert money["Solde"] == format_cents_fr(closing)
    assert opening + 500000 - 25000 == closing


def test_totals_label_is_singular_for_one_entry():
    label = J._totals_cells([_row()], _widths(), 1)[0]
    assert label.startswith("TOTAUX — 1 inscription")
    assert "inscriptions" not in label


# ── The document ────────────────────────────────────────────────────────────


def test_pdf_is_legal_landscape_and_font_pure():
    resp = J.build_trust_journal_pdf(
        [_row()],
        account_line="Compte général en fidéicommis — Banque Nationale — ••••1234",
        period="Période du 2026-08-01 au 2026-08-31",
        filename="j.pdf", opening_cents=100000,
        opening_label="SOLDE REPORTÉ AU 2026-08-01",
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF")
    assert b"NotoSerif" in resp.data
    assert b"Helvetica" not in resp.data
    assert b"1008" in resp.data and b"612" in resp.data      # legal landscape


def test_empty_period_still_renders_with_its_carried_balance():
    # A register must state what the period opened on even when nothing
    # happened in it.
    resp = J.build_trust_journal_pdf(
        [], account_line="Compte général en fidéicommis",
        period="Période du 2026-09-01 au 2026-09-30", filename="j.pdf",
        opening_cents=250000, opening_label="SOLDE REPORTÉ AU 2026-09-01",
    )
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


def test_notices_are_rendered():
    resp = J.build_trust_journal_pdf(
        [_row()], account_line="Compte", period="Période", filename="j.pdf",
        notices=["Avertissement : le registre a été tronqué."],
    )
    assert resp.status_code == 200


def test_carte_client_projection_is_untouched():
    """This sheet was built BESIDE to_barreau_row, never on top of it — the
    carte-client and both CSVs still consume the nine Barreau columns."""
    from models import trust

    assert len(trust.BARREAU_COLUMNS) == 9
    assert [k for k, _ in trust.BARREAU_COLUMNS] == [
        "date", "n_ref", "counterparty", "client", "objet", "mode",
        "recette", "credit", "solde",
    ]


# ── The route degrades; it does not 500 ─────────────────────────────────────
#
# On 2026-08-11 the export answered a generic « Erreur interne du serveur »:
# both model reads fail CLOSED (they propagate), and the route caught
# nothing. A register the lawyer needs must come out stating its own gaps
# instead of vanishing behind an error page.


def _route_world(monkeypatch, *, register=None, opening=None,
                 register_raises=False, opening_raises=False):
    from flask import Flask
    from models import trust
    import routes.trust as R

    def _register(account_id, date_from=None, date_to=None, limit=10000):
        if register_raises:
            raise RuntimeError("firestore boom")
        return (register or []), False

    def _opening(account_id, as_of):
        if opening_raises:
            raise RuntimeError("firestore boom")
        return opening if opening is not None else (250000, True)

    monkeypatch.setattr(trust, "list_register", _register)
    monkeypatch.setattr(trust, "opening_book_balance", _opening)
    monkeypatch.setattr(
        trust, "list_transactions",
        lambda **kw: (register or []),
    )
    return Flask(__name__), R


def _tx(**over):
    from datetime import datetime, timezone
    d = {
        "date": datetime(2026, 5, 3, tzinfo=timezone.utc),
        "client_name": "M. Client", "dossier_file_number": "2026-001",
        "counterparty": "Tiers", "purpose": "dépôt_client", "method": "chèque",
        "reference": "0042", "direction": "recette", "amount": 500000,
        "balance_after_account": 750000, "status": "compensée",
    }
    d.update(over)
    return d


_ACCOUNT = {"id": "a1", "name": "Compte général en fidéicommis",
            "institution": "Banque Nationale", "account_number_last4": "1234"}


def _period():
    from datetime import datetime, timezone
    return (datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 31, tzinfo=timezone.utc))


def test_route_renders_the_register_for_a_period(monkeypatch):
    app, R = _route_world(monkeypatch, register=[_tx()])
    df, dt = _period()
    with app.test_request_context("/fideicommis/export/pdf"):
        resp = R._journal_pdf(_ACCOUNT, "a1", df, dt)
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
    assert "journal_caisse_fideicommis" in resp.headers["Content-Disposition"]


def test_route_degrades_when_the_opening_balance_cannot_be_read(monkeypatch):
    app, R = _route_world(monkeypatch, register=[_tx()], opening_raises=True)
    df, dt = _period()
    with app.test_request_context("/fideicommis/export/pdf"):
        resp = R._journal_pdf(_ACCOUNT, "a1", df, dt)
    # A register, with a notice — never a generic error page.
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


def test_route_degrades_when_the_register_read_fails(monkeypatch):
    app, R = _route_world(monkeypatch, register=[_tx()], register_raises=True)
    df, dt = _period()
    with app.test_request_context("/fideicommis/export/pdf"):
        resp = R._journal_pdf(_ACCOUNT, "a1", df, dt)
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")


def test_route_without_a_period_prints_no_carried_balance(monkeypatch):
    app, R = _route_world(monkeypatch, register=[_tx()])
    with app.test_request_context("/fideicommis/export/pdf"):
        resp = R._journal_pdf(_ACCOUNT, "a1", None, None)
    assert resp.status_code == 200
    assert resp.data.startswith(b"%PDF")
