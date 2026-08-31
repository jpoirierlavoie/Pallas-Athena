"""Vider `description`, le troisième champ de texte d'un document (2026-08-31).

Décision du praticien : un document porte le texte du JURISTE (« notes
internes ») et celui du MODÈLE (« analyse »). `description` était le
troisième, et il était redondant par construction — `record_analyse` y
RECOPIAIT le résumé, donc après toute analyse les deux portaient la même
chaîne. Depuis que les deux s'éditaient séparément, elles pouvaient en
plus diverger, ce qui est pire que la redondance.

Mesuré avant d'écrire une ligne, sur les 984 documents de la production :

    57 descriptions, dont
    10  identiques au résumé de l'analyse   → redondance pure, on jette
    46  provenance écrite par le CODE       → `genere_depuis`
     1  écrite à la main par le juriste     → `notes_internes`

C'est ce comptage qui a donné sa forme au retrait : la provenance n'est ni
le texte du juriste ni celui du modèle, donc elle prend son champ à elle
plutôt que de polluer l'un des deux. Le précédent est celui du portail,
dont la provenance a quitté `description` le 2026-08-27 pour la même
raison exactement.

Trois règles, dans cet ordre — la première qui s'applique gagne :

  1. `description` == le résumé de l'analyse → on VIDE. La valeur est
     déjà stockée dans `analyse.resume`, et le journal en garde
     l'historique.
  2. `description` commence par une formule de provenance connue →
     `genere_depuis`. Le motif est ANCRÉ et strict : mieux vaut ne pas
     reconnaître une description et la laisser au juriste que d'en
     classer une par erreur.
  3. Sinon, c'est du texte humain → `notes_internes`. S'il y a DÉJÀ des
     notes internes, les deux sont conservées, séparées par une ligne
     vide : le champ du juriste ne se perd jamais, même partiellement.

    python -m scripts.migrer_description_documents            # simulation
    python -m scripts.migrer_description_documents --apply    # écrit
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

# Les formules que le code écrivait lui-même, mot pour mot. ANCRÉES au
# début : une description du juriste qui contiendrait « générée depuis »
# au milieu d'une phrase n'est pas de la provenance.
_MACHINE = re.compile(
    r"^(Générée? depuis (la facture|le gabarit)"
    r"|Reçu via le portail"
    r"|Brouillon .{0,80}versé)",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="écrit (sans ce drapeau : simulation)")
    args = parser.parse_args()

    # Le compte rendu porte « → » et des guillemets français : une console
    # Windows en cp1252 lèverait UnicodeEncodeError APRÈS avoir écrit une
    # partie des lignes. Même remède que scripts/migrate_vocabulaires.py.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    vus = jetes = provenance = notes = fusionnes = 0
    for snap in db.collection(COLLECTION).stream():
        doc = snap.to_dict() or {}
        desc = (doc.get("description") or "").strip()
        if not desc:
            continue
        vus += 1
        resume = ((doc.get("analyse") or {}).get("resume") or "").strip()
        maj: dict = {"description": ""}

        if resume and desc == resume:
            jetes += 1
            quoi = "REDONDANTE (== résumé)"
        elif _MACHINE.match(desc):
            provenance += 1
            maj["genere_depuis"] = desc
            quoi = "provenance → genere_depuis"
        else:
            existantes = (doc.get("notes_internes") or "").strip()
            if existantes:
                fusionnes += 1
                maj["notes_internes"] = f"{existantes}\n\n{desc}"
                quoi = "texte → notes_internes (FUSION, rien de perdu)"
            else:
                notes += 1
                maj["notes_internes"] = desc
                quoi = "texte → notes_internes"

        nom = (doc.get("display_name") or doc.get("filename") or "")[:40]
        print(f"  {quoi:44s} « {nom} »")
        if args.apply:
            maj["updated_at"] = datetime.now(timezone.utc)
            maj["etag"] = str(uuid.uuid4())
            snap.reference.update(maj)

    print()
    print(f"descriptions : {vus} · jetées (redondantes) : {jetes} · "
          f"provenance : {provenance} · notes : {notes} · fusions : {fusionnes}")
    if not args.apply:
        print("SIMULATION — rien n'a été écrit. Relancer avec --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
