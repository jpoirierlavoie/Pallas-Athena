"""A realistic in-memory Firestore for the test suite — the SERVER is fake,
the CLIENT is the real one.

Why this shape, and not another hand-written stand-in
-----------------------------------------------------
The suite already carries several ad-hoc fakes (``test_trust.py``,
``test_admin_ledger.py``, ``test_mcp_write_support.py``…). Each of them
re-implements a few client methods by hand, and each of them ACCEPTS what
the real store refuses: a write staged inside a transaction lands at once,
``create()`` overwrites, a precondition is ignored, a nested array is
stored. CLAUDE.md records what that costs — « un faux Firestore qui accepte
ce que le vrai magasin refuse ne prouve rien » — and the defect that proved
it surfaced only on the first real call, after the paid model call.

So this module keeps the REAL ``google.cloud.firestore_v1`` client — real
``DocumentReference``, ``Query``, ``WriteBatch``, ``Transaction``,
``DocumentSnapshot``, real ``firestore.transactional`` — and replaces only
the GAPIC transport it talks to (``Client._firestore_api``) with an
in-memory server. Everything the client does before a request leaves the
process is therefore the production code path, verbatim:

* value encoding (a ``datetime.date`` or a ``Decimal`` raises ``TypeError``
  at staging, exactly as in production);
* sentinel handling (``DELETE_FIELD`` in a plain ``set()`` is refused;
  ``update({})`` is refused);
* ``ReadAfterWriteError`` on a transactional read after a staged write, and
  ``ValueError`` on a read through a transaction that is not in progress;
* the ``transactional`` decorator's retry-on-``Aborted`` loop and its
  rollback on any exception;
* cursor normalisation, ``FieldFilter`` handling, ``== None`` becoming an
  ``IS_NULL`` unary filter.

What the fake SERVER implements (the documented Firestore contract):

* documents keyed by full path, subcollections independent of their parent
  (deleting a parent leaves its subcollections, as in production);
* ``Write`` application: full replace, ``update_mask`` merges (dotted paths,
  ``set(merge=True)``, ``DELETE_FIELD``), the field transforms
  (``SERVER_TIMESTAMP``, ``Increment``, ``Maximum``/``Minimum``,
  ``ArrayUnion``/``ArrayRemove``);
* preconditions: ``create()`` on an existing document →
  ``google.api_core.exceptions.AlreadyExists`` (a ``Conflict`` subclass);
  ``update()`` on a missing one → ``NotFound``;
  ``write_option(last_update_time=…)`` mismatch → ``FailedPrecondition``;
  ``write_option(exists=True)`` on a missing document → ``NotFound``;
* atomic commits: every write of a batch or a transaction applies, or none
  does; all writes of one commit share one ``update_time``;
* transactions that BUFFER their writes until commit (the real client does
  the buffering; the server applies them atomically) and that ABORT at
  commit when a document they read — or the result set of a query they
  ran — changed in the meantime. ``Aborted`` is what the real
  ``transactional`` decorator retries, so a stale read-modify-write is
  re-run on fresh data instead of committing a lost update;
* refusal of nested arrays (``InvalidArgument``) — Firestore rejects an
  array that directly contains an array; an array of maps holding arrays is
  legal;
* refusal of invalid document ids at RPC time (``""``, ``.``, ``..``,
  ``__reserved__``) — the client builds such a reference without complaint;
* the per-commit write cap the repo documents (``dav/sync.py``
  ``_BATCH_CHUNK``, ``models/folder.py``): 500 by default, configurable;
* queries: ``==``/``!=``/range/``in``/``not-in``/``array_contains``/
  ``array_contains_any``, ``IS_NULL``/``IS_NOT_NULL``/``IS_NAN``/
  ``IS_NOT_NAN``, AND/OR composites, ``order_by`` (a document missing an
  ordered or filtered field is excluded), the implicit ``__name__``
  tie-break, cursors, ``offset``, ``limit``, projections, collection
  groups, and COUNT/SUM/AVG aggregations. Range filters only match values of
  the operand's type, as in production. Values order by Firestore's type
  order (null < bool < number < timestamp < string < bytes < reference <
  geopoint < array < map).

What it deliberately does NOT model — each would need knowledge the fake
does not have, and silently pretending would be worse than saying so:

* composite INDEXES. A query the production server would refuse for want of
  an index succeeds here. CLAUDE.md item 7 remains a deploy-time check;
* document-size and field-depth limits, and the multi-inequality ordering
  rules of the server;
* real lock contention: a concurrent writer is modelled by
  :meth:`FakeFirestore.external_write` (or a commit hook), and a
  transaction that read the affected document aborts at commit — the
  serializable outcome the real server guarantees by locking;
* whether a no-op write bumps ``update_time``: here every committed write
  does.

``get_all`` answers in REVERSE request order. The real service documents no
order at all, so any order is faithful; a fixed non-request order makes a
caller that zips results against its request list fail here rather than in
production.

Usage::

    from tests._fake_firestore import FakeFirestore, install

    def test_something(monkeypatch):
        fake = install(monkeypatch, dav_sync)      # patches dav_sync.db
        fake.seed("dav_sync/general", {"ctag": "c0", "sync_token": "c0"})
        ...
        assert fake.peek("dav_sync/general")["ctag"] == "c0"

The client is a real ``google.cloud.firestore_v1.client.Client`` subclass,
so ``isinstance(fake, firestore.Client)`` holds and model code needs no
patching beyond its ``db`` attribute. ``firestore.transactional`` is used
UNPATCHED: :data:`transactional` is re-exported only for tests that swap a
module's ``firestore`` namespace.

This file is test support: it lives under ``tests/`` (never deployed —
``.gcloudignore``) and its name does not match ``test_*.py``, so pytest
imports it without collecting it. It leans on documented-private helpers of
``google.cloud.firestore_v1`` (``_helpers.encode_dict``/``decode_dict``);
the exact pin in ``requirements.in`` holds them still, and
``tests/test_fake_firestore.py`` fails loudly if a Dependabot bump moves
them.
"""

from __future__ import annotations

import copy
import functools
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from google.api_core import exceptions as gexc
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1 import field_path as _field_path
from google.cloud.firestore_v1._helpers import GeoPoint
from google.cloud.firestore_v1.client import Client as _RealClient
from google.cloud.firestore_v1.transaction import transactional  # noqa: F401 (re-export)
from google.cloud.firestore_v1.types import aggregation_result as _agg_types
from google.cloud.firestore_v1.types import document as _document_types
from google.cloud.firestore_v1.types import firestore as _firestore_types
from google.cloud.firestore_v1.types import query as _query_types
from google.cloud.firestore_v1.types import write as _write_types
from google.protobuf import timestamp_pb2

__all__ = [
    "CommitInfo",
    "CommitRecord",
    "FakeFirestore",
    "ReadRecord",
    "UnmodelledRPC",
    "install",
    "transactional",
]

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
# The fake clock starts here and advances 1 ms per RPC: deterministic,
# strictly increasing, and far from any date a test is likely to freeze.
_CLOCK_START_US = int((datetime(2026, 1, 1, tzinfo=_UTC) - _EPOCH).total_seconds()) * 1_000_000
_TICK_US = 1_000

# The per-commit cap the repo documents (dav/sync.py and models/folder.py
# chunk at 450 « because Firestore caps a batch at 500 operations »). Kept
# as the default so a chunk size raised past it fails here first.
DEFAULT_MAX_WRITES_PER_COMMIT = 500

_Op = _query_types.StructuredQuery.FieldFilter.Operator
_Unary = _query_types.StructuredQuery.UnaryFilter.Operator
_Composite = _query_types.StructuredQuery.CompositeFilter.Operator
_Dir = _query_types.StructuredQuery.Direction

_RANGE_OPS = frozenset({
    _Op.LESS_THAN, _Op.LESS_THAN_OR_EQUAL,
    _Op.GREATER_THAN, _Op.GREATER_THAN_OR_EQUAL,
})
# Fields filtered by these operators are implicitly ordered by the server.
_INEQUALITY_OPS = _RANGE_OPS | {_Op.NOT_EQUAL, _Op.NOT_IN}
_DISJUNCTION_CAP = {_Op.IN: 30, _Op.ARRAY_CONTAINS_ANY: 30, _Op.NOT_IN: 10}

_ONE_US = timedelta(microseconds=1)


class UnmodelledRPC(NotImplementedError, AttributeError):
    """An RPC the fake server does not implement.

    Loud on a direct call (``NotImplementedError``), yet still an
    ``AttributeError`` so ``hasattr``/``getattr(…, default)`` probes read it
    as « absent » rather than crashing.
    """


# ── Records exposed to tests ───────────────────────────────────────────────


@dataclass(frozen=True)
class ReadRecord:
    """One read RPC, as the server received it.

    ``paths`` are RELATIVE document paths (``"dossiers/d1"``): the documents
    looked up (``get``/``get_all``, found or not) or returned (queries,
    aggregations). ``transaction`` is the transaction id the client sent —
    ``None`` for a read made outside any transaction, which is exactly what
    a « read inside the transaction » assertion needs to see.
    """

    rpc: str
    paths: tuple[str, ...]
    transaction: Optional[bytes]

    @property
    def transactional(self) -> bool:
        return self.transaction is not None


@dataclass(frozen=True)
class CommitInfo:
    """What a commit hook sees BEFORE the server validates or applies it.

    ``index`` counts commit ATTEMPTS from 0 (a transaction retried after
    ``Aborted`` makes several). ``ops`` is ``((kind, relative_path), …)``
    with ``kind`` ∈ create | set | merge | update | delete | transform.
    """

    index: int
    transaction: Optional[bytes]
    ops: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CommitRecord:
    """A commit that SUCCEEDED (refused or aborted attempts are not here)."""

    index: int
    transaction: Optional[bytes]
    ops: tuple[tuple[str, str], ...]
    commit_time: datetime


@dataclass
class _StoredDoc:
    data: dict
    create_us: int
    update_us: int


@dataclass
class _TxnState:
    read_only: bool
    # First-read version of every document read (None = read as missing).
    reads: dict[str, Optional[int]] = field(default_factory=dict)
    # (parent, serialized StructuredQuery, result fingerprint) per query.
    queries: list[tuple[str, bytes, tuple]] = field(default_factory=list)


# ── Small value helpers ────────────────────────────────────────────────────


def _us_to_pb(us: int) -> timestamp_pb2.Timestamp:
    return timestamp_pb2.Timestamp(seconds=us // 1_000_000, nanos=(us % 1_000_000) * 1_000)


def _pb_to_us(ts: timestamp_pb2.Timestamp) -> tuple[int, int]:
    # Compared as (seconds, nanos) so a sub-microsecond precondition from a
    # foreign clock never matches by rounding.
    return ts.seconds, ts.nanos


def _us_to_datetime(us: int) -> DatetimeWithNanoseconds:
    return DatetimeWithNanoseconds.from_timestamp_pb(_us_to_pb(us))


def _datetime_us(value: datetime) -> int:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=_UTC)
    return (aware - _EPOCH) // _ONE_US


class _Name:
    """The ``__name__`` pseudo-field: a document's own path."""

    __slots__ = ("segments",)

    def __init__(self, rel: str) -> None:
        self.segments = tuple(rel.split("/")) if rel else ()


def _sort_key(value: Any) -> tuple:
    """Firestore's total order over values, as a Python-comparable key.

    Equal keys ⇔ Firestore-equal values (so ``1 == 1.0``, but ``True != 1``,
    which Python's own ``==`` gets wrong).
    """
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return (2, 0)
        return (2, 1, value)
    if isinstance(value, datetime):
        return (3, _datetime_us(value))
    if isinstance(value, str):
        return (4, value.encode("utf-8"))
    if isinstance(value, bytes):
        return (5, value)
    if isinstance(value, _Name):
        return (6, tuple(s.encode("utf-8") for s in value.segments))
    if hasattr(value, "_document_path") and hasattr(value, "_path"):
        return (6, tuple(str(s).encode("utf-8") for s in value._path))
    if isinstance(value, GeoPoint):
        return (7, value.latitude, value.longitude)
    if isinstance(value, list):
        return (8, tuple(_sort_key(v) for v in value))
    if isinstance(value, dict):
        return (9, tuple(
            (k.encode("utf-8"), _sort_key(value[k]))
            for k in sorted(value, key=lambda s: s.encode("utf-8"))
        ))
    raise TypeError(f"fake Firestore cannot order a {type(value).__name__}")


def _cmp(a: tuple, b: tuple) -> int:
    return (a > b) - (a < b)


def _get_path(data: Any, parts: list[str]) -> tuple[bool, Any]:
    cur = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _set_path(data: dict, parts: list[str], value: Any) -> None:
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _delete_path(data: dict, parts: list[str]) -> None:
    cur = data
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


def _project(data: dict, paths: Iterable[str]) -> dict:
    out: dict = {}
    for api_repr in paths:
        parts = _field_path.parse_field_path(api_repr)
        if parts == ["__name__"]:
            continue
        present, value = _get_path(data, parts)
        if present:
            _set_path(out, parts, copy.deepcopy(value))
    return out


def _reject_nested_arrays(value_pb, where: str) -> None:
    """Firestore refuses an array that DIRECTLY contains an array."""
    kind = value_pb.WhichOneof("value_type")
    if kind == "array_value":
        for element in value_pb.array_value.values:
            if element.WhichOneof("value_type") == "array_value":
                raise gexc.InvalidArgument(
                    f"Nested arrays are not supported: field '{where}' holds an "
                    "array that directly contains an array (Firestore rejects "
                    "this at commit)."
                )
            _reject_nested_arrays(element, where)
    elif kind == "map_value":
        for key, element in value_pb.map_value.fields.items():
            _reject_nested_arrays(element, f"{where}.{key}" if where else key)


def _check_doc_rel(rel: str, full_name: str) -> None:
    segments = rel.split("/") if rel else []
    if len(segments) < 2 or len(segments) % 2:
        raise gexc.InvalidArgument(f"Document name {full_name!r} is not a document path.")
    for seg in segments:
        if seg in ("", ".", "..") or (len(seg) >= 4 and seg.startswith("__") and seg.endswith("__")):
            raise gexc.InvalidArgument(
                f"Document name {full_name!r} has an invalid path segment {seg!r}."
            )


# ── The fake server (the transport the real client talks to) ─────────────


class _FakeServer:
    """In-memory stand-in for the Firestore GAPIC client.

    Only the RPCs the application uses are implemented; any other attribute
    access raises ``NotImplementedError`` naming it, so an unmodelled RPC
    fails loudly instead of returning a MagicMock-shaped nothing.
    """

    _IMPLEMENTED = frozenset({
        "commit", "begin_transaction", "rollback",
        "batch_get_documents", "run_query", "run_aggregation_query",
    })

    def __init__(self, client: "FakeFirestore", max_writes_per_commit: int) -> None:
        self._client = client
        self._prefix = client._database_string + "/documents"
        self.max_writes_per_commit = max_writes_per_commit
        self.docs: dict[str, _StoredDoc] = {}
        self.clock_us = _CLOCK_START_US
        self.reads: list[ReadRecord] = []
        self.commits: list[CommitRecord] = []
        self.commit_hooks: list[Callable[[CommitInfo], None]] = []
        self._txns: dict[bytes, _TxnState] = {}
        self._txn_seq = 0
        self._commit_attempts = 0

    def __getattr__(self, name: str):  # only reached for unknown attributes
        if name.startswith("__"):
            raise AttributeError(name)
        raise UnmodelledRPC(
            f"fake Firestore does not model the {name!r} RPC; add it to "
            "tests/_fake_firestore.py deliberately rather than stubbing it."
        )

    # ── paths & clock ──────────────────────────────────────────────────

    def tick(self) -> int:
        self.clock_us += _TICK_US
        return self.clock_us

    def rel(self, full_name: str) -> str:
        if full_name == self._prefix:
            return ""
        if not full_name.startswith(self._prefix + "/"):
            raise gexc.InvalidArgument(f"Name {full_name!r} is outside this database.")
        return full_name[len(self._prefix) + 1:]

    def full(self, rel: str) -> str:
        return f"{self._prefix}/{rel}" if rel else self._prefix

    def doc_rel(self, full_name: str) -> str:
        rel = self.rel(full_name)
        _check_doc_rel(rel, full_name)
        return rel

    def _txn(self, txn_id: Optional[bytes]) -> Optional[_TxnState]:
        if txn_id is None or txn_id == b"":
            return None
        state = self._txns.get(txn_id)
        if state is None:
            raise gexc.InvalidArgument(f"Transaction {txn_id!r} is invalid or has expired.")
        return state

    # ── documents as protobufs ─────────────────────────────────────────

    def document_pb(self, rel: str, doc: _StoredDoc, mask: Optional[Iterable[str]]):
        data = _project(doc.data, mask) if mask is not None else doc.data
        return _document_types.Document(
            name=self.full(rel),
            fields=_helpers.encode_dict(data),
            create_time=_us_to_pb(doc.create_us),
            update_time=_us_to_pb(doc.update_us),
        )

    def decode(self, fields_pb) -> dict:
        return _helpers.decode_dict(fields_pb, self._client)

    def decode_value(self, value_pb) -> Any:
        return _helpers.decode_value(value_pb, self._client)

    # ── transactions ───────────────────────────────────────────────────

    def begin_transaction(self, request, metadata=None, **_kwargs):
        options = request.get("options") if isinstance(request, dict) else None
        options_pb = getattr(options, "_pb", options)
        read_only = bool(options_pb is not None and options_pb.HasField("read_only"))
        self._txn_seq += 1
        txn_id = f"fake-txn-{self._txn_seq:06d}".encode("ascii")
        self._txns[txn_id] = _TxnState(read_only=read_only)
        return _firestore_types.BeginTransactionResponse(transaction=txn_id)

    def rollback(self, request, metadata=None, **_kwargs):
        # Lenient on purpose: the real decorator rolls back after a failed
        # commit, and a rollback error would MASK the original exception.
        self._txns.pop(request.get("transaction"), None)
        return None

    # ── reads ──────────────────────────────────────────────────────────

    def batch_get_documents(self, request, metadata=None, **kwargs):
        self._refuse_read_time(request)
        txn_id = request.get("transaction")
        state = self._txn(txn_id)
        mask = request.get("mask")
        mask_paths = None
        if mask:
            mask_paths = list(mask.get("field_paths") if isinstance(mask, dict) else mask.field_paths)
        names = list(request["documents"])
        rels = [self.doc_rel(n) for n in names]
        read_us = self.tick()
        responses = []
        seen: set[str] = set()
        for rel in reversed(rels):
            if rel in seen:
                continue
            seen.add(rel)
            doc = self.docs.get(rel)
            if state is not None:
                state.reads.setdefault(rel, doc.update_us if doc else None)
            if doc is not None:
                responses.append(_firestore_types.BatchGetDocumentsResponse(
                    found=self.document_pb(rel, doc, mask_paths),
                    read_time=_us_to_pb(read_us),
                ))
            else:
                responses.append(_firestore_types.BatchGetDocumentsResponse(
                    missing=self.full(rel), read_time=_us_to_pb(read_us),
                ))
        self.reads.append(ReadRecord("batch_get_documents", tuple(rels), txn_id or None))
        return iter(responses)

    def run_query(self, request, metadata=None, **kwargs):
        self._refuse_read_time(request)
        txn_id = request.get("transaction")
        state = self._txn(txn_id)
        parent = self.rel(request["parent"])
        sq = request["structured_query"]._pb
        rows = self.execute(parent, sq)
        read_us = self.tick()
        self._note_query(state, parent, sq, rows)
        self.reads.append(ReadRecord("run_query", tuple(r for r, _ in rows), txn_id or None))
        mask = [f.field_path for f in sq.select.fields] if sq.HasField("select") else None
        responses = [
            _firestore_types.RunQueryResponse(
                document=self.document_pb(rel, doc, mask), read_time=_us_to_pb(read_us),
            )
            for rel, doc in rows
        ]
        if not responses:
            responses = [_firestore_types.RunQueryResponse(read_time=_us_to_pb(read_us))]
        return iter(responses)

    def run_aggregation_query(self, request, metadata=None, **kwargs):
        self._refuse_read_time(request)
        txn_id = request.get("transaction")
        state = self._txn(txn_id)
        parent = self.rel(request["parent"])
        saq = request["structured_aggregation_query"]._pb
        rows = self.execute(parent, saq.structured_query)
        read_us = self.tick()
        self._note_query(state, parent, saq.structured_query, rows)
        self.reads.append(ReadRecord("run_aggregation_query", tuple(r for r, _ in rows), txn_id or None))
        fields = {}
        for i, agg in enumerate(saq.aggregations):
            alias = agg.alias or f"field_{i + 1}"
            fields[alias] = _helpers.encode_value(self._aggregate(agg, rows))
        return iter([_firestore_types.RunAggregationQueryResponse(
            result=_agg_types.AggregationResult(aggregate_fields=fields),
            read_time=_us_to_pb(read_us),
        )])

    @staticmethod
    def _refuse_read_time(request) -> None:
        if isinstance(request, dict) and request.get("read_time") is not None:
            raise NotImplementedError("fake Firestore does not model read_time (PITR) reads")

    def _note_query(self, state, parent, sq, rows) -> None:
        if state is None:
            return
        for rel, doc in rows:
            state.reads.setdefault(rel, doc.update_us)
        state.queries.append((parent, sq.SerializeToString(), self._fingerprint(rows)))

    @staticmethod
    def _fingerprint(rows) -> tuple:
        return tuple((rel, doc.update_us) for rel, doc in rows)

    def _aggregate(self, agg, rows) -> Any:
        kind = agg.WhichOneof("operator")
        if kind == "count":
            n = len(rows)
            if agg.count.HasField("up_to"):
                n = min(n, agg.count.up_to.value)
            return n
        if kind in ("sum", "avg"):
            spec = agg.sum if kind == "sum" else agg.avg
            parts = _field_path.parse_field_path(spec.field.field_path)
            nums = []
            for _rel, doc in rows:
                present, value = _get_path(doc.data, parts)
                if present and isinstance(value, (int, float)) and not isinstance(value, bool):
                    nums.append(value)
            if kind == "sum":
                return sum(nums) if nums else 0
            return (sum(nums) / len(nums)) if nums else None
        raise NotImplementedError(f"fake Firestore does not model aggregation {kind!r}")

    # ── query execution ────────────────────────────────────────────────

    def execute(self, parent: str, sq) -> list[tuple[str, _StoredDoc]]:
        if len(sq.from_) != 1:
            raise NotImplementedError("fake Firestore models single-collection queries only")
        selector = sq.from_[0]
        cid = selector.collection_id
        if selector.all_descendants:
            prefix = parent + "/" if parent else ""
            candidates = [
                (rel, doc) for rel, doc in self.docs.items()
                if rel.startswith(prefix) and rel.split("/")[-2] == cid
            ]
        else:
            coll = f"{parent}/{cid}" if parent else cid
            candidates = [
                (rel, doc) for rel, doc in self.docs.items()
                if rel.rsplit("/", 1)[0] == coll
            ]

        where = sq.where if sq.HasField("where") else None
        if where is not None:
            self._validate_filter(where)
            candidates = [(r, d) for r, d in candidates if self._match(where, r, d)]

        orders = self._orders(sq, where)
        keyed = []
        for rel, doc in candidates:
            keys = []
            ok = True
            for parts, _desc in orders:
                present, value = self._value_at(rel, doc, parts)
                if not present:
                    ok = False  # an ordered field must exist on the document
                    break
                keys.append(_sort_key(value))
            if ok:
                keyed.append((keys, rel, doc))

        def _order_cmp(a, b) -> int:
            for i, (_parts, desc) in enumerate(orders):
                c = _cmp(a[0][i], b[0][i])
                if c:
                    return -c if desc else c
            return 0

        keyed.sort(key=functools.cmp_to_key(_order_cmp))

        if sq.HasField("start_at"):
            keyed = self._apply_cursor(keyed, orders, sq.start_at, start=True)
        if sq.HasField("end_at"):
            keyed = self._apply_cursor(keyed, orders, sq.end_at, start=False)
        if sq.offset:
            keyed = keyed[sq.offset:]
        if sq.HasField("limit"):
            keyed = keyed[: sq.limit.value]
        return [(rel, doc) for _k, rel, doc in keyed]

    def _orders(self, sq, where) -> list[tuple[list[str], bool]]:
        orders = [
            (_field_path.parse_field_path(o.field.field_path), o.direction == _Dir.DESCENDING)
            for o in sq.order_by
        ]
        last_desc = orders[-1][1] if orders else False
        explicit = {tuple(p) for p, _ in orders}
        implicit = sorted(
            {tuple(p) for p in self._inequality_fields(where)} - explicit
        ) if where is not None else []
        orders += [(list(p), last_desc) for p in implicit]
        if ("__name__",) not in {tuple(p) for p, _ in orders}:
            orders.append((["__name__"], last_desc))
        return orders

    def _inequality_fields(self, flt) -> list[list[str]]:
        kind = flt.WhichOneof("filter_type")
        if kind == "composite_filter":
            out = []
            for sub in flt.composite_filter.filters:
                out += self._inequality_fields(sub)
            return out
        if kind == "field_filter" and flt.field_filter.op in _INEQUALITY_OPS:
            return [_field_path.parse_field_path(flt.field_filter.field.field_path)]
        return []

    def _apply_cursor(self, keyed, orders, cursor, *, start: bool):
        values = [_sort_key(self._cursor_value(v)) for v in cursor.values]
        if len(values) > len(orders):
            raise gexc.InvalidArgument("Too many cursor values for the query's orderings.")
        before = cursor.before
        kept = []
        for item in keyed:
            c = 0
            for i, key in enumerate(values):
                c = _cmp(item[0][i], key)
                if orders[i][1]:
                    c = -c
                if c:
                    break
            if start:
                keep = c > 0 or (c == 0 and before)
            else:
                keep = c < 0 or (c == 0 and not before)
            if keep:
                kept.append(item)
        return kept

    def _cursor_value(self, value_pb):
        if value_pb.WhichOneof("value_type") == "reference_value":
            return _Name(self.rel(value_pb.reference_value))
        return self.decode_value(value_pb)

    def _value_at(self, rel: str, doc: _StoredDoc, parts: list[str]) -> tuple[bool, Any]:
        if parts == ["__name__"]:
            return True, _Name(rel)
        return _get_path(doc.data, parts)

    def _validate_filter(self, flt) -> None:
        kind = flt.WhichOneof("filter_type")
        if kind == "composite_filter":
            for sub in flt.composite_filter.filters:
                self._validate_filter(sub)
        elif kind == "field_filter":
            op = flt.field_filter.op
            if op in _DISJUNCTION_CAP:
                operand = self.decode_value(flt.field_filter.value)
                if not isinstance(operand, list) or not operand:
                    raise gexc.InvalidArgument(f"'{_Op(op).name}' requires a non-empty array.")
                if len(operand) > _DISJUNCTION_CAP[op]:
                    raise gexc.InvalidArgument(
                        f"'{_Op(op).name}' supports up to {_DISJUNCTION_CAP[op]} values."
                    )

    def _match(self, flt, rel: str, doc: _StoredDoc) -> bool:
        kind = flt.WhichOneof("filter_type")
        if kind == "composite_filter":
            subs = [self._match(s, rel, doc) for s in flt.composite_filter.filters]
            if flt.composite_filter.op == _Composite.OR:
                return any(subs)
            return all(subs)
        if kind == "unary_filter":
            uf = flt.unary_filter
            present, value = self._value_at(rel, doc, _field_path.parse_field_path(uf.field.field_path))
            if not present:
                return False
            is_nan = isinstance(value, float) and math.isnan(value)
            if uf.op == _Unary.IS_NULL:
                return value is None
            if uf.op == _Unary.IS_NOT_NULL:
                return value is not None
            if uf.op == _Unary.IS_NAN:
                return is_nan
            if uf.op == _Unary.IS_NOT_NAN:
                return value is not None and not is_nan
            raise NotImplementedError(f"unary operator {uf.op}")
        if kind == "field_filter":
            ff = flt.field_filter
            present, value = self._value_at(rel, doc, _field_path.parse_field_path(ff.field.field_path))
            if not present:
                return False
            operand = self._cursor_value(ff.value)
            vk = _sort_key(value)
            op = ff.op
            if op == _Op.EQUAL:
                return vk == _sort_key(operand)
            if op == _Op.NOT_EQUAL:
                return value is not None and vk != _sort_key(operand)
            if op in _RANGE_OPS:
                ok_ = _sort_key(operand)
                if vk[0] != ok_[0] or vk == (2, 0) or ok_ == (2, 0):
                    return False  # range filters match only the operand's type; NaN never
                c = _cmp(vk, ok_)
                return {
                    _Op.LESS_THAN: c < 0, _Op.LESS_THAN_OR_EQUAL: c <= 0,
                    _Op.GREATER_THAN: c > 0, _Op.GREATER_THAN_OR_EQUAL: c >= 0,
                }[op]
            if op == _Op.ARRAY_CONTAINS:
                return isinstance(value, list) and any(_sort_key(e) == _sort_key(operand) for e in value)
            if op == _Op.IN:
                return any(vk == _sort_key(o) for o in operand)
            if op == _Op.ARRAY_CONTAINS_ANY:
                wanted = {_sort_key(o) for o in operand}
                return isinstance(value, list) and any(_sort_key(e) in wanted for e in value)
            if op == _Op.NOT_IN:
                return value is not None and all(vk != _sort_key(o) for o in operand)
            raise NotImplementedError(f"field operator {op}")
        raise NotImplementedError(f"filter type {kind!r}")

    # ── commits ────────────────────────────────────────────────────────

    def commit(self, request, metadata=None, **kwargs):
        txn_id = request.get("transaction") or None
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        index = self._commit_attempts
        self._commit_attempts += 1
        ops = tuple((self._op_kind(w), self.rel(self._write_name(w))) for w in writes)
        info = CommitInfo(index=index, transaction=txn_id, ops=ops)
        for hook in list(self.commit_hooks):
            hook(info)  # may raise: the commit then applies nothing

        if len(writes) > self.max_writes_per_commit:
            raise gexc.InvalidArgument(
                f"maximum {self.max_writes_per_commit} writes allowed per request "
                f"(got {len(writes)})"
            )
        state = self._txn(txn_id)
        if state is not None:
            if state.read_only and writes:
                raise gexc.InvalidArgument("Cannot modify entities in a read-only transaction.")
            self._check_serializable(txn_id, state)

        for w in writes:
            self.doc_rel(self._write_name(w))
            if w.WhichOneof("operation") == "update":
                for key, value in w.update.fields.items():
                    _reject_nested_arrays(value, key)
            for t in list(w.update_transforms) + (
                list(w.transform.field_transforms) if w.WhichOneof("operation") == "transform" else []
            ):
                for operand in self._transform_operands(t):
                    _reject_nested_arrays(operand, t.field_path)

        commit_us = self.tick()
        working = dict(self.docs)
        results = [self._apply(working, w, commit_us) for w in writes]  # may raise
        self.docs = working
        if txn_id is not None:
            self._txns.pop(txn_id, None)
        self.commits.append(CommitRecord(
            index=index, transaction=txn_id, ops=ops, commit_time=_us_to_datetime(commit_us),
        ))
        return _firestore_types.CommitResponse(
            write_results=results, commit_time=_us_to_pb(commit_us),
        )

    def _check_serializable(self, txn_id: bytes, state: _TxnState) -> None:
        stale = [
            rel for rel, seen in state.reads.items()
            if (self.docs[rel].update_us if rel in self.docs else None) != seen
        ]
        if not stale:
            for parent, sq_bytes, fingerprint in state.queries:
                sq = _query_types.StructuredQuery.pb()()
                sq.ParseFromString(sq_bytes)
                if self._fingerprint(self.execute(parent, sq)) != fingerprint:
                    stale.append(f"(query on {parent or '/'})")
                    break
        if stale:
            # The server ends an aborted transaction; the real decorator
            # then begins a new one and re-runs the function.
            self._txns.pop(txn_id, None)
            raise gexc.Aborted(
                "Transaction aborted: data read by the transaction changed "
                f"before commit ({', '.join(sorted(stale))})."
            )

    @staticmethod
    def _write_name(w) -> str:
        op = w.WhichOneof("operation")
        if op == "update":
            return w.update.name
        if op == "delete":
            return w.delete
        if op == "transform":
            return w.transform.document
        raise NotImplementedError(f"write operation {op!r}")

    @staticmethod
    def _op_kind(w) -> str:
        op = w.WhichOneof("operation")
        if op != "update":
            return op
        pre = w.current_document.WhichOneof("condition_type") if w.HasField("current_document") else None
        if pre == "exists" and not w.current_document.exists:
            return "create"
        if w.HasField("update_mask"):
            return "update" if pre is not None else "merge"
        return "set"

    @staticmethod
    def _transform_operands(t) -> list:
        """The operand VALUES of a transform, as the field would hold them.

        The array transforms are wrapped back into one array value: the
        elements land INSIDE the field's array, so an element that is itself
        an array is a nested array even though no single operand nests.
        """
        kind = t.WhichOneof("transform_type")
        if kind in ("increment", "maximum", "minimum"):
            return [getattr(t, kind)]
        if kind in ("append_missing_elements", "remove_all_from_array"):
            wrapped = _document_types.Value.pb()()
            wrapped.array_value.CopyFrom(getattr(t, kind))
            return [wrapped]
        return []

    def _check_precondition(self, w, current: Optional[_StoredDoc], name: str) -> None:
        if not w.HasField("current_document"):
            return
        cd = w.current_document
        kind = cd.WhichOneof("condition_type")
        if kind == "exists":
            if cd.exists and current is None:
                raise gexc.NotFound(f"No document to update: {name}")
            if not cd.exists and current is not None:
                raise gexc.AlreadyExists(f"Document already exists: {name}")
        elif kind == "update_time":
            want = _pb_to_us(cd.update_time)
            have = _pb_to_us(_us_to_pb(current.update_us)) if current is not None else None
            if have != want:
                raise gexc.FailedPrecondition(
                    f"The document {name} does not match the required update_time "
                    "precondition (it changed, or does not exist)."
                )

    def _apply(self, working: dict, w, commit_us: int):
        op = w.WhichOneof("operation")
        name = self._write_name(w)
        rel = self.doc_rel(name)
        current = working.get(rel)
        self._check_precondition(w, current, name)
        if op == "delete":
            working.pop(rel, None)
            return _write_types.WriteResult()
        if op == "update":
            fields = self.decode(w.update.fields)
            if w.HasField("update_mask"):
                data = copy.deepcopy(current.data) if current is not None else {}
                for api_repr in w.update_mask.field_paths:
                    parts = _field_path.parse_field_path(api_repr)
                    present, value = _get_path(fields, parts)
                    if present:
                        _set_path(data, parts, copy.deepcopy(value))
                    else:
                        _delete_path(data, parts)  # DELETE_FIELD: in the mask, not in the fields
            else:
                data = fields
            transforms = list(w.update_transforms)
        else:  # legacy stand-alone transform write
            data = copy.deepcopy(current.data) if current is not None else {}
            transforms = list(w.transform.field_transforms)
        transform_results = [
            _helpers.encode_value(self._apply_transform(data, t, commit_us)) for t in transforms
        ]
        created = current.create_us if current is not None else commit_us
        working[rel] = _StoredDoc(data=data, create_us=created, update_us=commit_us)
        return _write_types.WriteResult(
            update_time=_us_to_pb(commit_us), transform_results=transform_results,
        )

    def _apply_transform(self, data: dict, t, commit_us: int) -> Any:
        parts = _field_path.parse_field_path(t.field_path)
        present, current = _get_path(data, parts)
        kind = t.WhichOneof("transform_type")
        is_num = present and isinstance(current, (int, float)) and not isinstance(current, bool)
        if kind == "set_to_server_value":
            result: Any = _us_to_datetime(commit_us)
        elif kind == "increment":
            operand = self.decode_value(t.increment)
            result = current + operand if is_num else operand
        elif kind == "maximum":
            operand = self.decode_value(t.maximum)
            result = max(current, operand) if is_num else operand
        elif kind == "minimum":
            operand = self.decode_value(t.minimum)
            result = min(current, operand) if is_num else operand
        elif kind == "append_missing_elements":
            result = list(current) if present and isinstance(current, list) else []
            keys = [_sort_key(v) for v in result]
            for element in t.append_missing_elements.values:
                value = self.decode_value(element)
                if _sort_key(value) not in keys:
                    result.append(value)
                    keys.append(_sort_key(value))
        elif kind == "remove_all_from_array":
            doomed = {_sort_key(self.decode_value(e)) for e in t.remove_all_from_array.values}
            base = current if present and isinstance(current, list) else []
            result = [v for v in base if _sort_key(v) not in doomed]
        else:
            raise NotImplementedError(f"field transform {kind!r}")
        _set_path(data, parts, result)
        return result

    # ── direct access (setup, assertions, concurrent writers) ──────────

    def normalize(self, data: dict, where: str) -> dict:
        """Round-trip *data* through the REAL codec, as the store would.

        A value the client cannot encode raises ``TypeError`` (as it would at
        staging); a nested array raises ``InvalidArgument`` (as the server
        would at commit); naive datetimes come back aware UTC.
        """
        if not isinstance(data, dict):
            raise TypeError(f"a document must be a dict, not {type(data).__name__}")
        encoded = _helpers.encode_dict(data)
        for key, value in encoded.items():
            _reject_nested_arrays(getattr(value, "_pb", value), key)
        return _helpers.decode_dict(
            {k: getattr(v, "_pb", v) for k, v in encoded.items()}, self._client
        )

    def direct_write(self, rel: str, data: dict) -> None:
        _check_doc_rel(rel, rel)
        normalized = self.normalize(data, rel)
        now = self.tick()
        current = self.docs.get(rel)
        created = current.create_us if current is not None else now
        self.docs[rel] = _StoredDoc(data=normalized, create_us=created, update_us=now)

    def direct_delete(self, rel: str) -> None:
        _check_doc_rel(rel, rel)
        self.tick()
        self.docs.pop(rel, None)


# ── The client ─────────────────────────────────────────────────────────────


def _clean_rel(path: str) -> str:
    return str(path).strip("/")


class FakeFirestore(_RealClient):
    """A REAL ``firestore.Client`` wired to the in-memory server above.

    Monkeypatch it in wherever a module holds ``db`` (see :func:`install`).
    The helper methods below are for test SETUP and ASSERTIONS; the code
    under test should only ever use the ordinary client API.
    """

    def __init__(
        self,
        project: str = "fake-project",
        *,
        max_writes_per_commit: int = DEFAULT_MAX_WRITES_PER_COMMIT,
    ) -> None:
        super().__init__(project=project, credentials=AnonymousCredentials())
        self._fake_server = _FakeServer(self, max_writes_per_commit)
        self._firestore_api_internal = self._fake_server

    # ── setup ──────────────────────────────────────────────────────────

    def seed(self, path: str, data: dict) -> None:
        """Store *data* at document *path* directly (no RPC is logged).

        Goes through the real codec, so what a test seeds is exactly what
        the store could hold.
        """
        self._fake_server.direct_write(_clean_rel(path), data)

    def seed_collection(self, collection_path: str, docs: dict[str, dict]) -> None:
        base = _clean_rel(collection_path)
        for doc_id, data in docs.items():
            self.seed(f"{base}/{doc_id}", data)

    def external_write(self, path: str, data: dict) -> None:
        """Simulate ANOTHER process writing *path* (outside any transaction).

        Advances the document's ``update_time``, so a transaction that read
        it aborts at commit and a ``last_update_time`` precondition taken
        before it no longer matches.
        """
        self._fake_server.direct_write(_clean_rel(path), data)

    def external_delete(self, path: str) -> None:
        """Simulate another process deleting *path*."""
        self._fake_server.direct_delete(_clean_rel(path))

    # ── assertions ─────────────────────────────────────────────────────

    def peek(self, path: str) -> Optional[dict]:
        """Deep copy of the stored document at *path*, or ``None``. No RPC."""
        doc = self._fake_server.docs.get(_clean_rel(path))
        return copy.deepcopy(doc.data) if doc is not None else None

    def peek_collection(self, collection_path: str) -> dict[str, dict]:
        """``{doc_id: data}`` of one collection (not its subcollections)."""
        base = _clean_rel(collection_path)
        out = {}
        for rel, doc in self._fake_server.docs.items():
            parent, _, doc_id = rel.rpartition("/")
            if parent == base:
                out[doc_id] = copy.deepcopy(doc.data)
        return dict(sorted(out.items()))

    def stored_update_time(self, path: str) -> Optional[datetime]:
        doc = self._fake_server.docs.get(_clean_rel(path))
        return _us_to_datetime(doc.update_us) if doc is not None else None

    @property
    def reads(self) -> list[ReadRecord]:
        return self._fake_server.reads

    @property
    def commits(self) -> list[CommitRecord]:
        return self._fake_server.commits

    @property
    def max_writes_per_commit(self) -> int:
        return self._fake_server.max_writes_per_commit

    def reads_outside_transactions(self) -> list[ReadRecord]:
        return [r for r in self.reads if not r.transactional]

    def reset_logs(self) -> None:
        self._fake_server.reads.clear()
        self._fake_server.commits.clear()

    def add_commit_hook(self, hook: Callable[[CommitInfo], None]) -> Callable[[], None]:
        """Run *hook* at the start of every commit attempt; returns a remover.

        A hook that RAISES models a server-side failure of that commit —
        nothing of it is applied. A hook that calls :meth:`external_write`
        models a concurrent writer racing the commit.
        """
        self._fake_server.commit_hooks.append(hook)

        def _remove() -> None:
            if hook in self._fake_server.commit_hooks:
                self._fake_server.commit_hooks.remove(hook)

        return _remove


def install(monkeypatch, *modules, fake: Optional[FakeFirestore] = None) -> FakeFirestore:
    """Patch ``module.db`` on every module given; return the fake.

    ``monkeypatch.setattr`` raises on a module without a ``db`` attribute, so
    a typo cannot silently patch nothing.
    """
    fake = fake if fake is not None else FakeFirestore()
    for module in modules:
        monkeypatch.setattr(module, "db", fake)
    return fake
