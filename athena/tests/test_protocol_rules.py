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
from datetime import date, datetime, timezone
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
    # Snapshotted: since the L2 review the caller's step dicts take the
    # stamps the link wrote (so an etag handed on is the stored one).
    etags_before = [s["etag"] for s in steps]
    with caplog.at_level(logging.INFO):
        report = protocol_model.create_linked_tasks(proto["id"], proto, steps)
    assert report == {"created": 6, "linked": 6, "failed": 1}
    stored = sorted(fake.peek_collection(f"protocols/{proto['id']}/steps")
                    .values(), key=lambda s: s["order"])
    assert stored[0]["linked_task_id"] is None
    assert steps[0]["etag"] == stored[0]["etag"] == etags_before[0]
    for before, held, after in zip(etags_before[1:], steps[1:], stored[1:]):
        assert after["linked_task_id"] in fake.peek_collection("tasks")
        assert after["etag"] != before   # linking is a step write
        assert held["etag"] == after["etag"]
        assert held["linked_task_id"] == after["linked_task_id"]
    assert proto["etag"] == fake.peek(f"protocols/{proto['id']}")["etag"]
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



# ══════════════════════════════════════════════════════════════════════
# 7. update_protocol : ses champs, la règle « un seul actif », qui ferme,
#    et le recalcul dans la MÊME écriture
# ══════════════════════════════════════════════════════════════════════

START2 = datetime(2026, 10, 5, tzinfo=UTC)


def _cq(fake) -> dict:
    proto, errors = protocol_model.create_protocol(
        "d1", "cq_simplifié", WHEN, {"title": "Protocole"})
    assert errors == []
    return proto


def _steps_by_order(fake, pid: str) -> list[dict]:
    return sorted(fake.peek_collection(f"protocols/{pid}/steps").values(),
                  key=lambda s: s["order"])


@pytest.mark.parametrize("key", ["protocol_type", "dossier_id", "end_date",
                                 "closed_by", "steps", "id"])
def test_update_protocol_refuses_a_field_it_does_not_own(fake, key):
    _protocol(fake)
    before = _snapshot(fake)
    doc, errors = protocol_model.update_protocol(P, {key: "x"})
    assert doc is None
    assert errors == [f"Le champ « {key} » d'un protocole ne se modifie pas "
                      "ainsi."]
    assert _snapshot(fake) == before


def test_reactivating_beside_another_active_protocol_is_refused(fake):
    """update_protocol had NO one-actif guard: the edit form's status
    select reactivated a second protocol freely."""
    _protocol(fake, status="suspendu", closed_by="web")
    _protocol(fake, "p2")
    before = _snapshot(fake)
    doc, errors = protocol_model.update_protocol(P, {"status": "actif"})
    assert doc is None
    assert errors == [protocol_model.OTHER_ACTIVE_ON_REACTIVATION]
    assert _snapshot(fake) == before


def test_an_unreadable_reactivation_check_refuses(fake, monkeypatch):
    _protocol(fake, status="suspendu")
    _break_queries(monkeypatch, fake)
    before = _snapshot(fake)
    doc, errors = protocol_model.update_protocol(P, {"status": "actif"})
    assert doc is None and errors == [protocol_model.ACTIVE_CHECK_FAILED]
    assert _snapshot(fake) == before


@pytest.mark.parametrize("status", ["complété", "suspendu"])
def test_a_deliberate_closure_names_its_writer_and_reactivation_clears_it(
    fake, status
):
    _protocol(fake)
    with provenance.writing_via("mcp", tool="t"):
        _doc, errors = protocol_model.update_protocol(P, {"status": status})
    assert errors == []
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == status
    assert stored["closed_by"] == "mcp"          # never « auto »
    assert stored["closed_at"] is not None
    _doc, errors = protocol_model.update_protocol(P, {"status": "actif"})
    assert errors == []
    stored = fake.peek(f"protocols/{P}")
    assert stored["closed_by"] == "" and stored["closed_at"] is None


def test_a_start_date_change_recomputes_in_the_same_write(fake):
    """It used to be two writes — the protocol, then the steps — and the
    recompute's errors were ignored. Completed steps no longer move."""
    proto = _cq(fake)
    pid = proto["id"]
    first = _steps_by_order(fake, pid)[0]
    protocol_model.set_step_status(pid, first["id"], "complété")
    before = _steps_by_order(fake, pid)
    fake.reset_logs()
    doc, errors = protocol_model.update_protocol(pid, {"start_date": START2})
    assert errors == []
    assert len(fake.commits) == 1                 # ONE transaction
    after = _steps_by_order(fake, pid)
    assert after[0]["deadline_date"] == before[0]["deadline_date"]
    assert after[0]["etag"] == before[0]["etag"]  # untouched
    for b, a in zip(before[1:], after[1:]):
        assert a["deadline_date"] == protocol_model._compute_deadline(
            START2, b["deadline_offset_days"])
        assert a["etag"] != b["etag"]
    report = doc["_recompute"]
    assert {m["step_id"] for m in report["moved"]} == {s["id"] for s in after[1:]}
    assert report["preserved"] == [{"step_id": after[0]["id"],
                                    "reason": "completed"}]
    stored = fake.peek(f"protocols/{pid}")
    assert stored["start_date"] == START2
    assert stored["end_date"] == protocol_model._compute_end_date(START2, after)
    assert "_recompute" not in stored               # never stored


def _cs_step(fake, sid, order, offset, **over):
    doc = {
        "id": sid, "order": order, "title": f"Étape {sid}", "description": "",
        "cpc_reference": "", "deadline_offset_days": offset,
        "deadline_date": protocol_model._compute_deadline(WHEN, offset),
        "mandatory": True, "deadline_locked": False, "status": "à_venir",
        "completed_date": None, "linked_task_id": None,
        "linked_hearing_id": None, "notes": "", "date_confirmed": False,
        "phase": "", "sous_phase": "", "created_at": WHEN, "updated_at": WHEN,
        "etag": f"se-{sid}",
    }
    doc.update(over)
    fake.seed(f"protocols/{P}/steps/{sid}", doc)


def test_only_truly_confirmed_cs_dates_resist_a_start_date_change(fake):
    _protocol(fake, ptype="cs_ordinaire")
    hand = datetime(2026, 11, 20, tzinfo=UTC)
    _cs_step(fake, "plain", 1, 15)                              # suggestion
    _cs_step(fake, "spurious", 2, 45, date_confirmed=True)      # notes-only save
    _cs_step(fake, "legacy", 3, 120, date_confirmed=True,
             deadline_date=hand)                                # moved by hand
    _cs_step(fake, "stamped", 4, 150, date_confirmed=True,
             date_confirmed_at=WHEN)                            # explicit
    doc, errors = protocol_model.update_protocol(P, {"start_date": START2})
    assert errors == []
    report = doc["_recompute"]
    assert {m["step_id"] for m in report["moved"]} == {"plain", "spurious"}
    assert sorted(report["preserved"], key=lambda e: e["step_id"]) == [
        {"step_id": "legacy", "reason": "confirmed"},
        {"step_id": "stamped", "reason": "confirmed"},
    ]
    assert _step_doc(fake, "legacy")["deadline_date"] == hand


def test_recompute_deadlines_is_update_protocol_and_preserves_too(fake):
    proto = _cq(fake)
    pid = proto["id"]
    first = _steps_by_order(fake, pid)[0]
    protocol_model.set_step_status(pid, first["id"], "complété")
    before = _steps_by_order(fake, pid)[0]["deadline_date"]
    doc, errors = protocol_model.recompute_deadlines(pid, START2)
    assert errors == []
    assert _steps_by_order(fake, pid)[0]["deadline_date"] == before
    assert doc["_recompute"]["preserved"][0]["reason"] == "completed"


def test_a_step_completed_during_the_recompute_is_decided_again(fake):
    """The steps are read INSIDE the transaction: a step completed between
    the read and the commit aborts it, and the retry preserves it."""
    proto = _cq(fake)
    pid = proto["id"]
    target = _steps_by_order(fake, pid)[2]
    fired = []

    def _rival(info):
        if not fired and ("set", f"protocols/{pid}") in info.ops:
            fired.append(True)
            doc = fake.peek(f"protocols/{pid}/steps/{target['id']}")
            doc.update(status="complété", completed_date=WHEN, etag="rival")
            fake.external_write(f"protocols/{pid}/steps/{target['id']}", doc)

    fake.add_commit_hook(_rival)
    doc, errors = protocol_model.update_protocol(pid, {"start_date": START2})
    assert fired and errors == []
    stored = fake.peek(f"protocols/{pid}/steps/{target['id']}")
    assert stored["deadline_date"] == target["deadline_date"]
    assert stored["etag"] == "rival"
    assert {"step_id": target["id"], "reason": "completed"} in (
        doc["_recompute"]["preserved"])


def test_an_unchanged_save_writes_nothing(fake):
    _protocol(fake)
    fake.reset_logs()
    doc, errors = protocol_model.update_protocol(P, {
        "title": "Protocole de l'instance", "notes": "", "status": "actif",
        "start_date": WHEN})
    assert errors == [] and doc["etag"] == f"pe-{P}"
    # The transaction commits EMPTY (the real client always commits it).
    assert [c.ops for c in fake.commits] in ([], [()])


def test_update_protocol_refuses_a_stale_etag(fake):
    _protocol(fake)
    before = _snapshot(fake)
    doc, errors = protocol_model.update_protocol(
        P, {"notes": "Nouveau"}, expected_etag="perimee")
    assert doc is None and errors == [concurrency.STALE_ETAG_ERROR]
    assert _snapshot(fake) == before
    doc, errors = protocol_model.update_protocol(
        P, {"notes": "Nouveau"}, expected_etag=f"pe-{P}")
    assert errors == [] and fake.peek(f"protocols/{P}")["notes"] == "Nouveau"


# ══════════════════════════════════════════════════════════════════════
# 8. add_step : ses champs, ses épingles, et la date de fin
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("key", ["mandatory", "deadline_locked", "status",
                                 "linked_task_id", "deadline_offset_days",
                                 "order", "id"])
def test_add_step_refuses_what_it_pins(fake, key):
    _protocol(fake)
    before = _snapshot(fake)
    step, errors = protocol_model.add_step(P, {"title": "Étape", key: True})
    assert step is None
    assert errors == [f"Le champ « {key} » d'une étape ne se modifie pas "
                      "ainsi."]
    assert _snapshot(fake) == before


def test_an_added_step_is_pinned_stamped_and_moves_the_end_date(fake):
    _protocol(fake)
    _step(fake, "s1", order=3)
    far = datetime(2100, 6, 1, tzinfo=UTC)
    step, errors = protocol_model.add_step(P, {
        "title": "Plaidoirie", "deadline_date": far, "phase": "INS"})
    assert errors == []
    stored = _step_doc(fake, step["id"])
    assert stored["order"] == 4
    assert (stored["mandatory"], stored["deadline_locked"],
            stored["deadline_offset_days"], stored["status"],
            stored["linked_task_id"], stored["date_confirmed"]) == (
        False, False, None, "à_venir", None, True)
    assert stored["sous_phase"] == "INS-00"
    assert stored["etag"] and stored["created_via"] == "script"
    proto = fake.peek(f"protocols/{P}")
    assert proto["end_date"] == far and proto["etag"] != f"pe-{P}"


# ══════════════════════════════════════════════════════════════════════
# 9. update_step : ses champs, le texte du C.p.c., et « confirmée » vrai
# ══════════════════════════════════════════════════════════════════════


def test_a_posted_status_is_refused_with_its_own_message(fake):
    _protocol(fake)
    _step(fake, "s1")
    before = _snapshot(fake)
    step, errors = protocol_model.update_step(P, "s1", {"status": "complété"})
    assert step is None
    assert errors == [protocol_model.STEP_STATUS_NOT_EDITABLE]
    assert _snapshot(fake) == before


@pytest.mark.parametrize("key", ["mandatory", "deadline_locked",
                                 "linked_task_id", "order", "date_confirmed"])
def test_update_step_refuses_a_field_it_does_not_own(fake, key):
    _protocol(fake)
    _step(fake, "s1")
    step, errors = protocol_model.update_step(P, "s1", {key: False})
    assert step is None
    assert errors == [f"Le champ « {key} » d'une étape ne se modifie pas "
                      "ainsi."]


@pytest.mark.parametrize("field", ["title", "description", "cpc_reference"])
def test_the_cpc_text_of_a_template_step_is_locked(fake, field):
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 15, title="Réponse", cpc_reference="art. 145")
    before = _snapshot(fake)
    step, errors = protocol_model.update_step(P, "s1", {field: "Réécrit"})
    assert step is None and errors == [protocol_model.LEGAL_TEXT_LOCKED]
    assert _snapshot(fake) == before
    # Posting the SAME text is not a change, and a note still saves.
    same = _step_doc(fake, "s1")[field]
    step, errors = protocol_model.update_step(
        P, "s1", {field: same, "notes": "Suivi"})
    assert errors == [] and _step_doc(fake, "s1")["notes"] == "Suivi"


def test_a_locked_deadline_keeps_its_date_and_its_notes_still_save(fake):
    proto = _cq(fake)
    step = _steps_by_order(fake, proto["id"])[1]
    other = datetime(2026, 12, 24, tzinfo=UTC)
    _s, errors = protocol_model.update_step(
        proto["id"], step["id"], {"deadline_date": other})
    assert errors == [protocol_model.DEADLINE_LOCKED]
    _s, errors = protocol_model.update_step(
        proto["id"], step["id"],
        {"deadline_date": step["deadline_date"], "notes": "Vu"})
    assert errors == []


def test_a_template_step_deadline_cannot_be_cleared(fake):
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 15)
    step, errors = protocol_model.update_step(P, "s1", {"deadline_date": None})
    assert errors == [protocol_model.MANDATORY_DEADLINE_REQUIRED]


def test_a_notes_only_save_no_longer_confirms_a_cs_date(fake):
    """THE silent defect the recompute rule depends on: the inline form
    always posts the deadline beside the notes, and ANY key named
    deadline_date set date_confirmed — so a note « confirmed » a date and
    the next start-date change silently stopped moving it."""
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 15)
    unchanged = _step_doc(fake, "s1")["deadline_date"]
    _s, errors = protocol_model.update_step(
        P, "s1", {"deadline_date": unchanged, "notes": "Appeler le greffe"})
    assert errors == []
    stored = _step_doc(fake, "s1")
    assert stored["notes"] == "Appeler le greffe"
    assert stored["date_confirmed"] is False
    assert stored.get("date_confirmed_at") is None
    doc, _ = protocol_model.update_protocol(P, {"start_date": START2})
    assert [m["step_id"] for m in doc["_recompute"]["moved"]] == ["s1"]


def test_a_changed_deadline_confirms_it_and_refreshes_the_end_date(fake):
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 15)
    far = datetime(2101, 1, 10, tzinfo=UTC)
    step, errors = protocol_model.update_step(P, "s1", {"deadline_date": far})
    assert errors == []
    stored = _step_doc(fake, "s1")
    assert stored["date_confirmed"] is True
    assert stored["date_confirmed_at"] is not None
    assert step["_deadline_changed"]["new"] == far
    assert "_deadline_changed" not in stored
    assert fake.peek(f"protocols/{P}")["end_date"] == far
    doc, _ = protocol_model.update_protocol(P, {"start_date": START2})
    assert doc["_recompute"]["preserved"] == [
        {"step_id": "s1", "reason": "confirmed"}]


def test_an_explicit_confirmation_keeps_the_date_and_marks_it(fake):
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 15)
    date_before = _step_doc(fake, "s1")["deadline_date"]
    step, errors = protocol_model.update_step(P, "s1", {"confirm_date": True})
    assert errors == [] and "_deadline_changed" not in step
    stored = _step_doc(fake, "s1")
    assert stored["deadline_date"] == date_before
    assert stored["date_confirmed"] is True and stored["date_confirmed_at"]


def test_a_legacy_spurious_confirmation_can_be_confirmed_for_real(fake):
    """Revue L2. Une étape CS dont le drapeau vient d'une simple note
    d'AVANT le lot 1a (``date_confirmed`` vrai, aucun ``date_confirmed_at``,
    date toujours égale au gabarit) est une SUGGESTION pour le recalcul —
    qui la déplace. La page la disait pourtant confirmée (ni badge, ni
    case), et la case, postée quand même, était ignorée : aucun moyen de la
    confirmer sans changer sa date. Le prédicat est désormais celui du
    recalcul, et la confirmation explicite l'estampille."""
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 45, date_confirmed=True)
    proto = fake.peek(f"protocols/{P}")
    assert protocol_model.date_needs_confirmation(proto, _step_doc(fake, "s1"))
    _s, errors = protocol_model.update_step(P, "s1", {"confirm_date": True})
    assert errors == []
    stored = _step_doc(fake, "s1")
    assert stored["date_confirmed_at"] is not None
    assert not protocol_model.date_needs_confirmation(proto, stored)
    doc, _ = protocol_model.update_protocol(P, {"start_date": START2})
    assert doc["_recompute"]["preserved"] == [
        {"step_id": "s1", "reason": "confirmed"}]


def _legacy_cs_step(start: datetime, offset: int, stored: datetime) -> dict:
    """A CS step flagged ``date_confirmed`` before ``date_confirmed_at``
    existed (a notes-only save of lot 1a's era), stored at ``stored``."""
    return {**protocol_model._default_step(), "deadline_offset_days": offset,
            "deadline_date": stored, "date_confirmed": True}


@pytest.mark.parametrize("start, offset, old, new", [
    # Thu 11 Dec 2025 + 15 = Fri 26 Dec: juridical for the old calendar,
    # a holiday in procedure every year since (art. 82 C.p.c.) → Mon 29.
    ((2025, 12, 11), 15, (2025, 12, 26), (2025, 12, 29)),
    # Thu 18 Dec 2025 + 15 = Fri 2 Jan 2026 → Mon 5 Jan.
    ((2025, 12, 18), 15, (2026, 1, 2), (2026, 1, 5)),
    # Sat 9 Jun 2029 + 15 = Sun 24 Jun: the old calendar invented a Monday
    # 25 June holiday → Tue 26; art. 61(23) e) L.i. names none → Mon 25.
    ((2029, 6, 9), 15, (2029, 6, 26), (2029, 6, 25)),
])
def test_a_legacy_flag_on_the_old_calendars_suggestion_is_still_a_suggestion(
        start, offset, old, new):
    """Revue du 2026-09-30. The legacy branch recognises a suggestion by
    comparing the stored date with a recomputation. The calendar fix moved
    the recomputation, so a date the OLD calendar suggested read
    « confirmed » on that evidence alone — badge gone, MCP
    ``date_is_suggestion`` false, and PRESERVED by a start-date change.
    Either computation now counts as « what the template suggested »."""
    start_dt = datetime(*start, tzinfo=UTC)
    old_dt, new_dt = datetime(*old, tzinfo=UTC), datetime(*new, tzinfo=UTC)
    assert protocol_model._legacy_compute_deadline(start_dt, offset) == old_dt
    assert protocol_model._compute_deadline(start_dt, offset) == new_dt
    proto = {"protocol_type": "cs_ordinaire", "start_date": start_dt}
    for stored in (old_dt, new_dt):
        step = _legacy_cs_step(start_dt, offset, stored)
        assert protocol_model.date_needs_confirmation(proto, step), stored
        assert not protocol_model._date_truly_confirmed(
            "cs_ordinaire", start_dt, step)
    # A date moved by hand — neither calendar's — stays confirmed.
    hand = _legacy_cs_step(start_dt, offset, datetime(2030, 3, 4, tzinfo=UTC))
    assert not protocol_model.date_needs_confirmation(proto, hand)
    # A stamped confirmation is never second-guessed, whatever the date.
    stamped = {**_legacy_cs_step(start_dt, offset, old_dt),
               "date_confirmed_at": start_dt}
    assert not protocol_model.date_needs_confirmation(proto, stamped)


def test_a_legacy_old_calendar_suggestion_follows_a_start_date_change(fake):
    """The same step MOVES with the start date, as it did before the fix."""
    _protocol(fake, ptype="cs_ordinaire")
    fake.seed(f"protocols/{P}", {**fake.peek(f"protocols/{P}"),
                                 "start_date": datetime(2025, 12, 11,
                                                        tzinfo=UTC)})
    _cs_step(fake, "s1", 1, 15, date_confirmed=True,
             deadline_date=datetime(2025, 12, 26, tzinfo=UTC))
    doc, errors = protocol_model.update_protocol(P, {"start_date": START2})
    assert errors == []
    assert [m["step_id"] for m in doc["_recompute"]["moved"]] == ["s1"]


@pytest.mark.parametrize("day, juridical", [
    ((2025, 12, 26), True),     # Fri, Christmas Thu — no substitute then
    ((2022, 12, 26), False),    # Mon after a Sunday Christmas
    ((2023, 1, 2), False),      # Mon after a Sunday 1 January
    ((2018, 6, 25), False),     # Mon after a Sunday 24 June
    ((2018, 7, 2), False),      # Mon after a Sunday 1 July (kept since)
    ((2026, 1, 2), True),       # Fri — the old calendar missed art. 82
])
def test_the_legacy_calendar_is_the_retired_rule(day, juridical):
    """``_legacy_is_juridical_day`` reproduces the pre-2026-09-30 rule —
    and nothing else — so the comparison above means what it says."""
    assert protocol_model._legacy_is_juridical_day(date(*day)) is juridical


def test_a_moved_cs_date_is_a_suggestion_again(fake):
    """Revue L2 : une étape CS que la nouvelle date de début DÉPLACE n'était
    pas vraiment confirmée (sinon elle serait conservée) ; sa nouvelle date
    est celle du gabarit. Le drapeau hérité d'une simple note ne doit plus
    la dire confirmée — ni à la page, ni au connecteur qui lit
    ``date_confirmed``."""
    _protocol(fake, ptype="cs_ordinaire")
    _cs_step(fake, "s1", 1, 45, date_confirmed=True)
    doc, errors = protocol_model.update_protocol(P, {"start_date": START2})
    assert errors == [] and [m["step_id"] for m in
                             doc["_recompute"]["moved"]] == ["s1"]
    stored = _step_doc(fake, "s1")
    assert stored["date_confirmed"] is False
    assert stored.get("date_confirmed_at") is None
    (returned,) = doc["steps"]
    assert returned["date_confirmed"] is False


def test_confirmation_is_only_for_a_cs_suggestion(fake):
    """A CQ step (the law's date) and a custom step are never « to
    confirm »; neither is a CS date the lawyer truly confirmed."""
    _protocol(fake, ptype="cq_simplifié")
    cq = {**protocol_model._default_step(), "deadline_offset_days": 10,
          "date_confirmed": True}
    assert not protocol_model.date_needs_confirmation(
        fake.peek(f"protocols/{P}"), cq)
    cs_proto = {"protocol_type": "cs_ordinaire", "start_date": WHEN}
    custom = {**protocol_model._default_step(), "date_confirmed": True}
    assert not protocol_model.date_needs_confirmation(cs_proto, custom)
    stamped = {**protocol_model._default_step(), "deadline_offset_days": 15,
               "deadline_date": protocol_model._compute_deadline(WHEN, 15),
               "date_confirmed": True, "date_confirmed_at": WHEN}
    assert not protocol_model.date_needs_confirmation(cs_proto, stamped)


def test_an_unchanged_step_save_writes_nothing(fake):
    _protocol(fake)
    _step(fake, "s1", etag="se0")
    fake.reset_logs()
    step, errors = protocol_model.update_step(
        P, "s1", {"deadline_date": LATER, "notes": ""})
    assert errors == [] and step["etag"] == "se0"
    # The transaction commits EMPTY (the real client always commits it).
    assert [c.ops for c in fake.commits] in ([], [()])


def test_update_step_refuses_a_stale_etag(fake):
    _protocol(fake)
    _step(fake, "s1", etag="se0")
    before = _snapshot(fake)
    step, errors = protocol_model.update_step(
        P, "s1", {"notes": "x"}, expected_etag="perimee")
    assert step is None and errors == [concurrency.STALE_ETAG_ERROR]
    assert _snapshot(fake) == before


# ══════════════════════════════════════════════════════════════════════
# 10. Ce que le connecteur demande au modèle (lot 1b, L6)
# ══════════════════════════════════════════════════════════════════════


def _break_gets(monkeypatch, fake, fragment: str = "/protocols/"):
    """Every keyed read of a *fragment* document fails at the store."""
    server = fake._fake_server
    real = server.batch_get_documents

    def _flaky(request, *a, **k):
        docs = request.get("documents") if hasattr(request, "get") else []
        if any(fragment in d for d in docs):
            raise RuntimeError("store down")
        return real(request, *a, **k)

    monkeypatch.setattr(server, "batch_get_documents", _flaky)


def test_the_strict_reader_tells_absent_from_unreadable(fake, monkeypatch):
    """« Protocole introuvable » about a protocol that exists would send the
    caller hunting for an id that was right: the strict reader answers None
    ONLY on an absence, and lets a read error through."""
    _protocol(fake)
    _step(fake, "s2", order=2)
    _step(fake, "s1", order=1)
    proto = protocol_model.get_protocol_strict(P)
    assert [s["id"] for s in proto["steps"]] == ["s1", "s2"]
    assert protocol_model.get_protocol_strict("nope") is None
    _break_gets(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        protocol_model.get_protocol_strict(P)
    assert protocol_model.get_protocol(P) is None       # the fail-open one


# D17 (2026-09-27): the « actif » rule for steps is EVERY caller's. It was
# the connector's choice (``require_active``) until then, and these two
# tests pinned that the web path — passing nothing — still added to and
# edited the steps of any protocol. The flag is gone; the rule is one.

@pytest.mark.parametrize("status, closed_by", [
    ("suspendu", "web"), ("complété", "auto"), ("complété", "web"),
])
def test_add_step_refuses_a_protocol_that_is_not_active(
    fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    before = _snapshot(fake)
    step, errors = protocol_model.add_step(P, {"title": "Étape"})
    assert step is None
    assert errors == [protocol_model.inactive_protocol_edit_error(status)]
    assert _snapshot(fake) == before
    # The refusal names the web's way out.
    assert "réactivez-le" in errors[0]
    with pytest.raises(TypeError):
        protocol_model.add_step(P, {"title": "Étape"}, require_active=False)


@pytest.mark.parametrize("status, closed_by", [
    ("suspendu", "web"), ("complété", "auto"), ("complété", "web"),
])
def test_update_step_refuses_a_protocol_that_is_not_active(
    fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    _step(fake, "s1", etag="se0")
    before = _snapshot(fake)
    step, errors = protocol_model.update_step(P, "s1", {"notes": "x"})
    assert step is None
    assert errors == [protocol_model.inactive_protocol_edit_error(status)]
    assert _snapshot(fake) == before
    # A request that changes nothing needs no open protocol.
    step, errors = protocol_model.update_step(P, "s1", {"notes": ""})
    assert errors == [] and step["etag"] == "se0"
    with pytest.raises(TypeError):
        protocol_model.update_step(P, "s1", {"notes": "x"},
                                   require_active=False)


def test_an_active_protocol_still_takes_step_additions_and_edits(fake):
    _protocol(fake)
    _step(fake, "s1", etag="se0")
    step, errors = protocol_model.add_step(P, {"title": "Étape"})
    assert errors == [] and _step_doc(fake, step["id"])["title"] == "Étape"
    step, errors = protocol_model.update_step(P, "s1", {"notes": "x"})
    assert errors == [] and _step_doc(fake, "s1")["notes"] == "x"


def test_a_recompute_hands_back_the_stored_etag_of_each_moved_step(fake):
    """update_protocol returned its moved steps with the etag they had
    BEFORE the recompute wrote them — the next compare-and-set on one of
    them (update_protocol_step, lot 1b) would have been refused for a
    version nobody else wrote. The L2 review rule, on the recompute path."""
    pid = _cq(fake)["id"]
    doc, errors = protocol_model.update_protocol(pid, {"start_date": START2})
    assert errors == []
    moved = {m["step_id"] for m in doc["_recompute"]["moved"]}
    assert moved
    for step in doc["steps"]:
        stored = _step_doc(fake, step["id"], pid)
        assert step["etag"] == stored["etag"], step["id"]
        assert step["deadline_date"] == stored["deadline_date"]
        if step["id"] in moved:
            assert stored["updated_via"] == "script"
