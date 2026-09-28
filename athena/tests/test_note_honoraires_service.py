"""La note d'honoraires — UNE génération, pour le web et le connecteur
(plan, lot 3a, étape 2).

``services/note_honoraires.py`` est la génération sortie de
``routes/invoices.invoice_note_docx``, qui n'en est plus qu'un adaptateur.
Tout passe ici par les VRAIS modèles, le vrai moteur de remplissage et la
vraie sauvegarde (``services.gabarits.save_generated``) au-dessus du faux
Firestore partagé et du faux Cloud Storage : ce qu'on relit est ce que le
MAGASIN contient.

Quatre familles :

1. **La génération** — la note se classe dans « Projets », sous l'uid
   obtenu par la garde, liée à sa facture par ``source_invoice_id`` et
   l'empreinte de ce qu'elle imprime.
2. **La déduplication** — une note identique (même gabarit, même version,
   mêmes valeurs imprimées) est RENDUE au lieu d'être refaite ; un
   changement de ce que la note imprime, ou une nouvelle version du
   gabarit, en fait une nouvelle ; ``regenerate`` en force une ; un
   changement que la note n'imprime pas (le statut) ne compte pas.
3. **Les refus** — rien n'est écrit, pas même le dossier « Projets » :
   facture annulée, lignes illisibles sous un sous-total non nul (le
   lecteur STRICT), aucun gabarit désigné, désignation illisible, uid
   introuvable, recherche des notes déjà générées illisible.
4. **Le web** — le bouton garde son comportement (une note par clic, sous
   l'uid de la session), et le refus d'une lecture de lignes ratée est DIT.
   Ce dernier test échouait sur la route d'avant (vérifié en la
   rétablissant) : son lecteur avalait la panne et classait une note aux
   tableaux VIDES sous des totaux complets — un document client d'aspect
   achevé.
"""

import ast
import io
import os
import pathlib
import sys
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl
    import models.document as document_model
    import routes.documents as rd
    import routes.invoices as ri
    import services.note_honoraires as nh
    from models import folder as folder_model

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from utils import storage_identity  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
TODAY = date(2026, 9, 28)
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)
TEMPLATE_TEXT = "Note {{facture.numero}} — total {{facture.total_apres_taxes}}"


def _docx(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _invoice(**over) -> dict:
    doc = {
        "id": "i1", "invoice_number": "2026-F031", "status": "envoyée",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "client_id": "p1", "client_name": "Jean Tremblay",
        "billing_address": {"name": "Jean Tremblay", "street": "1 rue A",
                            "unit": "", "city": "Montréal",
                            "province": "Québec", "postal_code": "H1A 1A1"},
        "date": datetime(2026, 9, 1, tzinfo=UTC),
        "due_date": datetime(2026, 10, 1, tzinfo=UTC),
        "subtotal_fees": 45000, "subtotal_expenses": 0, "subtotal": 45000,
        "gst_rate": 500, "gst_amount": 2250, "qst_rate": 9975,
        "qst_amount": 4489, "total": 51739, "retainer_applied": 0,
        "amount_due": 51739, "amount_paid": 0,
        "gst_number": "123456789 RT0001", "qst_number": "1234567890 TQ0001",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC), "etag": "inv-e0",
    }
    doc.update(over)
    return doc


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(nh, "cabinet_dict", lambda: {})
    db.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                            "title": "Tremblay c. Lavoie", "clients": [],
                            "client_ids": [], "status": "actif"})
    db.seed("parties/p1", {"id": "p1", "type": "individual",
                           "contact_role": "client", "first_name": "Jean",
                           "last_name": "Tremblay"})
    db.seed("invoices/i1", _invoice())
    db.seed("invoices/i1/lineitems/li1", {
        "id": "li1", "type": "fee", "source_id": "e1",
        "date": datetime(2026, 8, 20, tzinfo=UTC),
        "description": "Rédaction", "hours": 1.5, "rate": 30000,
        "amount": 45000, "taxable": True,
    })
    data = _docx(TEMPLATE_TEXT)
    template, errors = tpl.create_template(
        io.BytesIO(data), "note.docx", len(data),
        {"name": "Note d'honoraires", "category": "correspondance",
         "kind": "note_honoraires"}, UID)
    assert errors == [], errors
    designated, errors = tpl.set_active_template(
        template["id"], par="juriste", expected_etag=None)
    assert errors == [], errors
    db.reset_logs()
    return db, bucket, designated


@pytest.fixture
def events(monkeypatch):
    seen: list = []
    monkeypatch.setattr(nh, "log_template_event",
                        lambda event, **kw: seen.append((event, kw)))
    return seen


def _generate(**kw):
    kw.setdefault("resolve_uid", lambda: UID)
    kw.setdefault("today", TODAY)
    return nh.generer_note_honoraires("i1", **kw)


def _documents(db) -> dict:
    return db.peek_collection("documents")


def _nothing_written(db, bucket_names_before=None) -> None:
    assert _documents(db) == {}
    assert db.peek_collection(folder_model.COLLECTION) == {}


def _stored_text(bucket, storage_path: str) -> str:
    data = bucket.blob(storage_path).download_as_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ══════════════════════════════════════════════════════════════════════
# 1. La génération
# ══════════════════════════════════════════════════════════════════════


def test_a_note_is_filed_in_projets_linked_to_its_invoice(store, events):
    db, bucket, template = store

    note = _generate()

    assert note.reused is False
    (doc_id, stored), = _documents(db).items()
    assert doc_id == note.document["id"]
    assert stored["source_invoice_id"] == "i1"
    assert stored["generation_fingerprint"] == note.fingerprint
    assert len(note.fingerprint) == 64
    assert stored["storage_path"].startswith(f"users/{UID}/dossiers/d1/")
    assert "unknown" not in stored["storage_path"]
    assert stored["category"] == "correspondance"
    assert stored["tags"] == ["note_honoraires"]
    assert stored["dossier_file_number"] == "2026-001"
    assert stored["genere_depuis"] == "Générée depuis la facture 2026-F031"
    assert stored["display_name"] == (
        "2026-001 - 2026-09-28 - Projet Note d'honoraires 2026-F031")
    folder = db.peek(f"{folder_model.COLLECTION}/{stored['folder_id']}")
    assert folder["system_role"] == folder_model.SYSTEM_ROLE_PROJETS
    text = _stored_text(bucket, stored["storage_path"])
    assert "Note 2026-F031" in text and "517,39" in text
    assert [e for e, _ in events] == ["document_generated"]
    assert events[0][1]["invoice_id"] == "i1"
    assert events[0][1]["source"] == "facture"


def test_the_default_uid_is_the_owners_obtained_by_the_guard(store, monkeypatch):
    """A caller with no browser session (the connector, lot 3b) files under
    the authorized user's uid — never a fallback."""
    db, _bucket, _template = store
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: UID)

    note = nh.generer_note_honoraires("i1", today=TODAY)

    assert db.peek(f"documents/{note.document['id']}")["storage_path"].startswith(
        f"users/{UID}/")


def test_the_note_fills_from_the_version_its_record_names(store, monkeypatch):
    """The bytes filled are those of the template record read — never a
    re-read that could print version N+1 under the fingerprint of N."""
    db, bucket, template = store
    seen = []
    real = nh.template_file_bytes

    def _spy(record):
        seen.append((record["id"], record.get("version")))
        return real(record)

    monkeypatch.setattr(nh, "template_file_bytes", _spy)
    _generate()
    assert seen == [(template["id"], template["version"])]


# ══════════════════════════════════════════════════════════════════════
# 2. La déduplication — sur ce que la note IMPRIME
# ══════════════════════════════════════════════════════════════════════


def test_an_identical_note_is_returned_not_filed_twice(store, events):
    db, bucket, _template = store
    first = _generate()
    objects = len(bucket.objects)
    events.clear()

    again = _generate()

    assert again.reused is True
    assert again.document["id"] == first.document["id"]
    assert again.fingerprint == first.fingerprint
    assert len(_documents(db)) == 1
    assert len(bucket.objects) == objects          # no second file
    assert events == []                             # nothing done, nothing said


def test_regenerate_files_a_new_note(store):
    db, _bucket, _template = store
    first = _generate()

    again = _generate(regenerate=True)

    assert again.reused is False
    assert again.document["id"] != first.document["id"]
    assert len(_documents(db)) == 2


def test_a_change_the_note_prints_makes_a_new_note(store):
    db, _bucket, _template = store
    first = _generate()
    db.external_write("invoices/i1", _invoice(total=60000, amount_due=60000))

    again = _generate()

    assert again.reused is False
    assert again.fingerprint != first.fingerprint
    assert len(_documents(db)) == 2


def test_a_change_the_note_does_not_print_is_not_a_new_note(store):
    """The invoice's status (and its updated_at) moves; the note prints
    neither — the same note is the answer."""
    db, _bucket, _template = store
    first = _generate()
    db.external_write("invoices/i1", _invoice(
        status="en_retard", updated_at=datetime(2026, 9, 20, tzinfo=UTC),
        etag="inv-e1"))

    again = _generate()

    assert again.reused is True and again.document["id"] == first.document["id"]


def test_a_new_template_version_makes_a_new_note(store):
    """A replaced letterhead is a new version — the old note no longer
    matches what the note d'honoraires looks like today."""
    db, _bucket, template = store
    first = _generate()
    data = _docx(TEMPLATE_TEXT + " ")
    _updated, errors, changed = tpl.update_template(
        template["id"], {}, io.BytesIO(data), "note.docx", len(data),
        expected_version=template["version"])
    assert errors == [] and changed

    again = _generate()

    assert again.reused is False
    assert again.fingerprint != first.fingerprint


def test_a_note_of_another_dossier_is_never_the_answer(store):
    db, _bucket, _template = store
    first = _generate()
    moved = {**db.peek(f"documents/{first.document['id']}"), "dossier_id": "d9"}
    db.external_write(f"documents/{first.document['id']}", moved)

    again = _generate()

    assert again.reused is False
    assert len(_documents(db)) == 2


def test_the_fingerprint_covers_template_version_values_rows_and_conditions():
    ctx = SimpleNamespace(rows={"ligne_honoraire": [{"h.date": "1er"}]},
                          conditions={"si_debours": False})
    base = nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"}, ctx)
    assert base == nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"}, ctx)
    for other in (
        nh.fill_fingerprint({"id": "t2", "version": 2}, {"a": "1"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 3}, {"a": "1"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "2"}, ctx),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"},
                            SimpleNamespace(rows={"ligne_honoraire": []},
                                            conditions={"si_debours": False})),
        nh.fill_fingerprint({"id": "t1", "version": 2}, {"a": "1"},
                            SimpleNamespace(rows=ctx.rows,
                                            conditions={"si_debours": True})),
    ):
        assert other != base


# ══════════════════════════════════════════════════════════════════════
# 3. Les refus — rien n'est écrit, pas même « Projets »
# ══════════════════════════════════════════════════════════════════════


def _refused(reason: str, **kw) -> nh.NoteRefusee:
    with pytest.raises(nh.NoteRefusee) as caught:
        _generate(**kw)
    assert caught.value.reason == reason
    return caught.value


def test_a_voided_invoice_is_refused(store, events):
    db, _bucket, _template = store
    db.external_write("invoices/i1", _invoice(status="annulée"))
    refusal = _refused("invoice_voided")
    assert refusal.message == nh.INVOICE_VOIDED
    _nothing_written(db)
    assert events == [("generation_failed", {"reason": "invoice_voided"})]


def test_an_unknown_invoice_is_refused(store):
    db, _bucket, _template = store
    with pytest.raises(nh.NoteRefusee) as caught:
        nh.generer_note_honoraires("absente", resolve_uid=lambda: UID)
    assert caught.value.reason == "invoice_not_found"
    _nothing_written(db)


def test_line_items_missing_under_a_non_zero_subtotal_are_refused(store, events):
    """Absent, not unreadable: the void's own rule (``line_items_missing``)
    — the note would print totals over empty tables."""
    db, _bucket, _template = store
    db.external_delete("invoices/i1/lineitems/li1")
    refusal = _refused("line_items_unreadable")
    assert refusal.message == nh.LINE_ITEMS_UNREADABLE
    _nothing_written(db)


def test_a_zero_invoice_without_lines_is_generated(store):
    db, _bucket, _template = store
    db.external_delete("invoices/i1/lineitems/li1")
    db.external_write("invoices/i1", _invoice(
        subtotal_fees=0, subtotal=0, gst_amount=0, qst_amount=0, total=0,
        amount_due=0))
    note = _generate()
    assert note.reused is False and len(_documents(db)) == 1


def _fail_line_item_reads(db, monkeypatch) -> list:
    """Every query of a ``lineitems`` subcollection fails at the SERVER;
    every other read still works. Returns the list of refused queries —
    a test asserts it is not empty, so the simulation cannot pass by
    failing something else."""
    real = db._fake_server.run_query
    refused: list = []

    def _run_query(request, metadata=None, **kw):
        sq = request["structured_query"]._pb
        if any(f.collection_id == "lineitems" for f in sq.from_):
            refused.append(request["parent"])
            raise RuntimeError("firestore indisponible")
        return real(request, metadata=metadata, **kw)

    monkeypatch.setattr(db._fake_server, "run_query", _run_query)
    return refused


def test_a_failed_line_item_read_is_refused_never_filled_empty(
    store, events, monkeypatch,
):
    db, _bucket, _template = store
    refused = _fail_line_item_reads(db, monkeypatch)
    refusal = _refused("invoice_unreadable")
    assert refusal.message == nh.INVOICE_UNREADABLE
    assert refused                      # the line-item read is what failed
    _nothing_written(db)


def test_no_designated_template_names_the_fix(store, events):
    db, _bucket, template = store
    cleared, errors = tpl.clear_active_template(
        template["id"], expected_etag=None)
    assert errors == [], errors
    refusal = _refused("no_note_template")
    assert refusal.message == nh.NO_ACTIVE_NOTE_HONORAIRES
    _nothing_written(db)
    assert events == [("generation_failed", {"reason": "no_note_template"})]


def test_an_unreadable_designation_says_retry(store, monkeypatch):
    db, _bucket, _template = store

    def unreadable(kind):
        raise tpl.TemplateReadError(kind)

    monkeypatch.setattr(nh, "get_active_template", unreadable)
    refusal = _refused("template_read_failed")
    assert refusal.message == nh.NOTE_TEMPLATE_UNREADABLE
    _nothing_written(db)


def test_an_unobtainable_owner_uid_writes_nothing(store, events, monkeypatch):
    db, _bucket, _template = store

    def _no_owner():
        raise storage_identity.StorageIdentityUnavailable()

    monkeypatch.setattr(storage_identity, "owner_uid", _no_owner)
    with pytest.raises(nh.NoteRefusee) as caught:
        nh.generer_note_honoraires("i1", today=TODAY)
    assert caught.value.reason == "save_failed"
    assert caught.value.message == storage_identity.UNAVAILABLE_MESSAGE
    _nothing_written(db)
    assert events[-1][0] == "generation_failed"


def test_a_placeholder_uid_is_refused_by_the_guard(store):
    db, _bucket, _template = store
    refusal = _refused("save_failed", resolve_uid=lambda: "unknown")
    assert refusal.message == storage_identity.INVALID_UID_MESSAGE
    _nothing_written(db)


def test_an_unreadable_lookup_refuses_rather_than_duplicating(store, monkeypatch):
    db, _bucket, _template = store
    _generate()

    def _boom(invoice_id):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(nh, "find_generated_for_invoice", _boom)
    refusal = _refused("generated_lookup_failed")
    assert refusal.message == nh.GENERATED_LOOKUP_FAILED
    assert len(_documents(db)) == 1


def test_regenerate_skips_the_lookup(store, monkeypatch):
    db, _bucket, _template = store
    monkeypatch.setattr(nh, "find_generated_for_invoice",
                        lambda iid: pytest.fail("lookup read on regenerate"))
    note = _generate(regenerate=True)
    assert note.reused is False


# ══════════════════════════════════════════════════════════════════════
# 4. Le modèle de document : le lien, et la recherche stricte
# ══════════════════════════════════════════════════════════════════════


def test_the_invoice_link_travels_only_by_its_keyword(store):
    """A form (or a caller's metadata) cannot pose it: an unknown metadata
    key is ignored by the whitelist; the keyword refuses a bad shape."""
    db, _bucket, _template = store
    data = _docx("x")
    doc, errors = document_model.upload_document(
        "d1", "2026-001", io.BytesIO(data), "lettre.docx", len(data),
        {"display_name": "Lettre", "source_invoice_id": "i1",
         "generation_fingerprint": "0" * 64}, UID)
    assert errors == [], errors
    stored = db.peek(f"documents/{doc['id']}")
    assert "source_invoice_id" not in stored
    assert "generation_fingerprint" not in stored

    for bad in ({"invoice_id": "i1"}, {"invoice_id": "i1", "fingerprint": "zz"},
                {"invoice_id": "a/b", "fingerprint": "0" * 64},
                {"invoice_id": "i1", "fingerprint": "A" * 64, "x": 1}):
        doc, errors = document_model.upload_document(
            "d1", "2026-001", io.BytesIO(data), "lettre.docx", len(data),
            {"display_name": "Lettre"}, UID, generated_from_invoice=bad)
        assert doc is None
        assert errors == [document_model._GENERATED_FROM_INVOICE_ERROR]


def test_find_generated_for_invoice_propagates_a_read_failure(store, monkeypatch):
    db, _bucket, _template = store

    def _boom(*_a, **_kw):
        raise RuntimeError("firestore indisponible")

    monkeypatch.setattr(db._fake_server, "run_query", _boom)
    with pytest.raises(RuntimeError):
        document_model.find_generated_for_invoice("i1")
    assert document_model.find_generated_for_invoice("a/b") == []


# ══════════════════════════════════════════════════════════════════════
# 5. Le web — même comportement, et le refus DIT
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def web(store):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", csp_nonce="n")
    app.register_blueprint(ri.invoices_bp)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


def test_the_web_button_still_files_one_note_per_click(store, web):
    db, _bucket, _template = store
    for _ in range(2):
        resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Note d&#39;honoraires" in resp.get_data(as_text=True)
    docs = list(_documents(db).values())
    assert len(docs) == 2
    assert all(d["storage_path"].startswith(f"users/{UID}/") for d in docs)
    assert {d["source_invoice_id"] for d in docs} == {"i1"}


def test_the_web_says_a_failed_line_item_read_and_files_nothing(
    store, web, monkeypatch,
):
    """Régression — la route d'avant lisait les lignes par le lecteur qui
    avale la panne, puis classait une note aux tableaux vides sous des
    totaux complets. Elle refuse, et le dit (200 : htmx n'échange qu'un
    2xx)."""
    db, _bucket, _template = store
    refused = _fail_line_item_reads(db, monkeypatch)

    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})

    assert refused                      # the line-item read is what failed

    assert resp.status_code == 200
    assert str(escape(nh.INVOICE_UNREADABLE)) in resp.get_data(as_text=True)
    _nothing_written(db)


# ══════════════════════════════════════════════════════════════════════
# 6. Balayages : une seule génération, sans session
# ══════════════════════════════════════════════════════════════════════


def _called_names(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return names


def test_the_route_no_longer_fills_nor_saves_a_note():
    called = _called_names(_ATHENA / "routes" / "invoices.py")
    for writer in ("fill_docx", "upload_document", "save_generated",
                   "ensure_system_folder", "build_invoice_context",
                   "get_active_template", "get_note_honoraires_template"):
        assert writer not in called, writer
    assert "generer_note_honoraires" in called


def test_the_service_never_reads_the_browser_session():
    tree = ast.parse((_ATHENA / "services" / "note_honoraires.py")
                     .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "session"
        if isinstance(node, ast.Attribute):
            assert node.attr != "session"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [getattr(node, "module", "") or ""] + [a.name for a in node.names]
            assert not any(m.split(".")[0] == "flask" for m in modules)
