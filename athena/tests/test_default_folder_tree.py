"""The default folder tree of a dossier (2026-09-30) — models/folder.py.

Every dossier receives eighteen folders; seven of them are the
application's own (Mandat, Factures, Déboursés, Interne, Projets, Autres,
Reçus du portail), found by their ROLE, locked against rename and move.
One pure planner (``_plan``) serves the fast path, the transaction and the
dry-run; one writer (``_ensure``) commits. Over the shared fake Firestore —
the REAL client, an in-memory server — and read back from what is STORED.
"""

import os
import sys
import unicodedata
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.folder as folder
    from models import provenance

from google.api_core import exceptions as gexc  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
SID = folder.system_folder_id
NID = folder.node_folder_id


@pytest.fixture
def store(monkeypatch):
    fake = install(monkeypatch, folder)
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001"})
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002"})
    return fake


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(
        folder, "log_dossier_event",
        lambda event, dossier_id, **kw: seen.append((event, kw)),
    )
    return seen


def _seed_folder(store, fid, name, parent=None, *, dossier="d1", **extra):
    data = {
        "id": fid, "dossier_id": dossier, "name": name,
        "parent_folder_id": parent, "order": 0,
        "created_at": T0, "updated_at": T0, "etag": f"e-{fid}", **extra,
    }
    store.seed(f"folders/{fid}", data)
    return data


def _folders(store) -> dict:
    return store.peek_collection("folders")


def _ops(store) -> list:
    return [op for c in store.commits for op in c.ops]


def _tree(store, dossier="d1") -> dict:
    """{« Parent / Enfant »: folder} of what is stored."""
    rows = {k: v for k, v in _folders(store).items() if v["dossier_id"] == dossier}

    def path(f):
        parent = rows.get(f.get("parent_folder_id") or "")
        return f"{path(parent)} / {f['name']}" if parent else f["name"]

    return {path(f): f for f in rows.values()}


EXPECTED_PATHS = {
    "Mandat", "Mandat / Vérifications", "Mandat / Conventions",
    "Mandat / Factures", "Mandat / Déboursés",
    "Procédures", "Procédures / Notification", "Procédures / Pièces",
    "Procédures / Engagements", "Procédures / Transcriptions",
    "Procédures / Procès-verbaux", "Procédures / Jugements",
    "Correspondance", "Correspondance / Courriels",
    "Interne", "Interne / Projets", "Autres", "Autres / Reçus du portail",
}


# ── Le registre ───────────────────────────────────────────────────────────


def test_the_registry_is_the_tree_the_lawyer_asked_for():
    assert len(folder.DEFAULT_TREE) == 18
    assert folder.VALID_SYSTEM_ROLES == (
        "mandat", "factures", "debourses", "interne", "projets", "autres", "portail",
    )
    assert folder.LEGACY_ROLES == ("projets", "portail")
    assert folder.SYSTEM_FOLDER_PARENTS == {
        "mandat": "", "factures": "mandat", "debourses": "mandat",
        "interne": "", "projets": "interne", "autres": "", "portail": "autres",
    }
    assert folder.SYSTEM_FOLDER_NAMES["projets"] == "Projets"
    assert folder.SYSTEM_FOLDER_NAMES["portail"] == "Reçus du portail"
    assert folder.DEFAULT_TREE_BUTTON == "Créer l'arborescence par défaut"
    for node in folder.DEFAULT_TREE:          # every name stores as given
        assert folder._name_errors(node.name) == [], node.key


# ── La création de l'arbre ────────────────────────────────────────────────


def test_a_new_dossier_gets_the_eighteen_folders_in_one_commit(store, events):
    report, errors = folder.ensure_default_tree("d1")

    assert errors == []
    assert len(store.commits) == 1 and len(store.commits[0].ops) == 18
    tree = _tree(store)
    assert set(tree) == EXPECTED_PATHS
    for node in folder.DEFAULT_TREE:
        stored = store.peek(f"folders/{NID('d1', node.key)}")
        assert stored["name"] == node.name
        assert stored["system_role"] == node.role
        assert stored["etag"] and stored["created_via"] == "script"
    assert report.counts()["created"] == 18
    assert events == [("default_folder_tree_created", {
        "created": 18, "adopted": 0, "reused": 0, "relocated": 0,
        "blocked": 0, "present": 0,
    })]


def test_a_second_run_writes_nothing(store, events):
    folder.ensure_default_tree("d1")
    etags = {k: v["etag"] for k, v in _folders(store).items()}
    store.reset_logs()
    del events[:]

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == [] and store.commits == []
    assert {k: v["etag"] for k, v in _folders(store).items()} == etags
    assert report.counts()["present"] == 18 and not report.pending
    assert events == []


def test_every_write_is_idempotent_for_the_connector(store):
    """A failure after the tree (the connector's payload) must stay a
    retryable refusal: a second run writes nothing, so the tree's commits
    are noted idempotent, never « ENREGISTRÉE — NE PAS RÉESSAYER »."""
    with provenance.writing_via("mcp", tool="create_dossier"):
        folder.ensure_default_tree("d1")
        assert provenance.committed_writes() == ()
        assert len(provenance.idempotent_writes()) == 18


def test_the_tree_refuses_an_unknown_dossier_and_an_unreadable_one(
    store, monkeypatch, events,
):
    report, errors = folder.ensure_default_tree("d9")
    assert report is None and errors == [folder.DOSSIER_NOT_FOUND]
    assert _folders(store) == {}
    assert folder.ensure_default_tree("  ")[1] == [folder.DOSSIER_REQUIRED]

    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("indisponible")

    monkeypatch.setattr(store._fake_server, "run_query", _boom)
    report, errors = folder.ensure_default_tree("d1")
    assert report is None and errors == [folder.READ_ERROR]
    assert ("default_folder_tree_incomplete", {"reason": "refused"}) in events


def test_the_dry_run_never_writes(store):
    _seed_folder(store, "legacy", "Projets")
    report, errors = folder.plan_default_tree("d1", relocate=True)
    assert errors == [] and store.commits == []
    assert store.reads_outside_transactions() == store.reads
    assert report.pending and "projets" in report.relocated
    assert report.counts()["created"] == 17


def test_a_document_of_another_dossier_at_a_node_id_is_never_overwritten(store):
    """AlreadyExists (a document at a deterministic id that this dossier's
    query does not return — only a hand edit makes one) re-runs ONCE, then
    refuses: nothing overwritten, nothing half-written."""
    foreign = NID("d1", "pieces")
    _seed_folder(store, foreign, "Ailleurs", dossier="d2")

    report, errors = folder.ensure_default_tree("d1")

    assert report is None and errors == [folder.READ_ERROR]
    assert store.peek(f"folders/{foreign}")["etag"] == f"e-{foreign}"
    assert store.peek(f"folders/{foreign}")["dossier_id"] == "d2"
    assert [f for f in _folders(store).values() if f["dossier_id"] == "d1"] == []


def test_a_folder_created_during_the_transaction_is_found_not_duplicated(store):
    """Another writer creates « Interne » between the engine's read and its
    commit: the transaction aborts, re-reads, and finds it — one Interne."""
    interne = SID("d1", "interne")
    fired = []

    def _hook(info):
        if not fired and any(p == f"folders/{interne}" for _k, p in info.ops):
            fired.append(True)
            store.external_write(f"folders/{interne}", {
                "id": interne, "dossier_id": "d1", "name": "Interne",
                "parent_folder_id": None, "order": 0, "system_role": "interne",
                "etag": "e-autre", "created_at": T0, "updated_at": T0,
            })

    store.add_commit_hook(_hook)
    report, errors = folder.ensure_default_tree("d1")

    assert fired and errors == []
    assert store.peek(f"folders/{interne}")["etag"] == "e-autre"
    assert sum(1 for p in _tree(store) if p == "Interne") == 1
    assert set(_tree(store)) == EXPECTED_PATHS


# ── Les dossiers existants : adopter, déplacer, jamais dupliquer ──────────


def test_homonyms_are_adopted_where_they_belong_and_left_alone_elsewhere(store, events):
    """Production holds root « Correspondance », « Mandat », « Autres »,
    and « Déboursés » at the ROOT (not under Mandat). A same-name folder
    where the node goes is ADOPTED (never a second one beside it); a system
    node stamps it. One elsewhere is the lawyer's: untouched, ordinary."""
    _seed_folder(store, "corr", "Correspondance")
    _seed_folder(store, "mandat", "mandat")
    _seed_folder(store, "deb", "Déboursés")

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    tree = _tree(store)
    assert tree["Correspondance"]["id"] == "corr"
    assert tree["Correspondance"]["etag"] == "e-corr"      # ordinary: not written
    assert tree["mandat"]["id"] == "mandat"
    assert tree["mandat"]["system_role"] == "mandat"       # system: stamped
    assert tree["mandat / Déboursés"]["id"] == SID("d1", "debourses")
    assert tree["Déboursés"]["id"] == "deb"                # the root one stays
    assert not tree["Déboursés"].get("system_role")
    # « Mandat » is STAMPED (adopted); « Correspondance » is only matched by
    # name — nothing written for it, now or on any later run.
    assert report.adopted == ["mandat"] and report.reused == ["correspondance"]
    again, _ = folder.plan_default_tree("d1", relocate=True)
    assert not again.pending and again.reused == ["correspondance"]
    assert ("system_folder_adopted", {
        "folder_id": "mandat", "role": "mandat", "legacy_candidates": 1,
    }) in events


def test_a_legacy_root_projets_moves_under_interne_with_its_documents(store, events):
    _seed_folder(store, "legacy", "Projets")
    _seed_folder(store, "sous", "2025", "legacy")
    store.seed("documents/doc1", {"id": "doc1", "dossier_id": "d1",
                                  "folder_id": "legacy", "display_name": "x"})

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    moved = store.peek("folders/legacy")
    assert moved["parent_folder_id"] == SID("d1", "interne")
    assert moved["system_role"] == "projets"
    assert store.peek("folders/sous")["parent_folder_id"] == "legacy"
    assert store.peek("documents/doc1")["folder_id"] == "legacy"   # untouched
    assert store.peek(f"folders/{SID('d1', 'projets')}") is None  # no second
    assert report.relocated == ["projets"] and "projets" in report.adopted
    names = [e for e, _kw in events]
    assert "system_folder_adopted" in names and "system_folder_relocated" in names


def test_a_stamped_root_projets_the_current_code_creates_moves_too(store):
    """The code serving before the tree creates « Projets » STAMPED at its
    deterministic id, at the ROOT. Relocation is a property of the folder
    the planner resolved, whatever branch found it."""
    pid = SID("d1", "projets")
    _seed_folder(store, pid, "Projets", system_role="projets")
    rid = SID("d1", "portail")
    _seed_folder(store, rid, "Reçus du portail", system_role="portail")

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    assert store.peek(f"folders/{pid}")["parent_folder_id"] == SID("d1", "interne")
    assert store.peek(f"folders/{rid}")["parent_folder_id"] == SID("d1", "autres")
    assert sorted(report.relocated) == ["portail", "projets"]


def test_a_generation_adopts_in_place_then_the_button_moves_it(store):
    """Leaf first at use time: a generation on a dossier with a legacy root
    « Projets » stamps it WHERE IT IS and creates no « Interne » (a write no
    result would report). The button, later, moves it."""
    _seed_folder(store, "legacy", "Projets")

    got, errors = folder.ensure_system_folder("d1", "projets")

    assert errors == [] and got["id"] == "legacy"
    assert store.peek("folders/legacy")["parent_folder_id"] is None
    assert list(_folders(store)) == ["legacy"]

    folder.ensure_default_tree("d1", relocate=True)
    assert store.peek("folders/legacy")["parent_folder_id"] == SID("d1", "interne")


def test_without_relocate_a_legacy_projets_stays_at_the_root(store):
    _seed_folder(store, "legacy", "Projets")
    report, errors = folder.ensure_default_tree("d1")
    assert errors == [] and report.relocated == []
    assert store.peek("folders/legacy")["parent_folder_id"] is None
    assert store.peek("folders/legacy")["system_role"] == "projets"
    assert store.peek(f"folders/{SID('d1', 'projets')}") is None


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_a_relocation_onto_a_homonym_is_blocked_and_the_rest_is_written(store, form):
    """Interne already holds a « Projets » the lawyer made: the legacy root
    one cannot move there without a duplicate — reported for a merge by
    hand, never an error of the whole tree."""
    _seed_folder(store, "legacy", "Projets")
    _seed_folder(store, "int", "Interne")
    _seed_folder(store, "fait-main", unicodedata.normalize(form, "Projets"), "int")

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    assert ("projets", "relocation_duplicate") in report.blocked
    assert store.peek("folders/legacy")["parent_folder_id"] is None
    assert store.peek("folders/legacy")["system_role"] == "projets"
    assert not store.peek("folders/fait-main").get("system_role")
    assert store.peek("folders/int")["system_role"] == "interne"
    assert len(_tree(store)) == 19                  # 17 new/adopted + 2 kept


def test_a_relocation_that_would_exceed_the_depth_is_blocked(store):
    _seed_folder(store, "legacy", "Projets")
    _seed_folder(store, "a", "A", "legacy")
    _seed_folder(store, "b", "B", "a")
    _seed_folder(store, "c", "C", "b")
    _seed_folder(store, "d", "D", "c")               # subtree height 4

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    assert ("projets", "relocation_depth") in report.blocked
    assert store.peek("folders/legacy")["parent_folder_id"] is None


def test_a_default_parent_moved_too_deep_blocks_its_children_only(store):
    """An ordinary default folder is the lawyer's to move — even to depth
    5. Its missing children are blocked, never created at depth 6."""
    folder.ensure_default_tree("d1")
    chain = None
    for i in range(4):
        fid = f"n{i}"
        _seed_folder(store, fid, f"N{i}", chain)
        chain = fid
    proc = NID("d1", "procedures")
    store.external_write(f"folders/{proc}", {
        **store.peek(f"folders/{proc}"), "parent_folder_id": chain,
    })
    store.external_delete(f"folders/{NID('d1', 'pieces')}")

    report, errors = folder.ensure_default_tree("d1", relocate=True)

    assert errors == []
    assert ("pieces", "depth") in report.blocked
    assert store.peek(f"folders/{NID('d1', 'pieces')}") is None
    assert store.peek(f"folders/{proc}")["parent_folder_id"] == chain   # not moved back


def test_a_folder_stamped_with_another_role_at_a_node_id_is_never_restamped(store):
    fid = SID("d1", "factures")
    _seed_folder(store, fid, "Factures", system_role="debourses")
    report, errors = folder.ensure_default_tree("d1")
    assert errors == []
    assert ("factures", "conflict") in report.blocked
    assert store.peek(f"folders/{fid}")["system_role"] == "debourses"


# ── Les rôles hérités, seuls réservés ─────────────────────────────────────


def test_a_root_mandat_or_factures_is_an_ordinary_folder(store):
    """Only « Projets » and « Reçus du portail » keep the legacy rules. A
    root « Mandat » is the lawyer's folder: creatable, renamable, never
    presumed system, never reported with a role."""
    for name in ("Mandat", "Factures", "Déboursés", "Interne", "Autres"):
        created, errors = folder.create_folder("d1", name)
        assert errors == [], name
        assert not folder.is_system_folder(created)
    roots = list(_folders(store).values())
    assert folder.system_roles(roots) == {}
    fac = next(f for f in roots if f["name"] == "Factures")
    _r, errors, changed = folder.rename_folder("d1", fac["id"], "Factures reçues")
    assert errors == [] and changed


def test_a_system_role_is_found_by_its_stamp_under_its_parent(store):
    """« Factures » of a dossier whose ROOT holds a lawyer's « Factures »:
    the root one is never adopted — the system one is created under
    « Mandat »."""
    _seed_folder(store, "fac", "Factures")
    got, errors = folder.ensure_system_folder("d1", "factures")
    assert errors == []
    assert got["id"] == SID("d1", "factures")
    assert got["parent_folder_id"] == SID("d1", "mandat")
    assert not store.peek("folders/fac").get("system_role")

    store.reset_logs()
    deb, errors = folder.ensure_system_folder("d1", "debourses")
    assert errors == [] and deb["parent_folder_id"] == SID("d1", "mandat")
    assert _ops(store) == [("create", f"folders/{SID('d1', 'debourses')}")]


def test_the_seven_are_locked_the_eleven_are_free(store):
    folder.ensure_default_tree("d1")
    for node in folder.DEFAULT_TREE:
        fid = NID("d1", node.key)
        _r, errors, changed = folder.rename_folder("d1", fid, node.name + " bis")
        if node.role:
            assert not changed and "dossier système" in errors[0], node.key
        else:
            assert errors == [] and changed, node.key


def test_a_deleted_interne_comes_back_with_projets_at_the_same_ids(store):
    folder.ensure_default_tree("d1")
    ok, _msg, _rapport = folder.delete_folder(
        "d1", SID("d1", "interne"), expected_documents=0, expected_folders=1)
    assert ok
    assert store.peek(f"folders/{SID('d1', 'projets')}") is None

    got, errors = folder.ensure_system_folder("d1", "projets")
    assert errors == [] and got["id"] == SID("d1", "projets")
    assert got["parent_folder_id"] == SID("d1", "interne")
    assert store.peek(f"folders/{SID('d1', 'interne')}") is not None


def test_the_tree_of_one_dossier_never_touches_another(store):
    folder.ensure_default_tree("d1")
    folder.ensure_default_tree("d2")
    assert set(_tree(store, "d1")) == set(_tree(store, "d2")) == EXPECTED_PATHS
    ids_d1 = {f["id"] for f in _tree(store, "d1").values()}
    ids_d2 = {f["id"] for f in _tree(store, "d2").values()}
    assert len(ids_d1) == len(ids_d2) == 18 and not ids_d1 & ids_d2


def test_the_plan_is_pure_over_the_folders_it_is_given(store):
    rows = [_seed_folder(store, "legacy", "Projets")]
    report = folder.plan_default_tree_from("d1", rows, relocate=True)
    assert report.pending and store.commits == [] and store.reads == []
    assert rows[0].get("system_role") is None        # the input is untouched


def test_a_parallel_run_that_finds_everything_written_logs_nothing(store, events):
    """The second of two parallel runs: its plain read predates the first's
    commit, its transaction finds all eighteen — nothing written, no event
    (it used to log « default_folder_tree_created » with created=0)."""
    real = folder._all_folders
    calls = []

    def _stale_then_real(dossier_id):
        calls.append(dossier_id)
        return [] if len(calls) == 1 else real(dossier_id)

    folder.ensure_default_tree("d1")
    del events[:]
    store.reset_logs()
    folder._all_folders = _stale_then_real
    try:
        report, errors = folder.ensure_default_tree("d1")
    finally:
        folder._all_folders = real
    assert errors == [] and not report.writes
    assert events == [] and _ops(store) == []


def test_a_commit_that_landed_with_its_answer_lost_is_a_success(store, monkeypatch):
    """A raise out of the commit does not prove nothing landed: the engine
    re-reads, finds the tree it wrote, and answers success — the button
    never says « n'a pas pu être créée » over folders it created."""
    real_commit = store._fake_server.commit
    fired = []

    def _commit(request, metadata=None, **kwargs):
        result = real_commit(request, metadata=metadata, **kwargs)
        if not fired:
            fired.append(True)
            raise gexc.DeadlineExceeded("réponse perdue")
        return result

    monkeypatch.setattr(store._fake_server, "commit", _commit)
    report, errors = folder.ensure_default_tree("d1")
    assert fired and errors == [] and report is not None
    assert set(_tree(store)) == EXPECTED_PATHS
