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
        "droit_action_date": DT, "date_avis": None, "prescription_notes": "",
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
        (handlers.task_model, "get_task_summary"),
        (handlers.hearing_model, "get_hearing_summary"),
        (handlers.note_model, "get_notes_summary"),
        (handlers.document_model, "get_document_summary"),
        (handlers.protocol_model, "get_protocol_summary"),
    ):
        monkeypatch.setattr(model, summary, lambda d: {"total": 1})
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
        lambda i: _dossier_doc(valeur=1500000, flat_fee=500000,
                               contingency_percent=2500, date_avis=DT,
                               closed_date=DT))
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
    monkeypatch.setattr(
        handlers.note_model, "list_notes",
        lambda **kw: [{"id": "n1", "dossier_id": "", "title": "Veille",
                       "content": "Texte", "category": "recherche",
                       "pinned": False, "created_at": DT, "updated_at": DT}])
    _conforms("list_notes", handlers.list_notes({}))


def test_get_note_both_branches_conform(monkeypatch):
    monkeypatch.setattr(
        handlers.note_model, "get_note",
        lambda i: {"id": "n1", "dossier_id": "d1",
                   "dossier_file_number": "2026-001", "dossier_title": "T",
                   "title": "Note", "content": "Corps",
                   "category": "recherche", "pinned": True,
                   "created_at": DT, "updated_at": DT})
    _conforms("get_note", handlers.get_note({"note_id": "n1"}))

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


def test_list_protocol_steps_conforms(monkeypatch):
    protocol = {"id": "pr1", "title": "Protocole de l'instance",
                "protocol_type": "cs_ordinaire", "status": "actif",
                "court": "C.S.", "start_date": DATE_ONLY, "end_date": None,
                "notes": "", "steps": [_step_doc()]}
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda d, active_only=True: protocol)
    _conforms("list_protocol_steps",
              handlers.list_protocol_steps({"dossier_id": "d1"}))


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
                               "bank_balance": 400000}],
                 "total_held_cents": 500000, "outstanding_count": 1,
                 "outstanding_total_cents": 100000, "in_transit_count": 1,
                 "in_transit_total_cents": 100000,
                 "last_reconciliation_date": DATE_ONLY,
                 "reconciliation_overdue": False})
    _conforms("get_trust_snapshot", handlers.get_trust_snapshot({}))


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
