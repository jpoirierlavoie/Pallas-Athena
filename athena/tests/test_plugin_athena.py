"""The claude.ai plugin « athena » (skill 1.0.0) — the deploy gate.

``scripts/exporter_plugin_athena.py`` builds the plugin from its handwritten
source (``plugin/athena/`` at the repo root) and the tool registry.
This module builds it in memory and in a temp dir, and checks the SHIPPED
files — what Claude reads — against the registry:

* every backticked tool-like name exists in ``TOOLS``, and every
  ``tool(param=…)`` names real input properties of that tool, with values
  its schema accepts (enum members, booleans, integer bounds);
* every other straight-quoted value in the skill is an enum value some
  schema declares (input or output);
* the frontmatter is exactly ``name`` + ``description`` (≤ 1 024
  characters, no ``<``/``>``), the byte and line budgets hold, and the
  kernel ends before byte 3 500;
* no ``{{GEN:…}}`` is left, none of the retired strings is back
  (``mcp__``, the draft tools, tool counts), ``dry_run`` stands only on
  its Noyau line, ``.mcp.json`` names the connector « Athena »;
* every recipe carries its fields; the accounting tools are named only in
  ``comptabilite.md`` and in the index's grant section; the index lists
  every tool exactly once;
* the build is deterministic, ``--check`` sees drift, and the exporter
  never imports ``models``.

It fails when a citation goes stale (a tool or parameter renamed, an enum
value removed) or a budget breaks. A registry change that alters only the
generated parts — the index, the version line — leaves it green:
``--check`` is what says the uploaded archive is out of date.
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from mcp import disclosure  # noqa: E402
from mcp import tools  # noqa: E402
from mcp.output_schemas import OUTPUT_SCHEMAS  # noqa: E402
from scripts import exporter_plugin_athena as ex  # noqa: E402

_ATHENA = pathlib.Path(__file__).resolve().parent.parent

_SPAN = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"([a-z][a-z0-9_]*)\((.*)\)", re.S)
_LEAD = re.compile(r"([a-z][a-z0-9_]*)(?=\(|$)")
_QUOTED = re.compile(r'"([^"\n]*)"')

#: Retired strings — never in a file the model reads. ``mcp__`` was the
#: wrong prefix of 1.3.0 (the real one is ``mcp__claude_ai_…``, and Claude
#: Code shows it anyway); the draft tools left with the internal chat;
#: qclaw_/canlii_ are other connectors' prefixes.
_BANNED = ("mcp__", "save_draft", "get_draft", "revise_draft", "qclaw_", "canlii_")
#: A tool COUNT (« 49 outils », « 82 tools ») goes stale with the registry.
_COUNT = re.compile(r"\b\d+\s+(?:outils|tools)\b", re.I)


@pytest.fixture(scope="module")
def shipped() -> dict:
    return ex.construire()


def _skill_texts(shipped: dict) -> dict:
    """The files the MODEL reads (the README is for the lawyer)."""
    return {p: b.decode("utf-8") for p, b in shipped.items()
            if p.startswith("skills/")}


def _md_texts(shipped: dict) -> dict:
    return {p: b.decode("utf-8") for p, b in shipped.items() if p.endswith(".md")}


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.index("\n---\n", 4)
        return text[end + 5:]
    return text


def _walk_props(props: dict):
    """Every property schema, nested objects and array items included."""
    for name, schema in props.items():
        yield name, schema
        if isinstance(schema, dict):
            if isinstance(schema.get("properties"), dict):
                yield from _walk_props(schema["properties"])
            items = schema.get("items")
            if isinstance(items, dict) and isinstance(items.get("properties"), dict):
                yield from _walk_props(items["properties"])


def _all_property_names() -> set:
    """Names that are FIELDS, not tools: every input property, and every
    property of every output schema."""
    names = set()
    for spec in tools.TOOLS.values():
        names |= {n for n, _ in _walk_props(spec["input_schema"].get("properties", {}))}

    def out(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                names.update(node["properties"])
            for value in node.values():
                out(value)
        elif isinstance(node, list):
            for value in node:
                out(value)

    out(OUTPUT_SCHEMAS)
    return names


def _enum_values(node, acc: set) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            acc.update(str(v) for v in node["enum"])
        if "const" in node:
            acc.add(str(node["const"]))
        for value in node.values():
            _enum_values(value, acc)
    elif isinstance(node, list):
        for value in node:
            _enum_values(value, acc)


def _enums_by_param() -> dict:
    by = {}
    for spec in tools.TOOLS.values():
        for name, schema in _walk_props(spec["input_schema"].get("properties", {})):
            values = set()
            if isinstance(schema, dict):
                if isinstance(schema.get("enum"), list):
                    values |= {str(v) for v in schema["enum"]}
                items = schema.get("items")
                if isinstance(items, dict) and isinstance(items.get("enum"), list):
                    values |= {str(v) for v in items["enum"]}
            if values:
                by.setdefault(name, set()).update(values)
    return by


# Pinned, then widened by the live registry: a verb only ONE tool uses
# (``reopen``, ``decide``…) must still read as a tool name once that tool is
# renamed, or the stale citation would pass as plain text.
_PINNED_VERBS = frozenset({
    "add", "append", "begin", "clear", "complete", "compute", "create",
    "decide", "edit", "fill", "finalize", "find", "get", "import", "list",
    "manage", "move", "parse", "preview", "record", "reopen", "reverse",
    "set", "update",
})
_TOOL_VERBS = _PINNED_VERBS | {name.split("_", 1)[0] for name in tools.TOOLS}
_FIELDS = _all_property_names()


def _tool_like(ident: str) -> bool:
    """A name that reads as a tool: a registry verb prefix, and not a field
    (``add_opposing_parties`` is an input of update_dossier)."""
    return ("_" in ident and ident.split("_", 1)[0] in _TOOL_VERBS
            and (ident in tools.TOOLS or ident not in _FIELDS))


def _split_args(text: str) -> list:
    """Split call arguments on top-level commas (brackets and quotes kept)."""
    args, depth, quoted, cur = [], 0, False, ""
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "[(":
            depth += 1
        elif not quoted and ch in "])":
            depth -= 1
        if ch == "," and depth == 0 and not quoted:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _calls(texts: dict):
    """``(path, span, tool, args)`` of every backticked ``tool(…)``."""
    for path, text in texts.items():
        for span in _SPAN.findall(text):
            m = _CALL.fullmatch(span)
            if m and m.group(1) in tools.TOOLS:
                yield path, span, m.group(1), _split_args(m.group(2))


def _is_ellipsis(value: str) -> bool:
    return value in ("…", "...", "[…]")


def _check_value(prop: dict, value: str) -> str:
    """'' when *value* (literal source text) is acceptable for *prop*."""
    value = value.strip()
    if _is_ellipsis(value) or re.fullmatch(r"[a-z][a-z0-9_]*", value):
        return ""                      # elided, or a reference to a field
    if value in ("true", "false"):
        return "" if prop.get("type") == "boolean" else "boolean given to a non-boolean"
    if re.fullmatch(r"-?\d+", value):
        if prop.get("type") != "integer":
            return "integer given to a non-integer"
        n = int(value)
        if "minimum" in prop and n < prop["minimum"]:
            return f"below minimum {prop['minimum']}"
        if "maximum" in prop and n > prop["maximum"]:
            return f"above maximum {prop['maximum']}"
        return ""
    strings = _QUOTED.findall(value)
    if strings:
        allowed = prop.get("enum")
        items = prop.get("items")
        if allowed is None and isinstance(items, dict):
            allowed = items.get("enum")
        if allowed is not None:
            bad = [s for s in strings if s not in allowed]
            return f"not in enum: {bad}" if bad else ""
        return ""
    return ""


# ── Layout and packaging ────────────────────────────────────────────────

def test_layout_is_exactly_the_manifest(shipped):
    assert sorted(shipped) == sorted(ex.SOURCES + ex.GENERES)


def test_nothing_blocks_delivery(shipped):
    """Byte and line budgets, leftover placeholders, the kernel's end,
    .mcp.json — the exporter's own refusal list is empty."""
    assert ex.problemes(shipped) == []


def test_mcp_json_names_the_athena_connector(shipped):
    assert shipped[ex.MCP_JSON] == ex.MCP_JSON_ATTENDU
    assert json.loads(shipped[ex.MCP_JSON]) == {"mcpServers": {"Athena": {}}}


def test_what_claude_reads_never_says_pallas(shipped):
    """The connector is « Athena » in every file Claude reads; only the
    README, written for the lawyer, names the old ``athena`` plugin."""
    for path, data in shipped.items():
        if path == "README.md":
            continue
        text = data.decode("utf-8")
        assert "pallas" not in text.lower(), path
        assert "Athéna" not in text, path


def test_plugin_json_carries_the_version(shipped):
    data = json.loads(shipped[ex.PLUGIN_JSON])
    assert data["name"] == ex.NOM
    assert data["version"] == ex.VERSION
    assert list(data)[:2] == ["name", "version"]
    assert ex.nom_archive() == f"athena-{ex.VERSION}.plugin"


def test_every_budget_names_a_shipped_file():
    assert set(ex.BUDGETS) <= set(ex.SOURCES + ex.GENERES)


def test_build_is_deterministic(tmp_path):
    first, second = tmp_path / "a.plugin", tmp_path / "b.plugin"
    assert ex.main(["--sortie", str(first)]) == 0
    assert ex.main(["--sortie", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    assert ex.lire_archive(first.read_bytes()) == ex.construire()


def test_archive_has_v1_layout_and_no_directories(tmp_path):
    out = tmp_path / ex.nom_archive()
    assert ex.main(["--sortie", str(out)]) == 0
    import zipfile
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert zf.testzip() is None
    assert names == sorted(names)
    assert not any(n.endswith("/") or "\\" in n for n in names)
    assert names[:3] == [".claude-plugin/plugin.json", ".mcp.json", "README.md"]


def test_check_sees_drift(tmp_path, capsys):
    out = tmp_path / ex.nom_archive()
    assert ex.main(["--check", "--sortie", str(out)]) == 1      # nothing yet
    assert ex.main(["--sortie", str(out)]) == 0
    assert ex.main(["--check", "--sortie", str(out)]) == 0
    stale = dict(ex.construire())
    stale[ex.SKILL] = stale[ex.SKILL] + b"\n"
    out.write_bytes(ex.assembler(stale))
    assert ex.main(["--check", "--sortie", str(out)]) == 1
    assert ex.SKILL in capsys.readouterr().out


def test_source_is_line_ending_agnostic(tmp_path):
    """A Windows checkout (core.autocrlf) must build the same plugin."""
    import shutil
    copy = tmp_path / "src"
    shutil.copytree(ex.SOURCE, copy)
    for path in copy.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert ex.construire(copy) == ex.construire()


def test_a_stray_source_file_is_refused(tmp_path):
    import shutil
    copy = tmp_path / "src"
    shutil.copytree(ex.SOURCE, copy)
    (copy / "skills" / "athena" / "notes.md").write_text("x", encoding="utf-8")
    with pytest.raises(ex.ErreurDeConstruction):
        ex.construire(copy)


def test_exporter_never_imports_models():
    tree = ast.parse((_ATHENA / "scripts" / "exporter_plugin_athena.py")
                     .read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m == "models" or m.startswith("models.") for m in imported)
    # …nor transitively: build in a fresh interpreter and look.
    code = (
        "import sys\n"
        "from scripts import exporter_plugin_athena as ex\n"
        "ex.construire()\n"
        "print(sorted(m for m in sys.modules if m == 'models' or m.startswith(("
        "'models.', 'google.cloud.firestore', 'firebase_admin',"
        " 'google.cloud.secretmanager'))))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ENV"}
    result = subprocess.run([sys.executable, "-c", code], cwd=_ATHENA, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_exporter_refuses_production(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setattr(ex, "_REGISTRE", None)
    with pytest.raises(ex.ErreurDeConstruction):
        ex.construire()


# ── Frontmatter and budgets ─────────────────────────────────────────────

def test_frontmatter_is_exactly_name_and_description(shipped):
    text = shipped[ex.SKILL].decode("utf-8")
    assert text.startswith("---\n")
    head = text[4:text.index("\n---\n", 4)]
    keys = {}
    for line in head.split("\n"):
        key, _, value = line.partition(": ")
        keys[key] = value
    assert list(keys) == ["name", "description"]
    assert keys["name"] == ex.NOM
    value = keys["description"]
    assert value.startswith('"') and value.endswith('"')
    description = value[1:-1]
    assert '"' not in description
    assert 0 < len(description) <= ex.DESCRIPTION_MAX
    assert "<" not in description and ">" not in description


def test_kernel_ends_before_its_ceiling(shipped):
    raw = shipped[ex.SKILL]
    end = raw.index(("\n" + ex.FIN_DU_NOYAU + "\n").encode("utf-8")) + 1
    assert end < ex.FIN_DU_NOYAU_MAX
    before = raw[:end].decode("utf-8")
    assert "\n## Noyau\n" in before
    assert "\n## Conventions" in before


def test_version_line_follows_the_frontmatter(shipped):
    body = _strip_frontmatter(shipped[ex.SKILL].decode("utf-8"))
    first = next(line for line in body.split("\n") if line.strip())
    assert first == ex.ligne_version()
    assert re.fullmatch(rf"Compétence {re.escape(ex.VERSION)} — registre [0-9a-f]{{12}}", first)


# ── Names, parameters, values ───────────────────────────────────────────

def test_every_tool_like_name_is_a_tool(shipped):
    unknown = []
    for path, text in _md_texts(shipped).items():
        for span in _SPAN.findall(text):
            m = _LEAD.match(span)
            if m and _tool_like(m.group(1)) and m.group(1) not in tools.TOOLS:
                unknown.append((path, span))
    assert unknown == []


def test_every_call_names_real_parameters_with_valid_values(shipped):
    problems = []
    for path, span, tool, args in _calls(_md_texts(shipped)):
        props = tools.TOOLS[tool]["input_schema"].get("properties", {})
        for arg in args:
            if _is_ellipsis(arg):
                continue
            name, eq, value = arg.partition("=")
            name = name.strip()
            if name not in props:
                problems.append((path, span, f"{name}: no such input"))
            elif eq:
                why = _check_value(props[name], value)
                if why:
                    problems.append((path, span, f"{name}: {why}"))
    assert problems == []


def test_standalone_param_values_are_enum_members(shipped):
    """```action="refuser"``` outside a call: some tool's ``action`` enum."""
    by_param = _enums_by_param()
    problems = []
    for path, text in _skill_texts(shipped).items():
        for span in _SPAN.findall(text):
            m = re.fullmatch(r"([a-z][a-z0-9_]*)=\"([^\"]*)\"", span)
            if m and m.group(2) not in by_param.get(m.group(1), set()):
                problems.append((path, span))
    assert problems == []


def test_every_quoted_value_is_a_declared_enum_value(shipped):
    """Straight quotes mark literal values in the skill (French prose uses
    « »): each must be an enum value some input or output schema declares."""
    declared = set()
    for spec in tools.TOOLS.values():
        _enum_values(spec["input_schema"], declared)
    _enum_values(OUTPUT_SCHEMAS, declared)
    unknown = []
    for path, text in _skill_texts(shipped).items():
        for value in _QUOTED.findall(_strip_frontmatter(text)):
            if value not in declared:
                unknown.append((path, value))
    assert unknown == []


def test_linked_files_exist(shipped):
    missing = []
    for path, text in _skill_texts(shipped).items():
        here = pathlib.PurePosixPath(path).parent
        for span in _SPAN.findall(text):
            if span.endswith(".md") and " " not in span:
                own = str(here / span)
                root = f"skills/athena/{span}"
                if own not in shipped and root not in shipped:
                    missing.append((path, span))
    assert missing == []


# ── Retired content ─────────────────────────────────────────────────────

def test_no_retired_string_in_the_skill(shipped):
    found = []
    for path, text in _skill_texts(shipped).items():
        found += [(path, s) for s in _BANNED if s in text]
        found += [(path, m.group(0)) for m in _COUNT.finditer(text)]
    assert found == []


def test_dry_run_only_on_its_noyau_line(shipped):
    texts = _skill_texts(shipped)
    for path, text in texts.items():
        if path != ex.SKILL:
            assert "dry_run" not in text, path
    lines = texts[ex.SKILL].split("\n")
    hits = [i for i, line in enumerate(lines) if "dry_run" in line]
    assert len(hits) == 1
    noyau = lines.index("## Noyau")
    end = next(i for i in range(noyau + 1, len(lines)) if lines[i].startswith("## "))
    assert noyau < hits[0] < end


# ── Recipes and generated blocks ────────────────────────────────────────

_FIELDS_OF_A_RECIPE = ("Charger", "Déclencheur", "Appels", "Arrêt", "À éviter")


def _recipes(shipped: dict):
    """``(path, heading, lines)``: the SKILL's ``###`` recipes, and every
    ``##`` section of the recipe files."""
    texts = _skill_texts(shipped)
    for path, text in texts.items():
        if path == ex.SKILL:
            level = "### "
        elif path in ex.RECETTES:
            level = "## "
        else:
            continue
        lines = text.split("\n")
        starts = [i for i, line in enumerate(lines) if line.startswith(level)]
        for i in starts:
            end = next((j for j in range(i + 1, len(lines))
                        if lines[j].startswith("#")), len(lines))
            yield path, lines[i], lines[i + 1:end]


def test_every_recipe_carries_its_fields(shipped):
    problems = []
    count = 0
    for path, heading, body in _recipes(shipped):
        count += 1
        fields = [m.group(1) for m in (re.match(r"- \*\*([^*]+)\*\*", line)
                                       for line in body) if m]
        if fields[:1] != ["Charger"]:
            problems.append((path, heading, "Charger must come first"))
        for field in _FIELDS_OF_A_RECIPE:
            if field not in fields:
                problems.append((path, heading, f"no {field}"))
    assert problems == []
    assert count >= 4 + len(ex.RECETTES)


def test_charger_lines_are_the_recipe_calls(shipped):
    """A Charger line lists exactly the tools the recipe's Déclencheur and
    Appels lines name, plus those of a sister recipe they cite (« D1 pour
    les parties ») — never one only its Arrêt or À éviter line names, which
    are the calls to AVOID."""
    by_file = {}
    for path, heading, body in _recipes(shipped):
        called = " ".join(line for line in body
                          if line.startswith(("- **Appels**", "- **Déclencheur**")))
        own = []
        for span in _SPAN.findall(called):
            m = _LEAD.match(span)
            if m and m.group(1) in tools.TOOLS and m.group(1) not in own:
                own.append(m.group(1))
        charger = next(line for line in body if line.startswith("- **Charger**"))
        rid = re.match(r"#+ ((?:Doc|[RFDNA])\d+)\b", heading)
        by_file.setdefault(path, []).append(
            (heading, rid.group(1) if rid else "", own, called,
             re.findall(r"`([^`]+)`", charger), charger))
    problems = []
    for path, recipes in by_file.items():
        own_of = {rid: own for _, rid, own, _, _, _ in recipes if rid}
        for heading, rid, own, called, names, charger in recipes:
            if "Comptabilité" in charger:
                # The accounting grant's own line: generated from the
                # registry, and its tools appear in no Appels on purpose.
                assert path == ex.COMPTABILITE
                assert names == ex.outils_comptables()
                continue
            allowed = list(own)
            for other, tools_of in own_of.items():
                if other != rid and re.search(rf"(?<![\w-]){other}(?![\w-])", called):
                    allowed += [t for t in tools_of if t not in allowed]
            if not names:
                problems.append((path, heading, "empty"))
            if names != allowed:
                problems.append((path, heading, names, allowed))
    assert problems == []


def test_pinned_verbs_cover_the_registry():
    """A new verb joins the pin, so a later rename of its tool is caught."""
    assert {name.split("_", 1)[0] for name in tools.TOOLS} <= _PINNED_VERBS


def test_seule_application_is_the_registry_off_core(shipped):
    """Every off-core promise is classified (a new one fails the build), the
    selected ones are shipped, and the consent screen's deixis is gone."""
    text = shipped[ex.SKILL].decode("utf-8")
    off_core = {n.key for n in disclosure.general_nevers() if not n.in_core}
    assert off_core == set(ex.SEULE_APPLICATION) | set(ex.HORS_SEULE_APPLICATION)
    lines = ex.bloc_seule_application()
    assert len(lines) == len(ex.SEULE_APPLICATION)
    for line in lines:
        assert f"{line}\n" in text
    for never in disclosure.general_nevers():
        if never.in_core:
            assert ex._texte_sans_balisage(never.fr) not in text
    assert "<strong>" not in text and "&nbsp;" not in text
    for deixis in ("ci-dessus", "vous seul", "écran de consentement"):
        assert deixis not in text


def test_accounting_tools_only_in_their_two_places(shipped):
    accounting = sorted(tools.ACCOUNTING_TOOLS)
    assert accounting
    pattern = re.compile(r"(?<![a-z0-9_])(" + "|".join(accounting) + r")(?![a-z0-9_])")
    stray = []
    for path, text in _md_texts(shipped).items():
        if path == ex.COMPTABILITE:
            continue
        if path == ex.INDEX:
            head, _, _ = text.partition("\n## Grant Comptabilité\n")
            stray += [(path, m.group(1)) for m in pattern.finditer(head)]
            continue
        stray += [(path, m.group(1)) for m in pattern.finditer(text)]
    assert stray == []


def test_index_lists_every_tool_exactly_once(shipped):
    text = shipped[ex.INDEX].decode("utf-8")
    listed = [m.group(1) for m in re.finditer(r"`([a-z][a-z0-9_]*)[(`]", text)
              if m.group(1) in tools.TOOLS]
    assert sorted(listed) == sorted(tools.TOOLS)
    head, _, grant = text.partition("\n## Grant Comptabilité\n")
    assert grant
    for name in tools.ACCOUNTING_TOOLS:
        assert f"`{name}" in grant
    for family in disclosure.families_for(disclosure.SCOPE_WRITE):
        assert f"**{family.label}**" in head


def test_index_flags_match_the_registry(shipped):
    text = shipped[ex.INDEX].decode("utf-8")
    for name in tools.TOOLS:
        m = re.search(rf"`{name}(?:\([^`]*\))?`([^`·\n]*)", text)
        assert m, name
        flags = m.group(1).split()
        assert ("R" in flags) == (name in tools.EDIT_TOOLS), name
        required_key = (name in tools.WRITE_TOOLS and tools.idempotency_policy(name)
                        == tools.IDEMPOTENCY_REQUIRED)
        assert ("C" in flags) == required_key, name


def test_reprise_limits_are_real_divergences(shipped):
    text = shipped[ex.REPRISE].decode("utf-8")
    rows = [line for line in text.split("\n") if re.match(r"- `[a-z_]+` : \d", line)]
    assert rows
    for row in rows:
        prop = re.match(r"- `([a-z_]+)`", row).group(1)
        values = {v for _, _, v in ex._plafonds_par_propriete()[prop]}
        assert len(values) > 1, prop


# ── Refusals of the builder itself ──────────────────────────────────────

def test_placeholder_must_stand_alone_and_be_known():
    with pytest.raises(ex.ErreurDeConstruction):
        ex.remplir("x.md", "# T\ntexte {{GEN:version}} ici\n")
    with pytest.raises(ex.ErreurDeConstruction):
        ex.remplir("x.md", "# T\n{{GEN:inconnu}}\n")


def test_a_recipe_naming_no_tool_is_refused():
    source = "## A1 Vide\n{{GEN:charger}}\n- **Appels** : rien à appeler.\n"
    with pytest.raises(ex.ErreurDeConstruction):
        ex.remplir("x.md", source)


def test_charger_ignores_the_calls_to_avoid():
    source = (
        "## A1 Essai\n{{GEN:charger}}\n"
        "- **Déclencheur** : « essai ».\n"
        "- **Appels** : (1) `get_agenda(days_ahead=7)`.\n"
        "- **Arrêt** : pas de `list_tasks`.\n"
        "- **À éviter** : `get_dossier` par élément.\n"
    )
    out = ex.remplir("x.md", source)
    assert "- **Charger** : `get_agenda`\n" in out


def test_version_digest_tracks_the_registry(monkeypatch):
    before = ex.empreinte_registre()
    patched = dict(tools.TOOLS)
    patched["zz_probe"] = {"input_schema": {"type": "object"}}
    monkeypatch.setattr(tools, "TOOLS", patched)
    assert ex.empreinte_registre() != before
