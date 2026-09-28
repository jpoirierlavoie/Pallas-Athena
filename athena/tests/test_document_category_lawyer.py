"""D18 — une catégorie présumée ne remplace jamais celle du juriste
(correctifs du lot 2A, 2026-09-28).

D15 laissait Claude poser une catégorie PRÉSUMÉE sur tout document non
analysé, y compris par-dessus une catégorie que le juriste venait de choisir
dans le formulaire ou de confirmer (« Confirmer la catégorie ») : l'outil
avertissait, sans refuser. D18 (le défaut recommandé par l'avocat, réversible
sur demande) : REFUSÉ quand la catégorie stockée est celle du juriste ;
permis sur une catégorie vide, restée à sa valeur par défaut, ou elle-même
présumée.

Comment on distingue un « autre » choisi d'un « autre » laissé par défaut :
le marqueur ``category_set_by_lawyer`` (``models.document.
category_set_by_lawyer``), posé VRAI par un geste explicite du juriste — le
formulaire d'édition qui CHANGE la catégorie, « Confirmer la catégorie », un
versement dont il a quitté la valeur présélectionnée — et FAUX partout
ailleurs. Rien n'est migré : un document sans marqueur n'est au juriste que
s'il porte ``category_confirmed_by``.

Tout passe par les VRAIS modèles et le vrai gestionnaire au-dessus du faux
Firestore partagé ; on relit ce qui est STOCKÉ. Chaque refus ÉCHOUE sur le
code d'avant (qui écrivait et avertissait).
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
    import dav.sync as dav_sync  # noqa: F401 — its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support  # noqa: F401
    from models import document as document_model
    from models import provenance

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _doc(fake, did, **over):
    record = {**document_model._default_doc(), "id": did,
              "dossier_id": "d1", "display_name": f"Pièce {did}",
              "category": "autre", "category_source": "juriste",
              "created_at": DT, "updated_at": DT, "etag": f"e-{did}"}
    record.update(over)
    fake.seed(f"documents/{did}", record)


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Dossier", "status": "actif"})
    _doc(fake, "defaut")                                   # untouched, legacy
    _doc(fake, "confirme", category="preuve",
         category_confirmed_by="me@cabinet.ca")            # legacy, confirmed
    _doc(fake, "choisi", category="jugement",
         category_set_by_lawyer=True)                      # the lawyer's
    _doc(fake, "presume", category="preuve", category_source="mcp",
         category_set_by_lawyer=False)                     # Claude's
    return fake


def _mcp_category(did, category, **extra):
    return handlers.update_document(
        {"document_id": did, "category": category, **extra})


def _refused(did, category) -> tools.ToolArgumentError:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        _mcp_category(did, category)
    return excinfo.value


# ── The rule, pure ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("doc, lawyers", [
    ({}, False),                                            # legacy, blank
    ({"category_source": "juriste"}, False),                # untouched default
    ({"category_confirmed_by": "me@x.ca"}, True),           # legacy, confirmed
    ({"category_confirmed_by": "   "}, False),
    ({"category_set_by_lawyer": True}, True),
    ({"category_set_by_lawyer": False,
      "category_confirmed_by": "me@x.ca"}, False),          # the marker decides
    ({"category_source": "mcp", "category_set_by_lawyer": True}, False),
    ({"category_source": "analyse",
      "category_confirmed_by": "me@x.ca"}, False),          # presumed: never his
    ({"category_set_by_lawyer": "oui"}, False),             # not a bool: legacy
])
def test_who_chose_the_category(doc, lawyers):
    assert document_model.category_set_by_lawyer(doc) is lawyers


# ── update_document ───────────────────────────────────────────────────────


@pytest.mark.parametrize("did", ["choisi", "confirme"])
def test_a_category_the_lawyer_chose_or_confirmed_is_refused(fake, did):
    """FAILS on the old handler, which replaced it and only warned."""
    before = fake.peek(f"documents/{did}")
    exc = _refused(did, "correspondance")
    assert "choisie ou confirmée par le juriste" in str(exc)
    assert "Rien n'a été modifié" in str(exc)
    assert before["category"] not in str(exc)             # names the rule only
    assert fake.peek(f"documents/{did}") == before
    # The other filing fields stay writable.
    other = handlers.update_document({"document_id": did,
                                      "display_name": "Renommé"})
    assert other["changed_fields"] == ["display_name"]
    assert fake.peek(f"documents/{did}")["category"] == before["category"]


@pytest.mark.parametrize("did", ["defaut", "presume"])
def test_an_untouched_default_or_a_presumed_category_stays_claudes(fake, did):
    result = _mcp_category(did, "correspondance")
    stored = fake.peek(f"documents/{did}")
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "mcp"
    assert stored["category_set_by_lawyer"] is False
    assert result["entity"]["category_set_by_lawyer"] is False
    assert result["entity"]["category_presumee"] is True


def test_the_warning_no_longer_calls_a_default_the_lawyers_choice(fake):
    """REWRITTEN intent (fixups of lot 2A): the old warning called ANY
    « juriste » category « un choix du juriste » — including an upload's
    untouched default. What remains replaceable was nobody's choice."""
    result = _mcp_category("defaut", "correspondance")
    text = " ".join(result["warnings"])
    assert "ni comme choisie ni comme confirmée" in text
    assert "PRÉSUMÉE" in text and "choix du juriste" not in text


def test_the_lawyers_choice_landing_before_the_commit_is_refused(fake, monkeypatch):
    """The handler read an untouched default; the lawyer's form save lands
    before the model's transaction — here WITHOUT a new etag, so the
    version check cannot catch it. The MODEL's D18 rule refuses on its own
    transactional read, and the connector says it in its own words."""
    real = document_model.get_document_strict

    def racing(document_id):
        result = real(document_id)
        fake.external_write("documents/defaut", {
            **fake.peek("documents/defaut"), "category": "pièce",
            "category_set_by_lawyer": True})
        return result

    monkeypatch.setattr(document_model, "get_document_strict", racing)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        _mcp_category("defaut", "correspondance", expected_etag="e-defaut")
    assert "choisie ou confirmée par le juriste" in str(excinfo.value)
    stored = fake.peek("documents/defaut")
    assert stored["category"] == "pièce" and stored["category_source"] == "juriste"


def test_list_documents_says_which_category_is_the_lawyers(fake):
    rows = {r["id"]: r for r in handlers.list_documents(
        {"dossier_id": "d1"})["items"]}
    assert {i: r["category_set_by_lawyer"] for i, r in rows.items()} == {
        "defaut": False, "confirme": True, "choisi": True, "presume": False}


# ── The web gestures that make it the lawyer's ────────────────────────────


def test_the_edit_form_changing_the_category_makes_it_the_lawyers(fake):
    doc, errors, changed = document_model.update_metadata(
        "defaut", {"category": "pièce"})
    assert errors == [] and changed is True
    stored = fake.peek("documents/defaut")
    assert stored["category_source"] == "juriste"
    assert stored["category_set_by_lawyer"] is True
    assert "choisie ou confirmée" in str(_refused("defaut", "preuve"))


def test_a_form_save_that_leaves_the_category_does_not_claim_it(fake):
    doc, errors, changed = document_model.update_metadata(
        "defaut", {"category": "autre", "display_name": "Renommé"})
    assert errors == [] and changed is True
    assert "category_set_by_lawyer" not in fake.peek("documents/defaut")
    assert _mcp_category("defaut", "preuve")["changed_fields"] == ["category"]


def test_confirming_makes_it_the_lawyers(fake):
    doc, errors = document_model.confirmer_categorie(
        "presume", "me@cabinet.ca", expected_etag="e-presume")
    assert errors == []
    assert fake.peek("documents/presume")["category_set_by_lawyer"] is True
    assert "choisie ou confirmée" in str(_refused("presume", "jugement"))


def test_the_model_refuses_the_connector_on_every_path(fake):
    """The rule lives in the MODEL (plan rule 2): a connector-path write
    that skips the handler is refused too."""
    with provenance.writing_via("mcp", tool="update_document"):
        doc, errors, changed = document_model.update_metadata(
            "choisi", {"category": "autre"}, source="mcp")
    assert doc is None and changed is False
    assert errors == [document_model.MCP_CATEGORY_ON_LAWYERS]


# ── Creation: the upload forms and the copy ───────────────────────────────


def _record(**kw):
    merged, errors = document_model._prepare_document_record(
        "d1", "2026-001", "lettre.pdf", ".pdf", "application/pdf", 10,
        {"category": kw.pop("category", "autre")}, UID, **kw)
    assert errors == [], errors
    return merged


def test_a_new_record_carries_the_marker_it_was_given():
    assert _record(category="pièce", lawyer_set_category=True)[
        "category_set_by_lawyer"] is True
    assert _record()["category_set_by_lawyer"] is False
    # Never the lawyer's beside a presumed source, whatever the caller says.
    assert _record(category="pièce", category_source="mcp",
                   lawyer_set_category=True)["category_set_by_lawyer"] is False


def test_the_upload_forms_start_on_the_default_the_rule_names():
    """The upload form and Réception's versement pre-select « autre »: a
    category still equal to it was not moved by the lawyer."""
    assert document_model.UPLOAD_DEFAULT_CATEGORY == "autre"
    upload = (_ATHENA / "templates" / "documents" / "upload.html").read_text(
        encoding="utf-8")
    assert "'selected' if cat_key == 'autre'" in upload
    for route in ("routes/documents.py", "routes/reception.py"):
        source = (_ATHENA / route).read_text(encoding="utf-8")
        assert "lawyer_set_category=" in source, route
        assert "UPLOAD_DEFAULT_CATEGORY" in source, route


def test_the_web_upload_passes_the_lawyers_choice(monkeypatch):
    """The finaliser hands the model True for a category moved off the
    default, False for the default — read from the same metadata it files."""
    from flask import Flask

    with mock.patch("google.cloud.firestore.Client"):
        import routes.documents as rd

    app = Flask(__name__)
    app.config.update(SECRET_KEY="t", TESTING=True)
    app.register_blueprint(rd.documents_bp)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u1"
        sess["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(rd, "get_dossier",
                        lambda d: {"id": "d1", "file_number": "2026-001"})
    monkeypatch.setattr(rd.storage_identity, "request_uid", lambda: "u1")
    bucket = mock.Mock()
    bucket.blob.return_value = mock.MagicMock()
    monkeypatch.setattr(rd.storage, "bucket", lambda: bucket)
    seen = []

    def ingest(*args, **kwargs):
        seen.append(kwargs["lawyer_set_category"])
        return {"id": "doc9"}, []

    monkeypatch.setattr(rd, "ingest_blob_as_document", ingest)
    seg = "3f2b8c1e-9a4d-4c6b-8e2f-1a2b3c4d5e6f"
    for category in ("pièce", "autre"):
        response = client.post("/documents/api/finaliser", json={
            "objet": f"staging/u1/{seg}/a.pdf", "name": "a.pdf",
            "dossier_id": "d1", "category": category, "tags": "",
            "folder_id": "", "display_name": "", "document_date": ""})
        assert response.status_code == 200, response.get_data(as_text=True)
    assert seen == [True, False]
