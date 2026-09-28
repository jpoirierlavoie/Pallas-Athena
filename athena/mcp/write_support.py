"""Shared write-tool protocol: the idempotency claim and the commit point
(WP15, PA-C04; plan Lot 0a, rule 7).

Every MCP write tool runs through :func:`run_write`, which layers two
behaviours on top of the tool's own logic.

1. The idempotency CLAIM
------------------------
``idempotency_key`` is the caller-supplied retry armour: a replay returns the
STORED result of the first call instead of writing twice, and a key reused
with DIFFERENT arguments is refused loudly (a silent mismatch would hand back
a result that does not match what was asked).

Until 2026-09-25 the sequence was lookup → execute → record, three separate
steps: two concurrent calls with the same key both found nothing, both
wrote, and the second record overwrote the first. The key is now CLAIMED
ATOMICALLY BEFORE anything executes — ``DocumentReference.create()``, which
the server refuses when the document exists — as a ``pending`` entry:

    {tool, args_fingerprint, status: "pending", claim_id,
     claimed_at, created_at, expire_at}

then FINALIZED once, by a partial ``update()`` that never touches the
fingerprint, the tool, the claim id or the expiry: ``committed`` with the
``result``, or ``partial`` with the ids of what committed (see 2). A second
call finds the claim and decides:

* different fingerprint → refused, ``idempotency_conflict``;
* ``committed``, or NO status at all — a legacy entry written before the
  claim existed, which carries its ``result`` — → replay, unchanged;
* ``partial`` → the same :class:`CommittedWriteError` as the first call;
* ``pending`` and YOUNGER than :data:`IN_FLIGHT_WINDOW` → refused,
  ``idempotency_in_flight``: « wait, then retry with the SAME key ». This is
  the ordinary retry-while-the-first-call-runs case, and the one the design
  review caught: telling the caller to re-read and pick a NEW key here would
  reopen exactly the race the claim closes — the re-read shows nothing, the
  new key writes, the first call then commits too. The refusal is worded
  for the other states a young pending claim can stand for — a call that
  ENDED without its result being stored (a failed finalize, a ``required``
  claim kept after a failure) — so it never promises a result
  unconditionally. The same refusal answers a claim that kept changing for
  every attempt (``ClaimContention``): that is live same-key concurrency,
  and it never fails open, under either policy;
* ``pending`` and OLDER → refused, ``idempotency_interrupted``: the first
  call can no longer be running, so re-reading has become reliable — « the
  write may have happened; re-read, and use a new key only if nothing was
  written ». A pending claim is never cleared automatically: expiring it
  after a lease would reopen the duplicate window. It lapses with the 24 h
  ``expire_at`` like every entry;
* expired (past ``expire_at``) → deleted under a ``last_update_time``
  precondition, then claimed afresh — a concurrent caller that got there
  first makes the precondition fail, and the claim is re-read.

The window. :data:`IN_FLIGHT_WINDOW` is the longest the first call can still
be running, i.e. the platform's request deadline — the LARGER of the two
bounds this deployment has. gunicorn's ``--timeout 60`` (``app.yaml``) is
the worker heartbeat; with the ``gthread`` worker (``--threads 4``) the
heartbeat runs on the worker's main loop, not in the request thread, so it
does not by itself end a request. App Engine standard with automatic scaling
does: its request deadline is 10 minutes. Ten minutes it is, plus a margin
for clock skew between the instance that claimed and the one that reads
(``claimed_at`` is each instance's own clock). The residue, stated rather
than hidden: a thread the platform abandons but does not stop could still
commit after the window — nothing in this process can observe that, and no
re-read could either.

2. The commit point
-------------------
A write can COMMIT and then fail: the CTag bump, the re-read of a cascaded
protocol step, the payload builder. That exception used to reach the
endpoint's blanket ``except`` and come back as a retryable « internal
error » — with no record — so the retry wrote a second time. The commit
point is now STRUCTURAL, not a handler convention: every model mutator the
connector reaches calls ``models.provenance.note_commit(collection, id)``
right after its Firestore write returns (pinned by
``tests/test_provenance.py``), and ``execute`` runs inside
``provenance.writing_via("mcp", tool=…)``, whose record this function reads.
So on an exception:

* a commit was noted → ``log_unexpected`` (the traceback — this is a bug to
  go and look at), the claim is finalized ``partial`` with the ids, and
  :class:`CommittedWriteError` is raised: « ENREGISTRÉE — NE PAS
  RÉESSAYER ». A same-key retry re-raises it without executing. That holds
  for a ``ToolArgumentError`` too: a refusal AFTER a commit is a handler
  bug, and reporting it as « nothing was written » would be false;
* nothing was committed → a ``ToolArgumentError`` (a refusal) releases the
  claim, so a refused call still records nothing and a corrected same-key
  retry can run. Any other exception releases it under the ``optional``
  policy (a retry is safe) and KEEPS it ``pending`` under ``required``
  (fail-closed: the same-key retry is refused and forces a re-read). Note
  the one blind spot a structural record cannot see: the models turn a
  Firestore write EXCEPTION into a French error list, which the handler
  raises as a refusal — and an error on a write does not prove it did not
  land (a timeout can follow a commit). The claim is released there as for
  any refusal — UNLESS the handler raised the refusal with
  ``keep_claim=True``, its statement that the outcome is uncertain (fixups
  of lot 3: ``create_invoice`` recognizes the model's
  ``CREATE_OUTCOME_UNCERTAIN``, a raise out of the invoice transaction,
  whose success would have consumed a permanent number). The claim then
  stays ``pending``, under either policy, and the same-key retry is refused
  exactly as after a ``required`` failure. The Lot 5 money models are
  transactional and read-verify for that reason.

A release is a ``delete()`` under the ``last_update_time`` the claim's own
``create()`` returned — Firestore's delete preconditions are ``exists`` and
``last_update_time`` only, so « delete only MY claim » is expressed that way.

3. Failure posture
------------------
The policy is read from the registry — ``mcp.tools.idempotency_policy``,
the tool's ``"idempotency"`` spec key — never passed by the handler.

* ``optional`` (every write tool but the three ``required`` ones below):
  the store fails OPEN. A Firestore blip on
  the claim must not block a legitimate first write; the call runs
  unclaimed, and afterwards a best-effort ``create()`` stores its result so
  a later retry can still replay (``op="record"``). The uncovered window —
  store down, retry — merely degrades to the pre-claim behaviour.
* ``required`` (``create_hearing_series`` and ``decide_rendez_vous`` since
  lot 1b — a series, and the one outbound effect —, and ``create_invoice``
  since lot 3b — a permanent number): the key is demanded
  (``idempotency_required``) and the store fails CLOSED
  (``idempotency_store_unavailable``): nothing executes when the claim
  cannot be established.

Failing open is only tenable if the failure is SEEN: each one is logged as
``mcp_idempotency_store_failure`` (tool, op, exception class — never the
exception's text, which can carry what was being stored), through the typed
helper. ``op`` ∈ ``lookup`` | ``claim`` | ``finalize`` | ``release`` |
``record`` | ``record_partial``.

Storage: ``mcp_idempotency/{sha256(tool ":" key)}`` — a documented exception
to Architecture Rule 6, exactly like the OAuth collections: the doc ID is
the lookup key, so a claim is one keyed ``get()``/``create()`` and no index
exists. ``expire_at`` (24 h from the claim) carries a Firestore TTL
fieldOverride for garbage collection ONLY — expiry is enforced in code on
every read, per the OAuth precedent.

Rollback and mixed versions: code from before the claim reads a ``pending``
or ``partial`` entry as « no stored result » and executes — a duplicate.
Run no MCP write during a deploy window (DEPLOYMENT.md §11).

4. What is stored — the persistence hooks (plan rule 7, lot 2A T5)
------------------------------------------------------------------
A committed result is stored VERBATIM for 24 h and replayed. One kind of
value must never land there: a CAPABILITY — a URL or a storage path whose
mere possession grants access. The lot 2 upload ticket returns exactly one
(``begin_upload``'s resumable-upload session URI, the one documented
exception to « no signed URL in tool output »), so a tool may declare two
hooks, registered by tool name like the policy is read by tool name —
:func:`register_persistence_hooks`, never passed by the handler:

* ``persist(payload) -> stored`` runs on a COPY of the committed result and
  returns what may be stored (the ticket's id, never its URL). The caller
  still receives the full payload;
* ``rehydrate(stored) -> payload`` runs on a REPLAY, before
  ``idempotent_replay`` is set, and rebuilds what the caller needs from what
  was stored (a fresh session for the SAME still-open ticket). It may raise
  ``ToolArgumentError`` (« ticket fermé ») — a refusal of the replay, with
  nothing written; it must not write records itself.

Both, or neither: a ``persist`` that strips what nothing puts back would
replay a crippled result. Independently of any hook, what is about to be
stored is SCANNED (:func:`capability_in`): a key named like a capability
(``upload_url``, ``signed_url``, ``download_url``, ``storage_path``,
``url``, ``*_url``, in any case) or a string carrying a signed-URL or
session marker (``X-Goog-Signature``, ``X-Goog-Credential``, a V2
``GoogleAccessId=``, ``upload_id=``) is never
persisted — the result is then NOT stored at all, logged as a store failure
(``error_type: CapabilityInResult``), and the claim stays ``pending``, so a
same-key retry is refused as in flight rather than duplicated. A tool that
forgets its hooks therefore degrades to « no replay », never to a
capability kept 24 h in Firestore. A ``persist`` that raises is the same
failure (its exception's class is what is logged).
"""

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, NamedTuple, Optional

from google.api_core import exceptions as gexc

from models import db, provenance
from mcp.tools import (
    IDEMPOTENCY_REQUIRED,
    TOOLS,
    WRITE_TOOLS,
    CommittedWriteError,
    ToolArgumentError,
    idempotency_policy,
    loggable_id,
)
from utils.logging_setup import log_mcp_event, log_unexpected

__all__ = [
    "COLLECTION",
    "CommittedWriteError",
    "IDEMPOTENCY_TTL",
    "IN_FLIGHT_WINDOW",
    "PLATFORM_REQUEST_DEADLINE",
    "PersistenceHooks",
    "args_fingerprint",
    "capability_in",
    "is_capability_key",
    "persistence_hooks",
    "persistence_tools",
    "register_persistence_hooks",
    "run_write",
]

COLLECTION = "mcp_idempotency"
IDEMPOTENCY_TTL = timedelta(hours=24)

# App Engine standard, automatic scaling: a request is cut at 10 minutes.
# gunicorn's --timeout 60 is shorter but does not bound a gthread request
# (see the module docstring), so the platform deadline is the larger bound.
PLATFORM_REQUEST_DEADLINE = timedelta(minutes=10)
# claimed_at is the CLAIMING instance's clock, compared against the reading
# instance's: a margin for the skew between two instances' clocks.
_CLOCK_SKEW_MARGIN = timedelta(seconds=30)
IN_FLIGHT_WINDOW = PLATFORM_REQUEST_DEADLINE + _CLOCK_SKEW_MARGIN

STATUS_PENDING = "pending"
STATUS_COMMITTED = "committed"
STATUS_PARTIAL = "partial"

# A claim that loses a race is re-read and decided again; three rounds is
# far more than a single-user connector can contend for one key.
_CLAIM_ATTEMPTS = 3
# The ids a partial record keeps — a bulk tool commits at most 50 rows.
_MAX_RECORDED_COMMITS = 100

# Protocol arguments stripped from the fingerprint: they parameterize the
# PROTOCOL, not the write — retrying with the same key after a dry run must
# match.
# ``dry_run`` stays in the EXCLUSION tuple though the exclusion is now
# INERT: no input schema accepts the argument since 2026-08-27, and every
# write schema carries ``additionalProperties: False``, so ``args`` can
# never contain it — the fingerprint is identical with or without the
# token. It is kept because a fingerprint is a 24 h contract with entries
# already stored, and because removing an inert exclusion buys nothing
# while a future argument named the same would silently change every
# fingerprint at once.
_PROTOCOL_ARGS = ("idempotency_key", "dry_run")


class MalformedEntry(Exception):
    """A stored entry this protocol cannot interpret (a non-dict snapshot,
    an unknown status, a committed entry without a result). Only its class
    name is ever logged."""


class ClaimContention(Exception):
    """The claim kept changing under this call for every attempt.

    Not a store failure: a round is lost only when something else touched
    this very key between this call's read and its write (a create refused
    because the entry appeared, a guarded delete refused because it changed
    or vanished). The TTL collector can account for one such round at most
    — it deletes an expired entry once — so running out of rounds means
    other CALLS are live on the key. Demonstrated concurrency on one key is
    the one situation the claim exists to arbitrate, so it never fails open
    — see :func:`_claim`."""


@dataclass(frozen=True)
class _Claim:
    """The pending entry THIS call created."""

    ref: Any
    claim_id: str
    # The WriteResult time of the claim's own create(): the precondition
    # that makes finalize/release touch THIS claim and nothing else.
    update_time: Any


class _Replay(NamedTuple):
    result: dict


class CapabilityInResult(Exception):
    """What was about to be stored carries a capability (a URL or a storage
    path). Only its class name is ever logged — never the value."""


# ── 4. Persistence hooks ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PersistenceHooks:
    """A tool's ``persist``/``rehydrate`` pair (module docstring, § 4)."""

    persist: Callable[[dict], dict]
    rehydrate: Callable[[dict], dict]


_PERSISTENCE_HOOKS: dict[str, PersistenceHooks] = {}

# The capability NAMES — the rule tests/test_mcp_framework_guards applies to
# every declared output schema, applied here to every stored value.
_CAPABILITY_KEYS = frozenset({"signed_url", "upload_url", "download_url",
                              "storage_path"})
# What a GCS V4 signed URL (the signature and credential query parameters),
# a V2 one (its GoogleAccessId — ``Blob.generate_signed_url`` still signs V2
# when no ``version`` is passed) and a resumable-upload session URI (its
# upload_id) carry, lower-cased.
_CAPABILITY_MARKERS = ("x-goog-signature", "x-goog-credential",
                       "googleaccessid=", "upload_id=")


def register_persistence_hooks(
    tool: str,
    *,
    persist: Callable[[dict], dict],
    rehydrate: Callable[[dict], dict],
) -> None:
    """Declare *tool*'s persistence hooks — both, never one.

    *tool* must be a registered WRITE tool (``KeyError`` for an unknown
    name, ``ValueError`` for a read tool, which never reaches
    :func:`run_write`). Registering the SAME pair again is a no-op (a module
    reload); a different pair for a tool that already has one raises — a
    silent replacement would change what a stored result means.
    """
    if tool not in TOOLS:
        raise KeyError(tool)
    if tool not in WRITE_TOOLS:
        raise ValueError(f"{tool} is not a write tool")
    if not callable(persist) or not callable(rehydrate):
        raise TypeError("persist and rehydrate must both be callables")
    hooks = PersistenceHooks(persist=persist, rehydrate=rehydrate)
    existing = _PERSISTENCE_HOOKS.get(tool)
    if existing is not None and existing != hooks:
        raise ValueError(f"{tool} already has persistence hooks")
    _PERSISTENCE_HOOKS[tool] = hooks


def persistence_hooks(tool: str) -> Optional[PersistenceHooks]:
    """The hooks *tool* declared, or ``None`` (store and replay verbatim)."""
    return _PERSISTENCE_HOOKS.get(tool)


def persistence_tools() -> frozenset[str]:
    """Every tool that declared persistence hooks."""
    return frozenset(_PERSISTENCE_HOOKS)


def is_capability_key(key: object) -> bool:
    """A key named like a capability: the four the connector's guards list,
    ``url``, or anything ending in ``_url`` — in any case (``Upload_URL`` is
    the same capability)."""
    if not isinstance(key, str):
        return False
    key = key.lower()
    return key in _CAPABILITY_KEYS or key == "url" or key.endswith("_url")


def capability_in(value: object) -> bool:
    """True when *value* — walked through dicts, lists and tuples — holds a
    capability-named key or a string carrying a signed-URL/session marker."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, sub in node.items():
                if is_capability_key(key):
                    return True
                stack.append(sub)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif isinstance(node, str):
            lowered = node.lower()
            if any(marker in lowered for marker in _CAPABILITY_MARKERS):
                return True
    return False


def _storable(tool: str, payload: dict, claimed: bool) -> Optional[dict]:
    """What may be stored of *payload*, or ``None`` when nothing may.

    ``persist`` (when declared) runs on a deep COPY, so it can never alter
    what the caller receives. A persist that raises or returns a non-dict,
    and a result that still carries a capability, are store failures under
    the op the store would have run (``finalize`` on a claim, ``record``
    without one): nothing is stored, and the claim stays pending.
    """
    op = "finalize" if claimed else "record"
    hooks = _PERSISTENCE_HOOKS.get(tool)
    try:
        stored = hooks.persist(copy.deepcopy(payload)) if hooks else payload
        if not isinstance(stored, dict):
            raise TypeError("persist must return a dict")
    except Exception as exc:
        _store_failure(tool, op, exc)
        return None
    if capability_in(stored):
        _store_failure(tool, op, CapabilityInResult())
        return None
    return stored


def _rehydrated(tool: str, stored: dict) -> dict:
    """The replayed payload: *stored*, through ``rehydrate`` when declared.
    A ``ToolArgumentError`` from the hook is the replay's refusal."""
    hooks = _PERSISTENCE_HOOKS.get(tool)
    if hooks is None:
        return stored
    result = hooks.rehydrate(dict(stored))
    if not isinstance(result, dict):
        raise TypeError("rehydrate must return a dict")
    return result


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _store_failure(tool: str, op: str, exc: BaseException) -> None:
    """Log a failed store operation — the class name only, never the text.

    An exception's message can carry what was being written (a stored
    ``result`` holds titles and names), so ``str(exc)`` never reaches the
    log; ``type(exc).__name__`` is enough to tell a timeout from a
    permission error.
    """
    log_mcp_event(
        "mcp_idempotency_store_failure",
        "failure",
        tool=tool,
        op=op,
        error_type=type(exc).__name__,
    )


def _unavailable(tool: str, op: str, exc: BaseException, policy: str) -> None:
    """A claim that could not be established: fail open, or refuse.

    Returns ``None`` (« run unclaimed ») under ``optional``; raises under
    ``required``, where nothing may execute without a claim.
    """
    _store_failure(tool, op, exc)
    if policy == IDEMPOTENCY_REQUIRED:
        raise ToolArgumentError(
            "Le registre d'idempotence est illisible pour le moment : rien "
            "n'a été écrit. Réessayez dans un instant avec la MÊME "
            "idempotency_key.",
            reason="idempotency_store_unavailable",
        )
    return None


def _snapshot_dict(snap: Any) -> Optional[dict]:
    """The stored entry, ``None`` when absent. A snapshot that EXISTS but
    does not hold a dict (a MagicMock's truthy ``exists`` and its MagicMock
    ``to_dict()``, the settings._read_raw precedent) is unreadable — never a
    silent « absent »."""
    if not snap.exists:
        return None
    data = snap.to_dict()
    if not isinstance(data, dict):
        raise MalformedEntry()
    return data


def _expired(data: dict, now: datetime) -> bool:
    """Past ``expire_at``. A missing or non-timestamp value never expires
    here — the entry is still decided on its fingerprint and status — as
    the pre-claim code read a missing one."""
    expire_at = data.get("expire_at")
    if not isinstance(expire_at, datetime):
        return False
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    return expire_at < now


def _age(data: dict, snap: Any, now: datetime) -> Optional[timedelta]:
    """How long ago the pending claim was made, or None if unknowable."""
    claimed_at = data.get("claimed_at") or getattr(snap, "create_time", None)
    if not isinstance(claimed_at, datetime):
        return None
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    return now - claimed_at


def _in_flight_refusal(remaining: timedelta) -> ToolArgumentError:
    """« Wait, then retry with the SAME key » — never « a new key ».

    Worded for every state it can be raised in, not only the one it is
    named after. A pending claim is also what a call leaves behind when it
    ENDED without its result being stored: a finalize that failed on a
    store blip (the write committed, the caller got its result), or a
    ``required`` tool kept pending after a pre-commit failure. Promising
    « you will get its result » unconditionally was false there — the
    retry would wait out the window and then read « interrompu » instead.
    """
    minutes = max(1, -(-int(remaining.total_seconds()) // 60))
    return ToolArgumentError(
        "Un appel avec cette idempotency_key est encore en cours, ou vient "
        "de se terminer sans que son résultat ait pu être enregistré — "
        "attendez puis réessayez avec la MÊME clé : si son résultat a été "
        "enregistré, vous l'obtiendrez, sans double écriture. Ne changez pas "
        "de clé : un appel encore en cours peut écrire. (Au-delà d'environ "
        f"{minutes} min, il sera tenu pour interrompu.)",
        reason="idempotency_in_flight",
    )


def _refuse_pending(data: dict, snap: Any, now: datetime) -> None:
    age = _age(data, snap, now)
    # An unknowable age reads as YOUNG: « wait, same key » can at worst make
    # the caller wait; « new key » could make it write twice.
    if age is None or age < IN_FLIGHT_WINDOW:
        raise _in_flight_refusal(IN_FLIGHT_WINDOW - (age or timedelta(0)))
    raise ToolArgumentError(
        "Un appel avec cette idempotency_key a été interrompu : l'écriture a "
        "peut-être eu lieu, relisez avant de réessayer (nouvelle clé "
        "seulement si rien n'a été écrit).",
        reason="idempotency_interrupted",
    )


def _stored_partial(tool: str, data: dict) -> CommittedWriteError:
    return CommittedWriteError(
        tool,
        loggable_id(data.get("entity_id")),
        loggable_id(data.get("dossier_id")),
        collection=str(data.get("collection") or ""),
        rows=int(data.get("rows") or 0),
        replay=True,
    )


def _decide(tool: str, data: dict, snap: Any, fingerprint: str,
            policy: str, now: datetime) -> Optional[_Replay]:
    """An unexpired entry exists: replay it, or refuse the call.

    Returns a :class:`_Replay`, or ``None`` when the entry is unusable and
    the call fails open (``optional``). Never returns a claim: an existing
    entry always belongs to another call.
    """
    if data.get("args_fingerprint") != fingerprint:
        raise ToolArgumentError(
            "Cette idempotency_key a déjà servi à un appel dont les "
            "arguments diffèrent. Une clé identifie UNE écriture "
            "précise — générez une clé nouvelle pour une écriture "
            "nouvelle.",
            reason="idempotency_conflict",
        )
    status = data.get("status")
    if status is None or status == STATUS_COMMITTED:
        result = data.get("result")
        if isinstance(result, dict):
            return _Replay(dict(result))
        return _unavailable(tool, "lookup", MalformedEntry(), policy)
    if status == STATUS_PARTIAL:
        raise _stored_partial(tool, data)
    if status == STATUS_PENDING:
        _refuse_pending(data, snap, now)
    return _unavailable(tool, "lookup", MalformedEntry(), policy)


def _claim(tool: str, key: str, fingerprint: str,
           policy: str) -> "Optional[_Claim | _Replay]":
    """Claim *key* for this call, or replay/refuse on what already holds it.

    Returns the :class:`_Claim` this call now owns, a :class:`_Replay`, or
    ``None`` when the store failed and the ``optional`` policy runs the call
    unclaimed. Raises ``ToolArgumentError`` for every refusal and
    :class:`CommittedWriteError` for a stored partial.
    """
    try:
        ref = db.collection(COLLECTION).document(_doc_id(tool, key))
    except Exception as exc:
        return _unavailable(tool, "lookup", exc, policy)

    for _attempt in range(_CLAIM_ATTEMPTS):
        now = _now()
        try:
            snap = ref.get()
            data = _snapshot_dict(snap)
        except Exception as exc:  # MalformedEntry included
            return _unavailable(tool, "lookup", exc, policy)

        if data is not None:
            if not _expired(data, now):
                return _decide(tool, data, snap, fingerprint, policy, now)
            # Expired: the TTL just hasn't collected it. Delete it ONLY as
            # read — a caller that re-claimed it meanwhile fails this
            # precondition, and the loop re-reads its claim instead.
            try:
                ref.delete(option=db.write_option(
                    last_update_time=snap.update_time))
            except (gexc.FailedPrecondition, gexc.NotFound):
                continue
            except Exception as exc:
                return _unavailable(tool, "claim", exc, policy)

        claim_id = str(uuid.uuid4())
        try:
            written = ref.create({
                "tool": tool,
                "args_fingerprint": fingerprint,
                "status": STATUS_PENDING,
                "claim_id": claim_id,
                "claimed_at": now,
                "created_at": now,
                "expire_at": now + IDEMPOTENCY_TTL,
            })
        except gexc.Conflict:
            continue  # claimed by a concurrent call in between: decide on it
        except Exception as exc:
            return _unavailable(tool, "claim", exc, policy)
        return _Claim(ref=ref, claim_id=claim_id,
                      update_time=written.update_time)

    # Every round was lost to another call on THIS key. Logged like a store
    # failure (so the op/error_type trail stays one place to look), but
    # REFUSED under both policies: failing open here would run this call
    # unclaimed precisely while a concurrent same-key call is live — the
    # duplicate the claim exists to prevent.
    _store_failure(tool, "claim", ClaimContention())
    raise _in_flight_refusal(IN_FLIGHT_WINDOW)


def _entry_ref(tool: str, key: str) -> Any:
    return db.collection(COLLECTION).document(_doc_id(tool, key))


def _finalize(tool: str, key: str, fingerprint: str,
              claim: Optional[_Claim], payload: dict) -> None:
    """Store the committed result. Best-effort: never fails the write.

    With a claim: a partial ``update()`` under the claim's precondition —
    the fingerprint, tool, claim id and expiry survive, so the next same-key
    call REPLAYS instead of reading as a conflict. A failure leaves the
    claim ``pending``: a same-key retry is then refused, never duplicated.
    Without one (the store failed open earlier): a ``create()`` of the whole
    entry, so a later retry can still replay — never a ``set()``, which
    would overwrite a claim some concurrent call made meanwhile.
    """
    now = _now()
    if claim is not None:
        try:
            claim.ref.update(
                {"status": STATUS_COMMITTED, "result": payload,
                 "committed_at": now},
                option=db.write_option(last_update_time=claim.update_time),
            )
        except Exception as exc:
            _store_failure(tool, "finalize", exc)
        return
    try:
        _entry_ref(tool, key).create({
            "tool": tool,
            "args_fingerprint": fingerprint,
            "status": STATUS_COMMITTED,
            "claim_id": str(uuid.uuid4()),
            "claimed_at": now,
            "created_at": now,
            "committed_at": now,
            "expire_at": now + IDEMPOTENCY_TTL,
            "result": payload,
        })
    except Exception as exc:
        _store_failure(tool, "record", exc)


def _release(tool: str, claim: Optional[_Claim]) -> None:
    """Delete THIS call's pending claim — nothing was committed.

    Under the ``last_update_time`` of the claim's own create(): the only
    « delete only if it is still mine » Firestore can express. A failure
    leaves the claim pending, which refuses a same-key retry for the
    in-flight window and then reads as interrupted — the safe side.
    """
    if claim is None:
        return
    try:
        claim.ref.delete(
            option=db.write_option(last_update_time=claim.update_time))
    except Exception as exc:
        _store_failure(tool, "release", exc)


def _distinct(commits: tuple) -> list[tuple[str, str]]:
    seen: set = set()
    out = []
    for commit in commits:
        if commit not in seen:
            seen.add(commit)
            out.append(commit)
    return out


def _committed_error(tool: str, args: dict,
                     commits: list[tuple[str, str]]) -> CommittedWriteError:
    """The error for a call that committed *commits*, then failed.

    The entity is the FIRST commit — a model writes its primary document
    first. The dossier is a committed ``dossiers`` document when there is
    one (the dossier recorders), else the call's own ``dossier_id``: a
    handler that committed has resolved it. Both only when id-shaped.
    """
    collection, entity_id = commits[0]
    dossier_id = next(
        (i for c, i in commits if c == "dossiers"), None
    ) or args.get("dossier_id")
    return CommittedWriteError(
        tool,
        loggable_id(entity_id),
        loggable_id(dossier_id),
        collection=collection,
        rows=len(commits),
    )


def _record_partial(tool: str, key: str, fingerprint: str,
                    claim: Optional[_Claim], error: CommittedWriteError,
                    commits: list[tuple[str, str]]) -> None:
    """Mark the entry ``partial`` so a same-key retry re-raises, never
    re-executes. Best-effort, like :func:`_finalize`."""
    if not key:
        return
    now = _now()
    fields = {
        "status": STATUS_PARTIAL,
        "partial_at": now,
        "entity_id": error.entity_id or "",
        "dossier_id": error.dossier_id or "",
        "collection": error.collection,
        "rows": error.rows,
        # A list of MAPS — Firestore refuses an array of arrays.
        "commits": [
            {"collection": c, "id": loggable_id(i) or ""}
            for c, i in commits[:_MAX_RECORDED_COMMITS]
        ],
    }
    try:
        if claim is not None:
            claim.ref.update(
                fields,
                option=db.write_option(last_update_time=claim.update_time),
            )
        else:
            _entry_ref(tool, key).create({
                "tool": tool,
                "args_fingerprint": fingerprint,
                "claim_id": str(uuid.uuid4()),
                "claimed_at": now,
                "created_at": now,
                "expire_at": now + IDEMPOTENCY_TTL,
                **fields,
            })
    except Exception as exc:
        _store_failure(tool, "record_partial", exc)


def run_write(tool: str, args: dict, execute: Callable[[], dict]) -> dict:
    """Run a write tool under the shared protocol.

    *execute()* performs the tool's own resolution + validation and the
    actual write. It must raise ``ToolArgumentError`` on any refusal, BEFORE
    writing, so a refused call never records an idempotency entry. See the
    module docstring for the claim, the commit point and the policies.

    ``dry_run`` was REMOVED on 2026-08-27 (user decision). It had never
    been a control: nothing required it, nothing checked it, and a caller
    that simply omitted it wrote. It was a courtesy the model extended or
    not — and the first real batch showed both failure directions at once,
    the model previewing 45 documents nobody asked it to preview while no
    mechanism could have made it preview anything else.

    What it cost was concrete. It doubled every write into two model calls,
    which is what killed a document-analysis batch on ``chain_ceiling``
    after six documents out of forty-five. And it carried its own class of
    trap: because this function short-circuited the dry branch WITHOUT
    calling the model, every model-side guard had to be repeated in the
    handler ahead of it, or a preview would promise a success the real call
    refused. That whole duplication class dies with it.

    What replaces it, for the case it was meant to serve — proposing an
    action without performing it — is simply NOT CALLING the tool and
    describing the intended write instead. The charter and the scheduled
    addendum say exactly that now. It is the honest form: a proposal that
    runs no code cannot half-run.

    The preview property is gone from every input schema, and those schemas
    carry ``additionalProperties: False``, so a caller that still sends
    ``dry_run`` is REFUSED by ``validate_args`` on both paths (the MCP
    endpoint) rather than silently written for —
    which would be the dangerous outcome.
    """
    policy = idempotency_policy(tool)
    key = str(args.get("idempotency_key") or "").strip()
    if not key and policy == IDEMPOTENCY_REQUIRED:
        raise ToolArgumentError(
            "Cet outil exige une `idempotency_key` : choisissez une chaîne "
            "stable qui identifie CETTE écriture, et réutilisez-la telle "
            "quelle si vous devez réessayer.",
            reason="idempotency_required",
        )
    fingerprint = args_fingerprint(args) if key else ""

    claim: Optional[_Claim] = None
    if key:
        outcome = _claim(tool, key, fingerprint, policy)
        if isinstance(outcome, _Replay):
            # Rebuilt through the tool's rehydrate hook, if it declared one
            # (§ 4) — BEFORE the replay flag, so the hook sees what was
            # stored and never a flag of its own making.
            replayed = _rehydrated(tool, outcome.result)
            replayed["idempotent_replay"] = True
            return replayed
        claim = outcome

    # Every model write the tool makes is stamped « mcp » (and its tool
    # name) by the model itself — see models/provenance.py. The block also
    # opens the commit record the models append to, which is read HERE,
    # inside the block: its reset on exit would empty it.
    with provenance.writing_via("mcp", tool=tool):
        try:
            payload = execute()
        except Exception as exc:
            commits = _distinct(provenance.committed_writes())
            if commits:
                refusal = isinstance(exc, ToolArgumentError)
                # A refusal's text describes user-supplied content, so a
                # refusal after a commit (a handler bug) is logged by class,
                # without its traceback; anything else keeps its traceback.
                log_unexpected(
                    "mcp write failed after its commit point",
                    exc_info=not refusal,
                    tool=tool,
                    commits=len(commits),
                    error_type=type(exc).__name__,
                )
                error = _committed_error(tool, args, commits)
                _record_partial(tool, key, fingerprint, claim, error, commits)
                raise error from exc
            if isinstance(exc, ToolArgumentError) and exc.keep_claim:
                # An UNCERTAIN outcome (§ 2): the write may have landed, so
                # the claim stays pending — a same-key retry must never run.
                raise
            if isinstance(exc, ToolArgumentError) or policy != IDEMPOTENCY_REQUIRED:
                _release(tool, claim)
            raise

    payload["idempotent_replay"] = False
    if key:
        # What may be STORED (§ 4): the persist hook's copy, and never a
        # capability. None → nothing is stored and the claim stays pending.
        stored = _storable(tool, payload, claim is not None)
        if stored is not None:
            _finalize(tool, key, fingerprint, claim, stored)
    return payload
