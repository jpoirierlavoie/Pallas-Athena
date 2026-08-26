"""Unit tests for utils/markdown_docx.py — markdown → OOXML conversion.

Every test that produces OOXML also parses it with defusedxml (wrapped in a
namespaced root) — the automated proxy for « Word opens it without repair »
(the real proof stays a manual Word-open check, documented in CLAUDE.md).
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from defusedxml import ElementTree as ET

from utils import markdown_docx as mdx
from utils.docx_fill import _TABLE_SEPARATOR
from utils.markdown_docx import (
    MAX_MARKDOWN_CHARS,
    MarkdownDocxError,
    markdown_to_ooxml,
)

_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _parse(ooxml: str) -> None:
    """Well-formedness gate — raises on unbalanced/invalid XML."""
    ET.fromstring(f"<root {_NS}>{ooxml}</root>")


def _convert(md: str, **kwargs) -> str:
    out = markdown_to_ooxml(md, **kwargs)
    _parse(out)
    return out


# ── Headings ───────────────────────────────────────────────────────────────


def test_heading_scale_from_default_seed():
    # Default seed size 22 half-points → h1..h6 = 36/32/28/24/22/20.
    for level, expected in ((1, 36), (2, 32), (3, 28), (4, 24), (5, 22), (6, 20)):
        out = _convert(f"{'#' * level} Titre")
        assert f'<w:sz w:val="{expected}"/>' in out, level
        assert "<w:b/>" in out
        assert "<w:keepNext/>" in out


def test_heading_scale_shifts_with_seed_size():
    out = _convert("# Titre", base_rpr='<w:sz w:val="28"/>')
    assert '<w:sz w:val="42"/>' in out  # 28 + 14


def test_heading_seed_without_sz_defaults_to_22():
    out = _convert("## Titre", base_rpr='<w:rFonts w:ascii="Garamond"/>')
    assert '<w:sz w:val="32"/>' in out  # 22 + 10


# ── Inline marks ───────────────────────────────────────────────────────────


def test_inline_marks_alone_and_combined():
    out = _convert("**gras** *italique* ***les deux***")
    assert "<w:b/>" in out
    assert "<w:i/>" in out
    # ***x*** → a run carrying BOTH b and i, in schema order.
    assert re.search(r"<w:b/><w:bCs/><w:i/><w:iCs/>", out)


def test_del_via_raw_html_strikethrough():
    # python-markdown has no ~~…~~ syntax; <del> arrives as raw HTML, which
    # markdown passes through and the bleach allowlist keeps.
    out = _convert("avant <del>barré</del> après")
    assert "<w:strike/>" in out


def test_seed_bold_not_duplicated():
    out = _convert("**gras**", base_rpr="<w:b/>")
    # One <w:b/> per run at most — never <w:b/><w:b/>.
    assert "<w:b/><w:b/>" not in out


def test_inline_code_mono_and_shaded():
    out = _convert("avant `du code` après")
    assert 'w:ascii="Consolas"' in out
    assert 'w:fill="F2F2F2"' in out


def test_fenced_code_block_single_paragraph_with_breaks():
    out = _convert("```\nligne1\nligne2 <&>\nligne3\n```")
    assert out.count("<w:p>") == 1
    assert out.count("<w:br/>") == 2
    assert "ligne2 &lt;&amp;&gt;" in out
    assert 'w:ascii="Consolas"' in out


# ── Lists ──────────────────────────────────────────────────────────────────


def test_bullet_list_geometry_and_glyphs():
    out = _convert("- premier\n- second\n    - imbriqué")
    assert out.count(">•</w:t>") == 2
    assert ">–</w:t>" in out
    assert '<w:ind w:left="720" w:hanging="360"/>' in out
    assert '<w:ind w:left="1440" w:hanging="360"/>' in out
    assert "<w:tab/>" in out


def test_ordered_list_literal_numbers_nested_restart():
    out = _convert("1. un\n2. deux\n    1. sous\n3. trois")
    assert ">1.</w:t>" in out
    assert ">2.</w:t>" in out
    assert ">3.</w:t>" in out
    # nested list restarted at 1. — two occurrences of "1."
    assert out.count(">1.</w:t>") == 2


def test_loose_list_item_keeps_glyph_on_content_line():
    # Loose list (<li><p>…</p></li>): the bullet must sit on the content
    # paragraph, never stranded on its own empty line.
    out = _convert("- premier\n\n- second")
    assert out.count(">•</w:t>") == 2
    paras = re.findall(r"<w:p>(?:(?!</w:p>)[\s\S])*</w:p>", out)
    for p in paras:
        if ">•</w:t>" in p:
            assert "premier" in p or "second" in p


# ── Blockquote / hr / br ───────────────────────────────────────────────────


def test_blockquote_left_border_no_italic():
    out = _convert("> « Citation »")
    assert '<w:left w:val="single" w:sz="12" w:space="4" w:color="A6A6A6"/>' in out
    assert "<w:i/>" not in out  # screen doesn't italicize quotes


def test_hr_bottom_border_paragraph():
    out = _convert("avant\n\n---\n\naprès")
    assert '<w:bottom w:val="single" w:sz="6" w:space="1" w:color="auto"/>' in out


def test_nl2br_soft_break_becomes_w_br():
    out = _convert("ligne un\nligne deux")
    assert "<w:br/>" in out
    assert out.count("<w:p>") == 1  # one paragraph, two lines


# ── Links ──────────────────────────────────────────────────────────────────


def test_link_underline_color_and_href_appended():
    out = _convert("[CanLII](https://canlii.ca/t/abc)")
    assert '<w:u w:val="single"/>' in out
    assert '<w:color w:val="0563C1"/>' in out
    assert "(https://canlii.ca/t/abc)" in out


def test_autolink_href_equal_to_text_not_appended():
    out = _convert("<https://canlii.ca/t/abc>")
    assert out.count("https://canlii.ca/t/abc") == 1


# ── Tables ─────────────────────────────────────────────────────────────────

_TABLE_MD = (
    "| Fait | Générateur | Admis |\n"
    "|------|:----------:|:-----:|\n"
    "| Contrat | oui | ☐ |\n"
    "| Défaut | non | ☐ |\n"
)


def test_table_minimal_requirements():
    out = _convert(_TABLE_MD, usable_width=9000)
    # tblPr first, then tblGrid with one gridCol per column.
    assert re.search(r"<w:tbl><w:tblPr>", out)
    assert out.count("<w:gridCol") == 3
    assert '<w:tblW w:w="9000" w:type="dxa"/>' in out
    assert '<w:tblLayout w:type="fixed"/>' in out
    assert out.count('<w:tcW w:w="3000" w:type="dxa"/>') == 9  # 3 rows × 3 cells
    # Header row: tblHeader + shading; body rows unshaded.
    assert out.count("<w:tblHeader/>") == 1
    # Alignment from the :---: rows.
    assert out.count('<w:jc w:val="center"/>') >= 4
    # Every cell ends with a paragraph.
    assert "</w:tc>" in out and "</w:p></w:tc>" in out


def test_table_ragged_row_padded_to_grid():
    md = "| a | b | c |\n|---|---|---|\n| seul |\n"
    out = _convert(md)
    assert out.count("<w:tc>") == 6  # 2 rows × 3 columns after padding


def test_adjacent_tables_get_separator():
    md = _TABLE_MD + "\n" + _TABLE_MD
    out = _convert(md)
    assert "</w:tbl><w:tbl>" not in out
    assert _TABLE_SEPARATOR in out


def test_note_ending_with_table_gets_trailing_separator():
    out = _convert(_TABLE_MD)
    assert out.endswith(_TABLE_SEPARATOR)
    assert not out.endswith("</w:tbl>")


# ── Escaping / glyph survival ─────────────────────────────────────────────


def test_escaping_in_every_context():
    md = (
        "Corps & <chevrons> \"guillemets\"\n\n"
        "| A&B |\n|---|\n| <c> |\n\n"
        "`code & <tags>`"
    )
    out = _convert(md)
    assert "&amp;" in out
    # No raw markup can leak from content into the XML stream.
    assert "<chevrons>" not in out
    assert "<c>" not in out


def test_checkbox_glyph_survives():
    out = _convert("- ☐ **Prescription** — délai")
    assert "☐" in out


# ── Seeds ──────────────────────────────────────────────────────────────────


def test_seed_ppr_and_rpr_inherited_by_body_text():
    out = _convert(
        "texte simple",
        base_ppr='<w:jc w:val="both"/>',
        base_rpr='<w:rFonts w:ascii="Garamond"/><w:sz w:val="24"/>',
    )
    assert '<w:jc w:val="both"/>' in out
    assert 'w:ascii="Garamond"' in out
    assert '<w:sz w:val="24"/>' in out


def test_unparseable_seed_dropped_not_corrupting():
    # A seed the flat element model cannot represent (nested same-name
    # container) is dropped — output stays valid, formatting plain.
    bad = "<w:rPr><w:rPrChange><w:rPr><w:b/></w:rPr></w:rPrChange></w:rPr>"
    out = _convert("texte", base_rpr=bad)
    _parse(out)


def test_empty_input_yields_one_empty_paragraph():
    out = _convert("")
    assert out.count("<w:p") == 1


# ── Bounds ─────────────────────────────────────────────────────────────────


def test_size_bound_raises():
    with pytest.raises(MarkdownDocxError):
        markdown_to_ooxml("x" * (MAX_MARKDOWN_CHARS + 1))


def test_nesting_bound_raises():
    deep = ""
    for _ in range(mdx.MAX_NESTING_DEPTH + 2):
        deep = f"> {deep}\n"
    deep_md = "\n".join(">" * i + " x" for i in range(1, mdx.MAX_NESTING_DEPTH + 4))
    with pytest.raises(MarkdownDocxError):
        markdown_to_ooxml(deep_md)


def test_full_size_note_converts():
    md = ("## Section\n\nParagraphe **gras**.\n\n- item\n\n" * 800)[:100_000]
    out = markdown_to_ooxml(md)
    _parse(out)


# ── Linearity invariant (CWE-1333) on the new pattern constant ────────────


def test_element_re_linearity_invariant():
    assert "." not in mdx._ELEMENT_RE.pattern.replace("\\.", "")
    assert not mdx._ELEMENT_RE.flags & re.DOTALL


# ── Shared-constants sync with the web pipeline ───────────────────────────


def test_table_separator_matches_engine():
    assert mdx.TABLE_SEPARATOR == _TABLE_SEPARATOR


def test_screen_pipeline_uses_shared_constants():
    """main.py's markdown filter must BE the shared pipeline function.

    Strengthened 2026-08-26: the constants were already shared, but the
    two-call markdown()+bleach.clean() composition had been copied three
    times (screen filter, this module, the chat email report). The Jinja
    filter is now markdown_to_safe_html itself, so screen and paper cannot
    drift even in the composition."""
    main_src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"),
        encoding="utf-8",
    ).read()
    assert "from utils.markdown_docx import markdown_to_safe_html" in main_src
    assert 'app.jinja_env.filters["markdown"] = markdown_to_safe_html' in main_src
