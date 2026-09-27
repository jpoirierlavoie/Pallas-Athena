"""Write-once revision snapshots: replaced prose is never lost.

Plan rule 4 (« History »). When a write REPLACES a prose value — a note's
content, a bloc of the théorie de la cause, a full rewrite — the value it
replaces is first snapshotted into ``{parent_collection}/{parent_id}/
revisions/{uuid4}``. Nothing in the application could undo a replaced note
until this module: the full-document ``set()`` models overwrite, and the
previous text was gone.

Its callers live in ``models/note.py``: ``update_note``, which snapshots
the content it replaces on EVERY path — the web form, the DAV PUT from the
phone, the connector's ``append_to_note``, ``update_note`` and
``edit_analyse`` (D17, 2026-09-27; until then only the connector's tools
asked, through ``revision=``, which now merely names the field) — and
``delete_note``, which snapshots the « Théorie de la cause » before
deleting it (lot 1a, step L3). The module shipped inert in lot 0a so that
its doctrine was pinned before its first caller.

A note revision always holds the note's WHOLE previous content, whatever
``field`` names: ``field`` says what the write replaced (one bloc, the
whole text), and a restore then needs nothing but the snapshot.

How a revision is written — and why this module writes NOTHING itself
---------------------------------------------------------------------
:func:`build_revision` returns a ``(reference, data)`` pair and stops. The
caller stages it inside ITS transaction or batch, next to the replacement
it records — ``models.concurrency.commit_document(extra_sets=[…])`` is the
intended path, and on its guarded branch the snapshot and the replacement
commit together or not at all. A revision written on its own could land
while the replacement it describes is refused (a stale etag): the history
would then record a change that never happened. Staying out of the write
path is what makes that impossible here, and ``tests/test_revision.py``
pins it by sweeping this module for any write call.

Only the GUARDED branch is atomic: with ``expected_etag=None``,
``commit_document`` keeps the legacy order and writes its ``extra_sets``
first, one by one, then the document — a replacement failing there would
leave its revision behind. So a revision always travels on the guarded
branch: ``update_note`` guards on the caller's etag, or — for a caller that
named none (a DAV PUT, a page older than its etag field) — on the etag it
has just read, re-reading if a write beats it there. ``tests/test_revision.py``
refuses any caller that builds a revision without committing it through
``commit_document`` — or, for the snapshot a DELETE leaves,
``commit_delete`` — with a non-None ``expected_etag``.

The shape — a documented exception to Architecture Rule 7
---------------------------------------------------------
``created_at`` only: no ``updated_at``, no ``etag``. A revision is
WRITE-ONCE, like the ``documents/{id}/analyses`` journal it generalizes
(``models/document.py``): no verb of this module updates or deletes one,
and a source sweep keeps it that way. Rule 6 holds — the ids are UUIDv4.
``previous_etag``/``new_etag`` are DATA (the version replaced, the version
written), not this document's own concurrency token, so the provenance
sweep (``tests/test_provenance.py``, which matches the literal key
``etag``) correctly ignores them.

``via``/``tool`` come from ``models.provenance`` — the path writing now and,
under the connector, the tool — so the history can say that a replacement
was Claude's without trusting any caller to declare it.

When the NOTE is deleted — the decision lot 1 had to take
---------------------------------------------------------
Its revisions are KEPT (lot 1a, step L3; recorded in CLAUDE.md). Firestore
does not cascade subcollections, so a revision outlives its parent, and
nothing in the application removes one — the ``documents/{id}/analyses``
journal doctrine. The « Théorie de la cause » goes further: its deletion
itself leaves a snapshot (field :data:`DELETE_FIELD`, ``new_etag`` empty —
no version follows a delete), committed in the same transaction as the
delete. A revision carries the full prior text, which is privileged: that
is the price of « nothing replaced is lost », and it is why the sweep in
``tests/test_revision.py`` still refuses any other module that reaches
this subcollection by name, and any blind cascade (``recursive_delete``,
a ``collections()`` walk) — a purge can only arrive as a new, deliberate
decision.

Reads
-----
:func:`list_revisions` fails OPEN (it serves a history display, never a
guard) and orders on ``created_at`` alone — the automatic single-field
index of the subcollection, no composite index. By default it PROJECTS the
snapshot text away: a listing shows when, by which path and how long; the
text of one revision is :func:`get_revision`'s job, which RAISES on a read
failure, because a restore must work from the truth or not at all.
:func:`count_revisions` is one ``COUNT`` aggregation (no document read, no
index) for the note page's « Versions précédentes : N » line, failing open
to ``None`` — never to a ``0`` that would claim nothing was kept.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from models import aggregation_values, db, provenance
from utils.logging_setup import log_unexpected

SUBCOLLECTION = "revisions"

# A Firestore document is capped at 1 MiB. At the UTF-8 worst case of four
# bytes a character, 250 000 characters is 1 000 000 bytes — the rest of
# the record fits in the remaining ~48 KiB. Note content is capped at
# 100 000 characters (``models/note.CONTENT_MAX_LENGTH``), so a real note
# never comes near; the cap exists so an oversized snapshot is REFUSED
# before it is staged, rather than failing the whole commit it travels in.
MAX_SNAPSHOT_CHARS = 250_000

# The parents a revision may hang under. Closed on purpose: a typo here
# would file privileged prose under a collection nothing ever reads. A
# later lot adds its collection in the same commit as its first caller.
VALID_PARENT_COLLECTIONS: tuple[str, ...] = ("notes",)

# What was replaced. ``content`` — a note's whole body; ``content:rewrite``
# — the théorie de la cause rewritten in one piece; ``bloc:…`` — one bloc
# of it (the heading block ``entete``, then A to H); ``content:delete`` —
# the note itself was deleted (the snapshot of the text that disappeared).
DELETE_FIELD = "content:delete"
VALID_FIELDS: tuple[str, ...] = (
    "content",
    "content:rewrite",
    "bloc:entete",
    *(f"bloc:{letter}" for letter in "ABCDEFGH"),
    DELETE_FIELD,
)

# The keys a listing returns when it leaves the text out.
_LISTING_FIELDS: tuple[str, ...] = (
    "id", "parent_collection", "parent_id", "field", "previous_length",
    "previous_etag", "new_etag", "via", "tool", "created_at",
)


class RevisionRefused(ValueError):
    """The snapshot cannot be built — nothing was staged."""


def _check_id(value: Any, name: str) -> str:
    """A document id this module may address — never a path in disguise.

    The client builds a reference from ANY string, and a ``/`` in it would
    silently address a nested document instead of refusing.
    """
    if not isinstance(value, str) or not value:
        raise RevisionRefused(f"{name} must be a non-empty string")
    if "/" in value or value in (".", "..") or (
        value.startswith("__") and value.endswith("__")
    ):
        raise RevisionRefused(f"{name} is not a valid document id")
    return value


def _check_parent(parent_collection: Any, parent_id: Any) -> tuple[str, str]:
    if parent_collection not in VALID_PARENT_COLLECTIONS:
        raise RevisionRefused(
            f"unsupported parent collection: {parent_collection!r}"
        )
    return parent_collection, _check_id(parent_id, "parent_id")


def _revisions_ref(parent_collection: str, parent_id: str):
    return (
        db.collection(parent_collection)
        .document(parent_id)
        .collection(SUBCOLLECTION)
    )


def build_revision(
    *,
    parent_collection: str,
    parent_id: str,
    field: str,
    previous_value: str,
    previous_etag: str,
    new_etag: str,
    now: datetime,
) -> tuple[Any, dict]:
    """The revision of ONE replacement, ready to stage — never written here.

    *previous_etag* is the etag of the version being replaced (``''`` for a
    legacy record written before Rule 7), *new_etag* the one the
    replacement stamps: together they chain a revision to the two versions
    it separates. For :data:`DELETE_FIELD` no version follows, so
    *new_etag* must be ``''`` — and for every other field it must not be.
    *now* must be timezone-aware (Architecture Rule 5).

    Raises :class:`RevisionRefused` — and stages nothing — on an unknown
    parent or field, an invalid id, a non-string value, or a value above
    :data:`MAX_SNAPSHOT_CHARS`.
    """
    parent_collection, parent_id = _check_parent(parent_collection, parent_id)
    if field not in VALID_FIELDS:
        raise RevisionRefused(f"unsupported revision field: {field!r}")
    if not isinstance(previous_value, str):
        raise RevisionRefused("previous_value must be a string")
    if len(previous_value) > MAX_SNAPSHOT_CHARS:
        raise RevisionRefused(
            f"previous_value exceeds {MAX_SNAPSHOT_CHARS} characters"
        )
    if not isinstance(previous_etag, str) or not isinstance(new_etag, str):
        raise RevisionRefused("previous_etag and new_etag must be strings")
    if field == DELETE_FIELD:
        if new_etag:
            # A delete writes no version: an etag here would claim one.
            raise RevisionRefused("a delete snapshot names no new version")
    elif not new_etag:
        # A replacement always stamps a fresh etag; an empty one means the
        # caller built the revision before stamping the document.
        raise RevisionRefused("new_etag must name the version written")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RevisionRefused("now must be a timezone-aware datetime")

    revision_id = str(uuid.uuid4())
    data = {
        "id": revision_id,
        "parent_collection": parent_collection,
        "parent_id": parent_id,
        "field": field,
        "previous_value": previous_value,
        "previous_length": len(previous_value),
        "previous_etag": previous_etag,
        "new_etag": new_etag,
        "via": provenance.current_via(),
        "tool": provenance.current_tool(),
        "created_at": now,
    }
    ref = _revisions_ref(parent_collection, parent_id).document(revision_id)
    return ref, data


def list_revisions(
    parent_collection: str,
    parent_id: str,
    *,
    limit: int = 20,
    include_values: bool = False,
) -> list[dict]:
    """The parent's revisions, newest first. Fails OPEN to ``[]``.

    One single-field ``order_by("created_at")`` over the parent's own
    subcollection: the automatic index serves it. Without
    *include_values* the snapshot text is projected away (see the module
    docstring); ``limit`` is clamped to 1..100.
    """
    try:
        parent_collection, parent_id = _check_parent(
            parent_collection, parent_id
        )
    except RevisionRefused:
        return []
    bounded = max(1, min(int(limit or 20), 100))
    try:
        query = _revisions_ref(parent_collection, parent_id)
        if not include_values:
            query = query.select(list(_LISTING_FIELDS))
        snaps = (
            query.order_by("created_at", direction="DESCENDING")
            .limit(bounded)
            .stream()
        )
        return [s.to_dict() or {} for s in snaps]
    except Exception:
        log_unexpected(
            "revision listing failed", parent_collection=parent_collection,
        )
        return []


def count_revisions(parent_collection: str, parent_id: str) -> Optional[int]:
    """How many revisions the parent holds, or ``None`` when unknown.

    ONE ``COUNT`` aggregation over the parent's own subcollection — no
    document read, no composite index (an unfiltered count needs none).
    Fails OPEN to ``None``, never ``0``: it serves a display line
    (« Versions précédentes : N » on the note page), and a failed read must
    not claim that nothing was kept.
    """
    try:
        parent_collection, parent_id = _check_parent(
            parent_collection, parent_id
        )
    except RevisionRefused:
        return None
    try:
        results = (
            _revisions_ref(parent_collection, parent_id)
            .count(alias="n")
            .get()
        )
        return int(aggregation_values(results).get("n", 0) or 0)
    except Exception:
        log_unexpected(
            "revision count failed", parent_collection=parent_collection,
        )
        return None


def get_revision(
    parent_collection: str, parent_id: str, revision_id: str,
) -> Optional[dict]:
    """One revision, text included; ``None`` when it does not exist.

    RAISES on a read failure (and :class:`RevisionRefused` on a malformed
    id): a restore works from the stored truth or not at all.
    """
    parent_collection, parent_id = _check_parent(parent_collection, parent_id)
    revision_id = _check_id(revision_id, "revision_id")
    snap = _revisions_ref(parent_collection, parent_id).document(
        revision_id
    ).get()
    if not snap.exists:
        return None
    return snap.to_dict() or {}
