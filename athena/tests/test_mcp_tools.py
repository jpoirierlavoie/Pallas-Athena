"""Tests for the MCP tool layer: validator, formatting, and handlers."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
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

UTC = timezone.utc
NBSP = " "


# ── Subset schema validator ─────────────────────────────────────────────

_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {"type": "integer", "minimum": 1, "maximum": 90},
        "name": {"type": "string", "maxLength": 5},
        "flag": {"type": "boolean"},
        "ratio": {"type": "number", "minimum": 0.5, "maximum": 2.0},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 3}},
        "kind": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["days"],
    "additionalProperties": False,
}


def test_validator_accepts_valid_args():
    args = {"days": 90, "name": "abc", "flag": True, "ratio": 1.5,
            "tags": ["ab"], "kind": "a"}
    assert tools.validate_args(_SCHEMA, args) == []


def test_validator_rejects_unknown_key():
    errors = tools.validate_args(_SCHEMA, {"days": 1, "bogus": 1})
    assert any("bogus" in e for e in errors)


def test_validator_enforces_required():
    errors = tools.validate_args(_SCHEMA, {})
    assert any("days" in e and "required" in e for e in errors)


def test_validator_integer_bounds_and_message():
    errors = tools.validate_args(_SCHEMA, {"days": 0})
    assert any(">= 1" in e for e in errors)
    errors = tools.validate_args(_SCHEMA, {"days": 91})
    assert any("<= 90" in e for e in errors)
    assert tools.validate_args(_SCHEMA, {"days": 1}) == []
    assert tools.validate_args(_SCHEMA, {"days": 90}) == []
    # Wrong type with bounds produces the spec's canonical message.
    errors = tools.validate_args(_SCHEMA, {"days": "ten"})
    assert errors == ["`days` must be an integer between 1 and 90"]


def test_validator_bool_is_not_an_integer():
    errors = tools.validate_args(_SCHEMA, {"days": True})
    assert errors  # bool must not satisfy type: integer


def test_validator_string_max_length_and_enum():
    assert tools.validate_args(_SCHEMA, {"days": 1, "name": "abcdef"})
    assert tools.validate_args(_SCHEMA, {"days": 1, "kind": "z"})
    assert tools.validate_args(_SCHEMA, {"days": 1, "kind": "b"}) == []


def test_validator_array_items_one_level():
    errors = tools.validate_args(_SCHEMA, {"days": 1, "tags": ["okay-too-long"]})
    assert any("tags[0]" in e for e in errors)
    assert tools.validate_args(_SCHEMA, {"days": 1, "tags": []}) == []
    errors = tools.validate_args(_SCHEMA, {"days": 1, "tags": "no"})
    assert any("array" in e for e in errors)


def test_validator_type_checks():
    assert tools.validate_args(_SCHEMA, {"days": 1, "flag": "yes"})
    assert tools.validate_args(_SCHEMA, {"days": 1, "ratio": "big"})
    assert tools.validate_args(_SCHEMA, "not-an-object")


# ── Money / date formatting ─────────────────────────────────────────────

def test_format_cents():
    assert tools.format_cents(1234567) == f"12{NBSP}345,67{NBSP}$"
    assert tools.format_cents(0) == f"0,00{NBSP}$"
    assert tools.format_cents(5) == f"0,05{NBSP}$"
    assert tools.format_cents(-250050) == f"-2{NBSP}500,50{NBSP}$"


def test_date_only_fields_never_shift_through_montreal():
    # Midnight-UTC date-only fixture: a Montréal conversion would render
    # 2026-07-06 (the previous day) — the #1 foreseeable bug of Phase I.
    midnight_utc = datetime(2026, 7, 7, 0, 0, tzinfo=UTC)
    assert tools.date_str(midnight_utc) == "2026-07-07"
    assert tools.date_str(datetime(2026, 7, 7)) == "2026-07-07"  # naive → UTC
    assert tools.date_str(date(2026, 7, 7)) == "2026-07-07"
    assert tools.date_str(None) is None


def test_true_timestamps_render_in_montreal():
    assert tools.iso_mtl(datetime(2026, 7, 7, 12, 0, tzinfo=UTC)) == (
        "2026-07-07T08:00:00-04:00"
    )
    assert tools.iso_mtl(None) is None


def test_tool_result_envelope():
    payload = {"titre": "Réponse déposée", "montant": 1}
    result = tools.tool_result(payload, "2025-03-26")
    assert result["isError"] is False
    assert "structuredContent" not in result
    text = result["content"][0]["text"]
    assert "Réponse déposée" in text  # ensure_ascii=False

    result_new = tools.tool_result(payload, "2025-06-18")
    assert result_new["structuredContent"] == payload


def test_registry_shape():
    assert len(tools.TOOLS) == 19  # 17 read-only + 2 note writes
    for name, spec in tools.TOOLS.items():
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        limit = schema.get("properties", {}).get("limit")
        if limit is not None:
            assert limit["maximum"] == 50  # hard cap


# ── Write-tool registry invariants ──────────────────────────────────────

def test_write_tools_set_is_pinned():
    """A third write tool must not be able to ship unnoticed."""
    assert tools.WRITE_TOOLS == frozenset({"create_note", "append_to_note"})
    assert tools.WRITE_TOOLS <= set(tools.TOOLS)


def test_annotations_split_both_directions():
    descriptors = {d["name"]: d for d in tools.list_tool_descriptors()}
    assert len(descriptors) == 19
    for name, d in descriptors.items():
        ann = d["annotations"]
        assert ann["openWorldHint"] is False
        if name in tools.WRITE_TOOLS:
            assert ann["readOnlyHint"] is False
            # Both must be explicit: the MCP spec defaults destructiveHint to
            # True once readOnlyHint is false, which would over-warn on a
            # purely additive call.
            assert ann["destructiveHint"] is False
            assert ann["idempotentHint"] is False
        else:
            assert ann["readOnlyHint"] is True
            assert "destructiveHint" not in ann
            assert "idempotentHint" not in ann


def test_required_scope_defaults_to_read_never_write():
    for name in tools.TOOLS:
        expected = "athena:write" if name in tools.WRITE_TOOLS else "athena:read"
        assert tools.required_scope(name) == expected


def test_list_tool_descriptors_filters_by_scope():
    read_only = tools.list_tool_descriptors(frozenset({"athena:read"}))
    names = {d["name"] for d in read_only}
    assert len(read_only) == 17
    assert not (names & tools.WRITE_TOOLS)

    both = tools.list_tool_descriptors(
        frozenset({"athena:read", "athena:write"})
    )
    assert {d["name"] for d in both} >= tools.WRITE_TOOLS


def test_write_schemas_are_bounded_and_track_the_model():
    from models import note as note_model

    # The tools.py enum is a hand-copied literal (house convention for the
    # other enums too) — pin it against the model so it cannot drift.
    assert (
        tools.TOOLS["create_note"]["input_schema"]["properties"]["category"]["enum"]
        == list(note_model.VALID_CATEGORIES)
    )
    for name in tools.WRITE_TOOLS:
        content = tools.TOOLS[name]["input_schema"]["properties"]["content"]
        # Strictly below the model ceiling: an oversized write must be
        # refused loudly here, never silently truncated by security.sanitize,
        # and appends need headroom under the ceiling.
        assert content["maxLength"] < note_model.CONTENT_MAX_LENGTH
        assert content["minLength"] == 1
    # Fields that would let a caller overwrite an existing note must not be
    # addressable at all.
    create_props = tools.TOOLS["create_note"]["input_schema"]["properties"]
    for forbidden in ("id", "vjournal_uid", "created_at", "etag"):
        assert forbidden not in create_props


def test_min_length_rejects_whitespace_only():
    schema = tools.TOOLS["create_note"]["input_schema"]
    errors = tools.validate_args(
        schema, {"dossier_id": "d1", "title": "   ", "content": "x"}
    )
    assert errors and "title" in errors[0]


def test_validate_args_blocks_id_injection():
    schema = tools.TOOLS["create_note"]["input_schema"]
    errors = tools.validate_args(
        schema,
        {"dossier_id": "d1", "title": "T", "content": "C", "id": "existing-note"},
    )
    assert any("`id` is not a supported argument" in e for e in errors)


# ── Handler helpers ─────────────────────────────────────────────────────

def _task(status="à_faire", due=None, tid="t1"):
    return {"id": tid, "title": "Préparer requête", "status": status,
            "priority": "haute", "category": "rédaction", "due_date": due,
            "dossier_id": "d1", "dossier_file_number": "2026-001",
            "dossier_title": "Tremblay c. Lavoie"}


# ── get_agenda ──────────────────────────────────────────────────────────

def test_get_agenda_filters_cancelled_and_formats_money(monkeypatch):
    calls = {}
    hearing = {"id": "h1", "title": "Audience", "status": "confirmée",
               "all_day": False,
               "start_datetime": datetime(2026, 7, 8, 18, 0, tzinfo=UTC),
               "end_datetime": datetime(2026, 7, 8, 19, 0, tzinfo=UTC)}
    cancelled = {**hearing, "id": "h2", "status": "annulée"}

    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [hearing, cancelled])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: calls.setdefault("cutoff", cutoff) and [] or [])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 7)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 12.5, "amount": 312500})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 1234567)

    payload = handlers.get_agenda({"days_ahead": 7})
    assert [h["id"] for h in payload["hearings"]] == ["h1"]
    assert payload["hearings"][0]["start"] == "2026-07-08T14:00:00-04:00"
    assert payload["stats"]["open_dossiers"] == 7
    assert payload["stats"]["unbilled_cents"] == 312500
    assert payload["stats"]["unbilled_display"] == f"3{NBSP}125,00{NBSP}$"
    assert payload["stats"]["outstanding_display"] == f"12{NBSP}345,67{NBSP}$"
    assert payload["window"]["days_ahead"] == 7


def test_get_agenda_marks_overdue_tasks(monkeypatch):
    past = datetime.now(UTC) - timedelta(days=3)
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: [_task(due=past)])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [
                            {"id": "s1", "title": "Dépôt", "status": "à_venir",
                             "deadline_date": past, "_protocol_id": "p1",
                             "_protocol_title": "Protocole", "_dossier_file_number": "2026-001"}])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 0)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 0.0, "amount": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total", lambda: 0)

    payload = handlers.get_agenda({})
    assert payload["urgent_tasks"][0]["is_overdue"] is True
    step = payload["urgent_protocol_steps"][0]
    assert step["is_overdue"] is True
    assert step["protocol_title"] == "Protocole"


# ── list_dossiers / get_dossier ─────────────────────────────────────────

def _dossier(did="d1", fn="2026-001", title="Tremblay c. Lavoie"):
    return {"id": did, "file_number": fn, "title": title, "status": "actif",
            "domaine": "REC", "action": "REC-01",
            "action_precision": "factures 2024-03",
            "mandate_type": "judiciaire",
            "role": "demandeur",
            "tribunal": "Cour supérieure", "court_file_number": "500-05-123456-241",
            "opened_date": datetime(2026, 1, 5, tzinfo=UTC),
            "prescription_date": None, "hourly_rate": 25000, "flat_fee": None,
            # date-only (midnight UTC) — must emit as the UTC calendar date
            "date_avis": datetime(2026, 8, 3, tzinfo=UTC),
            "clients": [{"id": "p1", "name": "Jean Tremblay"}],
            "opposing_parties": [{"id": "p2", "name": "Marc Lavoie"}]}


def test_list_dossiers_query_and_truncation(monkeypatch):
    rows = [_dossier(f"d{i}", f"2026-{i:03d}") for i in range(30)]
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda status_filter=None, limit=200: (rows, None))
    payload = handlers.list_dossiers({"query": "2026-0", "limit": 10})
    assert payload["count"] == 10
    assert payload["truncated"] is True
    assert payload["items"][0]["opened_date"] == "2026-01-05"
    assert payload["items"][0]["clients"] == ["Jean Tremblay"]


def test_get_dossier_requires_exactly_one_selector():
    with pytest.raises(tools.ToolArgumentError):
        handlers.get_dossier({})
    with pytest.raises(tools.ToolArgumentError):
        handlers.get_dossier({"dossier_id": "x", "file_number": "y"})


def test_get_dossier_not_found_is_data_not_error(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    payload = handlers.get_dossier({"dossier_id": "missing"})
    assert payload["found"] is False


def test_get_dossier_composes_summaries(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier())
    monkeypatch.setattr(handlers.task_model, "get_task_summary",
                        lambda d: {"total": 3, "active": 2, "completed": 1, "overdue": 0})
    monkeypatch.setattr(handlers.hearing_model, "get_hearing_summary",
                        lambda d: {"total": 1, "upcoming": 1, "past": 0})
    monkeypatch.setattr(handlers.note_model, "get_notes_summary", lambda d: {"total": 4})
    monkeypatch.setattr(handlers.document_model, "get_document_summary",
                        lambda d: {"total": 2, "total_size": 1024, "total_size_formatted": "1.0 Ko"})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary",
                        lambda d: {"total_hours": 10.0, "total_billable_amount": 250000,
                                   "unbilled_hours": 4.0, "unbilled_amount": 100000})
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary",
                        lambda d: {"total_expenses": 5000, "unbilled_expenses": 5000})
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary",
                        lambda d: {"count": 1, "total_invoiced": 150000,
                                   "total_paid": 0, "total_outstanding": 150000})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_summary",
                        lambda d: {"has_protocol": False, "has_history": False,
                                   "total": 0, "completed": 0, "overdue": 0, "upcoming": 0})

    payload = handlers.get_dossier({"dossier_id": "d1"})
    assert payload["found"] is True
    assert payload["dossier"]["hourly_rate_display"] == f"250,00{NBSP}$"
    assert payload["dossier"]["mandate_type"] == "judiciaire"
    # The free-text notes/internal_notes fields were removed from the dossier
    # schema (superseded by the standalone `notes` collection).
    assert "notes" not in payload["dossier"]
    assert "internal_notes" not in payload["dossier"]
    # Taxonomy: raw key + French label, mirroring the prescription_type /
    # prescription_label pair. Labels/delai prose are asserted against the
    # taxonomy module's live values (the handler's job is to pass them
    # through faithfully), so an editorial rewording does not break this.
    from utils import taxonomie
    d = payload["dossier"]
    assert d["domaine"] == "REC"
    assert d["domaine_label"] == taxonomie.DOMAINE_LABELS["REC"]
    assert d["action"] == "REC-01"
    assert d["action_label"] == taxonomie.action_label("REC-01")
    assert d["action_precision"] == "factures 2024-03"
    # The taxonomy's guidance travels with the action: the delay verbatim from
    # the table (never a computed one), plus what kind(s) of delay it is.
    src = taxonomie.ACTIONS["REC-01"]
    assert d["delai"] == src.delai
    assert d["delai_types"] == list(src.delai_types) == ["PE"]
    assert d["delai_types_label"] == taxonomie.delai_types_label("REC-01")
    assert d["a_valider"] == src.a_valider is False
    assert d["delai_point_depart"] == src.point_depart
    assert d["ref_delai"] == src.ref_delai
    assert d["ref_fondement"] == src.ref_fondement
    assert d["avis"] == []
    # The pre-split field names must be gone.
    assert "delai_type" not in d
    assert "action_references" not in d
    # date_avis is date-only (midnight UTC): the UTC calendar date, never a
    # Montréal-shifted timestamp.
    assert d["date_avis"] == "2026-08-03"
    # matter_type/objet were superseded by the taxonomy.
    assert "matter_type" not in d
    assert "objet" not in d
    summaries = payload["summaries"]
    assert summaries["time"]["unbilled_display"] == f"1{NBSP}000,00{NBSP}$"
    assert summaries["invoices"]["total_outstanding_cents"] == 150000
    assert summaries["protocol"]["has_protocol"] is False


def test_get_dossier_by_file_number(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda status_filter=None, limit=200: ([_dossier()], None))
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier() if i == "d1" else None)
    for name in ("get_task_summary",):
        monkeypatch.setattr(handlers.task_model, name, lambda d: {})
    monkeypatch.setattr(handlers.hearing_model, "get_hearing_summary", lambda d: {})
    monkeypatch.setattr(handlers.note_model, "get_notes_summary", lambda d: {})
    monkeypatch.setattr(handlers.document_model, "get_document_summary", lambda d: {})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary", lambda d: {})
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary", lambda d: {})
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary", lambda d: {})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_summary", lambda d: {})

    payload = handlers.get_dossier({"file_number": "2026-001"})
    assert payload["found"] is True


# ── list_tasks ──────────────────────────────────────────────────────────

def test_list_tasks_default_hides_completed(monkeypatch):
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda dossier_id=None, status_filter=None:
                        [_task(), _task(status="terminée", tid="t2"),
                         _task(status="annulée", tid="t3")])
    payload = handlers.list_tasks({})
    assert [t["id"] for t in payload["items"]] == ["t1"]

    payload = handlers.list_tasks({"include_completed": True})
    assert payload["count"] == 3


def test_list_tasks_due_date_is_date_only(monkeypatch):
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda dossier_id=None, status_filter=None:
                        [_task(due=datetime(2026, 7, 10, 0, 0, tzinfo=UTC))])
    payload = handlers.list_tasks({})
    assert payload["items"][0]["due_date"] == "2026-07-10"


# ── list_hearings ───────────────────────────────────────────────────────

def test_list_hearings_validates_dates(monkeypatch):
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=200: [])
    with pytest.raises(tools.ToolArgumentError):
        handlers.list_hearings({"date_from": "07/10/2026"})
    with pytest.raises(tools.ToolArgumentError):
        handlers.list_hearings({"date_from": "2026-07-10", "date_to": "2026-07-01"})
    with pytest.raises(tools.ToolArgumentError):
        handlers.list_hearings({"date_from": "2024-01-01", "date_to": "2026-01-01"})


def test_list_hearings_dossier_filter(monkeypatch):
    h1 = {"id": "h1", "dossier_id": "d1", "all_day": False,
          "start_datetime": datetime(2026, 7, 8, 14, 0, tzinfo=UTC)}
    h2 = {"id": "h2", "dossier_id": "d2", "all_day": False,
          "start_datetime": datetime(2026, 7, 9, 14, 0, tzinfo=UTC)}
    captured = {}

    def fake_range(a, b, limit=200):
        captured["from"], captured["to"] = a, b
        return [h1, h2]

    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range", fake_range)
    payload = handlers.list_hearings(
        {"date_from": "2026-07-01", "date_to": "2026-07-31", "dossier_id": "d2"}
    )
    assert [h["id"] for h in payload["items"]] == ["h2"]
    assert captured["from"] == datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    # Widened fetch window (+30 h past date_to midnight UTC) so Montreal
    # evening hearings on date_to are not clipped.
    assert captured["to"] == datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


def test_list_hearings_montreal_evening_boundaries(monkeypatch):
    # 22:00 EDT on date_to = 02:00 UTC the next day → must be INCLUDED;
    # 21:00 EDT the evening BEFORE date_from (01:00 UTC on date_from) →
    # must be EXCLUDED.
    included = {"id": "in", "all_day": False,
                "start_datetime": datetime(2026, 7, 9, 2, 0, tzinfo=UTC)}
    excluded = {"id": "out", "all_day": False,
                "start_datetime": datetime(2026, 7, 1, 1, 0, tzinfo=UTC)}
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=200: [included, excluded])
    payload = handlers.list_hearings(
        {"date_from": "2026-07-01", "date_to": "2026-07-08"}
    )
    assert [h["id"] for h in payload["items"]] == ["in"]


def test_list_hearings_all_day_uses_date_only(monkeypatch):
    h = {"id": "h1", "all_day": True,
         "start_datetime": datetime(2026, 7, 8, 0, 0, tzinfo=UTC),
         "end_datetime": datetime(2026, 7, 8, 0, 0, tzinfo=UTC)}
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=200: [h])
    payload = handlers.list_hearings({"date_from": "2026-07-01"})
    assert payload["items"][0]["start"] == "2026-07-08"


# ── notes ───────────────────────────────────────────────────────────────

def test_list_notes_preview_is_truncated_plain_text(monkeypatch):
    long_content = "x" * 500
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda dossier_id=None, **kw: [{"id": "n1", "title": "T",
                                                        "category": "appel", "pinned": True,
                                                        "content": long_content}])
    payload = handlers.list_notes({"dossier_id": "d1"})
    assert len(payload["items"][0]["content_preview"]) == 280
    assert "content" not in payload["items"][0]


def test_get_note_found_and_not_found(monkeypatch):
    monkeypatch.setattr(handlers.note_model, "get_note", lambda i: None)
    assert handlers.get_note({"note_id": "n9"})["found"] is False

    monkeypatch.setattr(handlers.note_model, "get_note",
                        lambda i: {"id": "n1", "content": "# Markdown brut"})
    payload = handlers.get_note({"note_id": "n1"})
    assert payload["note"]["content"] == "# Markdown brut"


# ── documents ───────────────────────────────────────────────────────────

def test_list_documents_metadata_only_and_folder_sentinel(monkeypatch):
    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        return [{"id": "doc1", "display_name": "Requête.pdf",
                 "category": "procédure", "file_type": "application/pdf",
                 "file_size": 2048, "version": 1, "folder_id": None,
                 "storage_path": "users/u/dossiers/d/doc1/req.pdf"}]

    monkeypatch.setattr(handlers.document_model, "list_documents", fake_list)
    payload = handlers.list_documents({"dossier_id": "d1"})
    # folder_id must NOT be passed when absent (model sentinel semantics).
    assert "folder_id" not in captured
    item = payload["items"][0]
    assert item["file_size_display"] == "2.0 Ko"
    assert "storage_path" not in item
    assert "signed_url" not in item


# ── parties ─────────────────────────────────────────────────────────────

def test_get_partie_card_with_dossier_relations(monkeypatch):
    partie = {"id": "p1", "type": "individual", "contact_role": "client",
              "first_name": "Jean", "last_name": "Tremblay",
              "phone_cell": "+15145551234", "identity_verified": "vérifié",
              "identity_verified_date": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(handlers.partie_model, "get_partie", lambda i: partie)
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_for_partie",
                        lambda i: [{"id": "d1", "file_number": "2026-001",
                                    "title": "T c. L", "status": "actif",
                                    "client_ids": ["p1"], "opposing_party_ids": []}])
    payload = handlers.get_partie({"partie_id": "p1"})
    card = payload["partie"]
    assert card["display_name"] == "Jean Tremblay"
    assert card["phone_cell"] == "+15145551234"
    assert "(514)" in card["phone_cell_display"]
    assert payload["dossiers"][0]["relation"] == "client"


def test_list_parties_summary_rows(monkeypatch):
    monkeypatch.setattr(handlers.partie_model, "list_parties",
                        lambda type_filter=None, role_filter=None, search=None:
                        [{"id": "p1", "type": "organization",
                          "organization_name": "9123-4567 Québec inc.",
                          "contact_role": "partie_adverse", "address_city": "Montréal"}])
    payload = handlers.list_parties({"contact_role": "partie_adverse"})
    row = payload["items"][0]
    assert row["display_name"] == "9123-4567 Québec inc."
    assert row["is_organization"] is True
    assert row["city"] == "Montréal"


# ── billing ─────────────────────────────────────────────────────────────

def test_billing_snapshot_global(monkeypatch):
    invoices = [
        {"id": "i1", "status": "envoyée", "total": 100000, "amount_due": 100000,
         "invoice_number": "2026-F001", "date": datetime(2026, 6, 1, tzinfo=UTC)},
        {"id": "i2", "status": "payée", "total": 50000, "amount_due": 0},
        {"id": "i3", "status": "en_retard", "total": 200000, "amount_due": 200000},
    ]
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 3.5, "amount": 87500})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 300000)
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: invoices)
    payload = handlers.get_billing_snapshot({})
    assert payload["scope"] == "global"
    assert {i["id"] for i in payload["outstanding_invoices"]} == {"i1", "i3"}
    assert payload["outstanding_invoices"][0]["date"] == "2026-06-01"
    assert payload["outstanding_display"] == f"3{NBSP}000,00{NBSP}$"


def test_billing_snapshot_unknown_dossier_is_found_false(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    payload = handlers.get_billing_snapshot({"dossier_id": "missing"})
    assert payload["found"] is False
    assert "total_invoiced_cents" not in payload


def test_billing_snapshot_dossier_caps_rows_at_50(monkeypatch):
    entries = [{"id": f"e{i}", "date": datetime(2026, 6, 1, tzinfo=UTC),
                "description": "Travail", "hours": 1.0, "rate": 25000,
                "amount": 25000} for i in range(60)]
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": i, "title": "T"})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary",
                        lambda d: {"total_hours": 60.0, "total_billable_amount": 0,
                                   "unbilled_hours": 60.0, "unbilled_amount": 0})
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary",
                        lambda d: {"total_expenses": 0, "unbilled_expenses": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary",
                        lambda d: {"count": 0, "total_invoiced": 0,
                                   "total_paid": 0, "total_outstanding": 0})
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_time_entries",
                        lambda d: entries)
    monkeypatch.setattr(handlers.expense_model, "get_unbilled_expenses",
                        lambda d: [])
    payload = handlers.get_billing_snapshot({"dossier_id": "d1"})
    assert len(payload["unbilled_time_entries"]) == 50
    assert payload["unbilled_time_entries_truncated"] is True
    assert payload["unbilled_time_entries"][0]["date"] == "2026-06-01"


# ── protocol steps ──────────────────────────────────────────────────────

def test_list_protocol_steps_derives_overdue_without_writes(monkeypatch):
    past = datetime.now(UTC) - timedelta(days=2)
    future = datetime.now(UTC) + timedelta(days=30)
    protocol = {"id": "p1", "title": "Protocole de l'instance",
                "protocol_type": "cq_simplifié", "status": "actif",
                "start_date": datetime(2026, 5, 1, tzinfo=UTC),
                "steps": [
                    {"id": "s1", "order": 1, "title": "Dépôt", "status": "à_venir",
                     "deadline_date": past},
                    {"id": "s2", "order": 2, "title": "Interrogatoires",
                     "status": "complété", "deadline_date": past},
                    {"id": "s3", "order": 3, "title": "Mise en état",
                     "status": "à_venir", "deadline_date": future},
                ]}

    def forbidden(*a, **kw):
        raise AssertionError("check_overdue_steps writes to Firestore — never call it")

    monkeypatch.setattr(handlers.protocol_model, "check_overdue_steps", forbidden)
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: protocol)
    payload = handlers.list_protocol_steps({"dossier_id": "d1"})
    steps = payload["protocols"][0]["steps"]
    assert [s["is_overdue"] for s in steps] == [True, False, False]
    assert payload["has_active_protocol"] is True


def test_step_and_task_due_today_are_not_overdue(monkeypatch):
    today_midnight = datetime.combine(
        datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC
    )
    protocol = {"id": "p1", "status": "actif",
                "steps": [{"id": "s1", "order": 1, "title": "Dépôt",
                           "status": "à_venir", "deadline_date": today_midnight}]}
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: protocol)
    payload = handlers.list_protocol_steps({"dossier_id": "d1"})
    assert payload["protocols"][0]["steps"][0]["is_overdue"] is False

    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: [_task(due=today_midnight)])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 0)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 0.0, "amount": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total", lambda: 0)
    agenda = handlers.get_agenda({})
    assert agenda["urgent_tasks"][0]["is_overdue"] is False


def test_list_documents_folder_filter_survives_query(monkeypatch):
    docs = [
        {"id": "a", "folder_id": "f1", "display_name": "Contrat.pdf",
         "file_type": "application/pdf", "file_size": 10, "version": 1},
        {"id": "b", "folder_id": "f2", "display_name": "Contrat 2.pdf",
         "file_type": "application/pdf", "file_size": 10, "version": 1},
    ]
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: docs)
    monkeypatch.setattr(handlers.folder_model, "get_folder_breadcrumb",
                        lambda d, f: [{"id": "f1", "name": "Procédures"}])
    payload = handlers.list_documents(
        {"dossier_id": "d1", "folder_id": "f1", "query": "contrat"}
    )
    assert [d["id"] for d in payload["items"]] == ["a"]
    assert payload["folder_path"] == "Procédures"


def test_list_protocol_steps_history(monkeypatch):
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: None)
    monkeypatch.setattr(handlers.protocol_model, "list_protocols_for_dossier",
                        lambda d: [{"id": "p1"}, {"id": "p2"}])
    monkeypatch.setattr(handlers.protocol_model, "get_protocol",
                        lambda pid: {"id": pid, "status": "complété", "steps": []})
    payload = handlers.list_protocol_steps(
        {"dossier_id": "d1", "include_history": True}
    )
    assert len(payload["protocols"]) == 2
    assert payload["has_active_protocol"] is False


# ── judicial deadline ───────────────────────────────────────────────────

def test_compute_judicial_deadline_weekend_extension():
    # 2026-07-03 + 8 days = 2026-07-11, a Saturday → Monday 2026-07-13.
    payload = handlers.compute_judicial_deadline(
        {"start_date": "2026-07-03", "delay_days": 8, "direction": "after"}
    )
    assert payload["raw_date"] == "2026-07-11"
    assert payload["deadline"] == "2026-07-13"
    assert payload["was_adjusted"] is True
    assert "Saturday" in payload["adjustment_reason"]


def test_compute_judicial_deadline_holiday_extension():
    # 2027-06-24 (Fête nationale, a Thursday) → Friday 2027-06-25.
    payload = handlers.compute_judicial_deadline(
        {"start_date": "2027-06-20", "delay_days": 4, "direction": "after"}
    )
    assert payload["raw_date"] == "2027-06-24"
    assert payload["deadline"] == "2027-06-25"
    assert "holiday" in payload["adjustment_reason"]


def test_compute_judicial_deadline_backward_direction():
    # 10 days before 2026-07-13 (Monday) = 2026-07-03 (Friday): juridical.
    payload = handlers.compute_judicial_deadline(
        {"start_date": "2026-07-13", "delay_days": 10, "direction": "before"}
    )
    assert payload["deadline"] == "2026-07-03"
    assert payload["was_adjusted"] is False
    assert payload["adjustment_reason"] is None


# ── court file number ───────────────────────────────────────────────────

def test_parse_court_file_number_success():
    payload = handlers.parse_court_file_number(
        {"court_file_number": "500-05-123456-241"}
    )
    assert payload["greffe_number"] == "500"
    assert payload["tribunal"] == "Cour supérieure"
    assert payload["palais_de_justice"] == "Montréal"
    assert payload["is_administrative"] is False
    assert payload["parse_error"] is None


def test_parse_court_file_number_administrative():
    payload = handlers.parse_court_file_number({"court_file_number": "TAL-12345"})
    assert payload["is_administrative"] is True
    assert payload["parse_error"] is None


# ════════════════════════════════════════════════════════════════════════
# Write tools
# ════════════════════════════════════════════════════════════════════════

def _wdossier(status="actif"):
    return {
        "id": "d1", "file_number": "2026-001",
        "title": "Tremblay c. Lavoie", "status": status,
    }


@pytest.fixture
def bumps(monkeypatch):
    """Record every CTag bump / tombstone removal the handlers perform."""
    recorded = {"bump": [], "tombstone": []}
    monkeypatch.setattr(handlers, "bump_ctag", lambda n: recorded["bump"].append(n))
    monkeypatch.setattr(
        handlers, "remove_tombstone",
        lambda n, r: recorded["tombstone"].append((n, r)),
    )
    return recorded


@pytest.fixture
def created(monkeypatch):
    """Capture the dict actually handed to models.note.create_note."""
    seen = {}

    def _create(data):
        seen.update(data)
        return {
            **data, "id": "n-new",
            "created_at": datetime(2026, 7, 22, 14, 0, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 22, 14, 0, tzinfo=UTC),
        }, []

    monkeypatch.setattr(handlers.note_model, "create_note", _create)
    return seen


# ── The DavX5 hinge ─────────────────────────────────────────────────────

def test_create_note_bumps_the_dossier_ctag(monkeypatch, bumps, created):
    """models/note.py never bumps — a tool path that forgets makes DavX5
    silently stop syncing the dossier. This is the pin."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    payload = handlers.create_note(
        {"dossier_id": "d1", "title": "Recherche", "content": "Corps"}
    )
    assert bumps["bump"] == ["dossier:d1"]
    assert bumps["tombstone"] == [("dossier:d1", "n-new")]
    assert payload["created"] is True
    assert payload["dav_synced"] is True
    assert payload["note"]["id"] == "n-new"


def test_append_to_note_bumps_the_dossier_ctag(monkeypatch, bumps):
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "Déjà là"},
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    monkeypatch.setattr(
        handlers.note_model, "update_note",
        lambda nid, data: ({"id": nid, "dossier_id": "d1", **data}, []),
    )
    payload = handlers.append_to_note({"note_id": "n1", "content": "Suite"})
    assert bumps["bump"] == ["dossier:d1"]
    # An append never removes a tombstone: the resource does not re-enter
    # the collection.
    assert bumps["tombstone"] == []
    assert payload["appended"] is True


def test_ctag_bump_failure_still_reports_the_write_as_a_success(
    monkeypatch, created
):
    """A raise after the commit would reach endpoint's blanket except and be
    reported as a failure — the model would retry and duplicate the note."""
    def _boom(_name):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(handlers, "bump_ctag", _boom)
    monkeypatch.setattr(handlers, "remove_tombstone", lambda n, r: None)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    payload = handlers.create_note(
        {"dossier_id": "d1", "title": "T", "content": "C"}
    )
    assert payload["created"] is True
    assert payload["note"]["id"] == "n-new"
    assert payload["dav_synced"] is False
    assert any("Ne pas réessayer" in w for w in payload["warnings"])


# ── Dossier resolution ──────────────────────────────────────────────────

def test_create_note_refuses_an_unknown_dossier(monkeypatch, bumps):
    """Never blank the dossier_id like the web route does: that path writes
    an orphan note reachable from nowhere."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)

    def _must_not_run(_data):
        raise AssertionError("create_note reached the model with a bad dossier")

    monkeypatch.setattr(handlers.note_model, "create_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
        handlers.create_note({"dossier_id": "nope", "title": "T", "content": "C"})
    assert bumps["bump"] == []


def test_create_note_denormalizes_dossier_labels(monkeypatch, bumps, created):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    handlers.create_note({"dossier_id": "d1", "title": "T", "content": "C"})
    assert created["dossier_file_number"] == "2026-001"
    assert created["dossier_title"] == "Tremblay c. Lavoie"


def test_closed_dossier_write_is_flagged_not_silently_invisible(
    monkeypatch, bumps, created
):
    """/dav/dossier-{id}/ only exposes actif/en_attente — say so."""
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier", lambda i: _wdossier("fermé")
    )
    payload = handlers.create_note(
        {"dossier_id": "d1", "title": "T", "content": "C"}
    )
    assert payload["created"] is True
    assert payload["dav_synced"] is False
    assert any("fermé" in w for w in payload["warnings"])


# ── Whitelist: no overwrite-by-id ───────────────────────────────────────

def test_create_note_never_forwards_caller_supplied_identity(
    monkeypatch, bumps, created
):
    """models.note.create_note honours a caller `id` and then set()s the whole
    document — forwarding args would silently destroy an existing note."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    handlers.create_note({
        "dossier_id": "d1", "title": "T", "content": "C",
        "id": "victim", "vjournal_uid": "x", "created_at": "2020-01-01",
        "etag": "e", "pinned": True,
    })
    assert "id" not in created
    assert "vjournal_uid" not in created
    assert "created_at" not in created
    assert "etag" not in created
    assert created["pinned"] is False
    assert set(created) == {
        "dossier_id", "dossier_file_number", "dossier_title",
        "title", "content", "category", "pinned",
    }


def test_append_only_ever_updates_content(monkeypatch, bumps):
    seen = {}
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "A"},
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())

    def _update(nid, data):
        seen.update(data)
        return {"id": nid, "dossier_id": "d1", **data}, []

    monkeypatch.setattr(handlers.note_model, "update_note", _update)
    handlers.append_to_note(
        {"note_id": "n1", "content": "B", "dossier_id": "autre"}
    )
    assert set(seen) == {"content"}
    assert seen["content"].startswith("A")


# ── Markdown survival ───────────────────────────────────────────────────

def test_autolinks_are_converted_not_destroyed(monkeypatch, bumps, created):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    handlers.create_note({
        "dossier_id": "d1", "title": "T",
        "content": "Source: <https://canlii.ca/t/abc123> et <me@example.com>.",
    })
    assert "[https://canlii.ca/t/abc123](https://canlii.ca/t/abc123)" in created["content"]
    assert "[me@example.com](mailto:me@example.com)" in created["content"]


def test_content_the_sanitizer_would_eat_is_refused_loudly(monkeypatch, bumps):
    """« si a < b et b > c » loses « < b et b > » inside security.sanitize,
    with no error. Refuse instead of losing the research."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())

    def _must_not_run(_data):
        raise AssertionError("reached the model with content that would be cut")

    monkeypatch.setattr(handlers.note_model, "create_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="chevrons"):
        handlers.create_note({
            "dossier_id": "d1", "title": "T",
            "content": "Si la valeur < 15 000 $ et > 300 000 $, voir art. 2925.",
        })


def test_normalized_content_survives_the_real_sanitizer(monkeypatch, bumps, created):
    """End-to-end against the ACTUAL security.sanitize, so this cannot drift."""
    from security import sanitize

    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    handlers.create_note({
        "dossier_id": "d1", "title": "T",
        "content": "Voir <https://canlii.ca/t/abc> — art. 2925 C.c.Q.",
    })
    stored = created["content"]
    assert sanitize(stored, max_length=100_000) == stored


# ── The truncation trap ─────────────────────────────────────────────────

def test_append_refuses_rather_than_truncating(monkeypatch, bumps):
    """security.sanitize truncates at CONTENT_MAX_LENGTH with no exception
    and no flag; update_note then set()s the truncated document."""
    from models import note as note_model

    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {
            "id": "n1", "dossier_id": "d1",
            "content": "x" * (note_model.CONTENT_MAX_LENGTH - 10),
        },
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())

    def _must_not_run(_nid, _data):
        raise AssertionError("update_note called with content that would truncate")

    monkeypatch.setattr(handlers.note_model, "update_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="trop longue"):
        handlers.append_to_note({"note_id": "n1", "content": "beaucoup de texte"})
    assert bumps["bump"] == []


def test_append_refuses_when_the_JOIN_would_eat_existing_content(
    monkeypatch, bumps
):
    """The addition is clean and the existing note is clean, but TAG_RE
    (`<[^<>]*>`) matches ACROSS NEWLINES — so an unpaired « < » already in
    the note plus a Markdown blockquote « > » in the addition makes the
    regex span the join and delete the note's tail, the separator, and the
    provenance stamp. Silently, behind an "appended: true" envelope."""
    from security import sanitize

    existing = "Le montant en litige est < 15 000 $, donc classe I."
    addition = "La Cour rappelle :\n\n> Le délai court dès la connaissance."
    # Both halves are individually storable — that is what makes it a trap.
    assert sanitize(existing, max_length=100_000) == existing
    assert sanitize(addition, max_length=100_000) == addition

    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": existing},
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())

    def _must_not_run(_nid, _data):
        raise AssertionError("update_note called with content that would be cut")

    monkeypatch.setattr(handlers.note_model, "update_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="Ajout refusé"):
        handlers.append_to_note({"note_id": "n1", "content": addition})
    assert bumps["bump"] == []


def test_refusal_messages_never_quote_the_note_content(monkeypatch, bumps):
    """These messages are recorded on the mcp.tool.* span by span()'s
    record_exception, and the exporter scrubs attributes, not exception
    events — an excerpt would ship privileged research to Cloud Trace."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    monkeypatch.setattr(
        handlers.note_model, "create_note",
        lambda d: (_ for _ in ()).throw(AssertionError("must not write")),
    )
    secret = "Stratégie: invoquer <RLRQ c. B-1, r. 5> contre Tremblay"
    with pytest.raises(tools.ToolArgumentError) as exc:
        handlers.create_note(
            {"dossier_id": "d1", "title": "T", "content": secret}
        )
    message = str(exc.value)
    for leaked in ("RLRQ", "Tremblay", "Stratégie", "B-1"):
        assert leaked not in message
    assert "chevrons" in message


def test_append_does_not_claim_a_closed_dossier_when_the_lookup_merely_failed(
    monkeypatch, bumps
):
    """get_dossier swallows read errors and returns None. That is not the
    same as « fermé » and must not be reported as it."""
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "A"},
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    monkeypatch.setattr(
        handlers.note_model, "update_note",
        lambda nid, data: ({"id": nid, "dossier_id": "d1", **data}, []),
    )
    payload = handlers.append_to_note({"note_id": "n1", "content": "B"})
    assert payload["ctag_bumped"] is True
    assert payload["dav_synced"] is True
    assert payload["warnings"] == []


def test_closed_dossier_still_reports_the_ctag_bump_as_having_happened(
    monkeypatch, bumps, created
):
    """dav_synced and ctag_bumped are different facts: a closed dossier
    bumps correctly but is never advertised to DavX5. Collapsing them makes
    a healthy write look like a sync failure in the audit trail."""
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier", lambda i: _wdossier("archivé")
    )
    payload = handlers.create_note(
        {"dossier_id": "d1", "title": "T", "content": "C"}
    )
    assert bumps["bump"] == ["dossier:d1"]
    assert payload["ctag_bumped"] is True
    assert payload["dav_synced"] is False


def test_append_refuses_an_unknown_note(monkeypatch, bumps):
    monkeypatch.setattr(handlers.note_model, "get_note", lambda i: None)
    with pytest.raises(tools.ToolArgumentError, match="Note introuvable"):
        handlers.append_to_note({"note_id": "nope", "content": "C"})
    assert bumps["bump"] == []


def test_append_refuses_the_analyse_note(monkeypatch, bumps):
    """The « Théorie de la cause » note is read-only via the connector:
    readable through list_notes/get_note, never writable."""
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "Analyse",
                   "is_analyse": True},
    )

    def _must_not_run(nid, data):
        raise AssertionError("wrote to the analyse note")

    monkeypatch.setattr(handlers.note_model, "update_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="théorie de la cause"):
        handlers.append_to_note({"note_id": "n1", "content": "Ajout"})
    assert bumps["bump"] == []


def test_mcp_list_notes_includes_the_analyse_note(monkeypatch):
    """The model default EXCLUDES the analyse note; the MCP read path must
    override it — left on the default, the note silently vanishes from
    Claude's view."""
    seen = {}

    def _list(dossier_id=None, include_analyse=False, **kw):
        seen["include_analyse"] = include_analyse
        return [{"id": "n-a", "dossier_id": dossier_id or "",
                 "title": "Théorie de la cause", "content": "Corps",
                 "category": "stratégie", "pinned": False,
                 "is_analyse": True, "created_at": None, "updated_at": None}]

    monkeypatch.setattr(handlers.note_model, "list_notes", _list)
    payload = handlers.list_notes({"dossier_id": "d1"})
    assert seen["include_analyse"] is True
    assert payload["items"][0]["is_analyse"] is True

    payload = handlers.list_notes({})  # « Général » branch
    assert seen["include_analyse"] is True


# ── Provenance ──────────────────────────────────────────────────────────

def test_writes_carry_a_dated_provenance_stamp(monkeypatch, bumps, created):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: _wdossier())
    monkeypatch.setattr(handlers, "_today_mtl", lambda: date(2026, 7, 22))
    handlers.create_note({"dossier_id": "d1", "title": "T", "content": "Corps"})
    assert created["content"].startswith(
        "*Note rédigée par Claude le 22 juillet 2026*"
    )
    assert created["content"].endswith("Corps")
    assert created["category"] == "recherche"

    seen = {}
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "Original"},
    )

    def _update(nid, data):
        seen.update(data)
        return {"id": nid, "dossier_id": "d1", **data}, []

    monkeypatch.setattr(handlers.note_model, "update_note", _update)
    handlers.append_to_note({"note_id": "n1", "content": "Ajout"})
    assert seen["content"].startswith("Original")
    assert "*Ajouté par Claude le 22 juillet 2026*" in seen["content"]
    assert "\n---\n" in seen["content"]


# ══════════════════════════════════════════════════════════════════════
# « Général » — notes attached to no dossier
# ══════════════════════════════════════════════════════════════════════

def test_general_note_bumps_the_general_ctag(monkeypatch, bumps, created):
    """THE risk: a note with no dossier must still bump a collection. Bump
    nothing and it is written, visible in the app, and never on the phone."""
    def _must_not_run(_i):
        raise AssertionError("no dossier lookup when dossier_id is absent")

    monkeypatch.setattr(handlers.dossier_model, "get_dossier", _must_not_run)
    payload = handlers.create_note({"title": "Veille", "content": "Corps"})
    assert bumps["bump"] == ["general"]
    assert bumps["tombstone"] == [("general", "n-new")]
    assert created["dossier_id"] == ""
    assert created["dossier_file_number"] == ""
    assert payload["dav_synced"] is True   # Général is never drained
    assert payload["warnings"] == []


def test_unknown_dossier_is_still_refused_never_downgraded(monkeypatch, bumps):
    """models/note._validate no longer requires a dossier, so a bad id would
    otherwise be filed silently under Général instead of erroring."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)

    def _must_not_run(_data):
        raise AssertionError("wrote a note despite an unknown dossier_id")

    monkeypatch.setattr(handlers.note_model, "create_note", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
        handlers.create_note(
            {"dossier_id": "inexistant", "title": "T", "content": "C"}
        )
    assert bumps["bump"] == []


def test_list_notes_without_dossier_returns_the_general_ones(monkeypatch):
    monkeypatch.setattr(
        handlers.note_model, "list_notes",
        lambda **kw: [
            {"id": "n1", "dossier_id": "", "title": "Veille", "content": "a"},
            {"id": "n2", "dossier_id": "d1", "title": "Dossier", "content": "b"},
        ],
    )
    payload = handlers.list_notes({})
    assert [i["id"] for i in payload["items"]] == ["n1"]


def test_create_note_schema_no_longer_requires_a_dossier():
    schema = tools.TOOLS["create_note"]["input_schema"]
    assert "dossier_id" not in schema["required"]
    assert "dossier_id" in schema["properties"]      # still accepted
    assert tools.validate_args(schema, {"title": "T", "content": "C"}) == []
    assert "dossier_id" not in tools.TOOLS["list_notes"]["input_schema"].get(
        "required", []
    )
