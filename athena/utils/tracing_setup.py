"""Centralized OpenTelemetry tracing setup for Pallas Athena.

This module is the tracing counterpart to ``logging_setup.py``:

* ``init_app(flask_app)`` configures a ``TracerProvider``, attaches a
  ``BatchSpanProcessor`` exporting to Cloud Trace in production (or a
  ``SimpleSpanProcessor`` exporting to the console in dev), installs the
  composite W3C / X-Cloud-Trace-Context propagator, and applies
  auto-instrumentation for Flask, ``requests`` (so ``firebase-admin``
  outbound calls are captured), and Jinja2 (HTMX-heavy template rendering).
* The ``span``, ``traced``, ``add_attributes``, and ``firestore_span``
  helpers are the public manual-instrumentation surface.  See
  ``OBSERVABILITY.md`` for span name conventions and the canonical DAV
  instrumentation example.

Sampling defaults to 10% in production (the ``TRACE_SAMPLE_RATIO`` env
var overrides — set to ``"1.0"`` for a debugging session, ``"0.0"`` to
disable).  Dev runs at 100% so every local request appears in the
console exporter.

Memory note: F2 = 256MB.  ``BatchSpanProcessor`` uses a bounded queue
(default max 2048).  Tune via ``OTEL_BSP_MAX_QUEUE_SIZE`` if instance
memory pressure shows up post-deploy.
"""

import logging
import os
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterable, Iterator, Optional

from flask import Flask
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)

logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────────

DEFAULT_SAMPLE_RATIO: float = 0.1

# Flask paths excluded from auto-instrumentation — purely static / no-logic
# requests that would create span noise without informational value.
EXCLUDED_URLS: str = ",".join(
    [
        "/static/.*",
        "/sw.js",
        "/manifest.json",
        "/favicon.ico",
        "/.well-known/.*",
        "/robots.txt",
    ]
)


def _is_production(flask_app: Flask) -> bool:
    return (
        flask_app.config.get("ENV") == "production"
        or os.environ.get("ENV") == "production"
    )


def _resolve_sample_ratio() -> float:
    """Read ``TRACE_SAMPLE_RATIO`` env var, clamped to ``[0.0, 1.0]``."""
    raw = os.getenv("TRACE_SAMPLE_RATIO")
    if raw is None or raw == "":
        return DEFAULT_SAMPLE_RATIO
    try:
        ratio = float(raw)
    except ValueError:
        logger.warning(
            "Invalid TRACE_SAMPLE_RATIO=%r; falling back to default %.2f",
            raw,
            DEFAULT_SAMPLE_RATIO,
        )
        return DEFAULT_SAMPLE_RATIO
    if ratio < 0.0:
        return 0.0
    if ratio > 1.0:
        return 1.0
    return ratio


def _build_resource() -> Resource:
    return Resource.create(
        {
            "service.name": "pallas-athena",
            "service.version": os.getenv("GAE_VERSION", "local"),
            "deployment.environment": os.getenv("ENV", "development"),
        }
    )


def _build_sampler(production: bool, ratio: float) -> Sampler:
    if not production and ratio >= 1.0:
        return ALWAYS_ON
    # ParentBased respects an upstream sampling decision when one exists,
    # falling back to the ratio sampler at the trace root.  This keeps
    # multi-service traces coherent.
    return ParentBased(root=TraceIdRatioBased(ratio))


def _install_propagator() -> None:
    """Install a composite propagator so both W3C ``traceparent`` and the
    GCP ``X-Cloud-Trace-Context`` header are honored on inbound requests.

    Without this, requests arriving via the GCP load balancer (which sets
    ``X-Cloud-Trace-Context``) won't have their trace context extracted
    and the trace IDs in Cloud Logging won't line up with traces in
    Cloud Trace.
    """
    try:
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
        from opentelemetry.propagators.cloud_trace_propagator import (
            CloudTraceFormatPropagator,
        )

        set_global_textmap(
            CompositePropagator(
                [
                    TraceContextTextMapPropagator(),
                    CloudTraceFormatPropagator(),
                ]
            )
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Trace propagator setup failed: %s", exc)


def init_app(flask_app: Flask) -> None:
    """Configure OpenTelemetry tracing for the Flask app.  Idempotent.

    * Installs a ``TracerProvider`` with a Resource describing the service.
    * In production: ``BatchSpanProcessor`` → Cloud Trace exporter.
      In dev: ``SimpleSpanProcessor`` → console exporter.
    * Sampling: ``TRACE_SAMPLE_RATIO`` env var, default 0.1 (10%).
    * Auto-instruments Flask, ``requests``, Jinja2.
    """
    if getattr(flask_app, "_pallas_tracing_initialized", False):
        return
    flask_app._pallas_tracing_initialized = True  # type: ignore[attr-defined]

    production = _is_production(flask_app)
    ratio = _resolve_sample_ratio()
    sampler = _build_sampler(production, ratio)

    provider = TracerProvider(resource=_build_resource(), sampler=sampler)

    if production:
        try:
            from opentelemetry.exporter.cloud_trace import (
                CloudTraceSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter())
            )
        except Exception as exc:
            logger.warning(
                "Cloud Trace exporter unavailable (%s); spans will not "
                "be exported.  Tracing is non-fatal — continuing.",
                exc,
            )
    else:
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )

    # ``set_tracer_provider`` is set-once; setting our provider before any
    # auto-instrumentation runs ensures every span is associated with this
    # provider.  Subsequent calls (e.g., from tests) are silently ignored
    # by the OTel API.
    otel_trace.set_tracer_provider(provider)

    _install_propagator()

    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        from opentelemetry.instrumentation.jinja2 import Jinja2Instrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        FlaskInstrumentor().instrument_app(
            flask_app,
            excluded_urls=EXCLUDED_URLS,
            tracer_provider=provider,
        )
        # ``requests`` and Jinja2 instrumentors are global (not per-app).
        # OTel's instrumentors silently no-op on re-instrumentation, so it
        # is safe to call here even across multiple ``create_app`` paths.
        RequestsInstrumentor().instrument(tracer_provider=provider)
        Jinja2Instrumentor().instrument(tracer_provider=provider)
    except Exception as exc:
        logger.warning("OTel auto-instrumentation failed: %s", exc)


# ── Manual instrumentation helpers ──────────────────────────────────────

def _tracer():
    """Lazy tracer accessor — picks up the current global provider."""
    return otel_trace.get_tracer("pallas")


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a manual span as a context manager.

    Attributes whose value is ``None`` are dropped so callers can pass
    optional fields directly without ``if v is not None`` ladders.

    Records the exception and sets ``Status(ERROR)`` if the wrapped block
    raises, then re-raises.
    """
    with _tracer().start_as_current_span(name) as s:
        for k, v in attributes.items():
            if v is None:
                continue
            try:
                s.set_attribute(k, v)
            except Exception:  # pragma: no cover — defensive against odd values
                pass
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(
                otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc))
            )
            raise


def traced(
    name: Optional[str] = None,
    **default_attributes: Any,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a function to run inside a span.

    The span name defaults to ``<module>.<qualname>`` if not given.
    Default attributes apply to every invocation.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(span_name, **default_attributes):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


def add_attributes(**attributes: Any) -> None:
    """Add attributes to the currently-active span, if any.

    Quietly no-ops when no span is recording — safe to call from utility
    code that may run outside a request context.
    """
    current = otel_trace.get_current_span()
    if current is None:
        return
    if hasattr(current, "is_recording") and not current.is_recording():
        return
    for k, v in attributes.items():
        if v is None:
            continue
        try:
            current.set_attribute(k, v)
        except Exception:  # pragma: no cover
            pass


@contextmanager
def firestore_span(
    operation: str,
    collection: str,
    doc_id: Optional[str] = None,
    **extra: Any,
) -> Iterator[Any]:
    """Wrap a Firestore call in a span with standardized attributes.

    Use sparingly — only on hot paths or where the call is the canonical
    next step in a DAV request.  See ``OBSERVABILITY.md`` for the
    standard attribute set.
    """
    attrs: dict[str, Any] = {
        "db.system": "firestore",
        "db.collection": collection,
    }
    if doc_id is not None:
        attrs["db.document_id"] = doc_id
    attrs.update(extra)
    with span(f"firestore.{operation}", **attrs) as s:
        yield s


def current_trace_field(project_id: str) -> Optional[str]:
    """Return ``projects/{project_id}/traces/{trace_id}`` for the active span.

    This is what ``logging_setup`` injects as the ``trace`` field on every
    record, so logs and traces correlate in the Cloud Logging UI.
    Returns ``None`` if no span is active or the project ID is empty.
    """
    if not project_id:
        return None
    span_obj = otel_trace.get_current_span()
    if span_obj is None:
        return None
    ctx = span_obj.get_span_context()
    if not ctx or not ctx.trace_id:
        return None
    return f"projects/{project_id}/traces/{format(ctx.trace_id, '032x')}"


__all__: Iterable[str] = (
    "DEFAULT_SAMPLE_RATIO",
    "EXCLUDED_URLS",
    "add_attributes",
    "current_trace_field",
    "firestore_span",
    "init_app",
    "span",
    "traced",
)
