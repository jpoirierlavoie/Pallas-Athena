"""Les éditions de l'agenda par le connecteur (lot 1b, étape L5).

Quatre outils — ``update_task``, ``reopen_task``, ``update_note``,
``edit_analyse`` — et deux enrichissements de lecture (``get_note`` rend la
structure de la théorie de la cause, ``get_dossier`` son identifiant). Tout
passe ici par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), les vrais modèles, les vrais gestionnaires
et le vrai protocole d'écriture (``run_write``) : un faux qui accepterait
ce que le magasin refuse ne prouverait rien.

On épingle, pour chaque outil :
* ce qu'il écrit, et ce qu'il n'écrit PAS (un appel dont chaque valeur est
  déjà stockée n'écrit rien, ne bumpe rien) ;
* le refus d'un etag périmé, rien d'écrit ;
* le rejeu d'une même clé d'idempotence ;
* la chorégraphie DAV d'un déplacement (pierre tombale dans l'ancienne
  collection, bump des deux) ;
* les invariants de la théorie de la cause (huit titres, un bloc à la fois,
  révision conservée, jamais de guess) ;
* la cascade de réouverture — et son refus AVANT écriture quand l'étape ne
  peut pas suivre (la règle elle-même, au modèle, est épinglée par
  ``tests/test_task_reopen_follow.py``).
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
    from models import concurrency
    from models import dossier as dossier_model  # noqa: F401
    from models import note as note_model
    from models import revision as revision_model
    from models import task as task_model

from security import sanitize  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from utils import analyse_blocs  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
P = "p1"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for did, num, status in (("d1", "2026-001", "actif"),
                             ("d2", "2026-002", "actif"),
                             ("d3", "2026-003", "fermé")):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": num,
                                      "title": f"Dossier {num}",
                                      "status": status})
        fake.seed(f"dav_sync/dossier:{did}", {"ctag": "c0", "sync_token": "c0",
                                             "updated_at": WHEN})
    fake.seed("dav_sync/general", {"ctag": "g0", "sync_token": "g0",
                                   "updated_at": WHEN})
    return fake


def _ctag(fake, did) -> str:
    name = f"dossier:{did}" if did else "general"
    return fake.peek(f"dav_sync/{name}")["ctag"]


def _task(fake, tid="t1", *, status="à_faire", dossier_id="d1", **over):
    doc = {
        "id": tid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Dossier 2026-001" if dossier_id else "",
        "title": f"Tâche {tid}", "description": "Texte initial.",
        "priority": "normale", "status": status, "due_date": None,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(f"tasks/{tid}", doc)


def _note(fake, nid="n1", *, dossier_id="d1", content="Premier jet.", **over):
    doc = {
        "id": nid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Dossier 2026-001" if dossier_id else "",
        "title": "Recherche", "content": content, "category": "recherche",
        "pinned": False, "dateless": False, "is_analyse": False,
        "vjournal_uid": f"v-{nid}", "etag": f"e-{nid}",
        "created_at": WHEN, "updated_at": WHEN,
    }
    doc.update(over)
    fake.seed(f"notes/{nid}", doc)


def _protocol(fake, pid=P, *, status="actif", closed_by=None,
              dossier_id="d1"):
    doc = {
        "id": pid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "Dossier 2026-001", "title": "Protocole",
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


def _analyse(fake, did="d1") -> dict:
    note, errors, created = note_model.ensure_analyse_note(did)
    assert errors == [] and created, errors
    return note


def _writes_to(fake, path) -> list:
    return [c for c in fake.commits if any(p == path for _k, p in c.ops)]


def _today_stamp(kind) -> str:
    return handlers._stamp_line(kind, handlers._today_mtl())


# ══════════════════════════════════════════════════════════════════════
# 2. update_task
# ══════════════════════════════════════════════════════════════════════


def test_update_task_replaces_what_it_names_and_hands_back_the_etag(fake):
    _task(fake, "t1")
    payload = handlers.update_task({
        "task_id": "t1", "title": "Produire la réponse",
        "description": "Nouveau texte.", "priority": "haute",
        "due_date": "2026-10-15", "sous_phase": "CTS-02",
        "expected_etag": "e-t1",
    })
    stored = fake.peek("tasks/t1")
    assert stored["title"] == "Produire la réponse"
    assert stored["description"] == "Nouveau texte."
    assert stored["priority"] == "haute"
    assert stored["due_date"].date().isoformat() == "2026-10-15"
    assert (stored["phase"], stored["sous_phase"]) == ("CTS", "CTS-02")
    assert stored["category"] == "suivi"          # omitted → untouched
    assert stored["status"] == "à_faire"          # never through here
    assert stored["updated_via"] == "mcp"
    assert payload["entity"]["etag"] == stored["etag"] != "e-t1"
    assert sorted(payload["changed_fields"]) == [
        "description", "due_date", "phase", "priority", "sous_phase", "title"]
    assert payload["moved"] is False
    assert payload["ctag_bumped"] is True and _ctag(fake, "d1") != "c0"
    assert payload["previous_collection_cleared"] is True


def test_update_task_same_values_write_nothing_even_on_a_stale_etag(fake):
    """A request that changes nothing needs no current version, and must
    not wake the phone: no commit, no CTag."""
    _task(fake, "t1", priority="haute")
    fake.reset_logs()
    payload = handlers.update_task({
        "task_id": "t1", "priority": "haute", "title": "Tâche t1",
        "expected_etag": "une-vieille-version",
    })
    assert payload["changed_fields"] == []
    assert payload["ctag_bumped"] is False and payload["dav_synced"] is False
    assert fake.commits == []
    assert _ctag(fake, "d1") == "c0"
    assert payload["entity"]["etag"] == "e-t1"
    assert any("rien n'a été modifié" in w for w in payload["warnings"])


def test_update_task_refuses_a_stale_etag(fake):
    _task(fake, "t1")
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_task({"task_id": "t1", "title": "X",
                              "expected_etag": "vieille"})
    assert excinfo.value.reason == "stale_etag"
    assert "list_tasks" in str(excinfo.value)
    assert fake.peek("tasks/t1")["title"] == "Tâche t1"


def test_update_task_moves_with_the_relocation_choreography(fake):
    _task(fake, "t1")
    fake.seed("dav_sync/dossier:d2/tombstones/t1", {"sync_token": "x"})
    payload = handlers.update_task({"task_id": "t1", "dossier_id": "d2"})
    stored = fake.peek("tasks/t1")
    assert stored["dossier_id"] == "d2"
    assert stored["dossier_file_number"] == "2026-002"   # re-snapshotted
    assert stored["dossier_title"] == "Dossier 2026-002"
    assert fake.peek("dav_sync/dossier:d1/tombstones/t1") is not None
    assert fake.peek("dav_sync/dossier:d2/tombstones/t1") is None
    assert _ctag(fake, "d1") != "c0" and _ctag(fake, "d2") != "c0"
    assert payload["moved"] is True and payload["changed_fields"] == ["dossier_id"]
    assert payload["previous_collection_cleared"] is True
    assert payload["entity"]["dossier_id"] == "d2"


def test_update_task_moves_to_general_storing_none(fake):
    _task(fake, "t1")
    payload = handlers.update_task({"task_id": "t1", "dossier_id": ""})
    stored = fake.peek("tasks/t1")
    assert stored["dossier_id"] is None               # the tasks convention
    assert stored["dossier_file_number"] == "" and stored["dossier_title"] == ""
    assert fake.peek("dav_sync/dossier:d1/tombstones/t1") is not None
    assert _ctag(fake, "") != "g0"
    assert payload["entity"]["dossier_id"] == ""


def test_update_task_to_a_closed_dossier_warns_it_never_reaches_the_phone(fake):
    _task(fake, "t1")
    payload = handlers.update_task({"task_id": "t1", "dossier_id": "d3"})
    assert payload["dav_synced"] is False
    assert any("fermé" in w for w in payload["warnings"])


def test_update_task_refuses_an_unknown_dossier_never_downgrades(fake):
    _task(fake, "t1")
    with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
        handlers.update_task({"task_id": "t1", "dossier_id": "nope"})
    assert fake.peek("tasks/t1")["dossier_id"] == "d1"


def test_update_task_never_moves_a_step_linked_task(fake):
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_task({"task_id": "t1", "dossier_id": "d2"})
    assert str(excinfo.value).startswith("`dossier_id` refusé")
    assert fake.peek("tasks/t1")["dossier_id"] == "d1"
    assert fake.peek("dav_sync/dossier:d1/tombstones/t1") is None


def test_update_task_warns_that_a_linked_step_keeps_its_deadline(fake):
    _protocol(fake)
    _step(fake, "s1", task="t1")
    _task(fake, "t1")
    payload = handlers.update_task({"task_id": "t1", "due_date": "2026-11-02"})
    assert any("l'échéance de l'étape, elle, ne change pas" in w
               for w in payload["warnings"])


@pytest.mark.parametrize("args, fragment", [
    ({"description": "x" * 2001}, "2000"),
    ({"description": "si a < b et b > c"}, "chevrons"),
    ({"title": "   "}, "vide"),
    ({"category": "inconnue"}, "« category »"),
    ({"priority": ""}, "« priority »"),
    ({}, "Aucun champ"),
])
def test_update_task_refusals_name_the_field(fake, args, fragment):
    _task(fake, "t1")
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.update_task({"task_id": "t1", **args})
    assert fake.peek("tasks/t1")["etag"] == "e-t1"


@pytest.mark.parametrize("tool, args, collection", [
    ("update_task", {"task_id": "t1", "title": "X"}, "tasks"),
    ("reopen_task", {"task_id": "t1"}, "tasks"),
    ("update_note", {"note_id": "n1", "title": "X"}, "notes"),
])
def test_an_unreadable_record_is_never_called_missing(
    fake, monkeypatch, tool, args, collection,
):
    """The fail-open getters answer None on a read error; « introuvable »
    about a record that exists would send the caller hunting for an id that
    was right. The edit tools read strictly and say what happened."""
    _task(fake, "t1", status="terminée")
    _note(fake, "n1")
    server = fake._fake_server
    real = server.batch_get_documents

    def _flaky(request, *a, **k):
        docs = request.get("documents") if hasattr(request, "get") else []
        if any(f"/{collection}/" in d for d in docs):
            raise RuntimeError("store down")
        return real(request, *a, **k)

    monkeypatch.setattr(server, "batch_get_documents", _flaky)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))
    assert "impossible" in str(excinfo.value)
    assert "introuvable" not in str(excinfo.value)


def test_update_task_schema_refuses_a_status():
    errors = tools.validate_args(
        tools.TOOLS["update_task"]["input_schema"],
        {"task_id": "t1", "status": "terminée"})
    assert any("status" in e for e in errors)


def test_update_task_same_key_replays_without_writing_again(fake):
    _task(fake, "t1")
    args = {"task_id": "t1", "title": "Une fois", "idempotency_key": "clé-maj-001"}
    first = handlers.update_task(dict(args))
    n = len(_writes_to(fake, "tasks/t1"))
    second = handlers.update_task(dict(args))
    assert second["idempotent_replay"] is True
    assert second["entity"]["etag"] == first["entity"]["etag"]
    assert len(_writes_to(fake, "tasks/t1")) == n


# ══════════════════════════════════════════════════════════════════════
# 3. reopen_task
# ══════════════════════════════════════════════════════════════════════


def test_reopen_task_reopens_and_clears_the_completion(fake):
    _task(fake, "t1", status="terminée")
    payload = handlers.reopen_task({"task_id": "t1", "expected_etag": "e-t1"})
    stored = fake.peek("tasks/t1")
    assert stored["status"] == "à_faire"
    assert stored["completed_date"] is None
    assert stored["updated_via"] == "mcp"
    assert payload["reopened"] is True and payload["already_open"] is False
    assert payload["entity"]["previous_status"] == "terminée"
    assert payload["entity"]["etag"] == stored["etag"]
    assert payload["ctag_bumped"] is True and _ctag(fake, "d1") != "c0"
    effect = payload["protocol_step_effect"]
    assert effect["checked"] is True and effect["linked_step_found"] is False


def test_reopen_task_cascades_into_an_auto_closed_protocol(fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _step(fake, "s2", status="complété", order=2)
    _task(fake, "t1", status="terminée")
    payload = handlers.reopen_task({"task_id": "t1"})
    effect = payload["protocol_step_effect"]
    assert effect["step_status_before"] == "complété"
    assert effect["step_status_after"] == "à_venir"      # RE-READ
    assert effect["protocol_status_before"] == "complété"
    assert effect["protocol_status_after"] == "actif"
    assert effect["protocol_reopened"] is True
    assert any("de nouveau « actif »" in w for w in payload["warnings"])


def test_reopen_task_says_when_the_cascade_could_not_be_reread(
    fake, monkeypatch,
):
    """get_protocol answers None on a read error: the report must say it
    could not look, never hand back protocol_reopened: false beside empty
    statuses — a reader takes that for « the protocol stayed closed » while
    the cascade reopened it (revue L5)."""
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    monkeypatch.setattr(handlers.protocol_model, "get_protocol",
                        lambda protocol_id: None)
    effect = handlers.reopen_task({"task_id": "t1"})["protocol_step_effect"]
    assert fake.peek(f"protocols/{P}")["status"] == "actif"   # it DID reopen
    assert effect["linked_step_found"] is True
    assert effect["step_status_after"] == ""
    assert "n'a pas pu être relu" in effect["note"]


@pytest.mark.parametrize("closed_by, other", [("web", False), ("auto", True)])
def test_reopen_task_refuses_when_the_step_cannot_follow(fake, closed_by, other):
    _protocol(fake, status="complété", closed_by=closed_by)
    if other:
        _protocol(fake, "p2")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    fake.reset_logs()
    with pytest.raises(tools.ToolArgumentError, match="Rouvrir cette tâche"):
        handlers.reopen_task({"task_id": "t1"})
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert _writes_to(fake, "tasks/t1") == []
    assert _ctag(fake, "d1") == "c0"


def test_reopen_task_never_uncancels_without_the_explicit_flag(fake):
    """Backs the disclosure never « uncancel »: a cancellation is a
    decision, undone only when asked for in so many words."""
    _task(fake, "t1", status="annulée")
    with pytest.raises(tools.ToolArgumentError, match="reopen_cancelled"):
        handlers.reopen_task({"task_id": "t1"})
    with pytest.raises(tools.ToolArgumentError, match="reopen_cancelled"):
        handlers.reopen_task({"task_id": "t1", "reopen_cancelled": False})
    assert fake.peek("tasks/t1")["status"] == "annulée"
    payload = handlers.reopen_task({"task_id": "t1", "reopen_cancelled": True})
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert payload["entity"]["previous_status"] == "annulée"


def test_reopen_task_same_state_writes_nothing(fake):
    _task(fake, "t1", status="en_cours")
    fake.reset_logs()
    payload = handlers.reopen_task({"task_id": "t1", "status": "en_cours"})
    assert payload["already_open"] is True
    assert payload["ctag_bumped"] is False
    assert fake.commits == []


def test_reopen_task_puts_an_in_progress_task_back_to_do(fake):
    _task(fake, "t1", status="en_cours")
    handlers.reopen_task({"task_id": "t1"})
    assert fake.peek("tasks/t1")["status"] == "à_faire"


def test_reopen_task_points_an_open_task_to_complete_task(fake):
    _task(fake, "t1", status="à_faire")
    with pytest.raises(tools.ToolArgumentError, match="complete_task"):
        handlers.reopen_task({"task_id": "t1", "status": "en_cours"})


def test_reopen_task_refuses_a_stale_etag(fake):
    _task(fake, "t1", status="terminée")
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.reopen_task({"task_id": "t1", "expected_etag": "vieille"})
    assert excinfo.value.reason == "stale_etag"
    assert fake.peek("tasks/t1")["status"] == "terminée"


def test_reopen_task_never_calls_the_toggle():
    """toggle_task_complete sends annulée AND terminée back to à_faire —
    the silent un-cancel the disclosure never forbids. Named in no handler
    (the disclosure sweep reads the syntax tree; this pins the tool)."""
    import inspect

    assert "toggle_task_complete" not in inspect.getsource(
        handlers._reopen_task_impl)


# ══════════════════════════════════════════════════════════════════════
# 4. update_note
# ══════════════════════════════════════════════════════════════════════


def test_update_note_content_demands_the_etag(fake):
    _note(fake)
    with pytest.raises(tools.ToolArgumentError, match="expected_etag"):
        handlers.update_note({"note_id": "n1", "content": "Autre texte."})
    assert fake.peek("notes/n1")["content"] == "Premier jet."


def test_update_note_replaces_the_body_and_keeps_a_revision(fake):
    _note(fake)
    payload = handlers.update_note({
        "note_id": "n1", "content": "Texte révisé.", "expected_etag": "e-n1"})
    stored = fake.peek("notes/n1")
    stamp = _today_stamp(handlers._NOTE_REVISION_STAMP)
    assert stored["content"] == f"{stamp}\n\nTexte révisé."
    assert sanitize(stored["content"], max_length=note_model.CONTENT_MAX_LENGTH) \
        == stored["content"]
    revisions = fake.peek_collection("notes/n1/revisions")
    (rev,) = revisions.values()
    assert rev["previous_value"] == "Premier jet."
    assert rev["field"] == "content" and rev["via"] == "mcp"
    assert rev["tool"] == "update_note"
    assert rev["new_etag"] == stored["etag"]
    assert payload["revision_id"] == rev["id"]
    assert payload["entity"]["content_length"] == len(stored["content"])
    assert payload["entity"]["etag"] == stored["etag"]
    assert payload["ctag_bumped"] is True


def test_update_note_does_not_stack_stamps_and_a_same_day_resend_is_a_noop(fake):
    _note(fake)
    first = handlers.update_note({
        "note_id": "n1", "content": "Texte révisé.", "expected_etag": "e-n1"})
    stored = fake.peek("notes/n1")["content"]
    fake.reset_logs()
    # The body read back WITH its stamp, re-sent unchanged.
    second = handlers.update_note({
        "note_id": "n1", "content": stored,
        "expected_etag": first["entity"]["etag"]})
    assert second["changed_fields"] == []
    assert fake.commits == []
    assert fake.peek("notes/n1")["content"].count("Révisée par Claude") == 1


def test_update_note_title_without_etag_guards_its_own_read(fake):
    _note(fake)
    handlers.update_note({"note_id": "n1", "title": "Nouveau titre",
                          "pinned": True})
    stored = fake.peek("notes/n1")
    assert stored["title"] == "Nouveau titre" and stored["pinned"] is True
    assert fake.peek_collection("notes/n1/revisions") == {}


@pytest.mark.parametrize("content, fragment", [
    ("si a < b et b > c", "chevrons"),
    ("x" * (note_model.CONTENT_MAX_LENGTH - 10), "plafond"),
], ids=["chevrons", "trop-long"])
def test_update_note_refuses_rather_than_losing_text(fake, content, fragment):
    _note(fake)
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.update_note({"note_id": "n1", "content": content,
                              "expected_etag": "e-n1"})
    assert fake.peek("notes/n1")["content"] == "Premier jet."


def test_update_note_keeps_a_lone_angle_bracket(fake):
    """Only a « <…> » SPAN is refused — the sanitizer deletes exactly that.
    A lone « < » is stored intact, so the description must not say that
    unpaired brackets are refused (it did; revue L5)."""
    _note(fake)
    handlers.update_note({"note_id": "n1", "expected_etag": "e-n1",
                          "content": "Si a < b, la clause s'applique."})
    assert fake.peek("notes/n1")["content"].endswith(
        "Si a < b, la clause s'applique.")
    assert "unpaired" not in tools.TOOLS["update_note"]["description"]


def test_update_note_converts_autolinks(fake):
    _note(fake)
    handlers.update_note({"note_id": "n1", "expected_etag": "e-n1",
                          "content": "Voir <https://canlii.ca/t/abc>."})
    assert "[https://canlii.ca/t/abc](https://canlii.ca/t/abc)" in \
        fake.peek("notes/n1")["content"]


def test_update_note_refuses_the_analyse_note_on_every_field(fake):
    note = _analyse(fake)
    for args in ({"title": "Autre"}, {"dossier_id": "d2"}, {"pinned": True},
                 {"content": "x", "expected_etag": note["etag"]}):
        with pytest.raises(tools.ToolArgumentError, match="edit_analyse"):
            handlers.update_note({"note_id": note["id"], **args})
    assert fake.peek(f"notes/{note['id']}")["dossier_id"] == "d1"


def test_update_note_moves_and_reports_the_tasks_left_behind(fake):
    _note(fake)
    _task(fake, "t1", related_note_id="n1")
    payload = handlers.update_note({"note_id": "n1", "dossier_id": ""})
    stored = fake.peek("notes/n1")
    assert stored["dossier_id"] == ""                 # the notes convention
    assert stored["dossier_file_number"] == ""
    assert fake.peek("dav_sync/dossier:d1/tombstones/n1") is not None
    assert _ctag(fake, "d1") != "c0" and _ctag(fake, "") != "g0"
    assert payload["moved"] is True
    assert payload["linked_tasks_left_behind"] == 1
    assert any("jtx Board" in w for w in payload["warnings"])


def test_update_note_refuses_a_stale_etag_naming_its_readers(fake):
    _note(fake)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_note({"note_id": "n1", "title": "X",
                              "expected_etag": "vieille"})
    assert excinfo.value.reason == "stale_etag"
    assert "get_note" in str(excinfo.value) and "list_notes" in str(excinfo.value)


def test_update_note_same_key_replay(fake):
    _note(fake)
    args = {"note_id": "n1", "content": "Une fois.", "expected_etag": "e-n1",
            "idempotency_key": "clé-note-0001"}
    handlers.update_note(dict(args))
    n = len(_writes_to(fake, "notes/n1"))
    second = handlers.update_note(dict(args))
    assert second["idempotent_replay"] is True
    assert len(_writes_to(fake, "notes/n1")) == n
    assert len(fake.peek_collection("notes/n1/revisions")) == 1


def test_update_note_input_ceiling_leaves_room_for_the_stamp():
    prop = tools.TOOLS["update_note"]["input_schema"]["properties"]["content"]
    stamp = handlers._stamp_line(handlers._NOTE_REVISION_STAMP,
                                 datetime(2026, 9, 30).date())
    assert prop["maxLength"] + len(stamp) + 2 < note_model.CONTENT_MAX_LENGTH
    full = tools.TOOLS["edit_analyse"]["input_schema"]["properties"]["full"]
    assert full["maxLength"] < note_model.CONTENT_MAX_LENGTH


# ══════════════════════════════════════════════════════════════════════
# 5. edit_analyse
# ══════════════════════════════════════════════════════════════════════


def _bloc_body(content: str, key: str) -> str:
    return analyse_blocs.parse(content).zone(key).body(content)


def test_edit_analyse_init_creates_once_then_finds(fake):
    first = handlers.edit_analyse({"dossier_id": "d1"})
    assert first["mode"] == "created" and first["ctag_bumped"] is True
    note = fake.peek(f"notes/{first['entity']['id']}")
    assert note["is_analyse"] is True and note["dateless"] is True
    assert note["created_via"] == "mcp"
    assert first["structure"]["ok"] is True
    assert all(b["is_seed"] for b in first["structure"]["blocs"])
    assert first["entity"]["etag"] == note["etag"]
    fake.reset_logs()
    second = handlers.edit_analyse({"dossier_id": "d1"})
    assert second["mode"] == "found" and second["ctag_bumped"] is False
    assert second["entity"]["id"] == first["entity"]["id"]
    assert fake.commits == []


def test_edit_analyse_operating_demands_the_etag(fake):
    note = _analyse(fake)
    with pytest.raises(tools.ToolArgumentError, match="expected_etag"):
        handlers.edit_analyse({"dossier_id": "d1", "operations": [
            {"bloc": "C", "mode": "append", "content": "Ajout."}]})
    assert fake.peek(f"notes/{note['id']}")["content"] == note["content"]


def test_edit_analyse_replaces_one_bloc_and_nothing_else(fake):
    note = _analyse(fake)
    before = note["content"]
    payload = handlers.edit_analyse({
        "dossier_id": "d1", "expected_etag": note["etag"],
        "operations": [{"bloc": "C", "mode": "replace",
                        "content": "Faute, préjudice, causalité."}]})
    stored = fake.peek(f"notes/{note['id']}")
    after = stored["content"]
    stamp = _today_stamp(handlers._BLOC_REVISION_STAMP)
    assert _bloc_body(after, "C").strip() == (
        f"{stamp}\n\nFaute, préjudice, causalité.")
    for key in ("entete", "A", "B", "D", "E", "F", "G", "H"):
        assert _bloc_body(after, key) == _bloc_body(before, key), key
    (rev,) = fake.peek_collection(f"notes/{note['id']}/revisions").values()
    assert rev["field"] == "bloc:C" and rev["previous_value"] == before
    assert payload["mode"] == "blocs" and payload["revision_id"] == rev["id"]
    assert payload["blocs_changed"] == [
        {"bloc": "C", "mode": "replace", "chars": 28}]
    assert payload["structure"]["ok"] is True
    blocs = {b["bloc"]: b for b in payload["structure"]["blocs"]}
    assert blocs["C"]["is_seed"] is False and blocs["D"]["is_seed"] is True
    assert stored["dateless"] is True and stored["is_analyse"] is True
    assert payload["ctag_bumped"] is True


def test_edit_analyse_appends_under_a_dated_line_never_a_separator(fake):
    note = _analyse(fake)
    handlers.edit_analyse({
        "dossier_id": "d1", "expected_etag": note["etag"],
        "operations": [{"bloc": "F", "mode": "append",
                        "content": "La preuve documentaire est mince."}]})
    body = _bloc_body(fake.peek(f"notes/{note['id']}")["content"], "F")
    assert body.rstrip().endswith(
        "*Ajouté par Claude le "
        + handlers.format_date_fr(handlers._today_mtl())
        + "*\n\nLa preuve documentaire est mince.")
    assert _bloc_body(note["content"], "F") in body   # kept byte for byte


def test_edit_analyse_empty_replace_keeps_only_the_revision_line(fake):
    """« replace » with "" empties the bloc's text but keeps the dated line
    saying a version was replaced (and kept) — the schema text says so
    instead of promising an empty bloc (revue L5)."""
    note = _analyse(fake)
    handlers.edit_analyse({"dossier_id": "d1", "expected_etag": note["etag"],
                           "operations": [{"bloc": "C", "mode": "replace",
                                           "content": ""}]})
    body = _bloc_body(fake.peek(f"notes/{note['id']}")["content"], "C")
    assert body.strip() == _today_stamp(handlers._BLOC_REVISION_STAMP)
    mode = (tools.TOOLS["edit_analyse"]["input_schema"]["properties"]
            ["operations"]["items"]["properties"]["mode"]["description"])
    assert "revision line" in mode


def test_edit_analyse_several_operations_make_one_revision(fake):
    note = _analyse(fake)
    payload = handlers.edit_analyse({
        "dossier_id": "d1", "expected_etag": note["etag"],
        "operations": [
            {"bloc": "A", "mode": "append", "content": "Un."},
            {"bloc": "H", "mode": "replace", "content": "Deux."}]})
    (rev,) = fake.peek_collection(f"notes/{note['id']}/revisions").values()
    assert rev["field"] == "content"
    assert [b["bloc"] for b in payload["blocs_changed"]] == ["A", "H"]


@pytest.mark.parametrize("operations, fragment", [
    ([{"bloc": "C", "mode": "append", "content": "a"},
      {"bloc": "C", "mode": "replace", "content": "b"}], "deux fois"),
    ([{"bloc": "C", "mode": "append", "content": "## Titre\nx"}], "titre"),
    ([{"bloc": "C", "mode": "append", "content": "```\ncode"}], "bloc de code"),
    ([{"bloc": "Z", "mode": "append", "content": "x"}], "operations"),
    ([{"bloc": "C", "mode": "edit", "content": "x"}], "operations"),
    ([{"bloc": "C", "mode": "append", "content": "a < b et c > d"}], "chevrons"),
])
def test_edit_analyse_refuses_rather_than_guesses(fake, operations, fragment):
    note = _analyse(fake)
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.edit_analyse({"dossier_id": "d1", "expected_etag": note["etag"],
                               "operations": operations})
    assert fake.peek(f"notes/{note['id']}")["content"] == note["content"]


def test_edit_analyse_refuses_on_a_missing_heading_naming_the_letter(fake):
    note = _analyse(fake)
    broken = note["content"].replace("## Bloc F", "## Forces", 1)
    fake.external_write(f"notes/{note['id']}", {
        **fake.peek(f"notes/{note['id']}"), "content": broken, "etag": "e-b"})
    with pytest.raises(tools.ToolArgumentError, match="F"):
        handlers.edit_analyse({"dossier_id": "d1", "expected_etag": "e-b",
                               "operations": [{"bloc": "E", "mode": "append",
                                               "content": "x"}]})
    assert fake.peek(f"notes/{note['id']}")["content"] == broken


def test_edit_analyse_join_guard_protects_stored_text(fake):
    """An unpaired « < » already in bloc B (legal from the web form) and a
    « > » appended in bloc C would make TAG_RE span the join and delete
    stored text — refused, not written."""
    note = _analyse(fake)
    content = note["content"].replace(
        "### Récit chronologique\n\n…", "### Récit chronologique\n\nsi a < b", 1)
    assert content != note["content"]
    fake.external_write(f"notes/{note['id']}", {
        **fake.peek(f"notes/{note['id']}"), "content": content, "etag": "e-j"})
    with pytest.raises(tools.ToolArgumentError, match="chevrons"):
        handlers.edit_analyse({"dossier_id": "d1", "expected_etag": "e-j",
                               "operations": [{"bloc": "C", "mode": "append",
                                               "content": "b > c"}]})
    assert fake.peek(f"notes/{note['id']}")["content"] == content


def test_edit_analyse_full_rewrite_keeps_the_eight_headings(fake):
    note = _analyse(fake)
    new = "# Théorie de la cause\n\nEntête.\n\n" + "\n\n".join(
        f"## Bloc {x} — Titre\n\nCorps {x}." for x in "ABCDEFGH")
    payload = handlers.edit_analyse({"dossier_id": "d1", "full": new,
                                     "expected_etag": note["etag"]})
    stored = fake.peek(f"notes/{note['id']}")["content"]
    stamp = _today_stamp(handlers._ANALYSE_REWRITE_STAMP)
    assert stored.startswith(f"# Théorie de la cause\n\n{stamp}\n\nEntête.")
    assert analyse_blocs.parse(stored).ok
    (rev,) = fake.peek_collection(f"notes/{note['id']}/revisions").values()
    assert rev["field"] == "content:rewrite"
    assert payload["mode"] == "full"


def test_edit_analyse_full_rewrite_missing_a_heading_is_refused(fake):
    note = _analyse(fake)
    new = "\n\n".join(f"## Bloc {x}\n\nCorps." for x in "ABCDEFG")
    with pytest.raises(tools.ToolArgumentError, match="H"):
        handlers.edit_analyse({"dossier_id": "d1", "full": new,
                               "expected_etag": note["etag"]})
    assert fake.peek(f"notes/{note['id']}")["content"] == note["content"]


def test_edit_analyse_refuses_operations_and_full_together(fake):
    note = _analyse(fake)
    with pytest.raises(tools.ToolArgumentError, match="jamais les deux"):
        handlers.edit_analyse({
            "dossier_id": "d1", "expected_etag": note["etag"], "full": "x",
            "operations": [{"bloc": "A", "mode": "append", "content": "x"}]})


def test_edit_analyse_refuses_a_stale_etag(fake):
    note = _analyse(fake)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.edit_analyse({"dossier_id": "d1", "expected_etag": "vieille",
                               "operations": [{"bloc": "A", "mode": "append",
                                               "content": "x"}]})
    assert excinfo.value.reason == "stale_etag"
    assert fake.peek(f"notes/{note['id']}")["content"] == note["content"]


def test_edit_analyse_without_a_note_points_to_the_init(fake):
    with pytest.raises(tools.ToolArgumentError, match="sans operations ni full"):
        handlers.edit_analyse({"dossier_id": "d1", "expected_etag": "",
                               "operations": [{"bloc": "A", "mode": "append",
                                               "content": "x"}]})


def test_edit_analyse_refuses_a_duplicated_theorie(fake):
    _analyse(fake)
    _note(fake, "n9", is_analyse=True, dateless=True)
    with pytest.raises(tools.ToolArgumentError, match="plusieurs"):
        handlers.edit_analyse({"dossier_id": "d1"})


def test_edit_analyse_same_day_identical_replace_is_a_noop(fake):
    note = _analyse(fake)
    op = [{"bloc": "B", "mode": "replace", "content": "Les faits."}]
    first = handlers.edit_analyse({"dossier_id": "d1", "operations": op,
                                   "expected_etag": note["etag"]})
    fake.reset_logs()
    second = handlers.edit_analyse({"dossier_id": "d1", "operations": op,
                                    "expected_etag": first["entity"]["etag"]})
    assert second["mode"] == "unchanged" and second["ctag_bumped"] is False
    assert fake.commits == []


def test_edit_analyse_same_key_replay(fake):
    note = _analyse(fake)
    args = {"dossier_id": "d1", "expected_etag": note["etag"],
            "idempotency_key": "clé-analyse-01",
            "operations": [{"bloc": "G", "mode": "append", "content": "Thème."}]}
    handlers.edit_analyse(dict(args))
    second = handlers.edit_analyse(dict(args))
    assert second["idempotent_replay"] is True
    assert len(fake.peek_collection(f"notes/{note['id']}/revisions")) == 1
    assert fake.peek(f"notes/{note['id']}")["content"].count("Thème.") == 1


def test_edit_analyse_writes_only_the_content(fake):
    """Payload whitelist of ONE key: the note keeps its title, category,
    flags and dossier whatever the call carries."""
    note = _analyse(fake)
    handlers.edit_analyse({"dossier_id": "d1", "expected_etag": note["etag"],
                           "operations": [{"bloc": "A", "mode": "append",
                                           "content": "x"}]})
    stored = fake.peek(f"notes/{note['id']}")
    for key in ("title", "category", "dossier_id", "is_analyse", "dateless",
                "pinned", "vjournal_uid"):
        assert stored[key] == note[key], key


def test_append_to_note_now_names_edit_analyse(fake):
    note = _analyse(fake)
    with pytest.raises(tools.ToolArgumentError, match="edit_analyse"):
        handlers.append_to_note({"note_id": note["id"], "content": "x"})


# ══════════════════════════════════════════════════════════════════════
# 6. Lectures enrichies
# ══════════════════════════════════════════════════════════════════════


def test_get_note_maps_the_theorie_and_nothing_for_an_ordinary_note(fake):
    note = _analyse(fake)
    payload = handlers.get_note({"note_id": note["id"]})
    structure = payload["note"]["structure"]
    assert structure["ok"] is True
    assert [b["bloc"] for b in structure["blocs"]] == list("ABCDEFGH")
    assert structure["blocs"][0]["heading"].startswith("Bloc A")
    assert structure["entete"]["heading"] == "Théorie de la cause"
    assert structure["remaining_chars"] == (
        note_model.CONTENT_MAX_LENGTH - len(note["content"]))
    assert payload["note"]["etag"] == note["etag"]
    _note(fake, "n2")
    assert handlers.get_note({"note_id": "n2"})["note"]["structure"] is None


@pytest.mark.parametrize("setup, state", [
    ("none", "absent"), ("one", "present"), ("two", "duplicate"),
])
def test_get_dossier_names_the_theorie_note(fake, monkeypatch, setup, state):
    for model, name in ((handlers.time_entry_model, "get_time_summary"),
                        (handlers.expense_model, "get_expense_summary"),
                        (handlers.invoice_model, "get_invoice_summary"),
                        (handlers.document_model, "get_document_summary")):
        monkeypatch.setattr(model, name, lambda d: {})
    note = None
    if setup in ("one", "two"):
        note = _analyse(fake)
    if setup == "two":
        _note(fake, "n9", is_analyse=True, dateless=True)
    record = handlers.get_dossier({"dossier_id": "d1"})["dossier"]
    assert record["analyse_note_state"] == state
    assert record["analyse_note_id"] == (note["id"] if state == "present"
                                         else None)


def test_get_dossier_says_unreadable_never_absent(fake, monkeypatch):
    def _boom(dossier_id):
        raise note_model.AnalyseLookupError("down")

    monkeypatch.setattr(note_model, "find_analyse_note_strict", _boom)
    assert handlers._analyse_note_ref("d1") == (None, "unreadable")


def test_revision_model_accepts_every_field_the_handlers_name():
    for letter in analyse_blocs.ZONE_KEYS:
        assert f"bloc:{letter}" in revision_model.VALID_FIELDS
    assert {"content", "content:rewrite"} <= set(revision_model.VALID_FIELDS)
    assert concurrency.STALE_ETAG_ERROR  # the refusal the handlers translate


def test_complete_task_en_cours_on_an_open_task_reopens_a_step_left_complete(
        fake):
    """Backs the one sentence complete_task's description and CORRECT's
    INSTRUCTIONS paragraph carry since the lot 1 completeness review: the
    lot-1a reopen cascade is also reachable from the EXISTING tool. An OPEN
    task whose linked step was left « complété » (the residue of a refused
    or failed reopen cascade) put en_cours reopens that step — and the
    protocol its last step had closed — while protocol_step_effect, which
    reads the ACTIVE protocol only, reports no linked step. Both halves are
    what the texts say; either changing must change them."""
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="à_faire")
    payload = handlers.complete_task({"task_id": "t1", "status": "en_cours"})
    assert fake.peek("tasks/t1")["status"] == "en_cours"
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "à_venir"
    assert fake.peek(f"protocols/{P}")["status"] == "actif"
    assert payload["protocol_step_effect"]["linked_step_found"] is False
    description = tools.TOOLS["complete_task"]["description"]
    assert "does not show" in description and "OPEN task" in description
