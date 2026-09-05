"""GABARITS_PLACEHOLDERS.md is a CONTRACT — pin it against the code.

The document calls itself « a human-readable index of what the fill engine
actually supports » and instructs every author to update it alongside the
catalog. Nothing enforced that, and it drifted: the July-2026 mandate_type
rework renamed all four labels and the `{{dossier.type_mandat}}` row kept
quoting « Transactionnel » / « Consultatif » / « Autre » for fourteen months.
A lawyer reading the reference would have written a procedure around labels
the code cannot produce.

These tests are the enforcement — deliberately DERIVED on both sides, never a
hand-kept inventory (an inventory decays the same way the document did).

Pure: template_fields, invoice_docx and note_docx are all Firestore-free.
"""

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from utils.invoice_docx import build_invoice_context
from utils.note_docx import build_note_context
from utils.template_fields import (
    CATALOG,
    FLAT_ALIASES,
    MANUAL_FIELDS,
    _FEE_TYPE_LABEL,
    _MANDATE_TYPE_LABEL,
    _ROLE_STEMS,
    manual_options,
)

_DOC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "GABARITS_PLACEHOLDERS.md",
)

# `<slot>` is the document's own shorthand for the three partie namespaces.
_TOKEN_RE = re.compile(
    r"\{\{\s*(<slot>\.[A-Za-zÀ-ÿ0-9_.]+|[#?/]?[A-Za-zÀ-ÿ0-9_.]+)\s*\}\}"
)


@pytest.fixture(scope="module")
def doc() -> str:
    with open(_DOC_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def cited(doc) -> set:
    """Every placeholder the document names, with `<slot>` expanded."""
    out = set()
    for name in _TOKEN_RE.findall(doc):
        if name.startswith("<slot>."):
            for slot in ("client", "adverse", "destinataire"):
                out.add(name.replace("<slot>", slot))
        else:
            out.add(name)
    return out


def test_every_catalog_field_is_documented(cited):
    # The accented `…_avec_civilité` twins are auto-registered aliases of their
    # unaccented form and are covered in prose on the same row, not as separate
    # rows — the one deliberate exemption.
    missing = {
        name for name in CATALOG
        if name not in cited and not name.endswith("_avec_civilité")
    }
    assert not missing, f"champs du catalogue absents de la doc : {sorted(missing)}"


def test_every_flat_alias_is_documented(cited):
    missing = set(FLAT_ALIASES) - cited
    assert not missing, f"alias plats absents de la doc : {sorted(missing)}"


def test_every_manual_field_and_every_option_is_documented(doc, cited):
    missing = set(MANUAL_FIELDS) - cited
    assert not missing, f"champs manuels absents de la doc : {sorted(missing)}"
    for name in MANUAL_FIELDS:
        for label, _value in manual_options(name) or []:
            assert label in doc, f"option « {label} » de {name} absente de la doc"


def test_the_role_family_table_matches_the_code_both_ways(cited):
    expected = set()
    for _role, stem in _ROLE_STEMS:
        expected.add(f"dossier.{stem}")
        expected.add(f"dossier.{stem}_avec_adresse")
    documented = {
        n for n in cited
        if n.startswith("dossier.") and n.endswith("_avec_adresse")
    }
    assert documented == {n for n in expected if n.endswith("_avec_adresse")}
    assert not expected - cited, f"famille de rôles incomplète : {sorted(expected - cited)}"


def test_facture_fields_regions_and_conditions_match_the_builder(cited):
    invoice = {
        "invoice_number": "2026-F001",
        "date": datetime.datetime(2026, 4, 25),
        "due_date": datetime.datetime(2026, 5, 25),
        "subtotal_fees": 100, "subtotal_expenses": 50, "subtotal": 150,
        "gst_rate": 500, "gst_amount": 8, "qst_rate": 9975, "qst_amount": 15,
        "total": 173, "retainer_applied": 10, "amount_due": 163,
        "gst_number": "G", "qst_number": "Q", "billing_address": {},
        "client_name": "X", "dossier_id": "d",
    }
    items = [
        {"type": "fee", "date": datetime.datetime(2026, 4, 1), "description": "a",
         "hours": 0.5, "amount": 100, "taxable": True},
        {"type": "expense", "date": datetime.datetime(2026, 4, 2), "description": "b",
         "amount": 30, "taxable": True},
        {"type": "expense", "date": datetime.datetime(2026, 4, 3), "description": "c",
         "amount": 20, "taxable": False},
    ]
    ctx = build_invoice_context(
        invoice, items, firm={}, destinataire=None, dossier=None,
        today=datetime.date(2026, 4, 25),
    )
    produced = {k for k in ctx.values if k.startswith("facture.")}
    documented = {n for n in cited if n.startswith("facture.")}
    assert produced == documented, (
        f"non documentés : {sorted(produced - documented)} ; "
        f"documentés mais jamais produits : {sorted(documented - produced)}"
    )
    for region in ctx.rows:
        assert f"#{region}" in cited, f"région {region} absente de la doc"
    for condition in ctx.conditions:
        assert f"?{condition}" in cited, f"condition {condition} absente de la doc"
    row_fields = {k for rows in ctx.rows.values() for row in rows for k in row}
    assert not row_fields - cited, f"champs de rangée absents : {sorted(row_fields - cited)}"


def test_note_fields_match_the_builder(cited):
    ctx = build_note_context(
        {"title": "T", "category": "stratégie", "content": "x",
         "created_at": datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
         "updated_at": datetime.datetime(2026, 8, 3, tzinfo=datetime.timezone.utc),
         "dossier_id": "", "dossier_file_number": "", "dossier_title": ""},
        dossier=None, firm={}, today=datetime.date(2026, 9, 5),
    )
    produced = {
        k for k in list(ctx.values) + list(getattr(ctx, "rich_values", {}) or {})
        if k.startswith("note.")
    }
    documented = {n for n in cited if n.startswith("note.")}
    assert produced == documented, (
        f"non documentés : {sorted(produced - documented)} ; "
        f"documentés mais jamais produits : {sorted(documented - produced)}"
    )


@pytest.mark.parametrize(
    "placeholder,labels",
    [
        ("dossier.type_mandat", _MANDATE_TYPE_LABEL),
        ("dossier.type_honoraires", _FEE_TYPE_LABEL),
    ],
)
def test_quoted_label_vocabularies_are_the_code_s(doc, placeholder, labels):
    # THE test that would have caught the type_mandat drift. A row quoting a
    # vocabulary must quote the CURRENT one — a stale label reads as a promise
    # the fill engine cannot keep.
    row = re.search(
        r"^\|\s*`\{\{" + re.escape(placeholder) + r"\}\}`\s*\|(.*)$", doc, re.M
    )
    assert row, f"ligne de {placeholder} introuvable dans la doc"
    stale = [value for value in labels.values() if value not in row.group(1)]
    assert not stale, f"{placeholder} : libellés du code absents de la ligne : {stale}"
