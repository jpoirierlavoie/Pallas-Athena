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

from models import db

logger = logging.getLogger(__name__)

SYNC_COLLECTION = "dav_sync"

# Tombstones older than this are pruned opportunistically (see get_tombstones).
TOMBSTONE_TTL_DAYS = 30


def _sync_ref(collection_name: str):
    """Return the Firestore document reference for a collection's sync data."""
    return db.collection(SYNC_COLLECTION).document(collection_name)


def get_ctag(collection_name: str) -> str:
    """Return the current CTag for a collection. Creates the doc if missing."""
    ref = _sync_ref(collection_name)
    try:
        doc = ref.get()
        if doc.exists:
            return doc.to_dict().get("ctag", "")
    except Exception as exc:
        logger.warning("get_ctag failed for %s: %s", collection_name, exc)
    # Initialise with a fresh ctag
    ctag = str(uuid.uuid4())
    ref.set({
        "ctag": ctag,
        "sync_token": ctag,
        "updated_at": datetime.now(timezone.utc),
    })
    return ctag


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
        ctag = found.get(name)
        if not ctag:
            # Initialise with a fresh ctag (same lazy behaviour as get_ctag)
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
    """Record that a resource was deleted (for sync-collection 404 reports)."""
    _sync_ref(collection_name).collection("tombstones").document(
        resource_id
    ).set({
        "deleted_at": datetime.now(timezone.utc),
        "sync_token": get_ctag(collection_name),
    })


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
            collection_name, resource_id, exc,
        )


def get_tombstones(
    collection_name: str, since_token: Optional[str] = None
) -> list[dict]:
    """Return tombstone records for a collection.

    The *since_token* parameter is kept for API compatibility, but sync
    tokens are non-monotonic UUIDs (they mirror the ctag), so tombstones
    cannot be filtered by token ordering.  Retention is TTL-based instead:
    tombstones older than ``TOMBSTONE_TTL_DAYS`` are pruned opportunistically
    while streaming and excluded from the results.
    """
    try:
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
                        collection_name, doc.id, exc,
                    )
                continue
            data["id"] = doc.id
            results.append(data)
        return results
    except Exception as exc:
        # Degrade to an empty list (sync omits deletions) rather than 500,
        # but make the failure visible in the logs.
        logger.warning("get_tombstones failed for %s: %s", collection_name, exc)
        return []


def clear_tombstones(collection_name: str) -> None:
    """Remove all tombstones for a collection (housekeeping)."""
    try:
        tombstones_ref = _sync_ref(collection_name).collection("tombstones")
        for doc in tombstones_ref.stream():
            doc.reference.delete()
    except Exception as exc:
        logger.warning("clear_tombstones failed for %s: %s", collection_name, exc)


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
            "delete_sync_state failed for %s: %s", collection_name, exc
        )
