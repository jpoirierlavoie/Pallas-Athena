"""Unit tests for utils/tracing_setup.py — helpers + sampler config."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opentelemetry.trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from flask import Flask

from utils import tracing_setup
from utils.tracing_setup import (
    add_attributes,
    current_trace_field,
    firestore_span,
    init_app,
    span,
    traced,
)


# ── Shared in-memory exporter so helpers operate on a real provider ──────


@pytest.fixture(scope="module", autouse=True)
def _provider() -> InMemorySpanExporter:
    """Install a TracerProvider with an in-memory exporter for the module.

    OTel's ``set_tracer_provider`` is set-once; we bypass that by directly
    overwriting the global so every test in this module exports to our
    in-memory exporter regardless of test order.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    previous = getattr(otel_trace, "_TRACER_PROVIDER", None)
    set_once = getattr(otel_trace, "_TRACER_PROVIDER_SET_ONCE", None)
    previous_done = getattr(set_once, "_done", None) if set_once else None

    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    if set_once is not None:
        # ``Once`` exposes a private flag indicating completion; setting it
        # lets later ``set_tracer_provider`` calls (e.g. from init_app) be
        # no-ops without warnings.
        if hasattr(set_once, "_done"):
            set_once._done = True

    yield exporter

    # Restore BOTH globals. Setting the provider to None while leaving
    # ``_done`` True used to poison the process for every later module: the
    # proxy provider hands out NonRecordingSpans (trace_id 0) and no
    # subsequent ``set_tracer_provider`` can take effect, so
    # ``current_trace_field`` returns None and the trace↔log correlation test
    # in test_logging_setup fails — but ONLY when that file happens to run
    # after this one. The full suite passed by the accident of alphabetical
    # ordering. Verified against both OTel 1.27.0/0.48b0 and 1.44.0/0.65b0:
    # same failure, so this is a test-isolation bug, not a version issue.
    otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
    if set_once is not None and previous_done is not None:
        set_once._done = previous_done


@pytest.fixture(autouse=True)
def _clear_spans(_provider: InMemorySpanExporter) -> None:
    _provider.clear()
    yield


# ── span ──────────────────────────────────────────────────────────────────


def test_span_emits_with_attributes(_provider: InMemorySpanExporter) -> None:
    with span("test.op", foo="bar", n=3, ignored=None):
        pass
    spans = _provider.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "test.op"
    assert s.attributes["foo"] == "bar"
    assert s.attributes["n"] == 3
    assert "ignored" not in s.attributes


def test_span_records_exception_and_sets_error_status(
    _provider: InMemorySpanExporter,
) -> None:
    raised = False
    try:
        with span("op"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised, "ValueError should have propagated"

    spans = _provider.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.status.status_code == StatusCode.ERROR
    # OTel formats the description as ``ClassName: message`` when set
    # alongside a recorded exception.
    assert "boom" in (s.status.description or "")
    assert any(
        event.name == "exception" for event in s.events
    ), "exception event missing"


# ── traced decorator ─────────────────────────────────────────────────────


def test_traced_preserves_signature_and_return(
    _provider: InMemorySpanExporter,
) -> None:
    @traced("mytest.op", domain="x")
    def add(a: int, b: int, *, scale: int = 1) -> int:
        return (a + b) * scale

    assert add(2, 3, scale=10) == 50

    spans = _provider.get_finished_spans()
    assert spans[-1].name == "mytest.op"
    assert spans[-1].attributes["domain"] == "x"


def test_traced_default_name_uses_qualname(
    _provider: InMemorySpanExporter,
) -> None:
    @traced()
    def my_func() -> int:
        return 1

    my_func()
    spans = _provider.get_finished_spans()
    assert spans[-1].name.endswith("my_func")


def test_traced_records_exception(_provider: InMemorySpanExporter) -> None:
    @traced("failing.op")
    def explode() -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        explode()

    spans = _provider.get_finished_spans()
    assert spans[-1].status.status_code == StatusCode.ERROR


# ── add_attributes ───────────────────────────────────────────────────────


def test_add_attributes_no_op_outside_span(
    _provider: InMemorySpanExporter,
) -> None:
    add_attributes(foo="bar")
    # ``get_finished_spans`` may return either tuple or list depending on
    # the SDK version; check emptiness either way.
    assert len(_provider.get_finished_spans()) == 0


def test_add_attributes_inside_span(
    _provider: InMemorySpanExporter,
) -> None:
    with span("outer"):
        add_attributes(more="info", count=5, ignored=None)
    s = _provider.get_finished_spans()[-1]
    assert s.attributes["more"] == "info"
    assert s.attributes["count"] == 5
    assert "ignored" not in s.attributes


# ── firestore_span ───────────────────────────────────────────────────────


def test_firestore_span_attributes(_provider: InMemorySpanExporter) -> None:
    with firestore_span("get", "dossiers", doc_id="d1", custom="val"):
        pass
    s = _provider.get_finished_spans()[-1]
    assert s.name == "firestore.get"
    assert s.attributes["db.system"] == "firestore"
    assert s.attributes["db.collection"] == "dossiers"
    assert s.attributes["db.document_id"] == "d1"
    assert s.attributes["custom"] == "val"


def test_firestore_span_omits_doc_id_when_none(
    _provider: InMemorySpanExporter,
) -> None:
    with firestore_span("query", "dossiers"):
        pass
    s = _provider.get_finished_spans()[-1]
    assert "db.document_id" not in s.attributes


# ── current_trace_field ──────────────────────────────────────────────────


def test_current_trace_field_with_active_span(
    _provider: InMemorySpanExporter,
) -> None:
    with span("test"):
        out = current_trace_field("athena-pallas")
    assert out is not None
    assert out.startswith("projects/athena-pallas/traces/")
    trace_id = out.rsplit("/", 1)[-1]
    assert len(trace_id) == 32
    assert all(c in "0123456789abcdef" for c in trace_id)


def test_current_trace_field_no_active_span() -> None:
    assert current_trace_field("athena-pallas") is None


def test_current_trace_field_no_project_id(
    _provider: InMemorySpanExporter,
) -> None:
    with span("test"):
        assert current_trace_field("") is None


# ── init_app ─────────────────────────────────────────────────────────────


def test_init_app_idempotent() -> None:
    app = Flask(__name__)
    app.config["ENV"] = "development"
    app.config["FIREBASE_PROJECT_ID"] = "athena-pallas"
    app.config["AUTHORIZED_USER_EMAIL"] = "test@example.com"
    app.config["SECRET_KEY"] = "test"

    init_app(app)
    init_app(app)

    assert getattr(app, "_pallas_tracing_initialized") is True


# ── Sample ratio resolution ──────────────────────────────────────────────


def test_sample_ratio_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACE_SAMPLE_RATIO", raising=False)
    assert tracing_setup._resolve_sample_ratio() == 0.1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0.5", 0.5),
        ("1.0", 1.0),
        ("0.0", 0.0),
        ("2.0", 1.0),  # clamped
        ("-0.5", 0.0),  # clamped
        ("not-a-number", 0.1),  # falls back to default
    ],
)
def test_sample_ratio_env_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv("TRACE_SAMPLE_RATIO", raw)
    assert tracing_setup._resolve_sample_ratio() == expected


def test_excluded_urls_includes_static_assets() -> None:
    excluded = tracing_setup.EXCLUDED_URLS
    assert "/static/.*" in excluded
    assert "/sw.js" in excluded
    assert "/manifest.json" in excluded
    assert "/.well-known/.*" in excluded


# ═══════════════════════════════════════════════════════════════════════════
# CHARACTERIZATION TESTS (2026-07-30)
#
# Written against api/sdk 1.27.0 + instrumentation 0.48b0, GREEN, *before*
# the bump to 1.44.0/0.65b0 — so they are an instrument that measures what
# the bump changes, not a recording of whatever it produced.
#
# They exist because an audit found that the parts of tracing_setup.py most
# likely to break were the parts with ZERO coverage: the PII scrubber, the
# three instrumentation hooks, the propagator, the sampler, and the entire
# production branch. Every failure in that module is swallowed into a
# logger.warning (init) or logger.debug (the scrubber), so a break produces
# a log line and an app that boots with tracing silently off — or, worse, a
# scrubber that no longer scrubs while spans keep flowing to Cloud Trace.
# ═══════════════════════════════════════════════════════════════════════════


class _CapturingExporter:
    """Stands in for the wrapped delegate (Cloud Trace / console)."""

    def __init__(self) -> None:
        self.batches: list = []
        self.shutdown_calls = 0
        self.flush_calls: list = []

    def export(self, spans):
        self.batches.append(list(spans))
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_calls.append(timeout_millis)
        return True


def _finished_span(**attributes):
    """A real, ENDED ReadableSpan carrying *attributes*.

    Built through a real TracerProvider so it is the genuine SDK object the
    exporter sees in production — not a stub that would hide a change in
    ReadableSpan itself.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("characterization")
    with tracer.start_as_current_span("s") as sp:
        for k, v in attributes.items():
            sp.set_attribute(k, v)
    return exporter.get_finished_spans()[0]


# ── Layer 2: the PII scrubber. The highest-value test in this file. ───────


def test_sanitizing_exporter_actually_rewrites_attributes() -> None:
    """The scrubber must be proven to have REWRITTEN the attributes.

    Asserting only that ``export()`` does not raise would pass even if the
    ``span._attributes`` assignment silently stopped taking effect — and
    that failure mode is caught by an ``except Exception`` that logs at
    DEBUG, i.e. invisible in production. This test asserts on what the
    DELEGATE RECEIVED, which is what actually reaches Cloud Trace."""
    span_obj = _finished_span(
        **{
            "http.url": "https://athena.example/parties/?q=Tremblay",
            "note": "écrire à jean.tremblay@example.com",
        }
    )
    delegate = _CapturingExporter()

    tracing_setup._SanitizingSpanExporter(delegate).export([span_obj])

    assert len(delegate.batches) == 1
    exported = delegate.batches[0][0]
    # The query string is gone — this is the client-name leak the layer exists for.
    assert exported.attributes["http.url"] == "https://athena.example/parties/"
    assert "Tremblay" not in str(dict(exported.attributes))
    # And the email was scrubbed by the shared RedactionFilter.
    assert "jean.tremblay@example.com" not in exported.attributes["note"]


def test_sanitizing_exporter_covers_every_url_key() -> None:
    """Each key in _URL_ATTRIBUTE_KEYS loses its query; url.query is BLANKED
    (it is nothing but the query, so stripping cannot help)."""
    attrs = {k: "https://h/p?q=secret" for k in tracing_setup._URL_ATTRIBUTE_KEYS}
    span_obj = _finished_span(**attrs)
    delegate = _CapturingExporter()

    tracing_setup._SanitizingSpanExporter(delegate).export([span_obj])

    exported = delegate.batches[0][0].attributes
    for key in tracing_setup._URL_ATTRIBUTE_KEYS:
        if key == "url.query":
            assert exported[key] == "", f"{key} must be blanked entirely"
        else:
            assert exported[key] == "https://h/p", f"{key} kept its query"
        assert "secret" not in exported[key]


def test_sanitizing_exporter_scrubs_sequence_values() -> None:
    span_obj = _finished_span(**{"tags": ("a@b.com", "plain")})
    delegate = _CapturingExporter()
    tracing_setup._SanitizingSpanExporter(delegate).export([span_obj])
    values = delegate.batches[0][0].attributes["tags"]
    assert "a@b.com" not in values
    assert "plain" in values


def test_sanitizing_exporter_delegates_lifecycle() -> None:
    """Pins the SpanExporter ABC contract — a signature drift in
    force_flush/shutdown would otherwise surface only in production."""
    delegate = _CapturingExporter()
    exporter = tracing_setup._SanitizingSpanExporter(delegate)
    exporter.shutdown()
    assert exporter.force_flush(1234) is True
    assert delegate.shutdown_calls == 1
    assert delegate.flush_calls == [1234]


def test_sanitizing_exporter_never_blocks_export_on_failure(caplog) -> None:
    """A span whose attributes cannot be rewritten must still be exported —
    tracing must never break the app — but the failure MUST be logged.

    ``__slots__`` is not an arbitrary choice of hostile object: it is the
    exact upstream change that would break the ``span._attributes``
    assignment (verified absent from ReadableSpan at v1.44.0, but it is the
    hypothesised future breakage). With slots the assignment raises, which
    is the *detectable* failure mode.

    Note the mode this test canNOT cover: if a future ReadableSpan kept
    ``_attributes`` writable but computed ``attributes`` from something
    else, the assignment would succeed while the scrub silently stopped
    taking effect — no exception, no log. That is why
    test_sanitizing_exporter_actually_rewrites_attributes asserts on what
    the DELEGATE RECEIVED; it is the only guard against that variant."""

    class _Hostile:
        __slots__ = ()  # no _attributes slot → assignment raises
        name = "hostile"

        @property
        def attributes(self):
            return {"k": "v"}

    delegate = _CapturingExporter()
    with caplog.at_level("DEBUG", logger=tracing_setup.logger.name):
        tracing_setup._SanitizingSpanExporter(delegate).export([_Hostile()])
    assert len(delegate.batches) == 1  # exported anyway
    assert caplog.records, "a failed sanitization must be logged, not silent"
    # ERROR, not DEBUG: the root logger sits at INFO in production, so a
    # debug line was invisible exactly where a PII leak would matter.
    assert any(r.levelname == "ERROR" for r in caplog.records), [
        r.levelname for r in caplog.records
    ]


# ── Layer 1: the three instrumentation hooks ─────────────────────────────


def test_flask_request_hook_strips_query_from_url_attributes() -> None:
    """FlaskInstrumentor captures http.target (and sometimes http.url) WITH
    the query string — `/parties/?q=Tremblay` leaks a client-name search."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("hooks")
    with tracer.start_as_current_span("http") as sp:
        sp.set_attribute("http.url", "https://athena.example/parties/?q=Tremblay")
        sp.set_attribute("http.target", "/parties/?q=Tremblay")
        tracing_setup._flask_request_hook(sp, {"PATH_INFO": "/parties/"})
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["http.target"] == "/parties/"
    assert attrs["http.url"] == "https://athena.example/parties/"
    assert "Tremblay" not in str(dict(attrs))


def test_flask_response_hook_reapplies_the_override() -> None:
    """The installed instrumentation re-applies its collected attributes
    after the request hook, so the response hook repeats the override."""
    app = Flask(__name__)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("hooks")
    with app.test_request_context("/dossiers/?q=Secret"):
        with tracer.start_as_current_span("http") as sp:
            sp.set_attribute("http.target", "/dossiers/?q=Secret")
            tracing_setup._flask_response_hook(sp, "200 OK", [])
    assert exporter.get_finished_spans()[0].attributes["http.target"] == "/dossiers/"


def test_requests_hook_rewrites_outbound_urls() -> None:
    """Storage URLs embed uid/dossier/filename in the path AND the name=
    query param, so for storage hosts only scheme+host survive."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("hooks")

    class _Req:
        def __init__(self, url):
            self.url = url

    cases = {
        "https://api.example.com/v1/x?token=abc": "https://api.example.com/v1/x",
        "https://storage.googleapis.com/b/o?name=users%2Fu1%2Fdossiers%2Fd1":
            "https://storage.googleapis.com",
        "https://user:pass@api.example.com/v1": "https://api.example.com/v1",
    }
    for url, expected in cases.items():
        with tracer.start_as_current_span("out") as sp:
            tracing_setup._requests_request_hook(sp, _Req(url))
        got = exporter.get_finished_spans()[-1].attributes["http.url"]
        assert got == expected, f"{url} -> {got}"
        assert "pass@" not in got


# ── Sampler / resource / production gate ─────────────────────────────────


def test_build_sampler_dev_vs_production() -> None:
    from opentelemetry.sdk.trace.sampling import ParentBased

    assert tracing_setup._build_sampler(False, 1.0) is tracing_setup.ALWAYS_ON
    prod = tracing_setup._build_sampler(True, 0.1)
    assert isinstance(prod, ParentBased)


def test_build_resource_carries_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAE_VERSION", "v42")
    attrs = tracing_setup._build_resource().attributes
    assert attrs["service.name"] == "pallas-athena"
    assert attrs["service.version"] == "v42"
    # The STABLE semconv key; the bare `deployment.environment` it replaced
    # is deprecated upstream.
    assert "deployment.environment.name" in attrs
    assert "deployment.environment" not in attrs


def test_is_production_from_config_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    app = Flask(__name__)
    monkeypatch.delenv("ENV", raising=False)
    app.config["ENV"] = "development"
    assert tracing_setup._is_production(app) is False
    app.config["ENV"] = "production"
    assert tracing_setup._is_production(app) is True
    app.config["ENV"] = "development"
    monkeypatch.setenv("ENV", "production")
    assert tracing_setup._is_production(app) is True


def test_production_branch_wires_batch_processor_with_sanitizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ENTIRE production export path had zero coverage — it is never
    executed by any test, in any process, because ENV is never 'production'
    in CI. This drives it with a fake Cloud Trace exporter."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    created: list = []

    class _FakeCloudTraceExporter:
        def __init__(self, *a, **kw):
            created.append(self)

        def export(self, spans):
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis: int = 30000):
            return True

    import types

    fake_mod = types.ModuleType("opentelemetry.exporter.cloud_trace")
    fake_mod.CloudTraceSpanExporter = _FakeCloudTraceExporter
    monkeypatch.setitem(
        sys.modules, "opentelemetry.exporter.cloud_trace", fake_mod
    )

    processors: list = []
    monkeypatch.setattr(
        TracerProvider,
        "add_span_processor",
        lambda self, p: processors.append(p),
    )

    app = Flask(__name__)
    app.config["ENV"] = "production"
    tracing_setup.init_app(app)

    assert created, "the production branch never built the Cloud Trace exporter"
    assert any(isinstance(p, BatchSpanProcessor) for p in processors)


def test_production_exporter_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Tracing is best-effort: a broken exporter must not stop the app from
    booting. It must however SAY so — this pins the warning."""
    import types

    fake_mod = types.ModuleType("opentelemetry.exporter.cloud_trace")

    def _boom(*a, **kw):
        raise RuntimeError("no credentials")

    fake_mod.CloudTraceSpanExporter = _boom
    monkeypatch.setitem(
        sys.modules, "opentelemetry.exporter.cloud_trace", fake_mod
    )

    app = Flask(__name__)
    app.config["ENV"] = "production"
    with caplog.at_level("WARNING", logger=tracing_setup.logger.name):
        tracing_setup.init_app(app)  # must not raise
    assert any("Cloud Trace" in r.message for r in caplog.records)


def test_install_propagator_sets_composite_with_cloud_trace() -> None:
    """X-Cloud-Trace-Context correlation dies quietly if the composite
    propagator is not installed (the whole helper sits in a try/except)."""
    from opentelemetry.propagate import get_global_textmap

    tracing_setup._install_propagator()
    fields = get_global_textmap().fields
    assert "traceparent" in fields
    assert any("cloud-trace" in f.lower() for f in fields), fields


# ── The end-to-end guard: no query string may reach an exported span ──────


def test_no_query_string_reaches_an_exported_span() -> None:
    """THE integration test: a real request, real Flask instrumentation, the
    real hooks and the real sanitizing exporter — asserting on the span that
    would actually be shipped to Cloud Trace.

    One test covers three independent ways a bump could reopen the leak:
      • the HTTP semantic-convention migration renaming http.url/http.target
        to url.full/url.path (the hooks write only the OLD keys, so a flip
        would make them no-ops);
      • the WSGI change of URL source (contrib 0.63b0 / PR #4551 —
        PATH_INFO+QUERY_STRING instead of RAW_URI), which alters the exact
        string the hooks receive;
      • any drift in the request/response hook signatures.
    None of the three would raise; each would silently ship
    `/parties/?q=Tremblay` — a client-name search — to Cloud Trace."""
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    delegate = _CapturingExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(tracing_setup._SanitizingSpanExporter(delegate))
    )

    app = Flask(__name__)

    @app.route("/parties/")
    def parties():
        return "ok"

    # Exactly the wiring init_app performs (see test below, which pins that
    # these are the kwargs it passes).
    FlaskInstrumentor().instrument_app(
        app,
        excluded_urls=tracing_setup.EXCLUDED_URLS,
        tracer_provider=provider,
        request_hook=tracing_setup._flask_request_hook,
        response_hook=tracing_setup._flask_response_hook,
    )
    try:
        assert app.test_client().get("/parties/?q=Tremblay").status_code == 200
    finally:
        FlaskInstrumentor().uninstrument_app(app)

    assert delegate.batches, "instrumentation produced no span at all"
    attrs = dict(delegate.batches[0][0].attributes)
    rendered = repr(attrs)
    assert "Tremblay" not in rendered, f"query string leaked: {rendered}"
    assert "q=" not in rendered, f"query string leaked: {rendered}"


def test_init_app_passes_our_hooks_to_the_instrumentors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the exact kwargs init_app relies on. If a bump renames or drops
    one, instrument_app raises TypeError — which init_app swallows into a
    single WARNING, leaving BOTH services with tracing silently degraded."""
    captured: dict = {}

    class _FakeFlaskInstrumentor:
        def instrument_app(self, app, **kwargs):
            captured.update(kwargs)

    class _FakeOther:
        def instrument(self, **kwargs):
            captured.setdefault("_others", []).append(kwargs)

    import types

    for mod_name, attr, fake in (
        ("opentelemetry.instrumentation.flask", "FlaskInstrumentor", _FakeFlaskInstrumentor),
        ("opentelemetry.instrumentation.jinja2", "Jinja2Instrumentor", _FakeOther),
        ("opentelemetry.instrumentation.requests", "RequestsInstrumentor", _FakeOther),
    ):
        mod = types.ModuleType(mod_name)
        setattr(mod, attr, fake)
        monkeypatch.setitem(sys.modules, mod_name, mod)

    app = Flask(__name__)
    app.config["ENV"] = "development"
    tracing_setup.init_app(app)

    assert captured["excluded_urls"] == tracing_setup.EXCLUDED_URLS
    assert captured["request_hook"] is tracing_setup._flask_request_hook
    assert captured["response_hook"] is tracing_setup._flask_response_hook
    assert captured["tracer_provider"] is not None
    # requests + jinja2 both instrumented in the same block.
    assert len(captured.get("_others", [])) == 2
