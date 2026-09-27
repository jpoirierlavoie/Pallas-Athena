"""The disclosure registry (mcp/disclosure.py) — and the code that keeps it true.

Two properties are pinned here, both DERIVED:

1. **The registry is the whole truth about the families.** FAMILIES
   partition WRITE_TOOLS (which is derived from them), every member's
   declared scope is its family's, and INSTRUCTIONS — assembled from the
   registry — name every write tool, with counts computed from the registry,
   never typed.

2. **Every « never » is backed by the code.** Each NEVER carries the calls
   it forbids; this module walks the SYNTAX TREE of every connector module
   (``mcp/*.py`` but the registry itself) — calls, attributes, names,
   imports, ``getattr`` constants — and fails on any reference. String
   literals and comments are invisible to the walk by construction, which
   is why the registry can NAME a forbidden function in order to forbid it
   (a raw-text sweep would have matched the registry itself). A promise no
   call can express (a dossier's status is a PAYLOAD) is backed by the input
   properties no write tool may declare, and by a behavioural test that must
   EXIST.

The consent screen's rendering is pinned in test_mcp_oauth.py (where the
consent-flow fixtures live): every family partial and every NEVER bullet
reach the page, and no known false claim does.
"""

import ast
import os
import pathlib
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp
    import mcp.disclosure as disclosure
    import mcp.endpoint as endpoint
    import mcp.tools as tools

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
_TEMPLATES = _ATHENA / "templates"

# Claims that were once on a surface and were FALSE. They must never come
# back — on the consent screen (test_mcp_oauth), in INSTRUCTIONS, in a
# handler warning or in a tool description. Case-insensitive substrings; the
# patterns allow the corrected continuation (or, for « signée », spare
# « consignée » through the word boundary).
KNOWN_FALSE_CLAIMS: tuple[str, ...] = (
    "se libère",                           # voiding does NOT free the number
    "frees the number",
    "chaque écriture est horodatée",       # only CREATED notes/tasks/events
                                           # carry a dated mention
    "can never edit or delete the entry",  # update_time_entry / update_expense
    "re-opens the linked step",            # complete_task now refuses it
    # create_note's stamp is « Note rédigée par Claude le … »; « Ajouté par
    # Claude » is append_to_note's separator.
    "« ajouté par claude » provenance line",
    # Cancelling a linked task triggers NO cascade (models/task
    # ._sync_protocol_step acts on « terminée » only): only TERMINATING it
    # completes the step.
    "clore une tâche rattachée à une étape de protocole complète",
    # Lot 1b: notes, tasks and the théorie de la cause became editable, and
    # reopening a task became a tool. Each of these once stood on a surface
    # and is false now.
    "cannot edit or delete it afterwards",           # create_note
    "is edited only in the app",                     # append_to_note
    "readable but read-only",                        # list_notes/get_note
    "the only status change this connector can make",  # complete_task
    "can close it with complete_task but can never edit",  # create_task
    "reopening a task is done in the application",
    "rouvrir une tâche se fait dans l'application",
    "jamais rouvrir une tâche close",                # consent, CORRECT
    "ajout à la fin — jamais de modification",       # consent, CREATE
    "en lecture seule via le connecteur",            # append_to_note refusal
    # update_note (revue L5): only a « <…> » span is refused — the sanitizer
    # deletes exactly that; a lone « < » is stored intact.
    "unpaired angle brackets are refused",
    # Lot 1b (L6): create_protocol exists — and record_signification had
    # made « cannot … file a signification » false since July 2026.
    "cannot create a protocol",
    "cannot file a signification",
    # Lot 1b (L7): update_hearing edits an event — create_hearing's old
    # description said the connector never could — and decide_rendez_vous
    # cancels a client's Outlook meeting: « nothing outbound » is false.
    "this connector can never edit or delete it",
    "the connector sends nothing outside",
    "n'envoie jamais rien",
)
KNOWN_FALSE_PATTERNS: tuple[str, ...] = (
    r"\bsignée\b",                         # « signée Claude »
    r"ne peut plus les modifier(?!, sauf)",       # the phase stays reclassifiable
    r"nothing here can modify them afterwards(?! except)",
    # The same billing-freeze claim in its other phrasings: set_*_phase
    # reaches an invoiced row (and so does the application's phase form).
    r"ne modifie(?:nt)? (?:jamais )?une entrée facturée",
    r"définitivement immodifiables",
    r"nothing here can touch it",
    r"neither this connector nor the application can modify it",
)


def _connector_files() -> dict[str, pathlib.Path]:
    """Every connector module, SUBPACKAGES included — a never swept over
    ``mcp/*.py`` alone would stop seeing a handler moved one level down."""
    root = _ATHENA / "mcp"
    return {
        "mcp/" + p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


def _swept_files() -> dict[str, pathlib.Path]:
    return {
        rel: p for rel, p in _connector_files().items()
        if rel not in disclosure.SWEEP_EXCLUDED
    }


# ══════════════════════════════════════════════════════════════════════
# 1. The families
# ══════════════════════════════════════════════════════════════════════


def test_the_families_partition_the_write_tools():
    seen: dict[str, str] = {}
    for family in disclosure.FAMILIES:
        assert family.tools, f"{family.key}: a family that grants nothing"
        for name in family.tools:
            assert name not in seen, f"{name} in {seen.get(name)} and {family.key}"
            seen[name] = family.key
    assert set(seen) == tools.WRITE_TOOLS
    assert disclosure.write_tools() == tools.WRITE_TOOLS
    keys = [f.key for f in disclosure.FAMILIES]
    labels = [f.label for f in disclosure.FAMILIES]
    assert len(keys) == len(set(keys)) and len(labels) == len(set(labels))


def test_each_family_scope_is_its_members_declared_scope():
    for family in disclosure.FAMILIES:
        assert family.scope in (mcp.SCOPE_WRITE, mcp.SCOPE_COMPTABILITE), family.key
        for name in family.tools:
            assert tools.TOOLS[name]["scope"] == family.scope, (family.key, name)


def test_each_family_names_every_member_and_has_its_partial():
    for family in disclosure.FAMILIES:
        for name in family.tools:
            assert f"`{name}`" in family.instructions_en, (family.key, name)
        assert family.consent_template.startswith("mcp/families/_")
        assert (_TEMPLATES / family.consent_template).is_file(), family.consent_template
        assert family.checkbox_summary_fr.strip(), family.key
    # No orphan partial: a partial with no family would never render.
    partials = {f"mcp/families/{p.name}"
                for p in (_TEMPLATES / "mcp" / "families").glob("*.html")}
    assert partials == {f.consent_template for f in disclosure.FAMILIES}


def test_instructions_name_every_write_tool_and_derive_their_counts():
    text = endpoint.INSTRUCTIONS
    assert text == disclosure.build_instructions()
    for name in tools.WRITE_TOOLS:
        assert f"`{name}`" in text, name
    families = [f for f in disclosure.FAMILIES if f.tools]
    reads = len(tools.TOOLS) - len(tools.WRITE_TOOLS)
    assert (
        f"{reads} tools read; {len(tools.WRITE_TOOLS)} write, in "
        f"{len(families)} families ("
        + ", ".join(f.label for f in families) + ")."
    ) in text
    for family in families:
        assert f"{family.label}: " in text


def test_no_count_is_typed_by_hand():
    """The counts went stale twice while they were literals. No connector
    string — docstrings included, they drift the same way — may state a
    tool or family count in words or digits."""
    number = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
    # « in N families » is INSTRUCTIONS' own phrasing — built by an
    # f-string, so its constant parts never hold the number; the domaine
    # taxonomy's « 20 families » counts no tool and stays legal.
    pattern = re.compile(
        rf"(?:\b{number} (?:tools read|read tools|write tools)\b"
        rf"|\bin {number} (?:write )?families\b)",
        re.IGNORECASE,
    )
    assert pattern.search("27 tools read; 22 write, in five families")
    assert not pattern.search("kind='domaines' (20 families)")
    for rel, path in _connector_files().items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not pattern.search(node.value), (rel, node.value[:80])


def test_the_instructions_follow_the_registry(monkeypatch):
    """Fed a registry, the builder reflects it: an accounting tool adds the
    separate-grant sentence, an etag-accepting tool is named, and the counts
    move with the registry."""
    registry = dict(tools.TOOLS)
    base = disclosure.build_instructions(registry, frozenset(), 50)
    assert "athena:comptabilite" not in base
    with_accounting = disclosure.build_instructions(registry, frozenset({"x"}), 50)
    assert "`athena:comptabilite`" in with_accounting
    assert "never stands in" in with_accounting
    for name in tools.TOOLS:
        if tools.TOOLS[name].get("concurrency") in ("optional", "required"):
            assert f"`{name}`" in base, name
    registry["zz_extra_read"] = {"input_schema": {}}
    grown = disclosure.build_instructions(registry, frozenset(), 50)
    assert f"{len(registry) - len(tools.WRITE_TOOLS)} tools read" in grown
    # The reclassifiers' ceiling comes from the registry, never a literal.
    assert "up to 7 rows a call" in disclosure.build_instructions(
        dict(tools.TOOLS), frozenset(), 7)


def test_the_instructions_carry_the_lot_0a_rules():
    text = endpoint.INSTRUCTIONS
    assert "`expected_etag`" in text and "REFUSED and nothing is written" in text
    assert "ENREGISTRÉE — NE PAS RÉESSAYER" in text and "do NOT retry" in text
    assert "retry with the SAME key — never a new one" in text
    assert "`updated_via`" in text and "`mcp_updated_at`" in text
    assert "the number stays on the voided invoice" in text
    # The dormant scope is not advertised while no tool carries it.
    assert not tools.ACCOUNTING_TOOLS
    assert "athena:comptabilite" not in text


def test_the_checkbox_summary_names_every_write_family():
    summary = str(disclosure.write_summary_fr())
    for family in disclosure.families_for(mcp.SCOPE_WRITE):
        assert family.checkbox_summary_fr.lower() in summary.lower()
    assert summary[0].isupper()
    for never in disclosure.NEVERS:
        if never.summary_fr:
            assert f"jamais {never.summary_fr}" in summary.lower()


# ══════════════════════════════════════════════════════════════════════
# 2. The « never » sweep — over the syntax tree, never the text
# ══════════════════════════════════════════════════════════════════════


def _references(tree: ast.AST) -> set[str]:
    """Every identifier the module can REACH: a name or attribute load, an
    imported name (aliased or not), or ``getattr``/``hasattr`` with a
    constant. Definitions and string literals are not references."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.add(node.args[1].value)
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _calls_missing(tree: ast.AST, call: str, keyword: str) -> list[int]:
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == call
        and not any(k.arg == keyword for k in node.keywords)
    ]


def _calls_to(tree: ast.AST, call: str) -> int:
    return sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == call
    )


def violations(
    files: dict[str, str], *, tool_code: "frozenset[str] | set[str]" = (
        disclosure.TOOL_CODE_MODULES),
) -> list[str]:
    """Every breach of every NEVER in *files* ({relative path: source}).

    *tool_code* — the modules the tool-code patterns (a bare ``.delete()``)
    also apply to: the registry's TOOL_CODE_MODULES, plus, for the real
    sweep, every service module a connector module reaches."""
    out: list[str] = []
    for rel, source in sorted(files.items()):
        if rel in disclosure.SWEEP_EXCLUDED:
            continue
        tree = ast.parse(source)
        refs = _references(tree)
        modules = _imported_modules(tree)
        for never in disclosure.NEVERS:
            patterns = list(never.forbidden)
            if rel in tool_code:
                patterns += list(never.forbidden_in_tool_code)
            for pattern in patterns:
                for ident in sorted(refs):
                    if re.fullmatch(pattern, ident):
                        out.append(f"{rel}: {never.key} forbids {ident}")
            for module in never.forbidden_modules:
                if module in modules:
                    out.append(f"{rel}: {never.key} forbids importing {module}")
            for call, keyword in never.required_keywords:
                for line in _calls_missing(tree, call, keyword):
                    out.append(
                        f"{rel}:{line}: {never.key} — {call}() without {keyword}="
                    )
    return out


def _sources() -> dict[str, str]:
    return {rel: p.read_text(encoding="utf-8") for rel, p in _connector_files().items()}


def _service_names(source: str) -> set[str]:
    """The ``services`` modules a source imports, in any form."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "services":
                names.update(alias.name for alias in node.names)
            elif node.module.startswith("services."):
                names.add(node.module.split(".", 2)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("services."):
                    names.add(alias.name.split(".", 2)[1])
    return names


def services_reached(
    files: dict[str, str], read: "dict[str, str] | None" = None,
) -> dict[str, str]:
    """``{"services/X.py": source}`` of every service module the connector
    modules in *files* import — transitively (a service importing a
    service). *read* maps a service name to its source (tests); the disk
    otherwise.

    Since lot 1b (L6) the protocol tools write THROUGH a service
    (``services/protocoles.py``, the door the web routes use), so a sweep
    of the ``mcp`` package alone would no longer prove what a « never »
    promises: a service reached by a tool could call a forbidden model
    function — a ``delete_step`` in a step edit — with the sweep green."""
    out: dict[str, str] = {}
    pending = sorted({n for src in files.values() for n in _service_names(src)})
    while pending:
        name = pending.pop()
        rel = f"services/{name}.py"
        if rel in out:
            continue
        if read is not None:
            source = read.get(name)
        else:
            path = _ATHENA / "services" / f"{name}.py"
            source = path.read_text(encoding="utf-8") if path.exists() else None
        if source is None:
            continue
        out[rel] = source
        pending.extend(sorted(_service_names(source) - {
            r.split("/", 1)[1][:-3] for r in out}))
    return out


def test_no_connector_module_breaks_a_never():
    sources = _sources()
    reached = services_reached(sources)
    assert violations(
        {**sources, **reached},
        tool_code=disclosure.TOOL_CODE_MODULES | set(reached),
    ) == []


def test_the_sweep_follows_the_services_the_tools_reach():
    """Non-vacuous: the protocol service is swept, and a forbidden call
    planted in a service a connector module imports — in each import
    form — is reported, including a bare ``.delete()`` (tool code)."""
    assert "services/protocoles.py" in services_reached(_sources())
    planted = {"relay": "from services import deep as d\n",
               "deep": "note_model.delete_note(n)\nref.delete()\n"}
    for form in ("from services import relay as r\n",
                 "from services.relay import something\n",
                 "import services.relay\n"):
        files = {"mcp/probe.py": form}
        reached = services_reached(files, read=planted)
        assert set(reached) == {"services/relay.py", "services/deep.py"}
        found = violations({**files, **reached},
                           tool_code=set(reached))
        assert any(v.startswith("services/deep.py: delete forbids "
                                "delete_note") for v in found), found
        assert any(v == "services/deep.py: delete forbids delete"
                   for v in found), found


def test_the_sweep_reads_every_connector_module_but_the_registry():
    swept = set(_swept_files())
    assert {"mcp/handlers.py", "mcp/tools.py", "mcp/endpoint.py",
            "mcp/write_support.py"} <= swept
    assert "mcp/disclosure.py" not in swept
    assert disclosure.SWEEP_EXCLUDED == {"mcp/disclosure.py"}
    assert disclosure.TOOL_CODE_MODULES <= swept


def test_the_sweep_ignores_strings_and_sees_every_reference_form():
    """The detector itself: prose never counts, and no evasive form escapes."""
    prose = {"mcp/probe.py": (
        '"""record_payment(x) — void_invoice, delete_task."""\n'
        '# update_kyc_status(\n'
        'MESSAGE = "toggle_task_complete create_transaction"\n'
    )}
    assert violations(prose) == []
    evasive = {
        "mcp/probe_a.py": "from models.invoice import record_payment as rp\n",
        "mcp/probe_b.py": "import services.encaissements as enc\n",
        "mcp/probe_c.py": "from services import encaissements\n",
        "mcp/probe_d.py": "cb = task_model.toggle_task_complete\n",
        "mcp/probe_e.py": "getattr(invoice_model, 'void_invoice')(i)\n",
        "mcp/probe_f.py": "note_model.delete_note(n)\n",
        "mcp/probe_g.py": "invoice_model.create_invoice(d, [], [], {})\n",
        "mcp/probe_h.py": "from models import admin_ledger\n",
    }
    found = violations(evasive)
    for rel in evasive:
        assert any(v.startswith(rel) for v in found), (rel, found)


def test_each_forbidden_pattern_is_caught_on_a_planted_call():
    """Non-vacuous per entry: a planted reference to an identifier each
    pattern matches is reported, under that entry's key."""
    for never in disclosure.NEVERS:
        for pattern in never.forbidden:
            ident = pattern.replace(r"\w+", "x")
            assert re.fullmatch(pattern, ident), pattern
            found = violations({"mcp/probe.py": f"model.{ident}(1)\n"})
            assert any(f"{never.key} forbids {ident}" in v for v in found), (pattern, found)
        for pattern in never.forbidden_in_tool_code:
            ident = pattern.replace(r"\w+", "x")
            planted = f"ref.{ident}()\n"
            assert any(never.key in v for v in violations({"mcp/handlers.py": planted}))
            # ... and only there: the protocol layer deletes its bookkeeping.
            assert violations({"mcp/store.py": planted}) == []


def test_the_required_keyword_sweep_has_something_to_check():
    """Vacuity check: the one create_invoice call the connector makes is
    found — and it names its number."""
    tree = ast.parse(_sources()["mcp/handlers.py"])
    for never in disclosure.NEVERS:
        for call, keyword in never.required_keywords:
            assert _calls_to(tree, call) >= 1, call
            assert _calls_missing(tree, call, keyword) == []


def test_the_forbidden_inputs_are_declared_by_no_write_tool():
    for never in disclosure.NEVERS:
        for tool, prop in never.forbidden_inputs:
            targets = tools.WRITE_TOOLS if tool == "*" else {tool}
            assert targets <= set(tools.TOOLS), (never.key, tool)
            for name in targets:
                props = tools.TOOLS[name]["input_schema"].get("properties", {})
                assert prop not in props, (never.key, name, prop)


def test_every_behavioural_test_exists():
    for never in disclosure.NEVERS:
        if not never.behavioural_test:
            continue
        path, _, name = never.behavioural_test.partition("::")
        tree = ast.parse((_ATHENA / path).read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert name in defs, never.behavioural_test


def test_every_never_is_backed_by_a_mechanism_and_speaks_both_languages():
    keys = [n.key for n in disclosure.NEVERS]
    assert len(keys) == len(set(keys))
    for never in disclosure.NEVERS:
        assert never.fr.strip() and never.en.strip(), never.key
        assert (
            never.forbidden or never.forbidden_modules
            or never.forbidden_in_tool_code or never.required_keywords
            or never.forbidden_inputs or never.behavioural_test
        ), f"{never.key}: a promise nothing enforces"
        # No trailing punctuation: the template adds « ; » / « . ».
        assert not never.fr.rstrip().endswith((";", ".", "&nbsp;;")), never.key
        assert never.en in endpoint.INSTRUCTIONS, never.key


def test_the_registry_is_pure():
    """It may import the scope constants and markupsafe — never a model or
    a service. (mcp.tools derives WRITE_TOOLS from it at import, so a model
    import here would pull Firestore into every tools import.)"""
    tree = ast.parse((_ATHENA / "mcp" / "disclosure.py").read_text(encoding="utf-8"))
    # EVERY import, the lazy ones inside functions included: a model
    # imported lazily in a builder would still run Firestore at the first
    # consent render. (The lazy `from mcp import tools` is the one allowed.)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__", "dataclasses", "typing", "markupsafe", "mcp"}, imported


def test_the_registry_names_forbidden_calls_only_as_strings():
    """The raw-text sweep of test_invoice_detail counts any file containing
    the payment writer's name followed by a parenthesis; the registry must
    never carry that text — nor any other connector module."""
    needle = "record_payment" + "("
    for rel, source in _sources().items():
        assert needle not in source, rel


# ══════════════════════════════════════════════════════════════════════
# 3. No known false claim, on any text surface the connector emits
# ══════════════════════════════════════════════════════════════════════


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the docstring constants — prose for maintainers, never emitted
    to a client (and the registry's own docstring RECOUNTS the false claims
    it replaced, which is the point of it)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _connector_strings() -> list[tuple[str, str]]:
    """Every string constant a connector module can EMIT — implicit
    concatenation already merged by the parser, so a claim split across
    source lines is still one string here. Docstrings excluded."""
    out = []
    for rel, source in _sources().items():
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                out.append((rel, node.value))
    return out


def _false_claims_in(text: str) -> list[str]:
    lowered = text.lower()
    hits = [c for c in KNOWN_FALSE_CLAIMS if c in lowered]
    hits += [p for p in KNOWN_FALSE_PATTERNS if re.search(p, text, re.IGNORECASE)]
    return hits


def test_no_known_false_claim_in_the_connector_texts():
    offenders = [
        (rel, hits, value[:100]) for rel, value in _connector_strings()
        if (hits := _false_claims_in(value))
    ]
    assert offenders == [], offenders
    assert _false_claims_in(endpoint.INSTRUCTIONS) == []
    for name, spec in tools.TOOLS.items():
        assert _false_claims_in(spec["description"]) == [], name


def test_the_false_claim_detector_is_not_vacuous():
    for claim in KNOWN_FALSE_CLAIMS:
        assert _false_claims_in(f"xx {claim.upper()} yy")
    assert _false_claims_in("horodatée et signée «&nbsp;Claude&nbsp;»")
    assert not _false_claims_in("aucune signification consignée")
    assert _false_claims_in("le connecteur ne peut plus les modifier. Pour")
    assert not _false_claims_in("le connecteur ne peut plus les modifier, sauf leur phase")
    assert _false_claims_in("nothing here can modify them afterwards, and")
    assert not _false_claims_in("nothing here can modify them afterwards except")
    assert _false_claims_in("Le connecteur ne modifie jamais une entrée facturée")
    assert _false_claims_in("ni l'application ne modifient une entrée facturée")
    assert not _false_claims_in("Le connecteur ne corrige jamais une entrée facturée")
    assert _false_claims_in("once invoiced nothing here can touch it.")
    assert _false_claims_in(
        "neither this connector nor the application can modify it, and")


def test_complete_task_describes_the_refusal_it_now_makes():
    desc = tools.TOOLS["complete_task"]["description"]
    assert "never reopens a closed task" in desc
    assert "REFUSED" in desc
    assert "complete_task" in tools.EDIT_TOOLS
