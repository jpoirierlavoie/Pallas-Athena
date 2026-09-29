"""D25 — l'analyse cède à la catégorie du juriste (décision du 2026-09-29).

D18 refusait déjà qu'une catégorie PRÉSUMÉE (celle que Claude pose par
``update_document``) remplace une catégorie que le juriste a choisie ou
confirmée. L'analyse (``record_document_analysis`` → ``record_analyse``) en
était la seule exception : elle ÉCRASAIT la catégorie par celle que sa
sous-nature dérive, et remettait l'analyse à « présumée ». D25 : sur une
catégorie du juriste (``category_set_by_lawyer`` — une analyse CONFIRMÉE
comprise), l'analyse GARDE la catégorie et sa provenance ; ce que la
sous-nature aurait dérivé est inscrit dans l'analyse (``categorie_derivee``)
avec le drapeau ``divergence_categorie``, le connecteur le dit dans un
avertissement, et la fiche du document le montre.

Et les DEUX confirmations sont distinctes (revue de D25, même jour) : celle
de la CATÉGORIE survit à une nouvelle analyse, celle de l'ANALYSE jamais —
un passage neuf repart présumé, la confirmation précédente consignée
(``confirmation_precedente``) ; et quand la catégorie n'était la sienne QUE
par cette confirmation, sa protection est d'abord rendue explicite (le
marqueur, ``category_confirmed_by`` / ``_at``).

Et deux conséquences, épinglées ici avec la règle :

* ``update_document`` juge la règle du juriste AVANT celle de l'analyse —
  sur un document analysé dont la catégorie est la sienne, son refus dit
  « dites-le au juriste » au lieu d'envoyer vers une analyse qui ne la
  changerait pas ;
* ``update_analyse`` (le juriste qui corrige l'analyse au formulaire) ne
  redérive la catégorie que s'il change la sous-nature : corriger un auteur
  ne défait plus, en silence, la catégorie qu'une analyse lui a gardée.

Tout passe par les VRAIS modèles, le vrai gestionnaire et la vraie route
au-dessus du faux Firestore partagé ; on relit ce qui est STOCKÉ. Chaque
test d'un comportement changé ÉCHOUE sur le code d'avant.
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
    import mcp.output_schemas as output_schemas
    import mcp.tools as tools
    import mcp.write_support as write_support  # noqa: F401
    from models import document as document_model
    from models import provenance
    import routes.documents as documents_routes

from tests._fake_firestore import install  # noqa: E402

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
CONFIRMED_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
PRIVATE_TEXT = "Texte-privé-7QX"
PRIVATE_NAME = "Tremblay-Q9"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _doc(fake, did, **over):
    record = {**document_model._default_doc(), "id": did,
              "dossier_id": "d1", "display_name": f"Pièce {did}",
              # Not previewable: the detail page never asks Storage for a URL.
              "filename": "lot.zip", "file_type": "application/zip",
              "category": "autre", "category_source": "juriste",
              "created_at": DT, "updated_at": DT, "etag": f"e-{did}"}
    record.update(over)
    fake.seed(f"documents/{did}", record)


def _confirmed_analysis(sous_nature: str) -> dict:
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": sous_nature}, document={"category": "autre"})
    assert not errors, errors
    champ.update({"confirme": True, "confirme_par": "me@cabinet.ca",
                  "confirme_le": CONFIRMED_AT})
    return champ


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "Dossier", "status": "actif"})
    # The lawyer's, with the marker: moved off the form's default.
    _doc(fake, "choisi", category="correspondance", category_set_by_lawyer=True)
    # The lawyer's, and it agrees with what the analysis will derive.
    _doc(fake, "concorde", category="procédure", category_set_by_lawyer=True)
    # Legacy (no marker), not the upload default: held to be his (D18).
    _doc(fake, "ancien", category="jugement")
    # Legacy « autre » whose analysis the lawyer CONFIRMED — the marker
    # never reached it; only the D25 clause makes it his.
    _doc(fake, "confirme_ancien", category="autre",
         analyse=_confirmed_analysis("CAB_MEMO"))
    # Nobody's: a new upload left on the default, and Claude's presumed one.
    _doc(fake, "defaut", category="autre", category_set_by_lawyer=False)
    _doc(fake, "presume", category="preuve", category_source="mcp",
         category_set_by_lawyer=False)
    return fake


_SORTIE = {"sous_nature": "PROC_DEM_INTRO", "privileges": ["PUBLIC"],
           "resume": PRIVATE_TEXT, "parties_mentionnees": [PRIVATE_NAME]}


def _record(did, **over):
    return document_model.record_analyse(did, {**_SORTIE, **over})


def _journal(fake, did) -> list[dict]:
    return list(fake.peek_collection(
        f"documents/{did}/{document_model.ANALYSES_SUBCOLLECTION}").values())


def _analyse_tool(did, **over):
    return handlers.record_document_analysis(
        {"document_id": did, **_SORTIE, **over})


# ── The rule, in the model ────────────────────────────────────────────────


def test_the_lawyers_category_is_kept_and_the_gap_recorded(fake):
    """FAILS on the old code, which stored « procédure » (source analyse)."""
    doc, errors = _record("choisi")
    assert errors == [], errors
    stored = fake.peek("documents/choisi")
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "juriste"
    assert stored["category_set_by_lawyer"] is True
    a = stored["analyse"]
    assert a["nature_detectee"] == "procédure"
    assert a["categorie_derivee"] == "procédure"
    assert a["categorie_conservee"] is True
    assert a["divergence_categorie"] is True
    # Nothing was replaced — what was there is recorded, and stays there.
    assert a["categorie_precedente"] == "correspondance"
    assert a["categorie_remplacee"] is False
    assert a["remplace_un_choix_du_juriste"] is False
    [entry] = _journal(fake, "choisi")
    assert entry["divergence_categorie"] is True
    assert entry["categorie_derivee"] == "procédure"
    assert doc["category"] == "correspondance"


def test_a_new_run_is_never_confirmed_and_records_the_prior_confirmation(fake):
    """Review of D25 — the two confirmations separated. FAILS on the D25
    code, which KEPT `confirme` true on the cache: a run the lawyer never
    read carried his confirmation, and the connector's analyse_confirmee
    said « confirmed ». His confirmation covered the PREVIOUS run: it is
    recorded (cache and journal), never carried over. His CATEGORY, its
    source and its marker stay."""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    _, errors = _record("qualifie")
    assert errors == [], errors
    stored = fake.peek("documents/qualifie")
    a = stored["analyse"]
    assert (a["confirme"], a["confirme_par"], a["confirme_le"]) == (
        False, None, None)
    assert a["confirmation_precedente"] == {"par": "me@cabinet.ca",
                                            "le": CONFIRMED_AT}
    assert a["sous_nature"] == "PROC_DEM_INTRO"           # the new run
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "juriste"
    assert stored["category_set_by_lawyer"] is True
    assert a["categorie_conservee"] is True and a["divergence_categorie"] is True
    # The marker protected it already: nothing to make explicit.
    assert "category_confirmed_by" not in stored
    assert document_model.category_set_by_lawyer(stored) is True
    [entry] = _journal(fake, "qualifie")
    assert entry["confirme"] is False and entry["confirme_par"] is None
    assert entry["confirmation_precedente"] == a["confirmation_precedente"]


def test_a_run_on_an_unconfirmed_analysis_records_no_prior_confirmation(fake):
    _record("choisi")
    _record("choisi")
    stored = fake.peek("documents/choisi")
    assert stored["analyse"]["confirmation_precedente"] is None
    assert stored["analyse"]["confirme"] is False


def test_a_legacy_autre_whose_analysis_he_confirmed_is_his(fake):
    """The D25 clause of `category_set_by_lawyer`: FAILS on the old rule,
    which read a legacy « autre » as the untouched upload default.

    Review of D25: this « autre » was his ONLY through the analysis
    confirmation the new run supersedes. Before that confirmation leaves
    the cache, the category's protection is made EXPLICIT — the marker,
    and category_confirmed_by / _at taken from it — so the category stays
    his once the analysis flag resets: a second run keeps it, and a
    presumed category from the connector is refused (D18). FAILS on the
    D25 code, which kept the analysis confirmed instead."""
    stored = fake.peek("documents/confirme_ancien")
    assert document_model.category_set_by_lawyer(stored) is True
    _, errors = _record("confirme_ancien")
    assert errors == [], errors
    after = fake.peek("documents/confirme_ancien")
    assert after["category"] == "autre"
    assert after["analyse"]["confirme"] is False
    assert after["analyse"]["divergence_categorie"] is True
    assert after["category_set_by_lawyer"] is True
    assert after["category_source"] == "juriste"
    assert after["category_confirmed_by"] == "me@cabinet.ca"
    assert after["category_confirmed_at"] == CONFIRMED_AT
    assert document_model.category_set_by_lawyer(after) is True
    # A second run: the analysis is presumed, the category still his.
    _, errors = _record("confirme_ancien", sous_nature="JUG_JUGEMENT")
    assert errors == [], errors
    again = fake.peek("documents/confirme_ancien")
    assert again["category"] == "autre"
    assert again["analyse"]["categorie_conservee"] is True
    assert again["analyse"]["confirmation_precedente"] is None
    assert again["category_confirmed_at"] == CONFIRMED_AT     # untouched
    # D18 still refuses a presumed category over it.
    with provenance.writing_via("mcp", tool="update_document"):
        doc, errs, changed = document_model.update_metadata(
            "confirme_ancien", {"category": "preuve"}, source="mcp")
    assert doc is None and errs == [document_model.MCP_CATEGORY_ON_LAWYERS]


@pytest.mark.parametrize("record, expected", [
    ({}, {}),                                             # no analysis
    ({"category": "autre",
      "analyse": {"sous_nature": "CAB_MEMO", "confirme": False}}, {}),
    # His through the marker: the confirmation adds nothing to protect.
    ({"category": "autre", "category_set_by_lawyer": True,
      "analyse": {"confirme": True, "confirme_par": "a", "confirme_le": DT}},
     {}),
    # A legacy non-default category is his without the confirmation.
    ({"category": "jugement",
      "analyse": {"confirme": True, "confirme_par": "a", "confirme_le": DT}},
     {}),
    # A legacy « autre »: his ONLY through the confirmation.
    ({"category": "autre",
      "analyse": {"confirme": True, "confirme_par": "a", "confirme_le": DT}},
     {"category_set_by_lawyer": True, "category_source": "juriste",
      "category_confirmed_by": "a", "category_confirmed_at": DT}),
    # A contradictory shape (the source says « analyse »): the shape
    # confirmer_analyse writes, source included.
    ({"category": "jugement", "category_source": "analyse",
      "analyse": {"confirme": True, "confirme_par": "a", "confirme_le": DT}},
     {"category_set_by_lawyer": True, "category_source": "juriste",
      "category_confirmed_by": "a", "category_confirmed_at": DT}),
    # A category confirmation already on record is never overwritten.
    ({"category": "jugement", "category_source": "analyse",
      "category_confirmed_by": "b",
      "analyse": {"confirme": True, "confirme_par": "a", "confirme_le": DT}},
     {"category_set_by_lawyer": True, "category_source": "juriste"}),
])
def test_the_category_protection_made_explicit(record, expected):
    assert document_model._category_protection_from(record) == expected


def test_a_legacy_category_held_to_be_his_is_kept(fake):
    _, errors = _record("ancien")
    assert errors == [], errors
    stored = fake.peek("documents/ancien")
    assert stored["category"] == "jugement"
    assert stored["category_source"] == "juriste"
    assert stored["analyse"]["divergence_categorie"] is True


@pytest.mark.parametrize("did", ["defaut", "presume"])
def test_a_category_nobody_chose_is_still_replaced(fake, did):
    """Today's behaviour, unchanged: the derived category replaces it,
    PRESUMED."""
    _, errors = _record(did)
    assert errors == [], errors
    stored = fake.peek(f"documents/{did}")
    assert stored["category"] == "procédure"
    assert stored["category_source"] == "analyse"
    a = stored["analyse"]
    assert a["categorie_conservee"] is False
    assert a["divergence_categorie"] is False
    assert a["categorie_remplacee"] is True
    assert a["confirme"] is False
    assert document_model.analysis_category_divergence(stored) == ""


def test_no_gap_when_his_category_agrees_with_the_analysis(fake):
    _, errors = _record("concorde")
    assert errors == [], errors
    stored = fake.peek("documents/concorde")
    assert stored["category"] == "procédure"
    assert stored["category_source"] == "juriste"
    assert stored["analyse"]["categorie_conservee"] is True
    assert stored["analyse"]["divergence_categorie"] is False
    assert document_model.analysis_category_divergence(stored) == ""


@pytest.mark.parametrize("doc, gap", [
    ({}, ""),                                                 # no analysis
    ({"category": "correspondance", "category_set_by_lawyer": True,
      "analyse": {"sous_nature": "PROC_DEM_INTRO",
                  "categorie_derivee": "procédure"}}, "procédure"),
    # He aligned the category afterwards: no gap left.
    ({"category": "procédure", "category_set_by_lawyer": True,
      "analyse": {"sous_nature": "PROC_DEM_INTRO",
                  "categorie_derivee": "procédure",
                  "divergence_categorie": True}}, ""),
    # A third value: still a gap, whatever the write-time flag said.
    ({"category": "jugement", "category_set_by_lawyer": True,
      "analyse": {"sous_nature": "PROC_DEM_INTRO",
                  "categorie_derivee": "procédure",
                  "divergence_categorie": False}}, "procédure"),
    # An analysis stored before D25: its nature_detectee is the derivation.
    ({"category": "jugement",
      "analyse": {"sous_nature": "PROC_DEM_INTRO",
                  "nature_detectee": "procédure"}}, "procédure"),
    # Not his (an analysis' own, inconsistent record): never « la vôtre ».
    ({"category": "autre", "category_source": "analyse",
      "analyse": {"sous_nature": "PROC_DEM_INTRO",
                  "nature_detectee": "procédure"}}, ""),
])
def test_the_gap_is_read_from_the_current_state(doc, gap):
    assert document_model.analysis_category_divergence(doc) == gap


def test_the_lawyers_choice_landing_before_the_commit_is_kept(fake, monkeypatch):
    """The handler read a category nobody chose; the lawyer's form save lands
    before the model's transaction (here WITHOUT a new etag, so the version
    check cannot catch it). The model decides on ITS transactional read and
    keeps it — FAILS on the old code, which replaced it."""
    real = document_model.get_document_strict

    def racing(document_id):
        result = real(document_id)
        fake.external_write("documents/defaut", {
            **fake.peek("documents/defaut"), "category": "pièce",
            "category_set_by_lawyer": True})
        return result

    monkeypatch.setattr(document_model, "get_document_strict", racing)
    result = _analyse_tool("defaut")
    stored = fake.peek("documents/defaut")
    assert stored["category"] == "pièce"
    assert stored["category_source"] == "juriste"
    assert result["category"] == "pièce"
    assert any("CONSERVÉE" in w for w in result["warnings"])


# ── The connector says it ─────────────────────────────────────────────────


def test_the_tool_names_the_gap_without_quoting_the_document(fake):
    """FAILS on the old handler, which replaced the category and warned
    « … est remplacée »."""
    result = _analyse_tool("choisi")
    assert result["category"] == "correspondance"
    assert result["category_source"] == "juriste"
    echo = result["analyse"]
    assert echo["categorie_derivee"] == "procédure"
    assert echo["categorie_conservee"] is True
    assert echo["divergence_categorie"] is True
    text = " ".join(result["warnings"])
    assert "Catégorie du juriste CONSERVÉE" in text
    assert "« correspondance »" in text and "« procédure »" in text
    assert "dites-le au juriste" in text
    assert "reste la sienne" in text                       # not confirmed
    assert "Classification PRÉSUMÉE" not in text
    assert "est remplacée" not in text
    # Codes and vocabulary only — never a line of the document.
    assert PRIVATE_TEXT not in text and PRIVATE_NAME not in text
    schema = output_schemas.OUTPUT_SCHEMAS["record_document_analysis"]
    assert tools.validate_args(schema, result) == []


def test_a_kept_legacy_category_reads_juriste_never_blank(fake):
    """A legacy document may store no `category_source` at all: kept by the
    analysis (D25), its source reads « juriste » like every document row —
    the handler used to echo the raw field, which the analysis always
    overwrote, so "" was never reachable before."""
    _doc(fake, "sans_source", category="jugement")
    record = fake.peek("documents/sans_source")
    del record["category_source"]
    fake.seed("documents/sans_source", record)
    result = _analyse_tool("sans_source")
    assert result["category"] == "jugement"
    assert result["category_source"] == "juriste"
    assert "category_source" not in fake.peek("documents/sans_source")


def test_the_tool_says_his_confirmation_covered_the_previous_analysis(fake):
    """Review of D25: FAILS on the D25 handler, which echoed `confirme`
    true and said « Catégorie et confirmation du juriste CONSERVÉES ». The
    run is presumed; the warning says the confirmation covered the previous
    analysis, not this one — never who confirmed it, never when, never a
    line of the document."""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    result = _analyse_tool("qualifie")
    assert result["analyse"]["confirme"] is False
    text = " ".join(result["warnings"])
    assert "votre confirmation couvrait l'analyse précédente, pas celle-ci" in text
    assert "La catégorie, elle, reste la sienne." in text
    assert "« Présumée »" in text
    for gone in ("CONSERVÉES", "confirmée avant cette analyse",
                 "sans qu'il l'ait lue"):
        assert gone not in text, gone
    for private in (PRIVATE_TEXT, PRIVATE_NAME, "me@cabinet.ca", "01/09"):
        assert private not in text, private
    schema = output_schemas.OUTPUT_SCHEMAS["record_document_analysis"]
    assert tools.validate_args(schema, result) == []


def test_list_documents_reports_a_new_run_unconfirmed(fake):
    """Review of D25: `analyse_confirmee` said true for a run the lawyer
    never read (the D25 cache kept his confirmation). FAILS on the D25
    code."""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))

    def row():
        return {r["id"]: r for r in handlers.list_documents(
            {"dossier_id": "d1"})["items"]}["qualifie"]

    assert row()["analyse_confirmee"] is True               # his, as read
    _analyse_tool("qualifie")
    assert row()["analyse_confirmee"] is False
    assert row()["category_presumee"] is False              # the category: his
    _, errors = document_model.confirmer_analyse("qualifie", "me@cabinet.ca")
    assert errors == [], errors
    assert row()["analyse_confirmee"] is True


def test_a_replacement_is_never_called_his_choice(fake):
    """What an analysis still replaces under the « juriste » source was
    nobody's choice. FAILS on the old text, « posée dans l'application »."""
    result = _analyse_tool("defaut")
    assert result["category"] == "procédure"
    text = " ".join(result["warnings"])
    assert "ni comme choisie ni comme confirmée par le juriste" in text
    assert "posée dans l'application" not in text
    assert "Classification PRÉSUMÉE" in text


def test_the_audit_line_carries_the_kept_category_and_the_gap(fake, monkeypatch):
    """OBSERVABILITY.md registers the two fields; codes only."""
    import utils.logging_setup as ls

    seen = []
    real = ls.log_mcp_event

    def spy(*a, **k):
        seen.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(ls, "log_mcp_event", spy)
    _analyse_tool("choisi")
    [(args, kwargs)] = [s for s in seen if s[0][0] == "mcp_document_analysed"]
    assert kwargs["categorie_conservee"] is True
    assert kwargs["divergence_categorie"] is True
    assert kwargs["categorie_remplacee"] is False
    assert kwargs["confirmation_precedente"] is False
    # A run over a confirmed analysis says so — a bool, never who.
    seen.clear()
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    _analyse_tool("qualifie")
    [(args, kwargs)] = [s for s in seen if s[0][0] == "mcp_document_analysed"]
    assert kwargs["confirmation_precedente"] is True


def test_list_documents_flags_the_gap(fake):
    """FAILS on the old handler: the row had no `divergence_categorie`."""
    _record("choisi")
    _record("defaut")
    rows = {r["id"]: r for r in handlers.list_documents(
        {"dossier_id": "d1"})["items"]}
    assert rows["choisi"]["divergence_categorie"] is True
    assert rows["choisi"]["nature_detectee"] == "procédure"
    assert rows["choisi"]["category"] == "correspondance"
    assert rows["defaut"]["divergence_categorie"] is False
    assert rows["presume"]["divergence_categorie"] is False   # never analysed


# ── update_document: the lawyer's rule is judged FIRST ───────────────────


def _qualified_and_confirmed(fake, did):
    """An analysed document the lawyer CONFIRMED — his category (D18 marker
    and D25 alike) AND an analysis."""
    _doc(fake, did, category="correspondance", category_source="analyse",
         analyse=document_model._analyse_derivee(
             {"sous_nature": "CORR_TIERS"}, document={"category": "autre"})[0])
    doc, errors = document_model.confirmer_analyse(did, "me@cabinet.ca")
    assert not errors, errors


def test_on_his_analysed_document_the_refusal_says_tell_him(fake):
    """FAILS on the old handler, which checked the analysis first and sent
    the caller to record_document_analysis — a path that now keeps his
    category too."""
    _qualified_and_confirmed(fake, "qualifie")
    before = fake.peek("documents/qualifie")
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_document({"document_id": "qualifie",
                                  "category": "preuve"})
    text = str(excinfo.value)
    assert "dites-le au juriste" in text
    assert "Une analyse la garderait aussi" in text
    assert "enregistrez une nouvelle analyse" not in text
    assert fake.peek("documents/qualifie") == before


def test_the_model_judges_his_rule_before_the_analysis_rule(fake):
    """The same order in the model (plan rule 2): FAILS on the old code,
    which answered MCP_CATEGORY_ON_ANALYSED."""
    _qualified_and_confirmed(fake, "qualifie")
    with provenance.writing_via("mcp", tool="update_document"):
        doc, errors, changed = document_model.update_metadata(
            "qualifie", {"category": "preuve"}, source="mcp")
    assert doc is None and changed is False
    assert errors == [document_model.MCP_CATEGORY_ON_LAWYERS]


def test_an_analysed_document_that_is_not_his_still_points_to_the_analysis(fake):
    _record("defaut")                         # replaced: source « analyse »
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_document({"document_id": "defaut",
                                  "category": "preuve"})
    assert "enregistrez une nouvelle analyse" in str(excinfo.value)


# ── update_analyse: an edit of another field keeps his category ─────────


def test_editing_another_field_keeps_the_category_an_analysis_kept(fake):
    """FAILS on the old code: every lawyer edit of the analysis wrote
    `category = nature_detectee`, so correcting the author silently undid
    the category D25 had just kept for him."""
    _record("choisi")
    doc, errors = document_model.update_analyse(
        "choisi", {"auteur": "Un Toit en Réserve"}, par="me@cabinet.ca")
    assert errors == [], errors
    stored = fake.peek("documents/choisi")
    assert stored["category"] == "correspondance"
    a = stored["analyse"]
    assert a["confirme"] is True                           # éditer = confirmer
    assert a["categorie_conservee"] is True
    assert a["divergence_categorie"] is True
    assert document_model.analysis_category_divergence(stored) == "procédure"


def test_the_lawyers_edit_records_the_confirmation_it_replaces(fake):
    """His edit confirms (« éditer, c'est confirmer ») and, like a model
    run, records what confirmed the entry before it — never carried."""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    _, errors = document_model.update_analyse(
        "qualifie", {"auteur": "Un Toit en Réserve"}, par="autre@cabinet.ca")
    assert errors == [], errors
    a = fake.peek("documents/qualifie")["analyse"]
    assert a["confirme"] is True and a["confirme_par"] == "autre@cabinet.ca"
    assert a["confirmation_precedente"] == {"par": "me@cabinet.ca",
                                            "le": CONFIRMED_AT}


def test_requalifying_rederives_the_category(fake):
    """What the form tells him: changing the sub-nature re-derives it."""
    _record("choisi")
    _, errors = document_model.update_analyse(
        "choisi", {"sous_nature": "JUG_JUGEMENT"}, par="me@cabinet.ca")
    assert errors == [], errors
    stored = fake.peek("documents/choisi")
    assert stored["category"] == "jugement"
    assert stored["analyse"]["categorie_conservee"] is False
    assert stored["analyse"]["divergence_categorie"] is False
    assert stored["analyse"]["categorie_remplacee"] is True


# ── The document page shows it ────────────────────────────────────────────


@pytest.fixture
def client(fake):
    from flask import Flask

    from tz import to_mtl
    from utils.icons import ms

    with mock.patch("google.cloud.firestore.Client"):
        import routes.dossiers as dossiers_routes
        import routes.parties as parties_routes
        import routes.notes as notes_routes
        import routes.tasks as tasks_routes
        import routes.hearings as hearings_routes
        import routes.invoices as invoices_routes
        import routes.protocols as protocols_routes
        import routes.time_expenses as time_expenses_routes
        import routes.doc_templates as doc_templates_routes

    app = Flask(__name__, template_folder=str(_ATHENA / "templates"),
                static_folder=str(_ATHENA / "static"))
    app.secret_key = "t"
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms,
                                 csp_nonce="n")
    app.jinja_env.filters.update(to_mtl=to_mtl, jsattr=lambda v: v,
                                 phone=lambda v: v,
                                 cents_fr=lambda c: str(c),
                                 markdown=lambda v: v)
    for bp in (parties_routes.parties_bp, dossiers_routes.dossiers_bp,
               time_expenses_routes.time_expenses_bp,
               documents_routes.documents_bp, notes_routes.notes_bp,
               tasks_routes.tasks_bp, protocols_routes.protocols_bp,
               hearings_routes.hearings_bp,
               doc_templates_routes.doc_templates_bp,
               invoices_routes.invoices_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "u1"
        s["email"] = "test@example.com"
        s["expires_at"] = datetime(2099, 1, 1, tzinfo=UTC)
    return c


_NOTE = "L'analyse suggère la catégorie « Procédure » ; la vôtre est conservée."


def _flat(html: str) -> str:
    return " ".join(html.split())


def test_the_detail_page_shows_the_gap(client, fake):
    """FAILS on the old page, which had no such note (and on the old model,
    which left no gap to show)."""
    _record("choisi")
    _record("choisi")                          # two runs: the journal shows
    html = _flat(client.get("/documents/choisi").get_data(as_text=True))
    assert _NOTE in html
    assert "suggérait « procédure » (votre catégorie conservée)" in html


def test_the_gap_stays_visible_once_the_analysis_is_confirmed(client, fake):
    """The note sits OUTSIDE the « not confirmed » block: it is about the
    CATEGORY, not the analysis confirmation — once he confirms the run,
    the alerts fold and the note stays. (Rewritten deliberately in the
    review of D25, twice: it first pinned a kept confirmation folding the
    new run's alerts, then a kept confirmation at all; a re-analysis of
    his confirmed document now reads « Présumée ».)"""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    _record("qualifie")
    html = _flat(client.get("/documents/qualifie").get_data(as_text=True))
    assert "Présumée" in html
    assert _NOTE in html
    _, errors = document_model.confirmer_analyse("qualifie", "me@cabinet.ca")
    assert errors == [], errors
    html = _flat(client.get("/documents/qualifie").get_data(as_text=True))
    assert "Confirmée" in html and "Présumée" not in html
    assert _NOTE in html


# ── Review of D25: a confirmation never vouches for the next run ─────────


def _confirmed_then_reanalysed_with_an_alert(fake):
    """A document he confirmed, then re-analysed through the connector as a
    hearing minute that carries the judgment — the alert that makes appeal
    delays run, which a kept confirmation used to fold away unseen."""
    _doc(fake, "qualifie", category="correspondance",
         category_set_by_lawyer=True, analyse=_confirmed_analysis("CORR_TIERS"))
    _, errors = _record("qualifie", sous_nature="PV_AUDIENCE_JUGEMENT")
    assert errors == [], errors
    stored = fake.peek("documents/qualifie")
    assert stored["analyse"]["confirme"] is False         # a new run: presumed
    assert stored["analyse"]["alerte_dispositif_detecte"] is True
    return stored


_DISPOSITIF = "Paraît porter le jugement lui-même"


def test_a_reanalysis_of_his_confirmed_document_reads_presumed(client, fake):
    """FAILS on cc9de4e (the card read « Confirmée » and folded every alert
    of a run the lawyer never read) and on b93273e (the card read
    « Confirmée avant cette analyse » over a stored confirmation the run
    kept). The run is presumed, stored and shown so — the ordinary path:
    « Présumée », its alerts, « Confirmer »; the footer recalls that the
    PREVIOUS analysis was confirmed."""
    stored = _confirmed_then_reanalysed_with_an_alert(fake)
    html = _flat(client.get("/documents/qualifie").get_data(as_text=True))
    assert "Présumée" in html
    assert _DISPOSITIF in html
    assert "/documents/qualifie/analyse/confirmer" in html
    assert "Présumée partout tant que non confirmée." in html
    assert "Analyse précédente confirmée le 01/09/2026" in html
    for gone in ("Confirmée avant cette analyse",
                 "Votre confirmation précède cette analyse",
                 "avant cette analyse du"):
        assert gone not in html, gone
    assert stored["analyse"]["confirmation_precedente"] == {
        "par": "me@cabinet.ca", "le": CONFIRMED_AT}


def test_confirming_the_new_run_folds_its_alerts_again(client, fake):
    """The existing gesture settles it: « Confirmer » confirms THIS run, and
    the card is « Confirmée » again, alerts folded."""
    _confirmed_then_reanalysed_with_an_alert(fake)
    _, errors = document_model.confirmer_analyse("qualifie", "me@cabinet.ca")
    assert errors == [], errors
    html = _flat(client.get("/documents/qualifie").get_data(as_text=True))
    assert "Présumée" not in html
    assert _DISPOSITIF not in html
    assert "Confirmée" in html
    assert "Analyse précédente confirmée" not in html


def test_the_edit_form_says_a_reanalysis_is_presumed(client, fake):
    """The form's analysis summary reads « présumé » for a new run over his
    confirmed analysis — FAILS on the D25 code (the stored confirmation was
    kept, and the summary said « — confirmée avant cette analyse »)."""
    _confirmed_then_reanalysed_with_an_alert(fake)
    html = _flat(client.get("/documents/qualifie/edit").get_data(as_text=True))
    assert "(PV_AUDIENCE_JUGEMENT) — présumé" in html
    assert "confirmée avant cette analyse" not in html


def test_the_texts_say_a_new_analysis_is_always_presumed():
    """Review of D25 — the consent screen and INSTRUCTIONS say what the card
    and the model do: a new analysis is ALWAYS presumed, his confirmation of
    a previous one covering that one only; his CATEGORY is kept; the
    analysis also replaces a category nobody chose; and an analysed
    document's category is the analysis's only when it is not his.
    (Rewritten deliberately: it pinned « confirmée avant cette analyse »,
    the mark of a kept confirmation that no longer exists.)"""
    from mcp import disclosure

    consent = (_ATHENA / "templates" / "mcp" / "families" / "_analyse.html"
               ).read_text(encoding="utf-8").replace("&nbsp;", " ")
    flat = " ".join(consent.split())
    assert "toujours <strong>présumée</strong>, ses alertes affichées" in flat
    assert ("votre confirmation d'une analyse précédente couvrait celle-là, "
            "pas celle-ci") in flat
    assert "confirmée avant cette analyse" not in flat
    assert "avec votre confirmation" not in flat
    assert "celle-là est <strong>conservée</strong>" in flat
    families = {f.key: f for f in disclosure.FAMILIES}
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it.
    assert "never over the lawyer's" in families["analyse"].instructions_en
    from mcp import tools as _tools
    assert "or a default nobody chose" in (
        _tools.TOOLS["record_document_analysis"]["description"])
    update_doc = _tools.TOOLS["update_document"]["description"]
    assert "on any OTHER document that carries an analysis" in update_doc
    assert "an analysis keeps it too" in update_doc


def test_the_anterior_confirmation_special_case_is_gone():
    """Review of D25: a run never carries a confirmation, so nothing reads
    one as « anterior » any more — the helper, its route context and its
    template branches left together."""
    assert not hasattr(document_model, "analysis_confirmation_predates_run")
    for rel in ("routes/documents.py", "templates/documents/_analyse.html",
                "templates/documents/edit.html"):
        text = (_ATHENA / rel).read_text(encoding="utf-8")
        assert "confirmation_anterieure" not in text, rel
        assert "analysis_confirmation_predates_run" not in text, rel


@pytest.mark.parametrize("did", ["defaut", "concorde"])
def test_no_note_without_a_gap(client, fake, did):
    _record(did)
    html = _flat(client.get(f"/documents/{did}").get_data(as_text=True))
    assert "la vôtre est conservée" not in html


def test_the_note_uses_only_compiled_classes():
    """The note's classes exist in the compiled artifact (a class absent
    from it silently does not apply) — no recompile, no rehash."""
    import re

    css_files = sorted((_ATHENA / "static" / "vendor").glob("app.*.css"))
    assert css_files, "compiled Tailwind artifact not found"
    css = css_files[-1].read_text(encoding="utf-8")
    block = (_ATHENA / "templates" / "documents" / "_analyse.html").read_text(
        encoding="utf-8")
    start = block.index("{% if categorie_suggeree %}")
    note = block[start:block.index("{% endif %}", start)]
    [classes] = re.findall(r'class="([^"]+)"', note)
    for cls in classes.split():
        assert "." + cls.replace(":", "\\:") in css, cls
