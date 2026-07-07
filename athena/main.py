"""Flask application factory and WSGI entrypoint for Pallas Athena."""

import os
import sys
import logging
from datetime import timedelta

# Ensure the athena/ directory is on sys.path so imports work both locally
# (where Python may resolve the parent package) and on App Engine (where
# athena/ is the root at /srv/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Bootstrap minimal logging so anything emitted before ``init_logging``
# attaches the proper handler chain still surfaces somewhere.  The real
# configuration (Cloud Logging in production, stderr locally, plus the
# context + redaction filters) is set up by ``utils.logging_setup.init_app``
# inside ``create_app``.
logging.basicConfig(level=logging.WARNING)

import firebase_admin
from firebase_admin import credentials
from flask import Flask, abort, render_template as _render_template, request

from config import Config
from security import init_security
from utils.logging_setup import init_app as init_logging, log_unexpected
from utils.tracing_setup import init_app as init_tracing


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # ── Configuration ────────────────────────────────────────────────
    app.config.from_object(Config)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = Config.ENV == "production"
    app.permanent_session_lifetime = timedelta(
        hours=Config.SESSION_LIFETIME_HOURS,
    )

    # ── Tracing (Cloud Trace + auto-instrumentation) ────────────────
    # Tracing must run before logging so the OTel Flask middleware wraps
    # the WSGI app first; this guarantees an active span exists by the
    # time logging's ``before_request`` reads its trace context.
    init_tracing(app)

    # ── Logging (Cloud Logging + context/redaction filters) ─────────
    init_logging(app)

    # ── Firebase Admin SDK ───────────────────────────────────────────
    if not firebase_admin._apps:
        # Uses GOOGLE_APPLICATION_CREDENTIALS env var or ADC on App Engine
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"storageBucket": Config.FIREBASE_STORAGE_BUCKET},
        )

    # ── Security (headers, CSRF, rate limiter) ───────────────────────
    init_security(app)

    # ── Jinja2 custom filters ───────────────────────────────────────────
    from tz import to_mtl
    app.jinja_env.filters["to_mtl"] = to_mtl

    from utils.validators import format_phone_display
    app.jinja_env.filters["phone"] = format_phone_display

    import json as _json
    from markupsafe import Markup

    def _jsattr(value: str) -> Markup:
        """Escape a string for safe use as a JS string inside a double-quoted HTML attribute."""
        js = _json.dumps(str(value), ensure_ascii=False)
        return Markup(
            js.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    app.jinja_env.filters["jsattr"] = _jsattr

    import markdown as _markdown_lib
    import bleach as _bleach

    _MD_EXTENSIONS = ["tables", "fenced_code", "nl2br"]
    _ALLOWED_TAGS = [
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "br", "hr", "strong", "em", "del",
        "code", "pre", "ul", "ol", "li", "blockquote",
        "a", "table", "thead", "tbody", "tr", "th", "td",
    ]
    _ALLOWED_ATTRS = {
        "a": ["href", "title"],
        "th": ["align"],
        "td": ["align"],
    }

    def render_markdown(text: str) -> str:
        """Convert markdown to sanitized HTML."""
        html = _markdown_lib.markdown(text, extensions=_MD_EXTENSIONS)
        return _bleach.clean(
            html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            strip=True,
        )

    app.jinja_env.filters["markdown"] = render_markdown

    # ── Blueprints ───────────────────────────────────────────────────
    from routes.auth_routes import auth_bp
    from routes.dashboard import dashboard_bp
    from routes.parties import parties_bp
    from routes.dossiers import dossiers_bp
    from routes.time_expenses import time_expenses_bp
    from routes.invoices import invoices_bp
    from routes.hearings import hearings_bp
    from routes.tasks import tasks_bp
    from routes.protocols import protocols_bp
    from routes.documents import documents_bp
    from routes.notes import notes_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(parties_bp)
    app.register_blueprint(dossiers_bp)
    app.register_blueprint(time_expenses_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(hearings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(protocols_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(notes_bp)

    # ── DAV blueprints (CardDAV, CalDAV, RFC-5545, per-dossier) ─────────
    from dav import dav_bp
    from dav.carddav import carddav_bp
    from dav.caldav import caldav_bp
    from dav.rfc5545 import rfc5545_bp
    from dav.dossier_collections import dossier_dav_bp

    app.register_blueprint(dav_bp)
    app.register_blueprint(carddav_bp)
    app.register_blueprint(caldav_bp)
    app.register_blueprint(rfc5545_bp)
    app.register_blueprint(dossier_dav_bp)

    # Exempt all DAV endpoints from CSRF protection (they use HTTP Basic Auth)
    from security import csrf
    csrf.exempt(dav_bp)
    csrf.exempt(carddav_bp)
    csrf.exempt(caldav_bp)
    csrf.exempt(rfc5545_bp)
    csrf.exempt(dossier_dav_bp)

    # ── MCP connector (Phase I): /mcp endpoint + embedded OAuth 2.1 AS ──
    from mcp import mcp_bp, register_mcp
    register_mcp(app)
    # /mcp uses Bearer tokens, never a browser session. The machine-facing
    # OAuth views (/oauth/register, /oauth/token, /oauth/revoke) are
    # exempted individually in mcp/oauth.py; the /oauth/authorize POST —
    # the one browser-origin form in the flow — keeps CSRF enforcement.
    csrf.exempt(mcp_bp)

    # ── Context processor (Firebase config for all templates) ──────────
    @app.context_processor
    def inject_firebase_config() -> dict[str, str]:
        return {
            "firebase_project_id": app.config["FIREBASE_PROJECT_ID"],
            "firebase_api_key": app.config.get("FIREBASE_API_KEY", ""),
            "firebase_app_id": app.config.get("FIREBASE_APP_ID", ""),
        }

    # ── Block direct appspot.com access (traffic must come via Cloudflare) ──
    @app.before_request
    def block_appspot() -> None:
        # App Engine internal requests (warmup, cron) arrive on the appspot
        # host without Cloudflare headers — they must not be blocked.
        if request.path.startswith("/_ah/"):
            return None
        host = request.host.split(":", 1)[0].lower().rstrip(".")
        if host == "appspot.com" or host.endswith(".appspot.com"):
            abort(403)

    # ── App Engine warmup (inbound_services: warmup in app.yaml) ──────
    # Fired before a new instance receives live traffic; priming the
    # Firestore channel here moves connection setup off the first user
    # request and softens cold starts (min_instances stays 0).
    @app.route("/_ah/warmup")
    def warmup() -> tuple[str, int]:
        try:
            from models import db
            db.collection("dav_sync").document("parties").get()
        except Exception:  # pragma: no cover — warmup must never fail loudly
            pass
        return "", 200

    # ── Offline fallback (PWA) ─────────────────────────────────────────
    @app.route("/offline")
    def offline():
        return _render_template("offline.html")

    # ── Digital Asset Links (Android TWA) ─────────────────────────────
    @app.route("/.well-known/assetlinks.json")
    def asset_links():
        import json
        data = [{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "ca.poirierlavoie.athena",
                "sha256_cert_fingerprints": ["47:3B:05:FB:50:D9:23:0E:51:FC:12:C7:AB:6A:DB:AD:02:FA:85:CB:3A:C9:BE:8E:80:1F:06:5E:7A:8B:D7:11"]
            }
        }]
        return json.dumps(data), 200, {"Content-Type": "application/json"}

    # ── CSP violation reporting (referenced by report-uri in security.py) ──
    from security import csrf as _csrf

    @app.route("/csp-report", methods=["POST"])
    @_csrf.exempt
    def csp_report():  # type: ignore[no-untyped-def]
        """Collect browser CSP violation reports (report-only policy)."""
        try:
            payload = request.get_json(force=True, silent=True) or {}
            report = payload.get("csp-report", payload) or {}
            from utils.logging_setup import log_security_event
            log_security_event(
                "csp_violation",
                "warning",
                directive=str(report.get("effective-directive")
                              or report.get("violated-directive") or "")[:120],
                blocked=str(report.get("blocked-uri") or "")[:200],
            )
        except Exception:  # pragma: no cover — reporting must never error
            pass
        return "", 204

    # ── Error handlers ─────────────────────────────────────────────────
    @app.errorhandler(404)
    def page_not_found(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_server_error(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/500.html"), 500

    @app.errorhandler(413)
    def request_entity_too_large(e):  # type: ignore[no-untyped-def]
        return _render_template("errors/404.html"), 413

    from flask_wtf.csrf import CSRFError
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):  # type: ignore[no-untyped-def]
        # Expected security event — WARNING via the typed helper, not an
        # ERROR-level unhandled exception.
        from utils.logging_setup import log_security_event
        log_security_event("csrf_failure", "warning", path=request.path)
        return _render_template("errors/404.html"), 400

    @app.errorhandler(Exception)
    def handle_unexpected(e):  # type: ignore[no-untyped-def]
        # Intended 4xx/redirect responses (abort(403), 401 from App Check,
        # 405s…) must keep their status and must NOT be logged as ERROR —
        # only genuine unhandled exceptions are.
        if isinstance(e, HTTPException):
            return e
        log_unexpected("unhandled exception")
        return _render_template("errors/500.html"), 500

    return app


# WSGI / gunicorn entrypoint
app = create_app()
