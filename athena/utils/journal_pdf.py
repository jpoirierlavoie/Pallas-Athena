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
from typing import NamedTuple
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

class Column(NamedTuple):
    label: str
    ratio: float          # share of the usable width; the ratios sum to 1
    money: bool           # right-aligned, fr-CA formatted
    key: str              # the journal-row key this column reads
    clip: bool = False    # may be ellipsised to fit — see below


# The sheet, in reading order — Date · Client · N/Réf · N° de note, which is
# the Barreau model's own (« Date de la facture | Nom du client | No Dossier
# Références | # Facture »). The ratios spend the whole legal-landscape width,
# which is what keeps every row on a single line.
#
# ORDER LIVES HERE ALONE: every consumer reads ``key``, so re-ordering is a
# permutation of this table and cannot silently shift a column's content the
# way a parallel positional list would.
#
# CLIENT IS THE ONLY CLIPPABLE COLUMN. Everything else on this sheet either
# identifies the entry (date, file number, note number) or states an amount,
# and a truncated identifier or figure is a FALSE one — worse in a book of
# account than an untidy one. Their widths are therefore budgeted past the
# widest value they can realistically hold (a legacy « 2026-F001 (ann.) »
# note number, a seven-figure amount), and a test measures that.
COLUMNS: tuple[Column, ...] = (
    Column("Date", 0.055, False, "date"),
    Column("Client", 0.178, False, "client", clip=True),
    Column("N/Réf", 0.065, False, "reference"),
    Column("N° de note", 0.072, False, "numero"),
    Column("Honoraires", 0.070, True, "honoraires"),
    Column("Débours TX", 0.070, True, "debours_tx"),
    Column("Débours NTX", 0.070, True, "debours_ntx"),
    Column("Sous-total", 0.070, True, "sous_total"),
    Column("TPS", 0.070, True, "tps"),
    Column("TVQ", 0.070, True, "tvq"),
    Column("Total", 0.070, True, "total"),
    Column("Sommes reçues", 0.070, True, "recu"),
    Column("Solde", 0.070, True, "solde"),
)

# Derived, never hand-kept: the totals row sums exactly the money columns
# that are printed, and the label spans exactly the leading text ones.
MONEY_KEYS: tuple[str, ...] = tuple(c.key for c in COLUMNS if c.money)
TEXT_COLUMN_COUNT: int = sum(1 for c in COLUMNS if not c.money)

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
    otherwise run under the next column.

    Applied to the CLIENT column alone (``Column.clip``). Every other column
    identifies the entry or states an amount, and a truncated identifier or
    figure is a false one — see the COLUMNS note.
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


def _short_note_number(numero: str, reference: str) -> str:
    """« 2026-001-03 » → « 03 » when the N/Réf column already shows the file.

    Self-referential on purpose: only the LITERAL ``f"{reference}-"`` prefix
    of this very row is removed, matched on the whole string — which is immune
    to the dashes a file number itself contains (« 2026-001 »), where a
    ``split("-")`` would fall apart. The remainder must be all digits, so the
    number is left WHOLE whenever the prefix is not really the file:

    * a legacy ``YYYY-FNNN`` invoice — the prefix there is the year, and it
      is deducible from no other column;
    * a row with no N/Réf — the note number is then its only identification;
    * a free-form file number like « 2026 », which would otherwise turn
      « 2026-F001 » into « F001 », a false reading;
    * any prefix/reference divergence (a rename race, a DAV import's stray
      space) — the journal must then show both values as they stand.

    Confined to this sheet: everywhere else the whole number is the identity
    of the accounting artefact sent to the client, and routes/trust.py matches
    a fee transfer against it by exact string.
    """
    ref = (reference or "").strip()
    num = str(numero or "")
    if ref and num.startswith(f"{ref}-"):
        suffix = num[len(ref) + 1:]
        if suffix.isdigit():
            return suffix
    return num


def _display_values(row: dict) -> dict[str, str]:
    """Every column's cell text, keyed by column — never positional."""
    out = {c.key: str(row.get(c.key) or "") for c in COLUMNS if not c.money}
    numero = _short_note_number(row.get("numero", ""), row.get("reference", ""))
    if row.get("annulee"):
        # No status column in this sheet — mark the row rather than let a
        # voided invoice read as a live one. Its amounts stay visible (and
        # counted): the journal shows what it shows. Marked AFTER the prefix
        # is dropped, or the suffix test would fail on « … (ann.) ».
        numero = f"{numero} (ann.)"
    out["numero"] = numero
    for col in COLUMNS:
        if col.money:
            out[col.key] = format_cents_fr(int(row.get(col.key) or 0))
    return out


def _row_cells(row: dict, widths: list[float]) -> list[str]:
    values = _display_values(row)
    return [
        _fit(values[col.key], width) if col.clip else values[col.key]
        for col, width in zip(COLUMNS, widths)
    ]


def _totals_cells(rows: list[dict], widths: list[float]) -> list[str]:
    label = f"TOTAL — {len(rows)} facture{'s' if len(rows) != 1 else ''}"
    n = TEXT_COLUMN_COUNT
    # The label spans the leading text columns (SPAN below), so measure it
    # against their combined width rather than the first one alone.
    cells = [_fit(label, sum(widths[:n]))] + [""] * (n - 1)
    cells += [
        format_cents_fr(sum(int(r.get(k) or 0) for r in rows))
        for k in MONEY_KEYS
    ]
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


def build_journal_pdf(
    rows: list[dict], *, subtitle: str, filename: str
) -> Response:
    """Render the fee journal. *rows* carry cents; formatting happens here."""
    page_size = landscape(LEGAL)
    side = 10 * mm
    usable = page_size[0] - 2 * side
    widths = [col.ratio * usable for col in COLUMNS]

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

    data: list[list[str]] = [[col.label for col in COLUMNS]]
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
    for idx, col in enumerate(COLUMNS):
        if col.money:
            style_cmds.append(("ALIGN", (idx, 1), (idx, -1), "RIGHT"))
    if rows:
        style_cmds += [
            ("FONTNAME", (0, last), (-1, last), _FONT_BOLD),
            ("BACKGROUND", (0, last), (-1, last), _BAND),
            ("SPAN", (0, last), (TEXT_COLUMN_COUNT - 1, last)),
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
