"""La « gabaritisation » par le connecteur (lot 2B, étape 2 ; décision D5 —
interprétation B) : preview_templatize (LECTURE) et create_template avec
``substitutions``.

Tout passe par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), le faux Cloud Storage qui garde les octets
(``tests/_fake_gcs.py``), les vrais modèles (document, gabarit), le vrai
moteur (``utils/docx_templatize``), le vrai contrôle des identifiants
(``services/docx_identifiers`` + ``utils/docx_leak_scan``) et le vrai
protocole d'écriture (``run_write``) ; on relit ce qui est STOCKÉ.

Épinglé :

1. l'aperçu COMPTE — par substitution et par partie, ce qui reste en place,
   la classe du champ, et les identifiants du dossier (et les textes du
   demandeur) qui RESTERAIENT une fois les substitutions faites — sans rien
   écrire et sans jamais rendre le texte du document ;
2. create_template avec substitutions enregistre une COPIE gabaritisée : les
   champs y sont, le dossier source n'y est plus, le document source est
   intact à l'octet ; le gabarit se REMPLIT pour un autre dossier sans rien
   laisser du premier ;
3. tout ou rien : un décompte différent, une source refusée (modifications
   suivies), un résidu non accepté — rien n'est écrit ;
4. le contrôle porte sur le RÉSULTAT, les textes demandés compris (une
   variante EN MAJUSCULES qui reste est un résidu), avec la seule exception
   dite : un texte contenu dans le NOM d'un champ du résultat ;
5. les refus précèdent tout téléchargement quand la requête seule suffit.
"""

import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timezone
from unittest import mock

import pytest
from defusedxml import ElementTree as ET

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import mcp.endpoint as endpoint
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support  # noqa: F401
    from models import doc_template as tpl_model
    from models import document as document_model
    from services import docx_identifiers
    from utils import storage_identity

from mcp import disclosure  # noqa: E402
from mcp.output_schemas import OUTPUT_SCHEMAS  # noqa: E402
from mcp.tools import ToolArgumentError  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from utils import docx_templatize  # noqa: E402
from utils.docx_fill import fill_docx, validate_template  # noqa: E402
from utils.docx_leak_scan import scan_identifiers  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f'xmlns:w="{W_NS}"'
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_CT = (
    f'{_DECL}<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"><Default Extension="xml" ContentType="application/xml"/>'
    "</Types>"
)
_CORE = (
    f'{_DECL}<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
    'package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:creator>{creator}</dc:creator></cp:coreProperties>"
)
DOCX_MIME = document_model.EXTENSION_MIME_TYPES[".docx"]
BOLD = "<w:rPr><w:b/></w:rPr>"
LANG_EN = '<w:rPr><w:lang w:val="en-CA"/></w:rPr>'
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def _r(text: str, rpr: str = "") -> str:
    space = ' xml:space="preserve"' if text[:1] == " " or text[-1:] == " " else ""
    return f"<w:r>{rpr}<w:t{space}>{text}</w:t></w:r>"


def _p(*runs: str) -> str:
    return "<w:p>" + "".join(runs) + "</w:p>"


def _docx(body: str, *, header: str = "", footer: str = "", creator: str = "",
          footnotes: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f"{_DECL}<w:document {_W}><w:body>{body}<w:sectPr/>"
                    "</w:body></w:document>")
        info = zipfile.ZipInfo("word/media/image1.png", (2024, 5, 6, 7, 8, 10))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, PNG)
        if header:
            zf.writestr("word/header1.xml", f"{_DECL}<w:hdr {_W}>{header}</w:hdr>")
        if footer:
            zf.writestr("word/footer1.xml", f"{_DECL}<w:ftr {_W}>{footer}</w:ftr>")
        if footnotes:
            zf.writestr("word/footnotes.xml",
                        f"{_DECL}<w:footnotes {_W}><w:footnote w:id=\"1\">"
                        f"{footnotes}</w:footnote></w:footnotes>")
        if creator:
            zf.writestr("docProps/core.xml", _CORE.format(creator=creator))
    return buf.getvalue()


# The pilot letter, in miniature: the client's name split by Word across
# runs of different formatting AND language (« Jean » bold | « Tremblay »
# en-CA), in the body and the header; the file number in the body and the
# footer; the client's street and postal code. « Veuillez agréer » is the
# letter's own text, which no output may ever carry.
LETTER = _docx(
    _p(_r("Objet : dossier 2026-001"))
    + _p(_r("Monsieur "), _r("Jean ", BOLD), _r("Tremblay", LANG_EN), _r(","))
    + _p(_r("1 rue A, Laval (Québec) H1A 1A1"))
    + _p(_r("Veuillez agréer nos salutations distinguées.")),
    header=_p(_r("Jean Tremblay")),
    footer=_p(_r("N/Réf. : 2026-001")),
)
FULL = [
    {"literal": "Jean Tremblay", "placeholder": "client.nom_complet",
     "expected_occurrences": 2},
    {"literal": "2026-001", "placeholder": "{{dossier.reference_interne}}",
     "expected_occurrences": 2},
    {"literal": "1 rue A", "placeholder": "client.adresse_civique",
     "expected_occurrences": 1},
    {"literal": "H1A 1A1", "placeholder": "client.code_postal",
     "expected_occurrences": 1},
]


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def world(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    db.seed("parties/c1", {
        "id": "c1", "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay",
        "address_street": "1 rue A", "address_unit": "",
        "address_city": "Laval", "address_province": "Québec",
        "address_postal_code": "H1A 1A1", "address_country": "Canada"})
    db.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Alpha",
        "status": "actif", "forum_type": "judiciaire", "created_at": DT,
        "clients": [{"id": "c1", "name": "Jean Tremblay",
                     "roles": ["demandeur"], "avocat_id": "",
                     "avocat_name": ""}],
        "client_ids": ["c1"], "opposing_parties": [],
        "opposing_party_ids": []})
    db.reset_logs()
    return {"db": db, "bucket": bucket}


def _seed_source(world, data: bytes = LETTER, doc_id: str = "src") -> str:
    path = f"users/{UID}/dossiers/d1/documents/{doc_id}/lettre.docx"
    world["bucket"].put(path, data, content_type=DOCX_MIME)
    world["db"].seed(f"documents/{doc_id}", {
        **document_model._default_doc(), "id": doc_id, "dossier_id": "d1",
        "dossier_file_number": "2026-001",
        "display_name": "Lettre à M. Tremblay",
        "filename": "Lettre_a_M._Tremblay.docx",
        "original_filename": "Lettre à M. Tremblay.docx",
        "file_type": DOCX_MIME, "file_size": len(data), "storage_path": path,
        "category": "correspondance", "category_source": "juriste",
        "created_at": DT, "updated_at": DT, "etag": f"e-{doc_id}"})
    return path


def _preview(subs, **over) -> dict:
    args = {"document_id": "src", "substitutions": subs}
    args.update(over)
    return handlers.preview_templatize(args)


def _create(subs, **over) -> dict:
    args = {"source_document_id": "src", "name": "Lettre type",
            "category": "correspondance", "substitutions": subs}
    args.update(over)
    return handlers.create_template(args)


def _refused(call, args) -> ToolArgumentError:
    with pytest.raises(ToolArgumentError) as excinfo:
        call(args)
    return excinfo.value


def _templates(world) -> dict:
    return world["db"].peek_collection("doc_templates")


def _objects(world) -> dict:
    return {k: v.generation for k, v in world["bucket"].objects.items()}


def _stored_bytes(world, template_id) -> bytes:
    stored = world["db"].peek(f"doc_templates/{template_id}")
    return world["bucket"].objects[stored["storage_path"]].data


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i.filename) for i in zf.infolist()}


def _text_of(data: bytes) -> str:
    """Every word/*.xml text node, joined — what a reader of the file sees."""
    out = []
    for name, raw in _entries(data).items():
        if name.startswith("word/") and name.endswith(".xml"):
            root = ET.fromstring(raw)
            out += [t.text or "" for t in root.iter(f"{{{W_NS}}}t")]
    return " ".join(out)


def _identifiers(world) -> list[str]:
    return docx_identifiers.dossier_identifiers(world["db"].peek("dossiers/d1"))


# ══════════════════════════════════════════════════════════════════════
# 1. preview_templatize — counts, never writes, never the text
# ══════════════════════════════════════════════════════════════════════


def test_the_preview_counts_each_part_and_names_what_would_remain(world):
    _seed_source(world)
    subs = [{"literal": "Jean Tremblay", "placeholder": "client.nom_complet"}]
    result = _preview(subs)
    row = result["substitutions"][0]
    assert row["placeholder"] == "client.nom_complet"
    assert row["classification"] == "auto"
    assert row["substituted"] == 2 and row["expected_occurrences"] is None
    assert row["matches_expected"] is None
    parts = {p["part"]: (p["where"], p["count"]) for p in row["by_part"]}
    # Found WHOLE although Word split it across a bold and an en-CA run.
    assert parts == {"word/document.xml": ("corps", 1),
                     "word/header1.xml": ("en-tête", 1)}
    assert result["source_blockers"] == [] and result["errors"] == []
    assert result["dossier_id"] == "d1"
    # What the dossier would still say once that one substitution applied.
    residues = {r["identifier"]: r for r in result["leak_scan"]["residues"]}
    assert set(residues) == {"2026-001", "1 rue A", "H1A 1A1"}
    assert residues["2026-001"]["where"] == ["corps", "pied de page"]
    assert residues["2026-001"]["count"] == 2
    assert all(r["origin"] == "dossier" for r in residues.values())
    assert result["ready_to_create"] is False
    assert result["result_placeholders"]["auto_count"] == 1
    assert any("« 2026-001 » resterait" in w for w in result["warnings"])


def test_the_full_set_is_ready_and_the_counts_match(world):
    _seed_source(world)
    result = _preview(FULL)
    assert [r["substituted"] for r in result["substitutions"]] == [2, 2, 1, 1]
    assert all(r["matches_expected"] is True for r in result["substitutions"])
    assert result["leak_scan"]["residues"] == []
    assert result["ready_to_create"] is True
    assert result["result_placeholders"] == {
        "placeholder_count": 4, "auto_count": 4, "manual_count": 0,
        "passthrough_count": 0, "fragmented_count": 0}


def test_the_preview_writes_nothing_at_all(world):
    path = _seed_source(world)
    before_doc = dict(world["db"].peek("documents/src"))
    obj = world["bucket"].objects[path]
    objects = _objects(world)
    world["db"].reset_logs()
    _preview(FULL)
    _preview(FULL[:1])
    assert world["db"].commits == []
    assert _objects(world) == objects and _templates(world) == {}
    assert world["db"].peek("documents/src") == before_doc
    assert world["bucket"].objects[path] is obj and obj.data == LETTER


def test_the_preview_never_returns_the_document_s_text(world):
    _seed_source(world)
    payload = json.dumps(tools._jsonable(_preview(FULL[:1])), ensure_ascii=False)
    for text in ("Veuillez agréer", "salutations", "Laval (Québec)", "Objet :",
                 "Monsieur", "N/Réf."):
        assert text not in payload, text
    # The literal itself is named by its index, never echoed in a row.
    assert "Jean Tremblay" not in json.dumps(
        tools._jsonable(_preview(FULL[:1])["substitutions"]), ensure_ascii=False)


def test_a_wrong_expectation_is_reported_not_raised(world):
    _seed_source(world)
    result = _preview([{"literal": "2026-001",
                        "placeholder": "dossier.reference_interne",
                        "expected_occurrences": 3}])
    row = result["substitutions"][0]
    assert row["substituted"] == 2 and row["matches_expected"] is False
    assert any("2 occurrence(s) remplaçable(s), 3 attendue(s)" in e
               for e in result["errors"])
    assert result["ready_to_create"] is False
    # The would-be result is still computed at the COUNTED figure.
    assert result["leak_scan"] is not None


def test_an_all_caps_variant_left_behind_is_a_residue_of_the_literal(world):
    """The engine is case-SENSITIVE, the scan is not: a heading that shouts
    a nickname the builder never generates is still caught, as the
    caller's own literal."""
    letter = _docx(_p(_r("Cher Jojo,")) + _p(_r("JOJO, RAPPEL")))
    _seed_source(world, letter)
    result = _preview([{"literal": "Jojo", "placeholder": "client.prenom"}])
    assert result["substitutions"][0]["substituted"] == 1
    residues = result["leak_scan"]["residues"]
    assert [(r["identifier"], r["origin"], r["count"]) for r in residues] == [
        ("Jojo", "substitution", 1)]
    assert result["ready_to_create"] is False
    # Its own substitution, with an ALL-CAPS name, clears it.
    fixed = _preview([{"literal": "Jojo", "placeholder": "client.prenom"},
                      {"literal": "JOJO", "placeholder": "CLIENT.PRENOM"}])
    assert fixed["leak_scan"]["residues"] == [] and fixed["ready_to_create"]


def test_a_literal_that_matches_nothing_scans_the_file_as_it_is(world):
    _seed_source(world)
    result = _preview([{"literal": "Inexistant", "placeholder": "client.nom"}])
    assert result["substitutions"][0]["substituted"] == 0
    identifiers = {r["identifier"] for r in result["leak_scan"]["residues"]}
    assert {"Jean Tremblay", "2026-001"} <= identifiers
    assert result["ready_to_create"] is False


def test_a_source_with_tracked_changes_is_blocked_by_name(world):
    tracked = _docx(
        _p(_r("Monsieur Jean Tremblay"))
        + '<w:p><w:ins w:id="1" w:author="JPL"><w:r><w:t>ajout</w:t></w:r>'
          "</w:ins></w:p>")
    _seed_source(world, tracked)
    result = _preview(FULL[:1])
    assert [b["code"] for b in result["source_blockers"]] == ["tracked_changes"]
    assert result["source_blockers"][0]["where"] == ["corps"]
    assert "Accepter ou Refuser" in result["source_blockers"][0]["message"]
    assert result["leak_scan"] is None and result["result_placeholders"] is None
    assert result["ready_to_create"] is False


_NS_BOX = (
    f'{_W} '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml" mc:Ignorable="wps"'
)


def _boxed_letter(choice_text: str, fallback_text: str) -> bytes:
    """A letter whose text box holds a Choice and a Fallback copy."""
    box = (
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f"{_DECL}<w:document {_NS_BOX}><w:body>"
                    + _p(_r("Dossier 2026-001 "), box) + "<w:sectPr/>"
                    "</w:body></w:document>")
    return buf.getvalue()


def test_a_text_box_whose_copies_differ_is_reported_under_the_caller_s_number(
        world):
    """The preview's SECOND pass numbers its own, shorter list: its
    « Substitution n° k » must never reach the caller — the first pass
    already named the text box, under the caller's own number."""
    _seed_source(world, _boxed_letter("Jean Tremblay", "J. Tremblay"))
    result = _preview([
        {"literal": "Absent", "placeholder": "client.nom"},       # n° 1: 0 hit
        {"literal": "Jean Tremblay", "placeholder": "client.nom_complet"},
    ])
    row = result["substitutions"][1]
    assert row["substituted"] == 1 and row["fallback_consistent"] is False
    labelled = [e for e in result["errors"] if e.startswith("Substitution n°")]
    assert labelled and all(e.startswith("Substitution n° 2") for e in labelled)
    assert any("n'a pas pu être produit" in e for e in result["errors"])
    assert result["leak_scan"] is None and result["ready_to_create"] is False


def test_a_zero_hit_literal_that_masked_a_shorter_one_is_explained(world):
    """Review of step 2. « Marie‑Ève Tremblay » written with Word's
    NON-BREAKING hyphen is found but never replaced (blocked_by_markup): it
    still MASKS its range, so « Tremblay » counts 1, not 2. create_template
    cannot take the full name (its count is 0), and without it « Tremblay »
    finds 2 — the preview's second pass tripped on exactly that, and said
    only « n'a pas pu être produit … les erreurs ci-dessus », with no error
    above. It now names the real count under the CALLER's number."""
    letter = _docx(
        '<w:p><w:r><w:t>Marie</w:t><w:noBreakHyphen/>'
        '<w:t xml:space="preserve">Ève Tremblay</w:t></w:r></w:p>'
        + _p(_r("Merci, M. Tremblay.")))
    _seed_source(world, letter)
    result = _preview([
        {"literal": "Marie-Ève Tremblay", "placeholder": "client.nom_complet"},
        {"literal": "Inexistant", "placeholder": "client.prenom"},
        {"literal": "Tremblay", "placeholder": "client.nom"}])
    full, absent, short = result["substitutions"]
    assert (full["substituted"], full["blocked_by_markup"]) == (0, 1)
    assert absent["substituted"] == 0 and short["substituted"] == 1
    explained = [e for e in result["errors"]
                 if e.startswith("Substitution n° 3 ({{client.nom}}) : 2 ")]
    # It names the pair that MASKED (n° 1), not the one that found nothing
    # at all (n° 2), and never a number of the second pass's own list.
    assert explained and "substitution(s) n° 1," in explained[0]
    assert "n° 2" not in explained[0]
    assert "relancez l'aperçu" in explained[0]
    assert not any(e.startswith(("Substitution n° 1 ", "Substitution n° 2 "))
                   for e in result["errors"])
    assert result["ready_to_create"] is False
    # Following that advice converges: the short literal alone reads 2, and
    # create_template takes it at 2.
    alone = _preview([{"literal": "Tremblay", "placeholder": "client.nom"}])
    assert alone["substitutions"][0]["substituted"] == 2
    assert alone["errors"] == []


def test_an_invalid_sibling_never_zeroes_a_valid_substitution(world):
    """Review of step 2: one invalid entry used to stop the engine before it
    counted anything — every OTHER row read « substituted: 0 » and the scan
    ran on the untouched source, reporting as « remaining » a number the
    valid pair would have replaced. The valid pairs are counted now; the
    invalid one is still an error, and nothing is ready."""
    _seed_source(world)
    result = _preview([
        {"literal": "Jean Tremblay", "placeholder": "client nom"},   # invalid
        {"literal": "2026-001", "placeholder": "dossier.reference_interne"}])
    bad, good = result["substitutions"]
    assert bad["classification"] is None and bad["substituted"] == 0
    assert good["substituted"] == 2
    assert {p["part"]: p["count"] for p in good["by_part"]} == {
        "word/document.xml": 1, "word/footer1.xml": 1}
    remaining = {r["identifier"] for r in result["leak_scan"]["residues"]}
    assert "2026-001" not in remaining and "Jean Tremblay" in remaining
    assert any(e.startswith("Substitution n° 1 : le nom de champ")
               for e in result["errors"])
    assert result["ready_to_create"] is False


def test_both_copies_of_a_text_box_are_counted_once(world):
    _seed_source(world, _boxed_letter("Jean Tremblay", "Jean Tremblay"))
    row = _preview([{"literal": "Jean Tremblay",
                     "placeholder": "client.nom_complet"}])["substitutions"][0]
    assert (row["substituted"], row["in_alternate_branches"]) == (1, 1)
    assert row["fallback_consistent"] is True


def test_what_is_left_in_place_is_counted_where_it_is(world):
    """REWRITTEN on purpose (review of step 2): the properties are ALWAYS
    emptied first on the templatized path — the preview counts the file as
    create_template stores it, so the author no longer counts as « left in
    place » (it will not be), while the footnote does."""
    letter = _docx(_p(_r("Monsieur Jean Tremblay")),
                   footnotes=_p(_r("Voir Jean Tremblay, 2025.")),
                   creator="Jean Tremblay")
    _seed_source(world, letter)
    result = _preview(FULL[:1])
    row = result["substitutions"][0]
    assert row["substituted"] == 1
    left = {p["where"]: p["count"] for p in row["in_non_target_parts"]}
    assert left == {"notes de bas de page": 1}
    assert result["scrubbed_properties"] == ["dc:creator"]


def test_an_invalid_field_name_and_a_passthrough_name_are_told_apart(world):
    _seed_source(world)
    result = _preview([
        {"literal": "Jean Tremblay", "placeholder": "client nom"},
        {"literal": "2026-001", "placeholder": "client.nom_compet"},
    ])
    bad, typo = result["substitutions"]
    assert bad["classification"] is None and bad["placeholder"] == ""
    assert typo["classification"] == "passthrough"
    assert any("Substitution n° 1" in e for e in result["errors"])
    assert any("n'est pas un champ que l'application remplit" in w
               for w in result["warnings"])


def test_a_message_s_number_is_its_row_s_index_plus_one(world):
    """Completeness review of lot 2B: the rows count from 0 (`index`) while
    every French message numbers from 1 (« Substitution n° 1 »). Both schemas
    now say so; this pins that the two conventions really are offset by one
    — the second entry, invalid, is row index 1 and message « n° 2 », so a
    caller adjusting the pair a message names never edits its neighbour."""
    _seed_source(world)
    result = _preview([
        {"literal": "2026-001", "placeholder": "dossier.reference_interne"},
        {"literal": "Jean Tremblay", "placeholder": "client nom"},   # invalid
    ])
    rows = result["substitutions"]
    assert [r["index"] for r in rows] == [0, 1]
    assert rows[1]["classification"] is None and rows[1]["placeholder"] == ""
    assert any(e.startswith("Substitution n° 2 : le nom de champ")
               for e in result["errors"])
    assert not any(e.startswith("Substitution n° 1 ") for e in result["errors"])
    row_schema = OUTPUT_SCHEMAS["preview_templatize"]["properties"][
        "substitutions"]["items"]
    created = OUTPUT_SCHEMAS["create_template"]["properties"]["templatized"][
        "properties"]["substitutions"]["items"]
    for schema in (row_schema, created):
        assert "« n° 1 » is index 0" in schema["properties"]["index"]["description"]


@pytest.mark.parametrize("args, needle", [
    ({"substitutions": "Jean"}, "doit être une liste"),
    ({"substitutions": []}, "au moins une"),
    ({"substitutions": [{"literal": "Jean Tremblay"}]}, "`placeholder`"),
    ({"substitutions": [{"literal": "Jean Tremblay", "placeholder": "x",
                         "whole_word": False}]}, "ne prend que"),
    ({"substitutions": [{"literal": "Jean", "placeholder": "x",
                         "expected_occurrences": True}]}, "un entier"),
    ({"document_id": "inconnu"}, "`document_id` : document introuvable"),
])
def test_the_preview_s_bad_calls_are_refused(world, args, needle):
    _seed_source(world)
    call = {"document_id": "src", "substitutions": FULL[:1]}
    call.update(args)
    assert needle in str(_refused(handlers.preview_templatize, call))


def test_the_preview_is_checked_against_the_document_s_own_dossier(world):
    """Fail CLOSED: an unreadable party refuses the preview, never a clean
    report."""
    _seed_source(world)
    world["db"].seed("dossiers/d1", {
        **world["db"].peek("dossiers/d1"),
        "clients": [{"id": "fantome", "name": "X", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
        "client_ids": ["fantome"]})
    assert "toutes les parties" in str(_refused(
        handlers.preview_templatize,
        {"document_id": "src", "substitutions": FULL[:1]}))


# ══════════════════════════════════════════════════════════════════════
# 2. create_template with substitutions — the templatized copy
# ══════════════════════════════════════════════════════════════════════


def test_a_letter_becomes_a_template_that_fills_for_another_dossier(world):
    path = _seed_source(world)
    source_rec = dict(world["db"].peek("documents/src"))
    source_obj = world["bucket"].objects[path]
    result = _create(FULL)
    tid = result["entity"]["id"]
    stored_rec = world["db"].peek(f"doc_templates/{tid}")
    stored = _stored_bytes(world, tid)

    # The template: its fields, none of the source dossier.
    assert result["created"] is True and stored_rec["created_via"] == "mcp"
    assert stored_rec["source_dossier_id"] == "d1"
    assert stored_rec["original_filename"] == "Lettre type.docx"
    assert set(validate_template(stored).placeholders) == {
        "client.nom_complet", "dossier.reference_interne",
        "client.adresse_civique", "client.code_postal"}
    assert validate_template(stored).split_run_suspects == []
    assert scan_identifiers(stored, _identifiers(world)).clean
    assert result["entity"]["auto_count"] == 4
    assert result["leak_scan"]["performed"] is True
    assert result["leak_scan"]["accepted_residues"] == []

    # The report: counts and names, never a literal.
    report = result["templatized"]
    assert report["substitution_count"] == 4
    assert report["substituted_total"] == 6
    assert [r["substituted"] for r in report["substitutions"]] == [2, 2, 1, 1]
    assert report["rewritten_parts"] == ["corps", "en-tête", "pied de page"]
    assert "Jean Tremblay" not in json.dumps(report, ensure_ascii=False)

    # Word must reopen it: every part parses, every other entry is the
    # source's byte for byte (the image, the content types).
    source_entries, stored_entries = _entries(LETTER), _entries(stored)
    assert list(stored_entries) == list(source_entries)
    for name, raw in stored_entries.items():
        if name.endswith(".xml"):
            ET.fromstring(raw)
        if name not in ("word/document.xml", "word/header1.xml",
                        "word/footer1.xml"):
            assert raw == source_entries[name], name

    # Filled for ANOTHER dossier: nothing of the first one remains.
    filled = fill_docx(stored, {
        "client.nom_complet": "Marie Roy",
        "dossier.reference_interne": "2026-099",
        "client.adresse_civique": "9 boulevard B",
        "client.code_postal": "J7Z 2Z2",
    })
    text = _text_of(filled)
    assert "Marie Roy" in text and "2026-099" in text and "J7Z 2Z2" in text
    assert scan_identifiers(filled, _identifiers(world)).clean
    assert "Veuillez agréer" in text        # the letter's own text stays

    # The source document is untouched — record, object, bytes.
    assert world["db"].peek("documents/src") == source_rec
    assert world["bucket"].objects[path] is source_obj
    assert source_obj.data == LETTER


def test_one_count_that_differs_refuses_the_whole_call(world):
    _seed_source(world)
    objects = _objects(world)
    subs = [dict(s) for s in FULL]
    subs[1]["expected_occurrences"] = 1
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": subs})
    assert exc.reason == "templatize_refused"
    message = str(exc)
    assert "Substitution n° 2" in message and "rien n'a été écrit" in message
    assert "preview_templatize" in message
    assert "2026-001" not in message and "Jean Tremblay" not in message
    assert _templates(world) == {} and _objects(world) == objects


def test_expected_occurrences_is_required_before_any_download(world, monkeypatch):
    _seed_source(world)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded before the request was judged")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    subs = [{"literal": "Jean Tremblay", "placeholder": "client.nom_complet"}]
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": subs})
    assert "`expected_occurrences` est requis" in str(exc)
    assert "Rien n'a été créé." in str(exc)


@pytest.mark.parametrize("sub, needle", [
    ({"literal": "Jean {Tremblay}", "placeholder": "client.nom",
      "expected_occurrences": 1}, "accolade"),
    ({"literal": "Jean Tremblay", "placeholder": "client nom",
      "expected_occurrences": 1}, "le nom de champ doit s'écrire"),
    ({"literal": " Jean", "placeholder": "client.nom",
      "expected_occurrences": 1}, "ni commencer ni finir par une espace"),
    ({"literal": "Jean Tremblay", "placeholder": "client.nom",
      "expected_occurrences": 0}, "un entier de 1"),
])
def test_an_invalid_substitution_is_refused_before_any_download(
        world, monkeypatch, sub, needle):
    _seed_source(world)
    monkeypatch.setattr(document_model, "get_document_bytes",
                        lambda *a, **k: pytest.fail("downloaded"))
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": [sub]})
    assert exc.reason == "templatize_refused" and needle in str(exc)


def test_a_duplicate_literal_is_refused(world):
    _seed_source(world)
    subs = [FULL[0], {**FULL[0], "placeholder": "client.nom"}]
    assert "même texte à remplacer" in str(_refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": subs}))


def test_what_remains_after_the_substitutions_refuses_naming_it(world):
    _seed_source(world)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": FULL[:2]})
    assert exc.reason == "template_residue"
    message = str(exc)
    assert "Une fois les substitutions faites" in message
    assert "« 1 rue A »" in message and "« H1A 1A1 »" in message
    assert "EN MAJUSCULES" in message and "accept_residual" in message
    assert "Rien n'a été créé." in message
    assert _templates(world) == {}


def test_an_accepted_residue_is_kept_and_echoed(world):
    _seed_source(world)
    result = _create(FULL[:2], accept_residual=["1 rue A", "H1A 1A1"])
    accepted = {r["identifier"] for r in result["leak_scan"]["accepted_residues"]}
    assert accepted == {"1 rue A", "H1A 1A1"}
    assert any("« 1 rue A » reste dans le gabarit" in w
               for w in result["warnings"])
    assert "1 rue A" in _text_of(_stored_bytes(world, result["entity"]["id"]))


def test_a_shouting_variant_left_behind_refuses_as_the_literal(world):
    letter = _docx(_p(_r("Cher Jojo,")) + _p(_r("JOJO, RAPPEL")))
    _seed_source(world, letter)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": [
            {"literal": "Jojo", "placeholder": "client.prenom",
             "expected_occurrences": 1}]})
    assert exc.reason == "template_residue" and "« Jojo »" in str(exc)
    created = _create([
        {"literal": "Jojo", "placeholder": "client.prenom",
         "expected_occurrences": 1},
        {"literal": "JOJO", "placeholder": "CLIENT.PRENOM",
         "expected_occurrences": 1}])
    stored = _stored_bytes(world, created["entity"]["id"])
    assert "JOJO" not in _text_of(stored) and "Jojo" not in _text_of(stored)
    assert "{{CLIENT.PRENOM}}, RAPPEL" in _text_of(stored)


def test_a_literal_inside_a_field_name_is_not_its_own_residue(world):
    """The one carve-out: « Nom complet » replaced by {{client.nom_complet}}
    — the scan reads text, and would otherwise flag the field that replaced
    it."""
    letter = _docx(_p(_r("Nom complet : à remplir")))
    _seed_source(world, letter)
    result = _create([{"literal": "Nom complet",
                       "placeholder": "client.nom_complet",
                       "expected_occurrences": 1}])
    assert result["templatized"]["substituted_total"] == 1
    assert result["leak_scan"]["accepted_residues"] == []


def test_a_source_the_engine_refuses_writes_nothing(world):
    tracked = _docx(
        _p(_r("Monsieur Jean Tremblay"))
        + '<w:p><w:del w:id="1" w:author="JPL"><w:r><w:delText>retiré'
          "</w:delText></w:r></w:del></w:p>")
    _seed_source(world, tracked)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": [
            {"literal": "Jean Tremblay", "placeholder": "client.nom_complet",
             "expected_occurrences": 1}]})
    assert exc.reason == "templatize_refused"
    assert "modifications suivies" in str(exc) and "(corps)" in str(exc)
    assert _templates(world) == {}


def _letter_with_properties(**props: str) -> bytes:
    """A letter whose docProps/core.xml carries the given dc:/cp: values."""
    body = "".join(f"<{k}>{v}</{k}>" for k, v in props.items())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f"{_DECL}<w:document {_W}><w:body>"
                    + _p(_r("Monsieur Jean Tremblay")) + "<w:sectPr/>"
                    "</w:body></w:document>")
        zf.writestr("docProps/core.xml", (
            f'{_DECL}<cp:coreProperties xmlns:cp="http://schemas.'
            'openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"{body}</cp:coreProperties>"))
        info = zipfile.ZipInfo("word/media/image1.png", (2024, 5, 6, 7, 8, 10))
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, PNG)
    return buf.getvalue()


_SUB_JT = [{"literal": "Jean Tremblay", "placeholder": "client.nom_complet",
            "expected_occurrences": 1}]


def test_a_templatized_copy_always_has_its_properties_emptied(world):
    """REWRITTEN on purpose (review of step 2 — the lot's design: the scrub is
    « always applied here »). The scan reads names, numbers and addresses
    only: a SUBJECT that says what the first matter was about is none of
    them, and Word shows it on no page the lawyer rereads — it rode into
    every document generated for another client while the scrub was
    opt-in. Now it never reaches the template, with or without the flag."""
    letter = _letter_with_properties(**{
        "dc:subject": "Fraude fiscale alléguée — mise en demeure",
        "dc:creator": "Jean Tremblay"})
    _seed_source(world, letter)
    result = _create(_SUB_JT)
    assert result["scrubbed_properties"] == ["dc:subject", "dc:creator"]
    assert not any("propriétés du document" in w for w in result["warnings"])
    stored = _entries(_stored_bytes(world, result["entity"]["id"]))
    core = stored["docProps/core.xml"]
    assert b"Fraude" not in core and b"Tremblay" not in core
    # Two rewrites (the scrub, then the substitutions), each through the
    # entries' own ZipInfo: every part parses, the order is the source's,
    # and every entry neither rewrote is the source's byte for byte.
    source = _entries(letter)
    assert list(stored) == list(source)
    for name, raw in stored.items():
        if name.endswith(".xml"):
            ET.fromstring(raw)
        if name not in ("docProps/core.xml", "word/document.xml"):
            assert raw == source[name], name
    assert stored["word/media/image1.png"] == PNG
    # `true` is merely redundant; an explicit `false` is refused BEFORE any
    # download — never silently overridden.
    assert _create(_SUB_JT, scrub_properties=True)["scrubbed_properties"] == [
        "dc:subject", "dc:creator"]


def test_a_scrub_that_cannot_run_is_reported_by_the_preview_refused_by_create(
        world):
    """The preview reports what create_template would refuse — here a
    property holding markup the scrub cannot empty — instead of raising."""
    _seed_source(world, _letter_with_properties(**{
        "dc:title": "<b>Objet</b>"}))
    result = _preview([{"literal": "Jean Tremblay",
                        "placeholder": "client.nom_complet"}])
    assert result["scrubbed_properties"] is None
    assert tools.validate_args(OUTPUT_SCHEMAS["preview_templatize"],
                               tools._jsonable(result)) == []
    assert any("ne peut pas être effacée" in e for e in result["errors"])
    assert result["substitutions"][0]["substituted"] == 1
    assert result["ready_to_create"] is False
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": _SUB_JT})
    assert "ne peut pas être effacée" in str(exc)
    assert _templates(world) == {}


def test_an_explicit_no_scrub_is_refused_on_the_templatized_path(
        world, monkeypatch):
    _seed_source(world)
    monkeypatch.setattr(document_model, "get_document_bytes",
                        lambda *a, **k: pytest.fail("downloaded"))
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": FULL,
        "scrub_properties": False})
    assert "TOUJOURS" in str(exc) and "Rien n'a été créé." in str(exc)
    assert _templates(world) == {}


def test_a_property_the_scrub_cannot_reach_is_refused_with_the_word_remedy(
        world):
    """The residue hint tells the truth: after the scrub (always, here), a
    name left in a property it does not touch (the keywords) is emptied in
    Word — the old hint said « scrub_properties true les efface », which
    had already run and could not."""
    _seed_source(world, _letter_with_properties(**{
        "cp:keywords": "Jean Tremblay"}))
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance", "substitutions": _SUB_JT})
    assert exc.reason == "template_residue"
    assert "Fichier › Informations › Propriétés" in str(exc)
    assert "scrub_properties true" not in str(exc)


def test_the_name_is_checked_against_the_literals_too(world, monkeypatch):
    """Review of step 2: the template's NAME prints into the name of every
    document generated from it. A literal the caller replaces is, by his own
    account, text of the first matter — a nickname the identifier builder
    never generates must not survive in the name either."""
    letter = _docx(_p(_r("Cher Jojo,")))
    _seed_source(world, letter)
    monkeypatch.setattr(document_model, "get_document_bytes",
                        lambda *a, **k: pytest.fail("downloaded"))
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre à Jojo",
        "category": "correspondance", "substitutions": [
            {"literal": "Jojo", "placeholder": "client.prenom",
             "expected_occurrences": 1}]})
    assert exc.reason == "template_residue"
    assert "`name`" in str(exc) and "« Jojo »" in str(exc)
    assert _templates(world) == {}


def test_the_name_is_still_checked_before_any_download(world, monkeypatch):
    _seed_source(world)
    monkeypatch.setattr(document_model, "get_document_bytes",
                        lambda *a, **k: pytest.fail("downloaded"))
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre Tremblay",
        "category": "correspondance", "substitutions": FULL})
    assert exc.reason == "template_residue" and "`name`" in str(exc)


def test_a_same_key_retry_replays_and_never_creates_twice(world):
    _seed_source(world)
    args = {"source_document_id": "src", "name": "Lettre type",
            "category": "correspondance", "substitutions": FULL,
            "idempotency_key": "gabaritiser-lot2b-1"}
    first = handlers.create_template(json.loads(json.dumps(args)))
    again = handlers.create_template(json.loads(json.dumps(args)))
    assert again["idempotent_replay"] is True
    assert again["entity"]["id"] == first["entity"]["id"]
    assert again["templatized"] == first["templatized"]
    assert len(_templates(world)) == 1


def _package_with_app_company(company: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f"{_DECL}<w:document {_W}><w:body>"
                    + _p(_r("Objet : {{objet_lettre}}")) + "<w:sectPr/>"
                    "</w:body></w:document>")
        zf.writestr("docProps/app.xml", (
            f'{_DECL}<Properties xmlns="http://schemas.openxmlformats.org/'
            'officeDocument/2006/extended-properties">'
            f"<Company>{company}</Company></Properties>"))
    return buf.getvalue()


def test_the_scrub_hint_is_offered_only_where_the_scrub_reaches(world):
    """Review of step 2 — the unchanged path (lot 2A) shares the refusal
    text: « scrub_properties true les efface » was offered for ANY residue
    in docProps, including app.xml (which the scrub never touches) and
    after the scrub had already run."""
    _seed_source(world, _package_with_app_company("Jean Tremblay"))
    for extra in ({}, {"scrub_properties": True}):
        exc = _refused(handlers.create_template, {
            "source_document_id": "src", "name": "Lettre type",
            "category": "correspondance", **extra})
        assert exc.reason == "template_residue"
        assert "scrub_properties true" not in str(exc)
        assert "Fichier › Informations › Propriétés" in str(exc)
    # A core property, not yet scrubbed: the flag IS the remedy.
    _seed_source(world, _docx(_p(_r("Objet : {{objet_lettre}}")),
                              creator="Jean Tremblay"), doc_id="src2")
    exc = _refused(handlers.create_template, {
        "source_document_id": "src2", "name": "Lettre type",
        "category": "correspondance"})
    assert "scrub_properties true efface le titre" in str(exc)


def test_without_substitutions_the_file_is_registered_unchanged(world):
    """Interpretation A is untouched: no `substitutions`, no templatizing —
    and the report says so (null)."""
    clean = _docx(_p(_r("Objet : {{objet_lettre}}")))
    _seed_source(world, clean)
    result = handlers.create_template({
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance"})
    assert result["templatized"] is None
    assert _stored_bytes(world, result["entity"]["id"]) == clean


def test_the_engine_s_request_check_is_the_one_the_writes_use():
    """check_request answers exactly what templatize would refuse on the
    request alone — the create handler refuses on it before a download."""
    subs = [docx_templatize.Substitution("Jean {X}", "client.nom", 1),
            docx_templatize.Substitution("Jean Tremblay", "client.nom", None)]
    errors = docx_templatize.check_request(subs)
    assert len(errors) == 2
    assert errors == docx_templatize.templatize(b"", subs).errors
    assert docx_templatize.check_request(subs[1:], require_expected=False) == ()


# ══════════════════════════════════════════════════════════════════════
# 3. The registry and the texts
# ══════════════════════════════════════════════════════════════════════


def test_the_registry_declares_a_read_and_a_create():
    assert "preview_templatize" not in tools.WRITE_TOOLS
    assert "create_template" in tools.WRITE_TOOLS
    assert "create_template" not in tools.EDIT_TOOLS
    preview = tools.TOOLS["preview_templatize"]
    assert preview["input_schema"]["required"] == ["document_id", "substitutions"]
    assert "scope" not in preview or preview["scope"] != tools.SCOPE_WRITE
    create = tools.TOOLS["create_template"]["input_schema"]
    assert create["required"] == ["source_document_id", "name", "category"]
    for schema, required in (
            (preview["input_schema"]["properties"]["substitutions"],
             ["literal", "placeholder"]),
            (create["properties"]["substitutions"],
             ["literal", "placeholder", "expected_occurrences"])):
        assert schema["maxItems"] == docx_templatize.MAX_SUBSTITUTIONS
        items = schema["items"]
        assert items["required"] == required
        assert items["additionalProperties"] is False
        props = items["properties"]
        assert props["literal"]["minLength"] == docx_templatize.MIN_LITERAL_CHARS
        assert props["literal"]["maxLength"] == docx_templatize.MAX_LITERAL_CHARS
        assert (props["expected_occurrences"]["maximum"]
                == docx_templatize.MAX_EXPECTED_OCCURRENCES)
        assert all(p.get("description") for p in props.values())
    # The endpoint's validator refuses what the handler also refuses.
    bad = {"source_document_id": "src", "name": "L", "category": "autre",
           "substitutions": [{"literal": "Jean Tremblay", "placeholder": "x"}]}
    assert tools.validate_args(create, bad)
    extra = {"document_id": "src", "substitutions": [
        {"literal": "Jean Tremblay", "placeholder": "x", "whole_word": True}]}
    assert tools.validate_args(preview["input_schema"], extra)
    # The preview ALWAYS counts the file with its properties emptied, as
    # create_template stores a templatized copy: no flag to pass, and one
    # passed is refused rather than silently ignored.
    assert "scrub_properties" not in preview["input_schema"]["properties"]
    assert tools.validate_args(preview["input_schema"], {
        "document_id": "src", "substitutions": FULL[:1],
        "scrub_properties": False})


def test_the_preview_is_advertised_read_only(monkeypatch):
    monkeypatch.setattr(tools, "write_enabled", lambda: True)
    descriptor = next(d for d in tools.list_tool_descriptors(None)
                      if d["name"] == "preview_templatize")
    assert descriptor["annotations"]["readOnlyHint"] is True
    assert descriptor["outputSchema"] is OUTPUT_SCHEMAS["preview_templatize"]


def test_the_texts_name_the_workflow():
    text = endpoint.INSTRUCTIONS
    assert "`preview_templatize`" in text and "`substitutions`" in text
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it: the case rule is the preview's, the field syntax
    # create_template's — neither goes through str.format any more.
    assert "ALL-CAPS variant of a name needs its own substitution" in (
        tools.TOOLS["preview_templatize"]["description"])
    assert "each literal becomes its {{field}}" in (
        tools.TOOLS["create_template"]["description"])
    templates = next(f for f in disclosure.FAMILIES if f.key == "templates")
    assert "transformé en gabarit" in templates.checkbox_summary_fr
    consent = " ".join((_ATHENA / "templates" / "mcp" / "families"
                        / "_templates.html").read_text(encoding="utf-8").split())
    assert "<strong>transformé en gabarit</strong>" in consent
    assert "relisez un gabarit transformé" in consent
    assert "{{" not in consent          # Jinja would read it as an expression
    desc = tools.TOOLS["preview_templatize"]["description"]
    assert "CASE-SENSITIVE" in desc and "Writes nothing" in desc
    assert "preview_templatize" in tools.TOOLS["create_template"]["description"]
    # Completeness review of lot 2B: the upload ticket files a gabarit
    # UNtemplatized (finalize_upload takes no substitutions), so the route
    # to templatize an outside letter is named — and it exists: a ticket of
    # purpose « document » files it in its dossier, where the two tools
    # below read it.
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the route is
    # create_template's own sentence now — where a caller about to
    # templatize reads it — since the TEMPLATES paragraph became an index
    # line.
    assert ("An OUTSIDE .docx is templatized only once filed as a document "
            "of its dossier (begin_upload purpose document): purpose gabarit "
            "never templatizes") in tools.TOOLS["create_template"]["description"]
    assert "substitutions" not in tools.TOOLS["finalize_upload"]["input_schema"][
        "properties"]
    assert "substitutions" not in tools.TOOLS["begin_upload"]["input_schema"][
        "properties"]
    purposes = tools.TOOLS["begin_upload"]["input_schema"]["properties"][
        "purpose"]["enum"]
    assert "document" in purposes


# ══════════════════════════════════════════════════════════════════════
# 4. Through the real endpoint — a read-only grant previews, never creates
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def read_only_client(world, monkeypatch):
    from datetime import timedelta

    import mcp.bearer as bearer
    import mcp.store as store
    from tests.test_mcp_jsonrpc import _make_app

    bearer.reset_brake_state()
    token = {"token_type": "access", "client_id": "client-1",
             "scope": "athena:read", "resource": None, "family_id": "fam-1",
             "revoked": False,
             "expire_at": datetime.now(UTC) + timedelta(hours=1)}
    monkeypatch.setattr(store, "get_token", lambda h: dict(token))
    monkeypatch.setattr(store, "stamp_token_last_used", lambda h: None)
    yield _make_app().test_client()
    bearer.reset_brake_state()


def _rpc(cl, method, params=None):
    return cl.post("/mcp", data=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}),
        content_type="application/json",
        headers={"Authorization": "Bearer test-token",
                 "MCP-Protocol-Version": "2025-06-18"})


def test_a_read_only_grant_previews_through_the_endpoint(world, read_only_client):
    _seed_source(world)
    listed = {t["name"] for t in _rpc(read_only_client, "tools/list")
              .get_json()["result"]["tools"]}
    assert "preview_templatize" in listed and "create_template" not in listed
    result = _rpc(read_only_client, "tools/call", {
        "name": "preview_templatize",
        "arguments": {"document_id": "src", "substitutions": FULL}},
    ).get_json()["result"]
    assert result["isError"] is False
    content = result["structuredContent"]
    assert tools.validate_args(OUTPUT_SCHEMAS["preview_templatize"], content) == []
    assert content["ready_to_create"] is True
    # The schema refuses what the handler refuses: an unknown key per item.
    refused = _rpc(read_only_client, "tools/call", {
        "name": "preview_templatize",
        "arguments": {"document_id": "src", "substitutions": [
            {"literal": "Jean Tremblay", "placeholder": "x", "whole_word": 1}]}},
    ).get_json()
    assert "error" in refused or refused["result"]["isError"] is True
    # And the write stays out of reach of that grant.
    denied = _rpc(read_only_client, "tools/call", {
        "name": "create_template",
        "arguments": {"source_document_id": "src", "name": "Lettre type",
                      "category": "correspondance", "substitutions": FULL}})
    assert denied.status_code == 403
    assert _templates(world) == {}
