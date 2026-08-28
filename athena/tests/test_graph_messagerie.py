"""La boîte de courriels du juriste (lot messagerie, 2026-08-28).

`utils/graph_messagerie.py` is the mailbox sibling of graph_calendrier and
graph_miroir. Almost everything pinned here is a NEGATIVE: the module exists
to make a set of specific silent failures impossible.
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
from utils import graph_messagerie as gm  # noqa: E402
from utils.graph import GraphError, GraphNotConfigured  # noqa: E402


@pytest.fixture(autouse=True)
def _mailbox(monkeypatch):
    monkeypatch.setattr(Config, "GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setattr(Config, "GRAPH_CLIENT_SECRET", "s3cret")
    monkeypatch.setattr(Config, "GRAPH_SENDER_UPN", "reception@example.com")
    monkeypatch.setattr(Config, "CHAT_MAIL_UPN", "juriste@example.com")
    # No test may actually sleep: the suite is a deploy gate, and a retry
    # backoff here would spend real seconds proving arithmetic. The RETRY
    # tests below still assert the call COUNT, which is the behaviour.
    monkeypatch.setattr(Config, "CHAT_MAIL_RETRY_MAX_SLEEP_S", 0.0)
    gm.reset_caches_for_tests()
    yield
    gm.reset_caches_for_tests()


def _page(value=None, **extra):
    return {"value": list(value or []), **extra}


# ── The three module invariants ─────────────────────────────────────────────


def test_the_module_never_calls_the_unbounded_graph_get():
    """graph_get merges EVERY nextLink page with no cap. Safe for a 90-day
    calendar window; on a mailbox it walks thousands of messages carrying
    bodies. A source sweep, because the mistake is one character wide."""
    import inspect

    source = inspect.getsource(gm)
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    assert "graph.graph_get(" not in code
    assert "graph_get_page" in code  # the bounded sibling IS used


def test_attachment_listing_always_excludes_contentbytes():
    """Without a $select Graph inlines every attachment as base64 — a 24 MiB
    lot becomes ~64 MiB of transient peak on a 512 MB instance."""
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page()) as g:
        gm.list_attachments("m1")
    params = g.call_args.args[1]
    assert "$select" in params
    assert "contentBytes" not in params["$select"]
    for field in ("id", "name", "contentType", "size"):
        assert field in params["$select"]


def test_every_interpolated_id_is_percent_encoded():
    """Outlook ids routinely contain / and + and =. An unescaped / addresses
    a DIFFERENT resource rather than failing."""
    hostile = "AAMk/AG+U=zY5QKjAAA="
    with mock.patch.object(gm.graph, "graph_get_page", return_value={}) as g:
        gm.get_message(hostile)
    path = g.call_args.args[0]
    assert "AAMk/AG+U=zY5QKjAAA=" not in path
    assert "%2F" in path and "%2B" in path and "%3D" in path


# ── KQL ─────────────────────────────────────────────────────────────────────


def test_participants_are_ord_and_the_groups_are_anded():
    q = gm.build_kql(participants=("a@x.ca", "b@y.ca"), terms=("Tremblay",))
    assert q == "(participants:a@x.ca OR participants:b@y.ca) AND (Tremblay)"


def test_too_many_addresses_is_refused_never_truncated():
    """A dropped participant is a dropped party, and the caller cannot see the
    loss in the results — so this refuses instead of silently narrowing."""
    many = tuple(f"a{i}@x.ca" for i in range(int(Config.CHAT_MAIL_MAX_ADDRESSES) + 1))
    with pytest.raises(gm.MailRefused) as excinfo:
        gm.build_kql(participants=many)
    assert str(Config.CHAT_MAIL_MAX_ADDRESSES) in str(excinfo.value)


def test_a_quote_in_a_clause_is_refused():
    """A quote would close the clause early and silently change the query's
    shape. On a privileged corpus, a search that quietly means something else
    is worse than one that refuses."""
    with pytest.raises(gm.MailRefused):
        gm.build_kql(terms=('Tremblay" OR subject:',))


def test_an_empty_query_stays_empty_rather_than_becoming_an_empty_group():
    assert gm.build_kql() == ""
    assert gm.build_kql(participants=(), terms=()) == ""


# ── Query shape ─────────────────────────────────────────────────────────────


def test_a_search_never_carries_an_orderby():
    """Graph rejects $orderby beside $search on messages; results are already
    sorted by date."""
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page()) as g:
        gm.search_messages(kql="participants:a@x.ca")
    params = g.call_args.args[1]
    assert params["$search"] == '"participants:a@x.ca"'
    assert "$orderby" not in params
    assert "$filter" not in params


def test_without_a_search_the_filter_property_leads_the_orderby():
    """Else Graph answers InefficientFilter — every property in $orderby must
    also appear in $filter, and first."""
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page()) as g:
        gm.search_messages(received_from="2026-01-01")
    params = g.call_args.args[1]
    assert params["$orderby"] == "receivedDateTime desc"
    assert params["$filter"].startswith("receivedDateTime ge")
    assert "$search" not in params


def test_reads_ask_for_plain_text_bodies():
    """HTML bodies are several times the tokens for the same words."""
    with mock.patch.object(gm.graph, "graph_get_page", return_value={}) as g:
        gm.get_message("m1")
    assert g.call_args.kwargs["extra_headers"] == {
        "Prefer": 'outlook.body-content-type="text"'
    }


def test_a_thread_selects_uniquebody_and_is_sorted_in_python():
    """uniqueBody is the message MINUS quoted history — without it a
    60-message thread is mostly the same chain repeated. Sorting happens here
    rather than in Graph because the InefficientFilter rule forbids the
    orderby, and per-page sorting would interleave two locally-sorted chunks."""
    rows = [
        {"id": "b", "receivedDateTime": "2026-02-02T10:00:00Z"},
        {"id": "a", "receivedDateTime": "2026-01-01T10:00:00Z"},
    ]
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page(rows)) as g:
        out = gm.list_conversation("conv-1")
    params = g.call_args.args[1]
    assert "uniqueBody" in params["$select"]
    assert "$orderby" not in params
    assert [r["id"] for r in out] == ["a", "b"]


def test_a_nextlink_page_is_followed_verbatim():
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page()) as g:
        gm.search_messages(page_url="https://graph/next?$skip=25")
    assert g.call_args.args[0] == "https://graph/next?$skip=25"


# ── Folder posture: labelling fails open, exclusion does not ────────────────


def test_a_failed_deleted_items_lookup_says_so_rather_than_reporting_none():
    """(None, False) means we do not KNOW. A caller must then not claim the
    Corbeille was excluded — otherwise a transient 503 presents deliberately
    discarded mail as live correspondence, and the envelope would report
    deleted_items_excluded: 0, which the model reads as 'nothing was there'."""
    with mock.patch.object(
        gm.graph, "graph_get_page", side_effect=GraphError("boom", status=503)
    ):
        folder_id, ok = gm.deleted_items_id()
    assert folder_id is None
    assert ok is False


def test_folder_path_walks_parents_and_reports_a_partial_walk():
    pages = {
        "f3": {"id": "f3", "displayName": "Tremblay", "parentFolderId": "f2"},
        "f2": {"id": "f2", "displayName": "Dossiers", "parentFolderId": "f1"},
        "f1": {"id": "f1", "displayName": "Boîte de réception", "parentFolderId": ""},
    }

    def _get(path, params=None, **kw):
        return pages[path.rsplit("/", 1)[-1]]

    with mock.patch.object(gm.graph, "graph_get_page", side_effect=_get):
        path, ok = gm.folder_path("f3")
    assert path == "Boîte de réception/Dossiers/Tremblay"
    assert ok is True


def test_folder_path_fails_open_on_the_label_but_admits_it():
    with mock.patch.object(
        gm.graph, "graph_get_page", side_effect=GraphError("boom", status=500)
    ):
        path, ok = gm.folder_path("f3")
    assert path == ""
    assert ok is False


# ── Retry: method-aware, and never on a write 5xx ───────────────────────────


def test_a_read_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(Config, "CHAT_MAIL_RETRY_MAX_SLEEP_S", 0.0)
    calls = []

    def _flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise GraphError("throttled", status=429, retry_after_s=0)
        return _page([{"id": "m1"}])

    with mock.patch.object(gm.graph, "graph_get_page", side_effect=_flaky):
        rows, _ = gm.search_messages(kql="x")
    assert len(calls) == 2
    assert rows == [{"id": "m1"}]


def test_a_write_never_retries_a_5xx(monkeypatch):
    """A 5xx on a POST is ambiguous — Graph may have created the draft and
    failed to answer. Retrying it is exactly the duplicate-draft hazard the
    no-gating decision creates."""
    monkeypatch.setattr(Config, "CHAT_MAIL_RETRY_MAX_SLEEP_S", 0.0)
    calls = []

    def _boom(*a, **kw):
        calls.append(1)
        raise GraphError("server", status=503)

    with mock.patch.object(gm.graph, "graph_send", side_effect=_boom):
        with pytest.raises(GraphError):
            gm.create_anchored_draft("m1", "reply")
    assert len(calls) == 1


def test_a_write_does_retry_a_429(monkeypatch):
    """429 is unambiguous: nothing was created."""
    monkeypatch.setattr(Config, "CHAT_MAIL_RETRY_MAX_SLEEP_S", 0.0)
    calls = []

    def _flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise GraphError("throttled", status=429, retry_after_s=0)
        return ({"id": "draft-1"}, 201)

    with mock.patch.object(gm.graph, "graph_send", side_effect=_flaky):
        out = gm.create_anchored_draft("m1", "reply")
    assert len(calls) == 2
    assert out["id"] == "draft-1"


# ── The turn budget ─────────────────────────────────────────────────────────


def test_an_exhausted_budget_refuses_before_any_graph_call():
    """The tool phase shares its gunicorn request with the Vertex call that
    preceded it, and _run_tools iterates its batch with no length check."""
    token = gm.start_budget(-1)
    try:
        with mock.patch.object(gm.graph, "graph_get_page") as g:
            with pytest.raises(gm.MailBudgetExhausted):
                gm.search_messages(kql="x")
        assert g.call_count == 0
    finally:
        gm.reset_budget(token)


def test_the_per_request_timeout_never_outlives_the_budget():
    token = gm.start_budget(5)
    try:
        assert gm._timeout() <= 5
    finally:
        gm.reset_budget(token)
    # With no budget open the configured timeout stands.
    assert gm._timeout() == int(Config.CHAT_MAIL_HTTP_TIMEOUT_S)


# ── Drafts ──────────────────────────────────────────────────────────────────


def test_the_drafts_listing_orders_by_creation_and_expands_the_marker():
    """Ordering is what makes the temporal argument TRUE rather than assumed:
    without it the page is whatever Graph returns, and the duplicate check
    silently stops finding markers once Drafts outgrows one page."""
    with mock.patch.object(gm.graph, "graph_get_page", return_value=_page()) as g:
        gm.list_marked_drafts()
    params = g.call_args.args[1]
    assert params["$orderby"] == "createdDateTime desc"
    assert params["$expand"] == gm.EXPAND_MARKER
    assert "$search" not in params and "$filter" not in params


def test_the_marker_is_read_client_side():
    """A server-side $filter existence test on an extended property is
    unreliable in Graph — the finding graph_miroir already carries."""
    msg = {
        "id": "d1",
        "singleValueExtendedProperties": [
            {"id": gm.MARKER_PROP_ID, "value": "planifie-abc"}
        ],
    }
    assert gm.marker_of(msg) == "planifie-abc"
    assert gm.marker_of({"id": "d2"}) == ""


def test_an_anchored_draft_asks_for_montreal_time():
    """Without it the quoted thread is stamped UTC and the lawyer reads 14:00
    for a 10:00 Montréal exchange."""
    with mock.patch.object(
        gm.graph, "graph_send", return_value=({"id": "d1"}, 201)
    ) as g:
        gm.create_anchored_draft("m1", "reply_all")
    assert g.call_args.args[0] == "POST"
    assert g.call_args.args[1].endswith("/createReplyAll")
    assert "Eastern Standard Time" in g.call_args.kwargs["extra_headers"]["Prefer"]


def test_an_unknown_draft_mode_is_refused():
    with pytest.raises(gm.MailRefused):
        gm.create_anchored_draft("m1", "send")


def test_a_new_draft_carries_the_marker_and_the_visible_category():
    """The category is the HUMAN half: the lawyer opening Outlook must be able
    to tell which drafts the assistant wrote without opening them."""
    with mock.patch.object(
        gm.graph, "graph_send", return_value=({"id": "d1"}, 201)
    ) as g:
        gm.create_new_draft(
            to=("x@y.ca",), subject="Objet", body_text="Corps",
            marker="planifie-abc",
        )
    payload = g.call_args.kwargs["json_body"]
    assert payload["categories"] == [gm.MARKER_CATEGORY]
    assert payload["singleValueExtendedProperties"][0]["value"] == "planifie-abc"
    assert payload["body"]["contentType"] == "Text"


def test_setting_a_body_stamps_the_marker_on_the_SAME_request():
    """Two requests would leave a window where a crash produces an unmarked
    draft the duplicate check can never find again."""
    with mock.patch.object(
        gm.graph, "graph_send", return_value=({"id": "d1"}, 200)
    ) as g:
        gm.set_draft_body("d1", "Corps", marker="planifie-abc")
    assert g.call_count == 1
    payload = g.call_args.kwargs["json_body"]
    assert payload["body"]["content"] == "Corps"
    assert payload["singleValueExtendedProperties"][0]["value"] == "planifie-abc"


def test_no_send_verb_exists_anywhere_in_the_module():
    """D3: the application never sends. Staging a draft is the whole
    capability, and this is the pin that keeps it so."""
    import inspect

    source = inspect.getsource(gm)
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "*"))
    )
    for forbidden in ("/send", "sendMail", '"DELETE"', "graph_delete"):
        assert forbidden not in code, forbidden


# ── Configuration ───────────────────────────────────────────────────────────


def test_an_unconfigured_mailbox_refuses_rather_than_addressing_users_empty(
    monkeypatch,
):
    monkeypatch.setattr(Config, "CHAT_MAIL_UPN", "")
    with pytest.raises(GraphNotConfigured):
        gm.get_message("m1")
