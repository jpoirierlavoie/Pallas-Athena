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

from client.config import (
    INVITATION_MAX_RENVOIS,
    PORTAIL_DB,
    STATUTS_SESSION,
)

logger = logging.getLogger(__name__)

# « not yet submitted » — still the vocabulary the emission paths use.
ACTIVE_STATUTS = ("envoyée", "ouverte")
# STATUTS_SESSION (« may still upload ») is re-exported from client.config so
# BOTH services read one definition — see the note there.


class LectureIndisponible(Exception):
    """The invitation could not be READ (Firestore outage, IAM, network).

    Deliberately distinct from « the document does not exist »: conflating the
    two made a transient blip look like a revocation, which cleared the
    session — unrecoverable, because the email link is single-use.
    """


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


def peut_relancer(invitation: dict) -> bool:
    """May we send this invitation ANOTHER sign-in link? (D-2 + D-4)

    Same statut window as :func:`peut_televerser` — a client who submitted a
    lot may still need a link to add a forgotten page — plus a hard per-
    invitation cap so one invitation can never become an unbounded outbound-
    email tap (the 5/h limit is per IP and resets).

    The cap is enforced SILENTLY: /api/renvoi returns a byte-identical
    response either way (§6.3 anti-enumeration), so a distinct « cap reached »
    message would turn the endpoint into an existence oracle. The standard
    message carries the firm's phone number instead, so a client who stops
    receiving links always has a way through.
    """
    return (
        peut_televerser(invitation)
        and int(invitation.get("resend_count") or 0) < INVITATION_MAX_RENVOIS
    )


def peut_televerser(invitation: dict) -> bool:
    """May this invitation still open a session / receive files? (D-2)

    A COMBINED test on purpose: substituting a bare membership check at a call
    site that used ``est_active`` would drop the expiry check and let an
    EXPIRED invitation upload.
    """
    return (
        invitation.get("statut") in STATUTS_SESSION
        and not est_expiree(invitation)
    )


def lire(inv_id: str) -> Optional[dict]:
    """Return the invitation, or None when it genuinely does not exist.

    Raises :class:`LectureIndisponible` when the read itself failed — the
    caller must answer 503 and LEAVE THE SESSION ALONE rather than treat the
    outage as a revocation.
    """
    if not inv_id:
        return None
    try:
        snap = _col().document(inv_id).get()
    except Exception:
        logger.exception("portal invitation read failed")
        raise LectureIndisponible from None
    return snap.to_dict() if snap.exists else None


def chercher_par_email(email: str, limit: int = 100) -> list[dict]:
    """Bounded lookup for the no-``i`` renvoi path (single-field equality —
    automatic index; NO order_by, which would demand a composite index on
    the named database — Firestore's default order is by document id, so
    the window must be wide enough to cover ALL of an address's invitations
    or the newest active one could fall outside it). Callers filter for
    active themselves."""
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
