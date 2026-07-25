"""Portal Flask app factory (spec L1 §2.1, §6).

Registers ONLY the portal blueprint — isolation by construction from the
main service, pinned by tests/test_portail_app.py. Sessions ride Flask's
signed cookie under the DISTINCT ``portail-secret-key``; per-request
authorization comes from re-reading the invitation document (routes.py),
which is what makes revocation instantaneous.
"""

import logging
import os
from datetime import timedelta

from flask import Flask, jsonify, render_template, request
from flask_wtf.csrf import CSRFError, CSRFProtect

from client import limiter, portail_bp
from client.config import (
    PORTAIL_SESSION_HOURS,
    firebase_api_key,
    portail_secret_key,
)
from client.security import init_portail_security

logger = logging.getLogger(__name__)

csrf = CSRFProtect()


def _init_firebase(app: Flask) -> None:
    """Initialize firebase_admin for verify_id_token + App Check.

    Guarded: locally without ADC the portal still boots — /session then
    degrades to the generic refusal instead of crashing the process.
    """
    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            firebase_admin.initialize_app(
                credentials.ApplicationDefault(),
                {"projectId": app.config.get("FIREBASE_PROJECT_ID", "")},
            )
    except Exception:
        logger.warning(
            "firebase_admin initialization failed — token verification "
            "will refuse every session until credentials exist",
            exc_info=True,
        )


def create_portail_app() -> Flask:
    athena_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder="templates",
        # Same static tree as the main service (vendored assets, compiled
        # Tailwind, icons). In production App Engine's static handlers in
        # portail.yaml serve /static/* directly; this is the dev fallback.
        static_folder=os.path.join(athena_root, "static"),
    )

    app.config.update(
        SECRET_KEY=portail_secret_key(),
        ENV=os.environ.get("ENV", "development"),
        # Distinct cookie name + key = hard session boundary with the main
        # service (spec §6.4).
        SESSION_COOKIE_NAME="pa_portail",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("ENV") == "production",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=PORTAIL_SESSION_HOURS),
        # JSON APIs only — file bytes go from the browser STRAIGHT to the
        # GCS resumable session, never through this service.
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
        # The documents page can legitimately sit open for hours while
        # large files upload; flask-wtf's default 1 h token lifetime would
        # break the finalization POST. Tokens stay valid for the session.
        WTF_CSRF_TIME_LIMIT=None,
        FIREBASE_PROJECT_ID=os.environ.get("FIREBASE_PROJECT_ID", ""),
        FIREBASE_APP_ID=os.environ.get("FIREBASE_APP_ID", ""),
        FIREBASE_API_KEY=firebase_api_key(),
        RECAPTCHA_ENTERPRISE_SITE_KEY=os.environ.get(
            "RECAPTCHA_ENTERPRISE_SITE_KEY", ""
        ),
        APPCHECK_DEBUG_TOKEN=os.environ.get("APPCHECK_DEBUG_TOKEN", ""),
    )

    # Observability — same order as the main factory: the OTel middleware
    # wraps WSGI first, then logging binds the trace field.
    from utils.tracing_setup import init_app as init_tracing
    from utils.logging_setup import init_app as init_logging

    init_tracing(app)
    init_logging(app)

    _init_firebase(app)

    csrf.init_app(app)
    limiter.init_app(app)
    init_portail_security(app)

    from client import routes  # noqa: F401 — routes attach to portail_bp

    app.register_blueprint(portail_bp)

    @app.context_processor
    def _inject_firm() -> dict:
        return {"firm_name": os.environ.get("FIRM_NAME", "")}

    # ── Error handlers (generic French, JSON on /api/*) ──────────────────

    def _api_or_page(message: str, code: int):
        if request.path.startswith("/api/") or request.path == "/session":
            return jsonify({"erreur": message}), code
        return render_template("erreur.html", message=message), code

    @app.errorhandler(404)
    def _not_found(e):
        return _api_or_page("Page introuvable.", 404)

    @app.errorhandler(413)
    def _too_large(e):
        return _api_or_page("Requête trop volumineuse.", 413)

    @app.errorhandler(CSRFError)
    def _csrf(e):
        return _api_or_page("Session invalide. Rechargez la page.", 400)

    @app.errorhandler(500)
    def _server_error(e):
        return _api_or_page("Une erreur est survenue. Réessayez.", 500)

    return app
