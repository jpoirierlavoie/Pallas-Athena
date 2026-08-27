"""MCP (Model Context Protocol) layer — Phase I.

Exposes Pallas Athena's data to Claude as a custom connector:

* ``oauth_bp`` — embedded OAuth 2.1 authorization server (RFC 8414/9728
  metadata, RFC 7591 Dynamic Client Registration, consent screen behind the
  Firebase session, token endpoint with PKCE + refresh rotation, RFC 7009
  revocation).
* ``mcp_bp`` — ``POST /mcp``: a stateless, JSON-response-mode Streamable
  HTTP server (initialize / ping / tools list + call). No SSE, no sessions.

**Reads dominate; nothing is ever deleted.** The write tools are pinned in
:data:`mcp.tools.WRITE_TOOLS`, in three families since lot Q (August 2026,
the historical import): CREATE (notes, tasks, hearings, time entries,
expenses, parties, dossiers, plus the recorders ``complete_dossier`` /
``record_signification`` / ``record_prescription_event``), CORRECT (the
four ``update_*`` tools and ``complete_task``), and IMPORT
(``import_invoice``). :data:`mcp.tools.EDIT_TOOLS` is the subset that
REPLACES a stored value, and it is what derives ``destructiveHint`` — the
annotation stopped being a family constant the day an edit shipped.
No tool deletes anything, sets an invoice status, or records a payment.

Notes, tasks and hearings are DAV-exposed per dossier, and parties through
the CardDAV addressbook; the models never bump a CTag — bumping lives in the
caller. **Every DAV-exposed write on a tool path MUST call
``bump_ctag(collection_for(dossier_id))``, or ``bump_ctag("parties")`` for a
contact**, or the item lands in Firestore, shows up in the web UI, and DavX5
silently never re-syncs it. All writes run through
``mcp/write_support.run_write`` (``idempotency_key``).

Two independent kill switches, both defaulting to on:

* ``MCP_ENABLED`` — when false, every route in both blueprints 404s.
* ``MCP_WRITE_ENABLED`` — when false, the write tools disappear from
  ``tools/list`` and are refused at ``tools/call``; reads are unaffected.
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

SCOPE_READ: str = "athena:read"
# Granted ONLY when the user ticks « autoriser l'écriture » on the French
# consent screen — never from the client's requested `scope` alone, so the
# page the user read and the grant that is minted can never disagree.
SCOPE_WRITE: str = "athena:write"
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


def _kill_switch() -> None:
    """404 every MCP/OAuth route when the MCP_ENABLED kill switch is off."""
    if not current_app.config.get("MCP_ENABLED", True):
        from utils.logging_setup import log_mcp_event

        log_mcp_event("mcp_disabled_hit", "refused", reason="kill_switch")
        abort(404)


mcp_bp.before_request(_kill_switch)
oauth_bp.before_request(_kill_switch)


def write_enabled() -> bool:
    """True when the note-write tools are live (``MCP_WRITE_ENABLED``).

    Read through ``current_app.config`` so the switch can be flipped by a
    redeploy without touching code, and falls back to :class:`Config` when
    called outside an application context (tests, scripts).
    """
    try:
        return bool(current_app.config.get("MCP_WRITE_ENABLED", Config.MCP_WRITE_ENABLED))
    except RuntimeError:  # outside an app context
        return bool(Config.MCP_WRITE_ENABLED)


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
