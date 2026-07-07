"""MCP (Model Context Protocol) layer — Phase I.

Exposes Pallas Athena's data to Claude as a custom connector:

* ``oauth_bp`` — embedded OAuth 2.1 authorization server (RFC 8414/9728
  metadata, RFC 7591 Dynamic Client Registration, consent screen behind the
  Firebase session, token endpoint with PKCE + refresh rotation, RFC 7009
  revocation).
* ``mcp_bp`` — ``POST /mcp``: a stateless, JSON-response-mode Streamable
  HTTP server (initialize / ping / tools list + call). No SSE, no sessions.

Everything is read-only in v1 — no CTag bumping is ever needed here.
The ``MCP_ENABLED`` config flag is a kill switch: when false, every route
in both blueprints returns 404.
"""

from flask import Blueprint, abort, current_app

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

SCOPES_SUPPORTED: tuple[str, ...] = ("athena:read",)
SCOPE_READ: str = "athena:read"

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


def _kill_switch() -> None:
    """404 every MCP/OAuth route when the MCP_ENABLED kill switch is off."""
    if not current_app.config.get("MCP_ENABLED", True):
        from utils.logging_setup import log_mcp_event

        log_mcp_event("mcp_disabled_hit", "refused", reason="kill_switch")
        abort(404)


mcp_bp.before_request(_kill_switch)
oauth_bp.before_request(_kill_switch)


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
