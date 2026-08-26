"""Répartiteur cron des tâches planifiées du clavardage (Phase N §12.2).

MACHINE blueprint on the DEFAULT service (all cron entries target default;
the dispatcher is millisecond work — reading a tiny collection and
enqueueing; the TURNS it enqueues run on the chat service via the queue
routing). Guarded by ``X-Appengine-Cron`` — set only by App Engine's cron
dispatcher, stripped from all external traffic (the taches_bookings shape).
CSRF-exempted in main.py as its own machine blueprint, never a browser one.

A sweep NEVER 5xxes for one task's failure — each failure is logged loudly
(chat_scheduled_dispatch/chat_scheduled_repair at ERROR) and the sweep
continues; the next 15-minute run retries whatever is still due (the
occurrence CAS makes that free).
"""

from __future__ import annotations

from flask import Blueprint, abort, jsonify, request

taches_chat_cron_bp = Blueprint(
    "taches_chat_cron", __name__, url_prefix="/taches/chat"
)


@taches_chat_cron_bp.get("/planification")
def planification():
    if request.headers.get("X-Appengine-Cron") != "true":
        abort(403)
    from chat.planification import executer_balayage

    return jsonify(executer_balayage()), 200
