"""« Journal de caisse des recettes et déboursés » — the trust cash register.

The book art. 38 RLRQ c. B-1, r. 5 requires (« Journal de caisse
recettes-déboursés en fidéicommis »), rendered on legal paper, landscape.
Its ten columns are that article's own list:

    1° recette : date (a) · somme reçue (b) · nom de la personne de qui la
       somme est reçue (c) · nom du client (d) · numéro du dossier (e) ·
       objet (f) · indication « espèces » le cas échéant (g) · solde après
       chaque inscription (h)
    2° débours : date (a) · montant (b) · bénéficiaire (c) · client (d) ·
       dossier (e) · objet (f) · mode de retrait (g) · numéro de chèque, le
       cas échéant (h) · solde (i)

« Mode » carries both 2°g and the 1°g cash indication (a « Comptant »
receipt reads as such). « N° de chèque » shows the entry's ``reference``,
the field whose own form label reads « Référence (n° chèque…) ».

A sibling of utils/journal_pdf.py rather than a parameterisation of it:
COLUMNS/TITLE are module globals there, and a second sheet cannot share them
without turning everything into arguments. Same proven shape — order lives
in ONE table keyed by ``key``, clipping is reserved to free-text columns,
money is fr-CA — plus what a cash register needs and a fee journal does not:
a carried-forward opening row and a closing reconciliation.

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

TITLE = "JOURNAL DE CAISSE DES RECETTES ET DÉBOURSÉS"
LEGAL_BASIS = (
    "(Article 38 du Règlement sur la comptabilité et les normes d'exercice "
    "professionnel des avocats)"
)
UNCLEARED_LEGEND = "* = en circulation (non compensé)"


class Column(NamedTuple):
    label: str
    ratio: float          # share of the usable width; the ratios sum to 1
    money: bool           # right-aligned, fr-CA formatted
    key: str              # the register-row key this column reads
    clip: bool = False    # may be ellipsised to fit — free text only


# The sheet, in art. 38's own reading order. ORDER LIVES HERE ALONE: every
# consumer reads ``key``, so re-ordering is a permutation of this table and
# cannot silently shift a column's content.
#
# Only the four FREE-TEXT columns may clip. A date, a mode, a cheque number,
# an amount or the running balance is never ellipsised: in a book of account
# a truncated figure or identifier is a FALSE one, and their widths are
# budgeted past the widest value they can hold.
COLUMNS: tuple[Column, ...] = (
    Column("Date", 0.070, False, "date"),
    Column("Client", 0.140, False, "client", clip=True),
    Column("N/Réf", 0.070, False, "n_ref"),
    Column("Somme reçue de / Bénéficiaire", 0.150, False, "counterparty", clip=True),
    Column("Objet", 0.130, False, "objet", clip=True),
    Column("Mode", 0.075, False, "mode"),
    Column("N° de chèque", 0.085, False, "cheque", clip=True),
    Column("Recette", 0.093, True, "recette"),
    Column("Débours", 0.093, True, "debours"),
    Column("Solde", 0.094, True, "solde"),
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
        # Marked on the DATE, and explained by the legend under the title —
        # an uncleared entry is in the book but not yet at the bank.
        out["date"] = f"{out['date']} *"
    for col in COLUMNS:
        if col.money:
            cents = row.get(col.key)
            # A recette leaves « Débours » BLANK rather than « 0,00 $ » —
            # the two columns are mutually exclusive by direction.
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


def build_trust_journal_pdf(
    rows: list[dict], *, account_line: str, period: str, filename: str,
    opening_cents: Optional[int] = None, opening_label: str = "",
    notices: Optional[list[str]] = None,
) -> Response:
    """Render the cash register.

    *rows* carry integer cents (``recette``/``debours`` mutually exclusive,
    ``None`` on the inapplicable one) — formatting happens here.
    *opening_cents* is the carried-forward balance; ``None`` prints no
    opening row (nothing precedes the period, or no period was asked for).
    *notices* are printed under the table — a register states its own limits
    rather than quietly leaving them out.
    """
    page_size = landscape(LEGAL)
    side = 10 * mm
    usable = page_size[0] - 2 * side
    widths = [col.ratio * usable for col in COLUMNS]

    base = getSampleStyleSheet()
    base["Normal"].fontName = _FONT
    centred = dict(alignment=TA_CENTER, parent=base["Normal"])
    title_style = ParagraphStyle(
        "TrustTitle", fontName=_FONT_BOLD, fontSize=13, leading=16,
        textColor=_INK, **centred)
    account_style = ParagraphStyle(
        "TrustAccount", fontName=_FONT_BOLD, fontSize=10, leading=13,
        textColor=_INK, **centred)
    basis_style = ParagraphStyle(
        "TrustBasis", fontSize=7, leading=10, textColor=_MUTED, **centred)
    period_style = ParagraphStyle(
        "TrustPeriod", fontSize=9, leading=12, textColor=_INK, **centred)
    notice_style = ParagraphStyle(
        "TrustNotice", fontSize=7, leading=10, textColor=_MUTED,
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
        Spacer(1, 2),
        Paragraph(escape(LEGAL_BASIS), basis_style),
        Spacer(1, 4),
        Paragraph(escape(period), period_style),
        Spacer(1, 8),
        table,
    ]
    if not rows:
        story += [
            Spacer(1, 10),
            Paragraph("Aucune inscription pour cette période.", basis_style),
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
        # Never log the message — it can embed client names.
        logger.warning(
            "trust journal PDF generation failed: %s", type(exc).__name__
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
