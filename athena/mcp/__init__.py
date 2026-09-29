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

Three kill switches, nested from the widest to the narrowest:

* ``MCP_ENABLED`` (default on) — when false, every route in both
  blueprints 404s.
* ``MCP_WRITE_ENABLED`` (default on) — when false, EVERY write tool
  disappears from ``tools/list`` and is refused at ``tools/call``, the
  accounting ones included, and the consent screen offers no write box at
  all; reads are unaffected.
* ``MCP_COMPTABILITE_ENABLED`` (default OFF) — when false, the accounting
  tools (``mcp.tools.ACCOUNTING_TOOLS``, the ``athena:comptabilite``
  scope) disappear and are refused the same way, and the consent screen
  does not offer their box. Money is fail-closed: a forgotten variable
  leaves accounting off. Since plan lot 5b six tools carry the scope —
  the ACCOUNTING family's five writes and the read ``get_admin_ledger`` —
  so ``true`` offers the box and, to a token granted it, the tools.
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
# The accounting grant (plan decision D1): trust and administration register
# entries. Its OWN unticked consent box, never implied by athena:write and
# never implying it — a token holding write alone cannot reach an accounting
# tool, by construction (required_scope + the tools/list filter + the
# write-time revalidation all demand THIS scope). Like write, it is added
# only from the visible checkbox in oauth.authorize_decision, offered only
# while write AND accounting are switched on AND at least one tool carries
# the scope (a box that grants nothing would be a false statement), frozen
# at issuance and copied verbatim across refresh rotation. An RFC 6749
# scope-token: ASCII, no accent — « comptabilite », not « comptabilité ».
SCOPE_COMPTABILITE: str = "athena:comptabilite"
# Advertised in the RFC 8414 / RFC 9728 metadata. Every scope but read gates
# a write: tests/test_mcp_framework_guards checks WRITE_TOOLS membership
# against the non-read scopes of THIS tuple, so a scope joins the write gate
# by being listed here — and a tool declaring an unlisted scope fails it.
SCOPES_SUPPORTED: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_COMPTABILITE)

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
    """True when the write tools are live (``MCP_WRITE_ENABLED``).

    The MASTER write switch: it governs every member of
    ``mcp.tools.WRITE_TOOLS``, the accounting tools included.

    Read through ``current_app.config`` so the switch can be flipped by a
    redeploy without touching code, and falls back to :class:`Config` when
    called outside an application context (tests, scripts).
    """
    try:
        return bool(current_app.config.get("MCP_WRITE_ENABLED", Config.MCP_WRITE_ENABLED))
    except RuntimeError:  # outside an app context
        return bool(Config.MCP_WRITE_ENABLED)


def comptabilite_enabled() -> bool:
    """True when the accounting tools may be live (``MCP_COMPTABILITE_ENABLED``).

    Necessary, not sufficient: an accounting tool is a write, so it is ALSO
    off whenever :func:`write_enabled` is false. Same resolution as
    :func:`write_enabled` — the application config first, :class:`Config`
    outside an application context — and the same fail-closed default seen
    from the other side: ``Config`` defaults it to FALSE.
    """
    try:
        return bool(current_app.config.get(
            "MCP_COMPTABILITE_ENABLED", Config.MCP_COMPTABILITE_ENABLED))
    except RuntimeError:  # outside an app context
        return bool(Config.MCP_COMPTABILITE_ENABLED)


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
