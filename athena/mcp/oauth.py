"""Embedded OAuth 2.1 authorization server for the MCP connector.

Endpoints: RFC 8414 / RFC 9728 metadata documents, RFC 7591 Dynamic Client
Registration (open but neutered by a hard redirect-URI allowlist), the
consent screen (behind the Firebase session + MFA via ``@login_required``),
the token endpoint (authorization_code + PKCE S256, refresh rotation with
family revocation on replay), and RFC 7009 revocation.

Public clients only (``token_endpoint_auth_method: none``) — PKCE is the
proof of possession. Never log tokens, codes, or verifiers.
"""

import base64
import hashlib
import hmac
import re
import time as _time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from flask import (
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
)

from auth import login_required
from config import Config
from mcp import (
    ALLOWED_REDIRECT_URIS,
    MCP_RESOURCE,
    SCOPE_READ,
    SCOPES_SUPPORTED,
    oauth_bp,
)
from mcp import store
from security import csrf, limiter, sanitize
from utils.logging_setup import log_mcp_event, log_unexpected

_MAX_CLIENT_NAME_LENGTH = 200
_DEFAULT_CLIENT_NAME = "Client MCP"
_MAX_PARAM_LENGTH = 2048
# RFC 7636: verifier is 43-128 unreserved chars; an S256 challenge is the
# 43-char base64url (no padding) encoding of a SHA-256 digest.
_CODE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


def _origin() -> str:
    return current_app.config.get("MCP_CANONICAL_ORIGIN", Config.MCP_CANONICAL_ORIGIN)


def redirect_uri_allowed(uri: str) -> bool:
    """True when *uri* is a Claude callback (or localhost outside prod)."""
    if uri in ALLOWED_REDIRECT_URIS:
        return True
    if current_app.config.get("ENV") != "production":
        parsed = urlparse(uri)
        if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
            return True
    return False


# ── Metadata documents (RFC 8414 / RFC 9728) ────────────────────────────

@oauth_bp.route("/.well-known/oauth-authorization-server", methods=["GET"])
def authorization_server_metadata() -> Response:
    origin = _origin()
    return jsonify(
        {
            "issuer": origin,
            "authorization_endpoint": f"{origin}/oauth/authorize",
            "token_endpoint": f"{origin}/oauth/token",
            "registration_endpoint": f"{origin}/oauth/register",
            "revocation_endpoint": f"{origin}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SCOPES_SUPPORTED),
        }
    )


def _protected_resource_doc() -> Response:
    origin = _origin()
    return jsonify(
        {
            "resource": f"{origin}/mcp",
            "authorization_servers": [origin],
            "scopes_supported": list(SCOPES_SUPPORTED),
            "bearer_methods_supported": ["header"],
        }
    )


@oauth_bp.route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
def protected_resource_metadata() -> Response:
    return _protected_resource_doc()


@oauth_bp.route("/.well-known/oauth-protected-resource", methods=["GET"])
def protected_resource_metadata_fallback() -> Response:
    # Fallback for clients that do not path-scope the metadata lookup.
    return _protected_resource_doc()


# ── Dynamic Client Registration (RFC 7591) ──────────────────────────────

@oauth_bp.route("/oauth/register", methods=["POST"])
@csrf.exempt
@limiter.limit("10 per hour")
def register() -> tuple[Response, int]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return (
            jsonify(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "Request body must be a JSON object.",
                }
            ),
            400,
        )

    redirect_uris = body.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or not all(isinstance(u, str) for u in redirect_uris)
    ):
        return (
            jsonify(
                {
                    "error": "invalid_client_metadata",
                    "error_description": "redirect_uris must be a non-empty array of strings.",
                }
            ),
            400,
        )
    # The single check that makes open DCR harmless (D-2): only Claude's
    # callback URLs (and localhost outside production) may register.
    if not all(redirect_uri_allowed(u) for u in redirect_uris):
        return (
            jsonify(
                {
                    "error": "invalid_redirect_uri",
                    "error_description": "redirect_uris contains an unsupported callback URL.",
                }
            ),
            400,
        )

    raw_name = body.get("client_name")
    client_name = sanitize(str(raw_name), max_length=_MAX_CLIENT_NAME_LENGTH).strip() if raw_name else ""
    client_name = client_name or _DEFAULT_CLIENT_NAME

    try:
        client = store.create_client(client_name, redirect_uris)
    except Exception:
        log_unexpected("mcp client registration write failed")
        return jsonify({"error": "invalid_client_metadata"}), 500

    log_mcp_event(
        "mcp_client_registered", "success", client_id=client["client_id"]
    )
    return (
        jsonify(
            {
                "client_id": client["client_id"],
                "client_id_issued_at": int(_time.time()),
                "client_name": client["client_name"],
                "redirect_uris": client["redirect_uris"],
                "token_endpoint_auth_method": "none",
                "grant_types": client["grant_types"],
                "response_types": client["response_types"],
            }
        ),
        201,
    )


# ── Authorization endpoint (consent screen) ─────────────────────────────

class _PageError(Exception):
    """Pre-redirect-validation failure: render a French error page."""

    def __init__(self, message_fr: str):
        super().__init__(message_fr)
        self.message_fr = message_fr


class _RedirectError(Exception):
    """Post-validation failure: 302 back to the client per RFC 6749."""

    def __init__(self, error: str, description: str = ""):
        super().__init__(error)
        self.error = error
        self.description = description


def _param(source: dict, name: str) -> str:
    value = source.get(name, "")
    if not isinstance(value, str):
        return ""
    return value[:_MAX_PARAM_LENGTH]


def _validate_authorize_request(source: dict) -> dict:
    """Validate authorize parameters from *source* (query or form).

    Returns the validated parameter set. Raises :class:`_PageError` before
    the redirect URI is trusted, :class:`_RedirectError` after.
    """
    client_id = _param(source, "client_id")
    if not client_id:
        raise _PageError("Paramètre client_id manquant.")
    try:
        client = store.get_client(client_id)
    except Exception:
        log_unexpected("mcp client lookup failed during authorize")
        raise _PageError("Erreur interne. Veuillez réessayer.")
    if client is None:
        raise _PageError("Client OAuth inconnu.")

    redirect_uri = _param(source, "redirect_uri")
    if not redirect_uri or redirect_uri not in client.get("redirect_uris", []):
        raise _PageError("URI de redirection non autorisée.")

    # From here on, errors are reported to the validated redirect_uri.
    state = _param(source, "state")

    if _param(source, "response_type") != "code":
        raise _RedirectError("unsupported_response_type")

    code_challenge = _param(source, "code_challenge")
    if not code_challenge or not _CODE_CHALLENGE_RE.match(code_challenge):
        raise _RedirectError("invalid_request", "code_challenge (S256) is required")
    if _param(source, "code_challenge_method") != "S256":
        raise _RedirectError(
            "invalid_request", "code_challenge_method must be S256"
        )

    scope = _param(source, "scope")
    if scope:
        granted = [s for s in scope.split() if s in SCOPES_SUPPORTED]
        if not granted:
            raise _RedirectError("invalid_scope")
        granted_scope = " ".join(granted)
    else:
        granted_scope = SCOPE_READ

    resource = _param(source, "resource")
    if resource and resource != MCP_RESOURCE:
        raise _RedirectError("invalid_target")

    return {
        "client_id": client_id,
        "client_name": client.get("client_name", _DEFAULT_CLIENT_NAME),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": granted_scope,
        "resource": resource,
        "state": state,
    }


def _redirect_with_error(redirect_uri: str, exc: _RedirectError, state: str) -> Response:
    params = {"error": exc.error}
    if exc.description:
        params["error_description"] = exc.description
    if state:
        params["state"] = state
    return redirect(f"{redirect_uri}?{urlencode(params)}", code=302)


def _render_error_page(message_fr: str) -> tuple[str, int]:
    return (
        render_template("mcp/consent.html", error_message=message_fr, params=None),
        400,
    )


@oauth_bp.route("/oauth/authorize", methods=["GET"])
@login_required
def authorize() -> Any:
    try:
        params = _validate_authorize_request(request.args)
    except _PageError as exc:
        return _render_error_page(exc.message_fr)
    except _RedirectError as exc:
        redirect_uri = _param(request.args, "redirect_uri")
        return _redirect_with_error(redirect_uri, exc, _param(request.args, "state"))
    return render_template("mcp/consent.html", error_message=None, params=params)


@oauth_bp.route("/oauth/authorize", methods=["POST"])
@login_required
def authorize_decision() -> Any:
    # CSRF is enforced here (the one browser-origin POST in the flow).
    # Hidden fields are attacker-modifiable — re-validate everything.
    try:
        params = _validate_authorize_request(request.form)
    except _PageError as exc:
        return _render_error_page(exc.message_fr)
    except _RedirectError as exc:
        redirect_uri = _param(request.form, "redirect_uri")
        return _redirect_with_error(redirect_uri, exc, _param(request.form, "state"))

    if request.form.get("decision") != "allow":
        log_mcp_event(
            "mcp_consent", "refused", client_id=params["client_id"], reason="denied"
        )
        return _redirect_with_error(
            params["redirect_uri"], _RedirectError("access_denied"), params["state"]
        )

    try:
        code = store.create_auth_code(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            scope=params["scope"],
            code_challenge=params["code_challenge"],
            resource=params["resource"] or None,
        )
    except Exception:
        log_unexpected("mcp authorization code write failed")
        return _render_error_page("Erreur interne. Veuillez réessayer.")

    log_mcp_event("mcp_consent", "success", client_id=params["client_id"])
    query = {"code": code}
    if params["state"]:
        query["state"] = params["state"]
    return redirect(f"{params['redirect_uri']}?{urlencode(query)}", code=302)


# ── Token endpoint ──────────────────────────────────────────────────────

def _token_error(
    error: str, status: int = 400, description: str = ""
) -> tuple[Response, int]:
    body: dict[str, str] = {"error": error}
    if description:
        body["error_description"] = description
    return jsonify(body), status


def _token_response(pair: dict) -> Response:
    return jsonify(
        {
            "access_token": pair["access_token"],
            "token_type": "Bearer",
            "expires_in": pair["expires_in"],
            "refresh_token": pair["refresh_token"],
            "scope": pair["scope"],
        }
    )


def _pkce_matches(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, code_challenge)


@oauth_bp.route("/oauth/token", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per hour")
def token() -> tuple[Response, int] | Response:
    grant_type = request.form.get("grant_type", "")
    try:
        if grant_type == "authorization_code":
            return _grant_authorization_code()
        if grant_type == "refresh_token":
            return _grant_refresh_token()
    except Exception:
        log_unexpected("mcp token endpoint failure")
        return _token_error("invalid_request", 500, "Internal error.")
    log_mcp_event(
        "mcp_token_refused", "refused", reason="unsupported_grant_type"
    )
    return _token_error("unsupported_grant_type")


def _refused(reason: str, client_id: Optional[str] = None) -> tuple[Response, int]:
    log_mcp_event("mcp_token_refused", "refused", reason=reason, client_id=client_id)
    return _token_error("invalid_grant")


def _grant_authorization_code() -> tuple[Response, int] | Response:
    code = request.form.get("code", "")
    redirect_uri = request.form.get("redirect_uri", "")
    client_id = request.form.get("client_id", "")
    code_verifier = request.form.get("code_verifier", "")
    if not code or not redirect_uri or not client_id or not code_verifier:
        return _token_error(
            "invalid_request",
            400,
            "code, redirect_uri, client_id and code_verifier are required.",
        )
    if not _CODE_VERIFIER_RE.match(code_verifier):
        return _token_error("invalid_request", 400, "Malformed code_verifier.")
    resource = request.form.get("resource", "")
    if resource and resource != MCP_RESOURCE:
        return _token_error("invalid_target")

    code_hash = store.sha256_hex(code)
    doc = store.get_auth_code(code_hash)
    if doc is None:
        return _refused("code_unknown", client_id)
    if doc.get("used"):
        # Replayed code: kill every token minted from it (OAuth 2.1).
        family_id = doc.get("family_id")
        if family_id:
            revoked = store.revoke_family(family_id)
            log_mcp_event(
                "mcp_family_revoked",
                "success",
                client_id=client_id,
                reason="code_reused",
                revoked_count=revoked,
            )
        return _refused("code_reused", client_id)
    if store.is_expired(doc):
        return _refused("code_expired", client_id)
    if doc.get("client_id") != client_id:
        return _refused("client_mismatch", client_id)
    if doc.get("redirect_uri") != redirect_uri:
        return _refused("redirect_uri_mismatch", client_id)
    if not _pkce_matches(code_verifier, doc.get("code_challenge", "")):
        return _refused("pkce_mismatch", client_id)

    # Single-use: burn the code atomically BEFORE issuing tokens.
    family_id = uuid.uuid4().hex
    consumed, already_used = store.consume_auth_code(code_hash, family_id)
    if consumed is None:
        return _refused("code_unknown", client_id)
    if already_used:
        prior_family = consumed.get("family_id")
        if prior_family:
            revoked = store.revoke_family(prior_family)
            log_mcp_event(
                "mcp_family_revoked",
                "success",
                client_id=client_id,
                reason="code_reused",
                revoked_count=revoked,
            )
        return _refused("code_reused", client_id)

    pair = store.create_token_pair(
        client_id=client_id,
        scope=doc.get("scope", SCOPE_READ),
        resource=doc.get("resource"),
        family_id=family_id,
    )
    store.touch_client(client_id)
    log_mcp_event(
        "mcp_token_issued",
        "success",
        client_id=client_id,
        grant="authorization_code",
    )
    return _token_response(pair)


def _grant_refresh_token() -> tuple[Response, int] | Response:
    refresh_token = request.form.get("refresh_token", "")
    client_id = request.form.get("client_id", "")
    if not refresh_token or not client_id:
        return _token_error(
            "invalid_request", 400, "refresh_token and client_id are required."
        )
    resource = request.form.get("resource", "")
    if resource and resource != MCP_RESOURCE:
        return _token_error("invalid_target")

    token_hash = store.sha256_hex(refresh_token)
    doc = store.get_token(token_hash)
    if doc is None or doc.get("token_type") != "refresh":
        return _refused("refresh_unknown", client_id)
    if doc.get("client_id") != client_id:
        return _refused("client_mismatch", client_id)
    if doc.get("revoked"):
        # Replay of a rotated refresh token: revoke the whole family.
        revoked = store.revoke_family(doc.get("family_id", ""))
        log_mcp_event(
            "mcp_family_revoked",
            "success",
            client_id=client_id,
            reason="refresh_replayed",
            revoked_count=revoked,
        )
        return _refused("refresh_replayed", client_id)
    if store.is_expired(doc):
        return _refused("refresh_expired", client_id)

    # Rotation: atomically claim (revoke) the presented token, then issue
    # the successor pair in the same family. Losing a race means a
    # concurrent rotation/replay — kill the family.
    if not store.claim_refresh_for_rotation(token_hash):
        revoked = store.revoke_family(doc.get("family_id", ""))
        log_mcp_event(
            "mcp_family_revoked",
            "success",
            client_id=client_id,
            reason="refresh_replayed",
            revoked_count=revoked,
        )
        return _refused("refresh_replayed", client_id)

    pair = store.create_token_pair(
        client_id=client_id,
        scope=doc.get("scope", SCOPE_READ),
        resource=doc.get("resource"),
        family_id=doc.get("family_id"),
    )
    store.set_rotated_to(token_hash, pair["refresh_token_hash"])
    store.touch_client(client_id)
    log_mcp_event(
        "mcp_token_issued", "success", client_id=client_id, grant="refresh_token"
    )
    return _token_response(pair)


# ── Revocation (RFC 7009) ───────────────────────────────────────────────

@oauth_bp.route("/oauth/revoke", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per hour")
def revoke() -> tuple[Response, int] | Response:
    token_value = request.form.get("token", "")
    if not token_value:
        return _token_error("invalid_request", 400, "token is required.")
    try:
        token_hash = store.sha256_hex(token_value)
        doc = store.get_token(token_hash)
        if doc is not None:
            if doc.get("token_type") == "refresh":
                revoked = store.revoke_family(doc.get("family_id", ""))
                log_mcp_event(
                    "mcp_family_revoked",
                    "success",
                    client_id=doc.get("client_id"),
                    reason="revocation_request",
                    revoked_count=revoked,
                )
            elif store.revoke_token_hash(token_hash):
                log_mcp_event(
                    "mcp_token_revoked",
                    "success",
                    client_id=doc.get("client_id"),
                    reason="revocation_request",
                )
    except Exception:
        # RFC 7009: the response never discloses whether the token existed.
        log_unexpected("mcp revocation failure")
    return jsonify({})
