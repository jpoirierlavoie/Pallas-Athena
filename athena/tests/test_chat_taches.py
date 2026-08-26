"""chat/taches.py — the chat-turns enqueue payload (Phase N).

Pins the ONE-field routing decision: app_engine_routing targets the
« chat » service, the body is ids-only JSON, the URI matches the machine
blueprint, and a failure PROPAGATES (each caller decides)."""

import json
import os
import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from chat import taches  # noqa: E402


class _FakeTasksClient:
    created = []

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, request):
        _FakeTasksClient.created.append(request)


def _stub_tasks_v2(monkeypatch):
    stub = types.ModuleType("google.cloud.tasks_v2")
    stub.CloudTasksClient = _FakeTasksClient
    stub.HttpMethod = types.SimpleNamespace(POST="POST")
    monkeypatch.setitem(sys.modules, "google.cloud.tasks_v2", stub)
    taches._client.cache_clear()
    _FakeTasksClient.created = []


def test_enqueue_targets_the_chat_service_with_ids_only(monkeypatch):
    _stub_tasks_v2(monkeypatch)
    taches.enfiler_tour("conv-1", "000002", "jeton-abc")
    assert len(_FakeTasksClient.created) == 1
    request = _FakeTasksClient.created[0]
    assert request["parent"].endswith("/queues/chat-turns")
    aehr = request["task"]["app_engine_http_request"]
    assert aehr["relative_uri"] == "/taches/chat/tour"
    # THE routing decision: the worker runs on the dedicated service.
    assert aehr["app_engine_routing"] == {"service": "chat"}
    body = json.loads(aehr["body"].decode())
    assert body == {
        "conversation_id": "conv-1",
        "turn_id": "000002",
        "step_token": "jeton-abc",
    }


def test_enqueue_failure_propagates_to_the_caller(monkeypatch):
    _stub_tasks_v2(monkeypatch)
    monkeypatch.setattr(
        _FakeTasksClient,
        "create_task",
        mock.Mock(side_effect=RuntimeError("file indisponible")),
    )
    with pytest.raises(RuntimeError):
        taches.enfiler_tour("c", "t", "s")


def test_queue_name_is_the_single_source_for_the_route_guard():
    from routes.taches_chat import CHAT_QUEUE as guard_queue

    assert guard_queue == taches.CHAT_QUEUE == "chat-turns"
