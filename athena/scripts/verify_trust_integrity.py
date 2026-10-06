"""Verify trust-register integrity (Phase K §13). READ-ONLY — writes nothing.

Run before inspecting the register, and before deploying any change to the
trust model's rules:

    python -m scripts.verify_trust_integrity [--revue FICHIER]

Two kinds of findings, and the exit code says which were found (each
printed with a key; ``--revue`` names the file of findings the lawyer has
already reviewed — :mod:`scripts.findings_review` says how a review counts):

* an ÉCART (error) is a figure or an invariant the register cannot stand
  on today — a denormalized balance that no longer adds up, a completed
  reconciliation that no longer re-proves, a date order the carried-forward
  balance relies on, a client in shortfall. Exit 1.
* a NOTE is history: an entry the model would REFUSE today but accepted
  when it was written, or a reconciliation completed under semantics the
  current re-proof does not share. The register is append-only and history
  is never rewritten, so a note is a decision for the lawyer, never an
  automatic repair. Exit 2 when there are notes and no écart; 0 when clean.

The reads are not one snapshot (see :func:`_write_marks`): a pass a
register write crossed is re-read, up to ``STABLE_READ_ATTEMPTS`` passes,
and when every pass was crossed the first écart says so instead of letting
a half-old, half-new read pass for a finding.

Checks 1-4 (Phase K) recompute the frozen and denormalized balances:

  1. ``balance_after_account`` on every row == the running book balance in
     ``sequence`` order. ONE mismatch is history, not an écart: until lot 5
     an inter-dossier transfer stored the pair's NET balance on BOTH legs,
     so its FIRST leg (the déboursé) reads high by exactly the amount. That
     signature — a transfer's déboursé leg, its recette leg the next
     sequence, the stored figure the recomputed one plus the amount — is a
     NOTE (the register is append-only; new transfers store the exact
     figure). Any other mismatch stays an écart;
  2. the account's ``book_balance`` / ``bank_balance`` == Σ deltas;
  3. per (dossier, client) couple found in the register:
     ``balance_after_client`` and the dossier's two maps == Σ deltas;
  4. per dossier with register rows: ``trust_balance`` == Σ its clients.

Checks 5-10 (lot 5a, 2026-09-28):

  5. every COMPLETED reconciliation still re-proves at its ``period_end``
     (the as-of context and variance the completion gate itself uses:
     statement + in transit − outstanding − book as of == 0). A
     reconciliation completed BEFORE the as-of rework — commit 945572a,
     2026-07-29 22:37:43 HAE — ran the old completion code, where the
     tickable set was « en circulation NOW » and a ticked entry dated after
     the period still got ``cleared_date = period_end``; it may fail a
     re-proof it was never held to, so it is a NOTE until the lawyer has
     reviewed it. The plan says « before 2026-07-29 »; the cutoff is the
     commit instant because a reconciliation completed during the day of the
     29th could only have run the old code.
  6. ``cleared_date`` coherence: a compensée entry carries a cleared_date,
     never before its own date, never in the future (Montréal), and never
     INSIDE a period whose reconciliation had already been completed when
     the clear was made. That last one is the defect the clearing lock floor
     closes: such a clear takes the entry out of the period's resurrection
     set, so the closed reconciliation silently stops re-proving. The clear
     instant is exact when a reconciliation made it (its
     ``completed_date``) or when the clear was the entry's last write
     (``updated_at`` — a trust entry is written again only when reversed,
     or rectified by ``scripts/rectify_registers``, whose trail in
     ``revisions`` keeps the ``updated_at`` it replaced: that one is read);
     for a reversed entry it is bracketed by ``created_at`` and
     ``updated_at``, and an undecidable bracket is a NOTE, never a guessed
     écart. Entries born compensée (transfer legs, their pair reversals) are
     cleared at creation: check 8's lock floor covers them. Anything tied to
     a reconciliation completed before the as-of rework is a NOTE (see 5).
  7. per account, dates are non-decreasing in ``sequence`` order and none is
     in the future (Montréal). ``book_balance_as_of`` and
     ``opening_book_balance`` read ONE frozen balance back and are exact
     only under that invariant; a future date also blocks every later entry
     through the backdating guard. A duplicated sequence number leaves the
     order itself undefined.
  8. NOTES — the rules the model enforces since lot 0b (D14, RLRQ c. B-1,
     r. 5 verified 2026-09-25), applied to the entries already written:
     the lock floor on create / reverse / transfer (an entry written after a
     reconciliation was completed but dated inside its period); art. 57 (no
     cash withdrawal) with its art. 72 exception (the cash refund of a cash
     receipt of 7 500 $ or more, which the refund must cite); art. 58 (a fee
     payment leaves by cheque or by transfer); no fee payment against an
     invoice carrying a provision (``retainer_applied > 0``); a correction
     is never reversed again; a two-leg transfer reverses whole; and every
     SINGLE-leg ``virement_inter_dossiers`` (no linked counter-leg — the
     create form offered the purpose until decision D24 reserved it to the
     two-leg transfer, 2026-09-29) is listed, and so is every entry whose
     objet contradicts its sens (``trust.PURPOSE_DIRECTIONS`` — refused by
     the connector since lot 5b, by the MODEL for every caller since D24).
     Since the same day's decisions, two fee-payment rules as well: an
     invoice addressed to ANOTHER client of the dossier than the one whose
     funds left (D21), and a payee who is neither the lawyer nor his firm as
     the firm profile names them (D23, art. 58 — the profile read STRICTLY:
     unreadable, it is an ÉCART, the payee check not run, never a clean
     pass judged on the deploy-time seed). Each is refused for every
     new entry; what the register already holds stays there, and is listed.
     Annulée entries (a cheque that never left the account) and corrections
     (which copy their original's method and withdraw nothing of their own)
     are outside the three withdrawal rules and the two fee-payment ones.
  9. per-client balances vs Σ deltas from the DOSSIER side: every dossier is
     read, so a stored map entry — or a ``trust_balance`` — that no register
     row backs at all is reported; checks 3 and 4 start from the rows and
     cannot see it. And a client whose book or cleared balance is NEGATIVE
     is a trust shortfall the lawyer must cover: reversing a compensée
     recette bypasses the overdraft control by design, so the register can
     reach one.
 10. the D-4 linkage between a trust fee payment and the administration
     register (lot 5a review). A « paiement d'honoraires » moves money OUT of
     trust INTO the operations account, so the admin register must hold,
     linked by ``trust_transaction_id``, standing recettes adding up to
     exactly its amount while the fee payment stands — and none once it is
     reversed or annulled. Until lot 5a the web wrote that recette AFTER the
     trust commit and failed open (a banner), and the reversal cascade
     reversed only the FIRST linked recette, so history can have drifted in
     both directions in silence: the operations account then under- or
     over-states the firm's cash, and the invoice's recorded payment follows
     it (since lot 5a both are ONE transaction, models/fee_payment, so new
     entries cannot drift this way). A fee payment with NO linked
     recette at all is a NOTE when it was written before the D-4 rule
     (commit e719588, 2026-08-17 11:23 HAE — the admin account became
     mandatory; the reprise of the same day back-filled the history), or
     when an UNLINKED standing recette of the same amount (an encaissement
     of the same invoice, when it names one) dated on or after it exists —
     the one the trust page's banner tells the lawyer to enter by hand, which
     can never carry the link. Every other mismatch, and a standing recette
     linked to an entry that is not a trust fee payment, is an écart. An
     unreadable admin register is an écart too: a check that cannot read
     must say so, never pass. And, as a NOTE (D16 applied to history), a
     linked recette dated BEFORE the fee payment it carries.

Checks 11-12 (2026-10-06 — what an edit outside the application leaves):

 11. NOTE — a date-only field (``date``, ``cleared_date``) carrying a time of
     day. The model stores both at midnight UTC on every path, so any other
     value was written outside it — the Firestore console edits in LOCAL
     time, and « 2026-09-01 at 7 PM » is stored 2026-09-01T23:00Z. Every
     reader still takes its UTC day, and the art. 38 sheet orders by day
     since the same change (``models/trust.register_order``); before it, the
     sheet sorted on the full timestamp and printed such an entry after the
     rest of its day, its frozen balance out of place.
 12. ÉCART — the numbering. Every number from 1 to the account's counter
     must be on an entry, and the counter cannot be behind the highest one.
     The application never deletes a trust entry, so a missing number is an
     entry deleted outside it: the art. 38 register is incomplete, even when
     the frozen balances around the gap still add up (a pair that cancelled
     out) — which is exactly why it needs its own check. The gap is
     :class:`~scripts.findings_review.Reviewable`: restored from a backup, it
     disappears; explained instead, the lawyer records the explanation in
     the review file. A counter behind the highest number would make the
     next entry reuse a number; that one is never reviewable.
"""

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from models import admin_ledger, db, fee_payment, trust
from models import settings as settings_model
from models.dossier import get_dossier
from scripts.findings_review import Reviewable, report
from tz import to_mtl
from utils.deadlines import today_mtl

# Every entry the LAST pass of :func:`collect` read, by id — what a finding's
# review key binds to (:func:`scripts.findings_review.finding_key`).
_LAST_ENTRIES: dict[str, dict] = {}

# The instant the as-of reconciliation rework was committed (945572a,
# « Fidéicommis : conciliation rétroactive (as-of) », 2026-07-29 22:37:43
# -04:00). A reconciliation completed before it ran the old completion code.
AS_OF_REWORK_AT = datetime(2026, 7, 30, 2, 37, 43, tzinfo=timezone.utc)

# The instant the D-4 rule was committed (e719588, « un paiement d'honoraires
# nomme son compte d'administration », 2026-08-17 11:23:44 -04:00): from then
# on a fee payment could not be written without naming the admin account its
# recette goes to. One written before it may have none (check 10).
D4_RULE_AT = datetime(2026, 8, 17, 15, 23, 44, tzinfo=timezone.utc)

# The trust purpose whose money lands in the administration register.
_FEE_PAYMENT_PURPOSE = "virement_honoraires"

# The purposes an entry can be BORN compensée under (a transfer leg, and the
# correction of a two-leg transfer): cleared at creation, never by a clear.
_BORN_CLEARED_PURPOSES = (trust.TRANSFER_PURPOSE, trust.REVERSAL_PURPOSE)


def _account_transactions(account_id: str) -> list[dict]:
    return [
        d.to_dict()
        for d in db.collection(trust.TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence")
        .stream()
    ]


def _sum(txs, key):
    return sum(
        trust.compute_deltas(t.get("direction", ""), int(t.get("amount", 0)), t.get("status", ""))[key]
        for t in txs
    )


def _day(value) -> Optional[date]:
    """UTC calendar day of a date-only field (stored at midnight UTC)."""
    v = trust._as_utc(value)
    return v.date() if isinstance(v, datetime) else None


def _instant(value) -> Optional[datetime]:
    """A true timestamp as tz-aware UTC, or None."""
    v = trust._as_utc(value)
    return v if isinstance(v, datetime) else None


def _stamp(value) -> str:
    """A true timestamp as the lawyer reads it — Montréal wall clock."""
    v = _instant(value)
    return to_mtl(v).strftime("%Y-%m-%d %H:%M") if v else "date inconnue"


def _pre_as_of(rec: dict) -> bool:
    """Completed before the as-of rework — or at an unknown instant, which
    cannot be placed after it."""
    completed = _instant(rec.get("completed_date"))
    return completed is None or completed < AS_OF_REWORK_AT


def _rec_label(rec: dict) -> str:
    return (
        f"conciliation {rec.get('id')} (fin de période {_day(rec.get('period_end'))}, "
        f"complétée le {_stamp(rec.get('completed_date'))})"
    )


def _where(aid: str, tx: dict) -> str:
    return f"compte {aid} seq {tx.get('sequence')} (écriture {tx.get('id')})"


def _born_cleared(tx: dict) -> bool:
    """Transfer legs and their pair reversals are created compensée."""
    return bool(tx.get("related_transaction_id")) and tx.get("purpose") in _BORN_CLEARED_PURPOSES


def _legacy_transfer_leg(tx: dict, stored: int, expected: int, by_sequence: dict) -> bool:
    """The one check-1 mismatch that is history: the FIRST leg of a two-leg
    inter-dossier transfer written before lot 5, which stored the pair's net
    balance — high by exactly its amount — where the running balance after a
    déboursé is lower. Recognized by its whole signature, never by a figure
    alone: a transfer déboursé, linked to a recette leg of the same transfer
    that is the NEXT sequence (both legs are written together), and a
    difference of exactly the amount. Anything else is an écart."""
    if tx.get("purpose") != trust.TRANSFER_PURPOSE or tx.get("direction") != "déboursé":
        return False
    pair_id = tx.get("related_transaction_id")
    if not pair_id:
        return False
    pair = by_sequence.get((tx.get("sequence") or 0) + 1)
    if (not pair or pair.get("id") != pair_id
            or pair.get("purpose") != trust.TRANSFER_PURPOSE
            or pair.get("direction") != "recette"
            or pair.get("related_transaction_id") != tx.get("id")):
        return False
    return stored - expected == int(tx.get("amount", 0) or 0)


# ── check 5 ───────────────────────────────────────────────────────────────


def _check_reproof(aid: str, by_period: list[dict], problems: list, notes: list) -> None:
    for rec in by_period:
        try:
            ctx = trust.reconciliation_as_of_context(aid, rec.get("period_end"))
        except Exception as exc:
            problems.append(
                f"{_rec_label(rec)}: contexte as-of illisible ({type(exc).__name__})"
            )
            continue
        outstanding = ctx["fixed_outstanding_total"] + sum(
            int(e.get("amount", 0)) for e in ctx["outstanding"]
        )
        in_transit = ctx["fixed_in_transit_total"] + sum(
            int(e.get("amount", 0)) for e in ctx["in_transit"]
        )
        variance = trust.reconciliation_variance(
            int(rec.get("statement_balance") or 0), ctx["book_as_of"],
            outstanding, in_transit,
        )
        if variance == 0:
            continue
        line = (
            f"{_rec_label(rec)}: ne se re-prouve plus à sa fin de période — "
            f"écart {variance} cents"
        )
        if _pre_as_of(rec):
            notes.append(
                f"{line}. Complétée avant la conciliation « as-of » du "
                f"2026-07-29 (commit 945572a), sous l'ancienne définition des "
                f"écritures cochables : à revoir avec l'avocat, pas une "
                f"anomalie établie."
            )
        else:
            problems.append(
                f"{line} (une écriture ou une compensation datée dans la "
                f"période close l'a déplacée — voir les contrôles 6 et 8)"
            )


# ── check 6 ───────────────────────────────────────────────────────────────


def _app_updated_at(tx: dict):
    """The entry's ``updated_at`` as of its last APPLICATION write.

    A rectification by ``scripts/rectify_registers`` stamps a fresh
    ``updated_at`` (house rule: a repair regenerates the etag with the
    stamps), and appends to ``revisions`` the one it replaced — the instant
    check 6 reads as the clear's. The FIRST script revision holds it; later
    ones replaced the script's own stamps."""
    for rev in tx.get("revisions") or []:
        if isinstance(rev, dict) and rev.get("via") == "script" and rev.get("updated_at_before"):
            return rev["updated_at_before"]
    return tx.get("updated_at")


def _clear_bracket(tx: dict, recs_by_id: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """(earliest, latest) instant at which the entry can have been cleared."""
    rid = tx.get("reconciliation_id")
    if rid and rid in recs_by_id:
        completed = _instant(recs_by_id[rid].get("completed_date"))
        return completed, completed
    updated = _instant(_app_updated_at(tx))
    if not tx.get("reversed_by_id"):
        return updated, updated
    return _instant(tx.get("created_at")), updated


def _check_clearing(
    aid: str, txs: list[dict], recs_by_id: dict, by_period: list[dict],
    today: date, problems: list, notes: list,
) -> None:
    for tx in txs:
        if tx.get("status") != "compensée":
            continue
        where = _where(aid, tx)
        cd = _day(tx.get("cleared_date"))
        if cd is None:
            problems.append(f"{where}: compensée sans date de compensation")
            continue
        own = recs_by_id.get(tx.get("reconciliation_id") or "")
        d = _day(tx.get("date"))
        if d is not None and cd < d:
            line = f"{where}: compensée le {cd}, avant sa propre date ({d})"
            if own is not None and _pre_as_of(own):
                notes.append(
                    f"{line} — par la {_rec_label(own)}, dont l'ancien code "
                    f"datait toute écriture cochée de la fin de période : à "
                    f"revoir avec l'avocat."
                )
            else:
                problems.append(line)
        if cd > today:
            problems.append(f"{where}: date de compensation {cd} dans le futur")
        if _born_cleared(tx):
            continue  # cleared at creation — check 8's lock floor covers it

        earliest, latest = _clear_bracket(tx, recs_by_id)
        violated: list[dict] = []
        undecided: list[dict] = []
        for rec in by_period:
            if own is not None and rec.get("id") == own.get("id"):
                continue
            pe = _day(rec.get("period_end"))
            completed_at = _instant(rec.get("completed_date"))
            if pe is None or completed_at is None or cd > pe:
                continue
            if earliest is not None and earliest > completed_at:
                violated.append(rec)
            elif latest is None or latest > completed_at:
                undecided.append(rec)
        if violated:
            names = ", ".join(_rec_label(r) for r in violated)
            line = (
                f"{where}: compensée au {cd} APRÈS la clôture de la période "
                f"qui couvre cette date — {names}. La compensation l'a sortie "
                f"des écritures encore en circulation à la fin de période : "
                f"la conciliation close ne se prouve plus."
            )
            if all(_pre_as_of(r) for r in violated):
                notes.append(f"{line} (ancienne conciliation : à revoir avec l'avocat)")
            else:
                problems.append(line)
        elif undecided:
            names = ", ".join(_rec_label(r) for r in undecided)
            notes.append(
                f"{where}: compensée au {cd}, dans la période de la {names} ; "
                f"l'écriture a été contre-passée depuis, si bien que l'instant "
                f"de la compensation (avant ou après la clôture) ne se lit plus "
                f"au registre — à vérifier au relevé."
            )


# ── check 7 ───────────────────────────────────────────────────────────────


def _check_date_order(aid: str, txs: list[dict], today: date, problems: list) -> None:
    running_max: Optional[date] = None
    seen: set = set()
    for tx in txs:
        seq = tx.get("sequence")
        where = _where(aid, tx)
        if seq in seen:
            problems.append(f"compte {aid}: numéro de séquence {seq} dupliqué")
        seen.add(seq)
        d = _day(tx.get("date"))
        if d is None:
            problems.append(f"{where}: écriture sans date")
            continue
        if running_max is not None and d < running_max:
            problems.append(
                f"{where}: datée du {d}, alors qu'une écriture antérieure dans "
                f"la séquence est datée du {running_max} — le solde reporté et "
                f"le solde aux livres à une date supposent des dates croissantes"
            )
        if d > today:
            problems.append(
                f"{where}: datée du {d}, dans le futur — la garde d'antidatage "
                f"refuse toute écriture ordinaire jusqu'à cette date"
            )
        running_max = d if running_max is None else max(running_max, d)


# ── check 8 ───────────────────────────────────────────────────────────────


def _valid_cash_refund(tx: dict, by_id: dict, refunds_by_receipt: dict) -> bool:
    """The art. 72 exception, as ``models.trust.create_transaction`` checks
    it: a cash refund citing a standing cash RECEIPT of 7 500 $ or more, same
    account (``by_id`` is one account's), dossier and client, the refunds in
    cash never exceeding it."""
    receipt_id = str(tx.get("cash_receipt_id") or "").strip()
    if tx.get("purpose") != trust.CASH_REFUND_PURPOSE or not receipt_id:
        return False
    receipt = by_id.get(receipt_id)
    if receipt is None:
        return False
    if (
        receipt.get("direction") != "recette"
        or receipt.get("method") != trust.CASH_METHOD
        or receipt.get("purpose") == trust.REVERSAL_PURPOSE
        or receipt.get("reversed_by_id")
        or receipt.get("dossier_id") != tx.get("dossier_id")
        or receipt.get("client_id") != tx.get("client_id")
        or int(receipt.get("amount", 0)) < trust.CASH_REFUND_THRESHOLD
    ):
        return False
    return refunds_by_receipt.get(receipt_id, 0) <= int(receipt.get("amount", 0))


def _invoice(iid: str, invoices: dict, where: str, problems: list):
    """The linked invoice (cached): a dict, None when absent, False when the
    read failed (reported as an écart — a check that cannot read must say
    so, never pass)."""
    if iid not in invoices:
        try:
            snap = db.collection(trust.INVOICES_COLLECTION).document(iid).get()
            invoices[iid] = snap.to_dict() if snap.exists else None
        except Exception as exc:
            problems.append(f"{where}: facture liée {iid} illisible ({type(exc).__name__})")
            invoices[iid] = False
    return invoices[iid]


def _check_history_rules(
    aid: str, txs: list[dict], by_period: list[dict], invoices: dict,
    problems: list, notes: list, payees: list[str],
) -> None:
    """Check 8. ``payees`` — the names a fee payment may carry (D23: the
    firm profile's), read ONCE per run by :func:`collect` through
    ``fee_payment.guard_fee_payees`` — strictly, never the fail-open display
    list. Empty skips the payee check: an empty profile names no rule, and
    an unreadable one is an écart :func:`collect` has already reported."""
    by_id = {t.get("id"): t for t in txs}
    refunds_by_receipt: dict = defaultdict(int)
    for t in txs:
        receipt_id = str(t.get("cash_receipt_id") or "").strip()
        if receipt_id and not t.get("reversed_by_id"):
            refunds_by_receipt[receipt_id] += int(t.get("amount", 0))

    for tx in txs:
        where = _where(aid, tx)
        purpose = tx.get("purpose")
        is_correction = purpose == trust.REVERSAL_PURPOSE or bool(tx.get("reverses_id"))

        # The lock floor on create / reverse / transfer (D14).
        created_at = _instant(tx.get("created_at"))
        d = _day(tx.get("date"))
        if created_at is not None and d is not None:
            for rec in by_period:
                pe = _day(rec.get("period_end"))
                completed_at = _instant(rec.get("completed_date"))
                if (pe is not None and completed_at is not None
                        and d <= pe and created_at > completed_at):
                    notes.append(
                        f"{where}: inscrite le {_stamp(created_at)} mais datée "
                        f"du {d}, dans la période déjà close de la "
                        f"{_rec_label(rec)} — le plancher de conciliation la "
                        f"refuse depuis le lot 0b."
                    )
                    break

        # A correction is never reversed again (lot 0b).
        if is_correction and tx.get("reversed_by_id"):
            notes.append(
                f"{where}: correction contre-passée à son tour (par "
                f"{tx.get('reversed_by_id')}) — refusé depuis le lot 0b ; pour "
                f"un paiement d'honoraires, les honoraires sont ressortis du "
                f"fidéicommis sans facture ni recette d'administration."
            )

        # A two-leg transfer reverses whole (lot 0b).
        if trust.is_transfer_pair_leg(tx) and tx.get("reversed_by_id"):
            other = by_id.get(tx.get("related_transaction_id"))
            if other is None or not other.get("reversed_by_id"):
                notes.append(
                    f"{where}: un seul volet du virement inter-dossiers a été "
                    f"contre-passé (autre volet "
                    f"{tx.get('related_transaction_id')}) — le solde d'un seul "
                    f"dossier a bougé, sans mouvement bancaire ni contrepartie."
                )

        # A SINGLE-leg « virement inter-dossiers » (the create form offered
        # the purpose): one dossier's balance moved with no linked counter-
        # leg. Still reversible alone; the purpose is reserved to the
        # two-leg transfer since decision D24 (2026-09-29), so every such leg
        # is history (design review, S1).
        if (purpose == trust.TRANSFER_PURPOSE and not is_correction
                and not tx.get("related_transaction_id")):
            notes.append(
                f"{where}: virement inter-dossiers à un seul volet "
                f"({trust.DIRECTION_LABELS.get(tx.get('direction'), tx.get('direction'))}"
                f" de {int(tx.get('amount', 0))} cents) — aucun volet "
                f"contrepartie lié : vérifier avec l'avocat où l'autre moitié "
                f"a été inscrite."
            )

        # An objet whose NAME contradicts the sens (« Dépôt du client » paid
        # out, « Remise au client » paid in): the connector refused the pair
        # from lot 5b, the MODEL refuses it for every caller since decision
        # D24 (2026-09-29) — the web form included —, so every such entry is
        # history. From the MODEL's map, never a copy (the lot 5a review).
        # Listed whatever the status: the register prints the line either
        # way.
        implied = trust.PURPOSE_DIRECTIONS.get(purpose)
        if implied and tx.get("direction") != implied:
            notes.append(
                f"{where}: objet « {trust.PURPOSE_LABELS.get(purpose, purpose)} » "
                f"inscrit en "
                f"{trust.DIRECTION_LABELS.get(tx.get('direction'), tx.get('direction')).lower()}"
                f" — l'objet dit {'une recette' if implied == 'recette' else 'un déboursé'} ; "
                f"ce couple est refusé à toute nouvelle écriture depuis la "
                f"décision D24 (2026-09-29)."
            )

        # The three withdrawal rules — never on a correction, never on an
        # annulée entry (that cheque never left the account).
        if is_correction or tx.get("status") == "annulée":
            continue
        method = tx.get("method", "")
        if purpose == "virement_honoraires" and method not in trust.FEE_WITHDRAWAL_METHODS:
            notes.append(
                f"{where}: paiement d'honoraires retiré par « "
                f"{trust.METHOD_LABELS.get(method, method)} » — l'art. 58 "
                f"(RLRQ c. B-1, r. 5) n'admet que le chèque à l'ordre de "
                f"l'avocat ou le virement à un compte qui n'est pas en "
                f"fidéicommis."
            )
        if (tx.get("direction") == "déboursé" and method == trust.CASH_METHOD
                and not _valid_cash_refund(tx, by_id, refunds_by_receipt)):
            notes.append(
                f"{where}: retrait en espèces — interdit par l'art. 57 (RLRQ "
                f"c. B-1, r. 5), sauf le remboursement en espèces (art. 72) "
                f"d'une somme de 7 500 $ ou plus reçue en espèces, que cette "
                f"écriture n'établit pas."
            )
        if purpose == "virement_honoraires" and tx.get("invoice_id"):
            iid = tx["invoice_id"]
            invoice = _invoice(iid, invoices, where, problems)
            if invoice is None:
                notes.append(
                    f"{where}: paiement d'honoraires lié à la facture {iid}, "
                    f"introuvable — il n'a plus d'appui vérifiable."
                )
            elif invoice and int(invoice.get("retainer_applied") or 0) > 0:
                notes.append(
                    f"{where}: paiement d'honoraires tiré sur la facture "
                    f"{invoice.get('invoice_number', iid)}, qui impute déjà une "
                    f"provision de {int(invoice.get('retainer_applied') or 0)} "
                    f"cents — la provision est comptée deux fois (refusé depuis "
                    f"le lot 0b)."
                )
            # D21 (2026-09-29): the funds of one client never settle the
            # invoice of another — ids only, never a name.
            invoice_client = str((invoice or {}).get("client_id") or "").strip()
            if invoice and invoice_client and invoice_client != tx.get("client_id"):
                notes.append(
                    f"{where}: paiement d'honoraires tiré des fonds du client "
                    f"{tx.get('client_id')} pour la facture "
                    f"{invoice.get('invoice_number', iid)}, adressée au client "
                    f"{invoice_client} du dossier — refusé depuis la décision "
                    f"D21 (2026-09-29)."
                )
        # D23 (2026-09-29, art. 58): the payee is the lawyer or his firm, as
        # the firm profile names them. Listed without the name (a payee may
        # be a person); an empty profile lists nothing — it names no rule.
        if (purpose == "virement_honoraires" and payees
                and fee_payment.match_fee_payee(tx.get("counterparty"), payees) is None):
            notes.append(
                f"{where}: paiement d'honoraires dont le bénéficiaire n'est ni "
                f"l'avocat ni son cabinet, tels que les nomme le profil du "
                f"cabinet — refusé depuis la décision D23 (2026-09-29, "
                f"art. 58)."
            )


# ── check 9 ───────────────────────────────────────────────────────────────


def _check_client_balances(couple_rows: dict, dossier_book: dict, problems: list) -> None:
    # Shortfalls, from the register itself — the truth, not the maps.
    for (did, cid), rows in sorted(couple_rows.items()):
        book = _sum(rows, "book")
        cleared = _sum(rows, "cleared")
        if book < 0 or cleared < 0:
            problems.append(
                f"dossier {did}/{cid}: découvert en fidéicommis — solde aux "
                f"livres {book} cents, fonds compensés {cleared} cents. Il est "
                f"sorti plus d'argent pour ce client qu'il n'en a été déposé et "
                f"compensé : l'écart est à combler."
            )

    # Stored balances that no register row backs at all.
    try:
        snaps = list(db.collection(trust.DOSSIERS_COLLECTION).stream())
    except Exception as exc:
        problems.append(f"dossiers: lecture impossible ({type(exc).__name__})")
        return
    for snap in snaps:
        doc = snap.to_dict() or {}
        did = snap.id
        book_map = doc.get("trust_balance_by_client") or {}
        cleared_map = doc.get("trust_cleared_by_client") or {}
        for cid in sorted(set(book_map) | set(cleared_map)):
            if (did, cid) in couple_rows:
                continue  # check 3 compared this couple with its rows
            book = int(book_map.get(cid) or 0)
            cleared = int(cleared_map.get(cid) or 0)
            if book or cleared:
                problems.append(
                    f"dossier {did}/{cid}: solde stocké (livres {book} cents, "
                    f"compensé {cleared} cents) sans aucune écriture au registre"
                )
        if did not in dossier_book and int(doc.get("trust_balance") or 0):
            problems.append(
                f"dossier {did}: trust_balance stocké {doc.get('trust_balance')} "
                f"sans aucune écriture au registre"
            )


# ── check 10 ──────────────────────────────────────────────────────────────


def _admin_rows_by_trust_link(problems: list) -> Optional[tuple[dict, list]]:
    """``(linked, unlinked_recettes)`` — every administration entry carrying
    a ``trust_transaction_id``, keyed by it, and every STANDING recette that
    carries none. ONE stream of the (small) admin register, read-only.
    ``None`` when the read fails, reported as an écart: the linkage is then
    unknown, and an unknown linkage must never read as a clean one."""
    try:
        snaps = list(db.collection(admin_ledger.TRANSACTIONS_COLLECTION).stream())
    except Exception as exc:
        problems.append(
            f"registre d'administration illisible ({type(exc).__name__}) — le "
            f"lien entre les paiements d'honoraires et leurs recettes "
            f"d'administration (D-4) n'a pas pu être vérifié"
        )
        return None
    by_link: dict = defaultdict(list)
    unlinked: list = []
    for snap in snaps:
        row = {**(snap.to_dict() or {})}
        row["id"] = row.get("id") or snap.id
        link = row.get("trust_transaction_id")
        if link:
            by_link[link].append(row)
        elif (row.get("direction") == "recette"
              and row.get("kind") in ("encaissement_facture", "recette_autre")
              and _admin_standing(row)):
            unlinked.append(row)
    return by_link, unlinked


def _manual_recettes(fee: dict, unlinked: list) -> list[dict]:
    """The UNLINKED standing recettes that can be the one the lawyer entered
    by hand for *fee* — what the trust page's banner tells him to do when
    the automatic recette fails (« inscrivez-la manuellement »): the link
    itself travels only as a keyword no form can fill, so a manual recette
    is never linked. Same amount, dated on or after the fee payment, and —
    when the fee payment names an Athéna invoice — an encaissement of THAT
    invoice (which also carries the payment onto it). A candidate turns a
    missing link into a NOTE to confirm, never into a silent pass."""
    amount = int(fee.get("amount", 0))
    fee_day = _day(fee.get("date"))
    out = []
    for row in unlinked:
        if int(row.get("amount", 0)) != amount:
            continue
        row_day = _day(row.get("date"))
        if fee_day is not None and (row_day is None or row_day < fee_day):
            continue
        if fee.get("invoice_id"):
            if (row.get("kind") != "encaissement_facture"
                    or row.get("invoice_id") != fee.get("invoice_id")):
                continue
        out.append(row)
    return out


def _admin_standing(row: dict) -> bool:
    """An admin recette whose effect still stands: neither annulée nor
    contre-passée (a compensée recette corrected by contre-passation stays
    « compensée » with ``reversed_by_id`` set — the ``sum_invoice_receipts``
    predicate)."""
    return row.get("status") != "annulée" and not row.get("reversed_by_id")


def _check_fee_payment_linkage(
    fee_payments: list[tuple[str, dict]], by_link: dict, unlinked: list,
    problems: list, notes: list,
) -> None:
    fee_ids = {tx.get("id") for _aid, tx in fee_payments}
    for aid, tx in fee_payments:
        where = _where(aid, tx)
        amount = int(tx.get("amount", 0))
        linked = by_link.get(tx.get("id"), [])
        standing = [r for r in linked if _admin_standing(r)]
        standing_total = sum(int(r.get("amount", 0)) for r in standing)
        fee_standing = tx.get("status") != "annulée" and not tx.get("reversed_by_id")

        # D16 on history: the money reaches the operations account on or
        # after the day it leaves trust, never before.
        fee_day = _day(tx.get("date"))
        for r in standing:
            r_day = _day(r.get("date"))
            if fee_day is not None and r_day is not None and r_day < fee_day:
                notes.append(
                    f"{where}: recette d'administration {r.get('id')} datée du "
                    f"{r_day}, AVANT le paiement d'honoraires qu'elle porte "
                    f"({fee_day}) — l'argent n'a pas pu arriver au compte "
                    f"d'opérations avant de quitter le fidéicommis ; la règle "
                    f"D16 (date d'administration ≥ date du fidéicommis) la "
                    f"refusera."
                )

        if fee_standing:
            if standing_total == amount:
                continue
            if not linked:
                line = (
                    f"{where}: paiement d'honoraires de {amount} cents sorti du "
                    f"fidéicommis sans aucune recette d'administration qui "
                    f"l'adosse (D-4) — le compte d'opérations ne voit pas cet "
                    f"argent"
                )
                manual = _manual_recettes(tx, unlinked)
                created = _instant(tx.get("created_at"))
                if manual:
                    ids = ", ".join(str(r.get("id")) for r in manual)
                    notes.append(
                        f"{where}: paiement d'honoraires de {amount} cents sans "
                        f"recette d'administration LIÉE ; une recette non liée "
                        f"du même montant existe ({ids}), probablement "
                        f"inscrite à la main après l'échec de la recette "
                        f"automatique — à confirmer avec l'avocat (le lien ne "
                        f"se pose pas depuis l'application)."
                    )
                elif created is not None and created >= D4_RULE_AT:
                    problems.append(line)
                else:
                    notes.append(
                        f"{line}. Inscrit avant la règle D-4 du 2026-08-17 "
                        f"(commit e719588), quand le compte d'administration "
                        f"était facultatif ; la reprise des encaissements du "
                        f"même jour devait l'adosser — à revoir avec l'avocat."
                    )
                continue
            problems.append(
                f"{where}: paiement d'honoraires de {amount} cents, mais les "
                f"recettes d'administration encore debout qui le portent "
                f"totalisent {standing_total} cents (D-4) — le compte "
                f"d'opérations (et, pour un encaissement, le paiement inscrit "
                f"sur la facture) en diverge d'autant"
            )
        elif standing_total:
            ids = ", ".join(str(r.get("id")) for r in standing)
            problems.append(
                f"{where}: paiement d'honoraires "
                f"{'annulé' if tx.get('status') == 'annulée' else 'contre-passé'}"
                f", mais {standing_total} cents de recette d'administration liée "
                f"restent debout ({ids}) — l'argent revenu au fidéicommis est "
                f"encore compté au compte d'opérations"
            )

    for link in sorted(set(by_link) - fee_ids):
        standing = [r for r in by_link[link] if _admin_standing(r)]
        if not standing:
            continue
        ids = ", ".join(str(r.get("id")) for r in standing)
        total = sum(int(r.get("amount", 0)) for r in standing)
        problems.append(
            f"recette(s) d'administration {ids} ({total} cents) liée(s) à "
            f"l'écriture du fidéicommis {link}, qui n'est pas un paiement "
            f"d'honoraires du registre — un lien que rien n'adosse"
        )


# ── check 11 ──────────────────────────────────────────────────────────────

_DATE_ONLY_FIELDS = (("date", "date de l'écriture"), ("cleared_date", "date de compensation"))


def _time_of_day(value) -> Optional[datetime]:
    """*value* as tz-aware UTC when it is NOT at midnight UTC, else None."""
    v = _instant(value)
    if v is None or (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
        return None
    return v


def _check_date_times(aid: str, txs: list[dict], notes: list) -> None:
    for tx in txs:
        for field, label in _DATE_ONLY_FIELDS:
            v = _time_of_day(tx.get(field))
            if v is None:
                continue
            notes.append(
                f"{_where(aid, tx)}: {label} enregistrée {v.strftime('%Y-%m-%d %H:%M')} UTC "
                f"({to_mtl(v).strftime('%Y-%m-%d %H:%M')} à Montréal) — une date seule "
                f"s'inscrit à minuit UTC : cette valeur a été écrite hors de "
                f"l'application (la console Firestore saisit en heure locale). "
                f"L'application la lit partout comme le {v.date()} ; à ramener à "
                f"minuit de ce jour."
            )


# ── check 12 ──────────────────────────────────────────────────────────────


def _number_ranges(numbers: list[int]) -> str:
    """« 96–97 », « 5, 9–11 »: consecutive runs joined, ascending."""
    runs: list[list[int]] = []
    for n in sorted(numbers):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def _check_numbering(aid: str, txs: list[dict], problems: list) -> None:
    present = {s for s in (tx.get("sequence") for tx in txs) if isinstance(s, int) and s >= 1}
    try:
        snap = db.collection(trust.COUNTERS_COLLECTION).document(trust._counter_id(aid)).get()
        counter = int((snap.to_dict() or {}).get("seq", 0)) if snap.exists else 0
    except Exception as exc:
        problems.append(
            f"compte {aid}: compteur de séquence illisible ({type(exc).__name__}) — "
            f"la continuité de la numérotation n'a pas été vérifiée"
        )
        return
    highest = max(present) if present else 0
    if counter < highest:
        problems.append(
            f"compte {aid}: compteur de séquence {counter} en deçà du plus haut "
            f"numéro inscrit ({highest}) — la prochaine écriture réutiliserait un "
            f"numéro"
        )
    missing = sorted(set(range(1, max(counter, highest) + 1)) - present)
    if missing:
        problems.append(Reviewable(
            f"compte {aid}: numéro(s) {_number_ranges(missing)} absent(s) du "
            f"registre — l'application ne supprime jamais une écriture au "
            f"fidéicommis : celle-ci l'a été hors de l'application. Le registre "
            f"de l'art. 38 doit être complet, même quand les soldes figés autour "
            f"du trou concordent encore ; à rétablir depuis une sauvegarde, ou à "
            f"expliquer (revue)."
        ))


# ── a stable read ─────────────────────────────────────────────────────────

#: Full passes the run makes before it stops looking for a stable read.
STABLE_READ_ATTEMPTS = 3


def _write_marks() -> dict:
    """The ``update_time`` of every trust and administration ACCOUNT.

    The checks read the registers in several NON-transactional passes — the
    accounts (their denormalized balances) first, then each account's
    entries, then the dossiers, then the administration register. A write
    committed between two of those reads (the lawyer in another tab, Claude
    through the connector) makes a stored figure read BEFORE it disagree
    with the entries read AFTER it: an « écart » no data carries. Every
    write to either register rewrites its account in the same transaction
    (a creation, a clear, a reversal, a transfer, a reconciliation's
    completion — the balance, or at least the etag the reconciliation
    sentinel watches; ``test_chaque_ecriture_des_deux_registres_deplace_le_
    releve`` pins it verb by verb), so two equal marks around a pass prove
    no register write committed during it. A read failure raises: the run
    then stops loudly, never concludes.
    """
    marks: dict = {}
    for collection in (trust.ACCOUNTS_COLLECTION, admin_ledger.ACCOUNTS_COLLECTION):
        for snap in db.collection(collection).stream():
            marks[(collection, snap.id)] = snap.update_time
    return marks


def collect_stable(attempts: int = STABLE_READ_ATTEMPTS) -> tuple[list[str], list[str]]:
    """:func:`collect` on a read no write crossed — re-run up to *attempts*
    times. When every pass was crossed, the last pass's findings are
    returned with an écart ahead of them saying so: an unstable read never
    concludes « clean », and never presents its figures as established."""
    problems: list[str] = []
    notes: list[str] = []
    for attempt in range(1, attempts + 1):
        before = _write_marks()
        problems, notes = collect()
        if _write_marks() == before:
            return problems, notes
        if attempt < attempts:
            print(
                f"Le registre a changé pendant la lecture (une écriture a été "
                f"validée) — nouvelle passe ({attempt + 1}/{attempts})."
            )
    problems.insert(
        0,
        f"le registre a changé pendant chacune des {attempts} passes (des "
        f"écritures sont en cours) : les constats ci-dessous peuvent ne "
        f"refléter que ces écritures — relancer quand personne n'écrit au "
        f"fidéicommis ni à l'administration",
    )
    return problems, notes


# ── the run ───────────────────────────────────────────────────────────────


def collect() -> tuple[list[str], list[str]]:
    """Run every check; return ``(problems, notes)``. Reads only."""
    problems: list[str] = []
    notes: list[str] = []
    _LAST_ENTRIES.clear()
    today = today_mtl()
    accounts = trust.list_accounts()
    print(f"Comptes en fidéicommis : {len(accounts)}")

    # Union of every dossier/client couple's rows, across all accounts, so the
    # denormalized dossier maps (which aggregate all accounts) recompute right.
    couple_rows: dict[tuple, list[dict]] = defaultdict(list)
    dossier_book: dict[str, int] = defaultdict(int)
    invoices: dict = {}
    fee_payments: list[tuple[str, dict]] = []
    # D23 — the accepted payees, read ONCE and STRICTLY: a check that cannot
    # read must say so. Degraded to the deploy-time seed (the fail-open
    # reader every RENDER of the profile uses), it judged the register
    # against names the lawyer may have cleared or replaced — a note built
    # out of a failed read, or a clean run where the check never ran.
    try:
        payees = fee_payment.guard_fee_payees()
    except settings_model.CabinetIllisible:
        payees = []
        problems.append(
            "profil du cabinet (Paramètres) illisible — le contrôle du "
            "bénéficiaire des paiements d'honoraires (D23, art. 58) n'a pas "
            "été fait"
        )

    for account in accounts:
        aid = account["id"]
        txs = _account_transactions(aid)
        print(f"  · {account.get('name', aid)} : {len(txs)} écriture(s)")
        for tx in txs:
            if tx.get("id"):
                _LAST_ENTRIES[tx["id"]] = tx

        # 1. Running account balance (balance_after_account) per row.
        running = trust.recompute_running_balances(txs, "journal")
        by_sequence = {tx.get("sequence"): tx for tx in txs}
        for tx, expected in zip(txs, running):
            stored = int(tx.get("balance_after_account", 0))
            if stored == expected:
                continue
            if _legacy_transfer_leg(tx, stored, expected, by_sequence):
                notes.append(
                    f"{_where(aid, tx)}: premier volet d'un virement "
                    f"inter-dossiers inscrit avant le lot 5 — son solde "
                    f"courant figé ({stored}) est celui de la paire ({expected} "
                    f"+ {int(tx.get('amount', 0))}) ; le registre étant en "
                    f"ajout seul, il le garde (le journal PDF l'imprime tel "
                    f"quel ; les soldes du compte, du dossier et du client sont "
                    f"justes)"
                )
                continue
            problems.append(
                f"compte {aid} seq {tx.get('sequence')}: "
                f"balance_after_account stocké {stored} ≠ recalculé {expected}"
            )

        # 2. Denormalized account book + bank totals.
        book = _sum(txs, "book")
        bank = _sum(txs, "bank")
        if book != int(account.get("book_balance", 0)):
            problems.append(
                f"compte {aid}: book_balance stocké {account.get('book_balance')} ≠ recalculé {book}"
            )
        if bank != int(account.get("bank_balance", 0)):
            problems.append(
                f"compte {aid}: bank_balance stocké {account.get('bank_balance')} ≠ recalculé {bank}"
            )

        for t in txs:
            if t.get("dossier_id") and t.get("client_id"):
                couple_rows[(t["dossier_id"], t["client_id"])].append(t)
            if t.get("purpose") == _FEE_PAYMENT_PURPOSE:
                fee_payments.append((aid, t))

        try:
            recs = trust.list_reconciliations(aid)
        except Exception as exc:
            problems.append(f"compte {aid}: conciliations illisibles ({type(exc).__name__})")
            recs = []
        recs_by_id = {r.get("id"): r for r in recs}
        by_period = sorted(
            (r for r in recs if r.get("status") == "complétée"),
            key=lambda r: _day(r.get("period_end")) or date.min,
        )

        _check_reproof(aid, by_period, problems, notes)                             # 5
        _check_clearing(aid, txs, recs_by_id, by_period, today, problems, notes)    # 6
        _check_date_order(aid, txs, today, problems)                                # 7
        _check_history_rules(aid, txs, by_period, invoices, problems, notes,
                             payees)                                                # 8
        _check_date_times(aid, txs, notes)                                          # 11
        _check_numbering(aid, txs, problems)                                        # 12

    # 3. Per (dossier, client): running balance_after_client + the two maps.
    for (did, cid), rows in couple_rows.items():
        # Creation order across accounts (sequence is per-account only).
        rows.sort(key=lambda t: (t.get("created_at") or 0, t.get("sequence", 0)))
        running = trust.recompute_running_balances(rows, "carte")
        for tx, expected in zip(rows, running):
            stored = int(tx.get("balance_after_client", 0))
            if stored != expected:
                problems.append(
                    f"dossier {did}/{cid} seq {tx.get('sequence')}: "
                    f"balance_after_client stocké {stored} ≠ recalculé {expected}"
                )
        book = _sum(rows, "book")
        cleared = _sum(rows, "cleared")
        dossier_book[did] += book

        dossier = get_dossier(did)
        if not dossier:
            problems.append(f"dossier {did} introuvable pour le couple {cid}")
            continue
        stored_book = int((dossier.get("trust_balance_by_client") or {}).get(cid, 0))
        stored_cleared = int((dossier.get("trust_cleared_by_client") or {}).get(cid, 0))
        if stored_book != book:
            problems.append(
                f"dossier {did}/{cid}: trust_balance_by_client stocké {stored_book} ≠ recalculé {book}"
            )
        if stored_cleared != cleared:
            problems.append(
                f"dossier {did}/{cid}: trust_cleared_by_client stocké {stored_cleared} ≠ recalculé {cleared}"
            )

    # 4. Per-dossier trust_balance == Σ its clients' book.
    for did, total in dossier_book.items():
        dossier = get_dossier(did)
        if dossier and int(dossier.get("trust_balance", 0)) != total:
            problems.append(
                f"dossier {did}: trust_balance stocké {dossier.get('trust_balance')} ≠ recalculé {total}"
            )

    # 9. Per-client balances from the dossier side, and shortfalls.
    _check_client_balances(couple_rows, dossier_book, problems)

    # 10. Every fee payment's administration recette (D-4), both directions.
    admin_rows = _admin_rows_by_trust_link(problems)
    if admin_rows is not None:
        by_link, unlinked = admin_rows
        _check_fee_payment_linkage(fee_payments, by_link, unlinked, problems, notes)
    return problems, notes


def main(argv: Optional[list[str]] = None) -> int:
    """Run every check and print the findings. *argv* is the command line
    after the module name; ``None`` (a call from code) means no option."""
    parser = argparse.ArgumentParser(prog="python -m scripts.verify_trust_integrity")
    parser.add_argument(
        "--revue", metavar="FICHIER",
        help="fichier JSON des constats déjà revus (scripts/findings_review.py)",
    )
    args = parser.parse_args(argv if argv is not None else [])
    problems, notes = collect_stable()
    return report(
        problems, notes, entries=_LAST_ENTRIES, review_path=args.revue,
        clean_line="✅ Aucun écart : le registre et les soldes dénormalisés concordent.",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
