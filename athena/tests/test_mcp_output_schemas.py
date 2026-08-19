"""Conformance: every declared outputSchema against the REAL handlers.

A declared ``outputSchema`` is a contract — the MCP spec (2025-06-18) makes
``structuredContent`` conformance a MUST. A schema the handlers violate is
therefore WORSE than no schema: a strict client would reject perfectly
valid responses, and nothing in production would say why. These tests run
each real handler (models monkeypatched, house pattern) and validate the
exact payload that becomes ``structuredContent`` — ``tools._jsonable(...)``
— against the schema shipped in tools/list, covering every ``anyOf``
branch.
"""

import os
import sys
from datetime import datetime, timezone
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
    from mcp.output_schemas import OUTPUT_SCHEMAS

UTC = timezone.utc
DT = datetime(2026, 7, 2, 14, 30, tzinfo=UTC)
DATE_ONLY = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _conforms(tool: str, payload) -> None:
    """Validate what structuredContent would carry against the contract."""
    clean = tools._jsonable(payload)
    errors = tools.validate_args(OUTPUT_SCHEMAS[tool], clean)
    assert errors == [], f"{tool}: {errors}"


# ══════════════════════════════════════════════════════════════════════
# Registry-level invariants
# ══════════════════════════════════════════════════════════════════════

def test_every_tool_declares_an_output_schema():
    assert set(OUTPUT_SCHEMAS) == set(tools.TOOLS)


def test_descriptors_ship_the_output_schema_and_title_mirror():
    for d in tools.list_tool_descriptors():
        assert d["outputSchema"] is OUTPUT_SCHEMAS[d["name"]]
        # 2025-03-26 clients read the display name from annotations.title.
        assert d["annotations"]["title"] == d["title"]


def test_output_schemas_never_forbid_additional_properties():
    """`additionalProperties: false` is a security control on INPUTS and
    poison on outputs: adding one payload field would make strict clients
    reject valid responses."""
    def walk(node):
        if isinstance(node, dict):
            assert node.get("additionalProperties") is not False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for name, schema in OUTPUT_SCHEMAS.items():
        walk(schema)


def test_every_output_schema_is_rooted_at_an_object():
    """The MCP wire schema for Tool.outputSchema REQUIRES a top-level
    `type: "object"` (const). A bare-anyOf root is invalid, and the official
    SDK zod-parses the whole ListToolsResult — one invalid descriptor kills
    all 19 tools at once, not just its own. Found by adversarial review
    against the official 2025-06-18 schema.json."""
    for name, schema in OUTPUT_SCHEMAS.items():
        assert schema.get("type") == "object", name


def test_every_input_property_carries_a_description():
    """The description is what the calling model reads BEFORE deciding to
    call. 31 of 48 properties had none (16 via the shared _ID fragment)."""
    for name, spec in tools.TOOLS.items():
        for prop, sub in spec["input_schema"].get("properties", {}).items():
            assert sub.get("description"), f"{name}.{prop} has no description"


# ══════════════════════════════════════════════════════════════════════
# Validator extensions the output schemas rely on
# ══════════════════════════════════════════════════════════════════════

def test_validator_nullable_union_types():
    schema = {"type": ["string", "null"]}
    assert tools.validate_args(schema, "x") == []
    assert tools.validate_args(schema, None) == []
    assert tools.validate_args(schema, 3) != []


def test_validator_anyof_accepts_any_matching_branch():
    schema = OUTPUT_SCHEMAS["get_note"]
    ok = {"found": False, "note_id": "n1"}
    assert tools.validate_args(schema, ok) == []


def test_validator_anyof_discriminates_on_the_enum():
    """A found=true payload missing its `note` must NOT sneak through the
    not-found branch — the enum discriminator blocks it."""
    schema = OUTPUT_SCHEMAS["get_note"]
    wrong = {"found": True, "note_id": "n1"}   # found=true but no note
    assert tools.validate_args(schema, wrong) != []


def test_validator_still_rejects_a_broken_envelope():
    assert tools.validate_args(
        OUTPUT_SCHEMAS["list_tasks"], {"items": "pas-une-liste"}
    ) != []


# ══════════════════════════════════════════════════════════════════════
# Fixtures — realistic model docs
# ══════════════════════════════════════════════════════════════════════

def _hearing_doc(hid="h1", dossier_id="d1"):
    return {
        "id": hid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Tremblay c. Lavoie" if dossier_id else "",
        "title": "Audience", "hearing_type": "audience",
        "start_datetime": datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
        "end_datetime": datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        "all_day": False, "location": "Palais de justice", "court": "C.S.",
        "judge": "", "status": "confirmée", "notes": "",
        "reminder_minutes": 1440, "etag": "e",
    }


def _task_doc(tid="t1", dossier_id="d1", due=DT):
    return {
        "id": tid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Tremblay" if dossier_id else "",
        "title": "Préparer requête", "description": "", "priority": "haute",
        "status": "à_faire", "category": "rédaction", "due_date": due,
        "completed_date": None, "related_note_id": None,
    }


def _step_doc(sid="s1"):
    return {
        "id": sid, "order": 1, "title": "Dépôt", "description": "",
        "cpc_reference": "art. 246 C.p.c.", "deadline_date": DT,
        "status": "à_venir", "mandatory": True, "deadline_locked": True,
        "date_confirmed": False, "completed_date": None,
        "linked_task_id": None, "linked_hearing_id": None, "notes": "",
    }


def _dossier_doc(**over):
    doc = {
        "id": "d1", "file_number": "2026-001", "title": "Tremblay c. Lavoie",
        "status": "actif", "domaine": "REC", "action": "REC-01",
        "action_precision": "", "role": "demandeur",
        "tribunal": "Cour supérieure", "court_file_number": "500-05-123456-241",
        "opened_date": DT, "closed_date": None, "prescription_date": DT,
        "clients": [{"id": "p1", "name": "Jean Tremblay"}],
        "opposing_parties": [{"id": "p2", "name": "Paul Lavoie"}],
        "sommaire": "Réclamation.", "greffe_number": "500",
        "juridiction_number": "05", "competence": "Division générale",
        "palais_de_justice": "Montréal", "district_judiciaire": "Montréal",
        "is_administrative_tribunal": False, "forum_type": "judiciaire",
        "mandate_type": "judiciaire", "fee_type": "hourly", "fee_notes": "",
        "hourly_rate": 25000, "flat_fee": None, "contingency_percent": None,
        "valeur": None, "prescription_type": "3_ans",
        "droit_action_date": DT, "date_avis": None,
        "prise_action_date": None, "prescription_notes": "",
        "created_at": DT, "updated_at": DT,
    }
    doc.update(over)
    return doc


def _partie_doc():
    return {
        "id": "p1", "type": "individual", "contact_role": "client",
        "prefix": "M.", "first_name": "Jean", "last_name": "Tremblay",
        "email": "jean@example.com", "phone_cell": "+15145551234",
        "address_city": "Montréal", "identity_verified": "vérifié",
        "identity_verified_date": DT, "conflict_check": "non_vérifié",
        "conflict_check_date": None, "kyc_document_ids": [],
        "mandataires": [{"id": "p3", "kind": "mandataire", "notes": ""}],
        "created_at": DT, "updated_at": DT,
    }


def _invoice_doc():
    return {
        "id": "i1", "invoice_number": "2026-001-01", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "client_name": "Jean Tremblay",
        "date": DATE_ONLY, "due_date": DATE_ONLY, "status": "envoyée",
        "total": 150000, "amount_due": 150000,
    }


_TIME_SUMMARY = {"total_hours": 10.0, "unbilled_hours": 4.0,
                 "total_billable_amount": 250000, "unbilled_amount": 100000}
_EXPENSE_SUMMARY = {"total_expenses": 5000, "unbilled_expenses": 5000}
_INVOICE_SUMMARY = {"count": 1, "total_invoiced": 150000,
                    "total_paid": 0, "total_outstanding": 150000}


# ══════════════════════════════════════════════════════════════════════
# Conformance — one real-handler run per anyOf branch
# ══════════════════════════════════════════════════════════════════════

def test_get_agenda_conforms(monkeypatch):
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=100: [_hearing_doc()])
    monkeypatch.setattr(handlers.task_model, "list_urgent_tasks",
                        lambda c, limit=50: [_task_doc()])
    monkeypatch.setattr(
        handlers.protocol_model, "list_urgent_steps",
        lambda c, limit=50: [{**_step_doc(), "_protocol_id": "pr1",
                              "_protocol_title": "Protocole",
                              "_dossier_file_number": "2026-001"}])
    monkeypatch.setattr(handlers.dossier_model, "list_prescription_alerts",
                        lambda c, limit=50: [_dossier_doc()])
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 4.0, "amount": 100000})
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 57495})
    monkeypatch.setattr(handlers.dossier_model, "count_open", lambda: 7)
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 150000)
    _conforms("get_agenda", handlers.get_agenda({"days_ahead": 14}))


def test_list_dossiers_conforms(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda **kw: ([_dossier_doc()], None))
    _conforms("list_dossiers", handlers.list_dossiers({}))


def test_get_dossier_both_branches_conform(monkeypatch):
    for model, summary in (
        (handlers.hearing_model, "get_hearing_summary"),
        (handlers.note_model, "get_notes_summary"),
        (handlers.document_model, "get_document_summary"),
    ):
        monkeypatch.setattr(model, summary, lambda d: {"total": 1})
    # These two take the Montréal day (lot 6) — the fake must accept it, or
    # it would mask a signature drift the deploy gate is meant to catch.
    monkeypatch.setattr(handlers.task_model, "get_task_summary",
                        lambda d, today=None: {"total": 1})
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_summary",
                        lambda d, today=None: {"total": 1})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary",
                        lambda d: dict(_TIME_SUMMARY))
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary",
                        lambda d: dict(_EXPENSE_SUMMARY))
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary",
                        lambda d: dict(_INVOICE_SUMMARY))

    # Branch: found, all-nullable fields at None (valeur/flat_fee/contingency)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier_doc())
    _conforms("get_dossier", handlers.get_dossier({"dossier_id": "d1"}))

    # Branch: found, every nullable field SET
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _dossier_doc(
            valeur=1500000, flat_fee=500000,
            contingency_percent=2500, date_avis=DT, prise_action_date=DT,
            closed_date=DT,
            # Full July-2026 party shape: roles + avocat per entry.
            clients=[{"id": "p1", "name": "Jean Tremblay",
                      "roles": ["défendeur", "demandeur reconventionnel"],
                      "avocat_id": "", "avocat_name": ""}],
            opposing_parties=[{"id": "p2", "name": "Paul Lavoie",
                               "roles": ["demandeur"],
                               "avocat_id": "av1", "avocat_name": "Roy"}]))
    _conforms("get_dossier", handlers.get_dossier({"dossier_id": "d1"}))

    # Branch: not found
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    _conforms("get_dossier", handlers.get_dossier({"dossier_id": "absent"}))


def test_list_tasks_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.task_model, "list_tasks",
        lambda **kw: [_task_doc(), _task_doc("t2", None, due=None)])
    _conforms("list_tasks", handlers.list_tasks({}))


def test_list_hearings_conforms(monkeypatch):
    monkeypatch.setattr(handlers.hearing_model, "list_hearings_in_range",
                        lambda a, b, limit=200: [_hearing_doc()])
    _conforms("list_hearings", handlers.list_hearings(
        {"date_from": "2026-08-25", "date_to": "2026-09-10"}))


def test_list_notes_conforms(monkeypatch):
    # One legacy note (no is_analyse key stored) + the analyse note — the
    # handler must emit a boolean is_analyse for both. The rows carry the
    # dossier_id the handler was called with, so the analyse row survives
    # BOTH branches (the Général branch filters on empty dossier_id — a
    # fixture pinned to "d1" would be dropped before validation and the
    # is_analyse=True case would never reach the schema).
    def _rows(dossier_id=None, **kw):
        did = dossier_id or ""
        return [{"id": "n1", "dossier_id": did, "title": "Veille",
                 "content": "Texte", "category": "recherche",
                 "pinned": False, "created_at": DT, "updated_at": DT},
                {"id": "n2", "dossier_id": did,
                 "title": "Théorie de la cause", "content": "Corps",
                 "category": "stratégie", "pinned": False,
                 "is_analyse": True, "dateless": True,
                 "created_at": DT, "updated_at": DT}]

    monkeypatch.setattr(handlers.note_model, "list_notes", _rows)

    payload = handlers.list_notes({})
    _conforms("list_notes", payload)
    assert payload["items"][1]["is_analyse"] is True

    payload = handlers.list_notes({"dossier_id": "d1"})
    _conforms("list_notes", payload)
    assert payload["items"][1]["is_analyse"] is True


def test_get_note_both_branches_conform(monkeypatch):
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1",
                   "dossier_file_number": "2026-001", "dossier_title": "T",
                   "title": "Note", "content": "Corps",
                   "category": "recherche", "pinned": True,
                   "created_at": DT, "updated_at": DT})
    _conforms("get_note", handlers.get_note({"note_id": "n1"}))

    # The analyse note (read-only flag emitted True)
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n2", "dossier_id": "d1",
                   "dossier_file_number": "2026-001", "dossier_title": "T",
                   "title": "Théorie de la cause", "content": "Corps",
                   "category": "stratégie", "pinned": False,
                   "is_analyse": True, "dateless": True,
                   "created_at": DT, "updated_at": DT})
    _conforms("get_note", handlers.get_note({"note_id": "n2"}))

    monkeypatch.setattr(handlers.note_model, "get_note", lambda i: None)
    _conforms("get_note", handlers.get_note({"note_id": "absent"}))


def test_list_documents_with_and_without_folder_conform(monkeypatch):
    doc = {"id": "doc1", "display_name": "Requête.pdf",
           "category": "procédure", "file_type": "application/pdf",
           "file_size": 1024, "version": 1, "folder_id": None,
           "description": "", "tags": ["urgent"], "created_at": DT}
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: [doc])
    _conforms("list_documents",
              handlers.list_documents({"dossier_id": "d1"}))

    # folder branch — the optional folder_path key appears
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: [{**doc, "folder_id": "f1"}])
    monkeypatch.setattr(handlers.folder_model, "get_folder_breadcrumb",
                        lambda d, f: [{"id": "f1", "name": "Projets"}])
    _conforms("list_documents",
              handlers.list_documents({"dossier_id": "d1", "folder_id": "f1"}))


def test_list_parties_conforms(monkeypatch):
    monkeypatch.setattr(handlers.partie_model, "list_parties",
                        lambda **kw: [_partie_doc()])
    _conforms("list_parties", handlers.list_parties({}))


def test_get_partie_both_branches_conform(monkeypatch):
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: _partie_doc())
    monkeypatch.setattr(
        handlers.dossier_model, "list_dossiers_for_partie",
        lambda i: [{"id": "d1", "file_number": "2026-001", "title": "T",
                    "status": "actif", "client_ids": ["p1"]}])
    _conforms("get_partie", handlers.get_partie({"partie_id": "p1"}))

    monkeypatch.setattr(handlers.partie_model, "get_partie", lambda i: None)
    _conforms("get_partie", handlers.get_partie({"partie_id": "absent"}))


def test_get_partie_list_valued_address_is_coerced_to_string(monkeypatch):
    """The CardDAV PUT path can store a LIST in an address field (vobject
    parses an unescaped ADR comma as a list; models/partie sanitizes only
    str values). The handler must coerce, or every later get_partie for
    that contact violates the declared schema and a strict client rejects
    it forever."""
    doc = _partie_doc()
    doc["address_street"] = ["450 rue Sainte-Catherine", "Bureau 5"]
    monkeypatch.setattr(handlers.partie_model, "get_partie", lambda i: doc)
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_for_partie",
                        lambda i: [])
    payload = handlers.get_partie({"partie_id": "p1"})
    assert payload["partie"]["address"]["street"] == (
        "450 rue Sainte-Catherine, Bureau 5"
    )
    _conforms("get_partie", payload)


def test_get_billing_snapshot_three_branches_conform(monkeypatch):
    # Branch 1: global
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 4.0, "amount": 100000})
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda: [_invoice_doc()])
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 150000)
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda billable_filter=None, **kw: {"amount": 57495})
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: ([{"id": "e1", "dossier_id": "d1",
                        "dossier_file_number": "2026-001",
                        "dossier_title": "Tremblay c. Lavoie",
                        "billable": True, "invoiced": False,
                        "hours": 2.0, "amount": 50000}], None))
    monkeypatch.setattr(
        handlers.expense_model, "list_expenses_page",
        lambda **kw: ([{"id": "x1", "dossier_id": "d1",
                        "dossier_file_number": "2026-001",
                        "dossier_title": "Tremblay c. Lavoie",
                        "amount": 57495}], None))
    _conforms("get_billing_snapshot", handlers.get_billing_snapshot({}))

    # Branch 2: dossier
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier_doc())
    monkeypatch.setattr(handlers.time_entry_model, "get_time_summary",
                        lambda d: dict(_TIME_SUMMARY))
    monkeypatch.setattr(handlers.expense_model, "get_expense_summary",
                        lambda d: dict(_EXPENSE_SUMMARY))
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_summary",
                        lambda d: dict(_INVOICE_SUMMARY))
    monkeypatch.setattr(
        handlers.time_entry_model, "get_unbilled_time_entries",
        lambda d: [{"id": "te1", "date": DATE_ONLY, "description": "Rédaction",
                    "hours": 2.0, "rate": 25000, "amount": 50000}])
    monkeypatch.setattr(
        handlers.expense_model, "get_unbilled_expenses",
        lambda d: [{"id": "ex1", "date": DATE_ONLY, "description": "Huissier",
                    "category": "signification", "taxable": True,
                    "amount": 5000}])
    _conforms("get_billing_snapshot",
              handlers.get_billing_snapshot({"dossier_id": "d1"}))

    # Branch 3: not found
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    _conforms("get_billing_snapshot",
              handlers.get_billing_snapshot({"dossier_id": "absent"}))


def test_list_time_entries_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.time_entry_model, "list_time_entries_page",
        lambda **kw: ([{"id": "e1", "dossier_id": "d1",
                        "dossier_file_number": "2026-001",
                        "dossier_title": "Tremblay c. Lavoie",
                        "date": DATE_ONLY, "description": "Rédaction",
                        "hours": 1.5, "rate": 30000, "amount": 45000,
                        "billable": True, "invoiced": True,
                        "invoice_id": "inv-1"}], None))
    _conforms("list_time_entries", handlers.list_time_entries({}))


def test_list_expenses_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.expense_model, "list_expenses_page",
        lambda **kw: ([{"id": "x1", "dossier_id": "d1",
                        "dossier_file_number": "2026-001",
                        "dossier_title": "Tremblay c. Lavoie",
                        "date": DATE_ONLY, "description": "Huissier",
                        "category": "signification", "taxable": True,
                        "invoiced": False, "amount": 9500}], None))
    _conforms("list_expenses", handlers.list_expenses({}))


def test_list_deletions_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.audit_event_model, "list_recent",
        lambda **kw: [{"id": "ev1", "at": DT, "entity_type": "task",
                       "entity_id": "t9", "dossier_id": "d1",
                       "snapshot_min": {"title": "Produire la proposition",
                                        "status": "à_faire"}}])
    payload = handlers.list_deletions({})
    _conforms("list_deletions", payload)
    assert payload["items"][0]["title"] == "Produire la proposition"


def test_list_protocol_steps_conforms(monkeypatch):
    protocol = {"id": "pr1", "title": "Protocole de l'instance",
                "protocol_type": "cs_ordinaire", "status": "actif",
                "court": "C.S.", "start_date": DATE_ONLY, "end_date": None,
                "notes": "", "steps": [_step_doc()]}
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: protocol)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda d: _dossier_doc())
    payload = handlers.list_protocol_steps({"dossier_id": "d1"})
    _conforms("list_protocol_steps", payload)
    # cs_ordinaire on the fixture's Cour supérieure dossier — coherent.
    assert payload["protocols"][0]["regime_mismatch"] is False


def test_compute_judicial_deadline_both_branches_conform():
    # 2026-07-10 + 2 lands on a Sunday → adjusted (reason non-null)
    _conforms("compute_judicial_deadline", handlers.compute_judicial_deadline(
        {"start_date": "2026-07-10", "delay_days": 2, "direction": "after"}))
    # plain weekday landing → unadjusted (reason null)
    _conforms("compute_judicial_deadline", handlers.compute_judicial_deadline(
        {"start_date": "2026-07-06", "delay_days": 1, "direction": "after"}))


def test_parse_court_file_number_three_branches_conform():
    _conforms("parse_court_file_number", handlers.parse_court_file_number(
        {"court_file_number": "500-05-123456-241"}))
    _conforms("parse_court_file_number", handlers.parse_court_file_number(
        {"court_file_number": "TAL-12345"}))
    _conforms("parse_court_file_number", handlers.parse_court_file_number(
        {"court_file_number": "n'importe quoi"}))


def test_get_trust_balance_both_branches_conform(monkeypatch):
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: _dossier_doc())
    monkeypatch.setattr(
        handlers.trust_model, "get_trust_summary",
        lambda d: {"has_trust": True, "total_cents": 500000,
                   "by_client": [{"client_id": "p1",
                                  "client_name": "Jean Tremblay",
                                  "book_cents": 500000,
                                  "cleared_cents": 400000,
                                  "in_transit_cents": 100000}]})
    _conforms("get_trust_balance",
              handlers.get_trust_balance({"dossier_id": "d1"}))

    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: None)
    _conforms("get_trust_balance",
              handlers.get_trust_balance({"dossier_id": "absent"}))


def test_list_trust_transactions_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.trust_model, "list_transactions",
        lambda **kw: [{"id": "tx1", "sequence": 12, "date": DATE_ONLY,
                       "dossier_file_number": "2026-001",
                       "counterparty": "Jean Tremblay",
                       "client_name": "Jean Tremblay",
                       "purpose": "avance_honoraires", "method": "virement",
                       "direction": "recette", "status": "compensée",
                       "cleared_date": DATE_ONLY, "reversed_by_id": None,
                       "balance_after_account": 500000,
                       "balance_after_client": 500000, "amount": 500000}])
    _conforms("list_trust_transactions", handlers.list_trust_transactions({}))


def test_get_trust_snapshot_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.trust_model, "get_firm_trust_snapshot",
        lambda: {"accounts": [{"id": "a1", "name": "Compte général",
                               "institution": "Desjardins",
                               "account_type": "général",
                               "book_balance": 500000,
                               "bank_balance": 400000,
                               "last_reconciliation_date": DATE_ONLY,
                               "never_reconciled": False,
                               "reconciliation_overdue": False}],
                 "total_held_cents": 500000, "outstanding_count": 1,
                 "outstanding_total_cents": 100000,
                 "outstanding_rows": [
                     {"id": "t9", "account_id": "a1", "date": DATE_ONLY,
                      "reference": "chq 42", "counterparty": "Huissiers QC",
                      "dossier_file_number": "2026-001", "amount": 100000},
                 ],
                 "in_transit_count": 1,
                 "in_transit_total_cents": 100000,
                 "last_reconciliation_date": DATE_ONLY,
                 "reconciliation_overdue": False,
                 "reconciliation_never_performed": False})
    monkeypatch.setattr(
        handlers.trust_model, "list_dossiers_with_trust",
        lambda: [{"dossier_id": "d1", "file_number": "2026-001",
                  "title": "Tremblay c. Lavoie", "status": "actif",
                  "book_cents": 500000, "cleared_cents": 400000}])
    payload = handlers.get_trust_snapshot({})
    _conforms("get_trust_snapshot", payload)
    # The two totals carry their fr-CA twins now (constraint-5 cleanup).
    assert payload["outstanding_total_display"]
    assert payload["in_transit_total_display"]
    assert payload["outstanding_cheques"][0]["reference"] == "chq 42"
    assert payload["by_dossier"][0]["book_balance_cents"] == 500000


@pytest.fixture()
def write_world(monkeypatch):
    monkeypatch.setattr(handlers, "bump_ctag", lambda n: None)
    monkeypatch.setattr(handlers, "remove_tombstone", lambda n, r: None)
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: {"id": "d1", "file_number": "2026-001",
                   "title": "Tremblay", "status": "actif"})
    monkeypatch.setattr(
        handlers.note_model, "create_note",
        lambda data: ({**data, "id": "n-new", "created_at": DT,
                       "updated_at": DT}, []))


def test_create_note_conforms(write_world):
    _conforms("create_note", handlers.create_note(
        {"dossier_id": "d1", "title": "Recherche", "content": "Corps"}))


def test_create_note_general_branch_conforms(write_world):
    _conforms("create_note", handlers.create_note(
        {"title": "Veille", "content": "Corps"}))


def test_append_to_note_conforms(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1", "content": "Original"})
    monkeypatch.setattr(
        handlers.note_model, "update_note",
        lambda nid, data: ({"id": nid, "dossier_id": "d1",
                            "dossier_file_number": "2026-001",
                            "dossier_title": "Tremblay", "title": "Note",
                            "category": "recherche", "created_at": DT,
                            "updated_at": DT, **data}, []))
    _conforms("append_to_note", handlers.append_to_note(
        {"note_id": "n1", "content": "Suite"}))


# ── WP16 creators: conformance, live and dry ────────────────────────────


def test_create_task_conforms_live_and_dry(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.task_model, "create_task",
        lambda data: ({**data, "id": "t-new"}, []))
    args = {"dossier_id": "d1", "title": "Produire la réponse",
            "due_date": "2026-08-05", "priority": "haute"}
    payload = handlers.create_task(dict(args))
    _conforms("create_task", payload)
    assert payload["entity"]["status"] == "à_faire"
    dry = handlers.create_task({**args, "dry_run": True})
    _conforms("create_task", dry)
    assert dry["dry_run"] is True and dry["entity"]["id"] == ""


def test_create_hearing_conforms_live_and_dry(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.hearing_model, "create_hearing",
        lambda data: ({**data, "id": "h-new"}, []))
    args = {"dossier_id": "d1", "title": "Interrogatoire",
            "hearing_type": "interrogatoire", "date": "2026-09-10",
            "start_time": "09:30"}
    payload = handlers.create_hearing(dict(args))
    _conforms("create_hearing", payload)
    assert payload["entity"]["forum"] == "extrajudiciaire"
    dry = handlers.create_hearing({**args, "dry_run": True})
    _conforms("create_hearing", dry)


def test_create_time_entry_conforms_live_and_dry(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.time_entry_model, "create_time_entry",
        lambda data: ({**data, "id": "e-new",
                       "amount": int(data["hours"] * data["rate"])}, []))
    args = {"dossier_id": "d1", "date": "2026-07-30",
            "description": "Rédaction de la demande", "hours": 1.5,
            "rate_cents": 30000}
    payload = handlers.create_time_entry(dict(args))
    _conforms("create_time_entry", payload)
    assert payload["entity"]["amount_cents"] == 45000
    assert "ctag_bumped" not in payload      # not DAV-exposed — never faked
    dry = handlers.create_time_entry({**args, "dry_run": True})
    _conforms("create_time_entry", dry)


def test_complete_dossier_conforms_live_and_dry(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _dossier_doc(domaine="", action="", valeur=None),
    )
    monkeypatch.setattr(handlers.dossier_model, "field_defaults",
                        lambda: {"domaine": "", "action": "", "valeur": None})
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: ({**_dossier_doc(), **data}, []),
    )
    args = {"dossier_id": "d1", "domaine": "REC", "action": "REC-01",
            "valeur": 1190000}
    payload = handlers.complete_dossier(dict(args))
    _conforms("complete_dossier", payload)
    assert set(payload["fields_set"]) == {"domaine", "action", "valeur"}
    dry = handlers.complete_dossier({**args, "dry_run": True})
    _conforms("complete_dossier", dry)


def test_record_signification_conforms(write_world, monkeypatch):
    dossier = _dossier_doc()
    dossier["significations"] = []
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: dict(dossier))
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: ({**dossier, **data}, []),
    )
    payload = handlers.record_signification({
        "dossier_id": "d1", "partie_id": "p2", "date": "2026-07-15",
        "mode": "huissier", "confirmee": True,
    })
    _conforms("record_signification", payload)
    assert payload["entity"]["partie_id"] == "p2"


def test_record_prescription_event_conforms_and_derives(write_world, monkeypatch):
    dossier = _dossier_doc()
    dossier["prescription_events"] = []
    dossier["prise_action_date"] = None
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: dict(dossier))
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data: ({**dossier, **data}, []),
    )
    payload = handlers.record_prescription_event({
        "dossier_id": "d1", "type": "interruption_depot",
        "date": "2026-05-15", "reference": "signification DII",
    })
    _conforms("record_prescription_event", payload)
    # The answer that motivated the call: the delay no longer runs.
    assert payload["prescription_status"] == "interrompue"
    assert payload["prescription_date_effective"] is None


def test_create_expense_conforms_live_and_dry(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.expense_model, "create_expense",
        lambda data: ({**data, "id": "x-new"}, []))
    args = {"dossier_id": "d1", "date": "2026-07-30",
            "description": "Huissier — signification", "amount_cents": 9500,
            "category": "signification"}
    payload = handlers.create_expense(dict(args))
    _conforms("create_expense", payload)
    assert "ctag_bumped" not in payload
    dry = handlers.create_expense({**args, "dry_run": True})
    _conforms("create_expense", dry)


# ── Lot 4: the invoice register conforms ────────────────────────────────


def _invoice_doc(**over):
    doc = {
        "id": "inv1", "invoice_number": "2026-001-01", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "Tremblay",
        "client_id": "p1", "client_name": "Jean Tremblay",
        "date": DT, "due_date": DT, "status": "envoyée",
        "subtotal_fees": 100000, "subtotal_expenses": 0, "subtotal": 100000,
        "gst_rate": 500, "gst_amount": 5000,
        "qst_rate": 9975, "qst_amount": 9975,
        "total": 114975, "retainer_applied": 0, "amount_due": 114975,
        "amount_paid": 0, "paid_date": None,
        "notes": "", "payment_terms": "Payable dans les 30 jours.",
    }
    doc.update(over)
    return doc


def test_list_invoices_conforms_both_branches(monkeypatch):
    monkeypatch.setattr(
        handlers.invoice_model, "list_invoices_page",
        lambda **kw: ([_invoice_doc()], None))
    _conforms("list_invoices", handlers.list_invoices({}))
    # The dossier-scoped branch takes the other code path entirely.
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: [_invoice_doc(amount_paid=50000)])
    _conforms("list_invoices", handlers.list_invoices({"dossier_id": "d1"}))


def test_get_invoice_conforms_found_and_not_found(monkeypatch):
    items = [
        {"id": "l1", "type": "fee", "source_id": "t1", "date": DT,
         "description": "Rédaction", "hours": 2.0, "rate": 30000,
         "amount": 100000, "taxable": True},
        {"id": "l2", "type": "expense", "source_id": "x1", "date": DT,
         "description": "Huissier", "hours": None, "rate": None,
         "amount": 0, "taxable": False},
    ]
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (_invoice_doc(), items))
    _conforms("get_invoice", handlers.get_invoice({"invoice_id": "inv1"}))
    # The warning branch has its own shape to satisfy.
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (_invoice_doc(), []))
    _conforms("get_invoice", handlers.get_invoice({"invoice_id": "inv1"}))
    monkeypatch.setattr(handlers.invoice_model, "get_invoice_with_items",
                        lambda i: (None, []))
    _conforms("get_invoice", handlers.get_invoice({"invoice_id": "nope"}))


def test_billing_snapshot_still_conforms_after_the_shared_row_grew(monkeypatch):
    """_invoice_row is SHARED — every key lot 4 added to it also lands in
    get_billing_snapshot.outstanding_invoices[]. Deliberate (one row shape,
    no drift), and pinned here so the blast radius stays visible."""
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: [_invoice_doc()])
    monkeypatch.setattr(handlers.invoice_model, "get_outstanding_total",
                        lambda: 114975)
    monkeypatch.setattr(handlers.time_entry_model, "get_unbilled_totals",
                        lambda: {"hours": 1.0, "amount": 1000})
    monkeypatch.setattr(handlers.expense_model, "get_filtered_expense_totals",
                        lambda **kw: {"amount": 0})
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_page",
                        lambda **kw: ([], None))
    payload = handlers.get_billing_snapshot({})
    _conforms("get_billing_snapshot", payload)
    assert payload["outstanding_invoices"][0]["payment_basis"] == "none"


# ── Lot 1: the scoped search conforms on every branch ───────────────────


def _scoped_note(nid, dossier_id=""):
    return {
        "id": nid, "dossier_id": dossier_id,
        "dossier_file_number": "2026-001" if dossier_id else "",
        "dossier_title": "Tremblay" if dossier_id else "",
        "title": "Recherche", "content": "Corps", "category": "recherche",
        "pinned": False, "is_analyse": False,
        "created_at": DT, "updated_at": DT,
    }


def test_list_notes_conforms_on_all_three_scopes(monkeypatch):
    corpus = [_scoped_note("n1"), _scoped_note("n2", "d1")]
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: list(corpus))
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    for args in ({}, {"dossier_id": "d1"}, {"scope": "cabinet"},
                 {"scope": "cabinet", "limit": 1}):
        _conforms("list_notes", handlers.list_notes(args))


def test_list_documents_conforms_on_both_scopes(monkeypatch):
    docs = [{
        "id": "x1", "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "Tremblay", "display_name": "Jugement.pdf",
        "category": "jugement", "file_type": "application/pdf",
        "file_size": 2048, "version": 1, "folder_id": None,
        "document_date": None, "description": "", "tags": [],
        "created_at": DT, "updated_at": DT,
    }]
    monkeypatch.setattr(handlers.document_model, "list_documents",
                        lambda **kw: list(docs))
    monkeypatch.setattr(handlers.folder_model, "get_folder_tree", lambda d: [])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    _conforms("list_documents", handlers.list_documents({"dossier_id": "d1"}))
    _conforms("list_documents", handlers.list_documents({"scope": "cabinet"}))


# ── Lot Q: the two reference/lookup reads ──────────────────────────────


@pytest.mark.parametrize(
    "kind",
    ["domaines", "actions", "prescription_types", "forums", "districts", "phases"],
)
def test_get_reference_vocabulary_conforms(kind):
    """Every branch: the six vocabularies come from six different pure
    sources, so one shape holding for one of them proves nothing."""
    _conforms("get_reference_vocabulary", handlers.get_reference_vocabulary(
        {"kind": kind}
    ))


def test_get_reference_vocabulary_conforms_filtered():
    _conforms("get_reference_vocabulary", handlers.get_reference_vocabulary(
        {"kind": "actions", "domaine": "REC"}
    ))


def test_partie_writes_conform_live_and_dry(monkeypatch):
    import models

    monkeypatch.setattr(handlers, "bump_ctag", lambda n: None)
    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    monkeypatch.setattr(handlers.partie_model, "create_partie",
                        lambda data: ({**data, "id": "p-new"}, []))
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: {"id": "p1", "type": "individual",
                                   "last_name": "Tremblay"})
    monkeypatch.setattr(handlers.partie_model, "update_partie",
                        lambda pid, data: ({"id": pid, "type": "individual",
                                            "last_name": "Tremblay", **data}, []))

    args = {"type": "individual", "last_name": "Tremblay",
            "first_name": "Jean", "legacy_ref": "L-42"}
    _conforms("create_partie", handlers.create_partie(dict(args)))
    _conforms("create_partie", handlers.create_partie({**args, "dry_run": True}))

    upd = {"partie_id": "p1", "notes": "corrigé"}
    _conforms("update_partie", handlers.update_partie(dict(upd)))
    _conforms("update_partie", handlers.update_partie({**upd, "dry_run": True}))


def test_a_failed_addressbook_bump_still_conforms(monkeypatch):
    """The write landed; only the sync did not. The payload must stay valid
    so the warning actually reaches the caller."""
    import models

    def _boom(name):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(handlers, "bump_ctag", _boom)
    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    monkeypatch.setattr(handlers.partie_model, "create_partie",
                        lambda data: ({**data, "id": "p-new"}, []))
    payload = handlers.create_partie({"type": "individual", "last_name": "T"})
    assert payload["ctag_bumped"] is False and payload["warnings"]
    _conforms("create_partie", payload)


def test_import_invoice_conforms_live_and_dry(monkeypatch):
    """Both branches: the dry run carries line_preview, the live one does
    not — and the schema types it without requiring it."""
    import models

    entry = {"id": "e1", "dossier_id": "d1", "amount": 45000,
             "invoiced": False, "description": "Rédaction", "taxable": True}
    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "file_number": "2019-014",
                                   "title": "T", "status": "fermé",
                                   "clients": [{"id": "p1", "name": "Jean"}]})
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: {"id": "p1", "type": "individual",
                                   "last_name": "Tremblay"})
    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry",
                        lambda i: entry)
    monkeypatch.setattr(
        handlers.invoice_model, "create_invoice",
        lambda d, e, x, data, **kw: ({**data, "id": "i-new",
                                      "invoice_number": kw["invoice_number"],
                                      "status": "brouillon",
                                      "subtotal_fees": 45000,
                                      "subtotal_expenses": 0,
                                      "subtotal": 45000, "gst_amount": 2250,
                                      "qst_amount": 4489, "total": 51739}, []))

    args = {"dossier_id": "d1", "invoice_number": "2019-F014",
            "date": "2019-11-08", "expected_total_cents": 51739,
            "time_entry_ids": ["e1"]}
    live = handlers.import_invoice(dict(args))
    _conforms("import_invoice", live)
    assert "line_preview" not in live

    dry = handlers.import_invoice({**args, "dry_run": True})
    _conforms("import_invoice", dry)
    assert dry["line_preview"]


def test_billing_edits_conform_live_and_dry(monkeypatch):
    entry = {"id": "e1", "dossier_id": "d1", "description": "Rédaction",
             "hours": 1.5, "rate": 30000, "amount": 45000, "billable": True,
             "invoiced": False, "phase": "CTS", "sous_phase": "CTS-02",
             "date": DT}
    disb = {"id": "x1", "dossier_id": "d1", "description": "Timbre",
            "amount": 5000, "taxable": True, "invoiced": False,
            "category": "timbre_judiciaire", "date": DT}
    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry",
                        lambda i: entry)
    monkeypatch.setattr(handlers.time_entry_model, "update_time_entry",
                        lambda i, d: ({**entry, **d}, []))
    monkeypatch.setattr(handlers.expense_model, "get_expense", lambda i: disb)
    monkeypatch.setattr(handlers.expense_model, "update_expense",
                        lambda i, d: ({**disb, **d}, []))

    te = {"time_entry_id": "e1", "hours": 0.25}
    _conforms("update_time_entry", handlers.update_time_entry(dict(te)))
    _conforms("update_time_entry",
              handlers.update_time_entry({**te, "dry_run": True}))

    ex = {"expense_id": "x1", "amount_cents": 5250}
    _conforms("update_expense", handlers.update_expense(dict(ex)))
    _conforms("update_expense",
              handlers.update_expense({**ex, "dry_run": True}))


def _phase_world(monkeypatch):
    """Two BILLED rows — the state these four tools exist to reach."""
    entries = {
        "e1": {"id": "e1", "dossier_id": "d1", "description": "Rédaction",
               "hours": 1.5, "rate": 30000, "amount": 45000, "billable": True,
               "invoiced": True, "invoice_id": "i1",
               "phase": "", "sous_phase": "", "date": DT},
        "e2": {"id": "e2", "dossier_id": "d1", "description": "Appel",
               "hours": 0.5, "rate": 30000, "amount": 15000, "billable": True,
               "invoiced": True, "invoice_id": "i1",
               "phase": "CTS", "sous_phase": "CTS-02", "date": DT},
    }
    disbs = {
        "x1": {"id": "x1", "dossier_id": "d1", "description": "Timbre",
               "amount": 5000, "taxable": True, "invoiced": True,
               "invoice_id": "i1", "category": "timbre_judiciaire",
               "phase": "", "sous_phase": "", "date": DT},
    }

    def _set_time(i, p, s):
        doc = entries[i]
        changed = (doc["phase"], doc["sous_phase"]) != (p, s)
        doc.update(phase=p, sous_phase=s)
        return dict(doc), [], changed

    def _set_exp(i, p, s):
        doc = disbs[i]
        changed = (doc["phase"], doc["sous_phase"]) != (p, s)
        doc.update(phase=p, sous_phase=s)
        return dict(doc), [], changed

    monkeypatch.setattr(handlers.time_entry_model, "get_time_entries_bulk",
                        lambda ids: {i: dict(entries[i]) for i in ids
                                     if i in entries})
    monkeypatch.setattr(handlers.time_entry_model, "set_time_entry_phase",
                        _set_time)
    monkeypatch.setattr(handlers.expense_model, "get_expenses_bulk",
                        lambda ids: {i: dict(disbs[i]) for i in ids
                                     if i in disbs})
    monkeypatch.setattr(handlers.expense_model, "set_expense_phase", _set_exp)


def test_phase_single_conforms_live_and_dry(monkeypatch):
    _phase_world(monkeypatch)

    te = {"time_entry_id": "e1", "sous_phase": "INT-01"}
    _conforms("set_time_entry_phase",
              handlers.set_time_entry_phase({**te, "dry_run": True}))
    _conforms("set_time_entry_phase", handlers.set_time_entry_phase(dict(te)))
    # …and the « unchanged » branch, which writes nothing at all.
    _conforms("set_time_entry_phase", handlers.set_time_entry_phase(dict(te)))

    ex = {"expense_id": "x1", "phase": "PRE"}
    _conforms("set_expense_phase",
              handlers.set_expense_phase({**ex, "dry_run": True}))
    _conforms("set_expense_phase", handlers.set_expense_phase(dict(ex)))


def test_phase_bulk_conforms_across_every_outcome(monkeypatch):
    """One call carrying all three outcomes — applied, unchanged, refused —
    because `reason`, `dossier_id` and `invoiced` are null on exactly one of
    them and the schema has to accept the mixture."""
    _phase_world(monkeypatch)

    args = {"entries": [
        {"time_entry_id": "e1", "sous_phase": "INT-01"},   # applied
        {"time_entry_id": "e2", "sous_phase": "CTS-02"},   # unchanged
        {"time_entry_id": "absent", "sous_phase": "PRE-01"},  # refused
        {"time_entry_id": "e3"},                           # refused, no code
    ]}
    dry = handlers.set_time_entry_phase_bulk(
        {"entries": [dict(i) for i in args["entries"]], "dry_run": True}
    )
    _conforms("set_time_entry_phase_bulk", dry)

    live = handlers.set_time_entry_phase_bulk(
        {"entries": [dict(i) for i in args["entries"]]}
    )
    _conforms("set_time_entry_phase_bulk", live)
    assert live["applied"] == 1 and live["unchanged"] == 1
    assert live["refused"] == 2 and live["requested"] == 4

    _conforms("set_expense_phase_bulk", handlers.set_expense_phase_bulk(
        {"entries": [{"expense_id": "x1", "phase": "PRE"}]}
    ))


def test_dossier_writes_conform_live_and_dry(monkeypatch):
    import models

    parties = {"p1": {"id": "p1", "type": "individual", "last_name": "T"}}
    existing = {"id": "d1", "file_number": "2019-014", "title": "T",
                "status": "actif", "clients": []}
    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    monkeypatch.setattr(handlers.partie_model, "get_partie", parties.get)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier_by_file_number",
                        lambda fn: None)
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: existing)
    monkeypatch.setattr(handlers.dossier_model, "create_dossier",
                        lambda data: ({**data, "id": "d-new"}, []))
    monkeypatch.setattr(handlers.dossier_model, "update_dossier",
                        lambda did, data: ({**existing, **data, "id": did}, []))

    args = {"file_number": "2019-014", "title": "Tremblay c. Lavoie",
            "clients": [{"partie_id": "p1", "roles": ["demandeur"]}],
            "status": "fermé"}
    _conforms("create_dossier", handlers.create_dossier(dict(args)))
    _conforms("create_dossier", handlers.create_dossier({**args, "dry_run": True}))

    upd = {"dossier_id": "d1", "sommaire": "résumé"}
    _conforms("update_dossier", handlers.update_dossier(dict(upd)))
    _conforms("update_dossier", handlers.update_dossier({**upd, "dry_run": True}))


def test_get_import_audit_conforms_found_and_not_found(monkeypatch):
    dossier = {"id": "d1", "file_number": "2019-014", "title": "T",
               "status": "fermé", "closed_date": None, "client_ids": ["p1"],
               "hourly_rate": 30000}
    invoice = {"id": "i1", "invoice_number": "2019-F014",
               "status": "brouillon", "subtotal": 45000, "total": 45000,
               "date": DT}
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: dossier if i == "d1" else None)
    monkeypatch.setattr(handlers.dossier_model, "field_defaults",
                        lambda: {"hourly_rate": 30000})
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        lambda **kw: ([{"id": "e1", "amount": 45000,
                                        "invoiced": False,
                                        "description": "A", "date": DT}], None))
    monkeypatch.setattr(handlers.expense_model, "list_expenses_page",
                        lambda **kw: ([], None))
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: [invoice])
    monkeypatch.setattr(handlers.invoice_model, "list_line_items",
                        lambda iid: [{"id": "li1", "source_id": "e1",
                                      "amount": 45000}])
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    _conforms("get_import_audit", payload)
    assert payload["findings"]          # the branch with findings, not an empty one

    _conforms("get_import_audit",
              handlers.get_import_audit({"dossier_id": "absent"}))


def test_get_import_audit_conforms_with_unreadable_line_items(monkeypatch):
    """The tri-state branch: subtotal_matches_line_items is null, which the
    schema types as ["boolean", "null"] and the validator must accept."""
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: {"id": "d1", "file_number": "x", "title": "T",
                                   "status": "actif", "client_ids": []})
    monkeypatch.setattr(handlers.dossier_model, "field_defaults",
                        lambda: {"hourly_rate": 30000})
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        lambda **kw: ([], None))
    monkeypatch.setattr(handlers.expense_model, "list_expenses_page",
                        lambda **kw: ([], None))
    monkeypatch.setattr(handlers.invoice_model, "list_invoices",
                        lambda **kw: [{"id": "i1", "invoice_number": "F1",
                                       "status": "payée", "subtotal": 1,
                                       "total": 1, "date": DT}])
    monkeypatch.setattr(handlers.invoice_model, "list_line_items",
                        lambda iid: [])
    payload = handlers.get_import_audit({"dossier_id": "d1"})
    assert payload["invoices"][0]["subtotal_matches_line_items"] is None
    _conforms("get_import_audit", payload)


def test_find_imported_conforms_found_and_empty(monkeypatch):
    import models

    rows = {
        "parties": [{"id": "p1", "type": "individual", "last_name": "Tremblay"}],
        "invoices": [{"id": "i1", "invoice_number": "2019-F014",
                      "dossier_id": "d1"}],
    }
    monkeypatch.setattr(models, "find_by_legacy_ref",
                        lambda c, r, limit=5: list(rows.get(c, [])))
    _conforms("find_imported", handlers.find_imported({"legacy_ref": "L-42"}))

    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    empty = handlers.find_imported({"legacy_ref": "L-42"})
    _conforms("find_imported", empty)
    assert empty["count"] == 0


# ── Lot 5: the coverage report conforms, including its guard branches ───


def test_get_coverage_report_conforms(monkeypatch):
    dossier = {
        "id": "d1", "file_number": "2026-001", "title": "T", "status": "actif",
        "forum_type": "judiciaire", "tribunal": "Cour supérieure",
        "court_file_number": "", "action": "REC-01", "valeur": None,
        "opposing_parties": [{"id": "a1"}], "significations": [],
        "client_ids": ["p1"], "prescription_type": "3_ans",
        "prescription_date": None, "prescription_events": [],
        "prise_action_date": None,
    }
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers",
                        lambda status_filter=None, **kw: [dossier])
    monkeypatch.setattr(handlers.protocol_model, "list_protocols",
                        lambda **kw: [{"dossier_id": "dX",
                                       "protocol_type": "cs_ordinaire"}])
    monkeypatch.setattr(handlers.protocol_model, "regime_mismatch",
                        lambda t, d: False)
    monkeypatch.setattr(
        handlers.partie_model, "get_parties_bulk",
        lambda ids: {"p1": {"identity_verified": "non_vérifié",
                            "conflict_check": "non_vérifié"}})
    monkeypatch.setattr(handlers.task_model, "list_tasks_by_status",
                        lambda st, **kw: [])
    payload = handlers.get_coverage_report({})
    _conforms("get_coverage_report", payload)
    assert payload["summary"]["manquements"] >= 1

    # The suppressed-checks branch has its own shape to satisfy.
    monkeypatch.setattr(handlers.protocol_model, "list_protocols",
                        lambda **kw: [])
    monkeypatch.setattr(handlers.partie_model, "get_parties_bulk",
                        lambda ids: {})
    guarded = handlers.get_coverage_report({})
    _conforms("get_coverage_report", guarded)
    assert guarded["data_completeness"]["kyc_checked"] is False


# ── Lot 3: complete_task conforms on every branch ───────────────────────


def test_complete_task_conforms(write_world, monkeypatch):
    task = {
        "id": "t1", "title": "Produire", "description": "", "status": "à_faire",
        "priority": "normale", "category": "rédaction", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "dossier_title": "T",
        "due_date": None, "completed_date": None, "related_note_id": None,
    }
    monkeypatch.setattr(handlers.task_model, "get_task", lambda i: dict(task))
    monkeypatch.setattr(handlers.task_model, "update_task",
                        lambda tid, data: ({**task, **data}, []))
    monkeypatch.setattr(handlers.task_model, "_validate", lambda d: [])
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda did, active_only=True: None)
    _conforms("complete_task", handlers.complete_task({"task_id": "t1"}))
    _conforms("complete_task",
              handlers.complete_task({"task_id": "t1", "dry_run": True}))

    # The already-closed branch writes nothing and has its own shape.
    task["status"] = "terminée"
    payload = handlers.complete_task({"task_id": "t1"})
    _conforms("complete_task", payload)
    assert payload["already_completed"] is True

    # And the cascade branch, where every protocol_step_effect key is filled.
    task["status"] = "à_faire"
    monkeypatch.setattr(
        handlers.protocol_model, "get_protocol_for_dossier",
        lambda did, active_only=True: {
            "id": "p1", "status": "actif",
            "steps": [{"id": "s1", "title": "Réponse", "status": "à_venir",
                       "linked_task_id": "t1"}]})
    monkeypatch.setattr(
        handlers.protocol_model, "get_protocol",
        lambda pid: {"id": "p1", "status": "complété",
                     "steps": [{"id": "s1", "title": "Réponse",
                                "status": "complété", "linked_task_id": "t1"}]})
    cascaded = handlers.complete_task({"task_id": "t1"})
    _conforms("complete_task", cascaded)
    assert cascaded["protocol_step_effect"]["protocol_closed"] is True
