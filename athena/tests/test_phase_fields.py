"""Phase O — the phase/sous_phase fields on the three phased models.

CI-only in the folders/document_naming sense: imports models (which builds the
Firestore client at import) but only exercises PURE code paths — ``_validate``,
``_default_doc`` and the shared ``phases`` helpers. No Firestore I/O.
"""

import os

import pytest

# The propagation test imports dav.sync, whose chain reaches config.py —
# the same bootstrap the DAV tests use.
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from models import expense as expense_model
from models import task as task_model
from models import time_entry as time_entry_model
from utils import phases


_MODELS = pytest.mark.parametrize(
    "model", [time_entry_model, expense_model, task_model],
    ids=["time_entry", "expense", "task"],
)


# ── Defaults + re-exports ───────────────────────────────────────────────────


@_MODELS
def test_default_doc_carries_blank_phase_pair(model):
    doc = model._default_doc()
    assert doc["phase"] == ""
    assert doc["sous_phase"] == ""


@_MODELS
def test_vocabulary_is_reexported_never_redefined(model):
    # The taxonomie.py pattern: the model re-exports the pure module's
    # constants so there is exactly one place to edit.
    assert model.VALID_PHASES is phases.VALID_PHASES
    assert model.VALID_SOUS_PHASES is phases.VALID_SOUS_PHASES
    assert model.PHASE_LABELS is phases.PHASE_LABELS
    assert model.SOUS_PHASE_LABELS is phases.SOUS_PHASE_LABELS


# ── Presence-gated validation (the domaine/action pattern) ──────────────────


def _valid_base(model) -> dict:
    """A payload that passes each model's own required-field checks."""
    if model is task_model:
        return {"title": "Préparer la défense"}
    base = {
        "dossier_id": "d-1",
        "date": object(),  # truthy stand-in; _validate only checks presence
        "description": "Travail",
    }
    if model is time_entry_model:
        base.update({"hours": 1.0, "rate": 30000, "amount": 30000})
    else:
        base.update({"amount": 5000})
    return base


@_MODELS
def test_absent_keys_are_tolerated(model):
    # A legacy doc read straight from Firestore has neither key — an
    # unconditional check would lock it out of editing entirely.
    assert model._validate(_valid_base(model)) == []


@_MODELS
def test_blank_pair_is_valid(model):
    data = {**_valid_base(model), "phase": "", "sous_phase": ""}
    assert model._validate(data) == []


@_MODELS
def test_valid_pair_passes(model):
    data = {**_valid_base(model), "phase": "CTS", "sous_phase": "CTS-02"}
    assert model._validate(data) == []


@_MODELS
def test_unknown_phase_refused(model):
    data = {**_valid_base(model), "phase": "ZZZ"}
    assert "Phase invalide." in model._validate(data)


@_MODELS
def test_unknown_sous_phase_refused(model):
    data = {**_valid_base(model), "phase": "CTS", "sous_phase": "CTS-77"}
    assert "Sous-phase invalide." in model._validate(data)


@_MODELS
def test_cross_prefix_refused(model):
    data = {**_valid_base(model), "phase": "INT", "sous_phase": "CTS-02"}
    assert (
        "La sous-phase choisie n'appartient pas à la phase choisie."
        in model._validate(data)
    )


# ── D-4 default imputation ─────────────────────────────────────────────────


def test_apply_sous_phase_default():
    doc = {"phase": "CTS", "sous_phase": ""}
    phases.apply_sous_phase_default(doc)
    assert doc["sous_phase"] == "CTS-00"

    doc = {"phase": "", "sous_phase": ""}
    phases.apply_sous_phase_default(doc)
    assert doc["sous_phase"] == ""  # a blank phase never invents an imputation

    doc = {"phase": "CTS", "sous_phase": "CTS-02"}
    phases.apply_sous_phase_default(doc)
    assert doc["sous_phase"] == "CTS-02"  # explicit choice untouched

    doc = {}
    phases.apply_sous_phase_default(doc)
    assert "sous_phase" not in doc  # absent keys stay absent


# ── Protocol templates (the D-5 keystone) ──────────────────────────────────


def test_template_steps_all_carry_a_coherent_phase():
    from models import protocol as protocol_model

    for tmpl in (protocol_model.CQ_TEMPLATE_STEPS, protocol_model.CS_TEMPLATE_STEPS):
        for step in tmpl:
            assert step["phase"] in phases.PHASES, step["title"]
            assert step["sous_phase"] in phases.SOUS_CODES, step["title"]
            assert phases.phase_of(step["sous_phase"]) == step["phase"], step["title"]


def test_template_step_mapping_is_pinned():
    # LEGAL CONTENT — approved with the Phase O plan (2026-08-10). A change
    # here must be the practitioner's decision, never a refactor side-effect.
    from models import protocol as protocol_model

    cq = [(s["order"], s["sous_phase"]) for s in protocol_model.CQ_TEMPLATE_STEPS]
    assert cq == [
        (1, "INT-02"), (2, "MEE-01"), (3, "PRL-00"), (4, "CTS-01"),
        (5, "MEE-03"), (6, "PRD-03"), (7, "INS-01"),
    ]
    cs = [(s["order"], s["sous_phase"]) for s in protocol_model.CS_TEMPLATE_STEPS]
    assert cs == [
        (1, "INT-02"), (2, "CTS-00"), (3, "INT-03"), (4, "INR-00"),
        (5, "EXP-02"), (6, "PRD-03"), (7, "MEE-03"), (8, "INS-01"),
    ]


def test_default_step_carries_blank_pair_and_validates():
    from models import protocol as protocol_model

    step = protocol_model._default_step()
    assert step["phase"] == "" and step["sous_phase"] == ""
    assert protocol_model._validate_step({"title": "Étape", "phase": "ZZZ"}) == [
        "Phase invalide."
    ]
    assert protocol_model._validate_step({"title": "Étape"}) == []


def test_auto_created_task_inherits_step_phase(monkeypatch):
    import dav.sync as dav_sync
    import models.task
    from models import protocol as protocol_model

    captured: list[dict] = []
    monkeypatch.setattr(
        models.task, "create_task",
        lambda data: (captured.append(data) or {**data, "id": "t-1"}, []),
    )
    monkeypatch.setattr(dav_sync, "bump_ctag", lambda name: "ctag")
    monkeypatch.setattr(dav_sync, "collection_for", lambda d: f"dossier:{d}")

    class _FakeRef:
        def collection(self, *_):
            return self

        def document(self, *_):
            return self

        def update(self, *_):
            return None

    monkeypatch.setattr(protocol_model, "db", _FakeRef())

    protocol_model._auto_create_tasks_for_steps(
        "p-1",
        {"dossier_id": "d-1", "title": "Protocole", "dossier_file_number": "", "dossier_title": ""},
        [{"id": "s-1", "title": "Réponse", "deadline_date": None,
          "phase": "CTS", "sous_phase": "CTS-00"}],
    )
    assert captured and captured[0]["phase"] == "CTS"
    assert captured[0]["sous_phase"] == "CTS-00"


def test_get_current_phase_for_dossier(monkeypatch):
    from models import protocol as protocol_model

    steps = [
        {"order": 1, "status": "complété", "phase": "INT", "sous_phase": "INT-02"},
        {"order": 2, "status": "à_venir", "phase": "CTS", "sous_phase": "CTS-00"},
        {"order": 3, "status": "à_venir", "phase": "INR", "sous_phase": "INR-00"},
    ]
    monkeypatch.setattr(
        protocol_model, "get_protocol_for_dossier",
        lambda d, active_only=True: {"id": "p-1", "steps": steps},
    )
    assert protocol_model.get_current_phase_for_dossier("d-1") == ("CTS", "CTS-00")

    # No dossier → never even queries; no protocol → blank pair.
    assert protocol_model.get_current_phase_for_dossier("") == ("", "")
    monkeypatch.setattr(
        protocol_model, "get_protocol_for_dossier",
        lambda d, active_only=True: None,
    )
    assert protocol_model.get_current_phase_for_dossier("d-1") == ("", "")


# ── DAV — VTODO serialization round-trip (D-7, spec §6) ────────────────────


def _task_doc(**over) -> dict:
    doc = task_model._default_doc()
    doc.update({
        "id": "t-1",
        "title": "Préparer la défense",
        "category": "rédaction",
        "vtodo_uid": "uid-1",
    })
    doc.update(over)
    return doc


def test_vtodo_emits_phase_code_in_categories_and_xprops():
    ical = task_model.task_to_vtodo(_task_doc(phase="CTS", sous_phase="CTS-02"))
    # The CODE, never the label (D-7): renaming a phase must touch no VTODO.
    assert "CTS" in ical and "Contestation" not in ical
    assert "Rédaction" in ical  # the category label still rides along
    assert "X-PALLAS-PHASE:CTS" in ical
    assert "X-PALLAS-SOUS-PHASE:CTS-02" in ical


def test_vtodo_omits_phase_props_when_unphased():
    ical = task_model.task_to_vtodo(_task_doc())
    assert "X-PALLAS-PHASE" not in ical
    assert "X-PALLAS-SOUS-PHASE" not in ical


def test_vtodo_round_trip_preserves_phase():
    ical = task_model.task_to_vtodo(_task_doc(phase="CTS", sous_phase="CTS-02"))
    parsed = task_model.vtodo_to_task(ical)
    assert parsed["phase"] == "CTS"
    assert parsed["sous_phase"] == "CTS-02"


def _vtodo(extra_lines: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VTODO\r\nUID:uid-1\r\nDTSTAMP:20260810T120000Z\r\n"
        "SUMMARY:Tâche\r\n" + extra_lines + "END:VTODO\r\nEND:VCALENDAR\r\n"
    )


def test_non_effacement_absent_props_omit_keys():
    # A client that drops the phase properties on a plain edit must not wipe
    # the stored value: absent property → absent key → merge keeps stored.
    parsed = task_model.vtodo_to_task(_vtodo(""))
    assert "phase" not in parsed
    assert "sous_phase" not in parsed


def test_categories_fallback_accepts_only_phase_codes():
    parsed = task_model.vtodo_to_task(_vtodo("CATEGORIES:Suivi,CTS\r\n"))
    assert parsed["phase"] == "CTS"
    assert "sous_phase" not in parsed
    # No phase code among the categories → key omitted.
    parsed = task_model.vtodo_to_task(_vtodo("CATEGORIES:Suivi,Urgent\r\n"))
    assert "phase" not in parsed


def test_unknown_phase_code_is_ignored():
    parsed = task_model.vtodo_to_task(_vtodo("X-PALLAS-PHASE:ZZZ\r\n"))
    assert "phase" not in parsed


def test_contradictory_sous_phase_is_ignored():
    parsed = task_model.vtodo_to_task(
        _vtodo("X-PALLAS-PHASE:INT\r\nX-PALLAS-SOUS-PHASE:CTS-02\r\n")
    )
    assert parsed["phase"] == "INT"
    assert "sous_phase" not in parsed


def test_sous_phase_alone_derives_its_parent():
    parsed = task_model.vtodo_to_task(_vtodo("X-PALLAS-SOUS-PHASE:CTS-02\r\n"))
    assert parsed["phase"] == "CTS"
    assert parsed["sous_phase"] == "CTS-02"


def test_update_task_repairs_phase_only_retag(monkeypatch):
    # A DAV client that kept CATEGORIES but stripped the X- props sends a
    # phase with no sub-code; when the stored sub-code contradicts the new
    # phase, the sub-code follows the phase (-00) instead of 422-ing the PUT.
    stored = _task_doc(phase="CTS", sous_phase="CTS-02", status="à_faire")
    monkeypatch.setattr(task_model, "get_task", lambda tid: dict(stored))

    written: dict = {}

    class _FakeDb:
        def collection(self, *_):
            return self

        def document(self, *_):
            return self

        def set(self, doc):
            written.update(doc)

    monkeypatch.setattr(task_model, "db", _FakeDb())

    updated, errors = task_model.update_task("t-1", {"phase": "INT"})
    assert errors == []
    assert updated["phase"] == "INT"
    assert updated["sous_phase"] == "INT-00"
    assert written["sous_phase"] == "INT-00"

    # Same phase re-sent (the ordinary round-trip): sub-code untouched.
    updated, errors = task_model.update_task("t-1", {"phase": "CTS"})
    assert errors == []
    assert updated["sous_phase"] == "CTS-02"


def test_validate_pair_gates_on_presence_not_truth():
    # The gate is `"phase" in data`, not the value's truthiness — the update
    # path merges {**existing, **data} and must not re-reject a legacy doc.
    assert phases.validate_pair({}) == []
    assert phases.validate_pair({"phase": ""}) == []
    assert phases.validate_pair({"sous_phase": ""}) == []
    assert phases.validate_pair({"phase": "ZZZ"}) == ["Phase invalide."]
    # sous_phase alone, valid: tolerated at the model layer (MCP derives the
    # parent before writing; a hand-crafted POST without phase stays coherent
    # because the prefix IS the parent).
    assert phases.validate_pair({"sous_phase": "CTS-02"}) == []
