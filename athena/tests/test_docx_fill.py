"""Tests for utils/docx_fill.py — synthetic in-memory .docx fixtures only."""

import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.docx_fill import (
    DocxFillError,
    extract_placeholders,
    fill_docx,
    validate_template,
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _para(text: str, ppr: str = "") -> str:
    ppr_xml = f"<w:pPr>{ppr}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr_xml}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _doc(*paragraphs: str) -> str:
    return (
        f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
        + "".join(paragraphs)
        + "</w:body></w:document>"
    )


def _hdr(*paragraphs: str) -> str:
    return (
        f'<?xml version="1.0"?><w:hdr {_W_NS}>' + "".join(paragraphs) + "</w:hdr>"
    )


def _make_docx(
    document_xml: str,
    headers: list[str] | None = None,
    footers: list[str] | None = None,
    extra: dict[str, bytes] | None = None,
    include_document: bool = True,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        if include_document:
            zf.writestr("word/document.xml", document_xml)
        for i, xml in enumerate(headers or [], start=1):
            zf.writestr(f"word/header{i}.xml", xml)
        for i, xml in enumerate(footers or [], start=1):
            zf.writestr(f"word/footer{i}.xml", xml)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _document_xml(docx: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(docx)) as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ── Scalar substitution ─────────────────────────────────────────────────

def test_scalar_substitution_accented_and_spaced():
    docx = _make_docx(_doc(
        _para("Réf : {{référence_interne}}"),
        _para("Ville : {{ ville_lettre }}"),
        _para("Civ : {{civilité}}"),
    ))
    out = _document_xml(fill_docx(docx, {
        "référence_interne": "2026-042",
        "ville_lettre": "Montréal",
        "civilité": "Maître",
    }))
    assert "Réf : 2026-042" in out
    assert "Ville : Montréal" in out
    assert "Civ : Maître" in out
    assert "{{" not in out


def test_unmapped_placeholder_left_untouched():
    docx = _make_docx(_doc(_para("{{connu}} et {{inconnu}}")))
    out = _document_xml(fill_docx(docx, {"connu": "X"}))
    assert "X et {{inconnu}}" in out


def test_single_newline_in_scalar_becomes_space():
    docx = _make_docx(_doc(_para("{{objet_lettre}}")))
    out = _document_xml(fill_docx(docx, {"objet_lettre": "ligne1\nligne2"}))
    assert "ligne1 ligne2" in out


def test_xml_escaping_control_chars_and_backslash_g():
    docx = _make_docx(_doc(_para("V: {{v}}")))
    out = _document_xml(fill_docx(docx, {"v": "A & B <w:evil> \\g<0> fin\x01\x02\tok"}))
    assert "A &amp; B &lt;w:evil&gt;" in out
    # Function replacement: backslash sequences survive literally.
    assert "\\g&lt;0&gt;" in out
    # C0 controls stripped except tab.
    assert "\x01" not in out and "\x02" not in out
    assert "fin\tok" in out
    assert "<w:evil>" not in out


# ── Block expansion ─────────────────────────────────────────────────────

_NUM_PPR = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr>'


def test_block_three_chunks_clone_paragraph_preserving_numbering():
    docx = _make_docx(_doc(_para("{{FAITS}}", ppr=_NUM_PPR)))
    value = "Premier fait.\n\nDeuxième fait.\n\nTroisième fait."
    out = _document_xml(fill_docx(docx, {"FAITS": value}))
    assert out.count('<w:numId w:val="7"/>') == 3
    assert out.index("Premier fait.") < out.index("Deuxième fait.") < out.index(
        "Troisième fait."
    )
    assert "{{FAITS}}" not in out


def test_block_in_second_paragraph_still_expands_no_count_1_regression():
    docx = _make_docx(_doc(
        _para("Paragraphe d'introduction sans champ."),
        _para("{{CONCLUSIONS}}", ppr=_NUM_PPR),
    ))
    out = _document_xml(fill_docx(docx, {"CONCLUSIONS": "Un.\n\nDeux."}))
    assert out.count('<w:numId w:val="7"/>') == 2
    assert "Un." in out and "Deux." in out
    assert "{{CONCLUSIONS}}" not in out


def test_block_crlf_and_whitespace_only_blank_lines():
    # Textareas submit \r\n; blank lines may carry stray spaces.
    docx = _make_docx(_doc(_para("{{CONTENU_LETTRE}}")))
    out = _document_xml(
        fill_docx(docx, {"CONTENU_LETTRE": "a\r\n \r\nb\r\n\r\n\r\nc"})
    )
    assert "a" in out and "b" in out and "c" in out
    # Three chunks → three paragraphs.
    assert out.count("<w:p>") == 3


def test_block_value_without_blank_line_fills_inline():
    docx = _make_docx(_doc(_para("{{FAITS}}", ppr=_NUM_PPR)))
    out = _document_xml(fill_docx(docx, {"FAITS": "Un seul paragraphe."}))
    assert out.count('<w:numId w:val="7"/>') == 1
    assert "Un seul paragraphe." in out


def test_self_closing_empty_paragraph_not_swallowed():
    # Word serializes blank paragraphs as self-closing <w:p .../>; the
    # paragraph regex must not treat one as an opening tag and clone it
    # together with the following numbered host (review regression).
    empty = '<w:p w:rsidR="00A" w:rsidRDefault="00A"/>'
    docx = _make_docx(_doc(empty, _para("{{FAITS}}", ppr=_NUM_PPR)))
    out = _document_xml(fill_docx(docx, {"FAITS": "Un.\n\nDeux.\n\nTrois."}))
    assert out.count(empty) == 1  # the blank paragraph is not cloned
    assert out.count('<w:numId w:val="7"/>') == 3
    assert out.index("Un.") < out.index("Deux.") < out.index("Trois.")


def test_textbox_inner_block_placeholder_stays_well_formed():
    # <w:p> nests inside <w:txbxContent> (text boxes — common in
    # letterheads). Cloning must land on the INNERMOST paragraph so the
    # output stays balanced XML (Word refuses unbalanced documents).
    import xml.etree.ElementTree as ET

    outer = (
        "<w:p><w:r><w:drawing><w:txbxContent>"
        "<w:p><w:r><w:t>{{CONTENU_LETTRE}}</w:t></w:r></w:p>"
        "</w:txbxContent></w:drawing></w:r></w:p>"
    )
    docx = _make_docx(_doc(outer))
    out = _document_xml(fill_docx(docx, {"CONTENU_LETTRE": "Alpha.\n\nBravo."}))
    ET.fromstring(out)  # raises on mismatched tags
    assert "Alpha." in out and "Bravo." in out
    assert "{{CONTENU_LETTRE}}" not in out


def test_block_outside_matchable_paragraph_falls_back_inline():
    # A block placeholder in a host paragraph that also embeds a text box
    # cannot be paragraph-cloned safely — it must still be substituted
    # (inline, space-joined), never shipped as a literal {{name}}.
    import xml.etree.ElementTree as ET

    outer = (
        "<w:p><w:r><w:drawing><w:txbxContent>"
        "<w:p><w:r><w:t>logo</w:t></w:r></w:p>"
        "</w:txbxContent></w:drawing></w:r>"
        "<w:r><w:t>{{FAITS}}</w:t></w:r></w:p>"
    )
    docx = _make_docx(_doc(outer))
    out = _document_xml(fill_docx(docx, {"FAITS": "Un.\n\nDeux."}))
    ET.fromstring(out)
    assert "{{FAITS}}" not in out
    assert "Un. Deux." in out


def test_quotes_escaped_in_values():
    docx = _make_docx(_doc(_para("V: {{v}}")))
    out = _document_xml(fill_docx(docx, {"v": 'dit "bonjour" à l\'avocat'}))
    assert "&quot;bonjour&quot;" in out
    assert "&#39;avocat" in out
    assert '"bonjour"' not in out


# ── Run normalization: repeated + Word-fragmented placeholders ───────────

def test_repeated_field_all_occurrences_filled():
    docx = _make_docx(_doc(
        _para("Devant le {{tribunal}}"),
        _para("au {{tribunal}}"),
        _para("le {{tribunal}} siégeant"),
    ))
    out = _document_xml(fill_docx(docx, {"tribunal": "Cour supérieure"}))
    assert out.count("Cour supérieure") == 3
    assert "{{tribunal}}" not in out


def test_partial_split_repeat_last_clean_still_fills_all():
    # Two occurrences fragmented by Word, one clean — the reported bug was
    # that only the clean (last) one filled. Normalization heals the rest.
    split = ("<w:p><w:r><w:t>{{</w:t></w:r>"
             "<w:r><w:t>tribunal}}</w:t></w:r></w:p>")
    docx = _make_docx(_doc(split, split, _para("{{tribunal}}")))
    out = _document_xml(fill_docx(docx, {"tribunal": "CS"}))
    assert out.count("CS") == 3
    assert "{{" not in out


def test_proof_err_split_is_healed_and_filled():
    # Word spell-check brackets the name in <w:proofErr>, splitting the run
    # around the braces.
    body = ('<w:p><w:r><w:t>{{</w:t></w:r>'
            '<w:proofErr w:type="spellStart"/><w:r><w:t>tribunal}}</w:t></w:r>'
            '<w:proofErr w:type="spellEnd"/></w:p>')
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []  # healed → no warning
    out = _document_xml(fill_docx(docx, {"tribunal": "CS"}))
    assert "CS" in out and "{{" not in out
    assert "proofErr" not in out


def test_normalization_only_merges_identical_formatting():
    # Different rPr between the braces and the name → cannot merge safely →
    # stays fragmented and is correctly reported (not silently mis-filled).
    body = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{</w:t></w:r>'
            '<w:r><w:t>tribunal}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == ["tribunal"]


def test_normalization_preserves_non_text_runs():
    # A run holding <w:br/> (not a plain <w:t>) must never be merged away.
    body = ('<w:p><w:r><w:t>{{</w:t></w:r><w:r><w:br/></w:r>'
            '<w:r><w:t>tribunal}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    out = _document_xml(fill_docx(docx, {"tribunal": "CS"}))
    assert "<w:br/>" in out  # the break survives


def test_per_occurrence_split_detection_not_masked_by_clean_copy():
    clean = _para("{{tribunal}} en clair")
    # A split copy whose halves carry DIFFERENT formatting can't be healed.
    split = ('<w:p><w:r><w:rPr><w:i/></w:rPr><w:t>{{</w:t></w:r>'
             '<w:r><w:t>tribunal}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(clean, split))
    # Name-level detection would clear it (one clean copy exists); the
    # per-occurrence check must still flag it.
    assert validate_template(docx).split_run_suspects == ["tribunal"]


# ── Headers / footers ───────────────────────────────────────────────────

def test_header_and_footer_placeholders_filled():
    docx = _make_docx(
        _doc(_para("corps {{x}}")),
        headers=[_hdr(_para("En-tête : {{cabinet.nom}}"))],
        footers=[_hdr(_para("Pied : {{date.aujourdhui_iso}}"))],
    )
    filled = fill_docx(docx, {
        "x": "B",
        "cabinet.nom": "Me Jason Poirier Lavoie",
        "date.aujourdhui_iso": "2026-07-07",
    })
    with zipfile.ZipFile(io.BytesIO(filled)) as zf:
        assert "Me Jason Poirier Lavoie" in zf.read("word/header1.xml").decode()
        assert "2026-07-07" in zf.read("word/footer1.xml").decode()


# ── Extraction & split-run detection ────────────────────────────────────

def test_extract_placeholders_document_order_distinct():
    docx = _make_docx(
        _doc(_para("{{beta}} puis {{alpha}} puis {{beta}}")),
        headers=[_hdr(_para("{{gamma}}"))],
    )
    assert extract_placeholders(docx) == ["beta", "alpha", "gamma"]


def test_split_run_detection_flags_fragmented_and_passes_clean():
    fragmented = (
        "<w:p><w:r><w:t>{{dis</w:t></w:r>"
        '<w:r w:rsidR="0"><w:t>trict}}</w:t></w:r></w:p>'
    )
    clean = _para("{{tribunal}}")
    docx = _make_docx(_doc(fragmented, clean))
    result = validate_template(docx)
    assert result.errors == []
    assert set(result.placeholders) == {"district", "tribunal"}
    assert result.split_run_suspects == ["district"]


def test_split_run_detected_per_target_not_globally():
    # A name typed cleanly in the body must not mask an UNHEALABLE
    # fragmented copy in a header (halves with different formatting can't
    # be merged — so they stay a genuine suspect).
    fragmented_header = _hdr(
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{dis</w:t></w:r>'
        '<w:r><w:t>trict}}</w:t></w:r></w:p>'
    )
    docx = _make_docx(_doc(_para("{{district}}")), headers=[fragmented_header])
    result = validate_template(docx)
    assert result.errors == []
    assert result.split_run_suspects == ["district"]


def test_validate_clean_template_no_suspects():
    docx = _make_docx(_doc(_para("{{a}}"), _para("{{B_LOC}}")))
    result = validate_template(docx)
    assert result.errors == []
    assert result.split_run_suspects == []
    assert result.placeholders == ["a", "B_LOC"]


# ── Zip safety ──────────────────────────────────────────────────────────

def test_not_a_zip_rejected():
    result = validate_template(b"ceci n'est pas un docx")
    assert result.errors
    with pytest.raises(DocxFillError):
        fill_docx(b"ceci n'est pas un docx", {})


def test_missing_document_xml_rejected():
    docx = _make_docx("", include_document=False)
    result = validate_template(docx)
    assert any("word/document.xml" in e for e in result.errors)


def test_dotdot_entry_name_rejected():
    docx = _make_docx(_doc(_para("ok")), extra={"word/../../evil.txt": b"x"})
    result = validate_template(docx)
    assert any("interdit" in e for e in result.errors)


def test_oversized_compressed_template_rejected(monkeypatch):
    monkeypatch.setattr("utils.docx_fill.MAX_COMPRESSED_BYTES", 64)
    docx = _make_docx(_doc(_para("beaucoup de texte " * 50)))
    result = validate_template(docx)
    assert any("10 Mo" in e for e in result.errors)


def test_oversized_xml_target_rejected(monkeypatch):
    monkeypatch.setattr("utils.docx_fill.MAX_SINGLE_XML_BYTES", 32)
    docx = _make_docx(_doc(_para("un contenu qui dépasse trente-deux octets")))
    result = validate_template(docx)
    assert any("volumineuse" in e for e in result.errors)
    with pytest.raises(DocxFillError):
        fill_docx(docx, {})


def test_total_decompressed_cap(monkeypatch):
    monkeypatch.setattr("utils.docx_fill.MAX_TOTAL_DECOMPRESSED_BYTES", 128)
    docx = _make_docx(_doc(_para("x")), extra={"word/media/big.bin": b"0" * 200})
    result = validate_template(docx)
    assert any("décompressé" in e for e in result.errors)


def test_entry_count_cap(monkeypatch):
    monkeypatch.setattr("utils.docx_fill.MAX_ENTRY_COUNT", 3)
    docx = _make_docx(_doc(_para("x")), extra={"a.txt": b"1", "b.txt": b"2"})
    result = validate_template(docx)
    assert any("entrées" in e for e in result.errors)


# ── Pass-through integrity ──────────────────────────────────────────────

def test_non_target_entries_byte_identical_after_fill():
    png = bytes(range(256)) * 4  # binary-ish payload
    styles = b'<?xml version="1.0"?><w:styles/>'
    docx = _make_docx(
        _doc(_para("{{x}}")),
        extra={"word/media/image1.png": png, "word/styles.xml": styles},
    )
    filled = fill_docx(docx, {"x": "rempli"})
    with zipfile.ZipFile(io.BytesIO(filled)) as zf:
        assert zf.read("word/media/image1.png") == png
        assert zf.read("word/styles.xml") == styles
        assert zf.read("[Content_Types].xml") == _CONTENT_TYPES.encode()
        # Entry order preserved.
        original_order = zipfile.ZipFile(io.BytesIO(docx)).namelist()
        assert zf.namelist() == original_order


def test_filled_document_is_valid_zip_with_deflate():
    docx = _make_docx(_doc(_para("{{x}}")))
    filled = fill_docx(docx, {"x": "ok"})
    assert filled.startswith(b"PK\x03\x04")
    with zipfile.ZipFile(io.BytesIO(filled)) as zf:
        assert zf.testzip() is None
