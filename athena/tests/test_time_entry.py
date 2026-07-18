"""Time-entry billable gating (models/time_entry.py).

Unbillable time carries no calculated cost: when 'Facturable' is off, the
stored ``amount`` is forced to 0 regardless of hours × rate. This keeps the
list totals, exports and on-screen display consistent, and — with the
``billable == True`` filter on ``get_unbilled_totals`` — keeps unbillable
time out of the dashboard's unbilled tracker.

Same import-stub approach as test_invoice_numbering / test_trust: stub
whatever google/firebase lib is missing on a bare interpreter, then drive
``create_time_entry`` / ``update_time_entry`` over a tiny in-memory fake
Firestore.
"""

import importlib
import importlib.util
import os
import sys
import types
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _avail(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _stub(name, module):
    parts = name.split(".")
    for i in range(1, len(parts)):
        pkg = ".".join(parts[:i])
        if pkg not in sys.modules:
            if _avail(pkg):
                importlib.import_module(pkg)
                continue
            m = types.ModuleType(pkg)
            m.__path__ = []
            sys.modules[pkg] = m
            if i > 1:
                setattr(sys.modules[".".join(parts[: i - 1])], parts[i - 1], m)
    sys.modules[name] = module
    if len(parts) > 1:
        setattr(sys.modules[".".join(parts[:-1])], parts[-1], module)


if not _avail("google.cloud.firestore"):
    _fs = types.ModuleType("google.cloud.firestore")
    _fs.Client = mock.MagicMock(name="firestore.Client")
    _fs.Query = type("Query", (), {"ASCENDING": "ASCENDING", "DESCENDING": "DESCENDING"})
    _fs.Transaction = type("Transaction", (), {})
    _fs.transactional = lambda fn: fn
    _stub("google.cloud.firestore", _fs)
if not _avail("google.cloud.firestore_v1.base_query"):
    _bq = types.ModuleType("google.cloud.firestore_v1.base_query")
    _bq.FieldFilter = type(
        "FieldFilter",
        (),
        {"__init__": lambda s, field_path=None, op_string=None, value=None, **k: None},
    )
    _stub("google.cloud.firestore_v1.base_query", _bq)
if not _avail("firebase_admin"):
    _fa = types.ModuleType("firebase_admin")
    _fa.__path__ = []
    _stub("firebase_admin", _fa)


with mock.patch("google.cloud.firestore.Client"):
    import models.time_entry as time_entry


# ── Pure helper: _compute_entry_amount ─────────────────────────────────────


def test_billable_amount_is_hours_times_rate():
    assert time_entry._compute_entry_amount(2.0, 25000, True) == 50000
    assert time_entry._compute_entry_amount(0.5, 30000, True) == 15000


def test_unbillable_amount_is_zero_regardless_of_hours_rate():
    assert time_entry._compute_entry_amount(2.0, 25000, False) == 0
    assert time_entry._compute_entry_amount(100.0, 99999, False) == 0


def test_billable_amount_guards_non_finite():
    assert time_entry._compute_entry_amount(float("inf"), 25000, True) == 0
    assert time_entry._compute_entry_amount(float("nan"), 25000, True) == 0


# ── Integration over a fake Firestore ──────────────────────────────────────


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self):
        return _Snap(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = dict(data)


class _Coll:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll

    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Coll(self._store, name)


@pytest.fixture
def store(monkeypatch):
    s = {"timeentries": {}}
    monkeypatch.setattr(time_entry, "db", _DB(s))
    return s


def _valid_data(**overrides):
    data = {
        "dossier_id": "d1",
        "date": datetime(2026, 7, 18, tzinfo=timezone.utc),
        "description": "Rédaction",
        "hours": 2.0,
        "rate": 25000,
        "billable": True,
    }
    data.update(overrides)
    return data


def test_create_billable_stores_computed_amount(store):
    entry, errors = time_entry.create_time_entry(_valid_data(billable=True))
    assert errors == []
    assert entry["amount"] == 50000
    # …and it's what got persisted
    stored = next(iter(store["timeentries"].values()))
    assert stored["amount"] == 50000


def test_create_unbillable_stores_zero_amount(store):
    entry, errors = time_entry.create_time_entry(_valid_data(billable=False))
    assert errors == []
    assert entry["billable"] is False
    assert entry["amount"] == 0
    stored = next(iter(store["timeentries"].values()))
    assert stored["amount"] == 0


def test_update_flip_to_unbillable_zeros_amount(store):
    entry, _ = time_entry.create_time_entry(_valid_data(billable=True))
    assert entry["amount"] == 50000

    updated, errors = time_entry.update_time_entry(entry["id"], _valid_data(billable=False))
    assert errors == []
    assert updated["amount"] == 0


def test_update_flip_back_to_billable_recomputes_amount(store):
    entry, _ = time_entry.create_time_entry(_valid_data(billable=False))
    assert entry["amount"] == 0

    updated, errors = time_entry.update_time_entry(entry["id"], _valid_data(billable=True))
    assert errors == []
    assert updated["amount"] == 50000
