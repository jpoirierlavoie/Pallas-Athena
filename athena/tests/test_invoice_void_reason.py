"""Ce que le lot 3b demande au modèle de facturation, avant tout outil.

Deux ajouts, tous deux additifs, sur le VRAI modèle au-dessus du faux
Firestore partagé :

1. **Le motif d'une annulation** — ``void_invoice_report(…, reason=)``
   l'enregistre (``void_reason``, avec l'instant ``voided_at``) ; trop long
   ou porteur d'un passage « < … > », il est REFUSÉ avant toute lecture,
   jamais tronqué (``sanitize`` le couperait en silence). L'annulation web,
   qui n'en demande pas, enregistre ``''``.
2. **``update_status_report``** — le corps d'``update_status``, qui rend la
   facture ÉCRITE (son nouvel etag compris) : l'outil du connecteur la rend
   à l'appelant sans la relire. ``update_status`` en reste l'enveloppe
   historique ``(bool, str)``.
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
    from models import invoice as invoice_model

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
WHEN = datetime(2026, 9, 1, tzinfo=UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if n.startswith("models.") and getattr(m, "db", None) is not None]


@pytest.fixture
def store(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("invoices/i1", {
        **invoice_model._default_doc(), "id": "i1",
        "invoice_number": "2026-F031", "dossier_id": "d1",
        "status": "brouillon", "date": WHEN, "due_date": WHEN,
        "subtotal_fees": 45000, "subtotal": 45000, "total": 51739,
        "amount_due": 51739, "created_at": WHEN, "updated_at": WHEN,
        "etag": "i1-e0"})
    fake.seed("invoices/i1/lineitems/li1", {
        "id": "li1", "type": "fee", "source_id": "e1", "date": WHEN,
        "description": "Rédaction", "hours": 1.5, "rate": 30000,
        "amount": 45000, "taxable": True})
    fake.seed("timeentries/e1", {
        "id": "e1", "dossier_id": "d1", "date": WHEN, "hours": 1.5,
        "rate": 30000, "amount": 45000, "billable": True, "invoiced": True,
        "invoice_id": "i1", "etag": "e1-e0"})
    fake.reset_logs()
    return fake


@pytest.mark.parametrize("bad", [
    "x" * (invoice_model.VOID_REASON_MAX_LENGTH + 1), "a <b> c", 42,
], ids=["trop_long", "chevrons", "pas_un_texte"])
def test_a_bad_reason_is_refused_before_anything_is_read(store, bad):
    report, errors = invoice_model.void_invoice_report("i1", reason=bad)
    assert report is None and errors
    assert store.reads == [] and store.commits == []
    assert store.peek("invoices/i1")["status"] == "brouillon"
    assert store.peek("timeentries/e1")["invoiced"] is True


def test_a_reason_is_stored_beside_the_instant_of_the_void(store):
    report, errors = invoice_model.void_invoice_report(
        "i1", expected_etag="i1-e0", reason="  Facturée au mauvais dossier.  ")
    assert errors == []
    stored = store.peek("invoices/i1")
    assert stored["status"] == "annulée"
    assert stored["void_reason"] == "Facturée au mauvais dossier."
    assert stored["voided_at"] is not None
    assert report["invoice"]["void_reason"] == stored["void_reason"]
    assert report["released_time_entry_ids"] == ["e1"]
    assert store.peek("timeentries/e1")["invoiced"] is False


def test_the_web_void_stores_an_empty_reason(store):
    ok, error = invoice_model.void_invoice("i1")
    assert ok, error
    stored = store.peek("invoices/i1")
    assert stored["void_reason"] == "" and stored["voided_at"] is not None


def test_update_status_report_returns_the_invoice_as_written(store):
    written, errors = invoice_model.update_status_report(
        "i1", "envoyée", expected_etag="i1-e0")
    assert errors == []
    stored = store.peek("invoices/i1")
    assert written["etag"] == stored["etag"] != "i1-e0"
    assert written["status"] == stored["status"] == "envoyée"
    (commit,) = store.commits
    assert commit.transaction is not None


def test_update_status_keeps_its_historical_shape(store):
    assert invoice_model.update_status("i1", "annulée") == (
        False, invoice_model.VOID_BY_STATUS_REFUSED)
    ok, error = invoice_model.update_status(
        "i1", "envoyée", expected_etag="perimee")
    assert ok is False and error
    assert store.peek("invoices/i1")["status"] == "brouillon"
    assert invoice_model.update_status("i1", "envoyée") == (True, "")
