"""Le contrôle des identifiants résiduels d'un .docx (lot 2A, étape T5).

Un gabarit vaut pour TOUT le cabinet : une lettre enregistrée comme gabarit
emporte chaque nom, numéro et adresse du dossier d'où elle vient dans chaque
lettre future. ``utils/docx_leak_scan`` est le contrôle. Ce qui est épinglé :

1. Il lit TOUTES les parties XML — corps, en-têtes, pieds, notes de bas de
   page et de fin, commentaires, propriétés (core/app/custom), customXml, les
   cibles des relations (un ``mailto:`` n'existe que là), et les attributs qui
   nomment une personne (l'auteur d'un commentaire ou d'une révision).
2. Il compare des MOTS, pliés (casse, accents, espaces insécables,
   apostrophes droites ou courbes) et ENTIERS : « Roy » ne se trouve pas dans
   « Royaume », un mot coupé par Word entre deux runs se relit entier, et le
   dernier mot d'un paragraphe ne se colle jamais au premier du suivant.
3. ``accept`` déplace un résidu accepté hors des refus, et une acceptation
   qui ne correspond à rien est signalée (une faute de frappe probable).
4. Il REFUSE plutôt que de conclure sur ce qu'il n'a pas lu : paquet
   illisible, partie trop grosse, texte au-delà du plafond.
5. Linéarité (CWE-1333) : aucun motif du module n'a de ``.`` hors classe ni
   de DOTALL — balayage DÉRIVÉ de chaque motif compilé — et une entrée
   hostile comme une entrée au plafond passent en temps borné.
6. ``scrub_core_properties`` vide les cinq propriétés nommées, laisse chaque
   autre entrée identique à l'octet, et ne réécrit jamais un document propre.

Et ``services/docx_identifiers.dossier_identifiers`` : il construit les
chaînes à chercher et échoue FERMÉ quand une partie liée est illisible.
"""

import io
import os
import re
import sys
import time
import zipfile
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from utils import docx_leak_scan as scan  # noqa: E402
from utils.docx_fill import MAX_SINGLE_XML_BYTES  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    from services import docx_identifiers as ids

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
    '2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/'
    '2001/XMLSchema-instance">'
    "<dc:title>Tremblay c. Lavoie</dc:title>"
    "<dc:subject>Mise en demeure</dc:subject>"
    "<dc:creator>Jean Tremblay</dc:creator>"
    "<cp:keywords>recouvrement</cp:keywords>"
    "<dc:description>Dossier 2026-001</dc:description>"
    "<cp:lastModifiedBy>Marie Lavoie</cp:lastModifiedBy>"
    '<dcterms:created xsi:type="dcterms:W3CDTF">2026-09-01T10:00:00Z'
    "</dcterms:created></cp:coreProperties>"
)


def _para(*runs: str) -> str:
    return "<w:p>" + "".join(
        f'<w:r><w:t xml:space="preserve">{r}</w:t></w:r>' for r in runs
    ) + "</w:p>"


def _docx(parts: dict, *, content_types: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if content_types:
            zf.writestr("[Content_Types].xml",
                        '<?xml version="1.0"?><Types xmlns="http://schemas.'
                        'openxmlformats.org/package/2006/content-types"/>')
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _document(*paras: str) -> str:
    return f"<w:document {W}><w:body>" + "".join(paras) + "</w:body></w:document>"


def _clean_body() -> dict:
    return {"word/document.xml": _document(_para("Madame, Monsieur,"))}


def _entries(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ══════════════════════════════════════════════════════════════════════
# 1. Ce qui est lu
# ══════════════════════════════════════════════════════════════════════


def test_every_text_part_is_read_and_named():
    parts = {
        "word/document.xml": _document(_para("Corps : Jean Tremblay.")),
        "word/header1.xml": f"<w:hdr {W}>{_para('En-tête Tremblay c. Lavoie')}</w:hdr>",
        "word/footer1.xml": f"<w:ftr {W}>{_para('Pied 2026-001')}</w:ftr>",
        "word/footnotes.xml": (f"<w:footnotes {W}><w:footnote w:id=\"1\">"
                               f"{_para('Note : jean.tremblay@example.com')}"
                               "</w:footnote></w:footnotes>"),
        "word/endnotes.xml": (f"<w:endnotes {W}><w:endnote w:id=\"1\">"
                              f"{_para('Fin : (514) 555-1234')}"
                              "</w:endnote></w:endnotes>"),
        "word/comments.xml": (f'<w:comments {W}><w:comment w:id="0" '
                              f'w:author="Marie Lavoie" w:initials="ML">'
                              f"{_para('Vérifier H2X 1Y4')}</w:comment>"
                              "</w:comments>"),
        "docProps/core.xml": CORE,
        "docProps/app.xml": ("<Properties><Company>Béton Nord inc.</Company>"
                             "</Properties>"),
        "docProps/custom.xml": ('<Properties><property name="Client">'
                                "<vt:lpwstr>450 rue Sainte-Catherine Ouest"
                                "</vt:lpwstr></property></Properties>"),
        "customXml/item1.xml": "<root><neq>1234567890</neq></root>",
    }
    result = scan.scan_identifiers(_docx(parts), [
        "Jean Tremblay", "Tremblay c. Lavoie", "2026-001",
        "jean.tremblay@example.com", "(514) 555-1234", "Marie Lavoie",
        "H2X 1Y4", "Béton Nord inc.", "450 rue Sainte-Catherine Ouest",
        "1234567890",
    ])
    found = {r.identifier: r.parts for r in result.residues}
    # the footnote's email CARRIES the name — « jean.tremblay@… » is the words
    # « jean tremblay example com »: over-matching on purpose, a leak all the same
    assert found["Jean Tremblay"] == ("word/document.xml", "word/footnotes.xml",
                                      "docProps/core.xml")
    assert found["Tremblay c. Lavoie"] == ("word/header1.xml", "docProps/core.xml")
    assert found["2026-001"] == ("word/footer1.xml", "docProps/core.xml")
    assert found["jean.tremblay@example.com"] == ("word/footnotes.xml",)
    assert found["(514) 555-1234"] == ("word/endnotes.xml",)
    # the comment's AUTHOR is an attribute; its text is text
    assert "word/comments.xml" in found["Marie Lavoie"]
    assert found["H2X 1Y4"] == ("word/comments.xml",)
    assert found["Béton Nord inc."] == ("docProps/app.xml",)
    assert found["450 rue Sainte-Catherine Ouest"] == ("docProps/custom.xml",)
    assert found["1234567890"] == ("customXml/item1.xml",)
    assert not result.clean
    assert set(result.parts_scanned) >= set(parts)


def test_a_mailto_link_lives_in_the_relationships_and_is_found():
    rels = ('<Relationships><Relationship Id="rId9" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="mailto:jean.tremblay%40example.com" TargetMode="External"/>'
            "</Relationships>")
    parts = {**_clean_body(), "word/_rels/document.xml.rels": rels}
    result = scan.scan_identifiers(_docx(parts), ["jean.tremblay@example.com"])
    assert [r.parts for r in result.residues] == [("word/_rels/document.xml.rels",)]


def test_people_accounts_document_variables_and_control_labels_are_read():
    people = (f'<w15:people xmlns:w15="x" {W}><w15:person w15:author="Jean '
              'Tremblay"><w15:presenceInfo w15:providerId="AD" w15:userId='
              '"S::jean.tremblay@example.com::1"/></w15:person></w15:people>')
    settings = (f'<w:settings {W}><w:docVars><w:docVar w:name="Client" '
                'w:val="Béton Nord inc."/></w:docVars></w:settings>')
    body = _document('<w:sdt><w:sdtPr><w:alias w:val="Marie Lavoie"/>'
                     "</w:sdtPr><w:sdtContent>" + _para("x")
                     + "</w:sdtContent></w:sdt>")
    parts = {"word/document.xml": body, "word/people.xml": people,
             "word/settings.xml": settings}
    result = scan.scan_identifiers(_docx(parts), [
        "Jean Tremblay", "jean.tremblay@example.com", "Béton Nord inc.",
        "Marie Lavoie"])
    found = {r.identifier: r.parts for r in result.residues}
    assert found["Jean Tremblay"] == ("word/people.xml",)
    assert found["jean.tremblay@example.com"] == ("word/people.xml",)
    assert found["Béton Nord inc."] == ("word/settings.xml",)
    assert found["Marie Lavoie"] == ("word/document.xml",)


def test_cdata_and_utf16_parts_are_read():
    custom = ('<?xml version="1.0" encoding="UTF-16"?><Properties><property>'
              "<vt:lpwstr><![CDATA[Jean Tremblay]]></vt:lpwstr></property>"
              "</Properties>").encode("utf-16")
    parts = {**_clean_body(), "docProps/custom.xml": custom}
    result = scan.scan_identifiers(_docx(parts), ["Jean Tremblay"])
    assert [r.parts for r in result.residues] == [("docProps/custom.xml",)]


# ── Revue de T5 : ce que le contrôle ne lisait pas ─────────────────────


def _raw_docx(entries: list[tuple[str, str | bytes]]) -> bytes:
    """An archive written entry by entry, duplicates allowed — the shape a
    hand-built or third-party package can take and Word never writes."""
    import warnings
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)   # « Duplicate name »
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return buf.getvalue()


CT_EMPTY = ('<?xml version="1.0"?><Types xmlns="http://schemas.'
            'openxmlformats.org/package/2006/content-types"/>')


def test_a_duplicated_entry_is_refused_never_half_read():
    """``ZipFile.open(name)`` reads the LAST entry of a name: with two
    ``word/document.xml`` the scan used to read the clean second copy twice
    and pass the package — the first copy, naming the client, unread."""
    data = _raw_docx([
        ("[Content_Types].xml", CT_EMPTY),
        ("word/document.xml", _document(_para("Jean Tremblay"))),
        ("word/document.xml", _document(_para("Madame, Monsieur,"))),
    ])
    with pytest.raises(scan.LeakScanError) as excinfo:
        scan.scan_identifiers(data, ["Jean Tremblay"])
    assert "Tremblay" not in str(excinfo.value)
    with pytest.raises(scan.LeakScanError):
        scan.scrub_core_properties(data)


def test_a_part_declared_xml_by_its_content_type_is_read_whatever_its_name():
    """System.IO.Packaging writes the core properties as a ``….psmdcp``;
    any part may carry an extension its content type is declared for. The
    scan read only names ending in ``.xml``, so a creator named there — or
    a whole part under an Override — was never seen."""
    types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.'
        'org/package/2006/content-types">'
        '<Default Extension="psmdcp" ContentType="application/vnd.openxml'
        'formats-package.core-properties+xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/word/notes%20client.bin" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.'
        'footnotes+xml"/></Types>')
    data = _raw_docx([
        ("[Content_Types].xml", types),
        ("word/document.xml", _document(_para("Madame, Monsieur,"))),
        ("package/services/metadata/core-properties/abc.psmdcp", CORE),
        ("word/notes client.bin", f"<w:footnotes {W}><w:footnote w:id=\"1\">"
                                  f"{_para('Béton Nord inc.')}</w:footnote>"
                                  "</w:footnotes>"),
        ("word/media/image1.png", b"\x89PNG Jean Tremblay"),
    ])
    result = scan.scan_identifiers(data, ["Jean Tremblay", "Béton Nord inc."])
    found = {r.identifier: r.parts for r in result.residues}
    assert found["Jean Tremblay"] == (
        "package/services/metadata/core-properties/abc.psmdcp",)
    assert found["Béton Nord inc."] == ("word/notes client.bin",)
    # an image is not an XML part: never read as text
    assert "word/media/image1.png" not in result.parts_scanned
    # the scrub scrubs only docProps/core.xml — and claims nothing here
    assert scan.scrub_core_properties(data) == (data, ())


def test_values_word_keeps_in_attributes_are_read():
    """A picture's alternative text, a watermark's text, a simple field's
    ``mailto:``, a drop-down's entries, a table's caption: content Word
    keeps in ATTRIBUTES, which the scan used to skip."""
    body = _document(
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Image 1" '
        'descr="Signature de Marie Lavoie" title="Béton Nord"/>'
        "</wp:inline></w:drawing></w:r></w:p>",
        '<w:p><w:fldSimple w:instr=" HYPERLINK &quot;mailto:jean.tremblay'
        '@example.com&quot; "><w:r><w:t>courriel</w:t></w:r></w:fldSimple></w:p>',
        '<w:sdt><w:sdtPr><w:dropDownList><w:listItem w:displayText='
        '"Luce Roy" w:value="1"/></w:dropDownList></w:sdtPr><w:sdtContent>'
        + _para("x") + "</w:sdtContent></w:sdt>",
        '<w:p><w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:ddList>'
        '<w:listEntry w:val="Paul Gagnon"/></w:ddList><w:textInput>'
        '<w:default w:val="Rue Sainte-Catherine"/></w:textInput></w:ffData>'
        "</w:fldChar></w:r></w:p>",
        '<w:tbl><w:tblPr><w:tblCaption w:val="Parties 2026-001"/>'
        "</w:tblPr></w:tbl>",
    )
    header = (f'<w:hdr {W}><w:p><w:r><w:pict><v:shape><v:textpath '
              'string="Projet Côté"/><v:imagedata o:title="Tremblay"/>'
              "</v:shape></w:pict></w:r></w:p></w:hdr>")
    result = scan.scan_identifiers(
        _docx({"word/document.xml": body, "word/header1.xml": header}),
        ["Marie Lavoie", "Béton Nord", "jean.tremblay@example.com",
         "Luce Roy", "Paul Gagnon", "Rue Sainte-Catherine", "2026-001",
         "Projet Côté", "Tremblay"])
    found = {r.identifier: r.parts for r in result.residues}
    for identifier in ("Marie Lavoie", "Béton Nord", "jean.tremblay@example.com",
                       "Luce Roy", "Paul Gagnon", "Rue Sainte-Catherine",
                       "2026-001"):
        assert found.get(identifier) == ("word/document.xml",), identifier
    assert found["Projet Côté"] == ("word/header1.xml",)
    assert "word/header1.xml" in found["Tremblay"]


def test_drawingml_paragraphs_and_chart_points_separate_words():
    """A chart's category labels and a shape's paragraphs are separate
    values: glued, « Tremblay » + « Lavoie » read as one word and neither
    was ever matched."""
    chart = ('<c:chartSpace><c:cat><c:strRef><c:strCache>'
             '<c:pt idx="0"><c:v>Tremblay</c:v></c:pt>'
             '<c:pt idx="1"><c:v>Lavoie</c:v></c:pt>'
             "</c:strCache></c:strRef></c:cat>"
             "<c:title><c:tx><c:rich><a:p><a:r><a:t>Gagnon</a:t></a:r></a:p>"
             "<a:p><a:r><a:t>Côté</a:t></a:r></a:p></c:rich></c:tx></c:title>"
             "</c:chartSpace>")
    parts = {**_clean_body(), "word/charts/chart1.xml": chart}
    result = scan.scan_identifiers(_docx(parts),
                                   ["Tremblay", "Lavoie", "Gagnon", "Côté"])
    assert sorted(r.identifier for r in result.residues) == [
        "Côté", "Gagnon", "Lavoie", "Tremblay"]


# ══════════════════════════════════════════════════════════════════════
# 2. Comment on compare
# ══════════════════════════════════════════════════════════════════════


def test_a_word_split_across_runs_reads_back_whole():
    """Word coupe un mot entre deux runs (correcteur, changement de langue) :
    les balises de run ne séparent pas."""
    body = _document(_para("Me Jean Trem", "blay"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Jean Tremblay"])
    assert [r.identifier for r in result.residues] == ["Jean Tremblay"]


def test_paragraphs_breaks_and_cells_separate_words():
    """Le dernier mot d'un paragraphe ne se colle pas au premier du suivant —
    sinon « Tremblay » + « Montréal » ne formerait qu'un mot, et le nom
    échapperait au contrôle."""
    body = _document(
        _para("Tremblay"), _para("Montréal"),
        "<w:p><w:r><w:t>Roy</w:t><w:br/><w:t>Lavoie</w:t></w:r></w:p>",
        "<w:tbl><w:tr><w:tc>" + _para("Gagnon") + "</w:tc><w:tc>"
        + _para("Côté") + "</w:tc></w:tr></w:tbl>",
    )
    result = scan.scan_identifiers(
        _docx({"word/document.xml": body}),
        ["Tremblay", "Lavoie", "Gagnon", "Côté", "TremblayMontréal",
         "RoyLavoie", "GagnonCôté"])
    assert sorted(r.identifier for r in result.residues) == [
        "Côté", "Gagnon", "Lavoie", "Tremblay"]


def test_folding_case_accents_nbsp_and_apostrophes():
    body = _document(_para("ÉMILE\u00a0TREMBLAY"), _para("Emile Tremblay"),
                     _para("Me O\u2019Brien"), _para("L\u2019Heureux\u202fGagnon"))
    result = scan.scan_identifiers(
        _docx({"word/document.xml": body}),
        ["Émile Tremblay", "O'Brien", "L'Heureux Gagnon"])
    found = {r.identifier: r.count for r in result.residues}
    assert found == {"Émile Tremblay": 2, "O'Brien": 1, "L'Heureux Gagnon": 1}


def test_invisible_characters_never_split_a_name():
    """Revue de T5 — a soft hyphen, a zero-width space, a direction mark or
    a BOM pasted INSIDE a name is drawn as nothing: the page reads
    « Tremblay ». Treated as separators, they cut it into « Trem » +
    « blay » and the name escaped the scan."""
    body = _document(_para("Jean Trem­blay"), _para("Jean Trem​blay"),
                     _para("Jean ‎Tremblay‏"), _para("﻿Jean Tremblay"),
                     _para("Béton⁠Nord"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Jean Tremblay", "BétonNord"])
    found = {r.identifier: r.count for r in result.residues}
    assert found == {"Jean Tremblay": 4, "BétonNord": 1}
    assert scan.fold_key("Trem­blay") == ("tremblay",)


def test_matching_is_whole_word_only():
    body = _document(_para("Le Royaume de Leroy"), _para("M. Roy, avocat"),
                     _para("Tremblayville"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Roy", "Tremblay"])
    assert [(r.identifier, r.count) for r in result.residues] == [("Roy", 1)]


def test_separators_are_not_compared_so_every_phone_spelling_matches():
    body = _document(_para("Tél. 514.555.1234"), _para("ou 514 555-1234"),
                     _para("ou +1 (514) 555-1234"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["(514) 555-1234"])
    assert result.residues[0].count == 3


def test_entities_are_decoded_before_comparing():
    body = _document(_para("Tremblay &amp; Fils"), _para("C&#244;t&#xE9;"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Tremblay & Fils", "Côté"])
    assert sorted(r.identifier for r in result.residues) == [
        "Côté", "Tremblay & Fils"]


def test_counts_are_per_occurrence_across_parts():
    parts = {"word/document.xml": _document(_para("Tremblay Tremblay"),
                                            _para("tremblay")),
             "word/header1.xml": f"<w:hdr {W}>{_para('TREMBLAY')}</w:hdr>"}
    result = scan.scan_identifiers(_docx(parts), ["Tremblay"])
    assert result.residues[0].count == 4
    assert result.residues[0].parts == ("word/document.xml", "word/header1.xml")


def test_duplicate_spellings_fold_to_one_identifier_first_spelling_wins():
    body = _document(_para("Jean Tremblay"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Jean Tremblay", "JEAN TREMBLAY", "jean-tremblay"])
    assert [r.identifier for r in result.residues] == ["Jean Tremblay"]


def test_short_identifiers_are_skipped_and_reported_never_dropped():
    body = _document(_para("Li et Wu"))
    result = scan.scan_identifiers(_docx({"word/document.xml": body}),
                                   ["Li", "Wu", "", "  ", "Roy"])
    assert result.residues == ()
    assert result.skipped == ("Li", "Wu")


def test_a_bare_string_is_refused_never_scanned_letter_by_letter():
    """A string is iterable: « Jean Tremblay » would become thirteen
    one-letter identifiers, every one « too short », and the scan would pass
    the document clean. Refused instead."""
    data = _docx({"word/document.xml": _document(_para("Jean Tremblay"))})
    with pytest.raises(TypeError):
        scan.scan_identifiers(data, "Jean Tremblay")
    with pytest.raises(TypeError):
        scan.scan_identifiers(data, ["Jean Tremblay"], accept="Jean Tremblay")


def test_nothing_found_is_clean():
    result = scan.scan_identifiers(_docx(_clean_body()), ["Jean Tremblay"])
    assert result.clean and result.residues == () and result.accepted == ()


# ══════════════════════════════════════════════════════════════════════
# 3. accept
# ══════════════════════════════════════════════════════════════════════


def test_accepted_residues_move_out_of_the_refusals():
    body = _document(_para("Jean Tremblay, Montréal, Béton Nord"))
    result = scan.scan_identifiers(
        _docx({"word/document.xml": body}),
        ["Jean Tremblay", "Béton Nord"],
        accept=["jean  TREMBLAY"])
    assert [r.identifier for r in result.residues] == ["Béton Nord"]
    assert [r.identifier for r in result.accepted] == ["Jean Tremblay"]
    assert result.unused_accept == ()


def test_an_accept_entry_matching_nothing_found_is_reported():
    """Une acceptation qui ne correspond à aucun résidu trouvé est une faute
    de frappe probable : le juriste croit avoir accepté ce qui ne l'est pas."""
    body = _document(_para("Jean Tremblay"))
    result = scan.scan_identifiers(
        _docx({"word/document.xml": body}), ["Jean Tremblay", "Marie Lavoie"],
        accept=["Jean Tremblai", "Marie Lavoie", "Jean Tremblay"])
    assert result.unused_accept == ("Jean Tremblai", "Marie Lavoie")
    assert result.clean


# ══════════════════════════════════════════════════════════════════════
# 4. Refus plutôt que conclusion partielle
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("data", [
    b"",
    b"not a zip at all",
    b"PK\x03\x04garbage",
])
def test_an_unreadable_package_refuses(data):
    with pytest.raises(scan.LeakScanError):
        scan.scan_identifiers(data, ["Jean Tremblay"])


def test_a_package_without_the_word_structure_refuses():
    data = _docx({"docProps/core.xml": CORE})
    with pytest.raises(scan.LeakScanError):
        scan.scan_identifiers(data, ["Jean Tremblay"])


def test_text_beyond_the_cap_refuses(monkeypatch):
    monkeypatch.setattr(scan, "MAX_SCANNED_CHARS", 1000)
    parts = {"word/document.xml": _document(_para("a " * 400)),
             "word/header1.xml": f"<w:hdr {W}>{_para('b ' * 200)}</w:hdr>"}
    with pytest.raises(scan.LeakScanError) as excinfo:
        scan.scan_identifiers(_docx(parts), ["Jean Tremblay"])
    assert "refuse" in str(excinfo.value)


def test_an_oversized_part_refuses_on_its_real_inflated_size(monkeypatch):
    """The central directory can understate a size; the read is bounded on
    what actually inflates (``_read_entry_bounded``)."""
    import utils.docx_leak_scan as module
    monkeypatch.setattr(module, "MAX_SINGLE_XML_BYTES", 2000)
    parts = {**_clean_body(), "docProps/custom.xml": "<p>" + "x" * 5000 + "</p>"}
    with pytest.raises(scan.LeakScanError):
        scan.scan_identifiers(_docx(parts), ["Jean Tremblay"])


def test_too_many_identifiers_refuses_rather_than_checking_some(monkeypatch):
    monkeypatch.setattr(scan, "MAX_IDENTIFIERS", 3)
    with pytest.raises(scan.LeakScanError):
        scan.scan_identifiers(_docx(_clean_body()),
                              ["Alpha Un", "Beta Deux", "Gamma Trois", "Delta Quatre"])


def test_the_refusal_messages_never_quote_an_identifier(monkeypatch):
    monkeypatch.setattr(scan, "MAX_SCANNED_CHARS", 10)
    body = _document(_para("Jean Tremblay est ici, longuement"))
    with pytest.raises(scan.LeakScanError) as excinfo:
        scan.scan_identifiers(_docx({"word/document.xml": body}), ["Jean Tremblay"])
    assert "Tremblay" not in str(excinfo.value)


# ══════════════════════════════════════════════════════════════════════
# 5. Linéarité
# ══════════════════════════════════════════════════════════════════════


def _dot_outside_class(pattern: str) -> bool:
    """True when *pattern* has an unescaped ``.`` outside a character class."""
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
            if pattern[i + 1:i + 2] == "]":   # a literal « ] » first in a class
                i += 1
        elif c == ".":
            return True
        i += 1
    return False


def test_the_dot_detector_catches_what_it_claims():
    assert _dot_outside_class(r"<a.*?>")
    assert not _dot_outside_class(r"<[.]>")
    assert not _dot_outside_class(r"a\.b")
    assert not _dot_outside_class(r"[^.]+")


def test_every_pattern_of_the_module_is_linear():
    """DÉRIVÉ : chaque motif compilé du module, y compris ceux qu'on
    ajoutera — aucun ``.`` hors classe (il rebalaye jusqu'à la fin de ligne,
    ou de la chaîne sous DOTALL, une fois par position), aucun DOTALL."""
    patterns = [v for v in vars(scan).values() if isinstance(v, re.Pattern)]
    assert len(patterns) >= 8, "the sweep found too few patterns"
    for pattern in patterns:
        assert not pattern.flags & re.DOTALL, pattern.pattern
        assert not _dot_outside_class(pattern.pattern), pattern.pattern


def test_the_per_element_scrub_patterns_are_linear_too():
    """The core scrub builds its element patterns at call time, from a
    declared prefix — the same invariant holds for them."""
    captured = []
    real_compile = re.compile

    def spy(pattern, flags=0):
        captured.append((pattern, flags))
        return real_compile(pattern, flags)

    with mock.patch.object(scan.re, "compile", spy):
        scan.scrub_core_properties(_docx({**_clean_body(), "docProps/core.xml": CORE}))
    assert captured
    for pattern, flags in captured:
        assert not flags & re.DOTALL and not _dot_outside_class(pattern), pattern


def test_hostile_unclosed_tags_are_scanned_in_bounded_time():
    """465 KB of unclosed ``<`` / ``<w:t`` in one part — the shape that cost
    the fill engine 45 s before its patterns were made linear."""
    hostile = ("<w:document><w:body><w:p><w:r><w:t>"
               + "<w:t" * 60000 + "<" * 240000
               + "</w:t></w:r></w:p></w:body></w:document>")
    assert len(hostile) > 465_000
    start = time.perf_counter()
    scan.scan_identifiers(_docx({"word/document.xml": hostile}),
                          ["Jean Tremblay", "Marie Lavoie"])
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"hostile scan took {elapsed:.2f}s — quadratic?"


def test_a_scan_at_the_cap_with_many_identifiers_is_bounded():
    """AT the cap: ~1 000 000 characters of text in ~34 000 paragraphs,
    against 400 identifiers — a large dossier. The cost is one pass per
    identifier over the joined words, all in C."""
    words = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
             "eiusmod tempor incididunt ut labore").split()
    paras, total, i = [], 0, 0
    while total < scan.MAX_SCANNED_CHARS - 200:
        text = " ".join(words[(i + k) % len(words)] for k in range(8)) + " "
        paras.append(_para(text))
        total += len(text) + 2      # <w:p> and </w:p> each read as a space
        i += 1
    data = _docx({"word/document.xml": _document(*paras)})
    identifiers = [f"Prénom{k} Nom{k}" for k in range(398)] + [
        "lorem ipsum", "sit amet"]
    start = time.perf_counter()
    result = scan.scan_identifiers(data, identifiers)
    elapsed = time.perf_counter() - start
    assert result.chars_scanned > scan.MAX_SCANNED_CHARS - 1000
    assert {r.identifier for r in result.residues} == {"lorem ipsum", "sit amet"}
    assert elapsed < 5.0, f"scan at the cap took {elapsed:.2f}s"


# ══════════════════════════════════════════════════════════════════════
# 6. scrub_core_properties
# ══════════════════════════════════════════════════════════════════════


def test_the_scrub_empties_the_five_properties_and_nothing_else():
    original = _docx({**_clean_body(), "docProps/core.xml": CORE,
                      "docProps/app.xml": "<Properties><Company>X</Company></Properties>"})
    scrubbed = scan.scrub_core_properties(original)
    assert set(scrubbed.emptied) == {"dc:title", "dc:subject", "dc:creator",
                                     "cp:lastModifiedBy", "dc:description"}
    before, after = _entries(original), _entries(scrubbed.data)
    assert list(before) == list(after)      # entry order kept
    for name in before:
        if name != "docProps/core.xml":
            assert after[name] == before[name], name    # byte-identical
    core = after["docProps/core.xml"].decode("utf-8")
    for tag in ("dc:title", "dc:subject", "dc:creator", "cp:lastModifiedBy",
                "dc:description"):
        assert f"<{tag}></{tag}>" in core
    # keywords and dates are not the scrub's business
    assert "<cp:keywords>recouvrement</cp:keywords>" in core
    assert "2026-09-01T10:00:00Z" in core
    assert "Tremblay" not in core and "Lavoie" not in core
    # the result is still a clean package for the scan
    assert scan.scan_identifiers(scrubbed.data, ["Jean Tremblay",
                                                 "Marie Lavoie"]).clean


def test_a_clean_document_is_returned_untouched():
    clean_core = CORE
    for tag in ("dc:title", "dc:subject", "dc:creator", "dc:description",
                "cp:lastModifiedBy"):
        clean_core = re.sub(f"<{tag}>[^<]*</{tag}>", f"<{tag}/>", clean_core)
    original = _docx({**_clean_body(), "docProps/core.xml": clean_core})
    scrubbed = scan.scrub_core_properties(original)
    assert scrubbed.emptied == () and scrubbed.data is original
    no_core = _docx(_clean_body())
    assert scan.scrub_core_properties(no_core).data is no_core


def test_the_scrub_follows_the_declared_prefixes():
    """The prefixes are read from the namespace declarations, not assumed."""
    core = CORE.replace("xmlns:dc=", "xmlns:purl=").replace(
        "<dc:", "<purl:").replace("</dc:", "</purl:")
    scrubbed = scan.scrub_core_properties(_docx({**_clean_body(),
                                                 "docProps/core.xml": core}))
    assert "purl:creator" in scrubbed.emptied
    assert "Jean Tremblay" not in _entries(scrubbed.data)["docProps/core.xml"].decode()


def test_the_scrub_refuses_what_it_cannot_empty():
    core = CORE.replace("<dc:creator>Jean Tremblay</dc:creator>",
                        "<dc:creator><x>Jean Tremblay</x></dc:creator>")
    with pytest.raises(scan.LeakScanError):
        scan.scrub_core_properties(_docx({**_clean_body(), "docProps/core.xml": core}))


def test_the_scrub_keeps_a_utf16_part_in_utf16():
    core = CORE.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
    scrubbed = scan.scrub_core_properties(_docx({**_clean_body(),
                                                 "docProps/core.xml": core}))
    raw = _entries(scrubbed.data)["docProps/core.xml"]
    assert raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    assert "Jean Tremblay" not in raw.decode("utf-16")


def test_the_scrub_refuses_an_unreadable_package():
    with pytest.raises(scan.LeakScanError):
        scan.scrub_core_properties(b"PK\x03\x04nope")


# ══════════════════════════════════════════════════════════════════════
# 6 bis. text_residues — a template's NAME (review of lot 2A T9)
# ══════════════════════════════════════════════════════════════════════


def test_a_name_is_matched_by_the_scan_s_own_whole_word_rule():
    """The name prints into every generated document's name: the same
    folding and whole-word rule as the file scan, the same escape hatch."""
    identifiers = ["Jean Tremblay", "Tremblay", "2026-001", "Roy", "ab"]
    found = scan.text_residues("Mise en demeure — TRÉMBLAY 2026 001",
                               identifiers)
    assert found.residues == ("Tremblay", "2026-001")
    assert found.accepted == ()
    # Whole words only: « Roy » is not in « Royaume » ; « ab » too short.
    assert scan.text_residues("Lettre au Royaume abc", identifiers) == (
        scan.TextResidues((), ()))
    accepted = scan.text_residues("Lettre Tremblay", identifiers,
                                  accept=["tremblay"])
    assert accepted == scan.TextResidues((), ("Tremblay",))


def test_text_residues_refuses_rather_than_scanning_a_part():
    with pytest.raises(TypeError):
        scan.text_residues("Tremblay", "Tremblay")
    with pytest.raises(TypeError):
        scan.text_residues(b"Tremblay", ["Tremblay"])
    with pytest.raises(scan.LeakScanError):
        scan.text_residues("x" * (scan.MAX_SCANNED_CHARS + 1), ["Tremblay"])
    with pytest.raises(scan.LeakScanError):
        scan.text_residues("x", [f"nom{i:05d}" for i in
                                 range(scan.MAX_IDENTIFIERS + 1)])


# ══════════════════════════════════════════════════════════════════════
# 7. services/docx_identifiers
# ══════════════════════════════════════════════════════════════════════

C1 = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e61"
C2 = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e62"
AV = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e63"
MD = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e64"
OC = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e65"
FIRM = {"nom": "Me Jason Poirier Lavoie", "organisation": "Poirier Lavoie, avocat",
        "adresse_civique": "1 rue du Cabinet, bureau 2", "code_postal": "H3A 1A1",
        "telephone": "(514) 737-2525", "telecopieur": "", "courriel":
        "reception@poirierlavoie.ca"}

PARTIES = {
    C1: {"id": C1, "type": "individual", "prefix": "M.", "first_name": "Jean",
         "last_name": "Tremblay", "email": "jean.tremblay@example.com",
         "phone_cell": "+15145551234", "address_street": "450 rue Sainte-Catherine Ouest",
         "address_postal_code": "H2X 1Y4", "mandataires": [{"id": MD}]},
    C2: {"id": C2, "type": "organization", "organization_name": "Béton Nord inc.",
         "trade_name": "Béton Nord", "company_neq": "1234567890",
         "work_address_street": ["12 boul. Industriel", "local 3"]},
    # The FIRM's own lawyer, linked as the client's lawyer: his bar number,
    # his personal cell and « Nom Prénom » are not letterhead strings.
    AV: {"id": AV, "type": "individual", "prefix": "Me", "first_name": "Jason",
         "last_name": "Poirier Lavoie", "phone_work": "+15147372525",
         "phone_cell": "+15145550000",
         "email_work": "reception@poirierlavoie.ca", "bar_number": "123456"},
    # Opposing counsel, with a record of his own.
    OC: {"id": OC, "type": "individual", "prefix": "Me", "first_name": "Paul",
         "last_name": "Gagnon", "bar_number": "987654",
         "email_work": "pgagnon@example.com"},
    MD: {"id": MD, "type": "individual", "prefix": "Mme", "first_name": "Luce",
         "last_name": "Roy"},
}
DOSSIER = {
    "id": "d1", "title": "Tremblay c. Béton Nord", "file_number": "2026-001",
    "court_file_number": "500-22-123456-261",
    "clients": [{"id": C1, "name": "M. Jean Tremblay", "roles": ["demandeur"],
                 "avocat_id": AV, "avocat_name": "Me Jason Poirier Lavoie"}],
    "opposing_parties": [{"id": C2, "name": "Béton Nord inc.", "roles": [],
                          "avocat_id": OC, "avocat_name": "Me Paul Gagnon"}],
    "client_ids": [C1], "opposing_party_ids": [C2], "avocat_ids": [AV, OC],
}


def _bulk(store: dict):
    calls = []

    def get_parties_bulk(requested):
        calls.append(list(requested))
        return {i: dict(store[i]) for i in requested if i in store}

    return get_parties_bulk, calls


def test_the_identifiers_cover_the_dossier_its_parties_and_their_mandataires(monkeypatch):
    fake, calls = _bulk(PARTIES)
    monkeypatch.setattr(ids, "get_parties_bulk", fake)
    found = ids.dossier_identifiers(DOSSIER, firm=FIRM)
    keys = {scan.fold_key(f) for f in found}
    for expected in (
        "Tremblay c. Béton Nord", "2026-001", "500-22-123456-261",
        "M. Jean Tremblay", "Jean Tremblay", "Tremblay Jean", "Tremblay",
        "jean.tremblay@example.com", "+15145551234", "5145551234",
        "(514) 555-1234", "450 rue Sainte-Catherine Ouest", "H2X 1Y4", "H2X1Y4",
        "Béton Nord inc.", "Béton Nord", "1234567890",
        "12 boul. Industriel local 3",          # a list component, joined
        "Me Paul Gagnon", "Paul Gagnon",        # opposing counsel
        "Gagnon Paul", "987654", "pgagnon@example.com",
        "Mme Luce Roy", "Luce Roy",             # the client's mandataire
    ):
        assert scan.fold_key(expected) in keys, expected
    # the mandataire was read in a SECOND bulk call, the linked ones first
    assert sorted(calls[0]) == sorted([C1, C2, AV, OC]) and calls[1] == [MD]
    # a three-letter surname alone is too common to check
    assert scan.fold_key("Roy") not in keys


def test_the_firm_s_own_details_are_left_out():
    """The lawyer's own record is linked as a client's lawyer: his name,
    phone and email are the letterhead of EVERY letter."""
    fake, _calls = _bulk(PARTIES)
    with mock.patch.object(ids, "get_parties_bulk", fake):
        found = ids.dossier_identifiers(DOSSIER, firm=FIRM)
    keys = {scan.fold_key(f) for f in found}
    for firm_value in ("Me Jason Poirier Lavoie", "Jason Poirier Lavoie",
                       "Poirier Lavoie", "(514) 737-2525",
                       "reception@poirierlavoie.ca"):
        assert scan.fold_key(firm_value) not in keys, firm_value
    # ...while a value that merely SHARES a word with the firm stays
    assert scan.fold_key("Tremblay c. Béton Nord") in keys


def test_the_firm_s_own_contact_record_contributes_nothing():
    """Revue de T5 — the lawyer's OWN record (linked as the client's lawyer)
    used to leak its bar number, its personal cell and « Poirier Lavoie
    Jason » into the list: not letterhead strings, so the firm exclusion
    missed them, and every procedure whose signature block carries the bar
    number would have been refused. The record is still READ (a missing one
    still refuses), then skipped — recognised by its EXACT full name."""
    fake, calls = _bulk(PARTIES)
    with mock.patch.object(ids, "get_parties_bulk", fake):
        found = ids.dossier_identifiers(DOSSIER, firm=FIRM)
    keys = {scan.fold_key(f) for f in found}
    for own in ("123456", "Poirier Lavoie Jason", "+15145550000",
                "(514) 555-0000"):
        assert scan.fold_key(own) not in keys, own
    assert AV in calls[0]                       # read, never assumed
    # opposing counsel's bar number, a lawyer's too, stays
    assert scan.fold_key("987654") in keys
    # a client merely SHARING the firm lawyer's surname is not the firm
    homonym = {**PARTIES, C1: {**PARTIES[C1], "first_name": "Marc",
                               "last_name": "Poirier Lavoie"}}
    fake, _calls = _bulk(homonym)
    with mock.patch.object(ids, "get_parties_bulk", fake):
        keys = {scan.fold_key(f) for f in ids.dossier_identifiers(DOSSIER, firm=FIRM)}
    assert scan.fold_key("Marc Poirier Lavoie") in keys
    assert scan.fold_key("jean.tremblay@example.com") in keys
    # and a firm record that cannot be read still refuses
    missing = {k: v for k, v in PARTIES.items() if k != AV}
    fake, _calls = _bulk(missing)
    with mock.patch.object(ids, "get_parties_bulk", fake):
        with pytest.raises(ids.IdentifiersUnavailable):
            ids.dossier_identifiers(DOSSIER, firm=FIRM)


def test_the_firm_defaults_to_the_cabinet_profile(monkeypatch):
    fake, _calls = _bulk(PARTIES)
    monkeypatch.setattr(ids, "get_parties_bulk", fake)
    monkeypatch.setattr(ids, "cabinet_dict", lambda: dict(FIRM))
    keys = {scan.fold_key(f) for f in ids.dossier_identifiers(DOSSIER)}
    assert scan.fold_key("Poirier Lavoie") not in keys


def test_the_prejudiciaire_placeholder_is_not_an_identifier(monkeypatch):
    monkeypatch.setattr(ids, "get_parties_bulk", lambda requested: {})
    dossier = {"title": "Consultation", "file_number": "2026-002",
               "court_file_number": "Préjudiciaire"}
    assert ids.dossier_identifiers(dossier, firm=FIRM) == [
        "Consultation", "2026-002"]


@pytest.mark.parametrize("store", [
    {},                                                    # read failure → {}
    {k: v for k, v in PARTIES.items() if k != C2},         # a linked party gone
    {k: v for k, v in PARTIES.items() if k != MD},         # a mandataire gone
])
def test_an_unreadable_party_fails_closed(monkeypatch, store):
    """``get_parties_bulk`` fails OPEN to ``{}`` and omits a missing id; a
    leak scan built on a partial list would pass what it should refuse."""
    fake, _calls = _bulk(store)
    monkeypatch.setattr(ids, "get_parties_bulk", fake)
    with pytest.raises(ids.IdentifiersUnavailable) as excinfo:
        ids.dossier_identifiers(DOSSIER, firm=FIRM)
    assert excinfo.value.missing >= 1
    assert "Tremblay" not in str(excinfo.value)


def test_a_non_dossier_fails_closed():
    with pytest.raises(ids.IdentifiersUnavailable):
        ids.dossier_identifiers(None, firm=FIRM)


def test_the_identifiers_feed_the_scan_end_to_end(monkeypatch):
    fake, _calls = _bulk(PARTIES)
    monkeypatch.setattr(ids, "get_parties_bulk", fake)
    letter = _docx({
        "word/document.xml": _document(
            _para("Me Jason Poirier Lavoie"), _para("(514) 737-2525"),
            _para("Objet : TREMBLAY, Jean — v/réf. 2026-001")),
        "docProps/core.xml": CORE,
    })
    result = scan.scan_identifiers(letter, ids.dossier_identifiers(DOSSIER, firm=FIRM))
    found = {r.identifier for r in result.residues}
    assert {"Tremblay Jean", "2026-001", "Tremblay"} <= found
    assert not any("Poirier" in f or "737" in f for f in found)
