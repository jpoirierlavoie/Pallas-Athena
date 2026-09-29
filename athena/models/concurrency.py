"""Optimistic concurrency: an edit commits only against the version it read.

Every editable record carries an ``etag`` (Architecture Rule 7), regenerated
by every model write (``models.provenance.update_fields`` is the one stamp
that writes it, and ``tests/test_provenance.py`` sweeps the models for any
other). Until 2026-09-25 only the DAV layer compared it — its ``If-Match``
check (``dav/carddav.py``, ``dav/dossier_collections.py``) reads the record
and compares BEFORE calling the model, outside any transaction, so a write
landing in between still wins — and the web forms and the connector
compared nothing: every full-document ``set()`` was last-write-wins, so a
stale browser tab, the phone and the connector silently erased each other's
edits. The web forms now carry the etag they were rendered from
(``routes/edit_conflict.py``). (Routing that DAV check through ``expected_etag`` would make it
atomic; it is not done here, and DAV keeps the legacy path — bar a note
PUT that changes the text, which ``note.update_note`` guards itself, below.)

The contract, for a caller that passes ``expected_etag``:

* the write happens only if the stored etag is STILL the one the caller
  read — checked INSIDE a Firestore transaction, on a read made before any
  write is staged (reads before writes: the real client refuses the other
  order), so nothing can land between the check and the commit;
* otherwise :class:`StaleWrite` is raised and NOTHING is written — not the
  document, not an extra write that travels with it;
* a transaction that loses a race at commit (``Aborted``) is re-run by the
  real ``firestore.transactional`` decorator, whose re-read then sees the
  new etag and refuses. The retry can never turn into a blind overwrite.

``expected_etag=None`` is the LEGACY path, byte for byte: the same single
``set()`` (or ``update()``) the model performed before this module existed,
no read, no transaction. DAV PUTs and a web page rendered before its form
carried an etag take it, so their behaviour is unchanged.

Two models step outside these helpers on purpose:
``time_entry.update_time_entry`` and ``expense.update_expense`` (lot 0b,
2026-09-26) read, check and write in ONE transaction of their own even when
``expected_etag`` is ``None``. The race they close — an invoice committing
between their read and their full-document write, whose ``invoiced`` flip
the write would undo — is not a matter of which version the caller read,
so no caller can opt out of it. They still compare with :func:`matches`.

A third keeps the legacy path only when it has nothing to snapshot:
``note.update_note`` (D17, 2026-09-27) keeps a revision of every change of
a note's CONTENT, and a revision travels only on the guarded branch — so
when a caller that passed ``None`` (a DAV PUT, a page older than its etag
field) changes the text, the model passes the etag it has just read as
``expected_etag`` itself, and re-reads and re-merges on a lost race
instead of refusing: the caller still asserts nothing and still wins,
and the snapshot is the text actually overwritten. A write that leaves
the content unchanged is the plain legacy ``set()``.

``''`` is a legitimate expected etag: it matches a legacy document written
before Rule 7, whose stored etag is absent (a caller that read such a row
was handed ``''`` by the connector, and must be able to write it back).

``read_etag`` is the etag of the copy the MODEL read to build the document
it is about to write. The models compare it to ``expected_etag`` before
they merge anything (:func:`matches`), so the two are equal on the path
through here — the second comparison is defence in depth for a caller that
did not, since a document built from an older read than the live one would
resurrect whatever changed in between.

No composite index: a keyed ``get()`` and a keyed ``set()``/``update()``.
Pure of any model import — models import it, never the reverse.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Sequence

from google.cloud import firestore

# The French refusal a model returns in its error list. One constant, so a
# caller can recognise the refusal (``is_stale``) without parsing French.
STALE_ETAG_ERROR = (
    "Cet élément a été modifié entre-temps — dans l'application, sur le "
    "téléphone ou par le connecteur. Vos changements n'ont PAS été "
    "enregistrés : rechargez-le, puis refaites la modification."
)


# The refusal a model returns when the read its write depends on FAILED
# (finitions, sync-3). Before it, the four update_* re-read through their
# fail-open getter and answered a Firestore blip with « … introuvable » —
# which the DAV PUT mapped to 422 « Données invalides. » (a permanent refusal
# to DavX5) and the connector reported about a record that exists. One
# constant, so DAV (503 + Retry-After) and the connector (reason
# ``read_unavailable``) recognise it (``is_read_unavailable``) without
# parsing French.
READ_UNAVAILABLE_ERROR = (
    "Cet élément n'a pas pu être lu — une panne passagère. Rien n'a été "
    "enregistré : réessayez dans un instant."
)


def is_read_unavailable(errors: Optional[Iterable[str]]) -> bool:
    """True when a model's error list is the failed-read refusal."""
    return READ_UNAVAILABLE_ERROR in list(errors or ())


# A create whose write RAISED (finitions, robustness-1). A write RPC that
# fails does not prove the write was not applied — a DeadlineExceeded or an
# UNAVAILABLE can follow a server-side commit, the answer lost — so the
# creators that mint a fresh uuid4 no longer answer « Erreur lors de la
# sauvegarde. Veuillez réessayer. » on the exception alone: they READ THE ID
# BACK (settle_failed_create). Present → the write landed, and the create
# proceeds as committed; absent → the plain save error, true now; the read
# failing too → this constant, which the connector raises with keep_claim
# (a same-key retry must never write a second record) and the web shows as
# is. The invoice has its own (models.invoice.CREATE_OUTCOME_UNCERTAIN).
WRITE_OUTCOME_UNCERTAIN_ERROR = (
    "L'enregistrement n'a pas pu être confirmé : il a peut-être été fait. "
    "Vérifiez-le avant de le refaire — ne le recréez pas à l'aveugle."
)

WRITE_LANDED = "landed"
WRITE_ABSENT = "absent"
WRITE_UNKNOWN = "unknown"


def is_outcome_uncertain(errors: Optional[Iterable[str]]) -> bool:
    """True when a model's error list is the uncertain-create answer."""
    return WRITE_OUTCOME_UNCERTAIN_ERROR in list(errors or ())


def settle_failed_create(ref) -> tuple[str, Optional[dict]]:
    """``(outcome, stored)`` after the write to *ref* RAISED.

    *ref* must name a document id the caller MINTED fresh for this very
    write (a uuid4): a document found there can then only be this call's —
    its write landed and only the answer was lost. :data:`WRITE_LANDED`
    with the stored dict, :data:`WRITE_ABSENT` (nothing was written), or
    :data:`WRITE_UNKNOWN` when the read-back fails too. Never raises.
    """
    try:
        snap = ref.get()
    except Exception:
        return WRITE_UNKNOWN, None
    if snap.exists:
        return WRITE_LANDED, snap.to_dict() or {}
    return WRITE_ABSENT, None


class StaleWrite(Exception):
    """The stored etag is not the one the caller read: nothing was written."""


class Vanished(Exception):
    """The document disappeared between the model's read and its commit."""


def etag_of(doc: Optional[dict]) -> str:
    """The stored etag of *doc*, ``''`` when it has none (a legacy row)."""
    return str((doc or {}).get("etag") or "")


def matches(doc: Optional[dict], expected_etag: Optional[str]) -> bool:
    """True when *doc* is the version *expected_etag* names.

    ``None`` means « the caller asserts nothing » and always matches — the
    legacy path. The models call this right after their own read, so a
    stale caller is refused before any validation runs: the useful answer
    to an outdated view is « re-read », not the first field error the
    outdated view happens to trip.
    """
    return expected_etag is None or etag_of(doc) == str(expected_etag)


def is_stale(errors: Optional[Iterable[str]]) -> bool:
    """True when a model's error list is the concurrency refusal."""
    return STALE_ETAG_ERROR in list(errors or ())


def _check_live(
    snap, expected_etag: str, read_etag: Optional[str]
) -> None:
    if not snap.exists:
        raise Vanished()
    live = etag_of(snap.to_dict())
    if live != expected_etag or (read_etag is not None and live != read_etag):
        raise StaleWrite()


def _run_checked(
    ref,
    expected_etag: str,
    read_etag: Optional[str],
    stage: Callable[[Any], None],
) -> None:
    """Re-read *ref* in a transaction, compare, then *stage* the writes.

    The client is the reference's own (``ref._client``): a transaction must
    belong to the client its references were built from, and taking it from
    the reference is what keeps a test that patches one model's ``db`` from
    reaching another client's transaction.
    """
    transaction = ref._client.transaction()

    @firestore.transactional
    def _body(txn) -> None:
        # The read comes FIRST: the real client raises ReadAfterWriteError on
        # a transactional read after a staged write, and the check is only
        # worth something if it sees the store as the commit will.
        _check_live(ref.get(transaction=txn), expected_etag, read_etag)
        stage(txn)

    _body(transaction)


def commit_document(
    ref,
    document: dict,
    *,
    expected_etag: Optional[str],
    read_etag: Optional[str] = None,
    extra_sets: Sequence[tuple[Any, dict]] = (),
) -> None:
    """Write *document* at *ref* (a full ``set()``), guarded by the etag.

    *extra_sets* — ``(reference, data)`` pairs — commit together with the
    document or not at all on the guarded path (an analysis journal entry,
    a revision snapshot). On the legacy path they are written first, one by
    one, then the document: the order the models used before this module,
    kept exactly (« le journal AVANT le cache »).

    Raises :class:`StaleWrite` or :class:`Vanished` on the guarded path;
    any other exception is a store failure, left for the caller to log.
    """
    if expected_etag is None:
        for extra_ref, data in extra_sets:
            extra_ref.set(data)
        ref.set(document)
        return

    def _stage(txn) -> None:
        for extra_ref, data in extra_sets:
            txn.set(extra_ref, data)
        txn.set(ref, document)

    _run_checked(ref, str(expected_etag), read_etag, _stage)


def commit_delete(
    ref,
    *,
    expected_etag: Optional[str],
    read_etag: Optional[str] = None,
    extra_sets: Sequence[tuple[Any, dict]] = (),
) -> None:
    """Delete *ref*, guarded by the etag — the twin of :func:`commit_document`.

    *extra_sets* commit with the delete or not at all on the guarded path:
    the write-once snapshot of what disappears (``models/note.delete_note``
    for the théorie de la cause) must never exist without the delete, nor
    the delete without it. On the legacy path they are written first, then
    the document is deleted.

    Raises :class:`StaleWrite` or :class:`Vanished` on the guarded path;
    any other exception is a store failure, left for the caller to log.
    """
    if expected_etag is None:
        for extra_ref, data in extra_sets:
            extra_ref.set(data)
        ref.delete()
        return

    def _stage(txn) -> None:
        for extra_ref, data in extra_sets:
            txn.set(extra_ref, data)
        txn.delete(ref)

    _run_checked(ref, str(expected_etag), read_etag, _stage)


def commit_fields(
    ref,
    fields: dict,
    *,
    expected_etag: Optional[str],
    read_etag: Optional[str] = None,
) -> None:
    """Partial ``update()`` of *fields* at *ref*, guarded by the etag.

    The twin of :func:`commit_document` for the writers whose guarantee is
    the SHAPE of their write — the phase reclassifiers, which must never
    turn into a full-document ``set()``. The key set staged is exactly
    *fields*, on both paths.
    """
    if expected_etag is None:
        ref.update(fields)
        return
    _run_checked(
        ref, str(expected_etag), read_etag, lambda txn: txn.update(ref, fields)
    )
