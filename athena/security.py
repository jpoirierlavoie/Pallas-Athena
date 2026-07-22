"""Security middleware: headers, CSRF, rate limiting, input sanitization, App Check."""

import hmac
import re
import secrets
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, Response, abort, current_app, g, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

from utils.logging_setup import sanitize_log_value

# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
csrf = CSRFProtect()


# ---------------------------------------------------------------------------
# Rate limiter (in-memory default; swap to Firestore for multi-instance)
# ---------------------------------------------------------------------------
def _client_ip() -> str:
    """Rate-limit key: the real client IP.

    Behind Cloudflare the direct peer is a proxy address shared by many
    clients; Cloudflare puts the true client IP in ``CF-Connecting-IP``.
    The header is only trustworthy when traffic actually transits
    Cloudflare (enforce with an App Engine firewall / origin secret).
    """
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


limiter = Limiter(
    key_func=_client_ip,
    default_limits=[],
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
# Frontend dependencies are vendored in static/vendor/ and served same-origin,
# so no script CDN origins (jsdelivr/unpkg) appear in script-src.  gstatic +
# google remain only for the reCAPTCHA Enterprise scripts that the Firebase App
# Check SDK loads at runtime.
#
# ENFORCED (not report-only) since 2026-07-11; hardened the same day after
# Rocket Loader was disabled at the edge:
#   * script-src NO LONGER carries 'unsafe-inline'.  A per-request nonce
#     (csp_nonce / build_csp) authorizes the app's own inline <script> blocks
#     instead, so an INJECTED inline script is blocked.  ajax.cloudflare.com is
#     also gone (Rocket Loader off).  Inline on* handlers were refactored to
#     data-attributes wired via addEventListener (see base.html) because a
#     nonce cannot authorize a handler attribute.
#   * script-src KEEPS 'unsafe-eval' -- Alpine.js's standard build evaluates
#     directive expressions with new Function(); dropping it needs a migration
#     to @alpinejs/csp plus a full expression rewrite.
#   * style-src KEEPS 'unsafe-inline' -- reCAPTCHA Enterprise injects dynamic
#     inline styles that cannot be hashed or nonced (low risk).
# The Cloudflare Web Analytics beacon (static.cloudflareinsights.com) is
# deliberately NOT allowlisted; Web Analytics is disabled at the edge.
#
# The policy is assembled per request so a fresh nonce can be spliced into
# script-src; everything from style-src onward is static.
_CSP_TAIL = (
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https://*.googleapis.com https://storage.googleapis.com; "
    "connect-src 'self' https://*.googleapis.com "
    "https://identitytoolkit.googleapis.com https://storage.googleapis.com "
    "https://content-firebaseappcheck.googleapis.com "
    "https://www.google.com https://recaptchaenterprise.googleapis.com; "
    "font-src 'self'; "
    "frame-src https://*.firebaseapp.com https://storage.googleapis.com blob: "
    "https://www.google.com https://recaptcha.google.com; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; "
    "report-uri /csp-report"
)


def csp_nonce() -> str:
    """Return this request's script nonce, generated once and cached on ``g``.

    Read by both the ``csp_nonce`` Jinja global (stamped onto every inline
    ``<script nonce=...>``) and by :func:`build_csp` (spliced into script-src),
    so the header and the rendered markup always carry the same value.
    """
    nonce = getattr(g, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        g._csp_nonce = nonce
    return nonce


def build_csp(nonce: str) -> str:
    """Assemble the enforced CSP with a per-request script ``nonce``."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval' "
        "https://www.gstatic.com https://apis.google.com https://www.google.com; "
        + _CSP_TAIL
    )


def _add_security_headers(response: Response) -> Response:
    """Attach hardened security headers to every response."""
    h = response.headers
    h["Content-Security-Policy"] = build_csp(csp_nonce())
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
    _add_early_hints(response)
    return response


# ---------------------------------------------------------------------------
# Early Hints (Link headers — Cloudflare turns these into HTTP 103 responses)
# ---------------------------------------------------------------------------
# With the "Early Hints" toggle enabled, Cloudflare caches these Link headers
# per URL and answers subsequent requests with an immediate 103 so the
# browser downloads the render-critical assets while App Engine cold-starts
# (min_instances: 0 makes that window several seconds). Browsers also honor
# the same header on the final 200.
#
# MAINTENANCE: the hashed filenames here must track the asset names used in
# base.html / auth/login.html and the PRECACHE list in static/sw.js — the
# CSS regeneration recipe in CLAUDE.md lists every touch point.
_EARLY_HINTS_BASE = (
    "</static/vendor/app.af95b30d.css>; rel=preload; as=style",
    "</static/vendor/htmx-2.0.4.min.js>; rel=preload; as=script",
    "</static/vendor/alpinejs-3.15.12.min.js>; rel=preload; as=script",
)
_EARLY_HINTS_APPCHECK = (
    "</static/vendor/firebase-app-compat-10.12.2.js>; rel=preload; as=script",
    "</static/vendor/firebase-app-check-compat-10.12.2.js>; rel=preload; as=script",
    "</static/vendor/appcheck-boot.fee929af.js>; rel=preload; as=script",
    "<https://www.gstatic.com>; rel=preconnect",
    "<https://www.google.com>; rel=preconnect",
)
# The standalone login page loads a different asset set (no htmx/boot, plus
# the auth SDK — the heaviest script on the cold-start path this feature
# exists for) and uses reCAPTCHA for phone MFA regardless of App Check.
_EARLY_HINTS_LOGIN = (
    "</static/vendor/app.af95b30d.css>; rel=preload; as=style",
    "</static/vendor/alpinejs-3.15.12.min.js>; rel=preload; as=script",
    "</static/vendor/firebase-app-compat-10.12.2.js>; rel=preload; as=script",
    "</static/vendor/firebase-auth-compat-10.12.2.js>; rel=preload; as=script",
    "</static/vendor/firebase-app-check-compat-10.12.2.js>; rel=preload; as=script",
    "<https://www.gstatic.com>; rel=preconnect",
    "<https://www.google.com>; rel=preconnect",
)


def _add_early_hints(response: Response) -> None:
    """Add preload/preconnect Link headers to full-page HTML responses.

    HTMX partials, DAV traffic, App Engine internals, and the offline
    fallback are skipped — the hints only matter where a browser parses a
    full document that loads the app shell.
    """
    if request.method != "GET" or response.status_code != 200:
        return
    if request.headers.get("HX-Request"):
        return
    if (
        request.path.startswith("/dav/")
        or request.path.startswith("/_ah/")
        or request.path == "/offline"
    ):
        return
    if not (response.content_type or "").startswith("text/html"):
        return
    if request.path == "/auth/login":
        hints = list(_EARLY_HINTS_LOGIN)
    else:
        hints = list(_EARLY_HINTS_BASE)
        if current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY"):
            hints.extend(_EARLY_HINTS_APPCHECK)
    response.headers["Link"] = ", ".join(hints)


# ---------------------------------------------------------------------------
# Request size guard (non-upload routes capped at 1 MB)
# ---------------------------------------------------------------------------
UPLOAD_PATHS = ("/documents/upload",)
_DAV_MAX_BODY = 5 * 1024 * 1024  # vCard/iCal payloads are KBs; 5 MB is generous
# Template upload/replace (Phase H): POST /gabarits/ and POST /gabarits/<id>
# carry a .docx (≤ 10 MB, also enforced model-side). The generation POST
# (/gabarits/generer) and the sub-routes stay under the 1 MB default.
_TEMPLATE_UPLOAD_MAX = 10 * 1024 * 1024
_TEMPLATE_RESERVED_SEGMENTS = {"new", "generer", "dossier-search", "partie-search"}


def _is_template_upload_path(path: str) -> bool:
    """True for /gabarits/ (create) and /gabarits/<id> (file replacement)."""
    parts = path.rstrip("/").split("/")
    if parts[:2] != ["", "gabarits"]:
        return False
    if len(parts) == 2:  # "/gabarits/" — create
        return True
    return len(parts) == 3 and parts[2] not in _TEMPLATE_RESERVED_SEGMENTS


def _enforce_request_size() -> Optional[Response]:
    """Reject oversized requests for non-upload and non-DAV endpoints."""
    # DAV payloads (vCard, iCal, PROPFIND XML) get their own, tighter cap
    # than the global 25 MB upload allowance.
    if request.path.startswith("/dav/") or request.path.startswith("/.well-known/"):
        if request.content_length and request.content_length > _DAV_MAX_BODY:
            abort(413)
        return None
    if (
        request.content_length
        and request.method == "POST"
        and _is_template_upload_path(request.path)
    ):
        if request.content_length > _TEMPLATE_UPLOAD_MAX:
            abort(413)
        return None
    if request.content_length and request.path not in UPLOAD_PATHS:
        if request.content_length > 1 * 1024 * 1024:  # 1 MB
            abort(413)
    return None


# ---------------------------------------------------------------------------
# Cloudflare origin check (optional, defense against direct App Engine access)
# ---------------------------------------------------------------------------
# The Host-header check in main.py is spoofable: App Engine routes on the
# TLS/appspot hostname, not the Host header an attacker sends.  When
# CF_ORIGIN_SECRET is configured (Secret Manager: cf-origin-secret), every
# request must carry the same value in X-Origin-Auth — configure a Cloudflare
# Transform Rule to inject the header at the edge, and pair with an App
# Engine firewall restricted to Cloudflare IP ranges.  Unset = check disabled
# (local dev, pre-rollout).
def _enforce_origin_secret() -> Optional[Response]:
    """Reject requests missing the Cloudflare-injected origin header."""
    secret = current_app.config.get("CF_ORIGIN_SECRET", "")
    if not secret:
        return None
    # App Engine internal requests (warmup, cron) never transit Cloudflare.
    if request.path.startswith("/_ah/"):
        return None
    supplied = request.headers.get("X-Origin-Auth", "")
    if not hmac.compare_digest(supplied, secret):
        abort(403)
    return None


# ---------------------------------------------------------------------------
# Input sanitization utility
# ---------------------------------------------------------------------------
# Tag stripper.  The body class excludes ``<`` as well as ``>`` so a run of
# unclosed ``<`` fails to match in O(1) at each position instead of re-scanning
# to end-of-string — keeping the substitution linear rather than quadratic
# (CWE-1333 / CodeQL ``py/polynomial-redos``).  On well-formed input this is
# identical to ``<[^>]+>``: a real tag body never contains a literal ``<``.
_TAG_RE = re.compile(r"<[^<>]*>")


def sanitize(value: str, max_length: int = 1000) -> str:
    """Strip HTML tags and truncate.  Output escaping is handled by Jinja2.

    The value is truncated to ``max_length`` *before* the tag-stripping regex
    runs, capping output length so no legitimate content is lost.  The regex
    itself is linear — the ``[^<>]`` body fails fast on adversarial input such
    as a long run of unclosed ``<`` — so it is not vulnerable to polynomial-time
    blow-up (CWE-1333 / CodeQL ``py/polynomial-redos``).
    """
    cleaned = _TAG_RE.sub("", value[:max_length])
    return cleaned[:max_length]


# ---------------------------------------------------------------------------
# Internal-redirect URL guard
# ---------------------------------------------------------------------------
def safe_internal_redirect(target: Optional[str], fallback: str) -> str:
    """Return ``target`` only if it is a same-origin internal path; else ``fallback``.

    Accepts only paths starting with a single ``/`` and no scheme or host —
    blocks ``//evil.com/x``, ``https://evil.com``, ``javascript:...``, and
    backslash-bypass tricks that some browsers normalize to ``/``. Used by
    forms that thread a ``return_to`` query string back to the caller.
    """
    if not isinstance(target, str):
        return fallback
    candidate = target.strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        _log_redirect_rejection("not_internal_path")
        return fallback
    if "\\" in candidate:
        _log_redirect_rejection("backslash_in_path")
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        _log_redirect_rejection("scheme_or_netloc_present")
        return fallback
    return candidate


def _log_redirect_rejection(reason: str) -> None:
    """Emit a structured security log without leaking the rejected URL."""
    try:
        # Late import to avoid circular dependency at module-load time.
        from utils.logging_setup import log_security_event

        log_security_event("redirect_rejected", "warning", reason=reason)
    except Exception:  # pragma: no cover — logging must never break the request
        pass


# ---------------------------------------------------------------------------
# App Check server-side verification
# ---------------------------------------------------------------------------
_APPCHECK_EXEMPT_PREFIXES = (
    "/static/",
    "/dav/",
    "/.well-known/",
    "/auth/",
)

_APPCHECK_MISSING_WARNED = False


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

    # Skip if App Check not configured — fail-open by design, but loudly in
    # production so a config regression cannot silently disable the control.
    if not current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY"):
        global _APPCHECK_MISSING_WARNED
        if current_app.config.get("ENV") == "production" and not _APPCHECK_MISSING_WARNED:
            _APPCHECK_MISSING_WARNED = True
            current_app.logger.warning(
                "App Check site key not configured in production — "
                "App Check verification is disabled"
            )
        return None

    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        current_app.logger.warning(
            "HTMX request missing App Check token: %s",
            sanitize_log_value(request.path),
        )
        abort(401)

    try:
        from firebase_admin import app_check as firebase_app_check
        firebase_app_check.verify_token(token)
    except Exception as exc:
        current_app.logger.warning(
            "App Check verification failed for %s: %s",
            sanitize_log_value(request.path), exc,
        )
        abort(401)

    return None


# ---------------------------------------------------------------------------
# Init helper
# ---------------------------------------------------------------------------
def init_security(app: Flask) -> None:
    """Register all security extensions and hooks on the Flask app."""
    csrf.init_app(app)
    limiter.init_app(app)
    app.before_request(_enforce_origin_secret)
    app.before_request(_enforce_request_size)
    app.before_request(_verify_app_check)
    app.after_request(_add_security_headers)

    # Expose the per-request CSP nonce to templates so every inline <script>
    # can carry nonce="{{ csp_nonce }}" (script-src no longer has
    # 'unsafe-inline'). Same value the response header uses (both read g).
    @app.context_processor
    def _inject_csp_nonce() -> dict:
        return {"csp_nonce": csp_nonce()}
