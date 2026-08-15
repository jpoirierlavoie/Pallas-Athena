"""Integration layer of the administration module — routes/admin_ledger.py
and the trust orchestration in routes/trust.py.

Covers: the global unpaid-invoice picker projection, the Lot P coexistence
(record_payment stays the SINGLE writer of amount_paid — the admin path
always writes *current + delta*, and the reversal reduces while passing the
existing paid_date through), the receipt endpoints' guards (whitelist, size,
staging-path ownership, sniff agreement), the fidéicommis auto-recette
orchestration (fail-open, never blocking the trust write), and the template
pins the house keeps for HTMX/OOB wiring.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.invoice as invoice_model
    import models.admin_ledger as al
    import routes.admin_ledger as ra
    import routes.trust as rt

from flask import Flask  # noqa: E402


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(ra.admin_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _post(web, path, payload):
    return web.post(path, data=json.dumps(payload),
                    content_type="application/json")


# ═══════════════════════════════════════════════════════════════════════════
# The global unpaid-invoice picker
# ═══════════════════════════════════════════════════════════════════════════


def test_factures_impayees_offers_only_issued_with_a_live_balance(monkeypatch):
    """The select offers exactly what the model's transactional verification
    will accept: issued statuses, balance_of > 0 — and the LIVE balance, not
    the frozen amount_due."""
    invs = {
        "envoyée": [
            {"id": "a", "invoice_number": "2026-F030", "dossier_file_number": "2026-001",
             "amount_due": 100000, "amount_paid": 100000},   # fully paid → out
            {"id": "b", "invoice_number": "2026-F031", "dossier_file_number": "2026-002",
             "amount_due": 100000, "amount_paid": 40000},    # partial → in, solde 600 $
        ],
        "en_retard": [
            {"id": "c", "invoice_number": "2026-F029", "dossier_file_number": "2026-003",
             "amount_due": 50000, "amount_paid": 0},
        ],
    }
    monkeypatch.setattr(
        invoice_model, "list_invoices",
        lambda status_filter=None, **kw: invs.get(status_filter, []),
    )
    rows = ra._factures_impayees()
    assert [r["id"] for r in rows] == ["c", "b"]  # number-sorted
    assert rows[1]["solde_cents"] == 60000


def test_factures_impayees_fails_open_to_empty(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("firestore down")
    monkeypatch.setattr(invoice_model, "list_invoices", _boom)
    assert ra._factures_impayees() == []


# ═══════════════════════════════════════════════════════════════════════════
# Lot P coexistence — record_payment stays the single writer
# ═══════════════════════════════════════════════════════════════════════════


def test_projeter_paiement_writes_current_plus_delta(monkeypatch):
    """record_payment SETS (invoice.py:702) — the admin path re-reads just
    before the call and passes the CUMULATIVE, so a manual correction on the
    invoice side is preserved by later admin recettes."""
    calls = {}
    monkeypatch.setattr(
        invoice_model, "get_invoice",
        lambda iid: {"id": iid, "amount_paid": 40000, "paid_date": None},
    )

    def _rp(iid, amount, paid_date=None):
        calls["args"] = (iid, amount, paid_date)
        return {"id": iid}, []
    monkeypatch.setattr(invoice_model, "record_payment", _rp)

    entry = {"id": "t1", "invoice_id": "fac1", "amount": 60000,
             "date": datetime(2026, 7, 10, tzinfo=timezone.utc)}
    assert ra._projeter_paiement(entry) is True
    assert calls["args"][0] == "fac1"
    assert calls["args"][1] == 100000            # 40000 + 60000, never SET(60000)
    assert calls["args"][2] == entry["date"]


def test_projeter_paiement_failure_leaves_the_entry_standing(monkeypatch):
    monkeypatch.setattr(invoice_model, "get_invoice", lambda iid: None)
    entry = {"id": "t1", "invoice_id": "fac1", "amount": 60000, "date": None}
    assert ra._projeter_paiement(entry) is False  # banner, never an exception


def test_reduire_paiement_passes_the_existing_paid_date_through(monkeypatch):
    """A partial reduction must NOT stamp today — record_payment nulls the
    date itself at zero, and keeps what it is given otherwise."""
    paid_date = datetime(2026, 7, 2, tzinfo=timezone.utc)
    calls = {}
    monkeypatch.setattr(
        invoice_model, "get_invoice",
        lambda iid: {"id": iid, "amount_paid": 100000, "paid_date": paid_date},
    )

    def _rp(iid, amount, paid_date=None):
        calls["args"] = (iid, amount, paid_date)
        return {"id": iid}, []
    monkeypatch.setattr(invoice_model, "record_payment", _rp)

    entry = {"id": "t1", "invoice_id": "fac1", "amount": 60000}
    assert ra._reduire_paiement(entry) is True
    assert calls["args"][1] == 40000
    assert calls["args"][2] == paid_date


def test_reduire_paiement_refuses_a_negative_result_instead_of_clamping(monkeypatch):
    """A max(0, …) clamp here would convert a register/invoice inconsistency
    (a projection that never committed, a manual correction in between) into
    SILENT data loss — other recorded payments erased. Refuse → banner."""
    called = {}
    monkeypatch.setattr(
        invoice_model, "get_invoice",
        lambda iid: {"id": iid, "amount_paid": 30000, "paid_date": None},
    )

    def _rp(iid, amount, paid_date=None):
        called["amount"] = amount
        return {"id": iid}, []
    monkeypatch.setattr(invoice_model, "record_payment", _rp)
    assert ra._reduire_paiement({"id": "t1", "invoice_id": "f", "amount": 60000}) is False
    assert "amount" not in called            # record_payment never touched
    # Exact zero is the legitimate full reversal of the only payment.
    assert ra._reduire_paiement({"id": "t1", "invoice_id": "f", "amount": 30000}) is True
    assert called["amount"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# Receipt endpoints — guards
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("nom", ["releve.docx", "archive.zip", "sans_extension"])
def test_televersement_extension_refusee(web, nom):
    reponse = _post(web, "/administration/api/televersement",
                    {"name": nom, "size": 100})
    assert reponse.status_code == 422


def test_televersement_taille_refusee(web):
    assert _post(web, "/administration/api/televersement",
                 {"name": "recu.pdf", "size": 11 * 1024 * 1024}).status_code == 422
    assert _post(web, "/administration/api/televersement",
                 {"name": "recu.pdf", "size": 0}).status_code == 422


def test_televersement_ouvre_une_session_staging(web, monkeypatch):
    blob = mock.Mock()
    blob.create_resumable_upload_session.return_value = "https://up.example/s1"
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(ra.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/administration/api/televersement",
                    {"name": "recu.pdf", "size": 12345})
    assert reponse.status_code == 200
    donnees = reponse.get_json()
    assert donnees["objet"].startswith("staging/u1/")
    kwargs = blob.create_resumable_upload_session.call_args.kwargs
    assert kwargs["size"] == 12345
    assert kwargs["content_type"] == "application/pdf"
    assert kwargs["origin"]


def test_recu_objet_etranger_400(web):
    assert _post(web, "/administration/t1/api/recu", {
        "objet": "staging/autre-uid/x/recu.pdf", "name": "recu.pdf",
    }).status_code == 400
    assert _post(web, "/administration/t1/api/recu", {
        "objet": "users/u1/administration/t1/recu.pdf", "name": "recu.pdf",
    }).status_code == 400


def test_recu_sniff_mismatch_consomme_le_staging(web, monkeypatch):
    monkeypatch.setattr(ra.al, "get_transaction", lambda t: {"id": t})
    blob = mock.MagicMock()
    blob.size = 1000
    blob.download_as_bytes.return_value = b"PK\x03\x04" + b"\x00" * 100  # un zip
    bucket = mock.Mock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(ra.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/administration/t1/api/recu", {
        "objet": "staging/u1/aaaa/recu.pdf", "name": "recu.pdf",
    })
    assert reponse.status_code == 422
    assert "extension" in reponse.get_json()["erreur"]
    blob.delete.assert_called_once()   # des octets refusés ne restent pas


def test_recu_heureux_reecrit_attache_et_purge(web, monkeypatch):
    monkeypatch.setattr(ra.al, "get_transaction", lambda t: {"id": t})
    attached = {}

    def _attach(tx_id, path, name, ct, size):
        attached.update(dict(tx_id=tx_id, path=path, name=name, ct=ct, size=size))
        return {"_previous_receipt_path": None}, []
    monkeypatch.setattr(ra.al, "attach_receipt", _attach)

    staging = mock.MagicMock()
    staging.size = 1000
    staging.download_as_bytes.return_value = b"%PDF-1.7 " + b"\x00" * 100
    dest = mock.MagicMock()
    dest.rewrite.return_value = (None, 1000, 1000)
    bucket = mock.Mock()
    bucket.blob.side_effect = lambda p: staging if p.startswith("staging/") else dest
    monkeypatch.setattr(ra.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/administration/t9/api/recu", {
        "objet": "staging/u1/aaaa/recu.pdf", "name": "recu.pdf",
    })
    assert reponse.status_code == 200
    assert attached["path"] == "users/u1/administration/t9/recu.pdf"
    assert attached["ct"] == "application/pdf"
    assert dest.content_disposition == "attachment"
    staging.delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Fidéicommis → recette automatique (orchestration route, fail-open)
# ═══════════════════════════════════════════════════════════════════════════


def _virement(**over):
    entry = {
        "id": "ttx1", "amount": 60000, "invoice_id": "fac1",
        "invoice_external_ref": "", "client_name": "Jean Tremblay",
        "dossier_id": "dos1", "dossier_file_number": "2026-001",
        "date": datetime(2026, 7, 10, tzinfo=timezone.utc), "reference": "",
    }
    entry.update(over)
    return entry


def test_creer_recette_administration_invoice_backed(monkeypatch):
    created = {}
    monkeypatch.setattr(rt, "_comptes_administration", lambda: [{"id": "ops1"}])

    def _ct(data):
        created.update(data)
        return {**data, "id": "adm1"}, []
    monkeypatch.setattr(al, "create_transaction", _ct)
    monkeypatch.setattr(ra, "_projeter_paiement", lambda e: True)

    assert rt._creer_recette_administration(_virement(), "ops1") is True
    assert created["kind"] == "encaissement_facture"
    assert created["invoice_id"] == "fac1"
    assert created["trust_transaction_id"] == "ttx1"
    assert created["amount"] == 60000
    assert created["counterparty"] == "Jean Tremblay"


def test_creer_recette_administration_external_ref(monkeypatch):
    created = {}
    monkeypatch.setattr(rt, "_comptes_administration", lambda: [{"id": "ops1"}])

    def _ct(data):
        created.update(data)
        return {**data, "id": "adm1"}, []
    monkeypatch.setattr(al, "create_transaction", _ct)

    virement = _virement(invoice_id=None, invoice_external_ref="F-1999-12")
    assert rt._creer_recette_administration(virement, "ops1") is True
    assert created["kind"] == "recette_autre"
    assert created["invoice_id"] is None
    assert "F-1999-12" in created["description"]


def test_creer_recette_refuses_an_unknown_admin_account(monkeypatch):
    monkeypatch.setattr(rt, "_comptes_administration", lambda: [{"id": "ops1"}])
    assert rt._creer_recette_administration(_virement(), "forgé") is False


def test_creer_recette_failure_is_a_banner_never_an_exception(monkeypatch):
    monkeypatch.setattr(rt, "_comptes_administration", lambda: [{"id": "ops1"}])
    monkeypatch.setattr(al, "create_transaction",
                        lambda data: (None, ["Compte d'administration introuvable."]))
    assert rt._creer_recette_administration(_virement(), "ops1") is False


def test_contrepasser_recette_reverses_and_reduces(monkeypatch):
    recette = {"id": "adm1", "invoice_id": "fac1", "amount": 60000,
               "reversed_by_id": None}
    calls = {}
    monkeypatch.setattr(al, "find_by_trust_transaction", lambda t: recette)

    def _rev(tx_id, reason, reversal_date=None, allow_linked=False):
        calls["reversed"] = (tx_id, allow_linked)
        return {"id": "rev1"}, []
    monkeypatch.setattr(al, "reverse_transaction", _rev)
    monkeypatch.setattr(ra, "_reduire_paiement", lambda e: True)

    assert rt._contrepasser_recette_administration("ttx1", "erreur") is True
    assert calls["reversed"] == ("adm1", True)   # allow_linked — the trust side calls


def test_contrepasser_recette_noop_when_nothing_was_created(monkeypatch):
    monkeypatch.setattr(al, "find_by_trust_transaction", lambda t: None)
    assert rt._contrepasser_recette_administration("ttx1", "x") is True


# ═══════════════════════════════════════════════════════════════════════════
# Template pins — the HTMX/OOB wiring the house keeps honest
# ═══════════════════════════════════════════════════════════════════════════


def _template(name: str) -> str:
    return open(os.path.join(_ATHENA, "templates", name), encoding="utf-8").read()


def test_rows_partial_reemits_the_export_links_oob():
    """The stale-export-URL class of bug (trust, 2026-08-11): the links live
    outside #admin-rows, so the rows partial must re-emit them out-of-band,
    outside the rows/no-rows branches, guarded on HX-Request."""
    src = _template("administration/_transaction_rows.html")
    assert 'hx-swap-oob="true"' in src
    assert 'id="admin-export"' in src
    # The HEADER cards too (revue 2026-08-13): they sit outside #admin-rows,
    # and an account switch would otherwise leave the previous account's
    # money figures above the new account's register.
    assert 'id="admin-header"' in src
    assert "request.headers.get('HX-Request')" in src
    src_list = _template("administration/list.html")
    assert 'id="admin-export"' in src_list
    assert 'id="admin-header"' in src_list
    assert 'hx-target="#admin-rows"' in src_list
    # The account select must carry the filters like every other control —
    # without hx-include, switching accounts silently dropped active filters.
    for line in src_list.split("\n"):
        if 'name="account_id"' in line:
            assert "hx-include" in line
            break
    else:
        raise AssertionError("account select not found")


def test_no_arrow_functions_in_template_attributes():
    """`=>` inside an HTML attribute breaks naive <input[^>]*> tag parsing in
    tests (the 2026-08-12 lesson) — function expressions only."""
    for name in os.listdir(os.path.join(_ATHENA, "templates", "administration")):
        src = _template(f"administration/{name}")
        assert "=>" not in src, name


def test_comptabilite_nav_entry_in_both_duplicated_lists():
    """Depuis la phase 2 de la consolidation (2026-08-15), l'entrée de nav
    comptable est « Comptabilité » → le hub /comptabilite, qui mène aux deux
    modules. Aucun lien /administration ni /fideicommis en dur dans la nav —
    le hub est le seul point d'entrée nav des deux comptabilités."""
    src = _template("base.html")
    assert src.count('href="/comptabilite"') == 2  # sidebar + menu « Plus »
    assert 'href="/administration"' not in src
    assert 'href="/fideicommis"' not in src


def test_form_has_csrf_and_the_ventilation_target():
    src = _template("administration/form.html")
    assert "csrf_token" in src
    assert 'id="ventilation-inputs"' in src
    assert "hx-include=\"#montant-input\"" in src
    # name="q" is load-bearing for hx-include="this" on the dossier picker
    assert 'name="q"' in src


def test_trust_form_offers_the_admin_account_select():
    src = _template("trust/form.html")
    assert 'name="admin_account_id"' in src
    assert "Aucune (saisie manuelle)" in src


# ═══════════════════════════════════════════════════════════════════════════
# Real-render smoke — the 2026-08-13 lesson: pin what the browser RECEIVES,
# never only the template source (a source pin shipped a broken Cancel link).
# ═══════════════════════════════════════════════════════════════════════════


_OPS = {
    "id": "ops1", "name": "Opérations", "account_type": "opérations",
    "status": "actif", "ledger_balance": -11498, "institution": "BNC",
    "account_number_last4": "1234", "transit": "12345", "created_at": None,
    "notes": "",
}


def _entry(**over):
    e = {
        "id": "t1", "account_id": "ops1", "sequence": 3,
        "date": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "direction": "déboursé", "kind": "dépense", "amount": 11498,
        "net_amount": 10000, "gst_amount": 500, "qst_amount": 998,
        "category": "loyer", "counterparty": "Immeubles X", "description": "",
        "reference": "", "supplier_invoice_ref": "F-1", "method": "virement",
        "dossier_id": None, "dossier_file_number": "", "dossier_title": "",
        "invoice_id": None, "invoice_number": "", "trust_transaction_id": None,
        "receipt_storage_path": None, "receipt_filename": "",
        "receipt_file_type": "", "receipt_file_size": 0,
        "status": "en_circulation", "cleared_date": None,
        "reconciliation_id": None, "reverses_id": None, "reversed_by_id": None,
        "related_transaction_id": None, "revisions": [],
    }
    e.update(over)
    return e


@pytest.fixture()
def web_rendu(monkeypatch):
    """App that REALLY renders the templates (the test_document_upload_api
    web_rendu pattern) — base.html only calls url_for for statics."""
    import json as _json

    from markupsafe import Markup

    from utils.format_fr import format_cents_fr
    from utils.icons import ms as _ms

    app = Flask(__name__, template_folder=os.path.join(_ATHENA, "templates"))
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.jinja_env.globals["ms"] = _ms
    app.jinja_env.globals["csrf_token"] = lambda: "jeton-test"
    app.jinja_env.filters["cents_fr"] = format_cents_fr

    def _jsattr(value):  # the main.py filter, verbatim semantics
        js = _json.dumps(str(value), ensure_ascii=False)
        return Markup(
            js.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
        )
    app.jinja_env.filters["jsattr"] = _jsattr
    app.register_blueprint(ra.admin_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def test_rendu_journal_etat_vide(web_rendu, monkeypatch):
    monkeypatch.setattr(ra.al, "list_accounts", lambda status=None: [])
    html = web_rendu.get("/administration/").get_data(as_text=True)
    assert "Aucun compte d'administration" in html
    assert "Créer un premier compte" in html


def test_rendu_journal_avec_solde_courant(web_rendu, monkeypatch):
    monkeypatch.setattr(ra.al, "list_accounts", lambda status=None: [dict(_OPS)])
    monkeypatch.setattr(ra.al, "list_register",
                        lambda aid, df=None, dt=None, limit=10000: ([_entry()], False))
    monkeypatch.setattr(ra.al, "opening_ledger_balance", lambda aid, d: (0, False))
    monkeypatch.setattr(ra.al, "list_outstanding", lambda aid, as_of=None: [])
    monkeypatch.setattr(ra.al, "list_in_transit", lambda aid, as_of=None: [])
    monkeypatch.setattr(ra.al, "list_reconciliations", lambda aid=None: [])
    html = web_rendu.get("/administration/").get_data(as_text=True)
    assert "Immeubles X" in html
    assert "Solde" in html                      # the running-balance column
    assert 'id="admin-export"' in html
    assert html.count('id="admin-export"') == 1  # full render: never the OOB twin


def test_rendu_formulaire_nouvelle(web_rendu, monkeypatch):
    monkeypatch.setattr(ra.al, "list_accounts", lambda status=None: [dict(_OPS)])
    monkeypatch.setattr(ra, "_factures_impayees", lambda: [
        {"id": "f1", "invoice_number": "2026-F031", "dossier": "2026-001",
         "solde_cents": 60000, "solde_fmt": "600,00 $"},
    ])
    html = web_rendu.get("/administration/nouvelle").get_data(as_text=True)
    assert 'name="kind"' in html
    assert "2026-F031" in html
    assert 'id="ventilation-inputs"' in html
    assert "Déjà compensée" in html


def test_rendu_detail_minimal(web_rendu, monkeypatch):
    monkeypatch.setattr(ra.al, "get_transaction", lambda t: _entry())
    monkeypatch.setattr(ra.al, "get_account", lambda a: dict(_OPS))
    monkeypatch.setattr(ra.al, "get_lock_floor", lambda a: None)
    html = web_rendu.get("/administration/t1").get_data(as_text=True)
    assert "Écriture n" in html
    assert "Pièce justificative" in html
    assert "Contre-passer" in html
    assert "Modifier" in html                    # unlocked → editable


def test_rendu_edit_survit_a_une_apostrophe_dans_le_titre(web_rendu, monkeypatch):
    """Le correctif |jsattr (revue 2026-08-13) : un dossier « L'Heureux c. X »
    interpolé cru terminait la chaîne JS du x-data — formulaire mort et,
    sous 'unsafe-eval', injection d'expression Alpine. On épingle le RENDU :
    la valeur voyage en chaîne JSON double-quotée (&quot;), jamais en
    chaîne simple-quotée cassable."""
    entry = _entry(dossier_id="d1", dossier_file_number="2026-004",
                   dossier_title="Succession de L'Heureux")
    monkeypatch.setattr(ra.al, "get_transaction", lambda t: entry)
    monkeypatch.setattr(ra.al, "get_lock_floor", lambda a: None)
    monkeypatch.setattr(ra.al, "list_accounts", lambda status=None: [dict(_OPS)])
    monkeypatch.setattr(
        ra, "get_dossier",
        lambda d: {"id": d, "file_number": "2026-004",
                   "title": "Succession de L'Heureux"},
    )
    html = web_rendu.get("/administration/t1/modifier").get_data(as_text=True)
    assert "dossierDisplay: &quot;2026-004 — Succession de L'Heureux&quot;" in html
    assert "dossierDisplay: '" not in html
