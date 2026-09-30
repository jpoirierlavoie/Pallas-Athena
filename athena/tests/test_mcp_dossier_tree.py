"""create_dossier gives the new dossier its default filing tree (2026-09-30).

The connector's ``create_dossier`` calls ``models.folder.ensure_default_tree``
AFTER the dossier's commit: the eighteen folders of the tree, the seven
system ones stamped at their deterministic ids. Three properties are pinned
here, on the shared fake Firestore (the REAL client over an in-memory
server — models, ``dav.sync`` and the idempotency store included), read back
from what is STORED:

* the tree is the registry's, node for node, stamped « mcp »;
* a same-key retry REPLAYS the stored result and adds no folder;
* a failure after the commit is ``CommittedWriteError`` naming the DOSSIER
  alone — the eighteen folder writes are noted idempotent by the engine
  (a retry reproduces them, never repeats them), so the partial record and
  the error never count them as writes a retry would repeat.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools
    from models import folder as folder_model
    from models import partie as partie_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
KEY = "cle-dossier-arbo-0001"


@pytest.fixture
def db(monkeypatch):
    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.")
                   or n in ("dav.sync", "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
    fake.seed("parties/p1", {
        **partie_model._default_doc(), "id": "p1", "type": "individual",
        "contact_role": "client", "first_name": "Jean", "last_name": "Tremblay",
        "etag": "e-p1", "created_at": DT, "updated_at": DT})
    return fake


def _args(**over) -> dict:
    args = {"file_number": "2026-101", "title": "Tremblay c. Lavoie",
            "clients": [{"partie_id": "p1", "roles": ["demandeur"]}]}
    args.update(over)
    return args


def _folders(db, dossier_id: str) -> dict:
    return {fid: f for fid, f in db.peek_collection("folders").items()
            if f.get("dossier_id") == dossier_id}


def test_a_new_dossier_gets_the_whole_default_tree_stamped_mcp(db):
    payload = handlers.create_dossier(_args())
    did = payload["entity"]["id"]
    assert payload["created"] is True
    assert not any("arborescence" in w for w in payload["warnings"])
    stored = _folders(db, did)
    assert len(stored) == len(folder_model.DEFAULT_TREE) == 18
    for node in folder_model.DEFAULT_TREE:
        folder = stored[folder_model.node_folder_id(did, node.key)]
        parent = (folder_model.node_folder_id(did, node.parent)
                  if node.parent else None)
        assert folder["name"] == node.name, node.key
        assert folder["parent_folder_id"] == parent, node.key
        assert folder["system_role"] == node.role, node.key
        assert folder["created_via"] == "mcp", node.key
    stamped = {f["system_role"]: fid for fid, f in stored.items()
               if f["system_role"]}
    assert set(stamped) == set(folder_model.VALID_SYSTEM_ROLES)
    for role, fid in stamped.items():
        assert fid == folder_model.system_folder_id(did, role)
    # « Projets » under « Interne », « Factures » and « Déboursés » under
    # « Mandat », « Reçus du portail » under « Autres ».
    sid = folder_model.system_folder_id
    assert stored[sid(did, "projets")]["parent_folder_id"] == sid(did, "interne")
    assert stored[sid(did, "factures")]["parent_folder_id"] == sid(did, "mandat")
    assert stored[sid(did, "debourses")]["parent_folder_id"] == sid(did, "mandat")
    assert stored[sid(did, "portail")]["parent_folder_id"] == sid(did, "autres")


def test_a_same_key_retry_replays_and_adds_no_folder(db):
    first = handlers.create_dossier(_args(idempotency_key=KEY))
    did = first["entity"]["id"]
    before = _folders(db, did)
    assert len(before) == 18
    again = handlers.create_dossier(_args(idempotency_key=KEY))
    assert again["idempotent_replay"] is True
    assert again["entity"]["id"] == did
    assert _folders(db, did) == before
    assert len(db.peek_collection("dossiers")) == 1


def test_a_failure_after_the_commit_names_only_the_dossier(db, monkeypatch):
    """The payload builder raising after the dossier AND its tree committed:
    « ENREGISTRÉE — NE PAS RÉESSAYER », for the dossier alone — the tree's
    eighteen creations are idempotent writes (``note_commit(…,
    idempotent=True)``), which a retry would find again, never repeat."""
    def _boom(*a, **k):
        raise RuntimeError("payload builder")

    monkeypatch.setattr(handlers, "_dossier_write_result", _boom)
    with pytest.raises(tools.CommittedWriteError) as excinfo:
        handlers.create_dossier(_args(idempotency_key=KEY))
    error = excinfo.value
    dossiers = db.peek_collection("dossiers")
    assert len(dossiers) == 1
    (did,) = dossiers
    assert error.collection == "dossiers" and error.rows == 1
    assert error.entity_id == did and error.dossier_id == did
    assert len(_folders(db, did)) == 18                   # the tree stands
    (entry,) = db.peek_collection("mcp_idempotency").values()
    assert entry["status"] == "partial"
    assert entry["collection"] == "dossiers" and entry["rows"] == 1
    assert entry["commits"] == [{"collection": "dossiers", "id": did}]

    # The same key re-raises from the partial record — nothing re-runs.
    with pytest.raises(tools.CommittedWriteError) as replay:
        handlers.create_dossier(_args(idempotency_key=KEY))
    assert replay.value.replay is True
    assert len(db.peek_collection("dossiers")) == 1
    assert len(_folders(db, did)) == 18


def test_an_unreadable_tree_is_a_warning_on_the_created_dossier(db, monkeypatch):
    """On the REAL engine: the folders cannot be read, so the tree is
    refused (fail closed) — the dossier is committed, the call a success
    with the warning, and the idempotency record the stored result."""
    def _raises(dossier_id):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(folder_model, "_all_folders", _raises)
    payload = handlers.create_dossier(_args(idempotency_key=KEY))
    did = payload["entity"]["id"]
    assert payload["created"] is True
    assert db.peek(f"dossiers/{did}") is not None
    assert _folders(db, did) == {}
    tree = [w for w in payload["warnings"] if "arborescence" in w]
    assert tree and "ne le recréez pas" in tree[0]
    assert folder_model.DEFAULT_TREE_BUTTON in tree[0]
    (entry,) = db.peek_collection("mcp_idempotency").values()
    assert entry["status"] == "committed"
