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
    by_scope = {n for n, spec in tools.TOOLS.items() if spec.get("scope") in _write_scopes()}
    assert by_scope == set(tools.WRITE_TOOLS)
    for name in tools.WRITE_TOOLS:
        assert tools.required_scope(name) != mcp.SCOPE_READ, name
    for name in set(tools.TOOLS) - tools.WRITE_TOOLS:
        assert tools.required_scope(name) == mcp.SCOPE_READ, name


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

EDIT_NAME_PREFIXES = ("update_", "set_", "replace_", "move_", "reopen_", "void_")

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
}


def _promises_an_edit(name: str) -> bool:
    return name.startswith(EDIT_NAME_PREFIXES)


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


def test_the_advertised_safety_hints_follow_the_registry():
    for d in tools.list_tool_descriptors(None):
        ann = d["annotations"]
        is_write = d["name"] in tools.WRITE_TOOLS
        assert ann["readOnlyHint"] is (not is_write), d["name"]
        if is_write:
            assert ann["destructiveHint"] is (d["name"] in tools.EDIT_TOOLS), d["name"]


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

# {tool: {property: reason}}. Empty today. The Lot 2 upload ticket (the one
# documented exception to « no signed URL in output ») must be added HERE, by
# name, with its justification — and with the persist/rehydrate hooks that
# keep the URL out of mcp_idempotency.
_OUTPUT_NAME_EXEMPTIONS: dict[str, dict[str, str]] = {}


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
