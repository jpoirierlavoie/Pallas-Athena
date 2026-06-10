"""HTTP Basic Authentication for DAV endpoints (separate from Firebase Auth).

Brute-force / CPU-DoS protection
--------------------------------
bcrypt verification is deliberately slow, so this module throttles it:

1. Fail fast (no bcrypt) on malformed or oversized credentials.
2. An in-memory failed-attempt tracker keyed by client IP returns 429 after
   ``_MAX_FAILURES`` failures within ``_FAILURE_WINDOW_SECONDS``.
3. A short-lived success cache (SHA-256 digest of the credentials, compared
   with ``hmac.compare_digest``) lets DavX5's frequent polling skip bcrypt.

NOTE: both stores live in process memory. App Engine runs at most two
instances of this app, so the state is per-instance and resets on restart —
this is a brake on brute force and bcrypt CPU exhaustion, not a guarantee.
"""

import functools
import hashlib
import hmac
import threading
import time
from typing import Callable

import bcrypt
from flask import Response, request

from config import Config

# ── Throttling parameters ──────────────────────────────────────────────────
_MAX_CREDENTIAL_LENGTH = 256
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 15 * 60
_MAX_TRACKED_IPS = 1000
_SUCCESS_CACHE_TTL_SECONDS = 5 * 60

_lock = threading.Lock()
# Client IP -> list of failure timestamps (time.time()) within the window.
_failed_attempts: dict[str, list[float]] = {}
# SHA-256 digest of (username, password) -> cache-entry expiry timestamp.
_success_cache: dict[bytes, float] = {}


def _client_ip() -> str:
    """Return the client IP, preferring Cloudflare's CF-Connecting-IP."""
    return (
        request.headers.get("CF-Connecting-IP")
        or request.remote_addr
        or "unknown"
    )


def _credentials_digest(username: str, password: str) -> bytes:
    """Return a SHA-256 digest binding username and password together."""
    return hashlib.sha256(
        f"{username}\x00{password}".encode("utf-8")
    ).digest()


def _is_rate_limited(ip: str) -> bool:
    """Return True when *ip* exceeded the failure budget within the window."""
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
    """Record a failed authentication attempt for *ip* (bounded store)."""
    now = time.time()
    with _lock:
        _failed_attempts.setdefault(ip, []).append(now)
        # Cap the tracker size: evict the IPs with the oldest latest-failure.
        if len(_failed_attempts) > _MAX_TRACKED_IPS:
            by_recency = sorted(
                _failed_attempts, key=lambda k: max(_failed_attempts[k])
            )
            for key in by_recency[: len(_failed_attempts) - _MAX_TRACKED_IPS]:
                del _failed_attempts[key]


def _check_success_cache(username: str, password: str) -> bool:
    """Return True when these credentials were bcrypt-verified recently."""
    now = time.time()
    digest = _credentials_digest(username, password)
    with _lock:
        for cached, expiry in list(_success_cache.items()):
            if expiry <= now:
                del _success_cache[cached]
                continue
            if hmac.compare_digest(cached, digest):
                return True
    return False


def _record_success(ip: str, username: str, password: str) -> None:
    """Reset the IP's failure count and cache the verified credentials."""
    now = time.time()
    with _lock:
        _failed_attempts.pop(ip, None)
        for cached in [d for d, exp in _success_cache.items() if exp <= now]:
            del _success_cache[cached]
        _success_cache[_credentials_digest(username, password)] = (
            now + _SUCCESS_CACHE_TTL_SECONDS
        )


def _check_credentials(username: str, password: str) -> bool:
    """Verify DAV credentials against configured username and bcrypt hash."""
    if not Config.DAV_USERNAME or not Config.DAV_PASSWORD_HASH:
        return False
    if username != Config.DAV_USERNAME:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            Config.DAV_PASSWORD_HASH.encode("utf-8"),
        )
    except Exception:
        return False


def _unauthorized_response() -> Response:
    """Return a 401 response with WWW-Authenticate header."""
    resp = Response(
        "Unauthorized",
        status=401,
        content_type="text/plain; charset=utf-8",
    )
    resp.headers["WWW-Authenticate"] = 'Basic realm="Pallas Athena"'
    return resp


def _too_many_requests_response() -> Response:
    """Return a 429 response with a Retry-After header."""
    resp = Response(
        "Trop de tentatives. Réessayez plus tard.",
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    resp.headers["Retry-After"] = str(_FAILURE_WINDOW_SECONDS)
    return resp


def dav_auth_required(f: Callable) -> Callable:
    """Decorator: require valid HTTP Basic Auth on every DAV request."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        # Fail fast — malformed/missing header or oversized credentials —
        # before any bcrypt work.
        if not auth or not auth.username or not auth.password:
            return _unauthorized_response()
        if (
            len(auth.username) > _MAX_CREDENTIAL_LENGTH
            or len(auth.password) > _MAX_CREDENTIAL_LENGTH
        ):
            return _unauthorized_response()

        ip = _client_ip()
        if _is_rate_limited(ip):
            return _too_many_requests_response()

        # Recently bcrypt-verified credentials skip the bcrypt round-trip.
        if _check_success_cache(auth.username, auth.password):
            return f(*args, **kwargs)

        if not _check_credentials(auth.username, auth.password):
            _record_failure(ip)
            return _unauthorized_response()

        _record_success(ip, auth.username, auth.password)
        return f(*args, **kwargs)

    return decorated
