"""La suppression d'un dossier (models/dossier.delete_dossier, 2026-09-30).

Depuis l'arborescence par défaut, TOUT dossier porte dix-huit dossiers de
classement dès sa création. L'ancienne suppression comptait « folders »
parmi les enfants qui bloquent : elle aurait rendu chaque nouveau dossier
insupprimable. Et elle lisait ses enfants HORS de toute transaction, puis
supprimait le dossier par un ``delete()`` nu : un document versé entre les
deux restait rattaché à un dossier disparu.

Ce que ces tests exigent, sur le faux Firestore partagé (le VRAI client) :

* un dossier vide — ses dix-huit dossiers de classement et un dossier fait
  main compris — part en UN seul commit, avec tous ses dossiers de
  classement, les enfants avant leurs parents ;
* chaque collection d'enfants qui reste bloque, et le refus n'écrit rien ;
* une lecture impossible refuse (« lecture »), rien d'écrit ;
* un document arrivé entre les lectures et le commit fait refuser : jamais
  un dossier de classement sans son dossier, jamais un document orphelin.
"""

import os
import sys
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import dossier as dossier_model
    from models import folder as folder_model

from tests._fake_firestore import install  # noqa: E402


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def did(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    report, errors = folder_model.ensure_default_tree(doc["id"])
    assert errors == [] and report is not None and not report.blocked
    return doc["id"]


def _folders_of(db, did) -> dict:
    return {k: v for k, v in db.peek_collection("folders").items()
            if v.get("dossier_id") == did}


def _fail_queries_on(monkeypatch, db, collection):
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == collection for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)


# ══════════════════════════════════════════════════════════════════════
# 1. Un dossier vide part avec ses dossiers de classement
# ══════════════════════════════════════════════════════════════════════


def test_an_empty_dossier_leaves_with_its_whole_tree_in_one_commit(db, did):
    """Régression — « folders » bloquait : ce dossier, qui ne contient que
    son arborescence par défaut et un dossier fait main, était refusé."""
    user_folder, errors = folder_model.create_folder(
        did, "Mes brouillons", folder_model.node_folder_id(did, "interne"))
    assert errors == [], errors
    before = _folders_of(db, did)
    assert len(before) == 19
    db.reset_logs()

    ok, message, report = dossier_model.delete_dossier(did)

    assert ok is True and message == ""
    assert report["code"] == ""
    assert report["dossier"]["file_number"] == "2026-001"
    assert db.peek(f"dossiers/{did}") is None
    assert _folders_of(db, did) == {}
    assert len(db.commits) == 1
    deleted = {p for kind, p in db.commits[0].ops if kind == "delete"}
    assert deleted == ({f"folders/{fid}" for fid in before}
                       | {f"dossiers/{did}"})
    # Children before their parents, the ids reported.
    order = report["folders"]
    assert set(order) == set(before) and user_folder["id"] in order
    for fid, f in before.items():
        parent = f.get("parent_folder_id")
        if parent:
            assert order.index(fid) < order.index(parent)


def test_every_read_is_made_inside_the_deleting_transaction(db, did):
    db.reset_logs()
    ok, _m, _r = dossier_model.delete_dossier(did)
    assert ok is True
    assert db.reads_outside_transactions() == []


def test_a_dossier_without_folders_is_deleted_too(db):
    doc, errors = dossier_model.create_dossier(
        {"file_number": "2026-009", "title": "Sans arborescence",
         "clients": [{"id": "p1", "name": "Jean Tremblay"}]})
    assert errors == []
    ok, _m, report = dossier_model.delete_dossier(doc["id"])
    assert ok is True and report["folders"] == []
    assert db.peek(f"dossiers/{doc['id']}") is None


def test_folders_are_no_longer_a_blocking_child():
    """The trap, pinned: a folder is the filing tree, not content."""
    names = [c for c, *_ in dossier_model._CHILD_COLLECTIONS]
    assert "folders" not in names
    assert "trust_transactions" in names and "documents" in names


# ══════════════════════════════════════════════════════════════════════
# 2. Ce qui bloque, bloque — et le refus n'écrit rien
# ══════════════════════════════════════════════════════════════════════


def test_a_dossier_holding_a_document_is_refused_and_keeps_its_folders(db, did):
    db.seed("documents/doc1", {
        "id": "doc1", "dossier_id": did,
        "folder_id": folder_model.node_folder_id(did, "pieces"),
    })
    folders = _folders_of(db, did)
    db.reset_logs()

    ok, message, report = dossier_model.delete_dossier(did)

    assert ok is False and report["code"] == "contenu"
    assert "documents" in message
    assert report["dossier"] is None and report["folders"] == []
    assert db.commits == []
    assert db.peek(f"dossiers/{did}") is not None
    assert _folders_of(db, did) == folders


@pytest.mark.parametrize(
    "collection", [c for c, *_ in dossier_model._CHILD_COLLECTIONS])
def test_each_remaining_child_collection_refuses(db, did, collection):
    db.seed(f"{collection}/x1", {"id": "x1", "dossier_id": did})
    db.reset_logs()
    ok, _message, report = dossier_model.delete_dossier(did)
    assert ok is False and report["code"] == "contenu"
    assert db.commits == []
    assert db.peek(f"dossiers/{did}") is not None
    assert len(_folders_of(db, did)) == 18


def test_a_trust_history_still_blocks_forever(db, did):
    """Phase K: the register is permanent, even at a zero balance."""
    db.seed("trust_transactions/t1", {
        "id": "t1", "dossier_id": did, "amount": 0, "status": "annulée"})
    ok, message, report = dossier_model.delete_dossier(did)
    assert ok is False and report["code"] == "contenu"
    assert "opérations fiduciaires" in message


def test_another_dossiers_children_do_not_block(db, did):
    db.seed("documents/other", {"id": "other", "dossier_id": "d-autre"})
    ok, _m, _r = dossier_model.delete_dossier(did)
    assert ok is True
    assert db.peek("documents/other") is not None


def test_an_unknown_dossier_is_introuvable_and_writes_nothing(db):
    db.reset_logs()
    ok, message, report = dossier_model.delete_dossier(
        "a0000000-0000-4000-8000-000000000000")
    assert ok is False and report["code"] == "introuvable"
    assert message == "Dossier introuvable."
    assert db.commits == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_id_is_introuvable_without_any_read(db, blank):
    db.reset_logs()
    ok, _m, report = dossier_model.delete_dossier(blank)
    assert ok is False and report["code"] == "introuvable"
    assert db.reads == [] and db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. Une lecture impossible refuse (fail closed)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("collection", ["tasks", "trust_transactions", "folders"])
def test_a_read_failure_refuses_with_lecture_and_writes_nothing(
        db, did, monkeypatch, collection):
    _fail_queries_on(monkeypatch, db, collection)
    folders = _folders_of(db, did)
    db.reset_logs()
    ok, message, report = dossier_model.delete_dossier(did)
    assert ok is False and report["code"] == "lecture"
    assert "rien n'a été supprimé" in message
    assert db.commits == []
    assert db.peek(f"dossiers/{did}") is not None
    assert _folders_of(db, did) == folders


def test_an_unreadable_dossier_is_lecture_never_introuvable(db, did, monkeypatch):
    """The old delete read the dossier through the FAIL-OPEN get_dossier: a
    blip read as « Dossier introuvable » and sent the lawyer to the list."""
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if f"dossiers/{did}" in names:
            raise gexc.ServiceUnavailable("injected read failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    db.reset_logs()
    ok, _m, report = dossier_model.delete_dossier(did)
    assert ok is False and report["code"] == "lecture"
    assert db.commits == []


def test_a_commit_failure_is_echec_and_deletes_nothing(db, did):
    def hook(info):
        if ("delete", f"dossiers/{did}") in info.ops:
            raise gexc.PermissionDenied("injected commit refusal")

    remove = db.add_commit_hook(hook)
    try:
        ok, message, report = dossier_model.delete_dossier(did)
    finally:
        remove()
    assert ok is False and report["code"] == "echec"
    assert "rien n'a été supprimé" in message
    # CERTAIN: the re-read found the dossier — nothing to journal, ever.
    assert report["dossier"] is None and report["folders"] == []
    assert db.peek(f"dossiers/{did}") is not None
    assert len(_folders_of(db, did)) == 18


def _commit_raises_then_rereads_fail(monkeypatch, db, did, *, lands: bool):
    """The dossier's delete commit RAISES (after applying, with ``lands``),
    and every plain re-read of the dossier after it fails."""
    server = db._fake_server
    real_commit = server.commit
    real_get = server.batch_get_documents
    state = {"raised": False, "rereads": 0}

    def commit(request, metadata=None, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            if lands:
                real_commit(request, metadata=metadata, **kwargs)
            raise gexc.DeadlineExceeded("answer lost")
        return real_commit(request, metadata=metadata, **kwargs)

    def batch_get(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if (state["raised"] and not request.get("transaction")
                and f"dossiers/{did}" in names):
            state["rereads"] += 1
            raise gexc.ServiceUnavailable("injected re-read failure")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", commit)
    monkeypatch.setattr(server, "batch_get_documents", batch_get)
    return state


@pytest.mark.parametrize("lands", [True, False])
def test_an_unknowable_outcome_never_says_nothing_was_deleted(
        db, did, monkeypatch, lands):
    """The commit raised and the re-read that would tell raised too: the
    outcome is UNKNOWN. « echec » still (the caller re-checks), but the
    message never claims « rien n'a été supprimé », and the report carries
    what the last attempt STAGED — the caller whose own re-read finds the
    dossier gone journals exactly that."""
    state = _commit_raises_then_rereads_fail(monkeypatch, db, did, lands=lands)
    ok, message, report = dossier_model.delete_dossier(did)
    assert state["raised"] and state["rereads"] == 1
    assert ok is False and report["code"] == "echec"
    assert "rien n'a été supprimé" not in message
    assert message == ("La suppression n'a peut-être pas abouti — rechargez "
                       "la page pour le vérifier.")
    assert report["dossier"]["file_number"] == "2026-001"
    assert len(report["folders"]) == 18
    assert (db.peek(f"dossiers/{did}") is None) is lands


def test_the_docstring_names_the_window_the_transaction_cannot_close():
    """A generation, a versement or a receipt copy already under way files
    its document AFTER the delete — the writers do not re-read the dossier.
    The guarantee is qualified where it is stated."""
    doc = " ".join((dossier_model.delete_dossier.__doc__ or "").split())
    assert "cannot close" in doc
    for word in ("generation", "portal versement", "receipt copy",
                 "do not re-read the dossier"):
        assert word in doc, word
    assert "no child is left pointing at a deleted one" not in doc


def test_a_commit_that_landed_with_its_answer_lost_is_a_success(
        db, did, monkeypatch):
    """The dossier's absence proves the delete landed: reporting « rien n'a
    été supprimé » would be false, and the trail row would be lost."""
    server = db._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        if state["armed"]:
            state["armed"] = False
            raise gexc.DeadlineExceeded("answer lost")
        return response

    monkeypatch.setattr(server, "commit", commit)
    ok, _m, report = dossier_model.delete_dossier(did)
    assert ok is True and report["code"] == ""
    assert len(report["folders"]) == 18
    assert report["dossier"]["file_number"] == "2026-001"
    assert db.peek(f"dossiers/{did}") is None and _folders_of(db, did) == {}


def test_a_folder_record_without_its_id_refuses_rather_than_orphan_it(db, did):
    """Only a hand edit makes one — but deleted by id, it would outlive its
    dossier. Refused, nothing written."""
    db.seed("folders/sans-id", {"dossier_id": did, "name": "Orphelin",
                                "parent_folder_id": None})
    db.reset_logs()
    ok, _m, report = dossier_model.delete_dossier(did)
    assert ok is False and report["code"] == "lecture"
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 4. Une écriture concurrente fait refuser (ou suit le dossier)
# ══════════════════════════════════════════════════════════════════════


def test_a_document_versed_during_the_delete_makes_it_refuse(db, did):
    """A document added between the reads and the commit: the commit aborts,
    the re-run sees it and refuses — the document never points at a deleted
    dossier, and no folder is ever left without its dossier."""
    fired = []

    def hook(info):
        if not fired and ("delete", f"dossiers/{did}") in info.ops:
            fired.append(info.index)
            db.external_write("documents/late", {
                "id": "late", "dossier_id": did,
                "folder_id": folder_model.system_folder_id(did, "portail"),
            })

    remove = db.add_commit_hook(hook)
    try:
        ok, _m, report = dossier_model.delete_dossier(did)
    finally:
        remove()
    assert fired, "the race never ran"
    assert ok is False and report["code"] == "contenu"
    assert db.peek(f"dossiers/{did}") is not None
    folders = _folders_of(db, did)
    assert len(folders) == 18
    assert db.peek("documents/late")["folder_id"] in folders


def test_a_folder_created_during_the_delete_leaves_with_the_dossier(db, did):
    """A folder the tree (or the lawyer) adds meanwhile aborts the commit;
    the re-run deletes it too — never a folder outliving its dossier."""
    fired = []

    def hook(info):
        if not fired and ("delete", f"dossiers/{did}") in info.ops:
            fired.append(info.index)
            db.external_write("folders/tard", {
                "id": "tard", "dossier_id": did, "name": "Tard",
                "parent_folder_id": None, "order": 0,
            })

    remove = db.add_commit_hook(hook)
    try:
        ok, _m, report = dossier_model.delete_dossier(did)
    finally:
        remove()
    assert fired
    assert ok is True and "tard" in report["folders"]
    assert db.peek(f"dossiers/{did}") is None
    assert _folders_of(db, did) == {}
