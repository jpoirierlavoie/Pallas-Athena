"""Le modèle des documents, durci avant que le connecteur ne l'atteigne
(plan, lot 2A — étape T1, 2026-09-27).

Chaque test ici épingle un défaut réel du code d'avant, et ÉCHOUE sur lui :

* ``_prepare_document_record`` fusionnait les métadonnées brutes de
  l'appelant — une analyse « confirmée », un ``storage_path``, une
  ``version`` arrivaient tels quels dans Firestore ;
* ``update_metadata``, ``record_analyse``, ``update_analyse``,
  ``confirmer_analyse`` et ``move_document`` lisaient HORS transaction puis
  faisaient un ``set()`` du document ENTIER : une écriture glissée entre les
  deux était annulée, et deux analyses parallèles faisaient DESCENDRE le
  niveau de protection sans divergence ;
* ``update_metadata`` posait ``category_source = « juriste »`` dès que la
  catégorie était PORTÉE, changée ou non : une présomption devenait
  détermination de l'avocat sans qu'il y touche ;
* une date mal formée EFFAÇAIT la date stockée ; un passage « < … > » ou un
  texte trop long était retiré ou tronqué en silence ;
* ``ingest_blob_as_document`` n'avait aucun identifiant réservé (deux
  finalisations → deux documents), et son annulation supprimait le chemin
  canonique à l'aveugle.

Tout passe par les VRAIS modèles au-dessus du faux Firestore partagé (le
client est le vrai) et d'un faux Cloud Storage qui garde les octets et
REFUSE ce que le service refuse (``tests/_fake_gcs.py``) ; on relit ce qui
est STOCKÉ. Le formulaire web qui s'en sert : ``test_document_edit_route.py``.
"""

import ast
import io
import os
import pathlib
import sys
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
    from models import concurrency
    from models import document as doc
    from models import folder as folder_model
    from models import provenance

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
PDF = b"%PDF-1.7\n" + b"0" * 2048
STALE = [concurrency.STALE_ETAG_ERROR]
DOC_ID = "3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f"
PATH = "documents/doc1"


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, doc, folder_model)


@pytest.fixture
def gcs(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setattr(doc.storage, "bucket", lambda: bucket)
    return bucket


def _seed(db, doc_id="doc1", **over):
    record = {**doc._default_doc(), "id": doc_id, "dossier_id": "d1",
              "display_name": "Lettre", "filename": "lettre.pdf",
              "file_type": "application/pdf", "category": "correspondance",
              "category_source": "juriste", "notes_internes": "Premier",
              "created_at": DT, "updated_at": DT, "etag": "e0"}
    record.update(over)
    db.seed(f"documents/{doc_id}", record)
    return doc_id


_CLIENT_LETTER = {"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]}


def _analysed(db, **over):
    champ, errors = doc._analyse_derivee(_CLIENT_LETTER, document={})
    assert errors == []
    return _seed(db, analyse=champ, category=champ["nature_detectee"],
                 category_source="analyse", **over)


def _rival_at_commit(db, path, **fields):
    """Another process writes *path* at the START of the next commit that
    touches it — after the model's read, before its write lands."""
    def _hook(info):
        if any(p == path for _op, p in info.ops):
            remove()
            db.external_write(path, {**db.peek(path), **fields,
                                     "etag": "e-rival"})

    remove = db.add_commit_hook(_hook)


# ══════════════════════════════════════════════════════════════════════
# 1. La création : une liste blanche, et des refus au lieu de retouches
# ══════════════════════════════════════════════════════════════════════


def test_a_creator_never_persists_a_forged_key(db, gcs):
    forged = {
        "category": "pièce",
        "analyse": {"sous_nature": "JUG_JUGEMENT", "confirme": True},
        "confirme": True, "category_source": "analyse",
        "storage_path": "users/u1/ailleurs/x.pdf", "version": 7,
        "parent_document_id": "autre", "portail_sha512": "f" * 128,
        "etag": "forgé", "id": "forged-id", "created_via": "mcp",
        "file_size": 1, "dossier_id": "d9",
    }
    created, errors = doc.upload_document(
        "d1", "2026-001", io.BytesIO(PDF), "piece.pdf", len(PDF), forged, "u1")

    assert errors == []
    stored = db.peek(f"documents/{created['id']}")
    for key in ("analyse", "confirme"):
        assert key not in stored, key
    assert stored["category"] == "pièce"
    assert stored["category_source"] == "juriste"
    assert stored["storage_path"].startswith(
        f"users/u1/dossiers/d1/documents/{created['id']}/")
    assert stored["version"] == 1 and stored["parent_document_id"] is None
    assert stored["portail_sha512"] == ""
    assert stored["file_size"] == len(PDF) and stored["dossier_id"] == "d1"
    assert created["id"] != "forged-id" and stored["etag"] != "forgé"
    # No request, no override: the path actually writing — never a forged one.
    assert stored["created_via"] == "script"
    assert gcs.objects[stored["storage_path"]].data == PDF


def test_the_portal_provenance_travels_by_its_own_keyword(db, gcs):
    gcs.put("quarantaine/lot/0_piece.pdf", PDF)
    source = gcs.blob("quarantaine/lot/0_piece.pdf")
    source.reload()
    created, errors = doc.ingest_blob_as_document(
        source, "d1", "2026-001", "piece.pdf",
        {"category": "pièce", "portail_lot": "forgé"}, "u1",
        portail={"portail_invitation_id": "inv1", "portail_lot": "b1",
                 "portail_sha512": "a" * 128},
    )
    assert errors == []
    stored = db.peek(f"documents/{created['id']}")
    assert (stored["portail_invitation_id"], stored["portail_lot"],
            stored["portail_sha512"]) == ("inv1", "b1", "a" * 128)


@pytest.mark.parametrize("portail", [
    {"storage_path": "x"},
    {"portail_lot": 3},
    {"portail_lot": "b" * 201},
])
def test_a_portal_provenance_outside_its_three_fields_is_refused(db, gcs,
                                                                 portail):
    gcs.put("q/p.pdf", PDF)
    source = gcs.blob("q/p.pdf")
    source.reload()
    created, errors = doc.ingest_blob_as_document(
        source, "d1", "2026-001", "piece.pdf", {}, "u1", portail=portail)
    assert created is None and errors == ["Provenance du portail invalide."]
    assert db.peek_collection("documents") == {}


@pytest.mark.parametrize("metadata, fragment", [
    ({"display_name": "Lettre <brouillon> finale"}, "chevrons"),
    ({"display_name": "x" * 301}, "300 caractères"),
    ({"notes_internes": "y" * 2001}, "2000 caractères"),
    ({"notes_internes": "si a < b et b > c"}, "chevrons"),
    ({"tags": ["urgent", "<b>gras</b>"]}, "chevrons"),
    ({"tags": ["t"] * 31}, "30 au plus"),
    ({"tags": "urgent"}, "liste de textes"),
    ({"tags": ["urgent", ""]}, "vide"),
    ({"document_date": "14/03/2026"}, "AAAA-MM-JJ"),
    ({"document_date": "2026-02-30"}, "AAAA-MM-JJ"),
    ({"category": "inventée"}, "Catégorie invalide"),
    ({"category": ""}, "Catégorie invalide"),
])
def test_a_creator_refuses_what_it_used_to_mangle(db, gcs, metadata, fragment):
    created, errors = doc.upload_document(
        "d1", "2026-001", io.BytesIO(PDF), "piece.pdf", len(PDF), metadata,
        "u1")
    assert created is None
    assert any(fragment in e for e in errors), errors
    # A refusal names the field, never quotes the value.
    assert not any("brouillon" in e or "gras" in e for e in errors)
    assert db.peek_collection("documents") == {} and gcs.objects == {}


def test_a_creator_never_poses_an_analysis_provenance(db, gcs):
    created, errors = doc.upload_document(
        "d1", "2026-001", io.BytesIO(PDF), "piece.pdf", len(PDF), {}, "u1",
        category_source="analyse")
    assert created is None and errors == ["Provenance de catégorie invalide."]


def test_a_category_claude_chose_is_born_presumed(db, gcs):
    created, errors = doc.upload_document(
        "d1", "2026-001", io.BytesIO(PDF), "piece.pdf", len(PDF),
        {"category": "pièce"}, "u1", category_source="mcp")
    assert errors == []
    assert db.peek(f"documents/{created['id']}")["category_source"] == "mcp"


# ══════════════════════════════════════════════════════════════════════
# 2. update_metadata : partiel, transactionnel, honnête sur la provenance
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("origin", ["analyse", "mcp"])
def test_an_innocuous_save_keeps_a_presumed_category_presumed(db, origin):
    """The edit form ALWAYS posts the category. Posting it unchanged used
    to make it the lawyer's determination — « présumée » vanished on a tag
    edit the lawyer made without looking at the category at all."""
    extra = {} if origin == "mcp" else {
        "analyse": {"sous_nature": "CAB_MEMO", "confirme": False}}
    _seed(db, category="procédure", category_source=origin, **extra)

    # Unpacked by slice on purpose: the proof must fail on the OLD code
    # for what it WROTE, not for its return arity.
    errors = doc.update_metadata(
        "doc1", {"category": "procédure", "tags": ["urgent"]})[1]

    assert errors == []
    stored = db.peek(PATH)
    assert stored["tags"] == ["urgent"]
    assert stored["category_source"] == origin


def test_changing_the_category_makes_it_the_lawyers(db):
    _seed(db, category="procédure", category_source="mcp")
    _saved, errors, _ = doc.update_metadata("doc1", {"category": "preuve"})
    assert errors == []
    stored = db.peek(PATH)
    assert (stored["category"], stored["category_source"]) == (
        "preuve", "juriste")


def test_a_malformed_date_is_refused_and_the_stored_date_kept(db):
    _seed(db, document_date=datetime(2026, 7, 15, tzinfo=UTC))
    db.reset_logs()

    saved, errors = doc.update_metadata(
        "doc1", {"document_date": "2026-02-30", "notes_internes": "x"})[:2]

    assert saved is None and errors == [doc.DOCUMENT_DATE_ERROR]
    assert db.commits == []
    assert db.peek(PATH)["document_date"] == datetime(2026, 7, 15, tzinfo=UTC)


@pytest.mark.parametrize("data, fragment", [
    ({"display_name": "Lettre <v2>"}, "chevrons"),
    ({"notes_internes": "z" * 2001}, "2000 caractères"),
    ({"tags": ["a"] * 31}, "30 au plus"),
])
def test_an_edit_refuses_instead_of_stripping_or_truncating(db, data, fragment):
    _seed(db)
    before = db.peek(PATH)
    saved, errors = doc.update_metadata("doc1", data)[:2]
    assert saved is None
    assert any(fragment in e for e in errors), errors
    assert db.peek(PATH) == before


def test_a_legacy_value_posted_back_untouched_does_not_block_a_save(db):
    """Values are held to the rules where they CHANGE: a legacy name the
    form posts back as it found it is not a new write."""
    _seed(db, display_name="Lettre <v1>", category="procès_verbal")
    _saved, errors, changed = doc.update_metadata("doc1", {
        "display_name": "Lettre <v1>", "category": "procès_verbal",
        "notes_internes": "Ajout"})
    assert errors == [] and changed is True
    stored = db.peek(PATH)
    assert stored["notes_internes"] == "Ajout"
    assert stored["display_name"] == "Lettre <v1>"


def test_a_save_that_changes_nothing_writes_nothing(db):
    _seed(db)
    db.reset_logs()
    saved, errors, changed = doc.update_metadata("doc1", {
        "display_name": "Lettre", "category": "correspondance",
        "notes_internes": "Premier", "tags": [], "document_date": ""},
        expected_etag="e0")
    assert errors == [] and changed is False
    # The transaction still ends with a Commit RPC — carrying no write, as
    # in production (a read-only commit releases the read locks).
    assert all(c.ops == () for c in db.commits)
    assert saved["etag"] == "e0" == db.peek(PATH)["etag"]


def test_a_stored_date_posted_back_is_not_a_change(db):
    """The store hands back a DatetimeWithNanoseconds; the form posts
    « YYYY-MM-DD ». The same day must compare equal, or every save of a
    dated document would write, and churn the etag."""
    _seed(db, document_date=datetime(2026, 7, 15, tzinfo=UTC))
    _saved, errors, changed = doc.update_metadata(
        "doc1", {"document_date": "2026-07-15"})
    assert errors == [] and changed is False


def test_an_edit_never_writes_a_key_outside_its_whitelist(db):
    """The folder moves through move_document; the rest is the machine's."""
    _seed(db)
    before = db.peek(PATH)
    _saved, errors, changed = doc.update_metadata("doc1", {
        "folder_id": "f9", "storage_path": "x", "category_source": "juriste",
        "analyse": {"confirme": True}, "genere_depuis": "forgé",
        "portail_lot": "b"})
    assert errors == [] and changed is False
    assert db.peek(PATH) == before


def test_only_the_changed_keys_and_their_stamp_are_written(db):
    _seed(db)
    before = db.peek(PATH)
    db.reset_logs()

    doc.update_metadata("doc1", {"display_name": "Mise en demeure",
                                 "category": "correspondance"})

    after = db.peek(PATH)
    assert db.commits[-1].ops == (("update", PATH),)
    assert db.commits[-1].transaction is not None
    moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert moved == {"display_name", "updated_at", "etag", "updated_via"}


def test_an_analysis_landing_during_a_metadata_save_survives(db):
    """The lost update: the old full-document set() wrote back the copy
    read before the rival commit — the analysis vanished."""
    _seed(db)
    _rival_at_commit(db, PATH, analyse={"sous_nature": "CAB_MEMO"},
                     category="autre", category_source="analyse")

    saved, errors = doc.update_metadata("doc1", {"display_name": "Nouveau"})[:2]

    assert errors == []
    stored = db.peek(PATH)
    assert stored["display_name"] == "Nouveau"
    assert stored["analyse"] == {"sous_nature": "CAB_MEMO"}
    assert stored["category_source"] == "analyse"
    assert saved["etag"] == stored["etag"]


def test_claude_cannot_pose_a_category_on_an_analysed_document(db):
    _analysed(db)
    before = db.peek(PATH)
    for kwargs in ({"source": "mcp"}, {}):
        with provenance.writing_via("mcp", tool="update_document"):
            saved, errors, changed = doc.update_metadata(
                "doc1", {"category": "preuve"}, **kwargs)
        assert saved is None and errors == [doc.MCP_CATEGORY_ON_ANALYSED]
    assert db.peek(PATH) == before


def test_claude_poses_a_presumed_category_on_an_unanalysed_document(db):
    # REWRITTEN (fixups of lot 3, D18): a LEGACY « correspondance » (no
    # marker) now reads as the lawyer's and is refused — so the record says
    # nobody chose its category (a generation's, an upload's default).
    _seed(db, category_set_by_lawyer=False)
    _saved, errors, _ = doc.update_metadata(
        "doc1", {"category": "preuve"}, source="mcp")
    assert errors == []
    stored = db.peek(PATH)
    assert (stored["category"], stored["category_source"]) == ("preuve", "mcp")


def test_under_the_connector_a_category_is_always_presumed(db):
    """A handler that forgot the keyword — or passed « juriste » — cannot
    make Claude's category read as the lawyer's determination."""
    _seed(db, category_set_by_lawyer=False)   # nobody's choice (D18, lot 3)
    with provenance.writing_via("mcp", tool="update_document"):
        _saved, errors, _ = doc.update_metadata(
            "doc1", {"category": "preuve"}, source="juriste")
    assert errors == []
    stored = db.peek(PATH)
    assert stored["category_source"] == "mcp"
    assert stored["updated_via"] == "mcp"


def test_a_category_provenance_nobody_may_pose_is_refused(db):
    _seed(db)
    saved, errors, _ = doc.update_metadata(
        "doc1", {"category": "preuve"}, source="analyse")
    assert saved is None and errors == ["Provenance de catégorie invalide."]


def test_an_unknown_document_is_refused(db):
    saved, errors, changed = doc.update_metadata("absent", {"tags": ["a"]})
    assert (saved, errors, changed) == (None, ["Document introuvable."], False)


# ══════════════════════════════════════════════════════════════════════
# 3. confirmer_categorie : la seule sortie de « présumée »
# ══════════════════════════════════════════════════════════════════════


def test_confirming_turns_claudes_category_into_the_lawyers(db):
    _seed(db, category="preuve", category_source="mcp")
    before = db.peek(PATH)
    confirmed, errors = doc.confirmer_categorie(
        "doc1", "me@cabinet.ca", expected_etag="e0")
    assert errors == []
    after = db.peek(PATH)
    assert after["category_source"] == "juriste"
    assert after["category_confirmed_by"] == "me@cabinet.ca"
    assert after["category_confirmed_at"] is not None
    assert confirmed["etag"] == after["etag"] != "e0"
    moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    # Gains `category_set_by_lawyer` deliberately (D18, fixups of lot 2A):
    # a confirmed category becomes the lawyer's — the connector may no
    # longer replace it.
    assert moved == {"category_source", "category_confirmed_by",
                     "category_confirmed_at", "updated_at", "etag",
                     "updated_via", "category_set_by_lawyer"}
    assert after["category_set_by_lawyer"] is True


@pytest.mark.parametrize("origin, fragment", [
    ("analyse", "confirmez l'analyse"),
    ("juriste", "Aucune catégorie présumée"),
])
def test_there_is_nothing_to_confirm_outside_a_claude_category(db, origin,
                                                              fragment):
    _seed(db, category_source=origin)
    before = db.peek(PATH)
    confirmed, errors = doc.confirmer_categorie("doc1", "me@cabinet.ca")
    assert confirmed is None and fragment in errors[0]
    assert db.peek(PATH) == before


def test_a_stale_confirmation_confirms_nothing(db):
    _seed(db, category="preuve", category_source="mcp")
    confirmed, errors = doc.confirmer_categorie(
        "doc1", "me@cabinet.ca", expected_etag="perimee")
    assert confirmed is None and errors == STALE
    assert db.peek(PATH)["category_source"] == "mcp"


# ══════════════════════════════════════════════════════════════════════
# 4. Les écrivains de l'analyse et le déplacement : la lecture DANS la
#    transaction, l'écriture partielle
# ══════════════════════════════════════════════════════════════════════


def test_two_parallel_analyses_never_lower_the_protection(db):
    """The non-downgrade floor bypassed by the connector's own parallel
    calls: A records a level-3 analysis while B (level 1) is between its
    read and its write. B used to derive from the SAME stale read, then
    overwrite A — level 3 → 1, no divergence, on a privileged letter."""
    _seed(db)
    champ_a, _ = doc._analyse_derivee(_CLIENT_LETTER, document={})
    assert champ_a["niveau_protection"] == 3
    _rival_at_commit(db, PATH, analyse=champ_a, category_source="analyse",
                     category=champ_a["nature_detectee"])

    maj, errors = doc.record_analyse(
        "doc1", {"sous_nature": "CORR_TIERS", "privileges": []})

    assert errors == []
    stored = db.peek(PATH)["analyse"]
    assert stored["niveau_protection"] == 3
    assert stored["divergence_protection"] is True
    assert stored["niveau_protection_analyse"] == 1
    assert "SECRET_PROFESSIONNEL" in stored["privileges"]
    assert maj["analyse"]["niveau_protection"] == 3
    # Exactly B's journal entry — never one from the aborted attempt.
    journal = db.peek_collection(f"{PATH}/analyses")
    assert len(journal) == 1
    assert list(journal.values())[0]["analyse_id"] == stored["analyse_id"]


def test_the_analysis_derives_from_the_transactional_read(db):
    """The structural pin the parallel test rests on: the document the
    derivation sees is read THROUGH the transaction, never before it."""
    _seed(db)
    db.reset_logs()
    _maj, errors = doc.record_analyse("doc1", {"sous_nature": "CORR_TIERS"})
    assert errors == []
    reads = [r for r in db.reads if PATH in r.paths]
    assert reads and all(r.transactional for r in reads)
    (commit,) = db.commits
    assert ("update", PATH) in commit.ops and ("set", PATH) not in commit.ops


def _analyse_edit(db):
    _analysed(db)
    return lambda: doc.update_analyse("doc1", {"auteur": "Me X"},
                                      par="me@cabinet.ca")


def _analyse_confirm(db):
    _analysed(db)
    return lambda: doc.confirmer_analyse("doc1", "me@cabinet.ca")


def _analyse_record(db):
    _seed(db)
    return lambda: doc.record_analyse("doc1", {"sous_nature": "CORR_TIERS"})


def _document_move(db):
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                           "parent_folder_id": None})
    _seed(db)
    return lambda: doc.move_document("d1", "doc1", "f1")[:2]


def _metadata_edit(db):
    _seed(db)
    return lambda: doc.update_metadata("doc1", {"tags": ["x"]})[:2]


@pytest.mark.parametrize("setup", [
    _analyse_edit, _analyse_confirm, _analyse_record, _document_move,
    _metadata_edit,
], ids=["update_analyse", "confirmer_analyse", "record_analyse",
        "move_document", "update_metadata"])
def test_a_write_landing_at_commit_time_survives(db, setup):
    """Each of the five writers used to set() the whole document from a
    copy read beforehand: the lawyer's note written meanwhile was reverted.
    Now the commit aborts, the retry re-reads, and only the writer's own
    keys are written."""
    call = setup(db)
    _rival_at_commit(db, PATH, notes_internes="Écrit pendant ce temps")

    result, errors = call()

    assert errors == [], errors
    stored = db.peek(PATH)
    assert stored["notes_internes"] == "Écrit pendant ce temps"
    assert result["etag"] == stored["etag"] != "e-rival"


def test_a_move_to_the_folder_it_is_in_writes_nothing(db):
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces"})
    _seed(db, folder_id="f1")
    db.reset_logs()
    moved, errors, changed = doc.move_document("d1", "doc1", "f1",
                                               expected_etag="e0")
    assert errors == [] and changed is False
    assert all(c.ops == () for c in db.commits)
    assert moved["etag"] == "e0"


@pytest.mark.parametrize("folder, fragment", [
    ({"id": "f2", "dossier_id": "autre", "name": "X"}, "destination"),
    (None, "destination"),
])
def test_a_move_into_a_folder_that_is_not_this_dossiers_is_refused(
    db, folder, fragment,
):
    if folder:
        db.seed("folders/f2", folder)
    _seed(db)
    before = db.peek(PATH)
    moved, errors, changed = doc.move_document("d1", "doc1", "f2")
    assert moved is None and changed is False and fragment in errors[0]
    assert db.peek(PATH) == before


def test_a_move_of_another_dossiers_document_is_refused(db):
    _seed(db, dossier_id="d2")
    moved, errors, _ = doc.move_document("d1", "doc1", None)
    assert moved is None and "n'appartient pas" in errors[0]


# ══════════════════════════════════════════════════════════════════════
# 5. ingest_blob_as_document : un identifiant réservé, et jamais la
#    suppression d'un objet qu'un autre appel a commis
# ══════════════════════════════════════════════════════════════════════

_STAGING = f"staging/u1/{DOC_ID}/piece.pdf"
_CANONICAL = f"users/u1/dossiers/d1/documents/{DOC_ID}/piece.pdf"


def _staged(gcs, data=PDF):
    gcs.put(_STAGING, data, content_type="application/octet-stream")
    source = gcs.blob(_STAGING)
    source.reload()
    return source


def _ingest(source, **kw):
    return doc.ingest_blob_as_document(
        source, "d1", "2026-001", "piece.pdf", {"category": "pièce"}, "u1",
        document_id=DOC_ID, **kw)


def test_two_finalizations_of_one_upload_make_one_document(db, gcs):
    source = _staged(gcs)
    first, errors = _ingest(source)
    assert errors == [] and first["id"] == DOC_ID
    generation = gcs.objects[_CANONICAL].generation

    again, errors = _ingest(source)

    assert errors == [] and again["id"] == DOC_ID
    assert list(db.peek_collection("documents")) == [DOC_ID]
    assert gcs.objects[_CANONICAL].generation == generation   # never rewritten
    assert gcs.objects[_CANONICAL].content_type == "application/pdf"


def _complete_first_call_during_rewrite(db, gcs, *, then_raise=None):
    """T1 runs to completion WHILE T2 is inside its rewrite: T1's copy and
    record commit, then T1's route consumes the staging object."""
    source_t1 = gcs.blob(_STAGING)
    source_t1.reload()

    def _hook(dest_name, _source_name):
        if dest_name != _CANONICAL:
            return
        gcs.rewrite_hooks.remove(_hook)
        done, errors = _ingest(source_t1)
        assert errors == [] and done["id"] == DOC_ID
        gcs.remove(_STAGING)
        if then_raise is not None:
            raise then_raise

    gcs.rewrite_hooks.append(_hook)


@pytest.mark.parametrize("failure", ["precondition", "source_gone"])
def test_a_concurrent_finalization_never_deletes_the_committed_bytes(
    db, gcs, failure,
):
    """The review's worst case: T2 passed its pre-read before T1
    committed. Whether T2's rewrite answers 412 (the canonical object
    exists) or 404 (T1 consumed the staging object), T2 must leave T1's
    object and record intact — and answer with T1's document."""
    from google.api_core.exceptions import NotFound

    source_t2 = _staged(gcs)
    _complete_first_call_during_rewrite(
        db, gcs,
        then_raise=NotFound("404") if failure == "source_gone" else None)

    got, errors = _ingest(source_t2)

    assert errors == [] and got["id"] == DOC_ID
    assert gcs.objects[_CANONICAL].data == PDF
    assert list(db.peek_collection("documents")) == [DOC_ID]
    assert db.peek(f"documents/{DOC_ID}")["storage_path"] == _CANONICAL


def test_a_first_attempt_that_died_after_its_copy_is_adopted(db, gcs):
    """The copy landed, the record never did (a SIGKILL in between): the
    retry adopts the identical object instead of refusing for ever."""
    source = _staged(gcs)
    gcs.put(_CANONICAL, PDF)
    generation = gcs.objects[_CANONICAL].generation

    got, errors = _ingest(source)

    assert errors == [] and got["id"] == DOC_ID
    assert gcs.objects[_CANONICAL].generation == generation
    assert gcs.objects[_CANONICAL].content_type == "application/pdf"
    assert db.peek(f"documents/{DOC_ID}")["file_size"] == len(PDF)


def test_an_occupied_destination_is_refused_and_left_alone(db, gcs):
    source = _staged(gcs)
    other = gcs.put(_CANONICAL, b"%PDF-1.4 autre chose")

    got, errors = _ingest(source)

    assert got is None and errors == [doc._INGEST_OCCUPIED]
    assert gcs.objects[_CANONICAL] is other
    assert gcs.objects[_CANONICAL].data == b"%PDF-1.4 autre chose"
    assert db.peek_collection("documents") == {}


def test_an_id_owned_by_another_document_is_never_overwritten(db, gcs):
    source = _staged(gcs)
    _seed(db, doc_id=DOC_ID, dossier_id="d2", file_size=5)
    before = db.peek(f"documents/{DOC_ID}")

    got, errors = _ingest(source)

    assert got is None and errors == [doc._INGEST_CONFLICT]
    assert db.peek(f"documents/{DOC_ID}") == before
    assert _CANONICAL not in gcs.objects


def test_a_failed_record_write_removes_only_this_calls_copy(db, gcs):
    source = _staged(gcs)

    def _fail(info):
        if any(p == f"documents/{DOC_ID}" for _op, p in info.ops):
            raise RuntimeError("firestore down")

    db.add_commit_hook(_fail)
    got, errors = _ingest(source)

    assert got is None and errors == [doc._INGEST_FAILED]
    assert _CANONICAL not in gcs.objects          # this call's copy, removed
    assert gcs.objects[_STAGING].data == PDF      # the source: the caller's


def test_a_rewrite_in_several_passes_repeats_its_precondition(db, gcs):
    gcs.rewrite_chunk = 1024
    source = _staged(gcs)
    got, errors = _ingest(source)
    assert errors == [] and gcs.objects[_CANONICAL].data == PDF


@pytest.mark.parametrize("bad", [
    "exports", "3F2B8C1E-9A4D-4C6B-8E2F-1A2B3C4D5E6F",
    "3f2b8c1e-9a4d-1c6b-8e2f-1a2b3c4d5e6f", "{3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f}",
    "", "../x",
])
def test_a_reserved_id_that_is_not_a_canonical_uuid4_is_refused(db, gcs, bad):
    source = _staged(gcs)
    db.reset_logs()
    got, errors = doc.ingest_blob_as_document(
        source, "d1", "2026-001", "piece.pdf", {}, "u1", document_id=bad)
    assert got is None and errors == [doc.INVALID_DOCUMENT_ID]
    assert db.reads == [] and list(gcs.objects) == [_STAGING]


def test_is_canonical_uuid4():
    import uuid
    assert doc.is_canonical_uuid4(str(uuid.uuid4()))
    for bad in (str(uuid.uuid1()), str(uuid.uuid4()).upper(), "x", None, 4,
                "urn:uuid:" + str(uuid.uuid4())):
        assert not doc.is_canonical_uuid4(bad), bad


# ══════════════════════════════════════════════════════════════════════
# 6. Balayage
# ══════════════════════════════════════════════════════════════════════


def test_no_document_writer_sets_a_whole_document_any_more():
    """The five writers converted in this step write partial updates only;
    a full-document set() creeping back reopens the lost update."""
    tree = ast.parse(pathlib.Path(doc.__file__).read_text(encoding="utf-8"))
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name in ("update_metadata", "record_analyse",
                              "update_analyse", "confirmer_analyse",
                              "move_document", "confirmer_categorie")):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "set":
                # Only the journal entry: transaction.set(<analyses ref>, champ).
                assert fn.name in ("record_analyse", "update_analyse"), fn.name
                assert "ANALYSES_SUBCOLLECTION" in ast.unparse(node.args[0])


# ══════════════════════════════════════════════════════════════════════
# 7. Revue adversariale de T1 (2026-09-27) — chacun échoue sur 04722bb
# ══════════════════════════════════════════════════════════════════════
#
# Two failure branches of ingest_blob_as_document deleted THIS call's
# generation without asking whether a COMMITTED record pointed at it — its
# docstring promised « never when a committed record references it ». The
# refusal of « < … > » also wrote the chevrons into its own message, which
# the pages that show a refusal on ?erreur= sanitize — stripping the
# explanation itself.


def _lose_the_answer_of_the_create(db, monkeypatch):
    """The record's commit LANDS, then the call raises: the answer was lost
    (a reset connection after the server committed)."""
    server = db._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(
                server._write_name(w).endswith(f"documents/{DOC_ID}")
                for w in writes):
            state["armed"] = False
            raise RuntimeError("connection reset after the commit")
        return response

    monkeypatch.setattr(server, "commit", _commit)


def test_a_record_commit_whose_answer_is_lost_keeps_its_bytes(
    db, gcs, monkeypatch,
):
    source = _staged(gcs)
    _lose_the_answer_of_the_create(db, monkeypatch)

    got, errors = _ingest(source)

    # The record committed: its bytes must survive, and it IS the answer.
    stored = db.peek(f"documents/{DOC_ID}")
    assert stored is not None and stored["storage_path"] == _CANONICAL
    assert gcs.objects[_CANONICAL].data == PDF
    assert errors == [] and got["id"] == DOC_ID


def test_a_patch_failure_after_a_concurrent_adoption_keeps_its_bytes(
    db, gcs, monkeypatch,
):
    """T1 wrote the copy; T2 (a second finalization of the same upload) got
    412 on its rewrite, found the same bytes, ADOPTED them and committed its
    record — then T1's patch failed. T1 used to delete the generation it
    wrote: T2's committed document lost its bytes, in silence."""
    from google.api_core.exceptions import ServiceUnavailable

    from tests._fake_gcs import FakeBlob

    source_t1 = _staged(gcs)
    real_patch = FakeBlob.patch
    calls = {"n": 0}

    def _patch(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:                      # T1's patch
            source_t2 = gcs.blob(_STAGING)
            source_t2.reload()
            adopted, errs = _ingest(source_t2)   # T2: 412 → adopt → commit
            assert errs == [] and adopted["id"] == DOC_ID
            raise ServiceUnavailable("503 patch")
        return real_patch(self, *args, **kwargs)

    monkeypatch.setattr(FakeBlob, "patch", _patch)

    got, errors = _ingest(source_t1)

    assert gcs.objects[_CANONICAL].data == PDF
    assert db.peek(f"documents/{DOC_ID}")["storage_path"] == _CANONICAL
    assert errors == [] and got["id"] == DOC_ID


def test_an_unreadable_record_keeps_the_copy_on_rollback(db, gcs, monkeypatch):
    """The check itself failing is not « nobody references it »: the copy
    is kept (an orphan costs storage, a record without bytes a document)."""
    source = _staged(gcs)

    def _fail(info):
        if any(p == f"documents/{DOC_ID}" for _op, p in info.ops):
            raise RuntimeError("firestore down")

    db.add_commit_hook(_fail)
    server = db._fake_server
    real_get = server.batch_get_documents
    state = {"reads": 0}

    def _get(request, metadata=None, **kwargs):
        state["reads"] += 1
        if state["reads"] > 1:        # the pre-read passes, the check fails
            raise RuntimeError("firestore unreadable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)

    got, errors = _ingest(source)

    assert got is None and errors == [doc._INGEST_FAILED]
    assert gcs.objects[_CANONICAL].data == PDF


def test_a_chevron_refusal_survives_the_pages_that_display_it():
    """Réception and the document page sanitize ?erreur= before display. A
    message quoting « < … > » had its own explanation stripped there."""
    from security import sanitize

    (message,) = doc._text_errors("Nom d'affichage", "Lettre <brouillon>", 300)
    assert "chevrons" in message
    assert sanitize(message, max_length=300) == message


def test_record_metadata_errors_is_the_creators_judgement(db, gcs):
    """The pure check the upload form runs BEFORE its first byte is exactly
    the creator's: what one refuses, the other refuses."""
    for metadata in ({"display_name": "Lettre <brouillon>"},
                     {"display_name": "x" * 301},
                     {"tags": ["t"] * 31},
                     {"category": "inventée"},
                     {"document_date": "15/07/2026"}):
        errors = doc.record_metadata_errors(metadata)
        assert errors, metadata
        source = _staged(gcs)
        got, creator_errors = doc.ingest_blob_as_document(
            source, "d1", "2026-001", "piece.pdf", metadata, "u1")
        assert got is None and creator_errors == errors, metadata
    assert doc.record_metadata_errors(
        {"display_name": "Lettre", "tags": ["a"], "category": "pièce",
         "document_date": "2026-07-15"}) == []


def _retry_after_a_lost_answer(db, monkeypatch):
    """What the client's commit retry (ServiceUnavailable is retried) does
    when the first attempt LANDED and only its answer was lost: the retry
    re-sends the same create(), which the server now answers AlreadyExists.
    The fake sits below the GAPIC retry wrapper, so the retry is played
    here, verbatim."""
    server = db._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(
                "/documents/" in server._write_name(w) for w in writes):
            state["armed"] = False
            real_commit(request, metadata=metadata, **kwargs)   # it landed
        return real_commit(request, metadata=metadata, **kwargs)  # the retry

    monkeypatch.setattr(server, "commit", _commit)


def test_a_generated_document_whose_create_was_retried_keeps_its_bytes(
    db, gcs, monkeypatch,
):
    """upload_document switched from set() to create() in T1: a retried
    commit, harmless with set(), now answers AlreadyExists — and the
    rollback deleted the file under the committed record."""
    _retry_after_a_lost_answer(db, monkeypatch)

    got, errors = doc.upload_document(
        "d1", "2026-001", io.BytesIO(PDF), "projet.pdf", len(PDF),
        {"category": "correspondance"}, "u1")

    assert errors == [], errors
    stored = db.peek(f"documents/{got['id']}")
    assert stored["storage_path"] == got["storage_path"]
    assert gcs.objects[got["storage_path"]].data == PDF


def test_an_ingestion_whose_create_was_retried_keeps_its_bytes(
    db, gcs, monkeypatch,
):
    source = _staged(gcs)
    _retry_after_a_lost_answer(db, monkeypatch)

    got, errors = _ingest(source)

    assert errors == [] and got["id"] == DOC_ID
    assert gcs.objects[_CANONICAL].data == PDF
    assert db.peek(f"documents/{DOC_ID}")["storage_path"] == _CANONICAL
