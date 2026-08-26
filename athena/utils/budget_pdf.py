"""Budget PDF builder — the two client-facing variants.

« Estimation des frais et honoraires » (portrait, no actuals — handed to
the client) and « Suivi budgétaire » (landscape, budget vs actuals). A
dedicated builder rather than export_pdf/export_pdf_grouped because the
document needs per-group SUBTOTAL rows, a grand total, the hourly-rate
block, the model sheet's disclaimer note, and a paginated firm footer —
none of which the generic table exporters offer. Styles, palette and
margins mirror utils/export_pdf.py; amounts are pre-formatted fr-CA with
utils/format_fr (never _format_value_pdf, whose « 1150.00 $ » is not a
client-grade rendering).

Font trap (documented in export_pdf.py): importing utils.export_pdf is
what registers NotoSerif/NotoSerif-Bold; SimpleDocTemplate needs
``initialFontName`` (a canvasmaker partial does NOT survive _makeCanvas),
and the footer callbacks must ``canvas.setFont("NotoSerif", …)``
explicitly — the test suite pins « NotoSerif present, Helvetica absent ».
"""

import io
import logging
from functools import partial
from xml.sax.saxutils import escape

from flask import Response
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from utils import export_pdf as _export_pdf  # noqa: F401 — registers NotoSerif
from utils import phases
from utils.format_fr import format_cents_fr, format_date_fr, format_hours_fr

logger = logging.getLogger(__name__)

VARIANT_TITLES = {
    "estimation": "Estimation des frais et honoraires",
    "suivi": "Suivi budgétaire",
}

# Verbatim from the firm's model sheet — never reworded.
DISCLAIMER = (
    "Note : Il s'agit d'une estimation basée sur notre expérience en tant "
    "qu'avocat et en fonction des travaux prévisibles à votre dossier. "
    "Les taxes de vente sont en sus."
)

_INK = colors.HexColor("#111827")
_MUTED = colors.HexColor("#6B7280")
_HEADER_INK = colors.HexColor("#374151")
_BAND = colors.HexColor("#F9FAFB")
_RULE = colors.HexColor("#D1D5DB")
_RULE_LIGHT = colors.HexColor("#E5E7EB")


def _styles() -> dict:
    base = getSampleStyleSheet()
    base["Normal"].fontName = "NotoSerif"
    return {
        "title": ParagraphStyle(
            "BudgetTitle", parent=base["Normal"], fontName="NotoSerif-Bold",
            fontSize=16, leading=20, textColor=_INK,
        ),
        "subtitle": ParagraphStyle(
            "BudgetSubtitle", parent=base["Normal"], fontSize=10, leading=13,
            textColor=_MUTED,
        ),
        "meta": ParagraphStyle(
            "BudgetMeta", parent=base["Normal"], fontSize=9, leading=12,
            textColor=_HEADER_INK,
        ),
        "group": ParagraphStyle(
            "BudgetGroup", parent=base["Normal"], fontName="NotoSerif-Bold",
            fontSize=10, leading=13, textColor=_INK,
        ),
        "cell": ParagraphStyle(
            "BudgetCell", parent=base["Normal"], fontSize=8, leading=10,
            textColor=_INK,
        ),
        "cell_num": ParagraphStyle(
            "BudgetCellNum", parent=base["Normal"], fontSize=8, leading=10,
            textColor=_INK, alignment=TA_RIGHT,
        ),
        "note": ParagraphStyle(
            "BudgetNote", parent=base["Normal"], fontSize=8, leading=11,
            textColor=_MUTED,
        ),
    }


def _columns(variant: str) -> list[tuple[str, float]]:
    if variant == "estimation":
        return [("Tâche", 0.46), ("Temps (h)", 0.14),
                ("Honoraires", 0.20), ("Frais", 0.20)]
    return [("Tâche", 0.30), ("Budget (h)", 0.09), ("Budget ($)", 0.11),
            ("Frais prévus", 0.11), ("Réalisé (h)", 0.09),
            ("Réalisé ($)", 0.11), ("Frais réels", 0.11), ("Écart ($)", 0.08)]


def _estimation_rows(group: dict) -> tuple[list[list[str]], list[str]]:
    """(data rows, subtotal row) for one phase group — pre-formatted fr-CA."""
    rows = [
        [line["label"], format_hours_fr(line["hours"]),
         format_cents_fr(line["fees_cents"]),
         format_cents_fr(line["frais_cents"]) if line["frais_cents"] else "—"]
        for line in group["lines"]
    ]
    st = group["subtotal"]
    subtotal = [
        f"Sous-total — {group['libelle']}", format_hours_fr(st["hours"]),
        format_cents_fr(st["fees_cents"]), format_cents_fr(st["frais_cents"]),
    ]
    return rows, subtotal


def _suivi_group_rows(
    group: dict, actual_by_sous: dict
) -> tuple[list[list[str]], list[str]]:
    rows = []
    tot_a_h = 0.0
    tot_a_fees = 0
    tot_a_frais = 0
    codes = [line["sous_phase"] for line in group["lines"]]
    # Consumed sub-codes of this phase that the budget did not line out
    # still appear (budget 0) — consumption is never hidden.
    phase_code = group["phase"]
    extra = sorted(
        code for code in actual_by_sous
        if phases.phase_of(code) == phase_code and code not in codes
    )
    entries = [
        (line["label"], line["hours"], line["fees_cents"],
         line["frais_cents"], line["sous_phase"])
        for line in group["lines"]
    ] + [
        (phases.SOUS_PHASE_LABELS.get(code, code), 0.0, 0, 0, code)
        for code in extra
    ]
    for label, b_hours, b_fees, b_frais, code in entries:
        a = actual_by_sous.get(code, {})
        a_h = float(a.get("hours") or 0)
        a_fees = int(a.get("fees_cents") or 0)
        a_frais = int(a.get("frais_cents") or 0)
        tot_a_h += a_h
        tot_a_fees += a_fees
        tot_a_frais += a_frais
        ecart = (b_fees + b_frais) - (a_fees + a_frais)
        rows.append([
            label, format_hours_fr(b_hours), format_cents_fr(b_fees),
            format_cents_fr(b_frais) if b_frais else "—",
            format_hours_fr(a_h), format_cents_fr(a_fees),
            format_cents_fr(a_frais) if a_frais else "—",
            format_cents_fr(ecart),
        ])
    st = group["subtotal"]
    ecart_st = (st["fees_cents"] + st["frais_cents"]) - (
        tot_a_fees + tot_a_frais
    )
    subtotal = [
        f"Sous-total — {group['libelle']}", format_hours_fr(st["hours"]),
        format_cents_fr(st["fees_cents"]), format_cents_fr(st["frais_cents"]),
        format_hours_fr(tot_a_h), format_cents_fr(tot_a_fees),
        format_cents_fr(tot_a_frais), format_cents_fr(ecart_st),
    ]
    return rows, subtotal


def _group_table(
    header_cells: list[str], rows: list[list[str]], subtotal: list[str],
    col_widths: list[float], styles: dict, first_group: bool,
) -> Table:
    data = []
    style_cmds = [
        ("FONTNAME", (0, 0), (-1, -1), "NotoSerif"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]
    offset = 0
    if first_group:
        data.append(header_cells)
        style_cmds += [
            ("FONTNAME", (0, 0), (-1, 0), "NotoSerif-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), _BAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), _HEADER_INK),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, _RULE),
        ]
        offset = 1
    data += rows
    data.append(subtotal)
    last = len(data) - 1
    style_cmds += [
        ("FONTNAME", (0, last), (-1, last), "NotoSerif-Bold"),
        ("BACKGROUND", (0, last), (-1, last), _BAND),
        ("LINEABOVE", (0, last), (-1, last), 0.5, _RULE),
    ]
    for i in range(offset, last):
        style_cmds.append(
            ("LINEBELOW", (0, i), (-1, i), 0.25, _RULE_LIGHT)
        )
    table = Table(data, colWidths=col_widths, repeatRows=0)
    table.setStyle(TableStyle(style_cmds))
    return table


def _build_story(
    variant: str, dossier: dict, budget: dict, view: dict | None,
    actuals: dict | None, styles: dict, usable_width: float,
) -> list:
    """The flowable list — separated from doc.build so tests can inspect
    the pre-formatted fr-CA strings (PDF streams are compressed)."""
    from utils.budget_math import budget_totals, group_lines_by_phase

    groups = group_lines_by_phase(
        budget.get("lines", []), int(budget.get("hourly_rate") or 0)
    )
    totals = budget_totals(budget)
    columns = _columns(variant)
    col_widths = [ratio * usable_width for _, ratio in columns]
    header_cells = [label for label, _ in columns]

    story: list = [
        Paragraph(escape(VARIANT_TITLES[variant]), styles["title"]),
        Spacer(1, 4),
        Paragraph(
            escape(
                f"{dossier.get('file_number', '')} — "
                f"{dossier.get('title', '')}"
            ),
            styles["subtitle"],
        ),
        Spacer(1, 2),
        Paragraph(
            escape(
                f"Version {budget.get('version', '')} du "
                f"{format_date_fr(budget['created_at']) if budget.get('created_at') else '—'}"
                f" · Taux horaire : "
                f"{format_cents_fr(int(budget.get('hourly_rate') or 0))} / heure"
            ),
            styles["meta"],
        ),
        Spacer(1, 8),
    ]

    actual_by_sous = (actuals or {}).get("by_sous_phase", {})
    first = True
    for group in groups:
        if variant == "estimation":
            rows, subtotal = _estimation_rows(group)
        else:
            rows, subtotal = _suivi_group_rows(group, actual_by_sous)
        flow = [
            Spacer(1, 8),
            HRFlowable(width="100%", thickness=0.4, color=_RULE),
            Spacer(1, 4),
            Paragraph(escape(group["libelle"]), styles["group"]),
            Spacer(1, 4),
            _group_table(header_cells, rows, subtotal, col_widths, styles,
                         first_group=first),
        ]
        story.append(KeepTogether(flow))
        first = False

    if variant == "suivi" and view and view.get("has_unphased"):
        un = view["unphased"]
        rows = [[
            "Temps hérité sans code de phase", "—", "—", "—",
            format_hours_fr(float(un.get("hours") or 0)),
            format_cents_fr(int(un.get("fees_cents") or 0)),
            format_cents_fr(int(un.get("frais_cents") or 0)),
            format_cents_fr(-int(un.get("total_cents") or 0)),
        ]]
        subtotal = [
            "Sous-total — Non renseignée", "—", "—", "—",
            format_hours_fr(float(un.get("hours") or 0)),
            format_cents_fr(int(un.get("fees_cents") or 0)),
            format_cents_fr(int(un.get("frais_cents") or 0)),
            format_cents_fr(-int(un.get("total_cents") or 0)),
        ]
        story.append(KeepTogether([
            Spacer(1, 8),
            HRFlowable(width="100%", thickness=0.4, color=_RULE),
            Spacer(1, 4),
            Paragraph("Non renseignée", styles["group"]),
            Spacer(1, 4),
            _group_table(header_cells, rows, subtotal, col_widths, styles,
                         first_group=False),
        ]))

    # Grand total
    if variant == "estimation":
        total_row = [[
            "Total des frais et honoraires", format_hours_fr(totals["hours"]),
            format_cents_fr(totals["fees_cents"]),
            format_cents_fr(totals["frais_cents"]),
        ]]
        grand_row = [["GRAND TOTAL", "", "",
                      format_cents_fr(totals["total_cents"])]]
    else:
        v = view or {}
        total_row = [[
            "Total des frais et honoraires",
            format_hours_fr(totals["hours"]),
            format_cents_fr(totals["fees_cents"]),
            format_cents_fr(totals["frais_cents"]),
            format_hours_fr(float(v.get("actual_total_hours") or 0)),
            format_cents_fr(int(v.get("actual_total_cents") or 0)), "",
            format_cents_fr(int(v.get("ecart_total_cents") or 0)),
        ]]
        grand_row = [["GRAND TOTAL"] + [""] * 6 +
                     [format_cents_fr(totals["total_cents"])]]

    for data, thickness in ((total_row, 0.5), (grand_row, 1.0)):
        t = Table(data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "NotoSerif-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LINEABOVE", (0, 0), (-1, 0), thickness, _RULE),
        ]))
        story += [Spacer(1, 6), t]

    story += [Spacer(1, 12), Paragraph(escape(DISCLAIMER), styles["note"])]
    if budget.get("note"):
        story += [
            Spacer(1, 4),
            Paragraph(escape(f"Hypothèses : {budget['note']}"),
                      styles["note"]),
        ]
    return story


def _footer(canvas, doc, cabinet: dict) -> None:
    """Paginated firm footer. setFont is EXPLICIT — a drawString without it
    would select Helvetica and break the font-purity test."""
    canvas.saveState()
    canvas.setFont("NotoSerif", 7)
    canvas.setFillColor(_MUTED)
    width = doc.pagesize[0]
    y = 14 * mm
    province = cabinet.get("province") or ""
    province_full = "Québec" if province in ("QC", "Québec") else province
    line1 = " ".join(filter(None, [
        cabinet.get("adresse_civique") or "",
        f", {cabinet.get('ville')}" if cabinet.get("ville") else "",
        f"({province_full})" if province_full and cabinet.get("ville") else "",
        cabinet.get("code_postal") or "",
    ])).replace(" ,", ",").strip()
    parts2 = []
    if cabinet.get("telephone"):
        parts2.append(f"Téléphone : {cabinet['telephone']}")
    if cabinet.get("telecopieur"):
        parts2.append(f"Télécopieur : {cabinet['telecopieur']}")
    if cabinet.get("courriel"):
        parts2.append(cabinet["courriel"])
    line2 = " | ".join(parts2)
    if cabinet.get("nom"):
        canvas.drawCentredString(width / 2, y + 16, cabinet["nom"])
    if line1:
        canvas.drawCentredString(width / 2, y + 8, line1)
    if line2:
        canvas.drawCentredString(width / 2, y, line2)
    canvas.drawRightString(width - 15 * mm, y, f"Page {doc.page}")
    canvas.restoreState()


def build_budget_pdf(
    *, variant: str, dossier: dict, budget: dict, view: dict | None,
    actuals: dict | None, cabinet: dict, filename: str,
) -> Response:
    """*view* is build_budget_view's rollup (unphased + totals); *actuals*
    is aggregate_actuals' raw dict — the suivi variant needs its per-sub-code
    detail, which the view deliberately rolls up. Both None for estimation."""
    page_size = LETTER if variant == "estimation" else (LETTER[1], LETTER[0])
    usable_width = page_size[0] - 30 * mm

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        topMargin=20 * mm,
        bottomMargin=28 * mm,  # room for the firm footer
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        title=VARIANT_TITLES.get(variant, "Budget"),
        initialFontName="NotoSerif",
    )
    story = _build_story(
        variant, dossier, budget, view, actuals, _styles(), usable_width
    )
    footer = partial(_footer, cabinet=cabinet)
    try:
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    except Exception as exc:
        # Never log the message — it can embed client names.
        logger.warning("budget PDF generation failed: %s", type(exc).__name__)
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
