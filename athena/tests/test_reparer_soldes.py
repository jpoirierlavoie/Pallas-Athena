"""La SÉLECTION du script de réparation des soldes d'administration.

Il écrit hors modèle — aucune fonction ne corrige `ledger_balance`, et une
écriture compensée est verrouillée. Ce qui le rend acceptable n'est donc pas
la prudence de l'écriture mais le fait que la valeur écrite soit CALCULÉE :
c'est cela que les tests épinglent, plus l'inverse — ce qu'il refuse de
toucher parce qu'il faudrait deviner.

Zéro écriture ici.
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
    from scripts import reparer_soldes_administration as rep

UTC = timezone.utc


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._d = data

    def to_dict(self):
        return dict(self._d)


class _Col:
    def __init__(self, rows):
        self._rows = rows

    def stream(self):
        return iter(self._rows)


def _compte(cid="cpt1", **over):
    doc = {"name": "Compte d'opérations", "account_type": "opérations",
           "status": "actif", "ledger_balance": 0}
    doc.update(over)
    return _Snap(cid, doc)


def _ecriture(tid="tx1", **over):
    doc = {"id": tid, "account_id": "cpt1", "sequence": 1,
           "date": datetime(2026, 4, 7, tzinfo=UTC), "direction": "déboursé",
           "kind": "dépense", "amount": 6189, "net_amount": 6189,
           "gst_amount": 0, "qst_amount": 0, "status": "compensée"}
    doc.update(over)
    return _Snap(tid, doc)


@pytest.fixture()
def base(monkeypatch):
    box = {"comptes": [], "ecritures": []}

    class _Db:
        def collection(self, name):
            if name == "admin_accounts":
                return _Col(box["comptes"])
            return _Col(box["ecritures"])

    monkeypatch.setattr(rep, "db", _Db())
    return box


# ── Le solde : la valeur écrite est CALCULÉE ────────────────────────────


def test_un_solde_qui_a_derive_est_recalcule(base):
    """Le cas réel : 1 154,79 $ stockés pour 109,68 $ d'écritures."""
    base["comptes"] = [_compte(ledger_balance=115479)]
    base["ecritures"] = [_ecriture("a", direction="recette", amount=10968,
                                   net_amount=0)]
    comptes, _, refus = rep._collect()
    assert refus == []
    assert len(comptes) == 1
    assert comptes[0]["stocke"] == 115479
    assert comptes[0]["recalcule"] == 10968


def test_un_solde_juste_n_est_pas_touche(base):
    """L'idempotence : une seconde exécution ne doit rien trouver."""
    base["comptes"] = [_compte(ledger_balance=-6189)]
    base["ecritures"] = [_ecriture()]
    comptes, ventilations, refus = rep._collect()
    assert comptes == [] and ventilations == [] and refus == []


def test_le_recalcul_est_AVEUGLE_AU_STATUT(base):
    """`admin_delta` compte une écriture annulée — elle nette avec sa
    contre-passation. Le solde stocké suit la même règle, donc écarter les
    annulées ici créerait un faux écart et ferait écrire un solde faux."""
    base["comptes"] = [_compte(ledger_balance=0)]
    base["ecritures"] = [
        _ecriture("a", direction="déboursé", amount=75000, status="annulée"),
        _ecriture("b", direction="recette", amount=75000, status="annulée",
                  net_amount=0),
    ]
    comptes, _, _ = rep._collect()
    assert comptes == [], "une paire annulée doit netter à zéro"


def test_chaque_compte_est_juge_separement(base):
    base["comptes"] = [_compte("cpt1", ledger_balance=999),
                       _compte("cpt2", name="Carte", ledger_balance=-6189)]
    base["ecritures"] = [_ecriture("a", account_id="cpt2")]
    comptes, _, _ = rep._collect()
    assert [c["id"] for c in comptes] == ["cpt1"]
    assert comptes[0]["recalcule"] == 0      # un compte sans écriture vaut 0


# ── La ventilation : on rétablit un défaut, on ne devine jamais ─────────


def test_une_ventilation_sans_taxe_est_retablie(base):
    """Le cas réel de seq 133 : le montant est passé de 16,78 $ à 61,89 $ hors
    de l'application, laissant le net à l'ancien montant. Sans taxe, le net
    n'est PAS un choix comptable — c'est le défaut que le modèle pose."""
    base["comptes"] = [_compte(ledger_balance=-6189)]
    base["ecritures"] = [_ecriture(sequence=133, amount=6189, net_amount=1678)]
    _, ventilations, refus = rep._collect()
    assert refus == []
    assert [(v["seq"], v["net"], v["montant"]) for v in ventilations] == [
        (133, 1678, 6189)]


def test_une_ventilation_AVEC_taxe_est_laissee_intacte(base):
    """La borne du script : dès qu'une taxe est non nulle, le net est une
    décision comptable. Le rétablir serait deviner — on refuse, et on le dit."""
    base["comptes"] = [_compte(ledger_balance=-6189)]
    base["ecritures"] = [_ecriture(sequence=7, amount=6189, net_amount=1678,
                                   gst_amount=100)]
    _, ventilations, refus = rep._collect()
    assert ventilations == []
    assert refus and "choix" in refus[0] and "NON corrigée" in refus[0]


def test_une_ventilation_vierge_n_est_pas_une_anomalie(base):
    """net = tps = tvq = 0 : le modèle l'écrit ainsi sur une recette. Rien à
    rétablir."""
    base["comptes"] = [_compte(ledger_balance=10000)]
    base["ecritures"] = [_ecriture(direction="recette", amount=10000,
                                   net_amount=0)]
    _, ventilations, refus = rep._collect()
    assert ventilations == [] and refus == []


def test_une_recette_n_est_jamais_examinee_pour_sa_ventilation(base):
    """`validate_ventilation` zéroise la ventilation d'une recette sans rien
    vérifier : y chercher une incohérence produirait un faux positif."""
    base["comptes"] = [_compte(ledger_balance=10000)]
    base["ecritures"] = [_ecriture(direction="recette", amount=10000,
                                   net_amount=1678)]
    _, ventilations, _ = rep._collect()
    assert ventilations == []


# ── La coquille ─────────────────────────────────────────────────────────


def test_la_simulation_est_le_defaut_et_n_ecrit_rien(base, monkeypatch):
    base["comptes"] = [_compte(ledger_balance=115479)]
    base["ecritures"] = [_ecriture()]

    class _Piege:
        def collection(self, name):
            class _C:
                def stream(_s):
                    return iter(base["comptes"] if name == "admin_accounts"
                                else base["ecritures"])

                def document(_s, _i):
                    pytest.fail("la simulation a écrit")
            return _C()

    monkeypatch.setattr(rep, "db", _Piege())
    assert rep.main([]) == 0


def test_rien_a_reparer_sort_en_zero(base):
    base["comptes"] = [_compte(ledger_balance=-6189)]
    base["ecritures"] = [_ecriture()]
    assert rep.main([]) == 0


def test_un_refus_fait_sortir_en_erreur_meme_sans_reparation(base):
    """Une ventilation écartée est une anomalie qui subsiste : le code de
    sortie doit la porter, pour qu'un enchaînement de commandes s'arrête."""
    base["comptes"] = [_compte(ledger_balance=-6189)]
    base["ecritures"] = [_ecriture(amount=6189, net_amount=1678,
                                   gst_amount=100)]
    assert rep.main([]) == 1
