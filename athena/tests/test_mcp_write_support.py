"""Le protocole d'écriture partagé (WP15) : l'idempotence.

Chaque outil d'écriture MCP passe par run_write. Invariants épinglés :

1. Une idempotency_key rejouée rend le résultat STOCKÉ du premier appel
   (idempotent_replay: true) — jamais une seconde écriture.
2. La même clé avec des arguments DIFFÉRENTS est refusée bruyamment — un
   silence rendrait un résultat qui ne correspond pas à la demande.
3. Un enregistrement expiré (>24 h) redevient une première écriture —
   le TTL Firestore n'est que du ramassage, l'expiration vit dans le code.
4. Le magasin échoue OUVERT dans les deux sens : une panne de lecture ne
   bloque pas une première écriture légitime ; une panne d'écriture de
   l'enregistrement ne fait pas échouer une écriture déjà commise.
5. Un appel REFUSÉ (ToolArgumentError) n'enregistre jamais de clé.
6. Le payload ne porte plus de clé dry_run.

`dry_run` a été RETIRÉ du protocole le 2026-08-27 : il n'était pas un
contrôle (rien ne l'exigeait, rien ne le vérifiait, un appelant qui
l'omettait écrivait) et il doublait chaque écriture en deux appels de
modèle. Le test qui l'éprouvait est parti avec lui ; le jeton ne survit
que dans le tuple d'EXCLUSION de l'empreinte, pour qu'un enregistrement
posé avant le retrait se rejoue encore pendant ses 24 h.
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
    from mcp import write_support as ws
    from mcp.tools import ToolArgumentError


class _Store:
    """In-memory stand-in for the mcp_idempotency collection."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def collection(self, name):
        assert name == "mcp_idempotency"
        return self

    def document(self, doc_id):
        outer = self

        class _Doc:
            def get(self):
                data = outer.docs.get(doc_id)
                snap = mock.Mock()
                snap.exists = data is not None
                snap.to_dict = lambda: dict(data) if data else None
                return snap

            def set(self, payload):
                outer.docs[doc_id] = dict(payload)

        return _Doc()


@pytest.fixture()
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(ws, "db", s)
    return s


def test_the_payload_no_longer_carries_a_dry_run_key(store):
    """Le retrait du 2026-08-27, épinglé : un appelant qui lirait encore
    cette clé doit trouver son absence, jamais un False trompeur."""
    result = ws.run_write(
        "create_note", {"idempotency_key": "cle-de-test"},
        lambda: {"created": True, "note": {"id": "n-1"}},
    )
    assert "dry_run" not in result
    assert result["idempotent_replay"] is False


def test_replay_returns_the_stored_result_without_rewriting(store):
    calls = []

    def execute():
        calls.append("exécutée")
        return {"created": True, "note": {"id": "n-1"}}

    args = {"idempotency_key": "cle-stable", "title": "T"}
    first = ws.run_write("create_note", dict(args), execute)
    assert first["idempotent_replay"] is False
    assert len(store.docs) == 1

    second = ws.run_write("create_note", dict(args), execute)
    assert calls == ["exécutée"]                # une seule exécution réelle
    assert second["idempotent_replay"] is True
    assert second["note"]["id"] == "n-1"


def test_same_key_different_args_is_refused(store):
    execute = lambda: {"created": True, "note": {"id": "n-1"}}
    ws.run_write("create_note",
                 {"idempotency_key": "cle-stable", "title": "A"}, execute)
    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note",
                     {"idempotency_key": "cle-stable", "title": "B"}, execute)


def test_protocol_args_do_not_change_the_fingerprint(store):
    """idempotency_key paramètre le PROTOCOLE, pas l'écriture. `dry_run`
    reste EXCLU de l'empreinte bien qu'aucun appelant ne puisse plus en
    envoyer : un enregistrement posé avant le retrait du 2026-08-27 a été
    empreint sans lui, et il doit se rejouer pendant ses 24 h."""
    a = ws.args_fingerprint({"title": "T", "idempotency_key": "k",
                             "dry_run": True})
    b = ws.args_fingerprint({"title": "T", "idempotency_key": "autre"})
    assert a == b


def test_expired_record_reads_as_a_first_write(store):
    execute_calls = []

    def execute():
        execute_calls.append("exécutée")
        return {"created": True, "note": {"id": f"n-{len(execute_calls)}"}}

    args = {"idempotency_key": "cle-perimee", "title": "T"}
    ws.run_write("create_note", dict(args), execute)
    # Vieillir l'enregistrement au-delà du TTL.
    (doc,) = store.docs.values()
    doc["expire_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)

    again = ws.run_write("create_note", dict(args), execute)
    assert again["idempotent_replay"] is False
    assert len(execute_calls) == 2


def test_store_fails_open_both_ways(monkeypatch):
    broken = mock.Mock()
    broken.collection.side_effect = RuntimeError("firestore down")
    monkeypatch.setattr(ws, "db", broken)

    result = ws.run_write(
        "create_note", {"idempotency_key": "cle-quand-meme", "title": "T"},
        lambda: {"created": True, "note": {"id": "n-1"}},
    )
    # L'écriture passe, le replay futur est simplement non couvert.
    assert result["idempotent_replay"] is False
    assert result["note"]["id"] == "n-1"


def test_refused_write_records_nothing(store):
    def execute():
        raise ToolArgumentError("refusé")

    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note",
                     {"idempotency_key": "cle-refusee", "title": "T"}, execute)
    assert store.docs == {}
