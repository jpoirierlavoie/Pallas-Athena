"""Leak scan: does a dossier's identity survive inside a .docx? (plan lot 2A, T5)

A gabarit is FIRM-WIDE: ``doc_templates`` has no dossier scope, so a letter
registered as a template carries every name, file number, address and phone
number it was written with into EVERY future letter, for every future
client. The connector's template writers (lot 2, MCP release) must therefore
refuse a source that still names the dossier it came from, unless the lawyer
accepts each residue by name. This module is the check; it decides nothing
else. ``services/docx_identifiers.dossier_identifiers`` builds the strings to
look for — from Firestore, fail-closed — and hands them here.

What is read — every XML part of the package, not only the three the fill
engine rewrites, because a residue can hide anywhere the file keeps text:

* ``word/*.xml`` — body, headers, footers, footnotes, endnotes, comments,
  the glossary, settings. The TEXT between tags, with paragraph-level tags
  (``w:p``, ``w:br``, ``w:tab``, table cells…) turned into a separator and
  every other tag removed — so a word Word split across two runs (``Trem`` +
  ``blay``, the spell-check split) reads back whole, while the last word of
  one paragraph never glues to the first word of the next (the DrawingML
  paragraph and a chart's data points separate too). Plus the attributes
  that carry a person or a value rather than formatting: the author and
  initials of a comment or a tracked change, the account id of
  ``word/people.xml``, a hyperlink tooltip, a picture's alternative text
  and title, a WordArt watermark's text, a simple field's instruction, and
  the values of document variables, content-control labels and entries,
  legacy form-field entries and a table's alternative text (``w:docVar``,
  ``w:alias``, ``w:tag``, ``w:listEntry``, ``w:default``, ``w:tblCaption``,
  ``w:tblDescription``);
* ``*.rels`` — the relationship ``Target`` values, percent-decoded: a
  ``mailto:`` link lives only there;
* every other XML part — ``docProps/core.xml`` (title, author, last editor),
  ``app.xml`` (company, manager), ``custom.xml``, ``customXml/item*.xml``
  (the data a bound content control shows) — with EVERY tag a separator,
  since there each element is its own value. « XML part » is decided by the
  NAME (``.xml``) or by ``[Content_Types].xml``: a part declared with an XML
  content type is read whatever its extension (System.IO.Packaging keeps the
  core properties in a ``….psmdcp``).

An archive holding two entries under one name is refused: ``ZipFile`` reads
the last one by name, so the first would go unread.

How it matches — by WORDS, never by characters. The text and each identifier
are folded the same way (Unicode compatibility decomposition, the
invisible format characters — soft hyphen, zero-width space, direction
marks — DELETED since the page draws nothing for them, accents stripped,
case-folded — so « ÉMILE » is « émile » is « Emile »), then cut into
alphanumeric words; every other character — spaces, the non-breaking
and narrow ones, apostrophes typed or curly, hyphens, dots, ``@`` — is a
separator. An identifier matches where its words appear CONSECUTIVELY and
WHOLE: « Roy » is found in « M. Roy, » and never in « Royaume » or
« Leroy »; « Jean Tremblay » is found in « TREMBLAY » only if « Jean »
precedes it. Separators are not compared, which over-matches on purpose:
« (514) 555-1234 », « 514.555.1234 » and « 514 555 1234 » are the same
three words, and a leak scan that misses a variant is worse than one that
asks the lawyer about a false positive (``accept`` is the escape hatch).
Identifiers shorter than :data:`MIN_IDENTIFIER_CHARS` letters and digits are
not scanned — reported in ``skipped``, never silently dropped.

The residue — a heuristic, stated plainly: text inside an image, a scanned
letterhead or an embedded object is invisible here, and so are variants the
identifier builder does not generate (a nickname, an abbreviation, an
international phone number typed with spaces). The scan is a guard, not a
proof.

Bounds — the scan refuses rather than concluding on what it did not read.
The package is checked by the fill engine's own structural gate
(``docx_fill._structural_errors``: 10 MB compressed, 2 000 entries, no
traversal) and every part is read through ``_read_entry_bounded`` against
the engine's byte caps (25 MB per part, 100 MB in total — the REAL inflated
size, not the central directory's claim). On top of them, the scanned TEXT
is capped at :data:`MAX_SCANNED_CHARS` — measured on the tag-stripped text
BEFORE entity decoding, which can only shrink it. (The fill engine itself has
no text cap: it rewrites each part in linear passes whose cost the byte caps
already bound. The scan's cost is per identifier — one pass over the text
for each — so it needs a cap in characters; one million is the bound the
lot 2B templatize engine adopts against gunicorn's 60 s timeout.)
Beyond it, or on any part that cannot be read, :class:`LeakScanError` —
never a partial result.

Linearity (CWE-1333): every pattern here is a character class or a literal
with bounded lookahead — no ``.`` outside a character class, no ``DOTALL``,
no pattern built from data. Matching the identifiers uses ``str.count`` on a
word-joined text, which is linear by construction. Tripwire:
``tests/test_docx_leak_scan.py`` derives the check over every compiled
pattern of this module.

:func:`scrub_core_properties` is the one byte change the lot allows when a
document is registered as a template UNCHANGED: it empties the five
``docProps/core.xml`` properties that name people or the matter (title,
subject, creator, last editor, description) and copies every other entry
byte-identical. Opt-in — the caller decides. It raises rather than claiming a
scrub it could not do.

Pure: no Firestore, no Flask, no logging — standard library plus the fill
engine's caps.
"""

from __future__ import annotations

import html
import io
import re
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Iterable, NamedTuple

from utils.docx_fill import (
    _XML_TAG_RE,
    MAX_SINGLE_XML_BYTES,
    MAX_TOTAL_DECOMPRESSED_BYTES,
    DocxFillError,
    _read_entry_bounded,
    _structural_errors,
)

__all__ = [
    "LeakScan",
    "LeakScanError",
    "MAX_IDENTIFIERS",
    "MAX_SCANNED_CHARS",
    "MIN_IDENTIFIER_CHARS",
    "Residue",
    "SCRUBBED_CORE_PROPERTIES",
    "ScrubbedDocx",
    "fold_key",
    "fold_tokens",
    "scan_identifiers",
    "scrub_core_properties",
    "words_within",
]

# The scanned text's ceiling, in characters (see the module docstring).
MAX_SCANNED_CHARS = 1_000_000
# Fewer letters and digits than this and an identifier matches everywhere.
MIN_IDENTIFIER_CHARS = 3
# A dossier with its parties yields a few hundred identifiers; far beyond
# that is a caller bug, and the cost is per identifier.
MAX_IDENTIFIERS = 2_000

# ── Patterns (linear: see the tripwire test) ─────────────────────────────

# A paragraph-level tag, opening, closing or empty: it separates words. The
# lookahead keeps « w:p » from matching « w:pPr ». WordprocessingML, plus the
# DrawingML paragraph and line break (a text box drawn as a shape, SmartArt)
# and a chart's data point and value (``c:pt``/``c:v`` — without them two
# category labels glue into one word and neither is ever matched).
_BREAK_TAG_RE = re.compile(
    r"</?(?:w:(?:p|br|cr|tab|ptab|sym|noBreakHyphen|tc|tr|tbl|txbxContent"
    r"|footnote|endnote|comment|hdr|ftr|body)|a:(?:p|br)|c:(?:pt|v))"
    r"(?=[\s/>])[^<>]*>"
)
# Attributes that name a person or carry a value, in word/ parts: the author
# of a comment or a tracked change, an account id, a hyperlink tooltip — and
# the values Word keeps in attributes although they are content: a picture's
# alternative text and title (``descr``/``title`` on ``wp:docPr`` and
# ``pic:cNvPr``, ``o:title`` on a VML image), a WordArt watermark's text
# (``string`` on ``v:textpath``), a simple field's instruction (``w:instr`` —
# a ``HYPERLINK "mailto:…"``), a drop-down content control's entry
# (``w:displayText``).
_WORD_TEXT_ATTRIBUTE_RE = re.compile(
    r"\s(?:w:author|w15:author|w15:userId|w:initials|w:tooltip"
    r"|descr|title|o:title|string|w:instr|w:displayText)"
    r"=(?:\"([^\"<>]*)\"|'([^'<>]*)')"
)
# Elements whose attribute VALUES are the content: document variables,
# content-control labels, a legacy form field's drop-down entries and default
# text, a table's alternative text.
_WORD_VALUE_ELEMENT_RE = re.compile(
    r"<w:(?:docVar|alias|tag|listEntry|default|tblCaption|tblDescription)"
    r"(?=[\s/>])[^<>]*>"
)
_QUOTED_VALUE_RE = re.compile(r"=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
# A relationship target, in a .rels part.
_REL_TARGET_RE = re.compile(r"\sTarget=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
# [Content_Types].xml: a Default (by extension) or Override (by part name)
# entry, and the attributes it carries. An XML part need not END in « .xml »:
# System.IO.Packaging writes the core properties as ``….psmdcp``, and any
# part can carry any extension its content type is declared for.
_CT_ENTRY_RE = re.compile(
    r"<(?:[A-Za-z_][\w.-]*:)?(Default|Override)(?=[\s/>])([^<>]*)>")
_CT_ATTRIBUTE_RE = re.compile(
    r"\s(Extension|PartName|ContentType)=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
# Combining marks left by the compatibility decomposition (accents).
_COMBINING_RE = re.compile(
    "[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]+"
)
# A word: a maximal run of Unicode letters and digits.
_WORD_RE = re.compile(r"[^\W_]+")
# The INVISIBLE format characters (Unicode category Cf) — the soft hyphen,
# the zero-width space and joiners, the direction marks, the BOM, the tag
# characters. Word draws nothing for them mid-line, so « Trem\u00adblay »
# reads « Tremblay » on the page; treated as separators they would split the
# word and hide the name. They are DELETED before cutting into words. Built
# once over the BMP plus the tag block (the astral remainder is script
# formatting no name carries).
_INVISIBLE = dict.fromkeys(
    cp for cp in (*range(0x10000), *range(0xE0000, 0xE0080))
    if unicodedata.category(chr(cp)) == "Cf"
)

# core.xml: the namespace declarations that tell which prefix is which.
_XMLNS_PREFIXED_RE = re.compile(r"\sxmlns:([^\s=:\"'<>/]+)=\"([^\"<>]*)\"")
_XMLNS_DEFAULT_RE = re.compile(r"\sxmlns=\"([^\"<>]*)\"")

_DC_NS = "http://purl.org/dc/elements/1.1/"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

# (namespace, local name) of the properties the scrub empties — the ones
# that name a person or the matter. Keywords, category, dates and revision
# are left alone.
SCRUBBED_CORE_PROPERTIES: tuple[tuple[str, str], ...] = (
    (_DC_NS, "title"),
    (_DC_NS, "subject"),
    (_DC_NS, "creator"),
    (_CP_NS, "lastModifiedBy"),
    (_DC_NS, "description"),
)
_CORE_PART = "docProps/core.xml"

_TOO_LARGE_MESSAGE = (
    "Le document contient trop de texte pour que ses identifiants soient "
    "contrôlés (plus de "
    + format(MAX_SCANNED_CHARS, ",").replace(",", "\u00a0")
    + " caractères) : le contrôle refuse plutôt que de conclure sans avoir "
    "tout lu."
)
_UNREADABLE_MESSAGE = (
    "Une partie du document est illisible : le contrôle des identifiants ne "
    "peut pas conclure."
)


class LeakScanError(ValueError):
    """The scan could not run to completion. French message, never content."""


@dataclass(frozen=True)
class Residue:
    """One identifier found in the document.

    ``identifier`` is the caller's own spelling (the first one supplied, when
    several fold to the same words); ``parts`` the package entries where it
    occurs, in archive order; ``count`` its non-overlapping occurrences, all
    parts together. Never the surrounding text.
    """

    identifier: str
    parts: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class LeakScan:
    """The outcome of :func:`scan_identifiers`.

    * ``residues`` — found and NOT accepted: the caller refuses unless empty;
    * ``accepted`` — found, and named in ``accept``;
    * ``unused_accept`` — ``accept`` entries that match nothing found (a
      likely typo: the lawyer believes a residue is accepted that is not
      the one present);
    * ``skipped`` — identifiers too short to scan;
    * ``parts_scanned`` / ``chars_scanned`` — what was read.
    """

    residues: tuple[Residue, ...]
    accepted: tuple[Residue, ...]
    unused_accept: tuple[str, ...]
    skipped: tuple[str, ...]
    parts_scanned: tuple[str, ...]
    chars_scanned: int

    @property
    def clean(self) -> bool:
        return not self.residues


class ScrubbedDocx(NamedTuple):
    """:func:`scrub_core_properties`'s result: the bytes, and the qualified
    names of the properties it actually emptied (``()`` → the input bytes,
    unchanged)."""

    data: bytes
    emptied: tuple[str, ...]


# ── Folding ──────────────────────────────────────────────────────────────


def fold_tokens(text: str) -> list[str]:
    """*text* as the scan compares it: compatibility-decomposed, accents
    stripped, case-folded, cut into words (runs of letters and digits).

    The ONE folding authority: the identifier builder uses it too, to
    deduplicate and to exclude the firm's own details.
    """
    folded = unicodedata.normalize("NFKD", text or "").translate(
        _INVISIBLE).casefold()
    return _WORD_RE.findall(_COMBINING_RE.sub("", folded))


def fold_key(text: str) -> tuple[str, ...]:
    """The comparison key of *text*: its folded words, as a tuple."""
    return tuple(fold_tokens(text))


def _joined(tokens: Iterable[str]) -> str:
    """Words joined by a DOUBLE space inside single spaces at both ends.

    With every word bounded by spaces and every gap exactly two wide, a
    needle built the same way matches only whole, consecutive words, and two
    adjacent occurrences never share a boundary — ``str.count`` then counts
    them both.
    """
    return " " + "  ".join(tokens) + " "


def words_within(needle: tuple[str, ...], container: tuple[str, ...]) -> bool:
    """True when the folded words *needle* occur, whole and consecutive,
    inside *container* — the scan's own matching rule, for a caller that
    compares two keys (the identifier builder excluding the firm's details).
    An empty *needle* is never within anything."""
    return bool(needle) and _joined(needle) in _joined(container)


# ── Reading the package ──────────────────────────────────────────────────


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _encode_like(original: bytes, text: str) -> bytes:
    """*text* in the encoding *original* was in — the XML declaration of a
    rewritten part must keep telling the truth. UTF-16 keeps a BOM (Python
    writes one), UTF-8 keeps its BOM only if it had one."""
    if original.startswith((b"\xff\xfe", b"\xfe\xff")):
        return text.encode("utf-16")
    if original.startswith(b"\xef\xbb\xbf"):
        return text.encode("utf-8-sig")
    return text.encode("utf-8")


_CONTENT_TYPES = "[Content_Types].xml"
_DUPLICATE_MESSAGE = (
    "Le document contient deux fois la même partie : Word ne l'ouvrirait "
    "pas sans réparation, et le contrôle des identifiants ne peut pas "
    "conclure. Enregistrez-le de nouveau depuis Word, puis réessayez."
)


def _refuse_duplicates(zf: zipfile.ZipFile) -> None:
    """Refuse an archive holding two entries under one name.

    ``ZipFile.open(name)`` reads the LAST entry of that name, so a scan by
    name would read the second copy twice and never the first — a copy Word
    may be the one to show. Word never writes such a package; the scan
    refuses rather than concluding on a part it did not read.
    """
    names = zf.namelist()
    if len(set(names)) != len(names):
        raise LeakScanError(_DUPLICATE_MESSAGE)


def _declared_xml_parts(zf: zipfile.ZipFile) -> tuple[frozenset, frozenset]:
    """``(extensions, part names)`` that ``[Content_Types].xml`` declares
    with an XML content type (one ending in ``xml``), lower-cased — part
    names without their leading ``/`` and percent-decoded, as zip entries
    are named. A package without the file declares nothing."""
    if _CONTENT_TYPES not in zf.namelist():
        return frozenset(), frozenset()
    try:
        data = _read_entry_bounded(zf, _CONTENT_TYPES, MAX_SINGLE_XML_BYTES)
    except DocxFillError as exc:
        raise LeakScanError(str(exc)) from None
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError,
            EOFError, OSError, ValueError):
        raise LeakScanError(_UNREADABLE_MESSAGE) from None
    extensions: set[str] = set()
    names: set[str] = set()
    for kind, attributes in _CT_ENTRY_RE.findall(_decode(data)):
        values = {key: a or b for key, a, b in _CT_ATTRIBUTE_RE.findall(attributes)}
        if not values.get("ContentType", "").strip().lower().endswith("xml"):
            continue
        if kind == "Default" and values.get("Extension"):
            extensions.add(values["Extension"].strip().lower().lstrip("."))
        elif kind == "Override" and values.get("PartName"):
            names.add(urllib.parse.unquote(
                values["PartName"].strip()).lstrip("/").lower())
    return frozenset(extensions), frozenset(names)


def _scanned(name: str, declared: tuple[frozenset, frozenset] = (
        frozenset(), frozenset())) -> bool:
    """An entry the scan reads: named ``.xml``/``.rels``, or declared with
    an XML content type by extension or by part name."""
    if name.endswith("/"):
        return False
    lower = name.lower()
    if lower.endswith((".xml", ".rels")):
        return True
    extensions, names = declared
    if lower in names:
        return True
    base = lower.rsplit("/", 1)[-1]
    return "." in base and base.rsplit(".", 1)[1] in extensions


def _quoted(matches: list[tuple[str, str]]) -> list[str]:
    return [a or b for a, b in matches]


def _raw_text(name: str, xml: str) -> str:
    """The text of one part, tags removed, entities NOT yet decoded."""
    lower = name.lower()
    if lower.endswith(".rels"):
        return " ".join(
            urllib.parse.unquote(value)
            for value in _quoted(_REL_TARGET_RE.findall(xml))
        )
    # CDATA sections keep their content as text.
    xml = xml.replace("<![CDATA[", " ").replace("]]>", " ")
    if lower.startswith("word/"):
        extra = _quoted(_WORD_TEXT_ATTRIBUTE_RE.findall(xml))
        for tag in _WORD_VALUE_ELEMENT_RE.findall(xml):
            extra.extend(_quoted(_QUOTED_VALUE_RE.findall(tag)))
        text = _XML_TAG_RE.sub("", _BREAK_TAG_RE.sub(" ", xml))
        return " ".join([text, *extra]) if extra else text
    return _XML_TAG_RE.sub(" ", xml)


def _haystacks(docx_bytes: bytes) -> tuple[list[tuple[str, str]], int]:
    """``([(part name, word-joined text)], characters scanned)``.

    Raises :class:`LeakScanError` on anything it cannot read in full.
    """
    errors, zf = _structural_errors(docx_bytes)
    if errors or zf is None:
        raise LeakScanError(errors[0] if errors else _UNREADABLE_MESSAGE)
    out: list[tuple[str, str]] = []
    remaining = MAX_TOTAL_DECOMPRESSED_BYTES
    chars = 0
    with zf:
        _refuse_duplicates(zf)
        declared = _declared_xml_parts(zf)
        for info in zf.infolist():
            name = info.filename
            if not _scanned(name, declared):
                continue
            try:
                data = _read_entry_bounded(
                    zf, name, min(MAX_SINGLE_XML_BYTES, remaining))
            except DocxFillError as exc:
                raise LeakScanError(str(exc)) from None
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError,
                    EOFError, OSError, ValueError):
                raise LeakScanError(_UNREADABLE_MESSAGE) from None
            remaining -= len(data)
            raw = _raw_text(name, _decode(data))
            chars += len(raw)
            if chars > MAX_SCANNED_CHARS:
                raise LeakScanError(_TOO_LARGE_MESSAGE)
            out.append((name, _joined(fold_tokens(html.unescape(raw)))))
    return out, chars


# ── The scan ─────────────────────────────────────────────────────────────


def _prepared(identifiers: Iterable[str]) -> tuple[list[tuple[str, tuple]], list[str]]:
    """``([(first spelling, key)], skipped)``, one entry per distinct key."""
    kept: dict[tuple, str] = {}
    skipped: list[str] = []
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        key = fold_key(identifier)
        if sum(len(word) for word in key) < MIN_IDENTIFIER_CHARS:
            if identifier.strip() and identifier not in skipped:
                skipped.append(identifier)
            continue
        kept.setdefault(key, identifier)
    return [(spelling, key) for key, spelling in kept.items()], skipped


def scan_identifiers(
    docx_bytes: bytes,
    identifiers: Iterable[str],
    *,
    accept: Iterable[str] = (),
) -> LeakScan:
    """Look for each of *identifiers* in every XML part of *docx_bytes*.

    *accept* names identifiers the lawyer has accepted as residues (compared
    after folding, so « jean tremblay » accepts « Jean Tremblay »): found,
    they land in ``accepted`` instead of ``residues``. An *accept* entry that
    matches nothing found is reported in ``unused_accept``.

    Raises :class:`LeakScanError` (French message) when the package or a
    part cannot be read, when its text exceeds :data:`MAX_SCANNED_CHARS`, or
    when more than :data:`MAX_IDENTIFIERS` distinct identifiers are given —
    never a partial answer.
    """
    # A bare string is iterable — character by character. Every character
    # would then be « too short » and the scan would pass anything: refuse.
    if isinstance(identifiers, (str, bytes)) or isinstance(accept, (str, bytes)):
        raise TypeError("identifiers and accept are collections of strings")
    prepared, skipped = _prepared(identifiers)
    if len(prepared) > MAX_IDENTIFIERS:
        raise LeakScanError(
            "Trop d'identifiants à contrôler d'un coup : le contrôle refuse "
            "plutôt que de n'en vérifier qu'une partie."
        )
    accept_list = [a for a in accept if isinstance(a, str)]
    accept_keys = {fold_key(a) for a in accept_list}
    haystacks, chars = _haystacks(docx_bytes)

    residues: list[Residue] = []
    accepted: list[Residue] = []
    found_keys: set[tuple] = set()
    for spelling, key in prepared:
        needle = _joined(key)
        parts: list[str] = []
        count = 0
        for name, haystack in haystacks:
            hits = haystack.count(needle)
            if hits:
                parts.append(name)
                count += hits
        if not count:
            continue
        found_keys.add(key)
        residue = Residue(spelling, tuple(parts), count)
        (accepted if key in accept_keys else residues).append(residue)

    unused = tuple(a for a in accept_list if fold_key(a) not in found_keys)
    return LeakScan(
        residues=tuple(residues),
        accepted=tuple(accepted),
        unused_accept=unused,
        skipped=tuple(skipped),
        parts_scanned=tuple(name for name, _ in haystacks),
        chars_scanned=chars,
    )


# ── The core-properties scrub ────────────────────────────────────────────


def _prefixes(xml: str) -> dict[str, set[str]]:
    """``{namespace URI: {prefix, …}}`` declared in *xml* (``""`` = default)."""
    found: dict[str, set[str]] = {}
    for prefix, uri in _XMLNS_PREFIXED_RE.findall(xml):
        found.setdefault(uri, set()).add(prefix)
    for uri in _XMLNS_DEFAULT_RE.findall(xml):
        found.setdefault(uri, set()).add("")
    return found


def _scrub_element(xml: str, qname: str) -> tuple[str, bool]:
    """Empty every ``<qname …>text</qname>`` of *xml*.

    ``(new xml, changed)``. Raises :class:`LeakScanError` when an opening
    tag has content this cannot empty (nested markup): claiming a scrub
    that did not happen would be the worst answer.
    """
    name = re.escape(qname)
    element = re.compile(
        "<" + name + "((?:\\s[^<>]*)?)>([^<]*)</" + name + "\\s*>")
    opening = re.compile("<" + name + "(?=[\\s/>])[^<>]*>")
    empty = re.compile("<" + name + "(?=[\\s/>])[^<>]*/>")
    changed = False

    def _empty(match: re.Match) -> str:
        nonlocal changed
        if match.group(2).strip():
            changed = True
        return "<" + qname + match.group(1) + "></" + qname + ">"

    rewritten, replaced = element.subn(_empty, xml)
    open_tags = len(opening.findall(xml)) - len(empty.findall(xml))
    if open_tags != replaced:
        raise LeakScanError(
            "Une propriété du document ne peut pas être effacée "
            "automatiquement : ouvrez le document dans Word (Fichier › "
            "Informations › Propriétés) et effacez-la, puis réessayez."
        )
    return (rewritten, True) if changed else (xml, False)


def scrub_core_properties(docx_bytes: bytes) -> ScrubbedDocx:
    """Empty title, subject, creator, last editor and description of
    ``docProps/core.xml``; copy every other entry byte-identical.

    No ``core.xml``, or nothing to empty → the INPUT bytes, untouched (a
    clean document is never rewritten). The rewritten archive reuses each
    entry's own ``ZipInfo`` — order, compression, timestamps — exactly as
    the fill engine does, so Word opens it without repair. Raises
    :class:`LeakScanError` on an unreadable package, a duplicated entry, or
    a property that cannot be emptied.

    Only ``docProps/core.xml`` is scrubbed. A package that keeps its core
    properties elsewhere (System.IO.Packaging's ``….psmdcp``) comes back
    untouched with ``emptied == ()`` — never a claimed scrub — and the scan,
    which reads every part declared with an XML content type, still reports
    what that part names.
    """
    errors, zf = _structural_errors(docx_bytes)
    if errors or zf is None:
        raise LeakScanError(errors[0] if errors else _UNREADABLE_MESSAGE)
    with zf:
        # Copying by name would write the LAST copy of a duplicated entry
        # twice — the same refusal as the scan's.
        _refuse_duplicates(zf)
        if _CORE_PART not in zf.namelist():
            return ScrubbedDocx(docx_bytes, ())
        try:
            core = _read_entry_bounded(zf, _CORE_PART, MAX_SINGLE_XML_BYTES)
        except DocxFillError as exc:
            raise LeakScanError(str(exc)) from None
        xml = _decode(core)
        prefixes = _prefixes(xml)
        emptied: list[str] = []
        for uri, local in SCRUBBED_CORE_PROPERTIES:
            for prefix in sorted(prefixes.get(uri, ())):
                qname = f"{prefix}:{local}" if prefix else local
                xml, changed = _scrub_element(xml, qname)
                if changed:
                    emptied.append(qname)
        if not emptied:
            return ScrubbedDocx(docx_bytes, ())
        new_core = _encode_like(core, xml)
        output = io.BytesIO()
        remaining = MAX_TOTAL_DECOMPRESSED_BYTES
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zf.infolist():
                try:
                    data = _read_entry_bounded(zf, info.filename, remaining)
                except DocxFillError as exc:
                    raise LeakScanError(str(exc)) from None
                remaining -= len(data)
                if info.filename == _CORE_PART:
                    data = new_core
                zout.writestr(info, data)
    return ScrubbedDocx(output.getvalue(), tuple(emptied))
