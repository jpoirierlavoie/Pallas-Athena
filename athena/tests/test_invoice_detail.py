"""The invoice detail page is a DATA sheet, not a facsimile of the invoice.

The client-facing document is the Word note d'honoraires (the gabarit owns
its letterhead and layout). This module renders the real template and pins
both halves of that decision: the stored data is all there, and the
document-render is gone — so a later « let's make it look like an invoice
again » edit fails the deploy gate instead of quietly reinstating a second
rendering to keep in step with the Word one.

Renders the ``content`` block alone (Jinja compiles ``{% extends %}`` lazily),
so no Flask app, no base.html, no session is needed.
"""

import os
import sys
from datetime import datetime, timezone

import pytest
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from utils.format_fr import format_cents_fr, format_rate_fr
from utils.icons import ms

TEMPLATE = "invoices/detail.html"
_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)


class _Args:
    def get(self, _key, default=None):
        return default


class _Request:
    args = _Args()


def _invoice(**over) -> dict:
    doc = {
        "id": "inv-1",
        "invoice_number": "2026-001-03",
        "dossier_id": "d-1",
        "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "client_id": "p-1",
        "client_name": "M. Jean Tremblay",
        "billing_address": {
            "name": "M. Jean Tremblay", "street": "12 rue des Érables",
            "unit": "", "city": "Montréal", "province": "QC",
            "postal_code": "H2X 1Y4",
        },
        "date": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "due_date": datetime(2026, 8, 31, tzinfo=timezone.utc),
        "status": "envoyée",
        "subtotal_fees": 150000, "subtotal_expenses": 10500,
        "subtotal": 160500,
        "gst_rate": 500, "gst_amount": 8025,
        "qst_rate": 9975, "qst_amount": 16010,
        "total": 184535,
        "retainer_applied": 0, "amount_due": 184535,
        "amount_paid": 0, "paid_date": None,
        "gst_number": "123456789 RT0001", "qst_number": "1234567890 TQ0001",
        "notes": "Merci de votre confiance.",
        "payment_terms": "Payable dans les 30 jours.",
    }
    doc.update(over)
    return doc


def _render(**over) -> str:
    invoice = _invoice(**over.pop("invoice", {}))
    env = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True)
    env.globals.update(
        url_for=lambda endpoint, **kw: f"/{endpoint}",
        csrf_token=lambda: "tok",
        ms=ms,
        request=_Request(),
    )
    env.filters["cents_fr"] = lambda c: format_cents_fr(c) if c is not None else ""
    ctx = {
        "invoice": invoice,
        "fee_items": [{
            "type": "fee", "date": datetime(2026, 7, 15, tzinfo=timezone.utc),
            "description": "Rédaction de la défense", "hours": 5.0,
            "rate": 30000, "amount": 150000, "taxable": True,
        }],
        "expense_items": [{
            "type": "expense", "date": datetime(2026, 7, 20, tzinfo=timezone.utc),
            "description": "Timbre judiciaire", "amount": 10500,
            "taxable": False,
        }],
        # available_transitions d'une facture « envoyée » depuis le
        # 2026-08-17 : « payée » n'est plus une cible manuelle.
        "transitions": ("en_retard", "annulée"),
        "balance": invoice["amount_due"] - invoice["amount_paid"],
        "gst_rate_display": format_rate_fr(invoice["gst_rate"], 100),
        "qst_rate_display": format_rate_fr(invoice["qst_rate"], 1000),
        "status_labels": {"envoyée": "Envoyée", "payée": "Payée"},
        # Sans cette cle, Jinja leve a l'iteration d'un Undefined et
        # TOUS les tests du fichier tombent, pas seulement ceux du bloc.
        "paiements": [],
        "method_labels": {"virement": "Virement", "cheque": "Chèque"},
        "tx_status_labels": {"compensée": "Compensée",
                             "en_circulation": "En circulation",
                             "annulée": "Annulée"},
        "return_to": "",
    }
    ctx.update(over)
    tpl = env.get_template(TEMPLATE)
    return "".join(tpl.blocks["content"](tpl.new_context(ctx)))


# ── The data is all there ───────────────────────────────────────────────────


def test_renders_the_stored_data():
    html = _render()
    assert "2026-001-03" in html                      # numéro
    assert "M. Jean Tremblay" in html                  # client
    assert "2026-08-01" in html and "2026-08-31" in html   # date + échéance
    assert "Rédaction de la défense" in html           # poste honoraire
    assert "Timbre judiciaire" in html                 # poste débours
    assert "12 rue des Érables" in html                # adresse figée
    assert "123456789 RT0001" in html                  # n° TPS de la facture
    assert "Merci de votre confiance." in html         # notes
    assert "Payable dans les 30 jours." in html        # conditions
    assert "Solde" in html
    assert format_cents_fr(184535) in html             # total, fr-CA


def test_tax_rates_come_from_the_invoice_not_from_hardcoded_markup():
    # An invoice issued under a different rate must read back under it.
    html = _render(
        invoice={"gst_rate": 700, "qst_rate": 8500},
        gst_rate_display=format_rate_fr(700, 100),
        qst_rate_display=format_rate_fr(8500, 1000),
    )
    # Exact match, NBSP included — format_rate_fr is the one formatter.
    assert f"TPS ({format_rate_fr(700, 100)})" in html
    assert f"TVQ ({format_rate_fr(8500, 1000)})" in html
    assert "9,975" not in html      # the statutory rate, not this one


def test_balance_is_labelled_solde_and_amount_due_never_is():
    # amount_due is FROZEN at issuance and stays non-zero on a paid invoice.
    html = _render(
        invoice={"retainer_applied": 50000, "amount_due": 134535,
                 "amount_paid": 134535,
                 "paid_date": datetime(2026, 8, 5, tzinfo=timezone.utc)},
        balance=0,
    )
    assert "Montant dû à l'émission" in html
    assert "Encaissé" in html
    assert "Solde" in html
    assert format_cents_fr(0) in html


def test_the_payment_form_is_gone_and_the_sheet_only_reports():
    """Le formulaire d'encaissement datait du lot P et precedait le module de
    comptabilite : c'etait un second ecrivain de amount_paid, invisible au
    grand livre. La fiche RAPPORTE desormais, elle n'accepte plus."""
    html = _render()
    assert "Encaissement" not in html
    assert "invoice_record_payment" not in html
    assert "Paiements" in html


def test_the_empty_state_says_where_a_payment_is_recorded():
    """Un blanc laisserait le juriste chercher le formulaire disparu."""
    html = _render()
    assert "Aucun paiement inscrit en comptabilité" in html
    assert "Administration" in html


def test_a_draft_is_not_told_to_go_and_record_a_payment():
    html = _render(invoice={"status": "brouillon"})
    assert "Aucun paiement inscrit en comptabilité" in html
    assert "Administration" not in html


def test_the_block_lists_entries_and_keeps_the_reversed_ones():
    """Cacher une contre-passation laisserait le mouvement du solde sans
    explication — c'est pourquoi list_invoice_receipts les garde."""
    html = _render(paiements=[
        {"id": "t1", "date": datetime(2026, 8, 3, tzinfo=timezone.utc),
         "method": "virement", "reference": "VIR-9", "amount": 174347,
         "status": "compensée", "reversed_by_id": ""},
        {"id": "t2", "date": datetime(2026, 8, 9, tzinfo=timezone.utc),
         "method": "cheque", "reference": "CHQ-4", "amount": 50000,
         "status": "annulée", "reversed_by_id": "t3"},
    ])
    assert "VIR-9" in html and "CHQ-4" in html
    assert "Virement" in html and "Chèque" in html
    assert "contre-passée" in html
    assert format_cents_fr(174347) in html


def test_word_button_is_the_client_document_path():
    assert "Note d'honoraires (Word)" in _render()
    # …and it disappears on a voided invoice (nothing to bill).
    assert "Note d'honoraires (Word)" not in _render(
        invoice={"status": "annulée"}
    )


# ── The facsimile is gone ───────────────────────────────────────────────────


@pytest.mark.parametrize("marker", [
    ">FACTURE<",        # the document title
    "data-print",       # the browser-print trigger
    "no-print",         # the print-only visibility classes
    "print-container",
])
def test_document_render_markers_are_gone(marker):
    assert marker not in _render()


def test_template_declares_no_print_stylesheet():
    src = open(
        os.path.join(_TEMPLATES_DIR, "invoices", "detail.html"), encoding="utf-8"
    ).read()
    # Only the explanatory comment may mention it — never a {% block head %}
    # carrying an @media print rule.
    assert "{% block head %}" not in src
    assert "@media print {" not in src


def test_route_context_carries_no_firm_block():
    """The firm letterhead left the page, so the config block that fed it
    left the blueprint — the Word gabarit owns the firm's identity now."""
    from routes import invoices as invoices_routes

    assert "firm" not in invoices_routes._template_context()
    assert not hasattr(invoices_routes, "_firm_info")


def test_the_payment_endpoint_no_longer_exists():
    """Le gabarit ne le pointe plus, mais une route survivante resterait
    atteignable par un signet ou un POST forge — et redeviendrait un second
    ecrivain de amount_paid, invisible au grand livre."""
    from flask import Flask

    from routes import invoices as invoices_routes

    app = Flask(__name__)
    app.register_blueprint(invoices_routes.invoices_bp)
    regles = [str(r) for r in app.url_map.iter_rules()]
    assert not any("paiement" in r for r in regles), regles
    assert not hasattr(invoices_routes, "invoice_record_payment")


def test_the_accounting_module_is_the_only_writer_of_a_payment():
    """record_payment garde sa place — mais routes/admin_ledger en est
    desormais le SEUL appelant SERVI PAR UNE REQUETE. Un balayage de source,
    faute de quoi un futur formulaire pourrait le rebrancher sans que rien ne
    le dise (le patron de test_comptabilite.test_la_route_est_en_lecture_seule).

    L'ensemble est nomme plutot que la portee relachee : un script a lancer a
    la main est un appelant legitime, mais il doit etre DECIDE, pas decouvert.

    Les deux admis sont des outils de reprise, et leur coexistence est sure
    parce qu'ils sont IDEMPOTENTS et convergent : la purge remet a zero tout
    montant que le grand livre n'adosse pas ; la reprise inscrit l'ecriture
    manquante puis re-projette le paiement. Dans un ordre comme dans l'autre,
    le second trouve le travail du premier deja fait et ne le defait pas."""
    import pathlib

    racine = pathlib.Path(__file__).resolve().parent.parent
    appelants = set()
    for chemin in list((racine / "routes").glob("*.py")) + \
            list((racine / "models").glob("*.py")) + \
            list((racine / "mcp").glob("*.py")) + \
            list((racine / "services").glob("*.py")) + \
            list((racine / "scripts").glob("*.py")):
        texte = chemin.read_text(encoding="utf-8")
        if "record_payment(" in texte and chemin.name != "invoice.py":
            appelants.add(chemin.name)
    # Depuis l'audit 2026-08-26 l'orchestration Lot P vit dans
    # services/encaissements.py (routes/admin_ledger et routes/trust
    # l'appellent) — le balayage couvre donc services/ aussi, pour que la
    # portée du pin ne rétrécisse jamais en silence.
    assert appelants == {
        "encaissements.py",
        "purge_encaissements_factures.py",
        "reprise_encaissements.py",
    }, appelants


# ── Le sens du statut s'inverse (2026-08-17) ────────────────────────────


def test_no_template_offers_to_mark_an_invoice_paid_by_hand():
    """La branche morte doit rester morte : laisser le balisage en place est
    la façon dont le prochain lecteur conclut que le bouton existe encore."""
    src = open(
        os.path.join(_TEMPLATES_DIR, "invoices", "detail.html"), encoding="utf-8"
    ).read()
    assert "Marquer comme payée" not in src


def test_a_hand_set_paid_invoice_offers_to_reopen_with_a_confirmation():
    """Le libellé dépend du statut COURANT : sur une facture payée, la cible
    « envoyée » se lit « Rouvrir », jamais « Marquer comme envoyée » — qui se
    lirait comme un renvoi au client."""
    html = _render(
        invoice={"status": "payée", "amount_paid": 0},
        transitions=("envoyée",),
    )
    assert "Rouvrir la facture" in html
    assert "Marquer comme envoyée" not in html
    # La confirmation dit ce que la réouverture NE fait pas.
    assert "data-confirm=" in html
    assert "sans encaissement inscrit en comptabilité" in html


def test_a_ledger_backed_paid_invoice_offers_no_reopen():
    """available_transitions rend () : la fiche ne doit alors afficher aucun
    bouton de statut — un bouton qui s'affiche pour être refusé est un défaut
    de conception, et celui-ci ouvrirait la voie payée → envoyée → annulée,
    qui libérerait les heures d'une facture réellement encaissée."""
    html = _render(
        invoice={"status": "payée", "amount_paid": 184535},
        transitions=(),
    )
    assert "Rouvrir la facture" not in html
    assert "invoice_update_status" not in html


# ── Journal des honoraires : la lecture des postes est payée seulement là
#    où elle sert (audit 2026-08-26) ─────────────────────────────────────


def test_journal_rows_skip_the_line_items_read_on_fee_only_invoices(monkeypatch):
    """expense_split ne fait que tailler la part non taxable HORS du
    subtotal_expenses stocké : a zéro, le partage est (0, 0) quel que soit
    le contenu des postes, donc la lecture de la sous-collection était du
    pur gaspillage sur la ligne la plus courante — sérialisée, non
    plafonnée, sur un export qui grandit pour toujours. Le saut doit rester
    invisible dans la feuille : mêmes colonnes, au cent près."""
    from routes import invoices as invoices_routes

    calls = []

    def _spy(invoice_id):
        calls.append(invoice_id)
        return [
            {"type": "expense", "amount": 4000, "taxable": True},
            {"type": "expense", "amount": 1500, "taxable": False},
        ]

    monkeypatch.setattr(invoices_routes, "list_line_items", _spy)

    fee_only = {
        "id": "f1", "date": datetime(2026, 3, 1, tzinfo=timezone.utc),
        "invoice_number": "2026-F001", "subtotal_fees": 150000,
        "subtotal_expenses": 0, "subtotal": 150000, "gst_amount": 7500,
        "qst_amount": 14963, "total": 172463, "amount_paid": 0,
        "amount_due": 172463, "status": "envoyée",
    }
    with_expenses = dict(
        fee_only, id="f2", invoice_number="2026-F002",
        subtotal_expenses=5500, subtotal=155500,
    )

    rows = invoices_routes._journal_rows([fee_only, with_expenses])

    assert calls == ["f2"]  # the fee-only invoice never pays the read
    assert (rows[0]["debours_tx"], rows[0]["debours_ntx"]) == (0, 0)
    assert (rows[1]["debours_tx"], rows[1]["debours_ntx"]) == (4000, 1500)
    assert rows[1]["debours_tx"] + rows[1]["debours_ntx"] == 5500
