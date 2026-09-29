"""The texts the finitions corrected (truth-3, truth-4, truth-5, truth-6,
truth-7, truth-9) — each once stated something the code no longer does.

Docs are read as files: a deploy recipe, an app.yaml comment, a README or a
Known Gotcha is what an operator or a reader acts on, and each of these
sent them the wrong way. Every assertion here fails on the texts before the
finitions.
"""

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
    import mcp.tools as tools

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
_REPO = _ATHENA.parent


def _flat(path: pathlib.Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_lot_2a_train_describes_the_rename_check_the_screen_shows():
    """truth-3: the consent screen says a rename IS checked against the
    dossiers the template's files came from; the train said « NO dossier »."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    assert "checked against NO dossier" not in text
    assert "checked against the dossiers its files came from" in text


def test_the_write_switch_never_claims_to_turn_the_accounting_read_off(monkeypatch):
    """truth-4: app.yaml said « accounting is off whatever this says » with
    MCP_WRITE_ENABLED false; the accounting READ answers to its own switch
    alone — and the code says so."""
    # Comment lines joined: the « # » markers dropped.
    yaml = " ".join(_flat(_ATHENA / "app.yaml").replace(" # ", " ").split())
    assert "accounting is off whatever this says" not in yaml
    assert "answers to THIS switch alone" in yaml
    assert "A read tool is never switched off here" not in (
        tools.unavailable_reason.__doc__ or "")
    monkeypatch.setattr(tools, "write_enabled", lambda: False)
    monkeypatch.setattr(tools, "comptabilite_enabled", lambda: True)
    assert tools.unavailable_reason("get_admin_ledger") is None
    assert tools.unavailable_reason("record_admin_entry") == tools.WRITE_SWITCH


def test_the_phase_history_records_lot_0b_and_the_fixes_of_lot_4():
    """truth-5: lot 0b's web-visible changes lived only in DEPLOYMENT §15,
    and the lot 4 entry still said three writes read fail-open."""
    text = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "### Lot 0b — " in text
    assert "lisent encore par le `get_dossier` qui échoue ouvert" not in text
    assert "*Correctifs du lot 4 (2026-09-29)*" in text


def test_the_fee_payment_warning_names_the_accounting_tools():
    """truth-6: the issuance warning said a provision applies only by a fee
    payment « inscrit dans l'application »."""
    src = (_ATHENA / "mcp" / "handlers.py").read_text(encoding="utf-8")
    assert "d'honoraires » inscrit dans l'application.\"" not in src
    assert "d'honoraires » inscrit au fidéicommis — dans l'application, ou" in src


def test_the_env_example_and_the_readme_describe_the_live_accounting_scope():
    """truth-7: the accounting scope carries six tools since lot 5b — never
    « dormant » — and there are more than 22 write tools."""
    env = _flat(_REPO / ".env.example")
    readme = _flat(_REPO / "README.md")
    assert "the 22 write tools" not in env
    assert "dormant until a tool carries" not in env
    assert "dormant until a tool carries it" not in readme
    assert "six accounting tools" in env
    assert "never a payment" in readme and "templates" in readme


def test_the_phase_gotcha_names_the_function_that_exists():
    """truth-9: the Phase-O gotcha named the renamed
    `_auto_create_tasks_for_steps`; only « ex- » mentions may remain."""
    text = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    import re
    bare = [m.start() for m in re.finditer(r"`_auto_create_tasks_for_steps`", text)
            if text[max(0, m.start() - 4):m.start()] != "(ex-"]
    assert bare == []
    from models import protocol
    assert hasattr(protocol, "create_linked_tasks")
    assert not hasattr(protocol, "_auto_create_tasks_for_steps")
