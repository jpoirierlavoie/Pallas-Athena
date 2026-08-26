"""Legal-research Worker tool specs (Phase N) — DATA, late-bound.

The two Cloudflare Workers (``legislation``, ``jurisprudence``) are plain
REST services with a bearer token each (user decisions D5/D10). Their
endpoints and schemas are supplied by the practitioner and pasted here as
data — late binding is a data edit, never a structural change. The registry
builder iterates this tuple, so an empty tuple simply means the chat has no
Worker tools yet.

Entry shape (one dict per tool):

    {
        "name": "jurisprudence_verifier_citation",   # ^(legislation|jurisprudence)_
        "title": "Vérifier une citation",            # French display title
        "description": "…",                          # English prose for the model
        "input_schema": {"type": "object", "properties": {…}, "required": […]},
        "worker": "jurisprudence",                   # which token/URL pair
        "path": "/citations/verifier",               # appended to the Worker URL
        "method": "POST",                            # POST only in v1
    }

Rules, pinned by tests/test_chat_registry.py:

* every ``name`` matches ``^(legislation|jurisprudence)_`` and is DISJOINT
  from ``mcp.tools.TOOLS`` — these tools live in the CHAT registry only and
  never reach the external MCP connector (claude.ai already talks to the
  Workers directly; re-exporting them would double the surface for nothing);
* ``worker`` ∈ {"legislation", "jurisprudence"} — it selects the URL and the
  bearer token (two DISTINCT secrets, independent revocation);
* ``input_schema`` is passed to the model verbatim. The Worker owns its own
  validation — the chat client only bounds sizes (see worker_client.py).
"""

from __future__ import annotations

WORKER_NAME_PREFIXES: tuple[str, ...] = ("legislation_", "jurisprudence_")

# Empty at delivery — the practitioner supplies the specs mid-implementation
# (decision D5). Paste entries per the module docstring's shape.
WORKER_TOOLS: tuple[dict, ...] = ()
