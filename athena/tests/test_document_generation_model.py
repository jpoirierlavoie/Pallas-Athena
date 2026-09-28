"""Ce que les générations du connecteur exigent des modèles (lot 2A, étape
T8) — posé AVANT l'outil qui s'en sert (règle transversale 1 du plan).

Chaque test épingle un défaut réel du code d'avant, et ÉCHOUE sur lui :

* ``upload_document`` et ``ingest_blob_as_document`` n'appelaient pas
  ``provenance.note_commit`` : le connecteur les atteint désormais (par
  ``services/gabarits`` et la copie), et un échec APRÈS le versement — le
  constructeur du résultat — serait revenu comme un refus, que la reprise
  aurait « réparé » par un SECOND document ;
* la création du dossier « Projets » était notée comme LA validation : une
  génération dont le versement échoue après l'avoir créé aurait répondu
  « ENREGISTRÉE — NE PAS RÉESSAYER » (en nommant un dossier de classement),
  interdisant la seule reprise sûre ;
* il n'existait aucune copie : la protection d'un document privilégié ne
  pouvait suivre sa copie, et rien n'empêchait structurellement une copie
  d'atterrir dans le dossier d'un autre client ;
* ``get_template_bytes`` relisait le gabarit par son identifiant : un
  remplacement commis entre la lecture et le téléchargement faisait
  imprimer la version N+1 sous le nom de la version N.

Tout passe par les VRAIS modèles au-dessus du faux Firestore partagé et du
faux Cloud Storage (``tests/_fake_gcs.py``), qui garde les octets et refuse
ce que le service refuse ; on relit ce qui est STOCKÉ.
"""

import inspect
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
    from models import doc_template as tpl_model
    from models import document as doc
    from models import folder as folder_model
    from models import provenance
    from services import gabarits as sg

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
DOCX_MIME = doc.EXTENSION_MIME_TYPES[".docx"]


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body><w:p>'
                    f"<w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


SOURCE_BYTES = _docx("Lettre au client")


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, doc, folder_model, tpl_model)
    bucket = FakeBucket()
    monkeypatch.setattr(doc.storage, "bucket", lambda: bucket)
    for did in ("d1", "d2"):
        db.seed(f"dossiers/{did}", {"id": did, "file_number": f"2026-00{did[-1]}",
                                    "title": f"Dossier {did}", "status": "actif"})
    return db, bucket


def _source(db, bucket, doc_id="src", *, dossier="d1", **over):
    path = f"users/{UID}/dossiers/{dossier}/documents/{doc_id}/lettre.docx"
    bucket.put(path, SOURCE_BYTES, content_type=DOCX_MIME)
    record = {**doc._default_doc(), "id": doc_id, "dossier_id": dossier,
              "dossier_file_number": "2026-001", "display_name": "Lettre",
              "filename": "lettre.docx", "original_filename": "lettre.docx",
              "file_type": DOCX_MIME, "file_size": len(SOURCE_BYTES),
              "storage_path": path, "category": "correspondance",
              "category_source": "juriste", "notes_internes": "Texte du juriste.",
              "tags": ["urgent"], "portail_lot": "lot-1",
              "document_date": DT, "created_at": DT, "updated_at": DT,
              "etag": "e0"}
    record.update(over)
    db.seed(f"documents/{doc_id}", record)
    return path


def _protected_analyse() -> dict:
    champ, errors = doc._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    assert errors == [] and champ["niveau_protection"] == 3
    return champ


# ══════════════════════════════════════════════════════════════════════
# 1. Le point de validation : ce qu'une reprise répéterait, et le reste
# ══════════════════════════════════════════════════════════════════════


def test_an_idempotent_commit_is_recorded_apart_and_handed_up():
    with provenance.writing_via("mcp", tool="t"):
        provenance.note_commit("folders", "f1", idempotent=True)
        with provenance.writing_via("mcp", tool="t"):
            provenance.note_commit("documents", "d1")
            provenance.note_commit("folders", "f2", idempotent=True)
        assert provenance.committed_writes() == (("documents", "d1"),)
        assert provenance.idempotent_writes() == (
            ("folders", "f1"), ("folders", "f2"))
    assert provenance.idempotent_writes() == ()


def test_creating_projets_is_a_commit_a_retry_reproduces(store):
    """Regression: noted as THE commit, the folder turned the failure of the
    upload that followed into « ENREGISTRÉE — NE PAS RÉESSAYER »."""
    with provenance.writing_via("mcp", tool="fill_gabarit"):
        folder, errors = folder_model.ensure_system_folder("d1", "projets")
        assert errors == [] and folder["system_role"] == "projets"
        assert provenance.committed_writes() == ()
        assert provenance.idempotent_writes() == (("folders", folder["id"]),)
        again, _ = folder_model.ensure_system_folder("d1", "projets")
        assert again["id"] == folder["id"]           # reproduced, never a second


def test_adopting_a_legacy_projets_is_idempotent_too(store):
    db, _bucket = store
    db.seed("folders/legacy", {"id": "legacy", "dossier_id": "d1",
                               "name": "Projets", "parent_folder_id": None,
                               "order": 0, "created_at": DT, "updated_at": DT})
    with provenance.writing_via("mcp", tool="fill_gabarit"):
        folder, _ = folder_model.ensure_system_folder("d1", "projets")
        assert folder["id"] == "legacy"
        assert provenance.committed_writes() == ()
        assert provenance.idempotent_writes() == (("folders", "legacy"),)


def test_upload_document_notes_its_commit(store):
    """Regression: the connector reaches it now — without the note, a
    failure after the upload read as « nothing written »."""
    with provenance.writing_via("mcp", tool="fill_gabarit"):
        created, errors = doc.upload_document(
            "d1", "2026-001", io.BytesIO(SOURCE_BYTES), "projet.docx",
            len(SOURCE_BYTES), {"display_name": "Projet"}, UID)
        assert errors == []
        assert provenance.committed_writes() == (("documents", created["id"]),)


def test_ingest_notes_its_commit(store):
    _db, bucket = store
    bucket.put("staging/u/x/lettre.docx", SOURCE_BYTES, content_type=DOCX_MIME)
    blob = bucket.blob("staging/u/x/lettre.docx")
    blob.reload()
    with provenance.writing_via("mcp", tool="create_document"):
        created, errors = doc.ingest_blob_as_document(
            blob, "d1", "2026-001", "lettre.docx", {}, UID)
        assert errors == []
        assert provenance.committed_writes() == (("documents", created["id"]),)


def test_a_refused_creation_notes_nothing(store):
    with provenance.writing_via("mcp", tool="fill_gabarit"):
        created, errors = doc.upload_document(
            "d1", "2026-001", io.BytesIO(b"pas un docx"), "projet.docx", 11,
            {}, UID)
        assert created is None and errors
        assert provenance.committed_writes() == ()


# ══════════════════════════════════════════════════════════════════════
# 2. La copie : un NOUVEAU document, dans le dossier de sa source
# ══════════════════════════════════════════════════════════════════════


def test_a_copy_is_a_new_document_of_the_source_dossier(store):
    db, bucket = store
    path = _source(db, bucket)
    before = bucket.objects[path]
    copy, errors = doc.copy_document("src", user_id=UID, genere_depuis="Copie")
    assert errors == []
    stored = db.peek(f"documents/{copy['id']}")
    assert copy["id"] != "src" and stored["dossier_id"] == "d1"
    assert stored["storage_path"] != path
    assert stored["storage_path"].startswith(f"users/{UID}/dossiers/d1/")
    assert bucket.objects[stored["storage_path"]].data == SOURCE_BYTES
    # The source is untouched — same object, same generation, same record.
    assert bucket.objects[path] is before and before.data == SOURCE_BYTES
    assert db.peek("documents/src")["etag"] == "e0"
    assert stored["display_name"] == "Copie de Lettre"
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "juriste"      # the lawyer's, copied
    assert stored["document_date"] == DT
    assert stored["genere_depuis"] == "Copie"
    # Never the lawyer's text about THE SOURCE, its tags, its portal lot.
    assert stored["notes_internes"] == "" and stored["tags"] == []
    assert stored["portail_lot"] == ""
    assert "analyse" not in stored                     # nothing to inherit


def test_the_copy_function_takes_no_target_dossier():
    """« Same dossier only » is structural: there is no parameter to name
    another one — the rule cannot be skipped by a caller."""
    params = set(inspect.signature(doc.copy_document).parameters)
    assert not {p for p in params if "dossier" in p}, params


def test_a_protected_source_hands_its_level_to_the_copy_in_one_commit(store):
    db, bucket = store
    champ = _protected_analyse()
    _source(db, bucket, analyse=champ, category=champ["nature_detectee"],
            category_source="analyse")
    db.reset_logs()
    copy, errors = doc.copy_document("src", user_id=UID)
    assert errors == []
    stored = db.peek(f"documents/{copy['id']}")
    seed = stored["analyse"]
    assert seed["niveau_protection"] == 3
    assert seed["privileges"] == ["SECRET_PROFESSIONNEL"]
    assert seed["confirme"] is False and seed["declenche_par"] == "copie"
    assert seed["source_document_id"] == "src"
    # A FLOOR, never a qualification: no sub-nature, so the copy does not
    # read « analysée », and its category — derived on the source — stays
    # PRESUMED here, never « analyse ».
    assert "sous_nature" not in seed and not doc.has_analysis(stored)
    assert stored["category_source"] == "mcp"
    # The journal entry, write-once, in the SAME commit as the record.
    journal = db.peek_collection(f"documents/{copy['id']}/analyses")
    assert list(journal.values()) == [seed]
    commits = [c for c in db.commits
               if any(p == f"documents/{copy['id']}" for _op, p in c.ops)]
    assert len(commits) == 1
    assert {p for _op, p in commits[0].ops} == {
        f"documents/{copy['id']}",
        f"documents/{copy['id']}/analyses/{seed['analyse_id']}"}


def test_a_reanalysis_of_the_copy_never_lands_below_the_inherited_level(store):
    """The whole point of the seed: the non-downgrade floor reads it."""
    db, bucket = store
    champ = _protected_analyse()
    _source(db, bucket, analyse=champ, category=champ["nature_detectee"],
            category_source="analyse")
    copy, _ = doc.copy_document("src", user_id=UID)
    # A plain third-party letter derives level 1 on its own.
    analysed, errors = doc.record_analyse(
        copy["id"], {"sous_nature": "CORR_TIERS", "privileges": []})
    assert errors == []
    assert analysed["analyse"]["niveau_protection"] == 3
    assert analysed["analyse"]["divergence_protection"] is True


def test_a_category_claude_presumed_stays_presumed_on_the_copy(store):
    db, bucket = store
    _source(db, bucket, category="preuve", category_source="mcp")
    copy, _ = doc.copy_document("src", user_id=UID)
    assert db.peek(f"documents/{copy['id']}")["category_source"] == "mcp"


@pytest.mark.parametrize("seed, ok", [
    (None, False),
    ({"niveau_protection": 3}, False),                       # not the shape
])
def test_a_creator_refuses_anything_but_a_protection_seed(store, seed, ok):
    assert doc._is_protection_seed(seed) is ok


def test_a_qualification_cannot_enter_through_the_seed(store):
    db, bucket = store
    champ = _protected_analyse()
    _source(db, bucket, analyse=champ)
    seed = doc.protection_seed(db.peek("documents/src"), now=DT)
    assert doc._is_protection_seed(seed)
    forged = {**seed, "sous_nature": "JUG_JUGEMENT"}
    assert not doc._is_protection_seed(forged)
    bucket.put("staging/u/y/x.docx", SOURCE_BYTES, content_type=DOCX_MIME)
    blob = bucket.blob("staging/u/y/x.docx")
    blob.reload()
    created, errors = doc.ingest_blob_as_document(
        blob, "d1", "2026-001", "x.docx", {}, UID, analyse_seed=forged)
    assert created is None and errors == [doc.PROTECTION_SEED_ERROR]
    assert not [p for p in db.peek_collection("documents") if p != "src"]


def test_an_unprotected_source_seeds_nothing():
    assert doc.protection_seed({"id": "s", "analyse": {"niveau_protection": None}},
                               now=DT) is None
    assert doc.protection_seed({"id": "s"}, now=DT) is None
    # A bool is not a level (True == 1 in Python).
    assert doc.protection_seed({"id": "s", "analyse": {"niveau_protection": True}},
                               now=DT) is None


def test_a_missing_source_or_file_copies_nothing(store):
    db, bucket = store
    assert doc.copy_document("nope", user_id=UID) == (
        None, [doc.COPY_SOURCE_NOT_FOUND])
    assert doc.copy_document("a/b", user_id=UID) == (
        None, [doc.COPY_SOURCE_NOT_FOUND])
    path = _source(db, bucket)
    bucket.remove(path)
    assert doc.copy_document("src", user_id=UID) == (
        None, [doc.COPY_FILE_MISSING])
    assert list(db.peek_collection("documents")) == ["src"]


def test_an_unreadable_source_is_never_introuvable(store, monkeypatch):
    def _boom(_id):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(doc, "get_document_strict", _boom)
    assert doc.copy_document("src", user_id=UID) == (
        None, [doc.COPY_SOURCE_UNREADABLE])


def test_the_copy_checks_the_uid_before_reading_anything(store):
    db, bucket = store
    _source(db, bucket)
    db.reset_logs()
    copy, errors = doc.copy_document("src", user_id="unknown")
    assert copy is None and errors
    assert db.reads == []


def test_a_default_copy_name_fits_the_ceiling():
    name = doc.default_copy_name({"display_name": "x" * doc.DISPLAY_NAME_MAX})
    assert name.startswith("Copie de ") and len(name) == doc.DISPLAY_NAME_MAX


# ══════════════════════════════════════════════════════════════════════
# 3. Les octets d'un gabarit : ceux de la version que l'on cite
# ══════════════════════════════════════════════════════════════════════


def test_template_file_bytes_reads_the_version_the_record_names(store):
    """Regression: re-reading by id after a replacement printed version 2's
    bytes under version 1's name."""
    v1, v2 = _docx("{{objet_lettre}} un"), _docx("{{objet_lettre}} deux")
    template, errors = tpl_model.create_template(
        io.BytesIO(v1), "lettre.docx", len(v1),
        {"name": "Lettre", "category": "correspondance", "kind": "gabarit"}, UID)
    assert errors == []
    held = tpl_model.get_template(template["id"])
    _updated, errors, _ = tpl_model.update_template(
        template["id"], {}, io.BytesIO(v2), "lettre.docx", len(v2))
    assert errors == []
    assert tpl_model.template_file_bytes(held) == v1
    assert tpl_model.get_template_bytes(template["id"]) == v2
    assert tpl_model.template_file_bytes(None) is None
    assert tpl_model.template_file_bytes({"storage_path": ""}) is None


# ══════════════════════════════════════════════════════════════════════
# 4. services/gabarits.save_generated — l'unique versement d'un .docx produit
# ══════════════════════════════════════════════════════════════════════


def test_save_generated_files_into_projets_by_default(store):
    db, _bucket = store
    dossier = db.peek("dossiers/d1")
    created, folder = sg.save_generated(
        dossier=dossier, filled=SOURCE_BYTES, uid=UID,
        display_name="Projet", filename="projet.docx", category="preuve",
        category_source="mcp", genere_depuis="Rédigé", document_date=DT)
    stored = db.peek(f"documents/{created['id']}")
    assert folder["system_role"] == "projets"
    assert stored["folder_id"] == folder["id"]
    assert stored["category_source"] == "mcp" and stored["category"] == "preuve"
    assert stored["document_date"] == DT and stored["genere_depuis"] == "Rédigé"


def test_save_generated_files_where_the_caller_resolved(store):
    db, _bucket = store
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                           "parent_folder_id": None, "order": 0,
                           "created_at": DT, "updated_at": DT})
    dossier = db.peek("dossiers/d1")
    at_root, folder = sg.save_generated(
        dossier=dossier, filled=SOURCE_BYTES, uid=UID, display_name="A",
        filename="a.docx", category="autre", genere_depuis="", folder=None)
    assert folder is None and db.peek(f"documents/{at_root['id']}")["folder_id"] is None
    in_f1, folder = sg.save_generated(
        dossier=dossier, filled=SOURCE_BYTES, uid=UID, display_name="B",
        filename="b.docx", category="autre", genere_depuis="",
        folder=db.peek("folders/f1"))
    assert folder["id"] == "f1"
    assert db.peek(f"documents/{in_f1['id']}")["folder_id"] == "f1"
    # Neither created « Projets ».
    assert not [f for f in db.peek_collection("folders").values()
                if f.get("system_role") == "projets"]
