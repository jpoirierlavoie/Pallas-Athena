"""A create whose write RAISED is settled by reading its id back — never
answered « réessayez » on the exception alone (finitions, robustness-1).

A write RPC that fails does not prove the write was not applied: a
DeadlineExceeded or an UNAVAILABLE can follow a server-side commit, the
answer lost. The creators that mint a fresh uuid4 answered « Erreur lors de
la sauvegarde. Veuillez réessayer. », raised as a plain refusal, and
``run_write`` RELEASED the idempotency claim — even under the ``required``
policy of ``create_hearing_series``. The same-key retry the protocol
prescribes then wrote a second record under a new uuid4: a duplicated
billable row the next invoice sweeps, up to 60 duplicated occurrences
synced to the phone and mirrored to Outlook.

Now the model reads the id it minted back (``concurrency.
settle_failed_create``): present → the write landed, the call succeeds as
committed; absent → the plain save error, true; the read failing too →
``WRITE_OUTCOME_UNCERTAIN_ERROR``, which the connector raises with
``keep_claim`` so a same-key retry is refused instead of writing twice.

Real handlers, real ``run_write`` and idempotency store, real models, over
the shared fake Firestore; the lost answer is injected at the transport.
"""

import itertools
import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support  # noqa: F401 — its db is patched below
    from models import concurrency
    from models import expense as expense_model
    from models import time_entry as time_entry_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
_KEYS = itertools.count(1)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    f.seed("dossiers/d1", {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif", "hourly_rate": 30000, "clients": [],
        "client_ids": [], "opposing_parties": [], "opposing_party_ids": [],
        "etag": "e-d1",
    })
    return f


def _land_then(fake, monkeypatch, exc, marker: str) -> None:
    """The next commit touching *marker* APPLIES, then its caller is
    answered *exc* — a commit whose answer was lost."""
    server = fake._fake_server
    real = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(marker in server._write_name(w) for w in writes):
            state["armed"] = False
            raise exc
        return response

    monkeypatch.setattr(server, "commit", _commit)


def _fail_before_applying(fake, monkeypatch, marker: str) -> None:
    server = fake._fake_server
    real = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(marker in server._write_name(w) for w in writes):
            state["armed"] = False
            raise gexc.InternalServerError("nothing applied")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)


def _reads_of(fake, monkeypatch, collection: str) -> None:
    """Every point read of *collection* fails — the read-back included."""
    server = fake._fake_server
    real = server.batch_get_documents

    def _get(request, metadata=None, **kwargs):
        rels = [server.doc_rel(n) for n in request["documents"]]
        if any(r.startswith(f"{collection}/") for r in rels):
            raise gexc.ServiceUnavailable("read-back failed")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)


def _time_entry_args(**over) -> dict:
    return {"dossier_id": "d1", "date": "2026-09-20", "hours": 1.5,
            "description": "Rédaction de la requête",
            "idempotency_key": f"cle-creation-{next(_KEYS):04d}", **over}


def _series_args() -> dict:
    return {"title": "Suivi hebdomadaire", "date": "2026-10-05",
            "start_time": "09:00", "frequency": "hebdomadaire", "count": 3,
            "dossier_id": "d1", "idempotency_key": f"cle-serie-{next(_KEYS):04d}"}


def _call(tool: str, args: dict) -> dict:
    assert tools.validate_args(tools.TOOLS[tool]["input_schema"], args) == []
    return getattr(handlers, tool)(dict(args))


def _refused(tool: str, args: dict) -> tools.ToolArgumentError:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        _call(tool, args)
    return excinfo.value


# ── a write that LANDED, its answer lost ─────────────────────────────────


def test_a_time_entry_whose_answer_was_lost_is_the_committed_one(fake, monkeypatch):
    _land_then(fake, monkeypatch, gexc.InternalServerError("answer lost"),
               "/timeentries/")
    args = _time_entry_args()
    payload = _call("create_time_entry", args)
    rows = fake.peek_collection("timeentries")
    assert len(rows) == 1
    assert payload["entity"]["id"] in rows
    # The same-key retry replays the stored result — never a second row.
    again = _call("create_time_entry", args)
    assert again["idempotent_replay"] is True
    assert len(fake.peek_collection("timeentries")) == 1


def test_a_series_whose_answer_was_lost_is_the_committed_one(fake, monkeypatch):
    """The required-policy one: every occurrence synced and mirrored."""
    _land_then(fake, monkeypatch, gexc.InternalServerError("answer lost"),
               "/hearings/")
    args = _series_args()
    payload = _call("create_hearing_series", args)
    stored = fake.peek_collection("hearings")
    assert len(stored) == 3
    assert payload["occurrences_count"] == 3
    assert {o["id"] for o in payload["occurrences"]} == set(stored)
    again = _call("create_hearing_series", args)
    assert again["idempotent_replay"] is True
    assert len(fake.peek_collection("hearings")) == 3


# ── the read-back failing too: uncertain, the claim KEPT ─────────────────


@pytest.mark.parametrize("tool, args_fn, collection", [
    ("create_time_entry", _time_entry_args, "timeentries"),
    ("create_hearing_series", _series_args, "hearings"),
])
def test_an_unconfirmable_create_keeps_the_claim_and_never_writes_twice(
        fake, monkeypatch, tool, args_fn, collection):
    _land_then(fake, monkeypatch, gexc.InternalServerError("answer lost"),
               f"/{collection}/")
    _reads_of(fake, monkeypatch, collection)
    args = args_fn()
    first = _refused(tool, args)
    assert first.reason == "write_outcome_uncertain"
    assert first.keep_claim is True
    assert "Rien n'a été créé" not in str(first)
    count = len(fake.peek_collection(collection))
    assert count >= 1   # it DID land
    # The same key is refused (in flight), never run again.
    again = _refused(tool, args)
    assert again.reason in ("idempotency_in_flight", "idempotency_interrupted")
    assert len(fake.peek_collection(collection)) == count


# ── a write that did NOT land ────────────────────────────────────────────


def test_a_create_that_did_not_land_is_the_plain_save_error(fake, monkeypatch):
    _fail_before_applying(fake, monkeypatch, "/timeentries/")
    args = _time_entry_args()
    first = _refused("create_time_entry", args)
    assert "Erreur lors de la sauvegarde" in str(first)
    assert first.reason != "write_outcome_uncertain"
    assert fake.peek_collection("timeentries") == {}
    # The claim was released: the same key now writes ONE row.
    _call("create_time_entry", args)
    assert len(fake.peek_collection("timeentries")) == 1


# ── the model's own contract ─────────────────────────────────────────────


@pytest.mark.parametrize("create, collection", [
    (lambda: time_entry_model.create_time_entry({
        "dossier_id": "d1", "date": datetime(2026, 9, 20, tzinfo=UTC),
        "description": "x", "hours": 1.0, "rate": 30000}), "timeentries"),
    (lambda: expense_model.create_expense({
        "dossier_id": "d1", "date": datetime(2026, 9, 20, tzinfo=UTC),
        "description": "x", "amount": 1000, "category": "autre"}), "expenses"),
])
def test_the_models_settle_by_reading_the_fresh_id_back(
        fake, monkeypatch, create, collection):
    _land_then(fake, monkeypatch, gexc.InternalServerError("lost"), f"/{collection}/")
    doc, errors = create()
    assert errors == [] and doc["id"] in fake.peek_collection(collection)


def test_settle_failed_create_never_raises():
    class _Ref:
        def get(self):
            raise RuntimeError("down")
    assert concurrency.settle_failed_create(_Ref()) == (concurrency.WRITE_UNKNOWN, None)


@pytest.mark.parametrize("name, collection", [
    ("task", "tasks"), ("note", "notes"), ("partie", "parties"),
    ("hearing", "hearings"),
])
def test_every_fresh_id_creator_settles_by_reading_back(
        fake, monkeypatch, name, collection):
    from models import hearing, note, partie, task
    create = {
        "task": lambda: task.create_task({"title": "T", "dossier_id": "d1"}),
        "note": lambda: note.create_note({"title": "N", "content": "C",
                                          "dossier_id": "d1",
                                          "category": "recherche"}),
        "partie": lambda: partie.create_partie({
            "type": "individual", "contact_role": "client",
            "first_name": "Jean", "last_name": "Tremblay"}),
        "hearing": lambda: hearing.create_hearing({
            "title": "H", "hearing_type": "rencontre",
            "start_datetime": datetime(2026, 10, 5, 13, tzinfo=UTC)}),
    }[name]
    _land_then(fake, monkeypatch, gexc.InternalServerError("lost"), f"/{collection}/")
    doc, errors = create()
    assert errors == [], errors
    assert list(fake.peek_collection(collection)) == [doc["id"]]
    # And unconfirmable → the uncertain answer, never « réessayez ».
    _land_then(fake, monkeypatch, gexc.InternalServerError("lost"), f"/{collection}/")
    _reads_of(fake, monkeypatch, collection)
    doc2, errors2 = create()
    assert doc2 is None and errors2 == [concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR]


def test_a_protocol_step_whose_transaction_answer_was_lost_is_the_committed_one(
        fake, monkeypatch):
    from models import protocol
    fake.seed("protocols/p1", {
        "id": "p1", "dossier_id": "d1", "status": "actif",
        "protocol_type": "conventionnel", "title": "Protocole",
        "start_date": datetime(2026, 9, 1, tzinfo=UTC), "etag": "e-p1",
    })
    _land_then(fake, monkeypatch, gexc.InternalServerError("lost"), "/steps/")
    step, errors = protocol.add_step("p1", {
        "title": "Interrogatoire", "deadline_date": datetime(2026, 11, 2, tzinfo=UTC)})
    assert errors == [], errors
    assert list(fake.peek_collection("protocols/p1/steps")) == [step["id"]]
