"""Regenerate ``chat/worker_tools.py`` from a Worker's live ``tools/list``.

The legal-research Workers (``legislation``, ``jurisprudence``) are MCP
servers. Their tool names, descriptions and input schemas are THEIR
contract, not ours: the descriptions carry the connectors' own reliability
warnings verbatim, and a schema copied by hand goes stale the day the
Worker ships a change — silently, because nothing here would fail.

So the specs are GENERATED, reviewed, and committed:

* generated, so nothing is transcribed by hand;
* committed rather than fetched per turn, because the model's tools array
  is the prompt-cache prefix — it must be byte-stable between turns, and a
  Worker deploy must never change the offered tool set without a human
  reading the diff;
* checkable, so drift is a failing command instead of a discovery.

Usage (from the athena/ directory)::

    # rewrite chat/worker_tools.py from the live Worker
    JURISPRUDENCE_WORKER_TOKEN=... python -m scripts.sync_worker_tools \
        --worker jurisprudence --url https://jurisprudence.poirierlavoie.ca

    # fail (exit 1) if the committed file no longer matches the Worker
    ... python -m scripts.sync_worker_tools --worker jurisprudence \
        --url ... --check

    # offline: render from a saved tools/list response
    python -m scripts.sync_worker_tools --worker jurisprudence \
        --from-json /tmp/tools.json

THE TOKEN IS NEVER AN ARGUMENT and is never printed. It comes from the
environment (``<WORKER>_WORKER_TOKEN``) or from Config, so it stays out of
shell history and out of this script's output — including its errors.

Regenerating one worker PRESERVES the other's specs: the file holds both,
and a sync run that dropped the twin because its URL was not at hand would
be a silent capability loss.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ATHENA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT_PATH = os.path.join(_ATHENA_DIR, "chat", "worker_tools.py")

WORKERS = ("legislation", "jurisprudence")

# The MCP endpoint path on both Workers, and the protocol revision we ask
# for. Both are Worker facts, not per-tool facts, so they are stamped on
# every generated spec rather than guessed by the client.
MCP_PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"

# ⚠ LOAD-BEARING. urllib's default User-Agent is « Python-urllib/3.x », and
# Cloudflare's edge answers it with a 403 before the Worker ever sees the
# request — measured against production 2026-08-27: urllib 403, everything
# else 200. Without this header the drift check fails where it matters most
# (against the live Worker) with a status that reads like an auth or origin
# problem and is neither. The runtime client is unaffected: `requests` sends
# its own UA, which passes.
USER_AGENT = "pallas-athena-sync-worker-tools/1"

TIMEOUT_S = 30

_TRIPLE = '"' * 3


class SyncError(Exception):
    """Fatal, and its message never quotes the token or a full URL."""


# ── Fetching ────────────────────────────────────────────────────────────────


def _token(worker: str) -> str:
    """The Worker's bearer token, from the environment or Config.

    Never returned to a caller that prints it, and never interpolated into
    an error message.
    """
    env_name = f"{worker.upper()}_WORKER_TOKEN"
    token = os.environ.get(env_name, "")
    if not token:
        try:
            from config import Config  # late: needs athena/ on sys.path

            token = getattr(Config, env_name, "") or ""
        except Exception:  # pragma: no cover - config is optional here
            token = ""
    if not token:
        raise SyncError(
            f"No token for « {worker} ». Set {env_name} in the environment "
            "(never pass it as an argument)."
        )
    return token


def fetch_descriptors(worker: str, base_url: str) -> list[dict]:
    """One ``tools/list`` call. Returns the MCP tool descriptors."""
    url = base_url.rstrip("/") + MCP_PATH
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {_token(worker)}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        # The status, never the body: a Worker error body can echo the URL.
        raise SyncError(f"« {worker} » answered HTTP {exc.code} to tools/list.") from exc
    except Exception as exc:
        raise SyncError(f"« {worker} » is unreachable.") from exc

    payload = _decode(raw, content_type)
    if "error" in payload:
        code = (payload.get("error") or {}).get("code")
        raise SyncError(f"« {worker} » returned JSON-RPC error {code}.")
    tools = ((payload.get("result") or {}).get("tools")) or []
    if not isinstance(tools, list) or not tools:
        raise SyncError(f"« {worker} » returned no tools.")
    return tools


def _decode(raw: str, content_type: str) -> dict:
    """A JSON-RPC response, whether framed as JSON or as SSE.

    The stateless Workers answer ``application/json``; an MCP server built
    on the official SDK answers ``text/event-stream`` for the very same
    request. Both are legal Streamable HTTP.
    """
    if "text/event-stream" in content_type.lower():
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        raise SyncError("Unreadable SSE response to tools/list.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SyncError("Unreadable response to tools/list.") from exc


# ── Rendering ───────────────────────────────────────────────────────────────


def build_specs(worker: str, descriptors: list[dict]) -> list[dict]:
    """MCP descriptors → chat registry specs, in the SERVER's order.

    The offered name is namespaced ``<worker>_`` + the remote name: the
    chat registry routes on that prefix, and it keeps these names disjoint
    from the firm's own in-process tools. ``tool`` carries the name the
    Worker actually answers to — the two must never be conflated.

    ``description`` and ``input_schema`` are copied VERBATIM. They are
    contract surface, not prose: the connectors' warnings live in them.
    """
    specs = []
    for descriptor in descriptors:
        remote = str(descriptor.get("name", ""))
        if not remote:
            raise SyncError(f"« {worker} » returned a tool with no name.")
        specs.append(
            {
                "name": f"{worker}_{remote}",
                "title": str(descriptor.get("title", "") or remote),
                "description": str(descriptor.get("description", "")),
                "input_schema": descriptor.get("inputSchema")
                or descriptor.get("input_schema")
                or {},
                "worker": worker,
                "tool": remote,
                "path": MCP_PATH,
                "method": "POST",
                "transport": "mcp",
            }
        )
    return specs


_HEADER_BODY = """Legal-research Worker tool specs — GENERATED. Do not edit by hand.

Regenerate with ``python -m scripts.sync_worker_tools --worker <name>
--url <base>``; check for drift with the same command plus ``--check``.
The generator is the only writer of this file, and it is the sole reason
nothing here can silently disagree with the Workers.

The two Cloudflare Workers (``legislation``, ``jurisprudence``) are MCP
servers: one JSON-RPC ``tools/call`` per invocation, over ``POST /mcp``,
authenticated by a bearer token each (user decisions D5/D10 — D5 amended
2026-08-26: the Workers speak MCP, not plain REST).

Entry shape (one dict per tool):

    {
        "name": "jurisprudence_canlii_verify_citations",  # offered to the model
        "title": "Vérifier des citations",                # French display title
        "description": "…",                               # VERBATIM from the Worker
        "input_schema": {…},                              # VERBATIM from the Worker
        "worker": "jurisprudence",                        # which token/URL pair
        "tool": "canlii_verify_citations",                # the REMOTE name
        "path": "/mcp",                                   # appended to the Worker URL
        "method": "POST",
        "transport": "mcp",
    }

Rules, pinned by tests/test_chat_registry.py:

* every ``name`` matches ``^(legislation|jurisprudence)_`` and is DISJOINT
  from ``mcp.tools.TOOLS`` — these tools live in the CHAT registry only and
  never reach the external MCP connector (claude.ai already talks to the
  Workers directly; re-exporting them would double the surface for nothing);
* ``worker`` ∈ {"legislation", "jurisprudence"} — it selects the URL and the
  bearer token (two DISTINCT secrets, independent revocation);
* ``name`` is the model-facing name and ``tool`` the REMOTE one; the client
  sends ``tool``, never ``name`` (chat/worker_client.py);
* ``description`` and ``input_schema`` reach the model verbatim. The Worker
  owns its own validation — the chat client only bounds sizes.

ORDER IS THE SERVER'S ORDER and must stay stable: the model's tools array
is the prompt-cache prefix, and a reshuffle costs a full cache miss on
every turn.
"""

_HEADER = (
    _TRIPLE
    + _HEADER_BODY
    + _TRIPLE
    + "\n\nfrom __future__ import annotations\n\n"
    + 'WORKER_NAME_PREFIXES: tuple[str, ...] = ("legislation_", "jurisprudence_")\n\n'
)


def _py(value, indent: int) -> str:
    """One JSON-ish value as a Python literal, deterministically.

    Hand-rolled rather than ``pprint``, for ONE reason that matters at
    review time: a description stays on a SINGLE line. ``pprint`` reflows
    a paragraph into a column of fragments, so changing one word there
    rewrites twenty lines of diff and the actual edit disappears into the
    reflow. Here, one changed description is one changed line.

    Strings go through ``json.dumps``: its escapes are a strict subset of
    Python's, so the output is a valid Python literal, and the accented
    French survives unescaped.
    """
    pad = " " * indent
    inner_pad = " " * (indent + 4)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [
            f"{inner_pad}{json.dumps(str(k), ensure_ascii=False)}: "
            f"{_py(v, indent + 4)},"
            for k, v in value.items()
        ]
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        # `required` lists and `enum`s are short lists of scalars; spread
        # over three lines each they bury the schema they belong to.
        if all(not isinstance(v, (dict, list)) for v in value):
            flat = "[" + ", ".join(_py(v, 0) for v in value) + "]"
            if indent + len(flat) <= 88:
                return flat
        lines = [f"{inner_pad}{_py(v, indent + 4)}," for v in value]
        return "[\n" + "\n".join(lines) + f"\n{pad}]"
    if isinstance(value, bool):  # before int — bool IS an int in Python
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def render(specs: list[dict]) -> str:
    """The complete file text. Deterministic — ``--check`` depends on it."""
    if not specs:
        return _HEADER + "WORKER_TOOLS: tuple[dict, ...] = ()\n"
    entries = "\n".join(f"    {_py(dict(spec), 4)}," for spec in specs)
    # A tuple, not a list: an importer cannot mutate it by accident.
    return _HEADER + f"WORKER_TOOLS: tuple[dict, ...] = (\n{entries}\n)\n"


def existing_specs() -> list[dict]:
    """The specs currently committed, so syncing ONE worker keeps the
    other's. Returns [] when the file is absent or not importable."""
    try:
        from chat.worker_tools import WORKER_TOOLS

        return [dict(spec) for spec in WORKER_TOOLS]
    except Exception:  # pragma: no cover - first run, or a broken file
        return []


def merge(existing: list[dict], worker: str, fresh: list[dict]) -> list[dict]:
    """Replace this worker's specs, keep every other worker's, in WORKERS
    order so the result never depends on the order the syncs were run."""
    kept = [s for s in existing if s.get("worker") != worker]
    combined = kept + fresh
    return sorted(combined, key=lambda s: WORKERS.index(str(s.get("worker"))))


# ── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate chat/worker_tools.py.")
    parser.add_argument("--worker", choices=WORKERS, required=True)
    parser.add_argument("--url", default="", help="Worker base URL, without /mcp")
    parser.add_argument(
        "--from-json",
        default="",
        help="a saved tools/list response, for offline runs",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed file differs from what the Worker says",
    )
    parser.add_argument("--out", default=_OUT_PATH)
    args = parser.parse_args(argv)

    try:
        if args.from_json:
            with io.open(args.from_json, encoding="utf-8") as handle:
                payload = json.load(handle)
            descriptors = ((payload.get("result") or {}).get("tools")) or payload
        else:
            if not args.url:
                raise SyncError("--url is required (or --from-json).")
            descriptors = fetch_descriptors(args.worker, args.url)
        specs = merge(
            existing_specs(), args.worker, build_specs(args.worker, descriptors)
        )
        rendered = render(specs)
    except SyncError as exc:
        print(f"sync_worker_tools: {exc}", file=sys.stderr)
        return 2

    current = ""
    if os.path.exists(args.out):
        with io.open(args.out, encoding="utf-8") as handle:
            current = handle.read()

    if args.check:
        if current == rendered:
            print(f"sync_worker_tools: {args.worker} — no drift ({len(specs)} tools).")
            return 0
        print(
            "sync_worker_tools: DRIFT — chat/worker_tools.py no longer matches "
            f"« {args.worker} ». Re-run without --check and read the diff.",
            file=sys.stderr,
        )
        return 1

    if current == rendered:
        print(f"sync_worker_tools: {args.worker} — already current ({len(specs)} tools).")
        return 0
    with io.open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"sync_worker_tools: wrote {len(specs)} tools to chat/worker_tools.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
