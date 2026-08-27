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
    import models.chat_reference_files as ref
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


# ── Reference files (the Claude Code skill model) ────────────────────────

_FILES = [
    {
        "name": "Modèle de mise en demeure",
        "description": "Structure type du cabinet.",
        "content": "# Mise en demeure\n\nPar la présente…",
    },
    {
        "name": "Aide-mémoire délais",
        "description": "",
        "content": "Prescription : vérifier utils/recours.",
    },
]


def _contents(store, skill_id):
    return store.get(f"chat_skills/{skill_id}/fichiers", {})


def _sha(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_create_with_files_writes_manifest_and_content_docs(store):
    doc, errors = cs.create_skill({**_VALID, "files": [dict(f) for f in _FILES]})
    assert errors == []
    manifest = doc["files"]
    assert [m["name"] for m in manifest] == [f["name"] for f in _FILES]
    for spec, entry in zip(_FILES, manifest):
        assert entry["sha256"] == _sha(spec["content"])
        assert entry["chars"] == len(spec["content"])
        stored = _contents(store, doc["id"])[entry["sha256"]]
        assert stored["content"] == spec["content"]
    # The version doc carries the SAME manifest (self-contained registre).
    assert _versions(store, doc["id"])["000001"]["files"] == manifest


def test_manifest_never_embeds_content(store):
    doc, _ = cs.create_skill({**_VALID, "files": [dict(f) for f in _FILES]})
    head = store["chat_skills"][doc["id"]]
    version = _versions(store, doc["id"])["000001"]
    for entry in list(head["files"]) + list(version["files"]):
        assert "content" not in entry  # the 1 MiB/doc guard, structurally


def test_revise_with_files_replaces_manifest_and_keeps_old_content(store):
    doc, _ = cs.create_skill({**_VALID, "files": [dict(_FILES[0])]})
    old_sha = doc["files"][0]["sha256"]
    revised, errors = cs.revise_skill(
        doc["id"], body="v2", files=[dict(_FILES[1])]
    )
    assert errors == []
    assert [m["name"] for m in revised["files"]] == [_FILES[1]["name"]]
    # Append-only: v1's manifest AND the old content doc both survive.
    assert _versions(store, doc["id"])["000001"]["files"][0]["sha256"] == old_sha
    assert old_sha in _contents(store, doc["id"])
    assert _versions(store, doc["id"])["000002"]["files"] == revised["files"]


def test_revise_files_none_keeps_current_manifest(store):
    doc, _ = cs.create_skill({**_VALID, "files": [dict(f) for f in _FILES]})
    revised, errors = cs.revise_skill(doc["id"], body="v2 sans toucher aux fichiers")
    assert errors == []
    assert revised["files"] == doc["files"]
    assert _versions(store, doc["id"])["000002"]["files"] == doc["files"]


def test_duplicate_content_across_files_dedupes_to_one_write():
    # Two names, ONE content → one sha key. Pinned on _content_writes'
    # KEYS because the real Firestore transaction refuses writing the same
    # document twice — a regression here aborts in production while the
    # fake harness (which applies writes immediately) stays green.
    from datetime import datetime, timezone

    entries, errors = ref.validate_files(
        [
            {"name": "a", "description": "", "content": "même contenu"},
            {"name": "b", "description": "", "content": "même contenu"},
        ]
    )
    assert errors == []
    writes = ref.content_writes(entries, datetime.now(timezone.utc))
    assert len(writes) == 1
    assert set(writes) == {_sha("même contenu")}


def test_file_validation_rules():
    big = "x" * (cs.FILE_MAX_CHARS + 1)
    cases = [
        ([{"name": "", "description": "", "content": "c"}], "porter un nom"),
        (
            [
                {"name": "Guide", "description": "", "content": "c"},
                {"name": "guide", "description": "", "content": "d"},
            ],
            "même nom",
        ),
        ([{"name": "Vide", "description": "", "content": "  "}], "est vide"),
        ([{"name": "Gros", "description": "", "content": big}], "dépasse"),
        ("pas-une-liste", "invalide"),
        ([["pas-un-dict"]], "invalide"),
    ]
    for files, fragment in cases:
        _, errors = ref.validate_files(files)
        assert errors and fragment in errors[0], (files, errors)
    too_many = [
        {"name": f"f{i}", "description": "", "content": "c"}
        for i in range(cs.MAX_FILES + 1)
    ]
    _, errors = ref.validate_files(too_many)
    assert any("Au plus" in e for e in errors)


def test_file_content_is_verbatim_except_c0(store):
    raw = "Avant <balise> après\r\nligne 2\tfin\x00\x0b"
    cleaned = "Avant <balise> après\nligne 2\tfin"
    doc, errors = cs.create_skill(
        {**_VALID, "files": [{"name": "Brut", "description": "", "content": raw}]}
    )
    assert errors == []
    entry = doc["files"][0]
    stored = _contents(store, doc["id"])[entry["sha256"]]
    # sanitize() would have eaten « <balise> » — the deviation is the point.
    assert stored["content"] == cleaned
    assert entry["sha256"] == _sha(cleaned)
    assert entry["chars"] == len(cleaned)


def test_get_version_file_success_and_distinct_reasons(store):
    doc, _ = cs.create_skill({**_VALID, "files": [dict(f) for f in _FILES]})
    sid = doc["id"]
    content, reason = cs.get_version_file(sid, 1, "aide-mémoire délais")
    assert reason is None  # case-folded name match
    assert content == _FILES[1]["content"]
    # Unknown version.
    _, reason = cs.get_version_file(sid, 9, _FILES[0]["name"])
    assert reason == "Version de compétence introuvable."
    # Unknown filename → the reason LISTS the available names.
    _, reason = cs.get_version_file(sid, 1, "inexistant")
    assert "Fichiers disponibles" in reason
    assert _FILES[0]["name"] in reason
    # Version without files.
    bare, _ = cs.create_skill(dict(_VALID))
    _, reason = cs.get_version_file(bare["id"], 1, "x")
    assert reason == "Cette compétence n'a aucun fichier de référence."
    # Manifest referencing an absent content doc — storage incoherence.
    sha = doc["files"][0]["sha256"]
    del store[f"chat_skills/{sid}/fichiers"][sha]
    _, reason = cs.get_version_file(sid, 1, _FILES[0]["name"])
    assert reason == "Fichier illisible (incohérence de stockage)."


def test_list_file_contents_fails_open_per_file(store):
    doc, _ = cs.create_skill({**_VALID, "files": [dict(f) for f in _FILES]})
    sha0 = doc["files"][0]["sha256"]
    del store[f"chat_skills/{doc['id']}/fichiers"][sha0]
    rows = cs.list_file_contents(doc["id"], doc["files"])
    assert rows[0]["missing"] is True and rows[0]["content"] == ""
    assert rows[1]["missing"] is False
    assert rows[1]["content"] == _FILES[1]["content"]
