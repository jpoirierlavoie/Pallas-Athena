"""Tests for models/folder.py — the folder model over the REAL client.

Two families:

* the lot 2A step T2 hardening (2026-09-27), over the shared fake Firestore
  (``tests/_fake_firestore.py`` — the client is the real one, only the
  server is in memory) and read back from what is STORED. Each test names
  the defect of the code before it and FAILS on that code: duplicate checks
  that failed OPEN, names silently sanitized into different names, depth
  and cycle walks that stopped at an unreadable parent, full-document
  ``set()`` on rename and move, and system folders found BY NAME — which
  forked « Projets » under parallel generations and after a rename;
* the folder deletion (2026-08-14), whose Firestore calls are faked below
  (``_arbre``).

``get_or_create_folder``'s two tests (idempotent reuse by name, scoping
per dossier) were REMOVED deliberately with the function (lot 2A, T2):
their successor is ``ensure_system_folder``, pinned here by role, by
deterministic id, under a race and against legacy folders.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.folder as folder
    from models import concurrency

from google.api_core import exceptions as gexc  # noqa: E402

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
STALE = [concurrency.STALE_ETAG_ERROR]


# ═══════════════════════════════════════════════════════════════════════════
# Lot 2A, étape T2 — le modèle durci, sur le vrai client
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def store(monkeypatch):
    fake = install(monkeypatch, folder)
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001"})
    fake.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002"})
    return fake


def _seed_folder(store, fid, name, parent=None, *, dossier="d1", **extra):
    data = {
        "id": fid, "dossier_id": dossier, "name": name,
        "parent_folder_id": parent, "order": 0,
        "created_at": T0, "updated_at": T0, **extra,
    }
    store.seed(f"folders/{fid}", data)
    return data


def _folders(store) -> dict:
    return store.peek_collection("folders")


def _writes(store) -> list:
    """Every write op committed. A read-only transaction still COMMITS (an
    empty commit, as the real client does), so « nothing written » is « no
    op », not « no commit »."""
    return [op for c in store.commits for op in c.ops]


def _fail_queries(monkeypatch, store) -> None:
    """Every query RPC fails at the server — an outage, not a code bug."""
    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(store._fake_server, "run_query", _boom)


# ── Créer ─────────────────────────────────────────────────────────────────


def test_the_collection_is_top_level_and_the_folder_carries_its_stamps(store):
    """Folders live in the TOP-LEVEL `folders` collection (CLAUDE.md said a
    `dossiers/{id}/folders` subcollection) and, since T2, carry an etag and
    the provenance stamps — the Rule 7 exception is lifted."""
    created, errors = folder.create_folder("d1", "Pièces")
    assert errors == []
    stored = store.peek(f"folders/{created['id']}")
    assert stored is not None and stored == created
    assert stored["name"] == "Pièces" and stored["dossier_id"] == "d1"
    assert stored["etag"] and stored["system_role"] == ""
    assert stored["created_via"] == stored["updated_via"] == "script"
    assert stored["created_at"] == stored["updated_at"]


def test_a_duplicate_check_that_cannot_read_refuses_instead_of_duplicating(
    store, monkeypatch,
):
    """FAILS on the old code: `_check_duplicate_name` read through
    `list_folders`, which answers [] on an outage — so « Pièces » read as
    absent and a second « Pièces » was written."""
    _seed_folder(store, "f1", "Pièces")
    _fail_queries(monkeypatch, store)

    created, errors = folder.create_folder("d1", "Pièces")

    assert created is None and errors == [folder.READ_ERROR]
    assert list(_folders(store)) == ["f1"]
    assert store.commits == []


def test_two_racing_creations_of_one_name_leave_one_folder(store):
    """FAILS on the old code (read, then an unconditional set() of a fresh
    uuid4). The duplicate check now runs INSIDE the transaction that writes:
    a folder created between that read and the commit aborts it, and the
    re-run sees the folder and refuses."""
    fired = []

    def _concurrent(_info):
        if not fired:
            fired.append(True)
            store.external_write("folders/fx", {
                "id": "fx", "dossier_id": "d1", "name": "pièces",
                "parent_folder_id": None, "created_at": T0,
            })

    store.add_commit_hook(_concurrent)
    created, errors = folder.create_folder("d1", "Pièces")

    assert created is None and errors == [folder.DUPLICATE_HERE]
    assert list(_folders(store)) == ["fx"]


@pytest.mark.parametrize("name, message", [
    ("<x>", folder.NAME_CHEVRONS),         # the old code stored ""
    ("a<b>c", folder.NAME_CHEVRONS),       # …and this as « ac »
    ("x" * 101, folder.NAME_TOO_LONG),
    ("a/b", folder.NAME_SLASH),
    ("a\\b", folder.NAME_SLASH),
    ("ligne\nsuite", folder.NAME_CONTROL),
    ("   ", folder.NAME_REQUIRED),
])
def test_a_name_storage_would_change_is_refused_never_rewritten(store, name, message):
    created, errors = folder.create_folder("d1", name)
    assert created is None and message in errors
    assert _folders(store) == {}


def test_surrounding_spaces_are_trimmed_not_refused(store):
    created, errors = folder.create_folder("d1", "  Pièces  ")
    assert errors == []
    assert store.peek(f"folders/{created['id']}")["name"] == "Pièces"


@pytest.mark.parametrize("name", [
    "lignesuite",      # NEL — a C1 control
    "ligne suite",      # LINE SEPARATOR
    "ligne suite",      # PARAGRAPH SEPARATOR
    "ab",                # CSI — a C1 control
])
def test_every_invisible_line_break_is_refused(store, name):
    """Review of T2 — FAILS on T2's check (``ord(c) < 32 or == 127``): the
    C1 controls and U+2028/U+2029 are line breaks too (``str.splitlines``,
    a browser), and they were stored in the name unseen."""
    created, errors = folder.create_folder("d1", name)
    assert created is None and folder.NAME_CONTROL in errors
    assert _folders(store) == {}


def test_names_that_look_identical_are_duplicates_whatever_their_normalization(store):
    """Review of T2 — FAILS without NFC folding: « Pièces » precomposed
    (NFC) and decomposed (NFD) passed the duplicate check side by side, and
    a decomposed « Reçus du portail » slipped past the reserved root name."""
    import unicodedata

    nfd = lambda text: unicodedata.normalize("NFD", text)  # noqa: E731
    first, errors = folder.create_folder("d1", "Pièces")
    assert errors == [] and nfd("Pièces") != "Pièces"
    twin, errors = folder.create_folder("d1", nfd("Pièces"))
    assert twin is None and errors == [folder.DUPLICATE_HERE]
    reserved, errors = folder.create_folder("d1", nfd("Reçus du portail"))
    assert reserved is None and "réservé" in errors[0]
    # A rename that only changes the normalization is not a duplicate of
    # the folder itself.
    _r, errors, changed = folder.rename_folder("d1", first["id"], nfd("Pièces"))
    assert errors == [] and changed is True
    assert list(_folders(store)) == [first["id"]]


def test_legacy_adoption_keeps_the_old_lookup_exactly(store):
    """The legacy match stays the deleted get_or_create_folder's own lookup
    (trimmed, case-insensitive, NOT normalized): an older hand-made folder
    whose name is the DECOMPOSED « Reçus du portail » was never the one the
    application filed into, so it is not adopted over the app's own."""
    import unicodedata

    _seed_folder(store, "main", unicodedata.normalize("NFD", "Reçus du portail"),
                 created_at=T0 - timedelta(days=30))
    _seed_folder(store, "app", "Reçus du portail", created_at=T0)
    adopted, errors = folder.ensure_system_folder("d1", "portail")
    assert errors == [] and adopted["id"] == "app"
    assert not store.peek("folders/main").get("system_role")


def test_an_unknown_dossier_is_refused(store):
    """FAILS on the old code: nothing checked the dossier, so a folder could
    be written on an id no dossier bears (routes/documents never did)."""
    created, errors = folder.create_folder("d9", "Pièces")
    assert created is None and errors == [folder.DOSSIER_NOT_FOUND]
    assert _folders(store) == {}


def test_the_parent_must_belong_to_the_same_dossier(store):
    _seed_folder(store, "autre", "Ailleurs", dossier="d2")
    created, errors = folder.create_folder("d1", "Pièces", parent_folder_id="autre")
    assert created is None and errors == ["Le dossier parent est introuvable."]
    assert list(_folders(store)) == ["autre"]


def test_the_depth_limit_comes_from_one_read(store):
    parent = None
    for level in range(1, folder.MAX_NESTING_DEPTH + 1):
        _seed_folder(store, f"n{level}", f"Niveau {level}", parent)
        parent = f"n{level}"
    store.reset_logs()

    created, errors = folder.create_folder("d1", "Trop bas", parent_folder_id=parent)

    assert created is None and "profondeur maximale" in errors[0]
    # ONE query for the whole walk (plus the keyed dossier check) — the old
    # walk read one parent at a time and stopped at the first unreadable one.
    queries = [r for r in store.reads if r.rpc == "run_query"]
    assert len(queries) == 1 and queries[0].transactional
    folder_gets = [r for r in store.reads if r.rpc == "batch_get_documents"
                   and any(p.startswith("folders/") for p in r.paths)]
    assert folder_gets == []


# ── Renommer ──────────────────────────────────────────────────────────────


def test_a_rename_is_a_partial_update_with_a_new_etag(store):
    _seed_folder(store, "f1", "Pièces", etag="e1", order=7, marque="intacte")
    store.reset_logs()

    renamed, errors, changed = folder.rename_folder(
        "d1", "f1", "Pièces déposées", expected_etag="e1",
    )

    assert errors == [] and changed is True
    stored = store.peek("folders/f1")
    assert stored["name"] == "Pièces déposées"
    assert stored["etag"] not in ("", "e1") and stored["etag"] == renamed["etag"]
    assert stored["order"] == 7 and stored["marque"] == "intacte"
    assert stored["updated_via"] == "script"
    assert store.commits[-1].ops == (("update", "folders/f1"),)


def test_a_rename_on_a_stale_etag_writes_nothing(store):
    _seed_folder(store, "f1", "Pièces", etag="e2")
    renamed, errors, changed = folder.rename_folder(
        "d1", "f1", "Autre", expected_etag="e1",
    )
    assert renamed is None and errors == STALE and changed is False
    assert store.peek("folders/f1")["name"] == "Pièces"
    assert _writes(store) == []


def test_a_legacy_folder_without_etag_matches_the_empty_etag(store):
    _seed_folder(store, "f1", "Pièces")            # written before Rule 7
    _renamed, errors, changed = folder.rename_folder(
        "d1", "f1", "Autre", expected_etag="",
    )
    assert errors == [] and changed is True


def test_renaming_to_the_same_name_writes_nothing(store):
    _seed_folder(store, "f1", "Pièces", etag="e1")
    renamed, errors, changed = folder.rename_folder("d1", "f1", "Pièces")
    assert errors == [] and changed is False and renamed["etag"] == "e1"
    assert _writes(store) == []


def test_a_rename_that_cannot_read_the_folders_refuses(store, monkeypatch):
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "f2", "Annexes")
    _fail_queries(monkeypatch, store)
    renamed, errors, changed = folder.rename_folder("d1", "f2", "Pièces")
    assert renamed is None and errors == [folder.READ_ERROR] and not changed
    assert store.peek("folders/f2")["name"] == "Annexes"


def test_a_rename_to_a_taken_name_is_refused_case_insensitively(store):
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "f2", "Annexes")
    _r, errors, _c = folder.rename_folder("d1", "f2", "PIÈCES")
    assert errors == [folder.DUPLICATE_HERE]


def test_a_rename_refuses_a_name_storage_would_change(store):
    _seed_folder(store, "f1", "Pièces")
    _r, errors, changed = folder.rename_folder("d1", "f1", "<b>Pièces")
    assert errors == [folder.NAME_CHEVRONS] and not changed
    assert store.peek("folders/f1")["name"] == "Pièces"


# ── Déplacer ──────────────────────────────────────────────────────────────


def test_a_move_into_its_own_descendant_is_refused_from_one_read(store):
    _seed_folder(store, "f1", "Un")
    _seed_folder(store, "f2", "Deux", "f1")
    _seed_folder(store, "f3", "Trois", "f2")
    store.reset_logs()

    moved, errors, changed = folder.move_folder("d1", "f1", "f3")

    assert moved is None and not changed
    assert errors == ["Impossible de déplacer un dossier dans un de ses sous-dossiers."]
    assert store.peek("folders/f1")["parent_folder_id"] is None
    assert [r.rpc for r in store.reads] == ["run_query"]


def test_a_move_over_the_depth_limit_is_refused(store):
    parent = None
    for level in range(1, 5):
        _seed_folder(store, f"n{level}", f"Niveau {level}", parent)
        parent = f"n{level}"                         # n4 sits at depth 4
    _seed_folder(store, "a", "Arbre")
    _seed_folder(store, "b", "Branche", "a")          # a's subtree is 1 deep
    _m, errors, _c = folder.move_folder("d1", "a", "n4")  # 4 + 1 + 1 = 6
    assert "profondeur maximale" in errors[0]
    _m, errors, changed = folder.move_folder("d1", "a", "n3")  # 3 + 1 + 1 = 5
    assert errors == [] and changed is True


def test_a_move_to_another_dossiers_folder_is_refused(store):
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "autre", "Ailleurs", dossier="d2")
    _m, errors, _c = folder.move_folder("d1", "f1", "autre")
    assert errors == ["Le dossier de destination est introuvable."]
    assert store.peek("folders/f1")["parent_folder_id"] is None


def test_a_move_is_a_partial_update_and_a_same_parent_move_writes_nothing(store):
    _seed_folder(store, "f1", "Pièces", etag="e1", marque="intacte")
    _seed_folder(store, "p", "Parent", etag="ep")
    _m, errors, changed = folder.move_folder("d1", "f1", None)
    assert errors == [] and changed is False and _writes(store) == []

    moved, errors, changed = folder.move_folder("d1", "f1", "p", expected_etag="e1")
    assert errors == [] and changed is True
    stored = store.peek("folders/f1")
    assert stored["parent_folder_id"] == "p" and stored["marque"] == "intacte"
    assert stored["etag"] == moved["etag"] != "e1"
    # The move itself, then the new parent's TIMESTAMP touch — which never
    # regenerates the parent's etag (its open rename form stays valid).
    assert _writes(store) == [("update", "folders/f1"), ("update", "folders/p")]
    assert store.peek("folders/p")["etag"] == "ep"


def test_a_move_on_a_stale_etag_writes_nothing(store):
    _seed_folder(store, "f1", "Pièces", etag="e2")
    _seed_folder(store, "p", "Parent")
    _m, errors, changed = folder.move_folder("d1", "f1", "p", expected_etag="e1")
    assert errors == STALE and not changed
    assert store.peek("folders/f1")["parent_folder_id"] is None


# ── Dossiers système ──────────────────────────────────────────────────────


def test_ensure_creates_the_system_folder_at_its_deterministic_id(store):
    created, errors = folder.ensure_system_folder("d1", folder.SYSTEM_ROLE_PROJETS)
    assert errors == []
    expected_id = folder.system_folder_id("d1", "projets")
    assert created["id"] == expected_id
    stored = store.peek(f"folders/{expected_id}")
    assert stored["name"] == "Projets" and stored["system_role"] == "projets"
    assert stored["parent_folder_id"] is None and stored["etag"]
    assert store.commits[-1].ops == (("create", f"folders/{expected_id}"),)

    again, errors = folder.ensure_system_folder("d1", "projets")
    assert errors == [] and again["id"] == expected_id
    assert len(store.commits) == 1                   # found, nothing written


def test_the_deterministic_id_is_per_dossier_and_per_role():
    ids = {folder.system_folder_id(d, r) for d in ("d1", "d2")
           for r in folder.VALID_SYSTEM_ROLES}
    assert len(ids) == 4
    assert folder.system_folder_id("d1", "projets") == folder.system_folder_id("d1", "projets")
    with pytest.raises(ValueError):
        folder.system_folder_id("d1", "autre")


def test_parallel_ensures_never_fork_the_system_folder(store, monkeypatch):
    """The defect the review found in the design itself: a read-then-create
    of a fresh uuid4 lets two parallel generations on a new dossier each see
    « no Projets » and each create one. Here the second call runs ENTIRELY
    between the first's read and the first's create: the first's create()
    meets the deterministic id already taken, reads it back, and both land
    in the SAME folder."""
    real_read = folder._all_folders
    interleaved = []

    def _read_then_let_the_other_call_run(dossier_id):
        rows = real_read(dossier_id)                   # A reads: nothing yet
        if not interleaved:
            interleaved.append(None)                   # B reads for real
            interleaved[0] = folder.ensure_system_folder(dossier_id, "projets")
        return rows                                    # A's stale view

    monkeypatch.setattr(folder, "_all_folders", _read_then_let_the_other_call_run)
    first, errors = folder.ensure_system_folder("d1", "projets")

    assert errors == []
    second, second_errors = interleaved[0]
    assert second_errors == []
    assert first["id"] == second["id"] == folder.system_folder_id("d1", "projets")
    assert [f["name"] for f in _folders(store).values()] == ["Projets"]


def test_legacy_adoption_stamps_the_oldest_and_logs_the_fork(store, monkeypatch):
    """Production holds « Projets » folders created BY NAME, one or — after
    a past fork — several. The oldest root one is adopted (stamped, once);
    the others stay ordinary folders the lawyer can rename or merge."""
    events = []
    monkeypatch.setattr(
        folder, "log_dossier_event",
        lambda event, dossier_id, **kw: events.append((event, dossier_id, kw)),
    )
    _seed_folder(store, "jeune", "Projets", created_at=T0 + timedelta(days=3))
    _seed_folder(store, "vieux", "projets", created_at=T0)
    _seed_folder(store, "p", "Pièces")
    _seed_folder(store, "sous", "Projets", "p", created_at=T0 - timedelta(days=9))

    adopted, errors = folder.ensure_system_folder("d1", "projets")

    assert errors == [] and adopted["id"] == "vieux"
    assert store.peek("folders/vieux")["system_role"] == "projets"
    assert store.peek("folders/vieux")["etag"]
    assert not store.peek("folders/jeune").get("system_role")
    assert not store.peek("folders/sous").get("system_role")   # not at the root
    assert store.peek(f"folders/{folder.system_folder_id('d1', 'projets')}") is None
    assert events == [("system_folder_adopted", "d1", {
        "folder_id": "vieux", "role": "projets", "legacy_candidates": 2,
    })]

    # Adopted for good: found by its role, nothing written, nothing logged.
    store.reset_logs()
    again, _ = folder.ensure_system_folder("d1", "projets")
    assert again["id"] == "vieux" and store.commits == [] and len(events) == 1

    # The fork's other copy is an ORDINARY folder now.
    roots = [f for f in _folders(store).values() if not f.get("parent_folder_id")]
    assert folder.is_system_folder(store.peek("folders/vieux"), roots)
    assert not folder.is_system_folder(store.peek("folders/jeune"), roots)
    _r, errors, changed = folder.rename_folder("d1", "jeune", "Projets — ancien")
    assert errors == [] and changed


def test_the_portal_folder_is_adopted_under_its_own_role(store):
    _seed_folder(store, "recus", "Reçus du portail")
    adopted, errors = folder.ensure_system_folder("d1", folder.SYSTEM_ROLE_PORTAIL)
    assert errors == [] and adopted["id"] == "recus"
    assert store.peek("folders/recus")["system_role"] == "portail"


def test_a_system_folder_does_not_rename_or_move_and_the_next_ensure_finds_it(store):
    """FAILS on the old code: « Projets » renamed, the next generation's
    lookup BY NAME found nothing and created a second « Projets »."""
    projets, _ = folder.ensure_system_folder("d1", "projets")
    _seed_folder(store, "p", "Pièces")
    store.reset_logs()

    renamed, errors, changed = folder.rename_folder("d1", projets["id"], "Brouillons")
    assert renamed is None and not changed
    assert errors == [folder.SYSTEM_FOLDER_LOCKED.format(name="Projets")]
    moved, errors, changed = folder.move_folder("d1", projets["id"], "p")
    assert moved is None and not changed and "dossier système" in errors[0]
    assert _writes(store) == []

    again, _ = folder.ensure_system_folder("d1", "projets")
    assert again["id"] == projets["id"]
    assert sum(1 for f in _folders(store).values() if f.get("system_role")) == 1


def test_an_unstamped_legacy_projets_is_already_protected(store):
    """Before any generation adopts it, the legacy « Projets » is the one the
    next generation would adopt: renaming it then would fork just the same."""
    _seed_folder(store, "legacy", "Projets")
    _r, errors, changed = folder.rename_folder("d1", "legacy", "Brouillons")
    assert not changed and "dossier système" in errors[0]
    assert store.peek("folders/legacy")["name"] == "Projets"


@pytest.mark.parametrize("name", ["Projets", "projets", "REÇUS DU PORTAIL"])
def test_a_root_folder_cannot_take_a_system_name(store, name):
    """Created by hand, such a folder would read as the legacy system folder
    and be locked; ensure_system_folder is the one door to these names at
    the root. In a sub-folder the name is free."""
    created, errors = folder.create_folder("d1", name)
    assert created is None and "réservé" in errors[0]
    _seed_folder(store, "b", "Brouillons")
    _r, errors, _c = folder.rename_folder("d1", "b", name)
    assert "réservé" in errors[0]
    _seed_folder(store, "p", "Pièces")
    inside, errors = folder.create_folder("d1", name, parent_folder_id="p")
    assert errors == [] and inside["parent_folder_id"] == "p"
    _m, errors, _c = folder.move_folder("d1", inside["id"], None)
    assert "réservé" in errors[0]


def test_ensure_refuses_when_the_folders_cannot_be_read(store, monkeypatch):
    """FAILS on the old code: get_or_create_folder read through the
    fail-open list_folders, so an outage read as « no Projets » and a
    duplicate was created (and its own errors were swallowed into None)."""
    _seed_folder(store, "legacy", "Projets")
    _fail_queries(monkeypatch, store)
    got, errors = folder.ensure_system_folder("d1", "projets")
    assert got is None and errors == [folder.READ_ERROR]
    assert list(_folders(store)) == ["legacy"]


def test_ensure_refuses_an_unknown_dossier(store):
    got, errors = folder.ensure_system_folder("d9", "projets")
    assert got is None and errors == [folder.DOSSIER_NOT_FOUND]
    assert _folders(store) == {}
    got, errors = folder.ensure_system_folder("", "projets")
    assert got is None and errors == [folder.DOSSIER_REQUIRED]


def test_get_or_create_folder_is_gone():
    assert not hasattr(folder, "get_or_create_folder")


# ── Suppression périmée ───────────────────────────────────────────────────


def _seed_document(store, doc_id, folder_id):
    store.seed(f"documents/{doc_id}", {
        "id": doc_id, "dossier_id": "d1", "folder_id": folder_id,
        "display_name": doc_id, "category": "autre",
    })


def test_a_delete_on_a_stale_count_touches_nothing(store):
    """Once the connector moves documents and folders, something may land in
    a folder between the dialog's « 1 fichier » and the click: « Tout
    supprimer » must not destroy what was never shown."""
    _seed_folder(store, "f1", "Pièces")
    _seed_document(store, "a", "f1")
    _seed_document(store, "glisse", "f1")          # moved in after the render

    ok, message, rapport = folder.delete_folder(
        "d1", "f1", contents="delete", expected_documents=1, expected_folders=0,
    )

    assert not ok and "a changé" in message and "2 fichiers" in message
    assert rapport == {"folders": [], "documents": [], "moved": 0}
    assert store.peek("folders/f1") is not None
    assert store.peek("documents/a") and store.peek("documents/glisse")
    assert store.commits == []


def test_a_new_sub_folder_also_makes_the_count_stale(store):
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "neuf", "Neuf", "f1")
    ok, message, _r = folder.delete_folder(
        "d1", "f1", contents="move", expected_documents=0, expected_folders=0,
    )
    assert not ok and "1 sous-dossier." in message
    assert store.peek("folders/neuf") is not None


def test_a_delete_whose_count_still_matches_proceeds(store):
    _seed_folder(store, "f1", "Pièces")
    _seed_document(store, "a", "f1")
    ok, message, rapport = folder.delete_folder(
        "d1", "f1", contents="move", expected_documents=1, expected_folders=0,
    )
    assert ok and message == "" and rapport["moved"] == 1
    assert store.peek("folders/f1") is None
    assert store.peek("documents/a")["folder_id"] is None


def test_a_swap_that_keeps_the_count_is_refused_by_the_fingerprint(store):
    """Review of T2 — FAILS with the counts alone: while the dialog shows
    « 1 fichier », one file is moved OUT and another moved IN (two
    connector calls). « 1 fichier » is still true, so the count check let
    « Tout supprimer » destroy the newcomer, which the dialog never showed.
    The fingerprint names WHICH records were announced."""
    _seed_folder(store, "f1", "Pièces")
    _seed_document(store, "annonce", "f1")
    _seed_document(store, "ailleurs", None)
    shown = folder.subtree_index("d1")["f1"]

    store.external_write("documents/annonce", {
        "id": "annonce", "dossier_id": "d1", "folder_id": None,
        "display_name": "annonce", "category": "autre",
    })
    store.external_write("documents/ailleurs", {
        "id": "ailleurs", "dossier_id": "d1", "folder_id": "f1",
        "display_name": "ailleurs", "category": "autre",
    })
    live = folder.subtree_index("d1")["f1"]
    assert (live["documents"], live["folders"]) == (shown["documents"], shown["folders"])

    ok, message, rapport = folder.delete_folder(
        "d1", "f1", contents="delete",
        expected_documents=shown["documents"],
        expected_folders=shown["folders"],
        expected_fingerprint=shown["fingerprint"],
    )

    assert not ok and "a changé" in message
    assert rapport == {"folders": [], "documents": [], "moved": 0}
    assert store.peek("documents/ailleurs")["folder_id"] == "f1"
    assert store.peek("folders/f1") is not None
    assert _writes(store) == []


def test_the_announced_fingerprint_lets_an_unchanged_delete_through(store):
    """The dialog's fingerprint and the deletion's are derived by ONE helper
    from the same two reads — an untouched subtree is never refused, and a
    rename inside it (ids unchanged) is not a change."""
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "f2", "Annexes", "f1")
    _seed_document(store, "a", "f1")
    _seed_document(store, "b", "f2")
    shown = folder.subtree_index("d1")["f1"]
    assert len(shown["fingerprint"]) == 64
    store.external_write("documents/b", {
        "id": "b", "dossier_id": "d1", "folder_id": "f2",
        "display_name": "b renommé", "category": "autre",
    })

    ok, message, rapport = folder.delete_folder(
        "d1", "f1", contents="move",
        expected_documents=2, expected_folders=1,
        expected_fingerprint=shown["fingerprint"],
    )

    assert ok and message == "" and rapport["moved"] == 2
    assert store.peek("folders/f1") is None and store.peek("folders/f2") is None


def test_a_system_folder_can_still_be_deleted_and_comes_back_at_its_id(store):
    """Deleting stays the lawyer's call (nothing forbids it), and the next
    generation recreates the folder at the SAME deterministic id — the
    documented exception to « ids are never reused »."""
    projets, _ = folder.ensure_system_folder("d1", "projets")
    ok, _m, _r = folder.delete_folder("d1", projets["id"], contents="move")
    assert ok and store.peek(f"folders/{projets['id']}") is None
    again, errors = folder.ensure_system_folder("d1", "projets")
    assert errors == [] and again["id"] == projets["id"]


# ═══════════════════════════════════════════════════════════════════════════
# Suppression d'un dossier de classement (2026-08-14)
#
# « Supprimer le contenu » est la SEULE cascade destructive de l'application —
# explicitement consentie, décomptée avant le clic, journalisée entité par
# entité. Ce qui suit épingle l'ordre load-bearing (documents d'abord,
# enregistrements de dossiers ensuite) et le fail CLOSED : un échec sur les
# documents ne doit JAMAIS supprimer le dossier, sous peine de laisser des
# fichiers avec un folder_id mort — invisibles dans le navigateur.
# ═══════════════════════════════════════════════════════════════════════════


class _FauxRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._id = doc_id


class _FauxBatch:
    def __init__(self, journal):
        self._journal = journal
        self._ops = []

    def update(self, ref, fields):
        self._ops.append(("update", ref, dict(fields)))

    def delete(self, ref):
        self._ops.append(("delete", ref, None))

    def commit(self):
        self._journal.append(list(self._ops))
        for kind, ref, fields in self._ops:
            if kind == "update":
                ref._store[ref._id].update(fields)
            else:
                ref._store.pop(ref._id, None)
        self._ops = []


class _FauxCollection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _FauxRef(self._store, doc_id)


class _FauxDB:
    def __init__(self, folders, documents, journal):
        self._stores = {"folders": folders, "documents": documents}
        self._journal = journal

    def collection(self, name):
        return _FauxCollection(self._stores[name])

    def batch(self):
        return _FauxBatch(self._journal)


def _arbre(monkeypatch):
    """d1 : « Pièces » (f1) ▸ « Annexes » (f2) — 1 document dans f1, 2 dans
    f2 — plus un document à la racine et un cousin dans « Ailleurs » (fx)."""
    folders = {
        "f1": {"id": "f1", "dossier_id": "d1", "name": "Pièces",
               "parent_folder_id": None},
        "f2": {"id": "f2", "dossier_id": "d1", "name": "Annexes",
               "parent_folder_id": "f1"},
        "fx": {"id": "fx", "dossier_id": "d1", "name": "Ailleurs",
               "parent_folder_id": None},
    }
    documents = {
        "a": {"id": "a", "dossier_id": "d1", "folder_id": "f1",
              "display_name": "Requête", "category": "procédure"},
        "b": {"id": "b", "dossier_id": "d1", "folder_id": "f2",
              "display_name": "Annexe 1", "category": "pièce"},
        "c": {"id": "c", "dossier_id": "d1", "folder_id": "f2",
              "display_name": "Annexe 2", "category": "pièce"},
        "racine": {"id": "racine", "dossier_id": "d1", "folder_id": None,
                   "display_name": "Note", "category": "autre"},
        "cousin": {"id": "cousin", "dossier_id": "d1", "folder_id": "fx",
                   "display_name": "Autre", "category": "autre"},
    }
    journal: list = []
    monkeypatch.setattr(folder, "db", _FauxDB(folders, documents, journal))
    monkeypatch.setattr(
        folder, "_all_folders",
        lambda did: [dict(f) for f in folders.values() if f["dossier_id"] == did],
    )
    monkeypatch.setattr(
        folder, "_all_documents",
        lambda did: [dict(d) for d in documents.values() if d["dossier_id"] == did],
    )
    monkeypatch.setattr(
        folder, "get_folder",
        lambda did, fid: dict(folders[fid]) if fid in folders else None,
    )
    monkeypatch.setattr(folder, "_touch_folder", lambda did, fid: None)
    return folders, documents, journal


# ── Les décomptes du dialogue ──────────────────────────────────────────────


def test_subtree_index_compte_le_sous_arbre_et_le_niveau(monkeypatch):
    _arbre(monkeypatch)
    index = folder.subtree_index("d1")
    # « Pièces » : 3 fichiers en tout (1 + 2), 1 sous-dossier…
    assert index["f1"]["documents"] == 3
    assert index["f1"]["folders"] == 1
    # …mais la ligne n'affiche que le niveau : 1 sous-dossier + 1 fichier.
    assert index["f1"]["direct"] == 2
    # Changé délibérément (revue de T2) : l'entrée porte aussi l'EMPREINTE
    # des éléments annoncés, que le dialogue renvoie avec le décompte.
    assert index["f2"] == {
        "direct": 2, "documents": 2, "folders": 0,
        "fingerprint": folder._subtree_fingerprint(["f2"], [{"id": "b"}, {"id": "c"}]),
    }
    assert index["f1"]["fingerprint"] == folder._subtree_fingerprint(
        ["f1", "f2"], [{"id": "a"}, {"id": "b"}, {"id": "c"}],
    )


@pytest.mark.parametrize("lecteur", ["_all_folders", "_all_documents"])
def test_subtree_index_echoue_ferme(monkeypatch, lecteur):
    """Un décompte illisible ne doit pas passer pour « dossier vide » : LES
    DEUX lectures propagent, contrairement à list_folders et list_documents,
    qui s'ouvrent toutes les deux. Le cas des DOCUMENTS est le
    piège : sans lecteur dédié, une panne de lecture aurait affiché « Ce
    dossier est vide », le dossier aurait été supprimé, et les fichiers
    seraient restés avec un folder_id mort — le bogue même que ce lot
    supprime, réintroduit par la porte de derrière."""
    _arbre(monkeypatch)

    def _boom(_did):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(folder, lecteur, _boom)
    with pytest.raises(RuntimeError):
        folder.subtree_index("d1")


@pytest.mark.parametrize("lecteur", ["_all_folders", "_all_documents"])
def test_la_suppression_refuse_quand_le_contenu_est_illisible(monkeypatch, lecteur):
    """Et delete_folder transforme cette propagation en refus net : rien
    n'est supprimé, ni fichier ni dossier."""
    folders, documents, journal = _arbre(monkeypatch)

    def _boom(_did):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(folder, lecteur, _boom)
    for mode in ("move", "delete"):
        ok, err, rapport = folder.delete_folder("d1", "f1", contents=mode)
        assert not ok, mode
        assert "Impossible de lire le contenu" in err, mode
        assert "f1" in folders and "f2" in folders, mode
        assert journal == [], mode
        assert rapport["documents"] == [] and rapport["folders"] == [], mode


# ── Mode « move » ──────────────────────────────────────────────────────────


def test_move_reparente_tout_le_sous_arbre_en_une_ecriture(monkeypatch):
    folders, documents, journal = _arbre(monkeypatch)
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="move")
    assert ok and err == ""
    # Les trois fichiers du sous-arbre passent au parent de « Pièces »
    # (None = racine), y compris ceux qui étaient deux niveaux plus bas.
    assert documents["a"]["folder_id"] is None
    assert documents["b"]["folder_id"] is None
    assert documents["c"]["folder_id"] is None
    # UNE écriture par document — l'ancienne récursion en faisait une par
    # niveau traversé (et mintait un etag à chaque fois).
    ecritures = [op for lot in journal for op in lot if op[0] == "update"]
    assert len(ecritures) == 3
    assert "f1" not in folders and "f2" not in folders
    assert "fx" in folders
    assert rapport["moved"] == 3
    assert rapport["documents"] == []            # rien n'a été supprimé
    assert {f["id"] for f in rapport["folders"]} == {"f1", "f2"}
    # Hors du sous-arbre : intact.
    assert documents["cousin"]["folder_id"] == "fx"
    assert documents["racine"]["folder_id"] is None


def test_move_vers_un_parent_intermediaire(monkeypatch):
    """Supprimer un sous-dossier remonte ses fichiers d'UN niveau, pas à la
    racine — le libellé du dialogue dit bien « dossier parent »."""
    _folders, documents, _journal = _arbre(monkeypatch)
    ok, _err, _rapport = folder.delete_folder("d1", "f2", contents="move")
    assert ok
    assert documents["b"]["folder_id"] == "f1"
    assert documents["c"]["folder_id"] == "f1"


# ── Mode « delete » ────────────────────────────────────────────────────────


def test_delete_supprime_les_documents_du_sous_arbre(monkeypatch):
    folders, documents, _journal = _arbre(monkeypatch)
    import models.document as document

    supprimes: list = []

    def faux_delete(doc_id):
        supprimes.append(doc_id)
        documents.pop(doc_id, None)
        return True, ""

    monkeypatch.setattr(document, "delete_document", faux_delete)

    ok, err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert ok and err == ""
    assert set(supprimes) == {"a", "b", "c"}
    assert "f1" not in folders and "f2" not in folders
    # Le compte-rendu porte de quoi journaliser UNE entité à la fois.
    assert {d["id"] for d in rapport["documents"]} == {"a", "b", "c"}
    assert {f["id"] for f in rapport["folders"]} == {"f1", "f2"}
    assert rapport["documents"][0]["display_name"]    # un titre pour la piste
    assert rapport["moved"] == 0
    # Hors du sous-arbre : intact.
    assert "cousin" in documents and "racine" in documents


def test_delete_echoue_ferme_et_conserve_les_dossiers(monkeypatch):
    """LE point : si une suppression de fichier échoue, le dossier RESTE.
    L'ancienne version avalait l'erreur et supprimait quand même, laissant
    des documents au folder_id mort, invisibles dans l'interface."""
    folders, documents, _journal = _arbre(monkeypatch)
    import models.document as document

    def faux_delete(doc_id):
        if doc_id == "c":
            return False, "Erreur lors de la suppression du fichier."
        documents.pop(doc_id, None)
        return True, ""

    monkeypatch.setattr(document, "delete_document", faux_delete)

    ok, err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert not ok
    assert "conservé" in err and "réessayez" in err.lower()
    # Les dossiers survivent : l'arborescence reste navigable et l'opération
    # se rejoue sur ce qui reste.
    assert "f1" in folders and "f2" in folders
    assert rapport["folders"] == []
    # Le compte-rendu dit honnêtement ce qui est déjà parti.
    assert len(rapport["documents"]) < 3


def test_delete_refuse_au_dela_du_plafond(monkeypatch):
    folders, _documents, journal = _arbre(monkeypatch)
    monkeypatch.setattr(folder, "MAX_FOLDER_DELETE_DOCUMENTS", 2)
    ok, err, _rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert not ok
    assert "3 fichiers" in err and "limite de 2" in err
    assert "f1" in folders and journal == []          # aucune écriture


def test_un_mode_inconnu_ne_supprime_jamais(monkeypatch):
    """Un champ de formulaire absent, périmé ou forgé retombe sur « move » :
    la branche destructive est un consentement, jamais un défaut."""
    for mode in ("", "recursive", "true", "DELETE", "supprimer"):
        _folders, documents, _journal = _arbre(monkeypatch)
        ok, _err, rapport = folder.delete_folder("d1", "f1", contents=mode)
        assert ok, mode
        assert rapport["documents"] == [], mode       # rien de supprimé
        assert documents["a"]["folder_id"] is None, mode


# ── Dossier vide, dossier introuvable ──────────────────────────────────────


def test_dossier_vide_dans_les_deux_modes(monkeypatch):
    for mode in ("move", "delete"):
        folders, _documents, _journal = _arbre(monkeypatch)
        folders["vide"] = {"id": "vide", "dossier_id": "d1", "name": "Vide",
                           "parent_folder_id": None}
        ok, err, rapport = folder.delete_folder("d1", "vide", contents=mode)
        assert ok and err == "", mode
        assert "vide" not in folders, mode
        assert rapport["documents"] == [] and rapport["moved"] == 0, mode


def test_dossier_introuvable(monkeypatch):
    _arbre(monkeypatch)
    ok, err, _rapport = folder.delete_folder("d1", "fantome", contents="delete")
    assert not ok and "introuvable" in err
