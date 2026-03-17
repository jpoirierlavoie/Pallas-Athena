"""Authentication routes: login, token verification, logout, MFA setup/manage."""

import json

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from auth import login_required, verify_and_create_session
from security import limiter

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login")
def login() -> str:
    """Render the login page (or redirect if already authenticated)."""
    if session.get("user_id"):
        return redirect(url_for("dashboard.index"))

    firebase_project_id = current_app.config["FIREBASE_PROJECT_ID"]
    firebase_api_key = current_app.config.get("FIREBASE_API_KEY", "")
    return render_template(
        "auth/login.html",
        firebase_project_id=firebase_project_id,
        firebase_api_key=firebase_api_key,
    )


@auth_bp.route("/verify-token", methods=["POST"])
@limiter.limit(lambda: current_app.config.get("RATE_LIMIT_LOGIN", "5 per minute"))
def verify_token() -> tuple[str, int]:
    """Verify the Firebase ID token POSTed by the client-side SDK.

    Returns a JSON-like response consumed by the login page JS.
    """
    id_token = request.form.get("id_token", "")
    if not id_token:
        return '{"ok":false,"error":"Jeton manquant."}', 400

    success, error_msg = verify_and_create_session(id_token)
    if success:
        next_url = request.args.get("next", url_for("dashboard.index"))
        return f'{{"ok":true,"redirect":"{next_url}"}}', 200

    return json.dumps({"ok": False, "error": error_msg or "Accès non autorisé."}), 403


@auth_bp.route("/mfa-setup")
@login_required
def mfa_setup() -> str:
    """Render the MFA enrollment page."""
    return render_template(
        "auth/mfa_setup.html",
        firebase_project_id=current_app.config["FIREBASE_PROJECT_ID"],
        firebase_api_key=current_app.config.get("FIREBASE_API_KEY", ""),
    )


@auth_bp.route("/mfa-manage")
@login_required
def mfa_manage() -> str:
    """Render the MFA management page."""
    return render_template(
        "auth/mfa_manage.html",
        firebase_project_id=current_app.config["FIREBASE_PROJECT_ID"],
        firebase_api_key=current_app.config.get("FIREBASE_API_KEY", ""),
    )


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout() -> str:
    """Clear the session and redirect to login."""
    session.clear()
    return redirect(url_for("auth.login"))
