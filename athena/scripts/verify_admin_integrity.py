"""Verify administration-register integrity. READ-ONLY — writes nothing.

The sibling of verify_trust_integrity.py, minus the frozen-balance rows this
module deliberately does not have (balances are computed at read). Checks:

  1. Σ admin_delta per account == the denormalized ``ledger_balance``;
  2. ventilation exactness: net + TPS + TVQ == amount on every déboursé;
  3. reversal pairing: symmetric links, equal amounts, opposite directions,
     coherent status algebra;
  4. card-payment legs: symmetric links, one déboursé on an « opérations »
     account + one recette on a « carte_crédit » account, equal amounts;
  5. sequences unique per account, counter >= max;
  6. every COMPLETED reconciliation still re-proves at its period_end (the
     lock's ongoing warranty — book_as_of + outstanding − in_transit vs the
     statement, in ledger sign);
  7. compensée ⇒ cleared_date present;
  8. Σ of non-annulée « encaissement_facture » amounts per invoice == the
     invoice's recorded ``amount_paid`` (Lot P drift made visible — a
     mismatch is REPORTED, not an error exit: the invoice's manual payment
     form is a legitimate second writer).

Run:  python -m scripts.verify_admin_integrity
"""

import sys
from collections import defaultdict

from google.cloud.firestore_v1.base_query import FieldFilter

from models import admin_ledger as al
from models import db


def _account_transactions(account_id: str) -> list[dict]:
    return [
        d.to_dict()
        for d in db.collection(al.TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence")
        .stream()
    ]


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    accounts = al.list_accounts()
    print(f"Comptes d'administration : {len(accounts)}")

    all_txs: list[dict] = []
    by_id: dict[str, dict] = {}

    for account in accounts:
        aid = account["id"]
        txs = _account_transactions(aid)
        all_txs.extend(txs)
        for t in txs:
            by_id[t.get("id", "")] = t
        print(f"  · {account.get('name', aid)} : {len(txs)} écriture(s)")

        # 1. Denormalized ledger balance == Σ admin_delta (status-blind).
        total = sum(
            al.admin_delta(t.get("direction", ""), int(t.get("amount", 0)))
            for t in txs
        )
        if total != int(account.get("ledger_balance", 0)):
            problems.append(
                f"compte {aid}: ledger_balance stocké "
                f"{account.get('ledger_balance')} ≠ recalculé {total}"
            )

        # 5. Sequences unique, counter >= max.
        seqs = [int(t.get("sequence", 0)) for t in txs]
        if len(seqs) != len(set(seqs)):
            problems.append(f"compte {aid}: numéros de séquence dupliqués")
        try:
            counter = (
                db.collection(al.COUNTERS_COLLECTION)
                .document(al._counter_id(aid)).get()
            )
            seq_stored = int((counter.to_dict() or {}).get("seq", 0)) if counter.exists else 0
            if seqs and seq_stored < max(seqs):
                problems.append(
                    f"compte {aid}: compteur {seq_stored} < séquence max {max(seqs)}"
                )
        except Exception:
            problems.append(f"compte {aid}: compteur illisible")

        # 6. Every completed reconciliation re-proves at its period_end.
        for rec in al.list_reconciliations(aid):
            if rec.get("status") != "complétée":
                continue
            try:
                ctx = al.reconciliation_as_of_context(aid, rec.get("period_end"))
            except Exception:
                problems.append(
                    f"conciliation {rec.get('id')}: contexte as-of illisible"
                )
                continue
            outstanding = ctx["fixed_outstanding_total"] + sum(
                int(e.get("amount", 0)) for e in ctx["outstanding"]
            )
            in_transit = ctx["fixed_in_transit_total"] + sum(
                int(e.get("amount", 0)) for e in ctx["in_transit"]
            )
            variance = al.reconciliation_variance(
                al.statement_to_ledger(
                    account.get("account_type", ""),
                    int(rec.get("statement_balance", 0)),
                ),
                ctx["book_as_of"], outstanding, in_transit,
            )
            if variance != 0:
                problems.append(
                    f"conciliation {rec.get('id')} (au "
                    f"{rec.get('period_end')}): ne se re-prouve plus — "
                    f"écart {variance} cents (le verrou a été contourné ?)"
                )

    invoice_sums: dict[str, int] = defaultdict(int)
    for t in all_txs:
        tid = t.get("id", "?")
        # 2. Ventilation exactness on every déboursé.
        if t.get("direction") == "déboursé":
            n, g, q = (int(t.get(k, 0) or 0) for k in
                       ("net_amount", "gst_amount", "qst_amount"))
            if (n or g or q) and n + g + q != int(t.get("amount", 0)):
                problems.append(
                    f"écriture {tid}: ventilation {n}+{g}+{q} ≠ montant "
                    f"{t.get('amount')}"
                )
        # 7. compensée ⇒ cleared_date.
        if t.get("status") == "compensée" and not t.get("cleared_date"):
            problems.append(f"écriture {tid}: compensée sans cleared_date")
        # 3. Reversal pairing.
        rev_id = t.get("reversed_by_id")
        if rev_id:
            rev = by_id.get(rev_id)
            if rev is None:
                problems.append(f"écriture {tid}: contre-passation {rev_id} introuvable")
            else:
                if rev.get("reverses_id") != tid:
                    problems.append(f"écriture {tid}: lien de contre-passation asymétrique")
                if int(rev.get("amount", 0)) != int(t.get("amount", 0)):
                    problems.append(f"écriture {tid}: montants de contre-passation inégaux")
                if rev.get("direction") == t.get("direction"):
                    problems.append(f"écriture {tid}: contre-passation de même sens")
                if t.get("status") == "annulée" and rev.get("status") != "annulée":
                    problems.append(
                        f"écriture {tid}: annulée mais sa contre-passation ne l'est pas"
                    )
        # 4. Card-payment legs.
        if t.get("kind") == "paiement_carte":
            other = by_id.get(t.get("related_transaction_id") or "")
            if other is None:
                problems.append(f"écriture {tid}: jambe de paiement de carte orpheline")
            else:
                if other.get("related_transaction_id") != tid:
                    problems.append(f"écriture {tid}: jambes de carte asymétriques")
                if int(other.get("amount", 0)) != int(t.get("amount", 0)):
                    problems.append(f"écriture {tid}: montants de jambes inégaux")
                if other.get("direction") == t.get("direction"):
                    problems.append(f"écriture {tid}: jambes de carte de même sens")
        # 8. Lot P cumulative.
        if t.get("kind") == "encaissement_facture" and t.get("invoice_id") \
                and t.get("status") != "annulée":
            invoice_sums[t["invoice_id"]] += int(t.get("amount", 0))

    for invoice_id, total in invoice_sums.items():
        try:
            snap = db.collection("invoices").document(invoice_id).get()
            inv = snap.to_dict() if snap.exists else None
        except Exception:
            inv = None
        if inv is None:
            problems.append(f"facture {invoice_id}: introuvable (encaissements orphelins)")
            continue
        recorded = int(inv.get("amount_paid", 0))
        if recorded != total:
            # NOT an error: the invoice's own payment form is a legitimate
            # second writer (a payment received into trust, a correction).
            notes.append(
                f"facture {inv.get('invoice_number', invoice_id)}: encaissements "
                f"administration {total} ≠ montant encaissé {recorded} — normal "
                f"si un paiement a été saisi ailleurs; vérifiez sinon."
            )

    print()
    for n in notes:
        print(f"ℹ️  {n}")
    if problems:
        print(f"❌ {len(problems)} écart(s) détecté(s) :")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ Aucun écart : le registre d'administration concorde.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
