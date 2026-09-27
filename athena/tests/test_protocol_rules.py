"""Les règles du protocole qui vivent dans le MODÈLE (lot 1a, L2).

Le web, la cascade depuis les tâches et — au lot 1b — le connecteur passent
tous par ``models/protocol.py`` ; chaque règle ci-dessous y est donc posée
une fois, et épinglée ici sur le faux Firestore partagé (le client, ses
transactions et la boucle de reprise de ``transactional`` sont les vrais ;
une écriture rivale est posée par un crochet de commit, une panne de lecture
en remplaçant l'appel du serveur). On relit ce qui est STOCKÉ.

1. **Les étapes portent un etag** (règle 7), régénéré par chaque écriture —
   sauf le tampon dérivé « en_retard » d'une simple consultation.
2. **Un seul protocole actif, vérifié sans jamais échouer ouvert** : la
   lecture se fait DANS la transaction qui écrit ; une erreur de lecture
   refuse ; un protocole activé entre la lecture et l'écriture l'annule.
3. **La fermeture dit qui l'a faite** (``closed_by``/``closed_at``) :
   « auto » quand la dernière étape la déclenche, et c'est la seule que la
   cascade défait d'elle-même.
4. **Le bouton d'étape respecte le protocole** : rouvrir une étape d'un
   protocole fermé automatiquement le réactive — si aucun autre n'est
   actif, vérifié AVANT toute écriture ; tout autre protocole fermé ou
   suspendu refuse, sans rien écrire.
5. **Retrouver l'étape d'une tâche** dans le dossier d'abord, puis parmi
   les protocoles actifs du cabinet — ce que la cascade atteint vraiment.
6. **Les tâches liées se créent étape par étape** : une panne n'arrête plus
   les suivantes.
"""

import logging
import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    from models import concurrency
    from models import dossier as dossier_model  # noqa: F401 — read by the regime gate
    from models import protocol as protocol_model
    from models import provenance
    from models import task as task_model  # noqa: F401

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
P = "p1"
CTAG = "dav_sync/dossier:d1"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "T c. L", "status": "actif",
                              "tribunal": ""})
    fake.seed(CTAG, {"ctag": "c0", "sync_token": "c0", "updated_at": WHEN})
    return fake


def _protocol(fake, pid: str = P, *, status: str = "actif",
              closed_by: str | None = None, dossier_id: str = "d1",
              ptype: str = "conventionnel") -> None:
    doc = {
        "id": pid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": "Protocole de l'instance",
        "protocol_type": ptype, "status": status,
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": f"pe-{pid}", "created_at": WHEN, "updated_at": WHEN,
    }
    if closed_by is not None:
        doc["closed_by"] = closed_by
        doc["closed_at"] = WHEN
    fake.seed(f"protocols/{pid}", doc)


def _step(fake, sid: str, *, pid: str = P, status: str = "à_venir",
          task=None, order: int = 1, etag: str | None = None) -> None:
    doc = {
        "id": sid, "order": order, "title": f"Étape {sid}", "description": "",
        "cpc_reference": "", "deadline_date": LATER,
        "deadline_offset_days": None, "mandatory": False,
        "deadline_locked": False, "status": status,
        "completed_date": WHEN if status == "complété" else None,
        "linked_task_id": task, "linked_hearing_id": None, "notes": "",
        "date_confirmed": True, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN,
    }
    if etag is not None:
        doc["etag"] = etag
    fake.seed(f"protocols/{pid}/steps/{sid}", doc)


def _task(fake, tid: str, *, status: str = "à_faire",
          dossier_id: str = "d1") -> None:
    fake.seed(f"tasks/{tid}", {
        "id": tid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": f"Tâche {tid}", "description": "",
        "priority": "normale", "status": status, "due_date": None,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    })


def _step_doc(fake, sid: str, pid: str = P) -> dict:
    return fake.peek(f"protocols/{pid}/steps/{sid}")


def _snapshot(fake) -> dict:
    return {rel: fake.peek(rel) for rel in list(fake._fake_server.docs)}


def _break_queries(monkeypatch, fake, collection_id: str = "protocols"):
    """Every query over *collection_id* fails at the store — the
    transient read error the old one-actif helper swallowed into « none »."""
    server = fake._fake_server
    real = server.run_query

    def _run_query(request, *a, **kw):
        sq = request.get("structured_query") if hasattr(request, "get") else None
        froms = [f.collection_id for f in getattr(sq, "from_", [])] if sq else []
        if collection_id in froms:
            raise RuntimeError("store unavailable")
        return real(request, *a, **kw)

    monkeypatch.setattr(server, "run_query", _run_query)


# ══════════════════════════════════════════════════════════════════════
# 1. Les étapes portent un etag
# ══════════════════════════════════════════════════════════════════════


def test_template_steps_are_created_stamped(fake):
    proto, errors = protocol_model.create_protocol(
        "d1", "cq_simplifié", WHEN, {"title": "Protocole"})
    assert errors == []
    steps = fake.peek_collection(f"protocols/{proto['id']}/steps")
    assert len(steps) == 7
    etags = {s["etag"] for s in steps.values()}
    assert "" not in etags and len(etags) == 7
    for step in steps.values():
        assert step["created_via"] == step["updated_via"] == "script"
    stored = fake.peek(f"protocols/{proto['id']}")
    assert stored["status"] == "actif"
    assert stored["closed_by"] == "" and stored["closed_at"] is None


def test_a_status_change_regenerates_the_step_etag(fake):
    _protocol(fake)
    _step(fake, "s1", etag="se0")
    _step(fake, "s2", order=2)
    with provenance.writing_via("mcp", tool="t"):
        protocol_model.set_step_status(P, "s1", "complété")
    stored = _step_doc(fake, "s1")
    assert stored["etag"] not in ("", "se0")
    assert stored["updated_via"] == "mcp"
    assert stored["mcp_updated_at"] is not None


# ══════════════════════════════════════════════════════════════════════
# 2. Un seul protocole actif — sans jamais échouer ouvert
# ══════════════════════════════════════════════════════════════════════


def test_a_second_active_protocol_is_refused_and_nothing_written(fake):
    _protocol(fake, "existing")
    before = _snapshot(fake)
    proto, errors = protocol_model.create_protocol(
        "d1", "conventionnel", WHEN, {})
    assert proto is None
    assert errors == [protocol_model.ONE_ACTIVE_ERROR]
    assert _snapshot(fake) == before


def test_an_unreadable_active_check_refuses_instead_of_creating(
    fake, monkeypatch, caplog
):
    """The old helper answered ``[]`` on ANY exception: a Firestore blip
    read as « no active protocol » and a second one was minted."""
    _break_queries(monkeypatch, fake)
    before = _snapshot(fake)
    with caplog.at_level(logging.INFO):
        proto, errors = protocol_model.create_protocol(
            "d1", "conventionnel", WHEN, {})
    assert proto is None
    assert errors == [protocol_model.ACTIVE_CHECK_FAILED]
    assert _snapshot(fake) == before
    refused = [r.json_fields for r in caplog.records
               if r.name == "pallas.protocol"]
    assert refused[-1]["event"] == "protocol_refused"
    assert refused[-1]["reason"] == "lecture_impossible"


def test_a_protocol_activated_during_the_creation_aborts_it(fake):
    """The check reads INSIDE the transaction that writes: a protocol
    activated in between changes the query's result set, the commit aborts,
    and the retry sees it — never two active protocols."""
    fired = []

    def _rival(info):
        if not fired and any(p.startswith("protocols/") for _, p in info.ops):
            fired.append(True)
            fake.external_write("protocols/rival", {
                "id": "rival", "dossier_id": "d1", "status": "actif"})

    fake.add_commit_hook(_rival)
    proto, errors = protocol_model.create_protocol(
        "d1", "conventionnel", WHEN, {})
    assert fired
    assert proto is None and errors == [protocol_model.ONE_ACTIVE_ERROR]
    assert list(fake.peek_collection("protocols")) == ["rival"]


def test_an_unknown_creation_field_is_refused_never_dropped(fake):
    proto, errors = protocol_model.create_protocol(
        "d1", "conventionnel", WHEN, {"status": "suspendu"})
    assert proto is None
    assert errors == ["Le champ « status » d'un protocole ne se modifie pas "
                      "ainsi."]
    assert fake.peek_collection("protocols") == {}


def test_the_fail_open_active_reader_is_gone():
    """A reader that answers « none » on a read error must not survive for
    a future caller to pick up."""
    assert not hasattr(protocol_model, "_get_active_protocols")


# ══════════════════════════════════════════════════════════════════════
# 3. La fermeture dit qui l'a faite
# ══════════════════════════════════════════════════════════════════════


def test_the_last_step_closes_the_protocol_as_auto(fake):
    _protocol(fake)
    _step(fake, "s1")
    _step(fake, "s2", status="complété", order=2)
    _, errors, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert errors == [] and outcome["protocol_closed"] is True
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "complété"
    assert stored["closed_by"] == protocol_model.CLOSED_BY_AUTO
    assert stored["closed_at"] is not None


def test_a_step_reopened_before_the_closure_commits_keeps_it_open(fake):
    """The completion check decides on the steps AS THEY STAND AT COMMIT:
    a step reopened between its read and its write aborts it."""
    _protocol(fake)
    _step(fake, "s1", status="complété")
    fired = []

    def _rival(info):
        if not fired and ("update", f"protocols/{P}") in info.ops:
            fired.append(True)
            doc = _step_doc(fake, "s1")
            doc.update(status="à_venir", completed_date=None)
            fake.external_write(f"protocols/{P}/steps/s1", doc)

    fake.add_commit_hook(_rival)
    assert protocol_model._check_protocol_completion(P) is False
    assert fired
    assert fake.peek(f"protocols/{P}")["status"] == "actif"


# ══════════════════════════════════════════════════════════════════════
# 4. Le bouton d'étape respecte le protocole
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("status, closed_by, target", [
    ("suspendu", None, "complété"),
    ("suspendu", None, "à_venir"),
    ("complété", None, "à_venir"),       # closed before closed_by existed
    ("complété", "", "à_venir"),
    ("complété", "web", "à_venir"),      # the lawyer closed it on purpose
    ("complété", "mcp", "à_venir"),
])
def test_a_closed_or_suspended_protocol_refuses_and_writes_nothing(
    fake, status, closed_by, target
):
    _protocol(fake, status=status, closed_by=closed_by)
    step_status = "complété" if target == "à_venir" else "à_venir"
    _step(fake, "s1", status=step_status, task="t1")
    _task(fake, "t1", status="terminée" if step_status == "complété"
          else "à_faire")
    before = _snapshot(fake)
    _step_out, errors, outcome = protocol_model.set_step_status(P, "s1", target)
    assert len(errors) == 1 and "réactivez-le" in errors[0]
    assert outcome["changed"] is False
    assert _snapshot(fake) == before


def test_reopening_a_step_of_an_auto_closed_protocol_reactivates_it(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _step(fake, "s2", status="complété", order=2)
    _task(fake, "t1", status="terminée")
    _, errors, outcome = protocol_model.set_step_status(P, "s1", "à_venir")
    assert errors == []
    assert outcome["protocol_reopened"] is True
    assert outcome["task_sync"] == "synced"
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "actif"
    assert stored["closed_by"] == "" and stored["closed_at"] is None
    assert _step_doc(fake, "s1")["status"] == "à_venir"
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert fake.peek(CTAG)["ctag"] != "c0"


def test_a_reopen_blocked_by_another_active_protocol_writes_nothing(fake):
    """Refused BEFORE any write: the step never lands open inside a closed
    protocol, and its task is not reopened either."""
    _protocol(fake, status="complété", closed_by="auto")
    _protocol(fake, "p2")                       # the other actif protocol
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    before = _snapshot(fake)
    _step_out, errors, outcome = protocol_model.set_step_status(
        P, "s1", "à_venir")
    assert errors == [protocol_model.OTHER_ACTIVE_ON_REOPEN]
    assert outcome["changed"] is False and outcome["protocol_reopened"] is False
    assert _snapshot(fake) == before


def test_a_reopen_whose_active_check_fails_is_refused(fake, monkeypatch):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété")
    _break_queries(monkeypatch, fake)
    before = _snapshot(fake)
    _step_out, errors, _outcome = protocol_model.set_step_status(
        P, "s1", "à_venir")
    assert errors == [protocol_model.ACTIVE_CHECK_FAILED]
    assert _snapshot(fake) == before


def test_a_stale_expected_etag_is_refused_a_fresh_one_writes(fake):
    _protocol(fake)
    _step(fake, "s1", etag="se0")
    _step(fake, "s2", order=2)
    _s, errors, _o = protocol_model.set_step_status(
        P, "s1", "complété", expected_etag="perimee")
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert _step_doc(fake, "s1")["status"] == "à_venir"
    _s, errors, _o = protocol_model.set_step_status(
        P, "s1", "complété", expected_etag="se0")
    assert errors == []
    assert _step_doc(fake, "s1")["status"] == "complété"


def test_the_same_state_is_a_no_op_whatever_the_etag_says(fake):
    """Asking for what already is needs no current version: the non-toggle
    answer to a stale page is « already done », never a conflict."""
    _protocol(fake, status="suspendu")
    _step(fake, "s1", status="complété", etag="se0")
    before = _snapshot(fake)
    step, errors, outcome = protocol_model.set_step_status(
        P, "s1", "complété", expected_etag="perimee")
    assert errors == [] and step["status"] == "complété"
    assert outcome["changed"] is False
    assert _snapshot(fake) == before


# ══════════════════════════════════════════════════════════════════════
# 5. Retrouver l'étape d'une tâche
# ══════════════════════════════════════════════════════════════════════


def test_the_step_is_found_in_a_closed_protocol_of_the_dossier(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    proto, step = protocol_model.find_step_for_task("t1", "d1")
    assert proto["id"] == P and step["id"] == "s1"


def test_the_actif_protocol_of_the_dossier_is_searched_first(fake):
    _protocol(fake, "old", status="complété", closed_by="web")
    _step(fake, "s-old", pid="old", task="t1")
    _protocol(fake, "new")
    _step(fake, "s-new", pid="new", task="t1")
    proto, step = protocol_model.find_step_for_task("t1", "d1")
    assert (proto["id"], step["id"]) == ("new", "s-new")


def test_a_task_moved_away_is_found_through_the_firm_wide_fallback(fake):
    """The step stayed in the old dossier's actif protocol — the one the
    cascade has always reached by scanning every actif protocol."""
    _protocol(fake, "p-elsewhere", dossier_id="d9")
    _step(fake, "s9", pid="p-elsewhere", task="t1")
    proto, step = protocol_model.find_step_for_task("t1", "d1")
    assert (proto["id"], step["id"]) == ("p-elsewhere", "s9")
    assert protocol_model.find_step_for_task("t1", None)[1]["id"] == "s9"


def test_no_linked_step_is_none_and_a_read_error_propagates(fake, monkeypatch):
    _protocol(fake)
    _step(fake, "s1", task="t-other")
    assert protocol_model.find_step_for_task("t1", "d1") is None
    assert protocol_model.find_step_for_task("", "d1") is None
    _break_queries(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        protocol_model.find_step_for_task("t1", "d1")


# ══════════════════════════════════════════════════════════════════════
# 6. Les tâches liées se créent étape par étape
# ══════════════════════════════════════════════════════════════════════


def test_one_failing_task_no_longer_stops_the_others(fake, monkeypatch, caplog):
    proto, errors = protocol_model.create_protocol(
        "d1", "cq_simplifié", WHEN, {"title": "Protocole"})
    assert errors == []
    steps = sorted(fake.peek_collection(f"protocols/{proto['id']}/steps")
                   .values(), key=lambda s: s["order"])
    real = task_model.create_task
    calls = []

    def _flaky(data, **kw):
        calls.append(data["title"])
        if len(calls) == 1:
            raise RuntimeError("store unavailable")
        return real(data, **kw)

    monkeypatch.setattr(task_model, "create_task", _flaky)
    with caplog.at_level(logging.INFO):
        report = protocol_model.create_linked_tasks(proto["id"], proto, steps)
    assert report == {"created": 6, "linked": 6, "failed": 1}
    stored = sorted(fake.peek_collection(f"protocols/{proto['id']}/steps")
                    .values(), key=lambda s: s["order"])
    assert stored[0]["linked_task_id"] is None
    for before, after in zip(steps[1:], stored[1:]):
        assert after["linked_task_id"] in fake.peek_collection("tasks")
        assert after["etag"] != before["etag"]   # linking is a step write
    assert fake.peek(CTAG)["ctag"] != "c0"
    assert [r.getMessage() for r in caplog.records
            if r.name == "pallas.unexpected"] == [
        "protocol linked task creation failed"]
    event = [r.json_fields for r in caplog.records
             if r.name == "pallas.protocol"][-1]
    assert event["event"] == "linked_tasks_created"
    assert event["outcome"] == "refused" and event["failed"] == 1


def test_create_protocol_with_tasks_returns_the_links(fake):
    proto, errors = protocol_model.create_protocol(
        "d1", "cq_simplifié", WHEN, {"title": "Protocole"},
        auto_create_tasks=True)
    assert errors == []
    tasks = fake.peek_collection("tasks")
    assert len(tasks) == 7
    assert {s["linked_task_id"] for s in proto["steps"]} == set(tasks)
