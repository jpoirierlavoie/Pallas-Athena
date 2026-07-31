"""Lot P — l'encaissement enregistré sur une facture.

Avant ce lot, un paiement n'était représentable QUE par le statut
« payée » : aucun montant, aucune date, et un paiement partiel était
inexprimable. Le piège du champ voisin est resté intact et est documenté
ici : ``amount_due`` est FIGÉ à l'émission et demeure non nul sur une
facture réglée — ce n'est pas un solde, malgré son nom. Le solde vivant
est ``balance_of`` = amount_due − amount_paid, dérivé, jamais stocké.

La bascule automatique à « payée » (décision de l'avocat) porte un piège
propre : « payée » est TERMINAL dans STATUS_TRANSITIONS, donc une saisie
erronée immobiliserait la facture. record_payment annule sa PROPRE bascule
— et seulement la sienne. Ces deux moitiés sont épinglées séparément.

Firestore est bouchonné : la transaction est simulée par un faux document.
"""

import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import invoice as imod

UTC = timezone.utc


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Ref:
    def __init__(self, store):
        self.store = store

    def get(self, transaction=None):
        return _Snap(self.store.get("doc"))


class _Txn:
    """Records the update instead of writing — an aborted transaction must
    therefore leave `applied` empty, which is what the refusal tests check."""

    def __init__(self, store):
        self.store = store

    def update(self, ref, updates):
        self.store.setdefault("applied", []).append(updates)
        self.store["doc"] = {**self.store["doc"], **updates}


@pytest.fixture()
def store(monkeypatch):
    box: dict = {}

    monkeypatch.setattr(
        imod.db, "collection",
        lambda name: mock.Mock(document=lambda i: _Ref(box)),
    )
    monkeypatch.setattr(imod.db, "transaction", lambda: _Txn(box))
    # firestore.transactional wraps the function; call it straight through.
    monkeypatch.setattr(imod.firestore, "transactional", lambda fn: fn)
    return box


def _invoice(**over):
    doc = {
        "id": "inv1", "invoice_number": "2026-001-01", "status": "envoyée",
        "total": 287437, "retainer_applied": 0, "amount_due": 287437,
        "amount_paid": 0, "paid_date": None,
    }
    doc.update(over)
    return doc


# ── balance_of: the derived figure ──────────────────────────────────────


def test_balance_is_derived_not_the_frozen_amount_due():
    """amount_due is what was due AT ISSUANCE and is never updated — on a
    paid invoice it is still the full figure. Reading it as a balance is
    the mistake this helper exists to prevent."""
    paid = _invoice(amount_paid=287437)
    assert paid["amount_due"] == 287437        # unchanged, by design
    assert imod.balance_of(paid) == 0          # the truth

    partial = _invoice(amount_paid=150000)
    assert imod.balance_of(partial) == 137437

    assert imod.balance_of(_invoice()) == 287437


def test_balance_accounts_for_a_retainer():
    """A retainer reduces amount_due at creation; the balance follows it."""
    inv = _invoice(total=287437, retainer_applied=100000, amount_due=187437,
                   amount_paid=87437)
    assert imod.balance_of(inv) == 100000


# ── record_payment: the nominal path ────────────────────────────────────


def test_partial_payment_leaves_the_invoice_open(store):
    store["doc"] = _invoice()
    updated, errors = imod.record_payment(
        "inv1", 150000, datetime(2026, 6, 15, tzinfo=UTC))
    assert errors == []
    assert updated["amount_paid"] == 150000
    assert updated["paid_date"] == datetime(2026, 6, 15, tzinfo=UTC)
    assert updated["status"] == "envoyée"      # NOT flipped
    assert imod.balance_of(updated) == 137437


def test_full_payment_flips_the_status(store):
    store["doc"] = _invoice()
    updated, errors = imod.record_payment(
        "inv1", 287437, datetime(2026, 7, 2, tzinfo=UTC))
    assert errors == []
    assert updated["status"] == "payée"
    assert imod.balance_of(updated) == 0


def test_a_late_invoice_can_also_be_paid(store):
    store["doc"] = _invoice(status="en_retard")
    updated, _ = imod.record_payment("inv1", 287437)
    assert updated["status"] == "payée"


def test_the_etag_and_updated_at_move_on_every_payment(store):
    store["doc"] = _invoice(etag="old")
    updated, _ = imod.record_payment("inv1", 1000)
    assert updated["etag"] != "old"
    assert updated["updated_at"] is not None


# ── The terminal-status trap, and its narrow undo ───────────────────────


def test_correcting_a_payment_downward_undoes_the_flip(store):
    """« payée » is TERMINAL: update_status refuses to leave it. Without
    this undo, one mistyped amount would strand the invoice for good."""
    store["doc"] = _invoice()
    imod.record_payment("inv1", 287437)                 # oops, the full sum
    assert store["doc"]["status"] == "payée"

    updated, errors = imod.record_payment("inv1", 150000)   # the correction
    assert errors == []
    assert updated["status"] == "envoyée"
    assert imod.balance_of(updated) == 137437


def test_clearing_a_payment_entirely_also_clears_the_date(store):
    """A zero payment with a stale date would read « paid on that day » on
    an invoice carrying no payment at all."""
    store["doc"] = _invoice()
    imod.record_payment("inv1", 287437, datetime(2026, 7, 2, tzinfo=UTC))
    updated, _ = imod.record_payment("inv1", 0)
    assert updated["amount_paid"] == 0
    assert updated["paid_date"] is None
    assert updated["status"] == "envoyée"


def test_a_hand_set_payee_status_is_never_undone(store):
    """The undo is narrow ON PURPOSE: it reverses only a flip this function
    could have made. « payée » set by hand, with no payment recorded, is the
    lawyer's own statement — recording a partial payment against it must not
    silently contradict him."""
    store["doc"] = _invoice(status="payée", amount_paid=0)
    updated, errors = imod.record_payment("inv1", 150000)
    assert errors == []
    assert updated["status"] == "payée"        # untouched
    assert updated["amount_paid"] == 150000    # but the figure is recorded


# ── Refusals: nothing is written ────────────────────────────────────────


def _refused(store, *args, **kwargs):
    updated, errors = imod.record_payment(*args, **kwargs)
    assert updated is None
    assert errors and errors[0]
    assert not store.get("applied"), "a refused payment wrote to the document"
    return errors[0]


def test_a_payment_larger_than_the_total_is_refused(store):
    store["doc"] = _invoice()
    assert "dépasser le total" in _refused(store, "inv1", 400000)


def test_a_negative_payment_is_refused(store):
    store["doc"] = _invoice()
    assert "négatif" in _refused(store, "inv1", -1)


def test_a_non_numeric_payment_is_refused(store):
    store["doc"] = _invoice()
    assert "entier" in _refused(store, "inv1", "beaucoup")


def test_a_cancelled_invoice_refuses_payment(store):
    store["doc"] = _invoice(status="annulée")
    assert "annulée" in _refused(store, "inv1", 1000)


def test_a_draft_invoice_refuses_payment(store):
    """Recording money against an unissued invoice would mint a receivable
    the client was never asked for."""
    store["doc"] = _invoice(status="brouillon")
    assert "brouillon" in _refused(store, "inv1", 1000)


def test_an_unknown_invoice_is_refused(store):
    store["doc"] = None
    assert "introuvable" in _refused(store, "nope", 1000)


# ── The default document ────────────────────────────────────────────────


def test_new_invoices_carry_the_payment_fields_unset():
    doc = imod._default_doc()
    assert doc["amount_paid"] == 0
    assert doc["paid_date"] is None


def test_existing_invoices_read_as_unpaid_without_a_backfill():
    """No backfill was run (the lawyer enters the real payments by hand), so
    a legacy document has NEITHER key. balance_of must not raise on it."""
    legacy = {"id": "old", "total": 100000, "amount_due": 100000}
    assert imod.balance_of(legacy) == 100000
