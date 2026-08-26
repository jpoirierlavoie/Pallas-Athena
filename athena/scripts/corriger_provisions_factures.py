"""One-shot: retirer d'une facture la provision que son virement acquitte.

L'ancien système imputait la provision du client **à l'émission** de la
facture : le « dû » qu'elle affiche est ce qui restait payable APRÈS cet
argent, et la somme figure en `retainer_applied`. Huit factures reprises
portent ainsi une provision égale, au cent près, au virement d'honoraires
qui l'a physiquement sortie du fidéicommis.

Décision du juriste (2026-08-17) : les corriger, plutôt que de porter ces
virements en « autre recette ». Retirer la provision et inscrire un
encaissement exprime la MÊME réalité dans l'idiome courant d'Athéna — la
chaîne devient « provision au fidéicommis → facture émise pour son total →
virement → encaissement » — et le solde final est identique au cent près
(256501-01 : 1 149,75 − 750,00 = 399,75 $, son solde d'aujourd'hui).

    python -m scripts.corriger_provisions_factures            # simulation
    python -m scripts.corriger_provisions_factures --apply    # écrit

ÉCRITURE HORS MODÈLE, ASSUMÉE. Il n'existe aucun `update_invoice` :
`retainer_applied` et `amount_due` ne s'écrivent qu'à la création
(`models/invoice.py`), et rien dans l'application ne peut les corriger. Le
script écrit donc les deux champs directement — mais il ne bouge que ceux-là,
sur un invariant vérifiable au cent près, et il refuse tout ce qui sort du cas
prouvé :

- **`provision ≤ Σ virements ≤ total`** — la provision doit être entièrement
  couverte par de l'argent réellement sorti du fidéicommis, et ce qui en est
  sorti ne doit jamais dépasser ce qui a été facturé. L'égalité stricte serait
  trop étroite : sur `2026-012-01`, la provision (2 500,00 $) ne couvre que le
  PREMIER des deux virements, le second acquittant le solde ;
- l'invariant d'entrée `amount_due == total − retainer_applied` doit tenir ;
- la facture ne doit porter AUCUN paiement (`amount_paid == 0`) — l'écraser
  fausserait son solde ;
- son statut doit être « envoyée » ou « en retard ».

Fenêtre assumée : entre ce script et la reprise, `get_outstanding_total` monte
du total des provisions retirées, puis redescend quand les encaissements sont
portés. Les deux se lancent dans la même séance.
"""

import argparse
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from models import db
from models import trust
from utils.format_fr import format_cents_fr as f

#: Statuts d'une facture qui peut encore recevoir un encaissement — miroir de
#: `models.admin_ledger._ISSUED_INVOICE_STATUSES` (doctrine du vocabulaire :
#: on recopie un privé, on ne l'importe pas).
STATUTS_EMIS = ("envoyée", "en_retard")

#: La séance d'application (2026-08-17). Une facture créée APRÈS n'entre pas
#: dans le cas prouvé de la reprise : sa provision relève du système ACTUEL,
#: où elle est légitime (formulaire de création, import_invoice) — et où il
#: n'existe aucun `update_invoice` pour la rétablir une fois effacée. La
#: retirer facturerait au client une seconde fois l'argent qu'il a déjà
#: avancé (garde de rejeu, audit 2026-08-26).
DATE_CORRECTION = datetime(2026, 8, 17, tzinfo=timezone.utc)

_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]")


def normaliser(numero: str) -> str:
    """La clé de rapprochement — même règle que `reprise_encaissements`."""
    return _NON_ALNUM.sub("", numero or "").upper()


def lire_arbitrage(chemin: str) -> dict[str, str]:
    """`trust_tx_id` → numéro de facture, lu du CSV de `reprise_encaissements`.

    L'arbitrage du juriste s'écrit UNE fois, dans le fichier de la reprise, et
    les deux scripts le lisent — sans quoi une référence corrigée d'un côté
    resterait fausse de l'autre. Le cas réel : les deux virements du dossier
    2026-012 citent « 250701-01 » alors qu'ils acquittent « 2026-012-01 ».
    """
    import csv

    mapping: dict[str, str] = {}
    with open(chemin, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            tid = (row.get("trust_tx_id") or "").strip()
            num = (row.get("facture_numero") or "").strip().lstrip("'")
            if tid and num and (row.get("mode") or "").strip() == "encaissement":
                mapping[tid] = num
    return mapping


def _virements_par_facture(factures: list[dict],
                           arbitrage: dict[str, str]) -> dict[str, int]:
    """Σ des virements d'honoraires rapprochés de chaque facture.

    Trois chemins, dans cet ordre : l'arbitrage du juriste, puis l'identifiant
    que le virement porte, puis le NUMÉRO normalisé sur `invoice_number` ET
    sur le `legacy_ref` que la reprise du lot Q y consigne. Une correspondance
    ambiguë ne compte pas — la facture reste hors du cas prouvé, ce qui est le
    bon défaut.
    """
    index: dict[str, dict[str, dict]] = defaultdict(dict)
    par_numero: dict[str, str] = {}
    for x in factures:
        if x.get("invoice_number"):
            par_numero[x["invoice_number"]] = x["id"]
        cles = {x.get("invoice_number") or "",
                (x.get("legacy_ref") or "").replace("facture:", "")}
        for cle in cles:
            if cle:
                index[normaliser(cle)][x["id"]] = x

    sommes: dict[str, int] = defaultdict(int)
    for snap in db.collection(trust.TRANSACTIONS_COLLECTION).stream():
        t = snap.to_dict() or {}
        if t.get("purpose") != "virement_honoraires":
            continue
        if t.get("reversed_by_id") or t.get("status") == "annulée":
            continue
        iid = par_numero.get(arbitrage.get(t.get("id", ""), ""))
        if not iid:
            iid = t.get("invoice_id")
        if not iid:
            candidates = index.get(normaliser(t.get("invoice_external_ref")), {})
            iid = next(iter(candidates)) if len(candidates) == 1 else None
        if iid:
            sommes[iid] += int(t.get("amount", 0))
    return sommes


def _collect(arbitrage: Optional[dict[str, str]] = None) -> tuple[list[dict], list[str]]:
    """Les factures à corriger, plus les refus — qui ne doivent JAMAIS être tus.

    Une facture portant une provision que le script écarte est un cas que
    personne n'examinera si on ne le nomme pas : elle resterait avec un dû
    diminué et son virement finirait en « autre recette », sans que rien ne
    signale l'anomalie.
    """
    factures = []
    for snap in db.collection("invoices").stream():
        inv = snap.to_dict() or {}
        inv["id"] = snap.id
        factures.append(inv)

    sommes = _virements_par_facture(factures, arbitrage or {})
    cibles: list[dict] = []
    refus: list[str] = []

    for inv in factures:
        prov = int(inv.get("retainer_applied", 0) or 0)
        if prov <= 0:
            continue
        num = inv.get("invoice_number", inv["id"])
        # Garde de rejeu : le cas prouvé ne couvre que les factures
        # ANTÉRIEURES à la séance du 2026-08-17 (voir DATE_CORRECTION).
        if (inv.get("created_at") or DATE_CORRECTION) >= DATE_CORRECTION:
            refus.append(
                f"{num} : facture postérieure au 2026-08-17 — sa provision "
                f"relève du système actuel, où elle est légitime. NON corrigée")
            continue
        total = int(inv.get("total", 0) or 0)
        du = int(inv.get("amount_due", 0) or 0)
        paye = int(inv.get("amount_paid", 0) or 0)
        vire = sommes.get(inv["id"], 0)

        if not vire or vire < prov:
            refus.append(
                f"{num} : provision {f(prov)} > virements rapprochés "
                f"{f(vire)} — la provision n'est pas couverte par de l'argent"
                f" réellement sorti du fidéicommis, NON corrigée")
            continue
        if vire > total:
            refus.append(
                f"{num} : virements {f(vire)} > total facturé {f(total)} "
                f"— NON corrigée (le surplus n'est pas un encaissement de cette facture)")
            continue
        if paye:
            refus.append(
                f"{num} : porte déjà {f(paye)} d'encaissé — NON corrigée "
                f"(l'écraser fausserait son solde)")
            continue
        if inv.get("status") not in STATUTS_EMIS:
            refus.append(
                f"{num} : statut « {inv.get('status')} » — NON corrigée")
            continue
        if du != total - prov:
            refus.append(
                f"{num} : invariant rompu ({f(du)} ≠ {f(total)} − {f(prov)}) "
                f"— NON corrigée")
            continue

        cibles.append({
            "id": inv["id"], "numero": num,
            "dossier": inv.get("dossier_file_number", ""),
            "total": total, "provision": prov, "du": du,
        })

    cibles.sort(key=lambda c: -c["provision"])
    return cibles, refus


def _table(cibles: list[dict], titre: str) -> None:
    print(f"\n{titre}\n")
    print(f"{'Facture':<18} {'Dossier':<10} {'Total':>13} "
          f"{'Provision':>13} {'Dû':>13}")
    for c in cibles:
        print(f"{c['numero']:<18} {c['dossier']:<10} {f(c['total']):>13} "
              f"{f(c['provision']):>13} {f(c['du']):>13}")
    print(f"{'':<18} {'':<10} {'TOTAL provisions':>13} "
          f"{f(sum(c['provision'] for c in cibles)):>13}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Retire la provision qu'un virement d'honoraires acquitte.")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit la correction (défaut : simulation).")
    parser.add_argument("--rapprochement", metavar="FICHIER",
                        help="Le CSV de reprise_encaissements, dont la "
                             "colonne facture_numero porte vos arbitrages.")
    args = parser.parse_args(argv)

    arbitrage = lire_arbitrage(args.rapprochement) if args.rapprochement else {}
    if arbitrage:
        print(f"{len(arbitrage)} arbitrage(s) lu(s) dans {args.rapprochement}.")
    cibles, refus = _collect(arbitrage)
    for r in refus:
        print(f"⚠️  {r}")

    if not cibles:
        print("Aucune facture à corriger.")
        return 1 if refus else 0

    _table(cibles, f"{len(cibles)} facture(s) dont la provision correspond "
                   f"exactement à son virement :")
    print("\nAprès correction, leur « dû » redeviendra leur total, et le "
          "virement s'y imputera")
    print("comme un encaissement ordinaire — solde final inchangé.")

    if not args.apply:
        print("\n(simulation — relancez avec --apply pour écrire)")
        return 0

    print()
    now = datetime.now(timezone.utc)
    echecs = 0
    for c in cibles:
        try:
            db.collection("invoices").document(c["id"]).update({
                "retainer_applied": 0,
                "amount_due": c["total"],
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception as exc:
            echecs += 1
            print(f"❌ {c['numero']} : {exc}")
            continue
        print(f"✔  {c['numero']} : provision {f(c['provision'])} retirée, "
              f"dû {f(c['du'])} → {f(c['total'])}")

    # Re-preuve : on relit, et l'invariant doit tenir sur chaque ligne écrite.
    print()
    for c in cibles:
        snap = db.collection("invoices").document(c["id"]).get()
        inv = snap.to_dict() if snap.exists else None
        if inv is None:
            print(f"❌ {c['numero']} : illisible après écriture")
            echecs += 1
            continue
        prov = int(inv.get("retainer_applied", 0) or 0)
        du = int(inv.get("amount_due", 0) or 0)
        if prov or du != c["total"]:
            print(f"❌ {c['numero']} : après écriture, provision {f(prov)} "
                  f"et dû {f(du)} — attendu 0 et {f(c['total'])}")
            echecs += 1
    if echecs:
        print(f"\n❌ {echecs} anomalie(s).")
        return 1
    print("✅ Correction vérifiée : les huit invariants tiennent.")
    print("\nLancez maintenant « reprise_encaissements --proposer » : les "
          "virements correspondants")
    print("doivent y devenir imputables.")
    return 1 if refus else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
