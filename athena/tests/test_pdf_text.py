"""utils/pdf_text.py — bounded document text extraction (Phase N).

Pure module: no Firestore, no Flask. PDF fixtures are built with reportlab
(already a pinned dependency) and pypdf's own writer (encryption); the .docx
fixtures are hand-built zips, the docx_fill test style.
"""

import io
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
