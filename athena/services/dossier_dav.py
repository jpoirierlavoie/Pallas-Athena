"""A dossier's DavX5 visibility — the drain on closing, the restore on
reopening, and the repair between the two (lot 4a).

A dossier's ``/dav/dossier-{id}/`` collection is advertised to DavX5 only
while its status is one of :data:`dav.sync.ACTIVE_DOSSIER_STATUSES`
(``actif``, ``en_attente``). When it leaves that set the collection stops
being listed at the root, so the phone must first be told that every task,
note and hearing it holds from that dossier is gone: a tombstone per
resource, then a CTag bump. That is the DRAIN. Reopening removes those
tombstones for the resources that are still there and bumps again — the
RESTORE — so the items sync back.

This used to live in ``routes/dossiers._sync_dossier_dav_visibility``,
called AFTER the status commit, and it failed OPEN at every step: its three
readers (``list_tasks`` / ``list_notes`` / ``list_hearings``) answer a
Firestore blip with ``[]``, so a drain on a blip tombstoned NOTHING, bumped
the CTag, and let the collection leave discovery — everything stranded on
the phone for good, with no error anywhere and no way to re-run it (the
function returned early whenever the status had not changed, and the status
had already been written). It had no test. What lives HERE instead:

* :func:`dav_member_ids` — the collection's membership, read through the
  STRICT readers (``list_tasks_strict``, ``list_notes_strict(
  include_analyse=True)``, ``list_hearings_strict(include_unconfirmed=
  False)`` — the exact membership of ``dav.dossier_collections.
  _collection_members``); a read failure raises :class:`DavMembersUnreadable`,
  an empty or blank dossier id raises ``ValueError`` before any query (the
  readers read the WHOLE collection for a falsy id);
* :func:`apply_dav_visibility` — the writes, DERIVED from a status and
  never from a transition: tombstones (inactive) or tombstone removals
  (active) in chunks, the bump in the final chunk (so up to 449 resources
  are one atomic commit). Failures PROPAGATE;
* :func:`run_status_transition` — the orchestration around a status write,
  FAIL-CLOSED: the members are read BEFORE the commit, and a read failure
  refuses the transition with nothing written; after the commit they are
  read again (a resource created meanwhile is drained too) and the
  visibility applied; a failure there cannot un-write the status, so it is
  REPORTED (``complete=False``) — never claimed a success, never raised;
* :func:`resync_dossier_dav_visibility` — the separate REPAIR: re-applies
  the visibility of the dossier's CURRENT status, re-runnable at will. The
  web « Resynchroniser le téléphone » button calls it; the connector will
  reach the same code by re-asking for the status the dossier already has.

Nothing here writes a task, a note or a hearing: tombstones live in
``dav_sync`` and are DAV markers only. The records stay in Firestore and in
the web UI whatever the dossier's status.

``en_attente`` is ACTIVE: a pending dossier keeps its collection on the
phone, and a transition between ``actif`` and ``en_attente`` writes nothing
here. (It is also still in the prescription alerts since lot 0b —
``models.dossier.PRESCRIPTION_ALERT_STATUSES`` — so « en attente » silences
neither the phone nor the limitation-period warnings; only ``fermé`` and
``archivé`` do both.)

Two known limits, both pre-existing and both repaired by a resync: a phone
offline for more than ``TOMBSTONE_TTL_DAYS`` (30) after a closure never
receives the deletions (the tombstones are pruned); and DavX5 may refresh
its collection list before syncing the drain (a client-side race).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from dav.sync import (
    ACTIVE_DOSSIER_STATUSES,
    collection_for,
    record_tombstones_bulk,
    remove_tombstones_bulk,
)
from models import dossier as dossier_model
from models import hearing as hearing_model
from models import note as note_model
from models import provenance
from models import task as task_model
from utils.logging_setup import log_dossier_event, log_unexpected
from utils.tracing_setup import span

# Directions — what a visibility write does to the collection.
DRAIN = "drain"
RESTORE = "restore"
NONE = "none"

# Machine-stable reason codes (logs, reports — the caller maps them to
# French; never shown as such).
ERR_MEMBERS_UNREADABLE = "members_unreadable"
ERR_MEMBERS_REREAD_FAILED = "members_reread_failed"
ERR_WRITE_FAILED = "write_failed"
ERR_STATUS_UNVERIFIED = "status_unverified"
ERR_STATUS_MOVING = "status_moving"
ERR_DOSSIER_UNREADABLE = "dossier_unreadable"
ERR_DOSSIER_NOT_FOUND = "dossier_not_found"

# The refusal of a transition whose members could not be read — returned
# BEFORE the commit, so « rien n'a été enregistré » is true.
MEMBERS_UNREADABLE_ERROR = (
    "Les tâches, les notes et les audiences du dossier n'ont pas pu être "
    "lues pour mettre le téléphone à jour : rien n'a été enregistré. "
    "Réessayez dans un instant."
)


class DavMembersUnreadable(Exception):
    """A strict membership read failed — the members are UNKNOWN, never
    « none ». The original exception is chained (``__cause__``)."""


@dataclass(frozen=True)
class DavVisibility:
    """What a visibility write did.

    ``direction`` — :data:`DRAIN`, :data:`RESTORE` or :data:`NONE`;
    ``status`` — the status the visibility was applied for (``""`` when none
    was); ``resources`` — how many member ids the write covered;
    ``ctag_bumped`` — the final chunk (which carries the bump) committed;
    ``complete`` — every write the direction requires is committed (a
    :data:`NONE` direction is complete by definition); ``error`` — a
    reason code when not complete, ``""`` otherwise.
    """

    direction: str
    status: str = ""
    resources: int = 0
    ctag_bumped: bool = False
    complete: bool = True
    error: str = ""


@dataclass(frozen=True)
class StatusTransition:
    """The outcome of :func:`run_status_transition`.

    ``errors`` non-empty means NOTHING was written — neither the status nor
    any DAV marker (the refusal of an unreadable membership, or the commit's
    own refusal). Otherwise ``doc`` is what the commit returned and ``dav``
    says what the phone will see — ``dav.complete`` False means the status
    IS written but the phone is not (yet) in step: offer the resync.
    """

    doc: Optional[dict]
    errors: list[str] = field(default_factory=list)
    dav: DavVisibility = field(default_factory=lambda: DavVisibility(NONE))


def _require_dossier_id(dossier_id: object) -> None:
    """First statement of every entry point: an empty or blank id would
    reach the readers' « whole collection » branch — and the drain would
    tombstone the firm's every task under ``dossier:``."""
    if not isinstance(dossier_id, str) or not dossier_id.strip():
        raise ValueError("a dossier's DAV visibility needs its dossier id")


def is_active(status: object) -> bool:
    """Whether a dossier in *status* has its collection on the phone."""
    return status in ACTIVE_DOSSIER_STATUSES


def planned_direction(
    old_status: Optional[str], new_status: str, *, reconcile_always: bool
) -> str:
    """The visibility a transition requires.

    DERIVED from the target status whenever the caller asks to reconcile
    (*reconcile_always* — the connector re-asking for a status, a repair)
    or does not know the old status (``None`` — a failed pre-read): the
    target's visibility is then (re)applied, whatever the old one was. With
    a known old status and no reconciliation (the web form, which saves
    every field on every edit), only a CROSSING of the active boundary
    writes anything — ``actif`` ↔ ``en_attente``, or ``fermé`` ↔
    ``archivé``, is :data:`NONE`.
    """
    target = RESTORE if is_active(new_status) else DRAIN
    if reconcile_always or old_status is None:
        return target
    if is_active(old_status) == is_active(new_status):
        return NONE
    return target


def _ordered_unique(ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for rid in ids:
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def dav_member_ids(dossier_id: str) -> list[str]:
    """The ids of every resource ``/dav/dossier-{id}/`` lists while active.

    Hearings (confirmed only), tasks, and notes INCLUDING the analyse note —
    the membership of ``dav.dossier_collections._collection_members``, read
    through strict readers. Raises :class:`DavMembersUnreadable` on a read
    failure, ``ValueError`` on an empty or blank id (before any query).
    """
    _require_dossier_id(dossier_id)
    try:
        hearings = hearing_model.list_hearings_strict(
            dossier_id, include_unconfirmed=False)
        tasks = task_model.list_tasks_strict(dossier_id)
        notes = note_model.list_notes_strict(dossier_id, include_analyse=True)
    except Exception as exc:
        raise DavMembersUnreadable(dossier_id) from exc
    return _ordered_unique(
        str(row.get("id") or "") for row in (*hearings, *tasks, *notes)
    )


def apply_dav_visibility(
    dossier_id: str, status: str, member_ids: Iterable[str]
) -> DavVisibility:
    """Make the phone's view of the collection match *status*.

    Inactive *status* → a tombstone per member id; active → the members'
    tombstones REMOVED (a tombstone of a resource deleted meanwhile is not a
    member, so it survives — it still has a deletion to report). Either way
    the CTag is bumped in the FINAL chunk (``dav.sync``'s
    ``record_tombstones_bulk`` / ``remove_tombstones_bulk`` with
    ``bump=True``), even with no member at all. Every write is idempotent,
    so a failed call is simply re-run. Failures PROPAGATE.
    """
    _require_dossier_id(dossier_id)
    ids = _ordered_unique(member_ids)
    sync_name = collection_for(dossier_id)
    if is_active(status):
        remove_tombstones_bulk(sync_name, ids, bump=True)
        direction = RESTORE
    else:
        record_tombstones_bulk(sync_name, ids, bump=True)
        direction = DRAIN
    return DavVisibility(
        direction=direction, status=status, resources=len(ids),
        ctag_bumped=True, complete=True,
    )


def _settle(dossier_id: str, status: str, pre_ids: list[str],
            *, attempts: int = 2) -> DavVisibility:
    """Apply the visibility of *status* after a committed write, then check
    that the stored status did not cross the active boundary meanwhile.

    The members are READ AGAIN here: for a drain, a resource created
    between the caller's pre-read and the commit (a phone PUT into the
    still-active collection) must be tombstoned too, so the two sets are
    united; if this re-read fails, the pre-read set is drained and the
    result is INCOMPLETE (a newcomer may be missed — a resync finds it). For
    a restore, only the re-read set is un-tombstoned: on a failed re-read
    the bump goes out alone (a tombstone of a LIVE resource is never
    reported — the sync-collection REPORT filters them — so leaving one is
    harmless, while removing the tombstone of a resource deleted in the
    window would erase its deletion), and the restore is still complete.

    The check afterwards: another transition committed in between (a second
    tab, the connector) could have its writes land BEFORE ours — a restore
    removing a concurrent drain's tombstones would strand the phone. So the
    stored status is re-read; if it no longer matches what was applied, the
    visibility of the STORED status is applied once more. Never raises.
    """
    applied_status = status
    for attempt in range(attempts):
        drain = not is_active(applied_status)
        reread_ok = True
        try:
            ids = dav_member_ids(dossier_id)
        except DavMembersUnreadable:
            log_unexpected("dossier dav members re-read failed",
                           dossier_id=dossier_id)
            ids, reread_ok = [], False
        if drain:
            ids = _ordered_unique([*pre_ids, *ids])
        try:
            with span("dossier.dav_visibility", dossier_id=dossier_id,
                      direction=DRAIN if drain else RESTORE,
                      resource_count=len(ids)):
                result = apply_dav_visibility(dossier_id, applied_status, ids)
        except Exception:
            log_unexpected("dossier dav visibility write failed",
                           dossier_id=dossier_id)
            return DavVisibility(
                direction=DRAIN if drain else RESTORE, status=applied_status,
                resources=len(ids), ctag_bumped=False, complete=False,
                error=ERR_WRITE_FAILED,
            )
        if drain and not reread_ok:
            result = DavVisibility(
                direction=result.direction, status=applied_status,
                resources=result.resources, ctag_bumped=True,
                complete=False, error=ERR_MEMBERS_REREAD_FAILED,
            )
        try:
            stored = dossier_model.get_dossier_strict(dossier_id)
        except Exception:
            log_unexpected("dossier dav visibility: status re-read failed",
                           dossier_id=dossier_id)
            return DavVisibility(
                direction=result.direction, status=applied_status,
                resources=result.resources, ctag_bumped=True,
                complete=False, error=result.error or ERR_STATUS_UNVERIFIED,
            )
        if stored is None:
            # Deleted meanwhile: its sync state is torn down with it
            # (routes/dossiers.dossier_delete), nothing is left to show.
            return result
        stored_status = str(stored.get("status") or "")
        if is_active(stored_status) == is_active(applied_status):
            return result
        applied_status = stored_status
        pre_ids = []
    return DavVisibility(
        direction=RESTORE if is_active(applied_status) else DRAIN,
        status=applied_status, resources=0, ctag_bumped=True,
        complete=False, error=ERR_STATUS_MOVING,
    )


def _report_incomplete(dossier_id: str, dav: DavVisibility) -> None:
    if not dav.complete:
        log_dossier_event(
            "dossier_dav_visibility_incomplete", dossier_id,
            level=logging.ERROR, via=provenance.current_via(),
            dav_direction=dav.direction, dav_resources=dav.resources,
            dav_ctag_bumped=dav.ctag_bumped, reason=dav.error,
        )


def run_status_transition(
    dossier_id: str,
    old_status: Optional[str],
    new_status: str,
    commit: Optional[Callable[[], tuple[Optional[dict], list[str]]]],
    *,
    reconcile_always: bool,
) -> StatusTransition:
    """Run a dossier status write together with the DavX5 visibility it
    requires — fail-closed before the commit, honest after it.

    *old_status* is the stored status as the caller read it (``None`` when
    that read failed: the target's visibility is then applied whatever it
    was); *new_status* the status *commit* writes; *commit* the model call —
    ``(doc, errors)`` — or ``None`` for no write at all. *reconcile_always*
    (REQUIRED — a caller decides): ``True`` re-applies the target's
    visibility even when the status does not cross the active boundary or
    does not change at all, which is what makes a same-status call a
    repair; ``False`` (the web form, saving every field on every edit)
    writes DAV markers only on a crossing. See :func:`planned_direction`.

    Order: (1) when a direction is planned, the members are read BEFORE the
    commit — a failure refuses with :data:`MEMBERS_UNREADABLE_ERROR`,
    nothing written, *commit* never called; (2) *commit*; its errors are
    returned as they are; (3) the visibility of the COMMITTED status is
    applied (:func:`_settle`) — a failure there is reported in
    ``dav.complete``/``dav.error`` and logged at ERROR, never raised: the
    status is written and nothing can un-write it, so claiming a failure
    would invite a retry of a write that happened, and claiming a success
    would hide a phone left out of step. The resync repairs it.

    There is no early return on ``old == new``: with *reconcile_always* the
    same status re-applies its visibility (lot 4a).
    """
    _require_dossier_id(dossier_id)
    direction = planned_direction(
        old_status, new_status, reconcile_always=reconcile_always)
    pre_ids: list[str] = []
    if direction != NONE:
        try:
            pre_ids = dav_member_ids(dossier_id)
        except DavMembersUnreadable:
            log_unexpected("dossier dav members unreadable — transition refused",
                           dossier_id=dossier_id)
            return StatusTransition(
                doc=None, errors=[MEMBERS_UNREADABLE_ERROR],
                dav=DavVisibility(direction=direction, complete=False,
                                  error=ERR_MEMBERS_UNREADABLE),
            )

    doc: Optional[dict] = None
    if commit is not None:
        doc, errors = commit()
        if errors:
            return StatusTransition(doc=None, errors=list(errors),
                                    dav=DavVisibility(NONE))

    committed = str((doc or {}).get("status") or new_status)
    direction = planned_direction(
        old_status, committed, reconcile_always=reconcile_always)
    if direction == NONE:
        dav = DavVisibility(NONE, status=committed)
    else:
        dav = _settle(dossier_id, committed, pre_ids)

    if old_status != committed:
        log_dossier_event(
            "dossier_status_changed", dossier_id,
            via=provenance.current_via(),
            status_from=old_status or "", status_from_known=old_status is not None,
            status_to=committed, dav_direction=dav.direction,
            dav_resources=dav.resources, dav_complete=dav.complete,
        )
    _report_incomplete(dossier_id, dav)
    return StatusTransition(doc=doc, errors=[], dav=dav)


def resync_dossier_dav_visibility(dossier_id: str) -> DavVisibility:
    """Re-apply the visibility of the dossier's CURRENT status — the repair.

    Reads the dossier strictly (unreadable → ``dossier_unreadable``; absent
    → ``dossier_not_found``; nothing written either way), then applies its
    status's visibility exactly as a transition would (:func:`_settle`,
    members read strictly). Idempotent and re-runnable: a closed dossier's
    members are tombstoned again (which also catches a resource the phone
    PUT into the collection after it closed), an open one's un-tombstoned;
    the CTag is bumped either way. Never raises on a store failure.
    """
    _require_dossier_id(dossier_id)
    try:
        dossier = dossier_model.get_dossier_strict(dossier_id)
    except Exception:
        log_unexpected("dossier dav resync: dossier unreadable",
                       dossier_id=dossier_id)
        dav = DavVisibility(NONE, complete=False, error=ERR_DOSSIER_UNREADABLE)
        _report_incomplete(dossier_id, dav)
        return dav
    if dossier is None:
        return DavVisibility(NONE, complete=False, error=ERR_DOSSIER_NOT_FOUND)
    status = str(dossier.get("status") or "")
    try:
        pre_ids = dav_member_ids(dossier_id)
    except DavMembersUnreadable:
        log_unexpected("dossier dav resync: members unreadable",
                       dossier_id=dossier_id)
        dav = DavVisibility(RESTORE if is_active(status) else DRAIN,
                            status=status, complete=False,
                            error=ERR_MEMBERS_UNREADABLE)
        _report_incomplete(dossier_id, dav)
        return dav
    dav = _settle(dossier_id, status, pre_ids)
    log_dossier_event(
        "dossier_dav_resynced", dossier_id, via=provenance.current_via(),
        dav_direction=dav.direction, dav_resources=dav.resources,
        dav_complete=dav.complete,
    )
    _report_incomplete(dossier_id, dav)
    return dav
