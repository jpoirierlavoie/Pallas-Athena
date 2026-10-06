"""Les règles du registre d'administration que le MODÈLE porte (lot 0b, B6).

Chaque règle vit dans ``models/admin_ledger.py`` — là où le web, le
fidéicommis, le script de reprise et demain le connecteur passent tous — et
chaque test marqué « régression » échoue sur le code antérieur (vérifié en le
rétablissant) ; les autres sont des témoins du chemin web.

1. **Le sens découle du type.** Seule la route web dérivait le sens d'une
   écriture ; le modèle vérifiait « encaissement ⇒ recette » et « dépense ⇒
   déboursé », mais PAS « autre recette ⇒ recette ». Un appel direct pouvait
   donc inscrire une « Autre recette » en DÉBOURSÉ : un débit sous l'étiquette
   d'une recette, le solde bougeant dans le mauvais sens pendant que tous les
   rapports lisaient « recette ». Le modèle dérive désormais un sens absent,
   refuse un sens nommé qui contredit le type, et une modification qui
   change le type re-dérive le sens.
2. **Le lien au fidéicommis ne voyage qu'en mot-clé.** Un
   ``trust_transaction_id`` glissé dans les données est refusé ; le paiement
   d'honoraires du fidéicommis et le script de reprise — ses deux seuls
   auteurs — le passent en mot-clé, basculés dans le MÊME commit (l'ordre
   critique relevé par la revue du lot 5), et le paiement web crée toujours
   sa recette de bout en bout.

Le banc est le faux Firestore partagé (``tests/_fake_firestore.py``) : le
client, ses transactions et la boucle de reprise de ``transactional`` sont
les vrais ; on relit ce qui est STOCKÉ.
"""

import json
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
    import routes.admin_ledger as admin_ledger_routes
    import routes.dossiers as dossiers_routes
    import routes.invoices as invoices_routes
    import routes.trust as trust_routes

from flask import Flask  # noqa: E402

from tests._accounting_history import FEE_PAYEE  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402

UTC = timezone.utc


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════════════
# Le banc
# ══════════════════════════════════════════════════════════════════════


def _fake_modules() -> list:
    """Every module holding the Firestore client — derived, so a model a
    route starts to read tomorrow cannot reach the mocked client instead."""
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    f.seed("admin_accounts/ops1", {
        "id": "ops1", "name": "Opérations", "status": "actif",
        "account_type": "opérations", "ledger_balance": 0, "etag": "e0",
    })
    return f


@pytest.fixture
def refusals(monkeypatch) -> list:
    """Every ``admin_transaction_refused`` the model emits, by reason."""
    seen: list = []
    real = al.log_admin_ledger_event

    def _spy(event, outcome="success", **fields):
        if event == "admin_transaction_refused":
            seen.append(fields.get("reason"))
        return real(event, outcome, **fields)

    monkeypatch.setattr(al, "log_admin_ledger_event", _spy)
    return seen


def _data(**over) -> dict:
    d = {
        "account_id": "ops1", "kind": "dépense", "category": "loyer",
        "amount": 100000, "method": "virement",
        "counterparty": "Immeubles Sainte-Catherine", "date": _d(2026, 9, 1),
        "description": "", "reference": "", "supplier_invoice_ref": "",
    }
    d.update(over)
    return d


def _recette(**over) -> dict:
    return _data(**{"kind": "recette_autre", "category": "",
                    "counterparty": "Intérêts BNC", **over})


def _ledger(fake) -> int:
    return fake.peek("admin_accounts/ops1")["ledger_balance"]


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
        s["user_email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _only_entry(fake) -> dict:
    rows = list(fake.peek_collection("admin_transactions").values())
    assert len(rows) == 1, rows
    return rows[0]


# ══════════════════════════════════════════════════════════════════════
# 1. Le sens découle du type
# ══════════════════════════════════════════════════════════════════════


def test_une_autre_recette_ne_s_inscrit_jamais_en_debourse(fake, refusals):
    """Régression — l'ancien modèle acceptait et STOCKAIT le déboursé : un
    débit de 1 000 $ étiqueté « Autre recette »."""
    entry, errs = al.create_transaction(_recette(direction="déboursé"))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert fake.peek_collection("admin_transactions") == {}
    assert fake.peek("counters/admin-ops1") is None
    assert _ledger(fake) == 0
    assert refusals == ["sens_incohérent"]


@pytest.mark.parametrize("kind, forged", [
    ("dépense", "recette"),
    ("encaissement_facture", "déboursé"),
])
def test_un_sens_qui_contredit_le_type_est_nomme(fake, kind, forged):
    """Les deux paires que l'ancien modèle refusait déjà — mais sous « Le
    type d'opération est invalide », qui désignait le mauvais champ."""
    over = {"kind": kind, "direction": forged}
    if kind == "encaissement_facture":
        over.update(category="", invoice_id="fac1")
    entry, errs = al.create_transaction(_data(**over))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert fake.peek_collection("admin_transactions") == {}


@pytest.mark.parametrize("kind, implied, ledger", [
    ("dépense", "déboursé", -100000),
    ("recette_autre", "recette", 100000),
])
def test_un_sens_absent_decoule_du_type(fake, kind, implied, ledger):
    """Régression — l'ancien modèle exigeait un sens (« Le sens de
    l'opération est invalide ») que seule la route savait déduire."""
    over = {"kind": kind}
    if kind == "recette_autre":
        over.update(category="", counterparty="Intérêts BNC")
    entry, errs = al.create_transaction(_data(**over))
    assert errs == [], errs
    assert fake.peek(f"admin_transactions/{entry['id']}")["direction"] == implied
    assert _ledger(fake) == ledger


def test_l_encaissement_sans_sens_est_une_recette(fake):
    fake.seed("invoices/fac1", {
        "id": "fac1", "invoice_number": "2026-F031", "status": "envoyée",
        "amount_due": 100000, "amount_paid": 0, "dossier_id": "dos1",
        "dossier_file_number": "2026-001", "dossier_title": "T c. X",
    })
    entry, errs = al.create_transaction(_data(
        kind="encaissement_facture", category="", invoice_id="fac1",
        amount=60000, counterparty="Jean Tremblay"))
    assert errs == [], errs
    assert fake.peek(f"admin_transactions/{entry['id']}")["direction"] == "recette"
    assert _ledger(fake) == 60000


def test_un_sens_inconnu_reste_refuse(fake):
    entry, errs = al.create_transaction(_recette(direction="crédit"))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["direction_invalide"]]


def test_changer_le_type_re_derive_le_sens(fake):
    """Régression — l'ancien update gardait le déboursé stocké quand le type
    passait à « Autre recette » : le solde restait à −1 000 $ sous une
    étiquette de recette."""
    entry, _ = al.create_transaction(_data())
    assert _ledger(fake) == -100000
    updated, errs = al.update_transaction(
        entry["id"], {"kind": "recette_autre", "category": ""})
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["kind"] == "recette_autre"
    assert stored["direction"] == "recette"
    assert _ledger(fake) == 100000
    assert stored["revisions"][-1]["changes"]["direction"] == ["déboursé", "recette"]


def test_un_sens_nomme_en_modification_doit_suivre_le_type(fake, refusals):
    entry, _ = al.create_transaction(_data())
    before = fake.peek(f"admin_transactions/{entry['id']}")
    updated, errs = al.update_transaction(
        entry["id"], {"kind": "recette_autre", "category": "",
                      "direction": "déboursé"})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert fake.peek(f"admin_transactions/{entry['id']}") == before
    assert _ledger(fake) == -100000
    assert refusals == ["sens_incohérent"]


def test_le_sens_seul_ne_change_jamais(fake):
    """Régression (message) — un sens sans son type est refusé et le refus
    nomme le sens ; l'ancien disait « Le type d'opération est invalide »."""
    entry, _ = al.create_transaction(_data())
    updated, errs = al.update_transaction(entry["id"], {"direction": "recette"})
    assert updated is None
    assert errs == [al._ABORT_MESSAGES["sens_incohérent"]]
    assert fake.peek(f"admin_transactions/{entry['id']}")["direction"] == "déboursé"


def test_un_compte_absent_est_un_refus_journalise(fake, refusals):
    """Régression (revue B6) — le registre promet « toute » abandon de
    création journalisé, garde sans lecture comprise ; le compte absent
    rendait son refus sans laisser de ligne."""
    entry, errs = al.create_transaction(_data(account_id=""))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["compte_introuvable"]]
    assert refusals == ["compte_introuvable"]
    assert fake.peek_collection("admin_transactions") == {}


def test_une_modification_sans_type_garde_le_sens(fake):
    """Témoin : une modification qui ne nomme pas le type ne touche pas au
    sens (et une modification sans changement n'écrit rien)."""
    entry, _ = al.create_transaction(_data())
    updated, errs = al.update_transaction(entry["id"], {"description": "Loyer de sept."})
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["direction"] == "déboursé"
    assert "direction" not in stored["revisions"][-1]["changes"]


# ── Le chemin web : la route ne dérive plus rien, le modèle le fait ─────


def _form(**over) -> dict:
    f = {
        "account_id": "ops1", "kind": "recette_autre", "amount": "250,00",
        "method": "virement", "counterparty": "Intérêts BNC",
        "date": "2026-09-01", "description": "", "reference": "",
    }
    f.update(over)
    return f


def test_le_formulaire_inscrit_une_recette_sans_choisir_de_sens(fake, client):
    """Témoin — et un champ « direction » forgé n'est même pas lu."""
    resp = client.post("/administration/", data=_form(direction="déboursé"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = _only_entry(fake)
    assert stored["direction"] == "recette"
    assert _ledger(fake) == 25000


def test_le_formulaire_de_modification_re_derive_le_sens(fake, client):
    """Témoin : la route n'envoie plus de sens ; le changement de type passe
    donc par la re-dérivation du modèle."""
    entry, _ = al.create_transaction(_data(amount=25000))
    resp = client.post(f"/administration/{entry['id']}/modifier",
                       data=_form(kind="recette_autre"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["kind"] == "recette_autre" and stored["direction"] == "recette"
    assert _ledger(fake) == 25000


def test_un_formulaire_sans_type_dit_le_type(fake, client):
    """Régression (message) — le refus nommait « le sens », un champ que le
    formulaire n'a pas ; il nomme maintenant le type."""
    resp = client.post("/administration/", data=_form(kind=""))
    assert resp.status_code == 400
    assert "Le type d&#39;opération est invalide." in resp.get_data(as_text=True)
    assert fake.peek_collection("admin_transactions") == {}


# ══════════════════════════════════════════════════════════════════════
# 2. Le lien au fidéicommis ne voyage qu'en MOT-CLÉ
# ══════════════════════════════════════════════════════════════════════
#
# Une recette qui porte un trust_transaction_id est verrouillée : ni
# modifiable, ni supprimable, contre-passable seulement depuis le
# fidéicommis (_entry_lock_reason). Ce lien ne doit donc jamais voyager dans
# des données qu'un formulaire ou un argument d'outil sait remplir. Le
# modèle le refuse dans ``data`` ; ses deux seuls auteurs — le paiement
# d'honoraires du fidéicommis et le script de reprise — le passent en
# mot-clé, basculés DANS LE MÊME COMMIT que le refus : sans quoi chaque
# paiement d'honoraires web aurait, au déploiement suivant, sorti l'argent
# du fidéicommis sans recette d'administration (la classe de l'incident de
# juillet 2026).


@pytest.mark.parametrize("valeur", ["ttx1", None, ""])
def test_un_lien_au_fideicommis_dans_les_donnees_est_refuse(fake, refusals, valeur):
    """Régression — l'ancien modèle lisait le lien dans ``data`` et le
    stockait (ou stockait None sans rien dire)."""
    entry, errs = al.create_transaction(_recette(trust_transaction_id=valeur))
    assert entry is None
    assert errs == [al._ABORT_MESSAGES["lien_fideicommis_réservé"]]
    assert fake.peek_collection("admin_transactions") == {}
    assert _ledger(fake) == 0
    assert refusals == ["lien_fideicommis_réservé"]


def test_le_mot_cle_pose_le_lien_et_verrouille_l_ecriture(fake):
    """Régression — le mot-clé n'existait pas (TypeError sur l'ancien code)."""
    entry, errs = al.create_transaction(_recette(), trust_transaction_id="ttx1")
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["trust_transaction_id"] == "ttx1"
    assert al._entry_lock_reason(stored, None) == "écriture_liée_fideicommis"


def test_sans_mot_cle_aucun_lien(fake):
    entry, errs = al.create_transaction(_recette())
    assert errs == [], errs
    assert fake.peek(f"admin_transactions/{entry['id']}")["trust_transaction_id"] is None


def test_le_formulaire_d_administration_ne_pose_jamais_le_lien(fake, client):
    """Témoin : le champ forgé n'est même pas lu par la route."""
    resp = client.post("/administration/", data=_form(trust_transaction_id="ttx1"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert _only_entry(fake)["trust_transaction_id"] is None


def test_la_modification_ne_pose_ni_ne_retire_jamais_le_lien(fake, client):
    """Témoin (revue B6) : le lien n'est pas un champ modifiable — ni par le
    modèle (hors de _EDITABLE_FIELDS), ni par le formulaire de modification.
    Le poser après coup verrouillerait une écriture que personne n'a
    rattachée ; le retirer déverrouillerait une recette du fidéicommis."""
    assert "trust_transaction_id" not in al._EDITABLE_FIELDS
    entry, _ = al.create_transaction(_recette())
    updated, errs = al.update_transaction(
        entry["id"], {"trust_transaction_id": "ttx1", "description": "Intérêts"})
    assert errs == [], errs
    stored = fake.peek(f"admin_transactions/{entry['id']}")
    assert stored["trust_transaction_id"] is None
    assert stored["description"] == "Intérêts"
    resp = client.post(f"/administration/{entry['id']}/modifier",
                       data=_form(trust_transaction_id="ttx1", description="Frais"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert fake.peek(f"admin_transactions/{entry['id']}")["trust_transaction_id"] is None


# ── Le paiement d'honoraires web crée toujours sa recette, de bout en bout ──


def _seed_trust(fake) -> None:
    from models import trust

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
    # A cleared deposit: the funds the overdraft control will release.
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


def test_le_paiement_d_honoraires_cree_sa_recette_liee(fake, client):
    """Régression de l'ORDRE (revue du lot 5, critique) : ce test échoue si
    le refus du modèle part sans la bascule de routes/trust — vérifié en
    rétablissant routes/trust seul. La recette naît liée, en recette, et le
    paiement s'inscrit sur la facture ; aucune bannière d'échec."""
    _seed_trust(fake)
    resp = client.post("/fideicommis/", data=_fee_form())
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    fee = _fee_entry(fake)
    recette = _only_entry(fake)
    assert recette["trust_transaction_id"] == fee["id"]
    assert recette["kind"] == "encaissement_facture"
    assert recette["direction"] == "recette"
    assert recette["invoice_id"] == "inv1"
    assert recette["amount"] == 50000
    assert _ledger(fake) == 50000
    assert fake.peek("invoices/inv1")["amount_paid"] == 50000


def test_le_paiement_sur_facture_papier_cree_une_autre_recette_liee(fake, client):
    _seed_trust(fake)
    resp = client.post("/fideicommis/", data=_fee_form(
        invoice_number="", invoice_external_ref="F-1999-12"))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    assert "avertissement" not in resp.location
    fee = _fee_entry(fake)
    recette = _only_entry(fake)
    assert recette["trust_transaction_id"] == fee["id"]
    assert recette["kind"] == "recette_autre"
    assert recette["direction"] == "recette"
    assert recette["invoice_id"] is None
    assert "F-1999-12" in recette["description"]
    assert fake.peek("invoices/inv1")["amount_paid"] == 0


def test_la_reprise_pose_le_lien_en_mot_cle(fake):
    """Régression de l'ORDRE, côté script : la reprise historique inscrit sa
    recette liée, compensée, et crédite la facture — sur le vrai modèle."""
    from scripts import reprise_encaissements as rep

    fake.seed("invoices/fac1", {
        "id": "fac1", "invoice_number": "2025-F010", "dossier_id": "dos1",
        "dossier_file_number": "2025-001", "dossier_title": "T c. X",
        "status": "envoyée", "total": 50000, "retainer_applied": 0,
        "amount_due": 50000, "amount_paid": 0,
    })
    virement = {
        "id": "ttx1", "date": _d(2026, 9, 3), "amount": 50000, "sequence": 3,
        "client_name": "Jean Tremblay", "dossier_id": "dos1",
        "dossier_file_number": "2025-001", "reference": "",
        "invoice_external_ref": "",
    }
    facture = dict(fake.peek("invoices/fac1"))
    echecs = rep.appliquer("ops1", [{
        "virement": virement, "facture": facture, "mode": "encaissement",
        "montant": 50000, "etat": "à_créer", "ecriture": None,
    }])
    assert echecs == [], echecs
    recette = _only_entry(fake)
    assert recette["trust_transaction_id"] == "ttx1"
    assert recette["kind"] == "encaissement_facture"
    assert recette["direction"] == "recette"
    assert recette["status"] == "compensée"
    assert fake.peek("invoices/fac1")["amount_paid"] == 50000


def _link_calls(tree) -> tuple[list[int], list[int]]:
    """``(offending, keyworded)`` line numbers of the ``create_transaction``
    calls in ``tree``: offending = a literal data dict carrying the
    ``trust_transaction_id`` key, whether the dict is passed positionally or
    as ``data=`` (the model's first parameter is NOT positional-only, so
    both reach it — the first version of this sweep only saw the positional
    form); keyworded = the link passed as the keyword, the one legal way."""
    import ast

    offending, keyworded = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        # The public create AND its prepare phase (lot 5a, step 3): the fee
        # payment composite reaches the link through ``_prepare_create``.
        if name not in ("create_transaction", "_prepare_create"):
            continue
        if any(kw.arg == "trust_transaction_id" for kw in node.keywords):
            keyworded.append(node.lineno)
        data = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "data"), None)
        if isinstance(data, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "trust_transaction_id"
            for k in data.keys
        ):
            offending.append(node.lineno)
    return offending, keyworded


def test_aucun_appelant_ne_glisse_le_lien_dans_les_donnees():
    """Balayage DÉRIVÉ de l'arbre (tests exclus) : aucun appel à
    ``create_transaction`` ne porte la clé ``trust_transaction_id`` dans son
    dictionnaire de données. Le refus du modèle est bruyant, mais côté
    fidéicommis il ne s'affiche qu'en bannière APRÈS le retrait des fonds —
    un nouvel appelant fautif doit tomber ici, avant le déploiement.

    Et les appelants du MOT-CLÉ sont exactement les deux que nomme la
    docstring de `models/admin_ledger.create_transaction` : un troisième
    doit la mettre à jour en même temps
    (preuve, aussi, que le balayage voit de vrais appels).

    Réécrit délibérément au lot 5a (étape 3) : la route du fidéicommis ne
    crée plus la recette après coup — le paiement d'honoraires l'écrit dans
    SA transaction (``models/fee_payment``, par ``_prepare_create``), et le
    balayage suit la phase de préparation autant que la fonction publique."""
    import ast

    offenders, keyworded = [], set()
    for path in sorted(_ATHENA.rglob("*.py")):
        rel = path.relative_to(_ATHENA).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")) or "/site-packages/" in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        bad, good = _link_calls(tree)
        offenders += [f"{rel}:{n}" for n in bad]
        if good:
            keyworded.add(rel)
    assert offenders == []
    # The model's own create composes its phases (it forwards the keyword
    # to _prepare_create): the definition, not a caller.
    keyworded.discard("models/admin_ledger.py")
    assert keyworded == {"models/fee_payment.py", "scripts/reprise_encaissements.py"}


@pytest.mark.parametrize("source, offending", [
    ('al.create_transaction({"trust_transaction_id": t})', True),
    ('al.create_transaction(data={"trust_transaction_id": t})', True),
    ('create_transaction({"account_id": a, "trust_transaction_id": None})', True),
    ('al.create_transaction({"account_id": a}, trust_transaction_id=t)', False),
    ('al.create_transaction(data={"account_id": a}, trust_transaction_id=t)', False),
])
def test_le_balayage_voit_les_deux_formes_du_dictionnaire(source, offending):
    """Régression du balayage lui-même (revue B6) : un dictionnaire passé en
    ``data=`` lui échappait, alors que le modèle l'accepte."""
    import ast

    bad, good = _link_calls(ast.parse(source))
    assert bool(bad) is offending
    assert bool(good) is ("trust_transaction_id=" in source)


# ══════════════════════════════════════════════════════════════════════
# 3. Le contrôle d'intégrité voit un sens qui contredit le type
# ══════════════════════════════════════════════════════════════════════
#
# Une modification web nomme toujours le type, donc elle RE-DÉRIVE le sens :
# une « Autre recette » inscrite en déboursé par un appel direct antérieur au
# lot 0b verrait son signe basculer — et le solde bouger du double de son
# montant — à sa première modification. Le contrôle n° 9 de
# scripts/verify_admin_integrity les nomme AVANT le déploiement (lecture
# seule ; chaque ligne est une décision du juriste).


def _run_integrity(fake, monkeypatch, capsys) -> tuple[int, str]:
    from scripts import verify_admin_integrity as vai

    install(monkeypatch, vai, fake=fake)
    code = vai.main()
    return code, capsys.readouterr().out


def test_le_controle_d_integrite_nomme_un_sens_incoherent(fake, monkeypatch, capsys):
    """Régression (revue B6) — le script ne vérifiait pas le sens contre le
    type : un tel rang passait « ✅ Aucun écart »."""
    fake.seed("admin_transactions/tx1", {
        "id": "tx1", "account_id": "ops1", "sequence": 1,
        "kind": "recette_autre", "direction": "déboursé", "amount": 1000,
        "status": "en_circulation", "date": _d(2026, 9, 1),
        "net_amount": 1000, "gst_amount": 0, "qst_amount": 0,
    })
    fake.seed("counters/admin-ops1", {"seq": 1})
    fake.seed("admin_accounts/ops1", {
        **fake.peek("admin_accounts/ops1"), "ledger_balance": -1000,
    })
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 1
    assert "écriture tx1: type recette_autre inscrit en déboursé" in out
    assert "« recette »" in out


def test_le_controle_d_integrite_accepte_un_registre_coherent(fake, monkeypatch, capsys):
    """Témoin : les écritures du modèle — et une contre-passation, dont le
    type « correction » prend son sens de son propre chemin — ne sont pas
    signalées."""
    depense, errs = al.create_transaction(_data())
    assert errs == [], errs
    recette, errs = al.create_transaction(_recette(amount=25000))
    assert errs == [], errs
    _, errs = al.reverse_transaction(depense["id"], "saisie en double")
    assert errs == [], errs
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 0, out
    assert "Aucun écart" in out


def test_le_controle_d_integrite_note_une_date_qui_porte_une_heure(
    fake, monkeypatch, capsys, tmp_path
):
    """Régression (2026-10-06) — le modèle inscrit toute date à minuit UTC :
    une heure (la console Firestore saisit en heure locale) est la trace
    d'une écriture faite hors de l'application, que le script ne voyait pas.
    Une NOTE — chaque lecteur prend le jour UTC, aucun solde ne bouge —
    qu'une revue peut reconnaître."""
    depense, errs = al.create_transaction(_data())
    assert errs == [], errs
    path = f"admin_transactions/{depense['id']}"
    fake.external_write(path, {**fake.peek(path),
                               "date": datetime(2026, 9, 1, 23, tzinfo=UTC)})
    code, out = _run_integrity(fake, monkeypatch, capsys)
    assert code == 2, out
    line = (f"(écriture {depense['id']}): date de l'écriture enregistrée "
            f"2026-09-01 23:00 UTC (2026-09-01 19:00 à Montréal)")
    assert line in out
    key = re.search(r"\[([0-9a-f]{10})\] [^\n]*" + re.escape(line), out).group(1)

    from scripts import verify_admin_integrity as vai

    review = tmp_path / "revue.json"
    review.write_text(json.dumps(
        [{"cle": key, "revu_le": "2026-10-06", "motif": "Date corrigée par l'avocat."}]
    ), encoding="utf-8")
    code = vai.main(["--revue", str(review)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Constats déjà revus (1)" in out
