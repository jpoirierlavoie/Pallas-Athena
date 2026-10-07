"""Reclassify the administration journal (verification of 2026-10-07) — a
one-shot migration that writes OUTSIDE the models.

What it does
------------
The lawyer's verification of the operations account found his own drawings
and contributions, and the firm's transfers to and from accounts outside this
ledger (the trust account, a savings account), booked as « dépense » or
« autre recette »: every results figure counted them. The model now has four
natures for them (``prélèvement``, ``apport``, ``virement_interne_sortant``,
``virement_interne_entrant`` — ``models/admin_ledger.NON_RESULT_KINDS``). A
CSV the lawyer approved, kept OUTSIDE the public repository, names every
entry to change, with the stored values it expects (``*_actuel``) and the
targets (``*_cible``): natures, and a few categories and dossiers (group A),
payees (group C) and two days (group D — one entry, and one card payment
with its card leg). This script applies it.

Why it writes outside the models
--------------------------------
CLAUDE.md, « One door per rule »: rules and writes live in ``models/``. This
script is a declared exception. Most of the entries are LOCKED in the
model's sense — reconciled, compensée, or a card-payment leg — and
``admin_ledger.update_transaction`` refuses every one of them
(``_entry_lock_reason``), as it must: no edit reaches behind a closed proof.
What the migration changes lies outside what the lock protects. It never
writes an amount, a sign, an account, a status, a clearing date, a sequence,
a tax split or a link (invoice, trust entry, reversal, card leg) — only an
entry's nature and the category, dossier, payee, supplier reference and
description that go with it, and two days moved within their month. And it
pays what a repair around the models owes:

* every document it writes gets the house stamps (``updated_at``, a new
  ``etag``, ``updated_via = "script"``, under ``provenance.writing_via``) and
  ONE ``revisions`` trail entry, the shape ``scripts/rectify_registers``
  writes — ``{at, via, motif, changes, updated_at_before}`` — plus
  ``source_sha256``, the digest of the CSV. The motif is « Reclassement :
  vérification du journal du 2026-10-07, ligne n »;
* nothing is ever trimmed from a trail (« rien ne se supprime »): a document
  already carrying more than ``_REVISIONS_CAP - 2`` entries is refused, which
  keeps room for this write and for one restoration;
* the ACCOUNT of an entry whose day moves gets a stamp-only update — never
  ``ledger_balance``: the reconciliation's completion compares the account's
  etag (its sentinel), and a moved day changes the as-of picture a pending
  reconciliation was computed on. A change of nature moves no money and
  leaves the account alone;
* the registers are not DAV-exposed: there is no CTag to bump.

Why ``scripts/rectify_registers`` was not extended
--------------------------------------------------
Its plan is all-or-nothing, its fields are isolated, and it never moves a
day. Taught to change an entry's nature, it would stay in the repository as a
PERMANENT door for reclassifying entries outside the model. This script is
bound to one CSV by its fingerprint and closes with it. It imports that
script's PURE helpers (``_instant``, ``_parse_day``, ``_check_text``,
``_addressable``) and leaves it — and its tests — untouched.

The CSV
-------
23 columns, in the order of :data:`CSV_COLUMNS`, UTF-8 (a BOM is accepted),
its quoting read strictly: a stray quote is an error of form, never repaired
into a value. ``statut`` is « appliquer » or « exclu »; an « exclu » line is
set aside BEFORE any judgment and copied verbatim into the report (« Lignes
exclues (groupe E) »). Only its ``entry_id`` cell is recorded, raw and
unjudged, and that entry is never written: an « appliquer » line naming it,
or a card payment whose card leg it is, refuses the whole run. « (aucune) »
in a ``*_actuel`` column matches ``None``, ``''`` or an absent key; in a
``*_cible`` column it clears the field (``None`` for a category or a dossier,
``""`` for a text field, the model's own empty values); an EMPTY ``*_cible``
leaves the field as it is. Identifiers must be UUIDs, sentinels apart. A
target text must survive ``security.sanitize`` unaltered and carry no
leading or trailing space. Any error of form refuses the whole run.

Judging a line (every read is strict: a failed read stops the run, it never
passes for an absence)
-----------------------------------------------------------------------
* What the line does not change must equal the CSV: account, sequence,
  amount, sens, every ``*_actuel`` whose target is empty, and the UTC day
  when the line moves no date.
* A field the line changes holds either its ``*_actuel`` value (to write) or
  its target (already done). Any other value is refused, and so is a line
  part done and part not (« état mixte »). A field that has only a target
  (``supplier_invoice_ref``, ``description``) is done or to write. A line
  done throughout is « déjà appliquée »: the migration is idempotent.
* Every document written — the card leg included — is refused when it is a
  member of a reversal (``reverses_id`` / ``reversed_by_id``), annulée,
  linked to an invoice or to a trust entry, or carries more than
  ``_REVISIONS_CAP - 2`` trail entries.
* A change of nature: never from ``paiement_carte`` or ``correction``; along
  ``admin_ledger.KIND_MOVES``; the sens the new nature implies
  (``admin_ledger._KIND_DIRECTION``) is the stored one; for a nature of
  ``NON_RESULT_KINDS``, no TPS, no TVQ and the canonical net
  (``canonical_ventilation`` — the migration writes no money field, it
  refuses one that is not already right), no category left, and no dossier
  left on a prélèvement or an apport. A category stays on a dépense only.
* A dossier target is read strictly; its ``file_number`` must be the CSV's
  ``dossier_cible``; the entry gets the dossier's ``dossier_file_number`` and
  ``dossier_title`` snapshots. The tax split of a dépense is never touched.
* A moved day, for each leg on ITS OWN account: same month, not after
  ``today_mtl()``, no reconciliation — completed OR draft — whose period end
  falls in ``[min(old, new), max(old, new))`` (it saw the entry on one side
  of its end), and a clearing date not before the new day.
* A card payment (only its day changes): its card leg is the entry its
  ``related_transaction_id`` names, which must be the one UUID of
  ``verification_prealable``; the leg links back, is a ``paiement_carte`` of
  the same amount, the same UTC day and the opposite sens. Both legs move in
  the same batch, each with its own trail entry.

The trust account (report only — the script never writes
``trust_transaction_id``)
----------------------------------------------------------
For each line whose counterparty is « Compte en fidéicommis », the trust
movement it mirrors (the opposite sens, the same amount): read strictly by
the UUIDs ``verification_prealable`` names (their sum must be the amount),
or searched by day — every trust account, ``trust.list_register`` on that
day, truncation checked, rows re-filtered on their UTC day. Reversals,
corrections, annulled entries and « virement inter-dossiers » legs are set
aside and listed apart; an entry is never attributed twice. Outcomes:
TROUVÉ; ABSENT (reported, the line still applies); AMBIGU; ILLISIBLE (a read
failed or was truncated — blocks ``--appliquer``); INCOHÉRENT (an annulled or
reversed entry, the wrong sens, a sum that is not the amount, a fee payment
behind an incoming transfer, alone or among several same-day candidates) —
that line is refused, the lawyer decides.

The controls (§6 of the spec)
-----------------------------
Computed on complete reads — every register, every reconciliation, the
``invoices`` collection streamed directly — three times: BEFORE, AFTER
SIMULATED (the documents read, merged with the exact payloads the batch will
write) and, once written, AFTER (re-read). The criterion is before = after:
6.1 each account's stored ``ledger_balance`` and Σ ``admin_delta``; 6.2 the
month-end balances, from the first entry's month to the current one, of
every « opérations » account and every account whose dates move; 6.3 each
account's lock floor and last reconciliation, and the re-proof of every
completed reconciliation (its stored ``book_balance``, the as-of book
balance and variance recomputed); 6.4 each invoice's ``amount_paid`` and
``invoice.balance_of``; 6.5 ``admin_ledger.results_statement`` over the
whole journal, whose deltas must be exactly those the CSV implies (gross
amounts by revenue kind and expense category, from the CSV's own columns;
nets by category, from the moved rows' stored nets; TPS and TVQ unchanged).
No reference figure lives in this code: the report prints the figures. Plus
check 10 of ``scripts/verify_trust_integrity``, simulated through its pure
``_admin_standing`` / ``_manual_recettes``: every fee payment whose set of
candidate manual recettes changes is listed. A control that cannot be
computed, or that moves, refuses ``--appliquer``.

The fingerprint
---------------
SHA-256 (shown as its first 16 hex characters) of a canonical JSON — sorted
keys, rows sorted by line then path, instants as UTC ISO — of the CSV's
digest, ``db.project``, every planned write — the accounts' stamp-only
updates included — (path, ``update_time`` read, the business fields it
writes, taken FROM the payloads), the refused lines with their reason codes
and the excluded line numbers. No stamp and no generated etag enters it.
``--appliquer`` takes the fingerprint of the approved dry run: anything that
moved since changes it — an entry recorded since on an account whose day
moves included — and calls for a new approval.

Backup and restoration
----------------------
Before the batch, a typed JSON (instants as ``{"$ts": iso}``, recursively)
of every document to write — whole, with the ``update_time`` read and the
etag the migration is about to write — is written in ``--sauvegarde``,
fsynced, read back and verified. ``--restaurer`` (dry by default, its own
fingerprint) puts the business fields back in ONE all-or-nothing batch: it
refuses everything when an entry's current etag is no longer the
migration's (or its fields no longer the migration's values), guards each
write by the ``update_time`` read, writes fresh stamps and a trail entry
« Restauration de la sauvegarde du … — ligne n », replays the date guards
backwards, and stamps — never rebalances — the accounts whose days move
back. An ACCOUNT's etag is not compared: every later entry on it moves it,
and the restoration writes no field of an account but its stamps. Restore the
data BEFORE any return of the code to an earlier version: the old code
refuses the new natures (``type_invalide``).

Running it — from ``athena/``, with ADC: even the dry run reads PRODUCTION,
and the report names ``db.project``. The repository is public: a report, a
backup directory, the CSV or the backup to restore inside it is refused
before anything is created (``Path.resolve`` then ``is_relative_to``, which
sees through Windows short 8.3 names and letter case). The report must be a
new file.

    python -m scripts.reclassify_admin_ledger CSV --rapport R.md
    python -m scripts.reclassify_admin_ledger CSV --rapport R.md \\
        --sauvegarde DOSSIER --appliquer EMPREINTE
    python -m scripts.reclassify_admin_ledger --restaurer SAUVEGARDE.json \\
        --rapport R.md [--appliquer EMPREINTE]

Afterwards, run both integrity scripts and compare their findings BY TEXT.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from google.cloud import firestore

from models import admin_ledger as al
from models import db, provenance, trust
from models import dossier as dossier_model
from models import invoice as invoice_model
from scripts import verify_trust_integrity as vti
from scripts.rectify_registers import (
    PlanRefused,
    _addressable,
    _check_text,
    _instant,
    _parse_day,
)
from tz import to_mtl
from utils.deadlines import today_mtl
from utils.format_fr import format_cents_fr

UTC = timezone.utc
#: The repository root: no report, backup or CSV may live under it (public).
REPO = Path(__file__).resolve().parents[2]

CSV_COLUMNS = (
    "ligne", "groupe", "statut", "sequence", "entry_id", "account_id",
    "date_actuelle", "montant_cents", "sens", "kind_actuel", "kind_cible",
    "category_actuelle", "category_cible", "dossier_id_actuel", "dossier_cible",
    "dossier_id_cible", "counterparty_actuel", "counterparty_cible", "date_cible",
    "supplier_invoice_ref_cible", "description_cible", "verification_prealable",
    "note",
)
NONE_MARK = "(aucune)"
APPLY, EXCLUDED = "appliquer", "exclu"
#: The counterparty that makes a line a movement to or from the trust account.
TRUST_COUNTERPARTY = "Compte en fidéicommis"
VERIFICATION_DAY = "2026-10-07"
MOTIF = "Reclassement : vérification du journal du " + VERIFICATION_DAY + ", ligne {ligne}"
RESTORE_MOTIF = "Restauration de la sauvegarde du {quand} — ligne {ligne}"
#: A written document keeps room for this migration AND one restoration.
MAX_TRAIL_BEFORE = al._REVISIONS_CAP - 2
TEXT_MAX = 2000
FINGERPRINT_HEX = 16
BACKUP_FORMAT = "athena.reclassement-administration/1"

PENDING, DONE, REFUSED = "à appliquer", "déjà appliquée", "refusée"
FOUND, ABSENT, AMBIGUOUS, UNREADABLE, INCOHERENT = (
    "TROUVÉ", "ABSENT", "AMBIGU", "ILLISIBLE", "INCOHÉRENT")
WRITTEN, LOST_BUT_WRITTEN, NOTHING, UNCERTAIN = "écrit", "écrit_réponse_perdue", "rien", "incertain"

TX = al.TRANSACTIONS_COLLECTION
ACCOUNTS = al.ACCOUNTS_COLLECTION

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_GROUP = re.compile(r"[A-Z][0-9]?")
_DIGITS = re.compile(r"[0-9]+")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_WORD = re.compile(r"[a-z0-9]+")
_ISO_DAY = re.compile(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b")

# (field, actuel column, cible column): a change the line may carry, and the
# stored value it expects. ``date`` compares by UTC day.
_PAIRED = (
    ("kind", "kind_actuel", "kind_cible"),
    ("category", "category_actuelle", "category_cible"),
    ("dossier_id", "dossier_id_actuel", "dossier_id_cible"),
    ("counterparty", "counterparty_actuel", "counterparty_cible"),
    ("date", "date_actuelle", "date_cible"),
)
# A change with no stored value to expect (the CSV has no actuel column).
_TARGET_ONLY = (
    ("supplier_invoice_ref", "supplier_invoice_ref_cible"),
    ("description", "description_cible"),
)
#: What a write may change — the fingerprint and the backup keep these;
#: everything else in a payload is a stamp or the trail.
BUSINESS_FIELDS = (
    "kind", "category", "dossier_id", "dossier_file_number", "dossier_title",
    "counterparty", "date", "supplier_invoice_ref", "description",
)
_FIELD_LABELS = {
    "kind": "nature", "category": "catégorie", "dossier_id": "dossier",
    "counterparty": "contrepartie", "date": "date",
    "supplier_invoice_ref": "n° de facture du fournisseur",
    "description": "description",
}
_STRUCTURAL_KINDS = ("paiement_carte", al.REVERSAL_KIND)


class RunRefused(Exception):
    """The run stops; nothing is written. The message is French."""


class ControlImpossible(Exception):
    """A §6 control cannot be computed — it refuses ``--appliquer``."""


@dataclass(frozen=True)
class Impossible:
    """A control's value when it could not be computed."""

    reason: str


# ── values ────────────────────────────────────────────────────────────────


def _out(text: str = "") -> None:
    """Print, degrading characters the console cannot encode (the report
    file, UTF-8, keeps them all)."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding))


def _day(value) -> Optional[date]:
    """The UTC day of a date-only field (stored at midnight UTC)."""
    instant = _instant(value)
    return instant.date() if instant is not None else None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _utc_iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _stamp_iso(value) -> str:
    """A server ``update_time`` as its exact RFC 3339 text (nanoseconds kept)."""
    rfc3339 = getattr(value, "rfc3339", None)
    if callable(rfc3339):
        return rfc3339()
    if isinstance(value, datetime):
        return _utc_iso(value)
    return str(value)


def _as_int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _canon(value):
    """*value* as canonical JSON material: instants as UTC ISO, a deleted
    field as a marker."""
    if value is firestore.DELETE_FIELD:
        return {"$supprimé": True}
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def _digest(material) -> str:
    text = json.dumps(_canon(material), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _stored(name: str, doc: dict):
    """A stored field as the CSV describes it: a day for ``date``, ``None``
    for a missing or empty value."""
    if name == "date":
        return _day(doc.get("date"))
    value = doc.get(name)
    return None if value is None or value == "" else value


def _comparable(name: str, value):
    """A CSV value (expected or target) in :func:`_stored`'s terms."""
    if name == "date":
        return value.date() if isinstance(value, datetime) else value
    return None if value is None or value == "" else value


def _show(value) -> str:
    if value is None or value == "":
        return NONE_MARK
    if isinstance(value, datetime):
        instant = _instant(value)
        if (instant.hour, instant.minute, instant.second, instant.microsecond) == (0, 0, 0, 0):
            return instant.date().isoformat()
        return instant.strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _money(cents) -> str:
    value = _as_int(cents)
    return format_cents_fr(value) if value is not None else f"? ({cents!r})"


def _cell(text) -> str:
    """A Markdown table cell."""
    return str(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


# ── the CSV ───────────────────────────────────────────────────────────────


@dataclass
class Line:
    """One CSV record. The fields after ``groups`` are set on an
    « appliquer » line only: an « exclu » line is set aside unjudged."""

    ligne: int
    statut: str
    cells: dict
    raw: str
    groups: tuple = ()
    sequence: int = 0
    entry_id: str = ""
    account_id: str = ""
    amount: int = 0
    direction: str = ""
    expected: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    dossier_number: str = ""
    uuids: tuple = ()

    def effective(self, name: str):
        """The value *name* holds once the line is applied, per the CSV."""
        return self.targets[name] if name in self.targets else self.expected.get(name)

    @property
    def is_trust(self) -> bool:
        return self.effective("counterparty") == TRUST_COUNTERPARTY


def _positive_int(text: str, what: str) -> int:
    if not _DIGITS.fullmatch(text or "") or int(text) <= 0:
        raise RunRefused(f"{what} : un entier positif est attendu, « {text} » lu")
    return int(text)


def _uuid_cell(text: str, what: str) -> str:
    if not _UUID.fullmatch(text or ""):
        raise RunRefused(f"{what} : un identifiant (UUID) est attendu, « {text} » lu")
    return text


def _day_cell(text: str, what: str) -> datetime:
    day = _parse_day(text)
    if day is None:
        raise RunRefused(f"{what} : une date AAAA-MM-JJ est attendue, « {text} » lu")
    return day


def _marked(text: str, what: str) -> Optional[str]:
    """A ``*_actuel`` cell: « (aucune) » is the empty value, a blank is an
    error (the CSV states every value it expects)."""
    if text == "":
        raise RunRefused(f"{what} : valeur requise (« {NONE_MARK} » pour une valeur vide)")
    return None if text == NONE_MARK else text


def _text_cell(text: str, what: str) -> str:
    """A target text written as is: never stripped, never sanitized away."""
    if text != text.strip():
        raise RunRefused(f"{what} : « {text} » porte des espaces en tête ou en fin — refusé")
    try:
        return _check_text(text, TEXT_MAX, what)
    except PlanRefused as exc:
        raise RunRefused(str(exc)) from None


def _groups(text: str, what: str, *, strict: bool = True) -> tuple:
    """``(code, label)`` of each part of « B1 prélèvement + D date »."""
    out = []
    for part in (p.strip() for p in (text or "").split("+")):
        words = part.split()
        code = words[0] if words else ""
        if not _GROUP.fullmatch(code):
            if strict:
                raise RunRefused(f"{what} : groupe « {text} » illisible "
                                 f"(« B1 prélèvement », « B1 prélèvement + D date »…)")
            code = "?"
        out.append((code, part))
    return tuple(out)


def _reserialize(cells: list) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(cells)
    return buffer.getvalue()


def _entry_key(text: str) -> str:
    """An entry id as the exclusion registry compares it: case and the
    spaces around it never hide an excluded entry."""
    return (text or "").strip().lower()


def excluded_entries(excluded: list[Line]) -> dict:
    """The ``entry_id`` cell of every « exclu » line → its line number.
    Recorded raw, its form never judged (an excluded line is set aside
    unjudged); no entry named there is ever written."""
    out: dict = {}
    for line in sorted(excluded, key=lambda l: l.ligne):
        out.setdefault(_entry_key(line.cells.get("entry_id", "")), line.ligne)
    return out


def parse_csv(data: bytes) -> tuple[list[Line], list[Line]]:
    """``(lines to judge, excluded lines)``, both sorted by line number.
    Raises :class:`RunRefused` on any error of form."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RunRefused("CSV illisible : le fichier n'est pas en UTF-8") from None
    try:
        # strict: a stray quote ('"Banque"X') is an error, never « BanqueX ».
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise RunRefused(f"CSV mal formé ({exc})") from None
    if not records:
        raise RunRefused("CSV vide")
    if tuple(records[0]) != CSV_COLUMNS:
        raise RunRefused("en-tête du CSV inattendu — les 23 colonnes attendues, dans "
                         "cet ordre : " + ", ".join(CSV_COLUMNS))
    physical = text.splitlines()
    verbatim = len(physical) == len(records)
    lines: list[Line] = []
    excluded: list[Line] = []
    numbers: set = set()
    entries: dict = {}
    for index, cells in enumerate(records[1:], start=1):
        if len(cells) != len(CSV_COLUMNS):
            raise RunRefused(f"enregistrement {index} du CSV : {len(cells)} colonne(s), "
                             f"{len(CSV_COLUMNS)} attendues")
        c = dict(zip(CSV_COLUMNS, cells))
        ligne = _positive_int(c["ligne"], f"enregistrement {index} du CSV, colonne ligne")
        if ligne in numbers:
            raise RunRefused(f"ligne {ligne} : numéro de ligne en double")
        numbers.add(ligne)
        if c["statut"] not in (APPLY, EXCLUDED):
            raise RunRefused(f"ligne {ligne} : statut « {c['statut']} » — "
                             f"« {APPLY} » ou « {EXCLUDED} » attendu")
        line = Line(ligne=ligne, statut=c["statut"], cells=c,
                    raw=physical[index] if verbatim else _reserialize(cells))
        if line.statut == EXCLUDED:
            # Set aside BEFORE any judgment — not even its form is checked.
            # Its entry_id is recorded below (excluded_entries), unjudged.
            line.groups = _groups(c["groupe"], f"ligne {ligne}", strict=False)
            excluded.append(line)
            continue
        _parse_apply_line(line)
        if line.entry_id in entries:
            raise RunRefused(f"ligne {ligne} : l'écriture {line.entry_id} figure aussi à "
                             f"la ligne {entries[line.entry_id]}")
        entries[line.entry_id] = ligne
        lines.append(line)
    if not lines and not excluded:
        raise RunRefused("CSV sans aucune ligne")
    # After the loop: an « exclu » line may come after the line naming it.
    barred = excluded_entries(excluded)
    for line in sorted(lines, key=lambda l: l.ligne):
        if _entry_key(line.entry_id) in barred:
            raise RunRefused(f"ligne {line.ligne} : l'écriture {line.entry_id} est exclue à "
                             f"la ligne {barred[_entry_key(line.entry_id)]}")
    lines.sort(key=lambda l: l.ligne)
    excluded.sort(key=lambda l: l.ligne)
    return lines, excluded


def _parse_apply_line(line: Line) -> None:
    c = line.cells
    where = f"ligne {line.ligne}"
    line.groups = _groups(c["groupe"], where)
    line.sequence = _positive_int(c["sequence"], f"{where}, sequence")
    line.entry_id = _uuid_cell(c["entry_id"], f"{where}, entry_id")
    line.account_id = _uuid_cell(c["account_id"], f"{where}, account_id")
    line.amount = _positive_int(c["montant_cents"], f"{where}, montant_cents")
    if c["sens"] not in al.VALID_DIRECTIONS:
        raise RunRefused(f"{where}, sens : « {c['sens']} » — "
                         f"{' ou '.join(al.VALID_DIRECTIONS)} attendu")
    line.direction = c["sens"]
    if c["kind_actuel"] not in al.VALID_KINDS:
        raise RunRefused(f"{where}, kind_actuel : nature « {c['kind_actuel']} » inconnue")
    dossier_actuel = c["dossier_id_actuel"]
    line.expected = {
        "kind": c["kind_actuel"],
        "category": _marked(c["category_actuelle"], f"{where}, category_actuelle"),
        "dossier_id": (None if dossier_actuel == NONE_MARK
                       else _uuid_cell(dossier_actuel, f"{where}, dossier_id_actuel")),
        "counterparty": _marked(c["counterparty_actuel"], f"{where}, counterparty_actuel"),
        "date": _day_cell(c["date_actuelle"], f"{where}, date_actuelle"),
    }

    targets: dict = {}
    if c["kind_cible"]:
        if c["kind_cible"] not in al.VALID_KINDS:
            raise RunRefused(f"{where}, kind_cible : nature « {c['kind_cible']} » inconnue")
        targets["kind"] = c["kind_cible"]
    if c["category_cible"]:
        if c["category_cible"] == NONE_MARK:
            targets["category"] = None
        elif c["category_cible"] in al.ADMIN_EXPENSE_CATEGORIES:
            targets["category"] = c["category_cible"]
        else:
            raise RunRefused(f"{where}, category_cible : catégorie « {c['category_cible']} » "
                             f"inconnue")
    dossier_id, number = c["dossier_id_cible"], c["dossier_cible"]
    if dossier_id or number:
        if dossier_id == NONE_MARK and number in ("", NONE_MARK):
            targets["dossier_id"] = None
        elif dossier_id and dossier_id != NONE_MARK and number and number != NONE_MARK:
            targets["dossier_id"] = _uuid_cell(dossier_id, f"{where}, dossier_id_cible")
            if number != number.strip():
                raise RunRefused(f"{where}, dossier_cible : espaces en tête ou en fin")
            line.dossier_number = number
        else:
            raise RunRefused(f"{where} : dossier_cible et dossier_id_cible vont ensemble "
                             f"(un identifiant et son numéro, ou « {NONE_MARK} »)")
    if c["counterparty_cible"]:
        if c["counterparty_cible"] == NONE_MARK:
            raise RunRefused(f"{where}, counterparty_cible : la contrepartie ne s'efface pas")
        targets["counterparty"] = _text_cell(c["counterparty_cible"],
                                             f"{where}, counterparty_cible")
    if c["date_cible"]:
        targets["date"] = _day_cell(c["date_cible"], f"{where}, date_cible")
    for name, column in _TARGET_ONLY:
        if c[column]:
            targets[name] = ("" if c[column] == NONE_MARK
                             else _text_cell(c[column], f"{where}, {column}"))
    if not targets:
        raise RunRefused(f"{where} : aucune cible — la ligne ne change rien")
    for name, _actuel, column in _PAIRED:
        if name in targets and (_comparable(name, targets[name])
                                == _comparable(name, line.expected[name])):
            raise RunRefused(f"{where}, {column} : la cible est la valeur actuelle")
    line.targets = targets
    line.uuids = tuple(dict.fromkeys(_UUID.findall(c["verification_prealable"])))


# ── the store ─────────────────────────────────────────────────────────────


def _doc(snap) -> dict:
    return dict(snap.to_dict() or {})


@dataclass
class Store:
    """Everything the run reads, read once. ``entries`` / ``account_snaps``
    keep the snapshots (their ``update_time`` guards the batch); ``rows`` /
    ``accounts`` hold the documents exactly as stored."""

    entries: dict
    rows: dict
    account_snaps: dict
    accounts: dict
    recs: list
    invoices: Optional[list]
    invoices_error: str
    trust_rows: Optional[list]
    trust_error: str

    def control_rows(self, payloads: Optional[dict] = None) -> list[dict]:
        """The entries as the controls read them (an ``id`` on each) —
        merged with *payloads* (by path) for the simulation."""
        return [_merge({**row, "id": row.get("id") or eid}, (payloads or {}).get(f"{TX}/{eid}"))
                for eid, row in self.rows.items()]

    def control_accounts(self, payloads: Optional[dict] = None) -> dict:
        return {aid: _merge(acc, (payloads or {}).get(f"{ACCOUNTS}/{aid}"))
                for aid, acc in self.accounts.items()}

    def recs_of(self, account_id) -> list[dict]:
        return [r for r in self.recs if r.get("account_id") == account_id]

    def account_label(self, account_id) -> str:
        account = self.accounts.get(account_id) or {}
        return f"{account.get('name') or '?'} ({account_id})"


def read_store() -> Store:
    """Read the administration registers whole, and what the controls need.
    A failure of the registers stops the run; the invoices and the trust
    register only make their control impossible (which blocks writing)."""
    try:
        entries = {s.id: s for s in db.collection(TX).stream()}
        account_snaps = {s.id: s for s in db.collection(ACCOUNTS).stream()}
        recs = [{**_doc(s), "id": _doc(s).get("id") or s.id}
                for s in db.collection(al.RECONCILIATIONS_COLLECTION).stream()]
    except Exception as exc:
        raise RunRefused(f"lecture du registre d'administration impossible "
                         f"({type(exc).__name__}) — rien n'a été écrit") from None
    invoices, invoices_error = None, ""
    try:
        invoices = [{**_doc(s), "id": s.id}
                    for s in db.collection(invoice_model.COLLECTION).stream()]
    except Exception as exc:
        invoices_error = type(exc).__name__
    trust_rows, trust_error = None, ""
    try:
        trust_rows = [{**_doc(s), "id": _doc(s).get("id") or s.id}
                      for s in db.collection(trust.TRANSACTIONS_COLLECTION).stream()]
    except Exception as exc:
        trust_error = type(exc).__name__
    return Store(
        entries=entries, rows={k: _doc(s) for k, s in entries.items()},
        account_snaps=account_snaps,
        accounts={k: _doc(s) for k, s in account_snaps.items()},
        recs=recs, invoices=invoices, invoices_error=invoices_error,
        trust_rows=trust_rows, trust_error=trust_error,
    )


# ── judging a line ────────────────────────────────────────────────────────


@dataclass
class TrustCheck:
    ligne: int
    method: str              # « identifiant » or « jour »
    outcome: str
    detail: str
    day: Optional[date] = None
    matches: list = field(default_factory=list)
    aside: list = field(default_factory=list)   # [(trust entry, why)]


@dataclass
class Verdict:
    line: Line
    state: str = PENDING
    reasons: list = field(default_factory=list)   # [(code, French message)]
    rule4_ok: bool = False
    doc: Optional[dict] = None
    leg: Optional[dict] = None
    # True only once the pair checks (link back, nature, amount, day, sens)
    # all passed — the report says « vérifiés » on nothing less.
    leg_checked: bool = False
    leg_failure: str = ""
    pending: dict = field(default_factory=dict)   # field → value to write
    trust: Optional[TrustCheck] = None

    def refuse(self, code: str, message: str) -> None:
        self.reasons.append((code, message))
        self.state = REFUSED


def _universal_refusals(doc: dict) -> list[tuple[str, str]]:
    """What refuses ANY document this script would write."""
    out = []
    if doc.get("reverses_id") or doc.get("reversed_by_id"):
        out.append(("lien_contre_passation", "membre d'une contre-passation"))
    if doc.get("status") == "annulée":
        out.append(("annulée", "écriture annulée"))
    if doc.get("invoice_id"):
        out.append(("liée_facture", "liée à une facture"))
    if doc.get("trust_transaction_id"):
        out.append(("liée_fideicommis", "liée à une écriture du fidéicommis"))
    revisions = doc.get("revisions")
    if revisions is not None and not isinstance(revisions, list):
        out.append(("fil_illisible", "fil de révisions illisible"))
    elif len(revisions or []) > MAX_TRAIL_BEFORE:
        out.append(("fil_plein", f"{len(revisions)} entrées au fil de révisions — au plus "
                                 f"{MAX_TRAIL_BEFORE}, pour garder la place de cette "
                                 f"migration et d'une restauration sans rien rogner"))
    return out


def _date_refusals(doc: dict, new_day: date, store: Store, today: date,
                   who: str = "") -> list[tuple[str, str]]:
    """The guards of a day moved on the document's OWN account."""
    out = []
    old_day = _day(doc.get("date"))
    account_id = doc.get("account_id")
    if account_id not in store.account_snaps:
        out.append(("compte_introuvable", f"{who}compte {account_id} introuvable"))
    if old_day is None:
        return out + [("date_illisible", f"{who}date inscrite illisible")]
    if (old_day.year, old_day.month) != (new_day.year, new_day.month):
        out.append(("date_hors_mois", f"{who}{old_day} → {new_day} : une date ne change "
                                      f"ici que dans son mois"))
    if new_day > today:
        out.append(("date_future", f"{who}{new_day} est dans le futur (aujourd'hui : {today})"))
    low, high = min(old_day, new_day), max(old_day, new_day)
    for rec in sorted(store.recs_of(account_id), key=lambda r: str(r.get("id"))):
        end = _day(rec.get("period_end"))
        if end is None:
            out.append(("date_conciliation", f"{who}conciliation {rec.get('id')} sans fin "
                                              f"de période lisible"))
        elif low <= end < high:
            out.append(("date_conciliation",
                        f"{who}la conciliation {rec.get('status')} {rec.get('id')} du compte "
                        f"{store.account_label(account_id)} se termine le {end}, entre "
                        f"l'ancienne date et la nouvelle"))
    cleared = _day(doc.get("cleared_date"))
    if cleared is not None and cleared < new_day:
        out.append(("date_compensation", f"{who}compensée le {cleared}, avant la nouvelle "
                                         f"date {new_day}"))
    return out


def _nature_refusals(doc: dict, kind_before: str, kind_after: str) -> list[tuple[str, str]]:
    if kind_before in _STRUCTURAL_KINDS:
        return [("nature_structurelle",
                 f"une écriture « {al.KIND_LABELS.get(kind_before, kind_before)} » ne "
                 f"change pas de nature")]
    out = []
    if not al.kind_move_allowed(kind_before, kind_after):
        out.append(("passage_non_permis", f"le passage {kind_before} → {kind_after} n'est "
                                          f"pas permis (admin_ledger.KIND_MOVES)"))
    implied = al._KIND_DIRECTION.get(kind_after)
    if implied != doc.get("direction"):
        out.append(("sens_incohérent", f"« {kind_after} » implique le sens {implied}, "
                                       f"l'écriture est inscrite en {doc.get('direction')}"))
    if kind_after in al.NON_RESULT_KINDS:
        try:
            split = {k: int(doc.get(k) or 0) for k in ("net_amount", "gst_amount", "qst_amount")}
        except (TypeError, ValueError):
            split = None
        amount = _as_int(doc.get("amount"))
        canonical = (al.canonical_ventilation(kind_after, doc.get("direction", ""), amount)
                     if amount is not None else None)
        if split is None or split != canonical:
            shown = ("illisible" if split is None else
                     f"{split['net_amount']}+{split['gst_amount']}+{split['qst_amount']}")
            out.append(("ventilation_non_canonique",
                        f"ventilation {shown} — « {al.KIND_LABELS[kind_after]} » ne porte ni "
                        f"TPS ni TVQ, et son net est "
                        f"{'le montant' if doc.get('direction') == 'déboursé' else 'nul'} ; "
                        f"la migration n'écrit aucun champ monétaire"))
    return out


def _category_refusals(kind_after: str, category_after) -> list[tuple[str, str]]:
    if kind_after == "dépense":
        if not category_after:
            return [("catégorie_requise", "une dépense porte une catégorie")]
        if category_after not in al.ADMIN_EXPENSE_CATEGORIES:
            return [("catégorie_invalide", f"catégorie « {category_after} » inconnue")]
        return []
    if category_after:
        code = "catégorie_restante" if kind_after in al.NON_RESULT_KINDS else "catégorie_hors_dépense"
        return [(code, f"« {al.KIND_LABELS.get(kind_after, kind_after)} » ne porte pas de "
                       f"catégorie (« {category_after} » resterait)")]
    return []


def judge_line(line: Line, store: Store, today: date, dossiers: dict) -> Verdict:
    """Judge one « appliquer » line against the stored entry."""
    verdict = Verdict(line)
    doc = store.rows.get(line.entry_id)
    if doc is None:
        verdict.refuse("écriture_introuvable", "écriture introuvable au registre d'administration")
        return verdict
    verdict.doc = doc

    # 1. What the line does not change must be what the CSV says.
    if doc.get("account_id") != line.account_id:
        verdict.refuse("compte_différent", f"inscrite au compte {doc.get('account_id')}, le "
                                           f"CSV dit {line.account_id}")
    if _as_int(doc.get("sequence")) != line.sequence:
        verdict.refuse("séquence_différente", f"séquence inscrite {doc.get('sequence')}, le "
                                              f"CSV dit {line.sequence}")
    if _as_int(doc.get("amount")) != line.amount:
        verdict.refuse("montant_différent", f"montant inscrit {_money(doc.get('amount'))}, le "
                                            f"CSV dit {_money(line.amount)}")
    if doc.get("direction") != line.direction:
        verdict.refuse("sens_différent", f"sens inscrit {doc.get('direction')}, le CSV dit "
                                         f"{line.direction}")
    states: dict = {}
    for name, _actuel, _cible in _PAIRED:
        stored = _stored(name, doc)
        expected = _comparable(name, line.expected[name])
        label = _FIELD_LABELS[name]
        if name not in line.targets:
            if stored != expected:
                verdict.refuse(f"actuel_différent:{name}",
                               f"{label} inscrite {_show(stored)}, le CSV attend {_show(expected)}")
            continue
        target = _comparable(name, line.targets[name])
        if stored == target:
            states[name] = DONE
        elif stored == expected:
            states[name] = PENDING
        else:
            verdict.refuse(f"valeur_inattendue:{name}",
                           f"{label} inscrite {_show(stored)} — ni la valeur actuelle du CSV "
                           f"({_show(expected)}) ni sa cible ({_show(target)})")
    if verdict.reasons:
        return verdict
    for name, _column in _TARGET_ONLY:
        if name in line.targets:
            done = _stored(name, doc) == _comparable(name, line.targets[name])
            states[name] = DONE if done else PENDING

    # 2. Done throughout, to write throughout, or a mix (refused).
    paired = [states[name] for name, _a, _c in _PAIRED if name in states]
    if all(s == DONE for s in states.values()):
        verdict.state = DONE
    elif paired and not all(s == PENDING for s in paired):
        verdict.refuse("état_mixte", "une partie de la ligne est déjà faite et l'autre non — "
                                     "l'écriture a changé depuis la préparation du CSV")
        return verdict
    else:
        verdict.pending = {name: line.targets[name] for name, s in states.items() if s == PENDING}
    verdict.rule4_ok = True

    kind_before = doc.get("kind") or ""
    if verdict.state == DONE:
        if kind_before == "paiement_carte" and "date" in line.targets:
            _judge_card_pair(verdict, store, today, pending=False)
        return verdict

    # 3. The guards of every written document, then the change's own.
    for code, message in _universal_refusals(doc):
        verdict.refuse(code, message)
    pending = verdict.pending
    kind_after = pending.get("kind", kind_before)
    category_after = (pending["category"] if "category" in pending
                      else (doc.get("category") or None))
    dossier_after = (pending["dossier_id"] if "dossier_id" in pending
                     else (doc.get("dossier_id") or None))
    if "kind" in pending:
        for code, message in _nature_refusals(doc, kind_before, kind_after):
            verdict.refuse(code, message)
    if "kind" in pending or "category" in pending:
        for code, message in _category_refusals(kind_after, category_after):
            verdict.refuse(code, message)
    if ("kind" in pending or "dossier_id" in pending) and kind_after in al.OWNER_KINDS \
            and dossier_after:
        verdict.refuse("dossier_interdit", f"un « {al.KIND_LABELS[kind_after]} » ne se "
                                           f"rattache à aucun dossier ({dossier_after})")
    if "dossier_id" in pending:
        _judge_dossier(verdict, dossiers)
    if "date" in pending:
        for code, message in _date_refusals(doc, pending["date"].date(), store, today):
            verdict.refuse(code, message)
    if kind_before == "paiement_carte":
        _judge_card_pair(verdict, store, today, pending=True)
    return verdict


def _judge_dossier(verdict: Verdict, dossiers: dict) -> None:
    """Group A: the dossier is read strictly, its number checked, its
    snapshots taken. A failed read stops the whole run."""
    line, pending = verdict.line, verdict.pending
    dossier_id = pending["dossier_id"]
    if dossier_id is None:
        pending["dossier_file_number"] = ""
        pending["dossier_title"] = ""
        return
    if dossier_id not in dossiers:
        try:
            dossiers[dossier_id] = dossier_model.get_dossier_strict(dossier_id)
        except Exception as exc:
            raise RunRefused(f"ligne {line.ligne} : lecture du dossier {dossier_id} "
                             f"impossible ({type(exc).__name__}) — rien n'a été écrit") from None
    dossier = dossiers[dossier_id]
    if dossier is None:
        verdict.refuse("dossier_introuvable", f"dossier {dossier_id} introuvable")
        return
    number = dossier.get("file_number") or ""
    if number != line.dossier_number:
        verdict.refuse("dossier_numéro_différent", f"le dossier {dossier_id} porte le numéro "
                                                   f"« {number} », le CSV dit "
                                                   f"« {line.dossier_number} »")
        return
    pending["dossier_file_number"] = number
    pending["dossier_title"] = dossier.get("title") or ""


def _judge_card_pair(verdict: Verdict, store: Store, today: date, *, pending: bool) -> None:
    """A card payment moves with its card leg — the entry its
    ``related_transaction_id`` names, which ``verification_prealable``
    must name too."""
    line, doc = verdict.line, verdict.doc
    if pending and set(line.targets) - {"date"}:
        verdict.refuse("paiement_carte_autre_champ",
                       "un paiement de carte ne change ici que de date")
    if "date" not in line.targets:
        return
    if len(line.uuids) != 1:
        verdict.refuse("volet_carte_non_désigné", "verification_prealable doit nommer "
                                                  "l'identifiant du volet lié de la carte, "
                                                  "et lui seul")
        return
    leg_id = line.uuids[0]
    if doc.get("related_transaction_id") != leg_id:
        verdict.refuse("volet_carte_différent", f"l'écriture est liée à "
                                                f"{doc.get('related_transaction_id')}, le CSV "
                                                f"nomme {leg_id}")
        return
    leg = store.rows.get(leg_id)
    if leg is None:
        verdict.refuse("volet_carte_introuvable", f"volet de carte {leg_id} introuvable")
        return
    verdict.leg = leg
    if pending is False and _day(leg.get("date")) != _day(doc.get("date")):
        verdict.leg_failure = (f"le volet de carte {leg_id} est au {_day(leg.get('date'))}, "
                               f"l'écriture au {_day(doc.get('date'))} : il n'a pas suivi")
        verdict.refuse("état_mixte", verdict.leg_failure)
        return
    problems = []
    if leg.get("related_transaction_id") != (doc.get("id") or line.entry_id):
        problems.append("son lien ne revient pas à l'écriture")
    if leg.get("kind") != "paiement_carte":
        problems.append(f"nature « {leg.get('kind')} »")
    if _as_int(leg.get("amount")) != _as_int(doc.get("amount")):
        problems.append("montant différent")
    if _day(leg.get("date")) != _day(doc.get("date")):
        problems.append("jour différent")
    if leg.get("direction") not in al.VALID_DIRECTIONS or leg.get("direction") == doc.get("direction"):
        problems.append("même sens")
    if problems:
        verdict.leg_failure = f"volet de carte {leg_id} : " + " ; ".join(problems)
        verdict.refuse("volet_carte_incohérent", verdict.leg_failure)
        return
    verdict.leg_checked = True
    if not pending:
        return
    for code, message in _universal_refusals(leg):
        verdict.refuse(f"volet_carte:{code}", f"volet de carte : {message}")
    for code, message in _date_refusals(leg, line.targets["date"].date(), store, today,
                                        who="volet de carte : "):
        verdict.refuse(f"volet_carte:{code}", message)


# ── the trust account ─────────────────────────────────────────────────────


def _set_aside(entry: dict, claimed: dict) -> str:
    """Why a same-day trust movement is not a candidate — ``""`` when it is."""
    if entry.get("purpose") == trust.TRANSFER_PURPOSE:
        return "virement inter-dossiers"
    if entry.get("purpose") == trust.REVERSAL_PURPOSE or entry.get("reverses_id"):
        return "écriture de correction"
    if entry.get("status") == "annulée":
        return "annulée"
    if entry.get("reversed_by_id"):
        return "contre-passée"
    if entry.get("id") in claimed:
        return f"déjà attribuée à la ligne {claimed[entry.get('id')]}"
    return ""


def _trust_by_id(line: Line, claimed: dict, expected_dir: str, incoming: bool) -> TrustCheck:
    def check(outcome: str, detail: str, matches=()) -> TrustCheck:
        return TrustCheck(line.ligne, "identifiant", outcome, detail, matches=list(matches))

    found, missing = [], []
    for trust_id in line.uuids:
        try:
            entry = trust.get_transaction_strict(trust_id)
        except Exception as exc:
            return check(UNREADABLE, f"lecture de l'écriture du fidéicommis {trust_id} "
                                     f"impossible ({type(exc).__name__})")
        if entry is None:
            missing.append(trust_id)
        else:
            found.append({**entry, "id": entry.get("id") or trust_id})
    problems = []
    for entry in found:
        tid = entry["id"]
        why = _set_aside(entry, claimed)
        if why:
            problems.append(f"{tid} : {why}")
        if entry.get("direction") != expected_dir:
            problems.append(f"{tid} : {entry.get('direction')} — un {expected_dir} est attendu")
        if incoming and entry.get("purpose") == trust.FEE_PAYMENT_PURPOSE:
            problems.append(f"{tid} : paiement d'honoraires — des honoraires tirés du "
                            f"fidéicommis sont un encaissement, jamais un virement interne")
    if problems:
        return check(INCOHERENT, " ; ".join(problems), found)
    if missing:
        return check(ABSENT, "introuvable(s) au registre du fidéicommis : "
                             + ", ".join(missing), found)
    total = sum(_as_int(e.get("amount")) or 0 for e in found)
    if total != line.amount:
        return check(INCOHERENT, f"somme des écritures nommées {_money(total)}, le montant de "
                                 f"la ligne est {_money(line.amount)}", found)
    for entry in found:
        claimed[entry["id"]] = line.ligne
    return check(FOUND, "écriture(s) nommée(s) au CSV", found)


def _trust_by_day(line: Line, claimed: dict, expected_dir: str, incoming: bool,
                  accounts: dict) -> TrustCheck:
    day = line.effective("date").date()

    def check(outcome: str, detail: str, matches=(), aside=()) -> TrustCheck:
        return TrustCheck(line.ligne, "jour", outcome, detail, day=day,
                          matches=list(matches), aside=list(aside))

    if "error" in accounts:
        return check(UNREADABLE, f"comptes du fidéicommis illisibles ({accounts['error']})")
    candidates, aside = [], []
    for account in accounts["list"]:
        account_id = account.get("id")
        if not _addressable(account_id):
            return check(UNREADABLE, "un compte du fidéicommis sans identifiant lisible")
        try:
            rows, truncated = trust.list_register(account_id, _midnight(day), _midnight(day))
        except Exception as exc:
            return check(UNREADABLE, f"registre du compte {account_id} illisible "
                                     f"({type(exc).__name__})")
        if truncated:
            return check(UNREADABLE, f"lecture du registre du compte {account_id} tronquée")
        for row in rows:
            if _day(row.get("date")) != day:
                continue
            if row.get("direction") != expected_dir or _as_int(row.get("amount")) != line.amount:
                continue
            why = _set_aside(row, claimed)
            if why:
                aside.append((row, why))
            else:
                candidates.append(row)
    if not candidates:
        return check(ABSENT, f"aucun {expected_dir} de {_money(line.amount)} le {day} au "
                             f"registre du fidéicommis", aside=aside)
    # Before ambiguity: a fee payment among the candidates — alone or not —
    # may be this transfer's counterpart, and fees are revenue. Nothing is
    # claimed; the line is refused and every candidate listed.
    fees = [c for c in candidates if c.get("purpose") == trust.FEE_PAYMENT_PURPOSE]
    if incoming and fees:
        among = f", parmi {len(candidates)} candidats le {day}" if len(candidates) > 1 else ""
        return check(INCOHERENT, f"{', '.join(str(f.get('id')) for f in fees)} : paiement "
                                 f"d'honoraires{among} — des honoraires tirés du fidéicommis "
                                 f"sont un encaissement, jamais un virement interne",
                     candidates, aside)
    if len(candidates) > 1:
        return check(AMBIGUOUS, f"{len(candidates)} candidats le {day}", candidates, aside)
    entry = candidates[0]
    claimed[entry.get("id")] = line.ligne
    return check(FOUND, f"trouvée le {day}", candidates, aside)


def check_trust(verdicts: list[Verdict]) -> list[TrustCheck]:
    """The trust movement behind each « Compte en fidéicommis » line. Named
    entries are claimed first, then the day searches run, in line order:
    an entry is never attributed twice."""
    claimed: dict = {}
    todo = [v for v in verdicts if v.rule4_ok and v.line.is_trust]
    accounts: dict = {}
    checks = []
    for by_id in (True, False):
        for verdict in sorted(todo, key=lambda v: v.line.ligne):
            line = verdict.line
            if bool(line.uuids) is not by_id:
                continue
            expected_dir = "recette" if line.direction == "déboursé" else "déboursé"
            incoming = line.effective("kind") == "virement_interne_entrant"
            if by_id:
                result = _trust_by_id(line, claimed, expected_dir, incoming)
            else:
                if not accounts:
                    try:
                        accounts["list"] = trust.list_accounts()
                    except Exception as exc:
                        accounts["error"] = type(exc).__name__
                result = _trust_by_day(line, claimed, expected_dir, incoming, accounts)
            verdict.trust = result
            if result.outcome == INCOHERENT and verdict.state == PENDING:
                verdict.refuse("fideicommis_incohérent", result.detail)
            checks.append(result)
    checks.sort(key=lambda c: c.ligne)
    return checks


# ── the writes ────────────────────────────────────────────────────────────


@dataclass
class Write:
    path: str
    collection: str
    doc_id: str
    ligne: int
    role: str          # « écriture », « volet_carte » or « compte »
    snap: Any
    before: dict
    business: dict
    changes: dict


def _entry_write(store: Store, doc_id: str, ligne: int, role: str, values: dict) -> Write:
    before = store.rows[doc_id]
    business = {name: value for name, value in values.items() if before.get(name) != value}
    return Write(f"{TX}/{doc_id}", TX, doc_id, ligne, role, store.entries[doc_id], before,
                 business, {name: [before.get(name), value] for name, value in business.items()})


def plan_writes(verdicts: list[Verdict], store: Store, excluded: list = ()) -> list[Write]:
    """The documents to write. An « appliquer » line naming an excluded entry
    never gets here (``parse_csv``); a card leg is known only now, and an
    excluded one refuses the run."""
    barred = excluded_entries(list(excluded))
    writes: list[Write] = []
    moved: dict = {}   # account id → first line moving a day on it
    for verdict in sorted(verdicts, key=lambda v: v.line.ligne):
        if verdict.state != PENDING:
            continue
        line = verdict.line
        write = _entry_write(store, line.entry_id, line.ligne, "écriture", verdict.pending)
        writes.append(write)
        if "date" in write.business:
            moved.setdefault(verdict.doc.get("account_id"), line.ligne)
            if verdict.leg is not None:
                leg_id = verdict.leg.get("id") or line.uuids[0]
                if _entry_key(leg_id) in barred:
                    raise RunRefused(f"ligne {line.ligne} (volet de carte) : l'écriture "
                                     f"{leg_id} est exclue à la ligne "
                                     f"{barred[_entry_key(leg_id)]}")
                writes.append(_entry_write(store, leg_id, line.ligne, "volet_carte",
                                           {"date": write.business["date"]}))
                moved.setdefault(verdict.leg.get("account_id"), line.ligne)
    for account_id, ligne in sorted(moved.items()):
        writes.append(Write(f"{ACCOUNTS}/{account_id}", ACCOUNTS, account_id, ligne, "compte",
                            store.account_snaps[account_id], store.accounts[account_id], {}, {}))
    seen: dict = {}
    for write in writes:
        if write.path in seen:
            raise RunRefused(f"le document {write.path} serait écrit deux fois (lignes "
                             f"{seen[write.path]} et {write.ligne}) — corrigez le CSV")
        seen[write.path] = write.ligne
    return writes


def _trail(write: Write, now: datetime, motif: str, source_sha: str) -> dict:
    return {
        "at": now,
        "via": provenance.current_via(),
        "motif": motif,
        "changes": write.changes,
        "updated_at_before": write.before.get("updated_at"),
        "source_sha256": source_sha,
    }


def build_payloads(writes: list[Write], now: datetime, source_sha: str,
                   motif: Callable[[Write], str]) -> dict:
    """The exact dicts the batch writes, by path — stamps under
    ``writing_via("script")``, one trail entry per entry document."""
    payloads: dict = {}
    with provenance.writing_via("script"):
        for write in writes:
            stamps = provenance.update_fields(now)
            if write.role == "compte":
                payloads[write.path] = stamps
                continue
            revisions = list(write.before.get("revisions") or [])
            revisions.append(_trail(write, now, motif(write), source_sha))
            payloads[write.path] = {**write.business, "revisions": revisions, **stamps}
    return payloads


def _merge(doc: dict, payload: Optional[dict]) -> dict:
    if not payload:
        return doc
    merged = dict(doc)
    for key, value in payload.items():
        if value is firestore.DELETE_FIELD:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _fingerprint(head: dict, writes: list[Write], payloads: dict) -> str:
    # EVERY planned write enters the print, the accounts' stamp-only updates
    # included (their path and the update time read; no business field): an
    # entry recorded on such an account since the approved dry run moves
    # that time, so the print changes and calls for a new approval.
    rows = [{
        "ligne": w.ligne,
        "chemin": w.path,
        "update_time": _stamp_iso(w.snap.update_time),
        "apres": {k: payloads[w.path][k] for k in BUSINESS_FIELDS if k in payloads[w.path]},
    } for w in writes]
    rows.sort(key=lambda r: (r["ligne"], r["chemin"]))
    return _digest({**head, "ecritures": rows})[:FINGERPRINT_HEX]


# ── the controls ──────────────────────────────────────────────────────────


def _attempt(fn: Callable, *args):
    try:
        return fn(*args)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ControlImpossible, al.UnresolvedCorrection)) else ""
        return Impossible(f"{type(exc).__name__}{' : ' + detail if detail else ''}")


def _signed(row: dict) -> int:
    amount = _as_int(row.get("amount"))
    if amount is None:
        raise ControlImpossible(f"montant illisible sur l'écriture {row.get('id')}")
    return al.admin_delta(row.get("direction", ""), amount)


def _control_balances(rows: list, accounts: dict) -> dict:
    out = {aid: {"ledger_balance": acc.get("ledger_balance"), "somme": 0}
           for aid, acc in accounts.items()}
    for row in rows:
        slot = out.setdefault(row.get("account_id") or "", {"ledger_balance": None, "somme": 0})
        slot["somme"] += _signed(row)
    return out


def _control_month_ends(rows: list, account_ids: list, months: list) -> dict:
    out = {}
    for account_id in account_ids:
        dated = []
        for row in rows:
            if row.get("account_id") != account_id:
                continue
            day = _day(row.get("date"))
            if day is None:
                raise ControlImpossible(f"date illisible sur l'écriture {row.get('id')}")
            dated.append((day, _signed(row)))
        out[account_id] = {m.isoformat(): sum(s for d, s in dated if d <= m) for m in months}
    return out


def _reproof(account: dict, rec: dict, end: date, rows: list) -> dict:
    """The completed reconciliation re-proved at its period end — the
    model's as-of sets (``reconciliation_as_of_context``), on the rows given."""
    by_id = {row.get("id"): row for row in rows}
    book = outstanding = in_transit = 0
    for row in rows:
        day = _day(row.get("date"))
        if day is None:
            raise ControlImpossible(f"date illisible sur l'écriture {row.get('id')}")
        if day > end:
            continue
        signed = _signed(row)
        book += signed
        status = row.get("status")
        counted = status == "en_circulation"
        if status == "compensée":
            cleared = _day(row.get("cleared_date"))
            counted = cleared is not None and cleared > end
        elif status == "annulée" and row.get("reversed_by_id") and not row.get("reverses_id"):
            reverser = by_id.get(row.get("reversed_by_id"))
            if reverser is None or _day(reverser.get("date")) is None:
                raise ControlImpossible(f"contre-passation de {row.get('id')} illisible")
            counted = _day(reverser.get("date")) > end
        if counted and row.get("direction") == "déboursé":
            outstanding += abs(signed)
        elif counted and row.get("direction") == "recette":
            in_transit += abs(signed)
    statement = _as_int(rec.get("statement_balance"))
    if statement is None:
        raise ControlImpossible(f"conciliation {rec.get('id')} sans solde de relevé lisible")
    variance = al.reconciliation_variance(
        al.statement_to_ledger(account.get("account_type", ""), statement),
        book, outstanding, in_transit)
    return {"book_balance": rec.get("book_balance"), "recalcule": book, "ecart": variance}


def _control_reconciliations(rows: list, accounts: dict, recs: list) -> dict:
    by_account: dict = {}
    for rec in recs:
        by_account.setdefault(rec.get("account_id") or "", []).append(rec)
    out = {}
    for account_id in sorted(set(accounts) | set(by_account)):
        ends = []
        for rec in by_account.get(account_id, []):
            end = _day(rec.get("period_end"))
            if end is None:
                raise ControlImpossible(f"conciliation {rec.get('id')} sans fin de période")
            ends.append((end, rec))
        completed = [(end, rec) for end, rec in ends if rec.get("status") == "complétée"]
        floor = max((end for end, _ in completed), default=None)
        last = max(ends, key=lambda t: (t[0], str(t[1].get("id"))), default=None)
        mine = [row for row in rows if row.get("account_id") == account_id]
        out[account_id] = {
            "plancher": floor.isoformat() if floor else None,
            "derniere": ({"id": last[1].get("id"), "statut": last[1].get("status"),
                          "fin": last[0].isoformat()} if last else None),
            "preuves": {str(rec.get("id")): _reproof(accounts.get(account_id) or {}, rec, end, mine)
                        for end, rec in completed},
        }
    return out


def _control_invoices(invoices: list) -> dict:
    return {str(inv.get("id")): {"paye": inv.get("amount_paid"),
                                 "solde": invoice_model.balance_of(inv)}
            for inv in invoices}


def _control_fee_candidates(trust_rows: list, rows: list) -> dict:
    """Check 10 of ``verify_trust_integrity``, its candidate side: for each
    standing, unlinked fee payment, the manual recettes that could be its."""
    linked = {row.get("trust_transaction_id") for row in rows if row.get("trust_transaction_id")}
    unlinked = [row for row in rows
                if not row.get("trust_transaction_id") and row.get("direction") == "recette"
                and row.get("kind") in al.REVENUE_KINDS and vti._admin_standing(row)]
    out = {}
    for fee in trust_rows:
        if fee.get("purpose") != trust.FEE_PAYMENT_PURPOSE:
            continue
        if fee.get("status") == "annulée" or fee.get("reversed_by_id") or fee.get("id") in linked:
            continue
        out[str(fee.get("id"))] = sorted(str(r.get("id")) for r in vti._manual_recettes(fee, unlinked))
    return out


def compute_controls(rows: list, accounts: dict, store: Store, month_accounts: list,
                     months: list) -> dict:
    return {
        "6.1": _attempt(_control_balances, rows, accounts),
        "6.2": _attempt(_control_month_ends, rows, month_accounts, months),
        "6.3": _attempt(_control_reconciliations, rows, accounts, store.recs),
        "6.4": (_attempt(_control_invoices, store.invoices) if store.invoices is not None
                else Impossible(f"factures illisibles ({store.invoices_error})")),
        "6.5": _attempt(al.results_statement, rows),
        "10": (_attempt(_control_fee_candidates, store.trust_rows, rows)
               if store.trust_rows is not None
               else Impossible(f"registre du fidéicommis illisible ({store.trust_error})")),
    }


def _month_ends(first: date, last: date) -> list[date]:
    out = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        following = date(year + (month == 12), month % 12 + 1, 1)
        out.append(following - timedelta(days=1))
        year, month = following.year, following.month
    return out


def _flat_results_delta(before: dict, after: dict) -> dict:
    """The deltas of two results statements, flattened, zeros dropped."""
    flat = {}
    for section in ("revenue", "expenses", "expenses_net"):
        keys = set(before[section]) | set(after[section])
        for key in sorted(keys, key=str):
            delta = after[section].get(key, 0) - before[section].get(key, 0)
            if delta:
                flat[f"{section}:{key}"] = delta
    for key in ("gst", "qst", "total_revenue", "total_expenses"):
        delta = after[key] - before[key]
        if delta:
            flat[key] = delta
    return flat


def _expected_results_delta(verdicts: list[Verdict], payloads: dict):
    """What the CSV implies for 6.5: gross amounts by revenue kind and expense
    category from the CSV's OWN columns; nets by category from the moved rows'
    stored nets; no TPS or TVQ moves."""
    try:
        csv_before, csv_after, moved_before, moved_after = [], [], [], []
        for verdict in verdicts:
            if verdict.state != PENDING or not ({"kind", "category"} & set(verdict.pending)):
                continue
            line = verdict.line
            base = {"id": f"ligne-{line.ligne}", "kind": line.expected["kind"],
                    "category": line.expected["category"], "amount": line.amount,
                    "direction": line.direction}
            csv_before.append(base)
            csv_after.append({**base, "kind": line.effective("kind"),
                              "category": line.effective("category")})
            row = {**verdict.doc, "id": verdict.doc.get("id") or line.entry_id}
            moved_before.append(row)
            moved_after.append(_merge(row, payloads.get(f"{TX}/{line.entry_id}")))
        gross = _flat_results_delta(al.results_statement(csv_before),
                                    al.results_statement(csv_after))
        net = _flat_results_delta(al.results_statement(moved_before),
                                  al.results_statement(moved_after))
    except Exception as exc:
        return Impossible(f"{type(exc).__name__}")
    expected = {k: v for k, v in gross.items() if not k.startswith(("expenses_net:", "gst", "qst"))}
    expected.update({k: v for k, v in net.items() if k.startswith("expenses_net:")})
    return expected


def _differences(before, after, path: str = "", out: Optional[list] = None,
                 limit: int = 12) -> list[str]:
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            if len(out) >= limit:
                break
            if key not in before:
                out.append(f"{path}{key} : absent → {after[key]!r}")
            elif key not in after:
                out.append(f"{path}{key} : {before[key]!r} → absent")
            else:
                _differences(before[key], after[key], f"{path}{key} / ", out, limit)
    elif before != after:
        out.append(f"{path.rstrip(' /')} : {before!r} → {after!r}")
    return out


_EQUAL_CONTROLS = ("6.1", "6.2", "6.3", "6.4")


def compare_controls(before: dict, after: dict, expected) -> tuple[list[str], list[str]]:
    """``(gaps, impossible)``: what moved, and what could not be computed.
    ``expected`` is the 6.5 delta the CSV implies, or ``None`` when 6.5 is
    only reported (a restoration)."""
    gaps, impossible = [], []
    for key in _EQUAL_CONTROLS:
        b, a = before.get(key), after.get(key)
        if isinstance(b, Impossible) or isinstance(a, Impossible):
            reason = (b if isinstance(b, Impossible) else a).reason
            impossible.append(f"§{key} impossible à calculer ({reason})")
        elif b != a:
            gaps.extend(f"§{key} — {d}" for d in _differences(b, a))
    b, a = before.get("6.5"), after.get("6.5")
    for value in (b, a, expected):
        if isinstance(value, Impossible):
            impossible.append(f"§6.5 impossible à calculer ({value.reason})")
            break
    else:
        if expected is not None:
            actual = _flat_results_delta(b, a)
            for key in sorted(set(actual) | set(expected)):
                if actual.get(key, 0) != expected.get(key, 0):
                    gaps.append(f"§6.5 — {key} : écart {actual.get(key, 0)}, attendu "
                                f"{expected.get(key, 0)}")
    for value in (before.get("10"), after.get("10")):
        if isinstance(value, Impossible):
            impossible.append(f"contrôle 10 du fidéicommis impossible à calculer ({value.reason})")
            break
    return gaps, impossible


def fee_candidate_changes(before, after) -> list[tuple[str, list, list]]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    return [(fee, before.get(fee, []), after.get(fee, []))
            for fee in sorted(set(before) | set(after))
            if before.get(fee, []) != after.get(fee, [])]


# ── the plan ──────────────────────────────────────────────────────────────


@dataclass
class Plan:
    csv_path: Path
    csv_sha: str
    project: str
    today: date
    lines: list
    excluded: list
    verdicts: list
    trust_checks: list
    writes: list
    payloads: dict
    fingerprint: str
    store: Store
    month_accounts: list
    months: list
    before: dict
    simulated: dict
    expected: Any
    gaps: list
    impossible: list
    fee_changes: list
    blocking: list
    observations: list


def _month_scope(store: Store, writes: list[Write], today: date) -> tuple[list, list]:
    moved = {w.doc_id for w in writes if w.role == "compte"}
    accounts = sorted(aid for aid, acc in store.accounts.items()
                      if acc.get("account_type") == "opérations" or aid in moved)
    days = [_day(row.get("date")) for row in store.rows.values()
            if row.get("account_id") in accounts]
    days = [d for d in days if d is not None]
    return accounts, (_month_ends(min(days), today) if days else [])


def _spelling_key(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = _WORD.findall(plain.casefold())
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)


def _observations(lines: list[Line]) -> list[str]:
    """Near-identical spellings of a counterparty — reported, never touched."""
    spellings: dict = {}
    for line in lines:
        name = line.effective("counterparty")
        if name:
            spellings.setdefault(_spelling_key(name), {}).setdefault(name, []).append(line.ligne)
    out = []
    for _key, names in sorted(spellings.items()):
        if len(names) > 1:
            listed = " et ".join(f"« {n} » (ligne(s) {', '.join(map(str, sorted(l)))})"
                                 for n, l in sorted(names.items()))
            out.append(f"Graphies voisines d'une contrepartie : {listed} — le script n'y "
                       f"touche pas.")
    return out


def prepare(csv_path) -> Plan:
    """Everything up to the write: read, judge, plan, simulate. Writes
    nothing. Raises :class:`RunRefused`."""
    csv_path = Path(csv_path)
    try:
        data = csv_path.read_bytes()
    except OSError as exc:
        raise RunRefused(f"CSV illisible ({type(exc).__name__}) : {csv_path}") from None
    csv_sha = hashlib.sha256(data).hexdigest()
    lines, excluded = parse_csv(data)
    today = today_mtl()
    store = read_store()
    dossiers: dict = {}
    verdicts = [judge_line(line, store, today, dossiers) for line in lines]
    trust_checks = check_trust(verdicts)
    writes = plan_writes(verdicts, store, excluded)
    payloads = build_payloads(writes, datetime.now(UTC), csv_sha,
                              lambda w: MOTIF.format(ligne=w.ligne))
    project = str(db.project)
    fingerprint = _fingerprint({
        "csv_sha256": csv_sha,
        "projet": project,
        "refusees": [{"ligne": v.line.ligne, "motifs": [code for code, _m in v.reasons]}
                     for v in verdicts if v.state == REFUSED],
        "exclues": [line.ligne for line in excluded],
    }, writes, payloads)

    month_accounts, months = _month_scope(store, writes, today)
    before = compute_controls(store.control_rows(), store.control_accounts(), store,
                              month_accounts, months)
    simulated = compute_controls(store.control_rows(payloads), store.control_accounts(payloads),
                                 store, month_accounts, months)
    expected = _expected_results_delta(verdicts, payloads)
    gaps, impossible = compare_controls(before, simulated, expected)
    blocking = list(impossible) + [f"contrôle en écart : {gap}" for gap in gaps]
    blocking += [f"ligne {c.ligne} : fidéicommis ILLISIBLE — {c.detail}"
                 for c in trust_checks if c.outcome == UNREADABLE]
    return Plan(
        csv_path=csv_path, csv_sha=csv_sha, project=project, today=today, lines=lines,
        excluded=excluded, verdicts=verdicts, trust_checks=trust_checks, writes=writes,
        payloads=payloads, fingerprint=fingerprint, store=store,
        month_accounts=month_accounts, months=months, before=before, simulated=simulated,
        expected=expected, gaps=gaps, impossible=impossible,
        fee_changes=fee_candidate_changes(before.get("10"), simulated.get("10")),
        blocking=blocking, observations=_observations(lines),
    )


# ── backup ────────────────────────────────────────────────────────────────


def _typed(value):
    """JSON with instants kept typed (``{"$ts": iso}``), recursively."""
    if isinstance(value, datetime):
        return {"$ts": _utc_iso(value)}
    if isinstance(value, dict):
        if "$ts" in value:
            raise TypeError("clé « $ts » réservée")
        return {str(k): _typed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_typed(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"valeur {type(value).__name__} non sauvegardable")


def _untyped(value):
    if isinstance(value, dict):
        if set(value) == {"$ts"}:
            return datetime.fromisoformat(value["$ts"])
        return {k: _untyped(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_untyped(v) for v in value]
    return value


def write_backup(directory: Path, plan: Plan) -> tuple[Path, str]:
    """Write, fsync, re-read and verify the backup; ``(path, sha256)``."""
    created = datetime.now(UTC)
    writes = sorted(plan.writes, key=lambda w: (w.ligne, w.path))
    documents = []
    for write in writes:
        payload = plan.payloads[write.path]
        documents.append({
            "chemin": write.path,
            "ligne": write.ligne,
            "role": write.role,
            "update_time_lu": _stamp_iso(write.snap.update_time),
            "etag_migration": payload["etag"],
            "champs": sorted(write.business),
            "avant": _typed(write.before),
            "apres": _typed({k: payload[k] for k in sorted(write.business)}),
        })
    body = {
        "format": BACKUP_FORMAT,
        "cree_le": created.isoformat(),
        "projet": plan.project,
        "csv": {"chemin": str(plan.csv_path), "sha256": plan.csv_sha},
        "empreinte": plan.fingerprint,
        "documents": documents,
        "contenu_sha256": _digest(documents),
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False)
    path = directory / (f"reclassement-administration-{created.strftime('%Y%m%dT%H%M%S%fZ')}"
                        f"-{plan.fingerprint}.json")
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    reread = path.read_bytes()
    parsed = json.loads(reread.decode("utf-8"))
    if parsed != json.loads(text) or parsed["contenu_sha256"] != _digest(parsed["documents"]):
        raise OSError("la sauvegarde relue ne concorde pas")
    for entry, write in zip(parsed["documents"], writes):
        if _untyped(entry["avant"]) != write.before:
            raise OSError(f"la sauvegarde relue de {write.path} ne concorde pas")
    return path, hashlib.sha256(reread).hexdigest()


# ── the batch ─────────────────────────────────────────────────────────────


def _landed(write: Write, payload: dict) -> Optional[bool]:
    """Re-read one document after a commit that raised: True when the write
    is there, False when it is not, None when the read fails too."""
    try:
        snap = db.collection(write.collection).document(write.doc_id).get()
    except Exception:
        return None
    return (snap.to_dict() or {}).get("etag") == payload["etag"]


def commit(writes: list[Write], payloads: dict) -> tuple[str, str]:
    """ONE batch, each document guarded by the update time it was read at.
    Returns ``(outcome, exception type)``."""
    batch = db.batch()
    for write in writes:
        batch.update(db.collection(write.collection).document(write.doc_id),
                     payloads[write.path],
                     option=db.write_option(last_update_time=write.snap.update_time))
    try:
        batch.commit()
    except Exception as exc:
        landed = [_landed(w, payloads[w.path]) for w in writes]
        if all(x is True for x in landed):
            return LOST_BUT_WRITTEN, type(exc).__name__
        if all(x is False for x in landed):
            return NOTHING, type(exc).__name__
        return UNCERTAIN, type(exc).__name__
    return WRITTEN, ""


def _outcome_sentence(outcome: str, error: str, count: int) -> str:
    if outcome == WRITTEN:
        return f"Écrit : {count} document(s), en un seul lot."
    if outcome == LOST_BUT_WRITTEN:
        return (f"Écrit ({count} document(s)) — la réponse du serveur s'était perdue "
                f"({error}), la relecture le confirme.")
    if outcome == NOTHING:
        return (f"Rien n'a été écrit ({error}) : un document a changé depuis la lecture, ou "
                f"le serveur a refusé. Relancez à blanc pour voir l'état actuel.")
    return (f"Issue INCERTAINE ({error}) : la relecture n'a pas pu trancher. Relancez à "
            f"blanc avant toute autre chose.")


def after_real(month_accounts: list, months: list) -> dict:
    """The controls on a fresh read, once written."""
    try:
        store = read_store()
    except RunRefused as exc:
        reason = Impossible(str(exc))
        return {key: reason for key in ("6.1", "6.2", "6.3", "6.4", "6.5", "10")}
    return compute_controls(store.control_rows(), store.control_accounts(), store,
                            month_accounts, months)


# ── the report ────────────────────────────────────────────────────────────


def _now_line() -> str:
    return to_mtl(datetime.now(UTC)).strftime("%Y-%m-%d %H:%M") + " (heure de Montréal)"


def _change_text(write: Write) -> str:
    return " ; ".join(f"{name} : {_show(old)} → {_show(new)}"
                      for name, (old, new) in write.changes.items())


def _trust_entry_text(entry: dict) -> str:
    parts = [
        str(entry.get("id")),
        _show(entry.get("date")),
        _money(entry.get("amount")),
        str(entry.get("direction") or "?"),
        trust.PURPOSE_LABELS.get(entry.get("purpose"), str(entry.get("purpose") or "?")),
        f"dossier {entry.get('dossier_file_number') or NONE_MARK}",
        f"client {entry.get('client_name') or NONE_MARK}",
        f"réf. {entry.get('reference') or NONE_MARK}",
        f"statut {entry.get('status') or '?'}",
    ]
    return " · ".join(parts)


def _render_groups(plan: Plan) -> list[str]:
    table: dict = {}
    outcome = {v.line.ligne: v.state for v in plan.verdicts}
    for line in plan.lines + plan.excluded:
        state = "exclue" if line.statut == EXCLUDED else outcome[line.ligne]
        amount = _as_int(line.amount) if line.statut == APPLY else None
        if amount is None:
            text = line.cells.get("montant_cents", "")
            amount = int(text) if _DIGITS.fullmatch(text or "") else 0
        for code, label in line.groups:
            slot = table.setdefault(code, {"label": label, "n": 0, "total": 0, PENDING: 0,
                                           DONE: 0, REFUSED: 0, "exclue": 0})
            slot["n"] += 1
            slot["total"] += amount
            slot[state] += 1
    out = ["| Groupe | Lignes | Total | À appliquer | Déjà appliquées | Refusées | Exclues |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for code in sorted(table):
        s = table[code]
        out.append(f"| {_cell(s['label'])} | {s['n']} | {_money(s['total'])} | {s[PENDING]} | "
                   f"{s[DONE]} | {s[REFUSED]} | {s['exclue']} |")
    return out


def _render_lines(plan: Plan) -> list[str]:
    writes = {w.path: w for w in plan.writes}
    out = ["| Ligne | Groupe | N° | Date | Montant | Issue | Détail |",
           "|---:|---|---:|---|---:|---|---|"]
    for verdict in plan.verdicts:
        line = verdict.line
        if verdict.state == REFUSED:
            detail = " ; ".join(f"`{code}` — {message}" for code, message in verdict.reasons)
        elif verdict.state == PENDING:
            parts = []
            for path in (f"{TX}/{line.entry_id}",
                         f"{TX}/{verdict.leg.get('id')}" if verdict.leg else ""):
                if path in writes and writes[path].changes:
                    prefix = "volet de carte — " if writes[path].role == "volet_carte" else ""
                    parts.append(prefix + _change_text(writes[path]))
            detail = " ; ".join(parts) or "—"
        else:
            detail = "—"
        out.append(f"| {line.ligne} | {_cell(line.cells['groupe'])} | {line.sequence} | "
                   f"{line.expected['date'].date()} | {_money(line.amount)} | {verdict.state} | "
                   f"{_cell(detail)} |")
    return out


def _render_trust(plan: Plan) -> list[str]:
    if not plan.trust_checks:
        return ["Aucune ligne de contrepartie « Compte en fidéicommis » à vérifier."]
    out = []
    lines = {v.line.ligne: v.line for v in plan.verdicts}
    for check in plan.trust_checks:
        how = ("par identifiant" if check.method == "identifiant"
               else f"par jour ({check.day})")
        line = lines[check.ligne]
        out.append(f"- **Ligne {check.ligne} — {check.outcome}** ({how} ; "
                   f"{line.direction} de {_money(line.amount)} au compte d'administration) : "
                   f"{check.detail}")
        for entry in check.matches:
            out.append(f"  - {_trust_entry_text(entry)}")
        for entry, why in check.aside:
            out.append(f"  - écartée ({why}) : {_trust_entry_text(entry)}")
        if check.outcome == INCOHERENT:
            out.append("  - la ligne est refusée : l'avocat tranche.")
        elif check.outcome == UNREADABLE:
            out.append("  - l'application est bloquée tant que la lecture échoue.")
    return out


def _render_card(plan: Plan) -> list[str]:
    out = []
    for verdict in plan.verdicts:
        if verdict.leg is None and not (verdict.doc or {}).get("kind") == "paiement_carte":
            continue
        line, doc, leg = verdict.line, verdict.doc or {}, verdict.leg or {}
        target = line.targets.get("date")
        out.append(f"- Ligne {line.ligne} ({verdict.state}) : écriture {line.entry_id} au compte "
                   f"{plan.store.account_label(doc.get('account_id'))}, "
                   f"{_show(doc.get('date'))} → {_show(target)}")
        if leg:
            checks = ("lien réciproque, même montant, même jour, sens opposé : vérifiés"
                      if verdict.leg_checked else
                      f"contrôles du volet : ÉCHEC — {verdict.leg_failure or 'non effectués'}")
            out.append(f"  - volet de carte {leg.get('id')} au compte "
                       f"{plan.store.account_label(leg.get('account_id'))}, "
                       f"{_show(leg.get('date'))} → {_show(target)} ; {checks}")
    return out or ["Aucun paiement de carte dans le CSV."]


def _status(before, after) -> str:
    if isinstance(before, Impossible) or isinstance(after, Impossible):
        return "IMPOSSIBLE"
    return "inchangé" if before == after else "ÉCART"


def _render_controls(plan: Plan, real: Optional[dict], store: Store) -> list[str]:
    columns = [("avant", plan.before), ("après (simulé)", plan.simulated)]
    if real is not None:
        columns.append(("après (réel)", real))
    head = " | ".join(name for name, _c in columns)
    out = []

    def values(key):
        return [c.get(key) for _n, c in columns]

    out.append("### 6.1 Soldes des comptes")
    out.append("")
    v = values("6.1")
    if any(isinstance(x, Impossible) for x in v):
        out.append(f"IMPOSSIBLE : {' ; '.join(x.reason for x in v if isinstance(x, Impossible))}")
    else:
        out.append(f"| Compte | ledger_balance {head} | Σ admin_delta {head} | Statut |")
        out.append("|---|" + "---:|" * (2 * len(columns)) + "---|")
        for aid in sorted(v[0], key=str):
            account = store.accounts.get(aid) or {}
            balances = [_money(x.get(aid, {}).get("ledger_balance")) for x in v]
            sums = [_money(x.get(aid, {}).get("somme", 0)) for x in v]
            shown = ""
            if account.get("account_type") == "carte_crédit":
                due = _as_int(v[0][aid].get("ledger_balance"))
                shown = f" — solde dû {_money(al.display_balance('carte_crédit', due))}" if due is not None else ""
            same = all(x.get(aid) == v[0].get(aid) for x in v)
            out.append(f"| {_cell(store.account_label(aid))}{shown} | {' | '.join(balances)} | "
                       f"{' | '.join(sums)} | {'inchangé' if same else 'ÉCART'} |")

    out.append("")
    out.append("### 6.2 Soldes de fin de mois")
    out.append("")
    v = values("6.2")
    if any(isinstance(x, Impossible) for x in v):
        out.append(f"IMPOSSIBLE : {' ; '.join(x.reason for x in v if isinstance(x, Impossible))}")
    elif not plan.months:
        out.append("Aucune écriture sur les comptes visés.")
    else:
        out.append(f"De {plan.months[0].isoformat()[:7]} à {plan.months[-1].isoformat()[:7]}.")
        out.append("")
        out.append(f"| Compte | Fin de mois | {head} | Statut |")
        out.append("|---|---|" + "---:|" * len(columns) + "---|")
        for aid in plan.month_accounts:
            for month in plan.months:
                key = month.isoformat()
                cells = [x.get(aid, {}).get(key) for x in v]
                out.append(f"| {_cell(store.account_label(aid))} | {key} | "
                           f"{' | '.join(_money(c) for c in cells)} | "
                           f"{'inchangé' if len(set(cells)) == 1 else 'ÉCART'} |")

    out.append("")
    out.append("### 6.3 Conciliations")
    out.append("")
    v = values("6.3")
    if any(isinstance(x, Impossible) for x in v):
        out.append(f"IMPOSSIBLE : {' ; '.join(x.reason for x in v if isinstance(x, Impossible))}")
    else:
        for aid in sorted(v[0], key=str):
            info = v[0][aid]
            last = info["derniere"]
            out.append(f"- {store.account_label(aid)} : plancher {info['plancher'] or NONE_MARK} ; "
                       f"dernière conciliation "
                       f"{(last['fin'] + ' (' + str(last['statut']) + ')') if last else NONE_MARK} ; "
                       f"{_status(v[0][aid], v[-1].get(aid))}")
            for rec_id, proof in sorted(info["preuves"].items()):
                after = [x.get(aid, {}).get("preuves", {}).get(rec_id) for x in v[1:]]
                out.append(f"  - conciliation {rec_id} : book_balance inscrit "
                           f"{_money(proof['book_balance'])}, recalculé {_money(proof['recalcule'])}, "
                           f"écart {_money(proof['ecart'])} — "
                           f"{'inchangé' if all(a == proof for a in after) else 'ÉCART'}")

    out.append("")
    out.append("### 6.4 Factures")
    out.append("")
    v = values("6.4")
    if any(isinstance(x, Impossible) for x in v):
        out.append(f"IMPOSSIBLE : {' ; '.join(x.reason for x in v if isinstance(x, Impossible))}")
    else:
        paid = sum(_as_int(i.get("paye")) or 0 for i in v[0].values())
        balance = sum(_as_int(i.get("solde")) or 0 for i in v[0].values())
        out.append(f"{len(v[0])} facture(s) ; montant payé total {_money(paid)} ; solde total "
                   f"{_money(balance)} — {_status(v[0], v[-1]) if all(x == v[0] for x in v) else 'ÉCART'}")
        for x in v[1:]:
            for diff in _differences(v[0], x):
                out.append(f"- ÉCART {diff}")

    out.append("")
    out.append("### 6.5 État des résultats (tout le journal)")
    out.append("")
    v = values("6.5")
    if any(isinstance(x, Impossible) for x in v) or isinstance(plan.expected, Impossible):
        reasons = [x.reason for x in v + [plan.expected] if isinstance(x, Impossible)]
        out.append(f"IMPOSSIBLE : {' ; '.join(reasons)}")
    else:
        # One delta per column after « avant » — the real one included once
        # written: « conforme » only when EVERY delta is the CSV's.
        deltas = [_flat_results_delta(v[0], x) for x in v[1:]]
        expected = plan.expected or {}
        delta_head = " | ".join(["Écart (simulé)", "Écart (réel)"][:len(deltas)])
        out.append(f"| Poste | {head} | {delta_head} | Écart attendu (CSV) | Statut |")
        out.append("|---|" + "---:|" * (len(columns) + len(deltas) + 1) + "---|")
        rows = []
        for section, label in (("revenue", "Revenus"), ("expenses", "Dépenses (montant)"),
                               ("expenses_net", "Dépenses (net)")):
            for key in sorted(set().union(*(x[section] for x in v)), key=str):
                rows.append((f"{label} — {key or '(sans catégorie)'}", f"{section}:{key}",
                             [x[section].get(key, 0) for x in v]))
        for key, label in (("gst", "TPS"), ("qst", "TVQ"), ("total_revenue", "Total des revenus"),
                           ("total_expenses", "Total des dépenses")):
            rows.append((label, key, [x[key] for x in v]))
        for label, flat_key, cells in rows:
            moved = [d.get(flat_key, 0) for d in deltas]
            wanted = expected.get(flat_key, 0)
            out.append(f"| {_cell(label)} | {' | '.join(_money(c) for c in cells)} | "
                       f"{' | '.join(_money(m) for m in moved)} | {_money(wanted)} | "
                       f"{'conforme' if all(m == wanted for m in moved) else 'ÉCART'} |")

    out.append("")
    out.append("### Contrôle 10 du fidéicommis (simulé)")
    out.append("")
    v = values("10")
    if any(isinstance(x, Impossible) for x in v):
        out.append(f"IMPOSSIBLE : {' ; '.join(x.reason for x in v if isinstance(x, Impossible))}")
    elif not plan.fee_changes:
        out.append("Aucun paiement d'honoraires ne voit changer l'ensemble de ses recettes "
                   "candidates.")
    else:
        for fee, was, now in plan.fee_changes:
            out.append(f"- paiement d'honoraires {fee} : recettes candidates "
                       f"{', '.join(was) or NONE_MARK} → {', '.join(now) or NONE_MARK}")
    return out


def render_report(plan: Plan, *, mode: str, approved: Optional[str] = None,
                  refusal: Optional[list] = None, outcome: Optional[str] = None,
                  backup: Optional[tuple] = None, real: Optional[dict] = None) -> str:
    counts = {state: sum(1 for v in plan.verdicts if v.state == state)
              for state in (PENDING, DONE, REFUSED)}
    roles = {role: sum(1 for w in plan.writes if w.role == role)
             for role in ("écriture", "volet_carte", "compte")}
    out = [
        "# Reclassement du journal d'administration",
        "",
        f"- Date : {_now_line()}",
        f"- Mode : {mode}",
        f"- Projet : `{plan.project}`",
        f"- CSV : `{plan.csv_path}`",
        f"- SHA-256 du CSV : `{plan.csv_sha}`",
        f"- Empreinte : **{plan.fingerprint}**",
    ]
    if approved is not None:
        out.append(f"- Empreinte approuvée : `{approved}` — "
                   f"{'concorde' if approved == plan.fingerprint else 'DIFFÈRE'}")
    trust_counts = {}
    for check in plan.trust_checks:
        trust_counts[check.outcome] = trust_counts.get(check.outcome, 0) + 1
    out += [
        "",
        "## Bilan",
        "",
        f"- {counts[PENDING]} ligne(s) à appliquer, {counts[DONE]} déjà appliquée(s), "
        f"{counts[REFUSED]} refusée(s), {len(plan.excluded)} exclue(s).",
        f"- {len(plan.writes)} document(s) à écrire : {roles['écriture']} écriture(s), "
        f"{roles['volet_carte']} volet(s) de carte, {roles['compte']} compte(s) (tampons seuls).",
        "- Fidéicommis : " + (", ".join(f"{n} {o}" for o, n in sorted(trust_counts.items()))
                              or "aucune ligne"),
        "- Contrôles : " + ("conformes" if not plan.gaps and not plan.impossible else
                            f"{len(plan.gaps)} écart(s), {len(plan.impossible)} impossible(s)"),
    ]
    if plan.blocking:
        out.append("- **L'application serait refusée :**")
        out += [f"  - {_cell(b)}" for b in plan.blocking]
    if refusal:
        out.append("- **Application refusée — rien n'a été écrit :**")
        out += [f"  - {_cell(r)}" for r in refusal]
    out += ["", "## Groupes", ""] + _render_groups(plan)
    out += ["", "## Lignes", ""] + _render_lines(plan)
    out += ["", "## Lignes exclues (groupe E)", ""]
    if plan.excluded:
        out += ["Reprises telles quelles du CSV ; le script n'y touche pas.", "", "```csv",
                ",".join(CSV_COLUMNS)] + [line.raw for line in plan.excluded] + ["```"]
    else:
        out.append("Aucune.")
    out += ["", "## Fidéicommis", ""] + _render_trust(plan)
    out += ["", "## Paiement de carte", ""] + _render_card(plan)
    out += ["", "## Contrôles (§6)", ""] + _render_controls(plan, real, plan.store)
    out += ["", "## Sauvegarde", ""]
    if backup:
        out.append(f"`{backup[0]}` (SHA-256 `{backup[1]}`) — écrite, synchronisée, relue et "
                   f"vérifiée avant le lot.")
    else:
        out.append("Aucune (rien n'a été écrit).")
    if outcome is not None:
        out += ["", "## Écriture", "", outcome]
    if real is not None:
        drift = _differences(plan.simulated, real)
        gaps, impossible = compare_controls(plan.before, real, plan.expected)
        out.append("")
        out.append("- Contrôles après réel : " + ("conformes (avant = après)" if not gaps and not impossible
                                                  else "; ".join(gaps + impossible)))
        out.append("- Écart à la passe approuvée : " + ("aucun (après réel = après simulé)"
                                                        if not drift else "; ".join(drift)))
    out += ["", "## Observations", ""]
    observations = list(plan.observations)
    for check in plan.trust_checks:
        line = next(v.line for v in plan.verdicts if v.line.ligne == check.ligne)
        day = line.effective("date").date()
        if check.method == "identifiant":
            for entry in check.matches:
                if _day(entry.get("date")) != day:
                    observations.append(f"Ligne {check.ligne} : l'écriture du fidéicommis "
                                        f"{entry.get('id')} est du {_day(entry.get('date'))}, "
                                        f"l'écriture d'administration du {day}.")
            continue
        named = sorted({d for d in _ISO_DAY.findall(line.cells["verification_prealable"])
                        if d != day.isoformat()})
        if named:
            observations.append(f"Ligne {check.ligne} : la vérification préalable nomme le "
                                f"{', '.join(named)} ; la recherche au fidéicommis porte sur "
                                f"le jour de l'écriture, le {day}.")
    out += [f"- {o}" for o in observations] or ["Aucune."]
    return "\n".join(out) + "\n"


# ── restoration ───────────────────────────────────────────────────────────


@dataclass
class RestorePlan:
    backup_path: Path
    backup_sha: str
    created: str
    project: str
    writes: list
    payloads: dict
    refusals: list          # [(path, line, code, message)]
    fingerprint: str
    store: Store
    month_accounts: list
    months: list
    before: dict
    simulated: dict
    gaps: list
    impossible: list
    blocking: list


def _load_backup(path: Path) -> tuple[dict, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RunRefused(f"sauvegarde illisible ({type(exc).__name__}) : {path}") from None
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RunRefused(f"sauvegarde illisible (JSON) : {path}") from None
    if not isinstance(body, dict) or body.get("format") != BACKUP_FORMAT:
        raise RunRefused(f"sauvegarde d'un autre format : {path}")
    documents = body.get("documents")
    if not isinstance(documents, list) or body.get("contenu_sha256") != _digest(documents):
        raise RunRefused(f"sauvegarde altérée (son contenu ne concorde plus avec son "
                         f"empreinte) : {path}")
    return body, hashlib.sha256(data).hexdigest()


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(iso)


def prepare_restore(backup_path) -> RestorePlan:
    backup_path = Path(backup_path)
    body, backup_sha = _load_backup(backup_path)
    project = str(db.project)
    if body.get("projet") != project:
        raise RunRefused(f"sauvegarde du projet « {body.get('projet')} », base « {project} » — "
                         f"refusé")
    today = today_mtl()
    store = read_store()
    refusals: list = []
    writes: list[Write] = []
    moved: dict = {}
    for entry in sorted(body["documents"], key=lambda d: (d.get("ligne") or 0, str(d.get("chemin")))):
        if entry.get("role") not in ("écriture", "volet_carte"):
            continue
        path, ligne = str(entry.get("chemin")), entry.get("ligne")
        collection, _sep, doc_id = path.partition("/")
        fields = entry.get("champs")

        def refuse(code: str, message: str) -> None:
            refusals.append((path, ligne, code, message))

        if (collection != TX or not _addressable(doc_id) or not isinstance(fields, list)
                or not set(fields) <= set(BUSINESS_FIELDS) or not isinstance(ligne, int)):
            refuse("sauvegarde_illisible", "entrée de sauvegarde mal formée")
            continue
        current = store.rows.get(doc_id)
        if current is None:
            refuse("écriture_introuvable", "écriture introuvable")
            continue
        if current.get("etag") != entry.get("etag_migration"):
            refuse("etag_déplacé", f"modifiée depuis la migration (etag {current.get('etag')}, "
                                   f"la migration a écrit {entry.get('etag_migration')})")
            continue
        before, after = _untyped(entry.get("avant") or {}), _untyped(entry.get("apres") or {})
        moved_fields = [f for f in fields if current.get(f) != after.get(f)]
        if moved_fields:
            refuse("valeur_déplacée", f"{', '.join(moved_fields)} ne porte(nt) plus la valeur "
                                      f"de la migration")
            continue
        if len(current.get("revisions") or []) + 1 > al._REVISIONS_CAP:
            refuse("fil_plein", "le fil de révisions est plein — rien ne se rogne")
            continue
        values = {f: before[f] if f in before else firestore.DELETE_FIELD for f in fields}
        if "date" in fields:
            old_day = _day(before.get("date"))
            if old_day is None:
                refuse("date_illisible", "date sauvegardée illisible")
                continue
            problems = _date_refusals(current, old_day, store, today)
            for code, message in problems:
                refuse(code, message)
            if problems:
                continue
            moved.setdefault(current.get("account_id"), ligne)
        changes = {f: [current.get(f), before.get(f)] for f in fields}
        writes.append(Write(path, TX, doc_id, ligne, entry["role"], store.entries[doc_id],
                            current, values, changes))
    for account_id, ligne in sorted(moved.items()):
        if account_id not in store.account_snaps:
            refusals.append((f"{ACCOUNTS}/{account_id}", ligne, "compte_introuvable",
                             "compte introuvable"))
            continue
        writes.append(Write(f"{ACCOUNTS}/{account_id}", ACCOUNTS, account_id, ligne, "compte",
                            store.account_snaps[account_id], store.accounts[account_id], {}, {}))
    if refusals:
        writes = []   # all or nothing
    quand = _when(body.get("cree_le"))
    payloads = build_payloads(writes, datetime.now(UTC), backup_sha,
                              lambda w: RESTORE_MOTIF.format(quand=quand, ligne=w.ligne))
    fingerprint = _fingerprint({
        "sauvegarde_sha256": backup_sha,
        "projet": project,
        "refus": [{"chemin": p, "ligne": l, "motif": c} for p, l, c, _m in refusals],
    }, writes, payloads)
    month_accounts, months = _month_scope(store, writes, today)
    before = compute_controls(store.control_rows(), store.control_accounts(), store,
                              month_accounts, months)
    simulated = compute_controls(store.control_rows(payloads), store.control_accounts(payloads),
                                 store, month_accounts, months)
    gaps, impossible = compare_controls(before, simulated, None)
    blocking = [f"{p} (ligne {l}) : `{c}` — {m}" for p, l, c, m in refusals]
    blocking += list(impossible) + [f"contrôle en écart : {g}" for g in gaps]
    return RestorePlan(
        backup_path=backup_path, backup_sha=backup_sha, created=quand, project=project,
        writes=writes, payloads=payloads, refusals=refusals, fingerprint=fingerprint,
        store=store, month_accounts=month_accounts, months=months, before=before,
        simulated=simulated, gaps=gaps, impossible=impossible, blocking=blocking,
    )


def render_restore_report(plan: RestorePlan, *, mode: str, approved: Optional[str] = None,
                          refusal: Optional[list] = None, outcome: Optional[str] = None,
                          real: Optional[dict] = None) -> str:
    out = [
        "# Restauration du reclassement du journal d'administration",
        "",
        f"- Date : {_now_line()}",
        f"- Mode : {mode}",
        f"- Projet : `{plan.project}`",
        f"- Sauvegarde : `{plan.backup_path}` (du {plan.created})",
        f"- SHA-256 de la sauvegarde : `{plan.backup_sha}`",
        f"- Empreinte : **{plan.fingerprint}**",
    ]
    if approved is not None:
        out.append(f"- Empreinte approuvée : `{approved}` — "
                   f"{'concorde' if approved == plan.fingerprint else 'DIFFÈRE'}")
    out += ["", "## Documents", ""]
    if plan.refusals:
        out.append("**Restauration refusée en entier** (tout ou rien) :")
        out += [f"- {p} (ligne {l}) : `{c}` — {_cell(m)}" for p, l, c, m in plan.refusals]
    elif not plan.writes:
        out.append("Rien à restaurer.")
    for write in plan.writes:
        text = _change_text(write) if write.changes else "tampons seuls (aucun solde)"
        out.append(f"- {write.path} (ligne {write.ligne}, {write.role}) : {_cell(text)}")
    if plan.blocking and not plan.refusals:
        out += ["", "**La restauration serait refusée :**"] + [f"- {_cell(b)}" for b in plan.blocking]
    if refusal:
        out += ["", "**Restauration refusée — rien n'a été écrit :**"] + [f"- {_cell(r)}" for r in refusal]
    out += ["", "## Contrôles", ""]
    columns = [("avant", plan.before), ("après (simulé)", plan.simulated)]
    if real is not None:
        columns.append(("après (réel)", real))
    for key in _EQUAL_CONTROLS + ("10",):
        statuses = [_status(plan.before.get(key), c.get(key)) for _n, c in columns[1:]]
        out.append(f"- §{key} : {', '.join(f'{n} {s}' for (n, _c), s in zip(columns[1:], statuses))}")
    # For information only (a restoration has no expected delta) — but
    # every column after « avant », the real one included once written.
    b = plan.before.get("6.5")
    for name, column in columns[1:]:
        after = column.get("6.5")
        if isinstance(b, Impossible) or isinstance(after, Impossible):
            out.append(f"- §6.5 ({name}) : IMPOSSIBLE")
            continue
        delta = _flat_results_delta(b, after)
        out.append(f"- §6.5 ({name}, pour information) : " + ("aucun écart" if not delta else
                   "; ".join(f"{k} {_money(v)}" for k, v in delta.items())))
    if outcome is not None:
        out += ["", "## Écriture", "", outcome]
    return "\n".join(out) + "\n"


# ── the command line ──────────────────────────────────────────────────────


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # French, exit 1 like every refusal here
        raise SystemExit(f"Arguments invalides ({message}).")


def _outside_repo(raw: str, what: str) -> Path:
    path = Path(raw)
    resolved = path.resolve()
    if resolved.is_relative_to(REPO.resolve()):
        raise RunRefused(f"{what} « {raw} » est dans le dépôt (public) — choisissez un "
                         f"chemin hors du dépôt")
    return resolved


def _check_paths(args) -> tuple[Path, Optional[Path], Optional[Path], Optional[Path]]:
    """Every path is judged BEFORE any file is created."""
    if not args.rapport:
        raise RunRefused("--rapport est requis")
    if bool(args.csv) == bool(args.restaurer):
        raise RunRefused("donnez soit le CSV, soit --restaurer SAUVEGARDE.json")
    if args.appliquer is not None and not _HEX16.fullmatch(args.appliquer):
        raise RunRefused("--appliquer attend l'empreinte de la passe à blanc approuvée "
                         "(16 caractères hexadécimaux)")
    if args.restaurer and args.sauvegarde:
        raise RunRefused("--sauvegarde ne sert qu'au reclassement, pas à --restaurer")
    if args.csv and args.appliquer and not args.sauvegarde:
        raise RunRefused("--appliquer exige --sauvegarde DOSSIER (hors du dépôt)")
    report = _outside_repo(args.rapport, "le rapport")
    if report.exists():
        raise RunRefused(f"le rapport « {report} » existe déjà — choisissez un autre nom")
    if not report.parent.is_dir():
        raise RunRefused(f"le dossier du rapport « {report.parent} » n'existe pas")
    backup_dir = None
    if args.sauvegarde:
        backup_dir = _outside_repo(args.sauvegarde, "le dossier de sauvegarde")
        if not backup_dir.is_dir():
            raise RunRefused(f"le dossier de sauvegarde « {backup_dir} » n'existe pas")
    source = None
    if args.csv:
        source = _outside_repo(args.csv, "le CSV")
    restore = None
    if args.restaurer:
        restore = _outside_repo(args.restaurer, "la sauvegarde")
    return report, backup_dir, source, restore


def _write_report(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _finish(report: Path, text: str, summary: list[str], code: int) -> int:
    for line in summary:
        _out(line)
    try:
        _write_report(report, text)
        _out(f"Rapport : {report}")
    except OSError as exc:
        _out(f"Rapport NON écrit ({type(exc).__name__}) : {report}")
        return 1
    return code


def _summary(plan: Plan, mode: str) -> list[str]:
    counts = {s: sum(1 for v in plan.verdicts if v.state == s) for s in (PENDING, DONE, REFUSED)}
    return [
        f"Reclassement du journal d'administration — {mode}",
        f"Projet : {plan.project}",
        f"CSV : {plan.csv_path} (SHA-256 {plan.csv_sha})",
        f"Lignes : {counts[PENDING]} à appliquer, {counts[DONE]} déjà appliquée(s), "
        f"{counts[REFUSED]} refusée(s), {len(plan.excluded)} exclue(s).",
        f"Documents à écrire : {len(plan.writes)}.",
        "Contrôles : " + ("conformes" if not plan.gaps and not plan.impossible else
                          f"{len(plan.gaps)} écart(s), {len(plan.impossible)} impossible(s)"),
        f"Empreinte : {plan.fingerprint}",
    ]


def _run_migration(report: Path, source: Path, backup_dir: Optional[Path],
                   approved: Optional[str]) -> int:
    try:
        plan = prepare(source)
    except RunRefused as exc:
        return _finish(report, f"# Reclassement du journal d'administration\n\n- Date : "
                               f"{_now_line()}\n- CSV : `{source}`\n\n**Refusé — rien n'a été "
                               f"écrit** : {exc}\n",
                       [f"Refusé — rien n'a été écrit : {exc}"], 1)
    if approved is None:
        summary = _summary(plan, "à blanc")
        summary.append("À blanc : rien n'a été écrit. Pour écrire, après approbation : "
                       f"--sauvegarde DOSSIER --appliquer {plan.fingerprint}")
        if plan.blocking:
            summary.append(f"L'application serait refusée ({len(plan.blocking)} motif(s), voir "
                           f"le rapport).")
        return _finish(report, render_report(plan, mode="à blanc"), summary, 0)

    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé depuis la passe approuvée ({approved} → "
                       f"{plan.fingerprint}) : relancez à blanc et faites-la approuver")
    refusal += plan.blocking
    if refusal:
        summary = _summary(plan, "application refusée")
        summary += [f"Refusé — rien n'a été écrit : {r}" for r in refusal]
        return _finish(report, render_report(plan, mode="application refusée", approved=approved,
                                             refusal=refusal), summary, 1)
    if not plan.writes:
        summary = _summary(plan, "application") + ["Rien à écrire : chaque ligne est déjà "
                                                   "appliquée, refusée ou exclue."]
        return _finish(report, render_report(plan, mode="application", approved=approved,
                                             outcome="Rien à écrire."), summary, 0)
    try:
        backup = write_backup(backup_dir, plan)
    except Exception as exc:
        message = f"sauvegarde impossible ({type(exc).__name__}) — rien n'a été écrit"
        return _finish(report, render_report(plan, mode="application refusée", approved=approved,
                                             refusal=[message]),
                       _summary(plan, "application refusée") + [f"Refusé : {message}"], 1)
    outcome, error = commit(plan.writes, plan.payloads)
    sentence = _outcome_sentence(outcome, error, len(plan.writes))
    # Said at once: whatever happens next, the outcome is never lost.
    _out(sentence)
    real = None
    if outcome in (WRITTEN, LOST_BUT_WRITTEN):
        real = after_real(plan.month_accounts, plan.months)
    summary = _summary(plan, "application") + [f"Sauvegarde : {backup[0]}"]
    if real is not None:
        gaps, impossible = compare_controls(plan.before, real, plan.expected)
        drift = _differences(plan.simulated, real)
        summary.append("Contrôles après réel : " + ("conformes" if not gaps and not impossible
                                                    else f"{len(gaps) + len(impossible)} écart(s)"))
        summary.append("Écart à la passe approuvée : " + ("aucun" if not drift else
                                                          f"{len(drift)} différence(s)"))
        summary.append("Relancez les deux contrôles d'intégrité (scripts.verify_admin_integrity, "
                       "scripts.verify_trust_integrity) et comparez leurs constats par texte.")
    text = render_report(plan, mode="application", approved=approved, outcome=sentence,
                         backup=backup, real=real)
    return _finish(report, text, summary, 0 if outcome in (WRITTEN, LOST_BUT_WRITTEN) else 1)


def _run_restore(report: Path, source: Path, approved: Optional[str]) -> int:
    try:
        plan = prepare_restore(source)
    except RunRefused as exc:
        return _finish(report, f"# Restauration du reclassement\n\n- Date : {_now_line()}\n"
                               f"- Sauvegarde : `{source}`\n\n**Refusé — rien n'a été écrit** : "
                               f"{exc}\n",
                       [f"Refusé — rien n'a été écrit : {exc}"], 1)
    head = [f"Restauration — {'à blanc' if approved is None else 'application'}",
            f"Projet : {plan.project}",
            f"Sauvegarde : {plan.backup_path} (SHA-256 {plan.backup_sha})",
            f"Documents à écrire : {len(plan.writes)} ; refus : {len(plan.refusals)}.",
            f"Empreinte : {plan.fingerprint}"]
    if approved is None:
        head.append("À blanc : rien n'a été écrit. Pour restaurer, après approbation : "
                    f"--appliquer {plan.fingerprint}")
        return _finish(report, render_restore_report(plan, mode="restauration à blanc"), head,
                       1 if plan.refusals else 0)
    refusal = []
    if approved != plan.fingerprint:
        refusal.append(f"l'empreinte a changé ({approved} → {plan.fingerprint}) : relancez à "
                       f"blanc")
    refusal += plan.blocking
    if not refusal and not plan.writes:
        refusal.append("rien à restaurer")
    if refusal:
        return _finish(report, render_restore_report(plan, mode="restauration refusée",
                                                     approved=approved, refusal=refusal),
                       head + [f"Refusé — rien n'a été écrit : {r}" for r in refusal], 1)
    outcome, error = commit(plan.writes, plan.payloads)
    sentence = _outcome_sentence(outcome, error, len(plan.writes))
    _out(sentence)
    real = None
    if outcome in (WRITTEN, LOST_BUT_WRITTEN):
        real = after_real(plan.month_accounts, plan.months)
    text = render_restore_report(plan, mode="restauration", approved=approved, outcome=sentence,
                                 real=real)
    return _finish(report, text, head, 0 if outcome in (WRITTEN, LOST_BUT_WRITTEN) else 1)


def main(argv: Optional[list[str]] = None) -> int:
    parser = _Parser(prog="python -m scripts.reclassify_admin_ledger",
                     description="Reclassement du journal d'administration (hors modèle, "
                                 "à blanc par défaut).")
    parser.add_argument("csv", nargs="?", help="le CSV approuvé (hors du dépôt public)")
    parser.add_argument("--rapport", metavar="RAPPORT.md",
                        help="le rapport à écrire (hors du dépôt ; nouveau fichier)")
    parser.add_argument("--sauvegarde", metavar="DOSSIER",
                        help="le dossier où écrire la sauvegarde avant le lot (hors du dépôt)")
    parser.add_argument("--appliquer", metavar="EMPREINTE",
                        help="écrire — avec l'empreinte de la passe à blanc approuvée")
    parser.add_argument("--restaurer", metavar="SAUVEGARDE.json",
                        help="restaurer les champs sauvegardés (à blanc sans --appliquer)")
    args = parser.parse_args(argv if argv is not None else [])
    try:
        report, backup_dir, source, restore = _check_paths(args)
    except RunRefused as exc:
        _out(f"Refusé — rien n'a été créé ni écrit : {exc}")
        return 1
    if restore is not None:
        return _run_restore(report, restore, args.appliquer)
    return _run_migration(report, source, backup_dir, args.appliquer)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
