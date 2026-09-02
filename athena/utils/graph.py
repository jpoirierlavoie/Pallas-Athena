"""Microsoft Graph access — application (client-credentials) flow.

Introduced by the portail client (spec L1 §8.1) for outbound email; the
future phase J notification pipeline reuses it as-is. Phase L2 added
Calendars.ReadWrite on the same Entra app registration (Bookings sync), and
the Outlook mirror reuses that permission for event create/patch/delete.

Deliberately msal-free: the client-credentials flow is a single POST and
``requests`` is already in the hash lock. MAIN SERVICE ONLY — the portal
service's environment carries no Graph credential, so any call from that
process raises GraphNotConfigured.

Error messages carry HTTP status codes only, never response bodies (a Graph
error body can echo tenant/user identifiers into logs).
"""

import json
import threading
import time
from typing import Any, Optional

import requests

from config import Config

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Refresh the cached token this many seconds before Entra's expiry.
_TOKEN_MARGIN_SECONDS = 300
_HTTP_TIMEOUT = 30

# One Session per worker: bare module-level ``requests.post`` creates and
# discards a transient connection per call, so every Graph call paid a fresh
# TCP+TLS handshake (~100-250 ms) — multiplied by the Outlook-mirror and
# Bookings-sync loops. urllib3's pooling is thread-safe for this usage; the
# timeouts and the GraphError taxonomy are unchanged. Tests patch
# ``graph._session`` (they used to patch ``graph.requests``).
_session = requests.Session()


class GraphError(RuntimeError):
    """A Graph or token-endpoint call failed (status code in the message)."""


class GraphNotConfigured(GraphError):
    """The GRAPH_* configuration is absent — outbound email is disabled."""


_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0  # time.monotonic() deadline


def _reset_token_cache_for_tests() -> None:
    global _cached_token, _token_expires_at
    with _token_lock:
        _cached_token = None
        _token_expires_at = 0.0


def jeton_application() -> str:
    """Return a cached application access token (client credentials).

    One in-process cache per gunicorn worker; renewed ``_TOKEN_MARGIN_SECONDS``
    before Entra's ``expires_in`` deadline.
    """
    global _cached_token, _token_expires_at

    if not Config.graph_configured():
        raise GraphNotConfigured("Le courriel sortant (Microsoft Graph) n'est pas configuré.")

    with _token_lock:
        if _cached_token and time.monotonic() < _token_expires_at:
            return _cached_token

        try:
            response = _session.post(
                _TOKEN_URL.format(tenant=Config.GRAPH_TENANT_ID),
                data={
                    "grant_type": "client_credentials",
                    "client_id": Config.GRAPH_CLIENT_ID,
                    "client_secret": Config.GRAPH_CLIENT_SECRET,
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            # Network failures must honour the module's GraphError contract
            # (callers catch GraphError, never requests internals). Type
            # name only — a requests exception str can embed the URL.
            raise GraphError(
                f"Échec réseau Graph ({type(exc).__name__})."
            ) from exc
        if response.status_code != 200:
            raise GraphError(f"Échec du jeton Graph (HTTP {response.status_code}).")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise GraphError("Réponse du point de jeton Graph sans access_token.")

        _cached_token = token
        expires_in = float(payload.get("expires_in", 3600))
        _token_expires_at = time.monotonic() + max(expires_in - _TOKEN_MARGIN_SECONDS, 60)
        return token


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {jeton_application()}"}


def graph_get(path: str, params: Optional[dict[str, Any]] = None) -> dict:
    """GET a Graph resource.

    Collection responses (carrying ``value``) are merged across
    ``@odata.nextLink`` pages — the shape phase L2's Bookings sync needs.
    ``path`` is relative to /v1.0 (or a full nextLink URL).
    """
    url = path if path.startswith("https://") else _GRAPH_BASE + path
    merged: Optional[dict] = None
    while url:
        try:
            response = _session.get(
                url,
                params=params if merged is None else None,
                headers=_auth_headers(),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise GraphError(
                f"Échec réseau Graph ({type(exc).__name__})."
            ) from exc
        if response.status_code != 200:
            raise GraphError(f"Échec d'un GET Graph (HTTP {response.status_code}).")
        data = response.json()
        if "value" not in data:
            return data
        if merged is None:
            merged = data
        else:
            merged["value"].extend(data["value"])
        url = data.get("@odata.nextLink", "")
    assert merged is not None
    merged.pop("@odata.nextLink", None)
    return merged


def graph_post(path: str, json_body: dict) -> Optional[dict]:
    """POST to a Graph endpoint; returns the JSON body, or None on 202/204."""
    try:
        response = _session.post(
            _GRAPH_BASE + path,
            json=json_body,
            headers=_auth_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    if response.status_code not in (200, 201, 202, 204):
        raise GraphError(f"Échec d'un POST Graph (HTTP {response.status_code}).")
    if response.status_code in (202, 204) or not response.content:
        return None
    return response.json()


def graph_patch(path: str, json_body: dict) -> Optional[dict]:
    """PATCH a Graph resource; returns the JSON body, or None on 204/empty.

    Introduced by the Outlook mirror (miroir des audiences) — Graph answers a
    PATCH on an event with 200 + the updated resource.
    """
    try:
        response = _session.patch(
            _GRAPH_BASE + path,
            json=json_body,
            headers=_auth_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    if response.status_code not in (200, 204):
        raise GraphError(f"Échec d'un PATCH Graph (HTTP {response.status_code}).")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def graph_delete(path: str) -> None:
    """DELETE a Graph resource (204 expected)."""
    try:
        response = _session.delete(
            _GRAPH_BASE + path,
            headers=_auth_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    if response.status_code != 204:
        raise GraphError(f"Échec d'un DELETE Graph (HTTP {response.status_code}).")
