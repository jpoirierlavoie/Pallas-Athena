"""La SÉLECTION du script de correction des provisions.

Il écrit hors modèle — aucun `update_invoice` n'existe — donc ce qui le rend
acceptable n'est pas la prudence de l'écriture (deux champs) mais l'exactitude
du choix. Une facture retenue à tort verrait son dû gonfler sans contrepartie ;
une facture écartée en silence garderait un dû diminué pendant que son virement
partirait en « autre recette », sans que rien ne le signale.

D'où : tout est épinglé sur `_collect`, et rien n'écrit.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from scripts import corriger_provisions_factures as corr


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


def _facture(fid="fac1", **over):
    doc = {"invoice_number": "256501-01", "dossier_file_number": "2025-065",
           "status": "envoyée", "total": 114975, "retainer_applied": 75000,
           "amount_due": 39975, "amount_paid": 0, "legacy_ref": "",
           # Antérieure à DATE_CORRECTION : le cas prouvé de la reprise.
           # La garde de rejeu (audit 2026-08-26) refuse toute facture créée
           # après la séance — et, fail-closed, toute facture SANS date.
           "created_at": datetime(2025, 6, 1, tzinfo=timezone.utc)}
    doc.update(over)
    return _Snap(fid, doc)


def _virement(**over):
    doc = {"purpose": "virement_honoraires", "amount": 75000,
           "invoice_id": None, "invoice_external_ref": "256501-01",
           "reversed_by_id": None, "status": "compensée"}
    doc.update(over)
    return _Snap("ttx1", doc)


@pytest.fixture()
def base(monkeypatch):
    box = {"invoices": [], "trust": []}

    class _Db:
        def collection(self, name):
            return _Col(box["invoices"] if name == "invoices" else box["trust"])

    monkeypatch.setattr(corr, "db", _Db())
    return box


# ── Le cas prouvé ───────────────────────────────────────────────────────


def test_une_provision_egale_a_son_virement_est_retenue(base):
    base["invoices"] = [_facture()]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert refus == []
    assert [c["numero"] for c in cibles] == ["256501-01"]
    assert cibles[0]["provision"] == 75000
    assert cibles[0]["total"] == 114975


def test_le_rapprochement_normalise_les_tirets(base):
    """Le virement cite « 251601-01 » quand la facture importée s'appelle
    « 25160101 » — même règle que la reprise, sans quoi le cas prouvé
    passerait pour non prouvé."""
    base["invoices"] = [_facture(invoice_number="25160101",
                                 legacy_ref="facture:25160101")]
    base["trust"] = [_virement(invoice_external_ref="251601-01")]
    cibles, refus = corr._collect()
    assert [c["numero"] for c in cibles] == ["25160101"] and refus == []


def test_plusieurs_virements_pour_une_meme_facture(base):
    """Le cas de 2026-012-01 : deux virements (2 500,00 + 241,24) dont la
    somme fait exactement le total. La provision ne couvre que le premier —
    c'est ce que la borne basse autorise."""
    base["invoices"] = [_facture(total=274124, retainer_applied=250000,
                                 amount_due=24124, invoice_number="2026-012-01")]
    base["trust"] = [_virement(amount=250000, invoice_external_ref="2026-012-01"),
                     _virement(amount=24124, invoice_external_ref="2026-012-01")]
    cibles, refus = corr._collect()
    assert [c["numero"] for c in cibles] == ["2026-012-01"] and refus == []


# ── Tout ce qui sort du cas prouvé est ÉCARTÉ, et NOMMÉ ─────────────────


def test_une_provision_qui_ne_correspond_a_aucun_virement_est_ecartee(base):
    base["invoices"] = [_facture()]
    base["trust"] = []
    cibles, refus = corr._collect()
    assert cibles == []
    assert refus and "n'est pas couverte" in refus[0]


def test_une_provision_plus_grande_que_les_virements_est_ecartee(base):
    """La borne basse de la règle : retirer une provision que l'argent réel ne
    couvre pas gonflerait le dû sans contrepartie au grand livre."""
    base["invoices"] = [_facture()]
    base["trust"] = [_virement(amount=74999)]
    cibles, refus = corr._collect()
    assert cibles == [] and "n'est pas couverte" in refus[0]


def test_des_virements_depassant_le_total_facture_sont_ecartes(base):
    """La borne haute : au-delà du total, le surplus n'acquitte plus cette
    facture, et le rapprochement n'est donc plus prouvé."""
    base["invoices"] = [_facture()]
    base["trust"] = [_virement(amount=114976)]
    cibles, refus = corr._collect()
    assert cibles == [] and "> total facturé" in refus[0]


def test_un_virement_superieur_a_la_provision_mais_sous_le_total_est_retenu(base):
    """Le cas de 2026-012-01 : la provision ne couvre que le PREMIER virement,
    le second acquittant le solde. L'égalité stricte l'aurait écarté."""
    base["invoices"] = [_facture()]
    base["trust"] = [_virement(amount=100000)]
    cibles, refus = corr._collect()
    assert [c["numero"] for c in cibles] == ["256501-01"] and refus == []


def test_une_facture_portant_un_paiement_est_epargnee(base):
    """Lui retirer sa provision gonflerait son dû sans toucher au montant
    encaissé : son solde deviendrait faux."""
    base["invoices"] = [_facture(amount_paid=1000)]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert cibles == [] and "d'encaissé" in refus[0]


@pytest.mark.parametrize("statut", ["brouillon", "payée", "annulée"])
def test_une_facture_non_emise_est_epargnee(base, statut):
    base["invoices"] = [_facture(status=statut)]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert cibles == [] and f"« {statut} »" in refus[0]


def test_un_invariant_rompu_arrete_tout(base):
    """`dû == total − provision` est ce que le script rétablit ; s'il ne tenait
    pas AVANT, on ne sait pas ce que la facture raconte."""
    base["invoices"] = [_facture(amount_due=12345)]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert cibles == [] and "invariant rompu" in refus[0]


def test_un_virement_contrepasse_ne_compte_pas(base):
    """Il n'a rien déplacé net : le retenir ferait passer pour prouvé un cas
    qui ne l'est pas."""
    base["invoices"] = [_facture()]
    base["trust"] = [_virement(reversed_by_id="rev1")]
    cibles, refus = corr._collect()
    assert cibles == [] and "n'est pas couverte" in refus[0]


def test_une_facture_sans_provision_n_est_jamais_regardee(base):
    base["invoices"] = [_facture(retainer_applied=0, amount_due=114975)]
    base["trust"] = [_virement()]
    assert corr._collect() == ([], [])


def test_un_numero_ambigu_ecarte_plutot_que_deviner(base):
    """Deux factures sous la même clé normalisée : on ne choisit pas."""
    base["invoices"] = [_facture("f1", invoice_number="256501-01"),
                        _facture("f2", invoice_number="2565-0101")]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert cibles == []
    assert len(refus) == 2


# ── La coquille ─────────────────────────────────────────────────────────


def test_la_simulation_est_le_defaut_et_n_ecrit_rien(base, monkeypatch):
    base["invoices"] = [_facture()]
    base["trust"] = [_virement()]

    class _Piege:
        def collection(self, name):
            class _C:
                def stream(_s):
                    return iter(base["invoices"] if name == "invoices"
                                else base["trust"])

                def document(_s, _i):
                    pytest.fail("la simulation a écrit")
            return _C()

    monkeypatch.setattr(corr, "db", _Piege())
    assert corr.main([]) == 0


def test_des_refus_font_sortir_en_erreur_meme_sans_cible(base):
    """Un écart ignoré est un dû qui reste faux : le code de sortie doit le
    porter, pour qu'un enchaînement de commandes s'arrête."""
    base["invoices"] = [_facture()]
    base["trust"] = []
    assert corr.main([]) == 1


# ── L'arbitrage du juriste, lu du CSV de la reprise ─────────────────────


def test_l_arbitrage_redirige_un_virement_mal_reference(base, tmp_path):
    """Le cas réel : les deux virements du dossier 2026-012 citent
    « 250701-01 » alors qu'ils acquittent « 2026-012-01 ». Sans l'arbitrage,
    la facture reste hors du cas prouvé — avec, elle y entre."""
    base["invoices"] = [_facture("f1", invoice_number="2026-012-01",
                                 total=274124, retainer_applied=250000,
                                 amount_due=24124)]
    base["trust"] = [_Snap("ttxA", {**_virement(amount=250000).to_dict(),
                                    "invoice_external_ref": "250701-01"}),
                     _Snap("ttxB", {**_virement(amount=24124).to_dict(),
                                    "invoice_external_ref": "250701-01"})]
    for t in base["trust"]:
        t._d["id"] = t.id

    assert corr._collect()[0] == []          # sans arbitrage : écartée

    csv = tmp_path / "reprise.csv"
    csv.write_text(
        "trust_tx_id,date,montant,reference_citee,mode,facture_numero,note\n"
        "ttxA,2026-03-02,2 500,250701-01,encaissement,2026-012-01,\n"
        "ttxB,2026-03-20,241,250701-01,encaissement,2026-012-01,\n",
        encoding="utf-8-sig")
    arbitrage = corr.lire_arbitrage(str(csv))
    assert arbitrage == {"ttxA": "2026-012-01", "ttxB": "2026-012-01"}

    cibles, refus = corr._collect(arbitrage)
    assert [c["numero"] for c in cibles] == ["2026-012-01"] and refus == []


def test_l_arbitrage_ignore_les_lignes_qui_n_imputent_rien(base, tmp_path):
    """Une ligne « autre recette » ou « ignorer » ne nomme aucune facture :
    la lire comme un arbitrage inventerait un rapprochement."""
    csv = tmp_path / "reprise.csv"
    csv.write_text(
        "trust_tx_id,date,montant,reference_citee,mode,facture_numero,note\n"
        "ttxA,2026-03-02,2 500,X,recette_autre,2026-012-01,\n"
        "ttxB,2026-03-20,241,X,ignorer,2026-012-01,\n"
        "ttxC,2026-03-20,100,X,encaissement,,\n",
        encoding="utf-8-sig")
    assert corr.lire_arbitrage(str(csv)) == {}


def test_une_facture_posterieure_a_la_seance_est_refusee(base):
    """Garde de rejeu (audit 2026-08-26) : le cas prouvé ne couvre que les
    factures ANTÉRIEURES au 2026-08-17. Une provision posée depuis relève du
    système actuel — la retirer facturerait deux fois l'avance du client, et
    aucun `update_invoice` n'existe pour la rétablir. Une facture SANS
    created_at est refusée aussi (fail-closed)."""
    base["invoices"] = [
        _facture("f1", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc)),
    ]
    base["trust"] = [_virement()]
    cibles, refus = corr._collect()
    assert cibles == []
    assert refus and "postérieure au 2026-08-17" in refus[0]

    sans_date = _facture("f2")
    sans_date._d.pop("created_at")
    base["invoices"] = [sans_date]
    cibles, refus = corr._collect()
    assert cibles == [] and "postérieure au 2026-08-17" in refus[0]
