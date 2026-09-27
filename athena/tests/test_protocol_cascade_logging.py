"""The task ↔ protocol-step cascade never fails SILENTLY (plan rule 14).

Both halves swallow their exceptions on purpose — the write they follow has
already committed, and a failed sync must not turn it into an error the
caller would retry. But until lot 0a one half logged through a raw
``logger.warning`` (outside the typed helpers, so outside the event
vocabulary) and the other did not log at all: ``models/task._sync_protocol_step``
ended in ``except Exception: pass``, while ``complete_task`` told its caller
that « la synchronisation du modèle avale ses erreurs » and pointed nowhere.
A task left out of step with its protocol step is a data inconsistency; it
is now an ``unexpected`` ERROR carrying ids only.
"""

import ast
import logging
import os
import pathlib
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import protocol as protocol_model
    from models import task as task_model

MODELS = pathlib.Path(__file__).resolve().parent.parent / "models"
_SECRET = "Tremblay c. Lavoie — texte privilégié"

# The cascade functions, by module. Any NEW cascade function joins here.
_CASCADE = {
    "task.py": ("_sync_protocol_step",),
    "protocol.py": ("_sync_task_status", "_check_protocol_completion",
                    "set_step_status", "_reopen_blocker", "create_protocol",
                    "create_linked_tasks", "_link_task_to_step"),
}


def _unexpected(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pallas.unexpected"]


def _fields(record) -> dict:
    return dict(getattr(record, "json_fields", {}) or {})


def test_a_failed_step_sync_is_logged_with_ids_only(monkeypatch, caplog):
    broken = mock.MagicMock()
    broken.collection.side_effect = RuntimeError(_SECRET)
    monkeypatch.setattr(task_model, "db", broken)
    with caplog.at_level(logging.INFO):
        task_model._sync_protocol_step("task-1", "terminée")   # never raises
    (record,) = _unexpected(caplog)
    assert record.getMessage() == "protocol cascade: step sync failed"
    assert record.levelno == logging.ERROR
    assert _fields(record) == {"event": "unexpected", "task_id": "task-1",
                               "task_status": "terminée"}
    # The guard set is released even on failure — or the task could never
    # sync again in this process.
    assert "task-1" not in task_model._SYNCING
    assert _SECRET not in record.getMessage()


def test_a_failed_task_sync_is_logged_with_ids_only(monkeypatch, caplog):
    # Since lot 0b the cascade READS the task before writing it (a cancelled
    # task is left alone), so the read is stubbed with a real, open task: on
    # the mocked client it would otherwise hand back a MagicMock and reach
    # the write only by accident.
    monkeypatch.setattr(task_model, "get_task", lambda tid: {
        "id": tid, "status": "à_faire", "etag": "e0", "dossier_id": "d1"})

    def _boom(task_id, data, **kw):
        raise RuntimeError(_SECRET)
    monkeypatch.setattr(task_model, "update_task", _boom)
    with caplog.at_level(logging.INFO):
        outcome = protocol_model._sync_task_status("task-2", "complété")
    assert outcome == "failed"
    (record,) = _unexpected(caplog)
    assert record.getMessage() == "protocol cascade: task status sync failed"
    assert _fields(record) == {"event": "unexpected", "task_id": "task-2",
                               "step_status": "complété"}
    assert "task-2" not in protocol_model._SYNCING
    assert not [r for r in caplog.records
                if r.name == protocol_model.logger.name
                and r.levelno == logging.WARNING]


def test_a_failed_completion_check_is_logged_with_ids_only(monkeypatch, caplog):
    """Rewritten in lot 1a: the completion check now decides and writes in
    ONE transaction on the real client, so the store failure is modelled by
    the shared fake Firestore — a commit hook that raises — instead of a
    MagicMock whose ``update`` raised (the check no longer calls it)."""
    from tests._fake_firestore import install

    fake = install(monkeypatch, protocol_model)
    fake.seed("protocols/proto-1", {"id": "proto-1", "status": "actif"})
    fake.seed("protocols/proto-1/steps/s1", {"id": "s1", "status": "complété"})

    def _down(_info):
        raise RuntimeError(_SECRET)

    fake.add_commit_hook(_down)
    with caplog.at_level(logging.INFO):
        assert protocol_model._check_protocol_completion("proto-1") is False
    (record,) = _unexpected(caplog)
    assert record.getMessage() == "protocol cascade: completion check failed"
    assert _fields(record) == {"event": "unexpected", "protocol_id": "proto-1"}
    assert fake.peek("protocols/proto-1")["status"] == "actif"


def _function(module: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((MODELS / module).read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


# What counts as « logged » in an except handler: the unexpected-error
# helper, or — for a REFUSAL raised on purpose inside a transaction
# (set_step_status's _StepRefusal, lot 0b) — the family's typed event
# helper with a machine reason. Never a raw logger call (swept above).
_LOGGING_HELPERS = {"log_unexpected", "log_protocol_event"}


def test_no_cascade_function_logs_raw_or_swallows_silently():
    """Swept on the source, so a re-introduced `pass` or `logger.warning`
    fails here even where no test drives that branch."""
    offenders = []
    for module, names in _CASCADE.items():
        for name in names:
            fn = _function(module, name)
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "logger"):
                    offenders.append(f"{module}:{name} raw logger.{node.attr}")
                if isinstance(node, ast.ExceptHandler):
                    body = [b for b in node.body
                            if not isinstance(b, ast.Pass)]
                    logs = [n for b in node.body for n in ast.walk(b)
                            if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Name)
                            and n.func.id in _LOGGING_HELPERS]
                    if not body or not logs:
                        offenders.append(
                            f"{module}:{name} swallows an exception unlogged")
    assert offenders == []
