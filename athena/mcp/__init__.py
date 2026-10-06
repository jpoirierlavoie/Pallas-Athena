"""MCP (Model Context Protocol) layer — Phase I.

Exposes Pallas Athena's data to Claude as a custom connector:

* ``oauth_bp`` — embedded OAuth 2.1 authorization server (RFC 8414/9728
  metadata, RFC 7591 Dynamic Client Registration, consent screen behind the
  Firebase session, token endpoint with PKCE + refresh rotation, RFC 7009
  revocation).
* ``mcp_bp`` — ``POST /mcp``: a stateless, JSON-response-mode Streamable
  HTTP server (initialize / ping / tools list + call). No SSE, no sessions.

**Reads dominate; nothing is ever deleted.** The write tools are
:data:`mcp.tools.WRITE_TOOLS`, DERIVED from the disclosure registry
:data:`mcp.disclosure.FAMILIES` — the ONE place the families, their members
and their descriptions live, and from which both INSTRUCTIONS and the
consent screen are assembled. What the connector can NEVER do lives beside
them, in :data:`mcp.disclosure.NEVERS`, each promise backed by a sweep of
the connector's syntax tree. Do not restate either here: hand-kept copies
of this list went stale three times. :data:`mcp.tools.EDIT_TOOLS` is the
subset that REPLACES a stored value, and it is what derives
``destructiveHint`` — the annotation stopped being a family constant the
day an edit shipped.

Notes, tasks and hearings are DAV-exposed per dossier, and parties through
the CardDAV addressbook; the models never bump a CTag — bumping lives in the
caller. **Every DAV-exposed write on a tool path MUST call
``bump_ctag(collection_for(dossier_id))``, or ``bump_ctag("parties")`` for a
contact**, or the item lands in Firestore, shows up in the web UI, and DavX5
silently never re-syncs it. All writes run through
``mcp/write_support.run_write`` (``idempotency_key``).

**No kill switch.** ``MCP_ENABLED``, ``MCP_WRITE_ENABLED`` and
``MCP_COMPTABILITE_ENABLED`` — and the separate ``athena:comptabilite``
scope with its consent box — were removed on 2026-10-05, the lawyer's
decision: the endpoint is always served, and every write tool, the
ACCOUNTING family's included, is reached by a token holding
``athena:write`` (the administration ledger's read, ``get_admin_ledger``,
by ``athena:read``, like the trust reads). What still separates a read
token from a write one is the ``athena:write`` scope, granted only by the
consent screen's write box. To stop the connector, revoke every token
(``python -m scripts.revoke_mcp_tokens``): a write re-reads the live token
first (``bearer.revalidate_for_write``), so a revoked token stops writing
at once and stops reading within the bearer cache's five minutes.
"""

from flask import Blueprint

from config import Config

# ── Derived constants ───────────────────────────────────────────────────

# RFC 8707 canonical resource URI of the MCP endpoint. Built from the
# configured canonical origin, never from request.host.
MCP_RESOURCE: str = f"{Config.MCP_CANONICAL_ORIGIN}/mcp"

# Hard allowlist of OAuth redirect URIs (D-2): Claude's callback URLs only.
# Localhost is additionally accepted outside production (MCP Inspector) —
# see oauth.redirect_uri_allowed().
ALLOWED_REDIRECT_URIS: frozenset[str] = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
)

# MCP protocol revisions supported, newest first.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26")
# Per spec guidance, an absent MCP-Protocol-Version header means 2025-03-26.
DEFAULT_PROTOCOL_VERSION: str = "2025-03-26"

SCOPE_READ: str = "athena:read"
# Granted ONLY when the user ticks « autoriser l'écriture » on the French
# consent screen — never from the client's requested `scope` alone, so the
# page the user read and the grant that is minted can never disagree.
SCOPE_WRITE: str = "athena:write"
# Advertised in the RFC 8414 / RFC 9728 metadata. Every scope but read gates
# a write: tests/test_mcp_framework_guards checks WRITE_TOOLS membership
# against the non-read scopes of THIS tuple, so a scope joins the write gate
# by being listed here — and a tool declaring an unlisted scope fails it.
# (A third scope, « athena:comptabilite », gated the accounting tools behind
# their own consent box from lot 0a to 2026-10-05; the lawyer removed it —
# those tools are write tools like the others. A token still carrying the
# string — none was live at the removal — simply holds an unknown scope.)
SCOPES_SUPPORTED: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE)

# Token / code lifetimes (seconds).
ACCESS_TOKEN_TTL: int = 3600
REFRESH_TOKEN_TTL: int = 30 * 86400
AUTH_CODE_TTL: int = 300

# Origins allowed to send an Origin header to /mcp (DNS-rebinding defense).
ALLOWED_BROWSER_ORIGINS: frozenset[str] = frozenset(
    {
        "https://claude.ai",
        "https://claude.com",
        Config.MCP_CANONICAL_ORIGIN,
    }
)

# ── Blueprints ──────────────────────────────────────────────────────────

mcp_bp = Blueprint("mcp", __name__)
oauth_bp = Blueprint("mcp_oauth", __name__)


def register_mcp(app) -> None:
    """Attach routes and register both MCP blueprints on *app*.

    CSRF exemptions: the /mcp endpoint and the machine-facing OAuth
    endpoints (/oauth/register, /oauth/token, /oauth/revoke) are exempted
    at the view level in their modules; the /oauth/authorize POST (the one
    browser-origin form in the flow) keeps CSRF enforcement.
    """
    # Import for side effects: the modules attach their routes to the
    # blueprints defined above.
    from mcp import endpoint, oauth  # noqa: F401

    app.register_blueprint(oauth_bp)
    app.register_blueprint(mcp_bp)
