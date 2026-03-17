"""HTTP Basic Authentication for DAV endpoints (separate from Firebase Auth)."""

import functools
from typing import Callable

import bcrypt
from flask import Response, request

from config import Config


def _check_credentials(username: str, password: str) -> bool:
    """Verify DAV credentials against configured username and bcrypt hash."""
    if not Config.DAV_USERNAME or not Config.DAV_PASSWORD_HASH:
        return False
    if username != Config.DAV_USERNAME:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"),
            Config.DAV_PASSWORD_HASH.encode("utf-8"),
        )
    except Exception:
        return False


def _unauthorized_response() -> Response:
    """Return a 401 response with WWW-Authenticate header."""
    resp = Response(
        "Unauthorized",
        status=401,
        content_type="text/plain; charset=utf-8",
    )
    resp.headers["WWW-Authenticate"] = 'Basic realm="Pallas Athena"'
    return resp


def dav_auth_required(f: Callable) -> Callable:
    """Decorator: require valid HTTP Basic Auth on every DAV request."""

    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not auth.username or not auth.password:
            return _unauthorized_response()
        if not _check_credentials(auth.username, auth.password):
            return _unauthorized_response()
        return f(*args, **kwargs)

    return decorated
