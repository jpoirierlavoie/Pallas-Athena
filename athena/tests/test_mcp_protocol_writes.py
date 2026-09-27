"""Le protocole de l'instance par le connecteur (lot 1b, étape L6).

Quatre outils — ``create_protocol``, ``update_protocol``,
``add_protocol_step``, ``update_protocol_step`` — et l'enrichissement des
lignes de ``list_protocol_steps``. Tout passe ici par le VRAI client
Firestore sur le faux serveur partagé (``tests/_fake_firestore.py``), les
vrais modèles, le vrai service (``services/protocoles.py``, la porte des
routes web), les vrais gestionnaires et le vrai protocole d'écriture
(``run_write``) ; on relit ce qui est STOCKÉ.

On épingle, pour chaque outil : ce qu'il écrit et ce qu'il n'écrit PAS (un
appel sans effet n'écrit rien, ne bumpe rien) ; le refus d'un etag périmé ;
les refus qui nomment leur champ (un seul protocole actif, le régime du
gabarit, le texte du C.p.c. d'une étape obligatoire, l'échéance verrouillée
d'une étape CQ) ; la cascade d'un statut d'étape — relue APRÈS l'écriture,
jamais prédite —, y compris le protocole entier qu'elle referme ; et la
synchronisation DavX5 des TÂCHES liées, les seules des quatre entités que le
téléphone voit.
"""

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
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support  # noqa: F401
    from models import dossier as dossier_model  # noqa: F401
    from models import protocol as protocol_model
    from models import task as task_model  # noqa: F401

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
OLD = datetime(2099, 11, 2, tzinfo=UTC)
P = "p1"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for did, num, status, tribunal in (
        ("d1", "2026-001", "actif", ""),
        ("d2", "2026-002", "actif", "Cour supérieure"),
        ("d3", "2026-003", "fermé", ""),
    ):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": num,
                                      "title": f"Dossier {num}",
                                      "status": status, "tribunal": tribunal})
        fake.seed(f"dav_sync/dossier:{did}", {"ctag": "c0", "sync_token": "c0",
                                             "updated_at": WHEN})
    return fake


def _ctag(fake, did="d1") -> str:
    return fake.peek(f"dav_sync/dossier:{did}")["ctag"]


def _protocol(fake, pid=P, *, status="actif", closed_by=None, dossier_id="d1",
              ptype="conventionnel"):
    doc = {
        "id": pid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "Dossier 2026-001", "title": "Protocole",
        "protocol_type": ptype, "status": status,
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": f"pe-{pid}", "created_at": WHEN, "updated_at": WHEN,
    }
    if closed_by is not None:
        doc["closed_by"] = closed_by
        doc["closed_at"] = WHEN
    fake.seed(f"protocols/{pid}", doc)


def _step(fake, sid, *, pid=P, status="à_venir", task=None, order=1,
          deadline=LATER, **over):
    doc = {
        "id": sid, "order": order, "title": f"Étape {sid}", "description": "",
        "cpc_reference": "", "deadline_date": deadline,
        "deadline_offset_days": None, "mandatory": False,
        "deadline_locked": False, "status": status,
        "completed_date": WHEN if status == "complété" else None,
        "linked_task_id": task, "linked_hearing_id": None, "notes": "",
        "date_confirmed": True, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN, "etag": f"se-{sid}",
    }
    doc.update(over)
    fake.seed(f"protocols/{pid}/steps/{sid}", doc)


def _task(fake, tid, *, status="à_faire", due=None, dossier_id="d1"):
    fake.seed(f"tasks/{tid}", {
        "id": tid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "Dossier 2026-001", "title": f"Tâche {tid}",
        "description": "", "priority": "normale", "status": status,
        "due_date": due,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    })


def _step_doc(fake, sid, pid=P) -> dict:
    return fake.peek(f"protocols/{pid}/steps/{sid}")


def _snapshot(fake) -> dict:
    return {rel: fake.peek(rel) for rel in list(fake._fake_server.docs)}


def _refused(call, *args, match=None):
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        call(*args)
    if match is not None:
        assert match in str(excinfo.value), str(excinfo.value)
    return excinfo.value


# ══════════════════════════════════════════════════════════════════════
# 0. Les vocabulaires recopiés sont ceux du modèle
# ══════════════════════════════════════════════════════════════════════


def test_the_protocol_vocabularies_are_the_models():
    assert tools._PROTOCOL_TYPES == list(protocol_model.VALID_PROTOCOL_TYPES)
    assert set(tools._PROTOCOL_STATUSES) == set(protocol_model.VALID_STATUSES)
    assert tools._STEP_STATUS_TARGETS == list(protocol_model.STEP_STATUS_TARGETS)
    for name in ("create_protocol", "update_protocol", "add_protocol_step",
                 "update_protocol_step"):
        assert name in tools.WRITE_TOOLS
    assert {"update_protocol", "update_protocol_step"} <= tools.EDIT_TOOLS
    assert not {"create_protocol", "add_protocol_step"} & tools.EDIT_TOOLS


# ══════════════════════════════════════════════════════════════════════
# 1. create_protocol
# ══════════════════════════════════════════════════════════════════════


def test_create_protocol_returns_its_steps_and_the_stored_etags(fake):
    payload = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01"})
    pid = payload["entity"]["id"]
    stored = fake.peek(f"protocols/{pid}")
    assert stored["status"] == "actif" and stored["created_via"] == "mcp"
    assert payload["entity"]["etag"] == stored["etag"]
    assert payload["entity"]["dossier_id"] == "d1"
    assert payload["entity"]["label"] == "Protocole de l'instance"
    assert len(payload["steps"]) == len(protocol_model.CQ_TEMPLATE_STEPS)
    for row in payload["steps"]:
        disk = _step_doc(fake, row["id"], pid)
        assert row["etag"] == disk["etag"]
        assert row["deadline_date"] == disk["deadline_date"].date().isoformat()
        assert row["phase"] == disk["phase"] and row["phase"]
        assert row["mandatory"] is True and row["deadline_locked"] is True
        assert row["linked_task_id"] is None
    # No task unless asked — so nothing for the phone.
    assert fake.peek_collection("tasks") == {}
    assert payload["tasks_created"] == 0
    assert payload["ctag_bumped"] is False and _ctag(fake) == "c0"


def test_create_protocol_with_linked_tasks_links_them_and_bumps(fake):
    payload = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01", "create_linked_tasks": True})
    pid = payload["entity"]["id"]
    n = len(protocol_model.CQ_TEMPLATE_STEPS)
    assert payload["tasks_created"] == payload["tasks_linked"] == n
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is True
    assert _ctag(fake) != "c0"
    tasks = fake.peek_collection("tasks")
    assert len(tasks) == n
    for row in payload["steps"]:
        disk = _step_doc(fake, row["id"], pid)
        assert row["linked_task_id"] == disk["linked_task_id"] in {
            t["id"] for t in tasks.values()}
        assert row["etag"] == disk["etag"]      # the link's etag, not before
    assert payload["entity"]["etag"] == fake.peek(f"protocols/{pid}")["etag"]
    assert all(t["created_via"] == "mcp" for t in tasks.values())


def test_create_protocol_refuses_a_second_active_one(fake):
    _protocol(fake)
    before = _snapshot(fake)
    err = _refused(handlers.create_protocol, {
        "dossier_id": "d1", "protocol_type": "conventionnel",
        "start_date": "2026-09-01"}, match="déjà un protocole actif")
    assert P in str(err) and "update_protocol" in str(err)
    assert _snapshot(fake) == before


def test_an_unreadable_active_check_refuses_instead_of_creating(
    fake, monkeypatch,
):
    server = fake._fake_server
    real = server.run_query

    def _run_query(request, *a, **kw):
        sq = request.get("structured_query") if hasattr(request, "get") else None
        froms = [f.collection_id for f in getattr(sq, "from_", [])] if sq else []
        if "protocols" in froms:
            raise RuntimeError("store unavailable")
        return real(request, *a, **kw)

    monkeypatch.setattr(server, "run_query", _run_query)
    before = _snapshot(fake)
    _refused(handlers.create_protocol, {
        "dossier_id": "d1", "protocol_type": "conventionnel",
        "start_date": "2026-09-01"}, match="Impossible de vérifier")
    assert _snapshot(fake) == before


def test_a_template_whose_regime_does_not_govern_the_court_is_named(fake):
    err = _refused(handlers.create_protocol, {
        "dossier_id": "d2", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01"})
    assert str(err).startswith("`protocol_type` refusé")
    assert "CS — Procédure ordinaire" in str(err)
    assert fake.peek_collection("protocols") == {}


def test_create_protocol_refuses_an_unknown_dossier(fake):
    _refused(handlers.create_protocol, {
        "dossier_id": "nope", "protocol_type": "conventionnel",
        "start_date": "2026-09-01"}, match="Dossier introuvable")


def test_a_cs_protocol_says_its_dates_are_suggestions(fake):
    payload = handlers.create_protocol({
        "dossier_id": "d2", "protocol_type": "cs_ordinaire",
        "start_date": "2026-09-01"})
    assert all(s["date_is_suggestion"] for s in payload["steps"]
               if s["deadline_offset_days"] is not None)
    assert any("SUGGESTIONS" in w for w in payload["warnings"])


def test_linked_tasks_on_a_closed_dossier_never_reach_the_phone(fake):
    payload = handlers.create_protocol({
        "dossier_id": "d3", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01", "create_linked_tasks": True})
    assert payload["ctag_bumped"] is True and payload["dav_synced"] is False
    assert any("fermé" in w for w in payload["warnings"])


def test_create_protocol_same_key_replays_without_a_second_protocol(fake):
    args = {"dossier_id": "d1", "protocol_type": "conventionnel",
            "start_date": "2026-09-01", "idempotency_key": "proto-key-0001"}
    first = handlers.create_protocol(dict(args))
    second = handlers.create_protocol(dict(args))
    assert second["idempotent_replay"] is True
    assert second["entity"]["id"] == first["entity"]["id"]
    assert len(fake.peek_collection("protocols")) == 1


# ══════════════════════════════════════════════════════════════════════
# 2. update_protocol
# ══════════════════════════════════════════════════════════════════════


def test_update_protocol_replaces_what_it_names(fake):
    _protocol(fake)
    payload = handlers.update_protocol({
        "protocol_id": P, "title": "Protocole révisé", "notes": "Suivi.",
        "expected_etag": f"pe-{P}"})
    stored = fake.peek(f"protocols/{P}")
    assert (stored["title"], stored["notes"]) == ("Protocole révisé", "Suivi.")
    assert stored["updated_via"] == "mcp"
    assert payload["entity"]["etag"] == stored["etag"] != f"pe-{P}"
    assert sorted(payload["changed_fields"]) == ["notes", "title"]
    assert payload["ctag_bumped"] is False            # no task moved
    assert payload["recompute"] == {"moved": [], "preserved": []}


def test_update_protocol_same_values_write_nothing_even_on_a_stale_etag(fake):
    _protocol(fake)
    fake.reset_logs()
    payload = handlers.update_protocol({
        "protocol_id": P, "title": "Protocole", "status": "actif",
        "expected_etag": "vieille"})
    assert payload["changed_fields"] == [] and fake.commits == []
    assert payload["entity"]["etag"] == f"pe-{P}"
    assert any("rien n'a été modifié" in w for w in payload["warnings"])


def test_update_protocol_refuses_a_stale_etag(fake):
    _protocol(fake)
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol, {
        "protocol_id": P, "notes": "x", "expected_etag": "vieille"})
    assert err.reason == "stale_etag" and "list_protocol_steps" in str(err)
    assert _snapshot(fake) == before


def test_a_new_start_date_reports_each_moved_and_kept_step(fake):
    created = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01", "create_linked_tasks": True})
    pid = created["entity"]["id"]
    first = created["steps"][0]
    done = handlers.update_protocol_step({
        "protocol_id": pid, "step_id": first["id"], "status": "complété"})
    assert done["changed_fields"] == ["status"]
    fake.seed(f"dav_sync/dossier:d1", {"ctag": "c1", "sync_token": "c1"})

    payload = handlers.update_protocol({
        "protocol_id": pid, "start_date": "2026-10-05"})
    moved = payload["recompute"]["moved"]
    assert len(moved) == len(created["steps"]) - 1
    assert payload["recompute"]["preserved"] == [{
        "step_id": first["id"], "title": first["title"],
        "reason": "completed"}]
    for entry in moved:
        disk = _step_doc(fake, entry["step_id"], pid)
        assert entry["to"] == disk["deadline_date"].date().isoformat()
        assert entry["from"] != entry["to"]
        assert entry["etag"] == disk["etag"]          # chainable
        assert entry["task_outcome"] == "aligned"
        task = fake.peek(f"tasks/{entry['linked_task_id']}")
        assert task["due_date"] == disk["deadline_date"]
    assert payload["linked_tasks"]["aligned"] == len(moved)
    assert payload["ctag_bumped"] is True and _ctag(fake) != "c1"
    assert payload["entity"]["date"] == "2026-10-05"


def test_suspending_stamps_the_connector_and_warns(fake):
    _protocol(fake)
    payload = handlers.update_protocol({"protocol_id": P,
                                        "status": "suspendu"})
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "suspendu" and stored["closed_by"] == "mcp"
    assert payload["entity"]["closed_by"] == "mcp"
    assert payload["entity"]["previous_status"] == "actif"
    assert any("get_agenda" in w for w in payload["warnings"])


def test_reactivation_beside_another_active_protocol_names_status(fake):
    _protocol(fake, "p-old", status="suspendu", closed_by="web")
    _protocol(fake, "p-new")
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol, {"protocol_id": "p-old",
                                              "status": "actif"})
    assert str(err).startswith("`status` « actif » refusé")
    assert "p-new" in str(err)
    assert _snapshot(fake) == before


def test_reactivation_clears_who_closed_it(fake):
    _protocol(fake, status="complété", closed_by="web")
    payload = handlers.update_protocol({"protocol_id": P, "status": "actif"})
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "actif" and stored["closed_by"] == ""
    assert any("de nouveau actif" in w for w in payload["warnings"])


@pytest.mark.parametrize("args, fragment", [
    ({"start_date": ""}, "« start_date » ne peut pas être vide"),
    ({"title": "   "}, "« title » ne peut pas être vide"),
    ({"notes": "si a < b et b > c"}, "chevrons"),
    ({}, "Aucun champ"),
])
def test_update_protocol_refusals_name_the_field(fake, args, fragment):
    _protocol(fake)
    _refused(handlers.update_protocol, {"protocol_id": P, **args},
             match=fragment)
    assert fake.peek(f"protocols/{P}")["etag"] == f"pe-{P}"


def test_an_unknown_or_unreadable_protocol_is_said_as_such(fake, monkeypatch):
    _refused(handlers.update_protocol, {"protocol_id": "nope", "notes": "x"},
             match="Protocole introuvable")
    _protocol(fake)
    server = fake._fake_server
    real = server.batch_get_documents

    def _flaky(request, *a, **k):
        docs = request.get("documents") if hasattr(request, "get") else []
        if any("/protocols/" in d for d in docs):
            raise RuntimeError("store down")
        return real(request, *a, **k)

    monkeypatch.setattr(server, "batch_get_documents", _flaky)
    err = _refused(handlers.update_protocol, {"protocol_id": P, "notes": "x"})
    assert "impossible" in str(err) and "introuvable" not in str(err)


# ══════════════════════════════════════════════════════════════════════
# 3. add_protocol_step
# ══════════════════════════════════════════════════════════════════════


def test_add_protocol_step_pins_a_custom_step(fake):
    _protocol(fake)
    _step(fake, "s1")
    payload = handlers.add_protocol_step({
        "protocol_id": P, "title": "Plaidoirie", "deadline_date": "2099-06-01",
        "sous_phase": "INS-01", "cpc_reference": "art. 268 C.p.c."})
    sid = payload["entity"]["id"]
    disk = _step_doc(fake, sid)
    assert (disk["mandatory"], disk["deadline_locked"], disk["order"]) == (
        False, False, 2)
    assert (disk["phase"], disk["sous_phase"]) == ("INS", "INS-01")
    assert disk["created_via"] == "mcp"
    assert payload["entity"]["etag"] == disk["etag"] == payload["step"]["etag"]
    assert payload["protocol_etag"] == fake.peek(f"protocols/{P}")["etag"]
    assert payload["entity"]["protocol_id"] == P
    assert payload["linked_task_id"] is None and payload["ctag_bumped"] is False


def test_add_protocol_step_with_its_task_links_and_bumps(fake):
    _protocol(fake)
    payload = handlers.add_protocol_step({
        "protocol_id": P, "title": "Expertise", "deadline_date": "2099-06-01",
        "sous_phase": "EXP-01", "create_linked_task": True})
    task_id = payload["linked_task_id"]
    task = fake.peek(f"tasks/{task_id}")
    assert task["title"] == "Expertise" and task["sous_phase"] == "EXP-01"
    assert task["due_date"].date().isoformat() == "2099-06-01"
    disk = _step_doc(fake, payload["entity"]["id"])
    assert disk["linked_task_id"] == task_id
    assert payload["entity"]["etag"] == disk["etag"]    # after the link
    assert payload["ctag_bumped"] is True and _ctag(fake) != "c0"


@pytest.mark.parametrize("status, closed_by", [
    ("suspendu", "web"), ("complété", "auto"),
])
def test_add_protocol_step_refuses_a_protocol_that_is_not_active(
    fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    before = _snapshot(fake)
    err = _refused(handlers.add_protocol_step,
                   {"protocol_id": P, "title": "Étape"})
    assert "update_protocol" in str(err) and "Rien n'a été écrit" in str(err)
    assert _snapshot(fake) == before


def test_a_protocol_closed_during_the_call_still_refuses_the_step(
    fake, monkeypatch,
):
    """The handler's own check read « actif »; the model re-reads in its
    transaction and refuses — the step never lands in a closed protocol."""
    _protocol(fake)
    real = protocol_model.get_protocol_strict

    def _racing(pid):
        proto = real(pid)
        fake.external_write(f"protocols/{P}", {
            **fake.peek(f"protocols/{P}"), "status": "suspendu",
            "closed_by": "web", "etag": "riv"})
        return proto

    monkeypatch.setattr(protocol_model, "get_protocol_strict", _racing)
    _refused(handlers.add_protocol_step, {"protocol_id": P, "title": "Étape"},
             match="update_protocol")
    assert fake.peek_collection(f"protocols/{P}/steps") == {}


def test_a_contradictory_phase_pair_is_refused(fake):
    _protocol(fake)
    _refused(handlers.add_protocol_step, {
        "protocol_id": P, "title": "Étape", "phase": "CTS",
        "sous_phase": "INS-01"}, match="n'appartient pas")


# ══════════════════════════════════════════════════════════════════════
# 4. update_protocol_step — les champs
# ══════════════════════════════════════════════════════════════════════


def test_update_protocol_step_replaces_fields_and_hands_back_both_etags(fake):
    _protocol(fake)
    _step(fake, "s1")
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "notes": "Suivi.",
        "sous_phase": "INT-01", "title": "Interrogatoire",
        "expected_etag": "se-s1"})
    disk = _step_doc(fake, "s1")
    assert (disk["notes"], disk["title"], disk["sous_phase"]) == (
        "Suivi.", "Interrogatoire", "INT-01")
    assert disk["updated_via"] == "mcp"
    assert payload["entity"]["etag"] == disk["etag"] != "se-s1"
    assert payload["protocol_etag"] == fake.peek(f"protocols/{P}")["etag"]
    assert sorted(payload["changed_fields"]) == [
        "notes", "phase", "sous_phase", "title"]
    assert payload["status_change"]["requested"] is False
    assert payload["linked_task"]["outcome"] == "none"


@pytest.mark.parametrize("field, value, over", [
    ("title", "Autre titre", {"mandatory": True}),
    ("description", "Autre texte", {"mandatory": True}),
    ("cpc_reference", "art. 1", {"mandatory": True}),
    ("deadline_date", "2099-01-15",
     {"mandatory": True, "deadline_locked": True}),
    ("deadline_date", "", {"mandatory": True}),
])
def test_the_cpc_locks_refuse_naming_the_field(fake, field, value, over):
    _protocol(fake, ptype="cq_simplifié")
    _step(fake, "s1", deadline_offset_days=30, **over)
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol_step,
                   {"protocol_id": P, "step_id": "s1", field: value})
    assert str(err).startswith(f"`{field}` refusé")
    assert _snapshot(fake) == before


def test_a_locked_step_still_takes_its_notes(fake):
    _protocol(fake, ptype="cq_simplifié")
    _step(fake, "s1", mandatory=True, deadline_locked=True)
    handlers.update_protocol_step({"protocol_id": P, "step_id": "s1",
                                   "notes": "Délai de rigueur."})
    assert _step_doc(fake, "s1")["notes"] == "Délai de rigueur."


def test_update_protocol_step_refuses_a_stale_etag(fake):
    _protocol(fake)
    _step(fake, "s1")
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "notes": "x",
        "expected_etag": "vieille"})
    assert err.reason == "stale_etag"
    assert "list_protocol_steps" in str(err) and "get_agenda" in str(err)
    assert _snapshot(fake) == before


def test_update_protocol_step_same_values_write_nothing(fake):
    _protocol(fake)
    _step(fake, "s1", notes="Suivi.")
    fake.reset_logs()
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "notes": "Suivi.",
        "deadline_date": "2099-12-01", "expected_etag": "vieille"})
    assert payload["changed_fields"] == [] and fake.commits == []
    assert payload["entity"]["etag"] == "se-s1"


def test_a_new_deadline_carries_its_task_and_confirms_a_cs_date(fake):
    _protocol(fake, ptype="cs_ordinaire")
    _step(fake, "s1", task="t1", deadline=OLD, mandatory=True,
          deadline_offset_days=30, date_confirmed=False)
    _task(fake, "t1", due=OLD)
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "deadline_date": "2099-11-20"})
    task = fake.peek("tasks/t1")
    assert task["due_date"].date().isoformat() == "2099-11-20"
    assert payload["linked_task"] == {"task_id": "t1", "outcome": "aligned",
                                      "ctag_bumped": True}
    assert payload["ctag_bumped"] is True and _ctag(fake) != "c0"
    assert payload["date_confirmed_now"] is True
    assert _step_doc(fake, "s1")["date_confirmed"] is True


def test_a_task_moved_by_hand_stays_and_is_said(fake):
    _protocol(fake)
    _step(fake, "s1", task="t1", deadline=OLD)
    _task(fake, "t1", due=datetime(2099, 11, 9, tzinfo=UTC))
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "deadline_date": "2099-11-20"})
    assert payload["linked_task"]["outcome"] == "diverged"
    assert payload["ctag_bumped"] is False
    assert any("propre échéance" in w for w in payload["warnings"])


def test_step_fields_are_refused_on_a_protocol_that_is_not_active(fake):
    _protocol(fake, status="suspendu", closed_by="web")
    _step(fake, "s1")
    before = _snapshot(fake)
    _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "notes": "x"},
        match="update_protocol")
    assert _snapshot(fake) == before


def test_status_and_fields_never_share_a_call(fake):
    _protocol(fake)
    _step(fake, "s1")
    before = _snapshot(fake)
    _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "notes": "x",
        "status": "complété"}, match="ne se combine pas")
    assert _snapshot(fake) == before


def test_an_unknown_step_is_refused(fake):
    _protocol(fake)
    _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "nope", "notes": "x"},
        match="Étape introuvable")


# ══════════════════════════════════════════════════════════════════════
# 5. update_protocol_step — le statut, cible et jamais bascule
# ══════════════════════════════════════════════════════════════════════


def test_completing_the_last_step_closes_the_protocol_and_says_so(fake):
    """The cascade, RE-READ after the write: the linked task is done, and
    the whole protocol closed with closed_by « auto »."""
    _protocol(fake)
    _step(fake, "s1", status="complété")
    _step(fake, "s2", task="t2", order=2)
    _task(fake, "t2")
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s2", "status": "complété",
        "expected_etag": "se-s2"})
    change = payload["status_change"]
    assert change["requested"] is True
    assert (change["step_status_before"], change["step_status_after"]) == (
        "à_venir", "complété")
    assert (change["task_id"], change["task_status_before"],
            change["task_status_after"], change["task_sync"]) == (
        "t2", "à_faire", "terminée", "synced")
    assert change["protocol_closed"] is True
    assert (change["protocol_status_before"],
            change["protocol_status_after"]) == ("actif", "complété")
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "complété" and stored["closed_by"] == "auto"
    assert payload["protocol_etag"] == stored["etag"]
    assert payload["entity"]["etag"] == _step_doc(fake, "s2")["etag"]
    assert payload["ctag_bumped"] is True and _ctag(fake) != "c0"
    assert any("PROTOCOLE ENTIER" in w for w in payload["warnings"])


@pytest.mark.parametrize("stored, target", [
    ("complété", "complété"), ("à_venir", "à_venir"),
    ("en_retard", "à_venir"), ("en_cours", "à_venir"),
])
def test_the_state_it_already_has_writes_nothing(fake, stored, target):
    _protocol(fake, status="suspendu", closed_by="web")  # no open protocol needed
    _step(fake, "s1", status=stored)
    fake.reset_logs()
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "status": target,
        "expected_etag": "vieille"})
    assert payload["changed_fields"] == [] and fake.commits == []
    change = payload["status_change"]
    assert change["requested"] is True
    assert change["step_status_before"] == change["step_status_after"] == stored
    assert change["task_sync"] == "none" and change["protocol_closed"] is False
    assert payload["ctag_bumped"] is False
    assert any("ne change rien" in w for w in payload["warnings"])


def test_reopening_a_step_of_an_auto_closed_protocol_reactivates_it(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "status": "à_venir"})
    change = payload["status_change"]
    assert change["protocol_reopened"] is True
    assert change["protocol_status_after"] == "actif"
    assert change["task_status_after"] == "à_faire"
    assert fake.peek(f"protocols/{P}")["closed_by"] == ""
    assert any("de nouveau actif" in w for w in payload["warnings"])


def test_a_reopen_blocked_by_another_active_protocol_writes_nothing(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _protocol(fake, "p2")
    _step(fake, "s1", status="complété")
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "status": "à_venir"})
    assert str(err).startswith("`status` refusé")
    assert _snapshot(fake) == before


def test_completing_a_step_of_a_suspended_protocol_is_refused(fake):
    _protocol(fake, status="suspendu", closed_by="web")
    _step(fake, "s1")
    before = _snapshot(fake)
    _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "status": "complété"},
        match="update_protocol")
    assert _snapshot(fake) == before


def test_a_cancelled_linked_task_is_left_alone_and_said(fake):
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", status="annulée")
    payload = handlers.update_protocol_step({
        "protocol_id": P, "step_id": "s1", "status": "complété"})
    assert payload["status_change"]["task_sync"] == "skipped_cancelled"
    assert fake.peek("tasks/t1")["status"] == "annulée"
    assert payload["ctag_bumped"] is False
    assert any("annulée" in w for w in payload["warnings"])


def test_a_stale_etag_refuses_the_status_change(fake):
    _protocol(fake)
    _step(fake, "s1")
    before = _snapshot(fake)
    err = _refused(handlers.update_protocol_step, {
        "protocol_id": P, "step_id": "s1", "status": "complété",
        "expected_etag": "vieille"})
    assert err.reason == "stale_etag"
    assert _snapshot(fake) == before


def test_the_status_path_never_calls_the_toggle():
    import ast
    import inspect

    source = inspect.getsource(handlers)
    names = {n.attr for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Attribute)}
    assert "complete_step" not in names


# ══════════════════════════════════════════════════════════════════════
# 6. list_protocol_steps : ce qu'il faut pour modifier
# ══════════════════════════════════════════════════════════════════════


def test_the_read_rows_carry_what_the_edits_need(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", sous_phase="INT-01", phase="INT",
          deadline_offset_days=30)
    payload = handlers.list_protocol_steps({"dossier_id": "d1",
                                            "include_history": True})
    (proto,) = payload["protocols"]
    assert proto["etag"] == f"pe-{P}"
    assert proto["closed_by"] == "auto" and proto["closed_at"]
    (row,) = proto["steps"]
    assert row["etag"] == "se-s1"
    assert (row["phase"], row["sous_phase"]) == ("INT", "INT-01")
    assert row["phase_label"] and row["sous_phase_label"]
    assert row["deadline_offset_days"] == 30
