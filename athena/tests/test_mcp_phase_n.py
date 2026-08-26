"""Phase N handler behavior — refusal paths and honesty guarantees of
get_document_text / get_draft / list_drafts / save_draft / revise_draft.

Conformance (payload vs declared outputSchema) lives in
tests/test_mcp_output_schemas.py; this file pins the REFUSALS — the French
messages, the argument gates, and the dry-run honesty (a dry run that
predicts a success the real call refuses is a lie).
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


# ── get_draft / list_drafts gates ───────────────────────────────────────────

def test_get_draft_version_out_of_bounds_names_the_count(monkeypatch):
    monkeypatch.setattr(
        handlers.chat_draft_model, "get_draft",
        lambda i: {"id": "b1", "current_version": 3, "content": "x"})
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers.get_draft({"draft_id": "b1", "version": 9})
    assert "3 version" in str(excinfo.value)


def test_list_drafts_dossier_and_floating_are_exclusive():
    with pytest.raises(ToolArgumentError):
        handlers.list_drafts({"dossier_id": "d1", "floating": True})


# ── save_draft / revise_draft refusals ──────────────────────────────────────

def test_save_draft_unknown_dossier_refused_never_downgraded(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier", lambda i: None)
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers.save_draft(
            {"dossier_id": "fantome", "title": "T", "content": "C"})
    message = str(excinfo.value)
    assert "Dossier introuvable" in message
    assert "PERMANENT" in message


def test_save_draft_chevron_content_is_refused_loudly(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001", "title": "T"})
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers.save_draft(
            {"dossier_id": "d1", "title": "T",
             "content": "si a < b et b > c, alors…"})
    assert "chevrons" in str(excinfo.value)


def test_save_draft_dry_run_validates_like_the_live_call(monkeypatch):
    # The dry branch must refuse everything the live call refuses — here the
    # unknown dossier, checked BEFORE the dry short-circuit.
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier", lambda i: None)
    with pytest.raises(ToolArgumentError):
        handlers.save_draft(
            {"dossier_id": "fantome", "title": "T", "content": "C",
             "dry_run": True})


def test_revise_draft_unknown_id_refused_never_a_create(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handlers.chat_draft_model, "get_draft", lambda i: None)
    monkeypatch.setattr(
        handlers.chat_draft_model, "revise_draft",
        lambda *a, **k: calls.append(1))
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers.revise_draft({"draft_id": "fantome", "content": "texte"})
    assert "ne crée" in str(excinfo.value) or "ne cree" in str(excinfo.value)
    assert calls == []  # the model was never asked to write


def test_refusals_never_quote_draft_content(monkeypatch):
    # The tracing gotcha: a refusal message ships to Cloud Trace via
    # record_exception, so it must never sample the text it refuses.
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001", "title": "T"})
    privileged = "le client < a avoué > ceci"
    with pytest.raises(ToolArgumentError) as excinfo:
        handlers.save_draft(
            {"dossier_id": "d1", "title": "T", "content": privileged})
    assert "avoué" not in str(excinfo.value)
