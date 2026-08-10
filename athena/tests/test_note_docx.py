"""Tests for utils/note_docx.py — the note-print context builder (H.3)."""

import io
import os
import sys
import zipfile
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.docx_fill import fill_docx
from utils.note_docx import (
    RICH_FIELD,
    _CATEGORY_LABELS,
    NoteContext,
    assemble_note_print_values,
    build_note_context,
)

_FIRM = {
    "nom": "Poirier Lavoie avocat",
    "adresse_civique": "1 rue Test",
    "ville": "Montréal",
    "province": "Québec",
    "code_postal": "H1H 1H1",
    "telephone": "+1 (514) 555-1234",
    "courriel": "info@example.com",
}


def _note(**over) -> dict:
    base = {
        "id": "n1",
        "title": "Stratégie d'interrogatoire",
        "content": "# Plan\n\nContenu **important**.",
        "category": "stratégie",
        "dossier_id": "d1",
        "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie",
        "created_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


def _ctx(**over) -> NoteContext:
    return build_note_context(
        _note(**over), dossier=None, firm=_FIRM, today=date(2026, 8, 10)
    )


# ── Builder values ────────────────────────────────────────────────────────


def test_note_values_basic():
    ctx = _ctx()
    assert ctx.values["note.titre"] == "Stratégie d'interrogatoire"
    assert ctx.values["note.categorie"] == "Stratégie"
    assert ctx.values["note.dossier"] == "2026-001 — Tremblay c. Lavoie"


def test_dates_are_montreal_calendar_dates():
    # 02:00 UTC on the 5th is the evening of the 4th in Montréal (EDT) —
    # an evening note must not print tomorrow's date.
    ctx = _ctx(created_at=datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc))
    assert ctx.values["note.date"] == "4 août 2026"


def test_date_maj_suppressed_when_same_mtl_day():
    ctx = _ctx(
        created_at=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc),
    )
    assert ctx.values["note.date_maj"] == ""


def test_date_maj_shown_when_different_day():
    ctx = _ctx(updated_at=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc))
    assert ctx.values["note.date_maj"] == "7 août 2026"


def test_dossier_general_fallback():
    ctx = _ctx(dossier_id="", dossier_file_number="", dossier_title="")
    assert ctx.values["note.dossier"] == "Général"


def test_unknown_category_key_passes_through_raw():
    ctx = _ctx(category="clef_inconnue")
    assert ctx.values["note.categorie"] == "clef_inconnue"


def test_rich_values_contains_only_contenu():
    ctx = _ctx()
    assert set(ctx.rich_values) == {RICH_FIELD}
    assert RICH_FIELD not in ctx.values
    assert ctx.rich_values[RICH_FIELD].startswith("# Plan")


def test_catalog_overlay_with_dossier():
    dossier = {
        "file_number": "2026-001",
        "title": "Tremblay c. Lavoie",
        "tribunal": "Cour supérieure",
    }
    ctx = build_note_context(
        _note(), dossier=dossier, firm=_FIRM, today=date(2026, 8, 10)
    )
    assert ctx.values.get("dossier.titre") == "Tremblay c. Lavoie"
    assert ctx.values.get("cabinet.nom") == "Poirier Lavoie avocat"
    # dossier=None leaves dossier.* names ABSENT (→ assembly marker).
    ctx_none = _ctx()
    assert "dossier.titre" not in ctx_none.values


def test_category_labels_mirror_models_note():
    """The local mirror must equal models.note.CATEGORY_LABELS — read from
    source without importing models (Firestore client at import)."""
    import ast

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "models",
        "note.py",
    )
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", "") == "CATEGORY_LABELS":
                    assert ast.literal_eval(node.value) == _CATEGORY_LABELS
                    return
    raise AssertionError("CATEGORY_LABELS not found in models/note.py")


# ── Assembly seam ─────────────────────────────────────────────────────────


def _template(placeholders: list[str]) -> dict:
    return {"placeholders": placeholders}


def test_assembly_rich_field_skipped_and_values_mapped():
    ctx = _ctx()
    values = assemble_note_print_values(
        _template(["note.titre", "note.contenu", "cabinet.nom"]), ctx
    )
    assert "note.contenu" not in values
    assert values["note.titre"] == "Stratégie d'interrogatoire"
    assert values["cabinet.nom"] == "Poirier Lavoie avocat"


def test_assembly_missing_dossier_field_gets_marker():
    ctx = _ctx()  # dossier=None
    values = assemble_note_print_values(_template(["dossier.titre"]), ctx)
    assert values["dossier.titre"] == "[CHAMP MANQUANT : dossier.titre]"


def test_assembly_case_insensitive_and_allcaps_uppercases():
    ctx = _ctx()
    values = assemble_note_print_values(_template(["NOTE.TITRE"]), ctx)
    assert values["NOTE.TITRE"] == "STRATÉGIE D'INTERROGATOIRE"


def test_assembly_manual_default_applied():
    ctx = _ctx()
    values = assemble_note_print_values(_template(["pièces_jointes"]), ctx)
    assert values["pièces_jointes"] == "Aucune"


def test_assembly_passthrough_omitted():
    ctx = _ctx()
    values = assemble_note_print_values(_template(["FAITS", "civilité"]), ctx)
    assert "FAITS" not in values
    assert "civilité" not in values


# ── End-to-end with the engine ────────────────────────────────────────────


def test_end_to_end_note_fill():
    from defusedxml import ElementTree as ET

    _W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    document = (
        f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
        "<w:p><w:r><w:t>Note : {{note.titre}} ({{note.categorie}})</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{note.contenu}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Dossier : {{note.dossier}}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document)
    docx = buf.getvalue()

    ctx = _ctx()
    template = _template(["note.titre", "note.categorie", "note.contenu", "note.dossier"])
    values = assemble_note_print_values(template, ctx)
    out = fill_docx(docx, values, rich_values=ctx.rich_values)
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    ET.fromstring(xml)
    assert "{{" not in xml
    # The apostrophe is XML-escaped by the fill engine (&#39;).
    assert "Stratégie d&#39;interrogatoire" in xml
    assert "<w:b/>" in xml  # **important** became real bold
    assert "2026-001 — Tremblay c. Lavoie" in xml
