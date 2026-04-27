"""PDF report generation using reportlab."""

import io
from datetime import datetime
from typing import Any

from flask import Response

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    HRFlowable,
    KeepTogether,
)


def export_pdf(
    rows: list[dict],
    columns: list[tuple[str, str, float]],
    title: str = "Rapport",
    subtitle: str = "",
    filename: str = "rapport.pdf",
    cents_fields: list[str] | None = None,
    hours_fields: list[str] | None = None,
    date_format: str = "%Y-%m-%d",
    landscape: bool = False,
) -> Response:
    """Generate a PDF report response.

    Args:
        rows: List of data dicts.
        columns: List of (field_key, header_label, width_ratio) tuples.
        title: Report title.
        subtitle: Optional subtitle.
        filename: Download filename.
        cents_fields: Fields containing integer cents.
        hours_fields: Fields containing float hours.
        date_format: strftime format for dates.
        landscape: If True, use landscape orientation.
    """
    cents_set = set(cents_fields or [])
    hours_set = set(hours_fields or [])

    buffer = io.BytesIO()
    page_size = LETTER if not landscape else (LETTER[1], LETTER[0])

    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    elements = []

    # ── Title ──────────────────────────────────────────────────
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=4,
        textColor=colors.HexColor("#111827"),
    )
    elements.append(Paragraph(title, title_style))

    if subtitle:
        sub_style = ParagraphStyle(
            "ReportSubtitle",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#6B7280"),
            spaceAfter=4,
        )
        elements.append(Paragraph(subtitle, sub_style))

    # Generation timestamp
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    ts_style = ParagraphStyle(
        "Timestamp",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#9CA3AF"),
        spaceAfter=12,
    )
    elements.append(Paragraph(f"Généré le {now_str}", ts_style))
    elements.append(
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E5E7EB"))
    )
    elements.append(Spacer(1, 8))

    # ── Table ──────────────────────────────────────────────────
    usable_width = page_size[0] - 30 * mm
    total_ratio = sum(c[2] for c in columns)
    col_widths = [(c[2] / total_ratio) * usable_width for c in columns]

    header_cells = [c[1] for c in columns]

    cell_style = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
    )
    header_style = ParagraphStyle(
        "HeaderCell",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#374151"),
        fontName="Helvetica-Bold",
    )

    table_data = [[Paragraph(h, header_style) for h in header_cells]]

    for row in rows:
        data_row = []
        for key, _, _ in columns:
            val = row.get(key, "")
            formatted = _format_value_pdf(val, key, date_format, cents_set, hours_set)
            data_row.append(Paragraph(str(formatted), cell_style))
        table_data.append(data_row)

    if not rows:
        empty_style = ParagraphStyle(
            "Empty", parent=cell_style, textColor=colors.HexColor("#9CA3AF")
        )
        table_data.append(
            [Paragraph("Aucune donnée.", empty_style)]
            + [Paragraph("", cell_style)] * (len(columns) - 1)
        )

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                # Header
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F9FAFB")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#374151")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                # Data rows
                ("FONTSIZE", (0, 1), (-1, -1), 8),
                ("TOPPADDING", (0, 1), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
                # Grid
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#D1D5DB")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
                # Alternating row colors
                *[
                    ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F9FAFB"))
                    for i in range(2, len(table_data), 2)
                ],
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    elements.append(table)

    # ── Row count footer ───────────────────────────────────────
    elements.append(Spacer(1, 12))
    count_style = ParagraphStyle(
        "RowCount",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#6B7280"),
    )
    elements.append(
        Paragraph(f"{len(rows)} entrée{'s' if len(rows) != 1 else ''}", count_style)
    )

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def export_pdf_grouped(
    groups: list[tuple[str, list[dict]]],
    columns: list[tuple[str, str, float]],
    title: str = "Rapport",
    subtitle: str = "",
    filename: str = "rapport.pdf",
    cents_fields: list[str] | None = None,
    hours_fields: list[str] | None = None,
    date_format: str = "%Y-%m-%d",
    landscape: bool = False,
) -> Response:
    """Generate a PDF report whose rows are grouped under section headers.

    Args:
        groups: Ordered list of (group_label, rows). One section per entry.
        columns: List of (field_key, header_label, width_ratio) tuples.
            The column-header band is rendered once at the top of the report.
    """
    cents_set = set(cents_fields or [])
    hours_set = set(hours_fields or [])

    buffer = io.BytesIO()
    page_size = LETTER if not landscape else (LETTER[1], LETTER[0])

    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    elements: list[Any] = []

    # ── Title / subtitle / timestamp (matches export_pdf) ──────
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=16,
        spaceAfter=4,
        textColor=colors.HexColor("#111827"),
    )
    elements.append(Paragraph(title, title_style))

    if subtitle:
        sub_style = ParagraphStyle(
            "ReportSubtitle",
            parent=styles["Normal"],
            fontSize=10,
            textColor=colors.HexColor("#6B7280"),
            spaceAfter=4,
        )
        elements.append(Paragraph(subtitle, sub_style))

    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    ts_style = ParagraphStyle(
        "Timestamp",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#9CA3AF"),
        spaceAfter=12,
    )
    elements.append(Paragraph(f"Généré le {now_str}", ts_style))
    elements.append(
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E5E7EB"))
    )
    elements.append(Spacer(1, 8))

    # ── Column geometry ────────────────────────────────────────
    usable_width = page_size[0] - 30 * mm
    total_ratio = sum(c[2] for c in columns)
    col_widths = [(c[2] / total_ratio) * usable_width for c in columns]

    cell_style = ParagraphStyle(
        "Cell",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
    )
    header_style = ParagraphStyle(
        "HeaderCell",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#374151"),
        fontName="Helvetica-Bold",
    )
    group_header_style = ParagraphStyle(
        "GroupHeader",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#111827"),
        fontName="Helvetica-Bold",
    )

    # ── Single column-header band at top of report ────────────
    header_row = [Paragraph(c[1], header_style) for c in columns]
    header_table = Table([header_row], colWidths=col_widths)
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F9FAFB")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#D1D5DB")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(header_table)

    total_row_count = sum(len(rows) for _, rows in groups)

    # ── Per-group sections ─────────────────────────────────────
    if total_row_count == 0:
        empty_style = ParagraphStyle(
            "Empty", parent=cell_style, textColor=colors.HexColor("#9CA3AF")
        )
        empty_table = Table(
            [
                [Paragraph("Aucune donnée.", empty_style)]
                + [Paragraph("", cell_style)] * (len(columns) - 1)
            ],
            colWidths=col_widths,
        )
        empty_table.setStyle(
            TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        elements.append(empty_table)
    else:
        for group_label, rows in groups:
            if not rows:
                continue

            data_rows = []
            for row in rows:
                data_row = []
                for key, _, _ in columns:
                    val = row.get(key, "")
                    formatted = _format_value_pdf(
                        val, key, date_format, cents_set, hours_set
                    )
                    data_row.append(Paragraph(str(formatted), cell_style))
                data_rows.append(data_row)

            group_table = Table(data_rows, colWidths=col_widths)
            group_table.setStyle(
                TableStyle(
                    [
                        ("FONTSIZE", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
                        *[
                            ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F9FAFB"))
                            for i in range(1, len(data_rows), 2)
                        ],
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )

            group_flow = [
                Spacer(1, 10),
                HRFlowable(
                    width="100%", thickness=0.4, color=colors.HexColor("#D1D5DB")
                ),
                Spacer(1, 4),
                Paragraph(group_label, group_header_style),
                Spacer(1, 6),
                group_table,
            ]
            elements.append(KeepTogether(group_flow))

    # ── Row count footer ───────────────────────────────────────
    elements.append(Spacer(1, 12))
    count_style = ParagraphStyle(
        "RowCount",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#6B7280"),
    )
    elements.append(
        Paragraph(
            f"{total_row_count} entrée{'s' if total_row_count != 1 else ''}",
            count_style,
        )
    )

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _format_value_pdf(
    val: Any,
    key: str,
    date_format: str,
    cents_fields: set[str],
    hours_fields: set[str],
) -> str:
    """Format a value for PDF cell display."""
    if val is None:
        return ""
    if key in cents_fields and isinstance(val, (int, float)):
        return f"{val / 100:.2f} $"
    if key in hours_fields and isinstance(val, (int, float)):
        return f"{val:.1f}"
    if isinstance(val, datetime):
        return val.strftime(date_format)
    if isinstance(val, bool):
        return "Oui" if val else "Non"
    if isinstance(val, list):
        if val and isinstance(val[0], dict) and "name" in val[0]:
            return ", ".join(v.get("name", "") for v in val)
        return ", ".join(str(v) for v in val)
    return str(val)
