"""Centralized logging setup for Pallas Athena.

This module is the single entry point for everything observability-related:

* ``init_app(flask_app)`` configures a Cloud Logging handler in production
  (via ``google.cloud.logging.AppEngineHandler`` named ``pallas-athena``)
  or a stderr stream handler locally, attaches the context and redaction
  filters, and registers a ``before_request`` hook that populates the
  per-request context.
* Every record emitted through the standard ``logging`` framework carries
  a stable set of structured fields — ``request_id``, ``trace``,
  ``auth_context``, ``route``, ``method``, ``is_htmx`` — merged into
  ``record.json_fields`` by ``ContextFilter``.
* ``RedactionFilter`` enforces the spec's "Do not log PII" rule
  (CLAUDE.md, Security Rules) at the filter layer: sensitive keys are
  replaced with ``"<redacted>"`` and free-text email / phone / postal-code
  matches are scrubbed.  Quebec court file numbers are preserved by
  default (public information once filed) — flip
  ``REDACT_COURT_FILE_NUMBERS`` to ``True`` to redact them too.
* Typed helpers (``log_auth_event``, ``log_dossier_event``,
  ``log_dav_operation``, ``log_security_event``, ``log_unexpected``)
  emit through dedicated logger names (``pallas.auth``, ``pallas.dossier``,
  ``pallas.dav``, ``pallas.security``, ``pallas.unexpected``) so that
  log-based metrics filter cleanly by ``logName``.

To add a new event type: extend the relevant ``Literal`` (or add a new
helper for a new domain), then document it in ``OBSERVABILITY.md``.
"""

import contextvars
import logging
import os
import re
import sys
import uuid
from typing import Any, Iterable, Literal, Optional

from flask import Flask, has_request_context, request, session


# ── Public configuration ────────────────────────────────────────────────

REDACT_COURT_FILE_NUMBERS: bool = False

SENSITIVE_KEYS: set[str] = {
    "authorization",
    "cookie",
    "set-cookie",
    "session",
    "password",
    "password_hash",
    "secret",
    "api_key",
    "token",
    "id_token",
    "access_token",
    "refresh_token",
    "private_key",
    "dav_password_hash",
    "csrf_token",
    "firebase_token",
}


# ── Request-scoped context (ContextVar) ─────────────────────────────────

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "pallas_log_context", default={}
)


def bind_context(**fields: Any) -> None:
    """Merge fields into the current logging context.

    Use this from background tasks, scripts, or webhook handlers that
    run outside a Flask request context.
    """
    current = dict(_log_context.get())
    current.update(fields)
    _log_context.set(current)


def clear_context() -> None:
    """Reset the logging context to empty."""
    _log_context.set({})


# ── Trace header parsing (Cloud Trace correlation) ──────────────────────

_TRACE_HEADER_RE = re.compile(
    r"^([a-fA-F0-9]+)(?:/(\d+))?(?:;o=\d+)?$"
)


def _format_trace(header_value: str, project_id: str) -> Optional[str]:
    """Convert ``X-Cloud-Trace-Context`` to the canonical trace resource name.

    Returns ``"projects/{project_id}/traces/{trace_id}"`` (the form Cloud
    Logging recognizes for automatic correlation with Cloud Trace), or
    ``None`` if the header is absent or unparseable.
    """
    if not header_value or not project_id:
        return None
    m = _TRACE_HEADER_RE.match(header_value.strip())
    if not m:
        return None
    return f"projects/{project_id}/traces/{m.group(1)}"


# ── Auth context derivation ─────────────────────────────────────────────

_DAV_PREFIX = "/dav/"
_ANON_PREFIXES: tuple[str, ...] = (
    "/auth/",
    "/static/",
    "/.well-known/",
)


def _derive_auth_context(path: str) -> str:
    """Classify the request as ``session`` / ``dav_basic`` / ``anonymous``.

    The classification is based on the request path and (for non-public
    paths) the presence of a Firebase session.  We never include the
    user's email or UID — the spec is single-user and that would just
    duplicate PII across every record.
    """
    if path.startswith(_DAV_PREFIX):
        return "dav_basic"
    for prefix in _ANON_PREFIXES:
        if path.startswith(prefix):
            return "anonymous"
    if has_request_context():
        try:
            if session.get("user_id"):
                return "session"
        except Exception:
            pass
    return "anonymous"


# ── Filters ─────────────────────────────────────────────────────────────

class ContextFilter(logging.Filter):
    """Inject request-scoped context into ``record.json_fields``.

    Reads the current ``ContextVar`` snapshot.  Fields that the call site
    already supplied via ``extra={"json_fields": {...}}`` take precedence
    over context fields — that lets a single call override
    ``request_id`` for chained worker correlation, etc.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        existing = getattr(record, "json_fields", None)
        if not isinstance(existing, dict):
            existing = {}
        ctx = dict(_log_context.get())
        merged: dict[str, Any] = {**ctx, **existing}
        record.json_fields = merged
        return True


class RedactionFilter(logging.Filter):
    """Strip PII and secrets from ``record.json_fields`` and dict messages.

    Runs after :class:`ContextFilter`.  Drops keys in :data:`SENSITIVE_KEYS`
    (case-insensitive) replacing values with ``"<redacted>"`` so the field
    shape is preserved for log-based metrics.  Walks string values and
    redacts emails, E.164 / North American phone numbers, and Canadian
    postal codes.  Truncates strings longer than :data:`MAX_LEN`.
    """

    EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    POSTAL_RE = re.compile(r"\b[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d\b")
    COURT_FILE_RE = re.compile(r"\b\d{3}-\d{2}-\d{6}-\d{3}\b")
    PHONE_RE = re.compile(
        r"\+\d{10,15}"
        r"|\(\d{3}\)\s*\d{3}[-.\s]?\d{4}"
        r"|\b\d{3}[-.]\d{3}[-.]\d{4}\b"
        r"|\b1?\d{10}\b"
    )
    MAX_LEN = 2048

    def filter(self, record: logging.LogRecord) -> bool:
        json_fields = getattr(record, "json_fields", None)
        if isinstance(json_fields, dict):
            record.json_fields = self._redact_value(json_fields)
        if isinstance(record.msg, dict):
            record.msg = self._redact_value(record.msg)
        return True

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._redact_field(k, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(v) for v in value)
        if isinstance(value, str):
            return self._redact_string(value)
        return value

    def _redact_field(self, key: str, value: Any) -> Any:
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            return "<redacted>"
        return self._redact_value(value)

    def _redact_string(self, s: str) -> str:
        if len(s) > self.MAX_LEN:
            return f"<truncated, {len(s)} chars>"
        if REDACT_COURT_FILE_NUMBERS:
            s = self.COURT_FILE_RE.sub("<court-file>", s)
            s = self.EMAIL_RE.sub("<email>", s)
            s = self.POSTAL_RE.sub("<postal>", s)
            s = self.PHONE_RE.sub("<phone>", s)
            return s
        # Preserve court file numbers: stash them in placeholders, redact
        # the rest, then restore.  This keeps phone / postal regexes from
        # over-matching segments inside a court file number.
        placeholders: list[tuple[str, str]] = []

        def _swap(m: re.Match[str]) -> str:
            token = f"\x00CFN{len(placeholders)}\x00"
            placeholders.append((token, m.group(0)))
            return token

        s = self.COURT_FILE_RE.sub(_swap, s)
        s = self.EMAIL_RE.sub("<email>", s)
        s = self.POSTAL_RE.sub("<postal>", s)
        s = self.PHONE_RE.sub("<phone>", s)
        for token, original in placeholders:
            s = s.replace(token, original)
        return s


# ── Init ────────────────────────────────────────────────────────────────

def _is_production(flask_app: Flask) -> bool:
    return (
        flask_app.config.get("ENV") == "production"
        or os.environ.get("ENV") == "production"
    )


def _build_handler(flask_app: Flask) -> logging.Handler:
    if _is_production(flask_app):
        try:
            import google.cloud.logging
            from google.cloud.logging.handlers import AppEngineHandler

            client = google.cloud.logging.Client()
            return AppEngineHandler(client, name="pallas-athena")
        except Exception:
            # Fall through to stderr if Cloud Logging is unreachable —
            # we never want logging configuration to fail the boot.
            pass
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    return handler


def init_app(flask_app: Flask) -> None:
    """Configure logging for the Flask app.  Idempotent.

    * Attaches one handler (Cloud Logging in production, stderr otherwise)
      with :class:`ContextFilter` then :class:`RedactionFilter`.
    * Registers a ``before_request`` hook that populates the request
      context (request id, trace, auth_context, route, method, is_htmx).
    * Registers a ``teardown_request`` hook that clears the context.
    """
    if getattr(flask_app, "_pallas_logging_initialized", False):
        return
    flask_app._pallas_logging_initialized = True  # type: ignore[attr-defined]

    project_id = flask_app.config.get(
        "FIREBASE_PROJECT_ID"
    ) or os.environ.get("FIREBASE_PROJECT_ID", "")

    handler = _build_handler(flask_app)
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(
        logging.INFO if _is_production(flask_app) else logging.DEBUG
    )

    @flask_app.before_request
    def _populate_request_context() -> None:
        path = request.path
        url_rule = request.url_rule
        route = url_rule.rule if url_rule is not None else path
        request_id = (
            request.headers.get("X-Request-Id") or uuid.uuid4().hex
        )
        ctx: dict[str, Any] = {
            "request_id": request_id,
            "auth_context": _derive_auth_context(path),
            "route": route,
            "method": request.method,
            "is_htmx": bool(request.headers.get("HX-Request")),
        }
        trace_header = request.headers.get("X-Cloud-Trace-Context", "")
        trace = _format_trace(trace_header, project_id)
        if trace:
            ctx["trace"] = trace
        _log_context.set(ctx)

    @flask_app.teardown_request
    def _clear_request_context(exc: Optional[BaseException] = None) -> None:
        _log_context.set({})


# ── Typed event helpers ─────────────────────────────────────────────────

_PALLAS_AUTH = logging.getLogger("pallas.auth")
_PALLAS_DOSSIER = logging.getLogger("pallas.dossier")
_PALLAS_DAV = logging.getLogger("pallas.dav")
_PALLAS_SECURITY = logging.getLogger("pallas.security")
_PALLAS_UNEXPECTED = logging.getLogger("pallas.unexpected")


AuthEvent = Literal[
    "login",
    "logout",
    "mfa_challenge",
    "mfa_success",
    "auth_failure",
    "appcheck_failure",
    "rate_limit_hit",
]
DossierEvent = Literal[
    "created",
    "updated",
    "archived",
    "viewed",
    "deleted",
    "court_file_parsed",
]
DavOperation = Literal[
    "propfind",
    "report",
    "get",
    "put",
    "delete",
    "mkcol",
    "sync_collection",
]
DavCollectionType = Literal["addressbook", "calendar", "tasks", "dossier"]
SecurityEvent = Literal[
    "csrf_failure",
    "request_too_large",
    "appspot_blocked",
    "csp_violation",
    "appcheck_failure",
]
SecuritySeverity = Literal["warning", "error", "critical"]


def _emit(
    logger: logging.Logger,
    level: int,
    message: str,
    fields: dict[str, Any],
    *,
    exc_info: bool = False,
) -> None:
    logger.log(
        level,
        message,
        exc_info=exc_info,
        extra={"json_fields": fields},
    )


def log_auth_event(
    event: AuthEvent,
    outcome: Literal["success", "failure"],
    *,
    reason: Optional[str] = None,
    **extra: Any,
) -> None:
    """Emit an authentication / authorization event.

    Failures default to ``WARNING``, successes to ``INFO``.  The ``reason``
    kwarg is the place for a short machine-stable string ("token_invalid",
    "mfa_missing", "rate_limit_exceeded") — never a user email or token.
    """
    fields: dict[str, Any] = {"event": event, "outcome": outcome, **extra}
    if reason is not None:
        fields["reason"] = reason
    level = logging.WARNING if outcome == "failure" else logging.INFO
    _emit(_PALLAS_AUTH, level, event, fields)


def log_dossier_event(
    event: DossierEvent,
    dossier_id: str,
    **extra: Any,
) -> None:
    """Emit a dossier lifecycle event at INFO."""
    fields: dict[str, Any] = {
        "event": event,
        "dossier_id": dossier_id,
        **extra,
    }
    _emit(_PALLAS_DOSSIER, logging.INFO, event, fields)


def log_dav_operation(
    operation: DavOperation,
    collection_type: DavCollectionType,
    *,
    dossier_id: Optional[str] = None,
    object_count: Optional[int] = None,
    duration_ms: Optional[float] = None,
    status_code: Optional[int] = None,
    ctag_bumped: Optional[bool] = None,
    **extra: Any,
) -> None:
    """Emit a DAV operation event at INFO.

    Only non-``None`` optional fields are included so that log-based
    metrics filtering on, e.g., ``ctag_bumped`` don't pick up
    structurally-empty records.
    """
    fields: dict[str, Any] = {
        "event": "dav_operation",
        "operation": operation,
        "collection_type": collection_type,
    }
    if dossier_id is not None:
        fields["dossier_id"] = dossier_id
    if object_count is not None:
        fields["object_count"] = object_count
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    if status_code is not None:
        fields["status_code"] = status_code
    if ctag_bumped is not None:
        fields["ctag_bumped"] = ctag_bumped
    fields.update(extra)
    _emit(_PALLAS_DAV, logging.INFO, operation, fields)


def log_security_event(
    event: SecurityEvent,
    severity: SecuritySeverity,
    **extra: Any,
) -> None:
    """Emit a security event at ``severity`` (warning / error / critical)."""
    level_map = {
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    fields: dict[str, Any] = {"event": event, **extra}
    _emit(_PALLAS_SECURITY, level_map[severity], event, fields)


def log_unexpected(
    message: str,
    *,
    exc_info: bool = True,
    **extra: Any,
) -> None:
    """Emit an unhandled-exception event at ERROR with traceback.

    Defaults to ``exc_info=True`` so it can be called bare from inside
    an ``except`` block.  This is what ``main.py``'s ``errorhandler(Exception)``
    invokes.
    """
    fields: dict[str, Any] = {"event": "unexpected", **extra}
    _emit(
        _PALLAS_UNEXPECTED,
        logging.ERROR,
        message,
        fields,
        exc_info=exc_info,
    )


__all__: Iterable[str] = (
    "REDACT_COURT_FILE_NUMBERS",
    "SENSITIVE_KEYS",
    "ContextFilter",
    "RedactionFilter",
    "bind_context",
    "clear_context",
    "init_app",
    "log_auth_event",
    "log_dav_operation",
    "log_dossier_event",
    "log_security_event",
    "log_unexpected",
)
