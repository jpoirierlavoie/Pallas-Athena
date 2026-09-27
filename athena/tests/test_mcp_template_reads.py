"""Lot 2A, step T6 — the connector's two FILE reads, against the real store.

``list_templates`` (new): the list of the practice's gabarits, or one
gabarit's placeholder inventory — auto fields with a RESOLVED FLAG and
never a value (SPEC H.4 D5), manual fields with their options (the
« (aucune mention) » sentinel said in words), the blocs left to write, the
versions, the « actif » designation the lawyer alone sets.

``list_documents`` (additive): each row's ``category_source`` and the
system role of its folder, and — with ``include_folders`` — the dossier's
whole folder tree, the etag reader of the folder edits to come.

Every test runs the REAL handler over the REAL models, on the shared fake
Firestore (``tests/_fake_firestore.py``) and the fake bucket
(``tests/_fake_gcs.py``): what a test asserts is what the store holds.
"""

import ast
import io
import json
import os
import pathlib
import sys
import zipfile
from datetime import datetime, timezone
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools
    import models.doc_template as tpl_model
    import models.folder as folder_model
    import services.gabarit_champs as champs
    from mcp.output_schemas import OUTPUT_SCHEMAS

from tests._fake_firestore import install  # noqa: E402
from tests._fake_gcs import FakeBucket  # noqa: E402
from utils.deadlines import today_mtl  # noqa: E402
from utils.template_fields import EMPTY_OPTION_VALUE, french_long_date  # noqa: E402

UTC = timezone.utc
UID = "kX9pQ2rT7vW1yZ3bD5fH8jL0nP4s"
_ATHENA = pathlib.Path(__file__).resolve().parents[1]
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_CT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/></Types>'
)

PLACEHOLDERS = [
    "dossier.titre",
    "client.nom_complet",
    "adverse.nom_complet",
    "destinataire.nom_complet",
    "date.aujourdhui",
    "objet_lettre",
    "PRIVILÈGE",
    "FAITS",
    "civilité",
]


def _docx(names, extra: str = "") -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{{{{{n}}}}}</w:t></w:r></w:p>" for n in names)
    body += extra
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document {_W}><w:body>{body}'
                    "</w:body></w:document>")
    return buf.getvalue()


def _conforms(tool: str, payload) -> None:
    clean = tools._jsonable(payload)
    errors = tools.validate_args(OUTPUT_SCHEMAS[tool], clean)
    assert errors == [], f"{tool}: {errors}"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


def _individual(pid, first, last, street):
    return {
        "id": pid, "type": "individual", "contact_role": "client",
        "first_name": first, "last_name": last,
        "address_street": street, "address_unit": "", "address_city": "Laval",
        "address_province": "Québec", "address_postal_code": "H1A 1A1",
        "address_country": "Canada",
    }


def _entry(pid, name, roles):
    return {"id": pid, "name": name, "roles": roles,
            "avocat_id": "", "avocat_name": ""}


def _keys(node) -> set:
    """Every key anywhere in a JSON-like payload."""
    found = set()
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            found |= _keys(v)
    elif isinstance(node, list):
        for v in node:
            found |= _keys(v)
    return found


# The keys no file read may carry: the capability, the path, and the two
# filenames — the ORIGINAL one may embed a client's name (a template
# registered from a finished letter), which the leak scan never checks.
_NEVER_KEYS = {"filename", "original_filename", "storage_path", "sha256",
               "signed_url", "download_url", "url", "active_designated_by"}


@pytest.fixture
def store(monkeypatch):
    db = install(monkeypatch, *_fake_modules())
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    db.seed("parties/c1", _individual("c1", "Jean", "Tremblay", "1 rue Alpha"))
    db.seed("parties/c2", _individual("c2", "Marie", "Lavoie", "2 rue Bêta"))
    db.seed("parties/x9", _individual("x9", "Paul", "Gagnon", "9 rue Gamma"))
    db.seed("parties/a1", {
        "id": "a1", "type": "organization", "contact_role": "partie_adverse",
        "organization_name": "Omicron inc.", "work_address_street": "3 rue Delta",
        "work_address_city": "Québec", "work_address_province": "Québec",
        "work_address_postal_code": "G1A 1A1", "work_address_country": "Canada",
    })
    base = {"status": "actif", "forum_type": "judiciaire", "role": "demandeur",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
    db.seed("dossiers/d1", {
        **base, "id": "d1", "file_number": "2026-001", "title": "Dossier un",
        "clients": [_entry("c1", "Jean Tremblay", ["demandeur"]),
                    _entry("c2", "Marie Lavoie", ["demandeur"])],
        "client_ids": ["c1", "c2"],
        "opposing_parties": [_entry("a1", "Omicron inc.", ["défendeur"])],
        "opposing_party_ids": ["a1"],
    })
    db.seed("dossiers/d2", {
        **base, "id": "d2", "file_number": "2026-002", "title": "Dossier deux",
        "clients": [_entry("c1", "Jean Tremblay", ["demandeur"])],
        "client_ids": ["c1"], "opposing_parties": [], "opposing_party_ids": [],
    })
    data = _docx(PLACEHOLDERS)
    # The ORIGINAL filename names the client, as a template registered from
    # a finished letter would: it must never reach the connector.
    letter, errors = tpl_model.create_template(
        io.BytesIO(data), "Lettre Tremblay finale.docx", len(data),
        {"name": "Lettre type", "category": "correspondance", "kind": "gabarit",
         "description": "Lettre au client."}, UID,
    )
    assert errors == [], errors
    note_bytes = _docx(["note.titre"])
    note, errors = tpl_model.create_template(
        io.BytesIO(note_bytes), "note.docx", len(note_bytes),
        {"name": "Impression de note", "category": "autre", "kind": "note"}, UID,
    )
    assert errors == [], errors
    return db, bucket, letter, note


# ══════════════════════════════════════════════════════════════════════
# 1. list_templates — the list
# ══════════════════════════════════════════════════════════════════════


def test_the_list_shows_metadata_and_counts_and_never_the_file(store):
    _, _, letter, note = store
    payload = handlers.list_templates({})
    assert payload["mode"] == "list"
    assert [r["id"] for r in payload["items"]] == [note["id"], letter["id"]]  # by name
    row = payload["items"][1]
    assert (row["name"], row["kind"], row["category"], row["version"]) == (
        "Lettre type", "gabarit", "correspondance", 1)
    # Re-classified from the stored placeholders on every read.
    assert (row["placeholder_count"], row["auto_count"], row["manual_count"],
            row["bloc_count"]) == (9, 5, 2, 2)
    assert row["slots_required"] == ["adverse", "client", "destinataire", "dossier"]
    assert row["etag"] and row["active"] is False
    assert row["description"] == "Lettre au client."
    assert not _keys(payload) & _NEVER_KEYS
    assert "Tremblay" not in json.dumps(payload, ensure_ascii=False)
    _conforms("list_templates", payload)


def test_the_list_filters_by_kind_category_and_query(store):
    _, _, letter, note = store
    ids = lambda p: [r["id"] for r in p["items"]]  # noqa: E731
    assert ids(handlers.list_templates({"kind": "note"})) == [note["id"]]
    assert ids(handlers.list_templates({"kind": "gabarit"})) == [letter["id"]]
    assert ids(handlers.list_templates({"category": "correspondance"})) == [letter["id"]]
    assert ids(handlers.list_templates({"query": "client"})) == [letter["id"]]  # description
    assert ids(handlers.list_templates({"kind": "note_honoraires"})) == []


def test_the_list_pages_by_offset(store):
    first = handlers.list_templates({"limit": 1})
    assert (first["count"], first["truncated"], first["next_offset"]) == (1, True, 1)
    second = handlers.list_templates({"limit": 1, "offset": 1})
    assert second["truncated"] is False and "next_offset" not in second
    assert first["items"][0]["id"] != second["items"][0]["id"]
    _conforms("list_templates", first)


class _Unreadable:
    """A store every read of which fails — the outage a strict read must
    refuse rather than answer as « nothing »."""

    def collection(self, *_a, **_k):
        raise RuntimeError("firestore unavailable")


def test_an_unreadable_store_is_refused_never_listed_as_empty(store, monkeypatch):
    monkeypatch.setattr(tpl_model, "db", _Unreadable())
    with pytest.raises(tools.ToolArgumentError, match="Lecture des gabarits impossible"):
        handlers.list_templates({})
    with pytest.raises(tools.ToolArgumentError, match="Lecture des gabarits impossible"):
        handlers.list_templates({"template_id": "t1"})


# ══════════════════════════════════════════════════════════════════════
# 2. list_templates — one template's inventory
# ══════════════════════════════════════════════════════════════════════


def test_the_detail_reports_resolved_flags_never_values(store):
    _, _, letter, _ = store
    payload = handlers.list_templates({
        "template_id": letter["id"], "dossier_id": "d1", "client_id": "c2",
        "destinataire_id": "x9",
    })
    assert (payload["mode"], payload["found"]) == ("detail", True)
    assert payload["dossier"] == {"id": "d1", "file_number": "2026-001",
                                  "title": "Dossier un"}
    # The adverse slot defaults to the dossier's ONLY opposing party.
    assert payload["slots"] == {"client_id": "c2", "adverse_id": "a1",
                                "destinataire_id": "x9"}
    autos = {f["name"]: f for f in payload["auto_fields"]}
    assert set(autos) == {"dossier.titre", "client.nom_complet",
                          "adverse.nom_complet", "destinataire.nom_complet",
                          "date.aujourdhui"}
    assert all(f["resolved"] for f in autos.values())
    assert autos["client.nom_complet"]["slot"] == "client"
    assert autos["date.aujourdhui"]["slot"] is None      # firm/date: no slot
    assert payload["unresolved_auto_fields"] == []
    # SPEC H.4 D5: not ONE resolved value leaves the server.
    dump = json.dumps(payload, ensure_ascii=False)
    for leaked in ("Marie", "Lavoie", "Omicron", "Gagnon", "rue Bêta",
                   "rue Delta", "rue Gamma", french_long_date(today_mtl())):
        assert leaked not in dump, leaked
    assert not _keys(payload) & _NEVER_KEYS
    _conforms("list_templates", payload)


def test_manual_fields_carry_their_options_and_the_no_mention_choice_in_words(store):
    _, _, letter, _ = store
    payload = handlers.list_templates({"template_id": letter["id"], "dossier_id": "d2"})
    manual = {f["name"]: f for f in payload["manual_fields"]}
    assert manual["objet_lettre"] == {"name": "objet_lettre", "default": "",
                                      "uppercase": False, "options": None}
    privilege = manual["PRIVILÈGE"]                 # matched case-insensitively
    assert privilege["uppercase"] is True           # printed in capitals
    silent = [o for o in privilege["options"] if o["prints_nothing"]]
    assert silent == [{"label": "(aucune mention)", "value": EMPTY_OPTION_VALUE,
                       "prints_nothing": True}]
    assert all(o["value"] == o["label"] for o in privilege["options"]
               if not o["prints_nothing"])
    # The blocs are the template's EXACT spelling — a fill matches them so.
    assert payload["blocs"] == [{"name": "FAITS", "uppercase": True},
                                {"name": "civilité", "uppercase": False}]


def test_without_a_dossier_the_party_fields_read_unresolved_and_say_why(store):
    _, _, letter, _ = store
    payload = handlers.list_templates({"template_id": letter["id"]})
    autos = {f["name"]: f["resolved"] for f in payload["auto_fields"]}
    assert autos["client.nom_complet"] is False
    assert autos["date.aujourdhui"] is True          # resolves whatever the slots
    assert payload["dossier"] is None
    assert payload["slots"] == {"client_id": None, "adverse_id": None,
                                "destinataire_id": None}
    assert "date.aujourdhui" not in payload["unresolved_auto_fields"]
    assert "dossier.titre" in payload["unresolved_auto_fields"]
    text = " ".join(payload["warnings"])
    assert "Aucun dossier fourni" in text
    assert "« destinataire »" in text
    _conforms("list_templates", payload)


@pytest.mark.parametrize("extra, fragment", [
    ({"dossier_id": "gone"}, "`dossier_id` : aucun dossier"),
    # Two clients on d1 and the template reads the slot: the preview must
    # not promise a fill that would then refuse (or pick the wrong one).
    ({"dossier_id": "d1"}, "`client_id` doit être précisé"),
    ({"dossier_id": "d1", "client_id": "x9"}, "`client_id` ne figure pas parmi les clients"),
    ({"dossier_id": "d2", "client_id": "c1", "adverse_id": "a1"},
     "`adverse_id` ne figure pas parmi les parties adverses"),
    ({"adverse_id": "a1"}, "`adverse_id` exige un `dossier_id`"),
    ({"dossier_id": "d2", "destinataire_id": "gone"}, "`destinataire_id` : aucun contact"),
])
def test_slot_refusals_are_the_fills_in_the_connectors_words(store, extra, fragment):
    _, _, letter, _ = store
    with pytest.raises(tools.ToolArgumentError) as exc:
        handlers.list_templates({"template_id": letter["id"], **extra})
    message = str(exc.value)
    assert fragment in message
    assert "fenêtre" not in message           # the popup's wording stays the popup's
    assert "gone" not in message and "x9" not in message  # ids are never echoed


@pytest.mark.parametrize("args, fragment", [
    ({"template_id": "T", "kind": "note"}, "`kind` ne s'applique qu'à la liste"),
    ({"template_id": "T", "limit": 5}, "`limit` ne s'applique qu'à la liste"),
    ({"template_id": "T", "offset": 0}, "`offset` ne s'applique qu'à la liste"),
    ({"dossier_id": "d1"}, "`dossier_id` ne s'applique qu'avec `template_id`"),
    ({"destinataire_id": "x9"}, "`destinataire_id` ne s'applique qu'avec"),
    ({"template_id": "  "}, "`template_id` est vide"),
])
def test_one_modes_arguments_are_refused_in_the_other(store, args, fragment):
    with pytest.raises(tools.ToolArgumentError, match=fragment):
        handlers.list_templates(args)


def test_an_unknown_template_is_absence_not_an_error(store):
    payload = handlers.list_templates({"template_id": "missing"})
    assert payload == {"mode": "detail", "found": False, "template_id": "missing"}
    _conforms("list_templates", payload)


def test_versions_list_every_recorded_file_and_mark_the_current(store):
    db, _, letter, _ = store
    replacement = _docx(PLACEHOLDERS + ["référence_externe"])
    updated, errors, changed = tpl_model.update_template(
        letter["id"], {}, io.BytesIO(replacement), "v2.docx", len(replacement))
    assert errors == [] and changed and updated["version"] == 2
    payload = handlers.list_templates({"template_id": letter["id"], "dossier_id": "d2"})
    assert [(v["version"], v["current"]) for v in payload["versions"]] == [
        (2, True), (1, False)]
    assert payload["versions_truncated"] is False
    assert all(v["installed_at"] for v in payload["versions"])
    assert payload["versions"][0]["file_size"] == len(replacement)
    assert payload["template"]["version"] == 2
    assert payload["template"]["manual_count"] == 3        # the new file's inventory
    _conforms("list_templates", payload)


def test_an_unreadable_history_degrades_to_null_and_says_so(store, monkeypatch):
    _, _, letter, _ = store

    class _Broken:
        def order_by(self, *_a, **_k):
            raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(tpl_model, "_versions_ref", lambda tid: _Broken())
    payload = handlers.list_templates({"template_id": letter["id"], "dossier_id": "d2"})
    assert payload["versions"] is None                    # never [] — [] says « none »
    assert any("historique des versions" in w for w in payload["warnings"])
    assert payload["auto_fields"]                         # the rest is complete
    _conforms("list_templates", payload)
    # The web pane keeps failing open.
    assert tpl_model.list_versions(letter["id"]) == []


def test_the_active_designation_is_reported_and_the_read_writes_nothing(store):
    db, _, _, note = store
    payload = handlers.list_templates({"template_id": note["id"]})
    assert payload["template"]["active"] is False
    assert payload["template"]["active_designated_at"] is None
    assert any("n'est PAS le gabarit actif" in w for w in payload["warnings"])

    designated, errors = tpl_model.set_active_template(
        note["id"], par="juriste@example.com", expected_etag=None)
    assert errors == [], errors
    db.reset_logs()
    payload = handlers.list_templates({"template_id": note["id"]})
    listed = handlers.list_templates({"kind": "note"})
    assert db.commits == []                                # a READ tool
    assert payload["template"]["active"] is True
    assert payload["template"]["active_designated_at"]
    assert any("Gabarit ACTIF" in w for w in payload["warnings"])
    assert listed["items"][0]["active"] is True
    assert "juriste@example.com" not in json.dumps(payload)   # who designated: never
    _conforms("list_templates", payload)


def test_a_special_kinds_own_fields_are_never_reported_as_blocs_to_write(store):
    """The gabarit classifier files note.* (and facture.*) under
    passthrough. Reported as blocs they would invite a caller to compose a
    note's title — or an invoice total — by hand; they are the kind's own
    flow's, and say so."""
    _, _, _, note = store
    payload = handlers.list_templates({"template_id": note["id"]})
    assert payload["flow_fields"] == ["note.titre"]
    assert payload["blocs"] == []
    assert payload["template"]["flow_count"] == 1
    assert payload["template"]["bloc_count"] == 0
    assert any("Imprimer (Word)" in w for w in payload["warnings"])
    row = handlers.list_templates({"kind": "note"})["items"][0]
    assert (row["bloc_count"], row["flow_count"]) == (0, 1)
    _conforms("list_templates", payload)


def test_the_flow_prefixes_are_the_builders_own_namespaces():
    """Pinned against the two builders (pure modules): a field either
    flow starts filling under a new namespace must be taught here."""
    from utils.invoice_docx import _build_rows, _facture_values
    from utils.note_docx import build_note_context

    facture = _facture_values({}, [])
    assert facture and all(k.startswith("facture.") for k in facture)
    rows = _build_rows([{"type": "fee"}, {"type": "expense", "taxable": True},
                        {"type": "expense", "taxable": False}])
    row_keys = {k for region in rows.values() for row in region for k in row}
    assert row_keys and {k.split(".", 1)[0] + "." for k in row_keys} == {"h.", "d."}
    assert handlers._FLOW_PREFIXES["note_honoraires"] == ("facture.", "h.", "d.")

    ctx = build_note_context({"title": "T"}, dossier=None, firm={},
                             today=datetime(2026, 9, 27).date())
    own = {k for k in ctx.values if "." in k and k.split(".", 1)[0] == "note"}
    assert own and all(k.startswith("note.") for k in {*own, *ctx.rich_values})
    assert handlers._FLOW_PREFIXES["note"] == ("note.",)
    # No catalog field lives in a flow namespace (it would be mis-filed).
    from utils.template_fields import CATALOG
    for prefixes in handlers._FLOW_PREFIXES.values():
        assert not [c for c in CATALOG if c.lower().startswith(prefixes)]


def test_the_enum_literals_track_the_model():
    """Hand-copied literals (importing models at tools.py load builds a
    Firestore client) — pinned so they cannot drift."""
    assert tools._TEMPLATE_KINDS == list(tpl_model.VALID_KINDS)
    assert tools._TEMPLATE_CATEGORIES == list(tpl_model.VALID_CATEGORIES)
    props = tools.TOOLS["list_templates"]["input_schema"]["properties"]
    assert props["kind"]["enum"] is tools._TEMPLATE_KINDS
    assert props["category"]["enum"] is tools._TEMPLATE_CATEGORIES


def test_the_connector_reads_through_the_read_half_of_the_service():
    """The connector imports the READ half of the generation service only,
    so the « never » sweep (which reads a reached service whole) proves it
    reaches no writer — the same resolver the popup uses (pinned in
    tests/test_template_folder_reads.py)."""
    tree = ast.parse((_ATHENA / "mcp" / "handlers.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "services":
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("services."):
            imported.add(node.module.split(".", 1)[1])
    assert "gabarit_champs" in imported and "gabarits" not in imported
    assert handlers.gabarit_service is champs


# ══════════════════════════════════════════════════════════════════════
# 3. list_documents — category source, folder roles, the folder tree
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def filed(store):
    db = store[0]
    projets = folder_model.system_folder_id("d1", folder_model.SYSTEM_ROLE_PROJETS)
    folders = {
        projets: {"name": "Projets", "parent_folder_id": None,
                  "system_role": "projets"},
        # A legacy « Reçus du portail », created by name before roles
        # existed: the portal role's holder although nothing stamped it.
        "fportail": {"name": "Reçus du portail", "parent_folder_id": None,
                     "system_role": "", "created_at": datetime(2026, 2, 1, tzinfo=UTC)},
        "fproc": {"name": "Procédures", "parent_folder_id": None, "system_role": ""},
        "fsig": {"name": "Significations", "parent_folder_id": "fproc",
                 "system_role": ""},
        "fvide": {"name": "Archives", "parent_folder_id": None, "system_role": ""},
    }
    for fid, data in folders.items():
        db.seed(f"folders/{fid}", {"id": fid, "dossier_id": "d1", "order": 0,
                                    "etag": f"etag-{fid}", **data})
    docs = {
        "gen": {"folder_id": projets, "category": "correspondance",
                "category_source": "juriste"},
        "recu": {"folder_id": "fportail", "category": "autre"},       # legacy: no source
        "pv": {"folder_id": "fsig", "category": "procès_verbal_signification",
               "category_source": "mcp"},
        "jug": {"folder_id": None, "category": "jugement",
                "category_source": "analyse",
                "analyse": {"sous_nature": "JUGEMENT_FOND"}},
    }
    for did, data in docs.items():
        db.seed(f"documents/{did}", {
            "id": did, "dossier_id": "d1", "dossier_file_number": "2026-001",
            "display_name": f"{did}.pdf", "file_type": "application/pdf",
            "file_size": 10, "version": 1, "tags": [], "etag": f"etag-{did}",
            "storage_path": f"users/{UID}/dossiers/d1/documents/{did}/{did}.pdf",
            "filename": f"{did}.pdf",
            "created_at": datetime(2026, 7, 1, tzinfo=UTC), **data,
        })
    return db, projets


def test_rows_say_who_posed_the_category_and_a_claude_category_is_presumed(filed):
    """Regression (D15): a category Claude posed (`category_source` « mcp »)
    read `category_presumee: false` — the connector presented its OWN
    supposition as the lawyer's determination. It was true only for an
    analysis."""
    payload = handlers.list_documents({"dossier_id": "d1"})
    rows = {r["id"]: r for r in payload["items"]}
    assert {k: (r["category_source"], r["category_presumee"]) for k, r in rows.items()} == {
        "gen": ("juriste", False),
        "recu": ("juriste", False),          # legacy: read as the lawyer's
        "pv": ("mcp", True),
        "jug": ("analyse", True),
    }
    assert all(r["etag"] == f"etag-{k}" for k, r in rows.items())
    assert not _keys(payload) & _NEVER_KEYS
    _conforms("list_documents", payload)


def test_rows_carry_the_system_role_of_their_folder(filed):
    payload = handlers.list_documents({"dossier_id": "d1"})
    rows = {r["id"]: r for r in payload["items"]}
    assert rows["gen"]["folder_system_role"] == "projets"
    assert rows["recu"]["folder_system_role"] == "portail"    # the legacy holder
    assert rows["pv"]["folder_system_role"] == ""
    assert rows["jug"]["folder_system_role"] == ""            # dossier root
    assert rows["pv"]["folder_path"] == "Procédures / Significations"
    assert "folders" not in payload                           # only when asked


def test_include_folders_returns_the_whole_tree_with_roles_and_etags(filed):
    _, projets = filed
    payload = handlers.list_documents(
        {"dossier_id": "d1", "include_folders": True, "folder_id": "fsig"})
    assert [r["id"] for r in payload["items"]] == ["pv"]      # the rows are filtered…
    tree = payload["folders"]                                  # …the tree is not
    assert [f["path"] for f in tree] == [
        "Archives", "Procédures", "Procédures / Significations", "Projets",
        "Reçus du portail"]
    by_id = {f["id"]: f for f in tree}
    assert by_id["fvide"]["path"] == "Archives"                # an empty folder listed
    assert by_id[projets]["system_role"] == "projets"
    assert by_id["fportail"]["system_role"] == "portail"
    assert by_id["fsig"] == {"id": "fsig", "name": "Significations",
                             "parent_folder_id": "fproc",
                             "path": "Procédures / Significations",
                             "system_role": "", "etag": "etag-fsig"}
    assert by_id["fproc"]["parent_folder_id"] is None
    assert payload["folders_truncated"] is False
    _conforms("list_documents", payload)


def test_the_tree_is_capped_and_says_so(filed, monkeypatch):
    monkeypatch.setattr(handlers, "FOLDER_TREE_MAX", 2)
    payload = handlers.list_documents({"dossier_id": "d1", "include_folders": True})
    assert len(payload["folders"]) == 2 and payload["folders_truncated"] is True


def test_an_unreadable_folder_store_is_refused_or_reported_unknown(filed, monkeypatch):
    def _raises(dossier_id):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(folder_model, "_all_folders", _raises)
    # Asked for the tree: refused — never an empty tree.
    with pytest.raises(tools.ToolArgumentError,
                       match="Lecture des dossiers de classement impossible"):
        handlers.list_documents({"dossier_id": "d1", "include_folders": True})
    # Not asked: the rows degrade — a filed document's role is UNKNOWN,
    # never « ordinary »; the root stays known.
    payload = handlers.list_documents({"dossier_id": "d1"})
    rows = {r["id"]: r for r in payload["items"]}
    assert rows["gen"]["folder_system_role"] is None
    assert rows["pv"]["folder_path"] == ""
    assert rows["jug"]["folder_system_role"] == ""
    _conforms("list_documents", payload)


def test_cabinet_scope_leaves_a_filed_documents_role_unresolved(filed):
    payload = handlers.list_documents({"scope": "cabinet"})
    rows = {r["id"]: r for r in payload["items"]}
    assert rows["gen"]["folder_system_role"] is None
    assert rows["jug"]["folder_system_role"] == ""
    with pytest.raises(tools.ToolArgumentError, match="`include_folders`"):
        handlers.list_documents({"scope": "cabinet", "include_folders": True})


def test_a_parent_cycle_neither_vanishes_nor_loops(store):
    """The application refuses to create one; a hand edit can. The walk
    must terminate and still LIST the folders (an empty path says why)."""
    db = store[0]
    for fid, parent in (("fa", "fb"), ("fb", "fa")):
        db.seed(f"folders/{fid}", {"id": fid, "dossier_id": "d2", "name": fid,
                                    "parent_folder_id": parent, "system_role": ""})
    payload = handlers.list_documents({"dossier_id": "d2", "include_folders": True})
    assert sorted((f["id"], f["path"]) for f in payload["folders"]) == [
        ("fa", ""), ("fb", "")]
