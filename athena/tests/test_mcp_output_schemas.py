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

import json
import os
import re
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
    import mcp.write_support as write_support
    from mcp.output_schemas import OUTPUT_SCHEMAS
    from models import concurrency

UTC = timezone.utc
DT = datetime(2026, 7, 2, 14, 30, tzinfo=UTC)
DATE_ONLY = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


# The ONE (tool, top-level key) pair whose VALUE may be a capability URL —
# begin_upload's resumable-upload session URI (plan D4, lot 2A T9). The same
# single pair tests/test_mcp_framework_guards exempts by NAME; the value
# scan below holds every other real payload to « no signed URL, no storage
# path, no session URI » — whatever key it would hide under.
_CAPABILITY_VALUE_ALLOWLIST = frozenset({("begin_upload", "upload_url")})


def _capability_leak(tool: str, payload) -> bool:
    """True when *payload* — outside the allowlisted pair — carries a
    capability: a key named like one, a signed-URL or session marker, or a
    Cloud Storage host."""
    scanned = payload
    if isinstance(payload, dict):
        scanned = {k: v for k, v in payload.items()
                   if (tool, k) not in _CAPABILITY_VALUE_ALLOWLIST}
    if write_support.capability_in(scanned):
        return True
    return "storage.googleapis.com" in json.dumps(
        scanned, ensure_ascii=False, default=str)


def _conforms(tool: str, payload) -> None:
    """Validate what structuredContent would carry against the contract —
    and scan its VALUES for a capability (lot 2A, T9)."""
    clean = tools._jsonable(payload)
    errors = tools.validate_args(OUTPUT_SCHEMAS[tool], clean)
    assert errors == [], f"{tool}: {errors}"
    assert not _capability_leak(tool, clean), f"{tool}: a capability in the payload"


def test_the_value_scan_bites_outside_its_one_allowlisted_pair():
    url = "https://storage.googleapis.com/upload/b/o?upload_id=ADPy-secret"
    assert not _capability_leak("begin_upload", {"upload_url": url})
    assert _capability_leak("create_document", {"upload_url": url})
    assert _capability_leak("begin_upload", {"note": url})
    assert _capability_leak("list_documents", {"items": [{"link": (
        "https://x/o?X-Goog-Signature=abc")}]})
    assert _capability_leak("get_note", {"a": {"storage_path": "users/x"}})
    assert not _capability_leak("get_note", {"conference_uri": "https://meet"})


def test_the_value_allowlist_is_the_name_exemption():
    from tests import test_mcp_framework_guards as guards

    assert _CAPABILITY_VALUE_ALLOWLIST == {
        (tool, key) for tool, props in guards._OUTPUT_NAME_EXEMPTIONS.items()
        for key in props
    }


# ══════════════════════════════════════════════════════════════════════
# Registry-level invariants
# ══════════════════════════════════════════════════════════════════════

def test_every_tool_declares_an_output_schema():
    assert set(OUTPUT_SCHEMAS) == set(tools.TOOLS)


@pytest.mark.parametrize("with_accounting_tool", [False, True])
def test_descriptors_ship_the_output_schema_and_title_mirror(
    monkeypatch, with_accounting_tool
):
    # Every switch ON: `list_tool_descriptors()` drops no scope but still
    # applies the kill switches, and MCP_COMPTABILITE_ENABLED defaults to
    # FALSE — so without this an accounting tool (plan lot 5) would be
    # skipped here in silence. The equality makes any other hiding loud.
    if with_accounting_tool:
        from tests import _dummy_accounting

        _dummy_accounting.register(monkeypatch)
    monkeypatch.setattr(tools, "write_enabled", lambda: True)
    monkeypatch.setattr(tools, "comptabilite_enabled", lambda: True)
    descriptors = tools.list_tool_descriptors()
    assert {d["name"] for d in descriptors} == set(tools.TOOLS)
    for d in descriptors:
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
    every tool at once, not just its own. Found by adversarial review
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

# The provenance + concurrency stamps a document carries since 2026-09-25
# (models/provenance.py). Folded into ONE branch of each reader below, so
# the schemas are validated against populated values as well as against the
# ''/null a legacy document yields.
_PROV = {"etag": "etag-7", "created_via": "web", "updated_via": "mcp",
         "mcp_updated_at": DT}


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
    # The hearing row must reach the payload — get_agenda judges each row by
    # its civil day since lot 1a, so « today » is frozen on the fixture's day
    # (without it the row is dropped and the branch goes unchecked).
    monkeypatch.setattr(handlers.deadlines, "today_mtl",
                        lambda: datetime(2026, 9, 1).date())
    payload = handlers.get_agenda({"days_ahead": 14})
    assert [h["id"] for h in payload["hearings"]] == ["h1"]
    _conforms("get_agenda", payload)


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
            closed_date=DT, **_PROV,
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
                 "pinned": False, "created_at": DT, "updated_at": DT,
                 **_PROV},
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
                   "created_at": DT, "updated_at": DT, **_PROV})
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
                        "invoice_id": "inv-1", **_PROV,
                        "created_via": "mcp"}], None))
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
        lambda nid, data, *, expected_etag=None: ({"id": nid, "dossier_id": "d1",
                            "dossier_file_number": "2026-001",
                            "dossier_title": "Tremblay", "title": "Note",
                            "category": "recherche", "created_at": DT,
                            "updated_at": DT, **data}, []))
    _conforms("append_to_note", handlers.append_to_note(
        {"note_id": "n1", "content": "Suite"}))


# ── WP16 creators: conformance ──────────────────────────────────────────


def test_create_task_conforms(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.task_model, "create_task",
        lambda data: ({**data, "id": "t-new"}, []))
    args = {"dossier_id": "d1", "title": "Produire la réponse",
            "due_date": "2026-08-05", "priority": "haute"}
    payload = handlers.create_task(dict(args))
    _conforms("create_task", payload)
    assert payload["entity"]["status"] == "à_faire"
    # The entity echoes what was STORED — it is the caller's only handle
    # on the new row.
    assert payload["entity"]["id"] == "t-new"


def test_create_hearing_conforms(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.hearing_model, "create_hearing",
        lambda data: ({**data, "id": "h-new"}, []))
    args = {"dossier_id": "d1", "title": "Interrogatoire",
            "hearing_type": "interrogatoire", "date": "2026-09-10",
            "start_time": "09:30"}
    payload = handlers.create_hearing(dict(args))
    _conforms("create_hearing", payload)
    assert payload["entity"]["forum"] == "extrajudiciaire"


def test_create_time_entry_conforms(write_world, monkeypatch):
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


def test_complete_dossier_conforms(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.dossier_model, "get_dossier",
        lambda i: _dossier_doc(domaine="", action="", valeur=None),
    )
    monkeypatch.setattr(handlers.dossier_model, "field_defaults",
                        lambda: {"domaine": "", "action": "", "valeur": None})
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data, *, expected_etag=None: ({**_dossier_doc(), **data}, []),
    )
    args = {"dossier_id": "d1", "domaine": "REC", "action": "REC-01",
            "valeur": 1190000}
    payload = handlers.complete_dossier(dict(args))
    _conforms("complete_dossier", payload)
    assert set(payload["fields_set"]) == {"domaine", "action", "valeur"}


def test_record_signification_conforms(write_world, monkeypatch):
    dossier = _dossier_doc()
    dossier["significations"] = []
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda i: dict(dossier))
    monkeypatch.setattr(
        handlers.dossier_model, "update_dossier",
        lambda did, data, *, expected_etag=None: ({**dossier, **data}, []),
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
        lambda did, data, *, expected_etag=None: ({**dossier, **data}, []),
    )
    payload = handlers.record_prescription_event({
        "dossier_id": "d1", "type": "interruption_depot",
        "date": "2026-05-15", "reference": "signification DII",
    })
    _conforms("record_prescription_event", payload)
    # The answer that motivated the call: the delay no longer runs.
    assert payload["prescription_status"] == "interrompue"
    assert payload["prescription_date_effective"] is None


def test_create_expense_conforms(write_world, monkeypatch):
    monkeypatch.setattr(
        handlers.expense_model, "create_expense",
        lambda data: ({**data, "id": "x-new"}, []))
    args = {"dossier_id": "d1", "date": "2026-07-30",
            "description": "Huissier — signification", "amount_cents": 9500,
            "category": "signification"}
    payload = handlers.create_expense(dict(args))
    _conforms("create_expense", payload)
    assert "ctag_bumped" not in payload


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
    monkeypatch.setattr(handlers.folder_model, "list_dossier_folders", lambda d: [
        {"id": "f1", "name": "Projets", "parent_folder_id": None,
         "dossier_id": "d1", "system_role": "projets", "etag": "e1"},
    ])
    monkeypatch.setattr(handlers.dossier_model, "get_dossiers_bulk", lambda ids: {})
    _conforms("list_documents", handlers.list_documents({"dossier_id": "d1"}))
    _conforms("list_documents", handlers.list_documents({"scope": "cabinet"}))
    # Lot 2A T6: the folder-tree branch, and a filed row in both scopes (its
    # role resolved in dossier scope, null across the firm).
    docs[0]["folder_id"] = "f1"
    monkeypatch.setattr(handlers.dossier_model, "get_dossier",
                        lambda d: {"id": d, "file_number": "2026-001"})
    _conforms("list_documents", handlers.list_documents(
        {"dossier_id": "d1", "include_folders": True}))
    _conforms("list_documents", handlers.list_documents({"scope": "cabinet"}))


def test_list_templates_conforms_on_every_branch(monkeypatch):
    template = {
        "id": "t1", "name": "Lettre", "description": "", "category": "autre",
        "kind": "note", "version": 2, "etag": "e1",
        "placeholders": ["client.nom_complet", "date.aujourdhui",
                         "privilège", "FAITS"],
        "validation_warnings": ["Le champ «x» semble fragmenté."],
        "active_for": "note", "active_designated_at": DT,
        "created_at": DT, "updated_at": DT,
    }
    monkeypatch.setattr(handlers.doc_template_model, "list_templates",
                        lambda **kw: [template, {"id": "t0", "name": "Legacy"}])
    _conforms("list_templates", handlers.list_templates({}))
    _conforms("list_templates", handlers.list_templates({"limit": 1}))

    monkeypatch.setattr(handlers.doc_template_model, "get_template",
                        lambda tid, strict=False: template if tid == "t1" else None)
    monkeypatch.setattr(handlers.doc_template_model, "list_versions",
                        lambda tid, limit=50, strict=False: [
                            {"version": 2, "created_at": DT, "created_via": "web",
                             "restored_from": 1, "file_size": 10},
                            {"version": 1, "created_at": None, "file_size": 9}])
    monkeypatch.setattr(handlers.gabarit_service, "cabinet_dict", lambda: {})
    _conforms("list_templates", handlers.list_templates({"template_id": "t1"}))
    _conforms("list_templates", handlers.list_templates({"template_id": "t0"}))

    def _unreadable(tid, limit=50, strict=False):
        raise handlers.doc_template_model.TemplateReadError(tid)

    monkeypatch.setattr(handlers.doc_template_model, "list_versions", _unreadable)
    _conforms("list_templates", handlers.list_templates({"template_id": "t1"}))


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


def test_partie_writes_conform(monkeypatch):
    import models

    monkeypatch.setattr(handlers, "bump_ctag", lambda n: None)
    monkeypatch.setattr(models, "find_by_legacy_ref", lambda c, r, limit=5: [])
    monkeypatch.setattr(handlers.partie_model, "create_partie",
                        lambda data: ({**data, "id": "p-new"}, []))
    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: {"id": "p1", "type": "individual",
                                   "last_name": "Tremblay"})
    # Widened (lot 0a, étape 5): the handler passes expected_etag; the stub
    # writes a NEW etag, as the model does, so the payload's one is checked.
    monkeypatch.setattr(handlers.partie_model, "update_partie",
                        lambda pid, data, *, expected_etag=None: (
                            {"id": pid, "type": "individual",
                             "last_name": "Tremblay", **data,
                             "etag": "e-ecrit"}, []))

    args = {"type": "individual", "last_name": "Tremblay",
            "first_name": "Jean", "legacy_ref": "L-42"}
    _conforms("create_partie", handlers.create_partie(dict(args)))

    upd = {"partie_id": "p1", "notes": "corrigé"}
    updated = handlers.update_partie(dict(upd))
    _conforms("update_partie", updated)
    assert updated["entity"]["etag"] == "e-ecrit"


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


def test_import_invoice_conforms(monkeypatch):
    """The result of a real import conforms — and carries NO line_preview.

    That key was the dry run's own half of the contract; the preview was
    removed on 2026-08-27, so nothing emits it any more. The schema still
    TYPES it (optional, never required), which is why the payload stays
    valid — and why this assertion is kept: it is the only thing saying
    out loud that the key is now dead surface."""
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


def test_billing_edits_conform(monkeypatch):
    entry = {"id": "e1", "dossier_id": "d1", "description": "Rédaction",
             "hours": 1.5, "rate": 30000, "amount": 45000, "billable": True,
             "invoiced": False, "phase": "CTS", "sous_phase": "CTS-02",
             "date": DT}
    disb = {"id": "x1", "dossier_id": "d1", "description": "Timbre",
            "amount": 5000, "taxable": True, "invoiced": False,
            "category": "timbre_judiciaire", "date": DT}
    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry",
                        lambda i: entry)
    # Widened (lot 0a, étape 5): the handler passes expected_etag.
    monkeypatch.setattr(handlers.time_entry_model, "update_time_entry",
                        lambda i, d, *, expected_etag=None: (
                            {**entry, **d, "etag": "e-ecrit"}, []))
    monkeypatch.setattr(handlers.expense_model, "get_expense", lambda i: disb)
    monkeypatch.setattr(handlers.expense_model, "update_expense",
                        lambda i, d, *, expected_etag=None: (
                            {**disb, **d, "etag": "e-ecrit"}, []))

    te = {"time_entry_id": "e1", "hours": 0.25}
    updated = handlers.update_time_entry(dict(te))
    _conforms("update_time_entry", updated)
    assert updated["entity"]["etag"] == "e-ecrit"

    ex = {"expense_id": "x1", "amount_cents": 5250}
    _conforms("update_expense", handlers.update_expense(dict(ex)))


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

    # Widened (lot 0a, étape 5): the handler passes expected_etag — the
    # etag it has just read. A row whose stored etag differs is answered the
    # way the model does (the concurrency refusal), and a write mints a new
    # etag, as the model's stamp does.
    def _set_time(i, p, s, *, expected_etag=None):
        doc = entries[i]
        if expected_etag is not None and expected_etag != doc.get("etag", ""):
            return None, [concurrency.STALE_ETAG_ERROR], False
        changed = (doc["phase"], doc["sous_phase"]) != (p, s)
        doc.update(phase=p, sous_phase=s)
        if changed:
            doc["etag"] = f"{doc.get('etag', '')}+1"
        return dict(doc), [], changed

    def _set_exp(i, p, s, *, expected_etag=None):
        doc = disbs[i]
        if expected_etag is not None and expected_etag != doc.get("etag", ""):
            return None, [concurrency.STALE_ETAG_ERROR], False
        changed = (doc["phase"], doc["sous_phase"]) != (p, s)
        doc.update(phase=p, sous_phase=s)
        if changed:
            doc["etag"] = f"{doc.get('etag', '')}+1"
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


def test_phase_single_conforms_applied_and_unchanged(monkeypatch):
    """Both outcomes of a single reclassification: the row that moves, then
    the replay that writes nothing at all — the property that makes a
    reclassification pass replayable."""
    _phase_world(monkeypatch)

    te = {"time_entry_id": "e1", "sous_phase": "INT-01"}
    applied = handlers.set_time_entry_phase(dict(te))
    _conforms("set_time_entry_phase", applied)
    assert applied["outcome"] == "applied"
    # The entity is built from the row AS WRITTEN: its etag is the new one.
    assert applied["entity"]["etag"] == "+1"
    unchanged = handlers.set_time_entry_phase(dict(te))
    _conforms("set_time_entry_phase", unchanged)
    assert unchanged["outcome"] == "unchanged"
    assert unchanged["entity"]["etag"] == "+1"

    ex = {"expense_id": "x1", "phase": "PRE"}
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
    live = handlers.set_time_entry_phase_bulk(
        {"entries": [dict(i) for i in args["entries"]]}
    )
    _conforms("set_time_entry_phase_bulk", live)
    assert live["applied"] == 1 and live["unchanged"] == 1
    assert live["refused"] == 2 and live["requested"] == 4

    # A row written by someone else between the handler's read and its
    # commit comes back REFUSED on its own row — the batch goes on.
    real_bulk = handlers.time_entry_model.get_time_entries_bulk
    monkeypatch.setattr(
        handlers.time_entry_model, "get_time_entries_bulk",
        lambda ids: {i: {**d, "etag": "lu-avant-la-course"}
                     for i, d in real_bulk(ids).items()},
    )
    raced = handlers.set_time_entry_phase_bulk(
        {"entries": [{"time_entry_id": "e1", "sous_phase": "PRE-01"}]}
    )
    _conforms("set_time_entry_phase_bulk", raced)
    assert raced["refused"] == 1
    assert raced["results"][0]["reason"].startswith(
        "Cette entrée de temps a été modifiée")

    _conforms("set_expense_phase_bulk", handlers.set_expense_phase_bulk(
        {"entries": [{"expense_id": "x1", "phase": "PRE"}]}
    ))


def test_dossier_writes_conform(monkeypatch):
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
    # Widened (lot 0a, étape 5): the handler passes expected_etag.
    monkeypatch.setattr(handlers.dossier_model, "update_dossier",
                        lambda did, data, *, expected_etag=None: (
                            {**existing, **data, "id": did,
                             "etag": "e-ecrit"}, []))

    args = {"file_number": "2019-014", "title": "Tremblay c. Lavoie",
            "clients": [{"partie_id": "p1", "roles": ["demandeur"]}],
            "status": "fermé"}
    _conforms("create_dossier", handlers.create_dossier(dict(args)))

    upd = {"dossier_id": "d1", "sommaire": "résumé"}
    updated = handlers.update_dossier(dict(upd))
    _conforms("update_dossier", updated)
    assert updated["entity"]["etag"] == "e-ecrit"


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
                        lambda tid, data, *, expected_etag=None: ({**task, **data}, []))
    monkeypatch.setattr(handlers.task_model, "_validate", lambda d: [])
    monkeypatch.setattr(handlers.protocol_model, "get_protocol_for_dossier",
                        lambda did, active_only=True: None)
    _conforms("complete_task", handlers.complete_task({"task_id": "t1"}))

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


def test_complete_task_already_closed_neither_bumps_nor_claims_a_sync(
        write_world, monkeypatch):
    """La seule sémantique réelle que le retrait du `dry_run` pouvait
    emporter (2026-08-27).

    Une tâche déjà dans l'état demandé n'écrit RIEN. Deux moitiés, et les
    deux comptent : aucun bump de CTag — dire à DavX5 de relire une
    collection inchangée est un mensonge, et c'est ce qui rend une tâche
    planifiée rejouable — mais les deux clés restent ÉMISES à faux, parce
    que le schéma de sortie les EXIGE. Les omettre ferait rejeter la
    réponse par un client strict, sans rien dire côté serveur : la classe
    de panne que ce fichier existe pour attraper. Le dossier est « actif »
    ici, donc `dav_synced` ne peut être faux que pour la bonne raison.
    """
    bumps: list[str] = []
    monkeypatch.setattr(handlers, "bump_ctag", lambda n: bumps.append(n))
    task = {
        "id": "t1", "title": "Produire", "description": "",
        "status": "terminée", "priority": "normale", "category": "rédaction",
        "dossier_id": "d1", "dossier_file_number": "2026-001",
        "dossier_title": "T", "due_date": None, "completed_date": None,
        "related_note_id": None,
    }
    monkeypatch.setattr(handlers.task_model, "get_task", lambda i: dict(task))
    monkeypatch.setattr(handlers.task_model, "update_task",
                        lambda tid, data, *, expected_etag=None: pytest.fail(
                            "un no-op ne doit jamais écrire"))

    payload = handlers.complete_task({"task_id": "t1"})
    _conforms("complete_task", payload)
    assert payload["already_completed"] is True
    assert bumps == []
    assert payload["ctag_bumped"] is False
    assert payload["dav_synced"] is False


# ── Lecture du contenu d'un document ────────────────────────────────────


def _pdf_bytes(pages):
    from io import BytesIO

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    for content in pages:
        if content:
            c.drawString(72, 720, content)
        c.showPage()
    c.save()
    return buffer.getvalue()


def _docx_bytes(xml):
    import zipfile
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def _doc_meta(file_type, size=1234):
    return {"id": "doc1", "display_name": "Pièce P-1",
            "file_type": file_type, "file_size": size,
            "storage_path": "users/u/x"}


def test_get_document_text_pdf_conforms(monkeypatch):
    data = _pdf_bytes(["Premier contrat", ""])  # a text page + a blank page
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta("application/pdf", len(data)))
    monkeypatch.setattr(
        handlers.document_model, "get_document_bytes",
        lambda i, **kw: (data, ""))
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["pagination_unit"] == "page"
    assert payload["pages_without_text"] == [2]
    assert payload["next_page"] is None


def test_get_document_text_docx_segments_conform(monkeypatch):
    xml = ("<w:document><w:body>"
           "<w:p><w:r><w:t>Alpha</w:t></w:r></w:p>"
           "<w:p><w:r><w:t>Beta</w:t></w:r></w:p>"
           "</w:body></w:document>")
    data = _docx_bytes(xml)
    docx_mime = ("application/vnd.openxmlformats-officedocument"
                 ".wordprocessingml.document")
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta(docx_mime, len(data)))
    monkeypatch.setattr(
        handlers.document_model, "get_document_bytes",
        lambda i, **kw: (data, ""))
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["pagination_unit"] == "segment"
    assert "Alpha" in payload["pages"][0]["text"]


def test_get_document_text_truncation_paging_conforms(monkeypatch):
    data = _pdf_bytes(["A" * 50, "B" * 50, "C" * 50])
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta("application/pdf", len(data)))
    monkeypatch.setattr(
        handlers.document_model, "get_document_bytes",
        lambda i, **kw: (data, ""))
    monkeypatch.setattr(handlers, "DOCUMENT_TEXT_MAX_CHARS", 80)
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["truncated"] is True
    assert payload["next_page"] == 3


def test_get_document_text_unreadable_branches_conform(monkeypatch):
    # too_large — refused on metadata, before any byte moves.
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta("application/pdf", 300 * 1024 * 1024))
    monkeypatch.setattr(
        handlers.document_model, "get_document_bytes",
        lambda i, **kw: (None, "too_large"))
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["reason"] == "too_large"
    assert "40" in payload["message"]
    # unsupported_type — never even reaches the byte seam.
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta("image/png"))
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["reason"] == "unsupported_type"
    # encrypted — via the real extractor on a pypdf-encrypted fixture.
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(_pdf_bytes(["secret"]))))
    writer.encrypt("mdp")
    out = BytesIO()
    writer.write(out)
    encrypted = out.getvalue()
    monkeypatch.setattr(
        handlers.document_model, "get_document",
        lambda i: _doc_meta("application/pdf", len(encrypted)))
    monkeypatch.setattr(
        handlers.document_model, "get_document_bytes",
        lambda i, **kw: (encrypted, ""))
    payload = handlers.get_document_text({"document_id": "doc1"})
    _conforms("get_document_text", payload)
    assert payload["reason"] == "encrypted"


def test_get_document_text_not_found_conforms(monkeypatch):
    monkeypatch.setattr(
        handlers.document_model, "get_document", lambda i: None)
    payload = handlers.get_document_text({"document_id": "absent"})
    _conforms("get_document_text", payload)
    assert payload["found"] is False


# ══════════════════════════════════════════════════════════════════════
# Provenance & concurrency stamps (plan lot 0a, étape 3)
# ══════════════════════════════════════════════════════════════════════

_PROV_KEYS = ("etag", "created_via", "updated_via", "mcp_updated_at")
# created_via was REQUIRED on these two rows before provenance existed (it
# read « 'mcp' or '' »); it stays required there, and nowhere else.
_PREEXISTING_REQUIRED = {
    ("list_time_entries", "created_via"), ("list_expenses", "created_via"),
}


def _objects(node, path=""):
    """Every object schema in *node*, with a readable path."""
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            yield path, node
            for key, sub in node["properties"].items():
                yield from _objects(sub, f"{path}.{key}")
        for key in ("items", "anyOf", "oneOf", "allOf"):
            sub = node.get(key)
            if isinstance(sub, dict):
                yield from _objects(sub, f"{path}[{key}]")
            elif isinstance(sub, list):
                for i, branch in enumerate(sub):
                    yield from _objects(branch, f"{path}[{key}{i}]")


def test_provenance_keys_are_never_auto_required():
    """Added to existing contracts, they must never become a key a strict
    client can reject a response over — nor one a replayed write result
    stored before 2026-09-25 (the 24 h idempotency cache) would lack."""
    offenders = []
    for tool, schema in OUTPUT_SCHEMAS.items():
        for path, obj in _objects(schema, tool):
            for key in obj.get("required", []):
                if key in _PROV_KEYS and (tool, key) not in _PREEXISTING_REQUIRED:
                    offenders.append(f"{path}: {key}")
    assert not offenders, offenders


def test_the_preexisting_created_via_stays_required():
    for tool in ("list_time_entries", "list_expenses"):
        row = OUTPUT_SCHEMAS[tool]["properties"]["items"]["items"]
        assert "created_via" in row["required"], tool
        assert not set(row["required"]) & {"etag", "updated_via",
                                           "mcp_updated_at"}, tool


def test_every_timestamped_object_says_how_it_was_written():
    """An object that says WHEN it was written also says by which path, and
    carries the token an edit would pass back as expected_etag."""
    missing = []
    seen = 0
    for tool, schema in OUTPUT_SCHEMAS.items():
        for path, obj in _objects(schema, tool):
            props = obj["properties"]
            if "created_at" in props or "updated_at" in props:
                seen += 1
                lacking = [k for k in _PROV_KEYS if k not in props]
                if lacking:
                    missing.append(f"{path}: {lacking}")
    assert not missing, missing
    assert seen >= 15, seen  # not vacuous


def _dossier_world(monkeypatch, doc):
    for model, summary in (
        (handlers.hearing_model, "get_hearing_summary"),
        (handlers.note_model, "get_notes_summary"),
        (handlers.document_model, "get_document_summary"),
    ):
        monkeypatch.setattr(model, summary, lambda d: {"total": 1})
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
    monkeypatch.setattr(handlers.dossier_model, "get_dossier", lambda i: doc)


def _stamped(record: dict) -> dict:
    return {k: record[k] for k in _PROV_KEYS}


_EMITTED = {"etag": "etag-7", "created_via": "web", "updated_via": "mcp",
            "mcp_updated_at": tools.iso_mtl(DT)}
_LEGACY = {"etag": "", "created_via": "", "updated_via": "",
           "mcp_updated_at": None}
_BOTH_STATES = pytest.mark.parametrize(
    "stored, emitted", [(_PROV, _EMITTED), ({}, _LEGACY)],
    ids=["stamped", "legacy"],
)


@_BOTH_STATES
def test_get_dossier_and_get_partie_emit_their_etag_and_provenance(
    monkeypatch, stored, emitted,
):
    """The two records update_dossier / update_partie edit bypassed
    _stamps: a model reading them got no etag to pass back."""
    _dossier_world(monkeypatch, _dossier_doc(**stored))
    payload = handlers.get_dossier({"dossier_id": "d1"})
    _conforms("get_dossier", payload)
    assert _stamped(payload["dossier"]) == emitted

    monkeypatch.setattr(handlers.partie_model, "get_partie",
                        lambda i: {**_partie_doc(), **stored})
    monkeypatch.setattr(handlers.dossier_model, "list_dossiers_for_partie",
                        lambda i: [])
    payload = handlers.get_partie({"partie_id": "p1"})
    _conforms("get_partie", payload)
    assert _stamped(payload["partie"]) == emitted


@_BOTH_STATES
def test_note_readers_emit_etag_and_provenance(monkeypatch, stored, emitted):
    note = {"id": "n1", "dossier_id": "", "title": "Veille",
            "content": "Texte", "category": "recherche", "pinned": False,
            "created_at": DT, "updated_at": DT, **stored}
    monkeypatch.setattr(handlers.note_model, "get_note", lambda i: dict(note))
    monkeypatch.setattr(handlers.note_model, "list_notes",
                        lambda **kw: [dict(note)])
    payload = handlers.get_note({"note_id": "n1"})
    _conforms("get_note", payload)
    assert _stamped(payload["note"]) == emitted
    payload = handlers.list_notes({})
    _conforms("list_notes", payload)
    assert _stamped(payload["items"][0]) == emitted


@_BOTH_STATES
def test_the_note_write_result_carries_the_written_stamps(
    write_world, monkeypatch, stored, emitted,
):
    """The note write payload bypassed _stamps too. (That the etag emitted
    is the one STORED is proven end to end over the real model in
    tests/test_provenance.py; here, the contract.)"""
    monkeypatch.setattr(
        handlers.note_model, "create_note",
        lambda data: ({**data, "id": "n-new", "created_at": DT,
                       "updated_at": DT, **stored}, []))
    payload = handlers.create_note({"dossier_id": "d1", "title": "T",
                                    "content": "Corps."})
    _conforms("create_note", payload)
    assert _stamped(payload["note"]) == emitted


@pytest.mark.parametrize("stored, expected", [
    ("mcp", "mcp"),   # the old meaning survives: this connector recorded it
    (None, ""),       # a legacy row recorded in the application
    ("web", "web"),   # the widened vocabulary
])
def test_billing_rows_keep_the_created_via_contract(monkeypatch, stored, expected):
    row = {"id": "e1", "dossier_id": "d1", "date": DATE_ONLY,
           "description": "R", "hours": 1.0, "rate": 30000, "amount": 30000,
           "billable": True, "invoiced": False}
    if stored is not None:
        row["created_via"] = stored
    monkeypatch.setattr(handlers.time_entry_model, "list_time_entries_page",
                        lambda **kw: ([row], None))
    payload = handlers.list_time_entries({})
    _conforms("list_time_entries", payload)
    assert payload["items"][0]["created_via"] == expected


def test_billing_created_via_description_documents_both_vocabularies():
    for tool in ("list_time_entries", "list_expenses"):
        text = (OUTPUT_SCHEMAS[tool]["properties"]["items"]["items"]
                ["properties"]["created_via"]["description"])
        assert "web | dav | mcp | cron | script" in text, tool
        assert "« mcp »" in text and "''" in text, tool


_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def test_provenance_texts_name_no_boundary_date():
    """The line between « path not recorded » and « path recorded » is the
    day provenance was DEPLOYED, which no commit can know: a date in these
    texts (the first draft said « before 2026-09-25 ») is false for every
    record written between that day and the deploy."""
    offenders = []
    for tool, schema in OUTPUT_SCHEMAS.items():
        for path, obj in _objects(schema, tool):
            for key in _PROV_KEYS:
                text = obj["properties"].get(key, {}).get("description", "")
                if _ISO_DATE.search(text):
                    offenders.append(f"{path}.{key}: {text}")
    assert not offenders, offenders


def _step_row_objects():
    """Every protocol-step row, found by the one key only a step row has."""
    for tool, schema in OUTPUT_SCHEMAS.items():
        for path, obj in _objects(schema, tool):
            if "status_stored" in obj["properties"]:
                yield path, obj


def test_step_rows_name_the_overdue_exception_and_nothing_else_does():
    """Steps are stamped since lot 1a, so a step row's provenance keys read
    like every other row's — with ONE difference, stated on step rows alone:
    the step etag does not move on the deadline-derived « en_retard » stamp
    a protocol page view writes (a page view must not invalidate the etag a
    caller holds). The texts that said « not stamped yet » are gone: they
    would now be false for every step written since."""
    rows = list(_step_row_objects())
    assert len(rows) >= 2, rows  # list_protocol_steps + get_agenda
    for path, obj in rows:
        assert "« en_retard »" in obj["properties"]["etag"]["description"], path
        for key in _PROV_KEYS:
            text = obj["properties"][key]["description"]
            assert "not stamped yet" not in text, (path, key)
    step_paths = {path for path, _ in rows}
    for tool, schema in OUTPUT_SCHEMAS.items():
        for path, obj in _objects(schema, tool):
            if path in step_paths or "etag" not in obj["properties"]:
                continue
            text = obj["properties"]["etag"].get("description", "")
            assert "« en_retard »" not in text, (path, "etag")


def test_steps_are_stamped_and_their_rows_emit_it(monkeypatch):
    """A step written through the real model (shared fake Firestore) under
    the connector stores its etag and provenance — its PROTOCOL is stamped
    too — and its row emits them. The one write that does NOT move the step
    etag is the overdue stamp of a page view."""
    from datetime import date

    from models import provenance
    from tests._fake_firestore import install

    protocol_model = handlers.protocol_model
    fake = install(monkeypatch, protocol_model)
    fake.seed("protocols/p1", {
        "id": "p1", "dossier_id": "d1", "title": "Protocole",
        "protocol_type": "conventionnel", "status": "actif",
        "etag": "e0", "created_at": DT, "updated_at": DT,
    })
    fake.seed("protocols/p1/steps/s1", {
        **protocol_model._default_step(), "id": "s1", "order": 1,
        "title": "Interrogatoire", "deadline_date": DATE_ONLY,
        "created_at": DT, "updated_at": DT, "etag": "se0",
    })
    with provenance.writing_via("mcp", tool="t"):
        _, errors = protocol_model.update_step("p1", "s1", {"notes": "Suivi."})
    assert errors == []

    step = fake.peek("protocols/p1/steps/s1")
    assert step["notes"] == "Suivi."  # the write did land
    assert step["etag"] not in ("", "se0")
    assert step["updated_via"] == "mcp"
    assert step["mcp_updated_at"] is not None
    assert fake.peek("protocols/p1")["updated_via"] == "mcp"

    row = handlers._step_row(step, date(2026, 9, 25))
    assert row["etag"] == step["etag"]
    assert row["updated_via"] == "mcp"
    assert row["mcp_updated_at"] == tools.iso_mtl(step["mcp_updated_at"])

    # The overdue stamp of a page view: status moves, the etag does not.
    etag = step["etag"]
    assert protocol_model.check_overdue_steps("p1") == 1  # DATE_ONLY is past
    stored = fake.peek("protocols/p1/steps/s1")
    assert stored["status"] == "en_retard" and stored["etag"] == etag


# ══════════════════════════════════════════════════════════════════════
# Lot 1b — the agenda edits: one real-handler run per shape they emit
# ══════════════════════════════════════════════════════════════════════
#
# Run on the shared fake Firestore with the REAL models underneath, so the
# payload validated is the one a real write produces — the relocation
# branch (previous_collection_cleared), the no-op branch (changed_fields
# empty, ctag_bumped false), the reopen cascade, the revision id, and the
# théorie's five modes.


def _agenda_world(monkeypatch):
    import sys

    from tests._fake_firestore import install

    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n in ("dav.sync",
                                                    "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
    for did in ("d1", "d2"):
        fake.seed(f"dossiers/{did}", {"id": did, "file_number": f"2026-{did}",
                                      "title": "T", "status": "actif"})
        fake.seed(f"dav_sync/dossier:{did}", {"ctag": "c0", "sync_token": "c0"})
    fake.seed("dav_sync/general", {"ctag": "g0", "sync_token": "g0"})
    return fake


def _agenda_task(fake, status="à_faire", **over):
    doc = {
        "id": "t1", "dossier_id": "d1", "dossier_file_number": "2026-d1",
        "dossier_title": "T", "title": "Produire", "description": "",
        "priority": "normale", "status": status, "due_date": DATE_ONLY,
        "completed_date": DT if status == "terminée" else None,
        "category": "rédaction", "phase": "", "sous_phase": "",
        "related_note_id": None, "vtodo_uid": "u", "etag": "e-t1",
        "created_at": DT, "updated_at": DT,
    }
    doc.update(over)
    fake.seed("tasks/t1", doc)


def test_update_task_conforms_on_write_noop_and_move(monkeypatch):
    fake = _agenda_world(monkeypatch)
    _agenda_task(fake)
    written = handlers.update_task({"task_id": "t1", "title": "Réviser",
                                    "expected_etag": "e-t1"})
    _conforms("update_task", written)
    assert written["changed_fields"] == ["title"]

    noop = handlers.update_task({"task_id": "t1", "title": "Réviser"})
    _conforms("update_task", noop)
    assert noop["changed_fields"] == [] and noop["ctag_bumped"] is False

    moved = handlers.update_task({"task_id": "t1", "dossier_id": "d2"})
    _conforms("update_task", moved)
    assert moved["moved"] is True and moved["previous_collection_cleared"] is True


def test_reopen_task_conforms_on_cascade_and_already_open(monkeypatch):
    fake = _agenda_world(monkeypatch)
    _agenda_task(fake, status="terminée")
    fake.seed("protocols/p1", {
        "id": "p1", "dossier_id": "d1", "title": "P",
        "protocol_type": "conventionnel", "status": "complété",
        "closed_by": "auto", "closed_at": DT, "etag": "pe",
        "created_at": DT, "updated_at": DT,
    })
    fake.seed("protocols/p1/steps/s1", {
        **handlers.protocol_model._default_step(), "id": "s1", "order": 1,
        "title": "Réponse", "status": "complété", "linked_task_id": "t1",
        "deadline_date": DATE_ONLY, "etag": "se", "created_at": DT,
        "updated_at": DT,
    })
    reopened = handlers.reopen_task({"task_id": "t1"})
    _conforms("reopen_task", reopened)
    assert reopened["protocol_step_effect"]["protocol_reopened"] is True

    already = handlers.reopen_task({"task_id": "t1"})
    _conforms("reopen_task", already)
    assert already["already_open"] is True


def test_update_note_conforms_on_revision_noop_and_move(monkeypatch):
    fake = _agenda_world(monkeypatch)
    fake.seed("notes/n1", {
        "id": "n1", "dossier_id": "d1", "dossier_file_number": "2026-d1",
        "dossier_title": "T", "title": "Note", "content": "Corps",
        "category": "recherche", "pinned": False, "dateless": False,
        "is_analyse": False, "vjournal_uid": "v", "etag": "e-n1",
        "created_at": DT, "updated_at": DT,
    })
    fake.seed("tasks/t9", {"id": "t9", "dossier_id": "d1", "title": "Lié",
                           "related_note_id": "n1", "status": "à_faire",
                           "etag": "e", "created_at": DT, "updated_at": DT})
    revised = handlers.update_note({"note_id": "n1", "content": "Autre",
                                    "expected_etag": "e-n1"})
    _conforms("update_note", revised)
    assert revised["revision_id"]

    noop = handlers.update_note({"note_id": "n1", "category": "recherche"})
    _conforms("update_note", noop)
    assert noop["changed_fields"] == [] and noop["revision_id"] is None

    moved = handlers.update_note({"note_id": "n1", "dossier_id": ""})
    _conforms("update_note", moved)
    assert moved["linked_tasks_left_behind"] == 1


def test_edit_analyse_conforms_in_every_mode(monkeypatch):
    fake = _agenda_world(monkeypatch)
    created = handlers.edit_analyse({"dossier_id": "d1"})
    _conforms("edit_analyse", created)
    found = handlers.edit_analyse({"dossier_id": "d1"})
    _conforms("edit_analyse", found)
    op = [{"bloc": "B", "mode": "replace", "content": "Les faits."}]
    blocs = handlers.edit_analyse({"dossier_id": "d1", "operations": op,
                                   "expected_etag": found["entity"]["etag"]})
    _conforms("edit_analyse", blocs)
    unchanged = handlers.edit_analyse({
        "dossier_id": "d1", "operations": op,
        "expected_etag": blocs["entity"]["etag"]})
    _conforms("edit_analyse", unchanged)
    full = handlers.edit_analyse({
        "dossier_id": "d1", "expected_etag": blocs["entity"]["etag"],
        "full": "\n\n".join(f"## Bloc {x}\n\nCorps." for x in "ABCDEFGH")})
    _conforms("edit_analyse", full)
    assert [p["mode"] for p in (created, found, blocs, unchanged, full)] == [
        "created", "found", "blocs", "unchanged", "full"]
    # A write result never echoes the note's text — not even a heading.
    assert "heading" not in full["structure"]["entete"]
    assert fake.peek(f"notes/{full['entity']['id']}")["is_analyse"] is True


def test_get_note_and_get_dossier_conform_with_the_theorie(monkeypatch):
    fake = _agenda_world(monkeypatch)
    for model, name in ((handlers.time_entry_model, "get_time_summary"),
                        (handlers.expense_model, "get_expense_summary"),
                        (handlers.invoice_model, "get_invoice_summary"),
                        (handlers.document_model, "get_document_summary")):
        monkeypatch.setattr(model, name, lambda d: {})
    absent = handlers.get_dossier({"dossier_id": "d1"})
    _conforms("get_dossier", absent)
    assert absent["dossier"]["analyse_note_state"] == "absent"

    note = handlers.edit_analyse({"dossier_id": "d1"})["entity"]
    present = handlers.get_dossier({"dossier_id": "d1"})
    _conforms("get_dossier", present)
    assert present["dossier"]["analyse_note_id"] == note["id"]

    read = handlers.get_note({"note_id": note["id"]})
    _conforms("get_note", read)
    assert read["note"]["structure"]["ok"] is True
    assert fake.peek(f"notes/{note['id']}") is not None


# ══════════════════════════════════════════════════════════════════════
# Lot 1b (L6) — the protocol writes: one real-handler run per shape
# ══════════════════════════════════════════════════════════════════════
#
# Real models, real service, real run_write, on the shared fake store: a
# creation with and without its linked tasks, an edit that recomputes and
# one that changes nothing, a step added with and without its task, a
# step's fields (with a linked task carried along), its status (the
# cascade that closes the protocol) and the state it already has.


def _protocol_world(monkeypatch):
    fake = _agenda_world(monkeypatch)
    fake.seed("protocols/p1", {
        "id": "p1", "dossier_id": "d1", "dossier_file_number": "2026-d1",
        "dossier_title": "T", "title": "Protocole",
        "protocol_type": "conventionnel", "status": "actif",
        "start_date": DATE_ONLY, "end_date": DATE_ONLY, "court": "",
        "notes": "", "etag": "pe", "created_at": DT, "updated_at": DT,
    })
    return fake


def _protocol_step(fake, sid, *, order=1, status="à_venir", task=None,
                   deadline=None):
    fake.seed(f"protocols/p1/steps/{sid}", {
        **handlers.protocol_model._default_step(), "id": sid, "order": order,
        "title": f"Étape {sid}", "deadline_date": deadline or DATE_ONLY,
        "status": status, "linked_task_id": task,
        "created_at": DT, "updated_at": DT, "etag": f"se-{sid}",
    })


def test_create_protocol_conforms_with_and_without_tasks(monkeypatch):
    _agenda_world(monkeypatch)
    plain = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01"})
    _conforms("create_protocol", plain)
    handlers.update_protocol({"protocol_id": plain["entity"]["id"],
                              "status": "suspendu"})
    tasks = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cs_ordinaire",
        "start_date": "2026-09-01", "create_linked_tasks": True})
    _conforms("create_protocol", tasks)
    assert tasks["tasks_created"] and tasks["ctag_bumped"] is True
    empty = handlers.create_protocol({
        "dossier_id": "d2", "protocol_type": "conventionnel",
        "start_date": "2026-09-01"})
    _conforms("create_protocol", empty)
    assert empty["steps"] == []


def test_update_protocol_conforms_on_recompute_and_noop(monkeypatch):
    fake = _agenda_world(monkeypatch)
    created = handlers.create_protocol({
        "dossier_id": "d1", "protocol_type": "cq_simplifié",
        "start_date": "2026-09-01", "create_linked_tasks": True})
    pid = created["entity"]["id"]
    handlers.update_protocol_step({"protocol_id": pid,
                                   "step_id": created["steps"][0]["id"],
                                   "status": "complété"})
    moved = handlers.update_protocol({"protocol_id": pid,
                                      "start_date": "2026-10-05"})
    _conforms("update_protocol", moved)
    assert moved["recompute"]["moved"] and moved["recompute"]["preserved"]
    noop = handlers.update_protocol({"protocol_id": pid,
                                     "start_date": "2026-10-05"})
    _conforms("update_protocol", noop)
    assert noop["changed_fields"] == []
    closed = handlers.update_protocol({"protocol_id": pid,
                                       "status": "complété"})
    _conforms("update_protocol", closed)
    assert fake.peek(f"protocols/{pid}")["closed_by"] == "mcp"


def test_add_protocol_step_conforms_with_and_without_its_task(monkeypatch):
    _protocol_world(monkeypatch)
    plain = handlers.add_protocol_step({"protocol_id": "p1",
                                        "title": "Plaidoirie"})
    _conforms("add_protocol_step", plain)
    linked = handlers.add_protocol_step({
        "protocol_id": "p1", "title": "Expertise",
        "deadline_date": "2099-06-01", "create_linked_task": True})
    _conforms("add_protocol_step", linked)
    assert linked["linked_task_id"] and linked["ctag_bumped"] is True


def test_update_protocol_step_conforms_on_fields_status_and_noop(monkeypatch):
    fake = _protocol_world(monkeypatch)
    old = datetime(2099, 11, 2, tzinfo=UTC)
    _protocol_step(fake, "s1", task="t1", deadline=old)
    _protocol_step(fake, "s2", order=2)
    _agenda_task(fake, due_date=old)
    fields = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s1", "deadline_date": "2099-11-20",
        "notes": "Suivi."})
    _conforms("update_protocol_step", fields)
    assert fields["linked_task"]["outcome"] == "aligned"
    noop = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s1", "notes": "Suivi."})
    _conforms("update_protocol_step", noop)
    already = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s1", "status": "à_venir"})
    _conforms("update_protocol_step", already)
    first = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s1", "status": "complété"})
    _conforms("update_protocol_step", first)
    closing = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s2", "status": "complété"})
    _conforms("update_protocol_step", closing)
    assert closing["status_change"]["protocol_closed"] is True
    reopened = handlers.update_protocol_step({
        "protocol_id": "p1", "step_id": "s2", "status": "à_venir"})
    _conforms("update_protocol_step", reopened)
    assert reopened["status_change"]["protocol_reopened"] is True


def test_list_protocol_steps_conforms_with_its_etags_and_closure(monkeypatch):
    fake = _protocol_world(monkeypatch)
    _protocol_step(fake, "s1")
    fake.seed("protocols/p1", {**fake.peek("protocols/p1"),
                               "status": "complété", "closed_by": "auto",
                               "closed_at": DT})
    payload = handlers.list_protocol_steps({"dossier_id": "d1",
                                            "include_history": True})
    _conforms("list_protocol_steps", payload)
    proto = payload["protocols"][0]
    assert proto["closed_by"] == "auto" and proto["etag"] == "pe"
    assert proto["steps"][0]["deadline_offset_days"] is None


# ══════════════════════════════════════════════════════════════════════
# Lot 1b (L7) — the calendar: one real-handler run per shape emitted
# ══════════════════════════════════════════════════════════════════════
#
# On the shared fake Firestore with the REAL models and the REAL
# services/rendez_vous underneath; only Graph is replaced. Each shape the
# three new tools emit is validated — a write, a no-op, a move, a
# confirmed Bookings rendez-vous (its notes and its dossier, the only two
# things D10 leaves editable), a series, a confirmation, a refusal, and
# the warned success after a cancellation the local write could not
# follow — plus create_hearing's added keys and list_hearings' two modes.

L7_START = datetime(2026, 10, 15, 13, tzinfo=UTC)   # 09:00 Montréal


def _calendar_world(monkeypatch):
    fake = _agenda_world(monkeypatch)
    from services import rendez_vous

    monkeypatch.setattr(rendez_vous.Config, "bookings_configured", lambda: True)
    monkeypatch.setattr(rendez_vous.graph_calendrier, "annuler_reservation",
                        lambda gid, motif="": None)
    monkeypatch.setattr(handlers.deadlines, "today_mtl",
                        lambda: datetime(2026, 10, 1).date())
    return fake


def _l7_hearing(fake, hid="h1", **over):
    doc = {
        "id": hid, "title": "Rencontre", "hearing_type": "rencontre",
        "dossier_id": "d1", "dossier_file_number": "2026-d1",
        "dossier_title": "T", "start_datetime": L7_START,
        "end_datetime": L7_START.replace(hour=14), "all_day": False,
        "notes": "", "reminder_minutes": 1440, "status": "confirmée",
        "modalite": "présentiel", "conference_uri": "", "source": "",
        "confirmation": "", "serie_id": "", "serie_rule": None,
        "vevent_uid": f"u-{hid}", "etag": f"e-{hid}",
        "created_at": DT, "updated_at": DT,
    }
    doc.update(over)
    fake.seed(f"hearings/{hid}", doc)


def _l7_booking(fake, hid="b1", confirmation="à_confirmer"):
    _l7_hearing(fake, hid, dossier_id="", dossier_file_number="",
                dossier_title="", title="Consultation",
                hearing_type="consultation", source="bookings",
                confirmation=confirmation, graph_event_id=f"EVT-{hid}",
                client_email="client@ex.com", client_nom="Jean Tremblay")


def test_create_hearing_with_the_new_fields_conforms(monkeypatch):
    _calendar_world(monkeypatch)
    payload = handlers.create_hearing({
        "dossier_id": "d1", "title": "Audience", "hearing_type": "audience",
        "date": "2026-11-03", "start_time": "09:30",
        "modalite": "visioconférence", "conference_uri": "https://ex.com/v",
        "reminder_minutes": 60, "status": "confirmée"})
    _conforms("create_hearing", payload)
    assert payload["entity"]["status"] == "confirmée"


def test_update_hearing_conforms_on_write_noop_move_and_bookings(monkeypatch):
    fake = _calendar_world(monkeypatch)
    _l7_hearing(fake)
    written = handlers.update_hearing({"hearing_id": "h1",
                                       "date": "2026-11-06",
                                       "expected_etag": "e-h1"})
    _conforms("update_hearing", written)
    noop = handlers.update_hearing({"hearing_id": "h1",
                                    "title": "Rencontre"})
    _conforms("update_hearing", noop)
    assert noop["changed_fields"] == [] and noop["ctag_bumped"] is False
    moved = handlers.update_hearing({"hearing_id": "h1", "dossier_id": "d2",
                                     "status": "annulée"})
    _conforms("update_hearing", moved)
    assert moved["moved"] is True and moved["outlook_mirror"] == "removed"
    _l7_hearing(fake, "h2", serie_id="s1")
    detached = handlers.update_hearing({"hearing_id": "h2",
                                        "detach_from_series": True})
    _conforms("update_hearing", detached)
    # D10 (2026-09-27): a confirmed Bookings rendez-vous takes only its
    # notes and its dossier — both shapes validated, the move included.
    _l7_booking(fake, "b1", confirmation="")
    booking = handlers.update_hearing({"hearing_id": "b1",
                                       "notes_append": "Salle 2.08."})
    _conforms("update_hearing", booking)
    assert booking["outlook_mirror"] == "not_mirrored"
    filed = handlers.update_hearing({"hearing_id": "b1", "dossier_id": "d1"})
    _conforms("update_hearing", filed)
    assert filed["moved"] is True and filed["outlook_mirror"] == "not_mirrored"


def test_create_hearing_series_conforms_timed_and_all_day(monkeypatch):
    _calendar_world(monkeypatch)
    timed = handlers.create_hearing_series({
        "dossier_id": "d1", "title": "Suivi", "date": "2026-10-26",
        "start_time": "09:00", "frequency": "hebdomadaire", "count": 3,
        "idempotency_key": "cle-serie-conf-1"})
    _conforms("create_hearing_series", timed)
    all_day = handlers.create_hearing_series({
        "title": "Revue", "date": "2026-10-26", "frequency": "mensuelle",
        "until": "2026-12-31", "idempotency_key": "cle-serie-conf-2"})
    _conforms("create_hearing_series", all_day)
    assert all_day["entity"]["all_day"] is True


def test_decide_rendez_vous_conforms_on_every_outcome(monkeypatch):
    fake = _calendar_world(monkeypatch)
    _l7_booking(fake, "b1")
    confirmed = handlers.decide_rendez_vous({
        "hearing_id": "b1", "action": "confirmer", "lier_partie": False,
        "expected_etag": "e-b1", "idempotency_key": "cle-rdv-conf-1"})
    _conforms("decide_rendez_vous", confirmed)
    assert confirmed["ctag_bumped"] is True
    already = handlers.decide_rendez_vous({
        "hearing_id": "b1", "action": "confirmer", "lier_partie": False,
        "expected_etag": "e-b1", "idempotency_key": "cle-rdv-conf-2"})
    _conforms("decide_rendez_vous", already)
    assert already["changed"] is False
    _l7_booking(fake, "b2")
    refused = handlers.decide_rendez_vous({
        "hearing_id": "b2", "action": "refuser",
        "expected_etag": "e-b2", "idempotency_key": "cle-rdv-conf-3"})
    _conforms("decide_rendez_vous", refused)
    assert refused["cancellation_message"]

    # The warned success: Outlook cancelled, the local write failed twice.
    from google.api_core import exceptions as gexc

    _l7_booking(fake, "b3")

    def _fail(info):
        if any(p == "hearings/b3" for _k, p in info.ops):
            raise gexc.ServiceUnavailable("injected")

    fake.add_commit_hook(_fail)
    warned = handlers.decide_rendez_vous({
        "hearing_id": "b3", "action": "refuser",
        "expected_etag": "e-b3", "idempotency_key": "cle-rdv-conf-4"})
    _conforms("decide_rendez_vous", warned)
    assert warned["local_written"] is False and warned["graph_cancelled"]


def test_list_hearings_conforms_in_its_two_selection_modes(monkeypatch):
    fake = _calendar_world(monkeypatch)
    _l7_booking(fake, "b1")
    pending = handlers.list_hearings({"bookings": "pending"})
    _conforms("list_hearings", pending)
    assert pending["items"][0]["client_nom"] == "Jean Tremblay"
    _l7_hearing(fake, "h1", serie_id="s1")
    serie = handlers.list_hearings({"serie_id": "s1"})
    _conforms("list_hearings", serie)
    assert serie["mode"] == "serie" and serie["window"]["from"] is None


# ══════════════════════════════════════════════════════════════════════
# Lot 2A (T7) — FILES: update_document, move_documents, manage_folder,
# every outcome run through the REAL handler on the shared fake store
# ══════════════════════════════════════════════════════════════════════


def _files_world(monkeypatch):
    import sys

    from models import document as document_model
    from models import folder as folder_model
    from tests._fake_firestore import install

    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n in ("dav.sync",
                                                    "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "T", "status": "actif"})
    base = {"dossier_id": "d1", "order": 0, "created_at": DT,
            "updated_at": DT, "system_role": ""}
    fake.seed("folders/f1", {**base, "id": "f1", "name": "Pièces",
                             "parent_folder_id": None, "etag": "e-f1"})
    fake.seed("folders/f2", {**base, "id": "f2", "name": "Expertises",
                             "parent_folder_id": "f1", "etag": "e-f2"})
    projets = folder_model.system_folder_id("d1", "projets")
    fake.seed(f"folders/{projets}", {**base, "id": projets, "name": "Projets",
                                     "parent_folder_id": None,
                                     "system_role": "projets", "etag": "e-p"})
    for did, folder in (("a", None), ("b", "f1")):
        fake.seed(f"documents/{did}", {
            **document_model._default_doc(), "id": did, "dossier_id": "d1",
            "display_name": f"Pièce {did}", "category": "autre",
            "folder_id": folder, "created_at": DT, "updated_at": DT,
            "etag": f"e-{did}"})
    return fake, projets


def test_update_document_conforms_on_write_presumed_category_and_noop(monkeypatch):
    _files_world(monkeypatch)
    written = handlers.update_document({
        "document_id": "a", "display_name": "Rapport", "tags": ["x"],
        "document_date": "2026-02-10", "folder_id": "f2",
        "expected_etag": "e-a"})
    _conforms("update_document", written)
    assert written["changed_fields"] == [
        "display_name", "document_date", "tags", "folder_id"]

    presumed = handlers.update_document({"document_id": "a",
                                         "category": "preuve"})
    _conforms("update_document", presumed)
    assert presumed["entity"]["category_presumee"] is True

    noop = handlers.update_document({"document_id": "a",
                                     "display_name": "Rapport"})
    _conforms("update_document", noop)
    assert noop["changed_fields"] == []


def test_move_documents_conforms_on_every_row_outcome(monkeypatch):
    _fake, projets = _files_world(monkeypatch)
    mixed = handlers.move_documents({
        "dossier_id": "d1", "document_ids": ["a", "b", "inconnu"],
        "folder_id": "f1"})
    _conforms("move_documents", mixed)
    assert {r["outcome"] for r in mixed["results"]} == {
        "moved", "unchanged", "refused"}
    to_system = handlers.move_documents({
        "dossier_id": "d1", "document_ids": ["a"], "folder_id": projets})
    _conforms("move_documents", to_system)
    to_root = handlers.move_documents({
        "dossier_id": "d1", "document_ids": ["a", "b"], "folder_id": ""})
    _conforms("move_documents", to_root)
    assert to_root["target"]["folder_id"] is None


def test_manage_folder_conforms_on_every_outcome(monkeypatch):
    _files_world(monkeypatch)
    created = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                      "name": "Correspondance"})
    _conforms("manage_folder", created)
    reused = handlers.manage_folder({"action": "create", "dossier_id": "d1",
                                     "name": "Pièces", "if_exists": "reuse"})
    _conforms("manage_folder", reused)
    renamed = handlers.manage_folder({"action": "rename", "dossier_id": "d1",
                                      "folder_id": "f2", "name": "Experts"})
    _conforms("manage_folder", renamed)
    moved = handlers.manage_folder({"action": "move", "dossier_id": "d1",
                                    "folder_id": "f2", "parent_folder_id": ""})
    _conforms("manage_folder", moved)
    unchanged = handlers.manage_folder({"action": "move", "dossier_id": "d1",
                                        "folder_id": "f2",
                                        "parent_folder_id": ""})
    _conforms("manage_folder", unchanged)
    assert [r["outcome"] for r in (created, reused, renamed, moved, unchanged)] == [
        "created", "reused", "renamed", "moved", "unchanged"]


# ══════════════════════════════════════════════════════════════════════
# Lot 2A (T8) — FILES: fill_gabarit, create_document (markdown, copy —
# with and without an inherited protection), every outcome run through the
# REAL handler on the shared fake store and the fake Cloud Storage
# ══════════════════════════════════════════════════════════════════════


def _generation_world(monkeypatch):
    import io
    import sys
    import zipfile

    from models import doc_template as tpl_model
    from models import document as document_model
    from tests._fake_firestore import install
    from tests._fake_gcs import FakeBucket
    from utils import storage_identity

    modules = [m for n, m in sorted(sys.modules.items())
               if (n.startswith("models.") or n in ("dav.sync",
                                                    "mcp.write_support"))
               and getattr(m, "db", None) is not None]
    fake = install(monkeypatch, *modules)
    bucket = FakeBucket()
    monkeypatch.setattr(tpl_model.storage, "bucket", lambda: bucket)
    monkeypatch.setattr(storage_identity, "owner_uid", lambda: "uid-conformance")
    fake.seed("dossiers/d1", {"id": "d1", "file_number": "2026-001",
                              "title": "T", "status": "actif",
                              "clients": [], "client_ids": [],
                              "opposing_parties": [], "opposing_party_ids": []})
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
          '/package/2006/content-types"><Default Extension="xml" '
          'ContentType="application/xml"/></Types>')

    def docx(names):
        body = "".join(f"<w:p><w:r><w:t>{{{{{n}}}}}</w:t></w:r></w:p>"
                       for n in names)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", ct)
            zf.writestr("word/document.xml",
                        f'<?xml version="1.0"?><w:document {w}><w:body>{body}'
                        "</w:body></w:document>")
        return buf.getvalue()

    ids = {}
    for key, names, kind in (
        ("gabarit", ["dossier.titre", "objet_lettre", "FAITS", "civilité"],
         "gabarit"),
        ("note", ["note.titre", "note.contenu"], "note"),
    ):
        data = docx(names)
        tpl, errors = tpl_model.create_template(
            io.BytesIO(data), f"{key}.docx", len(data),
            {"name": key, "category": "autre", "kind": kind}, "uid-conformance")
        assert errors == [], errors
        ids[key] = tpl["id"]
    _d, errors = tpl_model.set_active_template(ids["note"], par="j",
                                               expected_etag=None)
    assert errors == []
    source = docx(["x"])
    path = "users/uid-conformance/dossiers/d1/documents/src/lettre.docx"
    bucket.put(path, source)
    mime = document_model.EXTENSION_MIME_TYPES[".docx"]
    base = {**document_model._default_doc(), "dossier_id": "d1",
            "dossier_file_number": "2026-001", "filename": "lettre.docx",
            "file_type": mime, "file_size": len(source), "storage_path": path,
            "category": "correspondance", "category_source": "juriste",
            "created_at": DT, "updated_at": DT}
    fake.seed("documents/src", {**base, "id": "src", "display_name": "Lettre",
                                "etag": "e-src"})
    champ, errors = document_model._analyse_derivee(
        {"sous_nature": "CORR_CLIENT", "privileges": ["SECRET_PROFESSIONNEL"]},
        document={})
    assert errors == []
    fake.seed("documents/prot", {**base, "id": "prot", "display_name": "Secret",
                                 "analyse": champ, "category_source": "analyse",
                                 "etag": "e-prot"})
    return fake, ids


def test_fill_gabarit_conforms_filled_and_left_for_word(monkeypatch):
    _fake, ids = _generation_world(monkeypatch)
    filled = handlers.fill_gabarit({
        "template_id": ids["gabarit"], "dossier_id": "d1",
        "blocs": [{"nom": "FAITS", "contenu": "Un.\n\nDeux."}],
        "champs_manuels": [{"nom": "objet_lettre", "valeur": "Objet"}]})
    _conforms("fill_gabarit", filled)
    assert filled["fields"]["blocs_left_verbatim"] == ["civilité"]
    bare = handlers.fill_gabarit({"template_id": ids["gabarit"],
                                  "dossier_id": "d1"})
    _conforms("fill_gabarit", bare)
    assert bare["fields"]["manual_missing"] == ["objet_lettre"]


def test_create_document_conforms_on_both_sources(monkeypatch):
    _fake, ids = _generation_world(monkeypatch)
    markdown = handlers.create_document({
        "source": "markdown", "dossier_id": "d1", "title": "Titre",
        "markdown": "**gras**", "category": "preuve", "folder_id": ""})
    _conforms("create_document", markdown)
    assert markdown["template"]["id"] == ids["note"]
    plain_copy = handlers.create_document({"source": "copy",
                                           "document_id": "src"})
    _conforms("create_document", plain_copy)
    assert plain_copy["protection"] is None and plain_copy["template"] is None
    protected = handlers.create_document({"source": "copy",
                                          "document_id": "prot"})
    _conforms("create_document", protected)
    assert protected["protection"]["niveau_protection"] == 3


# ══════════════════════════════════════════════════════════════════════
# Lot 2A (T9) — the upload ticket
# ══════════════════════════════════════════════════════════════════════


def _upload_world(monkeypatch):
    import base64
    import hashlib

    fake, ids = _generation_world(monkeypatch)
    from models import doc_template as tpl_model

    bucket = tpl_model.storage.bucket()

    def md5(data: bytes) -> str:
        return base64.b64encode(hashlib.md5(data).digest()).decode()

    return fake, ids, bucket, md5


def test_begin_upload_conforms_on_both_purposes(monkeypatch):
    _fake, ids, _bucket, md5 = _upload_world(monkeypatch)
    pdf = b"%PDF-1.7 " + bytes(200)
    document = handlers.begin_upload({
        "purpose": "document", "dossier_id": "d1", "filename": "a.pdf",
        "size_bytes": len(pdf), "md5_base64": md5(pdf),
        "category": "preuve"})
    _conforms("begin_upload", document)
    assert "upload_id=" in document["upload_url"]
    gabarit = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "replace",
        "aucun_dossier_source": True,          # fixups of lot 2A
        "template_id": ids["gabarit"], "expected_version": 1,
        "filename": "x.docx", "size_bytes": 1000,
        "md5_base64": md5(b"x")})
    _conforms("begin_upload", gabarit)
    assert gabarit["entity"]["dossier_id"] == ""


def test_finalize_upload_conforms_on_every_branch(monkeypatch):
    import io
    import zipfile

    _fake, _ids, bucket, md5 = _upload_world(monkeypatch)
    pdf = b"%PDF-1.7 " + bytes(200)
    opened = handlers.begin_upload({
        "purpose": "document", "dossier_id": "d1", "filename": "a.pdf",
        "size_bytes": len(pdf), "md5_base64": md5(pdf)})
    bucket.complete_session(opened["upload_url"], pdf)
    document = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    _conforms("finalize_upload", document)
    assert document["purpose"] == "document"
    replay = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    _conforms("finalize_upload", replay)
    assert replay["already_finalized"] is True

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats'
            '.org/package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>'))
        zf.writestr("word/document.xml", (
            f'<?xml version="1.0"?><w:document {w}><w:body><w:p><w:r>'
            "<w:t>{{objet_lettre}}</w:t></w:r></w:p></w:body></w:document>"))
    docx = buf.getvalue()
    opened = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "create", "name": "Modèle",
        "dossier_id": "d1", "scrub_properties": True, "filename": "m.docx",
        "size_bytes": len(docx), "md5_base64": md5(docx)})
    bucket.complete_session(opened["upload_url"], docx)
    gabarit = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    _conforms("finalize_upload", gabarit)
    assert gabarit["leak_scan"]["performed"] is True
    assert gabarit["scrubbed_properties"] == []
    gabarit_replay = handlers.finalize_upload({"ticket_id": opened["ticket_id"]})
    _conforms("finalize_upload", gabarit_replay)
    assert gabarit_replay["leak_scan"] is None
    # Fixups of lot 2A: the DECLARED absence of source dossier — the one
    # way a gabarit is filed unchecked, and the report says so.
    declared = handlers.begin_upload({
        "purpose": "gabarit", "template_mode": "create", "name": "Modèle 2",
        "aucun_dossier_source": True, "filename": "m2.docx",
        "size_bytes": len(docx), "md5_base64": md5(docx)})
    bucket.complete_session(declared["upload_url"], docx)
    unchecked = handlers.finalize_upload({"ticket_id": declared["ticket_id"]})
    _conforms("finalize_upload", unchecked)
    assert unchecked["leak_scan"]["performed"] is False


# ══════════════════════════════════════════════════════════════════════
# Lot 2A (T10) — the templates taken from a stored document
# ══════════════════════════════════════════════════════════════════════


def _seed_leaky_source(fake, bucket):
    """A stored .docx still carrying the dossier's file number."""
    import io
    import zipfile

    from models import document as document_model

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats'
            '.org/package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>'))
        zf.writestr("word/document.xml", (
            f'<?xml version="1.0"?><w:document {w}><w:body><w:p><w:r>'
            "<w:t>Dossier 2026-001 : {{objet_lettre}}</w:t></w:r></w:p>"
            "</w:body></w:document>"))
    data = buf.getvalue()
    path = "users/uid-conformance/dossiers/d1/documents/leaky/l.docx"
    bucket.put(path, data)
    fake.seed("documents/leaky", {
        **document_model._default_doc(), "id": "leaky", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "display_name": "L",
        "filename": "l.docx",
        "file_type": document_model.EXTENSION_MIME_TYPES[".docx"],
        "file_size": len(data), "storage_path": path,
        "category": "correspondance", "category_source": "juriste",
        "created_at": DT, "updated_at": DT, "etag": "e-leaky"})


def test_create_template_conforms_clean_scrubbed_and_with_residues(monkeypatch):
    fake, _ids = _generation_world(monkeypatch)
    from models import doc_template as tpl_model

    clean = handlers.create_template({
        "source_document_id": "src", "name": "Modèle", "category": "autre"})
    _conforms("create_template", clean)
    assert clean["scrubbed_properties"] is None
    scrubbed = handlers.create_template({
        "source_document_id": "src", "name": "Modèle bis", "category": "autre",
        "kind": "note", "scrub_properties": True})
    _conforms("create_template", scrubbed)
    assert scrubbed["scrubbed_properties"] == []
    _seed_leaky_source(fake, tpl_model.storage.bucket())
    accepted = handlers.create_template({
        "source_document_id": "leaky", "name": "Modèle ter",
        "category": "correspondance", "accept_residual": ["2026-001"]})
    _conforms("create_template", accepted)
    assert accepted["leak_scan"]["accepted_residues"][0]["identifier"] == "2026-001"
    assert accepted["templatized"] is None


# ══════════════════════════════════════════════════════════════════════
# Lot 2B — templatizing a stored document
# ══════════════════════════════════════════════════════════════════════


def _seed_tracked_source(fake, bucket):
    """A stored .docx carrying a tracked insertion — a source the engine
    refuses (the blocker branch: no result, no scan)."""
    import io
    import zipfile

    from models import document as document_model

    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats'
            '.org/package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>'))
        zf.writestr("word/document.xml", (
            f'<?xml version="1.0"?><w:document {w}><w:body><w:p><w:r>'
            "<w:t>Dossier 2026-001</w:t></w:r></w:p><w:p><w:ins w:id=\"1\" "
            'w:author="J"><w:r><w:t>ajout</w:t></w:r></w:ins></w:p>'
            "</w:body></w:document>"))
    data = buf.getvalue()
    path = "users/uid-conformance/dossiers/d1/documents/tracked/t.docx"
    bucket.put(path, data)
    fake.seed("documents/tracked", {
        **document_model._default_doc(), "id": "tracked", "dossier_id": "d1",
        "dossier_file_number": "2026-001", "display_name": "T",
        "filename": "t.docx",
        "file_type": document_model.EXTENSION_MIME_TYPES[".docx"],
        "file_size": len(data), "storage_path": path,
        "category": "correspondance", "category_source": "juriste",
        "created_at": DT, "updated_at": DT, "etag": "e-tracked"})


def test_preview_templatize_conforms_ready_residual_and_blocked(monkeypatch):
    fake, _ids = _generation_world(monkeypatch)
    from models import doc_template as tpl_model

    bucket = tpl_model.storage.bucket()
    _seed_leaky_source(fake, bucket)
    _seed_tracked_source(fake, bucket)
    sub = {"literal": "2026-001", "placeholder": "dossier.reference_interne"}
    ready = handlers.preview_templatize({
        "document_id": "leaky", "substitutions": [dict(sub)]})
    _conforms("preview_templatize", ready)
    assert ready["ready_to_create"] is True
    assert ready["result_placeholders"] is not None
    assert ready["substitutions"][0]["matches_expected"] is None
    # A wrong expectation, an unknown literal and an invalid name — and the
    # residue the untouched file number leaves.
    residual = handlers.preview_templatize({
        "document_id": "leaky", "scrub_properties": True, "substitutions": [
            {"literal": "Absent", "placeholder": "client.nom",
             "expected_occurrences": 2},
            {"literal": "Dossier", "placeholder": "pas un nom"}]})
    _conforms("preview_templatize", residual)
    assert residual["leak_scan"]["residues"][0]["identifier"] == "2026-001"
    assert residual["substitutions"][0]["matches_expected"] is False
    assert residual["substitutions"][1]["classification"] is None
    assert residual["scrubbed_properties"] == []
    assert residual["ready_to_create"] is False
    blocked = handlers.preview_templatize({
        "document_id": "tracked", "substitutions": [dict(sub)]})
    _conforms("preview_templatize", blocked)
    assert blocked["source_blockers"][0]["code"] == "tracked_changes"
    assert blocked["leak_scan"] is None
    assert blocked["result_placeholders"] is None


def test_create_template_conforms_when_templatized(monkeypatch):
    fake, _ids = _generation_world(monkeypatch)
    from models import doc_template as tpl_model

    _seed_leaky_source(fake, tpl_model.storage.bucket())
    templatized = handlers.create_template({
        "source_document_id": "leaky", "name": "Modèle gabaritisé",
        "category": "correspondance", "substitutions": [
            {"literal": "2026-001", "placeholder": "dossier.reference_interne",
             "expected_occurrences": 1}]})
    _conforms("create_template", templatized)
    assert templatized["templatized"]["substituted_total"] == 1
    assert templatized["leak_scan"]["accepted_residues"] == []


def test_update_template_conforms_in_both_modes_and_on_no_ops(monkeypatch):
    _fake, ids = _generation_world(monkeypatch)
    tid = ids["gabarit"]
    metadata = handlers.update_template({"template_id": tid, "name": "Renommé"})
    _conforms("update_template", metadata)
    assert metadata["mode"] == "metadata" and metadata["leak_scan"] is None
    unchanged = handlers.update_template({"template_id": tid, "name": "Renommé"})
    _conforms("update_template", unchanged)
    assert unchanged["changed_fields"] == []
    replaced = handlers.update_template({
        "template_id": tid, "source_document_id": "src", "expected_version": 1,
        "scrub_properties": True})
    _conforms("update_template", replaced)
    assert replaced["file_replaced"] is True and replaced["replaced_version"] == 1
    identical = handlers.update_template({
        "template_id": tid, "source_document_id": "src", "expected_version": 1})
    _conforms("update_template", identical)
    assert identical["file_replaced"] is False
    assert identical["replaced_version"] is None
