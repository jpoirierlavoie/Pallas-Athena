"""utils/pdf_text.py — bounded document text extraction (Phase N).

Pure module: no Firestore, no Flask. PDF fixtures are built with reportlab
(already a pinned dependency) and pypdf's own writer (encryption); the .docx
fixtures are hand-built zips, the docx_fill test style.
"""

import io
import logging
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import pdf_text  # noqa: E402
from utils.pdf_text import (  # noqa: E402
    DocumentTextError,
    extract_docx_text,
    extract_pdf_pages,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

def _pdf(pages: list[str]) -> bytes:
    """One page per entry; an empty string draws nothing (no text layer)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    for content in pages:
        if content:
            text = c.beginText(72, 720)
            for line in content.split("\n"):
                text.textLine(line)
            c.drawText(text)
        c.showPage()
    c.save()
    return buffer.getvalue()


def _encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_pdf(["secret"]))))
    writer.encrypt("motdepasse")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _docx(document_xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


# ── PDF extraction ──────────────────────────────────────────────────────────

def test_text_page_extracts_with_has_text():
    result = extract_pdf_pages(_pdf(["Contrat de vente entre les parties"]))
    assert result.readable is True
    assert result.page_count == 1
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.page == 1
    assert page.has_text is True
    assert "Contrat de vente" in page.text
    assert result.pages_without_text == []
    assert result.next_page is None
    assert result.truncated is False


def test_blank_page_is_honest_not_invented():
    result = extract_pdf_pages(_pdf(["du texte", "", "encore du texte"]))
    assert [p.has_text for p in result.pages] == [True, False, True]
    assert result.pages_without_text == [2]
    assert result.pages[1].text == ""


def test_encrypted_pdf_refused_with_reason():
    result = extract_pdf_pages(_encrypted_pdf())
    assert result.readable is False
    assert result.reason == "encrypted"
    assert result.pages == []


def test_garbage_bytes_refused_as_invalid_pdf():
    result = extract_pdf_pages(b"pas un pdf du tout" * 100)
    assert result.readable is False
    assert result.reason == "invalid_pdf"


def test_page_window_is_one_based_inclusive():
    data = _pdf(["page un", "page deux", "page trois", "page quatre"])
    result = extract_pdf_pages(data, first_page=2, last_page=3)
    assert [p.page for p in result.pages] == [2, 3]
    assert "deux" in result.pages[0].text
    assert "trois" in result.pages[1].text
    assert result.next_page == 4          # more document remains after the window
    assert result.truncated is False      # the requested window itself is complete


def test_char_cap_stops_midway_with_next_page():
    data = _pdf(["A" * 50, "B" * 50, "C" * 50])
    result = extract_pdf_pages(data, char_cap=80)
    # Page 1 fits (50), page 2 overflows the remaining 30 → truncated there.
    assert result.pages[0].page_truncated is False
    assert result.pages[1].page_truncated is True
    assert len(result.pages[1].text) == 30
    assert result.truncated is True
    assert result.next_page == 3


def test_single_oversized_page_resumes_at_next_never_loops():
    data = _pdf(["X" * 200, "suite"])
    result = extract_pdf_pages(data, char_cap=50)
    assert result.pages[0].page_truncated is True
    # Resuming at the SAME page would return the same prefix forever.
    assert result.next_page == 2


def test_first_page_beyond_document_warns_machine_stable():
    result = extract_pdf_pages(_pdf(["seule page"]), first_page=9)
    assert result.readable is True
    assert result.pages == []
    assert result.warnings == ["first_page_beyond_document:1"]


def test_malformed_page_is_isolated_not_fatal(monkeypatch):
    class _BadPage:
        def extract_text(self):
            raise ValueError("boom")

    class _GoodPage:
        def extract_text(self):
            return "texte valide"

    class _FakeReader:
        is_encrypted = False
        pages = [_GoodPage(), _BadPage(), _GoodPage()]

        def __init__(self, _stream):
            pass

    monkeypatch.setattr(pdf_text, "PdfReader", _FakeReader)
    result = extract_pdf_pages(b"peu importe")
    assert result.readable is True
    assert [p.has_text for p in result.pages] == [True, False, True]
    assert result.pages_without_text == [2]
    assert result.warnings == ["page_extraction_failed:2"]


# ── pypdf's fontTools notice (pypdf ≥ 6.17.0) ──────────────────────────────

def _pdf_with_type1c_font(pages: int) -> bytes:
    """A Type1 font whose only embedded file is a CFF (/FontFile3 /Subtype
    /Type1C) and which has no /ToUnicode: what makes pypdf log its fontTools
    notice on every page that uses the font."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    font_file = DecodedStreamObject()
    font_file.set_data(b"\x01\x00\x04\x01")  # never parsed: no fontTools
    font_file[NameObject("/Subtype")] = NameObject("/Type1C")
    descriptor = DictionaryObject({
        NameObject("/Type"): NameObject("/FontDescriptor"),
        NameObject("/FontName"): NameObject("/ABCDEF+Essai"),
        NameObject("/FontFile3"): writer._add_object(font_file),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/ABCDEF+Essai"),
        NameObject("/FirstChar"): NumberObject(32),
        NameObject("/LastChar"): NumberObject(126),
        NameObject("/Widths"): ArrayObject([NumberObject(500)] * 95),
        NameObject("/FontDescriptor"): writer._add_object(descriptor),
    })
    resources = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        }),
    })
    for number in range(1, pages + 1):
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = resources
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 720 Td (Page {number}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _fonttools_notices(records) -> list:
    return [
        r for r in records
        if r.name == "pypdf._cmap"
        and str(r.msg).startswith(pdf_text.FONTTOOLS_NOTICE_PREFIX)
    ]


def test_pypdfs_fonttools_notice_is_dropped_before_it_reaches_a_handler(caplog):
    """The filter sits on « pypdf._cmap » itself; the control proves this
    fixture really makes pypdf emit the notice, so the assertion cannot pass
    because the notice was never logged."""
    data = _pdf_with_type1c_font(pages=2)
    logger = logging.getLogger("pypdf._cmap")
    assert pdf_text.FONTTOOLS_NOTICE_FILTER in logger.filters

    logger.removeFilter(pdf_text.FONTTOOLS_NOTICE_FILTER)
    try:
        with caplog.at_level(logging.WARNING):
            unfiltered = extract_pdf_pages(data)
        assert _fonttools_notices(caplog.records), (
            "pypdf no longer logs the fontTools notice on this fixture: "
            "re-check FONTTOOLS_NOTICE_PREFIX against pypdf/_cmap.py"
        )
    finally:
        logger.addFilter(pdf_text.FONTTOOLS_NOTICE_FILTER)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        filtered = extract_pdf_pages(data)
    assert _fonttools_notices(caplog.records) == []
    assert [p.text for p in filtered.pages] == [p.text for p in unfiltered.pages]
    assert filtered.warnings == unfiltered.warnings == []


def test_the_filter_lets_every_other_pypdf_diagnostic_through():
    def record(msg):
        return logging.LogRecord(
            "pypdf._cmap", logging.WARNING, __file__, 1, msg, None, None
        )

    keep = pdf_text.FONTTOOLS_NOTICE_FILTER.filter
    assert keep(record("Advanced encoding %(enc)s not implemented yet")) is True
    assert keep(record("Skipping broken line %(line)r")) is True
    assert keep(record(pdf_text.FONTTOOLS_NOTICE_PREFIX + " … %(ft)s")) is False


# ── .docx extraction ────────────────────────────────────────────────────────

def test_docx_paragraphs_tabs_breaks_entities():
    xml = (
        "<w:document><w:body>"
        "<w:p><w:r><w:t>Premier paragraphe</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Avant</w:t></w:r><w:tab/><w:r><w:t>apr&amp;s</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>ligne 1</w:t></w:r><w:br/><w:r><w:t>ligne 2</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    paragraphs = extract_docx_text(_docx(xml))
    assert paragraphs[0] == "Premier paragraphe"
    assert paragraphs[1] == "Avant\tapr&s"
    assert paragraphs[2] == "ligne 1"
    assert paragraphs[3] == "ligne 2"


def test_docx_invalid_container_raises_with_reason():
    with pytest.raises(DocumentTextError) as excinfo:
        extract_docx_text(b"pas un zip")
    assert excinfo.value.reason == "invalid_docx"


def test_docx_without_document_xml_raises():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("autre.xml", "<x/>")
    with pytest.raises(DocumentTextError):
        extract_docx_text(buffer.getvalue())


# ── The CWE-1333 tripwire (docx_fill doctrine) ─────────────────────────────

def test_regex_linearity_invariant():
    import re as _re

    for pattern in (
        pdf_text._PARA_END_RE,
        pdf_text._TAB_RE,
        pdf_text._BREAK_RE,
        pdf_text._TAG_RE,
    ):
        assert "." not in pattern.pattern, pattern.pattern
        assert not pattern.flags & _re.DOTALL, pattern.pattern
