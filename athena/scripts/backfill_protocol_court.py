"""One-shot backfill of the protocol `court` field (PA-D03 copy bug).

Every protocol ever created carries `court: ""` — the creation route copied
``dossier.get("court")``, a key that has never existed on dossiers (the
field is `tribunal`). The route now copies `tribunal`, but existing docs
never self-heal: `update_protocol` full-sets the merged existing doc, so an
in-app edit preserves the empty string forever.

Behaviour:
- ``--dry-run`` (DEFAULT): reads only, prints what would change.
- ``--apply``: writes `court` from the dossier's current `tribunal` on every
  protocol whose `court` is empty AND whose dossier has one; regenerates the
  touched doc's etag. A protocol whose `court` is already non-empty is never
  touched (nothing today writes one, but the guard costs nothing).
- Idempotent; protocols are not DAV-exposed — no CTag to bump.
- Logs ids and counts only.

    python -m scripts.backfill_protocol_court             # dry-run
    python -m scripts.backfill_protocol_court --apply     # write
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone

from models import db


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write the backfill (default: dry-run).")
    args = parser.parse_args(argv)

    dossiers: dict[str, str] = {}
    for snap in db.collection("dossiers").stream():
        d = snap.to_dict() or {}
        dossiers[snap.id] = (d.get("tribunal") or "").strip()

    changed = skipped_has_court = skipped_no_tribunal = 0
    now = datetime.now(timezone.utc)
    for snap in db.collection("protocols").stream():
        p = snap.to_dict() or {}
        if (p.get("court") or "").strip():
            skipped_has_court += 1
            continue
        tribunal = dossiers.get(p.get("dossier_id", ""), "")
        if not tribunal:
            skipped_no_tribunal += 1
            continue
        changed += 1
        print(f"  {snap.id}: court '' -> {tribunal!r}")
        if args.apply:
            snap.reference.update({
                "court": tribunal,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })

    mode = "APPLIED" if args.apply else "DRY-RUN (nothing written)"
    print(f"\n{mode}: {changed} protocole(s) rempli(s), "
          f"{skipped_has_court} déjà renseigné(s), "
          f"{skipped_no_tribunal} sans tribunal côté dossier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
