"""La liste des factures montre le SOLDE, pas le total facturé.

`amount_due` est figé à l'émission et reste à pleine valeur sur une facture
réglée : la liste annonçait donc, pour chaque ligne, une somme qui ne disait
rien de ce qui restait dû. Depuis que les paiements sont inscrits (reprise du
2026-08-17), l'écart est visible sur la moitié du fichier.

Rend le partial SEUL — c'est la cible HTMX des filtres et de la pagination,
donc c'est lui qui doit porter le changement. Ni Flask, ni session, ni
base.html.
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

from utils.format_fr import format_cents_fr
from utils.icons import ms

_TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"
)
UTC = timezone.utc


def _facture(**over) -> dict:
    doc = {
        "id": "inv-1", "invoice_number": "2026-F001",
        "client_name": "M. Jean Tremblay", "dossier_title": "Tremblay c. Lavoie",
        "dossier_file_number": "2026-001", "status": "envoyée",
        "date": datetime(2026, 8, 1, tzinfo=UTC),
        "total": 184535, "amount_due": 184535, "amount_paid": 0,
    }
    doc.update(over)
    # Le route annote ; on reproduit exactement ce contrat.
    from models.invoice import balance_of
    doc["_balance"] = balance_of(doc)
    return doc


def _render(*factures) -> str:
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals.update(
        url_for=lambda endpoint, **kw: f"/{endpoint}",
        csrf_token=lambda: "tok", ms=ms,
        request=type("R", (), {"headers": {}, "args": type(
            "A", (), {"get": lambda s, k, d=None: d})()})(),
    )
    env.filters["cents_fr"] = lambda c: format_cents_fr(c) if c is not None else ""
    return env.get_template("invoices/_invoice_rows.html").render(
        invoices=list(factures),
        status_labels={"envoyée": "Envoyée", "payée": "Payée",
                       "annulée": "Annulée", "en_retard": "En retard"},
        pagination={"page": 1, "pages": 1, "has_prev": False, "has_next": False},
        status_filter="", dossier_id="", date_from="", date_to="",
    )


def test_une_facture_impayee_montre_son_solde_entier():
    """Solde == total : le second chiffre serait une redite, on l'omet."""
    html = _render(_facture())
    assert format_cents_fr(184535) in html
    assert "sur " not in html


def test_une_facture_partiellement_payee_montre_LE_RESTE_du():
    """Le cœur du correctif : 1 845,35 $ facturés, 1 000,00 $ reçus — la ligne
    doit annoncer 845,35 $, pas le total."""
    html = _render(_facture(amount_paid=100000))
    assert format_cents_fr(84535) in html          # le solde, en principal
    assert f"sur {format_cents_fr(184535)}" in html  # le total, en second


def test_une_facture_soldee_montre_zero_et_rappelle_son_total():
    """« 0,00 $ » seul ne dirait plus rien de la facture — d'où le rappel."""
    html = _render(_facture(status="payée", amount_paid=184535))
    assert format_cents_fr(0) in html
    assert f"sur {format_cents_fr(184535)}" in html


def test_une_facture_annulee_ne_doit_RIEN():
    """Son « solde » vaudrait le total et se lirait comme une créance. On
    n'affiche que le montant facturé, en gris."""
    html = _render(_facture(status="annulée"))
    assert "sur " not in html
    assert "text-gray-400" in html


def test_une_facture_heritee_sans_les_champs_ne_leve_pas():
    """`balance_of` tolère l'absence des deux clés ; une soustraction en Jinja
    lèverait. C'est la raison pour laquelle le solde est annoté côté route."""
    doc = {"id": "inv-2", "invoice_number": "2019-014", "client_name": "X",
           "dossier_file_number": "2019-014", "status": "envoyée",
           "date": None, "total": 5000}
    from models.invoice import balance_of
    doc["_balance"] = balance_of(doc)
    html = _render(doc)
    assert "2019-014" in html


def test_le_partial_porte_le_chiffre_car_il_est_la_cible_HTMX():
    """Les filtres et la pagination échangent `#invoice-rows` : un chiffre
    posé dans list.html resterait figé après un filtrage."""
    src = open(os.path.join(_TEMPLATES, "invoices", "_invoice_rows.html"),
               encoding="utf-8").read()
    assert "_balance" in src
    liste = open(os.path.join(_TEMPLATES, "invoices", "list.html"),
                 encoding="utf-8").read()
    assert "cents_fr" not in liste, "une somme a migré hors de la cible HTMX"


def test_aucune_classe_absente_du_css_compile():
    """`tabular-nums` est l'instinct naturel pour aligner des montants — et il
    n'est PAS dans l'artefact compilé, donc il ne ferait rien, en silence."""
    src = open(os.path.join(_TEMPLATES, "invoices", "_invoice_rows.html"),
               encoding="utf-8").read()
    assert "tabular-nums" not in src
