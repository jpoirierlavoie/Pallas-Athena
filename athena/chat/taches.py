"""Enfilage des tours de clavardage sur la file « chat-turns » (Phase N).

Importable by BOTH services (the ``client/services/taches.py`` precedent —
same source tree, both processes): the default service enqueues the FIRST
task of a turn (message POST, authorization decision, scheduled dispatch),
the chat service enqueues its own continuations.

``app_engine_routing`` targets the **chat** service — the one-field change
the portail enqueuer proved. No deterministic task names (native dedup adds
latency + a tombstone window); the HANDLER's idempotence — the step-token
claim — is the guarantee. No ``dispatch_deadline``: for App Engine targets
the auto-scaling 10-minute request deadline governs, and the real per-call
bound is the Vertex client's read timeout (540 s) under gunicorn's 570 s.

Raises on enqueue failure — each caller decides: the turn-engine commit
RAISES so the queue retries into the repair branch; the browser POST
surfaces a French banner with the turn left visibly pending.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from client.config import TASKS_LOCATION  # plain constant, both-services safe

CHAT_QUEUE = "chat-turns"
CHAT_SERVICE = "chat"
TASK_URI = "/taches/chat/tour"


@lru_cache(maxsize=1)
def _client():
    from google.cloud import tasks_v2

    return tasks_v2.CloudTasksClient()


def enfiler_tour(conversation_id: str, turn_id: str, step_token: str) -> None:
    """Enqueue one worker task (ids only — far under the 1 MB cap)."""
    from google.cloud import tasks_v2

    client = _client()
    parent = client.queue_path(
        os.environ["FIREBASE_PROJECT_ID"], TASKS_LOCATION, CHAT_QUEUE
    )
    corps = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "step_token": step_token,
    }
    client.create_task(
        request={
            "parent": parent,
            "task": {
                "app_engine_http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "relative_uri": TASK_URI,
                    "app_engine_routing": {"service": CHAT_SERVICE},
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(corps).encode(),
                }
            },
        }
    )
