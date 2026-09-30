"""D18 — une catégorie présumée ne remplace jamais celle du juriste
(correctifs du lot 2A, 2026-09-28).

D15 laissait Claude poser une catégorie PRÉSUMÉE sur tout document non
analysé, y compris par-dessus une catégorie que le juriste venait de choisir
dans le formulaire ou de confirmer (« Confirmer la catégorie ») : l'outil
avertissait, sans refuser. D18 (confirmée par l'avocat le 2026-09-28) :
REFUSÉ quand la catégorie stockée est celle du juriste ;
permis sur une catégorie vide, restée à sa valeur par défaut, ou elle-même
présumée.

Comment on distingue un « autre » choisi d'un « autre » laissé par défaut :
le marqueur ``category_set_by_lawyer`` (``models.document.
category_set_by_lawyer``), posé VRAI par un geste explicite du juriste — le
formulaire d'édition qui CHANGE la catégorie, « Confirmer la catégorie », un
versement dont il a quitté la valeur présélectionnée — et FAUX partout
ailleurs. Rien n'est migré. Un document SANS marqueur (tout document
antérieur au 2026-09-28) est au juriste s'il porte ``category_confirmed_by``,
OU si sa catégorie est autre chose que vide ou « autre » (la valeur par
défaut du téléversement) — la règle de l'avocat, « refuser sur une catégorie
confirmée », appliquée du côté qu'il a choisi : rien n'enregistrait, avant le
marqueur, qu'une catégorie avait été choisie, donc toute catégorie autre que
le défaut est tenue pour la sienne (correctifs du lot 3, qui RÉÉCRIVENT
délibérément la règle des correctifs du lot 2A, où un document ancien n'était
au juriste que s'il portait ``category_confirmed_by``).

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
    import dav.sync as dav_sync  # its db is patched below
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import document as document_model
    from models import provenance

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (dav_sync, write_support)

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
    _doc(fake, "ancien", category="jugement")              # legacy, chosen
    _doc(fake, "vide", category="")                        # legacy, blank
    _doc(fake, "defaut_neuf", category="autre",
         category_set_by_lawyer=False)                     # new upload default
    _doc(fake, "genere", category="procédure",
         category_set_by_lawyer=False)                     # a generation's
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
    ({"category_source": "juriste"}, False),                # legacy, no category
    ({"category": "autre"}, False),                         # legacy upload default
    ({"category": "  autre  "}, False),
    ({"category": "", "category_source": ""}, False),       # legacy, blank
    ({"category_confirmed_by": "me@x.ca"}, True),           # legacy, confirmed
    ({"category": "autre",
      "category_confirmed_by": "me@x.ca"}, True),           # a confirmed « autre »
    ({"category_confirmed_by": "   "}, False),
    # REWRITTEN (fixups of lot 3): a legacy category other than the upload
    # default is the LAWYER'S — the fixups of lot 2A read these three as
    # untouched defaults, which left his older choices replaceable.
    ({"category": "jugement"}, True),                       # source absent
    ({"category": "pièce", "category_source": ""}, True),   # source blank
    ({"category": "procédure", "category_source": "juriste"}, True),
    ({"category_set_by_lawyer": True}, True),
    ({"category_set_by_lawyer": False,
      "category_confirmed_by": "me@x.ca"}, False),          # the marker decides
    ({"category": "jugement",
      "category_set_by_lawyer": False}, False),             # the marker decides
    ({"category_source": "mcp", "category_set_by_lawyer": True}, False),
    ({"category": "jugement", "category_source": "mcp"}, False),   # presumed
    ({"category": "jugement", "category_source": "analyse"}, False),
    ({"category_source": "analyse",
      "category_confirmed_by": "me@x.ca"}, False),          # presumed: never his
    ({"category_set_by_lawyer": "oui"}, False),             # not a bool: legacy
    ({"category_set_by_lawyer": "oui",
      "category": "jugement"}, True),                       # ... judged as legacy
])
def test_who_chose_the_category(doc, lawyers):
    assert document_model.category_set_by_lawyer(doc) is lawyers


def test_a_retired_legacy_value_is_judged_as_the_autre_it_reads_as(fake):
    """« entente » and « note » left the vocabulary and read as « autre »
    (``_migrate_category``): stored on a legacy document, they are the
    untouched default, not a choice — every reader migrates first."""
    _doc(fake, "entente", category="entente")
    # The raw record would read as a choice; the migrated one does not.
    assert document_model.category_set_by_lawyer(
        {"category": "entente"}) is True
    assert document_model.category_set_by_lawyer(
        document_model.get_document("entente")) is False
    assert _mcp_category("entente", "preuve")["changed_fields"] == ["category"]


# ── update_document ───────────────────────────────────────────────────────


@pytest.mark.parametrize("did", ["choisi", "confirme", "ancien"])
def test_a_category_the_lawyer_chose_or_confirmed_is_refused(fake, did):
    """FAILS on the old handler, which replaced it and only warned — and,
    for « ancien » (a legacy « jugement » with no marker), on the lot-2A
    fixup rule, which read it as an untouched default and replaced it."""
    before = fake.peek(f"documents/{did}")
    exc = _refused(did, "correspondance")
    assert "choisie ou confirmée par le juriste" in str(exc)
    # Review of the fixups of lot 3: a legacy category is refused as HELD
    # to be his — nothing recorded that he chose it — and the refusal says
    # so rather than asserting a choice as a fact. FAILS on the old text.
    assert "ou posée avant ce suivi, et tenue pour la sienne" in str(exc)
    assert "Rien n'a été modifié" in str(exc)
    assert before["category"] not in str(exc)             # names the rule only
    assert fake.peek(f"documents/{did}") == before
    # The other filing fields stay writable.
    other = handlers.update_document({"document_id": did,
                                      "display_name": "Renommé"})
    assert other["changed_fields"] == ["display_name"]
    assert fake.peek(f"documents/{did}")["category"] == before["category"]


@pytest.mark.parametrize(
    "did", ["defaut", "presume", "vide", "defaut_neuf", "genere"])
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
        "defaut": False, "confirme": True, "choisi": True, "presume": False,
        "ancien": True, "vide": False, "defaut_neuf": False, "genere": False}


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
    assert "posée avant ce suivi" in document_model.MCP_CATEGORY_ON_LAWYERS


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


def test_each_upload_form_is_judged_against_its_own_pre_selection():
    """Rewritten deliberately (review of the fixups of lot 2A): it claimed
    « the upload form and Réception's versement pre-select « autre » » and
    checked only the first. Réception pre-selects « pièce » — so the route,
    comparing to « autre », recorded every versement left on its default as
    the lawyer's choice. Each form now names its own pre-selection, and the
    Réception select renders the very constant its route compares to."""
    assert document_model.UPLOAD_DEFAULT_CATEGORY == "autre"
    upload = (_ATHENA / "templates" / "documents" / "upload.html").read_text(
        encoding="utf-8")
    assert "'selected' if cat_key == 'autre'" in upload
    documents_route = (_ATHENA / "routes/documents.py").read_text(
        encoding="utf-8")
    assert "lawyer_set_category=" in documents_route
    assert "UPLOAD_DEFAULT_CATEGORY" in documents_route

    with mock.patch("google.cloud.firestore.Client"):
        import routes.reception as rc

    assert rc.VERSEMENT_DEFAULT_CATEGORY == "pièce"
    reception = (_ATHENA / "templates" / "reception" / "index.html").read_text(
        encoding="utf-8")
    assert "cle == versement_default_category %}selected" in reception
    assert 'cle == "pièce"' not in reception        # no copied literal
    source = (_ATHENA / "routes/reception.py").read_text(encoding="utf-8")
    assert "versement_default_category=VERSEMENT_DEFAULT_CATEGORY" in source
    assert "UPLOAD_DEFAULT_CATEGORY" not in source.replace(
        "models.document.UPLOAD_DEFAULT_CATEGORY", "")


@pytest.mark.parametrize("submitted, lawyers", [
    ("pièce", False),        # the pre-selected value, left alone
    ("autre", True),         # moved OFF it: the lawyer's choice
    ("jugement", True),
    (None, False),           # no field at all: nobody's choice
    ("", False),
])
def test_reception_counts_only_a_moved_select_as_the_lawyers(submitted, lawyers):
    """Fails on the old route, which compared to « autre »: « pièce » read
    True and a deliberate « autre » False."""
    with mock.patch("google.cloud.firestore.Client"):
        import routes.reception as rc

    assert rc._categorie_choisie(submitted) is lawyers


# ── An analysis the lawyer confirmed or edited is his determination ──────


def _analysed(fake, did):
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": "CORR_TIERS"},
        document={"id": did, "category": "autre"})
    assert not errors, errors
    _doc(fake, did, analyse=champ, category=champ["nature_detectee"],
         category_source="analyse")


def test_confirming_an_analysis_makes_the_category_the_lawyers(fake):
    """Review of the fixups: confirmer_analyse made the category « a
    determination of the lawyer » (category_source « juriste ») but left
    the D18 marker absent — the document itself stayed protected by its
    analysis, yet its COPY (which carries no analysis) read the category
    as an untouched default Claude could replace. Fails on the old code."""
    _analysed(fake, "qualifie")
    doc, errors = document_model.confirmer_analyse(
        "qualifie", "me@cabinet.ca")
    assert not errors, errors
    stored = fake.peek("documents/qualifie")
    assert stored["category_set_by_lawyer"] is True
    assert document_model.category_set_by_lawyer(stored) is True


def test_editing_an_analysis_makes_the_category_the_lawyers(fake):
    _analysed(fake, "edite")
    doc, errors = document_model.update_analyse(
        "edite", {"auteur": "Un Toit en Réserve"}, par="me@cabinet.ca")
    assert not errors, errors
    assert fake.peek("documents/edite")["category_set_by_lawyer"] is True


def test_a_copy_of_a_confirmed_analysis_keeps_the_lawyers_category(
    fake, monkeypatch,
):
    """The consent promises that a copy's category stays presumed « sauf si
    vous l'aviez posée vous-même »: a category the lawyer CONFIRMED through
    its analysis travels to the copy as his."""
    _analysed(fake, "source")
    fake.seed("documents/source", {
        **fake.peek("documents/source"),
        "storage_path": "users/u/dossiers/d1/documents/source/x.pdf",
        "filename": "x.pdf", "file_type": "application/pdf"})
    document_model.confirmer_analyse("source", "me@cabinet.ca")
    bucket = mock.Mock()
    monkeypatch.setattr(document_model.storage, "bucket", lambda: bucket)
    seen = {}

    def ingest(*args, **kwargs):
        seen.update(kwargs)
        return {"id": "copie"}, []

    monkeypatch.setattr(document_model, "ingest_blob_as_document", ingest)
    copy, errors = document_model.copy_document("source", user_id=UID)
    assert not errors, errors
    assert seen["category_source"] == "juriste"
    assert seen["lawyer_set_category"] is True


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
