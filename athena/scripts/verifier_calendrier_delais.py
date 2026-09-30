"""Read-only report: stored dates the pre-2026-09-30 holiday calendar left.

The calendar fix of 2026-09-30 (art. 82 C.p.c. — 26 December and 2 January
are holidays in civil procedure every year; art. 61(23) L.i. — no « Sunday →
Monday » substitute except 2 July) changes what ``utils.deadlines``
COMPUTES. It recomputes nothing already stored:

- a dossier's ``prescription_date`` is re-derived by
  ``models.dossier._apply_prescription_deadline`` only on the dossier's NEXT
  save. A date the old calendar computed onto the Monday after a Sunday 24
  June, 1 January or 25 December (next: 25-26 June 2029, 26-27 Dec 2033,
  2-3 Jan 2034) is stored one day LATE — the unsafe direction for the
  plaintiff — until then;
- a protocol step's ``deadline_date`` moves only through
  ``recompute_deadlines`` on a start-date change. A template step stored on
  a weekday 26 Dec / 2 Jan is shown EARLIER than art. 83 allows (safe), one
  stored on a 26 June a Sunday 24 June pushed it to is shown one day LATE.

This script LISTS both, so the lawyer can re-save the few dossiers and look
at the few steps. It writes NOTHING — there is no ``--apply``, by design:
re-saving a dossier through the application is the correction, and it goes
through the model's guards and CTag discipline like any other edit.

Output: ids, file numbers and dates only — never a title (a title names the
parties).

    python -m scripts.verifier_calendrier_delais
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from typing import Optional


def _date_key(value) -> Optional[date]:
    """The calendar date a date-only field stands for (midnight UTC)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    return None


def prescription_drift(doc: dict) -> Optional[tuple[Optional[date], Optional[date]]]:
    """``(stored, recomputed)`` when a save would move the dossier's
    ``prescription_date`` today, else ``None``. Pure — works on a copy,
    through the SAME migration + derivation a save runs."""
    from models import dossier as dossier_model

    candidate = dossier_model._migrate_parties(dict(doc))
    stored = _date_key(candidate.get("prescription_date"))
    dossier_model._apply_prescription_deadline(candidate)
    recomputed = _date_key(candidate.get("prescription_date"))
    return None if stored == recomputed else (stored, recomputed)


def step_drift(protocol: dict, step: dict) -> Optional[tuple[date, date]]:
    """``(stored, recomputed)`` when a not-completed TEMPLATE step still
    carries the date the RETIRED calendar computed and the current one
    computes another — i.e. the drift is the calendar's, not the lawyer's
    (a hand-moved date is never reported). Pure."""
    from models import protocol as protocol_model

    offset = step.get("deadline_offset_days")
    start = protocol.get("start_date")
    if offset is None or not isinstance(start, datetime):
        return None
    if step.get("status") == "complété":
        return None
    stored = _date_key(step.get("deadline_date"))
    old = _date_key(protocol_model._legacy_compute_deadline(start, offset))
    new = _date_key(protocol_model._compute_deadline(start, offset))
    if stored == old and old != new:
        return stored, new
    return None


def main(argv: list[str]) -> int:
    if argv:
        print("Ce script ne prend aucun argument : il ne fait que lire.")
        return 2
    from models import db

    print("Dossiers dont la date pour agir bougerait au prochain "
          "enregistrement :")
    moved = 0
    for snap in db.collection("dossiers").stream():
        doc = snap.to_dict() or {}
        drift = prescription_drift(doc)
        if drift:
            moved += 1
            stored, new = drift
            print(f"  {doc.get('file_number') or '(sans n°)'} [{snap.id}] "
                  f"{doc.get('status', '')} : {stored} -> {new}")
    if not moved:
        print("  aucun.")

    print("\nÉtapes de protocole portant encore la date de l'ancien "
          "calendrier :")
    steps = 0
    for psnap in db.collection("protocols").stream():
        protocol = psnap.to_dict() or {}
        if protocol.get("status") != "actif":
            continue
        for ssnap in psnap.reference.collection("steps").stream():
            drift = step_drift(protocol, ssnap.to_dict() or {})
            if drift:
                steps += 1
                stored, new = drift
                print(f"  {protocol.get('dossier_file_number') or '(sans n°)'}"
                      f" protocole {psnap.id} étape {ssnap.id} : "
                      f"affichée {stored}, calculée aujourd'hui {new}")
    if not steps:
        print("  aucune.")

    print(f"\nLECTURE SEULE (rien n'a été écrit) : {moved} dossier(s), "
          f"{steps} étape(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
