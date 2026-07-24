"""Note category vocabulary + read-time migration (2026-07-24, spec §5)."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import models.note as note


def test_label_parity():
    for c in note.VALID_CATEGORIES:
        assert c in note.CATEGORY_LABELS, c
    for key in note.CATEGORY_LABELS:
        assert key in note.VALID_CATEGORIES, key


def test_strategie_is_kept_for_the_analyse_note():
    # The « Théorie de la cause » note is category=stratégie — must survive.
    assert "stratégie" in note.VALID_CATEGORIES


def test_migration_table_is_well_formed():
    for src, dst in note._CATEGORY_MIGRATION.items():
        assert src not in note.VALID_CATEGORIES, f"{src} still live"
        assert dst in note.VALID_CATEGORIES, f"{dst} not in live domain"


def test_read_migration_folds_removed_keys():
    assert note._migrate_category({"category": "audience"})["category"] == "vacation"
    assert note._migrate_category({"category": "appel"})["category"] == "autre"
    assert note._migrate_category({"category": "correspondance"})["category"] == "autre"
    # A live key is untouched
    assert note._migrate_category({"category": "recherche"})["category"] == "recherche"
