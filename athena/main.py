"""Flask application factory and WSGI entrypoint for Pallas Athena."""

import os
import sys
from datetime import timedelta

# Ensure the athena/ directory is on sys.path so imports work both locally
# (where Python may resolve the parent package) and on App Engine (where
# athena/ is the root at /srv/).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import firebase_admin
from firebase_admin import credentials
from flask import Flask, abort, render_template as _render_template, request

from config import Config
from security import init_security


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
        if "appspot.com" in request.host.lower():
            abort(403)

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
                "sha256_cert_fingerprints": ["0F:97:FB:B6:FD:38:3C:C0:C5:22:AA:26:33:E3:6B:D3:6F:38:DD:80:E7:77:F2:E9:44:72:41:0D:6E:B4:12:74"]
            }
        }]
        return json.dumps(data), 200, {"Content-Type": "application/json"}

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

    return app


# WSGI / gunicorn entrypoint
app = create_app()
