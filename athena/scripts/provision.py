"""Constater l'état de provisionnement d'un déploiement, et rendre le plan.

    python -m scripts.provision --project VOTRE-PROJET
    python -m scripts.provision --project VOTRE-PROJET --json
    python -m scripts.provision --project VOTRE-PROJET --plan-seulement

**Ce script ne mute RIEN, et ce n'est pas une promesse.** Chaque commande qu'il
exécute vient de ``utils/deployment_inventory.RESOURCES``, dont un balayage de
``tests/test_deployment_inventory.py`` refuse — par liste BLANCHE — tout verbe
qui n'est pas ``describe`` / ``list`` / ``get-iam-policy``. Il n'existe pas
d'``--apply`` : la moitié qui mute ne peut pas être éprouvée sans dépenser un
projet jetable, et la leçon du 2026-09-11, payée quatre fois en une journée,
est qu'une commande écrite mais jamais exécutée est confiante et fausse.

``--project`` est OBLIGATOIRE et n'est jamais hérité de ``gcloud config``. Un
projet actif périmé est la façon dont on inspecte — ou provisionne — chez
quelqu'un d'autre.

La région n'est pas demandée : elle est LUE de l'application App Engine, qui
fait autorité parce que sa région est définitive. ``--region`` existe pour un
projet où l'application n'existe pas encore, et sert alors de repli.

Codes de sortie — et le 2 distingué compte, parce qu'un CI qui confond
« va cliquer dans une console » avec « quelque chose est cassé » finit par
ignorer les deux :

    0  tout ce qui est constatable est en place
    1  DÉRIVE — une ressource existe et contredit ce que le code attend
    2  du travail reste (à poser, à faire à la main, ou non constaté)

Un échec de sonde — ``gcloud`` absent, identifiants périmés, API désactivée —
rend ``inconnu``, JAMAIS « absent ». Un rapport ne doit pas fabriquer un
manquement à partir d'une lecture ratée : ce serait envoyer l'exploitant
provisionner ce qui existe déjà.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config_checks import run_all  # noqa: E402
from utils.deployment_probe import (  # noqa: E402
    check_resources,
    exit_code,
    render_plan,
)
from utils.deployment_report import (  # noqa: E402
    FAIL,
    OK,
    WARN,
    render_json,
)


def _load_env() -> None:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--project",
        required=True,
        help=("Identifiant du projet GCP à INSPECTER. Obligatoire, et jamais "
              "hérité de `gcloud config` : un projet actif périmé est la "
              "façon dont on inspecte chez quelqu'un d'autre."),
    )
    parser.add_argument(
        "--region",
        default="",
        help=("Repli utilisé seulement tant que l'application App Engine "
              "n'existe pas. Sinon la région est LUE d'elle : sa région est "
              "définitive, donc c'est elle qui fait autorité."),
    )
    parser.add_argument(
        "--plan-seulement",
        action="store_true",
        help="N'imprimer que le plan, sans les constats ligne par ligne.",
    )
    parser.add_argument(
        "--sans-config",
        action="store_true",
        help=("Sauter les quatre passes de `check_config` et ne sonder que "
              "les ressources."),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Rapport structuré, pour une machine.",
    )
    args = parser.parse_args()

    silencieux = args.json or args.plan_seulement
    printer = None if silencieux else print

    if not silencieux:
        print("Pallas Athena — état de provisionnement de « %s »" % args.project)
        print("Lecture seule : ce script ne crée, ne modifie et ne supprime "
              "rien.")

    if args.sans_config:
        from utils.deployment_report import Report

        rpt = Report(printer=printer)
    else:
        rpt = run_all(is_prod=None, printer=printer)

    resultats = check_resources(rpt, args.project, args.region)
    code = exit_code(resultats)

    if args.json:
        charge = render_json(rpt)
        charge["project"] = args.project
        charge["resources"] = [
            {"key": r.key, "state": r.state, "detail": r.detail}
            for r in resultats
        ]
        charge["exit_code"] = code
        print(json.dumps(charge, indent=2, ensure_ascii=False))
        return code

    print()
    print(render_plan(resultats))

    if not args.plan_seulement:
        counts = rpt.counts()
        print()
        print("Résumé")
        print("------")
        print("  %d ok, %d avertissement(s), %d échec(s)"
              % (counts[OK], counts[WARN], counts[FAIL]))
    print()
    print({
        0: "RÉSULTAT : tout ce qui est constatable est en place.",
        1: "RÉSULTAT : DÉRIVE — une ressource contredit ce que le code "
           "attend ; voir « À CORRIGER » ci-dessus.",
        2: "RÉSULTAT : du travail reste. Rien n'est cassé ; des étapes ne "
           "sont pas faites, ou n'ont pas pu être constatées.",
    }[code])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
