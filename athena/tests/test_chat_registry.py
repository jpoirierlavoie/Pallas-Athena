"""chat/registry.py + chat/executors.py + chat/worker_client.py +
chat/charter.py (Phase N) — the pure chat-side tool layer.

Import preamble mirrors tests/test_mcp_tools.py: env vars first (config.py
resolves at import), then the chat modules. No Firestore is touched — the
in-process handler resolution is monkeypatched at ``mcp.tools.get_handler``.
"""

import json
import os
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
import mcp.tools as mcp_tools  # noqa: E402
from chat import charter, executors, registry, worker_client  # noqa: E402
from chat.worker_tools import WORKER_NAME_PREFIXES, WORKER_TOOLS  # noqa: E402


# ── Registry pins ───────────────────────────────────────────────────────────

def test_write_parity_is_derived_not_copied():
    # D9 (2026-08-26): la POLITIQUE reste dérivée, donc elle ne peut pas
    # dériver. Ce que le chat EXPOSE est une autre question — voir
    # test_the_chat_withholds_exactly_the_named_tools.
    assert registry.CHAT_WRITE_TOOLS == mcp_tools.WRITE_TOOLS


def test_the_chat_withholds_exactly_the_named_tools():
    """L'écart avec le connecteur, épinglé nom par nom.

    Une liste tenue à la main dérive : un outil renommé dans TOOLS y
    resterait sous son ancien nom, silencieusement réexposé. D'où la
    première assertion — chaque nom exclu doit EXISTER.
    """
    inconnus = registry.CHAT_EXCLUDED_TOOLS - set(mcp_tools.TOOLS)
    assert not inconnus, f"noms exclus qui n'existent plus : {sorted(inconnus)}"

    exposes = set(registry.chat_tool_names(include_writes=True))
    assert exposes == set(mcp_tools.TOOLS) - registry.CHAT_EXCLUDED_TOOLS
    # L'écart d'écriture assumé : le chat ne crée plus ni dossier ni
    # contact, le connecteur le peut toujours.
    retenues = registry.CHAT_EXCLUDED_TOOLS & set(mcp_tools.WRITE_TOOLS)
    assert "create_dossier" in retenues and "create_partie" in retenues

    # Et ce qui fait le travail de conversation reste offert.
    for indispensable in ("create_note", "append_to_note", "create_task",
                          "complete_task", "create_hearing", "save_draft",
                          "revise_draft", "get_document_text", "get_dossier"):
        assert indispensable in exposes, indispensable


def test_the_exclusion_never_touches_the_connector():
    """Le connecteur externe garde ses 52 outils : c'est là que la reprise
    de données se fait, et elle en a besoin."""
    assert registry.CHAT_EXCLUDED_TOOLS <= set(mcp_tools.TOOLS)
    assert len(mcp_tools.TOOLS) == 53


def test_gated_set_is_empty_in_v1():
    # FLAG 3 — mechanism implemented, policy empty. Widening it is a
    # deliberate one-name edit, and this pin makes it a conscious one.
    assert registry.GATED_TOOLS == frozenset()


def test_internal_schemas_are_referenced_by_identity():
    tools = registry.anthropic_tools(include_writes=True)
    by_name = {t["name"]: t for t in tools if "input_schema" in t}
    for name, entry in by_name.items():
        if name in mcp_tools.TOOLS:
            assert entry["input_schema"] is mcp_tools.TOOLS[name]["input_schema"]


def test_toolset_is_tools_plus_workers_plus_web_search():
    names = [t["name"] for t in registry.anthropic_tools(include_writes=True)]
    worker_names = {spec["name"] for spec in WORKER_TOOLS}
    for name in names:
        assert (
            name in mcp_tools.TOOLS
            or name in worker_names
            or name == registry.GET_SKILL_FILE_NAME
            or name == registry.WEB_SEARCH_NAME
        ), name
    # web_search stays LAST (the trailing cache_control breakpoint lands on
    # it); get_skill_file sits just before, after the workers.
    assert names[-1] == registry.WEB_SEARCH_NAME
    assert names[-2] == registry.GET_SKILL_FILE_NAME


def test_excluding_writes_removes_exactly_the_write_tools():
    """Le coupe-circuit retire les écritures OFFERTES, ni plus ni moins.

    Depuis CHAT_EXCLUDED_TOOLS (2026-08-27) une partie des écritures
    n'est jamais offerte : le coupe-circuit ne peut pas retirer ce qui
    n'est pas là, et l'écart attendu se mesure sur le reste. La
    soustraction est ce qui garde le test exact plutôt que permissif.
    """
    with_writes = {t["name"] for t in registry.anthropic_tools(include_writes=True)}
    without = {t["name"] for t in registry.anthropic_tools(include_writes=False)}
    attendu = set(mcp_tools.WRITE_TOOLS) - registry.CHAT_EXCLUDED_TOOLS
    assert with_writes - without == attendu
    assert attendu, "le coupe-circuit ne garderait plus rien à retirer"


def test_web_search_entry_is_the_basic_vertex_version():
    entry = registry.anthropic_tools()[-1]
    # Basic web search only on Vertex (no dynamic filtering) — verified fact.
    assert entry["type"] == "web_search_20250305"
    assert entry["name"] == "web_search"
    assert entry["max_uses"] == Config.CHAT_WEB_SEARCH_MAX_USES


def test_worker_names_are_namespaced_and_disjoint_from_tools():
    for spec in WORKER_TOOLS:
        assert re.match(r"^(legislation|jurisprudence)_", spec["name"]), spec
        assert spec["name"] not in mcp_tools.TOOLS
        assert spec["worker"] in ("legislation", "jurisprudence")
    assert WORKER_NAME_PREFIXES == ("legislation_", "jurisprudence_")


def test_no_delete_capability_exists_anywhere():
    # SPEC §13.5 — the registry half of the static sweep (routes/templates
    # get their own in the UI lot). No tool NAME carries a delete verb, and
    # the module sources never register one.
    delete_verbs = re.compile(r"^(delete|remove|supprimer|effacer|purge)_")
    for name in list(mcp_tools.TOOLS) + [s["name"] for s in WORKER_TOOLS]:
        assert not delete_verbs.match(name), name
    for module in (registry, executors):
        source_path = module.__file__
        with open(source_path, encoding="utf-8") as fh:
            source = fh.read()
        assert "def delete" not in source
        assert '"delete"' not in source


def test_nothing_model_facing_contains_a_secret(monkeypatch):
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_TOKEN", "jeton-legis-XYZ")
    monkeypatch.setattr(Config, "JURISPRUDENCE_WORKER_TOKEN", "jeton-juris-ABC")
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_URL", "https://l.example")
    monkeypatch.setattr(Config, "JURISPRUDENCE_WORKER_URL", "https://j.example")
    serialized = json.dumps(registry.anthropic_tools(include_writes=True))
    assert "jeton-legis-XYZ" not in serialized
    assert "jeton-juris-ABC" not in serialized
    blocks = json.dumps(charter.system_blocks())
    assert "jeton-legis-XYZ" not in blocks


# ── Executors ───────────────────────────────────────────────────────────────

def _events(monkeypatch):
    collected = []

    def _capture(event, outcome="success", **fields):
        collected.append({"event": event, "outcome": outcome, **fields})

    monkeypatch.setattr(executors, "log_chat_event", _capture)
    return collected


_CTX = {"conversation_id": "c1", "turn_id": "t1", "step": 2}


def test_unknown_tool_is_refused_not_raised(monkeypatch):
    events = _events(monkeypatch)
    result = executors.execute_tool("outil_inconnu", {}, **_CTX)
    assert result.is_error is True
    assert "Unknown tool" in result.content
    assert events[0]["event"] == "chat_tool_refused"
    assert events[0]["reason"] == "unknown_tool"


def test_validation_failure_lists_errors_and_never_calls_the_handler(monkeypatch):
    _events(monkeypatch)

    def _must_not_resolve(name):
        raise AssertionError("handler resolved despite invalid arguments")

    monkeypatch.setattr(mcp_tools, "get_handler", _must_not_resolve)
    result = executors.execute_tool("create_note", {}, **_CTX)
    assert result.is_error is True
    assert "Invalid arguments for create_note" in result.content
    assert "required" in result.content


def test_tool_argument_error_surfaces_the_french_message(monkeypatch):
    _events(monkeypatch)

    def _handler(args):
        raise mcp_tools.ToolArgumentError("Dossier introuvable : abc.")

    monkeypatch.setattr(mcp_tools, "get_handler", lambda name: _handler)
    monkeypatch.setitem(
        mcp_tools.TOOLS,
        "test_outil",
        {"input_schema": {"type": "object"}, "handler": "x"},
    )
    result = executors.execute_tool("test_outil", {}, **_CTX)
    assert result.is_error is True
    assert result.content == "Dossier introuvable : abc."


def test_unexpected_exception_is_generic_and_logged(monkeypatch):
    _events(monkeypatch)
    logged = []
    monkeypatch.setattr(
        executors, "log_unexpected", lambda msg, **kw: logged.append(kw)
    )

    def _handler(args):
        raise RuntimeError("contenu privilégié: le client X a avoué Y")

    monkeypatch.setattr(mcp_tools, "get_handler", lambda name: _handler)
    monkeypatch.setitem(
        mcp_tools.TOOLS,
        "test_outil",
        {"input_schema": {"type": "object"}, "handler": "x"},
    )
    result = executors.execute_tool("test_outil", {}, **_CTX)
    assert result.is_error is True
    # The privileged detail must NOT reach the tool_result.
    assert "avoué" not in result.content
    assert "journalisée" in result.content
    assert logged and logged[0]["tool"] == "test_outil"


def test_success_serializes_json_and_logs_the_call(monkeypatch):
    events = _events(monkeypatch)
    monkeypatch.setattr(
        mcp_tools, "get_handler", lambda name: (lambda args: {"ok": True, "n": 3})
    )
    monkeypatch.setitem(
        mcp_tools.TOOLS,
        "test_outil",
        {"input_schema": {"type": "object"}, "handler": "x"},
    )
    result = executors.execute_tool("test_outil", {}, **_CTX)
    assert result.is_error is False
    assert json.loads(result.content) == {"ok": True, "n": 3}
    call_events = [e for e in events if e["event"] == "chat_tool_call"]
    assert call_events and call_events[0]["outcome"] == "success"
    assert call_events[0]["executor"] == "in_process"
    assert "duration_ms" in call_events[0]


def test_write_kill_switch_refuses_before_execution(monkeypatch):
    events = _events(monkeypatch)
    monkeypatch.setattr(Config, "CHAT_WRITE_ENABLED", False)

    def _must_not_resolve(name):
        raise AssertionError("handler resolved despite the kill switch")

    monkeypatch.setattr(mcp_tools, "get_handler", _must_not_resolve)
    result = executors.execute_tool("create_note", {"dossier_id": "d"}, **_CTX)
    assert result.is_error is True
    assert "CHAT_WRITE_ENABLED" in result.content
    assert events[0]["reason"] == "write_disabled"


def test_gated_tool_is_auto_refused_unattended_with_dry_run_directive(monkeypatch):
    events = _events(monkeypatch)
    monkeypatch.setattr(registry, "GATED_TOOLS", frozenset({"create_note"}))

    def _must_not_resolve(name):
        raise AssertionError("gated tool executed in unattended context")

    monkeypatch.setattr(mcp_tools, "get_handler", _must_not_resolve)
    result = executors.execute_tool(
        "create_note", {"dossier_id": "d"}, unattended=True, **_CTX
    )
    assert result.is_error is True
    assert "dry_run" in result.content
    assert events[0]["reason"] == "gated_unattended"


def test_unattended_write_gets_a_deterministic_idempotency_key(monkeypatch):
    _events(monkeypatch)
    captured = {}

    def _handler(args):
        captured.update(args)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools, "get_handler", lambda name: _handler)
    schema = mcp_tools.TOOLS["create_note"]["input_schema"]
    args = {"dossier_id": "d1", "title": "Titre valide", "content": "Corps valide."}
    errors = mcp_tools.validate_args(schema, args)
    assert not errors, errors

    kwargs = dict(
        unattended=True,
        idempotency_seed="task9|2026-08-26|3",
        tool_use_id="toolu_1",
        **_CTX,
    )
    executors.execute_tool("create_note", dict(args), **kwargs)
    first = captured["idempotency_key"]
    assert first.startswith("planifie-")
    captured.clear()
    executors.execute_tool("create_note", dict(args), **kwargs)
    assert captured["idempotency_key"] == first  # deterministic → replayable

    captured.clear()
    executors.execute_tool(
        "create_note", {**args, "idempotency_key": "cle-fournie-12"}, **kwargs
    )
    assert captured["idempotency_key"] == "cle-fournie-12"  # never overridden


# ── get_skill_file (executor skill_file) ────────────────────────────────────

# Des DICTS, jamais des paires : Firestore refuse un tableau
# imbriqué, et cette forme est celle qui est réellement écrite.
_PAIRS = [{"skill_id": "s-1", "version": 3},
          {"skill_id": "s-2", "version": 1}]


def test_get_skill_file_resolves_to_the_skill_file_executor():
    assert registry.executor_for(registry.GET_SKILL_FILE_NAME) == registry.SKILL_FILE


def test_get_skill_file_never_reaches_the_connector():
    # The Workers' isolation mechanism: the external MCP surface is derived
    # exclusively from mcp.tools.TOOLS — a name absent from it is
    # structurally unreachable from claude.ai.
    assert registry.GET_SKILL_FILE_NAME not in mcp_tools.TOOLS


def test_get_skill_file_always_offered_even_without_writes():
    # Unconditional inclusion — the tools-array prefix must never flap with
    # the skill selection or the write switch (prompt-cache stability).
    for include_writes in (True, False):
        names = [
            t["name"]
            for t in registry.anthropic_tools(include_writes=include_writes)
        ]
        assert registry.GET_SKILL_FILE_NAME in names


def test_get_skill_file_validates_arguments(monkeypatch):
    _events(monkeypatch)
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME, {"skill_id": "s-1"}, **_CTX
    )
    assert result.is_error is True
    assert "Invalid arguments for get_skill_file" in result.content
    assert "filename" in result.content


def test_get_skill_file_refuses_an_unselected_skill_in_french(monkeypatch):
    events = _events(monkeypatch)
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME,
        {"skill_id": "s-fantome", "filename": "Guide"},
        skill_pairs=list(_PAIRS),
        **_CTX,
    )
    assert result.is_error is True
    assert result.content == executors._SKILL_NOT_SELECTED_FR
    assert "s-fantome" not in result.content  # refusals quote nothing
    calls = [e for e in events if e["event"] == "chat_tool_call"]
    assert calls and calls[-1]["executor"] == "skill_file"


def test_get_skill_file_returns_raw_content_without_envelope(monkeypatch):
    _events(monkeypatch)
    raw = 'Texte { "brut" } avec « guillemets » et <chevrons>'
    seen = {}

    def _fake_read(skill_id, version, filename):
        seen.update(skill_id=skill_id, version=version, filename=filename)
        return raw, None

    monkeypatch.setattr(executors, "_read_skill_file", _fake_read)
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME,
        {"skill_id": "s-2", "filename": "Guide"},
        skill_pairs=list(_PAIRS),
        **_CTX,
    )
    assert result.is_error is False
    assert result.content == raw  # verbatim — no JSON envelope
    # The version came from THIS turn's pairs, not any head read.
    assert seen == {"skill_id": "s-2", "version": 1, "filename": "Guide"}


def test_get_skill_file_surfaces_the_reader_reason(monkeypatch):
    _events(monkeypatch)
    monkeypatch.setattr(
        executors,
        "_read_skill_file",
        lambda *a: (None, "Fichier inconnu. Fichiers disponibles : Guide."),
    )
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME,
        {"skill_id": "s-1", "filename": "Absent"},
        skill_pairs=list(_PAIRS),
        **_CTX,
    )
    assert result.is_error is True
    assert "Fichiers disponibles" in result.content


def test_get_skill_file_never_resolves_a_handler(monkeypatch):
    # Risk-5 pin: the branch is a SIBLING of _execute_in_process — none of
    # the in-process machinery (handler resolution included) may run.
    _events(monkeypatch)
    monkeypatch.setattr(
        mcp_tools,
        "get_handler",
        mock.Mock(side_effect=AssertionError("handler resolved")),
    )
    monkeypatch.setattr(
        executors, "_read_skill_file", lambda *a: ("contenu", None)
    )
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME,
        {"skill_id": "s-1", "filename": "Guide"},
        skill_pairs=list(_PAIRS),
        **_CTX,
    )
    assert result.is_error is False
    assert result.content == "contenu"


def test_get_skill_file_unexpected_reader_error_is_generic(monkeypatch):
    _events(monkeypatch)
    logged = []
    monkeypatch.setattr(
        executors, "log_unexpected", lambda msg, **kw: logged.append(kw)
    )
    monkeypatch.setattr(
        executors,
        "_read_skill_file",
        mock.Mock(side_effect=RuntimeError("firestore down")),
    )
    result = executors.execute_tool(
        registry.GET_SKILL_FILE_NAME,
        {"skill_id": "s-1", "filename": "Guide"},
        skill_pairs=list(_PAIRS),
        **_CTX,
    )
    assert result.is_error is True
    assert "firestore down" not in result.content
    assert logged and logged[0]["tool"] == registry.GET_SKILL_FILE_NAME


# ── Worker client ───────────────────────────────────────────────────────────

_SPEC = {
    "name": "legislation_chercher",
    "worker": "legislation",
    "tool": "chercher",
    "path": "/mcp",
    "transport": "mcp",
}


def _mcp(text, is_error=False):
    """One MCP tools/call response, as the Workers frame it."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _configure_worker(monkeypatch):
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_URL", "https://l.example")
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_TOKEN", "jeton")


def test_worker_unconfigured_refuses_without_network(monkeypatch):
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_URL", "")
    monkeypatch.setattr(Config, "LEGISLATION_WORKER_TOKEN", "")
    result = worker_client.call_worker(_SPEC, {})
    assert result["ok"] is False
    assert result["reason"] == "not_configured"


def test_worker_timeout_and_connection_errors_are_machine_stable(monkeypatch):
    import requests as _requests

    _configure_worker(monkeypatch)
    monkeypatch.setattr(
        worker_client.requests,
        "post",
        mock.Mock(side_effect=_requests.Timeout()),
    )
    assert worker_client.call_worker(_SPEC, {})["reason"] == "timeout"
    monkeypatch.setattr(
        worker_client.requests,
        "post",
        mock.Mock(side_effect=_requests.ConnectionError()),
    )
    assert worker_client.call_worker(_SPEC, {})["reason"] == "connection_error"


class _FakeResponse:
    def __init__(self, status_code=200, chunks=(b"{}",), content_type="application/json"):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_worker_http_error_and_bad_json(monkeypatch):
    _configure_worker(monkeypatch)
    monkeypatch.setattr(
        worker_client.requests, "post", lambda *a, **k: _FakeResponse(502)
    )
    # A 502 must not be read as an empty result: it is a failure, loudly.
    assert worker_client.call_worker(_SPEC, {})["reason"] == "http_502"
    monkeypatch.setattr(
        worker_client.requests,
        "post",
        lambda *a, **k: _FakeResponse(200, (b"pas du json",)),
    )
    assert worker_client.call_worker(_SPEC, {})["reason"] == "invalid_json"


def test_worker_oversized_response_is_refused_not_truncated(monkeypatch):
    _configure_worker(monkeypatch)
    big = b"x" * (worker_client.RESPONSE_CAP_BYTES + 1)
    monkeypatch.setattr(
        worker_client.requests,
        "post",
        lambda *a, **k: _FakeResponse(200, (big,)),
    )
    result = worker_client.call_worker(_SPEC, {})
    assert result["ok"] is False
    assert result["reason"] == "response_too_large"


def test_worker_success_returns_the_tools_text_verbatim(monkeypatch):
    _configure_worker(monkeypatch)
    prose = "CONFIRMÉE — établit l'existence, jamais l'autorité actuelle."
    monkeypatch.setattr(
        worker_client.requests,
        "post",
        lambda *a, **k: _FakeResponse(200, (_mcp(prose),)),
    )
    result = worker_client.call_worker(_SPEC, {"q": "art. 2925"})
    assert result == {"ok": True, "text": prose}


# ── Charter ─────────────────────────────────────────────────────────────────

def test_charter_version_and_base_content():
    assert charter.SOURCE_CHARTER_VERSION == 1
    # Structurel : les cinq sections du texte de repli, pas leur prose —
    # celle-ci est éditable, et une épingle littérale la fige de fait.
    for section in ("DEVOIRS ÉPISTÉMIQUES", "DONNÉES PRIVILÉGIÉES",
                    "RÈGLES DE STYLE", "RÈGLES DE SORTIE",
                    "DISCIPLINE D'ÉCRITURE"):
        assert section in charter.BASE_CHARTER, section
    # The scheduled addendum only appears when asked for.
    # L'addendum n'apparaît QUE sur demande — par son texte, pas par un
    # titre que le praticien peut renommer.
    assert charter.SCHEDULED_ADDENDUM.strip() not in charter.charter_text()
    assert charter.SCHEDULED_ADDENDUM.strip() in charter.charter_text(
        scheduled=True
    )


def test_system_blocks_stable_order_and_trailing_cache_control():
    skills = [
        {"id": "b-skill", "name": "B", "version": 2, "body": "corps B"},
        {"id": "a-skill", "name": "A", "version": 1, "body": "corps A"},
        {"id": "vide", "name": "V", "version": 1, "body": "   "},
    ]
    blocks = charter.system_blocks(skills)
    # Charter first; skills sorted by id; blank-bodied skill dropped.
    assert blocks[0]["text"].startswith(charter.SOCLE.strip())
    assert "corps A" in blocks[1]["text"]
    assert "corps B" in blocks[2]["text"]
    assert len(blocks) == 3
    # Only the LAST block carries the cache breakpoint.
    assert blocks[-1].get("cache_control") == {"type": "ephemeral"}
    assert all("cache_control" not in b for b in blocks[:-1])


def test_system_blocks_without_skills_marks_the_charter_block():
    blocks = charter.system_blocks(None, scheduled=True)
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert charter.SCHEDULED_ADDENDUM.strip() in blocks[0]["text"]


_SKILL_WITH_FILES = {
    "id": "s-files",
    "name": "Rédaction",
    "version": 3,
    "body": "corps",
    "files": [
        {
            "name": "Modèle",
            "description": "Structure type.",
            "sha256": "a" * 64,
            "chars": 1200,
        },
        {"name": "Zéro-tri", "description": "", "sha256": "b" * 64, "chars": 8},
    ],
}


def test_skill_block_lists_reference_files_inside_the_same_block():
    blocks = charter.system_blocks([dict(_SKILL_WITH_FILES)])
    # Same block COUNT as a files-less skill — the listing lives INSIDE.
    assert len(blocks) == 2
    text = blocks[1]["text"]
    assert text.startswith("COMPÉTENCE — Rédaction\n\ncorps")
    assert "FICHIERS DE RÉFÉRENCE" in text
    assert "get_skill_file" in text
    assert "skill_id : s-files" in text
    assert "- Modèle — Structure type. (1200 caractères)" in text
    # Empty description → segment OMITTED, not blank.
    assert "- Zéro-tri (8 caractères)" in text
    assert "Zéro-tri —" not in text
    # A skill WITHOUT files renders byte-identical to the pre-files format
    # (existing conversations' cache prefixes untouched).
    bare = charter.system_blocks(
        [{"id": "x", "name": "Nu", "version": 1, "body": "corps nu"}]
    )
    assert bare[1]["text"] == "COMPÉTENCE — Nu\n\ncorps nu"


def test_skill_block_listing_is_byte_stable():
    once = charter.system_blocks([dict(_SKILL_WITH_FILES)])
    twice = charter.system_blocks([dict(_SKILL_WITH_FILES)])
    assert once[1]["text"] == twice[1]["text"]
    # Manifest order preserved as saved — never re-sorted (« Zéro-tri »
    # would sort before « Modèle » neither alphabetically nor otherwise).
    text = once[1]["text"]
    assert text.index("- Modèle") < text.index("- Zéro-tri")


# ── La charte éditable : le socle, le gel, et le bloc jamais vide ──────────

_SHA_BASE = "93df897e6dc940897c28c7579d77bcec57053a85c8cdaa54dbe0ada2163f7493"
_SHA_ADDENDUM = "9f4d69b1c64952db84ec2a55b8f1944566f6e8dd8c85cdfe5035ab2fad5c3525"



def test_the_fallback_text_mirrors_the_socle_and_the_seed():
    """Le repli ne peut plus contredire la charte en vigueur.

    BASE_CHARTER était un littéral gelé par sha, pour que
    « charter_version: 1 » identifie des octets fixes. Il est DÉRIVÉ
    depuis le 2026-08-27 : un repli qui sert un texte périmé — ancien
    registre, anciennes règles — dégrade en silence sur le FOND, pas
    seulement sur le numéro. Le prix a été mesuré avant d'être payé :
    quatre tours du registre portent 1, tous antérieurs au lot.

    Ce qui reste vrai de « 1 » : c'est le texte source du commit
    déployé, résoluble par git — pas par un hachage épinglé ici.
    """
    assert charter.SOURCE_CHARTER_VERSION == 1
    assert charter.SOCLE.strip() in charter.BASE_CHARTER
    assert charter.SEED_CORPS.strip() in charter.BASE_CHARTER
    # Le repli est un texte COMPLET : socle + règles de travail.
    for section in ("DEVOIRS ÉPISTÉMIQUES", "DONNÉES PRIVILÉGIÉES",
                    "RÈGLES DE SORTIE", "DISCIPLINE D'ÉCRITURE"):
        assert section in charter.BASE_CHARTER, section


def _sections(texte):
    """{TITRE EN CAPITALES: corps} — la structure, pas la prose.

    Le socle est passé en PROSE le 2026-08-27 : compter des puces ne
    veut plus rien dire. On mesure la LONGUEUR de chaque section, ce
    qui attrape encore la suppression d'une règle sur deux — ce
    qu'une simple sous-chaîne laisse passer.
    """
    out, courante = {}, None
    for ligne in texte.splitlines():
        depouille = ligne.strip()
        if not depouille:
            continue
        if depouille == depouille.upper() and len(depouille) > 3:
            courante = depouille
            out[courante] = []
        elif courante is not None:
            out[courante].append(depouille)
    return {k: " ".join(v) for k, v in out.items()}


def test_the_socle_still_carries_every_guarantee_it_exists_for():
    """L'égalité avec BASE_CHARTER a été retirée le 2026-08-27, à dessein.

    Elle prouvait que la SCISSION ne changeait aucune règle ; cette
    preuve est faite. Le socle a ensuite évolué pour son compte — ce
    qui est exactement ce à quoi sert un texte sous revue de code, et
    ce qu'une égalité figée aurait interdit.

    Reste ce pour quoi le socle existe : il ne peut pas être vide (sans
    quoi le bloc 0 le serait, et Vertex répondrait 400 sur TOUTES les
    conversations), et il porte encore les garanties qu'un lapsus à
    l'écran ne doit pas pouvoir retirer.
    """
    assert len(charter.SOCLE.strip()) > 500
    sections = _sections(charter.SOCLE)
    assert set(sections) == {"DEVOIRS ÉPISTÉMIQUES", "DONNÉES PRIVILÉGIÉES"}
    # La LONGUEUR par section attrape une perte MASSIVE — une section
    # vidée, une règle entière tombée. Elle n'attrape PAS le retrait
    # d'une phrase (mesuré : −42 caractères passent), et rien ici ne le
    # peut sans épingler la prose, ce qui interdirait au socle d'être
    # retouché. Le contrôle premier reste la revue de code ; ceci en est
    # le filet, pas le juge.
    assert len(sections["DEVOIRS ÉPISTÉMIQUES"]) > 400
    assert len(sections["DONNÉES PRIVILÉGIÉES"]) > 400
    for ancre in ("legislation_*", "jurisprudence_*", "web_search",
                  "get_document_text", "suppression",
                  # 2026-08-27 : le socle envoie lire les fichiers de la
                  # charte. Sans cette phrase, le listing du bloc 0 dit
                  # « à lire seulement au besoin » et rien ne dit que le
                  # besoin EXISTE — un fichier qui documente les pièges des
                  # données ne sert à rien s'il n'est jamais ouvert.
                  "fichiers de référence", "plutôt que de supposer"):
        assert ancre in charter.SOCLE, ancre
    # ⚠ Le socle ne NOMME aucun fichier, et ne doit jamais en nommer : il est
    # sous revue de code et se déploie, alors que les fichiers de la charte
    # s'éditent à l'écran. Y inscrire « dossier-athena.md » ferait mentir le
    # socle le jour où il est renommé ou retiré — or le socle est justement
    # la partie qui doit TOUJOURS être vraie.
    assert ".md" not in charter.SOCLE
    # ⚠ Le socle ne NOMME aucun fichier, et ne doit jamais en nommer : il
    # est sous revue de code et se déploie, alors que les fichiers de la
    # charte s'éditent à l'écran. Nommer « dossier-athena.md » ici ferait
    # mentir le socle le jour où il est renommé ou retiré — or le socle
    # est précisément la partie qui doit TOUJOURS être vraie.
    assert ".md" not in charter.SOCLE
    # La graine garde les deux sections de travail.
    assert "RÈGLES DE SORTIE" in charter.SEED_CORPS
    assert "DISCIPLINE D'ÉCRITURE" in charter.SEED_CORPS


def test_the_socle_vouvoie_throughout():
    """Un seul registre dans le bloc 0.

    Le socle vouvoie depuis le 2026-08-27 ; il a été mi-« vous » mi-« tu »
    pendant une édition, ce que le modèle lisait dans le même bloc.
    BASE_CHARTER, lui, reste au tutoiement — il est gelé, et c'est ce que
    voit un tour tombé en repli.
    """
    for tutoiement in (" tu ", "Tu ", " te ", " ton ", " tes "):
        assert tutoiement not in charter.SOCLE, tutoiement


def test_one_join_path_for_every_version():
    """Il y en a eu DEUX, tant que les octets de la v1 étaient gelés.

    Ils ne le sont plus : un seul joint explicite sert les deux, et un
    corps sans saut final ne colle plus l'addendum à son dernier
    paragraphe — des deux côtés.
    """
    source = charter.source_charter()
    assert charter.charter_text(source) == charter.BASE_CHARTER
    assert charter.charter_text() == charter.BASE_CHARTER
    autre = {"version": 9, "body": "CORPS", "addendum": "PLANIFIÉ",
             "files": []}
    for resolue in (source, autre):
        texte = charter.charter_text(resolue, scheduled=True)
        corps = str(resolue["body"]).rstrip()
        assert texte.startswith(corps)
        # Exactement une ligne blanche entre le corps et l'addendum.
        reste = texte[len(corps):]
        assert reste.startswith(chr(10) * 2) and reste[2] != chr(10)


def test_a_firestore_charter_gets_the_socle_and_an_explicit_join():
    resolue = charter.charter_from_head(
        {"current_version": 7, "body": "RÈGLES\n- Une règle.",
         "addendum": "PLANIFIÉ\n- Une autre.", "files": []}
    )
    assert resolue["version"] == 7
    texte = charter.charter_text(resolue)
    assert texte.startswith(charter.SOCLE.strip())   # le socle d'abord
    assert "DEVOIRS ÉPISTÉMIQUES" in texte
    assert texte.rstrip().endswith("- Une règle.")
    # Un corps enregistré sans saut final ne colle PAS l'addendum au
    # dernier paragraphe : le joint est explicite.
    planifie = charter.charter_text(resolue, scheduled=True)
    assert "- Une règle.\n\nPLANIFIÉ" in planifie


def test_a_blank_resolved_charter_falls_back_never_an_empty_block():
    """`system_blocks` construisait le bloc 0 SANS garde — il ne pouvait
    pas être vide tant qu'il était une constante. Un bloc texte vide fait
    répondre 400 à Vertex, donc chaque tour de chaque conversation
    finalise `failed` jusqu'à ré-édition."""
    blanche = {"version": 9, "body": "   ", "addendum": "", "files": []}
    blocks = charter.system_blocks(None, charter=blanche)
    assert blocks[0]["text"].strip()
    assert blocks[0]["text"] == charter.BASE_CHARTER


def test_blocks_are_fresh_each_call_never_a_shared_cache_control():
    """La charte résolue est une DONNÉE, jamais un bloc prêt-à-servir.

    Mémoïser un bloc partagé y stamperait un `cache_control` persistant :
    deux points d'arrêt dans `system`, le premier au mauvais endroit, sans
    erreur — le jumeau du piège que `_build_tools` documente déjà.
    """
    resolue = charter.charter_from_head(
        {"current_version": 3, "body": "RÈGLES\n- Une règle.", "files": []}
    )
    skills = [{"id": "a", "name": "A", "version": 1, "body": "corps A"}]
    premier = charter.system_blocks(skills, charter=resolue)
    second = charter.system_blocks(skills, charter=resolue)
    assert premier == second
    assert "cache_control" not in premier[0]
    assert premier[-1]["cache_control"] == {"type": "ephemeral"}
    # La donnée résolue n'a pas été mutée au passage.
    assert "cache_control" not in resolue


def test_the_charter_lists_its_own_reference_files_in_block_zero():
    resolue = charter.charter_from_head(
        {
            "current_version": 4,
            "body": "RÈGLES\n- Une règle.",
            "files": [
                {"name": "Grille", "description": "Aide-mémoire.",
                 "sha256": "c" * 64, "chars": 900}
            ],
        }
    )
    blocks = charter.system_blocks(None, charter=resolue)
    assert len(blocks) == 1                      # le listing vit DEDANS
    texte = blocks[0]["text"]
    assert "FICHIERS DE RÉFÉRENCE" in texte
    assert f"skill_id : {charter.CHARTER_FILE_ID}" in texte
    assert "- Grille — Aide-mémoire. (900 caractères)" in texte
    # Sans fichier, le bloc 0 reste octet-identique au format d'avant.
    nue = charter.system_blocks(None, charter=charter.source_charter())
    assert nue[0]["text"] == charter.BASE_CHARTER


def test_the_reserved_charter_file_id_matches_the_model():
    """Littéral tenu à la main des deux côtés (chat/ ne doit pas tirer
    models/ à l'import) — donc épinglé, comme les enums de mcp/tools.py."""
    with mock.patch("google.cloud.firestore.Client"):
        import models.chat_charter as charter_model

    assert charter.CHARTER_FILE_ID == charter_model.DOC_ID
    assert charter.SOURCE_CHARTER_VERSION == charter_model.SOURCE_VERSION


# ── Le dossier de la conversation ──────────────────────────────────────────

_CONV_DOSSIER = {
    "id": "c1", "dossier_id": "d-42", "dossier_file_number": "2026-027",
    "dossier_title": "Hraki c. Solo inc.",
}


def test_a_bound_conversation_names_its_dossier_to_the_model():
    """Le rattachement existait — pour le regroupement à l'écran et la
    comptabilité — mais n'atteignait JAMAIS le prompt, si bien que le
    modèle demandait de quel dossier on parlait dans une conversation qui
    en portait un depuis sa création."""
    blocks = charter.system_blocks(None, conv=dict(_CONV_DOSSIER))
    dernier = blocks[-1]["text"]
    assert "DOSSIER DE CETTE CONVERSATION" in dernier
    assert "2026-027" in dernier
    assert "Hraki c. Solo inc." in dernier
    assert "dossier_id : d-42" in dernier


def test_a_floating_conversation_gets_no_dossier_block():
    for conv in (None, {}, {"dossier_id": ""}, {"dossier_id": "   "}):
        blocks = charter.system_blocks(None, conv=conv)
        assert len(blocks) == 1, conv
        assert "DOSSIER DE CETTE CONVERSATION" not in blocks[0]["text"]


def test_the_dossier_block_sits_after_the_cache_breakpoint():
    """Il VARIE d'une conversation à l'autre.

    L'inclure au préfixe mis en cache ferait que deux conversations n'en
    partagent plus rien — or ce préfixe (charte + compétences) vaut ~18 000
    jetons et le cache implicite de Gemini y atteint 60 à 92 %. Le bloc de
    dossier fait ~70 jetons : le relire à chaque appel coûte infiniment
    moins que de perdre le partage du reste.
    """
    skills = [{"id": "a", "name": "A", "version": 1, "body": "corps A"}]
    blocks = charter.system_blocks(skills, conv=dict(_CONV_DOSSIER))
    marques = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert marques == [len(blocks) - 2]
    assert "DOSSIER DE CETTE CONVERSATION" in blocks[-1]["text"]
    # Et le préfixe partagé est IDENTIQUE d'une conversation à l'autre.
    autre = charter.system_blocks(
        skills, conv={**_CONV_DOSSIER, "dossier_id": "d-99"}
    )
    assert blocks[:-1] == autre[:-1]
    assert blocks[-1] != autre[-1]
