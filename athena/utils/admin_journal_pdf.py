"""« Journal de caisse — compte d'administration » — the firm cash register.

The operations-account / corporate-card journal on legal paper, landscape —
a SIBLING of utils/trust_journal_pdf.py (that file's own doctrine: COLUMNS/
TITLE are module globals, a second sheet cannot share them without turning
everything into arguments). Same proven shape — order lives in ONE table
keyed by ``key``, clipping is reserved to free-text columns, money is fr-CA,
a carried-forward opening row and a totals row that reconciles
« report + Σ recettes − Σ déboursés = solde de clôture » — plus what a firm
register carries and the trust one does not: the TPS/TVQ ventilation columns
(the CTI/RTI payoff) and a closing tax line that spells them out.

No statutory article in the subtitle: this register is good practice and
fiscal necessity, not art. 38 — the account line takes its place.

Font trap (documented in export_pdf.py): importing utils.export_pdf is what
registers NotoSerif; SimpleDocTemplate needs ``initialFontName`` and the page
callbacks must call ``canvas.setFont`` explicitly, or the deploy gate's « no
Helvetica » assertion fires.
"""

import io
import logging
from datetime import datetime
from typing import NamedTuple, Optional
from xml.sax.saxutils import escape

from flask import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LEGAL, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from utils import export_pdf as _export_pdf  # noqa: F401 — registers NotoSerif
from utils.format_fr import format_cents_fr

logger = logging.getLogger(__name__)

TITLE = "JOURNAL DE CAISSE — COMPTE D'ADMINISTRATION"
UNCLEARED_LEGEND = "* = en circulation (non compensé / non relevé)"


class Column(NamedTuple):
    label: str
    ratio: float          # share of the usable width; the ratios sum to 1
    money: bool           # right-aligned, fr-CA formatted
    key: str              # the register-row key this column reads
    clip: bool = False    # may be ellipsised to fit — free text only


# The sheet. ORDER LIVES HERE ALONE: every consumer reads ``key``, so
# re-ordering is a permutation of this table and cannot silently shift a
# column's content. Only the three FREE-TEXT columns may clip — a date, a
# mode, an amount or the running balance is never ellipsised (in a book of
# account a truncated figure or identifier is a FALSE one). Six money
# columns ≥ 25 mm each on the 335 mm usable width hold « 1 234 567,89 $ »
# at 7 pt with margin.
COLUMNS: tuple[Column, ...] = (
    Column("Date", 0.065, False, "date"),
    Column("Fournisseur / Source", 0.180, False, "counterparty", clip=True),
    Column("Catégorie", 0.115, False, "categorie", clip=True),
    Column("N° facture", 0.095, False, "facture", clip=True),
    Column("Mode", 0.060, False, "mode"),
    Column("Net", 0.080, True, "net"),
    Column("TPS", 0.075, True, "tps"),
    Column("TVQ", 0.075, True, "tvq"),
    Column("Recette", 0.085, True, "recette"),
    Column("Déboursé", 0.085, True, "debours"),
    Column("Solde", 0.085, True, "solde"),
)

MONEY_KEYS: tuple[str, ...] = tuple(c.key for c in COLUMNS if c.money)
TEXT_COLUMN_COUNT: int = sum(1 for c in COLUMNS if not c.money)

_FONT = "NotoSerif"
_FONT_BOLD = "NotoSerif-Bold"
_SIZE = 7
_PAD = 3

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6B7280")
_BAND = colors.HexColor("#F9FAFB")
_RULE = colors.HexColor("#D1D5DB")


def _fit(text: str, width: float) -> str:
    """Clip *text* to *width*, ellipsised — never wrapped. Free text only."""
    text = str(text or "")
    usable = width - 2 * _PAD
    if pdfmetrics.stringWidth(text, _FONT, _SIZE) <= usable:
        return text
    ellipsis = "…"
    while text and pdfmetrics.stringWidth(text + ellipsis, _FONT, _SIZE) > usable:
        text = text[:-1]
    return text + ellipsis


def _display_values(row: dict) -> dict[str, str]:
    """Every column's cell text, keyed by column — never positional."""
    out = {c.key: str(row.get(c.key) or "") for c in COLUMNS if not c.money}
    if row.get("en_circulation"):
        out["date"] = f"{out['date']} *"
    for col in COLUMNS:
        if col.money:
            cents = row.get(col.key)
            # The inapplicable side prints BLANK, never « 0,00 $ » —
            # Recette/Déboursé are mutually exclusive by direction, the
            # ventilation columns exist only on expenses, and a Solde the
            # route could not establish stays honest by its absence.
            out[col.key] = "" if cents is None else format_cents_fr(int(cents))
    return out


def _row_cells(row: dict, widths: list[float]) -> list[str]:
    values = _display_values(row)
    return [
        _fit(values[col.key], width) if col.clip else values[col.key]
        for col, width in zip(COLUMNS, widths)
    ]


def _spanned_row(label: str, solde_cents: Optional[int],
                 widths: list[float]) -> list[str]:
    """A row whose label spans the text columns, with a balance at the end."""
    n = TEXT_COLUMN_COUNT
    cells = [_fit(label, sum(widths[:n]))] + [""] * (n - 1)
    cells += [""] * (len(COLUMNS) - n)
    if solde_cents is not None:
        cells[-1] = format_cents_fr(int(solde_cents))
    return cells


def _totals_cells(rows: list[dict], widths: list[float],
                  closing_cents: Optional[int]) -> list[str]:
    n = TEXT_COLUMN_COUNT
    label = f"TOTAUX — {len(rows)} inscription{'s' if len(rows) != 1 else ''}"
    cells = [_fit(label, sum(widths[:n]))] + [""] * (n - 1)
    for col in COLUMNS:
        if not col.money:
            continue
        if col.key == "solde":
            cells.append(
                "" if closing_cents is None else format_cents_fr(int(closing_cents))
            )
        else:
            cells.append(format_cents_fr(
                sum(int(r.get(col.key) or 0) for r in rows)
            ))
    return cells


def _page_furniture(canvas, doc, generated: str) -> None:
    canvas.saveState()
    canvas.setFont(_FONT, 6.5)          # explicit — never inherit Helvetica
    canvas.setFillColor(_MUTED)
    canvas.drawString(doc.leftMargin, 10 * mm, generated)
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}"
    )
    canvas.restoreState()


def build_admin_journal_pdf(
    rows: list[dict], *, account_line: str, period: str, filename: str,
    opening_cents: Optional[int] = None, opening_label: str = "",
    tps_total: int = 0, tvq_total: int = 0,
    notices: Optional[list[str]] = None,
) -> Response:
    """Render the firm cash register.

    *rows* carry integer cents (``recette``/``debours`` mutually exclusive;
    ``net``/``tps``/``tvq`` present on expenses only; ``solde`` may be
    ``None`` when the route could not establish the running balance) —
    formatting happens here. *tps_total*/*tvq_total* feed the closing tax
    line, the register's fiscal payoff. *notices* are printed under the
    table — a register states its own limits rather than quietly leaving
    them out.
    """
    page_size = landscape(LEGAL)
    side = 10 * mm
    usable = page_size[0] - 2 * side
    widths = [col.ratio * usable for col in COLUMNS]

    base = getSampleStyleSheet()
    base["Normal"].fontName = _FONT
    centred = dict(alignment=TA_CENTER, parent=base["Normal"])
    title_style = ParagraphStyle(
        "AdminTitle", fontName=_FONT_BOLD, fontSize=13, leading=16,
        textColor=_INK, **centred)
    account_style = ParagraphStyle(
        "AdminAccount", fontName=_FONT_BOLD, fontSize=10, leading=13,
        textColor=_INK, **centred)
    period_style = ParagraphStyle(
        "AdminPeriod", fontSize=9, leading=12, textColor=_INK, **centred)
    tax_style = ParagraphStyle(
        "AdminTax", fontName=_FONT_BOLD, fontSize=8, leading=11,
        textColor=_INK, parent=base["Normal"])
    notice_style = ParagraphStyle(
        "AdminNotice", fontSize=7, leading=10, textColor=_MUTED,
        parent=base["Normal"])

    data: list[list[str]] = [[col.label for col in COLUMNS]]
    opening_index: Optional[int] = None
    if opening_cents is not None:
        opening_index = len(data)
        data.append(_spanned_row(opening_label, opening_cents, widths))
    data += [_row_cells(r, widths) for r in rows]
    totals_index: Optional[int] = None
    if rows:
        totals_index = len(data)
        closing = rows[-1].get("solde")
        data.append(_totals_cells(rows, widths, closing))

    style_cmds = [
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTSIZE", (0, 0), (-1, -1), _SIZE),
        ("LEADING", (0, 0), (-1, -1), _SIZE + 1.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), _PAD),
        ("RIGHTPADDING", (0, 0), (-1, -1), _PAD),
        ("GRID", (0, 0), (-1, -1), 0.25, _RULE),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("BACKGROUND", (0, 0), (-1, 0), _BAND),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
    ]
    for idx, col in enumerate(COLUMNS):
        if col.money:
            style_cmds.append(("ALIGN", (idx, 1), (idx, -1), "RIGHT"))
    for idx in (opening_index, totals_index):
        if idx is None:
            continue
        style_cmds += [
            ("FONTNAME", (0, idx), (-1, idx), _FONT_BOLD),
            ("BACKGROUND", (0, idx), (-1, idx), _BAND),
            ("SPAN", (0, idx), (TEXT_COLUMN_COUNT - 1, idx)),
            ("ALIGN", (0, idx), (0, idx), "LEFT"),
        ]

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))

    story = [
        Paragraph(escape(TITLE), title_style),
        Spacer(1, 3),
        Paragraph(escape(account_line), account_style),
        Spacer(1, 4),
        Paragraph(escape(period), period_style),
        Spacer(1, 8),
        table,
    ]
    if not rows:
        story += [
            Spacer(1, 10),
            Paragraph("Aucune inscription pour cette période.", notice_style),
        ]
    else:
        # The fiscal payoff, spelled out beside the totals row that already
        # carries it — a bookkeeper hands THIS line to the tax return.
        story += [
            Spacer(1, 8),
            Paragraph(escape(
                f"Taxes payées sur les déboursés de la période — "
                f"TPS : {format_cents_fr(int(tps_total))} · "
                f"TVQ : {format_cents_fr(int(tvq_total))}"
            ), tax_style),
        ]
    for notice in (notices or []) + [UNCLEARED_LEGEND]:
        story += [Spacer(1, 6), Paragraph(escape(notice), notice_style)]

    generated = "Généré le " + datetime.now().strftime("%Y-%m-%d %H:%M")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        leftMargin=side,
        rightMargin=side,
        title=TITLE,
        initialFontName=_FONT,
    )
    try:
        doc.build(
            story,
            onFirstPage=lambda c, d: _page_furniture(c, d, generated),
            onLaterPages=lambda c, d: _page_furniture(c, d, generated),
        )
    except Exception as exc:
        # Never log the message — it can embed supplier names.
        logger.warning(
            "admin journal PDF generation failed: %s", type(exc).__name__
        )
        buffer.close()
        return Response(
            "Erreur lors de la génération du PDF.",
            status=500,
            mimetype="text/plain; charset=utf-8",
        )
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
