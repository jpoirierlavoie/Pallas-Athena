"""Test support: the shapes the accounting registers held BEFORE lot 5a.

Since lot 5a (step 3) a fee payment is ONE transaction — trust entry,
administration recette, invoice payment (``models/fee_payment``) — and the
trust purpose ``virement_honoraires`` is refused on the public trust paths.
The production store still holds what the old code wrote: a trust fee
payment whose recette never came (the fail-open after-commit write), one
whose recette was typed by hand, a transfer the reprise split across two
recettes. The integrity checks and the reversal of such history must still
be tested ON those shapes, so this module rebuilds them through the trust
model's OWN phases with the reservation lifted — exactly what the old
``create_transaction`` / ``reverse_transaction`` did. A NEW write in a test
goes through ``models/fee_payment``, never through here.

Not collected by pytest (its name does not match ``test_*.py``), never
deployed (``tests/`` is in ``.gcloudignore``).
"""

from __future__ import annotations

from datetime import datetime, timezone


def legacy_fee_entry(data: dict) -> dict:
    """The trust leg of a fee payment ALONE — the pre-lot-5a shape."""
    from google.cloud import firestore

    from models import trust

    data = {**data, "purpose": trust.FEE_PAYMENT_PURPOSE}
    data.setdefault("direction", "déboursé")
    ctx, reason, _clean = trust._prepare_create(
        data, reserved_ok=(trust.FEE_PAYMENT_PURPOSE,)
    )
    assert reason is None, reason
    now = datetime.now(timezone.utc)
    out: dict = {}

    @firestore.transactional
    def _create(txn):
        reads = trust._read_create(txn, ctx)
        out.update(trust._stage_create(txn, ctx, reads, now))

    _create(trust.db.transaction())
    return out["entry"]


def legacy_fee_reversal(tx_id: str, reason: str) -> dict:
    """The trust reversal of a fee payment ALONE — what the old route
    committed before reversing the recettes one by one, fail-open."""
    from google.cloud import firestore

    from models import trust

    now = datetime.now(timezone.utc)
    today = trust._today_midnight_utc()
    out: dict = {}

    @firestore.transactional
    def _reverse(txn):
        ctx = trust._read_reverse(txn, tx_id, today, fee_payment_ok=True)
        out.update(trust._stage_reverse(txn, ctx, reason, today, now))

    _reverse(trust.db.transaction())
    return out["reversal"]
