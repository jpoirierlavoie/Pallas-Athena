"""Lot 2A, step T6 (model half) — reads that can say « unreadable ».

The connector's file reads (``list_templates``, ``list_documents`` with its
folder tree) must never answer « no template », « no such template »,
« no history » or « no folder » over an outage — a false statement a caller
acts on. The web keeps its fail-open reads (a page degrades, it does not
500); the models gain STRICT variants beside them:

* ``doc_template.get_template`` / ``list_templates`` / ``list_versions``
  with ``strict=True`` raise :class:`TemplateReadError`;
* ``folder.list_dossier_folders`` propagates, where ``get_folder_tree``
  fails open; ``build_folder_tree`` is its pure builder, and
  ``system_roles`` names the system folders by the WRITERS' rule;
* the generation service's READ half (``services/gabarit_champs.py``) is
  its own module, re-exported by ``services/gabarits.py``: one resolver for
  the popup and the connector, and a read tool that imports it provably
  reaches no writer (the « never » sweep reads a reached service whole).
"""

import ast
import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.doc_template as tpl_model
    import models.folder as folder_model
    import services.gabarit_champs as champs
    import services.gabarits as sg

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_ATHENA = pathlib.Path(__file__).resolve().parents[1]
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _docx(names) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{{{{{n}}}}}</w:t></w:r></w:p>" for n in names)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
    return buf.getvalue()


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


class _Unreadable:
    """A store every read of which fails."""

    def collection(self, *_a, **_k):
        raise RuntimeError("firestore unavailable")


@pytest.fixture
def template(monkeypatch):
    install(monkeypatch, *_fake_modules())
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: FakeBucket())
    data = _docx(["dossier.titre", "client.nom_complet", "date.aujourdhui",
                  "objet_lettre", "FAITS"])
    created, errors = tpl_model.create_template(
        io.BytesIO(data), "lettre.docx", len(data),
        {"name": "Lettre", "category": "correspondance"}, UID)
    assert errors == [], errors
    return created


# ══════════════════════════════════════════════════════════════════════
# 1. Templates: strict beside fail-open
# ══════════════════════════════════════════════════════════════════════


def test_the_strict_template_reads_find_what_the_default_reads_find(template):
    assert tpl_model.get_template(template["id"], strict=True)["name"] == "Lettre"
    assert tpl_model.get_template("absent", strict=True) is None     # absence ≠ error
    assert [t["id"] for t in tpl_model.list_templates(strict=True)] == [template["id"]]
    assert [v["version"] for v in tpl_model.list_versions(template["id"], strict=True)] == [1]


def test_an_outage_raises_under_strict_and_stays_quiet_on_the_web(template, monkeypatch):
    monkeypatch.setattr(tpl_model, "db", _Unreadable())
    # The web: unchanged, fail-open.
    assert tpl_model.list_templates() == []
    assert tpl_model.get_template(template["id"]) is None
    # A strict caller learns it could not read — never « none ».
    with pytest.raises(tpl_model.TemplateReadError):
        tpl_model.list_templates(strict=True)
    with pytest.raises(tpl_model.TemplateReadError):
        tpl_model.get_template(template["id"], strict=True)


def test_an_unreadable_history_raises_under_strict_only(template, monkeypatch):
    class _Broken:
        def order_by(self, *_a, **_k):
            raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(tpl_model, "_versions_ref", lambda tid: _Broken())
    assert tpl_model.list_versions(template["id"]) == []              # the pane
    with pytest.raises(tpl_model.TemplateReadError):
        tpl_model.list_versions(template["id"], strict=True)


# ══════════════════════════════════════════════════════════════════════
# 2. Folders: the strict flat read, the pure tree, the system roles
# ══════════════════════════════════════════════════════════════════════


def test_build_folder_tree_copies_and_sorts_without_touching_its_input():
    folders = [{"id": "b", "name": "beta", "parent_folder_id": None},
               {"id": "a", "name": "Alpha", "parent_folder_id": None},
               {"id": "c", "name": "enfant", "parent_folder_id": "a"},
               {"id": "d", "name": "orphelin", "parent_folder_id": "gone"},
               {"name": "sans identifiant", "parent_folder_id": None}]
    snapshot = json.dumps(folders)
    roots = folder_model.build_folder_tree(folders)
    assert [r["id"] for r in roots] == ["a", "b", "d"]     # dangling parent = a root
    assert [c["id"] for c in roots[0]["children"]] == ["c"]
    assert json.dumps(folders) == snapshot                  # input untouched


def test_get_folder_tree_is_unchanged_for_its_web_callers(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    for fid, name, parent in (("f1", "Zeta", None), ("f2", "alpha", None),
                              ("f3", "Enfant", "f1")):
        db.seed(f"folders/{fid}", {"id": fid, "dossier_id": "d1", "name": name,
                                    "parent_folder_id": parent})
    tree = folder_model.get_folder_tree("d1")
    assert [(n["id"], [c["id"] for c in n["children"]]) for n in tree] == [
        ("f2", []), ("f1", ["f3"])]
    assert [f["id"] for f in folder_model.list_dossier_folders("d1")] == ["f1", "f2", "f3"]


def test_list_dossier_folders_propagates_where_get_folder_tree_fails_open(monkeypatch):
    monkeypatch.setattr(folder_model, "db", _Unreadable())
    assert folder_model.get_folder_tree("d1") == []
    with pytest.raises(RuntimeError):
        folder_model.list_dossier_folders("d1")


def test_system_roles_follow_the_writers_rule():
    stamped_elsewhere = {"id": "s", "dossier_id": "d", "name": "Autre nom",
                         "parent_folder_id": None, "system_role": "projets"}
    legacy = {"id": "l", "dossier_id": "d", "name": "Projets",
              "parent_folder_id": None, "system_role": ""}
    # A stamped holder wins; the same-named legacy folder is then ordinary.
    assert folder_model.system_roles([stamped_elsewhere, legacy]) == {"s": "projets"}
    assert folder_model.system_roles([legacy]) == {"l": "projets"}
    nested = {**legacy, "id": "n", "parent_folder_id": "l"}
    assert folder_model.system_roles([nested]) == {}        # never below the root
    # One rule, two readers: what the reader reports is what the writers protect.
    assert folder_model.is_system_folder(legacy, [legacy])
    assert not folder_model.is_system_folder(legacy, [stamped_elsewhere, legacy])


# ══════════════════════════════════════════════════════════════════════
# 3. The generation service's read half
# ══════════════════════════════════════════════════════════════════════


def test_services_gabarits_reexports_the_read_half_as_one_resolver():
    for name in ("resolve_slots", "resolve_auto_values", "field_inventory",
                 "form_fields", "values_from_submission", "field_ceiling",
                 "passthrough_fields", "dossier_parties", "GenerationRefused",
                 "SlotResolution", "Inventory", "InventoryField",
                 "AUTO_MAX_CHARS", "MANUAL_MAX_CHARS", "MULTILINE_MAX_CHARS",
                 "FIELD_PREFIX", "DOSSIER_NOT_FOUND"):
        assert getattr(sg, name) is getattr(champs, name), name


def test_the_read_half_names_no_writer():
    """The « never » sweep reads a reached service WHOLE, and the
    « document » promise forbids upload_document / ensure_system_folder:
    the read half must not even name them, or a read tool importing it
    would break the promise without calling anything."""
    tree = ast.parse((_ATHENA / "services" / "gabarit_champs.py").read_text(encoding="utf-8"))
    refs = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    refs |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    refs |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not refs & {"upload_document", "ensure_system_folder", "fill_docx",
                       "ingest_blob_as_document", "storage_identity"}


def test_the_inventory_names_the_slot_each_auto_field_reads(template):
    slots = champs.resolve_slots()
    by_name = {f.name: f for f in champs.field_inventory(template, slots).fields}
    assert by_name["dossier.titre"].slot == "dossier"
    assert by_name["client.nom_complet"].slot == "client"
    assert by_name["date.aujourdhui"].slot == ""            # the date: no slot
    assert by_name["date.aujourdhui"].resolved is True       # whatever the slots
    assert by_name["objet_lettre"].slot == "" and by_name["FAITS"].slot == ""
