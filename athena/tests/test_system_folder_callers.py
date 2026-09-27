"""The three writers into a SYSTEM folder — found by role, never a root fallback.

Lot 2A, step T2 (2026-09-27). « Projets » (gabarit generation, the invoice
note d'honoraires) and « Reçus du portail » (Réception's « Verser », pinned
in ``tests/test_reception.py``) used to go through ``get_or_create_folder``,
which found them BY NAME and answered ``None`` on any failure — and each
caller then saved the document with ``folder_id: None``, i.e. at the dossier
ROOT, reporting success. They now call ``models.folder.ensure_system_folder``
and REFUSE when it cannot give them the folder: nothing is saved, and the
reason is shown (a 200 fragment — htmx only swaps a 2xx).

Each refusal test FAILS on the old code, which saved at the root. The model
itself is pinned over the real client in ``tests/test_folders.py``.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import routes.doc_templates as dt
    import routes.documents as rd
    import routes.invoices as ri
    from models import folder as folder_model

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

SYSTEM_FOLDER = {"id": "sys-projets", "name": "Projets", "system_role": "projets"}


@pytest.fixture()
def web():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(dt.doc_templates_bp)
    app.register_blueprint(ri.invoices_bp)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = "u1"
        s["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return client


# ── Génération depuis un gabarit ───────────────────────────────────────────


@pytest.fixture()
def gabarit(monkeypatch):
    monkeypatch.setattr(dt, "get_template", lambda tid: {
        "id": "t1", "name": "Lettre", "placeholders": [], "category": "autre",
        "version": 1,
    })
    monkeypatch.setattr(dt, "get_dossier",
                        lambda did: {"id": "d1", "file_number": "2026-001"})
    monkeypatch.setattr(dt, "_collect_values", lambda template: ({}, 0))
    monkeypatch.setattr(dt, "get_template_bytes", lambda tid: b"docx")
    monkeypatch.setattr(dt, "fill_docx", lambda data, values: b"rempli")
    events: list = []
    monkeypatch.setattr(dt, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_a_generation_without_projets_is_refused_never_saved_at_the_root(
    web, gabarit, monkeypatch,
):
    monkeypatch.setattr(dt, "ensure_system_folder",
                        lambda did, role: (None, [folder_model.READ_ERROR]))
    monkeypatch.setattr(dt, "upload_document",
                        lambda **kw: pytest.fail("enregistré hors de « Projets »"))

    reponse = web.post("/gabarits/generer",
                       data={"template_id": "t1", "dossier_id": "d1"},
                       headers={"HX-Request": "true"})

    assert reponse.status_code == 200
    assert str(escape(folder_model.READ_ERROR)) in reponse.get_data(as_text=True)
    assert ("generation_failed", {
        "template_id": "t1", "dossier_id": "d1", "reason": "projets_unavailable",
    }) in gabarit


def test_a_generation_files_into_the_system_folder_by_role(web, gabarit, monkeypatch):
    roles = []

    def _ensure(did, role):
        roles.append((did, role))
        return SYSTEM_FOLDER, []

    saved = {}

    def _upload(**kw):
        saved.update(kw)
        return {"id": "doc1"}, []

    monkeypatch.setattr(dt, "ensure_system_folder", _ensure)
    monkeypatch.setattr(dt, "upload_document", _upload)

    reponse = web.post("/gabarits/generer",
                       data={"template_id": "t1", "dossier_id": "d1"})

    assert reponse.status_code == 302
    assert roles == [("d1", folder_model.SYSTEM_ROLE_PROJETS)]
    assert saved["metadata"]["folder_id"] == "sys-projets"
    assert saved["user_id"] == "u1"


def test_a_generation_resolves_the_uid_before_writing_the_folder(
    web, gabarit, monkeypatch,
):
    """Nothing is written — not even « Projets » — for a request whose uid
    cannot name a Storage prefix (plan rule 8)."""
    with web.session_transaction() as s:
        s["user_id"] = "unknown"
    monkeypatch.setattr(dt, "ensure_system_folder",
                        lambda *a: pytest.fail("dossier « Projets » écrit"))
    monkeypatch.setattr(dt, "upload_document",
                        lambda **kw: pytest.fail("document enregistré"))
    reponse = web.post("/gabarits/generer",
                       data={"template_id": "t1", "dossier_id": "d1"},
                       headers={"HX-Request": "true"})
    assert reponse.status_code == 200


# ── Note d'honoraires ─────────────────────────────────────────────────────


@pytest.fixture()
def facture(monkeypatch):
    monkeypatch.setattr(ri, "get_invoice_with_items", lambda iid: ({
        "id": "i1", "status": "envoyée", "dossier_id": "d1", "client_id": "",
        "invoice_number": "2026-F031", "dossier_file_number": "2026-001",
    }, []))
    monkeypatch.setattr(ri, "get_note_honoraires_template",
                        lambda: {"id": "t9", "name": "Note", "placeholders": []})
    monkeypatch.setattr(ri, "get_dossier",
                        lambda did: {"id": "d1", "file_number": "2026-001"})
    monkeypatch.setattr(ri, "cabinet_dict", lambda: {})
    monkeypatch.setattr(ri, "build_invoice_context", lambda *a, **k: SimpleNamespace(
        values={}, conditions={},
        rows={"ligne_honoraire": [], "ligne_debours_tx": [], "ligne_debours_ntx": []},
    ))
    monkeypatch.setattr(ri, "get_template_bytes", lambda tid: b"docx")
    monkeypatch.setattr(ri, "fill_docx", lambda *a, **k: b"rempli")
    events: list = []
    monkeypatch.setattr(ri, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_a_note_without_projets_is_refused_never_saved_at_the_root(
    web, facture, monkeypatch,
):
    monkeypatch.setattr(ri, "ensure_system_folder",
                        lambda did, role: (None, [folder_model.READ_ERROR]))
    monkeypatch.setattr(ri, "upload_document",
                        lambda **kw: pytest.fail("note enregistrée hors de « Projets »"))

    reponse = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert reponse.status_code == 200
    assert str(escape(folder_model.READ_ERROR)) in reponse.get_data(as_text=True)
    assert any(e == "generation_failed" and kw.get("reason") == "projets_unavailable"
               for e, kw in facture)


def test_a_note_files_into_the_system_folder_by_role(web, facture, monkeypatch):
    roles = []
    saved = {}

    def _ensure(did, role):
        roles.append((did, role))
        return SYSTEM_FOLDER, []

    def _upload(**kw):
        saved.update(kw)
        return {"id": "doc1", "display_name": "Note"}, []

    monkeypatch.setattr(ri, "ensure_system_folder", _ensure)
    monkeypatch.setattr(ri, "upload_document", _upload)

    reponse = web.post("/factures/i1/note-docx")

    assert reponse.status_code == 302
    assert roles == [("d1", folder_model.SYSTEM_ROLE_PROJETS)]
    assert saved["metadata"]["folder_id"] == "sys-projets"


# ── Aucun appelant ne connaît plus les dossiers système par leur NOM ──────


def test_no_caller_finds_a_system_folder_by_name_any_more():
    """The three writers reach the system folders by ROLE. A name-based
    lookup (the old ``get_or_create_folder(dossier, GENERATED_FOLDER_NAME)``)
    is exactly what forked « Projets » after a rename — derived sweep over
    routes/ and services/, so a fourth caller cannot reintroduce it."""
    import pathlib

    athena = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for package in ("routes", "services", "mcp"):
        for path in sorted((athena / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "get_or_create_folder(" in text:
                offenders.append(str(path.relative_to(athena)))
    assert offenders == []
    for module in (dt, ri):
        assert module.ensure_system_folder is folder_model.ensure_system_folder
