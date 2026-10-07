"""Verify administration-register integrity. READ-ONLY — writes nothing.

The sibling of verify_trust_integrity.py, minus the frozen-balance rows this
module deliberately does not have (balances are computed at read). Checks:

  1. Σ admin_delta per account == the denormalized ``ledger_balance``;
  2. ventilation exactness: net + TPS + TVQ == amount on every déboursé;
  3. reversal pairing: symmetric links, equal amounts, opposite directions,
     coherent status algebra;
  4. card-payment legs: symmetric links, one déboursé on an « opérations »
     account + one recette on a « carte_crédit » account, equal amounts,
     both legs ``paiement_carte`` and on the same UTC DAY (one payment, one
     date — a time of day stays check 10's note);
  5. sequences unique per account, counter >= max;
  6. every COMPLETED reconciliation still re-proves at its period_end (the
     lock's ongoing warranty — book_as_of + outstanding − in_transit vs the
     statement, in ledger sign);
  7. compensée ⇒ cleared_date present;
  8. ``sum_invoice_receipts`` per invoice == the invoice's recorded
     ``amount_paid``. This became a PROBLEM on 2026-08-17: the accounting
     module is the only writer of a payment since the invoice's own form was
     removed, so a mismatch is no longer explainable and means the operations
     account is understated by the difference. It was degraded to a mere note
     while that form existed as a legitimate second writer — the reason five
     invoices carried 5 397,36 $ that no ledger row ever recorded, and this
     very check could not see it.

     The sum comes from the MODEL, not from a local reimplementation: the
     copy that lived here omitted ``reversed_by_id`` and would report a false
     gap after any contre-passation of a compensée encaissement — the one
     correction flow the two-step lifecycle exists for. For the same reason
     an invoice enters this check as CITED only by a STANDING encaissement:
     reversed rows citing an invoice voided and deleted since (the normal
     reverse → void → delete path) are not « orphans ».
  9. the direction of every SIMPLE kind is the one its kind implies
     (``models/admin_ledger._KIND_DIRECTION``, the one source of that map).
     Since lot 0b the model derives the
     direction and refuses a contradicting one, and a web edit — which
     always names the kind — RE-DERIVES it: an « Autre recette » stored as a
     déboursé by an older direct model call would have its sign flipped, and
     the ledger balance moved by twice its amount, the first time anyone
     edits it. Run this BEFORE deploying lot 0b; each row it names is a
     decision for the lawyer, never an automatic repair.
 10. NOTE (2026-10-06) — a date-only field (``date``, ``cleared_date``)
     carrying a time of day. The model stores both at midnight UTC on every
     path; any other value was written outside it (the Firestore console
     edits in LOCAL time). Every reader takes its UTC day, so no figure
     moves — a time of day only reorders the entry within its day — but it
     is the trace of an edit no trail records.
 11. (2026-10-07) every kind is one the register knows — an unknown kind is
     named, never skipped — and the four natures outside the results
     (``admin_ledger.NON_RESULT_KINDS``: the lawyer's prélèvement and apport,
     the internal transfers) carry what their model writes: no category,
     the canonical split (``canonical_ventilation`` — no TPS, no TVQ), no
     invoice, no trust link, and no dossier on a prélèvement or an apport.
     A correction reversing one of them carries no category and no split at
     all (net included): either would leak into the journal's totals. What
     a correction reverses is its ``reverses_kind`` stamp, else its
     original's kind (a reversal by an older release carries no stamp); a
     stamp that contradicts the original is named too.

Exit 1 when an écart is found, 2 when only notes are, 0 when clean. Every
finding prints with a key; ``--revue FICHIER`` names the findings the
lawyer has already reviewed (:mod:`scripts.findings_review`).

Run:  python -m scripts.verify_admin_integrity [--revue FICHIER]
"""

import argparse
import sys
from datetime import datetime
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from models import admin_ledger as al
from models import db
from scripts.findings_review import report
from tz import to_mtl

# Every entry the last :func:`collect` read, by id — what a finding's review
# key binds to (:func:`scripts.findings_review.finding_key`).
_LAST_ENTRIES: dict[str, dict] = {}

_DATE_ONLY_FIELDS = (("date", "date de l'écriture"), ("cleared_date", "date de compensation"))


def _time_of_day(value) -> Optional[datetime]:
    """*value* as tz-aware UTC when it is NOT at midnight UTC, else None."""
    v = al._as_utc(value)
    if not isinstance(v, datetime) or (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
        return None
    return v


def _account_transactions(account_id: str) -> list[dict]:
    return [
        d.to_dict()
        for d in db.collection(al.TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence")
        .stream()
    ]


def collect() -> tuple[list[str], list[str]]:
    """Run every check; return ``(problems, notes)``. Reads only."""
    problems: list[str] = []
    notes: list[str] = []
    _LAST_ENTRIES.clear()
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
            if t.get("id"):
                _LAST_ENTRIES[t["id"]] = t
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

    invoice_ids: set[str] = set()
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
        # 9. A simple kind carries the direction its kind implies. The
        # structural kinds (paiement_carte, correction) take theirs from
        # their own write path and are covered by checks 3 and 4.
        implied = al._KIND_DIRECTION.get(t.get("kind", ""))
        if implied and t.get("direction") != implied:
            problems.append(
                f"écriture {tid}: type {t.get('kind')} inscrit en "
                f"{t.get('direction') or '(sans sens)'} — le type implique "
                f"« {implied} » (encore modifiable, sa prochaine modification "
                f"web re-dériverait le sens et déplacerait le solde)"
            )
        # 7. compensée ⇒ cleared_date.
        if t.get("status") == "compensée" and not t.get("cleared_date"):
            problems.append(f"écriture {tid}: compensée sans cleared_date")
        # 10. A date-only field carrying a time of day — written outside the
        # model (NOTE: every reader takes its UTC day, no figure moves).
        for field, label in _DATE_ONLY_FIELDS:
            v = _time_of_day(t.get(field))
            if v is not None:
                notes.append(
                    f"compte {t.get('account_id')} seq {t.get('sequence')} "
                    f"(écriture {tid}): {label} enregistrée "
                    f"{v.strftime('%Y-%m-%d %H:%M')} UTC "
                    f"({to_mtl(v).strftime('%Y-%m-%d %H:%M')} à Montréal) — une "
                    f"date seule s'inscrit à minuit UTC : cette valeur a été "
                    f"écrite hors de l'application (la console Firestore saisit en "
                    f"heure locale). L'application la lit partout comme le "
                    f"{v.date()} ; à ramener à minuit de ce jour."
                )
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
                if other.get("kind") != "paiement_carte":
                    problems.append(
                        f"écriture {tid}: l'autre jambe ({other.get('id')}) n'est "
                        f"pas un paiement de carte"
                    )
                # The UTC DAY, as every reader takes it: a time of day on one
                # leg is check 10's note, not a second date.
                day, other_day = al._as_utc(t.get("date")), al._as_utc(other.get("date"))
                if (isinstance(day, datetime) and isinstance(other_day, datetime)
                        and day.date() != other_day.date()):
                    problems.append(
                        f"écriture {tid}: jambes de carte à des jours différents "
                        f"({day.date()} et {other_day.date()}) — un paiement, "
                        f"une date"
                    )
        # 11. A kind the register knows; the four natures outside the
        # results carry what their model writes (see the docstring).
        kind = t.get("kind") or ""
        if kind not in al.VALID_KINDS:
            problems.append(f"écriture {tid}: type « {kind or '(vide)'} » inconnu du registre")
        elif kind in al.NON_RESULT_KINDS:
            label = al.KIND_LABELS[kind]
            if t.get("category"):
                problems.append(f"écriture {tid}: {label} portant une catégorie ({t.get('category')})")
            split = {k: int(t.get(k) or 0) for k in ("net_amount", "gst_amount", "qst_amount")}
            canonical = al.canonical_ventilation(
                kind, t.get("direction", ""), int(t.get("amount", 0)))
            if split != canonical:
                problems.append(
                    f"écriture {tid}: {label} ventilé {split['net_amount']}+"
                    f"{split['gst_amount']}+{split['qst_amount']} — cette nature ne "
                    f"porte ni TPS ni TVQ"
                )
            if t.get("invoice_id"):
                problems.append(f"écriture {tid}: {label} liée à une facture")
            if t.get("trust_transaction_id"):
                problems.append(f"écriture {tid}: {label} liée au fidéicommis")
            if kind in al.OWNER_KINDS and t.get("dossier_id"):
                problems.append(f"écriture {tid}: {label} rattaché à un dossier")
        elif kind == al.REVERSAL_KIND:
            # What the correction reverses: its stamp, else its original's
            # kind (a reversal by an older release carries no stamp).
            original = by_id.get(t.get("reverses_id") or "")
            stamped = t.get("reverses_kind")
            if stamped and original is not None and original.get("kind") != stamped:
                problems.append(
                    f"écriture {tid}: contre-passation notée « {stamped} », "
                    f"l'écriture qu'elle contre-passe est « {original.get('kind')} »"
                )
            reversed_kind = stamped or (original or {}).get("kind")
            if reversed_kind in al.NON_RESULT_KINDS and (
                    t.get("category")
                    or any(int(t.get(k) or 0) for k in ("net_amount", "gst_amount", "qst_amount"))):
                problems.append(
                    f"écriture {tid}: contre-passation d'un « "
                    f"{al.KIND_LABELS[reversed_kind]} » portant une catégorie "
                    f"ou une ventilation — elle fausserait les totaux du journal"
                )
        # 8. Lot P cumulative — on ne retient ici que les factures CITÉES par
        # le registre ; les autres sont balayées plus bas, car une facture
        # payée SANS écriture est exactement le trou que ce contrôle a manqué.
        # Citée par un encaissement DEBOUT seulement — le prédicat de
        # ``sum_invoice_receipts`` (ni annulé, ni contre-passé). Le chemin
        # normal de l'application est « contre-passer l'encaissement, annuler
        # la facture, la supprimer » (l'annulation refuse tant qu'un
        # encaissement tient) : les lignes contre-passées citent alors une
        # facture disparue, et les compter en faisait un faux « encaissements
        # orphelins » — un écart que les modèles n'ont jamais produit, sur le
        # contrôle même que le lot 5a exige propre avant son déploiement.
        if (t.get("kind") == "encaissement_facture" and t.get("invoice_id")
                and t.get("status") != "annulée" and not t.get("reversed_by_id")):
            invoice_ids.add(t["invoice_id"])

    # Toute facture portant un montant encaissé entre dans le contrôle, qu'une
    # écriture la cite ou non : la version antérieure n'examinait que les
    # factures déjà présentes au registre, si bien qu'un paiement saisi hors
    # comptabilité — le cas même qu'elle était censée révéler — restait
    # invisible.
    try:
        for snap in db.collection("invoices").stream():
            inv = snap.to_dict() or {}
            if int(inv.get("amount_paid", 0)):
                invoice_ids.add(snap.id)
    except Exception as exc:
        problems.append(f"factures: lecture impossible ({exc})")

    for invoice_id in sorted(invoice_ids):
        try:
            snap = db.collection("invoices").document(invoice_id).get()
            inv = snap.to_dict() if snap.exists else None
        except Exception:
            inv = None
        if inv is None:
            problems.append(f"facture {invoice_id}: introuvable (encaissements orphelins)")
            continue
        try:
            total = al.sum_invoice_receipts(invoice_id)
        except Exception as exc:
            problems.append(f"facture {invoice_id}: cumul illisible ({exc})")
            continue
        recorded = int(inv.get("amount_paid", 0))
        if recorded != total:
            problems.append(
                f"facture {inv.get('invoice_number', invoice_id)}: encaissements "
                f"administration {total} ≠ montant encaissé {recorded} — la "
                f"comptabilité est le seul écrivain d'un paiement depuis le "
                f"2026-08-17; le compte d'opérations est faux de l'écart."
            )

    return problems, notes


def main(argv: Optional[list[str]] = None) -> int:
    """Run every check and print the findings. *argv* is the command line
    after the module name; ``None`` (a call from code) means no option."""
    parser = argparse.ArgumentParser(prog="python -m scripts.verify_admin_integrity")
    parser.add_argument(
        "--revue", metavar="FICHIER",
        help="fichier JSON des constats déjà revus (scripts/findings_review.py)",
    )
    args = parser.parse_args(argv if argv is not None else [])
    problems, notes = collect()
    return report(
        problems, notes, entries=_LAST_ENTRIES, review_path=args.revue,
        clean_line="✅ Aucun écart : le registre d'administration concorde.",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
