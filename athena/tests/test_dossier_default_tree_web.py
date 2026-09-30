"""L'arborescence par défaut et la suppression d'un dossier, au web
(routes/dossiers, 2026-09-30).

Les VRAIES routes, les VRAIS gabarits, les VRAIS modèles, au-dessus du faux
Firestore partagé (le client est le vrai) : on relit ce qui est STOCKÉ.

Ce que ces tests refusent de l'ancien code :

* « Supprimer » redirigeait vers la LISTE quoi qu'il arrive — un refus se
  lisait comme une réussite, et le fragment 422 de la branche htmx ne
  paraissait jamais ;
* le dialogue promettait « Toutes les données de ce dossier seront
  supprimées », alors que rien n'est supprimé tant que le dossier contient
  quelque chose ;
* le bandeau ambre offrait TOUJOURS « Resynchroniser le téléphone », qui ne
  répare pas une arborescence manquante ;
* un nouveau dossier naissait sans dossiers de classement.
"""

import ast
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    from models import audit_event  # record_deletion's store
    from models import dossier as dossier_model
    from models import folder as folder_model
    import routes.doc_templates as doc_templates_routes
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.hearings as hearings_routes
    import routes.invoices as invoices_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.reception as reception_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, audit_event,)

UTC = timezone.utc
_BUTTON = folder_model.DEFAULT_TREE_BUTTON


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


def _app():
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               time_expenses_routes.time_expenses_bp,
               documents_routes.documents_bp, notes_routes.notes_bp,
               tasks_routes.tasks_bp, protocols_routes.protocols_bp,
               hearings_routes.hearings_bp,
               doc_templates_routes.doc_templates_bp,
               invoices_routes.invoices_bp,
               reception_routes.reception_bp):
        app.register_blueprint(bp)
    return app


@pytest.fixture
def client(db):
    c = _app().test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(
        dossiers_routes, "log_dossier_event",
        lambda event, dossier_id, **kw: seen.append((event, dossier_id, kw)),
    )
    return seen


_CREATE_FORM = {
    "file_number": "2026-001", "title": "Tremblay c. Lavoie",
    "clients_json": ('[{"id": "p1", "name": "Jean Tremblay", '
                     '"roles": ["demandeur"]}]'),
    "status": "actif", "forum_type": "judiciaire",
    "mandate_type": "judiciaire", "fee_type": "hourly",
}


def _bare_dossier(file_number="2026-050") -> str:
    """A dossier created by the MODEL alone — no tree (as every dossier
    created before 2026-09-30)."""
    doc, errors = dossier_model.create_dossier({
        "file_number": file_number, "title": "Sans arborescence",
        "clients": [{"id": "p1", "name": "Jean Tremblay"}],
    })
    assert errors == [], errors
    return doc["id"]


@pytest.fixture
def did(client, db):
    """A dossier created through the ROUTE — it carries its default tree."""
    resp = client.post("/dossiers/", data=_CREATE_FORM)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    return resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _folders_of(db, did) -> dict:
    return {k: v for k, v in db.peek_collection("folders").items()
            if v.get("dossier_id") == did}


def _audit_rows(db) -> list:
    return list(db.peek_collection("audit_events").values())


def _amber_banner(page: str) -> str:
    i = page.index('role="alert" class="mb-6 p-4 bg-amber-50')
    return page[i:page.index("</div>", i)]


# ══════════════════════════════════════════════════════════════════════
# 1. La création pose l'arborescence
# ══════════════════════════════════════════════════════════════════════


def test_creating_a_dossier_through_the_form_gives_it_its_tree(client, db):
    resp = client.post("/dossiers/", data=_CREATE_FORM)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    did = location.rsplit("/", 1)[-1]
    assert "?" not in location
    folders = _folders_of(db, did)
    assert len(folders) == 18
    assert {f["name"] for f in folders.values()
            if f.get("parent_folder_id") is None} == {
        "Mandat", "Procédures", "Correspondance", "Interne", "Autres"}


def test_the_create_route_asks_the_engine_for_the_new_id(client, db, monkeypatch):
    calls = []
    real = folder_model.ensure_default_tree

    def spy(dossier_id, **kwargs):
        calls.append((dossier_id, kwargs))
        return real(dossier_id, **kwargs)

    monkeypatch.setattr(folder_model, "ensure_default_tree", spy)
    resp = client.post("/dossiers/", data=_CREATE_FORM)
    did = resp.headers["Location"].rsplit("/", 1)[-1]
    assert calls == [(did, {})]


@pytest.mark.parametrize("failure", ["errors", "raises", "blocked"])
def test_a_tree_that_fails_warns_but_the_dossier_is_created(
        client, db, monkeypatch, failure):
    def broken(dossier_id, **kwargs):
        if failure == "raises":
            raise RuntimeError("boom")
        if failure == "blocked":
            report = folder_model.TreeReport()
            report.blocked.append(("mandat", "duplicate"))
            return report, []
        return None, [folder_model.READ_ERROR]

    monkeypatch.setattr(folder_model, "ensure_default_tree", broken)
    resp = client.post("/dossiers/", data=_CREATE_FORM)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "tab=documents" in location and "avertissement=arborescence" in location
    did = location.split("?")[0].rsplit("/", 1)[-1]
    assert db.peek(f"dossiers/{did}") is not None

    page = client.get(location).get_data(as_text=True)
    banner = _amber_banner(page)
    assert "Le dossier est créé, mais son arborescence" in banner
    assert f'action="/dossiers/{did}/arborescence"' in banner
    assert 'name="csrf_token" value="tok"' in banner
    assert _BUTTON in banner or _BUTTON.replace("'", "&#39;") in banner
    assert "/dav/resync" not in banner
    assert "Resynchroniser le téléphone" not in banner


def test_the_htmx_create_answers_a_2xx_hx_redirect(client, db, monkeypatch):
    monkeypatch.setattr(folder_model, "ensure_default_tree",
                        lambda dossier_id, **kw: (None, ["x"]))
    resp = client.post("/dossiers/", data=_CREATE_FORM,
                       headers={"HX-Request": "true"})
    assert 200 <= resp.status_code < 300
    assert "avertissement=arborescence" in resp.headers["HX-Redirect"]


def test_the_dav_warnings_still_offer_the_resync(client, db, did):
    page = client.get(f"/dossiers/{did}?avertissement=dav_fermeture"
                      ).get_data(as_text=True)
    banner = _amber_banner(page)
    assert f'action="/dossiers/{did}/dav/resync"' in banner
    assert "/arborescence" not in banner


def test_every_warning_names_its_button():
    assert (set(dossiers_routes._DETAIL_WARNING_ACTIONS)
            == set(dossiers_routes._DETAIL_WARNINGS))
    assert set(dossiers_routes._DETAIL_WARNING_ACTIONS.values()) <= {
        "dav_resync", "arborescence"}


# ══════════════════════════════════════════════════════════════════════
# 2. « Supprimer »
# ══════════════════════════════════════════════════════════════════════


def test_deleting_an_empty_dossier_journals_one_row_and_goes_to_the_list(
        client, db, did, events):
    """Its eighteen folders leave with it — ONE audit row, never nineteen
    (models/audit_event: one row per atomic deletion)."""
    assert len(_folders_of(db, did)) == 18
    resp = client.post(f"/dossiers/{did}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dossiers/")
    assert db.peek(f"dossiers/{did}") is None
    assert _folders_of(db, did) == {}
    rows = _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["entity_type"] == "dossier"
    assert rows[0]["entity_id"] == did
    assert rows[0]["snapshot_min"] == {"title": "2026-001", "status": "actif"}
    assert ("deleted", did, {"folders_deleted": 18}) in events


def test_a_refused_delete_lands_back_on_the_dossier_with_its_reason(
        client, db, did, events):
    db.seed("documents/doc1", {"id": "doc1", "dossier_id": did})
    resp = client.post(f"/dossiers/{did}/delete")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        f"/dossiers/{did}?erreur=suppression_contenu")
    assert db.peek(f"dossiers/{did}") is not None
    assert len(_folders_of(db, did)) == 18
    assert _audit_rows(db) == [] and events == []
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Suppression refusée : ce dossier contient encore des éléments" in page


def test_a_refused_htmx_delete_is_a_2xx_hx_redirect_never_a_422(
        client, db, did):
    db.seed("tasks/t1", {"id": "t1", "dossier_id": did})
    resp = client.post(f"/dossiers/{did}/delete",
                       headers={"HX-Request": "true"})
    assert 200 <= resp.status_code < 300
    assert resp.headers["HX-Redirect"].endswith(
        f"/dossiers/{did}?erreur=suppression_contenu")


def test_an_unreadable_content_says_so(client, db, did, monkeypatch):
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "notes" for f in sq.from_):
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    resp = client.post(f"/dossiers/{did}/delete")
    assert resp.headers["Location"].endswith("erreur=suppression_lecture")
    monkeypatch.setattr(server, "run_query", real)
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert ("Le contenu du dossier n&#39;a pas pu être vérifié" in page
            or "Le contenu du dossier n'a pas pu être vérifié" in page)


def test_a_failed_commit_says_so(client, db, did):
    def hook(info):
        if ("delete", f"dossiers/{did}") in info.ops:
            raise gexc.PermissionDenied("injected")

    remove = db.add_commit_hook(hook)
    try:
        resp = client.post(f"/dossiers/{did}/delete")
    finally:
        remove()
    assert resp.headers["Location"].endswith("erreur=suppression_echec")
    assert _audit_rows(db) == []


def _lose_the_delete(monkeypatch, db, did, *, lands: bool,
                     failed_rereads: int) -> dict:
    """The dossier's delete commit RAISES — after the server applied it
    (``lands``) or before — and the next *failed_rereads* plain reads of the
    dossier fail: the model's own re-read first, then the route's."""
    server = db._fake_server
    real_commit = server.commit
    real_get = server.batch_get_documents
    state = {"raised": False, "rereads": 0, "route_rereads": 0}

    def commit(request, metadata=None, **kwargs):
        names = {server.rel(getattr(w, "_pb", w).delete)
                 for w in request.get("writes") or []
                 if getattr(getattr(w, "_pb", w), "delete", "")}
        if not state["raised"] and f"dossiers/{did}" in names:
            state["raised"] = True
            if lands:
                real_commit(request, metadata=metadata, **kwargs)
                raise gexc.DeadlineExceeded("answer lost")
            raise gexc.DeadlineExceeded("injected commit failure")
        return real_commit(request, metadata=metadata, **kwargs)

    def batch_get(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if (state["raised"] and not request.get("transaction")
                and f"dossiers/{did}" in names):
            state["rereads"] += 1
            if state["rereads"] <= failed_rereads:
                raise gexc.ServiceUnavailable("injected re-read failure")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", commit)
    monkeypatch.setattr(server, "batch_get_documents", batch_get)
    return state


@pytest.fixture
def dav_teardown(monkeypatch):
    seen = []
    monkeypatch.setattr(dossiers_routes, "clear_tombstones",
                        lambda name: seen.append(("clear", name)))
    monkeypatch.setattr(dossiers_routes, "delete_sync_state",
                        lambda name: seen.append(("state", name)))
    return seen


def test_a_delete_that_landed_unseen_by_the_model_is_a_success(
        client, db, did, events, dav_teardown, monkeypatch):
    """The commit applied, its answer lost, and the MODEL's re-read failed:
    « echec ». The route asks the store once more, finds the dossier gone,
    and takes the success branch — one trail row from the staged snapshot,
    the event, the DAV teardown, the list. The old route said « rien n'a été
    supprimé » over a dossier that WAS deleted, and lost its trail row."""
    state = _lose_the_delete(monkeypatch, db, did, lands=True, failed_rereads=1)
    resp = client.post(f"/dossiers/{did}/delete")
    assert state["raised"] and state["rereads"] == 2  # model, then route
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/dossiers/")
    assert db.peek(f"dossiers/{did}") is None and _folders_of(db, did) == {}
    rows = _audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["entity_type"] == "dossier" and rows[0]["entity_id"] == did
    assert rows[0]["snapshot_min"] == {"title": "2026-001", "status": "actif"}
    assert ("deleted", did, {"folders_deleted": 18}) in events
    assert dav_teardown == [("clear", f"dossier:{did}"),
                            ("state", f"dossier:{did}")]


def test_a_failed_delete_the_route_rechecks_stays_suppression_echec(
        client, db, did, events, dav_teardown, monkeypatch):
    """Nothing applied, the model's re-read failed: the route's own re-read
    finds the dossier still there — « rien n'a été supprimé » is then TRUE."""
    calls = []
    real = dossiers_routes.get_dossier_strict
    monkeypatch.setattr(dossiers_routes, "get_dossier_strict",
                        lambda i: calls.append(i) or real(i))
    state = _lose_the_delete(monkeypatch, db, did, lands=False, failed_rereads=1)
    resp = client.post(f"/dossiers/{did}/delete")
    assert state["raised"] and calls == [did]
    assert resp.headers["Location"].endswith(
        f"/dossiers/{did}?erreur=suppression_echec")
    assert db.peek(f"dossiers/{did}") is not None
    assert len(_folders_of(db, did)) == 18
    assert _audit_rows(db) == [] and events == [] and dav_teardown == []


@pytest.mark.parametrize("lands", [True, False])
def test_an_unknowable_delete_says_so_never_rien_supprime(
        client, db, did, events, dav_teardown, monkeypatch, lands):
    """Both re-reads fail: whether it landed is UNKNOWN. The page says so —
    never « rien n'a été supprimé », never a trail row it cannot vouch for."""
    _lose_the_delete(monkeypatch, db, did, lands=lands, failed_rereads=2)
    resp = client.post(f"/dossiers/{did}/delete")
    assert resp.headers["Location"].endswith(
        f"/dossiers/{did}?erreur=suppression_incertaine")
    assert _audit_rows(db) == [] and events == [] and dav_teardown == []
    text = dossiers_routes._DETAIL_ERRORS["suppression_incertaine"]
    assert text == ("La suppression n'a peut-être pas abouti — rechargez la "
                    "page pour le vérifier.")
    assert "rien n'a été supprimé" not in text
    if not lands:
        page = client.get(resp.headers["Location"]).get_data(as_text=True)
        assert str(escape(text)) in page


def test_an_unknowable_htmx_delete_is_a_2xx_hx_redirect(
        client, db, did, monkeypatch):
    _lose_the_delete(monkeypatch, db, did, lands=False, failed_rereads=2)
    resp = client.post(f"/dossiers/{did}/delete", headers={"HX-Request": "true"})
    assert 200 <= resp.status_code < 300
    assert resp.headers["HX-Redirect"].endswith("erreur=suppression_incertaine")


def test_a_dossier_gone_without_any_staged_delete_is_not_journaled(
        client, db, did, events, monkeypatch):
    """« echec » with NO staged snapshot (no attempt of this call reached
    its delete) and the dossier gone: someone else deleted it. The list —
    and no trail row this call cannot vouch for."""
    def fake_delete(dossier_id):
        return False, "x", {"code": dossier_model.DELETE_FAILED,
                            "dossier": None, "folders": []}

    monkeypatch.setattr(dossiers_routes, "delete_dossier", fake_delete)
    monkeypatch.setattr(dossiers_routes, "get_dossier_strict", lambda i: None)
    resp = client.post(f"/dossiers/{did}/delete")
    assert resp.headers["Location"].endswith("/dossiers/")
    assert _audit_rows(db) == [] and events == []


@pytest.mark.parametrize("htmx", [False, True])
def test_deleting_an_unknown_dossier_goes_to_the_list(client, db, htmx):
    headers = {"HX-Request": "true"} if htmx else {}
    resp = client.post("/dossiers/a0000000-0000-4000-8000-000000000000/delete",
                       headers=headers)
    target = resp.headers["HX-Redirect"] if htmx else resp.headers["Location"]
    assert target.endswith("/dossiers/")
    assert _audit_rows(db) == []


def test_the_delete_dialog_tells_the_truth(client, db, did):
    page = client.get(f"/dossiers/{did}").get_data(as_text=True)
    assert "Toutes les données" not in page
    assert ("Un dossier ne se supprime que s&#39;il est vide" in page
            or "Un dossier ne se supprime que s'il est vide" in page)
    assert "Ses dossiers de classement vides sont supprimés avec lui." in page


@pytest.mark.parametrize("code", ["suppression_contenu", "suppression_lecture",
                                  "suppression_echec", "suppression_incertaine",
                                  "arborescence"])
def test_each_error_code_renders_a_red_banner(client, db, did, code):
    page = client.get(f"/dossiers/{did}?erreur={code}").get_data(as_text=True)
    assert str(escape(dossiers_routes._DETAIL_ERRORS[code])) in page
    assert 'bg-red-50 border border-red-200 rounded-lg" role="alert"' in page


def test_the_tree_error_banner_promises_nothing_about_what_was_written():
    """A refusal can follow a commit whose answer was lost (the engine's
    re-read can fail too): « rien n'a été modifié » could be false. What is
    always true is that a second run never duplicates a folder."""
    text = dossiers_routes._DETAIL_ERRORS["arborescence"]
    assert text == (
        "L'arborescence par défaut n'a pas pu être créée ou complétée — "
        "réessayez dans un instant ; les dossiers déjà en place ne sont "
        "jamais dupliqués.")
    assert "rien n'a été modifié" not in text


def test_the_complete_message_never_says_nothing_was_modified():
    """« arborescence_complete » is also what a run whose commit RAISED and
    then re-planned nothing left answers — a run that DID write."""
    text = dossiers_routes._DETAIL_MESSAGES["arborescence_complete"]
    assert "rien n'a été modifié" not in text
    assert "est en place" in text


def test_a_tree_commit_that_landed_unseen_is_complete_and_says_nothing_false(
        client, db, monkeypatch):
    """The engine's commit applies, its answer is lost; its re-plan finds
    nothing left to write and answers success — the route's
    « arborescence_complete », whose text must be true of a run that wrote."""
    did = _bare_dossier()
    server = db._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        if state["armed"]:
            state["armed"] = False
            raise gexc.DeadlineExceeded("answer lost")
        return response

    monkeypatch.setattr(server, "commit", commit)
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert not state["armed"]
    assert resp.headers["Location"].endswith("message=arborescence_complete")
    assert len(_folders_of(db, did)) == 18
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "rien n&#39;a été modifié" not in page
    assert "rien n'a été modifié" not in page


@pytest.mark.parametrize("query, table", [
    ("message=arborescence", "_DETAIL_MESSAGES"),
    ("message=arborescence_rangee", "_DETAIL_MESSAGES"),
    ("message=arborescence_rangee_projets", "_DETAIL_MESSAGES"),
    ("message=arborescence_rangee_portail", "_DETAIL_MESSAGES"),
    ("message=arborescence_complete", "_DETAIL_MESSAGES"),
    ("avertissement=arborescence", "_DETAIL_WARNINGS"),
    ("avertissement=arborescence_partielle", "_DETAIL_WARNINGS"),
])
def test_each_tree_code_renders_its_text(client, db, did, query, table):
    code = query.split("=", 1)[1]
    text = getattr(dossiers_routes, table)[code]
    page = client.get(f"/dossiers/{did}?{query}").get_data(as_text=True)
    assert str(escape(text)) in page


# ══════════════════════════════════════════════════════════════════════
# 3. « Créer l'arborescence par défaut »
# ══════════════════════════════════════════════════════════════════════


def test_the_button_creates_the_tree_then_a_second_click_writes_nothing(
        client, db):
    did = _bare_dossier()
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        f"/dossiers/{did}?tab=documents&message=arborescence")
    assert len(_folders_of(db, did)) == 18
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "L&#39;arborescence par défaut est en place" in page \
        or "L'arborescence par défaut est en place" in page

    db.reset_logs()
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("message=arborescence_complete")
    assert db.commits == []


def test_the_button_ranges_an_old_root_projets(client, db):
    did = _bare_dossier()
    db.seed("folders/vieux", {
        "id": "vieux", "dossier_id": did, "name": "Projets",
        "parent_folder_id": None, "order": 0,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    })
    resp = client.post(f"/dossiers/{did}/arborescence")
    # Only « Projets » moved: the banner must not say « Reçus du portail »
    # was ranged too (it said both, whatever moved).
    assert resp.headers["Location"].endswith("message=arborescence_rangee_projets")
    stored = db.peek("folders/vieux")
    assert stored["parent_folder_id"] == folder_model.node_folder_id(did, "interne")
    assert stored["system_role"] == "projets"
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    text = dossiers_routes._DETAIL_MESSAGES["arborescence_rangee_projets"]
    assert str(escape(text)) in page
    assert "Reçus du portail" not in text and "« Autres »" not in text


def _seed_legacy_root(db, did, fid, name):
    db.seed(f"folders/{fid}", {
        "id": fid, "dossier_id": did, "name": name,
        "parent_folder_id": None, "order": 0,
        "created_at": datetime(2025, 1, 1, tzinfo=UTC),
    })


def test_the_button_ranges_an_old_root_portail_alone(client, db):
    did = _bare_dossier()
    _seed_legacy_root(db, did, "vieux-portail", "Reçus du portail")
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("message=arborescence_rangee_portail")
    stored = db.peek("folders/vieux-portail")
    assert stored["parent_folder_id"] == folder_model.node_folder_id(did, "autres")
    assert stored["system_role"] == "portail"
    text = dossiers_routes._DETAIL_MESSAGES["arborescence_rangee_portail"]
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert str(escape(text)) in page
    assert "Projets" not in text and "« Interne »" not in text


def test_the_button_ranging_both_says_both(client, db):
    did = _bare_dossier()
    _seed_legacy_root(db, did, "vieux-projets", "Projets")
    _seed_legacy_root(db, did, "vieux-portail", "Reçus du portail")
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("message=arborescence_rangee")
    assert db.peek("folders/vieux-projets")["parent_folder_id"] == \
        folder_model.node_folder_id(did, "interne")
    assert db.peek("folders/vieux-portail")["parent_folder_id"] == \
        folder_model.node_folder_id(did, "autres")


@pytest.mark.parametrize("relocated, code", [
    (["projets", "portail"], "arborescence_rangee"),
    (["portail", "projets"], "arborescence_rangee"),
    (["projets"], "arborescence_rangee_projets"),
    (["portail"], "arborescence_rangee_portail"),
    (["factures"], ""),
    ([], ""),
])
def test_the_relocation_code_names_what_actually_moved(relocated, code):
    assert dossiers_routes._relocation_message_code(relocated) == code
    if code:
        assert code in dossiers_routes._DETAIL_MESSAGES


def test_a_refused_tree_is_an_error_banner(client, db, monkeypatch):
    did = _bare_dossier()
    monkeypatch.setattr(folder_model, "ensure_default_tree",
                        lambda dossier_id, **kw: (None, [folder_model.READ_ERROR]))
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("tab=documents&erreur=arborescence")


def test_a_partly_placed_tree_warns_with_the_button(client, db, monkeypatch):
    did = _bare_dossier()

    def partial(dossier_id, **kw):
        report = folder_model.TreeReport()
        report.blocked.append(("projets", "relocation_duplicate"))
        return report, []

    monkeypatch.setattr(folder_model, "ensure_default_tree", partial)
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith(
        "tab=documents&avertissement=arborescence_partielle")
    banner = _amber_banner(client.get(resp.headers["Location"]).get_data(as_text=True))
    assert f'action="/dossiers/{did}/arborescence"' in banner


def test_the_partial_warning_is_true_of_a_real_blocked_tree(client, db):
    """The REAL engine, a real obstacle: an old root « Projets » that cannot
    be ranged under « Interne », which already holds a « Projets ». What the
    banner says is checked against the store: the old folder was not moved
    (« créés ou RANGÉS » — the old text spoke only of folders not
    « placés »), the others ARE in place, and renaming the obstacle then
    clicking again completes the tree."""
    did = _bare_dossier()
    _seed_legacy_root(db, did, "interne-main", "Interne")
    db.seed("folders/obstacle", {
        "id": "obstacle", "dossier_id": did, "name": "Projets",
        "parent_folder_id": "interne-main", "order": 0,
        "created_at": datetime(2025, 6, 1, tzinfo=UTC)})
    db.seed("folders/vieux", {
        "id": "vieux", "dossier_id": did, "name": "Projets",
        "parent_folder_id": None, "order": 0,
        "created_at": datetime(2024, 1, 1, tzinfo=UTC)})

    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith(
        "tab=documents&avertissement=arborescence_partielle")
    assert db.peek("folders/vieux")["parent_folder_id"] is None  # not ranged
    # « Les autres sont en place » — every other node of the tree is stored.
    for key in ("mandat", "factures", "debourses", "procedures", "pieces",
                "correspondance", "autres", "portail"):
        assert db.peek(f"folders/{folder_model.node_folder_id(did, key)}"), key
    banner = _amber_banner(client.get(resp.headers["Location"]).get_data(as_text=True))
    text = dossiers_routes._DETAIL_WARNINGS["arborescence_partielle"]
    assert str(escape(text)) in banner
    assert "créés ou rangés à leur place" in text
    assert "Les autres sont en place." in text
    assert "cliquez de nouveau" in text

    # « Renommez … puis cliquez de nouveau » — and it IS completed.
    renamed, errors, _changed = folder_model.rename_folder(
        did, "obstacle", "Projets (ancien)")
    assert errors == [], errors
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("message=arborescence_rangee_projets")
    assert db.peek("folders/vieux")["parent_folder_id"] == "interne-main"


def test_an_unreadable_dossier_is_an_error_never_the_list(client, db, monkeypatch):
    did = _bare_dossier()
    server = db._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kwargs):
        names = [server.doc_rel(n) for n in request["documents"]]
        if f"dossiers/{did}" in names:
            raise gexc.ServiceUnavailable("injected")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", failing)
    db.reset_logs()
    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("erreur=arborescence")
    assert db.commits == []


def test_the_button_on_an_unknown_dossier_goes_to_the_list(client, db):
    resp = client.post("/dossiers/a0000000-0000-4000-8000-000000000000/arborescence")
    assert resp.headers["Location"].endswith("/dossiers/")
    assert db.peek_collection("folders") == {}


def test_the_arborescence_route_is_a_post_behind_login(db):
    from flask import Blueprint
    from flask import Flask as _F
    app = _F(__name__)
    app.secret_key = "t"
    auth_stub = Blueprint("auth", __name__, url_prefix="/auth")
    auth_stub.add_url_rule("/login", "login", lambda: "login")
    app.register_blueprint(auth_stub)
    app.register_blueprint(dossiers_routes.dossiers_bp)
    methods = {r.rule: r.methods for r in app.url_map.iter_rules()
               if r.rule.endswith("/arborescence")}
    assert methods == {"/dossiers/<dossier_id>/arborescence": {"POST", "OPTIONS"}}

    did = _bare_dossier()
    resp = app.test_client().post(f"/dossiers/{did}/arborescence")
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]
    assert _folders_of(db, did) == {}


# ══════════════════════════════════════════════════════════════════════
# 4. L'onglet Fichiers n'offre le bouton que quand il reste à faire
# ══════════════════════════════════════════════════════════════════════


def _tab(client, did) -> str:
    resp = client.get(f"/dossiers/{did}/tab/documents")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_the_tab_offers_the_button_on_a_dossier_without_its_tree(client, db):
    did = _bare_dossier()
    html = _tab(client, did)
    form = re.search(
        r'<form method="post" action="/dossiers/[^"]+/arborescence">(.*?)</form>',
        html, re.S)
    assert form, "the button is missing"
    assert 'name="csrf_token" value="tok"' in form.group(1)
    assert "Créer l&#39;arborescence par défaut" in form.group(1) \
        or "Créer l'arborescence par défaut" in form.group(1)


def test_the_tab_hides_the_button_once_the_tree_is_complete(client, db, did):
    html = _tab(client, did)
    assert "/arborescence" not in html
    # The root folders, alphabetically — as list_folders sorts them.
    names = re.findall(
        r'<span class="text-sm font-medium text-gray-900 truncate block">([^<]+)</span>',
        html)
    assert names == ["Autres", "Correspondance", "Interne", "Mandat", "Procédures"]


def test_an_ordinary_extra_root_folder_offers_nothing(client, db, did):
    db.seed("folders/perso", {
        "id": "perso", "dossier_id": did, "name": "Brouillons",
        "parent_folder_id": None, "order": 0})
    assert "/arborescence" not in _tab(client, did)


def test_the_tab_offers_the_button_to_range_an_old_root_projets(client, db):
    """A dossier completed WITHOUT relocate (a generation) keeps its old root
    « Projets » where it is: the tree is not fully in place, and the tab
    offers the button that ranges it."""
    did = _bare_dossier()
    fid = folder_model.system_folder_id(did, "projets")
    db.seed(f"folders/{fid}", {
        "id": fid, "dossier_id": did, "name": "Projets",
        "parent_folder_id": None, "order": 0, "system_role": "projets"})
    report, errors = folder_model.ensure_default_tree(did)
    assert errors == [] and not report.relocated
    assert db.peek(f"folders/{fid}")["parent_folder_id"] is None
    assert "/arborescence" in _tab(client, did)

    resp = client.post(f"/dossiers/{did}/arborescence")
    assert resp.headers["Location"].endswith("message=arborescence_rangee_projets")
    assert "/arborescence" not in _tab(client, did)


def test_a_failed_folder_read_shows_no_button_and_still_renders(
        client, db, monkeypatch):
    did = _bare_dossier()
    server = db._fake_server
    real = server.run_query

    def failing(request, metadata=None, **kwargs):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "folders" for f in sq.from_):
            raise gexc.ServiceUnavailable("injected")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    html = _tab(client, did)
    assert "/arborescence" not in html
    assert "Fichiers" in html


# ══════════════════════════════════════════════════════════════════════
# 5. Les classes du balisage existent dans l'artefact compilé
# ══════════════════════════════════════════════════════════════════════


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_new_markup_uses_only_compiled_classes(client, db, did):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    bare = _bare_dossier("2026-060")
    snippets = [
        _amber_banner(client.get(
            f"/dossiers/{did}?avertissement=arborescence").get_data(as_text=True)),
        re.search(r'<form method="post" action="/dossiers/[^"]+/arborescence">.*?</form>',
                  _tab(client, bare), re.S).group(0),
    ]
    page = client.get(f"/dossiers/{did}?message=arborescence&erreur=suppression_contenu"
                      ).get_data(as_text=True)
    for marker in ("L&#39;arborescence par défaut est en place",
                   "Suppression refusée", "Un dossier ne se supprime"):
        i = page.index(marker)
        snippets.append(page[max(0, i - 300):i + 300])
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    classes = {c for s in snippets
               for block in re.findall(r'class="([^"]+)"', s)
               for c in block.split()}
    assert {"text-xs", "font-medium", "text-gray-500", "hover:text-gray-700"} <= classes
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent


# ── Every creator of a dossier gives it its tree (derived) ──────────────
#
# A new dossier gets its eighteen folders from the CALLER of
# ``dossier_model.create_dossier``, after the commit — never from the model
# (a raise after the commit would read as a failed creation). So the rule
# lives at every call site, and the sweep below finds them in the source:
# a third creator (a script, a Réception « ouverture ») that skips the tree,
# or calls it outside a ``try``, fails here instead of shipping dossiers
# without their folders.


def _calls_named(node: ast.AST, name: str) -> list:
    """Every call, under *node*, of *name* — bare or as an attribute."""
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and (
        (isinstance(c.func, ast.Name) and c.func.id == name)
        or (isinstance(c.func, ast.Attribute) and c.func.attr == name))]


def _tree_in_try(node: ast.AST) -> list:
    """The ``ensure_default_tree`` calls, under *node*, that sit in the BODY
    of a ``try`` (a raise there is caught, never after-commit)."""
    return [c for t in ast.walk(node) if isinstance(t, ast.Try)
            for stmt in t.body for c in _calls_named(stmt, "ensure_default_tree")]


def _creation_violations(source: str, label: str) -> tuple[list, list]:
    """``(callers, violations)`` for one module: each function calling
    ``create_dossier`` must, AFTER its ``if errors`` check, give the new
    dossier its tree — a call of ``ensure_default_tree`` inside a ``try``,
    or a call of a helper of the same module that makes one."""
    tree = ast.parse(source)
    functions = {f.name: f for f in ast.walk(tree)
                 if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    safe_helpers = {name for name, f in functions.items() if _tree_in_try(f)}
    callers, violations = [], []
    for fn in functions.values():
        if fn.name == "create_dossier":
            continue            # the model itself, never its caller
        creations = _calls_named(fn, "create_dossier")
        if not creations:
            continue
        where = f"{label}:{fn.name}"
        callers.append(where)
        created_at = min(c.lineno for c in creations)
        # The errors check: an ``if`` testing a name bound by the creation.
        bound = {n.id for a in ast.walk(fn) if isinstance(a, ast.Assign)
                 and any(c in creations for c in ast.walk(a.value))
                 for t in a.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        checks = [i.lineno for i in ast.walk(fn) if isinstance(i, ast.If)
                  and i.lineno > created_at
                  and {n.id for n in ast.walk(i.test) if isinstance(n, ast.Name)} & bound]
        if not checks:
            violations.append(f"{where}: no error check after create_dossier")
            continue
        after = min(checks)
        tree_calls = [c for c in _tree_in_try(fn) if c.lineno > after]
        tree_calls += [c for h in safe_helpers - {fn.name}
                       for c in _calls_named(fn, h) if c.lineno > after]
        if not tree_calls:
            violations.append(
                f"{where}: no ensure_default_tree in a try after the error check")
    return callers, violations


def test_every_creator_of_a_dossier_gives_it_its_default_tree():
    callers, violations, scanned = [], [], 0
    for path in sorted(_ATHENA.rglob("*.py")):
        rel = path.relative_to(_ATHENA).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")) or "/site-packages/" in rel:
            continue
        scanned += 1
        found, bad = _creation_violations(path.read_text(encoding="utf-8"), rel)
        callers += found
        violations += bad
    assert scanned > 50, "the sweep did not walk the source tree"
    # Non-vacuous: the two creators of today are found — more may join.
    assert {"routes/dossiers.py:dossier_create",
            "mcp/handlers.py:_create_dossier_impl"} <= set(callers), callers
    assert violations == []


_DIRECT = """
def f(d):
    doc, errors = create_dossier(d)
    if errors:
        return errors
    try:
        ensure_default_tree(doc["id"])
    except Exception:
        pass
"""

_THROUGH_A_HELPER = """
def h(i):
    try:
        folder.ensure_default_tree(i)
    except Exception:
        return ["x"]

def f(d):
    doc, errors = dossier_model.create_dossier(d)
    if errors:
        raise ValueError
    return h(doc["id"])
"""

_NO_TREE = """
def f(d):
    doc, errors = create_dossier(d)
    if errors:
        return errors
    return doc
"""

_OUTSIDE_A_TRY = """
def f(d):
    doc, errors = create_dossier(d)
    if errors:
        return errors
    ensure_default_tree(doc["id"])
"""

_BEFORE_THE_CHECK = """
def f(d):
    doc, errors = create_dossier(d)
    try:
        ensure_default_tree(doc["id"])
    except Exception:
        pass
    if errors:
        return errors
"""


@pytest.mark.parametrize("snippet, flagged", [
    (_DIRECT, False),               # today's route shape
    (_THROUGH_A_HELPER, False),     # today's connector shape
    (_NO_TREE, True),
    (_OUTSIDE_A_TRY, True),         # its raise would follow the commit
    (_BEFORE_THE_CHECK, True),      # it would run for a refused dossier
])
def test_the_creator_sweep_flags_what_it_should(snippet, flagged):
    callers, violations = _creation_violations(snippet, "x.py")
    assert callers == ["x.py:f"]
    assert bool(violations) is flagged, violations
