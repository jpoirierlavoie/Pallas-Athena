"""Integration layer of the administration module — routes/admin_ledger.py
and the trust orchestration in routes/trust.py.

Covers: the global unpaid-invoice picker projection, the routes' share of
the atomic payment (lot 5a, step 2: the MODEL writes an encaissement's
payment — and a reversal's reduction — in the entry's own commit, so no
route projects anything any more; the behaviour itself is proved on the
shared fake store in tests/test_admin_payment_atomic.py), the receipt
endpoints' guards (whitelist, size, staging-path ownership, sniff
agreement), the fidéicommis fee-payment wiring (since lot 5a, step 3 ONE
transaction in ``models/fee_payment``, reached through
``services/comptabilite`` — the route orchestrates nothing any more), and
the template pins the house keeps for HTMX/OOB wiring.
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
    import routes.admin_ledger as ra
    import routes.trust as rt

from flask import Flask  # noqa: E402

from tests._accounting_history import FEE_PAYEE  # noqa: E402


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
# Lot P — the routes project NOTHING (rewritten deliberately, lot 5a step 2)
# ═══════════════════════════════════════════════════════════════════════════
#
# The four tests that stood here pinned services/encaissements'
# projeter_paiement / reduire_paiement: *current + delta*, the failure that
# left the entry standing under a banner, the paid_date passed through on a
# reduction, the refusal to clamp a negative result. That module is DELETED:
# the model writes the payment in the entry's own transaction. Each of those
# four properties is now proved on the MODEL against the shared fake store —
# tests/test_admin_payment_atomic.py:
#   * test_le_paiement_s_ajoute_a_celui_qui_est_deja_inscrit (current + delta);
#   * test_un_paiement_que_la_facture_refuse_refuse_l_ecriture (a refused
#     payment now refuses the ENTRY — no « entry stands » state is left);
#   * test_une_reduction_partielle_garde_la_date_du_paiement;
#   * test_un_paiement_inferieur_a_la_contre_passation_refuse_sans_rien_ecrire.
# What stays here is the ROUTE's half: it must not project a second time.


def _trap_payment_writers(monkeypatch):
    """Any payment write outside the model's own transaction fails the test."""
    def _trap(*_a, **_k):
        pytest.fail("la route a écrit un paiement hors de la transaction du modèle")
    monkeypatch.setattr(invoice_model, "record_payment", _trap)
    monkeypatch.setattr(invoice_model, "payment_updates", _trap)


def test_la_route_d_administration_n_importe_plus_la_projection():
    assert not hasattr(ra, "projeter_paiement")
    assert not hasattr(ra, "reduire_paiement")


def test_la_creation_d_un_encaissement_ne_projette_rien(web, monkeypatch):
    _trap_payment_writers(monkeypatch)
    monkeypatch.setattr(
        ra.al, "create_transaction",
        lambda data, **kw: ({"id": "t1", "invoice_id": "fac1", "amount": 60000,
                             "date": datetime(2026, 7, 10, tzinfo=timezone.utc)}, []),
    )
    resp = web.post("/administration/", data={
        "account_id": "ops1", "kind": "encaissement_facture", "amount": "600,00",
        "method": "virement", "counterparty": "Jean Tremblay",
        "invoice_id": "fac1", "date": "2026-07-10",
    })
    assert resp.status_code == 302
    assert resp.location.endswith("/administration/t1")    # no « facture » banner


def test_la_contre_passation_d_un_encaissement_ne_reduit_rien(web, monkeypatch):
    _trap_payment_writers(monkeypatch)
    monkeypatch.setattr(ra.al, "get_transaction",
                        lambda t: {"id": t, "invoice_id": "fac1", "amount": 60000})
    # **kw: the service passes its report channel (lot 5a, step 3).
    monkeypatch.setattr(ra.al, "reverse_transaction",
                        lambda tx_id, reason, reversal_date=None, **kw: ({"id": "rev1"}, []))
    resp = web.post("/administration/t1/contrepasser", data={"reason": "NSF"})
    assert resp.status_code == 302
    assert resp.location.endswith("/administration/rev1")


def test_la_route_d_administration_ecrit_par_le_service_commun():
    """Lot 5a, étape 3 — les écritures du registre passent par
    ``services/comptabilite``, le chemin que le connecteur partagera au lot
    5b : la route n'appelle plus directement les écrivains du modèle (la
    suppression, le reçu et les conciliations restent les siens — aucun
    outil ne les atteindra)."""
    import ast

    source = open(os.path.join(_ATHENA, "routes", "admin_ledger.py"), encoding="utf-8").read()
    writers = {"create_transaction", "update_transaction", "reverse_transaction",
               "clear_transaction", "clear_transactions_bulk", "create_card_payment"}
    reached = {
        node.attr for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in writers
        and isinstance(node.value, ast.Name) and node.value.id == "al"
    }
    assert reached == set(), reached


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


# The exact shape api_televersement mints — staging/{uid}/{uuid4}/{name}.
_STAGING_RECU = "staging/u1/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/recu.pdf"


@pytest.mark.parametrize("objet", [
    # A connector upload ticket's staging object (models/upload_ticket):
    # filing it as a receipt would skip the ticket's size + MD5 check, its
    # expiry and claim_ticket, and consume it under finalize_upload.
    "staging/u1/mcp/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/upload.pdf",
    # A folder-zip export (models/document.build_folder_zip_url).
    "staging/u1/exports/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/dossier.pdf",
    # Third segment not a canonical uuid4.
    "staging/u1/aaaa/recu.pdf",
    "staging/u1/3F2B8C1E-9D4A-4E6B-8F7A-1C2D3E4F5A6B/recu.pdf",
    # No file name.
    "staging/u1/3f2b8c1e-9d4a-4e6b-8f7a-1c2d3e4f5a6b/",
])
def test_recu_refuse_toute_autre_forme_de_staging_sans_toucher_l_objet(
        web, monkeypatch, objet):
    """security-2 — only the shape the receipt's OWN session mints is
    finalizable, and anything else is refused BEFORE the bucket is touched:
    no reload, no rewrite, and above all no delete (consuming a ticket's
    staging object is what made finalize_upload answer « rien reçu »)."""
    monkeypatch.setattr(ra.al, "get_transaction", lambda t: {"id": t})
    bucket = mock.Mock()
    monkeypatch.setattr(ra.storage, "bucket", lambda: bucket)

    reponse = _post(web, "/administration/t1/api/recu", {
        "objet": objet, "name": "recu.pdf",
    })
    assert reponse.status_code == 400
    bucket.blob.assert_not_called()


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
        "objet": _STAGING_RECU, "name": "recu.pdf",
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
        "objet": _STAGING_RECU, "name": "recu.pdf",
    })
    assert reponse.status_code == 200
    assert attached["path"] == "users/u1/administration/t9/recu.pdf"
    assert attached["ct"] == "application/pdf"
    assert dest.content_disposition == "attachment"
    staging.delete.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Fidéicommis → la route n'écrit plus au registre d'administration
# ═══════════════════════════════════════════════════════════════════════════
#
# Rewritten deliberately (lot 5a, step 3). The eight tests that stood here
# pinned routes/trust's two after-commit helpers:
# _creer_recette_administration (the recette minted AFTER the trust commit,
# fail-open — « failure is a banner, never an exception ») and
# _contrepasser_recette_administration (each recette reversed after the
# trust reversal, a refusal or a read failure answering with a banner).
# Both helpers are DELETED: the fee payment writes the trust entry, its
# recette and the invoice's payment in ONE transaction, and reverses them
# the same way (models/fee_payment). The banner doctrine is REVERSED — an
# administration-side refusal now refuses the whole payment, nothing
# written. Every property those tests pinned is proved on the model, on
# the shared fake store, in tests/test_fee_payment.py:
#   * the link travels as a keyword, never in the data (and the
#     sweep test_admin_rules::test_aucun_appelant_ne_glisse_le_lien_… sees
#     the composite's _prepare_create call);
#   * an invoice-backed payment mints an encaissement, an external
#     reference a « recette_autre » citing the paper number;
#   * an unknown / closed / card admin account refuses — before, the
#     trust withdrawal had already committed;
#   * EVERY linked recette is reversed, a split transfer's two included;
#   * an unreadable link REFUSES the reversal (it once meant « nothing to
#     do »), and a refused recette refuses the whole reversal.
# What stays here is the ROUTE's half: it reaches no administration writer.


def test_la_route_du_fideicommis_n_atteint_aucun_ecrivain_d_administration():
    """The trust blueprint writes through services/comptabilite only — a
    helper reborn here would be the fail-open after-commit write again."""
    import ast

    source = open(os.path.join(_ATHENA, "routes", "trust.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    writers = {"create_transaction", "reverse_transaction", "clear_transaction",
               "clear_transactions_bulk", "list_by_trust_transaction"}
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in writers:
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in ("trust", "admin_ledger", "al"):
                reached.add(f"{owner.id}.{node.attr}")
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("admin_ledger"):
            reached.add(f"import {node.module}")
        if isinstance(node, ast.Import) and any(
                a.name.endswith("admin_ledger") for a in node.names):
            reached.add("import admin_ledger")
    assert reached == set(), reached
    assert not hasattr(rt, "_creer_recette_administration")
    assert not hasattr(rt, "_contrepasser_recette_administration")
    assert not hasattr(rt, "_resolve_invoice_number")


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
    # D-4 (2026-08-17) : « Aucune (saisie manuelle) » a disparu. C'était le
    # chemin par lequel un paiement d'honoraires sortait du fidéicommis sans
    # écriture comptable — et la saisie manuelle promise n'avait rien pour la
    # rappeler. La garde vit dans la route ; l'option retirée évite de la
    # heurter par inadvertance.
    assert "Aucune (saisie manuelle)" not in src


def test_trust_form_tells_an_outage_from_an_absence():
    """Doctrine du hub Comptabilité : un état vide pendant une panne invite à
    ouvrir un compte en double. Les deux branches doivent donc être là, et
    seule celle de l'absence porte le lien de création."""
    src = _template("trust/form.html")
    assert "{% elif admin_lisible %}" in src
    assert "indisponibles" in src
    creation = src.index("admin_ledger.account_new")
    panne = src.index("indisponibles")
    assert creation < panne, "le lien « ouvrir un compte » a glissé dans la panne"


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


# ═══════════════════════════════════════════════════════════════════════════
# D-4 : un paiement d'honoraires NOMME son compte d'administration
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def web_trust():
    """Le blueprint fidéicommis seul. Le rendu du gabarit est bouchonné (il
    exige base.html, admin_bp pour ses url_for, et tout le contexte de la
    fiche) : ce qui est mis à l'épreuve ici est la garde, pas la page."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(rt.trust_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


def _form_virement(**over) -> dict:
    payload = {
        "account_id": "acc1", "direction": "déboursé", "amount": "600,00",
        "purpose": "virement_honoraires", "method": "virement",
        # D23 (2026-09-29, art. 58): the firm, as the unseeded profile's seed
        # names it — the form's select offers nothing else.
        "counterparty": FEE_PAYEE, "dossier_id": "dos1", "client_id": "cli1",
        "date": "2026-07-10", "admin_account_id": "ops1",
    }
    payload.update(over)
    return payload


def _bouchonner_rendu(monkeypatch):
    """Rend les erreurs en texte brut — le gabarit n'est pas le sujet."""
    monkeypatch.setattr(
        rt, "render_template",
        lambda _tpl, **ctx: " | ".join(ctx.get("errors") or []),
    )
    monkeypatch.setattr(rt.trust, "list_accounts", lambda **kw: [])
    monkeypatch.setattr(rt, "get_dossier", lambda _d: None)
    monkeypatch.setattr(rt, "_factures_emises", lambda _d=None: [])
    monkeypatch.setattr(rt.comptabilite, "comptes_operations_actifs",
                        lambda: ([{"id": "ops1"}], True))


def test_un_paiement_dhonoraires_sans_compte_est_refuse_avant_toute_ecriture(
    web_trust, monkeypatch
):
    """Sans compte d'administration, les fonds quittent le fidéicommis sans la
    moindre écriture comptable et sans rien inscrire sur la facture — les trois
    virements perdus de juillet 2026.

    Réécrit délibérément au lot 5a (étape 3) : la garde a quitté la route
    pour le MODÈLE (``models/fee_payment``) — le formulaire n'est pas une
    garde, et la route non plus. Le refus tombe avant toute lecture : ni le
    fidéicommis ni le registre d'administration ne sont atteints."""
    _bouchonner_rendu(monkeypatch)
    from models import fee_payment

    monkeypatch.setattr(fee_payment.trust, "_prepare_create",
                        lambda *a, **k: pytest.fail("rien ne doit se préparer"))
    resp = web_trust.post("/fideicommis/", data=_form_virement(admin_account_id=""))

    assert resp.status_code == 400
    assert "compte d'administration" in resp.get_data(as_text=True)


def test_la_garde_ne_vise_que_le_paiement_dhonoraires(web_trust, monkeypatch):
    """Une provision, un déboursé à un tiers : aucun compte d'administration
    n'entre en jeu, et exiger le champ bloquerait des saisies légitimes.
    (Réécrit au lot 5a, étape 3 : l'écriture ordinaire passe par le service,
    qui transmet son canal de rapport — le paiement d'honoraires n'est pas
    atteint.)"""
    _bouchonner_rendu(monkeypatch)
    appels = []

    def _ct(data, **kw):
        appels.append(data)
        return {"id": "t1", **data}, []
    monkeypatch.setattr(rt.trust, "create_transaction", _ct)
    monkeypatch.setattr(rt.comptabilite.fee_payment, "create_fee_payment",
                        lambda *a, **k: pytest.fail("aucun paiement d'honoraires attendu"))

    resp = web_trust.post("/fideicommis/", data=_form_virement(
        purpose="dépôt_client", direction="recette", admin_account_id=""))

    assert resp.status_code == 302
    assert len(appels) == 1


def test_le_paiement_d_honoraires_passe_par_l_operation_unique(
    web_trust, monkeypatch
):
    """Réécrit délibérément au lot 5a (étape 3) — il s'appelait
    « avec un compte, le comportement reste celui du 13 août » : la route
    inscrivait le virement puis la recette. Elle remet maintenant tout au
    service, qui écrit les trois écritures en une transaction ; ce test
    épingle ce qu'elle lui transmet — le compte, la date de dépôt (D16) et
    le chemin de la facture papier, que seul le formulaire web ouvre."""
    _bouchonner_rendu(monkeypatch)
    recus = {}

    def _paiement(data, **kw):
        recus.update(data=data, **kw)
        return {"ok": True, "errors": [], "reason": None, "warnings": [],
                "trust_entry": {"id": "t1"}, "admin_recette": {"id": "a1"},
                "invoice": None, "client_balance": None}
    monkeypatch.setattr(rt.comptabilite, "enregistrer_paiement_honoraires", _paiement)

    resp = web_trust.post("/fideicommis/", data=_form_virement(admin_date="2026-07-12"))

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/fideicommis/t1")
    assert "avertissement" not in resp.headers["Location"]
    assert recus["admin_account_id"] == "ops1"
    assert recus["admin_date"] == datetime(2026, 7, 12, tzinfo=timezone.utc)
    assert recus["allow_external_ref"] is True
    assert "admin_date" not in recus["data"] and "admin_date_raw" not in recus["data"]


def test_un_refus_de_la_recette_refuse_tout_le_paiement(web_trust, monkeypatch):
    """Doctrine RENVERSÉE au lot 5a (étape 3) — ce test s'appelait « un échec
    de la recette reste une bannière » : le virement était COMMIS quand la
    recette échouait, et une bannière demandait de l'inscrire à la main.
    Désormais les trois écritures sont une seule transaction : un refus côté
    administration est un refus de TOUT, rendu au formulaire (400), et rien
    n'est inscrit (preuve sur le vrai magasin : tests/test_fee_payment.py)."""
    _bouchonner_rendu(monkeypatch)
    monkeypatch.setattr(
        rt.comptabilite, "enregistrer_paiement_honoraires",
        lambda data, **kw: {"ok": False, "errors": ["Ce compte d'administration est fermé."],
                            "reason": "compte_administration_fermé", "warnings": [],
                            "trust_entry": None, "admin_recette": None,
                            "invoice": None, "client_balance": None},
    )

    resp = web_trust.post("/fideicommis/", data=_form_virement())

    assert resp.status_code == 400
    assert "Ce compte d'administration est fermé." in resp.get_data(as_text=True)


def test_une_date_de_depot_illisible_est_refusee_avant_toute_ecriture(
    web_trust, monkeypatch
):
    _bouchonner_rendu(monkeypatch)
    monkeypatch.setattr(rt.comptabilite, "enregistrer_paiement_honoraires",
                        lambda *a, **k: pytest.fail("rien ne doit s'inscrire"))
    resp = web_trust.post("/fideicommis/", data=_form_virement(admin_date="31/02/2026"))
    assert resp.status_code == 400
    assert "date du dépôt au compte d'administration est invalide" in resp.get_data(as_text=True)


def test_la_facture_d_un_autre_client_est_refusee_au_formulaire(web_trust, monkeypatch):
    """Réécrit sur la décision D21 (2026-09-29) — ce test s'appelait « un
    avertissement du paiement voyage jusqu'à la fiche » : la facture d'un
    autre client du dossier passait, et son CODE voyageait sur la
    redirection. C'est désormais un REFUS du modèle, rendu au formulaire en
    ligne (le re-rendu), sans redirection ni bandeau — et sans nom (preuve
    sur le vrai magasin et le vrai gabarit :
    tests/test_trust_decisions_2026_09_29.py)."""
    _bouchonner_rendu(monkeypatch)
    message = rt.trust._ABORT_MESSAGES["facture_autre_client"]
    monkeypatch.setattr(
        rt.comptabilite, "enregistrer_paiement_honoraires",
        lambda data, **kw: {"ok": False, "errors": [message],
                            "reason": "facture_autre_client", "warnings": [],
                            "warning_codes": [], "trust_entry": None,
                            "admin_recette": None, "invoice": None,
                            "client_balance": None},
    )
    resp = web_trust.post("/fideicommis/", data=_form_virement())
    assert resp.status_code == 400
    assert message in resp.get_data(as_text=True)
    assert "Location" not in resp.headers


def test_un_avertissement_de_contre_passation_voyage_jusqu_a_la_fiche(web_trust, monkeypatch):
    """Un paiement d'honoraires contre-passé au fidéicommis SEUL (aucune
    recette liée) ou un solde compensé devenu négatif — un manque à combler :
    la route redirigeait sans rien en dire."""
    # Widened deliberately (lot 5b review): the route now hands the service
    # the version its confirmation page described — here none, the form
    # carrying no field (a page rendered before it): nothing is asserted.
    passed: dict = {}

    def _reverse(tx_id, reason, *, expected_etag=None):
        passed["expected_etag"] = expected_etag
        return {"ok": True, "errors": [], "reason": None,
                "warnings": ["…"],
                "warning_codes": ["sans_recette_liee", "solde_compense_negatif"],
                "reversal": {"id": "r1"}}

    monkeypatch.setattr(rt.comptabilite, "contrepasser_ecriture_fideicommis", _reverse)
    resp = web_trust.post("/fideicommis/t1/contrepasser", data={"reason": "erreur"})
    assert resp.status_code == 302
    assert passed == {"expected_etag": None}
    assert "/fideicommis/r1?" in resp.headers["Location"]
    assert "avertissement=sans_recette_liee,solde_compense_negatif" in resp.headers["Location"]


def test_la_fiche_dit_les_avertissements_connus_et_tait_les_autres(web_trust, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(rt, "render_template",
                        lambda tpl, **ctx: captured.update(tpl=tpl, **ctx) or "ok")
    monkeypatch.setattr(rt.trust, "get_transaction", lambda tx_id: {"id": tx_id, "account_id": "acc1"})
    monkeypatch.setattr(rt.trust, "get_account", lambda aid: {"id": aid})
    # « facture_autre_client » was a known code until decision D21
    # (2026-09-29) made the case a refusal: an old link carrying it now
    # shows nothing, like any other unknown code (rewritten deliberately —
    # it was this test's known code).
    resp = web_trust.get("/fideicommis/t1?avertissement=sans_recette_liee,forge,"
                         "<script>,facture_autre_client,sans_recette_liee")
    assert resp.status_code == 200
    assert captured["tpl"] == "trust/detail.html"
    assert captured["avertissements"] == [
        rt.comptabilite.WARNING_MESSAGES["sans_recette_liee"]]
    captured.clear()
    web_trust.get("/fideicommis/t1")
    assert captured["avertissements"] == []
