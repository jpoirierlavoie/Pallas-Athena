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
    # Review of T2: the loser READ BACK the winner's folder — it did not
    # overwrite it. A `set()` at the deterministic id would pass every
    # assertion above (one folder, one id) while replacing the winner's
    # record; the etag is what tells the two apart.
    stored = store.peek(f"folders/{first['id']}")
    assert stored["etag"] == second["etag"] == first["etag"]


def test_the_loser_of_the_race_never_overwrites_the_existing_folder(store, monkeypatch):
    """Review of T2 — the create() / AlreadyExists contract on its own: the
    system folder exists (created an instant ago by another generation) but
    this call's read predates it. Its create() meets AlreadyExists and it
    reads the stored folder back; nothing it built is written."""
    fid = folder.system_folder_id("d1", "projets")
    _seed_folder(store, fid, "Projets", system_role="projets", etag="e-premier")
    monkeypatch.setattr(folder, "_all_folders", lambda dossier_id: [])

    got, errors = folder.ensure_system_folder("d1", "projets")

    assert errors == [] and got["etag"] == "e-premier"
    assert store.peek(f"folders/{fid}")["etag"] == "e-premier"
    assert store.peek(f"folders/{fid}")["created_at"] == T0


def test_parallel_adoptions_stamp_the_legacy_folder_once(store, monkeypatch):
    """Two generations on a dossier holding a legacy « Projets »: the second
    adoption runs entirely between the first's read and its transaction.
    The transaction re-reads the folder, sees the stamp, and returns it —
    one stamp, one `system_folder_adopted` line, never a second folder."""
    _seed_folder(store, "legacy", "Projets")
    events = []
    monkeypatch.setattr(
        folder, "log_dossier_event",
        lambda event, dossier_id, **kw: events.append((event, kw)),
    )
    real_read = folder._all_folders
    interleaved = []

    def _read_then_let_the_other_call_run(dossier_id):
        rows = real_read(dossier_id)
        if not interleaved:
            interleaved.append(None)
            interleaved[0] = folder.ensure_system_folder(dossier_id, "projets")
        return rows

    monkeypatch.setattr(folder, "_all_folders", _read_then_let_the_other_call_run)
    first, errors = folder.ensure_system_folder("d1", "projets")

    second, second_errors = interleaved[0]
    assert errors == second_errors == []
    assert first["id"] == second["id"] == "legacy"
    assert first["etag"] == second["etag"] == store.peek("folders/legacy")["etag"]
    assert [e for e, _kw in events] == ["system_folder_adopted"]
    assert list(_folders(store)) == ["legacy"]


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
    # The report gained `orphaned_files` (fixups of lot 2A — files whose
    # records went but whose bytes could not be erased): deliberately.
    assert rapport == {"folders": [], "documents": [], "moved": 0,
                       "orphaned_files": 0}
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
    # The report gained `orphaned_files` (fixups of lot 2A — files whose
    # records went but whose bytes could not be erased): deliberately.
    assert rapport == {"folders": [], "documents": [], "moved": 0,
                       "orphaned_files": 0}
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
#
# PORTÉ DÉLIBÉRÉMENT sur le faux Firestore partagé (correctifs du lot 2A) :
# ces tests tournaient sur un faux fait main (_FauxDB — `batch()` et
# `document()` seulement). La suppression relit désormais son sous-arbre DANS
# une transaction avant chaque écriture destructive (la course de la revue
# de T7), ce qu'un tel faux ne sait pas faire : chaque assertion d'origine
# est conservée, relue dans ce que le magasin STOCKE. Le mode « delete »
# efface les fichiers par le faux Cloud Storage réaliste — il ne passe plus
# par `document.delete_document` (fichier d'abord, hors transaction), dont
# le test qui le simulait est remplacé par les deux pannes réelles : celle de
# la transaction (rien n'est supprimé) et celle d'un fichier (orphelin compté).
# ═══════════════════════════════════════════════════════════════════════════


from tests._fake_gcs import FakeBucket  # noqa: E402


def _arbre(store, monkeypatch):
    """d1 : « Pièces » (f1) ▸ « Annexes » (f2) — 1 document dans f1, 2 dans
    f2 — plus un document à la racine et un cousin dans « Ailleurs » (fx).
    Chaque document a son fichier dans le faux seau."""
    bucket = FakeBucket()
    monkeypatch.setattr(folder.storage, "bucket", lambda: bucket)
    _seed_folder(store, "f1", "Pièces")
    _seed_folder(store, "f2", "Annexes", "f1")
    _seed_folder(store, "fx", "Ailleurs")
    for doc_id, fid, name, cat in (
        ("a", "f1", "Requête", "procédure"),
        ("b", "f2", "Annexe 1", "pièce"),
        ("c", "f2", "Annexe 2", "pièce"),
        ("racine", None, "Note", "autre"),
        ("cousin", "fx", "Autre", "autre"),
    ):
        path = f"users/u1/dossiers/d1/documents/{doc_id}/{doc_id}.pdf"
        store.seed(f"documents/{doc_id}", {
            "id": doc_id, "dossier_id": "d1", "folder_id": fid,
            "display_name": name, "category": cat, "storage_path": path,
        })
        bucket.put(path, b"%PDF-" + doc_id.encode())
    monkeypatch.setattr(folder, "_touch_folder", lambda did, fid: None)
    return bucket


def _doc(store, doc_id):
    return store.peek(f"documents/{doc_id}")


# ── Les décomptes du dialogue ──────────────────────────────────────────────


def test_subtree_index_compte_le_sous_arbre_et_le_niveau(store, monkeypatch):
    _arbre(store, monkeypatch)
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
def test_subtree_index_echoue_ferme(store, monkeypatch, lecteur):
    """Un décompte illisible ne doit pas passer pour « dossier vide » : LES
    DEUX lectures propagent, contrairement à list_folders et list_documents,
    qui s'ouvrent toutes les deux. Le cas des DOCUMENTS est le
    piège : sans lecteur dédié, une panne de lecture aurait affiché « Ce
    dossier est vide », le dossier aurait été supprimé, et les fichiers
    seraient restés avec un folder_id mort — le bogue même que ce lot
    supprime, réintroduit par la porte de derrière."""
    _arbre(store, monkeypatch)

    def _boom(_did):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(folder, lecteur, _boom)
    with pytest.raises(RuntimeError):
        folder.subtree_index("d1")


@pytest.mark.parametrize("lecteur", ["_all_folders", "_all_documents"])
def test_la_suppression_refuse_quand_le_contenu_est_illisible(
    store, monkeypatch, lecteur,
):
    """Et delete_folder transforme cette propagation en refus net : rien
    n'est supprimé, ni fichier ni dossier."""
    bucket = _arbre(store, monkeypatch)

    def _boom(_did):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(folder, lecteur, _boom)
    for mode in ("move", "delete"):
        ok, err, rapport = folder.delete_folder("d1", "f1", contents=mode)
        assert not ok, mode
        assert "Impossible de lire le contenu" in err, mode
        assert store.peek("folders/f1") and store.peek("folders/f2"), mode
        assert _writes(store) == [], mode
        assert rapport["documents"] == [] and rapport["folders"] == [], mode
    assert len(bucket.objects) == 5


# ── Mode « move » ──────────────────────────────────────────────────────────


def test_move_reparente_tout_le_sous_arbre_en_une_ecriture(store, monkeypatch):
    bucket = _arbre(store, monkeypatch)
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="move")
    assert ok and err == ""
    # Les trois fichiers du sous-arbre passent au parent de « Pièces »
    # (None = racine), y compris ceux qui étaient deux niveaux plus bas.
    for doc_id in ("a", "b", "c"):
        assert _doc(store, doc_id)["folder_id"] is None
    # UNE écriture par document — l'ancienne récursion en faisait une par
    # niveau traversé (et mintait un etag à chaque fois).
    ecritures = [op for op in _writes(store) if op[0] == "update"]
    assert sorted(ecritures) == [
        ("update", "documents/a"), ("update", "documents/b"),
        ("update", "documents/c"),
    ]
    assert store.peek("folders/f1") is None and store.peek("folders/f2") is None
    assert store.peek("folders/fx") is not None
    assert rapport["moved"] == 3
    assert rapport["documents"] == []            # rien n'a été supprimé
    assert {f["id"] for f in rapport["folders"]} == {"f1", "f2"}
    # Hors du sous-arbre : intact. Et aucun fichier n'est effacé.
    assert _doc(store, "cousin")["folder_id"] == "fx"
    assert _doc(store, "racine")["folder_id"] is None
    assert len(bucket.objects) == 5


def test_move_vers_un_parent_intermediaire(store, monkeypatch):
    """Supprimer un sous-dossier remonte ses fichiers d'UN niveau, pas à la
    racine — le libellé du dialogue dit bien « dossier parent »."""
    _arbre(store, monkeypatch)
    ok, _err, _rapport = folder.delete_folder("d1", "f2", contents="move")
    assert ok
    assert _doc(store, "b")["folder_id"] == "f1"
    assert _doc(store, "c")["folder_id"] == "f1"
    assert store.peek("folders/f1") is not None


# ── Mode « delete » ────────────────────────────────────────────────────────


def test_delete_supprime_les_documents_du_sous_arbre(store, monkeypatch):
    bucket = _arbre(store, monkeypatch)
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert ok and err == ""
    for doc_id in ("a", "b", "c"):
        assert _doc(store, doc_id) is None
        assert f"users/u1/dossiers/d1/documents/{doc_id}/{doc_id}.pdf" not in bucket.objects
    assert store.peek("folders/f1") is None and store.peek("folders/f2") is None
    # Le compte-rendu porte de quoi journaliser UNE entité à la fois.
    assert {d["id"] for d in rapport["documents"]} == {"a", "b", "c"}
    assert {f["id"] for f in rapport["folders"]} == {"f1", "f2"}
    assert rapport["documents"][0]["display_name"]    # un titre pour la piste
    assert rapport["moved"] == 0 and rapport["orphaned_files"] == 0
    # Hors du sous-arbre : intact, fichiers compris.
    assert _doc(store, "cousin") and _doc(store, "racine")
    assert len(bucket.objects) == 2


def test_delete_echoue_ferme_et_conserve_les_dossiers(store, monkeypatch):
    """LE point : si la suppression des fichiers échoue, le dossier RESTE.
    L'ancienne version avalait l'erreur et supprimait quand même, laissant
    des documents au folder_id mort, invisibles dans l'interface.

    Réécrit délibérément (correctifs du lot 2A) : la panne simulée était
    celle de `document.delete_document` sur le 3e fichier ; les fichiers
    d'un lot se suppriment désormais dans UNE transaction, si bien qu'une
    panne de son commit ne supprime RIEN — ni enregistrement, ni fichier."""
    bucket = _arbre(store, monkeypatch)

    def _panne(info):
        if any(path.startswith("documents/") for _k, path in info.ops):
            raise gexc.ServiceUnavailable("commit refusé")

    store.add_commit_hook(_panne)
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert not ok
    assert "conservé" in err and "réessayez" in err.lower()
    # Les dossiers survivent : l'arborescence reste navigable et l'opération
    # se rejoue sur ce qui reste.
    assert store.peek("folders/f1") and store.peek("folders/f2")
    assert rapport["folders"] == [] and rapport["documents"] == []
    for doc_id in ("a", "b", "c"):
        assert _doc(store, doc_id)["folder_id"] in ("f1", "f2")
    assert len(bucket.objects) == 5


def test_un_fichier_non_efface_est_compte_comme_orphelin(store, monkeypatch):
    """Les ENREGISTREMENTS partent d'abord, dans la transaction ; un fichier
    que le stockage refuse d'effacer ensuite reste orphelin sous le préfixe
    du propriétaire — référencé par rien — et le compte-rendu le dit."""
    bucket = _arbre(store, monkeypatch)
    real_blob = bucket.blob

    class _Tenace:
        def __init__(self, blob):
            self._blob = blob

        def delete(self, **_kw):
            raise gexc.ServiceUnavailable("stockage indisponible")

    monkeypatch.setattr(
        bucket, "blob",
        lambda name: _Tenace(real_blob(name)) if name.endswith("/b.pdf")
        else real_blob(name))
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert ok and err == ""
    assert _doc(store, "b") is None                  # l'enregistrement est parti
    assert "users/u1/dossiers/d1/documents/b/b.pdf" in bucket.objects
    assert rapport["orphaned_files"] == 1
    assert {d["id"] for d in rapport["documents"]} == {"a", "b", "c"}


def test_delete_refuse_au_dela_du_plafond(store, monkeypatch):
    _arbre(store, monkeypatch)
    monkeypatch.setattr(folder, "MAX_FOLDER_DELETE_DOCUMENTS", 2)
    ok, err, _rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert not ok
    assert "3 fichiers" in err and "limite de 2" in err
    assert store.peek("folders/f1") and _writes(store) == []   # aucune écriture


def test_un_mode_inconnu_ne_supprime_jamais(store, monkeypatch):
    """Un champ de formulaire absent, périmé ou forgé retombe sur « move » :
    la branche destructive est un consentement, jamais un défaut."""
    for mode in ("", "recursive", "true", "DELETE", "supprimer"):
        _arbre(store, monkeypatch)
        ok, _err, rapport = folder.delete_folder("d1", "f1", contents=mode)
        assert ok, mode
        assert rapport["documents"] == [], mode       # rien de supprimé
        assert _doc(store, "a")["folder_id"] is None, mode


# ── Dossier vide, dossier introuvable ──────────────────────────────────────


def test_dossier_vide_dans_les_deux_modes(store, monkeypatch):
    for mode in ("move", "delete"):
        _arbre(store, monkeypatch)
        _seed_folder(store, "vide", "Vide")
        ok, err, rapport = folder.delete_folder("d1", "vide", contents=mode)
        assert ok and err == "", mode
        assert store.peek("folders/vide") is None, mode
        assert rapport["documents"] == [] and rapport["moved"] == 0, mode


def test_dossier_introuvable(store, monkeypatch):
    _arbre(store, monkeypatch)
    ok, err, _rapport = folder.delete_folder("d1", "fantome", contents="delete")
    assert not ok and "introuvable" in err


# ── La course de la revue de T7 (correctifs du lot 2A) ─────────────────────
#
# Le connecteur reclasse désormais des documents (move_documents,
# update_document) et crée des dossiers de classement (manage_folder). Une
# telle écriture qui tombe ENTRE la lecture du sous-arbre et les écritures
# en lot de la suppression laissait un document sous un folder_id mort (en
# « delete » comme en « move »), et en « move » l'écriture de reparentage,
# sans garde, ramenait au parent un document que le connecteur venait de
# classer ailleurs. Le crochet de commit du faux serveur glisse l'écriture
# concurrente juste avant la première écriture destructive — la fenêtre
# même de l'ancien code. Chaque test ÉCHOUE sur lui.


def _race_on_first_destructive_commit(store, path, data):
    """Slip ANOTHER writer's write of *path* in right before the first
    commit that deletes or rewrites a document or a folder."""
    fired = []

    def _hook(info):
        if fired:
            return
        if any(kind in ("update", "delete", "set")
               and (p.startswith("documents/") or p.startswith("folders/"))
               for kind, p in info.ops):
            fired.append(True)
            store.external_write(path, data)

    store.add_commit_hook(_hook)
    return fired


@pytest.mark.parametrize("mode", ["move", "delete"])
def test_un_document_classe_dans_le_sous_arbre_pendant_la_suppression_n_est_jamais_orphelin(
    store, monkeypatch, mode,
):
    _arbre(store, monkeypatch)
    fired = _race_on_first_destructive_commit(store, "documents/racine", {
        "id": "racine", "dossier_id": "d1", "folder_id": "f2",
        "display_name": "Note", "category": "autre",
        "storage_path": "users/u1/dossiers/d1/documents/racine/racine.pdf",
    })

    ok, err, rapport = folder.delete_folder(
        "d1", "f1", contents=mode, expected_documents=3, expected_folders=1)

    assert fired
    assert not ok and "a changé pendant la suppression" in err
    assert "Le dossier a été conservé" in err
    # Le document glissé dedans vit dans un dossier VIVANT, jamais mort.
    assert _doc(store, "racine")["folder_id"] == "f2"
    assert store.peek("folders/f2") is not None
    assert store.peek("folders/f1") is not None
    assert rapport["folders"] == []
    # Rien n'a été fait : la première transaction a vu le changement.
    assert rapport["documents"] == [] and rapport["moved"] == 0
    for doc_id in ("a", "b", "c"):
        assert _doc(store, doc_id) is not None


def test_un_document_sorti_du_sous_arbre_n_est_pas_ramene_au_parent(
    store, monkeypatch,
):
    """« move » : le connecteur classe « b » dans « Ailleurs » pendant la
    suppression — l'ancien reparentage, sans garde, le ramenait à la racine."""
    _arbre(store, monkeypatch)
    _race_on_first_destructive_commit(store, "documents/b", {
        "id": "b", "dossier_id": "d1", "folder_id": "fx",
        "display_name": "Annexe 1", "category": "pièce",
        "storage_path": "users/u1/dossiers/d1/documents/b/b.pdf",
    })
    ok, err, _rapport = folder.delete_folder("d1", "f1", contents="move")
    assert not ok and "a changé pendant la suppression" in err
    assert _doc(store, "b")["folder_id"] == "fx"
    assert store.peek("folders/f1") is not None


def test_un_document_sorti_du_sous_arbre_n_est_pas_detruit(store, monkeypatch):
    """« delete » : le connecteur sort « b » du sous-arbre pendant la
    suppression — « Tout supprimer » ne détruit pas un document qui n'y est
    plus, ni son fichier."""
    bucket = _arbre(store, monkeypatch)
    _race_on_first_destructive_commit(store, "documents/b", {
        "id": "b", "dossier_id": "d1", "folder_id": "fx",
        "display_name": "Annexe 1", "category": "pièce",
        "storage_path": "users/u1/dossiers/d1/documents/b/b.pdf",
    })
    ok, _err, rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert not ok
    assert _doc(store, "b")["folder_id"] == "fx"
    assert "users/u1/dossiers/d1/documents/b/b.pdf" in bucket.objects
    assert rapport["documents"] == []


def test_un_document_classe_apres_les_fichiers_bloque_la_suppression_des_dossiers(
    store, monkeypatch,
):
    """La fenêtre entre la phase des documents et celle des dossiers : un
    document classé dans « Annexes » APRÈS le reparentage de son contenu.
    Les dossiers sont conservés — et ce qui était déjà déplacé est dit."""
    _arbre(store, monkeypatch)
    fired = []

    def _hook(info):
        if fired:
            return
        if any(p.startswith("folders/") and kind == "delete"
               for kind, p in info.ops):
            fired.append(True)
            store.external_write("documents/racine", {
                "id": "racine", "dossier_id": "d1", "folder_id": "f2",
                "display_name": "Note", "category": "autre",
            })

    store.add_commit_hook(_hook)
    ok, err, rapport = folder.delete_folder("d1", "f1", contents="move")
    assert fired
    assert not ok and "3 fichiers avaient déjà été déplacés" in err
    assert store.peek("folders/f1") and store.peek("folders/f2")
    assert _doc(store, "racine")["folder_id"] == "f2"
    assert rapport["moved"] == 3 and rapport["folders"] == []


def test_un_sous_dossier_cree_pendant_la_suppression_n_est_jamais_orphelin(
    store, monkeypatch,
):
    """manage_folder crée un sous-dossier sous « Annexes » pendant la
    suppression : il ne reste jamais sous un parent supprimé."""
    _arbre(store, monkeypatch)
    _race_on_first_destructive_commit(store, "folders/neuf", {
        "id": "neuf", "dossier_id": "d1", "name": "Neuf",
        "parent_folder_id": "f2", "order": 0,
    })
    ok, err, _rapport = folder.delete_folder("d1", "f1", contents="move")
    assert not ok and "a changé pendant la suppression" in err
    assert store.peek("folders/neuf")["parent_folder_id"] == "f2"
    assert store.peek("folders/f2") is not None


def test_chaque_ecriture_destructive_relit_le_sous_arbre_dans_sa_transaction(
    store, monkeypatch,
):
    """La garde est STRUCTURELLE : les deux lectures (dossiers, documents)
    de chaque phase passent par la transaction qui écrit."""
    _arbre(store, monkeypatch)
    store.reset_logs()
    ok, _err, _rapport = folder.delete_folder("d1", "f1", contents="delete")
    assert ok
    transactional_queries = [
        r for r in store.reads if r.rpc == "run_query" and r.transactional]
    # Phase des documents (1 lot) + phase des dossiers : 2 × 2 requêtes.
    assert len(transactional_queries) == 4
    for commit in store.commits:
        if any(p.startswith(("documents/", "folders/")) for _k, p in commit.ops):
            assert commit.transaction is not None
