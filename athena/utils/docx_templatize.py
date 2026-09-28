"""Templatize a finished .docx: literal case values → ``{{placeholders}}``
(plan lot 2B, step 1).

A lawyer's real letter carries the names, file numbers and addresses of the
matter it was written for. Turning it into a gabarit means replacing each of
those literals by the ``{{placeholder}}`` the fill engine resolves — without
disturbing a single byte of the letterhead Word must reopen without repair.
``utils.docx_fill`` cannot do it: its ``_normalize_runs`` merges runs only on
identical formatting or across a ``{{`` bridge, so a literal Word split
across a language or bold change (``Jean`` | ``Tremblay``, the spell-check
split) would silently never match. This module is the engine; the MCP tools
(``preview_templatize`` / ``templatize_document``) and the leak scan
(``utils.docx_leak_scan``) sit on top of it.

API::

    templatize(docx_bytes, substitutions) -> TemplatizeResult   # .data = bytes
    analyse(docx_bytes, substitutions)    -> TemplatizeResult   # .data = None

Both return the SAME report — per substitution: its classification
(auto / manual / passthrough, from ``utils.template_fields``), the count it
substituted per part, and every occurrence it deliberately did NOT
substitute (field results, data-bound content controls, markup inside the
literal, parts the fill engine never reads). ``templatize`` refuses — returns
``data=None`` with the reason — unless every substitution's substituted
count EQUALS its ``expected_occurrences`` (the ``import_invoice``
expected-total doctrine): there is never a partial output.

What is rewritten, and what is only reported
--------------------------------------------
* TARGET parts — exactly the ones ``fill_docx`` rewrites (``docx_fill``'s
  ``_TARGET_RE``: ``word/document.xml``, ``header*.xml``, ``footer*.xml``).
  A placeholder planted anywhere else would never be filled.
* REPORTED parts — ``word/footnotes.xml``, ``endnotes.xml``, ``comments.xml``
  and every ``docProps/*.xml``: their occurrences are COUNTED, never
  substituted, so the caller knows what stays behind.
* Every other entry is copied byte-identical (the ``fill_docx`` ``ZipInfo``
  loop), and so is every target part the call does not edit.

How a target part is read — a streaming lexer, not a parser
-----------------------------------------------------------
One pattern, ``_TOKEN_RE`` (a tag, or a run of text), walks each part; a
position it cannot tokenize (a stray ``<``) REFUSES the source, never skips
it. A stack keeps the tags balanced (a mismatch refuses too), which is what
lets the edit below guarantee well-formed output by construction. From the
tokens a single character STREAM per part is built from the ``<w:t>`` text
nodes, each character remembering its text node (a *segment*):

* Transparent — runs, bookmarks, proofing marks, hyperlinks, smart tags and
  every property subtree (``w:rPr``/``w:pPr``/…): a literal Word split
  across runs of different formatting, language or rsid reads back whole.
* Hard boundaries (U+FFFF in the stream — no match can cross one):
  paragraph edges (paragraphs nest in text boxes), ``w:tab``/``w:br``/
  ``w:cr``, drawings, objects, field characters, footnote references,
  content-control and ``mc:AlternateContent`` edges, and any element this
  module does not know — the conservative direction.
* ``w:softHyphen`` — an invisible in-word mark: a boundary too (the edit
  never removes an element), but an occurrence it interrupts is REPORTED as
  ``blocked_by_markup``; so is one containing a ``w:noBreakHyphen``, which
  draws a visible ``-``.
* Not editable — text inside a field (between ``fldChar`` begin and end,
  or inside ``w:fldSimple``: Word regenerates results on update) and inside
  a content control bound to ``customXml`` (``w:dataBinding``: Word
  re-populates it). Matched, counted, never substituted.
* Refused sources — tracked changes (``w:ins``/``w:del``/``w:moveFrom``/
  ``w:moveTo`` and every other revision element: deleted text is invisible
  but present), comment anchors or any ``w:comment``, Strict Open XML, a
  part not declaring ``w:`` as WordprocessingML, a non-UTF-8 target part,
  and more than :data:`MAX_VISIBLE_CHARS` characters of text.
* ``mc:AlternateContent`` — Word stores a text box twice, a ``mc:Choice``
  and a ``mc:Fallback`` copy. Both are edited identically; per substitution
  and per AlternateContent the branch counts must be EQUAL or the call is
  refused. Logical counts (compared with ``expected_occurrences``) count the
  first branch only — the one Word shows.

How a literal matches
---------------------
On a folded copy of the stream — NBSP, U+202F, U+2007, U+2009 → space;
’ ‘ ʼ → ``'``; U+2010, U+2011 → ``-`` — ONE character for ONE, so every
position still maps to its segment; the literal is folded the same way. The
fold is for MATCHING only: the text Word keeps is edited, never re-folded.
Case-SENSITIVE (an ALL-CAPS heading gets its own substitution with an
ALL-CAPS placeholder, the catalog's upper-casing convention); whole-word by
Unicode category (letters, digits, marks, ``_``), looking through soft
hyphens; the LONGEST literal first, each accepted occurrence masking its
range; existing ``{{…}}`` spans are masked before anything, so a literal can
never land inside a placeholder or break one.

How it edits
------------
The placeholder is written into the FIRST segment of an occurrence at its
start offset and the literal's remaining characters are deleted from the
following segments: the element structure is never added to or removed from
— only text-token contents change, plus ``xml:space="preserve"`` on a
modified ``<w:t>`` whose new text needs it. Untouched tokens are re-emitted
verbatim.

Checks on the output (templatize) — the archive re-opens with ``zipfile``
and passes ``testzip``; ``docx_fill.validate_template`` reports no error,
every inserted name among its placeholders and none among its split-run
suspects; and in every rewritten part, the fill engine's own normalized view
holds exactly the source's count of each inserted name plus what was
inserted. Any failure refuses (``data=None``).

Linearity (CWE-1333) — every pattern here is a character class, a literal
or a bounded repetition: no ``.`` outside a class, no ``DOTALL``, no nested
unbounded quantifier, and no pattern is ever built from data (literals are
found with ``str.find``). Tripwire: ``tests/test_docx_templatize.py`` derives
the check over every compiled pattern of this module and spies on
``re.compile`` during a run. The candidate budget
(:data:`MAX_CANDIDATES`) bounds the Python-level work a literal that
matches everywhere but never as a whole word could otherwise cost.

Pure: standard library plus the fill engine's caps and the field catalog —
no Firestore, no Flask, no logging, no ``python-docx``/``docxtpl``. Messages
are French and never quote a literal (they name the substitution by its
number, and the caller's own placeholder name).
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from array import array
from dataclasses import dataclass, field
from typing import Optional, Sequence

from utils.docx_fill import (
    _ANY_TOKEN_RE,
    MAX_SINGLE_XML_BYTES,
    MAX_TOTAL_DECOMPRESSED_BYTES,
    PLACEHOLDER_RE,
    DocxFillError,
    _normalize_runs,
    _read_entry_bounded,
    _structural_errors,
    _target_names,
    validate_template,
)
from utils.template_fields import classify_placeholders, is_uppercase_name

__all__ = [
    "Blocker",
    "CLASSIFICATIONS",
    "MAX_CANDIDATES",
    "MAX_EXPECTED_OCCURRENCES",
    "MAX_LITERAL_CHARS",
    "MAX_PLACEHOLDER_CHARS",
    "MAX_SUBSTITUTIONS",
    "MAX_TOKENS",
    "MAX_VISIBLE_CHARS",
    "MIN_LITERAL_CHARS",
    "Substitution",
    "SubstitutionReport",
    "TemplatizeResult",
    "analyse",
    "fold",
    "templatize",
]

# ── Bounds ───────────────────────────────────────────────────────────────

MAX_SUBSTITUTIONS = 50
MIN_LITERAL_CHARS = 3
MAX_LITERAL_CHARS = 300
MAX_PLACEHOLDER_CHARS = 120
MAX_EXPECTED_OCCURRENCES = 10_000
# The text read (targets and reported parts together), in characters — the
# bound docx_leak_scan adopts too, against gunicorn's 60 s timeout.
MAX_VISIBLE_CHARS = 1_000_000
# Occurrences examined, accepted or not, all substitutions together.
MAX_CANDIDATES = 200_000
# XML tokens (tags and text runs) lexed, every part read together. The
# character cap above never sees markup without text: 100 MB of empty runs
# (the fill engine's total cap) lexed in 14 s and templatized in 23 s on a
# workstation, which an F2 instance and the leak scan after it would push
# past gunicorn's 60 s. A long real letter holds ~100 000 tokens.
MAX_TOKENS = 3_000_000

CLASSIFICATIONS = ("auto", "manual", "passthrough")

# ── Patterns (linear — see the module docstring and the tripwire test) ───

# The lexer: a tag (no « < » or « > » inside), or a run of text.
_TOKEN_RE = re.compile(r"<[^<>]*>|[^<]+")
# A tag's qualified name.
_TAG_NAME_RE = re.compile(r"</?([^\s/<>]+)")
# The five XML entities and the numeric character references.
_ENTITY_RE = re.compile(
    r"&(?:#x([0-9A-Fa-f]{1,6})|#([0-9]{1,7})|(amp|lt|gt|quot|apos));")
# Attribute values the lexer reads.
_FLDCHAR_TYPE_RE = re.compile(r"\sw:fldCharType=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
_XML_SPACE_RE = re.compile(r"\sxml:space=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
_W_NAMESPACE_RE = re.compile(r"\sxmlns:w=(?:\"([^\"<>]*)\"|'([^'<>]*)')")
# Anything between double braces — valid placeholder or not — is masked,
# on top of docx_fill's own token pattern (bounded: a stray « {{ » masks at
# most this much).
_BRACE_SPAN_RE = re.compile(r"\{\{[^{}]{0,300}\}\}")
# Parts whose occurrences are reported, never substituted.
_REPORTED_RE = re.compile(
    r"^(?:word/(?:footnotes|endnotes|comments)\.xml|docProps/[^/]+\.xml)$")
# A placeholder NAME — docx_fill.PLACEHOLDER_RE's name class, whole.
_NAME_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_.]+")

_TRANSITIONAL_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STRICT_MARK = "purl.oclc.org/ooxml/"

# ── Stream sentinels (Unicode noncharacters: illegal in XML, so no text ──
# node can ever contain one — the decoder refuses them)
_HARD = "\uffff"
_SOFT = "\ufffe"
_SEG_HARD = -1
_SEG_MARKUP = -2      # a visible character an ELEMENT draws (noBreakHyphen)
_SEG_SOFT = -3

# The one-for-one matching fold.
_FOLD = str.maketrans({
    "\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2009": " ",
    "\u2018": "'", "\u2019": "'", "\u02bc": "'",
    "\u2010": "-", "\u2011": "-",
})

_NAMED_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

# Every revision element: tracked insertions, deletions, moves, formatting
# and table changes. Deleted text is invisible but present; a formatting
# change nests a second rPr. The lawyer accepts or rejects them in Word.
_TRACKED_CHANGES = frozenset({
    "w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:delText", "w:delInstrText",
    "w:moveFromRangeStart", "w:moveFromRangeEnd", "w:moveToRangeStart",
    "w:moveToRangeEnd", "w:rPrChange", "w:pPrChange", "w:sectPrChange",
    "w:tblPrChange", "w:tblPrExChange", "w:trPrChange", "w:tcPrChange",
    "w:tblGridChange", "w:numberingChange", "w:cellIns", "w:cellDel",
    "w:cellMerge", "w:customXmlInsRangeStart", "w:customXmlInsRangeEnd",
    "w:customXmlDelRangeStart", "w:customXmlDelRangeEnd",
    "w:customXmlMoveFromRangeStart", "w:customXmlMoveFromRangeEnd",
    "w:customXmlMoveToRangeStart", "w:customXmlMoveToRangeEnd",
})
_COMMENT_ANCHORS = frozenset({
    "w:commentRangeStart", "w:commentRangeEnd", "w:commentReference",
})
# Property subtrees: nothing inside one is text flow.
_PROPERTY_CONTAINERS = frozenset({
    "w:rPr", "w:pPr", "w:sdtPr", "w:sdtEndPr", "w:customXmlPr",
    "w:smartTagPr", "w:ffData", "w:tblPr", "w:tblPrEx", "w:trPr", "w:tcPr",
    "w:tblGrid", "w:sectPr",
})
# Elements a literal reads straight through.
_TRANSPARENT = frozenset({
    "w:r", "w:proofErr", "w:bookmarkStart", "w:bookmarkEnd", "w:hyperlink",
    "w:smartTag", "w:customXml", "w:lastRenderedPageBreak", "w:permStart",
    "w:permEnd", "w:dir", "w:bdo",
})
_AC = "mc:AlternateContent"
_AC_BRANCHES = frozenset({"mc:Choice", "mc:Fallback"})

_MODE_TARGET = "target"
_MODE_WORD = "word"      # a word/ part, reported only
_MODE_PROPS = "props"    # docProps: every text token is a value

# ── Messages ─────────────────────────────────────────────────────────────

_BLOCKER_MESSAGES = {
    "tracked_changes": (
        "Le document contient des modifications suivies : acceptez-les ou "
        "refusez-les dans Word (Révision › Accepter ou Refuser), enregistrez, "
        "puis réessayez."
    ),
    "comments": (
        "Le document contient des commentaires : supprimez-les dans Word "
        "(Révision › Supprimer › Supprimer tous les commentaires du "
        "document), enregistrez, puis réessayez."
    ),
    "strict_ooxml": (
        "Le document est enregistré au format « Strict Open XML » : "
        "enregistrez-le depuis Word au format « Document Word (.docx) », "
        "puis réessayez."
    ),
    "namespace": (
        "Une partie du document n'a pas la structure WordprocessingML "
        "attendue : enregistrez-le de nouveau depuis Word, puis réessayez."
    ),
    "malformed_xml": (
        "Une partie du document est mal formée : ouvrez-le dans Word, "
        "enregistrez-le de nouveau, puis réessayez."
    ),
    "encoding": (
        "Une partie du document n'est pas encodée en UTF-8 : "
        "enregistrez-le de nouveau depuis Word, puis réessayez."
    ),
    "too_complex": (
        "Le document est trop complexe pour être transformé en gabarit "
        "(plus de " + format(MAX_TOKENS, ",").replace(",", "\u00a0")
        + " éléments XML)."
    ),
    "too_much_text": (
        "Le document contient trop de texte pour être transformé en gabarit "
        "(plus de " + format(MAX_VISIBLE_CHARS, ",").replace(",", "\u00a0")
        + " caractères)."
    ),
    "duplicate_entry": (
        "Le document contient deux fois la même partie : Word ne l'ouvrirait "
        "pas sans réparation. Enregistrez-le de nouveau depuis Word, puis "
        "réessayez."
    ),
    "unreadable": (
        "Une partie du document est illisible : enregistrez-le de nouveau "
        "depuis Word, puis réessayez."
    ),
}

_OUTPUT_CHECK = (
    "La vérification du document produit a échoué ({reason}) : rien n'a été "
    "écrit."
)


# ── Public types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Substitution:
    """One literal → placeholder replacement.

    * ``literal`` — the text as it reads in the document (compared after the
      one-for-one fold, case-sensitively), :data:`MIN_LITERAL_CHARS` to
      :data:`MAX_LITERAL_CHARS` characters, no edge whitespace, at least one
      letter or digit, no brace, no control character.
    * ``placeholder`` — the field name (``client.nom_complet``), or the same
      name written ``{{client.nom_complet}}``.
    * ``expected_occurrences`` — the substituted count the caller expects; it
      must EQUAL the logical count or :func:`templatize` refuses. ``None`` is
      accepted by :func:`analyse` only (a preview that wants the counts).
    * ``whole_word`` — refuse an occurrence glued to a letter or digit.
    * ``expect`` — optional classification the placeholder must have
      (``auto``/``manual``/``passthrough``), refused when it differs.
    """

    literal: str
    placeholder: str
    expected_occurrences: Optional[int]
    whole_word: bool = True
    expect: Optional[str] = None


@dataclass(frozen=True)
class Blocker:
    """A reason the SOURCE cannot be templatized: a stable ``code``, a French
    ``message`` and the ``parts`` concerned (package entry names)."""

    code: str
    message: str
    parts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubstitutionReport:
    """What one substitution found.

    * ``index`` — its position in the caller's list (0-based);
    * ``placeholder`` — the normalized name (``""`` when invalid);
    * ``classification`` — ``auto``/``manual``/``passthrough``, ``None``
      when the name is invalid;
    * ``substituted`` — LOGICAL substituted count (the first branch of a
      text box only), the number compared with ``expected``;
    * ``by_part`` — that count per target part;
    * ``in_alternate_branches`` — occurrences ALSO substituted in the other
      copies of a text box (``mc:Fallback``), not counted above;
    * ``in_field_results`` / ``in_bound_controls`` / ``blocked_by_markup``
      — occurrences deliberately NOT substituted;
    * ``in_non_target_parts`` — occurrences per reported part (footnotes,
      endnotes, comments, docProps), never substituted;
    * ``fallback_consistent`` — every text box holds the same count in each
      of its copies.
    """

    index: int
    placeholder: str
    classification: Optional[str]
    expected: Optional[int]
    substituted: int = 0
    by_part: dict = field(default_factory=dict)
    in_alternate_branches: int = 0
    in_field_results: int = 0
    in_bound_controls: int = 0
    blocked_by_markup: int = 0
    in_non_target_parts: dict = field(default_factory=dict)
    fallback_consistent: bool = True

    @property
    def not_substituted(self) -> int:
        """Occurrences seen and left in place, wherever they are."""
        return (self.in_field_results + self.in_bound_controls
                + self.blocked_by_markup + sum(self.in_non_target_parts.values()))


@dataclass(frozen=True)
class TemplatizeResult:
    """The outcome of :func:`templatize` / :func:`analyse`.

    ``data`` is the templatized archive (``None`` from :func:`analyse`, and
    whenever the call is refused). ``blockers`` are reasons the SOURCE
    cannot be templatized; ``errors`` reasons the REQUEST cannot be applied
    (invalid substitution, count mismatch, text-box mismatch, a failed
    output check). ``warnings`` never refuse. ``rewritten_parts`` names the
    target parts that changed.
    """

    data: Optional[bytes]
    substitutions: tuple[SubstitutionReport, ...]
    blockers: tuple[Blocker, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rewritten_parts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers and not self.errors


def fold(text: str) -> str:
    """*text* as the matcher compares it (the one-for-one fold)."""
    return text.translate(_FOLD)


# ── Internal structures ──────────────────────────────────────────────────


class _Malformed(Exception):
    """A part the lexer cannot read to its end."""


class _TooMuchText(Exception):
    """The visible-text budget is exhausted."""


class _TooComplex(Exception):
    """The token budget is exhausted."""


class _TooManyCandidates(Exception):
    """The candidate budget is exhausted."""


class _Budget:
    __slots__ = ("chars", "candidates", "tokens")

    def __init__(self) -> None:
        self.chars = 0
        self.candidates = 0
        self.tokens = 0

    def candidate(self) -> None:
        self.candidates += 1
        if self.candidates > MAX_CANDIDATES:
            raise _TooManyCandidates


class _Segment:
    """One ``<w:t>`` text node (or one docProps value) in a part stream."""

    __slots__ = ("open_token", "text_token", "text", "start", "reason", "path")

    def __init__(self, open_token: int, text_token: int, text: str, start: int,
                 reason: Optional[str], path: tuple) -> None:
        self.open_token = open_token
        self.text_token = text_token
        self.text = text
        self.start = start
        self.reason = reason     # None (editable) | "field" | "bound"
        self.path = path         # ((alternate-content id, branch), …)


class _Part:
    __slots__ = ("name", "mode", "tokens", "raw", "text", "char_seg",
                 "segments", "ac_branches", "has_soft", "mask", "_stripped",
                 "encoding_bom")

    def __init__(self, name: str, mode: str, tokens: list[str], raw: str,
                 char_seg: array, segments: list[_Segment],
                 ac_branches: dict[int, int], has_soft: bool,
                 encoding_bom: bool) -> None:
        self.name = name
        self.mode = mode
        self.tokens = tokens
        self.raw = raw
        self.text = raw.translate(_FOLD)
        self.char_seg = char_seg
        self.segments = segments
        self.ac_branches = ac_branches
        self.has_soft = has_soft
        self.encoding_bom = encoding_bom
        self.mask = bytearray(len(raw))
        for pattern in (_ANY_TOKEN_RE, _BRACE_SPAN_RE):
            for match in pattern.finditer(raw):
                start, end = match.span()
                self.mask[start:end] = b"\x01" * (end - start)
        self._stripped: Optional[tuple[str, array]] = None

    def stripped(self) -> tuple[str, array]:
        """The folded stream without its soft marks, and the position map."""
        if self._stripped is None:
            positions = array(
                "i", (k for k, ch in enumerate(self.text) if ch != _SOFT))
            self._stripped = (self.text.replace(_SOFT, ""), positions)
        return self._stripped


@dataclass
class _Prepared:
    index: int
    name: str
    folded: str
    whole_word: bool
    expected: Optional[int]
    classification: str
    starts_word: bool
    ends_word: bool


@dataclass
class _Tally:
    substituted: int = 0
    by_part: dict = field(default_factory=dict)
    alternate: int = 0
    field_results: int = 0
    bound: int = 0
    markup: int = 0
    non_target: dict = field(default_factory=dict)
    # (part, alternate-content id) → {branch: substituted count}
    branch_hits: dict = field(default_factory=dict)


# ── Text helpers ─────────────────────────────────────────────────────────


def _is_word_char(ch: str) -> bool:
    return ch == "_" or unicodedata.category(ch)[0] in "LNM"


def _legal_xml_char(cp: int) -> bool:
    return (cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF)


def _decode_text(token: str) -> str:
    """The characters a text token stands for, entities decoded; raises
    :class:`_Malformed` on anything that is not well-formed XML text."""
    if _HARD in token or _SOFT in token:
        raise _Malformed
    if "&" not in token:
        return token
    out: list[str] = []
    pos = 0
    while True:
        amp = token.find("&", pos)
        if amp == -1:
            out.append(token[pos:])
            return "".join(out)
        out.append(token[pos:amp])
        match = _ENTITY_RE.match(token, amp)
        if match is None:
            raise _Malformed
        hexa, decimal, named = match.groups()
        if named:
            out.append(_NAMED_ENTITIES[named])
        else:
            cp = int(hexa, 16) if hexa else int(decimal)
            if not _legal_xml_char(cp):
                raise _Malformed
            out.append(chr(cp))
        pos = match.end()


def _escape_text(text: str) -> str:
    """Text as a ``<w:t>`` node holds it. A carriage return is kept as a
    reference: raw, an XML parser would read it back as a line feed."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\r", "&#13;"))


def _quoted_value(match: Optional[re.Match]) -> Optional[str]:
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _whitespace_shape(text: str) -> tuple[bool, bool, bool]:
    """``(leading, trailing, doubled)`` whitespace — what an XML consumer
    may drop from a text node that lacks ``xml:space="preserve"``."""
    if not text:
        return (False, False, False)
    return (text[0].isspace(), text[-1].isspace(), "  " in text)


def _edit_needs_preserve(old: str, new: str) -> bool:
    """True when the edit brought whitespace to an edge (or doubled it) that
    stood inside the old text. A shape the old text ALREADY had without the
    attribute is left exactly as Word was reading it."""
    return any(n and not o for n, o in zip(_whitespace_shape(new),
                                           _whitespace_shape(old)))


def _with_preserve(open_tag: str) -> str:
    """*open_tag* (a ``<w:t …>``) carrying ``xml:space="preserve"``."""
    match = _XML_SPACE_RE.search(open_tag)
    if match is not None:
        if _quoted_value(match) == "preserve":
            return open_tag
        start, end = match.span()
        return open_tag[:start] + ' xml:space="preserve"' + open_tag[end:]
    return open_tag[:4] + ' xml:space="preserve"' + open_tag[4:]


def _decode_part(data: bytes, *, target: bool) -> tuple[str, bool]:
    """``(text, had a UTF-8 BOM)``. A TARGET part must be UTF-8 — the fill
    engine decodes UTF-8, so a UTF-16 one could never be filled — and raises
    ``UnicodeDecodeError`` otherwise; a reported part, only read, may also
    be UTF-16 (with its BOM)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        if target:
            raise UnicodeDecodeError("utf-8", data[:2], 0, 2, "UTF-16 part")
        return data.decode("utf-16"), False
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), True
    return data.decode("utf-8"), False


# ── The lexer ────────────────────────────────────────────────────────────


def _lex_part(name: str, xml: str, mode: str, budget: _Budget,
              found: dict[str, list[str]], *, bom: bool = False) -> _Part:
    """Tokenize one part and build its character stream.

    Source blockers seen on the way are recorded in *found* (code → parts).
    Raises :class:`_Malformed` (a stray ``<``, unbalanced tags, markup inside
    a text node, an invalid entity), :class:`_TooMuchText` or
    :class:`_TooComplex`.
    """
    target = mode == _MODE_TARGET
    props = mode == _MODE_PROPS
    comments_part = name == "word/comments.xml"
    tokens: list[str] = []
    pieces: list[str] = []
    char_seg = array("i")
    segments: list[_Segment] = []
    ac_branches: dict[int, int] = {}
    stack: list[str] = []
    prop_depth = 0
    field_depth = 0
    fld_simple = 0
    sdt_stack: list[bool] = []
    ac_stack: list[list[int]] = []
    ac_counter = 0
    in_t = False
    t_open = -1
    t_token = -1
    t_text = ""
    t_reason: Optional[str] = None
    t_path: tuple = ()
    length = 0
    at_boundary = True
    has_soft = False
    root_seen = not target
    seen: set[str] = set()

    def note(code: str) -> None:
        if code not in seen:
            seen.add(code)
            found.setdefault(code, []).append(name)

    def add_segment(open_token: int, text_token: int, text: str,
                    reason: Optional[str], path: tuple) -> None:
        nonlocal length, at_boundary
        budget.chars += len(text)
        if budget.chars > MAX_VISIBLE_CHARS:
            raise _TooMuchText
        seg_id = len(segments)
        segments.append(_Segment(open_token, text_token, text, length,
                                 reason, path))
        pieces.append(text)
        char_seg.extend(array("i", [seg_id]) * len(text))
        length += len(text)
        at_boundary = False

    token_room = MAX_TOKENS - budget.tokens
    pos = 0
    for match in _TOKEN_RE.finditer(xml):
        if match.start() != pos:
            raise _Malformed
        tok = match.group()
        pos = match.end()
        index = len(tokens)
        if index >= token_room:
            raise _TooComplex
        tokens.append(tok)

        if tok[0] != "<":
            if in_t:
                t_token = index
                t_text = _decode_text(tok)
            elif props:
                text = _decode_text(tok)
                if text.strip():
                    if not at_boundary:
                        pieces.append(_HARD)
                        char_seg.append(_SEG_HARD)
                        length += 1
                    add_segment(-1, index, text, None, ())
                    pieces.append(_HARD)
                    char_seg.append(_SEG_HARD)
                    length += 1
                    at_boundary = True
            continue

        second = tok[1:2]
        if in_t and second != "/":
            # A <w:t> holds text only — a comment, PI or element inside one
            # is not Word's output, and splitting its text around it would
            # make the edit drop characters.
            raise _Malformed
        if second == "?":
            if not tok.endswith("?>"):
                raise _Malformed
            continue
        if second == "!":
            if tok.startswith("<!--") and tok.endswith("-->"):
                continue
            raise _Malformed            # CDATA, DOCTYPE: not Word's output
        # A « > » inside an attribute value ends the tag early: the quotes
        # of the token no longer pair up.
        if tok.count('"') % 2:
            raise _Malformed
        name_match = _TAG_NAME_RE.match(tok)
        if name_match is None:
            raise _Malformed
        qname = name_match.group(1)

        if second == "/":
            if not stack or stack[-1] != qname:
                raise _Malformed
            stack.pop()
            if in_t:
                # The stack check above guarantees qname == "w:t".
                if t_text:
                    add_segment(t_open, t_token, t_text, t_reason, t_path)
                in_t = False
                continue
            if prop_depth:
                if qname in _PROPERTY_CONTAINERS:
                    prop_depth -= 1
                continue
            if qname in _TRANSPARENT:
                continue
            if qname == "w:fldSimple":
                fld_simple = max(0, fld_simple - 1)
            elif qname == "w:sdt":
                if sdt_stack:
                    sdt_stack.pop()
            elif qname == _AC:
                if ac_stack:
                    ac_stack.pop()
            if not at_boundary:
                pieces.append(_HARD)
                char_seg.append(_SEG_HARD)
                length += 1
                at_boundary = True
            continue

        # An opening or empty-element tag.
        empty = tok.endswith("/>")
        if not root_seen:
            root_seen = True
            declared = _quoted_value(_W_NAMESPACE_RE.search(tok))
            if declared is not None and _STRICT_MARK in declared:
                note("strict_ooxml")
            elif declared != _TRANSITIONAL_W:
                note("namespace")
        if qname in _TRACKED_CHANGES:
            note("tracked_changes")
        elif qname in _COMMENT_ANCHORS or (comments_part
                                            and qname == "w:comment"):
            note("comments")
        if not empty:
            stack.append(qname)

        if prop_depth:
            if not empty and qname in _PROPERTY_CONTAINERS:
                prop_depth += 1
            if qname.endswith(":dataBinding") and sdt_stack:
                sdt_stack[-1] = True
            continue
        if qname in _PROPERTY_CONTAINERS:
            if not empty:
                prop_depth += 1
            continue
        if qname == "w:t":
            if not empty:
                in_t = True
                t_open = index
                t_token = -1
                t_text = ""
                if field_depth or fld_simple:
                    t_reason = "field"
                elif any(sdt_stack):
                    t_reason = "bound"
                else:
                    t_reason = None
                t_path = tuple((a, b) for a, b in ac_stack)
            continue
        if qname in _TRANSPARENT:
            continue
        if qname == "w:noBreakHyphen":
            pieces.append("-")
            char_seg.append(_SEG_MARKUP)
            length += 1
            at_boundary = False
            continue
        if qname == "w:softHyphen":
            pieces.append(_SOFT)
            char_seg.append(_SEG_SOFT)
            length += 1
            at_boundary = False
            has_soft = True
            continue
        if qname == "w:fldChar":
            kind = _quoted_value(_FLDCHAR_TYPE_RE.search(tok))
            if kind == "begin":
                field_depth += 1
            elif kind == "end":
                field_depth = max(0, field_depth - 1)
        elif qname == "w:fldSimple":
            if not empty:
                fld_simple += 1
        elif qname == "w:sdt":
            if not empty:
                sdt_stack.append(False)
        elif qname == _AC:
            if not empty:
                ac_counter += 1
                ac_stack.append([ac_counter, -1])
                ac_branches[ac_counter] = 0
        elif qname in _AC_BRANCHES and ac_stack:
            ac_stack[-1][1] += 1
            ac_branches[ac_stack[-1][0]] = ac_stack[-1][1] + 1
        if not at_boundary:
            pieces.append(_HARD)
            char_seg.append(_SEG_HARD)
            length += 1
            at_boundary = True

    if pos != len(xml) or stack or in_t:
        raise _Malformed
    budget.tokens += len(tokens)
    return _Part(name, mode, tokens, "".join(pieces), char_seg, segments,
                 ac_branches, has_soft, bom)


# ── Request validation ───────────────────────────────────────────────────


def _label(index: int) -> str:
    return f"Substitution n° {index + 1}"


def _normalized_name(placeholder: str) -> Optional[str]:
    """The field name *placeholder* designates, or ``None`` when invalid."""
    if not isinstance(placeholder, str):
        return None
    candidate = placeholder
    if candidate.startswith("{{") or candidate.endswith("}}"):
        match = PLACEHOLDER_RE.fullmatch(candidate)
        if match is None:
            return None
        candidate = match.group(1)
    if not candidate or len(candidate) > MAX_PLACEHOLDER_CHARS:
        return None
    if _NAME_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _classification(name: str) -> str:
    result = classify_placeholders([name])
    if name in result.auto:
        return "auto"
    if name in result.manual:
        return "manual"
    return "passthrough"


def _literal_error(literal: object, label: str) -> Optional[str]:
    if not isinstance(literal, str) or not (
            MIN_LITERAL_CHARS <= len(literal) <= MAX_LITERAL_CHARS):
        return (f"{label} : le texte à remplacer doit compter de "
                f"{MIN_LITERAL_CHARS} à {MAX_LITERAL_CHARS} caractères.")
    if literal[0].isspace() or literal[-1].isspace():
        return (f"{label} : le texte à remplacer ne doit ni commencer ni "
                "finir par une espace.")
    if "{" in literal or "}" in literal:
        return (f"{label} : le texte à remplacer ne peut pas contenir "
                "d'accolade — un champ {{…}} existant n'est jamais remplacé.")
    for ch in literal:
        if ch in (_HARD, _SOFT) or unicodedata.category(ch) in ("Cc", "Cs"):
            return (f"{label} : le texte à remplacer contient un caractère "
                    "de contrôle (tabulation, saut de ligne…) : dans Word ce "
                    "sont des séparateurs, qui ne peuvent pas faire partie "
                    "d'un texte à remplacer.")
    if not any(ch.isalnum() for ch in literal):
        return (f"{label} : le texte à remplacer doit contenir au moins une "
                "lettre ou un chiffre.")
    return None


def _prepare(substitutions: Sequence[Substitution], *, dry: bool
             ) -> tuple[list[Optional[_Prepared]], list[str], list[str]]:
    """``(prepared per index — None when invalid, errors, warnings)``."""
    prepared: list[Optional[_Prepared]] = []
    errors: list[str] = []
    warnings: list[str] = []
    if not substitutions:
        errors.append("Aucune substitution fournie : indiquez au moins un "
                      "texte à remplacer.")
        return prepared, errors, warnings
    if len(substitutions) > MAX_SUBSTITUTIONS:
        errors.append(f"Trop de substitutions d'un coup : au plus "
                      f"{MAX_SUBSTITUTIONS}.")
        return [None] * len(substitutions), errors, warnings
    seen_literals: dict[str, int] = {}
    for index, sub in enumerate(substitutions):
        label = _label(index)
        problem = _literal_error(sub.literal, label)
        name = _normalized_name(sub.placeholder)
        if problem is None and name is None:
            problem = (f"{label} : le nom de champ doit s'écrire en lettres, "
                       "chiffres, points ou soulignés (p. ex. "
                       "client.nom_complet), au plus "
                       f"{MAX_PLACEHOLDER_CHARS} caractères.")
        expected = sub.expected_occurrences
        if problem is None:
            if expected is None:
                if not dry:
                    problem = (f"{label} : le nombre d'occurrences attendu est "
                               "requis — lancez d'abord l'analyse pour le "
                               "connaître.")
            elif (isinstance(expected, bool) or not isinstance(expected, int)
                  or not 1 <= expected <= MAX_EXPECTED_OCCURRENCES):
                problem = (f"{label} : le nombre d'occurrences attendu doit "
                           f"être un entier de 1 à {MAX_EXPECTED_OCCURRENCES}.")
        if problem is None and not isinstance(sub.whole_word, bool):
            problem = f"{label} : « whole_word » doit valoir vrai ou faux."
        if problem is None and sub.expect is not None and (
                sub.expect not in CLASSIFICATIONS):
            problem = (f"{label} : la classification attendue doit être "
                       "« auto », « manual » ou « passthrough ».")
        classification = _classification(name) if name else None
        if problem is None and sub.expect is not None and (
                sub.expect != classification):
            problem = (f"{label} : {{{{{name}}}}} se classe « "
                       f"{classification} », pas « {sub.expect} ».")
        folded = fold(sub.literal) if problem is None else ""
        if problem is None and folded in seen_literals:
            problem = (f"{label} : même texte à remplacer que la substitution "
                       f"n° {seen_literals[folded] + 1} (après normalisation "
                       "des espaces insécables et des apostrophes).")
        if problem is not None:
            errors.append(problem)
            prepared.append(None)
            continue
        seen_literals[folded] = index
        prepared.append(_Prepared(
            index=index, name=name, folded=folded,
            whole_word=sub.whole_word, expected=expected,
            classification=classification,
            starts_word=_is_word_char(folded[0]),
            ends_word=_is_word_char(folded[-1]),
        ))
        warnings.extend(_classification_warnings(index, name, classification,
                                                 sub.literal))
    return prepared, errors, warnings


def _classification_warnings(index: int, name: str, classification: str,
                             literal: str) -> list[str]:
    label = _label(index)
    if classification == "passthrough":
        return [f"{label} : {{{{{name}}}}} n'est pas un champ que "
                "l'application remplit — il restera tel quel dans chaque "
                "document généré, à compléter dans Word."]
    letters = [ch for ch in literal if ch.isalpha()]
    literal_caps = bool(letters) and all(ch == ch.upper() for ch in letters)
    name_caps = is_uppercase_name(name)
    if literal_caps and not name_caps and any(
            ch != ch.lower() for ch in letters):
        return [f"{label} : le texte remplacé est en majuscules, mais "
                f"{{{{{name}}}}} ne l'est pas — la valeur s'imprimera dans sa "
                f"casse d'origine ; écrivez {{{{{name.upper()}}}}} pour une "
                "valeur en majuscules."]
    if name_caps and not literal_caps:
        return [f"{label} : {{{{{name}}}}} est en majuscules — la valeur "
                "s'imprimera en majuscules, alors que le texte remplacé ne "
                "l'est pas."]
    return []


# ── Matching ─────────────────────────────────────────────────────────────


def _boundaries_ok(text: str, start: int, end: int, prep: _Prepared) -> bool:
    """Whole-word check, looking through soft hyphens."""
    if prep.starts_word:
        k = start - 1
        while k >= 0 and text[k] == _SOFT:
            k -= 1
        if k >= 0 and _is_word_char(text[k]):
            return False
    if prep.ends_word:
        k = end
        while k < len(text) and text[k] == _SOFT:
            k += 1
        if k < len(text) and _is_word_char(text[k]):
            return False
    return True


def _primary(path: tuple) -> bool:
    return all(branch == 0 for _, branch in path)


def _scan_part(part: _Part, order: list[_Prepared], tallies: dict[int, _Tally],
               edits: dict[int, list[tuple[int, int, str]]],
               inserted: dict[str, int], budget: _Budget) -> None:
    """Find every occurrence of every substitution in *part* (longest first,
    masking as it goes), tally them, and — for a target part — record the
    edits of the substitutable ones."""
    text = part.text
    mask = part.mask
    char_seg = part.char_seg
    segments = part.segments
    target = part.mode == _MODE_TARGET
    for prep in order:
        tally = tallies[prep.index]
        needle = prep.folded
        size = len(needle)
        at = text.find(needle)
        while at != -1:
            budget.candidate()
            end = at + size
            if mask.find(1, at, end) != -1 or (
                    prep.whole_word and not _boundaries_ok(text, at, end, prep)):
                at = text.find(needle, at + 1)
                continue
            mask[at:end] = b"\x01" * size
            span = char_seg[at:end]
            if not target:
                tally.non_target[part.name] = tally.non_target.get(
                    part.name, 0) + 1
                at = text.find(needle, end)
                continue
            markup = _SEG_MARKUP in span
            first = next(s for s in span if s >= 0)
            segment = segments[first]
            primary = _primary(segment.path)
            if markup or segment.reason is not None:
                if primary:
                    if markup:
                        tally.markup += 1
                    elif segment.reason == "field":
                        tally.field_results += 1
                    else:
                        tally.bound += 1
                at = text.find(needle, end)
                continue
            if primary:
                tally.substituted += 1
                tally.by_part[part.name] = tally.by_part.get(part.name, 0) + 1
            else:
                tally.alternate += 1
            path = segment.path
            for depth, (ac_id, branch) in enumerate(path):
                if _primary(path[depth + 1:]):
                    counts = tally.branch_hits.setdefault(
                        (part.name, ac_id), {})
                    counts[branch] = counts.get(branch, 0) + 1
            inserted[prep.name] = inserted.get(prep.name, 0) + 1
            _record_edit(part, at, end, "{{" + prep.name + "}}", edits)
            at = text.find(needle, end)
        if target and part.has_soft:
            _count_soft_blocked(part, prep, tally, budget)


def _count_soft_blocked(part: _Part, prep: _Prepared, tally: _Tally,
                        budget: _Budget) -> None:
    """Occurrences a soft hyphen interrupts: reported, never substituted —
    and masked, like every occurrence a longer literal found, so a shorter
    literal never lands inside one."""
    stripped, positions = part.stripped()
    needle = prep.folded
    size = len(needle)
    at = stripped.find(needle)
    while at != -1:
        budget.candidate()
        start = positions[at]
        end = positions[at + size - 1] + 1
        if end - start > size and part.mask.find(1, start, end) == -1 and (
                not prep.whole_word
                or _boundaries_ok(part.text, start, end, prep)):
            first = next((s for s in part.char_seg[start:end] if s >= 0), -1)
            if first >= 0 and _primary(part.segments[first].path):
                tally.markup += 1
            part.mask[start:end] = b"\x01" * (end - start)
            at = stripped.find(needle, at + size)
            continue
        at = stripped.find(needle, at + 1)


def _record_edit(part: _Part, start: int, end: int, insert: str,
                 edits: dict[int, list[tuple[int, int, str]]]) -> None:
    """The placeholder into the first segment, the literal's other
    characters deleted from the following ones."""
    k = start
    first = True
    while k < end:
        seg_id = part.char_seg[k]
        segment = part.segments[seg_id]
        seg_end = min(end, segment.start + len(segment.text))
        edits.setdefault(seg_id, []).append(
            (k - segment.start, seg_end - segment.start,
             insert if first else ""))
        first = False
        k = seg_end


def _apply_edits(part: _Part, edits: dict[int, list[tuple[int, int, str]]]
                 ) -> bytes:
    tokens = list(part.tokens)
    for seg_id, seg_edits in edits.items():
        segment = part.segments[seg_id]
        text = segment.text
        out: list[str] = []
        pos = 0
        for start, end, insert in sorted(seg_edits):
            out.append(text[pos:start])
            out.append(insert)
            pos = end
        out.append(text[pos:])
        new_text = "".join(out)
        tokens[segment.text_token] = _escape_text(new_text)
        if _edit_needs_preserve(text, new_text):
            tokens[segment.open_token] = _with_preserve(
                tokens[segment.open_token])
    data = "".join(tokens).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if part.encoding_bom else data


# ── Reading the package ──────────────────────────────────────────────────


def _blocker(code: str, parts: Sequence[str] = (), message: str = "") -> Blocker:
    return Blocker(code, message or _BLOCKER_MESSAGES[code], tuple(parts))


def _read_parts(zf: zipfile.ZipFile, budget: _Budget
                ) -> tuple[list[_Part], dict[str, bytes], list[Blocker]]:
    """Lex every target and reported part. ``(parts, raw target bytes,
    blockers)`` — parts in target order, then the reported ones."""
    names = zf.namelist()
    if len(set(names)) != len(names):
        return [], {}, [_blocker("duplicate_entry")]
    targets = _target_names(zf)
    reported = [n for n in names if _REPORTED_RE.match(n)]
    parts: list[_Part] = []
    raw: dict[str, bytes] = {}
    found: dict[str, list[str]] = {}
    remaining = MAX_TOTAL_DECOMPRESSED_BYTES
    for name in [*targets, *reported]:
        try:
            data = _read_entry_bounded(
                zf, name, min(MAX_SINGLE_XML_BYTES, remaining))
        except DocxFillError as exc:
            return [], {}, [_blocker("unreadable", [name], str(exc))]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError,
                EOFError, OSError, ValueError):
            return [], {}, [_blocker("unreadable", [name])]
        remaining -= len(data)
        target = name in targets
        try:
            xml, bom = _decode_part(data, target=target)
        except UnicodeDecodeError:
            found.setdefault("encoding", []).append(name)
            continue
        if target:
            raw[name] = data
            mode = _MODE_TARGET
        elif name.startswith("docProps/"):
            mode = _MODE_PROPS
        else:
            mode = _MODE_WORD
        try:
            parts.append(_lex_part(name, xml, mode, budget, found, bom=bom))
        except _Malformed:
            found.setdefault("malformed_xml", []).append(name)
        except _TooMuchText:
            return [], {}, [_blocker("too_much_text")]
        except _TooComplex:
            return [], {}, [_blocker("too_complex")]
    blockers = [_blocker(code, found[code]) for code in (
        "strict_ooxml", "namespace", "encoding", "malformed_xml",
        "tracked_changes", "comments") if code in found]
    return parts, raw, blockers


# ── The two entry points ─────────────────────────────────────────────────


def analyse(docx_bytes: bytes, substitutions: Sequence[Substitution]
            ) -> TemplatizeResult:
    """Count what :func:`templatize` would do; never produce bytes.

    ``expected_occurrences`` may be ``None`` here (the counts are what the
    caller wants to learn); when it is given, a mismatch is reported in
    ``errors`` exactly as :func:`templatize` would refuse it.
    """
    return _run(docx_bytes, substitutions, write=False)


def templatize(docx_bytes: bytes, substitutions: Sequence[Substitution]
               ) -> TemplatizeResult:
    """Replace each literal by its placeholder; all or nothing.

    ``data`` holds the new archive only when ``ok`` — no blocker, every
    substitution valid, every logical count equal to its
    ``expected_occurrences``, every text box consistent, and every output
    check passed. Otherwise ``data`` is ``None`` and nothing was produced.
    """
    return _run(docx_bytes, substitutions, write=True)


def _empty_report(index: int, sub: Substitution) -> SubstitutionReport:
    """The report of a substitution nothing was counted for (a refused
    source, an invalid request): its name and class when they are valid."""
    name = _normalized_name(sub.placeholder)
    expected = sub.expected_occurrences
    if isinstance(expected, bool) or not isinstance(expected, int):
        expected = None
    return SubstitutionReport(
        index=index,
        placeholder=name or "",
        classification=_classification(name) if name else None,
        expected=expected,
    )


def _check_input(docx_bytes: object, substitutions: object) -> None:
    if not isinstance(docx_bytes, (bytes, bytearray)):
        raise TypeError("docx_bytes must be bytes")
    if isinstance(substitutions, (str, bytes)) or not isinstance(
            substitutions, (list, tuple)):
        raise TypeError("substitutions must be a list of Substitution")
    for sub in substitutions:
        if not isinstance(sub, Substitution):
            raise TypeError("substitutions must be a list of Substitution")


def _run(docx_bytes: bytes, substitutions: Sequence[Substitution], *,
         write: bool) -> TemplatizeResult:
    _check_input(docx_bytes, substitutions)
    docx_bytes = bytes(docx_bytes)
    prepared, errors, warnings = _prepare(substitutions, dry=not write)
    empty_reports = tuple(
        _empty_report(index, sub) for index, sub in enumerate(substitutions))

    structural, zf = _structural_errors(docx_bytes)
    if structural or zf is None:
        blocker = _blocker("unreadable", message=(
            structural[0] if structural else _BLOCKER_MESSAGES["unreadable"]))
        return TemplatizeResult(None, empty_reports, (blocker,),
                                tuple(errors), tuple(warnings))
    budget = _Budget()
    with zf:
        parts, raw_targets, blockers = _read_parts(zf, budget)
        if blockers or errors:
            return TemplatizeResult(None, empty_reports, tuple(blockers),
                                    tuple(errors), tuple(warnings))

        valid = [p for p in prepared if p is not None]
        order = sorted(valid, key=lambda p: (-len(p.folded), p.index))
        tallies = {p.index: _Tally() for p in valid}
        part_edits: dict[str, dict[int, list[tuple[int, int, str]]]] = {}
        part_inserted: dict[str, dict[str, int]] = {}
        try:
            for part in parts:
                edits: dict[int, list[tuple[int, int, str]]] = {}
                inserted: dict[str, int] = {}
                _scan_part(part, order, tallies, edits, inserted, budget)
                if edits:
                    part_edits[part.name] = edits
                    part_inserted[part.name] = inserted
        except _TooManyCandidates:
            errors.append(
                "Le document contient trop d'occurrences candidates des textes "
                "à remplacer : précisez-les (des textes plus longs), puis "
                "réessayez.")
            return TemplatizeResult(None, empty_reports, (), tuple(errors),
                                    tuple(warnings))

        reports, count_errors, count_warnings = _reports(
            valid, tallies, parts, len(substitutions), empty_reports)
        errors.extend(count_errors)
        warnings.extend(count_warnings)
        if not write or errors:
            warnings.extend(_source_split_warnings(docx_bytes, part_inserted))
            return TemplatizeResult(None, reports, (), tuple(errors),
                                    tuple(warnings))

        rewritten = {
            name: _apply_edits(next(p for p in parts if p.name == name), edits)
            for name, edits in part_edits.items()
        }
        try:
            output = _write_archive(zf, rewritten)
        except (DocxFillError, zipfile.BadZipFile, RuntimeError,
                NotImplementedError, EOFError, OSError, ValueError):
            errors.append(_OUTPUT_CHECK.format(reason="archive illisible"))
            return TemplatizeResult(None, reports, (), tuple(errors),
                                    tuple(warnings))

    problem = _check_output(output, rewritten, raw_targets, part_inserted)
    if problem is not None:
        errors.append(_OUTPUT_CHECK.format(reason=problem))
        return TemplatizeResult(None, reports, (), tuple(errors),
                                tuple(warnings))
    warnings.extend(_source_split_warnings(docx_bytes, part_inserted))
    return TemplatizeResult(output, reports, (), (), tuple(warnings),
                            tuple(n for n in _target_order(rewritten, parts)))


def _target_order(rewritten: dict[str, bytes], parts: list[_Part]) -> list[str]:
    return [p.name for p in parts if p.name in rewritten]


def _reports(valid: list[_Prepared], tallies: dict[int, _Tally],
             parts: list[_Part], count: int,
             empty_reports: tuple[SubstitutionReport, ...]
             ) -> tuple[tuple[SubstitutionReport, ...], list[str], list[str]]:
    branches = {(p.name, ac_id): n for p in parts
                for ac_id, n in p.ac_branches.items()}
    by_index: dict[int, SubstitutionReport] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for prep in sorted(valid, key=lambda p: p.index):
        tally = tallies[prep.index]
        consistent = True
        for key, counts in tally.branch_hits.items():
            total = branches.get(key, 1)
            values = [counts.get(b, 0) for b in range(max(total, 1))]
            if len(set(values)) > 1:
                consistent = False
        report = SubstitutionReport(
            index=prep.index,
            placeholder=prep.name,
            classification=prep.classification,
            expected=prep.expected,
            substituted=tally.substituted,
            by_part=dict(tally.by_part),
            in_alternate_branches=tally.alternate,
            in_field_results=tally.field_results,
            in_bound_controls=tally.bound,
            blocked_by_markup=tally.markup,
            in_non_target_parts=dict(tally.non_target),
            fallback_consistent=consistent,
        )
        by_index[prep.index] = report
        label = f"{_label(prep.index)} ({{{{{prep.name}}}}})"
        if not consistent:
            errors.append(
                f"{label} : le texte n'apparaît pas le même nombre de fois "
                "dans les deux versions d'une zone de texte (mc:Choice et "
                "mc:Fallback) — rien n'a été écrit ; retapez la zone de texte "
                "dans Word.")
        if prep.expected is not None and tally.substituted != prep.expected:
            errors.append(
                f"{label} : {tally.substituted} occurrence(s) remplaçable(s), "
                f"{prep.expected} attendue(s) — rien n'a été écrit.")
        left = _left_in_place(report)
        if left:
            warnings.append(f"{label} : {left}.")
    reports = tuple(by_index.get(i, empty_reports[i]) for i in range(count))
    return reports, errors, warnings


def _left_in_place(report: SubstitutionReport) -> str:
    """French summary of the occurrences left in place, or ``""``."""
    bits: list[str] = []
    if report.in_field_results:
        bits.append(f"{report.in_field_results} dans le résultat d'un champ "
                    "Word (recalculé par Word)")
    if report.in_bound_controls:
        bits.append(f"{report.in_bound_controls} dans un contrôle de contenu "
                    "lié à des données")
    if report.blocked_by_markup:
        bits.append(f"{report.blocked_by_markup} coupée(s) par un trait "
                    "d'union conditionnel ou insécable")
    for part, n in report.in_non_target_parts.items():
        bits.append(f"{n} dans {part} (partie que le remplissage ne lit pas)")
    if not bits:
        return ""
    return "occurrence(s) laissée(s) telle(s) quelle(s) : " + " ; ".join(bits)


def _write_archive(zf: zipfile.ZipFile, rewritten: dict[str, bytes]) -> bytes:
    """Every entry through its own ``ZipInfo`` (order, compression,
    timestamps), the rewritten parts replaced, every other entry's bytes
    identical."""
    output = io.BytesIO()
    remaining = MAX_TOTAL_DECOMPRESSED_BYTES
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zf.infolist():
            data = _read_entry_bounded(zf, info.filename, remaining)
            remaining -= len(data)
            if info.filename in rewritten:
                data = rewritten[info.filename]
            zout.writestr(info, data)
    return output.getvalue()


def _name_count(xml_bytes: bytes, name: str) -> int:
    xml = _normalize_runs(xml_bytes.decode("utf-8", errors="replace"))
    return sum(1 for m in PLACEHOLDER_RE.finditer(xml) if m.group(1) == name)


def _check_output(output: bytes, rewritten: dict[str, bytes],
                  source_targets: dict[str, bytes],
                  part_inserted: dict[str, dict[str, int]]) -> Optional[str]:
    """``None`` when the templatized archive holds what was inserted, as the
    fill engine will see it; otherwise the French reason."""
    try:
        with zipfile.ZipFile(io.BytesIO(output)) as check:
            if check.testzip() is not None:
                return "archive illisible"
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
            OSError, ValueError):
        return "archive illisible"
    validation = validate_template(output)
    if validation.errors:
        return "gabarit invalide"
    names = {n for inserted in part_inserted.values() for n in inserted}
    for name in sorted(names):
        if name not in validation.placeholders:
            return f"champ {{{{{name}}}}} introuvable"
        if name in validation.split_run_suspects:
            return (f"champ {{{{{name}}}}} fragmenté — une occurrence déjà "
                    "présente du même champ est coupée entre deux passages ; "
                    "retapez-la dans Word")
    for part, inserted in part_inserted.items():
        for name, n in inserted.items():
            before = _name_count(source_targets[part], name)
            after = _name_count(rewritten[part], name)
            if after != before + n:
                return f"décompte de {{{{{name}}}}} dans {part}"
    return None


def _source_split_warnings(docx_bytes: bytes,
                           part_inserted: dict[str, dict[str, int]]) -> list[str]:
    """Placeholders the SOURCE already carries fragmented (they will not
    fill until retyped in Word) — named, so the lawyer can fix them."""
    inserted = {n for names in part_inserted.values() for n in names}
    suspects = [n for n in validate_template(docx_bytes).split_run_suspects
                if n not in inserted]
    if not suspects:
        return []
    listed = ", ".join("{{" + n + "}}" for n in suspects)
    return [f"Champ(s) déjà présent(s) mais fragmenté(s) dans le document "
            f"source, qui ne se rempliront pas avant d'être retapés dans "
            f"Word : {listed}."]
