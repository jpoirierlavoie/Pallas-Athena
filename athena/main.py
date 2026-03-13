"""Flask application factory and WSGI entrypoint for Pallas Athena."""

from datetime import timedelta

import firebase_admin
from firebase_admin import credentials
from flask import Flask

from athena.config import Config
from athena.security import init_security


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

    # ── Blueprints ───────────────────────────────────────────────────
    from athena.routes.auth_routes import auth_bp
    from athena.routes.dashboard import dashboard_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)

    return app


# WSGI / gunicorn entrypoint
app = create_app()
