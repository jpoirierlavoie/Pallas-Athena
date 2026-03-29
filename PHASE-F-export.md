# PHASE F — Data Export (CSV + PDF Reports)

Read CLAUDE.md for project context. This phase adds CSV and PDF export capabilities to all major data tables in the application.

## Context

All exportable modules: parties (contacts), dossiers, time entries, expenses, invoices, hearings, tasks, protocol steps, documents (metadata), and notes (if Phase D is implemented).

CSV export is straightforward — stream a response with `text/csv` content type. PDF export uses `reportlab` for server-side generation of structured tabular reports.

## Step 1 — Add `reportlab` to Requirements

In `requirements.txt`, add:
```
reportlab==4.*
```

`reportlab` is pure Python — no system-level dependencies. It works on App Engine Standard without issues.

## Step 2 — Create `utils/export_csv.py`

```python
"""CSV export utility for all data tables."""

import csv
import io
from datetime import datetime, timezone
from typing import Any

from flask import Response


def export_csv(
    rows: list[dict],
    columns: list[tuple[str, str]],  # [(field_key, display_label), ...]
    filename: str = "export.csv",
    date_format: str = "%Y-%m-%d",
    cents_fields: list[str] | None = None,
    hours_fields: list[str] | None = None,
) -> Response:
    """Generate a CSV response from a list of dicts.

    Args:
        rows: List of data dicts to export.
        columns: Ordered list of (field_key, column_header) tuples.
                 Only these fields are included in the output.
        filename: Download filename.
        date_format: strftime format for datetime fields.
        cents_fields: Field keys that contain integer cents — convert to dollars.
        hours_fields: Field keys that contain float hours — format to 1 decimal.

    Returns:
        A Flask Response with text/csv content type and Content-Disposition header.
    """
    cents_fields = set(cents_fields or [])
    hours_fields = set(hours_fields or [])

    output = io.StringIO()
    # UTF-8 BOM for Excel compatibility
    output.write('\ufeff')

    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    # Header row
    writer.writerow([label for _, label in columns])

    # Data rows
    for row in rows:
        csv_row = []
        for key, _ in columns:
            val = row.get(key, "")
            val = _format_value(val, key, date_format, cents_fields, hours_fields)
            csv_row.append(val)
        writer.writerow(csv_row)

    response = Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
    return response


def _format_value(
    val: Any,
    key: str,
    date_format: str,
    cents_fields: set[str],
    hours_fields: set[str],
) -> str:
    """Format a single cell value for CSV output."""
    if val is None:
        return ""
    if key in cents_fields and isinstance(val, (int, float)):
        return f"{val / 100:.2f}"
    if key in hours_fields and isinstance(val, (int, float)):
        return f"{val:.1f}"
    if isinstance(val, datetime):
        return val.strftime(date_format)
    if isinstance(val, bool):
        return "Oui" if val else "Non"
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if isinstance(val, dict):
        # For nested dicts like clients [{id, name}], extract names
        if all(isinstance(v, dict) and "name" in v for v in val) if isinstance(val, list) else False:
            return ", ".join(v["name"] for v in val)
        return str(val)
    return str(val)
```

## Step 3 — Create `utils/export_pdf.py`

```python
"""PDF report generation using reportlab."""

import io
from datetime import datetime
from typing import Any

from flask import Response

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    HRFlowable,
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER


def export_pdf(
    rows: list[dict],
    columns: list[tuple[str, str, float]],  # [(field_key, header, col_width_ratio), ...]
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
                 width_ratio is relative (e.g., 2.0 for double-width column).
        title: Report title (large heading at top).
        subtitle: Optional subtitle (e.g., "Dossier 2025-001" or date range).
        filename: Download filename.
        cents_fields: Fields containing integer cents.
        hours_fields: Fields containing float hours.
        date_format: strftime format for dates.
        landscape: If True, use landscape orientation.

    Returns:
        Flask Response with application/pdf content type.
    """
    cents_fields = set(cents_fields or [])
    hours_fields = set(hours_fields or [])

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
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E5E7EB")))
    elements.append(Spacer(1, 8))

    # ── Table ──────────────────────────────────────────────────
    usable_width = page_size[0] - 30 * mm
    total_ratio = sum(c[2] for c in columns)
    col_widths = [(c[2] / total_ratio) * usable_width for c in columns]

    # Header row
    header_cells = [c[1] for c in columns]

    # Data rows
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
            formatted = _format_value_pdf(val, key, date_format, cents_fields, hours_fields)
            data_row.append(Paragraph(str(formatted), cell_style))
        table_data.append(data_row)

    if not rows:
        # Empty state row
        empty_style = ParagraphStyle("Empty", parent=cell_style, textColor=colors.HexColor("#9CA3AF"))
        table_data.append([Paragraph("Aucune donnée.", empty_style)] + [Paragraph("", cell_style)] * (len(columns) - 1))

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
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
        *[("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F9FAFB")) for i in range(2, len(table_data), 2)],
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))

    elements.append(table)

    # ── Row count footer ───────────────────────────────────────
    elements.append(Spacer(1, 12))
    count_style = ParagraphStyle(
        "RowCount",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#6B7280"),
    )
    elements.append(Paragraph(f"{len(rows)} entrée{'s' if len(rows) != 1 else ''}", count_style))

    # Build PDF
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


def _format_value_pdf(val, key, date_format, cents_fields, hours_fields) -> str:
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
```

## Step 4 — Add Export Routes to Each Module

For each module, add two export routes: CSV and PDF. The pattern is identical across modules — only the column definitions and data source differ.

### Pattern (apply to each module):

```python
@bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export data as CSV."""
    from utils.export_csv import export_csv
    # Fetch data with same filters as list view
    items = list_items(...)
    columns = [
        ("field_key", "Column Header"),
        ...
    ]
    return export_csv(
        rows=items,
        columns=columns,
        filename="module_export_YYYY-MM-DD.csv",
        cents_fields=["amount", "rate"],
    )

@bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export data as PDF report."""
    from utils.export_pdf import export_pdf
    items = list_items(...)
    columns = [
        ("field_key", "Column Header", 1.5),  # width ratio
        ...
    ]
    return export_pdf(
        rows=items,
        columns=columns,
        title="Report Title",
        filename="module_rapport_YYYY-MM-DD.pdf",
        cents_fields=["amount", "rate"],
    )
```

### Module-specific column definitions:

**Parties** (`routes/parties.py`):
```python
# CSV + PDF columns
EXPORT_COLUMNS = [
    ("_display_name", "Nom", 2.0),
    ("contact_role", "Rôle", 1.0),           # Use ROLE_LABELS lookup
    ("email", "Courriel", 1.5),
    ("phone_cell", "Cellulaire", 1.0),
    ("phone_work", "Tél. professionnel", 1.0),
    ("organization", "Organisation", 1.5),
    ("address_city", "Ville", 1.0),
]
```
Pre-process: attach `_display_name` to each row before export.
For the `contact_role` column, map the key to its French label using `ROLE_LABELS`.

**Dossiers** (`routes/dossiers.py`):
```python
EXPORT_COLUMNS = [
    ("file_number", "N° dossier", 1.0),
    ("title", "Titre", 2.0),
    ("_client_names", "Client(s)", 1.5),      # Join client names
    ("matter_type", "Type", 1.0),             # MATTER_TYPE_LABELS lookup
    ("court", "Tribunal", 1.0),
    ("status", "Statut", 0.8),                # STATUS_LABELS lookup
    ("opened_date", "Ouverture", 1.0),
]
```
Pre-process: compute `_client_names` = ", ".join(c["name"] for c in d.get("clients", [])).
Map `matter_type` and `status` to their French labels.

**Time Entries** (`routes/time_expenses.py`):
```python
TIME_EXPORT_COLUMNS = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.5),
    ("hours", "Heures", 0.6),
    ("rate", "Taux", 0.8),
    ("amount", "Montant", 0.8),
    ("billable", "Facturable", 0.6),
    ("invoiced", "Facturé", 0.6),
]
```
`cents_fields=["rate", "amount"]`, `hours_fields=["hours"]`

**Expenses** (`routes/time_expenses.py`):
```python
EXPENSE_EXPORT_COLUMNS = [
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("description", "Description", 2.5),
    ("category", "Catégorie", 1.0),           # CATEGORY_LABELS lookup
    ("amount", "Montant", 0.8),
    ("taxable", "Taxable", 0.6),
    ("invoiced", "Facturé", 0.6),
]
```

**Hearings** (`routes/hearings.py`):
```python
EXPORT_COLUMNS = [
    ("start_datetime", "Date", 1.0),
    ("title", "Titre", 2.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("hearing_type", "Type", 1.0),
    ("location", "Lieu", 1.5),
    ("status", "Statut", 0.8),
]
```

**Tasks** (`routes/tasks.py`):
```python
EXPORT_COLUMNS = [
    ("title", "Titre", 2.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("priority", "Priorité", 0.8),
    ("category", "Catégorie", 1.0),
    ("status", "Statut", 0.8),
    ("due_date", "Échéance", 1.0),
]
```

**Invoices** (`routes/invoices.py`):
```python
EXPORT_COLUMNS = [
    ("invoice_number", "N° facture", 1.0),
    ("date", "Date", 1.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("client_name", "Client", 1.5),
    ("subtotal", "Sous-total", 0.8),
    ("gst_amount", "TPS", 0.6),
    ("qst_amount", "TVQ", 0.6),
    ("total", "Total", 0.8),
    ("status", "Statut", 0.8),
]
```

**Notes** (`routes/notes.py` — if Phase D is implemented):
```python
EXPORT_COLUMNS = [
    ("created_at", "Date", 1.0),
    ("title", "Titre", 2.0),
    ("dossier_file_number", "Dossier", 1.0),
    ("category", "Catégorie", 1.0),
    ("content", "Contenu", 3.0),
]
```

## Step 5 — Pre-processing Helper

Create a helper to map enum keys to French labels before export:

```python
# In utils/export_csv.py (or a shared helper)
def prepare_export_rows(
    rows: list[dict],
    label_maps: dict[str, dict[str, str]] | None = None,
) -> list[dict]:
    """Pre-process rows for export: apply label mappings.

    Args:
        label_maps: {field_key: {raw_value: display_label}, ...}
                    e.g., {"status": {"actif": "Actif", "fermé": "Fermé"}}
    """
    if not label_maps:
        return rows
    processed = []
    for row in rows:
        r = dict(row)
        for field, mapping in label_maps.items():
            if field in r and r[field] in mapping:
                r[field] = mapping[r[field]]
        processed.append(r)
    return processed
```

## Step 6 — Add Export Buttons to List Pages

Add export buttons to each list view's header area. Pattern:

```jinja2
<div class="flex items-center gap-2">
  {# Existing "New" button #}
  <a href="..." class="...">Nouveau ...</a>

  {# Export dropdown #}
  <div x-data="{ exportOpen: false }" class="relative">
    <button @click="exportOpen = !exportOpen"
            class="px-3 py-2 text-sm font-medium text-gray-600 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 inline-flex items-center gap-1.5">
      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"/>
      </svg>
      Exporter
    </button>
    <div x-show="exportOpen" @click.outside="exportOpen = false" x-cloak
         class="absolute right-0 mt-1 bg-white rounded-lg shadow-lg border border-gray-200 py-1 w-36 z-10">
      <a href="{{ url_for('MODULE.export_csv_route') }}"
         class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100">
        CSV
      </a>
      <a href="{{ url_for('MODULE.export_pdf_route') }}"
         class="block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100">
        PDF
      </a>
    </div>
  </div>
</div>
```

Apply to: parties/list.html, dossiers/list.html, time_expenses/list.html, hearings/list.html, tasks/list.html, invoices/list.html, protocols/list.html, documents/list.html, notes/list.html (if Phase D done).

The export routes should respect the same filters as the current view (pass query params through):
```python
@bp.route("/export/csv")
@login_required
def export_csv_route():
    # Read the same filter params as the list view
    status_filter = request.args.get("status", "")
    search = request.args.get("q", "").strip()
    ...
    items = list_items(status_filter=..., search=...)
    ...
```

## Step 7 — Filename Generation

Use a consistent filename pattern including the current date:
```python
from datetime import datetime
date_str = datetime.now().strftime("%Y-%m-%d")
filename = f"parties_{date_str}.csv"  # or .pdf
```

## Testing Checklist
- [ ] CSV export for each module downloads correctly with UTF-8 BOM
- [ ] CSV opens in Excel/LibreOffice without encoding issues (French accents preserved)
- [ ] CSV column headers are in French
- [ ] CSV monetary values are in dollars (not cents)
- [ ] CSV dates are formatted correctly
- [ ] CSV boolean fields show "Oui"/"Non"
- [ ] CSV enum fields show French labels (not raw keys)
- [ ] PDF export for each module downloads correctly
- [ ] PDF title and subtitle display correctly
- [ ] PDF table is readable — no truncated columns
- [ ] PDF handles empty data sets gracefully (shows "Aucune donnée")
- [ ] PDF handles large data sets (100+ rows) — proper pagination
- [ ] Export respects current filters (status, search, date range)
- [ ] Export buttons appear on all list pages
- [ ] Export dropdown closes on click outside
- [ ] `reportlab` deploys to App Engine without errors
