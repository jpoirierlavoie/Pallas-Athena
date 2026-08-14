"""Tests for models/folder.py — get_or_create_folder idempotency (Phase H.2 §8).

The Firestore ``db`` calls are monkeypatched out (via the module's
``list_folders`` / ``create_folder``) so this runs without an emulator.
Importing ``models.folder`` still pulls in the google-cloud libraries, which
are present in the Cloud Build deploy-gate install.
"""

import os
import sys

import pytest

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
    assert index["f2"] == {"direct": 2, "documents": 2, "folders": 0}


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
