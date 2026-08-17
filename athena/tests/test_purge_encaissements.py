"""La SÉLECTION du script de purge des encaissements hors comptabilité.

Ce que fait `record_payment(id, 0)` est déjà épinglé dix-neuf fois par
`test_invoice_payments.py` — y compris l'effacement de la date et la bascule
inverse « payée → envoyée ». Ce qui n'est couvert nulle part, et ce qui décide
de la donnée qui disparaît, c'est le choix des factures : une erreur ici
efface un paiement légitime, sans annulation possible.

D'où quatre épingles, toutes sur `_collect`, et aucune écriture.
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
    from scripts import purge_encaissements_factures as purge

UTC = timezone.utc


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Collection:
    def __init__(self, snaps):
        self._snaps = snaps

    def stream(self):
        return iter(self._snaps)


class _Db:
    def __init__(self, snaps):
        self._snaps = snaps

    def collection(self, name):
        assert name == "invoices"
        return _Collection(self._snaps)


def _facture(doc_id, **over):
    doc = {
        "invoice_number": f"2026-F{doc_id}",
        "dossier_file_number": "2026-001",
        "client_name": "M. Jean Tremblay",
        "status": "payée",
        "amount_paid": 51739,
        "paid_date": datetime(2026, 8, 6, tzinfo=UTC),
    }
    doc.update(over)
    return _Snap(doc_id, doc)


@pytest.fixture()
def monde(monkeypatch):
    """Pose la base et le cumul du registre ; rend un poseur pour chaque."""
    etat = {"cumuls": {}}

    def _poser(snaps, cumuls=None):
        etat["cumuls"] = cumuls or {}
        monkeypatch.setattr(purge, "db", _Db(snaps))
        monkeypatch.setattr(
            purge.al, "sum_invoice_receipts",
            lambda iid: etat["cumuls"].get(iid, 0),
        )
    return _poser


def test_une_facture_payee_sans_ecriture_est_retenue(monde):
    monde([_facture("001")])
    cibles, erreurs = purge._collect()
    assert erreurs == []
    assert [c["numero"] for c in cibles] == ["2026-F001"]
    assert cibles[0]["paye"] == 51739


def test_une_facture_deja_adossee_au_registre_est_epargnee(monde):
    """Rejouer le script ne doit rien détruire : un paiement ressaisi en
    comptabilité est exactement ce que la purge cherche à obtenir."""
    monde([_facture("001")], cumuls={"001": 51739})
    cibles, erreurs = purge._collect()
    assert cibles == [] and erreurs == []


def test_un_paiement_partiellement_adosse_reste_retenu(monde):
    """Un cumul INFÉRIEUR au montant encaissé trahit encore une saisie hors
    comptabilité — l'écart, lui, est le trou dans le compte d'opérations."""
    monde([_facture("001")], cumuls={"001": 20000})
    cibles, _ = purge._collect()
    assert len(cibles) == 1
    assert cibles[0]["registre"] == 20000


def test_les_dix_neuf_payees_sans_montant_ne_sont_pas_touchees(monde):
    """Le statut « payée » posé à la main, sans montant, est la parole de
    l'avocat : la purge n'a rien à y redire."""
    monde([_facture("001", amount_paid=0), _facture("002", amount_paid=None)])
    cibles, erreurs = purge._collect()
    assert cibles == [] and erreurs == []


def test_un_cumul_illisible_epargne_la_facture_et_le_dit(monde, monkeypatch):
    """Fail CLOSED : effacer un paiement dont l'adossement n'a pas pu être lu
    détruirait peut-être un chiffre légitime. La facture est laissée intacte
    ET le refus est annoncé — un silence la ferait passer pour saine."""
    monde([_facture("001"), _facture("002")])

    def _boum(iid):
        if iid == "001":
            raise RuntimeError("indisponible")
        return 0
    monkeypatch.setattr(purge.al, "sum_invoice_receipts", _boum)

    cibles, erreurs = purge._collect()
    assert [c["numero"] for c in cibles] == ["2026-F002"]
    assert len(erreurs) == 1 and "NON purgée" in erreurs[0]


def test_la_simulation_est_le_defaut_et_n_ecrit_rien(monde, monkeypatch):
    """Le patron maison : --dry-run par défaut. Une purge lancée par
    inadvertance serait irrattrapable."""
    monde([_facture("001")])
    monkeypatch.setattr(
        purge, "record_payment",
        lambda *a, **k: pytest.fail("la simulation a écrit"),
    )
    assert purge.main([]) == 0
