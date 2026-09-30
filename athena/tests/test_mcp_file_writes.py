"""Les écritures FICHIERS du connecteur (lot 2A, étape T7) : update_document,
move_documents, manage_folder.

Tout passe par le VRAI client Firestore sur le faux serveur partagé
(``tests/_fake_firestore.py``), les vrais modèles, les vrais gestionnaires
et le vrai protocole d'écriture (``run_write``) ; on relit ce qui est
STOCKÉ, jamais un dictionnaire remis à un faux. La concurrence (etag périmé,
lecture propre du gestionnaire, édition enchaînée) est épinglée pour les
deux outils à etag par le banc dérivé de ``tests/test_concurrency_models.py``
— qui échoue tant qu'un outil acceptant ``expected_etag`` n'y figure pas.

On épingle, pour chaque outil :
* ce qu'il écrit — et ce qu'il n'écrit PAS (les notes internes du juriste,
  une valeur déjà stockée, une ligne déjà rangée) ;
* les refus, en français, qui nomment le champ sans citer ce qui a été
  envoyé ;
* la D15 : une catégorie posée par Claude est PRÉSUMÉE, et refusée sur un
  document analysé en nommant record_document_analysis ;
* les dossiers système, jamais renommés ni déplacés, leurs noms réservés à
  la racine ;
* le registre, les textes et les plafonds recopiés des modèles.
"""

import os
import pathlib
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import dav.sync as dav_sync  # its db is patched below
    import mcp.disclosure as disclosure
    import mcp.endpoint as endpoint
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support
    from models import document as document_model
    from models import folder as folder_model

from tests._fake_firestore import install  # noqa: E402

# Loaded for their side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so each must be imported — under the
# Firestore mock — before a test installs it.
_LOADED_UNDER_FAKE = (dav_sync, write_support)

UTC = timezone.utc
DT = datetime(2026, 3, 4, tzinfo=UTC)
PROJETS = folder_model.system_folder_id("d1", "projets")


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


def _doc(fake, did, *, dossier="d1", folder=None, **over):
    record = {**document_model._default_doc(), "id": did,
              "dossier_id": dossier, "display_name": f"Pièce {did}",
              "category": "autre", "category_source": "juriste",
              "notes_internes": "Texte du juriste.", "folder_id": folder,
              "tags": [], "created_at": DT, "updated_at": DT,
              "etag": f"e-{did}"}
    record.update(over)
    fake.seed(f"documents/{did}", record)


def _folder(fake, fid, name, *, dossier="d1", parent=None, **over):
    fake.seed(f"folders/{fid}", {
        "id": fid, "dossier_id": dossier, "name": name,
        "parent_folder_id": parent, "order": 0, "system_role": "",
        "etag": f"e-{fid}", "created_at": DT, "updated_at": DT, **over})


@pytest.fixture
def fake(monkeypatch):
    fake = install(monkeypatch, *_fake_modules())
    for did in ("d1", "d2"):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": f"2026-{did}",
                                      "title": f"Dossier {did}",
                                      "status": "actif"})
    _folder(fake, "f1", "Pièces")
    _folder(fake, "f2", "Expertises", parent="f1")
    _folder(fake, PROJETS, "Projets", system_role="projets")
    _folder(fake, "g1", "Autre dossier", dossier="d2")
    _doc(fake, "a")
    _doc(fake, "b", folder="f1")
    _doc(fake, "c", dossier="d2")
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    assert errors == []
    _doc(fake, "an", analyse=champ, category=champ["nature_detectee"],
         category_source="analyse")
    return fake


def _writes(fake) -> list:
    return [op for c in fake.commits for op in c.ops
            if not op[1].startswith("mcp_idempotency/")]


def _refused(call, args) -> str:
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        call(args)
    return str(excinfo.value)


# ══════════════════════════════════════════════════════════════════════
# 1. update_document
# ══════════════════════════════════════════════════════════════════════


def test_update_document_replaces_what_it_names_and_nothing_else(fake):
    result = handlers.update_document({
        "document_id": "a", "display_name": "Mise en demeure",
        "document_date": "2026-02-10", "tags": ["  urgent ", "client"],
        "expected_etag": "e-a"})

    stored = fake.peek("documents/a")
    assert stored["display_name"] == "Mise en demeure"
    assert stored["document_date"] == datetime(2026, 2, 10, tzinfo=UTC)
    assert stored["tags"] == ["urgent", "client"]      # trimmed, as the form does
    assert stored["notes_internes"] == "Texte du juriste."   # never touched
    assert stored["category"] == "autre" and stored["category_source"] == "juriste"
    assert stored["updated_via"] == "mcp" and stored["etag"] != "e-a"
    assert result["changed_fields"] == ["display_name", "document_date", "tags"]
    assert result["entity"]["etag"] == stored["etag"]
    assert result["entity"]["document_date"] == "2026-02-10"
    assert result["idempotent_replay"] is False
    # Never the lawyer's text, never a filename or a path.
    assert "notes_internes" not in str(result) and "Texte du juriste" not in str(result)


def test_a_category_set_by_claude_is_stored_presumed(fake):
    result = handlers.update_document({"document_id": "a",
                                       "category": "correspondance"})
    stored = fake.peek("documents/a")
    assert stored["category"] == "correspondance"
    assert stored["category_source"] == "mcp"
    assert result["entity"]["category_presumee"] is True
    assert result["entity"]["category_source"] == "mcp"
    # REWRITTEN deliberately (D18, fixups of lot 2A): « a » carries an
    # untouched « autre » — replaceable, and no longer called « un choix du
    # juriste » (a category the lawyer chose is now REFUSED:
    # tests/test_document_category_lawyer.py).
    assert any("ni comme choisie ni comme confirmée" in w and "PRÉSUMÉE" in w
               for w in result["warnings"])
    assert stored["category_set_by_lawyer"] is False
    assert result["entity"]["category_set_by_lawyer"] is False


def test_an_analysed_documents_category_is_refused_naming_the_analysis_tool(fake):
    before = fake.peek("documents/an")
    message = _refused(handlers.update_document,
                       {"document_id": "an", "category": "preuve"})
    assert "record_document_analysis" in message and "Rien n'a été modifié" in message
    assert fake.peek("documents/an") == before
    # The same category is no change, and the other fields stay writable.
    same = handlers.update_document({"document_id": "an",
                                     "category": before["category"],
                                     "display_name": "Lettre au client"})
    assert same["changed_fields"] == ["display_name"]
    assert fake.peek("documents/an")["category_source"] == "analyse"


def test_an_analysis_landing_before_the_commit_refuses_the_category(fake, monkeypatch):
    """The handler read an UNanalysed document; an analysis lands before the
    model's transaction — here WITHOUT a new etag (a maintenance script does
    not regenerate one), so the version check cannot catch it. The MODEL's
    D15 rule refuses on its own transactional read, and the connector says
    so in its own words, naming the tool to use — never a bare model error.
    (With a new etag, the version check refuses first: the derived bench.)"""
    real = document_model.get_document_strict
    champ = fake.peek("documents/an")["analyse"]

    def racing(document_id):
        result = real(document_id)
        fake.external_write("documents/a", {
            **fake.peek("documents/a"), "analyse": champ,
            "category_source": "analyse"})
        return result

    monkeypatch.setattr(document_model, "get_document_strict", racing)
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        handlers.update_document({"document_id": "a", "category": "preuve",
                                  "expected_etag": "e-a"})
    assert "record_document_analysis" in str(excinfo.value)
    assert fake.peek("documents/a")["category"] == "autre"
    assert fake.peek("documents/a")["category_source"] == "analyse"


def test_a_malformed_date_is_refused_and_an_empty_one_clears(fake):
    handlers.update_document({"document_id": "a", "document_date": "2026-02-10"})
    message = _refused(handlers.update_document,
                       {"document_id": "a", "document_date": "10/02/2026"})
    assert "YYYY-MM-DD" in message
    assert fake.peek("documents/a")["document_date"] == datetime(2026, 2, 10, tzinfo=UTC)
    handlers.update_document({"document_id": "a", "document_date": ""})
    assert fake.peek("documents/a")["document_date"] is None


@pytest.mark.parametrize("tags, fragment", [
    (["a,b"], "virgule"),
    (["x", "x"], "déjà dans la liste"),
    (["   "], "vide"),
    (["<b>gras</b>"], "chevrons"),
])
def test_a_tag_is_refused_never_repaired(fake, tags, fragment):
    before = fake.peek("documents/a")
    message = _refused(handlers.update_document,
                       {"document_id": "a", "tags": tags})
    assert fragment in message
    assert "n° 1" in message or "n° 2" in message
    assert fake.peek("documents/a") == before


def test_the_schema_refuses_the_lawyers_notes_and_the_legacy_category():
    schema = tools.TOOLS["update_document"]["input_schema"]
    errors = tools.validate_args(schema, {"document_id": "a",
                                          "notes_internes": "x"})
    assert any("notes_internes" in e for e in errors)
    errors = tools.validate_args(schema, {"document_id": "a",
                                          "category": "procès_verbal"})
    assert errors
    too_many = [f"t{i}" for i in range(tools.DOCUMENT_TAGS_MAX + 1)]
    assert tools.validate_args(schema, {"document_id": "a", "tags": too_many})


def test_a_refile_lands_in_the_same_commit_as_the_rename(fake):
    fake.reset_logs()
    result = handlers.update_document({"document_id": "a", "folder_id": "f2",
                                       "display_name": "Rapport"})
    stored = fake.peek("documents/a")
    assert stored["folder_id"] == "f2" and stored["display_name"] == "Rapport"
    assert result["changed_fields"] == ["display_name", "folder_id"]
    assert result["entity"]["folder_id"] == "f2"
    written = [c for c in fake.commits
               if any(p == "documents/a" for _k, p in c.ops)]
    assert len(written) == 1
    # Filing INTO a system folder is allowed; "" is the root.
    handlers.update_document({"document_id": "a", "folder_id": PROJETS})
    assert fake.peek("documents/a")["folder_id"] == PROJETS
    handlers.update_document({"document_id": "a", "folder_id": ""})
    assert fake.peek("documents/a")["folder_id"] is None


@pytest.mark.parametrize("folder_id", ["g1", "inconnu", "f1/x/y"])
def test_a_folder_outside_the_documents_dossier_is_refused(fake, folder_id):
    before = fake.peek("documents/a")
    message = _refused(handlers.update_document,
                       {"document_id": "a", "folder_id": folder_id,
                        "display_name": "Nouveau"})
    assert "`folder_id` refusé" in message
    assert fake.peek("documents/a") == before


def test_values_already_stored_write_nothing(fake):
    fake.reset_logs()
    result = handlers.update_document({
        "document_id": "b", "display_name": "Pièce b", "folder_id": "f1",
        "category": "autre", "tags": []})
    assert result["changed_fields"] == []
    assert any("rien n'a été modifié" in w for w in result["warnings"])
    assert _writes(fake) == []
    assert result["entity"]["etag"] == "e-b"


def test_an_unknown_a_slashed_and_an_unreadable_document(fake, monkeypatch):
    for bad in ("inconnu", "a/analyses/j1"):
        message = _refused(handlers.update_document,
                           {"document_id": bad, "display_name": "X"})
        assert "Document introuvable" in message and bad not in message

    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(fake._fake_server, "batch_get_documents", _boom)
    message = _refused(handlers.update_document,
                       {"document_id": "a", "display_name": "X"})
    assert "Lecture du document impossible" in message


@pytest.mark.parametrize("bad", ["__reserve__", ".", ".."])
def test_an_id_firestore_refuses_is_absent_never_a_store_failure(fake, bad):
    """Review of T7 — fails on the reviewed code: the client builds a
    reference to « __reserve__ », « . » or « .. » without complaint and the
    SERVER refuses the RPC, so the strict reader answered « Lecture du
    document impossible — réessayez » for an id no retry will ever read,
    and a refile into such a folder id surfaced as a failed save."""
    message = _refused(handlers.update_document,
                       {"document_id": bad, "display_name": "X"})
    assert "Document introuvable" in message
    assert "réessayez" not in message

    fake.reset_logs()
    message = _refused(handlers.update_document,
                       {"document_id": "a", "folder_id": bad})
    assert message == handlers._DOCUMENT_FOLDER_UNKNOWN
    assert _writes(fake) == []


def test_update_document_demands_a_field(fake):
    message = _refused(handlers.update_document, {"document_id": "a"})
    assert "Aucun champ" in message and "notes internes" in message


def test_update_document_replays_its_key(fake):
    args = {"document_id": "a", "display_name": "Rapport",
            "idempotency_key": "cle-classement-1"}
    first = handlers.update_document(dict(args))
    second = handlers.update_document(dict(args))
    assert second["idempotent_replay"] is True
    assert second["entity"]["etag"] == first["entity"]["etag"]


# ══════════════════════════════════════════════════════════════════════
# 2. move_documents
# ══════════════════════════════════════════════════════════════════════


def test_move_documents_answers_each_id_in_order(fake, caplog):
    import logging

    caplog.set_level(logging.INFO)
    fake.reset_logs()
    result = handlers.move_documents({
        "dossier_id": "d1", "document_ids": ["a", "c", "b", "inconnu"],
        "folder_id": "f1"})

    assert [(r["document_id"], r["outcome"]) for r in result["results"]] == [
        ("a", "moved"), ("c", "refused"), ("b", "unchanged"),
        ("inconnu", "refused")]
    assert (result["moved"], result["unchanged"], result["refused"],
            result["requested"]) == (1, 1, 2, 4)
    assert result["target"] == {"folder_id": "f1", "path": "Pièces",
                                "system_role": ""}
    assert result["results"][0]["previous_folder_id"] is None
    assert result["results"][0]["etag"] == fake.peek("documents/a")["etag"]
    assert result["results"][2]["etag"] == "e-b"          # unchanged, same etag
    assert result["results"][1]["etag"] is None
    assert result["results"][1]["reason"] == document_model.MOVE_OTHER_DOSSIER
    assert fake.peek("documents/c")["folder_id"] is None
    assert any("NOUVELLE idempotency_key" in w for w in result["warnings"])
    # The counts line — counts only, never an id list.
    lines = [r for r in caplog.records
             if getattr(r, "json_fields", {}).get("event") == "mcp_documents_moved"]
    assert len(lines) == 1
    fields = lines[0].json_fields
    assert (fields["moved"], fields["refused"]) == (1, 2)
    assert "inconnu" not in str(fields)


def test_move_documents_into_a_system_folder_and_to_the_root(fake):
    result = handlers.move_documents({"dossier_id": "d1",
                                      "document_ids": ["a"],
                                      "folder_id": PROJETS})
    assert result["target"]["system_role"] == "projets"
    assert fake.peek("documents/a")["folder_id"] == PROJETS
    result = handlers.move_documents({"dossier_id": "d1",
                                      "document_ids": ["a", "b"],
                                      "folder_id": ""})
    assert result["target"] == {"folder_id": None, "path": "",
                                "system_role": ""}
    assert fake.peek("documents/b")["folder_id"] is None


def test_an_id_named_twice_refuses_the_whole_call_by_position(fake):
    fake.reset_logs()
    message = _refused(handlers.move_documents, {
        "dossier_id": "d1", "document_ids": ["a", "b", "a"],
        "folder_id": "f1"})
    assert "positions 1 et 3" in message and "Rien n'a été déplacé" in message
    assert _writes(fake) == []


@pytest.mark.parametrize("folder_id", ["g1", "inconnu"])
def test_an_unknown_target_refuses_before_any_write(fake, folder_id):
    fake.reset_logs()
    message = _refused(handlers.move_documents, {
        "dossier_id": "d1", "document_ids": ["a"], "folder_id": folder_id})
    assert "`folder_id`" in message and "Rien n'a été déplacé" in message
    assert _writes(fake) == []


def test_move_documents_refuses_an_unknown_dossier_and_an_unreadable_tree(
    fake, monkeypatch,
):
    assert "Dossier introuvable" in _refused(handlers.move_documents, {
        "dossier_id": "d9", "document_ids": ["a"], "folder_id": ""})

    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(fake._fake_server, "run_query", _boom)
    message = _refused(handlers.move_documents, {
        "dossier_id": "d1", "document_ids": ["a"], "folder_id": "f1"})
    assert "Lecture des dossiers de classement impossible" in message
    assert fake.peek("documents/a")["folder_id"] is None


def test_one_id_firestore_refuses_is_one_refused_row_not_a_sunk_batch(fake):
    """Review of T7 — fails on the reviewed code: « __x__ » reached the
    transaction's batched read, the server refused the RPC, and the WHOLE
    move failed as « Erreur lors du déplacement » (with a traceback logged
    as an unexpected store failure) — the good row never moved."""
    result = handlers.move_documents({
        "dossier_id": "d1", "document_ids": ["a", "__x__", ".."],
        "folder_id": "f1"})

    assert [(r["document_id"], r["outcome"]) for r in result["results"]] == [
        ("a", "moved"), ("__x__", "refused"), ("..", "refused")]
    assert result["results"][1]["reason"] == document_model.MOVE_NOT_FOUND
    assert fake.peek("documents/a")["folder_id"] == "f1"


def test_get_document_text_never_reads_a_deeper_record(fake):
    """Review of T7 — fails on the reviewed code: the fail-open reader
    behind the connector's document READS had no slash guard, so
    ``get_document_text`` answered ``found: true`` for an entry of a
    document's analysis journal."""
    fake.seed("documents/a/analyses/j1", {
        "analyse_id": "j1", "dossier_id": "d1",
        "file_type": "application/pdf"})

    assert document_model.get_document("a/analyses/j1") is None
    result = handlers.get_document_text({"document_id": "a/analyses/j1"})
    assert result["found"] is False


def test_the_move_schema_bounds_the_batch():
    schema = tools.TOOLS["move_documents"]["input_schema"]
    base = {"dossier_id": "d1", "folder_id": ""}
    assert tools.validate_args(schema, {**base, "document_ids": []})
    too_many = [f"x{i}" for i in range(tools.DOCUMENT_MOVE_MAX + 1)]
    assert tools.validate_args(schema, {**base, "document_ids": too_many})
    assert tools.validate_args(schema, {**base, "document_ids": ["a"]}) == []


def test_move_documents_repeated_writes_nothing(fake):
    args = {"dossier_id": "d1", "document_ids": ["a"], "folder_id": "f1"}
    handlers.move_documents(dict(args))
    fake.reset_logs()
    again = handlers.move_documents(dict(args))
    assert again["unchanged"] == 1 and again["moved"] == 0
    assert _writes(fake) == []


# ══════════════════════════════════════════════════════════════════════
# 3. manage_folder
# ══════════════════════════════════════════════════════════════════════


def test_create_a_folder_at_the_root_and_below(fake):
    root = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                   "name": "Correspondance"})
    assert root["outcome"] == "created" and root["entity"]["path"] == "Correspondance"
    stored = fake.peek(f"folders/{root['entity']['id']}")
    assert stored["created_via"] == "mcp" and stored["parent_folder_id"] is None
    below = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                    "name": "Rapports", "parent_folder_id": "f2"})
    assert below["entity"]["path"] == "Pièces / Expertises / Rapports"
    assert below["entity"]["etag"] == fake.peek(
        f"folders/{below['entity']['id']}")["etag"]


def test_a_taken_name_is_refused_or_reused_on_request(fake):
    fake.reset_logs()
    message = _refused(handlers.manage_folder, {
        "action": "create", "dossier_id": "d1", "name": "pièces"})
    assert "if_exists « reuse »" in message and "Rien n'a été créé" in message
    reused = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                     "name": " PIÈCES ", "if_exists": "reuse"})
    assert reused["outcome"] == "reused" and reused["entity"]["id"] == "f1"
    assert reused["changed_fields"] == []
    assert _writes(fake) == []


@pytest.mark.parametrize("name, fragment", [
    ("Projets", "réservé"),
    ("Reçus du portail", "réservé"),
    ("<Brouillons>", "chevrons"),
    ("a/b", "/"),
])
def test_a_create_name_is_refused_never_altered(fake, name, fragment):
    before = fake.peek_collection("folders")
    message = _refused(handlers.manage_folder,
                       {"action": "create", "dossier_id": "d1", "name": name})
    assert fragment in message
    assert fake.peek_collection("folders") == before


def test_rename_and_move_an_ordinary_folder(fake):
    renamed = handlers.manage_folder({"action": "rename", "dossier_id": "d1",
                                      "folder_id": "f2", "name": "Experts",
                                      "expected_etag": "e-f2"})
    assert renamed["outcome"] == "renamed"
    assert renamed["changed_fields"] == ["name"]
    assert renamed["entity"]["path"] == "Pièces / Experts"
    assert fake.peek("folders/f2")["name"] == "Experts"
    moved = handlers.manage_folder({"action": "move", "dossier_id": "d1",
                                    "folder_id": "f2", "parent_folder_id": ""})
    assert moved["outcome"] == "moved" and moved["entity"]["path"] == "Experts"
    assert fake.peek("folders/f2")["parent_folder_id"] is None
    assert fake.peek("folders/f2")["updated_via"] == "mcp"


@pytest.mark.parametrize("action, extra", [
    ("rename", {"name": "Brouillons"}),
    ("move", {"parent_folder_id": "f1"}),
])
def test_a_system_folder_is_never_renamed_or_moved(fake, action, extra):
    before = fake.peek(f"folders/{PROJETS}")
    message = _refused(handlers.manage_folder, {
        "action": action, "dossier_id": "d1", "folder_id": PROJETS, **extra})
    assert "dossier système" in message
    assert fake.peek(f"folders/{PROJETS}") == before


def test_a_legacy_projets_is_protected_too(fake):
    """A « Projets » created by NAME before system roles (no stamp) is THE
    system folder of a dossier that has no stamped one."""
    _folder(fake, "leg", "Projets", dossier="d2")
    message = _refused(handlers.manage_folder, {
        "action": "rename", "dossier_id": "d2", "folder_id": "leg",
        "name": "Brouillons"})
    assert "dossier système" in message


def test_same_name_and_same_parent_write_nothing(fake):
    fake.reset_logs()
    same = handlers.manage_folder({"action": "rename", "dossier_id": "d1",
                                   "folder_id": "f1", "name": "Pièces"})
    assert same["outcome"] == "unchanged" and same["changed_fields"] == []
    here = handlers.manage_folder({"action": "move", "dossier_id": "d1",
                                   "folder_id": "f2", "parent_folder_id": "f1"})
    assert here["outcome"] == "unchanged"
    assert _writes(fake) == []


@pytest.mark.parametrize("args, fragment", [
    ({"action": "move", "folder_id": "f1", "parent_folder_id": "f2"},
     "sous-dossiers"),
    ({"action": "move", "folder_id": "f1", "parent_folder_id": "f1"},
     "lui-même"),
    ({"action": "move", "folder_id": "f1", "parent_folder_id": "g1"},
     "destination"),
    ({"action": "rename", "folder_id": "f2", "name": "Pièces"}, None),
    ({"action": "rename", "folder_id": "g1", "name": "X"}, "introuvable"),
])
def test_folder_edits_refused_by_the_model_write_nothing(fake, args, fragment):
    before = fake.peek_collection("folders")
    if fragment is None:
        # Renaming f2 « Pièces » under f1 is legal (the root « Pièces » is
        # its PARENT, not a sibling) — the control case.
        result = handlers.manage_folder({"dossier_id": "d1", **args})
        assert result["outcome"] == "renamed"
        return
    message = _refused(handlers.manage_folder, {"dossier_id": "d1", **args})
    assert fragment in message
    assert fake.peek_collection("folders") == before


@pytest.mark.parametrize("args, fragment", [
    ({"action": "create", "name": "X", "expected_etag": "e"}, "`expected_etag`"),
    ({"action": "create", "name": "X", "folder_id": "f1"}, "`folder_id`"),
    ({"action": "rename", "folder_id": "f1", "name": "X",
      "if_exists": "reuse"}, "`if_exists`"),
    ({"action": "move", "folder_id": "f1", "parent_folder_id": "",
      "name": "X"}, "`name`"),
    ({"action": "rename", "folder_id": "f1"}, "`name` est requis"),
    ({"action": "move", "folder_id": "f1"}, "`parent_folder_id` est requis"),
    ({"action": "create"}, "`name` est requis"),
])
def test_an_argument_of_another_action_is_refused_never_ignored(fake, args, fragment):
    message = _refused(handlers.manage_folder, {"dossier_id": "d1", **args})
    assert fragment in message


def test_an_unreadable_tree_refuses_a_rename(fake, monkeypatch):
    def _boom(*_a, **_k):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(fake._fake_server, "run_query", _boom)
    message = _refused(handlers.manage_folder, {
        "action": "rename", "dossier_id": "d1", "folder_id": "f1",
        "name": "Autre"})
    assert "Lecture des dossiers de classement impossible" in message
    assert fake.peek("folders/f1")["name"] == "Pièces"


def test_a_created_folder_survives_an_unreadable_tree_after_its_commit(
    fake, monkeypatch,
):
    """The folder is COMMITTED: a failed path re-read must never turn it
    into a refusal (the retry would then be refused as a duplicate)."""
    real = folder_model.list_dossier_folders

    def _boom(dossier_id):
        raise gexc.ServiceUnavailable("firestore indisponible")

    monkeypatch.setattr(folder_model, "list_dossier_folders", _boom)
    result = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                     "name": "Nouveau"})
    assert result["outcome"] == "created" and result["entity"]["path"] is None
    monkeypatch.setattr(folder_model, "list_dossier_folders", real)
    assert any(f.get("name") == "Nouveau"
               for f in fake.peek_collection("folders").values())


# ══════════════════════════════════════════════════════════════════════
# 4. Le registre et les textes
# ══════════════════════════════════════════════════════════════════════


def test_the_ceilings_are_the_models():
    assert tools.DOCUMENT_NAME_MAX_CHARS == document_model.DISPLAY_NAME_MAX
    assert tools.DOCUMENT_TAG_MAX_CHARS == document_model.TAG_MAX
    assert tools.DOCUMENT_TAGS_MAX <= document_model.TAGS_MAX_ITEMS
    assert tools.DOCUMENT_MOVE_MAX == document_model.MOVE_BULK_MAX
    assert tools.FOLDER_NAME_MAX_CHARS == folder_model.MAX_NAME_LENGTH
    props = tools.TOOLS["update_document"]["input_schema"]["properties"]
    assert props["category"]["enum"] == list(document_model.CATEGORY_CHOICES)
    assert "procès_verbal" not in props["category"]["enum"]


def test_the_files_family_and_its_promises():
    # REWRITTEN in lot 2A (T8): the family gained the two generations, and
    # the « document » NEVER no longer forbids ADDING a file (a generation
    # or a copy is a NEW document) — only changing an existing one's bytes,
    # which the connector cannot express: the GCS byte verbs are forbidden
    # to every connector module and reached service, and the two creators
    # it reaches write a new object, create-only
    # (tests/test_mcp_generation.py pins the behaviour).
    # T9 adds the upload ticket (begin_upload / finalize_upload): an upload
    # is filed as a NEW document too, so the « document » NEVER holds.
    # T10 adds the template writes (create_template / update_template): the
    # source document is only READ, so the « document » NEVER holds again
    # (tests/test_mcp_template_writes.py pins the behaviour).
    # REWRITTEN in lot 2A (T11): the two template writes left FILES for their
    # own family, TEMPLATES — the partition still covers every write tool
    # (tests/test_mcp_disclosure.py), and each family names its members.
    family = next(f for f in disclosure.FAMILIES if f.key == "files")
    assert set(family.tools) == {"update_document", "move_documents",
                                 "manage_folder", "fill_gabarit",
                                 "create_document", "begin_upload",
                                 "finalize_upload"}
    templates = next(f for f in disclosure.FAMILIES if f.key == "templates")
    assert set(templates.tools) == {"create_template", "update_template"}
    text = endpoint.INSTRUCTIONS
    assert "FILES: " in text and "PRESUMED" in text
    # REWRITTEN deliberately (finitions, contracts-1 part 2): the family paragraph became a one-line INSTRUCTIONS index entry; the fact is pinned in the tool description that now carries it.
    assert "« Projets »" in tools.TOOLS["fill_gabarit"]["description"]
    assert "notes_internes" in tools.TOOLS["update_document"]["description"]
    keys = {n.key: n for n in disclosure.NEVERS}
    assert "update_metadata" not in keys["document"].forbidden
    assert {"upload_from_file", "upload_from_string", "rewrite"} <= set(
        keys["document"].forbidden)
    assert "never adds a new file" not in text
    assert {"confirmer_analyse", "update_analyse"} <= set(
        keys["confirm"].forbidden)
    assert "its name and its folder are read-only" not in text


def test_the_file_edits_carry_the_hints_they_owe():
    for name in ("update_document", "move_documents", "manage_folder"):
        assert name in tools.WRITE_TOOLS and name in tools.EDIT_TOOLS, name
    assert tools.TOOLS["move_documents"]["concurrency"] == tools.CONCURRENCY_EXEMPT
    for name in ("update_document", "manage_folder"):
        assert tools.TOOLS[name]["etag_readers"] == ("list_documents",)
