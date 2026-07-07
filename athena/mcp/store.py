"""Firestore persistence for MCP OAuth clients, authorization codes, and tokens.

Three top-level collections: ``oauth_clients``, ``oauth_codes``,
``oauth_tokens``. Document IDs are deliberately the lookup keys — the
client_id, or the SHA-256 hex of the code/token — a documented exception to
the UUIDv4-doc-ID rule: raw credentials are never stored, and validation is
a single keyed ``get()`` with no index. High-entropy random tokens
(``secrets.token_urlsafe(32)``) need no salt or bcrypt.

Expiry (``expire_at``) is enforced in code on every read by the callers;
the Firestore TTL policies on ``oauth_codes`` / ``oauth_tokens`` are only a
garbage collector (deletion can lag by days), never a security control.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.cloud import firestore

from models import db
from mcp import ACCESS_TOKEN_TTL, AUTH_CODE_TTL, REFRESH_TOKEN_TTL

logger = logging.getLogger(__name__)

CLIENTS_COLLECTION = "oauth_clients"
CODES_COLLECTION = "oauth_codes"
TOKENS_COLLECTION = "oauth_tokens"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    """SHA-256 hex digest of a token/code — the Firestore document ID."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_expired(doc: dict) -> bool:
    """True when the doc's ``expire_at`` is missing or in the past.

    Enforced in code on every read — the Firestore TTL policy is lagging
    garbage collection, not a security boundary.
    """
    expire_at = doc.get("expire_at")
    if not isinstance(expire_at, datetime):
        return True
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    return expire_at <= _now()


# ── Clients ─────────────────────────────────────────────────────────────

def create_client(client_name: str, redirect_uris: list[str]) -> dict:
    """Register a public OAuth client (RFC 7591) and return its record."""
    now = _now()
    client_id = secrets.token_urlsafe(24)
    doc = {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "last_used_at": None,
        "created_at": now,
        "updated_at": now,
    }
    db.collection(CLIENTS_COLLECTION).document(client_id).set(doc)
    return doc


def get_client(client_id: str) -> Optional[dict]:
    """Fetch a client record; None when missing (errors propagate)."""
    if not client_id:
        return None
    snap = db.collection(CLIENTS_COLLECTION).document(client_id).get()
    return snap.to_dict() if snap.exists else None


def touch_client(client_id: str) -> None:
    """Stamp ``last_used_at`` after a successful token issuance (best effort)."""
    try:
        db.collection(CLIENTS_COLLECTION).document(client_id).update(
            {"last_used_at": _now()}
        )
    except Exception as exc:
        logger.warning("touch_client failed: %s", type(exc).__name__)


def purge_stale_clients(max_age_days: int = 30) -> int:
    """Delete clients that never completed a token issuance (junk DCR).

    A registration older than *max_age_days* with ``last_used_at`` still
    None was abandoned — the consent screen gate means no third party can
    ever have used it. Returns the number of deleted clients.
    """
    cutoff = _now() - timedelta(days=max_age_days)
    deleted = 0
    query = db.collection(CLIENTS_COLLECTION).where(
        filter=firestore.FieldFilter("last_used_at", "==", None)
    )
    for snap in query.stream():
        doc = snap.to_dict() or {}
        created_at = doc.get("created_at")
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if isinstance(created_at, datetime) and created_at < cutoff:
            snap.reference.delete()
            deleted += 1
    return deleted


# ── Authorization codes ─────────────────────────────────────────────────

def create_auth_code(
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    resource: Optional[str],
) -> str:
    """Store a single-use authorization code; returns the raw code."""
    now = _now()
    code = secrets.token_urlsafe(32)
    doc = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": resource,
        "used": False,
        "family_id": None,
        "expire_at": now + timedelta(seconds=AUTH_CODE_TTL),
        "created_at": now,
        "updated_at": now,
    }
    db.collection(CODES_COLLECTION).document(sha256_hex(code)).set(doc)
    return code


def get_auth_code(code_hash: str) -> Optional[dict]:
    """Fetch a code record by its SHA-256 hex; None when missing."""
    snap = db.collection(CODES_COLLECTION).document(code_hash).get()
    return snap.to_dict() if snap.exists else None


def consume_auth_code(code_hash: str, family_id: str) -> tuple[Optional[dict], bool]:
    """Atomically mark a code used, stamping the token family it spawns.

    Returns ``(doc, already_used)``: ``(None, False)`` when the code does
    not exist; ``(doc, True)`` when it was already consumed (replay —
    caller must revoke ``doc['family_id']``); ``(doc, False)`` when this
    call performed the transition.
    """
    ref = db.collection(CODES_COLLECTION).document(code_hash)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn: firestore.Transaction) -> tuple[Optional[dict], bool]:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return None, False
        doc = snap.to_dict() or {}
        if doc.get("used"):
            return doc, True
        txn.update(ref, {"used": True, "family_id": family_id, "updated_at": _now()})
        return doc, False

    return _txn(transaction)


# ── Tokens ──────────────────────────────────────────────────────────────

def create_token_pair(
    client_id: str,
    scope: str,
    resource: Optional[str],
    family_id: Optional[str] = None,
) -> dict:
    """Issue an access + refresh token pair sharing one family.

    Returns the RAW tokens (the only time they exist in cleartext) plus
    their hashes and metadata. Never log the returned token values.
    """
    now = _now()
    family = family_id or uuid.uuid4().hex
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    access_hash = sha256_hex(access_token)
    refresh_hash = sha256_hex(refresh_token)
    base = {
        "client_id": client_id,
        "scope": scope,
        "resource": resource,
        "family_id": family,
        "revoked": False,
        "rotated_to": None,
        "last_used_at": None,
        "created_at": now,
        "updated_at": now,
    }
    batch = db.batch()
    batch.set(
        db.collection(TOKENS_COLLECTION).document(access_hash),
        {
            **base,
            "token_type": "access",
            "expire_at": now + timedelta(seconds=ACCESS_TOKEN_TTL),
        },
    )
    batch.set(
        db.collection(TOKENS_COLLECTION).document(refresh_hash),
        {
            **base,
            "token_type": "refresh",
            "expire_at": now + timedelta(seconds=REFRESH_TOKEN_TTL),
        },
    )
    batch.commit()
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_hash": access_hash,
        "refresh_token_hash": refresh_hash,
        "family_id": family,
        "scope": scope,
        "expires_in": ACCESS_TOKEN_TTL,
    }


def get_token(token_hash: str) -> Optional[dict]:
    """Fetch a token record by SHA-256 hex; None when missing."""
    snap = db.collection(TOKENS_COLLECTION).document(token_hash).get()
    return snap.to_dict() if snap.exists else None


def claim_refresh_for_rotation(token_hash: str) -> bool:
    """Atomically flip a refresh token to revoked for rotation.

    Returns True when this call performed the not-revoked → revoked
    transition; False when the token was missing or already revoked
    (concurrent rotation or replay — caller revokes the family).
    """
    ref = db.collection(TOKENS_COLLECTION).document(token_hash)
    transaction = db.transaction()

    @firestore.transactional
    def _txn(txn: firestore.Transaction) -> bool:
        snap = ref.get(transaction=txn)
        if not snap.exists:
            return False
        doc = snap.to_dict() or {}
        if doc.get("revoked"):
            return False
        txn.update(ref, {"revoked": True, "updated_at": _now()})
        return True

    return _txn(transaction)


def set_rotated_to(token_hash: str, successor_hash: str) -> None:
    """Record the successor of a rotated refresh token (audit trail)."""
    try:
        db.collection(TOKENS_COLLECTION).document(token_hash).update(
            {"rotated_to": successor_hash, "updated_at": _now()}
        )
    except Exception as exc:
        logger.warning("set_rotated_to failed: %s", type(exc).__name__)


def revoke_token_hash(token_hash: str) -> bool:
    """Revoke a single token by hash; True when a live token was revoked."""
    ref = db.collection(TOKENS_COLLECTION).document(token_hash)
    snap = ref.get()
    if not snap.exists:
        return False
    doc = snap.to_dict() or {}
    if doc.get("revoked"):
        return False
    ref.update({"revoked": True, "updated_at": _now()})
    return True


def revoke_family(family_id: str) -> int:
    """Revoke every token in a family (OAuth 2.1 rotation replay defense)."""
    if not family_id:
        return 0
    revoked = 0
    query = db.collection(TOKENS_COLLECTION).where(
        filter=firestore.FieldFilter("family_id", "==", family_id)
    )
    now = _now()
    for snap in query.stream():
        doc = snap.to_dict() or {}
        if not doc.get("revoked"):
            snap.reference.update({"revoked": True, "updated_at": now})
            revoked += 1
    return revoked


def revoke_all_tokens() -> int:
    """Break-glass: revoke every live token (scripts/revoke_mcp_tokens.py)."""
    revoked = 0
    now = _now()
    for snap in db.collection(TOKENS_COLLECTION).stream():
        doc = snap.to_dict() or {}
        if not doc.get("revoked"):
            snap.reference.update({"revoked": True, "updated_at": now})
            revoked += 1
    return revoked


def stamp_token_last_used(token_hash: str) -> None:
    """Lazily stamp ``last_used_at`` on a validated access token."""
    try:
        db.collection(TOKENS_COLLECTION).document(token_hash).update(
            {"last_used_at": _now()}
        )
    except Exception as exc:
        logger.warning("stamp_token_last_used failed: %s", type(exc).__name__)
