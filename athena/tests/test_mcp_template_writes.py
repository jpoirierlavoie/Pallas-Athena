"""Les gabarits écrits par le connecteur depuis un document du dossier
(lot 2A, étape T10 ; décisions D5 — interprétation A — et D11) :
create_template et update_template.

Tout passe par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), le faux Cloud Storage qui garde les octets et
REFUSE ce que le service refuse (``tests/_fake_gcs.py``), les vrais modèles
(document, gabarit), le vrai contrôle des identifiants
(``services/docx_identifiers`` + ``utils/docx_leak_scan``) et le vrai
protocole d'écriture (``run_write``) ; on relit ce qui est STOCKÉ — le
gabarit, ses versions, ses objets, et le document source.

Épinglé :

1. create_template enregistre un .docx DÉJÀ versé à un dossier comme
   NOUVEAU gabarit, ses octets inchangés (hors l'effacement demandé des
   propriétés), sous un nom de fichier NEUTRE — jamais celui du document ;
   le contrôle des identifiants porte TOUJOURS sur le dossier du document
   (fermé à l'échec), sur le fichier ET sur le nom ; chaque résidu accepté
   est rendu, nommé ; un type spécial n'est jamais désigné actif ;
2. update_template corrige les métadonnées OU installe une NOUVELLE
   version du fichier depuis un document (expected_version exigée), la
   précédente conservée ; le type du gabarit actif ne change pas ; la
   désignation n'est jamais touchée ;
3. les refus n'écrivent rien — ni gabarit, ni version, ni objet ; le
   document source n'est jamais modifié (NEVER « document »).
"""

import hashlib
import io
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timezone
from unittest import mock

import pytest

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
    from utils import storage_identity

ToolArgumentError = tools.ToolArgumentError
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
_CORE = (
    '<?xml version="1.0" encoding="UTF-8"?><cp:coreProperties '
    'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
    'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<dc:creator>{creator}</dc:creator></cp:coreProperties>"
)
DOCX_MIME = document_model.EXTENSION_MIME_TYPES[".docx"]
TEMPLATE_KEYS = {"name", "description", "category", "kind"}


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _docx(body: str, *, creator: str = "", header: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
        if header:
            zf.writestr("word/header1.xml",
                        f'<?xml version="1.0"?><w:hdr {_W}>{header}</w:hdr>')
        if creator:
            zf.writestr("docProps/core.xml", _CORE.format(creator=creator))
    return buf.getvalue()


CLEAN = _docx(_p("Objet : {{objet_lettre}}") + _p("{{dossier.titre}}")
              + _p("{{FAITS}}"))
LEAKY = _docx(_p("Monsieur Jean Tremblay, {{objet_lettre}}"),
              header=_p("Dossier 2026-001"))
V2 = _docx(_p("Deuxième mouture : {{objet_lettre}}"))


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


@pytest.fixture
def world(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)
    db.seed("parties/c1", _individual("c1", "Jean", "Tremblay"))
    db.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Alpha",
        "status": "actif", "forum_type": "judiciaire", "created_at": DT,
        "clients": [{"id": "c1", "name": "Jean Tremblay",
                     "roles": ["demandeur"], "avocat_id": "",
                     "avocat_name": ""}],
        "client_ids": ["c1"], "opposing_parties": [],
        "opposing_party_ids": []})
    existing = _docx(_p("Version un : {{objet_lettre}}") + _p("{{FAITS}}"))
    tpl, errors = tpl_model.create_template(
        io.BytesIO(existing), "base.docx", len(existing),
        {"name": "Lettre de base", "category": "correspondance",
         "kind": "gabarit"}, UID)
    assert errors == [], errors
    db.reset_logs()
    return {"db": db, "bucket": bucket, "template": tpl["id"]}


def _seed_source(world, doc_id="src", data=CLEAN, *, dossier="d1",
                 file_type=DOCX_MIME, **over) -> str:
    path = f"users/{UID}/dossiers/{dossier}/documents/{doc_id}/lettre.docx"
    world["bucket"].put(path, data, content_type=file_type)
    record = {**document_model._default_doc(), "id": doc_id,
              "dossier_id": dossier, "dossier_file_number": "2026-001",
              "display_name": "Lettre à M. Tremblay",
              "filename": "Lettre_a_M._Tremblay.docx",
              "original_filename": "Lettre à M. Tremblay.docx",
              "file_type": file_type, "file_size": len(data),
              "storage_path": path, "category": "correspondance",
              "category_source": "juriste", "created_at": DT,
              "updated_at": DT, "etag": f"e-{doc_id}"}
    record.update(over)
    world["db"].seed(f"documents/{doc_id}", record)
    return path


def _create(**over) -> dict:
    args = {"source_document_id": "src", "name": "Lettre type",
            "category": "correspondance"}
    args.update(over)
    return handlers.create_template(args)


def _templates(world) -> dict:
    return world["db"].peek_collection("doc_templates")


def _objects(world) -> dict:
    return {k: v.generation for k, v in world["bucket"].objects.items()}


def _refused(call, args) -> ToolArgumentError:
    with pytest.raises(ToolArgumentError) as excinfo:
        call(args)
    return excinfo.value


def _stored_bytes(world, template_id) -> bytes:
    stored = world["db"].peek(f"doc_templates/{template_id}")
    return world["bucket"].objects[stored["storage_path"]].data


# ══════════════════════════════════════════════════════════════════════
# 1. create_template
# ══════════════════════════════════════════════════════════════════════


def test_a_stored_docx_becomes_a_new_template_its_bytes_unchanged(world):
    path = _seed_source(world)
    source_rec = dict(world["db"].peek("documents/src"))
    source_obj = world["bucket"].objects[path]
    result = _create(description="Pour les mises en demeure.")
    tid = result["entity"]["id"]
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert result["created"] is True and result["entity_type"] == "template"
    assert stored["name"] == "Lettre type"
    assert stored["category"] == "correspondance" and stored["kind"] == "gabarit"
    assert stored["description"] == "Pour les mises en demeure."
    assert stored["created_via"] == "mcp" and stored["version"] == 1
    assert _stored_bytes(world, tid) == CLEAN          # unchanged
    assert world["db"].peek(f"doc_templates/{tid}/versions/1") is not None
    # The neutral file name: the template's, never the document's.
    assert stored["original_filename"] == "Lettre type.docx"
    assert "Tremblay" not in stored["filename"] + stored["storage_path"]
    # The inventory counts, the new etag, the always-performed scan.
    entity = result["entity"]
    assert entity["placeholder_count"] == 3 and entity["auto_count"] == 1
    assert entity["manual_count"] == 1 and entity["bloc_count"] == 1
    assert entity["etag"] == stored["etag"] and entity["active"] is False
    assert result["leak_scan"]["performed"] is True
    assert result["leak_scan"]["dossier_id"] == "d1"
    assert result["leak_scan"]["accepted_residues"] == []
    assert result["scrubbed_properties"] is None
    assert result["source_document_id"] == "src"
    # The source document is untouched — record, object, bytes.
    assert world["db"].peek("documents/src") == source_rec
    assert world["bucket"].objects[path] is source_obj


def test_a_residue_of_the_source_dossier_refuses_naming_it(world):
    _seed_source(world, data=LEAKY)
    before, objects = _templates(world), _objects(world)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance"})
    assert exc.reason == "template_residue"
    message = str(exc)
    assert "« Jean Tremblay »" in message and "corps" in message
    assert "« 2026-001 »" in message and "en-tête" in message
    assert "accept_residual" in message and "Rien n'a été créé." in message
    assert _templates(world) == before and _objects(world) == objects


def test_each_accepted_residue_is_echoed_by_name(world):
    _seed_source(world, data=LEAKY)
    result = _create(accept_residual=["Jean Tremblay", "Tremblay", "2026-001",
                                      "Inexistant"])
    residues = {r["identifier"]: r for r in result["leak_scan"]["accepted_residues"]}
    assert set(residues) == {"Jean Tremblay", "Tremblay", "2026-001"}
    assert residues["2026-001"]["where"] == ["en-tête"]
    assert residues["Jean Tremblay"]["count"] == 1
    assert result["leak_scan"]["accepted"] == 3
    assert result["leak_scan"]["unused_accept"] == 1
    for identifier in residues:
        assert any(f"« {identifier} » reste dans le gabarit" in w
                   for w in result["warnings"]), identifier
    assert any("1 entrée(s) d'accept_residual" in w for w in result["warnings"])
    assert _stored_bytes(world, result["entity"]["id"]) == LEAKY


def test_a_name_naming_the_source_dossier_is_refused_before_any_download(
        world, monkeypatch):
    _seed_source(world)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded before the name was judged")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Mise en demeure Tremblay",
        "category": "correspondance"})
    assert exc.reason == "template_residue"
    assert "`name`" in str(exc) and "« Tremblay »" in str(exc)
    assert "Mise en demeure" not in str(exc)        # the caller's text, never


def test_a_name_accepted_on_the_lawyer_s_word_is_echoed(world):
    _seed_source(world)
    result = _create(name="Mise en demeure Tremblay",
                     accept_residual=["Tremblay"])
    assert result["leak_scan"]["accepted_in_name"] == ["Tremblay"]
    assert result["leak_scan"]["unused_accept"] == 0       # used by the name
    assert any("« Tremblay » reste dans le NOM" in w for w in result["warnings"])


def test_the_scan_fails_closed_on_an_unreadable_party(world):
    _seed_source(world)
    world["db"].seed("dossiers/d1", {
        **world["db"].peek("dossiers/d1"),
        "clients": [{"id": "fantome", "name": "X", "roles": [],
                     "avocat_id": "", "avocat_name": ""}],
        "client_ids": ["fantome"]})
    before = _templates(world)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre type",
        "category": "correspondance"})
    assert "toutes les parties" in str(exc)
    assert _templates(world) == before


def test_an_unreadable_source_dossier_refuses(world):
    _seed_source(world, dossier="d-absent")
    before = _templates(world)
    assert "introuvable ou illisible" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))
    assert _templates(world) == before


def test_a_source_without_a_dossier_is_never_registered_unchecked(world):
    _seed_source(world, dossier="")
    world["db"].seed("documents/src", {**world["db"].peek("documents/src"),
                                       "dossier_id": ""})
    assert "aucun dossier" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))


@pytest.mark.parametrize("doc_id", ["inconnu", "src/analyses/x", ""])
def test_an_unknown_source_is_refused(world, doc_id):
    _seed_source(world)
    assert "introuvable" in str(_refused(
        handlers.create_template, {"source_document_id": doc_id,
                                   "name": "Lettre", "category": "autre"}))


def test_only_a_word_document_is_registered(world):
    _seed_source(world, file_type="application/pdf", filename="lettre.pdf")
    assert "(.docx)" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))


def test_an_oversized_source_is_refused_on_its_metadata_before_the_bytes(
        world, monkeypatch):
    _seed_source(world, file_size=tpl_model.MAX_TEMPLATE_SIZE + 1)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded an oversized source")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    assert "10 Mo" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))


def test_stale_metadata_cannot_smuggle_a_larger_file_in(world):
    """The size is re-checked on the downloaded bytes: a record claiming a
    small file whose object is larger is refused."""
    big = CLEAN + b"\0" * (tpl_model.MAX_TEMPLATE_SIZE + 1 - len(CLEAN))
    _seed_source(world, data=big, file_size=len(CLEAN))
    assert "10 Mo" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))


def test_a_source_whose_file_vanished_writes_nothing(world):
    path = _seed_source(world)
    world["bucket"].remove(path)
    before = _templates(world)
    assert "réessayez" in str(_refused(
        handlers.create_template, {"source_document_id": "src",
                                   "name": "Lettre", "category": "autre"}))
    assert _templates(world) == before


def test_a_file_that_is_not_a_template_is_refused(world):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", _CT)
    _seed_source(world, data=buf.getvalue())
    before = _templates(world)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre", "category": "autre"})
    assert "Rien n'a été créé." in str(exc)
    assert _templates(world) == before


def test_scrubbing_the_properties_clears_a_residue_there(world):
    data = _docx(_p("Objet : {{objet_lettre}}"), creator="Jean Tremblay")
    _seed_source(world, data=data)
    exc = _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre", "category": "autre"})
    assert "scrub_properties" in str(exc) and "propriétés du document" in str(exc)
    result = _create(scrub_properties=True)
    assert result["scrubbed_properties"] == ["dc:creator"]
    stored = _stored_bytes(world, result["entity"]["id"])
    assert b"Tremblay" not in zipfile.ZipFile(io.BytesIO(stored)).read(
        "docProps/core.xml")
    # Every other entry is byte-identical.
    before = zipfile.ZipFile(io.BytesIO(data))
    after = zipfile.ZipFile(io.BytesIO(stored))
    assert before.read("word/document.xml") == after.read("word/document.xml")


def test_a_special_kind_is_created_but_never_designated(world):
    note = _docx(_p("{{note.titre}}") + _p("{{note.contenu}}"))
    active, errors = tpl_model.create_template(
        io.BytesIO(note), "n.docx", len(note),
        {"name": "Impression", "category": "autre", "kind": "note"}, UID)
    assert errors == []
    tpl_model.set_active_template(active["id"], par="juriste", expected_etag=None)
    _seed_source(world, data=note)
    result = _create(kind="note", category="autre")
    assert result["entity"]["kind"] == "note"
    assert result["entity"]["active"] is False
    assert tpl_model.get_active_template("note")["id"] == active["id"]
    assert any("n'est PAS le gabarit actif" in w for w in result["warnings"])


def test_a_note_print_template_without_its_body_field_is_warned(world):
    _seed_source(world)                    # CLEAN has no {{note.contenu}}
    result = _create(kind="note", category="autre")
    assert any("{{note.contenu}}" in w for w in result["warnings"])


def test_a_field_word_fragmented_is_reported(world):
    fragmented = _docx(
        "<w:p><w:r><w:t>{{objet_</w:t></w:r>"
        '<w:bookmarkStart w:id="0" w:name="b"/>'
        "<w:r><w:t>lettre}}</w:t></w:r></w:p>")
    _seed_source(world, data=fragmented)
    result = _create()
    assert result["entity"]["validation_warnings"]
    assert any("fragmenté" in w for w in result["warnings"])


def test_a_protected_source_says_the_text_was_not_checked(world):
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    assert errors == []
    _seed_source(world, analyse=champ, category_source="analyse")
    result = _create()
    assert any("niveau 3" in w and "jamais sur le texte" in w
               for w in result["warnings"])


def test_a_malformed_analysis_cache_never_fails_a_written_template(world):
    """Review of T10: the protection warning read the source's stored
    ``analyse`` AFTER the template committed — a cache that is not a mapping
    raised there, and the call reported « ENREGISTRÉE — NE PAS RÉESSAYER »
    for a template written cleanly. It is read before the write now."""
    _seed_source(world, analyse=["pas", "un", "dictionnaire"])
    result = _create()
    assert result["created"] is True
    assert not any("qualifié" in w for w in result["warnings"])


def test_without_a_storage_identity_nothing_is_written(world, monkeypatch):
    _seed_source(world)

    def _unavailable():
        raise storage_identity.StorageIdentityUnavailable()

    monkeypatch.setattr(storage_identity, "owner_uid", _unavailable)
    before, objects = _templates(world), _objects(world)
    _refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre", "category": "autre"})
    assert _templates(world) == before and _objects(world) == objects


def test_a_same_key_retry_replays_and_never_creates_twice(world):
    _seed_source(world)
    args = {"source_document_id": "src", "name": "Lettre type",
            "category": "autre", "idempotency_key": "gabarit-t10-1"}
    first = handlers.create_template(dict(args))
    again = handlers.create_template(dict(args))
    assert again["idempotent_replay"] is True
    assert again["entity"]["id"] == first["entity"]["id"]
    assert len(_templates(world)) == 2               # the base one + this one


def test_a_failure_after_the_create_is_reported_committed(world, monkeypatch):
    """A raise after the template committed must never read as a refusal a
    retry would answer with a SECOND template."""
    _seed_source(world)

    def _boom(_t):
        raise RuntimeError("payload builder")

    monkeypatch.setattr(handlers, "_template_row", _boom)
    with pytest.raises(tools.CommittedWriteError) as exc:
        _create()
    assert exc.value.collection == "doc_templates"
    assert len(_templates(world)) == 2


@pytest.mark.parametrize("over, needle", [
    ({"category": "pièce"}, "`category`"),
    ({"name": "   "}, "`name` est requis"),
    ({"name": "Lettre <b>x</b>"}, "chevrons"),
    ({"accept_residual": ["a", "a"]}, "figure déjà"),
    ({"scrub_properties": "oui"}, "vrai ou faux"),
])
def test_a_bad_argument_is_refused_before_anything_is_read(world, over, needle):
    _seed_source(world)
    args = {"source_document_id": "src", "name": "Lettre", "category": "autre"}
    args.update(over)
    before = _templates(world)
    assert needle in str(_refused(handlers.create_template, args))
    assert _templates(world) == before


# ══════════════════════════════════════════════════════════════════════
# 2. update_template — metadata
# ══════════════════════════════════════════════════════════════════════


def _update(world, **over) -> dict:
    args = {"template_id": world["template"]}
    args.update(over)
    return handlers.update_template(args)


def test_a_metadata_edit_replaces_only_what_it_names(world):
    tid = world["template"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    result = _update(world, name="Lettre révisée", description="Nouvelle",
                     expected_etag=before["etag"])
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert result["mode"] == "metadata"
    assert result["changed_fields"] == ["name", "description"]
    assert stored["name"] == "Lettre révisée" and stored["description"] == "Nouvelle"
    assert stored["category"] == before["category"]            # untouched
    assert stored["storage_path"] == before["storage_path"]
    assert stored["version"] == 1 and stored["updated_via"] == "mcp"
    assert result["entity"]["etag"] == stored["etag"] != before["etag"]
    assert result["file_replaced"] is False and result["leak_scan"] is None
    assert result["source_document_id"] is None


def test_values_already_stored_write_nothing(world):
    tid = world["template"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    result = _update(world, name="Lettre de base", category="correspondance")
    assert result["changed_fields"] == []
    assert any("déjà enregistrées" in w for w in result["warnings"])
    assert world["db"].peek(f"doc_templates/{tid}") == before


def test_a_rename_without_a_recorded_source_is_said_unchecked(world):
    """REWRITTEN deliberately (fixups of lot 2A). It pinned « a rename is
    checked against NO dossier » — the review of T11's disclosure of a gap
    the critic of lot 2A then asked to close. A template now RECORDS the
    dossier it was drawn from, and a rename is checked against it. This
    one (the web form's: no source recorded) has nothing to be checked
    against: the name is stored, and the result SAYS it was not checked —
    never a silent pass."""
    tid = world["template"]
    result = _update(world, name="Lettre Tremblay")
    assert result["changed_fields"] == ["name"]
    assert result["leak_scan"] is None
    assert result["name_check"] == {
        "performed": False, "dossier_ids": [], "missing_dossier_ids": [],
        "accepted": [], "unused_accept": 0}
    assert any("contrôlé contre AUCUN dossier" in w for w in result["warnings"])
    assert world["db"].peek(f"doc_templates/{tid}")["name"] == "Lettre Tremblay"
    # An acceptance with nothing checked is said to be ignored.
    again = _update(world, name="Lettre Tremblay 2",
                    accept_residual=["Tremblay"])
    assert any("`accept_residual` ignoré" in w for w in again["warnings"])


def test_a_rename_is_checked_against_the_source_dossier_and_the_texts_say_so(
    world,
):
    """Fixups of lot 2A — FAILS on the old handler, which stored « Lettre
    Tremblay » on a template drawn from Tremblay's dossier: that name then
    printed in the name of every document generated from it, for any
    client. Refused, naming the identifier; accepted only on the lawyer's
    word, echoed back."""
    _seed_source(world)
    created = _create(name="Lettre type")
    tid = created["entity"]["id"]
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert stored["source_dossier_id"] == "d1"
    before = dict(stored)
    exc = _refused(handlers.update_template,
                   {"template_id": tid, "name": "Lettre Tremblay"})
    assert exc.reason == "template_residue"
    assert "« Tremblay »" in str(exc) and "accept_residual" in str(exc)
    assert "Rien n'a été modifié" in str(exc)
    assert world["db"].peek(f"doc_templates/{tid}") == before

    result = handlers.update_template({
        "template_id": tid, "name": "Lettre Tremblay",
        "accept_residual": ["Tremblay"]})
    assert result["name_check"]["performed"] is True
    assert result["name_check"]["dossier_ids"] == ["d1"]
    assert result["name_check"]["accepted"] == ["Tremblay"]
    assert any("« Tremblay » reste dans le NOM" in w for w in result["warnings"])
    assert world["db"].peek(f"doc_templates/{tid}")["name"] == "Lettre Tremblay"

    text = endpoint.INSTRUCTIONS
    assert "refused while it or the template's name" not in text
    assert "checked against no dossier" not in text
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it.
    assert "or `name` (literals included too) still carries the source " in (
        tools.TOOLS["create_template"]["description"])
    assert "never a party's name" in (
        tools.TOOLS["update_template"]["description"])
    assert "a new name is checked against the dossiers its files came from" in (
        tools.TOOLS["update_template"]["description"])
    consent = " ".join((_ATHENA / "templates" / "mcp" / "families"
                        / "_templates.html").read_text(encoding="utf-8").split())
    assert "n'est contrôlé contre <strong>aucun</strong> dossier" not in consent
    assert "Le nouveau nom donné à un gabarit existant est contrôlé" in consent


def test_the_model_refuses_a_residue_on_every_path(world):
    """The rule lives in the MODEL (plan rule 2): a caller that skips the
    handler's check — the web form, a future path — is refused too, with a
    message that names nothing of the dossier."""
    _seed_source(world)
    tid = _create(name="Lettre type")["entity"]["id"]
    got, errors, changed = tpl_model.update_template(
        tid, {"name": "Mise en demeure Tremblay"})
    assert got is None and changed is False
    assert errors == [tpl_model.NAME_RESIDUE_ERROR]
    assert "Tremblay" not in tpl_model.NAME_RESIDUE_ERROR
    got, errors, changed = tpl_model.update_template(
        tid, {"name": "Mise en demeure"})
    assert errors == [] and changed is True


def test_a_new_version_s_source_dossier_is_checked_too(world):
    """A web template (no source) given a new FILE from d1's document: the
    version records d1, and a rename is then checked against it."""
    _seed_source(world, "v2src", V2)
    tid = world["template"]
    result = _update(world, source_document_id="v2src", expected_version=1)
    assert result["file_replaced"] is True
    entry = world["db"].peek(f"doc_templates/{tid}/versions/2")
    assert entry["source_dossier_id"] == "d1"
    assert world["db"].peek(f"doc_templates/{tid}")["source_dossier_id"] == ""
    exc = _refused(handlers.update_template,
                   {"template_id": tid, "name": "Lettre Tremblay"})
    assert exc.reason == "template_residue"


def test_a_rename_check_that_cannot_read_refuses(world, monkeypatch):
    _seed_source(world)
    tid = _create(name="Lettre type")["entity"]["id"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))

    def _down(_did):
        raise RuntimeError("firestore indisponible")

    from models import dossier as dossier_model
    monkeypatch.setattr(dossier_model, "get_dossier_strict", _down)
    import services.template_names as template_names
    monkeypatch.setattr(template_names, "get_dossier_strict", _down)
    exc = _refused(handlers.update_template,
                   {"template_id": tid, "name": "Lettre neutre"})
    assert "réessayez" in str(exc)
    assert world["db"].peek(f"doc_templates/{tid}") == before


def test_a_full_versions_window_is_never_taken_for_the_whole_history(
    world, monkeypatch,
):
    """Review of the fixups of lot 2A: the version list was read up to a
    window « more than any template will carry » and then TAKEN as whole —
    a source recorded past it was a rename checked against nothing, in
    silence. A window that comes back full now refuses (« réessayez »),
    never « nothing found ». With the window at 1 the old code read only
    the newest version (no source) and stored « Lettre Tremblay » over the
    d1 source of version 2: this test FAILS on it."""
    import io as _io

    _seed_source(world, "v2src", V2)
    tid = world["template"]
    _update(world, source_document_id="v2src", expected_version=1)  # v2 ← d1
    got, errors, changed = tpl_model.update_template(      # v3, no source
        tid, {}, _io.BytesIO(CLEAN), "gabarit.docx", len(CLEAN))
    assert errors == [] and changed is True
    monkeypatch.setattr(tpl_model, "_SOURCE_VERSIONS_WINDOW", 1)
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    exc = _refused(handlers.update_template,
                   {"template_id": tid, "name": "Lettre Tremblay"})
    assert "réessayez" in str(exc)
    got, errors, changed = tpl_model.update_template(
        tid, {"name": "Lettre Tremblay"})
    assert errors == [tpl_model.NAME_CHECK_UNAVAILABLE_ERROR]
    assert world["db"].peek(f"doc_templates/{tid}") == before


def test_a_deleted_source_dossier_is_said_not_checked(world):
    _seed_source(world)
    tid = _create(name="Lettre type")["entity"]["id"]
    world["db"].external_delete("dossiers/d1")
    result = _update(world, template_id=tid, name="Lettre Tremblay")
    assert result["name_check"]["performed"] is False
    assert result["name_check"]["missing_dossier_ids"] == ["d1"]
    assert any("n'existe plus" in w for w in result["warnings"])


def test_a_stale_expected_etag_is_refused_and_nothing_written(world):
    tid = world["template"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    exc = _refused(handlers.update_template, {
        "template_id": tid, "name": "Autre", "expected_etag": "perime"})
    assert exc.reason == "stale_etag" and "list_templates" in str(exc)
    assert world["db"].peek(f"doc_templates/{tid}") == before


def test_without_an_etag_a_write_landing_between_read_and_commit_is_refused(
        world, monkeypatch):
    """Plan rule 3: the handler compare-and-sets against the etag it read."""
    tid = world["template"]
    real = tpl_model.update_template

    def _racing(template_id, data, *a, **k):
        # Another process (the application) edits the template first — a
        # write outside this call, so outside its commit record.
        stored = world["db"].peek(f"doc_templates/{template_id}")
        world["db"].seed(f"doc_templates/{template_id}", {
            **stored, "description": "Web", "etag": "e-web"})
        return real(template_id, data, *a, **k)

    monkeypatch.setattr(tpl_model, "update_template", _racing)
    exc = _refused(handlers.update_template, {"template_id": tid,
                                              "name": "Connecteur"})
    assert exc.reason == "stale_etag"
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert stored["name"] == "Lettre de base" and stored["description"] == "Web"


def _active_note(world) -> str:
    note = _docx(_p("{{note.titre}}") + _p("{{note.contenu}}"))
    tpl, errors = tpl_model.create_template(
        io.BytesIO(note), "n.docx", len(note),
        {"name": "Impression", "category": "autre", "kind": "note"}, UID)
    assert errors == []
    _d, errors = tpl_model.set_active_template(tpl["id"], par="juriste",
                                               expected_etag=None)
    assert errors == []
    return tpl["id"]


def test_the_kind_of_the_active_template_cannot_change(world):
    tid = _active_note(world)
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    exc = _refused(handlers.update_template, {"template_id": tid,
                                              "kind": "gabarit"})
    assert "ACTIF" in str(exc) and "désignation" in str(exc)
    assert world["db"].peek(f"doc_templates/{tid}") == before
    assert tpl_model.get_active_template("note")["id"] == tid


def test_editing_the_active_template_never_touches_its_designation(world):
    tid = _active_note(world)
    designated = world["db"].peek(f"doc_templates/{tid}")["active_designated_at"]
    result = _update(world, template_id=tid, name="Impression révisée")
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert result["entity"]["active"] is True
    assert stored["active_for"] == "note"
    assert stored["active_designated_at"] == designated


def test_a_kind_change_to_a_special_kind_is_never_active_and_says_so(world):
    result = _update(world, kind="note_honoraires")
    assert result["entity"]["kind"] == "note_honoraires"
    assert result["entity"]["active"] is False
    assert tpl_model.get_active_template("note_honoraires") is None
    assert any("n'est PAS le gabarit actif" in w for w in result["warnings"])


@pytest.mark.parametrize("args, needle", [
    ({"name": "X", "source_document_id": "src", "expected_version": 1},
     "Deux corrections distinctes"),
    ({}, "Rien à corriger"),
    ({"expected_version": 1}, "ne s'applique qu'à un nouveau fichier"),
    # Changed deliberately (fixups of lot 2A): accept_residual now also
    # serves a new NAME, so alone it is simply « nothing to correct » — and
    # beside metadata WITHOUT a name it is refused as misplaced.
    ({"accept_residual": ["Tremblay"]}, "Rien à corriger"),
    ({"description": "x", "accept_residual": ["Tremblay"]},
     "ne s'applique qu'à un nouveau nom"),
    ({"name": ""}, "ne peut pas être vide"),
    ({"category": "pièce"}, "`category`"),
])
def test_the_call_shapes_are_refused_writing_nothing(world, args, needle):
    tid = world["template"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    assert needle in str(_refused(handlers.update_template,
                                  {"template_id": tid, **args}))
    assert world["db"].peek(f"doc_templates/{tid}") == before


@pytest.mark.parametrize("template_id", ["inconnu", "a/b"])
def test_an_unknown_template_is_refused(world, template_id):
    assert "list_templates" in str(_refused(
        handlers.update_template, {"template_id": template_id, "name": "X"}))


def test_an_unreadable_template_store_is_never_introuvable(world, monkeypatch):
    def _broken(*_a, **_k):
        raise tpl_model.TemplateReadError("x")

    monkeypatch.setattr(tpl_model, "get_template", _broken)
    message = str(_refused(handlers.update_template,
                           {"template_id": world["template"], "name": "X"}))
    assert "réessayez" in message and "introuvable" not in message


# ══════════════════════════════════════════════════════════════════════
# 3. update_template — a new version of the file
# ══════════════════════════════════════════════════════════════════════


def _replace(world, **over) -> dict:
    args = {"template_id": world["template"], "source_document_id": "src",
            "expected_version": 1}
    args.update(over)
    return handlers.update_template(args)


def test_a_new_file_is_installed_as_a_new_version_the_old_one_kept(world):
    tid = world["template"]
    path = _seed_source(world, data=V2)
    source_rec = dict(world["db"].peek("documents/src"))
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    old_bytes = _stored_bytes(world, tid)
    result = _replace(world)
    after = world["db"].peek(f"doc_templates/{tid}")
    assert result["mode"] == "file" and result["file_replaced"] is True
    assert result["changed_fields"] == ["file"]
    assert result["replaced_version"] == 1 and after["version"] == 2
    assert result["entity"]["version"] == 2
    assert result["entity"]["etag"] == after["etag"]
    assert _stored_bytes(world, tid) == V2
    # Version 1 is KEPT: its object and its entry, restorable.
    assert world["bucket"].objects[before["storage_path"]].data == old_bytes
    assert world["db"].peek(f"doc_templates/{tid}/versions/1") is not None
    assert world["db"].peek(f"doc_templates/{tid}/versions/2")["sha256"] == (
        hashlib.sha256(V2).hexdigest())
    # The metadata, the name, the neutral file name — untouched or derived.
    assert after["name"] == "Lettre de base"
    assert after["original_filename"] == "Lettre de base.docx"
    assert any("version 1 est conservée" in w for w in result["warnings"])
    # The version dropped {{FAITS}}: said.
    assert any("{{FAITS}}" in w for w in result["warnings"])
    assert result["leak_scan"]["performed"] is True
    assert result["source_document_id"] == "src"
    assert world["db"].peek("documents/src") == source_rec
    assert world["bucket"].objects[path].data == V2
    restored, errors, changed = tpl_model.restore_template_version(
        tid, 1, par="juriste", expected_etag=None)
    assert errors == [] and changed and _stored_bytes(world, tid) == old_bytes


def test_expected_version_is_required_for_a_new_file(world):
    _seed_source(world, data=V2)
    exc = _refused(handlers.update_template, {
        "template_id": world["template"], "source_document_id": "src"})
    assert "`expected_version` est requis" in str(exc)


def test_a_stale_expected_version_is_refused_before_any_download(
        world, monkeypatch):
    tid = world["template"]
    for body in ("deux", "trois"):
        data = _docx(_p(f"{body} {{{{objet_lettre}}}}"))
        _t, errors, _c = tpl_model.update_template(
            tid, {}, io.BytesIO(data), "x.docx", len(data))
        assert errors == []
    _seed_source(world, data=V2)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded for a stale version")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    exc = _refused(handlers.update_template, {
        "template_id": tid, "source_document_id": "src", "expected_version": 1})
    assert exc.reason == "stale_etag" and "version 3, pas 1" in str(exc)


def test_a_stale_named_etag_is_refused_before_any_download(world, monkeypatch):
    """Plan rule 3: an outdated view is answered « re-read » before any
    payload work — here, before the source's bytes are downloaded."""
    _seed_source(world, data=V2)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded for a stale etag")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    exc = _refused(handlers.update_template, {
        "template_id": world["template"], "source_document_id": "src",
        "expected_version": 1, "expected_etag": "perime"})
    assert exc.reason == "stale_etag" and "list_templates" in str(exc)


def test_one_version_ahead_with_other_bytes_is_refused(world):
    tid = world["template"]
    other = _docx(_p("Autre main : {{objet_lettre}}"))
    _t, errors, _c = tpl_model.update_template(
        tid, {}, io.BytesIO(other), "o.docx", len(other))
    assert errors == []
    _seed_source(world, data=V2)
    exc = _refused(handlers.update_template, {
        "template_id": tid, "source_document_id": "src", "expected_version": 1})
    assert exc.reason == "stale_etag" and "version 2, pas 1" in str(exc)
    assert _stored_bytes(world, tid) == other


def test_a_replacement_retried_after_it_landed_writes_nothing(world):
    """The answer of a first call lost, the same call again: the file in
    force already IS these bytes — no third version, no stale refusal."""
    tid = world["template"]
    _seed_source(world, data=V2)
    first = _replace(world)
    assert first["file_replaced"] is True
    again = _replace(world)
    assert again["file_replaced"] is False and again["replaced_version"] is None
    assert again["changed_fields"] == []
    assert any("identique à la version en vigueur (v2)" in w
               for w in again["warnings"])
    assert world["db"].peek(f"doc_templates/{tid}")["version"] == 2


def test_an_identical_file_installs_nothing(world):
    tid = world["template"]
    same = _stored_bytes(world, tid)
    _seed_source(world, data=same)
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    result = _replace(world)
    assert result["file_replaced"] is False
    assert world["db"].peek(f"doc_templates/{tid}") == before


def test_a_new_file_naming_the_source_dossier_is_refused(world):
    tid = world["template"]
    _seed_source(world, data=LEAKY)
    before, objects = dict(world["db"].peek(f"doc_templates/{tid}")), _objects(world)
    exc = _refused(handlers.update_template, {
        "template_id": tid, "source_document_id": "src", "expected_version": 1})
    assert exc.reason == "template_residue" and "« Jean Tremblay »" in str(exc)
    assert "Rien n'a été modifié." in str(exc)
    assert world["db"].peek(f"doc_templates/{tid}") == before
    assert _objects(world) == objects
    accepted = _replace(world, accept_residual=["Jean Tremblay", "Tremblay",
                                                "2026-001"])
    assert accepted["file_replaced"] is True
    assert accepted["leak_scan"]["accepted"] == 3


def test_a_new_file_for_the_active_template_keeps_its_designation(world):
    tid = _active_note(world)
    new = _docx(_p("{{note.titre}} v2") + _p("{{note.contenu}}"))
    _seed_source(world, data=new)
    result = _replace(world, template_id=tid)
    assert result["entity"]["active"] is True and result["entity"]["version"] == 2
    assert tpl_model.get_active_template("note")["id"] == tid
    assert any("ACTIF" in w and "Sa désignation n'a pas changé" in w
               for w in result["warnings"])


def test_a_new_file_for_the_active_note_template_without_its_body_says_now(
        world):
    """Review of T10 — REWRITTEN on purpose: this test used to pin the
    conditional « désigné actif, il n'aurait nulle part… » on the ACTIVE
    template, which already is designated. Its new version prints a note
    WITHOUT its text from this very moment (utils.note_docx skips the
    field, it refuses nothing) and create_document refuses — said in the
    present tense, with the way back (the version kept)."""
    tid = _active_note(world)
    _seed_source(world, data=V2)
    result = _replace(world, template_id=tid)
    body = [w for w in result["warnings"] if "{{note.contenu}}" in w
            and "ne porte pas" in w]
    assert len(body) == 1, result["warnings"]
    assert "dès maintenant" in body[0] and "SANS son texte" in body[0]
    assert "create_document refuse" in body[0]
    assert "rétablir la version 1" in body[0]
    assert "désigné actif, il n'aurait" not in body[0]


def test_a_new_file_for_a_note_template_not_active_stays_conditional(world):
    """The same missing field on a « note » template that is NOT the active
    one: nothing breaks yet — the warning stays conditional."""
    note = _docx(_p("{{note.titre}}") + _p("{{note.contenu}}"))
    tpl, errors = tpl_model.create_template(
        io.BytesIO(note), "n.docx", len(note),
        {"name": "Impression bis", "category": "autre", "kind": "note"}, UID)
    assert errors == []
    _seed_source(world, data=V2)
    result = _replace(world, template_id=tpl["id"])
    assert result["entity"]["active"] is False
    assert any("désigné actif, il n'aurait nulle part" in w
               for w in result["warnings"])
    assert not any("dès maintenant" in w for w in result["warnings"])


def test_a_model_refusal_already_saying_nothing_was_written_is_not_doubled(
        world, monkeypatch):
    """Review of T10: the template model's VERSION_IN_PROGRESS_ERROR (and
    READ_ERROR) already end on « Rien n'a été modifié : réessayez » — the
    handler appended a second « Rien n'a été modifié. »."""
    _seed_source(world, data=V2)
    monkeypatch.setattr(
        tpl_model, "_store_version_bytes",
        lambda *_a, **_k: (None, [tpl_model.VERSION_IN_PROGRESS_ERROR]))
    message = str(_refused(handlers.update_template, {
        "template_id": world["template"], "source_document_id": "src",
        "expected_version": 1}))
    assert message.count("Rien n'a été modifié") == 1, message
    assert "réessayez dans quelques minutes" in message
    monkeypatch.setattr(
        tpl_model, "_store_version_bytes",
        lambda *_a, **_k: (None, [tpl_model.UPLOAD_ERROR]))
    message = str(_refused(handlers.create_template, {
        "source_document_id": "src", "name": "Lettre", "category": "autre"}))
    assert message.endswith("Rien n'a été créé."), message


def test_an_expected_version_ahead_of_the_stored_one_is_named_as_such(
        world, monkeypatch):
    """Review of T10: a version is never withdrawn (a restore installs N+1),
    so an expected_version AHEAD of the stored one was never read — the
    refusal used to claim « une autre version a été installée depuis votre
    lecture », sending the caller after a write that never happened."""
    _seed_source(world, data=V2)

    def _no_download(*_a, **_k):
        raise AssertionError("downloaded for an impossible version")

    monkeypatch.setattr(document_model, "get_document_bytes", _no_download)
    exc = _refused(handlers.update_template, {
        "template_id": world["template"], "source_document_id": "src",
        "expected_version": 5})
    message = str(exc)
    assert "version 1" in message and "aucune version 5 n'existe" in message
    assert "installée depuis votre lecture" not in message
    assert exc.reason != "stale_etag"      # a wrong argument, not a stale view


def test_a_version_installed_during_the_call_is_refused_by_the_model(
        world, monkeypatch):
    """The race past the handler's own version check: the template model
    refuses in its transaction, and removes the object THIS call wrote."""
    tid = world["template"]
    _seed_source(world, data=V2)
    rival = _docx(_p("Rivale : {{objet_lettre}}"))
    rival_path = f"users/{UID}/templates/{tid}/v2/r.docx"
    real = tpl_model.update_template

    def _racing(template_id, data, stream=None, filename=None, size=None, **k):
        # Another process installs version 2 first — outside this call, so
        # outside its commit record.
        world["bucket"].put(rival_path, rival)
        stored = world["db"].peek(f"doc_templates/{template_id}")
        world["db"].seed(f"doc_templates/{template_id}", {
            **stored, "version": 2, "storage_path": rival_path,
            "sha256": hashlib.sha256(rival).hexdigest(), "etag": "e-rival"})
        return real(template_id, data, stream, filename, size, **k)

    monkeypatch.setattr(tpl_model, "update_template", _racing)
    exc = _refused(handlers.update_template, {
        "template_id": tid, "source_document_id": "src", "expected_version": 1})
    assert exc.reason == "stale_etag"
    stored = world["db"].peek(f"doc_templates/{tid}")
    assert stored["version"] == 2 and _stored_bytes(world, tid) == rival
    ours = [o for o in world["bucket"].objects.values() if o.data == V2]
    assert len(ours) == 1               # the source document's object only


def test_a_source_in_another_format_is_refused_for_a_new_file(world):
    _seed_source(world, file_type="application/pdf")
    assert "(.docx)" in str(_refused(handlers.update_template, {
        "template_id": world["template"], "source_document_id": "src",
        "expected_version": 1}))


# ══════════════════════════════════════════════════════════════════════
# 4. The NEVERS, the registry, the texts
# ══════════════════════════════════════════════════════════════════════


def test_no_template_write_ever_touches_the_source_document(world):
    """NEVER « document »: the source is only READ — its record, its object
    and its generation stay exactly as they were, through a creation and a
    new version alike."""
    path = _seed_source(world, data=V2)
    record = dict(world["db"].peek("documents/src"))
    obj = world["bucket"].objects[path]
    _create()
    _replace(world)
    assert world["db"].peek("documents/src") == record
    assert world["bucket"].objects[path] is obj and obj.data == V2


def test_no_template_write_designates_an_active_template(world):
    """NEVER « active_template », behaviourally: no call of either tool
    leaves a template active that the lawyer did not designate."""
    _seed_source(world)
    created = _create(kind="note_honoraires", category="autre")
    _update(world, template_id=created["entity"]["id"], name="Autre nom")
    _update(world, kind="note")
    for template in _templates(world).values():
        assert not template.get("active_for"), template["name"]


def test_the_registry_declares_what_the_tools_do():
    assert {"create_template", "update_template"} <= tools.WRITE_TOOLS
    assert "update_template" in tools.EDIT_TOOLS
    assert "create_template" not in tools.EDIT_TOOLS
    update = tools.TOOLS["update_template"]
    assert update["concurrency"] == tools.CONCURRENCY_OPTIONAL
    assert update["etag_readers"] == ("list_templates",)
    assert update["annotations"] == {"idempotentHint": True}
    assert "concurrency" not in tools.TOOLS["create_template"]
    for name in ("create_template", "update_template"):
        spec = tools.TOOLS[name]
        assert spec["idempotency"] == tools.IDEMPOTENCY_OPTIONAL
        props = spec["input_schema"]["properties"]
        assert set(props["kind"]["enum"]) == set(tpl_model.VALID_KINDS)
        assert set(props["category"]["enum"]) == set(tpl_model.VALID_CATEGORIES)
        assert props["name"]["maxLength"] == tpl_model.NAME_MAX
        assert props["description"]["maxLength"] == tpl_model.DESCRIPTION_MAX
        assert "category" not in props or "enum" in props["category"]
        assert "etag" not in props
    assert tools.TOOLS["create_template"]["input_schema"]["required"] == [
        "source_document_id", "name", "category"]


def test_no_template_result_carries_a_path_a_url_or_a_file_name(world):
    _seed_source(world, data=V2)
    results = [_create(), _update(world, name="Nouveau nom"), _replace(world)]
    for result in results:
        text = str(tools._jsonable(result))
        for forbidden in ("storage_path", "users/", "googleapis", "sha256",
                          "Lettre_a_M._Tremblay", "Lettre à M. Tremblay",
                          "original_filename"):
            assert forbidden not in text, forbidden


def test_the_instructions_and_the_consent_name_the_template_tools():
    text = endpoint.INSTRUCTIONS
    assert "`create_template`" in text and "`update_template`" in text
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it: a stored file is refused while it names its source dossier.
    assert "still carries the source dossier's names" in (
        tools.TOOLS["create_template"]["description"])
    # REWRITTEN in lot 2A (T11): the template paragraph moved with its tools
    # from the FILES partial to the TEMPLATES family's own.
    assert "TEMPLATES: " in text
    consent = (_ATHENA / "templates" / "mcp" / "families"
               / "_templates.html").read_text(encoding="utf-8")
    flat = " ".join(consent.split())
    assert "Gérer vos gabarits" in flat
    assert "document d'origine n'est jamais modifié" in flat


# ══════════════════════════════════════════════════════════════════════
# The web edit form's rename (fixups of lot 2A)
# ══════════════════════════════════════════════════════════════════════


def _web_client():
    from flask import Flask

    with mock.patch("google.cloud.firestore.Client"):
        import routes.doc_templates as rdt
    from tz import to_mtl
    from utils.icons import ms

    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl)
    app.register_blueprint(rdt.doc_templates_bp)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = UID
        sess["email"] = "juriste@example.com"
        sess["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def test_the_web_form_refuses_a_rename_naming_the_source_dossier(world):
    """FAILS on the old model: the edit form stored « Lettre Tremblay » on
    a template drawn from Tremblay's dossier. The refusal re-renders the
    form (200 — htmx and the plain POST alike) and names NOTHING of the
    dossier beyond « le nom reprend un identifiant du dossier source »."""
    _seed_source(world)
    tid = _create(name="Lettre type")["entity"]["id"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    web = _web_client()
    resp = web.post(f"/gabarits/{tid}", data={
        "name": "Lettre Tremblay", "description": "", "category":
        "correspondance", "kind": "gabarit",
        "expected_etag": before["etag"]})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Le nom reprend un identifiant du dossier source" in html
    assert "« Tremblay »" not in html
    assert world["db"].peek(f"doc_templates/{tid}") == before
    ok = web.post(f"/gabarits/{tid}", data={
        "name": "Lettre neutre", "description": "", "category":
        "correspondance", "kind": "gabarit",
        "expected_etag": before["etag"]})
    assert ok.status_code == 302
    assert world["db"].peek(f"doc_templates/{tid}")["name"] == "Lettre neutre"


def test_the_web_form_renames_freely_a_template_with_no_recorded_source(world):
    tid = world["template"]
    before = dict(world["db"].peek(f"doc_templates/{tid}"))
    resp = _web_client().post(f"/gabarits/{tid}", data={
        "name": "Lettre Tremblay", "description": "", "category":
        "correspondance", "kind": "gabarit",
        "expected_etag": before["etag"]})
    assert resp.status_code == 302
    assert world["db"].peek(f"doc_templates/{tid}")["name"] == "Lettre Tremblay"
