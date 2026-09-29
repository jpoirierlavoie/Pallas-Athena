"""``services/comptabilite`` — l'orchestration UNIQUE des deux registres
(lot 5a, étape 3).

Les règles restent aux modèles ; le service tient ce qui était éparpillé
dans les deux routes, pour que le web et — au lot 5b — le connecteur passent
par le même chemin :

1. **Les lectures strictes** sur lesquelles une décision d'argent repose :
   une facture ou une écriture illisible REFUSE, jamais « absente » (la
   résolution de la route passait par ``list_invoices``, qui échoue ouvert —
   un hoquet de lecture répondait « Aucune facture »).
2. **Le bon chemin** : un paiement d'honoraires n'est jamais une écriture
   ordinaire ; sa contre-passation emporte ses recettes.
3. **Un rapport structuré** par opération — les écritures, la facture avant
   et après, les fonds libérés, les avertissements.

Le banc est le faux Firestore partagé ; on relit ce qui est STOCKÉ.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import admin_ledger as al
    from models import invoice as invoice_model
    from models import trust
    from services import comptabilite as svc

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    from utils import deadlines as dl

    frozen = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)
    f = install(monkeypatch, *_fake_modules())
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
    })
    for did, clients in (("dos1", ["c1", "c2"]), ("dos2", ["c3"])):
        f.seed(f"dossiers/{did}", {
            "id": did, "file_number": f"2026-00{did[-1]}", "title": "T c. X",
            "client_ids": clients,
            "clients": [{"id": c, "name": c.upper()} for c in clients],
            "trust_balance": 0, "trust_balance_by_client": {},
            "trust_cleared_by_client": {},
        })
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0,
    })
    f.seed("admin_accounts/ops2", {
        "id": "ops2", "name": "Ancien", "status": "fermé",
        "account_type": "opérations", "ledger_balance": 0,
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0,
    })
    for iid, number, did, client in (("inv1", "2026-F040", "dos1", "c1"),
                                     ("inv9", "2026-F090", "dos2", "c3")):
        f.seed(f"invoices/{iid}", {
            "id": iid, "invoice_number": number, "dossier_id": did,
            "dossier_file_number": "2026-001", "client_id": client,
            "status": "envoyée", "total": 100000, "retainer_applied": 0,
            "amount_due": 100000, "amount_paid": 0,
        })
    receipt = svc.enregistrer_ecriture_fideicommis(_deposit())
    assert receipt["ok"], receipt
    cleared = svc.compenser_fideicommis([receipt["entry"]["id"]], _d(2026, 9, 2))
    assert cleared["ok"], cleared
    return f


def _deposit(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "recette", "amount": 200000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 2),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


def _fee(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "déboursé", "amount": 60000,
        "purpose": "virement_honoraires", "method": "chèque",
        "counterparty": "Me Avocat", "dossier_id": "dos1", "client_id": "c1",
        "date": _d(2026, 9, 10), "invoice_number": "2026-F040",
        "description": "", "reference": "",
    }
    d.update(over)
    return d


# ══════════════════════════════════════════════════════════════════════
# 1. La résolution de la facture — stricte
# ══════════════════════════════════════════════════════════════════════


def test_la_facture_se_resout_par_son_numero_dans_le_dossier(fake):
    assert svc.resolve_fee_invoice(dossier_id="dos1", invoice_number="2026-F040")["id"] == "inv1"
    assert svc.resolve_fee_invoice(dossier_id="dos1", invoice_id="inv1")["id"] == "inv1"


@pytest.mark.parametrize("label, kwargs, reason", [
    ("numéro inconnu", {"invoice_number": "2026-F009"}, "facture_introuvable"),
    ("autre dossier", {"invoice_number": "2026-F090"}, "facture_autre_dossier"),
    ("autre dossier, par id", {"invoice_id": "inv9"}, "facture_autre_dossier"),
    ("id inconnu", {"invoice_id": "nope"}, "facture_introuvable"),
    ("les deux", {"invoice_id": "inv1", "invoice_number": "2026-F040"}, "facture_ambiguë"),
    ("aucun", {}, "facture_requise"),
])
def test_une_facture_qui_ne_se_resout_pas_est_un_refus_nomme(fake, label, kwargs, reason):
    with pytest.raises(svc.ComptabiliteRefus) as refusal:
        svc.resolve_fee_invoice(dossier_id="dos1", **kwargs)
    assert refusal.value.reason == reason, label


def test_deux_factures_du_meme_numero_sont_refusees_jamais_choisies(fake):
    fake.seed("invoices/inv2", {**fake.peek("invoices/inv1"), "id": "inv2"})
    with pytest.raises(svc.ComptabiliteRefus) as refusal:
        svc.resolve_fee_invoice(dossier_id="dos1", invoice_number="2026-F040")
    assert refusal.value.reason == "facture_ambiguë"


@pytest.mark.parametrize("reader", ["get_invoices_by_number", "get_invoice_strict"])
def test_une_panne_de_lecture_n_est_jamais_une_facture_inconnue(fake, monkeypatch, reader):
    """Régression — l'ancienne résolution (routes/trust._resolve_invoice_number)
    passait par list_invoices, qui échoue ouvert : un hoquet de lecture
    répondait « Aucune facture « … » dans ce dossier »."""
    def _boom(*_a, **_k):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(invoice_model, reader, _boom)
    kwargs = ({"invoice_number": "2026-F040"} if reader == "get_invoices_by_number"
              else {"invoice_id": "inv1"})
    with pytest.raises(svc.ComptabiliteRefus) as refusal:
        svc.resolve_fee_invoice(dossier_id="dos1", **kwargs)
    assert refusal.value.reason == "facture_illisible"
    assert "rien n'a été inscrit" in refusal.value.message


def test_un_numero_errone_n_est_jamais_degrade_en_facture_externe(fake):
    """(Déplacé de test_trust.py::test_resolve_invoice_number_hard_errors_on_typo.)
    Un numéro Athéna mal tapé est une erreur FRANCHE — jamais un repli sur
    « facture externe », qui sauterait le contrôle du montant."""
    before = fake.peek_collection("trust_transactions")
    report = svc.enregistrer_paiement_honoraires(
        _fee(invoice_number="2026-F009"), admin_account_id="ops1",
        allow_external_ref=True)
    assert not report["ok"]
    assert report["reason"] == "facture_introuvable"
    assert fake.peek_collection("trust_transactions") == before
    assert fake.peek_collection("admin_transactions") == {}


# ══════════════════════════════════════════════════════════════════════
# 2. Le paiement d'honoraires et son rapport
# ══════════════════════════════════════════════════════════════════════


def test_le_rapport_du_paiement_dit_ce_qui_a_ete_ecrit(fake):
    report = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    assert report["ok"], report
    assert report["errors"] == [] and report["warnings"] == []
    assert report["trust_entry"]["purpose"] == "virement_honoraires"
    assert report["admin_recette"]["trust_transaction_id"] == report["trust_entry"]["id"]
    assert report["invoice"] == {
        "id": "inv1", "invoice_number": "2026-F040",
        "status_before": "envoyée", "status_after": "envoyée",
        "amount_paid": 60000, "balance": 40000, "paid_in_full": False,
    }
    assert report["client_balance"] == {"book": 140000, "cleared": 140000}


def test_une_facture_d_un_autre_client_du_dossier_est_signalee_jamais_refusee(fake):
    """Deux clients au dossier : les fonds de c2 acquittent une facture de
    c1. Le client a pu l'autoriser — l'avocat vérifie ; le service le dit."""
    svc.compenser_fideicommis(
        [svc.enregistrer_ecriture_fideicommis(_deposit(client_id="c2", amount=100000,
                                                       date=_d(2026, 9, 3)))["entry"]["id"]],
        _d(2026, 9, 3))
    report = svc.enregistrer_paiement_honoraires(
        _fee(client_id="c2"), admin_account_id="ops1")
    assert report["ok"], report
    assert len(report["warnings"]) == 1
    assert "autre client du dossier" in report["warnings"][0]


def test_un_refus_du_paiement_rapporte_le_cote_et_n_ecrit_rien(fake):
    before = (fake.peek_collection("trust_transactions"),
              fake.peek_collection("admin_transactions"), fake.peek("invoices/inv1"))
    report = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops2")
    assert not report["ok"]
    assert report["reason"] == "compte_administration_fermé"
    assert report["side"] == "administration"
    assert report["trust_entry"] is None and report["invoice"] is None
    assert (fake.peek_collection("trust_transactions"),
            fake.peek_collection("admin_transactions"),
            fake.peek("invoices/inv1")) == before


def test_l_ecriture_ordinaire_refuse_le_paiement_d_honoraires(fake):
    report = svc.enregistrer_ecriture_fideicommis({**_fee(), "invoice_id": "inv1"})
    assert not report["ok"]
    assert report["reason"] == "paiement_honoraires_composite"


# ══════════════════════════════════════════════════════════════════════
# 3. La contre-passation choisit son chemin sur une lecture STRICTE
# ══════════════════════════════════════════════════════════════════════


def test_un_paiement_d_honoraires_se_contre_passe_avec_sa_recette(fake):
    paid = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    report = svc.contrepasser_ecriture_fideicommis(paid["trust_entry"]["id"], "chèque perdu")
    assert report["ok"], report
    assert report["original"] == {"id": paid["trust_entry"]["id"], "status_after": "annulée"}
    assert len(report["admin_reversals"]) == 1
    (block,) = report["invoices"]
    assert (block["status_before"], block["amount_paid"]) == ("envoyée", 0)
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0


def test_une_ecriture_ordinaire_se_contre_passe_au_fideicommis(fake):
    entry = svc.enregistrer_ecriture_fideicommis(
        _deposit(amount=5000, date=_d(2026, 9, 11)))["entry"]
    report = svc.contrepasser_ecriture_fideicommis(entry["id"], "erreur")
    assert report["ok"], report
    assert report["admin_reversals"] == [] and report["invoices"] == []
    assert report["original"]["status_after"] == "annulée"


def test_une_ecriture_introuvable_ou_illisible_est_refusee(fake, monkeypatch):
    """Régression — la route lisait l'original par get_transaction, qui
    échoue ouvert à None : illisible voulait dire « pas un paiement
    d'honoraires », et la contre-passation des recettes était sautée sans
    un mot."""
    report = svc.contrepasser_ecriture_fideicommis("fantome", "x")
    assert not report["ok"] and report["reason"] == "écriture_introuvable"
    paid = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    before = (fake.peek_collection("trust_transactions"),
              fake.peek_collection("admin_transactions"))

    def _boom(*_a, **_k):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(trust, "get_transaction_strict", _boom)
    report = svc.contrepasser_ecriture_fideicommis(paid["trust_entry"]["id"], "x")
    assert not report["ok"] and report["reason"] == "lecture_impossible"
    assert "rien n'a été contre-passé" in report["errors"][0]
    assert (fake.peek_collection("trust_transactions"),
            fake.peek_collection("admin_transactions")) == before


def test_un_solde_compense_negatif_est_signale(fake):
    """Une contre-passation échappe au contrôle de découvert : contre-passer
    un dépôt compensé dont les fonds ont déjà été déboursés rend le solde
    compensé du client NÉGATIF — un manque au fidéicommis, dit en toutes
    lettres."""
    spent = svc.enregistrer_ecriture_fideicommis(_deposit(
        direction="déboursé", purpose="déboursé_tiers", amount=150000,
        counterparty="Huissier", date=_d(2026, 9, 5)))
    assert spent["ok"], spent
    deposit = next(t for t in fake.peek_collection("trust_transactions").values()
                   if t.get("purpose") == "dépôt_client")
    report = svc.contrepasser_ecriture_fideicommis(deposit["id"], "chèque sans provision")
    assert report["ok"], report
    assert report["client_cleared_after"] == -150000
    assert any("devient négatif" in w for w in report["warnings"])


# ══════════════════════════════════════════════════════════════════════
# 4. La compensation
# ══════════════════════════════════════════════════════════════════════


def test_la_compensation_rapporte_les_fonds_liberes(fake):
    entry = svc.enregistrer_ecriture_fideicommis(
        _deposit(amount=30000, date=_d(2026, 9, 12)))["entry"]
    report = svc.compenser_fideicommis([entry["id"]], _d(2026, 9, 14))
    assert report["ok"], report
    assert report["released_funds"] == [{"dossier_id": "dos1", "client_id": "c1",
                                         "amount": 30000}]
    assert "disponibles pour un déboursé" in report["warnings"][0]


def test_la_compensation_exige_la_date_du_releve(fake):
    report = svc.compenser_fideicommis(["x"], None)
    assert not report["ok"] and report["reason"] == "date_compensation_requise"
    report = svc.compenser_administration(["x"], None)
    assert not report["ok"] and report["reason"] == "date_compensation_requise"


def test_un_refus_de_compensation_dit_pourquoi(fake):
    entry = svc.enregistrer_ecriture_fideicommis(
        _deposit(amount=30000, date=_d(2026, 9, 12)))["entry"]
    report = svc.compenser_fideicommis([entry["id"], entry["id"]], _d(2026, 9, 14))
    assert not report["ok"] and report["reason"] == "compensation_doublon"
    report = svc.compenser_fideicommis([entry["id"]], _d(2026, 9, 11))
    assert not report["ok"] and report["reason"] == "compensation_invalide"


# ══════════════════════════════════════════════════════════════════════
# 5. L'administration
# ══════════════════════════════════════════════════════════════════════


def _depense(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "dépense", "category": "loyer",
        "amount": 11498, "method": "virement", "counterparty": "Immeubles X",
        "date": _d(2026, 9, 10), "net_amount": 10000, "gst_amount": 500,
        "qst_amount": 998,
    }
    d.update(over)
    return d


def test_deja_compensee_est_une_seule_creation(fake):
    fake.reset_logs()
    report = svc.enregistrer_ecriture_administration(_depense(), cleared_date=_d(2026, 9, 10))
    assert report["ok"], report
    assert report["entry"]["status"] == "compensée"
    assert report["invoice"] is None
    assert len(fake.commits) == 1


def test_un_encaissement_rapporte_sa_facture(fake):
    report = svc.enregistrer_ecriture_administration({
        "account_id": "ops1", "kind": "encaissement_facture", "invoice_id": "inv1",
        "amount": 100000, "method": "virement", "counterparty": "C1",
        "date": _d(2026, 9, 10),
    })
    assert report["ok"], report
    assert report["invoice"]["status_after"] == "payée"
    assert report["invoice"]["paid_in_full"] is True


def test_la_modification_rapporte_un_conflit(fake):
    created = svc.enregistrer_ecriture_administration(_depense())["entry"]
    report = svc.modifier_ecriture_administration(
        created["id"], {"description": "Loyer"}, expected_etag=created["etag"])
    assert report["ok"] and report["changed_fields"] == ["description"]
    report = svc.modifier_ecriture_administration(
        created["id"], {"description": "Autre"}, expected_etag=created["etag"])
    assert not report["ok"] and report["stale"] is True


def test_une_recette_liee_au_fideicommis_ne_se_contre_passe_pas_ici(fake):
    paid = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    report = svc.contrepasser_ecriture_administration(paid["admin_recette"]["id"], "seule")
    assert not report["ok"] and report["reason"] == "écriture_liée_fideicommis"


def test_le_paiement_de_carte_rapporte_ses_deux_jambes(fake):
    report = svc.enregistrer_paiement_carte("ops1", "card1", 5000, _d(2026, 9, 10), "virement")
    assert report["ok"], report
    assert report["entry"]["account_id"] == "ops1"
    assert report["card_leg"]["account_id"] == "card1"
    assert svc.record_card_payment is svc.enregistrer_paiement_carte
    report = svc.enregistrer_paiement_carte("card1", "ops1", 5000, _d(2026, 9, 10), "virement")
    assert not report["ok"] and report["reason"] == "comptes_incompatibles"


def test_une_contre_passation_d_encaissement_rapporte_la_facture(fake):
    created = svc.enregistrer_ecriture_administration({
        "account_id": "ops1", "kind": "encaissement_facture", "invoice_id": "inv1",
        "amount": 40000, "method": "virement", "counterparty": "C1",
        "date": _d(2026, 9, 10),
    })["entry"]
    report = svc.contrepasser_ecriture_administration(created["id"], "NSF")
    assert report["ok"], report
    (block,) = report["invoices"]
    assert block["amount_paid"] == 0 and block["balance"] == 100000


def test_comptes_operations_actifs_distingue_l_absence_de_la_panne(fake, monkeypatch):
    comptes, lisible = svc.comptes_operations_actifs()
    assert [c["id"] for c in comptes] == ["ops1"] and lisible

    def _boom(**_k):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(al, "list_accounts", _boom)
    assert svc.comptes_operations_actifs() == ([], False)


def test_invoice_payment_block_est_pur():
    assert svc.invoice_payment_block(None, None) is None
    block = svc.invoice_payment_block(
        None, {"id": "i", "invoice_number": "N", "status": "payée",
               "amount_due": 100, "amount_paid": 100})
    assert block["status_before"] is None           # unknown, never invented
    assert block["paid_in_full"] is True and block["balance"] == 0


def test_le_service_n_atteint_aucune_collection_dav():
    """Ni l'un ni l'autre registre n'est exposé en DAV : le service n'importe
    rien de ``dav/``."""
    import ast

    tree = ast.parse((_ATHENA / "services" / "comptabilite.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any((m or "").startswith("dav") for m in modules), modules


def test_une_facture_nommee_deux_fois_est_ambigue_jamais_choisie(fake):
    """Par son identifiant ET par son numéro : refusé, jamais l'un préféré
    en silence à l'autre."""
    report = svc.enregistrer_paiement_honoraires(
        {**_fee(), "invoice_id": "inv9"}, admin_account_id="ops1")
    assert not report["ok"] and report["reason"] == "facture_ambiguë"
    assert fake.peek_collection("admin_transactions") == {}


# ══════════════════════════════════════════════════════════════════════
# 7. Les avertissements — dits vrais, et portés jusqu'à la page (revue du
#    lot 5a)
# ══════════════════════════════════════════════════════════════════════


def test_des_recettes_deja_contre_passees_ne_se_disent_pas_absentes(fake):
    """Régression (revue du lot 5a) — un paiement d'honoraires dont la
    recette liée avait déjà été contre-passée (par l'ancienne cascade de la
    route) se contre-passe au fidéicommis seul. Le rapport disait « Aucune
    recette au compte d'administration n'était liée (inscription antérieure
    au registre d'administration) » : faux — une recette était liée, le
    compte d'opérations avait bien vu l'argent, et c'est sa contre-passation
    qui l'avait retiré. Deux faits, deux phrases."""
    paid = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    assert paid["ok"], paid
    _, errs = al.reverse_transaction(paid["admin_recette"]["id"], "historique",
                                     allow_linked=True)
    assert errs == []
    report = svc.contrepasser_ecriture_fideicommis(paid["trust_entry"]["id"], "erreur")
    assert report["ok"], report
    assert report["admin_reversals"] == []
    assert report["warning_codes"] == ["recettes_deja_contre_passees"]
    assert report["warnings"] == [svc.WARNING_MESSAGES["recettes_deja_contre_passees"]]
    assert not any("Aucune recette" in w for w in report["warnings"])


def test_un_paiement_sans_recette_liee_le_dit_et_dit_quoi_faire(fake):
    from tests._accounting_history import legacy_fee_entry

    fee = legacy_fee_entry({**_fee(), "invoice_id": "inv1"})
    report = svc.contrepasser_ecriture_fideicommis(fee["id"], "erreur")
    assert report["ok"], report
    assert report["warning_codes"] == ["sans_recette_liee"]
    assert "contre-passez-la au registre d'administration" in report["warnings"][0]


def test_la_facture_d_un_autre_client_nomme_les_deux_clients(fake):
    """La revue demandait de refuser le cas, ou d'avertir EN NOMMANT les
    deux clients : l'avertissement du rapport les nomme (l'avocat, ou
    Claude au lot 5b, le lit) ; le code l'accompagne pour que la page web le
    dise sans qu'un nom voyage dans une URL."""
    doc = fake.peek("invoices/inv1")
    doc.update(client_name="Jean Tremblay")
    fake.external_write("invoices/inv1", doc)
    deposit = svc.enregistrer_ecriture_fideicommis(
        _deposit(client_id="c2", amount=100000, date=_d(2026, 9, 3)))
    svc.compenser_fideicommis([deposit["entry"]["id"]], _d(2026, 9, 3))
    report = svc.enregistrer_paiement_honoraires(_fee(client_id="c2"),
                                                 admin_account_id="ops1")
    assert report["ok"], report
    assert report["warning_codes"] == ["facture_autre_client"]
    # The page the web lands on can say it (an unknown code shows nothing).
    assert set(report["warning_codes"]) <= set(svc.WARNING_MESSAGES)
    (warning,) = report["warnings"]
    assert "Jean Tremblay" in warning and "C2" in warning
    # Same client: no warning, no code.
    same = svc.enregistrer_paiement_honoraires(_fee(amount=10000), admin_account_id="ops1")
    assert same["ok"] and same["warning_codes"] == [] and same["warnings"] == []



# ══════════════════════════════════════════════════════════════════════
# Revue du lot 5a (concurrence) — l'issue inconnue traverse le service
# ══════════════════════════════════════════════════════════════════════


def test_une_issue_inconnue_traverse_le_service_sous_sa_raison(fake, monkeypatch):
    """Le lot 5b doit GARDER la réservation de clé d'un paiement dont
    l'issue est inconnue (keep_claim, la doctrine de create_invoice) : il la
    reconnaît à la raison « issue_incertaine » que le service transmet,
    jamais à un texte. Ici le commit ABOUTIT puis sa réponse se perd."""
    from google.api_core import exceptions as gexc

    from models import fee_payment

    server = fake._fake_server
    real_commit = server.commit
    armed = {"on": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if armed["on"] and any("/trust_transactions/" in server._write_name(w)
                               for w in writes):
            armed["on"] = False
            raise gexc.Aborted("the retried commit of a landed transaction")
        return response

    monkeypatch.setattr(server, "commit", _commit)
    report = svc.enregistrer_paiement_honoraires(_fee(), admin_account_id="ops1")
    assert report["ok"] is False
    assert report["reason"] == "issue_incertaine"
    assert report["errors"] == [fee_payment.CREATE_OUTCOME_UNCERTAIN]
    fees = [t for t in fake.peek_collection("trust_transactions").values()
            if t.get("purpose") == "virement_honoraires"]
    assert len(fees) == 1                              # landed, ONCE
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000
