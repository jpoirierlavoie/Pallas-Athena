"""Lot P — l'encaissement enregistré sur une facture.

Avant ce lot, un paiement n'était représentable QUE par le statut
« payée » : aucun montant, aucune date, et un paiement partiel était
inexprimable. Le piège du champ voisin est resté intact et est documenté
ici : ``amount_due`` est FIGÉ à l'émission et demeure non nul sur une
facture réglée — ce n'est pas un solde, malgré son nom. Le solde vivant
est ``balance_of`` = amount_due − amount_paid, dérivé, jamais stocké.

La bascule automatique à « payée » (décision de l'avocat) porte un piège
propre : une saisie erronée immobiliserait la facture. record_payment annule
sa PROPRE bascule — et seulement la sienne. Ces deux moitiés sont épinglées
séparément.

Depuis le 2026-08-17 « payée » n'est plus un cul-de-sac, mais l'annulation
étroite reste nécessaire : available_transitions referme la sortie manuelle
dès qu'un paiement est INSCRIT, si bien que pour le SEUL statut que
record_payment sait poser, elle demeure l'unique chemin de retour. Le dernier
bloc du fichier épingle ce nouveau sens.

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

    def update(self, updates):
        """Le chemin NON transactionnel — update_status écrit par ici."""
        self.store.setdefault("applied", []).append(updates)
        self.store["doc"] = {**self.store["doc"], **updates}


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


# ── The closed-status trap, and its narrow undo ─────────────────────────


def test_correcting_a_payment_downward_undoes_the_flip(store):
    """Une facture PORTANT un paiement ne se rouvre pas à la main
    (available_transitions le refuse — la voie est la contre-passation), donc
    sans cette annulation un montant erroné immobiliserait la facture."""
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


def test_a_payment_larger_than_the_balance_due_is_refused(store):
    store["doc"] = _invoice()
    assert "dépasser le solde dû" in _refused(store, "inv1", 400000)


def test_the_cap_is_the_balance_due_not_the_invoice_total(store):
    """With a retainer applied, amount_due < total. Capping on `total` let a
    payment land between the two and produced a NEGATIVE balance with
    nothing to explain it — found by the lot-4 review."""
    store["doc"] = _invoice(total=287437, retainer_applied=100000,
                            amount_due=187437)
    # Between the balance due and the total: refused, nothing written.
    assert "dépasser le solde dû" in _refused(store, "inv1", 250000)
    # Exactly the balance due: accepted, and it settles the invoice.
    updated, errors = imod.record_payment("inv1", 187437)
    assert errors == []
    assert imod.balance_of(updated) == 0
    assert updated["status"] == "payée"


def test_the_balance_can_never_go_negative(store):
    """The property the cap exists to guarantee."""
    store["doc"] = _invoice(total=287437, retainer_applied=50000,
                            amount_due=237437)
    for amount in (0, 1, 100000, 237437):
        updated, errors = imod.record_payment("inv1", amount)
        assert errors == [], amount
        assert imod.balance_of(updated) >= 0, amount


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


# ═══════════════════════════════════════════════════════════════════════════
# Le sens du statut s'inverse (2026-08-17) : la comptabilité pose « payée »,
# la main la retire.
# ═══════════════════════════════════════════════════════════════════════════


def test_la_table_des_transitions_est_une_doctrine_pas_un_detail():
    """Elle est épinglée LITTÉRALEMENT : chaque case dit qui a le droit de
    déclarer qu'une facture est réglée. « payée » n'est plus une cible, et
    n'est plus un cul-de-sac."""
    assert imod.STATUS_TRANSITIONS == {
        "brouillon": ("envoyée", "annulée"),
        "envoyée": ("en_retard", "annulée"),
        "en_retard": ("envoyée", "annulée"),
        "payée": ("envoyée",),
    }


def test_marquer_payee_a_la_main_est_refuse(store):
    """C'était la porte que le retrait du formulaire d'encaissement venait de
    fermer : un statut affirmant un paiement, sans montant, sans date,
    invisible au grand livre."""
    store["doc"] = _invoice(status="envoyée")
    ok, err = imod.update_status("inv1", "payée")
    assert ok is False
    assert "registre d'administration" in err
    assert not store.get("applied"), "un refus a écrit"


def test_la_bascule_automatique_survit_au_retrait(store):
    """LA moitié qui aurait cassé en silence. record_payment écrit `status`
    directement dans sa transaction, sans passer par la table — retirer
    « payée » du menu manuel ne peut donc pas l'empêcher de fonctionner."""
    store["doc"] = _invoice()
    updated, errors = imod.record_payment("inv1", 287437)
    assert errors == []
    assert updated["status"] == "payée"


def test_une_payee_posee_a_la_main_se_rouvre(store):
    """Les dix-neuf factures « payée » sans montant de la production : elles
    doivent pouvoir revenir impayées, sans quoi aucun encaissement ne peut
    plus s'y porter (create_transaction refuse tout statut hors envoyée /
    en_retard)."""
    store["doc"] = _invoice(status="payée", amount_paid=0)
    ok, err = imod.update_status("inv1", "envoyée")
    assert ok is True and err == ""
    assert store["doc"]["status"] == "envoyée"


def test_une_payee_adossee_au_grand_livre_ne_se_rouvre_pas(store):
    """Le piège que la réouverture ouvrirait : void_invoice ne refuse QUE le
    statut « payée », jamais un montant encaissé. Sans ce garde,
    « Rouvrir » puis « Annuler » libérerait les heures d'une facture
    réellement encaissée — et rien ne l'attraperait, l'annulation ne touchant
    pas amount_paid."""
    store["doc"] = _invoice(status="payée", amount_paid=287437)
    ok, err = imod.update_status("inv1", "envoyée")
    assert ok is False
    assert "Contre-passez" in err and "Administration" in err
    assert not store.get("applied"), "un refus a écrit"


def test_available_transitions_est_la_seule_autorite():
    """La fiche et update_status lisent la MÊME fonction : un bouton qui
    s'afficherait pour être refusé serait un défaut de conception."""
    posee_a_la_main = _invoice(status="payée", amount_paid=0)
    adossee = _invoice(status="payée", amount_paid=287437)
    assert imod.available_transitions(posee_a_la_main) == ("envoyée",)
    assert imod.available_transitions(adossee) == ()
    # Et elle n'invente rien pour les autres statuts.
    assert imod.available_transitions(_invoice(status="envoyée")) == (
        "en_retard", "annulée")


def test_en_retard_garde_une_sortie_non_destructive(store):
    """Rien n'écrit « en retard » automatiquement : ce statut se pose à la
    main, donc une erreur doit se corriger autrement qu'en annulant la
    facture (ce qui libérerait toutes ses heures)."""
    store["doc"] = _invoice(status="en_retard")
    ok, err = imod.update_status("inv1", "envoyée")
    assert ok is True and err == ""


def test_le_chemin_payee_envoyee_annulee_reste_ferme_sur_une_facture_encaissee(store):
    """Bout en bout : la seule voie vers l'annulation d'une facture encaissée
    passerait par sa réouverture, et elle est fermée. void_invoice n'a jamais
    eu à regarder l'argent, et n'a toujours pas à le faire."""
    store["doc"] = _invoice(status="payée", amount_paid=287437)
    assert "envoyée" not in imod.available_transitions(store["doc"])
    ok, _ = imod.void_invoice("inv1")
    assert ok is False        # void refuse « payée » — la porte de derrière
