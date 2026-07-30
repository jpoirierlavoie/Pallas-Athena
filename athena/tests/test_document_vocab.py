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
