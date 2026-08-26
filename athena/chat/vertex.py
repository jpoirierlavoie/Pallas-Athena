"""The Vertex AI Messages API client (Phase N) — transport only.

Raw ``requests`` + ``google-auth`` (both already pinned — no Anthropic SDK,
zero new dependencies in this seam). The turn engine owns assembly, cache
breakpoints and state; this module owns exactly: the URL, the auth, the
body envelope, the timeouts, and the error taxonomy.

Endpoint: ``global`` —
``https://{CHAT_VERTEX_HOST}/v1/projects/{p}/locations/{CHAT_VERTEX_LOCATION}
/publishers/anthropic/models/{vertex_model_id}:rawPredict``. Verified live
2026-08-26: it is the ONLY location serving these models (the spec's
multi-region ``us`` answers 501, ``us-east5`` 404s them), and it bills at
the base rate. Host and location stay env-overridable so a regional
endpoint, if these models ever gain one, is a config edit. Not
``streamRawPredict``: no streaming, by design (SPEC §2).

Auth is ADC of the service account (refreshed PER CALL — the OTLP
``credentials=`` lesson: anything frozen at construction expires in an
hour). No API key exists anywhere.

Error taxonomy (SPEC §2.2 — no in-task retry loop; a second multi-minute
attempt inside one task could straddle the 10-minute platform deadline):

* 429 / 5xx / timeout / connection error → :class:`ChatVertexRetryable` —
  the handler propagates it as a 5xx and CLOUD TASKS retries on the
  queue's backoff. A failed call bills nothing (caveat: a client-side
  read-timeout may still have completed and billed server-side — visible
  only through the started/recorded drift counters).
* 400 / 401 / 403 / 404 → :class:`ChatVertexFatal` — retrying an invalid
  request ten times changes nothing (the taches_portail malformed-payload
  doctrine); the engine finalizes the turn ``failed`` with the
  machine-stable reason. The bounded error excerpt goes in the TURN DOC
  only (Firestore is privileged storage); logs carry codes, never bodies.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

import google.auth
import google.auth.transport.requests as google_auth_requests
import requests

from config import Config

ANTHROPIC_VERSION = "vertex-2023-10-16"
_ERROR_EXCERPT_CHARS = 2000
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


class ChatVertexRetryable(Exception):
    """Transient — propagate; Cloud Tasks retries on the queue's backoff."""

    def __init__(self, reason: str, status: Optional[int] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class ChatVertexFatal(Exception):
    """Permanent — the turn finalizes ``failed``; never retried."""

    def __init__(
        self,
        reason: str,
        status: Optional[int] = None,
        excerpt: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        # Bounded; stored on the turn doc ONLY — never logged, never in a
        # span (a Vertex 400 body can quote the request's privileged text).
        self.excerpt = (excerpt or "")[:_ERROR_EXCERPT_CHARS]


@lru_cache(maxsize=1)
def _credentials():
    creds, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return creds


def _bearer_token() -> str:
    creds = _credentials()
    if not creds.valid:
        creds.refresh(google_auth_requests.Request())
    return creds.token


def model_config(model_key: str) -> dict:
    """The allowlist entry for *model_key*, pre-flight validated.

    Raises :class:`ChatVertexFatal` on an unknown model (the allowlist is
    CLOSED — SPEC §9) or an effort level outside the API's vocabulary;
    spending a multi-minute call to discover a config error would be the
    expensive way to read this function.
    """
    cfg = Config.CHAT_MODELS.get(model_key)
    if cfg is None:
        raise ChatVertexFatal("unknown_model")
    if cfg.get("effort") not in Config.VERTEX_EFFORTS:
        raise ChatVertexFatal("config_effort")
    return cfg


def endpoint_url(model_key: str) -> str:
    cfg = model_config(model_key)
    return (
        f"https://{Config.CHAT_VERTEX_HOST}/v1/projects/"
        f"{Config.FIREBASE_PROJECT_ID}/locations/"
        f"{Config.CHAT_VERTEX_LOCATION}/publishers/anthropic/models/"
        f"{cfg['vertex_model_id']}:rawPredict"
    )


def call_model(
    model_key: str,
    *,
    system: list[dict],
    messages: list[dict],
    tools: list[dict],
) -> dict[str, Any]:
    """One non-streamed Messages API call. Returns the parsed response.

    The model goes in the URL (never the body); ``anthropic_version`` in
    the body; extended thinking always on.

    **The request surface of this model generation (repaired 2026-08-26).**
    ``thinking: {"type": "enabled", "budget_tokens": N}`` is REMOVED on
    claude-sonnet-5 / claude-opus-5 and returns a **400**, and so does any
    sampling parameter — ``temperature``, ``top_p``, ``top_k``. The current
    shape is ``{"type": "adaptive"}``, steered by ``output_config.effort``.
    This was shipped wrong and could not be observed: the Vertex quota was
    zero from the day the code landed, so no request had ever been sent and
    the whole suite (which mocks the transport) stayed green. Anything added
    to this body must be checked against the CURRENT model generation, never
    against a recalled pattern.

    ``display: "summarized"`` is explicit because the default became
    ``"omitted"`` on these models: left at the default, every thinking block
    comes back with empty text and the transcript's « Réflexion » section
    renders blank. Display never changes what is thought or billed, and the
    replay rule is unaffected — blocks still go back verbatim, signatures
    included.
    """
    cfg = model_config(model_key)
    body: dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": int(cfg["max_tokens"]),
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": str(cfg["effort"])},
        "system": system,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools

    try:
        response = requests.post(
            endpoint_url(model_key),
            json=body,
            headers={
                "Authorization": f"Bearer {_bearer_token()}",
                "Content-Type": "application/json",
            },
            timeout=(
                Config.CHAT_VERTEX_CONNECT_TIMEOUT_S,
                Config.CHAT_VERTEX_READ_TIMEOUT_S,
            ),
        )
    except requests.Timeout as exc:
        raise ChatVertexRetryable("vertex_timeout") from exc
    except requests.RequestException as exc:
        raise ChatVertexRetryable("vertex_connection_error") from exc

    if response.status_code in _RETRYABLE_STATUSES:
        raise ChatVertexRetryable(
            f"vertex_http_{response.status_code}", response.status_code
        )
    if response.status_code == 400:
        raise ChatVertexFatal(
            "vertex_invalid_request", 400, response.text
        )
    if response.status_code in (401, 403):
        raise ChatVertexFatal(
            "vertex_permission", response.status_code, response.text
        )
    if response.status_code == 404:
        raise ChatVertexFatal(
            "vertex_endpoint_absent", 404, response.text
        )
    if response.status_code != 200:
        # An unmapped status is treated as transient: wrongly retrying a
        # permanent error costs a few queue attempts; wrongly failing a
        # transient one kills a turn.
        raise ChatVertexRetryable(
            f"vertex_http_{response.status_code}", response.status_code
        )

    try:
        parsed = response.json()
    except ValueError as exc:
        raise ChatVertexFatal("vertex_bad_response", 200) from exc
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("content"), list)
        or "stop_reason" not in parsed
        or not isinstance(parsed.get("usage"), dict)
    ):
        raise ChatVertexFatal("vertex_bad_response", 200)
    return parsed


# ── Pricing (SPEC §7 — snapshot from config, never code) ────────────────────


def segment_cost_usd_micros(usage: dict, model_key: str) -> int:
    """Cost of one call's usage block, in USD MICROS (int — never float
    money past this boundary).

    Reads the CONFIG snapshot (``CHAT_PRICING``): per-Mtok rates × the
    multi-region multiplier, plus web searches. Unknown model → 0 with the
    honest consequence that the indicator under-reports — the pricing
    version stamped on the segment is what lets a later rate change never
    silently re-price history.
    """
    pricing = Config.CHAT_PRICING
    rates = pricing.get("models", {}).get(model_key)
    if not rates:
        return 0
    multiplier = float(pricing.get("multiregion_multiplier", 1.0))

    def _tok(key: str) -> int:
        return int(usage.get(key) or 0)

    usd = (
        _tok("input_tokens") * rates["input_usd_per_mtok"]
        + _tok("output_tokens") * rates["output_usd_per_mtok"]
        + _tok("cache_creation_input_tokens") * rates["cache_write_usd_per_mtok"]
        + _tok("cache_read_input_tokens") * rates["cache_read_usd_per_mtok"]
    ) / 1_000_000.0
    usd *= multiplier
    searches = int((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
    usd += searches * float(pricing.get("web_search_usd_per_1000", 0.0)) / 1000.0
    return int(round(usd * 1_000_000))
