"""Provenance: WHO wrote a record last, stamped by the model that writes it.

Every Firestore document carries ``created_at``/``updated_at``/``etag``
(Architecture Rule 7). This module adds the missing fact — by which path the
write came — as three more keys, set by the SAME helpers that regenerate the
etag, so one can never move without the other:

* ``created_via`` — the path that created the record;
* ``updated_via`` — the path of the LAST write (it moves with the etag);
* ``mcp_updated_at`` — the instant of the last write made through the MCP
  connector. It is STICKY: a later web or DAV write never clears it, so
  « Claude a modifié ceci le … » survives the next phone sync.

The value domain is closed: :data:`VALID_VIA`. ``''`` (absent) on a record
means « not recorded » — written before this module was deployed (before
lot 1a for a protocol step, the last kind of record to start stamping) —
never « written by the application ». Deliberately no date: the boundary is the DEPLOY, not
the commit, and nothing here can know when that happens.

Why the stamp lives in the MODEL and not in the callers. The models merge
``{**existing, **data}`` then write the whole document, so a caller-set
``updated_via`` would survive, stale, into the NEXT write made by anyone
else: after one connector edit, a later web save would still read
« modifié par Claude ». Only the function that regenerates the etag knows
that a new write is happening, so it is the only place the answer can be
true. ``tests/test_provenance.py`` sweeps every model statement that writes
an ``etag`` and requires it to write ``updated_via`` too.

How the path is known. :func:`writing_via` sets an explicit override in a
ContextVar — ``mcp.write_support.run_write`` wraps every connector write in
``writing_via("mcp", tool=…)``. Without an override the path is DERIVED, at
read time, from the Flask request's blueprint: a blueprint defined in the
``dav`` package is ``dav``; the machine blueprints of ``routes/taches_*.py``
(Cloud Tasks and cron dispatches) are ``cron``; the ``mcp`` package is
``mcp`` (a write reached there outside ``run_write`` is still the
connector's, and saying « application » would be false); any other request
is ``web``; no request at all is ``script``. Deriving it at read time, rather
than in a new ``before_request`` hook, keeps this module off the security
chain entirely.

The commit record. :func:`note_commit` is called by a mutator right after its
Firestore write succeeds; inside a :func:`writing_via` block it appends
``(collection, doc_id)`` to a per-block list that :func:`committed_writes`
reads. It is the STRUCTURAL commit point — the write protocol can tell « the
model committed, then something after it failed » from « nothing was
written » without trusting every handler to announce it. Outside a
``writing_via`` block it records nothing (a thread-local list that grew
across requests would be a leak, not a record). A NESTED block opens its own
record and, on exit — normal or not — hands what it noted to the enclosing
one: a commit made inside the inner block happened inside the outer one too,
and a record that forgot it would tell the write protocol « nothing was
written » about a write that was. Over-reporting a commit only costs a
retry refusal; under-reporting one invites a duplicate.

One kind of commit is recorded APART (lot 2A, T8): a write a retry
REPRODUCES rather than repeats — an ensure on a deterministic id, the
« Projets » system folder a generation creates on first use
(``models/folder.ensure_system_folder``). ``note_commit(…, idempotent=True)``
keeps it out of :func:`committed_writes`: a call that fails after such a
write ALONE has committed nothing a retry would write twice, and reporting
« ENREGISTRÉE — NE PAS RÉESSAYER » (naming a folder as « the write ») would
forbid the one retry that is safe — the generation whose upload failed
after « Projets » was created. :func:`idempotent_writes` lists them.

Pure: no Firestore, no model import. Flask is imported lazily and only to
read the current request.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from datetime import datetime
from typing import Iterator, Optional

VALID_VIA: tuple[str, ...] = ("web", "dav", "mcp", "cron", "script")

# (via, tool) when an explicit writer is declared; None otherwise.
_OVERRIDE: contextvars.ContextVar[Optional[tuple[str, str]]] = (
    contextvars.ContextVar("provenance_override", default=None)
)
# The commits noted inside the innermost writing_via block; None outside.
# Each entry is ``(collection, doc_id, idempotent)`` — see note_commit.
_COMMITS: contextvars.ContextVar[Optional[list[tuple[str, str, bool]]]] = (
    contextvars.ContextVar("provenance_commits", default=None)
)


def via_for_blueprint_module(import_name: str) -> str:
    """Classify a Flask blueprint by the MODULE that defines it.

    By module and not by blueprint name, because the module is the
    structural fact: every blueprint written in the ``dav`` package speaks
    DAV whatever it is called, and a new machine blueprint lands in a
    ``routes/taches_*.py`` file by the house convention. (Note that
    ``routes/tasks.py`` — the web task list, mounted under ``/taches`` — is
    ``web``: the match is on the module, never on the URL.)
    """
    name = import_name or ""
    head = name.split(".", 1)[0]
    if head == "dav":
        return "dav"
    if name.startswith("routes.taches_"):
        return "cron"
    if head == "mcp":
        return "mcp"
    return "web"


def _request_via() -> str:
    try:
        from flask import current_app, has_request_context, request
    except Exception:  # pragma: no cover — Flask is a hard dependency
        return "script"
    if not has_request_context():
        return "script"
    blueprint = request.blueprint
    bp = current_app.blueprints.get(blueprint) if blueprint else None
    return via_for_blueprint_module(bp.import_name if bp is not None else "")


def current_via() -> str:
    """The path writing now: the explicit override, else the request's."""
    override = _OVERRIDE.get()
    if override is not None:
        return override[0]
    return _request_via()


def current_tool() -> str:
    """The MCP tool writing now, or ``''`` outside a declared tool call."""
    override = _OVERRIDE.get()
    return override[1] if override is not None else ""


@contextlib.contextmanager
def writing_via(via: str, *, tool: str = "") -> Iterator[None]:
    """Declare the writer for the block, and open a fresh commit record.

    Nested blocks override and restore. The reset runs in ``finally``, so
    an exception inside the block never leaves the override behind for the
    next request served by the same thread. Also in ``finally``: the
    commits a nested block noted are appended to the enclosing block's
    record — the exception path is precisely the one where the outer reader
    must still see them.
    """
    if via not in VALID_VIA:
        raise ValueError(f"unknown provenance via: {via!r}")
    enclosing = _COMMITS.get()
    inner: list[tuple[str, str, bool]] = []
    override_token = _OVERRIDE.set((via, str(tool or "")))
    commits_token = _COMMITS.set(inner)
    try:
        yield
    finally:
        _COMMITS.reset(commits_token)
        _OVERRIDE.reset(override_token)
        if enclosing is not None:
            enclosing.extend(inner)


def update_fields(now: datetime) -> dict:
    """The stamp of ONE write, for a partial ``update()`` or a spread.

    ``updated_at``, a fresh ``etag``, ``updated_via`` — and, when the
    connector is the writer, ``mcp_updated_at``. Absent otherwise, never
    ``None``: on a partial update an absent key leaves the stored value
    alone, which is exactly what makes the field sticky.
    """
    via = current_via()
    fields = {
        "updated_at": now,
        "etag": str(uuid.uuid4()),
        "updated_via": via,
    }
    if via == "mcp":
        fields["mcp_updated_at"] = now
    return fields


def create_fields(now: datetime) -> dict:
    """The stamp of a CREATION, for a document built as a dict literal.

    :func:`update_fields` plus ``created_at`` and ``created_via`` (the path
    actually writing). A creator that merges caller data carrying its own
    ``created_via`` uses :func:`stamp_create` instead, which honours it.
    """
    fields = update_fields(now)
    fields["created_at"] = now
    fields["created_via"] = fields["updated_via"]
    return fields


def stamp_update(doc: dict, now: datetime) -> dict:
    """Stamp a full document about to be ``set()`` — in place; returns it.

    Never touches ``created_*`` and never removes ``mcp_updated_at``: a
    merged document keeps the value it carried.
    """
    doc.update(update_fields(now))
    return doc


def stamp_create(
    doc: dict, now: datetime, *, created_at: Optional[datetime] = None,
) -> dict:
    """Stamp a document about to be created — in place; returns it.

    ``created_at``/``updated_at``/``etag`` plus both vias. ``created_at`` is
    *now* unless the caller passes the one it deliberately preserves (a
    creator that keeps a client-supplied creation instant says so at the
    call site, never implicitly here). An explicit, VALID ``created_via``
    already in *doc* is kept (the connector's creators have always named
    themselves); anything else — absent, empty, or a value outside
    :data:`VALID_VIA` — is replaced by the path actually writing.
    ``updated_via`` is always the path actually writing.
    """
    explicit = doc.get("created_via")
    doc.update(update_fields(now))
    doc["created_at"] = created_at or now
    doc["created_via"] = (
        explicit if explicit in VALID_VIA else doc["updated_via"]
    )
    return doc


def note_commit(collection: str, doc_id: str, *, idempotent: bool = False) -> None:
    """Record that a write to ``collection/doc_id`` has COMMITTED.

    Called by a mutator right after its Firestore write returns. A no-op
    outside a :func:`writing_via` block.

    ``idempotent=True`` — a write a retry REPRODUCES, never repeats (an
    ensure on a deterministic id): recorded, but left out of
    :func:`committed_writes` (see the module docstring). Only a mutator
    whose second run provably writes NOTHING new may pass it.
    """
    commits = _COMMITS.get()
    if commits is not None:
        commits.append((str(collection), str(doc_id or ""), bool(idempotent)))


def committed_writes() -> tuple[tuple[str, str], ...]:
    """The commits noted so far in the innermost :func:`writing_via` block
    — those a retry would REPEAT (the idempotent ones are left out).

    A copy: a reader can never edit the record. Empty outside a block.
    """
    commits = _COMMITS.get()
    if commits is None:
        return ()
    return tuple((c, i) for c, i, idem in commits if not idem)


def idempotent_writes() -> tuple[tuple[str, str], ...]:
    """The commits noted with ``idempotent=True`` in the innermost block."""
    commits = _COMMITS.get()
    if commits is None:
        return ()
    return tuple((c, i) for c, i, idem in commits if idem)
