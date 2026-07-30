"""Placeholder fill engine for .docx templates (Phase H — gabarits).

Pure stdlib (``zipfile``, ``re``, ``io``) — no Firestore, no Flask, no new
dependencies. The engine operates by direct string substitution on the XML
entries inside the zip archive, copying every other entry through
byte-identical. A ``python-docx``/``docxtpl`` load/save round-trip is
deliberately NOT used: it rewrites enough of the OOXML package that Word
refuses to open the result for letterhead templates with multiple
headers/footers, ``titlePg`` sections, and embedded fonts (empirical —
see SPEC_PHASE_H_GABARITS.md §1.1).

Word quirk: typed text is often split across multiple ``<w:r>`` runs
(autocorrect, formatting changes mid-typing), which fragments a
placeholder in the raw XML. Fragmented placeholders cannot be filled; they
are DETECTED at upload time (:func:`validate_template`) and reported so
the user can retype the placeholder in Word in one stroke.
"""

import io
import re
import zipfile
from dataclasses import dataclass, field

# {{name}} — French accents allowed, optional namespacing (dossier.titre),
# optional whitespace inside the braces.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-zÀ-ÿ0-9_.]+)\s*\}\}")

# An INNERMOST <w:p> paragraph element. Two deliberate deviations from a
# naive `<w:p\b[^>]*>.*?</w:p>` (regression-tested):
# 1. Self-closing blank paragraphs (`<w:p w:rsidR="..."/>` — Word's
#    standard serialization) must NOT match as an opening tag, or they get
#    swallowed into the following paragraph and cloned with it.
# 2. Paragraphs DO nest in OOXML via text boxes (<w:txbxContent> inside a
#    run), common in letterheads. The tempered body ((?!<w:p[\s/>]).)
#    refuses to cross another opening <w:p>, so the match always lands on
#    an innermost, balanced paragraph — cloning it never produces
#    unbalanced XML (which Word would refuse to open).
_PARAGRAPH_RE = re.compile(
    r"<w:p(?:\s[^>]*[^/])?>(?:(?!<w:p[\s/>]).)*?</w:p>", re.DOTALL
)

# Strips XML tags to expose visible text.  The body class excludes ``<`` (not
# just ``>``) so a run of unclosed ``<`` fails fast per position instead of
# re-scanning to end-of-string, keeping the substitution linear on adversarial
# .docx XML (CWE-1333).  Identical to ``<[^>]+>`` on well-formed XML, where a
# tag body never contains a literal ``<``.
_XML_TAG_RE = re.compile(r"<[^<>]*>")

# Fill targets inside the archive: main document + all headers/footers.
_TARGET_RE = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")

# C0 control characters except tab/newline/CR (handled separately).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Blank-line separator between block chunks (whitespace-only lines count —
# textarea input frequently carries stray spaces on empty lines).
_BLANK_LINE_RE = re.compile(r"\n\s*\n")

# ── Phase H.2 structural tokens (repeating table rows + conditional regions)
# Region open marker for a repeating row: {{#region}}. Conditional section:
# {{?cond}} … {{/cond}}. Distinct from a {{name}} placeholder by the leading
# #/?// sigil. Optional inner whitespace tolerated, like PLACEHOLDER_RE.
# Concrete matching uses the per-name pattern builders (_region_pattern /
# _cond_open_pattern / _cond_close_pattern), re.escape-bound to a specific
# region/condition name — not standalone regex constants.
# Any token — a {{name}} OR a {{#…}}/{{?…}}/{{/…}} marker — used ONLY by the
# split-run suspect scan so a fragmented marker is reported too (§3.4). The
# placeholder INVENTORY stays on PLACEHOLDER_RE (markers are structural, not
# fillable fields).
_ANY_TOKEN_RE = re.compile(r"\{\{\s*([#?/]?[A-Za-zÀ-ÿ0-9_.]+)\s*\}\}")

# An INNERMOST <w:tr> table row (mirrors _PARAGRAPH_RE). Rows nest when a
# table sits inside a cell, so the tempered body ((?!<w:tr[\s/>]).) refuses
# to cross another opening <w:tr> — the match lands on a balanced innermost
# row, and cloning it never yields unbalanced XML. `<w:tr(?:\s…)?>` matches a
# row carrying rsid attributes; it never matches <w:trPr> (a letter, not
# whitespace or '>', follows `<w:tr`).
_TABLE_ROW_RE = re.compile(
    r"<w:tr(?:\s[^>]*)?>(?:(?!<w:tr[\s/>]).)*?</w:tr>", re.DOTALL
)

# Two directly-adjacent <w:tbl> are invalid in Word — it merges them into one
# table (or auto-inserts a separator paragraph = a visible blank line). After a
# conditional removes the paragraphs that used to sit between two tables, we
# insert a MINIMAL (~1pt line, 1pt font) empty paragraph so the tables stay
# distinct AND the visible gap all but disappears. Lookahead so 3+ adjacent
# tables all get separated; `<w:tbl[\s>]` never matches <w:tblPr>.
_ADJACENT_TABLES_RE = re.compile(r"</w:tbl>\s*(?=<w:tbl[\s>])")
_TABLE_SEPARATOR = (
    '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" '
    'w:lineRule="exact"/><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr>'
    "</w:pPr></w:p>"
)

# ── Run normalization (heal Word's run-splitting so placeholders match) ──
# Word fragments a typed placeholder across multiple <w:r> runs — proofing
# (spell/grammar) brackets it in <w:proofErr> markers that force run
# boundaries, tracked changes wrap edits in <w:ins>, and mid-word format or
# language changes split runs (notably at the dot in a namespaced name like
# {{dossier.defendeur}}, where the two halves get a different proofing/lang
# rPr). A fragmented {{champ}} then can't be matched. We heal it BEFORE
# matching with a byte-level pass (no python-docx round-trip): strip the
# proofing markers, then fold each maximal CHAIN of adjacent text runs (each
# holding one <w:t>), merging a neighbour into the accumulator when either
# they carry identical formatting — Word's own save-time optimization — OR
# joining them bridges a placeholder (see _bridges_placeholder), in which
# case formatting differences are ignored and the accumulator's rPr wins,
# because the whole {{name}} is replaced by a single value anyway. Runs
# holding anything else (<w:br/>, <w:tab/>, <w:drawing>, field codes…) never
# match the chain unit, so they end the chain and are left untouched; a
# bookmark or comment marker between two runs does the same (they are no
# longer adjacent) — a genuinely STRUCTURAL split thus stays unmerged and is
# still reported as a suspect. The output still opens without repair (merging
# adjacent text runs is a valid OOXML operation), and a run that is NOT
# merged is re-emitted byte-for-byte.
#
# LINEARITY INVARIANT (CWE-1333) — none of the three patterns below contains
# a `.`, and none carries re.DOTALL. That is what keeps normalization linear,
# and it is not a style preference: a `.*?` body under DOTALL rescans to
# end-of-string once per unmatched opening tag, which is quadratic. Measured
# before this was fixed: a 1.2 KB .docx whose document.xml held 465 KB of
# unclosed `<w:proofErr …>` cost 45 SECONDS inside validate_template
# (repetitive XML deflates ~350:1, so MAX_COMPRESSED_BYTES never bounded the
# CPU — MAX_SINGLE_XML_BYTES is the real ceiling), and the unclosed-`<w:rPr>`
# variant multiplied with the old fixpoint loop into O(n³). Two tests pin the
# invariant by asserting `"." not in pattern` and `not flags & re.DOTALL`.

# `w:proofErr` is an EMPTY element per OOXML (CT_ProofErr carries only a
# `w:type` attribute, no children) and Word always writes it self-closing, so
# alternative 1 is the only shape real output has. Alternative 2 is defensive,
# for a producer that serializes the empty element as an open/close pair. Its
# body is `[^<]*` — deliberately NOT `.*?` — for BOTH linearity (above) and
# correctness: `.*?` under DOTALL DELETED everything between an opening marker
# and the next `</w:proofErr>`, including intervening <w:r> runs and the
# placeholder text they carried, which then vanished from the document AND
# from the placeholder inventory. `[^<]*` can never cross markup.
_PROOF_ERR_RE = re.compile(
    r"<w:proofErr\b[^>]*/>|<w:proofErr\b[^>]*>[^<]*</w:proofErr>"
)
# The optional run-properties block, tempered so the body cannot cross another
# <w:rPr>/</w:rPr>. Deliberate narrowing: an rPr that NESTS another one (only
# <w:rPrChange>, i.e. a tracked FORMATTING change) no longer matches, so such a
# run simply ends the chain — it keeps its own formatting and, if it fragments
# a placeholder, is reported as a suspect. That is the conservative direction.
# (The previous comment claimed "rPr never nests another rPr", which is false;
# `.*?` handled the nesting only by accident, via the very backtracking that
# made it quadratic.)
_RUN_RPR = r"(?:<w:rPr>(?:(?!</?w:rPr[\s/>])[\s\S])*</w:rPr>)?"
# One text run: `<w:r [attrs]>[rPr]<w:t [attrs]>text</w:t></w:r>`. The text
# body is `[^<]*` — a <w:t> node never contains a raw '<' (it is escaped as
# &lt;) — which also stops a body from swallowing markup and coalescing runs
# across paragraph boundaries. The run open tag is `<w:r(?:\s…)?>` so runs
# carrying revision attributes (`<w:r w:rsidR="…">`, common in real Word
# output) match too; those are pure save-tracking metadata and are dropped
# when a run is merged (Word reopens fine without them). The alternation
# `<w:r`-then-`\s`-or-`>` never matches <w:rPr>/<w:rFonts>/… (a letter, not
# whitespace or '>', follows `<w:r`).
_TEXT_RUN = (
    r"<w:r(?:\s[^>]*)?>(?P<rpr>" + _RUN_RPR + r")"
    r"<w:t(?:\s[^>]*)?>(?P<t>[^<]*)</w:t></w:r>"
)
_TEXT_RUN_RE = re.compile(_TEXT_RUN)
# A maximal chain of two or more adjacent text runs. Each unit is delimited
# and deterministic (it starts at `<w:r` and ends at `</w:r>`), so `(?:…){2,}`
# scans linearly with no inter-iteration ambiguity. The named groups inside
# repeat harmlessly (each iteration overwrites them and we ignore them here —
# _fold_run_chain re-scans the matched chain with _TEXT_RUN_RE to get every
# run). `{2,}` rather than `+` so a lone run is never even visited.
_RUN_CHAIN_RE = re.compile("(?:" + _TEXT_RUN + "){2,}")


def _bridges_placeholder(t1: str, t2: str) -> bool:
    """True when joining these two runs' text continues a ``{{…}}`` Word split.

    Either ``t1`` holds an unclosed ``{{`` (so the rest of the placeholder
    lives in following runs), or the split fell between the two opening
    braces (``…{`` | ``{…}}``). This is the frequent case where Word
    fragments a namespaced name at the dot (``{{dossier.`` | ``defendeur}}``)
    with a different language/proofing ``rPr`` on each half — which a
    formatting-only merge would refuse forever, so retyping never fixes it.

    It also stops on its own: once the accumulated text has swallowed its
    closing ``}}``, this returns False and the chain fold moves on.
    """
    last_open = t1.rfind("{{")
    if last_open != -1 and last_open > t1.rfind("}}"):
        return True
    return t1.endswith("{") and t2.startswith("{")


def _fold_run_chain(match: re.Match) -> str:
    """Fold one maximal chain of adjacent text runs, left to right.

    A neighbour is absorbed into the accumulator when their rPr are identical
    (Word's own save-time optimization) or when joining them bridges a
    placeholder; otherwise the accumulator is flushed and the neighbour
    becomes the new accumulator. Folding — rather than the pairwise
    substitution this replaces — fixes a real defect: ``re.sub`` consumed BOTH
    runs of a REFUSED pair, so the second was never offered to its right
    neighbour, and a placeholder split across differently-formatted runs
    healed or not depending on the PARITY of its alignment. That is the
    « fragmenté persists even after retyping » symptom. It is also one pass
    instead of a fixpoint loop: O(n), with no iteration cap to tune.

    A run that is never merged into is re-emitted VERBATIM (its original
    bytes), so normalization is a no-op on documents with nothing to heal.
    """
    runs = [
        (m.group("rpr"), m.group("t"), m.group(0))
        for m in _TEXT_RUN_RE.finditer(match.group(0))
    ]
    out: list[str] = []
    rpr, text, source = runs[0]
    merged = False
    for next_rpr, next_text, next_source in runs[1:]:
        if rpr == next_rpr or _bridges_placeholder(text, next_text):
            text += next_text
            merged = True
            continue
        out.append(
            # xml:space="preserve" so no boundary whitespace is lost on merge.
            f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'
            if merged
            else source
        )
        rpr, text, source, merged = next_rpr, next_text, next_source, False
    out.append(
        f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'
        if merged
        else source
    )
    return "".join(out)


def _normalize_runs(xml: str) -> str:
    """Strip proofing markers and coalesce same-format adjacent text runs.

    One linear pass: every chain is folded in a single visit (see
    _fold_run_chain), so there is no fixpoint loop and no pass budget. The
    previous implementation looped `re.sub` until stable, which needed one
    pass per fragment in the bridge regime — quadratic, and unbounded.
    """
    xml = _PROOF_ERR_RE.sub("", xml)
    return _RUN_CHAIN_RE.sub(_fold_run_chain, xml)

# ── Safety caps (§7.3 — zip-bomb defense) ──────────────────────────────
MAX_COMPRESSED_BYTES = 10 * 1024 * 1024
MAX_SINGLE_XML_BYTES = 25 * 1024 * 1024
MAX_TOTAL_DECOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ENTRY_COUNT = 2000

_REQUIRED_MEMBERS = ("[Content_Types].xml", "word/document.xml")
_ZIP_MAGIC = b"PK\x03\x04"


@dataclass
class TemplateValidation:
    """Result of :func:`validate_template`."""

    placeholders: list[str] = field(default_factory=list)
    split_run_suspects: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DocxFillError(ValueError):
    """Structural problem with the template archive (caps, members…)."""


# ── Internal helpers ────────────────────────────────────────────────────

def _structural_errors(docx_bytes: bytes) -> tuple[list[str], zipfile.ZipFile | None]:
    """Validate archive structure and caps; return (French errors, open zip)."""
    errors: list[str] = []
    if len(docx_bytes) > MAX_COMPRESSED_BYTES:
        errors.append("Le fichier dépasse la taille maximale de 10 Mo.")
        return errors, None
    if not docx_bytes.startswith(_ZIP_MAGIC):
        errors.append("Le fichier n'est pas un document Word (.docx) valide.")
        return errors, None
    try:
        zf = zipfile.ZipFile(io.BytesIO(docx_bytes))
    except zipfile.BadZipFile:
        errors.append("Le fichier n'est pas une archive .docx lisible.")
        return errors, None

    infos = zf.infolist()
    if len(infos) > MAX_ENTRY_COUNT:
        errors.append("L'archive contient trop d'entrées.")
        return errors, None

    total = 0
    for info in infos:
        name = info.filename
        if name.startswith("/") or name.startswith("\\") or ".." in name:
            errors.append("L'archive contient un chemin d'entrée interdit.")
            return errors, None
        total += info.file_size
        if _TARGET_RE.match(name) and info.file_size > MAX_SINGLE_XML_BYTES:
            errors.append("Une partie XML du document est trop volumineuse.")
            return errors, None
    if total > MAX_TOTAL_DECOMPRESSED_BYTES:
        errors.append("Le contenu décompressé du document est trop volumineux.")
        return errors, None

    names = set(zf.namelist())
    for member in _REQUIRED_MEMBERS:
        if member not in names:
            errors.append(
                "Le fichier ne contient pas la structure d'un document Word "
                f"({member} manquant)."
            )
            return errors, None

    return errors, zf


def _read_entry_bounded(zf: zipfile.ZipFile, name: str, cap: int) -> bytes:
    """Read an entry enforcing *cap* on the ACTUAL inflated size.

    The metadata caps in :func:`_structural_errors` check the
    central-directory ``file_size``, which a crafted archive can
    understate — this bounds the real decompression (zip-bomb defense in
    depth).
    """
    with zf.open(name) as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise DocxFillError(
            "Le contenu décompressé du document est trop volumineux."
        )
    return data


def _target_names(zf: zipfile.ZipFile) -> list[str]:
    """Fill-target entry names: document first, then headers, then footers."""
    names = [n for n in zf.namelist() if _TARGET_RE.match(n)]

    def sort_key(name: str) -> tuple[int, str]:
        if name == "word/document.xml":
            return (0, name)
        if name.startswith("word/header"):
            return (1, name)
        return (2, name)

    return sorted(names, key=sort_key)


def _names_in_text(text: str) -> list[str]:
    """Distinct placeholder names in order of first appearance."""
    seen: list[str] = []
    for match in PLACEHOLDER_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _all_tokens_in_text(text: str) -> list[str]:
    """Distinct tokens (names AND #/?// markers) in order of first appearance."""
    seen: list[str] = []
    for match in _ANY_TOKEN_RE.finditer(text):
        token = match.group(1)
        if token not in seen:
            seen.append(token)
    return seen


def _all_token_counts(text: str) -> dict[str, int]:
    """Count every token occurrence (names + markers) — for split detection."""
    counts: dict[str, int] = {}
    for match in _ANY_TOKEN_RE.finditer(text):
        token = match.group(1)
        counts[token] = counts.get(token, 0) + 1
    return counts


def _escape_xml(value: str) -> str:
    """XML-escape a plain-text value and strip stray control characters.

    Quotes are escaped too: substitution runs over the raw XML, so a
    placeholder the user typed inside an attribute value must not let a
    quote in the data break out of it (harmless in text nodes).
    """
    value = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
    return _CONTROL_RE.sub("", value)


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(r"\{\{\s*" + re.escape(name) + r"\s*\}\}")


# ── Phase H.2 — conditional regions + repeating table rows ───────────────

def _cond_open_pattern(cond: str) -> re.Pattern:
    return re.compile(r"\{\{\?\s*" + re.escape(cond) + r"\s*\}\}")


def _cond_close_pattern(cond: str) -> re.Pattern:
    return re.compile(r"\{\{/\s*" + re.escape(cond) + r"\s*\}\}")


def _region_pattern(region: str) -> re.Pattern:
    return re.compile(r"\{\{#\s*" + re.escape(region) + r"\s*\}\}")


def _remove_marker_paragraph(xml: str, marker_pat: re.Pattern) -> str:
    """Strip a conditional marker and, if its paragraph is then empty, drop the
    whole ``<w:p>``.

    The markers sit on their own line bracketing a table (§5.2), so a KEPT
    section must leave **no blank line** where the marker was — removing just
    the marker text would strand an empty paragraph. A paragraph that still
    holds other text keeps it (marker removed); one carrying ``<w:sectPr>``
    (section properties) is never dropped.
    """
    def repl(match: re.Match) -> str:
        para = match.group(0)
        if not marker_pat.search(para):
            return para
        stripped = marker_pat.sub("", para)
        if "<w:sectPr" in stripped:
            return stripped
        if _XML_TAG_RE.sub("", stripped).strip():
            return stripped  # other text remains — keep the paragraph
        return ""  # marker-only paragraph → drop entirely (no blank line)

    return _PARAGRAPH_RE.sub(repl, xml)


def _apply_conditions(xml: str, conditions: dict[str, bool]) -> str:
    """Resolve ``{{?cond}}`` … ``{{/cond}}`` regions (§5).

    Per condition: when ``True`` strip just the two markers (gated content
    stays); when ``False`` delete the whole span from the opening marker's
    ``<w:p>`` through the closing marker's ``</w:p>`` inclusive — markers sit
    in their own paragraphs bracketing the table, so this removes the table
    cleanly without producing partial-table XML Word would reject. A present
    open with no matching close (or vice versa) raises :class:`DocxFillError`.
    """
    for cond, keep in conditions.items():
        open_pat = _cond_open_pattern(cond)
        close_pat = _cond_close_pattern(cond)
        has_open = open_pat.search(xml) is not None
        has_close = close_pat.search(xml) is not None
        if not has_open and not has_close:
            continue  # template does not use this condition
        if has_open != has_close:
            raise DocxFillError(
                f"Région conditionnelle « {cond} » incomplète dans le gabarit "
                "(marqueur d'ouverture ou de fermeture manquant)."
            )
        if keep:
            # Keep the gated content; remove the marker paragraphs entirely
            # so no blank line is left where the markers were.
            xml = _remove_marker_paragraph(xml, open_pat)
            xml = _remove_marker_paragraph(xml, close_pat)
            continue
        # False → remove the whole marker-paragraph → marker-paragraph span.
        # Every body is tempered against the PARAGRAPH boundary (`</?w:p[\s/>]`,
        # so an OPENING <w:p> stops it too) and the middle one against a second
        # opening marker. Two reasons, both verified:
        #   • linearity (CWE-1333): the old leading `(?:(?!</w:p>).)*?` was
        #     tempered only against the CLOSING tag, so it could run across
        #     arbitrarily many `<w:p` opens, and each extension re-triggered the
        #     unbounded middle `.*?`. Measured on `"<w:p>{{?c}}"*1600` (17.6 KB):
        #     43.3 s before, 0.45 ms after — it grew ~8x per doubling.
        #   • correctness: because the leading body could cross a paragraph
        #     open, a self-closing `<w:p w:rsidR="…"/>` (Word's ordinary blank
        #     paragraph) sitting BEFORE the region was swallowed into the span
        #     and deleted with it. That is exactly what _PARAGRAPH_RE's comment
        #     and test_self_closing_empty_paragraph_not_swallowed guard against;
        #     span_re had never received the guard.
        # `[\s\S]` rather than `.` so no re.DOTALL is needed (same invariant as
        # the run-normalization patterns).
        span_re = re.compile(
            r"<w:p(?:\s[^>]*)?>(?:(?!</?w:p[\s/>])[\s\S])*?"
            + open_pat.pattern
            + r"(?:(?!" + open_pat.pattern + r")[\s\S])*?"
            + close_pat.pattern
            + r"(?:(?!</?w:p[\s/>])[\s\S])*?</w:p>"
        )
        new_xml, n = span_re.subn("", xml)
        if n:
            xml = new_xml
        else:
            # Markers not in the expected own-paragraph placement (§5.2).
            # Deleting a partial table would produce invalid XML, so just
            # remove the marker paragraphs (no literal token survives).
            xml = _remove_marker_paragraph(xml, open_pat)
            xml = _remove_marker_paragraph(xml, close_pat)
    return xml


def _apply_rows(xml: str, rows_by_region: dict[str, list[dict]]) -> str:
    """Clone a marked ``<w:tr>`` once per row dict, substituting row fields (§4).

    The ``{{#region}}`` marker (in the row's first cell) selects the row; it
    is removed in every clone. Row-scoped fields (``{{h.date}}``, ``{{d.cout}}``)
    resolve from the row dict, XML-escaped via a function replacement (never a
    bare string — same rule as the scalar path). An empty row list removes the
    marked row entirely. Scans ALL rows (a template may hold several regions).
    """
    for region, rows in rows_by_region.items():
        region_pat = _region_pattern(region)

        def _expand(match: re.Match, rows=rows, region_pat=region_pat) -> str:
            row_xml = match.group(0)
            if not region_pat.search(row_xml):
                return row_xml
            clones: list[str] = []
            for row in rows:
                clone = region_pat.sub("", row_xml)
                for fname, fval in row.items():
                    escaped = _escape_xml(
                        _normalize_newlines("" if fval is None else str(fval)).replace(
                            "\n", " "
                        )
                    )
                    clone = _name_pattern(fname).sub(lambda m, e=escaped: e, clone)
                clones.append(clone)
            return "".join(clones)

        xml = _TABLE_ROW_RE.sub(_expand, xml)
    return xml


def _ensure_table_separation(xml: str) -> str:
    """Insert a minimal empty paragraph between any two directly-adjacent
    ``<w:tbl>`` (which a conditional may have produced), so Word keeps them as
    separate tables instead of merging them — with a ~1pt height so no visible
    gap appears. To avoid even that hair-line, put the section heading INSIDE
    the ``{{?cond}}`` so a real paragraph separates the tables."""
    return _ADJACENT_TABLES_RE.sub("</w:tbl>" + _TABLE_SEPARATOR, xml)


# ── Public API ──────────────────────────────────────────────────────────

def extract_placeholders(docx_bytes: bytes) -> list[str]:
    """Distinct ``{{...}}`` names in document order.

    Scanned across ``word/document.xml``, ``word/header*.xml`` and
    ``word/footer*.xml`` on TAG-STRIPPED text, so placeholders fragmented
    across ``<w:r>`` runs still appear in the inventory (they surface as
    split-run suspects in :func:`validate_template`).
    """
    errors, zf = _structural_errors(docx_bytes)
    if errors or zf is None:
        raise DocxFillError(errors[0] if errors else "Archive invalide.")
    names: list[str] = []
    with zf:
        for target in _target_names(zf):
            xml = _normalize_runs(
                _read_entry_bounded(zf, target, MAX_SINGLE_XML_BYTES).decode(
                    "utf-8", errors="replace"
                )
            )
            for name in _names_in_text(_XML_TAG_RE.sub("", xml)):
                if name not in names:
                    names.append(name)
    return names


def validate_template(docx_bytes: bytes) -> TemplateValidation:
    """Validate a template archive and inventory its placeholders.

    ``split_run_suspects`` lists names visible in the tag-stripped text
    but NOT matchable in the raw XML — Word fragmented them across runs;
    the user must retype the field in Word in one stroke, without pause
    or autocorrect, then re-upload.
    """
    result = TemplateValidation()
    errors, zf = _structural_errors(docx_bytes)
    if errors or zf is None:
        result.errors = errors
        return result

    # The B − A set difference is computed PER TARGET (spec §7.4): a name
    # typed cleanly in the body but fragmented in a header must still be
    # flagged — a global difference would let the clean occurrence mask
    # the fragmented one.
    suspects: list[str] = []
    with zf:
        for target in _target_names(zf):
            try:
                xml_bytes = _read_entry_bounded(zf, target, MAX_SINGLE_XML_BYTES)
            except DocxFillError as exc:
                result.errors.append(str(exc))
                return result
            xml = _normalize_runs(xml_bytes.decode("utf-8", errors="replace"))
            stripped = _XML_TAG_RE.sub("", xml)
            # Inventory: fillable {{name}} placeholders only (markers are
            # structural, not fields).
            for name in _names_in_text(stripped):
                if name not in result.placeholders:
                    result.placeholders.append(name)
            # Split-run suspects over ALL tokens — names AND {{#…}}/{{?…}}/
            # {{/…}} markers (§3.4) — and per-OCCURRENCE (not per-name): flag
            # when some occurrences remain fragmented in the raw XML even
            # though others are clean, so one clean copy can't mask a broken
            # sibling (which would silently fail to fill).
            raw_counts = _all_token_counts(xml)
            strip_counts = _all_token_counts(stripped)
            for token in _all_tokens_in_text(stripped):
                if raw_counts.get(token, 0) < strip_counts.get(token, 0) and (
                    token not in suspects
                ):
                    suspects.append(token)

    result.split_run_suspects = suspects
    return result


def _fill_target_xml(
    xml: str,
    values: dict[str, str],
    *,
    rows_by_region: dict[str, list[dict]] | None = None,
    conditions: dict[str, bool] | None = None,
) -> str:
    """Fill one target XML.

    Order (§4.3): normalize runs → conditional regions → repeating rows →
    block paragraphs → scalars. Conditionals first so a removed table never
    reaches row expansion; scalars last so globals in surviving structure
    resolve. ``rows_by_region``/``conditions`` are passed only for
    ``word/document.xml`` (tables live in the body); headers/footers get the
    Phase H block + scalar passes only.
    """
    # Heal Word's run-splitting first, so EVERY occurrence of a repeated
    # placeholder (and every structural marker) matches — not just the clean
    # ones (a fragmented copy would otherwise ship as a literal {{name}}
    # while its clean sibling filled).
    xml = _normalize_runs(xml)
    if conditions:
        xml = _apply_conditions(xml, conditions)
    if rows_by_region:
        xml = _apply_rows(xml, rows_by_region)
    if conditions or rows_by_region:
        # A removed conditional can leave two tables adjacent — keep them
        # distinct (and gap-free) before the block/scalar passes.
        xml = _ensure_table_separation(xml)
    block_pairs: list[tuple[str, str]] = []
    scalar_pairs: list[tuple[str, str]] = []
    for name, raw_value in values.items():
        value = _normalize_newlines("" if raw_value is None else str(raw_value))
        if _BLANK_LINE_RE.search(value):
            block_pairs.append((name, value))
        else:
            scalar_pairs.append((name, value))

    # a. Block expansion: clone the host paragraph once per blank-line-
    #    separated chunk, so numbered-list formatting yields sequential
    #    numbered paragraphs. Scan ALL paragraphs (regression guard: a
    #    previous implementation passed count=1 and silently skipped any
    #    placeholder not in the first paragraph).
    for name, value in block_pairs:
        chunks = [c.strip() for c in _BLANK_LINE_RE.split(value)]
        chunks = [c for c in chunks if c] or [""]
        name_re = _name_pattern(name)

        def _expand(match: re.Match) -> str:
            paragraph = match.group(0)
            if not name_re.search(paragraph):
                return paragraph
            clones = []
            for chunk in chunks:
                escaped = _escape_xml(chunk.replace("\n", " "))
                # Function replacement — a bare string would interpret
                # backslashes / \g sequences in user content.
                clones.append(name_re.sub(lambda m: escaped, paragraph))
            return "".join(clones)

        xml = _PARAGRAPH_RE.sub(_expand, xml)

        # Fallback: a block placeholder sitting outside every matchable
        # paragraph (e.g. in a host paragraph that embeds a text box) must
        # never ship as a literal {{name}} — substitute the chunks inline,
        # space-joined (paragraph separation is lost, content is not).
        if name_re.search(xml):
            inline = _escape_xml(" ".join(c.replace("\n", " ") for c in chunks))
            xml = name_re.sub(lambda m: inline, xml)

    # b. Scalar substitution: single \n inside any value becomes a space.
    for name, value in scalar_pairs:
        escaped = _escape_xml(value.replace("\n", " "))
        xml = _name_pattern(name).sub(lambda m: escaped, xml)

    return xml


def fill_docx(
    docx_bytes: bytes,
    values: dict[str, str],
    *,
    rows_by_region: dict[str, list[dict]] | None = None,
    conditions: dict[str, bool] | None = None,
) -> bytes:
    """Fill placeholders in a .docx template; return the new archive.

    Only ``word/document.xml`` and ``word/header*.xml``/``word/footer*.xml``
    are rewritten; every other entry is copied through byte-identical (the
    whole point of this engine — Word must reopen the output without
    repair). Raises :class:`DocxFillError` on a structurally invalid or
    oversized archive.

    ``rows_by_region`` (repeating table rows, §4) and ``conditions``
    (conditional sections, §5) are the Phase H.2 extensions; they apply to
    ``word/document.xml`` only. When both are ``None`` the behavior is
    identical to Phase H (existing callers are untouched).
    """
    errors, zf = _structural_errors(docx_bytes)
    if errors or zf is None:
        raise DocxFillError(errors[0] if errors else "Archive invalide.")

    output = io.BytesIO()
    remaining = MAX_TOTAL_DECOMPRESSED_BYTES
    with zf, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zf.infolist():
            is_target = bool(_TARGET_RE.match(info.filename))
            cap = min(MAX_SINGLE_XML_BYTES, remaining) if is_target else remaining
            data = _read_entry_bounded(zf, info.filename, cap)
            remaining -= len(data)
            if is_target:
                is_document = info.filename == "word/document.xml"
                xml = data.decode("utf-8", errors="replace")
                data = _fill_target_xml(
                    xml,
                    values,
                    rows_by_region=rows_by_region if is_document else None,
                    conditions=conditions if is_document else None,
                ).encode("utf-8")
            # Reuse the original ZipInfo: preserves entry order, per-entry
            # compress_type, timestamps and attributes.
            zout.writestr(info, data)
    return output.getvalue()
