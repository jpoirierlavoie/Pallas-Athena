"""Projection d'un encaissement sur sa facture — l'orchestration Lot P.

Hoisted out of ``routes/admin_ledger.py`` (audit 2026-08-26): these were
underscore-private helpers of a blueprint module, yet ``routes/trust.py``
and ``scripts/reprise_encaissements.py`` imported them cross-module — a
money-invariant business seam living as a route detail. They touch only
``models.invoice`` and the entry dict (no Flask objects), so ``services/``
is their home, beside :mod:`services.portail_emission`.

The invariant both functions serve: ``record_payment`` stays the SINGLE
writer of ``amount_paid``, and every write here is *current + delta* — the
invoice's recorded payment is re-read just before the call, so two
encaissements on one invoice ADD instead of clobbering, and a manual
correction on the invoice side survives the next projection.
"""

from utils.logging_setup import log_admin_ledger_event, log_unexpected


def projeter_paiement(entry: dict) -> bool:
    """Lot P projection: an encaissement entry also records the payment on
    its invoice — ``record_payment`` stays the SINGLE writer of
    ``amount_paid``, and this always writes *current + delta* (re-read just
    before the call) so a manual correction on the invoice side is
    preserved. Returns True on success; on failure the ENTRY STANDS (the
    register is the book of record) and the caller shows a banner."""
    from models.invoice import get_invoice, record_payment

    try:
        inv = get_invoice(entry["invoice_id"])
        if inv is None:
            raise RuntimeError("invoice unreadable")
        new_total = int(inv.get("amount_paid", 0)) + int(entry["amount"])
        updated, errs = record_payment(
            entry["invoice_id"], new_total, paid_date=entry.get("date")
        )
        if errs:
            raise RuntimeError(errs[0])
    except Exception:
        log_unexpected("admin: invoice payment projection failed")
        log_admin_ledger_event(
            "admin_invoice_payment_projected", "failure",
            transaction_id=entry.get("id"), invoice_id=entry.get("invoice_id"),
        )
        return False
    log_admin_ledger_event(
        "admin_invoice_payment_projected", transaction_id=entry.get("id"),
        invoice_id=entry.get("invoice_id"),
    )
    return True


def reduire_paiement(entry: dict) -> bool:
    """Reversal counterpart of :func:`projeter_paiement`: reduce the
    invoice's recorded payment by the reversed entry's amount. Passes the
    EXISTING paid_date through so a partial reduction does not stamp today
    (record_payment nulls it itself at zero). The narrow payée→envoyée undo
    is exactly the model behaviour this exists to trigger."""
    from models.invoice import get_invoice, record_payment

    try:
        inv = get_invoice(entry["invoice_id"])
        if inv is None:
            raise RuntimeError("invoice unreadable")
        new_total = int(inv.get("amount_paid", 0)) - int(entry["amount"])
        if new_total < 0:
            # An inconsistency between the register and the invoice (a
            # projection that never committed, a manual correction in
            # between). A max(0, …) clamp here would convert it into
            # SILENT data loss — other recorded payments erased with no
            # signal. Surface it instead: banner → manual correction.
            raise RuntimeError("recorded payment below the reversed amount")
        updated, errs = record_payment(
            entry["invoice_id"], new_total, paid_date=inv.get("paid_date")
        )
        if errs:
            raise RuntimeError(errs[0])
    except Exception:
        log_unexpected("admin: invoice payment reduction failed")
        log_admin_ledger_event(
            "admin_invoice_payment_projected", "failure",
            transaction_id=entry.get("id"), invoice_id=entry.get("invoice_id"),
        )
        return False
    log_admin_ledger_event(
        "admin_invoice_payment_projected", transaction_id=entry.get("id"),
        invoice_id=entry.get("invoice_id"), reduced=True,
    )
    return True
