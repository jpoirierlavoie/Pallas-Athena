"""The bounded Graph verbs added for the mailbox lot (2026-08-28).

`utils/graph.py` grew three siblings — `graph_get_page`, `graph_get_bytes`,
`graph_send` — because the four original verbs cannot serve a mailbox:
`graph_get` merges EVERY nextLink page with no cap of any kind, all four do
`response.json()` so raw bytes are unreachable, and none accepts a header, so
`Prefer: outlook.body-content-type: text` cannot be sent.

What these tests pin is mostly what must NOT happen.
"""

import io
import json
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
from utils import graph  # noqa: E402


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(Config, "GRAPH_SENDER_UPN", "juriste@example.com")
    graph._reset_token_cache_for_tests()
    # Every test here drives the token cache directly so no POST is needed.
    graph._cached_token = "tok-1"
    graph._token_expires_at = float("inf")
    yield
    graph._reset_token_cache_for_tests()


class _Resp:
    """A streamed response that records whether it was closed."""

    def __init__(self, *, status=200, body=b"", headers=None, chunks=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False
        self.content = body

    def iter_content(self, chunk_size=65536):
        if self._chunks is not None:
            yield from self._chunks
            return
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def json(self):
        return json.loads(self._body.decode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _json_resp(payload, **kw):
    return _Resp(body=json.dumps(payload).encode("utf-8"), **kw)


# ── The header merge direction ──────────────────────────────────────────────


def test_caller_headers_can_add_prefer_but_never_replace_authorization():
    """The whole point of the merge direction. `{**auth, **extra}` reads
    identically and would let a caller send an unauthenticated request that
    fails as a bare 401 explaining nothing."""
    merged = graph._merged_headers(
        {
            "Prefer": "outlook.body-content-type=\"text\"",
            "Authorization": "Bearer forged",
        }
    )
    assert merged["Prefer"] == 'outlook.body-content-type="text"'
    assert merged["Authorization"] == "Bearer tok-1"


def test_prefer_header_actually_reaches_the_wire():
    resp = _json_resp({"value": []})
    with mock.patch.object(graph._session, "get", return_value=resp) as get:
        graph.graph_get_page(
            "/users/x/messages",
            {"$top": 1},
            extra_headers={"Prefer": 'outlook.body-content-type="text"'},
        )
    sent = get.call_args.kwargs["headers"]
    assert sent["Prefer"] == 'outlook.body-content-type="text"'
    assert sent["Authorization"] == "Bearer tok-1"


# ── graph_get_page: ONE page, nextLink intact ───────────────────────────────


def test_get_page_fetches_exactly_one_page_and_keeps_the_nextlink():
    """The contract that is the opposite of graph_get. If this ever merged,
    a mailbox query would walk thousands of messages carrying bodies."""
    resp = _json_resp(
        {"value": [{"id": "m1"}],
         "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
    )
    with mock.patch.object(graph._session, "get", return_value=resp) as get:
        page = graph.graph_get_page("/users/x/messages", {"$top": 1})
    assert get.call_count == 1
    assert page["@odata.nextLink"] == "https://graph.microsoft.com/v1.0/next"
    assert page["value"] == [{"id": "m1"}]


def test_get_page_passes_an_absolute_nextlink_verbatim_and_drops_params():
    """A nextLink already carries the query string; re-applying params would
    silently change the continuation."""
    with mock.patch.object(
        graph._session, "get", return_value=_json_resp({"value": []})
    ) as get:
        graph.graph_get_page(
            "https://graph.microsoft.com/v1.0/users/x/messages?$skip=10",
            {"$top": 99},
        )
    assert get.call_args.args[0] == (
        "https://graph.microsoft.com/v1.0/users/x/messages?$skip=10"
    )
    assert get.call_args.kwargs["params"] is None


def test_get_page_refuses_an_oversized_body_and_releases_the_connection():
    """The backstop for the day someone drops the $select on an attachment
    listing, which inlines every attachment as base64."""
    resp = _Resp(body=b"x" * 5000)
    with mock.patch.object(graph._session, "get", return_value=resp):
        with pytest.raises(graph.GraphTooLarge):
            graph.graph_get_page("/users/x/messages", max_bytes=1000)
    # urllib3 pools 10 connections per host: a leak here would start failing
    # the Outlook mirror and the portal's email, not just this call.
    assert resp.closed is True


def test_get_page_refuses_on_a_declared_length_before_reading_a_byte():
    consumed = []

    def _chunks():
        consumed.append(1)
        yield b"x" * 10

    resp = _Resp(headers={"Content-Length": "99999"}, chunks=_chunks())
    with mock.patch.object(graph._session, "get", return_value=resp):
        with pytest.raises(graph.GraphTooLarge):
            graph.graph_get_page("/users/x/messages", max_bytes=1000)
    assert consumed == []


# ── Status-bearing errors ───────────────────────────────────────────────────


def test_a_429_carries_its_status_and_retry_after():
    """So the mail layer branches on an int instead of parsing a French
    sentence for an HTTP code."""
    resp = _Resp(status=429, headers={"Retry-After": "17"})
    with mock.patch.object(graph._session, "get", return_value=resp):
        with pytest.raises(graph.GraphError) as excinfo:
            graph.graph_get_page("/users/x/messages")
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after_s == 17.0


def test_a_plain_grapherror_still_constructs_with_only_a_message():
    """Additive: every existing raise site in this module passes a message
    alone, and str(exc) must be byte-identical to what it was."""
    exc = graph.GraphError("Échec réseau Graph (Timeout).")
    assert str(exc) == "Échec réseau Graph (Timeout)."
    assert exc.status is None and exc.retry_after_s is None
    assert isinstance(graph.GraphNotConfigured("x"), graph.GraphError)
    assert isinstance(graph.GraphTooLarge("x"), graph.GraphError)


def test_an_unparseable_retry_after_is_none_not_an_exception():
    resp = _Resp(status=503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with mock.patch.object(graph._session, "get", return_value=resp):
        with pytest.raises(graph.GraphError) as excinfo:
            graph.graph_get_page("/x")
    assert excinfo.value.status == 503
    assert excinfo.value.retry_after_s is None


# ── graph_get_bytes ─────────────────────────────────────────────────────────


def test_get_bytes_returns_the_payload_and_its_content_type():
    resp = _Resp(body=b"%PDF-1.7 ...", headers={"Content-Type": "application/pdf"})
    with mock.patch.object(graph._session, "get", return_value=resp):
        data, ctype = graph.graph_get_bytes("/users/x/messages/m1/$value",
                                            max_bytes=1024)
    assert data == b"%PDF-1.7 ..."
    assert ctype == "application/pdf"
    assert resp.closed is True


def test_get_bytes_stops_mid_stream_at_the_cap():
    """The cap must bite on the RUNNING total, not on a declared length —
    Graph does not always send Content-Length on a $value."""
    resp = _Resp(chunks=[b"a" * 400, b"b" * 400, b"c" * 400])
    with mock.patch.object(graph._session, "get", return_value=resp):
        with pytest.raises(graph.GraphTooLarge):
            graph.graph_get_bytes("/x/$value", max_bytes=1000)
    assert resp.closed is True


def test_get_bytes_streams_rather_than_buffering_the_whole_body():
    with mock.patch.object(
        graph._session, "get", return_value=_Resp(body=b"ok")
    ) as get:
        graph.graph_get_bytes("/x/$value", max_bytes=1024)
    assert get.call_args.kwargs["stream"] is True


# ── graph_send ──────────────────────────────────────────────────────────────


def test_send_surfaces_the_created_status_alongside_the_body():
    """graph_post collapses 201 and 204 into None, which loses the one bit a
    draft-creation caller needs."""
    resp = _json_resp({"id": "draft-1", "isDraft": True}, status=201)
    with mock.patch.object(graph._session, "request", return_value=resp):
        body, status = graph.graph_send("POST", "/users/x/messages",
                                        json_body={"subject": "s"})
    assert status == 201
    assert body["id"] == "draft-1"


def test_send_refuses_a_verb_it_does_not_implement():
    """No DELETE, ever, on this path — and the refusal is a programming
    error, not a French message: no model input reaches this argument."""
    with pytest.raises(ValueError):
        graph.graph_send("DELETE", "/users/x/messages/m1")


def test_send_carries_the_status_on_failure():
    resp = _Resp(status=429, headers={"Retry-After": "3"})
    with mock.patch.object(graph._session, "request", return_value=resp):
        with pytest.raises(graph.GraphError) as excinfo:
            graph.graph_send("PATCH", "/users/x/messages/m1", json_body={})
    assert excinfo.value.status == 429
    assert excinfo.value.retry_after_s == 3.0


# ── The original four are untouched ─────────────────────────────────────────


def test_the_original_verbs_still_send_no_extra_header():
    """Their behaviour is what the Outlook mirror's sizing is calibrated on.
    A header appearing here would be a change to three live subsystems."""
    with mock.patch.object(
        graph._session, "get", return_value=_json_resp({"value": []})
    ) as get:
        graph.graph_get("/users/x/events")
    assert set(get.call_args.kwargs["headers"]) == {"Authorization"}
    assert "stream" not in get.call_args.kwargs


def test_an_absolute_url_off_the_graph_host_is_refused():
    """These verbs attach the application's bearer token to every request,
    and the mailbox reader takes a continuation token FROM THE MODEL, which
    is reading email written by anyone who knows the address. Without this,
    a page_token of « https://attacker.example/x » sends the firm's Graph
    credential to that host."""
    for hostile in (
        "https://attacker.example/collect",
        "https://graph.microsoft.com.attacker.example/x",
        "https://evil/graph.microsoft.com/",
    ):
        with mock.patch.object(graph._session, "get") as get:
            with pytest.raises(graph.GraphError):
                graph.graph_get_page(hostile)
            with pytest.raises(graph.GraphError):
                graph.graph_get_bytes(hostile, max_bytes=10)
            assert get.call_count == 0, hostile


def test_a_relative_path_is_unaffected():
    with mock.patch.object(
        graph._session, "get", return_value=_json_resp({"value": []})
    ) as get:
        graph.graph_get_page("/users/x/messages")
    assert get.call_args.args[0] == "https://graph.microsoft.com/v1.0/users/x/messages"


def test_requests_strips_authorization_across_a_redirect():
    """A LIBRARY property this design leans on, pinned because a Dependabot
    bump could change it silently.

    _resolve_url refuses an absolute URL off the Graph host, but it can only
    check the URL we send. If Graph itself answered a redirect to another
    host, requests follows it — and the app's bearer token must not follow.
    requests.Session.rebuild_auth drops the header when the netloc changes;
    this asserts it still does.
    """
    from requests.models import PreparedRequest, Response

    session = graph._session
    prep = PreparedRequest()
    prep.prepare(
        method="GET",
        url="https://graph.microsoft.com/v1.0/users/x/messages",
        headers={"Authorization": "Bearer tok-1"},
    )
    resp = Response()
    resp.request = prep
    resp.url = "https://graph.microsoft.com/v1.0/users/x/messages"

    elsewhere = prep.copy()
    elsewhere.url = "https://attacker.example/collect"
    session.rebuild_auth(elsewhere, resp)
    assert "Authorization" not in elsewhere.headers

    # ...and it survives a redirect that stays on Graph, or paging would break.
    same_host = prep.copy()
    same_host.url = "https://graph.microsoft.com/v1.0/users/x/messages?$skip=25"
    session.rebuild_auth(same_host, resp)
    assert same_host.headers["Authorization"] == "Bearer tok-1"
