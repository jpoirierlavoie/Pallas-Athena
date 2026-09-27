"""La page d'un protocole et son service (lot 1a, L2).

``services/protocoles.py`` est la porte unique des routes (et, au lot 1b, du
connecteur) : il construit la création depuis le dossier, et il fait suivre
à chaque tâche liée l'échéance de son étape quand elle bouge — ce que rien
ne faisait (la date n'était copiée qu'à la création). Les routes passent
par lui, portent l'etag de ce qu'elles affichent, et disent leurs refus par
un bandeau en 2xx (htmx n'échange jamais un 4xx).

Tout passe par les vraies routes, le vrai gabarit et le faux Firestore
partagé ; on relit ce qui est STOCKÉ.
"""

import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

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
    import routes.dossiers as dossiers_routes
    import routes.protocols as protocols_routes
    import routes.tasks as tasks_routes
    from services import protocoles as protocol_service

from flask import Flask  # noqa: E402
from markupsafe import Markup, escape  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
START2 = datetime(2026, 10, 5, tzinfo=UTC)
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


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    # jsattr as main.py registers it: a JS string literal, HTML-escaped
    # for a double-quoted attribute, returned as Markup.
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        jsattr=lambda v: Markup(json.dumps(str(v)).replace('"', "&quot;")))
    for bp in (protocols_routes.protocols_bp, dossiers_routes.dossiers_bp,
               tasks_routes.tasks_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _protocol(fake, pid: str = P, *, status: str = "actif", closed_by=None,
              ptype: str = "conventionnel") -> None:
    doc = {
        "id": pid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": "Protocole de l'instance",
        "protocol_type": ptype, "status": status,
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": f"pe-{pid}", "created_at": WHEN, "updated_at": WHEN,
    }
    if closed_by is not None:
        doc["closed_by"] = closed_by
        doc["closed_at"] = WHEN
    fake.seed(f"protocols/{pid}", doc)


def _step(fake, sid: str, *, status: str = "à_venir", task=None,
          order: int = 1, deadline=LATER, **over) -> None:
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
    fake.seed(f"protocols/{P}/steps/{sid}", doc)


def _task(fake, tid: str, *, status: str = "à_faire", due=None,
          dossier_id: str = "d1") -> None:
    fake.seed(f"tasks/{tid}", {
        "id": tid, "dossier_id": dossier_id, "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": f"Tâche {tid}", "description": "",
        "priority": "normale", "status": status, "due_date": due,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    })


def _ctag(fake) -> str:
    return fake.peek(CTAG)["ctag"]


def _params(resp) -> dict:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    location = urlparse(resp.headers["Location"])
    return {k: v[0] for k, v in parse_qs(location.query).items()}


# ══════════════════════════════════════════════════════════════════════
# 1. align_linked_tasks — la tâche suit son étape, sauf si elle a sa date
# ══════════════════════════════════════════════════════════════════════

OLD = datetime(2026, 11, 2, tzinfo=UTC)
NEW = datetime(2026, 11, 16, tzinfo=UTC)


def _moved(*task_ids):
    return [{"step_id": f"s-{t}", "old": OLD, "new": NEW, "linked_task_id": t}
            for t in task_ids]


def test_tasks_still_on_the_old_date_follow_and_one_bump_per_collection(fake):
    _task(fake, "t1", due=OLD)
    _task(fake, "t2", due=OLD)
    fake.reset_logs()
    report = protocol_service.align_linked_tasks(_moved("t1", "t2"))
    assert report["aligned"] == 2
    assert fake.peek("tasks/t1")["due_date"] == NEW
    assert fake.peek("tasks/t2")["due_date"] == NEW
    assert fake.peek("tasks/t1")["updated_via"] == "script"
    assert _ctag(fake) != "c0"
    bumps = [c for c in fake.commits if any(p == CTAG for _, p in c.ops)]
    assert len(bumps) == 1


@pytest.mark.parametrize("kw, outcome", [
    ({"due": datetime(2026, 11, 9, tzinfo=UTC)}, "diverged"),   # by hand
    ({"due": None}, "diverged"),                                 # cleared
    ({"due": NEW}, "unchanged"),
    ({"due": OLD, "status": "terminée"}, "closed"),
    ({"due": OLD, "status": "annulée"}, "closed"),
])
def test_a_task_that_is_not_on_the_old_date_is_left_alone(fake, kw, outcome):
    _task(fake, "t1", **kw)
    before = fake.peek("tasks/t1")
    report = protocol_service.align_linked_tasks(_moved("t1"))
    assert report[outcome] == 1 and report["aligned"] == 0
    assert fake.peek("tasks/t1") == before
    assert _ctag(fake) == "c0"


def test_a_missing_task_is_counted_and_nothing_else_moves(fake):
    report = protocol_service.align_linked_tasks(
        _moved("t-gone") + [{"step_id": "s", "old": OLD, "new": NEW,
                             "linked_task_id": None}])
    assert report == {**{k: 0 for k in protocol_service.ALIGN_OUTCOMES},
                      "missing": 1}


def test_a_task_rewritten_during_the_alignment_is_decided_again(fake):
    """Compare-and-set on the task just read: the phone moving its due date
    in between wins — the re-read then finds it off the old date."""
    _task(fake, "t1", due=OLD)
    fired = []

    def _rival(info):
        if not fired and any(p == "tasks/t1" for _, p in info.ops):
            fired.append(True)
            doc = fake.peek("tasks/t1")
            doc.update(due_date=datetime(2026, 12, 1, tzinfo=UTC), etag="riv")
            fake.external_write("tasks/t1", doc)

    fake.add_commit_hook(_rival)
    report = protocol_service.align_linked_tasks(_moved("t1"))
    assert fired and report["diverged"] == 1
    assert fake.peek("tasks/t1")["due_date"] == datetime(2026, 12, 1, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# 2. La création : depuis le dossier, et des tâches seulement si demandées
# ══════════════════════════════════════════════════════════════════════


def test_the_service_creates_no_task_unless_asked(fake):
    dossier = fake.peek("dossiers/d1")
    proto, errors, report = protocol_service.create_protocol(
        dossier, "cq_simplifié", WHEN)
    assert errors == [] and fake.peek_collection("tasks") == {}
    assert report == {"tasks_created": 0, "tasks_linked": 0,
                      "tasks_failed": 0}
    stored = fake.peek(f"protocols/{proto['id']}")
    assert stored["title"] == "Protocole de l'instance"
    assert (stored["dossier_file_number"], stored["dossier_title"]) == (
        "2026-001", "T c. L")


def test_linked_tasks_leave_the_returned_etags_equal_to_the_stored_ones(fake):
    """Revue L2 : lier une tâche est une écriture de l'étape ET du
    protocole (leurs etags changent). Le service rendait pourtant le
    protocole et ses étapes avec les etags d'AVANT le lien — la prochaine
    comparaison (update_protocol, update_step, au connecteur du lot 1b)
    aurait été refusée pour une version que personne d'autre n'a écrite."""
    dossier = fake.peek("dossiers/d1")
    proto, errors, report = protocol_service.create_protocol(
        dossier, "cq_simplifié", WHEN, create_linked_tasks=True)
    assert errors == [] and report["tasks_linked"] == 7
    stored = fake.peek(f"protocols/{proto['id']}")
    assert proto["etag"] == stored["etag"]
    for step in proto["steps"]:
        on_disk = fake.peek(f"protocols/{proto['id']}/steps/{step['id']}")
        assert step["linked_task_id"] == on_disk["linked_task_id"]
        assert step["etag"] == on_disk["etag"]
    # The etag handed back is usable as is.
    _doc, errors, _r = protocol_service.update_protocol(
        proto["id"], {"notes": "Suivi"}, expected_etag=proto["etag"])
    assert errors == []


@pytest.mark.parametrize("checked, tasks", [(False, 0), (True, 7)])
def test_the_wizard_checkbox_decides_the_linked_tasks(client, fake, checked,
                                                      tasks):
    data = {"dossier_id": "d1", "protocol_type": "cq_simplifié",
            "start_date": "2026-09-01", "title": ""}
    if checked:
        data["auto_create_tasks"] = "on"
    resp = client.post("/protocoles/", data=data)
    assert resp.status_code == 302
    assert len(fake.peek_collection("tasks")) == tasks


def test_a_refused_creation_re_renders_what_was_submitted(client, fake):
    _protocol(fake, "existing")
    resp = client.post("/protocoles/", data={
        "dossier_id": "d1", "protocol_type": "conventionnel",
        "start_date": "2026-09-01", "title": "Mon protocole",
        "notes": "Note gardée"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "déjà un protocole actif" in html
    assert 'value="Mon protocole"' in html and "Note gardée" in html
    assert 'name="expected_etag"' not in html    # a creation carries none


# ══════════════════════════════════════════════════════════════════════
# 3. La date de début : les étapes ET leurs tâches, dit dans un bandeau
# ══════════════════════════════════════════════════════════════════════


def test_a_new_start_date_moves_steps_and_tasks_and_says_so(client, fake):
    dossier = fake.peek("dossiers/d1")
    proto, errors, _ = protocol_service.create_protocol(
        dossier, "cq_simplifié", WHEN, create_linked_tasks=True)
    assert errors == []
    pid = proto["id"]
    steps = sorted(fake.peek_collection(f"protocols/{pid}/steps").values(),
                   key=lambda s: s["order"])
    # One task keeps a date set by hand; one step is already completed.
    hand = datetime(2026, 9, 30, tzinfo=UTC)
    task_by_hand = steps[1]["linked_task_id"]
    doc = fake.peek(f"tasks/{task_by_hand}")
    doc["due_date"] = hand
    fake.seed(f"tasks/{task_by_hand}", doc)
    protocol_service.set_step_status(pid, steps[0]["id"], "complété")
    etag = fake.peek(f"protocols/{pid}")["etag"]
    ctag_before = _ctag(fake)

    params = _params(client.post(f"/protocoles/{pid}", data={
        "title": "Protocole de l'instance", "notes": "", "status": "actif",
        "start_date": "2026-10-05", "expected_etag": etag}))

    assert params["message"] == (
        "Date de début modifiée : 6 échéances recalculées. 1 échéance "
        "conservée (étape complétée ou date confirmée). 5 tâches liées ont "
        "suivi leur étape. 1 tâche liée garde sa propre échéance, modifiée à "
        "la main : elle n'a pas été déplacée.")
    for step in steps[2:]:
        stored = fake.peek(f"protocols/{pid}/steps/{step['id']}")
        task = fake.peek(f"tasks/{step['linked_task_id']}")
        assert task["due_date"] == stored["deadline_date"]
        assert stored["deadline_date"] == protocol_model._compute_deadline(
            START2, step["deadline_offset_days"])
    assert fake.peek(f"tasks/{task_by_hand}")["due_date"] == hand
    assert _ctag(fake) != ctag_before


def test_a_save_without_status_no_longer_reactivates(client, fake):
    """The old route defaulted a missing status to « actif »."""
    _protocol(fake, status="complété", closed_by="web")
    resp = client.post(f"/protocoles/{P}", data={
        "title": "Protocole de l'instance", "notes": "Clos",
        "start_date": "2026-09-01"})
    assert resp.status_code == 302
    stored = fake.peek(f"protocols/{P}")
    assert stored["status"] == "complété" and stored["notes"] == "Clos"


def test_reactivating_beside_another_protocol_is_refused_on_the_form(
    client, fake
):
    _protocol(fake, status="suspendu", closed_by="web")
    _protocol(fake, "p2")
    resp = client.post(f"/protocoles/{P}", data={
        "title": "Protocole de l'instance", "notes": "",
        "status": "actif", "start_date": "2026-09-01",
        "expected_etag": f"pe-{P}"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Un autre protocole est actif dans ce dossier" in html
    assert fake.peek(f"protocols/{P}")["status"] == "suspendu"


# ══════════════════════════════════════════════════════════════════════
# 4. Les formulaires d'étape
# ══════════════════════════════════════════════════════════════════════


def test_an_edited_deadline_carries_its_task_along(client, fake):
    _protocol(fake)
    _step(fake, "s1", task="t1", deadline=OLD)
    _task(fake, "t1", due=OLD)
    params = _params(client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "2026-11-16", "notes": "", "expected_etag": "se-s1"}))
    assert params["message"] == ("Échéance modifiée. 1 tâche liée a suivi son "
                                 "étape.")
    assert fake.peek("tasks/t1")["due_date"] == NEW
    assert _ctag(fake) != "c0"


def test_a_posted_status_is_never_forwarded(client, fake):
    _protocol(fake)
    _step(fake, "s1")
    _step(fake, "s2", order=2)
    params = _params(client.post(f"/protocoles/{P}/steps/s1", data={
        "notes": "Vu", "status": "complété", "expected_etag": "se-s1"}))
    assert params == {}
    stored = fake.peek(f"protocols/{P}/steps/s1")
    assert stored["status"] == "à_venir" and stored["notes"] == "Vu"


def test_a_refused_step_edit_reopens_its_form_on_the_submission(client, fake):
    _protocol(fake, ptype="cs_ordinaire")
    _step(fake, "s1", mandatory=True, deadline_offset_days=15)
    resp = client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "", "notes": "GARDÉE-4Z", "expected_etag": "se-s1"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "ne peut pas être effacée" in html
    assert "editStepId: &quot;s1&quot;" in html
    assert 'value="GARDÉE-4Z"' in html
    assert re.findall(r'name="expected_etag" value="([^"]*)"', html) == ["se-s1"]


def test_a_stale_step_edit_shows_the_banner_and_the_current_etag(client, fake):
    _protocol(fake)
    _step(fake, "s1")
    doc = fake.peek(f"protocols/{P}/steps/s1")
    doc.update(notes="Rivale", etag="se-rival")
    fake.external_write(f"protocols/{P}/steps/s1", doc)
    resp = client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "2099-12-01", "notes": "MIENNE-8K",
        "expected_etag": "se-s1"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Cet élément a été modifié entre-temps." in html
    assert 'value="MIENNE-8K"' in html
    assert re.findall(r'name="expected_etag" value="([^"]*)"', html) == [
        "se-rival"]
    assert fake.peek(f"protocols/{P}/steps/s1")["notes"] == "Rivale"


def test_the_confirmation_box_confirms_without_changing_the_date(client, fake):
    _protocol(fake, ptype="cs_ordinaire")
    _step(fake, "s1", mandatory=True, deadline_offset_days=15,
          date_confirmed=False)
    page = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert 'name="confirm_date"' in page and "À modifier" in page
    _params(client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "2099-12-01", "notes": "", "confirm_date": "1",
        "expected_etag": "se-s1"}))
    stored = fake.peek(f"protocols/{P}/steps/s1")
    assert stored["date_confirmed"] is True
    assert stored["deadline_date"] == LATER
    page = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert 'name="confirm_date"' not in page and "À modifier" not in page


def test_a_notes_save_leaves_the_suggested_date_unconfirmed(client, fake):
    _protocol(fake, ptype="cs_ordinaire")
    _step(fake, "s1", mandatory=True, deadline_offset_days=15,
          date_confirmed=False)
    _params(client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "2099-12-01", "notes": "Appel",
        "expected_etag": "se-s1"}))
    stored = fake.peek(f"protocols/{P}/steps/s1")
    assert stored["notes"] == "Appel" and stored["date_confirmed"] is False


def test_a_legacy_spurious_confirmation_shows_as_a_suggestion(client, fake):
    """Revue L2 : le drapeau posé par une simple note avant le lot 1a ne
    fait plus dire « confirmée » à la page pour une date que le recalcul
    déplacerait — le badge et la case reviennent, et la case confirme."""
    _protocol(fake, ptype="cs_ordinaire")
    suggested = protocol_model._compute_deadline(WHEN, 15)
    _step(fake, "s1", mandatory=True, deadline_offset_days=15,
          deadline=suggested, date_confirmed=True)
    page = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert 'name="confirm_date"' in page and "À modifier" in page
    _params(client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": suggested.strftime("%Y-%m-%d"), "notes": "",
        "confirm_date": "1", "expected_etag": "se-s1"}))
    assert fake.peek(f"protocols/{P}/steps/s1")["date_confirmed_at"]
    page = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert 'name="confirm_date"' not in page and "À modifier" not in page


def test_a_refused_step_addition_comes_back_as_a_banner(client, fake):
    params = _params(client.post("/protocoles/gone/steps", data={
        "title": "Plaidoirie"}))
    assert params == {"erreur": "Protocole introuvable."}


def test_an_added_step_is_created_without_a_task(client, fake):
    _protocol(fake)
    assert _params(client.post(f"/protocoles/{P}/steps", data={
        "title": "Plaidoirie", "deadline_date": "2099-06-01"})) == {}
    steps = fake.peek_collection(f"protocols/{P}/steps")
    (step,) = steps.values()
    assert step["mandatory"] is False and step["linked_task_id"] is None
    assert fake.peek_collection("tasks") == {}


# ══════════════════════════════════════════════════════════════════════
# 5. Le bouton d'étape et le protocole
# ══════════════════════════════════════════════════════════════════════


def test_the_step_button_on_a_suspended_protocol_says_why(client, fake):
    _protocol(fake, status="suspendu")
    _step(fake, "s1")
    params = _params(client.post(f"/protocoles/{P}/steps/s1/complete",
                                 data={"target": "complété"}))
    assert "réactivez-le" in params["erreur"]
    assert fake.peek(f"protocols/{P}/steps/s1")["status"] == "à_venir"


def test_reopening_a_step_of_an_auto_closed_protocol_is_announced(client, fake):
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété")
    params = _params(client.post(f"/protocoles/{P}/steps/s1/complete",
                                 data={"target": "à_venir"}))
    assert params["message"] == ("Le protocole, fermé à sa dernière étape, "
                                 "est de nouveau actif.")
    assert fake.peek(f"protocols/{P}")["status"] == "actif"


# ══════════════════════════════════════════════════════════════════════
# 5 bis. Un protocole non actif (D17, 2026-09-27) : la règle du connecteur
#        vaut aussi pour le web
# ══════════════════════════════════════════════════════════════════════
#
# Le modèle refuse désormais d'ajouter ou de modifier une étape d'un
# protocole qui n'est pas « actif », quel que soit l'appelant ; la page
# masque ces commandes et dit comment le réactiver. Seule exception, celle
# du modèle : rouvrir une étape complétée d'un protocole fermé par la
# cascade (closed_by « auto ») le réactive.

_INACTIVE = [("suspendu", "web"), ("complété", "web"), ("complété", "auto")]
# As the page prints them: Jinja escapes the apostrophe.
_BANNER = str(escape(protocols_routes.INACTIVE_STEPS_BANNER))
_HINT = str(escape(protocols_routes.AUTO_CLOSED_REOPEN_HINT))


@pytest.mark.parametrize("status, closed_by", _INACTIVE)
def test_a_web_step_addition_on_an_inactive_protocol_is_refused(
    client, fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    params = _params(client.post(f"/protocoles/{P}/steps", data={
        "title": "Plaidoirie", "deadline_date": "2099-06-01"}))
    assert params == {
        "erreur": protocol_model.inactive_protocol_edit_error(status)}
    assert "réactivez-le (Modifier le protocole" in params["erreur"]
    assert fake.peek_collection(f"protocols/{P}/steps") == {}
    # ... and the page it lands on SHOWS it, in the red box (a refusal
    # that only travels in a query string is a silent one).
    page = client.get(f"/protocoles/{P}", query_string=params)
    refusal = str(escape(params["erreur"]))
    assert re.search(r'role="alert"[^>]*>\s*' + re.escape(refusal),
                     page.get_data(as_text=True))


@pytest.mark.parametrize("status, closed_by", _INACTIVE)
def test_a_web_step_edit_on_an_inactive_protocol_is_refused_at_200(
    client, fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    _step(fake, "s1")
    before = fake.peek(f"protocols/{P}/steps/s1")
    resp = client.post(f"/protocoles/{P}/steps/s1", data={
        "deadline_date": "2099-06-01", "notes": "Vu",
        "expected_etag": "se-s1"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # The MODEL's refusal, in the red box — not merely the amber banner,
    # which carries « avant de modifier ses étapes » on every render of an
    # inactive protocol and so proved nothing about the refusal itself
    # (review of D17).
    refusal = str(escape(protocol_model.inactive_protocol_edit_error(status)))
    assert re.search(r'role="alert"[^>]*>\s*' + re.escape(refusal), html)
    assert fake.peek(f"protocols/{P}/steps/s1") == before


@pytest.mark.parametrize("status, closed_by", _INACTIVE)
def test_an_inactive_protocol_page_shows_the_banner_and_hides_the_controls(
    client, fake, status, closed_by,
):
    _protocol(fake, status=status, closed_by=closed_by)
    _step(fake, "s1")                       # open
    _step(fake, "s2", order=2, status="complété")
    html = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert _BANNER in html
    assert "+ Ajouter une étape" not in html
    assert f'action="/protocoles/{P}/steps"' not in html
    assert f'action="/protocoles/{P}/steps/s1"' not in html
    assert f'action="/protocoles/{P}/steps/s2"' not in html
    # Neither inline edit opener (« Modifier » / « Ajouter une note »).
    assert "editStepId = 's1'" not in html
    assert "editStepId = 's2'" not in html
    assert "Ajouter une note" not in html
    # The open step is never offered « Compléter » here.
    assert f"/protocoles/{P}/steps/s1/complete" not in html
    reopen = f"/protocoles/{P}/steps/s2/complete"
    if closed_by == "auto":
        # ... but the auto-closed protocol keeps the one path the model
        # accepts: reopening a completed step, which reactivates it.
        assert reopen in html
        assert _HINT in html
    else:
        assert reopen not in html
        assert _HINT not in html


def test_an_active_protocol_page_keeps_every_step_control(client, fake):
    _protocol(fake)
    _step(fake, "s1")
    html = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert _BANNER not in html
    assert "+ Ajouter une étape" in html
    assert f'action="/protocoles/{P}/steps"' in html
    assert f'action="/protocoles/{P}/steps/s1"' in html
    assert f"/protocoles/{P}/steps/s1/complete" in html


def test_the_auto_closed_reopen_still_works_from_the_page(client, fake):
    """The exception the banner names, end to end: the reopen button the
    page keeps reactivates the protocol, after which the controls return."""
    _protocol(fake, status="complété", closed_by="auto")
    _step(fake, "s1", status="complété")
    _params(client.post(f"/protocoles/{P}/steps/s1/complete",
                        data={"target": "à_venir"}))
    assert fake.peek(f"protocols/{P}")["status"] == "actif"
    html = client.get(f"/protocoles/{P}").get_data(as_text=True)
    assert _BANNER not in html
    assert "+ Ajouter une étape" in html


def test_the_inactive_banner_uses_only_compiled_classes(client, fake):
    _protocol(fake, status="suspendu", closed_by="web")
    html = client.get(f"/protocoles/{P}").get_data(as_text=True)
    banner = re.search(
        r'<div role="status" class="([^"]+)">\s*'
        + re.escape(_BANNER), html)
    assert banner, "the banner is not rendered"
    assert _absent_classes(set(banner.group(1).split())) == []


# ══════════════════════════════════════════════════════════════════════
# 6. Les classes employées existent dans l'artefact compilé
# ══════════════════════════════════════════════════════════════════════


def _absent_classes(classes: set[str]) -> list[str]:
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(
        encoding="utf-8")
    absent = []
    for c in sorted(classes):
        needle = "." + c.replace(":", r"\:").replace("/", r"\/")
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    return absent


def test_the_confirmation_box_uses_only_compiled_classes(client, fake):
    _protocol(fake, ptype="cs_ordinaire")
    _step(fake, "s1", mandatory=True, deadline_offset_days=15,
          date_confirmed=False)
    page = client.get(f"/protocoles/{P}").get_data(as_text=True)
    label = re.search(
        r'<label class="([^"]+)">\s*<input type="checkbox" name="confirm_date"'
        r'[^>]*class="([^"]+)"', page)
    assert label, "the confirmation box is not rendered"
    assert _absent_classes(set(label.group(1).split())
                           | set(label.group(2).split())) == []


# ══════════════════════════════════════════════════════════════════════
# Le détail que le connecteur rapporte (lot 1b, L6)
# ══════════════════════════════════════════════════════════════════════
#
# Le web dit des COMPTES dans un bandeau ; un outil du connecteur doit
# nommer chaque étape déplacée ou conservée et chaque tâche, et dire si le
# téléphone a été prévenu. Le service rend donc le détail À CÔTÉ des
# comptes, sans rien changer à ceux-ci.


def test_the_detailed_alignment_names_each_task_and_its_bump(fake):
    _task(fake, "t1", due=OLD)
    _task(fake, "t2", due=datetime(2026, 11, 9, tzinfo=UTC))   # by hand
    counts, tasks = protocol_service.align_linked_tasks_detailed(
        _moved("t1", "t2") + [{"step_id": "s-none", "old": OLD, "new": NEW,
                               "linked_task_id": None}])
    assert counts["aligned"] == 1 and counts["diverged"] == 1
    assert tasks == [
        {"task_id": "t1", "step_id": "s-t1", "outcome": "aligned",
         "ctag_bumped": True},
        {"task_id": "t2", "step_id": "s-t2", "outcome": "diverged",
         "ctag_bumped": False},
    ]


def test_a_failed_bump_is_reported_not_claimed(fake, monkeypatch):
    """The phone learns of a moved task through the bump alone: a caller
    reporting the sync must be able to say when it did not happen."""
    _task(fake, "t1", due=OLD)

    def _down(_name):
        raise RuntimeError("store down")

    monkeypatch.setattr(protocol_service, "bump_ctag", _down)
    counts, tasks = protocol_service.align_linked_tasks_detailed(_moved("t1"))
    assert counts["aligned"] == 1
    assert tasks == [{"task_id": "t1", "step_id": "s-t1",
                      "outcome": "aligned", "ctag_bumped": False}]
    assert fake.peek("tasks/t1")["due_date"] == NEW   # the write did land


def test_update_protocol_reports_what_moved_what_stayed_and_each_task(fake):
    dossier = fake.peek("dossiers/d1")
    proto, errors, _r = protocol_service.create_protocol(
        dossier, "cq_simplifié", WHEN, create_linked_tasks=True)
    assert errors == []
    first = sorted(proto["steps"], key=lambda s: s["order"])[0]
    _step_doc, errors, _o = protocol_service.set_step_status(
        proto["id"], first["id"], "complété")
    assert errors == []
    _doc, errors, report = protocol_service.update_protocol(
        proto["id"], {"start_date": START2})
    assert errors == []
    assert report["moved"] == len(report["moved_steps"]) == 6
    assert report["preserved_steps"] == [{"step_id": first["id"],
                                          "reason": "completed"}]
    assert {t["step_id"] for t in report["tasks"]} == {
        m["step_id"] for m in report["moved_steps"]}
    assert all(t["outcome"] == "aligned" and t["ctag_bumped"]
               for t in report["tasks"])
    assert report["aligned"] == 6        # the web's counts, unchanged


def test_update_step_reports_its_linked_task(fake):
    _protocol(fake)
    _step(fake, "s1", task="t1", deadline=OLD)
    _task(fake, "t1", due=OLD)
    step, errors, report = protocol_service.update_step(
        P, "s1", {"deadline_date": NEW})
    assert errors == []
    assert report["aligned"] == 1
    assert report["tasks"] == [{"task_id": "t1", "step_id": "s1",
                                "outcome": "aligned", "ctag_bumped": True}]
    # A notes-only edit moves nothing, and says so.
    _s, errors, report = protocol_service.update_step(
        P, "s1", {"notes": "Suivi"})
    assert errors == [] and report["tasks"] == [] and report["aligned"] == 0
