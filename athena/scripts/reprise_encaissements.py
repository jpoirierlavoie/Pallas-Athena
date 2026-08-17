"""Reprise au grand livre des honoraires sortis du fidéicommis.

Quarante virements « Paiement d'honoraires » ont quitté le compte en
fidéicommis entre septembre 2025 et août 2026, pour 34 343,39 $, et le compte
d'administration n'en porte pas un dollar. Ce script les y inscrit.

Il n'est PAS une migration automatique, et il ne peut pas l'être : le
rapprochement virement → facture n'est pas dérivable des données. Un virement
d'honoraires n'est pas « le paiement d'une facture », c'est un mouvement
d'argent qui peut en acquitter plusieurs, une seule en partie, ou davantage
que tout ce qui est facturé — trois factures reçoivent plus que leur dû, une
référence en nomme deux à la fois, et treize citent un numéro qu'aucune
facture d'Athéna ne porte. C'est donc un ASSISTANT : il propose, le juriste
tranche, puis il exécute exactement ce qui a été signé.

    python -m scripts.reprise_encaissements --compte <id> --proposer
    python -m scripts.reprise_encaissements --compte <id> --verifier fichier.csv
    python -m scripts.reprise_encaissements --compte <id> --appliquer fichier.csv
    python -m scripts.reprise_encaissements --compte <id> --appliquer fichier.csv \
        --seulement <trust_tx_id>

UN VIREMENT = UNE ÉCRITURE, toujours pour son montant entier. Ce n'est pas
une simplification, c'est une contrainte : deux écritures partageant un
`trust_transaction_id` défont la clé d'idempotence, et
`routes/trust._contrepasser_recette_administration` n'en contre-passerait
qu'une en rendant `True`, sans bannière. Un virement qui ne peut pas s'imputer
entier sur une facture se porte donc en « autre recette » (aucun plafond,
aucun lien de facture) ou s'écarte — jamais en deux morceaux.

TOUTE ÉCRITURE ÉCRITE EST DÉFINITIVE. Portant à la fois `invoice_id` et
`trust_transaction_id`, elle refuse la modification, la suppression ET la
contre-passation depuis Administration (`_entry_lock_reason`, clauses 5 et 6) ;
sa seule correction repasserait par la contre-passation du virement au
fidéicommis, que le juriste refuse. D'où : un export Firestore avant, et un
premier `--appliquer` sur UN SEUL virement (`--seulement`).
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from models import admin_ledger as al
from models import db
from models import trust
from models.invoice import get_invoice, record_payment, update_status
from utils.format_fr import format_cents_fr

# ═══════════════════════════════════════════════════════════════════════════
# Couche PURE — aucune lecture Firestore, donc testable telle quelle
# ═══════════════════════════════════════════════════════════════════════════

COLONNES = (
    "trust_tx_id", "date", "montant", "reference_citee",
    "mode", "facture_numero", "note",
)

MODES = ("encaissement", "recette_autre", "ignorer")

#: Statuts d'une facture qui peut recevoir un encaissement (miroir de
#: models.admin_ledger._ISSUED_INVOICE_STATUSES — doctrine du vocabulaire :
#: on recopie, on n'importe pas un privé).
STATUTS_EMIS = ("envoyée", "en_retard")

_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]")
_PREFIXES_FORMULE = ("=", "+", "-", "@", "\t", "\r")


def normaliser(numero: str) -> str:
    """La clé de rapprochement d'un numéro de facture.

    Les tirets tombent parce que la reprise du lot Q les a perdus en chemin :
    le virement cite « 251601-01 » quand la facture importée s'appelle
    « 25160101 ». Sans cette normalisation, sept virements sur dix-sept ne se
    rapprochent d'aucune facture qui existe pourtant.
    """
    return _NON_ALNUM.sub("", numero or "").upper()


def indexer_factures(factures: list[dict]) -> dict[str, list[dict]]:
    """Numéro normalisé → factures. Une facture entre sous SON numéro ET sous
    son `legacy_ref` (la reprise y consigne le numéro d'origine), dédupliquée
    par id : les deux se normalisent souvent pareil, et compter deux fois la
    même facture la ferait passer pour ambiguë."""
    index: dict[str, dict[str, dict]] = defaultdict(dict)
    for f in factures:
        cles = {f.get("invoice_number") or "",
                (f.get("legacy_ref") or "").replace("facture:", "")}
        for cle in cles:
            if cle:
                index[normaliser(cle)][f["id"]] = f
    return {k: list(v.values()) for k, v in index.items()}


def resoudre(virement: dict, index: dict[str, list[dict]],
             factures_par_id: dict[str, dict]) -> tuple[Optional[dict], str]:
    """La facture visée par un virement. Rend (facture, "") ou (None, motif).

    Deux chemins, exclusifs, comme à la saisie : l'identifiant d'une facture
    d'Athéna, ou un NUMÉRO d'origine que la reprise vient d'y faire entrer.
    Le second est une correspondance de CHAÎNES, et c'est le seul endroit du
    script où l'on devine — d'où l'exactitude exigée (jamais un préfixe),
    l'unicité, et le contrôle du dossier, où un numéro réattribué par
    l'ancien système se trahit.
    """
    iid = virement.get("invoice_id")
    if iid:
        facture = factures_par_id.get(iid)
        return (facture, "") if facture else (None, "facture introuvable")

    ref = (virement.get("invoice_external_ref") or "").strip()
    if not ref:
        return None, "aucune facture citée"
    candidates = index.get(normaliser(ref), [])
    if not candidates:
        return None, "numéro d'origine pas encore repris dans Athéna"
    if len(candidates) > 1:
        return None, f"numéro ambigu ({len(candidates)} factures)"
    facture = candidates[0]
    if facture.get("dossier_id") and virement.get("dossier_id") \
            and facture["dossier_id"] != virement["dossier_id"]:
        return None, "la facture appartient à un autre dossier"
    return facture, ""


def motif_inexecutable(facture: dict, somme_groupe: int,
                       deja_au_registre: int) -> str:
    """Pourquoi un virement ne peut PAS s'imputer sur cette facture. "" sinon.

    La comparaison porte sur `amount_due`, figé à l'émission, jamais sur le
    solde vivant : `amount_paid` bouge entre deux exécutions, `amount_due` non,
    et c'est ce qui rend l'invariant stable au rejeu.
    """
    if facture.get("status") == "annulée":
        return "facture annulée"
    if facture.get("status") == "brouillon":
        return "facture encore au brouillon — à promouvoir d'abord"
    du = int(facture.get("amount_due", 0) or 0)
    if du <= 0:
        return "facture sans solde à l'émission"
    total = somme_groupe + deja_au_registre
    if total > du:
        return (f"les virements de cette facture totalisent "
                f"{format_cents_fr(total)} pour un dû de {format_cents_fr(du)}")
    return ""


def neutraliser(texte: str) -> str:
    """Empêche un tableur d'exécuter une cellule comme une formule."""
    return "'" + texte if texte.startswith(_PREFIXES_FORMULE) else texte


def deneutraliser(texte: str) -> str:
    """L'inverse — indispensable au VA-ET-VIENT.

    Sans elle, la valeur relue n'est plus celle qui a été proposée : un nom
    de client venu du portail commence légitimement par un tiret, et il
    reviendrait avec une apostrophe collée devant.
    """
    return texte[1:] if texte.startswith("'") else texte


def _date_str(valeur) -> str:
    return valeur.strftime("%Y-%m-%d") if hasattr(valeur, "strftime") else ""


# ═══════════════════════════════════════════════════════════════════════════
# Lectures
# ═══════════════════════════════════════════════════════════════════════════


def lire_virements(seulement: Optional[str] = None) -> tuple[list[dict], list[tuple]]:
    """Les virements d'honoraires, du plus ancien au plus récent, + les exclus.

    Un virement contre-passé ou annulé n'a rien déplacé net : l'inscrire
    enregistrerait une recette revenue au client. On les ÉCARTE, mais on les
    nomme — un décompte qui ne retombe pas sur 40 doit s'expliquer.
    """
    q = db.collection(trust.TRANSACTIONS_COLLECTION)
    retenus, exclus = [], []
    for snap in q.stream():
        t = snap.to_dict() or {}
        if t.get("purpose") != "virement_honoraires":
            continue
        if seulement and t.get("id") != seulement:
            continue
        if t.get("reversed_by_id") or t.get("status") == "annulée":
            exclus.append((t, "virement contre-passé ou annulé"))
            continue
        retenus.append(t)
    retenus.sort(key=lambda t: (t.get("date") or datetime.min.replace(
        tzinfo=timezone.utc), int(t.get("sequence") or 0)))
    return retenus, exclus


def lire_factures() -> list[dict]:
    factures = []
    for snap in db.collection("invoices").stream():
        f = snap.to_dict() or {}
        f.setdefault("id", snap.id)
        factures.append(f)
    return factures


# ═══════════════════════════════════════════════════════════════════════════
# --proposer
# ═══════════════════════════════════════════════════════════════════════════


def proposer(chemin: str, seulement: Optional[str]) -> int:
    virements, exclus = lire_virements(seulement)
    factures = lire_factures()
    par_id = {f["id"]: f for f in factures}
    index = indexer_factures(factures)

    # Résolution, puis somme par facture — l'ordre compte : le motif
    # d'inexécution d'un virement dépend de ses FRÈRES.
    resolus: list[tuple[dict, Optional[dict], str]] = []
    groupes: dict[str, int] = defaultdict(int)
    for v in virements:
        facture, motif = resoudre(v, index, par_id)
        resolus.append((v, facture, motif))
        if facture:
            groupes[facture["id"]] += int(v.get("amount", 0))

    lignes = []
    for v, facture, motif in resolus:
        if facture:
            deja = al.sum_invoice_receipts(facture["id"])
            motif = motif_inexecutable(facture, groupes[facture["id"]], deja)
        mode = "encaissement" if (facture and not motif) else "recette_autre"
        lignes.append({
            "trust_tx_id": v.get("id", ""),
            "date": _date_str(v.get("date")),
            "montant": format_cents_fr(int(v.get("amount", 0))),
            "reference_citee": v.get("invoice_external_ref", "") or "(par id)",
            "mode": mode,
            "facture_numero": facture.get("invoice_number", "") if facture and not motif else "",
            "note": motif or "",
        })

    with open(chemin, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLONNES)
        w.writeheader()
        for ligne in lignes:
            w.writerow({k: neutraliser(str(v)) for k, v in ligne.items()})

    prets = sum(1 for l in lignes if l["mode"] == "encaissement")
    print(f"{len(virements)} virement(s) retenu(s), {len(exclus)} écarté(s).")
    print(f"  imputables sur une facture : {prets}")
    print(f"  à trancher par vous        : {len(lignes) - prets}")
    for t, motif in exclus:
        print(f"  écarté — {_date_str(t.get('date'))} "
              f"{format_cents_fr(int(t.get('amount', 0)))} : {motif}")
    print(f"\nProposition écrite dans {chemin}")
    print("Relisez-la, corrigez la colonne « mode » et « facture_numero », "
          "puis --verifier.")
    print("Le mode « recette_autre » inscrit la recette SANS lien de facture ; "
          "« ignorer » n'inscrit rien.")
    return 0


# ═══════════════════════════════════════════════════════════════════════════
# --verifier / --appliquer
# ═══════════════════════════════════════════════════════════════════════════


def lire_csv(chemin: str) -> list[dict]:
    with open(chemin, encoding="utf-8-sig", newline="") as fh:
        return [{k: deneutraliser((v or "").strip()) for k, v in row.items()}
                for row in csv.DictReader(fh)]


def _etat(trust_tx_id: str) -> tuple[str, Optional[dict], str]:
    """Où en est ce virement au grand livre. (état, écriture, motif de refus).

    Fail-CLOSED par construction : `list_by_trust_transaction` propage. Une
    lecture ratée doit arrêter la reprise, jamais lui faire conclure « rien
    n'est écrit » — ce qui doublerait une écriture qu'on ne peut plus corriger.
    """
    lignes = al.list_by_trust_transaction(trust_tx_id)
    if not lignes:
        return "à_créer", None, ""
    if len(lignes) > 1:
        return "refus", None, f"{len(lignes)} écritures portent déjà ce virement"
    ecriture = lignes[0]
    if ecriture.get("status") == "annulée" or ecriture.get("reversed_by_id"):
        return "refus", ecriture, "l'écriture existante est annulée"
    if ecriture.get("status") == "en_circulation":
        return "à_compenser", ecriture, ""
    return "faite", ecriture, ""


def planifier(compte_id: str, lignes: list[dict],
              seulement: Optional[str]) -> tuple[list[dict], list[str]]:
    """Rejoue TOUTES les gardes sans écrire. Rend (actions, refus)."""
    refus: list[str] = []
    compte = al.get_account(compte_id)
    if not compte:
        return [], ["Compte d'administration introuvable."]
    if compte.get("account_type") != "opérations":
        return [], ["Un encaissement de facture s'inscrit au compte "
                    "d'opérations, jamais à la carte de crédit."]
    if compte.get("status") != "actif":
        return [], ["Ce compte est fermé."]

    virements, _ = lire_virements(seulement)
    par_tx = {v["id"]: v for v in virements}
    factures = lire_factures()
    par_numero = {f.get("invoice_number", ""): f for f in factures}

    actions: list[dict] = []
    groupes: dict[str, int] = defaultdict(int)
    for ligne in lignes:
        tid = ligne.get("trust_tx_id", "")
        if seulement and tid != seulement:
            continue
        virement = par_tx.get(tid)
        if virement is None:
            refus.append(f"{tid} : ce virement n'est plus éligible "
                         f"(contre-passé, annulé, ou inconnu).")
            continue
        mode = ligne.get("mode", "")
        if mode not in MODES:
            refus.append(f"{tid} : mode « {mode} » inconnu.")
            continue
        if mode == "ignorer":
            continue

        etat, ecriture, motif = _etat(tid)
        if etat == "refus":
            refus.append(f"{tid} : {motif}")
            continue
        if etat == "faite":
            continue

        facture = None
        if mode == "encaissement":
            numero = ligne.get("facture_numero", "")
            facture = par_numero.get(numero)
            if facture is None:
                refus.append(f"{tid} : aucune facture « {numero} ».")
                continue
            if facture.get("status") not in STATUTS_EMIS \
                    and not _sera_rouverte(facture):
                refus.append(f"{tid} : la facture « {numero} » est "
                             f"« {facture.get('status')} » et ne peut pas "
                             f"recevoir d'encaissement.")
                continue
            groupes[facture["id"]] += int(virement.get("amount", 0))

        actions.append({
            "virement": virement, "facture": facture, "mode": mode,
            "etat": etat, "ecriture": ecriture,
        })

    # L'invariant de saturation : la somme d'un groupe, augmentée de ce que
    # le registre porte DÉJÀ hors de ce lot, ne doit pas dépasser le dû.
    for facture_id, somme in groupes.items():
        facture = next(f for f in factures if f["id"] == facture_id)
        deja = al.sum_invoice_receipts(facture_id)
        motif = motif_inexecutable(facture, somme, deja)
        if motif:
            refus.append(f"facture {facture.get('invoice_number')} : {motif}")

    actions.sort(key=lambda a: (a["virement"].get("date"),
                                int(a["virement"].get("sequence") or 0)))
    return actions, refus


def _sera_rouverte(facture: dict) -> bool:
    """Une facture « payée » que la passe de dépaiement va ramener."""
    return facture.get("status") == "payée"


def _depayer(actions: list[dict]) -> list[str]:
    """Ramène à « envoyée » les factures visées, AVANT toute écriture.

    Deux mécanismes, parce que le modèle en impose deux : un montant inscrit
    s'efface par `record_payment(id, 0)`, dont l'annulation étroite fait la
    bascule ; un « payée » posé à la main SANS montant est délibérément
    intouchable par `record_payment` et ne se retire que par `update_status`.
    """
    echecs = []
    vues: set[str] = set()
    for a in actions:
        facture = a["facture"]
        if not facture or facture["id"] in vues:
            continue
        vues.add(facture["id"])
        vivante = get_invoice(facture["id"])
        if vivante is None:
            echecs.append(f"facture {facture['id']} : illisible")
            continue
        if vivante.get("status") != "payée":
            continue
        if int(vivante.get("amount_paid", 0) or 0) > 0:
            _, erreurs = record_payment(facture["id"], 0)
            mecanisme = "montant remis à zéro"
        else:
            ok, err = update_status(facture["id"], "envoyée")
            erreurs = [] if ok else [err]
            mecanisme = "statut ramené à « envoyée »"
        if erreurs:
            echecs.append(f"facture {vivante.get('invoice_number')} : "
                          f"{erreurs[0]}")
        else:
            print(f"  ↩ {vivante.get('invoice_number')} — {mecanisme}")
    return echecs


def appliquer(compte_id: str, actions: list[dict]) -> list[str]:
    """Exécute, virement par virement, dans l'ordre chronologique."""
    from routes.admin_ledger import _projeter_paiement

    echecs = _depayer(actions)
    if echecs:
        return echecs

    for a in actions:
        v, facture = a["virement"], a["facture"]
        ecriture = a["ecriture"]
        if a["etat"] == "à_créer":
            description = (
                f"Paiement d'honoraires du fidéicommis — dossier "
                f"{v.get('dossier_file_number', '')}"
            )
            if v.get("invoice_external_ref"):
                description += f" (facture d'origine {v['invoice_external_ref']})"
            description += " — reprise historique"
            ecriture, erreurs = al.create_transaction({
                "account_id": compte_id,
                "kind": "encaissement_facture" if facture else "recette_autre",
                "direction": "recette",
                "amount": int(v.get("amount", 0)),
                "method": "virement",
                "counterparty": v.get("client_name") or "Fidéicommis",
                "date": v.get("date"),
                "description": description,
                "reference": v.get("reference", ""),
                "invoice_id": facture["id"] if facture else None,
                "dossier_id": None if facture else (v.get("dossier_id") or None),
                "trust_transaction_id": v.get("id"),
            })
            if erreurs:
                return [f"{v.get('id')} : {erreurs[0]}"]
            print(f"  ✔ {_date_str(v.get('date'))} "
                  f"{format_cents_fr(int(v.get('amount', 0))):>13} "
                  f"{facture.get('invoice_number') if facture else '(autre recette)'}")

        if ecriture.get("status") == "en_circulation":
            _, erreurs = al.clear_transaction(ecriture["id"], ecriture.get("date"))
            if erreurs:
                return [f"{v.get('id')} : compensation refusée — {erreurs[0]}"]

        if ecriture.get("invoice_id") and not _projeter_paiement(ecriture):
            # ARRÊT DUR. L'écriture est COMMISE et la facture n'est pas
            # créditée ; elle porte invoice_id et trust_transaction_id, donc
            # elle est incorrigible depuis l'application. Continuer la boucle
            # multiplierait ce cas.
            return [
                f"{v.get('id')} : l'écriture {ecriture['id']} est inscrite mais "
                f"la facture n'a PAS été créditée. Arrêt. Cette écriture ne se "
                f"corrige pas depuis l'application — faites-la examiner avant "
                f"de relancer."
            ]
    return []


# ═══════════════════════════════════════════════════════════════════════════


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--compte", required=True,
                   help="Identifiant du compte d'administration (opérations).")
    p.add_argument("--proposer", metavar="FICHIER",
                   help="Écrit la proposition de rapprochement (n'écrit rien en base).")
    p.add_argument("--verifier", metavar="FICHIER",
                   help="Rejoue toutes les gardes sans écrire.")
    p.add_argument("--appliquer", metavar="FICHIER",
                   help="Exécute le fichier vérifié.")
    p.add_argument("--seulement", metavar="TRUST_TX_ID",
                   help="Restreint à un seul virement — la PREMIÈRE exécution.")
    args = p.parse_args(argv)

    if args.proposer:
        return proposer(args.proposer, args.seulement)

    chemin = args.verifier or args.appliquer
    if not chemin:
        p.error("choisissez --proposer, --verifier ou --appliquer")

    lignes = lire_csv(chemin)
    actions, refus = planifier(args.compte, lignes, args.seulement)

    for r in refus:
        print(f"  ✖ {r}")
    total = sum(int(a["virement"].get("amount", 0)) for a in actions)
    print(f"\n{len(actions)} écriture(s) à porter, {format_cents_fr(total)}.")

    if refus:
        print(f"\n{len(refus)} impossibilité(s) — rien ne sera écrit.")
        return 2
    if args.verifier:
        print("(vérification seule — relancez avec --appliquer pour écrire)")
        return 0
    if not actions:
        print("Rien à faire.")
        return 0

    print()
    echecs = appliquer(args.compte, actions)
    if echecs:
        print("\n❌ ARRÊT :")
        for e in echecs:
            print(f"   {e}")
        return 1
    print("\n✅ Reprise appliquée. Lancez maintenant "
          "python -m scripts.verify_admin_integrity — aucun écart attendu.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
