"""Report rows for the configuration checks — one accumulator, two renderings.

Lifted out of ``scripts/check_config.py``, whose ``Report.emit`` and
``Report.section`` called ``print()`` directly. That coupling is what made the
knowledge in that script unreachable from anywhere but a terminal — and
CLAUDE.md records the cost: the only thing that reports a missing
``cf-origin-secret`` is that script, and nothing runs it, so the whole edge
defence was off for months with nothing signalling it.

So printing moves OUT of accumulation. A ``Report`` collects :class:`Row`
objects and forwards each to an optional ``printer`` callable; the CLI passes
one and gets byte-identical output, while a Flask route passes none and reads
``report.rows``.

Stdlib only, and deliberately: like ``deployment_inventory``, this must be
importable by a route, by a script and by a test with no credentials.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
LEVELS = (OK, WARN, FAIL)

# The exact strings the CLI has always printed. Kept here so `render_text`
# and the live printer cannot drift apart.
SYMBOL = {OK: "  ok ", WARN: "warn ", FAIL: "FAIL "}


@dataclass(frozen=True)
class Row:
    """One finding.

    ``detail`` carries machine-readable context for a structured consumer (a
    web page, a JSON report). It must never carry a secret VALUE — only its
    shape. The rule the checker states about payloads applies here too.
    """

    section: str
    level: str
    message: str
    detail: dict = field(default_factory=dict)


class Report:
    def __init__(self, printer: Optional[Callable[[str], None]] = None) -> None:
        self._printer = printer
        self._section = ""
        self.rows: list[Row] = []

    def section(self, title: str) -> None:
        self._section = title
        if self._printer is not None:
            self._printer("")
            self._printer(title)
            self._printer("-" * len(title))

    def emit(self, level: str, message: str, **detail) -> None:
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r}")
        self.rows.append(Row(self._section, level, message, detail))
        if self._printer is not None:
            self._printer(f"  [{SYMBOL[level]}] {message}")

    def counts(self) -> dict:
        c = {OK: 0, WARN: 0, FAIL: 0}
        for row in self.rows:
            c[row.level] += 1
        return c

    def worst(self) -> str:
        """The most severe level present — what a badge should show."""
        levels = {row.level for row in self.rows}
        for level in (FAIL, WARN, OK):
            if level in levels:
                return level
        return OK

    def sections(self) -> list[tuple[str, list[Row]]]:
        """Rows grouped by section, in first-seen order (for rendering)."""
        order: list[str] = []
        grouped: dict[str, list[Row]] = {}
        for row in self.rows:
            if row.section not in grouped:
                grouped[row.section] = []
                order.append(row.section)
            grouped[row.section].append(row)
        return [(name, grouped[name]) for name in order]


def render_text(report: Report) -> str:
    """The CLI rendering, byte-compatible with the historical output."""
    lines: list[str] = []
    for name, rows in report.sections():
        if name:
            lines.append("")
            lines.append(name)
            lines.append("-" * len(name))
        for row in rows:
            lines.append(f"  [{SYMBOL[row.level]}] {row.message}")
    return "\n".join(lines)


def render_json(report: Report) -> dict:
    return {
        "worst": report.worst(),
        "counts": report.counts(),
        "sections": [
            {
                "title": name,
                "rows": [
                    {"level": r.level, "message": r.message, "detail": r.detail}
                    for r in rows
                ],
            }
            for name, rows in report.sections()
        ],
    }


__all__ = [
    "FAIL",
    "LEVELS",
    "OK",
    "SYMBOL",
    "WARN",
    "Report",
    "Row",
    "render_json",
    "render_text",
]
