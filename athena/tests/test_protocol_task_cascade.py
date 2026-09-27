"""La cascade tâche → étape → protocole, épinglée sur le vrai magasin (lot 1a).

Pourquoi ce fichier existe AVANT les changements qu'il protège. La cascade
côté tâche (``models/task._sync_protocol_step``) importe ce qu'elle appelle de
``models.protocol`` DANS la fonction, à l'intérieur d'un ``try`` qui avale
toute exception — un nom manquant (une fonction pas encore livrée, un
renommage) y lève ``ImportError``, l'``except`` l'attrape, et la cascade
ENTIÈRE meurt en silence : plus aucune tâche terminée ne complète son étape,
sur le web, au téléphone ou par le connecteur. Rien d'autre ne le dirait.

Deux gardes, qui doivent rester vertes à CHAQUE commit du lot :

1. le comportement, sur le faux Firestore partagé (le client et ses
   transactions sont les vrais) : terminer une tâche liée complète son étape
   et, si c'était la dernière, ferme le protocole ; rouvrir la tâche dans un
   protocole actif rouvre l'étape ;
2. la structure : chaque import ``from models.… import …`` écrit DANS une
   fonction d'un modèle nomme un attribut qui existe — balayé sur le source,
   pour qu'une branche qu'aucun test ne pilote soit couverte aussi.
"""

import ast
import importlib
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
    from models import protocol as protocol_model  # noqa: F401
    from models import task as task_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)
LATER = datetime(2099, 12, 1, tzinfo=UTC)
P = "p1"


def _fake_modules() -> list:
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
    fake.seed("dav_sync/dossier:d1", {"ctag": "c0", "sync_token": "c0",
                                      "updated_at": WHEN})
    return fake


def _step(fake, sid: str, *, status: str = "à_venir", task=None,
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


def _task(fake, tid: str, *, status: str = "à_faire") -> None:
    fake.seed(f"tasks/{tid}", {
        "id": tid, "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T c. L", "title": f"Tâche {tid}", "description": "",
        "priority": "normale", "status": status, "due_date": None,
        "completed_date": WHEN if status == "terminée" else None,
        "category": "suivi", "phase": "", "sous_phase": "",
        "vtodo_uid": f"u-{tid}", "dav_href": "", "related_note_id": None,
        "etag": f"e-{tid}", "created_at": WHEN, "updated_at": WHEN,
    })


def _step_status(fake, sid: str) -> str:
    return fake.peek(f"protocols/{P}/steps/{sid}")["status"]


# ══════════════════════════════════════════════════════════════════════
# 1. Le comportement
# ══════════════════════════════════════════════════════════════════════


def test_finishing_a_linked_task_completes_its_step(fake):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)          # keeps the protocol open
    _task(fake, "t1")
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    assert _step_status(fake, "s1") == "complété"
    assert fake.peek(f"protocols/{P}/steps/s1")["completed_date"] is not None
    assert fake.peek(f"protocols/{P}")["status"] == "actif"


def test_finishing_the_last_linked_task_closes_the_protocol(fake):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", status="complété", order=2)
    _task(fake, "t1")
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    assert _step_status(fake, "s1") == "complété"
    assert fake.peek(f"protocols/{P}")["status"] == "complété"


@pytest.mark.parametrize("reopened", ["à_faire", "en_cours"])
def test_reopening_a_linked_task_in_an_active_protocol_reopens_its_step(
    fake, reopened
):
    _step(fake, "s1", status="complété", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1", status="terminée")
    _doc, errors = task_model.update_task("t1", {"status": reopened})
    assert errors == []
    assert _step_status(fake, "s1") == "à_venir"
    assert fake.peek(f"protocols/{P}/steps/s1")["completed_date"] is None


def test_cancelling_a_linked_task_leaves_its_step_alone(fake):
    _step(fake, "s1", task="t1")
    _step(fake, "s2", order=2)
    _task(fake, "t1")
    before = fake.peek(f"protocols/{P}/steps/s1")
    _doc, errors = task_model.update_task("t1", {"status": "annulée"})
    assert errors == []
    assert fake.peek(f"protocols/{P}/steps/s1") == before


def test_a_task_linked_to_no_step_changes_no_protocol(fake):
    _step(fake, "s1", task="t-other")
    _task(fake, "t1")
    before = fake.peek(f"protocols/{P}")
    _doc, errors = task_model.update_task("t1", {"status": "terminée"})
    assert errors == []
    assert fake.peek(f"protocols/{P}") == before
    assert _step_status(fake, "s1") == "à_venir"


# ══════════════════════════════════════════════════════════════════════
# 2. La structure : aucun import en fonction ne nomme un absent
# ══════════════════════════════════════════════════════════════════════


def _imports_in(source: str) -> list[tuple[int, str, str]]:
    """``(line, module, name)`` for every ``from models[.x] import name``
    written inside a function of *source*."""
    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.ImportFrom) and node.level == 0
                    and node.module
                    and (node.module == "models"
                         or node.module.startswith("models."))):
                found.extend((node.lineno, node.module, a.name)
                             for a in node.names)
    return found


def _in_function_model_imports() -> list[tuple[str, int, str, str]]:
    """``(file, line, module, name)`` over every ``models/*.py`` file."""
    return [(path.name, line, module, name)
            for path in sorted((_ATHENA / "models").glob("*.py"))
            for line, module, name in _imports_in(
                path.read_text(encoding="utf-8"))]


def _missing(imports) -> list[str]:
    out = []
    for filename, line, module, name in imports:
        with mock.patch("google.cloud.firestore.Client"):
            target = importlib.import_module(module)
        if not hasattr(target, name):
            out.append(f"models/{filename}:{line} {module}.{name}")
    return out


def test_the_import_sweep_is_not_vacuous():
    names = {(m, n) for _f, _l, m, n in _in_function_model_imports()}
    # The cascade's own imports, both directions.
    assert any(m == "models.protocol" for m, _ in names), names
    assert any(m == "models.task" for m, _ in names), names


def test_the_sweep_flags_a_planted_missing_name():
    """The guard proves its own mechanism: a planted import of a name
    ``models.protocol`` does not define is caught — at module level it
    would be ignored (only function bodies defer the failure)."""
    planted = (
        "from models.protocol import COLLECTION\n"
        "def f():\n"
        "    from models.protocol import COLLECTION, not_there_yet\n"
    )
    imports = [("planted.py", line, module, name)
               for line, module, name in _imports_in(planted)]
    assert _missing(imports) == [
        "models/planted.py:3 models.protocol.not_there_yet"]


def test_every_in_function_model_import_names_an_existing_attribute():
    missing = _missing(_in_function_model_imports())
    assert missing == [], (
        "an import inside a model function names nothing — it raises at "
        "call time, and the cascades swallow it:\n  " + "\n  ".join(missing)
    )
