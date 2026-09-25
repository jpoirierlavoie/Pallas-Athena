"""Optimistic concurrency: an edit commits only against the version it read.

Every editable record carries an ``etag`` (Architecture Rule 7), regenerated
by every model write (``models.provenance.update_fields`` is the one stamp
that writes it, and ``tests/test_provenance.py`` sweeps the models for any
other). Until 2026-09-25 nothing compared it: every full-document ``set()``
was last-write-wins, so a stale browser tab, the phone and the connector
silently erased each other's edits.

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
no read, no transaction. DAV PUTs and the web forms that do not carry an
etag yet take it, so their behaviour is unchanged.

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
