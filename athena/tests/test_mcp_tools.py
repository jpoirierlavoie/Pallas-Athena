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

from pagination import decode_cursor, encode_cursor
from tz import MTL

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
    assert len(tools.TOOLS) == 33  # 23 read-only + 10 writes
    for name, spec in tools.TOOLS.items():
        schema = spec["input_schema"]
        assert schema["additionalProperties"] is False
        limit = schema.get("properties", {}).get("limit")
        if limit is not None:
            assert limit["maximum"] == 50  # hard cap


# ── Write-tool registry invariants ──────────────────────────────────────

def test_write_tools_set_is_pinned():
    """A third write tool must not be able to ship unnoticed."""
    assert tools.WRITE_TOOLS == frozenset({
        "create_note", "append_to_note",
        "create_task", "create_hearing",
        "create_time_entry", "create_expense",
        "complete_dossier", "record_signification",
        "record_prescription_event",
        "complete_task",
    })
    assert tools.WRITE_TOOLS <= set(tools.TOOLS)


def test_annotations_split_both_directions():
    descriptors = {d["name"]: d for d in tools.list_tool_descriptors()}
    assert len(descriptors) == 33
    # idempotentHint is PER TOOL, not per family: every creator appends
    # again on a second call, while complete_task with the same status
    # writes nothing at all. A single family value would misdescribe one of
    # them, and the hint is what a client uses to decide whether a retry is
    # safe.
    idempotent = {"complete_task"}
    for name, d in descriptors.items():
        ann = d["annotations"]
        assert ann["openWorldHint"] is False
        if name in tools.WRITE_TOOLS:
            assert ann["readOnlyHint"] is False
            # destructiveHint must be explicit: the MCP spec defaults it to
            # True once readOnlyHint is false, which would over-warn on a
            # call that never deletes and never overwrites.
            assert ann["destructiveHint"] is False
            assert ann["idempotentHint"] is (name in idempotent), name
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
    assert len(read_only) == 23
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
    # NOTE tools only — other write tools have no `content` property (the
    # old loop over WRITE_TOOLS KeyError'd the moment a third write tool
    # shipped, which was the single hardest-to-notice blocker of the
    # write-surface extension).
    for name in ("create_note", "append_to_note"):
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


def test_every_write_tool_carries_the_write_protocol():
    """Generic invariants over WRITE_TOOLS — they hold for any FUTURE write
    tool without naming it: SCOPE_WRITE + the dry_run/idempotency_key
    protocol properties, and no identity-injection fields addressable."""
    for name in tools.WRITE_TOOLS:
        props = tools.TOOLS[name]["input_schema"]["properties"]
        assert tools.TOOLS[name].get("scope") == "athena:write", name
        assert "dry_run" in props, name
        assert "idempotency_key" in props, name
        assert props["idempotency_key"]["minLength"] >= 8, name
        for forbidden in ("id", "etag", "created_at", "updated_at"):
            assert forbidden not in props, (name, forbidden)


def test_kill_switch_covers_every_write_tool(monkeypatch):
    """MCP_WRITE_ENABLED=false must hide/refuse the WHOLE write surface —
    derived from WRITE_TOOLS membership, so a new tool is covered by
    construction; this pins that derivation."""
    monkeypatch.setattr(tools, "write_enabled", lambda: False)
    for name in tools.WRITE_TOOLS:
        assert tools.tool_available(name) is False, name
    for name in set(tools.TOOLS) - tools.WRITE_TOOLS:
        assert tools.tool_available(name) is True, name


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

    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 0})
    payload = handlers.get_agenda({"days_ahead": 7})
    assert [h["id"] for h in payload["hearings"]] == ["h1"]
    assert payload["hearings"][0]["start"] == "2026-07-08T14:00:00-04:00"
    assert payload["stats"]["open_dossiers"] == 7
    assert payload["stats"]["unbilled_cents"] == 312500
    assert payload["stats"]["unbilled_display"] == f"3{NBSP}125,00{NBSP}$"
    assert payload["stats"]["outstanding_display"] == f"12{NBSP}345,67{NBSP}$"
    assert payload["window"]["days_ahead"] == 7


def test_get_agenda_marks_overdue_tasks(monkeypatch):
    # FROZEN day, fixed deadline — never derived from the clock. This test
    # used to read `now(UTC) - 3 days` and broke the 2026-08-11 00:03 UTC
    # build: in the 00:00-04:00 UTC band today_mtl() is still the PREVIOUS
    # Montréal day, so a 3-day UTC offset is only 2 Montréal days back — it
    # landed on Saturday the 8th, which prorogation carries to Monday the
    # 10th, i.e. today, i.e. not yet late. The app was right; the test mixed
    # a UTC-derived date with a Montréal today. Widening the offset (as the
    # sibling test below once did) only makes the landmine rarer.
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))       # a Friday
    past = datetime(2026, 7, 28, tzinfo=UTC)                # a Tuesday
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

    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 0})
    payload = handlers.get_agenda({})
    assert payload["urgent_tasks"][0]["is_overdue"] is True
    step = payload["urgent_protocol_steps"][0]
    assert step["is_overdue"] is True
    assert step["protocol_title"] == "Protocole"


def test_get_agenda_prescription_alert_last_action_semantics(monkeypatch):
    """PA-D02: last_action_day is INCLUSIVE — a business-day deadline keeps
    its own date (differs False); a weekend deadline pulls it earlier
    (differs True). The alert also carries droit_action_date so the delay
    can be sanity-checked without a second get_dossier call."""
    monday = datetime(2026, 9, 21, tzinfo=UTC)      # a juridical Monday
    saturday = datetime(2026, 9, 26, tzinfo=UTC)    # pulls back to Friday
    droit = datetime(2023, 9, 21, tzinfo=UTC)
    alerts = [
        {"id": "d1", "file_number": "2026-030", "title": "Marchand c. Gélinas",
         "prescription_date": monday, "droit_action_date": droit,
         "prescription_notes": ""},
        {"id": "d2", "file_number": "2026-031", "title": "X c. Y",
         "prescription_date": saturday, "prescription_notes": ""},
    ]
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: alerts)
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 0)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 0.0, "amount": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total", lambda: 0)

    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 0})
    rows = handlers.get_agenda({})["prescription_alerts"]
    on_business_day, on_weekend = rows[0], rows[1]

    assert on_business_day["last_action_date"] == "2026-09-21"
    assert on_business_day["last_action_differs"] is False
    assert on_business_day["droit_action_date"] == "2023-09-21"

    assert on_weekend["last_action_date"] == "2026-09-25"  # the Friday
    assert on_weekend["last_action_differs"] is True
    assert on_weekend["droit_action_date"] is None


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
            "prise_action_date": datetime(2026, 9, 15, tzinfo=UTC),
            "clients": [{"id": "p1", "name": "Jean Tremblay"}],
            "opposing_parties": [{"id": "p2", "name": "Marc Lavoie"}]}


def test_list_dossiers_query_and_truncation(monkeypatch):
    rows = [_dossier(f"d{i}", f"2026-{i:03d}") for i in range(30)]
    monkeypatch.setattr(
        handlers.dossier_model, "list_dossiers_page",
        lambda status_filter=None, limit=200, cursor=None: (rows, None))
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
                        lambda d, today=None: {"total": 3, "active": 2,
                                               "completed": 1, "overdue": 0})
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
                        lambda d, today=None: {"has_protocol": False,
                                               "has_history": False, "total": 0,
                                               "completed": 0, "overdue": 0,
                                               "upcoming": 0})

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
    # Même règle pour la prise d'action — date seule, jamais un décalage
    # Montréal qui la reculerait d'un jour.
    assert d["prise_action_date"] == "2026-09-15"
    # matter_type/objet were superseded by the taxonomy.
    assert "matter_type" not in d
    assert "objet" not in d
    summaries = payload["summaries"]
    assert summaries["time"]["unbilled_display"] == f"1{NBSP}000,00{NBSP}$"
    assert summaries["invoices"]["total_outstanding_cents"] == 150000
    assert summaries["protocol"]["has_protocol"] is False


def _blank_summaries(monkeypatch):
    monkeypatch.setattr(handlers.hearing_model, "get_hearing_summary", lambda d: {})
    monkeypatch.setattr(handlers.note_model, "get_notes_summary", lambda d: {})
    monkeypatch.setattr(handlers.document_model, "get_document_summary", lambda d: {})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary", lambda d: {})
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary", lambda d: {})
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary", lambda d: {})
    monkeypatch.setattr(handlers.task_model, "get_task_summary",
                        lambda d, today=None: {})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_summary",
                        lambda d, today=None: {})


def test_get_dossier_by_file_number(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        lambda fn: _dossier() if fn == "2026-001" else None)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier() if i == "d1" else None)
    monkeypatch.setattr(handlers.task_model, "get_task_summary",
                        lambda d, today=None: {})
    monkeypatch.setattr(handlers.hearing_model, "get_hearing_summary", lambda d: {})
    monkeypatch.setattr(handlers.note_model, "get_notes_summary", lambda d: {})
    monkeypatch.setattr(handlers.document_model, "get_document_summary", lambda d: {})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary", lambda d: {})
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary", lambda d: {})
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary", lambda d: {})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_summary",
                        lambda d, today=None: {})

    payload = handlers.get_dossier({"file_number": "2026-001"})
    assert payload["found"] is True


def test_get_dossier_by_file_number_reaches_past_the_newest_window(monkeypatch):
    """The lookup is a keyed query, not a filter over the 200 most recently
    OPENED dossiers. A historical import's files are by construction the
    OLDEST in the base, so under the old scan « does 2014-007 exist? »
    answered « no » for a dossier that does — and the connector, which can
    never delete, would then mint a duplicate."""
    old = _dossier(did="d-old", fn="2014-007")

    def _paged_must_not_run(**kw):
        raise AssertionError(
            "the file_number lookup must be a keyed query, never a page scan"
        )

    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        _paged_must_not_run)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        lambda fn: old if fn == "2014-007" else None)
    _blank_summaries(monkeypatch)

    payload = handlers.get_dossier({"file_number": "2014-007"})
    assert payload["found"] is True
    assert payload["dossier"]["id"] == "d-old"


def test_get_dossier_by_file_number_fails_closed_on_a_read_error(monkeypatch):
    """« Is there already a dossier numbered X? » is asked immediately before
    creating one. A swallowed read error answering « no » is what mints a
    duplicate, so this branch propagates where the dossier_id branch
    swallows."""
    def _raises(fn):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        _raises)
    with pytest.raises(RuntimeError):
        handlers.get_dossier({"file_number": "2014-007"})


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


def test_list_notes_passes_query_and_category_to_the_model(monkeypatch):
    """PA-G08: category + query are pure model passthrough — and never lose
    include_analyse=True on the way (the load-bearing flag)."""
    captured = {}

    def _list(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(handlers.note_model, "list_notes", _list)
    handlers.list_notes({"dossier_id": "d1", "query": "Olivares",
                         "category": "stratégie"})
    assert captured["search"] == "Olivares"
    assert captured["category"] == "stratégie"
    assert captured["include_analyse"] is True

    captured.clear()
    handlers.list_notes({"query": "veille"})     # « Général » branch
    assert captured["search"] == "veille"
    assert captured["include_analyse"] is True


def test_list_notes_pinned_is_a_select_filter(monkeypatch):
    """`pinned` SELECTS — it is deliberately not wired to the model's
    pinned_first flag, which only reorders."""
    notes = [
        {"id": "n1", "dossier_id": "d1", "pinned": True, "content": "a",
         "created_at": datetime(2026, 7, 10, 12, 0, tzinfo=UTC)},
        {"id": "n2", "dossier_id": "d1", "pinned": False, "content": "b",
         "created_at": datetime(2026, 7, 20, 12, 0, tzinfo=UTC)},
    ]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(notes))
    only_pinned = handlers.list_notes({"dossier_id": "d1", "pinned": True})
    assert [i["id"] for i in only_pinned["items"]] == ["n1"]
    only_unpinned = handlers.list_notes({"dossier_id": "d1", "pinned": False})
    assert [i["id"] for i in only_unpinned["items"]] == ["n2"]


def test_list_notes_date_window_is_montreal_calendar(monkeypatch):
    """created_at is a true instant: 2026-07-16 01:30 UTC is still July 15
    in Montréal — a UTC comparison would file the note on the wrong day."""
    notes = [
        {"id": "late-evening", "dossier_id": "d1", "content": "a",
         "created_at": datetime(2026, 7, 16, 1, 30, tzinfo=UTC)},
        {"id": "next-day", "dossier_id": "d1", "content": "b",
         "created_at": datetime(2026, 7, 16, 15, 0, tzinfo=UTC)},
    ]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(notes))
    payload = handlers.list_notes(
        {"dossier_id": "d1", "date_from": "2026-07-15",
         "date_to": "2026-07-15"}
    )
    assert [i["id"] for i in payload["items"]] == ["late-evening"]


def test_list_dossiers_query_matches_the_sommaire(monkeypatch):
    rows = [
        _dossier(did="d1", fn="2026-001"),
        {**_dossier(did="d2", fn="2026-002", title="Succession Untel"),
         "sommaire": "Litige successoral entre les héritiers."},
    ]
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda **kw: (rows, None))
    payload = handlers.list_dossiers({"query": "successoral"})
    assert [i["id"] for i in payload["items"]] == ["d2"]


def test_list_dossiers_pagination_cursor_round_trip(monkeypatch):
    """G07: the model cursor was in hand and thrown away. The emitted
    next_cursor is minted from the LAST RETURNED row, so a continuation
    resumes right after it — nothing between limit and the 200-doc window
    is ever skipped."""
    from pagination import decode_cursor
    captured = {}

    def _page(status_filter=None, limit=200, cursor=None):
        captured["cursor_in"] = cursor
        return ([_dossier(did=f"d{i}", fn=f"2026-{i:03d}")
                 | {"opened_date": datetime(2026, 7, i + 1, tzinfo=UTC)}
                 for i in range(3)], None)

    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page", _page)
    payload = handlers.list_dossiers({"limit": 2})
    assert captured["cursor_in"] is None
    assert payload["count"] == 2
    assert payload["truncated"] is True
    # The token decodes to the SECOND row's [opened_date, id] — the last
    # one we actually returned.
    values = decode_cursor(payload["next_cursor"])
    assert values[1] == "d1"

    handlers.list_dossiers({"limit": 2, "cursor": payload["next_cursor"]})
    assert captured["cursor_in"] == payload["next_cursor"]

    # Last page: no more rows, no cursor.
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda **kw: ([_dossier()], None))
    final = handlers.list_dossiers({"limit": 2})
    assert final["next_cursor"] is None
    assert final["truncated"] is False


def test_offset_pages_the_materialized_tools(monkeypatch):
    tasks = [{"id": f"t{i}", "title": f"T{i}", "status": "à_faire"}
             for i in range(5)]
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda **kw: list(tasks))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk",
                        lambda ids: {})
    first = handlers.list_tasks({"limit": 2})
    assert [i["id"] for i in first["items"]] == ["t0", "t1"]
    assert first["next_offset"] == 2
    second = handlers.list_tasks({"limit": 2, "offset": first["next_offset"]})
    assert [i["id"] for i in second["items"]] == ["t2", "t3"]
    last = handlers.list_tasks({"limit": 2, "offset": 4})
    assert [i["id"] for i in last["items"]] == ["t4"]
    assert last["truncated"] is False
    assert "next_offset" not in last


def test_list_trust_transactions_newest_first(monkeypatch):
    """G07 companion the audit missed: the register was returned OLDEST
    first — the default call showed the 25 oldest movements of the
    account. A bare account_id rides the DESC index; the other shapes
    re-sort the bounded window in Python."""
    # Bare account_id → the indexed DESC page function.
    monkeypatch.setattr(
        handlers.trust_model, "list_transactions_page",
        lambda account_id, cursor=None, limit=25: (
            [{"id": "t3", "sequence": 3, "amount": 100},
             {"id": "t2", "sequence": 2, "amount": 100}], "cur"),
    )
    payload = handlers.list_trust_transactions({"account_id": "a1", "limit": 2})
    assert [t["sequence"] for t in payload["transactions"]] == [3, 2]
    assert payload["truncated"] is True

    # Filtered shape → bounded fetch re-sorted DESC in Python.
    monkeypatch.setattr(
        handlers.trust_model, "list_transactions",
        lambda **kw: [{"id": "t1", "sequence": 1, "amount": 100,
                       "account_id": "a1", "status": "compensée"},
                      {"id": "t2", "sequence": 2, "amount": 100,
                       "account_id": "a1", "status": "compensée"}],
    )
    payload = handlers.list_trust_transactions(
        {"account_id": "a1", "status": "compensée", "limit": 25}
    )
    assert [t["sequence"] for t in payload["transactions"]] == [2, 1]


def test_dossier_labels_are_freshened_from_the_live_dossier(monkeypatch):
    """PA-D04: dossier_file_number/dossier_title are creation-time
    snapshots — they survived a renumbering (the audit saw « 250701 », a
    number that no longer exists) and intitulé corrections. Rows now join
    the live dossier in ONE batched read; the stored snapshot remains the
    fallback (bulk fails open to {}, deleted dossiers keep their label)."""
    stale = {"id": "t1", "title": "Produire la proposition",
             "status": "à_faire", "dossier_id": "d-old",
             "dossier_file_number": "250701",
             "dossier_title": "Dolores Pepin c. 9313-5630 Québec Inc."}
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda **kw: [dict(stale)])
    captured_ids = {}

    def _bulk(ids):
        captured_ids["ids"] = list(ids)
        return {"d-old": {"id": "d-old", "file_number": "2026-012",
                          "title": "9313-5630 Québec Inc. c. Dolores Pepin"}}

    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", _bulk)
    row = handlers.list_tasks({})["items"][0]
    assert captured_ids["ids"] == ["d-old"]
    assert row["dossier_file_number"] == "2026-012"
    assert row["dossier_title"] == "9313-5630 Québec Inc. c. Dolores Pepin"

    # Fail-open: an empty bulk return keeps the stored snapshot — a read
    # blip must never blank every label on the page.
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk",
                        lambda ids: {})
    row = handlers.list_tasks({})["items"][0]
    assert row["dossier_file_number"] == "250701"


def test_rows_carry_timestamps_and_updated_since_filters(monkeypatch):
    """PA-G05: every row emits created_at/updated_at (already stored — the
    gap was emission-only), and the materialized tools take updated_since.
    A bare YYYY-MM-DD reads as a Montréal calendar day."""
    old = {"id": "t-old", "title": "Vieille", "status": "à_faire",
           "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
           "updated_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}
    fresh = {"id": "t-new", "title": "Récente", "status": "à_faire",
             "created_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
             "updated_at": datetime(2026, 7, 29, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda **kw: [old, fresh])
    everything = handlers.list_tasks({})
    assert everything["items"][0]["updated_at"] is not None
    assert everything["items"][0]["created_at"] is not None

    recent = handlers.list_tasks({"updated_since": "2026-07-01"})
    assert [i["id"] for i in recent["items"]] == ["t-new"]

    # A legacy row with no updated_at never matches a cutoff (can't claim
    # freshness it cannot prove).
    legacy = {"id": "t-legacy", "title": "Héritée", "status": "à_faire"}
    monkeypatch.setattr(handlers.task_model, "list_tasks",
                        lambda **kw: [legacy])
    assert handlers.list_tasks({"updated_since": "2020-01-01"})["items"] == []
    # …but still lists fine without the filter, with null stamps.
    row = handlers.list_tasks({})["items"][0]
    assert row["created_at"] is None and row["updated_at"] is None


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


def test_list_documents_resolves_folder_path_per_row(monkeypatch):
    """PA-G11: folder_id used to be a bare UUID with no resolver unless the
    CALLER already knew the folder. One folder-tree query per request, an
    id→path map, never per-row breadcrumb walks."""
    tree = [{"id": "f1", "name": "Procédures", "children": [
        {"id": "f2", "name": "Significations", "children": []},
    ]}]
    docs = [
        {"id": "doc1", "display_name": "PV Solo.pdf", "folder_id": "f2",
         "document_date": datetime(2026, 7, 15, tzinfo=UTC),
         "created_at": datetime(2026, 7, 25, 14, 0, tzinfo=UTC)},
        {"id": "doc2", "display_name": "Racine.pdf", "folder_id": None},
    ]
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: list(docs))
    monkeypatch.setattr(handlers.folder_model, "get_folder_tree",
                        lambda d: tree)
    payload = handlers.list_documents({"dossier_id": "d1"})
    by_id = {i["id"]: i for i in payload["items"]}
    assert by_id["doc1"]["folder_path"] == "Procédures / Significations"
    assert by_id["doc2"]["folder_path"] == ""          # dossier root
    # PA-G03: the document's OWN date, not the upload instant.
    assert by_id["doc1"]["document_date"] == "2026-07-15"
    assert by_id["doc2"]["document_date"] is None


def test_list_documents_date_window_uses_the_effective_date(monkeypatch):
    """The window reads document_date when set, else the upload date — a
    July-uploaded PV dated the 15th matches « July 15 », and an undated
    legacy doc stays findable by its upload period."""
    docs = [
        {"id": "dated", "folder_id": None,
         "document_date": datetime(2026, 7, 15, tzinfo=UTC),
         "created_at": datetime(2026, 7, 25, 14, 0, tzinfo=UTC)},
        {"id": "undated", "folder_id": None,
         "created_at": datetime(2026, 7, 25, 14, 0, tzinfo=UTC)},
    ]
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: list(docs))
    monkeypatch.setattr(handlers.folder_model, "get_folder_tree", lambda d: [])
    on_the_15th = handlers.list_documents(
        {"dossier_id": "d1", "date_from": "2026-07-15",
         "date_to": "2026-07-15"}
    )
    assert [i["id"] for i in on_the_15th["items"]] == ["dated"]
    on_the_25th = handlers.list_documents(
        {"dossier_id": "d1", "date_from": "2026-07-25",
         "date_to": "2026-07-25"}
    )
    assert [i["id"] for i in on_the_25th["items"]] == ["undated"]


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
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 77434})
    unbilled_time = [
        {"id": "e1", "dossier_id": "d1", "dossier_file_number": "2026-018",
         "dossier_title": "Gaudreau c. Bossé", "billable": True,
         "invoiced": False, "hours": 2.0, "amount": 50000},
        # non-billable row: non_facture includes it, hours must NOT count it
        {"id": "e2", "dossier_id": "d1", "dossier_file_number": "2026-018",
         "dossier_title": "Gaudreau c. Bossé", "billable": False,
         "invoiced": False, "hours": 9.0, "amount": 0},
        {"id": "e3", "dossier_id": "d2", "dossier_file_number": "2026-027",
         "dossier_title": "Hraki c. Solo", "billable": True,
         "invoiced": False, "hours": 1.5, "amount": 37500},
    ]
    unbilled_exp = [
        {"id": "x1", "dossier_id": "d1", "dossier_file_number": "2026-018",
         "dossier_title": "Gaudreau c. Bossé", "amount": 57495},
        {"id": "x2", "dossier_id": "d2", "dossier_file_number": "2026-027",
         "dossier_title": "Hraki c. Solo", "amount": 19939},
    ]
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        lambda **kw: (unbilled_time, None))
    monkeypatch.setattr(handlers.expense_model, "list_expenses_page",
                        lambda **kw: (unbilled_exp, None))
    payload = handlers.get_billing_snapshot({})
    assert payload["scope"] == "global"
    assert {i["id"] for i in payload["outstanding_invoices"]} == {"i1", "i3"}
    assert payload["outstanding_invoices"][0]["date"] == "2026-06-01"
    assert payload["outstanding_display"] == f"3{NBSP}000,00{NBSP}$"
    # PA-G09: the firm-wide figure now carries the unbilled disbursements…
    assert payload["unbilled_expenses_cents"] == 77434
    # …and by_dossier says which files hold the WIP, newest file first.
    assert [b["file_number"] for b in payload["by_dossier"]] == [
        "2026-027", "2026-018"
    ]
    d1 = payload["by_dossier"][1]
    assert d1["unbilled_hours"] == 2.0          # billable-only (e2 excluded)
    assert d1["unbilled_fees_cents"] == 50000
    assert d1["unbilled_expenses_cents"] == 57495
    assert payload["by_dossier_truncated"] is False


def test_list_time_entries_shows_invoiced_rows(monkeypatch):
    """PA-G04: an invoiced entry used to be unreachable through the
    connector — this is the work-history view."""
    rows = [
        {"id": "e1", "dossier_id": "d1", "dossier_file_number": "2026-027",
         "dossier_title": "Hraki c. Solo",
         "date": datetime(2026, 7, 20, tzinfo=UTC),
         "description": "Rédaction de la demande", "hours": 3.5,
         "rate": 30000, "amount": 105000, "billable": True,
         "invoiced": True, "invoice_id": "inv-9"},
        {"id": "e2", "dossier_id": "d1", "dossier_file_number": "2026-027",
         "dossier_title": "Hraki c. Solo",
         "date": datetime(2026, 7, 22, tzinfo=UTC),
         "description": "Appel du client", "hours": 0.5,
         "rate": 30000, "amount": 15000, "billable": True,
         "invoiced": False},
    ]
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        lambda **kw: (rows, None))
    payload = handlers.list_time_entries({"dossier_id": "d1"})
    assert payload["count"] == 2
    assert payload["items"][0]["invoiced"] is True
    assert payload["items"][0]["invoice_id"] == "inv-9"
    assert payload["items"][0]["date"] == "2026-07-20"
    assert payload["items"][0]["amount_display"] == f"1{NBSP}050,00{NBSP}$"
    assert payload["items"][1]["invoice_id"] is None
    assert payload["truncated"] is False


def test_the_two_billing_lists_read_back_the_phase(monkeypatch):
    """Le connecteur ÉCRIT phase/sous_phase depuis Phase O et aucun
    constructeur de rangée ne les relisait : il enregistrait une
    classification qu'il ne pouvait pas vérifier. C'est précisément la
    demande « complete access … including the phase and subphase
    categorization »."""
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: ([{
            "id": "e1", "dossier_id": "d1",
            "date": datetime(2026, 7, 20, tzinfo=UTC),
            "description": "Rédaction", "hours": 1.5, "rate": 30000,
            "amount": 45000, "billable": True,
            "phase": "CTS", "sous_phase": "CTS-02", "created_via": "mcp",
        }], None))
    monkeypatch.setattr(
        handlers.expense_model, "list_expenses_page",
        lambda **kw: ([{
            "id": "x1", "dossier_id": "d1",
            "date": datetime(2026, 7, 21, tzinfo=UTC),
            "description": "Timbre", "amount": 5000, "taxable": True,
            "phase": "PRE", "sous_phase": "PRE-00",
        }], None))

    entry = handlers.list_time_entries({"dossier_id": "d1"})["items"][0]
    assert entry["phase"] == "CTS"
    assert entry["sous_phase"] == "CTS-02"
    assert entry["phase_label"] == "Contestation"
    assert entry["created_via"] == "mcp"

    disb = handlers.list_expenses({"dossier_id": "d1"})["items"][0]
    assert disb["phase"] == "PRE"
    assert disb["sous_phase_label"]
    assert disb["created_via"] == ""      # saisi dans l'application


def test_the_phase_label_is_bare_never_repeating_the_code(monkeypatch):
    """phases.sous_phase_label rend « Libellé [CODE] » — l'employer ici
    répéterait le code que la rangée porte déjà dans sa propre clé. Les
    libellés viennent donc de PHASE_LABELS / SOUS_PHASE_LABELS."""
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: ([{"id": "e1", "dossier_id": "d1", "hours": 1.0,
                        "rate": 100, "amount": 100,
                        "phase": "CTS", "sous_phase": "CTS-02"}], None))
    row = handlers.list_time_entries({"dossier_id": "d1"})["items"][0]
    assert "[" not in row["sous_phase_label"]
    assert "CTS-02" not in row["sous_phase_label"]


def test_an_unphased_row_reads_non_renseignee(monkeypatch):
    """« » est une valeur RÉELLE du vocabulaire, pas un champ manquant : les
    documents hérités n'ont jamais été rétro-remplis, et la rangée doit le
    dire au lieu de laisser un blanc muet."""
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: ([{"id": "e1", "dossier_id": "d1", "hours": 1.0,
                        "rate": 100, "amount": 100}], None))
    row = handlers.list_time_entries({"dossier_id": "d1"})["items"][0]
    assert row["phase"] == "" and row["sous_phase"] == ""
    assert row["phase_label"] == "Non renseignée"
    assert row["sous_phase_label"] == "Non renseignée"


def test_a_task_row_carries_its_phase_too(monkeypatch):
    monkeypatch.setattr(
        handlers.task_model, "list_tasks",
        lambda **kw: [{"id": "t1", "title": "Déposer", "status": "à_faire",
                       "phase": "INS", "sous_phase": "INS-00"}])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk",
                        lambda ids: {})
    row = handlers.list_tasks({})["items"][0]
    assert row["phase"] == "INS"
    assert row["phase_label"]


def test_list_time_entries_routes_the_unsupported_combo_in_python(monkeypatch):
    """dossier_id + billable_filter TOGETHER is unindexed server-side and
    the page function swallows the FAILED_PRECONDITION into [] — passing
    both through would silently return nothing. The handler must fetch by
    dossier+dates and filter the flag in Python."""
    captured = {}

    def _page(**kw):
        captured.update(kw)
        return ([
            {"id": "e1", "dossier_id": "d1", "billable": True,
             "invoiced": False, "hours": 1.0, "amount": 25000,
             "date": datetime(2026, 7, 1, tzinfo=UTC)},
            {"id": "e2", "dossier_id": "d1", "billable": True,
             "invoiced": True, "hours": 2.0, "amount": 50000,
             "date": datetime(2026, 7, 2, tzinfo=UTC)},
        ], None)

    monkeypatch.setattr(handlers.time_entry_model,
                        "list_time_entries_page", _page)
    payload = handlers.list_time_entries(
        {"dossier_id": "d1", "billable_filter": "non_facture"}
    )
    assert "billable_filter" not in captured          # never sent server-side
    assert captured["dossier_id"] == "d1"
    assert [i["id"] for i in payload["items"]] == ["e1"]


def test_list_expenses_truncation_reflects_the_window(monkeypatch):
    """A non-None cursor from the model means the ≤200 window itself was
    full — truncated must say so even when fewer than `limit` rows return."""
    rows = [{"id": "x1", "dossier_id": "d1", "amount": 5000, "taxable": True,
             "invoiced": False, "category": "expertise",
             "date": datetime(2026, 7, 1, tzinfo=UTC)}]
    monkeypatch.setattr(handlers.expense_model, "list_expenses_page",
                        lambda **kw: (rows, "curseur-opaque"))
    payload = handlers.list_expenses({})
    assert payload["count"] == 1
    assert payload["truncated"] is True


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
    # FROZEN day, fixed deadlines. Lateness evaluates on the PROROGUED
    # deadline (2026-08-02 decision), so a clock-derived offset lands on a
    # weekend some days and reads as not-yet-late. This test once answered
    # that by widening the offset from 2 days to 7 — a probabilistic patch
    # that left the same landmine armed elsewhere (see the agenda test
    # above, which broke a build). Freezing the day settles it outright.
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))       # a Friday
    past = datetime(2026, 7, 24, tzinfo=UTC)                # a Friday
    future = datetime(2026, 8, 28, tzinfo=UTC)
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
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda d: _dossier(did=d, fn="2026-018"))
    payload = handlers.list_protocol_steps({"dossier_id": "d1"})
    steps = payload["protocols"][0]["steps"]
    assert [s["is_overdue"] for s in steps] == [True, False, False]
    assert payload["has_active_protocol"] is True
    # The audit's live case (PA-D03): a cq_simplifié (arts. 535.x) on a
    # Superior Court dossier — the payload must say so, not hide it.
    assert payload["protocols"][0]["regime_mismatch"] is True
    assert payload["protocols"][0]["dossier_tribunal"] == "Cour supérieure"


def test_step_and_task_due_today_are_not_overdue(monkeypatch):
    # FROZEN, and not merely to be deterministic: with a clock-derived
    # `now(UTC).date()` this test QUIETLY STOPPED testing its own claim in
    # the 00:00-04:00 UTC band, where that date is already TOMORROW in
    # Montréal — it went on passing while asserting nothing about « due
    # today ».
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))       # a Friday
    today_midnight = datetime(2026, 7, 31, tzinfo=UTC)
    protocol = {"id": "p1", "status": "actif",
                "steps": [{"id": "s1", "order": 1, "title": "Dépôt",
                           "status": "à_venir", "deadline_date": today_midnight}]}
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: protocol)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda d: _dossier(did=d))
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
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 0})
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
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda d: _dossier(did=d))
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
    """An alpha prefix resolves to its tribunal via _FORUMS (PA-D09) —
    the prefix IS the answer to the question the tool was asked."""
    payload = handlers.parse_court_file_number({"court_file_number": "TAL-12345"})
    assert payload["is_administrative"] is True
    assert payload["tribunal"] == "Tribunal administratif du logement"
    assert payload["parse_error"] is None


def test_parse_court_file_number_federal_is_not_administrative():
    """Federal courts resolve by dotted or dotless prefix, and are NOT
    administrative tribunals (reference.py design note)."""
    for raw in ("C.F.-T-1234-26", "CF-T-1234-26"):
        payload = handlers.parse_court_file_number({"court_file_number": raw})
        assert payload["tribunal"] == "Cour fédérale", raw
        assert payload["is_administrative"] is False, raw
        assert payload["parse_error"] is None, raw


def test_parse_court_file_number_unmapped_prefix_keeps_historical_shape():
    """An unknown alpha prefix stays conservatively flagged administrative
    with tribunal null — never an error, never a guessed name."""
    payload = handlers.parse_court_file_number({"court_file_number": "XYZ-9999"})
    assert payload["is_administrative"] is True
    assert payload["tribunal"] is None
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


# ── WP16 creators: the pinned write invariants, per tool ────────────────


def test_create_task_bumps_collection_for_and_pins_status(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    created = {}

    def _create(data):
        created.update(data)
        return {**data, "id": "t-new"}, []

    monkeypatch.setattr(handlers.task_model, "create_task", _create)
    payload = handlers.create_task({
        "dossier_id": "d1", "title": "Produire la réponse",
        "due_date": "2026-08-05",
        # injection attempts — must never reach the model:
        "status": "terminée", "id": "victim", "completed_date": "2020-01-01",
    })
    assert bumps["bump"] == ["dossier:d1"]
    assert payload["ctag_bumped"] is True
    assert created["status"] == "à_faire"          # pinned, never caller's
    assert "id" not in created
    assert "completed_date" not in created
    assert created["dossier_file_number"] == "2026-001"
    assert created["created_via"] == "mcp"
    # Provenance lives in the description, dated.
    assert "Créée par Claude" in created["description"]


def test_create_task_general_stores_none_and_bumps_tasks(monkeypatch, bumps):
    """Tasks store None for « no dossier » (notes/hearings store "") — the
    model convention collection_for depends on."""
    created = {}

    def _create(data):
        created.update(data)
        return {**data, "id": "t-new"}, []

    monkeypatch.setattr(handlers.task_model, "create_task", _create)
    handlers.create_task({"title": "Veille hebdo"})
    assert created["dossier_id"] is None
    assert bumps["bump"] == ["general"]     # the « Général » collection


def test_create_task_refuses_unknown_dossier_without_writing(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)

    def _must_not_run(_data):
        raise AssertionError("create_task reached the model with a bad dossier")

    monkeypatch.setattr(handlers.task_model, "create_task", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
        handlers.create_task({"dossier_id": "nope", "title": "T"})
    assert bumps["bump"] == []


def test_create_task_refuses_the_2000_char_ceiling(monkeypatch, bumps):
    """task._sanitize_data truncates at 2000 chars — refuse loudly, never
    let the model truncate a computed deadline's justification silently."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    with pytest.raises(tools.ToolArgumentError, match="2000"):
        handlers.create_task({"dossier_id": "d1", "title": "T",
                              "description": "x" * 2001})


def test_create_task_dry_run_writes_nothing(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())

    def _must_not_run(_data):
        raise AssertionError("dry_run reached the model")

    monkeypatch.setattr(handlers.task_model, "create_task", _must_not_run)
    payload = handlers.create_task(
        {"dossier_id": "d1", "title": "T", "dry_run": True}
    )
    assert payload["dry_run"] is True
    assert payload["entity"]["id"] == ""
    assert bumps["bump"] == []                     # no CTag on a simulation
    assert any("Simulation" in w for w in payload["warnings"])


def test_create_hearing_times_are_montreal_and_bump_fires(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    created = {}

    def _create(data):
        created.update(data)
        return {**data, "id": "h-new"}, []

    monkeypatch.setattr(handlers.hearing_model, "create_hearing", _create)
    handlers.create_hearing({
        "dossier_id": "d1", "title": "Interrogatoire",
        "hearing_type": "interrogatoire",
        "date": "2026-09-10", "start_time": "09:30",
        "id": "victim", "vevent_uid": "x",     # injection — must not pass
    })
    assert bumps["bump"] == ["dossier:d1"]
    # 09:30 Montréal (EDT, UTC-4) = 13:30 UTC.
    assert created["start_datetime"].hour == 13
    assert created["start_datetime"].tzinfo is not None
    assert "id" not in created
    assert "vevent_uid" not in created
    assert created["created_via"] == "mcp"
    assert "Créée par Claude" in created["notes"]


def test_create_hearing_defaults_to_rencontre(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    created = {}
    monkeypatch.setattr(
        handlers.hearing_model, "create_hearing",
        lambda data: (created.update(data) or ({**data, "id": "h"}, [])),
    )
    payload = handlers.create_hearing(
        {"dossier_id": "d1", "title": "Rendez-vous", "date": "2026-09-10"}
    )
    assert created["hearing_type"] == "rencontre"
    assert payload["entity"]["forum"] == "extrajudiciaire"
    assert created["all_day"] is True              # no start_time given


def test_create_time_entry_defaults_to_the_dossier_rate(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: {**_wdossier(), "hourly_rate": 32500},
    )
    created = {}
    monkeypatch.setattr(
        handlers.time_entry_model, "create_time_entry",
        lambda data: (created.update(data) or ({**data, "id": "e",
                                                "amount": 48750}, [])),
    )
    payload = handlers.create_time_entry({
        "dossier_id": "d1", "date": "2026-07-30",
        "description": "Rédaction", "hours": 1.5,
    })
    assert created["rate"] == 32500                # dossier default
    assert created["invoiced"] is False
    assert created["created_via"] == "mcp"
    # NO provenance text — the description prints on the client's invoice.
    assert "Claude" not in created["description"]
    assert "ctag_bumped" not in payload            # not DAV-exposed


def test_create_expense_requires_dossier_and_positive_amount(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    with pytest.raises(tools.ToolArgumentError, match="dossier_id"):
        handlers.create_expense({"date": "2026-07-30",
                                 "description": "X", "amount_cents": 100})
    with pytest.raises(tools.ToolArgumentError, match="amount_cents"):
        handlers.create_expense({"dossier_id": "d1", "date": "2026-07-30",
                                 "description": "X", "amount_cents": 0})


def test_wp16_enums_track_the_models():
    """The tools.py literals are hand-copied (firestore-at-import rule) —
    pin them against the models so they cannot drift."""
    from models import expense as expense_model
    from models import hearing as hearing_model
    from models import task as task_model

    assert (tools.TOOLS["create_task"]["input_schema"]["properties"]
            ["priority"]["enum"] == list(task_model.VALID_PRIORITIES))
    assert (tools.TOOLS["create_task"]["input_schema"]["properties"]
            ["category"]["enum"] == list(task_model.VALID_CATEGORIES))
    assert (tools.TOOLS["create_hearing"]["input_schema"]["properties"]
            ["hearing_type"]["enum"] == list(hearing_model.VALID_HEARING_TYPES))
    assert (tools.TOOLS["create_expense"]["input_schema"]["properties"]
            ["category"]["enum"] == list(expense_model.VALID_CATEGORIES))


# ── WP17 dossier mutators: fill-only + append-only recorders ────────────


def _wdossier_parties(**over):
    doc = {
        **_wdossier(),
        "clients": [{"id": "p1", "name": "Jean Tremblay"}],
        "opposing_parties": [{"id": "p2", "name": "Paul Lavoie"}],
    }
    doc.update(over)
    return doc


def test_complete_dossier_fills_only_the_empty_fields(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(domaine="", action="", sommaire=""),
    )
    written = {}
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: (written.update(data)
                           or ({**_wdossier_parties(), **data}, [])),
    )
    payload = handlers.complete_dossier({
        "dossier_id": "d1", "domaine": "REC", "action": "REC-01",
        "sommaire": "Réclamation sur compte.",
    })
    assert set(written) == {"domaine", "action", "sommaire"}
    assert payload["fields_set"] == ["action", "domaine", "sommaire"]
    assert "prescription_status" in payload


def test_complete_dossier_conflict_is_atomic(monkeypatch):
    """One conflicting field poisons the WHOLE call: the empty sommaire
    must not be filled either — a partial fill leaves the caller guessing
    which half happened."""
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(domaine="REC", sommaire=""),
    )

    def _must_not_run(_did, _data):
        raise AssertionError("conflict must refuse BEFORE update_dossier")

    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="jamais écrasés"):
        handlers.complete_dossier({
            "dossier_id": "d1", "domaine": "CON",       # conflicts
            "sommaire": "Nouveau résumé.",              # would fill
        })


def test_complete_dossier_default_value_counts_as_empty(monkeypatch):
    """« Empty » ≡ still equal to the model default — the untouched
    hourly_rate 30000 is fillable, not a conflict."""
    defaults = handlers.dossier_model.field_defaults()
    assert defaults["hourly_rate"] == 30000
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(hourly_rate=30000),
    )
    written = {}
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: (written.update(data)
                           or ({**_wdossier_parties(), **data}, [])),
    )
    handlers.complete_dossier({"dossier_id": "d1", "hourly_rate": 35000})
    assert written == {"hourly_rate": 35000}


def test_complete_dossier_identical_values_are_a_quiet_skip(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(domaine="REC", sommaire=""),
    )
    written = {}
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: (written.update(data)
                           or ({**_wdossier_parties(), **data}, [])),
    )
    payload = handlers.complete_dossier({
        "dossier_id": "d1", "domaine": "REC",           # identical → skip
        "sommaire": "Résumé.",                          # fills
    })
    assert written == {"sommaire": "Résumé."}
    assert payload["fields_already_identical"] == ["domaine"]
    # ALL identical → nothing to do, said plainly, never a silent success.
    with pytest.raises(tools.ToolArgumentError, match="déjà ces valeurs"):
        handlers.complete_dossier({"dossier_id": "d1", "domaine": "REC"})


def test_complete_dossier_court_file_number_derives_fill_only(monkeypatch):
    """Filling the number mirrors the web form's parse step — but the
    derived fields obey the same fill-only rule (tribunal already set
    stays untouched)."""
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(
            court_file_number="", tribunal="Cour d'appel",
        ),
    )
    written = {}
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: (written.update(data)
                           or ({**_wdossier_parties(), **data}, [])),
    )
    handlers.complete_dossier({
        "dossier_id": "d1", "court_file_number": "500-05-123456-241",
    })
    assert written["court_file_number"] == "500-05-123456-241"
    assert written["district_judiciaire"] == "Montréal"
    assert written["greffe_number"] == "500"
    assert "tribunal" not in written               # pre-filled → untouched


def test_complete_dossier_dry_run_never_reaches_the_model(monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(domaine=""),
    )

    def _must_not_run(_did, _data):
        raise AssertionError("dry_run reached update_dossier")

    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _must_not_run)
    payload = handlers.complete_dossier(
        {"dossier_id": "d1", "domaine": "REC", "dry_run": True}
    )
    assert payload["fields_set"] == ["domaine"]
    assert any("Simulation" in w for w in payload["warnings"])


def test_record_signification_refuses_a_stranger_partie(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier_parties())

    def _must_not_run(_did, _data):
        raise AssertionError("stranger partie reached update_dossier")

    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="partie au dossier"):
        handlers.record_signification({
            "dossier_id": "d1", "partie_id": "p9",
            "date": "2026-07-15", "mode": "huissier",
        })


def test_record_signification_supersedes_marks_the_old_entry(monkeypatch):
    old = {
        "id": "sig-old", "partie_id": "p2",
        "date": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "mode": "huissier", "huissier_id": "", "pv_document_id": "",
        "superseded_by": "", "confirmee": True,
    }
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(significations=[dict(old)]),
    )
    written = {}
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: (written.update(data)
                           or ({**_wdossier_parties(), **data}, [])),
    )
    payload = handlers.record_signification({
        "dossier_id": "d1", "partie_id": "p2", "date": "2026-07-15",
        "mode": "huissier", "supersedes": "sig-old", "confirmee": True,
    })
    stored = {s["id"]: s for s in written["significations"]}
    new_id = payload["entity"]["id"]
    assert stored["sig-old"]["superseded_by"] == new_id
    assert stored[new_id]["superseded_by"] == ""
    # An unknown supersedes id is refused, never silently dropped.
    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.record_signification({
            "dossier_id": "d1", "partie_id": "p2", "date": "2026-07-20",
            "mode": "huissier", "supersedes": "nope",
        })


def test_record_prescription_event_validates_through_the_model(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier_parties())

    def _must_not_run(_did, _data):
        raise AssertionError("invalid event reached update_dossier")

    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _must_not_run)
    with pytest.raises(tools.ToolArgumentError, match="invalide"):
        handlers.record_prescription_event({
            "dossier_id": "d1", "type": "bogus", "date": "2026-05-15",
        })
    with pytest.raises(tools.ToolArgumentError, match="date de fin"):
        handlers.record_prescription_event({
            "dossier_id": "d1", "type": "suspension", "date": "2026-05-15",
        })


def test_record_prescription_event_dry_run_still_answers_the_question(
    monkeypatch,
):
    """dry_run writes nothing but STILL derives the post-event status —
    that derivation is the whole reason to call the tool."""
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _wdossier_parties(prescription_events=[],
                                    prise_action_date=None),
    )

    def _must_not_run(_did, _data):
        raise AssertionError("dry_run reached update_dossier")

    monkeypatch.setattr(handlers.dossier_model, "update_dossier", _must_not_run)
    payload = handlers.record_prescription_event({
        "dossier_id": "d1", "type": "interruption_depot",
        "date": "2026-05-15", "dry_run": True,
    })
    assert payload["dry_run"] is True
    assert payload["prescription_status"] == "interrompue"
    assert payload["prescription_date_effective"] is None


def test_wp17_tools_refuse_an_unknown_dossier(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    for call in (
        lambda: handlers.complete_dossier({"dossier_id": "x", "domaine": "REC"}),
        lambda: handlers.record_signification(
            {"dossier_id": "x", "partie_id": "p1", "date": "2026-07-15"}),
        lambda: handlers.record_prescription_event(
            {"dossier_id": "x", "type": "renonciation", "date": "2026-07-15"}),
    ):
        with pytest.raises(tools.ToolArgumentError, match="Dossier introuvable"):
            call()


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

    handlers.list_notes({})  # « Général » branch — only the flag is asserted
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


# ── Lot 6: one definition of "today", one derived step status ────────────


def _step(status="à_venir", deadline=None, **over):
    doc = {
        "id": "s1", "order": 1, "title": "Réponse",
        "description": "", "cpc_reference": "art. 145(2) C.p.c.",
        "deadline_date": deadline, "status": status,
        "mandatory": True, "deadline_locked": False, "date_confirmed": True,
        "completed_date": None, "linked_task_id": None,
        "linked_hearing_id": None, "notes": "",
    }
    doc.update(over)
    return doc


def test_step_status_is_derived_not_the_stored_fossil():
    """THE audit case (step 209f0c54): the document carries a latched
    « en_retard » written by the pre-fix wall-clock rule, on a step whose
    deadline is TODAY. Nothing ever clears that word, and a read handler is
    forbidden from writing — so the connector must derive instead."""
    row = handlers._step_row(
        _step("en_retard", datetime(2026, 7, 30, tzinfo=UTC)),
        date(2026, 7, 30),
    )
    assert row["status"] == "à_venir"          # derived, and it governs
    assert row["status_stored"] == "en_retard"  # provenance kept
    assert row["status_differs"] is True        # the fossil is made visible
    assert row["is_overdue"] is False


def test_step_status_and_is_overdue_can_never_contradict():
    """The pair the audit found impossible to trust. Both come from ONE
    predicate now, so the invariant holds over the whole grid rather than
    by convention."""
    today = date(2026, 7, 31)
    offsets = [-10, -1, 0, 1, 10, None]
    for stored in ("à_venir", "en_cours", "en_retard", "complété", ""):
        for offset in offsets:
            deadline = (
                None if offset is None
                else datetime(2026, 7, 31, tzinfo=UTC) + timedelta(days=offset)
            )
            row = handlers._step_row(_step(stored, deadline), today)
            assert (row["status"] == "en_retard") == row["is_overdue"], (
                stored, offset, row["status"], row["is_overdue"]
            )
            # A completed step is never overdue, however old its deadline.
            if stored == "complété":
                assert row["status"] == "complété"
                assert row["is_overdue"] is False


def test_completion_is_a_fact_never_re_derived():
    row = handlers._step_row(
        _step("complété", datetime(2020, 1, 1, tzinfo=UTC)), date(2026, 7, 31)
    )
    assert row["status"] == "complété"
    assert row["status_differs"] is False


def test_undated_step_keeps_en_cours_but_falls_back_to_a_venir():
    today = date(2026, 7, 31)
    assert handlers._step_row(_step("en_cours", None), today)["status"] == "en_cours"
    assert handlers._step_row(_step("en_retard", None), today)["status"] == "à_venir"


def test_task_row_carries_is_overdue_on_every_surface():
    """list_tasks emitted no is_overdue at all, so no client could derive
    lateness. It is now an unconditional row key."""
    today = date(2026, 7, 31)
    late = handlers._task_row(
        {"id": "t", "status": "à_faire",
         "due_date": datetime(2026, 7, 30, tzinfo=UTC)}, today=today)
    assert late["is_overdue"] is True
    due_today = handlers._task_row(
        {"id": "t", "status": "à_faire",
         "due_date": datetime(2026, 7, 31, tzinfo=UTC)}, today=today)
    assert due_today["is_overdue"] is False
    undated = handlers._task_row({"id": "t", "status": "à_faire"}, today=today)
    assert undated["is_overdue"] is False


def test_a_closed_task_is_never_overdue():
    """Whatever its due date says — otherwise every completed task in the
    history reads as late."""
    today = date(2026, 7, 31)
    for status in ("terminée", "annulée"):
        row = handlers._task_row(
            {"id": "t", "status": status,
             "due_date": datetime(2020, 1, 1, tzinfo=UTC)}, today=today)
        assert row["is_overdue"] is False, status


def _agenda_world(monkeypatch, *, hearings=(), tasks=(), steps=()):
    """Minimal get_agenda world: only the reads it performs."""
    captured = {}

    def _range(start, end, limit=100):
        captured["hearing_start"] = start
        captured["hearing_end"] = end
        return list(hearings)

    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range", _range)
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda cutoff, limit=50: list(tasks))
    monkeypatch.setattr(handlers.protocol_model, "list_urgent_steps",
                        lambda cutoff, limit=50: list(steps))
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda cutoff, limit=50: [])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 3)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 1.0, "amount": 1000})
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda **kw: {"amount": 0})
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total", lambda: 0)
    return captured


def _freeze_mtl_today(monkeypatch, value: date):
    monkeypatch.setattr(handlers.deadlines, "today_mtl", lambda: value)


def test_get_agenda_window_opens_at_midnight_montreal(monkeypatch):
    """The lower bound was the INSTANT now, so a 09:00 hearing vanished at
    09:01 from a window whose own `from` claimed today was included."""
    captured = _agenda_world(monkeypatch)
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))
    payload = handlers.get_agenda({})
    assert payload["window"]["from"] == "2026-07-31"
    start = captured["hearing_start"]
    # Midnight Montréal on the 31st = 04:00 UTC (EDT). The query must reach
    # back to it, never start mid-morning.
    assert start.astimezone(MTL).date() == date(2026, 7, 31)
    assert start.astimezone(MTL).hour == 0


def test_get_agenda_is_stable_across_the_utc_day_boundary(monkeypatch):
    """The audit's « window started 2026-07-30 on a 31 July call ». The
    window was always Montréal-correct; what contradicted it was is_overdue
    being computed on the UTC date. Same Montréal day in, same answer out."""
    step = {"id": "s", "status": "à_venir",
            "deadline_date": datetime(2026, 7, 30, tzinfo=UTC),
            "_dossier_id": "", "_protocol_id": "p", "_protocol_title": "P",
            "_dossier_file_number": ""}
    seen = []
    for _ in range(2):
        # Two calls that a UTC clock would place on different days but a
        # Montréal clock places on the same one.
        _agenda_world(monkeypatch, steps=[step])
        _freeze_mtl_today(monkeypatch, date(2026, 7, 30))
        payload = handlers.get_agenda({})
        seen.append((payload["window"]["from"],
                     payload["urgent_protocol_steps"][0]["is_overdue"],
                     payload["urgent_protocol_steps"][0]["status"]))
    assert seen[0] == seen[1]
    window_from, overdue, status = seen[0]
    assert window_from == "2026-07-30"
    # The window says the 30th is included, so the 30th is NOT yet past.
    assert overdue is False and status == "à_venir"


def test_get_agenda_urgent_tasks_use_the_montreal_day(monkeypatch):
    task = {"id": "t", "status": "à_faire", "title": "Produire",
            "due_date": datetime(2026, 7, 31, tzinfo=UTC)}
    _agenda_world(monkeypatch, tasks=[task])
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))
    payload = handlers.get_agenda({})
    assert payload["urgent_tasks"][0]["is_overdue"] is False
    _agenda_world(monkeypatch, tasks=[task])
    _freeze_mtl_today(monkeypatch, date(2026, 8, 1))
    assert handlers.get_agenda({})["urgent_tasks"][0]["is_overdue"] is True


def test_list_protocol_steps_never_writes_and_derives(monkeypatch):
    """The read path is forbidden from repairing the stored word — pinned
    here beside the derivation that replaces the repair."""
    def forbidden(*_a, **_k):
        raise AssertionError("check_overdue_steps writes to Firestore")

    monkeypatch.setattr(handlers.protocol_model, "check_overdue_steps", forbidden)
    monkeypatch.setattr(
        handlers.protocol_model, "get_protocol_for_dossier",
        lambda did, active_only=True: {
            "id": "p1", "dossier_id": "d1", "title": "Protocole",
            "protocol_type": "cs_ordinaire", "status": "actif",
            "start_date": None, "end_date": None, "court": "Cour supérieure",
            "notes": "",
            "steps": [_step("en_retard", datetime(2026, 8, 5, tzinfo=UTC))],
        },
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "tribunal": "Cour supérieure"})
    _freeze_mtl_today(monkeypatch, date(2026, 7, 31))
    payload = handlers.list_protocol_steps({"dossier_id": "d1"})
    step = payload["protocols"][0]["steps"][0]
    assert step["status"] == "à_venir"
    assert step["status_stored"] == "en_retard"
    assert step["is_overdue"] is False


# ── Lot 2: cursor pagination on the four windowed tools ─────────────────
#
# The defect: `truncated` warned there was more and NOTHING could fetch it.
# On a billing statement that is a false statement by omission.


def _entries(n, *, day_start=1):
    """n time entries, one per day, newest first as the model returns them."""
    return [
        {"id": f"e{i:03d}",
         "date": datetime(2026, 7, day_start + i, tzinfo=UTC),
         "description": f"Rédaction {i}", "hours": 1.0, "rate": 30000,
         "amount": 30000, "billable": True, "invoiced": False,
         "dossier_id": "d1", "dossier_file_number": "2026-001",
         "dossier_title": "T"}
        for i in range(n)
    ][::-1]


def _paged_model(rows):
    """A fake list_*_page honouring cursor + limit over `rows`, exactly as
    the Firestore-backed one does (date DESC, id DESC)."""
    ordered = sorted(rows, key=lambda r: (r["date"], r["id"]), reverse=True)

    def _page(dossier_id=None, billable_filter=None, date_from=None,
              date_to=None, limit=200, cursor=None):
        remaining = ordered
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            marker = (values[0], values[1])
            remaining = [r for r in ordered if (r["date"], r["id"]) < marker]
        window = remaining[:limit]
        nxt = encode_cursor(
            [window[-1]["date"], window[-1]["id"]]
        ) if len(remaining) > limit else None
        return window, nxt

    return _page


def _walk_tool(fn, base_args, key="items"):
    """Page a tool to exhaustion; return the ids served in order."""
    served, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 60, "tool pagination did not terminate"
        args = dict(base_args)
        if cursor:
            args["cursor"] = cursor
        payload = fn(args)
        served.extend(r["id"] for r in payload[key])
        cursor = payload.get("next_cursor")
        if not cursor:
            assert payload["truncated"] is False or not payload[key], (
                "a last page must not claim there is more without a cursor"
            )
            return served


def test_list_time_entries_walks_a_full_month_without_loss(monkeypatch):
    """The mandate's acceptance criterion: a paged walk of a whole period
    equals the direct count, no duplicate, no omission."""
    rows = _entries(31)
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    served = _walk_tool(handlers.list_time_entries, {"limit": 7})
    assert len(served) == 31
    assert len(set(served)) == 31
    assert set(served) == {r["id"] for r in rows}


def test_list_time_entries_pages_newest_first(monkeypatch):
    rows = _entries(5)
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    first = handlers.list_time_entries({"limit": 2})
    assert [r["date"] for r in first["items"]] == ["2026-07-05", "2026-07-04"]
    assert first["truncated"] is True
    assert first["next_cursor"]
    second = handlers.list_time_entries(
        {"limit": 2, "cursor": first["next_cursor"]})
    assert [r["date"] for r in second["items"]] == ["2026-07-03", "2026-07-02"]


def test_next_cursor_is_always_present_never_omitted(monkeypatch):
    """Null on the last page, not absent — an omitted key is one a client
    forgets to check."""
    rows = _entries(2)
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    payload = handlers.list_time_entries({"limit": 25})
    assert "next_cursor" in payload
    assert payload["next_cursor"] is None
    assert payload["truncated"] is False


def test_cursor_is_minted_from_the_last_ROW_not_the_model_window(monkeypatch):
    """The dossier_id + billable_filter branch re-filters in Python, so the
    model's window cursor points PAST rows this handler dropped. Minting
    from it would skip them silently — the exact class of defect this lot
    exists to remove."""
    rows = _entries(6)
    # Half the window is non-billable and gets dropped in Python.
    for r in rows[:3]:
        r["billable"] = False
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    served = _walk_tool(
        handlers.list_time_entries,
        {"limit": 2, "dossier_id": "d1", "billable_filter": "billable"},
    )
    billable_ids = {r["id"] for r in rows if r["billable"]}
    assert set(served) == billable_ids
    assert len(served) == len(billable_ids)


def test_a_malformed_cursor_restarts_at_page_one(monkeypatch):
    """The documented contract of every cursor in this connector."""
    rows = _entries(3)
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    clean = handlers.list_time_entries({"limit": 25})
    junk = handlers.list_time_entries({"limit": 25, "cursor": "!!not-base64!!"})
    assert [r["id"] for r in junk["items"]] == [r["id"] for r in clean["items"]]


def test_list_expenses_paginates_too(monkeypatch):
    rows = [
        {"id": f"x{i}", "date": datetime(2026, 7, i + 1, tzinfo=UTC),
         "description": "Huissier", "category": "signification",
         "taxable": True, "invoiced": False, "amount": 5000,
         "dossier_id": "d1", "dossier_file_number": "2026-001",
         "dossier_title": "T"}
        for i in range(9)
    ]
    monkeypatch.setattr(handlers.expense_model, "list_expenses_page",
                        _paged_model(rows))
    served = _walk_tool(handlers.list_expenses, {"limit": 4})
    assert len(served) == 9 and len(set(served)) == 9


def test_a_row_without_a_key_yields_no_cursor_but_still_says_truncated(
    monkeypatch,
):
    """A legacy row missing date/id cannot mint a handle. An honest dead end
    beats a cursor that would mis-position the reader."""
    rows = _entries(4)
    # Rows come back newest-first, so rows[0] is the one page 1 returns and
    # therefore the one that would mint the cursor.
    rows[0]["id"] = ""
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        _paged_model(rows))
    payload = handlers.list_time_entries({"limit": 1})
    assert payload["truncated"] is True
    assert payload["next_cursor"] is None


def _hearings(n):
    return [
        {"id": f"h{i:02d}",
         "start_datetime": datetime(2026, 7, 1 + i, 14, 0, tzinfo=UTC),
         "end_datetime": datetime(2026, 7, 1 + i, 15, 0, tzinfo=UTC),
         "title": f"Audience {i}", "hearing_type": "audience",
         "status": "confirmée", "all_day": False, "dossier_id": "d1",
         "dossier_file_number": "2026-001", "dossier_title": "T",
         "location": "", "court": "", "judge": "", "modalite": "présentiel",
         "conference_uri": "", "notes": ""}
        for i in range(n)
    ]


def test_list_hearings_paginates_without_a_composite_index(monkeypatch):
    """`hearings` carries ZERO composite indexes, so the cursor raises the
    lower bound on start_datetime — a range filter on the very field the
    query orders by — and resolves the tie group in Python."""
    rows = _hearings(12)
    seen_bounds = []

    def _range(start, end, limit=200):
        seen_bounds.append(start)
        return [h for h in rows if start <= h["start_datetime"] <= end]

    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range", _range)
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    served = _walk_tool(
        handlers.list_hearings,
        {"limit": 5, "date_from": "2026-07-01", "date_to": "2026-07-20"},
    )
    assert len(served) == 12 and len(set(served)) == 12
    # The bound actually moved forward — otherwise every page re-reads all.
    assert seen_bounds[-1] > seen_bounds[0]


def test_list_hearings_pages_oldest_first(monkeypatch):
    """The one ascending cursor in the connector: an agenda reads forward."""
    rows = _hearings(4)
    monkeypatch.setattr(
        handlers.hearing_model, "list_hearings_in_range",
        lambda start, end, limit=200: [
            h for h in rows if start <= h["start_datetime"] <= end
        ],
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    first = handlers.list_hearings(
        {"limit": 2, "date_from": "2026-07-01", "date_to": "2026-07-20"})
    assert [h["id"] for h in first["items"]] == ["h00", "h01"]
    second = handlers.list_hearings(
        {"limit": 2, "date_from": "2026-07-01", "date_to": "2026-07-20",
         "cursor": first["next_cursor"]})
    assert [h["id"] for h in second["items"]] == ["h02", "h03"]


def test_hearings_sharing_one_instant_are_not_lost_at_a_page_boundary(
    monkeypatch,
):
    """Raising the lower bound alone would re-serve the whole tie group; the
    Python (start, id) comparison resolves the exact position inside it."""
    same = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    rows = [
        {**_hearings(1)[0], "id": f"tie{i}", "start_datetime": same}
        for i in range(4)
    ]
    monkeypatch.setattr(
        handlers.hearing_model, "list_hearings_in_range",
        lambda start, end, limit=200: [
            h for h in rows if start <= h["start_datetime"] <= end
        ],
    )
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    served = _walk_tool(
        handlers.list_hearings,
        {"limit": 2, "date_from": "2026-07-01", "date_to": "2026-07-20"},
    )
    assert sorted(served) == ["tie0", "tie1", "tie2", "tie3"]


def test_trust_cursor_only_on_the_exact_path(monkeypatch):
    """A bare account_id walks the whole register; every filtered shape
    returns next_cursor: null rather than a handle that would silently walk
    a bounded, oldest-first window."""
    monkeypatch.setattr(
        handlers.trust_model, "list_transactions_page",
        lambda account_id, cursor=None, limit=25: (
            [{"id": "t9", "sequence": 9, "amount": 100}], "more"),
    )
    exact = handlers.list_trust_transactions({"account_id": "a1", "limit": 1})
    assert exact["next_cursor"] == "more"
    assert exact["truncated"] is True

    monkeypatch.setattr(
        handlers.trust_model, "list_transactions",
        lambda **kw: [
            {"id": f"t{i}", "sequence": i, "amount": 100, "account_id": "a1"}
            for i in range(5)
        ],
    )
    filtered = handlers.list_trust_transactions(
        {"account_id": "a1", "status": "compensée", "limit": 2})
    assert filtered["next_cursor"] is None      # honest: no handle exists
    assert filtered["truncated"] is True
    # Still newest-first within the window it did read.
    assert [t["sequence"] for t in filtered["transactions"]] == [4, 3]


def test_every_paged_tool_declares_a_cursor_input():
    """The schema is the contract a client reads; a handler that honours a
    cursor the schema does not advertise is unreachable."""
    # DERIVED, never a hand-kept list: a frozen tuple is a test that stops
    # proving anything about the next tool added (lot 4 was missed exactly
    # that way). Every tool whose OUTPUT declares next_cursor must accept a
    # cursor on the way in — otherwise the paging handle is unreachable.
    from mcp.output_schemas import OUTPUT_SCHEMAS

    paged = [
        name for name, schema in OUTPUT_SCHEMAS.items()
        if "next_cursor" in (schema.get("properties") or {})
    ]
    assert len(paged) >= 6, paged           # guard against an empty sweep
    for name in paged:
        props = tools.TOOLS[name]["input_schema"]["properties"]
        assert "cursor" in props, f"{name} emits next_cursor but takes no cursor"
        assert props["cursor"]["type"] == "string", name


# ── Lot 4: the invoice register ─────────────────────────────────────────


def _inv(n, day, status="envoyée", **over):
    doc = {
        "id": f"inv{n:03d}", "invoice_number": f"2026-001-{n:02d}",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay c. Lavoie", "client_id": "p1",
        "client_name": "Jean Tremblay",
        "date": datetime(2026, 7, day, tzinfo=UTC),
        "due_date": datetime(2026, 8, day, tzinfo=UTC),
        "status": status,
        "subtotal_fees": 100000, "subtotal_expenses": 0, "subtotal": 100000,
        "gst_rate": 500, "gst_amount": 5000,
        "qst_rate": 9975, "qst_amount": 9975,
        "total": 114975, "retainer_applied": 0, "amount_due": 114975,
        "amount_paid": 0, "paid_date": None,
        "notes": "", "payment_terms": "Payable dans les 30 jours.",
    }
    doc.update(over)
    return doc


def _paged_invoices(rows):
    """A fake list_invoices_page honouring the model's REAL ordering:
    date DESC, id ASC — the mixed directions this lot had to respect."""
    def _page(status_filter=None, date_from=None, date_to=None,
              limit=200, cursor=None):
        sel = [r for r in rows if not status_filter
               or r.get("status") == status_filter]
        sel = sorted(sel, key=lambda r: r["id"])
        sel = sorted(sel, key=lambda r: r["date"], reverse=True)
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            md, mi = values[0], values[1]
            sel = [r for r in sel
                   if r["date"] < md or (r["date"] == md and r["id"] > mi)]
        window = sel[:limit]
        nxt = encode_cursor([window[-1]["date"], window[-1]["id"]]) \
            if len(sel) > limit else None
        return window, nxt
    return _page


def test_list_invoices_pages_newest_first_without_loss(monkeypatch):
    rows = [_inv(i, i + 1) for i in range(12)]
    monkeypatch.setattr(handlers.invoice_model, "list_invoices_page",
                        _paged_invoices(rows))
    served = _walk_tool(handlers.list_invoices, {"limit": 5})
    assert len(served) == 12 and len(set(served)) == 12
    first = handlers.list_invoices({"limit": 3})
    assert [r["date"] for r in first["items"]] == [
        "2026-07-12", "2026-07-11", "2026-07-10"]


def test_invoices_sharing_a_date_survive_a_page_boundary(monkeypatch):
    """Month-end: several invoices on one date. The server orders
    date DESC, id ASC — MIXED directions — so a uniform-descending keyset
    would skip or repeat inside the tie group."""
    rows = [_inv(i, 15) for i in range(5)]          # all the same date
    monkeypatch.setattr(handlers.invoice_model, "list_invoices_page",
                        _paged_invoices(rows))
    served = _walk_tool(handlers.list_invoices, {"limit": 2})
    assert served == ["inv000", "inv001", "inv002", "inv003", "inv004"]


def test_dossier_scoped_branch_pages_in_python(monkeypatch):
    """With a dossier_id there is no composite index for date ordering, so
    the model's single-equality read is paged here — and must mint the SAME
    cursor shape as the server branch."""
    rows = [_inv(i, i + 1) for i in range(7)]

    def _must_not_run(**kw):
        raise AssertionError("dossier branch must not hit list_invoices_page")

    monkeypatch.setattr(handlers.invoice_model, "list_invoices_page", _must_not_run)
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: list(rows))
    served = _walk_tool(handlers.list_invoices, {"dossier_id": "d1", "limit": 3})
    assert len(served) == 7 and len(set(served)) == 7


def test_status_group_impayee_runs_two_queries_never_an_in_filter(monkeypatch):
    """A Firestore `in` + order_by + start_after raises FAILED_PRECONDITION,
    which the model swallows into [] — a silently blank billing statement.
    Two small queries cannot fail that way."""
    calls = []
    rows = [_inv(0, 1, "envoyée"), _inv(1, 2, "en_retard"),
            _inv(2, 3, "payée"), _inv(3, 4, "brouillon")]

    def _page(status_filter=None, **kw):
        calls.append(status_filter)
        sel = [r for r in rows if r["status"] == status_filter]
        return sel, None

    monkeypatch.setattr(handlers.invoice_model, "list_invoices_page", _page)
    payload = handlers.list_invoices({"status_group": "impayée"})
    assert sorted(calls) == ["en_retard", "envoyée"]      # two, never one `in`
    assert {r["status"] for r in payload["items"]} == {"envoyée", "en_retard"}


def test_status_and_status_group_are_mutually_exclusive(monkeypatch):
    with pytest.raises(tools.ToolArgumentError, match="mutually exclusive"):
        handlers.list_invoices({"status": "payée", "status_group": "impayée"})


def test_the_unpaid_pair_matches_the_billing_snapshot_definition():
    """The mandate's reconciliation criterion, enforced structurally: the
    register and get_outstanding_total must sum the SAME statuses, or the
    two surfaces disagree about money."""
    import inspect
    from models import invoice as imod

    source = inspect.getsource(imod.get_outstanding_total)
    assert '"envoyée", "en_retard"' in source
    assert handlers._INVOICE_UNPAID == ("envoyée", "en_retard")


def test_outstanding_reconciles_to_the_cent(monkeypatch):
    """Σ amount_due over the impayée group == get_billing_snapshot's
    outstanding_cents, by construction."""
    rows = [_inv(0, 1, "envoyée", amount_due=114975),
            _inv(1, 2, "en_retard", amount_due=200000),
            _inv(2, 3, "payée", amount_due=50000)]      # excluded both sides
    monkeypatch.setattr(
        handlers.invoice_model, "list_invoices_page",
        lambda status_filter=None, **kw: (
            [r for r in rows if r["status"] == status_filter], None),
    )
    register = handlers.list_invoices({"status_group": "impayée", "limit": 50})
    total = sum(r["amount_due_cents"] for r in register["items"])

    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 114975 + 200000)
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: list(rows))
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 0.0, "amount": 0})
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda **kw: {"amount": 0})
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda **kw: ([], None))
    snapshot = handlers.get_billing_snapshot({})
    assert total == snapshot["outstanding_cents"] == 314975


def test_payment_basis_never_reads_silence_as_payment(monkeypatch):
    """A balance equal to the total means « nothing RECORDED », not
    « nothing paid » — the whole reason the field exists."""
    unrecorded = _inv(0, 1, "payée", amount_paid=0)     # legacy: status only
    recorded = _inv(1, 2, "envoyée", amount_paid=50000)
    monkeypatch.setattr(
        handlers.invoice_model, "list_invoices_page",
        lambda **kw: ([unrecorded, recorded], None))
    items = {r["id"]: r for r in handlers.list_invoices({})["items"]}
    assert items["inv000"]["payment_basis"] == "none"
    assert items["inv000"]["balance_cents"] == 114975   # NOT 0
    assert items["inv001"]["payment_basis"] == "recorded"
    assert items["inv001"]["balance_cents"] == 114975 - 50000


def test_amount_due_stays_the_frozen_issuance_figure(monkeypatch):
    """amount_due is never updated. Emitting it beside `balance` is the
    point: one is provenance, the other is what is owed."""
    paid = _inv(0, 1, "payée", amount_paid=114975)
    monkeypatch.setattr(handlers.invoice_model, "list_invoices_page",
                        lambda **kw: ([paid], None))
    row = handlers.list_invoices({})["items"][0]
    assert row["amount_due_cents"] == 114975       # frozen
    assert row["balance_cents"] == 0               # the truth


def test_get_invoice_returns_lines_and_checks_the_subtotal(monkeypatch):
    items = [
        {"id": "l1", "type": "fee", "source_id": "t1",
         "date": datetime(2026, 7, 2, tzinfo=UTC),
         "description": "Rédaction de la mise en demeure", "hours": 2.0,
         "rate": 30000, "amount": 60000, "taxable": True},
        {"id": "l2", "type": "expense", "source_id": "x1",
         "date": datetime(2026, 7, 3, tzinfo=UTC),
         "description": "Frais d'huissier", "hours": None,
         "rate": None, "amount": 40000, "taxable": True},
    ]
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (_inv(0, 1), items))
    payload = handlers.get_invoice({"invoice_id": "inv000"})
    assert payload["found"] is True
    inv = payload["invoice"]
    assert inv["subtotal_matches_line_items"] is True
    assert inv["line_items_total_cents"] == 100000
    assert inv["warnings"] == []
    # Descriptions verbatim — they printed on the client's invoice.
    assert inv["line_items"][0]["description"] == "Rédaction de la mise en demeure"
    # A disbursement has no hourly rate: explicit null, never 0.
    assert inv["line_items"][1]["rate_cents"] is None
    assert inv["line_items"][1]["hours"] is None
    assert inv["gst_rate_display"].startswith("5")


def test_get_invoice_flags_a_subtotal_that_does_not_add_up(monkeypatch):
    items = [{"id": "l1", "type": "fee", "source_id": "", "date": None,
              "description": "X", "hours": None, "rate": None,
              "amount": 999, "taxable": True}]
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (_inv(0, 1), items))
    inv = handlers.get_invoice({"invoice_id": "inv000"})["invoice"]
    assert inv["subtotal_matches_line_items"] is False


def test_empty_lines_on_a_non_zero_invoice_is_reported_as_a_read_failure(
    monkeypatch,
):
    """get_invoice_with_items swallows a subcollection failure into [], and
    create_invoice refuses an invoice with no line — so this is ALWAYS a
    failed read, never data. Rendering a plausible empty invoice would be
    the worst outcome."""
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (_inv(0, 1), []))
    inv = handlers.get_invoice({"invoice_id": "inv000"})["invoice"]
    assert inv["warnings"] and "n'ont pas pu être lus" in inv["warnings"][0]


def test_get_invoice_unknown_id_is_found_false(monkeypatch):
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (None, []))
    payload = handlers.get_invoice({"invoice_id": "nope"})
    assert payload == {"found": False, "invoice_id": "nope"}


def test_invoice_status_enum_tracks_the_model():
    """tools.py literals are hand-copied (importing models at load builds a
    Firestore client) — pin them so they cannot drift."""
    from models import invoice as imod

    assert (tools.TOOLS["list_invoices"]["input_schema"]["properties"]
            ["status"]["enum"] == list(imod.VALID_STATUSES))


def test_the_two_outstanding_definitions_deliberately_disagree():
    """get_dossier.summaries.invoices.total_outstanding sums `total`,
    counts brouillons and treats payée as settled; the firm-wide figure sums
    `amount_due` over envoyée+en_retard. They will NOT reconcile.

    Pinned ON PURPOSE: « fixing » get_invoice_summary would change a money
    value the 07:00 briefing already reads. Both definitions are stated in
    the tool descriptions instead."""
    import inspect
    from models import invoice as imod

    summary_src = inspect.getsource(imod.get_invoice_summary)
    assert 'inv.get("total", 0)' in summary_src        # sums total…
    assert "amount_due" not in summary_src             # …never amount_due
    assert '"annulée"' in summary_src                  # only annulée excluded
    description = tools.TOOLS["list_invoices"]["description"]
    assert "will not agree" in description             # said out loud


# ── Lot 1: cabinet-wide search ──────────────────────────────────────────
#
# The defect: `list_notes(query="Olivares")` with no dossier_id searched ONLY
# the « Général » notes and returned nothing, while the note sat in a
# dossier. The caller read that as « no such note exists » — the worst
# possible answer from the firm's memory.


def _note(nid, *, dossier_id="", title="Note", content="", pinned=False,
          created_day=1, **over):
    doc = {
        "id": nid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Tremblay" if dossier_id else "",
        "title": title, "content": content, "category": "recherche",
        "pinned": pinned, "is_analyse": False,
        "created_at": datetime(2026, 7, created_day, tzinfo=UTC),
        "updated_at": datetime(2026, 7, created_day, tzinfo=UTC),
    }
    doc.update(over)
    return doc


@pytest.fixture()
def notes_world(monkeypatch):
    """Every note in the firm — the model streams the whole collection on
    the no-dossier_id path, which is what makes cabinet scope free."""
    corpus = [
        _note("n-gen", title="Veille", created_day=1),
        _note("n-oliv", dossier_id="d27", created_day=2,
              title="Recherche — Éléments constitutifs de la faute de Solo "
                    "inc. et d'Olivares"),
        _note("n-other", dossier_id="d9", created_day=3, title="Appel"),
    ]

    def _list(dossier_id=None, category=None, search=None,
              include_analyse=False, **kw):
        rows = list(corpus)
        if dossier_id:
            rows = [n for n in rows if n["dossier_id"] == dossier_id]
        if search:
            q = search.lower()
            rows = [n for n in rows
                    if q in n["title"].lower() or q in n["content"].lower()]
        return rows

    monkeypatch.setattr(handlers.note_model, "list_notes", _list)
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    return corpus


def test_the_olivares_criterion(notes_world):
    """The mandate's acceptance test, verbatim: the note must come back."""
    payload = handlers.list_notes({"query": "Olivares", "scope": "cabinet"})
    assert [n["id"] for n in payload["items"]] == ["n-oliv"]
    assert payload["scope"] == "cabinet"
    # And it is attributable without a second call — the row gap the mandate
    # flagged as independent of scope.
    assert payload["items"][0]["dossier_id"] == "d27"
    assert payload["items"][0]["dossier_file_number"] == "2026-001"


def test_the_default_call_is_unchanged(notes_world):
    """The two scheduled jobs read this. Same corpus, same order, same
    paging key — only the additive keys are new."""
    payload = handlers.list_notes({})
    assert [n["id"] for n in payload["items"]] == ["n-gen"]   # Général only
    assert payload["scope"] == "general"
    assert payload["next_cursor"] is None       # general pages with offset
    assert payload["truncated"] is False


def test_a_dossier_id_still_means_that_dossier(notes_world):
    payload = handlers.list_notes({"dossier_id": "d27"})
    assert [n["id"] for n in payload["items"]] == ["n-oliv"]
    assert payload["scope"] == "dossier"        # implicit, no scope passed


def test_cabinet_returns_every_note_including_general(notes_world):
    payload = handlers.list_notes({"scope": "cabinet"})
    assert {n["id"] for n in payload["items"]} == {"n-gen", "n-oliv", "n-other"}


def test_every_contradiction_is_refused_never_resolved(notes_world):
    """Silently preferring one of two contradictory arguments is how a
    caller ends up believing it searched the firm."""
    cases = [
        ({"scope": "cabinet", "dossier_id": "d1"}, "contradicts"),
        ({"scope": "general", "dossier_id": "d1"}, "contradicts"),
        ({"scope": "dossier"}, "is required to search one dossier"),
        ({"scope": "bogus"}, "must be one of"),
        ({"scope": "cabinet", "cursor": "x", "offset": 5}, "never both"),
        ({"cursor": "x"}, "only available"),
        ({"dossier_status": "actif"}, "only applies"),
    ]
    for args, fragment in cases:
        with pytest.raises(tools.ToolArgumentError, match=fragment):
            handlers.list_notes(args)


def test_cabinet_paging_walks_everything_once(monkeypatch):
    corpus = [_note(f"n{i:02d}", dossier_id="d1", created_day=(i % 28) + 1)
              for i in range(14)]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    served, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 30
        args = {"scope": "cabinet", "limit": 5}
        if cursor:
            args["cursor"] = cursor
        payload = handlers.list_notes(args)
        served.extend(n["id"] for n in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            break
    assert len(served) == 14 and len(set(served)) == 14


def test_pinning_a_note_between_pages_cannot_shift_the_page(monkeypatch):
    """Cabinet orders on (created_at, id) — IMMUTABLE fields. The model's own
    order puts pinned notes first, and `pinned` is a one-click toggle: a
    mutable component in a cursor key moves rows across the boundary."""
    corpus = [_note(f"n{i:02d}", dossier_id="d1", created_day=i + 1)
              for i in range(6)]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    first = handlers.list_notes({"scope": "cabinet", "limit": 3})
    corpus[-1]["pinned"] = True                  # toggled between pages
    second = handlers.list_notes(
        {"scope": "cabinet", "limit": 3, "cursor": first["next_cursor"]})
    served = [n["id"] for n in first["items"]] + [n["id"] for n in second["items"]]
    assert len(served) == 6 and len(set(served)) == 6


def test_dossier_status_costs_one_query_never_one_per_row(monkeypatch):
    corpus = [_note(f"n{i}", dossier_id=f"d{i}", created_day=i + 1)
              for i in range(5)]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    calls = []

    def _all(status_filter=None, **kw):
        calls.append(status_filter)
        return [{"id": "d1"}, {"id": "d3"}]

    def _paged_must_not_run(**kw):
        raise AssertionError(
            "the dossier universe must be read COMPLETE, not one page deep: "
            "a discarded next_cursor drops every row past the 200th dossier"
        )

    monkeypatch.setattr(handlers.dossier_model, "list_dossiers", _all)
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        _paged_must_not_run)
    payload = handlers.list_notes({"scope": "cabinet", "dossier_status": "actif"})
    assert calls == ["actif"]                    # exactly one, not one per row
    assert {n["id"] for n in payload["items"]} == {"n1", "n3"}
    assert payload["dossier_status_matched"] == 2


def test_the_analyse_note_stays_visible_on_every_scope(monkeypatch):
    """include_analyse=True is a per-CALLER decision; a scope branch that
    dropped it would make the « Théorie de la cause » vanish silently."""
    seen = []

    def _list(dossier_id=None, include_analyse=False, **kw):
        seen.append(include_analyse)
        return []

    monkeypatch.setattr(handlers.note_model, "list_notes", _list)
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    handlers.list_notes({})
    handlers.list_notes({"dossier_id": "d1"})
    handlers.list_notes({"scope": "cabinet"})
    assert seen == [True, True, True]


# ── list_documents: the relaxed `required`, re-imposed in the handler ────


def _doc(did, dossier_id="d1", **over):
    doc = {
        "id": did, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001", "dossier_title": "Tremblay",
        "display_name": "Jugement.pdf", "category": "jugement",
        "file_type": "application/pdf", "file_size": 1024, "version": 1,
        "folder_id": None, "document_date": None, "description": "",
        "tags": [], "created_at": datetime(2026, 7, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
    }
    doc.update(over)
    return doc


def test_documents_still_demand_a_dossier_id_outside_cabinet(monkeypatch):
    """The schema no longer marks it required (cabinet has no dossier), so
    the HANDLER must — otherwise an omitted dossier_id turns a loud error
    into a silent firm-wide scan."""
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: [])
    with pytest.raises(tools.ToolArgumentError, match="required"):
        handlers.list_documents({})
    # The schema really did relax — otherwise the handler guard is dead code.
    assert "required" not in tools.TOOLS["list_documents"]["input_schema"]


def test_documents_cabinet_scope_spans_dossiers(monkeypatch):
    corpus = [_doc("x1", "d1"), _doc("x2", "d2")]
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    payload = handlers.list_documents({"scope": "cabinet"})
    assert {d["id"] for d in payload["items"]} == {"x1", "x2"}
    assert payload["scope"] == "cabinet"
    # folder_path is "" firm-wide: resolving breadcrumbs would be one query
    # per dossier. The key stays present — the contract requires it.
    assert all(d["folder_path"] == "" for d in payload["items"])
    assert payload["items"][0]["dossier_id"] in {"d1", "d2"}


def test_documents_refuse_a_folder_across_the_firm(monkeypatch):
    with pytest.raises(tools.ToolArgumentError, match="no meaning across"):
        handlers.list_documents({"scope": "cabinet", "folder_id": "f1"})


def test_document_query_is_metadata_only_and_says_so():
    """Reserving the meaning of `query` now is what keeps a future
    content-search tool from having to contradict it."""
    description = tools.TOOLS["list_documents"]["description"]
    assert "METADATA" in description
    assert "NEVER the text inside the file" in description


def test_offset_is_refused_in_cabinet_scope_not_silently_dropped():
    """Cabinet pages by cursor. Accepting an `offset`, validating it, then
    dropping it served page 1 forever — a caller keeping its offset habit
    would walk the firm, see the same rows, and conclude the corpus is that
    small. That is the very failure this lot exists to remove, reproduced by
    its own new path. Found by the lot-1 review."""
    for tool in (handlers.list_notes, handlers.list_documents):
        with pytest.raises(tools.ToolArgumentError, match="does not page"):
            tool({"scope": "cabinet", "offset": 3})
        # offset: 0 is a no-op, not an error — same truthiness rule the
        # cursor+offset guard already uses.
        tool({"scope": "cabinet", "offset": 0})


def test_an_undated_row_does_not_strand_the_tail(monkeypatch):
    """A row with no created_at keys to the sort floor. Refusing to mint a
    cursor from it left truncated: true with next_cursor: null — the rest of
    the corpus unreachable. Found by the lot-1 review."""
    corpus = [_note(f"u{i:02d}", dossier_id="d1") for i in range(8)]
    for n in corpus:
        n["created_at"] = None
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    served, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 20
        args = {"scope": "cabinet", "limit": 3}
        if cursor:
            args["cursor"] = cursor
        payload = handlers.list_notes(args)
        served.extend(n["id"] for n in payload["items"])
        cursor = payload["next_cursor"]
        if not cursor:
            assert payload["truncated"] is False, (
                "truncated with no cursor = a tail nobody can reach")
            break
    assert len(served) == 8 and len(set(served)) == 8


def test_an_empty_dossier_filter_is_explained_not_asserted(monkeypatch):
    """The dossier index fails open to []. Without the count, a zero-row
    answer built from a READ ERROR would read as « the firm holds no such
    note » — the exact wrong answer this lot removes elsewhere."""
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: [_note("n1", dossier_id="d1")])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers", lambda **kw: [])
    payload = handlers.list_notes({"scope": "cabinet", "dossier_status": "actif"})
    assert payload["items"] == []
    assert payload["dossier_status_matched"] == 0     # says WHY it is empty
    # And null when no filter was asked for — the two cases stay distinct.
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers",
                        lambda **kw: [{"id": "d1"}])
    assert handlers.list_notes({"scope": "cabinet"})["dossier_status_matched"] is None


def test_dossier_status_enum_tracks_the_model():
    """Hand-copied literal (importing models at tools.py load builds a
    Firestore client). This lot added two consumers, and the model DROPS an
    out-of-vocabulary status silently — drift would widen a search without
    a word."""
    from models import dossier as dmod

    assert tools._DOSSIER_STATUSES == list(dmod.VALID_STATUSES)


def test_tool_titles_do_not_claim_a_single_dossier():
    """The titles are mirrored into annotations.title, which 2025-03-26
    clients display — « Notes d'un dossier » on a firm-wide search misleads
    before the description is ever read."""
    for name in ("list_notes", "list_documents"):
        assert "d'un dossier" not in tools.TOOLS[name]["title"], name


# ── Lot 5: get_coverage_report — the sweep and its two guards ────────────


def _cov_dossier(did="d1", **over):
    doc = {
        "id": did, "file_number": f"2026-{did[-1]}01", "title": "T",
        "status": "actif", "forum_type": "judiciaire",
        "tribunal": "Cour supérieure",
        "court_file_number": "500-05-123456-241",
        "action": "REC-01", "valeur": 100000,
        "opposing_parties": [], "significations": [], "client_ids": [],
        # A dated, running limitation period — otherwise derive_prescription
        # legitimately reports « a_verifier » and the fixture is not clean.
        "prescription_type": "3_ans",
        "prescription_date": datetime(2030, 1, 1, tzinfo=UTC),
        "prescription_events": [], "prise_action_date": None,
    }
    doc.update(over)
    return doc


@pytest.fixture()
def cov(monkeypatch):
    """Only the reads get_coverage_report performs."""
    state = {
        "dossiers": [_cov_dossier()],
        "protocols": [{"dossier_id": "d1", "protocol_type": "cs_ordinaire",
                       "status": "actif"}],
        "parties": {},
        "tasks": [],
    }
    monkeypatch.setattr(
        handlers.dossier_model, "list_dossiers",
        lambda status_filter=None, **kw: [
            d for d in state["dossiers"]
            if not status_filter or d.get("status") == status_filter
        ],
    )
    monkeypatch.setattr(handlers.protocol_model, "list_protocols",
                        lambda **kw: list(state["protocols"]))
    monkeypatch.setattr(handlers.protocol_model, "regime_mismatch",
                        lambda ptype, d: False)
    monkeypatch.setattr(handlers.partie_model, "get_parties_bulk",
                        lambda ids: dict(state["parties"]))
    monkeypatch.setattr(handlers.task_model, "list_tasks_by_status",
                        lambda st, **kw: [t for t in state["tasks"]
                                          if t.get("status") == st])
    return state


def test_a_clean_firm_reports_nothing(cov):
    payload = handlers.get_coverage_report({})
    assert payload["items"] == []
    assert payload["summary"]["manquements"] == 0
    assert payload["scope"]["dossiers_examined"] == 1
    assert payload["data_completeness"]["protocol_index_complete"] is True


def test_the_2026_007_case_a_linked_instance_without_a_protocol(cov):
    cov["dossiers"] = [_cov_dossier("d7")]
    cov["protocols"] = [{"dossier_id": "dX", "protocol_type": "cs_ordinaire",
                         "status": "actif"}]
    payload = handlers.get_coverage_report({})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert "PROTO_ABSENT" in codes
    assert payload["summary"]["manquements"] >= 1


def test_the_2026_027_case_two_defendants_and_an_empty_register(cov):
    cov["dossiers"] = [_cov_dossier(
        "d2", opposing_parties=[{"id": "a1"}, {"id": "a2"}], significations=[])]
    payload = handlers.get_coverage_report({})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert "SIGN_ABSENTE" in codes


def test_a_failed_protocol_read_suppresses_the_protocol_checks(cov):
    """list_protocols swallows a read failure into []. Unguarded,
    PROTO_ABSENT would fire on EVERY dossier — a false-manquement storm on a
    compliance report, worse than reporting nothing."""
    cov["dossiers"] = [_cov_dossier("d1"), _cov_dossier("d2")]
    cov["protocols"] = []                    # indistinguishable from failure
    payload = handlers.get_coverage_report({})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert "PROTO_ABSENT" not in codes
    assert "PROTO_REGIME" not in codes
    assert set(payload["scope"]["checks_skipped"]) >= {"PROTO_ABSENT", "PROTO_REGIME"}
    assert payload["data_completeness"]["protocol_index_complete"] is False


def test_a_failed_contact_read_never_accuses_a_client(cov):
    """Reporting a client as unverified because a read failed is a
    regulatory accusation built on an error."""
    cov["dossiers"] = [_cov_dossier("d1", client_ids=["p1"])]
    cov["parties"] = {}                      # read failed
    payload = handlers.get_coverage_report({})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert not ({"CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE",
                 "CLIENT_INTROUVABLE"} & codes)
    assert payload["data_completeness"]["kyc_checked"] is False
    assert payload["data_completeness"]["kyc_reason"]


def test_the_deontological_checks_fire_when_the_contacts_read(cov):
    cov["dossiers"] = [_cov_dossier("d1", client_ids=["p1"])]
    cov["parties"] = {"p1": {"identity_verified": "non_vérifié",
                             "conflict_check": "non_vérifié"}}
    payload = handlers.get_coverage_report({})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert {"CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE"} <= codes
    assert payload["data_completeness"]["kyc_checked"] is True


def test_the_ghost_task_on_a_closed_dossier_surfaces_cross_scope(cov):
    """Under the default « actif » filter these could never appear — yet the
    ghost task on the closed 2026-012 is one of the audit's own examples."""
    cov["dossiers"] = [_cov_dossier("d1"),
                       _cov_dossier("d12", status="fermé")]
    cov["tasks"] = [{"id": "t1", "status": "à_faire", "dossier_id": "d12"}]
    payload = handlers.get_coverage_report({})
    # The closed dossier is NOT in items (the scope filter holds)…
    assert all(i["dossier_id"] != "d12" for i in payload["items"])
    # …but its ghost task is reported.
    cross = {f["code"]: f for f in payload["cross_scope_findings"]}
    assert "TACHE_OUVERTE_DOSSIER_FERME" in cross
    assert cross["TACHE_OUVERTE_DOSSIER_FERME"]["dossier_id"] == "d12"


def test_no_false_positive_on_a_closed_dossier_under_the_active_filter(cov):
    cov["dossiers"] = [_cov_dossier("d12", status="fermé",
                                    court_file_number="", valeur=None)]
    payload = handlers.get_coverage_report({})
    assert payload["items"] == []
    assert payload["scope"]["dossiers_examined"] == 0


def test_checks_narrowing_is_declared_not_silent(cov):
    cov["dossiers"] = [_cov_dossier("d1", valeur=None, court_file_number="")]
    payload = handlers.get_coverage_report({"checks": ["VALEUR_ABSENTE"]})
    codes = {f["code"] for i in payload["items"] for f in i["findings"]}
    assert codes == {"VALEUR_ABSENTE"}
    assert "NO_COUR_ABSENT" in payload["scope"]["checks_skipped"]
    assert payload["scope"]["checks_run"] == ["VALEUR_ABSENTE"]


def test_the_report_never_promises_the_connector_will_fix_it(cov):
    """The report creates a call to action the connector must not answer."""
    cov["dossiers"] = [_cov_dossier("d1", valeur=None, court_file_number="")]
    payload = handlers.get_coverage_report({})
    details = [f["detail"] for i in payload["items"] for f in i["findings"]]
    assert details
    description = tools.TOOLS["get_coverage_report"]["description"]
    assert "cannot create a protocol" in description


def test_the_codes_enum_is_derived_from_the_running_checks():
    """Not a hand-copied literal: mcp.coverage imports no model, so the enum
    is the registry itself and cannot drift from what actually runs."""
    from mcp import coverage as cov_mod

    enum = tools.TOOLS["get_coverage_report"]["input_schema"]["properties"]["checks"]["items"]["enum"]
    assert enum == list(cov_mod.ALL_CODES)


# ── Lot 3: complete_task — the only status change in the connector ───────


def _ct_task(status="à_faire", **over):
    doc = {
        "id": "t1", "title": "Produire la réponse", "description": "Corps",
        "status": status, "priority": "normale", "category": "rédaction",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay", "due_date": None, "completed_date": None,
        "related_note_id": None,
    }
    doc.update(over)
    return doc


@pytest.fixture()
def ct(monkeypatch, bumps):
    """complete_task's world: the model, the dossier, and no protocol."""
    state = {"task": _ct_task(), "updated": None, "protocol": None}

    def _update(task_id, data):
        state["updated"] = dict(data)
        return {**state["task"], **data}, []

    monkeypatch.setattr(handlers.task_model, "get_task",
                        lambda i: dict(state["task"]) if state["task"] else None)
    monkeypatch.setattr(handlers.task_model, "update_task", _update)
    monkeypatch.setattr(handlers.task_model, "_validate", lambda d: [])
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "status": "actif",
                                   "file_number": "2026-001", "title": "T"})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda did, active_only=True: state["protocol"])
    monkeypatch.setattr(handlers.protocol_model, "get_protocol",
                        lambda pid: state["protocol"])

    def _forbidden(*a, **k):
        raise AssertionError(
            "toggle_task_complete is a FOUR-state toggle: it sends annulee "
            "back to a_faire, silently un-cancelling a cancelled task"
        )

    monkeypatch.setattr(handlers.task_model, "toggle_task_complete", _forbidden)
    return state


def test_complete_task_closes_and_bumps_once(ct, bumps):
    payload = handlers.complete_task({"task_id": "t1"})
    assert payload["completed"] is True
    assert ct["updated"] == {"status": "terminée"}
    assert payload["entity"]["status"] == "terminée"
    assert payload["entity"]["previous_status"] == "à_faire"
    assert bumps["bump"] == ["dossier:d1"]
    # A status change is NOT a creation: no tombstone work on a resource
    # that was never deleted.
    assert bumps["tombstone"] == []
    assert payload["already_completed"] is False


def test_toggle_task_complete_is_never_called(ct):
    """It flips annulée AND terminée back to à_faire — calling it would
    silently un-cancel a cancelled task."""
    handlers.complete_task({"task_id": "t1", "status": "annulée"})
    assert ct["updated"] == {"status": "annulée"}


def test_a_task_already_in_that_state_writes_nothing(ct, bumps):
    """What makes a scheduled job replayable: no model call, no cascade,
    no CTag."""
    ct["task"] = _ct_task("terminée")
    payload = handlers.complete_task({"task_id": "t1"})
    assert payload["already_completed"] is True
    assert ct["updated"] is None            # update_task never reached
    assert bumps["bump"] == []
    assert any("déjà le statut" in w for w in payload["warnings"])


def test_the_other_terminal_state_is_refused_not_rewritten(ct):
    """Silently converting a cancellation into a completion rewrites what
    the lawyer decided."""
    ct["task"] = _ct_task("annulée")
    with pytest.raises(tools.ToolArgumentError, match="annulée"):
        handlers.complete_task({"task_id": "t1", "status": "terminée"})
    assert ct["updated"] is None


def test_a_faire_is_refused(ct):
    """Reopening clears completed_date and DE-completes the linked step —
    an edit shaped like a destruction. It stays in the application."""
    with pytest.raises(tools.ToolArgumentError, match="status"):
        handlers.complete_task({"task_id": "t1", "status": "à_faire"})
    assert ct["updated"] is None


def test_an_unknown_task_is_refused_explicitly(ct):
    ct["task"] = None
    with pytest.raises(tools.ToolArgumentError, match="introuvable"):
        handlers.complete_task({"task_id": "nope"})


def test_dry_run_writes_nothing_but_shows_the_cascade(ct, bumps):
    ct["protocol"] = {
        "id": "p1", "status": "actif",
        "steps": [{"id": "s1", "title": "Réponse", "status": "à_venir",
                   "linked_task_id": "t1"}],
    }
    payload = handlers.complete_task({"task_id": "t1", "dry_run": True})
    assert ct["updated"] is None
    assert bumps["bump"] == []
    effect = payload["protocol_step_effect"]
    assert effect["linked_step_found"] is True
    assert effect["step_id"] == "s1"
    assert "protocole entier" in effect["note"]


def test_the_cascade_is_verified_not_predicted(ct, bumps):
    """_sync_protocol_step swallows every exception, so a PREDICTED
    « complété » could be a lie. The step is re-read after the write."""
    ct["protocol"] = {
        "id": "p1", "status": "actif",
        "steps": [{"id": "s1", "title": "Réponse", "status": "à_venir",
                   "linked_task_id": "t1"}],
    }
    payload = handlers.complete_task({"task_id": "t1"})
    effect = payload["protocol_step_effect"]
    assert effect["step_status_before"] == "à_venir"
    # The fake protocol never changed, so the handler must report that —
    # not the completion it hoped for.
    assert effect["step_status_after"] == "à_venir"
    assert "changé d'état" in effect["note"]


def test_closing_the_whole_protocol_is_warned_about_by_name(ct):
    """The cascade the lawyer accepted: list_urgent_steps keeps only actif
    protocols, so a closure silently empties the dossier's deadline feed."""
    ct["protocol"] = {
        "id": "p1", "status": "complété",
        "steps": [{"id": "s1", "title": "Réponse", "status": "complété",
                   "linked_task_id": "t1"}],
    }
    payload = handlers.complete_task({"task_id": "t1"})
    assert payload["protocol_step_effect"]["protocol_closed"] is True
    assert any("PROTOCOLE ENTIER" in w for w in payload["warnings"])


def test_a_general_task_never_looks_for_a_protocol(ct, bumps):
    ct["task"] = _ct_task(dossier_id="")
    payload = handlers.complete_task({"task_id": "t1"})
    effect = payload["protocol_step_effect"]
    assert effect["checked"] is False       # no lookup ran at all
    assert effect["linked_step_found"] is False
    assert bumps["bump"] == ["general"]


def test_a_completion_note_is_stamped_and_appended(ct):
    handlers.complete_task(
        {"task_id": "t1", "completion_note": "Déposée au greffe."})
    desc = ct["updated"]["description"]
    assert desc.startswith("Corps")          # the lawyer's text survives
    assert "Complétée par Claude le" in desc
    assert "Déposée au greffe." in desc


def test_a_note_that_would_truncate_is_refused(ct):
    """_sanitize_data cuts at 2000 with no exception and no flag — a silent
    cut would land on the lawyer's OWN earlier text, not on ours."""
    with pytest.raises(tools.ToolArgumentError, match="2000"):
        handlers.complete_task({"task_id": "t1", "completion_note": "x" * 2100})
    assert ct["updated"] is None


def test_dry_run_never_announces_a_success_the_write_would_refuse(ct, monkeypatch):
    """update_task re-validates the WHOLE merged document: a legacy task
    with an out-of-vocabulary category fails for a reason invisible in the
    application. The dry run must surface it, not promise success."""
    monkeypatch.setattr(handlers.task_model, "_validate",
                        lambda d: ["Catégorie invalide."])
    with pytest.raises(tools.ToolArgumentError, match="Catégorie invalide"):
        handlers.complete_task({"task_id": "t1", "dry_run": True})


def test_a_ctag_failure_still_reports_the_write_as_committed(ct, monkeypatch):
    """The task is ALREADY written; letting the bump raise would report a
    committed write as a failure and invite a retry."""
    def _boom(_name):
        raise RuntimeError("boom")

    monkeypatch.setattr(handlers, "bump_ctag", _boom)
    payload = handlers.complete_task({"task_id": "t1"})
    assert payload["completed"] is True
    assert payload["ctag_bumped"] is False
    assert any("Ne pas réessayer" in w for w in payload["warnings"])


def test_complete_task_is_the_only_idempotent_write():
    """The hint is what a client uses to decide whether a retry is safe:
    every creator appends again, complete_task writes nothing."""
    descriptors = {d["name"]: d for d in tools.list_tool_descriptors()}
    for name in tools.WRITE_TOOLS:
        expected = name == "complete_task"
        assert descriptors[name]["annotations"]["idempotentHint"] is expected, name


def test_task_row_prorogue_une_echeance_de_fin_de_semaine():
    """Décision 2026-08-02 (D-B) : due samedi → agissable lundi → en retard
    mardi, sur la surface MCP comme sur le web (le briefing de 7 h lit ceci)."""
    saturday = {"id": "t", "status": "à_faire",
                "due_date": datetime(2026, 7, 18, tzinfo=UTC)}  # samedi
    assert handlers._task_row(saturday, today=date(2026, 7, 19))["is_overdue"] is False
    assert handlers._task_row(saturday, today=date(2026, 7, 20))["is_overdue"] is False  # lundi agissable
    assert handlers._task_row(saturday, today=date(2026, 7, 21))["is_overdue"] is True


def test_derive_step_status_prorogue_une_echeance_de_fin_de_semaine():
    """Même règle sur les étapes : le statut dérivé ne bascule à en_retard
    qu'après le jour juridique suivant l'échéance non juridique."""
    saturday = datetime(2026, 7, 18, tzinfo=UTC)
    assert handlers.derive_step_status(
        "à_venir", saturday, today=date(2026, 7, 20)) != "en_retard"
    assert handlers.derive_step_status(
        "à_venir", saturday, today=date(2026, 7, 21)) == "en_retard"


# ════════════════════════════════════════════════════════════════════════
# Phase O — phase/sous_phase on the three phased creators (§7 conformance)
# ════════════════════════════════════════════════════════════════════════

_PHASED_TOOLS = ("create_task", "create_time_entry", "create_expense")


def test_phase_enums_are_derived_from_the_pure_module():
    """Unlike the hand-copied model literals, these enums are DERIVED from
    utils/phases.py (the _COVERAGE_CODES precedent) — this pin documents the
    contract rather than protecting against drift, which is impossible."""
    from utils import phases as phases_mod

    expected_phases = [c for c in phases_mod.VALID_PHASES if c]
    expected_sous = [c for c in phases_mod.VALID_SOUS_PHASES if c]
    for tool in _PHASED_TOOLS:
        schema = tools.TOOLS[tool]["input_schema"]
        props = schema["properties"]
        assert props["phase"]["enum"] == expected_phases, tool
        assert props["sous_phase"]["enum"] == expected_sous, tool
        # Optional (D-6 lives at the web form) with per-usage descriptions.
        assert "phase" not in schema.get("required", []), tool
        assert "sous_phase" not in schema.get("required", []), tool
        assert props["phase"]["description"], tool
        assert props["sous_phase"]["description"], tool


def test_phase_prefix_invariant_over_the_exposed_enums():
    from utils import phases as phases_mod

    props = tools.TOOLS["create_task"]["input_schema"]["properties"]
    for sp in props["sous_phase"]["enum"]:
        assert phases_mod.phase_of(sp) in props["phase"]["enum"], sp


def test_schema_rejects_unknown_phase_codes():
    schema = tools.TOOLS["create_task"]["input_schema"]
    assert tools.validate_args(schema, {"title": "x", "phase": "ZZZ"})
    assert tools.validate_args(schema, {"title": "x", "sous_phase": "CTS-77"})
    assert tools.validate_args(schema, {"title": "x", "phase": ""})  # "" excluded
    assert not tools.validate_args(schema, {"title": "x", "phase": "CTS"})


def test_resolve_phase_pair_ergonomics():
    assert handlers._resolve_phase_pair({}) == ("", "")
    assert handlers._resolve_phase_pair({"phase": "CTS"}) == ("CTS", "CTS-00")
    assert handlers._resolve_phase_pair({"sous_phase": "CTS-02"}) == ("CTS", "CTS-02")
    with pytest.raises(tools.ToolArgumentError, match="n'appartient pas"):
        handlers._resolve_phase_pair({"phase": "INT", "sous_phase": "CTS-02"})


def test_create_time_entry_stores_and_echoes_phase(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    seen = {}

    def _create(data):
        seen.update(data)
        return {**data, "id": "te-new", "amount": 30000}, []

    monkeypatch.setattr(handlers.time_entry_model, "create_time_entry", _create)
    payload = handlers.create_time_entry({
        "dossier_id": "d1", "date": "2026-08-10",
        "description": "Rédaction de la défense", "hours": 1.0,
        "sous_phase": "CTS-02",   # alone: the parent derives from the prefix
    })
    assert seen["phase"] == "CTS"
    assert seen["sous_phase"] == "CTS-02"
    assert payload["entity"]["phase"] == "CTS"
    assert payload["entity"]["sous_phase"] == "CTS-02"


def test_create_expense_without_phase_stays_blank(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())
    seen = {}

    def _create(data):
        seen.update(data)
        return {**data, "id": "e-new"}, []

    monkeypatch.setattr(handlers.expense_model, "create_expense", _create)
    payload = handlers.create_expense({
        "dossier_id": "d1", "date": "2026-08-10",
        "description": "Timbre", "amount_cents": 10500,
    })
    assert seen["phase"] == "" and seen["sous_phase"] == ""
    assert payload["entity"]["phase"] == ""


def test_create_task_contradictory_pair_refused_before_write(monkeypatch, bumps):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _wdossier())

    def _must_not_run(_data):
        raise AssertionError("a contradictory pair reached the model")

    monkeypatch.setattr(handlers.task_model, "create_task", _must_not_run)
    with pytest.raises(tools.ToolArgumentError):
        handlers.create_task({
            "dossier_id": "d1", "title": "x",
            "phase": "INT", "sous_phase": "CTS-02",
        })
    assert bumps["bump"] == []
