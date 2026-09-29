"""CTag / sync-token management for DAV collections.

Each collection type (parties, hearings, tasks, dossiers) has a Firestore
document at ``dav_sync/{collection_name}`` that stores:

- ``ctag``  – a UUID v4 regenerated on every mutation (create/update/delete)
- ``sync_token`` – equals the ctag (used by DavX5 for sync-collection)
- ``updated_at`` – UTC timestamp of last mutation

Tombstones (deleted resource IDs) are stored in the sub-collection
``dav_sync/{collection_name}/tombstones/{resource_id}`` so that
sync-collection can report 404 for resources deleted since the client's
last sync token.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.api_core.exceptions import AlreadyExists

from models import db
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

SYNC_COLLECTION = "dav_sync"

# Tombstones older than this are pruned opportunistically (see get_tombstones).
TOMBSTONE_TTL_DAYS = 30

# The "Général" collection: hearings, tasks and notes that belong to no
# dossier. It replaced the split /dav/calendar/ + /dav/tasks/ pair in July
# 2026 and has exactly the shape of a dossier collection.
GENERAL_COLLECTION = "general"

# The dossier statuses whose ``/dav/dossier-{id}/`` collection is ADVERTISED
# to DavX5 and lists live resources. THE single source (lot 4a): the root
# Depth:1 PROPFIND (``dav/__init__.py``) iterates it to build discovery, the
# collection's own handlers (``dav.dossier_collections._dossier_is_active``)
# test it to decide « live or draining », and ``services/dossier_dav.py``
# derives from it whether a status transition drains or restores. They used
# to carry three hand-typed copies (plus six in ``mcp/handlers.py``), and a
# drift between any two is silent: discovery advertising a collection the
# drain has emptied, or a drained collection the root keeps listing.
#
# ``en_attente`` is ACTIVE here: a pending dossier keeps its collection on
# the phone. (It is ALSO still in the prescription alerts since lot 0b —
# ``models.dossier.PRESCRIPTION_ALERT_STATUSES`` — a different question that
# happens to have the same answer today; the two are deliberately not one
# constant.)
ACTIVE_DOSSIER_STATUSES: tuple[str, ...] = ("actif", "en_attente")


def collection_for(dossier_id: Optional[str]) -> str:
    """DAV collection name an item belongs to, from its ``dossier_id``.

    THE single source of truth for routing a write to the right CTag, used by
    every write path (routes/notes, routes/tasks, routes/hearings, the DAV
    handlers and the MCP note tools).

    It exists as one function because the models never bump a CTag themselves
    — bumping lives in the caller — so a path that picks the wrong name, or
    picks none at all, leaves the item visible in the web UI while DavX5
    silently never re-syncs it. Tasks store ``None`` for "no dossier" while
    notes and hearings store ``""``; both are falsy, so this handles all
    three without a data migration.
    """
    return f"dossier:{dossier_id}" if dossier_id else GENERAL_COLLECTION


def _sync_ref(collection_name: str):
    """Return the Firestore document reference for a collection's sync data."""
    return db.collection(SYNC_COLLECTION).document(collection_name)


def _initialise_ctag(ref) -> str:
    """Give a collection that has NO sync document its first token.

    ``create()``, never ``set()``: this path runs because a read found the
    document ABSENT, and between that read and this write a concurrent
    initialiser or ``bump_ctag`` may have stored a token — overwriting it
    would silently invalidate the token that caller just handed out. On
    ``AlreadyExists`` the stored token is served instead.
    """
    ctag = str(uuid.uuid4())
    try:
        ref.create({
            "ctag": ctag,
            "sync_token": ctag,
            "updated_at": datetime.now(timezone.utc),
        })
    except AlreadyExists:
        snap = ref.get()  # propagates: never guess a token
        if not snap.exists:
            raise
        return (snap.to_dict() or {}).get("ctag", "")
    return ctag


def get_ctag(collection_name: str) -> str:
    """Return the current CTag for a collection. Creates the doc if missing.

    Three outcomes, kept apart (lot 0b, 2026-09-27):

    * the document exists → its stored token;
    * the read SUCCEEDED and found no document → a first token is created
      (lazy initialisation, as before — with ``create()``, see
      :func:`_initialise_ctag`);
    * the read FAILED → the exception PROPAGATES and nothing is written.

    The last one used to be swallowed and followed by a ``set()`` of a fresh
    token OVER the stored one: a transient read blip reset the collection's
    sync token, and every DAV client holding the real one was pushed into a
    full resync, with no error anywhere. A read path must never write over a
    token. Callers: a DAV PROPFIND/REPORT answers 500 on the failure
    (DavX5 retries, nothing is lost); the tombstone writers read the token
    only to stamp it (see :func:`_tombstone_token`) and degrade instead.
    """
    ref = _sync_ref(collection_name)
    doc = ref.get()  # propagates a read failure — never a reset
    if doc.exists:
        return (doc.to_dict() or {}).get("ctag", "")
    return _initialise_ctag(ref)


def _tombstone_token(collection_name: str) -> str:
    """The token a tombstone is stamped with — best effort, never a reset.

    (The one write it can cause is :func:`get_ctag`'s lazy FIRST token,
    when the read succeeded and found no sync document — ``create()``,
    which never overwrites a stored token.)

    A tombstone's ``sync_token`` is informational: nothing filters on it
    (``get_tombstones`` reports by TTL — sync tokens are non-monotonic
    UUIDs), and the caller bumps the CTag right after. What matters is that
    the TOMBSTONE is written: it is the only removal signal in this sync
    model, so a failed token read must not prevent it. It is stamped ``""``
    (« unknown ») and the failure logged — and, unlike before, the stored
    token is never reset on the way.
    """
    try:
        return get_ctag(collection_name)
    except Exception:
        log_unexpected(
            "dav tombstone token read failed",
            collection=sanitize_log_value(collection_name),
        )
        return ""


def get_ctags_bulk(names: list[str]) -> dict[str, str]:
    """Return CTags for several collections using a single batched read.

    Missing sync documents are initialised lazily with a fresh ctag,
    mirroring :func:`get_ctag`.
    """
    if not names:
        return {}
    # A transient read failure must propagate: silently re-initialising every
    # requested ctag here would reset all collections' sync tokens at once
    # and force every DAV client into a simultaneous full resync.
    found: dict[str, str] = {}
    refs = [_sync_ref(name) for name in names]
    for snap in db.get_all(refs):
        if snap.exists:
            found[snap.id] = (snap.to_dict() or {}).get("ctag", "")
    ctags: dict[str, str] = {}
    for name in names:
        if name not in found:
            # ABSENT (the read succeeded): lazy first token, as get_ctag —
            # created, never set over a token a racing writer just stored.
            ctags[name] = _initialise_ctag(_sync_ref(name))
            continue
        ctag = found[name]
        if not ctag:
            # Present but token-less: nothing to lose, repaired in place.
            ctag = str(uuid.uuid4())
            _sync_ref(name).set({
                "ctag": ctag,
                "sync_token": ctag,
                "updated_at": datetime.now(timezone.utc),
            })
        ctags[name] = ctag
    return ctags


def get_sync_token(collection_name: str) -> str:
    """Return the current sync-token (same as ctag)."""
    return get_ctag(collection_name)


def bump_ctag(collection_name: str) -> str:
    """Regenerate the CTag/sync-token for a collection.  Returns new ctag."""
    ctag = str(uuid.uuid4())
    _sync_ref(collection_name).set({
        "ctag": ctag,
        "sync_token": ctag,
        "updated_at": datetime.now(timezone.utc),
    })
    return ctag


def record_tombstone(collection_name: str, resource_id: str) -> None:
    """Record that a resource was deleted (for sync-collection 404 reports).

    The token read is best effort (:func:`_tombstone_token`): a failed read
    stamps « » and the tombstone is still written; a failed WRITE raises.
    """
    _sync_ref(collection_name).collection("tombstones").document(
        resource_id
    ).set({
        "deleted_at": datetime.now(timezone.utc),
        "sync_token": _tombstone_token(collection_name),
    })


# Firestore caps a batch at 500 operations; 450 is the repo's safety chunk
# (models/folder.py uses the same value for the same reason).
_BATCH_CHUNK = 450


def bump_ctag_in_batch(batch, collection_name: str) -> str:
    """Stage a CTag bump into a caller-owned batch. Returns the new token.

    A bulk write that commits its documents and THEN bumps has a fatal gap:
    if the bump raises, the resources exist and are visible in the web UI
    while DavX5 silently never re-syncs any of them — ``_handle_sync_collection``
    short-circuits on an unchanged token. Firestore batches span collections,
    so staging the bump alongside the writes closes it for good.
    """
    ctag = str(uuid.uuid4())
    batch.set(_sync_ref(collection_name), {
        "ctag": ctag,
        "sync_token": ctag,
        "updated_at": datetime.now(timezone.utc),
    })
    return ctag


def record_tombstones_in_batch(
    batch, collection_name: str, resource_ids: list[str], sync_token: str
) -> None:
    """Stage tombstones into a caller-owned batch, under *sync_token*.

    Pair with :func:`bump_ctag_in_batch` so the deletions, their tombstones
    and the bump commit together or not at all. Tombstones are the ONLY
    removal signal in this sync model: a delete that commits without them
    leaves the resources on the phone permanently, and the documents are gone
    so nothing can re-derive the list.
    """
    tombstones = _sync_ref(collection_name).collection("tombstones")
    now = datetime.now(timezone.utc)
    for rid in resource_ids:
        batch.set(
            tombstones.document(rid),
            {"deleted_at": now, "sync_token": sync_token},
        )


def remove_tombstones_in_batch(
    batch, collection_name: str, resource_ids: list[str]
) -> None:
    """Stage the DELETION of *resource_ids*' tombstones into a caller-owned
    batch — the restore-side sibling of :func:`record_tombstones_in_batch`.

    For resources that (re)enter a collection in bulk (a reopened dossier):
    paired with :func:`bump_ctag_in_batch`, the removals and the bump commit
    together or not at all. Deleting a tombstone that does not exist is a
    no-op in Firestore, so a resource that was never tombstoned costs one
    harmless write. Unlike :func:`remove_tombstone`, nothing is swallowed
    here: the caller's ``commit()`` raises, and the caller decides.
    """
    tombstones = _sync_ref(collection_name).collection("tombstones")
    for rid in resource_ids:
        batch.delete(tombstones.document(rid))


def _commit_chunks_with_bump(collection_name: str, resource_ids, stage) -> str:
    """Stage *resource_ids* chunk by chunk, the CTag bump in the FINAL chunk,
    then commit the chunks IN ORDER. Returns the new token.

    Each chunk holds at most ``_BATCH_CHUNK - 1`` resources, so a write of up
    to 449 resources and its bump are ONE atomic commit; a longer one bumps
    only once every earlier chunk has committed. A failed chunk therefore
    leaves the token UNCHANGED: no client can record a token that covers half
    a write, and the caller — told by the propagated exception — re-runs the
    whole thing (every stage is idempotent). ``stage(batch, chunk, token)``
    writes one chunk into its batch under the new token.
    """
    ids = list(resource_ids)
    per = _BATCH_CHUNK - 1
    chunks = [ids[i:i + per] for i in range(0, len(ids), per)] or [[]]
    batches = [db.batch() for _ in chunks]
    token = bump_ctag_in_batch(batches[-1], collection_name)
    for batch, chunk in zip(batches, chunks):
        stage(batch, chunk, token)
    for batch in batches:
        batch.commit()
    return token


def record_tombstones_bulk(
    collection_name: str, resource_ids: list[str], *, bump: bool = False
) -> Optional[str]:
    """Record many tombstones with ONE ctag read and chunked batch writes.

    ``record_tombstone`` calls ``get_ctag`` inline, so it costs TWO serialized
    round trips per resource. Draining a hearing-heavy dossier that way — or
    deleting a recurring series — walks straight into the gunicorn 60 s
    timeout, and a SIGKILL there is unrecoverable: the resources are gone from
    the collection listing, the CTag was never bumped, and nothing remains to
    tell DavX5 to look. One read plus ``ceil(N / 450)`` commits instead.

    Read-side sibling of :func:`get_ctags_bulk`. WRITE failures propagate —
    a caller deleting on the strength of this must not mistake a write
    failure for a completed drain. A failed token READ does not: the
    tombstones are written anyway, stamped « » (:func:`_tombstone_token`).

    ``bump=True`` (lot 4a — the dossier drain, ``services/dossier_dav``):
    the CTag bump rides in the FINAL chunk (:func:`_commit_chunks_with_bump`)
    — even for an empty list, which then commits the bump alone — and the
    tombstones carry the NEW token, with no read. Returns that token;
    ``None`` without a bump (the caller bumps, as before).
    """
    if bump:
        return _commit_chunks_with_bump(
            collection_name, resource_ids,
            lambda batch, chunk, token: record_tombstones_in_batch(
                batch, collection_name, chunk, token),
        )
    if not resource_ids:
        return None
    token = _tombstone_token(collection_name)
    now = datetime.now(timezone.utc)
    tombstones = _sync_ref(collection_name).collection("tombstones")
    for start in range(0, len(resource_ids), _BATCH_CHUNK):
        batch = db.batch()
        for rid in resource_ids[start:start + _BATCH_CHUNK]:
            batch.set(
                tombstones.document(rid),
                {"deleted_at": now, "sync_token": token},
            )
        batch.commit()
    return None


def remove_tombstones_bulk(
    collection_name: str, resource_ids: list[str], *, bump: bool = False
) -> Optional[str]:
    """Delete many tombstones in chunked batch writes — resources that
    (re)enter a collection together (a reopened dossier, lot 4a).

    WRITE failures propagate, unlike :func:`remove_tombstone`, which logs and
    swallows its own: a bulk caller must know. ``bump=True`` stages the CTag
    bump in the FINAL chunk (:func:`_commit_chunks_with_bump`) and returns
    the new token; ``None`` otherwise.
    """
    if bump:
        return _commit_chunks_with_bump(
            collection_name, resource_ids,
            lambda batch, chunk, _token: remove_tombstones_in_batch(
                batch, collection_name, chunk),
        )
    ids = list(resource_ids)
    for start in range(0, len(ids), _BATCH_CHUNK):
        batch = db.batch()
        remove_tombstones_in_batch(
            batch, collection_name, ids[start:start + _BATCH_CHUNK])
        batch.commit()
    return None


def remove_tombstone(collection_name: str, resource_id: str) -> None:
    """Remove a tombstone when a resource (re)enters a collection.

    Without this, a resurrected resource id would be reported both as a
    live 200 propstat and as a 404 tombstone in the same sync-collection
    REPORT (RFC 6578 violation — clients may delete live data).
    Missing tombstones are ignored.
    """
    try:
        _sync_ref(collection_name).collection("tombstones").document(
            resource_id
        ).delete()
    except Exception as exc:
        logger.warning(
            "remove_tombstone failed for %s/%s: %s",
            sanitize_log_value(collection_name), sanitize_log_value(resource_id), exc,
        )


# ── Relocation: a resource moving between collections ────────────────────
#
# A task, note or hearing whose dossier changes LEAVES one DAV collection and
# ENTERS another. Deletions travel only by tombstone — sync-collection reports
# the live members plus the tombstones, and an href it does not mention reads
# to the client as « unchanged » — so the old collection needs a tombstone AND
# a bump, or the phone keeps the old copy for ever; and the new one needs its
# stale tombstone (if any) removed before its bump, or one REPORT would call
# the resource both live and deleted (RFC 6578). Three routes carried that
# choreography by hand (routes/tasks, routes/notes, routes/hearings — the
# last one compared raw dossier ids instead of collections, so None vs ""
# churned « Général »), and the connector's future movers need it too.
#
# The ORDER lives in ONE place, :func:`relocation_plan`, and two executors
# run it: :func:`relocate_resource` (raises on the first failure — the loud
# path a web route wants) and ``mcp.handlers._dav_resync`` (each step in its
# own guard, because a failure there comes AFTER a committed write, and must
# be reported rather than raised into a retryable error).

RELOCATION_OPERATIONS: tuple[str, ...] = (
    "record_tombstone", "bump_ctag", "remove_tombstone",
)


def relocation_plan(
    resource_id: str,
    *,
    old_dossier_id: Optional[str],
    new_dossier_id: Optional[str],
    created: bool = False,
) -> tuple[tuple[str, str], ...]:
    """The ordered ``(operation, collection)`` steps of one write's resync.

    Keyed on COLLECTIONS (:func:`collection_for`), never raw ids: ``None``
    (a task without a dossier) and ``""`` (a note or hearing without one)
    are the same « Général » collection, so moving between them is no move.

    * a creation — ``remove_tombstone(new)``, ``bump_ctag(new)`` (a recycled
      id may still carry a tombstone from a previous delete);
    * a real move — ``record_tombstone(old)``, ``bump_ctag(old)``,
      ``remove_tombstone(new)``, ``bump_ctag(new)``;
    * otherwise — ``bump_ctag(new)``.

    *resource_id* is not part of the plan's shape (every tombstone step
    concerns it) but is required: a plan for no resource is a caller bug.
    """
    if not resource_id:
        raise ValueError("relocation_plan needs the resource id")
    new_scope = collection_for(new_dossier_id)
    if created:
        return (("remove_tombstone", new_scope), ("bump_ctag", new_scope))
    old_scope = collection_for(old_dossier_id)
    if old_scope != new_scope:
        return (
            ("record_tombstone", old_scope),
            ("bump_ctag", old_scope),
            ("remove_tombstone", new_scope),
            ("bump_ctag", new_scope),
        )
    return (("bump_ctag", new_scope),)


def relocate_resource(
    resource_id: str,
    *,
    old_dossier_id: Optional[str],
    new_dossier_id: Optional[str],
    created: bool = False,
) -> None:
    """Run :func:`relocation_plan` — the move choreography — in order.

    Raises on the first failure (``remove_tombstone`` never does: it logs
    and swallows its own, as it always has). The primitives are looked up
    at CALL time, so a test that patches ``dav.sync.bump_ctag`` sees every
    step.
    """
    for operation, collection in relocation_plan(
        resource_id,
        old_dossier_id=old_dossier_id,
        new_dossier_id=new_dossier_id,
        created=created,
    ):
        if operation == "bump_ctag":
            bump_ctag(collection)
        elif operation == "record_tombstone":
            record_tombstone(collection, resource_id)
        else:
            remove_tombstone(collection, resource_id)


def get_tombstones(
    collection_name: str, since_token: Optional[str] = None
) -> list[dict]:
    """Return tombstone records for a collection — FAIL-OPEN (``[]``).

    The *since_token* parameter is kept for API compatibility, but sync
    tokens are non-monotonic UUIDs (they mirror the ctag), so tombstones
    cannot be filtered by token ordering.  Retention is TTL-based instead:
    tombstones older than ``TOMBSTONE_TTL_DAYS`` are pruned opportunistically
    while streaming and excluded from the results.

    ⚠ A sync-collection REPORT must use :func:`get_tombstones_strict`: its
    answer carries the NEW sync token, so a deletion this ``[]`` silently
    dropped is never reported to that client again.
    """
    try:
        return get_tombstones_strict(collection_name)
    except Exception as exc:
        # Degrade to an empty list (sync omits deletions) rather than 500,
        # but make the failure visible in the logs.
        logger.warning(
            "get_tombstones failed for %s: %s", sanitize_log_value(collection_name), exc
        )
        return []


def get_tombstones_strict(collection_name: str) -> list[dict]:
    """:func:`get_tombstones`, a read failure PROPAGATING.

    For the sync-collection REPORTs (CalDAV collections and the address
    book): each answers with the collection's CURRENT token, so a response
    that omitted a deletion because the tombstone read failed would move the
    client past it for good — the phone keeps the deleted item. The caller
    answers 503 + ``Retry-After`` instead, and the client retries with its
    old token. (A failed opportunistic PRUNE is not a failed read: it is
    logged and the stale tombstone skipped, as before.)
    """
    tombstones_ref = _sync_ref(collection_name).collection("tombstones")
    cutoff = datetime.now(timezone.utc) - timedelta(days=TOMBSTONE_TTL_DAYS)
    results = []
    for doc in tombstones_ref.stream():
        data = doc.to_dict()
        deleted_at = data.get("deleted_at")
        if deleted_at is not None and deleted_at < cutoff:
            # Opportunistic prune of expired tombstones
            try:
                doc.reference.delete()
            except Exception as exc:
                logger.warning(
                    "tombstone prune failed for %s/%s: %s",
                    sanitize_log_value(collection_name), doc.id, exc,
                )
            continue
        data["id"] = doc.id
        results.append(data)
    return results


def clear_tombstones(collection_name: str) -> None:
    """Remove all tombstones for a collection (housekeeping)."""
    try:
        tombstones_ref = _sync_ref(collection_name).collection("tombstones")
        for doc in tombstones_ref.stream():
            doc.reference.delete()
    except Exception as exc:
        logger.warning(
            "clear_tombstones failed for %s: %s", sanitize_log_value(collection_name), exc
        )


def delete_sync_state(collection_name: str) -> None:
    """Delete a collection's ``dav_sync`` document (ctag + sync-token).

    Used when a DAV collection ceases to exist (e.g. a dossier is deleted).
    Callers should run :func:`clear_tombstones` first — Firestore does not
    delete subcollections when the parent document is removed.
    """
    try:
        _sync_ref(collection_name).delete()
    except Exception as exc:
        logger.warning(
            "delete_sync_state failed for %s: %s",
            sanitize_log_value(collection_name), exc,
        )
