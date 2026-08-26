"""One-shot: purge the payments recorded OUTSIDE the accounting module.

The invoice's own payment form (Lot P, 2 August 2026) predated the
administration ledger by eleven days and was a second writer of
``amount_paid``, invisible to the register. It was removed on 2026-08-17;
this script clears what it left behind so the lawyer can re-enter each
payment in « Administration », where it will also post to the operations
account.

Scope, deliberately narrow: an invoice with ``amount_paid > 0`` and NO
matching ledger entry (``sum_invoice_receipts`` returning less). A payment
already backed by the register is left alone — re-running is safe.

It does not write Firestore directly. It calls
``models.invoice.record_payment(invoice_id, 0)``, which already does the
whole job and carries 19 tests:

- ``amount_paid → 0`` and ``paid_date → None`` (an unpaid invoice bears no
  payment date);
- **the narrow undo fires: payée → envoyée.** That is not cosmetic, it is
  what makes re-entry possible. ``_ISSUED_INVOICE_STATUSES`` is
  ``("envoyée", "en_retard")``, so an invoice left « payée » would appear in
  no picker and ``admin_ledger.create_transaction`` would refuse it outright
  (``facture_non_émise``). The undo is narrow by design — it only touches a
  « payée » invoice that CARRIES a recorded payment — so the nineteen
  « payée » invoices with no amount are never touched.

The list is printed BEFORE any write and again after, because it is the only
record of what has to be re-entered.

**Relation with ``reprise_encaissements``.** That script books the ledger
entry a fee transfer out of trust should have produced, and un-pays only the
invoices it is about to credit. This one is the wider, permanent hygiene
tool: it clears ANY recorded amount the register does not back — including
the receipts that never came from trust and that no automated source can
reconstruct. The two are idempotent and converge, so either order is safe:
whichever runs second finds the first's work done and does not undo it. Since
2026-08-17 an unbacked ``amount_paid`` is by definition wrong (check 8 of
``verify_admin_integrity`` treats it as an error), so this script keeps a job
after the historical reprise is over.

    python -m scripts.purge_encaissements_factures            # dry-run
    python -m scripts.purge_encaissements_factures --apply    # write
"""

import argparse
import sys
from datetime import datetime, timezone

from models import admin_ledger as al
from models import db
from models.invoice import record_payment
from utils.format_fr import format_cents_fr

#: La séance de purge (2026-08-17). Un paiement inscrit APRÈS cette date
#: n'est pas un vestige de l'ancien formulaire : la comptabilité est seule
#: écrivain depuis ce jour, et un écart récent signifie une écriture qui a
#: mal tourné (une réduction fail-open, une écriture hors application) —
#: à examiner, jamais à purger en bloc (garde de rejeu, audit 2026-08-26).
DATE_PURGE = datetime(2026, 8, 17, tzinfo=timezone.utc)


def _date_str(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else "—"


def _collect() -> tuple[list[dict], list[str]]:
    """The invoices to purge, plus the failures that must NOT be silent.

    Fails CLOSED on a per-invoice cumulative read: clearing a payment whose
    ledger backing could not be read would destroy a legitimate figure.
    """
    cibles: list[dict] = []
    erreurs: list[str] = []
    for snap in db.collection("invoices").stream():
        inv = snap.to_dict() or {}
        paye = int(inv.get("amount_paid", 0) or 0)
        if paye <= 0:
            continue
        try:
            registre = al.sum_invoice_receipts(snap.id)
        except Exception as exc:
            erreurs.append(
                f"{inv.get('invoice_number', snap.id)}: cumul du registre "
                f"illisible ({exc}) — NON purgée"
            )
            continue
        if registre >= paye:
            continue  # déjà adossée à la comptabilité
        # ── Gardes de rejeu (audit 2026-08-26) ──────────────────────────
        # (1) Un paiement daté d'APRÈS la séance de purge n'est pas un
        # vestige : record_payment(id, 0) effacerait une saisie du système
        # actuel dont l'écart s'explique autrement.
        paid = inv.get("paid_date")
        if paid is not None and paid >= DATE_PURGE:
            erreurs.append(
                f"{inv.get('invoice_number', snap.id)} : paiement daté du "
                f"{_date_str(paid)}, postérieur au 2026-08-17 — ce n'est pas "
                f"un vestige de l'ancien formulaire. NON purgée : faites "
                f"examiner l'écart (verify_admin_integrity, contrôle nº 8)."
            )
            continue
        # (2) Un adossement PARTIEL n'est plus le cas de 2026-08-17 (tout ou
        # rien) : remettre à zéro effacerait AUSSI la part que le registre
        # adosse légitimement — le trou changerait de côté, sans se fermer.
        if registre > 0:
            erreurs.append(
                f"{inv.get('invoice_number', snap.id)} : le registre en "
                f"adosse {format_cents_fr(registre)} sur "
                f"{format_cents_fr(paye)} — remettre à zéro effacerait AUSSI "
                f"la part légitime. NON purgée : contre-passez l'écriture "
                f"fautive dans « Administration »."
            )
            continue
        cibles.append({
            "id": snap.id,
            "numero": inv.get("invoice_number", snap.id),
            "dossier": inv.get("dossier_file_number", ""),
            "client": inv.get("client_name", ""),
            "statut": inv.get("status", ""),
            "paye": paye,
            "registre": registre,
            "paid_date": inv.get("paid_date"),
        })
    cibles.sort(key=lambda c: c["numero"])
    return cibles, erreurs


def _print_table(cibles: list[dict]) -> None:
    print(f"{'Facture':<14} {'Dossier':<10} {'Payé le':<11} "
          f"{'Montant':>13}  {'Statut':<10} Client")
    for c in cibles:
        print(f"{c['numero']:<14} {c['dossier']:<10} "
              f"{_date_str(c['paid_date']):<11} "
              f"{format_cents_fr(c['paye']):>13}  {c['statut']:<10} {c['client']}")
    total = sum(c["paye"] for c in cibles)
    print(f"{'':<14} {'':<10} {'TOTAL':<11} {format_cents_fr(total):>13}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Purge des encaissements "
                                                 "saisis hors comptabilité.")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit la purge (défaut : simulation).")
    args = parser.parse_args(argv)

    cibles, erreurs = _collect()
    for e in erreurs:
        print(f"⚠️  {e}")

    if not cibles:
        print("Aucune facture à purger : tout montant encaissé est adossé "
              "à une écriture comptable.")
        return 1 if erreurs else 0

    print(f"\n{len(cibles)} facture(s) portent un paiement que le registre "
          f"d'administration ne connaît pas :\n")
    _print_table(cibles)
    print("\nCes paiements seront effacés (montant, date) et le statut "
          "« payée » reviendra à « envoyée »,")
    print("pour que chaque facture se ressaisisse normalement en "
          "« Administration ».")

    if not args.apply:
        print("\n(simulation — relancez avec --apply pour écrire)")
        return 0

    print()
    echecs = 0
    for c in cibles:
        inv, errors = record_payment(c["id"], 0)
        if errors or inv is None:
            echecs += 1
            print(f"❌ {c['numero']} : {'; '.join(errors) or 'échec'}")
            continue
        print(f"✔  {c['numero']} : {format_cents_fr(c['paye'])} effacé, "
              f"statut « {inv.get('status', '')} »")

    print("\nÀ RESSAISIR EN COMPTABILITÉ (conservez cette liste) :\n")
    _print_table(cibles)
    print("\nUn paiement venu du fidéicommis se ressaisit DEPUIS le "
          "fidéicommis (contre-passer le virement,")
    print("puis le refaire en nommant le compte d'administration) ; les "
          "autres directement au registre.")
    return 1 if (echecs or erreurs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
