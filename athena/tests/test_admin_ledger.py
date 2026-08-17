"""Tests for models/admin_ledger.py — comptabilité d'administration.

The pure layer (delta/display arithmetic, the TPS/TVQ ventilation and the
gross split) carries the suite without Firestore; the transactional logic
(create/update/delete/clear/reverse/card-payment/reconcile + the
reconciliation LOCK) runs against the fake-Firestore harness copied from
tests/test_trust.py — with ONE extension: ``_FakeQuery._match`` understands
the inequality operators (>=, <, <=, >), because this module pushes DATE
BOUNDS to Firestore (``list_register``) and its as-of sums would silently
test nothing under an equality-only fake.

The stub preamble mirrors test_trust.py: patch ``firestore`` ON THE MODULE
so ``@firestore.transactional`` is an identity decorator — with the real
decorator the fake would be driven through the begin/commit protocol and
AttributeError.
"""

import copy
import importlib
import importlib.util
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Conditional third-party stubs (test_trust.py preamble) ─────────────────
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
    import models.admin_ledger as al
    import models.invoice as invoice_model


X = 100_00  # $100.00 in cents


# ═══════════════════════════════════════════════════════════════════════════
# Pure layer
# ═══════════════════════════════════════════════════════════════════════════


def test_admin_delta_signs_and_status_blindness():
    assert al.admin_delta("recette", X) == X
    assert al.admin_delta("déboursé", X) == -X
    # No status argument at all — annulée rows count by construction.


def test_display_balance_card_reads_as_positive_owed():
    assert al.display_balance("opérations", 5000) == 5000
    assert al.display_balance("opérations", -5000) == -5000
    # Card ledger runs negative when owed → « Solde dû » positive.
    assert al.display_balance("carte_crédit", -125000) == 125000
    assert al.display_balance("carte_crédit", 2000) == -2000  # credit balance


def test_statement_to_ledger_converts_once():
    assert al.statement_to_ledger("opérations", 123400) == 123400
    assert al.statement_to_ledger("carte_crédit", 123400) == -123400
    assert al.statement_to_ledger("carte_crédit", 0) == 0


def test_direction_labels_adapt_to_account_type():
    assert al.direction_labels_for("opérations")["déboursé"] == "Déboursé"
    assert al.direction_labels_for("carte_crédit")["déboursé"] == "Charge"
    assert al.direction_labels_for("carte_crédit")["recette"] == "Paiement / Remboursement"


# ── extract_taxes_from_gross ────────────────────────────────────────────────


def test_extract_taxes_matches_the_hand_computed_case():
    # 114,98 $ gross → net 100,00 $, TPS 5,00 $, TVQ 9,98 $ (9,975 rounded).
    net, tps, tvq = al.extract_taxes_from_gross(11498)
    assert (net, tps, tvq) == (10000, 500, 998)


def test_extract_taxes_exactness_property_over_a_cents_sweep():
    """net + tps + tvq == gross EXACTLY, every part non-negative — the
    remainder is imputed to the net, never dropped."""
    for gross in list(range(1, 3000)) + [11498, 114975, 999999, 123456789]:
        net, tps, tvq = al.extract_taxes_from_gross(gross)
        assert net + tps + tvq == gross, gross
        assert net >= 0 and tps >= 0 and tvq >= 0, gross


def test_tax_constants_pinned_to_the_invoice_rates():
    """LOCAL constants (vocabulary doctrine forbids the import) — this pin is
    what keeps a future rate change from silently diverging the two."""
    from decimal import Decimal

    assert al._GST == Decimal(invoice_model.GST_RATE_BPS) / Decimal(10000)
    assert al._QST == Decimal(invoice_model.QST_RATE_BPS) / Decimal(100000)
    assert al._GROSS_DIVISOR == Decimal("1.14975")


# ── validate_ventilation ────────────────────────────────────────────────────


def test_ventilation_recette_zeroes_stray_values_silently():
    # The form's hidden x-show fields may submit strays — the trust rule.
    vent, reason = al.validate_ventilation("recette", X, 5000, 250, 499)
    assert reason == ""
    assert vent == {"net_amount": 0, "gst_amount": 0, "qst_amount": 0}


def test_ventilation_blank_deboursé_defaults_to_net_equals_amount():
    vent, reason = al.validate_ventilation("déboursé", X, None, "", 0)
    assert reason == ""
    assert vent == {"net_amount": X, "gst_amount": 0, "qst_amount": 0}


def test_ventilation_deboursé_must_sum_to_amount():
    vent, reason = al.validate_ventilation("déboursé", 11498, 10000, 500, 998)
    assert reason == "" and vent["net_amount"] == 10000
    vent, reason = al.validate_ventilation("déboursé", 11498, 10000, 500, 999)
    assert vent is None and reason == "ventilation_invalide"


def test_ventilation_refuses_negative_and_non_int_parts():
    assert al.validate_ventilation("déboursé", X, -1, 0, 1 + X)[0] is None
    assert al.validate_ventilation("déboursé", X, 50.5, 0, 0)[0] is None
    assert al.validate_ventilation("déboursé", X, True, 0, X - 1)[0] is None


# ── running_balances ────────────────────────────────────────────────────────


def test_running_balances_from_an_opening():
    txs = [
        {"direction": "recette", "amount": 100000},
        {"direction": "déboursé", "amount": 30000},
        {"direction": "déboursé", "amount": 20000},
    ]
    assert al.running_balances(txs, opening=5000) == [105000, 75000, 55000]
    assert al.running_balances([], opening=42) == []


# ═══════════════════════════════════════════════════════════════════════════
# Fake-Firestore harness (test_trust.py:384-565, _match extended to
# inequality operators — the date-bounded register queries need them).
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
            dv = doc.get(fp)
            if op == "==":
                if dv != val:
                    return False
            elif op == ">=":
                if dv is None or dv < val:
                    return False
            elif op == ">":
                if dv is None or dv <= val:
                    return False
            elif op == "<=":
                if dv is None or dv > val:
                    return False
            elif op == "<":
                if dv is None or dv >= val:
                    return False
            else:  # an operator the fake does not model must FAIL, not pass
                raise AssertionError(f"unsupported operator in fake: {op}")
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
    def __init__(self, field_path=None, op_string=None, value=None, **_kw):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


class _FakeFirestore:
    transactional = staticmethod(lambda fn: fn)
    Transaction = object

    class Query:
        ASCENDING = "ASCENDING"
        DESCENDING = "DESCENDING"


_TODAY = date(2026, 8, 13)


def _base_store():
    return {
        "admin_accounts": {
            "ops1": {
                "id": "ops1", "name": "Opérations", "status": "actif",
                "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
            },
            "carte1": {
                "id": "carte1", "name": "Visa corporative", "status": "actif",
                "account_type": "carte_crédit", "ledger_balance": 0, "etag": "e0",
            },
        },
        "counters": {},
        "dossiers": {
            "dos1": {"id": "dos1", "file_number": "2026-001", "title": "Tremblay c. X"},
        },
        "invoices": {
            "fac1": {
                "id": "fac1", "invoice_number": "2026-F031", "status": "envoyée",
                "amount_due": 100000, "amount_paid": 0,
                "dossier_id": "dos1", "dossier_file_number": "2026-001",
                "dossier_title": "Tremblay c. X",
            },
        },
        "admin_transactions": {},
        "admin_reconciliations": {},
    }


@pytest.fixture
def store(monkeypatch):
    s = _base_store()
    monkeypatch.setattr(al, "db", _FakeDB(s))
    monkeypatch.setattr(al, "firestore", _FakeFirestore)
    monkeypatch.setattr(al, "FieldFilter", _TestFieldFilter)
    # Freeze the Montréal clock: date-derived-from-clock tests are the house
    # landmine (the 2026-08-11 00:03 UTC build) — every date below is fixed.
    monkeypatch.setattr(al, "today_mtl", lambda: _TODAY)
    return s


def _d(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _new(**over):
    payload = {
        "account_id": "ops1", "direction": "déboursé", "kind": "dépense",
        "category": "loyer", "amount": 100000, "method": "virement",
        "counterparty": "Immeubles Sainte-Catherine",
        "date": _d(2026, 7, 10),
        "description": "", "reference": "", "supplier_invoice_ref": "",
    }
    payload.update(over)
    return payload


# ═══════════════════════════════════════════════════════════════════════════
# create_transaction
# ═══════════════════════════════════════════════════════════════════════════


def test_create_expense_moves_the_ledger_down(store):
    entry, errs = al.create_transaction(_new())
    assert errs == []
    assert entry["sequence"] == 1
    assert entry["status"] == "en_circulation"
    assert entry["net_amount"] == 100000  # blank ventilation → net = amount
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == -100000
    # No frozen balance column exists — the deliberate divergence.
    assert "balance_after_account" not in entry


def test_create_receipt_moves_the_ledger_up(store):
    entry, errs = al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             counterparty="Intérêts BNC")
    )
    assert errs == []
    assert entry["category"] is None
    assert entry["net_amount"] == 0  # recettes carry no ventilation
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 100000


def test_create_with_ventilation_stores_the_three_parts(store):
    entry, errs = al.create_transaction(
        _new(amount=11498, net_amount=10000, gst_amount=500, qst_amount=998)
    )
    assert errs == []
    assert (entry["net_amount"], entry["gst_amount"], entry["qst_amount"]) == (10000, 500, 998)


def test_create_refuses_a_bad_ventilation(store):
    entry, errs = al.create_transaction(
        _new(amount=11498, net_amount=10000, gst_amount=500, qst_amount=999)
    )
    assert entry is None and "ventilation" in errs[0]


def test_create_refuses_future_dates_on_the_montreal_clock(store):
    entry, errs = al.create_transaction(_new(date=_d(2026, 8, 14)))
    assert entry is None and "futur" in errs[0]
    # Today itself is fine.
    entry, errs = al.create_transaction(_new(date=_d(2026, 8, 13)))
    assert errs == []


def test_backdating_is_allowed_while_the_period_is_open(store):
    """THE feature: an earlier economic date after a later one — refused by
    trust (antidatage_refusé), accepted here."""
    _, errs = al.create_transaction(_new(date=_d(2026, 7, 20)))
    assert errs == []
    entry, errs = al.create_transaction(_new(date=_d(2026, 7, 5)))
    assert errs == []
    assert entry["sequence"] == 2  # insertion order survives as the audit cursor


def test_create_refuses_expense_without_category(store):
    entry, errs = al.create_transaction(_new(category=""))
    assert entry is None and "catégorie" in errs[0].lower()
    entry, errs = al.create_transaction(_new(category="carburant_spatial"))
    assert entry is None and "invalide" in errs[0]


def test_create_refuses_special_kinds(store):
    for kind in ("paiement_carte", "correction", "n_importe_quoi"):
        entry, errs = al.create_transaction(_new(kind=kind, direction="recette", category=""))
        assert entry is None, kind


def test_create_refuses_direction_kind_mismatch(store):
    entry, _ = al.create_transaction(_new(kind="dépense", direction="recette"))
    assert entry is None
    entry, _ = al.create_transaction(
        _new(kind="encaissement_facture", direction="déboursé", invoice_id="fac1", category="")
    )
    assert entry is None


def test_create_refuses_closed_or_unknown_account(store):
    store["admin_accounts"]["ops1"]["status"] = "fermé"
    entry, errs = al.create_transaction(_new())
    assert entry is None and "fermé" in errs[0]
    entry, errs = al.create_transaction(_new(account_id="nope"))
    assert entry is None


def test_create_optional_dossier_is_snapshotted_or_refused(store):
    entry, errs = al.create_transaction(_new(dossier_id="dos1", category="huissier"))
    assert errs == []
    assert entry["dossier_file_number"] == "2026-001"
    entry, errs = al.create_transaction(_new(dossier_id="fantome"))
    assert entry is None and "Dossier" in errs[0]


# ── encaissement_facture ───────────────────────────────────────────────────


def _encaissement(**over):
    base = dict(
        direction="recette", kind="encaissement_facture", category="",
        invoice_id="fac1", amount=60000, counterparty="Jean Tremblay",
    )
    base.update(over)
    return _new(**base)


def test_encaissement_snapshots_the_invoice_and_its_dossier(store):
    entry, errs = al.create_transaction(_encaissement())
    assert errs == []
    assert entry["invoice_id"] == "fac1"
    assert entry["invoice_number"] == "2026-F031"
    assert entry["dossier_id"] == "dos1"
    assert entry["dossier_file_number"] == "2026-001"


def test_encaissement_requires_an_invoice(store):
    entry, errs = al.create_transaction(_encaissement(invoice_id=""))
    assert entry is None and "facture" in errs[0].lower()


def test_encaissement_refused_on_a_credit_card(store):
    entry, errs = al.create_transaction(_encaissement(account_id="carte1"))
    assert entry is None and "carte de crédit" in errs[0]


def test_encaissement_caps_on_the_live_balance_not_amount_due(store):
    """The stricter-than-trust check: amount_due is frozen; the LIVE balance
    (amount_due − amount_paid) is what a new deposit may still cover."""
    store["invoices"]["fac1"]["amount_paid"] = 50000
    entry, errs = al.create_transaction(_encaissement(amount=60000))
    assert entry is None and "excède" in errs[0]
    entry, errs = al.create_transaction(_encaissement(amount=50000))
    assert errs == []


def test_encaissement_refuses_a_non_issued_invoice(store):
    for status in ("brouillon", "payée", "annulée"):
        store["invoices"]["fac1"]["status"] = status
        entry, errs = al.create_transaction(_encaissement())
        assert entry is None, status


# ═══════════════════════════════════════════════════════════════════════════
# The reconciliation lock (create/update/delete refusals)
# ═══════════════════════════════════════════════════════════════════════════


def _completed_rec(store, period_end, rec_id="rec0"):
    store["admin_reconciliations"][rec_id] = {
        "id": rec_id, "account_id": "ops1", "status": "complétée",
        "period_end": period_end, "statement_balance": 0,
    }


def test_create_refuses_a_date_inside_a_reconciled_period(store):
    _completed_rec(store, _d(2026, 7, 31))
    entry, errs = al.create_transaction(_new(date=_d(2026, 7, 15)))
    assert entry is None and "conciliation complétée" in errs[0]
    # The day AFTER the floor is open.
    entry, errs = al.create_transaction(_new(date=_d(2026, 8, 1)))
    assert errs == []


def test_lock_reasons_cover_the_four_clauses_plus_linkage():
    floor = _d(2026, 7, 31)
    base = {"status": "en_circulation", "date": _d(2026, 8, 5), "kind": "dépense"}
    assert al._entry_lock_reason(dict(base), floor) is None
    assert al._entry_lock_reason({**base, "date": _d(2026, 7, 15)}, floor) == "période_verrouillée"
    assert al._entry_lock_reason({**base, "status": "compensée"}, floor) == "écriture_verrouillée"
    assert al._entry_lock_reason({**base, "status": "annulée"}, floor) == "écriture_verrouillée"
    assert al._entry_lock_reason({**base, "reversed_by_id": "r1"}, floor) == "écriture_verrouillée"
    assert al._entry_lock_reason({**base, "kind": "paiement_carte"}, floor) == "paiement_carte_indivisible"
    assert al._entry_lock_reason({**base, "invoice_id": "fac1"}, floor) == "écriture_liée_facture"
    assert (
        al._entry_lock_reason({**base, "trust_transaction_id": "t1"}, floor)
        == "écriture_liée_fideicommis"
    )
    # No floor at all: only the structural clauses lock.
    assert al._entry_lock_reason({**base, "date": _d(2020, 1, 1)}, None) is None


# ═══════════════════════════════════════════════════════════════════════════
# update_transaction / delete_transaction
# ═══════════════════════════════════════════════════════════════════════════


def test_update_adjusts_the_ledger_and_keeps_a_revision(store):
    entry, _ = al.create_transaction(_new(amount=100000))
    # The edit form always resubmits the three ventilation fields; blanks
    # (zeros) mean « re-default » — a STALE net against a changed amount is
    # refused loudly (see test_update_refuses_a_stale_ventilation below).
    updated, errs = al.update_transaction(
        entry["id"], _new(amount=80000, net_amount=0, gst_amount=0, qst_amount=0)
    )
    assert errs == []
    assert updated["amount"] == 80000
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == -80000
    assert len(updated["revisions"]) == 1
    assert updated["revisions"][0]["changes"]["amount"] == [100000, 80000]
    # net follows the blank-ventilation default on the resubmitted form
    assert updated["net_amount"] == 80000


def test_update_refuses_a_stale_ventilation_against_a_changed_amount(store):
    """An API-style partial update that changes the amount but not the
    ventilation keys inherits the stored net — and must be refused rather
    than silently re-split (the register never guesses which of net/taxes
    absorbs a correction)."""
    entry, _ = al.create_transaction(_new(amount=100000))
    updated, errs = al.update_transaction(entry["id"], _new(amount=80000))
    assert updated is None and "ventilation" in errs[0].lower()


def test_update_direction_flip_swings_the_ledger_both_ways(store):
    entry, _ = al.create_transaction(_new(amount=100000))
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == -100000
    updated, errs = al.update_transaction(
        entry["id"],
        _new(direction="recette", kind="recette_autre", category="", amount=100000),
    )
    assert errs == []
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 100000
    assert updated["category"] is None


def test_update_noop_writes_nothing(store):
    entry, _ = al.create_transaction(_new())
    before = copy.deepcopy(store["admin_transactions"][entry["id"]])
    updated, errs = al.update_transaction(entry["id"], _new())
    assert errs == []
    assert store["admin_transactions"][entry["id"]] == before  # no etag churn


def test_update_refuses_a_locked_entry(store):
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    _completed_rec(store, _d(2026, 7, 31))
    updated, errs = al.update_transaction(entry["id"], _new(amount=1))
    assert updated is None and "conciliation" in errs[0]


def test_update_refuses_moving_the_date_behind_the_floor(store):
    _completed_rec(store, _d(2026, 6, 30))
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    updated, errs = al.update_transaction(entry["id"], _new(date=_d(2026, 6, 15)))
    assert updated is None and "conciliation" in errs[0]


def test_update_refuses_compensée_and_reversal_members(store):
    entry, _ = al.create_transaction(_new())
    al.clear_transaction(entry["id"], _d(2026, 7, 12))
    updated, errs = al.update_transaction(entry["id"], _new(amount=1))
    assert updated is None and "verrouillée" in errs[0]


def test_update_refuses_kind_change_to_a_structural_kind(store):
    entry, _ = al.create_transaction(_new())
    updated, errs = al.update_transaction(entry["id"], _new(kind="paiement_carte"))
    assert updated is None


def test_delete_restores_the_ledger_and_returns_the_doc(store):
    entry, _ = al.create_transaction(_new())
    deleted, errs = al.delete_transaction(entry["id"])
    assert errs == []
    assert deleted["id"] == entry["id"]
    assert entry["id"] not in store["admin_transactions"]
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 0


def test_delete_refuses_locked_and_linked_entries(store):
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    _completed_rec(store, _d(2026, 7, 31))
    deleted, errs = al.delete_transaction(entry["id"])
    assert deleted is None
    enc, _ = al.create_transaction(_encaissement(date=_d(2026, 8, 5)))
    deleted, errs = al.delete_transaction(enc["id"])
    assert deleted is None and "facture" in errs[0]


# ═══════════════════════════════════════════════════════════════════════════
# clear / reverse
# ═══════════════════════════════════════════════════════════════════════════


def test_clear_stamps_and_regenerates_the_account_etag(store):
    entry, _ = al.create_transaction(_new())
    etag_before = store["admin_accounts"]["ops1"]["etag"]
    cleared, errs = al.clear_transaction(entry["id"], _d(2026, 7, 12))
    assert errs == []
    assert cleared["status"] == "compensée"
    # The sentinel duty: clearing changes the as-of context, so the account
    # etag must move even though no balance does.
    assert store["admin_accounts"]["ops1"]["etag"] != etag_before


def test_clear_refuses_before_entry_date_or_future(store):
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    _, errs = al.clear_transaction(entry["id"], _d(2026, 7, 9))
    assert errs
    _, errs = al.clear_transaction(entry["id"], datetime.now(timezone.utc) + timedelta(days=2))
    assert errs


def test_reverse_en_circulation_annuls_both_and_restores_the_ledger(store):
    entry, _ = al.create_transaction(_new(amount=100000))
    reversal, errs = al.reverse_transaction(entry["id"], "montant erroné")
    assert errs == []
    assert reversal["status"] == "annulée"
    assert reversal["kind"] == "correction"
    assert reversal["direction"] == "recette"  # opposite of the déboursé
    assert store["admin_transactions"][entry["id"]]["status"] == "annulée"
    assert store["admin_transactions"][entry["id"]]["reversed_by_id"] == reversal["id"]
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 0


def test_reverse_compensée_leaves_original_and_births_en_circulation(store):
    entry, _ = al.create_transaction(_new())
    al.clear_transaction(entry["id"], _d(2026, 7, 12))
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert errs == []
    assert store["admin_transactions"][entry["id"]]["status"] == "compensée"
    assert reversal["status"] == "en_circulation"
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 0


def test_reverse_requires_a_reason_and_refuses_double_reversal(store):
    entry, _ = al.create_transaction(_new())
    _, errs = al.reverse_transaction(entry["id"], "   ")
    assert "motif" in errs[0].lower()
    al.reverse_transaction(entry["id"], "erreur")
    _, errs = al.reverse_transaction(entry["id"], "encore")
    assert "déjà" in errs[0]


def test_reverse_date_is_choosable_within_the_open_window(store):
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    reversal, errs = al.reverse_transaction(
        entry["id"], "même période", reversal_date=_d(2026, 7, 10)
    )
    assert errs == []
    assert al._as_utc(reversal["date"]).date() == date(2026, 7, 10)


def test_reverse_date_refused_before_original_or_behind_the_floor(store):
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    _, errs = al.reverse_transaction(entry["id"], "trop tôt", reversal_date=_d(2026, 7, 9))
    assert "contre-passation" in errs[0]
    _completed_rec(store, _d(2026, 7, 31))
    _, errs = al.reverse_transaction(entry["id"], "période close", reversal_date=_d(2026, 7, 20))
    assert "contre-passation" in errs[0]
    # Above the floor: fine — reversing a locked-period entry is legal.
    reversal, errs = al.reverse_transaction(entry["id"], "corrigé", reversal_date=_d(2026, 8, 2))
    assert errs == []


def test_reverse_of_a_correction_is_refused(store):
    """Revue 2026-08-13 : une contre-passation de contre-passation
    double-compterait la ventilation copiée sur les rapports et ne peut
    jamais re-lier une facture — l'annulation d'une contre-passation
    erronée est une NOUVELLE écriture."""
    entry, _ = al.create_transaction(_new())
    reversal, _ = al.reverse_transaction(entry["id"], "erreur")
    _, errs = al.reverse_transaction(reversal["id"], "oups, la contre-passation était l'erreur")
    assert "correction ne se contre-passe pas" in errs[0]


def test_clear_refuses_a_date_inside_a_locked_period(store):
    """Revue 2026-08-13 : une compensation antidatée dans une période
    conciliée réécrirait la preuve close (l'ensemble de résurrection ne
    ressusciterait plus l'écriture) — le message doit nommer le verrou,
    jamais le générique « date invalide »."""
    entry, _ = al.create_transaction(_new(date=_d(2026, 7, 10)))
    _completed_rec(store, _d(2026, 7, 31))
    _, errs = al.clear_transaction(entry["id"], _d(2026, 7, 20))
    assert "conciliée" in errs[0]
    # After the floor: fine (the cross-period outstanding-cheque flow).
    cleared, errs = al.clear_transaction(entry["id"], _d(2026, 8, 5))
    assert errs == []


def test_encaissement_ignores_a_caller_dossier_id(store):
    """Revue 2026-08-13 : le champ caché du sélecteur de dossier survit au
    changement de type sur le formulaire — la FACTURE seule détermine le
    dossier d'un encaissement, sinon le registre attribue le paiement de B
    au dossier A."""
    store["dossiers"]["dosB"] = {"id": "dosB", "file_number": "2026-099",
                                 "title": "Autre c. Chose"}
    entry, errs = al.create_transaction(_encaissement(dossier_id="dosB"))
    assert errs == []
    assert entry["dossier_id"] == "dos1"            # the invoice's dossier
    assert entry["dossier_file_number"] == "2026-001"


def test_sum_invoice_receipts_excludes_a_reversed_compensee(store):
    """The bounced-cheque flow: reversing a COMPENSÉE encaissement leaves
    the original « compensée » (with reversed_by_id) while the payment was
    reduced — the recomputable cumulative must net it out too."""
    a, _ = al.create_transaction(_encaissement(amount=30000))
    al.clear_transaction(a["id"], _d(2026, 7, 12))
    assert al.sum_invoice_receipts("fac1") == 30000
    al.reverse_transaction(a["id"], "chèque sans provision", allow_linked=True)
    assert store["admin_transactions"][a["id"]]["status"] == "compensée"
    assert al.sum_invoice_receipts("fac1") == 0


def test_reverse_trust_linked_needs_the_flag(store):
    entry, _ = al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             trust_transaction_id="trust-tx-1")
    )
    _, errs = al.reverse_transaction(entry["id"], "essai")
    assert "fidéicommis" in errs[0]
    reversal, errs = al.reverse_transaction(entry["id"], "depuis le fidéicommis", allow_linked=True)
    assert errs == []


# ═══════════════════════════════════════════════════════════════════════════
# create_card_payment / delete_card_payment
# ═══════════════════════════════════════════════════════════════════════════


def test_card_payment_writes_two_linked_legs(store):
    leg, errs = al.create_card_payment("ops1", "carte1", 50000, _d(2026, 7, 15), "virement")
    assert errs == []
    txs = store["admin_transactions"]
    assert len(txs) == 2
    legs = list(txs.values())
    a = next(t for t in legs if t["account_id"] == "ops1")
    b = next(t for t in legs if t["account_id"] == "carte1")
    assert a["direction"] == "déboursé" and b["direction"] == "recette"
    assert a["related_transaction_id"] == b["id"]
    assert b["related_transaction_id"] == a["id"]
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == -50000
    assert store["admin_accounts"]["carte1"]["ledger_balance"] == 50000


def test_card_payment_refuses_wrong_account_types(store):
    _, errs = al.create_card_payment("carte1", "ops1", 50000, _d(2026, 7, 15), "virement")
    assert "compte d'opérations" in errs[0]
    _, errs = al.create_card_payment("ops1", "ops1", 50000, _d(2026, 7, 15), "virement")
    assert errs


def test_reverse_card_payment_reverses_both_legs(store):
    al.create_card_payment("ops1", "carte1", 50000, _d(2026, 7, 15), "virement")
    leg_id = next(iter(store["admin_transactions"]))
    reversal, errs = al.reverse_transaction(leg_id, "NSF")
    assert errs == []
    assert len(store["admin_transactions"]) == 4  # 2 originals + 2 reversals
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 0
    assert store["admin_accounts"]["carte1"]["ledger_balance"] == 0
    for t in store["admin_transactions"].values():
        assert t["status"] == "annulée"  # both en_circulation pairs annulled


def test_delete_card_payment_removes_both_legs(store):
    al.create_card_payment("ops1", "carte1", 50000, _d(2026, 7, 15), "virement")
    leg_id = next(iter(store["admin_transactions"]))
    # A single leg refuses the plain delete…
    _, errs = al.delete_transaction(leg_id)
    assert "jambes" in errs[0]
    # …the pair delete removes both and restores both ledgers.
    deleted, errs = al.delete_card_payment(leg_id)
    assert errs == []
    assert store["admin_transactions"] == {}
    assert store["admin_accounts"]["ops1"]["ledger_balance"] == 0
    assert store["admin_accounts"]["carte1"]["ledger_balance"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# attach_receipt
# ═══════════════════════════════════════════════════════════════════════════


def test_attach_receipt_sets_fields_and_returns_previous_path(store):
    entry, _ = al.create_transaction(_new())
    first, errs = al.attach_receipt(
        entry["id"], "users/u1/administration/t1/recu.pdf", "recu.pdf",
        "application/pdf", 1234,
    )
    assert errs == []
    assert first["_previous_receipt_path"] is None
    second, errs = al.attach_receipt(
        entry["id"], "users/u1/administration/t1/recu2.pdf", "recu2.pdf",
        "application/pdf", 999,
    )
    assert second["_previous_receipt_path"] == "users/u1/administration/t1/recu.pdf"
    stored = store["admin_transactions"][entry["id"]]
    assert stored["receipt_filename"] == "recu2.pdf"
    assert "_previous_receipt_path" not in stored


# ═══════════════════════════════════════════════════════════════════════════
# Read-time balances — list_register / opening / as-of
# ═══════════════════════════════════════════════════════════════════════════


def _seed_july(store):
    al.create_transaction(_new(direction="recette", kind="recette_autre", category="",
                               amount=500000, date=_d(2026, 6, 20)))
    al.create_transaction(_new(amount=100000, date=_d(2026, 7, 5)))
    al.create_transaction(_new(direction="recette", kind="recette_autre", category="",
                               amount=200000, date=_d(2026, 7, 15)))
    # Backdated insertion — arrives LAST, dated FIRST in July.
    al.create_transaction(_new(amount=50000, date=_d(2026, 7, 2)))


def test_list_register_orders_by_date_then_sequence(store):
    _seed_july(store)
    rows, truncated = al.list_register("ops1", _d(2026, 7, 1), _d(2026, 7, 31))
    assert not truncated
    assert [al._as_utc(r["date"]).day for r in rows] == [2, 5, 15]
    assert [r["sequence"] for r in rows] == [4, 2, 3]  # ledger order ≠ insertion order


def test_opening_and_running_balances_reconcile(store):
    _seed_july(store)
    opening, had_prior = al.opening_ledger_balance("ops1", _d(2026, 7, 1))
    assert (opening, had_prior) == (500000, True)
    rows, _ = al.list_register("ops1", _d(2026, 7, 1), _d(2026, 7, 31))
    balances = al.running_balances(rows, opening=opening)
    assert balances == [450000, 350000, 550000]
    assert balances[-1] == al.book_balance_as_of("ops1", _d(2026, 7, 31))


def test_opening_balance_distinguishes_zero_from_nothing(store):
    opening, had_prior = al.opening_ledger_balance("ops1", _d(2026, 7, 1))
    assert (opening, had_prior) == (0, False)


# ═══════════════════════════════════════════════════════════════════════════
# Reconciliation — the gate, the sign conversion, the lock it establishes
# ═══════════════════════════════════════════════════════════════════════════


def test_reconciliation_completes_at_zero_variance_and_locks_the_period(store):
    entry, _ = al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             amount=100000, date=_d(2026, 7, 10))
    )
    rec, errs = al.create_reconciliation("ops1", _d(2026, 7, 31), 100000)
    assert errs == []
    done, errs = al.complete_reconciliation(rec["id"], [entry["id"]])
    assert errs == []
    assert done["status"] == "complétée"
    assert done["book_balance"] == 100000
    stamped = store["admin_transactions"][entry["id"]]
    assert stamped["status"] == "compensée"
    assert stamped["reconciliation_id"] == rec["id"]
    # The lock is now in force.
    refused, errs = al.create_transaction(_new(date=_d(2026, 7, 20)))
    assert refused is None and "conciliation complétée" in errs[0]


def test_reconciliation_refuses_nonzero_variance(store):
    al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             amount=100000, date=_d(2026, 7, 10))
    )
    rec, _ = al.create_reconciliation("ops1", _d(2026, 7, 31), 99999)
    done, errs = al.complete_reconciliation(rec["id"], list(store["admin_transactions"]))
    assert done is None and "équilibrée" in errs[0]


def test_card_reconciliation_converts_the_statement_sign(store):
    """A card statement states the SOLDE DÛ. One 250 $ charge, statement says
    250 $ owed — the ledger reads −250 $, and the variance must still be 0."""
    entry, _ = al.create_transaction(
        _new(account_id="carte1", amount=25000, date=_d(2026, 7, 10),
             counterparty="Fournisseur SaaS", category="abonnements", method="carte")
    )
    rec, errs = al.create_reconciliation("carte1", _d(2026, 7, 31), 25000)
    assert errs == []
    done, errs = al.complete_reconciliation(rec["id"], [entry["id"]])
    assert errs == []
    assert done["book_balance"] == -25000  # ledger sign, snapshotted as-of


def test_reconciliation_unticked_entry_carries_to_the_next_period(store):
    a, _ = al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             amount=100000, date=_d(2026, 7, 10))
    )
    b, _ = al.create_transaction(_new(amount=30000, date=_d(2026, 7, 20)))
    # Statement shows only the deposit — the cheque is still outstanding.
    rec, _ = al.create_reconciliation("ops1", _d(2026, 7, 31), 100000)
    done, errs = al.complete_reconciliation(rec["id"], [a["id"]])
    assert errs == []
    assert store["admin_transactions"][b["id"]]["status"] == "en_circulation"
    # Next period: the cheque clears at the bank.
    ctx = al.reconciliation_as_of_context("ops1", _d(2026, 8, 12))
    assert [e["id"] for e in ctx["outstanding"]] == [b["id"]]
    rec2, _ = al.create_reconciliation("ops1", _d(2026, 8, 12), 70000)
    done2, errs = al.complete_reconciliation(rec2["id"], [b["id"]])
    assert errs == []


def test_retroactive_context_resurrects_later_cleared_and_annulled(store):
    a, _ = al.create_transaction(_new(amount=40000, date=_d(2026, 7, 10)))
    b, _ = al.create_transaction(_new(amount=10000, date=_d(2026, 7, 12)))
    # a clears AFTER the period; b is reversed AFTER the period.
    al.clear_transaction(a["id"], _d(2026, 8, 5))
    al.reverse_transaction(b["id"], "erreur", reversal_date=_d(2026, 8, 6))
    ctx = al.reconciliation_as_of_context("ops1", _d(2026, 7, 31))
    assert [e["id"] for e in ctx["cleared_later"]] == [a["id"]]
    assert [e["id"] for e in ctx["annulled_later"]] == [b["id"]]
    assert ctx["fixed_outstanding_total"] == 50000
    assert ctx["outstanding"] == [] and ctx["in_transit"] == []


def test_reconciliation_guards_future_double_and_ordering(store):
    _, errs = al.create_reconciliation("ops1", datetime.now(timezone.utc) + timedelta(days=3), 0)
    assert "futur" in errs[0]
    _, errs = al.create_reconciliation("ops1", _d(2026, 7, 31), None)
    assert "requis" in errs[0]
    rec, errs = al.create_reconciliation("ops1", _d(2026, 7, 31), 0)  # literal 0 legitimate
    assert errs == []
    _, errs = al.create_reconciliation("ops1", _d(2026, 8, 10), 0)
    assert "déjà en cours" in errs[0]
    al.delete_reconciliation(rec["id"])
    done, errs = al.create_reconciliation("ops1", _d(2026, 7, 31), 0)
    assert errs == []


def test_abandon_refuses_a_completed_reconciliation(store):
    entry, _ = al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             amount=100000, date=_d(2026, 7, 10))
    )
    rec, _ = al.create_reconciliation("ops1", _d(2026, 7, 31), 100000)
    al.complete_reconciliation(rec["id"], [entry["id"]])
    ok, errs = al.delete_reconciliation(rec["id"])
    assert not ok and "complétée" in errs[0]


# ═══════════════════════════════════════════════════════════════════════════
# Linkage queries + firm snapshot
# ═══════════════════════════════════════════════════════════════════════════


def test_sum_invoice_receipts_excludes_annulled(store):
    a, _ = al.create_transaction(_encaissement(amount=30000))
    store["invoices"]["fac1"]["amount_paid"] = 30000
    b, _ = al.create_transaction(_encaissement(amount=20000))
    assert al.sum_invoice_receipts("fac1") == 50000
    al.reverse_transaction(b["id"], "erreur", allow_linked=True)
    assert al.sum_invoice_receipts("fac1") == 30000


def test_list_invoice_receipts_rend_les_lignes_pas_le_total(store):
    a, _ = al.create_transaction(_encaissement(amount=30000))
    store["invoices"]["fac1"]["amount_paid"] = 30000
    b, _ = al.create_transaction(_encaissement(amount=20000))
    rows = al.list_invoice_receipts("fac1")
    assert [r["id"] for r in rows] == [a["id"], b["id"]]
    assert [r["amount"] for r in rows] == [30000, 20000]


def test_list_invoice_receipts_garde_les_contrepassees(store):
    """La différence voulue avec sum_invoice_receipts : le total doit écarter
    ce dont l'effet économique ne tient plus, mais la FICHE doit montrer ce
    qui s'est passé — une contre-passation fait partie de l'histoire, et la
    cacher laisserait le lecteur sans explication du mouvement du solde."""
    a, _ = al.create_transaction(_encaissement(amount=30000))
    store["invoices"]["fac1"]["amount_paid"] = 30000
    al.reverse_transaction(a["id"], "chèque sans provision", allow_linked=True)

    assert al.sum_invoice_receipts("fac1") == 0          # le cumul les écarte
    rows = al.list_invoice_receipts("fac1")
    assert len(rows) >= 1                                 # la liste les garde
    assert any(r.get("reversed_by_id") or r.get("status") == "annulée"
               for r in rows)


def test_list_invoice_receipts_est_ordonnee_du_plus_ancien(store):
    tardif, _ = al.create_transaction(_encaissement(amount=10000, date=_d(2026, 3, 9)))
    store["invoices"]["fac1"]["amount_paid"] = 10000
    ancien, _ = al.create_transaction(_encaissement(amount=5000, date=_d(2026, 1, 4)))
    rows = al.list_invoice_receipts("fac1")
    assert [r["id"] for r in rows] == [ancien["id"], tardif["id"]]


def test_list_invoice_receipts_echoue_ouvert(store, monkeypatch):
    """Aide à l'affichage, jamais le registre : une lecture ratée ne doit pas
    emporter la fiche de la facture. sum_invoice_receipts, elle, garde son
    fail-closed — on projette de l'argent depuis elle."""
    class _Boom:
        def collection(self, _name):
            raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(al, "db", _Boom())
    assert al.list_invoice_receipts("fac1") == []


def test_list_invoice_receipts_sans_identifiant_ne_requete_pas(store):
    assert al.list_invoice_receipts("") == []


def test_find_by_trust_transaction(store):
    al.create_transaction(
        _new(direction="recette", kind="recette_autre", category="",
             trust_transaction_id="ttx9")
    )
    found = al.find_by_trust_transaction("ttx9")
    assert found and found["trust_transaction_id"] == "ttx9"
    assert al.find_by_trust_transaction("absent") is None


def test_firm_snapshot_display_balances_and_overdue(store):
    al.create_transaction(_new(amount=25000))  # ops ledger −250 $
    al.create_transaction(
        _new(account_id="carte1", amount=40000, method="carte", category="abonnements")
    )
    snap = al.get_firm_admin_snapshot()
    by_id = {a["id"]: a for a in snap["accounts"]}
    assert by_id["ops1"]["display_balance"] == -25000
    assert by_id["ops1"]["balance_label"] == "Solde"
    assert by_id["carte1"]["display_balance"] == 40000  # solde dû, positive
    assert by_id["carte1"]["balance_label"] == "Solde dû"
    # Never reconciled + no created_at floor → overdue.
    assert snap["reconciliation_overdue"] is True
