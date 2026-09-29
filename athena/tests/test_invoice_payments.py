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
        """Le chemin NON transactionnel. update_status y écrivait jusqu'au
        2026-09-26 ; il écrit désormais par _Txn.update, comme
        record_payment. Gardé : une écriture hors transaction qui
        reviendrait remplirait quand même `applied`, et les assertions
        « un refus a écrit » la verraient."""
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
    """Le piège que la réouverture ouvrirait : « Rouvrir » puis « Annuler »
    libérerait les heures d'une facture réellement encaissée. Jusqu'au
    2026-09-26 ce garde était le SEUL rempart — void_invoice ne refusait que
    le statut « payée », jamais un montant encaissé. Il refuse désormais
    l'argent lui-même (tests/test_invoice_lifecycle.py) ; la sortie reste
    fermée ici quand même, parce qu'un bouton qui s'affiche pour être refusé
    est un défaut de conception."""
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
    """Bout en bout : la voie « Rouvrir » vers l'annulation d'une facture
    encaissée est fermée. (Réécrit le 2026-09-26 : la docstring affirmait que
    void_invoice « n'a toujours pas à regarder l'argent ». C'était faux dès
    qu'une facture ENVOYÉE portait un paiement partiel — void l'annulait et
    libérait ses heures, le paiement restant sur la facture annulée. Il
    regarde désormais l'argent ; tests/test_invoice_lifecycle.py l'épingle.)"""
    store["doc"] = _invoice(status="payée", amount_paid=287437)
    assert "envoyée" not in imod.available_transitions(store["doc"])
    ok, _ = imod.void_invoice("inv1")
    assert ok is False        # void refuse « payée » — la porte de derrière


# ═══════════════════════════════════════════════════════════════════════════
# Lot 5a — payment_updates, la SEULE computation d'un paiement, et
# get_invoices_by_number, la résolution stricte d'une facture par son numéro.
#
# Ce bloc tourne sur le faux Firestore PARTAGÉ (tests/_fake_firestore.py) : le
# client, ses transactions et la boucle de reprise de `transactional` sont les
# vrais, et l'on relit ce qui est STOCKÉ — pas un dict remis à un bouchon.
#
# La matrice est une TABLE ÉCRITE À LA MAIN, pas une comparaison entre deux
# chemins du même code : record_payment tourne désormais SUR payment_updates,
# si bien qu'« il donne le même résultat que lui » serait vrai par
# construction. La table est la sémantique de record_payment AVANT
# l'extraction ; la moitié record_payment de la matrice a été rejouée contre
# l'ancien models/invoice.py (commit 9e89e54) et y passe à l'identique.
# ═══════════════════════════════════════════════════════════════════════════

from tests._fake_firestore import install  # noqa: E402

PAID_ON = datetime(2026, 9, 15, tzinfo=UTC)

# The exact refusals — pinned: a ledger transaction will relay them to the
# lawyer and to the connector alike.
_DRAFT = ("Cette facture est encore un brouillon : envoyez-la avant "
          "d'y porter un encaissement.")
_VOIDED = "Cette facture est annulée : aucun encaissement ne peut y être porté."
_CAP = ("Le montant encaissé ne peut pas dépasser le solde dû (1000.00 $). "
        "Pour un trop-perçu, portez-le au fidéicommis plutôt qu'à la facture.")
_REDUCE = ("Une réduction d'encaissement ne peut pas dépasser le montant "
           "déjà encaissé sur la facture.")

# A retainer invoice: total 1 200 $, provision 200 $, amount_due 1 000 $ — so
# « more than due but within the total » is a row of its own.
_STATES = {
    "brouillon": {"status": "brouillon", "amount_paid": 0},
    "envoyée": {"status": "envoyée", "amount_paid": 0},
    "en_retard": {"status": "en_retard", "amount_paid": 0},
    # « payée » set by HAND (nothing recorded) — the lawyer's statement.
    "payée_main": {"status": "payée", "amount_paid": 0},
    # « payée » the ledger set (a payment recorded) — its flip is undoable.
    "payée_inscrite": {"status": "payée", "amount_paid": 100000,
                       "paid_date": datetime(2026, 9, 1, tzinfo=UTC)},
    "annulée": {"status": "annulée", "amount_paid": 0},
}
_AMOUNTS = {"zéro": 0, "partiel": 40000, "solde_dû": 100000, "au_delà_du_dû": 110000}

# (state, amount) → the stored status after, or the exact refusal.
_EXPECTED = {
    ("brouillon", "zéro"): _DRAFT,
    ("brouillon", "partiel"): _DRAFT,
    ("brouillon", "solde_dû"): _DRAFT,
    ("brouillon", "au_delà_du_dû"): _DRAFT,
    ("envoyée", "zéro"): "envoyée",
    ("envoyée", "partiel"): "envoyée",
    ("envoyée", "solde_dû"): "payée",
    ("envoyée", "au_delà_du_dû"): _CAP,
    ("en_retard", "zéro"): "en_retard",
    ("en_retard", "partiel"): "en_retard",
    ("en_retard", "solde_dû"): "payée",
    ("en_retard", "au_delà_du_dû"): _CAP,
    ("payée_main", "zéro"): "payée",
    ("payée_main", "partiel"): "payée",
    ("payée_main", "solde_dû"): "payée",
    ("payée_main", "au_delà_du_dû"): _CAP,
    ("payée_inscrite", "zéro"): "envoyée",
    ("payée_inscrite", "partiel"): "envoyée",
    ("payée_inscrite", "solde_dû"): "payée",
    ("payée_inscrite", "au_delà_du_dû"): _CAP,
    ("annulée", "zéro"): _VOIDED,
    ("annulée", "partiel"): _VOIDED,
    ("annulée", "solde_dû"): _VOIDED,
    ("annulée", "au_delà_du_dû"): _VOIDED,
}

_MATRIX = sorted(_EXPECTED)
_STAMP_KEYS = {"etag", "updated_at", "updated_via", "mcp_updated_at"}


def _seeded(state: str) -> dict:
    doc = {
        "id": "inv1", "invoice_number": "2026-F031", "dossier_id": "dos1",
        "total": 120000, "retainer_applied": 20000, "amount_due": 100000,
        "paid_date": None, "etag": "e0",
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    doc.update(_STATES[state])
    return doc


@pytest.fixture
def real_store(monkeypatch):
    return install(monkeypatch, imod)


def _is_refusal(expected: str) -> bool:
    return expected not in ("envoyée", "en_retard", "payée")


def _core(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in _STAMP_KEYS}


@pytest.mark.parametrize("state, amount_key", _MATRIX)
def test_record_payment_matrice_sur_le_vrai_magasin(real_store, state, amount_key):
    """record_payment × {6 états} × {4 montants}, relu au magasin. Rejouée
    contre le models/invoice.py d'avant l'extraction : identique."""
    seed = _seeded(state)
    real_store.seed("invoices/inv1", seed)
    amount = _AMOUNTS[amount_key]
    expected = _EXPECTED[(state, amount_key)]

    updated, errors = imod.record_payment("inv1", amount, PAID_ON)
    stored = real_store.peek("invoices/inv1")

    if _is_refusal(expected):
        assert (updated, errors) == (None, [expected])
        assert stored == seed                                  # nothing moved
        assert [c for c in real_store.commits if c.ops] == []  # nothing committed
        return
    assert errors == []
    assert stored["status"] == expected
    assert stored["amount_paid"] == amount
    assert stored["paid_date"] == (PAID_ON if amount > 0 else None)
    assert stored["etag"] != "e0" and stored["updated_at"] > seed["updated_at"]
    # Only the payment fields and the stamp moved.
    moved = {k for k in stored if stored[k] != seed.get(k)}
    assert moved <= {"amount_paid", "paid_date", "status"} | _STAMP_KEYS
    assert _core(updated) == _core(stored)


@pytest.mark.parametrize("state, amount_key", _MATRIX)
def test_payment_updates_suit_la_meme_table_sans_toucher_au_magasin(
    real_store, state, amount_key
):
    """La fonction pure rend la même table — sans une seule lecture ni
    écriture (aucun RPC au magasin), et sans modifier la facture reçue."""
    invoice = _seeded(state)
    before = dict(invoice)
    now = datetime(2026, 9, 20, 14, tzinfo=UTC)
    amount = _AMOUNTS[amount_key]
    expected = _EXPECTED[(state, amount_key)]

    if _is_refusal(expected):
        with pytest.raises(imod.PaymentRefused) as refusal:
            imod.payment_updates(invoice, amount, PAID_ON, now=now)
        assert str(refusal.value) == expected
    else:
        updates = imod.payment_updates(invoice, amount, PAID_ON, now=now)
        assert updates.get("status", invoice["status"]) == expected
        assert updates["amount_paid"] == amount
        assert updates["paid_date"] == (PAID_ON if amount > 0 else None)
        assert updates["updated_at"] == now
        assert updates["etag"] and updates["etag"] != "e0"
        assert set(updates) <= {"amount_paid", "paid_date", "status"} | _STAMP_KEYS
    assert invoice == before
    assert real_store.reads == [] and real_store.commits == []


@pytest.mark.parametrize(
    "state, amount_key",
    [key for key in _MATRIX if not _is_refusal(_EXPECTED[key])],
)
def test_record_payment_ecrit_exactement_ce_que_payment_updates_calcule(
    real_store, state, amount_key
):
    """La couture : ce qui est STOCKÉ est la facture lue plus les champs de
    payment_updates — rien d'autre, rien de moins (le tampon mis à part)."""
    seed = _seeded(state)
    real_store.seed("invoices/inv1", seed)
    amount = _AMOUNTS[amount_key]
    imod.record_payment("inv1", amount, PAID_ON)
    computed = imod.payment_updates(
        seed, amount, PAID_ON, now=datetime(2026, 9, 20, tzinfo=UTC))
    assert _core(real_store.peek("invoices/inv1")) == _core({**seed, **computed})


def test_une_reduction_sur_une_facture_annulee_passe_et_laisse_son_statut():
    """reducing=True : un encaissement contre-passé doit pouvoir reprendre
    ce qui avait été porté sur une facture annulée depuis — sans en toucher
    le statut."""
    invoice = {**_seeded("annulée"), "amount_paid": 40000,
               "paid_date": datetime(2026, 9, 1, tzinfo=UTC)}
    now = datetime(2026, 9, 20, tzinfo=UTC)
    updates = imod.payment_updates(invoice, 0, invoice["paid_date"],
                                   now=now, reducing=True)
    assert updates["amount_paid"] == 0 and updates["paid_date"] is None
    assert "status" not in updates
    updates = imod.payment_updates(invoice, 10000, invoice["paid_date"],
                                   now=now, reducing=True)
    assert updates["amount_paid"] == 10000
    assert updates["paid_date"] == invoice["paid_date"]
    assert "status" not in updates
    # Without the flag, the same write is refused as before.
    with pytest.raises(imod.PaymentRefused) as refusal:
        imod.payment_updates(invoice, 10000, invoice["paid_date"], now=now)
    assert str(refusal.value) == _VOIDED


def test_une_reduction_n_augmente_jamais_l_encaisse_ni_n_ouvre_un_brouillon():
    now = datetime(2026, 9, 20, tzinfo=UTC)
    voided = {**_seeded("annulée"), "amount_paid": 40000}
    with pytest.raises(imod.PaymentRefused) as refusal:
        imod.payment_updates(voided, 50000, None, now=now, reducing=True)
    assert str(refusal.value) == _REDUCE
    with pytest.raises(imod.PaymentRefused) as refusal:
        imod.payment_updates(_seeded("brouillon"), 0, None, now=now, reducing=True)
    assert str(refusal.value) == _DRAFT


def test_une_reduction_d_une_payee_inscrite_defait_la_bascule():
    updates = imod.payment_updates(
        _seeded("payée_inscrite"), 60000, PAID_ON,
        now=datetime(2026, 9, 20, tzinfo=UTC), reducing=True)
    assert updates["status"] == "envoyée" and updates["amount_paid"] == 60000


@pytest.mark.parametrize("bad", [True, 12.5, "40000", None, -1])
def test_payment_updates_refuse_ce_qui_n_est_pas_des_cents_positifs(bad):
    """record_payment convertit sa saisie avant d'appeler ; un appelant qui
    stage dans SA transaction passe des cents entiers, ou il est refusé —
    jamais tronqué en silence."""
    with pytest.raises(imod.PaymentRefused):
        imod.payment_updates(_seeded("envoyée"), bad, None,
                             now=datetime(2026, 9, 20, tzinfo=UTC))


def test_payment_refused_garde_son_ancien_nom():
    assert imod._PaymentRefused is imod.PaymentRefused


# ── get_invoices_by_number : strict, et chaque correspondance ────────────


def test_get_invoices_by_number_rend_chaque_correspondance(real_store):
    real_store.seed("invoices/a", {"id": "a", "invoice_number": "2026-F031",
                                   "dossier_id": "dos1"})
    real_store.seed("invoices/b", {"id": "b", "invoice_number": "2026-F031",
                                   "dossier_id": "dos2"})
    real_store.seed("invoices/c", {"id": "c", "invoice_number": "2026-F032",
                                   "dossier_id": "dos1"})
    found = imod.get_invoices_by_number("  2026-F031 ")
    assert sorted(i["id"] for i in found) == ["a", "b"]
    assert imod.get_invoices_by_number("2026-F099") == []


def test_get_invoices_by_number_prend_l_id_du_document(real_store):
    """L'id du DOCUMENT fait foi : c'est par lui qu'un appelant écrira."""
    real_store.seed("invoices/vrai", {"invoice_number": "2026-F040"})
    assert imod.get_invoices_by_number("2026-F040")[0]["id"] == "vrai"


def test_get_invoices_by_number_vide_ne_lit_rien(real_store):
    assert imod.get_invoices_by_number("") == []
    assert imod.get_invoices_by_number("   ") == []
    assert imod.get_invoices_by_number(None) == []
    assert real_store.reads == []


def test_get_invoices_by_number_propage_une_panne_de_lecture(real_store, monkeypatch):
    """La défaillance que la résolution par list_invoices avalait : une panne
    se lisait « Aucune facture ». Ici elle REMONTE — l'appelant décide."""
    real_store.seed("invoices/a", {"id": "a", "invoice_number": "2026-F031",
                                   "dossier_id": "dos1"})

    def _boom(*_a, **_kw):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(real_store._fake_server, "run_query", _boom)
    with pytest.raises(RuntimeError):
        imod.get_invoices_by_number("2026-F031")
    # The contrast that motivated it: the fail-open lister hides the outage.
    assert imod.list_invoices(dossier_id="dos1") == []


# ── La lecture de la facture est TRANSACTIONNELLE (revue du lot 5a) ─────
#
# payment_updates calcule le refus, le plafond et la bascule à partir de la
# facture qu'on lui remet ; il ne vaut que si cette facture a été lue DANS la
# transaction qui écrit. C'est la propriété que l'étape 5 réutilise pour
# porter le paiement dans la transaction du registre : une extraction qui
# lirait la facture hors transaction (get_invoice, puis le calcul, puis
# l'écriture) garderait la même matrice et perdrait la sérialisation. Ces
# deux épingles la fixent sur le vrai client (boucle de reprise comprise).


def test_record_payment_ne_lit_la_facture_que_dans_sa_transaction(real_store):
    real_store.seed("invoices/inv1", _seeded("envoyée"))
    imod.record_payment("inv1", 40000, PAID_ON)
    assert real_store.reads_outside_transactions() == []
    assert any(r.transactional and "invoices/inv1" in r.paths for r in real_store.reads)


def test_une_annulation_validee_pendant_l_encaissement_le_fait_refuser(real_store):
    """Une annulation (le formulaire web, ou Claude) est validée entre la
    lecture de la facture et le commit de l'encaissement. Le commit avorte,
    la transaction relit : la facture est annulée, le paiement est refusé —
    jamais porté sur une facture dont les sources viennent d'être libérées."""
    seed = _seeded("envoyée")
    real_store.seed("invoices/inv1", seed)
    fired = []

    def concurrent_void(info):
        if not fired and ("update", "invoices/inv1") in info.ops:
            fired.append(info.index)
            real_store.external_write(
                "invoices/inv1", {**seed, "status": "annulée", "etag": "void"})

    remove = real_store.add_commit_hook(concurrent_void)
    try:
        updated, errors = imod.record_payment("inv1", 100000, PAID_ON)
    finally:
        remove()
    assert fired, "the concurrent void must have raced the commit"
    assert (updated, errors) == (None, [_VOIDED])
    stored = real_store.peek("invoices/inv1")
    assert stored["status"] == "annulée"
    assert stored["amount_paid"] == 0 and stored["etag"] == "void"
    # Two reads of the invoice, both inside a transaction: the first
    # attempt's, and the retry's that saw the void.
    reads = [r for r in real_store.reads if "invoices/inv1" in r.paths]
    assert len(reads) == 2 and all(r.transactional for r in reads)
