"""La concurrence optimiste dans les MODÈLES (plan lot 0a, règle 3).

Les dix mutateurs qui gagnent ``expected_etag`` le 2026-09-25 :
``partie.update_partie``, ``dossier.update_dossier``,
``time_entry.update_time_entry`` / ``set_time_entry_phase``,
``expense.update_expense`` / ``set_expense_phase`` — ceux que le connecteur
modifie — et ``note.update_note``, ``task.update_task``,
``document.update_metadata`` / ``update_analyse`` — ceux des enregistrements
que le connecteur écrit déjà et dont les formulaires web recevront l'etag —,
plus ``document.confirmer_analyse`` (le bouton « Confirmer » d'une analyse).
Le lot 2A (étape T1, 2026-09-27) en ajoute deux : ``document.move_document``
et ``document.confirmer_categorie`` (le « Confirmer » d'une catégorie posée
par Claude, D15). Le lot 3a (étape 2, 2026-09-28) en ajoute deux :
``time_entry.move_time_entry`` et ``expense.move_expense`` (le déplacement
d'une ligne non facturée vers un autre dossier), qui ne lisent QUE dans leur
transaction et n'écrivent qu'un ``update()`` partiel.

Chacun tourne ici au-dessus du faux Firestore partagé (le client est le
vrai), et l'on relit ce qui est STOCKÉ. Pour chacun :

* etag à jour → UNE transaction, lecture comprise, et le document renvoyé
  porte le NOUVEL etag (celui qui est stocké) ;
* etag périmé → ``[STALE_ETAG_ERROR]``, et le magasin n'a pas bougé ;
* une écriture concurrente ENTRE la lecture du modèle et sa validation →
  refus, et l'écriture concurrente survit ;
* un document supprimé entre-temps n'est jamais ressuscité ;
* ``None`` → l'unique ``set()`` d'avant, hors transaction : DAV et les
  formulaires sans etag ne voient aucune différence — SAUF pour
  ``update_time_entry`` / ``update_expense``, qui depuis le lot 0b (étape
  B2) lisent et écrivent TOUJOURS dans une transaction : la course contre
  une facture ne dépend pas de la version que l'appelant a lue
  (``tests/test_time_expense_races.py``). Sans etag, ils restent « la
  dernière écriture gagne » sur les champs édités.

La section 5 fait la même preuve à travers les VRAIS gestionnaires du
connecteur (``run_write`` compris), sur les six outils qui acceptent
``expected_etag``.
"""

import contextvars
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
    from models import doc_template as doc_template_model
    from models import document as document_model
    from models import dossier as dossier_model
    from models import expense as expense_model
    from models import folder as folder_model
    from models import hearing as hearing_model
    from models import note as note_model
    from models import partie as partie_model
    from models import protocol as protocol_model
    from models import revision as revision_model
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
    # models.revision too: a note content change stages its revision since
    # D17 (2026-09-27), and the reference must belong to the same store.
    return install(monkeypatch, *_MODULES, revision_model)


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


def _time_entry_to_move(db):
    """A time entry in « d1 », and the dossier « d2 » it moves to."""
    db.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                            "title": "Gagnon c. Roy"})
    return _time_entry(db)


def _expense_to_move(db):
    db.seed("dossiers/d2", {"id": "d2", "file_number": "2026-002",
                            "title": "Gagnon c. Roy"})
    return _expense(db)


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


def _document_and_folder(db):
    """A document at its dossier's root, and a folder of that dossier to
    move it into (folders are a top-level collection keyed by id)."""
    db.seed("folders/f1", {"id": "f1", "dossier_id": "d1", "name": "Pièces",
                           "parent_folder_id": None, "order": 0})
    return _document(db)


def _presumed_document(db):
    """A category Claude posed outside any analysis (D15)."""
    return _document(db, category="correspondance", category_source="mcp")


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
    # Lot 3a (step 2): the triple return — « moved » is not this
    # harness's concern either.
    "move_time_entry": ("timeentries", _time_entry_to_move,
                        lambda i, **kw: time_entry_model.move_time_entry(
                            i, {"id": "d2"}, from_dossier_id="d1", **kw)[:2],
                        "dossier_id", "d2"),
    "move_expense": ("expenses", _expense_to_move,
                     lambda i, **kw: expense_model.move_expense(
                         i, {"id": "d2"}, from_dossier_id="d1", **kw)[:2],
                     "dossier_id", "d2"),
    "update_note": ("notes", _note,
                    lambda i, **kw: note_model.update_note(
                        i, {"content": "Second jet."}, **kw),
                    "content", "Second jet."),
    "update_task": ("tasks", _task,
                    lambda i, **kw: task_model.update_task(
                        i, {"priority": "haute"}, **kw),
                    "priority", "haute"),
    # Lot 2A (T1): the triple return — its third member (« changed ») is
    # not this harness's concern.
    "update_metadata": ("documents", _document,
                        lambda i, **kw: document_model.update_metadata(
                            i, {"display_name": "Mise en demeure"}, **kw)[:2],
                        "display_name", "Mise en demeure"),
    "move_document": ("documents", _document_and_folder,
                      lambda i, **kw: document_model.move_document(
                          "d1", i, "f1", **kw)[:2],
                      "folder_id", "f1"),
    "confirmer_categorie": ("documents", _presumed_document,
                            lambda i, **kw: document_model.confirmer_categorie(
                                i, "juriste@example.com", **kw),
                            "category_source", "juriste"),
    "update_analyse": ("documents", _analysed_document,
                       lambda i, **kw: document_model.update_analyse(
                           i, {"resume": "Lettre au confrère."},
                           par="juriste", **kw),
                       None, None),
    # Not an edit form but a one-click control (critique, lot 0a): the
    # « Confirmer » button of an analysis says « I read THIS version ».
    "confirmer_analyse": ("documents", _analysed_document,
                          lambda i, **kw: document_model.confirmer_analyse(
                              i, "juriste@example.com", **kw),
                          "category_source", "juriste"),
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
    "move_time_entry": (time_entry_model, "get_time_entry"),
    "move_expense": (expense_model, "get_expense"),
    "update_note": (note_model, "get_note"),
    "update_task": (task_model, "get_task"),
    "update_metadata": (document_model, "get_document"),
    "update_analyse": (document_model, "get_document"),
    "confirmer_analyse": (document_model, "get_document"),
    "move_document": (document_model, "get_document"),
    "confirmer_categorie": (document_model, "get_document"),
}

# The mutators that read their record ONLY inside their transaction (lot 0b,
# step B2): no pre-read exists for a rival to slip in after, so the race is
# armed at their COMMIT instead — the rival lands after the transactional
# read, and the real ``transactional`` retry must re-read and refuse.
_READS_ONLY_IN_TRANSACTION = {"update_time_entry", "update_expense"}
# Lot 2A (T1, 2026-09-27): the document writers read ONLY inside their
# transaction too — and write a PARTIAL update(), never the whole document,
# so a rival write to another field survives by construction. Changed
# deliberately: update_metadata / update_analyse / confirmer_analyse used
# to take the plain legacy set() without an etag; move_document and
# confirmer_categorie join with this lot.
_DOCUMENT_PARTIAL = {"update_metadata", "update_analyse", "confirmer_analyse",
                     "move_document", "confirmer_categorie"}
# Lot 3a (step 2): the two moves read only inside their transaction and
# write a PARTIAL update() of the dossier link and its stamp.
_MOVES = {"move_time_entry", "move_expense"}
_RACE_AT_COMMIT = _READS_ONLY_IN_TRANSACTION | _DOCUMENT_PARTIAL | _MOVES
# D17 (2026-09-27): a note whose CONTENT changes keeps a revision on every
# path, so even without an etag its write is the guarded transaction that
# carries the snapshot (the model guards on the version it read). Its case
# changes the content: it left the legacy list below, deliberately, for
# the two tests after it.
_CONTENT_REVISED_WITHOUT_ETAG = {"update_note"}
_LEGACY_WITHOUT_ETAG = sorted(
    set(_CASES) - _RACE_AT_COMMIT - _CONTENT_REVISED_WITHOUT_ETAG)


def _write_kind(case):
    """The op a case's guarded write stages on its own document."""
    if case.startswith("set_") or case in _DOCUMENT_PARTIAL | _MOVES:
        return "update"
    return "set"


def _arm_race(db, monkeypatch, case, path, rival) -> None:
    """Run *rival* (another process's write) between the model's read and
    its commit, through the seam that case actually has."""
    if case in _RACE_AT_COMMIT:
        def _hook(info) -> None:
            if any(p == path for _op, p in info.ops):
                remove()
                rival()

        remove = db.add_commit_hook(_hook)
        return

    module, getter_name = _GETTERS[case]
    real_getter = getattr(module, getter_name)

    def racing_getter(doc_id):
        doc = real_getter(doc_id)
        rival()
        return doc

    monkeypatch.setattr(module, getter_name, racing_getter)


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
    assert (_write_kind(case), path) in guarded[0].ops
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
    """The model's own check passes (it read the version the caller named);
    a rival write lands right after that read. The transactional re-read
    catches it, and the rival's write survives intact."""
    row_id, path, edit = _setup(db, case)
    etag0 = db.peek(path)["etag"]
    _arm_race(db, monkeypatch, case, path, lambda: db.external_write(
        path, {**db.peek(path), "etag": "e-rival", "rival": True}))

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
    _arm_race(db, monkeypatch, case, path, lambda: db.external_delete(path))

    doc, errors = edit(row_id, expected_etag=etag0)

    assert doc is None and len(errors) == 1 and "introuvable" in errors[0]
    assert db.peek(path) is None
    assert db.commits == []


# ══════════════════════════════════════════════════════════════════════
# 3. None : le chemin d'avant, octet pour octet
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("case", _LEGACY_WITHOUT_ETAG, ids=_LEGACY_WITHOUT_ETAG)
def test_without_an_etag_the_write_is_the_plain_legacy_one(db, case):
    row_id, path, edit = _setup(db, case)
    db.external_write(path, {**db.peek(path), "etag": "e-rival"})
    db.reset_logs()

    doc, errors = edit(row_id)

    # Last-write-wins, exactly as before: the rival etag is not consulted.
    assert errors == [], errors
    assert _stored_field(db, case, path) == _expected_value(case)
    assert all(c.transaction is None for c in db.commits)
    assert db.commits[-1].ops == ((_write_kind(case), path),)
    # No read through a transaction, and no read beyond the model's own.
    assert not any(r.transactional for r in db.reads)


@pytest.mark.parametrize("case", sorted(_READS_ONLY_IN_TRANSACTION),
                         ids=sorted(_READS_ONLY_IN_TRANSACTION))
def test_without_an_etag_a_billing_edit_is_last_write_wins_in_a_transaction(
    db, case,
):
    """Changed deliberately (lot 0b, step B2): these two used to take the
    plain legacy ``set()`` above. The version is still not consulted — the
    rival etag does not refuse the edit — but the read, the invoiced check
    and the write now share ONE transaction, because the invoice that can
    commit in between does not care which version the caller read."""
    row_id, path, edit = _setup(db, case)
    db.external_write(path, {**db.peek(path), "etag": "e-rival"})
    db.reset_logs()

    doc, errors = edit(row_id)

    assert errors == [], errors
    assert _stored_field(db, case, path) == _expected_value(case)
    assert [c.ops for c in db.commits] == [(("set", path),)]
    assert all(c.transaction is not None for c in db.commits)
    assert db.reads and all(r.transactional for r in db.reads)
    assert doc["etag"] == db.peek(path)["etag"] != "e-rival"


@pytest.mark.parametrize("case", sorted(_DOCUMENT_PARTIAL),
                         ids=sorted(_DOCUMENT_PARTIAL))
def test_without_an_etag_a_document_write_is_partial_and_transactional(
    db, case,
):
    """Changed deliberately (lot 2A, T1): the three document writers that
    took the plain legacy ``set()`` above read inside a transaction now,
    whatever the caller passes, and write only their own keys. The version
    is still not consulted — the rival etag does not refuse the write — but
    the rival's OTHER field survives, which the full-document ``set()``
    reverted."""
    row_id, path, edit = _setup(db, case)
    db.external_write(path, {**db.peek(path), "etag": "e-rival",
                             "notes_internes": "Écrit ailleurs"})
    db.reset_logs()

    doc, errors = edit(row_id)

    assert errors == [], errors
    stored = db.peek(path)
    assert _stored_field(db, case, path) == _expected_value(case)
    assert stored["notes_internes"] == "Écrit ailleurs"
    assert db.commits and all(c.transaction is not None for c in db.commits)
    assert ("update", path) in db.commits[-1].ops
    assert ("set", path) not in db.commits[-1].ops
    assert db.reads and all(r.transactional for r in db.reads)
    assert doc["etag"] == stored["etag"] != "e-rival"


@pytest.mark.parametrize("case", sorted(_MOVES), ids=sorted(_MOVES))
def test_without_an_etag_a_move_is_partial_and_transactional(db, case):
    """Lot 3a (step 2): a move never takes a legacy path. Without an etag
    the version is not consulted — the rival etag does not refuse it —, but
    the read, the invoiced check and the partial write share ONE
    transaction, and the rival's OTHER field survives."""
    row_id, path, edit = _setup(db, case)
    db.external_write(path, {**db.peek(path), "etag": "e-rival",
                             "description": "Écrit ailleurs"})
    db.reset_logs()

    doc, errors = edit(row_id)

    assert errors == [], errors
    stored = db.peek(path)
    assert stored["dossier_id"] == "d2"
    assert stored["dossier_file_number"] == "2026-002"
    assert stored["description"] == "Écrit ailleurs"
    assert [c.ops for c in db.commits] == [(("update", path),)]
    assert all(c.transaction is not None for c in db.commits)
    assert db.reads and all(r.transactional for r in db.reads)
    assert doc["etag"] == stored["etag"] != "e-rival"


def test_without_an_etag_a_note_content_change_carries_its_revision(db):
    """Changed deliberately (D17): update_note took the plain legacy
    ``set()`` above. The version is still not consulted — the rival etag
    does not refuse the edit — but the replaced text is snapshotted, in the
    SAME transaction as the write, guarded on the version the model read."""
    row_id, path, edit = _setup(db, "update_note")
    db.external_write(path, {**db.peek(path), "etag": "e-rival"})
    db.reset_logs()

    doc, errors = edit(row_id)

    assert errors == [], errors
    assert db.peek(path)["content"] == "Second jet."
    assert len(db.commits) == 1 and db.commits[0].transaction is not None
    ops = db.commits[0].ops
    assert ("set", path) in ops and len(ops) == 2
    (rev,) = db.peek_collection(f"{path}/revisions").values()
    assert rev["previous_value"] == "Premier jet."
    assert rev["previous_etag"] == "e-rival" and rev["new_etag"] == doc["etag"]


def test_without_an_etag_a_note_edit_that_keeps_its_content_is_legacy(db):
    """No content change, no revision — and then the plain legacy set()."""
    row_id, path, _edit = _setup(db, "update_note")
    doc, errors = note_model.update_note(row_id, {"title": "Titre revu"})
    assert errors == [], errors
    assert db.peek(path)["title"] == "Titre revu"
    assert all(c.transaction is None for c in db.commits)
    assert db.commits[-1].ops == (("set", path),)
    assert db.peek_collection(f"{path}/revisions") == {}
    assert "_revision_id" not in doc


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
    # Widened in lot 1a: update_task now also hands the task's dossier to
    # the cascade (find_step_for_task searches it first).
    monkeypatch.setattr(task_model, "_sync_protocol_step",
                        lambda tid, status, **kw: synced.append((tid, status)))
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
    # Changed deliberately (lot 2A, T1): the cache is a partial update().
    assert len(journal) == 1 and ("update", path) in ops
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

    doc, errors, changed = document_model.update_metadata(
        row_id, {"display_name": "Mise en demeure"}, expected_etag=form_etag)
    assert errors == [] and changed is True

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
    # protocol_model too (lot 1b): update_task / reopen_task consult the
    # step a task is linked from, and a MagicMock there would answer
    # « not linked » for every query — a fake that accepts what the store
    # refuses proves nothing.
    # models.revision too: a content replacement stages its write-once
    # snapshot through that module's OWN client reference.
    # hearing_model too (lot 1b, L7): update_hearing and decide_rendez_vous
    # write calendar events through their model.
    # folder_model too (lot 2A, T7): manage_folder reads and writes the
    # filing tree through its model. doc_template_model too (lot 2A, T10):
    # update_template reads and writes the template through its model.
    return install(monkeypatch, *_MODULES, protocol_model, revision_model,
                   hearing_model, folder_model, doc_template_model, dav_sync,
                   write_support)


def _closed_task(db):
    tid = _task(db)
    _doc, errors = task_model.update_task(tid, {"status": "terminée"})
    assert errors == [], errors
    return tid


def _analyse(db):
    """The dossier's théorie de la cause, created by the REAL init. Its
    row is the note, but edit_analyse is addressed by the DOSSIER: the
    factory returns both (row id, argument id)."""
    did = _dossier(db)
    note, errors, created = note_model.ensure_analyse_note(did)
    assert errors == [] and created, errors
    return note["id"], did


def _protocol(db):
    """A protocol born through the REAL creator (conventionnel: no
    template steps, so no regime gate on the dossier's tribunal)."""
    did = _dossier(db)
    proto, errors = protocol_model.create_protocol(
        did, "conventionnel", DT, {"title": "Protocole"})
    assert errors == [], errors
    return proto["id"]


def _protocol_step(db):
    """A custom step of a real protocol. Its row lives in the steps
    subcollection, and the tool is addressed by TWO ids: the factory
    returns (row path under protocols/, the argument dict)."""
    pid = _protocol(db)
    step, errors = protocol_model.add_step(pid, {"title": "Étape"})
    assert errors == [], errors
    return (f"{pid}/steps/{step['id']}",
            {"protocol_id": pid, "step_id": step["id"]})


def _hearing(db):
    """A calendar event born through the REAL creator (« Général »)."""
    doc, errors = hearing_model.create_hearing(
        {"title": "Rencontre", "start_datetime": DT.replace(hour=14)})
    assert errors == [], errors
    return doc["id"]


def _booking(db):
    """A pending « Bookings with me » request: a real event, then the
    server-owned gate the Bookings sync sets — through the model's own
    server_fields door, the one the sync uses."""
    hid = _hearing(db)
    _doc, errors = hearing_model.update_hearing(hid, {}, server_fields={
        "source": "bookings", "confirmation": "à_confirmer",
        "graph_event_id": "EVT-1", "client_email": "client@ex.com"})
    assert errors == [], errors
    return hid


def _contains(fragment: str):
    """An expectation on a stored value that is not the argument verbatim
    (edit_analyse writes a bloc INTO the note)."""
    return lambda stored: fragment in stored


def _folder(db):
    """A folder of a real dossier, born through the REAL creator. The tool
    is addressed by the folder AND its dossier (and an action): the factory
    returns (row id, the argument dict)."""
    did = _dossier(db)
    folder, errors = folder_model.create_folder(did, "Pièces")
    assert errors == [], errors
    return folder["id"], {"folder_id": folder["id"], "dossier_id": did,
                          "action": "rename"}


def _template(db):
    """A gabarit record as the template model stores it (a metadata edit
    touches no Storage object, so none is planted)."""
    db.seed("doc_templates/tpl1", {
        **doc_template_model._default_doc(), "id": "tpl1", "name": "Lettre",
        "category": "correspondance", "kind": "gabarit",
        "filename": "lettre.docx", "file_size": 10, "sha256": "0" * 64,
        "storage_path": "users/kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s/templates/tpl1/v1/"
                        "lettre.docx",
        "created_at": DT, "updated_at": DT, "etag": "e0"})
    return "tpl1"


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
    # Lot 1b — the agenda edits.
    "update_task": ("tasks", _task, "task_id",
                    {"title": "Titre révisé"}, ("title", "Titre révisé")),
    "reopen_task": ("tasks", _closed_task, "task_id",
                    {"status": "en_cours"}, ("status", "en_cours")),
    "update_note": ("notes", _note, "note_id",
                    {"title": "Titre révisé"}, ("title", "Titre révisé")),
    "edit_analyse": ("notes", _analyse, "dossier_id",
                     {"operations": [{"bloc": "C", "mode": "append",
                                      "content": "Ajout de Claude."}]},
                     ("content", _contains("Ajout de Claude."))),
    # Lot 1b (L6) — the protocol edits. A step is addressed by its
    # protocol AND its own id: no single id key (None), the factory hands
    # the argument dict.
    "update_protocol": ("protocols", _protocol, "protocol_id",
                        {"notes": "Suivi"}, ("notes", "Suivi")),
    "update_protocol_step": ("protocols", _protocol_step, None,
                             {"notes": "Suivi"}, ("notes", "Suivi")),
    # Lot 1b (L7) — the calendar. The decision demands its key (plan rule
    # 7), so its case carries one.
    "update_hearing": ("hearings", _hearing, "hearing_id",
                       {"notes": "Suivi"}, ("notes", "Suivi")),
    "decide_rendez_vous": ("hearings", _booking, "hearing_id",
                           {"action": "confirmer", "lier_partie": False,
                            "idempotency_key": "cle-decision-e2e"},
                           ("confirmation", "")),
    # Lot 2A (T7) — the document and folder edits.
    "update_document": ("documents", _document, "document_id",
                        {"display_name": "Mise en demeure"},
                        ("display_name", "Mise en demeure")),
    "manage_folder": ("folders", _folder, None,
                      {"name": "Expertises"}, ("name", "Expertises")),
    # Lot 2A (T10) — a template's metadata (its file path is the lot's own
    # tests: tests/test_mcp_template_writes.py).
    "update_template": ("doc_templates", _template, "template_id",
                        {"name": "Lettre révisée"}, ("name", "Lettre révisée")),
}
_E2E = pytest.mark.parametrize("tool", sorted(_HANDLER_CASES),
                               ids=sorted(_HANDLER_CASES))

# A second, DIFFERENT change for the chained-edit test, where « the same
# field, another value » does not make a valid call: a reopened task can
# only go back to à_faire; a théorie edit is an operation list.
_SECOND_ARGS = {
    "reopen_task": {"status": "à_faire"},
    "edit_analyse": {"operations": [{"bloc": "D", "mode": "append",
                                     "content": "Deuxième version"}]},
}

# Tools that DEMAND expected_etag for the change the case makes (plan rule
# 3, D8: replacing prose). They cannot « guard their own read » without
# one — they refuse the call instead, which the own-read test asserts.
_ETAG_DEMANDED = {"edit_analyse", "decide_rendez_vous"}

# A decision is taken ONCE (lot 1b, L7): the chained-edit test cannot make a
# second, different write — confirming a confirmed request is the honest
# no-op, refusing it is refused. Its second call proves that instead.
_SINGLE_DECISION = {"decide_rendez_vous"}

# The getter the HANDLER reads its record through (the bulk reader for the
# reclassifiers) — the seam a rival write is slipped in after.
_HANDLER_GETTERS = {
    "update_partie": (partie_model, "get_partie"),
    "update_dossier": (dossier_model, "get_dossier"),
    "update_time_entry": (time_entry_model, "get_time_entry"),
    "update_expense": (expense_model, "get_expense"),
    "set_time_entry_phase": (time_entry_model, "get_time_entries_bulk"),
    "set_expense_phase": (expense_model, "get_expenses_bulk"),
    "update_task": (task_model, "get_task_strict"),
    "reopen_task": (task_model, "get_task_strict"),
    "update_note": (note_model, "get_note_strict"),
    "edit_analyse": (note_model, "find_analyse_note_strict"),
    "update_protocol": (protocol_model, "get_protocol_strict"),
    "update_protocol_step": (protocol_model, "get_protocol_strict"),
    "update_hearing": (hearing_model, "get_hearing_strict"),
    "decide_rendez_vous": (hearing_model, "get_hearing_strict"),
    "update_document": (document_model, "get_document_strict"),
    "manage_folder": (folder_model, "list_dossier_folders"),
    "update_template": (doc_template_model, "get_template"),
}


def _e2e_setup(db, tool):
    coll, factory, id_key, extra, _expect = _HANDLER_CASES[tool]
    made = factory(db)
    row_id, arg_id = made if isinstance(made, tuple) else (made, made)
    path = f"{coll}/{row_id}"
    db.reset_logs()
    ids = arg_id if isinstance(arg_id, dict) else {id_key: arg_id}
    return path, {**ids, **extra}


def _stored_as_expected(stored, expected) -> bool:
    return expected(stored) if callable(expected) else stored == expected


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
    assert _stored_as_expected(stored[field], value), stored[field]
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
    if tool in _ETAG_DEMANDED:
        # No own-read fallback exists here BY DESIGN: the change replaces
        # prose, so the version must come from the caller. Omitting it is
        # refused, and nothing at all is written.
        before = db.peek(path)
        with pytest.raises(tools.ToolArgumentError, match="expected_etag"):
            getattr(handlers, tool)(dict(args))
        assert db.peek(path) == before
        assert _row_commits(db, path) == []
        return
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
    first_args = dict(args)
    if tool in _ETAG_DEMANDED:
        first_args["expected_etag"] = db.peek(path)["etag"]
    first = getattr(handlers, tool)(first_args)
    etag1 = first["entity"]["etag"]
    assert etag1 == db.peek(path)["etag"]

    if tool in _SINGLE_DECISION:
        # Another key: the same key with another expected_etag is refused
        # as a different call (write_support's fingerprint).
        second = getattr(handlers, tool)({
            **args, "expected_etag": etag1,
            "idempotency_key": "cle-decision-e2e-bis"})
        assert second["changed"] is False
        assert second["entity"]["etag"] == etag1 == db.peek(path)["etag"]
        return

    if tool in _SECOND_ARGS:
        second_args = {**args, **_SECOND_ARGS[tool]}
    elif tool.startswith("set_"):
        second_args = {**args, "sous_phase": "AUD-01"}
    else:
        field = _HANDLER_CASES[tool][4][0]
        second_args = {**args, field: "Deuxième version"}
    second = getattr(handlers, tool)({**second_args, "expected_etag": etag1})
    assert second["entity"]["etag"] == db.peek(path)["etag"] != etag1


# ══════════════════════════════════════════════════════════════════════
# 6. Les écrivains SANS expected_etag comparent quand même à leur lecture
# ══════════════════════════════════════════════════════════════════════
#
# Plan, règle 3 : « omis, le gestionnaire compare-et-écrit contre l'etag
# qu'il vient de lire ». Les six outils d'édition l'ont reçu à l'étape 5 ;
# cinq écrivains qui n'acceptent aucun etag calculent pourtant eux aussi
# leur écriture à partir d'une LECTURE, puis la réécrivent entière :
#
# * append_to_note — le contenu stocké est « la note lue + le bloc » ;
# * complete_task — chaque verdict (même état, l'autre état terminal, la
#   réouverture refusée) et la description combinée viennent de la tâche lue ;
# * complete_dossier — le « remplir si vide » a été jugé sur le dossier lu ;
# * record_signification / record_prescription_event — le registre est la
#   liste lue plus l'entrée neuve, réécrite en bloc.
#
# Sans garde, une écriture glissée entre la lecture et le commit était
# EFFACÉE sous une enveloppe de succès — deux appels parallèles de Claude à
# record_signification, et la première signification disparaissait. La
# critique du lot 0a leur donne la comparaison ; ce qui suit la prouve sur
# le vrai magasin, avec les vrais gestionnaires et les vrais modèles.


def _dossier_with_parties(db):
    doc, errors = dossier_model.create_dossier({
        "file_number": "2026-002", "title": "Tremblay c. Lavoie",
        "clients": [{"id": "p1", "name": "Jean Tremblay",
                     "roles": ["demandeur"]}],
        "opposing_parties": [{"id": "p2", "name": "Paul Lavoie",
                              "roles": ["défendeur"]}],
    })
    assert errors == [], errors
    return doc["id"]


# tool → (collection, factory, arguments(id), getter, rival fields,
#         the stored field a successful call writes)
_OWN_READ_CASES = {
    "append_to_note": (
        "notes", _note, lambda i: {"note_id": i, "content": "Suite."},
        (note_model, "get_note"), {"content": "Réécrite au téléphone."},
        "content"),
    "complete_task": (
        "tasks", _task, lambda i: {"task_id": i},
        (task_model, "get_task"), {"status": "annulée"},
        "status"),
    "complete_dossier": (
        "dossiers", _dossier_with_parties,
        lambda i: {"dossier_id": i, "sommaire": "Rempli par Claude."},
        (dossier_model, "get_dossier"), {"sommaire": "Écrit dans l'appli."},
        "sommaire"),
    "record_signification": (
        "dossiers", _dossier_with_parties,
        lambda i: {"dossier_id": i, "partie_id": "p2",
                   "date": "2026-07-15", "mode": "huissier"},
        (dossier_model, "get_dossier"), {"rival": True},
        "significations"),
    "record_prescription_event": (
        "dossiers", _dossier_with_parties,
        lambda i: {"dossier_id": i, "type": "interruption_depot",
                   "date": "2026-05-15"},
        (dossier_model, "get_dossier"), {"rival": True},
        "prescription_events"),
}
_OWN = pytest.mark.parametrize("tool", sorted(_OWN_READ_CASES),
                               ids=sorted(_OWN_READ_CASES))


def _own_setup(monkeypatch, tool):
    db = _e2e_db(monkeypatch)
    coll, factory, make_args, *_rest = _OWN_READ_CASES[tool]
    row_id = factory(db)
    db.reset_logs()
    return db, f"{coll}/{row_id}", make_args(row_id)


def test_the_own_read_cases_are_the_writes_that_rewrite_what_they_read():
    """Not derived (nothing in the registry says « computes from a read »),
    so pinned from both sides: none of them accepts an expected_etag, and
    none of the tools that do is listed twice."""
    for tool in _OWN_READ_CASES:
        assert "expected_etag" not in tools.TOOLS[tool]["input_schema"][
            "properties"], tool
    assert not set(_OWN_READ_CASES) & set(_HANDLER_CASES)


@_OWN
def test_a_write_racing_the_handlers_read_is_refused_never_erased(
    monkeypatch, tool,
):
    db, path, args = _own_setup(monkeypatch, tool)
    _c, _f, _a, (module, name), rival, _field = _OWN_READ_CASES[tool]
    real = getattr(module, name)
    fired = []

    def racing(*a, **kw):
        result = real(*a, **kw)
        if not fired:                  # right after the HANDLER's read
            fired.append(True)
            db.external_write(path, {**db.peek(path), **rival,
                                     "etag": "e-rival"})
        return result

    monkeypatch.setattr(module, name, racing)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))

    assert fired and excinfo.value.reason == "stale_etag"
    message = str(excinfo.value)
    assert "Rien n'a été écrit" in message and "renvoyez l'appel" in message
    # Its schema refuses an expected_etag: the remedy never asks for one.
    assert "etag actuel" not in message
    stored = db.peek(path)
    assert stored["etag"] == "e-rival"
    for key, value in rival.items():
        assert stored[key] == value          # the rival's write SURVIVES
    assert _row_commits(db, path) == []


@_OWN
def test_without_a_race_the_guarded_write_commits_once(monkeypatch, tool):
    db, path, args = _own_setup(monkeypatch, tool)
    before = db.peek(path)
    field = _OWN_READ_CASES[tool][5]

    getattr(handlers, tool)(dict(args))

    stored = db.peek(path)
    assert stored[field] != before.get(field)
    assert stored["updated_via"] == "mcp"
    (commit,) = _row_commits(db, path)
    assert commit.transaction is not None    # the guarded, transactional path


def test_two_parallel_significations_both_survive_or_one_is_refused(
    monkeypatch,
):
    """The concrete loss: Claude records two significations in PARALLEL.
    Both handlers read the same register; before the guard, the second
    write erased the first entry. Now the loser is refused and the winner's
    entry is kept — the caller re-reads and sends it again."""
    db, path, args = _own_setup(monkeypatch, "record_signification")
    real = dossier_model.get_dossier
    state = {"reads": 0}

    def interleaved(dossier_id):
        doc = real(dossier_id)
        state["reads"] += 1
        if state["reads"] == 1:
            # The SECOND call runs to completion right after the first
            # call's read — the parallel tool call. In a FRESH context, as a
            # separate request has: nested in this one, its commit would be
            # handed up to the first call's writing block (models.provenance)
            # and read there as the first call's own.
            monkeypatch.setattr(dossier_model, "get_dossier", real)
            contextvars.Context().run(handlers.record_signification, {
                **args, "partie_id": "p1", "date": "2026-07-16"})
            monkeypatch.setattr(dossier_model, "get_dossier", interleaved)
        return doc

    monkeypatch.setattr(dossier_model, "get_dossier", interleaved)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.record_signification(dict(args))
    assert excinfo.value.reason == "stale_etag"
    (kept,) = db.peek(path)["significations"]
    assert kept["partie_id"] == "p1"

    # The re-sent call, on a fresh read, adds its entry beside the other.
    handlers.record_signification(dict(args))
    assert {s["partie_id"] for s in db.peek(path)["significations"]} == {
        "p1", "p2"}


# ══════════════════════════════════════════════════════════════════════
# 7. « …or the last write result » — the etag a CHAIN of writes presents
# ══════════════════════════════════════════════════════════════════════
#
# Every `expected_etag` description sends the caller to « your latest read
# … or the last write result ». Until the lot 0a critique that was true of
# the edit tools' results only: create_time_entry / create_expense handed
# back no etag, and the three dossier recorders none of the dossier's (the
# review's correction — a TOP-LEVEL `dossier_etag`, never inside the
# signification or event entity, an array entry with no etag of its own).


@pytest.mark.parametrize("creator, editor, id_key, edit", [
    ("create_time_entry", "update_time_entry", "time_entry_id",
     {"description": "Révision"}),
    ("create_expense", "update_expense", "expense_id",
     {"description": "Timbre (corrigé)"}),
])
def test_a_created_entry_hands_back_the_etag_its_next_edit_presents(
    monkeypatch, creator, editor, id_key, edit,
):
    db = _e2e_db(monkeypatch)
    dossier_id = _dossier_with_parties(db)
    args = {"dossier_id": dossier_id, "date": "2026-03-04",
            "description": "Premier jet"}
    args.update({"hours": 1.5} if creator == "create_time_entry"
                else {"amount_cents": 5000, "category": "timbre_judiciaire"})
    created = getattr(handlers, creator)(args)
    entity = created["entity"]
    coll = "timeentries" if creator == "create_time_entry" else "expenses"
    assert entity["etag"] == db.peek(f"{coll}/{entity['id']}")["etag"]

    edited = getattr(handlers, editor)(
        {id_key: entity["id"], **edit, "expected_etag": entity["etag"]})
    assert edited["entity"]["etag"] == db.peek(f"{coll}/{entity['id']}")["etag"]


@pytest.mark.parametrize("recorder, args", [
    ("complete_dossier", {"sommaire": "Rempli par Claude."}),
    ("record_signification", {"partie_id": "p2", "date": "2026-07-15",
                              "mode": "huissier"}),
    ("record_prescription_event", {"type": "interruption_depot",
                                   "date": "2026-05-15"}),
])
def test_a_recorder_hands_back_the_dossier_etag_update_dossier_presents(
    monkeypatch, recorder, args,
):
    db = _e2e_db(monkeypatch)
    dossier_id = _dossier_with_parties(db)
    result = getattr(handlers, recorder)({"dossier_id": dossier_id, **args})
    stored = db.peek(f"dossiers/{dossier_id}")
    assert result["dossier_etag"] == stored["etag"]
    assert "etag" not in (result.get("entity") or {})   # an array entry

    edited = handlers.update_dossier({
        "dossier_id": dossier_id, "sommaire": "Deuxième version.",
        "expected_etag": result["dossier_etag"]})
    assert edited["entity"]["etag"] == db.peek(f"dossiers/{dossier_id}")["etag"]


def test_the_handed_back_etags_are_declared_and_never_required():
    """A result replayed from `mcp_idempotency` may predate the key (24 h
    cache): declared, typed, never required — the `_written_etag` rule."""
    from mcp.output_schemas import OUTPUT_SCHEMAS

    for tool in ("complete_dossier", "record_signification",
                 "record_prescription_event"):
        schema = OUTPUT_SCHEMAS[tool]
        assert schema["properties"]["dossier_etag"]["type"] == "string", tool
        assert "dossier_etag" not in schema.get("required", []), tool
        entity = schema["properties"].get("entity", {})
        assert "etag" not in entity.get("properties", {}), tool
    for tool in ("create_time_entry", "create_expense"):
        entity = OUTPUT_SCHEMAS[tool]["properties"]["entity"]
        assert entity["properties"]["etag"]["type"] == "string", tool
        assert "etag" not in entity.get("required", []), tool
