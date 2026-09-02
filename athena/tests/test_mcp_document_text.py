"""get_document_text — refusal paths and honesty guarantees.

Conformance (payload vs declared outputSchema) lives in
tests/test_mcp_output_schemas.py; this file pins the REFUSALS — the
French messages and the argument gates.

Ce fichier s'appelait test_mcp_phase_n.py et couvrait aussi les quatre
outils de brouillon, partis avec le clavardage le 2026-09-02.
"""

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    from mcp.tools import ToolArgumentError


# ── get_document_text argument gates ────────────────────────────────────────

@pytest.mark.parametrize("raw", ["abc", "3-2", "0", "0-4", "1-2-3", "-4"])
def test_page_range_malformed_is_refused(raw):
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers._parse_page_range({"page_range": raw})
    assert "page_range" in str(excinfo.value)


def test_page_range_single_number_means_onward():
    assert handlers._parse_page_range({"page_range": "4"}) == (4, None)
    assert handlers._parse_page_range({"page_range": "2-6"}) == (2, 6)
    assert handlers._parse_page_range({}) == (1, None)


def test_doc_mime_dispatch_refuses_legacy_word(monkeypatch):
    # .doc (OLE2) has no stdlib-readable text — honest refusal, never a fake.
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: {"id": "doc1", "file_type": "application/msword",
                   "file_size": 100, "storage_path": "users/u/x",
                   "display_name": "Vieux contrat"})
    payload = handlers.get_document_text({"document_id": "doc1"})
    assert payload["readable"] is False
    assert payload["reason"] == "unsupported_type"
    assert "application/msword" in payload["message"]
