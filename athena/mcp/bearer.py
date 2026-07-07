"""Bearer-token authentication for the /mcp endpoint.

Brute-force / Firestore-read protection mirrors ``dav/dav_auth.py``:

1. Fail fast (no Firestore) on missing, malformed, or oversized tokens.
2. An in-memory failed-attempt tracker keyed by client IP returns 429 after
   ``_MAX_FAILURES`` invalid tokens within ``_FAILURE_WINDOW_SECONDS`` —
   before Firestore is touched.
3. A short-lived success cache (keyed by HMAC-SHA-256 of the token under an
   ephemeral per-process random key) lets an active Claude conversation
   skip one Firestore read per request. Cache entries never outlive the
   token's own ``expire_at``; revocation therefore takes effect within at
   most ``_SUCCESS_CACHE_TTL_SECONDS`` on a warm instance.

Both stores are per-instance memory — a brake, not a guarantee (same
caveat as the DAV brake).

The bare 401 with a ``resource_metadata`` challenge is what triggers
Claude's OAuth discovery — it is a feature, not an error path to minimize.
"""

import functools
import hashlib
import hmac
import secrets
import threading
import time
from typing import Callable, Optional

from flask import Response, current_app, jsonify, request

from config import Config
from mcp import ALLOWED_BROWSER_ORIGINS, MCP_RESOURCE, SCOPE_READ
from mcp import store
from utils.logging_setup import log_mcp_event

# ── Throttling parameters ──────────────────────────────────────────────
_MAX_TOKEN_LENGTH = 512
_MAX_FAILURES = 20
_FAILURE_WINDOW_SECONDS = 15 * 60
_MAX_TRACKED_IPS = 1000
_SUCCESS_CACHE_TTL_SECONDS = 5 * 60

# Ephemeral MAC key: random per process, never persisted. A leaked cache
# entry cannot be brute-forced offline without it.
_CACHE_HMAC_KEY = secrets.token_bytes(32)

_lock = threading.Lock()
# Client IP -> list of failure timestamps within the window.
_failed_attempts: dict[str, list[float]] = {}
# HMAC-SHA-256 of the token -> cache-entry expiry timestamp.
_success_cache: dict[bytes, float] = {}


def _client_ip() -> str:
    """Return the client IP, preferring Cloudflare's CF-Connecting-IP."""
    return (
        request.headers.get("CF-Connecting-IP")
        or request.remote_addr
        or "unknown"
    )


def _token_digest(token: str) -> bytes:
    return hmac.new(_CACHE_HMAC_KEY, token.encode("utf-8"), hashlib.sha256).digest()


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        attempts = [
            t
            for t in _failed_attempts.get(ip, [])
            if now - t < _FAILURE_WINDOW_SECONDS
        ]
        if attempts:
            _failed_attempts[ip] = attempts
        else:
            _failed_attempts.pop(ip, None)
        return len(attempts) >= _MAX_FAILURES


def _record_failure(ip: str) -> None:
    now = time.time()
    with _lock:
        _failed_attempts.setdefault(ip, []).append(now)
        if len(_failed_attempts) > _MAX_TRACKED_IPS:
            by_recency = sorted(
                _failed_attempts, key=lambda k: max(_failed_attempts[k])
            )
            for key in by_recency[: len(_failed_attempts) - _MAX_TRACKED_IPS]:
                del _failed_attempts[key]


def _check_success_cache(token: str) -> bool:
    now = time.time()
    digest = _token_digest(token)
    with _lock:
        for cached, expiry in list(_success_cache.items()):
            if expiry <= now:
                del _success_cache[cached]
                continue
            if hmac.compare_digest(cached, digest):
                return True
    return False


def _record_success(ip: str, token: str, token_expire_ts: float) -> None:
    """Reset the IP's failures and cache the verdict (capped at token expiry)."""
    now = time.time()
    cache_expiry = min(now + _SUCCESS_CACHE_TTL_SECONDS, token_expire_ts)
    with _lock:
        _failed_attempts.pop(ip, None)
        for cached in [d for d, exp in _success_cache.items() if exp <= now]:
            del _success_cache[cached]
        if cache_expiry > now:
            _success_cache[_token_digest(token)] = cache_expiry


def reset_brake_state() -> None:
    """Clear both in-memory stores (test isolation helper)."""
    with _lock:
        _failed_attempts.clear()
        _success_cache.clear()


# ── Responses ───────────────────────────────────────────────────────────

def _metadata_url() -> str:
    origin = current_app.config.get(
        "MCP_CANONICAL_ORIGIN", Config.MCP_CANONICAL_ORIGIN
    )
    return f"{origin}/.well-known/oauth-protected-resource/mcp"


def _challenge_value(error: Optional[str] = None) -> str:
    parts = ['Bearer realm="Pallas Athena"', f'resource_metadata="{_metadata_url()}"']
    if error:
        parts.append(f'error="{error}"')
    return ", ".join(parts)


def _unauthorized(error: Optional[str] = None) -> Response:
    resp = jsonify({"error": error or "unauthorized"})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = _challenge_value(error)
    return resp


def _forbidden(error: str) -> Response:
    resp = jsonify({"error": error})
    resp.status_code = 403
    resp.headers["WWW-Authenticate"] = _challenge_value(error)
    return resp


def _too_many_requests() -> Response:
    resp = jsonify({"error": "too_many_requests"})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(_FAILURE_WINDOW_SECONDS)
    return resp


def _service_unavailable() -> Response:
    resp = jsonify({"error": "temporarily_unavailable"})
    resp.status_code = 503
    return resp


# ── Decorator ───────────────────────────────────────────────────────────

def mcp_auth_required(f: Callable) -> Callable:
    """Require a valid ``Authorization: Bearer`` access token on /mcp."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # DNS-rebinding defense (MCP spec): a browser-sent Origin must be
        # one we trust; absent Origin (server-to-server) is allowed.
        origin = request.headers.get("Origin")
        if origin and origin not in ALLOWED_BROWSER_ORIGINS:
            log_mcp_event("mcp_auth_failure", "refused", reason="origin_forbidden")
            return _forbidden("forbidden_origin")

        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            # Discovery path: this 401 tells Claude where the OAuth
            # metadata lives. Expected on every first connection.
            log_mcp_event("mcp_auth_failure", "refused", reason="missing_token")
            return _unauthorized()
        if len(token) > _MAX_TOKEN_LENGTH:
            log_mcp_event("mcp_auth_failure", "refused", reason="oversized_token")
            return _unauthorized("invalid_token")

        ip = _client_ip()
        if _is_rate_limited(ip):
            log_mcp_event("mcp_brake_engaged", "refused", reason="invalid_token_flood")
            return _too_many_requests()

        if _check_success_cache(token):
            return f(*args, **kwargs)

        token_hash = store.sha256_hex(token)
        try:
            doc = store.get_token(token_hash)
        except Exception:
            from utils.logging_setup import log_unexpected

            log_unexpected("mcp bearer token lookup failed")
            return _service_unavailable()

        if (
            doc is None
            or doc.get("token_type") != "access"
            or doc.get("revoked")
            or store.is_expired(doc)
        ):
            _record_failure(ip)
            log_mcp_event("mcp_auth_failure", "refused", reason="invalid_token")
            return _unauthorized("invalid_token")

        scopes = str(doc.get("scope") or "").split()
        if SCOPE_READ not in scopes:
            log_mcp_event(
                "mcp_auth_failure",
                "refused",
                reason="insufficient_scope",
                client_id=doc.get("client_id"),
            )
            return _forbidden("insufficient_scope")

        resource = doc.get("resource")
        if resource and resource != MCP_RESOURCE:
            _record_failure(ip)
            log_mcp_event(
                "mcp_auth_failure",
                "refused",
                reason="resource_mismatch",
                client_id=doc.get("client_id"),
            )
            return _unauthorized("invalid_token")

        expire_at = doc.get("expire_at")
        _record_success(ip, token, expire_at.timestamp())
        # At most one write per success-cache window, not one per request.
        store.stamp_token_last_used(token_hash)

        return f(*args, **kwargs)

    return decorated
