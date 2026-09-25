"""Le protocole d'écriture partagé (WP15 ; lot 0a, règle 7) : la RÉSERVATION
de la clé d'idempotence, et le point de validation.

Chaque outil d'écriture MCP passe par run_write. Invariants épinglés :

1. Une idempotency_key rejouée rend le résultat STOCKÉ du premier appel
   (idempotent_replay: true) — jamais une seconde écriture.
2. La même clé avec des arguments DIFFÉRENTS est refusée bruyamment — un
   silence rendrait un résultat qui ne correspond pas à la demande.
3. La clé est RÉSERVÉE atomiquement AVANT l'exécution (``create()``, que le
   serveur refuse quand le document existe) : un second appel de même clé
   pendant que le premier s'exécute est refusé, jamais exécuté. Tant que la
   réservation est plus JEUNE que l'échéance de requête de la plateforme, le
   refus dit « attendez, MÊME clé » ; plus vieille, il dit « interrompu :
   relisez ». Dire « nouvelle clé » au premier cas rouvrirait exactement la
   course que la réservation ferme.
4. Un enregistrement expiré (>24 h) redevient une première écriture — le TTL
   Firestore n'est que du ramassage, l'expiration vit dans le code — et il
   n'est supprimé que TEL QU'IL A ÉTÉ LU (précondition last_update_time).
5. Une écriture VALIDÉE puis un échec : CommittedWriteError, « ENREGISTRÉE —
   NE PAS RÉESSAYER », l'entrée marquée « partial », et une reprise de même
   clé relance ce message sans rien exécuter. Le point de validation est
   STRUCTUREL : c'est le modèle qui l'a noté (provenance.note_commit).
6. Un appel REFUSÉ avant toute validation libère sa réservation : il
   n'enregistre rien. Un échec ordinaire la libère sous la politique
   « optional », la GARDE sous « required ».
7. La finalisation est une mise à jour PARTIELLE : l'empreinte, l'outil,
   l'identifiant de réservation et l'échéance survivent — sinon la reprise
   suivante se lirait comme un conflit.
8. Le magasin échoue OUVERT sous « optional » (la posture d'avant la
   réservation) et FERMÉ sous « required ». Chaque panne est un événement
   typé `mcp_idempotency_store_failure` (outil, opération, classe de
   l'exception) — jamais le texte de l'exception.
9. Une entrée héritée, posée avant la réservation (sans `status`), se rejoue
   inchangée ; un instantané qui n'est pas un dict est illisible.

Tous les tests qui éprouvent la réservation, une précondition ou une
transaction tournent sur le faux Firestore PARTAGÉ (tests/_fake_firestore.py) :
le client est le vrai, seul le serveur est faux, et ce serveur refuse ce que
Firestore refuse (``create()`` sur un document existant, une précondition
périmée). Le faux écrit à la main qui vivait ici acceptait les deux : il ne
prouvait rien de ce que ce lot ajoute. Les seuls MagicMock qui restent
figurent un magasin EN PANNE, jamais un magasin qui fonctionne.

`dry_run` a été RETIRÉ du protocole le 2026-08-27 ; le jeton ne survit que
dans le tuple d'EXCLUSION de l'empreinte, pour qu'un enregistrement posé
avant le retrait se rejoue encore pendant ses 24 h.
"""

import ast
import logging
import os
import pathlib
import re
import sys
import uuid
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
    from mcp import tools
    from mcp import write_support as ws
    from mcp.tools import (
        DEFAULT_REFUSAL_REASON,
        CommittedWriteError,
        ToolArgumentError,
    )
    from models import provenance

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
ATHENA_DIR = pathlib.Path(__file__).resolve().parent.parent
KEY = "cle-de-test-0001"
TASK_ID = "0f8b6c1e-3a2d-4c5b-9e7f-1a2b3c4d5e6f"
DOSSIER_ID = "9d1c2b3a-4e5f-4a6b-8c7d-0e1f2a3b4c5d"


@pytest.fixture()
def fake(monkeypatch):
    """The shared realistic fake, patched in as the idempotency store."""
    return install(monkeypatch, ws)


def _path(tool: str, key: str = KEY) -> str:
    return f"{ws.COLLECTION}/{ws._doc_id(tool, key)}"


def _entries(fake) -> dict:
    return fake.peek_collection(ws.COLLECTION)


def _note(n: int = 1) -> dict:
    return {"created": True, "note": {"id": f"n-{n}"}}


def _args(title: str = "T", key: str = KEY) -> dict:
    return {"idempotency_key": key, "title": title}


def _raising(exc: Exception):
    def execute():
        raise exc
    return execute


def _pending(fake, tool: str, args: dict, *, age: timedelta) -> None:
    """Seed the pending claim a call made *age* ago and never finalized."""
    now = datetime.now(UTC)
    fake.seed(_path(tool, args["idempotency_key"]), {
        "tool": tool,
        "args_fingerprint": ws.args_fingerprint(args),
        "status": "pending",
        "claim_id": str(uuid.uuid4()),
        "claimed_at": now - age,
        "created_at": now - age,
        "expire_at": now - age + ws.IDEMPOTENCY_TTL,
    })


def _fail_ops(fake, kinds: set, *, times: int = -1):
    """Make commits touching mcp_idempotency with an op in *kinds* fail
    server-side (nothing of such a commit applies) — every time, or only the
    first *times*. Returns the hook remover."""
    left = {"n": times}

    def hook(info):
        if left["n"] == 0:
            return
        if any(kind in kinds and path.startswith(ws.COLLECTION + "/")
               for kind, path in info.ops):
            left["n"] -= 1
            raise gexc.ServiceUnavailable("magasin indisponible")

    return fake.add_commit_hook(hook)


def _store_failures(caplog) -> list[dict]:
    return [
        r.json_fields for r in caplog.records
        if r.name == "pallas.mcp"
        and getattr(r, "json_fields", {}).get("event")
        == "mcp_idempotency_store_failure"
    ]


def _all_log_text(caplog) -> str:
    return "\n".join(
        f"{r.getMessage()} {getattr(r, 'json_fields', '')} {r.exc_text or ''}"
        for r in caplog.records
    )


# ══════════════════════════════════════════════════════════════════════
# 1-2. Replay and conflict
# ══════════════════════════════════════════════════════════════════════


def test_the_payload_no_longer_carries_a_dry_run_key(fake):
    """Le retrait du 2026-08-27, épinglé : un appelant qui lirait encore
    cette clé doit trouver son absence, jamais un False trompeur."""
    result = ws.run_write("create_note", _args(), _note)
    assert "dry_run" not in result
    assert result["idempotent_replay"] is False


def test_replay_returns_the_stored_result_without_rewriting(fake):
    calls = []

    def execute():
        calls.append("exécutée")
        return _note()

    first = ws.run_write("create_note", _args(), execute)
    assert first["idempotent_replay"] is False
    assert len(_entries(fake)) == 1

    second = ws.run_write("create_note", _args(), execute)
    assert calls == ["exécutée"]                # une seule exécution réelle
    assert second["idempotent_replay"] is True
    assert second["note"]["id"] == "n-1"


def test_same_key_different_args_is_refused(fake):
    ws.run_write("create_note", _args("A"), _note)
    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note", _args("B"),
                     lambda: pytest.fail("a conflict must not execute"))


def test_protocol_args_do_not_change_the_fingerprint():
    """idempotency_key paramètre le PROTOCOLE, pas l'écriture. `dry_run`
    reste EXCLU de l'empreinte bien qu'aucun appelant ne puisse plus en
    envoyer : un enregistrement posé avant le retrait du 2026-08-27 a été
    empreint sans lui, et il doit se rejouer pendant ses 24 h."""
    a = ws.args_fingerprint({"title": "T", "idempotency_key": "k",
                             "dry_run": True})
    b = ws.args_fingerprint({"title": "T", "idempotency_key": "autre"})
    assert a == b


def test_a_replay_round_trips_through_the_real_client(fake):
    """The stored result survives the real codec (nested maps, booleans,
    None) and comes back as the first call's payload, flagged replayed."""
    payload = {"created": True, "ctag_bumped": True, "dav_synced": False,
               "note": {"id": "n-1", "dossier_id": "d-1", "warning": None}}
    first = ws.run_write("create_note", _args(), lambda: dict(payload))
    replay = ws.run_write("create_note", _args(),
                          lambda: pytest.fail("a replay must not execute"))
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert {k: v for k, v in replay.items() if k != "idempotent_replay"} == payload


def test_a_legacy_entry_without_a_status_replays_unchanged(fake):
    """Written by the code that preceded the claim: no `status`, a `result`.
    It must replay for the rest of its 24 h, exactly as it did."""
    now = datetime.now(UTC)
    fake.seed(_path("create_note"), {
        "tool": "create_note",
        "args_fingerprint": ws.args_fingerprint(_args()),
        "result": {"created": True, "note": {"id": "ancienne"},
                   "idempotent_replay": False},
        "created_at": now, "expire_at": now + ws.IDEMPOTENCY_TTL,
    })
    replay = ws.run_write("create_note", _args(),
                          lambda: pytest.fail("a legacy entry replays"))
    assert replay["idempotent_replay"] is True
    assert replay["note"]["id"] == "ancienne"


# ══════════════════════════════════════════════════════════════════════
# 3. The atomic claim — and the in-flight refusals
# ══════════════════════════════════════════════════════════════════════


def test_the_key_is_claimed_before_execute_runs(fake):
    """During execute the entry already exists, PENDING, with its claim id,
    fingerprint, claim instant and expiry — and no result."""
    seen = {}

    def execute():
        seen.update(fake.peek(_path("create_note")) or {})
        return _note()

    ws.run_write("create_note", _args(), execute)
    assert seen["status"] == "pending"
    assert seen["args_fingerprint"] == ws.args_fingerprint(_args())
    assert seen["tool"] == "create_note"
    assert uuid.UUID(seen["claim_id"])
    assert seen["expire_at"] == seen["claimed_at"] + ws.IDEMPOTENCY_TTL
    assert "result" not in seen


def test_a_concurrent_same_key_call_is_refused_while_the_first_runs(fake):
    """The race the claim closes: a second same-key call arriving while the
    first executes. Before 2026-09-25 both found nothing, both wrote."""
    refused = {}

    def execute():
        with pytest.raises(ToolArgumentError) as excinfo:
            ws.run_write("create_note", _args(),
                         lambda: pytest.fail("the second call must not run"))
        refused["reason"] = excinfo.value.reason
        refused["message"] = str(excinfo.value)
        return _note()

    first = ws.run_write("create_note", _args(), execute)
    assert first["idempotent_replay"] is False
    assert refused["reason"] == "idempotency_in_flight"
    # « Wait, SAME key » — never « new key », which would reopen the race.
    assert "MÊME clé" in refused["message"]
    assert "nouvelle clé" not in refused["message"].lower()
    # …and the same-key retry, once the first call is done, replays it.
    again = ws.run_write("create_note", _args(),
                         lambda: pytest.fail("a finished call replays"))
    assert again["idempotent_replay"] is True


def test_a_young_pending_claim_is_refused_as_in_flight(fake):
    _pending(fake, "create_note", _args(), age=timedelta(minutes=2))
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(),
                     lambda: pytest.fail("an in-flight key must not run"))
    assert excinfo.value.reason == "idempotency_in_flight"
    assert "encore en cours" in str(excinfo.value)
    assert fake.peek(_path("create_note"))["status"] == "pending"


def test_a_stale_pending_claim_is_refused_as_interrupted(fake):
    """Older than the platform's request deadline, the first call cannot be
    running any more: re-reading has become reliable, so — and only now —
    the refusal says « re-read; a NEW key only if nothing was written ».
    Never auto-cleared: that would reopen the duplicate window."""
    _pending(fake, "create_note", _args(),
             age=ws.IN_FLIGHT_WINDOW + timedelta(minutes=1))
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(),
                     lambda: pytest.fail("an interrupted key must not run"))
    assert excinfo.value.reason == "idempotency_interrupted"
    message = str(excinfo.value)
    assert "interrompu" in message and "relisez" in message
    assert "nouvelle clé seulement si rien n'a été écrit" in message
    assert fake.peek(_path("create_note"))["status"] == "pending"


def test_a_pending_claim_with_other_arguments_is_a_conflict(fake):
    _pending(fake, "create_note", _args("A"), age=timedelta(minutes=1))
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args("B"),
                     lambda: pytest.fail("a conflict must not run"))
    assert excinfo.value.reason == "idempotency_conflict"


def test_a_claim_that_appears_between_the_read_and_the_create_wins(fake):
    """The create() precondition, not the read, is what makes the claim
    atomic: a concurrent claim landing right before this call's create is
    refused by the SERVER (AlreadyExists), re-read, and decided on."""
    args = _args()

    def racer(info):
        if ("create", _path("create_note")) in info.ops:
            remove()
            _pending(fake, "create_note", args, age=timedelta(seconds=1))

    remove = fake.add_commit_hook(racer)
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", dict(args),
                     lambda: pytest.fail("the loser of the race must not run"))
    assert excinfo.value.reason == "idempotency_in_flight"


def test_a_claim_lost_on_every_attempt_is_refused_never_run_unclaimed(
    fake, monkeypatch, caplog
):
    """Contention is not a store failure. Every round below is lost to a
    REAL racer on the same key: it claims right before this call's create
    (so the SERVER refuses ours, AlreadyExists), then releases — a refused
    call — before this call re-reads, so the next round starts on an absent
    entry again. Before the review, the attempts ran out and the `optional`
    policy failed OPEN: this call executed UNCLAIMED while a same-key call
    was demonstrably live, which is exactly the duplicate the claim exists
    to prevent. It is now refused « same key », under the default policy."""
    args = _args()
    path = _path("create_note")
    released = {"pending": False}

    def racer_claims(info):
        if ("create", path) in info.ops:
            _pending(fake, "create_note", args, age=timedelta(seconds=1))
            released["pending"] = True

    fake.add_commit_hook(racer_claims)
    server = fake._fake_server
    real_read = server.batch_get_documents

    def racer_releases(request, *a, **k):
        if released["pending"]:
            released["pending"] = False
            fake.external_delete(path)
        return real_read(request, *a, **k)

    monkeypatch.setattr(server, "batch_get_documents", racer_releases)

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        with pytest.raises(ToolArgumentError) as excinfo:
            ws.run_write("create_note", dict(args),
                         lambda: pytest.fail("never run while contended"))
    assert tools.idempotency_policy("create_note") == tools.IDEMPOTENCY_OPTIONAL
    assert excinfo.value.reason == "idempotency_in_flight"
    assert "MÊME clé" in str(excinfo.value)
    assert [(f["op"], f["error_type"]) for f in _store_failures(caplog)] == [
        ("claim", "ClaimContention")]


def test_the_in_flight_window_covers_the_platform_request_deadline():
    """The window is the LARGER of the two bounds app.yaml sets: gunicorn's
    --timeout (60 s — a gthread worker's heartbeat, which does not end a
    request thread) and App Engine standard's automatic-scaling deadline
    (10 minutes). A switch to basic or manual scaling raises the platform
    deadline to 24 h and invalidates the window — so it is pinned here."""
    yaml = (ATHENA_DIR / "app.yaml").read_text(encoding="utf-8")
    assert re.search(r"^automatic_scaling:", yaml, re.MULTILINE)
    assert not re.search(r"^(basic|manual)_scaling:", yaml, re.MULTILINE)
    gunicorn = re.search(r"--timeout (\d+)", yaml)
    assert gunicorn
    assert timedelta(seconds=int(gunicorn.group(1))) <= ws.PLATFORM_REQUEST_DEADLINE
    assert ws.PLATFORM_REQUEST_DEADLINE == timedelta(minutes=10)
    assert ws.IN_FLIGHT_WINDOW > ws.PLATFORM_REQUEST_DEADLINE


# ══════════════════════════════════════════════════════════════════════
# 4. Expiry
# ══════════════════════════════════════════════════════════════════════


def _age_entry(fake, tool: str = "create_note") -> None:
    entry = fake.peek(_path(tool))
    entry["expire_at"] = datetime.now(UTC) - timedelta(minutes=1)
    fake.external_write(_path(tool), entry)


def test_expired_record_reads_as_a_first_write(fake):
    execute_calls = []

    def execute():
        execute_calls.append("exécutée")
        return _note(len(execute_calls))

    ws.run_write("create_note", _args(), execute)
    _age_entry(fake)

    again = ws.run_write("create_note", _args(), execute)
    assert again["idempotent_replay"] is False
    assert len(execute_calls) == 2
    entry = fake.peek(_path("create_note"))
    assert entry["status"] == "committed"
    assert entry["result"]["note"]["id"] == "n-2"
    assert entry["expire_at"] > datetime.now(UTC)


def test_an_expired_entry_is_deleted_only_as_it_was_read(fake):
    """The delete runs under the last_update_time it read. A caller that
    re-claimed the key in between makes it fail; the loop re-reads and finds
    THAT claim — it is never deleted from under its owner."""
    ws.run_write("create_note", _args(), _note)
    _age_entry(fake)
    args = _args()

    def racer(info):
        if ("delete", _path("create_note")) in info.ops:
            remove()
            _pending(fake, "create_note", args, age=timedelta(seconds=1))

    remove = fake.add_commit_hook(racer)
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", dict(args),
                     lambda: pytest.fail("the other call holds the key"))
    assert excinfo.value.reason == "idempotency_in_flight"
    assert fake.peek(_path("create_note"))["status"] == "pending"


# ══════════════════════════════════════════════════════════════════════
# 5. The commit point
# ══════════════════════════════════════════════════════════════════════


def _commit_then(exc: Exception, *, collection="tasks", doc_id=TASK_ID):
    """An execute that COMMITS (the model's own note) and then fails."""
    def execute():
        provenance.note_commit(collection, doc_id)
        raise exc
    return execute


def _unexpected(caplog) -> list:
    return [r for r in caplog.records if r.name == "pallas.unexpected"]


def test_a_failure_after_the_commit_is_reported_committed_never_retryable(
    fake, caplog
):
    """The CTag bump, the cascade re-read, the builder: anything that fails
    AFTER the model noted its commit. It used to surface as a retryable
    « internal error » — and the retry wrote twice."""
    with caplog.at_level(logging.INFO):
        with pytest.raises(CommittedWriteError) as excinfo:
            ws.run_write("create_task",
                         {"idempotency_key": KEY, "dossier_id": DOSSIER_ID},
                         _commit_then(RuntimeError("bump exploded")))
    error = excinfo.value
    assert (error.tool, error.entity_id, error.dossier_id) == (
        "create_task", TASK_ID, DOSSIER_ID)
    assert error.collection == "tasks" and error.rows == 1
    assert error.replay is False
    message = str(error)
    assert "ENREGISTRÉE" in message and "NE PAS RÉESSAYER" in message
    assert "relisez l'élément" in message
    # Only the SAME key is safe: a new key — or none at all — writes again.
    assert "même idempotency_key" in message and "sans clé" in message
    assert TASK_ID in message
    assert isinstance(error.__cause__, RuntimeError)
    # Not a refusal: a -32602 promises nothing was written.
    assert not isinstance(error, ToolArgumentError)

    entry = fake.peek(_path("create_task"))
    assert entry["status"] == "partial"
    assert (entry["entity_id"], entry["dossier_id"], entry["collection"],
            entry["rows"]) == (TASK_ID, DOSSIER_ID, "tasks", 1)
    assert entry["commits"] == [{"collection": "tasks", "id": TASK_ID}]
    assert "result" not in entry
    # The ERROR with its traceback — this is a bug to go and look at.
    (line,) = _unexpected(caplog)
    assert line.levelno == logging.ERROR
    assert line.json_fields["tool"] == "create_task"
    assert line.json_fields["commits"] == 1
    assert line.exc_info or line.exc_text


def test_a_same_key_retry_after_a_partial_re_raises_without_executing(fake):
    args = {"idempotency_key": KEY, "dossier_id": DOSSIER_ID}
    with pytest.raises(CommittedWriteError):
        ws.run_write("create_task", dict(args),
                     _commit_then(RuntimeError("boom")))
    with pytest.raises(CommittedWriteError) as excinfo:
        ws.run_write("create_task", dict(args),
                     lambda: pytest.fail("a partial write must not re-run"))
    assert excinfo.value.replay is True
    assert excinfo.value.entity_id == TASK_ID
    assert excinfo.value.dossier_id == DOSSIER_ID
    assert "NE PAS RÉESSAYER" in str(excinfo.value)


def test_a_refusal_after_the_commit_is_a_handler_bug_reported_committed(
    fake, caplog
):
    """A ToolArgumentError raised AFTER a commit would tell the caller
    « nothing was written » — false. It is reported committed, and logged
    WITHOUT its traceback: a refusal's text describes user content."""
    with caplog.at_level(logging.INFO):
        with pytest.raises(CommittedWriteError):
            ws.run_write("create_task", {"idempotency_key": KEY},
                         _commit_then(ToolArgumentError("titre SECRET")))
    assert fake.peek(_path("create_task"))["status"] == "partial"
    (line,) = _unexpected(caplog)
    assert not line.exc_info and not line.exc_text
    assert line.json_fields["error_type"] == "ToolArgumentError"
    assert "SECRET" not in _all_log_text(caplog)


def test_a_bulk_partial_counts_its_distinct_rows(fake):
    def execute():
        provenance.note_commit("timeentries", TASK_ID)
        provenance.note_commit("timeentries", DOSSIER_ID)
        provenance.note_commit("timeentries", TASK_ID)   # noted twice: one row
        raise RuntimeError("row 3 exploded")

    with pytest.raises(CommittedWriteError) as excinfo:
        ws.run_write("set_time_entry_phase_bulk", {"idempotency_key": KEY},
                     execute)
    assert excinfo.value.rows == 2
    assert "2 éléments écrits" in str(excinfo.value)
    entry = fake.peek(_path("set_time_entry_phase_bulk"))
    assert entry["rows"] == 2 and len(entry["commits"]) == 2


def test_a_dossier_recorder_names_the_committed_dossier(fake):
    with pytest.raises(CommittedWriteError) as excinfo:
        ws.run_write("record_signification", {"idempotency_key": KEY},
                     _commit_then(RuntimeError("x"), collection="dossiers",
                                  doc_id=DOSSIER_ID))
    assert excinfo.value.dossier_id == DOSSIER_ID


def test_a_non_id_dossier_argument_never_reaches_the_record(fake):
    """The call's own `dossier_id` is used only when id-shaped: the error is
    logged and stored, and a title pasted into the argument must not be."""
    with pytest.raises(CommittedWriteError) as excinfo:
        ws.run_write("create_task",
                     {"idempotency_key": KEY, "dossier_id": "Tremblay c. Lavoie"},
                     _commit_then(RuntimeError("x")))
    assert excinfo.value.dossier_id is None
    assert fake.peek(_path("create_task"))["dossier_id"] == ""
    assert "Tremblay" not in str(excinfo.value)


def test_a_post_commit_failure_without_a_key_still_says_committed():
    """No key, no record — but the caller must still be told not to retry.
    (No store is touched: the fake is deliberately not installed.)"""
    with pytest.raises(CommittedWriteError):
        ws.run_write("create_task", {}, _commit_then(RuntimeError("x")))


def test_the_provenance_context_is_reset_after_a_committed_failure(fake):
    with pytest.raises(CommittedWriteError):
        ws.run_write("create_task", {"idempotency_key": KEY},
                     _commit_then(RuntimeError("x")))
    assert provenance.current_via() == "script"
    assert provenance.current_tool() == ""
    assert provenance.committed_writes() == ()


def test_the_real_model_s_own_commit_note_drives_it_end_to_end(monkeypatch):
    """No stub notes the commit here: the REAL create_note handler writes
    through the REAL note model on the shared fake, and it is the model's
    own ``note_commit`` that makes a later failure — the payload builder,
    broken on purpose — come back « ENREGISTRÉE », with the note stored and
    the idempotency entry naming it. Structural, not a handler convention."""
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    from models import note as note_model

    fake = install(monkeypatch, note_model, dav_sync, ws)

    def broken_builder(*_a, **_k):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(handlers, "_write_result", broken_builder)
    args = {"title": "Recherche", "content": "Texte.", "idempotency_key": KEY}
    with pytest.raises(CommittedWriteError) as excinfo:
        handlers.create_note(dict(args))

    (note_id,) = fake.peek_collection("notes")
    assert excinfo.value.entity_id == note_id
    assert excinfo.value.collection == "notes"
    entry = fake.peek(_path("create_note"))
    assert entry["status"] == "partial" and entry["entity_id"] == note_id

    # A same-key retry re-raises from the stored partial: had it re-run the
    # handler, the model would have written a SECOND note before the
    # builder broke again.
    with pytest.raises(CommittedWriteError) as again:
        handlers.create_note(dict(args))
    assert again.value.replay is True
    assert list(fake.peek_collection("notes")) == [note_id]   # one note, ever


def test_a_write_that_commits_and_succeeds_is_simply_committed(fake):
    """A commit followed by success is the ordinary case — nothing partial."""
    def execute():
        provenance.note_commit("tasks", TASK_ID)
        return {"created": True, "entity": {"id": TASK_ID}}

    result = ws.run_write("create_task", {"idempotency_key": KEY}, execute)
    assert result["idempotent_replay"] is False
    assert fake.peek(_path("create_task"))["status"] == "committed"


# ══════════════════════════════════════════════════════════════════════
# 6. Release, keep, finalize
# ══════════════════════════════════════════════════════════════════════


def test_refused_write_records_nothing(fake):
    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note", _args(), _raising(ToolArgumentError("refusé")))
    assert _entries(fake) == {}


def test_a_refused_call_releases_its_claim_so_a_corrected_retry_runs(fake):
    calls = []

    def refuse():
        calls.append("refusée")
        raise ToolArgumentError("titre manquant")

    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note", _args(), refuse)
    assert _entries(fake) == {}

    def accept():
        calls.append("acceptée")
        return _note()

    result = ws.run_write("create_note", _args(), accept)
    assert calls == ["refusée", "acceptée"]
    assert result["idempotent_replay"] is False


def test_a_release_deletes_only_the_claim_this_call_made(fake, caplog):
    """The delete runs under the last_update_time of the claim's own
    create(). Someone who touched the entry meanwhile keeps it: the release
    fails its precondition, is logged, and the entry survives."""
    def execute():
        entry = fake.peek(_path("create_note"))
        fake.external_write(_path("create_note"), {**entry, "claim_id": "autre"})
        raise ToolArgumentError("refusé")

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        with pytest.raises(ToolArgumentError):
            ws.run_write("create_note", _args(), execute)
    assert fake.peek(_path("create_note"))["claim_id"] == "autre"
    assert [(f["op"], f["error_type"]) for f in _store_failures(caplog)] == [
        ("release", "FailedPrecondition")]


def test_an_optional_tool_releases_its_claim_on_a_pre_commit_failure(fake):
    with pytest.raises(RuntimeError):
        ws.run_write("create_note", _args(), _raising(RuntimeError("firestore")))
    assert _entries(fake) == {}
    assert ws.run_write("create_note", _args(), _note)["idempotent_replay"] is False


def test_finalize_is_partial_and_keeps_the_claim_identity(fake):
    """A plain set() would drop the fingerprint — and the next same-key call
    would be refused as a CONFLICT instead of replaying."""
    claims = []

    def execute():
        claims.append(fake.peek(_path("create_note")))
        return _note()

    ws.run_write("create_note", _args(), execute)
    (claim,) = claims
    entry = fake.peek(_path("create_note"))
    assert entry["status"] == "committed"
    for key in ("tool", "args_fingerprint", "claim_id", "claimed_at",
                "created_at", "expire_at"):
        assert entry[key] == claim[key], key
    assert entry["result"]["note"]["id"] == "n-1"
    assert entry["result"]["idempotent_replay"] is False
    assert ws.run_write("create_note", _args(), _note)["idempotent_replay"] is True


def test_a_finalize_failure_leaves_the_claim_pending_and_never_duplicates(
    fake, caplog
):
    """Before the claim, this was THE dangerous half: the write committed,
    nothing was stored, so the same key WROTE AGAIN. Now the claim stays
    pending and the retry is refused as in flight."""
    remove = _fail_ops(fake, {"update"})
    calls = []

    def execute():
        calls.append("écrit")
        return _note(len(calls))

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        first = ws.run_write("create_note", _args(), execute)
    assert first["idempotent_replay"] is False
    assert [(f["op"], f["error_type"]) for f in _store_failures(caplog)] == [
        ("finalize", "ServiceUnavailable")]
    assert fake.peek(_path("create_note"))["status"] == "pending"

    remove()
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(), execute)
    assert excinfo.value.reason == "idempotency_in_flight"
    assert calls == ["écrit"]                      # never a second write
    # The first call is OVER and its result was never stored: the refusal
    # must not promise that waiting will yield it (review, 2026-09-25).
    message = str(excinfo.value)
    assert "sans que son résultat ait pu être enregistré" in message
    assert "si son résultat a été enregistré" in message


# ══════════════════════════════════════════════════════════════════════
# 7. Policy: optional fails open, required fails closed
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def required(monkeypatch):
    """create_note declared `required` for the test — no tool is, in Lot 0."""
    monkeypatch.setitem(tools.TOOLS["create_note"], "idempotency",
                        tools.IDEMPOTENCY_REQUIRED)


def test_the_policy_is_the_registry_declaration():
    for name in tools.WRITE_TOOLS:
        assert tools.idempotency_policy(name) == tools.TOOLS[name]["idempotency"]


def test_an_unrecognised_policy_reads_as_required(monkeypatch):
    monkeypatch.setitem(tools.TOOLS["create_note"], "idempotency", "optionnel")
    assert tools.idempotency_policy("create_note") == tools.IDEMPOTENCY_REQUIRED


def test_a_required_tool_refuses_a_call_without_a_key(fake, required):
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", {"title": "T"},
                     lambda: pytest.fail("a keyless call must not run"))
    assert excinfo.value.reason == "idempotency_required"
    assert _entries(fake) == {}


def test_a_required_tool_fails_closed_when_the_store_is_down(
    monkeypatch, required, caplog
):
    broken = mock.Mock()
    broken.collection.side_effect = RuntimeError("firestore down")
    monkeypatch.setattr(ws, "db", broken)
    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        with pytest.raises(ToolArgumentError) as excinfo:
            ws.run_write("create_note", _args(),
                         lambda: pytest.fail("nothing runs unclaimed"))
    assert excinfo.value.reason == "idempotency_store_unavailable"
    assert "rien n'a été écrit" in str(excinfo.value)
    assert [f["op"] for f in _store_failures(caplog)] == ["lookup"]


def test_a_required_tool_fails_closed_when_the_claim_cannot_be_created(
    fake, required
):
    _fail_ops(fake, {"create"})
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(),
                     lambda: pytest.fail("nothing runs unclaimed"))
    assert excinfo.value.reason == "idempotency_store_unavailable"
    assert _entries(fake) == {}


def test_a_required_tool_keeps_its_claim_on_a_pre_commit_failure(fake, required):
    """Fail-closed: an ordinary exception before any commit leaves the claim
    pending, so the same-key retry is refused and forces a re-read."""
    with pytest.raises(RuntimeError):
        ws.run_write("create_note", _args(), _raising(RuntimeError("timeout")))
    assert fake.peek(_path("create_note"))["status"] == "pending"
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(), _note)
    assert excinfo.value.reason == "idempotency_in_flight"


def test_a_required_tool_still_releases_on_a_refusal(fake, required):
    with pytest.raises(ToolArgumentError):
        ws.run_write("create_note", _args(), _raising(ToolArgumentError("non")))
    assert _entries(fake) == {}


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


def test_an_unclaimed_call_still_stores_its_result_for_a_later_replay(
    fake, caplog
):
    """The claim's create failed (fail-open): the call runs unclaimed, and
    afterwards a best-effort create() of the committed entry still lets a
    later retry replay rather than write again."""
    _fail_ops(fake, {"create"}, times=1)
    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        first = ws.run_write("create_note", _args(), _note)
    assert first["idempotent_replay"] is False
    assert [f["op"] for f in _store_failures(caplog)] == ["claim"]
    assert fake.peek(_path("create_note"))["status"] == "committed"
    replay = ws.run_write("create_note", _args(),
                          lambda: pytest.fail("the stored result replays"))
    assert replay["idempotent_replay"] is True


def test_an_unclaimed_partial_is_still_recorded(fake):
    _fail_ops(fake, {"create"}, times=1)
    with pytest.raises(CommittedWriteError):
        ws.run_write("create_task", {"idempotency_key": KEY},
                     _commit_then(RuntimeError("x")))
    assert fake.peek(_path("create_task"))["status"] == "partial"
    with pytest.raises(CommittedWriteError) as excinfo:
        ws.run_write("create_task", {"idempotency_key": KEY},
                     lambda: pytest.fail("must not re-run"))
    assert excinfo.value.replay is True


# ══════════════════════════════════════════════════════════════════════
# 8-9. Unreadable entries
# ══════════════════════════════════════════════════════════════════════


def _mock_snapshot_db():
    """A store whose snapshot is a MagicMock: truthy `exists`, MagicMock
    `to_dict()` — the settings._read_raw trap."""
    db = mock.MagicMock()
    db.collection.return_value.document.return_value.get.return_value = (
        mock.MagicMock())
    return db


def test_a_non_dict_snapshot_is_unreadable_never_a_replay(monkeypatch, caplog):
    monkeypatch.setattr(ws, "db", _mock_snapshot_db())
    calls = []

    def execute():
        calls.append("x")
        return _note()

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        result = ws.run_write("create_note", _args(), execute)
    assert calls == ["x"]                             # fail-open: it ran
    assert result["idempotent_replay"] is False
    (failure,) = [f for f in _store_failures(caplog) if f["op"] == "lookup"]
    assert failure["error_type"] == "MalformedEntry"


def test_a_non_dict_snapshot_refuses_a_required_tool(monkeypatch, required):
    monkeypatch.setattr(ws, "db", _mock_snapshot_db())
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args(), lambda: pytest.fail("no"))
    assert excinfo.value.reason == "idempotency_store_unavailable"


def test_an_unknown_status_is_unreadable(fake, caplog):
    now = datetime.now(UTC)
    fake.seed(_path("create_note"), {
        "tool": "create_note", "args_fingerprint": ws.args_fingerprint(_args()),
        "status": "étrange", "created_at": now,
        "expire_at": now + ws.IDEMPOTENCY_TTL,
    })
    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        ws.run_write("create_note", _args(), _note)
    ops = [(f["op"], f["error_type"]) for f in _store_failures(caplog)]
    assert ("lookup", "MalformedEntry") in ops


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


def test_a_key_conflict_is_refused_under_its_own_reason(fake):
    ws.run_write("create_note", _args("A"), _note)
    with pytest.raises(ToolArgumentError) as excinfo:
        ws.run_write("create_note", _args("B"), _note)
    assert excinfo.value.reason == "idempotency_conflict"


# ── Store failures: typed events, never text ───────────────────────────


def test_both_store_failures_are_typed_events_naming_the_operation(
    monkeypatch, caplog
):
    """The fail-open posture is kept (the write goes ahead) — and each
    failure is now SEEN, under the registered event, with the tool and the
    operation: the claim's read (`lookup`), then the best-effort store of the
    result of a call that ran unclaimed (`record`). The exception's text
    never reaches the log: it can carry the stored result, which holds
    titles and names."""
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


def test_a_store_that_refuses_every_write_degrades_to_an_uncovered_retry(
    fake, caplog
):
    """Against the REAL client over the shared fake: every write to the
    store fails server-side. The claim cannot be made (fail-open: the call
    runs), nor can the result be stored afterwards — so the same key WILL
    write again. The uncovered window, stated rather than hidden; the two
    log lines are what let anyone find the duplicate afterwards.

    Changed deliberately on 2026-09-25 (was « a record failure alone is
    reported as record »): the claim adds its own `claim` line, and a
    failure of the FINAL record alone no longer duplicates anything — see
    test_a_finalize_failure_leaves_the_claim_pending_and_never_duplicates."""
    remove = _fail_ops(fake, {"create", "update", "delete", "set"})
    calls = []

    def execute():
        calls.append("écrit")
        return _note(len(calls))

    with caplog.at_level(logging.WARNING, logger="pallas.mcp"):
        first = ws.run_write("create_note", _args(), execute)
    assert first["idempotent_replay"] is False
    assert [(f["op"], f["error_type"]) for f in _store_failures(caplog)] == [
        ("claim", "ServiceUnavailable"), ("record", "ServiceUnavailable"),
    ]
    assert _entries(fake) == {}

    remove()
    second = ws.run_write("create_note", _args(), execute)
    assert calls == ["écrit", "écrit"]
    assert second["idempotent_replay"] is False


def test_write_support_logs_through_the_typed_helpers_only():
    """CLAUDE.md item 4: never a raw `logger.*`. The module neither imports
    `logging` nor holds a logger — its only ways to log are the helpers."""
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
    assert {"log_mcp_event", "log_unexpected"} <= names


def test_the_commit_record_is_read_inside_the_writing_block():
    """committed_writes() is emptied when the writing_via block exits, so it
    must be read INSIDE it — pinned from the source, like the block itself
    (test_provenance.test_run_write_wraps_execute_in_writing_via_…)."""
    tree = ast.parse(pathlib.Path(ws.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_write")
    reads = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "committed_writes"]
    assert reads
    blocks = [w for w in ast.walk(fn) if isinstance(w, ast.With) and any(
        isinstance(i.context_expr, ast.Call)
        and getattr(i.context_expr.func, "attr", "") == "writing_via"
        for i in w.items)]
    inside = {id(n) for w in blocks for n in ast.walk(w)}
    assert all(id(r) in inside for r in reads)
