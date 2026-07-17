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


def test_complete_reconciliation_variance_refused(store):
    trust.create_transaction(_new(direction="recette", amount=100000))  # in transit
    rec, _ = trust.create_reconciliation(
        "acc1", datetime(2026, 7, 31, tzinfo=timezone.utc), statement_balance=500
    )
    _, errs = trust.complete_reconciliation(rec["id"], [])
    assert errs
    assert store["trust_reconciliations"][rec["id"]]["status"] == "brouillon"


def test_complete_reconciliation_balanced(store):
    r, _ = trust.create_transaction(_new(direction="recette", amount=100000))
    rec, _ = trust.create_reconciliation(
        "acc1", datetime(2026, 7, 31, tzinfo=timezone.utc), statement_balance=100000
    )
    result, errs = trust.complete_reconciliation(rec["id"], [r["id"]])
    assert errs == []
    assert result["status"] == "complétée"
    assert result["variance"] == 0
    entry = store["trust_transactions"][r["id"]]
    assert entry["status"] == "compensée"
    assert entry["reconciliation_id"] == rec["id"]
    assert store["trust_accounts"]["acc1"]["bank_balance"] == 100000


def test_create_reconciliation_one_brouillon_per_account(store):
    trust.create_reconciliation("acc1", datetime(2026, 7, 31, tzinfo=timezone.utc), 0)
    _, errs = trust.create_reconciliation("acc1", datetime(2026, 8, 31, tzinfo=timezone.utc), 0)
    assert errs


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
