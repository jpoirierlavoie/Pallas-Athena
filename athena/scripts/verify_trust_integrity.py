"""Verify trust-register integrity (Phase K §13). READ-ONLY — writes nothing.

Recomputes both running balances from ``sequence`` order and compares them
against the frozen ``balance_after_account`` / ``balance_after_client`` on
every row; recomputes the denormalized account and dossier balances and
compares. Prints a report and exits non-zero if any discrepancy is found. Run
before inspecting the register:

    python -m scripts.verify_trust_integrity
"""

import sys
from collections import defaultdict

from google.cloud.firestore_v1.base_query import FieldFilter

from models import db, trust
from models.dossier import get_dossier


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


def main() -> int:
    problems: list[str] = []
    accounts = trust.list_accounts()
    print(f"Comptes en fidéicommis : {len(accounts)}")

    # Union of every dossier/client couple's rows, across all accounts, so the
    # denormalized dossier maps (which aggregate all accounts) recompute right.
    couple_rows: dict[tuple, list[dict]] = defaultdict(list)
    dossier_book: dict[str, int] = defaultdict(int)

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

    print()
    if problems:
        print(f"❌ {len(problems)} écart(s) détecté(s) :")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✅ Aucun écart : le registre et les soldes dénormalisés concordent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
