"""Tests for utils/invoice_docx.py — the note-d'honoraires context builder."""

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.invoice_docx import build_invoice_context

NBSP = " "
TODAY = date(2026, 1, 15)


def _dt(y, m, d):
    return datetime(y, m, d, 0, 0, tzinfo=timezone.utc)


def _invoice(**overrides):
    base = {
        "invoice_number": "2025-F007",
        "date": _dt(2025, 12, 11),
        "due_date": _dt(2026, 1, 10),
        "client_id": "c1",
        "billing_address": {
            "name": "Jean Tremblay",
            "street": "12 rue Principale",
            "unit": "",
            "city": "Montréal",
            "province": "QC",
            "postal_code": "H2X 1Y6",
        },
        "subtotal_fees": 50000,
        "subtotal_expenses": 15000,
        "subtotal": 65000,
        "gst_rate": 500,
        "gst_amount": 3250,
        "qst_rate": 9975,
        "qst_amount": 6484,
        "total": 74734,
        "retainer_applied": 0,
        "amount_due": 74734,
        "gst_number": "123456789 RT0001",
        "qst_number": "1234567890 TQ0001",
    }
    base.update(overrides)
    return base


def _fee(desc, hours, rate, amount, d=_dt(2025, 12, 1)):
    return {"type": "fee", "date": d, "description": desc, "hours": hours,
            "rate": rate, "amount": amount, "taxable": True}


def _exp(desc, amount, taxable, d=_dt(2025, 12, 2)):
    return {"type": "expense", "date": d, "description": desc, "hours": None,
            "rate": None, "amount": amount, "taxable": taxable}


def _build(invoice, items, **kw):
    defaults = dict(firm={}, destinataire=None, dossier=None, today=TODAY)
    defaults.update(kw)
    return build_invoice_context(invoice, items, **defaults)


# ── Region splitting ────────────────────────────────────────────────────

def test_splits_into_three_regions_in_order():
    items = [
        _fee("Recherche", 2.5, 25000, 62500),
        _exp("Signification", 5000, True),
        _exp("Timbre judiciaire", 3000, False),
    ]
    ctx = _build(_invoice(), items)
    assert [r["h.description"] for r in ctx.rows["ligne_honoraire"]] == ["Recherche"]
    assert [r["d.description"] for r in ctx.rows["ligne_debours_tx"]] == ["Signification"]
    assert [r["d.description"] for r in ctx.rows["ligne_debours_ntx"]] == ["Timbre judiciaire"]


def test_conditions_reflect_empty_and_nonempty_regions():
    items = [_fee("R", 1, 25000, 25000), _exp("S", 5000, True)]  # no non-taxable
    ctx = _build(_invoice(), items)
    assert ctx.conditions == {
        "si_honoraires": True, "si_debours_tx": True, "si_debours_ntx": False,
    }
    empty = _build(_invoice(), [])
    assert empty.conditions == {
        "si_honoraires": False, "si_debours_tx": False, "si_debours_ntx": False,
    }


# ── Subtotal invariant (§6.4) ───────────────────────────────────────────

def test_subtotal_invariant_tx_plus_ntx_equals_expenses():
    items = [_exp("A", 5000, True), _exp("B", 3000, False), _exp("C", 7000, True)]
    inv = _invoice(subtotal_expenses=15000)
    ctx = _build(inv, items)
    tx = sum(e["amount"] for e in items if e["taxable"])
    ntx = sum(e["amount"] for e in items if not e["taxable"])
    assert tx + ntx == inv["subtotal_expenses"]  # exact cents
    assert ctx.values["facture.sous_total_debours_tx"] == f"120,00{NBSP}$"
    assert ctx.values["facture.sous_total_debours_ntx"] == f"30,00{NBSP}$"


# ── Formatting (§7) ─────────────────────────────────────────────────────

def test_scalar_money_date_hours_rate_strings():
    items = [_fee("Recherche", 0.5, 25000, 12500)]
    inv = _invoice(subtotal_fees=115000, subtotal=115000,
                   retainer_applied=115000, amount_due=0)
    ctx = _build(inv, items)
    v = ctx.values
    assert v["facture.numero"] == "2025-F007"
    assert v["facture.date"] == "11 décembre 2025"
    assert v["facture.sous_total_honoraires"] == f"1{NBSP}150,00{NBSP}$"
    assert v["facture.avances_fideicommis"] == f"(1{NBSP}150,00){NBSP}$"
    assert v["facture.solde"] == f"0,00{NBSP}$"
    assert v["facture.nombre_heures"] == "0,50"
    assert v["facture.tps_taux"] == f"5{NBSP}%"
    assert v["facture.tvq_taux"] == f"9,975{NBSP}%"
    # Row-scoped formatting.
    row = ctx.rows["ligne_honoraire"][0]
    assert row["h.date"] == "1er décembre 2025"
    assert row["h.temps"] == "0,50"


def test_taux_horaire_single_vs_mixed_rates():
    same = _build(_invoice(), [_fee("A", 1, 25000, 25000), _fee("B", 2, 25000, 50000)])
    assert same.values["facture.taux_horaire"] == f"250,00{NBSP}$"
    mixed = _build(_invoice(), [_fee("A", 1, 25000, 25000), _fee("B", 1, 30000, 30000)])
    assert mixed.values["facture.taux_horaire"] == ""


# ── Taxes read, never recomputed (§7.2) ─────────────────────────────────

def test_taxes_are_read_not_recomputed():
    # Stored amounts deliberately unrelated to any % of the subtotal.
    inv = _invoice(gst_amount=9999, qst_amount=8888)
    ctx = _build(inv, [_fee("R", 1, 25000, 25000)])
    assert ctx.values["facture.tps_montant"] == f"99,99{NBSP}$"
    assert ctx.values["facture.tvq_montant"] == f"88,88{NBSP}$"


# ── Client fallback to billing_address (§6.6) ───────────────────────────

def test_client_fallback_to_billing_address_when_partie_missing():
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)], destinataire=None)
    assert ctx.values.get("destinataire.nom_complet") == "Jean Tremblay"
    assert "12 rue Principale" in ctx.values.get("destinataire.adresse_complete", "")


def test_live_partie_used_when_provided():
    partie = {
        "type": "individual", "prefix": "M.", "first_name": "Luc",
        "last_name": "Gagnon", "contact_role": "client",
        "address_street": "9 av. du Parc", "address_city": "Laval",
        "address_province": "Québec", "address_postal_code": "H7A 1B2",
    }
    ctx = _build(_invoice(), [_fee("R", 1, 25000, 25000)], destinataire=partie)
    assert ctx.values["destinataire.nom_complet"] == "Luc Gagnon"  # bare
    assert ctx.values["destinataire.nom_complet_avec_civilite"] == "M. Luc Gagnon"


# ── End-to-end: build context → fill a full note template (§13.1–4) ─────

import io
import zipfile
import xml.etree.ElementTree as ET

from utils.docx_fill import extract_placeholders, fill_docx
from utils.template_fields import (
    MANUAL_FIELDS, classify_placeholders, fallback_value,
)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _tc(text):
    return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _tr(*cells):
    return f"<w:tr>{''.join(cells)}</w:tr>"


def _tbl(*rows):
    return f"<w:tbl><w:tblPr/>{''.join(rows)}</w:tbl>"


def _make_docx(body):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _document_xml(docx):
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _assemble(placeholders, ctx):
    """Mirror routes.invoices._assemble_note_values (pure)."""
    classification = classify_placeholders(placeholders)
    values = {}
    for name in placeholders:
        if name in ctx.values:
            values[name] = ctx.values[name]
        elif name in classification.auto:
            values[name] = fallback_value(name, is_auto=True)
        elif name in classification.manual:
            values[name] = MANUAL_FIELDS[name]["default"] or fallback_value(name, is_auto=False)
    return values


def _note_template():
    honoraires = (
        _p("{{?si_honoraires}}")
        + _tbl(
            _tr(_tc("Date"), _tc("Description"), _tc("Temps")),
            _tr(_tc("{{#ligne_honoraire}}{{h.date}}"),
                _tc("{{h.description}}"), _tc("{{h.temps}}")),
        )
        + _p("{{/si_honoraires}}")
    )
    debours_tx = (
        _p("{{?si_debours_tx}}")
        + _tbl(_tr(_tc("{{#ligne_debours_tx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_tx}}")
    )
    debours_ntx = (
        _p("{{?si_debours_ntx}}")
        + _tbl(_tr(_tc("Débours non assujettis {{#ligne_debours_ntx}}{{d.date}}"),
                   _tc("{{d.description}}"), _tc("{{d.cout}}")))
        + _p("{{/si_debours_ntx}}")
    )
    totals = _p(
        "Total avant taxes {{facture.total_avant_taxes}} "
        "TPS {{facture.tps_montant}} TVQ {{facture.tvq_montant}} "
        "Total {{facture.total_apres_taxes}} Solde {{facture.solde}}"
    )
    header = _p("Facture {{facture.numero}} — {{destinataire.nom_complet}}")
    return _make_docx(header + honoraires + debours_tx + debours_ntx + totals)


def test_end_to_end_fill_note_template():
    docx = _note_template()
    items = [
        _fee("Recherche", 2.5, 25000, 62500),
        _fee("Rédaction", 1.0, 25000, 25000),
        _fee("Appel", 0.5, 25000, 12500),
        _exp("Signification", 5000, True),
        # No non-taxable disbursement → that table must be removed entirely.
    ]
    inv = _invoice(subtotal_fees=100000, subtotal_expenses=5000, subtotal=105000,
                   gst_amount=5250, qst_amount=10474, total=120724, amount_due=120724)
    ctx = _build(inv, items)
    placeholders = extract_placeholders(docx)
    values = _assemble(placeholders, ctx)
    out = _document_xml(fill_docx(docx, values,
                                  rows_by_region=ctx.rows, conditions=ctx.conditions))

    ET.fromstring(out)  # acceptance §13.1 — opens (well-formed) without repair

    # §13.2 — N fee rows, M taxable rows, K=0 non-taxable table removed.
    assert out.count("<w:tr>") == 5  # honoraires header + 3 data + 1 débours-tx
    assert "Recherche" in out and "Rédaction" in out and "Appel" in out
    assert "Signification" in out
    # §13.3 — empty table gone (heading, marker and all).
    assert "Débours non assujettis" not in out
    assert "{{#ligne_debours_ntx}}" not in out and "{{?si_debours_ntx}}" not in out
    # Header + totals filled from stored figures (§13.4/5).
    assert "2025-F007" in out and "Jean Tremblay" in out
    assert "52,50" in out and "104,74" in out  # stored gst / qst
    # Nothing left unfilled among the app-filled fields / row fields.
    assert "{{facture." not in out
    assert "{{h." not in out and "{{d." not in out
    assert "{{destinataire.nom_complet}}" not in out
