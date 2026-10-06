"""The texts the finitions corrected (truth-3, truth-5, truth-6, truth-7,
truth-9) — each once stated something the code no longer does. (truth-4's
test — the app.yaml comment of the write switch — left on 2026-10-05 with
the MCP kill switches it read.)

Docs are read as files: a deploy recipe, an app.yaml comment, a README or a
Known Gotcha is what an operator or a reader acts on, and each of these
sent them the wrong way. Every assertion here fails on the texts before the
finitions.
"""

import os
import pathlib
import re
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


def test_the_env_example_and_the_readme_never_call_the_accounting_tools_dormant():
    """truth-7: the accounting tools are live since lot 5b — never
    « dormant » — and there are more than 22 write tools. (Its pin of « six
    accounting tools » — the separate scope's count in .env.example — left
    with that scope and its switch on 2026-10-05.)"""
    env = _flat(_REPO / ".env.example")
    readme = _flat(_REPO / "README.md")
    assert "the 22 write tools" not in env
    assert "dormant until a tool carries" not in env
    assert "dormant until a tool carries it" not in readme
    assert "never a payment" in readme and "templates" in readme


def test_the_phase_gotcha_names_the_function_that_exists():
    """truth-9: the Phase-O gotcha named the renamed
    `_auto_create_tasks_for_steps`; only « ex- » mentions may remain."""
    text = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
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
    """D22 (2026-09-29): the branch is pushed ONCE, under one consent train.
    §15 carries ONE ordered runbook for it, ahead of the per-lot sections it
    references — and its order is the one that avoids an outage or a silent
    grant: the designation and the index BEFORE the push, the revocation
    before the push, the push, the re-consent and 80 tools. Fails on
    75efb45.

    REWRITTEN 2026-10-05: the pins of the switch values at the push and of
    the later accounting train (its arming, its 86 tools, its pilot, the
    emergency switches) left with the MCP kill switches and the separate
    accounting scope — the lawyer's decision."""
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
        "*Re-add the connector and READ the new screen before ticking",
        "**80** tools (31 read, 49 write)",
        "*DavX5, on the wire then on the device*",
        "*A phone edit*",
        "*The Outlook mirror*",
        "*Word opens every generated document WITHOUT repair*",
        "*The upload ticket, once*",
        "*No invoice number burned*",
    ]
    positions = [body.index(fragment) for fragment in ordered]
    for (a, pa), (b, pb) in zip(zip(ordered, positions),
                                zip(ordered[1:], positions[1:])):
        assert pa < pb, f"« {b} » comes before « {a} »"
    # The decisions of the plan's « Ops » row, stated where they bind.
    assert "trust sequences 28 and 42" in body
    assert re.search(r"storage\.googleapis\.com", body)
    assert "lot 2B pilot on one real letter" in body and "not a" in body
    # The counts it asks to verify are the registry's AS THAT RELEASE
    # SHIPPED IT. REWRITTEN deliberately (2026-09-30): the bulk creators
    # came after the D22 push, under their own train (« Bulk creators »,
    # pinned below) — the runbook of a past release keeps its counts, and
    # the registry minus what came after must still match them. Rewritten
    # again 2026-10-05: what a write token did not see THEN — the six tools
    # of the separate accounting scope — is named here, the registry no
    # longer carrying that scope.
    after_d22 = _AFTER_D22
    assert after_d22 <= tools.WRITE_TOOLS
    visible = set(tools.TOOLS) - _ACCOUNTING_SCOPE_UNTIL_2026_10_05 - after_d22
    assert len(visible) == 80 and len(visible & tools.WRITE_TOOLS) == 49
    assert len(set(tools.TOOLS) - after_d22) == 86
    assert len(tools.WRITE_TOOLS - after_d22) == 54


# The write tools shipped AFTER the D22 release, each under its own train.
_AFTER_D22 = frozenset({"create_time_entries_bulk", "create_expenses_bulk"})

# The six tools the separate `athena:comptabilite` scope carried from lot 5b
# until 2026-10-05, when the lawyer removed it: the ACCOUNTING family's five
# writes and its read. A past release's runbook counts what a token saw THEN
# — a write token without that scope saw none of the six.
_ACCOUNTING_SCOPE_UNTIL_2026_10_05 = (
    tools.ACCOUNTING_WRITE_TOOLS | {"get_admin_ledger"})


def test_the_bulk_creators_release_runs_the_d19_train_with_the_registry_counts():
    """2026-09-30: two new write tools reach the token in force the moment
    the deploy lands (D19 — no code gate), so their release carries its own
    ordered train in §15 — revoke BEFORE the push, re-consent AFTER it — and
    the counts it asks to verify are the registry's, derived here."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    runbook = _section_15(text)
    head = ("**Bulk creators — `create_time_entries_bulk` / "
            "`create_expenses_bulk`")
    assert head in runbook
    body = runbook[runbook.index(head):runbook.index("**Cold starts:**")]
    # What a write token saw AT THAT RELEASE: the separate accounting scope
    # still held six tools back (removed 2026-10-05 — a write token sees
    # them all since; the runbook of a past release keeps its counts).
    visible = set(tools.TOOLS) - _ACCOUNTING_SCOPE_UNTIL_2026_10_05
    reads = len(visible - tools.WRITE_TOOLS)
    writes = len(visible & tools.WRITE_TOOLS)
    all_reads = len(set(tools.TOOLS) - tools.WRITE_TOOLS)
    ordered = [
        "python -m pytest tests/ -q -p no:cacheprovider",
        "*Revoke and disconnect — BEFORE the push*",
        "python -m scripts.revoke_mcp_tokens",
        "*The push*",
        "*Re-add the connector and READ the CREATE paragraph",
        f"**{len(visible)}** tools ({reads} read, {writes} write)",
        f"**{len(tools.TOOLS)}** ({all_reads} read, "
        f"{len(tools.WRITE_TOOLS)} write)",
        "*One smoke test, on a TEST dossier only*",
        "`entries[1]`",
        "skill `pallas-athena`",
    ]
    positions = [body.index(fragment) for fragment in ordered]
    assert positions == sorted(positions), list(zip(ordered, positions))
    assert f"up to {tools.ENTRY_BULK_MAX} rows a call" in body
    assert "there is no code gate (D19" in body
    assert _AFTER_D22 <= tools.WRITE_TOOLS
    # §11 points at it from the D19 paragraph.
    section_11 = text[text.index("## 11. Optional — MCP connector"):
                      text.index("## 11b.")]
    assert "§15 « Bulk creators », step 2" in section_11


def test_review_e3_the_accounting_index_line_says_what_its_tools_say():
    """Review of E3: the ACCOUNTING index line said « record ONLY what the
    bank shows » where both writers' descriptions say « a movement that
    HAPPENED at the bank, dated the day it happened » — and a cheque just
    written is recorded en_circulation, shown on no statement until it
    clears. DEPLOYMENT.md's lot 5 pilot quotes the rule the tools state.
    Fails on 3191774."""
    from mcp import disclosure, endpoint

    accounting = next(f for f in disclosure.FAMILIES if f.key == "accounting")
    assert "what the bank shows" not in accounting.instructions_en
    assert "movements that HAPPENED at the bank" in accounting.instructions_en
    # The ONE text every token reads since 2026-10-05 (the accounting
    # variant left with the separate scope).
    assert "movements that HAPPENED at the bank" in endpoint.INSTRUCTIONS
    for name in ("record_trust_entry", "record_admin_entry"):
        assert "HAPPENED" in tools.TOOLS[name]["description"], name
    deployment = _flat(_REPO / "DEPLOYMENT.md")
    assert "record only movements that happened at the bank" in deployment


def test_review_e3_the_d22_runbook_writes_only_what_it_says():
    """Review of E3: the post-push steps promised « checks that write
    nothing, or only on test data », then deleted the test data at step 9
    while steps 10-13 still needed an event, a task and a dossier; asked for
    ONE test dossier where its relocation check needs two; sent step 12 to a
    note d'honoraires that needs a REAL invoice (a test one burns a number)
    without saying it files a draft into that dossier; and folded every
    re-consent into step 7 where lot 5's is step 17. Fails on 3191774."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    runbook = _section_15(text)
    head = "**Déploiement unique (D22) — the whole MCP write-expansion program"
    body = runbook[runbook.index(head):runbook.index("**Storage identity (lot 0a")]
    assert "the re-consents into step 7." not in body
    # (Its positive half — lot 5's re-consent as step 17 — pinned the later
    # accounting train, obsolete since the switches left on 2026-10-05.)
    assert "on TWO test dossiers" in body
    step_9 = body[body.index("*DavX5, on the wire then on the device*"):
                  body.index("*A phone edit*")]
    assert "Delete the test data" not in step_9
    step_10 = body[body.index("*A phone edit*"):body.index("*The Outlook mirror*")]
    assert "the test dossiers'" in step_10
    step_12 = body[body.index("*Word opens every generated document"):
                   body.index("*The upload ticket, once*")]
    assert "a REAL invoice" in step_12 and "burns a number" in step_12
    tail = body[body.index("*No invoice number burned*"):
                body.index("*Tell the lawyer*")]
    assert "delete the test data of steps 9 to 13" in tail


def test_final_check_the_finitions_and_lot_5_texts_follow_the_d22_runbook():
    """Final check of the branch (2026-09-29): three texts still described
    the pre-D22 trains, or a rule the tools no longer state.

    * The « Finitions » entry said its connector changes « ride the stack's
      own revocation (§15 « Lot 4 », then « Lot 5 ») » — two revocations
      the D22 runbook collapses into its step 5.
    * It tied the D25 skill note and the « Analyse documentaire » re-export
      to « Lot 5 » step 9, which the runbook defers to step 17 — the
      accounting train, weeks later — while D25 ships under the ordinary
      write grant on push day; and the runbook's step 15 never named that
      re-export at all.
    * « Lot 5 » step 9 still taught the skill « record only what the BANK
      shows » — the phrasing the review of E3 removed from INSTRUCTIONS:
      both writers record a movement that HAPPENED at the bank, and a
      cheque just written is on no statement until it clears.

    Fails on 9a80416."""
    text = _flat(_REPO / "DEPLOYMENT.md")
    runbook = _section_15(text)
    finitions = runbook[runbook.index("**Finitions — the adversarial review"):
                        runbook.index("**Cold starts:**")]
    assert "ride the stack's own revocation" not in finitions
    assert ("ride the ONE revocation of §15 « Déploiement unique (D22) » "
            "(step 5") in finitions
    assert "are updated with §15 « Lot 5 » step 9, add" not in finitions
    assert "step 15 — D25 rides the ordinary write grant" in finitions
    assert "re-export it at that same step 15" in finitions

    head = "**Déploiement unique (D22) — the whole MCP write-expansion program"
    body = runbook[runbook.index(head):runbook.index("**Storage identity (lot 0a")]
    # Step 15 ends where the later accounting train began — a heading the
    # removal of that train (2026-10-05) may take with it: then the runbook
    # ends there.
    end = body.find("**Later — the accounting switch")
    step_15 = body[body.index("*Tell the lawyer*"):end if end != -1 else len(body)]
    assert "python -m scripts.exporter_competence_analyse" in step_15
    assert "« Analyse documentaire »" in step_15

    lot_5 = runbook[runbook.index("**Lot 5 — accounting through the connector"):
                    runbook.index("**Finitions — the adversarial review")]
    assert "record only what the BANK shows" not in lot_5
    assert "record only a movement that HAPPENED at the bank" in lot_5
