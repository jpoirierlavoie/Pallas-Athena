"""Les générations du connecteur (lot 2A, étape T8) : fill_gabarit et
create_document — les premières écritures qui VERSENT un fichier au dossier,
toujours comme un NOUVEAU document.

Tout passe par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), le faux Cloud Storage qui garde les octets
(``tests/_fake_gcs.py``), les vrais modèles, le vrai service de génération,
le vrai moteur de remplissage et le vrai protocole d'écriture
(``run_write``) ; on relit ce qui est STOCKÉ — l'enregistrement, et le .docx
lui-même, dont chaque partie XML doit s'analyser (defusedxml) : c'est ce que
« Word l'ouvre sans réparation » peut prouver sans Word.

On épingle :
* SPEC H.4 adaptée (sans le clavardage) : dossier REQUIS, créneaux STRICTS
  (jamais le repli silencieux du web), blocs et champs manuels comparés
  EXACTEMENT, un champ que l'application résout jamais « écrasé », « {{ » /
  « }} » refusés, seuls les gabarits ordinaires ;
* les allégations d'un bloc héritent UNE fois de la numérotation Word du
  paragraphe hôte ; un bloc Markdown dans un hôte partagé est rétrogradé et
  le résultat le DIT ;
* create_document « markdown » : le gabarit « Note (impression) » ACTIF
  seulement — jamais le plus récent —, refusé plutôt que stocké plein de
  symboles Markdown ; « copy » : dans le dossier de la source seulement, la
  protection héritée, la source intacte ;
* la validation : un échec APRÈS le versement est « ENREGISTRÉE — NE PAS
  RÉESSAYER » ; un échec après la seule création de « Projets » reste un
  refus, et la reprise réussit ;
* le NEVER « document » : aucune génération ne touche le fichier d'un
  document existant.
"""

import io
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
    import dav.sync as dav_sync  # its db is patched below
    import mcp.endpoint as endpoint
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import doc_template as tpl_model
    from models import document as document_model
    from models import folder as folder_model
    from services import gabarit_champs
    from services import gabarits as gabarit_writer
    from utils import storage_identity

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, write_support)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f'xmlns:w="{W_NS}"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
DOCX_MIME = document_model.EXTENSION_MIME_TYPES[".docx"]
PROJETS = folder_model.system_folder_id("d1", "projets")
_NUMBERED = ('<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="5"/>'
             "</w:numPr></w:pPr>")


def _p(text: str, ppr: str = "") -> str:
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, *, header: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
        if header:
            zf.writestr("word/header1.xml",
                        f'<?xml version="1.0"?><w:hdr {_W}>{header}</w:hdr>')
    return buf.getvalue()


GABARIT = _docx(
    _p("{{dossier.titre}}")
    + _p("{{client.nom_complet}}")
    + _p("{{objet_lettre}}")
    + _p("{{privilège}}")
    + _p("{{FAITS}}", _NUMBERED)
    + _p("{{CONCLUSIONS}}")
    + _p("Voir {{MOTIFS}} ci-dessus.")          # a SHARED host
    + _p("{{civilité}}"),
    header=_p("{{ANNEXE}}"),
)
NOTE_TEMPLATE = _docx(
    _p("{{note.titre}}") + _p("{{note.categorie}}") + _p("{{note.contenu}}")
    + _p("{{dossier.titre}}"))
MD = "## Faits\n\n**Le client** a payé.\n\n| A | B |\n|---|---|\n| 1 | 2 |"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _individual(pid, first, last):
    return {"id": pid, "type": "individual", "contact_role": "client",
            "first_name": first, "last_name": last,
            "address_street": "1 rue A", "address_unit": "",
            "address_city": "Laval", "address_province": "Québec",
            "address_postal_code": "H1A 1A1", "address_country": "Canada"}


def _entry(pid, name, roles):
    return {"id": pid, "name": name, "roles": roles,
            "avocat_id": "", "avocat_name": ""}


@pytest.fixture
def world(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    db.seed("parties/c1", _individual("c1", "Jean", "Tremblay"))
    db.seed("parties/c2", _individual("c2", "Marie", "Lavoie"))
    base = {"status": "actif", "forum_type": "judiciaire", "role": "demandeur",
            "created_at": DT}
    db.seed("dossiers/d1", {
        **base, "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Alpha",
        "clients": [_entry("c1", "Jean Tremblay", ["demandeur"]),
                    _entry("c2", "Marie Lavoie", ["demandeur"])],
        "client_ids": ["c1", "c2"], "opposing_parties": [],
        "opposing_party_ids": []})
    db.seed("dossiers/d2", {
        **base, "id": "d2", "file_number": "2026-002", "title": "Autre",
        "clients": [_entry("c1", "Jean Tremblay", ["demandeur"])],
        "client_ids": ["c1"], "opposing_parties": [], "opposing_party_ids": []})
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                           "parent_folder_id": None, "order": 0,
                           "system_role": "", "etag": "e-f1",
                           "created_at": DT, "updated_at": DT})
    gabarit, errors = tpl_model.create_template(
        io.BytesIO(GABARIT), "lettre.docx", len(GABARIT),
        {"name": "Mise en demeure", "category": "correspondance",
         "kind": "gabarit"}, UID)
    assert errors == [], errors
    note, errors = tpl_model.create_template(
        io.BytesIO(NOTE_TEMPLATE), "note.docx", len(NOTE_TEMPLATE),
        {"name": "Impression", "category": "autre", "kind": "note"}, UID)
    assert errors == [], errors
    _designated, errors = tpl_model.set_active_template(
        note["id"], par="juriste@example.com", expected_etag=None)
    assert errors == [], errors
    return {"db": db, "bucket": bucket, "gabarit": gabarit["id"],
            "note": note["id"]}


def _documents(db) -> dict:
    return db.peek_collection("documents")


def _stored_docx(world, doc_id) -> bytes:
    stored = world["db"].peek(f"documents/{doc_id}")
    return world["bucket"].objects[stored["storage_path"]].data


def _parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _assert_word_valid(data: bytes) -> None:
    """What « opens in Word without repair » can mean without Word: a valid
    archive whose every XML part parses."""
    assert data.startswith(b"PK\x03\x04")
    parts = _parts(data)
    assert "[Content_Types].xml" in parts and "word/document.xml" in parts
    for name, raw in parts.items():
        if name.endswith((".xml", ".rels")):
            ET.fromstring(raw)          # raises on malformed XML


def _paragraphs(data: bytes, part: str = "word/document.xml") -> list:
    root = ET.fromstring(_parts(data)[part])
    return root.iter(f"{{{W_NS}}}p")


def _text(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{{{W_NS}}}t"))


def _fill(world, **over) -> dict:
    args = {"template_id": world["gabarit"], "dossier_id": "d1",
            "client_id": "c1",
            "blocs": [{"nom": "FAITS", "contenu": "Un.\n\nDeux.\n\nTrois."},
                      {"nom": "CONCLUSIONS", "contenu": MD, "markdown": True}],
            "champs_manuels": [{"nom": "objet_lettre", "valeur": "Mise en demeure"}]}
    args.update(over)
    return handlers.fill_gabarit(args)


def _refused(call, args) -> str:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        call(args)
    return str(excinfo.value)


def _nothing_generated(world, before: dict) -> None:
    assert _documents(world["db"]) == before
    assert world["db"].peek(f"folders/{PROJETS}") is None


# ══════════════════════════════════════════════════════════════════════
# 1. fill_gabarit — le parcours complet
# ══════════════════════════════════════════════════════════════════════


def test_fill_gabarit_fills_on_the_server_and_files_a_new_projet(world):
    result = _fill(world)
    doc_id = result["document_id"]
    stored = world["db"].peek(f"documents/{doc_id}")
    assert result["entity"]["id"] == doc_id and result["created"] is True
    assert stored["dossier_id"] == "d1" and stored["folder_id"] == PROJETS
    assert result["folder"] == {"id": PROJETS, "name": "Projets",
                                "system_role": "projets"}
    assert stored["display_name"].startswith("2026-001 - ")
    assert stored["display_name"].endswith(" - Projet Mise en demeure")
    # The template's OWN category — the lawyer's choice, never presumed.
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "juriste"
    assert stored["created_via"] == "mcp"
    assert "par Claude (connecteur)" in stored["genere_depuis"]
    assert stored["storage_path"].startswith(f"users/{UID}/dossiers/d1/")
    assert result["entity"]["etag"] == stored["etag"]
    assert result["gabarit"]["version"] == 1

    data = _stored_docx(world, doc_id)
    _assert_word_valid(data)
    texts = [_text(p) for p in _paragraphs(data)]
    assert "Tremblay c. Alpha" in texts                    # auto, the server's
    assert "Jean Tremblay" in texts                        # the named slot
    assert "Mise en demeure" in texts                      # manual
    assert "{{civilité}}" in texts                          # left for Word
    fields = result["fields"]
    assert fields["auto_resolved"] == 2 and fields["auto_missing"] == []
    assert fields["manual_missing"] == ["privilège"]
    assert fields["blocs_filled"] == ["FAITS", "CONCLUSIONS"]
    assert "civilité" in fields["blocs_left_verbatim"]
    assert "MOTIFS" in fields["blocs_left_verbatim"]


def test_each_allegation_inherits_the_host_numbering_exactly_once(world):
    data = _stored_docx(world, _fill(world)["document_id"])
    numbered = {}
    for p in _paragraphs(data):
        if _text(p) in ("Un.", "Deux.", "Trois."):
            ppr = p.find(f"{{{W_NS}}}pPr")
            nums = list(ppr.iter(f"{{{W_NS}}}numPr")) if ppr is not None else []
            numbered[_text(p)] = [
                n.find(f"{{{W_NS}}}numId").get(f"{{{W_NS}}}val") for n in nums]
    assert numbered == {"Un.": ["5"], "Deux.": ["5"], "Trois.": ["5"]}


def test_a_markdown_bloc_becomes_real_word_formatting(world):
    data = _stored_docx(world, _fill(world)["document_id"])
    xml = _parts(data)["word/document.xml"].decode("utf-8")
    body = " ".join(_text(p) for p in _paragraphs(data))
    assert "Le client" in body and "**" not in body and "##" not in body
    assert "<w:b/>" in xml and "<w:tbl>" in xml        # bold run, real table


def test_a_markdown_bloc_in_a_shared_host_is_demoted_and_said(world):
    result = _fill(world, blocs=[
        {"nom": "MOTIFS", "contenu": "**motif**", "markdown": True}])
    assert result["fields"]["blocs_demoted"] == ["MOTIFS"]
    assert "MOTIFS" not in result["fields"]["blocs_filled"]
    assert any("MOTIFS" in w and "texte brut" in w for w in result["warnings"])
    texts = [_text(p) for p in _paragraphs(_stored_docx(world, result["document_id"]))]
    assert "Voir **motif** ci-dessus." in texts        # sigils visible, valid


def test_a_bloc_the_body_cannot_hold_is_reported_left_not_filled(world):
    """A Markdown bloc placed only in a header stays literal — the result
    says so instead of reporting it filled."""
    result = _fill(world, blocs=[
        {"nom": "ANNEXE", "contenu": "annexe", "markdown": True}])
    assert "ANNEXE" in result["fields"]["blocs_left_verbatim"]
    assert "ANNEXE" not in result["fields"]["blocs_filled"]
    assert any("ANNEXE" in w and "tel(s) quel(s)" in w for w in result["warnings"])
    data = _stored_docx(world, result["document_id"])
    _assert_word_valid(data)


def test_self_numbering_and_single_newlines_are_warned_not_rewritten(world):
    result = _fill(world, blocs=[
        {"nom": "FAITS", "contenu": "1. Un.\n\n2. Deux."},
        {"nom": "MOTIFS", "contenu": "ligne un\nligne deux"}])
    warnings = " ".join(result["warnings"])
    assert "numéro" in warnings and "LIGNE VIDE" in warnings
    texts = [_text(p) for p in _paragraphs(_stored_docx(world, result["document_id"]))]
    assert "1. Un." in texts                               # never rewritten


def test_a_missing_dossier_field_is_reported_to_the_lawyer(world):
    world["db"].external_write("dossiers/d1", {
        **world["db"].peek("dossiers/d1"), "title": ""})
    result = _fill(world)
    assert result["fields"]["auto_missing"] == ["dossier.titre"]
    assert any("CHAMP MANQUANT" in w for w in result["warnings"])


# ══════════════════════════════════════════════════════════════════════
# 2. fill_gabarit — les gardes, une par une, rien d'écrit
# ══════════════════════════════════════════════════════════════════════


def test_no_dossier_no_generation(world):
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit,
                       {"template_id": world["gabarit"], "dossier_id": ""})
    assert "`dossier_id` est requis" in message and "téléchargement" in message
    _nothing_generated(world, before)


@pytest.mark.parametrize("template_id", ["inconnu", "a/b", ""])
def test_an_unknown_template_is_refused_naming_list_templates(world, template_id):
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit,
                       {"template_id": template_id, "dossier_id": "d1"})
    assert "list_templates" in message
    _nothing_generated(world, before)


def test_the_special_kinds_are_filled_by_their_own_flows(world):
    message = _refused(handlers.fill_gabarit,
                       {"template_id": world["note"], "dossier_id": "d1"})
    assert "create_document" in message and "fill_gabarit ne remplit" in message


def test_a_note_honoraires_template_is_pointed_to_create_document_invoice_note(world):
    """Lot 3b (the text step): the note d'honoraires is filed by
    create_document (source invoice_note) as well as by the application's
    button — a refusal naming the button alone was false on this surface."""
    template, errors = tpl_model.create_template(
        io.BytesIO(NOTE_TEMPLATE), "honoraires.docx", len(NOTE_TEMPLATE),
        {"name": "Note d'honoraires", "category": "autre",
         "kind": "note_honoraires"}, UID)
    assert errors == [], errors
    message = _refused(handlers.fill_gabarit,
                       {"template_id": template["id"], "dossier_id": "d1"})
    assert "« invoice_note »" in message and "invoice_id" in message
    assert "fill_gabarit ne remplit" in message


def test_an_unreadable_template_store_is_never_introuvable(world, monkeypatch):
    def _boom(*_a, **_k):
        raise tpl_model.TemplateReadError("t")

    monkeypatch.setattr(tpl_model, "get_template", _boom)
    message = _refused(handlers.fill_gabarit,
                       {"template_id": world["gabarit"], "dossier_id": "d1"})
    assert "réessayez" in message


def test_the_slots_are_strict(world):
    before = _documents(world["db"])
    unknown = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "nope"})
    assert "list_dossiers" in unknown
    foreign = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "x9"})
    assert "`client_id` ne figure pas" in foreign
    ambiguous = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1"})
    assert "`client_id` doit être précisé" in ambiguous
    stranger = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "destinataire_id": "zz"})
    assert "`destinataire_id`" in stranger
    _nothing_generated(world, before)


def _fail_open_bulk(*_a, **_k):
    """``models.partie.get_parties_bulk`` on a Firestore error: ``{}``."""
    return {}


def _fail_open_partie(*_a, **_k):
    """``models.partie.get_partie`` on a Firestore error: ``None``."""
    return None


@pytest.mark.parametrize("reader, broken", [
    ("get_parties_bulk", _fail_open_bulk),
    ("get_partie", _fail_open_partie),
])
def test_a_party_read_that_failed_refuses_never_files_a_blank_intitule(
    world, monkeypatch, reader, broken,
):
    """Critique de complétude du lot 2A (constat des revues de T4 et de T8,
    resté ouvert) : les deux lecteurs de parties échouent OUVERTS, et
    ``fill_gabarit`` versait alors au dossier une procédure dont l'intitulé
    avait perdu les noms et les adresses de ses parties — en l'annonçant
    « données manquantes au dossier ». Le connecteur ne voit pas ce qu'il
    verse : il refuse, rien n'est écrit, « Projets » compris. Échoue sur le
    gestionnaire d'avant (un document est versé)."""
    from services import gabarit_champs as service

    before = _documents(world["db"])
    monkeypatch.setattr(service, reader, broken)
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "blocs": [{"nom": "FAITS", "contenu": "Un."}]})
    assert "n'a pas pu être lue" in message and "réessayez" in message
    assert "Rien n'a été généré" in message
    _nothing_generated(world, before)


def test_the_web_popup_keeps_reading_a_failed_party_as_missing(world, monkeypatch):
    """The fail-closed rule is the CONNECTOR's: the web popup shows every
    resolved value before anything is generated, so its resolution stays
    fail-open, as it always was (resolve_slots' contract, unchanged)."""
    from services import gabarit_champs as service

    real_bulk = service.get_parties_bulk
    monkeypatch.setattr(service, "get_parties_bulk", _fail_open_bulk)
    slots = service.resolve_slots("d1", "c1")
    assert slots.parties == {} and slots.client is not None
    with pytest.raises(service.GenerationRefused) as excinfo:
        service.require_parties_read(slots)
    assert excinfo.value.reason == "parties_unreadable"
    # Every party loaded → no refusal; a dossier-less resolution → none.
    monkeypatch.setattr(service, "get_parties_bulk", real_bulk)
    service.require_parties_read(service.resolve_slots("d1", "c1"))
    service.require_parties_read(service.resolve_slots(""))


@pytest.mark.parametrize("blocs, needle", [
    ([{"nom": "dossier.titre", "contenu": "x"}], "remplit elle-même"),
    ([{"nom": "objet_lettre", "contenu": "x"}], "`champs_manuels`"),
    ([{"nom": "faits", "contenu": "x"}], "la casse compte"),
    ([{"nom": "FAITS", "contenu": "a"}, {"nom": "FAITS", "contenu": "b"}],
     "une seule fois"),
    ([{"nom": "FAITS", "contenu": "voir {{dossier.demandeur}}"}], "« {{ »"),
    ([{"nom": "FAITS", "contenu": "<b>x</b>", "markdown": True}], "chevrons"),
])
def test_a_bloc_is_refused_before_anything_is_written(world, blocs, needle):
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "blocs": blocs})
    assert needle in message
    _nothing_generated(world, before)


def test_a_manual_field_is_checked_against_its_options(world):
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "champs_manuels": [{"nom": "privilège", "valeur": "Au hasard"}]})
    assert "Valeurs admises" in message and "Au hasard" not in message
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "champs_manuels": [{"nom": "objet_lettre", "valeur": "}} fin"}]})
    assert "« }} »" in message
    _nothing_generated(world, before)


def test_the_validator_enforces_the_counts(world):
    schema = tools.TOOLS["fill_gabarit"]["input_schema"]
    args = {"template_id": "t", "dossier_id": "d1",
            "blocs": [{"nom": f"B{i}", "contenu": "x"} for i in range(13)]}
    assert any("at most 12" in e for e in tools.validate_args(schema, args))
    assert tools.validate_args(schema, {**args, "dry_run": True})  # refused


def test_a_missing_template_file_writes_nothing(world, monkeypatch):
    before = _documents(world["db"])
    monkeypatch.setattr(tpl_model, "template_file_bytes", lambda _t: None)
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1"})
    assert "introuvable" in message
    _nothing_generated(world, before)


def test_without_a_storage_identity_nothing_is_written(world, monkeypatch):
    def _none():
        raise storage_identity.StorageIdentityUnavailable()

    monkeypatch.setattr(storage_identity, "owner_uid", _none)
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1"})
    assert "identité de stockage" in message
    _nothing_generated(world, before)


def test_projets_unavailable_refuses_and_never_files_at_the_root(world, monkeypatch):
    monkeypatch.setattr(gabarit_writer, "ensure_system_folder",
                        lambda did, role: (None, [folder_model.READ_ERROR]))
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1"})
    assert folder_model.READ_ERROR in message
    assert _documents(world["db"]) == before


# ══════════════════════════════════════════════════════════════════════
# 3. La validation, et la reprise
# ══════════════════════════════════════════════════════════════════════


def test_an_upload_failing_after_projets_was_created_stays_a_refusal(world, monkeypatch):
    """Regression: « Projets » noted as THE commit turned this into
    « ENREGISTRÉE — NE PAS RÉESSAYER » — and the one safe retry was
    forbidden. The folder is what a retry finds again."""
    real = gabarit_writer.upload_document
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, ["Erreur lors du téléversement. Veuillez réessayer."]
        return real(*a, **k)

    monkeypatch.setattr(gabarit_writer, "upload_document", _flaky)
    args = {"template_id": world["gabarit"], "dossier_id": "d1",
            "client_id": "c1", "idempotency_key": "gabarit-cle-1"}
    with pytest.raises(tools.ToolArgumentError):
        handlers.fill_gabarit(dict(args))
    assert world["db"].peek(f"folders/{PROJETS}") is not None
    assert _documents(world["db"]) == {}
    retried = handlers.fill_gabarit(dict(args))           # same key: allowed
    assert retried["idempotent_replay"] is False
    assert len(_documents(world["db"])) == 1


def test_a_failure_after_the_save_is_reported_committed(world, monkeypatch):
    """Regression: upload_document noted no commit, so a failure after it
    came back as a refusal — and the retry saved a SECOND document."""
    def _boom(_doc):
        raise RuntimeError("payload builder")

    monkeypatch.setattr(handlers, "_document_entity", _boom)
    with pytest.raises(tools.CommittedWriteError) as exc:
        handlers.fill_gabarit({"template_id": world["gabarit"],
                               "dossier_id": "d1", "client_id": "c1"})
    assert exc.value.collection == "documents"
    assert len(_documents(world["db"])) == 1


def test_a_same_key_retry_replays_and_never_saves_twice(world):
    args = {"template_id": world["gabarit"], "dossier_id": "d1",
            "client_id": "c1", "idempotency_key": "gabarit-cle-2"}
    first = handlers.fill_gabarit(dict(args))
    again = handlers.fill_gabarit(dict(args))
    assert again["idempotent_replay"] is True
    assert again["document_id"] == first["document_id"]
    assert len(_documents(world["db"])) == 1


# ══════════════════════════════════════════════════════════════════════
# 4. create_document — source « markdown »
# ══════════════════════════════════════════════════════════════════════


def _markdown(world, **over) -> dict:
    args = {"source": "markdown", "dossier_id": "d1", "title": "Note de recherche",
            "markdown": MD}
    args.update(over)
    return handlers.create_document(args)


def test_markdown_is_printed_on_the_active_note_template(world):
    result = _markdown(world, document_date="2026-02-10")
    stored = world["db"].peek(f"documents/{result['entity']['id']}")
    assert result["source"] == "markdown" and result["template"]["id"] == world["note"]
    assert stored["folder_id"] == PROJETS and result["folder"]["name"] == "Projets"
    assert stored["display_name"].endswith(" - Projet Note de recherche")
    assert stored["category"] == "autre" and stored["category_source"] == "juriste"
    assert stored["document_date"] == datetime(2026, 2, 10, tzinfo=UTC)
    assert stored["genere_depuis"].startswith("Rédigé par Claude (connecteur)")
    data = _stored_docx(world, result["entity"]["id"])
    _assert_word_valid(data)
    texts = [_text(p) for p in _paragraphs(data)]
    assert "Note de recherche" in texts and "Tremblay c. Alpha" in texts
    assert "<w:tbl>" in _parts(data)["word/document.xml"].decode("utf-8")
    assert "{{" not in " ".join(texts)
    assert any("note.categorie" in w for w in result["warnings"])


def test_a_category_claude_gives_is_stored_presumed(world):
    result = _markdown(world, category="correspondance", folder_id="f1")
    stored = world["db"].peek(f"documents/{result['entity']['id']}")
    assert stored["category_source"] == "mcp" and result["entity"]["category_presumee"]
    assert stored["folder_id"] == "f1" and result["folder"]["id"] == "f1"
    at_root = _markdown(world, folder_id="")
    assert world["db"].peek(f"documents/{at_root['entity']['id']}")["folder_id"] is None
    assert at_root["folder"] == {"id": None, "name": "", "system_role": ""}


def test_no_active_note_template_means_no_document_never_the_newest(world):
    """No recency fallback: a note template EXISTS, merely undesignated."""
    world["db"].external_write(f"doc_templates/{world['note']}", {
        k: v for k, v in world["db"].peek(f"doc_templates/{world['note']}").items()
        if k not in tpl_model.ACTIVE_FIELDS})
    before = _documents(world["db"])
    message = _refused(handlers.create_document,
                       {"source": "markdown", "dossier_id": "d1",
                        "title": "T", "markdown": "x"})
    assert "désigné comme actif" in message
    _nothing_generated(world, before)


def test_an_unreadable_designation_is_never_none_designated(world, monkeypatch):
    def _boom(_kind):
        raise tpl_model.TemplateReadError("note")

    monkeypatch.setattr(tpl_model, "get_active_template", _boom)
    message = _refused(handlers.create_document,
                       {"source": "markdown", "dossier_id": "d1",
                        "title": "T", "markdown": "x"})
    assert "réessayez" in message and "désigné comme actif" not in message


def test_a_note_template_without_its_body_field_is_refused(world, monkeypatch):
    template = dict(world["db"].peek(f"doc_templates/{world['note']}"))
    template["placeholders"] = ["note.titre"]
    monkeypatch.setattr(tpl_model, "get_active_template", lambda _k: template)
    message = _refused(handlers.create_document,
                       {"source": "markdown", "dossier_id": "d1",
                        "title": "T", "markdown": "x"})
    assert "{{note.contenu}}" in message


def test_a_body_the_template_cannot_format_is_refused_and_nothing_stored(world, monkeypatch):
    shared = _docx(_p("Texte : {{note.contenu}}"))
    template = dict(world["db"].peek(f"doc_templates/{world['note']}"))
    template["placeholders"] = ["note.contenu"]
    monkeypatch.setattr(tpl_model, "get_active_template", lambda _k: template)
    monkeypatch.setattr(tpl_model, "template_file_bytes", lambda _t: shared)
    before = _documents(world["db"])
    message = _refused(handlers.create_document,
                       {"source": "markdown", "dossier_id": "d1",
                        "title": "T", "markdown": "**x**"})
    assert "seul dans son paragraphe" in message
    _nothing_generated(world, before)


@pytest.mark.parametrize("over, needle", [
    ({"markdown": "Voir <https://canlii.ca/t/x> et a < b et b > c"}, "chevrons"),
    ({"markdown": "Le {{dossier.titre}} est"}, "« {{ »"),
    ({"title": "Titre {{x}}"}, "« {{ »"),
    ({"title": "Ligne un\nLigne deux"}, "une seule ligne"),
    ({"title": "x" * 201}, "dépasse 200"),
    ({"markdown": "a\x07b"}, "contrôle"),
    ({"folder_id": "g9"}, "`folder_id`"),
    ({"dossier_id": "nope"}, "Dossier introuvable"),
])
def test_a_markdown_document_is_refused_before_anything_is_written(world, over, needle):
    before = _documents(world["db"])
    message = _refused(handlers.create_document, {
        "source": "markdown", "dossier_id": "d1", "title": "T", "markdown": "x",
        **over})
    assert needle in message
    _nothing_generated(world, before)


@pytest.mark.parametrize("markdown", [
    r"Voir \{\{dossier.titre\}\} ci-dessus",
    "Voir &#123;&#123;dossier.titre&#125;&#125;",
    "[lien](https://a/&#123;&#123;dossier.titre&#125;&#125;)",
])
def test_a_markdown_body_that_prints_sigils_once_formatted_is_refused(world, markdown):
    """Regression (review of T8): no « {{ » in the raw text, one in the
    formatted text — and the note template's {{dossier.titre}} scalar pass
    printed the dossier's title there. Measured on the old code: the stored
    body read « Voir Tremblay c. Alpha ci-dessus »."""
    before = _documents(world["db"])
    message = _refused(handlers.create_document, {
        "source": "markdown", "dossier_id": "d1", "title": "T",
        "markdown": markdown})
    assert "une fois mis en forme" in message and markdown not in message
    _nothing_generated(world, before)


def test_a_markdown_bloc_that_prints_sigils_once_formatted_is_refused(world):
    before = _documents(world["db"])
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "blocs": [{"nom": "CONCLUSIONS", "markdown": True,
                   "contenu": r"Voir \{\{client.nom_complet\}\}"}]})
    assert "une fois mis en forme" in message
    _nothing_generated(world, before)


def test_a_markdown_text_the_formatter_cannot_convert_is_refused(world):
    """Regression (review of T8): past the formatter's nesting ceiling the
    body was demoted and the refusal told the lawyer to fix a TEMPLATE that
    is fine; a bloc was stored as raw Markdown under the same diagnosis."""
    nested = "\n".join(("  " * i) + "- x" for i in range(40))
    before = _documents(world["db"])
    message = _refused(handlers.create_document, {
        "source": "markdown", "dossier_id": "d1", "title": "T",
        "markdown": nested})
    assert "ne peut pas être mis en forme" in message
    assert "corriger le gabarit" not in message
    message = _refused(handlers.fill_gabarit, {
        "template_id": world["gabarit"], "dossier_id": "d1", "client_id": "c1",
        "blocs": [{"nom": "CONCLUSIONS", "contenu": nested, "markdown": True}]})
    assert "ne peut pas être mis en forme" in message
    _nothing_generated(world, before)


@pytest.mark.parametrize("body, header", [
    (_p("{{note.titre}}"), _p("{{note.contenu}}")),          # header only
    (_p("{{note.titre}}")
     + "<w:p><w:r><w:t>{{note.</w:t></w:r><w:r><w:br/></w:r>"
       "<w:r><w:t>contenu}}</w:t></w:r></w:p>", ""),       # split by Word
])
def test_a_body_the_template_never_prints_is_refused_never_stored_empty(
    world, monkeypatch, body, header,
):
    """Regression (review of T8): {{note.contenu}} outside the body, or
    split by Word, is neither formatted nor demoted — the old code stored a
    document WITHOUT Claude's text and answered « created », no warning."""
    data = _docx(body, header=header)
    template = dict(world["db"].peek(f"doc_templates/{world['note']}"))
    template["placeholders"] = ["note.titre", "note.contenu"]
    monkeypatch.setattr(tpl_model, "get_active_template", lambda _k: template)
    monkeypatch.setattr(tpl_model, "template_file_bytes", lambda _t: data)
    before = _documents(world["db"])
    message = _refused(handlers.create_document,
                       {"source": "markdown", "dossier_id": "d1",
                        "title": "T", "markdown": "Le texte rédigé."})
    assert "CORPS du document" in message
    _nothing_generated(world, before)


def test_an_autolink_alone_is_normalized_not_refused(world):
    result = _markdown(world, markdown="Voir <https://canlii.ca/t/x>.")
    texts = " ".join(_text(p) for p in _paragraphs(
        _stored_docx(world, result["entity"]["id"])))
    assert "https://canlii.ca/t/x" in texts


def test_each_source_takes_only_its_own_arguments(world):
    assert "`document_id`" in _refused(handlers.create_document, {
        "source": "markdown", "dossier_id": "d1", "title": "T",
        "markdown": "x", "document_id": "a"})
    assert "`markdown`" in _refused(handlers.create_document, {
        "source": "copy", "document_id": "a", "markdown": "x"})
    assert "`title`" in _refused(handlers.create_document, {
        "source": "markdown", "dossier_id": "d1", "markdown": "x"})


# ══════════════════════════════════════════════════════════════════════
# 5. create_document — source « copy »
# ══════════════════════════════════════════════════════════════════════


SOURCE_BYTES = _docx(_p("Lettre signée au client"))


def _seed_source(world, doc_id="src", *, dossier="d1", file_type=DOCX_MIME,
                 filename="lettre.docx", **over) -> str:
    path = f"users/{UID}/dossiers/{dossier}/documents/{doc_id}/{filename}"
    world["bucket"].put(path, SOURCE_BYTES, content_type=file_type)
    record = {**document_model._default_doc(), "id": doc_id, "dossier_id": dossier,
              "dossier_file_number": "2026-001", "display_name": "Lettre",
              "filename": filename, "original_filename": filename,
              "file_type": file_type, "file_size": len(SOURCE_BYTES),
              "storage_path": path, "category": "correspondance",
              "category_source": "juriste", "notes_internes": "Mon texte.",
              "created_at": DT, "updated_at": DT, "etag": f"e-{doc_id}"}
    record.update(over)
    world["db"].seed(f"documents/{doc_id}", record)
    return path


def test_a_copy_is_a_new_document_of_the_source_dossier(world):
    _seed_source(world)
    result = handlers.create_document({"source": "copy", "document_id": "src"})
    copy_id = result["entity"]["id"]
    stored = world["db"].peek(f"documents/{copy_id}")
    assert copy_id != "src" and stored["dossier_id"] == "d1"
    assert stored["folder_id"] == PROJETS
    assert stored["display_name"] == "Copie de Lettre"
    assert stored["category_source"] == "juriste"
    assert result["source_document_id"] == "src" and result["template"] is None
    assert result["protection"] is None
    assert any("notes internes" in w for w in result["warnings"])
    assert _stored_docx(world, copy_id) == SOURCE_BYTES


def test_a_copy_inherits_the_protection_and_says_so(world):
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    assert errors == []
    _seed_source(world, analyse=champ, category=champ["nature_detectee"],
                 category_source="analyse")
    result = handlers.create_document({"source": "copy", "document_id": "src",
                                       "display_name": "Copie de travail",
                                       "folder_id": ""})
    stored = world["db"].peek(f"documents/{result['entity']['id']}")
    assert stored["analyse"]["niveau_protection"] == 3
    assert stored["analyse"]["confirme"] is False
    assert stored["category_source"] == "mcp" and stored["folder_id"] is None
    assert stored["display_name"] == "Copie de travail"
    assert result["protection"] == {"niveau_protection": 3,
                                    "label": "Secret professionnel",
                                    "privileges": ["SECRET_PROFESSIONNEL"]}
    assert any("niveau 3" in w and "PRÉSUMÉ" in w for w in result["warnings"])


def test_the_inherited_level_is_never_announced_as_confirmable(world):
    """Regression (review of T8): the warning said « le juriste le confirme
    … dans l'application », but a seed has no sub-nature and the only
    confirm path refuses it (« Aucune analyse à confirmer. »). Claude would
    have sent the lawyer looking for a button that does not exist."""
    champ, _ = document_model._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    _seed_source(world, analyse=champ, category_source="analyse")
    result = handlers.create_document({"source": "copy", "document_id": "src"})
    warning = next(w for w in result["warnings"] if "niveau 3" in w)
    assert "le confirme" not in warning and "qualifiant la copie" in warning
    copy = world["db"].peek(f"documents/{result['entity']['id']}")
    _stored, errors = document_model.confirmer_analyse(copy["id"], par="j")
    assert errors == ["Aucune analyse à confirmer."]


def test_the_texts_say_which_half_of_a_copy_is_presumed(world):
    """Regression (review of T11): INSTRUCTIONS said « the copy keeping the
    source's category and protection level, presumed », and the consent
    « … de l'original, à confirmer » — but a category the LAWYER set stays
    his on the copy (models.document.copy_category_source: no « Confirmer »
    button), and the inherited level has nothing to confirm (the test
    above: it is verified by qualifying the copy). The consent had kept the
    very wording T8's review removed from the tool's warning."""
    _seed_source(world)                       # a category the lawyer set
    result = handlers.create_document({"source": "copy", "document_id": "src"})
    copy = world["db"].peek(f"documents/{result['entity']['id']}")
    assert copy["category_source"] == "juriste"
    text = endpoint.INSTRUCTIONS
    assert "category and protection level, presumed" not in text
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it — and says which half is presumed.
    desc = tools.TOOLS["create_document"]["description"]
    assert ("the source's protection level, PRESUMED, and its category — "
            "PRESUMED unless the lawyer had set it") in desc
    consent = " ".join((_ATHENA / "templates" / "mcp" / "families"
                        / "_files.html").read_text(encoding="utf-8").split())
    assert "de l'original, à confirmer" not in consent
    assert "à vérifier en qualifiant la copie" in consent
    assert "sauf si vous l'aviez posée vous-même" in consent


def test_a_copy_never_leaves_its_dossier(world):
    _seed_source(world)
    before = _documents(world["db"])
    message = _refused(handlers.create_document,
                       {"source": "copy", "document_id": "src", "dossier_id": "d2"})
    assert "gabarit" in message and "reste dans le dossier" in message
    assert _documents(world["db"]) == before


@pytest.mark.parametrize("over, needle", [
    ({"file_type": "application/pdf", "filename": "lettre.pdf"}, "(.docx)"),
])
def test_only_a_docx_is_copied(world, over, needle):
    _seed_source(world, **over)
    assert needle in _refused(handlers.create_document,
                              {"source": "copy", "document_id": "src"})


@pytest.mark.parametrize("doc_id", ["inconnu", "src/analyses/x"])
def test_an_unknown_source_is_refused(world, doc_id):
    _seed_source(world)
    assert "introuvable" in _refused(handlers.create_document,
                                     {"source": "copy", "document_id": doc_id})


def test_a_copy_whose_file_vanished_writes_nothing(world):
    path = _seed_source(world)
    world["bucket"].remove(path)
    before = _documents(world["db"])
    assert "introuvable" in _refused(handlers.create_document,
                                     {"source": "copy", "document_id": "src"})
    assert _documents(world["db"]) == before


# ══════════════════════════════════════════════════════════════════════
# 6. Le NEVER « document » — le comportement qui l'adosse
# ══════════════════════════════════════════════════════════════════════


def test_no_generation_ever_touches_an_existing_document_file(world):
    """What the « document » NEVER promises since T8: a generation or a
    copy is always a NEW document — the record, the object, its generation
    and its bytes of every EXISTING document stay exactly as they were."""
    path = _seed_source(world)
    existing_obj = world["bucket"].objects[path]
    existing_rec = dict(world["db"].peek("documents/src"))
    made = [
        _fill(world)["entity"],
        _markdown(world)["entity"],
        handlers.create_document({"source": "copy", "document_id": "src"})["entity"],
    ]
    assert world["bucket"].objects[path] is existing_obj
    assert existing_obj.data == SOURCE_BYTES
    assert world["db"].peek("documents/src") == existing_rec
    paths = {world["db"].peek(f"documents/{e['id']}")["storage_path"] for e in made}
    assert len(paths) == 3 and path not in paths
    assert "src" not in {e["id"] for e in made}


# ══════════════════════════════════════════════════════════════════════
# 7. Le registre, les plafonds, les textes
# ══════════════════════════════════════════════════════════════════════


def test_the_ceilings_are_the_services():
    assert tools.BLOCS_MAX_ITEMS == gabarit_champs.BLOCS_MAX_ITEMS
    assert tools.CHAMPS_MANUELS_MAX_ITEMS == gabarit_champs.CHAMPS_MANUELS_MAX_ITEMS
    assert tools.BLOC_MAX_CHARS == gabarit_champs.BLOC_MAX_CHARS
    assert tools.BLOCS_TOTAL_MAX_CHARS == gabarit_champs.BLOCS_TOTAL_MAX_CHARS
    assert tools.CHAMP_MANUEL_MAX_CHARS == gabarit_champs.CHAMP_MANUEL_MAX_CHARS
    assert tools.DOCUMENT_TITLE_MAX_CHARS <= document_model.DISPLAY_NAME_MAX
    props = tools.TOOLS["create_document"]["input_schema"]["properties"]
    assert props["category"]["enum"] == list(document_model.CATEGORY_CHOICES)


def test_the_generations_are_creators_not_edits():
    for name in ("fill_gabarit", "create_document"):
        assert name in tools.WRITE_TOOLS and name not in tools.EDIT_TOOLS
        spec = tools.TOOLS[name]
        assert spec["idempotency"] == tools.IDEMPOTENCY_OPTIONAL
        assert "concurrency" not in spec
        props = spec["input_schema"]["properties"]
        assert "expected_etag" not in props and "etag" not in props


def test_no_generation_result_carries_a_value_a_path_or_a_url(world):
    _seed_source(world)
    results = [_fill(world), _markdown(world),
               handlers.create_document({"source": "copy", "document_id": "src"})]
    for result in results:
        text = str(tools._jsonable(result))
        for forbidden in ("storage_path", "users/", "googleapis",
                          "lettre.docx", "Le client a payé", "Un.", "Mon texte"):
            assert forbidden not in text, forbidden


def test_the_instructions_state_the_rules_verbatim():
    """Regression (review of T8 itself): the FILES paragraph goes through
    str.format(), and a single « {{ » there printed « { » — the rule stated
    wrong to every client model.

    REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it: the brace rule is fill_gabarit's own sentence (never
    formatted), and no index line may hold a brace but the one field the
    builder fills."""
    from mcp import disclosure

    text = endpoint.INSTRUCTIONS
    assert "« {{ » or « }} » is refused" in (
        tools.TOOLS["fill_gabarit"]["description"])
    for family in disclosure.FAMILIES:
        line = family.instructions_en.replace("{phase_bulk_max}", "")
        assert "{" not in line and "}" not in line, family.key
    assert "`fill_gabarit`" in text and "`create_document`" in text
    assert "It never changes an existing document's FILE" in text
    assert "in ITS OWN dossier only" in (
        tools.TOOLS["create_document"]["description"])
    assert "« par Claude (connecteur) »" in text
