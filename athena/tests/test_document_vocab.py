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


def test_gabarit_taxonomy_is_separate_and_untouched():
    # Spec §11 — doc_template keeps its own narrow taxonomy.
    assert doc_template.VALID_CATEGORIES == ("procédure", "correspondance", "autre")
