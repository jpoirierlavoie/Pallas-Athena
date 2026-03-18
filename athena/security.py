"""Security middleware: headers, CSRF, rate limiting, input sanitization, App Check."""

import re
from typing import Optional

from flask import Flask, Response, abort, current_app, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
csrf = CSRFProtect()

# ---------------------------------------------------------------------------
# Rate limiter (in-memory default; swap to Firestore for multi-instance)
# ---------------------------------------------------------------------------
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net https://unpkg.com "
    "https://www.gstatic.com https://apis.google.com "
    "https://www.google.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob: https://*.googleapis.com https://storage.googleapis.com; "
    "connect-src 'self' https://*.googleapis.com https://*.firebaseio.com "
    "https://identitytoolkit.googleapis.com https://storage.googleapis.com "
    "https://content-firebaseappcheck.googleapis.com "
    "https://www.google.com https://recaptchaenterprise.googleapis.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "frame-src https://*.firebaseapp.com https://storage.googleapis.com blob: "
    "https://www.google.com https://recaptcha.google.com; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def _add_security_headers(response: Response) -> Response:
    """Attach hardened security headers to every response."""
    h = response.headers
    h["Content-Security-Policy-Report-Only"] = CSP
    h["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains; preload"
    )
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    h["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    h["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Request size guard (non-upload routes capped at 1 MB)
# ---------------------------------------------------------------------------
UPLOAD_PATHS = ("/documents/upload",)


def _enforce_request_size() -> Optional[Response]:
    """Reject oversized requests for non-upload and non-DAV endpoints."""
    # DAV endpoints handle their own payloads (vCard, iCal); exempt them.
    if request.path.startswith("/dav/") or request.path.startswith("/.well-known/"):
        return None
    if request.content_length and request.path not in UPLOAD_PATHS:
        if request.content_length > 1 * 1024 * 1024:  # 1 MB
            abort(413)
    return None


# ---------------------------------------------------------------------------
# Input sanitization utility
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def sanitize(value: str, max_length: int = 1000) -> str:
    """Strip HTML tags and truncate.  Output escaping is handled by Jinja2."""
    cleaned = _TAG_RE.sub("", value)
    return cleaned[:max_length]


# ---------------------------------------------------------------------------
# App Check server-side verification
# ---------------------------------------------------------------------------
_APPCHECK_EXEMPT_PREFIXES = (
    "/static/",
    "/dav/",
    "/.well-known/",
    "/auth/",
)


def _verify_app_check() -> Optional[Response]:
    """Verify Firebase App Check token on HTMX requests.

    Full page loads (non-HTMX) are protected by session + CSRF.
    HTMX partial requests must include a valid App Check token.
    """
    # Only enforce on HTMX requests (initiated by JS, token available)
    if not request.headers.get("HX-Request"):
        return None

    # Skip exempt paths
    for prefix in _APPCHECK_EXEMPT_PREFIXES:
        if request.path.startswith(prefix):
            return None

    # Skip if App Check not configured
    if not current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY"):
        return None

    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        current_app.logger.warning("HTMX request missing App Check token: %s", request.path)
        abort(401)

    try:
        from firebase_admin import app_check as firebase_app_check
        firebase_app_check.verify_token(token)
    except Exception as exc:
        current_app.logger.warning("App Check verification failed for %s: %s", request.path, exc)
        abort(401)

    return None


# ---------------------------------------------------------------------------
# Init helper
# ---------------------------------------------------------------------------
def init_security(app: Flask) -> None:
    """Register all security extensions and hooks on the Flask app."""
    csrf.init_app(app)
    limiter.init_app(app)
    app.before_request(_enforce_request_size)
    app.before_request(_verify_app_check)
    app.after_request(_add_security_headers)
