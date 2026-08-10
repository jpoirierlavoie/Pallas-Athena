"""Tests for the template kind discriminator (Phase H.3 — the « note » kind).

Pure: the form-mapping helper and label agreement, no Firestore (the
selectors are Firestore-coupled and stay untested, consistent with the
existing suite's treatment of get_note_honoraires_template).
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models.doc_template import KIND_LABELS, VALID_KINDS
    from routes.doc_templates import _kind_from_form


def test_valid_kinds_all_labelled():
    assert set(VALID_KINDS) == set(KIND_LABELS)
    assert VALID_KINDS == ("gabarit", "note_honoraires", "note")


def test_radio_value_wins():
    for kind in VALID_KINDS:
        assert _kind_from_form({"kind": kind}) == kind


def test_radio_value_wins_over_legacy_checkbox():
    assert _kind_from_form({"kind": "note", "is_note_honoraires": "1"}) == "note"


def test_unknown_radio_falls_through_to_legacy_checkbox():
    assert _kind_from_form({"kind": "n_importe_quoi", "is_note_honoraires": "1"}) == (
        "note_honoraires"
    )


def test_neither_field_defaults_to_gabarit():
    assert _kind_from_form({}) == "gabarit"
    assert _kind_from_form({"kind": "  "}) == "gabarit"
