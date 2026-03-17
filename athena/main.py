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
from flask import Flask, render_template as _render_template

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

    # ── Jinja2 timezone filter ────────────────────────────────────────
    from tz import to_mtl
    app.jinja_env.filters["to_mtl"] = to_mtl

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

    # ── DAV blueprints (CardDAV, CalDAV, RFC-5545) ────────────────────
    from dav import dav_bp
    from dav.carddav import carddav_bp
    from dav.caldav import caldav_bp
    from dav.rfc5545 import rfc5545_bp

    app.register_blueprint(dav_bp)
    app.register_blueprint(carddav_bp)
    app.register_blueprint(caldav_bp)
    app.register_blueprint(rfc5545_bp)

    # Exempt all DAV endpoints from CSRF protection (they use HTTP Basic Auth)
    from security import csrf
    csrf.exempt(dav_bp)
    csrf.exempt(carddav_bp)
    csrf.exempt(caldav_bp)
    csrf.exempt(rfc5545_bp)

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
