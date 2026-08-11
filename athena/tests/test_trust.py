"""Tests for models/trust.py — Phase K trust accounting.

The pure functions (spec §6.1) carry the suite; no Firestore is needed. The
balance arithmetic (§4.4), the overdraft control (§4.3), the reconciliation
variance (§3.3) and the Barreau-column projection (§8) are exercised here.
Firestore-transaction guards (create/clear/reverse/reconcile) are covered by
db-faked tests added alongside those functions.

Imports are stubbed the same way as test_dashboard_aggregation: the canonical
CI venv has google-cloud-firestore / firebase-admin / icalendar installed; a
bare local interpreter may not, so whatever is missing is stubbed and the
Firestore client constructor is patched (models/__init__ builds it at import).
The stubs are inert — these tests never touch the client.
"""

import copy
import importlib
import importlib.util
import os
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Conditional third-party stubs ─────────────────────────────────────────
def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _install_stub(name: str, module: types.ModuleType) -> None:
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
        def __init__(self, field_path, op_string, value=None):
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
    _install_stub("firebase_admin", _fa_stub)
    _install_stub("firebase_admin.auth", types.ModuleType("firebase_admin.auth"))


with mock.patch("google.cloud.firestore.Client"):
    import models.trust as trust


X = 100_00  # $100.00, the § reference amount, in cents


# ═══════════════════════════════════════════════════════════════════════════
# Balance arithmetic — compute_deltas / the §4.4 table
# ═══════════════════════════════════════════════════════════════════════════


def _c(direction, status, amount=X):
    return trust.compute_deltas(direction, amount, status)


def test_compute_deltas_per_entry_contributions():
    """The six (direction, status) contributions the §4.4 table is built from."""
    assert _c("recette", "en_circulation") == {"book": X, "cleared": 0, "bank": 0}
    assert _c("recette", "compensée") == {"book": X, "cleared": X, "bank": X}
    assert _c("recette", "annulée") == {"book": X, "cleared": 0, "bank": 0}
    assert _c("déboursé", "en_circulation") == {"book": -X, "cleared": -X, "bank": 0}
    assert _c("déboursé", "compensée") == {"book": -X, "cleared": -X, "bank": -X}
    assert _c("déboursé", "annulée") == {"book": -X, "cleared": 0, "bank": 0}


def _delta(a, b):
    """new contribution minus old contribution, per key."""
    return {k: a[k] - b[k] for k in a}


def _sum(*deltas):
    out = {"book": 0, "cleared": 0, "bank": 0}
    for d in deltas:
        for k in out:
            out[k] += d[k]
    return out


def test_section_4_4_table_row1_create_receipt():
    assert _c("recette", "en_circulation") == {"book": X, "cleared": 0, "bank": 0}


def test_section_4_4_table_row2_create_disbursement():
    assert _c("déboursé", "en_circulation") == {"book": -X, "cleared": -X, "bank": 0}


def test_section_4_4_table_row3_clear_receipt():
    # status change en_circulation → compensée
    delta = _delta(_c("recette", "compensée"), _c("recette", "en_circulation"))
    assert delta == {"book": 0, "cleared": X, "bank": X}


def test_section_4_4_table_row4_clear_disbursement():
    delta = _delta(_c("déboursé", "compensée"), _c("déboursé", "en_circulation"))
    assert delta == {"book": 0, "cleared": 0, "bank": -X}


def test_section_4_4_table_row5_annul_encirc_receipt_pair():
    # original en_circ → annulée, plus a new annulée déboursé reversal.
    original = _delta(_c("recette", "annulée"), _c("recette", "en_circulation"))
    reversal = _c("déboursé", "annulée")
    pair = _sum(original, reversal)
    # « 0 (nets) » in the table = the pair's net BOOK contribution is zero.
    net_book = _c("recette", "annulée")["book"] + _c("déboursé", "annulée")["book"]
    assert net_book == 0
    assert pair["cleared"] == 0
    assert pair["bank"] == 0


def test_section_4_4_table_row6_annul_encirc_disbursement_pair():
    original = _delta(_c("déboursé", "annulée"), _c("déboursé", "en_circulation"))
    reversal = _c("recette", "annulée")
    pair = _sum(original, reversal)
    net_book = _c("déboursé", "annulée")["book"] + _c("recette", "annulée")["book"]
    assert net_book == 0
    assert pair["cleared"] == X  # the +X that restores the committed funds
    assert pair["bank"] == 0


def test_section_4_4_table_row7_reverse_compensee_receipt():
    # the reversal is a new déboursé starting en_circulation.
    assert _c("déboursé", "en_circulation") == {"book": -X, "cleared": -X, "bank": 0}


def test_section_4_4_table_row8_reverse_compensee_disbursement():
    # the reversal is a new recette starting en_circulation.
    assert _c("recette", "en_circulation") == {"book": X, "cleared": 0, "bank": 0}


def test_annul_disbursement_pair_net_cleared_effect_is_zero():
    """create disbursement (cleared −X) then annul it (cleared +X) → net 0."""
    create_cleared = _c("déboursé", "en_circulation")["cleared"]
    annul_original = _delta(_c("déboursé", "annulée"), _c("déboursé", "en_circulation"))
    annul_reversal = _c("recette", "annulée")
    annul_cleared = annul_original["cleared"] + annul_reversal["cleared"]
    assert create_cleared == -X
    assert annul_cleared == X
    assert create_cleared + annul_cleared == 0


def test_reverse_compensee_receipt_takes_cleared_immediately():
    """A bounced deposit: reversing a compensée receipt yields an
    en_circulation disbursement that removes cleared funds at once."""
    reversal = _c("déboursé", "en_circulation")
    assert reversal["cleared"] == -X


def test_book_includes_annulee_cleared_excludes_it():
    assert _c("recette", "annulée")["book"] == X
    assert _c("recette", "annulée")["cleared"] == 0
    assert _c("déboursé", "annulée")["book"] == -X
    assert _c("déboursé", "annulée")["cleared"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# The control — check_disbursement_allowed (§4.3)
# ═══════════════════════════════════════════════════════════════════════════


def test_control_refuses_disbursement_over_cleared():
    ok, reason = trust.check_disbursement_allowed(0, 1)
    assert ok is False
    assert reason == "solde_compensé_insuffisant"


def test_control_allows_exact_zero():
    ok, reason = trust.check_disbursement_allowed(10000, 10000)
    assert ok is True
    assert reason == ""


def test_control_refuses_one_cent_over():
    ok, _ = trust.check_disbursement_allowed(10000, 10001)
    assert ok is False


def test_control_deposit_in_transit_case():
    # cleared 0 while book is +5000 (uncleared deposit) → a 1¢ déboursé refused.
    ok, _ = trust.check_disbursement_allowed(0, 1)
    assert ok is False


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation variance (§3.3)
# ═══════════════════════════════════════════════════════════════════════════


def test_variance_balanced():
    assert trust.reconciliation_variance(10000, 10000, 0, 0) == 0


def test_variance_outstanding_cheque_reconciles():
    # book already down for the cheque; statement still high; outstanding closes it
    assert trust.reconciliation_variance(12000, 10000, 2000, 0) == 0


def test_variance_deposit_in_transit_reconciles():
    assert trust.reconciliation_variance(8000, 10000, 0, 2000) == 0


def test_variance_signed_statement_exceeds_book():
    assert trust.reconciliation_variance(12000, 10000, 0, 0) == 2000


def test_variance_signed_statement_below_book():
    assert trust.reconciliation_variance(8000, 10000, 0, 0) == -2000


# ═══════════════════════════════════════════════════════════════════════════
# Exports — to_barreau_row (§8) + the two-column shape (user decision)
# ═══════════════════════════════════════════════════════════════════════════

_D = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def _tx(**over):
    base = {
        "date": _D,
        "dossier_file_number": "2026-001",
        "counterparty": "Banque Nationale",
        "client_name": "Jean Tremblay",
        "purpose": "dépôt_client",
        "method": "chèque",
        "direction": "recette",
        "amount": 500000,
        "status": "en_circulation",
        "balance_after_account": 900000,
        "balance_after_client": 500000,
    }
    base.update(over)
    return base


def test_barreau_columns_exact_order_and_headers():
    keys = [k for k, _ in trust.BARREAU_COLUMNS]
    labels = [label for _, label in trust.BARREAU_COLUMNS]
    assert keys == [
        "date", "n_ref", "counterparty", "client",
        "objet", "mode", "recette", "credit", "solde",
    ]
    assert labels == [
        "Date",
        "N/Réf",
        "Somme reçue de / Bénéficiaire du débours",
        "Client pour qui la somme est reçue ou le débours est effectué",
        "Objet de la recette ou du débours",
        "Mode du retrait",
        "Recette",
        "Crédit",
        "Solde",
    ]


def test_to_barreau_row_emits_columns_in_order():
    row = trust.to_barreau_row(_tx(), "journal")
    assert list(row.keys()) == [k for k, _ in trust.BARREAU_COLUMNS]


def test_to_barreau_row_view_journal_uses_account_balance():
    row = trust.to_barreau_row(_tx(), "journal")
    assert row["solde"] == 900000


def test_to_barreau_row_view_carte_uses_client_balance():
    row = trust.to_barreau_row(_tx(), "carte")
    assert row["solde"] == 500000


def test_to_barreau_row_recette_populates_recette_column():
    row = trust.to_barreau_row(_tx(direction="recette", amount=500000), "journal")
    assert row["recette"] == 500000
    assert row["credit"] is None


def test_to_barreau_row_deboursé_populates_credit_column():
    row = trust.to_barreau_row(
        _tx(direction="déboursé", amount=250000, purpose="déboursé_tiers"), "journal"
    )
    assert row["credit"] == 250000
    assert row["recette"] is None


def test_to_barreau_row_labels_purpose_and_method():
    row = trust.to_barreau_row(_tx(purpose="virement_honoraires", method="virement"), "journal")
    assert row["objet"] == "Virement d'honoraires"
    assert row["mode"] == "Virement"


def test_to_barreau_row_annulee_suffix_on_objet():
    row = trust.to_barreau_row(_tx(status="annulée", purpose="dépôt_client"), "journal")
    assert row["objet"] == "Dépôt du client (annulée)"


def test_to_barreau_row_passes_date_through_raw():
    row = trust.to_barreau_row(_tx(), "journal")
    assert row["date"] == _D


# ═══════════════════════════════════════════════════════════════════════════
# recompute_running_balances (§13 verification helper)
# ═══════════════════════════════════════════════════════════════════════════


def test_recompute_running_balances_book():
    txs = [
        _tx(direction="recette", amount=500000, status="compensée"),
        _tx(direction="déboursé", amount=200000, status="en_circulation"),
        _tx(direction="recette", amount=100000, status="en_circulation"),
    ]
    assert trust.recompute_running_balances(txs, "journal") == [500000, 300000, 400000]


def test_recompute_running_balances_counts_annulee_in_book():
    # an annulée receipt still contributes to the book running balance until
    # its reversal removes it (register is chronological).
    txs = [
        _tx(direction="recette", amount=500000, status="annulée"),
        _tx(direction="déboursé", amount=500000, status="annulée"),  # its reversal
    ]
    assert trust.recompute_running_balances(txs, "journal") == [500000, 0]


# ═══════════════════════════════════════════════════════════════════════════
# Firestore-transaction tests — a tiny in-memory Firestore fake drives the
# real create/clear/reverse/transfer/reconcile logic (spec §13). The stub
# preamble makes @firestore.transactional a no-op, so the transactional body
# runs directly against the fake. No concurrency is modelled — only logic.
# ═══════════════════════════════════════════════════════════════════════════


class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, coll, doc_id):
        self._store = store
        self._coll = coll
        self.id = doc_id

    def get(self, transaction=None):
        return _FakeSnapshot(self.id, self._store.get(self._coll, {}).get(self.id))

    def set(self, data):
        self._store.setdefault(self._coll, {})[self.id] = copy.deepcopy(data)

    def update(self, fields):
        doc = self._store.setdefault(self._coll, {}).get(self.id)
        if doc is None:
            raise KeyError(f"update on missing {self._coll}/{self.id}")
        doc.update(copy.deepcopy(fields))

    def delete(self):
        self._store.setdefault(self._coll, {}).pop(self.id, None)


class _FakeQuery:
    def __init__(self, store, coll):
        self._store = store
        self._coll = coll
        self._filters = []
        self._orders = []
        self._limit = None
        self._start_after = None

    def where(self, filter=None):
        self._filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def order_by(self, field, direction="ASCENDING"):
        self._orders.append((field, direction))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def start_after(self, values):
        self._start_after = values
        return self

    def _match(self, doc):
        for fp, op, val in self._filters:
            if op == "==" and doc.get(fp) != val:
                return False
        return True

    def _rows(self):
        rows = [d for d in self._store.get(self._coll, {}).values() if self._match(d)]
        for field, direction in reversed(self._orders):
            rows.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction == "DESCENDING"),
            )
        if self._start_after is not None and self._orders:
            field, direction = self._orders[0]
            cur = self._start_after[field]
            if direction == "DESCENDING":
                rows = [d for d in rows if d.get(field) < cur]
            else:
                rows = [d for d in rows if d.get(field) > cur]
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def stream(self, transaction=None):
        return [_FakeSnapshot(d.get("id"), d) for d in self._rows()]

    def get(self, transaction=None):
        return self.stream(transaction=transaction)


class _FakeCollectionRef(_FakeQuery):
    def document(self, doc_id):
        return _FakeDocRef(self._store, self._coll, doc_id)


class _FakeTransaction:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data):
        ref.set(data)

    def update(self, ref, fields):
        ref.update(fields)

    def delete(self, ref):
        ref.delete()


class _FakeDB:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCollectionRef(self._store, name)

    def transaction(self):
        return _FakeTransaction(self._store)


class _TestFieldFilter:
    """Stand-in for google FieldFilter so _FakeQuery can read its parts —
    independent of the real lib's internal attribute names."""

    def __init__(self, field_path=None, op_string=None, value=None, **_kw):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


class _FakeFirestore:
    """Stand-in for the ``firestore`` module used inside models/trust.py.

    Critically, ``@firestore.transactional`` becomes an IDENTITY decorator so
    the transactional body runs directly against ``_FakeTransaction``. With the
    REAL decorator (CI, where google-cloud-firestore is installed) it would try
    to drive the fake through the real begin/commit protocol and AttributeError
    — the false-positive the local stub hid.
    """

    transactional = staticmethod(lambda fn: fn)
    Transaction = object

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


def _base_store():
    return {
        "trust_accounts": {
            "acc1": {
                "id": "acc1", "name": "Général", "status": "actif",
                "account_type": "général", "book_balance": 0, "bank_balance": 0,
            }
        },
        "counters": {},
        "dossiers": {
            "dos1": {
                "id": "dos1", "file_number": "2026-001", "title": "Tremblay c. X",
                "client_ids": ["c1"], "clients": [{"id": "c1", "name": "Jean Tremblay"}],
                "trust_balance": 0, "trust_balance_by_client": {},
                "trust_cleared_by_client": {},
            }
        },
        "trust_transactions": {},
        "invoices": {},
        "trust_reconciliations": {},
    }


@pytest.fixture
def store(monkeypatch):
    s = _base_store()
    monkeypatch.setattr(trust, "db", _FakeDB(s))
    # Decouple the transaction tests from whether google-cloud-firestore is
    # real (CI) or stubbed (bare local): identity transactional + a Query with
    # directions + a plain FieldFilter the fake query can introspect.
    monkeypatch.setattr(trust, "firestore", _FakeFirestore)
    monkeypatch.setattr(trust, "FieldFilter", _TestFieldFilter)
    return s


def _new(**over):
    d = {
        "account_id": "acc1", "direction": "recette", "amount": 100000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1",
        "date": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


# ── create_transaction: happy paths + the balance wiring ───────────────────


def test_create_receipt_updates_book_not_cleared(store):
    entry, errs = trust.create_transaction(_new(direction="recette", amount=100000))
    assert errs == []
    assert entry["status"] == "en_circulation"
    assert entry["sequence"] == 1
    assert entry["balance_after_account"] == 100000
    assert entry["balance_after_client"] == 100000
    assert store["trust_accounts"]["acc1"]["book_balance"] == 100000
    dos = store["dossiers"]["dos1"]
    assert dos["trust_balance_by_client"]["c1"] == 100000
    assert dos["trust_cleared_by_client"]["c1"] == 0  # a receipt is not cleared
    assert dos["trust_balance"] == 100000


def test_full_lifecycle_receipt_clear_then_disburse(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000))
    _, errs = trust.clear_transaction(r["id"], datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert errs == []
    dos = store["dossiers"]["dos1"]
    assert dos["trust_cleared_by_client"]["c1"] == 100000
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 100000
    d, errs = trust.create_transaction(
        _new(direction="déboursé", amount=100000, purpose="déboursé_tiers",
             date=datetime(2026, 7, 3, tzinfo=timezone.utc))
    )
    assert errs == []
    assert dos["trust_cleared_by_client"]["c1"] == 0
    assert store["trust_accounts"]["acc1"]["book_balance"] == 0


# ── create_transaction: the control + validation guards (§13) ──────────────


def test_create_disbursement_over_cleared_refused(store):
    _, errs = trust.create_transaction(
        _new(direction="déboursé", amount=50000, purpose="déboursé_tiers")
    )
    assert errs and "compensé" in errs[0].lower()
    assert store["trust_transactions"] == {}
    assert store["trust_accounts"]["acc1"]["book_balance"] == 0


def test_create_client_not_on_dossier_refused(store):
    _, errs = trust.create_transaction(_new(client_id="c9"))
    assert errs and "client" in errs[0].lower()


def test_create_amount_nonpositive_refused(store):
    assert trust.create_transaction(_new(amount=0))[1]
    assert trust.create_transaction(_new(amount=-5))[1]


def test_create_backdating_refused(store):
    trust.create_transaction(_new(date=datetime(2026, 7, 10, tzinfo=timezone.utc)))
    _, errs = trust.create_transaction(_new(date=datetime(2026, 7, 5, tzinfo=timezone.utc)))
    assert errs and "antérieure" in errs[0].lower()


def test_create_purpose_correction_refused(store):
    assert trust.create_transaction(_new(purpose="correction"))[1]


def test_create_no_dossier_requires_bank_purpose(store):
    assert trust.create_transaction(
        _new(dossier_id=None, client_id=None, purpose="avance_honoraires")
    )[1]
    e, errs = trust.create_transaction(
        _new(dossier_id=None, client_id=None, purpose="intérêts",
             direction="recette", counterparty="Banque")
    )
    assert errs == []
    assert e["dossier_id"] is None


def test_create_on_closed_account_refused(store):
    store["trust_accounts"]["acc1"]["status"] = "fermé"
    assert trust.create_transaction(_new())[1]


def _fund_cleared(store, amount=100000, day=2):
    r, _ = trust.create_transaction(_new(direction="recette", amount=amount))
    trust.clear_transaction(r["id"], datetime(2026, 7, day, tzinfo=timezone.utc))
    return r


def test_virement_honoraires_exceeds_invoice_refused(store):
    store["invoices"]["inv1"] = {
        "id": "inv1", "status": "envoyée", "dossier_id": "dos1", "amount_due": 50000,
    }
    _fund_cleared(store)
    _, errs = trust.create_transaction(
        _new(direction="déboursé", purpose="virement_honoraires", invoice_id="inv1",
             amount=60000, date=datetime(2026, 7, 3, tzinfo=timezone.utc))
    )
    assert errs and "solde dû" in errs[0].lower()


def test_virement_honoraires_on_draft_invoice_refused(store):
    store["invoices"]["inv1"] = {
        "id": "inv1", "status": "brouillon", "dossier_id": "dos1", "amount_due": 100000,
    }
    _fund_cleared(store)
    _, errs = trust.create_transaction(
        _new(direction="déboursé", purpose="virement_honoraires", invoice_id="inv1",
             amount=50000, date=datetime(2026, 7, 3, tzinfo=timezone.utc))
    )
    assert errs and "émise" in errs[0].lower()


# ── reverse_transaction (§13) ──────────────────────────────────────────────


def test_reverse_en_circulation_both_annulee(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000))
    rev, errs = trust.reverse_transaction(r["id"], "erreur de saisie")
    assert errs == []
    assert store["trust_transactions"][r["id"]]["status"] == "annulée"
    assert rev["status"] == "annulée"
    assert rev["purpose"] == "correction"
    assert rev["direction"] == "déboursé"
    assert rev["reverses_id"] == r["id"]
    assert store["trust_transactions"][r["id"]]["reversed_by_id"] == rev["id"]
    assert store["trust_accounts"]["acc1"]["book_balance"] == 0


def test_reverse_compensee_creates_en_circulation(store):
    r = _fund_cleared(store)
    rev, errs = trust.reverse_transaction(r["id"], "chèque sans provision")
    assert errs == []
    assert store["trust_transactions"][r["id"]]["status"] == "compensée"  # unchanged
    assert rev["status"] == "en_circulation"
    assert rev["direction"] == "déboursé"
    # bounced deposit removes the cleared funds immediately
    assert store["dossiers"]["dos1"]["trust_cleared_by_client"]["c1"] == 0


def test_double_reversal_refused(store):
    r, _ = trust.create_transaction(_new())
    trust.reverse_transaction(r["id"], "x")
    _, errs = trust.reverse_transaction(r["id"], "y")
    assert errs and "contre-passée" in errs[0].lower()


def test_reverse_requires_reason(store):
    r, _ = trust.create_transaction(_new())
    assert trust.reverse_transaction(r["id"], "   ")[1]


def test_reversal_uses_today_not_original_date(store):
    r, _ = trust.create_transaction(_new(date=datetime(2026, 7, 1, tzinfo=timezone.utc)))
    rev, _ = trust.reverse_transaction(r["id"], "x")
    assert trust._as_utc(rev["date"]).date() == datetime.now(timezone.utc).date()


# ── clearing (§13) ─────────────────────────────────────────────────────────


def test_clear_before_date_refused(store):
    r, _ = trust.create_transaction(_new(date=datetime(2026, 7, 10, tzinfo=timezone.utc)))
    assert trust.clear_transaction(r["id"], datetime(2026, 7, 5, tzinfo=timezone.utc))[1]


def test_clear_future_refused(store):
    r, _ = trust.create_transaction(_new())
    assert trust.clear_transaction(r["id"], datetime.now(timezone.utc) + timedelta(days=5))[1]


def test_clear_already_compensee_refused(store):
    r, _ = trust.create_transaction(_new())
    trust.clear_transaction(r["id"], datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert trust.clear_transaction(r["id"], datetime(2026, 7, 3, tzinfo=timezone.utc))[1]


def test_bulk_clear_all_or_nothing(store):
    r1, _ = trust.create_transaction(_new(amount=100000, date=datetime(2026, 7, 1, tzinfo=timezone.utc)))
    r2, _ = trust.create_transaction(_new(amount=50000, date=datetime(2026, 7, 2, tzinfo=timezone.utc)))
    trust.clear_transaction(r2["id"], datetime(2026, 7, 3, tzinfo=timezone.utc))
    count, failed = trust.clear_transactions_bulk(
        [r1["id"], r2["id"]], datetime(2026, 7, 4, tzinfo=timezone.utc)
    )
    assert count == 0
    assert r2["id"] in failed
    assert store["trust_transactions"][r1["id"]]["status"] == "en_circulation"


# ── inter-dossier transfer (§6.4) ──────────────────────────────────────────


def _add_dos2(store):
    store["dossiers"]["dos2"] = {
        "id": "dos2", "file_number": "2026-002", "title": "Autre",
        "client_ids": ["c2"], "clients": [{"id": "c2", "name": "Marie Roy"}],
        "trust_balance": 0, "trust_balance_by_client": {}, "trust_cleared_by_client": {},
    }


def test_inter_dossier_transfer(store):
    _add_dos2(store)
    _fund_cleared(store, amount=100000)
    leg, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 40000, "virement", "virement", ""
    )
    assert errs == []
    assert store["dossiers"]["dos1"]["trust_balance_by_client"]["c1"] == 60000
    assert store["dossiers"]["dos1"]["trust_cleared_by_client"]["c1"] == 60000
    assert store["dossiers"]["dos2"]["trust_balance_by_client"]["c2"] == 40000
    assert store["dossiers"]["dos2"]["trust_cleared_by_client"]["c2"] == 40000
    # funds stay in the account: net book unchanged
    assert store["trust_accounts"]["acc1"]["book_balance"] == 100000


def test_inter_dossier_transfer_insufficient_cleared(store):
    _add_dos2(store)
    trust.create_transaction(_new(direction="recette", amount=100000))  # NOT cleared
    _, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos2", "c2", 40000, "x", "virement", ""
    )
    assert errs and "compensé" in errs[0].lower()


def test_inter_dossier_transfer_same_couple_refused(store):
    _, errs = trust.create_inter_dossier_transfer(
        "acc1", "dos1", "c1", "dos1", "c1", 10000, "x", "virement", ""
    )
    assert errs


# ── reconciliation (§13) ───────────────────────────────────────────────────
# NOTE: relative dates throughout — the anti-future guard on period_end
# refuses any hardcoded date the calendar eventually overtakes.


def _day(n: int) -> datetime:
    """n days AGO, tz-aware UTC (period_ends and entry dates for retro tests)."""
    return datetime.now(timezone.utc) - timedelta(days=n)


def test_complete_reconciliation_variance_refused(store):
    trust.create_transaction(_new(direction="recette", amount=100000))  # in transit
    rec, _ = trust.create_reconciliation("acc1", _day(1), statement_balance=500)
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs
    assert store["trust_reconciliations"][rec["id"]]["status"] == "brouillon"


def test_complete_reconciliation_balanced(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000))
    rec, _ = trust.create_reconciliation("acc1", _day(1), statement_balance=100000)
    result, errs = trust.complete_reconciliation(rec["id"], [r["id"]])
    assert errs == []
    assert result["status"] == "complétée"
    assert result["variance"] == 0
    entry = store["trust_transactions"][r["id"]]
    assert entry["status"] == "compensée"
    assert entry["reconciliation_id"] == rec["id"]
    # bank_balance is INCREMENTED by the ticked deltas (0 + 100000) — the
    # regression pin that the += semantics keeps the current-period behavior.
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 100000


def test_create_reconciliation_one_brouillon_per_account(store):
    trust.create_reconciliation("acc1", _day(2), 0)
    _, errs = trust.create_reconciliation("acc1", _day(1), 0)
    assert errs


# ═══════════════════════════════════════════════════════════════════════════
# Retroactive (as-of) reconciliation — every figure is anchored to period_end,
# never to now. The old completion compared a September statement against
# TODAY's book/bank balances, so a retro reconciliation could never balance
# (and, a brouillon being neither editable nor deletable, permanently blocked
# the account). These tests pin the as-of reconstruction, the resurrection
# sets, the cross-period outstanding cheque, and the abandon escape hatch.
# ═══════════════════════════════════════════════════════════════════════════


# ── book_balance_as_of ──────────────────────────────────────────────────────


def test_book_as_of_empty_register_is_zero(store):
    assert trust.book_balance_as_of("acc1", _day(10)) == 0


def test_book_as_of_picks_last_by_sequence_same_day(store):
    """Two entries the same day: the frozen balance of the HIGHER sequence is
    the end-of-day figure (dates are non-decreasing in sequence order, so the
    sequence disambiguates same-day entries)."""
    trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.create_transaction(_new(direction="recette", amount=50000, date=_day(40)))
    assert trust.book_balance_as_of("acc1", _day(40)) == 150000
    assert trust.book_balance_as_of("acc1", _day(41)) == 0


def test_book_as_of_ignores_later_entries(store):
    trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.create_transaction(_new(direction="recette", amount=50000, date=_day(5)))
    assert trust.book_balance_as_of("acc1", _day(30)) == 100000


# ── retro completion — the production shape and the settled requirements ───


def test_retro_verification_only_completion(store):
    """The production data shape: everything already cleared by hand with
    accurate cleared_dates, later activity after the statement date. A retro
    reconciliation is then a pure VERIFICATION pass — zero ticks, variance 0.
    Under the old now-anchored arithmetic this compared the old statement to
    today's balances and could never balance."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(r["id"], _day(40))
    d, _ = trust.create_transaction(
        _new(direction="déboursé", amount=30000, purpose="déboursé_tiers", date=_day(10))
    )
    trust.clear_transaction(d["id"], _day(10))

    rec, errs = trust.create_reconciliation("acc1", _day(30), statement_balance=100000)
    assert errs == []
    result, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == []
    assert result["status"] == "complétée" and result["variance"] == 0
    assert result["cleared_transaction_ids"] == []
    # No tick → the current bank balance is untouched (100000 − 30000).
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 70000


def test_cross_period_outstanding_cheque(store):
    """Settled requirement #2: a cheque left unticked in period N counts as
    outstanding-as-of in N's variance, STAYS en_circulation, and is cleared by
    period N+1's reconciliation — across statements."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(r["id"], _day(39))
    cheque, _ = trust.create_transaction(
        _new(direction="déboursé", amount=20000, purpose="déboursé_tiers", date=_day(35))
    )

    # Period 1: the cheque has not cleared the bank — left unticked.
    rec1, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=100000)
    result1, errs = trust.complete_reconciliation(rec1["id"], [])
    assert errs == [] and result1["variance"] == 0
    assert result1["outstanding_cheques_total"] == 20000
    assert store["trust_transactions"][cheque["id"]]["status"] == "en_circulation"

    # Period 2: the cheque appears on THIS statement — ticked.
    rec2, _ = trust.create_reconciliation("acc1", _day(1), statement_balance=80000)
    result2, errs = trust.complete_reconciliation(rec2["id"], [cheque["id"]])
    assert errs == [] and result2["variance"] == 0
    entry = store["trust_transactions"][cheque["id"]]
    assert entry["status"] == "compensée"
    assert entry["reconciliation_id"] == rec2["id"]
    assert trust._as_utc(entry["cleared_date"]).date() == _day(1).date()
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 80000


def test_resurrection_cleared_after_period(store):
    """An entry manually cleared AFTER the statement date was still in transit
    AT that date: it counts in the as-of variance (fixed set) and is NOT
    tickable (it is already compensée)."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(r["id"], _day(20))  # cleared AFTER the period below

    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=0)
    # Ticking a resurrected (already compensée) entry is refused loudly.
    _, errs = trust.complete_reconciliation(rec["id"], [r["id"]])
    assert errs and "conciliable" in errs[0]
    assert store["trust_reconciliations"][rec["id"]]["status"] == "brouillon"
    # Untouched, it balances: 0 (relevé) + 100000 (transit as-of) − 100000 (livre).
    result, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [] and result["variance"] == 0
    assert result["deposits_in_transit_total"] == 100000


def test_annulled_after_period_counts_outstanding(store):
    """An entry reversed AFTER the statement date was still en_circulation AT
    that date — the annulée original counts in the as-of variance."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.reverse_transaction(r["id"], "erreur")  # reversal dated TODAY

    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=0)
    result, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == [] and result["variance"] == 0
    assert result["deposits_in_transit_total"] == 100000


def test_annulled_before_period_excluded(store):
    """A pair fully annulled BEFORE the statement date contributes nothing:
    the frozen book already nets it, and the original was no longer
    outstanding at period_end. (Hand-crafted rows — the API cannot backdate a
    reversal, by design.)"""
    store["trust_transactions"]["orig"] = {
        "id": "orig", "account_id": "acc1", "status": "annulée",
        "direction": "recette", "amount": 100000, "date": _day(40),
        "sequence": 1, "balance_after_account": 100000,
        "reversed_by_id": "rev", "reverses_id": None,
    }
    store["trust_transactions"]["rev"] = {
        "id": "rev", "account_id": "acc1", "status": "annulée",
        "direction": "déboursé", "amount": 100000, "date": _day(38),
        "sequence": 2, "balance_after_account": 0,
        "reversed_by_id": None, "reverses_id": "orig",
    }
    assert trust._list_annulled_after("acc1", _day(30)) == []
    assert trust.book_balance_as_of("acc1", _day(30)) == 0


def test_tick_entry_dated_after_period_refused(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(r["id"], _day(39))
    late, _ = trust.create_transaction(_new(direction="recette", amount=50000, date=_day(5)))
    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=100000)
    _, errs = trust.complete_reconciliation(rec["id"], [late["id"]])
    assert errs and "conciliable" in errs[0]


def test_create_reconciliation_future_period_refused(store):
    _, errs = trust.create_reconciliation(
        "acc1", datetime.now(timezone.utc) + timedelta(days=2), 0
    )
    assert errs == ["La date de fin de période ne peut être dans le futur."]
    rec, errs = trust.create_reconciliation("acc1", datetime.now(timezone.utc), 0)
    assert errs == [] and rec is not None


def test_statement_blank_refused_zero_accepted(store):
    """None (blank/unparseable form input) must ERROR; the literal 0 is a
    LEGITIMATE statement balance (an emptied account reads exactly 0,00 $)."""
    _, errs = trust.create_reconciliation("acc1", _day(1), None)
    assert errs == ["Le solde du relevé est requis."]
    rec, errs = trust.create_reconciliation("acc1", _day(1), 0)
    assert errs == [] and rec["statement_balance"] == 0


def test_route_statement_parse_never_coalesces_to_zero():
    """Static source guard (house pattern): the reconciliation route used to
    do ``_parse_cents(f.get("statement_balance", "")) or 0`` — a malformed
    amount silently became a 0,00 $ statement, which is a LEGITIMATE value
    (an emptied account) and therefore an undetectable corruption. (The
    transfer route's ``or 0`` on ``amount`` is different: 0 is never a valid
    transfer, so the model refuses it — fail closed, not silent.)"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "routes", "trust.py",
    )
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    statement_parses = [
        line for line in src.splitlines() if 'f.get("statement_balance"' in line
    ]
    assert statement_parses, "routes/trust.py: statement_balance parse not found"
    for line in statement_parses:
        assert "or 0" not in line, (
            "routes/trust.py: _parse_cents(statement_balance) or 0 silently "
            "turns a malformed statement amount into 0,00 $ — None must error"
        )


# ── abandon d'un brouillon ──────────────────────────────────────────────────


def test_delete_reconciliation_brouillon(store):
    rec, _ = trust.create_reconciliation("acc1", _day(30), 0)
    ok, errs = trust.delete_reconciliation(rec["id"])
    assert ok is True and errs == []
    assert rec["id"] not in store["trust_reconciliations"]
    # The one-brouillon guard is unblocked by construction.
    rec2, errs = trust.create_reconciliation("acc1", _day(20), 0)
    assert errs == [] and rec2 is not None


def test_delete_reconciliation_completee_refused(store):
    rec, _ = trust.create_reconciliation("acc1", _day(30), 0)
    _, errs = trust.complete_reconciliation(rec["id"], [])  # empty register → 0
    assert errs == []
    ok, errs = trust.delete_reconciliation(rec["id"])
    assert ok is False and errs == ["Cette conciliation est déjà complétée."]
    assert rec["id"] in store["trust_reconciliations"]


# ═══════════════════════════════════════════════════════════════════════════
# _reconciliation_overdue — pinned-clock tests (PA-D06)
#
# The original predicate hid a grace-window early-return ABOVE the
# never-reconciled branch: « jamais conciliée » could only read True on the
# few days each month past `last_month_end + 30 j` — at most ~21 days a
# year, and NEVER in February (Jan 31 + 30 j lands in March). It also
# compared only against LAST month's end, so arrears older than one month
# were invisible while the newest month sat in grace. Every test here pins
# the clock (the injectable `now`) — a mid-month clock made the old and new
# predicates agree, which is exactly how the defect survived.
# ═══════════════════════════════════════════════════════════════════════════


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


def test_never_reconciled_is_overdue_mid_month():
    """The audit's observation: {last_reconciliation_date: null,
    reconciliation_overdue: false} on a mid-month clock."""
    assert trust._reconciliation_overdue(None, now=_dt(2026, 7, 15)) is True


def test_never_reconciled_is_overdue_in_february():
    """February was unreachable outright for the old predicate."""
    assert trust._reconciliation_overdue(None, now=_dt(2026, 2, 15)) is True


def test_fresh_account_is_not_overdue():
    """An account younger than its first due month-end is exempt — nothing
    existed to reconcile."""
    assert trust._reconciliation_overdue(
        None, now=_dt(2026, 7, 15), account_floor=_dt(2026, 7, 2)
    ) is False


def test_account_older_than_due_month_end_is_overdue():
    assert trust._reconciliation_overdue(
        None, now=_dt(2026, 7, 30), account_floor=_dt(2026, 1, 10)
    ) is True


def test_last_month_still_in_grace_is_not_overdue():
    """Reconciled through June, asked July 15: July's month-end has not
    happened and June is covered — not overdue."""
    assert trust._reconciliation_overdue(
        _dt(2026, 6, 30), now=_dt(2026, 7, 15)
    ) is False


def test_newest_month_in_grace_does_not_mask_older_arrears():
    """Last reconciled Nov 30, asked Feb 15: January is within grace but
    DECEMBER's reconciliation was due end of January — overdue. The old
    early-return reported False here."""
    assert trust._reconciliation_overdue(
        _dt(2025, 11, 30), now=_dt(2026, 2, 15)
    ) is True


def test_reconciled_through_due_month_is_fine():
    """Last reconciled Dec 31, asked Feb 15: December is covered and
    January is still in its 30-day grace."""
    assert trust._reconciliation_overdue(
        _dt(2025, 12, 31), now=_dt(2026, 2, 15)
    ) is False


def test_overdue_once_grace_expires():
    """Last reconciled May 31, asked July 31: June's grace (30 j) has just
    expired — overdue."""
    assert trust._reconciliation_overdue(
        _dt(2026, 5, 31), now=_dt(2026, 7, 31)
    ) is True


def test_firm_snapshot_or_of_accounts_and_never_flag(store, monkeypatch):
    """One reconciled account must not mask a never-reconciled sibling, and
    reconciliation_never_performed only fires when NO account has ever
    completed one."""
    acc2, _ = trust.create_account({
        "name": "Compte 2", "institution": "BN", "transit": "12345",
        "account_number_last4": "9999",
    })
    rec, _ = trust.create_reconciliation("acc1", _day(45), 0)
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == []

    snap = trust.get_firm_trust_snapshot()
    by_id = {a["id"]: a for a in snap["accounts"]}
    assert by_id["acc1"]["never_reconciled"] is False
    assert by_id[acc2["id"]]["never_reconciled"] is True
    assert snap["reconciliation_never_performed"] is False
    # acc2 was just created → young-account exemption applies to ITS flag,
    # but the never_reconciled marker still tells the truth.
    assert snap["reconciliation_overdue"] == (
        by_id["acc1"]["reconciliation_overdue"]
        or by_id[acc2["id"]]["reconciliation_overdue"]
    )


def test_delete_reconciliation_missing(store):
    ok, errs = trust.delete_reconciliation("nope")
    assert ok is False and errs == ["Conciliation introuvable."]


# ── arithmétique bancaire + concurrence + instantanés ───────────────────────


def test_complete_bank_balance_incremented_not_set(store):
    """The retro arithmetic pin: bank_balance moves by the ticked deltas, it is
    never SET to the statement figure — under the old code this completion
    aborted because current bank (150000) − 20000 ≠ statement (80000)."""
    a, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(a["id"], _day(39))
    cheque, _ = trust.create_transaction(
        _new(direction="déboursé", amount=20000, purpose="déboursé_tiers", date=_day(35))
    )
    b, _ = trust.create_transaction(_new(direction="recette", amount=50000, date=_day(10)))
    trust.clear_transaction(b["id"], _day(9))

    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=80000)
    result, errs = trust.complete_reconciliation(rec["id"], [cheque["id"]])
    assert errs == [] and result["variance"] == 0
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 130000


def test_complete_concurrency_abort_on_account_change(store):
    """The etag sentinel replaces the old « bank + deltas == relevé » equality
    as the concurrency check: any register movement between the pre-pass and
    the transaction regenerates the account etag and aborts the commit."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=100000)

    original_get = trust.get_account

    def _get_then_mutate(account_id):
        account = original_get(account_id)
        store["trust_accounts"][account_id]["etag"] = "moved-since-pre-pass"
        return account

    with mock.patch.object(trust, "get_account", _get_then_mutate):
        _, errs = trust.complete_reconciliation(rec["id"], [r["id"]])
    assert errs == ["Le compte a changé pendant la conciliation. Veuillez recommencer."]
    assert store["trust_transactions"][r["id"]]["status"] == "en_circulation"
    assert store["trust_reconciliations"][rec["id"]]["status"] == "brouillon"


def test_completed_rec_snapshots_as_of_totals(store):
    """The finalized doc snapshots the AS-OF figures — book at period_end and
    totals including the resurrection sets — never today's balances."""
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    trust.clear_transaction(r["id"], _day(20))  # resurrected for the period below
    late, _ = trust.create_transaction(_new(direction="recette", amount=50000, date=_day(5)))
    trust.clear_transaction(late["id"], _day(4))

    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=0)
    result, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs == []
    assert result["book_balance"] == 100000          # as-of, NOT 150000 (today)
    assert result["deposits_in_transit_total"] == 100000
    assert result["outstanding_cheques_total"] == 0
    assert result["variance"] == 0


def test_ticked_recette_increments_dossier_cleared_map(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    rec, _ = trust.create_reconciliation("acc1", _day(30), statement_balance=100000)
    _, errs = trust.complete_reconciliation(rec["id"], [r["id"]])
    assert errs == []
    assert store["dossiers"]["dos1"]["trust_cleared_by_client"]["c1"] == 100000


def test_as_of_context_partition(store):
    """Each row lands in exactly ONE as-of bucket: tickable (a), resurrected
    (b), or excluded (dated after the period)."""
    a, _ = trust.create_transaction(_new(direction="recette", amount=100000, date=_day(40)))
    b, _ = trust.create_transaction(_new(direction="recette", amount=50000, date=_day(39)))
    trust.clear_transaction(b["id"], _day(20))
    trust.create_transaction(_new(direction="recette", amount=25000, date=_day(5)))

    ctx = trust.reconciliation_as_of_context("acc1", _day(30))
    assert [e["id"] for e in ctx["in_transit"]] == [a["id"]]
    assert [e["id"] for e in ctx["cleared_later"]] == [b["id"]]
    assert ctx["annulled_later"] == []
    assert ctx["outstanding"] == []
    assert ctx["fixed_in_transit_total"] == 50000
    assert ctx["fixed_outstanding_total"] == 0
    assert ctx["book_as_of"] == 150000


# ── get_trust_summary (in_transit = book − cleared) ────────────────────────


def test_get_trust_summary_in_transit(monkeypatch):
    import models.dossier as dm

    monkeypatch.setattr(
        dm, "get_dossier",
        lambda did: {
            "id": did, "clients": [{"id": "c1", "name": "Jean"}],
            "trust_balance": 30000, "trust_balance_by_client": {"c1": 30000},
            "trust_cleared_by_client": {"c1": 10000},
        },
    )
    summary = trust.get_trust_summary("dos1")
    assert summary["has_trust"] is True
    assert summary["total_cents"] == 30000
    assert summary["by_client"][0]["in_transit_cents"] == 20000


# ═══════════════════════════════════════════════════════════════════════════
# Template wiring guards — an HTMX lookup input that does not actually SEND
# its parameter fails SILENTLY: the route just sees an empty query and the
# picker stays empty forever. These static guards exist because exactly that
# shipped — the dossier picker had no name="q", so hx-include="this"
# serialized nothing and no dossier ever appeared.
# ═══════════════════════════════════════════════════════════════════════════

_TRUST_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "trust"
)


def _template(name: str) -> str:
    with open(os.path.join(_TRUST_TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def _input_tags(html: str) -> list[str]:
    return re.findall(r"<input\b[^>]*>", html, re.S)


def test_dossier_search_inputs_send_a_query_param():
    """hx-include="this" serializes an input BY ITS NAME — without name="q" the
    dossier_search route receives no query and the picker stays empty."""
    for tpl in ("form.html", "transfer_form.html"):
        tags = [t for t in _input_tags(_template(tpl)) if "dossier_search" in t]
        assert tags, f"{tpl}: expected at least one dossier_search input"
        for tag in tags:
            assert 'name="q"' in tag, (
                f'{tpl}: a dossier-search input lacks name="q" — HTMX would send '
                "no query and no dossier would ever appear"
            )


def test_client_and_counterparty_lookups_send_dossier_id():
    """client_search / counterparty_suggest read dossier_id from the query
    string; the input must carry a mechanism that sends it."""
    tags = [
        t for t in _input_tags(_template("form.html"))
        if "client_search" in t or "counterparty_suggest" in t
    ]
    assert tags, "form.html: expected client/counterparty lookup inputs"
    for tag in tags:
        assert "dossier_id" in tag, (
            "form.html: a lookup input does not send dossier_id "
            "(needs hx-include=\"[name='dossier_id']\" or hx-vals)"
        )


def test_no_results_div_closes_itself_on_the_click_that_opens_it():
    """@click.outside must sit on the WRAPPER, never on the results div itself.

    The click that focuses the input is OUTSIDE the results div, so Alpine would
    close the dropdown in the very click that opened it — the bug that made the
    client picker permanently invisible.
    """
    for tpl in ("form.html", "transfer_form.html"):
        html = _template(tpl)
        for m in re.finditer(r'<div[^>]*id="[^"]*(?:res|results)"[^>]*>', html, re.S):
            assert "click.outside" not in m.group(0), (
                f"{tpl}: a results div carries @click.outside — it would close on "
                f"the opening click. Move it to the wrapper. Offender: {m.group(0)[:80]}"
            )


def test_client_is_a_native_select_not_an_autocomplete():
    """§4.3 — funds are held for a client OF THE DOSSIER: a closed set. A native
    select makes an unlisted client unsubmittable and has no dropdown to race."""
    assert '<select name="client_id"' in _template("form.html")
    transfer = _template("transfer_form.html")
    assert '<select name="from_client_id"' in transfer
    assert '<select name="to_client_id"' in transfer


def test_dossier_rows_carry_their_clients_for_the_select():
    """The client select is populated straight off the picked dossier row, so
    dossier_search must emit data-clients (no second request to race)."""
    with open(
        os.path.join(os.path.dirname(_TRUST_TEMPLATES), "..", "routes", "trust.py"),
        encoding="utf-8",
    ) as fh:
        route = fh.read()
    assert "data-clients=" in route, "dossier_search must emit data-clients on each row"
    for tpl in ("form.html", "transfer_form.html"):
        assert "dataset.clients" in _template(tpl), (
            f"{tpl}: the dossier picker must load the row's clients into the select"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Fee transfer backed by an invoice — Athena OR external (user decision
# 2026-07-17). A virement_honoraires must be backed by exactly one of them.
# ═══════════════════════════════════════════════════════════════════════════


def test_virement_with_external_ref_allowed(store):
    _fund_cleared(store, amount=100000)  # cleared 100000 for c1
    entry, errs = trust.create_transaction(_new(
        direction="déboursé", purpose="virement_honoraires", amount=60000,
        invoice_external_ref="INV-2019-042",
        date=datetime(2026, 7, 3, tzinfo=timezone.utc),
    ))
    assert errs == []
    assert entry["invoice_external_ref"] == "INV-2019-042"
    assert entry["invoice_id"] is None


def test_virement_without_any_invoice_refused(store):
    _fund_cleared(store, amount=100000)
    _, errs = trust.create_transaction(_new(
        direction="déboursé", purpose="virement_honoraires", amount=50000,
        date=datetime(2026, 7, 3, tzinfo=timezone.utc),
    ))
    assert errs and "facture" in errs[0].lower()


def test_virement_with_both_invoice_and_external_refused(store):
    store["invoices"]["inv1"] = {
        "id": "inv1", "status": "envoyée", "dossier_id": "dos1", "amount_due": 100000,
    }
    _fund_cleared(store, amount=100000)
    _, errs = trust.create_transaction(_new(
        direction="déboursé", purpose="virement_honoraires", amount=50000,
        invoice_id="inv1", invoice_external_ref="INV-2019-042",
        date=datetime(2026, 7, 3, tzinfo=timezone.utc),
    ))
    assert errs and "jamais les deux" in errs[0].lower()


def test_valid_athena_invoice_still_verified_with_no_external(store):
    store["invoices"]["inv1"] = {
        "id": "inv1", "status": "envoyée", "dossier_id": "dos1", "amount_due": 100000,
    }
    _fund_cleared(store, amount=100000)
    entry, errs = trust.create_transaction(_new(
        direction="déboursé", purpose="virement_honoraires", amount=40000,
        invoice_id="inv1",
        date=datetime(2026, 7, 3, tzinfo=timezone.utc),
    ))
    assert errs == []
    assert entry["invoice_id"] == "inv1"
    assert entry["invoice_external_ref"] == ""


def test_invoice_fields_ignored_on_non_virement(store):
    # A stray external ref / invoice id on a deposit is dropped, not stored.
    entry, errs = trust.create_transaction(_new(
        direction="recette", purpose="dépôt_client", amount=50000,
        invoice_external_ref="STRAY", invoice_id="inv1",
    ))
    assert errs == []
    assert entry["invoice_external_ref"] == ""
    assert entry["invoice_id"] is None


# ── The route's Athena-invoice NUMBER -> id resolver (routes/trust.py) ──────


def test_resolve_invoice_number_hard_errors_on_typo(monkeypatch):
    """A typo'd Athena invoice number must be a HARD error, never a silent
    downgrade to 'external' (which would skip the amount check)."""
    import routes.trust as rt
    import models.invoice as invoice_model

    monkeypatch.setattr(
        invoice_model, "list_invoices",
        lambda dossier_id=None: [{"id": "i1", "invoice_number": "2026-F001"}],
    )

    ok = {"purpose": "virement_honoraires", "dossier_id": "d1", "invoice_number": "2026-F001"}
    assert rt._resolve_invoice_number(ok) == []
    assert ok["invoice_id"] == "i1"

    typo = {"purpose": "virement_honoraires", "dossier_id": "d1", "invoice_number": "2026-F009"}
    assert rt._resolve_invoice_number(typo)  # non-empty error
    assert typo["invoice_id"] is None  # NOT downgraded to external

    non_transfer = {"purpose": "dépôt_client", "dossier_id": "d1", "invoice_number": "2026-F001"}
    assert rt._resolve_invoice_number(non_transfer) == []
    assert non_transfer["invoice_id"] is None


# ── Worksheet as-of wiring guards (retro reconciliation) ────────────────────


def test_worksheet_variance_reads_the_as_of_book_balance():
    """The client-side variance must consume the SAME as-of numbers as the
    server gate (reconciliation_as_of_context) — reading the account's
    CURRENT book_balance is exactly the now-anchoring bug being fixed."""
    html = _template("reconciliation_worksheet.html")
    assert "bookCents: {{ book_as_of | int }}" in html
    assert "account.book_balance if account else 0) | int" not in html
    # The resurrection sets seed the recompute as fixed constants.
    assert "fixedOutstanding" in html and "fixedInTransit" in html


def test_worksheet_has_abandon_form_with_csrf():
    """The abandon dialog must POST with a CSRF token — and live OUTSIDE the
    completion <form> (nested forms are invalid HTML: the browser drops the
    inner one and the button would submit the COMPLETION instead)."""
    html = _template("reconciliation_worksheet.html")
    assert "trust.reconciliation_abandon" in html
    abandon_form = html.split("trust.reconciliation_abandon")[1]
    assert 'name="csrf_token"' in abandon_form
    completion = html.split("trust.reconciliation_complete")[1].split("</form>")[0]
    assert "trust.reconciliation_abandon" not in completion


def test_worksheet_readonly_sections_carry_no_checkbox():
    """The resurrection sections are informational: an <input> there would let
    a click try to re-clear an already-compensée entry (refused server-side,
    but the worksheet must not invite it)."""
    html = _template("reconciliation_worksheet.html")
    for heading in ("Compensées après cette période", "Annulées après cette période"):
        assert heading in html
        section = html.split(heading)[1].split("</div>\n    {% endif %}")[0]
        assert "cleared_tx_ids" not in section


def test_reconciliation_form_caps_period_end_at_today():
    html = _template("reconciliation_form.html")
    tags = [t for t in _input_tags(html) if 'name="period_end"' in t]
    assert tags and 'max="{{ today }}"' in tags[0]


# ── Solde reporté (art. 38 period sheet) ────────────────────────────────────


class _OpeningQuery:
    """Minimal stand-in for the Firestore chain of opening_book_balance:
    where(account_id) → where(date <) → order_by ×2 → limit → stream."""

    def __init__(self, rows):
        self._rows = rows
        self._cutoff = None
        self._account = None

    def where(self, filter=None):
        field, op, value = filter.field_path, filter.op_string, filter.value
        if field == "account_id":
            self._account = value
        elif field == "date" and op == "<":
            self._cutoff = value
        return self

    def order_by(self, *a, **kw):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def stream(self):
        rows = [
            r for r in self._rows
            if r["account_id"] == self._account and r["date"] < self._cutoff
        ]
        # date DESC, sequence DESC — the query's own ordering
        rows.sort(key=lambda r: (r["date"], r["sequence"]), reverse=True)
        for r in rows[:1]:
            yield mock.Mock(to_dict=lambda r=r: r)


def _opening_db(rows):
    collection = mock.Mock()
    collection.where.side_effect = lambda filter=None: _OpeningQuery(rows).where(
        filter=filter
    )
    db = mock.Mock()
    db.collection.return_value = collection
    return db


def _entry(seq, day, balance, account="a1"):
    return {
        "account_id": account, "sequence": seq,
        "date": datetime(2026, 8, day, tzinfo=timezone.utc),
        "balance_after_account": balance,
    }


def test_opening_balance_empty_register_is_flagged(monkeypatch):
    monkeypatch.setattr(trust, "db", _opening_db([]))
    assert trust.opening_book_balance("a1", datetime(2026, 8, 1, tzinfo=timezone.utc)) == (0, False)


def test_opening_balance_takes_the_last_entry_before_the_period(monkeypatch):
    monkeypatch.setattr(trust, "db", _opening_db([
        _entry(1, 5, 100000),
        _entry(2, 20, 250000),          # last one before 2026-08-25
        _entry(3, 28, 999999),          # inside the period — must not count
    ]))
    assert trust.opening_book_balance("a1", datetime(2026, 8, 25, tzinfo=timezone.utc)) == (250000, True)


def test_opening_balance_same_day_takes_the_highest_sequence(monkeypatch):
    monkeypatch.setattr(trust, "db", _opening_db([
        _entry(7, 20, 100000),
        _entry(8, 20, 175000),          # same day, later sequence
    ]))
    assert trust.opening_book_balance("a1", datetime(2026, 8, 21, tzinfo=timezone.utc)) == (175000, True)


def test_opening_balance_excludes_the_first_day_itself(monkeypatch):
    # « reporté AU 1er » means the balance standing BEFORE that day's entries.
    monkeypatch.setattr(trust, "db", _opening_db([
        _entry(1, 10, 100000),
        _entry(2, 15, 400000),
    ]))
    assert trust.opening_book_balance("a1", datetime(2026, 8, 15, tzinfo=timezone.utc)) == (100000, True)


def test_opening_balance_ignores_other_accounts(monkeypatch):
    monkeypatch.setattr(trust, "db", _opening_db([
        _entry(1, 5, 100000, account="a1"),
        _entry(9, 9, 888888, account="autre"),
    ]))
    assert trust.opening_book_balance("a1", datetime(2026, 8, 20, tzinfo=timezone.utc)) == (100000, True)


def test_implied_opening_balance_reads_back_the_first_entry():
    # balance_after_account minus that entry's own book contribution.
    recette = {"direction": "recette", "amount": 500000,
               "status": "en_circulation", "balance_after_account": 600000}
    assert trust.implied_opening_balance(recette) == 100000
    debourse = {"direction": "déboursé", "amount": 25000,
                "status": "compensée", "balance_after_account": 75000}
    assert trust.implied_opening_balance(debourse) == 100000
    # annulée still counts in the book balance (compute_deltas §4.2).
    annulee = {"direction": "recette", "amount": 30000,
               "status": "annulée", "balance_after_account": 130000}
    assert trust.implied_opening_balance(annulee) == 100000


def test_period_sheet_reconciles_opening_entries_and_closing():
    """The arithmetic the carried-forward line exists for:
    report + Σ recettes − Σ déboursés = solde de clôture."""
    opening = 100000
    entries = [
        {"direction": "recette", "amount": 500000, "status": "compensée"},
        {"direction": "déboursé", "amount": 25000, "status": "en_circulation"},
        {"direction": "recette", "amount": 12550, "status": "annulée"},
    ]
    running = opening
    for e in entries:
        running += trust.compute_deltas(
            e["direction"], e["amount"], e["status"]
        )["book"]
        e["balance_after_account"] = running

    recettes = sum(e["amount"] for e in entries if e["direction"] == "recette")
    debours = sum(e["amount"] for e in entries if e["direction"] == "déboursé")
    assert opening + recettes - debours == running
    # …and the first entry alone implies the opening balance.
    assert trust.implied_opening_balance(entries[0]) == opening


# ── Export links must follow the ACTIVE period ──────────────────────────────


def test_export_links_carry_the_period_and_live_in_one_partial():
    """Both links read the active filters. The PDF register drops statut/sens
    (a book of account is complete) but MUST carry the period: it prints it
    and carries a balance forward into it."""
    html = _template("_export_links.html")
    for fmt in ("'csv'", "'pdf'"):
        link = next(
            line for line in html.splitlines()
            if f"fmt={fmt}" in line
        )
        assert "date_from=filters.date_from" in link, fmt
        assert "date_to=filters.date_to" in link, fmt
    csv_link = next(l for l in html.splitlines() if "fmt='csv'" in l)
    pdf_link = next(l for l in html.splitlines() if "fmt='pdf'" in l)
    assert "status=filters.status" in csv_link
    assert "status=filters.status" not in pdf_link       # complete register


def test_rows_partial_reswaps_the_export_links_out_of_band():
    """The regression this pins: the links sit OUTSIDE #trust-rows, the only
    region an HTMX filter change swaps. Without the OOB re-emission they keep
    the period of the initial page load, and the exported register silently
    covers everything — which is exactly what shipped on 2026-08-11."""
    rows = _template("_transaction_rows.html")
    assert 'hx-swap-oob="true"' in rows
    assert 'id="trust-export"' in rows
    assert '{% include "trust/_export_links.html" %}' in rows
    # Guarded, or a full-page render would emit the id twice.
    assert "request.headers.get('HX-Request')" in rows
    # OUTSIDE the rows/no-rows branches: an empty period must refresh them too.
    oob_at = rows.index('hx-swap-oob="true"')
    assert oob_at > rows.rindex("{% else %}")


def test_list_page_hosts_the_same_export_container():
    """The OOB swap replaces by id — the host container must carry the same
    id and classes, or the toolbar reflows on the first filter change."""
    html = _template("list.html")
    assert '<div id="trust-export" class="ml-auto flex gap-2">' in html
    assert '{% include "trust/_export_links.html" %}' in html
    # The links themselves live in the partial alone — no second copy to drift.
    assert "trust.journal_export" not in html
