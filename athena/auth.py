"""Firebase Authentication verification and session management."""

from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

from flask import (
    current_app,
    redirect,
    request,
    session,
    url_for,
)
from firebase_admin import auth as firebase_auth


def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that ensures the request has a valid, non-expired session.

    Redirects to login with a ``next`` query parameter so the user is
    returned to the originally-requested page after authenticating.
    """

    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        user_id = session.get("user_id")
        expires_at = session.get("expires_at")

        if not user_id or not expires_at:
            session.clear()
            return redirect(url_for("auth.login", next=request.path))

        # Check expiry
        if datetime.now(timezone.utc) >= expires_at:
            session.clear()
            return redirect(url_for("auth.login", next=request.path))

        return f(*args, **kwargs)

    return decorated


def verify_and_create_session(id_token: str) -> tuple[bool, str]:
    """Verify a Firebase ID token and establish a server-side session.

    Returns a tuple of (success: bool, error_message: str).
    On success error_message is empty.
    """
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception as exc:
        current_app.logger.error("Firebase token verification failed: %s", exc)
        return False, "Jeton invalide."

    email = decoded.get("email", "")
    authorized_email = current_app.config["AUTHORIZED_USER_EMAIL"]

    if email.lower() != authorized_email.lower():
        current_app.logger.warning(
            "Unauthorized login attempt: %s (expected %s)", email, authorized_email
        )
        return False, "Accès non autorisé."

    # MFA strictness check: if REQUIRE_MFA is enabled, ensure the token
    # was issued after MFA verification (contains sign_in_second_factor).
    if current_app.config.get("REQUIRE_MFA", False):
        firebase_claim = decoded.get("firebase", {})
        if "sign_in_second_factor" not in firebase_claim:
            current_app.logger.warning(
                "MFA required but token lacks second factor claim for %s", email
            )
            return False, "Vérification en deux étapes requise."

    lifetime = current_app.config.get("SESSION_LIFETIME_HOURS", 12)
    now = datetime.now(timezone.utc)

    session.permanent = True
    session["user_id"] = decoded["uid"]
    session["email"] = email
    session["login_time"] = now
    session["expires_at"] = now + timedelta(hours=lifetime)

    return True, ""
