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

Decision D24 (2026-09-29) added two more shapes the public create now
refuses and the store still holds: an entry whose objet contradicts its sens,
and a SINGLE « virement inter-dossiers » leg the entry form used to offer —
:func:`legacy_trust_entry` rebuilds both.

Not collected by pytest (its name does not match ``test_*.py``), never
deployed (``tests/`` is in ``.gcloudignore``).
"""

from __future__ import annotations

from datetime import datetime, timezone

#: The payee a NEW fee payment names in the suite (D23, art. 58 — the lawyer
#: or his firm, as the firm profile names them). Under pytest no ``FIRM_NAME``
#: is set and no ``settings/cabinet`` document is seeded, so the profile is
#: the deploy-time seed and its ORGANISATION is the one accepted name
#: (``models.settings.ORGANISATION_SEED`` — ``tests/test_fee_payment.py``
#: pins the two equal). A test of the rule itself seeds the profile.
FEE_PAYEE = "Poirier Lavoie, avocat"


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


def legacy_trust_entry(data: dict) -> dict:
    """An ordinary trust entry as the pre-D24 public create wrote it — an
    objet whose NAME contradicts its sens (« Dépôt du client » paid out), or
    a SINGLE « virement inter-dossiers » leg with no counter-leg — through
    the trust model's own phases, the D24 read-free refusals stepped over:
    the context is prepared under a purpose both directions accept, then
    given the historical one back (the stage writes what the context says,
    as the old create did)."""
    from google.cloud import firestore

    from models import trust

    purpose = data["purpose"]
    ctx, reason, _clean = trust._prepare_create({**data, "purpose": "autre"})
    assert reason is None, reason
    ctx["purpose"] = purpose
    ctx["clean"]["purpose"] = purpose
    now = datetime.now(timezone.utc)
    out: dict = {}

    @firestore.transactional
    def _create(txn):
        reads = trust._read_create(txn, ctx)
        out.update(trust._stage_create(txn, ctx, reads, now))

    _create(trust.db.transaction())
    return out["entry"]
