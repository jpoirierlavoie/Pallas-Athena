"""Portal-service security surface — CSP (spec L1 §10), headers, App Check.

Deliberately separate from the main service's security.py: the portal has
its own, tighter CSP (no 'unsafe-eval' — the portal ships no Alpine, all its
JS is vanilla under nonce; connect-src admits exactly the Firebase Auth and
GCS-resumable-upload origins), and none of the main service's edge posture
(origin secret, appspot block, Early Hints) applies to this public host.
"""

import secrets
from typing import Optional

from flask import Flask, Response, abort, current_app, g, request

from utils.logging_setup import sanitize_log_value


def csp_nonce() -> str:
    nonce = getattr(g, "_csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        g._csp_nonce = nonce
    return nonce


def build_csp(nonce: str, appcheck: bool) -> str:
    """Assemble the portal CSP (§10 table).

    With App Check (D-2, default on) the reCAPTCHA Enterprise origins join
    script-src/frame-src, the App Check token exchange joins connect-src,
    and style-src gains 'unsafe-inline' (reCAPTCHA injects dynamic inline
    styles — the same documented necessity as the main service).
    """
    script_src = f"'self' 'nonce-{nonce}'"
    connect_src = (
        "'self' https://identitytoolkit.googleapis.com "
        "https://securetoken.googleapis.com https://storage.googleapis.com"
    )
    style_src = "'self'"
    frame_src = "'none'"
    if appcheck:
        script_src += " https://www.gstatic.com https://www.google.com"
        connect_src += (
            " https://content-firebaseappcheck.googleapis.com"
            " https://www.google.com"
        )
        style_src = "'self' 'unsafe-inline'"
        frame_src = "https://www.google.com https://recaptcha.google.com"
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        f"connect-src {connect_src}; "
        "img-src 'self' data:; "
        f"style-src {style_src}; "
        f"frame-src {frame_src}; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )


def add_security_headers(response: Response) -> Response:
    h = response.headers
    h["Content-Security-Policy"] = build_csp(
        csp_nonce(),
        bool(current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY")),
    )
    h["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains; preload"
    )
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    # Stricter than the main service (§10): client sign-in links must never
    # leak through referrers.
    h["Referrer-Policy"] = "no-referrer"
    h["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    h["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    h["Pragma"] = "no-cache"
    return response


_APPCHECK_MISSING_WARNED = False


def verify_app_check() -> Optional[Response]:
    """Fail-open App Check verification on the portal's mutating APIs.

    Mirrors the main service's ``_verify_app_check`` shape, with a different
    predicate: the portal has no HTMX — its JS ``fetch`` calls attach
    ``X-Firebase-AppCheck`` themselves, so enforcement targets every POST.
    Fail-open when the site key is unset (loud in production).
    """
    if request.method != "POST":
        return None

    if not current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY"):
        global _APPCHECK_MISSING_WARNED
        if (
            current_app.config.get("ENV") == "production"
            and not _APPCHECK_MISSING_WARNED
        ):
            _APPCHECK_MISSING_WARNED = True
            current_app.logger.warning(
                "App Check site key not configured in production — "
                "portal App Check verification is disabled"
            )
        return None

    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        current_app.logger.warning(
            "portal POST missing App Check token: %s",
            sanitize_log_value(request.path),
        )
        abort(401)
    try:
        from firebase_admin import app_check as firebase_app_check

        firebase_app_check.verify_token(token)
    except Exception:
        current_app.logger.warning(
            "portal App Check verification failed: %s",
            sanitize_log_value(request.path),
        )
        abort(401)
    return None


def init_portail_security(app: Flask) -> None:
    app.before_request(verify_app_check)
    app.after_request(add_security_headers)

    @app.context_processor
    def _inject_nonce() -> dict:
        return {"csp_nonce": csp_nonce()}
