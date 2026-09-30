"""The read-only calendar-drift report (revue du 2026-09-30).

The calendar fix recomputes nothing already stored. ``scripts.
verifier_calendrier_delais`` lists what the old calendar left behind; these
tests pin its two pure predicates and that it CANNOT write (no ``--apply``,
no Firestore write verb anywhere in its source).
"""

import ast
import os
import pathlib
import sys
from datetime import date, datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import dossier as _dossier_model  # noqa: F401  (import path)
    from models import protocol as _protocol_model  # noqa: F401
    from scripts import verifier_calendrier_delais as script

UTC = timezone.utc


def _d(y, m, day):
    return datetime(y, m, day, tzinfo=UTC)


# ── Dossiers ─────────────────────────────────────────────────────────────


def test_a_prescription_stored_on_the_retired_sunday_substitute_is_reported():
    """Droit d'action 2026-06-24 + 3 ans = Sun 24 June 2029. The old
    calendar invented a Monday 25 June holiday and stored Tue 26 — one day
    LATE; art. 2879 C.c.Q. + art. 61(23) L.i. give Mon 25."""
    doc = {"id": "d1", "prescription_type": "3_ans",
           "droit_action_date": _d(2026, 6, 24),
           "prescription_date": _d(2029, 6, 26)}
    assert script.prescription_drift(doc) == (date(2029, 6, 26),
                                              date(2029, 6, 25))
    # The input is never mutated (the report works on a copy).
    assert doc["prescription_date"] == _d(2029, 6, 26)


def test_an_up_to_date_prescription_is_not_reported():
    doc = {"id": "d1", "prescription_type": "3_ans",
           "droit_action_date": _d(2026, 6, 24),
           "prescription_date": _d(2029, 6, 25)}
    assert script.prescription_drift(doc) is None


def test_a_manual_date_the_model_never_recomputes_is_not_reported():
    """No droit d'action → a save leaves the date alone → no drift."""
    doc = {"id": "d1", "prescription_type": "",
           "prescription_date": _d(2029, 6, 26)}
    assert script.prescription_drift(doc) is None


# ── Protocol steps ───────────────────────────────────────────────────────


def _step(stored, **over):
    return {"deadline_offset_days": 15, "deadline_date": stored,
            "status": "à_venir", **over}


def test_a_step_on_the_old_calendars_date_is_reported():
    """Thu 11 Dec 2025 + 15 = Fri 26 Dec: juridical for the old calendar,
    a holiday in procedure (art. 82 C.p.c.) → Mon 29."""
    proto = {"start_date": _d(2025, 12, 11)}
    assert script.step_drift(proto, _step(_d(2025, 12, 26))) == (
        date(2025, 12, 26), date(2025, 12, 29))


def test_a_current_completed_or_hand_moved_step_is_not_reported():
    proto = {"start_date": _d(2025, 12, 11)}
    assert script.step_drift(proto, _step(_d(2025, 12, 29))) is None
    assert script.step_drift(
        proto, _step(_d(2025, 12, 26), status="complété")) is None
    assert script.step_drift(proto, _step(_d(2026, 1, 20))) is None
    # A custom step (no offset) never is.
    assert script.step_drift(
        proto, _step(_d(2025, 12, 26), deadline_offset_days=None)) is None


# ── It cannot write ──────────────────────────────────────────────────────


def test_the_report_has_no_write_path():
    src = (pathlib.Path(script.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    verbs = {"set", "update", "delete", "add", "create", "batch",
             "transaction", "commit"}
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & verbs), called & verbs
    # No option parsing at all: any argument — « --apply » first — is
    # refused before Firestore is even imported.
    assert "argparse" not in src
    assert script.main(["--apply"]) == 2
