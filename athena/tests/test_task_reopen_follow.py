"""Rouvrir une tâche sans laisser son étape derrière (lot 1b, étape L5 — modèle).

Depuis le lot 1a, rouvrir une tâche (à_faire / en_cours) renvoie l'étape de
protocole qui la lie à « à_venir » — mais APRÈS l'écriture de la tâche.
Quand cette étape ne peut pas suivre (protocole suspendu, fermé à la main
ou avant ``closed_by``, ou fermé par la cascade alors qu'un autre protocole
du dossier est actif), ``set_step_status`` refuse et journalise : la tâche
est rouverte, l'étape reste « complété ». Le web et le téléphone s'en
accommodent (le juriste voit les deux pages) ; le connecteur, lui, ne doit
pas créer cet état (catalogue du plan, lot 1 : « refused before any write
if infeasible »).

D'où ``models/task.update_task(require_step_follow=True)`` : la prédiction
de ``models/protocol.step_reopen_refusal`` — le jumeau en lecture seule de
la porte de ``set_step_status`` — est consultée AVANT l'écriture, lue
strictement. Et ``list_tasks_for_note``, lecture stricte des tâches liées à
une note (le déplacement d'une note les signale). Le tout sur le vrai
client Firestore et le faux serveur partagé.
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
    from models import dossier as dossier_model  # noqa: F401
    from models import protocol as protocol_model
    from models import task as task_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
P = "p1"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "T", "status": "actif"})
    fake.seed("dav_sync/dossier:d1", {"ctag": "c0", "sync_token": "c0"})
    return fake


def _protocol(fake, pid=P, *, status="actif", closed_by=None):
    doc = {
        "id": pid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T", "title": "Protocole",
        "protocol_type": "conventionnel", "status": status,
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": f"pe-{pid}", "created_at": WHEN, "updated_at": WHEN,
    }
    if closed_by is not None:
        doc["closed_by"] = closed_by
        doc["closed_at"] = WHEN
    fake.seed(f"protocols/{pid}", doc)


def _step(fake, sid, *, pid=P, status="à_venir", task=None, order=1):
    fake.seed(f"protocols/{pid}/steps/{sid}", {
        "id": sid, "order": order, "title": f"Étape {sid}", "description": "",
        "cpc_reference": "", "deadline_date": LATER,
        "deadline_offset_days": None, "mandatory": False,
        "deadline_locked": False, "status": status,
        "completed_date": WHEN if status == "complété" else None,
        "linked_task_id": task, "linked_hearing_id": None, "notes": "",
        "date_confirmed": True, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN, "etag": f"se-{sid}",
    })


def _task(fake, tid="t1", *, status="à_faire", **over):
    doc = {
        "id": tid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T", "title": f"Tâche {tid}", "description": "",
        "priority": "normale", "status": status, "due_date": None,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(f"tasks/{tid}", doc)


def _protocol_events(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records if r.name == "pallas.protocol"]


# ══════════════════════════════════════════════════════════════════════
# 1. La prédiction — le jumeau en lecture seule de la porte d'étape
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("step_status, proto_status, closed_by, other, verdict", [
    ("à_venir", "suspendu", None, False, None),        # already open
    ("en_retard", "complété", "web", False, None),      # open in another form
    ("complété", "actif", None, False, None),
    ("complété", "complété", "auto", False, None),      # the cascade closed it
    ("complété", "complété", "auto", True, "autre_protocole_actif"),
    ("complété", "complété", "web", False, "protocole_non_actif"),
    ("complété", "complété", None, False, "protocole_non_actif"),
    ("complété", "suspendu", None, False, "protocole_non_actif"),
])
def test_step_reopen_refusal_mirrors_the_step_gate(
    fake, step_status, proto_status, closed_by, other, verdict,
):
    _protocol(fake, status=proto_status, closed_by=closed_by)
    if other:
        _protocol(fake, "p2")
    protocol = fake.peek(f"protocols/{P}")
    refusal = protocol_model.step_reopen_refusal(protocol, {"status": step_status})
    assert (refusal[1] if refusal else None) == verdict
    if verdict is not None:
        assert "Rien n'a été modifié" in refusal[0]


def test_step_reopen_refusal_agrees_with_what_set_step_status_does(fake):
    """The prediction is only worth something if it says what the gate
    will do: run both on the same states."""
    for closed_by, other in (("auto", False), ("auto", True), ("web", False)):
        fake._fake_server.docs.clear()
        _protocol(fake, status="complété", closed_by=closed_by)
        if other:
            _protocol(fake, "p2")
        _step(fake, "s1", status="complété")
        predicted = protocol_model.step_reopen_refusal(
            fake.peek(f"protocols/{P}"), fake.peek(f"protocols/{P}/steps/s1"))
        _step_doc, errors, _outcome = protocol_model.set_step_status(
            P, "s1", "à_venir")
        assert (predicted is None) == (errors == []), (closed_by, other)


# ══════════════════════════════════════════════════════════════════════
# 2. update_task(require_step_follow=True)
# ══════════════════════════════════════════════════════════════════════


def test_the_web_path_still_reopens_beside_a_step_that_cannot_follow(fake):
    """Compatibility pin: without the keyword, lot 1a's behaviour holds —
    the task reopens and the step's refusal is only logged."""
    _protocol(fake, status="complété", closed_by="web")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task("t1", {"status": "à_faire"})
    assert errors == []
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "complété"


@pytest.mark.parametrize("status, closed_by, other, reason", [
    ("complété", "web", False, "protocole_non_actif"),
    ("complété", None, False, "protocole_non_actif"),
    ("suspendu", None, False, "protocole_non_actif"),
    ("complété", "auto", True, "autre_protocole_actif"),
])
def test_require_step_follow_refuses_before_any_write(
    fake, caplog, status, closed_by, other, reason,
):
    """THE defect the flag closes: the old path committed the task and
    left it open beside a « complété » step."""
    _protocol(fake, status=status, closed_by=closed_by)
    if other:
        _protocol(fake, "p2")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    fake.reset_logs()
    with caplog.at_level(logging.INFO):
        doc, errors = task_model.update_task(
            "t1", {"status": "à_faire"}, require_step_follow=True)
    assert doc is None and len(errors) == 1
    assert errors[0].startswith("Rouvrir cette tâche rouvrirait aussi")
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert fake.commits == []
    (event,) = [e for e in _protocol_events(caplog)
                if e["event"] == "task_reopen_refused"]
    assert event["reason"] == reason
    assert event["task_id"] == "t1" and event["step_id"] == "s1"


def test_require_step_follow_lets_a_followable_cascade_through(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task(
        "t1", {"status": "à_faire"}, require_step_follow=True)
    assert errors == []
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "à_venir"
    assert fake.peek(f"protocols/{P}")["status"] == "actif"


def test_an_open_step_needs_nothing(fake):
    _protocol(fake, status="suspendu")
    _step(fake, "s1", status="à_venir", task="t1")
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task(
        "t1", {"status": "à_faire"}, require_step_follow=True)
    assert errors == []


def test_a_closing_change_is_never_held_back(fake):
    """The flag guards REOPENS only: completing a task under a suspended
    protocol behaves exactly as before."""
    _protocol(fake, status="suspendu")
    _step(fake, "s1", status="à_venir", task="t1")
    _task(fake, "t1")
    _doc, errors = task_model.update_task(
        "t1", {"status": "terminée"}, require_step_follow=True)
    assert errors == []
    assert fake.peek("tasks/t1")["status"] == "terminée"


def test_an_unreadable_link_refuses_the_reopen(fake, monkeypatch):
    _task(fake, "t1", status="terminée")

    def _boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(protocol_model, "find_step_for_task", _boom)
    doc, errors = task_model.update_task(
        "t1", {"status": "à_faire"}, require_step_follow=True)
    assert doc is None and errors == [task_model.STEP_LINK_CHECK_FAILED]
    assert fake.peek("tasks/t1")["status"] == "terminée"


# ══════════════════════════════════════════════════════════════════════
# 3. list_tasks_for_note — strict
# ══════════════════════════════════════════════════════════════════════


def test_list_tasks_for_note_is_strict(fake, monkeypatch):
    _task(fake, "t1", related_note_id="n1")
    _task(fake, "t2", related_note_id="n2")
    assert [t["id"] for t in task_model.list_tasks_for_note("n1")] == ["t1"]
    assert task_model.list_tasks_for_note("") == []

    server = fake._fake_server

    def _down(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(server, "run_query", _down)
    with pytest.raises(RuntimeError):
        task_model.list_tasks_for_note("n1")
