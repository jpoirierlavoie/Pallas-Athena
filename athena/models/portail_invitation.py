"""Invitations du portail client — base Firestore NOMMÉE « portail » (spec L1 §5).

Single-writer principle: the MAIN service writes everything (creation,
statuses, submission entries, the accusé marker); the portal service only
READS this database and signals through Cloud Tasks. This module is that
single write path.

The client targets the named database « portail » through its own lazy
``firestore.Client(database=...)`` — deliberately NOT the ``models.db``
default-database singleton, and deliberately not ``firebase_admin.firestore``
(whose ``database_id`` support varies by version — spec §5). Lazy so
importing this module never requires the database to exist: until the ops
checklist creates it, every function degrades (None / empty / False) and the
routes show French empty states instead of crashing.

Confidentiality trap (spec §5): every field of an invitation document is
readable by the PUBLIC portal service. Nothing beyond the necessary ever
goes in — ``display_label`` is the ONLY designation the client sees (no
dossier title revealing the opposing party, no internal memo).

No ``etag`` — the collection is not DAV-exposed (documented exception, like
the OAuth collections).
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from client.config import (
    INVITATION_DOCUMENTS_JOURS,
    INVITATION_INTAKE_JOURS,
    INVITATION_MAX_RENVOIS,
    PORTAIL_DB,
    PORTAIL_MAX_FILES,
    PORTAIL_MAX_TOTAL_MB,
    STATUTS_SESSION,
)
from security import sanitize

logger = logging.getLogger(__name__)

VALID_TYPES = ("documents", "intake")  # « intake » réservé à la phase L3
VALID_STATUTS = (
    "envoyée", "ouverte", "soumise", "traitée", "refusée", "révoquée",
)
# Statuses under which the client may still open a session / upload.
ACTIVE_STATUTS = ("envoyée", "ouverte")

_LISTE_MAX = 100  # bounded reads — single-user volume, no pagination UI


@lru_cache(maxsize=1)
def _pdb() -> firestore.Client:
    return firestore.Client(database=PORTAIL_DB)


def _col():
    return _pdb().collection("invitations")


def est_expiree(invitation: dict, now: Optional[datetime] = None) -> bool:
    """Logical expiry (spec §5) — no « expirée » status is ever written."""
    expires_at = invitation.get("expires_at")
    if expires_at is None:
        return True
    return (now or datetime.now(timezone.utc)) >= expires_at


def est_active(invitation: dict) -> bool:
    return invitation.get("statut") in ACTIVE_STATUTS and not est_expiree(invitation)


def peut_relancer(invitation: dict) -> bool:
    """May another sign-in link be sent for this invitation? (D-2 + D-4)

    MIRRORS ``client.services.invitations.peut_relancer`` — the two services
    MUST agree. If the portal enqueued a renvoi that this side refused, the
    handler would drop it with a 200 while the client read « un nouveau lien
    vient d'être transmis »: a phantom email. The statut vocabulary and the
    cap both come from ``client/config.py``, which both services import, so
    there is no third hand-maintained copy.
    """
    return (
        invitation.get("statut") in STATUTS_SESSION
        and not est_expiree(invitation)
        and int(invitation.get("resend_count") or 0) < INVITATION_MAX_RENVOIS
    )


def creer_invitation(
    type_: str,
    email: str,
    *,
    dossier_id: Optional[str] = None,
    partie_id: Optional[str] = None,
    client_name: str = "",
    display_label: str = "",
    jours: Optional[int] = None,
) -> tuple[Optional[dict], list[str]]:
    """Create an invitation document (statut « envoyée »).

    ``client_name`` is the sender name shown on the accusé bordereau. It is
    portail-readable (confidentiality trap §5) — a low-sensitivity string on
    par with the ``email`` already stored; the richer contact (address,
    phone) is NEVER stored, only resolved main-side from ``partie_id`` when
    the accusé is built.
    """
    errors: list[str] = []
    if type_ not in VALID_TYPES:
        errors.append("Type d'invitation invalide.")
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        errors.append("Adresse courriel invalide.")
    display_label = sanitize(display_label, max_length=120)
    if not display_label:
        errors.append("Une désignation (visible par le client) est requise.")
    if errors:
        return None, errors

    if jours is None:
        jours = (
            INVITATION_DOCUMENTS_JOURS if type_ == "documents"
            else INVITATION_INTAKE_JOURS
        )
    now = datetime.now(timezone.utc)
    inv_id = str(uuid.uuid4())
    doc = {
        "id": inv_id,
        "type": type_,
        "email": email,
        "partie_id": partie_id or None,
        "dossier_id": dossier_id or None,
        "client_name": sanitize(client_name, max_length=200),
        "display_label": display_label,
        "statut": "envoyée",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + timedelta(days=int(jours)),
        "resend_count": 0,
        "quota_files": PORTAIL_MAX_FILES,
        "quota_mb": PORTAIL_MAX_TOTAL_MB,
        "soumissions": [],
        "accuses": {},
        "prefill": None,  # réservé à L3
    }
    try:
        _col().document(inv_id).set(doc)
    except Exception:
        logger.exception("portail invitation create failed")
        return None, ["Erreur lors de la création de l'invitation."]
    return doc, []


def lire_invitation(inv_id: str) -> Optional[dict]:
    if not inv_id:
        return None
    try:
        snap = _col().document(inv_id).get()
    except Exception:
        logger.exception("portail invitation read failed")
        return None
    return snap.to_dict() if snap.exists else None


def lister_invitations(
    type_: Optional[str] = None,
    statuts: Optional[tuple[str, ...]] = None,
    limit: int = _LISTE_MAX,
) -> list[dict]:
    """Bounded newest-first listing.

    Single-field order only (automatic index); the type/status filters are
    applied in Python so no composite index is ever needed on the named
    database (spec §9.1).
    """
    try:
        snaps = (
            _col()
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(max(limit, _LISTE_MAX))
            .stream()
        )
        rows = [s.to_dict() for s in snaps]
    except Exception:
        logger.exception("portail invitation list failed")
        return []
    if type_:
        rows = [r for r in rows if r.get("type") == type_]
    if statuts:
        rows = [r for r in rows if r.get("statut") in statuts]
    return rows[:limit]


def maj_statut(inv_id: str, statut: str) -> bool:
    if statut not in VALID_STATUTS:
        return False
    try:
        _col().document(inv_id).update(
            {"statut": statut, "updated_at": datetime.now(timezone.utc)}
        )
        return True
    except Exception:
        logger.exception("portail invitation status update failed")
        return False


def revoquer(inv_id: str) -> bool:
    """Instant revocation — the portal re-reads the doc on every request."""
    return maj_statut(inv_id, "révoquée")


def marquer_ouverte(inv_id: str) -> bool:
    """Transactional « envoyée » → « ouverte »; any other statut is a no-op.

    A plain read-check-write could race the « soumise » task (Cloud Tasks
    guarantees neither ordering nor non-concurrency) and regress a processed
    submission back to « ouverte », hiding it from Réception. Same CAS shape
    as the module's other transactional guards. False only on failure.
    """
    ref = _col().document(inv_id)
    transaction = _pdb().transaction()

    @firestore.transactional
    def _txn(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return True  # nothing to open — no-op
        if snap.to_dict().get("statut") != "envoyée":
            return True  # already ouverte/soumise/… — never regress
        txn.update(
            ref,
            {"statut": "ouverte", "updated_at": datetime.now(timezone.utc)},
        )
        return True

    try:
        return _txn(transaction)
    except Exception:
        logger.exception("portail invitation marquer_ouverte failed")
        return False


def incrementer_resend(inv_id: str) -> bool:
    try:
        _col().document(inv_id).update(
            {
                "resend_count": firestore.Increment(1),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return True
    except Exception:
        logger.exception("portail invitation resend increment failed")
        return False


def ajouter_soumission(
    inv_id: str, batch: str, files_count: int, total_bytes: int
) -> bool:
    """Append a submission entry if that batch is not recorded yet.

    Transactional read-modify-write (an ArrayUnion cannot express « append
    if absent » because ``recu_at`` differs per attempt). Idempotent: a
    replayed task or a reconciliation replay leaves an existing entry alone.
    """
    ref = _col().document(inv_id)
    transaction = _pdb().transaction()

    @firestore.transactional
    def _txn(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return False
        doc = snap.to_dict()
        soumissions = list(doc.get("soumissions") or [])
        if any(s.get("batch") == batch for s in soumissions):
            return True  # already recorded — nothing to do
        soumissions.append(
            {
                "batch": batch,
                "files_count": int(files_count),
                "total_bytes": int(total_bytes),
                "recu_at": datetime.now(timezone.utc),
            }
        )
        txn.update(
            ref,
            {
                "soumissions": soumissions,
                "statut": "soumise",
                "updated_at": datetime.now(timezone.utc),
            },
        )
        return True

    try:
        return _txn(transaction)
    except Exception:
        logger.exception("portail submission append failed")
        return False


def poser_accuse(inv_id: str, batch: str) -> bool:
    """Transactional test-and-set on ``accuses[batch]`` (spec §8.3.e).

    Returns True exactly ONCE per (invitation, batch) — the single guard of
    the single non-idempotent effect (the accusé email). Also protects a
    replayed task racing the reconciliation. Any failure → False (fail
    closed: better a missing accusé, replayed later, than a duplicate).
    """
    ref = _col().document(inv_id)
    transaction = _pdb().transaction()

    @firestore.transactional
    def _txn(txn) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return False
        accuses = dict(snap.to_dict().get("accuses") or {})
        if accuses.get(batch):
            return False  # already sent (or being sent by a racing task)
        accuses[batch] = True
        txn.update(
            ref,
            {"accuses": accuses, "updated_at": datetime.now(timezone.utc)},
        )
        return True

    try:
        return _txn(transaction)
    except Exception:
        logger.exception("portail accuse test-and-set failed")
        return False


def compter_soumises() -> Optional[int]:
    """COUNT of invitations awaiting review (nav badge).

    Single-field equality — automatic index. Fail-open: None on ANY failure
    (base not created yet, IAM, outage) so the badge silently disappears
    instead of breaking every page render.
    """
    try:
        agg = (
            _col()
            .where(filter=FieldFilter("statut", "==", "soumise"))
            .count(alias="n")
            .get()
        )
        return int(agg[0][0].value)
    except Exception:
        return None
