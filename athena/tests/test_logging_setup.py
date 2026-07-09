"""Unit tests for utils/logging_setup.py — context, redaction, and helpers."""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

from utils.logging_setup import (
    ContextFilter,
    RedactionFilter,
    REDACT_COURT_FILE_NUMBERS,
    SENSITIVE_KEYS,
    _build_handler,
    bind_context,
    clear_context,
    init_app,
    log_auth_event,
    log_dav_operation,
    log_dossier_event,
    log_security_event,
    log_unexpected,
    sanitize_log_value,
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
    # Tracing must be initialized first so the OTel Flask middleware
    # wraps the WSGI app before logging's before_request hook reads
    # the active span.
    from utils.tracing_setup import init_app as init_tracing

    init_tracing(flask_app)
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
    # Trace is sourced from the active OTel span; format is fixed but
    # the trace ID itself is generated per request.
    assert fields["trace"].startswith("projects/athena-pallas/traces/")
    trace_id = fields["trace"].rsplit("/", 1)[-1]
    assert len(trace_id) == 32 and all(c in "0123456789abcdef" for c in trace_id)


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
    assert REDACT_COURT_FILE_NUMBERS is False
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


def test_redaction_scrubs_percent_args_message():
    # Regression (LOG-ARGS): %s args are interpolated at emit time, after
    # filters — the filter must pre-format the message and scrub it.
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        logging.getLogger("pallas.test").warning(
            "contact %s at %s",
            "jane.doe@example.com",
            "514-555-1234",
        )
    finally:
        logging.getLogger().removeHandler(capture)
    rec = capture.records[-1]
    # Args were consumed by the filter so handlers don't re-interpolate.
    assert rec.args is None
    formatted = logging.Formatter("%(message)s").format(rec)
    assert "jane.doe@example.com" not in formatted
    assert "<email>" in formatted
    assert "514-555-1234" not in formatted
    assert "<phone>" in formatted


def test_redaction_scrubs_plain_string_msg_without_args():
    # Regression (LOG-ARGS): plain string messages (no args) must be
    # scrubbed too, not only dict messages / json_fields.
    record = _make_record(msg="email jane.doe@example.com at H2T 1S6")
    RedactionFilter().filter(record)
    assert "jane.doe@example.com" not in record.msg
    assert "<email>" in record.msg
    assert "H2T 1S6" not in record.msg
    assert "<postal>" in record.msg
    formatted = logging.Formatter("%(message)s").format(record)
    assert "jane.doe@example.com" not in formatted


def test_redaction_scrubs_traceback_text():
    # Regression (LOG-TRACEBACK): tracebacks render at format time from
    # exc_info (after filters), so the filter must pre-render and scrub
    # them into exc_text and clear exc_info.
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        try:
            raise RuntimeError("lookup failed for jane.doe@example.com")
        except RuntimeError:
            logging.getLogger("pallas.test").error("boom", exc_info=True)
    finally:
        logging.getLogger().removeHandler(capture)
    rec = capture.records[-1]
    assert rec.exc_info is None
    assert rec.exc_text is not None
    assert "RuntimeError" in rec.exc_text
    assert "jane.doe@example.com" not in rec.exc_text
    assert "<email>" in rec.exc_text
    # Formatter.format appends exc_text whenever it is set — both the
    # stderr and Cloud Logging handler paths go through this.
    formatted = logging.Formatter("%(message)s").format(rec)
    assert "jane.doe@example.com" not in formatted
    assert "RuntimeError" in formatted


# ── Control-character neutralization (log injection, CWE-117) ─────────────

def test_newlines_neutralized_in_plain_message():
    record = _make_record(msg="line1\n2026-01-01 [ERROR] forged\r\nline2")
    RedactionFilter().filter(record)
    assert "\n" not in record.msg
    assert "\r" not in record.msg
    assert "\\n" in record.msg
    assert "forged" in record.msg  # escaped, not silently dropped


def test_newlines_neutralized_in_percent_args():
    # The log-injection scenario: a user-controlled %s arg containing CRLF
    # must not produce a multi-line formatted message.
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        logging.getLogger("pallas.test").warning(
            "PUT failed for %s", "abc\r\n2026-01-01 [INFO] forged entry"
        )
    finally:
        logging.getLogger().removeHandler(capture)
    rec = capture.records[-1]
    formatted = logging.Formatter("%(message)s").format(rec)
    assert "\n" not in formatted
    assert "\r" not in formatted
    assert "forged entry" in formatted


def test_control_chars_escaped_to_visible_sequences():
    record = _make_record(msg="a\tb\x1b[31mc\x00d")
    RedactionFilter().filter(record)
    assert "\t" not in record.msg
    assert "\x1b" not in record.msg
    assert "\x00" not in record.msg
    assert record.msg == "a\\tb\\x1b[31mc\\x00d"


def test_traceback_multiline_structure_preserved():
    # Neutralization is applied per traceback line — the real newlines
    # between frames must survive so the stack stays readable.
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("pallas.test").error("fail", exc_info=True)
    finally:
        logging.getLogger().removeHandler(capture)
    rec = capture.records[-1]
    assert rec.exc_text.count("\n") >= 2
    assert "RuntimeError" in rec.exc_text


def test_sanitize_log_value_strips_newlines():
    assert sanitize_log_value("abc\r\nDEF\nx\ry") == "abc DEF x y"
    assert "\n" not in sanitize_log_value(["err\n1", "err2"])
    assert sanitize_log_value("clean-uuid") == "clean-uuid"
    assert sanitize_log_value(None) == "None"


def test_pii_redaction_survives_control_separator():
    # Regression: neutralization must run AFTER the PII pass — a phone or
    # postal code split across control whitespace must still redact, since
    # PHONE_RE/POSTAL_RE rely on \s matching the raw separator char.
    record = _make_record(msg="appel (514) 555\n1234 svp")
    RedactionFilter().filter(record)
    assert "555" not in record.msg
    assert "<phone>" in record.msg
    assert "\n" not in record.msg

    record = _make_record(msg="adresse H2T\n1S6 fin")
    RedactionFilter().filter(record)
    assert "H2T" not in record.msg
    assert "<postal>" in record.msg


def test_traceback_exotic_separators_neutralized():
    # Regression: splitlines() also splits on \v \f \x1c-\x1e \x85 U+2028,
    # which the join would re-emit as REAL newlines — a crafted exception
    # message could forge a log line. exc_text must escape them instead.
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        try:
            raise RuntimeError("boom\x0c2026-01-01 [ERROR] forged\u2028more")
        except RuntimeError:
            logging.getLogger("pallas.test").error("fail", exc_info=True)
    finally:
        logging.getLogger().removeHandler(capture)
    rec = capture.records[-1]
    assert "\x0c" not in rec.exc_text
    assert "\u2028" not in rec.exc_text
    assert "\\x0c" in rec.exc_text
    assert "\\u2028" in rec.exc_text
    # The forged text must share its line with the exception, not stand alone.
    last_line = rec.exc_text.split("\n")[-1]
    assert "RuntimeError" in last_line
    assert "forged" in last_line


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


def test_log_unexpected_includes_traceback():
    capture = _Capture()
    logging.getLogger().addHandler(capture)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log_unexpected("unhandled")
    finally:
        logging.getLogger().removeHandler(capture)
    rec = next(
        r for r in capture.records if r.name == "pallas.unexpected"
    )
    assert rec.levelno == logging.ERROR
    assert rec.json_fields["event"] == "unexpected"
    # The RedactionFilter consumes exc_info (so formatters can't render
    # the raw traceback) and leaves the redacted text in exc_text.
    assert rec.exc_info is None
    assert rec.exc_text is not None
    assert "RuntimeError: boom" in rec.exc_text


def test_log_unexpected_no_exception_context(caplog):
    with caplog.at_level(logging.DEBUG, logger="pallas.unexpected"):
        log_unexpected("standalone", exc_info=False)
    rec = caplog.records[-1]
    # exc_info may be None or False depending on Python version; both
    # mean "no traceback was captured".
    assert not rec.exc_info


# ── Trace correlation (delegates to utils.tracing_setup) ──────────────────

def test_trace_field_uses_active_otel_span():
    from utils.tracing_setup import current_trace_field, span as otel_span

    with otel_span("test"):
        out = current_trace_field("athena-pallas")
    assert out is not None
    assert out.startswith("projects/athena-pallas/traces/")
    trace_id = out.rsplit("/", 1)[-1]
    assert len(trace_id) == 32


def test_trace_field_returns_none_without_span():
    from utils.tracing_setup import current_trace_field

    assert current_trace_field("athena-pallas") is None


def test_trace_field_returns_none_without_project():
    from utils.tracing_setup import current_trace_field, span as otel_span

    with otel_span("test"):
        assert current_trace_field("") is None


# ── Production handler selection (structured jsonPayload) ─────────────────

def test_production_uses_structured_cloud_logging_handler(flask_app, monkeypatch):
    # Regression (OBS): AppEngineHandler.emit str-formats the record and
    # drops record.json_fields, so every structured event shipped as
    # textPayload with a null jsonPayload — log-based metrics on
    # jsonPayload.event matched nothing. Production must build a
    # CloudLoggingHandler (named pallas-athena), which routes json_fields
    # into the LogEntry jsonPayload.
    import google.cloud.logging as gcl
    import google.cloud.logging.handlers as gclh

    captured: dict = {}

    class _FakeCLH(logging.Handler):
        def __init__(self, client, *, name=None, **kw):
            super().__init__()
            captured["client"] = client
            captured["name"] = name

        def emit(self, record):  # pragma: no cover — never emitted here
            pass

    monkeypatch.setattr(gcl, "Client", lambda: "fake-client")
    monkeypatch.setattr(gclh, "CloudLoggingHandler", _FakeCLH)

    flask_app.config["ENV"] = "production"
    handler = _build_handler(flask_app)

    assert isinstance(handler, _FakeCLH)
    assert captured["name"] == "pallas-athena"


def test_cloud_logging_message_parser_emits_json_fields_as_dict():
    # Proves the mechanism the fix relies on: the library's message parser
    # turns a record carrying json_fields into a dict payload (→ jsonPayload)
    # with the human message preserved under "message". Guards against a
    # library change that would silently regress structured logging.
    from google.cloud.logging_v2.handlers.handlers import (
        _format_and_parse_message,
    )

    record = _make_record(
        msg="mcp_tool_call",
        json_fields={"event": "mcp_tool_call", "tool": "get_agenda",
                     "outcome": "success"},
    )

    class _Fmt:
        def format(self, r):
            return r.getMessage()

    payload = _format_and_parse_message(record, _Fmt())
    assert isinstance(payload, dict)
    assert payload["event"] == "mcp_tool_call"
    assert payload["tool"] == "get_agenda"
    assert payload["message"] == "mcp_tool_call"


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
