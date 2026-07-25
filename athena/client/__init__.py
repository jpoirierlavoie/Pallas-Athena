"""Portail client (spec L1) — second App Engine service « portail ».

This package is the CLIENT-facing portal (document intake). The lawyer-facing
service remains the repo's main app (a future rename may move it under a
``juriste/`` sibling — user decision 2026-07-25). Isolation is by
construction: ``client.wsgi:app`` registers ONLY the portal blueprint, so no
main-service route exists in that process even though the whole codebase
ships in the image (pinned by tests/test_portail_app.py).

Import discipline: portal modules never import ``models``/``security``/
``config`` from the main service — the portal process must not construct the
default-database Firestore client nor resolve the main service's secrets.
The reverse is allowed: the main service imports ``client.config`` constants
(secret resolution there is lazy, never at import).
"""

from flask import Blueprint, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

portail_bp = Blueprint("portail", __name__)


def _client_ip() -> str:
    # Same trust model as the main service: CF-Connecting-IP is only
    # meaningful because the App Engine firewall admits Cloudflare ranges
    # (plus the internal task/cron address) — see security.py's twin.
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


# The portal's own limiter instance (in-memory, per instance). Only
# decorated routes are limited: /session (brute-force on tokens) and
# /api/renvoi (each accepted renvoi sends an email — bombing guard).
limiter = Limiter(
    key_func=_client_ip,
    default_limits=[],
    storage_uri="memory://",
)
