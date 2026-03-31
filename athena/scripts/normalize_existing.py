"""One-time migration: normalize phone numbers and postal codes for all parties.

Usage:
    cd athena
    python -m scripts.normalize_existing

Reads all parties from Firestore, normalizes phone/email/postal/address fields,
and writes back only the records that changed. Bumps the CardDAV ctag afterward
so DavX5 picks up the changes.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

# Ensure athena/ is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore

from utils.validators import (
    apply_address_defaults,
    normalize_email,
    normalize_phone,
    normalize_postal_code,
)

COLLECTION = "parties"
CARDDAV_SYNC_COLLECTION = "dav_sync"
CARDDAV_COLLECTION_NAME = "parties"


def _normalize_record(data: dict) -> dict:
    """Return a copy of *data* with all contact fields normalized."""
    out = dict(data)

    # Address defaults
    for prefix in ("address", "work_address"):
        out = apply_address_defaults(out, prefix)

    # Phone normalization
    for field in ("phone_home", "phone_cell", "phone_work", "fax"):
        raw = out.get(field, "")
        if raw and raw.strip():
            normalized = normalize_phone(raw)
            if normalized:
                out[field] = normalized

    # Email normalization
    for field in ("email", "email_work"):
        raw = out.get(field, "")
        if raw and raw.strip():
            normalized = normalize_email(raw)
            if normalized:
                out[field] = normalized

    # Postal code normalization
    for prefix in ("address", "work_address"):
        country = out.get(f"{prefix}_country", "CA")
        raw_pc = out.get(f"{prefix}_postal_code", "")
        if raw_pc and raw_pc.strip():
            normalized = normalize_postal_code(raw_pc, country)
            if normalized:
                out[f"{prefix}_postal_code"] = normalized

    return out


def _changed_fields(original: dict, normalized: dict) -> list[str]:
    """Return the list of field names that differ between the two dicts."""
    changed = []
    for key in normalized:
        if normalized[key] != original.get(key):
            changed.append(key)
    return changed


def _bump_carddav_ctag(db: firestore.Client) -> str:
    """Regenerate the CardDAV ctag so DavX5 picks up the changes."""
    ctag = str(uuid.uuid4())
    db.collection(CARDDAV_SYNC_COLLECTION).document(CARDDAV_COLLECTION_NAME).set(
        {"ctag": ctag, "sync_token": ctag},
        merge=True,
    )
    return ctag


def run() -> None:
    """Execute the normalization migration."""
    # Initialize Firebase Admin SDK
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.ApplicationDefault())

    db = firestore.client()

    print("Fetching all parties from Firestore…")
    docs = list(db.collection(COLLECTION).stream())
    total = len(docs)
    print(f"Found {total} records.")

    updated_count = 0
    field_change_count = 0
    now = datetime.now(timezone.utc)

    for doc in docs:
        original = doc.to_dict()
        normalized = _normalize_record(original)
        changed = _changed_fields(original, normalized)

        if changed:
            normalized["updated_at"] = now
            normalized["etag"] = str(uuid.uuid4())
            field_change_count += len(changed)
            updated_count += 1
            print(
                f"  Updating {doc.id} — changed fields: {', '.join(changed)}"
            )
            try:
                db.collection(COLLECTION).document(doc.id).set(normalized)
            except Exception as exc:
                print(f"  ERROR updating {doc.id}: {exc}")

    if updated_count:
        ctag = _bump_carddav_ctag(db)
        print(f"\nBumped CardDAV ctag → {ctag}")

    print(
        f"\nDone. Normalized {updated_count} of {total} records. "
        f"{field_change_count} field(s) changed."
    )


if __name__ == "__main__":
    run()
