"""chat/app.py — the worker service's route-map isolation (Phase N).

The test_portail_app pattern: the chat process must expose EXACTLY the
machine handler + warmup — no browser route, no session, no CSRF object.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from chat.app import create_chat_app


def _build():
    return create_chat_app()


def test_route_map_is_exactly_the_worker_set():
    app = _build()
    rules = {r.rule for r in app.url_map.iter_rules()}
    rules.discard("/static/<path:filename>")  # Flask's built-in
    assert rules == {"/taches/chat/tour", "/_ah/warmup"}


def test_no_browser_surface_leaks_into_the_worker():
    app = _build()
    for rule in app.url_map.iter_rules():
        for forbidden in ("/chat", "/dossiers", "/mcp", "/dav", "/auth",
                          "/fideicommis", "/administration"):
            assert not rule.rule.startswith(forbidden), rule.rule


def test_worker_guard_refuses_external_posts():
    app = _build()
    client = app.test_client()
    # No X-AppEngine-QueueName (App Engine strips it from external traffic).
    response = client.post("/taches/chat/tour", json={})
    assert response.status_code == 403
    # Wrong queue name is refused too — exact value, never mere presence.
    response = client.post(
        "/taches/chat/tour",
        json={},
        headers={"X-AppEngine-QueueName": "portail"},
    )
    assert response.status_code == 403


def test_malformed_payload_is_consumed_not_retried():
    app = _build()
    client = app.test_client()
    response = client.post(
        "/taches/chat/tour",
        data="pas du json",
        content_type="application/json",
        headers={"X-AppEngine-QueueName": "chat-turns"},
    )
    assert response.status_code == 200
    response = client.post(
        "/taches/chat/tour",
        json={"conversation_id": ""},
        headers={"X-AppEngine-QueueName": "chat-turns"},
    )
    assert response.status_code == 200


def test_retryable_engine_error_answers_503(monkeypatch):
    from chat import turn_engine
    from chat.vertex import ChatVertexRetryable

    app = _build()
    monkeypatch.setattr(
        turn_engine,
        "process_task",
        mock.Mock(side_effect=ChatVertexRetryable("vertex_http_503")),
    )
    client = app.test_client()
    response = client.post(
        "/taches/chat/tour",
        json={"conversation_id": "c", "turn_id": "t", "step_token": "s"},
        headers={"X-AppEngine-QueueName": "chat-turns"},
    )
    assert response.status_code == 503


def test_warmup_answers_200():
    app = _build()
    assert app.test_client().get("/_ah/warmup").status_code == 200
