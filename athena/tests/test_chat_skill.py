"""models/chat_skill.py — runtime-managed skills (Phase N §5)."""

import copy
import importlib
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


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name, module):
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

with mock.patch("google.cloud.firestore.Client"):
    import models.chat_skill as cs

from tests.test_chat_draft import (  # noqa: E402 — the shared fake harness
    _FakeDB,
    _FakeFirestore,
)


@pytest.fixture()
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(cs, "db", _FakeDB(data))
    monkeypatch.setattr(cs, "firestore", _FakeFirestore)
    return data


_VALID = {
    "name": "redaction-juridique-quebecoise",
    "description": "Conventions de rédaction du cabinet.",
    "body": "# Style\n\nRédiger en français soutenu.",
}


def _versions(store, skill_id):
    return store.get(f"chat_skills/{skill_id}/versions", {})


def test_create_seeds_version_one(store):
    doc, errors = cs.create_skill(dict(_VALID))
    assert errors == []
    assert doc["current_version"] == 1
    assert doc["active"] is True
    assert _versions(store, doc["id"])["000001"]["body"] == _VALID["body"]


def test_revise_appends_and_keeps_prior_versions(store):
    doc, _ = cs.create_skill(dict(_VALID))
    revised, errors = cs.revise_skill(doc["id"], body="# Style v2")
    assert errors == []
    assert revised["current_version"] == 2
    versions = _versions(store, doc["id"])
    assert versions["000001"]["body"] == _VALID["body"]  # intact
    assert versions["000002"]["body"] == "# Style v2"
    # The turn records head-at-each-turn (FLAG 4): the head moved.
    assert store["chat_skills"][doc["id"]]["body"] == "# Style v2"


def test_deactivation_exists_deletion_does_not(store):
    doc, _ = cs.create_skill(dict(_VALID))
    toggled, errors = cs.set_active(doc["id"], False)
    assert errors == []
    assert store["chat_skills"][doc["id"]]["active"] is False
    for attr in dir(cs):
        assert not attr.startswith("delete"), attr
    with open(cs.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "def delete" not in source
    assert ".delete(" not in source


def test_get_heads_skips_inactive_and_unknown(store):
    active, _ = cs.create_skill(dict(_VALID))
    inactive, _ = cs.create_skill({**_VALID, "name": "autre"})
    cs.set_active(inactive["id"], False)
    heads = cs.get_heads([active["id"], inactive["id"], "fantome"])
    assert [h["id"] for h in heads] == [active["id"]]


def test_validation_requires_name_and_body(store):
    doc, errors = cs.create_skill({"name": " ", "body": ""})
    assert doc is None
    assert len(errors) == 2
