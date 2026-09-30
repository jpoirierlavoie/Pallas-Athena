"""Une tâche et l'étape de protocole qui la lie (lot 1a, L2).

Deux défauts silencieux, fermés dans le MODÈLE des tâches — le web, le
téléphone et le connecteur passent tous par ``models/task.update_task`` :

1. **Rouvrir une tâche laissait son étape « complétée » dans un protocole
   fermé.** La cascade côté tâche ne balayait que les protocoles ACTIFS ;
   quand la dernière étape avait fermé le protocole, rouvrir la tâche
   (au formulaire, au téléphone) laissait tâche « à faire », étape
   « complétée », protocole « complété » — sans une ligne nulle part.
   La cascade passe désormais par la règle unique du bouton d'étape
   (``set_step_status``) : un protocole fermé PAR LA CASCADE se réactive,
   si aucun autre protocole du dossier n'est actif ; un protocole fermé à
   la main ou suspendu reste tel quel, et le refus se journalise.
2. **Une tâche liée à une étape changeait de dossier sans rien dire** —
   l'étape la perdait là où les rapports du connecteur la cherchent. Le
   déplacement est refusé, lu strictement (une lecture en échec refuse).

Tout passe par le faux Firestore partagé, les vraies routes web et DAV.
"""

import logging
import os
import pathlib
import re
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
    import dav.dossier_collections as dc
    import dav.sync as dav_sync  # its db is patched below
    from models import dossier as dossier_model
    from models import protocol as protocol_model  # noqa: F401
    from models import task as task_model
    import routes.tasks as tasks_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (dav_sync, dossier_model)

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
P = "p1"
AUTH = {"Authorization": "Basic dGVzdEBleGFtcGxlLmNvbTpwdw=="}


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for did, num in (("d1", "2026-001"), ("d2", "2026-002")):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": num,
                                      "title": "T c. L", "status": "actif"})
        fake.seed(f"dav_sync/dossier:{did}", {"ctag": "c0", "sync_token": "c0",
                                             "updated_at": WHEN})
    return fake


def _protocol(fake, pid: str = P, *, status: str = "actif",
              closed_by=None, dossier_id: str = "d1") -> None:
    doc = {
        "id": pid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": "Protocole de l'instance",
        "protocol_type": "conventionnel", "status": status,
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": f"pe-{pid}", "created_at": WHEN, "updated_at": WHEN,
    }
    if closed_by is not None:
        doc["closed_by"] = closed_by
        doc["closed_at"] = WHEN
    fake.seed(f"protocols/{pid}", doc)


def _step(fake, sid: str, *, pid: str = P, status: str = "à_venir",
          task=None, order: int = 1) -> None:
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


def _break_queries(monkeypatch, fake, collection_id: str = "protocols"):
    server = fake._fake_server
    real = server.run_query

    def _run_query(request, *a, **kw):
        sq = request.get("structured_query") if hasattr(request, "get") else None
        froms = [f.collection_id for f in getattr(sq, "from_", [])] if sq else []
        if collection_id in froms:
            raise RuntimeError("store unavailable")
        return real(request, *a, **kw)

    monkeypatch.setattr(server, "run_query", _run_query)


def _protocol_events(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records if r.name == "pallas.protocol"]


# ══════════════════════════════════════════════════════════════════════
# 1. Rouvrir une tâche rouvre son étape — et le protocole fermé par la cascade
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("reopened", ["à_faire", "en_cours"])
def test_reopening_a_task_reactivates_the_protocol_its_step_closed(
    fake, reopened
):
    """THE defect: the protocol had closed on this task's step; reopening
    the task left task open / step complété / protocol complété."""
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _step(fake, "s2", status="complété", order=2)
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task("t1", {"status": reopened})
    assert errors == []
    assert fake.peek("tasks/t1")["status"] == reopened
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "à_venir"
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "actif"
    assert stored["closed_by"] == "" and stored["closed_at"] is None


def test_a_reopen_blocked_by_another_active_protocol_is_logged(fake, caplog):
    """The task write has committed — it cannot be refused any more — so
    the step and the protocol stay closed, and the refusal is the trace."""
    _protocol(fake, status="complété", closed_by="auto")
    _protocol(fake, "p2")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    with caplog.at_level(logging.INFO):
        _doc, errors = task_model.update_task("t1", {"status": "à_faire"})
    assert errors == []
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "complété"
    assert fake.peek(f"protocols/{P}")["status"] == "complété"
    (event,) = [e for e in _protocol_events(caplog)
                if e["event"] == "step_status_refused"]
    assert event["reason"] == "autre_protocole_actif"
    assert event["step_id"] == "s1"


@pytest.mark.parametrize("closed_by", [None, "", "web"])
def test_a_protocol_not_closed_by_the_cascade_never_reopens(
    fake, caplog, closed_by
):
    """Closed on purpose (or before closed_by existed): reopening a task
    does not undo the lawyer's closure."""
    _protocol(fake, status="complété", closed_by=closed_by)
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    with caplog.at_level(logging.INFO):
        _doc, errors = task_model.update_task("t1", {"status": "à_faire"})
    assert errors == []
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "complété"
    assert fake.peek(f"protocols/{P}")["status"] == "complété"
    assert [e["reason"] for e in _protocol_events(caplog)
            if e["event"] == "step_status_refused"] == ["protocole_non_actif"]


def test_finishing_a_task_whose_step_sits_in_a_suspended_protocol(fake):
    _protocol(fake, status="suspendu")
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "à_venir"


def test_a_task_moved_away_before_the_rule_still_drives_its_step(fake):
    """The firm-wide actif fallback: the cascade has always reached a step
    whose task lives in another dossier."""
    _protocol(fake, dossier_id="d1")
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", dossier_id="d2")
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "complété"


def test_the_step_cascade_back_into_the_task_is_a_no_op(fake):
    """set_step_status cascades into the linked task: here it finds it
    already in the target state — one task write in all, and no loop."""
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    fake.reset_logs()
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    task_writes = [c for c in fake.commits
                   if any(p == "tasks/t1" for _, p in c.ops)]
    assert len(task_writes) == 1
    assert "t1" not in task_model._SYNCING
    assert "t1" not in protocol_model._SYNCING


# ══════════════════════════════════════════════════════════════════════
# 2. Une tâche liée à une étape reste dans le dossier du protocole
# ══════════════════════════════════════════════════════════════════════


def test_moving_a_step_linked_task_is_refused_and_nothing_written(fake, caplog):
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    before = fake.peek("tasks/t1")
    with caplog.at_level(logging.INFO):
        doc, errors = task_model.update_task(
            "t1", {"dossier_id": "d2", "title": "Autre"})
    assert doc is None
    assert errors == [task_model.STEP_LINKED_MOVE_REFUSED]
    assert fake.peek("tasks/t1") == before
    (event,) = [e for e in _protocol_events(caplog)
                if e["event"] == "task_move_refused"]
    assert event == {"event": "task_move_refused", "outcome": "refused",
                     "protocol_id": P, "reason": "tache_liee",
                     "task_id": "t1", "step_id": "s1"}


def test_a_step_of_a_closed_protocol_still_keeps_its_task(fake):
    _protocol(fake, status="complété", closed_by="web")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task("t1", {"dossier_id": ""})
    assert errors == [task_model.STEP_LINKED_MOVE_REFUSED]
    assert fake.peek("tasks/t1")["dossier_id"] == "d1"


def test_a_task_moved_away_before_the_rule_may_come_back(fake):
    """Revue L2 : une tâche déplacée AVANT la règle (son étape la retrouve
    par le repli des protocoles actifs du cabinet) pouvait être refusée
    même pour REVENIR dans le dossier de son protocole — bloquée à jamais,
    sous un message affirmant qu'elle « reste » dans un dossier où elle
    n'est pas. Le retour est permis ; tout autre départ reste refusé."""
    _protocol(fake, dossier_id="d1")
    _step(fake, "s1", task="t1")
    _task(fake, "t1", dossier_id="d2")
    fake.seed("dossiers/d3", {"id": "d3", "file_number": "2026-003",
                              "title": "T c. L", "status": "actif"})
    _doc, errors = task_model.update_task("t1", {"dossier_id": "d3"})
    assert errors == [task_model.STEP_LINKED_MOVE_REFUSED]
    assert fake.peek("tasks/t1")["dossier_id"] == "d2"
    _doc, errors = task_model.update_task("t1", {"dossier_id": "d1"})
    assert errors == []
    assert fake.peek("tasks/t1")["dossier_id"] == "d1"
    # Home again, the rule holds as for any linked task.
    _doc, errors = task_model.update_task("t1", {"dossier_id": "d2"})
    assert errors == [task_model.STEP_LINKED_MOVE_REFUSED]


def test_an_unlinked_task_moves_freely(fake):
    _protocol(fake)
    _step(fake, "s1", task="t-other")
    _task(fake, "t1")
    _doc, errors = task_model.update_task("t1", {"dossier_id": "d2"})
    assert errors == []
    assert fake.peek("tasks/t1")["dossier_id"] == "d2"


def test_an_unreadable_link_refuses_the_move(fake, monkeypatch):
    _task(fake, "t1")
    before = fake.peek("tasks/t1")
    _break_queries(monkeypatch, fake)
    doc, errors = task_model.update_task("t1", {"dossier_id": "d2"})
    assert doc is None
    assert errors == [task_model.STEP_LINK_CHECK_FAILED]
    assert fake.peek("tasks/t1") == before


@pytest.mark.parametrize("posted", ["d1", "", None])
def test_resaving_the_same_dossier_never_looks_the_link_up(
    fake, monkeypatch, posted
):
    """The web form posts dossier_id on every save; only a CHANGE is
    checked — so a store that cannot answer does not block an ordinary
    edit. (None and '' are the same « no dossier »: a « Général » task.)"""
    _task(fake, "t1", dossier_id="d1" if posted == "d1" else None)
    _break_queries(monkeypatch, fake)
    _doc, errors = task_model.update_task(
        "t1", {"dossier_id": posted, "title": "Retitrée"})
    assert errors == []
    assert fake.peek("tasks/t1")["title"] == "Retitrée"


# ── Par les vraies routes ─────────────────────────────────────────────


@pytest.fixture
def web(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, jsattr=lambda v: v)
    app.register_blueprint(tasks_routes.tasks_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def test_the_web_form_names_the_refused_move(web, fake):
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    resp = web.post("/taches/t1", data={
        "dossier_id": "d2", "title": "Tâche t1", "description": "",
        "priority": "normale", "status": "à_faire", "category": "suivi"})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "elle reste dans le dossier de ce protocole" in html
    assert fake.peek("tasks/t1")["dossier_id"] == "d1"


@pytest.fixture
def dav(fake, monkeypatch):
    monkeypatch.setattr("dav.dav_auth._check_credentials", lambda u, p: True)
    monkeypatch.setattr("dav.dav_auth._check_success_cache", lambda u, p: True)
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(dc.dossier_dav_bp)
    return app.test_client()


def test_a_jtx_move_of_a_linked_task_is_refused_and_both_copies_stay(
    dav, fake
):
    """jtx moves a task by PUTting it into the other collection. Refused
    (422, nothing written): the task stays in its dossier, and neither
    collection is tombstoned or bumped."""
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    body = dav.get("/dav/dossier-d1/t1.ics", headers=AUTH).get_data(as_text=True)
    assert "BEGIN:VTODO" in body
    before = fake.peek("tasks/t1")
    resp = dav.put("/dav/dossier-d2/t1.ics", data=body.encode("utf-8"),
                   headers={**AUTH, "Content-Type": "text/calendar"})
    assert resp.status_code == 422
    assert fake.peek("tasks/t1") == before
    for did in ("d1", "d2"):
        assert fake.peek(f"dav_sync/dossier:{did}")["ctag"] == "c0"
        assert not re.search(
            "t1", repr(fake.peek_collection(f"dav_sync/dossier:{did}/tombstones")))
