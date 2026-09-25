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
7. Échouer ouvert n'est tenable que si l'échec se VOIT : chaque panne du
   magasin est un événement typé `mcp_idempotency_store_failure` (outil,
   opération, classe de l'exception) — jamais le texte de l'exception, et
   plus jamais un `logger.warning` brut hors du registre.
8. Un refus porte un code de motif stable (`reason`), celui sous lequel
   le point d'entrée le journalise ; le conflit de clé a le sien.

`dry_run` a été RETIRÉ du protocole le 2026-08-27 : il n'était pas un
contrôle (rien ne l'exigeait, rien ne le vérifiait, un appelant qui
l'omettait écrivait) et il doublait chaque écriture en deux appels de
modèle. Le test qui l'éprouvait est parti avec lui ; le jeton ne survit
que dans le tuple d'EXCLUSION de l'empreinte, pour qu'un enregistrement
posé avant le retrait se rejoue encore pendant ses 24 h.
"""

import ast
import logging
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from google.api_core import exceptions as gexc  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    from mcp import write_support as ws
    from mcp.tools import DEFAULT_REFUSAL_REASON, ToolArgumentError

from tests._fake_firestore import install  # noqa: E402


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


# ── Refusal reasons ────────────────────────────────────────────────────


def test_a_refusal_defaults_to_the_generic_reason_and_keeps_its_message():
    """Every existing raise passes one message and nothing else, so the
    reason is keyword-only with a default: 125 call sites stay unchanged
    and log as `argument_refused`."""
    exc = ToolArgumentError("Le dossier est introuvable.")
    assert str(exc) == "Le dossier est introuvable."
    assert exc.reason == DEFAULT_REFUSAL_REASON == "argument_refused"
    named = ToolArgumentError("Refusé.", reason="idempotency_conflict")
    assert str(named) == "Refusé." and named.reason == "idempotency_conflict"
    with pytest.raises(TypeError):
        ToolArgumentError("Refusé.", "idempotency_conflict")  # keyword-only


def test_a_key_conflict_is_refused_under_its_own_reason(store):
    execute = lambda: {"created": True, "note": {"id": "n-1"}}
    ws.run_write("create_note",
                 {"idempotency_key": "cle-stable", "title": "A"}, execute)
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note",
                     {"idempotency_key": "cle-stable", "title": "B"}, execute)
    assert excinfo.value.reason == "idempotency_conflict"


# ── Store failures: typed events, never text ───────────────────────────


def _store_failures(caplog) -> list[dict]:
    return [
        r.json_fields for r in caplog.records
        if r.name == "pallas.mcp"
        and getattr(r, "json_fields", {}).get("event")
        == "mcp_idempotency_store_failure"
    ]


def _all_log_text(caplog) -> str:
    return "\n".join(
        f"{r.getMessage()} {getattr(r, 'json_fields', '')}"
        for r in caplog.records
    )


def test_both_store_failures_are_typed_events_naming_the_operation(
    monkeypatch, caplog
):
    """The fail-open posture is kept (the write goes ahead) — and each
    failure is now SEEN, under the registered event, with the tool and the
    operation. The exception's text never reaches the log: it can carry the
    stored result, which holds titles and names."""
    broken = mock.Mock()
    broken.collection.side_effect = RuntimeError("résultat SECRET du client")
    monkeypatch.setattr(ws, "db", broken)

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        result = ws.run_write(
            "create_note", {"idempotency_key": "cle-quand-meme", "title": "T"},
            lambda: {"created": True, "note": {"id": "n-1"}},
        )

    assert result["note"]["id"] == "n-1"            # still fail-OPEN
    failures = _store_failures(caplog)
    assert [f["op"] for f in failures] == ["lookup", "record"]
    for fields in failures:
        assert fields["tool"] == "create_note"
        assert fields["outcome"] == "failure"
        assert fields["error_type"] == "RuntimeError"
    assert all(
        r.levelno == logging.WARNING for r in caplog.records
        if r.name == "pallas.mcp"
    )
    assert "SECRET" not in _all_log_text(caplog)


def test_a_record_failure_alone_is_reported_as_record_and_leaves_no_entry(
    monkeypatch, caplog
):
    """Against the REAL client over the shared fake: the lookup succeeds,
    the record's commit fails server-side. That is the dangerous half — the
    write committed, nothing was stored, so the same key WILL write again —
    and the log line is what lets anyone find the duplicate afterwards."""
    fake = install(monkeypatch, ws)

    def _unavailable(_info):
        raise gexc.ServiceUnavailable("magasin indisponible")

    remove_hook = fake.add_commit_hook(_unavailable)
    calls = []

    def execute():
        calls.append("écrit")
        return {"created": True, "note": {"id": f"n-{len(calls)}"}}

    args = {"idempotency_key": "cle-perdue", "title": "T"}
    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        first = ws.run_write("create_note", dict(args), execute)
    assert first["idempotent_replay"] is False
    failures = _store_failures(caplog)
    assert [(f["op"], f["error_type"]) for f in failures] == [
        ("record", "ServiceUnavailable")
    ]
    assert fake.peek_collection(ws.COLLECTION) == {}

    remove_hook()
    second = ws.run_write("create_note", dict(args), execute)
    # The uncovered window, stated rather than hidden: nothing was stored,
    # so the retry is a second write.
    assert calls == ["écrit", "écrit"]
    assert second["idempotent_replay"] is False


def test_a_replay_round_trips_through_the_real_client(monkeypatch):
    """The stored result survives the real codec (nested maps, booleans,
    None) and comes back as the first call's payload, flagged replayed."""
    install(monkeypatch, ws)
    payload = {"created": True, "ctag_bumped": True, "dav_synced": False,
               "note": {"id": "n-1", "dossier_id": "d-1", "warning": None}}
    args = {"idempotency_key": "cle-codec", "title": "T"}
    first = ws.run_write("create_note", dict(args), lambda: dict(payload))
    replay = ws.run_write(
        "create_note", dict(args),
        lambda: pytest.fail("a replay must not execute"),
    )
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert {k: v for k, v in replay.items() if k != "idempotent_replay"} == payload


def test_write_support_logs_through_the_typed_helper_only():
    """CLAUDE.md item 4: never a raw `logger.*`. The module neither imports
    `logging` nor holds a logger — its only way to log is the helper."""
    source = pathlib.Path(ws.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "logging" not in imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "logger" not in names
    assert "log_mcp_event" in names
