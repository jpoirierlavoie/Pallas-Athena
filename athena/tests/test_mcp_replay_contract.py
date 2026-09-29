"""A key added to an already-shipped WRITE output is never required
(finitions, contracts-4).

A same-key retry within 24 h returns the STORED payload verbatim
(``mcp/write_support.run_write``; only ``begin_upload`` and the accounting
tools rehydrate). A result stored under the previous release therefore
lacks every key the next release added — and when such a key is declared
``required``, a strict SDK client rejects the replayed tool result, and the
model's natural reaction is a retry under a NEW key: for ``create_document``
or ``create_template``, a second document or template. The framework guard
already kept a write result's ``etag`` optional for exactly this reason
(« a replayed result stored before 2026-09-25 lacks it »).

The program had added required keys across release boundaries anyway:
``update_time_entry`` / ``update_expense`` gained ``moved`` and
``previous_dossier_id`` (lot 3b, tools in production), ``create_document``
``reused`` and ``invoice`` (lot 2 → 3), ``create_template`` ``templatized``
(lot 2A → 2B). They are optional now, and the rule is PINNED:
``tests/fixtures/mcp_write_outputs_first_shipped.json`` records each write
tool's effectively-required output paths as it FIRST shipped (the
production release 21012c0, or the first lot branch that carried it — the
lots deploy one by one), and the current contract may require nothing
more. A write tool that ships gets its entry there.
"""

import json
import os
import pathlib
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    import mcp.tools as tools
    from mcp.output_schemas import OUTPUT_SCHEMAS

from tests.test_mcp_output_schemas import _conforms  # noqa: E402

_FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
            / "mcp_write_outputs_first_shipped.json")


def required_paths(schema, prefix: str = "") -> set[str]:
    """The paths a payload MUST carry: a required key, and — only below a
    REQUIRED key — what that key's own schema requires (a key a payload may
    omit cannot make its children mandatory). anyOf branches union, array
    items descend."""
    out: set[str] = set()
    if not isinstance(schema, dict):
        return out
    props = schema.get("properties") or {}
    for key in schema.get("required", []) or []:
        out.add(f"{prefix}.{key}")
        if key in props:
            out |= required_paths(props[key], f"{prefix}.{key}")
    if "items" in schema:
        out |= required_paths(schema["items"], prefix + "[]")
    for branch in schema.get("anyOf") or []:
        out |= required_paths(branch, prefix)
    return out


def replay_violations(schemas: dict, first_shipped: dict, write_tools) -> list[str]:
    out = []
    for tool in sorted(write_tools):
        if tool not in first_shipped:
            out.append(
                f"{tool}: no first-shipped record — add it to "
                f"{_FIXTURE.name} with its required paths as it ships: "
                f"{sorted(required_paths(schemas[tool]))}")
            continue
        added = required_paths(schemas[tool]) - set(first_shipped[tool]["required"])
        if added:
            out.append(
                f"{tool}: requires {sorted(added)}, which its first shipped "
                f"release ({first_shipped[tool]['first_shipped_in']}) did not "
                "— a replay stored before them fails the contract. Declare "
                "them optional (the etag / provenance doctrine).")
    return out


def _first_shipped() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_no_write_output_requires_a_key_added_after_it_shipped():
    assert replay_violations(OUTPUT_SCHEMAS, _first_shipped(),
                             tools.WRITE_TOOLS) == []


def test_the_guard_catches_a_key_made_required_after_shipping():
    schemas = {"x": {"type": "object", "properties": {
        "a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"]}}
    first = {"x": {"first_shipped_in": "rel-1", "required": [".a"]}}
    (violation,) = replay_violations(schemas, first, {"x"})
    assert "['.b']" in violation and "rel-1" in violation
    assert replay_violations(schemas, {}, {"x"})[0].startswith("x: no first-shipped")


def test_the_guard_is_not_vacuous():
    record = _first_shipped()
    assert set(tools.WRITE_TOOLS) <= set(record)
    assert record["update_time_entry"]["first_shipped_in"] == "21012c0"
    for tool, keys in (("update_time_entry", (".moved", ".previous_dossier_id")),
                       ("update_expense", (".moved", ".previous_dossier_id")),
                       ("create_document", (".reused", ".invoice")),
                       ("create_template", (".templatized",))):
        assert not set(keys) & set(record[tool]["required"]), tool
        assert not set(keys) & required_paths(OUTPUT_SCHEMAS[tool]), tool


def test_a_payload_stored_before_the_move_keys_still_conforms(monkeypatch):
    """A replay of an update_time_entry result stored by the production
    release: the REAL handler's payload, minus the two keys lot 3b added,
    must still conform — through the conformance checker every tool's real
    payload passes."""
    with mock.patch("google.cloud.firestore.Client"):
        import mcp.handlers as handlers
    from datetime import datetime, timezone

    entry = {"id": "e1", "dossier_id": "d1", "description": "Rédaction",
             "hours": 1.5, "rate": 30000, "amount": 45000, "billable": True,
             "invoiced": False, "phase": "", "sous_phase": "",
             "date": datetime(2026, 9, 20, tzinfo=timezone.utc)}
    monkeypatch.setattr(handlers.time_entry_model, "get_time_entry_strict",
                        lambda i: entry)
    monkeypatch.setattr(handlers.time_entry_model, "update_time_entry",
                        lambda i, d, *, expected_etag=None: (
                            {**entry, **d, "etag": "e-ecrit"}, []))
    live = handlers.update_time_entry({"time_entry_id": "e1", "hours": 0.25})
    _conforms("update_time_entry", live)
    stored_before = {k: v for k, v in live.items()
                     if k not in ("moved", "previous_dossier_id")}
    stored_before["idempotent_replay"] = True
    _conforms("update_time_entry", stored_before)
