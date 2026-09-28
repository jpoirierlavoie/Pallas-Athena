"""Le gabarit ACTIF est désigné par le juriste, jamais déduit (lot 2A, T3, D11).

Avant T3, la note d'honoraires et l'impression de note se remplissaient
depuis « le gabarit le plus récemment modifié » de leur type : TOUTE
modification — une retouche de description, bientôt une écriture de Claude —
changeait en silence le papier à en-tête imprimé sur chaque note d'un
client. Désormais un gabarit par type est DÉSIGNÉ (``active_for``), par le
juriste, dans l'application ; aucune récence ne le remplace ; une lecture
impossible n'est jamais lue « aucun n'est désigné ».

Modèle, routes et script de migration passent par le VRAI code au-dessus du
faux Firestore partagé et du faux Cloud Storage réaliste ; on relit le
magasin.
"""

import ast
import io
import os
import pathlib
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
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
    import routes.doc_templates as rdt
    import routes.invoices as ri
    import routes.notes as rn
    import scripts.designer_gabarits_actifs as script
    from models import concurrency

from flask import Flask  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.icons import ms  # noqa: E402

UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
UTC = timezone.utc
_ETAG_INPUT = re.compile(r'name="expected_etag" value="([^"]*)"')

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def docx(text: str) -> bytes:
    body = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        'openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
        f'<w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("word/document.xml", body)
    return buf.getvalue()


@pytest.fixture()
def store(monkeypatch):
    db = install(monkeypatch, tpl)
    bucket = FakeBucket()
    monkeypatch.setattr(tpl.storage, "bucket", lambda: bucket)
    return db, bucket


def _create(name="Note", kind="note_honoraires", text="{{facture.numero}}"):
    data = docx(text)
    doc, errors = tpl.create_template(
        io.BytesIO(data), "note.docx", len(data),
        {"name": name, "category": "autre", "kind": kind}, UID)
    assert errors == [], errors
    return doc["id"]


def _seed(db, tid, *, kind="note_honoraires", updated_at=None, name=None,
          **extra):
    db.seed(f"doc_templates/{tid}", {
        **tpl._default_doc(), "id": tid, "name": name or f"Gabarit {tid}",
        "kind": kind, "category": "autre",
        "storage_path": f"users/{UID}/templates/{tid}/v1/{tid}.docx",
        "updated_at": updated_at or datetime(2026, 1, 1, tzinfo=UTC),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "etag": f"e-{tid}", **extra,
    })


def _holders(db, kind):
    return sorted(i for i, t in db.peek_collection("doc_templates").items()
                  if t.get("active_for") == kind)


# ══════════════════════════════════════════════════════════════════════
# 1. set_active_template / get_active_template
# ══════════════════════════════════════════════════════════════════════


def test_the_designation_fields_are_absent_from_the_default_doc():
    """Absent means « never designated » — a default would designate
    nothing and still read as a decision."""
    assert not set(tpl.ACTIVE_FIELDS) & set(tpl._default_doc())


def test_designating_a_template_makes_it_the_one_its_kind_uses(store):
    db, _ = store
    tid = _create()
    etag = db.peek(f"doc_templates/{tid}")["etag"]
    doc, errors = tpl.set_active_template(
        tid, par="juriste@example.com", expected_etag=etag)
    assert errors == []
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["active_for"] == "note_honoraires"
    assert stored["active_designated_by"] == "juriste@example.com"
    assert stored["active_designated_at"] is not None
    assert stored["etag"] != etag and doc["etag"] == stored["etag"]
    assert tpl.get_active_template("note_honoraires")["id"] == tid
    assert tpl.get_note_honoraires_template()["id"] == tid
    assert tpl.get_note_template() is None


def test_designating_another_clears_the_previous_one(store):
    db, _ = store
    a, b = _create("A"), _create("B")
    tpl.set_active_template(a, par="j", expected_etag=None)
    a_etag = db.peek(f"doc_templates/{a}")["etag"]
    tpl.set_active_template(b, par="j", expected_etag=None)
    assert _holders(db, "note_honoraires") == [b]
    a_doc = db.peek(f"doc_templates/{a}")
    assert not set(tpl.ACTIVE_FIELDS) & set(a_doc)       # really cleared
    assert a_doc["etag"] != a_etag                       # its state changed
    assert tpl.get_active_template("note_honoraires")["id"] == b


def test_each_kind_has_its_own_designation(store):
    db, _ = store
    note_h, note_p = _create("NH"), _create("NP", kind="note")
    tpl.set_active_template(note_h, par="j", expected_etag=None)
    tpl.set_active_template(note_p, par="j", expected_etag=None)
    assert _holders(db, "note_honoraires") == [note_h]
    assert _holders(db, "note") == [note_p]


def test_only_one_template_is_active_even_under_racing_designations(store):
    """Two designations race on a kind with NO designation yet: each
    transaction read every template of the kind; the loser re-runs on the
    winner's commit and clears it — never two holders."""
    db, _ = store
    a, b = _create("A"), _create("B")
    fired = []

    def rival(info):
        if fired or not any(p == f"doc_templates/{a}" for _op, p in info.ops):
            return
        fired.append(1)
        _, errors = tpl.set_active_template(b, par="rival", expected_etag=None)
        assert errors == []

    remove = db.add_commit_hook(rival)
    doc, errors = tpl.set_active_template(a, par="moi", expected_etag=None)
    remove()

    assert fired and errors == []
    assert _holders(db, "note_honoraires") == [a]        # the last to commit wins
    assert not set(tpl.ACTIVE_FIELDS) & set(db.peek(f"doc_templates/{b}"))


def test_a_designation_racing_an_existing_holder_still_leaves_one(store):
    db, _ = store
    a, b, c = _create("A"), _create("B"), _create("C")
    tpl.set_active_template(a, par="j", expected_etag=None)
    fired = []

    def rival(info):
        if fired or not any(p == f"doc_templates/{b}" for _op, p in info.ops):
            return
        fired.append(1)
        tpl.set_active_template(c, par="rival", expected_etag=None)

    remove = db.add_commit_hook(rival)
    tpl.set_active_template(b, par="moi", expected_etag=None)
    remove()
    assert fired
    assert len(_holders(db, "note_honoraires")) == 1


def test_a_stale_designation_writes_nothing(store):
    db, _ = store
    tid = _create()
    before = db.peek(f"doc_templates/{tid}")
    doc, errors = tpl.set_active_template(tid, par="j", expected_etag="périmé")
    assert errors == [concurrency.STALE_ETAG_ERROR]
    assert db.peek(f"doc_templates/{tid}") == before


def test_an_ordinary_gabarit_cannot_be_designated(store):
    db, _ = store
    tid = _create(kind="gabarit")
    doc, errors = tpl.set_active_template(tid, par="j", expected_etag=None)
    assert errors == [tpl.NOT_SPECIAL_ERROR]
    assert "active_for" not in db.peek(f"doc_templates/{tid}")


def test_an_unknown_template_cannot_be_designated(store):
    doc, errors = tpl.set_active_template("inconnu", par="j", expected_etag=None)
    assert errors == [tpl.NOT_FOUND_ERROR]


def test_designating_the_sole_holder_again_writes_nothing(store):
    db, _ = store
    tid = _create()
    tpl.set_active_template(tid, par="j", expected_etag=None)
    before = db.peek(f"doc_templates/{tid}")
    db.reset_logs()
    doc, errors = tpl.set_active_template(tid, par="j", expected_etag=None)
    assert errors == [] and db.peek(f"doc_templates/{tid}") == before
    assert [c for c in db.commits if c.ops] == []


def test_there_is_no_recency_fallback(store):
    """THE regression: a template of the kind exists — the newest — yet
    none is designated, so none is used. The old reader returned it."""
    db, _ = store
    _seed(db, "recent", updated_at=datetime(2026, 9, 1, tzinfo=UTC))
    assert tpl.get_active_template("note_honoraires") is None
    assert tpl.get_note_honoraires_template() is None


def test_an_edit_never_switches_the_active_template(store):
    """The defect D11 names: editing another template of the kind made it
    the newest, hence the one printed. Now the designation holds."""
    db, _ = store
    a, b = _create("A"), _create("B")
    tpl.set_active_template(a, par="j", expected_etag=None)
    got, errors, changed = tpl.update_template(b, {"description": "retouche"})
    assert errors == [] and changed is True
    assert tpl.get_active_template("note_honoraires")["id"] == a


def test_a_read_error_raises_and_is_not_none_designated(store, monkeypatch):
    db, _ = store
    logged = []
    monkeypatch.setattr(tpl, "log_unexpected",
                        lambda msg, **kw: logged.append((msg, kw)))
    broken = mock.Mock()
    broken.collection.return_value.where.return_value.stream.side_effect = (
        RuntimeError("503"))
    monkeypatch.setattr(tpl, "db", broken)
    with pytest.raises(tpl.TemplateReadError):
        tpl.get_active_template("note_honoraires")
    with pytest.raises(tpl.TemplateReadError):
        tpl.get_note_template()
    assert logged and logged[0][0] == "active template read failed"


def test_two_holders_pick_the_latest_designation_and_say_so(store, monkeypatch):
    db, _ = store
    logged = []
    monkeypatch.setattr(tpl, "log_unexpected",
                        lambda msg, **kw: logged.append((msg, kw)))
    _seed(db, "old", active_for="note_honoraires",
          active_designated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(db, "new", active_for="note_honoraires",
          active_designated_at=datetime(2026, 5, 1, tzinfo=UTC))
    assert tpl.get_active_template("note_honoraires")["id"] == "new"
    assert logged[0][0] == "several templates designated active for one kind"
    assert logged[0][1] == {"exc_info": False, "kind": "note_honoraires",
                            "count": 2}


def test_a_designation_whose_kind_no_longer_matches_is_ignored(store):
    db, _ = store
    _seed(db, "t", kind="gabarit", active_for="note_honoraires")
    assert tpl.get_active_template("note_honoraires") is None


def test_get_active_template_only_knows_the_special_kinds(store):
    with pytest.raises(ValueError):
        tpl.get_active_template("gabarit")


# ══════════════════════════════════════════════════════════════════════
# 2. Balayage : seul le juriste désigne
# ══════════════════════════════════════════════════════════════════════


def _calls_of(name: str) -> dict[str, int]:
    found = {}
    for path in _ATHENA.rglob("*.py"):
        rel = path.relative_to(_ATHENA).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        n = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", ""))
                == name)
        if n:
            found[rel] = n
    return found


def test_only_the_web_route_and_the_migration_script_designate():
    """D11: « Claude never switches it ». The connector — and any service
    it could reach — never names the function at all."""
    assert _calls_of("set_active_template") == {
        "routes/doc_templates.py": 1,
        "scripts/designer_gabarits_actifs.py": 1,
    }
    # The disclosure registry is the ONE exception, since lot 2A T9: its
    # NEVER « active_template » names the function as a STRING, in order to
    # forbid it — tests/test_mcp_disclosure sweeps every connector module's
    # syntax tree (and every service it reaches) for a reference to it, and
    # excludes the registry for the same reason.
    for package in ("mcp", "services"):
        for path in (_ATHENA / package).rglob("*.py"):
            if path.relative_to(_ATHENA).as_posix() == "mcp/disclosure.py":
                continue
            assert "set_active_template" not in path.read_text(encoding="utf-8"), path


def test_no_reader_selects_by_recency_any_more():
    """``recency_winner`` survives for the migration script ONLY."""
    assert _calls_of("recency_winner") == {"scripts/designer_gabarits_actifs.py": 1}
    source = (_ATHENA / "models" / "doc_template.py").read_text(encoding="utf-8")
    assert "_latest_template_of_kind" not in source


# ══════════════════════════════════════════════════════════════════════
# 3. Les générateurs : aucun désigné ≠ lecture impossible
# ══════════════════════════════════════════════════════════════════════


def _app(*blueprints):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms, csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl)
    for bp in blueprints:
        app.register_blueprint(bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = UID
        s["email"] = "juriste@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return client


@pytest.fixture()
def invoice_web(monkeypatch):
    monkeypatch.setattr(ri, "get_invoice_with_items", lambda iid: ({
        "id": "i1", "status": "envoyée", "dossier_id": "d1", "client_id": "",
        "invoice_number": "2026-F031",
    }, []))
    monkeypatch.setattr(ri, "get_template_bytes",
                        lambda tid: pytest.fail("no template may be filled"))
    events = []
    monkeypatch.setattr(ri, "log_template_event",
                        lambda event, **kw: events.append((event, kw)))
    return _app(ri.invoices_bp), events


def test_the_invoice_note_names_the_fix_when_none_is_designated(
    invoice_web, monkeypatch
):
    web, events = invoice_web
    monkeypatch.setattr(ri, "get_note_honoraires_template", lambda: None)
    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "n&#39;est désigné comme actif" in html
    assert "désignez-en un dans Gabarits" in html
    assert events == [("generation_failed", {"reason": "no_note_template"})]


def test_the_invoice_note_says_retry_when_the_designation_is_unreadable(
    invoice_web, monkeypatch
):
    web, events = invoice_web

    def unreadable():
        raise tpl.TemplateReadError("note_honoraires")

    monkeypatch.setattr(ri, "get_note_honoraires_template", unreadable)
    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "lecture impossible" in html and "réessayez" in html
    assert "désignez-en un" not in html      # never « designate one »
    assert events == [("generation_failed", {"reason": "template_read_failed"})]


def test_the_invoice_route_reads_the_real_designation(store, invoice_web):
    """End to end on the store: a special template exists, undesignated →
    the route refuses (no recency fallback reached it)."""
    db, _ = store
    web, events = invoice_web
    _seed(db, "recent", updated_at=datetime(2026, 9, 1, tzinfo=UTC))
    resp = web.post("/factures/i1/note-docx", headers={"HX-Request": "true"})
    assert "désignez-en un dans Gabarits" in resp.get_data(as_text=True)


@pytest.fixture()
def note_web(monkeypatch):
    monkeypatch.setattr(rn, "get_note", lambda nid: {
        "id": "n1", "title": "Note", "dossier_id": "", "content": "x"})
    return _app(rn.notes_bp)


@pytest.mark.parametrize("answer, code", [
    (lambda: None, "aucun_gabarit"),
    ("raise", "lecture_impossible"),
])
def test_the_note_print_distinguishes_none_from_unreadable(
    note_web, monkeypatch, answer, code
):
    import models.doc_template as model

    if answer == "raise":
        def answer():
            raise model.TemplateReadError("note")

    monkeypatch.setattr(model, "get_note_template", answer)
    monkeypatch.setattr(model, "get_template_bytes",
                        lambda tid: pytest.fail("no template may be filled"))
    resp = note_web.post("/notes/n1/gabarit-docx")
    assert resp.status_code == 302
    assert f"gabarit_erreur={code}" in resp.headers["Location"]
    assert code in rn._GABARIT_ERRORS
    if code == "aucun_gabarit":
        assert "désigné comme actif" in rn._GABARIT_ERRORS[code]
    else:
        assert "lecture impossible" in rn._GABARIT_ERRORS[code]


# ══════════════════════════════════════════════════════════════════════
# 4. Les écrans : désigner, rétablir, le badge
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def web(store):
    return _app(rdt.doc_templates_bp)


def test_the_detail_page_offers_the_designation_with_the_version_shown(
    store, web
):
    db, _ = store
    tid = _create()
    html = web.get(f"/gabarits/{tid}").get_data(as_text=True)
    assert "Désigner comme gabarit actif" in html
    assert "Aucun gabarit « Note d&#39;honoraires » n'est désigné" in html
    assert db.peek(f"doc_templates/{tid}")["etag"] in _ETAG_INPUT.findall(html)
    assert ">Actif<" not in html


def test_designating_from_the_page_designates_and_says_so(store, web):
    db, _ = store
    tid = _create()
    etag = db.peek(f"doc_templates/{tid}")["etag"]
    resp = web.post(f"/gabarits/{tid}/activer", data={"expected_etag": etag})
    assert resp.status_code == 302
    assert _holders(db, "note_honoraires") == [tid]
    assert (db.peek(f"doc_templates/{tid}")["active_designated_by"]
            == "juriste@example.com")
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "est désormais le gabarit actif" in page
    assert ">Actif<" in page
    assert "Désigner comme gabarit actif" not in page


def test_a_stale_designation_button_designates_nothing_and_says_why(store, web):
    db, _ = store
    tid = _create()
    resp = web.post(f"/gabarits/{tid}/activer",
                    data={"expected_etag": "11111111-2222-4333-8444-555555555555"})
    assert resp.status_code == 302
    assert _holders(db, "note_honoraires") == []
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Rien n&#39;a été désigné" in page


def test_a_double_tap_on_designate_does_not_say_designate_it_again(store, web):
    """T3 review: the second POST of a double tap carries the SAME etag,
    now stale. The page it lands on shows « Actif » and no button — the
    banner must not tell the lawyer to designate it again."""
    db, _ = store
    tid = _create()
    etag = db.peek(f"doc_templates/{tid}")["etag"]
    first = web.post(f"/gabarits/{tid}/activer", data={"expected_etag": etag})
    assert "message=" in first.headers["Location"]
    second = web.post(f"/gabarits/{tid}/activer", data={"expected_etag": etag})
    assert second.status_code == 302
    assert "erreur=" not in second.headers["Location"]
    page = web.get(second.headers["Location"]).get_data(as_text=True)
    assert "déjà le gabarit actif" in page and "rien n&#39;a été modifié" in page
    assert "désignez-le de nouveau" not in page
    assert _holders(db, "note_honoraires") == [tid]


def test_a_failed_delete_says_so_on_the_page(store, web):
    """T3 review: the detail page's delete is a plain POST; a refusal used
    to redirect to the page with no word — a failed delete read as a dialog
    that had merely closed."""
    db, bucket = store
    tid = _create()

    def boom(info):
        if any(op == "delete" and p == f"doc_templates/{tid}"
               for op, p in info.ops):
            raise RuntimeError("commit refusé")

    remove = db.add_commit_hook(boom)
    resp = web.post(f"/gabarits/{tid}/delete")
    remove()
    assert resp.status_code == 302
    assert db.peek(f"doc_templates/{tid}") is not None
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Erreur lors de la suppression" in page
    assert 'role="alert"' in page


@pytest.mark.parametrize("url", [
    "/gabarits/{tid}/download",
    "/gabarits/{tid}/versions/1/download",
])
def test_a_download_that_cannot_be_signed_says_so_on_the_page(
    store, web, monkeypatch, url
):
    """T3 review: a signing failure bounced back to the page silently (the
    current file) or claimed the file « introuvable » (a version)."""
    db, _ = store
    tid = _create()
    monkeypatch.setattr(tpl, "_signed_download_url", lambda *a, **k: None)
    resp = web.get(url.format(tid=tid))
    assert resp.status_code == 302
    assert "erreur=" in resp.headers["Location"]
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "n&#39;a pas pu être téléchargé" in page
    assert "le lien de téléchargement n&#39;a pas pu être créé" in page


def test_the_page_names_the_template_the_kind_uses_now(store, web):
    db, _ = store
    a, b = _create("Papier A"), _create("Papier B")
    tpl.set_active_template(a, par="j", expected_etag=None)
    html = web.get(f"/gabarits/{b}").get_data(as_text=True)
    assert "utilisent « Papier A »" in html


def test_an_unreadable_designation_is_never_shown_as_none(store, web, monkeypatch):
    db, _ = store
    tid = _create()

    def unreadable(kind):
        raise tpl.TemplateReadError(kind)

    monkeypatch.setattr(rdt, "get_active_template", unreadable)
    html = web.get(f"/gabarits/{tid}").get_data(as_text=True)
    assert "lecture impossible" in html
    assert "n&#39;est désigné comme actif" not in html


def test_the_list_marks_the_active_template(store, web):
    db, _ = store
    a, b = _create("A"), _create("B")
    tpl.set_active_template(a, par="j", expected_etag=None)
    html = web.get("/gabarits/").get_data(as_text=True)
    assert html.count(">Actif<") == 1
    row_a = html[html.index(f"/gabarits/{a}"):]
    assert row_a.index(">Actif<") < row_a.index("/gabarits/", 10)


def test_restoring_from_the_page_creates_a_new_version(store, web):
    db, bucket = store
    tid = _create(text="{{facture.numero}} v1")
    v2 = docx("{{facture.numero}} v2")
    tpl.update_template(tid, {}, file_stream=io.BytesIO(v2),
                        filename="note.docx", file_size=len(v2))
    html = web.get(f"/gabarits/{tid}").get_data(as_text=True)
    assert "Version 2" in html and "en vigueur" in html
    assert "Rétablir" in html
    etag = db.peek(f"doc_templates/{tid}")["etag"]

    resp = web.post(f"/gabarits/{tid}/versions/1/retablir",
                    data={"expected_etag": etag})

    assert resp.status_code == 302
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 3
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "La version 1 a été rétablie" in page
    assert "rétablie depuis la version 1" in page


def test_a_stale_restore_button_restores_nothing(store, web):
    db, _ = store
    tid = _create(text="{{facture.numero}} v1")
    v2 = docx("{{facture.numero}} v2")
    tpl.update_template(tid, {}, file_stream=io.BytesIO(v2),
                        filename="note.docx", file_size=len(v2))
    resp = web.post(f"/gabarits/{tid}/versions/1/retablir",
                    data={"expected_etag": "11111111-2222-4333-8444-555555555555"})
    assert db.peek(f"doc_templates/{tid}")["version"] == 2
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Rien n&#39;a été rétabli" in page


def test_the_edit_form_replaces_the_file_as_a_new_version(store, web):
    db, bucket = store
    tid = _create()
    v1_path = db.peek(f"doc_templates/{tid}")["storage_path"]
    etag = db.peek(f"doc_templates/{tid}")["etag"]
    v2 = docx("{{facture.numero}} nouvelle")
    resp = web.post(f"/gabarits/{tid}", data={
        "name": "Note", "category": "autre", "kind": "note_honoraires",
        "expected_etag": etag, "file": (io.BytesIO(v2), "note.docx"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    stored = db.peek(f"doc_templates/{tid}")
    assert stored["version"] == 2 and bucket.objects[stored["storage_path"]].data == v2
    assert v1_path in bucket.objects                      # the old one is kept


def test_resubmitting_the_same_file_says_no_version_was_made(store, web):
    db, bucket = store
    data = docx("{{facture.numero}}")
    doc, errors = tpl.create_template(
        io.BytesIO(data), "note.docx", len(data),
        {"name": "Note", "category": "autre", "kind": "note_honoraires"}, UID)
    tid = doc["id"]
    before = db.peek(f"doc_templates/{tid}")
    resp = web.post(f"/gabarits/{tid}", data={
        "name": "Note", "category": "autre", "kind": "note_honoraires",
        "expected_etag": before["etag"],
        "file": (io.BytesIO(data), "autre-nom.docx"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert db.peek(f"doc_templates/{tid}") == before
    page = web.get(resp.headers["Location"]).get_data(as_text=True)
    assert "aucune nouvelle version n&#39;a été créée" in page


def test_the_edit_form_refuses_a_kind_change_of_the_active_template(store, web):
    db, _ = store
    tid = _create()
    tpl.set_active_template(tid, par="j", expected_etag=None)
    etag = db.peek(f"doc_templates/{tid}")["etag"]
    resp = web.post(f"/gabarits/{tid}", data={
        "name": "Note", "category": "autre", "kind": "gabarit",
        "expected_etag": etag})
    assert resp.status_code == 200
    assert "son type ne peut pas" in resp.get_data(as_text=True)
    assert db.peek(f"doc_templates/{tid}")["kind"] == "note_honoraires"


def _escape_css(cls: str) -> str:
    b = "\\"
    for raw, esc in ((b, b * 2), (":", b + ":"), (".", b + "."),
                     ("/", b + "/"), ("[", b + "["), ("]", b + "]"),
                     ("(", b + "("), (")", b + ")"), ("+", b + "+"),
                     ("#", b + "#"), (",", b + ",")):
        cls = cls.replace(raw, esc)
    return cls


def test_the_pages_use_only_compiled_classes(store, web):
    """A class absent from the compiled artifact silently does not apply,
    and adding one is a seven-file fan-out (CLAUDE.md item 6)."""
    db, _ = store
    a, b = _create("A"), _create("B")
    tpl.set_active_template(a, par="j", expected_etag=None)
    v2 = docx("{{facture.numero}} v2")
    tpl.update_template(b, {}, file_stream=io.BytesIO(v2),
                        filename="note.docx", file_size=len(v2))
    def content(url):
        # Only what the gabarit templates render: the <main> content
        # block (base.html's own chrome is not this lot's to verify).
        html = web.get(url).get_data(as_text=True)
        start = html.index("<main")
        return html[html.index(">", start) + 1: html.index("</main>")]

    pages = [
        content(f"/gabarits/{a}?message=ok"),
        content(f"/gabarits/{b}?erreur=non"),
        content("/gabarits/"),
        content(f"/gabarits/{b}/edit"),
    ]
    assert any("Désigner comme gabarit actif" in p for p in pages)
    assert any("Rétablir" in p for p in pages)
    css = next(_ATHENA.glob("static/vendor/app.*.css")).read_text(encoding="utf-8")
    classes = {c for html in pages
               # Plain class attributes only — never an Alpine `:class`
               # binding, whose value is JavaScript, not class names.
               for block in re.findall(r'(?<![\w:-])class="([^"]+)"', html)
               for c in block.split()}
    assert classes
    absent = []
    for c in sorted(classes):
        needle = "." + _escape_css(c)
        hits = [m.end() for m in re.finditer(re.escape(needle), css)]
        if not any(i >= len(css) or not (css[i].isalnum() or css[i] in "-_\\")
                   for i in hits):
            absent.append(c)
    assert not absent, absent


# ══════════════════════════════════════════════════════════════════════
# 5. Le script de migration
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def migration(store, monkeypatch):
    db, _ = store
    monkeypatch.setattr(script, "db", db)
    return db


def test_the_script_is_a_dry_run_by_default(migration, capsys):
    db = migration
    _seed(db, "old", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(db, "new", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    before = db.peek_collection("doc_templates")
    db.reset_logs()

    assert script.main([]) == 0

    out = capsys.readouterr().out
    assert "à désigner : « Gabarit new » (new)" in out
    assert "simulation" in out
    assert db.peek_collection("doc_templates") == before
    assert [c for c in db.commits if c.ops] == []


def test_the_script_designates_exactly_the_recency_winner(migration, capsys):
    """The rule production uses TODAY — so running it before the deploy
    changes nothing the lawyer can see."""
    db = migration
    _seed(db, "old", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(db, "new", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    _seed(db, "legacy_no_kind", kind=None,
          updated_at=datetime(2026, 9, 1, tzinfo=UTC))
    _seed(db, "p1", kind="note", updated_at=datetime(2026, 3, 1, tzinfo=UTC))
    _seed(db, "g", kind="gabarit", updated_at=datetime(2026, 9, 9, tzinfo=UTC))
    winner_nh = tpl.recency_winner(
        list(db.peek_collection("doc_templates").values()), "note_honoraires")
    assert winner_nh["id"] == "new"

    assert script.main(["--apply"]) == 0

    assert _holders(db, "note_honoraires") == ["new"]
    assert _holders(db, "note") == ["p1"]
    stored = db.peek("doc_templates/new")
    assert stored["active_designated_by"] == script.PAR
    assert stored["updated_via"] == "script"
    # The old reader's choice is unchanged by the run: the winner only got
    # NEWER, so the code still deployed keeps printing the same template.
    assert tpl.recency_winner(
        list(db.peek_collection("doc_templates").values()),
        "note_honoraires")["id"] == "new"


def test_the_script_is_idempotent(migration, capsys):
    db = migration
    _seed(db, "new")
    assert script.main(["--apply"]) == 0
    after_first = db.peek_collection("doc_templates")
    db.reset_logs()
    capsys.readouterr()

    assert script.main(["--apply"]) == 0

    assert db.peek_collection("doc_templates") == after_first
    assert [c for c in db.commits if c.ops] == []
    assert "déjà désigné : « Gabarit new » (new)" in capsys.readouterr().out


def test_the_script_flags_a_designation_the_old_code_does_not_print(
    migration, capsys
):
    db = migration
    _seed(db, "designated", active_for="note_honoraires",
          updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(db, "newer", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert "l'ancien code imprime « Gabarit newer »" in out


def test_the_script_says_when_a_kind_has_no_template(migration, capsys):
    assert script.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert out.count("aucun gabarit de ce type") == 2


def test_the_script_refuses_a_template_edited_since_it_read_it(
    migration, capsys, monkeypatch
):
    db = migration
    _seed(db, "new")
    real = script._read_all

    def racing():
        rows = real()
        doc = db.peek("doc_templates/new")
        doc["etag"] = "rival"
        doc["description"] = "retouche"
        db.external_write("doc_templates/new", doc)
        return rows

    monkeypatch.setattr(script, "_read_all", racing)
    assert script.main(["--apply"]) == 1
    assert _holders(db, "note_honoraires") == []
    assert "modifié entre-temps" in capsys.readouterr().out


def test_the_script_output_survives_a_cp1252_console(migration, capsys):
    """It runs from the lawyer's Windows console (cp1252): an emoji there
    raised UnicodeEncodeError — in --apply, after a write. Every line it
    can print must encode."""
    db = migration
    _seed(db, "designated", active_for="note_honoraires",
          updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    _seed(db, "newer", updated_at=datetime(2026, 6, 1, tzinfo=UTC))
    _seed(db, "p1", kind="note")
    script.main(["--apply"])
    out = capsys.readouterr().out
    assert "[!]" in out and "[OK]" in out
    out.encode("cp1252")                        # raises if a line cannot


def test_the_script_writes_nothing_when_the_store_is_unreadable(
    migration, monkeypatch, capsys
):
    def broken():
        raise RuntimeError("503")

    monkeypatch.setattr(script, "_read_all", broken)
    assert script.main(["--apply"]) == 2
    assert "rien n'a été écrit" in capsys.readouterr().out
