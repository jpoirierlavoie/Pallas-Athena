"""Comptabilité — the ONE orchestration of the two registers' writes
(lot 5a, étape 3).

The trust register (``models/trust``) and the administration ledger
(``models/admin_ledger``) keep every RULE; this module keeps what used to be
spread across the two web routes, so the web and — from lot 5b — the
connector run the same path:

* the routing a caller must not get wrong: a fee payment is never an
  ordinary trust entry (``models/fee_payment``, one transaction), a
  fee-payment reversal carries its administration recettes with it;
* the strict reads a money decision stands on — an unreadable invoice or
  entry REFUSES, it never reads as « absent »;
* one structured REPORT per operation, instead of a French sentence alone:
  ``{"ok", "errors", "reason", "warnings", …}`` plus what was written — the
  entries, the invoice before and after (:func:`invoice_payment_block`),
  the balances. A web route shows ``errors``; a tool handler emits the rest.

What it deliberately does NOT do: decide a guard. Each refusal it names is
the model's own (repeated here only where a resolution precedes the model —
an invoice number, an entry's purpose). It writes nothing itself, logs
nothing the models do not, and never touches ``dav/`` (neither register is
DAV-exposed).

Since lot 5b the connector's accounting tools (scope ``athena:comptabilite``)
are its third caller — through the SAME functions, never a copy — and the
strict reads they decide on (an entry, a lock floor, the administration
register) live at the end of this module.

Provenance is the writer's context (``models/provenance``), never an
argument.
"""

from __future__ import annotations

from typing import Optional

from models import admin_ledger as al
from models import fee_payment
from models import invoice as invoice_model
from models import trust
from utils.logging_setup import log_unexpected



class ComptabiliteRefus(Exception):
    """A refusal this module decides before any model call (an invoice that
    does not resolve, an entry that cannot be read). ``reason`` is a
    machine-stable code; the message is French and never quotes content."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


#: The warnings a successful write can carry — machine code → French text.
#: The report's ``warnings`` are the sentences (the client-mismatch one names
#: both clients); ``warning_codes`` are the codes, so a web route can carry
#: them across its POST → redirect and the page they land on can SAY them
#: (``routes/trust.entry_detail``). A warning the web dropped was a warning
#: the lawyer never read — the client-mismatch check was, on the web, no
#: check at all (lot 5a review). These generic texts are what a page shows:
#: a URL never carries a client's name.
WARNING_MESSAGES = {
    "facture_autre_client": (
        "La facture est adressée à un autre client du dossier que celui dont "
        "les fonds quittent le fidéicommis : vérifiez que ce client a "
        "autorisé ce paiement."
    ),
    "sans_recette_liee": (
        "Aucune recette au compte d'administration n'était liée à ce "
        "paiement d'honoraires (inscription antérieure au registre "
        "d'administration) : seule l'écriture au fidéicommis est "
        "contre-passée. Si une recette a été inscrite à la main pour ce "
        "paiement, contre-passez-la au registre d'administration."
    ),
    "recettes_deja_contre_passees": (
        "Les recettes au compte d'administration liées à ce paiement "
        "d'honoraires étaient déjà contre-passées : seule l'écriture au "
        "fidéicommis est contre-passée."
    ),
    "solde_compense_negatif": (
        "Le solde compensé de ce client devient négatif : les fonds "
        "contre-passés avaient déjà été déboursés. C'est un manque au "
        "fidéicommis que le cabinet doit combler."
    ),
    "fonds_liberes": "Ces dépôts sont maintenant disponibles pour un déboursé.",
}


def _report(ok: bool, *, errors=(), reason: Optional[str] = None,
            warnings=(), warning_codes=(), **fields) -> dict:
    return {"ok": ok, "errors": list(errors), "reason": reason,
            "warnings": list(warnings), "warning_codes": list(warning_codes),
            **fields}


def _refused(errors, reason, **fields) -> dict:
    return _report(False, errors=errors, reason=reason or "refus", **fields)


#: The refusal reason of a write judged on a version its caller no longer
#: has — the three money models share it (``écriture_modifiée``). A report
#: carrying it says ``stale: True``: re-read, then ask again.
STALE_REASON = "écriture_modifiée"


# ── Report helpers ─────────────────────────────────────────────────────────


def invoice_payment_block(before: Optional[dict], after: Optional[dict]) -> Optional[dict]:
    """What a payment did to an invoice, in one pure dict — ``None`` when
    no invoice was touched. ``status_before`` is ``None`` when the state
    before is unknown (a replay after the fact)."""
    if after is None:
        return None
    return {
        "id": after.get("id") or (before or {}).get("id"),
        "invoice_number": after.get("invoice_number", ""),
        "status_before": (before or {}).get("status") if before is not None else None,
        "status_after": after.get("status"),
        "amount_paid": int(after.get("amount_paid", 0) or 0),
        "balance": invoice_model.balance_of(after),
        "paid_in_full": invoice_model.balance_of(after) <= 0,
    }


# ── Reads a decision stands on ─────────────────────────────────────────────


def comptes_operations_actifs() -> tuple[list[dict], bool]:
    """Active OPERATIONS accounts — where a fee payment is deposited.

    ``(comptes, lisible)``: the list fails OPEN to ``[]`` for the form's
    select, but the second member tells « no account » from « unreadable »
    — the Comptabilité hub doctrine: an outage shows « indisponibles »,
    never an invitation to open an account (a duplicate)."""
    try:
        return [
            a for a in al.list_accounts(status="actif")
            if a.get("account_type") == "opérations"
        ], True
    except Exception:
        log_unexpected("comptabilite: admin accounts read failed")
        return [], False


def resolve_fee_invoice(
    *, dossier_id: Optional[str], invoice_id: Optional[str] = None,
    invoice_number: Optional[str] = None,
) -> dict:
    """The Athéna invoice a fee payment settles — by id or by the number
    the lawyer typed (or Claude cites), within the dossier. STRICT: an
    unreadable store, an unknown number, two invoices bearing it, an
    invoice of another dossier — each raises :class:`ComptabiliteRefus`,
    never a silent downgrade (to « external », which would skip the amount
    check, or to « introuvable » on a read blip).

    The issued-status, provision and balance rules stay the MODEL's, read
    again inside the payment's transaction."""
    number = (invoice_number or "").strip()
    wanted_id = (invoice_id or "").strip()
    if wanted_id and number:
        raise ComptabiliteRefus(
            "facture_ambiguë",
            "Indiquez la facture par son identifiant ou par son numéro — pas les deux.",
        )
    if not wanted_id and not number:
        raise ComptabiliteRefus(
            "facture_requise",
            "Indiquez la facture de Pallas Athéna que ce paiement d'honoraires acquitte.",
        )
    if not dossier_id:
        raise ComptabiliteRefus(
            "dossier_requis",
            "Sélectionnez le dossier avant d'indiquer une facture.",
        )
    try:
        if wanted_id:
            invoice = invoice_model.get_invoice_strict(wanted_id)
            matches = [invoice] if invoice is not None else []
        else:
            matches = invoice_model.get_invoices_by_number(number)
    except Exception:
        log_unexpected("comptabilite: fee invoice lookup failed")
        raise ComptabiliteRefus(
            "facture_illisible",
            "Impossible de vérifier la facture pour le moment : rien n'a été "
            "inscrit. Veuillez réessayer.",
        )
    in_dossier = [m for m in matches if m.get("dossier_id") == dossier_id]
    shown = number or "indiquée"
    if not matches:
        raise ComptabiliteRefus(
            "facture_introuvable",
            f"Aucune facture « {shown} » dans Pallas Athéna." if number
            else "Facture introuvable.",
        )
    if not in_dossier:
        raise ComptabiliteRefus(
            "facture_autre_dossier",
            f"La facture « {shown} » appartient à un autre dossier." if number
            else "La facture appartient à un autre dossier.",
        )
    if len(in_dossier) > 1:
        raise ComptabiliteRefus(
            "facture_ambiguë",
            f"Plusieurs factures portent le numéro « {shown} » dans ce dossier : "
            f"rien n'a été inscrit. Faites vérifier le registre des factures.",
        )
    return in_dossier[0]


# ── Fidéicommis ────────────────────────────────────────────────────────────


def enregistrer_ecriture_fideicommis(data: dict) -> dict:
    """An ordinary trust entry (recette or déboursé) — never a fee payment,
    which the model refuses on this path (``paiement_honoraires_composite``)
    and :func:`enregistrer_paiement_honoraires` records."""
    report: dict = {}
    entry, errors = trust.create_transaction(data, _report_out=report)
    if errors:
        return _refused(errors, report.get("reason"), entry=None, client_balance=None)
    return _report(True, entry=entry, client_balance=report.get("client_balance"))


def enregistrer_paiement_honoraires(
    data: dict, *, admin_account_id: str, admin_date=None,
    allow_external_ref: bool = False,
) -> dict:
    """A fee payment — trust entry, administration recette, invoice payment,
    ONE transaction (``models/fee_payment.create_fee_payment``).

    The invoice is resolved STRICTLY first when ``data`` names it by
    ``invoice_number`` (the web form's select, a number Claude cites).
    ``allow_external_ref`` — accept a paper invoice's number instead: the
    web form's path (the lawyer's 2026-07-17 decision), never the
    connector's (D1). A warning — never a refusal — names an invoice
    addressed to another client of the dossier than the one whose funds
    leave trust (the client may have authorised it; the lawyer checks)."""
    data = dict(data or {})
    invoice = None
    number = (data.pop("invoice_number", "") or "").strip()
    external = (data.get("invoice_external_ref") or "").strip()
    if number or (data.get("invoice_id") and not external):
        try:
            # Both named → refused as ambiguous, never one silently preferred.
            invoice = resolve_fee_invoice(
                dossier_id=data.get("dossier_id"),
                invoice_id=data.get("invoice_id") or None,
                invoice_number=number or None,
            )
        except ComptabiliteRefus as refusal:
            return _refused([refusal.message], refusal.reason, **_empty_fee())
        data["invoice_id"] = invoice["id"]
    invoice_client = (invoice or {}).get("client_id")
    other_client = bool(
        invoice_client and data.get("client_id") and invoice_client != data["client_id"]
    )
    report: dict = {}
    result, errors = fee_payment.create_fee_payment(
        data, admin_account_id=admin_account_id, admin_date=admin_date,
        allow_external_ref=allow_external_ref, _report_out=report,
    )
    if errors:
        return _refused(errors, report.get("reason"), side=report.get("side"),
                        **_empty_fee())
    warnings: list[str] = []
    codes: list[str] = []
    if other_client:
        # Named here — the report goes to the lawyer (or Claude); a URL gets
        # the generic WARNING_MESSAGES text.
        payer = result["trust_entry"].get("client_name") or "le client débité"
        billed = invoice.get("client_name") or "un autre client"
        warnings.append(
            f"La facture est adressée à {billed}, alors que les fonds quittent "
            f"le fidéicommis au nom de {payer} (un autre client du dossier) : "
            f"vérifiez que ce client a autorisé ce paiement."
        )
        codes.append("facture_autre_client")
    return _report(
        True, warnings=warnings, warning_codes=codes,
        trust_entry=result["trust_entry"],
        admin_recette=result["admin_recette"],
        invoice=invoice_payment_block(result["invoice_before"], result["invoice_after"]),
        client_balance=result["client_balance"],
    )


def _empty_fee() -> dict:
    return {"trust_entry": None, "admin_recette": None, "invoice": None,
            "client_balance": None}


def contrepasser_ecriture_fideicommis(
    tx_id: str, reason: str, *, expected_etag: Optional[str] = None,
) -> dict:
    """Reverse a trust entry — a fee payment through its composite (the
    recettes and their invoices follow in the same transaction), any other
    entry through the register.

    The original is read STRICTLY to choose the path: an absent entry is
    refused, an UNREADABLE one too — never « not a fee payment », which
    used to skip the administration side without a word.

    ``expected_etag`` — the version of the entry the caller decided on (the
    confirmation page's hidden field, the connector's read), checked inside
    the reversal's transaction: a stale one refuses (``stale: True``),
    nothing written. ``None`` asserts nothing."""
    try:
        original = trust.get_transaction_strict(tx_id)
    except Exception:
        log_unexpected("comptabilite: trust entry read failed")
        return _refused(
            ["Impossible de lire cette écriture pour le moment : rien n'a été "
             "contre-passé. Veuillez réessayer."], "lecture_impossible",
            **_empty_reversal())
    if original is None:
        return _refused([trust._ABORT_MESSAGES["écriture_introuvable"]],
                        "écriture_introuvable", **_empty_reversal())

    codes: list[str] = []
    if original.get("purpose") == trust.FEE_PAYMENT_PURPOSE:
        report: dict = {}
        result, errors = fee_payment.reverse_fee_payment(
            tx_id, reason, expected_etag=expected_etag, _report_out=report)
        if errors:
            return _refused(errors, report.get("reason"), side=report.get("side"),
                            stale=report.get("reason") == STALE_REASON,
                            **_empty_reversal())
        if not result["admin_reversals"]:
            # Two different facts (lot 5a review): nothing was ever linked (a
            # legacy fee payment), or every linked recette was already
            # reversed. Worded as the first, the second told the lawyer the
            # operations account had never seen the money.
            codes.append("recettes_deja_contre_passees" if result.get("linked_recettes")
                         else "sans_recette_liee")
        cleared_after = result["client_cleared_after"]
        codes += _negative_cleared(cleared_after)
        return _report(
            True, warnings=[WARNING_MESSAGES[c] for c in codes], warning_codes=codes,
            reversal=result["trust_reversal"],
            reversals=result["trust_reversals"],
            original={"id": tx_id, "status_after": result["original_status_after"]},
            admin_reversals=result["admin_reversals"],
            invoices=[invoice_payment_block(before, after)
                      for _iid, before, after in result["invoices"]],
            client_cleared_after=cleared_after,
        )

    report = {}
    reversal, errors = trust.reverse_transaction(
        tx_id, reason, expected_etag=expected_etag, _report_out=report)
    if errors:
        return _refused(errors, report.get("reason"),
                        stale=report.get("reason") == STALE_REASON,
                        **_empty_reversal())
    cleared_after = report.get("client_cleared_after")
    codes = _negative_cleared(cleared_after)
    return _report(
        True, warnings=[WARNING_MESSAGES[c] for c in codes], warning_codes=codes,
        reversal=reversal, reversals=report.get("reversals", [reversal]),
        original={"id": tx_id, "status_after": report.get("original_status_after")},
        admin_reversals=[], invoices=[], client_cleared_after=cleared_after,
    )


def _empty_reversal() -> dict:
    return {"reversal": None, "reversals": [], "original": None,
            "admin_reversals": [], "invoices": [], "client_cleared_after": None}



def _negative_cleared(cleared_after: Optional[int]) -> list[str]:
    """A reversal bypasses the overdraft control (spec §4.3): reversing a
    cleared deposit whose funds were already disbursed drives the client's
    cleared balance NEGATIVE — a shortfall the lawyer must cover. Said,
    never refused (the reversal is the correction the register needs).
    Returns the warning CODE (``WARNING_MESSAGES`` holds its text)."""
    if cleared_after is not None and cleared_after < 0:
        return ["solde_compense_negatif"]
    return []


def compenser_fideicommis(tx_ids: list, cleared_date) -> dict:
    """Clear trust entries at the date the BANK STATEMENT shows — required,
    never defaulted here (the web form pre-fills today; a caller that
    omits it is refused). All-or-nothing; above the lock floor. Reports the
    funds each cleared recette releases to the overdraft control."""
    ids = list(tx_ids or [])
    if cleared_date is None:
        return _refused(["Indiquez la date de compensation — celle du relevé bancaire."],
                        "date_compensation_requise", entries=[], released_funds=[])
    if not ids:
        return _refused(["Aucune écriture à compenser."], "aucune_écriture",
                        entries=[], released_funds=[])
    report: dict = {}
    count, failed = trust.clear_transactions_bulk(ids, cleared_date, _reason_out=report)
    if failed:
        return _refused([report.get("message") or trust._ABORT_MESSAGES["compensation_invalide"]],
                        report.get("reason") or "compensation_invalide",
                        entries=[], released_funds=[], failed=list(failed))
    cleared = report.get("cleared", [])
    released: dict = {}
    for e in cleared:
        if e.get("direction") == "recette" and e.get("dossier_id") and e.get("client_id"):
            key = (e["dossier_id"], e["client_id"])
            released[key] = released.get(key, 0) + int(e.get("amount", 0))
    codes = ["fonds_liberes"] if released else []
    return _report(
        True, warnings=[WARNING_MESSAGES[c] for c in codes], warning_codes=codes,
        entries=cleared,
        released_funds=[{"dossier_id": d, "client_id": c, "amount": a}
                        for (d, c), a in released.items()],
    )


# ── Administration ─────────────────────────────────────────────────────────


def enregistrer_ecriture_administration(data: dict, *, cleared_date=None) -> dict:
    """An administration entry — a dépense, another recette, or an
    encaissement (whose payment lands on its invoice in the entry's own
    commit). ``cleared_date`` — « déjà compensée »: born compensée in the
    SAME commit (never create-then-clear)."""
    report: dict = {}
    entry, errors = al.create_transaction(data, cleared_date=cleared_date,
                                          _report_out=report)
    if errors:
        return _refused(errors, report.get("reason"), entry=None, invoice=None)
    return _report(
        True, entry=entry,
        invoice=invoice_payment_block(report.get("invoice_before"),
                                      report.get("invoice_after")),
    )


def modifier_ecriture_administration(
    tx_id: str, data: dict, *, expected_etag: Optional[str],
) -> dict:
    """Correct an unlocked administration entry. ``expected_etag`` — the
    version the caller read; a stale one refuses (``stale: True``), nothing
    written. ``None`` asserts nothing (a page rendered before the field)."""
    report: dict = {}
    entry, errors = al.update_transaction(tx_id, data, expected_etag=expected_etag,
                                          _report_out=report)
    if errors:
        return _refused(errors, report.get("reason"), entry=None, changed_fields=[],
                        stale=report.get("reason") == STALE_REASON)
    return _report(True, entry=entry, changed_fields=report.get("fields", []),
                   stale=False)


def compenser_administration(
    tx_ids: list, cleared_date, *, expected_etags: Optional[dict] = None,
) -> dict:
    """Clear administration entries at the statement date — required.
    All-or-nothing; above the lock floor. ``expected_etags`` —
    ``{tx_id: etag}``, the versions the caller SAW (an administration entry
    stays editable until cleared, by the connector too): a changed one
    refuses the batch (``stale: True``), nothing written."""
    ids = list(tx_ids or [])
    if cleared_date is None:
        return _refused(["Indiquez la date de compensation — celle du relevé bancaire."],
                        "date_compensation_requise", entries=[])
    if not ids:
        return _refused(["Aucune écriture à compenser."], "aucune_écriture", entries=[])
    report: dict = {}
    count, failed = al.clear_transactions_bulk(ids, cleared_date,
                                               expected_etags=expected_etags,
                                               _report_out=report)
    if failed:
        return _refused([report.get("message") or al._ABORT_MESSAGES["compensation_invalide"]],
                        report.get("reason") or "compensation_invalide",
                        entries=[], failed=list(failed),
                        stale=report.get("reason") == STALE_REASON)
    return _report(True, entries=report.get("cleared", []))


def contrepasser_ecriture_administration(
    tx_id: str, reason: str, reversal_date=None, *,
    expected_etag: Optional[str] = None,
) -> dict:
    """Reverse an administration entry (a card-payment leg carries its
    pair; an encaissement reduces its invoice in the same commit). A
    recette linked to a fee payment is REFUSED here
    (``écriture_liée_fideicommis``): it reverses from the trust side, with
    its fee payment — never alone. ``expected_etag`` — as
    :func:`contrepasser_ecriture_fideicommis`."""
    report: dict = {}
    reversal, errors = al.reverse_transaction(tx_id, reason, reversal_date=reversal_date,
                                              expected_etag=expected_etag,
                                              _report_out=report)
    if errors:
        return _refused(errors, report.get("reason"), reversal=None, reversals=[],
                        invoices=[], stale=report.get("reason") == STALE_REASON)
    return _report(
        True, reversal=reversal, reversals=report.get("reversals", [reversal]),
        invoices=[invoice_payment_block(before, after)
                  for _iid, before, after in report.get("invoices", [])],
    )


def enregistrer_paiement_carte(
    bank_account_id: str, card_account_id: str, amount, date_value, method: str,
    reference: str = "", description: str = "",
) -> dict:
    """Pay the corporate card from the operations account — two linked
    legs, one transaction."""
    report: dict = {}
    leg, errors = al.create_card_payment(
        bank_account_id=bank_account_id, card_account_id=card_account_id,
        amount=amount, date_value=date_value, method=method,
        reference=reference, description=description, _report_out=report,
    )
    if errors:
        return _refused(errors, report.get("reason"), entry=None, card_leg=None)
    legs = report.get("legs") or [leg]
    return _report(True, entry=legs[0], card_leg=legs[1] if len(legs) > 1 else None)


#: The name the lot's plan gives the card payment (step text « record_card_payment »).
record_card_payment = enregistrer_paiement_carte


# ── Reads the connector's accounting tools stand on (lot 5b) ───────────────
#
# The connector's accounting tools (``mcp/handlers`` — scope
# ``athena:comptabilite``) read the two registers through THIS module, as they
# write them through it: one door for the web and the connector. Every read
# here is STRICT — it raises on a store error — because each one feeds a
# decision (which account, which lock, which entry): an unreadable register
# must refuse, never read as « empty » or « unlocked ».

#: The registers a tool names — ``trust`` (the fidéicommis, RLRQ c. B-1, r. 5)
#: and ``admin`` (the operations account and the corporate card).
REGISTERS: tuple[str, ...] = ("trust", "admin")

#: A report's ``reason`` when the write's outcome is UNKNOWN — its
#: transaction raised after a commit was attempted, or a re-run found its own
#: landed entry. The three money models share the code; a caller that holds
#: an idempotency claim must KEEP it (the write may stand), never release it.
OUTCOME_UNCERTAIN_REASON: str = trust.OUTCOME_UNCERTAIN_REASON

#: French labels the connector shows beside the stored codes.
ADMIN_KIND_LABELS: dict = dict(al.KIND_LABELS)
ADMIN_CATEGORY_LABELS: dict = dict(al.ADMIN_CATEGORY_LABELS)
TRUST_PURPOSE_LABELS: dict = dict(trust.PURPOSE_LABELS)
#: The direction each unambiguous trust purpose's name implies — the
#: model's vocabulary (``trust.PURPOSE_DIRECTIONS``), which the connector
#: enforces and the integrity script measures.
TRUST_PURPOSE_DIRECTIONS: dict = dict(trust.PURPOSE_DIRECTIONS)


def ventiler_montant(gross: int) -> tuple[int, int, int]:
    """A taxes-included amount split into ``(net, tps, tvq)`` — the ONE
    implementation the web form's « Ventiler » button uses too."""
    return al.extract_taxes_from_gross(gross)


def soldes_courants(rows: list[dict], opening: int) -> list[int]:
    """The running ledger balance after each row (``(date, sequence)``
    order) — the administration journal's own computation."""
    return al.running_balances(rows, opening)


def lire_facture(invoice_id: str) -> Optional[dict]:
    """One invoice, read STRICTLY — ``None`` when it does not exist, a raise
    when the store could not answer."""
    return invoice_model.get_invoice_strict(invoice_id)


def lire_ecriture(register: str, tx_id: str) -> Optional[dict]:
    """One entry of *register*, read STRICTLY — ``None`` when the store
    answered « no such entry », a raise when it could not answer."""
    if register == "trust":
        return trust.get_transaction_strict(tx_id)
    if register == "admin":
        return al.get_transaction_strict(tx_id)
    raise ValueError(f"unknown register: {register!r}")


def plancher_conciliation(register: str, account_id: str):
    """``period_end`` of the account's latest COMPLETED reconciliation — the
    lock floor both registers' writes refuse to reach under — or ``None``.
    Strict: a read error raises (a floor nobody could read is not « no
    floor »)."""
    if register == "trust":
        return trust._read_lock_floor(account_id)
    if register == "admin":
        return al.get_lock_floor(account_id)
    raise ValueError(f"unknown register: {register!r}")


def motif_verrou_administration(entry: dict, lock_floor) -> Optional[str]:
    """Why an administration entry refuses an EDIT — the model's own
    predicate (``admin_ledger._entry_lock_reason``) — or ``None``."""
    return al._entry_lock_reason(entry, lock_floor)


def instantane_administration() -> dict:
    """The firm-wide administration picture — every account with its
    display balance and reconciliation state (``get_firm_admin_snapshot``),
    plus its lock floor. Fails CLOSED: an unreadable account list or floor
    raises (an empty list on an outage would invite a duplicate account)."""
    snap = al.get_firm_admin_snapshot()
    for account in snap.get("accounts", []):
        account["lock_floor"] = al.get_lock_floor(account.get("id", ""))
    return snap


def registre_administration(
    account_id: str, date_from, date_to, *, limit: int,
) -> tuple[list[dict], bool]:
    """One administration account's register for a window, in ledger order
    ``(date, sequence)`` — ``(rows, truncated)``, never a silently shortened
    list. Fails CLOSED."""
    return al.list_register(account_id, date_from, date_to, limit=limit)


def solde_reporte_administration(account_id: str, date_from) -> tuple[int, bool]:
    """The ledger balance carried into *date_from* — ``(cents,
    had_prior_entry)``. Raises when the read truncates: a partial sum is a
    wrong balance."""
    return al.opening_ledger_balance(account_id, date_from)
