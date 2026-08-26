"""models/chat_draft.py — versioned drafts (Phase N).

Fake-Firestore harness copied from tests/test_admin_ledger.py (itself from
test_trust.py), extended with SUBCOLLECTIONS (versions/) and db.batch() —
the two shapes this model uses that the canon did not.
"""

import copy
import importlib.util
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")


# ── Conditional third-party stubs (test_trust.py preamble) ─────────────────
def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name: str, module: types.ModuleType) -> None:
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg in sys.modules:
            continue
        if _module_available(pkg):
            importlib.import_module(pkg)
            continue
        pkg_module = types.ModuleType(pkg)
        pkg_module.__path__ = []
        sys.modules[pkg] = pkg_module
        if i > 1:
            setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], pkg_module)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


import importlib  # noqa: E402

if not _module_available("google.cloud.firestore"):
    _firestore_stub = types.ModuleType("google.cloud.firestore")

    class _StubQuery:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"

    _firestore_stub.Client = mock.MagicMock(name="firestore.Client")
    _firestore_stub.Query = _StubQuery
    _firestore_stub.Transaction = type("Transaction", (), {})
    _firestore_stub.transactional = lambda func: func
    _install_stub("google.cloud.firestore", _firestore_stub)

if not _module_available("google.cloud.firestore_v1.base_query"):
    _base_query_stub = types.ModuleType("google.cloud.firestore_v1.base_query")

    class _StubFieldFilter:
        def __init__(self, field_path, op_string, value=None):
            self.field_path = field_path
            self.op_string = op_string
            self.value = value

    _base_query_stub.FieldFilter = _StubFieldFilter
    _install_stub("google.cloud.firestore_v1.base_query", _base_query_stub)


with mock.patch("google.cloud.firestore.Client"):
    import models.chat_draft as cd


# ── Fake Firestore (canon + subcollections + batch) ─────────────────────────


class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def collection(self, name):
        # Subcollection: keyed on the parent path — the harness extension.
        return _FakeCollectionRef(self._store, f"{self._coll}/{self.id}/{name}")

    def get(self, transaction=None):
        return _FakeSnapshot(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def update(self, fields):
        doc = self._store.setdefault(self._coll, {}).get(self.id)
        if doc is None:
            raise KeyError(f"update on missing {self._coll}/{self.id}")
        doc.update(copy.deepcopy(fields))


class _FakeQuery:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._orders = []
        self._limit = None

    def order_by(self, field, direction="ASCENDING"):
        self._orders.append((field, direction))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _rows(self):
        rows = list(self._store.get(self._coll, {}).values())
        for field, direction in reversed(self._orders):
            rows.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction == "DESCENDING"),
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def stream(self, transaction=None):
        return [_FakeSnapshot(d.get("id"), d) for d in self._rows()]


class _FakeCollectionRef(_FakeQuery):
    def document(self, doc_id):
        return _FakeDocRef(self._store, self._coll, doc_id)


class _FakeBatch:
    def __init__(self):
        self._ops = []

    def set(self, ref, data):
        self._ops.append(("set", ref, copy.deepcopy(data)))

    def update(self, ref, fields):
        self._ops.append(("update", ref, copy.deepcopy(fields)))

    def commit(self):
        for op, ref, payload in self._ops:
            getattr(ref, op)(payload)
        self._ops = []


class _FakeTransaction:
    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, fields):
        ref.update(fields)


class _FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCollectionRef(self._store, name)

    def transaction(self):
        return _FakeTransaction()

    def batch(self):
        return _FakeBatch()


class _FakeFirestore:
    transactional = staticmethod(lambda fn: fn)

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(cd, "db", _FakeDB(data))
    monkeypatch.setattr(cd, "firestore", _FakeFirestore)
    return data


def _versions(data, draft_id):
    return data.get(f"chat_drafts/{draft_id}/versions", {})


_VALID = {
    "dossier_id": "d1",
    "dossier_file_number": "2026-001",
    "dossier_title": "Tremblay c. Lavoie",
    "title": "Projet de mise en demeure",
    "content": "# Mise en demeure\n\nPremier jet.",
}


# ── create_draft ────────────────────────────────────────────────────────────

def test_create_writes_head_and_version_one_atomically(store):
    doc, errors = cd.create_draft(dict(_VALID))
    assert errors == []
    assert doc["current_version"] == 1
    assert doc["content_length"] == len(_VALID["content"])
    assert doc["etag"]
    head = store["chat_drafts"][doc["id"]]
    assert head["title"] == _VALID["title"]
    versions = _versions(store, doc["id"])
    assert list(versions) == ["000001"]
    v1 = versions["000001"]
    assert v1["version"] == 1
    assert v1["content"] == _VALID["content"]
    assert v1["provenance"]["created_via"] == "connector"  # no ContextVar set


def test_create_never_honours_a_caller_supplied_id(store):
    doc, errors = cd.create_draft({**_VALID, "id": "id-forge"})
    assert errors == []
    assert doc["id"] != "id-forge"
    assert "id-forge" not in store["chat_drafts"]


def test_create_requires_title_and_content(store):
    doc, errors = cd.create_draft({"title": "  ", "content": ""})
    assert doc is None
    assert any("titre" in e for e in errors)
    assert any("contenu" in e for e in errors)
    assert store.get("chat_drafts", {}) == {}


def test_validate_payload_writes_nothing(store):
    errors = cd.validate_payload(dict(_VALID))
    assert errors == []
    assert store.get("chat_drafts", {}) == {}


# ── revise_draft — spec acceptance test 6 ───────────────────────────────────

def test_revise_appends_version_moves_head_prior_intact(store):
    created, _ = cd.create_draft(dict(_VALID))
    revised, errors = cd.revise_draft(
        created["id"], content="Deuxième jet, revu."
    )
    assert errors == []
    assert revised["current_version"] == 2
    head = store["chat_drafts"][created["id"]]
    assert head["content"] == "Deuxième jet, revu."
    assert head["title"] == _VALID["title"]          # title kept when None
    assert head["etag"] != created["etag"]
    versions = _versions(store, created["id"])
    assert sorted(versions) == ["000001", "000002"]
    # Version 1 INTACT — the append-only invariant.
    assert versions["000001"]["content"] == _VALID["content"]
    assert versions["000002"]["version"] == 2


def test_revise_can_retitle(store):
    created, _ = cd.create_draft(dict(_VALID))
    revised, errors = cd.revise_draft(
        created["id"], content="v2", title="Nouveau titre"
    )
    assert errors == []
    assert revised["title"] == "Nouveau titre"
    assert _versions(store, created["id"])["000001"]["title"] == _VALID["title"]


def test_revise_unknown_id_refuses_never_creates(store):
    doc, errors = cd.revise_draft("inconnu", content="texte")
    assert doc is None
    assert errors == ["Brouillon introuvable."]
    assert "inconnu" not in store.get("chat_drafts", {})


def test_revise_requires_content_and_nonblank_title(store):
    created, _ = cd.create_draft(dict(_VALID))
    doc, errors = cd.revise_draft(created["id"], content="   ")
    assert doc is None and errors
    doc, errors = cd.revise_draft(created["id"], content="ok", title="   ")
    assert doc is None and any("titre" in e for e in errors)
    # Nothing moved.
    assert store["chat_drafts"][created["id"]]["current_version"] == 1


# ── Provenance (ContextVar seam) ────────────────────────────────────────────

def test_provenance_is_whitelisted_from_the_contextvar(store):
    token = cd.PROVENANCE.set(
        {
            "created_via": "chat",
            "conversation_id": "c9",
            "turn_id": "t3",
            "model": "claude-sonnet-5",
            "charter_version": 1,
            "champ_forge": "jamais stocké",
        }
    )
    try:
        doc, _ = cd.create_draft(dict(_VALID))
    finally:
        cd.PROVENANCE.reset(token)
    provenance = _versions(store, doc["id"])["000001"]["provenance"]
    assert provenance["created_via"] == "chat"
    assert provenance["conversation_id"] == "c9"
    assert provenance["model"] == "claude-sonnet-5"
    assert "champ_forge" not in provenance
    # And once reset, the next write is connector-attributed again.
    doc2, _ = cd.create_draft(dict(_VALID))
    assert (
        _versions(store, doc2["id"])["000001"]["provenance"]["created_via"]
        == "connector"
    )


# ── Reads ───────────────────────────────────────────────────────────────────

def test_list_drafts_python_filters_by_dossier_without_an_index(store):
    a, _ = cd.create_draft(dict(_VALID))
    b, _ = cd.create_draft({**_VALID, "dossier_id": "", "title": "Flottant"})
    everything = cd.list_drafts()
    assert {d["id"] for d in everything} == {a["id"], b["id"]}
    scoped = cd.list_drafts(dossier_id="d1")
    assert [d["id"] for d in scoped] == [a["id"]]
    floating = cd.list_drafts(dossier_id="")
    assert [d["id"] for d in floating] == [b["id"]]


def test_list_versions_newest_first(store):
    created, _ = cd.create_draft(dict(_VALID))
    cd.revise_draft(created["id"], content="v2")
    cd.revise_draft(created["id"], content="v3")
    versions = cd.list_versions(created["id"])
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert cd.get_version(created["id"], 2)["content"] == "v2"


# ── The no-delete pin (SPEC §13.5, model half) ─────────────────────────────

def test_no_delete_exists_in_the_module():
    for attr in dir(cd):
        assert not attr.startswith("delete"), attr
    with open(cd.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "def delete" not in source
    assert ".delete(" not in source


def test_content_cap_matches_the_word_conversion_ceiling():
    from utils.markdown_docx import MAX_MARKDOWN_CHARS

    # The coupling is the guarantee that « Verser en Word » can always
    # convert a stored draft — never weaken one side alone.
    assert cd.CONTENT_MAX_LENGTH == MAX_MARKDOWN_CHARS
