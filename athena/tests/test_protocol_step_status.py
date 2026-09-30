"""Le bouton d'étape de protocole et la cascade vers la tâche liée (lot 0b).

Trois défauts silencieux, chacun atteignable depuis la page du protocole :

1. **Le bouton basculait.** ``complete_step`` inversait l'état STOCKÉ : une
   page restée ouverte pendant que le téléphone, un second onglet ou le
   connecteur complétait l'étape faisait le CONTRAIRE de ce qu'elle
   montrait — elle rouvrait l'étape, et rouvrait la tâche liée avec elle.
   Le bouton porte maintenant l'état visé (``target``) ; un clic sur l'état
   déjà atteint n'écrit rien (``set_step_status``).
2. **La cascade réécrivait une tâche annulée.** Compléter l'étape faisait
   passer une tâche « annulée » à « terminée », la rouvrir la faisait
   passer à « à_faire » — la décision d'annuler, effacée sans un mot.
3. **La cascade ne prévenait pas le téléphone.** La tâche liée — exposée en
   DAV — changeait de statut sans qu'aucun CTag ne bouge : DavX5 ne l'a
   jamais su. La cascade bumpe désormais la collection de la tâche.

Tout passe par le faux Firestore partagé (``tests/_fake_firestore.py`` : le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais) ; une écriture rivale est posée par un crochet de commit, et l'on
relit ce qui est STOCKÉ.
"""

import ast
import logging
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
    from models import protocol as protocol_model
    from models import task as task_model
    import routes.dossiers as dossiers_routes
    import routes.protocols as protocols_routes
    import routes.tasks as tasks_routes

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (task_model,)

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2026, 12, 1, tzinfo=UTC)
P = "p1"
CTAG = "dav_sync/dossier:d1"


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model the
    cascade starts to read tomorrow cannot reach the mocked client."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "T c. L", "status": "actif"})
    fake.seed(f"protocols/{P}", {
        "id": P, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": "Protocole de l'instance",
        "protocol_type": "conventionnel", "status": "actif",
        "start_date": WHEN, "end_date": LATER, "court": "", "notes": "",
        "etag": "pe0", "created_at": WHEN, "updated_at": WHEN,
    })
    fake.seed(CTAG, {"ctag": "c0", "sync_token": "c0", "updated_at": WHEN})
    return fake


def _step(fake, sid: str, *, status: str = "à_venir", task: str | None = None,
          order: int = 1) -> None:
    fake.seed(f"protocols/{P}/steps/{sid}", {
        "id": sid, "order": order, "title": f"Étape {sid}", "description": "",
        "cpc_reference": "", "deadline_date": LATER,
        "deadline_offset_days": None, "mandatory": False,
        "deadline_locked": False, "status": status,
        "completed_date": WHEN if status == "complété" else None,
        "linked_task_id": task, "linked_hearing_id": None, "notes": "",
        "date_confirmed": True, "phase": "", "sous_phase": "",
        "created_at": WHEN, "updated_at": WHEN,
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


def _ctag(fake) -> str:
    return fake.peek(CTAG)["ctag"]


def _step_doc(fake, sid: str) -> dict:
    return fake.peek(f"protocols/{P}/steps/{sid}")


# ══════════════════════════════════════════════════════════════════════
# 1. set_step_status — jamais une bascule
# ══════════════════════════════════════════════════════════════════════


def test_completing_a_step_completes_its_task_and_bumps_the_task_collection(fake):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)  # keeps the protocol open
    _task(fake, "t1")
    step, errors, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert errors == []
    assert step["status"] == "complété"
    assert _step_doc(fake, "s1")["status"] == "complété"
    assert _step_doc(fake, "s1")["completed_date"] is not None
    assert fake.peek("tasks/t1")["status"] == "terminée"
    # THE bump: the task is DAV-exposed, and the phone learns only through it.
    assert _ctag(fake) != "c0"
    assert outcome == {"changed": True, "task_id": "t1",
                       "task_sync": "synced", "protocol_closed": False,
                       "protocol_reopened": False}


def test_reopening_a_step_reopens_a_done_task_and_bumps(fake):
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    _, errors, outcome = protocol_model.set_step_status(P, "s1", "à_venir")
    assert errors == []
    assert _step_doc(fake, "s1")["status"] == "à_venir"
    assert _step_doc(fake, "s1")["completed_date"] is None
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert fake.peek("tasks/t1")["completed_date"] is None
    assert _ctag(fake) != "c0"
    assert outcome["task_sync"] == "synced"


@pytest.mark.parametrize("stored, target", [
    ("complété", "complété"),
    ("à_venir", "à_venir"),
    ("en_cours", "à_venir"),
    ("en_retard", "à_venir"),
])
def test_asking_for_the_state_the_step_is_in_writes_nothing(fake, stored, target):
    """The stale-page case: the button was rendered for the OTHER state, the
    phone or the connector got there first. The old toggle flipped it the
    wrong way and cascaded that into the task. Now: no write at all — not
    the step, not the protocol stamp, not the task, not the CTag."""
    _step(fake, "s1", status=stored, task="t1")
    _task(fake, "t1", status="terminée" if stored == "complété" else "à_faire")
    before = {p: fake.peek(p) for p in (f"protocols/{P}", f"protocols/{P}/steps/s1",
                                        "tasks/t1", CTAG)}
    step, errors, outcome = protocol_model.set_step_status(P, "s1", target)
    assert errors == [] and step["status"] == stored
    assert outcome == {"changed": False, "task_id": None, "task_sync": "none",
                       "protocol_closed": False, "protocol_reopened": False}
    assert {p: fake.peek(p) for p in before} == before


def test_a_write_racing_the_click_is_decided_again_never_overwritten(fake):
    """A write landing between the transaction's read and its commit aborts
    it; the retry re-reads and decides on the NEW state — here « already
    complété », so nothing more is written and nothing cascades."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    fired = []

    def _rival(info):
        if not fired and ("update", f"protocols/{P}/steps/s1") in info.ops:
            fired.append(True)
            doc = _step_doc(fake, "s1")
            doc.update(status="complété", completed_date=WHEN)
            fake.external_write(f"protocols/{P}/steps/s1", doc)

    fake.add_commit_hook(_rival)
    _, errors, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert fired and errors == []
    assert outcome["changed"] is False
    assert _step_doc(fake, "s1")["completed_date"] == WHEN  # the rival's
    assert fake.peek("tasks/t1")["status"] == "à_faire"     # no cascade


def test_completing_the_last_open_step_closes_the_protocol(fake):
    _step(fake, "s1")
    _step(fake, "s2", status="complété", order=2)
    _, _, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert outcome["protocol_closed"] is True
    assert fake.peek(f"protocols/{P}")["status"] == "complété"


@pytest.mark.parametrize("target", ["terminée", "", "toggle", "en_cours"])
def test_an_unknown_target_is_refused_before_any_read(fake, target):
    _step(fake, "s1")
    before = _step_doc(fake, "s1")
    _, errors, outcome = protocol_model.set_step_status(P, "s1", target)
    assert errors == ["Statut d'étape demandé invalide."]
    assert outcome["changed"] is False
    assert _step_doc(fake, "s1") == before


def test_a_missing_step_or_protocol_is_refused_in_french(fake):
    assert protocol_model.set_step_status(P, "nope", "complété")[1] == [
        "Étape introuvable."]
    assert protocol_model.set_step_status("nope", "s1", "complété")[1] == [
        "Protocole introuvable."]


def test_complete_step_survives_as_a_wrapper_over_set_step_status(fake):
    """Kept for compatibility — it still toggles, which is why no route and
    no MCP handler may call it (the sweep below)."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    step, errors = protocol_model.complete_step(P, "s1")
    assert errors == [] and step["status"] == "complété"
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert _ctag(fake) != "c0"  # the wrapper inherits the cascade's bump


def test_the_overdue_stamp_never_regenerates_a_step_etag(fake):
    """check_overdue_steps is a write-on-READ: it runs on every protocol
    page view. Steps carry no etag yet (lot 1a adds one), and when they do
    this derived « en_retard » stamp must not churn it — the lawyer merely
    VIEWING the page would otherwise invalidate every etag the connector
    holds. Pinned now, on the shape lot 1a will give the step."""
    _step(fake, "s1")
    doc = _step_doc(fake, "s1")
    doc.update(deadline_date=datetime(2020, 1, 6, tzinfo=UTC), etag="se0")
    fake.seed(f"protocols/{P}/steps/s1", doc)
    assert protocol_model.check_overdue_steps(P) == 1
    stored = _step_doc(fake, "s1")
    assert stored["status"] == "en_retard"
    assert stored["etag"] == "se0"


def test_nothing_in_routes_or_mcp_calls_the_toggle():
    root = _ATHENA
    offenders = []
    for folder in ("routes", "mcp"):
        for path in sorted((root / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = (node.id if isinstance(node, ast.Name)
                        else node.attr if isinstance(node, ast.Attribute)
                        else node.name if isinstance(node, ast.alias)
                        else None)
                if name == "complete_step":
                    offenders.append(f"{folder}/{path.name}:{node.lineno if hasattr(node, 'lineno') else '?'}")
    assert offenders == []


# ══════════════════════════════════════════════════════════════════════
# 2. La cascade ne réécrit jamais une tâche annulée
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("stored, target", [
    ("à_venir", "complété"),   # completing: annulée used to become terminée
    ("complété", "à_venir"),   # reopening: annulée used to become à_faire
])
def test_a_cancelled_linked_task_is_left_alone_both_ways(fake, stored, target):
    _step(fake, "s1", status=stored, task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", status="annulée")
    task_before = fake.peek("tasks/t1")
    _, errors, outcome = protocol_model.set_step_status(P, "s1", target)
    assert errors == []
    assert _step_doc(fake, "s1")["status"] == target   # the step does move
    assert fake.peek("tasks/t1") == task_before        # the task does not
    assert outcome["task_sync"] == "skipped_cancelled"
    assert _ctag(fake) == "c0"                          # nothing to tell DavX5


def test_reopening_a_step_never_demotes_an_in_progress_task(fake):
    """« à_venir » means « not done »: an en_cours task already is. The old
    cascade forced it back to à_faire."""
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="en_cours")
    _, _, outcome = protocol_model.set_step_status(P, "s1", "à_venir")
    assert fake.peek("tasks/t1")["status"] == "en_cours"
    assert outcome["task_sync"] == "noop"


@pytest.mark.parametrize("step_status, task_status, expected", [
    ("complété", "à_faire", ("terminée", None)),
    ("complété", "en_cours", ("terminée", None)),
    ("complété", "terminée", (None, None)),
    ("complété", "annulée", (None, "tache_annulee")),
    ("à_venir", "terminée", ("à_faire", None)),
    ("à_venir", "à_faire", (None, None)),
    ("à_venir", "en_cours", (None, None)),
    ("à_venir", "annulée", (None, "tache_annulee")),
    ("en_cours", "à_faire", ("en_cours", None)),
    ("en_cours", "en_cours", (None, None)),
    ("en_cours", "terminée", (None, "tache_terminee")),
    ("en_cours", "annulée", (None, "tache_annulee")),
    ("en_retard", "à_faire", (None, None)),
    ("en_retard", "annulée", (None, None)),
])
def test_the_cascade_table(step_status, task_status, expected):
    assert protocol_model.cascaded_task_status(step_status, task_status) == expected


def test_a_task_cancelled_during_the_cascade_is_decided_again(fake):
    """The cascade compare-and-sets against the task it read: the phone
    cancelling the task in between must win, not be overwritten."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    fired = []

    def _rival(info):
        if not fired and any(p == "tasks/t1" for _, p in info.ops):
            fired.append(True)
            doc = fake.peek("tasks/t1")
            doc.update(status="annulée", etag="e-rival")
            fake.external_write("tasks/t1", doc)

    fake.add_commit_hook(_rival)
    _, _, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert fired
    assert fake.peek("tasks/t1")["status"] == "annulée"
    assert outcome["task_sync"] == "skipped_cancelled"


def test_a_failed_bump_after_the_task_write_is_logged_never_raised(
    fake, monkeypatch, caplog
):
    """The task write has committed: a raise here would turn a done click
    into an error page the lawyer would retry. Logged, reported synced."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")

    def _boom(name):
        raise RuntimeError("store down")

    monkeypatch.setattr(dav_sync, "bump_ctag", _boom)
    with caplog.at_level(logging.INFO):
        _, errors, outcome = protocol_model.set_step_status(P, "s1", "complété")
    assert errors == [] and outcome["task_sync"] == "synced"
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert [r.getMessage() for r in caplog.records
            if r.name == "pallas.unexpected"] == [
        "protocol cascade: task CTag bump failed"]


def test_the_cascade_leaves_a_trace_with_ids_only(fake, caplog):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", status="annulée")
    with caplog.at_level(logging.INFO):
        protocol_model.set_step_status(P, "s1", "complété")
    events = [r.json_fields for r in caplog.records
              if r.name == "pallas.protocol"]
    assert [e["event"] for e in events] == ["cascade_task_skipped",
                                           "step_status_set"]
    assert events[0]["reason"] == "tache_annulee"
    assert events[1]["task_sync"] == "skipped_cancelled"
    # Never a title: « Étape s1 » / « Tâche t1 » name the case.
    blob = repr(events)
    assert "Étape" not in blob and "Tâche" not in blob


# ══════════════════════════════════════════════════════════════════════
# 3. La route et la page (routes + gabarit réels)
# ══════════════════════════════════════════════════════════════════════


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
    app.jinja_env.filters.update(to_mtl=to_mtl, jsattr=lambda v: v)
    for bp in (protocols_routes.protocols_bp, dossiers_routes.dossiers_bp,
               tasks_routes.tasks_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _click(client, sid: str, target: str | None) -> dict:
    data = {} if target is None else {"target": target}
    resp = client.post(f"/protocoles/{P}/steps/{sid}/complete", data=data)
    assert resp.status_code == 302
    location = urlparse(resp.headers["Location"])
    assert location.path == f"/protocoles/{P}"
    return {k: v[0] for k, v in parse_qs(location.query).items()}


def test_the_web_click_completes_and_the_phone_is_told(client, fake):
    """On the old route this POST toggled and bumped NOTHING: the task
    changed status on the server and DavX5 never re-synced it."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    params = _click(client, "s1", "complété")
    assert params == {}
    assert _step_doc(fake, "s1")["status"] == "complété"
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert _ctag(fake) != "c0"


def test_a_stale_page_click_is_a_no_op_and_says_so(client, fake):
    """The page showed the step open; the phone completed it meanwhile. The
    old toggle REOPENED it (and the task with it)."""
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    params = _click(client, "s1", "complété")
    assert params == {"message": "Cette étape était déjà complétée — rien "
                                 "n'a été changé."}
    assert _step_doc(fake, "s1")["status"] == "complété"
    assert fake.peek("tasks/t1")["status"] == "terminée"
    assert _ctag(fake) == "c0"


def test_the_cancelled_task_is_named_on_the_page(client, fake):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", status="annulée")
    params = _click(client, "s1", "complété")
    assert params == {"message": "Étape mise à jour. La tâche liée est "
                                 "annulée : elle n'a pas été modifiée."}
    assert fake.peek("tasks/t1")["status"] == "annulée"


def test_a_vanished_linked_task_is_named_honestly(client, fake):
    """The step still moves; the link points at no task. ``get_task``
    fails OPEN, so the cascade cannot tell « deleted » from « unreadable »
    — the banner names both instead of asserting the wrong one."""
    _step(fake, "s1", task="t-gone")
    _step(fake, "s2", order=2)
    params = _click(client, "s1", "complété")
    assert params == {"message": "Étape mise à jour. La tâche liée est "
                                 "introuvable ou n'a pas pu être lue : elle "
                                 "n'a pas été modifiée."}
    assert _step_doc(fake, "s1")["status"] == "complété"
    assert fake.peek_collection("tasks") == {}
    assert _ctag(fake) == "c0"


def test_a_refused_task_write_is_named_on_the_page(client, fake):
    """A legacy task the model refuses to rewrite (here an unknown category)
    leaves the step and its task out of step: said, never swallowed."""
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    doc = fake.peek("tasks/t1")
    doc["category"] = "catégorie_disparue"
    fake.seed("tasks/t1", doc)
    params = _click(client, "s1", "complété")
    assert params == {"message": "Étape mise à jour. La tâche liée n'a pas "
                                 "pu être mise à jour — vérifiez-la depuis "
                                 "sa fiche."}
    assert _step_doc(fake, "s1")["status"] == "complété"
    assert fake.peek("tasks/t1")["status"] == "à_faire"
    assert _ctag(fake) == "c0"


def test_closing_the_protocol_is_announced(client, fake):
    _step(fake, "s1")
    params = _click(client, "s1", "complété")
    assert "le protocole est maintenant complété" in params["message"]


def test_a_refusal_comes_back_as_a_banner_never_silence(client, fake):
    """The old route answered a refusal on this full-page form with a bare
    redirect: nothing on screen said why the click did nothing."""
    params = _click(client, "gone", "complété")
    assert params == {"erreur": "Étape introuvable."}


def test_a_page_rendered_before_the_target_existed_still_works(client, fake):
    """No `target` posted: the intent is read off the stored status (the
    old toggle's reading), accepted only for pages open at deploy time."""
    _step(fake, "s1", status="complété", task="t1")
    _task(fake, "t1", status="terminée")
    assert _click(client, "s1", None) == {}
    assert _step_doc(fake, "s1")["status"] == "à_venir"
    assert fake.peek("tasks/t1")["status"] == "à_faire"


def test_the_page_posts_the_rendered_target_and_shows_the_banners(client, fake):
    _step(fake, "s1")
    _step(fake, "s2", status="complété", order=2)
    page = client.get(
        f"/protocoles/{P}?erreur=refus&message=reste").get_data(as_text=True)
    targets = re.findall(r'name="target" value="([^"]+)"', page)
    assert sorted(targets) == ["complété", "à_venir"]
    assert "toggle" not in page
    banners = re.findall(r'<div role="(?:alert|status)" class="([^"]+)"', page)
    assert len(banners) == 2, banners
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    absent = []
    for c in sorted({c for block in banners for c in block.split()}):
        needle = "." + c.replace(":", r"\:").replace("/", r"\/")
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent
