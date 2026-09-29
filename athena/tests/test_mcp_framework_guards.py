"""Derived guards over the MCP connector's framework — plan Lot 0a.

Every inventory here is DERIVED from the registry (`mcp.tools.TOOLS`,
`WRITE_TOOLS`, `EDIT_TOOLS`, `OUTPUT_SCHEMAS`) or from the source itself,
never re-typed: a hand-kept list stops proving anything the day the next tool
lands, which this repo has watched happen more than once
(`test_every_paged_tool_declares_a_cursor_input` was first written with a
tuple and missed `list_invoices`, then `list_notes`). Where a rule has a
legitimate exception, it is an explicit dict of {tool: reason}, and a test
fails if an exemption stops being needed.

What each guard buys:

(a) every write handler funnels through `run_write("<its own name>", args, …)`
    — the idempotency record is keyed by that name, so a copy-pasted sibling
    name would let one tool replay ANOTHER tool's stored result;
(b) WRITE_TOOLS membership and the declared scope are the same fact — a
    write tool that lost its `scope` would default to `athena:read` and be
    callable by a read-only token;
(c) a name that promises an edit (`update_`, `set_`, …) carries
    `destructiveHint`, and every non-prefixed edit is declared with a reason;
(d) no per-tool annotation override can rewrite a derived safety hint;
(e) every edit declares its concurrency policy; an ``expected_etag`` in the
    input schema is exactly what that policy says; and every tool that
    accepts one names read tools whose output declares the ``etag`` it
    expects — and hands the NEW etag back in its own result;
(f) a write that reaches a DAV-exposed model (derived: a model whose
    serializer the dav/ package calls) declares ``ctag_bumped`` +
    ``dav_synced`` and reaches ``bump_ctag``, and a tool that declares them
    really writes such a record — the design's hand-kept ``writes`` list,
    derived from the handlers' call closure instead;
(g) every tool under ``athena:comptabilite`` is a write that DEMANDS an
    ``idempotency_key`` (policy ``required``, listed in its schema) — armed
    now, vacuous on the real registry until plan lot 5, proven on a planted
    one and on the dummy the scope tests register;
(i) no write output declares a property that would persist privileged
    content or a capability URL into the `mcp_idempotency` replay cache, and
    no output at all declares a signed-URL or storage-path property;
(j) every tool has at least one conformance run against its REAL handler.

(Pinned counts live in test_mcp_tools.py, once. Nothing here pins 49.)
"""

import ast
import inspect
import os
import pathlib
import re
import sys
import textwrap
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from mcp.output_schemas import OUTPUT_SCHEMAS

from tests import _dummy_accounting  # noqa: E402

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _handler(name: str):
    return getattr(handlers, tools.TOOLS[name]["handler"])


# ══════════════════════════════════════════════════════════════════════
# (a) Every write handler wraps run_write("<its own name>")
# ══════════════════════════════════════════════════════════════════════


def _function_def(fn) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef), fn
    return node


def _statements(fn_def: ast.FunctionDef) -> list:
    body = list(fn_def.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # the docstring
    return body


def _eval(node, env: dict):
    """Abstract value of an argument node: ("const", v), ("args",) or None."""
    if isinstance(node, ast.Constant):
        return ("const", node.value)
    if isinstance(node, ast.Name):
        return env.get(node.id)
    return None


def _run_write_call(fn, env: dict, seen: frozenset) -> tuple:
    """Follow `return f(...)` delegation inside mcp.handlers down to the
    run_write call; return the abstract (tool_name, args) it receives.

    Resolution is honest: a delegate is followed with its parameters bound
    to what the caller actually passed, so a delegate that forwards the tool
    name is accepted, and a delegate that could run anything BEFORE or
    INSTEAD of run_write is not.
    """
    fn_def = _function_def(fn)
    stmts = _statements(fn_def)
    assert len(stmts) == 1 and isinstance(stmts[0], ast.Return), (
        f"{fn.__name__}: a write handler must be a single `return run_write(...)` "
        "(or a single `return <delegate>(...)` to one): any other statement "
        "runs OUTSIDE the idempotency protocol."
    )
    call = stmts[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name), fn.__name__
    if call.func.id == "run_write":
        assert len(call.args) == 3 and not call.keywords, (
            f"{fn.__name__}: run_write takes (tool, args, execute) positionally"
        )
        return _eval(call.args[0], env), _eval(call.args[1], env)
    delegate = getattr(handlers, call.func.id, None)
    assert inspect.isfunction(delegate) and delegate.__module__ == handlers.__name__, (
        f"{fn.__name__}: returns {call.func.id}(...), which is not a function of "
        "mcp.handlers — nothing proves it reaches run_write"
    )
    assert call.func.id not in seen, f"delegation cycle through {call.func.id}"
    params = [a.arg for a in _function_def(delegate).args.args]
    bound = {p: _eval(a, env) for p, a in zip(params, call.args)}
    bound.update({k.arg: _eval(k.value, env) for k in call.keywords if k.arg})
    return _run_write_call(delegate, bound, seen | {call.func.id})


def test_mcp_handlers_uses_the_real_run_write():
    """Shadowing `run_write` in mcp.handlers would bypass every guard below."""
    assert handlers.run_write is write_support.run_write


@pytest.mark.parametrize("name", sorted(tools.WRITE_TOOLS))
def test_every_write_handler_funnels_through_run_write_under_its_own_name(name):
    fn = _handler(name)
    first_param = _function_def(fn).args.args[0].arg
    tool_arg, args_arg = _run_write_call(fn, {first_param: ("args",)}, frozenset())
    assert tool_arg == ("const", name), (
        f"{name}: run_write is keyed by {tool_arg!r} — the idempotency record "
        "would be shared with another tool"
    )
    assert args_arg == ("args",), (
        f"{name}: run_write must fingerprint the handler's OWN arguments"
    )


@pytest.mark.parametrize("name", sorted(tools.WRITE_TOOLS))
def test_every_write_handler_calls_run_write_at_runtime(name, monkeypatch):
    """The runtime half: whatever indirection the source uses, the call that
    actually happens carries the tool's own name and the very args object,
    exactly once, with a callable `execute` left to run_write.

    What this half does NOT prove is that nothing ran BEFORE the call: the
    models behind the handlers are MagicMocks in this process, so a write
    made ahead of run_write would succeed silently. The AST half above
    (a single `return` statement, followed through every delegate) is what
    closes that door."""
    seen = []

    def spy(tool, args, execute):
        seen.append((tool, args, callable(execute)))
        return {"spied": True}

    monkeypatch.setattr(handlers, "run_write", spy)
    args = {"idempotency_key": "guard-key-0001"}
    assert _handler(name)(args) == {"spied": True}
    assert seen == [(name, args, True)]
    assert seen[0][1] is args


_DELEGATION_FIXTURE = '''
def run_write(tool, args, execute):
    return execute()


def forwards(args):
    return _funnel(args, "forwards")


def forwards_by_keyword(args):
    return _funnel(tool="forwards_by_keyword", a=args)


def _funnel(a, tool):
    return run_write(tool, a, lambda: None)


def borrows_a_sibling_name(args):
    return _sibling(args)


def _sibling(a):
    return run_write("someone_else", a, lambda: None)


def drops_the_args(args):
    return _funnel({}, "drops_the_args")


def works_first(args):
    args = dict(args)
    return run_write("works_first", args, lambda: None)


def cycles(args):
    return _cycle_back(args)


def _cycle_back(args):
    return cycles(args)
'''


@pytest.fixture()
def delegation_module(tmp_path, monkeypatch):
    """A throw-away module standing in for mcp.handlers, so the delegation
    branch of the walker — which no handler exercises today, all 22 being
    the direct form — is proven to follow bindings rather than wave them
    through."""
    import importlib.util

    path = tmp_path / "fake_handlers_for_guard.py"
    path.write_text(_DELEGATION_FIXTURE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fake_handlers_for_guard", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys.modules[__name__], "handlers", module)
    return module


def _walk(fn):
    first_param = _function_def(fn).args.args[0].arg
    return _run_write_call(fn, {first_param: ("args",)}, frozenset())


def test_the_walker_follows_a_delegate_with_its_parameters_bound(delegation_module):
    m = delegation_module
    assert _walk(m.forwards) == (("const", "forwards"), ("args",))
    assert _walk(m.forwards_by_keyword) == (("const", "forwards_by_keyword"), ("args",))


def test_the_walker_reports_what_a_delegate_really_passes(delegation_module):
    """A delegate keyed by another tool's name, or one that fingerprints a
    literal instead of the handler's arguments, comes back as exactly that —
    so the parametrized guard above refuses it instead of passing it."""
    m = delegation_module
    assert _walk(m.borrows_a_sibling_name) == (("const", "someone_else"), ("args",))
    assert _walk(m.drops_the_args)[1] is None


def test_the_walker_refuses_work_before_the_call_and_delegation_cycles(delegation_module):
    m = delegation_module
    with pytest.raises(AssertionError, match="single `return run_write"):
        _walk(m.works_first)
    with pytest.raises(AssertionError, match="cycle"):
        _walk(m.cycles)


def test_no_read_handler_calls_run_write():
    """The converse: the write protocol wraps writes only, so a READ handler
    that calls run_write is a write that escaped WRITE_TOOLS."""
    offenders = []
    for name in sorted(set(tools.TOOLS) - tools.WRITE_TOOLS):
        src = ast.parse(textwrap.dedent(inspect.getsource(_handler(name))))
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run_write"
            for n in ast.walk(src)
        ):
            offenders.append(name)
    assert offenders == []


# ══════════════════════════════════════════════════════════════════════
# (b) Scope ↔ membership, both directions
# ══════════════════════════════════════════════════════════════════════


def _write_scopes() -> set:
    """Every scope that gates a write. Derived from the package: the plan's
    `athena:comptabilite` joins by being defined, not by editing this test."""
    return {s for s in mcp.SCOPES_SUPPORTED if s != mcp.SCOPE_READ}


def test_write_membership_and_write_scope_are_the_same_fact():
    """REWRITTEN in lot 5b. It read « every tool under a non-read scope is a
    write » — true until get_admin_ledger, the one READ that carries
    athena:comptabilite (the administration ledger is not under
    athena:read). The fact survives in its two exact halves: athena:write
    is carried by writes and only writes, and every write carries a write
    scope; and a tool under neither write scope is a read gated by
    athena:read. The accounting read is the one allowed exception, and
    (g) below proves it carries no write protocol at all."""
    write_scoped = {
        n for n, spec in tools.TOOLS.items() if spec.get("scope") == mcp.SCOPE_WRITE
    }
    assert write_scoped <= set(tools.WRITE_TOOLS)
    assert write_scoped | tools.ACCOUNTING_WRITE_TOOLS == set(tools.WRITE_TOOLS)
    by_scope = {n for n, spec in tools.TOOLS.items() if spec.get("scope") in _write_scopes()}
    assert by_scope - set(tools.WRITE_TOOLS) <= tools.ACCOUNTING_TOOLS
    for name in tools.WRITE_TOOLS:
        assert tools.required_scope(name) != mcp.SCOPE_READ, name
    for name in set(tools.TOOLS) - tools.WRITE_TOOLS - tools.ACCOUNTING_TOOLS:
        assert tools.required_scope(name) == mcp.SCOPE_READ, name
    for name in tools.ACCOUNTING_TOOLS - tools.WRITE_TOOLS:
        assert tools.required_scope(name) == mcp.SCOPE_COMPTABILITE, name


def test_every_declared_scope_is_advertised():
    """A tool gated by a scope the OAuth metadata never advertises could not
    be granted — a permanently dead tool, or worse, one a later edit makes
    reachable through the wrong scope."""
    declared = {spec["scope"] for spec in tools.TOOLS.values() if "scope" in spec}
    assert declared <= set(mcp.SCOPES_SUPPORTED)


def test_the_subsets_nest():
    assert tools.WRITE_TOOLS <= set(tools.TOOLS)
    assert tools.EDIT_TOOLS <= tools.WRITE_TOOLS


def test_the_write_protocol_marks_writes_and_only_writes():
    """`idempotency_key` in the input schema ⇔ the tool is a write. Together
    with test_mcp_tools' per-write checks, a read tool cannot carry the
    write protocol (a sign of a write that escaped the registry) and a write
    tool cannot lack it."""
    carrying = {
        n for n, spec in tools.TOOLS.items()
        if "idempotency_key" in spec["input_schema"].get("properties", {})
    }
    assert carrying == set(tools.WRITE_TOOLS)


def test_every_write_tool_declares_its_idempotency_policy():
    """run_write reads the policy from the registry (`"idempotency"`), never
    from the handler — so it must be DECLARED, explicitly, on every write:
    an absent key would silently mean `optional`, which is exactly the
    wrong default for the money and outbound tools that are coming. A read
    tool declares none (it has no write to protect). A `required` tool must
    also list `idempotency_key` in its schema's `required`: the client
    should learn from the schema, not from a refusal, that a key is owed."""
    for name, spec in tools.TOOLS.items():
        if name not in tools.WRITE_TOOLS:
            assert "idempotency" not in spec, name
            continue
        assert spec.get("idempotency") in tools.IDEMPOTENCY_POLICIES, name
        if spec["idempotency"] == tools.IDEMPOTENCY_REQUIRED:
            assert "idempotency_key" in spec["input_schema"].get("required", []), name


def test_the_key_s_description_says_what_its_policy_does():
    """Lot-5 completeness review: the eight `required` tools described their
    key as « Recommended on every unattended/scheduled write » — the
    optional policy's text — beside a schema that demands it. DERIVED from
    the declaration both ways: a required key says REQUIRED and never
    « Recommended », an optional one keeps the recommendation — worded
    « Pass one on every write » since the finitions (contracts-7), the
    INSTRUCTIONS rule, never the weaker « unattended/scheduled » one."""
    required = {n for n, s in tools.TOOLS.items()
                if s.get("idempotency") == tools.IDEMPOTENCY_REQUIRED}
    assert len(required) == 8, sorted(required)
    for name, spec in tools.TOOLS.items():
        prop = spec["input_schema"]["properties"].get("idempotency_key")
        if prop is None:
            assert name not in tools.WRITE_TOOLS, name
            continue
        text = prop["description"]
        if name in required:
            assert text == tools.REQUIRED_KEY_DESCRIPTION, name
            assert "REQUIRED" in text and "Recommended" not in text, name
        else:
            assert "Pass one on every write" in text, name
            assert "unattended/scheduled" not in text, name
            assert "REQUIRED" not in text, name


# ══════════════════════════════════════════════════════════════════════
# (g) Accounting: its own scope, and a key it DEMANDS
# ══════════════════════════════════════════════════════════════════════
#
# Plan decision D1 + rule 7. The trust and administration registers are
# append-only — a mistake is corrected by a reversal, never erased — so a
# money write retried without a key is a SECOND entry that no tool can take
# back. Every WRITE under athena:comptabilite must therefore demand an
# `idempotency_key` (policy `required`, so the store fails CLOSED) and list
# it in its schema's `required`.
#
# Lot 5b gave the scope its first tools — five writes and ONE read,
# get_admin_ledger (the administration ledger is not under athena:read). A
# tool under the scope that is NOT in WRITE_TOOLS used to be a violation
# outright (« it would slip past the write gate »); it is now allowed only
# as a PROVEN read: it declares no idempotency policy, its schema carries no
# `idempotency_key`, and no annotation claims it writes. A write that
# forgot to join WRITE_TOOLS carries the write protocol, and is caught by
# exactly those three checks. (test_no_read_handler_calls_run_write, above,
# is the fourth: its handler never opens the write protocol.)
#
# The rule lives in ONE pure function, proven below on a planted registry,
# so each clause is shown to catch what it claims.


def accounting_violations(registry: dict, accounting_tools, write_tools) -> list[str]:
    """Every breach of the accounting contract in *registry*."""
    out: list[str] = []
    for name, spec in sorted(registry.items()):
        declared = spec.get("scope") == mcp.SCOPE_COMPTABILITE
        if declared is not (name in accounting_tools):
            out.append(
                f"{name}: ACCOUNTING_TOOLS membership contradicts its declared scope")
        if not declared:
            continue
        if name not in write_tools:
            # The accounting READ (lot 5b): allowed only while nothing about
            # it says « write » — the write protocol is what a write that
            # escaped WRITE_TOOLS would still be carrying.
            props = spec["input_schema"].get("properties", {})
            annotations = spec.get("annotations") or {}
            if (
                spec.get("idempotency") is not None
                or "idempotency_key" in props
                or annotations.get("readOnlyHint") is False
            ):
                out.append(
                    f"{name}: an accounting tool outside WRITE_TOOLS escapes the write gate")
            continue
        if spec.get("idempotency") != tools.IDEMPOTENCY_REQUIRED:
            out.append(
                f"{name}: an accounting tool must declare idempotency 'required'")
        if "idempotency_key" not in spec["input_schema"].get("required", []):
            out.append(
                f"{name}: an accounting tool must list idempotency_key in required")
    return out


def test_the_real_registry_honours_the_accounting_contract():
    assert accounting_violations(
        tools.TOOLS, tools.ACCOUNTING_TOOLS, tools.WRITE_TOOLS) == []


def test_the_dummy_accounting_tool_honours_the_contract(monkeypatch):
    """The dummy the scope tests register (tests/_dummy_accounting.py) is
    shaped like a lot-5 tool. Were it not, those tests would prove the
    gates on a tool that could never ship."""
    name = _dummy_accounting.register(monkeypatch)
    assert name in tools.ACCOUNTING_TOOLS
    assert accounting_violations(
        tools.TOOLS, tools.ACCOUNTING_TOOLS, tools.WRITE_TOOLS) == []


def _planted_accounting(**spec_over) -> dict:
    spec = {
        "scope": mcp.SCOPE_COMPTABILITE,
        "idempotency": tools.IDEMPOTENCY_REQUIRED,
        "input_schema": {"type": "object", "properties": {},
                         "required": ["idempotency_key"]},
    }
    spec.update(spec_over)
    return {"record_x": spec}


@pytest.mark.parametrize("over, sets, fragment", [
    ({}, None, None),  # the well-formed world is clean
    ({"idempotency": tools.IDEMPOTENCY_OPTIONAL}, None, "idempotency 'required'"),
    ({"idempotency": None}, None, "idempotency 'required'"),
    ({"input_schema": {"type": "object", "properties": {}, "required": []}},
     None, "idempotency_key in required"),
    # A write that forgot WRITE_TOOLS still carries the write protocol.
    ({}, {"write": frozenset()}, "escapes the write gate"),
    ({"input_schema": {"type": "object", "properties": {"idempotency_key": {}},
                       "required": []}, "idempotency": None},
     {"write": frozenset()}, "escapes the write gate"),
    ({"idempotency": None, "annotations": {"readOnlyHint": False},
      "input_schema": {"type": "object", "properties": {}}},
     {"write": frozenset()}, "escapes the write gate"),
    # …while a genuine accounting READ (get_admin_ledger's shape) is clean.
    ({"idempotency": None, "input_schema": {"type": "object", "properties": {}}},
     {"write": frozenset()}, None),
    ({}, {"accounting": frozenset()}, "contradicts its declared scope"),
    ({"scope": mcp.SCOPE_WRITE}, None, "contradicts its declared scope"),
])
def test_the_accounting_guard_catches_what_it_claims(over, sets, fragment):
    registry = _planted_accounting(**over)
    sets = sets or {}
    found = accounting_violations(
        registry,
        sets.get("accounting", frozenset({"record_x"})),
        sets.get("write", frozenset({"record_x"})),
    )
    if fragment is None:
        assert found == []
    else:
        assert any(fragment in v for v in found), found


def test_every_input_schema_refuses_unknown_arguments():
    """additionalProperties: False on EVERY tool, reads included. On a write
    it is what made the dry_run removal fail-closed; on a read it is what
    turns a misspelled filter into a named refusal instead of an ignored one
    that silently widens the result."""
    open_schemas = [
        n for n, spec in tools.TOOLS.items()
        if spec["input_schema"].get("additionalProperties") is not False
    ]
    assert open_schemas == []


# ══════════════════════════════════════════════════════════════════════
# (c) Edit-promising names ⊆ EDIT_TOOLS, and every other edit declared
# ══════════════════════════════════════════════════════════════════════

# The policy lives beside EDIT_TOOLS (mcp/tools.py); this guard reads it. The
# six prefixes the guard shipped with are pinned as a floor, so the policy
# can widen but never quietly lose one.
EDIT_NAME_PREFIXES = tools.EDIT_NAME_PREFIXES
_PREFIX_FLOOR = ("update_", "set_", "replace_", "move_", "reopen_", "void_")

# Tools whose name promises an edit but which are NOT destructive. Empty.
_PREFIX_EXEMPT: dict[str, str] = {}

# Edits whose name does not say so — each with the reason it REPLACES or
# irreversibly flips a stored value. A new member needs its reason here.
_EDIT_BY_DECLARATION: dict[str, str] = {
    "record_document_analysis": (
        "replaces the document's stored category with the one the code "
        "derives from the recorded sous-nature"
    ),
    "import_invoice": (
        "flips its sources to « facturée », after which the connector's "
        "update tools refuse them — undone only by voiding in the app"
    ),
    "complete_task": (
        "replaces the task's stored status and, through the model's "
        "cascade, the linked protocol step's — up to closing the whole "
        "protocol (plan lot 0a, disclosure step)"
    ),
    "decide_rendez_vous": (
        "replaces a Bookings request's stored decision gate — and its "
        "refusal cancels the client's Outlook meeting, which notifies him "
        "(lot 1b, L7; plan D10)"
    ),
    "manage_folder": (
        "its rename and move REPLACE a folder's stored name or parent — "
        "every document filed below moves with it (lot 2A, T7)"
    ),
    "finalize_upload": (
        "its gabarit « replace » mode REPLACES the template file in force — "
        "every future letter prints from it; the previous version is kept "
        "(lot 2A, T9; review of lot 2)"
    ),
    "create_invoice": (
        "consumes the year's next invoice number FOR EVER (no void gives it "
        "back) and flips its sources to « facturée » (lot 3b)"
    ),
    "create_budget_version": (
        "supersedes the dossier's reference budget — the version whose "
        "« Estimation » is the client quote; earlier ones are kept (lot 3b)"
    ),
    "record_kyc_status": (
        "replaces a client's stored identity or conflict-check status — "
        "presumed until the lawyer confirms it, never over his own "
        "attestation, but a compliance record replaced all the same "
        "(lot 4b, D7)"
    ),
    "record_trust_entry": (
        "moves a client's trust balance in a register that is never erased "
        "— only reversed — and a fee payment also records a payment on the "
        "invoice, which may flip it to « payée » (lot 5b)"
    ),
    "record_admin_entry": (
        "moves an account's balance in a register a reconciliation must "
        "then explain, and an encaissement records a payment on the invoice, "
        "which may flip it to « payée » (lot 5b)"
    ),
}


def _promises_an_edit(name: str) -> bool:
    return name.startswith(EDIT_NAME_PREFIXES)


def test_the_edit_prefix_policy_only_ever_widens():
    assert set(_PREFIX_FLOOR) <= set(EDIT_NAME_PREFIXES)
    # « complete_ » would sweep complete_dossier in, which fills EMPTY
    # fields only and replaces nothing.
    assert not any(p.startswith("complete") for p in EDIT_NAME_PREFIXES)
    assert "complete_dossier" not in tools.EDIT_TOOLS


def test_every_edit_named_tool_is_an_edit_tool():
    missing = sorted(
        n for n in tools.TOOLS
        if _promises_an_edit(n) and n not in tools.EDIT_TOOLS and n not in _PREFIX_EXEMPT
    )
    assert missing == [], (
        f"{missing}: the name promises an edit, so the tool must be in "
        "EDIT_TOOLS (destructiveHint) or exempted here with a reason"
    )


def test_every_other_edit_tool_is_declared_with_a_reason():
    undeclared = sorted(
        n for n in tools.EDIT_TOOLS
        if not _promises_an_edit(n) and n not in _EDIT_BY_DECLARATION
    )
    assert undeclared == [], undeclared


def test_the_exemption_lists_are_not_stale():
    for name, reason in _PREFIX_EXEMPT.items():
        assert name in tools.TOOLS and _promises_an_edit(name) and name not in tools.EDIT_TOOLS, name
        assert reason.strip(), name
    for name, reason in _EDIT_BY_DECLARATION.items():
        assert name in tools.EDIT_TOOLS and not _promises_an_edit(name), name
        assert reason.strip(), name


# ══════════════════════════════════════════════════════════════════════
# (d) No annotation override can rewrite a derived safety hint
# ══════════════════════════════════════════════════════════════════════

# The only hint a tool may state for itself: whether a repeat call is a
# no-op, which genuinely varies per tool within a family.
_OVERRIDABLE_HINTS = {"idempotentHint"}


def test_annotation_overrides_touch_only_the_idempotency_hint():
    for name, spec in tools.TOOLS.items():
        override = spec.get("annotations") or {}
        assert set(override) <= _OVERRIDABLE_HINTS, (
            f"{name} overrides {sorted(set(override) - _OVERRIDABLE_HINTS)}: "
            "readOnlyHint/destructiveHint/openWorldHint are DERIVED from "
            "WRITE_TOOLS/EDIT_TOOLS and must not be restated per tool"
        )
        assert all(isinstance(v, bool) for v in override.values()), name
        if override:
            assert name in tools.WRITE_TOOLS, (
                f"{name}: a read tool has no write semantics to annotate"
            )


@pytest.mark.parametrize("with_accounting_tool", [False, True])
def test_the_advertised_safety_hints_follow_the_registry(monkeypatch, with_accounting_tool):
    """Every switch ON: `list_tool_descriptors(None)` drops no scope but
    still applies the kill switches, and MCP_COMPTABILITE_ENABLED defaults
    to FALSE — so without this an accounting tool (plan lot 5) would have
    its hints go unchecked in silence, and a money write advertised
    `readOnlyHint: true` would pass. The equality makes any hiding loud; the
    dummy run proves the accounting subset is really enumerated."""
    if with_accounting_tool:
        _dummy_accounting.register(monkeypatch)
    monkeypatch.setattr(tools, "write_enabled", lambda: True)
    monkeypatch.setattr(tools, "comptabilite_enabled", lambda: True)
    descriptors = tools.list_tool_descriptors(None)
    assert {d["name"] for d in descriptors} == set(tools.TOOLS)
    for d in descriptors:
        ann = d["annotations"]
        is_write = d["name"] in tools.WRITE_TOOLS
        assert ann["readOnlyHint"] is (not is_write), d["name"]
        if is_write:
            assert ann["destructiveHint"] is (d["name"] in tools.EDIT_TOOLS), d["name"]


# ══════════════════════════════════════════════════════════════════════
# (e) Concurrency: declared on every edit, and an etag the caller can get
# ══════════════════════════════════════════════════════════════════════
#
# Plan rule 3. An `expected_etag` a caller cannot obtain from any read is a
# dead argument — worse, its description (« from your latest read ») would
# be false. And a policy that is not declared would let the next edit tool
# ship last-write-wins without anyone deciding it should. The rules live in
# ONE pure function over a registry, so the planted-defect test below
# proves the mechanism and the real-registry test proves the code.

_ETAGGED = (tools.CONCURRENCY_OPTIONAL, tools.CONCURRENCY_REQUIRED)


def _declares_property(schema, name: str) -> bool:
    return any(key == name for _p, key in _property_names(schema))


def concurrency_violations(
    registry: dict, output_schemas: dict, write_tools, edit_tools,
) -> list[str]:
    """Every breach of the concurrency contract in *registry*."""
    out: list[str] = []
    for name, spec in sorted(registry.items()):
        policy = spec.get("concurrency")
        schema = spec["input_schema"]
        props = schema.get("properties", {})
        if name not in write_tools:
            if policy is not None:
                out.append(f"{name}: a read tool declares a concurrency policy")
            continue
        if name in edit_tools and policy not in tools.CONCURRENCY_POLICIES:
            out.append(
                f"{name}: replaces a stored value but declares no "
                "`concurrency` policy (optional | required | exempt — with a "
                "`concurrency_reason` when exempt). Decide whether it is "
                "last-write-wins before it ships")
        if name not in edit_tools and policy not in (None, tools.CONCURRENCY_EXEMPT):
            # A non-edit write may pre-declare only an exemption.
            out.append(f"{name}: a non-edit write declares {policy!r}")
        if policy == tools.CONCURRENCY_EXEMPT:
            if not str(spec.get("concurrency_reason") or "").strip():
                out.append(f"{name}: exempt without a concurrency_reason")
        elif "concurrency_reason" in spec:
            out.append(f"{name}: a concurrency_reason on a non-exempt tool")

        # The argument is exactly what the policy says.
        if ("expected_etag" in props) is not (policy in _ETAGGED):
            out.append(f"{name}: expected_etag in the schema contradicts {policy!r}")
        if "etag" in props:
            out.append(f"{name}: a property literally named `etag`")
        if policy == tools.CONCURRENCY_REQUIRED and "expected_etag" not in schema.get("required", []):
            out.append(f"{name}: required policy, expected_etag not required")
        if "expected_etag" in props:
            prop = props["expected_etag"]
            if prop.get("type") != "string" or not prop.get("description"):
                out.append(f"{name}: expected_etag must be a described string")
            if prop.get("minLength", 0):
                out.append(f"{name}: expected_etag refuses '' (a legacy etag)")

        # An etag the caller can obtain, and the new one handed back.
        readers = tuple(spec.get("etag_readers") or ())
        if readers and policy not in _ETAGGED:
            out.append(f"{name}: names etag_readers but accepts no etag")
        if policy not in _ETAGGED:
            continue
        if not readers:
            out.append(f"{name}: accepts expected_etag but names no reader")
        desc = props.get("expected_etag", {}).get("description", "")
        for reader in readers:
            if reader not in registry or reader in write_tools:
                out.append(f"{name}: {reader} is not a read tool")
                continue
            if not _declares_property(output_schemas[reader], "etag"):
                out.append(f"{name}: {reader}'s output never declares an etag")
            if reader not in desc:
                out.append(f"{name}: expected_etag's text does not name {reader}")
        entity = output_schemas[name].get("properties", {}).get("entity", {})
        if "etag" not in entity.get("properties", {}):
            out.append(f"{name}: its result does not hand back the new etag")
        elif "etag" in entity.get("required", []):
            # A replayed result stored before 2026-09-25 lacks it.
            out.append(f"{name}: the result etag is required, not optional")
    return out


def test_the_real_registry_honours_the_concurrency_contract():
    assert concurrency_violations(
        tools.TOOLS, OUTPUT_SCHEMAS, tools.WRITE_TOOLS, tools.EDIT_TOOLS,
    ) == []


def test_the_concurrency_guard_is_not_vacuous():
    guarded = sorted(n for n, spec in tools.TOOLS.items()
                     if spec.get("concurrency") in _ETAGGED)
    assert {"update_partie", "update_dossier", "update_time_entry",
            "update_expense", "set_time_entry_phase",
            "set_expense_phase"} <= set(guarded), guarded
    assert all(tools.TOOLS[n]["concurrency"] for n in tools.EDIT_TOOLS)


def _planted_registry(**spec_over) -> tuple[dict, dict]:
    """A two-tool world: one reader exposing an etag, one guarded editor."""
    reader_out = {"type": "object", "properties": {"items": {
        "type": "array", "items": {"type": "object", "properties": {
            "etag": {"type": "string"}}}}}}
    editor = {
        "input_schema": {"type": "object", "properties": {
            "x_id": {"type": "string", "description": "id"},
            "expected_etag": {"type": "string", "maxLength": 64,
                              "description": "etag from list_x"},
        }},
        "concurrency": tools.CONCURRENCY_OPTIONAL,
        "etag_readers": ("list_x",),
    }
    editor.update(spec_over)
    registry = {
        "list_x": {"input_schema": {"type": "object", "properties": {}}},
        "update_x": editor,
    }
    outputs = {
        "list_x": reader_out,
        "update_x": {"type": "object", "properties": {"entity": {
            "type": "object", "properties": {"etag": {"type": "string"}},
            "required": []}}},
    }
    return registry, outputs


@pytest.mark.parametrize("over, fragment", [
    ({}, None),  # the well-formed world is clean
    ({"concurrency": None}, "declares no `concurrency` policy"),
    ({"etag_readers": ()}, "names no reader"),
    ({"etag_readers": ("list_y",)}, "is not a read tool"),
    ({"concurrency": tools.CONCURRENCY_EXEMPT,
      "concurrency_reason": "because"}, "contradicts"),
    ({"concurrency": tools.CONCURRENCY_EXEMPT}, "without a concurrency_reason"),
    ({"concurrency": tools.CONCURRENCY_REQUIRED}, "not required"),
])
def test_the_concurrency_guard_catches_what_it_claims(over, fragment):
    registry, outputs = _planted_registry(**over)
    found = concurrency_violations(
        registry, outputs, frozenset({"update_x"}), frozenset({"update_x"}))
    if fragment is None:
        assert found == []
    else:
        assert any(fragment in v for v in found), found


def test_the_concurrency_guard_catches_a_reader_without_an_etag_and_a_result_without_one():
    registry, outputs = _planted_registry()
    outputs["list_x"] = {"type": "object", "properties": {}}
    found = concurrency_violations(
        registry, outputs, frozenset({"update_x"}), frozenset({"update_x"}))
    assert any("never declares an etag" in v for v in found), found

    registry, outputs = _planted_registry()
    outputs["update_x"]["properties"]["entity"]["properties"] = {}
    found = concurrency_violations(
        registry, outputs, frozenset({"update_x"}), frozenset({"update_x"}))
    assert any("hand back the new etag" in v for v in found), found

    registry, outputs = _planted_registry()
    outputs["update_x"]["properties"]["entity"]["required"] = ["etag"]
    found = concurrency_violations(
        registry, outputs, frozenset({"update_x"}), frozenset({"update_x"}))
    assert any("required, not optional" in v for v in found), found

    registry, outputs = _planted_registry()
    registry["update_x"]["input_schema"]["properties"]["etag"] = {"type": "string"}
    found = concurrency_violations(
        registry, outputs, frozenset({"update_x"}), frozenset({"update_x"}))
    assert any("literally named `etag`" in v for v in found), found


# ══════════════════════════════════════════════════════════════════════
# (f) A write that reaches a DAV-exposed model reports its CTag bump
# ══════════════════════════════════════════════════════════════════════
#
# CLAUDE.md, Change Impact item 1: a write to a DAV-exposed record that
# skips `bump_ctag` desyncs DavX5 SILENTLY — the note is in Firestore and in
# the web app, the phone never hears of it. The design's guard (f) wanted a
# hand-declared `writes` list per tool; this is its DERIVED form, so it
# cannot go stale the day a tool lands without the list:
#
# * a model module is DAV-exposed when it defines a `<x>_to_v…` serializer
#   the dav/ package actually calls (derived from both sources — the legacy
#   `dossier_to_vjournal`, which no DAV path calls, correctly stays out);
# * a write tool DAV-writes when its handler's call closure inside
#   mcp/handlers.py references a MUTATOR (`create_`, `update_`, …) of such a
#   module;
# * DAV-writes ⇔ its outputSchema declares `ctag_bumped` AND `dav_synced`,
#   and a DAV writer's closure reaches `bump_ctag`.
#
# The schema half is what the conformance runs then hold the handler to: a
# declared, required `ctag_bumped` the handler does not emit fails
# test_mcp_output_schemas. The closure over-approximates (it follows every
# module-level name a function mentions), so it can only ever flag MORE
# writers, never hide one.
#
# Two indirections the closure follows since lot 1b (L6), because the plan
# routes the protocol tools through a SERVICE and their task writes happen
# inside a MODEL:
#
# * a `from services import X as Y` binding: `Y.fn` is followed into
#   `services/X.py`'s own closure, with that module's model aliases;
# * a MODEL function that writes a DAV-exposed record from another model
#   module (`models/protocol.create_linked_tasks` creates tasks,
#   `set_step_status` cascades into one) counts as a mutator of that
#   record — derived from the models' source (`model_dav_writers`), never
#   listed. Without both, a protocol tool that syncs tasks would look like
#   « a sync that does not exist », and one that forgot to report the sync
#   would pass.

_DAV_SERIALIZER = re.compile(r"^[a-z_]+_to_v(card|todo|event|journal)$")
_MUTATOR_VERB = re.compile(
    r"^(create|update|set|record|append|void|reverse|clear|confirm|move|"
    r"delete|toggle|complete|attach|link|unlink|reopen|replace|remove|add)_"
)
_PACKAGE = _TESTS_DIR.parent


def dav_exposed_models() -> set[str]:
    """`models` modules whose records DavX5 syncs — derived, not listed."""
    used = set()
    for path in sorted((_PACKAGE / "dav").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _DAV_SERIALIZER.match(node.attr):
                used.add(node.attr)
            elif isinstance(node, ast.Name) and _DAV_SERIALIZER.match(node.id):
                used.add(node.id)
            elif isinstance(node, ast.alias) and _DAV_SERIALIZER.match(node.name):
                used.add(node.name)
    exposed = set()
    for path in sorted((_PACKAGE / "models").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if defined & used:
            exposed.add(path.stem)
    return exposed


def module_index(source: str) -> tuple[dict, dict]:
    """(module-level name → node, model alias → models module) of a source."""
    tree = ast.parse(source)
    defs: dict = {}
    aliases: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defs[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                defs[node.target.id] = node.value
        elif isinstance(node, ast.ImportFrom) and node.module == "models":
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return defs, aliases


def service_aliases_of(source: str) -> dict:
    """`from services import X as Y` bindings of a source: {Y: X}."""
    out: dict = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ImportFrom) and node.module == "services":
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _service_source(module: str, services: "dict | None") -> str:
    if services is not None and module in services:
        return services[module]
    return (_PACKAGE / "services" / f"{module}.py").read_text(encoding="utf-8")


def reach(defs: dict, aliases: dict, start: str, *,
          service_aliases: "dict | None" = None,
          services: "dict | None" = None,
          _visited: "set | None" = None) -> tuple[set, set]:
    """Every module-level name *start* can reach, and every
    `(models module, attribute)` referenced on the way.

    With *service_aliases*, a `Y.fn` on a service binding is followed into
    that service module's own closure (its names and model attributes are
    merged in); *services* maps a service module to its source (tests),
    the disk otherwise."""
    names: set = set()
    model_attrs: set = set()
    service_aliases = service_aliases or {}
    visited = _visited if _visited is not None else set()
    stack, seen = [start], set()
    while stack:
        current = stack.pop()
        if current in seen or current not in defs:
            continue
        seen.add(current)
        for sub in ast.walk(defs[current]):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
                if sub.id in defs:
                    stack.append(sub.id)
            elif (isinstance(sub, ast.Attribute)
                  and isinstance(sub.value, ast.Name)
                  and sub.value.id in aliases):
                model_attrs.add((aliases[sub.value.id], sub.attr))
            elif (isinstance(sub, ast.Attribute)
                  and isinstance(sub.value, ast.Name)
                  and sub.value.id in service_aliases):
                module = service_aliases[sub.value.id]
                if (module, sub.attr) in visited:
                    continue
                visited.add((module, sub.attr))
                source = _service_source(module, services)
                s_defs, s_aliases = module_index(source)
                s_names, s_attrs = reach(
                    s_defs, s_aliases, sub.attr,
                    service_aliases=service_aliases_of(source),
                    services=services, _visited=visited)
                names |= s_names
                model_attrs |= s_attrs
    return names, model_attrs


def model_dav_writers(
    exposed: set, sources: "dict | None" = None,
) -> set:
    """`(models module, function)` of every model function that writes a
    DAV-exposed record OF ANOTHER MODULE — derived, transitively within its
    module.

    A function qualifies when it references a mutator (`_MUTATOR_VERB`)
    bound from an exposed module: a bare name from `from models.<exposed>
    import <mutator>` (module-level or, the house pattern for the
    protocol↔task edge, inside the function), or `<alias>.<mutator>` on a
    `from models import <exposed> as <alias>` binding. A function that
    calls such a function of its own module qualifies too. *sources* maps a
    module to its source (tests); the disk otherwise. An exposed module's
    own functions are already mutators of their own record and are not
    listed here."""
    if sources is None:
        sources = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted((_PACKAGE / "models").glob("*.py"))
        }
    out: set = set()
    for module, source in sorted(sources.items()):
        if module in exposed:
            continue
        tree = ast.parse(source)
        defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

        def _bindings(node) -> tuple[set, dict]:
            bare, alias = set(), {}
            for sub in ast.walk(node):
                if not isinstance(sub, ast.ImportFrom):
                    continue
                mod = sub.module or ""
                if mod.startswith("models.") and mod.split(".", 1)[1] in exposed:
                    for a in sub.names:
                        if _MUTATOR_VERB.match(a.name):
                            bare.add(a.asname or a.name)
                elif mod == "models":
                    for a in sub.names:
                        if a.name in exposed:
                            alias[a.asname or a.name] = a.name
            return bare, alias

        top_bare, top_alias = _bindings(ast.Module(
            body=[n for n in tree.body if isinstance(n, ast.ImportFrom)],
            type_ignores=[]))
        writers = set()
        for name, fn in defs.items():
            bare, alias = _bindings(fn)
            bare |= top_bare
            alias = {**top_alias, **alias}
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Name) and sub.id in bare:
                    writers.add(name)
                elif (isinstance(sub, ast.Attribute)
                      and isinstance(sub.value, ast.Name)
                      and sub.value.id in alias
                      and _MUTATOR_VERB.match(sub.attr)):
                    writers.add(name)
        changed = True
        while changed:
            changed = False
            for name, fn in defs.items():
                if name in writers:
                    continue
                if any(isinstance(sub, ast.Name) and sub.id in writers
                       for sub in ast.walk(fn)):
                    writers.add(name)
                    changed = True
        out |= {(module, name) for name in writers}
    return out


def model_self_bumping(sources: "dict | None" = None) -> set:
    """`(models module, function)` of every model function that bumps its
    OWN DAV collection inside its write batch — a reference to
    `bump_ctag_in_batch` in its body. Derived, never listed.

    CLAUDE.md's documented exception to « the bump lives in the route » (a
    batched write puts its bump IN the batch, since commit-then-bump leaves
    N records DavX5 never re-syncs): `models/hearing.create_hearing_series`
    and `delete_series`. A handler reaching ONLY such writers has nothing
    left to bump — bumping again would be a second, pointless sync — so
    the « reaches bump_ctag » half of the guard is satisfied by the model
    (lot 1b, L7). The declaration half is not: the tool must still report
    `ctag_bumped`/`dav_synced`."""
    if sources is None:
        sources = {
            path.stem: path.read_text(encoding="utf-8")
            for path in sorted((_PACKAGE / "models").glob("*.py"))
        }
    out: set = set()
    for module, source in sorted(sources.items()):
        for fn in ast.parse(source).body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            if any((isinstance(n, ast.Name) and n.id == "bump_ctag_in_batch")
                   or (isinstance(n, ast.Attribute)
                       and n.attr == "bump_ctag_in_batch")
                   or (isinstance(n, ast.alias)
                       and n.name == "bump_ctag_in_batch")
                   for n in ast.walk(fn)):
                out.add((module, fn.name))
    return out


def dav_writer_violations(
    source: str, registry: dict, output_schemas: dict, write_tools,
    exposed: set, *, services: "dict | None" = None,
    model_writers: "set | None" = None,
    self_bumping: "set | None" = None,
) -> list[str]:
    """Every breach of « DAV-writes ⇔ reports its CTag bump »."""
    defs, aliases = module_index(source)
    svc = service_aliases_of(source)
    if model_writers is None:
        model_writers = model_dav_writers(exposed)
    if self_bumping is None:
        self_bumping = model_self_bumping()
    out: list[str] = []
    for name in sorted(registry):
        names, attrs = reach(defs, aliases, registry[name]["handler"],
                             service_aliases=svc, services=services)
        pairs = sorted((m, a) for m, a in attrs
                       if (m in exposed and _MUTATOR_VERB.match(a))
                       or (m, a) in model_writers)
        mutated = [f"{m}.{a}" for m, a in pairs]
        bumps_itself = bool(pairs) and all(p in self_bumping for p in pairs)
        is_write = name in write_tools
        declares = (_declares_property(output_schemas[name], "ctag_bumped")
                    and _declares_property(output_schemas[name], "dav_synced"))
        if mutated and not is_write:
            out.append(f"{name}: a READ tool reaches {mutated}")
            continue
        if not is_write:
            continue
        if mutated and not declares:
            out.append(
                f"{name}: writes a DAV-exposed record ({mutated}) but its "
                "outputSchema does not declare ctag_bumped + dav_synced")
        if mutated and "bump_ctag" not in names and not bumps_itself:
            out.append(f"{name}: writes {mutated} but never reaches bump_ctag")
        if declares and not mutated:
            out.append(
                f"{name}: declares ctag_bumped/dav_synced but writes no "
                "DAV-exposed record — a sync that does not exist")
    return out


def _handlers_source() -> str:
    return pathlib.Path(handlers.__file__).read_text(encoding="utf-8")


def test_the_dav_exposed_models_are_derived_and_not_vacuous():
    exposed = dav_exposed_models()
    # Anchors, not an inventory: the four DavX5 surfaces of CLAUDE.md.
    assert {"note", "task", "hearing", "partie"} <= exposed, exposed
    # The legacy dossier VJOURNAL is serialized by no DAV path post-D1.
    assert "dossier" not in exposed


def test_every_dav_writer_reports_its_ctag_bump():
    assert dav_writer_violations(
        _handlers_source(), tools.TOOLS, OUTPUT_SCHEMAS, tools.WRITE_TOOLS,
        dav_exposed_models(),
    ) == []


def test_the_dav_writer_guard_is_not_vacuous():
    source = _handlers_source()
    defs, aliases = module_index(source)
    exposed = dav_exposed_models()
    model_writers = model_dav_writers(exposed)
    writers = set()
    for name in tools.WRITE_TOOLS:
        _names, attrs = reach(defs, aliases, tools.TOOLS[name]["handler"],
                              service_aliases=service_aliases_of(source))
        if any((m in exposed and _MUTATOR_VERB.match(a))
               or (m, a) in model_writers for m, a in attrs):
            writers.add(name)
    # One anchor per DAV surface the connector writes today — and, since
    # lot 1b (L6), the protocol tools that reach tasks through the service
    # (create_protocol through a model cascade, update_protocol through the
    # alignment).
    assert {"create_note", "append_to_note", "create_task", "complete_task",
            "create_hearing", "create_partie", "update_partie",
            "create_protocol", "update_protocol", "add_protocol_step",
            "update_protocol_step", "update_hearing",
            "create_hearing_series", "decide_rendez_vous"} <= writers, writers


def test_the_handlers_import_models_only_through_an_alias():
    """The closure sees a model write only as `<alias>.<verb>_…`. A handler
    importing a model FUNCTION directly (`from models.note import
    update_note`) would write through a bare name the guard never maps to a
    module — so that import form is refused outright."""
    tree = ast.parse(_handlers_source())
    direct = sorted(
        f"line {node.lineno}: from {node.module} import …"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("models.")
    )
    assert direct == []


def test_no_read_handler_reaches_run_write_through_a_helper():
    """(a)'s converse, transitively: the direct-body check above cannot see a
    read handler that delegates to a helper which calls run_write."""
    defs, aliases = module_index(_handlers_source())
    offenders = sorted(
        name for name in set(tools.TOOLS) - tools.WRITE_TOOLS
        if "run_write" in reach(defs, aliases, tools.TOOLS[name]["handler"])[0]
    )
    assert offenders == []


_DAV_FIXTURE = '''
from models import note as note_model
from models import dossier as dossier_model

def bump_ctag(c): ...

def _bump(did):
    bump_ctag(did)

def good(args):
    return run_write("good", args, lambda: _good_impl(args))

def _good_impl(args):
    note_model.update_note(args["id"], {})
    _bump("x")
    return {}

def silent(args):
    return run_write("silent", args, lambda: note_model.create_note({}))

def undeclared(args):
    return run_write("undeclared", args, lambda: _good_impl(args))

def phantom(args):
    return run_write("phantom", args, lambda: dossier_model.update_dossier("d", {}))

def batched(args):
    return run_write("batched", args, lambda: note_model.create_notes_batch({}))

def half_batched(args):
    return run_write("half_batched", args, lambda: _half(args))

def _half(args):
    note_model.create_notes_batch({})
    note_model.update_note("n", {})
    return {}

def sneaky_read(args):
    return _good_impl(args)
'''


def _fixture_world():
    registry = {n: {"handler": n} for n in
                ("good", "silent", "undeclared", "phantom", "sneaky_read",
                 "batched", "half_batched")}
    reporting = {"type": "object", "properties": {
        "ctag_bumped": {"type": "boolean"}, "dav_synced": {"type": "boolean"}}}
    plain = {"type": "object", "properties": {}}
    outputs = {"good": reporting, "silent": reporting, "undeclared": plain,
               "phantom": reporting, "sneaky_read": plain,
               "batched": reporting, "half_batched": reporting}
    writes = frozenset({"good", "silent", "undeclared", "phantom",
                        "batched", "half_batched"})
    return registry, outputs, writes


def test_the_self_bumping_model_writers_are_derived_and_not_vacuous():
    """The series writers are the documented exception (the bump rides in
    the batch); an ordinary mutator never qualifies."""
    found = model_self_bumping()
    assert {("hearing", "create_hearing_series"),
            ("hearing", "delete_series")} <= found, found
    assert not {("hearing", "create_hearing"), ("hearing", "update_hearing"),
                ("hearing", "unlink_hearing")} & found
    planted = model_self_bumping({"x": (
        "def f():\n    from dav.sync import bump_ctag_in_batch\n"
        "    bump_ctag_in_batch(b, 'c')\n"
        "def g():\n    return 1\n")})
    assert planted == {("x", "f")}


def test_the_model_dav_writers_are_derived_and_not_vacuous():
    """The protocol↔task edge: a protocol function that creates or
    cascades into a task writes a DAV-exposed record, although the
    protocol itself is not DAV-exposed."""
    writers = model_dav_writers(dav_exposed_models())
    assert {("protocol", "create_linked_tasks"),
            ("protocol", "_sync_task_status"),
            ("protocol", "set_step_status")} <= writers, writers
    # A read, or a write of the protocol alone, never qualifies.
    assert not {("protocol", "get_protocol"), ("protocol", "add_step"),
                ("protocol", "update_step")} & writers


_SERVICE_FIXTURE = '''
from models import note as note_model
from models import ledger as ledger_model


def write_note(i):
    return _inner(i)


def _inner(i):
    note_model.create_note({})


def read_only(i):
    return note_model.get_note(i)


def via_model(i):
    return ledger_model.cascade(i)
'''

_MODEL_FIXTURE = '''
def cascade(i):
    return _deeper(i)


def _deeper(i):
    from models.note import update_note
    update_note(i, {})


def harmless(i):
    from models.note import get_note
    return get_note(i)
'''

_SERVICE_HANDLERS = '''
from services import svc as svc_service

def bump_ctag(c): ...

def via_service(args):
    return run_write("via_service", args, lambda: _vs(args))

def _vs(args):
    svc_service.write_note(args)
    bump_ctag("x")
    return {}

def via_service_silent(args):
    return run_write("via_service_silent", args,
                     lambda: svc_service.write_note(args))

def via_service_read(args):
    return run_write("via_service_read", args,
                     lambda: svc_service.read_only(args))

def via_model_cascade(args):
    return run_write("via_model_cascade", args, lambda: _vm(args))

def _vm(args):
    svc_service.via_model(args)
    bump_ctag("x")
    return {}
'''


def test_the_dav_writer_guard_follows_services_and_model_cascades():
    registry = {n: {"handler": n} for n in (
        "via_service", "via_service_silent", "via_service_read",
        "via_model_cascade")}
    reporting = {"type": "object", "properties": {
        "ctag_bumped": {"type": "boolean"}, "dav_synced": {"type": "boolean"}}}
    plain = {"type": "object", "properties": {}}
    outputs = {"via_service": reporting, "via_service_silent": plain,
               "via_service_read": reporting, "via_model_cascade": reporting}
    writers = model_dav_writers({"note"}, sources={"ledger": _MODEL_FIXTURE})
    assert writers == {("ledger", "cascade"), ("ledger", "_deeper")}
    found = dav_writer_violations(
        _SERVICE_HANDLERS, registry, outputs, frozenset(registry), {"note"},
        services={"svc": _SERVICE_FIXTURE}, model_writers=writers)
    assert not any(v.startswith("via_service:") for v in found), found
    assert not any(v.startswith("via_model_cascade:") for v in found), found
    assert any(v.startswith("via_service_silent:") and "does not declare" in v
               for v in found), found
    assert any(v.startswith("via_service_read:")
               and "a sync that does not exist" in v for v in found), found


def test_the_dav_writer_guard_catches_what_it_claims():
    registry, outputs, writes = _fixture_world()
    found = dav_writer_violations(
        _DAV_FIXTURE, registry, outputs, writes, {"note"},
        self_bumping={("note", "create_notes_batch")})
    assert not any(v.startswith("good:") for v in found), found
    # A writer that bumps inside its own batch satisfies the bump half…
    assert not any(v.startswith("batched:") for v in found), found
    # …but only when EVERY record the tool writes is written that way.
    assert any(v.startswith("half_batched:") and "never reaches bump_ctag" in v
               for v in found), found
    assert any(v.startswith("silent:") and "never reaches bump_ctag" in v
               for v in found), found
    assert any(v.startswith("undeclared:") and "does not declare" in v
               for v in found), found
    assert any(v.startswith("phantom:") and "a sync that does not exist" in v
               for v in found), found
    assert any(v.startswith("sneaky_read:") and "READ tool" in v
               for v in found), found
    # and the closure is what lets a read tool be caught through a helper
    defs, aliases = module_index(_DAV_FIXTURE)
    assert "run_write" not in reach(defs, aliases, "sneaky_read")[0]
    assert "run_write" in reach(defs, aliases, "good")[0]


# ══════════════════════════════════════════════════════════════════════
# (i) Output-schema property names that must never appear
# ══════════════════════════════════════════════════════════════════════

# A write result is stored VERBATIM in mcp_idempotency for 24 h and replayed.
# Content fields would persist privileged text there; URL/path fields would
# persist a capability. Reads may return content (get_note, get_document_text)
# — that is their job — but no output may ever carry a URL or a storage path.
_NO_URL_NAMES = {"signed_url", "upload_url", "download_url", "storage_path"}
_CONTENT_NAMES = {"content", "text", "body", "contenu", "previous_value"}
_WRITE_FORBIDDEN_NAMES = _NO_URL_NAMES | _CONTENT_NAMES


def _is_capability_name(key: str) -> bool:
    """A signed URL or a storage path, by NAME — the four the design lists,
    plus anything URL-shaped (`url`, `*_url`): a `preview_url` or a
    `file_url` added later is the same capability under a name nobody
    thought to list. (`conference_uri` — a hearing's video link, which the
    lawyer typed — is a URI, not a URL-to-our-storage, and stays legal.)"""
    return key in _NO_URL_NAMES or key == "url" or key.endswith("_url")


def _is_write_forbidden_name(key: str) -> bool:
    return key in _CONTENT_NAMES or _is_capability_name(key)

# {tool: {property: reason}}. The Lot 2 upload ticket (the one documented
# exception to « no signed URL in output ») is added HERE, by name, with its
# justification — and with the persist/rehydrate hooks that keep the URL out
# of mcp_idempotency (test_an_exempted_capability_output_declares_its_
# persistence_hooks). tests/test_mcp_output_schemas.py allowlists the SAME
# single pair when it scans every real payload's VALUES.
_OUTPUT_NAME_EXEMPTIONS: dict[str, dict[str, str]] = {
    "begin_upload": {
        "upload_url": (
            "plan D4: a WRITE-only resumable-upload session URI for ONE "
            "neutral staging object, capped at the declared size; "
            "finalize_upload files its bytes only if their size and MD5 are "
            "the declared ones, so a leaked URL cannot inject content. "
            "Never stored: the persist hook strips it from mcp_idempotency, "
            "and a replay re-opens a session for the same open ticket."
        ),
    },
}


def _property_names(schema) -> list[tuple[str, str]]:
    """Every (json-path, property name) declared anywhere in *schema*.

    Walks the WHOLE structure — anyOf branches, items, nested objects — and
    treats any dict under a `properties` key as a name → schema map, so a
    keyword the helpers start using later cannot hide a property.
    """
    out = []

    def walk(node, path):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for key, sub in props.items():
                    out.append((f"{path}/{key}", key))
                    walk(sub, f"{path}/{key}")
            for key, sub in node.items():
                if key != "properties":
                    walk(sub, f"{path}[{key}]")
        elif isinstance(node, list):
            for i, sub in enumerate(node):
                walk(sub, f"{path}[{i}]")

    walk(schema, "")
    return out


def _violations(is_forbidden, tool_filter) -> dict:
    found = {}
    for tool in sorted(OUTPUT_SCHEMAS):
        if not tool_filter(tool):
            continue
        allowed = _OUTPUT_NAME_EXEMPTIONS.get(tool, {})
        hits = [
            p for p, key in _property_names(OUTPUT_SCHEMAS[tool])
            if is_forbidden(key) and key not in allowed
        ]
        if hits:
            found[tool] = hits
    return found


def test_the_property_walker_sees_nested_and_branched_properties():
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {"type": "array", "items": {
            "type": "object", "properties": {"signed_url": {"type": "string"}}}}}},
        {"type": "object", "properties": {"b": {"type": "object", "properties": {
            "content": {"type": "string"}}}}},
    ]}
    assert {k for _p, k in _property_names(schema)} == {"a", "signed_url", "b", "content"}


def test_the_forbidden_name_rules_bite():
    for key in _WRITE_FORBIDDEN_NAMES | {"url", "preview_url", "file_url"}:
        assert _is_write_forbidden_name(key), key
    for key in ("conference_uri", "folder_path", "hourly_rate", "urls_count"):
        assert not _is_capability_name(key), key
    assert not _is_write_forbidden_name("description")


def test_no_write_output_declares_content_or_a_capability():
    assert _violations(_is_write_forbidden_name, lambda t: t in tools.WRITE_TOOLS) == {}


def test_no_output_at_all_declares_a_url_or_a_storage_path():
    assert _violations(_is_capability_name, lambda t: True) == {}


def test_output_exemptions_are_not_stale():
    for tool, props in _OUTPUT_NAME_EXEMPTIONS.items():
        declared = {k for _p, k in _property_names(OUTPUT_SCHEMAS[tool])}
        for prop, reason in props.items():
            assert prop in declared and reason.strip(), (tool, prop)


def test_an_exempted_capability_output_declares_its_persistence_hooks():
    """Lot 2A, T5: the one tool allowed to RETURN a capability must keep it
    out of mcp_idempotency — its persist/rehydrate pair is registered in
    ``mcp.write_support`` (the handlers are imported above, so their
    registrations have run). ``run_write`` refuses to store a capability
    either way; without the hooks the tool would simply never replay."""
    for tool, props in _OUTPUT_NAME_EXEMPTIONS.items():
        if any(_is_capability_name(prop) for prop in props):
            assert tool in write_support.persistence_tools(), tool
    # run_write's storage guard and this module name capabilities alike.
    for key in _NO_URL_NAMES | {"url", "preview_url", "file_url"}:
        assert write_support.is_capability_key(key), key
    for key in ("conference_uri", "folder_path", "urls_count"):
        assert not write_support.is_capability_key(key), key


# ══════════════════════════════════════════════════════════════════════
# (j) Every tool has a conformance run against its REAL handler
# ══════════════════════════════════════════════════════════════════════


def _conformance_calls(fn_node) -> set[str]:
    """Tool names whose output schema this test function validates:
    `_conforms("<tool>", …)` or `validate_args(OUTPUT_SCHEMAS["<tool>"], …)`."""
    names = set()
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        first = node.args[0]
        if fname == "_conforms" and isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        elif fname == "validate_args" and isinstance(first, ast.Subscript):
            base = first.value
            bname = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
            if bname == "OUTPUT_SCHEMAS" and isinstance(first.slice, ast.Constant):
                names.add(first.slice.value)
    return names


def _handler_calls(fn_node) -> set[str]:
    """Attribute calls `<x>.<attr>(…)` in the function — a handler call reads
    `handlers.list_notes(...)` or `h.record_document_analysis(...)`. A model
    alias (`note_model.create_note`) is excluded so a same-named MODEL call
    cannot pass for the handler."""
    out = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            value = node.func.value
            if isinstance(value, ast.Name) and not value.id.endswith("_model"):
                out.add(node.func.attr)
    return out


def _covered_tools() -> dict[str, list[str]]:
    covered: dict[str, list[str]] = {}
    for path in sorted(_TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = _handler_calls(fn)
            for tool in _conformance_calls(fn):
                handler = tools.TOOLS.get(tool, {}).get("handler")
                if handler and handler in called:
                    covered.setdefault(tool, []).append(f"{path.name}::{fn.name}")
    return covered


def test_every_tool_has_a_real_handler_conformance_run():
    """The outputSchema is a MUST-conform contract, and only a run of the
    real handler proves the payload honours it — a fixture validated in
    isolation proves the fixture. A tool without one ships its contract
    unproven (the `_obj` auto-required-key trap is exactly the defect such
    a run catches)."""
    covered = _covered_tools()
    missing = sorted(set(tools.TOOLS) - set(covered))
    assert missing == [], (
        f"no test validates the REAL handler output of {missing} against "
        "OUTPUT_SCHEMAS (tests/test_mcp_output_schemas.py is the place)"
    )


def test_the_conformance_scan_recognises_both_call_forms():
    fn = ast.parse(textwrap.dedent('''
        def test_x():
            _conforms("list_notes", handlers.list_notes({}))
            r = h.get_note({})
            assert t.validate_args(o.OUTPUT_SCHEMAS["get_note"], r) == []
            note_model.create_note({})
            _conforms("create_note", {})
    ''')).body[0]
    assert _conformance_calls(fn) == {"list_notes", "get_note", "create_note"}
    calls = _handler_calls(fn)
    assert {"list_notes", "get_note"} <= calls
    assert "create_note" not in calls  # a MODEL call does not pass for the handler


# ══════════════════════════════════════════════════════════════════════
# (k) Outbound tools: declared, derived, and held to the strict policies
# ══════════════════════════════════════════════════════════════════════
#
# Lot 1b (L7) shipped the connector's first effect OUTSIDE the practice's
# records: decide_rendez_vous's refusal cancels a client's Outlook meeting
# (plan D10). MCP's openWorldHint is DERIVED from tools.OUTBOUND_TOOLS; this
# section makes the set itself derived from the code, both ways — a tool
# whose closure (services followed) reaches an outbound module is a member,
# and a member that reaches none is refused as a false alarm — and holds
# every member to plan rules 3 and 7: the etag AND the key demanded.

# Names through which an outbound effect is reached: the Graph calendar
# module (the Bookings cancellation) and the raw Graph write verbs. A
# reference to any of them in a tool's closure is the fact. The email
# module is NOT a marker, deliberately: the closure collects bare names, and
# `courriel` is also an ordinary local variable (services/rendez_vous reads
# the requester's address into one) — a marker that fires on it would make
# list_hearings an « outbound » tool. utils.courriel is forbidden outright
# to every connector module and reached service instead (the
# « client_message » and « invoice_status » nevers, tests/test_mcp_disclosure).
_OUTBOUND_MARKERS = frozenset({
    "graph_calendrier", "graph_post", "graph_patch", "graph_delete",
})


def outbound_violations(source: str, registry: dict, outbound: frozenset,
                        *, services: "dict | None" = None) -> list[str]:
    defs, aliases = module_index(source)
    svc = service_aliases_of(source)
    out: list[str] = []
    for name in sorted(registry):
        names, _attrs = reach(defs, aliases, registry[name]["handler"],
                              service_aliases=svc, services=services)
        reaches = bool(names & _OUTBOUND_MARKERS)
        if reaches and name not in outbound:
            out.append(f"{name}: reaches an outbound effect but is not in "
                       "OUTBOUND_TOOLS (openWorldHint would under-warn)")
        if name in outbound and not reaches:
            out.append(f"{name}: in OUTBOUND_TOOLS but reaches no outbound "
                       "effect — a false alarm")
    return out


def test_the_outbound_set_is_derived_from_the_code():
    assert outbound_violations(
        _handlers_source(), tools.TOOLS, tools.OUTBOUND_TOOLS) == []


def test_every_outbound_tool_demands_its_key_and_its_etag():
    assert tools.OUTBOUND_TOOLS, "non-vacuous: decide_rendez_vous"
    for name in tools.OUTBOUND_TOOLS:
        spec = tools.TOOLS[name]
        assert name in tools.WRITE_TOOLS and name in tools.EDIT_TOOLS, name
        assert spec.get("idempotency") == tools.IDEMPOTENCY_REQUIRED, name
        assert spec.get("concurrency") == tools.CONCURRENCY_REQUIRED, name
        required = spec["input_schema"].get("required", [])
        assert {"idempotency_key", "expected_etag"} <= set(required), name


_OUTBOUND_FIXTURE = '''
from services import mailer as mailer_service

def loud(args):
    return run_write("loud", args, lambda: mailer_service.send(args))

def quiet(args):
    return run_write("quiet", args, lambda: {})
'''


def test_the_outbound_guard_catches_what_it_claims():
    registry = {"loud": {"handler": "loud"}, "quiet": {"handler": "quiet"}}
    services = {"mailer": "from utils import graph_calendrier\n"
                          "def send(a):\n"
                          "    graph_calendrier.annuler_reservation('x', 'y')\n"}
    found = outbound_violations(_OUTBOUND_FIXTURE, registry, frozenset(),
                                services=services)
    assert any(v.startswith("loud:") and "not in OUTBOUND_TOOLS" in v
               for v in found), found
    found = outbound_violations(_OUTBOUND_FIXTURE, registry,
                                frozenset({"loud", "quiet"}),
                                services=services)
    assert found == ["quiet: in OUTBOUND_TOOLS but reaches no outbound "
                     "effect — a false alarm"], found
