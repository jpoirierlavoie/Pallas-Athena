"""Serve a DAV collection's member readers from in-memory rows.

Since the finitions (sync-1) ``dav/dossier_collections._collection_members``
reads through the STRICT model readers — a failed read raises and the
collection answers 503, never an empty 207 — so a test that stages a
collection's contents patches those six names, not the fail-open
``list_hearings`` / ``list_tasks`` / ``list_notes`` it used to.

Rows are scoped the way the models scope them: a dossier collection lists
the rows whose ``dossier_id`` equals its id, « Général » (``""``) the rows
with NO dossier (``""`` on a hearing or a note, ``None`` on a task). The
hearings are also passed through ``models.hearing._filter_confirmation`` on
the DAV default (``include_unconfirmed=False``), so a staged unconfirmed
Bookings import is dropped exactly as the real reader drops it.
"""

from typing import Iterable


def _scoped(rows: Iterable[dict], dossier_id: str) -> list[dict]:
    if dossier_id:
        return [r for r in rows if r.get("dossier_id") == dossier_id]
    return [r for r in rows if not r.get("dossier_id")]


def patch_members(monkeypatch, dc, *, hearings=(), tasks=(), notes=()) -> None:
    """Point *dc*'s six strict member readers at *hearings* / *tasks* /
    *notes* (lists of stored-shaped rows)."""
    from models import hearing as hearing_model

    hearings, tasks, notes = list(hearings), list(tasks), list(notes)

    def _hearings(dossier_id, *, include_unconfirmed):
        return hearing_model._filter_confirmation(
            _scoped(hearings, dossier_id), include_unconfirmed)

    monkeypatch.setattr(dc, "list_hearings_strict", _hearings)
    monkeypatch.setattr(
        dc, "list_hearings_without_dossier_strict",
        lambda *, include_unconfirmed: _hearings("", include_unconfirmed=include_unconfirmed))
    monkeypatch.setattr(dc, "list_tasks_strict", lambda dossier_id: _scoped(tasks, dossier_id))
    monkeypatch.setattr(dc, "list_tasks_without_dossier_strict", lambda: _scoped(tasks, ""))

    def _notes(dossier_id, *, include_analyse):
        rows = _scoped(notes, dossier_id)
        return rows if include_analyse else [n for n in rows if not n.get("is_analyse")]

    monkeypatch.setattr(dc, "list_notes_strict", _notes)
    monkeypatch.setattr(
        dc, "list_notes_without_dossier_strict",
        lambda *, include_analyse: _notes("", include_analyse=include_analyse))
