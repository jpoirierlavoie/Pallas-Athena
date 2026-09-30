"""One-shot: designate the ACTIVE template of each special kind (lot 2A, T3).

Until T3 the invoice note d'honoraires and the note print were filled from
« the most recently updated template of the kind »
(``models.doc_template.recency_winner`` keeps that rule verbatim). Since
T3 the application reads ONLY the template the lawyer designated
(``active_for``), with no recency fallback — so a kind with no designation
refuses to generate.

This script designates, for each special kind that has no designation
yet, the template production is using TODAY: the recency winner, by the
exact rule the old code applies. It therefore changes nothing a user can
see — which is why it must run BEFORE the T3 code is deployed: the old
code ignores the new field, and the new code finds the designation already
in place, so production never passes through an undesignated state
(DEPLOYMENT.md §15).

* Dry-run by default: it prints, per kind, what it WOULD designate.
* ``--apply`` designates, through ``set_active_template`` — the model's own
  transaction, the one the web « Désigner comme gabarit actif » button
  uses — against the etag it just read (a template edited in between is
  refused, never designated blind; re-run).
* Idempotent: a kind that already has a designation is left alone. When
  that designation is NOT the template the old code would pick, it says
  so — between this run and the deploy, the old code still selects by
  recency, so that line means production is printing another template
  than the one the new code will use.

    python -m scripts.designer_gabarits_actifs            # simulation
    python -m scripts.designer_gabarits_actifs --apply    # écriture

Exit codes: 0 — nothing left to do (or simulation without anomaly);
1 — a designation failed, or an anomaly needs a look; 2 — the templates
could not be read (nothing was written).
"""

import argparse
import sys

from models import db
from models import doc_template as tpl

#: What the designation records as its author.
PAR = "migration (gabarit le plus récent, lot 2A)"


def _label(t: dict) -> str:
    return f"« {t.get('name') or '(sans nom)'} » ({t.get('id', '')})"


def _read_all() -> list[dict]:
    return [snap.to_dict() or {} for snap in db.collection(tpl.COLLECTION).stream()]


def plan(templates: list[dict]) -> list[dict]:
    """One line per special kind: what exists, and what to do (pure)."""
    lines = []
    for kind in tpl.SPECIAL_KINDS:
        holders = [t for t in templates
                   if t.get("active_for") == kind
                   and (t.get("kind") or "gabarit") == kind]
        winner = tpl.recency_winner(list(templates), kind)
        line = {"kind": kind, "holders": holders, "winner": winner,
                "action": "none", "anomaly": ""}
        if len(holders) > 1:
            line["anomaly"] = "plusieurs gabarits sont désignés pour ce type"
        elif holders:
            if winner is not None and winner.get("id") != holders[0].get("id"):
                line["anomaly"] = (
                    "le gabarit désigné n'est pas le plus récent : jusqu'au "
                    "déploiement, l'ancien code imprime " + _label(winner)
                )
        elif winner is not None:
            line["action"] = "designate"
        lines.append(line)
    return lines


def _safe_console() -> None:
    """Never crash on the console's encoding.

    The script is run from the lawyer's Windows machine, whose console
    encodes cp1252: a character outside it (an emoji) raised
    ``UnicodeEncodeError`` — in ``--apply``, AFTER a designation was written
    and before the next one. The markers are ASCII; this is the belt.
    """
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        # A stream that cannot be reconfigured keeps its encoding — nothing to repair.
        pass


def main(argv: list[str]) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(
        description="Désigne le gabarit actif de chaque type spécial.")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit la désignation (défaut : simulation).")
    args = parser.parse_args(argv)

    try:
        templates = _read_all()
    except Exception as exc:
        print(f"[ECHEC] Lecture des gabarits impossible "
              f"({type(exc).__name__}) — rien n'a été écrit.")
        return 2

    status = 0
    for line in plan(templates):
        name = tpl.ACTIVE_KIND_NAMES[line["kind"]]
        print(f"\n— « {name} » —")
        for holder in line["holders"]:
            print(f"   déjà désigné : {_label(holder)}")
        if line["anomaly"]:
            print(f"   [!] {line['anomaly']}")
            status = 1
        if line["action"] == "none" and not line["holders"]:
            print("   aucun gabarit de ce type : rien à désigner (la "
                  "génération refusera jusqu'à ce que le juriste en "
                  "désigne un dans Gabarits).")
            continue
        if line["action"] != "designate":
            continue
        winner = line["winner"]
        print(f"   à désigner : {_label(winner)} — le gabarit que la "
              f"génération utilise aujourd'hui")
        if not args.apply:
            continue
        doc, errors = tpl.set_active_template(
            winner["id"], par=PAR, expected_etag=str(winner.get("etag") or ""))
        if errors:
            print(f"   [ECHEC] {'; '.join(errors)}")
            status = 1
        else:
            print(f"   [OK] désigné : {_label(doc)}")

    if not args.apply:
        print("\n(simulation — relancez avec --apply pour écrire)")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
