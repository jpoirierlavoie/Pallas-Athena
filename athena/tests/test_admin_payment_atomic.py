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
    from models import trust
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

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
        "counterparty": "Me Jason Poirier Lavoie", "dossier_id": "dos1",
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


def test_une_recette_refusee_ne_laisse_ni_recette_ni_paiement(
    client, fake, monkeypatch
):
    """Régression — l'ancienne route inscrivait la recette, puis la
    projection échouait : une recette d'administration debout sans son
    paiement sur la facture. Aujourd'hui les deux échouent ENSEMBLE ; le
    virement au fidéicommis, déjà commis, reste signalé par la bannière
    (le rendre atomique avec lui est l'étape 8 du lot)."""
    _seed_trust(fake)

    def _refuse(*_a, **_k):
        raise invoice_model.PaymentRefused("La facture refuse ce paiement.")

    monkeypatch.setattr(invoice_model, "payment_updates", _refuse)
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert _avertissement(resp.location) == "administration"
    assert _fee_entry(fake)["amount"] == 50000         # the trust side committed
    assert _entries(fake) == {}                         # no half recette
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    # Revue du lot 5a — le bandeau disait « inscrivez-la (ou corrigez-la) » :
    # il n'existe plus de recette debout à corriger (elle et son paiement
    # échouent ensemble), il n'y a qu'à l'inscrire.
    page = client.get(resp.location).get_data(as_text=True)
    assert "la recette au compte" in page and "inscrivez-la manuellement" in page
    assert "corrigez-la" not in page


def _avertissement(location: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(location).query).get("avertissement", [""])[0]


def _fee_with_recettes(fake, shares) -> dict:
    """A fee payment carried by several recettes — the reprise's split
    shape (one transfer paying two invoices), minted by the real models."""
    fee, errs = trust.create_transaction({
        "account_id": "acc1", "direction": "déboursé", "amount": 50000,
        "purpose": "virement_honoraires", "method": "chèque",
        "counterparty": "Me Avocat", "dossier_id": "dos1", "client_id": "c1",
        "date": _d(2026, 9, 5), "invoice_id": "inv1",
        "description": "", "reference": "",
    })
    assert errs == [], errs
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
    paiement restaient debout. Chacune est maintenant contre-passée, et
    chaque facture réduite dans le commit de sa contre-passation."""
    _seed_trust(fake)
    fee = _fee_with_recettes(fake, [("inv1", 30000), ("inv2", 20000)])
    assert fake.peek("invoices/inv1")["amount_paid"] == 30000
    assert fake.peek("invoices/inv2")["amount_paid"] == 20000
    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    linked = [t for t in _entries(fake).values()
              if t.get("trust_transaction_id") == fee["id"]]
    assert len(linked) == 2 and all(t.get("reversed_by_id") for t in linked)
    assert fake.peek("invoices/inv1")["amount_paid"] == 0
    assert fake.peek("invoices/inv2")["amount_paid"] == 0
    assert _ledger(fake) == 0


def test_une_lecture_ratee_du_lien_est_une_banniere(client, fake, monkeypatch):
    """Régression — l'ancien lecteur échouait OUVERT à None : la cascade
    concluait « rien à contre-passer », sans bannière, et la recette et le
    paiement restaient debout. La lecture propage maintenant ; l'avocat voit
    que le côté administration n'a pas suivi."""
    from google.cloud.firestore_v1.collection import CollectionReference

    _seed_trust(fake)
    fee = _fee_with_recettes(fake, [("inv1", 50000)])
    # The STORE fails the lookup by trust link — whichever reader asks it.
    real_where = CollectionReference.where

    def _where(self, *args, **kwargs):
        flt = kwargs.get("filter")
        if getattr(flt, "field_path", None) == "trust_transaction_id":
            raise RuntimeError("firestore indisponible")
        return real_where(self, *args, **kwargs)

    monkeypatch.setattr(CollectionReference, "where", _where)
    resp = client.post(f"/fideicommis/{fee['id']}/contrepasser",
                       data={"reason": "chèque perdu"})
    assert resp.status_code == 302
    (recette,) = [t for t in _entries(fake).values()
                  if t.get("trust_transaction_id") == fee["id"]]
    assert not recette.get("reversed_by_id")       # it did not follow…
    assert fake.peek("invoices/inv1")["amount_paid"] == 50000   # …and it says so
    # Revue du lot 5a — régression : la contre-passation empruntait le
    # bandeau de la CRÉATION (« inscrivez-la (ou corrigez-la) manuellement au
    # registre d'administration »), une consigne impossible — ce registre
    # refuse de contre-passer seul une recette liée au fidéicommis
    # (écriture_liée_fideicommis). Le bandeau propre dit ce qui reste debout.
    assert _avertissement(resp.location) == "administration_contrepassation"
    monkeypatch.setattr(CollectionReference, "where", real_where)
    page = client.get(resp.location).get_data(as_text=True)
    assert "La contre-passation est inscrite au fidéicommis" in page
    assert "ne se contre-passe pas" in page
    assert "inscrivez-la" not in page and "corrigez-la" not in page
    _, errs = al.reverse_transaction(recette["id"], "à la main")
    assert errs == [al._ABORT_MESSAGES["écriture_liée_fideicommis"]]  # the reason


def _run_trust_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_trust_integrity as vti

    install(monkeypatch, vti, fake=fake)
    code = vti.main()
    return code, capsys.readouterr().out


def test_les_deux_controles_d_integrite_suivent_le_cycle_du_paiement_d_honoraires(
    client, fake, monkeypatch, capsys
):
    """Revue du lot 5a — les deux scripts de contrôle restent d'accord avec
    les modèles sur le cycle entier que cette étape a rendu atomique côté
    administration : paiement d'honoraires (recette + paiement de facture en
    un commit), puis sa contre-passation (chaque recette contre-passée, sa
    facture réduite dans le même commit). Et quand la cascade ne suit PAS,
    le contrôle du fidéicommis nomme la recette restée debout — le bandeau
    de la contre-passation y renvoie l'avocat, il faut donc qu'il la voie."""
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
    # refuses backdating) whose reversal cascade is REFUSED by the model.
    resp = client.post("/fideicommis/", data=_fee_form(amount="200,00",
                                                       date="2026-09-20"))
    assert resp.status_code == 302 and "avertissement" not in resp.location
    (second,) = [t for t in fake.peek_collection("trust_transactions").values()
                 if t.get("purpose") == "virement_honoraires"
                 and not t.get("reversed_by_id")]
    monkeypatch.setattr(al, "reverse_transaction",
                        lambda *a, **k: (None, ["Contre-passation refusée."]))
    resp = client.post(f"/fideicommis/{second['id']}/contrepasser",
                       data={"reason": "erreur"})
    assert _avertissement(resp.location) == "administration_contrepassation"
    code, out = _run_trust_integrity(fake, monkeypatch, capsys)
    assert code == 1, out
    assert f"(écriture {second['id']}): paiement d'honoraires annulé" in out
    assert "restent debout" in out
    assert "encore compté au compte d'opérations" in out
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
