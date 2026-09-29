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


def test_the_templatize_chain_names_the_argument_it_hands_over():
    """contracts-9: preview_templatize takes `document_id`, create_template
    the SAME stored .docx as `source_document_id`; carried over verbatim,
    `document_id` is refused by additionalProperties:false. Both texts now
    name the hand-over."""
    preview = tools.TOOLS["preview_templatize"]
    create = tools.TOOLS["create_template"]
    assert "create_template's `source_document_id`" in preview["description"]
    prop = create["input_schema"]["properties"]["source_document_id"]
    assert "preview_templatize" in prop["description"]
    assert "document_id" not in create["input_schema"]["properties"]


def _section_15(text: str) -> str:
    return text[text.index("## 15. Operations"):text.index("## 16. Troubleshooting")]


def test_d19_the_manual_train_is_stated_as_the_only_control():
    """D19 (2026-09-29): tokens granted before the program get NO code gate;
    the manual revoke → deploy → re-consent is the only control, and the
    runbook must SAY so — a reader who thinks the code protects him skips
    the step. Fails on 75efb45."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    section_11 = text[text.index("## 11. Optional — MCP connector"):
                      text.index("## 11b.")]
    assert "is the ONLY control that keeps a token granted before the MCP" in (
        section_11)
    assert "there is no code gate" in section_11 and "D19" in section_11
    runbook = _section_15(text)
    assert ("**This step is the ONLY control that keeps a token granted "
            "before the program from reaching its new write tools**") in runbook
    assert "there is no code gate (the lawyer's decision D19" in runbook


def test_d22_one_release_is_one_ordered_runbook_before_the_lot_sections():
    """D22 (2026-09-29): the branch is pushed ONCE, under one consent train;
    the accounting switch flips later, after a supervised pilot. §15 carries
    ONE ordered runbook for it, ahead of the per-lot sections it references
    — and its order is the one that avoids an outage or a silent grant:
    the designation and the index BEFORE the push, the revocation before
    the push, the push with the accounting switch off, the re-consent and
    80 tools, and only later 86. Fails on 75efb45."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    runbook = _section_15(text)
    head = "**Déploiement unique (D22) — the whole MCP write-expansion program"
    assert head in runbook
    start = runbook.index(head)
    assert start < runbook.index("**Storage identity (lot 0a")
    body = runbook[start:runbook.index("**Storage identity (lot 0a")]
    ordered = [
        "python -m scripts.verify_trust_integrity",
        "python -m scripts.verify_admin_integrity",
        "the lot 4 measurement",
        "firebase deploy --only firestore:indexes",
        "python -m scripts.designer_gabarits_actifs",
        "*Merge `main` into the branch.*",
        "`f69663e`",
        "python -m scripts.revoke_mcp_tokens",
        "*ONE push of the merged branch to `main`*",
        '`MCP_WRITE_ENABLED: "true"` and `MCP_COMPTABILITE_ENABLED: "false"`',
        "*Re-add the connector and READ the new screen before ticking",
        "**80** tools (31 read, 49 write)",
        "*DavX5, on the wire then on the device*",
        "*A phone edit*",
        "*The Outlook mirror*",
        "*Word opens every generated document WITHOUT repair*",
        "*The upload ticket, once*",
        "*No invoice number burned*",
        '`MCP_COMPTABILITE_ENABLED: "true"` in `app.yaml`, and deploy',
        "**86** tools (32 read, 54 write)",
        "The supervised pilot on a TEST administration account",
        "**Emergency switches**",
    ]
    positions = [body.index(fragment) for fragment in ordered]
    for (a, pa), (b, pb) in zip(zip(ordered, positions),
                                zip(ordered[1:], positions[1:])):
        assert pa < pb, f"« {b} » comes before « {a} »"
    # The decisions of the plan's « Ops » row, stated where they bind.
    assert "trust sequences 28 and 42" in body
    assert "storage.googleapis.com" in body
    assert "lot 2B pilot on one real letter" in body and "not a" in body
    # The counts it asks to verify are the registry's.
    visible = set(tools.TOOLS) - tools.ACCOUNTING_TOOLS
    assert len(visible) == 80 and len(visible & tools.WRITE_TOOLS) == 49
    assert len(tools.TOOLS) == 86 and len(tools.WRITE_TOOLS) == 54
