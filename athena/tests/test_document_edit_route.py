"""Le formulaire d'édition d'un document, et la catégorie posée par Claude
(plan, lot 2A — étape T1, 2026-09-27).

Chaque test ici épingle un défaut réel de la route d'avant, et ÉCHOUE sur
elle :

* la section d'analyse poste ses 25 champs à CHAQUE enregistrement, et la
  route appelait ``update_analyse`` avec eux : une simple étiquette ajoutée
  confirmait l'analyse, faisait de sa catégorie celle de l'avocat et
  écrivait une entrée au journal — sans que le juriste touche à l'analyse ;
* le bandeau disait « Les renseignements de base ont été enregistrés »
  même quand l'écriture des métadonnées n'avait rien écrit ;
* une catégorie posée par Claude (D15, ``category_source == "mcp"``)
  n'avait ni mention « présumée » ni bouton « Confirmer la catégorie » ;
* les routes lisaient ``session["user_email"]``, une clé que rien
  n'écrit : chaque confirmation était signée d'un nom vide ;
* un déplacement refusé revenait sur la fiche sans un mot.

Tout passe par les VRAIES routes, les VRAIS gabarits et les VRAIS modèles
au-dessus du faux Firestore partagé ; on relit ce qui est STOCKÉ. Le modèle
lui-même : ``test_document_model_hardening.py``.
"""

import ast
import html as html_module
import os
import pathlib
import re
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
    from models import document as doc
    from models import folder as folder_model
    import routes.documents as documents_routes

from flask import Flask  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402

from tests._fake_firestore import install  # noqa: E402
from tz import to_mtl  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402
from utils.icons import ms  # noqa: E402
from utils.markdown_docx import markdown_to_safe_html  # noqa: E402
from utils.validators import format_phone_display  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
PATH = "documents/doc1"


@pytest.fixture
def db(monkeypatch):
    return install(monkeypatch, doc, folder_model)


def _seed(db, doc_id="doc1", **over):
    record = {**doc._default_doc(), "id": doc_id, "dossier_id": "d1",
              "display_name": "Lettre", "filename": "lettre.pdf",
              "file_type": "application/pdf", "category": "correspondance",
              "category_source": "juriste", "notes_internes": "Premier",
              "created_at": DT, "updated_at": DT, "etag": "e0"}
    record.update(over)
    db.seed(f"documents/{doc_id}", record)
    return doc_id


_CLIENT_LETTER = {"sous_nature": "CORR_CLIENT",
                  "privileges": ["SECRET_PROFESSIONNEL"]}


def _analysed(db, **over):
    champ, errors = doc._analyse_derivee(_CLIENT_LETTER, document={})
    assert errors == []
    return _seed(db, analyse=champ, category=champ["nature_detectee"],
                 category_source="analyse", **over)


# ══════════════════════════════════════════════════════════════════════
# 1. Le formulaire web : l'analyse n'est écrite que si le juriste l'a
#    changée ; « présumée » et « Confirmer la catégorie »
# ══════════════════════════════════════════════════════════════════════

_ETAG = re.compile(r'name="expected_etag" value="([^"]*)"')


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(
        to_mtl=to_mtl, phone=format_phone_display,
        cents_fr=lambda c: format_cents_fr(c) if c is not None else "",
        jsattr=lambda v: v, markdown=markdown_to_safe_html,
    )
    app.register_blueprint(documents_routes.documents_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        # Exactly what auth.py sets — never the « user_email » the routes
        # used to read.
        s["user_id"] = "u1"
        s["email"] = "me@cabinet.ca"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


def _form(**over):
    """The edit form as the browser posts it back UNTOUCHED for doc1."""
    form = {"display_name": "Lettre", "category": "correspondance",
            "tags": "", "notes_internes": "Premier", "document_date": "",
            "analyse_presente": "1"}
    form.update(over)
    return form


def _posted_back(client, **over):
    """GET the edit page, then post every field back exactly as rendered."""
    html = client.get("/documents/doc1/edit").get_data(as_text=True)
    fields = dict(re.findall(
        r'<input\s+type="(?:text|hidden|date)"\s+name="([^"]+)"\s+'
        r'value="([^"]*)"', html))
    fields.update({k: html_module.unescape(v) for k, v in fields.items()})
    for name, body in re.findall(
            r'<textarea name="([^"]+)"[^>]*>([^<]*)</textarea>', html):
        fields[name] = html_module.unescape(body)
    for name, block in re.findall(
            r'<select name="([^"]+)"[^>]*>(.*?)</select>', html, re.S):
        chosen = re.search(r'<option value="([^"]*)"\s+selected', block)
        fields[name] = html_module.unescape(chosen.group(1)) if chosen else ""
    checked = re.findall(
        r'<input\s+type="checkbox"\s+name="([^"]+)"\s+value="([^"]+)"'
        r'[^>]*checked', html)
    # The harness proves itself: every named control of the page was read
    # (a missed one would post as « emptied » and fake an edit).
    form = html[html.index('action="/documents/doc1/edit"'):]
    form = form[:form.index("</form>")]
    boxes = set(re.findall(r'<input\s+type="checkbox"\s+name="([^"]+)"', form))
    missed = set(re.findall(r'\bname="([^"]+)"', form)) - set(fields) - boxes
    assert missed <= {"csrf_token"}, missed
    data = [(k, v) for k, v in fields.items() if k != "csrf_token"]
    data += [(k, v) for k, v in checked]
    data = [(k, over.get(k, v)) for k, v in data]
    data += [(k, v) for k, v in over.items() if k not in fields]
    return MultiDict(data)


def test_a_tag_only_save_never_confirms_an_analysis(client, db):
    """The form posts all 25 analysis fields on every save. Calling
    update_analyse with them confirmed the analysis, flipped its category
    to the lawyer's and wrote a journal entry — for a tag edit."""
    motif = doc.MOTIF_NON_DECLASSEMENT   # a comma inside a list item
    champ, _ = doc._analyse_derivee(_CLIENT_LETTER, document={})
    champ["motifs_protection"] = [motif]
    champ["resume"] = "Ligne un\nLigne deux"
    _seed(db, analyse=champ, category=champ["nature_detectee"],
          category_source="analyse")

    resp = client.post("/documents/doc1/edit",
                       data=_posted_back(client, tags="urgent"))

    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    stored = db.peek(PATH)
    assert stored["tags"] == ["urgent"]
    assert stored["analyse"]["confirme"] is False
    assert stored["category_source"] == "analyse"
    assert stored["analyse"]["motifs_protection"] == [motif]
    assert db.peek_collection(f"{PATH}/analyses") == {}


def test_an_edited_analysis_field_alone_reaches_the_analysis(client, db):
    champ, _ = doc._analyse_derivee(
        {**_CLIENT_LETTER, "tribunal": "Cour supérieure"}, document={})
    champ["motifs_protection"] = [doc.MOTIF_NON_DECLASSEMENT]
    _seed(db, analyse=champ, category=champ["nature_detectee"],
          category_source="analyse")

    resp = client.post("/documents/doc1/edit",
                       data=_posted_back(client, analyse_auteur="Me Tremblay"))

    assert resp.status_code == 302
    stored = db.peek(PATH)["analyse"]
    assert stored["auteur"] == "Me Tremblay"
    assert stored["confirme"] is True and stored["confirme_par"] == "me@cabinet.ca"
    # Untouched fields kept their stored shape — the list was never re-split.
    assert stored["motifs_protection"] == [doc.MOTIF_NON_DECLASSEMENT]
    assert stored["tribunal"] == "Cour supérieure"
    (entry,) = db.peek_collection(f"{PATH}/analyses").values()
    assert entry["declenche_par"] == "juriste"


def test_a_copy_s_inherited_protection_shows_and_survives_an_untouched_save(
    client, db,
):
    """Lot 2A (T8): a copy carries its source's level as a SEED — no
    sub-nature. The detail page shows the régime; the edit page renders; a
    save that touches only a tag leaves the seed exactly as it was (the
    form posts every analysis field back, and none of them differs)."""
    champ, _ = doc._analyse_derivee(_CLIENT_LETTER, document={})
    seed = doc.protection_seed({"id": "src", "analyse": champ},
                               now=DT)
    _seed(db, analyse=seed, filename="lettre.docx",
          file_type=doc.EXTENSION_MIME_TYPES[".docx"])

    detail = client.get("/documents/doc1").get_data(as_text=True)
    assert "Secret professionnel" in html_module.unescape(detail)
    # Review of T8: the level is the ORIGINAL's, never a qualification of
    # the copy — and the only confirm path refuses a seed. The page says
    # where it comes from instead of presenting it as determined.
    assert "repris de l'original" in html_module.unescape(detail)
    resp = client.post("/documents/doc1/edit",
                       data=_posted_back(client, tags="urgent"))

    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    stored = db.peek(PATH)
    assert stored["tags"] == ["urgent"]
    assert stored["analyse"] == seed
    assert db.peek_collection(f"{PATH}/analyses") == {}


def test_an_analysed_document_never_reads_as_a_copy(client, db):
    """The « repris de l'original » mention belongs to a copy's seed only —
    an analysed document's level is its own analysis's."""
    _analysed(db)
    detail = html_module.unescape(
        client.get("/documents/doc1").get_data(as_text=True))
    assert "Secret professionnel" in detail
    assert "repris de l'original" not in detail


def test_analysis_values_typed_without_a_sub_nature_are_said_not_saved(
    client, db,
):
    _seed(db)
    resp = client.post("/documents/doc1/edit", data=_form(
        notes_internes="Revu", analyse_auteur="Me X"))
    html = html_module.unescape(resp.get_data(as_text=True))
    assert resp.status_code == 200
    assert "Choisissez une sous-nature" in html
    assert "Les renseignements de base ont été enregistrés" in html
    stored = db.peek(PATH)
    assert stored["notes_internes"] == "Revu"
    assert not (stored.get("analyse") or {}).get("sous_nature")


def test_a_stale_analysis_after_an_unchanged_metadata_says_nothing_was_saved(
    client, db, monkeypatch,
):
    """« Les renseignements de base ont été enregistrés » only when the
    metadata write WROTE something — a save that changed nothing wrote
    nothing, and saying otherwise is false."""
    _analysed(db)
    data = _posted_back(client, analyse_auteur="Me X")
    real = documents_routes.update_analyse

    def _racing(document_id, champs, **kw):
        db.external_write(PATH, {**db.peek(PATH), "etag": "e-rival"})
        return real(document_id, champs, **kw)

    monkeypatch.setattr(documents_routes, "update_analyse", _racing)
    html = html_module.unescape(
        client.post("/documents/doc1/edit", data=data).get_data(as_text=True))
    assert "Cet élément a été modifié entre-temps." in html
    assert "Les renseignements de base ont été enregistrés" not in html
    assert "Vos changements n'ont pas été enregistrés" in html


def test_a_refused_analysis_after_saved_metadata_says_what_landed(client, db):
    _analysed(db)
    resp = client.post("/documents/doc1/edit", data=_posted_back(
        client, notes_internes="Revu", analyse_sous_nature="INVENTE"))
    html = html_module.unescape(resp.get_data(as_text=True))
    assert resp.status_code == 200
    assert "Sous-nature inconnue" in html
    assert "Les renseignements de base ont été enregistrés ; l'analyse" in html
    assert db.peek(PATH)["notes_internes"] == "Revu"


def test_the_detail_page_presumes_and_offers_to_confirm_claudes_category(
    client, db,
):
    _seed(db, category="preuve", category_source="mcp",
          file_type="application/zip", filename="lot.zip")
    html = client.get("/documents/doc1").get_data(as_text=True)
    assert ">présumée</span>" in html
    form = html[html.index('action="/documents/doc1/categorie/confirmer"'):]
    form = form[:form.index("</form>")]
    assert _ETAG.findall(form) == ["e0"]
    assert "Confirmer la catégorie" in form

    _seed(db, category="preuve", category_source="juriste",
          file_type="application/zip", filename="lot.zip")
    html = client.get("/documents/doc1").get_data(as_text=True)
    assert "categorie/confirmer" not in html and ">présumée<" not in html


def test_the_browser_marks_claudes_category_presumed(client, db):
    _seed(db, category="preuve", category_source="mcp")
    html = client.get("/documents/?dossier_id=d1",
                      headers={"HX-Request": "true"}).get_data(as_text=True)
    assert "Catégorie posée par Claude (connecteur) — à confirmer" in html


def test_confirming_the_category_from_the_page(client, db):
    _seed(db, category="preuve", category_source="mcp",
          file_type="application/zip", filename="lot.zip")
    resp = client.post("/documents/doc1/categorie/confirmer",
                       data={"expected_etag": "e0"})
    assert resp.status_code == 302 and "erreur" not in resp.headers["Location"]
    stored = db.peek(PATH)
    assert stored["category_source"] == "juriste"
    assert stored["category_confirmed_by"] == "me@cabinet.ca"


def test_a_stale_category_confirmation_bounces_and_is_shown(client, db):
    _seed(db, category="preuve", category_source="mcp",
          file_type="application/zip", filename="lot.zip")
    db.external_write(PATH, {**db.peek(PATH), "etag": "e-rival"})
    resp = client.post("/documents/doc1/categorie/confirmer",
                       data={"expected_etag": "e0"})
    assert resp.status_code == 302       # a 2xx-bound bounce (htmx rule)
    assert db.peek(PATH)["category_source"] == "mcp"
    page = html_module.unescape(
        client.get(resp.headers["Location"]).get_data(as_text=True))
    assert "Rien n'a été confirmé" in page


def test_the_analysis_confirmation_is_signed_with_the_real_session_key(
    client, db,
):
    _analysed(db, file_type="application/zip", filename="lot.zip")
    client.post("/documents/doc1/analyse/confirmer", data={"expected_etag": "e0"})
    assert db.peek(PATH)["analyse"]["confirme_par"] == "me@cabinet.ca"


def test_a_refused_move_is_shown_on_the_detail_page(client, db):
    _seed(db, file_type="application/zip", filename="lot.zip")
    resp = client.post("/documents/doc1/move",
                       data={"target_folder_id": "absent"})
    assert resp.status_code == 302
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Le dossier de destination est introuvable." in page


# ══════════════════════════════════════════════════════════════════════
# 2. Balayages
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


def test_the_category_confirmation_has_one_caller_the_web_route():
    """D15: only the lawyer confirms Claude's category — the connector can
    never reach the function, directly or through a service.

    Rewritten deliberately in lot 2A (T7): the disclosure registry now
    NAMES the function — as a string, in the « confirm » NEVER — precisely
    to forbid it; tests/test_mcp_disclosure then sweeps every other
    connector module's syntax tree for any reference. The registry is the
    one module exempted here, and the promise must be there."""
    from mcp import disclosure

    assert _calls_of("confirmer_categorie") == {"routes/documents.py": 1}
    for path in (_ATHENA / "mcp").rglob("*.py"):
        rel = "mcp/" + path.relative_to(_ATHENA / "mcp").as_posix()
        if rel in disclosure.SWEEP_EXCLUDED:
            continue
        assert "confirmer_categorie" not in path.read_text(encoding="utf-8")
    assert any("confirmer_categorie" in never.forbidden
               for never in disclosure.NEVERS)


def test_no_route_reads_the_dead_session_key():
    for path in (_ATHENA / "routes").rglob("*.py"):
        assert "user_email" not in path.read_text(encoding="utf-8"), path.name


def test_the_new_detail_block_uses_only_compiled_classes():
    css = next((_ATHENA / "static" / "vendor").glob("app.*.css")).read_text(
        encoding="utf-8")
    src = (_ATHENA / "templates" / "documents" / "detail.html").read_text(
        encoding="utf-8")
    block = src[src.index("{% if document.category_source == 'mcp' %}"):]
    block = block[:block.index("{% endif %}")]
    classes = {c for attr in re.findall(r'class="([^"]+)"', block)
               for c in attr.split()}
    assert classes

    def escape(c):
        for raw, esc in (("\\", "\\\\"), (":", "\\:"), (".", "\\."),
                         ("/", "\\/")):
            c = c.replace(raw, esc)
        return c

    missing = [c for c in sorted(classes) if "." + escape(c) not in css]
    assert not missing, missing
