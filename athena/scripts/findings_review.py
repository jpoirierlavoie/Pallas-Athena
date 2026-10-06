"""Findings of the two register integrity scripts: keys, reviews, report.

``scripts/verify_trust_integrity`` and ``scripts/verify_admin_integrity``
end the same way: écarts (exit 1), notes (exit 2), or nothing (exit 0). A
note is history — an entry the model would refuse today — and history is
never rewritten, so the same notes came back on every run and a NEW one was
easy to miss among them. This module lets the lawyer record, once, that a
finding was reviewed, without the scripts ever hiding it:

* every finding prints with a KEY: ten hex digits of a SHA-256 over the
  finding's text AND the stored document of every register entry the text
  names by id. A review covers exactly what was seen — if the entry changes
  afterwards (a reversal, a repair) or the finding's figures do, the key
  changes and the finding comes back as new;
* ``--revue FICHIER`` names a JSON file listing the reviewed keys, each with
  the date of the review and its reason. The file names entry ids and the
  lawyer's reasons, so it stays OUT of the public repository, with the
  signed memo in the trust records;
* a reviewed finding still prints, under « Constats déjà revus », with its
  date and reason; it no longer counts toward the exit code. A review whose
  key matches no finding (the finding disappeared, or changed) is listed so
  the file can be pruned;
* only HISTORY can be reviewed: every note, and an écart its check marks
  :class:`Reviewable` (a gap in the trust numbering: the register is
  incomplete, which only the lawyer can explain or repair). A figure that
  does not add up — a balance, a reconciliation that no longer re-proves —
  never can: a review naming one is reported and ignored;
* an unreadable or malformed review file is an écart, and then no finding
  is taken as reviewed: a review that cannot be read must not pass a run.

The file::

    [
      {"cle": "3f2a91c0d4", "revu_le": "2026-10-06",
       "motif": "Honoraires de l'avocate-conseil payés directement — voulu."}
    ]

(a JSON list, or an object whose ``"revues"`` key holds one). Read-only:
nothing here writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Iterable, Optional

#: Hex digits of a finding's key. 40 bits: no collision among the few dozen
#: findings a register produces, short enough to copy by hand.
KEY_LENGTH = 10

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_RE = re.compile(r"[0-9a-f]{%d}" % KEY_LENGTH)
_MOTIF_MAX = 1000


class Reviewable(str):
    """An écart that is history, not a figure: a review may acknowledge it.

    Notes are always reviewable; an écart is only when its check builds it
    with this type. It IS a ``str`` — every caller that prints, joins or
    searches findings keeps working unchanged."""


def finding_key(text: str, entries: dict) -> str:
    """The key of a finding: its text, plus the stored document of each
    entry it names by id (``entries`` maps id → document as read)."""
    digest = hashlib.sha256(text.encode("utf-8"))
    for entry_id in sorted(set(_UUID_RE.findall(text))):
        doc = entries.get(entry_id)
        if doc is None:
            continue
        digest.update(b"\x00" + entry_id.encode("ascii") + b"\x00")
        digest.update(
            json.dumps(doc, sort_keys=True, default=str, ensure_ascii=False)
            .encode("utf-8")
        )
    return digest.hexdigest()[:KEY_LENGTH]


def _is_day(value) -> bool:
    """A ``YYYY-MM-DD`` string naming a real calendar day."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def load_reviews(path: str) -> tuple[dict[str, dict], list[str]]:
    """``(reviews by key, errors)``. Any error voids EVERY review: a file
    that cannot be trusted whole is not trusted at all."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, [f"fichier de revue introuvable : {path}"]
    except (OSError, ValueError) as exc:
        return {}, [f"fichier de revue illisible ({type(exc).__name__}) : {path}"]
    if isinstance(data, dict):
        data = data.get("revues")
    if not isinstance(data, list):
        return {}, [
            "fichier de revue mal formé : attendu une liste de revues (ou un "
            "objet dont « revues » est la liste)"
        ]
    reviews: dict[str, dict] = {}
    errors: list[str] = []
    for i, item in enumerate(data):
        where = f"revue n° {i + 1}"
        if not isinstance(item, dict):
            errors.append(f"{where} : un objet est attendu")
            continue
        key = item.get("cle")
        when = item.get("revu_le")
        motif = item.get("motif")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            errors.append(
                f"{where} : « cle » doit être les {KEY_LENGTH} chiffres "
                f"hexadécimaux imprimés entre crochets devant le constat"
            )
            continue
        if not _is_day(when):
            errors.append(f"{where} ({key}) : « revu_le » doit être une date AAAA-MM-JJ")
            continue
        if not isinstance(motif, str) or not motif.strip() or len(motif) > _MOTIF_MAX:
            errors.append(
                f"{where} ({key}) : « motif » est requis (au plus {_MOTIF_MAX} caractères)"
            )
            continue
        if key in reviews:
            errors.append(f"{where} : la clé {key} figure deux fois")
            continue
        reviews[key] = {"revu_le": when, "motif": motif.strip()}
    if errors:
        return {}, errors
    return reviews, []


def report(
    problems: Iterable[str],
    notes: Iterable[str],
    *,
    entries: dict,
    review_path: Optional[str],
    clean_line: str,
) -> int:
    """Print the run's findings with their keys, apply the reviews, and
    return the exit code: 1 when an écart is not reviewed (a review file
    that cannot be read is one), 2 when only notes are, 0 otherwise."""
    problems = list(problems)
    notes = list(notes)
    reviews: dict[str, dict] = {}
    if review_path:
        reviews, errors = load_reviews(review_path)
        problems = [
            f"{e} — aucun constat n'a été tenu pour revu" for e in errors
        ] + problems

    used: set[str] = set()
    open_problems: list[tuple[str, str]] = []
    open_notes: list[tuple[str, str]] = []
    reviewed: list[tuple[str, str]] = []
    refused: list[tuple[str, str]] = []
    for text in problems:
        key = finding_key(text, entries)
        if key in reviews and isinstance(text, Reviewable):
            used.add(key)
            reviewed.append((key, text))
            continue
        if key in reviews:
            used.add(key)
            refused.append((key, text))
        open_problems.append((key, text))
    for text in notes:
        key = finding_key(text, entries)
        if key in reviews:
            used.add(key)
            reviewed.append((key, text))
        else:
            open_notes.append((key, text))

    print()
    if open_problems:
        print(f"❌ {len(open_problems)} écart(s) détecté(s) :")
        for key, text in open_problems:
            print(f"   - [{key}] {text}")
    else:
        print(clean_line)
    for key, _text in refused:
        print(
            f"   (la revue {key} ne s'applique pas : un chiffre qui ne concorde "
            f"pas ne se marque jamais comme revu — il se corrige)"
        )
    if open_notes:
        print()
        print(
            f"Notes à revoir avec l'avocat ({len(open_notes)}) — l'historique "
            f"n'est jamais réécrit ; chaque ligne est une décision, pas une "
            f"réparation :"
        )
        for key, text in open_notes:
            print(f"   - [{key}] {text}")
    if reviewed:
        print()
        print(f"Constats déjà revus ({len(reviewed)}) — {review_path} :")
        for key, text in reviewed:
            review = reviews[key]
            print(f"   - [{key}] {text}")
            print(f"     revu le {review['revu_le']} — {review['motif']}")
    stale = sorted(k for k in reviews if k not in used)
    if stale:
        print()
        print(
            f"Revues sans constat correspondant ({len(stale)}) — le constat a "
            f"disparu, ou il a changé (l'écriture a été modifiée depuis) : à "
            f"retirer du fichier, ou à refaire :"
        )
        for key in stale:
            print(f"   - {key} (revu le {reviews[key]['revu_le']}) — {reviews[key]['motif']}")
    if (open_problems or open_notes) and not review_path:
        print()
        print(
            "Un constat revu se consigne dans un fichier passé par --revue "
            "(clé entre crochets, date, motif — format : scripts/findings_review.py)."
        )
    if open_problems:
        return 1
    return 2 if open_notes else 0
