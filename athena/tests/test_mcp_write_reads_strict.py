"""A connector write whose record could not be READ says so — never
« introuvable » (finitions, sync-6 and robustness-3).

Lot 4's fixes set the rule: « une panne n'est plus jamais « Dossier
introuvable » ». Seven write handlers still read their PRIMARY record
through a fail-open getter, which swallows a Firestore blip into ``None``:

* ``update_partie``, and the party / lawyer resolution ``create_dossier`` and
  ``update_dossier`` share — « Contact introuvable … Créez-le avec
  create_partie » about a contact that exists: the path to a DUPLICATE
  contact, which a contact write then bumps into the phone's address book;
* ``append_to_note`` (« Note introuvable … Utilisez list_notes »),
  ``complete_task``, ``update_invoice``, ``record_document_analysis``,
  ``update_time_entry`` and ``update_expense``.

Each now reads through the model's STRICT getter and refuses « n'a pas pu
être lu — réessayez » under reason ``read_unavailable`` — the
stop-the-batch signal OBSERVABILITY.md describes, where « introuvable » was
logged as an ordinary ``argument_refused``.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools


def _boom(*_a, **_k):
    raise RuntimeError("store down")


# (tool, args, the strict getter it must read through)
_CASES = [
    ("update_partie", {"partie_id": "p1", "notes": "x"},
     (handlers.partie_model, "get_partie_strict")),
    ("append_to_note", {"note_id": "n1", "content": "Ajout."},
     (handlers.note_model, "get_note_strict")),
    ("complete_task", {"task_id": "t1", "status": "terminée"},
     (handlers.task_model, "get_task_strict")),
    ("update_invoice", {"invoice_id": "i1", "expected_etag": "e",
                        "notes": "x"},
     (handlers.invoice_model, "get_invoice_strict")),
    ("record_document_analysis", {"document_id": "doc1", "sous_nature": "X"},
     (handlers.document_model, "get_document_strict")),
    ("update_time_entry", {"time_entry_id": "te1", "description": "x"},
     (handlers.time_entry_model, "get_time_entry_strict")),
    ("update_expense", {"expense_id": "ex1", "description": "x"},
     (handlers.expense_model, "get_expense_strict")),
]


@pytest.mark.parametrize("tool, args, getter", _CASES,
                         ids=[c[0] for c in _CASES])
def test_a_failed_read_refuses_as_unreadable_never_introuvable(
        monkeypatch, tool, args, getter):
    module, name = getter
    monkeypatch.setattr(module, name, _boom)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))
    assert excinfo.value.reason == "read_unavailable"
    message = str(excinfo.value)
    assert "n'a pas pu être lu" in message
    assert "introuvable" not in message
    assert "create_partie" not in message


@pytest.mark.parametrize("tool, args, getter", _CASES,
                         ids=[c[0] for c in _CASES])
def test_an_absent_record_is_still_introuvable(monkeypatch, tool, args, getter):
    """The fix changes the answer to a FAILED read only."""
    module, name = getter
    monkeypatch.setattr(module, name, lambda *_a, **_k: None)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))
    assert "introuvable" in str(excinfo.value)
    assert excinfo.value.reason != "read_unavailable"


@pytest.mark.parametrize("entry", [
    {"partie_id": "p1"},
    {"partie_id": "p1", "avocat_partie_id": "p2"},
])
def test_a_dossier_party_that_cannot_be_read_never_invites_create_partie(
        monkeypatch, entry):
    """The party resolution of create_dossier / update_dossier: « Créez-le
    avec create_partie » on an outage invited a duplicate contact."""
    real = handlers.partie_model.get_partie_strict

    def reader(pid):
        if pid == entry.get("avocat_partie_id", "p1"):
            raise RuntimeError("store down")
        return {"id": pid, "type": "individual", "first_name": "Jean",
                "last_name": "Tremblay", "contact_role": "client"}

    monkeypatch.setattr(handlers.partie_model, "get_partie_strict", reader)
    del real
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers._resolve_party_entries([entry], "clients")
    assert excinfo.value.reason == "read_unavailable"
    assert "create_partie" not in str(excinfo.value)
    assert "introuvable" not in str(excinfo.value)


def test_the_write_handlers_never_read_their_record_fail_open():
    """The tripwire: none of these handlers' bodies names the fail-open
    getter of its own record again."""
    import ast
    import inspect

    banned = {
        "_update_partie_impl": {"get_partie"},
        "_append_to_note_impl": {"get_note"},
        "_complete_task_impl": {"get_task"},
        "_update_invoice_impl": {"get_invoice"},
        "_record_document_analysis_impl": {"get_document"},
        "_resolve_party_entries": {"get_partie"},
    }
    tree = ast.parse(inspect.getsource(handlers))

    def attrs_outside_lambdas(node) -> set:
        # A `reread=lambda: …get_note(…)` only names the time of a rival
        # write in a stale refusal — best-effort by design, never the read
        # the write decides on.
        out = set()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Attribute):
                out.add(child.attr)
            out |= attrs_outside_lambdas(child)
        return out

    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in banned:
            found[node.name] = attrs_outside_lambdas(node) & banned[node.name]
    assert set(found) == set(banned), set(banned) - set(found)
    assert all(not v for v in found.values()), found
    # The two billing edits hand the strict getters to _billing_edit.
    src = inspect.getsource(handlers.update_time_entry)
    assert "get_time_entry_strict" in src
    assert "get_expense_strict" in inspect.getsource(handlers.update_expense)


_ID_KEYS = {"update_partie": "partie_id", "append_to_note": "note_id",
            "complete_task": "task_id", "update_invoice": "invoice_id",
            "record_document_analysis": "document_id",
            "update_time_entry": "time_entry_id",
            "update_expense": "expense_id"}


@pytest.mark.parametrize("bad_id", ["a/b/c", "a/b", "..", "__x__"])
@pytest.mark.parametrize("tool, args, getter", _CASES,
                         ids=[c[0] for c in _CASES])
def test_an_id_that_can_name_no_record_is_introuvable_never_read(
        monkeypatch, tool, args, getter, bad_id):
    """Review of the finitions. The store REFUSES a read of such an id, so
    through the strict reader it came back « n'a pas pu être lu —
    réessayez » under read_unavailable — the stop-the-batch signal, for a
    call that can never succeed — and a slashed id with an even number of
    segments was re-split by the client into a path reading a record
    DEEPER in the tree (``notes/{id}/revisions/{r}`` as a note). It is an
    absence (the _read_dossier_strict rule), and nothing is read."""
    module, name = getter

    def _never(*_a, **_k):
        raise AssertionError("an unaddressable id must never be read")

    monkeypatch.setattr(module, name, _never)
    call = {**args, _ID_KEYS[tool]: bad_id}
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(call)
    assert excinfo.value.reason != "read_unavailable"
    assert "n'a pas pu être lu" not in str(excinfo.value)
    assert "introuvable" in str(excinfo.value)


@pytest.mark.parametrize("bad_id", ["a/b/c", ".."])
def test_the_contact_readers_of_lot_4b_never_read_an_unaddressable_id(
        monkeypatch, bad_id):
    def _never(*_a, **_k):
        raise AssertionError("an unaddressable id must never be read")

    monkeypatch.setattr(handlers.partie_model, "get_partie_strict", _never)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers._read_partie_for_write(bad_id)
    assert "introuvable" in str(excinfo.value)
    assert excinfo.value.reason != "read_unavailable"
    assert handlers._other_partie_exists(bad_id) is False
