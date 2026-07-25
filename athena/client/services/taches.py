"""Cloud Tasks enqueue — the portal's signal channel (spec L1 §8.2).

The portal never writes invitation state; it enqueues an event onto the
« portail » queue and the MAIN service's handler
(``/taches/portail/evenement``, service ``default``) performs the mutation.
IAM backstop: ``roles/cloudtasks.enqueuer`` on the queue — create tasks,
nothing else.

No deterministic task names (native dedup adds latency + a tombstone
window); the HANDLER's idempotence is the guarantee, duplicates are
harmless (spec §8.2). The client is lazy so tests and an infra-less boot
never touch the API.

The main service's reconciliation cron imports this same module to
re-enqueue lost submissions (§8.4).
"""

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from client.config import PORTAIL_QUEUE, TASKS_LOCATION

from utils.logging_setup import log_portail_event

logger = logging.getLogger(__name__)

EVENEMENTS = ("ouverte", "soumise", "renvoi")


@lru_cache(maxsize=1)
def _client():
    from google.cloud import tasks_v2

    return tasks_v2.CloudTasksClient()


def signaler(event: str, invitation_id: str, batch: str | None = None) -> None:
    """Enqueue one event (schéma Annexe B.3). Raises on enqueue failure —
    each caller decides whether that failure is fatal (it is NOT at
    finalization: the envelope is already written, spec §7.4)."""
    if event not in EVENEMENTS:
        raise ValueError(f"unknown portail event: {event}")
    from google.cloud import tasks_v2

    corps = {
        "event": event,
        "invitation_id": invitation_id,
        "batch": batch,
        "emis_at": datetime.now(timezone.utc).isoformat(),
    }
    client = _client()
    parent = client.queue_path(
        os.environ["FIREBASE_PROJECT_ID"], TASKS_LOCATION, PORTAIL_QUEUE
    )
    client.create_task(
        request={
            "parent": parent,
            "task": {
                "app_engine_http_request": {
                    "http_method": tasks_v2.HttpMethod.POST,
                    "relative_uri": "/taches/portail/evenement",
                    "app_engine_routing": {"service": "default"},
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(corps).encode(),
                }
            },
        }
    )
    log_portail_event(
        "tache_enfilee", invitation_id=invitation_id, batch=batch, evenement=event
    )
