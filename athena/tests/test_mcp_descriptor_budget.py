"""The connector's descriptor byte budget (plan rule 12, decision D13).

Every tool descriptor the connector advertises is paid for on EVERY turn of
every conversation that has the connector enabled: the client model reads
`name`, `title`, `description`, `inputSchema` and `annotations` before it
can call anything. The plan widens the connector from 49 tools toward ~86;
without a ceiling the cost would grow unnoticed one generous description at
a time, and it is the model's context — not a server resource — that pays.

The measure is the MODEL-VISIBLE bytes of each descriptor: its JSON without
`outputSchema` (a contract for the client's validator, not text the model is
steered by), `ensure_ascii=False`, UTF-8 — so « é » counts as the two bytes it
costs on the wire, not as a six-byte `\\u00e9` escape. Default separators,
the shape the 2026-09-25 baseline was measured with.

Baseline measured 2026-09-25 on HEAD 861c6b8: 49 tools, 135 236 bytes in
total (≈ 34 k tokens), largest `create_partie` at 7 603 bytes; 259 408 bytes
with outputSchema included.

Measured again 2026-09-29 with lot 5b's six accounting tools: 86 tools,
250 651 bytes in total (the six weigh 18 505, the largest `record_admin_entry`
at 4 640). After lot 5's text step (the same day — two accounting
descriptions made true: the reversal is the only correction of a TRUST entry,
the revision trail keeps its latest 25): 251 024 bytes, the six 18 878, the
largest descriptor overall still `update_partie` at 7 858. After the
finitions (2026-09-29), the family prose of INSTRUCTIONS moved into fourteen
tool descriptions included (contracts-1, part 2): 257 183 bytes, the largest
still `update_partie` at 7 904 — 257 227 after the review of E3 (the
detected-conflict sentence of `record_kyc_status` made true).

Measured again 2026-09-30, HEAD d8de4e5 (257 794 bytes by then), and after
the context-cost lot and its review: 248 645 bytes, the largest
`update_partie` at 7 721. The saving is the shared property texts repeated
on every write tool — `idempotency_key` (54 copies, 15 684 → 9 512 bytes of
description), `expected_etag` (23 copies, 6 014 → 4 611) and `phase` /
`sous_phase` (12 and 13 copies, 6 071 → 4 617, the per-tool omission
sentence included; their enums are the input contract and stay whole) —
made concise, plus `get_dossier`'s description brought under the
2 048-character cut below (2 154 → 2 038). For the write grant (80 tools,
description + inputSchema only): 218 610 → 209 872 bytes.

Tool COUNTS are pinned in test_mcp_tools.py, once; this file pins bytes only.
"""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

import pytest

with mock.patch("google.cloud.firestore.Client"):
    import mcp.tools as tools

from tests import _dummy_accounting  # noqa: E402


@pytest.fixture(autouse=True)
def _whole_surface(monkeypatch):
    """Measure the surface with EVERY switch on, whatever this process says.

    `list_tool_descriptors(None)` drops no scope but still applies the kill
    switches, and MCP_COMPTABILITE_ENABLED defaults to FALSE (money is
    fail-closed). Without this, every accounting tool (plan lot 5) would be
    left out of the ceiling — the budget pays for what a fully-granted
    token is shown, and that includes them.
    """
    monkeypatch.setattr(tools, "write_enabled", lambda: True)
    monkeypatch.setattr(tools, "comptabilite_enabled", lambda: True)


# The plan's target for the whole widened surface (~86 tools), 2026-09-25.
TOTAL_CAP = 280_000
# max(8 000, today's largest rounded up to the next 500) — 7 603 → 8 000.
# A descriptor past it is a tool trying to be two tools: split it, or cut
# the prose that restates what the schema already says.
PER_TOOL_CAP = 8_000


def _model_visible_bytes() -> dict[str, int]:
    sizes = {}
    for descriptor in tools.list_tool_descriptors(None):
        visible = {k: v for k, v in descriptor.items() if k != "outputSchema"}
        sizes[descriptor["name"]] = len(
            json.dumps(visible, ensure_ascii=False).encode("utf-8")
        )
    return sizes


@pytest.mark.parametrize("with_accounting_tool", [False, True])
def test_the_measure_covers_every_tool_and_excludes_only_the_output_schema(
    monkeypatch, with_accounting_tool
):
    """The budget must see the whole advertised surface — a tool hidden by a
    kill switch in this process would escape the ceiling unnoticed. The
    dummy run proves an accounting tool is measured although its switch
    defaults to off — the six real ones (lot 5b) included."""
    if with_accounting_tool:
        name = _dummy_accounting.register(monkeypatch)
        assert name in _model_visible_bytes()
    descriptors = tools.list_tool_descriptors(None)
    assert {d["name"] for d in descriptors} == set(tools.TOOLS)
    for d in descriptors:
        assert set(d) == {"name", "title", "description", "inputSchema", "outputSchema", "annotations"}


def test_no_single_tool_exceeds_the_per_tool_budget():
    over = {n: s for n, s in _model_visible_bytes().items() if s > PER_TOOL_CAP}
    assert not over, (
        "model-visible descriptor over the per-tool budget of "
        f"{PER_TOOL_CAP} bytes: "
        + ", ".join(f"{n} = {s} bytes" for n, s in sorted(over.items(), key=lambda x: -x[1]))
    )


# Claude Code cuts each tool DESCRIPTION at 2 048 characters, as it cuts the
# server instructions (CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH). A rule past
# the cut is a rule that client never reads. Measured 2026-09-30: one tool
# over it, get_dossier at 2 154 — tightened to 2 038; the next largest were
# list_invoices (1 775) and import_invoice (1 743).
DESCRIPTION_CUT = 2_048


def test_no_tool_description_passes_the_client_s_cut():
    over = {
        d["name"]: len(d["description"])
        for d in tools.list_tool_descriptors(None)
        if len(d["description"]) > DESCRIPTION_CUT
    }
    assert not over, (
        f"tool descriptions past the {DESCRIPTION_CUT}-character cut a real "
        "client applies — tighten the wording, or move reference detail "
        "into the input-property description it concerns: "
        + ", ".join(f"{n} = {c}" for n, c in sorted(over.items())))


def test_the_whole_surface_stays_within_the_total_budget():
    sizes = _model_visible_bytes()
    total = sum(sizes.values())
    largest = sorted(sizes.items(), key=lambda x: -x[1])[:5]
    assert total <= TOTAL_CAP, (
        f"all {len(sizes)} descriptors weigh {total} model-visible bytes, over "
        f"the {TOTAL_CAP}-byte budget; the largest: "
        + ", ".join(f"{n} = {s}" for n, s in largest)
    )


def test_bytes_are_counted_as_utf8_not_as_escapes():
    """« é » is two bytes on the wire; an ASCII-escaped measure would count
    six and over-report every French description."""
    assert len(json.dumps("é", ensure_ascii=False).encode("utf-8")) == 4  # "é" + quotes
    assert len(json.dumps("é").encode("utf-8")) == 8


# ── INSTRUCTIONS (finitions, contracts-1) ────────────────────────────────
#
# The descriptors are not the only text paid on every turn: INSTRUCTIONS
# rides every `initialize`, and nothing measured it — it grew from 4 461
# characters (21012c0) to 21 579 / 24 636 (the comptabilité variant) with no
# test noticing. And a real client CUTS the field: this connector's
# production text reached a Claude Code session truncated after its
# 2 048th character, while the protocol rules came last, after ~16 KB of
# family prose.
#
# Part 1 (0459d60) moved a protocol core to the front and capped the sizes
# at 24 500 / 27 500 bytes. Part 2 (this file's current form) restructured
# the text: a SAFETY CORE first — the « never » list, the one outbound
# effect, confirm-before-writing, idempotency and etag, re-read before a
# retry —, complete within 2 000 characters; then ONE index line per family
# naming its tools; the detailed prose moved into the tool descriptions.
# Measured 2026-09-29: core 1 734 characters; 6 504 / 7 892 UTF-8 bytes
# (base / accounting variant) — 6 504 / 7 904 after the review of E3, the
# ACCOUNTING line restating its tools' rule.
# REWRITTEN deliberately: the two caps (24 500 / 27 500) became ONE cap for
# every token type, and « the core comes before READ-CONTENT » became « the
# core IS the first characters, and is complete ».
#
# Part 3 (2026-09-30, the context-cost lot): the CONVENTIONS — money
# (`*_cents` + `*_display`), date-only `YYYY-MM-DD` vs Montréal timestamps,
# ids verbatim, the provenance the SERVER stamps — still closed the text,
# ~4 KB after a client's 2 048-character cut, so every truncating client
# lost them. The protocol paragraph was compressed (every rule kept) and
# the text now opens on a HEAD: the SAFETY CORE then the CONVENTIONS, whole
# within disclosure.HEAD_LIMIT (2 048 — Claude Code's own cut, counted in
# characters as the client counts — this connector's text reached a
# session cut exactly after its 2 048th character). REWRITTEN
# deliberately: CORE_LIMIT (2 000, the core alone) became HEAD_LIMIT (core +
# conventions), and « the index follows the core » became « the CONVENTIONS
# follow the core, then the index ».
# The review of that lot (same day) made the head TRUE rather than shorter:
# « never type » the provenance became « never add a stamp, keep those a
# re-sent text holds » (update_task stores a description exactly as sent,
# so a model told « never type it » strips the « Créée par Claude » line for
# good); the row fields regained « where declared » and « `created_via` on
# creations »; « ONE effect reaches » became « Only ONE effect reaches » (an
# exclusivity, pinned below) and the lead regained « manager »; the
# INTERRUPTED gloss moved into that refusal itself (tests/test_mcp_write_
# support.py pins it there), and « never blindly » left as a restatement of
# « redo the write on the current record ». Measured: core 1 470
# characters, head 2 045 (2 077 UTF-8 bytes — a client cutting at 2 048
# BYTES would lose its last 28 characters, « …aude] » (compliance
# checks). »; Claude Code counts characters); the counts header « TOOLS: … »
# now starts at character 2 046, past the cut; 6 115 / 7 567 UTF-8 bytes
# (base / accounting), from 6 504 / 7 956 before the lot.
INSTRUCTIONS_CAP = 8_000
_CORE_MARKERS = (
    "SAFETY CORE",
    "NEVER, whatever the tool:",
    "DELETE anything",
    "record a payment outside the accounting registers",
    "SEND an invoice to anyone",
    "CONFIRM an identity or conflict check",
    "`decide_rendez_vous`",
    "cancels the client's Outlook meeting",
    # An EXCLUSIVITY: nothing else leaves the practice (review of the
    # context-cost lot — « ONE effect reaches » alone reads « one such »).
    "Only ONE effect reaches anyone outside the practice",
    "confirm with the user unless a standing instruction",
    "idempotency_key",
    "the SAME key, never a new one",
    # The third state of a claimed key (review of the finitions): without
    # it, « retry only with the SAME key » after an uncertain outcome was a
    # dead end once the key's refusal turned « interrompu ».
    "refused as INTERRUPTED",
    "a NEW key only if nothing was written",
    "« ENREGISTRÉE — NE PAS RÉESSAYER »",
    "`expected_etag`",
    "stale_etag",
    "re-read, then redo the write on the current record",
    "Each tool's description carries its own rules",
)
# What the CONVENTIONS must carry — each a fact a caller needs to read a
# result or write an argument, whatever tool it opens.
_CONVENTION_MARKERS = (
    "CONVENTIONS:",
    "French",
    "Markdown", "raw HTML refused",
    "`*_cents`", "`*_display`", "(CAD)",
    "ISO 8601 America/Montreal",
    "date-only fields `YYYY-MM-DD`",
    "UUIDv4", "verbatim",
    # BOTH halves (review of the context-cost lot): the server stamps, and
    # a text sent back KEEPS the stamps it held — « never type it » alone
    # had a model strip them from a body it re-sent in full.
    "never add a stamp, keep those a re-sent text holds",
    "`created_via` on creations", "`updated_via`", "`mcp_updated_at`",
    "where declared",
    "« … par Claude le … »",
    "« par Claude (connecteur) »", "`genere_depuis`",
    "`*_source` \"mcp\"", "« [AAAA-MM-JJ — inscrit par Claude] »",
)


def _instructions():
    with mock.patch("google.cloud.firestore.Client"):
        from mcp import endpoint
    return endpoint.INSTRUCTIONS, endpoint.INSTRUCTIONS_COMPTABILITE


def _core():
    from mcp import disclosure

    return disclosure.safety_core_en()


def _head_limit() -> int:
    from mcp import disclosure

    return disclosure.HEAD_LIMIT


def test_the_head_limit_is_the_client_s_cut():
    """Claude Code cuts server instructions and tool descriptions at 2 048
    characters (CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH). A larger limit
    here would let the head outgrow the cut it exists to survive."""
    assert _head_limit() == 2_048


def test_the_instructions_stay_within_their_budget():
    """Every token type — the read-only, write and accounting tokens read
    one of these two texts (endpoint.instructions_for) — within ONE cap."""
    base, accounting = _instructions()
    for label, text in (("INSTRUCTIONS", base),
                        ("INSTRUCTIONS_COMPTABILITE", accounting)):
        size = len(text.encode("utf-8"))
        assert size <= INSTRUCTIONS_CAP, (
            f"{label} weighs {size} bytes, over its {INSTRUCTIONS_CAP}-byte "
            "budget: it rides every initialize — put the rule in the tool's "
            "description, and keep the family's index line to its tools")


@pytest.mark.parametrize("variant", [0, 1], ids=["base", "comptabilite"])
def test_the_safety_core_is_the_first_characters_and_complete(variant):
    """The core opens the text — its first N characters ARE the core — and
    carries every protocol rule; the CONVENTIONS follow it, then the
    index."""
    from mcp import disclosure

    text = _instructions()[variant]
    core = _core()
    assert text.startswith(core + " "), "the SAFETY CORE must open the text"
    missing = [m for m in _CORE_MARKERS if m not in core]
    assert missing == [], f"not in the SAFETY CORE: {missing}"
    # REWRITTEN deliberately (2026-09-30): the CONVENTIONS, not the index,
    # follow the core — the index comes after the head.
    rest = text[len(core):]
    assert rest.lstrip().startswith("CONVENTIONS: ")
    head = disclosure.instructions_head_en()
    assert text.startswith(head + " TOOLS: ")
    # Everything after the core is the conventions, the index and the rest
    # — never a second copy of the protocol rules.
    assert "idempotency_key` on EVERY write" not in rest


@pytest.mark.parametrize("variant", [0, 1], ids=["base", "comptabilite"])
def test_the_conventions_and_every_core_promise_survive_the_client_s_cut(
    variant,
):
    """What a client cutting at 2 048 characters reads: the WHOLE safety
    core — every in-core promise, the outbound effect, the protocol — and
    the whole CONVENTIONS, in both variants. Asserted on the actual first
    2 048 characters of the served text, not on the pieces' lengths: a
    reordering that pushed a promise past the cut fails here even if every
    piece stayed short."""
    from mcp import disclosure

    text = _instructions()[variant]
    cut = text[:_head_limit()]
    head = disclosure.instructions_head_en()
    assert len(head) <= _head_limit(), (
        f"the head (core + conventions) is {len(head)} characters, over "
        f"{_head_limit()}: a client cutting there loses its end")
    assert head in cut
    for never in disclosure.core_nevers():
        assert never.en in cut, never.key
    missing = [m for m in _CORE_MARKERS + _CONVENTION_MARKERS if m not in cut]
    assert missing == [], f"past the client's cut: {missing}"
    # The conventions are said ONCE: the paragraphs they replaced do not
    # reappear after the index.
    assert text.count("`*_cents`") == 1
    assert text.count("inscrit par Claude") == 1
    # The wording the review refused: obeyed literally, it deletes the
    # stamp lines of a body re-sent through update_task / update_note.
    assert "never type it" not in text


def test_the_core_states_only_promises_true_for_every_token():
    """The core is the same for every token, so no accounting-only promise
    may stand in it — the accounting variant states those after the
    index."""
    from mcp import disclosure

    core = _core()
    assert disclosure.core_nevers()
    for never in disclosure.core_nevers():
        assert not never.accounting_only, never.key
        assert never.en in core, never.key
    for never in disclosure.accounting_nevers():
        assert never.en not in core, never.key


def test_each_family_is_one_short_index_line():
    """The index names a family's tools and its one rule — the prose moved
    into the tool descriptions. A line past the cap is prose creeping back."""
    from mcp import disclosure

    for family in disclosure.FAMILIES:
        size = len(family.instructions_en.encode("utf-8"))
        assert size <= 450, (family.key, size)
