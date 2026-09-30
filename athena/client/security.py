"""Portal-service security surface — CSP (spec L1 §10), headers, App Check.

Deliberately separate from the main service's security.py: the portal has
its own, tighter CSP (no 'unsafe-eval' — the portal ships no Alpine, all its
JS is vanilla under nonce; connect-src admits exactly the Firebase Auth and
GCS-resumable-upload origins), and no Early Hints. Since 2026-09-30 it does
carry layers 2 and 3 of the edge defence (:func:`enforce_edge` — the origin
secret and the appspot block), COPIED rather than imported: this process
must never import the main service's security.py (nor config.py, nor models).
"""

import hmac
import secrets
from typing import Optional

from flask import Flask, Response, abort, current_app, g, request

from utils.logging_setup import log_security_event, sanitize_log_value


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


# The deployment facts already said once in this process (warn-once) — the
# main service's shape: a set mutated in place, never a rebound flag.
_WARNED_ONCE: set[str] = set()


def verify_app_check() -> Optional[Response]:
    """Fail-open App Check verification on the portal's mutating APIs.

    Mirrors the main service's ``_verify_app_check`` shape, with a different
    predicate: the portal has no HTMX — its JS ``fetch`` calls attach
    ``X-Firebase-AppCheck`` themselves, so enforcement targets every POST.
    Fail-open when the site key is unset (loud in production).

    ONE deliberate exception: ``portail.creer_session``. There, the caller has
    ALREADY spent the single-use email-link code before this check runs, so an
    attestation hiccup would not merely refuse the request — it would destroy
    the client's invitation permanently (the reCAPTCHA score of a client on a
    fresh phone or a private window is routinely low). The route keeps its own
    hard gates: a Firebase ID token carrying the ``portail`` claim,
    ``email_verified``, an active non-expired invitation and an exact email
    match, behind 10/min per IP. Enforcement stays ON for /api/renvoi (which is
    unauthenticated and sends email — the real bot surface) and the upload APIs.
    """
    if request.method != "POST":
        return None

    if request.endpoint == "portail.creer_session":
        return None

    if not current_app.config.get("RECAPTCHA_ENTERPRISE_SITE_KEY"):
        if (
            current_app.config.get("ENV") == "production"
            and "appcheck_disabled" not in _WARNED_ONCE
        ):
            _WARNED_ONCE.add("appcheck_disabled")
            # The main service's event, so ONE log-based metric covers both
            # services (filter on resource.labels.module_id to tell them
            # apart). Was a raw `current_app.logger.warning`, which carries no
            # `jsonPayload.event`, until 2026-09-30.
            log_security_event(
                "appcheck_disabled",
                "warning",
                reason="recaptcha_site_key_unset",
            )
        return None

    token = request.headers.get("X-Firebase-AppCheck")
    if not token:
        log_security_event(
            "appcheck_failure",
            "warning",
            reason="token_missing",
            path=sanitize_log_value(request.path),
        )
        abort(401)
    try:
        from firebase_admin import app_check as firebase_app_check

        firebase_app_check.verify_token(token)
    except Exception as exc:
        # The class only — the message of a pyjwt error carries the kid of
        # the unverified header (see security._verify_app_check).
        cause = exc.__cause__   # the wrapped pyjwt error's CLASS, see there
        log_security_event(
            "appcheck_failure",
            "warning",
            reason="verification_failed",
            error_type=type(exc).__name__,
            **({"cause_type": type(cause).__name__} if cause is not None else {}),
            path=sanitize_log_value(request.path),
        )
        abort(401)
    return None


def _is_appengine_internal_request() -> bool:
    """Cloud Tasks / cron traffic — the main service's predicate, copied.

    App Engine STRIPS every ``X-AppEngine-*`` header from external traffic,
    Cloudflare's included, so their mere presence proves an internal
    dispatch (source 0.1.0.2).
    """
    return bool(
        request.headers.get("X-AppEngine-QueueName")
        or request.headers.get("X-Appengine-Cron")
    )


def enforce_edge() -> Optional[Response]:
    """Layers 2 and 3 of the edge defence, on the portal too (2026-09-30).

    The portal shares the app's firewall (Cloudflare ranges only) but, until
    this date, neither of the main service's two other layers: anyone
    fronting ``portail-dot-….appspot.com`` with THEIR OWN Cloudflare zone
    reached it, and ``CF-Connecting-IP`` — the key of every portal rate
    limit — was theirs to choose. Same rules as the main service:

    * the appspot host is refused (the Host header is spoofable, so this is
      the weakest layer — but free);
    * when ``CF_ORIGIN_SECRET`` is set, every request must carry it in
      ``X-Origin-Auth`` (the zone-wide Transform Rule injects it). Unset or
      unreadable — ``portail-svc`` not yet granted the accessor — the check
      is OFF (fail-open, as on the main service) and says so ONCE per
      process, structured, with the reason;
    * App Engine's own ``/_ah/`` paths and internal dispatches never transit
      Cloudflare and are exempt from both.

    Compared as BYTES: ``hmac.compare_digest`` raises on a non-ASCII
    ``str``, so a forged header carrying one would have been a 500.
    """
    if request.path.startswith("/_ah/") or _is_appengine_internal_request():
        return None
    host = request.host.split(":", 1)[0].lower().rstrip(".")
    if host == "appspot.com" or host.endswith(".appspot.com"):
        abort(403)
    secret = current_app.config.get("CF_ORIGIN_SECRET", "")
    if not secret:
        if (
            current_app.config.get("ENV") == "production"
            and "origin_secret_disabled" not in _WARNED_ONCE
        ):
            _WARNED_ONCE.add("origin_secret_disabled")
            reason, error_type = current_app.config.get(
                "CF_ORIGIN_SECRET_OFF", ("cf_origin_secret_unset", ""))
            log_security_event(
                "origin_secret_disabled", "warning",
                reason=reason or "cf_origin_secret_unset",
                **({"error_type": error_type} if error_type else {}),
            )
        return None
    supplied = request.headers.get("X-Origin-Auth", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
        abort(403)
    return None


def init_portail_security(app: Flask) -> None:
    # The edge first: a request the edge refuses reaches no other check.
    app.before_request(enforce_edge)
    app.before_request(verify_app_check)
    app.after_request(add_security_headers)

    @app.context_processor
    def _inject_nonce() -> dict:
        return {"csp_nonce": csp_nonce()}
