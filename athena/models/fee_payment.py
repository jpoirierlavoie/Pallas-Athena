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
  decision), the connector never will (D1: an Athéna invoice only);
* the PAYEE (D23, 2026-09-29, art. 58 — « chèque tiré à l'ordre de
  l'avocat »): the trust entry's « Bénéficiaire » is the lawyer or his firm
  and nobody else — the two names the firm profile holds
  (``settings/cabinet``: ``nom`` and ``organisation``). Any other value is
  refused (``bénéficiaire_honoraires_invalide`` — a blank one too, ahead of
  the register's generic « contrepartie requise »), and the one accepted is
  stored as the profile spells it (:func:`match_fee_payee` folds case,
  Unicode composition and spacing only). The GUARD reads the profile
  STRICTLY (``models/settings.get_cabinet_strict``): a read failure refuses
  (« erreur », nothing written) instead of judging against the deploy-time
  seed, as :func:`fee_payees` — the display list, fail-open like every
  reader of the profile that only renders it — would. This model never
  picks a payee: the connector, when its caller names none, and the web
  form's preselection take :func:`default_fee_payee` — the LAWYER on a
  cheque, since art. 58 draws a fee cheque « à l'ordre de l'avocat » and
  names the société only as the holder of a transfer's account; the firm
  on a transfer. The firm named explicitly stays accepted on a cheque: D23
  lets the lawyer choose either name, and only he knows whether his firm is
  a distinct société.

The trust side's rules stay the trust model's: art. 58 (cheque or transfer
only), art. 59 (cleared funds), the backdating guard, the lock floor, an
issued invoice of the same dossier — addressed to the client whose funds
are withdrawn (D21: ``facture_autre_client``, no override) —, no provision
imputed on it, the live balance. The trust purpose ``virement_honoraires``
is RESERVED to this module on the public create and reverse paths.

Provenance is the writer's context (``models/provenance``), never an
argument: ``mcp`` under the connector's ``writing_via``, the request's
blueprint otherwise.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

from models import admin_ledger as al
from models import db, provenance
from models import settings as settings_model
from models import trust
from security import sanitize
from utils import cabinet as cabinet_util
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

# The composite's answer when its outcome is UNKNOWN (review of lot 5a, step
# 3): its transaction raised after a commit was attempted — a commit whose
# answer is lost may have landed —, or a re-run of its body found the trust
# entry THIS call minted (``trust._OwnCommitLanded``: an earlier attempt
# landed, and the unguarded re-run withdrew the fees a SECOND time). Never
# « rien n'a été inscrit » — a retry of a fee payment is a second withdrawal
# of the client's funds —, and the connector (lot 5b) recognizes this
# constant to keep its idempotency claim, as it does
# ``models.invoice.CREATE_OUTCOME_UNCERTAIN``.
CREATE_OUTCOME_UNCERTAIN = (
    "L'enregistrement du paiement d'honoraires a échoué d'une façon qui ne "
    "permet pas de savoir s'il a été inscrit : vérifiez le journal du "
    "fidéicommis et la facture avant de réessayer."
)
REVERSE_OUTCOME_UNCERTAIN = (
    "La contre-passation du paiement d'honoraires a échoué d'une façon qui ne "
    "permet pas de savoir si elle a été inscrite : vérifiez l'écriture au "
    "fidéicommis avant de réessayer."
)
OUTCOME_UNCERTAIN_REASON = trust.OUTCOME_UNCERTAIN_REASON
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
    # D23 (art. 58) — ``{detail}`` is the accepted names, as the firm
    # profile holds them (never a client's). « Au profit de », never « à
    # l'ordre de »: art. 58 draws a CHEQUE « à l'ordre de l'avocat » only,
    # and names the société as the holder of a TRANSFER's account — a text
    # saying the article allows a cheque to the firm would misquote it.
    "bénéficiaire_honoraires_invalide": (
        "Un paiement d'honoraires ne se fait qu'au profit de l'avocat ou de "
        "son cabinet (art. 58, RLRQ c. B-1, r. 5 : chèque à l'ordre de "
        "l'avocat, ou virement à un compte ouvert à son nom ou à celui de la "
        "société au sein de laquelle il exerce) : le bénéficiaire est "
        "{detail}, tel que le nomme le profil du cabinet (Paramètres). "
        + _NOTHING_WRITTEN
    ),
    "bénéficiaires_honoraires_inconnus": (
        "Le profil du cabinet (Paramètres) ne nomme ni l'avocat ni le "
        "cabinet : un paiement d'honoraires, qui ne se fait qu'au profit de "
        "l'un ou de l'autre (art. 58, RLRQ c. B-1, r. 5), ne peut désigner "
        "son bénéficiaire. Complétez le profil, puis réessayez. "
        + _NOTHING_WRITTEN
    ),
}

# The firm profile could not be read (``settings.CabinetIllisible``): the
# payee rule cannot be judged, and is never judged against the deploy-time
# seed instead. An operational failure, not a refusal — the same « erreur »
# as a register read that fails before any commit.
_PROFILE_UNREADABLE = (
    "Impossible de lire le profil du cabinet (Paramètres), qui nomme le "
    "bénéficiaire d'un paiement d'honoraires (art. 58) : rien n'a été "
    "inscrit. Veuillez réessayer."
)

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


# ── The payee (D23, art. 58) ─────────────────────────────────────────────


def _payee_key(value) -> str:
    """The comparison form of a payee name: Unicode composition (NFC), case
    and runs of whitespace folded — never an accent dropped (« Lavoie » and
    « Lavoié » are two names)."""
    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(text.split()).casefold()


def fee_payees() -> list[str]:
    """The payees D23 accepts for a fee payment: the FIRM, then the
    LAWYER — ``organisation`` and ``nom`` of the firm profile
    (``utils/cabinet.cabinet_dict``, which fails open to the deploy-time
    seed), a blank one dropped, a name given twice kept once. The firm comes
    first: it is the default payee of a TRANSFER (to the firm's own
    non-trust account), and the order every surface shows. A CHEQUE's
    default is the lawyer (:func:`default_fee_payee`).

    The DISPLAY list — the web form's select, the connector's pre-check and
    default. Never the guard's: :func:`create_fee_payment` judges on
    :func:`guard_fee_payees`, which fails CLOSED."""
    return _payees_from(cabinet_util.cabinet_dict())


def guard_fee_payees() -> list[str]:
    """The same list, read for a DECISION: through
    ``models/settings.get_cabinet_strict``, so an unreadable profile raises
    ``settings.CabinetIllisible`` instead of yielding the deploy-time seed —
    the ``ORGANISATION_SEED`` literal and ``FIRM_NAME``, names the lawyer may
    have cleared or replaced since (review of D23, concurrency and
    atomicity). The seed answers only when no profile was ever saved. Read
    by the payment's guard and by ``scripts/verify_trust_integrity``."""
    return _payees_from(settings_model.get_cabinet_strict())


def _payees_from(cab: dict) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for key in ("organisation", "nom"):
        name = " ".join(str(cab.get(key) or "").split())
        folded = _payee_key(name)
        if folded and folded not in seen:
            seen.add(folded)
            names.append(name)
    return names


def match_fee_payee(value, payees: Optional[list[str]] = None) -> Optional[str]:
    """The accepted payee *value* names, spelt as the firm profile spells
    it — or ``None`` when it names neither the lawyer nor the firm."""
    wanted = _payee_key(value)
    if not wanted:
        return None
    for name in (fee_payees() if payees is None else payees):
        if _payee_key(name) == wanted:
            return name
    return None


def default_fee_payee(method: str) -> Optional[str]:
    """The payee a fee payment takes when its caller names none — the
    connector's default, the web form's first choice. On a CHEQUE
    (``chèque``) the LAWYER: art. 58 draws a fee cheque « à l'ordre de
    l'avocat » and names the société only as the holder of a transfer's
    account, so his is the one name the article allows on either method. On
    a TRANSFER the firm, whose non-trust account receives the fees (the
    first of :func:`fee_payees`). The lawyer when the profile names no firm,
    the firm when it names no lawyer; ``None`` when it names nobody. A
    DEFAULT only: the firm, named explicitly, stays accepted on a cheque
    (D23 — the lawyer's choice, who alone knows whether his firm is a
    distinct société)."""
    cab = cabinet_util.cabinet_dict()
    payees = _payees_from(cab)
    if not payees:
        return None
    if method == "chèque":
        lawyer = match_fee_payee(cab.get("nom"), payees)
        if lawyer:
            return lawyer
    return payees[0]


def payees_label(payees: list[str]) -> str:
    """« A » ou « B » — the accepted names, for a refusal."""
    return " ou ".join(f"« {name} »" for name in payees)


def _uncertain(report: Optional[dict], message: str, log_message: str,
               **ids) -> tuple[None, list[str]]:
    """The answer of an operation whose outcome is unknown — never a
    refusal line (nothing was refused; the write may stand). Ids only."""
    log_unexpected(log_message, exc_info=False,
                   **{k: v for k, v in ids.items() if v})
    if report is not None:
        report["reason"] = OUTCOME_UNCERTAIN_REASON
        report["side"] = "paiement"
    return None, [message]


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

    # D23 (art. 58) — the accepted payees, read ONCE and STRICTLY (review of
    # D23, concurrency and atomicity): an unreadable profile refuses here,
    # nothing written, rather than degrading to the deploy-time seed, which
    # would make this guard accept a name the lawyer cleared or replaced.
    # Outside the transaction: the profile is no money figure, and read here
    # it is read once rather than on every retry of the commit.
    try:
        payees = guard_fee_payees()
    except settings_model.CabinetIllisible:
        ids = {"account_id": data.get("account_id"),
               "dossier_id": data.get("dossier_id")}
        log_unexpected("fee payment: firm profile unreadable", exc_info=False,
                       **{k: v for k, v in ids.items() if v})
        if _report_out is not None:
            _report_out["reason"] = "erreur"
            _report_out["side"] = "paiement"
        return None, [_PROFILE_UNREADABLE]

    # A fee payment that names NO payee is refused on the D23 rule, not the
    # register's generic « contrepartie requise »: the web form's payee is a
    # select of the firm profile's names, so an empty profile posts none —
    # and the lawyer must read that the PROFILE is what is missing.
    if not str(data.get("counterparty") or "").strip():
        reason = ("bénéficiaire_honoraires_invalide" if payees
                  else "bénéficiaires_honoraires_inconnus")
        return _fail(_report_out, reason, _message(reason, payees_label(payees)),
                     side="paiement", operation="create",
                     account_id=data.get("account_id"),
                     dossier_id=data.get("dossier_id"))

    # The trust side's read-free rules FIRST (art. 58 names the method
    # before anything else is said about the deposit).
    t_ctx, reason, t_clean = trust._prepare_create(data, reserved_ok=(TRUST_PURPOSE,))
    if reason:
        return _fail(_report_out, reason, trust._abort_message(reason),
                     side="fidéicommis", operation="create",
                     account_id=t_clean.get("account_id"),
                     dossier_id=t_clean.get("dossier_id"))

    # D23 (art. 58) — the payee is the lawyer or his firm, as the firm
    # profile names them (the list read above, never re-read), and it is
    # stored as the profile spells it.
    if not payees:
        return _fail(_report_out, "bénéficiaires_honoraires_inconnus",
                     _message("bénéficiaires_honoraires_inconnus"),
                     side="paiement", operation="create",
                     account_id=t_ctx["account_id"], dossier_id=t_ctx["dossier_id"])
    payee = match_fee_payee(t_ctx["counterparty"], payees)
    if payee is None:
        return _fail(_report_out, "bénéficiaire_honoraires_invalide",
                     _message("bénéficiaire_honoraires_invalide",
                              payees_label(payees)),
                     side="paiement", operation="create",
                     account_id=t_ctx["account_id"], dossier_id=t_ctx["dossier_id"])
    t_ctx["counterparty"] = payee

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
    # Sticky across attempts: once a body has staged its writes, a commit was
    # attempted, and a later raise no longer proves that nothing landed.
    attempted: dict = {"commit": False}

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
        # dossier the trust leg read are passed on, never read twice). The
        # trust leg's FIRST read is the entry this call minted: found, an
        # earlier attempt LANDED (trust._OwnCommitLanded) — its answer lost,
        # the commit retried and answered Aborted — and this re-run must not
        # withdraw the fees, record the recette and pay the invoice again.
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
        attempted["commit"] = True

    try:
        with span("trust.transaction", direction="déboursé", purpose=TRUST_PURPOSE,
                  dossier_id=t_ctx["dossier_id"]):
            _pay(transaction)
    except (trust._OwnCommitLanded, al._OwnCommitLanded):
        return _uncertain(_report_out, CREATE_OUTCOME_UNCERTAIN,
                          "fee payment: an earlier attempt of the transaction landed",
                          account_id=t_ctx["account_id"], dossier_id=t_ctx["dossier_id"])
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
        if attempted["commit"]:
            return _uncertain(_report_out, CREATE_OUTCOME_UNCERTAIN,
                              "fee payment: transaction failed after a commit attempt",
                              account_id=t_ctx["account_id"],
                              dossier_id=t_ctx["dossier_id"],
                              error_type=type(exc).__name__)
        # No body ever staged its writes: a READ failed before any commit
        # was attempted — nothing can have landed.
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
    tx_id: str, reason: str, *, expected_etag: Optional[str] = None,
    _report_out: Optional[dict] = None,
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

    ``expected_etag`` — the version of the TRUST entry the caller decided
    on (``trust._read_reverse``): a mismatch refuses, nothing written.
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
    attempted: dict = {"commit": False}

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
        t_ctx = trust._read_reverse(txn, tx_id, today, fee_payment_ok=True,
                                    expected_etag=expected_etag)
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
        attempted["commit"] = True

    try:
        with span("trust.transaction", direction="reversal", purpose=TRUST_PURPOSE,
                  dossier_id=None):
            _reverse(transaction)
    except _FeeAbort as abort:
        return _fail(_report_out, abort.reason, _message(abort.reason, abort.detail),
                     side="administration" if abort.reason != "pas_un_paiement_honoraires"
                     else "paiement", operation="reverse", transaction_id=tx_id)
    except trust._TxnAbort as abort:
        if abort.reason == "déjà_contrepassée" and attempted["commit"]:
            # A re-run found the fee payment reversed AFTER this call had
            # attempted its own commit: most likely that commit landed with
            # its answer lost (the reversal ids are minted per attempt, so
            # the re-run cannot tell it from another caller's). « Déjà
            # contre-passée » worded as a refusal would tell the lawyer his
            # reversal failed; it is uncertain, and the entry says which.
            return _uncertain(_report_out, REVERSE_OUTCOME_UNCERTAIN,
                              "fee payment reversal: reversed after this call's "
                              "own commit attempt", transaction_id=tx_id)
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
        if attempted["commit"]:
            return _uncertain(_report_out, REVERSE_OUTCOME_UNCERTAIN,
                              "fee payment reversal: transaction failed after a "
                              "commit attempt", transaction_id=tx_id,
                              error_type=type(exc).__name__)
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
