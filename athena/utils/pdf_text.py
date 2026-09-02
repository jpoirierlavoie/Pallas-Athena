"""Bounded document text extraction (Phase N) — pure: pypdf + stdlib only.

No Firestore, no Flask — mirrors ``docx_fill.py`` in style so the extraction
logic carries its own test suite (``tests/test_pdf_text.py``) and stays
importable from anywhere. The BYTE bound lives in the caller
(``models/document.get_document_bytes`` refuses a file above
``DOCUMENT_TEXT_MAX_BYTES`` before a single byte is downloaded — pypdf's
parse memory runs ~2-3x file size); this module bounds the OUTPUT (character
caps, page windows) and isolates per-page failures so one malformed page
never kills the call.

Honesty contract: a scanned page has no text layer, and this module never
pretends otherwise — it reports ``has_text: False`` and lists the page in
``pages_without_text`` so the caller (the MCP get_document_text tool
fallback, or the connector's model) can decide what to do. There is no OCR
here and none is implied. Binary content is never returned.

Warnings are machine-stable English tokens (``page_extraction_failed:7``);
the handler maps outcomes to French. Reasons for an unreadable document:
``encrypted`` | ``invalid_pdf`` | ``invalid_docx``.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from pypdf import PdfReader

# A single decompressed word/document.xml above this is refused rather than
# parsed — the docx_fill MAX_SINGLE_XML_BYTES doctrine (repetitive XML
# deflates ~350:1, so the compressed size bounds nothing).
MAX_DOCX_XML_BYTES = 25 * 1024 * 1024


class DocumentTextError(Exception):
    """Container-level failure. ``reason`` is a machine-stable token."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class PageText:
    page: int                 # 1-based
    text: str
    has_text: bool            # False = empty text layer (scan, image page…)
    page_truncated: bool      # True = this page alone overflowed the cap


@dataclass
class PdfTextResult:
    readable: bool
    reason: str = ""                       # set when readable is False
    page_count: int = 0
    pages: list[PageText] = field(default_factory=list)
    pages_without_text: list[int] = field(default_factory=list)
    truncated: bool = False                # the requested window was cut short
    next_page: Optional[int] = None        # resume here; None = window done
    warnings: list[str] = field(default_factory=list)


def extract_pdf_pages(
    data: bytes,
    *,
    first_page: int = 1,
    last_page: Optional[int] = None,
    char_cap: int = 40_000,
) -> PdfTextResult:
    """Extract the text layer of pages ``first_page..last_page`` (1-based,
    inclusive), accumulating at most ``char_cap`` characters.

    * An encrypted document → ``readable=False, reason="encrypted"`` (no
      password path exists on purpose — the app never stores one).
    * A document pypdf cannot open → ``readable=False, reason="invalid_pdf"``.
    * A single page whose extraction raises is isolated: it yields
      ``has_text=False`` plus a ``page_extraction_failed:<n>`` warning and
      the sweep continues.
    * A single page larger than the remaining cap is delivered TRUNCATED
      (``page_truncated=True``) and the window resumes at the NEXT page —
      re-reading the same page would return the same prefix forever, which
      reads to a paging model as an infinite loop. A >``char_cap``-character
      single page is pathological; the flag keeps it honest.
    * ``pages_without_text`` covers the RETURNED window only (computing it
      for the whole document would mean extracting the whole document).
    """
    result = PdfTextResult(readable=True)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return PdfTextResult(readable=False, reason="encrypted")
        page_count = len(reader.pages)
    except Exception:
        return PdfTextResult(readable=False, reason="invalid_pdf")

    result.page_count = page_count
    first = max(1, int(first_page))
    last = page_count if last_page is None else min(int(last_page), page_count)
    if first > page_count:
        result.warnings.append(f"first_page_beyond_document:{page_count}")
        return result

    remaining = max(0, int(char_cap))
    stopped_at: Optional[int] = None
    for number in range(first, last + 1):
        try:
            text = reader.pages[number - 1].extract_text() or ""
        except Exception:
            text = ""
            result.warnings.append(f"page_extraction_failed:{number}")
        text = text.strip("\n")
        has_text = bool(text.strip())
        if not has_text:
            result.pages_without_text.append(number)
            result.pages.append(
                PageText(page=number, text="", has_text=False, page_truncated=False)
            )
            continue
        if len(text) > remaining:
            result.pages.append(
                PageText(
                    page=number,
                    text=text[:remaining],
                    has_text=True,
                    page_truncated=True,
                )
            )
            stopped_at = number
            break
        remaining -= len(text)
        result.pages.append(
            PageText(page=number, text=text, has_text=True, page_truncated=False)
        )
        if remaining == 0 and number < last:
            stopped_at = number
            break

    if stopped_at is not None and stopped_at < last:
        result.truncated = True
        result.next_page = stopped_at + 1
    elif stopped_at is not None and stopped_at == last:
        # The cap fell exactly on the window's last page: the window itself
        # is complete (possibly minus the tail of a truncated page).
        result.truncated = any(p.page_truncated for p in result.pages)
        result.next_page = last + 1 if last < page_count else None
    else:
        result.next_page = last + 1 if last < page_count else None
    return result


# ── .docx text (stdlib — the docx_fill zip+regex approach) ──────────────────
#
# LINEARITY INVARIANT (CWE-1333, the docx_fill doctrine): no pattern below
# contains a `.` and none carries re.DOTALL — every quantified class is
# negated and linear. tests/test_pdf_text.py carries the tripwire.

_PARA_END_RE = re.compile(r"</w:p>")
_TAB_RE = re.compile(r"<w:tab[^>]*/>")
_BREAK_RE = re.compile(r"<w:br[^>]*/>")
_TAG_RE = re.compile(r"<[^>]+>")

_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
}


def _unescape(text: str) -> str:
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return text


def extract_docx_text(data: bytes) -> list[str]:
    """Return a .docx body as a list of paragraph strings, in order.

    Stdlib only (zipfile + linear regex over ``word/document.xml`` — the
    ``docx_fill`` approach). Headers/footers are deliberately excluded: the
    body is what a reader means by « the text of the document ». Raises
    :class:`DocumentTextError` (``invalid_docx``) on a broken container or
    an oversized XML part.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise DocumentTextError(
                "invalid_docx", "word/document.xml exceeds the size ceiling"
            )
        xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except DocumentTextError:
        raise
    except Exception as exc:
        raise DocumentTextError("invalid_docx", str(type(exc).__name__)) from exc

    xml = _TAB_RE.sub("\t", xml)
    xml = _BREAK_RE.sub("\n", xml)
    xml = _PARA_END_RE.sub("\n", xml)
    text = _unescape(_TAG_RE.sub("", xml))
    paragraphs = [line.rstrip() for line in text.split("\n")]
    # Collapse the trailing run of empties the final </w:p> conversions leave.
    while paragraphs and not paragraphs[-1]:
        paragraphs.pop()
    return paragraphs
