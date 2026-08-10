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
        "transitions": ("payée", "en_retard", "annulée"),
        "balance": invoice["amount_due"] - invoice["amount_paid"],
        "gst_rate_display": format_rate_fr(invoice["gst_rate"], 100),
        "qst_rate_display": format_rate_fr(invoice["qst_rate"], 1000),
        "status_labels": {"envoyée": "Envoyée", "payée": "Payée"},
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


def test_payment_form_present_when_issued_absent_on_draft():
    assert "Encaissement" in _render()
    assert "Encaissement" not in _render(invoice={"status": "brouillon"})


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
