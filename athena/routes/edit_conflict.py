"""Web-form optimistic concurrency — the browser half of D9 (plan rule 11).

An edit form carries the ``etag`` of the version it was rendered from, in a
hidden ``expected_etag`` input (``templates/components/_edit_conflict.html``).
The route hands that value to the model, which commits only if the stored
etag is STILL that one — checked inside a transaction (``models.concurrency``).
Without it, every save was a full-document ``set()`` of what the page showed
when it opened: a tab left open over lunch silently erased what the phone,
a second tab or the connector had written since (a block appended to a note
by Claude, a task Claude closed, an analysis Claude recorded on a document).

What the three helpers here decide, so that eight routes decide it the same
way:

* :func:`submitted_etag` — ``None`` when the form carries NO such field: a
  page rendered before this deploy. No check runs and the save behaves
  exactly as before, so a rollout never breaks a tab already open. A field
  present but malformed is a hand-made POST: a French 400, nothing written.
  ``''`` is legitimate — the etag of a legacy record written before Rule 7.
* :func:`split_stale` — separates the model's concurrency refusal from its
  ordinary validation errors, because the two re-render differently:
  a STALE save re-renders with the amber banner and the CURRENT etag (the
  next save is then a deliberate overwrite, made after reading the
  banner), while a validation error re-renders with the SUBMITTED etag
  (the original version keeps protecting the retry).
* :func:`conflict_context` — what the banner shows: the current etag, the
  time of the last write, and a link to compare. The time is formatted
  HERE, in Montréal, never in the template: templates do not handle
  datetimes, and a macro using a custom filter would not even compile in
  the bare Jinja environments some tests render forms with.

The banner names WHEN the record changed, never WHO changed it. Every model
stamps ``updated_via`` with the etag it regenerates, but a maintenance
script under ``scripts/`` regenerates an etag without stamping it, so the
recorded writer can be the PREVIOUS one — a false attribution on a
lawyer-facing screen. The connector's own refusal makes the same choice
(``mcp.handlers._stale_message``).

A stale re-render answers **200**, like every validation re-render of these
forms: they are full-page forms, and htmx only swaps a 2xx.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Optional

from flask import abort, make_response, request

from tz import to_mtl
from utils.format_fr import format_date_fr
from utils.logging_setup import log_unexpected

FIELD = "expected_etag"

# An etag is a UUIDv4 string (or '' for a legacy record). Anything else in
# the field did not come from a form this application rendered.
_ETAG_SHAPE = re.compile(r"[0-9A-Za-z-]{0,64}")

MALFORMED_MESSAGE = (
    "Requête invalide : la version de l'élément transmise par le "
    "formulaire est illisible. Rien n'a été enregistré — rechargez la "
    "page, puis refaites la modification."
)


def submitted_etag() -> Optional[str]:
    """The etag the submitted form was rendered from, or ``None``.

    ``None`` — the field is absent (a page rendered before the field
    existed): the caller passes it through and the model takes its legacy,
    unchecked path. A present but malformed value aborts the request with a
    French 400; nothing downstream runs.
    """
    raw = request.form.get(FIELD)
    if raw is None:
        return None
    if not _ETAG_SHAPE.fullmatch(raw):
        response = make_response(MALFORMED_MESSAGE, 400)
        response.mimetype = "text/plain"
        abort(response)
    return raw


NOTHING_SAVED = (
    "Vos changements n'ont pas été enregistrés : ils sont conservés "
    "ci-dessous."
)


def split_stale(errors: Optional[list[str]]) -> tuple[bool, list[str]]:
    """``(stale, other_errors)`` — the concurrency refusal pulled out.

    The refusal is a sentence meant for a caller with no banner (the model
    returns it to DAV and the connector too); on a web form the banner says
    it better, so it is dropped from the error list the page prints.
    """
    from models import concurrency

    errors = list(errors or [])
    stale = concurrency.is_stale(errors)
    return stale, [e for e in errors if e != concurrency.STALE_ETAG_ERROR]


def resolve_refusal(
    errors: Optional[list[str]],
    *,
    submitted: Optional[str],
    reread: Callable[[], Optional[dict]],
    compare_url: str = "",
    note: str = "",
    outcome: str = NOTHING_SAVED,
) -> tuple[list[str], Optional[dict], Optional[str]]:
    """``(errors_to_print, conflict_or_None, etag_for_the_re_render)``.

    The one decision every edit route makes on a refused save. A STALE
    refusal re-reads the record (*reread*) and hands back the banner and the
    CURRENT etag; any other refusal hands back *submitted* — the etag the
    re-rendered form now stands for. That is the etag the form was sent
    with, ``None`` for a page that carried none (its re-render then carries
    none either and stays on the legacy, unchecked path), or — for a form
    that writes in two steps and saved the first — the etag that first
    write produced.
    """
    stale, others = split_stale(errors)
    if not stale:
        return others, None, submitted
    try:
        current = reread()
    except Exception:
        log_unexpected("edit conflict: current version unreadable")
        current = None
    conflict = conflict_context(
        current, compare_url=compare_url, note=note, outcome=outcome
    )
    return others, conflict, conflict["etag"]


def _when(value) -> str:
    """« 25 septembre 2026 à 14 h 05 », Montréal time; '' when unknown."""
    if not isinstance(value, datetime):
        return ""
    local = to_mtl(value)
    return f"{format_date_fr(local.date())} à {local:%H} h {local:%M}"


def conflict_context(
    current: Optional[dict],
    *,
    compare_url: str = "",
    note: str = "",
    outcome: str = NOTHING_SAVED,
) -> dict:
    """What the banner needs, fully computed — the template formats nothing.

    *current* is the record as it is stored NOW (re-read after the refusal);
    ``None`` when that re-read failed, in which case the banner still
    appears, without a time, and the etag handed back is ``''`` — the next
    save is then refused again rather than blessed. *compare_url* opens the
    current version in a new tab. *note* is one extra sentence a form may
    need (the dossier's: a trust entry also counts as a change). *outcome*
    says what happened to the submission — « nothing was saved » unless a
    form that writes in two steps saved its first one (the document form).
    """
    from models import concurrency

    current = current or {}
    return {
        "etag": concurrency.etag_of(current),
        "updated_at_display": _when(current.get("updated_at")),
        "compare_url": compare_url,
        "note": note,
        "outcome": outcome,
    }
