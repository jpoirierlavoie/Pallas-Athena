"""Tests for models/folder.py — get_or_create_folder idempotency (Phase H.2 §8).

The Firestore ``db`` calls are monkeypatched out (via the module's
``list_folders`` / ``create_folder``) so this runs without an emulator.
Importing ``models.folder`` still pulls in the google-cloud libraries, which
are present in the Cloud Build deploy-gate install.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models.folder as folder


def _fakes():
    store: list[dict] = []

    def fake_list_folders(dossier_id, parent_folder_id=None):
        return [
            f for f in store
            if f["dossier_id"] == dossier_id
            and f.get("parent_folder_id") == parent_folder_id
        ]

    def fake_create_folder(dossier_id, name, parent_folder_id=None):
        created = {
            "id": f"f{len(store)}",
            "dossier_id": dossier_id,
            "name": name,
            "parent_folder_id": parent_folder_id,
        }
        store.append(created)
        return created, []

    return store, fake_list_folders, fake_create_folder


def test_get_or_create_folder_creates_then_reuses(monkeypatch):
    store, fake_list, fake_create = _fakes()
    monkeypatch.setattr(folder, "list_folders", fake_list)
    monkeypatch.setattr(folder, "create_folder", fake_create)

    first = folder.get_or_create_folder("d1", "Notes d'honoraires")
    # Second call (different case) must reuse — no duplicate created.
    second = folder.get_or_create_folder("d1", "notes d'honoraires")
    assert first["id"] == second["id"]
    assert len(store) == 1


def test_get_or_create_folder_scoped_per_dossier(monkeypatch):
    store, fake_list, fake_create = _fakes()
    monkeypatch.setattr(folder, "list_folders", fake_list)
    monkeypatch.setattr(folder, "create_folder", fake_create)

    a = folder.get_or_create_folder("d1", "Notes d'honoraires")
    b = folder.get_or_create_folder("d2", "Notes d'honoraires")
    assert a["id"] != b["id"]
    assert len(store) == 2
