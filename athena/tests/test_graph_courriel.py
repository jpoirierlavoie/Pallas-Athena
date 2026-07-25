"""Microsoft Graph token cache + sendMail payload (portail client, spec L1).

Pins §13 criteria (i) — token cache renews exactly once on expiry — and (j)
— the sendMail payload is conforme (recipient, HTML, saveToSentItems: true).
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from config import Config  # noqa: E402
from utils import courriel, graph  # noqa: E402


@pytest.fixture(autouse=True)
def _graph_configured(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(Config, "GRAPH_SENDER_UPN", "juriste@example.com")
    graph._reset_token_cache_for_tests()
    yield
    graph._reset_token_cache_for_tests()


def _token_response(token: str = "tok-1", expires_in: int = 3600):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": token, "expires_in": expires_in}
    return resp


# ── Token cache ──────────────────────────────────────────────────────────


def test_token_cached_then_renewed_once_on_expiry():
    with mock.patch.object(graph.requests, "post", return_value=_token_response()) as post:
        assert graph.jeton_application() == "tok-1"
        assert graph.jeton_application() == "tok-1"  # cache hit — no 2nd POST
        assert post.call_count == 1

        # Simulated expiry: exactly ONE renewal POST, then cached again.
        graph._token_expires_at = 0.0
        post.return_value = _token_response("tok-2")
        assert graph.jeton_application() == "tok-2"
        assert graph.jeton_application() == "tok-2"
        assert post.call_count == 2


def test_token_request_shape():
    with mock.patch.object(graph.requests, "post", return_value=_token_response()) as post:
        graph.jeton_application()
    url = post.call_args.args[0]
    data = post.call_args.kwargs["data"]
    assert url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    assert data["grant_type"] == "client_credentials"
    assert data["scope"] == "https://graph.microsoft.com/.default"


def test_unconfigured_raises_not_configured(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "")
    with pytest.raises(graph.GraphNotConfigured):
        graph.jeton_application()


def test_token_failure_message_has_status_never_body():
    resp = mock.Mock()
    resp.status_code = 401
    resp.text = "AADSTS-secret-detail"
    with mock.patch.object(graph.requests, "post", return_value=resp):
        with pytest.raises(graph.GraphError) as exc:
            graph.jeton_application()
    assert "401" in str(exc.value)
    assert "AADSTS" not in str(exc.value)


# ── graph_get pagination ─────────────────────────────────────────────────


def test_graph_get_follows_next_link():
    page1 = mock.Mock(status_code=200)
    page1.json.return_value = {
        "value": [1, 2],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/x?$skip=2",
    }
    page2 = mock.Mock(status_code=200)
    page2.json.return_value = {"value": [3]}
    with mock.patch.object(graph.requests, "post", return_value=_token_response()):
        with mock.patch.object(graph.requests, "get", side_effect=[page1, page2]) as get:
            merged = graph.graph_get("/x")
    assert merged["value"] == [1, 2, 3]
    assert "@odata.nextLink" not in merged
    assert get.call_count == 2


# ── sendMail payload (§13.j) ─────────────────────────────────────────────


def test_envoyer_sendmail_payload_conforme():
    send = mock.Mock(status_code=202, content=b"")
    with mock.patch.object(graph.requests, "post", side_effect=[_token_response(), send]) as post:
        courriel.envoyer("client@exemple.com", "Objet test", "<p>Bonjour</p>")

    url = post.call_args.args[0]
    body = post.call_args.kwargs["json"]
    assert url.endswith("/users/juriste@example.com/sendMail")
    assert body["saveToSentItems"] is True
    assert body["message"]["subject"] == "Objet test"
    assert body["message"]["body"] == {"contentType": "HTML", "content": "<p>Bonjour</p>"}
    assert body["message"]["toRecipients"] == [
        {"emailAddress": {"address": "client@exemple.com"}}
    ]


def test_envoyer_raises_on_http_failure():
    send = mock.Mock(status_code=500, content=b"boom")
    with mock.patch.object(graph.requests, "post", side_effect=[_token_response(), send]):
        with pytest.raises(graph.GraphError):
            courriel.envoyer("client@exemple.com", "x", "y")
