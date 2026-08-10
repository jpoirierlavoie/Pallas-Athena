"""Markdown → OOXML block-sequence converter (note printing, kind « note »).

Pure module — no Firestore, no Flask. Converts a note's Markdown body into a
sequence of ``<w:p>``/``<w:tbl>`` block elements that :func:`utils.docx_fill.
fill_docx` splices in place of the host paragraph carrying ``{{note.contenu}}``
(the ``rich_values`` hook).

Fidelity target: the SCREEN rendering. The pipeline is exactly the web one —
``markdown`` with the same extensions (incl. ``nl2br``: a single newline is a
real line break here, unlike the plain fill path's newline→space rule) then
``bleach`` with the same allowlist — so the converter sees precisely the tag
vocabulary the browser sees. The shared constants below are imported by
``main.py``'s ``render_markdown`` (single source of truth, pinned by test).

Formatting is DIRECT, never named styles (style IDs vary per template and per
Word language). Body text inherits the host paragraph's ``pPr``/run ``rPr``
seeds; headings/lists/quotes/tables build on top of them. Two deliberate
renunciations, both because they would require NEW zip parts or ``.rels``
edits — the multi-part surgery whose absence is this engine's no-repair
guarantee: lists use text bullets / computed numbers (never ``numbering.xml``),
and links render as underlined text with the URL appended (never
``<w:hyperlink>`` relationships).

Output is well-formed by construction: only the ``_r/_p/_tc/_tr/_tbl`` writer
functions emit tags, each closing everything it opens, and every text fragment
passes through ``docx_fill._escape_xml`` (one escaping authority).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import bleach
import markdown as _markdown_lib

from utils.docx_fill import _escape_xml

# ── Shared markdown pipeline constants (main.py imports these) ───────────
MD_EXTENSIONS = ["tables", "fenced_code", "nl2br"]
# use_align_attribute: markdown 3.x emits `style="text-align: …"` by default,
# which bleach strips (style is not in the allowlist) — the allowlist's
# `align` on th/td proves the original intent. With this config the extension
# emits `align=`, restoring table alignment on screen AND defining it here.
MD_EXTENSION_CONFIGS = {"tables": {"use_align_attribute": True}}
ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "strong", "em", "del",
    "code", "pre", "ul", "ol", "li", "blockquote",
    "a", "table", "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "th": ["align"],
    "td": ["align"],
}

# ── Bounds ───────────────────────────────────────────────────────────────
MAX_MARKDOWN_CHARS = 120_000   # notes cap at 100k; headroom, hard bound
MAX_NESTING_DEPTH = 32         # element-stack ceiling (real content ≤ ~8)

# Default usable width (twips): US Letter (12240) minus 1" margins (2×1440).
DEFAULT_USABLE_WIDTH = 9360

_MONO_FONT = "Consolas"
_SHADE_GREY = "F2F2F2"
_LINK_COLOR = "0563C1"
_BULLET_GLYPHS = ("•", "–", "▪")
_VALID_ALIGNS = ("left", "center", "right")

# Heading sizes in half-points, as offsets from the seed size S (default 22).
_HEADING_DELTAS = {1: 14, 2: 10, 3: 6, 4: 2, 5: 0, 6: -2}
_HEADING_SPACING = {
    1: (360, 160), 2: (320, 140), 3: (280, 120),
    4: (240, 120), 5: (240, 120), 6: (240, 120),
}

# Minimal ~1pt separator paragraph between adjacent tables (mirrors
# docx_fill._TABLE_SEPARATOR — duplicated here as a literal to keep the
# import surface one-way; a sync test pins the two equal).
TABLE_SEPARATOR = (
    '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" '
    'w:lineRule="exact"/><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr>'
    "</w:pPr></w:p>"
)


class MarkdownDocxError(ValueError):
    """Input exceeds a converter bound (size, nesting)."""


# ── Seed pPr/rPr parsing and merging ─────────────────────────────────────
# A seed is the INNER XML of the host paragraph's <w:pPr> / placeholder run's
# <w:rPr>. Both are parsed into top-level elements so additions merge in
# schema order. LINEARITY: the body is tempered against the element's own
# closing tag (no `.`, no DOTALL). A self-NESTING container (only the
# w:rPrChange/w:pPrChange tracked-change family) would mis-split — the
# round-trip check below catches that and drops the seed entirely
# (conservative: plain formatting, still valid).
_ELEMENT_RE = re.compile(
    r"<w:([A-Za-z0-9]+)(?:\s[^>]*)?(?:/>|>(?:(?!</w:\1>)[\s\S])*?</w:\1>)"
)

# Approximate ECMA-376 child order. Word tolerates deviations, but emitting
# in order costs nothing and removes a class of doubt.
_RPR_ORDER = (
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
    "strike", "dstrike", "outline", "shadow", "emboss", "imprint", "noProof",
    "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
    "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd",
    "fitText", "vertAlign", "rtl", "cs", "em", "lang",
)
_PPR_ORDER = (
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
    "widowControl", "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs",
    "suppressAutoHyphens", "autoSpaceDE", "autoSpaceDN", "bidi",
    "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing",
    "mirrorIndents", "jc", "textAlignment", "outlineLvl", "rPr",
)


def _parse_elements(inner: str) -> dict[str, str] | None:
    """Seed inner XML → {element name: full element}, or None when the seed
    does not round-trip through the element scan (nested same-name container,
    stray text) — the caller then drops the seed."""
    if not inner.strip():
        return {}
    elements: dict[str, str] = {}
    consumed: list[str] = []
    for m in _ELEMENT_RE.finditer(inner):
        consumed.append(m.group(0))
        elements.setdefault(m.group(1), m.group(0))
    # Round-trip check (inter-element whitespace tolerated): if the element
    # scan did not account for every byte, the seed holds something the flat
    # model cannot represent (nested same-name container, stray text) — drop
    # it rather than risk re-emitting it wrong.
    if "".join(consumed) != re.sub(r">\s+<", "><", inner.strip()):
        return None
    return elements


def _emit_ordered(elements: dict[str, str], order: tuple[str, ...]) -> str:
    known = [elements[n] for n in order if n in elements]
    unknown = [v for n, v in elements.items() if n not in order]
    return "".join(known + unknown)


def _merge_rpr(
    base_inner: str,
    *,
    bold: bool = False,
    italic: bool = False,
    strike: bool = False,
    mono: bool = False,
    link: bool = False,
    sz: int | None = None,
) -> str:
    base = _parse_elements(base_inner)
    if base is None:
        base = {}
    merged = dict(base)
    if mono:
        merged["rFonts"] = (
            f'<w:rFonts w:ascii="{_MONO_FONT}" w:hAnsi="{_MONO_FONT}"'
            f' w:cs="{_MONO_FONT}"/>'
        )
        merged["shd"] = (
            f'<w:shd w:val="clear" w:color="auto" w:fill="{_SHADE_GREY}"/>'
        )
    if bold:
        merged["b"] = "<w:b/>"
        merged["bCs"] = "<w:bCs/>"
    if italic:
        merged["i"] = "<w:i/>"
        merged["iCs"] = "<w:iCs/>"
    if strike:
        merged["strike"] = "<w:strike/>"
    if link:
        merged["u"] = '<w:u w:val="single"/>'
        merged["color"] = f'<w:color w:val="{_LINK_COLOR}"/>'
    if sz is not None:
        merged["sz"] = f'<w:sz w:val="{sz}"/>'
        merged["szCs"] = f'<w:szCs w:val="{sz}"/>'
    return _emit_ordered(merged, _RPR_ORDER)


def _merge_ppr(
    base_inner: str,
    *,
    extra: dict[str, str] | None = None,
    strip: tuple[str, ...] = (),
) -> str:
    """Merge addition elements into the seed pPr. ``extra`` maps element name
    → full element; ``strip`` removes seed elements first. A seed carrying
    ``pBdr`` when the block needs its own borders is dropped wholesale by the
    caller (rare host, conservative)."""
    base = _parse_elements(base_inner)
    if base is None:
        base = {}
    for name in strip:
        base.pop(name, None)
    merged = dict(base)
    merged.update(extra or {})
    return _emit_ordered(merged, _PPR_ORDER)


def _seed_size(base_rpr: str) -> int:
    m = re.search(r'<w:sz\s[^>]*w:val="(\d+)"', base_rpr)
    if m:
        try:
            return max(int(m.group(1)), 2)
        except ValueError:
            return 22
    return 22


# ── Element writers (the ONLY tag emitters) ──────────────────────────────

def _r(rpr_inner: str, text: str) -> str:
    rpr = f"<w:rPr>{rpr_inner}</w:rPr>" if rpr_inner else ""
    return (
        f'<w:r>{rpr}<w:t xml:space="preserve">{_escape_xml(text)}</w:t></w:r>'
    )


def _br() -> str:
    return "<w:r><w:br/></w:r>"


def _tab() -> str:
    return "<w:r><w:tab/></w:r>"


def _p(ppr_inner: str, runs: list[str]) -> str:
    ppr = f"<w:pPr>{ppr_inner}</w:pPr>" if ppr_inner else ""
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _tc(tcpr_inner: str, paragraphs: list[str]) -> str:
    if not paragraphs or not paragraphs[-1].endswith("</w:p>"):
        paragraphs = list(paragraphs) + ["<w:p/>"]
    tcpr = f"<w:tcPr>{tcpr_inner}</w:tcPr>" if tcpr_inner else ""
    return f"<w:tc>{tcpr}{''.join(paragraphs)}</w:tc>"


def _tr(trpr_inner: str, cells: list[str]) -> str:
    trpr = f"<w:trPr>{trpr_inner}</w:trPr>" if trpr_inner else ""
    return f"<w:tr>{trpr}{''.join(cells)}</w:tr>"


def _tbl(width: int, grid_cols: list[int], rows: list[str]) -> str:
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in grid_cols)
    return (
        "<w:tbl><w:tblPr>"
        f'<w:tblW w:w="{width}" w:type="dxa"/>'
        f"<w:tblBorders>{borders}</w:tblBorders>"
        '<w:tblLayout w:type="fixed"/>'
        "</w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{''.join(rows)}</w:tbl>"
    )


# ── The HTML → OOXML parser ──────────────────────────────────────────────

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_LIST_INDENT_STEP = 720   # twips per level
_LIST_HANGING = 360


class _Cell:
    __slots__ = ("header", "align", "runs")

    def __init__(self, header: bool, align: str | None) -> None:
        self.header = header
        self.align = align if align in _VALID_ALIGNS else None
        self.runs: list[str] = []


class _HtmlToOoxml(HTMLParser):
    """Event-driven HTML → OOXML blocks. Input is bleach-sanitized, so the
    tag vocabulary is closed; unknown tags (defensive) contribute only their
    text."""

    def __init__(self, base_ppr: str, base_rpr: str, usable_width: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_ppr = base_ppr
        self.base_rpr = base_rpr
        self.usable_width = usable_width
        self.seed_sz = _seed_size(base_rpr)

        self.blocks: list[str] = []
        self.runs: list[str] = []
        self.para_open = False
        self.heading: int | None = None

        self.bold = 0
        self.italic = 0
        self.strike = 0
        self.code = 0
        self.link_href: list[str] = []
        self.link_text: list[str] = []

        self.list_stack: list[dict] = []   # {"type": "ul"|"ol", "counter": int}
        self.li_pending_glyph = False
        self.quote_depth = 0
        self.in_pre = False
        self.pre_text: list[str] = []

        self.table_rows: list[list[_Cell]] | None = None
        self.current_row: list[_Cell] | None = None
        self.current_cell: _Cell | None = None

        self.depth = 0

    # ── run/paragraph helpers ────────────────────────────────────────

    def _rpr(self, *, sz: int | None = None, force_bold: bool = False) -> str:
        return _merge_rpr(
            self.base_rpr,
            bold=force_bold or self.bold > 0,
            italic=self.italic > 0,
            strike=self.strike > 0,
            mono=self.code > 0 or self.in_pre,
            link=bool(self.link_href),
            sz=sz,
        )

    def _emit_run(self, text: str) -> None:
        if self.link_text:
            self.link_text[-1] += text
        sz = None
        if self.heading is not None:
            sz = max(self.seed_sz + _HEADING_DELTAS[self.heading], 12)
        run = _r(self._rpr(sz=sz, force_bold=self.heading is not None), text)
        if self.current_cell is not None:
            self.current_cell.runs.append(run)
        else:
            self.para_open = True
            self.runs.append(run)

    def _emit_break(self) -> None:
        if self.current_cell is not None:
            self.current_cell.runs.append(_br())
        elif self.para_open:
            self.runs.append(_br())

    def _para_ppr(self) -> str:
        strip: list[str] = []
        extra: dict[str, str] = {}
        base = self.base_ppr

        if self.heading is not None:
            before, after = _HEADING_SPACING[self.heading]
            strip.append("spacing")
            extra["keepNext"] = "<w:keepNext/>"
            extra["spacing"] = (
                f'<w:spacing w:before="{before}" w:after="{after}"/>'
            )
        if self.list_stack:
            level = len(self.list_stack) - 1
            strip.append("ind")
            extra["ind"] = (
                f'<w:ind w:left="{_LIST_INDENT_STEP * (level + 1)}"'
                f' w:hanging="{_LIST_HANGING}"/>'
            )
        if self.quote_depth > 0:
            if "<w:pBdr" in base:
                base = ""   # conservative: never merge two border sets
            extra["pBdr"] = (
                '<w:pBdr><w:left w:val="single" w:sz="12" w:space="4"'
                ' w:color="A6A6A6"/></w:pBdr>'
            )
            if not self.list_stack:
                strip.append("ind")
                extra["ind"] = '<w:ind w:left="360"/>'
        if self.in_pre:
            strip.append("spacing")
            extra["shd"] = (
                f'<w:shd w:val="clear" w:color="auto" w:fill="{_SHADE_GREY}"/>'
            )
            extra["spacing"] = '<w:spacing w:before="120" w:after="120"/>'
        return _merge_ppr(base, extra=extra, strip=tuple(strip))

    def _flush_para(self) -> None:
        if self.current_cell is not None:
            return  # cell content is flushed by the cell, not here
        if not self.runs:
            # Never emit an empty paragraph from a flush — a loose list item
            # (<li><p>…) would otherwise strand its bullet on its own line
            # when the <p> start flushes the still-empty li paragraph. The
            # empty-INPUT case is handled in result().
            self.para_open = False
            return
        runs = self.runs
        if self.list_stack and self.li_pending_glyph:
            level = len(self.list_stack) - 1
            frame = self.list_stack[-1]
            if frame["type"] == "ol":
                frame["counter"] += 1
                glyph = f"{frame['counter']}."
            else:
                glyph = _BULLET_GLYPHS[level % len(_BULLET_GLYPHS)]
            runs = [_r(self._rpr(), glyph), _tab()] + runs
            self.li_pending_glyph = False
        self._append_block(_p(self._para_ppr(), runs))
        self.runs = []
        self.para_open = False

    def _append_block(self, block: str) -> None:
        if (
            block.startswith("<w:tbl>")
            and self.blocks
            and self.blocks[-1].endswith("</w:tbl>")
        ):
            self.blocks.append(TABLE_SEPARATOR)
        self.blocks.append(block)

    # ── table emission ───────────────────────────────────────────────

    def _flush_table(self) -> None:
        rows = self.table_rows or []
        self.table_rows = None
        rows = [r for r in rows if r]
        if not rows:
            return
        ncols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < ncols:
                r.append(_Cell(header=False, align=None))
        col_w = max(self.usable_width // ncols, 240)
        out_rows: list[str] = []
        for r in rows:
            is_header = any(c.header for c in r)
            cells: list[str] = []
            for c in r:
                tcpr = f'<w:tcW w:w="{col_w}" w:type="dxa"/>'
                if c.header:
                    tcpr += (
                        f'<w:shd w:val="clear" w:color="auto"'
                        f' w:fill="{_SHADE_GREY}"/>'
                    )
                ppr_extra: dict[str, str] = {}
                if c.align:
                    ppr_extra["jc"] = f'<w:jc w:val="{c.align}"/>'
                ppr = _merge_ppr(self.base_ppr, extra=ppr_extra,
                                 strip=("ind", "spacing"))
                cells.append(_tc(tcpr, [_p(ppr, c.runs)]))
            out_rows.append(_tr("<w:tblHeader/>" if is_header else "", cells))
        self._append_block(_tbl(col_w * ncols, [col_w] * ncols, out_rows))

    # ── HTMLParser events ────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING_DEPTH:
            raise MarkdownDocxError("Imbrication du contenu trop profonde.")
        attrs_d = dict(attrs)

        if tag in _HEADING_TAGS:
            self._flush_para()
            self.heading = _HEADING_TAGS[tag]
        elif tag == "p":
            self._flush_para()
            if self.current_cell is None:
                self.para_open = True
        elif tag == "br":
            self._emit_break()
        elif tag == "hr":
            self._flush_para()
            self._append_block(_p(
                _merge_ppr(
                    self.base_ppr,
                    extra={
                        "pBdr": '<w:pBdr><w:bottom w:val="single" w:sz="6"'
                                ' w:space="1" w:color="auto"/></w:pBdr>',
                        "spacing": '<w:spacing w:after="120"/>',
                    },
                    strip=("spacing",),
                ),
                [],
            ))
        elif tag == "strong":
            self.bold += 1
        elif tag == "em":
            self.italic += 1
        elif tag == "del":
            self.strike += 1
        elif tag == "code":
            if not self.in_pre:
                self.code += 1
        elif tag == "pre":
            self._flush_para()
            self.in_pre = True
            self.pre_text = []
        elif tag in ("ul", "ol"):
            self._flush_para()
            self.list_stack.append({"type": tag, "counter": 0})
        elif tag == "li":
            self._flush_para()
            self.li_pending_glyph = True
            if self.current_cell is None:
                self.para_open = True
        elif tag == "blockquote":
            self._flush_para()
            self.quote_depth += 1
        elif tag == "a":
            self.link_href.append(attrs_d.get("href") or "")
            self.link_text.append("")
        elif tag == "table":
            self._flush_para()
            if self.table_rows is None:
                self.table_rows = []
            # nested tables (impossible in md) — outer state kept, inner rows
            # simply merge into the same table
        elif tag == "tr":
            self.current_row = []
        elif tag in ("th", "td"):
            self.current_cell = _Cell(
                header=(tag == "th"), align=attrs_d.get("align")
            )
        # thead/tbody: structural no-ops (th vs td carries the header bit)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/> and <hr/> arrive here with some serializers.
        if tag in ("br", "hr"):
            self.depth += 1
            self.handle_starttag(tag, attrs)
            self.depth -= 1

    def handle_endtag(self, tag: str) -> None:
        self.depth = max(self.depth - 1, 0)

        if tag in _HEADING_TAGS:
            self._flush_para()
            self.heading = None
        elif tag == "p":
            self._flush_para()
        elif tag == "strong":
            self.bold = max(self.bold - 1, 0)
        elif tag == "em":
            self.italic = max(self.italic - 1, 0)
        elif tag == "del":
            self.strike = max(self.strike - 1, 0)
        elif tag == "code":
            if not self.in_pre:
                self.code = max(self.code - 1, 0)
        elif tag == "pre":
            self._emit_pre()
        elif tag in ("ul", "ol"):
            self._flush_para()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "li":
            self._flush_para()
            self.li_pending_glyph = False
        elif tag == "blockquote":
            self._flush_para()
            self.quote_depth = max(self.quote_depth - 1, 0)
        elif tag == "a":
            href = self.link_href.pop() if self.link_href else ""
            text = self.link_text.pop() if self.link_text else ""
            target = href[7:] if href.startswith("mailto:") else href
            if href and target.strip() != text.strip():
                self._emit_run_plain(f" ({href})")
        elif tag in ("th", "td"):
            if self.current_row is not None and self.current_cell is not None:
                self.current_row.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr":
            if self.table_rows is not None and self.current_row is not None:
                self.table_rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self._flush_table()

    def handle_data(self, data: str) -> None:
        if self.in_pre:
            self.pre_text.append(data)
            return
        # HTML whitespace semantics: runs of whitespace render as ONE space
        # (raw newlines in the HTML source are layout, not line breaks —
        # nl2br already turned meaningful ones into <br>).
        data = re.sub(r"[ \t\r\n]+", " ", data)
        if data == " ":
            # Leading whitespace (paragraph/cell start, or between blocks)
            # is layout noise; keep it only mid-content.
            if self.current_cell is not None:
                if not self.current_cell.runs:
                    return
            elif not self.runs:
                return
        self._emit_run(data)

    # ── helpers ──────────────────────────────────────────────────────

    def _emit_run_plain(self, text: str) -> None:
        run = _r(_merge_rpr(self.base_rpr), text)
        if self.current_cell is not None:
            self.current_cell.runs.append(run)
        else:
            self.para_open = True
            self.runs.append(run)

    def _emit_pre(self) -> None:
        text = "".join(self.pre_text)
        self.pre_text = []
        lines = text.split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        runs: list[str] = []
        rpr = _merge_rpr(self.base_rpr, mono=True)
        for i, line in enumerate(lines):
            if i:
                runs.append(_br())
            runs.append(_r(rpr, line) if line else "")
        ppr = self._para_ppr()          # in_pre still True → shading/spacing
        self.in_pre = False
        self._append_block(_p(ppr, [r for r in runs if r]))
        self.para_open = False
        self.runs = []

    def result(self) -> str:
        self._flush_para()
        if self.table_rows is not None:
            self._flush_table()
        if not self.blocks:
            self.blocks.append(_p(_merge_ppr(self.base_ppr), []))
        if self.blocks[-1].endswith("</w:tbl>"):
            # Word always writes a paragraph after a final table; a table as
            # the last child of a cell is outright invalid. One trailing
            # separator covers body-end, cell-end and note-ends-with-table.
            self.blocks.append(TABLE_SEPARATOR)
        return "".join(self.blocks)


# ── Public API ───────────────────────────────────────────────────────────

def markdown_to_ooxml(
    md_text: str,
    *,
    base_ppr: str = "",
    base_rpr: str = "",
    usable_width: int = DEFAULT_USABLE_WIDTH,
) -> str:
    """Convert Markdown to a sequence of ``<w:p>``/``<w:tbl>`` blocks.

    ``base_ppr``/``base_rpr`` are the INNER XML of the host paragraph's
    ``<w:pPr>`` and of the placeholder run's ``<w:rPr>`` (either may be "").
    ``usable_width`` is the printable width in twips (page minus margins).

    Raises :class:`MarkdownDocxError` on bound violations; never returns
    unbalanced XML; never returns "" (empty input yields one empty paragraph
    carrying the seed pPr, preserving the line the placeholder occupied).
    """
    if len(md_text) > MAX_MARKDOWN_CHARS:
        raise MarkdownDocxError("Le contenu de la note est trop volumineux.")

    html = _markdown_lib.markdown(
        md_text or "",
        extensions=MD_EXTENSIONS,
        extension_configs=MD_EXTENSION_CONFIGS,
    )
    html = bleach.clean(
        html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True
    )

    parser = _HtmlToOoxml(base_ppr, base_rpr, max(usable_width, 1440))
    parser.feed(html)
    parser.close()
    return parser.result()
