"""Pure-logic tests for the P2 dashboard aggregation work.

Every Firestore call is stubbed or monkeypatched — these tests exercise
result shaping, rounding, cutoff selection, overdue-first sorting, and the
collection-group parent-join only. The correctness of the SDK call shapes
(``Query.count/sum`` chaining, ``AggregationQuery.get()`` returning a list of
lists of ``AggregationResult``, ``Client.collection_group``) was established
by inspecting google-cloud-firestore 2.27 directly.

Composite indexes required by the new query paths (deploy with
``firebase deploy --only firestore:indexes --project athena-pallas`` after
pointing firebase.json at firestore.indexes.json):

- timeentries (billable ASC, invoiced ASC, hours ASC, amount ASC):
  serves ``get_unbilled_totals`` — one aggregation query running SUM(hours)
  and SUM(amount) over billable==True AND invoiced==False. Firestore requires
  the composite index serving a SUM/AVG aggregation to contain the filtered
  fields and every aggregated field.
- timeentries (billable ASC, invoiced ASC, hours ASC) and
  timeentries (billable ASC, invoiced ASC, amount ASC): defensive single-SUM
  variants of the same query, in case the two SUMs are ever issued separately
  or the planner requires per-field indexes.
- invoices (status ASC, amount_due ASC): serves ``get_outstanding_total`` —
  SUM(amount_due) over status in (envoyée, en_retard); ``in`` counts as an
  equality for index purposes.
- tasks (status ASC, due_date ASC): serves ``list_urgent_tasks`` —
  status in (à_faire, en_cours) AND due_date <= cutoff, order_by due_date.
- dossiers (status ASC, prescription_date ASC): serves
  ``list_prescription_alerts`` — status == actif AND
  prescription_date <= cutoff, order_by prescription_date.
- steps (status ASC, deadline_date ASC) with queryScope COLLECTION_GROUP:
  serves ``protocol.list_urgent_steps`` — the collection-group query over
  every protocol's steps subcollection.
- fieldOverride steps.deadline_date (adds COLLECTION_GROUP ASC to the
  defaults): defensive, lets a deadline-only collection-group query run
  should the status filter ever be removed.

No composite index is needed for ``dossier.count_open`` (COUNT over a
single-field ``in`` filter), ``list_hearings_in_range`` (range + order_by on
the same field), or the file-number suggestion (same-field range + order_by).
"""

import importlib
import importlib.util
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone
from unittest import mock

# Ensure athena/ is on the path when running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Conditional third-party stubs ─────────────────────────────────────────
# The canonical CI venv has google-cloud-firestore / firebase-admin /
# icalendar installed; the bare local interpreter may not. Stub whatever is
# missing so the modules under test import either way — the stubs are inert
# because every test replaces the model-level ``db`` handle anyway.


def _module_available(name: str) -> bool:
    """Return True when *name* is importable without actually importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name: str, module: types.ModuleType) -> None:
    """Register *module* under *name*, creating stub parent packages as needed."""
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg in sys.modules:
            continue
        if _module_available(pkg):
            importlib.import_module(pkg)
            continue
        pkg_module = types.ModuleType(pkg)
        pkg_module.__path__ = []  # mark as package
        sys.modules[pkg] = pkg_module
        if i > 1:
            setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], pkg_module)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _module_available("google.cloud.firestore"):
    _firestore_stub = types.ModuleType("google.cloud.firestore")

    class _StubQuery:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"

    _firestore_stub.Client = mock.MagicMock(name="firestore.Client")
    _firestore_stub.Query = _StubQuery
    _firestore_stub.Transaction = type("Transaction", (), {})
    _firestore_stub.transactional = lambda func: func
    _install_stub("google.cloud.firestore", _firestore_stub)

if not _module_available("google.cloud.firestore_v1.base_query"):
    _base_query_stub = types.ModuleType("google.cloud.firestore_v1.base_query")

    class _StubFieldFilter:
        def __init__(self, field_path: str, op_string: str, value: object = None):
            self.field_path = field_path
            self.op_string = op_string
            self.value = value

    _base_query_stub.FieldFilter = _StubFieldFilter
    _install_stub("google.cloud.firestore_v1.base_query", _base_query_stub)

if not _module_available("icalendar"):
    _install_stub("icalendar", types.ModuleType("icalendar"))

if not _module_available("firebase_admin"):
    _fa_stub = types.ModuleType("firebase_admin")
    _fa_stub.__path__ = []
    _fa_auth_stub = types.ModuleType("firebase_admin.auth")
    _install_stub("firebase_admin", _fa_stub)
    _install_stub("firebase_admin.auth", _fa_auth_stub)


# Import the modules under test with the Firestore client constructor
# patched so no credentials/emulator are required in any environment
# (models/__init__.py instantiates the client at import time).
with mock.patch("google.cloud.firestore.Client"):
    import models  # noqa: F401  — binds models.db to the patched client
    import models.dossier as dossier_model
    import models.hearing as hearing_model
    import models.invoice as invoice_model
    import models.protocol as protocol_model
    import models.task as task_model
    import models.time_entry as time_entry_model
    import routes.dashboard as dashboard


UTC = timezone.utc
NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


# ── Test doubles ──────────────────────────────────────────────────────────


class _AggResult:
    """Stand-in for google.cloud.firestore_v1.base_aggregation.AggregationResult."""

    def __init__(self, alias: str, value: float):
        self.alias = alias
        self.value = value


class _ChainQueryStub:
    """Chainable query/aggregation stub.

    Any method call (collection, where, order_by, limit, sum, count, …)
    returns the stub itself; ``get()`` returns the canned aggregation result
    and ``stream()`` yields the canned snapshots.
    """

    def __init__(self, get_result: list | None = None, stream_result: list | None = None):
        self._get_result = get_result if get_result is not None else []
        self._stream_result = stream_result if stream_result is not None else []
        self.calls: list[tuple] = []

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return self._get_result

    def stream(self, *args, **kwargs):
        self.calls.append(("stream", args, kwargs))
        return iter(self._stream_result)

    def __getattr__(self, name: str):
        def _chain(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return _chain


class _ExplodingDb:
    """db stub whose every query entry point raises."""

    def collection(self, *args, **kwargs):
        raise RuntimeError("firestore unavailable")

    def collection_group(self, *args, **kwargs):
        raise RuntimeError("firestore unavailable")

    def get_all(self, *args, **kwargs):
        raise RuntimeError("firestore unavailable")


class _FakeRef:
    def __init__(self, ref_id: str, parent: "_FakeRef | None" = None):
        self.id = ref_id
        self.parent = parent


class _FakeSnap:
    def __init__(self, data: dict | None, reference: _FakeRef | None = None,
                 exists: bool = True, snap_id: str = ""):
        self._data = data
        self.reference = reference
        self.exists = exists
        self.id = snap_id or (reference.id if reference else "")

    def to_dict(self) -> dict | None:
        return self._data


def _step_snap(step: dict, protocol_id: str) -> _FakeSnap:
    """Build a fake step snapshot at protocols/{protocol_id}/steps/{step_id}."""
    proto_ref = _FakeRef(protocol_id)
    steps_col = _FakeRef("steps", parent=proto_ref)
    return _FakeSnap(step, reference=_FakeRef(step.get("id", "step"), parent=steps_col))


class _FakeProtocolDb:
    """db stub for the collection-group urgent-steps query."""

    def __init__(self, step_snaps: list, protocols: dict):
        self._step_snaps = step_snaps
        self._protocols = protocols  # {protocol_id: dict | None (missing doc)}
        self.get_all_calls: list[list[str]] = []

    def collection_group(self, name: str) -> _ChainQueryStub:
        assert name == "steps"
        return _ChainQueryStub(stream_result=self._step_snaps)

    def get_all(self, refs: list) -> list:
        self.get_all_calls.append([r.id for r in refs])
        out = []
        for ref in refs:
            data = self._protocols.get(ref.id)
            out.append(_FakeSnap(data, exists=data is not None, snap_id=ref.id))
        return out


# ── _aggregation_values (result-shape flattening) ─────────────────────────


def test_aggregation_values_nested_lists():
    results = [[_AggResult("hours", 7.5), _AggResult("amount", 187500)]]
    assert time_entry_model._aggregation_values(results) == {
        "hours": 7.5,
        "amount": 187500,
    }


def test_aggregation_values_flat_list_tolerated():
    results = [_AggResult("open", 4)]
    assert dossier_model._aggregation_values(results) == {"open": 4}


def test_aggregation_values_empty():
    assert invoice_model._aggregation_values([]) == {}


# ── time_entry.get_unbilled_totals ────────────────────────────────────────


def test_unbilled_totals_rounding_and_coercion(monkeypatch):
    stub = _ChainQueryStub(
        get_result=[[_AggResult("hours", 12.34), _AggResult("amount", 45678.0)]]
    )
    monkeypatch.setattr(time_entry_model, "db", stub)
    totals = time_entry_model.get_unbilled_totals()
    assert totals == {"hours": 12.3, "amount": 45678}
    assert isinstance(totals["hours"], float)
    assert isinstance(totals["amount"], int)


def test_unbilled_totals_empty_aggregation_defaults_to_zero(monkeypatch):
    monkeypatch.setattr(time_entry_model, "db", _ChainQueryStub(get_result=[]))
    assert time_entry_model.get_unbilled_totals() == {"hours": 0.0, "amount": 0}


def test_unbilled_totals_failure_returns_safe_default(monkeypatch):
    monkeypatch.setattr(time_entry_model, "db", _ExplodingDb())
    assert time_entry_model.get_unbilled_totals() == {"hours": 0.0, "amount": 0}


# ── dossier.count_open / invoice.get_outstanding_total ───────────────────


def test_count_open_coerces_to_int(monkeypatch):
    stub = _ChainQueryStub(get_result=[[_AggResult("open", 3.0)]])
    monkeypatch.setattr(dossier_model, "db", stub)
    result = dossier_model.count_open()
    assert result == 3
    assert isinstance(result, int)


def test_count_open_failure_returns_zero(monkeypatch):
    monkeypatch.setattr(dossier_model, "db", _ExplodingDb())
    assert dossier_model.count_open() == 0


def test_outstanding_total_sums_cents(monkeypatch):
    stub = _ChainQueryStub(get_result=[[_AggResult("outstanding", 250075)]])
    monkeypatch.setattr(invoice_model, "db", stub)
    result = invoice_model.get_outstanding_total()
    assert result == 250075
    assert isinstance(result, int)


def test_outstanding_total_failure_returns_zero(monkeypatch):
    monkeypatch.setattr(invoice_model, "db", _ExplodingDb())
    assert invoice_model.get_outstanding_total() == 0


# ── task.list_urgent_tasks / hearing.list_hearings_in_range ──────────────


def test_list_urgent_tasks_maps_documents(monkeypatch):
    docs = [
        {"id": "t1", "title": "A", "due_date": NOW + timedelta(days=1)},
        {"id": "t2", "title": "B", "due_date": NOW + timedelta(days=2)},
    ]
    stub = _ChainQueryStub(stream_result=[_FakeSnap(d, snap_id=d["id"]) for d in docs])
    monkeypatch.setattr(task_model, "db", stub)
    assert task_model.list_urgent_tasks(NOW + timedelta(days=14)) == docs


def test_list_urgent_tasks_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(task_model, "db", _ExplodingDb())
    assert task_model.list_urgent_tasks(NOW) == []


def test_list_hearings_in_range_maps_documents(monkeypatch):
    docs = [{"id": "h1", "start_datetime": NOW + timedelta(days=1)}]
    stub = _ChainQueryStub(stream_result=[_FakeSnap(d, snap_id=d["id"]) for d in docs])
    monkeypatch.setattr(hearing_model, "db", stub)
    assert hearing_model.list_hearings_in_range(NOW, NOW + timedelta(days=7)) == docs


def test_list_hearings_in_range_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(hearing_model, "db", _ExplodingDb())
    assert hearing_model.list_hearings_in_range(NOW, NOW + timedelta(days=7)) == []


# ── dossier.list_prescription_alerts (legacy party migration) ─────────────


def test_prescription_alerts_migrates_legacy_parties(monkeypatch):
    legacy = {
        "id": "d1",
        "client_id": "p1",
        "client_name": "Client X",
        "prescription_date": NOW + timedelta(days=5),
    }
    stub = _ChainQueryStub(stream_result=[_FakeSnap(legacy, snap_id="d1")])
    monkeypatch.setattr(dossier_model, "db", stub)
    alerts = dossier_model.list_prescription_alerts(NOW + timedelta(days=60))
    assert len(alerts) == 1
    assert alerts[0]["clients"] == [{"id": "p1", "name": "Client X"}]
    assert alerts[0]["client_ids"] == ["p1"]


def test_prescription_alerts_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(dossier_model, "db", _ExplodingDb())
    assert dossier_model.list_prescription_alerts(NOW) == []


# ── dossier._suggest_next_file_number (P7 carry-over) ─────────────────────


def _year() -> int:
    return datetime.now(timezone.utc).year


def test_suggest_file_number_increments_top_result(monkeypatch):
    stub = _ChainQueryStub(
        stream_result=[_FakeSnap({"file_number": f"{_year()}-007"}, snap_id="d1")]
    )
    monkeypatch.setattr(dossier_model, "db", stub)
    assert dossier_model._suggest_next_file_number() == f"{_year()}-008"


def test_suggest_file_number_empty_year_starts_at_one(monkeypatch):
    monkeypatch.setattr(dossier_model, "db", _ChainQueryStub(stream_result=[]))
    assert dossier_model._suggest_next_file_number() == f"{_year()}-001"


def test_suggest_file_number_unparseable_falls_back(monkeypatch):
    stub = _ChainQueryStub(
        stream_result=[_FakeSnap({"file_number": f"{_year()}-XYZ"}, snap_id="d1")]
    )
    monkeypatch.setattr(dossier_model, "db", stub)
    assert dossier_model._suggest_next_file_number() == f"{_year()}-001"


# ── protocol.list_urgent_steps (collection-group parent join) ─────────────


def _urgent_step_fixture() -> _FakeProtocolDb:
    cutoff_safe = NOW + timedelta(days=3)
    steps = [
        # Two steps under the same active protocol p1 (parents must dedupe)
        _step_snap({"id": "s1", "title": "Étape 1", "status": "en_retard",
                    "deadline_date": cutoff_safe - timedelta(days=10)}, "p1"),
        _step_snap({"id": "s2", "title": "Étape 2", "status": "à_venir",
                    "deadline_date": cutoff_safe}, "p1"),
        # Step under a non-active protocol p2 — must be dropped
        _step_snap({"id": "s3", "title": "Étape 3", "status": "à_venir",
                    "deadline_date": cutoff_safe}, "p2"),
        # Completed step (defensive Python-side filter) — must be dropped
        _step_snap({"id": "s4", "title": "Étape 4", "status": "complété",
                    "deadline_date": cutoff_safe}, "p1"),
        # Step whose parent protocol doc is missing — must be dropped
        _step_snap({"id": "s5", "title": "Étape 5", "status": "à_venir",
                    "deadline_date": cutoff_safe}, "p9"),
    ]
    protocols = {
        "p1": {"id": "p1", "title": "Protocole 1", "status": "actif",
               "dossier_file_number": "2026-001"},
        "p2": {"id": "p2", "title": "Protocole 2", "status": "complété",
               "dossier_file_number": "2026-002"},
        "p9": None,  # missing document
    }
    return _FakeProtocolDb(steps, protocols)


def test_list_urgent_steps_joins_and_filters_parents(monkeypatch):
    fake_db = _urgent_step_fixture()
    monkeypatch.setattr(protocol_model, "db", fake_db)
    steps = protocol_model.list_urgent_steps(NOW + timedelta(days=14))

    assert [s["id"] for s in steps] == ["s1", "s2"]
    for step in steps:
        assert step["_protocol_id"] == "p1"
        assert step["_protocol_title"] == "Protocole 1"
        assert step["_dossier_file_number"] == "2026-001"


def test_list_urgent_steps_dedupes_parent_fetches(monkeypatch):
    fake_db = _urgent_step_fixture()
    monkeypatch.setattr(protocol_model, "db", fake_db)
    protocol_model.list_urgent_steps(NOW + timedelta(days=14))

    # One get_all round-trip, with each distinct parent fetched exactly once
    assert len(fake_db.get_all_calls) == 1
    fetched = fake_db.get_all_calls[0]
    assert sorted(fetched) == ["p1", "p2", "p9"]
    assert len(fetched) == len(set(fetched))


def test_list_urgent_steps_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(protocol_model, "db", _ExplodingDb())
    assert protocol_model.list_urgent_steps(NOW) == []


# ── routes.dashboard._get_quick_stats ─────────────────────────────────────


def test_quick_stats_shape_and_passthrough(monkeypatch):
    monkeypatch.setattr(dossier_model, "count_open", lambda: 7)
    monkeypatch.setattr(
        time_entry_model, "get_unbilled_totals",
        lambda: {"hours": 12.3, "amount": 45678},
    )
    monkeypatch.setattr(invoice_model, "get_outstanding_total", lambda: 250075)

    stats = dashboard._get_quick_stats()
    assert stats == {
        "open_dossiers": 7,
        "unbilled_hours": 12.3,
        "unbilled_amount": 45678,
        "outstanding_invoices": 250075,
    }


def test_quick_stats_degrades_per_stat(monkeypatch):
    def _boom() -> int:
        raise RuntimeError("aggregation failed")

    monkeypatch.setattr(dossier_model, "count_open", _boom)
    monkeypatch.setattr(
        time_entry_model, "get_unbilled_totals",
        lambda: {"hours": 1.5, "amount": 100},
    )
    monkeypatch.setattr(invoice_model, "get_outstanding_total", lambda: 200)

    stats = dashboard._get_quick_stats()
    # The failed stat keeps its safe default; the others still populate.
    assert stats["open_dossiers"] == 0
    assert stats["unbilled_hours"] == 1.5
    assert stats["unbilled_amount"] == 100
    assert stats["outstanding_invoices"] == 200


# ── routes.dashboard._get_urgent_tasks ────────────────────────────────────


def test_urgent_tasks_cutoff_overdue_flags_and_sort(monkeypatch):
    captured: dict = {}

    overdue_old = {"id": "o1", "due_date": NOW - timedelta(days=5)}
    overdue_new = {"id": "o2", "due_date": NOW - timedelta(days=1)}
    future_near = {"id": "f1", "due_date": NOW + timedelta(days=1)}
    future_far = {"id": "f2", "due_date": NOW + timedelta(days=3)}

    def fake_list_urgent_tasks(cutoff: datetime, limit: int = 50) -> list[dict]:
        captured["cutoff"] = cutoff
        # Deliberately shuffled to prove the route re-sorts
        return [future_far, overdue_new, future_near, overdue_old]

    monkeypatch.setattr(task_model, "list_urgent_tasks", fake_list_urgent_tasks)

    result = dashboard._get_urgent_tasks(NOW)
    assert captured["cutoff"] == NOW + timedelta(days=14)
    assert [t["id"] for t in result] == ["o1", "o2", "f1", "f2"]
    assert [t["_overdue"] for t in result] == [True, True, False, False]


def test_urgent_tasks_model_failure_returns_empty(monkeypatch):
    def _boom(cutoff: datetime, limit: int = 50) -> list[dict]:
        raise RuntimeError("query failed")

    monkeypatch.setattr(task_model, "list_urgent_tasks", _boom)
    assert dashboard._get_urgent_tasks(NOW) == []


# ── routes.dashboard._get_urgent_protocol_steps ───────────────────────────


def test_urgent_protocol_steps_overdue_first_sort(monkeypatch):
    captured: dict = {}

    s_overdue = {"id": "s1", "deadline_date": NOW - timedelta(days=2),
                 "_protocol_id": "p1"}
    s_today = {"id": "s2", "deadline_date": NOW + timedelta(hours=4),
               "_protocol_id": "p1"}
    s_later = {"id": "s3", "deadline_date": NOW + timedelta(days=10),
               "_protocol_id": "p1"}

    def fake_list_urgent_steps(cutoff: datetime, limit: int = 50) -> list[dict]:
        captured["cutoff"] = cutoff
        return [s_later, s_overdue, s_today]

    monkeypatch.setattr(protocol_model, "list_urgent_steps", fake_list_urgent_steps)

    result = dashboard._get_urgent_protocol_steps(NOW)
    assert captured["cutoff"] == NOW + timedelta(days=14)
    assert [s["id"] for s in result] == ["s1", "s2", "s3"]
    assert [s["_overdue"] for s in result] == [True, False, False]


# ── routes.dashboard._get_prescription_alerts ─────────────────────────────


def test_prescription_alerts_cutoff_clamp_and_juridical_dates(monkeypatch):
    captured: dict = {}

    # 2026-06-20 is a Saturday → last juridical action day is Friday the 19th
    saturday = datetime(2026, 6, 20, 0, 0, tzinfo=UTC)
    # 2026-06-17 is a Wednesday (juridical) → last action day is itself
    wednesday = datetime(2026, 6, 17, 0, 0, tzinfo=UTC)
    past = NOW - timedelta(days=2)

    d_sat = {"id": "d1", "prescription_date": saturday}
    d_wed = {"id": "d2", "prescription_date": wednesday}
    d_past = {"id": "d3", "prescription_date": past}

    def fake_list_prescription_alerts(cutoff: datetime, limit: int = 50) -> list[dict]:
        captured["cutoff"] = cutoff
        return [d_sat, d_wed, d_past]  # deliberately unsorted

    monkeypatch.setattr(
        dossier_model, "list_prescription_alerts", fake_list_prescription_alerts
    )

    alerts = dashboard._get_prescription_alerts(NOW)
    assert captured["cutoff"] == NOW + timedelta(days=60)
    # Sorted by prescription_date ascending
    assert [a["id"] for a in alerts] == ["d3", "d2", "d1"]

    by_id = {a["id"]: a for a in alerts}
    assert by_id["d3"]["_days_remaining"] == 0  # past dates clamp to 0
    assert by_id["d2"]["_last_action_date"] == date(2026, 6, 17)
    assert by_id["d2"]["_last_action_differs"] is False
    assert by_id["d1"]["_last_action_date"] == date(2026, 6, 19)
    assert by_id["d1"]["_last_action_differs"] is True


# ── routes.dashboard._get_hearings_in_range ───────────────────────────────


def test_hearings_in_range_excludes_cancelled(monkeypatch):
    h_ok = {"id": "h1", "status": "confirmée"}
    h_cancelled = {"id": "h2", "status": "annulée"}

    monkeypatch.setattr(
        hearing_model, "list_hearings_in_range",
        lambda date_from, date_to, limit=100: [h_ok, h_cancelled],
    )

    result = dashboard._get_hearings_in_range(NOW, NOW + timedelta(days=7))
    assert [h["id"] for h in result] == ["h1"]
