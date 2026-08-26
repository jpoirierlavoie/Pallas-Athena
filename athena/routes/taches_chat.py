"""Gestionnaire MACHINE des tours de clavardage (Phase N) — service « chat ».

CSRF-exempt-by-construction: this blueprint is registered ONLY on the chat
service (chat/app.py), whose process has no CSRFProtect at all — no browser
ever reaches it. Origin proof is the ``X-AppEngine-QueueName`` header, which
App Engine strips from ALL external traffic (the taches_portail doctrine);
the handler re-checks the exact queue name, never mere presence.

Outcome → HTTP mapping (the queue's contract):

* transient (``ChatVertexRetryable`` — Vertex 429/5xx/timeout, enqueue
  failure, unreadable conversation) → **503** → Cloud Tasks retries on the
  queue's backoff;
* everything else → **200** — the task is consumed. A malformed payload is
  a bug, not a transient (retrying it ten times would change nothing); a
  deterministic failure has already terminalized the turn LOUDLY inside the
  engine; a duplicate/skip observed the advanced state without calling
  Vertex.
"""

from __future__ import annotations

import logging

from flask import Blueprint, abort, request

from chat import turn_engine
from chat.taches import CHAT_QUEUE
from chat.vertex import ChatVertexRetryable
from utils.logging_setup import log_chat_event

logger = logging.getLogger(__name__)

taches_chat_bp = Blueprint("taches_chat", __name__, url_prefix="/taches/chat")


@taches_chat_bp.post("/tour")
def tour():
    if request.headers.get("X-AppEngine-QueueName") != CHAT_QUEUE:
        abort(403)
    try:
        retry_count = int(request.headers.get("X-AppEngine-TaskRetryCount", "0"))
    except ValueError:
        retry_count = 0

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        logger.warning("chat task: malformed payload (not a JSON object)")
        return "requête ignorée", 200

    try:
        outcome = turn_engine.process_task(payload, retry_count)
    except ChatVertexRetryable as exc:
        log_chat_event(
            "chat_model_call",
            "failure",
            conversation_id=str(payload.get("conversation_id") or ""),
            turn_id=str(payload.get("turn_id") or ""),
            reason=exc.reason,
            retry_count=retry_count,
        )
        return "nouvelle tentative demandée", 503

    if outcome == "malformed":
        logger.warning("chat task: malformed payload (missing ids)")
        return "requête ignorée", 200
    return outcome, 200
