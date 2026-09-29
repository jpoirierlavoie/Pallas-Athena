"""Les deux registres découpés en phases, pour qu'une écriture COMPOSÉE
tienne dans une seule transaction (lot 5a, étape 3 — le socle).

Le paiement d'honoraires doit écrire, en UN commit, l'écriture au
fidéicommis, la recette au compte d'administration et le paiement de la
facture. Firestore refuse toute lecture après une écriture préparée : chaque
registre expose donc ses lectures (``_read_create`` / ``_read_reverse`` /
``_read_reverse_legs``) séparées de ses gardes et de ses écritures
(``_stage_*``), et les fonctions publiques les composent dans leur propre
transaction — comportement inchangé, ce que la suite existante prouve.

Ce fichier épingle ce que le découpage AJOUTE :

1. **« Déjà compensée » dans le même commit.** La création d'administration
   accepte ``cleared_date`` : l'écriture naît compensée, sous les gardes de
   la compensation. La route composait création-puis-compensation, qui
   pouvait échouer à moitié (écriture debout « en circulation » sous une
   bannière).
2. **Des lecteurs stricts** pour qui décide sur la réponse : une panne de
   lecture PROPAGE, ``None`` veut dire « absent ».
3. **La lecture des recettes liées au fidéicommis dans une transaction** :
   une recette écrite entre-temps interrompt le commit, qui se rejoue.
4. **Des canaux de rapport** (``_report_out``) : le motif machine d'un
   refus, et ce que l'écriture a produit — pour un service qui doit en dire
   plus qu'une phrase.

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais ; on relit ce qui est STOCKÉ.
"""

import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from google.cloud import firestore
    from models import admin_ledger as al
    from models import invoice as invoice_model
    from models import trust

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def clock(monkeypatch):
    from utils import deadlines as dl

    frozen = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    return frozen


@pytest.fixture
def fake(monkeypatch, clock):
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0, "etag": "c0",
    })
    f.seed("invoices/fac1", {
        "id": "fac1", "invoice_number": "2026-F031", "status": "envoyée",
        "total": 100000, "retainer_applied": 0,
        "amount_due": 100000, "amount_paid": 0, "paid_date": None,
        "dossier_id": "dos1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. X", "etag": "inv-e0",
    })
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "t0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "client_ids": ["c1"], "clients": [{"id": "c1", "name": "Jean Tremblay"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    return f


def _depense(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "dépense", "category": "loyer",
        "amount": 11498, "method": "virement", "counterparty": "Immeubles X",
        "date": _d(2026, 9, 10), "net_amount": 10000, "gst_amount": 500,
        "qst_amount": 998, "description": "", "reference": "",
        "supplier_invoice_ref": "",
    }
    d.update(over)
    return d


def _enc(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "encaissement_facture",
        "invoice_id": "fac1", "amount": 60000, "method": "virement",
        "counterparty": "Jean Tremblay", "date": _d(2026, 9, 10),
    }
    d.update(over)
    return d


def _commit_touching(fake, rel: str) -> list:
    return [c for c in fake.commits if any(path == rel for _op, path in c.ops)]


# ══════════════════════════════════════════════════════════════════════
# 1. « Déjà compensée » : né compensé, dans le même commit
# ══════════════════════════════════════════════════════════════════════


def test_une_ecriture_deja_compensee_nait_compensee_en_un_commit(fake):
    fake.reset_logs()
    entry, errs = al.create_transaction(_depense(), cleared_date=_d(2026, 9, 12))
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["status"] == "compensée"
    assert stored["cleared_date"] == _d(2026, 9, 12)
    assert stored["cleared_via"] == "script"            # WHO, beside WHEN
    assert len(fake.commits) == 1                       # never create-then-clear
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == -11498


def test_un_encaissement_deja_compense_porte_son_paiement_dans_ce_commit(fake):
    fake.reset_logs()
    entry, errs = al.create_transaction(_enc(), cleared_date=_d(2026, 9, 10))
    assert errs == [], errs
    assert fake.peek(f"admin_transactions/{entry['id']}")["status"] == "compensée"
    assert fake.peek("invoices/fac1")["amount_paid"] == 60000
    assert len(fake.commits) == 1


@pytest.mark.parametrize("label, cleared", [
    ("avant l'écriture", _d(2026, 9, 9)),
    ("future à Montréal", _d(2026, 9, 21)),
])
def test_une_date_de_compensation_invalide_refuse_toute_la_creation(
    fake, label, cleared
):
    """Régression — la route créait l'écriture, puis la compensation
    échouait : l'écriture restait « en circulation » sous une bannière.
    Aujourd'hui la création entière est refusée, et rien n'est écrit."""
    report: dict = {}
    entry, errs = al.create_transaction(_depense(), cleared_date=cleared,
                                        _report_out=report)
    assert entry is None, label
    assert errs == [al._ABORT_MESSAGES["compensation_invalide"]]
    assert report["reason"] == "compensation_invalide"
    assert fake.peek_collection("admin_transactions") == {}
    assert fake.peek("counters/admin-ops1") is None
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0


def test_sans_date_de_compensation_la_creation_reste_en_circulation(fake):
    entry, errs = al.create_transaction(_depense())
    assert errs == []
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["status"] == "en_circulation"
    assert stored["cleared_date"] is None and stored["cleared_via"] == ""


# ══════════════════════════════════════════════════════════════════════
# 2. Les lecteurs stricts
# ══════════════════════════════════════════════════════════════════════


def _failing_get(monkeypatch):
    from google.cloud.firestore_v1.document import DocumentReference

    def _boom(self, *a, **k):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(DocumentReference, "get", _boom)


@pytest.mark.parametrize("reader, seeded, path", [
    (trust.get_transaction_strict, "t1", "trust_transactions/t1"),
    (al.get_transaction_strict, "a1", "admin_transactions/a1"),
    (invoice_model.get_invoice_strict, "fac1", None),
])
def test_un_lecteur_strict_propage_une_panne_et_dit_absent_sinon(
    fake, monkeypatch, reader, seeded, path
):
    if path:
        fake.seed(path, {"id": seeded, "amount": 1})
    assert reader(seeded)["id"] == seeded
    assert reader("inconnu") is None
    assert reader("a/b") is None and reader("") is None
    _failing_get(monkeypatch)
    with pytest.raises(RuntimeError):
        reader(seeded)


def test_le_lecteur_d_affichage_continue_d_echouer_ouvert(fake, monkeypatch):
    """Témoin : les lecteurs de page gardent leur posture — seule une
    décision passe par le lecteur strict."""
    _failing_get(monkeypatch)
    assert trust.get_transaction("t1") is None
    assert al.get_transaction("a1") is None


# ══════════════════════════════════════════════════════════════════════
# 3. Les recettes liées se lisent dans la transaction de l'appelant
# ══════════════════════════════════════════════════════════════════════


def test_les_recettes_liees_se_lisent_dans_une_transaction_qui_se_rejoue(fake):
    """Une recette liée écrite pendant la transaction interrompt son
    commit ; le rejeu la voit. (Hors transaction, la requête ne rejoindrait
    pas l'ensemble lu, et le commit passerait sur une liste périmée.)"""
    first, errs = al.create_transaction(_enc(amount=30000), trust_transaction_id="ttx1")
    assert errs == []
    seen: list = []
    raced: dict = {}

    def _race(info):
        if raced or not any(p == "admin_accounts/ops1" for _o, p in info.ops):
            return
        raced["done"] = True
        _, errs = al.create_transaction(_enc(amount=20000), trust_transaction_id="ttx1")
        assert errs == []

    @firestore.transactional
    def _read(txn):
        rows = al.list_by_trust_transaction("ttx1", txn=txn)
        seen.append(len(rows))
        # A write to something the transaction did NOT read, so only the
        # QUERY's result set can abort it.
        txn.update(fake.document("admin_accounts/ops1"), {"note": len(rows)})

    remove = fake.add_commit_hook(_race)
    try:
        _read(fake.transaction())
    finally:
        remove()
    assert seen == [1, 2]
    assert fake.peek("admin_accounts/ops1")["note"] == 2
    assert first["id"] in {r["id"] for r in al.list_by_trust_transaction("ttx1")}


# ══════════════════════════════════════════════════════════════════════
# 4. Les canaux de rapport
# ══════════════════════════════════════════════════════════════════════


def _trust_entry(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "recette", "amount": 100000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 2),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


def test_le_fideicommis_rapporte_le_solde_du_client_et_le_motif(fake):
    report: dict = {}
    entry, errs = trust.create_transaction(_trust_entry(), _report_out=report)
    assert errs == []
    assert report["client_balance"] == {"book": 100000, "cleared": 0}
    report = {}
    _, errs = trust.create_transaction(
        _trust_entry(direction="déboursé", purpose="déboursé_tiers", amount=5000,
                     date=_d(2026, 9, 3)), _report_out=report)
    assert errs and report["reason"] == "solde_compensé_insuffisant"
    report = {}
    rev, errs = trust.reverse_transaction(entry["id"], "erreur", _report_out=report)
    assert errs == []
    assert report["original_status_after"] == "annulée"
    assert report["client_cleared_after"] == 0
    assert [r["id"] for r in report["reversals"]] == [rev["id"]]


def test_la_compensation_en_lot_rapporte_ses_ecritures(fake):
    entry, _ = trust.create_transaction(_trust_entry())
    report: dict = {}
    count, failed = trust.clear_transactions_bulk([entry["id"]], _d(2026, 9, 5),
                                                  _reason_out=report)
    assert (count, failed) == (1, [])
    assert [e["id"] for e in report["cleared"]] == [entry["id"]]
    a, _ = al.create_transaction(_depense())
    report = {}
    count, failed = al.clear_transactions_bulk([a["id"]], _d(2026, 9, 11),
                                               _report_out=report)
    assert (count, failed) == (1, [])
    assert report["cleared"][0]["cleared_via"] == "script"
    report = {}
    count, failed = al.clear_transactions_bulk([a["id"]], _d(2026, 9, 11),
                                               _report_out=report)
    assert count == 0 and report["reason"] == "compensation_invalide"
    assert report["message"] == al._ABORT_MESSAGES["compensation_invalide"]


def test_l_administration_rapporte_la_facture_avant_et_apres(fake):
    report: dict = {}
    entry, errs = al.create_transaction(_enc(), _report_out=report)
    assert errs == []
    assert report["invoice_before"]["amount_paid"] == 0
    assert report["invoice_after"]["amount_paid"] == 60000
    report = {}
    _, errs = al.reverse_transaction(entry["id"], "chèque sans provision",
                                     _report_out=report)
    assert errs == []
    ((iid, before, after),) = report["invoices"]
    assert iid == "fac1"
    assert (before["amount_paid"], after["amount_paid"]) == (60000, 0)


def test_le_paiement_de_carte_rapporte_ses_deux_jambes(fake):
    report: dict = {}
    leg, errs = al.create_card_payment("ops1", "card1", 5000, _d(2026, 9, 10),
                                       "virement", _report_out=report)
    assert errs == []
    bank, card = report["legs"]
    assert bank["id"] == leg["id"] and card["account_id"] == "card1"
    report = {}
    _, errs = al.create_card_payment("card1", "ops1", 5000, _d(2026, 9, 10),
                                     "virement", _report_out=report)
    assert errs and report["reason"] == "comptes_incompatibles"


def test_la_modification_rapporte_ses_champs_et_le_conflit(fake):
    entry, _ = al.create_transaction(_depense())
    report: dict = {}
    _, errs = al.update_transaction(entry["id"], {"description": "Loyer"},
                                    expected_etag=entry["etag"], _report_out=report)
    assert errs == []
    assert report == {"fields": ["description"], "noop": False}
    report = {}
    _, errs = al.update_transaction(entry["id"], {"description": "Autre"},
                                    expected_etag=entry["etag"], _report_out=report)
    assert errs and report["reason"] == "écriture_modifiée"


# ══════════════════════════════════════════════════════════════════════
# 5. Une écriture listée deux fois ne se compense jamais deux fois
# ══════════════════════════════════════════════════════════════════════


def test_un_depot_liste_deux_fois_ne_libere_pas_deux_fois_les_fonds(fake):
    """Régression (vérifiée sur le code antérieur : 200 000 ¢ au solde
    bancaire et au solde COMPENSÉ du client pour un dépôt de 100 000 ¢) —
    chaque copie de l'identifiant ajoutait son montant, et le solde
    compensé est le seul chiffre que le contrôle de découvert lit : le
    client pouvait retirer deux fois ce qu'il avait déposé. La compensation
    en lot est encore latente au web ; l'outil du connecteur la rendra
    courante (lot 5b)."""
    entry, _ = trust.create_transaction(_trust_entry())
    before_account = fake.peek("trust_accounts/acc1")
    before_dossier = fake.peek("dossiers/dos1")
    report: dict = {}
    count, failed = trust.clear_transactions_bulk(
        [entry["id"], entry["id"]], _d(2026, 9, 3), _reason_out=report)
    assert count == 0 and failed == [entry["id"], entry["id"]]
    assert report["reason"] == "compensation_doublon"
    assert report["message"] == trust._ABORT_MESSAGES["compensation_doublon"]
    assert fake.peek("trust_accounts/acc1") == before_account
    assert fake.peek("dossiers/dos1") == before_dossier
    assert fake.peek(f"trust_transactions/{entry['id']}")["status"] == "en_circulation"


def test_l_administration_refuse_aussi_une_ecriture_listee_deux_fois(fake):
    entry, _ = al.create_transaction(_depense())
    before = fake.peek(f"admin_transactions/{entry['id']}")
    report: dict = {}
    count, failed = al.clear_transactions_bulk(
        [entry["id"], entry["id"]], _d(2026, 9, 11), _report_out=report)
    assert count == 0 and len(failed) == 2
    assert report["reason"] == "compensation_doublon"
    assert fake.peek(f"admin_transactions/{entry['id']}") == before


# ══════════════════════════════════════════════════════════════════════
# 6. Une écriture rejouée prend l'instant de l'essai qui commet
# ══════════════════════════════════════════════════════════════════════


def test_une_ecriture_rejouee_est_datee_de_l_essai_qui_commet(fake, monkeypatch):
    """Régression (lot 5a, étape 3) — ``now`` était pris AVANT la
    transaction : interrompue par une écriture rivale puis rejouée, l'écriture
    gardait l'instant de son premier essai, ANTÉRIEUR à la rivale, alors que
    sa séquence vient après. Le contrôle d'intégrité ordonne les écritures
    d'un client par instant de création : il y lisait une erreur de solde
    courant. L'horloge du module est remplacée par un compteur pour que
    l'ordre ne dépende pas de la résolution de l'horloge murale."""
    ticks = iter(range(1, 1000))

    class _Tick(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(seconds=next(ticks))

    monkeypatch.setattr(trust, "datetime", _Tick)
    rival: dict = {}

    def _race(info):
        if rival or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["entry"], _ = trust.create_transaction(_trust_entry(amount=1000))

    remove = fake.add_commit_hook(_race)
    try:
        entry, errs = trust.create_transaction(_trust_entry(amount=2000))
    finally:
        remove()
    assert errs == []
    stored = fake.peek(f"trust_transactions/{entry['id']}")
    first = fake.peek(f"trust_transactions/{rival['entry']['id']}")
    assert stored["sequence"] > first["sequence"]
    assert stored["created_at"] > first["created_at"]


def test_un_virement_inter_dossiers_rejoue_est_date_de_l_essai_qui_commet(
    fake, monkeypatch, capsys
):
    """Régression (revue de l'étape 3) — le correctif ci-dessus n'avait pas
    atteint ``create_inter_dossier_transfer``, qui prenait encore ``now``
    AVANT sa transaction. Un dépôt rival au client source interrompt le
    virement, qui se rejoue sur le nouveau solde — mais ses deux volets
    gardaient l'instant du premier essai, ANTÉRIEUR au dépôt : le contrôle
    d'intégrité, qui ordonne les écritures d'un client par instant de
    création, calculait le solde courant du volet source sans le dépôt et
    signalait un écart qui n'existe pas."""
    fake.seed("dossiers/dos2", {
        "id": "dos2", "file_number": "2026-002", "title": "Y c. Z",
        "client_ids": ["c9"], "clients": [{"id": "c9", "name": "Marie Roy"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    ticks = iter(range(1, 1000))

    class _Tick(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(seconds=next(ticks))

    monkeypatch.setattr(trust, "datetime", _Tick)
    deposit, errs = trust.create_transaction(_trust_entry())
    assert errs == []
    _, errs = trust.clear_transaction(deposit["id"], _d(2026, 9, 3))
    assert errs == []
    rival: dict = {}

    def _race(info):
        if rival or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["entry"], _ = trust.create_transaction(
            _trust_entry(amount=5000, date=_d(2026, 9, 20)))

    remove = fake.add_commit_hook(_race)
    try:
        leg, errs = trust.create_inter_dossier_transfer(
            "acc1", "dos1", "c1", "dos2", "c9", 20000, "partage", "virement", "")
    finally:
        remove()
    assert errs == [], errs
    stored = fake.peek(f"trust_transactions/{leg['id']}")
    first = fake.peek(f"trust_transactions/{rival['entry']['id']}")
    assert stored["sequence"] > first["sequence"]
    assert stored["created_at"] > first["created_at"]

    from scripts import verify_trust_integrity as vti

    install(monkeypatch, *_fake_modules(), vti, fake=fake)
    vti.main()
    out = capsys.readouterr().out
    # The per-CLIENT running balances agree with the model. (The account
    # view keeps ONE known écart on a transfer's first leg — the pair's net
    # balance stored on both legs, DEPLOYMENT.md « One écart is known »,
    # its model fix scheduled apart — which this test does not judge.)
    assert "dossier dos1/c1" not in out and "dossier dos2/c9" not in out, out
