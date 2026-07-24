"""One-shot normalization of the three refonded vocabularies (spec §7.2).

The read-time migrations (models.hearing._migrate_hearing,
models.note._migrate_category, models.document._migrate_category) relabel on
read but never REWRITE storage — so the DAV CTag never moves and jtx Board
keeps its old tiles (« Appel », « Procès », « Correspondance »…) until each
resource is next edited. This script rewrites the stored keys once and bumps
the affected CTags so DavX5 re-syncs.

Behaviour:
- ``--dry-run`` (DEFAULT): reads only, prints a per-transition count, writes
  nothing.
- ``--apply``: rewrites hearing_type / category on affected docs, sets
  modalite="présentiel" on any hearing missing it, regenerates each touched
  doc's etag, and bumps the affected DAV CTags.
- Idempotent: a second run finds nothing to change (the tables only map
  removed keys, which no longer exist once rewritten).
- Never logs note/hearing content — only ids and transition counts.

CTag routing note: contrary to the spec's literal ``bump_ctag("hearings")``,
a dossier-linked hearing/note lives in the ``dossier:{id}`` collection (the
July 2026 split), a dossier-less one in « Général ». We bump the collection
``dav.sync.collection_for(dossier_id)`` returns for each changed hearing/note
— the single routing rule the whole app uses. Documents are not DAV-exposed:
no bump.

    python -m scripts.migrate_vocabulaires             # dry-run
    python -m scripts.migrate_vocabulaires --apply     # write
"""

import argparse
import sys
import uuid
from collections import Counter

from models import db
from models.hearing import _HEARING_TYPE_MIGRATION
from models.note import _CATEGORY_MIGRATION as _NOTE_MIGRATION
from models.document import _CATEGORY_MIGRATION as _DOC_MIGRATION
from dav.sync import bump_ctag, collection_for


def _migrate_hearings(apply: bool) -> tuple[Counter, set[str]]:
    """Rewrite removed hearing_type keys + default modalite. Returns the
    transition counter and the set of DAV collections to bump."""
    counts: Counter = Counter()
    collections: set[str] = set()
    for snap in db.collection("hearings").stream():
        doc = snap.to_dict()
        update: dict = {}
        old = doc.get("hearing_type", "")
        if old in _HEARING_TYPE_MIGRATION:
            new = _HEARING_TYPE_MIGRATION[old]
            update["hearing_type"] = new
            counts[f"hearing_type {old} → {new}"] += 1
        if "modalite" not in doc:
            update["modalite"] = "présentiel"
            counts["modalite (absent) → présentiel"] += 1
        if not update:
            continue
        collections.add(collection_for(doc.get("dossier_id") or ""))
        if apply:
            update["etag"] = str(uuid.uuid4())
            db.collection("hearings").document(snap.id).update(update)
    return counts, collections


def _migrate_notes(apply: bool) -> tuple[Counter, set[str]]:
    counts: Counter = Counter()
    collections: set[str] = set()
    for snap in db.collection("notes").stream():
        doc = snap.to_dict()
        old = doc.get("category", "")
        if old not in _NOTE_MIGRATION:
            continue
        new = _NOTE_MIGRATION[old]
        counts[f"note category {old} → {new}"] += 1
        collections.add(collection_for(doc.get("dossier_id") or ""))
        if apply:
            db.collection("notes").document(snap.id).update({
                "category": new, "etag": str(uuid.uuid4()),
            })
    return counts, collections


def _migrate_documents(apply: bool) -> Counter:
    counts: Counter = Counter()
    for snap in db.collection("documents").stream():
        doc = snap.to_dict()
        old = doc.get("category", "")
        if old not in _DOC_MIGRATION:
            continue
        new = _DOC_MIGRATION[old]
        counts[f"document category {old} → {new}"] += 1
        if apply:
            db.collection("documents").document(snap.id).update({
                "category": new, "etag": str(uuid.uuid4()),
            })
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize the refonded vocabularies (spec §7.2).")
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the changes (default is a read-only dry-run).",
    )
    args = parser.parse_args()
    apply = args.apply

    # The transition labels contain « → » (U+2192); a Windows cp1252 console
    # would raise UnicodeEncodeError on print. Force UTF-8 output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    mode = "APPLY (writing)" if apply else "DRY-RUN (no writes)"
    print(f"Migration des vocabulaires — {mode}\n")

    h_counts, h_cols = _migrate_hearings(apply)
    n_counts, n_cols = _migrate_notes(apply)
    d_counts = _migrate_documents(apply)

    total = Counter()
    total.update(h_counts)
    total.update(n_counts)
    total.update(d_counts)

    if not total:
        print("Aucun enregistrement à migrer. (Idempotent : rien à faire.)")
        return 0

    print("Transitions :")
    for transition, n in sorted(total.items()):
        print(f"  {transition} : {n}")

    to_bump = h_cols | n_cols
    print(f"\nCollections DAV à rafraîchir (CTag) : {len(to_bump)}")
    if apply:
        for name in sorted(to_bump):
            bump_ctag(name)
        print("CTags rafraîchis. DavX5 resynchronisera au prochain sondage.")
    else:
        print("(dry-run — aucun CTag bumpé, aucune écriture)")
        print("\nRelancer avec --apply pour écrire, après validation du décompte.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
