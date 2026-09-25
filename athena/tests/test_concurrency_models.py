"""La concurrence optimiste dans les MODÈLES (plan lot 0a, règle 3).

Les dix mutateurs qui gagnent ``expected_etag`` le 2026-09-25 :
``partie.update_partie``, ``dossier.update_dossier``,
``time_entry.update_time_entry`` / ``set_time_entry_phase``,
``expense.update_expense`` / ``set_expense_phase`` — ceux que le connecteur
modifie — et ``note.update_note``, ``task.update_task``,
``document.update_metadata`` / ``update_analyse`` — ceux des enregistrements
que le connecteur écrit déjà et dont les formulaires web recevront l'etag.

Chacun tourne ici au-dessus du faux Firestore partagé (le client est le
vrai), et l'on relit ce qui est STOCKÉ. Pour chacun :

* etag à jour → UNE transaction, lecture comprise, et le document renvoyé
  porte le NOUVEL etag (celui qui est stocké) ;
* etag périmé → ``[STALE_ETAG_ERROR]``, et le magasin n'a pas bougé ;
* une écriture concurrente ENTRE la lecture du modèle et sa validation →
  refus, et l'écriture concurrente survit ;
* un document supprimé entre-temps n'est jamais ressuscité ;
* ``None`` → l'unique ``set()`` d'avant, hors transaction : DAV et les
  formulaires sans etag ne voient aucune différence.

La section 5 fait la même preuve à travers les VRAIS gestionnaires du
connecteur (``run_write`` compris), sur les six outils qui acceptent
``expected_etag``.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import concurrency
    from models import document as document_model
    from models import dossier as dossier_model
    from models import expense as expense_model
    from models import note as note_model
    from models import partie as partie_model
    from models import task as task_model
    from models import time_entry as time_entry_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
STALE = [concurrency.STALE_ETAG_ERROR]
_MODULES = (partie_model, dossier_model, time_entry_model, expense_model,
            note_model, task_model, document_model)


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, *_MODULES)


# ── Fabriques : chaque enregistrement naît par le VRAI créateur ──────────


def _partie(db):
    doc, errors = partie_model.create_partie(
        {"type": "individual", "contact_role": "client",
         "first_name": "Jean", "last_name": "Tremblay"})
    assert errors == [], errors
    return doc["id"]


def _dossier(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
    })
    assert errors == [], errors
    return doc["id"]


def _time_entry(db):
    doc, errors = time_entry_model.create_time_entry({
        "dossier_id": "d1", "date": DT, "description": "Rédaction",
        "hours": 1.5, "rate": 30000, "billable": True})
    assert errors == [], errors
    return doc["id"]


def _expense(db):
    doc, errors = expense_model.create_expense({
        "dossier_id": "d1", "date": DT, "description": "Timbre",
        "amount": 5000, "category": "timbre_judiciaire", "taxable": False})
    assert errors == [], errors
    return doc["id"]


def _note(db):
    doc, errors = note_model.create_note(
        {"title": "Recherche", "content": "Premier jet.",
         "category": "recherche"})
    assert errors == [], errors
    return doc["id"]


def _task(db):
    doc, errors = task_model.create_task({"title": "Préparer la requête"})
    assert errors == [], errors
    return doc["id"]


def _document(db, **over):
    doc = {**document_model._default_doc(), "id": "doc1",
           "dossier_id": "d1", "display_name": "Lettre", "category": "autre",
           "created_at": DT, "updated_at": DT, "etag": "e0"}
    doc.update(over)
    db.seed("documents/doc1", doc)
    return "doc1"


def _analysed_document(db):
    return _document(db, analyse={"sous_nature": "CAB_MEMO", "resume": "",
                                  "privileges": [], "niveau_protection": 1},
                     category="autre", category_source="analyse")


# (collection, factory, edit(id, **kw) -> (doc, errors), field, value)
_CASES = {
    "update_partie": ("parties", _partie,
                      lambda i, **kw: partie_model.update_partie(
                          i, {"notes": "corrigé"}, **kw),
                      "notes", "corrigé"),
    "update_dossier": ("dossiers", _dossier,
                       lambda i, **kw: dossier_model.update_dossier(
                           i, {"sommaire": "Résumé."}, **kw),
                       "sommaire", "Résumé."),
    "update_time_entry": ("timeentries", _time_entry,
                          lambda i, **kw: time_entry_model.update_time_entry(
                              i, {"description": "Révision"}, **kw),
                          "description", "Révision"),
    "update_expense": ("expenses", _expense,
                       lambda i, **kw: expense_model.update_expense(
                           i, {"description": "Timbre (corrigé)"}, **kw),
                       "description", "Timbre (corrigé)"),
    "set_time_entry_phase": ("timeentries", _time_entry,
                             lambda i, **kw: time_entry_model.set_time_entry_phase(
                                 i, "", "INT-01", **kw)[:2],
                             "sous_phase", "INT-01"),
    "set_expense_phase": ("expenses", _expense,
                          lambda i, **kw: expense_model.set_expense_phase(
                              i, "", "INT-01", **kw)[:2],
                          "sous_phase", "INT-01"),
    "update_note": ("notes", _note,
                    lambda i, **kw: note_model.update_note(
                        i, {"content": "Second jet."}, **kw),
                    "content", "Second jet."),
    "update_task": ("tasks", _task,
                    lambda i, **kw: task_model.update_task(
                        i, {"priority": "haute"}, **kw),
                    "priority", "haute"),
    "update_metadata": ("documents", _document,
                        lambda i, **kw: document_model.update_metadata(
                            i, {"display_name": "Mise en demeure"}, **kw),
                        "display_name", "Mise en demeure"),
    "update_analyse": ("documents", _analysed_document,
                       lambda i, **kw: document_model.update_analyse(
                           i, {"resume": "Lettre au confrère."},
                           par="juriste", **kw),
                       None, None),
}
_ALL = pytest.mark.parametrize("case", sorted(_CASES), ids=sorted(_CASES))

# The getter each case's model reads its `existing` through — the seam a
# concurrent write is slipped into, between the model's read and its commit.
_GETTERS = {
    "update_partie": (partie_model, "get_partie"),
    "update_dossier": (dossier_model, "get_dossier"),
    "update_time_entry": (time_entry_model, "get_time_entry"),
    "update_expense": (expense_model, "get_expense"),
    "set_time_entry_phase": (time_entry_model, "get_time_entry"),
    "set_expense_phase": (expense_model, "get_expense"),
    "update_note": (note_model, "get_note"),
    "update_task": (task_model, "get_task"),
    "update_metadata": (document_model, "get_document"),
    "update_analyse": (document_model, "get_document"),
}


def _stored_field(db, case, path):
    field = _CASES[case][3]
    stored = db.peek(path)
    if field is None:  # update_analyse: the value lives in the cache
        return stored["analyse"].get("resume")
    return stored.get(field)


def _expected_value(case):
    return _CASES[case][4] if _CASES[case][3] else "Lettre au confrère."


def _setup(db, case):
    coll, factory, edit, _f, _v = _CASES[case]
    row_id = factory(db)
    path = f"{coll}/{row_id}"
    db.reset_logs()
    return row_id, path, edit


def test_the_cases_cover_every_function_that_gained_the_keyword():
    """Derived from the models' own signatures: a mutator that grows the
    keyword without a case here fails, rather than shipping unproven."""
    import inspect

    grown = set()
    for module in _MODULES:
        for name, fn in vars(module).items():
            if (inspect.isfunction(fn) and fn.__module__ == module.__name__
                    and "expected_etag" in inspect.signature(fn).parameters):
                grown.add(name)
    assert grown == set(_CASES)
    for name in grown:
        module, _getter = _GETTERS[name]
        param = inspect.signature(getattr(module, name)).parameters["expected_etag"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert param.default is None, name


# ══════════════════════════════════════════════════════════════════════
# 1. À jour : une transaction, et le nouvel etag renvoyé
# ══════════════════════════════════════════════════════════════════════


@_ALL
def test_a_current_etag_commits_in_one_transaction(db, case):
    row_id, path, edit = _setup(db, case)
    etag0 = db.peek(path)["etag"]

    doc, errors = edit(row_id, expected_etag=etag0)

    assert errors == [], errors
    stored = db.peek(path)
    assert _stored_field(db, case, path) == _expected_value(case)
    assert stored["etag"] != etag0
    # The document handed back carries the etag that is STORED — the one
    # the next guarded edit must present, never the one this edit read.
    assert doc["etag"] == stored["etag"]
    guarded = [c for c in db.commits if c.transaction is not None]
    assert len(guarded) == 1
    kind = "update" if case.startswith("set_") else "set"
    assert (kind, path) in guarded[0].ops
    assert any(r.transactional and path in r.paths for r in db.reads)


# ══════════════════════════════════════════════════════════════════════
# 2. Périmé : rien n'est écrit
# ══════════════════════════════════════════════════════════════════════


@_ALL
def test_a_stale_etag_is_refused_and_nothing_is_written(db, case):
    row_id, path, edit = _setup(db, case)
    before = db.peek(path)

    doc, errors = edit(row_id, expected_etag="une-version-perimee")

    assert doc is None and errors == STALE
    assert db.peek(path) == before
    assert db.commits == []


@_ALL
def test_a_write_between_the_models_read_and_its_commit_is_refused(
    db, monkeypatch, case,
):
    """The model's own pre-check passes (it read the version the caller
    named); a rival write lands right after that read. The transactional
    re-read catches it, and the rival's write survives intact."""
    row_id, path, edit = _setup(db, case)
    etag0 = db.peek(path)["etag"]
    module, getter_name = _GETTERS[case]
    real_getter = getattr(module, getter_name)

    def racing_getter(doc_id):
        doc = real_getter(doc_id)
        db.external_write(path, {**db.peek(path), "etag": "e-rival",
                                 "rival": True})
        return doc

    monkeypatch.setattr(module, getter_name, racing_getter)
    doc, errors = edit(row_id, expected_etag=etag0)

    assert doc is None and errors == STALE
    stored = db.peek(path)
    assert stored["etag"] == "e-rival" and stored["rival"] is True
    assert db.commits == []


@_ALL
def test_a_document_deleted_since_the_read_is_never_resurrected(
    db, monkeypatch, case,
):
    row_id, path, edit = _setup(db, case)
    etag0 = db.peek(path)["etag"]
    module, getter_name = _GETTERS[case]
    real_getter = getattr(module, getter_name)

    def vanishing_getter(doc_id):
        doc = real_getter(doc_id)
        db.external_delete(path)
        return doc

    monkeypatch.setattr(module, getter_name, vanishing_getter)
    doc, errors = edit(row_id, expected_etag=etag0)

    assert doc is None and len(errors) == 1 and "introuvable" in errors[0]
    assert db.peek(path) is None
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. None : le chemin d'avant, octet pour octet
# ══════════════════════════════════════════════════════════════════════


@_ALL
def test_without_an_etag_the_write_is_the_plain_legacy_one(db, case):
    row_id, path, edit = _setup(db, case)
    db.external_write(path, {**db.peek(path), "etag": "e-rival"})
    db.reset_logs()

    doc, errors = edit(row_id)

    # Last-write-wins, exactly as before: the rival etag is not consulted.
    assert errors == [], errors
    assert _stored_field(db, case, path) == _expected_value(case)
    assert all(c.transaction is None for c in db.commits)
    assert db.commits[-1].ops == ((
        "update" if case.startswith("set_") else "set", path),)
    # No read through a transaction, and no read beyond the model's own.
    assert not any(r.transactional for r in db.reads)


# ══════════════════════════════════════════════════════════════════════
# 4. Particularités
# ══════════════════════════════════════════════════════════════════════


_PHASE_WRITE_KEYS = {"phase", "sous_phase", "updated_at", "etag", "updated_via"}


@pytest.mark.parametrize("case", ["set_time_entry_phase", "set_expense_phase"])
def test_the_guarded_phase_write_keeps_its_shape(db, case):
    """The reclassifiers' guarantee is the SHAPE of their write — a partial
    update of the pair and its stamp. The guard must not turn it into a
    full-document set() that could move a money figure."""
    row_id, path, edit = _setup(db, case)
    before = db.peek(path)

    _doc, errors = edit(row_id, expected_etag=before["etag"])

    assert errors == []
    after = db.peek(path)
    assert db.commits[0].ops == (("update", path),)
    moved = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert moved <= _PHASE_WRITE_KEYS


@pytest.mark.parametrize("model, setter", [
    (time_entry_model, "set_time_entry_phase"),
    (expense_model, "set_expense_phase"),
])
def test_an_unchanged_phase_pair_is_answered_whatever_the_etag(
    db, model, setter,
):
    """No update can be lost when the row already carries the requested
    code — refusing that no-op would break the replayability the phase
    tools promise (« re-sending the code writes nothing at all »)."""
    row_id = (_time_entry if model is time_entry_model else _expense)(db)
    fn = getattr(model, setter)
    doc, errors, changed = fn(row_id, "", "INT-01")
    assert errors == [] and changed is True
    db.reset_logs()

    doc, errors, changed = fn(row_id, "", "INT-01", expected_etag="perimee")
    assert errors == [] and changed is False
    assert doc["sous_phase"] == "INT-01"
    assert db.commits == []

    # A CHANGED pair on the same stale etag is refused.
    doc, errors, changed = fn(row_id, "", "AUD-01", expected_etag="perimee")
    assert doc is None and errors == STALE and changed is False
    assert db.commits == []


def test_a_trust_write_racing_a_guarded_dossier_save_is_never_clobbered(
    db, monkeypatch,
):
    """The trust fields are owned by models/trust.py. On the guarded path
    the etag comparison replaces the last-moment re-read: a trust write
    since the read regenerates the dossier etag, and the save is refused."""
    row_id, path, edit = _setup(db, "update_dossier")
    etag0 = db.peek(path)["etag"]
    real = dossier_model.get_dossier

    def racing(doc_id):
        doc = real(doc_id)
        db.external_write(path, {**db.peek(path), "trust_balance": 90000,
                                 "trust_cleared_by_client": {"p1": 90000},
                                 "etag": "e-trust"})
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier", racing)
    doc, errors = edit(row_id, expected_etag=etag0)

    assert errors == STALE
    stored = db.peek(path)
    assert stored["trust_balance"] == 90000
    assert stored["trust_cleared_by_client"] == {"p1": 90000}


def test_the_legacy_dossier_save_still_refreshes_the_trust_fields(
    db, monkeypatch,
):
    """Kept on purpose: the path without an etag (every web save today)
    still re-reads the three trust fields at the last moment, so a trust
    write that committed since its read is not clobbered either."""
    row_id, path, edit = _setup(db, "update_dossier")
    real = dossier_model.get_dossier

    def racing(doc_id):
        doc = real(doc_id)
        db.external_write(path, {**db.peek(path), "trust_balance": 90000,
                                 "trust_balance_by_client": {"p1": 90000},
                                 "trust_cleared_by_client": {"p1": 90000},
                                 "etag": "e-trust"})
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier", racing)
    doc, errors = edit(row_id)

    assert errors == []
    stored = db.peek(path)
    assert stored["sommaire"] == "Résumé."
    assert stored["trust_balance"] == 90000
    assert stored["trust_cleared_by_client"] == {"p1": 90000}


def test_a_stale_task_edit_fires_no_protocol_sync(db, monkeypatch):
    """Nothing written, so nothing to cascade: the step must not be
    completed on the strength of a status change that never landed."""
    synced = []
    monkeypatch.setattr(task_model, "_sync_protocol_step",
                        lambda tid, status: synced.append((tid, status)))
    row_id = _task(db)

    _doc, errors = task_model.update_task(
        row_id, {"status": "terminée"}, expected_etag="perimee")
    assert errors == STALE and synced == []

    etag = db.peek(f"tasks/{row_id}")["etag"]
    _doc, errors = task_model.update_task(
        row_id, {"status": "terminée"}, expected_etag=etag)
    assert errors == [] and synced == [(row_id, "terminée")]


def test_a_guarded_analyse_edit_writes_journal_and_cache_together(db):
    row_id, path, edit = _setup(db, "update_analyse")
    etag0 = db.peek(path)["etag"]

    doc, errors = edit(row_id, expected_etag=etag0)

    assert errors == []
    assert len(db.commits) == 1
    ops = db.commits[0].ops
    journal = [p for k, p in ops if p.startswith(f"{path}/analyses/")]
    assert len(journal) == 1 and ("set", path) in ops
    assert db.peek(journal[0])["declenche_par"] == "juriste"


def test_a_stale_analyse_edit_leaves_no_journal_entry(db):
    row_id, path, edit = _setup(db, "update_analyse")

    _doc, errors = edit(row_id, expected_etag="perimee")

    assert errors == STALE
    assert db.peek_collection(f"{path}/analyses") == {}


def test_a_document_save_chains_its_second_write_on_the_new_etag(db):
    """The document edit form saves metadata THEN the analysis. The second
    write must present the etag the first one produced: the form's own
    etag is stale by then, by construction."""
    row_id = _analysed_document(db)
    form_etag = db.peek("documents/doc1")["etag"]

    doc, errors = document_model.update_metadata(
        row_id, {"display_name": "Mise en demeure"}, expected_etag=form_etag)
    assert errors == []

    _stale, errors = document_model.update_analyse(
        row_id, {"resume": "x"}, par="juriste", expected_etag=form_etag)
    assert errors == STALE

    _doc, errors = document_model.update_analyse(
        row_id, {"resume": "x"}, par="juriste", expected_etag=doc["etag"])
    assert errors == []
    assert db.peek("documents/doc1")["analyse"]["resume"] == "x"


# ══════════════════════════════════════════════════════════════════════
# 5. Le connecteur, bout à bout — vrais gestionnaires, vrais modèles
# ══════════════════════════════════════════════════════════════════════
#
# Plan, règle 3, telle que l'appelant la vit : `expected_etag` périmé →
# refus `stale_etag`, rien d'écrit ; à jour → écrit, sous le connecteur, et
# le résultat rend l'etag STOCKÉ ; omis → le gestionnaire compare-et-écrit
# contre SA propre lecture, si bien qu'une écriture glissée entre cette
# lecture et le commit est refusée au lieu d'être écrasée. La liste des cas
# est DÉRIVÉE des outils qui déclarent une politique de concurrence : un
# nouvel outil qui accepte `expected_etag` échoue ici tant qu'il n'y figure
# pas.


def _e2e_db(monkeypatch):
    return install(monkeypatch, *_MODULES, dav_sync, write_support)


# tool → (collection, factory, id key, arguments, (field, stored value))
_HANDLER_CASES = {
    "update_partie": ("parties", _partie, "partie_id",
                      {"notes": "corrigé"}, ("notes", "corrigé")),
    "update_dossier": ("dossiers", _dossier, "dossier_id",
                       {"sommaire": "Résumé."}, ("sommaire", "Résumé.")),
    "update_time_entry": ("timeentries", _time_entry, "time_entry_id",
                          {"description": "Révision"},
                          ("description", "Révision")),
    "update_expense": ("expenses", _expense, "expense_id",
                       {"description": "Timbre (corrigé)"},
                       ("description", "Timbre (corrigé)")),
    "set_time_entry_phase": ("timeentries", _time_entry, "time_entry_id",
                             {"sous_phase": "INT-01"},
                             ("sous_phase", "INT-01")),
    "set_expense_phase": ("expenses", _expense, "expense_id",
                          {"sous_phase": "INT-01"}, ("sous_phase", "INT-01")),
}
_E2E = pytest.mark.parametrize("tool", sorted(_HANDLER_CASES),
                               ids=sorted(_HANDLER_CASES))

# The getter the HANDLER reads its record through (the bulk reader for the
# reclassifiers) — the seam a rival write is slipped in after.
_HANDLER_GETTERS = {
    "update_partie": (partie_model, "get_partie"),
    "update_dossier": (dossier_model, "get_dossier"),
    "update_time_entry": (time_entry_model, "get_time_entry"),
    "update_expense": (expense_model, "get_expense"),
    "set_time_entry_phase": (time_entry_model, "get_time_entries_bulk"),
    "set_expense_phase": (expense_model, "get_expenses_bulk"),
}


def _e2e_setup(db, tool):
    coll, factory, id_key, extra, _expect = _HANDLER_CASES[tool]
    row_id = factory(db)
    path = f"{coll}/{row_id}"
    db.reset_logs()
    return path, {id_key: row_id, **extra}


def _row_commits(db, path):
    return [c for c in db.commits if any(p == path for _k, p in c.ops)]


def test_the_handler_cases_are_the_tools_that_accept_an_etag():
    accepting = {
        n for n, spec in tools.TOOLS.items()
        if spec.get("concurrency") in (tools.CONCURRENCY_OPTIONAL,
                                       tools.CONCURRENCY_REQUIRED)
    }
    assert accepting == set(_HANDLER_CASES) == set(_HANDLER_GETTERS)


@_E2E
def test_a_stale_expected_etag_is_refused_and_nothing_is_written(
    monkeypatch, tool,
):
    db = _e2e_db(monkeypatch)
    path, args = _e2e_setup(db, tool)
    before = db.peek(path)

    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)({**args, "expected_etag": "une-autre-version"})

    assert excinfo.value.reason == "stale_etag"
    message = str(excinfo.value)
    assert "Rien n'a été écrit" in message
    for reader in tools.TOOLS[tool]["etag_readers"]:
        assert reader in message
    assert before["etag"] not in message  # never an etag to retry blind with
    # The remedy names the record, never a pronoun: the subjects differ in
    # gender (« Cette entrée de temps… Relisez-le » shipped once).
    assert "Relisez l'enregistrement" in message
    assert "Relisez-le" not in message and "Relisez-la" not in message
    assert db.peek(path) == before
    assert _row_commits(db, path) == []


@_E2E
def test_the_current_etag_writes_as_the_connector_and_hands_back_the_new_one(
    monkeypatch, tool,
):
    db = _e2e_db(monkeypatch)
    path, args = _e2e_setup(db, tool)
    etag0 = db.peek(path)["etag"]
    field, value = _HANDLER_CASES[tool][4]

    payload = getattr(handlers, tool)({**args, "expected_etag": etag0})

    stored = db.peek(path)
    assert stored[field] == value
    assert stored["updated_via"] == "mcp"
    assert stored["etag"] != etag0
    assert payload["entity"]["etag"] == stored["etag"]
    (commit,) = _row_commits(db, path)
    assert commit.transaction is not None


@_E2E
def test_without_an_etag_the_handler_guards_its_own_read(monkeypatch, tool):
    """The caller sent none; a rival writes right after the HANDLER's read.
    The handler compared-and-set against that read, so the rival survives
    and the call is refused — never a silent overwrite."""
    db = _e2e_db(monkeypatch)
    path, args = _e2e_setup(db, tool)
    module, name = _HANDLER_GETTERS[tool]
    real = getattr(module, name)
    fired = []

    def racing(*a, **kw):
        result = real(*a, **kw)
        if not fired:
            fired.append(True)
            db.external_write(path, {**db.peek(path), "etag": "e-rival",
                                     "rival": True})
        return result

    monkeypatch.setattr(module, name, racing)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))

    assert fired and excinfo.value.reason == "stale_etag"
    stored = db.peek(path)
    assert stored["etag"] == "e-rival" and stored["rival"] is True
    assert _row_commits(db, path) == []


@_E2E
def test_a_chained_edit_presents_the_etag_the_first_one_returned(
    monkeypatch, tool,
):
    db = _e2e_db(monkeypatch)
    path, args = _e2e_setup(db, tool)
    first = getattr(handlers, tool)(dict(args))
    etag1 = first["entity"]["etag"]
    assert etag1 == db.peek(path)["etag"]

    if tool.startswith("set_"):
        second_args = {**args, "sous_phase": "AUD-01"}
    else:
        field = _HANDLER_CASES[tool][4][0]
        second_args = {**args, field: "Deuxième version"}
    second = getattr(handlers, tool)({**second_args, "expected_etag": etag1})
    assert second["entity"]["etag"] == db.peek(path)["etag"] != etag1
