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

        # Preserve the query string so parameterized deep links (filtered
        # lists, /oauth/authorize?client_id=...) survive the login
        # round-trip; _safe_next re-validates the value on the way back.
        return_to = request.full_path if request.query_string else request.path

        if not user_id or not expires_at:
            session.clear()
            return redirect(url_for("auth.login", next=return_to))

        # Check expiry
        if datetime.now(timezone.utc) >= expires_at:
            session.clear()
            return redirect(url_for("auth.login", next=return_to))

        return f(*args, **kwargs)

    return decorated


# A token must have been minted by a *fresh* interactive sign-in to create a
# session; older (but still unexpired) ID tokens are refused to limit replay.
_MAX_TOKEN_AGE_SECONDS = 10 * 60


def verify_and_create_session(id_token: str) -> tuple[bool, str]:
    """Verify a Firebase ID token and establish a server-side session.

    Returns a tuple of (success: bool, error_message: str).
    On success error_message is empty.
    """
    from utils.logging_setup import log_auth_event

    try:
        # check_revoked: reject tokens minted before a revocation/disable
        # event so a console-side compromise response takes effect.
        decoded = firebase_auth.verify_id_token(
            id_token, check_revoked=True, clock_skew_seconds=10
        )
    except Exception as exc:
        # Never log the token or any claim — only the failure class.
        log_auth_event(
            "auth_failure", "failure",
            reason="token_invalid", error_type=type(exc).__name__,
        )
        return False, "Jeton invalide."

    email = decoded.get("email", "")
    authorized_email = current_app.config["AUTHORIZED_USER_EMAIL"]

    if email.lower() != authorized_email.lower():
        # The attempted email is attacker-controlled PII — do not log it.
        log_auth_event("auth_failure", "failure", reason="unauthorized_email")
        return False, "Accès non autorisé."

    # Replay guard: only tokens from a recent interactive authentication may
    # establish a session (a captured ID token is otherwise valid ~1 h).
    auth_time = decoded.get("auth_time", 0)
    now_ts = datetime.now(timezone.utc).timestamp()
    if not auth_time or now_ts - float(auth_time) > _MAX_TOKEN_AGE_SECONDS:
        log_auth_event("auth_failure", "failure", reason="token_stale")
        return False, "Session expirée. Veuillez vous reconnecter."

    # MFA strictness check: if REQUIRE_MFA is enabled, ensure the token
    # was issued after MFA verification (contains sign_in_second_factor).
    if current_app.config.get("REQUIRE_MFA", False):
        firebase_claim = decoded.get("firebase", {})
        if "sign_in_second_factor" not in firebase_claim:
            log_auth_event("auth_failure", "failure", reason="mfa_missing")
            return False, "Vérification en deux étapes requise."

    lifetime = current_app.config.get("SESSION_LIFETIME_HOURS", 12)
    now = datetime.now(timezone.utc)

    session.permanent = True
    session["user_id"] = decoded["uid"]
    session["email"] = email
    session["login_time"] = now
    session["expires_at"] = now + timedelta(hours=lifetime)

    log_auth_event("login", "success")
    return True, ""
