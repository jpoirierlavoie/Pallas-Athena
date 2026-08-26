"""Réparation PERMANENTE : recalculer un solde de grand livre qui a dérivé.

(Longtemps intitulé « One-shot » — l'audit du 2026-08-26 a corrigé le mot :
la valeur écrite est RECALCULÉE des écritures vivantes à chaque exécution,
il n'y a aucune prémisse figée qui puisse se périmer. Le script est
convergent par construction et se relance sans danger tant qu'aucun second
écrivain légitime de `ledger_balance` n'existe — il n'en existe aucun.)

`admin_accounts.ledger_balance` est le SEUL chiffre dénormalisé du registre
d'administration ; tout le reste se calcule à la lecture. Quand il diverge de
la somme des écritures, c'est lui qui a tort — le journal, lui, recalcule.

Constaté le 2026-08-18 : le compte d'opérations affichait 1 154,79 $ sur sa
carte pendant que la dernière ligne de son journal montrait 109,68 $, le vrai
solde bancaire. Écart de 1 045,11 $, entièrement expliqué par trois documents
modifiés ou supprimés HORS de l'application (deux encaissements verrouillés
disparus, une dépense dont le montant avait été relevé sans ajustement).

    python -m scripts.reparer_soldes_administration            # simulation
    python -m scripts.reparer_soldes_administration --apply    # écrit

DEUX RÉPARATIONS, chacune rétablissant un invariant que le modèle lui-même
aurait écrit — jamais une valeur devinée :

1. `ledger_balance` ← Σ `admin_delta` sur toutes les écritures du compte.
   C'est mot pour mot la définition que le contrôle nº 1 de
   `verify_admin_integrity` applique déjà ; le script ne fait que l'écrire.

2. Ventilation d'un déboursé dont la TPS ET la TVQ valent zéro et dont le net
   diffère du montant : `net_amount` ← `amount`, le défaut exact que
   `validate_ventilation` pose sur une ventilation vierge. Une ventilation
   dont une taxe est non nulle est LAISSÉE INTACTE — le net y est un choix
   comptable, pas un défaut, et le rétablir serait deviner.

ÉCRITURE HORS MODÈLE, ASSUMÉE : aucune fonction ne corrige `ledger_balance`,
et une écriture compensée est verrouillée. Ce qui rend le script acceptable
n'est pas la prudence de l'écriture, mais le fait que la valeur écrite soit
CALCULÉE. Il est idempotent : une seconde exécution ne trouve plus rien.
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone

from models import admin_ledger as al
from models import db
from utils.format_fr import format_cents_fr as f


def _collect() -> tuple[list[dict], list[dict], list[str]]:
    """(soldes à corriger, ventilations à corriger, refus).

    Les refus ne sont JAMAIS tus : une anomalie que le script écarte est une
    anomalie que personne n'examinera si elle n'est pas nommée.
    """
    refus: list[str] = []
    comptes: list[dict] = []
    ventilations: list[dict] = []

    par_compte: dict[str, list[dict]] = {}
    for snap in db.collection(al.TRANSACTIONS_COLLECTION).stream():
        t = snap.to_dict() or {}
        par_compte.setdefault(t.get("account_id"), []).append(t)

    for snap in db.collection(al.ACCOUNTS_COLLECTION).stream():
        compte = snap.to_dict() or {}
        rows = par_compte.get(snap.id, [])
        # admin_delta est AVEUGLE AU STATUT : une écriture annulée compte, et
        # elle nette avec sa contre-passation. Le solde stocké suit la même
        # règle, donc la comparaison est exacte.
        recalcule = sum(
            al.admin_delta(t.get("direction", ""), int(t.get("amount", 0) or 0))
            for t in rows
        )
        stocke = int(compte.get("ledger_balance", 0) or 0)
        if stocke != recalcule:
            comptes.append({
                "id": snap.id, "nom": compte.get("name", snap.id),
                "stocke": stocke, "recalcule": recalcule, "n": len(rows),
            })

    for rows in par_compte.values():
        for t in rows:
            if t.get("direction") != "déboursé":
                continue
            net, tps, tvq = (int(t.get(k, 0) or 0)
                             for k in ("net_amount", "gst_amount", "qst_amount"))
            montant = int(t.get("amount", 0) or 0)
            if not (net or tps or tvq) or net + tps + tvq == montant:
                continue
            if tps or tvq:
                refus.append(
                    "écriture seq {} ({}) : ventilation {}+{}+{} incohérente, "
                    "mais une taxe est non nulle — le net y est un choix "
                    "comptable. NON corrigée.".format(
                        t.get("sequence"), f(montant), f(net), f(tps), f(tvq))
                )
                continue
            ventilations.append({
                "id": t.get("id"), "seq": t.get("sequence"),
                "date": t.get("date"), "montant": montant, "net": net,
            })

    comptes.sort(key=lambda c: c["nom"])
    ventilations.sort(key=lambda v: v["seq"] or 0)
    return comptes, ventilations, refus


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Recalcule un solde de grand livre qui a dérivé.")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit la réparation (défaut : simulation).")
    args = parser.parse_args(argv)

    comptes, ventilations, refus = _collect()
    for r in refus:
        print("⚠️  " + r)

    if not comptes and not ventilations:
        print("Rien à réparer : chaque solde égale la somme de ses écritures.")
        return 1 if refus else 0

    if comptes:
        print("\n{} solde(s) à recalculer :\n".format(len(comptes)))
        print("{:<28} {:>5} {:>14} {:>14} {:>14}".format(
            "Compte", "Écr.", "Stocké", "Recalculé", "Écart"))
        for c in comptes:
            print("{:<28} {:>5} {:>14} {:>14} {:>14}".format(
                c["nom"], c["n"], f(c["stocke"]), f(c["recalcule"]),
                f(c["stocke"] - c["recalcule"])))

    if ventilations:
        print("\n{} ventilation(s) à rétablir :\n".format(len(ventilations)))
        for v in ventilations:
            d = (v["date"].strftime("%Y-%m-%d")
                 if hasattr(v["date"], "strftime") else "—")
            print("  seq {:>4}  {}  montant {:>12}  net {} → {}".format(
                v["seq"], d, f(v["montant"]), f(v["net"]), f(v["montant"])))

    if not args.apply:
        print("\n(simulation — relancez avec --apply pour écrire)")
        return 0

    print()
    now = datetime.now(timezone.utc)
    echecs = 0
    # Les ventilations D'ABORD : elles ne bougent aucun solde (admin_delta ne
    # lit que le sens et le montant), mais les faire après reviendrait à
    # recalculer un solde puis à toucher encore aux écritures.
    for v in ventilations:
        try:
            db.collection(al.TRANSACTIONS_COLLECTION).document(v["id"]).update({
                "net_amount": v["montant"], "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
            print("✔  seq {} : net {} → {}".format(
                v["seq"], f(v["net"]), f(v["montant"])))
        except Exception as exc:
            echecs += 1
            print("❌ seq {} : {}".format(v["seq"], exc))

    for c in comptes:
        try:
            db.collection(al.ACCOUNTS_COLLECTION).document(c["id"]).update({
                "ledger_balance": c["recalcule"], "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
            print("✔  {} : {} → {}".format(
                c["nom"], f(c["stocke"]), f(c["recalcule"])))
        except Exception as exc:
            echecs += 1
            print("❌ {} : {}".format(c["nom"], exc))

    # Re-preuve : on relit, et l'invariant doit tenir.
    print()
    restants, rest_vent, _ = _collect()
    if restants or rest_vent:
        print("❌ {} solde(s) et {} ventilation(s) divergent encore après "
              "écriture.".format(len(restants), len(rest_vent)))
        return 1
    if echecs:
        print("\n❌ {} échec(s).".format(echecs))
        return 1
    print("✅ Réparation vérifiée : chaque solde égale la somme de ses écritures.")
    print("\nLancez python -m scripts.verify_admin_integrity — le contrôle nº 1 "
          "doit se taire.")
    return 1 if refus else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
