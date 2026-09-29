"""Le paiement d'honoraires : UNE transaction, trois écritures (lot 5a,
étape 3 — décisions D1, D2, D14, D16, D-4).

Un paiement d'honoraires sort les honoraires du fidéicommis, les dépose au
compte d'opérations et les inscrit comme payés sur la facture. Jusqu'ici la
ROUTE du fidéicommis écrivait le retrait, puis — après le commit, en échec
ouvert — la recette d'administration et le paiement de la facture. Trois
défauts, tous silencieux, que chaque test marqué « régression » attrape sur
l'ancien code :

1. **Tout ou rien.** Un refus côté administration (compte fermé, carte de
   crédit, période conciliée) laissait le retrait COMMIS au fidéicommis,
   sous une bannière. Désormais rien n'est écrit, nulle part.
2. **La course.** Deux paiements parallèles sur une facture passaient tous
   deux le plafond du fidéicommis — le montant encaissé ne bougeait
   qu'après. La facture fait maintenant partie de l'ensemble lu.
3. **La contre-passation en cascade.** La route contre-passait le
   fidéicommis, puis chaque recette, une par une, en échec ouvert ; une
   lecture ratée du lien voulait dire « rien à faire ». Désormais le
   fidéicommis, CHAQUE recette liée et leurs factures tiennent dans un seul
   commit, et une lecture ratée refuse.

Et la règle D16 : la recette porte SA date (le dépôt au compte d'opérations),
par défaut celle du retrait ; jamais avant, jamais future à Montréal, jamais
dans une période conciliée — ce qui permet d'inscrire un retrait du 30 août
déposé le 2 septembre même une fois août concilié au compte d'opérations.

Les sept tests de ``tests/test_trust.py`` qui écrivaient un paiement
d'honoraires par ``trust.create_transaction`` (qui refuse désormais l'objet)
sont ici, assertions inchangées (section 2).

Les décisions de l'avocat du 2026-09-29 (section 7) : D20 — le refus d'une
facture non envoyée nomme le juriste comme celui qui atteste l'envoi, sans
jamais dire comment passer outre ; D21 — les fonds d'un client n'acquittent
jamais la facture d'un AUTRE client du dossier, refus dans la transaction,
sans dérogation (c'était un avertissement après le commit) ; D23 — le
bénéficiaire est l'avocat ou son cabinet, tels que les nomme le profil du
cabinet (art. 58).

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais ; on relit ce qui est STOCKÉ.
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
    from models import fee_payment
    from models import invoice as invoice_model
    from models import provenance
    from models import settings as settings_model
    from models import trust

from tests._accounting_history import FEE_PAYEE, legacy_fee_entry  # noqa: E402
from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _freeze(monkeypatch, iso: str) -> None:
    """Freeze the ONE Montréal clock read (``utils.deadlines.today_mtl``)."""
    from utils import deadlines as dl

    frozen = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(dl, "datetime", _Clock)


@pytest.fixture
def fake(monkeypatch):
    _freeze(monkeypatch, "2026-09-20T16:00:00+00:00")
    f = install(monkeypatch, *_fake_modules())
    f.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "t0",
    })
    f.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "client_ids": ["c1", "c2"],
        "clients": [{"id": "c1", "name": "Jean Tremblay"},
                    {"id": "c2", "name": "Marie Roy"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "a0",
    })
    f.seed("admin_accounts/card1", {
        "id": "card1", "name": "Carte", "status": "actif",
        "account_type": "carte_crédit", "ledger_balance": 0, "etag": "c0",
    })
    for iid, number, due in (("inv1", "2026-F040", 100000), ("inv2", "2026-F041", 50000)):
        f.seed(f"invoices/{iid}", {
            "id": iid, "invoice_number": number, "dossier_id": "dos1",
            "dossier_file_number": "2026-001", "dossier_title": "T c. X",
            "client_id": "c1", "status": "envoyée", "total": due,
            "retainer_applied": 0, "amount_due": due, "amount_paid": 0,
            "paid_date": None, "etag": f"{iid}-e0",
        })
    # A cleared deposit of 2 000 $ for c1 — the funds the overdraft control
    # (art. 59) releases.
    receipt, errs = trust.create_transaction(_entry(
        direction="recette", purpose="dépôt_client", amount=200000,
        counterparty="Jean Tremblay", date=_d(2026, 9, 2)))
    assert errs == [], errs
    _, errs = trust.clear_transaction(receipt["id"], _d(2026, 9, 2))
    assert errs == [], errs
    return f


def _entry(**over) -> dict:
    d = {
        "account_id": "acc1", "direction": "déboursé", "amount": 60000,
        "purpose": "virement_honoraires", "method": "chèque",
        "counterparty": FEE_PAYEE, "dossier_id": "dos1",
        "client_id": "c1", "date": _d(2026, 9, 10), "invoice_id": "inv1",
        "description": "", "reference": "1042",
    }
    d.update(over)
    return d


def _pay(admin_account_id: str = "ops1", **over):
    admin = {k: over.pop(k) for k in ("admin_date", "allow_external_ref") if k in over}
    return fee_payment.create_fee_payment(
        _entry(**over), admin_account_id=admin_account_id, **admin)


def _snapshot(fake) -> dict:
    """Everything a fee payment may touch."""
    return {
        "trust": fake.peek_collection("trust_transactions"),
        "trust_account": fake.peek("trust_accounts/acc1"),
        "trust_counter": fake.peek("counters/trust-acc1"),
        "dossier": fake.peek("dossiers/dos1"),
        "admin": fake.peek_collection("admin_transactions"),
        "ops1": fake.peek("admin_accounts/ops1"),
        "card1": fake.peek("admin_accounts/card1"),
        "admin_counters": (fake.peek("counters/admin-ops1"),
                           fake.peek("counters/admin-card1")),
        "invoices": fake.peek_collection("invoices"),
    }


def _fees(fake) -> list:
    return [t for t in fake.peek_collection("trust_transactions").values()
            if t.get("purpose") == "virement_honoraires"]


def _commit_touching(fake, rel: str) -> list:
    return [c for c in fake.commits if any(path == rel for _op, path in c.ops)]


# ══════════════════════════════════════════════════════════════════════
# 1. Le chemin heureux : trois écritures, un seul commit
# ══════════════════════════════════════════════════════════════════════


def test_le_paiement_ecrit_fideicommis_recette_et_facture_en_un_commit(fake):
    fake.reset_logs()
    result, errs = _pay()
    assert errs == [], errs
    entry, recette = result["trust_entry"], result["admin_recette"]
    assert len(fake.commits) == 1
    ops = fake.commits[0].ops
    assert ("set", f"trust_transactions/{entry['id']}") in ops
    assert ("set", f"admin_transactions/{recette['id']}") in ops
    assert ("update", "invoices/inv1") in ops

    stored = fake.peek(f"admin_transactions/{recette['id']}")
    assert stored["trust_transaction_id"] == entry["id"]
    assert stored["kind"] == "encaissement_facture"
    assert stored["direction"] == "recette"
    assert stored["invoice_id"] == "inv1"
    assert stored["amount"] == 60000
    assert stored["method"] == "chèque"          # the trust method, never « virement » by fiat
    assert stored["counterparty"] == "Jean Tremblay"
    assert stored["description"] == "Paiement d'honoraires du fidéicommis — dossier 2026-001"
    assert stored["reference"] == "1042"
    assert stored["date"] == _d(2026, 9, 10)     # D16 default: the trust date
    assert stored["status"] == "en_circulation"

    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 60000
    assert invoice["paid_date"] == _d(2026, 9, 10)
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 60000
    assert fake.peek("dossiers/dos1")["trust_cleared_by_client"]["c1"] == 140000
    assert result["client_balance"] == {"book": 140000, "cleared": 140000}
    assert result["invoice_before"]["amount_paid"] == 0
    assert result["invoice_after"]["amount_paid"] == 60000


def test_le_solde_atteint_zero_la_facture_passe_a_payee_dans_le_meme_commit(fake):
    result, errs = _pay(amount=100000)
    assert errs == [], errs
    assert fake.peek("invoices/inv1")["status"] == "payée"
    assert result["invoice_after"]["status"] == "payée"


def test_la_provenance_du_connecteur_est_estampillee_et_le_commit_note(fake):
    """Pour le lot 5b : sous ``writing_via("mcp")`` les trois écritures
    portent la provenance du connecteur, et le protocole d'écriture voit les
    trois commits (la base de « ENREGISTRÉE — NE PAS RÉESSAYER »)."""
    with provenance.writing_via("mcp", tool="record_trust_fee_payment"):
        result, errs = _pay()
        committed = set(provenance.committed_writes())
    assert errs == [], errs
    entry, recette = result["trust_entry"], result["admin_recette"]
    assert fake.peek(f"trust_transactions/{entry['id']}")["created_via"] == "mcp"
    assert fake.peek(f"admin_transactions/{recette['id']}")["created_via"] == "mcp"
    assert fake.peek("invoices/inv1")["updated_via"] == "mcp"
    assert {("trust_transactions", entry["id"]),
            ("admin_transactions", recette["id"]),
            ("invoices", "inv1")} <= committed


# ══════════════════════════════════════════════════════════════════════
# 2. Les règles du fidéicommis — déplacées de tests/test_trust.py
# ══════════════════════════════════════════════════════════════════════


def test_virement_honoraires_exceeds_invoice_refused(fake):
    """(Déplacé de test_trust.py, assertion inchangée.)"""
    _, errs = _pay(amount=110000)
    assert errs and "solde dû" in errs[0].lower()


def test_virement_honoraires_caps_on_the_live_balance_since_lot_p(fake):
    """(Déplacé de test_trust.py.) The cap reads amount_due − amount_paid."""
    doc = fake.peek("invoices/inv1")
    doc.update(amount_paid=60000)
    fake.external_write("invoices/inv1", doc)
    _, errs = _pay(amount=50000)
    assert errs and "solde dû" in errs[0].lower()
    result, errs = _pay(amount=40000)
    assert errs == [], errs
    assert fake.peek("invoices/inv1")["amount_paid"] == 100000


def test_virement_honoraires_on_draft_invoice_refused(fake):
    """(Déplacé de test_trust.py.) Art. 56 2° — « facturation envoyée ».
    Réécrit sur la décision D20 (2026-09-29) : l'assertion lisait « émise »
    dans le refus ; le refus nomme désormais le JURISTE comme celui qui
    atteste l'envoi, et la règle — jamais un moyen de passer outre."""
    doc = fake.peek("invoices/inv1")
    doc.update(status="brouillon")
    fake.external_write("invoices/inv1", doc)
    before = _snapshot(fake)
    _, errs = _pay()
    assert errs == [trust._ABORT_MESSAGES["facture_non_émise"]]
    assert "envoyée par le juriste" in errs[0] and "art. 56 2°" in errs[0]
    assert _snapshot(fake) == before


def test_virement_with_external_ref_allowed_on_the_web_path(fake):
    """(Déplacé de test_trust.py.) The paper-invoice path — the lawyer's
    2026-07-17 decision — opened by ``allow_external_ref`` (the web form),
    its recette a « recette_autre » citing the number, atomic too."""
    result, errs = _pay(invoice_id=None, invoice_external_ref="INV-2019-042",
                        allow_external_ref=True)
    assert errs == [], errs
    entry, recette = result["trust_entry"], result["admin_recette"]
    assert entry["invoice_external_ref"] == "INV-2019-042"
    assert entry["invoice_id"] is None
    assert recette["kind"] == "recette_autre"
    assert recette["invoice_id"] is None
    assert recette["dossier_id"] == "dos1"
    assert "INV-2019-042" in recette["description"]
    assert result["invoice_before"] is None and result["invoice_after"] is None
    assert fake.peek_collection("invoices")["inv1"]["amount_paid"] == 0


def test_the_external_ref_is_refused_by_default(fake):
    """D1: the connector records a fee payment against an ATHÉNA invoice
    only — the default refuses a paper number before any read."""
    before = _snapshot(fake)
    _, errs = _pay(invoice_id=None, invoice_external_ref="INV-2019-042")
    assert errs == [fee_payment._MESSAGES["facture_externe_refusée"]]
    _, errs = _pay(invoice_id=None)
    assert errs == [fee_payment._MESSAGES["facture_athena_requise"]]
    assert _snapshot(fake) == before


def test_virement_without_any_invoice_refused(fake):
    """(Déplacé de test_trust.py.)"""
    _, errs = _pay(invoice_id=None, allow_external_ref=True)
    assert errs and "facture" in errs[0].lower()


def test_virement_with_both_invoice_and_external_refused(fake):
    """(Déplacé de test_trust.py.)"""
    _, errs = _pay(invoice_external_ref="INV-2019-042", allow_external_ref=True)
    assert errs and "jamais les deux" in errs[0].lower()


def test_valid_athena_invoice_still_verified_with_no_external(fake):
    """(Déplacé de test_trust.py.)"""
    result, errs = _pay(amount=40000)
    assert errs == []
    assert result["trust_entry"]["invoice_id"] == "inv1"
    assert result["trust_entry"]["invoice_external_ref"] == ""


def test_an_invoice_of_another_dossier_is_refused(fake):
    fake.seed("dossiers/dos9", {"id": "dos9", "file_number": "2026-009",
                                "client_ids": [], "clients": []})
    doc = fake.peek("invoices/inv1")
    doc.update(dossier_id="dos9")
    fake.external_write("invoices/inv1", doc)
    before = _snapshot(fake)
    _, errs = _pay()
    assert errs == [trust._ABORT_MESSAGES["facture_autre_dossier"]]
    assert _snapshot(fake) == before


def test_art_58_and_the_provision_rule_hold_on_the_composite(fake):
    before = _snapshot(fake)
    _, errs = _pay(method="traite")
    assert errs == [trust._ABORT_MESSAGES["mode_retrait_honoraires"]]
    doc = fake.peek("invoices/inv1")
    doc.update(retainer_applied=10000, amount_due=90000)
    fake.external_write("invoices/inv1", doc)
    before["invoices"]["inv1"] = fake.peek("invoices/inv1")
    _, errs = _pay()
    assert errs == [trust._ABORT_MESSAGES["facture_avec_provision"]]
    assert _snapshot(fake) == before


# ══════════════════════════════════════════════════════════════════════
# 3. Tout ou rien — un refus d'UN côté n'écrit rien des DEUX côtés
# ══════════════════════════════════════════════════════════════════════


def _close(fake, path: str) -> None:
    doc = fake.peek(path)
    doc.update(status="fermé")
    fake.external_write(path, doc)


def _complete_admin_rec(fake, account_id: str, period_end: datetime) -> None:
    fake.seed(f"admin_reconciliations/rec-{account_id}", {
        "id": f"rec-{account_id}", "account_id": account_id,
        "period_end": period_end, "status": "complétée",
    })


@pytest.mark.parametrize("label, setup, reason", [
    ("compte d'administration fermé",
     lambda f: _close(f, "admin_accounts/ops1"), "compte_administration_fermé"),
    ("période d'administration conciliée",
     lambda f: _complete_admin_rec(f, "ops1", _d(2026, 9, 15)),
     "date_administration_verrouillée"),
])
def test_un_refus_cote_administration_ne_laisse_aucune_ecriture_au_fideicommis(
    fake, label, setup, reason
):
    """Régression — l'ancienne route COMMETTAIT le retrait au fidéicommis,
    puis la recette était refusée : les fonds du client sortis, rien au
    compte d'opérations, une bannière. (Vérifié en rétablissant la route :
    l'écriture au fidéicommis restait, la recette manquait.)"""
    setup(fake)
    before = _snapshot(fake)
    report: dict = {}
    result, errs = fee_payment.create_fee_payment(
        _entry(), admin_account_id="ops1", _report_out=report)
    assert result is None, label
    assert errs == [fee_payment._message(reason, "2026-09-10")]
    assert report == {"reason": reason, "side": "administration"}
    assert _snapshot(fake) == before


def test_une_carte_de_credit_n_est_jamais_le_compte_du_depot(fake):
    before = _snapshot(fake)
    _, errs = _pay(admin_account_id="card1")
    assert errs == [fee_payment._MESSAGES["compte_administration_invalide"]]
    assert _snapshot(fake) == before
    # …on the paper-invoice path too: a « recette_autre » has no type
    # check of its own, the composite brings it.
    _, errs = _pay(admin_account_id="card1", invoice_id=None,
                   invoice_external_ref="P-1", allow_external_ref=True)
    assert errs == [fee_payment._MESSAGES["compte_administration_invalide"]]
    assert _snapshot(fake) == before


def test_un_compte_d_administration_inconnu_refuse_tout(fake):
    before = _snapshot(fake)
    _, errs = _pay(admin_account_id="fantome")
    assert errs == [fee_payment._MESSAGES["compte_administration_introuvable"]]
    assert _snapshot(fake) == before


def test_d4_le_compte_d_administration_est_requis_avant_toute_lecture(fake):
    fake.reset_logs()
    _, errs = _pay(admin_account_id="")
    assert errs == [fee_payment._MESSAGES["compte_administration_requis"]]
    assert fake.reads == [] and fake.commits == []


def test_une_facture_qui_refuse_le_paiement_refuse_tout(fake, monkeypatch):
    """La facture a le dernier mot sur son paiement — et ce mot annule le
    retrait au fidéicommis avec lui."""
    def _refuse(*_a, **_k):
        raise invoice_model.PaymentRefused("La facture refuse ce paiement.")

    monkeypatch.setattr(invoice_model, "payment_updates", _refuse)
    before = _snapshot(fake)
    _, errs = _pay()
    assert errs and errs[0].startswith("Recette au compte d'administration refusée :")
    assert "La facture refuse ce paiement." in errs[0]
    assert _snapshot(fake) == before


def test_un_refus_cote_fideicommis_n_ecrit_rien_non_plus(fake):
    """Art. 59 — un retrait au-delà des fonds compensés : ni l'écriture, ni
    la recette, ni le paiement."""
    doc = fake.peek("invoices/inv1")
    doc.update(amount_due=500000, total=500000)
    fake.external_write("invoices/inv1", doc)
    before = _snapshot(fake)
    report: dict = {}
    _, errs = fee_payment.create_fee_payment(
        _entry(amount=250000), admin_account_id="ops1", _report_out=report)
    assert errs == [trust._ABORT_MESSAGES["solde_compensé_insuffisant"]]
    assert report == {"reason": "solde_compensé_insuffisant", "side": "fidéicommis"}
    assert _snapshot(fake) == before


def test_le_chemin_public_refuse_le_paiement_d_honoraires_seul(fake):
    """D-4 structurel : ni la création ni la contre-passation publiques du
    fidéicommis n'écrivent le volet seul."""
    before = _snapshot(fake)
    entry, errs = trust.create_transaction(_entry())
    assert entry is None
    assert errs == [trust._ABORT_MESSAGES["paiement_honoraires_composite"]]
    assert _snapshot(fake) == before
    result, _ = _pay()
    before = _snapshot(fake)
    _, errs = trust.reverse_transaction(result["trust_entry"]["id"], "erreur")
    assert errs == [trust._ABORT_MESSAGES["paiement_honoraires_contre_passation"]]
    assert _snapshot(fake) == before


# ══════════════════════════════════════════════════════════════════════
# 4. D16 — la recette porte la date du dépôt
# ══════════════════════════════════════════════════════════════════════


def test_une_periode_d_administration_conciliee_n_empeche_pas_un_depot_posterieur(fake):
    """Le cas de la revue : un chèque d'honoraires tiré du fidéicommis le
    10 septembre, déposé au compte d'opérations le 17 — après que la période
    jusqu'au 15 a été conciliée. Avec la date du fidéicommis la recette
    tomberait dans la période close ; avec SA date, tout s'inscrit, et le
    paiement de la facture porte la date du dépôt."""
    _complete_admin_rec(fake, "ops1", _d(2026, 9, 15))
    result, errs = _pay(admin_date=_d(2026, 9, 17))
    assert errs == [], errs
    assert result["trust_entry"]["date"] == _d(2026, 9, 10)
    assert fake.peek(f"admin_transactions/{result['admin_recette']['id']}")["date"] == _d(2026, 9, 17)
    assert fake.peek("invoices/inv1")["paid_date"] == _d(2026, 9, 17)


def test_la_date_du_depot_ne_precede_jamais_le_retrait(fake):
    before = _snapshot(fake)
    _, errs = _pay(admin_date=_d(2026, 9, 9))
    assert errs == [fee_payment._MESSAGES["date_administration_antérieure"]]
    assert _snapshot(fake) == before


def test_la_bande_du_soir_juge_le_depot_au_calendrier_de_montreal(fake, monkeypatch):
    """21 h 30 HAE le 25 septembre — la date UTC est déjà le 26. Un retrait
    et un dépôt du 25 passent ; un dépôt daté du 26 est FUTUR à Montréal et
    refusé. (Une comparaison UTC accepterait le 26.) La contre-passation du
    soir est datée du 25 dans les DEUX registres."""
    _freeze(monkeypatch, "2026-09-26T01:30:00+00:00")
    before = _snapshot(fake)
    _, errs = _pay(date=_d(2026, 9, 25), admin_date=_d(2026, 9, 26))
    assert errs == [fee_payment._MESSAGES["date_administration_future"]]
    assert _snapshot(fake) == before
    result, errs = _pay(date=_d(2026, 9, 25))
    assert errs == [], errs
    reversal, errs = fee_payment.reverse_fee_payment(
        result["trust_entry"]["id"], "chèque perdu")
    assert errs == [], errs
    assert reversal["trust_reversal"]["date"] == _d(2026, 9, 25)
    (adm,) = reversal["admin_reversals"]
    assert fake.peek(f"admin_transactions/{adm['reversal_id']}")["date"] == _d(2026, 9, 25)


# ══════════════════════════════════════════════════════════════════════
# 5. Les courses — la facture est dans l'ensemble lu
# ══════════════════════════════════════════════════════════════════════


def test_deux_paiements_paralleles_se_serialisent(fake):
    """Régression — le cas D2 : deux paiements de 600 $ sur une facture de
    1 000 $, simultanés. L'ancien code les inscrivait TOUS LES DEUX au
    fidéicommis (le plafond lisait un montant encaissé que seule la recette
    postérieure faisait bouger), puis refusait la seconde recette : 1 200 $
    sortis des fonds du client pour une facture de 1 000 $. Le second commit
    est maintenant interrompu, rejoué sur le solde réel, et refusé — rien
    de lui n'est écrit."""
    rival: dict = {}

    def _race(info):
        if rival or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["result"], rival["errs"] = _pay()

    remove = fake.add_commit_hook(_race)
    try:
        result, errs = _pay()
    finally:
        remove()
    assert rival["errs"] == [] and rival["result"]
    assert result is None
    assert errs == [trust._ABORT_MESSAGES["virement_excède_facture"]]
    assert len(_fees(fake)) == 1
    assert len(fake.peek_collection("admin_transactions")) == 1
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000
    assert fake.peek("counters/trust-acc1")["seq"] == 2


def test_un_paiement_et_une_contre_passation_paralleles_se_serialisent(
    fake, monkeypatch, capsys
):
    """Un paiement A de 600 $ est inscrit ; puis, EN MÊME TEMPS, un paiement
    B de 400 $ sur la même facture et la contre-passation de A. La
    contre-passation commet pendant la tentative de B, qui lisait encore
    A debout : sans la facture dans son ensemble lu, B écrirait
    « 600 + 400 = 1 000 $ encaissés » — un montant absolu périmé, la
    facture « payée » pour 400 $ réellement reçus. B est interrompu, rejoué
    sur la facture réelle, et inscrit 400 $. Rien n'est perdu ni compté deux
    fois, et les deux contrôles d'intégrité le confirment."""
    first, errs = _pay(date=_d(2026, 9, 10))
    assert errs == []
    raced: dict = {}

    def _race(info):
        if raced or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        raced["done"] = True
        raced["result"], raced["errs"] = fee_payment.reverse_fee_payment(
            first["trust_entry"]["id"], "chèque perdu")

    remove = fake.add_commit_hook(_race)
    try:
        second, errs = _pay(amount=40000, date=_d(2026, 9, 20))
    finally:
        remove()
    assert raced["errs"] == [], raced["errs"]
    assert errs == [], errs
    invoice = fake.peek("invoices/inv1")
    assert invoice["amount_paid"] == 40000              # B alone, never 100 000
    assert invoice["status"] == "envoyée"
    assert al.sum_invoice_receipts("inv1") == 40000     # the register agrees
    assert fake.peek(f"trust_transactions/{first['trust_entry']['id']}")["reversed_by_id"]

    from scripts import verify_admin_integrity as vai
    from scripts import verify_trust_integrity as vti

    # Every module the scripts reach must read THIS store — derived again
    # now that the scripts (and what they import) are loaded.
    install(monkeypatch, *_fake_modules(), vti, vai, fake=fake)
    for script in (vti, vai):
        code = script.main()
        out = capsys.readouterr().out
        assert code == 0, out


# ══════════════════════════════════════════════════════════════════════
# 6. La contre-passation : le fidéicommis et CHAQUE recette, un commit
# ══════════════════════════════════════════════════════════════════════


def _split_fee(fake, shares) -> dict:
    """The reprise's split shape — one transfer carrying several recettes
    (HISTORY: the trust leg rebuilt through the model's phases)."""
    fee = legacy_fee_entry(_entry(amount=sum(a for _i, a in shares), invoice_id=None,
                                  invoice_external_ref="REPRISE-1"))
    for invoice_id, amount in shares:
        _, errs = al.create_transaction({
            "account_id": "ops1", "kind": "encaissement_facture",
            "invoice_id": invoice_id, "amount": amount, "method": "virement",
            "counterparty": "Fidéicommis", "date": _d(2026, 9, 10),
        }, trust_transaction_id=fee["id"])
        assert errs == [], errs
    return fee


def test_la_contre_passation_prend_toutes_les_recettes_en_un_commit(fake):
    """Régression — l'ancienne cascade (routes/trust) contre-passait le
    fidéicommis, PUIS chaque recette dans son propre commit ; plus tôt
    encore, la première seulement. Ici : la contre-passation du fidéicommis,
    celle des deux recettes et la réduction des deux factures tiennent dans
    UN commit."""
    fee = _split_fee(fake, [("inv1", 30000), ("inv2", 20000)])
    fake.reset_logs()
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "chèque perdu")
    assert errs == [], errs
    assert len(fake.commits) == 1
    assert len(result["admin_reversals"]) == 2
    linked = [t for t in fake.peek_collection("admin_transactions").values()
              if t.get("trust_transaction_id") == fee["id"]]
    assert all(t.get("reversed_by_id") for t in linked)
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    assert fake.peek("invoices/inv2")["amount_paid"] == 0
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0
    assert result["original_status_after"] == "annulée"
    reasons = {fake.peek(f"admin_transactions/{r['reversal_id']}")["description"]
               for r in result["admin_reversals"]}
    assert reasons == {"Contre-passation du paiement d'honoraires au fidéicommis — chèque perdu"}


def test_une_recette_deja_contre_passee_est_sautee(fake):
    fee = _split_fee(fake, [("inv1", 30000), ("inv2", 20000)])
    first = next(t for t in fake.peek_collection("admin_transactions").values()
                 if t.get("invoice_id") == "inv1")
    _, errs = al.reverse_transaction(first["id"], "historique", allow_linked=True)
    assert errs == []
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "chèque perdu")
    assert errs == [], errs
    assert [r["admin_transaction_id"] for r in result["admin_reversals"]] != [first["id"]]
    assert len(result["admin_reversals"]) == 1


def test_une_lecture_ratee_des_recettes_refuse_sans_rien_ecrire(fake, monkeypatch):
    from google.cloud.firestore_v1.collection import CollectionReference

    fee = _split_fee(fake, [("inv1", 30000)])
    before = _snapshot(fake)
    real_where = CollectionReference.where

    def _where(self, *args, **kwargs):
        flt = kwargs.get("filter")
        if getattr(flt, "field_path", None) == "trust_transaction_id":
            raise RuntimeError("firestore indisponible")
        return real_where(self, *args, **kwargs)

    monkeypatch.setattr(CollectionReference, "where", _where)
    report: dict = {}
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "x", _report_out=report)
    monkeypatch.setattr(CollectionReference, "where", real_where)
    assert result is None
    assert errs == [fee_payment._MESSAGES["recettes_illisibles"]]
    assert report["reason"] == "recettes_illisibles"
    assert _snapshot(fake) == before


def test_un_paiement_inferieur_a_la_recette_refuse_toute_la_contre_passation(fake):
    fee = _split_fee(fake, [("inv1", 30000), ("inv2", 20000)])
    doc = fake.peek("invoices/inv2")
    doc.update(amount_paid=5000)
    fake.external_write("invoices/inv2", doc)
    before = _snapshot(fake)
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "x")
    assert result is None
    assert al._ABORT_MESSAGES["paiement_facture_incohérent"] in errs[0]
    assert "Rien n'a été contre-passé" in errs[0]
    assert _snapshot(fake) == before


def test_un_paiement_historique_sans_recette_se_contre_passe_au_fideicommis_seul(fake):
    fee = legacy_fee_entry(_entry(amount=30000))
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "honoraires remboursés")
    assert errs == [], errs
    assert result["admin_reversals"] == [] and result["invoices"] == []
    assert fake.peek(f"trust_transactions/{fee['id']}")["reversed_by_id"]


def test_une_periode_d_administration_couvrant_aujourd_hui_refuse_tout(fake):
    result, _ = _pay()
    _complete_admin_rec(fake, "ops1", _d(2026, 9, 20))
    before = _snapshot(fake)
    rev, errs = fee_payment.reverse_fee_payment(result["trust_entry"]["id"], "x")
    assert rev is None
    assert errs == [fee_payment._MESSAGES["contre_passation_administration_verrouillée"]]
    assert _snapshot(fake) == before


def test_ce_chemin_ne_contre_passe_que_des_paiements_d_honoraires(fake):
    deposit = next(t for t in fake.peek_collection("trust_transactions").values()
                   if t.get("purpose") == "dépôt_client")
    before = _snapshot(fake)
    rev, errs = fee_payment.reverse_fee_payment(deposit["id"], "x")
    assert rev is None
    assert errs == [fee_payment._MESSAGES["pas_un_paiement_honoraires"]]
    assert _snapshot(fake) == before


def test_une_recette_liee_ne_se_contre_passe_toujours_pas_seule(fake):
    """Le côté administration garde sa règle : la recette d'un paiement
    d'honoraires se contre-passe depuis le fidéicommis, jamais seule."""
    result, _ = _pay()
    _, errs = al.reverse_transaction(result["admin_recette"]["id"], "seule")
    assert errs == [al._ABORT_MESSAGES["écriture_liée_fideicommis"]]


# ══════════════════════════════════════════════════════════════════════
# 7. Revue du lot 5a — ce que les refus et les résultats disent
# ══════════════════════════════════════════════════════════════════════


def test_une_conciliation_couvrant_aujourd_hui_ne_renvoie_pas_a_une_date_impossible(fake):
    """Régression (revue du lot 5a) — la période d'administration conciliée
    JUSQU'À AUJOURD'HUI. Le refus disait d'indiquer une date de dépôt
    « postérieure à la conciliation » : demain, que la garde du futur
    refuse — une consigne que le formulaire ne permet pas de suivre. Il dit
    maintenant de réessayer demain (le « période_verrouillée_jour » du
    fidéicommis). Une période close AVANT aujourd'hui garde la consigne de
    la date réelle, postérieure."""
    _complete_admin_rec(fake, "ops1", _d(2026, 9, 20))      # the frozen today
    before = _snapshot(fake)
    report: dict = {}
    result, errs = fee_payment.create_fee_payment(
        _entry(), admin_account_id="ops1", _report_out=report)
    assert result is None
    assert report == {"reason": "date_administration_verrouillée_jour",
                      "side": "administration"}
    assert errs == [fee_payment._message("date_administration_verrouillée_jour", "2026-09-20")]
    assert "Réessayez demain" in errs[0] and "postérieure à la conciliation" not in errs[0]
    assert _snapshot(fake) == before
    # …and with a later deposit date typed, it is the same honest refusal.
    _, errs = _pay(admin_date=_d(2026, 9, 20))
    assert "Réessayez demain" in errs[0]
    assert _snapshot(fake) == before


def test_la_contre_passation_compte_toutes_les_recettes_liees(fake):
    """``admin_reversals`` vide recouvre DEUX faits : aucune recette ne fut
    jamais liée (un paiement historique), ou toutes étaient déjà
    contre-passées. ``linked_recettes`` les distingue."""
    fee = _split_fee(fake, [("inv1", 30000), ("inv2", 20000)])
    for row in [t for t in fake.peek_collection("admin_transactions").values()
                if t.get("trust_transaction_id") == fee["id"]]:
        _, errs = al.reverse_transaction(row["id"], "historique", allow_linked=True)
        assert errs == []
    result, errs = fee_payment.reverse_fee_payment(fee["id"], "x")
    assert errs == [], errs
    assert result["admin_reversals"] == [] and result["linked_recettes"] == 2

    # Dated today: the register refuses to backdate behind the reversal.
    legacy = legacy_fee_entry(_entry(amount=30000, date=_d(2026, 9, 20)))
    result, errs = fee_payment.reverse_fee_payment(legacy["id"], "x")
    assert errs == [], errs
    assert result["admin_reversals"] == [] and result["linked_recettes"] == 0


# ══════════════════════════════════════════════════════════════════════
# 8. Revue du lot 5a (concurrence) — une tentative qui a ABOUTI
# ══════════════════════════════════════════════════════════════════════
#
# Le commit d'une transaction peut ABOUTIR et sa réponse se perdre : le
# client rejoue alors le commit sous le même identifiant de transaction
# (« ServiceUnavailable »), la reprise répond « Aborted », et le décorateur
# « transactional » réexécute le corps PAR-DESSUS les écritures qui ont
# abouti (la doctrine models/invoice._OwnCommitLandedError, revue des
# correctifs du lot 3). Ou le commit lève autre chose (« DeadlineExceeded »)
# après avoir abouti. Dans les deux cas, rien ne prouve que rien n'a été
# inscrit.


def _land_then(fake, monkeypatch, exc, marker: str = "/trust_transactions/") -> dict:
    """The next commit touching *marker* APPLIES, then its caller is answered
    *exc* — the real client and its real ``transactional`` loop see what
    production would: a commit whose answer was lost."""
    server = fake._fake_server
    real_commit = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if state["armed"] and any(marker in server._write_name(w) for w in writes):
            state["armed"] = False
            raise exc
        return response

    monkeypatch.setattr(server, "commit", _commit)
    return state


def test_un_paiement_rejoue_par_dessus_son_propre_commit_ne_retire_pas_deux_fois(
    fake, monkeypatch,
):
    """Régression — le commit du paiement de 300 $ ABOUTIT, sa réponse se
    perd, la reprise répond « Aborted » : l'ancien corps, rejoué, lisait les
    soldes déjà débités et le compteur déjà avancé, réécrivait la MÊME
    écriture sous la séquence suivante et appliquait le montant une SECONDE
    fois — 600 $ sortis des fonds compensés du client, 600 $ encaissés sur la
    facture, 600 $ au compte d'opérations, pour UN paiement de 300 $ — puis
    annonçait un succès. Le corps lit maintenant d'abord l'écriture que CET
    appel a créée : trouvée, l'issue est incertaine, et rien n'est réécrit."""
    from google.api_core import exceptions as gexc

    _land_then(fake, monkeypatch, gexc.Aborted("the retried commit of a landed transaction"))
    report: dict = {}
    result, errs = fee_payment.create_fee_payment(
        _entry(amount=30000), admin_account_id="ops1", _report_out=report)
    assert result is None
    assert errs == [fee_payment.CREATE_OUTCOME_UNCERTAIN]
    assert report == {"reason": "issue_incertaine", "side": "paiement"}
    (fee,) = _fees(fake)                                  # ONE withdrawal
    assert fee["sequence"] == 2                            # never re-sequenced
    assert fake.peek("counters/trust-acc1")["seq"] == 2
    assert fake.peek("trust_accounts/acc1")["book_balance"] == 170000
    dossier = fake.peek("dossiers/dos1")
    assert dossier["trust_cleared_by_client"]["c1"] == 170000
    assert dossier["trust_balance_by_client"]["c1"] == 170000
    assert fake.peek("invoices/inv1")["amount_paid"] == 30000
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 30000
    assert len(fake.peek_collection("admin_transactions")) == 1
    assert fake.peek("counters/admin-ops1")["seq"] == 1


def test_un_commit_qui_aboutit_puis_expire_ne_dit_jamais_rien_n_a_ete_inscrit(
    fake, monkeypatch,
):
    """Régression — le commit ABOUTIT puis le client reçoit
    « DeadlineExceeded » (jamais rejoué) : l'ancien message disait « Rien
    n'a été inscrit. Veuillez réessayer. » par-dessus un paiement inscrit,
    et l'avocat qui réessayait RETIRAIT UNE SECONDE FOIS les fonds du
    client."""
    from google.api_core import exceptions as gexc

    _land_then(fake, monkeypatch, gexc.DeadlineExceeded("answer lost"))
    report: dict = {}
    result, errs = fee_payment.create_fee_payment(
        _entry(amount=30000), admin_account_id="ops1", _report_out=report)
    assert len(_fees(fake)) == 1                           # it DID land
    assert result is None
    assert "Rien n'a été inscrit" not in errs[0]
    assert errs == [fee_payment.CREATE_OUTCOME_UNCERTAIN]
    assert report["reason"] == "issue_incertaine"


def test_une_lecture_ratee_avant_tout_commit_dit_toujours_rien_n_a_ete_inscrit(
    fake, monkeypatch,
):
    """La certitude reste dite quand elle existe : aucune tentative n'a
    préparé ses écritures (la PREMIÈRE lecture a échoué), aucun commit n'a
    été tenté — « rien n'a été inscrit » est vrai, et le reste.

    Réécrit délibérément (revue D23, concurrence et atomicité) : la panne
    TOTALE du magasin tombe désormais d'abord sur la lecture STRICTE du
    profil du cabinet, faite avant la transaction — qui le dit, dans ses
    propres mots, et dit aussi que rien n'a été inscrit. La panne qui ne
    frappe qu'à partir de la transaction garde le message générique
    qu'épinglait ce test."""
    from google.api_core import exceptions as gexc

    server = fake._fake_server
    real_get = server.batch_get_documents

    def _boom(request, metadata=None, **kwargs):
        raise gexc.ServiceUnavailable("store down")

    def _boom_after_profile(request, metadata=None, **kwargs):
        if all(d.endswith("/settings/cabinet") for d in request["documents"]):
            return real_get(request, metadata=metadata, **kwargs)
        raise gexc.ServiceUnavailable("store down")

    before = _snapshot(fake)
    monkeypatch.setattr(server, "batch_get_documents", _boom)
    _, errs = _pay()
    assert errs == [fee_payment._PROFILE_UNREADABLE]
    assert "rien n'a été inscrit" in errs[0]
    monkeypatch.setattr(server, "batch_get_documents", _boom_after_profile)
    _, errs = _pay()
    monkeypatch.setattr(server, "batch_get_documents", real_get)
    assert errs == ["Erreur lors de l'enregistrement du paiement d'honoraires. "
                    "Rien n'a été inscrit. Veuillez réessayer."]
    assert _snapshot(fake) == before


def test_une_contre_passation_rejouee_par_dessus_son_commit_n_est_pas_un_refus(
    fake, monkeypatch,
):
    """Régression — la contre-passation ABOUTIT, sa réponse se perd, le
    corps se rejoue et trouve le paiement… contre-passé, par elle-même :
    l'ancien code répondait le refus « Cette écriture a déjà été
    contre-passée », que la page affichait en 400 comme un échec. L'issue
    est incertaine — et rien n'est contre-passé deux fois."""
    from google.api_core import exceptions as gexc

    first, errs = _pay(amount=30000)
    assert errs == []
    _land_then(fake, monkeypatch, gexc.Aborted("the retried commit of a landed transaction"))
    report: dict = {}
    rev, errs = fee_payment.reverse_fee_payment(
        first["trust_entry"]["id"], "chèque perdu", _report_out=report)
    assert rev is None
    assert errs != [trust._ABORT_MESSAGES["déjà_contrepassée"]]
    assert errs == [fee_payment.REVERSE_OUTCOME_UNCERTAIN]
    assert report == {"reason": "issue_incertaine", "side": "paiement"}
    corrections = [t for t in fake.peek_collection("trust_transactions").values()
                   if t.get("purpose") == "correction"]
    assert len(corrections) == 1                           # reversed ONCE
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    assert fake.peek("admin_accounts/ops1")["ledger_balance"] == 0


def test_un_paiement_qui_commet_pendant_la_contre_passation_la_fait_rejouer(fake):
    """L'autre ordre de la course du § 5 : un paiement B commet PENDANT la
    tentative de contre-passation de A. La contre-passation lisait encore la
    facture sans B ; sans la facture dans son ensemble lu, elle écrirait
    « 600 − 600 = 0 » par-dessus les 400 $ de B — un paiement réel effacé.
    Elle est interrompue, rejouée sur la facture réelle, et n'ôte que A."""
    first, errs = _pay(date=_d(2026, 9, 10))
    assert errs == []
    raced: dict = {}

    def _race(info):
        if raced or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        raced["done"] = True
        raced["result"], raced["errs"] = _pay(amount=40000, date=_d(2026, 9, 20))

    remove = fake.add_commit_hook(_race)
    try:
        _rev, errs = fee_payment.reverse_fee_payment(first["trust_entry"]["id"], "chèque perdu")
    finally:
        remove()
    assert raced["errs"] == [], raced["errs"]
    assert errs == [], errs
    assert fake.peek("invoices/inv1")["amount_paid"] == 40000   # B stands, A is gone
    assert al.sum_invoice_receipts("inv1") == 40000


def test_un_encaissement_d_administration_et_un_paiement_d_honoraires_se_serialisent(fake):
    """Les DEUX écrivains d'un paiement sur la même facture — un
    encaissement saisi au registre d'administration (le formulaire web) et
    un paiement d'honoraires (demain, le connecteur) — se sérialisent sur
    la facture lue dans chacune des deux transactions. L'encaissement de
    600 $ commet pendant la tentative du paiement de 600 $ : le paiement,
    rejoué sur le solde réel (400 $), est refusé en entier — rien au
    fidéicommis, rien au compte d'opérations de sa part, la facture à 600 $."""
    rival: dict = {}

    def _race(info):
        if rival or not any(p.startswith("trust_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["entry"], rival["errs"] = al.create_transaction({
            "account_id": "ops1", "kind": "encaissement_facture",
            "invoice_id": "inv1", "amount": 60000, "method": "virement",
            "counterparty": "Jean Tremblay", "date": _d(2026, 9, 12),
        })

    remove = fake.add_commit_hook(_race)
    try:
        result, errs = _pay()
    finally:
        remove()
    assert rival["errs"] == [], rival["errs"]
    assert result is None
    assert errs == [trust._ABORT_MESSAGES["virement_excède_facture"]]
    assert _fees(fake) == []
    assert fake.peek("invoices/inv1")["amount_paid"] == 60000
    assert len(fake.peek_collection("admin_transactions")) == 1


# ══════════════════════════════════════════════════════════════════════
# 7. Les décisions du 2026-09-29 — D20, D21, D23
# ══════════════════════════════════════════════════════════════════════


def _seed_profile(fake, nom: str = "Me Jason Poirier Lavoie",
                  organisation: str = "Poirier Lavoie, avocat") -> None:
    """The firm profile as « Paramètres » stores it (``settings/cabinet``):
    once the document exists it is the WHOLE truth, the seed ignored."""
    fake.seed("settings/cabinet", {"nom": nom, "organisation": organisation})


def test_d20_le_refus_d_une_facture_non_envoyee_ne_dit_pas_comment_passer_outre(fake):
    """D20 : le contrôle fait toujours confiance au statut « envoyée » — seul
    le texte change. Il nomme le juriste comme celui qui atteste l'envoi et
    ne prescrit AUCUN geste qui franchirait le contrôle (« promouvez-la
    d'abord », update_invoice, « marquez-la envoyée »). Même texte pour un
    brouillon, une facture payée ou annulée : aucune n'ouvre de retrait."""
    text = trust._ABORT_MESSAGES["facture_non_émise"]
    assert "envoyée par le juriste" in text and "art. 56 2°" in text
    for forbidden in ("promouv", "update_invoice", "marquez", "passez-la"):
        assert forbidden not in text.lower(), forbidden
    for status in ("brouillon", "payée", "annulée"):
        doc = fake.peek("invoices/inv1")
        doc.update(status=status)
        fake.external_write("invoices/inv1", doc)
        report: dict = {}
        _, errs = fee_payment.create_fee_payment(
            _entry(), admin_account_id="ops1", _report_out=report)
        assert errs == [text], status
        assert report == {"reason": "facture_non_émise", "side": "fidéicommis"}


def _fund_c2(amount: int = 200000) -> None:
    receipt, errs = trust.create_transaction(_entry(
        direction="recette", purpose="dépôt_client", amount=amount,
        counterparty="Marie Roy", client_id="c2", invoice_id=None,
        date=_d(2026, 9, 3)))
    assert errs == [], errs
    _, errs = trust.clear_transaction(receipt["id"], _d(2026, 9, 3))
    assert errs == [], errs


def test_d21_les_fonds_d_un_client_n_acquittent_pas_la_facture_d_un_autre(fake):
    """Régression — D21 : la facture inv1 est adressée à c1 ; tirer les fonds
    COMPENSÉS de c2 (un autre client du même dossier) passait toutes les
    gardes et n'était signalé qu'APRÈS le commit, par un avertissement, les
    honoraires déjà sortis. Refusé désormais dans la transaction, sans
    dérogation : rien d'écrit, nulle part. (Sur l'ancien code : l'écriture
    passait, le paiement portait sur inv1.)"""
    _fund_c2()
    before = _snapshot(fake)
    report: dict = {}
    result, errs = fee_payment.create_fee_payment(
        _entry(client_id="c2"), admin_account_id="ops1", _report_out=report)
    assert result is None
    assert errs == [trust._ABORT_MESSAGES["facture_autre_client"]]
    assert report == {"reason": "facture_autre_client", "side": "fidéicommis"}
    # Neither a name nor an amount in the refusal.
    for word in ("Jean", "Tremblay", "Marie", "Roy", "600", "$"):
        assert word not in errs[0], word
    assert _snapshot(fake) == before


def test_d21_suit_le_client_de_la_facture_telle_qu_elle_est_stockee(fake):
    """La règle se juge sur la facture que la transaction du paiement LIT :
    adressée à c2, elle s'acquitte des fonds de c2 — et plus de ceux de c1,
    qui la payait l'instant d'avant."""
    _fund_c2()
    doc = fake.peek("invoices/inv1")
    doc.update(client_id="c2")
    fake.external_write("invoices/inv1", doc)
    result, errs = fee_payment.create_fee_payment(
        _entry(client_id="c2"), admin_account_id="ops1")
    assert errs == [], errs
    assert result["trust_entry"]["client_id"] == "c2"
    _, errs = fee_payment.create_fee_payment(
        _entry(client_id="c1", amount=10000), admin_account_id="ops1")
    assert errs == [trust._ABORT_MESSAGES["facture_autre_client"]]


def test_d21_une_facture_sans_client_ne_nomme_aucun_autre_client(fake):
    """Une facture qui ne nomme aucun client (une reprise qui n'en portait
    pas) n'est pas refusée sur ce motif : elle n'est adressée à aucun AUTRE
    client."""
    doc = fake.peek("invoices/inv1")
    doc.update(client_id="")
    fake.external_write("invoices/inv1", doc)
    _, errs = _pay()
    assert errs == [], errs


def test_d23_le_beneficiaire_est_l_avocat_ou_son_cabinet(fake):
    """Régression — D23 (art. 58, « chèque tiré à l'ordre de l'avocat ») :
    le bénéficiaire d'un paiement d'honoraires était un texte libre ; « Jean
    Tremblay » passait. Désormais l'avocat ou son cabinet, tels que les nomme
    le profil du cabinet — et inscrit comme le profil l'écrit, jamais comme
    il a été tapé."""
    _seed_profile(fake)
    assert fee_payment.fee_payees() == ["Poirier Lavoie, avocat",
                                        "Me Jason Poirier Lavoie"]
    result, errs = _pay(amount=10000, counterparty="  me JASON   poirier lavoie ")
    assert errs == [], errs
    stored = fake.peek(f"trust_transactions/{result['trust_entry']['id']}")
    assert stored["counterparty"] == "Me Jason Poirier Lavoie"
    result, errs = _pay(amount=10000, counterparty="Poirier Lavoie, avocat")
    assert errs == [], errs

    before = _snapshot(fake)
    report: dict = {}
    _, errs = fee_payment.create_fee_payment(
        _entry(amount=10000, counterparty="Jean Tremblay"),
        admin_account_id="ops1", _report_out=report)
    assert errs == [fee_payment._message(
        "bénéficiaire_honoraires_invalide",
        "« Poirier Lavoie, avocat » ou « Me Jason Poirier Lavoie »")]
    assert "art. 58" in errs[0]
    assert report == {"reason": "bénéficiaire_honoraires_invalide", "side": "paiement"}
    assert _snapshot(fake) == before


def test_d23_un_accent_distingue_deux_noms(fake):
    """Le pliage ne porte que sur la casse, la composition Unicode (NFC) et
    les espaces — jamais sur un accent : « Cote » n'est pas « Côté »."""
    import unicodedata

    _seed_profile(fake, nom="Me Hélène Côté", organisation="Côté avocats")
    decomposed = unicodedata.normalize("NFD", "Me Hélène Côté")
    assert decomposed != "Me Hélène Côté"
    assert fee_payment.match_fee_payee(decomposed) == "Me Hélène Côté"
    assert fee_payment.match_fee_payee("CÔTÉ AVOCATS") == "Côté avocats"
    assert fee_payment.match_fee_payee("Me Helene Cote") is None
    _, errs = _pay(amount=10000, counterparty="Cote avocats")
    assert errs and "art. 58" in errs[0]


def test_d23_un_profil_sans_nom_refuse_tout_paiement(fake):
    _seed_profile(fake, nom="", organisation="")
    before = _snapshot(fake)
    report: dict = {}
    _, errs = fee_payment.create_fee_payment(
        _entry(), admin_account_id="ops1", _report_out=report)
    assert errs == [fee_payment._MESSAGES["bénéficiaires_honoraires_inconnus"]]
    assert report == {"reason": "bénéficiaires_honoraires_inconnus", "side": "paiement"}
    assert _snapshot(fake) == before


def _profile_unreadable(fake, monkeypatch) -> None:
    """``settings/cabinet`` unreadable — a server-side read failure of that
    one document, every other read untouched (the fake's own RPC)."""
    server = fake._fake_server
    real = server.batch_get_documents

    def failing(request, metadata=None, **kw):
        if any(d.endswith("/settings/cabinet") for d in request["documents"]):
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(server, "batch_get_documents", failing)


def test_d23_un_profil_illisible_refuse_au_lieu_de_retomber_sur_la_semence(
    fake, monkeypatch
):
    """Régression — revue D23 (concurrence et atomicité) : la garde lisait
    le profil par ``cabinet_dict()``, qui RETOMBE SUR LA SEMENCE de
    déploiement quand Firestore ne répond pas — le littéral
    ``ORGANISATION_SEED`` et ``FIRM_NAME``. Or le profil enregistré est
    toute la vérité : l'avocat qui a vidé « organisation » (son cabinet
    n'est pas une société distincte) voyait, sur une lecture manquée, le nom
    semé redevenir un bénéficiaire accepté et s'inscrire au registre. La
    garde refuse désormais — rien d'écrit, nulle part —, et ne décide pas
    davantage sur le nom de l'avocat : elle n'a rien lu. (Sur l'ancien
    code : le paiement passait, à l'ordre de « Poirier Lavoie, avocat ».)"""
    _seed_profile(fake, organisation="")
    assert fee_payment.guard_fee_payees() == ["Me Jason Poirier Lavoie"]
    assert fee_payment.match_fee_payee(FEE_PAYEE) is None  # the stored truth
    _profile_unreadable(fake, monkeypatch)
    before = _snapshot(fake)
    for payee in (FEE_PAYEE, "Me Jason Poirier Lavoie", ""):
        report: dict = {}
        result, errs = fee_payment.create_fee_payment(
            _entry(amount=10000, counterparty=payee), admin_account_id="ops1",
            _report_out=report)
        assert result is None, payee
        assert errs == [fee_payment._PROFILE_UNREADABLE], payee
        assert report == {"reason": "erreur", "side": "paiement"}, payee
    assert "rien n'a été inscrit" in fee_payment._PROFILE_UNREADABLE
    assert _snapshot(fake) == before
    # The DISPLAY list stays fail-open, as every render of the profile: the
    # form still shows a select, and the model is what decides.
    assert FEE_PAYEE in fee_payment.fee_payees()


def test_d23_la_garde_lit_le_profil_une_seule_fois(fake):
    """Le bénéficiaire se juge sur UNE lecture du profil : le refus d'un
    bénéficiaire vide et la garde partagent la liste lue une fois, pour
    qu'aucune décision ne repose sur deux lectures qui auraient pu différer."""
    _seed_profile(fake)
    fake.reset_logs()
    _, errs = _pay(amount=10000)
    assert errs == [], errs
    profile_reads = [r for r in fake.reads if "settings/cabinet" in r.paths]
    assert len(profile_reads) == 1, profile_reads


def test_d23_un_nom_donne_deux_fois_ne_compte_qu_une_fois(fake):
    _seed_profile(fake, nom="Me Jason Poirier Lavoie",
                  organisation="  me jason poirier lavoie")
    assert fee_payment.fee_payees() == ["me jason poirier lavoie"]


def test_d23_sans_cabinet_nomme_l_avocat_reste_le_seul_beneficiaire(fake):
    """Le cabinet d'abord — mais un profil qui ne nomme que l'avocat
    l'offre seul, et c'est lui qu'on inscrit."""
    _seed_profile(fake, organisation="")
    assert fee_payment.fee_payees() == ["Me Jason Poirier Lavoie"]
    _, errs = _pay(amount=10000, counterparty=FEE_PAYEE)
    assert errs and "art. 58" in errs[0]
    result, errs = _pay(amount=10000, counterparty="Me Jason Poirier Lavoie")
    assert errs == [], errs


def test_d23_le_beneficiaire_par_defaut_suit_le_mode(fake):
    """Régression — l'art. 58 tire un CHÈQUE d'honoraires « à l'ordre de
    l'avocat » et ne nomme la société qu'en titulaire du compte d'un
    VIREMENT. Le bénéficiaire par défaut était le cabinet quel que fût le
    mode : un chèque inscrit, sans que personne l'ait choisi, à l'ordre de la
    société. Le défaut suit désormais le mode — l'avocat sur un chèque, le
    cabinet sur un virement —, et le seul nom du profil sert aux deux quand
    il n'en porte qu'un. Un défaut SEULEMENT : le cabinet nommé reste
    accepté sur un chèque (D23)."""
    _seed_profile(fake)
    assert fee_payment.default_fee_payee("chèque") == "Me Jason Poirier Lavoie"
    assert fee_payment.default_fee_payee("virement") == "Poirier Lavoie, avocat"
    result, errs = _pay(amount=10000, method="chèque",
                        counterparty="Poirier Lavoie, avocat")
    assert errs == [], errs

    _seed_profile(fake, organisation="")
    assert fee_payment.default_fee_payee("chèque") == "Me Jason Poirier Lavoie"
    assert fee_payment.default_fee_payee("virement") == "Me Jason Poirier Lavoie"
    _seed_profile(fake, nom="")
    assert fee_payment.default_fee_payee("chèque") == "Poirier Lavoie, avocat"
    _seed_profile(fake, nom="", organisation="")
    assert fee_payment.default_fee_payee("chèque") is None


def test_le_beneficiaire_des_tests_est_la_semence_du_profil(fake):
    """``FEE_PAYEE`` (tests/_accounting_history.py) est le nom que la suite
    donne à un paiement d'honoraires : l'organisation de la semence, seul
    nom accepté quand aucun profil n'est enregistré et qu'aucun FIRM_NAME
    n'est posé."""
    assert FEE_PAYEE == settings_model.ORGANISATION_SEED
    assert FEE_PAYEE in fee_payment.fee_payees()
