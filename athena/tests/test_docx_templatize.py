"""Le moteur de « gabaritisation » d'un .docx (lot 2B, étape 1).

``utils/docx_templatize`` remplace, dans une lettre achevée, chaque texte
propre au dossier (un nom, un numéro, une adresse) par le ``{{champ}}`` que
le moteur de remplissage résout — sans toucher à un octet de ce que Word
doit rouvrir sans réparation. Ce qui est épinglé :

1. Un texte que Word a coupé entre plusieurs passages (``w:r``) de mises en
   forme, de langues ou de rsid différents est trouvé ENTIER et remplacé : le
   champ va dans le premier passage, les caractères du texte quittent les
   suivants, et la structure (la suite des éléments) reste identique.
2. Parties : le corps, les en-têtes et les pieds sont remplacés — exactement
   les cibles de ``fill_docx`` ; les notes de bas de page et de fin, les
   commentaires et les propriétés (``docProps``) sont COMPTÉS, jamais
   remplacés (un champ planté là ne se remplirait jamais).
3. Ce qui n'est jamais remplacé mais toujours compté : le résultat d'un
   champ Word (``fldChar`` / ``fldSimple``), un contrôle de contenu lié à des
   données (``w:dataBinding``), un texte coupé par un trait d'union
   conditionnel ou insécable. Les frontières (tabulation, saut, dessin,
   paragraphe) ne se franchissent pas.
4. Sources refusées, raison nommée : modifications suivies, commentaires,
   Strict Open XML, XML mal formé, partie non UTF-8, trop de texte, trop
   d'éléments.
5. Zones de texte ``mc:Choice``/``mc:Fallback`` : les deux copies sont
   remplacées pareillement, ou l'appel est refusé ; le décompte logique ne
   compte que la première.
6. Comparaison : espaces insécables et apostrophes repliées UN pour UN, pour
   la comparaison seulement ; mots entiers (Unicode) ; le texte le plus long
   d'abord ; un ``{{…}}`` existant n'est jamais atteint ni cassé.
7. Tout ou rien : un décompte différent de ``expected_occurrences`` refuse
   l'appel entier, sans aucune sortie.
8. La sortie : ``validate_template`` sans erreur ni fragment pour les champs
   insérés, l'archive se rouvre, chaque partie réécrite se lit avec
   defusedxml, chaque autre entrée est identique à l'octet — et
   ``fill_docx`` remplit le résultat.
9. Linéarité (CWE-1333) : balayage DÉRIVÉ de chaque motif du module (aucun
   ``.`` hors classe, aucun DOTALL, aucune répétition imbriquée), aucun motif
   construit à partir des données, et des entrées hostiles aux plafonds du
   moteur de remplissage en temps borné.
"""

import ast
import io
import os
import pathlib
import re
import sys
import time
import zipfile
from re import _constants as sre_constants
from re import _parser as sre_parse
from unittest import mock

import pytest
from defusedxml import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import docx_templatize as tz  # noqa: E402
from utils.docx_fill import (  # noqa: E402
    MAX_SINGLE_XML_BYTES,
    fill_docx,
    validate_template,
)
from utils.docx_templatize import Substitution as Sub  # noqa: E402

W_URI = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = (
    f'xmlns:w="{W_URI}" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" mc:Ignorable="wps"'
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _r(text: str, rpr: str = "", attrs: str = "") -> str:
    space = ' xml:space="preserve"' if text[:1] == " " or text[-1:] == " " else ""
    return f"<w:r{attrs}>{rpr}<w:t{space}>{text}</w:t></w:r>"


def _p(*content: str) -> str:
    return "<w:p>" + "".join(content) + "</w:p>"


def _doc(*paragraphs: str) -> str:
    return f"{DECL}<w:document {NS}><w:body>" + "".join(paragraphs) + (
        "<w:sectPr/></w:body></w:document>")


def _hdr(*paragraphs: str) -> str:
    return f"{DECL}<w:hdr {NS}>" + "".join(paragraphs) + "</w:hdr>"


def _ftr(*paragraphs: str) -> str:
    return f"{DECL}<w:ftr {NS}>" + "".join(paragraphs) + "</w:ftr>"


def _docx(document: str, **parts) -> bytes:
    """A package: [Content_Types].xml, the document, a STORED image (a
    different compression type, for the byte-identity check) and *parts*
    (``word__header1_xml="…"`` → ``word/header1.xml``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("word/document.xml", document)
        info = zipfile.ZipInfo("word/media/image1.png", (2024, 5, 6, 7, 8, 10))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, PNG)
        for key, data in parts.items():
            zf.writestr(key.replace("__", "/").replace("_xml", ".xml"), data)
    return buf.getvalue()


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _part(data: bytes, name: str = "word/document.xml") -> str:
    return _entries(data)[name].decode("utf-8")


def _text(xml: str) -> str:
    """Visible text of a part, paragraphs joined by « | »."""
    return re.sub(r"<[^<>]*>", "", re.sub(r"</w:p>", "|", xml))


def _tag_names(xml: str) -> list[str]:
    return re.findall(r"<(/?[A-Za-z_][^\s/>]*)", xml)


def _ok(data: bytes, subs: list) -> tz.TemplatizeResult:
    result = tz.templatize(data, subs)
    assert result.ok, (result.blockers, result.errors)
    assert result.data is not None
    _assert_sound_output(data, result)
    return result


def _assert_sound_output(source: bytes, result: tz.TemplatizeResult) -> None:
    """The output-side guarantees, on EVERY fixture that produces bytes."""
    out = result.data
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert zf.testzip() is None
        out_infos = zf.infolist()
    with zipfile.ZipFile(io.BytesIO(source)) as zf:
        src_infos = zf.infolist()
    # Same entries, same order, same compression and timestamps.
    assert [i.filename for i in out_infos] == [i.filename for i in src_infos]
    for a, b in zip(src_infos, out_infos):
        assert (a.compress_type, a.date_time) == (b.compress_type, b.date_time)
    src, dst = _entries(source), _entries(out)
    for name in src:
        if name in result.rewritten_parts:
            ET.fromstring(dst[name])                       # parses
            assert _tag_names(dst[name].decode()) == _tag_names(src[name].decode())
        else:
            assert dst[name] == src[name], name            # byte-identical
    validation = validate_template(out)
    assert validation.errors == []
    inserted = {r.placeholder for r in result.substitutions if r.substituted
                or r.in_alternate_branches}
    assert inserted <= set(validation.placeholders)
    assert not inserted & set(validation.split_run_suspects)


def _joined_messages(result: tz.TemplatizeResult) -> str:
    return " ".join([*result.errors, *result.warnings,
                     *(b.message for b in result.blockers)])


# ══════════════════════════════════════════════════════════════════════
# 1. Un texte coupé entre plusieurs passages
# ══════════════════════════════════════════════════════════════════════


SPLIT_BODY = _p(
    _r("Madame Jean "),
    '<w:proofErr w:type="spellStart"/>',
    _r("Trem", '<w:rPr><w:b/><w:lang w:val="en-US"/></w:rPr>', ' w:rsidR="00A1B2C3"'),
    '<w:proofErr w:type="spellEnd"/>',
    '<w:bookmarkStart w:id="0" w:name="nom"/>',
    _r("blay, bonjour.", '<w:rPr><w:i/><w:color w:val="FF0000"/></w:rPr>'),
    '<w:bookmarkEnd w:id="0"/>',
)


def test_a_literal_split_across_differently_formatted_runs_is_replaced():
    data = _docx(_doc(SPLIT_BODY))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    report = result.substitutions[0]
    assert report.substituted == 1
    assert report.by_part == {"word/document.xml": 1}
    assert report.classification == "auto"
    xml = _part(result.data)
    # The placeholder sits whole in the FIRST run; the others lost only the
    # literal's characters — their formatting, the rsid, the proofing marks
    # and the bookmark are all still there.
    assert '<w:t xml:space="preserve">Madame {{client.nom_complet}}</w:t>' in xml
    assert ('<w:r w:rsidR="00A1B2C3"><w:rPr><w:b/><w:lang w:val="en-US"/>'
            '</w:rPr><w:t></w:t></w:r>') in xml
    assert ('<w:rPr><w:i/><w:color w:val="FF0000"/></w:rPr>'
            '<w:t>, bonjour.</w:t>') in xml
    assert '<w:bookmarkStart w:id="0" w:name="nom"/>' in xml
    assert _text(xml) == "Madame {{client.nom_complet}}, bonjour.|"


def test_the_filled_output_prints_the_value_where_the_literal_was():
    data = _docx(_doc(SPLIT_BODY))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    filled = fill_docx(result.data, {"client.nom_complet": "Marie Roy"})
    assert _text(_part(filled)) == "Madame Marie Roy, bonjour.|"


def test_text_around_the_literal_and_other_runs_are_untouched():
    other = _p(_r("Autre paragraphe, sans rien.", "<w:rPr><w:u w:val=\"single\"/></w:rPr>"))
    data = _docx(_doc(SPLIT_BODY, other))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert other in _part(result.data)


def test_a_literal_inside_a_hyperlink_is_replaced_and_the_link_kept():
    link = ('<w:hyperlink r:id="rId7" w:history="1">'
            + _r("jean@tremblay.ca", '<w:rPr><w:rStyle w:val="Lienhypertexte"/></w:rPr>')
            + "</w:hyperlink>")
    data = _docx(_doc(_p(_r("Écrivez à "), link, _r("."))))
    result = _ok(data, [Sub("jean@tremblay.ca", "client.courriel", 1)])
    xml = _part(result.data)
    assert '<w:hyperlink r:id="rId7" w:history="1">' in xml
    assert "<w:t>{{client.courriel}}</w:t></w:r></w:hyperlink>" in xml


def test_a_literal_straddling_a_hyperlink_edge_is_found_whole():
    link = '<w:hyperlink r:id="rId7">' + _r("Tremblay") + "</w:hyperlink>"
    data = _docx(_doc(_p(_r("Me Jean "), link)))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    xml = _part(result.data)
    assert "Me {{client.nom_complet}}" in xml
    assert '<w:hyperlink r:id="rId7"><w:r><w:t></w:t></w:r></w:hyperlink>' in xml


# ══════════════════════════════════════════════════════════════════════
# 2. Les parties
# ══════════════════════════════════════════════════════════════════════


def test_headers_and_footers_are_targets_and_counted_per_part():
    data = _docx(
        _doc(_p(_r("Dossier 2026-001 — Jean Tremblay"))),
        word__header1_xml=_hdr(_p(_r("N/Réf. : 2026-001"))),
        word__header2_xml=_hdr(_p(_r("Page de garde"))),
        word__footer1_xml=_ftr(_p(_r("2026-001 | page"))),
    )
    subs = [Sub("2026-001", "dossier.reference_interne", 3)]
    result = _ok(data, subs)
    assert tz.analyse(data, subs).substitutions == result.substitutions
    report = result.substitutions[0]
    assert report.by_part == {"word/document.xml": 1, "word/header1.xml": 1,
                              "word/footer1.xml": 1}
    assert set(result.rewritten_parts) == {
        "word/document.xml", "word/header1.xml", "word/footer1.xml"}
    # A target part the call does not edit is copied byte-identical.
    assert _entries(result.data)["word/header2.xml"] == _entries(data)[
        "word/header2.xml"]
    filled = fill_docx(result.data, {"dossier.reference_interne": "2027-042"})
    assert "N/Réf. : 2027-042" in _text(_part(filled, "word/header1.xml"))


FOOTNOTES = (f"{DECL}<w:footnotes {NS}><w:footnote w:id=\"1\">"
             + _p(_r("Voir la lettre de Jean Tremblay.")) + "</w:footnote></w:footnotes>")
CORE = (f"{DECL}<cp:coreProperties xmlns:cp=\"http://schemas.openxmlformats.org/"
        "package/2006/metadata/core-properties\" xmlns:dc=\"http://purl.org/dc/"
        "elements/1.1/\"><dc:title>Lettre à Jean Tremblay</dc:title>"
        "<dc:creator>Jean Tremblay</dc:creator></cp:coreProperties>")


def test_footnotes_endnotes_and_properties_are_counted_never_replaced():
    endnotes = (f"{DECL}<w:endnotes {NS}><w:endnote w:id=\"2\">"
                + _p(_r("Jean Tremblay")) + "</w:endnote></w:endnotes>")
    data = _docx(_doc(_p(_r("Bonjour Jean Tremblay."))),
                 word__footnotes_xml=FOOTNOTES, word__endnotes_xml=endnotes,
                 docProps__core_xml=CORE)
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    report = result.substitutions[0]
    assert report.substituted == 1
    assert report.in_non_target_parts == {
        "word/footnotes.xml": 1, "word/endnotes.xml": 1, "docProps/core.xml": 2}
    assert report.not_substituted == 4
    entries, source = _entries(result.data), _entries(data)
    for name in ("word/footnotes.xml", "word/endnotes.xml", "docProps/core.xml"):
        assert entries[name] == source[name]
    assert any("word/footnotes.xml" in w for w in result.warnings)


# ══════════════════════════════════════════════════════════════════════
# 3. Compté, jamais remplacé — et les frontières
# ══════════════════════════════════════════════════════════════════════


COMPLEX_FIELD = (
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> DOCPROPERTY Client </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    + _r("Jean Tremblay")
    + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
)


def test_a_field_result_is_counted_but_never_replaced():
    simple = ('<w:fldSimple w:instr=" AUTHOR ">' + _r("Jean Tremblay")
              + "</w:fldSimple>")
    data = _docx(_doc(_p(_r("À "), COMPLEX_FIELD), _p(simple),
                      _p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    report = result.substitutions[0]
    assert (report.substituted, report.in_field_results) == (1, 2)
    xml = _part(result.data)
    assert xml.count("<w:t>Jean Tremblay</w:t>") == 2      # both results intact
    assert xml.count("{{client.nom_complet}}") == 1


def test_a_field_spanning_paragraphs_keeps_its_whole_result_uneditable():
    data = _docx(_doc(
        _p('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
           '<w:r><w:instrText> TOC </w:instrText></w:r>'
           '<w:r><w:fldChar w:fldCharType="separate"/></w:r>', _r("Jean Tremblay")),
        _p(_r("Jean Tremblay"), '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
        _p(_r("Jean Tremblay"))))
    report = tz.analyse(data, [Sub("Jean Tremblay", "client.nom_complet", 1)]
                        ).substitutions[0]
    assert (report.substituted, report.in_field_results) == (1, 2)


def test_a_data_bound_content_control_is_counted_an_unbound_one_replaced():
    bound = ('<w:sdt><w:sdtPr><w:alias w:val="Client"/>'
             '<w:dataBinding w:xpath="/root/client" w:storeItemID="{1}"/>'
             '</w:sdtPr><w:sdtContent>' + _r("Jean Tremblay")
             + "</w:sdtContent></w:sdt>")
    unbound = ('<w:sdt><w:sdtPr><w:alias w:val="Nom"/></w:sdtPr><w:sdtContent>'
               + _r("Jean Tremblay") + "</w:sdtContent></w:sdt>")
    data = _docx(_doc(_p(bound), _p(unbound)))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    report = result.substitutions[0]
    assert (report.substituted, report.in_bound_controls) == (1, 1)
    xml = _part(result.data)
    assert "<w:t>Jean Tremblay</w:t></w:r></w:sdtContent></w:sdt></w:p><w:p>" in xml


@pytest.mark.parametrize("barrier", [
    "<w:r><w:tab/></w:r>", "<w:r><w:br/></w:r>", "<w:r><w:cr/></w:r>",
    '<w:r><w:footnoteReference w:id="1"/></w:r>',
    '<w:r><w:drawing><wp:inline/></w:drawing></w:r>',
    '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
    '<w:r><w:sym w:font="Symbol" w:char="F0B7"/></w:r>',
    "</w:p><w:p>",
])
def test_boundaries_are_never_crossed(barrier):
    data = _docx(_doc("<w:p>" + _r("Jean") + barrier + _r("Tremblay") + "</w:p>"))
    result = tz.analyse(data, [Sub("Jean Tremblay", "client.nom_complet", None)])
    assert result.ok
    assert result.substitutions[0].substituted == 0


def test_a_text_box_paragraph_never_joins_its_host_paragraph():
    box = ('<w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
           + _p(_r("Tremblay")) + "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>")
    data = _docx(_doc(_p(_r("Jean "), box, _r(" suite"))))
    report = tz.analyse(data, [Sub("Jean Tremblay", "client.nom_complet", None),
                               Sub("Tremblay", "adverse.nom_complet", None)])
    assert [r.substituted for r in report.substitutions] == [0, 1]


def test_a_soft_hyphen_or_a_non_breaking_hyphen_blocks_and_is_reported():
    data = _docx(_doc(
        _p(_r("Trem"), "<w:r><w:softHyphen/></w:r>", _r("blay")),
        _p(_r("Jean"), "<w:r><w:noBreakHyphen/></w:r>", _r("Pierre Roy"))))
    result = tz.analyse(data, [Sub("Tremblay", "adverse.nom_complet", None),
                               Sub("Jean-Pierre Roy", "client.nom_complet", None)])
    assert [(r.substituted, r.blocked_by_markup) for r in result.substitutions] == [
        (0, 1), (0, 1)]
    assert any("trait d'union" in w for w in result.warnings)


def test_a_longer_literal_blocked_by_a_soft_hyphen_still_owns_its_range():
    """Longest first holds for an occurrence that cannot be substituted: a
    shorter literal never lands inside it (« Inc. » stays in the reported
    « Trem\u00adblay Inc. »), while a free occurrence elsewhere is replaced."""
    data = _docx(_doc(
        _p(_r("Trem"), "<w:r><w:softHyphen/></w:r>", _r("blay Inc. a signé")),
        _p(_r("Roy Inc. aussi"))))
    result = tz.analyse(data, [Sub("Tremblay Inc.", "adverse.nom_complet", None),
                               Sub("Inc.", "x", None)])
    assert [(r.substituted, r.blocked_by_markup) for r in result.substitutions] == [
        (0, 1), (1, 0)]


def test_a_soft_hyphen_is_inside_the_word_for_the_whole_word_rule():
    """« Trem\u00adblay » reads « Tremblay » on the page: « blay » must not match."""
    data = _docx(_doc(_p(_r("Trem"), "<w:r><w:softHyphen/></w:r>", _r("blay"))))
    report = tz.analyse(data, [Sub("blay", "x", None)]).substitutions[0]
    assert report.substituted == 0


# ══════════════════════════════════════════════════════════════════════
# 4. Sources refusées
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("change", [
    '<w:ins w:id="1" w:author="Marie Lavoie" w:date="2026-09-01T00:00:00Z">'
    + _r("Jean Tremblay") + "</w:ins>",
    '<w:del w:id="1" w:author="X"><w:r><w:delText>Jean Tremblay</w:delText></w:r></w:del>',
    '<w:moveFrom w:id="1" w:author="X">' + _r("Jean Tremblay") + "</w:moveFrom>",
    '<w:moveTo w:id="1" w:author="X">' + _r("Jean Tremblay") + "</w:moveTo>",
    '<w:r><w:rPr><w:b/><w:rPrChange w:id="2" w:author="X"><w:rPr/></w:rPrChange>'
    + "</w:rPr><w:t>Jean Tremblay</w:t></w:r>",
])
def test_tracked_changes_refuse_the_source_naming_the_reason(change):
    data = _docx(_doc(_p(change), _p(_r("Jean Tremblay"))))
    for fn in (tz.templatize, tz.analyse):
        result = fn(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
        assert result.data is None and not result.ok
        assert [b.code for b in result.blockers] == ["tracked_changes"]
        assert result.blockers[0].parts == ("word/document.xml",)
        assert "modifications suivies" in result.blockers[0].message
        assert result.substitutions[0].substituted == 0


def test_a_tracked_change_in_a_header_or_footnote_refuses_too():
    data = _docx(_doc(_p(_r("Jean Tremblay"))),
                 word__footer1_xml=_ftr(_p('<w:ins w:id="1" w:author="X">'
                                          + _r("x") + "</w:ins>")))
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.blockers[0].code == "tracked_changes"
    assert result.blockers[0].parts == ("word/footer1.xml",)


def test_comment_anchors_or_comments_refuse_the_source():
    anchored = _docx(_doc(_p('<w:commentRangeStart w:id="0"/>', _r("Jean Tremblay"),
                             '<w:commentRangeEnd w:id="0"/>',
                             '<w:r><w:commentReference w:id="0"/></w:r>')))
    comments_only = _docx(
        _doc(_p(_r("Jean Tremblay"))),
        word__comments_xml=(f"{DECL}<w:comments {NS}><w:comment w:id=\"0\" "
                            "w:author=\"Marie Lavoie\">" + _p(_r("À revoir"))
                            + "</w:comment></w:comments>"))
    empty_comments = _docx(_doc(_p(_r("Jean Tremblay"))),
                           word__comments_xml=f"{DECL}<w:comments {NS}/>")
    for data in (anchored, comments_only):
        result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
        assert [b.code for b in result.blockers] == ["comments"]
        assert "commentaires" in result.blockers[0].message
    assert tz.templatize(empty_comments,
                         [Sub("Jean Tremblay", "client.nom_complet", 1)]).ok


def test_every_blocker_is_reported_at_once():
    data = _docx(_doc(_p('<w:ins w:id="1" w:author="X">' + _r("a") + "</w:ins>",
                         '<w:commentRangeStart w:id="0"/>')))
    codes = {b.code for b in tz.analyse(data, [Sub("Jean", "x", None)]).blockers}
    assert codes == {"tracked_changes", "comments"}


@pytest.mark.parametrize("xml", [
    _doc(_p(_r("a < b"))),                                     # stray « < »
    _doc(_p("<w:r><w:t>a<w:b/>b</w:t></w:r>")),                # markup in w:t
    _doc(_p("<w:r><w:t>a<!-- x -->b</w:t></w:r>")),            # comment in w:t
    _doc(_p("<w:r><w:t>Tremblay &eacute;</w:t></w:r>")),       # HTML entity
    _doc(_p("<w:r><w:t>Tremblay & fils</w:t></w:r>")),         # bare «&»
    _doc(_p("<w:r><w:t>&#0;</w:t></w:r>")),                    # illegal char
    _doc(_p('<w:fldSimple w:instr="IF a > 3">' + _r("x") + "</w:fldSimple>")),
    _doc(_p("<w:r><w:t><![CDATA[Jean]]></w:t></w:r>")),
    _doc("<w:p><w:r><w:t>Jean</w:t></w:r>"),                   # unbalanced
    _doc(_p("<w:r><w:t>Jean</w:r></w:t>")),                     # mis-nested
    _doc(_p(_r("\ufffe"))),                                    # a sentinel
])
def test_malformed_xml_refuses_never_skips(xml):
    result = tz.analyse(_docx(xml), [Sub("Jean", "x", None)])
    assert [b.code for b in result.blockers] == ["malformed_xml"]
    assert result.blockers[0].parts == ("word/document.xml",)


def test_strict_ooxml_a_foreign_prefix_and_utf16_are_refused():
    strict = _docx(_doc(_p(_r("Jean"))).replace(
        W_URI, "http://purl.oclc.org/ooxml/wordprocessingml/main"))
    foreign = _docx(_doc(_p(_r("Jean"))).replace('xmlns:w=', 'xmlns:x='))
    utf16 = _docx(_doc(_p(_r("Jean"))).encode("utf-16"))
    for data, code in ((strict, "strict_ooxml"), (foreign, "namespace"),
                       (utf16, "encoding")):
        result = tz.templatize(data, [Sub("Jean", "x", 1)])
        assert [b.code for b in result.blockers] == [code], code
        assert result.data is None


def test_an_unreadable_or_duplicated_package_refuses():
    assert tz.analyse(b"not a zip", [Sub("Jean", "x", None)]).blockers[0].code == (
        "unreadable")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf, pytest.warns(UserWarning):
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("word/document.xml", _doc(_p(_r("Jean"))))
        zf.writestr("word/document.xml", _doc(_p(_r("Jean"))))
    result = tz.analyse(buf.getvalue(), [Sub("Jean", "x", None)])
    assert result.blockers[0].code == "duplicate_entry"


def test_too_much_text_and_too_many_elements_refuse(monkeypatch):
    data = _docx(_doc(_p(_r("Jean Tremblay, " * 10))))
    monkeypatch.setattr(tz, "MAX_VISIBLE_CHARS", 100)
    assert tz.analyse(data, [Sub("Jean", "x", None)]).blockers[0].code == (
        "too_much_text")
    monkeypatch.setattr(tz, "MAX_VISIBLE_CHARS", 1_000_000)
    monkeypatch.setattr(tz, "MAX_TOKENS", 10)
    assert tz.analyse(data, [Sub("Jean", "x", None)]).blockers[0].code == (
        "too_complex")


# ══════════════════════════════════════════════════════════════════════
# 5. Zones de texte : mc:Choice et mc:Fallback
# ══════════════════════════════════════════════════════════════════════


def _text_box(choice_text: str, fallback_text: str) -> str:
    return (
        "<w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic>'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
        'wordprocessingShape"><wps:wsp><wps:txbx><w:txbxContent>'
        + _p(_r(choice_text))
        + "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic>"
        "</wp:anchor></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>"
        + _p(_r(fallback_text))
        + "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


def test_both_copies_of_a_text_box_are_replaced_and_counted_once():
    data = _docx(_doc(_p(_r("En-tête "), _text_box("Jean Tremblay", "Jean Tremblay")),
                      _p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 2)])
    report = result.substitutions[0]
    assert (report.substituted, report.in_alternate_branches) == (2, 1)
    assert report.fallback_consistent
    xml = _part(result.data)
    assert xml.count("{{client.nom_complet}}") == 3
    assert "Jean Tremblay" not in xml


def test_a_text_box_whose_copies_differ_is_refused():
    data = _docx(_doc(_p(_text_box("Jean Tremblay", "J. Tremblay"))))
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None
    report = result.substitutions[0]
    assert report.substituted == 1 and not report.fallback_consistent
    assert any("mc:Fallback" in e for e in result.errors)


# ══════════════════════════════════════════════════════════════════════
# 6. La comparaison
# ══════════════════════════════════════════════════════════════════════


def test_nbsp_apostrophe_and_hyphen_variants_fold_one_for_one():
    data = _docx(_doc(
        _p(_r("Me\u00a0Jean\u202fTremblay")),
        _p(_r("à l\u2019Hôtel\u2011Dieu, puis")),
        _p(_r("rue de l'Hôtel-Dieu"))))
    result = _ok(data, [Sub("Me Jean Tremblay", "client.nom_complet", 1),
                        Sub("l\u2019Hôtel-Dieu", "client.adresse_civique", 2)])
    xml = _part(result.data)
    assert "{{client.nom_complet}}" in xml
    assert "à {{client.adresse_civique}}, puis" in xml
    assert "rue de {{client.adresse_civique}}" in xml


def test_folding_is_for_matching_only_the_kept_text_keeps_its_nbsp():
    data = _docx(_doc(_p(_r("Me\u00a0Jean Tremblay, avocat\u00a0:"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert "Me\u00a0{{client.nom_complet}}, avocat\u00a0:" in _part(result.data)


def test_folding_maps_one_character_to_one():
    for ch in "\u00a0\u202f\u2007\u2009\u2018\u2019\u02bc\u2010\u2011":
        assert len(tz.fold(ch)) == 1


def test_matching_is_whole_word_and_unicode_aware():
    data = _docx(_doc(_p(_r("M. Roy, Royaume, Leroy, Royé, Roy\u0301, _Roy, Roy."))))
    report = tz.analyse(data, [Sub("Roy", "adverse.nom_complet", None)]
                        ).substitutions[0]
    assert report.substituted == 2                       # « M. Roy, » and « Roy. »
    loose = tz.analyse(data, [Sub("Roy", "x", None, whole_word=False)]
                       ).substitutions[0]
    assert loose.substituted == 6                        # « Leroy » is lowercase


def test_matching_is_case_sensitive_an_all_caps_heading_needs_its_own():
    data = _docx(_doc(_p(_r("JEAN TREMBLAY")), _p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("JEAN TREMBLAY", "CLIENT.NOM_COMPLET", 1),
                        Sub("Jean Tremblay", "client.nom_complet", 1)])
    xml = _part(result.data)
    assert "{{CLIENT.NOM_COMPLET}}" in xml and "{{client.nom_complet}}" in xml
    filled = fill_docx(result.data, {"CLIENT.NOM_COMPLET": "MARIE ROY",
                                     "client.nom_complet": "Marie Roy"})
    assert _text(_part(filled)) == "MARIE ROY|Marie Roy|"


def test_the_longest_literal_goes_first():
    data = _docx(_doc(_p(_r("Jean Tremblay et M. Tremblay"))))
    result = _ok(data, [Sub("Tremblay", "adverse.nom_complet", 1),
                        Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert _text(_part(result.data)) == (
        "{{client.nom_complet}} et M. {{adverse.nom_complet}}|")


def test_overlapping_literals_the_longer_wins_the_shorter_counts_zero():
    data = _docx(_doc(_p(_r("Jean Tremblay Inc. a signé."))))
    subs = [Sub("Jean Tremblay", "client.nom_complet", 1),
            Sub("Tremblay Inc.", "adverse.nom_complet", 1)]
    result = tz.templatize(data, subs)
    assert result.data is None
    assert [r.substituted for r in result.substitutions] == [1, 0]
    assert result.errors == (
        "Substitution n° 2 ({{adverse.nom_complet}}) : 0 occurrence(s) "
        "remplaçable(s), 1 attendue(s) — rien n'a été écrit.",)


def test_an_existing_placeholder_is_never_matched_or_broken():
    data = _docx(_doc(
        _p(_r("{{client.nom}}Tremblay et {{client.nom}}")),
        _p(_r("{{ Jean Tremblay }} reste tel quel")),
        _p(_r("{{#client}} bloc {{/client}}"))))
    result = _ok(data, [Sub("Tremblay", "adverse.nom_complet", 1)])
    xml = _part(result.data)
    assert "{{client.nom}}{{adverse.nom_complet}} et {{client.nom}}" in xml
    assert "{{ Jean Tremblay }} reste tel quel" in xml
    masked = tz.analyse(data, [Sub("client", "x", None, whole_word=False),
                               Sub("Jean Tremblay", "client.nom_complet", None)])
    assert [r.substituted for r in masked.substitutions] == [0, 0]


def test_a_placeholder_fragmented_across_runs_is_masked_too():
    data = _docx(_doc(_p(_r("{{client."), _r("nom}} "), _r("nom de famille"))))
    report = tz.analyse(data, [Sub("nom", "x", None)]).substitutions[0]
    assert report.substituted == 1


# ══════════════════════════════════════════════════════════════════════
# 7. Tout ou rien
# ══════════════════════════════════════════════════════════════════════


def test_an_expected_count_mismatch_refuses_the_whole_call():
    data = _docx(_doc(_p(_r("Jean Tremblay, 2026-001")), _p(_r("Jean Tremblay"))))
    subs = [Sub("2026-001", "dossier.reference_interne", 1),
            Sub("Jean Tremblay", "client.nom_complet", 1)]
    result = tz.templatize(data, subs)
    assert result.data is None and not result.ok
    assert [r.substituted for r in result.substitutions] == [1, 2]
    assert result.errors == (
        "Substitution n° 2 ({{client.nom_complet}}) : 2 occurrence(s) "
        "remplaçable(s), 1 attendue(s) — rien n'a été écrit.",)
    # analyse reports the same counts, and the same refusal.
    dry = tz.analyse(data, subs)
    assert dry.errors == result.errors and dry.data is None
    assert dry.substitutions == result.substitutions


def test_analyse_takes_no_expected_count_and_never_produces_bytes():
    data = _docx(_doc(_p(_r("Jean Tremblay"))))
    result = tz.analyse(data, [Sub("Jean Tremblay", "client.nom_complet", None)])
    assert result.ok and result.data is None
    assert result.substitutions[0].substituted == 1
    refused = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", None)])
    assert refused.data is None
    assert "nombre d'occurrences attendu est requis" in refused.errors[0]


def test_zero_occurrence_refuses():
    data = _docx(_doc(_p(_r("Bonjour."))))
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None and result.substitutions[0].substituted == 0


def test_no_message_ever_quotes_a_literal():
    """Both paths: the COUNTING one (a mismatch, a text box whose copies
    differ, occurrences left in a field and a footnote, a passthrough and a
    case warning) and the REQUEST one (invalid substitutions)."""
    data = _docx(_doc(_p(_r("Jean Tremblay, Jean Tremblay")),
                      _p(_text_box("Marie Lavoie", "M. Lavoie")),
                      _p(COMPLEX_FIELD), _p(_r("TREMBLAY"))),
                 word__footnotes_xml=FOOTNOTES)
    counting = [Sub("Jean Tremblay", "client.nom_complet", 1),
                Sub("Marie Lavoie", "adverse.nom_complet", 1),
                Sub("TREMBLAY", "adverse.nom", 1),
                Sub("Lavoie", "FAITS", 9)]
    invalid = [Sub("{Tremblay}", "x", 1), Sub("Jean Tremblay", "bad name!", 1),
               Sub(" Lavoie", "x", 1)]
    results = [fn(data, subs) for fn in (tz.templatize, tz.analyse)
               for subs in (counting, invalid)]
    counted = results[0]
    assert len(counted.errors) >= 3 and len(counted.warnings) >= 3
    assert any("mc:Fallback" in e for e in counted.errors)
    assert any("résultat d'un champ" in w for w in counted.warnings)
    assert len(results[1].errors) == 3
    for result in results:
        text = _joined_messages(result)
        for literal in ("Jean Tremblay", "Marie Lavoie", "Tremblay", "TREMBLAY",
                        "Lavoie"):
            assert literal not in text


# ══════════════════════════════════════════════════════════════════════
# 8. La sortie
# ══════════════════════════════════════════════════════════════════════


def test_xml_space_preserve_is_added_only_when_the_new_text_needs_it():
    data = _docx(_doc(
        _p("<w:r><w:t>Jean</w:t></w:r>", "<w:r><w:t>Tremblay suite</w:t></w:r>"),
        _p('<w:r><w:t xml:space="default">Marie</w:t></w:r>',
           "<w:r><w:t>Lavoie  deux espaces</w:t></w:r>"),
        # Already standing on an edge space WITHOUT the attribute: the edit
        # leaves the tag as Word was reading it.
        _p("<w:r><w:t>Roy Inc. </w:t></w:r>")))
    # « Jean » | « Tremblay suite » are two runs with NO space between: the
    # literal is « JeanTremblay », so use a literal that spans the join.
    result = _ok(data, [Sub("JeanTremblay", "client.nom_complet", 1),
                        Sub("MarieLavoie", "adverse.nom_complet", 1),
                        Sub("Roy Inc.", "destinataire.nom_complet", 1)])
    xml = _part(result.data)
    assert "<w:t>{{destinataire.nom_complet}} </w:t>" in xml
    assert "<w:t>{{client.nom_complet}}</w:t>" in xml
    assert '<w:t xml:space="preserve"> suite</w:t>' in xml
    assert '<w:t xml:space="default">{{adverse.nom_complet}}</w:t>' in xml
    assert '<w:t xml:space="preserve">  deux espaces</w:t>' in xml


def test_entities_are_decoded_for_matching_and_re_escaped_on_output():
    data = _docx(_doc(_p("<w:r><w:t>Tremblay &amp; Associ&#233;s &lt;inc&gt;"
                         "&#13;fin</w:t></w:r>")))
    result = _ok(data, [Sub("Tremblay & Associés", "adverse.nom_complet", 1)])
    xml = _part(result.data)
    assert "<w:t>{{adverse.nom_complet}} &lt;inc&gt;&#13;fin</w:t>" in xml
    assert ET.fromstring(xml.encode()).find(f".//{{{W_URI}}}t").text == (
        "{{adverse.nom_complet}} <inc>\rfin")


def test_a_utf8_bom_is_kept():
    data = _docx("\ufeff" + _doc(_p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert _entries(result.data)["word/document.xml"].startswith(b"\xef\xbb\xbf")


def test_a_placeholder_already_fragmented_in_the_source_refuses_its_reuse():
    """The name the call inserts is ALSO present, fragmented, in the source:
    the template would not fill it — refused, with the fix named."""
    # A <w:br/> INSIDE the braces is a structural split the fill engine
    # cannot heal (docx_fill._normalize_runs only folds adjacent text runs).
    data = _docx(_doc(_p(_r("{{client."), "<w:r><w:br/></w:r>",
                         _r("nom_complet}}"), _r(" et Jean Tremblay"))))
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None
    assert "fragmenté" in result.errors[0] and "retapez" in result.errors[0]


def test_a_source_s_other_fragmented_placeholders_are_warned_about():
    data = _docx(_doc(_p(_r("{{dossier."), "<w:r><w:br/></w:r>", _r("titre}}")),
                      _p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert any("fragmenté" in w and "{{dossier." in w for w in result.warnings)


def test_an_output_check_failure_refuses_rather_than_ship(monkeypatch):
    data = _docx(_doc(_p(_r("Jean Tremblay"))))
    original = tz.validate_template

    def lying(docx_bytes):
        validation = original(docx_bytes)
        validation.placeholders = []
        return validation

    monkeypatch.setattr(tz, "validate_template", lying)
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None
    assert result.errors[0].startswith("La vérification du document produit a échoué")


def test_a_result_pushed_past_the_template_cap_says_so():
    """Completeness review of lot 2B: a source just under 10 MB — here a
    STORED, incompressible image — is accepted, but a field name is longer
    than the text it replaces, so the RESULT crosses the fill engine's
    10 MB cap. The engine refused (right) with « gabarit invalide » and
    nothing else, and the preview then said only that no result could be
    produced. The reason is now named; nothing is still written, and the
    request itself stays valid (the analysis counts it)."""
    from utils.docx_fill import MAX_COMPRESSED_BYTES

    document = _doc(_p(_r("Monsieur Jean Tremblay")))

    def package(pad: int) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", CONTENT_TYPES)
            zf.writestr("word/document.xml", document)
            info = zipfile.ZipInfo("word/media/image1.png")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, os.urandom(pad))
        return buf.getvalue()

    data = package(MAX_COMPRESSED_BYTES - len(package(0)) - 2)
    assert len(data) <= MAX_COMPRESSED_BYTES
    subs = [Sub("Jean Tremblay", "client.nom_complet", 1)]
    assert tz.analyse(data, subs).substitutions[0].substituted == 1
    result = tz.templatize(data, subs)
    assert result.data is None and not result.blockers
    assert "taille maximale de 10 Mo" in result.errors[0]
    assert result.errors[0].endswith("rien n'a été écrit.")


# ══════════════════════════════════════════════════════════════════════
# 8 bis. Le lexer n'est pas un parseur (revue de l'étape 1)
#
# Chacun de ces tests ÉCHOUE sur le moteur livré à l'étape 1, qui se disait
# « bien formé PAR CONSTRUCTION » et ne lisait aucune partie avec un parseur.
# ══════════════════════════════════════════════════════════════════════


def test_a_spaced_xml_space_attribute_is_replaced_never_duplicated():
    """XML admet « xml:space = "default" ». L'expression d'attribut exigeait
    « xml:space=" » : l'attribut passait inaperçu et l'édition en ajoutait
    un SECOND — attribut en double, qu'aucun parseur (ni Word) n'accepte."""
    data = _docx(_doc(_p('<w:r><w:t xml:space = "default">Jean</w:t></w:r>',
                         '<w:r><w:t xml:space = "default">Tremblay et</w:t></w:r>')))
    result = _ok(data, [Sub("JeanTremblay", "x", 1, whole_word=False)])
    xml = _part(result.data)
    assert xml.count("xml:space") == 2          # one per <w:t>, never two
    assert '<w:t xml:space="preserve"> et</w:t>' in xml


def test_a_spaced_fld_char_type_still_marks_the_field_result():
    """« w:fldCharType = "begin" » : le champ passait inaperçu, et son
    résultat — que Word régénère — se remplaçait comme du texte."""
    field = ('<w:r><w:fldChar w:fldCharType = "begin"/></w:r>'
             '<w:r><w:instrText> DOCPROPERTY Client </w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType = "separate"/></w:r>'
             + _r("Jean Tremblay")
             + '<w:r><w:fldChar w:fldCharType = "end"/></w:r>')
    report = tz.analyse(_docx(_doc(_p(field))),
                        [Sub("Jean Tremblay", "client.nom_complet", None)]
                        ).substitutions[0]
    assert (report.substituted, report.in_field_results) == (0, 1)


def test_a_single_quoted_value_hiding_a_gt_is_refused_an_apostrophe_is_not():
    """« a='x>y' » : le motif de balise coupait au « > », le reste se lisait
    comme du TEXTE et l'édition réécrivait la valeur d'attribut. Une
    apostrophe DANS une valeur entre guillemets (« descr="l'avis" », le
    texte de remplacement d'une image) reste admise."""
    hidden = _docx(_doc(_p("<w:r><w:t a='x>Jean Tremblay'>abc</w:t></w:r>")))
    result = tz.templatize(hidden, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert [b.code for b in result.blockers] == ["malformed_xml"]
    assert result.data is None
    alt_text = _docx(_doc(_p(
        "<w:r><w:drawing><wp:docPr id=\"1\" name=\"Image\" descr=\"l'avis\"/>"
        "</w:drawing></w:r>", _r("Jean Tremblay"))))
    _ok(alt_text, [Sub("Jean Tremblay", "client.nom_complet", 1)])


@pytest.mark.parametrize("run", [
    "<w:r><w:t a=b>Jean Tremblay</w:t></w:r>",          # unquoted value
    "<w:r><w:t>Jean Tremblay \x01</w:t></w:r>",        # raw control char
    '<w:r><w:t w:a="1" w:a="2">Jean Tremblay</w:t></w:r>',  # duplicate attribute
])
def test_a_target_no_parser_accepts_is_refused_by_name_even_in_preview(run):
    """Ce que le lexer ne sait pas voir, un parseur le voit : la partie est
    refusée comme « XML mal formé » dès l'analyse — l'ancien moteur la
    réécrivait et livrait un gabarit que Word refuse ou « répare »."""
    data = _docx(_doc(_p(run)))
    preview = tz.analyse(data, [Sub("Jean Tremblay", "client.nom_complet", None)])
    assert [b.code for b in preview.blockers] == ["malformed_xml"]
    assert preview.blockers[0].parts == ("word/document.xml",)
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None and result.blockers


def test_every_rewritten_part_is_parsed_before_anything_ships(monkeypatch):
    """La garde de sortie : une édition qui casserait le XML (ici, un
    échappement neutralisé laisse un « & » nu) refuse l'appel au lieu de
    livrer — validate_template, qui ne lit aucun XML, ne l'aurait pas vu."""
    data = _docx(_doc(_p(_r("Jean Tremblay &amp; fils"))))
    monkeypatch.setattr(tz, "_escape_text", lambda text: text)
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    assert result.data is None
    assert "XML mal formé dans word/document.xml" in result.errors[0]


def test_an_occurrence_inside_an_existing_brace_span_is_counted_never_silent():
    """Le masque protège un {{…}} existant — mais l'occurrence qui s'y trouve
    RESTE dans le gabarit : « {{ Jean Tremblay }} » n'est pas un champ valide
    (une espace dans le nom), chaque document généré l'imprimerait. L'ancien
    moteur ne la comptait nulle part : l'appel réussissait, sans un mot."""
    data = _docx(_doc(_p(_r("{{ Jean Tremblay }} reste")),
                      _p(_r("Jean Tremblay"))),
                 word__footnotes_xml=(
                     f"{DECL}<w:footnotes {NS}><w:footnote w:id=\"1\">"
                     + _p(_r("{{ Jean Tremblay }}")) + "</w:footnote></w:footnotes>"))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    report = result.substitutions[0]
    assert report.substituted == 1
    assert report.in_existing_placeholders == 1
    assert report.in_non_target_parts == {"word/footnotes.xml": 1}
    assert report.not_substituted == 2
    assert any("déjà présent" in w for w in result.warnings)
    # A longer literal's range is NOT a brace span: the shorter counts zero
    # there, as before — it was substituted, by the longer.
    overlap = tz.analyse(_docx(_doc(_p(_r("Jean Tremblay inc.")))),
                         [Sub("Jean Tremblay inc.", "adverse.nom_complet", None),
                          Sub("Tremblay", "x", None)])
    assert [(r.substituted, r.in_existing_placeholders)
            for r in overlap.substitutions] == [(1, 0), (0, 0)]


def test_the_recount_normalizes_each_rewritten_part_once(monkeypatch):
    """Le recomptage normalisait la partie ENTIÈRE une fois par CHAMP, avant
    et après : 50 substitutions dans une partie au plafond d'éléments ont
    pris 84 s sur un poste de travail — au-delà des 60 s de gunicorn. Une
    normalisation par partie et par côté, quel que soit le nombre de champs."""
    names = [f"Prénom{k} Nom{k}" for k in range(5)]
    data = _docx(_doc(*(_p(_r(n)) for n in names)),
                 word__header1_xml=_hdr(_p(_r(names[0]))))
    calls = []
    real = tz._normalize_runs

    def spy(xml):
        calls.append(len(xml))
        return real(xml)

    monkeypatch.setattr(tz, "_normalize_runs", spy)
    result = _ok(data, [Sub(n, f"x{k}", 2 if k == 0 else 1)
                        for k, n in enumerate(names)])
    assert set(result.rewritten_parts) == {"word/document.xml", "word/header1.xml"}
    assert len(calls) == 2 * len(result.rewritten_parts)


# ══════════════════════════════════════════════════════════════════════
# 9. La demande
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("sub, fragment", [
    (Sub("ab", "x", 1), "de 3 à 300 caractères"),
    (Sub("a" * 301, "x", 1), "de 3 à 300 caractères"),
    (Sub(" Jean", "x", 1), "ni commencer ni finir par une espace"),
    (Sub("Jean {x}", "x", 1), "accolade"),
    (Sub("Jean\tTremblay", "x", 1), "caractère de contrôle"),
    (Sub("...", "x", 1), "au moins une lettre ou un chiffre"),
    (Sub("Jean", "client nom", 1), "nom de champ"),
    (Sub("Jean", "{{ }}", 1), "nom de champ"),
    (Sub("Jean", "x" * 121, 1), "nom de champ"),
    (Sub("Jean", "x", 0), "entier de 1 à"),
    (Sub("Jean", "x", True), "entier de 1 à"),
    (Sub("Jean", "x", 1, whole_word="oui"), "whole_word"),
    (Sub("Jean", "x", 1, expect="bloc"), "classification attendue"),
    (Sub("Jean", "client.nom_complet", 1, expect="manual"),
     "se classe « auto », pas « manual »"),
])
def test_an_invalid_substitution_is_refused_by_number(sub, fragment):
    data = _docx(_doc(_p(_r("Jean Tremblay"))))
    result = tz.templatize(data, [Sub("Tremblay", "adverse.nom_complet", 1), sub])
    assert result.data is None
    assert len(result.errors) == 1
    assert result.errors[0].startswith("Substitution n° 2 : ")
    assert fragment in result.errors[0]


def test_an_invalid_substitution_never_zeroes_the_valid_ones():
    """Review of lot 2B step 2: a request error used to stop the run before
    anything was counted — every VALID sibling then read « substituted: 0 »
    (a preview's lie). They are counted now, on both entry points; the
    invalid one keeps its error, and templatize still writes nothing."""
    data = _docx(_doc(_p(_r("Jean Tremblay et Jean Tremblay"))))
    subs = [Sub("Jean {X}", "client.nom", None),
            Sub("Jean Tremblay", "client.nom_complet", None)]
    counted = tz.analyse(data, subs)
    assert len(counted.errors) == 1 and "accolade" in counted.errors[0]
    assert counted.substitutions[1].substituted == 2
    assert counted.substitutions[0].substituted == 0
    written = tz.templatize(data, [Sub("Jean {X}", "client.nom", 1),
                                   Sub("Jean Tremblay", "client.nom_complet", 2)])
    assert written.data is None and written.rewritten_parts == ()
    assert len(written.errors) == 1
    assert written.substitutions[1].substituted == 2


def test_literals_equal_after_folding_are_refused_as_duplicates():
    data = _docx(_doc(_p(_r("l'Hôtel"))))
    result = tz.analyse(data, [Sub("l'Hôtel", "a", None), Sub("l\u2019Hôtel", "b", None)])
    assert result.errors == (
        "Substitution n° 2 : même texte à remplacer que la substitution n° 1 "
        "(après normalisation des espaces insécables et des apostrophes).",)


def test_the_substitution_count_is_bounded_and_non_empty():
    data = _docx(_doc(_p(_r("Jean"))))
    assert "Aucune substitution" in tz.analyse(data, []).errors[0]
    many = [Sub(f"Jean{k:03d}", f"x{k}", None) for k in range(tz.MAX_SUBSTITUTIONS + 1)]
    assert "Trop de substitutions" in tz.analyse(data, many).errors[0]


def test_programming_errors_raise_type_error():
    with pytest.raises(TypeError):
        tz.analyse("not bytes", [Sub("Jean", "x", None)])
    with pytest.raises(TypeError):
        tz.analyse(b"", [{"literal": "Jean"}])
    with pytest.raises(TypeError):
        tz.analyse(b"", "Jean")


def test_a_braced_placeholder_is_accepted_and_every_name_is_placeholder_valid():
    data = _docx(_doc(_p(_r("Jean Tremblay"))))
    result = _ok(data, [Sub("Jean Tremblay", "{{client.nom_complet}}", 1)])
    assert result.substitutions[0].placeholder == "client.nom_complet"
    from utils.docx_fill import PLACEHOLDER_RE
    for name in ("client.nom_complet", "PRIVILÈGE", "FAITS", "a_b.c9"):
        assert tz._normalized_name(name) == name
        assert PLACEHOLDER_RE.fullmatch("{{" + name + "}}").group(1) == name


def test_the_classification_is_reported_and_passthrough_is_warned():
    data = _docx(_doc(_p(_r("Jean Tremblay — objet — FAITS À VENIR"))))
    result = _ok(data, [Sub("Jean Tremblay", "client.nom_complet", 1),
                        Sub("objet", "objet_lettre", 1),
                        Sub("FAITS À VENIR", "FAITS", 1)])
    assert [r.classification for r in result.substitutions] == [
        "auto", "manual", "passthrough"]
    assert any("{{FAITS}} n'est pas un champ que l'application remplit" in w
               for w in result.warnings)


def test_a_case_mismatch_between_heading_and_placeholder_is_warned():
    data = _docx(_doc(_p(_r("JEAN TREMBLAY")), _p(_r("Marie Lavoie"))))
    result = _ok(data, [Sub("JEAN TREMBLAY", "client.nom_complet", 1),
                        Sub("Marie Lavoie", "ADVERSE.NOM_COMPLET", 1)])
    assert any("{{CLIENT.NOM_COMPLET}}" in w for w in result.warnings)
    assert any("{{ADVERSE.NOM_COMPLET}} est en majuscules" in w
               for w in result.warnings)


def test_a_candidate_storm_is_refused_not_ground_through(monkeypatch):
    data = _docx(_doc(_p(_r("a" * 5000))))
    monkeypatch.setattr(tz, "MAX_CANDIDATES", 1000)
    result = tz.analyse(data, [Sub("aaa", "x", None)])
    assert "trop d'occurrences candidates" in result.errors[0]


# ══════════════════════════════════════════════════════════════════════
# 10. Pureté et linéarité
# ══════════════════════════════════════════════════════════════════════


def test_the_engine_is_pure():
    """Standard library, defusedxml, the fill engine and the field catalog —
    nothing else: no Firestore, no Flask, no python-docx/docxtpl, no logging.

    RÉÉCRIT à dessein (revue de l'étape 1) : ``defusedxml.ElementTree`` entre
    dans la liste. L'étape exige que chaque partie réécrite se lise avec
    defusedxml, et la « bonne formation PAR CONSTRUCTION » qui en dispensait
    était fausse — un attribut ``xml:space = "…"`` espacé recevait un double,
    une valeur entre apostrophes cachant un « > » se réécrivait. defusedxml
    est une dépendance directe épinglée, en Python pur, sans aucune E/S (le
    module DAV s'en sert déjà) : la pureté qui compte — ni Firestore, ni
    Flask, ni aller-retour python-docx — tient."""
    source = pathlib.Path(tz.__file__).read_text(encoding="utf-8")
    allowed = {"__future__", "io", "re", "unicodedata", "zipfile", "array",
               "collections", "dataclasses", "typing", "defusedxml.ElementTree",
               "utils.docx_fill", "utils.template_fields"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= allowed
        elif isinstance(node, ast.ImportFrom):
            assert node.module in allowed, node.module


def _dot_outside_class(pattern: str) -> bool:
    i, in_class = 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "]":
                i += 1
        elif c == ".":
            return True
        i += 1
    return False


_REPEATS = {sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT,
            sre_constants.POSSESSIVE_REPEAT}


def _children(op, av):
    if op in _REPEATS:
        return [av[2]]
    if op is sre_constants.SUBPATTERN:
        return [av[3]]
    if op is sre_constants.BRANCH:
        return list(av[1])
    if op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
        return [av[1]]
    if op is sre_constants.ATOMIC_GROUP:
        return [av]
    if op is sre_constants.GROUPREF_EXISTS:
        return [s for s in av[1:] if s is not None]
    return []


def _contains(items, ops) -> bool:
    for op, av in items:
        if op in ops or any(_contains(c, ops) for c in _children(op, av)):
            return True
    return False


def _nested_repetition(items) -> bool:
    """A repeat (``*``, ``+``, ``{m,n}`` with n > 1) whose body holds another
    repeat or an alternation — the shapes that backtrack super-linearly."""
    for op, av in items:
        if op in _REPEATS and av[1] > 1 and _contains(
                av[2], _REPEATS | {sre_constants.BRANCH}):
            return True
        if any(_nested_repetition(c) for c in _children(op, av)):
            return True
    return False


def test_the_detectors_catch_what_they_claim():
    assert _dot_outside_class(r"<a.*?>") and not _dot_outside_class(r"[.]\.")
    assert _nested_repetition(sre_parse.parse(r"(?:a+)*"))
    assert _nested_repetition(sre_parse.parse(r"(?:[^\"<]|\"[^\"]*\")*"))
    assert _nested_repetition(sre_parse.parse(r"(a|ab){2,5}"))
    assert not _nested_repetition(sre_parse.parse(r"<[^<>]*>|[^<]+"))
    assert not _nested_repetition(sre_parse.parse(r"\{\{[^{}]{0,300}\}\}"))
    assert not _nested_repetition(sre_parse.parse(r"(?:x|y)?z+"))


def test_every_pattern_of_the_module_is_linear():
    """DÉRIVÉ : chaque motif compilé du module, y compris ceux qu'on
    ajoutera et ceux qu'il importe du moteur de remplissage — aucun ``.``
    hors classe, aucun DOTALL, aucune répétition imbriquée."""
    patterns = [v for v in vars(tz).values() if isinstance(v, re.Pattern)]
    assert len(patterns) >= 10, "the sweep found too few patterns"
    for pattern in patterns:
        assert not pattern.flags & re.DOTALL, pattern.pattern
        assert not _dot_outside_class(pattern.pattern), pattern.pattern
        assert not _nested_repetition(sre_parse.parse(pattern.pattern)), (
            pattern.pattern)


def test_no_pattern_is_ever_built_from_data():
    """The literals are found with ``str.find``: a run compiles nothing."""
    data = _docx(_doc(SPLIT_BODY), word__footnotes_xml=FOOTNOTES,
                 docProps__core_xml=CORE)
    subs = [Sub("Jean Tremblay", "client.nom_complet", 1), Sub("(a+)+$", "x", None)]
    tz.templatize(data, subs[:1])                 # warm the imports
    real_compile = re.compile
    calls = []

    def spy(pattern, flags=0):
        calls.append(pattern)
        return real_compile(pattern, flags)

    with mock.patch.object(re, "compile", spy):
        assert tz.templatize(data, subs[:1]).ok
        tz.analyse(data, subs)
    assert calls == []


def _body_of_size(target_bytes: int, text: str = "ab ") -> str:
    unit = (f'<w:r><w:rPr><w:b/><w:lang w:val="fr-CA"/></w:rPr>'
            f"<w:t xml:space=\"preserve\">{text}</w:t></w:r>")
    return "<w:p>" + unit * (target_bytes // len(unit)) + "</w:p>"


def test_hostile_unclosed_tags_at_the_part_cap_refuse_in_bounded_time():
    """The shape that cost the fill engine 45 s: unclosed ``<w:t`` / ``<``
    runs, here filling a part to the fill engine's per-part cap."""
    fill = MAX_SINGLE_XML_BYTES - 2000
    hostile = (f"<w:document {NS}><w:body><w:p><w:r><w:t>"
               + "<w:t" * (fill // 8) + "<" * (fill // 2)
               + "</w:t></w:r></w:p></w:body></w:document>")
    data = _docx(hostile)
    start = time.perf_counter()
    result = tz.analyse(data, [Sub("Jean Tremblay", "x", None)])
    elapsed = time.perf_counter() - start
    assert result.blockers[0].code == "malformed_xml"
    assert elapsed < 2.0, f"hostile lex took {elapsed:.2f}s"


def test_text_at_the_visible_cap_with_many_literals_is_bounded():
    words = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
             "eiusmod tempor incididunt ut labore").split()
    paras, total, i = [], 0, 0
    while total < tz.MAX_VISIBLE_CHARS - 200:
        text = " ".join(words[(i + k) % len(words)] for k in range(8)) + " "
        paras.append(f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
        total += len(text)
        i += 1
    data = _docx(_doc(*paras))
    subs = [Sub(f"Prénom{k} Nom{k}", f"x{k}", None)
            for k in range(tz.MAX_SUBSTITUTIONS - 1)] + [Sub("sit amet", "y", None)]
    start = time.perf_counter()
    result = tz.analyse(data, subs)
    elapsed = time.perf_counter() - start
    assert result.ok and result.substitutions[-1].substituted > 1000
    assert elapsed < 5.0, f"analyse at the text cap took {elapsed:.2f}s"


def test_markup_just_under_the_token_cap_templatizes_in_bounded_time():
    """At the token budget — the bound that replaced the fill engine's byte
    caps, under which 100 MB of empty runs cost 23 s — a full templatize
    (lex, match, edit, re-zip, validate_template, recount) stays well under
    gunicorn's 60 s."""
    unit = '<w:r><w:rPr><w:b/></w:rPr><w:tab/></w:r>'      # 7 tokens, no text
    runs = (tz.MAX_TOKENS - 100) // 7
    data = _docx(_doc("<w:p>" + _r("Jean Tremblay") + unit * runs + "</w:p>"))
    start = time.perf_counter()
    result = tz.templatize(data, [Sub("Jean Tremblay", "client.nom_complet", 1)])
    elapsed = time.perf_counter() - start
    assert result.ok, (result.blockers, result.errors)
    assert elapsed < 15.0, f"templatize at the token cap took {elapsed:.2f}s"
