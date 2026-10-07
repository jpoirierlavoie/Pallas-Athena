"""Rectify, OUTSIDE the models, a few non-money fields of register entries.

The trust register is append-only, and an administration entry locks once
its period is reconciled: nothing in the application corrects a payee typed
wrong at transcription, or a date the Firestore console left with a time of
day (the console edits in LOCAL time, so a correct midnight-UTC date shows
there as 8 PM the evening before, and « correcting » it stores the evening
hour). This script does it for the fields listed below, the way the house
rule asks of a repair that writes around the models (CLAUDE.md, « One door
per rule »):

* it reads a PLAN — a JSON file kept outside the public repository, since it
  names entries and the lawyer's reasons — where every change states the
  stored value it expects (``avant``) and the one to write (``apres``);
* by default it only PRINTS what it would write; ``--appliquer`` writes;
* every document it writes gets the provenance stamps (``updated_at``, a new
  ``etag``, ``updated_via = "script"``) and one ``revisions`` trail entry —
  ``{at, via, motif, changes: {field: [before, after]}, updated_at_before}``,
  the shape the administration ledger already keeps (capped like it).
  ``updated_at_before`` is what check 6 of ``scripts/verify_trust_integrity``
  reads, on a trust entry, as the instant it was cleared;
* every document is written in ONE batch, each guarded by the update time it
  was read at: a document that changed since the read fails the whole
  batch, and nothing is written. A commit whose answer is lost is re-read and
  reported written, not written, or uncertain;
* no money field, status, sequence or link is reachable, and no DAV
  collection is touched (the registers are not DAV-exposed: no CTag).

What it can change — anything else refuses the whole plan:

* trust (``fideicommis``): ``counterparty`` — on a fee payment, only to a
  name of the firm profile, stored as the profile spells it (D23, art. 58:
  the rule binds a repair too; the profile is read strictly);
  ``invoice_external_ref`` on a fee payment that links no Athéna invoice;
  ``date`` and ``cleared_date``, only to midnight UTC of the SAME UTC day —
  a normalization: the frozen balances, the backdating guard and every
  reconciliation rest on the day, never on the hour; ``purpose``, only from
  « Paiement d'honoraires » to « Déboursé à un tiers » — a withdrawal that
  paid a third party (a colleague's fees billed to the client as a
  disbursement), recorded as the lawyer's own fees. Only on a déboursé that
  links no Athéna invoice, is neither reversed nor annulled, and that NO
  administration entry is linked to (read strictly): a linked recette means
  the money did reach the firm, and reclassifying it would orphan that
  recette. The invoice reference is kept — the entry page shows it as
  « Facture (externe) », the invoice the payment settled — although the
  create path drops it on that objet; nothing reads it there as a rule;
* administration (``administration``): ``date`` and ``cleared_date`` under
  the same rule; ``created_at``, to an instant no later than the entry's own
  ``updated_at`` — to put back the creation instant a console edit replaced
  (the change's ``motif`` names where the true value came from);
  ``reference``, the bank reference a transcription left out or mistyped —
  never on a card payment, whose two legs carry it;
  ``cleared_date`` may also move to ANOTHER day — a statement date
  transcribed wrong (a year, the day before) — on a ``compensée`` entry, not
  before the entry's own day, not after today, and only when no
  reconciliation of its account, completed or draft, would see the entry
  otherwise: one whose period end falls on or after the entry's day and in
  ``[min(old, new), max(old, new))`` counted it cleared on one side of that
  end and outstanding on the other (the as-of rule of
  ``admin_ledger._list_cleared_after``). The reconciliations are read
  strictly: a failed read stops the plan.

The plan::

    {"motif": "Rectification approuvée par l'avocat le 2026-10-06.",
     "changements": [
       {"registre": "fideicommis", "ecriture": "<id>", "champ": "counterparty",
        "avant": "Mme X", "apres": "Me Y", "motif": "…"},
       {"registre": "administration", "ecriture": "<id>", "champ": "date",
        "avant": "2026-08-31T02:00:00+00:00", "apres": "2026-08-31"}
     ]}

A date's ``avant`` is the stored instant (ISO 8601 with its offset) and its
``apres`` a day (``AAAA-MM-JJ``); ``created_at`` takes instants on both
sides. A change whose ``apres`` is already stored is reported « déjà fait »
and skipped, so a plan can be run again once applied.

Run, from ``athena/``:

    python -m scripts.rectify_registers PLAN.json               # prints
    python -m scripts.rectify_registers PLAN.json --appliquer   # writes

Afterwards, run both integrity scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

from models import admin_ledger, db, fee_payment, provenance, trust
from models import settings as settings_model
from security import sanitize
from utils.deadlines import today_mtl
from utils.format_fr import format_cents_fr

REGISTERS = {
    "fideicommis": trust.TRANSACTIONS_COLLECTION,
    "administration": admin_ledger.TRANSACTIONS_COLLECTION,
}
FIELDS = {
    "fideicommis": ("counterparty", "invoice_external_ref", "date", "cleared_date", "purpose"),
    "administration": ("date", "cleared_date", "created_at", "reference"),
}
#: The one reclassification the tool performs (see the module docstring).
RECLASSIFY_FROM = trust.FEE_PAYMENT_PURPOSE
RECLASSIFY_TO = "déboursé_tiers"
_DAY_FIELDS = ("date", "cleared_date")
_TEXT_MAX = 2000
_REF_MAX = 200


class PlanRefused(Exception):
    """The plan cannot be applied as written; nothing is written."""


@dataclass
class Change:
    register: str
    entry_id: str
    field: str
    before: Any          # the stored value, as read
    after: Any           # the value to write
    motif: str
    done: bool = False   # ``after`` is already stored


# ── values ────────────────────────────────────────────────────────────────


def _instant(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _parse_instant(text) -> Optional[datetime]:
    """An ISO 8601 instant WITH its offset; ``None`` for anything else."""
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _parse_day(text) -> Optional[datetime]:
    """``AAAA-MM-JJ`` as midnight UTC (Architecture Rule 5)."""
    if not isinstance(text, str) or len(text) != 10:
        return None
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _same(stored, expected) -> bool:
    """Does the plan's ``avant`` describe the stored value?"""
    if expected is None or stored is None:
        return stored is None and expected is None
    stored_instant = _instant(stored)
    if stored_instant is not None:
        return stored_instant == _parse_instant(expected)
    return stored == expected


def _show(value, *, day: bool = True) -> str:
    """A value as the plan's reader should see it: a date-only field at
    midnight as its day, any other instant to the microsecond."""
    instant = _instant(value)
    if instant is not None:
        if day and (instant.hour, instant.minute, instant.second, instant.microsecond) == (0, 0, 0, 0):
            return instant.date().isoformat()
        return instant.strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    if value is None:
        return "(vide)"
    return f"« {value} »"


def _addressable(entry_id) -> bool:
    return (isinstance(entry_id, str) and 0 < len(entry_id) <= 1500
            and "/" not in entry_id and entry_id not in (".", "..")
            and not (entry_id.startswith("__") and entry_id.endswith("__")))


# ── the plan ──────────────────────────────────────────────────────────────


def load_plan(path: str) -> tuple[str, list[dict]]:
    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PlanRefused(f"plan illisible ({type(exc).__name__}) : {path}") from None
    if not isinstance(plan, dict):
        raise PlanRefused("plan mal formé : un objet { motif, changements } est attendu")
    motif = plan.get("motif")
    items = plan.get("changements")
    if not isinstance(motif, str) or not motif.strip():
        raise PlanRefused("plan sans « motif » général")
    if not isinstance(items, list) or not items:
        raise PlanRefused("plan sans « changements »")
    return motif.strip(), items


def _check_text(value, limit: int, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanRefused(f"{what} : une valeur non vide est attendue")
    value = value.strip()
    if len(value) > limit:
        raise PlanRefused(f"{what} : au plus {limit} caractères")
    if sanitize(value, max_length=limit) != value:
        raise PlanRefused(f"{what} : « {value} » serait altéré à l'enregistrement — refusé")
    return value


def _check_reclassification(doc: dict, entry_id: str, apres, where: str) -> None:
    """« Paiement d'honoraires » → « Déboursé à un tiers », and nothing else."""
    if doc.get("purpose") != RECLASSIFY_FROM or apres != RECLASSIFY_TO:
        raise PlanRefused(
            f"{where} : seul un « {trust.PURPOSE_LABELS[RECLASSIFY_FROM]} » se reclasse "
            f"ici, et seulement en « {trust.PURPOSE_LABELS[RECLASSIFY_TO]} »")
    if doc.get("direction") != trust.PURPOSE_DIRECTIONS[RECLASSIFY_TO]:
        raise PlanRefused(f"{where} : un « {trust.PURPOSE_LABELS[RECLASSIFY_TO]} » est un déboursé")
    if doc.get("invoice_id"):
        raise PlanRefused(f"{where} : l'écriture paie une facture d'Athéna — ce paiement "
                          f"est celui des honoraires de l'avocat")
    if doc.get("reversed_by_id") or doc.get("reverses_id") or doc.get("status") == "annulée":
        raise PlanRefused(f"{where} : une écriture contre-passée ou annulée ne se reclasse pas")
    linked = admin_ledger.list_by_trust_transaction(entry_id)
    if linked:
        seqs = ", ".join(str(r.get("sequence")) for r in linked)
        raise PlanRefused(
            f"{where} : une recette du registre d'administration y est liée (n° {seqs}) — "
            f"l'argent est allé au cabinet : ce n'était pas un déboursé à un tiers")


def _check_clearing_move(doc: dict, old: date, new: date, where: str) -> None:
    """An administration entry's clearing date moved to another day, and
    nothing a reconciliation saw changes with it (module docstring)."""
    if doc.get("status") != "compensée":
        raise PlanRefused(f"{where} : seule une écriture compensée change de jour de "
                          f"compensation")
    entry_day = _instant(doc.get("date"))
    if entry_day is None:
        raise PlanRefused(f"{where} : la date de l'écriture est illisible")
    entry_day = entry_day.date()
    if new < entry_day:
        raise PlanRefused(f"{where} : une compensation ne précède pas l'écriture, "
                          f"datée du {entry_day}")
    if new > today_mtl():
        raise PlanRefused(f"{where} : {new} est dans le futur")
    account_id = doc.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise PlanRefused(f"{where} : l'écriture ne nomme aucun compte")
    low, high = min(old, new), max(old, new)
    for rec in admin_ledger.list_reconciliations(account_id):
        end = _instant(rec.get("period_end"))
        if end is None:
            raise PlanRefused(f"{where} : la conciliation {rec.get('id')} n'a pas de fin "
                              f"de période lisible")
        if entry_day <= end.date() and low <= end.date() < high:
            raise PlanRefused(
                f"{where} : la conciliation ({rec.get('status')}) au {end.date()} a vu "
                f"l'écriture {'compensée' if old <= end.date() else 'en circulation'} — "
                f"compensée le {new}, elle l'aurait vue autrement")


def resolve(items: list[dict], plan_motif: str) -> tuple[list[Change], dict]:
    """Every change judged against the stored document, read strictly.

    Returns the changes and the documents read (``(register, id)`` →
    snapshot). The first refusal raises :class:`PlanRefused`."""
    seen: set = set()
    snaps: dict = {}
    changes: list[Change] = []
    payees: Optional[list[str]] = None
    for i, item in enumerate(items):
        where = f"changement n° {i + 1}"
        if not isinstance(item, dict):
            raise PlanRefused(f"{where} : un objet est attendu")
        register = item.get("registre")
        entry_id = item.get("ecriture")
        field = item.get("champ")
        if register not in REGISTERS:
            raise PlanRefused(f"{where} : registre « {register} » inconnu "
                              f"(fideicommis ou administration)")
        if not _addressable(entry_id):
            raise PlanRefused(f"{where} : identifiant d'écriture invalide")
        if field not in FIELDS[register]:
            raise PlanRefused(
                f"{where} : le champ « {field} » ne se rectifie pas ici "
                f"({register} : {', '.join(FIELDS[register])})")
        if (register, entry_id, field) in seen:
            raise PlanRefused(f"{where} : {register}/{entry_id}/{field} figure deux fois")
        seen.add((register, entry_id, field))
        where = f"{where} ({register} {entry_id}, {field})"
        motif = item.get("motif")
        motif = motif.strip() if isinstance(motif, str) and motif.strip() else plan_motif

        key = (register, entry_id)
        if key not in snaps:
            snap = db.collection(REGISTERS[register]).document(entry_id).get()
            if not snap.exists:
                raise PlanRefused(f"{where} : écriture introuvable")
            snaps[key] = snap
        doc = snaps[key].to_dict() or {}
        stored = doc.get(field)
        apres = item.get("apres")

        if field in _DAY_FIELDS:
            after = _parse_day(apres)
            if after is None:
                raise PlanRefused(f"{where} : « apres » doit être un jour AAAA-MM-JJ")
            stored_instant = _instant(stored)
            if stored_instant is None:
                raise PlanRefused(f"{where} : aucune date n'est inscrite à rectifier")
            if stored_instant.date() != after.date():
                if (register, field) != ("administration", "cleared_date"):
                    raise PlanRefused(
                        f"{where} : {after.date()} n'est pas le jour inscrit "
                        f"({stored_instant.date()}) — seule l'heure se rectifie ici, "
                        f"jamais le jour")
                _check_clearing_move(doc, stored_instant.date(), after.date(), where)
        elif field == "created_at":
            after = _parse_instant(apres)
            if after is None:
                raise PlanRefused(f"{where} : « apres » doit être un instant ISO 8601 "
                                  f"avec son fuseau")
            updated = _instant(doc.get("updated_at"))
            if updated is None or after > updated:
                raise PlanRefused(f"{where} : une création ne peut pas suivre la "
                                  f"dernière modification de l'écriture")
        elif field == "counterparty":
            after = _check_text(apres, _TEXT_MAX, where)
            if doc.get("purpose") == trust.FEE_PAYMENT_PURPOSE:
                if payees is None:
                    try:
                        payees = fee_payment.guard_fee_payees()
                    except settings_model.CabinetIllisible:
                        raise PlanRefused("profil du cabinet illisible — le "
                                          "bénéficiaire ne peut pas être vérifié") from None
                canonical = fee_payment.match_fee_payee(after, payees)
                if canonical is None:
                    raise PlanRefused(
                        f"{where} : un paiement d'honoraires a pour bénéficiaire "
                        f"l'avocat ou son cabinet, tels que les nomme le profil du "
                        f"cabinet ({', '.join(payees) or 'aucun nom'}) — D23, art. 58")
                after = canonical
        elif field == "purpose":
            after = apres
            if stored != after:
                _check_reclassification(doc, entry_id, apres, where)
        elif field == "reference":
            after = _check_text(apres, _REF_MAX, where)
            if doc.get("kind") == "paiement_carte":
                raise PlanRefused(f"{where} : un paiement de carte porte sa référence sur "
                                  f"ses deux volets — elle ne se rectifie pas d'un seul côté")
        else:  # invoice_external_ref
            after = _check_text(apres, _REF_MAX, where)
            if doc.get("purpose") != trust.FEE_PAYMENT_PURPOSE or doc.get("invoice_id"):
                raise PlanRefused(
                    f"{where} : seule la référence d'un paiement d'honoraires qui ne "
                    f"lie aucune facture d'Athéna se rectifie")

        change = Change(register, entry_id, field, stored, after, motif)
        if _same(stored, after if isinstance(after, str) else after.isoformat()):
            change.done = True
        elif not _same(stored, item.get("avant")):
            raise PlanRefused(
                f"{where} : la valeur inscrite ({_show(stored)}) n'est pas celle "
                f"que le plan attend ({_show(item.get('avant'))}) — l'écriture a "
                f"changé depuis la préparation du plan")
        changes.append(change)
    return changes, snaps


# ── output ────────────────────────────────────────────────────────────────


def _label(register: str, doc: dict) -> str:
    day = _instant(doc.get("date"))
    parts = [
        "fidéicommis" if register == "fideicommis" else "administration",
        f"seq {doc.get('sequence')}",
        day.date().isoformat() if day else "?",
        format_cents_fr(int(doc.get("amount") or 0)),
    ]
    if register == "fideicommis":
        parts.append(trust.PURPOSE_LABELS.get(doc.get("purpose"), doc.get("purpose") or ""))
        if doc.get("dossier_file_number"):
            parts.append(f"dossier {doc['dossier_file_number']}")
    else:
        parts.append(doc.get("kind") or "")
    return " · ".join(p for p in parts if p)


def describe(changes: list[Change], snaps: dict) -> None:
    by_doc: dict = {}
    for change in changes:
        by_doc.setdefault((change.register, change.entry_id), []).append(change)
    for (register, entry_id), group in by_doc.items():
        doc = snaps[(register, entry_id)].to_dict() or {}
        print(f"\n{_label(register, doc)}  (écriture {entry_id})")
        for change in group:
            day = change.field in _DAY_FIELDS
            state = ("déjà fait" if change.done else
                     f"{_show(change.before, day=day)} → {_show(change.after, day=day)}")
            print(f"   {change.field} : {state}")
            if not change.done:
                print(f"      motif : {change.motif}")
        if any(not c.done for c in group):
            print("   + trace dans « revisions », updated_at, etag, updated_via = script")


# ── the write ─────────────────────────────────────────────────────────────


def _payloads(changes: list[Change], snaps: dict, now: datetime) -> dict:
    by_doc: dict = {}
    for change in changes:
        if not change.done:
            by_doc.setdefault((change.register, change.entry_id), []).append(change)
    payloads: dict = {}
    for key, group in by_doc.items():
        doc = snaps[key].to_dict() or {}
        motifs = list(dict.fromkeys(c.motif for c in group))
        trail = {
            "at": now,
            "via": "script",
            "motif": " ; ".join(motifs),
            "changes": {c.field: [c.before, c.after] for c in group},
            "updated_at_before": doc.get("updated_at"),
        }
        revisions = list(doc.get("revisions") or []) + [trail]
        payload = {c.field: c.after for c in group}
        payload["revisions"] = revisions[-admin_ledger._REVISIONS_CAP:]
        with provenance.writing_via("script"):
            payload.update(provenance.update_fields(now))
        payloads[key] = payload
    return payloads


def _landed(key, payload: dict) -> Optional[bool]:
    """Re-read one document after a commit that raised: True when the write
    is there, False when it is not, None when the read fails too."""
    try:
        doc = db.collection(REGISTERS[key[0]]).document(key[1]).get().to_dict() or {}
    except Exception:
        return None
    return doc.get("etag") == payload["etag"]


def apply(changes: list[Change], snaps: dict) -> int:
    now = datetime.now(timezone.utc)
    payloads = _payloads(changes, snaps, now)
    if not payloads:
        print("\nRien à écrire : chaque changement du plan est déjà fait.")
        return 0
    batch = db.batch()
    for key, payload in payloads.items():
        ref = db.collection(REGISTERS[key[0]]).document(key[1])
        batch.update(ref, payload,
                     option=db.write_option(last_update_time=snaps[key].update_time))
    try:
        batch.commit()
    except Exception as exc:
        landed = {key: _landed(key, payload) for key, payload in payloads.items()}
        if all(v is True for v in landed.values()):
            print(f"\nÉcrit ({len(payloads)} écriture(s)) — la réponse du serveur "
                  f"s'était perdue ({type(exc).__name__}), la relecture le confirme.")
            return 0
        if all(v is False for v in landed.values()):
            print(f"\nRien n'a été écrit ({type(exc).__name__}) : une écriture a "
                  f"changé depuis la lecture, ou le serveur a refusé. Relancez à "
                  f"blanc pour voir l'état actuel.")
            return 1
        print(f"\nIssue INCERTAINE ({type(exc).__name__}) : la relecture n'a pas "
              f"pu trancher. Relancez à blanc avant toute autre chose.")
        return 1
    print(f"\nÉcrit : {len(payloads)} écriture(s), en un seul lot. Relancez les deux "
          f"contrôles d'intégrité (scripts.verify_trust_integrity, "
          f"scripts.verify_admin_integrity).")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.rectify_registers")
    parser.add_argument("plan", help="le plan JSON (hors du dépôt public)")
    parser.add_argument("--appliquer", action="store_true",
                        help="écrire (par défaut : afficher seulement)")
    args = parser.parse_args(argv if argv is not None else [])
    try:
        motif, items = load_plan(args.plan)
        changes, snaps = resolve(items, motif)
    except PlanRefused as exc:
        print(f"Plan refusé — rien n'a été écrit : {exc}")
        return 1
    print(f"Plan : {motif}")
    describe(changes, snaps)
    pending = sum(1 for c in changes if not c.done)
    print(f"\n{pending} changement(s) à écrire, {len(changes) - pending} déjà fait(s).")
    if not args.appliquer:
        print("À blanc : rien n'a été écrit. Relancez avec --appliquer pour écrire.")
        return 0
    return apply(changes, snaps)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
