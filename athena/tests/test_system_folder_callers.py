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

Lot 2A, step T4: the gabarit generation's save moved into
``services/gabarits.save_into_projets`` (the one assembly the popup and the
connector share), so the gabarit tests below patch the SERVICE's seams —
``get_dossier``, ``fill_docx``, ``ensure_system_folder``,
``upload_document`` — where they used to patch the route's. What they pin
is unchanged; the route-level pins over the real store are in
``tests/test_gabarit_service.py``.

Lot 3a, step 2: the note d'honoraires moved the same way, into
``services/note_honoraires.py`` (the one generation the web button and the
connector share) whose save is ``services.gabarits.save_generated``. Its
tests below patch the SERVICE's seams — changed deliberately, the pins
unchanged.

The default folder tree (2026-09-30): the note d'honoraires is filed in
« Mandat › Factures » (role ``factures``), no longer in « Projets » — its
two tests below changed deliberately (the role asked for, the refusal's
reason ``factures_unavailable``); the gabarit generation still asks for
« Projets » (role ``projets``, now under « Interne »), unchanged.
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
    import services.gabarit_champs as champs
    import services.gabarits as sg
    import services.note_honoraires as nh
    from models import folder as folder_model

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

SYSTEM_FOLDER = {"id": "sys-projets", "name": "Projets", "system_role": "projets"}
FACTURES_FOLDER = {"id": "sys-factures", "name": "Factures",
                   "system_role": "factures"}


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
    # The slot and value reads live in the READ half since lot 2A T6
    # (services/gabarit_champs.py); services.gabarits re-exports them, so
    # the patch goes where the functions look their names up.
    monkeypatch.setattr(champs, "get_dossier",
                        lambda did: {"id": "d1", "file_number": "2026-001"})
    monkeypatch.setattr(champs, "cabinet_dict", lambda: {})
    monkeypatch.setattr(dt, "get_template_bytes", lambda tid: b"docx")
    monkeypatch.setattr(sg, "fill_docx", lambda data, values, **kw: b"rempli")
    events: list = []
    monkeypatch.setattr(dt, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_a_generation_without_projets_is_refused_never_saved_at_the_root(
    web, gabarit, monkeypatch,
):
    monkeypatch.setattr(sg, "ensure_system_folder",
                        lambda did, role: (None, [folder_model.READ_ERROR]))
    monkeypatch.setattr(sg, "upload_document",
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

    monkeypatch.setattr(sg, "ensure_system_folder", _ensure)
    monkeypatch.setattr(sg, "upload_document", _upload)

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
    monkeypatch.setattr(sg, "ensure_system_folder",
                        lambda *a: pytest.fail("dossier « Projets » écrit"))
    monkeypatch.setattr(sg, "upload_document",
                        lambda **kw: pytest.fail("document enregistré"))
    reponse = web.post("/gabarits/generer",
                       data={"template_id": "t1", "dossier_id": "d1"},
                       headers={"HX-Request": "true"})
    assert reponse.status_code == 200


# ── Note d'honoraires ─────────────────────────────────────────────────────


@pytest.fixture()
def facture(monkeypatch):
    monkeypatch.setattr(nh, "get_invoice_with_items_strict", lambda iid: ({
        "id": "i1", "status": "envoyée", "dossier_id": "d1", "client_id": "",
        "invoice_number": "2026-F031", "dossier_file_number": "2026-001",
    }, []))
    monkeypatch.setattr(nh, "get_active_template", lambda kind: {
        "id": "t9", "name": "Note", "placeholders": [], "version": 1,
        "kind": kind})
    # The service reads the dossier STRICTLY since the review of lot 3a,
    # step 2 (a failed read refuses) — the seam renamed with it.
    monkeypatch.setattr(nh, "get_dossier_strict",
                        lambda did: {"id": "d1", "file_number": "2026-001"})
    monkeypatch.setattr(nh, "cabinet_dict", lambda: {})
    monkeypatch.setattr(nh, "build_invoice_context", lambda *a, **k: SimpleNamespace(
        values={}, conditions={},
        rows={"ligne_honoraire": [], "ligne_debours_tx": [], "ligne_debours_ntx": []},
    ))
    monkeypatch.setattr(nh, "template_file_bytes", lambda template: b"docx")
    monkeypatch.setattr(nh, "fill_docx", lambda *a, **k: b"rempli")
    events: list = []
    monkeypatch.setattr(nh, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_a_note_without_factures_is_refused_never_saved_at_the_root(
    web, facture, monkeypatch,
):
    roles = []

    def _unavailable(did, role):
        roles.append(role)
        return None, [folder_model.READ_ERROR]

    monkeypatch.setattr(sg, "ensure_system_folder", _unavailable)
    monkeypatch.setattr(sg, "upload_document",
                        lambda **kw: pytest.fail("note enregistrée hors de « Factures »"))

    reponse = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert reponse.status_code == 200
    assert str(escape(folder_model.READ_ERROR)) in reponse.get_data(as_text=True)
    assert roles == [folder_model.SYSTEM_ROLE_FACTURES]
    assert any(e == "generation_failed" and kw.get("reason") == "factures_unavailable"
               for e, kw in facture)


def test_a_note_without_factures_and_no_reason_says_factures(
    web, facture, monkeypatch,
):
    """The folder model gave no reason of its own: the refusal names the
    folder the note needed — « Factures », never « Projets »."""
    monkeypatch.setattr(sg, "ensure_system_folder", lambda did, role: (None, []))
    monkeypatch.setattr(sg, "upload_document",
                        lambda **kw: pytest.fail("note enregistrée hors de « Factures »"))

    reponse = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    body = reponse.get_data(as_text=True)
    assert reponse.status_code == 200
    assert str(escape(sg.FACTURES_UNAVAILABLE)) in body
    assert "Projets" not in body


def test_a_note_files_into_the_system_folder_by_role(web, facture, monkeypatch):
    roles = []
    saved = {}

    def _ensure(did, role):
        roles.append((did, role))
        return FACTURES_FOLDER, []

    def _upload(**kw):
        saved.update(kw)
        return {"id": "doc1", "display_name": "Note"}, []

    monkeypatch.setattr(sg, "ensure_system_folder", _ensure)
    monkeypatch.setattr(sg, "upload_document", _upload)

    reponse = web.post("/factures/i1/note-docx")

    assert reponse.status_code == 302
    assert roles == [("d1", folder_model.SYSTEM_ROLE_FACTURES)]
    assert saved["metadata"]["folder_id"] == "sys-factures"
    # Filed under the SESSION's uid (the web passes request_uid), and
    # linked to its invoice with the fingerprint of what it printed.
    assert saved["user_id"] == "u1"
    assert saved["generated_from_invoice"]["invoice_id"] == "i1"
    assert len(saved["generated_from_invoice"]["fingerprint"]) == 64


# ── Aucun appelant ne connaît plus les dossiers système par leur NOM ──────


def test_a_note_resolves_the_uid_before_writing_the_folder(
    web, facture, monkeypatch,
):
    """Nothing is written — not even « Mandat › Factures » — for a request
    whose uid cannot name a Storage prefix (plan rule 8)."""
    with web.session_transaction() as s:
        s["user_id"] = "unknown"
    monkeypatch.setattr(sg, "ensure_system_folder",
                        lambda *a: pytest.fail("dossier « Factures » écrit"))
    monkeypatch.setattr(sg, "upload_document",
                        lambda **kw: pytest.fail("note enregistrée"))
    reponse = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
    assert reponse.status_code == 200


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
    # The gabarit route reaches « Projets » through the service since T4,
    # the note d'honoraires through the same save since lot 3a (step 2).
    assert sg.ensure_system_folder is folder_model.ensure_system_folder
    assert not hasattr(dt, "ensure_system_folder")
    assert not hasattr(ri, "ensure_system_folder")
    assert nh.save_generated is sg.save_generated
