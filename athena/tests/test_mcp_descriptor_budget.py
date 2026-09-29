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
largest descriptor overall still `update_partie` at 7 858.

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
# production text reached a Claude Code session truncated at character
# 2 047. The protocol rules came last, after ~16 KB of family prose.
# Measured 2026-09-29 with the protocol core in place: 22 852 / 26 037
# UTF-8 bytes. The caps sit just above.
INSTRUCTIONS_CAP = 24_500
INSTRUCTIONS_COMPTABILITE_CAP = 27_500
# What a client cutting at ~2 048 characters must still have read.
TRUNCATION_POINT = 2_048
_CORE_MARKERS = (
    "idempotency_key",
    "the SAME key, never a new one",
    "« ENREGISTRÉE — NE PAS RÉESSAYER »",
    "`expected_etag`",
    "stale_etag",
    "`decide_rendez_vous`",
    "cancels the client's Outlook meeting",
    "a deletion",
    "a payment outside the accounting registers",
)


def _instructions():
    with mock.patch("google.cloud.firestore.Client"):
        from mcp import endpoint
    return endpoint.INSTRUCTIONS, endpoint.INSTRUCTIONS_COMPTABILITE


def test_the_instructions_stay_within_their_budget():
    base, accounting = _instructions()
    for label, text, cap in (("INSTRUCTIONS", base, INSTRUCTIONS_CAP),
                             ("INSTRUCTIONS_COMPTABILITE", accounting,
                              INSTRUCTIONS_COMPTABILITE_CAP)):
        size = len(text.encode("utf-8"))
        assert size <= cap, (
            f"{label} weighs {size} bytes, over its {cap}-byte budget: it "
            "rides every initialize — move prose the tool descriptions "
            "already carry out of it")


@pytest.mark.parametrize("variant", [0, 1], ids=["base", "comptabilite"])
def test_the_protocol_core_survives_a_client_that_cuts_at_2048(variant):
    text = _instructions()[variant]
    head = text[:TRUNCATION_POINT]
    missing = [m for m in _CORE_MARKERS if m not in head]
    assert missing == [], (
        f"not within the first {TRUNCATION_POINT} characters: {missing}")
    # And it comes FIRST, right after the header — before any family.
    assert head.index("BEFORE ANY WRITE") < head.index("READ-CONTENT")
