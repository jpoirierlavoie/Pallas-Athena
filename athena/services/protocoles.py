"""Protocol orchestration — the web routes today, the MCP connector from lot 1b.

The RULES live in ``models/protocol.py`` (whitelists, the one-actif rule,
the C.p.c. locks, the non-toggle step status, the recompute preservation),
where every path meets them. What lives HERE is what crosses into another
subsystem after a protocol write has committed, so that two callers cannot
do it two ways:

* **creation** builds the protocol's snapshot fields from the dossier the
  caller resolved (the tribunal as ``court``, the file number and title
  labels) — hoisted from ``routes/protocols.protocol_create`` — and creates
  the linked tasks only when ASKED (``create_linked_tasks`` defaults to
  False; the web wizard passes its checkbox, unchecked by default);
* **alignment**: nothing kept a linked task's due date in step with its
  step — ``create_linked_tasks`` copies the date once, at creation. A step
  whose deadline moves (a new start date, an edited deadline) now carries
  its task along, IF the task still sat on the step's old date: a due date
  the lawyer set by hand is his, and is left alone and reported. Every
  task written has its DAV collection bumped (the tasks are DAV-exposed;
  the phone learns only through the CTag).

Every function returns ``(result, errors, report)`` — the model's pair plus
what the orchestration did, in counts a banner (or a tool payload) can say.
Nothing here raises once the model has committed: a failure after the
commit is logged by ids and reported.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

from dav.sync import bump_ctag, collection_for
from models import concurrency
from models import protocol as protocol_model
from models import task as task_model
from utils.logging_setup import log_protocol_event, log_unexpected

DEFAULT_TITLE = "Protocole de l'instance"

# The outcomes of aligning ONE linked task, in the report's key order.
ALIGN_OUTCOMES: tuple[str, ...] = (
    "aligned",     # moved with its step (and its collection bumped)
    "unchanged",   # already on the new date
    "diverged",    # its due date was not the step's old one: left alone
    "closed",      # terminée / annulée: a closed task's date is history
    "missing",     # not found — or unreadable (get_task fails open)
    "failed",      # the write was refused, raised, or lost two races
)


def _day(value) -> Optional[date]:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _empty_alignment() -> dict:
    return {key: 0 for key in ALIGN_OUTCOMES}


# ── Creation ────────────────────────────────────────────────────────────


def create_protocol(
    dossier: dict,
    protocol_type: str,
    start_date: Optional[datetime],
    *,
    title: str = "",
    notes: str = "",
    create_linked_tasks: bool = False,
) -> tuple[Optional[dict], list[str], dict]:
    """Create a protocol on a dossier the CALLER has resolved.

    ``court`` is the dossier's ``tribunal`` (a ``court`` key never existed
    on dossiers — the copy used to write "" on every protocol). Linked
    tasks are created only when asked: ``create_linked_tasks`` defaults to
    False here, and the web wizard passes its own checkbox explicitly.
    Report: ``tasks_created``, ``tasks_linked``, ``tasks_failed``.
    """
    report = {"tasks_created": 0, "tasks_linked": 0, "tasks_failed": 0}
    data = {
        "title": (title or "").strip() or DEFAULT_TITLE,
        "court": dossier.get("tribunal", "") or "",
        "dossier_file_number": dossier.get("file_number", "") or "",
        "dossier_title": dossier.get("title", "") or "",
        "notes": (notes or "").strip(),
    }
    protocol, errors = protocol_model.create_protocol(
        dossier.get("id", ""), protocol_type, start_date, data,
        auto_create_tasks=False,
    )
    if protocol is None:
        return None, errors, report
    if create_linked_tasks and protocol.get("steps"):
        made = protocol_model.create_linked_tasks(
            protocol["id"], protocol, protocol["steps"])
        report = {"tasks_created": made["created"],
                  "tasks_linked": made["linked"],
                  "tasks_failed": made["failed"]}
    return protocol, [], report


def _stored_step(protocol_id: str, step_id: str) -> Optional[dict]:
    protocol = protocol_model.get_protocol(protocol_id) or {}
    return next(
        (s for s in protocol.get("steps", []) or [] if s.get("id") == step_id),
        None,
    )


def add_step(
    protocol_id: str,
    step_data: dict,
    *,
    create_linked_task: bool = False,
) -> tuple[Optional[dict], list[str], dict]:
    """Add a custom step; optionally create and link its task.

    Report: ``task_created``, ``task_linked``. With a task, the step is
    re-read so the caller holds its CURRENT etag (the link is a step
    write). A protocol that cannot be re-read for the task's labels leaves
    the step created and the task not — reported, never raised. A
    protocol that is not « actif » refuses the step for every caller (the
    model's rule since D17, 2026-09-27).
    """
    report = {"task_created": False, "task_linked": False}
    step, errors = protocol_model.add_step(protocol_id, step_data)
    if step is None or not create_linked_task:
        return step, errors, report
    protocol = protocol_model.get_protocol(protocol_id)
    if not protocol:
        log_unexpected("protocol step: linked task skipped, protocol unreadable",
                       protocol_id=protocol_id, step_id=step.get("id"))
        return step, [], report
    made = protocol_model.create_linked_tasks(protocol_id, protocol, [step])
    report = {"task_created": made["created"] == 1,
              "task_linked": made["linked"] == 1}
    return _stored_step(protocol_id, step["id"]) or step, [], report


# ── Edits ───────────────────────────────────────────────────────────────


def update_step(
    protocol_id: str,
    step_id: str,
    data: dict,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Edit a step; a changed deadline carries its linked task along.

    Report: the :data:`ALIGN_OUTCOMES` counts (all zero when the deadline
    did not change or the step links no task), plus ``tasks`` — the entry
    :func:`align_linked_tasks_detailed` made for the linked task, if the
    deadline moved a step that links one. A protocol that is not « actif »
    refuses the edit for every caller (the model's rule since D17).
    """
    step, errors = protocol_model.update_step(
        protocol_id, step_id, data, expected_etag=expected_etag)
    if step is None:
        return None, errors, {**_empty_alignment(), "tasks": []}
    moved = step.pop("_deadline_changed", None)
    counts, tasks = _empty_alignment(), []
    if moved is not None and step.get("linked_task_id"):
        counts, tasks = align_linked_tasks_detailed([{
            "step_id": step_id,
            "old": moved["old"],
            "new": moved["new"],
            "linked_task_id": step["linked_task_id"],
        }], protocol_id=protocol_id)
    return step, [], {**counts, "tasks": tasks}


def set_step_status(
    protocol_id: str,
    step_id: str,
    target: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Complete or reopen a step — the model's non-toggle rule, whole.

    The gate, the reactivation of an auto-closed protocol, the task
    cascade (with its CTag bump) and the completion check all live in
    ``protocol_model.set_step_status``; this is the one door the routes and
    the connector use, so the order cannot be re-implemented elsewhere.
    """
    return protocol_model.set_step_status(
        protocol_id, step_id, target, expected_etag=expected_etag)


def update_protocol(
    protocol_id: str,
    data: dict,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Edit a protocol; a new start date moves the steps and their tasks.

    The recompute happens in the model's own transaction; the linked tasks
    of the steps it MOVED are then aligned. Report: ``moved``,
    ``preserved_completed``, ``preserved_confirmed`` and the
    :data:`ALIGN_OUTCOMES` counts — what a banner says — plus the detail a
    tool payload names step by step: ``moved_steps`` (``{step_id, old,
    new, linked_task_id}``), ``preserved_steps`` (``{step_id, reason}``)
    and ``tasks`` (:func:`align_linked_tasks_detailed`'s entries).
    """
    protocol, errors = protocol_model.update_protocol(
        protocol_id, data, expected_etag=expected_etag)
    report = {"moved": 0, "preserved_completed": 0, "preserved_confirmed": 0,
              **_empty_alignment(), "moved_steps": [], "preserved_steps": [],
              "tasks": []}
    if protocol is None:
        return None, errors, report
    recompute = protocol.pop("_recompute", None) or {"moved": [],
                                                    "preserved": []}
    report["moved"] = len(recompute["moved"])
    for entry in recompute["preserved"]:
        report[f"preserved_{entry['reason']}"] += 1
    counts, tasks = align_linked_tasks_detailed(recompute["moved"],
                                                protocol_id=protocol_id)
    report.update(counts)
    report["moved_steps"] = [dict(e) for e in recompute["moved"]]
    report["preserved_steps"] = [dict(e) for e in recompute["preserved"]]
    report["tasks"] = tasks
    return protocol, [], report


# ── Alignment ───────────────────────────────────────────────────────────


def _align_one(task_id: str, old, new) -> tuple[str, Optional[str]]:
    """``(outcome, dossier_id)`` of carrying one task from *old* to *new*.

    Compare-and-set against the task just read — the decision (« it still
    sat on the old date ») was made on that version; one re-read is
    allowed when a write lands in between.
    """
    for _attempt in range(2):
        task = task_model.get_task(task_id)
        if task is None:
            return "missing", None
        if task.get("status") in ("terminée", "annulée"):
            return "closed", None
        due = _day(task.get("due_date"))
        if due == _day(new):
            return "unchanged", None
        if due != _day(old):
            return "diverged", None
        doc, errors = task_model.update_task(
            task_id, {"due_date": new},
            expected_etag=concurrency.etag_of(task),
        )
        if errors and concurrency.is_stale(errors):
            continue
        if errors or doc is None:
            return "failed", None
        return "aligned", doc.get("dossier_id")
    return "failed", None


def align_linked_tasks(
    moved: Iterable[dict], *, protocol_id: str = ""
) -> dict:
    """Carry each moved step's linked task to the step's new deadline.

    *moved* holds ``{step_id, old, new, linked_task_id}`` entries (the
    model's recompute report, or an edited step). A task moves only if its
    due date still equals the step's OLD deadline — one the lawyer changed
    by hand, or cleared, stays his (``diverged``); a closed task is left
    alone. Each DAV collection a task was written in is bumped once, after
    the writes, each bump guarded on its own. Returns the
    :data:`ALIGN_OUTCOMES` counts; never raises.
    """
    counts, _tasks = align_linked_tasks_detailed(moved,
                                                 protocol_id=protocol_id)
    return counts


def align_linked_tasks_detailed(
    moved: Iterable[dict], *, protocol_id: str = ""
) -> tuple[dict, list[dict]]:
    """:func:`align_linked_tasks`, plus what happened to EACH task.

    Returns ``(counts, tasks)``: the :data:`ALIGN_OUTCOMES` counts, and one
    ``{task_id, step_id, outcome, ctag_bumped}`` entry per step that links
    a task, in *moved*'s order. ``ctag_bumped`` is true only for an
    ``aligned`` task whose collection's bump succeeded — the phone learns
    of a moved task through that bump alone, so a caller reporting a sync
    must be able to say when it did not happen. Never raises.
    """
    report = _empty_alignment()
    entries: list[dict] = []
    touched: set[str] = set()
    for entry in moved:
        task_id = entry.get("linked_task_id")
        if not task_id:
            continue
        try:
            outcome, dossier_id = _align_one(
                task_id, entry.get("old"), entry.get("new"))
        except Exception:
            log_unexpected("protocol: linked task alignment failed",
                           protocol_id=protocol_id, task_id=task_id)
            outcome, dossier_id = "failed", None
        report[outcome] += 1
        collection = collection_for(dossier_id) if outcome == "aligned" else ""
        if collection:
            touched.add(collection)
        entries.append({"task_id": task_id,
                        "step_id": entry.get("step_id", ""),
                        "outcome": outcome, "collection": collection})
    bumped: dict[str, bool] = {}
    for name in sorted(touched):
        try:
            bump_ctag(name)
            bumped[name] = True
        except Exception:
            bumped[name] = False
            log_unexpected("protocol: linked task alignment CTag bump failed",
                           protocol_id=protocol_id)
    tasks = [
        {"task_id": e["task_id"], "step_id": e["step_id"],
         "outcome": e["outcome"],
         "ctag_bumped": bool(e["collection"]) and bumped.get(
             e["collection"], False)}
        for e in entries
    ]
    if any(report.values()):
        log_protocol_event(
            "linked_tasks_aligned", protocol_id,
            outcome="refused" if report["failed"] or report["missing"]
            else "success",
            **report,
        )
    return report, tasks
