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

_XML_TAG_RE = re.compile(r"<[^>]+>")

# Fill targets inside the archive: main document + all headers/footers.
_TARGET_RE = re.compile(r"^word/(document|header\d*|footer\d*)\.xml$")

# C0 control characters except tab/newline/CR (handled separately).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Blank-line separator between block chunks (whitespace-only lines count —
# textarea input frequently carries stray spaces on empty lines).
_BLANK_LINE_RE = re.compile(r"\n\s*\n")

# ── Run normalization (heal Word's run-splitting so placeholders match) ──
# Word fragments a typed placeholder across multiple <w:r> runs — proofing
# (spell/grammar) brackets it in <w:proofErr> markers that force run
# boundaries, tracked changes wrap edits in <w:ins>, and mid-word format
# changes split runs. A fragmented {{champ}} then can't be matched. We
# heal it BEFORE matching with a conservative, byte-level pass (no
# python-docx round-trip): strip the empty proofing markers, then merge
# ADJACENT runs that carry identical formatting and whose only content is
# one <w:t>. Runs holding anything else (<w:br/>, <w:tab/>, <w:drawing>,
# field codes…) never match the pattern, so they are left untouched; a
# bookmark or comment marker between two runs also blocks the merge (they
# are no longer adjacent). Merging identical-format adjacent text runs is
# exactly what Word itself does on save, so the output still opens without
# repair.
_PROOF_ERR_RE = re.compile(r"<w:proofErr\b[^>]*/>|<w:proofErr\b[^>]*>.*?</w:proofErr>",
                           re.DOTALL)
# t1/t2 are `[^<]*` — a <w:t> text node never contains a raw '<' (it is
# escaped as &lt;). This is load-bearing: `.*?` with DOTALL could swallow
# markup and match ACROSS run/paragraph boundaries, wrongly coalescing
# unrelated runs. The rPr body stays `.*?` (bounded by the first
# </w:rPr>; rPr never nests another rPr).
_ADJACENT_TEXT_RUNS_RE = re.compile(
    r"<w:r>(?P<rpr1>(?:<w:rPr>.*?</w:rPr>)?)<w:t(?:\s[^>]*)?>(?P<t1>[^<]*)</w:t></w:r>"
    r"<w:r>(?P<rpr2>(?:<w:rPr>.*?</w:rPr>)?)<w:t(?:\s[^>]*)?>(?P<t2>[^<]*)</w:t></w:r>",
    re.DOTALL,
)


def _merge_adjacent_runs(match: re.Match) -> str:
    # Only merge when the two runs share identical run-properties; else
    # leave them exactly as they were (formatting must be preserved).
    if match.group("rpr1") != match.group("rpr2"):
        return match.group(0)
    text = match.group("t1") + match.group("t2")
    # xml:space="preserve" so no boundary whitespace is lost on merge.
    return (
        f'<w:r>{match.group("rpr1")}'
        f'<w:t xml:space="preserve">{text}</w:t></w:r>'
    )


def _normalize_runs(xml: str) -> str:
    """Strip proofing markers and coalesce same-format adjacent text runs."""
    xml = _PROOF_ERR_RE.sub("", xml)
    # Repeat until stable: each sub pass merges at most one boundary per
    # run pair (the merged run sits behind the scan cursor), so a run split
    # into N pieces needs up to N-1 passes.
    while True:
        merged = _ADJACENT_TEXT_RUNS_RE.sub(_merge_adjacent_runs, xml)
        if merged == xml:
            return merged
        xml = merged

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


def _name_counts(text: str) -> dict[str, int]:
    """Count every placeholder occurrence (not distinct) by name."""
    counts: dict[str, int] = {}
    for match in PLACEHOLDER_RE.finditer(text):
        name = match.group(1)
        counts[name] = counts.get(name, 0) + 1
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
            raw_counts = _name_counts(xml)
            strip_counts = _name_counts(stripped)
            for name in _names_in_text(stripped):
                if name not in result.placeholders:
                    result.placeholders.append(name)
                # Per-OCCURRENCE (not per-name): flag when some occurrences
                # remain fragmented in the raw XML even though others are
                # clean — a name-level check would let one clean copy mask
                # the broken ones (they'd silently fail to fill).
                if raw_counts.get(name, 0) < strip_counts.get(name, 0) and (
                    name not in suspects
                ):
                    suspects.append(name)

    result.split_run_suspects = suspects
    return result


def _fill_target_xml(xml: str, values: dict[str, str]) -> str:
    """Fill one target XML: normalize runs, then block + scalar substitution."""
    # Heal Word's run-splitting first, so EVERY occurrence of a repeated
    # placeholder matches — not just the clean ones (a fragmented copy
    # would otherwise ship as a literal {{name}} while its clean sibling
    # filled).
    xml = _normalize_runs(xml)
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


def fill_docx(docx_bytes: bytes, values: dict[str, str]) -> bytes:
    """Fill placeholders in a .docx template; return the new archive.

    Only ``word/document.xml`` and ``word/header*.xml``/``word/footer*.xml``
    are rewritten; every other entry is copied through byte-identical (the
    whole point of this engine — Word must reopen the output without
    repair). Raises :class:`DocxFillError` on a structurally invalid or
    oversized archive.
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
                xml = data.decode("utf-8", errors="replace")
                data = _fill_target_xml(xml, values).encode("utf-8")
            # Reuse the original ZipInfo: preserves entry order, per-entry
            # compress_type, timestamps and attributes.
            zout.writestr(info, data)
    return output.getvalue()
