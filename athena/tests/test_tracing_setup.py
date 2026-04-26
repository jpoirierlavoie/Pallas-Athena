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

    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    set_once = getattr(otel_trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if set_once is not None:
        # ``Once`` exposes a private flag indicating completion; clearing it
        # lets later ``set_tracer_provider`` calls (e.g. from init_app) be
        # no-ops without warnings.
        if hasattr(set_once, "_done"):
            set_once._done = True

    yield exporter

    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


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
