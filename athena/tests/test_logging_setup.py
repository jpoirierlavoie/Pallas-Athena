"""Unit tests for utils/logging_setup.py — context, redaction, and helpers."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import utils.logging_setup as logging_setup
from utils.logging_setup import (
    ContextFilter,
    RedactionFilter,
    SENSITIVE_KEYS,
    bind_context,
    clear_context,
    init_app,
    log_auth_event,
    log_dav_operation,
    log_dossier_event,
    log_security_event,
    log_unexpected,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_record(
    msg: object = "test",
    level: int = logging.INFO,
    json_fields: dict | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if json_fields is not None:
        record.json_fields = json_fields
    return record


class _Capture(logging.Handler):
    """Test handler that runs the same filter chain as production."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(ContextFilter())
        self.addFilter(RedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _reset_context() -> None:
    clear_context()
    yield
    clear_context()


@pytest.fixture
def flask_app() -> Flask:
    app = Flask(__name__)
    app.config["FIREBASE_PROJECT_ID"] = "athena-pallas"
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["ENV"] = "development"
    app.config["SECRET_KEY"] = "test-secret"
    return app


# ── ContextFilter ─────────────────────────────────────────────────────────

def test_context_filter_default_empty():
    record = _make_record()
    ContextFilter().filter(record)
    assert record.json_fields == {}


def test_context_filter_picks_up_bound_context():
    bind_context(request_id="abc", route="/x")
    record = _make_record()
    ContextFilter().filter(record)
    assert record.json_fields["request_id"] == "abc"
    assert record.json_fields["route"] == "/x"


def test_context_filter_call_site_overrides_context():
    bind_context(request_id="from-context")
    record = _make_record(json_fields={"request_id": "from-call"})
    ContextFilter().filter(record)
    assert record.json_fields["request_id"] == "from-call"


def test_context_filter_in_flask_request(flask_app):
    init_app(flask_app)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    @flask_app.route("/dossiers/<dossier_id>")
    def dossier_view(dossier_id):
        logging.getLogger("pallas.test").info("inside-request")
        return "ok"

    try:
        client = flask_app.test_client()
        client.get(
            "/dossiers/abc123",
            headers={
                "X-Request-Id": "req-fixed",
                "HX-Request": "true",
                "X-Cloud-Trace-Context": "deadbeef1234/56;o=1",
            },
        )
    finally:
        logging.getLogger().removeHandler(capture)

    in_request = [
        r for r in capture.records if r.getMessage() == "inside-request"
    ]
    assert in_request, "no record captured from inside the request"
    fields = in_request[-1].json_fields
    assert fields["request_id"] == "req-fixed"
    assert fields["route"] == "/dossiers/<dossier_id>"
    assert fields["method"] == "GET"
    assert fields["is_htmx"] is True
    assert fields["trace"] == "projects/athena-pallas/traces/deadbeef1234"


# ── auth_context derivation ───────────────────────────────────────────────

def test_auth_context_dav_path(flask_app):
    init_app(flask_app)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    @flask_app.route("/dav/addressbook/<vcf>", endpoint="dav_addressbook_test")
    def dav_view(vcf):
        logging.getLogger("pallas.test").info("dav-call")
        return "ok"

    try:
        client = flask_app.test_client()
        client.get("/dav/addressbook/abc.vcf")
    finally:
        logging.getLogger().removeHandler(capture)

    fields = next(
        r.json_fields for r in capture.records if r.getMessage() == "dav-call"
    )
    assert fields["auth_context"] == "dav_basic"


def test_auth_context_login_is_anonymous(flask_app):
    init_app(flask_app)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    @flask_app.route("/auth/login")
    def login_view():
        logging.getLogger("pallas.test").info("login-call")
        return "ok"

    try:
        flask_app.test_client().get("/auth/login")
    finally:
        logging.getLogger().removeHandler(capture)

    fields = next(
        r.json_fields for r in capture.records if r.getMessage() == "login-call"
    )
    assert fields["auth_context"] == "anonymous"


def test_auth_context_session_when_user_id_present(flask_app):
    init_app(flask_app)
    capture = _Capture()
    logging.getLogger().addHandler(capture)

    @flask_app.route("/dossiers/")
    def list_view():
        logging.getLogger("pallas.test").info("session-call")
        return "ok"

    try:
        with flask_app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user_id"] = "fake-uid"
            client.get("/dossiers/")
    finally:
        logging.getLogger().removeHandler(capture)

    fields = next(
        r.json_fields for r in capture.records
        if r.getMessage() == "session-call"
    )
    assert fields["auth_context"] == "session"


# ── RedactionFilter ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "key",
    ["password", "Password", "AUTHORIZATION", "Cookie", "api_key", "csrf_token"],
)
def test_redaction_drops_sensitive_keys(key):
    record = _make_record(json_fields={key: "secret-value", "safe": "ok"})
    RedactionFilter().filter(record)
    assert record.json_fields[key] == "<redacted>"
    assert record.json_fields["safe"] == "ok"


def test_redaction_walks_nested_dicts():
    record = _make_record(
        json_fields={"outer": {"password": "x", "ok": "y"}}
    )
    RedactionFilter().filter(record)
    assert record.json_fields["outer"]["password"] == "<redacted>"
    assert record.json_fields["outer"]["ok"] == "y"


def test_redaction_redacts_emails_in_strings():
    record = _make_record(
        json_fields={"note": "ping me at jane.doe@example.com today"}
    )
    RedactionFilter().filter(record)
    assert "jane.doe@example.com" not in record.json_fields["note"]
    assert "<email>" in record.json_fields["note"]


def test_redaction_redacts_phone_numbers():
    cases = [
        "+15145551234",
        "(514) 555-1234",
        "514-555-1234",
        "514.555.1234",
    ]
    for text in cases:
        record = _make_record(json_fields={"v": f"call {text} please"})
        RedactionFilter().filter(record)
        assert text not in record.json_fields["v"], text
        assert "<phone>" in record.json_fields["v"], text


def test_redaction_redacts_postal_codes():
    record = _make_record(json_fields={"v": "address H2T 1S6 etc"})
    RedactionFilter().filter(record)
    assert "H2T 1S6" not in record.json_fields["v"]
    assert "<postal>" in record.json_fields["v"]


def test_redaction_preserves_court_file_numbers_by_default():
    assert logging_setup.REDACT_COURT_FILE_NUMBERS is False
    record = _make_record(
        json_fields={"v": "dossier 500-05-123456-241 to file"}
    )
    RedactionFilter().filter(record)
    assert "500-05-123456-241" in record.json_fields["v"]


def test_redaction_court_file_inside_phone_like_context_is_preserved():
    # The court file segment 500-05-123456-241 must not be partially
    # eaten by the phone regex even though the suffix "-241" looks like
    # the tail of a phone number.
    record = _make_record(
        json_fields={"v": "ref 500-05-123456-241 contact 514-555-1234"}
    )
    RedactionFilter().filter(record)
    assert "500-05-123456-241" in record.json_fields["v"]
    assert "514-555-1234" not in record.json_fields["v"]
    assert "<phone>" in record.json_fields["v"]


def test_redaction_truncates_oversize_strings():
    big = "a" * 3000
    record = _make_record(json_fields={"v": big})
    RedactionFilter().filter(record)
    out = record.json_fields["v"]
    assert out.startswith("<truncated,")
    assert "3000" in out


def test_redaction_handles_dict_msg():
    record = _make_record(msg={"password": "x", "safe": "y"})
    RedactionFilter().filter(record)
    assert record.msg["password"] == "<redacted>"
    assert record.msg["safe"] == "y"


def test_sensitive_keys_set_extendable():
    # Keep the public set's contract: it's a set of lowercase strings.
    assert "password" in SENSITIVE_KEYS
    assert all(k == k.lower() for k in SENSITIVE_KEYS)


# ── Typed helpers ─────────────────────────────────────────────────────────

def test_log_auth_event_failure_warns(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.auth"):
        log_auth_event("login", "failure", reason="token_invalid")
    rec = caplog.records[-1]
    assert rec.name == "pallas.auth"
    assert rec.levelno == logging.WARNING
    assert rec.json_fields["event"] == "login"
    assert rec.json_fields["outcome"] == "failure"
    assert rec.json_fields["reason"] == "token_invalid"


def test_log_auth_event_success_info(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.auth"):
        log_auth_event("login", "success")
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO


def test_log_dossier_event(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.dossier"):
        log_dossier_event("created", dossier_id="d1", file_number="2025-001")
    rec = caplog.records[-1]
    assert rec.name == "pallas.dossier"
    assert rec.levelno == logging.INFO
    assert rec.json_fields["event"] == "created"
    assert rec.json_fields["dossier_id"] == "d1"
    assert rec.json_fields["file_number"] == "2025-001"


def test_log_dav_operation_omits_none_fields(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.dav"):
        log_dav_operation(
            "propfind",
            "addressbook",
            object_count=3,
            duration_ms=12.5,
            status_code=207,
        )
    rec = caplog.records[-1]
    assert rec.name == "pallas.dav"
    assert rec.json_fields["event"] == "dav_operation"
    assert rec.json_fields["operation"] == "propfind"
    assert rec.json_fields["collection_type"] == "addressbook"
    assert rec.json_fields["object_count"] == 3
    assert rec.json_fields["duration_ms"] == 12.5
    assert rec.json_fields["status_code"] == 207
    assert "dossier_id" not in rec.json_fields
    assert "ctag_bumped" not in rec.json_fields


def test_log_security_event_severity_mapping(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.security"):
        log_security_event("csrf_failure", "warning")
        log_security_event("appspot_blocked", "error")
        log_security_event("csp_violation", "critical")
    levels = [r.levelno for r in caplog.records[-3:]]
    assert levels == [logging.WARNING, logging.ERROR, logging.CRITICAL]
    assert all(r.name == "pallas.security" for r in caplog.records[-3:])


def test_log_unexpected_includes_traceback(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.unexpected"):
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log_unexpected("unhandled")
    rec = caplog.records[-1]
    assert rec.name == "pallas.unexpected"
    assert rec.levelno == logging.ERROR
    assert rec.json_fields["event"] == "unexpected"
    # exc_info should be populated by the logging framework when
    # exc_info=True was passed in the helper.
    assert rec.exc_info is not None
    assert rec.exc_info[0] is RuntimeError


def test_log_unexpected_no_exception_context(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.unexpected"):
        log_unexpected("standalone", exc_info=False)
    rec = caplog.records[-1]
    # exc_info may be None or False depending on Python version; both
    # mean "no traceback was captured".
    assert not rec.exc_info


# ── Trace formatting ──────────────────────────────────────────────────────

def test_trace_header_formatting():
    out = logging_setup._format_trace(
        "abc123def456/789;o=1", "athena-pallas"
    )
    assert out == "projects/athena-pallas/traces/abc123def456"


def test_trace_header_minimal():
    out = logging_setup._format_trace("deadbeef", "athena-pallas")
    assert out == "projects/athena-pallas/traces/deadbeef"


def test_trace_header_invalid_returns_none():
    assert logging_setup._format_trace("not a trace!", "p") is None
    assert logging_setup._format_trace("", "p") is None
    assert logging_setup._format_trace("abc", "") is None


# ── bind_context outside a request ────────────────────────────────────────

def test_bind_context_works_outside_request():
    bind_context(request_id="cron-job-1", source="scheduler")
    record = _make_record()
    ContextFilter().filter(record)
    assert record.json_fields["request_id"] == "cron-job-1"
    assert record.json_fields["source"] == "scheduler"


def test_clear_context():
    bind_context(foo="bar")
    clear_context()
    record = _make_record()
    ContextFilter().filter(record)
    assert record.json_fields == {}
