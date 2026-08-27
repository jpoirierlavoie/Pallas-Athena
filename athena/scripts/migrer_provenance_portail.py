"""Sortir la provenance du portail du champ `description` (2026-08-27).

Jusqu'ici le versement de Réception écrivait l'invitation, le lot et le
SHA-512 DANS `description` — décision de 2026-07-25, « aucun champ nouveau ».
Le défaut n'est apparu qu'à l'usage : `description` est le SEUL champ de
texte libre que le formulaire d'édition offre au juriste, si bien qu'il ne
pouvait pas décrire un document reçu sans effacer sa traçabilité, ni la
garder sans renoncer à le décrire.

Ce script transporte les trois valeurs vers leurs champs dédiés et VIDE la
description — mais seulement quand elle ne contient QUE la ligne de
provenance. Une description à laquelle le juriste a déjà ajouté quelque
chose est laissée telle quelle et signalée : on ne devine pas où finit la
provenance et où commence son texte.

    python -m scripts.migrer_provenance_portail            # simulation
    python -m scripts.migrer_provenance_portail --apply    # écrit
"""

import argparse
import os
import re
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass


_load_env()

from models import db  # noqa: E402
from models.document import COLLECTION  # noqa: E402

# La ligne telle que `routes/reception.py` l'écrivait, mot pour mot. Un motif
# ANCRÉ au début et strict sur la forme : mieux vaut ne pas reconnaître une
# description et la laisser au juriste que d'en découper une par erreur.
_MOTIF = re.compile(
    r"^Reçu via le portail — invitation ([0-9a-f-]{36}), lot (\S+)\. "
    r"SHA-512 : ([0-9a-f]*)\s*$"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="écrit (sans ce drapeau : simulation)")
    args = parser.parse_args()

    vus = migres = intacts = deja = 0
    for snap in db.collection(COLLECTION).stream():
        doc = snap.to_dict() or {}
        desc = (doc.get("description") or "").strip()
        if not desc.startswith("Reçu via le portail"):
            continue
        vus += 1
        if doc.get("portail_invitation_id"):
            deja += 1
            continue

        m = _MOTIF.match(desc)
        if not m:
            intacts += 1
            print(f"  INTACT  {snap.id} — description enrichie par le juriste, "
                  f"laissée telle quelle : {desc[:70]}…")
            continue

        inv, lot, sha = m.group(1), m.group(2), m.group(3)
        migres += 1
        print(f"  MIGRE   {snap.id} — invitation {inv[:8]}…, lot {lot}, "
              f"sha {sha[:16]}…")
        if args.apply:
            snap.reference.update({
                "portail_invitation_id": inv,
                "portail_lot": lot,
                "portail_sha512": sha,
                "description": "",
                "updated_at": datetime.now(timezone.utc),
                "etag": str(uuid.uuid4()),
            })

    print()
    print(f"documents portail : {vus} · à migrer : {migres} · "
          f"déjà faits : {deja} · laissés intacts : {intacts}")
    if not args.apply:
        print("SIMULATION — rien n'a été écrit. Relancer avec --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
