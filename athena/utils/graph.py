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
    """A Graph or token-endpoint call failed (status code in the message).

    Since 2026-08-28 it also CARRIES the status and any ``Retry-After``, so a
    caller can branch on an int instead of parsing a French sentence for an
    HTTP code. Both are keyword-only and default to ``None``: every existing
    raise site and every ``except GraphError`` is unchanged, and
    ``str(exc)`` is byte-identical to what it was.

    Only the verbs added for the mailbox populate them. The four original
    verbs are left exactly as they were — the Outlook mirror's sizing
    (``_PLAFOND_MUTATIONS`` against gunicorn ``--timeout 60``) is calibrated
    on their behaviour, and this module is shared by three live subsystems.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        retry_after_s: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


class GraphNotConfigured(GraphError):
    """The GRAPH_* configuration is absent — outbound email is disabled."""


class GraphTooLarge(GraphError):
    """A bounded byte fetch exceeded its ceiling; the body was NOT read on."""


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


# ── Bounded verbs (mailbox lot, 2026-08-28) ─────────────────────────────────
#
# The four verbs above are UNTOUCHED on purpose: three live subsystems depend
# on their exact behaviour, and the Outlook mirror is sized against it
# (_PLAFOND_MUTATIONS against gunicorn --timeout 60). These are siblings, not
# replacements. graph_get's unbounded nextLink merge is safe for a calendar
# window; it is not safe for a mailbox, where one query can walk thousands of
# messages and every page carries bodies.
#
# The ~14 duplicated lines of a one-page GET are the cheaper of two risks.

# Backstop for a JSON page. NOT the primary control — the primary control is
# always a $select that keeps the payload small (an attachment listing without
# one inlines every attachment as base64). This catches the day someone drops
# the $select.
GRAPH_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# An absolute URL reaching these verbs must be Graph's own. The bounded verbs
# attach the application's bearer token to every request (_merged_headers), so
# a caller-supplied host is a credential-exfiltration path — and the mailbox
# reader takes a continuation token from the MODEL, which is reading email
# written by anyone who knows the address. The original four verbs are not
# touched: they only ever see an absolute URL produced by their own nextLink
# loop, never a caller argument.
_ABSOLUTE_PREFIX = "https://graph.microsoft.com/"


def _resolve_url(path: str) -> tuple[str, bool]:
    """(url, was_absolute). Refuses an absolute URL off the Graph host."""
    if path.startswith("https://"):
        if not path.startswith(_ABSOLUTE_PREFIX):
            raise GraphError(
                "URL Graph refusée : une adresse absolue doit viser "
                "graph.microsoft.com."
            )
        return path, True
    return _GRAPH_BASE + path, False


def _merged_headers(extra_headers: Optional[dict[str, str]]) -> dict[str, str]:
    """Caller headers merged UNDER the auth header.

    The direction is load-bearing: {**extra, **auth} lets a caller send a
    Prefer header but can never replace Authorization. The reverse spelling
    reads identically and hands a caller the ability to send an
    unauthenticated request that fails as a bare 401 explaining nothing.
    """
    return {**(extra_headers or {}), **_auth_headers()}


def _retry_after_seconds(response) -> Optional[float]:
    """Retry-After in seconds, or None. The HTTP-date form is not parsed — a
    caller with no number falls back to its own backoff."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _declared_length(response) -> Optional[int]:
    try:
        return int(response.headers.get("Content-Length") or "")
    except ValueError:
        return None


def _read_capped(response, max_bytes: int, chunk_size: int) -> bytes:
    """Accumulate a streamed body, refusing past *max_bytes*.

    The caller MUST hold the response open (a with-statement). The refusal
    path is exactly where an un-released connection would leak, and urllib3
    pools only 10 per host — ten refusals would start failing OTHER
    subsystems sharing this module's Session.
    """
    declared = _declared_length(response)
    if declared is not None and declared > max_bytes:
        raise GraphTooLarge(
            f"Réponse Graph trop volumineuse ({declared} > {max_bytes} octets).",
            status=response.status_code,
        )
    buffer = bytearray()
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise GraphTooLarge(
                f"Réponse Graph trop volumineuse (> {max_bytes} octets).",
                status=response.status_code,
            )
    return bytes(buffer)


def graph_get_page(
    path: str,
    params: Optional[dict[str, Any]] = None,
    *,
    extra_headers: Optional[dict[str, str]] = None,
    max_bytes: int = GRAPH_MAX_RESPONSE_BYTES,
    timeout: Optional[int] = None,
    chunk_size: int = 65536,
) -> dict:
    """GET exactly ONE page. The @odata.nextLink is left INTACT in the result.

    The opposite contract to graph_get, which merges every page: here the
    caller owns the loop and therefore owns the bound. An absolute URL (a
    nextLink) is passed verbatim and *params* is ignored, because a nextLink
    already carries the query string.
    """
    url, absolute = _resolve_url(path)
    try:
        response = _session.get(
            url,
            params=None if absolute else params,
            headers=_merged_headers(extra_headers),
            timeout=timeout or _HTTP_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    with response:
        if response.status_code != 200:
            raise GraphError(
                f"Échec d'un GET Graph (HTTP {response.status_code}).",
                status=response.status_code,
                retry_after_s=_retry_after_seconds(response),
            )
        raw = _read_capped(response, max_bytes, chunk_size)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GraphError("Réponse Graph illisible (JSON invalide).") from exc


def graph_get_bytes(
    path: str,
    *,
    max_bytes: int,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
    chunk_size: int = 65536,
) -> tuple[bytes, str]:
    """GET raw bytes (a $value segment), bounded. Returns (bytes, type).

    The four original verbs all do response.json(), so a MIME message or an
    attachment's bytes are unreachable through them.
    """
    url, _absolute = _resolve_url(path)
    try:
        response = _session.get(
            url,
            headers=_merged_headers(extra_headers),
            timeout=timeout or _HTTP_TIMEOUT,
            stream=True,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    with response:
        if response.status_code != 200:
            raise GraphError(
                f"Échec d'un GET Graph (HTTP {response.status_code}).",
                status=response.status_code,
                retry_after_s=_retry_after_seconds(response),
            )
        payload = _read_capped(response, max_bytes, chunk_size)
        content_type = str(response.headers.get("Content-Type") or "")
    return payload, content_type


def graph_send(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> tuple[Optional[dict], int]:
    """POST or PATCH, surfacing the STATUS alongside the body.

    graph_post and graph_patch collapse 201 and 204 into None, which loses the
    one bit a draft-creation caller needs. Status-bearing errors are what let
    the mail layer retry a 429 and never retry a 5xx on a write.
    """
    verb = method.upper()
    if verb not in ("POST", "PATCH"):
        raise ValueError(f"graph_send: unsupported method {verb!r}")
    try:
        response = _session.request(
            verb,
            _GRAPH_BASE + path,
            json=json_body,
            headers=_merged_headers(extra_headers),
            timeout=timeout or _HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GraphError(f"Échec réseau Graph ({type(exc).__name__}).") from exc
    if response.status_code not in (200, 201, 202, 204):
        raise GraphError(
            f"Échec d'un {verb} Graph (HTTP {response.status_code}).",
            status=response.status_code,
            retry_after_s=_retry_after_seconds(response),
        )
    if response.status_code in (202, 204) or not response.content:
        return None, response.status_code
    try:
        return response.json(), response.status_code
    except ValueError as exc:
        raise GraphError("Réponse Graph illisible (JSON invalide).") from exc
