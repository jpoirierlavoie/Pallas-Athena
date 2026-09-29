"""Verify trust-register integrity (Phase K §13). READ-ONLY — writes nothing.

Run before inspecting the register, and before deploying any change to the
trust model's rules:

    python -m scripts.verify_trust_integrity

Two kinds of findings, and the exit code says which were found:

* an ÉCART (error) is a figure or an invariant the register cannot stand
  on today — a denormalized balance that no longer adds up, a completed
  reconciliation that no longer re-proves, a date order the carried-forward
  balance relies on, a client in shortfall. Exit 1.
* a NOTE is history: an entry the model would REFUSE today but accepted
  when it was written, or a reconciliation completed under semantics the
  current re-proof does not share. The register is append-only and history
  is never rewritten, so a note is a decision for the lawyer, never an
  automatic repair. Exit 2 when there are notes and no écart; 0 when clean.

Checks 1-4 (Phase K) recompute the frozen and denormalized balances:

  1. ``balance_after_account`` on every row == the running book balance in
     ``sequence`` order;
  2. the account's ``book_balance`` / ``bank_balance`` == Σ deltas;
  3. per (dossier, client) couple found in the register:
     ``balance_after_client`` and the dossier's two maps == Σ deltas;
  4. per dossier with register rows: ``trust_balance`` == Σ its clients.

Checks 5-9 (lot 5a, 2026-09-28):

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
     (``updated_at`` — a trust entry is written again only when reversed);
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
     is never reversed again; a two-leg transfer reverses whole. Annulée
     entries (a cheque that never left the account) and corrections (which
     copy their original's method and withdraw nothing of their own) are
     outside the three withdrawal rules.
  9. per-client balances vs Σ deltas from the DOSSIER side: every dossier is
     read, so a stored map entry — or a ``trust_balance`` — that no register
     row backs at all is reported; checks 3 and 4 start from the rows and
     cannot see it. And a client whose book or cleared balance is NEGATIVE
     is a trust shortfall the lawyer must cover: reversing a compensée
     recette bypasses the overdraft control by design, so the register can
     reach one.
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from models import db, trust
from models.dossier import get_dossier
from tz import to_mtl
from utils.deadlines import today_mtl

# The instant the as-of reconciliation rework was committed (945572a,
# « Fidéicommis : conciliation rétroactive (as-of) », 2026-07-29 22:37:43
# -04:00). A reconciliation completed before it ran the old completion code.
AS_OF_REWORK_AT = datetime(2026, 7, 30, 2, 37, 43, tzinfo=timezone.utc)

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


def _clear_bracket(tx: dict, recs_by_id: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """(earliest, latest) instant at which the entry can have been cleared."""
    rid = tx.get("reconciliation_id")
    if rid and rid in recs_by_id:
        completed = _instant(recs_by_id[rid].get("completed_date"))
        return completed, completed
    if not tx.get("reversed_by_id"):
        updated = _instant(tx.get("updated_at"))
        return updated, updated
    return _instant(tx.get("created_at")), _instant(tx.get("updated_at"))


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
    problems: list, notes: list,
) -> None:
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


# ── the run ───────────────────────────────────────────────────────────────


def collect() -> tuple[list[str], list[str]]:
    """Run every check; return ``(problems, notes)``. Reads only."""
    problems: list[str] = []
    notes: list[str] = []
    today = today_mtl()
    accounts = trust.list_accounts()
    print(f"Comptes en fidéicommis : {len(accounts)}")

    # Union of every dossier/client couple's rows, across all accounts, so the
    # denormalized dossier maps (which aggregate all accounts) recompute right.
    couple_rows: dict[tuple, list[dict]] = defaultdict(list)
    dossier_book: dict[str, int] = defaultdict(int)
    invoices: dict = {}

    for account in accounts:
        aid = account["id"]
        txs = _account_transactions(aid)
        print(f"  · {account.get('name', aid)} : {len(txs)} écriture(s)")

        # 1. Running account balance (balance_after_account) per row.
        running = trust.recompute_running_balances(txs, "journal")
        for tx, expected in zip(txs, running):
            stored = int(tx.get("balance_after_account", 0))
            if stored != expected:
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
        _check_history_rules(aid, txs, by_period, invoices, problems, notes)        # 8

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
    return problems, notes


def main() -> int:
    problems, notes = collect()
    print()
    if problems:
        print(f"❌ {len(problems)} écart(s) détecté(s) :")
        for p in problems:
            print(f"   - {p}")
    else:
        print("✅ Aucun écart : le registre et les soldes dénormalisés concordent.")
    if notes:
        print()
        print(
            f"Notes à revoir avec l'avocat ({len(notes)}) — l'historique n'est "
            f"jamais réécrit ; chaque ligne est une décision, pas une réparation :"
        )
        for n in notes:
            print(f"   - {n}")
    if problems:
        return 1
    return 2 if notes else 0


if __name__ == "__main__":
    sys.exit(main())
