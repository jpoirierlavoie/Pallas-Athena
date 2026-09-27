"""Les formulaires d'édition refusent d'écraser une version qu'ils n'ont pas vue
(plan, règle transversale 11 ; décision D9).

Chaque formulaire d'édition d'un enregistrement que le connecteur écrit porte
l'etag de la version qu'il affiche. La route le remet au modèle, qui ne
valide la sauvegarde que si l'etag stocké est TOUJOURS celui-là. Sinon la page
revient — en 200, comme toute nouvelle saisie — avec le bandeau ambre, les
valeurs SOUMISES et l'etag ACTUEL : la sauvegarde suivante est alors un
écrasement délibéré, fait après avoir lu le bandeau.

Tout ici passe par les VRAIES routes, les VRAIS gabarits et les VRAIS
modèles, au-dessus du faux Firestore partagé (le client est le vrai). Une
« écriture rivale » est celle d'un autre processus — le téléphone, un
second onglet, le connecteur — posée directement dans le magasin entre la
lecture du formulaire et sa soumission ; on relit ensuite ce qui est
STOCKÉ, jamais un dictionnaire remis à un faux.

La carte des formulaires est DÉRIVÉE : chaque entité qu'un outil
d'écriture du connecteur MODIFIE (un mutateur autre que ``create_`` atteint
depuis son gestionnaire, suivi dans ``mcp/handlers.py``) doit avoir son
formulaire web ici, ou figurer dans ``PENDING`` avec le lot qui le livrera.
``PENDING`` ne peut que rétrécir ; il doit être VIDE au lot 5.
"""

import ast
import os
import pathlib
import html as html_module
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.tools as tools
    from models import concurrency
    from models import document as document_model
    from models import dossier as dossier_model
    from models import expense as expense_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import protocol as protocol_model
    from models import task as task_model
    from models import time_entry as time_entry_model
    import routes.documents as documents_routes
    import routes.dossiers as dossiers_routes
    import routes.hearings as hearings_routes
    import routes.notes as notes_routes
    import routes.parties as parties_routes
    import routes.protocols as protocols_routes
    import routes.tasks as tasks_routes
    import routes.time_expenses as time_expenses_routes
    from routes import edit_conflict

from flask import Flask, render_template_string  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
RIVAL_ETAG = "11111111-2222-4333-8444-555555555555"
BANNER = "Cet élément a été modifié entre-temps."
_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')


# ══════════════════════════════════════════════════════════════════════
# Le banc : l'application, le magasin, un dossier
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module that holds the Firestore client — the models and the
    DAV sync layer (a bump reaches it). Derived, so a model a route starts
    to read tomorrow cannot silently reach the mocked client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_fake_modules())


@pytest.fixture
def client(db):
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
               hearings_routes.hearings_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


@pytest.fixture
def dossier_id(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return doc["id"]


def _etags(html: str) -> list[str]:
    return _ETAG_INPUT.findall(html)


def _rival(db, path: str, field: str, value) -> None:
    """Another process writes the record: a changed field, a new etag."""
    doc = db.peek(path)
    doc[field] = value
    doc["etag"] = RIVAL_ETAG
    db.external_write(path, doc)


# ══════════════════════════════════════════════════════════════════════
# Les formulaires — un cas par formulaire d'édition
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FormCase:
    id: str
    entity: str                     # the models module the form edits
    collection: str
    seed: Callable                  # (db, dossier_id) -> record id
    edit_url: Callable[[str], str]
    post_url: Callable[[str], str]
    create_url: Optional[str]       # the matching create form, if any
    form: Callable                  # (dossier_id, marker) -> valid payload
    invalid: Callable               # (dossier_id, marker) -> refused payload
    field: str                      # the stored field `form` writes
    marker: str                     # what `form` writes there
    rival_value: str = "RIVAL-9Z"
    # The HTML proving the SUBMITTED value survived a re-render, when the
    # bare marker cannot: every phase form prints every sub-code anyway
    # (the selector embeds the whole taxonomy), so there only the
    # selector's own state says what was kept.
    kept: Optional[str] = None

    @property
    def evidence(self) -> str:
        return self.kept or self.marker


def _seed_partie(db, dossier_id):
    doc, errors = partie_model.create_partie({
        "type": "individual", "contact_role": "client",
        "first_name": "Jean", "last_name": "Tremblay", "notes": "Premier"})
    assert errors == [], errors
    return doc["id"]


def _partie_form(dossier_id, notes):
    return {"type": "individual", "contact_role": "client",
            "first_name": "Jean", "last_name": "Tremblay", "notes": notes}


def _seed_dossier(db, dossier_id):
    return dossier_id


def _dossier_form(dossier_id, sommaire, *, title="Tremblay c. Lavoie"):
    return {
        "file_number": "2026-001", "title": title, "sommaire": sommaire,
        "clients_json": ('[{"id": "p1", "name": "Jean Tremblay", '
                         '"roles": ["demandeur"]}]'),
        "status": "actif", "forum_type": "judiciaire",
        "mandate_type": "judiciaire", "fee_type": "hourly",
    }


def _seed_time_entry(db, dossier_id):
    doc, errors = time_entry_model.create_time_entry({
        "dossier_id": dossier_id, "date": DT, "description": "Premier",
        "hours": 1.5, "rate": 30000, "billable": True})
    assert errors == [], errors
    return doc["id"]


def _time_form(dossier_id, description, *, hours="1.5"):
    return {"dossier_id": dossier_id, "date": "2026-03-04",
            "description": description, "hours": hours, "rate": "300",
            "billable": "on"}


def _seed_expense(db, dossier_id):
    doc, errors = expense_model.create_expense({
        "dossier_id": dossier_id, "date": DT, "description": "Premier",
        "amount": 5000, "category": "timbre_judiciaire", "taxable": False})
    assert errors == [], errors
    return doc["id"]


def _expense_form(dossier_id, description, *, amount="50"):
    return {"dossier_id": dossier_id, "date": "2026-03-04",
            "description": description, "category": "timbre_judiciaire",
            "amount": amount}


def _seed_note(db, dossier_id):
    doc, errors = note_model.create_note({
        "dossier_id": dossier_id, "title": "Recherche",
        "content": "Premier jet.", "category": "recherche"})
    assert errors == [], errors
    return doc["id"]


def _note_form(dossier_id, content, *, title="Recherche"):
    return {"dossier_id": dossier_id, "title": title, "content": content,
            "category": "recherche"}


def _seed_task(db, dossier_id):
    doc, errors = task_model.create_task(
        {"dossier_id": dossier_id, "title": "Préparer la requête",
         "description": "Premier"})
    assert errors == [], errors
    return doc["id"]


def _task_form(dossier_id, description, *, title="Préparer la requête"):
    return {"dossier_id": dossier_id, "title": title,
            "description": description, "priority": "normale",
            "status": "à_faire", "category": "autre"}


def _seed_document(db, dossier_id, **over):
    doc = {**document_model._default_doc(), "id": "doc1",
           "dossier_id": dossier_id, "display_name": "Lettre",
           "filename": "lettre.pdf", "file_type": "application/pdf",
           "category": "correspondance", "notes_internes": "Premier",
           "created_at": DT, "updated_at": DT, "etag": "e0"}
    doc.update(over)
    db.seed("documents/doc1", doc)
    return "doc1"


def _document_form(dossier_id, notes, *, category="correspondance"):
    return {"display_name": "Lettre", "category": category, "tags": "",
            "notes_internes": notes, "document_date": "",
            "analyse_presente": "1"}


def _time_phase_form(dossier_id, sous_phase):
    return {"phase": sous_phase.split("-")[0], "sous_phase": sous_phase}


def _seed_hearing(db, dossier_id):
    doc, errors = hearing_model.create_hearing({
        "dossier_id": dossier_id, "title": "Audience",
        "hearing_type": "audience", "status": "confirmée",
        "start_datetime": datetime(2026, 10, 15, 13, tzinfo=UTC),
        "end_datetime": datetime(2026, 10, 15, 14, tzinfo=UTC),
        "notes": "Premier"})
    assert errors == [], errors
    return doc["id"]


def _hearing_form(dossier_id, notes, *, title="Audience"):
    return {"dossier_id": dossier_id, "title": title, "notes": notes,
            "hearing_type": "audience", "status": "confirmée",
            "start_date": "2026-10-15", "start_time": "09:00",
            "end_time": "10:00", "reminder_minutes": "1440",
            "modalite": "présentiel"}


def _seed_protocol(db, dossier_id):
    doc, errors = protocol_model.create_protocol(
        dossier_id, "conventionnel", DT,
        {"title": "Protocole", "notes": "Premier"})
    assert errors == [], errors
    return doc["id"]


def _protocol_form(dossier_id, notes, *, status="actif"):
    return {"title": "Protocole", "notes": notes, "status": status,
            "start_date": "2026-03-04"}


# A step's record id is its path under « protocols »: the page that edits
# it is its PROTOCOL's detail page, where it is the only step (one inline
# form, one etag). A template step (mandatory) so that clearing its
# deadline is the refusal the invalid payload needs.
_STEP_DEADLINE = datetime(2099, 12, 1, tzinfo=UTC)


def _seed_protocol_step(db, dossier_id):
    pid = _seed_protocol(db, dossier_id)
    db.seed(f"protocols/{pid}/steps/s1", {
        **protocol_model._default_step(), "id": "s1", "order": 1,
        "title": "Réponse", "deadline_date": _STEP_DEADLINE,
        "deadline_offset_days": 15, "mandatory": True, "notes": "Premier",
        "date_confirmed": True, "created_at": DT, "updated_at": DT,
        "etag": "e-step-1",
    })
    return f"{pid}/steps/s1"


def _step_form(dossier_id, notes, *, deadline="2099-12-01"):
    return {"deadline_date": deadline, "notes": notes}


CASES = [
    FormCase(
        "partie", "partie", "parties", _seed_partie,
        lambda i: f"/parties/{i}/edit", lambda i: f"/parties/{i}",
        "/parties/new", _partie_form,
        lambda d, m: {**_partie_form(d, m), "last_name": ""},
        "notes", "SOUMIS-7Q4",
    ),
    FormCase(
        "dossier", "dossier", "dossiers", _seed_dossier,
        lambda i: f"/dossiers/{i}/edit", lambda i: f"/dossiers/{i}",
        "/dossiers/new", _dossier_form,
        lambda d, m: _dossier_form(d, m, title=""),
        "sommaire", "SOUMIS-7Q4",
    ),
    FormCase(
        "time_entry", "time_entry", "timeentries", _seed_time_entry,
        lambda i: f"/temps/{i}/edit", lambda i: f"/temps/{i}",
        "/temps/new", _time_form,
        lambda d, m: _time_form(d, m, hours="0"),
        "description", "SOUMIS-7Q4",
    ),
    FormCase(
        "time_entry_phase", "time_entry", "timeentries", _seed_time_entry,
        lambda i: f"/temps/{i}/phase", lambda i: f"/temps/{i}/phase",
        None, _time_phase_form,
        lambda d, m: {"phase": "PRE", "sous_phase": "CTS-02"},
        "sous_phase", "CTS-02", rival_value="PRE-01",
        kept="sousPhase: 'CTS-02'",
    ),
    FormCase(
        "expense", "expense", "expenses", _seed_expense,
        lambda i: f"/temps/depenses/{i}/edit",
        lambda i: f"/temps/depenses/{i}",
        "/temps/depenses/new", _expense_form,
        lambda d, m: _expense_form(d, m, amount="0"),
        "description", "SOUMIS-7Q4",
    ),
    FormCase(
        "expense_phase", "expense", "expenses", _seed_expense,
        lambda i: f"/temps/depenses/{i}/phase",
        lambda i: f"/temps/depenses/{i}/phase",
        None, _time_phase_form,
        lambda d, m: {"phase": "PRE", "sous_phase": "CTS-02"},
        "sous_phase", "CTS-02", rival_value="PRE-01",
        kept="sousPhase: 'CTS-02'",
    ),
    FormCase(
        "note", "note", "notes", _seed_note,
        lambda i: f"/notes/{i}/edit", lambda i: f"/notes/{i}",
        None, _note_form,
        lambda d, m: _note_form(d, m, title=""),
        "content", "SOUMIS-7Q4",
    ),
    FormCase(
        "task", "task", "tasks", _seed_task,
        lambda i: f"/taches/{i}/edit", lambda i: f"/taches/{i}",
        "/taches/new", _task_form,
        lambda d, m: _task_form(d, m, title=""),
        "description", "SOUMIS-7Q4",
    ),
    FormCase(
        "document", "document", "documents", _seed_document,
        lambda i: f"/documents/{i}/edit", lambda i: f"/documents/{i}/edit",
        None, _document_form,
        lambda d, m: _document_form(d, m, category="inventée"),
        "notes_internes", "SOUMIS-7Q4",
    ),
    # Lot 1a (L4): the hearing edit form. Like the protocol forms below,
    # no connector write modifies a hearing yet (update_hearing is lot
    # 1b's), so the derived map does not require it — wired first, as the
    # plan orders, and its cycle proved like every other.
    FormCase(
        "hearing", "hearing", "hearings", _seed_hearing,
        lambda i: f"/audiences/{i}/edit", lambda i: f"/audiences/{i}",
        "/audiences/new", _hearing_form,
        lambda d, m: _hearing_form(d, m, title=""),
        "notes", "SOUMIS-7Q4",
    ),
    # Lot 1a (L2): the protocol edit form and a step's inline form. No
    # connector write reaches a protocol yet (lot 1b), so these forms are
    # not REQUIRED by the derived map above — they are wired first, as the
    # plan orders (1a before 1b), and their cycle is proved like every other.
    FormCase(
        "protocol", "protocol", "protocols", _seed_protocol,
        lambda i: f"/protocoles/{i}/edit", lambda i: f"/protocoles/{i}",
        None, _protocol_form,
        lambda d, m: _protocol_form(d, m, status="inventé"),
        "notes", "SOUMIS-7Q4",
    ),
    FormCase(
        "protocol_step", "protocol", "protocols", _seed_protocol_step,
        lambda i: f"/protocoles/{i.split('/')[0]}",
        lambda i: f"/protocoles/{i.split('/')[0]}/steps/{i.split('/')[2]}",
        None, _step_form,
        lambda d, m: _step_form(d, m, deadline=""),
        "notes", "SOUMIS-7Q4",
    ),
]
_CASE_IDS = [c.id for c in CASES]


def _path(case: FormCase, record_id: str) -> str:
    return f"{case.collection}/{record_id}"


# ══════════════════════════════════════════════════════════════════════
# 1. La carte dérivée : chaque entité que le connecteur modifie a son
#    formulaire protégé — ou le lot qui le livrera
# ══════════════════════════════════════════════════════════════════════

# {models module: the lot whose (a) release wires its web edit form}. Only
# shrinks: every entry must still be reached by a connector write (below),
# and the plan requires this dict to be EMPTY by Lot 5.
PENDING: dict[str, str] = {
    # import_invoice creates an invoice (and flips its sources). The web
    # form that edits an invoice — the draft form /factures/<id>/brouillon
    # — is Lot 3a's (plan, Lot 3 : « Web etags on the time, expense, draft
    # and budget forms »).
    "invoice": "Lot 3",
}
_LOTS = ("Lot 1", "Lot 2", "Lot 3", "Lot 4", "Lot 5")

_MUTATOR = re.compile(
    r"^(create|update|set|record|append|void|reverse|clear|confirm|move|"
    r"delete|toggle|complete|attach|link|import)_"
)


def _handler_reach() -> dict[str, set[tuple[str, str]]]:
    """``{write tool: {(models module, mutator)}}`` — what each write
    handler reaches, following the names it references inside
    ``mcp/handlers.py`` (its delegates, the module-level tables whose
    lambdas hold a setter) down to ``<models alias>.<verb>_…``."""
    tree = ast.parse(pathlib.Path(handlers.__file__).read_text(encoding="utf-8"))
    aliases, top = {}, {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "models":
            for a in node.names:
                aliases[a.asname or a.name] = a.name
        elif isinstance(node, ast.FunctionDef):
            top[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign)
                      else [node.target]):
                if isinstance(t, ast.Name):
                    top[t.id] = node

    def reach(start: str) -> set[tuple[str, str]]:
        seen, stack, found = set(), [start], set()
        while stack:
            name = stack.pop()
            if name in seen or name not in top:
                continue
            seen.add(name)
            for sub in ast.walk(top[name]):
                if isinstance(sub, ast.Name) and sub.id in top:
                    stack.append(sub.id)
                elif (isinstance(sub, ast.Attribute)
                      and isinstance(sub.value, ast.Name)
                      and sub.value.id in aliases
                      and _MUTATOR.match(sub.attr)):
                    found.add((aliases[sub.value.id], sub.attr))
        return found

    return {t: reach(tools.TOOLS[t]["handler"]) for t in tools.WRITE_TOOLS}


def _edited_by_edit_tools() -> set[str]:
    reach = _handler_reach()
    return {module for t in tools.EDIT_TOOLS for module, _ in reach[t]}


def _modified_by_any_write() -> set[str]:
    """Entities whose EXISTING record some write tool rewrites — an
    ``update_``/``set_``/``record_``… mutator, not a ``create_``. This is
    wider than EDIT_TOOLS on purpose: ``append_to_note`` and
    ``complete_task`` are not edits in the MCP-hint sense, yet a stale web
    tab over the record they wrote would erase that write (review, Lot 0)."""
    return {module for pairs in _handler_reach().values()
            for module, verb in pairs if not verb.startswith("create_")}


_FORMS: dict[str, set[str]] = {}
for _c in CASES:
    _FORMS.setdefault(_c.entity, set()).add(_c.id)


def test_the_reach_is_derived_and_not_vacuous():
    reach = _handler_reach()
    assert ("partie", "update_partie") in reach["update_partie"]
    assert ("time_entry", "set_time_entry_phase") in reach["set_time_entry_phase"]
    assert ("note", "update_note") in reach["append_to_note"]
    assert ("task", "update_task") in reach["complete_task"]
    assert ("document", "record_analyse") in reach["record_document_analysis"]
    # Every write tool reaches at least one mutator: a handler the walker
    # cannot follow would otherwise vanish from the map in silence.
    assert all(reach.values()), [t for t, r in reach.items() if not r]


def test_every_entity_an_edit_tool_edits_has_its_web_form_or_its_lot():
    missing = _edited_by_edit_tools() - set(_FORMS) - set(PENDING)
    assert not missing, (
        f"the connector edits {sorted(missing)} but no web edit form of it "
        "carries expected_etag — a stale browser tab would silently erase "
        "the connector's write. Wire the form (routes/edit_conflict.py) or "
        "name its lot in PENDING."
    )


def test_every_record_a_write_tool_rewrites_has_its_web_form_or_its_lot():
    missing = _modified_by_any_write() - set(_FORMS) - set(PENDING)
    assert not missing, sorted(missing)


def test_pending_only_shrinks_and_names_a_lot():
    reached = _edited_by_edit_tools() | _modified_by_any_write()
    for entity, lot in PENDING.items():
        assert lot in _LOTS, (entity, lot)
        assert entity not in _FORMS, f"{entity} is wired: drop it from PENDING"
        assert entity in reached, f"{entity}: no write reaches it any more"


def test_complete_task_edits_a_task_whose_form_is_protected():
    """complete_task joined EDIT_TOOLS in the disclosure step (lot 0a); its
    record was already covered, so that move needed no new form — and the
    edit-tool map above now reaches it by membership."""
    assert "complete_task" in tools.EDIT_TOOLS
    assert "task" in _FORMS
    assert "task" in _edited_by_edit_tools()


# ══════════════════════════════════════════════════════════════════════
# 2. Le cycle, formulaire par formulaire
# ══════════════════════════════════════════════════════════════════════


def _open(client, db, case, dossier_id):
    """Seed the record, open its edit form; return (id, etag shown)."""
    record_id = case.seed(db, dossier_id)
    resp = client.get(case.edit_url(record_id))
    assert resp.status_code == 200, resp.status_code
    shown = _etags(resp.get_data(as_text=True))
    assert len(shown) == 1, shown
    return record_id, shown[0]


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_the_edit_form_carries_the_stored_etag(client, db, dossier_id, case):
    record_id, shown = _open(client, db, case, dossier_id)
    assert shown == db.peek(_path(case, record_id))["etag"]
    assert shown  # a real etag, not the legacy ''
    assert BANNER not in client.get(case.edit_url(record_id)).get_data(as_text=True)


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.create_url],
    ids=[c.id for c in CASES if c.create_url],
)
def test_a_create_form_carries_no_etag(client, db, dossier_id, case):
    resp = client.get(case.create_url)
    assert resp.status_code == 200
    assert _etags(resp.get_data(as_text=True)) == []


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_fresh_save_commits_and_moves_the_etag(client, db, dossier_id, case):
    record_id, shown = _open(client, db, case, dossier_id)
    resp = client.post(case.post_url(record_id), data={
        **case.form(dossier_id, case.marker), "expected_etag": shown})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = db.peek(_path(case, record_id))
    assert stored[case.field] == case.marker
    assert stored["etag"] != shown
    assert stored["updated_via"] == "web"


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_stale_save_writes_nothing_and_shows_the_banner(
    client, db, dossier_id, case
):
    record_id, shown = _open(client, db, case, dossier_id)
    path = _path(case, record_id)
    _rival(db, path, case.field, case.rival_value)
    before = db.peek(path)
    # The evidence is not on the form as it renders the stored record — or
    # finding it after the refusal would prove nothing.
    assert case.evidence not in (
        client.get(case.edit_url(record_id)).get_data(as_text=True))

    resp = client.post(case.post_url(record_id), data={
        **case.form(dossier_id, case.marker), "expected_etag": shown})

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BANNER in html
    # The submitted value is kept on the page…
    assert case.evidence in html
    # …the form now carries the CURRENT etag (the next save is deliberate)…
    assert _etags(html) == [RIVAL_ETAG]
    # …and the rival write survived intact: nothing was written.
    assert db.peek(path) == before
    assert db.peek(path)[case.field] == case.rival_value


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_saving_again_after_the_banner_is_a_deliberate_overwrite(
    client, db, dossier_id, case
):
    record_id, shown = _open(client, db, case, dossier_id)
    path = _path(case, record_id)
    _rival(db, path, case.field, case.rival_value)
    payload = case.form(dossier_id, case.marker)
    stale = client.post(case.post_url(record_id),
                        data={**payload, "expected_etag": shown})
    current = _etags(stale.get_data(as_text=True))[0]

    resp = client.post(case.post_url(record_id),
                       data={**payload, "expected_etag": current})
    assert resp.status_code == 302
    assert db.peek(path)[case.field] == case.marker


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_validation_error_keeps_the_submitted_etag(
    client, db, dossier_id, case
):
    record_id, shown = _open(client, db, case, dossier_id)
    path = _path(case, record_id)
    before = db.peek(path)

    resp = client.post(case.post_url(record_id), data={
        **case.invalid(dossier_id, case.marker), "expected_etag": shown})

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BANNER not in html
    # The ORIGINAL version keeps protecting the retry.
    assert _etags(html) == [shown]
    assert db.peek(path) == before


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_page_without_the_field_runs_no_check(client, db, dossier_id, case):
    """A tab opened before this deploy: the save behaves as it always did
    (last write wins) instead of failing on a field it could not carry."""
    record_id, _ = _open(client, db, case, dossier_id)
    path = _path(case, record_id)
    _rival(db, path, case.field, case.rival_value)

    resp = client.post(case.post_url(record_id),
                       data=case.form(dossier_id, case.marker))
    assert resp.status_code == 302
    assert db.peek(path)[case.field] == case.marker


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_legacy_refusal_re_renders_without_an_etag(
    client, db, dossier_id, case
):
    """…and its validation re-render stays on the legacy path: handing it
    the CURRENT etag would bless a view whose version nobody knows."""
    record_id, _ = _open(client, db, case, dossier_id)
    resp = client.post(case.post_url(record_id),
                       data=case.invalid(dossier_id, case.marker))
    assert resp.status_code == 200
    assert _etags(resp.get_data(as_text=True)) == []


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_a_malformed_etag_is_a_french_400(client, db, dossier_id, case):
    record_id, _ = _open(client, db, case, dossier_id)
    path = _path(case, record_id)
    before = db.peek(path)
    resp = client.post(case.post_url(record_id), data={
        **case.form(dossier_id, case.marker),
        "expected_etag": '"><script>x</script>'})
    assert resp.status_code == 400
    assert "Rien n'a été enregistré" in resp.get_data(as_text=True)
    assert db.peek(path) == before


_PHASE_CASES = [c for c in CASES if c.kept]
_RECAP_PHASE = re.compile(
    r"Phase actuelle</span>\s*<span[^>]*>\s*([^<]*?)\s*</span>")


def _recap_phase(html: str) -> str:
    found = _RECAP_PHASE.findall(html)
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("case", _PHASE_CASES, ids=[c.id for c in _PHASE_CASES])
def test_a_stale_phase_refusal_shows_the_phase_actually_stored(
    client, db, dossier_id, case
):
    """« Phase actuelle » is the one line of the recap the banner is about:
    another writer just set that phase. It read the SUBMITTED pair — the
    pair this save failed to write — while the store held the rival's."""
    from utils import phases

    record_id, shown = _open(client, db, case, dossier_id)
    _rival(db, _path(case, record_id), case.field, case.rival_value)
    html = client.post(case.post_url(record_id), data={
        **case.form(dossier_id, case.marker), "expected_etag": shown,
    }).get_data(as_text=True)
    assert BANNER in html
    assert _recap_phase(html) == phases.SOUS_PHASE_LABELS[case.rival_value]
    assert case.kept in html                 # the selector keeps the submission


@pytest.mark.parametrize("case", _PHASE_CASES, ids=[c.id for c in _PHASE_CASES])
def test_a_refused_phase_pair_is_not_shown_as_the_current_phase(
    client, db, dossier_id, case
):
    record_id, shown = _open(client, db, case, dossier_id)
    html = client.post(case.post_url(record_id), data={
        **case.invalid(dossier_id, case.marker), "expected_etag": shown,
    }).get_data(as_text=True)
    assert BANNER not in html
    assert _recap_phase(html) == "Non renseignée"   # what is stored
    assert "sousPhase: 'CTS-02'" in html            # what was submitted


# ══════════════════════════════════════════════════════════════════════
# 3. Le document : métadonnées et analyse, UNE unité
# ══════════════════════════════════════════════════════════════════════

_ANALYSE = {"sous_nature": "CAB_MEMO", "nature_detectee": "memo",
            "resume": "Résumé du modèle", "privileges": ["SECRET_PROFESSIONNEL"],
            "niveau_protection": 3}


def _analysed(db, dossier_id):
    return _seed_document(db, dossier_id, analyse=dict(_ANALYSE),
                          category="autre", category_source="analyse")


def _analysis_journal(db) -> dict:
    return db.peek_collection("documents/doc1/analyses")


def _analyse_form(**over):
    form = {"display_name": "Lettre", "category": "autre", "tags": "",
            "notes_internes": "SOUMIS-7Q4", "document_date": "",
            "analyse_presente": "1", "analyse_sous_nature": "CAB_MEMO",
            "analyse_privileges": "SECRET_PROFESSIONNEL", "analyse_niveau_protection": "3",
            "analyse_resume": "Résumé du juriste",
            "analyse_parties_mentionnees": "Alpha, Beta"}
    form.update(over)
    return form


def test_a_stale_document_tab_cannot_undo_a_later_analysis(
    client, db, dossier_id
):
    """The review's worst case: the connector recorded an analysis while
    the edit tab was open. Before, saving the tab reverted the derived
    category, flipped it back to « juriste » and — through the ONE path
    that can lower it — rewrote the protection level under the lawyer's
    name, counted as his confirmation."""
    record_id = _seed_document(db, dossier_id)
    shown = _etags(client.get(f"/documents/{record_id}/edit")
                   .get_data(as_text=True))[0]
    # The connector analyses the document meanwhile.
    doc = db.peek("documents/doc1")
    doc.update(analyse=dict(_ANALYSE), category="autre",
               category_source="analyse", etag=RIVAL_ETAG)
    db.external_write("documents/doc1", doc)
    before = db.peek("documents/doc1")

    resp = client.post(f"/documents/{record_id}/edit", data=_analyse_form(
        category="correspondance", analyse_niveau_protection="1",
        expected_etag=shown))

    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and BANNER in html
    assert db.peek("documents/doc1") == before
    assert _analysis_journal(db) == {}
    assert _etags(html) == [RIVAL_ETAG]
    # The submitted ANALYSIS values are kept too, in the shape the form
    # renders from (a list joined back, the level still selected).
    assert "Résumé du juriste" in html
    assert 'value="Alpha, Beta"' in html
    assert re.search(r'<option value="1"\s+selected', html)


def test_a_fresh_document_save_writes_both_halves_on_one_etag(
    client, db, dossier_id
):
    record_id = _analysed(db, dossier_id)
    shown = _etags(client.get(f"/documents/{record_id}/edit")
                   .get_data(as_text=True))[0]
    resp = client.post(f"/documents/{record_id}/edit",
                       data=_analyse_form(expected_etag=shown))
    assert resp.status_code == 302
    stored = db.peek("documents/doc1")
    assert stored["notes_internes"] == "SOUMIS-7Q4"
    assert stored["analyse"]["resume"] == "Résumé du juriste"
    assert len(_analysis_journal(db)) == 1


def test_a_write_between_the_two_halves_is_refused_and_said(
    client, db, dossier_id, monkeypatch
):
    """The second write commits against the etag the FIRST produced. A
    write landing between them (the connector's record_document_analysis)
    is refused, and the banner says the metadata WAS saved — never « rien
    n'a été enregistré » over a write that committed."""
    record_id = _analysed(db, dossier_id)
    shown = _etags(client.get(f"/documents/{record_id}/edit")
                   .get_data(as_text=True))[0]
    real = documents_routes.update_analyse

    def _racing(document_id, champs, **kw):
        doc = db.peek("documents/doc1")
        doc["analyse"] = {**doc["analyse"], "resume": "Écrit par Claude"}
        doc["etag"] = RIVAL_ETAG
        db.external_write("documents/doc1", doc)
        return real(document_id, champs, **kw)

    monkeypatch.setattr(documents_routes, "update_analyse", _racing)
    resp = client.post(f"/documents/{record_id}/edit",
                       data=_analyse_form(expected_etag=shown))

    html = resp.get_data(as_text=True)
    stored = db.peek("documents/doc1")
    assert resp.status_code == 200 and BANNER in html
    assert "Les renseignements de base ont été enregistrés" in html
    assert "n'ont pas été enregistrés" not in html
    assert stored["notes_internes"] == "SOUMIS-7Q4"       # first half: saved
    assert stored["analyse"]["resume"] == "Écrit par Claude"  # second: refused
    assert _analysis_journal(db) == {}
    assert _etags(html) == [RIVAL_ETAG]


def test_an_analysis_refusal_after_saved_metadata_carries_the_new_etag(
    client, db, dossier_id
):
    """Metadata committed, the analysis refused for a field: the page must
    stand for the version the metadata write produced — the form's own
    etag would be refused as stale on the very next save."""
    record_id = _analysed(db, dossier_id)
    shown = _etags(client.get(f"/documents/{record_id}/edit")
                   .get_data(as_text=True))[0]
    resp = client.post(f"/documents/{record_id}/edit", data=_analyse_form(
        expected_etag=shown, analyse_sous_nature="INVENTE"))
    html = resp.get_data(as_text=True)
    stored = db.peek("documents/doc1")
    assert resp.status_code == 200 and BANNER not in html
    assert "Sous-nature inconnue" in html
    assert stored["notes_internes"] == "SOUMIS-7Q4"
    assert _etags(html) == [stored["etag"]] and stored["etag"] != shown


def test_an_unanalysed_document_saves_without_inventing_an_analysis(
    client, db, dossier_id
):
    """The form always posts the analysis section; on a document never
    analysed and left empty, the save used to fail AFTER the metadata was
    written (« Sous-nature inconnue : . »)."""
    record_id = _seed_document(db, dossier_id)
    shown = _etags(client.get(f"/documents/{record_id}/edit")
                   .get_data(as_text=True))[0]
    resp = client.post(f"/documents/{record_id}/edit", data={
        **_document_form(dossier_id, "SOUMIS-7Q4"), "expected_etag": shown})
    assert resp.status_code == 302
    stored = db.peek("documents/doc1")
    assert stored["notes_internes"] == "SOUMIS-7Q4"
    assert not (stored.get("analyse") or {}).get("sous_nature")
    assert _analysis_journal(db) == {}


def test_a_refused_document_save_with_a_date_re_renders(
    client, db, dossier_id
):
    """The re-render calls strftime on the date: fed the submitted STRING
    it raised, and every refused save with a date was a 500."""
    record_id = _seed_document(db, dossier_id)
    resp = client.post(f"/documents/{record_id}/edit", data={
        **_document_form(dossier_id, "x", category="inventée"),
        "document_date": "2026-02-03"})
    assert resp.status_code == 200
    assert 'value="2026-02-03"' in resp.get_data(as_text=True)


# ══════════════════════════════════════════════════════════════════════
# 4. Les bascules deviennent des consignes
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def bumps(monkeypatch):
    seen: list[str] = []
    for module in (tasks_routes, notes_routes):
        monkeypatch.setattr(module, "bump_ctag",
                            lambda name, _s=seen: _s.append(name) or "c")
    return seen


def test_the_task_checkboxes_post_their_target(client, db, dossier_id):
    task_id = _seed_task(db, dossier_id)
    html = client.get("/taches/").get_data(as_text=True)
    assert f'hx-post="/taches/{task_id}/toggle"' in html
    assert """hx-vals='{"target": "terminée"}'""" in html
    detail = client.get(f"/taches/{task_id}").get_data(as_text=True)
    assert 'name="target" value="terminée"' in detail


def test_a_stale_checkbox_asking_for_done_on_a_done_task_writes_nothing(
    client, db, dossier_id, bumps
):
    task_id = _seed_task(db, dossier_id)
    ok = client.post(f"/taches/{task_id}/toggle", data={"target": "terminée"})
    assert ok.status_code == 302
    after_first = db.peek(f"tasks/{task_id}")
    assert after_first["status"] == "terminée"
    assert bumps == [f"dossier:{dossier_id}"]

    # The same list, still showing the box unticked, is clicked again. A
    # toggle would have REOPENED the task.
    again = client.post(f"/taches/{task_id}/toggle", data={"target": "terminée"})
    assert again.status_code == 302
    assert db.peek(f"tasks/{task_id}") == after_first
    assert bumps == [f"dossier:{dossier_id}"]   # no second bump


def test_the_reopen_controls_post_a_faire_and_reopen(
    client, db, dossier_id, bumps
):
    """The completed list's ticked box and the detail page's « Rouvrir »
    post `à_faire`. Swapped for `terminée`, they would be silent no-ops on
    a done task — nothing written, nothing said — so they are pinned."""
    task_id = _seed_task(db, dossier_id)
    task_model.update_task(task_id, {"status": "terminée"})
    listing = client.get("/taches/?status=terminée").get_data(as_text=True)
    row = listing[listing.index(f'hx-post="/taches/{task_id}/toggle"'):]
    assert row[:row.index(">")].count("""hx-vals='{"target": "à_faire"}'""") == 1
    detail = client.get(f"/taches/{task_id}").get_data(as_text=True)
    assert 'name="target" value="à_faire"' in detail

    client.post(f"/taches/{task_id}/toggle", data={"target": "à_faire"})
    stored = db.peek(f"tasks/{task_id}")
    assert stored["status"] == "à_faire" and stored["completed_date"] is None
    assert bumps == [f"dossier:{dossier_id}"]


def test_reopening_an_open_task_keeps_en_cours(client, db, dossier_id, bumps):
    task_id = _seed_task(db, dossier_id)
    task_model.update_task(task_id, {"status": "en_cours"})
    before = db.peek(f"tasks/{task_id}")
    client.post(f"/taches/{task_id}/toggle", data={"target": "à_faire"})
    assert db.peek(f"tasks/{task_id}") == before
    assert bumps == []


def test_a_cancelled_task_is_never_flipped_by_a_checkbox(
    client, db, dossier_id, bumps
):
    task_id = _seed_task(db, dossier_id)
    task_model.update_task(task_id, {"status": "annulée"})
    before = db.peek(f"tasks/{task_id}")
    for data in ({"target": "terminée"}, {"target": "à_faire"}, {}):
        resp = client.post(f"/taches/{task_id}/toggle", data=data)
        assert resp.status_code == 302
        assert "annul" in resp.headers["Location"]  # the ?erreur= banner
    assert db.peek(f"tasks/{task_id}") == before
    assert bumps == []


def test_a_page_without_target_keeps_the_old_toggle(
    client, db, dossier_id, bumps
):
    task_id = _seed_task(db, dossier_id)
    client.post(f"/taches/{task_id}/toggle", data={})
    assert db.peek(f"tasks/{task_id}")["status"] == "terminée"
    client.post(f"/taches/{task_id}/toggle", data={})
    assert db.peek(f"tasks/{task_id}")["status"] == "à_faire"


def test_the_pin_button_posts_its_target(client, db, dossier_id):
    note_id = _seed_note(db, dossier_id)
    html = client.get(f"/notes/{note_id}").get_data(as_text=True)
    assert 'name="pinned" value="1"' in html


def test_a_stale_pin_click_writes_nothing_and_never_unpins(
    client, db, dossier_id, bumps
):
    note_id = _seed_note(db, dossier_id)
    client.post(f"/notes/{note_id}/pin", data={"pinned": "1"})
    pinned = db.peek(f"notes/{note_id}")
    assert pinned["pinned"] is True and pinned["updated_via"] == "web"
    assert bumps == [f"dossier:{dossier_id}"]

    # A second page, rendered before the first click, still says « Épingler ».
    client.post(f"/notes/{note_id}/pin", data={"pinned": "1"})
    assert db.peek(f"notes/{note_id}") == pinned
    assert bumps == [f"dossier:{dossier_id}"]

    client.post(f"/notes/{note_id}/pin", data={"pinned": "0"})
    assert db.peek(f"notes/{note_id}")["pinned"] is False
    assert len(bumps) == 2


def test_pinning_never_rewrites_the_content_the_page_read(
    client, db, dossier_id, bumps
):
    """The pin is a partial update of the flag: a block appended since the
    page opened survives it (update_note's full set() would have written
    the content back)."""
    note_id = _seed_note(db, dossier_id)
    appended = db.peek(f"notes/{note_id}")
    appended["content"] = "Premier jet.\n\nAjouté par Claude."
    appended["etag"] = RIVAL_ETAG
    db.external_write(f"notes/{note_id}", appended)
    client.post(f"/notes/{note_id}/pin", data={"pinned": "1"})
    stored = db.peek(f"notes/{note_id}")
    assert stored["content"] == "Premier jet.\n\nAjouté par Claude."
    assert stored["pinned"] is True and stored["etag"] != RIVAL_ETAG


def test_a_malformed_pin_target_is_refused(client, db, dossier_id, bumps):
    note_id = _seed_note(db, dossier_id)
    before = db.peek(f"notes/{note_id}")
    client.post(f"/notes/{note_id}/pin", data={"pinned": "oui"})
    assert db.peek(f"notes/{note_id}") == before and bumps == []


# ── « Confirmer » une analyse : le troisième contrôle en un clic ────────
#
# Confirmer, c'est dire « j'ai vu ». Le bouton portait seulement le jeton
# CSRF : un onglet ouvert AVANT que le connecteur ne réanalyse le document
# confirmait, sous le nom du juriste, une qualification qu'il n'avait
# jamais lue — niveau de protection compris — et faisait tomber la mention
# « présumée » (category_source → « juriste »). Il porte désormais l'etag
# de la version affichée (constat de la revue de l'étape 6, corrigé par la
# critique du lot 0a).


def _confirmable(db, dossier_id):
    # Not previewable: the detail page never reaches Storage for a URL.
    return _seed_document(db, dossier_id, analyse=dict(_ANALYSE),
                          category="autre", category_source="analyse",
                          file_type="application/zip", filename="lot.zip")


def test_the_confirm_button_carries_the_displayed_etag(client, db, dossier_id):
    doc_id = _confirmable(db, dossier_id)
    html = client.get(f"/documents/{doc_id}").get_data(as_text=True)
    form = html[html.index(f'action="/documents/{doc_id}/analyse/confirmer"'):]
    form = form[:form.index("</form>")]
    assert _etags(form) == ["e0"]


def test_a_stale_confirm_confirms_nothing_and_says_so(client, db, dossier_id):
    doc_id = _confirmable(db, dossier_id)
    shown = _etags(client.get(f"/documents/{doc_id}").get_data(as_text=True))[0]
    # The connector re-analyses the document meanwhile — a higher level.
    reanalysed = db.peek("documents/doc1")
    reanalysed["analyse"] = {**_ANALYSE, "niveau_protection": 3,
                             "resume": "Nouvelle analyse"}
    reanalysed["etag"] = RIVAL_ETAG
    db.external_write("documents/doc1", reanalysed)
    before = db.peek("documents/doc1")

    resp = client.post(f"/documents/{doc_id}/analyse/confirmer",
                       data={"expected_etag": shown})

    assert resp.status_code == 302            # a 2xx-bound bounce (htmx rule)
    assert db.peek("documents/doc1") == before
    assert not before["analyse"].get("confirme")
    assert before["category_source"] == "analyse"   # still « présumée »
    shown_after = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Rien n'a été confirmé" in html_module.unescape(shown_after)
    assert _etags(shown_after) == [RIVAL_ETAG]      # the new version, to read


def test_a_fresh_confirm_confirms_under_the_lawyers_name(client, db, dossier_id):
    doc_id = _confirmable(db, dossier_id)
    shown = _etags(client.get(f"/documents/{doc_id}").get_data(as_text=True))[0]
    resp = client.post(f"/documents/{doc_id}/analyse/confirmer",
                       data={"expected_etag": shown})
    assert resp.status_code == 302 and "erreur" not in resp.headers["Location"]
    stored = db.peek("documents/doc1")
    assert stored["analyse"]["confirme"] is True
    assert stored["analyse"]["confirme_par"] == "test@example.com"
    assert stored["category_source"] == "juriste"
    assert stored["etag"] != shown


def test_a_confirm_from_a_page_without_the_field_keeps_the_old_behaviour(
    client, db, dossier_id
):
    doc_id = _confirmable(db, dossier_id)
    client.post(f"/documents/{doc_id}/analyse/confirmer", data={})
    assert db.peek("documents/doc1")["analyse"]["confirme"] is True


def test_the_detail_page_shows_a_confirm_refusal(client, db, dossier_id):
    """The route always bounced its refusals on ?erreur=, and the page never
    read it: « Aucune analyse à confirmer. » vanished too."""
    doc_id = _seed_document(db, dossier_id, file_type="application/zip")
    resp = client.post(f"/documents/{doc_id}/analyse/confirmer", data={})
    html = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Aucune analyse à confirmer." in html


# ══════════════════════════════════════════════════════════════════════
# 5. Le bandeau lui-même
# ══════════════════════════════════════════════════════════════════════


def _banner(conflict) -> str:
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    with app.app_context():
        return render_template_string(
            '{% from "components/_edit_conflict.html" import conflict_banner %}'
            "{{ conflict_banner(conflict) }}", conflict=conflict)


def test_the_banner_names_the_time_in_montreal_never_the_writer():
    current = {"etag": "e9", "updated_via": "mcp",
               "updated_at": datetime(2026, 1, 15, 19, 5, tzinfo=UTC)}
    ctx = edit_conflict.conflict_context(current, compare_url="/x")
    assert ctx["etag"] == "e9"
    assert ctx["updated_at_display"] == "15 janvier 2026 à 14 h 05"
    html = _banner(ctx)
    assert "15 janvier 2026 à 14 h 05" in html
    assert 'href="/x"' in html and 'target="_blank"' in html
    # WHEN, never WHO: a script regenerates an etag without stamping.
    assert "Claude" not in html and "connecteur" not in html


def test_an_unreadable_current_version_still_refuses_the_next_save():
    ctx = edit_conflict.conflict_context(None)
    assert ctx["etag"] == "" and ctx["updated_at_display"] == ""
    assert BANNER in _banner(ctx)


def test_no_conflict_renders_nothing():
    assert _banner(None).strip() == ""


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_banner_uses_only_compiled_classes():
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    html = _banner(edit_conflict.conflict_context(
        {"etag": "e", "updated_at": DT}, compare_url="/x", note="n"))
    classes = {c for block in re.findall(r'class="([^"]+)"', html)
               for c in block.split()}
    assert classes, html
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent


def test_the_component_uses_no_filter_and_no_global():
    """It must compile in the bare Jinja environments some tests render a
    form in (test_phase_reclassement_web): no `|filter`, no call to a
    context global, nothing but the values the route precomputed."""
    src = (_ATHENA / "templates/components/_edit_conflict.html").read_text(
        encoding="utf-8")
    body = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    assert "|" not in body
    assert "to_mtl" not in body and "url_for" not in body


# ══════════════════════════════════════════════════════════════════════
# 6. Le câblage, vu du source
# ══════════════════════════════════════════════════════════════════════

_ROUTE_FILES = ("parties.py", "dossiers.py", "time_expenses.py",
                "documents.py", "notes.py", "tasks.py", "hearings.py",
                "reception.py")


def test_expected_etag_never_enters_a_model_payload():
    """The form's version is a GUARD handed to the model as a keyword,
    never a field of the data dict — a stored `expected_etag` key would
    be written into the document by the full-document set()."""
    for name in _ROUTE_FILES:
        src = (_ATHENA / "routes" / name).read_text(encoding="utf-8")
        assert '"expected_etag"' not in src and "'expected_etag'" not in src, name


def test_no_edit_route_calls_a_toggle_helper():
    for name in ("tasks.py", "notes.py"):
        src = (_ATHENA / "routes" / name).read_text(encoding="utf-8")
        assert "toggle_task_complete" not in src, name
    notes_src = (_ATHENA / "routes" / "notes.py").read_text(encoding="utf-8")
    # toggle_pin survives ONLY for a page that posts no target.
    assert notes_src.count("toggle_pin(") == 1


def test_the_model_refusal_text_is_what_split_stale_recognises():
    stale, rest = edit_conflict.split_stale(
        [concurrency.STALE_ETAG_ERROR, "autre"])
    assert stale and rest == ["autre"]
    assert edit_conflict.split_stale(["autre"]) == (False, ["autre"])
