"""Le registre d'administration écrit le paiement de la facture dans SA
transaction, et son formulaire de modification refuse une version dépassée
(lot 5a, étape 2 ; décisions D2 et D9).

1. **Un encaissement et son paiement sont UNE écriture.** Jusqu'ici la
   route projetait le paiement APRÈS le commit de l'écriture
   (``services/encaissements.projeter_paiement``, supprimé), par une lecture
   puis une écriture absolue hors transaction : deux encaissements
   parallèles passaient tous deux le contrôle du solde vivant — le montant
   encaissé ne bougeait qu'après —, et un échec de projection laissait
   l'écriture debout sous une bannière. Le modèle lit maintenant la facture
   dans la transaction de l'écriture et y inscrit
   ``invoice.payment_updates`` dans le même commit ; la contre-passation
   réduit le paiement de la même façon.
2. **Les appelants ne projettent plus rien** — la route d'administration,
   la recette automatique du fidéicommis et le script de reprise : une
   seconde projection compterait chaque paiement deux fois.
3. **La modification compare l'etag EN PREMIER**, avant le verrou et avant
   toute validation ; un montant de déboursé changé sans sa ventilation est
   refusé en nommant les champs ; le formulaire web porte l'etag de la
   version qu'il affiche.

Chaque test marqué « régression » échoue sur le code antérieur. Le banc est
le faux Firestore partagé (``tests/_fake_firestore.py``) : le client, ses
transactions et la boucle de reprise de ``transactional`` sont les vrais ;
on relit ce qui est STOCKÉ, jamais un dictionnaire remis à un faux.
"""

import ast
import html
import os
import pathlib
import re
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
    from models import concurrency
    from models import invoice as invoice_model
    from models import provenance
    from models import trust
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._accounting_history import FEE_PAYEE, legacy_fee_entry  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc
RIVAL_ETAG = "11111111-2222-4333-8444-555555555555"
BANNER = "Cet élément a été modifié entre-temps."
_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, as the sibling
    benches do, so a model a route starts to read cannot reach the mocked
    client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def clock(monkeypatch):
    """Freeze the ONE Montréal clock read (``utils.deadlines.today_mtl``) —
    a reversal defaults to today, and a date derived from the clock is the
    house landmine."""
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
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    f.seed("invoices/fac1", {
        "id": "fac1", "invoice_number": "2026-F031", "status": "envoyée",
        "total": 100000, "retainer_applied": 0,
        "amount_due": 100000, "amount_paid": 0, "paid_date": None,
        "dossier_id": "dos1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. X", "etag": "inv-e0",
    })
    return f


@pytest.fixture
def client(fake):
    app = Flask(
        __name__,
        template_folder=str(_ATHENA / "templates"),
        static_folder=str(_ATHENA / "static"),
    )
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v,
    )
    for bp in (trust_routes.trust_bp, invoices_routes.invoices_bp,
               admin_ledger_routes.admin_bp, dossiers_routes.dossiers_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _enc(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "encaissement_facture",
        "invoice_id": "fac1", "amount": 60000, "method": "virement",
        "counterparty": "Jean Tremblay", "date": _d(2026, 9, 10),
        "description": "", "reference": "",
    }
    d.update(over)
    return d


def _depense(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "dépense", "category": "loyer",
        "amount": 11498, "method": "virement",
        "counterparty": "Immeubles X", "date": _d(2026, 9, 10),
        "net_amount": 10000, "gst_amount": 500, "qst_amount": 998,
        "description": "Premier", "reference": "", "supplier_invoice_ref": "",
    }
    d.update(over)
    return d


def _invoice(fake) -> dict:
    return fake.peek("invoices/fac1")


def _entries(fake) -> dict:
    return fake.peek_collection("admin_transactions")


def _ledger(fake) -> int:
    return fake.peek("admin_accounts/ops1")["ledger_balance"]


def _commit_touching(fake, rel: str) -> list:
    return [c for c in fake.commits if any(path == rel for _op, path in c.ops)]


def _run_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_admin_integrity as vai

    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    return code, capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# 1. L'encaissement et son paiement : un seul commit
# ══════════════════════════════════════════════════════════════════════


def test_l_encaissement_porte_son_paiement_dans_le_meme_commit(fake):
    """Régression — l'ancien modèle n'écrivait jamais la facture : c'était
    la route qui la projetait, APRÈS, dans un second commit."""
    fake.reset_logs()
    entry, errs = al.create_transaction(_enc())
    assert errs == [], errs
    invoice = _invoice(fake)
    assert invoice["amount_paid"] == 60000
    assert invoice["paid_date"] == _d(2026, 9, 10)     # the entry's own date
    assert invoice["etag"] != "inv-e0"                 # a real write, stamped
    assert invoice["updated_via"] == "script"          # no request here
    commits = _commit_touching(fake, "invoices/fac1")
    assert len(commits) == 1
    assert ("set", f"admin_transactions/{entry['id']}") in commits[0].ops


def test_le_paiement_s_ajoute_a_celui_qui_est_deja_inscrit(fake):
    """Courant + delta, jamais un SET aveugle : un paiement inscrit plus tôt
    (ou une correction côté facture) survit à l'encaissement suivant — la
    propriété que la projection supprimée portait, désormais dans le
    modèle. Le solde atteignant zéro bascule la facture à « payée »."""
    doc = _invoice(fake)
    doc.update(amount_paid=40000, paid_date=_d(2026, 9, 2))
    fake.external_write("invoices/fac1", doc)
    entry, errs = al.create_transaction(_enc(amount=60000))
    assert errs == [], errs
    invoice = _invoice(fake)
    assert invoice["amount_paid"] == 100000
    assert invoice["status"] == "payée"


def test_un_paiement_que_la_facture_refuse_refuse_l_ecriture(fake, monkeypatch):
    """Régression — l'ancienne route inscrivait l'écriture, puis la
    projection échouait et l'écriture restait debout sous une bannière : le
    registre comptait un encaissement que la facture ne montrait pas.
    Désormais le refus de la facture annule TOUT."""
    def _refuse(*_a, **_k):
        raise invoice_model.PaymentRefused("La facture refuse ce paiement.")

    monkeypatch.setattr(invoice_model, "payment_updates", _refuse)
    before = _invoice(fake)
    entry, errs = al.create_transaction(_enc())
    assert entry is None
    assert errs and "La facture refuse ce paiement." in errs[0]
    assert "Rien n'a été inscrit" in errs[0]
    assert _entries(fake) == {}
    assert fake.peek("counters/admin-ops1") is None
    assert _ledger(fake) == 0
    assert _invoice(fake) == before


def test_deux_encaissements_paralleles_se_serialisent(fake):
    """Régression — le cas que la décision D2 exigeait de rendre sûr avant
    tout outil : deux encaissements simultanés de 600 $ sur une facture de
    1 000 $. L'ancien code les inscrivait TOUS LES DEUX (le montant encaissé
    ne bougeait qu'après, par la route) : 1 200 $ au registre pour une
    facture de 1 000 $. La facture est maintenant lue dans la transaction :
    le second commit est interrompu, rejoué sur le solde réel, et refusé."""
    rival: dict = {}

    def _race(info):
        # Fires at the FIRST commit attempt of the outer call, once: another
        # process records its own 600 $ encaissement just before it lands.
        if rival or not any(p.startswith("admin_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["entry"], rival["errs"] = al.create_transaction(_enc())

    remove = fake.add_commit_hook(_race)
    try:
        entry, errs = al.create_transaction(_enc())
    finally:
        remove()

    assert rival["errs"] == [] and rival["entry"]
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["encaissement_excède_solde"]]
    assert list(_entries(fake)) == [rival["entry"]["id"]]
    assert _invoice(fake)["amount_paid"] == 60000
    assert _ledger(fake) == 60000
    assert fake.peek("counters/admin-ops1")["seq"] == 1


# ══════════════════════════════════════════════════════════════════════
# 2. La contre-passation réduit le paiement dans le même commit
# ══════════════════════════════════════════════════════════════════════


def test_la_contre_passation_reduit_le_paiement_dans_le_meme_commit(fake):
    """Régression — l'ancien modèle ne touchait pas la facture : la route
    réduisait après coup (``reduire_paiement``) et un échec laissait le
    paiement debout, signalé par une bannière seulement."""
    entry, _ = al.create_transaction(_enc(amount=100000))
    assert _invoice(fake)["status"] == "payée"
    fake.reset_logs()
    reversal, errs = al.reverse_transaction(entry["id"], "chèque sans provision")
    assert errs == [], errs
    invoice = _invoice(fake)
    assert invoice["amount_paid"] == 0
    assert invoice["paid_date"] is None
    assert invoice["status"] == "envoyée"          # the narrow undo, atomic
    commits = _commit_touching(fake, "invoices/fac1")
    assert len(commits) == 1
    assert ("set", f"admin_transactions/{reversal['id']}") in commits[0].ops


def test_une_reduction_partielle_garde_la_date_du_paiement(fake):
    """Le ``paid_date`` EXISTANT traverse une réduction partielle — elle ne
    doit jamais estampiller aujourd'hui (la règle de l'ancienne réduction,
    portée par le modèle)."""
    doc = _invoice(fake)
    doc.update(amount_paid=40000, paid_date=_d(2026, 9, 2))
    fake.external_write("invoices/fac1", doc)
    entry, _ = al.create_transaction(_enc(amount=30000, date=_d(2026, 9, 10)))
    reversal, errs = al.reverse_transaction(entry["id"], "erreur de saisie")
    assert errs == [], errs
    invoice = _invoice(fake)
    assert invoice["amount_paid"] == 40000
    assert invoice["paid_date"] == _d(2026, 9, 10)  # kept, never « today »


def test_un_paiement_inferieur_a_la_contre_passation_refuse_sans_rien_ecrire(fake):
    """Jamais d'écrêtage : si la facture porte moins que ce que la
    contre-passation reprend (une correction hors registre entre-temps), un
    ``max(0, …)`` effacerait en silence d'autres paiements. Refus, et rien
    n'est écrit — ni la contre-passation, ni la facture, ni le solde."""
    entry, _ = al.create_transaction(_enc(amount=60000))
    doc = _invoice(fake)
    doc.update(amount_paid=30000)
    fake.external_write("invoices/fac1", doc)
    before_entries, before_invoice = _entries(fake), _invoice(fake)
    ledger = _ledger(fake)

    reversal, errs = al.reverse_transaction(entry["id"], "chèque sans provision")

    assert reversal is None
    assert errs == [al._ABORT_MESSAGES["paiement_facture_incohérent"]]
    assert _entries(fake) == before_entries
    assert _invoice(fake) == before_invoice
    assert _ledger(fake) == ledger


def test_une_facture_disparue_refuse_la_contre_passation(fake):
    entry, _ = al.create_transaction(_enc())
    fake.external_delete("invoices/fac1")
    before = _entries(fake)
    reversal, errs = al.reverse_transaction(entry["id"], "erreur")
    assert reversal is None
    assert errs == [al._ABORT_MESSAGES["facture_paiement_introuvable"]]
    assert _entries(fake) == before


def test_une_contre_passation_et_un_encaissement_paralleles_se_serialisent(fake):
    """Revue du lot 5a (concurrence) — régression : l'ancienne réduction
    lisait la facture HORS transaction, puis y écrivait un montant ABSOLU
    (``record_payment``) : un encaissement commis entre les deux était
    effacé. La contre-passation lit maintenant la facture dans sa
    transaction : l'encaissement rival interrompt son commit, et le rejeu
    réduit le montant RÉEL — rien n'est perdu, rien n'est compté deux fois."""
    first, errs = al.create_transaction(_enc(amount=30000))
    assert errs == []
    rival: dict = {}

    def _race(info):
        # The reversal's FIRST commit attempt: another process records a
        # 200 $ encaissement on the same invoice just before it lands.
        if rival or not any(p.startswith("admin_transactions/") for _o, p in info.ops):
            return
        rival["done"] = True
        rival["entry"], rival["errs"] = al.create_transaction(
            _enc(amount=20000, date=_d(2026, 9, 12)))

    remove = fake.add_commit_hook(_race)
    try:
        reversal, errs = al.reverse_transaction(first["id"], "chèque sans provision")
    finally:
        remove()

    assert rival["errs"] == [] and rival["entry"]
    assert errs == [], errs
    invoice = _invoice(fake)
    assert invoice["amount_paid"] == 20000          # 30 000 + 20 000 − 30 000
    assert invoice["status"] == "envoyée"
    assert al.sum_invoice_receipts("fac1") == 20000  # the register agrees


def test_une_depense_se_contre_passe_sans_toucher_aucune_facture(fake):
    entry, _ = al.create_transaction(_depense())
    fake.reset_logs()
    reversal, errs = al.reverse_transaction(entry["id"], "doublon")
    assert errs == [], errs
    assert _commit_touching(fake, "invoices/fac1") == []


def test_le_registre_et_les_factures_concordent_apres_les_ecritures(
    fake, monkeypatch, capsys
):
    """Régression — le contrôle nº 8 de verify_admin_integrity (Σ des
    encaissements debout == montant encaissé de chaque facture) sur un
    registre écrit par le MODÈLE seul : l'ancien modèle laissait la facture
    à 0 $, et le contrôle signalait l'écart que la route devait combler."""
    first, errs = al.create_transaction(_enc(amount=30000))
    assert errs == []
    second, errs = al.create_transaction(_enc(amount=50000, date=_d(2026, 9, 12)))
    assert errs == []
    al.clear_transaction(first["id"], _d(2026, 9, 11))
    _, errs = al.reverse_transaction(first["id"], "chèque sans provision")
    assert errs == []
    _, errs = al.reverse_transaction(second["id"], "saisie en double")
    assert errs == []
    third, errs = al.create_transaction(_enc(amount=20000, date=_d(2026, 9, 14)))
    assert errs == []
    assert _invoice(fake)["amount_paid"] == 20000
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out


def test_une_facture_supprimee_apres_contre_passation_n_est_pas_orpheline(
    fake, monkeypatch, capsys
):
    """Revue du lot 5a — régression du CONTRÔLE, pas du modèle : le chemin
    normal de l'application (contre-passer l'encaissement, annuler la
    facture, la supprimer — l'annulation refuse tant qu'un encaissement
    tient) laissait des lignes contre-passées citant une facture disparue,
    et le contrôle nº 8 en faisait un « encaissements orphelins » : un écart
    que les modèles n'avaient jamais produit, sur le script que l'étape
    exige propre avant son déploiement. Un encaissement DEBOUT sur une
    facture disparue reste, lui, signalé."""
    entry, errs = al.create_transaction(_enc(amount=60000))
    assert errs == []
    _, errs = al.reverse_transaction(entry["id"], "chèque sans provision")
    assert errs == []
    assert invoice_model.void_invoice("fac1") == (True, "")
    assert invoice_model.delete_invoice("fac1") == (True, "")
    assert fake.peek("invoices/fac1") is None
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out

    # The real orphan — a STANDING encaissement whose invoice vanished out of
    # band (the app cannot produce it: the void refuses first).
    fake.seed("invoices/fac2", {
        "id": "fac2", "invoice_number": "2026-F032", "status": "envoyée",
        "total": 50000, "retainer_applied": 0,
        "amount_due": 50000, "amount_paid": 0, "paid_date": None,
        "dossier_id": "dos1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. X", "etag": "inv2-e0",
    })
    _, errs = al.create_transaction(_enc(invoice_id="fac2", amount=10000))
    assert errs == []
    fake.external_delete("invoices/fac2")
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert "facture fac2: introuvable (encaissements orphelins)" in out
    assert "fac1" not in out


# ══════════════════════════════════════════════════════════════════════
# 3. La modification : l'etag d'abord, la ventilation nommée
# ══════════════════════════════════════════════════════════════════════


def test_l_etag_est_verifie_avant_le_verrou(fake):
    """Régression — l'ancien update_transaction n'avait pas d'etag
    (TypeError). Claude compense l'écriture pendant que le formulaire est
    ouvert : la réponse utile est « elle a changé depuis », jamais le
    verrou que cette écriture inconnue a posé."""
    entry, _ = al.create_transaction(_depense())
    stored = _entries(fake)[entry["id"]]
    _, errs = al.clear_transaction(entry["id"], _d(2026, 9, 11))
    assert errs == []
    before = _entries(fake)[entry["id"]]
    updated, errs = al.update_transaction(
        entry["id"], {"description": "Loyer"}, expected_etag=stored["etag"])
    assert updated is None
    assert errs == [concurrency.STALE_ETAG_ERROR]
    assert _entries(fake)[entry["id"]] == before


def test_l_etag_est_verifie_avant_la_validation(fake):
    entry, _ = al.create_transaction(_depense())
    shown = _entries(fake)[entry["id"]]["etag"]
    doc = _entries(fake)[entry["id"]]
    doc.update(description="Écrit par Claude", etag=RIVAL_ETAG)
    fake.external_write(f"admin_transactions/{entry['id']}", doc)
    updated, errs = al.update_transaction(
        entry["id"], {"amount": 0}, expected_etag=shown)   # invalid AND stale
    assert updated is None
    assert errs == [concurrency.STALE_ETAG_ERROR]
    assert _entries(fake)[entry["id"]]["description"] == "Écrit par Claude"


def test_un_etag_vide_n_est_pas_une_version(fake):
    entry, _ = al.create_transaction(_depense())
    updated, errs = al.update_transaction(
        entry["id"], {"description": "Loyer"}, expected_etag="")
    assert updated is None and errs == [concurrency.STALE_ETAG_ERROR]


def test_le_bon_etag_ecrit_et_en_produit_un_nouveau(fake):
    entry, _ = al.create_transaction(_depense())
    shown = _entries(fake)[entry["id"]]["etag"]
    updated, errs = al.update_transaction(
        entry["id"], {"description": "Loyer d'octobre"}, expected_etag=shown)
    assert errs == [], errs
    stored = _entries(fake)[entry["id"]]
    assert stored["description"] == "Loyer d'octobre"
    assert stored["etag"] != shown and updated["etag"] == stored["etag"]
    assert stored["revisions"][-1]["via"] == "script"


def test_sans_etag_le_chemin_historique_reste_ouvert(fake):
    entry, _ = al.create_transaction(_depense())
    doc = _entries(fake)[entry["id"]]
    doc.update(etag=RIVAL_ETAG)
    fake.external_write(f"admin_transactions/{entry['id']}", doc)
    updated, errs = al.update_transaction(entry["id"], {"description": "Loyer"})
    assert errs == [], errs


def test_une_modification_rejouee_apres_un_essai_sans_effet_reste_notee(fake):
    """Revue du lot 5a (concurrence) — régression : le décorateur rejoue le
    corps de la transaction après un commit interrompu, et un commit VIDE
    s'interrompt aussi (un essai sans effet dont la lecture a changé). Le
    « rien à écrire » du premier essai survivait au second, qui écrivait
    pour de vrai : aucun note_commit — le protocole d'écriture lirait « rien
    n'a été écrit » et inviterait un doublon au rejeu — et aucune ligne de
    journal."""
    entry, _ = al.create_transaction(_depense(description="Loyer"))
    rel = f"admin_transactions/{entry['id']}"
    state: dict = {}

    def _race(info):
        # The no-op attempt commits NOTHING — that empty commit is the one
        # a concurrent writer interrupts.
        if state or info.ops:
            return
        state["done"] = True
        doc = fake.peek(rel)
        doc.update(description="Écrit ailleurs", etag=RIVAL_ETAG)
        fake.external_write(rel, doc)

    remove = fake.add_commit_hook(_race)
    try:
        with provenance.writing_via("mcp", tool="update_admin_entry"):
            updated, errs = al.update_transaction(entry["id"], {"description": "Loyer"})
            writes = provenance.committed_writes()
    finally:
        remove()

    assert state, "the no-op attempt must have run and been interrupted"
    assert errs == [], errs
    assert fake.peek(rel)["description"] == "Loyer"        # the retry wrote
    assert ("admin_transactions", entry["id"]) in writes     # …and said so


def test_un_montant_de_debourse_change_sans_ventilation_est_refuse_nommement(fake):
    """Régression (message) — l'ancien refus disait « la ventilation doit
    égaler le montant » : une somme que l'appelant n'avait jamais envoyée.
    Le refus nomme maintenant les trois champs, et rien n'est écrit."""
    entry, _ = al.create_transaction(_depense())
    before = _entries(fake)[entry["id"]]
    updated, errs = al.update_transaction(entry["id"], {"amount": 22996})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["ventilation_requise"]]
    for field in ("net_amount", "gst_amount", "qst_amount"):
        assert field in errs[0]
    assert _entries(fake)[entry["id"]] == before


def test_un_montant_change_avec_sa_ventilation_passe(fake):
    entry, _ = al.create_transaction(_depense())
    updated, errs = al.update_transaction(entry["id"], {
        "amount": 22996, "net_amount": 20000, "gst_amount": 1000,
        "qst_amount": 1996})
    assert errs == [], errs
    assert _ledger(fake) == -22996
    # Zeros mean « no tax claimed »: net = amount.
    updated, errs = al.update_transaction(entry["id"], {
        "amount": 5000, "net_amount": 0, "gst_amount": 0, "qst_amount": 0})
    assert errs == [], errs
    assert _entries(fake)[entry["id"]]["net_amount"] == 5000


def test_une_recette_change_de_montant_sans_ventilation(fake):
    """Une recette ne porte pas de ventilation : la règle ne la vise pas."""
    entry, _ = al.create_transaction(_depense(
        kind="recette_autre", category="", net_amount=None, gst_amount=None,
        qst_amount=None, counterparty="Intérêts BNC"))
    updated, errs = al.update_transaction(entry["id"], {"amount": 1234})
    assert errs == [], errs
    assert _ledger(fake) == 1234


# ══════════════════════════════════════════════════════════════════════
# 4. Le formulaire web de modification (D9)
# ══════════════════════════════════════════════════════════════════════


def _etags(html: str) -> list[str]:
    return _ETAG_INPUT.findall(html)


def _form(**over) -> dict:
    f = {
        "kind": "dépense", "amount": "114,98", "method": "virement",
        "counterparty": "Immeubles X", "category": "loyer",
        "net_amount": "100,00", "gst_amount": "5,00", "qst_amount": "9,98",
        "date": "2026-09-10", "description": "SOUMIS-7Q4", "reference": "",
        "supplier_invoice_ref": "",
    }
    f.update(over)
    return f


def test_deja_compensee_au_formulaire_est_une_seule_creation(client, fake):
    """Régression (lot 5a, étape 3) — la route composait création PUIS
    compensation, deux commits : la seconde pouvait échouer et laisser
    l'écriture « en circulation » sous une bannière, alors que l'avocat
    avait affirmé qu'elle figurait au relevé. C'est maintenant UNE création,
    née compensée à sa date, par le service commun."""
    fake.reset_logs()
    resp = client.post("/administration/", data={
        **_form(), "account_id": "ops1", "deja_compensee": "1"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    (entry,) = _entries(fake).values()
    assert entry["status"] == "compensée"
    assert entry["cleared_date"] == _d(2026, 9, 10)
    assert len(_commit_touching(fake, f"admin_transactions/{entry['id']}")) == 1


def test_une_compensation_refusee_a_la_creation_refuse_la_creation(client, fake):
    """Une date de l'écriture dans le futur de Montréal : la création
    entière est refusée, rien n'est écrit (plus d'écriture debout « en
    circulation » avec sa compensation manquée)."""
    resp = client.post("/administration/", data={
        **_form(date="2026-09-21"), "account_id": "ops1", "deja_compensee": "1"})
    assert resp.status_code == 400
    assert _entries(fake) == {}


def _open(client, fake) -> tuple[str, str]:
    entry, errs = al.create_transaction(_depense())
    assert errs == [], errs
    html = client.get(f"/administration/{entry['id']}/modifier").get_data(as_text=True)
    shown = _etags(html)
    assert len(shown) == 1, shown
    return entry["id"], shown[0]


def _rival(fake, tx_id: str) -> None:
    doc = _entries(fake)[tx_id]
    doc.update(description="RIVAL-9Z", etag=RIVAL_ETAG)
    fake.external_write(f"admin_transactions/{tx_id}", doc)


def test_le_formulaire_de_modification_porte_l_etag_stocke(client, fake):
    tx_id, shown = _open(client, fake)
    assert shown == _entries(fake)[tx_id]["etag"] and shown
    create = client.get("/administration/nouvelle").get_data(as_text=True)
    assert _etags(create) == []


def test_une_sauvegarde_fraiche_ecrit(client, fake):
    tx_id, shown = _open(client, fake)
    resp = client.post(f"/administration/{tx_id}/modifier",
                       data={**_form(), "expected_etag": shown})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = _entries(fake)[tx_id]
    assert stored["description"] == "SOUMIS-7Q4"
    assert stored["etag"] != shown and stored["updated_via"] == "web"


def test_une_sauvegarde_perimee_n_ecrit_rien_et_montre_le_bandeau(client, fake):
    """Régression — l'ancienne route n'avait pas d'etag : l'onglet resté
    ouvert effaçait en silence ce qu'un autre avait écrit depuis."""
    tx_id, shown = _open(client, fake)
    _rival(fake, tx_id)
    before = _entries(fake)[tx_id]
    resp = client.post(f"/administration/{tx_id}/modifier",
                       data={**_form(), "expected_etag": shown})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BANNER in html
    assert "SOUMIS-7Q4" in html                     # the submission is kept
    assert _etags(html) == [RIVAL_ETAG]             # the CURRENT version
    assert _entries(fake)[tx_id] == before          # nothing written

    again = client.post(f"/administration/{tx_id}/modifier",
                        data={**_form(), "expected_etag": RIVAL_ETAG})
    assert again.status_code == 302                 # a deliberate overwrite
    assert _entries(fake)[tx_id]["description"] == "SOUMIS-7Q4"


def test_une_erreur_de_saisie_garde_l_etag_soumis(client, fake):
    tx_id, shown = _open(client, fake)
    before = _entries(fake)[tx_id]
    resp = client.post(f"/administration/{tx_id}/modifier",
                       data={**_form(counterparty=""), "expected_etag": shown})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert BANNER not in html
    assert _etags(html) == [shown]                  # the original protects the retry
    assert _entries(fake)[tx_id] == before


def test_une_page_sans_le_champ_ne_verifie_rien(client, fake):
    tx_id, _shown = _open(client, fake)
    _rival(fake, tx_id)
    resp = client.post(f"/administration/{tx_id}/modifier", data=_form())
    assert resp.status_code == 302
    assert _entries(fake)[tx_id]["description"] == "SOUMIS-7Q4"
    refused = client.post(f"/administration/{tx_id}/modifier",
                          data=_form(counterparty=""))
    assert refused.status_code == 400
    assert _etags(refused.get_data(as_text=True)) == []


def test_un_etag_illisible_est_un_400_en_francais(client, fake):
    tx_id, _shown = _open(client, fake)
    before = _entries(fake)[tx_id]
    resp = client.post(f"/administration/{tx_id}/modifier", data={
        **_form(), "expected_etag": '"><script>x</script>'})
    assert resp.status_code == 400
    assert "Rien n'a été enregistré" in resp.get_data(as_text=True)
    assert _entries(fake)[tx_id] == before


def test_une_ecriture_verrouillee_entre_temps_renvoie_a_la_fiche(client, fake):
    """Le formulaire était ouvert quand Claude a compensé l'écriture : elle
    a changé ET elle est désormais verrouillée. Re-rendre un formulaire
    qu'aucune sauvegarde ne pourra plus passer serait une impasse — la
    fiche l'explique."""
    tx_id, shown = _open(client, fake)
    _, errs = al.clear_transaction(tx_id, _d(2026, 9, 11))
    assert errs == []
    before = _entries(fake)[tx_id]
    resp = client.post(f"/administration/{tx_id}/modifier",
                       data={**_form(), "expected_etag": shown})
    assert resp.status_code == 302
    assert resp.location.endswith(f"/administration/{tx_id}?avertissement=verrouillee")
    assert _entries(fake)[tx_id] == before
    page = " ".join(client.get(resp.location).get_data(as_text=True).split())
    assert "désormais verrouillée" in page
    assert "vos changements n'ont pas été enregistrés" in page


# ══════════════════════════════════════════════════════════════════════
# 5. Le fidéicommis : la recette automatique ne projette plus rien
# ══════════════════════════════════════════════════════════════════════


def _seed_trust(fake) -> None:
    fake.seed("trust_accounts/acc1", {
        "id": "acc1", "name": "Général", "status": "actif",
        "account_type": "général", "book_balance": 0, "bank_balance": 0,
        "etag": "e0",
    })
    fake.seed("dossiers/dos1", {
        "id": "dos1", "file_number": "2026-001", "title": "T c. X",
        "client_ids": ["c1"], "clients": [{"id": "c1", "name": "Jean Tremblay"}],
        "trust_balance": 0, "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
    })
    fake.seed("invoices/inv1", {
        "id": "inv1", "invoice_number": "2026-F040", "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "dossier_title": "T c. X",
        "status": "envoyée", "total": 114975, "retainer_applied": 0,
        "amount_due": 114975, "amount_paid": 0,
    })
    fake.seed("invoices/inv2", {
        "id": "inv2", "invoice_number": "2026-F041", "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "dossier_title": "T c. X",
        "status": "envoyée", "total": 50000, "retainer_applied": 0,
        "amount_due": 50000, "amount_paid": 0,
    })
    receipt, errs = trust.create_transaction({
        "account_id": "acc1", "direction": "recette", "amount": 100000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 2),
        "description": "", "reference": "",
    })
    assert errs == [], errs
    _, errs = trust.clear_transaction(receipt["id"], _d(2026, 9, 2))
    assert errs == [], errs


def _fee_form(**over) -> dict:
    f = {
        "account_id": "acc1", "direction": "déboursé", "amount": "500,00",
        "purpose": "virement_honoraires", "method": "chèque",
        # D23 (2026-09-29, art. 58): the firm — the form offers only the
        # lawyer and the firm, and the model refuses anyone else.
        "counterparty": FEE_PAYEE, "dossier_id": "dos1",
        "client_id": "c1", "date": "2026-09-05",
        "invoice_number": "2026-F040", "admin_account_id": "ops1",
    }
    f.update(over)
    return f


def _fee_entry(fake) -> dict:
    fees = [t for t in fake.peek_collection("trust_transactions").values()
            if t.get("purpose") == "virement_honoraires"]
    assert len(fees) == 1, fees
    return fees[0]


def test_la_recette_du_paiement_d_honoraires_porte_le_paiement_une_seule_fois(
    client, fake
):
    """Régression d'ORDRE — si la route projetait encore après la recette
    désormais atomique, la facture porterait 1 000 $ pour un virement de
    500 $. La recette et le paiement tiennent dans UN commit."""
    _seed_trust(fake)
    fake.reset_logs()
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    recettes = [t for t in _entries(fake).values()
                if t.get("trust_transaction_id") == _fee_entry(fake)["id"]]
    assert len(recettes) == 1
    assert fake.peek("invoices/inv1")["amount_paid"] == 50000
    commits = _commit_touching(fake, "invoices/inv1")
    assert len(commits) == 1
    assert ("set", f"admin_transactions/{recettes[0]['id']}") in commits[0].ops


def test_une_recette_refusee_ne_laisse_rien_au_fideicommis_non_plus(
    client, fake, monkeypatch
):
    """Réécrit délibérément au lot 5a, étape 3 — il s'appelait « une
    recette refusée ne laisse ni recette ni paiement », et épinglait que le
    retrait au fidéicommis, lui, restait COMMIS sous une bannière (« inscrivez-
    la manuellement »). Régression de cette étape : le retrait, la recette et
    le paiement de la facture sont UNE transaction — la facture refuse, et
    rien n'est inscrit, nulle part : ni l'écriture au fidéicommis, ni son
    numéro de séquence, ni le solde du client. Le refus s'affiche au
    formulaire (400)."""
    _seed_trust(fake)
    trust_before = {
        "account": fake.peek("trust_accounts/acc1"),
        "dossier": fake.peek("dossiers/dos1"),
        "counter": fake.peek("counters/trust-acc1"),
        "entries": fake.peek_collection("trust_transactions"),
    }

    def _refuse(*_a, **_k):
        raise invoice_model.PaymentRefused("La facture refuse ce paiement.")

    monkeypatch.setattr(invoice_model, "payment_updates", _refuse)
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 400
    page = html.unescape(resp.get_data(as_text=True))
    assert "La facture refuse ce paiement." in page
    assert "ni au fidéicommis, ni au compte" in page
    assert fake.peek_collection("trust_transactions") == trust_before["entries"]
    assert fake.peek("trust_accounts/acc1") == trust_before["account"]
    assert fake.peek("dossiers/dos1") == trust_before["dossier"]
    assert fake.peek("counters/trust-acc1") == trust_before["counter"]
    assert _entries(fake) == {}
    assert fake.peek("invoices/inv1")["amount_paid"] == 0


def _avertissement(location: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(location).query).get("avertissement", [""])[0]


def _fee_with_recettes(fake, shares) -> dict:
    """A fee payment carried by several recettes — the reprise's split
    shape (one transfer paying two invoices). HISTORY: the trust leg is
    rebuilt through the trust model's own phases
    (``tests/_accounting_history`` — the public create refuses the purpose
    since lot 5a, step 3), the recettes by the real admin model."""
    fee = legacy_fee_entry({
        "account_id": "acc1", "direction": "déboursé", "amount": 50000,
        "purpose": "virement_honoraires", "method": "chèque",
        "counterparty": FEE_PAYEE, "dossier_id": "dos1", "client_id": "c1",
        "date": _d(2026, 9, 5), "invoice_id": "inv1",
        "description": "", "reference": "",
    })
    for invoice_id, amount in shares:
        _, errs = al.create_transaction({
            "account_id": "ops1", "kind": "encaissement_facture",
            "invoice_id": invoice_id, "amount": amount, "method": "virement",
            "counterparty": "Fidéicommis", "date": _d(2026, 9, 5),
        }, trust_transaction_id=fee["id"])
        assert errs == [], errs
    return fee


def test_la_contre_passation_du_virement_contre_passe_toutes_ses_recettes(
    client, fake
):
    """Régression — l'ancienne cascade lisait find_by_trust_transaction,
    qui ne rendait que la PREMIÈRE recette : le second encaissement et son
    paiement restaient debout. Chacune est maintenant contre-passée, chaque
    facture réduite — et depuis l'étape 3, tout cela ET la contre-passation
    au fidéicommis tiennent dans UN seul commit."""
    _seed_trust(fake)
    fee = _fee_with_recettes(fake, [("inv1", 30000), ("inv2", 20000)])
    assert fake.peek("invoices/inv1")["amount_paid"] == 30000
    assert fake.peek("invoices/inv2")["amount_paid"] == 20000
    fake.reset_logs()
    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})
    assert len(fake.commits) == 1
    assert ("update", f"trust_transactions/{fee['id']}") in fake.commits[0].ops
    assert ("update", "invoices/inv1") in fake.commits[0].ops
    assert ("update", "invoices/inv2") in fake.commits[0].ops
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    linked = [t for t in _entries(fake).values()
              if t.get("trust_transaction_id") == fee["id"]]
    assert len(linked) == 2 and all(t.get("reversed_by_id") for t in linked)
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    assert fake.peek("invoices/inv2")["amount_paid"] == 0
    assert _ledger(fake) == 0


def test_un_refus_d_une_recette_refuse_toute_la_contre_passation(
    client, fake
):
    """Réécrit délibérément au lot 5a, étape 3 — il s'appelait « un refus
    dans la cascade n'arrête pas les recettes suivantes » : la contre-passation
    au fidéicommis était DÉJÀ commise, chaque recette suivait dans son propre
    commit, et un refus laissait une recette debout sans plus aucun chemin
    pour la contre-passer. Régression de cette étape : c'est UNE transaction.
    La facture de la première recette ne concorde plus avec le registre —
    toute la contre-passation est refusée, et RIEN n'est écrit : ni au
    fidéicommis, ni à aucune des deux recettes, ni sur aucune facture."""
    _seed_trust(fake)
    fee = _fee_with_recettes(fake, [("inv1", 30000), ("inv2", 20000)])
    # The FIRST recette's invoice no longer agrees with the register (a
    # correction out of band): its reduction cannot be made.
    doc = fake.peek("invoices/inv1")
    doc.update(amount_paid=10000)
    fake.external_write("invoices/inv1", doc)
    before = {
        "trust": fake.peek_collection("trust_transactions"),
        "admin": _entries(fake),
        "inv1": fake.peek("invoices/inv1"),
        "inv2": fake.peek("invoices/inv2"),
    }

    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})

    assert resp.status_code == 400
    page = html.unescape(resp.get_data(as_text=True))
    assert "Rien n'a été contre-passé" in page
    assert fake.peek_collection("trust_transactions") == before["trust"]
    assert _entries(fake) == before["admin"]
    assert fake.peek("invoices/inv1") == before["inv1"]
    assert fake.peek("invoices/inv2") == before["inv2"]


def test_une_lecture_ratee_du_lien_refuse_la_contre_passation(client, fake, monkeypatch):
    """Réécrit délibérément au lot 5a, étape 3 — il s'appelait « une lecture
    ratée du lien est une bannière ». Avant le lot 5a le lecteur échouait
    OUVERT (« rien à contre-passer ») ; à l'étape 2 la lecture propageait,
    mais le fidéicommis était déjà contre-passé et seul un bandeau le
    disait. Désormais la lecture se fait DANS la transaction : illisible, la
    contre-passation est refusée et rien n'est écrit — le fidéicommis
    compris."""
    from google.cloud.firestore_v1.collection import CollectionReference

    _seed_trust(fake)
    fee = _fee_with_recettes(fake, [("inv1", 50000)])
    before = fake.peek_collection("trust_transactions")
    real_where = CollectionReference.where

    def _where(self, *args, **kwargs):
        flt = kwargs.get("filter")
        if getattr(flt, "field_path", None) == "trust_transaction_id":
            raise RuntimeError("firestore indisponible")
        return real_where(self, *args, **kwargs)

    monkeypatch.setattr(CollectionReference, "where", _where)
    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})
    monkeypatch.setattr(CollectionReference, "where", real_where)
    assert resp.status_code == 400
    assert "n'ont pas pu être lues" in html.unescape(resp.get_data(as_text=True))
    assert fake.peek_collection("trust_transactions") == before
    (recette,) = [t for t in _entries(fake).values()
                  if t.get("trust_transaction_id") == fee["id"]]
    assert not recette.get("reversed_by_id")
    assert fake.peek("invoices/inv1")["amount_paid"] == 50000


def test_le_formulaire_inscrit_la_recette_a_la_date_du_depot(client, fake):
    """D16 au formulaire web : le retrait du 5 septembre, déposé au compte
    d'opérations le 8, s'inscrit même une fois la période jusqu'au 6
    conciliée au compte d'opérations — la recette porte SA date, et le
    paiement de la facture aussi. Sans la date du dépôt, le refus NOMME le
    champ à remplir, et rien n'est inscrit."""
    _seed_trust(fake)
    fake.seed("admin_reconciliations/rec-ops1", {
        "id": "rec-ops1", "account_id": "ops1",
        "period_end": _d(2026, 9, 6), "status": "complétée",
    })
    before = fake.peek_collection("trust_transactions")
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 400
    page = html.unescape(resp.get_data(as_text=True))
    assert "« Date du dépôt au compte d'administration »" in page
    assert fake.peek_collection("trust_transactions") == before
    assert _entries(fake) == {}

    resp = client.post("/fideicommis/", data=_fee_form(admin_date="2026-09-08"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    (recette,) = _entries(fake).values()
    assert recette["date"] == _d(2026, 9, 8)
    assert _fee_entry(fake)["date"] == _d(2026, 9, 5)
    assert fake.peek("invoices/inv1")["paid_date"] == _d(2026, 9, 8)


def test_le_formulaire_offre_la_date_du_depot_et_la_reaffiche(client, fake):
    _seed_trust(fake)
    page = html.unescape(client.get("/fideicommis/nouvelle").get_data(as_text=True))
    assert 'name="admin_date"' in page
    assert "Date du dépôt au compte d'administration" in page
    resp = client.post("/fideicommis/", data=_fee_form(admin_date="2026-09-04"))
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert 'name="admin_date" value="2026-09-04"' in page
    assert "ne peut précéder la date" in html.unescape(page)


def test_la_fiche_du_fideicommis_n_a_plus_les_bandeaux_d_echec_de_la_recette():
    """Les deux états qu'ils annonçaient — la recette qui n'a pas suivi le
    paiement, une recette restée debout après la contre-passation — ne
    peuvent plus exister (une seule transaction, lot 5a étape 3)."""
    src = (_ATHENA / "templates" / "trust" / "detail.html").read_text(encoding="utf-8")
    assert "request.args.get('avertissement')" not in src
    assert "inscrivez-la manuellement" not in src


def _run_trust_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_trust_integrity as vti

    install(monkeypatch, vti, fake=fake)
    code = vti.main()
    return code, capsys.readouterr().out


def test_les_deux_controles_d_integrite_suivent_le_cycle_du_paiement_d_honoraires(
    client, fake, monkeypatch, capsys
):
    """Revue du lot 5a — les deux scripts de contrôle restent d'accord avec
    les modèles sur le cycle entier : paiement d'honoraires (retrait,
    recette et paiement de facture en UN commit depuis l'étape 3), puis sa
    contre-passation (le fidéicommis, chaque recette et sa facture, en UN
    commit aussi).

    Réécrit délibérément à l'étape 3 : la dernière partie simulait une
    cascade de contre-passation qui ne suivait pas (la route appelait
    ``admin_ledger.reverse_transaction`` après coup) et vérifiait que le
    contrôle nommait la recette restée debout. Cet état ne peut plus naître
    — le contrôle garde son test sur l'HISTORIQUE (test_verify_trust_integrity).
    Ce qui reste à prouver : un refus de la contre-passation laisse les deux
    registres propres."""
    _seed_trust(fake)
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 302 and "avertissement" not in resp.location
    fee = _fee_entry(fake)
    assert _run_trust_integrity(fake, monkeypatch, capsys)[0] == 0
    assert _run_integrity(fake, monkeypatch, capsys)[0] == 0

    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})
    assert resp.status_code == 302 and "avertissement" not in resp.location
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    assert _run_trust_integrity(fake, monkeypatch, capsys)[0] == 0
    assert _run_integrity(fake, monkeypatch, capsys)[0] == 0

    # A second fee payment (dated after the reversal — the trust register
    # refuses backdating) whose reversal is REFUSED: its invoice no longer
    # agrees with the register. Nothing moves, both checks stay clean.
    resp = client.post("/fideicommis/", data=_fee_form(amount="200,00",
                                                       date="2026-09-20"))
    assert resp.status_code == 302 and "avertissement" not in resp.location
    (second,) = [t for t in fake.peek_collection("trust_transactions").values()
                 if t.get("purpose") == "virement_honoraires"
                 and not t.get("reversed_by_id")]
    resp = client.post(f"/fideicommis/{second['id']}/contrepasser", data={"reason": ""})
    assert resp.status_code == 400
    assert fake.peek(f"trust_transactions/{second['id']}").get("reversed_by_id") is None
    assert _run_trust_integrity(fake, monkeypatch, capsys)[0] == 0
    assert _run_integrity(fake, monkeypatch, capsys)[0] == 0


# ══════════════════════════════════════════════════════════════════════
# 6. La reprise historique ne projette plus — jamais deux fois
# ══════════════════════════════════════════════════════════════════════


def _reprise_action(fake, etat: str, ecriture=None) -> dict:
    virement = {
        "id": "ttx1", "date": _d(2026, 9, 3), "amount": 50000, "sequence": 3,
        "client_name": "Jean Tremblay", "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "reference": "",
        "invoice_external_ref": "",
    }
    return {"virement": virement, "facture": dict(_invoice(fake)),
            "mode": "encaissement", "montant": 50000, "etat": etat,
            "ecriture": ecriture}


def test_la_reprise_ne_credite_la_facture_qu_une_fois(fake):
    from scripts import reprise_encaissements as rep

    echecs = rep.appliquer("ops1", [_reprise_action(fake, "à_créer")])
    assert echecs == [], echecs
    assert _invoice(fake)["amount_paid"] == 50000
    (entry,) = _entries(fake).values()
    assert entry["status"] == "compensée"


def test_rejouer_une_ligne_a_compenser_ne_double_pas_le_paiement(fake):
    """Régression — l'ancienne reprise re-projetait le paiement sur une ligne
    « à compenser » (écriture déjà inscrite, compensation manquée) : la
    facture recevait le montant une SECONDE fois. L'écriture porte son
    paiement depuis sa création ; le rejeu ne fait que compenser."""
    from scripts import reprise_encaissements as rep

    entry, errs = al.create_transaction(_enc(amount=50000, date=_d(2026, 9, 3)),
                                        trust_transaction_id="ttx1")
    assert errs == []
    assert _invoice(fake)["amount_paid"] == 50000
    echecs = rep.appliquer("ops1", [_reprise_action(fake, "à_compenser", entry)])
    assert echecs == [], echecs
    assert _invoice(fake)["amount_paid"] == 50000
    assert _entries(fake)[entry["id"]]["status"] == "compensée"


def test_la_reprise_n_appelle_aucun_ecrivain_de_paiement_hors_du_depaiement():
    """Le seul écrivain de paiement que le script atteint encore est la
    remise à zéro de ``_depayer`` (``record_payment(id, 0)``) — jamais
    l'exécution : l'écriture porte son paiement."""
    tree = ast.parse((_ATHENA / "scripts" / "reprise_encaissements.py")
                     .read_text(encoding="utf-8"))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    watched = {"record_payment", "payment_updates", "projeter_paiement",
               "reduire_paiement"}

    def _refs(fn) -> set:
        out = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in watched:
                out.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in watched:
                out.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                out |= {a.name for a in node.names if a.name in watched}
        return out

    assert _refs(functions["appliquer"]) == set()
    assert _refs(functions["_depayer"]) == {"record_payment"}
    assert not any(isinstance(n, ast.ImportFrom)
                   and (n.module or "").startswith("services")
                   for n in ast.walk(tree))


def test_un_paiement_dont_l_issue_est_inconnue_le_dit_au_formulaire(
    client, fake, monkeypatch,
):
    """Revue du lot 5a (concurrence) — le commit du paiement d'honoraires
    ABOUTIT, puis le client reçoit une expiration. Le formulaire se réaffiche
    en disant que l'issue est INCONNUE et qu'il faut vérifier le journal —
    jamais « Rien n'a été inscrit. Veuillez réessayer », que l'ancien code
    affichait par-dessus un retrait inscrit : la reprise de l'avocat
    retirait les honoraires une seconde fois."""
    from google.api_core import exceptions as gexc

    from models import fee_payment

    _seed_trust(fake)
    server = fake._fake_server
    real_commit = server.commit
    armed = {"on": True}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        if armed["on"] and any("/trust_transactions/" in server._write_name(w)
                               for w in writes):
            armed["on"] = False
            raise gexc.DeadlineExceeded("answer lost")
        return response

    monkeypatch.setattr(server, "commit", _commit)
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 400
    page = html.unescape(resp.get_data(as_text=True))
    assert "Rien n'a été inscrit" not in page
    assert html.unescape(fee_payment.CREATE_OUTCOME_UNCERTAIN) in page
    _fee_entry(fake)                                   # it DID land, once


# ══════════════════════════════════════════════════════════════════════
# Revue du lot 5b (concurrence) — la page de confirmation d'une
# contre-passation dit ce qu'elle fera, et cela dépend du STATUT
# ══════════════════════════════════════════════════════════════════════
#
# En circulation, l'écriture et sa contre-passation deviennent annulée ;
# compensée, la contre-passation entre en circulation — un mouvement bancaire
# à venir. Le connecteur compense désormais des écritures : une page ouverte
# avant sa compensation annonçait « annulée », et le clic contre-passait
# l'écriture compensée. La page porte maintenant la version qu'elle décrit ;
# le modèle refuse une confirmation faite sur une autre (plan D9).

_REVERSE_STALE = "Cette écriture a changé depuis l'ouverture de cette page."


def _deposit_id(fake) -> str:
    """A deposit still EN CIRCULATION (the seeded one is cleared)."""
    _seed_trust(fake)
    deposit, errs = trust.create_transaction({
        "account_id": "acc1", "direction": "recette", "amount": 25000,
        "purpose": "dépôt_client", "method": "chèque", "counterparty": "Client",
        "dossier_id": "dos1", "client_id": "c1", "date": _d(2026, 9, 4),
        "description": "", "reference": "",
    })
    assert errs == [], errs
    return deposit["id"]


def test_la_confirmation_de_contre_passation_porte_la_version_decrite(client, fake):
    rid = _deposit_id(fake)
    page = client.get(f"/fideicommis/{rid}/contrepasser").get_data(as_text=True)
    assert _etags(page) == [fake.peek(f"trust_transactions/{rid}")["etag"]]
    entry, errs = al.create_transaction(_depense())
    assert errs == [], errs
    page = client.get(f"/administration/{entry['id']}/contrepasser").get_data(as_text=True)
    assert _etags(page) == [entry["etag"]]


def test_une_contre_passation_confirmee_sur_un_statut_depasse_n_ecrit_rien(
    client, fake,
):
    """Régression — la page annonçait « toutes deux marquées annulée », le
    connecteur compensait l'écriture entre-temps, et le clic créait une
    contre-passation EN CIRCULATION que la page n'avait jamais annoncée."""
    from services import comptabilite as svc

    rid = _deposit_id(fake)
    url = f"/fideicommis/{rid}/contrepasser"
    page = html.unescape(client.get(url).get_data(as_text=True))
    assert "toutes deux marquées" in page
    (shown,) = _etags(page)
    assert svc.compenser_fideicommis([rid], _d(2026, 9, 5))["ok"]  # Claude
    before = fake.peek_collection("trust_transactions")

    resp = client.post(url, data={"reason": "Doublon-4K2", "expected_etag": shown})
    assert resp.status_code == 200
    page = html.unescape(resp.get_data(as_text=True))
    assert _REVERSE_STALE in page and "Rien n'a été contre-passé" in page
    assert "demeure compensée" in page            # what it will do NOW
    assert "Doublon-4K2" in page                  # the motif is kept
    current = fake.peek(f"trust_transactions/{rid}")["etag"]
    assert _etags(page) == [current] and current != shown
    assert fake.peek_collection("trust_transactions") == before

    again = client.post(url, data={"reason": "Doublon-4K2", "expected_etag": current})
    assert again.status_code == 302, again.get_data(as_text=True)[:500]
    stored = fake.peek(f"trust_transactions/{rid}")
    assert stored["status"] == "compensée" and stored["reversed_by_id"]
    reversal = fake.peek(f"trust_transactions/{stored['reversed_by_id']}")
    assert reversal["status"] == "en_circulation"


def test_une_contre_passation_d_administration_sur_un_statut_depasse_n_ecrit_rien(
    client, fake,
):
    from services import comptabilite as svc

    entry, errs = al.create_transaction(_depense())
    assert errs == [], errs
    url = f"/administration/{entry['id']}/contrepasser"
    (shown,) = _etags(client.get(url).get_data(as_text=True))
    assert svc.compenser_administration([entry["id"]], _d(2026, 9, 12))["ok"]
    before = _entries(fake)

    resp = client.post(url, data={"reason": "Doublon-7P1", "expected_etag": shown,
                                  "reversal_date": "2026-09-15"})
    assert resp.status_code == 200
    page = html.unescape(resp.get_data(as_text=True))
    assert _REVERSE_STALE in page and "demeure compensée" in page
    assert "Doublon-7P1" in page
    current = _entries(fake)[entry["id"]]["etag"]
    assert _etags(page) == [current] and current != shown
    assert _entries(fake) == before

    again = client.post(url, data={"reason": "Doublon-7P1", "expected_etag": current,
                                   "reversal_date": "2026-09-15"})
    assert again.status_code == 302, again.get_data(as_text=True)[:500]
    assert _entries(fake)[entry["id"]]["reversed_by_id"]


def test_une_page_de_contre_passation_sans_le_champ_ne_verifie_rien(client, fake):
    """Une page ouverte AVANT ce déploiement ne porte aucune version : elle
    contre-passe comme avant (le modèle ne vérifie rien), jamais un refus
    qu'aucune page ouverte ne pourrait passer."""
    rid = _deposit_id(fake)
    resp = client.post(f"/fideicommis/{rid}/contrepasser", data={"reason": "Doublon"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert fake.peek(f"trust_transactions/{rid}")["status"] == "annulée"


def test_une_version_illisible_a_la_contre_passation_est_un_400_en_francais(
    client, fake,
):
    rid = _deposit_id(fake)
    before = fake.peek_collection("trust_transactions")
    resp = client.post(f"/fideicommis/{rid}/contrepasser", data={
        "reason": "Doublon", "expected_etag": '"><script>x</script>'})
    assert resp.status_code == 400
    assert "Rien n'a été enregistré" in resp.get_data(as_text=True)
    assert fake.peek_collection("trust_transactions") == before


# ── La compensation d'une écriture d'administration porte la version vue ──
#
# Compenser affirme que CE montant, à CETTE date, figure au relevé — et une
# écriture d'administration reste modifiable, par le connecteur aussi
# (update_admin_entry, lot 5b), jusqu'à sa compensation. La fiche ouverte sur
# 114,98 $ compensait les 200,00 $ que Claude avait écrits entre-temps, et
# les verrouillait là.


def test_une_compensation_d_une_ecriture_modifiee_entre_temps_n_ecrit_rien(
    client, fake,
):
    """Régression — la fiche affichait un montant, le connecteur l'a corrigé,
    le bouton « Compenser » compensait le nouveau montant, jamais vu."""
    entry, errs = al.create_transaction(_depense())
    assert errs == [], errs
    tx_id = entry["id"]
    page = client.get(f"/administration/{tx_id}").get_data(as_text=True)
    (shown,) = _etags(page)
    assert shown == entry["etag"]
    edited, errs = al.update_transaction(                       # Claude
        tx_id, {"amount": 20000, "net_amount": 20000, "gst_amount": 0,
                "qst_amount": 0}, expected_etag=shown)
    assert errs == [], errs
    before = _entries(fake)[tx_id]

    resp = client.post(f"/administration/{tx_id}/compenser", data={
        "cleared_date": "2026-09-12", "expected_etag": shown})
    assert resp.status_code == 302
    assert "avertissement=compensation_modifiee" in resp.location
    assert _entries(fake)[tx_id] == before                      # nothing written
    landing = html.unescape(client.get(resp.location).get_data(as_text=True))
    assert "n'a PAS été compensée" in landing
    assert _etags(landing) == [before["etag"]]

    again = client.post(f"/administration/{tx_id}/compenser", data={
        "cleared_date": "2026-09-12", "expected_etag": before["etag"]})
    assert again.status_code == 302 and "avertissement" not in again.location
    stored = _entries(fake)[tx_id]
    assert stored["status"] == "compensée" and stored["amount"] == 20000


def test_une_compensation_sans_le_champ_ne_verifie_rien(client, fake):
    entry, errs = al.create_transaction(_depense())
    assert errs == [], errs
    resp = client.post(f"/administration/{entry['id']}/compenser",
                       data={"cleared_date": "2026-09-12"})
    assert resp.status_code == 302 and "avertissement" not in resp.location
    assert _entries(fake)[entry["id"]]["status"] == "compensée"
