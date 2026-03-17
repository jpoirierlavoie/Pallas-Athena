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

import uuid
from datetime import datetime, timezone
from typing import Optional

from models import db

SYNC_COLLECTION = "dav_sync"


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
    except Exception:
        pass
    # Initialise with a fresh ctag
    ctag = str(uuid.uuid4())
    ref.set({
        "ctag": ctag,
        "sync_token": ctag,
        "updated_at": datetime.now(timezone.utc),
    })
    return ctag


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


def get_tombstones(
    collection_name: str, since_token: Optional[str] = None
) -> list[dict]:
    """Return tombstone records, optionally filtered by sync_token.

    If *since_token* is provided, only return tombstones recorded *after*
    the provided token was current (i.e. tombstones whose sync_token !=
    since_token).  Without a token, returns all tombstones.
    """
    try:
        tombstones_ref = _sync_ref(collection_name).collection("tombstones")
        results = []
        for doc in tombstones_ref.stream():
            data = doc.to_dict()
            data["id"] = doc.id
            results.append(data)
        return results
    except Exception:
        return []


def clear_tombstones(collection_name: str) -> None:
    """Remove all tombstones for a collection (housekeeping)."""
    try:
        tombstones_ref = _sync_ref(collection_name).collection("tombstones")
        for doc in tombstones_ref.stream():
            doc.reference.delete()
    except Exception:
        pass
