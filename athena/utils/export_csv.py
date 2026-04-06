"""CSV export utility for all data tables."""

import csv
import io
from datetime import datetime
from typing import Any

from flask import Response


def export_csv(
    rows: list[dict],
    columns: list[tuple[str, str]],
    filename: str = "export.csv",
    date_format: str = "%Y-%m-%d",
    cents_fields: list[str] | None = None,
    hours_fields: list[str] | None = None,
) -> Response:
    """Generate a CSV response from a list of dicts.

    Args:
        rows: List of data dicts to export.
        columns: Ordered list of (field_key, column_header) tuples.
        filename: Download filename.
        date_format: strftime format for datetime fields.
        cents_fields: Field keys that contain integer cents — convert to dollars.
        hours_fields: Field keys that contain float hours — format to 1 decimal.
    """
    cents_set = set(cents_fields or [])
    hours_set = set(hours_fields or [])

    output = io.StringIO()
    # UTF-8 BOM for Excel compatibility
    output.write("\ufeff")

    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    # Header row
    writer.writerow([label for _, label in columns])

    # Data rows
    for row in rows:
        csv_row = []
        for key, _ in columns:
            val = row.get(key, "")
            val = _format_value(val, key, date_format, cents_set, hours_set)
            csv_row.append(val)
        writer.writerow(csv_row)

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


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
    return str(val)


def prepare_export_rows(
    rows: list[dict],
    label_maps: dict[str, dict[str, str]] | None = None,
) -> list[dict]:
    """Pre-process rows for export: apply label mappings.

    Args:
        label_maps: {field_key: {raw_value: display_label}, ...}
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
