"""Document category vocabulary + read-time migration (2026-07-24, spec §6)
and MCP enum parity (§10.5)."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.document as doc
    import models.doc_template as doc_template
    import mcp.tools as tools


def test_label_parity():
    for c in doc.VALID_CATEGORIES:
        assert c in doc.CATEGORY_LABELS, c
    for key in doc.CATEGORY_LABELS:
        assert key in doc.VALID_CATEGORIES, key


def test_migration_table_is_well_formed():
    for src, dst in doc._CATEGORY_MIGRATION.items():
        assert src not in doc.VALID_CATEGORIES, f"{src} still live"
        assert dst in doc.VALID_CATEGORIES, f"{dst} not in live domain"


def test_read_migration_folds_removed_keys():
    assert doc._migrate_category({"category": "entente"})["category"] == "autre"
    assert doc._migrate_category({"category": "note"})["category"] == "autre"
    assert doc._migrate_category({"category": "preuve"})["category"] == "preuve"


def test_mcp_document_enum_matches_model():
    # §10.5 — the MCP list_documents enum must equal the model vocabulary.
    assert (
        tools.TOOLS["list_documents"]["input_schema"]["properties"]["category"]["enum"]
        == list(doc.VALID_CATEGORIES)
    )


# ── La scission de « procès_verbal » (2026-08-26) ───────────────────────

_LEGACY_PV = "procès_verbal"
_NOUVEAUX_PV = ("procès_verbal_signification", "procès_verbal_audience")


def test_les_deux_nouveaux_proces_verbaux_sont_vivants():
    for cle in _NOUVEAUX_PV:
        assert cle in doc.VALID_CATEGORIES, cle
        assert cle in doc.CATEGORY_LABELS, cle


def test_le_selecteur_de_saisie_exclut_la_valeur_heritee():
    # « plus offert à la création, toujours lisible et filtrable ».
    assert _LEGACY_PV not in doc.CATEGORY_CHOICES
    for cle in _NOUVEAUX_PV:
        assert cle in doc.CATEGORY_CHOICES, cle


def test_la_valeur_heritee_reste_valide_et_filtrable():
    # Elle n'est PAS repliée : replier exigerait de deviner entre
    # signification et audience, et test_migration_table_is_well_formed
    # interdit qu'une source de migration soit encore valide.
    assert _LEGACY_PV in doc.VALID_CATEGORIES
    assert _LEGACY_PV in doc.CATEGORY_LABELS
    assert _LEGACY_PV not in doc._CATEGORY_MIGRATION
    assert doc._migrate_category({"category": _LEGACY_PV})["category"] == _LEGACY_PV


def test_choices_est_labels_moins_la_seule_valeur_heritee():
    # Dérivé, pas recopié : une valeur ajoutée à LABELS entre dans CHOICES
    # sans qu'on y pense, et l'écart reste exactement d'une clé.
    assert set(doc.CATEGORY_LABELS) - set(doc.CATEGORY_CHOICES) == {_LEGACY_PV}


def test_le_formulaire_d_edition_rend_toutes_les_categories_valides():
    """Le piège mortel de la scission, épinglé.

    `edit.html` construit son <select> en itérant la map qu'on lui passe.
    Servi avec la liste amputée, un document héritant de « procès_verbal »
    n'aurait AUCUNE option sélectionnée — le navigateur retiendrait la
    première (« procédure ») et la prochaine sauvegarde anodine réécrirait
    la catégorie EN SILENCE : la reclassification que la scission existe
    pour éviter, introduite par la scission elle-même.
    """
    import re
    from pathlib import Path

    gabarits = Path(__file__).resolve().parent.parent / "templates"
    rend_tout = ("documents/edit.html", "documents/list.html")
    saisie = ("documents/upload.html", "reception/index.html")
    for nom in rend_tout:
        src = (gabarits / nom).read_text(encoding="utf-8")
        assert re.search(r"category_labels\.items\(\)", src), nom
        assert "category_choices.items()" not in src, nom
    for nom in saisie:
        src = (gabarits / nom).read_text(encoding="utf-8")
        assert re.search(r"category_choices\.items\(\)", src), nom
        assert "category_labels.items()" not in src, nom


def test_les_trois_dicts_de_couleurs_couvrent_le_vocabulaire():
    """Les trois `category_colors` sont écrits à la main et aucun test ne
    les couvrait : une valeur neuve rendait un badge gris avec le bon
    libellé, et la suite restait verte."""
    import re
    from pathlib import Path

    gabarits = Path(__file__).resolve().parent.parent / "templates"
    for nom in ("documents/detail.html",
                "documents/_browser.html",
                "dossiers/_tab_documents.html"):
        src = (gabarits / nom).read_text(encoding="utf-8")
        bloc = src.split("category_colors", 1)[1].split("%}", 1)[0]
        cles = set(re.findall(r"'([^']+)':", bloc))
        manquantes = set(doc.VALID_CATEGORIES) - cles
        assert not manquantes, f"{nom}: {sorted(manquantes)}"


# ── document_date (PA-G03) ──────────────────────────────────────────────


def test_coerce_document_date_three_forms():
    from datetime import date, datetime, timezone
    attendu = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert doc._coerce_document_date("2026-07-15") == attendu
    assert doc._coerce_document_date(date(2026, 7, 15)) == attendu
    assert doc._coerce_document_date(
        datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    ) == attendu   # time dropped — date-only convention


def test_coerce_document_date_refuses_junk():
    for raw in ("", "  ", "hier", "2026-13-45", None, 42, []):
        assert doc._coerce_document_date(raw) is None


def test_update_metadata_presence_gates_document_date(monkeypatch):
    """A caller that does not carry the key never touches the stored date;
    a carried empty string CLEARS it (the edit form always submits it)."""
    from datetime import datetime, timezone
    stored = {**doc._default_doc(), "id": "doc1", "dossier_id": "d1",
              "display_name": "PV",
              "document_date": datetime(2026, 7, 15, tzinfo=timezone.utc)}
    monkeypatch.setattr(doc, "get_document", lambda i: dict(stored))
    written = {}

    class _Doc:
        def set(self, payload):
            written.update(payload)

    monkeypatch.setattr(
        doc, "db",
        mock.Mock(collection=lambda n: mock.Mock(document=lambda i: _Doc())),
    )
    # Key absent → date survives.
    _, errs = doc.update_metadata("doc1", {"description": "maj"})
    assert errs == []
    assert written["document_date"] == stored["document_date"]
    # Key carried empty → cleared.
    written.clear()
    _, errs = doc.update_metadata("doc1", {"document_date": ""})
    assert errs == []
    assert written["document_date"] is None
    # Key carried with a date → stored at midnight UTC.
    written.clear()
    _, errs = doc.update_metadata("doc1", {"document_date": "2026-07-21"})
    assert errs == []
    assert written["document_date"] == datetime(
        2026, 7, 21, tzinfo=timezone.utc
    )


def test_gabarit_taxonomy_is_separate_and_untouched():
    # Spec §11 — doc_template keeps its own narrow taxonomy.
    assert doc_template.VALID_CATEGORIES == ("procédure", "correspondance", "autre")
