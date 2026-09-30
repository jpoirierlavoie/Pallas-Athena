"""Backfill: give every existing dossier the default folder tree (2026-09-30).

Since the lawyer's decision of 2026-09-30 every dossier carries the same
eighteen folders (``models.folder.DEFAULT_TREE``), seven of them the
application's own. A NEW dossier receives them at creation; this script
gives them to the dossiers that already exist — the same work the Fichiers
tab's « Créer l'arborescence par défaut » button does for ONE dossier, by
the same engine: ``models.folder.ensure_default_tree(relocate=True)``. So it
also moves the old ROOT « Projets » and « Reçus du portail » under
« Interne » and « Autres ».

* Dry-run by DEFAULT: one strict read of each dossier's folders, planned by
  the engine's own pure planner (``plan_default_tree_from`` — what
  ``plan_default_tree`` runs, over the SAME read the homonym report uses, so
  the plan and the report cannot disagree). Nothing is written.
* ``--apply`` writes, through ``ensure_default_tree`` — which re-reads the
  folders inside its transaction and re-plans on what it locks.
* ``--dossier ID`` treats one dossier; ``--statuts actif,en_attente``
  restricts to those statuses (default: every dossier, whatever its status —
  a closed file keeps its documents too). Without either, reading NO
  dossier at all is a failure (exit 1): it is what a wrong project reads.
* A second ``--apply`` writes nothing — UNLESS the lawyer deleted a folder
  of the tree since: every default folder he deleted, the ordinary ones
  included (« Transcriptions » he had no use for), is RECREATED at the same
  id. To repair one dossier without touching the others, pass ``--dossier``.
* It never touches a DOCUMENT. A folder that moves keeps its documents
  (they point at the folder's id); the documents a portal versement left at
  a dossier's ROOT before « Reçus du portail » existed stay where they are.
* Each dossier's line says what is done — created, ADOPTED (an existing
  folder that becomes the application's own: it can no longer be renamed or
  moved), moved — and, apart, how many existing ordinary folders were
  REUSED by their name (nothing is written for them, so a dossier holding
  its own « Correspondance » still reads « (à jour) » on a re-run). Every
  adoption and every move gets its own line, by the DEFAULT folder's name.
* Out-of-place HOMONYMS — a folder whose name is a default folder's name
  (case and Unicode normalization aside) that the engine did not take for
  that folder, e.g. a root « Déboursés » of the lawyer's own — are LISTED
  « à vérifier », by folder name only, with the parent's name when that
  parent bears a default folder's name. A folder inside a system folder's
  subtree (a « Courriels » under « Reçus du portail ») is the lawyer's own
  sub-filing there and is never listed. Nothing is merged: the lawyer looks.

The output is aggregates only: per dossier its file number and the counts,
never a title or a party name (a folder's own name is printed only when it
IS a default folder's name — generic by construction).

The printed output is the ONLY record of a run: the engine's log events
(``default_folder_tree_created``, ``system_folder_adopted``…) do NOT reach
Cloud Logging from a script — the logging handlers are attached by the
Flask app (``utils.logging_setup.init_app``), never here. Keep the output.

The script reads a ``.env`` if it finds one (``find_dotenv``, from the
working directory upward): it fills the variables left unset and never
overrides one given inline — so the variables given inline
(``GOOGLE_CLOUD_PROJECT``, the credentials) must point at the intended
project. The project read and the ``.env`` file loaded are printed first.

    python -m scripts.arborescence_par_defaut                 # simulation
    python -m scripts.arborescence_par_defaut --apply         # écriture
    python -m scripts.arborescence_par_defaut --dossier ID    # un seul dossier
    python -m scripts.arborescence_par_defaut --statuts actif,en_attente

Exit codes: 0 — done, nothing blocked; 1 — a read or a write failed, a
dossier could not be treated, a folder of the tree could not be placed (its
reason is printed), or — without ``--dossier``/``--statuts`` — no dossier
was read at all; 2 — a usage error (argparse).
"""

import argparse
import os
import sys
import unicodedata
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> str:
    """Load a ``.env`` if one is found — returns its path (``""`` = none).
    It never overrides a variable given inline (``override`` off)."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return ""
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path or ""


#: The ``.env`` this run read (``""`` = none) — printed in the header.
_ENV_PATH = _load_env()

from models import db  # noqa: E402
from models import folder as folder_model  # noqa: E402
from models.dossier import COLLECTION as DOSSIERS, VALID_STATUSES  # noqa: E402

#: {node key: node} of the registry (the engine's own table is private).
_NODES = {node.key: node for node in folder_model.DEFAULT_TREE}

def _fold(name: object) -> str:
    """The comparison form of a folder name — NFC, trimmed, case-folded:
    the engine's own equivalence (« Pièces » precomposed or decomposed is
    the same name)."""
    if not isinstance(name, str):
        return ""
    return unicodedata.normalize("NFC", name).strip().casefold()


#: A default folder's folded name → its node key. The homonym report reads
#: a name as ONE node, so the eighteen names must be distinct — checked at
#: import (raises, never asserts: ``python -O`` strips an assert).
_NODE_BY_NAME = {_fold(node.name): node.key for node in folder_model.DEFAULT_TREE}
if len(_NODE_BY_NAME) != len(folder_model.DEFAULT_TREE):
    raise RuntimeError("DEFAULT_TREE: two default folders share a name")

#: The planner's blocking reasons (``TreeReport.blocked``), in French.
_REASONS = {
    "depth": "profondeur maximale atteinte à son emplacement",
    "duplicate": "un dossier du même nom occupe sa place",
    "cycle": "une boucle de dossiers l'en empêche",
    "conflict": "le dossier trouvé porte déjà un autre rôle de l'application",
    "parent": "son dossier parent n'a pas pu être placé",
    "relocation_depth": (
        "ne peut être déplacé sous son dossier parent (profondeur maximale) "
        "— laissé où il est"
    ),
    "relocation_duplicate": (
        "ne peut être déplacé : un dossier du même nom existe déjà à la "
        "destination — laissé où il est"
    ),
    "relocation_cycle": "ne peut être déplacé sous lui-même — laissé où il est",
}

#: Labels of the four counts — what WOULD happen, then what did.
_LABELS = {
    False: ("à créer", "à adopter", "à déplacer", "bloqués"),
    True: ("créés", "adoptés", "déplacés", "bloqués"),
}
_COUNT_KEYS = ("created", "adopted", "relocated", "blocked")

#: The disclosure of one adoption / one move — the future in a simulation,
#: the present once written. ``{name}`` and the parent are DEFAULT folder
#: names (the registry's), never the stored ones. Every character printed
#: must exist in cp1252 (the lawyer's console, and the file its output is
#: kept in) — hence no arrow: « → » would print « ? ».
_ADOPTED = {
    False: "« {name} » existant sera le dossier de l'application "
           "(ne se renommera plus, ne se déplacera plus)",
    True: "« {name} » existant est désormais le dossier de l'application "
          "(ne se renomme plus, ne se déplace plus)",
}
_RELOCATED = {
    False: "« {name} » sera rangé {where}",
    True: "« {name} » est rangé {where}",
}

#: A written run whose report shows no write although the read made just
#: before it planned some: its commit raised and the answer was lost (the
#: engine then re-reads and answers success when nothing is left to write),
#: or another writer completed the tree in between. Either way the counts
#: may miss what was written — such a line never reads « (à jour) ».
_UNREPORTED = (
    "[?] la lecture faite juste avant annonçait des écritures que ce compte "
    "rendu ne montre pas (faites par cette exécution, sa réponse perdue, ou "
    "par un autre appel) : l'arborescence est complète, mais les comptes "
    "ci-dessus peuvent être incomplets — relancez une simulation pour vérifier"
)

#: The closing notes — printed at the end of every run that got that far.
_NOTE_RECORD = (
    "Note : ce compte rendu est la seule trace de cette exécution — les "
    "événements journalisés par un script n'atteignent pas Cloud Logging. "
    "Conservez-le."
)
_NOTE_RECREATE = (
    "Note : relancer --apply RECRÉE tout dossier de l'arborescence que le "
    "juriste a supprimé depuis, les dossiers ordinaires compris. Pour réparer "
    "un seul dossier : --dossier ID."
)
_NOTE_REUSED = (
    "Repris : dossiers ordinaires déjà présents sous le nom d'un dossier de "
    "l'arborescence, gardés tels quels — rien n'y est écrit."
)
_NO_DOSSIER_READ = "Aucun dossier lu — vérifiez le projet visé (GOOGLE_CLOUD_PROJECT)"


def _safe_console() -> None:
    """Never crash on the console's encoding (the lawyer's Windows console
    encodes cp1252): a line that cannot be encoded is written with a
    replacement character rather than raising — in ``--apply``, after a
    dossier was written and before the next."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        # A stream that cannot be reconfigured keeps its encoding — nothing to repair.
        pass


def _parse_statuts(raw: Optional[str]) -> tuple[Optional[set], list[str]]:
    """``(statuses, errors)`` — ``None`` = every dossier, whatever its
    status."""
    if raw is None:
        return None, []
    wanted = {unicodedata.normalize("NFC", s).strip().lower()
              for s in raw.split(",") if s.strip()}
    unknown = sorted(s for s in wanted if s not in VALID_STATUSES)
    if not wanted or unknown:
        return None, [
            "Statut inconnu : " + (", ".join(unknown) or "(aucun)")
            + ". Statuts valides : " + ", ".join(VALID_STATUSES) + "."
        ]
    return wanted, []


def _row(snap) -> dict:
    """What the script keeps of a dossier: its id, reference and status —
    never its title or its parties."""
    data = snap.to_dict() or {}
    return {
        "id": snap.id,
        "ref": str(data.get("file_number") or "").strip() or snap.id,
        "status": data.get("status") or "",
    }


def _read_dossiers(dossier_id: Optional[str]) -> list[dict]:
    """The dossiers to treat — a STRICT read: a failure propagates (the
    caller stops, never « nothing to do »). Materialized before any write.
    Raises ``LookupError`` for a ``--dossier`` that does not exist."""
    if dossier_id:
        snap = db.collection(DOSSIERS).document(dossier_id).get()
        if not snap.exists:
            raise LookupError(dossier_id)
        return [_row(snap)]
    return [_row(snap) for snap in db.collection(DOSSIERS).stream()]


def _system_ids(folders: list[dict], report) -> set:
    """The ids of the dossier's system folders: stamped with a role, or
    resolved by the plan for a system node — so a folder the plan ADOPTS is
    already one in a simulation."""
    ids = {f["id"] for f in folders if f.get("system_role")}
    for key, f in report.resolved.items():
        node = _NODES.get(key)
        if node is not None and node.role and isinstance(f, dict) and f.get("id"):
            ids.add(f["id"])
    return ids


def homonyms(folders: list[dict], report) -> list[dict]:
    """The folders bearing a default folder's name that the plan did NOT
    resolve as that folder — pure. ``[{"name", "root", "parent"}]``, sorted.

    A folder INSIDE a system folder's subtree (an ancestor is a system
    folder — a « Courriels » under « Reçus du portail », a
    « Correspondance » under « Projets ») is the lawyer's own sub-filing
    there: never listed. ``parent`` is the parent's name when that name is a
    default folder's name — generic by construction — else ``""`` (a
    sub-folder may be named after a client, and is never printed)."""
    rows = [f for f in folders if isinstance(f, dict) and f.get("id")]
    by_id = {f["id"]: f for f in rows}
    system = _system_ids(rows, report)
    resolved = {key: (f or {}).get("id") for key, f in report.resolved.items()}

    def in_system_subtree(folder: dict) -> bool:
        seen = {folder["id"]}
        parent_id = folder.get("parent_folder_id")
        while parent_id and parent_id not in seen:
            if parent_id in system:
                return True
            seen.add(parent_id)
            parent_id = (by_id.get(parent_id) or {}).get("parent_folder_id")
        return False

    out = []
    for f in rows:
        key = _NODE_BY_NAME.get(_fold(f.get("name")))
        if key is None or resolved.get(key) == f["id"] or in_system_subtree(f):
            continue
        parent = by_id.get(f.get("parent_folder_id") or "")
        parent_name = ""
        if parent is not None and _fold(parent.get("name")) in _NODE_BY_NAME:
            parent_name = str(parent.get("name") or "")
        out.append({"name": str(f.get("name") or ""),
                    "root": not f.get("parent_folder_id"),
                    "parent": parent_name})
    return sorted(out, key=lambda h: (h["name"], not h["root"], h["parent"]))


def _where(node) -> str:
    """Where *node* belongs — by its parent's DEFAULT name."""
    parent = _NODES.get(node.parent) if node.parent else None
    return f"sous « {parent.name} »" if parent is not None else "à la racine du dossier"


def _disclosures(result: dict, apply: bool) -> list[str]:
    """One line per adopted system folder and per move, in tree order, by
    the DEFAULT names."""
    lines = []
    for node in folder_model.DEFAULT_TREE:
        if node.key in result["adopted"]:
            lines.append(_ADOPTED[apply].format(name=node.name))
        if node.key in result["relocated"]:
            lines.append(_RELOCATED[apply].format(name=node.name, where=_where(node)))
    return lines


def _treat(dossier: dict, apply: bool) -> dict:
    """One dossier: ``{"counts", "pending", "unreported", "adopted",
    "relocated", "blocked", "homonyms", "errors"}``. ``pending`` is the
    report's own (``TreeReport.pending`` — its writes), never read off the
    counts: a folder REUSED by its name is counted and writes nothing."""
    try:
        folders = folder_model.list_dossier_folders(dossier["id"])
    except Exception:
        return {"errors": [folder_model.READ_ERROR]}
    planned = folder_model.plan_default_tree_from(
        dossier["id"], folders, relocate=True)
    if apply:
        report, errors = folder_model.ensure_default_tree(
            dossier["id"], relocate=True)
        if report is None:
            return {"errors": errors or [folder_model.CREATE_ERROR]}
    else:
        report = planned
    return {
        "counts": report.counts(),
        "pending": bool(report.pending),
        "unreported": bool(apply and planned.pending and not report.pending),
        "adopted": list(report.adopted),
        "relocated": list(report.relocated),
        "blocked": list(report.blocked),
        "homonyms": homonyms(folders, report),
        "errors": [],
    }


def _project() -> str:
    """The Firestore project the client reads (``""`` when unknown)."""
    project = getattr(db, "project", None)
    return project if isinstance(project, str) else ""


def main(argv: list[str]) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(
        description="Donne l'arborescence par défaut aux dossiers existants.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Simulation, rien n'est écrit (le défaut).")
    mode.add_argument("--apply", action="store_true",
                      help="Écrit l'arborescence (défaut : simulation).")
    parser.add_argument("--dossier", metavar="ID",
                        help="Ne traiter que ce dossier (son identifiant).")
    parser.add_argument("--statuts", metavar="S1,S2",
                        help="Ne traiter que ces statuts, séparés par des "
                             "virgules (défaut : tous).")
    args = parser.parse_args(argv)

    statuts, errors = _parse_statuts(args.statuts)
    if errors:
        print(f"[ECHEC] {errors[0]}")
        return 1

    project = _project()
    if project:
        print(f"Projet Firestore : {project}")
    if _ENV_PATH:
        print(f"Fichier .env lu : {_ENV_PATH} (il ne remplace aucune variable "
              "donnée en ligne)")

    try:
        dossiers = _read_dossiers((args.dossier or "").strip() or None)
    except LookupError:
        print("[ECHEC] Dossier introuvable — rien n'a été écrit.")
        return 1
    except Exception as exc:
        print(f"[ECHEC] Lecture des dossiers impossible "
              f"({type(exc).__name__}) — rien n'a été écrit.")
        return 1
    if not dossiers:
        # A practice has dossiers: reading none is what a wrong project (or
        # a wrong environment) reads. Unfiltered, that is a failure; with
        # --statuts it is said, and the run goes on (exit 0). (--dossier
        # never gets here: an absent one is « Dossier introuvable ».)
        if statuts is None:
            print(f"[ECHEC] {_NO_DOSSIER_READ} — rien n'a été écrit.")
            return 1
        print(f"[!] {_NO_DOSSIER_READ}.")
    if statuts is not None:
        dossiers = [d for d in dossiers if d["status"] in statuts]
    dossiers.sort(key=lambda d: (d["ref"], d["id"]))

    apply = bool(args.apply)
    print("ÉCRITURE" if apply else
          "SIMULATION — ce que --apply ferait ; rien n'est écrit")
    labels = _LABELS[apply]
    totals = {key: 0 for key in (*_COUNT_KEYS, "reused")}
    treated = failed = homonym_count = 0

    for dossier in dossiers:
        result = _treat(dossier, apply)
        if result["errors"]:
            failed += 1
            print(f"  [ECHEC] {dossier['ref']} : {' '.join(result['errors'])}")
            continue
        treated += 1
        counts = result["counts"]
        for key in totals:
            totals[key] += counts.get(key, 0)
        line = " · ".join(
            f"{label} {counts[key]}" for label, key in zip(labels, _COUNT_KEYS))
        # « (à jour) » = nothing to write (the REPORT's writes — never the
        # counts, which count a folder reused by its name), nothing
        # blocked, and no write the read just before announced gone
        # missing from the report.
        up_to_date = not (result["pending"] or counts["blocked"]
                          or result["unreported"])
        reused = counts.get("reused", 0)
        print(f"  {dossier['ref']} : {line}"
              + (" (à jour)" if up_to_date else "")
              + (f" — repris : {reused}" if reused else ""))
        for text in _disclosures(result, apply):
            print(f"      {text}")
        for key, reason in result["blocked"]:
            node = _NODES.get(key)
            name = node.name if node is not None else key
            print(f"      [!] bloqué : « {name} » — {_REASONS.get(reason, reason)}")
        if result["unreported"]:
            print(f"      {_UNREPORTED}")
        for h in result["homonyms"]:
            homonym_count += 1
            if h["root"]:
                where = "à la racine"
            elif h["parent"]:
                where = f"dans « {h['parent']} »"
            else:
                where = "dans un sous-dossier"
            print(f"      à vérifier : « {h['name']} » ({where}) porte le nom "
                  "d'un dossier de l'arborescence sans en être un")

    print()
    print(f"Dossiers traités : {treated} · en échec : {failed} · "
          + " · ".join(
              f"{label} {totals[key]}" for label, key in zip(labels, _COUNT_KEYS))
          + f" · repris : {totals['reused']}"
          + f" · homonymes à vérifier : {homonym_count}")
    if not dossiers:
        print("Aucun dossier ne correspond à la sélection.")
    if totals["reused"]:
        print(_NOTE_REUSED)
    if not apply:
        print("SIMULATION — rien n'a été écrit. Relancez avec --apply pour écrire.")
    print(_NOTE_RECORD)
    print(_NOTE_RECREATE)
    return 1 if failed or totals["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
