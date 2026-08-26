"""Bounded HTTP client for the legal-research Workers (Phase N).

Server-to-server REST with a bearer token per Worker (D5/D10). The contract
with the turn engine: :func:`call_worker` NEVER raises — every failure
becomes ``{"ok": False, reason, message}`` that the executor turns into an
error tool_result, visible in the transcript (fail loud, degrade never,
SPEC §4.5). No automatic retry: a retry doubles latency inside a budgeted
turn chain, and the model retries visibly if it judges it worthwhile.

Bounds: connect 5 s / read 30 s; the response body is read in bounded
chunks and REFUSED past ``RESPONSE_CAP_BYTES`` (a truncated JSON document
would parse into a silently wrong answer — refusing is honest). ``reason``
values are machine-stable (``not_configured``, ``timeout``,
``connection_error``, ``http_502``, ``invalid_json``,
``response_too_large``) and are the ONLY thing logged — a Worker response
may quote legal content and never reaches the logs.
"""

from __future__ import annotations

from typing import Any

import requests

from config import Config

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
RESPONSE_CAP_BYTES = 2 * 1024 * 1024


def _base_url(worker: str) -> str:
    if worker == "legislation":
        return Config.LEGISLATION_WORKER_URL.rstrip("/")
    if worker == "jurisprudence":
        return Config.JURISPRUDENCE_WORKER_URL.rstrip("/")
    return ""


def _token(worker: str) -> str:
    if worker == "legislation":
        return Config.LEGISLATION_WORKER_TOKEN
    if worker == "jurisprudence":
        return Config.JURISPRUDENCE_WORKER_TOKEN
    return ""


def call_worker(spec: dict, args: dict) -> dict[str, Any]:
    """Execute one Worker tool call. Returns ``{"ok": True, "payload": …}``
    or ``{"ok": False, "reason": …, "message": …}`` (French message)."""
    worker = str(spec.get("worker", ""))
    if not Config.worker_configured(worker):
        return {
            "ok": False,
            "reason": "not_configured",
            "message": (
                f"L'outil {spec.get('name', '?')} est indisponible : le "
                f"service « {worker} » n'est pas configuré."
            ),
        }
    url = _base_url(worker) + str(spec.get("path", ""))
    try:
        response = requests.post(
            url,
            json=args,
            headers={"Authorization": f"Bearer {_token(worker)}"},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            stream=True,
        )
    except requests.Timeout:
        return {
            "ok": False,
            "reason": "timeout",
            "message": (
                f"Le service « {worker} » n'a pas répondu à temps. "
                "Réessayez si le résultat est nécessaire."
            ),
        }
    except requests.RequestException:
        return {
            "ok": False,
            "reason": "connection_error",
            "message": f"Le service « {worker} » est injoignable.",
        }

    with response:
        if response.status_code != 200:
            return {
                "ok": False,
                "reason": f"http_{response.status_code}",
                "message": (
                    f"Le service « {worker} » a répondu avec l'erreur "
                    f"HTTP {response.status_code}."
                ),
            }
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > RESPONSE_CAP_BYTES:
                # A truncated JSON body would parse into a silently wrong
                # answer — refuse the oversized response outright instead.
                return {
                    "ok": False,
                    "reason": "response_too_large",
                    "message": (
                        f"La réponse du service « {worker} » dépasse la "
                        "taille admise (2 Mo). Restreignez la requête."
                    ),
                }
            chunks.append(chunk)

    try:
        import json

        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return {
            "ok": False,
            "reason": "invalid_json",
            "message": f"Le service « {worker} » a répondu un corps illisible.",
        }
    return {"ok": True, "payload": payload}
