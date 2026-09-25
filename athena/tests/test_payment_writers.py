"""Who may write a payment — pinned by an AST sweep, keyed by RELATIVE path.

`models.invoice.record_payment` is the single writer of `amount_paid`, and
since 2026-08-17 the accounting module is its only request-served caller,
through `services/encaissements.projeter_paiement` / `reduire_paiement`.
`tests/test_invoice_detail.py::test_the_accounting_module_is_the_only_writer_of_a_payment`
already pins `record_payment`'s callers, but by BASENAME and by raw TEXT:

* basenames collide in this repo (`routes/admin_ledger.py` and
  `models/admin_ledger.py`, `routes/trust.py` and `models/trust.py`), so a
  new caller in the wrong one of a pair would pass unnoticed;
* a raw-text sweep counts a docstring as a call and misses
  `getattr(invoice_model, "record_payment")` or an aliased import — and it
  would flag the literal text of a future disclosure registry that NAMES the
  function in order to forbid it.

This sweep reads the syntax tree only: calls, attribute and name
references, imports (aliases included), and `getattr` with a constant name.
String literals and comments are invisible to it by construction, so no
exemption list is needed for text that merely talks about payments.

The connector (`mcp/`) must reach none of it. The plan opens exactly one
door, in Lot 5, and names it (services/comptabilite.py); until then an MCP
path to a payment is a defect, whatever the tool description says.
"""

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # athena/

# Directories that are not Python application code.
_NOT_CODE = {"tests", "static", "templates", "__pycache__", "node_modules"}

# The scope the plan names. The sweep covers MORE (every first-party package
# and the top-level modules), and this pin keeps it from ever covering less.
_REQUIRED_ROOTS = {"routes", "models", "mcp", "services", "scripts"}

_PAYMENT_WRITER = "record_payment"
_ORCHESTRATION = ("projeter_paiement", "reduire_paiement")
_WATCHED = (_PAYMENT_WRITER,) + _ORCHESTRATION
_ORCHESTRATION_MODULE = "services.encaissements"


def _roots() -> list[pathlib.Path]:
    return sorted(
        p for p in _ROOT.iterdir()
        if p.is_dir() and p.name not in _NOT_CODE and not p.name.startswith(".")
        and any(p.rglob("*.py"))
    )


def _source_files() -> list[pathlib.Path]:
    files = sorted(_ROOT.glob("*.py"))
    for root in _roots():
        files += sorted(f for f in root.rglob("*.py") if "__pycache__" not in f.parts)
    return files


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _scan(tree: ast.AST) -> tuple[dict[str, int], dict[str, int], bool]:
    """(references, definitions, imports_orchestration_module) of one module.

    A REFERENCE is anything that can reach the function: a call, a bare
    name or attribute load (passing it as a callback), an import of it
    (aliased or not), or `getattr(x, "<name>")`.
    A DEFINITION is a def of that name or a rebinding of it.
    """
    refs = dict.fromkeys(_WATCHED, 0)
    defs = dict.fromkeys(_WATCHED, 0)
    imports_module = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in defs:
            defs[node.name] += 1
        elif isinstance(node, ast.Name) and node.id in refs:
            if isinstance(node.ctx, ast.Store):
                defs[node.id] += 1
            else:
                refs[node.id] += 1
        elif isinstance(node, ast.Attribute) and node.attr in refs:
            refs[node.attr] += 1
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name in refs:
                    refs[alias.name] += 1
                if f"{module}.{alias.name}" == _ORCHESTRATION_MODULE:
                    imports_module = True
            if module == _ORCHESTRATION_MODULE:
                imports_module = True
        elif isinstance(node, ast.Import):
            if any(a.name == _ORCHESTRATION_MODULE for a in node.names):
                imports_module = True
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant) and node.args[1].value in refs
        ):
            refs[node.args[1].value] += 1
    return refs, defs, imports_module


@pytest.fixture(scope="module")
def sweep() -> dict[str, tuple[dict[str, int], dict[str, int], bool]]:
    out = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out[_rel(path)] = _scan(tree)
    return out


def _referencing(sweep, names) -> set[str]:
    return {rel for rel, (refs, _d, _m) in sweep.items() if any(refs[n] for n in names)}


def test_the_sweep_covers_at_least_the_named_roots():
    scanned = {p.name for p in _roots()}
    assert _REQUIRED_ROOTS <= scanned, _REQUIRED_ROOTS - scanned


def test_the_sweep_ignores_strings_and_sees_every_reference_form():
    """The detector itself, on a synthetic module: prose never counts, and
    none of the evasive forms escapes."""
    prose = ast.parse('"""record_payment(x) — see projeter_paiement."""\n# reduire_paiement(\n')
    assert _scan(prose) == (dict.fromkeys(_WATCHED, 0), dict.fromkeys(_WATCHED, 0), False)

    evasive = ast.parse(
        "from models.invoice import record_payment as rp\n"
        "import services.encaissements as enc\n"
        "from services import encaissements\n"
        "enc.projeter_paiement(e)\n"
        "cb = invoice_model.reduire_paiement\n"
        "getattr(invoice_model, 'record_payment')(i, 0)\n"
    )
    refs, defs, imports_module = _scan(evasive)
    assert refs == {"record_payment": 2, "projeter_paiement": 1, "reduire_paiement": 1}
    assert defs == dict.fromkeys(_WATCHED, 0)
    assert imports_module


def test_each_payment_function_is_defined_exactly_where_expected(sweep):
    """A second definition elsewhere — a local wrapper that writes
    `amount_paid` under the same name — would make every caller pin below
    meaningless."""
    where = {n: {rel for rel, (_r, defs, _m) in sweep.items() if defs[n]} for n in _WATCHED}
    assert where == {
        "record_payment": {"models/invoice.py"},
        "projeter_paiement": {"services/encaissements.py"},
        "reduire_paiement": {"services/encaissements.py"},
    }, where


def test_record_payment_callers_are_exactly_the_decided_set(sweep):
    """The orchestration, and two hand-run reprise tools (their coexistence
    is argued in test_invoice_detail). Anything else is a second writer of
    `amount_paid` — the very thing the 2026-08-17 lot removed."""
    assert _referencing(sweep, [_PAYMENT_WRITER]) == {
        "services/encaissements.py",
        "scripts/purge_encaissements_factures.py",
        "scripts/reprise_encaissements.py",
    }


def test_projection_callers_are_exactly_the_decided_set(sweep):
    """The accounting routes (administration and the trust fee payment's
    automatic recette) and the reprise assistant. Keyed by relative path:
    `models/admin_ledger.py` and `models/trust.py` are NOT on this list."""
    assert _referencing(sweep, _ORCHESTRATION) == {
        "routes/admin_ledger.py",
        "routes/trust.py",
        "scripts/reprise_encaissements.py",
    }


def test_no_connector_module_can_reach_a_payment(sweep):
    offenders = {
        rel: refs for rel, (refs, _d, imports_module) in sweep.items()
        if rel.startswith("mcp/") and (any(refs.values()) or imports_module)
    }
    assert offenders == {}, offenders
