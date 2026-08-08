"""Shared write-tool protocol: ``dry_run`` + idempotency (WP15, PA-C04).

Every MCP write tool runs through :func:`run_write`, which layers two
behaviours the audit asked for on top of the tool's own logic:

* **dry_run** — full resolution/validation, computed effect returned,
  NOTHING persisted (no model write, no CTag, no idempotency record). The
  propose-then-approve flow for scheduled jobs.
* **idempotency_key** — the caller-supplied retry armour. The old
  create_note description warned « a retry duplicates » as a workaround
  for its absence; with a key, a replay returns the STORED result of the
  first call instead of writing twice, and a key reused with DIFFERENT
  arguments is refused loudly (a silent mismatch would hand back a result
  that does not match what was asked).

Storage: ``mcp_idempotency/{sha256(tool ":" key)}`` — a documented
exception to Architecture Rule 6, exactly like the OAuth collections: the
doc ID is the lookup key, so a replay is one keyed ``get()`` and no index
exists. ``expire_at`` (24 h) carries a Firestore TTL fieldOverride for
garbage collection ONLY — expiry is enforced in code on every read, per
the OAuth precedent (TTL deletion can lag by days and is never a
correctness control).

Failure posture: the store fails OPEN in both directions. Idempotency is
retry ARMOUR, not an authorization gate — a Firestore blip on the lookup
must not block a legitimate first write, and a blip on the record must
not fail a write that already committed. The uncovered window (key sent,
record failed, client retries) merely degrades to today's behaviour.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from models import db
from mcp.tools import ToolArgumentError

logger = logging.getLogger(__name__)

COLLECTION = "mcp_idempotency"
IDEMPOTENCY_TTL = timedelta(hours=24)

# Protocol arguments stripped from the fingerprint: they parameterize the
# PROTOCOL, not the write — retrying with the same key after a dry run must
# match.
_PROTOCOL_ARGS = ("idempotency_key", "dry_run")


def _doc_id(tool: str, key: str) -> str:
    return hashlib.sha256(f"{tool}:{key}".encode("utf-8")).hexdigest()


def args_fingerprint(args: dict) -> str:
    """Canonical hash of the write's OWN arguments."""
    payload = {
        k: v for k, v in sorted(args.items()) if k not in _PROTOCOL_ARGS
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")
    ).hexdigest()


def _lookup(tool: str, key: str, fingerprint: str) -> tuple[Optional[dict], bool]:
    """(stored result, conflict). Fails OPEN to (None, False)."""
    try:
        snap = db.collection(COLLECTION).document(_doc_id(tool, key)).get()
        if not snap.exists:
            return None, False
        doc = snap.to_dict() or {}
        expire_at = doc.get("expire_at")
        if expire_at is not None and expire_at < datetime.now(timezone.utc):
            return None, False  # expired — TTL GC just hasn't caught up
        if doc.get("args_fingerprint") != fingerprint:
            return None, True
        result = doc.get("result")
        return (dict(result) if isinstance(result, dict) else None), False
    except Exception as exc:
        logger.warning(
            "mcp idempotency lookup failed: %s", type(exc).__name__
        )
        return None, False


def _record(tool: str, key: str, fingerprint: str, result: dict) -> None:
    """Best-effort: a failure here must never fail the committed write."""
    try:
        now = datetime.now(timezone.utc)
        db.collection(COLLECTION).document(_doc_id(tool, key)).set({
            "tool": tool,
            "args_fingerprint": fingerprint,
            "result": result,
            "created_at": now,
            "expire_at": now + IDEMPOTENCY_TTL,
        })
    except Exception as exc:
        logger.warning(
            "mcp idempotency record failed: %s", type(exc).__name__
        )


def run_write(tool: str, args: dict, execute: Callable[[bool], dict]) -> dict:
    """Run a write tool under the shared protocol.

    *execute(dry_run)* performs the tool's own resolution + validation and
    — only when ``dry_run`` is False — the actual write. It must raise
    ``ToolArgumentError`` on any refusal so a refused call never records
    an idempotency entry.
    """
    if bool(args.get("dry_run")):
        payload = execute(True)
        payload["dry_run"] = True
        payload["idempotent_replay"] = False
        return payload

    key = str(args.get("idempotency_key") or "").strip()
    fingerprint = args_fingerprint(args) if key else ""
    if key:
        prior, conflict = _lookup(tool, key, fingerprint)
        if conflict:
            raise ToolArgumentError(
                "Cette idempotency_key a déjà servi à un appel dont les "
                "arguments diffèrent. Une clé identifie UNE écriture "
                "précise — générez une clé nouvelle pour une écriture "
                "nouvelle."
            )
        if prior is not None:
            prior["dry_run"] = False
            prior["idempotent_replay"] = True
            return prior

    payload = execute(False)
    payload["dry_run"] = False
    payload["idempotent_replay"] = False
    if key:
        _record(tool, key, fingerprint, payload)
    return payload
