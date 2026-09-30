"""Append-only deletion trail — the `audit_events` collection (PA-G06).

Why it exists: every entity hard-deletes, and a hard delete left NO trace
anywhere. The DAV tombstones cannot serve as a deletion log — they carry
only ``{deleted_at, sync_token}`` under the resource id (no entity type, no
title, no dossier), they are ALSO minted for live resources (dossier
close/archive drains, cross-collection moves), they are pruned after 30
days as a side effect of being read, and reading them therefore WRITES.
The audit's motivating case: a high-priority task carrying a deadline
vanished between two daily briefings, and nothing could distinguish a
deliberate withdrawal from an accidental deletion.

Shape — a documented exception to Architecture Rule 7 (like folders and
the OAuth collections): ``created_at`` but no ``etag`` (never DAV-exposed,
never conditionally updated) and no ``updated_at`` (append-only, nothing
ever updates one):

    audit_events/{eventId}
    {
        "id": UUIDv4,
        "at": UTC datetime,          # the deletion instant
        "entity_type": str,          # task | hearing | note | document |
                                     # expense | time_entry | invoice |
                                     # partie | protocol | protocol_step |
                                     # folder | doc_template | dossier |
                                     # admin_transaction | hearing_series |
                                     # dossier_party | mandataire
        "entity_id": str,
        "dossier_id": str,           # "" when the entity had none
        "snapshot_min": {            # just enough to answer « what was it » —
            "title": str,            # NEVER the full document (a deleted
            "status": str,           # note's body stays deleted)
        },
        "created_at": UTC datetime,  # == at
    }

Write side lives in the CALLERS (the delete routes + DAV DELETE branches),
mirroring the CTag-bump discipline — models stay single-collection writers.
ONE documented exception (lot 4a): the two LINK detaches — a party leaving
a dossier (``dossier_party``) and a mandataire leaving a contact
(``mandataire``). A link is an entry of an array inside a full-document
``set()`` that several callers perform (the web forms post the arrays
whole, the connector edits one entry), so only the model sees every
detach; ``models.dossier.update_dossier`` / ``models.partie.update_partie``
journal them after their own commit, best-effort like every caller here.
Neither is a record deletion: the contact stays — the row says a LINK went.

ONE row per ATOMIC deletion, never one per record it took with it — two
documented cases. (1) A recurring series deleted in one batch writes ONE
``hearing_series`` row carrying its occurrence count (see
``VALID_ENTITY_TYPES``). (2) A dossier deleted with its empty filing
folders writes ONE ``dossier`` row (2026-09-30): since the default tree
every dossier holds eighteen folders from its creation, and
``models.dossier.delete_dossier`` deletes them with the dossier in the SAME
transaction. ``list_recent`` reads a hard 200-row window and filters in
Python after the read: a row per folder would evict the firm's deletion
history, after which ``list_deletions`` would answer « nothing » with a
false ``truncated: false``. Both are legitimate ONLY because the deletion
is atomic — a deletion that could succeed in part would owe the detail.
(``models.folder.delete_folder`` is not atomic across its chunks, and its
route journals one row per entity it destroyed.)

``record_deletion`` is deliberately try/except-swallowing: it runs AFTER
the successful delete, and a trail-write blip must never turn a completed
deletion into a user-facing error (the delete already happened; erroring
would only invite a retry that re-deletes nothing).

Read side: one bounded fetch ordered by ``at`` DESC (single-field
auto-index — deliberately NO composite: a (dossier_id, at) pairing would
exist only for the MCP tool, which the index discipline forbids; filters
run in Python over the ≤200 window).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import db
from security import sanitize

logger = logging.getLogger(__name__)

COLLECTION = "audit_events"

VALID_ENTITY_TYPES = (
    "task", "hearing", "note", "document", "expense", "time_entry",
    "invoice", "partie", "protocol", "protocol_step", "folder",
    "doc_template", "dossier", "admin_transaction",
    # Une série récurrente supprimée s'inscrit en UNE ligne portant son
    # nombre d'occurrences, jamais N lignes. list_recent lit une fenêtre dure
    # de 200 et applique tous ses filtres EN PYTHON après coup : N lignes par
    # chaîne évinceraient tout l'historique de suppression du cabinet, après
    # quoi list_deletions(entity_type="invoice") répondrait vide avec
    # truncated: false — une affirmation de complétude qui serait fausse.
    # Légitime UNIQUEMENT parce que la suppression d'une chaîne est atomique
    # (un seul lot) : au-delà, une réussite partielle exigerait le détail.
    "hearing_series",
    # Lot 4a — a LINK removed, never a record: the contact stays. Journaled
    # by the model itself (see the module docstring's one exception).
    #   dossier_party: entity_id = the contact, dossier_id = the dossier,
    #     title = the party's name snapshot, status = the side it left
    #     (« clients » / « opposing_parties »);
    #   mandataire: entity_id = the mandataire contact, title = the name of
    #     the contact it represented, status = the kind of representation.
    "dossier_party",
    "mandataire",
)

_FETCH_CAP = 200


def record_deletion(
    entity_type: str,
    entity_id: str,
    dossier_id: str = "",
    title: str = "",
    status: str = "",
) -> Optional[dict]:
    """Append one deletion event. Best-effort: returns None on any failure.

    Called AFTER the successful model delete — a refused delete must never
    mint a phantom event, and a trail failure must never fail the route
    (the deletion is already committed; ``log_unexpected`` would be noise
    here, a warning suffices for a lost trail row).
    """
    try:
        now = datetime.now(timezone.utc)
        event_id = str(uuid.uuid4())
        doc = {
            "id": event_id,
            "at": now,
            "entity_type": entity_type,
            "entity_id": str(entity_id or ""),
            "dossier_id": str(dossier_id or ""),
            "snapshot_min": {
                "title": sanitize(str(title or ""), max_length=300),
                "status": sanitize(str(status or ""), max_length=50),
            },
            "created_at": now,
        }
        db.collection(COLLECTION).document(event_id).set(doc)
        return doc
    except Exception as exc:
        logger.warning(
            "audit_event write failed for %s deletion: %s",
            entity_type, type(exc).__name__,
        )
        return None


def list_recent(
    entity_type: Optional[str] = None,
    dossier_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Most recent deletions first, filters in Python over a ≤200 window.

    Fails OPEN to [] — the trail is a display aid; a read blip must never
    break the consumer. A caller needing « was X deleted » semantics must
    remember the window is bounded: an empty answer past 200 deletions is
    « not in the recent window », never « never deleted ».
    """
    try:
        query = (
            db.collection(COLLECTION)
            .order_by("at", direction=firestore.Query.DESCENDING)
            .limit(_FETCH_CAP)
        )
        rows = [snap.to_dict() for snap in query.stream()]
    except Exception as exc:
        logger.warning("audit_event read failed: %s", type(exc).__name__)
        return []
    if entity_type:
        rows = [r for r in rows if r.get("entity_type") == entity_type]
    if dossier_id:
        rows = [r for r in rows if r.get("dossier_id") == dossier_id]
    return rows[: max(0, int(limit))]
