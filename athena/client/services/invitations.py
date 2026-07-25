"""Read-only access to the « portail » invitation database (spec L1 §5-6).

The PORTAL never writes Firestore (single-writer principle — the main
service owns every mutation; the portal signals through Cloud Tasks). The
IAM backstop is ``roles/datastore.viewer`` conditioned to the named
database, so even a bug here could not write.

The expiry/active helpers are a small deliberate duplication of
``models/portail_invitation.py``: importing ``models`` would construct the
main service's default-database client singleton at import time, which the
portal process must never do.
"""

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from client.config import PORTAIL_DB

logger = logging.getLogger(__name__)

ACTIVE_STATUTS = ("envoyée", "ouverte")


@lru_cache(maxsize=1)
def _pdb() -> firestore.Client:
    return firestore.Client(database=PORTAIL_DB)


def _col():
    return _pdb().collection("invitations")


def est_expiree(invitation: dict) -> bool:
    expires_at = invitation.get("expires_at")
    if expires_at is None:
        return True
    return datetime.now(timezone.utc) >= expires_at


def est_active(invitation: dict) -> bool:
    return invitation.get("statut") in ACTIVE_STATUTS and not est_expiree(invitation)


def lire(inv_id: str) -> Optional[dict]:
    if not inv_id:
        return None
    try:
        snap = _col().document(inv_id).get()
    except Exception:
        logger.exception("portal invitation read failed")
        return None
    return snap.to_dict() if snap.exists else None


def chercher_par_email(email: str, limit: int = 10) -> list[dict]:
    """Bounded lookup for the no-``i`` renvoi path (single-field equality —
    automatic index). Callers filter for active themselves."""
    if not email:
        return []
    try:
        snaps = (
            _col()
            .where(filter=FieldFilter("email", "==", email))
            .limit(limit)
            .stream()
        )
        return [s.to_dict() for s in snaps]
    except Exception:
        logger.exception("portal invitation email lookup failed")
        return []
