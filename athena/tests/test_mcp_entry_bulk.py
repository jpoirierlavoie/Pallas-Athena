"""The two bulk creators — ``create_time_entries_bulk`` / ``create_expenses_bulk``
(2026-09-30, approved by the lawyer).

One import made 61 separate ``create_time_entry`` calls, and
``create_time_entry → create_time_entry`` was the most frequent same-tool pair
in 30 days of logs. The batch form writes up to ``ENTRY_BULK_MAX`` rows in ONE
call, and it is ALL OR NOTHING: every row is built through the single tool's
own builder first, one refused row refuses the whole call — naming each bad
row by its 0-based index, ``entries[i]``, the path ``validate_args`` gives an
item (and ``entries[i].hours`` one of its fields — bare until this lot, which
left the commonest faults unlocated, the schema check running FIRST on the
MCP path) — and the rows then land in ONE Firestore batch. The key is REQUIRED
(money: a retry without it would be a second batch the next invoice sweeps).

Real handlers, real ``run_write`` and idempotency store, real models, over the
shared fake Firestore (``tests/_fake_firestore.py``) — so « nothing written »
is read off the store, never off a mock.
"""

import itertools
import os
import sys
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.handlers as handlers
    import mcp.tools as tools
    import mcp.write_support as write_support  # its db is patched below
    from mcp import disclosure, endpoint
    # `import models.x as y`, never `from models import x`: a test below
    # imports the package itself to patch models.find_by_legacy_ref, and
    # both forms of import of one module are what CodeQL flags.
    import models.concurrency as concurrency
    import models.dossier as dossier_model
    import models.expense as expense_model
    import models.time_entry as time_entry_model

from tests._fake_firestore import install  # noqa: E402

# Loaded for its side effect, and named here so the dependency is
# visible: the fake store is installed on every LOADED module holding a
# `db` (a sweep of sys.modules), so it must be imported — under the
# Firestore mock — before a test installs it. Bound to `_`, the name
# that says « deliberately unused ».
_ = (write_support,)

_KEYS = itertools.count(1)
_TIME = "create_time_entries_bulk"
_EXPENSE = "create_expenses_bulk"


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n == "models" or n.startswith("models.")
                or n in ("dav.sync", "mcp.write_support"))
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    for did, number, rate in (("d1", "2026-001", 30000), ("d2", "2026-002", 25000)):
        f.seed(f"dossiers/{did}", {
            "id": did, "file_number": number, "title": f"Dossier {did}",
            "status": "actif", "hourly_rate": rate, "clients": [],
            "client_ids": [], "opposing_parties": [],
            "opposing_party_ids": [], "etag": f"e-{did}",
        })
    return f


def _key() -> str:
    return f"cle-lot-{next(_KEYS):04d}"


def _time_row(**over) -> dict:
    return {"dossier_id": "d1", "date": "2026-09-28", "hours": 1.5,
            "description": "Rédaction de la requête", **over}


def _expense_row(**over) -> dict:
    return {"dossier_id": "d1", "date": "2026-09-28", "amount_cents": 9500,
            "description": "Huissier — signification", **over}


def _call(tool: str, args: dict) -> dict:
    assert tools.validate_args(tools.TOOLS[tool]["input_schema"], args) == []
    return getattr(handlers, tool)(dict(args))


def _refused(tool: str, args: dict, *, schema: bool = True) -> tools.ToolArgumentError:
    if schema:
        assert tools.validate_args(tools.TOOLS[tool]["input_schema"], args) == []
    with pytest.raises(tools.ToolArgumentError) as excinfo:
        getattr(handlers, tool)(dict(args))
    return excinfo.value


def _dossier_reads(fake, did: str) -> int:
    return sum(1 for r in fake.reads if f"dossiers/{did}" in r.paths)


# ══════════════════════════════════════════════════════════════════════
# 1. The registry: the single tools' fields, the required key, the hints
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("bulk, single", [(_TIME, "create_time_entry"),
                                          (_EXPENSE, "create_expense")])
def test_each_row_takes_exactly_the_single_tool_s_fields(bulk, single):
    """« Each row takes the same fields as the single tool » — pinned by
    comparison, never by a second list: the items ARE the single tool's
    properties minus the call-level protocol key, with its required list."""
    single_schema = tools.TOOLS[single]["input_schema"]
    entries = tools.TOOLS[bulk]["input_schema"]["properties"]["entries"]
    items = entries["items"]
    expected = {k: v for k, v in single_schema["properties"].items()
                if k != "idempotency_key"}
    assert items["properties"] == expected
    assert items["required"] == single_schema["required"]
    assert items["additionalProperties"] is False
    assert entries["minItems"] == 1
    assert entries["maxItems"] == tools.ENTRY_BULK_MAX
    for name, prop in items["properties"].items():
        assert prop.get("description"), name


def test_the_ceiling_is_the_models_own():
    assert tools.ENTRY_BULK_MAX == time_entry_model.BULK_CREATE_MAX
    assert tools.ENTRY_BULK_MAX == expense_model.BULK_CREATE_MAX == 50


@pytest.mark.parametrize("tool", [_TIME, _EXPENSE])
def test_the_key_is_required_and_the_hints_are_a_creator_s(tool):
    spec = tools.TOOLS[tool]
    assert spec["idempotency"] == tools.IDEMPOTENCY_REQUIRED
    assert spec["input_schema"]["required"] == ["entries", "idempotency_key"]
    assert spec["input_schema"]["additionalProperties"] is False
    key = spec["input_schema"]["properties"]["idempotency_key"]
    assert key["description"] == tools.REQUIRED_KEY_DESCRIPTION
    assert spec["scope"] == "athena:write"
    assert tool in tools.WRITE_TOOLS and tool not in tools.EDIT_TOOLS
    create = next(f for f in disclosure.FAMILIES if f.key == "create")
    assert tool in create.tools
    (d,) = [d for d in tools.list_tool_descriptors(None) if d["name"] == tool]
    ann = d["annotations"]
    # Additive — it replaces nothing — and NOT idempotent: a second call
    # without its key is a second batch (the create_invoice rule).
    assert ann["destructiveHint"] is False
    assert ann["idempotentHint"] is False
    assert ann["readOnlyHint"] is False and ann["openWorldHint"] is False


@pytest.mark.parametrize("tool", [_TIME, _EXPENSE])
def test_the_description_opens_on_the_refusal_and_the_confirmation(tool):
    text = tools.TOOLS[tool]["description"]
    assert len(text) <= 2048
    head = text[:400]
    assert "ALL OR NOTHING" in head and "nothing is written" in head
    # Both forms a refusal takes: the handler's row, the schema's field.
    field = "hours" if tool == _TIME else "amount_cents"
    assert f"0-based index (`entries[3]`, a field `entries[3].{field}`)" in head
    assert "idempotency_key REQUIRED" in head
    assert "confirm the batch with the user before calling" in head
    assert "VERBATIM on the client's invoice" in text
    assert "never delete" in text


def test_the_instructions_and_the_consent_name_them_with_their_ceiling():
    text = endpoint.INSTRUCTIONS
    assert "`create_time_entries_bulk`" in text and "`create_expenses_bulk`" in text
    assert f"up to {tools.ENTRY_BULK_MAX} rows, ALL OR NOTHING" in text
    assert disclosure.consent_context()["entry_bulk_max"] == tools.ENTRY_BULK_MAX
    assert "par lots" in disclosure.write_summary_fr()


# ══════════════════════════════════════════════════════════════════════
# 2. The happy path — every row through the single tool's rules
# ══════════════════════════════════════════════════════════════════════


def test_a_batch_of_time_entries_lands_in_order_in_one_commit(fake):
    rows = [
        _time_row(hours=0.25, sous_phase="PRE-01"),          # rate: d1's
        _time_row(dossier_id="d2", hours=2, rate_cents=40000),
        _time_row(hours=1, billable=False, phase="CTS"),
        _time_row(date="2026-09-29", legacy_ref="ancien-1"),
    ]
    fake.reset_logs()
    payload = _call(_TIME, {"entries": rows, "idempotency_key": _key()})
    assert payload["created"] is True and payload["count"] == 4
    assert payload["entity_type"] == "time_entry"
    assert payload["idempotent_replay"] is False and payload["warnings"] == []
    entities = payload["entities"]
    stored = fake.peek_collection("timeentries")
    assert [e["id"] for e in entities] == [e["id"] for e in entities if e["id"] in stored]
    assert len(stored) == 4
    # Request order, each the single tool's entity.
    assert [e["dossier_id"] for e in entities] == ["d1", "d2", "d1", "d1"]
    assert [e["hours"] for e in entities] == [0.25, 2.0, 1.0, 1.5]
    first, second, third, fourth = (stored[e["id"]] for e in entities)
    assert first["rate"] == 30000 and first["amount"] == 7500   # the quarter-hour, exact
    assert (first["phase"], first["sous_phase"]) == ("PRE", "PRE-01")
    assert second["rate"] == 40000 and second["amount"] == 80000
    assert second["dossier_file_number"] == "2026-002"
    assert third["amount"] == 0 and third["billable"] is False  # non-billable
    assert (third["phase"], third["sous_phase"]) == ("CTS", "CTS-00")
    assert fourth["legacy_ref"] == "ancien-1"
    for doc in stored.values():
        # The billed state: never invoiced at birth, no invoice behind it.
        assert doc["invoiced"] is False and doc["invoice_id"] is None
        assert doc["created_via"] == "mcp"
        # No provenance TEXT: the description prints on the invoice.
        assert doc["description"] == "Rédaction de la requête"
        assert "Claude" not in doc["description"]
    for entity in entities:
        assert entity["etag"] == stored[entity["id"]]["etag"]
    # ONE commit carried every row — all or nothing.
    commits = [c for c in fake.commits
               if any(path.startswith("timeentries/") for _k, path in c.ops)]
    assert len(commits) == 1
    assert sum(1 for _k, p in commits[0].ops if p.startswith("timeentries/")) == 4
    # Each distinct dossier was read once.
    assert _dossier_reads(fake, "d1") == 1 and _dossier_reads(fake, "d2") == 1


def test_a_batch_of_disbursements_takes_create_expense_s_defaults(fake):
    payload = _call(_EXPENSE, {"entries": [
        _expense_row(),
        _expense_row(amount_cents=1200, category="photocopie", taxable=False,
                     sous_phase="PRE-01"),
    ], "idempotency_key": _key()})
    stored = fake.peek_collection("expenses")
    assert len(stored) == 2
    first, second = (stored[e["id"]] for e in payload["entities"])
    assert first["category"] == "autre" and first["taxable"] is True
    assert (first["phase"], first["sous_phase"]) == ("", "")   # unclassified
    assert second["category"] == "photocopie" and second["taxable"] is False
    assert (second["phase"], second["sous_phase"]) == ("PRE", "PRE-01")
    for doc in stored.values():
        assert doc["invoiced"] is False and doc["invoice_id"] is None
        assert doc["created_via"] == "mcp"
    assert [e["amount_cents"] for e in payload["entities"]] == [9500, 1200]


def test_the_single_tool_still_takes_the_same_path(fake):
    """The refactor that shares the builder left the single tool whole."""
    payload = _call("create_time_entry", {**_time_row(hours=0.25),
                                          "idempotency_key": _key()})
    assert payload["entity"]["amount_cents"] == 7500
    stored = fake.peek(f"timeentries/{payload['entity']['id']}")
    assert stored["created_via"] == "mcp" and stored["invoiced"] is False


# ══════════════════════════════════════════════════════════════════════
# 3. ALL OR NOTHING — each bad row named, nothing written
# ══════════════════════════════════════════════════════════════════════


def test_one_bad_row_refuses_the_whole_batch_and_writes_nothing(fake):
    rows = [_time_row(), _time_row(hours=0.333), _time_row()]
    fake.reset_logs()
    exc = _refused(_TIME, {"entries": rows, "idempotency_key": _key()})
    text = str(exc)
    assert "Aucune entrée de temps n'a été enregistrée." in text
    assert "1 ligne(s) sur 3" in text
    assert "`entries[1]` : `hours`" in text and "deux décimales" in text
    assert "`entries[0]`" not in text and "`entries[2]`" not in text
    assert exc.reason == "argument_refused" and exc.keep_claim is False
    assert fake.peek_collection("timeentries") == {}
    assert not [c for c in fake.commits
                if any(p.startswith("timeentries/") for _k, p in c.ops)]


def test_every_bad_row_is_named_by_its_0_based_index(fake):
    rows = [
        _time_row(description="   "),                      # blank
        _time_row(),                                        # fine
        _time_row(phase="INT", sous_phase="CTS-02"),        # contradictory
        _time_row(dossier_id="inconnu"),                    # unknown dossier
        _time_row(),                                        # fine
        _time_row(date="2026-13-40"),                       # not a date
    ]
    exc = _refused(_TIME, {"entries": rows, "idempotency_key": _key()},
                   schema=False)
    text = str(exc)
    assert "4 ligne(s) sur 6" in text
    for index in (0, 2, 3, 5):
        assert f"`entries[{index}]` :" in text, index
    for index in (1, 4):
        assert f"`entries[{index}]`" not in text, index
    assert "n'appartient pas à la phase" in text
    assert "inconnu" in text
    # In index order, so the caller fixes top to bottom.
    positions = [text.index(f"`entries[{i}]`") for i in (0, 2, 3, 5)]
    assert positions == sorted(positions)
    assert fake.peek_collection("timeentries") == {}


@pytest.mark.parametrize("tool, row, bad", [
    (_TIME, _time_row, ({"hours": 30}, "hours", {"phase": "XYZ"})),
    (_EXPENSE, _expense_row, ({"amount_cents": 0}, "amount_cents",
                              {"category": "XYZ"})),
])
def test_the_schema_check_names_the_row_of_every_field_it_refuses(tool, row, bad):
    """The FIRST check on the real MCP path is validate_args
    (endpoint._tools_call runs it before the handler), and it used to name a
    row's field BARE — « `hours` must be <= 24; `hours` is required » — so
    the commonest faults of a 50-row batch reached the caller with no row to
    fix, and a new row has no id to find it by. Non-zero positions, one fault
    of each kind: a bound, a missing required field, a value off an enum."""
    bound, required, enum = bad
    rows = [row() for _ in range(50)]
    rows[12].update(bound)
    del rows[37][required]
    rows[44].update(enum)
    errors = tools.validate_args(tools.TOOLS[tool]["input_schema"],
                                 {"entries": rows, "idempotency_key": _key()})
    (e12,) = [e for e in errors if "`entries[12]" in e]
    (e37,) = [e for e in errors if "`entries[37]" in e]
    (e44,) = [e for e in errors if "`entries[44]" in e]
    assert e12.startswith(f"`entries[12].{next(iter(bound))}` must be")
    assert e37 == f"`entries[37].{required}` is required"
    assert e44.startswith(f"`entries[44].{next(iter(enum))}` must be one of")
    # Nothing else is refused, and no field is named bare any more.
    assert len(errors) == 3
    assert not [e for e in errors if not e.startswith("`entries[")]


def test_a_legacy_ref_twice_is_named_even_when_its_first_row_fails(fake):
    """The first row bearing a reference claims it whether or not that row
    builds. Registered only on a successful build, a reference whose first
    row was refused for another reason let its later twin through unnamed:
    the caller fixed row 0, resent the WHOLE batch as told — and only then
    learnt of row 2."""
    exc = _refused(_TIME, {"entries": [
        _time_row(legacy_ref="F", hours=0.333), _time_row(),
        _time_row(legacy_ref="F"),
    ], "idempotency_key": _key()})
    text = str(exc)
    assert "2 ligne(s) sur 3" in text
    assert "`entries[0]` : `hours`" in text and "deux décimales" in text
    assert "`entries[2]` : La référence d'origine « F » figure déjà à " \
           "`entries[0]` de ce lot" in text
    assert "`entries[1]`" not in text
    assert fake.peek_collection("timeentries") == {}


def test_a_duplicate_row_s_other_fault_is_named_in_the_same_refusal(fake):
    """A duplicate row is still built, so a second fault it carries is not
    left for the next attempt — and the count is of ROWS, not messages."""
    exc = _refused(_EXPENSE, {"entries": [
        _expense_row(legacy_ref="F-9"),
        _expense_row(legacy_ref="F-9", dossier_id="inconnu"),
    ], "idempotency_key": _key()})
    text = str(exc)
    assert "1 ligne(s) sur 2" in text
    assert text.count("`entries[1]` :") == 2
    assert "de ce lot" in text and "inconnu" in text
    assert "`entries[0]` :" not in text


@pytest.mark.parametrize("tool, row", [(_TIME, _time_row),
                                       ("create_time_entry", None)])
def test_a_failed_legacy_ref_check_refuses_and_releases_the_key(
    fake, monkeypatch, tool, row,
):
    """find_by_legacy_ref RAISES on a failed query, by design (a swallowed
    error would read « absent » and mint a duplicate). Raised bare, it left
    the handler as an internal error, and run_write KEEPS the claim of a
    key-REQUIRED tool on anything but a refusal — so the same key was then
    refused « encore en cours », later « interrompu », for a call that had
    written nothing (the check precedes every write). It now refuses under
    read_unavailable — the stop-the-batch signal —, releasing the key."""
    import models

    real = models.find_by_legacy_ref

    def _down(collection, legacy_ref, limit=5):
        raise gexc.ServiceUnavailable("down")

    monkeypatch.setattr(models, "find_by_legacy_ref", _down)
    key = _key()
    if row is None:
        args = {**_time_row(legacy_ref="ancien-3"), "idempotency_key": key}
    else:
        args = {"entries": [row(), row(legacy_ref="ancien-3")],
                "idempotency_key": key}
    exc = _refused(tool, args)
    assert exc.reason == "read_unavailable" and exc.keep_claim is False
    assert "n'a pas pu être lue" in str(exc)
    assert "rien n'a été enregistré" in str(exc)
    assert fake.peek_collection("timeentries") == {}
    # The fault clears: the SAME key runs the call afresh, and it lands.
    monkeypatch.setattr(models, "find_by_legacy_ref", real)
    payload = _call(tool, args)
    assert payload["idempotent_replay"] is False
    expected = 1 if row is None else 2
    assert len(fake.peek_collection("timeentries")) == expected


def test_a_refused_batch_releases_its_key_for_the_corrected_one(fake):
    key = _key()
    _refused(_EXPENSE, {"entries": [_expense_row(), _expense_row(dossier_id="nope")],
                        "idempotency_key": key})
    assert fake.peek_collection("expenses") == {}
    # Nothing was reserved: the corrected batch runs under the SAME key.
    payload = _call(_EXPENSE, {"entries": [_expense_row(), _expense_row()],
                               "idempotency_key": key})
    assert payload["count"] == 2 and len(fake.peek_collection("expenses")) == 2


def test_rows_naming_one_unknown_dossier_cost_one_read(fake):
    rows = [_time_row(dossier_id="dx")] * 3
    fake.reset_logs()
    exc = _refused(_TIME, {"entries": rows, "idempotency_key": _key()})
    text = str(exc)
    assert all(f"`entries[{i}]`" in text for i in range(3))
    assert _dossier_reads(fake, "dx") == 1


def test_an_unreadable_dossier_stops_the_batch_under_its_own_reason(fake, monkeypatch):
    real = dossier_model.get_dossier_strict

    def _flaky(did):
        if did == "d2":
            raise gexc.ServiceUnavailable("down")
        return real(did)

    monkeypatch.setattr(dossier_model, "get_dossier_strict", _flaky)
    exc = _refused(_TIME, {"entries": [_time_row(), _time_row(dossier_id="d2")],
                           "idempotency_key": _key()})
    assert exc.reason == "read_unavailable"
    assert fake.peek_collection("timeentries") == {}


def test_a_legacy_ref_twice_in_one_batch_is_refused_naming_both_rows(fake):
    exc = _refused(_EXPENSE, {"entries": [
        _expense_row(legacy_ref="F-12"), _expense_row(), _expense_row(legacy_ref="F-12"),
    ], "idempotency_key": _key()})
    text = str(exc)
    assert "`entries[2]` :" in text and "`entries[0]` de ce lot" in text
    assert fake.peek_collection("expenses") == {}


def test_a_legacy_ref_already_stored_is_refused(fake):
    fake.seed("timeentries/old", {"id": "old", "dossier_id": "d1",
                                  "legacy_ref": "ancien-7"})
    exc = _refused(_TIME, {"entries": [_time_row(), _time_row(legacy_ref="ancien-7")],
                           "idempotency_key": _key()})
    assert "`entries[1]` : La référence d'origine « ancien-7 »" in str(exc)
    assert set(fake.peek_collection("timeentries")) == {"old"}


# ══════════════════════════════════════════════════════════════════════
# 4. The bounds, the key and the billed state
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tool, row", [(_TIME, _time_row), (_EXPENSE, _expense_row)])
def test_fifty_one_rows_are_refused_and_fifty_land(fake, tool, row):
    schema = tools.TOOLS[tool]["input_schema"]
    over = {"entries": [row() for _ in range(51)], "idempotency_key": _key()}
    assert tools.validate_args(schema, over) == [
        "`entries` must contain at most 50 items (51 given)"]
    # The handler repeats the bound — a direct call skips the schema.
    exc = _refused(tool, over, schema=False)
    assert "plafonné à 50 lignes" in str(exc) and "(51 reçues)" in str(exc)
    collection = "timeentries" if tool == _TIME else "expenses"
    assert fake.peek_collection(collection) == {}
    assert tools.validate_args(schema, {"entries": [], "idempotency_key": _key()})
    payload = _call(tool, {"entries": [row() for _ in range(50)],
                           "idempotency_key": _key()})
    assert payload["count"] == 50
    assert len(fake.peek_collection(collection)) == 50


@pytest.mark.parametrize("tool, row", [(_TIME, _time_row), (_EXPENSE, _expense_row)])
def test_without_a_key_nothing_runs(fake, tool, row):
    args = {"entries": [row()]}
    assert tools.validate_args(tools.TOOLS[tool]["input_schema"], args)
    exc = _refused(tool, args, schema=False)
    assert exc.reason == "idempotency_required"
    collection = "timeentries" if tool == _TIME else "expenses"
    assert fake.peek_collection(collection) == {}


def test_the_same_key_replays_and_never_writes_a_second_batch(fake):
    args = {"entries": [_time_row(), _time_row(hours=0.5)], "idempotency_key": _key()}
    first = _call(_TIME, args)
    again = _call(_TIME, args)
    assert again["idempotent_replay"] is True
    assert [e["id"] for e in again["entities"]] == [e["id"] for e in first["entities"]]
    assert len(fake.peek_collection("timeentries")) == 2
    # The same key with other rows is refused, never a third write.
    other = _refused(_TIME, {**args, "entries": [_time_row(hours=3)]})
    assert other.reason == "idempotency_conflict"
    assert len(fake.peek_collection("timeentries")) == 2


@pytest.mark.parametrize("tool, row", [(_TIME, _time_row), (_EXPENSE, _expense_row)])
def test_a_row_can_never_be_born_invoiced(tool, row):
    """The billed state is the model's: no row may name it, nor an id."""
    schema = tools.TOOLS[tool]["input_schema"]
    for forged in ({"invoiced": True}, {"invoice_id": "inv1"}, {"id": "x"},
                   {"created_via": "web"}, {"idempotency_key": "cle-ligne-01"}):
        errors = tools.validate_args(
            schema, {"entries": [row(**forged)], "idempotency_key": _key()})
        assert errors and "is not a supported argument" in errors[0], forged


def test_a_phase_code_outside_the_vocabulary_is_refused_by_the_schema():
    schema = tools.TOOLS[_TIME]["input_schema"]
    for bad in ({"phase": "ZZZ"}, {"sous_phase": "CTS-77"}, {"phase": ""}):
        assert tools.validate_args(
            schema, {"entries": [_time_row(**bad)], "idempotency_key": _key()}), bad


# ══════════════════════════════════════════════════════════════════════
# 5. The model: its own belt, and ONE batch settled by its first id
# ══════════════════════════════════════════════════════════════════════


def _model_row(**over) -> dict:
    from datetime import datetime, timezone

    return {"dossier_id": "d1", "date": datetime(2026, 9, 28, tzinfo=timezone.utc),
            "description": "x", "hours": 1.0, "rate": 30000, **over}


def test_the_model_refuses_the_whole_list_naming_each_row(fake):
    docs, errors = time_entry_model.create_time_entries_bulk(
        [_model_row(), _model_row(description=""), _model_row(hours=0)])
    assert docs == []
    assert errors[0].startswith("entries[1] : ")
    assert errors[1].startswith("entries[2] : ")
    assert fake.peek_collection("timeentries") == {}
    docs, errors = expense_model.create_expenses_bulk([{"dossier_id": "d1"}] * 51)
    assert docs == [] and "Au plus 50" in errors[0]
    assert time_entry_model.create_time_entries_bulk([]) == (
        [], ["Aucune entrée de temps à créer."])


def test_the_model_never_honours_a_caller_id(fake):
    docs, errors = time_entry_model.create_time_entries_bulk(
        [_model_row(id="forged"), _model_row(id="forged")])
    assert errors == [] and len({d["id"] for d in docs}) == 2
    assert "forged" not in fake.peek_collection("timeentries")


def _fail_commit(fake, monkeypatch, *, land: bool, marker: str) -> None:
    server = fake._fake_server
    real = server.commit
    state = {"armed": True}

    def _commit(request, metadata=None, **kwargs):
        writes = [getattr(w, "_pb", w) for w in request.get("writes") or []]
        hit = state["armed"] and any(marker in server._write_name(w) for w in writes)
        if hit:
            state["armed"] = False
            if land:
                real(request, metadata=metadata, **kwargs)
            raise gexc.InternalServerError("answer lost")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)


def test_a_batch_that_landed_with_its_answer_lost_is_the_committed_one(fake, monkeypatch):
    _fail_commit(fake, monkeypatch, land=True, marker="/timeentries/")
    args = {"entries": [_time_row(), _time_row()], "idempotency_key": _key()}
    payload = _call(_TIME, args)
    stored = fake.peek_collection("timeentries")
    assert {e["id"] for e in payload["entities"]} == set(stored) and len(stored) == 2
    assert _call(_TIME, args)["idempotent_replay"] is True
    assert len(fake.peek_collection("timeentries")) == 2


def test_a_batch_that_did_not_land_is_the_plain_save_error(fake, monkeypatch):
    _fail_commit(fake, monkeypatch, land=False, marker="/expenses/")
    key = _key()
    exc = _refused(_EXPENSE, {"entries": [_expense_row()], "idempotency_key": key})
    assert "Erreur lors de la sauvegarde" in str(exc)
    assert fake.peek_collection("expenses") == {}
    assert _call(_EXPENSE, {"entries": [_expense_row()], "idempotency_key": key})["count"] == 1


def test_an_unconfirmable_batch_keeps_the_claim(fake, monkeypatch):
    _fail_commit(fake, monkeypatch, land=True, marker="/timeentries/")
    server = fake._fake_server
    real_get = server.batch_get_documents

    def _get(request, metadata=None, **kwargs):
        if any(server.doc_rel(n).startswith("timeentries/")
               for n in request["documents"]):
            raise gexc.ServiceUnavailable("read-back failed")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)
    args = {"entries": [_time_row(), _time_row()], "idempotency_key": _key()}
    exc = _refused(_TIME, args)
    assert exc.reason == "write_outcome_uncertain" and exc.keep_claim is True
    assert concurrency.WRITE_OUTCOME_UNCERTAIN_ERROR in str(exc)
    count = len(fake.peek_collection("timeentries"))
    assert count == 2           # it DID land — and is never written again
    again = _refused(_TIME, args)
    assert again.reason in ("idempotency_in_flight", "idempotency_interrupted")
    assert len(fake.peek_collection("timeentries")) == count


# ══════════════════════════════════════════════════════════════════════
# 6. The audit line: counts only
# ══════════════════════════════════════════════════════════════════════


def test_the_audit_line_carries_counts_and_never_a_description(fake, monkeypatch):
    from utils import logging_setup

    seen: list = []
    monkeypatch.setattr(logging_setup, "log_mcp_event",
                        lambda event, outcome, **kw: seen.append((event, outcome, kw)))
    _call(_TIME, {"entries": [_time_row(), _time_row(dossier_id="d2")],
                  "idempotency_key": _key()})
    (event, outcome, fields) = [s for s in seen if s[0] == "mcp_entry_bulk"][0]
    assert outcome == "success"
    # Two dossiers: no single dossier_id to name.
    assert fields == {"entity_type": "time_entry", "requested": 2,
                      "created": 2, "dossiers": 2}
    # ONE id-shaped dossier: named; a description or an amount: never.
    uuid = "0f3c2a1e-9b7d-4c5e-8a6f-1d2e3f4a5b6c"
    fake.seed(f"dossiers/{uuid}", {"id": uuid, "file_number": "2026-003",
                                   "title": "T", "status": "actif"})
    seen.clear()
    _call(_EXPENSE, {"entries": [_expense_row(dossier_id=uuid)] * 2,
                     "idempotency_key": _key()})
    (_e, _o, fields) = [s for s in seen if s[0] == "mcp_entry_bulk"][0]
    assert fields == {"entity_type": "expense", "requested": 2, "created": 2,
                      "dossiers": 1, "dossier_id": uuid}
    assert "Huissier" not in repr(seen) and "9500" not in repr(seen)
    # A refused batch emits no line here (it is an mcp_write_refused).
    seen.clear()
    _refused(_TIME, {"entries": [_time_row(hours=0.333)], "idempotency_key": _key()})
    assert not [s for s in seen if s[0] == "mcp_entry_bulk"]
