"""Per-file invoice numbering (models/invoice.py).

Going forward an invoice number is « {file_number}-NN » — the dossier's file
number and the 2-digit-padded sequence within that file (user decision
2026-07-17). Existing invoices keep their legacy « YYYY-F### » numbers.

Same import-stub approach as test_trust: stub whatever google/firebase lib is
missing on a bare interpreter, and (in the integration fixture) patch
invoice.firestore so @firestore.transactional is an identity decorator —
otherwise the real decorator would drive the fake Transaction and fail.
"""

import importlib
import importlib.util
import sys
import types
from datetime import datetime, timezone
from unittest import mock

import os

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
        "FieldFilter", (), {"__init__": lambda s, field_path=None, op_string=None, value=None, **k: None}
    )
    _stub("google.cloud.firestore_v1.base_query", _bq)
if not _avail("icalendar"):
    _stub("icalendar", types.ModuleType("icalendar"))
if not _avail("firebase_admin"):
    _fa = types.ModuleType("firebase_admin")
    _fa.__path__ = []
    _stub("firebase_admin", _fa)
    _stub("firebase_admin.auth", types.ModuleType("firebase_admin.auth"))


with mock.patch("google.cloud.firestore.Client"):
    import models.invoice as invoice


# ── Pure helpers ───────────────────────────────────────────────────────────


def test_format_invoice_number_2_digit_pad():
    assert invoice._format_invoice_number("2025-001", 1) == "2025-001-01"
    assert invoice._format_invoice_number("2025-001", 3) == "2025-001-03"
    assert invoice._format_invoice_number("2025-001", 10) == "2025-001-10"
    assert invoice._format_invoice_number("2025-001", 100) == "2025-001-100"  # rolls to 3


def test_seed_counts_all_invoices_in_the_file():
    assert invoice._seed_invoice_seq([], "2025-001") == 0
    legacy = [{"invoice_number": "2025-F007"}, {"invoice_number": "2025-F012"}]
    assert invoice._seed_invoice_seq(legacy, "2025-001") == 2  # next will be -03


def test_seed_is_deletion_safe():
    # -02 was deleted: only two rows remain but -03 exists → seed 3, not 2,
    # so the next number can never collide with the surviving -03.
    mixed = [{"invoice_number": "2025-001-01"}, {"invoice_number": "2025-001-03"}]
    assert invoice._seed_invoice_seq(mixed, "2025-001") == 3


def test_seed_parses_padded_suffix():
    assert invoice._seed_invoice_seq([{"invoice_number": "2025-001-09"}], "2025-001") == 9


def test_seed_ignores_other_dossiers_suffixes():
    # a different file's number must not bleed into this file's max-suffix
    other = [{"invoice_number": "2025-002-05"}]
    assert invoice._seed_invoice_seq(other, "2025-001") == 1  # counts 1 row, suffix 0


# ── Integration: the transactional generator over a fake Firestore ─────────


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Query:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._filters = []

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def stream(self, transaction=None):
        rows = list(self._store.get(self._coll, {}).values())
        for fp, _op, val in self._filters:
            rows = [d for d in rows if d.get(fp) == val]
        return [_Snap(d.get("id"), d) for d in rows]


class _DocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self, transaction=None):
        return _Snap(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = dict(data)


class _Coll(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, self._coll, doc_id)


class _Txn:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data):
        ref.set(data)


class _DB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Coll(self._store, name)

    def transaction(self):
        return _Txn(self._store)


class _FS:
    transactional = staticmethod(lambda fn: fn)
    Transaction = object

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


class _FF:
    def __init__(self, field_path=None, op_string=None, value=None, **_k):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


@pytest.fixture
def store(monkeypatch):
    s = {"invoices": {}, "counters": {}}
    monkeypatch.setattr(invoice, "db", _DB(s))
    monkeypatch.setattr(invoice, "firestore", _FS)
    monkeypatch.setattr(invoice, "FieldFilter", _FF)
    import models.dossier as dossier_model

    files = {"dosA": "2025-001", "dosB": "2025-002", "dosEmpty": ""}
    monkeypatch.setattr(
        dossier_model, "get_dossier",
        lambda did: {"id": did, "file_number": files.get(did, "2025-999")},
    )
    return s


def test_per_file_sequence_is_monotonic_and_independent(store):
    assert invoice._generate_invoice_number("dosA") == "2025-001-01"
    assert invoice._generate_invoice_number("dosA") == "2025-001-02"
    # a different file gets its own sequence, starting at 01
    assert invoice._generate_invoice_number("dosB") == "2025-002-01"
    assert invoice._generate_invoice_number("dosA") == "2025-001-03"


def test_sequence_seeds_from_existing_invoices(store):
    store["invoices"]["i1"] = {"id": "i1", "dossier_id": "dosA", "invoice_number": "2025-F007"}
    store["invoices"]["i2"] = {"id": "i2", "dossier_id": "dosA", "invoice_number": "2025-F012"}
    # two invoices already in the file → next is the 3rd
    assert invoice._generate_invoice_number("dosA") == "2025-001-03"


def test_empty_file_number_falls_back_to_year_scheme(store):
    number = invoice._generate_invoice_number("dosEmpty")
    year = datetime.now(timezone.utc).strftime("%Y")
    assert number.startswith(f"{year}-F")
