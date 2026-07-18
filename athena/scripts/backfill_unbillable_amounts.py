"""One-time backfill: zero the stored amount on legacy unbillable time entries.

Context: as of 2026-07-18 a time entry with ``billable == False`` stores
``amount == 0`` — unbillable time has no calculated cost (see
``models.time_entry._compute_entry_amount``). Entries created before that
change may still carry a non-zero ``amount``. This script rewrites ``amount``
to 0 on every non-invoiced, unbillable entry that still has one, so the
/temps list totals and CSV/PDF exports match the on-screen « Non fact. » rows.

The dashboard's unbilled tracker is unaffected either way — it already filters
``billable == True`` — so this is purely a display/exports consistency pass.

⚠️  DELETE THIS SCRIPT once it has been run in production. It is a throwaway
    one-shot, not a retained maintenance tool.

``timeentries`` is NOT a DAV-exposed collection (only parties/hearings/tasks/
notes are), so there is no CTag to bump here.

Usage:
    cd athena
    python -m scripts.backfill_unbillable_amounts [--dry-run]

    --dry-run  Report what would change without writing anything.

Invoiced entries are skipped on principle: an invoiced entry is an immutable
accounting artifact tied to a committed invoice's line items, and it can never
be both invoiced and unbillable in the first place (invoicing only ever pulls
billable, uninvoiced entries). The skip count should always read 0.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

# Ensure athena/ is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import firebase_admin
from firebase_admin import credentials, firestore

COLLECTION = "timeentries"


def run(dry_run: bool = False) -> None:
    """Zero the amount on every legacy unbillable, non-invoiced time entry."""
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.ApplicationDefault())

    db = firestore.client()

    prefix = "DRY RUN — " if dry_run else ""
    print(
        f"{prefix}Scanning '{COLLECTION}' for unbillable entries "
        f"with a non-zero amount…"
    )

    docs = list(db.collection(COLLECTION).stream())
    total = len(docs)
    print(f"Found {total} time entries.")

    now = datetime.now(timezone.utc)
    updated = 0
    skipped_invoiced = 0

    for doc in docs:
        data = doc.to_dict() or {}

        if data.get("billable"):
            continue  # billable → its amount is correct, leave it
        if not data.get("amount"):
            continue  # already 0 (or missing) → nothing to do

        if data.get("invoiced"):
            # Should never happen; never rewrite a committed accounting artifact.
            skipped_invoiced += 1
            print(f"  SKIP {doc.id} — invoiced; amount left untouched")
            continue

        old_amount = data.get("amount")
        action = "Would zero" if dry_run else "Zeroing"
        print(f"  {action} {doc.id} — amount {old_amount}¢ → 0")
        updated += 1

        if not dry_run:
            try:
                db.collection(COLLECTION).document(doc.id).update({
                    "amount": 0,
                    "updated_at": now,
                    "etag": str(uuid.uuid4()),
                })
            except Exception as exc:
                print(f"  ERROR updating {doc.id}: {exc}")
                updated -= 1

    verb = "Would update" if dry_run else "Updated"
    summary = f"\nDone. {verb} {updated} of {total} entries."
    if skipped_invoiced:
        summary += f" Skipped {skipped_invoiced} invoiced (unexpected — investigate)."
    print(summary)


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv[1:])
