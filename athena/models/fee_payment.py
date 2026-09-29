"""The fee payment (« Paiement d'honoraires ») — ONE transaction, three
records (lot 5a, étape 3 ; décisions D1, D2, D14, D16, D-4).

A fee payment takes the lawyer's fees out of the general trust account, puts
them into the firm's operations account, and records them as paid on the
invoice they settle. Until this module those were three writes in sequence,
from the route (``routes/trust.entry_create``): the trust entry committed
first, and the administration recette — with, for an Athéna invoice, the
payment on the invoice — followed AFTER, fail-open, under a banner when it
did not. Two consequences, both silent:

* **D-4 lived in the route.** A direct model call — tomorrow a connector
  tool — let fees leave trust with no entry at all in the operations
  account: the July 2026 incident class (three transfers lost until the
  reprise).
* **The parallel race.** The trust cap reads ``invoice.amount_paid``,
  which only the later recette moved: two fee payments on one invoice both
  passed it, both committed at trust, and the second recette was refused —
  client funds withdrawn beyond the invoice, with nothing to show for it.

:func:`create_fee_payment` writes the trust entry, the administration
recette and the invoice's payment in ONE Firestore transaction — every read
first (both registers' and the invoice), then every guard, then every write.
ANY refusal, on either side, writes NOTHING, anywhere. The invoice is in the
transaction's read set: a concurrent payment aborts the commit, the retry
re-reads the balance, and the second payment is refused if it no longer
fits.

:func:`reverse_fee_payment` is the mirror: the trust reversal and the
reversal of EVERY administration recette the payment carries (a transfer
the reprise split across two invoices carries two) — each reducing the
payment on its invoice — in ONE transaction, the linked rows read inside it
(fail CLOSED: an unreadable link refuses, it never means « nothing to
reverse »).

The rules are the two registers' own, run through their phase functions
(``models/trust._prepare_create`` / ``_read_create`` / ``_stage_create``,
``models/admin_ledger`` likewise) — this module adds only what belongs to the
composite:

* the administration account is REQUIRED (D-4), an active OPERATIONS
  account — for an external-reference fee payment too (a ``recette_autre``
  has no type check of its own);
* the recette's date, ``admin_date`` (D16) — the day the fees reached the
  operations account: by default the trust date; never before it, never in
  the future (Montréal), never inside a reconciled administration period —
  so a trust withdrawal of 30 August deposited on 2 September records at
  trust even after August is reconciled on the operations account;
* the external-invoice path (``invoice_external_ref``, a paper invoice that
  predates Athéna) is OFF unless the caller turns it on
  (``allow_external_ref=True``): the web form does (the lawyer's 2026-07-17
  decision), the connector never will (D1: an Athéna invoice only).

The trust side's rules stay the trust model's: art. 58 (cheque or transfer
only), art. 59 (cleared funds), the backdating guard, the lock floor, an
issued invoice of the same dossier, no provision imputed on it, the live
balance. The trust purpose ``virement_honoraires`` is RESERVED to this
module on the public create and reverse paths.

Provenance is the writer's context (``models/provenance``), never an
argument: ``mcp`` under the connector's ``writing_via``, the request's
blueprint otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import admin_ledger as al
from models import db, provenance
from models import trust
from security import sanitize
from utils.deadlines import today_mtl
from utils.logging_setup import log_trust_event, log_unexpected
from utils.tracing_setup import span


TRUST_PURPOSE = trust.FEE_PAYMENT_PURPOSE


class _FeeAbort(Exception):
    """A refusal of the composite itself (not of one register)."""

    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


_NOTHING_WRITTEN = (
    "Rien n'a été inscrit — ni au fidéicommis, ni au compte "
    "d'administration, ni sur la facture."
)
_NOTHING_REVERSED = (
    "Rien n'a été contre-passé — ni au fidéicommis, ni au compte "
    "d'administration, ni sur la facture."
)

_MESSAGES = {
    "compte_administration_requis": (
        "Sélectionnez le compte d'administration où le paiement d'honoraires "
        "est déposé : la recette correspondante doit être inscrite au registre."
    ),
    "compte_administration_invalide": (
        "Un paiement d'honoraires se dépose au compte d'opérations — jamais à "
        "une carte de crédit. " + _NOTHING_WRITTEN
    ),
    "compte_administration_introuvable": (
        "Compte d'administration introuvable. " + _NOTHING_WRITTEN
    ),
    "compte_administration_fermé": (
        "Ce compte d'administration est fermé. " + _NOTHING_WRITTEN
    ),
    "facture_externe_refusée": (
        "Ce chemin n'accepte qu'une facture de Pallas Athéna déjà envoyée — "
        "jamais un numéro de facture externe."
    ),
    "facture_athena_requise": (
        "Un paiement d'honoraires doit nommer la facture de Pallas Athéna "
        "qu'il acquitte (une facture déjà envoyée)."
    ),
    "date_administration_invalide": (
        "La date du dépôt au compte d'administration est invalide."
    ),
    "date_administration_antérieure": (
        "La date du dépôt au compte d'administration ne peut précéder la date "
        "du retrait au fidéicommis : l'argent n'arrive pas au compte "
        "d'opérations avant d'avoir quitté le fidéicommis."
    ),
    "date_administration_future": (
        "La date du dépôt au compte d'administration ne peut être dans le "
        "futur — le registre consigne ce qui est arrivé."
    ),
    "date_administration_verrouillée": (
        "La date du dépôt au compte d'administration ({detail}) tombe dans une "
        "période déjà conciliée de ce compte : indiquez la date réelle du "
        "dépôt (champ « Date du dépôt au compte d'administration »), "
        "postérieure à la conciliation. " + _NOTHING_WRITTEN
    ),
    # The floor covers TODAY: no permitted deposit date is left today (after
    # the floor is tomorrow, and a future date is refused) — telling the
    # lawyer to pick a later date would send him to a date the form refuses
    # (the trust register's « période_verrouillée_jour »).
    "date_administration_verrouillée_jour": (
        "Une conciliation complétée du compte d'administration couvre déjà la "
        "date d'aujourd'hui (conciliation au {detail}) : la recette de ce "
        "paiement d'honoraires ne peut s'inscrire aujourd'hui à aucune date "
        "permise. Réessayez demain, avec la date réelle du dépôt (champ "
        "« Date du dépôt au compte d'administration »). " + _NOTHING_WRITTEN
    ),
    "pas_un_paiement_honoraires": (
        "Cette écriture n'est pas un paiement d'honoraires."
    ),
    "recettes_illisibles": (
        "Les recettes d'administration liées à ce paiement d'honoraires n'ont "
        "pas pu être lues. " + _NOTHING_REVERSED + " Réessayez dans un instant."
    ),
    "contre_passation_administration_verrouillée": (
        "Une conciliation complétée du compte d'administration couvre déjà la "
        "date d'aujourd'hui : la recette liée à ce paiement d'honoraires ne "
        "peut pas être contre-passée aujourd'hui — ni, donc, le paiement "
        "lui-même (les deux vont ensemble). Réessayez demain. "
        + _NOTHING_REVERSED
    ),
    "motif_requis": "Un motif de contre-passation est requis.",
}

# An administration-side refusal inside the composite, reworded so the
# lawyer knows it concerns the RECETTE — and that nothing was written.
_ADMIN_REASON_TO_FEE = {
    "compte_introuvable": "compte_administration_introuvable",
    "compte_fermé": "compte_administration_fermé",
    "encaissement_carte_interdit": "compte_administration_invalide",
}


def _message(reason: str, detail: Optional[str] = None) -> str:
    text = _MESSAGES.get(reason, "Paiement d'honoraires refusé. " + _NOTHING_WRITTEN)
    return text.replace("{detail}", detail or "—")


def _midnight(value) -> Optional[datetime]:
    return trust._midnight_utc(value)


def _fail(report: Optional[dict], reason: str, message: str, *, side: str,
          operation: str, **ids) -> tuple[None, list[str]]:
    if report is not None:
        report["reason"] = reason
        report["side"] = side
    log_trust_event(
        "trust_fee_payment_refused", "refused", reason=reason,
        side=side, operation=operation,
        **{k: v for k, v in ids.items() if v},
    )
    return None, [message]


def create_fee_payment(
    data: dict,
    *,
    admin_account_id: str,
    admin_date=None,
    allow_external_ref: bool = False,
    _report_out: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Record a fee payment: trust entry + administration recette + the
    invoice's payment, in ONE transaction. Returns ``(result, [])`` or
    ``(None, [french_error])`` — on a refusal NOTHING is written.

    ``data`` is the trust entry (the fields of ``trust.create_transaction``;
    ``purpose`` is forced to the fee payment's, ``direction`` defaults to
    ``déboursé``). ``admin_account_id`` — REQUIRED (D-4). ``admin_date`` —
    the deposit date in the operations account, default the trust date
    (D16). ``allow_external_ref`` — accept a paper-invoice number instead of
    an Athéna invoice; the web form's path only.

    ``result``: ``{"trust_entry", "admin_recette", "invoice_before",
    "invoice_after", "client_balance"}`` (the invoice pair is ``None`` on
    the external-reference path). ``_report_out`` receives ``reason`` and
    ``side`` (``fidéicommis`` | ``administration`` | ``paiement``) on a
    refusal.
    """
    data = dict(data or {})
    if data.get("purpose") not in (None, "", TRUST_PURPOSE):
        return _fail(_report_out, "objet_invalide",
                     trust._abort_message("objet_invalide"),
                     side="paiement", operation="create")
    data["purpose"] = TRUST_PURPOSE
    data.setdefault("direction", "déboursé")
    admin_account_id = (admin_account_id or "").strip()
    external_ref = (data.get("invoice_external_ref") or "").strip()
    invoice_id = (data.get("invoice_id") or "").strip() or None

    if not admin_account_id:
        return _fail(_report_out, "compte_administration_requis",
                     _message("compte_administration_requis"),
                     side="paiement", operation="create")
    if external_ref and not allow_external_ref:
        return _fail(_report_out, "facture_externe_refusée",
                     _message("facture_externe_refusée"),
                     side="paiement", operation="create")
    if not invoice_id and not allow_external_ref:
        return _fail(_report_out, "facture_athena_requise",
                     _message("facture_athena_requise"),
                     side="paiement", operation="create")

    # The trust side's read-free rules FIRST (art. 58 names the method
    # before anything else is said about the deposit).
    t_ctx, reason, t_clean = trust._prepare_create(data, reserved_ok=(TRUST_PURPOSE,))
    if reason:
        return _fail(_report_out, reason, trust._abort_message(reason),
                     side="fidéicommis", operation="create",
                     account_id=t_clean.get("account_id"),
                     dossier_id=t_clean.get("dossier_id"))

    trust_date = _midnight(t_ctx["tx_date"])
    if admin_date is None or admin_date == "":
        deposit_date = trust_date
    else:
        deposit_date = _midnight(admin_date)
        if deposit_date is None:
            return _fail(_report_out, "date_administration_invalide",
                         _message("date_administration_invalide"),
                         side="paiement", operation="create")
    if deposit_date.date() < trust_date.date():
        return _fail(_report_out, "date_administration_antérieure",
                     _message("date_administration_antérieure"),
                     side="paiement", operation="create")
    if deposit_date.date() > today_mtl():
        return _fail(_report_out, "date_administration_future",
                     _message("date_administration_future"),
                     side="paiement", operation="create")

    invoice_backed = t_ctx["invoice_id"] is not None
    recette = {
        "account_id": admin_account_id,
        "kind": "encaissement_facture" if invoice_backed else "recette_autre",
        "amount": t_ctx["amount"],
        # The trust method (cheque or transfer — art. 58 already held): the
        # deposit is what the withdrawal was, never a hard-coded « virement ».
        "method": t_ctx["method"],
        # Placeholders the stage replaces with what the reads name (the
        # client and the dossier), validated non-empty meanwhile.
        "counterparty": "Fidéicommis",
        "description": "Paiement d'honoraires du fidéicommis",
        "reference": t_clean.get("reference", ""),
        "date": deposit_date,
        "invoice_id": t_ctx["invoice_id"],
        "dossier_id": None if invoice_backed else t_ctx["dossier_id"],
    }
    a_ctx, reason, _a_clean = al._prepare_create(
        recette, trust_transaction_id=t_ctx["tx_id"],
    )
    if reason:
        fee_reason = _ADMIN_REASON_TO_FEE.get(reason, reason)
        message = (
            _message(fee_reason) if fee_reason in _MESSAGES
            else f"Recette au compte d'administration refusée : "
                 f"{al._ABORT_MESSAGES.get(reason, 'opération refusée.')} "
                 f"{_NOTHING_WRITTEN}"
        )
        return _fail(_report_out, fee_reason, message, side="administration",
                     operation="create", account_id=admin_account_id)

    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _pay(txn) -> None:
        result.clear()
        # The instant is taken PER ATTEMPT (lot 5a): the decorator re-runs
        # this body after an Aborted commit, and a ``now`` captured before
        # the first attempt would stamp the entry EARLIER than the write
        # that aborted it — while its sequence comes after. The integrity
        # check orders a client's entries by creation instant, and would
        # read that as a running-balance error.
        now = datetime.now(timezone.utc)
        # 1. READS — the trust leg's, then the recette's (the invoice and the
        # dossier the trust leg read are passed on, never read twice).
        t_reads = trust._read_create(txn, t_ctx)
        a_reads = al._read_create(
            txn, a_ctx, dossier=t_reads["dossier"], invoice=t_reads["invoice"],
        )
        # 2. The composite's own guards, named before either register's.
        account = a_reads["account"]
        if account.get("account_type") != "opérations":
            raise _FeeAbort("compte_administration_invalide")
        if account.get("status") != "actif":
            raise _FeeAbort("compte_administration_fermé")
        floor = a_reads["lock_floor"]
        if floor is not None and deposit_date.date() <= floor.date():
            if floor.date() >= today_mtl():
                raise _FeeAbort("date_administration_verrouillée_jour",
                                detail=floor.strftime("%Y-%m-%d"))
            raise _FeeAbort("date_administration_verrouillée",
                            detail=deposit_date.strftime("%Y-%m-%d"))
        # 3. The trust leg: guards (art. 59 cleared funds, backdating, the
        # lock floor, the invoice), arithmetic, staged writes.
        t_result = trust._stage_create(txn, t_ctx, t_reads, now)
        entry = t_result["entry"]
        file_number = entry.get("dossier_file_number", "")
        description = f"Paiement d'honoraires du fidéicommis — dossier {file_number}"
        if not invoice_backed and entry.get("invoice_external_ref"):
            description += f" (facture {entry['invoice_external_ref']})"
        # 4. The recette and — for an Athéna invoice — its payment, computed
        # on the invoice read above: current + delta.
        a_result = al._stage_create(
            txn, a_ctx, a_reads, now,
            overrides={
                "counterparty": entry.get("client_name") or "Fidéicommis",
                "description": description,
            },
        )
        result.update(trust=t_result, admin=a_result)

    try:
        with span("trust.transaction", direction="déboursé", purpose=TRUST_PURPOSE,
                  dossier_id=t_ctx["dossier_id"]):
            _pay(transaction)
    except _FeeAbort as abort:
        return _fail(_report_out, abort.reason, _message(abort.reason, abort.detail),
                     side="administration", operation="create",
                     account_id=admin_account_id, dossier_id=t_ctx["dossier_id"])
    except trust._TxnAbort as abort:
        if abort.reason == "solde_compensé_insuffisant":
            log_trust_event(
                "trust_overdraft_refused", "refused",
                dossier_id=t_ctx["dossier_id"], account_id=t_ctx["account_id"],
                reason="insufficient_cleared_balance",
            )
        return _fail(_report_out, abort.reason,
                     trust._abort_message(abort.reason, abort.detail),
                     side="fidéicommis", operation="create",
                     account_id=t_ctx["account_id"], dossier_id=t_ctx["dossier_id"])
    except al._TxnAbort as abort:
        fee_reason = _ADMIN_REASON_TO_FEE.get(abort.reason)
        if fee_reason:
            message = _message(fee_reason)
        else:
            message = (f"Recette au compte d'administration refusée : "
                       f"{al._abort_message(abort, 'opération refusée.')} "
                       f"{_NOTHING_WRITTEN}")
        return _fail(_report_out, fee_reason or abort.reason, message,
                     side="administration", operation="create",
                     account_id=admin_account_id, dossier_id=t_ctx["dossier_id"])
    except Exception as exc:
        log_unexpected("fee payment write failed", error_type=type(exc).__name__)
        if _report_out is not None:
            _report_out["reason"] = "erreur"
            _report_out["side"] = "paiement"
        return None, ["Erreur lors de l'enregistrement du paiement d'honoraires. "
                      "Rien n'a été inscrit. Veuillez réessayer."]

    t_result = result["trust"]
    a_result = result["admin"]
    entry = t_result["entry"]
    recette_doc = a_result["entry"]
    # The two registers' commit records and log lines — the same writes
    # their own public functions would have committed.
    provenance.note_commit(trust.TRANSACTIONS_COLLECTION, entry["id"])
    log_trust_event(
        "trust_transaction_created", transaction_id=entry["id"],
        dossier_id=t_ctx["dossier_id"], account_id=t_ctx["account_id"],
        direction=t_ctx["direction"], purpose=TRUST_PURPOSE, sequence=entry["sequence"],
    )
    al._after_create_commit(a_ctx, a_result)
    log_trust_event(
        "trust_fee_payment_recorded", transaction_id=entry["id"],
        dossier_id=t_ctx["dossier_id"], account_id=t_ctx["account_id"],
        admin_transaction_id=recette_doc["id"],
        invoice_id=t_ctx["invoice_id"], admin_date_differs=(
            deposit_date.date() != trust_date.date()),
    )
    invoice_before = a_result.get("invoice_before")
    payment = a_result.get("payment")
    return {
        "trust_entry": entry,
        "admin_recette": recette_doc,
        "invoice_before": invoice_before,
        "invoice_after": (
            {**invoice_before, **payment} if invoice_before is not None and payment
            else None
        ),
        "client_balance": t_result.get("client_balance"),
    }, []


def _standing(row: dict) -> bool:
    """An administration recette whose effect still stands — neither
    reversed, annulée, nor itself a reversal row."""
    return not (
        row.get("reversed_by_id")
        or row.get("status") == "annulée"
        or row.get("kind") == al.REVERSAL_KIND
    )


def reverse_fee_payment(
    tx_id: str, reason: str, *, _report_out: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Reverse a fee payment: the trust entry, EVERY standing
    administration recette linked to it, and each recette's payment on its
    invoice — in ONE transaction. Returns ``(result, [])`` or ``(None,
    [french_error])``; on a refusal nothing is written.

    The linked recettes are read INSIDE the transaction
    (``admin_ledger.list_by_trust_transaction(…, txn)``): an unreadable
    link refuses — never « nothing to reverse » — and a recette written
    meanwhile re-runs the commit. A legacy fee payment with no linked
    recette (written before the administration register) reverses at trust
    alone, and the result says so (``admin_reversals == []``,
    ``linked_recettes == 0``).

    ``result``: ``{"trust_reversal", "trust_reversals",
    "original_status_after", "admin_reversals": [{"admin_transaction_id",
    "reversal_id"}], "linked_recettes", "invoices": [(invoice_id, before,
    after)], "client_cleared_after"}`` — ``linked_recettes`` counts EVERY
    row linked to the fee payment, already-reversed ones included, so an
    empty ``admin_reversals`` can be told apart: nothing was ever linked
    (0), or everything linked was already reversed (> 0).
    """
    # Sanitized like every stored text (tags stripped, bounded): the motif is
    # printed in both registers.
    reason = sanitize((reason or "").strip(), max_length=500)
    if not reason:
        return _fail(_report_out, "motif_requis", _message("motif_requis"),
                     side="paiement", operation="reverse", transaction_id=tx_id)

    admin_reason = f"Contre-passation du paiement d'honoraires au fidéicommis — {reason}"
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _reverse(txn) -> None:
        result.clear()
        # The instant is taken PER ATTEMPT (lot 5a): the decorator re-runs
        # this body after an Aborted commit, and a ``now`` captured before
        # the first attempt would stamp the entry EARLIER than the write
        # that aborted it — while its sequence comes after. The integrity
        # check orders a client's entries by creation instant, and would
        # read that as a running-balance error.
        now = datetime.now(timezone.utc)
        today = trust._today_midnight_utc()
        # 1. READS — the trust original and its reversal context, then the
        # linked recettes (fail CLOSED), then what their reversals touch.
        t_ctx = trust._read_reverse(txn, tx_id, today, fee_payment_ok=True)
        if t_ctx["original"].get("purpose") != TRUST_PURPOSE:
            raise _FeeAbort("pas_un_paiement_honoraires")
        try:
            rows = al.list_by_trust_transaction(tx_id, txn=txn)
        except Exception:
            raise _FeeAbort("recettes_illisibles")
        standing = [r for r in rows if _standing(r)]
        a_ctx = al._read_reverse_legs(txn, standing) if standing else None
        # 2. The composite's guard: the recettes reverse TODAY too.
        if a_ctx is not None:
            for info in a_ctx["accounts"].values():
                floor = info["floor"]
                if floor is not None and today.date() <= floor.date():
                    raise _FeeAbort("contre_passation_administration_verrouillée")
        # 3. Stage both — the trust reversal, then every recette's (each
        # reducing its invoice's payment; paid < reduce refuses, never clamps).
        t_result = trust._stage_reverse(txn, t_ctx, reason, today, now)
        a_result = (
            al._stage_reverse_legs(txn, a_ctx, admin_reason, today, now)
            if a_ctx is not None else {"reversals": [], "legs": [], "invoices": []}
        )
        # Every linked row, standing or not: « none was ever linked » (a
        # legacy fee payment) and « all were already reversed » are two
        # different facts, and the caller must not word one as the other.
        result.update(trust=t_result, admin=a_result, linked=len(rows))

    try:
        with span("trust.transaction", direction="reversal", purpose=TRUST_PURPOSE,
                  dossier_id=None):
            _reverse(transaction)
    except _FeeAbort as abort:
        return _fail(_report_out, abort.reason, _message(abort.reason, abort.detail),
                     side="administration" if abort.reason != "pas_un_paiement_honoraires"
                     else "paiement", operation="reverse", transaction_id=tx_id)
    except trust._TxnAbort as abort:
        return _fail(_report_out, abort.reason,
                     trust._abort_message(abort.reason, abort.detail,
                                          "Contre-passation refusée."),
                     side="fidéicommis", operation="reverse", transaction_id=tx_id)
    except al._TxnAbort as abort:
        return _fail(
            _report_out, abort.reason,
            f"Contre-passation de la recette au compte d'administration "
            f"refusée : {al._abort_message(abort, 'contre-passation refusée.')} "
            f"{_NOTHING_REVERSED}",
            side="administration", operation="reverse", transaction_id=tx_id,
        )
    except Exception as exc:
        log_unexpected("fee payment reversal failed", error_type=type(exc).__name__)
        if _report_out is not None:
            _report_out["reason"] = "erreur"
            _report_out["side"] = "paiement"
        return None, ["Erreur lors de la contre-passation du paiement d'honoraires. "
                      "Rien n'a été contre-passé. Veuillez réessayer."]

    t_result = result["trust"]
    a_result = result["admin"]
    trust._after_reverse_commit(t_result)
    if a_result["reversals"]:
        al._after_reverse_commit(a_result, original_id=tx_id)
    admin_reversals = [
        {"admin_transaction_id": rev.get("reverses_id"), "reversal_id": rev["id"]}
        for rev in a_result["reversals"]
    ]
    log_trust_event(
        "trust_fee_payment_reversed", transaction_id=tx_id,
        account_id=t_result["reversal"].get("account_id"),
        dossier_id=t_result["reversal"].get("dossier_id"),
        reversal_id=t_result["reversal"]["id"],
        admin_reversal_count=len(admin_reversals),
    )
    return {
        "trust_reversal": t_result["reversal"],
        "trust_reversals": list(t_result["reversals"]),
        "original_status_after": t_result.get("original_status_after"),
        "admin_reversals": admin_reversals,
        "linked_recettes": result["linked"],
        "invoices": [
            (iid, before, {**before, **updates})
            for iid, before, updates in a_result["invoices"]
        ],
        "client_cleared_after": t_result.get("client_cleared_after"),
    }, []
