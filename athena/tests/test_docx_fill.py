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


def _tc(text: str, tcpr: str = "") -> str:
    """A table cell holding one paragraph (optional cell properties)."""
    tcpr_xml = f"<w:tcPr>{tcpr}</w:tcPr>" if tcpr else ""
    return f"<w:tc>{tcpr_xml}<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _tr(*cells: str) -> str:
    return f"<w:tr>{''.join(cells)}</w:tr>"


def _tbl(*rows: str) -> str:
    return f"<w:tbl><w:tblPr/>{''.join(rows)}</w:tbl>"


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


def test_format_split_placeholder_heals_and_fills():
    # Different rPr between the braces and the name (Word's proofing/format
    # split). Now BRIDGED — the placeholder heals and fills, instead of
    # shipping a literal {{...}} that retyping never fixes.
    body = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{</w:t></w:r>'
            '<w:r><w:t>tribunal}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []
    out = _document_xml(fill_docx(docx, {"tribunal": "CS"}))
    assert "CS" in out and "{{" not in out


def test_dotted_name_language_split_heals_and_fills():
    # The reported bug: {{dossier.defendeur}} split AT THE DOT with different
    # <w:lang> runs (fr-CA vs en-US) — Word re-splits at the dot every save,
    # so retyping never cleared the "fragmenté" warning.
    body = (
        "<w:p>"
        '<w:r><w:rPr><w:lang w:val="fr-CA"/></w:rPr><w:t>{{dossier.</w:t></w:r>'
        '<w:r><w:rPr><w:lang w:val="en-US"/></w:rPr><w:t>defendeur}}</w:t></w:r>'
        "</w:p>"
    )
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []
    out = _document_xml(fill_docx(docx, {"dossier.defendeur": "Marc Lavoie"}))
    assert "Marc Lavoie" in out
    assert "{{dossier." not in out and "defendeur}}" not in out


def test_three_way_format_split_heals():
    # {{ | dossier. | defendeur}} — three runs, each a different rPr.
    body = (
        "<w:p>"
        "<w:r><w:rPr><w:b/></w:rPr><w:t>{{</w:t></w:r>"
        "<w:r><w:rPr><w:i/></w:rPr><w:t>dossier.</w:t></w:r>"
        "<w:r><w:t>defendeur}}</w:t></w:r>"
        "</w:p>"
    )
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []
    out = _document_xml(fill_docx(docx, {"dossier.defendeur": "X"}))
    assert "X" in out and "{{" not in out


def test_split_between_opening_braces_heals():
    # Word split BETWEEN the two opening braces: "{" | "{district}}".
    body = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{</w:t></w:r>'
            '<w:r><w:t>{district}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []
    out = _document_xml(fill_docx(docx, {"district": "Montréal"}))
    assert "Montréal" in out


def test_run_attribute_split_heals():
    # Runs carrying revision attributes (<w:r w:rsidR="…">) with different
    # rPr must still bridge (the attributes are dropped on merge).
    body = (
        "<w:p>"
        '<w:r w:rsidR="00AB12"><w:rPr><w:lang w:val="fr-CA"/></w:rPr><w:t>{{dossier.</w:t></w:r>'
        '<w:r w:rsidR="00CD34"><w:rPr><w:lang w:val="en-US"/></w:rPr><w:t>defendeur}}</w:t></w:r>'
        "</w:p>"
    )
    docx = _make_docx(_doc(body))
    assert validate_template(docx).split_run_suspects == []
    out = _document_xml(fill_docx(docx, {"dossier.defendeur": "Marc Lavoie"}))
    assert "Marc Lavoie" in out


def test_bridge_does_not_merge_unrelated_formatted_runs():
    # Two differently-formatted runs with NO placeholder between them must
    # stay distinct — the bridge only fires to complete a {{...}}.
    body = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Gras</w:t></w:r>'
            '<w:r><w:t> et normal</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    out = _document_xml(fill_docx(docx, {}))
    # The bold run keeps its own formatting (not flattened into the next).
    assert "<w:rPr><w:b/></w:rPr><w:t>Gras</w:t>" in out


def test_normalization_preserves_non_text_runs():
    # A run holding <w:br/> (not a plain <w:t>) must never be merged away.
    body = ('<w:p><w:r><w:t>{{</w:t></w:r><w:r><w:br/></w:r>'
            '<w:r><w:t>tribunal}}</w:t></w:r></w:p>')
    docx = _make_docx(_doc(body))
    out = _document_xml(fill_docx(docx, {"tribunal": "CS"}))
    assert "<w:br/>" in out  # the break survives


def test_per_occurrence_split_detection_not_masked_by_clean_copy():
    clean = _para("{{tribunal}} en clair")
    # A STRUCTURAL split (a <w:br/> between the halves) can't be bridged —
    # only formatting splits heal. The clean copy must not mask it.
    split = ('<w:p><w:r><w:t>{{</w:t></w:r><w:r><w:br/></w:r>'
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
    # Structural split — a <w:br/> inside the braces can't be bridged.
    fragmented = (
        "<w:p><w:r><w:t>{{dis</w:t></w:r><w:r><w:br/></w:r>"
        "<w:r><w:t>trict}}</w:t></w:r></w:p>"
    )
    clean = _para("{{tribunal}}")
    docx = _make_docx(_doc(fragmented, clean))
    result = validate_template(docx)
    assert result.errors == []
    assert set(result.placeholders) == {"district", "tribunal"}
    assert result.split_run_suspects == ["district"]


def test_split_run_detected_per_target_not_globally():
    # A name typed cleanly in the body must not mask an UNHEALABLE
    # fragmented copy in a header (a <w:br/> between the halves can't be
    # bridged — so it stays a genuine suspect).
    fragmented_header = _hdr(
        '<w:p><w:r><w:t>{{dis</w:t></w:r><w:r><w:br/></w:r>'
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


# ── Phase H.2: repeating table rows ─────────────────────────────────────

_CELL_BORDER = '<w:tcBorders><w:top w:val="single"/></w:tcBorders>'


def test_repeating_rows_clone_tr_preserving_cell_formatting():
    data_row = _tr(
        _tc("{{#ligne_honoraire}}{{h.date}}", tcpr=_CELL_BORDER),
        _tc("{{h.description}}"),
        _tc("{{h.temps}}"),
    )
    header = _tr(_tc("Date"), _tc("Description"), _tc("Temps"))
    docx = _make_docx(_doc(_tbl(header, data_row)))
    rows = {"ligne_honoraire": [
        {"h.date": "1er mai 2026", "h.description": "Recherche", "h.temps": "2,50"},
        {"h.date": "2 mai 2026", "h.description": "Rédaction", "h.temps": "1,00"},
        {"h.date": "3 mai 2026", "h.description": "Appel", "h.temps": "0,50"},
    ]}
    out = _document_xml(fill_docx(docx, {}, rows_by_region=rows))
    import xml.etree.ElementTree as ET
    ET.fromstring(out)
    assert out.count("<w:tr>") == 4  # header + 3 data rows
    assert "1er mai 2026" in out and "Rédaction" in out and "0,50" in out
    assert "{{#ligne_honoraire}}" not in out and "{{h.date}}" not in out
    # Cell formatting (borders) preserved in each clone (once per data row).
    assert out.count("<w:tcBorders>") == 3
    assert out.index("Recherche") < out.index("Rédaction") < out.index("Appel")


def test_two_regions_both_expand_no_count_1():
    r1 = _tr(_tc("{{#ligne_honoraire}}{{h.description}}"))
    r2 = _tr(_tc("{{#ligne_debours_tx}}{{d.description}}"))
    docx = _make_docx(_doc(_tbl(r1), _tbl(r2)))
    rows = {
        "ligne_honoraire": [{"h.description": "A"}, {"h.description": "B"}],
        "ligne_debours_tx": [
            {"d.description": "X"}, {"d.description": "Y"}, {"d.description": "Z"}
        ],
    }
    out = _document_xml(fill_docx(docx, {}, rows_by_region=rows))
    assert out.count("<w:tr>") == 5  # 2 + 3
    assert "{{#" not in out


def test_empty_region_collapses_to_nothing():
    data_row = _tr(_tc("{{#ligne_debours_ntx}}{{d.description}}"))
    docx = _make_docx(_doc(_tbl(_tr(_tc("Entête")), data_row)))
    out = _document_xml(fill_docx(docx, {}, rows_by_region={"ligne_debours_ntx": []}))
    assert out.count("<w:tr>") == 1  # only the header row survives
    assert "{{#ligne_debours_ntx}}" not in out and "{{d.description}}" not in out


def test_row_scoped_field_escaping_and_backslash():
    data_row = _tr(_tc("{{#ligne_honoraire}}{{h.description}}"))
    docx = _make_docx(_doc(_tbl(data_row)))
    rows = {"ligne_honoraire": [{"h.description": "A & B <x> \\g<0>"}]}
    out = _document_xml(fill_docx(docx, {}, rows_by_region=rows))
    assert "A &amp; B &lt;x&gt;" in out
    assert "\\g&lt;0&gt;" in out  # function replacement — backslash literal
    assert "<x>" not in out


# ── Phase H.2: conditional regions ──────────────────────────────────────

def test_conditional_true_keeps_content_strips_markers():
    import xml.etree.ElementTree as ET
    docx = _make_docx(_doc(
        _para("{{?si_honoraires}}"),
        _tbl(_tr(_tc("Honoraires professionnels"))),
        _para("{{/si_honoraires}}"),
    ))
    out = _document_xml(fill_docx(docx, {}, conditions={"si_honoraires": True}))
    ET.fromstring(out)
    assert "Honoraires professionnels" in out
    assert "{{?si_honoraires}}" not in out and "{{/si_honoraires}}" not in out


def test_conditional_true_removes_empty_marker_paragraphs_no_blank_line():
    # A kept section must not leave a blank line: the marker-only paragraphs
    # are removed entirely, not merely emptied.
    import xml.etree.ElementTree as ET
    docx = _make_docx(_doc(
        _para("Avant"),
        _para("{{?si_honoraires}}"),
        _tbl(_tr(_tc("Honoraires"))),
        _para("{{/si_honoraires}}"),
        _para("Après"),
    ))
    out = _document_xml(fill_docx(docx, {}, conditions={"si_honoraires": True}))
    ET.fromstring(out)
    assert "Honoraires" in out and "Avant" in out and "Après" in out
    # Only Avant, the cell paragraph, and Après remain — the two marker
    # paragraphs are gone (no empty <w:p> left behind).
    assert out.count("<w:p>") == 3
    assert "{{?" not in out and "{{/" not in out


def test_conditional_marker_paragraph_with_other_text_is_kept():
    # If a marker shares its paragraph with real text, keep the paragraph
    # (strip only the marker) — don't drop the author's content.
    docx = _make_docx(_doc(
        _para("Détail {{?si_honoraires}}important"),
        _para("corps"),
        _para("fin {{/si_honoraires}} suite"),
    ))
    out = _document_xml(fill_docx(docx, {}, conditions={"si_honoraires": True}))
    assert "Détail important" in out and "corps" in out
    assert "fin" in out and "suite" in out
    assert "{{?si_honoraires}}" not in out and "{{/si_honoraires}}" not in out
    assert out.count("<w:p>") == 3  # all three paragraphs kept (they hold text)


def test_adjacent_tables_after_conditional_get_minimal_separator():
    # Two kept conditional tables would end up directly adjacent (which Word
    # merges) — a minimal (~1pt) separator paragraph keeps them distinct with
    # no visible gap.
    import xml.etree.ElementTree as ET
    docx = _make_docx(_doc(
        _para("{{?si_honoraires}}"), _tbl(_tr(_tc("HON"))), _para("{{/si_honoraires}}"),
        _para("{{?si_debours_tx}}"), _tbl(_tr(_tc("TX"))), _para("{{/si_debours_tx}}"),
    ))
    out = _document_xml(fill_docx(
        docx, {}, conditions={"si_honoraires": True, "si_debours_tx": True}))
    ET.fromstring(out)
    assert "HON" in out and "TX" in out
    assert "</w:tbl><w:tbl>" not in out          # never directly adjacent
    assert 'w:line="20"' in out                   # minimal separator inserted


def test_conditional_false_removes_whole_span():
    import xml.etree.ElementTree as ET
    docx = _make_docx(_doc(
        _para("Avant"),
        _para("{{?si_debours_ntx}}"),
        _tbl(_tr(_tc("Débours non assujettis"))),
        _para("{{/si_debours_ntx}}"),
        _para("Après"),
    ))
    out = _document_xml(fill_docx(docx, {}, conditions={"si_debours_ntx": False}))
    ET.fromstring(out)  # well-formed after span removal
    assert "Débours non assujettis" not in out
    assert "Avant" in out and "Après" in out
    assert "{{?" not in out and "{{/" not in out


def test_unbalanced_condition_raises():
    docx = _make_docx(_doc(_para("{{?si_honoraires}}"), _tbl(_tr(_tc("x")))))
    with pytest.raises(DocxFillError):
        fill_docx(docx, {}, conditions={"si_honoraires": True})


def test_false_conditional_wrapping_table_skips_row_expansion():
    # Conditionals run BEFORE rows (§4.3): a removed table's repeating rows
    # are never expanded and no orphan marker survives.
    import xml.etree.ElementTree as ET
    docx = _make_docx(_doc(
        _para("{{?si_debours_tx}}"),
        _tbl(_tr(_tc("{{#ligne_debours_tx}}{{d.description}}"))),
        _para("{{/si_debours_tx}}"),
    ))
    out = _document_xml(fill_docx(
        docx, {},
        rows_by_region={"ligne_debours_tx": [{"d.description": "NE DOIT PAS PARAÎTRE"}]},
        conditions={"si_debours_tx": False},
    ))
    ET.fromstring(out)
    assert "NE DOIT PAS PARAÎTRE" not in out
    assert "{{#ligne_debours_tx}}" not in out
    assert "<w:tbl>" not in out


def test_phase_h_callers_unaffected_without_extras():
    # No rows/conditions → identical to Phase H behavior.
    docx = _make_docx(_doc(_para("{{x}} et {{#pasunregion}} littéral")))
    out = _document_xml(fill_docx(docx, {"x": "rempli"}))
    assert "rempli" in out
    # A region marker with no rows_by_region is left untouched.
    assert "{{#pasunregion}}" in out


# ── Phase H.2: marker split-run detection (§3.4) ────────────────────────

def test_structural_split_of_region_marker_flagged():
    # A <w:br/> inside {{#ligne_honoraire}} can't be bridged → suspect.
    split = ("<w:tr><w:tc><w:p><w:r><w:t>{{#ligne</w:t></w:r>"
             "<w:r><w:br/></w:r><w:r><w:t>_honoraire}}</w:t></w:r></w:p></w:tc></w:tr>")
    docx = _make_docx(_doc(_tbl(split)))
    result = validate_template(docx)
    assert "#ligne_honoraire" in result.split_run_suspects


def test_clean_markers_not_flagged():
    docx = _make_docx(_doc(
        _para("{{?si_honoraires}}"),
        _tbl(_tr(_tc("{{#ligne_honoraire}}{{h.date}}"))),
        _para("{{/si_honoraires}}"),
    ))
    result = validate_template(docx)
    assert result.split_run_suspects == []
    assert result.errors == []


def test_xml_tag_re_is_redos_safe_and_equivalent():
    """The tag-stripping regex excludes ``<`` (not just ``>``) from the body so
    a run of unclosed ``<`` cannot cause quadratic blow-up (CWE-1333 /
    py/polynomial-redos).  The identical pattern backs ``security.sanitize``."""
    import time

    from utils.docx_fill import _XML_TAG_RE

    # Well-formed XML: identical to the historical ``<[^>]+>`` behavior.
    assert _XML_TAG_RE.sub("", "<w:p><w:t>Bonjour</w:t></w:p>") == "Bonjour"
    assert _XML_TAG_RE.sub("", "plain text") == "plain text"

    # Pins the ReDoS-safe body class: the old ``<[^>]+>`` would swallow the
    # nested ``<`` and return ""; the ``[^<>]`` body stops at it, leaving "<a".
    # If this assertion fails, the vulnerable pattern was reintroduced.
    assert _XML_TAG_RE.sub("", "<a<b>") == "<a"

    # Linearity guard: a long run of unclosed ``<`` must be handled fast. The
    # old quadratic pattern took tens of seconds at this size; the linear one
    # is sub-millisecond.  Threshold is deliberately loose to avoid flakiness.
    pathological = "<" * 100_000
    start = time.perf_counter()
    result = _XML_TAG_RE.sub("", pathological)
    elapsed = time.perf_counter() - start
    assert result == pathological  # no ``>`` present → nothing is stripped
    assert elapsed < 2.0, f"tag strip took {elapsed:.2f}s — regex may be quadratic again"
