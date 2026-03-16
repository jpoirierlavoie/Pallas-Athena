"""Security middleware: headers, CSRF, rate limiting, input sanitization."""

import re
from typing import Optional

from flask import Flask, Request, Response, request
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
    "https://www.gstatic.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob: https://*.googleapis.com https://storage.googleapis.com; "
    "connect-src 'self' https://*.googleapis.com https://*.firebaseio.com "
    "https://identitytoolkit.googleapis.com https://storage.googleapis.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "frame-src https://*.firebaseapp.com https://storage.googleapis.com blob:; "
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
    """Reject oversized requests for non-upload endpoints."""
    if request.content_length and request.path not in UPLOAD_PATHS:
        if request.content_length > 1 * 1024 * 1024:  # 1 MB
            from flask import abort
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
# Init helper
# ---------------------------------------------------------------------------
def init_security(app: Flask) -> None:
    """Register all security extensions and hooks on the Flask app."""
    csrf.init_app(app)
    limiter.init_app(app)
    app.before_request(_enforce_request_size)
    app.after_request(_add_security_headers)
