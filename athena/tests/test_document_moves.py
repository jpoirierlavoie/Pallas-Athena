"""Déplacer et reclasser des documents : le modèle, durci avant que le
connecteur ne l'atteigne (plan, lot 2A — étape T7, 2026-09-27).

Les outils ``update_document``, ``move_documents`` et ``manage_folder``
passent par les fonctions ci-dessous ; chaque test marqué « échoue sur
l'ancien code » épingle un défaut réel du code d'avant :

* ``move_documents_bulk`` lisait chaque document HORS transaction par le
  lecteur ouvert ``get_document`` — une lecture en panne se lisait
  « introuvable » pendant que les autres lignes partaient —, réécrivait une
  ligne déjà rangée là (un etag neuf pour rien), vérifiait le dossier cible
  une fois, AVANT : supprimé entre-temps, il laissait des documents sous un
  ``folder_id`` mort (dans aucun dossier, à aucune racine) ; et répondait
  par des chaînes libres ;
* un identifiant à barre oblique, que le client Firestore joint puis
  recoupe, atteignait un enregistrement PLUS PROFOND : ``update_metadata``
  et ``record_analyse`` écrivaient sur une entrée du journal des analyses,
  que rien ne devait jamais réécrire.

Tout passe par les VRAIS modèles au-dessus du faux Firestore partagé (le
client est le vrai) ; on relit ce qui est STOCKÉ.
"""

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import document as doc
    from models import folder as folder_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, doc, folder_model)


def _seed(db, doc_id, *, dossier="d1", folder=None, **over):
    record = {**doc._default_doc(), "id": doc_id, "dossier_id": dossier,
              "display_name": f"Pièce {doc_id}", "category": "pièce",
              "folder_id": folder, "created_at": DT, "updated_at": DT,
              "etag": f"e-{doc_id}"}
    record.update(over)
    db.seed(f"documents/{doc_id}", record)
    return doc_id


def _folder(db, fid, *, dossier="d1", name=None, parent=None, **over):
    db.seed(f"folders/{fid}", {"id": fid, "dossier_id": dossier,
                               "name": name or fid, "parent_folder_id": parent,
                               "order": 0, "etag": f"e-{fid}", **over})
    return fid


def _writes(db) -> list:
    return [op for c in db.commits for op in c.ops]


def _delete_at_commit(db, doomed: str, touching: str) -> None:
    """Another process deletes *doomed* at the start of the next commit
    that touches *touching* — after the model's read, before its write."""
    def _hook(info):
        if any(p == touching for _op, p in info.ops):
            remove()
            db.external_delete(doomed)

    remove = db.add_commit_hook(_hook)


# ══════════════════════════════════════════════════════════════════════
# 1. move_documents_bulk
# ══════════════════════════════════════════════════════════════════════


def test_a_bulk_move_answers_each_row_in_request_order(db):
    _folder(db, "f1")
    _seed(db, "a")
    _seed(db, "b", folder="f1")
    _seed(db, "c", dossier="d2")

    rows, errors = doc.move_documents_bulk("d1", ["a", "absent", "b", "c"], "f1")

    assert errors == []
    assert [(r["id"], r["outcome"]) for r in rows] == [
        ("a", "moved"), ("absent", "refused"), ("b", "unchanged"),
        ("c", "refused")]
    assert rows[1]["reason"] == doc.MOVE_NOT_FOUND
    assert rows[3]["reason"] == doc.MOVE_OTHER_DOSSIER
    assert rows[0]["previous_folder_id"] is None
    assert rows[2]["previous_folder_id"] == "f1"
    assert rows[1]["doc"] is None and rows[3]["doc"] is None
    stored = db.peek("documents/a")
    assert stored["folder_id"] == "f1" and stored["etag"] != "e-a"
    assert rows[0]["doc"]["etag"] == stored["etag"]
    assert db.peek("documents/c")["folder_id"] is None


def test_the_moved_rows_commit_together_in_one_transaction(db):
    _folder(db, "f1")
    for i in ("a", "b", "c"):
        _seed(db, i)
    db.reset_logs()

    rows, errors = doc.move_documents_bulk("d1", ["a", "b", "c"], "f1")

    assert errors == [] and {r["outcome"] for r in rows} == {"moved"}
    written = [c for c in db.commits if c.ops]
    assert len(written) == 1 and written[0].transaction is not None
    assert sorted(p for _k, p in written[0].ops) == [
        "documents/a", "documents/b", "documents/c"]
    # One etag PER ROW: each document's etag is its own token.
    etags = {db.peek(f"documents/{i}")["etag"] for i in ("a", "b", "c")}
    assert len(etags) == 3


def test_a_row_already_in_the_target_is_not_rewritten(db):
    """Échoue sur l'ancien code : il réécrivait la ligne — un etag neuf
    pour rien, et chaque onglet ouvert sur ce document devenait périmé."""
    _folder(db, "f1")
    _seed(db, "a", folder="f1")
    db.reset_logs()

    rows, errors = doc.move_documents_bulk("d1", ["a"], "f1")

    assert errors == [] and rows[0]["outcome"] == "unchanged"
    assert db.peek("documents/a")["etag"] == "e-a"
    assert _writes(db) == []


def test_a_target_folder_deleted_before_the_commit_moves_nothing(db):
    """Échoue sur l'ancien code : le dossier cible était vérifié une fois,
    avant ; supprimé entre la vérification et le commit, il laissait le
    document sous un folder_id MORT — dans aucun dossier, à aucune racine.
    Lu désormais DANS la transaction : le commit avorte, la reprise ne le
    trouve plus, rien ne bouge."""
    _folder(db, "f1")
    _seed(db, "a")
    _delete_at_commit(db, "folders/f1", "documents/a")

    rows, errors = doc.move_documents_bulk("d1", ["a"], "f1")

    assert rows == [] and errors == [doc.TARGET_FOLDER_NOT_FOUND]
    assert db.peek("documents/a")["folder_id"] is None
    assert db.peek("folders/f1") is None


def test_an_unreadable_row_refuses_the_whole_batch(db, monkeypatch):
    """Échoue sur l'ancien code : une lecture en panne se lisait
    « introuvable » pendant que les autres lignes partaient — un lot à
    moitié fait, annoncé comme un succès partiel ordinaire."""
    _folder(db, "f1")
    _seed(db, "a")
    _seed(db, "bad")
    server = db._fake_server
    real = server.batch_get_documents

    def _flaky(request, *a, **kw):
        if any(str(n).endswith("/documents/bad") for n in request["documents"]):
            raise gexc.ServiceUnavailable("firestore indisponible")
        return real(request, *a, **kw)

    monkeypatch.setattr(server, "batch_get_documents", _flaky)
    db.reset_logs()

    rows, errors = doc.move_documents_bulk("d1", ["a", "bad"], "f1")

    assert rows == [] and errors and "Rien n'a été déplacé" in errors[0]
    assert db.peek("documents/a")["folder_id"] is None
    assert _writes(db) == []


def test_an_id_named_twice_and_an_oversized_batch_are_refused(db):
    _seed(db, "a")
    assert doc.move_documents_bulk("d1", ["a", "a"], None) == (
        [], [doc.MOVE_DUPLICATE_IDS])
    ids = [f"x{i}" for i in range(doc.MOVE_BULK_MAX + 1)]
    rows, errors = doc.move_documents_bulk("d1", ids, None)
    assert rows == [] and str(doc.MOVE_BULK_MAX) in errors[0]
    assert db.commits == []


def test_a_bulk_move_to_the_root_and_into_another_dossiers_folder(db):
    _folder(db, "f1")
    _folder(db, "g1", dossier="d2")
    _seed(db, "a", folder="f1")

    rows, errors = doc.move_documents_bulk("d1", ["a"], "g1")
    assert rows == [] and errors == [doc.TARGET_FOLDER_NOT_FOUND]
    assert db.peek("documents/a")["folder_id"] == "f1"

    rows, errors = doc.move_documents_bulk("d1", ["a"], "")
    assert errors == [] and rows[0]["outcome"] == "moved"
    assert db.peek("documents/a")["folder_id"] is None


# ══════════════════════════════════════════════════════════════════════
# 2. update_metadata(folder_id=…) — renommer ET reclasser, UN commit
# ══════════════════════════════════════════════════════════════════════


def test_a_rename_and_a_refile_share_one_etag_check_and_one_commit(db):
    _folder(db, "f1")
    _seed(db, "a")
    db.reset_logs()

    saved, errors, changed = doc.update_metadata(
        "a", {"display_name": "Mise en demeure"}, expected_etag="e-a",
        folder_id="f1")

    assert errors == [] and changed is True
    stored = db.peek("documents/a")
    assert stored["display_name"] == "Mise en demeure"
    assert stored["folder_id"] == "f1"
    assert saved["etag"] == stored["etag"] != "e-a"
    written = [c for c in db.commits if c.ops]
    assert len(written) == 1 and written[0].transaction is not None


def test_a_refile_into_another_dossiers_folder_writes_nothing(db):
    _folder(db, "g1", dossier="d2")
    _seed(db, "a")
    before = db.peek("documents/a")

    saved, errors, changed = doc.update_metadata(
        "a", {"display_name": "Nouveau"}, folder_id="g1")

    assert saved is None and changed is False
    assert errors == [doc.TARGET_FOLDER_NOT_FOUND]
    assert db.peek("documents/a") == before


def test_a_refile_whose_folder_vanishes_at_commit_writes_nothing(db):
    _folder(db, "f1")
    _seed(db, "a")
    _delete_at_commit(db, "folders/f1", "documents/a")

    saved, errors, changed = doc.update_metadata(
        "a", {"tags": ["x"]}, folder_id="f1")

    assert saved is None and errors == [doc.TARGET_FOLDER_NOT_FOUND]
    assert db.peek("documents/a")["tags"] == []
    assert db.peek("documents/a")["folder_id"] is None


def test_a_refile_to_where_it_already_is_writes_nothing(db):
    _folder(db, "f1")
    _seed(db, "a", folder="f1")
    db.reset_logs()

    _saved, errors, changed = doc.update_metadata("a", {}, folder_id="f1")
    assert errors == [] and changed is False
    _saved, errors, changed = doc.update_metadata("a", {}, folder_id="")
    assert errors == [] and changed is True
    assert db.peek("documents/a")["folder_id"] is None


def test_the_form_data_still_cannot_move_a_document(db):
    """The folder moves through the KEYWORD only: a `folder_id` in *data*
    (a forged web form) stays outside the whitelist and is ignored."""
    _folder(db, "f1")
    _seed(db, "a")
    _saved, errors, changed = doc.update_metadata("a", {"folder_id": "f1"})
    assert errors == [] and changed is False
    assert db.peek("documents/a")["folder_id"] is None


# ══════════════════════════════════════════════════════════════════════
# 3. Un identifiant à barre oblique est une ABSENCE, jamais un chemin
# ══════════════════════════════════════════════════════════════════════

_JOURNAL = "documents/a/analyses/j1"


def _journal(db):
    _seed(db, "a")
    db.seed(_JOURNAL, {"analyse_id": "j1", "sous_nature": "CAB_MEMO",
                       "genere_le": DT})
    return dict(db.peek(_JOURNAL))


def test_an_edit_never_reaches_a_journal_entry_through_a_slashed_id(db):
    """Échoue sur l'ancien code : `document("a/analyses/j1")` désigne une
    entrée du journal des analyses — write-once — et update_metadata y
    posait le nom d'affichage."""
    before = _journal(db)

    saved, errors, changed = doc.update_metadata(
        "a/analyses/j1", {"display_name": "Écrasé"})

    assert saved is None and changed is False
    assert errors == ["Document introuvable."]
    assert db.peek(_JOURNAL) == before


def test_an_analysis_never_lands_on_a_journal_entry_through_a_slashed_id(db):
    """Échoue sur l'ancien code : record_analyse lisait l'entrée du journal
    comme un document, lui écrivait un cache d'analyse et lui ouvrait un
    journal à elle."""
    before = _journal(db)

    saved, errors = doc.record_analyse(
        "a/analyses/j1", {"sous_nature": "CAB_MEMO"})

    assert saved is None and errors == ["Document introuvable."]
    assert db.peek(_JOURNAL) == before
    assert db.peek_collection(f"{_JOURNAL}/analyses") == {}


def test_a_move_and_a_bulk_move_treat_a_slashed_id_as_absent(db):
    before = _journal(db)
    moved, errors, _ = doc.move_document("d1", "a/analyses/j1", None)
    assert moved is None and errors == ["Document introuvable."]
    rows, errors = doc.move_documents_bulk("d1", ["a/analyses/j1"], None)
    assert errors == [] and rows[0]["outcome"] == "refused"
    assert rows[0]["reason"] == doc.MOVE_NOT_FOUND
    assert db.peek(_JOURNAL) == before


def test_the_strict_reader_propagates_and_never_reads_a_slashed_id(db, monkeypatch):
    _seed(db, "a")
    assert doc.get_document_strict("a")["id"] == "a"
    assert doc.get_document_strict("absent") is None
    db.reset_logs()
    assert doc.get_document_strict("a/analyses/j1") is None
    assert doc.get_document_strict("") is None
    assert db.reads == []

    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(db._fake_server, "batch_get_documents", _boom)
    with pytest.raises(gexc.ServiceUnavailable):
        doc.get_document_strict("a")
    assert doc.get_document("a") is None  # the fail-open one, unchanged


def test_is_addressable_id():
    assert doc.is_addressable_id("3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f")
    for bad in ("", "a/b", "a/b/c", None, 4, ["a"]):
        assert not doc.is_addressable_id(bad), bad


# ══════════════════════════════════════════════════════════════════════
# 4. find_folder_named — ce que la réutilisation reprend est ce que la
#    règle de doublon refuse
# ══════════════════════════════════════════════════════════════════════


def test_find_folder_named_folds_like_the_duplicate_rule():
    folders = [
        {"id": "r", "name": "Pièces", "parent_folder_id": None},
        {"id": "s", "name": "Pièces", "parent_folder_id": "r"},
    ]
    assert folder_model.find_folder_named(folders, None, "  pièces ")["id"] == "r"
    # A decomposed spelling is the SAME name (the T2 review rule).
    assert folder_model.find_folder_named(folders, "r", "Pièces")["id"] == "s"
    assert folder_model.find_folder_named(folders, None, "Expertises") is None
    assert folder_model.find_folder_named(folders, "r", "") is None
    assert folder_model._name_taken(folders, None, "PIÈCES") is True


# ══════════════════════════════════════════════════════════════════════
# 5. La route web orpheline /documents/move-bulk suit la nouvelle forme
# ══════════════════════════════════════════════════════════════════════


def test_the_orphan_bulk_move_route_follows_the_new_shape(db):
    from flask import Flask

    import routes.documents as rd

    _folder(db, "f1")
    _seed(db, "a")
    app = Flask(__name__)
    app.config.update(SECRET_KEY="t", TESTING=True)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(UTC) + timedelta(hours=1)

    # A checkbox posted twice means it once — deduplicated by the route.
    resp = client.post("/documents/move-bulk", data={
        "dossier_id": "d1", "target_folder_id": "f1",
        "document_ids": ["a", "a"]})
    assert resp.status_code == 302
    assert db.peek("documents/a")["folder_id"] == "f1"
