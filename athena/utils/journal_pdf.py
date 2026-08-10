"""« Journal des honoraires » — the Barreau's fee-journal sheet.

Modelled on the Barreau du Québec's own template: legal paper, landscape,
one row per invoice, thirteen columns. Two deliberate departures from that
sheet, per the practitioner (2026-08-10): its top grouping band
(Facturation / Détail de la facture / Paiement) is dropped as noise, and its
historical rate footnotes are not reproduced — they describe the pre-2013
QST regimes this application never issued under, and each invoice carries
the rate it was actually issued at.

A dedicated builder rather than ``utils.export_pdf``: that one wraps cells
in Paragraphs (rows that fold onto themselves — precisely what a journal
must not do), formats money as « 1150.00 $ », and only knows LETTER. Here
every cell is a plain string clipped to its column, so a row can neither
wrap nor overflow into its neighbour, and money is fr-CA throughout.

Font trap (documented in export_pdf.py): importing utils.export_pdf is what
registers NotoSerif; SimpleDocTemplate needs ``initialFontName``, and the
page callbacks must call ``canvas.setFont`` explicitly or the deploy gate's
« no Helvetica » assertion fires.
"""

import io
import logging
from datetime import datetime
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

TITLE = "JOURNAL DES HONORAIRES"

# (header label, ratio, money?) — ratios sum to 1 and are spent over the full
# legal-landscape width, which is what keeps every row on a single line.
# Header labels carry their own line breaks; DATA cells never do.
COLUMNS: tuple[tuple[str, float, bool], ...] = (
    ("Date", 0.055, False),
    ("N/Réf", 0.055, False),
    ("Client", 0.145, False),
    ("N° de note", 0.075, False),
    ("Honoraires", 0.075, True),
    ("Débours\ntaxables", 0.075, True),
    ("Débours non\ntaxables", 0.080, True),
    ("Sous-total", 0.075, True),
    ("TPS", 0.065, True),
    ("TVQ", 0.065, True),
    ("Total", 0.075, True),
    ("Sommes\nreçues", 0.075, True),
    ("Solde", 0.085, True),
)

# The money keys of a journal row, in column order — the totals row sums
# exactly these, so what is printed is what is added.
MONEY_KEYS = (
    "honoraires", "debours_tx", "debours_ntx", "sous_total",
    "tps", "tvq", "total", "recu", "solde",
)

_FONT = "NotoSerif"
_FONT_BOLD = "NotoSerif-Bold"
_SIZE = 7
_PAD = 3  # left/right cell padding, mirrored in the width budget

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6B7280")
_BAND = colors.HexColor("#F9FAFB")
_RULE = colors.HexColor("#D1D5DB")


def _fit(text: str, width: float) -> str:
    """Clip *text* to *width*, ellipsised — never wrapped, never overflowing.

    « Les lignes ne devraient pas plier sur elles-mêmes » (practitioner,
    2026-08-10): plain-string cells do not wrap, but a long client name would
    otherwise run under the next column. Only that column ever realistically
    clips; every money column is budgeted well beyond its widest value.
    """
    text = str(text or "")
    usable = width - 2 * _PAD
    if pdfmetrics.stringWidth(text, _FONT, _SIZE) <= usable:
        return text
    ellipsis = "…"
    while text and pdfmetrics.stringWidth(
        text + ellipsis, _FONT, _SIZE
    ) > usable:
        text = text[:-1]
    return text + ellipsis


def _row_cells(row: dict, widths: list[float]) -> list[str]:
    numero = row.get("numero", "")
    if row.get("annulee"):
        # No status column in this sheet — mark the row rather than let a
        # voided invoice read as a live one. Its amounts stay visible (and
        # counted): the journal shows what it shows.
        numero = f"{numero} (ann.)"
    values = [
        row.get("date", ""),
        row.get("reference", ""),
        row.get("client", ""),
        numero,
    ] + [format_cents_fr(int(row.get(k) or 0)) for k in MONEY_KEYS]
    return [_fit(v, w) for v, w in zip(values, widths)]


def _totals_cells(rows: list[dict], widths: list[float]) -> list[str]:
    label = f"TOTAL — {len(rows)} facture{'s' if len(rows) != 1 else ''}"
    sums = [sum(int(r.get(k) or 0) for r in rows) for k in MONEY_KEYS]
    cells = [label, "", "", ""] + [format_cents_fr(s) for s in sums]
    # The label spans the four text columns (SPAN below), so measure it
    # against their combined width rather than the first one alone.
    fitted = [_fit(cells[0], sum(widths[:4]))] + ["", "", ""]
    fitted += [_fit(c, w) for c, w in zip(cells[4:], widths[4:])]
    return fitted


def _page_furniture(canvas, doc, generated: str) -> None:
    canvas.saveState()
    canvas.setFont(_FONT, 6.5)          # explicit — never inherit Helvetica
    canvas.setFillColor(_MUTED)
    canvas.drawString(doc.leftMargin, 10 * mm, generated)
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin, 10 * mm, f"Page {doc.page}"
    )
    canvas.restoreState()


def build_journal_pdf(
    rows: list[dict], *, subtitle: str, filename: str
) -> Response:
    """Render the fee journal. *rows* carry cents; formatting happens here."""
    page_size = landscape(LEGAL)
    side = 10 * mm
    usable = page_size[0] - 2 * side
    widths = [ratio * usable for _, ratio, _ in COLUMNS]

    base = getSampleStyleSheet()
    base["Normal"].fontName = _FONT
    title_style = ParagraphStyle(
        "JournalTitle", parent=base["Normal"], fontName=_FONT_BOLD,
        fontSize=13, leading=16, alignment=TA_CENTER, textColor=_INK,
    )
    subtitle_style = ParagraphStyle(
        "JournalSubtitle", parent=base["Normal"], fontSize=8, leading=11,
        alignment=TA_CENTER, textColor=_MUTED,
    )

    data: list[list[str]] = [[label for label, _, _ in COLUMNS]]
    data += [_row_cells(r, widths) for r in rows]
    if rows:
        data.append(_totals_cells(rows, widths))

    last = len(data) - 1
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
        # Header band
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("BACKGROUND", (0, 0), (-1, 0), _BAND),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
    ]
    # Money columns right-aligned in the body.
    for idx, (_, _, is_money) in enumerate(COLUMNS):
        if is_money:
            style_cmds.append(("ALIGN", (idx, 1), (idx, -1), "RIGHT"))
    if rows:
        style_cmds += [
            ("FONTNAME", (0, last), (-1, last), _FONT_BOLD),
            ("BACKGROUND", (0, last), (-1, last), _BAND),
            ("SPAN", (0, last), (3, last)),
            ("ALIGN", (0, last), (0, last), "LEFT"),
        ]

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style_cmds))

    story = [Paragraph(escape(TITLE), title_style), Spacer(1, 4)]
    if subtitle:
        story += [Paragraph(escape(subtitle), subtitle_style)]
    story += [Spacer(1, 8), table]
    if not rows:
        story += [
            Spacer(1, 10),
            Paragraph("Aucune facture pour ces critères.", subtitle_style),
        ]

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
        logger.warning("journal PDF generation failed: %s", type(exc).__name__)
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
