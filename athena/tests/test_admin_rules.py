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
