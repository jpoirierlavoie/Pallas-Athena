"""The MCP tool handlers — the reads, plus the writes of mcp.tools.WRITE_TOOLS.

Each handler takes the validated ``arguments`` dict and returns a
JSON-serializable payload; the endpoint wraps it in the MCP envelope.
Handlers call EXISTING model/util functions only. Nothing here may assume a
Flask request context.

**Read handlers must never write to Firestore.** Only the handlers named in
:data:`mcp.tools.WRITE_TOOLS` mutate anything. Their families — and the
counts — live in ONE place, :data:`mcp.disclosure.FAMILIES` (which
``WRITE_TOOLS`` is derived from); the promises the connector makes about
what it can NEVER do live beside them in :data:`mcp.disclosure.NEVERS`, each
backed by a sweep of this module's syntax tree (tests/test_mcp_disclosure).
Restating either here is how they drifted before. The writable collections
are ``notes`` (with their write-once ``revisions`` subcollection, lot 1b),
``tasks``, ``hearings``, ``protocols`` (with their ``steps`` subcollection,
lot 1b), ``timeentries``, ``expenses``, ``parties``, ``invoices`` (with its
``lineitems`` subcollection, and the ``invoiced`` flips import_invoice
causes on the two billing collections), ``documents`` (its ``analyse``
cache, the DERIVED ``category`` and an append-only ``analyses`` journal
entry — never the file, its name or its folder) and ``dossiers``. ONE path
reaches outside Firestore: ``decide_rendez_vous``'s refusal, through
``services/rendez_vous``, cancels the Outlook meeting of a pending Bookings
request (Graph ``/cancel``, with the service's fixed text) — the connector's
only call outside Firestore, and a « never » of the registry forbids any
other. (An edited event's copy in the lawyer's own Outlook calendar follows
through the 10-minute mirror cron, never through this code.)

The versioned-drafts family (``save_draft``/``revise_draft``/``get_draft``/
``list_drafts``) LEFT this connector on 2026-09-02 with the internal chat
client it was built for: the practice moved to a Claude for Work account
under a data-processing agreement, so the reason the chat existed — keeping
privileged material out of a consumer product — no longer holds. Nothing
was ever stored in ``chat_drafts``; the feature never saw use.
**NOTHING is ever deleted**, no invoice status is ever set and no payment is
ever recorded (``mcp.disclosure.NEVERS`` — the sweep would catch a handler
reaching one). That is why, for example, ``list_protocol_steps`` derives
overdue status by date comparison instead of calling
``check_overdue_steps``, which writes. (Note the request path itself does
write outside the tool path: ``bearer.stamp_token_last_used``,
``oauth.touch_client``, and ``mcp/write_support.py``'s idempotency
records.)

**Every note write MUST bump the dossier's CTag, and every contact write
the addressbook's.** ``models/note.py`` and ``models/partie.py`` never bump
— bumping lives in the caller (``routes/notes.py``, ``routes/parties.py``,
``dav/dossier_collections.py``). A tool path that writes and skips
``bump_ctag(f"dossier:{dossier_id}")`` or ``bump_ctag("parties")`` leaves
the record visible in the web UI while DavX5 silently never re-syncs it:
nothing errors, and only the phone is wrong.

**A dossier write must resolve every party id BEFORE the model sees it.**
``dossier._rebuild_party_mirrors`` subscripts ``c["id"]`` raw, so an entry
without an id raises an uncaught ``KeyError`` — an HTTP 500, not a
validation error — and ``_validate`` never checks for the key.

**Guards run in the HANDLER, ahead of the model.** An invoiced entry, a
missing name, an unknown id are refused here, in French, before any model
function is reached. That started as a `dry_run` obligation — a preview
that skipped them announced successes the live call refused — and the
preview is gone since 2026-08-27, but the guards stay: refusing early with
a message that names the field beats refusing late with a model error.

Serialization rules (§10.1):
* money → ``<field>_cents`` (int) + ``<field>_display`` (fr-CA string);
* date-only fields stored at midnight UTC (timeentries/expenses ``date``,
  invoice ``date``/``due_date``, task ``due_date``, protocol
  ``start_date``/``end_date``/step ``deadline_date``, dossier
  ``opened_date``/``closed_date``/``prescription_date``) → the UTC
  calendar date as YYYY-MM-DD via :func:`mcp.tools.date_str` — NEVER
  through ``to_mtl``;
* true timestamps → ISO 8601 in America/Montreal via
  :func:`mcp.tools.iso_mtl`.
"""

import functools
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Optional

from dav.sync import (
    bump_ctag,
    collection_for,
    record_tombstone,
    relocation_plan,
    remove_tombstone,
)
from mcp import coverage, import_audit
from mcp.write_support import run_write
from pagination import decode_cursor, encode_cursor
from models import audit_event as audit_event_model
from models import concurrency
from models import dossier as dossier_model
from models import document as document_model
from models import expense as expense_model
from models import folder as folder_model
from models import hearing as hearing_model
from models import invoice as invoice_model
from models import note as note_model
from models import partie as partie_model
from models import protocol as protocol_model
from models import reference
from models import task as task_model
from models import time_entry as time_entry_model
from models import trust as trust_model
from security import sanitize
from services import protocoles as protocol_service
from services import rendez_vous as rendez_vous_service
from tz import MTL, mtl_to_utc
from utils import (
    analyse_blocs, deadlines, pdf_text, phases, recurrence, taxonomie,
)
from utils.cabinet import cabinet_dict
from utils.format_fr import format_date_fr, format_rate_fr
from utils.logging_setup import log_hearing_series_event
from utils.recours import PRESCRIPTION_LABELS, compute_class
from utils.taxonomie import DOMAINE_LABELS
from utils.template_fields import selected_address
from utils.validators import format_phone_display

from mcp.tools import (
    ANALYSE_OPERATIONS_MAX,
    CONCURRENCY_OPTIONAL,
    CONTENT_MAX_CHARS,
    CONCURRENCY_REQUIRED,
    DOCUMENT_TEXT_MAX_CHARS,
    PHASE_BULK_MAX,
    TOOLS,
    ToolArgumentError,
    date_str,
    format_cents,
    iso_mtl,
)

# Bounded superset size for Python-side post-filtering (§10.1): never more
# than 200 docs fetched per tool call, never a new composite index.
_FETCH_CAP = 200
# Sort floor for rows whose date is missing — they order LAST, never first.
_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)
_NOTE_PREVIEW_CHARS = 280
_UNBILLED_ROW_CAP = 50
# ── Shared serialization helpers ────────────────────────────────────────

def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _money(payload: dict, key: str, cents: Any) -> None:
    value = int(cents or 0)
    payload[f"{key}_cents"] = value
    payload[f"{key}_display"] = format_cents(value)


def _parse_iso_date(value: str, name: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ToolArgumentError(f"`{name}` must be a valid date in YYYY-MM-DD format")


def _phone(value: str) -> str:
    if not value:
        return ""
    try:
        return format_phone_display(value)
    except Exception:
        return value


def _limit_arg(args: dict, default: int) -> int:
    return int(args.get("limit", default))


def _filter_updated_since(rows: list[dict], args: dict) -> list[dict]:
    """Keep rows whose updated_at is on/after the caller's cutoff.

    Offered ONLY on the fully-materialized tools (tasks, notes, documents,
    parties) — on the 200-doc windowed tools (dossiers, hearings) a filter
    inside the window would silently miss older rows touched recently, so
    they deliberately do not take the argument. Accepts YYYY-MM-DD (a
    Montréal calendar day) or a full ISO-8601 timestamp; a naive value is
    read as Montréal local time.
    """
    raw = args.get("updated_since")
    if not raw:
        return rows
    try:
        cutoff = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        raise ToolArgumentError(
            "`updated_since` must be YYYY-MM-DD or an ISO-8601 timestamp"
        )
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=MTL)
    cutoff = cutoff.astimezone(timezone.utc)
    return [
        r
        for r in rows
        if (ts := _as_utc(r.get("updated_at"))) is not None and ts >= cutoff
    ]


def _list_payload(items: list, truncated: bool) -> dict:
    return {"items": items, "count": len(items), "truncated": truncated}


def _offset_arg(args: dict) -> int:
    return max(0, int(args.get("offset", 0)))


def _offset_page(rows: list, args: dict, limit: int) -> tuple[list, bool, Optional[int]]:
    """Slice a fully-materialized result by offset (G07).

    Correct ONLY because these tools receive the complete, deterministically
    sorted list before slicing — the windowed tools (dossiers, hearings)
    page by cursor instead. NOT snapshot-stable: the list is re-derived per
    request, so a row completed between two pages shifts the following ones.
    Returns (page, truncated, next_offset — None on the last page).
    """
    offset = _offset_arg(args)
    page = rows[offset:offset + limit]
    truncated = len(rows) > offset + limit
    return page, truncated, (offset + limit) if truncated else None


def _freshen_dossier_labels(rows: list[dict], live: dict[str, dict]) -> list[dict]:
    """Overwrite stale creation-time dossier labels with live values.

    dossier_file_number/dossier_title are snapshots frozen at each row's
    last full write — they survived a renumbering migration and every
    intitulé correction, so the audit saw file numbers that no longer
    exist and one dossier under three different titles (PA-D04). The
    STORED snapshot stays the fallback: *live* fails open to {} on a read
    blip, and a deleted dossier's rows keep the only label they have.
    """
    if not live:
        return rows
    out = []
    for r in rows:
        d = live.get(r.get("dossier_id") or "")
        if d:
            r = {
                **r,
                "dossier_file_number": (
                    d.get("file_number") or r.get("dossier_file_number", "")
                ),
                "dossier_title": d.get("title") or r.get("dossier_title", ""),
            }
        out.append(r)
    return out


def _live_dossiers(*row_lists: list[dict]) -> dict[str, dict]:
    """ONE batched get_all over the distinct dossier ids of every list.

    Document-ID lookups need no index; the read cost is the number of
    DISTINCT dossiers on the page. get_dossiers_bulk fails open to {}.
    """
    ids = sorted({
        r.get("dossier_id")
        for rows in row_lists
        for r in rows
        if r.get("dossier_id")
    })
    if not ids:
        return {}
    return dossier_model.get_dossiers_bulk(ids)


def _phase_pair(doc: dict) -> dict:
    """The Phase O classification, code AND bare label.

    The connector WRITES phase/sous_phase on time entries, expenses and tasks
    and, until this shipped, no row builder read them back: it recorded a
    classification it could not verify. The label comes from
    ``phases.PHASE_LABELS`` / ``SOUS_PHASE_LABELS``, which are bare
    («&nbsp;Contestation&nbsp;») — never ``phases.sous_phase_label``, which
    renders « Libellé [CODE] » and would repeat the code the row already
    carries. An unclassified row reads « Non renseignée », which is the
    vocabulary's own name for that state, not an invention.
    """
    phase = doc.get("phase", "") or ""
    sous_phase = doc.get("sous_phase", "") or ""
    return {
        "phase": phase,
        "sous_phase": sous_phase,
        "phase_label": phases.PHASE_LABELS.get(phase, ""),
        "sous_phase_label": phases.SOUS_PHASE_LABELS.get(sous_phase, ""),
    }


def _stamps(doc: dict) -> dict:
    """The stamps every row carries: when, and — since 2026-09-25 — by
    which path, plus the concurrency token.

    created_at/updated_at (PA-G05) are stored on every entity (Architecture
    Rule 7) — the gap was emission-only. True instants → iso_mtl, nullable
    for pre-Rule-7 legacy docs. Note updated_at is NOISY: DAV round-trips,
    protocol-step syncs and bulk folder moves all re-stamp it without
    content changing.

    etag is the stored concurrency token ('' on a legacy doc that never had
    one). created_via/updated_via/mcp_updated_at are what the models stamp
    (models/provenance.py): '' / null on a document not written since
    provenance existed — never a guess."""
    return {
        "created_at": iso_mtl(_as_utc(doc.get("created_at"))),
        "updated_at": iso_mtl(_as_utc(doc.get("updated_at"))),
        "etag": str(doc.get("etag") or ""),
        "created_via": str(doc.get("created_via") or ""),
        "updated_via": str(doc.get("updated_via") or ""),
        "mcp_updated_at": iso_mtl(_as_utc(doc.get("mcp_updated_at"))),
    }


def _reminder_minutes(h: dict) -> Optional[int]:
    """The stored reminder as an integer, ``None`` when unusable.

    The web form and the connector store one of VALID_REMINDER_MINUTES, but
    a phone VALARM may carry any duration (``vevent_to_hearing``), and a
    legacy document may lack the key — in which case the VEVENT serializer
    applies 1440, which is therefore what the phone shows."""
    value = h.get("reminder_minutes", 1440)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hearing_row(h: dict, *, pending: bool = False) -> dict:
    """One calendar event. *pending* (the bookings "pending" mode of
    list_hearings, lot 1b L7) adds the requester and the contact a
    confirmation would link — never on an ordinary agenda row."""
    all_day = bool(h.get("all_day"))
    start = _as_utc(h.get("start_datetime"))
    end = _as_utc(h.get("end_datetime"))
    modalite = h.get("modalite", "présentiel")
    row = {
        "id": h.get("id", ""),
        "title": h.get("title", ""),
        "hearing_type": h.get("hearing_type", ""),
        # Forum is derived from the type (never stored) — expose it so a
        # client need not carry the type→forum table.
        "forum": hearing_model.forum_of(h.get("hearing_type", "")),
        "start": date_str(start) if all_day else iso_mtl(start),
        "end": date_str(end) if all_day else iso_mtl(end),
        "all_day": all_day,
        "location": h.get("location", ""),
        "modalite": modalite,
        "modalite_label": hearing_model.MODALITE_LABELS.get(modalite, modalite),
        "conference_uri": h.get("conference_uri", ""),
        "court": h.get("court", ""),
        "judge": h.get("judge", ""),
        "status": h.get("status", ""),
        "notes": h.get("notes", ""),
        "dossier_id": h.get("dossier_id", "") or "",
        "dossier_file_number": h.get("dossier_file_number", ""),
        "dossier_title": h.get("dossier_title", ""),
        "reminder_minutes": _reminder_minutes(h),
        "serie_id": str(h.get("serie_id") or ""),
        "source": str(h.get("source") or ""),
        **_stamps(h),
    }
    if pending:
        row["confirmation"] = str(h.get("confirmation") or "")
        row["client_nom"] = str(h.get("client_nom") or "")
        row["client_email"] = str(h.get("client_email") or "")
        row["partie_suggeree_id"] = str(h.get("_partie_id") or "")
        row["partie_suggeree_nom"] = str(h.get("_partie_nom") or "")
    return row


def _task_row(t: dict, *, today: Optional[date] = None) -> dict:
    # A closed task is never overdue, whatever its due date says, and an
    # undated one cannot be late. `today` is threaded from the handler so
    # every row of one response shares a single clock read.
    status = t.get("status", "")
    is_overdue = status not in ("terminée", "annulée") and deadlines.is_past_due(
        t.get("due_date"), today=today or deadlines.today_mtl()
    )
    return {
        "id": t.get("id", ""),
        "title": t.get("title", ""),
        "description": t.get("description", ""),
        "priority": t.get("priority", ""),
        "status": status,
        "category": t.get("category", ""),
        "due_date": date_str(t.get("due_date")),
        "is_overdue": is_overdue,
        "completed_date": iso_mtl(_as_utc(t.get("completed_date"))),
        "dossier_id": t.get("dossier_id") or None,
        "dossier_file_number": t.get("dossier_file_number", ""),
        "dossier_title": t.get("dossier_title", ""),
        "related_note_id": t.get("related_note_id"),
        **_phase_pair(t),
        **_stamps(t),
    }


def derive_step_status(stored: str, deadline, *, today: date) -> str:
    """The step status a fresh read of the deadline implies.

    The STORED status is a fossil: ``check_overdue_steps`` is its only writer
    of ``en_retard``, it has no branch that ever CLEARS one, and it runs only
    when a browser loads the protocol page. A step stamped by the pre-2026-07-30
    wall-clock rule (which fired at 00:00 UTC — 20:00 the previous evening in
    Montréal) therefore carries ``en_retard`` for ever, and no read handler may
    repair it (writing from a read path is forbidden and pinned by a test).

    So the connector derives instead. ``complété`` is authoritative and never
    re-derived — completion is a fact, not a computation. Everything else
    follows the deadline against the Montréal calendar day.
    """
    if stored == "complété":
        return "complété"
    if deadlines.is_past_due(deadline, today=today):
        return "en_retard"
    if stored == "en_cours":
        return "en_cours"
    return "à_venir"


def _step_row(s: dict, today: date) -> dict:
    deadline = _as_utc(s.get("deadline_date"))
    stored = s.get("status", "")
    status = derive_step_status(stored, deadline, today=today)
    # Consistent BY CONSTRUCTION: both come from the same predicate, so the
    # « status: en_retard + is_overdue: false » pair the audit found can no
    # longer be emitted.
    is_overdue = status == "en_retard"
    return {
        "id": s.get("id", ""),
        "order": s.get("order", 0),
        "title": s.get("title", ""),
        "description": s.get("description", ""),
        "cpc_reference": s.get("cpc_reference", ""),
        "deadline_date": date_str(deadline),
        "status": status,
        "status_stored": stored,
        "status_differs": status != stored,
        "mandatory": bool(s.get("mandatory")),
        "deadline_locked": bool(s.get("deadline_locked")),
        # The template offset the deadline was computed from (days after
        # the protocol's start date) — a step that has one follows a new
        # start date; null on a custom step, which never moves by itself.
        "deadline_offset_days": s.get("deadline_offset_days"),
        "date_confirmed": bool(s.get("date_confirmed")),
        "completed_date": iso_mtl(_as_utc(s.get("completed_date"))),
        "linked_task_id": s.get("linked_task_id"),
        "linked_hearing_id": s.get("linked_hearing_id"),
        "notes": s.get("notes", ""),
        "is_overdue": is_overdue,
        # Lot 1b (L6): the phase the step carries — the defaults a new
        # time entry or task on the dossier is offered — read back, since
        # update_protocol_step and add_protocol_step now write it.
        **_phase_pair(s),
        **_stamps(s),
    }


def _dossier_row(d: dict) -> dict:
    # WP13: the list-level status fixes the interrompue-vs-échue blindness
    # (a past prescription_date used to be indistinguishable from a blown
    # deadline without one get_dossier call per row).
    derived = dossier_model.derive_prescription(d)
    return {
        "id": d.get("id", ""),
        "file_number": d.get("file_number", ""),
        "title": d.get("title", ""),
        "status": d.get("status", ""),
        "domaine": d.get("domaine", ""),
        "domaine_label": DOMAINE_LABELS.get(d.get("domaine", ""), ""),
        "role": d.get("role", ""),
        "tribunal": d.get("tribunal", ""),
        "court_file_number": d.get("court_file_number", ""),
        "opened_date": date_str(_as_utc(d.get("opened_date"))),
        "prescription_date": date_str(_as_utc(d.get("prescription_date"))),
        "prescription_status": derived["status"],
        "prescription_date_effective": date_str(
            _as_utc(derived["date_effective"])
        ),
        "clients": [c.get("name", "") for c in d.get("clients", [])],
        "opposing_parties": [p.get("name", "") for p in d.get("opposing_parties", [])],
        **_stamps(d),
    }


def _invoice_row(inv: dict) -> dict:
    """One invoice line. SHARED with get_billing_snapshot.outstanding_invoices
    — one row shape must not drift between the register and the snapshot, so
    every key added here appears in both.

    The dossier/client labels are the SNAPSHOT taken at issuance, deliberately
    not freshened: an invoice is an accounting artifact, and it must read as
    what was actually sent to the client, not as the file's current title.
    """
    row = {
        "id": inv.get("id", ""),
        "invoice_number": inv.get("invoice_number", ""),
        "dossier_id": inv.get("dossier_id", ""),
        "dossier_file_number": inv.get("dossier_file_number", ""),
        "client_name": inv.get("client_name", ""),
        "date": date_str(_as_utc(inv.get("date"))),
        "due_date": date_str(_as_utc(inv.get("due_date"))),
        "status": inv.get("status", ""),
        "status_label": invoice_model.STATUS_LABELS.get(
            inv.get("status", ""), inv.get("status", "")
        ),
        "paid_date": date_str(_as_utc(inv.get("paid_date"))),
        # « recorded » only once the accounting module posted an amount
        # (since 2026-08-17 it is the single writer). A balance
        # equal to the total must never be read as « nothing was paid » when
        # it means « nothing was RECORDED » — the distinction the payment
        # field was added to make sayable.
        "payment_basis": "recorded" if int(inv.get("amount_paid", 0)) else "none",
    }
    _money(row, "total", inv.get("total", 0))
    _money(row, "amount_due", inv.get("amount_due", 0))
    _money(row, "amount_paid", inv.get("amount_paid", 0))
    _money(row, "balance", invoice_model.balance_of(inv))
    return row


def _prescription_row(d: dict, today: date) -> dict:
    # WP13: the countdown runs on the EFFECTIVE date (a reconnaissance or
    # suspension event may have pushed it later); the raw prescription_date
    # stays emitted beside it as provenance. list_prescription_alerts
    # attaches the derived pair; a row reaching this builder without them
    # (unit tests, direct calls) falls back to the raw date.
    effective = _as_utc(
        d.get("prescription_date_effective") or d.get("prescription_date")
    )
    days_remaining: Optional[int] = None
    last_action: Optional[str] = None
    # last_action_day is INCLUSIVE (on-or-before): when the deadline already
    # falls on a juridical day, last_action_date EQUALS the deadline and
    # differs is False. The web dashboard only shows the date in the differs
    # case; the MCP emits both plus the boolean so a client can do the same
    # instead of reading the duplicate as a data bug (PA-D02).
    last_action_differs = False
    if effective:
        # Countdown against the user's (Montréal) calendar date — UTC
        # "today" runs ahead of the user's evening by up to 5 hours. Floored
        # at 0: an already-blown deadline reads 0, and prescription_status
        # carries the distinction.
        days_remaining = max(0, deadlines.days_until(effective, today=today) or 0)
        last_day, last_action_differs = deadlines.last_action_day(
            effective.date()
        )
        last_action = last_day.isoformat()
    return {
        "dossier_id": d.get("id", ""),
        "file_number": d.get("file_number", ""),
        "title": d.get("title", ""),
        "prescription_date": date_str(_as_utc(d.get("prescription_date"))),
        "prescription_date_effective": date_str(effective),
        "prescription_status": d.get("prescription_status", ""),
        "days_remaining": days_remaining,
        "last_action_date": last_action,
        "last_action_differs": last_action_differs,
        "droit_action_date": date_str(_as_utc(d.get("droit_action_date"))),
        "prescription_notes": d.get("prescription_notes", ""),
    }


# ── 1. get_agenda ───────────────────────────────────────────────────────

def get_agenda(args: dict) -> dict:
    days_ahead = int(args.get("days_ahead", 14))
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    # ONE clock read per request: every derived flag below shares it, so a
    # response can never straddle two calendar days.
    today = deadlines.today_mtl()

    # The window covers whole CIVIL days, `from` through `to`. It opened at
    # the instant `now` first (a 09:00 hearing vanished at 09:01), then at
    # midnight Montréal — after the midnight-UTC start of every all-day
    # hearing of the day, so the 07:00 briefing never listed TODAY's. The
    # read now starts at the earlier of the day's two instants
    # (hearing_model.civil_day_floor) and each row is judged by its civil
    # day (occurrence_day: the UTC date for an all-day hearing, the Montréal
    # date for a timed one) — which also drops the previous evening's timed
    # hearings the widened read brings back, and an all-day hearing of the
    # day after `to` that falls before the instant `cutoff`.
    window_to = cutoff.astimezone(MTL).date()
    raw_hearings = [
        h
        for h in hearing_model.on_or_after_day(
            hearing_model.list_hearings_in_range(
                hearing_model.civil_day_floor(today), cutoff, limit=100
            ),
            today,
        )
        if h.get("status") != "annulée"
        and (hearing_model.occurrence_day(h) or today) <= window_to
    ]
    raw_tasks = task_model.list_urgent_tasks(cutoff, limit=50)
    raw_steps = protocol_model.list_urgent_steps(cutoff, limit=50)

    # PA-D04: one batched join over every distinct dossier on the page so
    # the briefing never cites a renumbered file or an inverted intitulé.
    # Steps join through their parent protocol's dossier id.
    live = _live_dossiers(
        raw_hearings, raw_tasks,
        [{"dossier_id": s.get("_dossier_id", "")} for s in raw_steps],
    )

    hearings = [
        _hearing_row(h) for h in _freshen_dossier_labels(raw_hearings, live)
    ]
    urgent_tasks = [
        _task_row(t, today=today)
        for t in _freshen_dossier_labels(raw_tasks, live)
    ]
    urgent_steps = [
        {
            **_step_row(s, today),
            "protocol_id": s.get("_protocol_id", ""),
            "protocol_title": s.get("_protocol_title", ""),
            # The id, not just the number. A caller acting on a step — the
            # briefing minting a follow-up task — needs the dossier_id every
            # OTHER agenda row already carries (_hearing_row, _task_row,
            # _prescription_row); without it a file NUMBER is all it has, and
            # create_task refuses a number where it wants a UUID. It is in
            # hand three lines below for the label join, so emitting it costs
            # nothing and removes a per-candidate get_dossier call.
            "dossier_id": s.get("_dossier_id", ""),
            # The protocol doc's own snapshot is DOUBLY stale (copied from
            # the dossier at protocol creation) — prefer the live label.
            "dossier_file_number": (
                (live.get(s.get("_dossier_id") or "") or {}).get("file_number")
                or s.get("_dossier_file_number", "")
            ),
        }
        for s in raw_steps
    ]
    alerts = [
        _prescription_row(d, today)
        for d in dossier_model.list_prescription_alerts(
            now + timedelta(days=60), limit=50
        )
    ]

    unbilled = time_entry_model.get_unbilled_totals()
    stats: dict[str, Any] = {
        "open_dossiers": dossier_model.count_open(),
        "unbilled_hours": unbilled.get("hours", 0.0),
    }
    _money(stats, "unbilled", unbilled.get("amount", 0))
    # Same PA-G09 gap as the billing snapshot, fixed in the same commit —
    # two contradictory firm-wide unbilled figures would be worse than one.
    _money(
        stats,
        "unbilled_expenses",
        expense_model.get_filtered_expense_totals(
            billable_filter="non_facture"
        ).get("amount", 0),
    )
    _money(stats, "outstanding", invoice_model.get_outstanding_total())

    return {
        "window": {
            "from": today.isoformat(),
            "to": window_to.isoformat(),
            "days_ahead": days_ahead,
        },
        "hearings": hearings,
        "urgent_tasks": urgent_tasks,
        "urgent_protocol_steps": urgent_steps,
        "prescription_alerts": alerts,
        "stats": stats,
    }


# ── 2. list_dossiers ────────────────────────────────────────────────────

def list_dossiers(args: dict) -> dict:
    status = args.get("status")
    query = (args.get("query") or "").strip().lower()
    limit = _limit_arg(args, 20)

    # G07: the model cursor was already in hand and thrown away. The
    # request's `cursor` resumes the server scan; the emitted next_cursor
    # is minted from the LAST ROW WE RETURN, so nothing between `limit` and
    # the 200-doc fetch window is ever skipped — a continuation re-scans
    # (over-fetches, never loses) the remainder of the window.
    rows, window_cursor = dossier_model.list_dossiers_page(
        status_filter=status, limit=_FETCH_CAP, cursor=args.get("cursor")
    )
    if query:
        # The sommaire is where the SUBSTANCE lives (« litige successoral »,
        # party circumstances…) — matching only labels made every
        # subject-matter search fail (§7 Q6 of the MCP audit).
        rows = [
            d
            for d in rows
            if query
            in " ".join(
                [
                    d.get("file_number", ""),
                    d.get("title", ""),
                    d.get("court_file_number", ""),
                    d.get("sommaire", ""),
                ]
            ).lower()
        ]
    page = rows[:limit]
    next_cursor: Optional[str] = None
    if len(rows) > limit or window_cursor:
        last = page[-1] if page else None
        if last and last.get("opened_date") and last.get("id"):
            next_cursor = encode_cursor(
                [last.get("opened_date"), last.get("id")]
            )
        else:
            next_cursor = window_cursor
    payload = _list_payload([_dossier_row(d) for d in page],
                            next_cursor is not None)
    payload["next_cursor"] = next_cursor
    return payload


# ── 3. get_dossier ──────────────────────────────────────────────────────

def get_dossier(args: dict) -> dict:
    dossier_id = args.get("dossier_id")
    file_number = args.get("file_number")
    if bool(dossier_id) == bool(file_number):
        raise ToolArgumentError(
            "Provide exactly one of `dossier_id` or `file_number`"
        )

    if file_number:
        # A keyed query, not a scan of the 200 most recently OPENED dossiers:
        # the oldest files in the base — a historical import's, by
        # construction — sat outside that window and read as « absent ».
        # This branch also fails CLOSED where the dossier_id branch below
        # swallows a read error into found: false. The asymmetry is
        # deliberate: « is there already a dossier numbered X? » is the
        # question asked right before creating one, and a false « no » there
        # mints a duplicate nothing can delete.
        d = dossier_model.get_dossier_by_file_number(file_number)
    else:
        d = dossier_model.get_dossier(dossier_id)

    if d is None:
        return {
            "found": False,
            "dossier_id": dossier_id,
            "file_number": file_number,
        }

    did = d.get("id", "")
    action_obj = taxonomie.get_action(d.get("action", ""))
    record = _dossier_row(d)
    record.update(
        {
            "sommaire": d.get("sommaire", ""),
            "clients": d.get("clients", []),
            "opposing_parties": d.get("opposing_parties", []),
            "greffe_number": d.get("greffe_number", ""),
            "juridiction_number": d.get("juridiction_number", ""),
            "competence": d.get("competence", ""),
            "palais_de_justice": d.get("palais_de_justice", ""),
            "district_judiciaire": d.get("district_judiciaire", ""),
            "is_administrative_tribunal": bool(d.get("is_administrative_tribunal")),
            # Forum: "judiciaire" (a Québec judicial court, file number
            # parsed), "administratif"/"federal" (body named in `tribunal`,
            # file number unparsed), or "prejudiciaire" (nothing filed —
            # court_file_number reads « Préjudiciaire »). Legacy "autre" is
            # migrated on read by the model.
            "forum_type": d.get("forum_type", "judiciaire"),
            "mandate_type": d.get("mandate_type", ""),
            "fee_type": d.get("fee_type", ""),
            "fee_notes": d.get("fee_notes", ""),
            "closed_date": date_str(_as_utc(d.get("closed_date"))),
            # Recours & prescription. prescription_date (= "date pour agir") is
            # already in the base row; these are its source fields. domaine /
            # domaine_label are on the base row too.
            "action": d.get("action", ""),
            "action_label": taxonomie.action_label(d.get("action", "")),
            "action_precision": d.get("action_precision", ""),
            # The taxonomy's own guidance for this action. delai is the
            # SUGGESTED delay verbatim, never a computed one; delai_types
            # lists its legal nature(s) as tokens of the closed § 4
            # vocabulary (PE/PA/D/DR/A/R/N/I/S/V/F), delai_types_label the
            # joined French rendering, a_valider a qualification still to be
            # confirmed at the sources, and avis the structured prior-notice
            # obligations. ref_delai cites the source of the DELAY,
            # ref_fondement the seat of the right of action.
            "delai": action_obj.delai if action_obj else "",
            "delai_types": list(action_obj.delai_types) if action_obj else [],
            "delai_types_label": taxonomie.delai_types_label(
                d.get("action", "")
            ),
            "a_valider": bool(action_obj.a_valider) if action_obj else False,
            "delai_point_depart": action_obj.point_depart if action_obj else "",
            "ref_delai": action_obj.ref_delai if action_obj else "",
            "ref_fondement": action_obj.ref_fondement if action_obj else "",
            "avis": [
                {
                    "libelle": v.libelle,
                    "delai": taxonomie.avis_delai_display(v.delai_key),
                    "sanction": v.sanction,
                    "conditionnel": v.conditionnel,
                }
                for v in (action_obj.avis if action_obj else ())
            ],
            "prescription_type": d.get("prescription_type", ""),
            "prescription_label": PRESCRIPTION_LABELS.get(
                d.get("prescription_type", ""), ""
            ),
            "droit_action_date": date_str(_as_utc(d.get("droit_action_date"))),
            # Confirmed avis préalable date — manual, optional; date-only
            # (midnight UTC), so date_str, never iso_mtl.
            "date_avis": date_str(_as_utc(d.get("date_avis"))),
            # Acte interruptif posé (art. 2892 C.c.Q.). Renseignée, elle
            # retire aussi le dossier des prescription_alerts de get_agenda —
            # sans quoi l'assistant continuerait d'avertir d'un délai qui ne
            # court plus. Date seule : date_str, jamais iso_mtl.
            "prise_action_date": date_str(_as_utc(d.get("prise_action_date"))),
            # WP13: the event register + its derived projection. The RAW
            # prescription_date (base row) is never recomputed — provenance;
            # prescription_status/date_effective already computed by
            # _dossier_row via the one derivation seam.
            "prescription_events": [
                {
                    "id": ev.get("id", ""),
                    "type": ev.get("type", ""),
                    "type_label": dossier_model.PRESCRIPTION_EVENT_LABELS.get(
                        ev.get("type", ""), ev.get("type", "")
                    ),
                    "date": date_str(_as_utc(ev.get("date"))),
                    "end_date": date_str(_as_utc(ev.get("end_date"))),
                    "reference": ev.get("reference", ""),
                    "document_id": ev.get("document_id", ""),
                }
                for ev in (d.get("prescription_events") or [])
                if isinstance(ev, dict)
            ],
            "prescription_notes": d.get("prescription_notes", ""),
            # WP14: service of process, one entry PER PARTY (arts. 145/147
            # delays run per party). superseded_by handles the second-PV
            # case — the OPERATIVE service is the one no sibling
            # supersedes. Réponse-deadline derivation is a later phase.
            "significations": [
                {
                    "id": sig.get("id", ""),
                    "partie_id": sig.get("partie_id", ""),
                    "date": date_str(_as_utc(sig.get("date"))),
                    "mode": sig.get("mode", ""),
                    "mode_label": dossier_model.SIGNIFICATION_MODE_LABELS.get(
                        sig.get("mode", ""), sig.get("mode", "")
                    ),
                    "huissier_id": sig.get("huissier_id", ""),
                    "pv_document_id": sig.get("pv_document_id", ""),
                    "superseded_by": sig.get("superseded_by", ""),
                    "confirmee": bool(sig.get("confirmee")),
                }
                for sig in (d.get("significations") or [])
                if isinstance(sig, dict)
            ],
            # get_dossier bypassed _stamps until 2026-09-25, so the record
            # update_dossier edits carried no etag and no provenance.
            **_stamps(d),
        }
    )
    # The théorie de la cause's id — the handle edit_analyse's etag is read
    # through (get_note). Strict: « unreadable » is never reported absent.
    record["analyse_note_id"], record["analyse_note_state"] = (
        _analyse_note_ref(did)
    )
    _money(record, "hourly_rate", d.get("hourly_rate", 0))
    flat_fee = d.get("flat_fee")
    if flat_fee is None:
        record["flat_fee_cents"] = None
        record["flat_fee_display"] = None
    else:
        _money(record, "flat_fee", flat_fee)

    # Contingency rate: stored in basis points → numeric percent + fr-CA
    # display. None when unset — never coerced to 0.
    percent = d.get("contingency_percent")
    if percent is None:
        record["contingency_percent"] = None
        record["contingency_percent_display"] = None
    else:
        record["contingency_percent"] = int(percent) / 100
        record["contingency_percent_display"] = format_rate_fr(int(percent), 100)

    # Amount in dispute (+ derived class). None when unset — never coerced to 0.
    valeur = d.get("valeur")
    if valeur is None:
        record["valeur_cents"] = None
        record["valeur_display"] = None
        record["valeur_classe"] = None
    else:
        _money(record, "valeur", valeur)
        record["valeur_classe"] = compute_class(valeur)

    time_summary = time_entry_model.get_time_summary(did)
    time_out = {
        "total_hours": time_summary.get("total_hours", 0.0),
        "unbilled_hours": time_summary.get("unbilled_hours", 0.0),
    }
    _money(time_out, "total_billable", time_summary.get("total_billable_amount", 0))
    _money(time_out, "unbilled", time_summary.get("unbilled_amount", 0))

    expense_summary = expense_model.get_expense_summary(did)
    expense_out: dict[str, Any] = {}
    _money(expense_out, "total", expense_summary.get("total_expenses", 0))
    _money(expense_out, "unbilled", expense_summary.get("unbilled_expenses", 0))

    invoice_summary = invoice_model.get_invoice_summary(did)
    invoice_out: dict[str, Any] = {"count": invoice_summary.get("count", 0)}
    _money(invoice_out, "total_invoiced", invoice_summary.get("total_invoiced", 0))
    _money(invoice_out, "total_paid", invoice_summary.get("total_paid", 0))
    _money(
        invoice_out, "total_outstanding", invoice_summary.get("total_outstanding", 0)
    )

    # Both summaries take the Montréal day so their overdue counts agree
    # with the derived rows this connector emits elsewhere. Their DEFAULTS
    # (omitted argument) are the historical rules, which is what keeps the
    # web dashboard byte-identical.
    today = deadlines.today_mtl()
    return {
        "found": True,
        "dossier": record,
        "summaries": {
            "tasks": task_model.get_task_summary(did, today),
            "hearings": hearing_model.get_hearing_summary(did),
            "notes": note_model.get_notes_summary(did),
            "documents": document_model.get_document_summary(did),
            "time": time_out,
            "expenses": expense_out,
            "invoices": invoice_out,
            "protocol": protocol_model.get_protocol_summary(did, today),
        },
    }


# ── 4. list_tasks ───────────────────────────────────────────────────────

def list_tasks(args: dict) -> dict:
    status = args.get("status")
    include_completed = bool(args.get("include_completed", False))
    limit = _limit_arg(args, 25)
    today = deadlines.today_mtl()

    tasks = task_model.list_tasks(
        dossier_id=args.get("dossier_id"), status_filter=status
    )
    if not status and not include_completed:
        tasks = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]
    tasks = _filter_updated_since(tasks, args)

    page, truncated, next_offset = _offset_page(tasks, args, limit)
    page = _freshen_dossier_labels(page, _live_dossiers(page))
    payload = _list_payload(
        [_task_row(t, today=today) for t in page], truncated
    )
    if next_offset is not None:
        payload["next_offset"] = next_offset
    return payload


# ── 5. list_hearings ────────────────────────────────────────────────────

# The arguments of the DATE-WINDOW mode, which the two selection modes
# (serie_id, bookings) refuse rather than silently ignore — a caller that
# sends both is told which one applies.
_HEARING_WINDOW_ARGS = ("date_from", "date_to", "dossier_id")


def _hearing_key(h: dict) -> tuple:
    """(start, id) — the ascending agenda order every mode pages on."""
    return (_as_utc(h.get("start_datetime")) or _UTC_MIN,
            str(h.get("id") or ""))


def _hearing_selection(args: dict) -> tuple[list[dict], str]:
    """The rows of the serie / bookings modes, and the mode's name.

    Both are STRICT reads: a failure is a refusal, never an empty list —
    « aucune demande en attente » or « cette série est vide » over a
    Firestore outage would be a false statement a caller acts on."""
    if "bookings" in args and "serie_id" in args:
        raise ToolArgumentError(
            "`bookings` et `serie_id` s'excluent : un seul mode par appel."
        )
    for key in _HEARING_WINDOW_ARGS:
        if key in args:
            raise ToolArgumentError(
                f"`{key}` ne s'applique pas avec `bookings` ou `serie_id` : "
                "ces modes rendent la liste entière qu'ils désignent."
            )
    if "bookings" in args:
        if args.get("bookings") != "pending":
            raise ToolArgumentError("`bookings` n'admet que « pending ».")
        try:
            rows = rendez_vous_service.lister_en_attente()
        except rendez_vous_service.LectureImpossible:
            raise ToolArgumentError(rendez_vous_service.LECTURE_IMPOSSIBLE)
        return rows, "bookings_pending"
    serie_id = str(args.get("serie_id") or "").strip()
    if not serie_id:
        # "" is a STORED value (every standalone event): list_series
        # refuses it, and so does this mode.
        raise ToolArgumentError(
            "`serie_id` est vide : prenez-le sur une ligne de list_hearings."
        )
    try:
        rows = hearing_model.list_series(serie_id)
    except Exception:
        raise ToolArgumentError(
            "Lecture de la série impossible — réessayez dans un moment."
        )
    # The MCP read contract: never an unconfirmed Bookings import outside
    # the pending mode (a series never holds one; the rule is kept anyway).
    return [h for h in rows if (h.get("confirmation") or "") == ""], "serie"


def _list_hearings_selected(args: dict, limit: int) -> dict:
    rows, mode = _hearing_selection(args)
    resume = decode_cursor(args.get("cursor"))
    rows = sorted(rows, key=_hearing_key)
    if resume and len(resume) == 2 and isinstance(resume[0], datetime):
        marker = (resume[0], str(resume[1]))
        rows = [h for h in rows if _hearing_key(h) > marker]
    truncated = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if truncated and page:
        last_start, last_id = _hearing_key(page[-1])
        next_cursor = encode_cursor([last_start, last_id])
    page = _freshen_dossier_labels(page, _live_dossiers(page))
    payload = _list_payload(
        [_hearing_row(h, pending=mode == "bookings_pending") for h in page],
        truncated,
    )
    payload["mode"] = mode
    payload["window"] = {"from": None, "to": None}
    payload["next_cursor"] = next_cursor
    return payload


def list_hearings(args: dict) -> dict:
    limit = _limit_arg(args, 25)
    if "bookings" in args or "serie_id" in args:
        return _list_hearings_selected(args, limit)
    today = datetime.now(MTL).date()
    date_from = (
        _parse_iso_date(args["date_from"], "date_from")
        if args.get("date_from")
        else today
    )
    date_to = (
        _parse_iso_date(args["date_to"], "date_to")
        if args.get("date_to")
        else date_from + timedelta(days=60)
    )
    if date_to < date_from:
        raise ToolArgumentError("`date_to` must be on or after `date_from`")
    if (date_to - date_from).days > 366:
        raise ToolArgumentError("The date span must be at most 366 days")

    # Fetch a widened UTC window, then filter per-hearing: all-day events
    # live at midnight UTC (a UTC calendar date), while timed hearings are
    # true instants the user reads in Montreal time — a 22h00 hearing on
    # date_to is stored past midnight UTC and must not fall off the edge.
    # +30h covers Montreal's worst-case UTC offset (EST, UTC-5).
    start_dt = datetime.combine(date_from, dtime.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(date_to, dtime.min, tzinfo=timezone.utc) + timedelta(
        hours=30
    )

    # Cursor without an index. `hearings` carries ZERO composite indexes, so
    # there is no server-side (start_datetime, id) ordering to resume from.
    # Instead the cursor RAISES THE LOWER BOUND on start_datetime — a range
    # filter on the very field the query already orders by, which the
    # automatic single-field index serves — and the exact position inside
    # that instant's tie group is then resolved in Python. Re-reading one
    # tie group per page is the whole cost.
    resume = decode_cursor(args.get("cursor"))
    resume_key: Optional[tuple] = None
    if resume and len(resume) == 2 and isinstance(resume[0], datetime):
        resume_key = (resume[0], str(resume[1]))
        start_dt = max(start_dt, resume[0])

    rows = hearing_model.list_hearings_in_range(start_dt, end_dt, limit=_FETCH_CAP)
    window_full = len(rows) >= _FETCH_CAP

    def _in_window(h: dict) -> bool:
        start = _as_utc(h.get("start_datetime"))
        if not isinstance(start, datetime):
            return False
        if h.get("all_day"):
            local_date = start.astimezone(timezone.utc).date()
        else:
            local_date = start.astimezone(MTL).date()
        return date_from <= local_date <= date_to

    rows = [h for h in rows if _in_window(h)]

    dossier_id = args.get("dossier_id")
    if dossier_id:
        rows = [h for h in rows if h.get("dossier_id") == dossier_id]

    # Hearings read ASCENDING (the agenda order) — the only ascending cursor
    # in the connector, matching list_hearings_in_range's own order_by.
    def _key(h: dict) -> tuple:
        return (_as_utc(h.get("start_datetime")), str(h.get("id") or ""))

    if resume_key is not None:
        rows = [h for h in rows if _key(h) > resume_key]
    rows.sort(key=_key)

    truncated = window_full or len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if truncated and page:
        last_start, last_id = _key(page[-1])
        if last_start and last_id:
            next_cursor = encode_cursor([last_start, last_id])
    page = _freshen_dossier_labels(page, _live_dossiers(page))
    payload = _list_payload([_hearing_row(h) for h in page], truncated)
    payload["mode"] = "window"
    payload["window"] = {"from": date_from.isoformat(), "to": date_to.isoformat()}
    payload["next_cursor"] = next_cursor
    return payload


def _resolve_scope(args: dict, *, default: str, allowed: tuple) -> str:
    """Resolve and VALIDATE the search scope; refuse every contradiction.

    ``dossier`` is implicit as soon as ``dossier_id`` is present, so the
    common call needs no scope at all and the historical behaviour is the
    default. Every incoherent combination is refused loudly rather than
    resolved by precedence: silently preferring one of two contradictory
    arguments is how a caller ends up believing it searched the whole firm
    when it searched one file (the defect this lot exists to remove).
    """
    scope = args.get("scope")
    dossier_id = (args.get("dossier_id") or "").strip()

    if scope is None:
        scope = "dossier" if dossier_id else default
    if scope not in allowed:
        raise ToolArgumentError(
            f"`scope` must be one of: {', '.join(allowed)}."
        )

    if scope == "cabinet" and dossier_id:
        raise ToolArgumentError(
            "`scope=\"cabinet\"` searches every dossier, so it contradicts "
            "`dossier_id`. Drop one: omit dossier_id to search the firm, or "
            "drop scope to search that one file."
        )
    if scope == "general" and dossier_id:
        raise ToolArgumentError(
            "`scope=\"general\"` means the notes attached to NO dossier, so "
            "it contradicts `dossier_id`. Omit one."
        )
    if scope == "dossier" and not dossier_id:
        raise ToolArgumentError(
            "`dossier_id` is required to search one dossier. To search every "
            "dossier instead, ask for it by name: `scope=\"cabinet\"`."
        )
    if scope == "cabinet" and args.get("folder_id"):
        raise ToolArgumentError(
            "`folder_id` identifies a folder INSIDE one dossier and has no "
            "meaning across the firm. Drop it, or pass a dossier_id."
        )
    # Two paging mechanisms with two different orderings: preferring one in
    # silence would serve a page the caller cannot reason about.
    if args.get("cursor") and args.get("offset"):
        raise ToolArgumentError(
            "Pass `cursor` OR `offset`, never both — they page different "
            "orderings."
        )
    if args.get("cursor") and scope != "cabinet":
        raise ToolArgumentError(
            "`cursor` is only available with `scope=\"cabinet\"`; the other "
            "scopes page with `offset`."
        )
    # The mirror image, and the one that actually bites: cabinet scope pages
    # by cursor, so an `offset` here would be accepted, validated, and then
    # silently dropped — every page identical to the first. A caller keeping
    # its offset habit would walk the firm, see the same rows, and conclude
    # the corpus is that small: the very failure this lot exists to remove.
    # Truthiness, so offset: 0 stays a harmless no-op.
    if args.get("offset") and scope == "cabinet":
        raise ToolArgumentError(
            "`offset` does not page `scope=\"cabinet\"` — follow "
            "`next_cursor` from the response instead."
        )
    if args.get("dossier_status") and scope != "cabinet":
        raise ToolArgumentError(
            "`dossier_status` filters ACROSS dossiers and only applies to "
            "`scope=\"cabinet\"`."
        )
    return scope


def _dossier_status_filter(args: dict) -> Optional[set]:
    """Ids of the dossiers matching `dossier_status`, or None for no filter.

    ONE query — a single-field equality, served by the automatic index, so
    no composite index is introduced. Deliberately NOT the paged variant:
    that reads one page (200) and hands back a cursor, and discarding it
    would silently drop every row filed past the 200th matching dossier
    while `truncated` still read false. The id set is one string per
    dossier; completeness costs nothing worth saving here.

    The set can also come back empty because the READ FAILED — the model
    swallows a Firestore error into ``[]`` — which is why callers publish
    the match count rather than letting a zero-row answer pass for fact.
    """
    wanted = args.get("dossier_status")
    if not wanted:
        return None
    return {
        d.get("id", "")
        for d in dossier_model.list_dossiers(status_filter=wanted)
    }


def _cabinet_page(
    rows: list[dict], args: dict, limit: int, key=None
) -> tuple[list[dict], Optional[str], bool]:
    """Page a cabinet-wide result set by KEYSET on (created_at, id).

    Cabinet scope is a NEW mode, so it gets a sound ordering rather than
    inheriting the model's (pinned first, created_at DESC): `pinned` is a
    one-click toggle, and a mutable component in a cursor key can move a row
    across the page boundary. `created_at` and `id` are never rewritten.

    The other scopes keep the model's ordering and their `offset` paging
    UNCHANGED — the two scheduled jobs read those, and a reordered page is a
    behaviour change however harmless it looks.
    """
    def _default_key(row: dict) -> tuple:
        return (_as_utc(row.get("created_at")) or _UTC_MIN,
                str(row.get("id") or ""))

    _key = key or _default_key
    ordered = sorted(rows, key=_key, reverse=True)
    marker = decode_cursor(args.get("cursor"))
    if marker and len(marker) == 2:
        cut = tuple(marker)
        try:
            ordered = [r for r in ordered if tuple(_key(r)) < cut]
        except TypeError:
            # A foreign cursor (another tool's key types) must degrade to
            # page 1 — the documented contract — never crash and never
            # position the reader somewhere arbitrary.
            pass
    page = ordered[:limit]
    truncated = len(ordered) > limit
    next_cursor = None
    if truncated and page:
        when, ident = _key(page[-1])
        # Mint it even when `when` is the _UTC_MIN floor (a legacy row with
        # no created_at): datetime.min round-trips through the codec, and
        # `id` already orders the undated block, so the resume lands inside
        # it. Refusing to mint here stranded the whole tail behind
        # truncated: true with no handle to reach it.
        if ident:
            next_cursor = encode_cursor([when, ident])
    return page, next_cursor, truncated


# ── 6. list_notes ───────────────────────────────────────────────────────

def list_notes(args: dict) -> dict:
    limit = _limit_arg(args, 20)
    scope = _resolve_scope(
        args, default="general", allowed=("general", "dossier", "cabinet")
    )
    matched_dossiers: Optional[int] = None
    dossier_id = args.get("dossier_id")
    # category + query are pure model passthrough (models/note.py already
    # filters both, Python-side over its fetch — PA-G08); the enum at the
    # schema gate matters, because the model silently NO-OPS on an unknown
    # category value instead of erroring.
    category = args.get("category")
    search = args.get("query")
    # include_analyse=True: the MCP read paths EXPOSE the « Théorie de la
    # cause » note (written only through edit_analyse since lot 1b —
    # append_to_note and update_note refuse it). The model's default would
    # silently hide it from Claude.
    if scope == "dossier":
        notes = note_model.list_notes(
            dossier_id=dossier_id, category=category, search=search,
            include_analyse=True,
        )
    else:
        # The model has no "no dossier" query, so BOTH remaining scopes read
        # the same firm-wide set and differ only in the Python filter after
        # it — « général » keeps the notes attached to nothing, « cabinet »
        # keeps them all. The read cost was already being paid on the
        # général path; cabinet simply stops throwing the rest away.
        notes = note_model.list_notes(
            category=category, search=search, include_analyse=True
        )
        if scope == "general":
            notes = [n for n in notes if not n.get("dossier_id")]
        else:
            keep = _dossier_status_filter(args)
            if keep is not None:
                matched_dossiers = len(keep)
                notes = [n for n in notes if n.get("dossier_id") in keep]
    if args.get("pinned") is not None:
        # A SELECT filter — deliberately not the model's `pinned_first`,
        # which only reorders (the obvious mis-wiring).
        want = bool(args["pinned"])
        notes = [n for n in notes if bool(n.get("pinned")) == want]
    if args.get("date_from") or args.get("date_to"):
        # created_at is a true instant; the argument is a Montréal calendar
        # date — compare in MTL (the list_hearings boundary precedent),
        # never date_str/UTC, or an evening note lands on the wrong day.
        lo = (
            _parse_iso_date(args["date_from"], "date_from")
            if args.get("date_from") else None
        )
        hi = (
            _parse_iso_date(args["date_to"], "date_to")
            if args.get("date_to") else None
        )

        def _in_window(n: dict) -> bool:
            ts = _as_utc(n.get("created_at"))
            if ts is None:
                return False
            d = ts.astimezone(MTL).date()
            return (lo is None or d >= lo) and (hi is None or d <= hi)

        notes = [n for n in notes if _in_window(n)]
    notes = _filter_updated_since(notes, args)

    next_cursor = None
    next_offset = None
    if scope == "cabinet":
        page, next_cursor, truncated = _cabinet_page(notes, args, limit)
    else:
        # Untouched: same ordering, same offset paging the two scheduled
        # jobs already read.
        page, truncated, next_offset = _offset_page(notes, args, limit)

    # PA: the row carried NONE of the three dossier fields, so a note found
    # firm-wide could not be attributed to a file without one get_note call
    # each. Added in EVERY scope (the gap was never scope-specific), and
    # freshened from the live dossier the way get_note already does.
    page = _freshen_dossier_labels(page, _live_dossiers(page))
    items = [
        {
            "id": n.get("id", ""),
            "dossier_id": n.get("dossier_id") or "",
            "dossier_file_number": n.get("dossier_file_number", ""),
            "dossier_title": n.get("dossier_title", ""),
            "title": n.get("title", ""),
            "category": n.get("category", ""),
            "pinned": bool(n.get("pinned")),
            "is_analyse": bool(n.get("is_analyse")),
            **_stamps(n),
            "content_preview": (n.get("content", "") or "")[:_NOTE_PREVIEW_CHARS],
        }
        for n in page
    ]
    payload = _list_payload(items, truncated)
    payload["scope"] = scope
    payload["next_cursor"] = next_cursor
    # How many dossiers the dossier_status filter matched (null when no
    # filter was asked for). Zero rows with zero matched dossiers is a
    # filter that selected nothing — or a swallowed read error; either way
    # the caller can see WHY the answer is empty instead of reading it as
    # « the firm holds no such note ».
    payload["dossier_status_matched"] = matched_dossiers
    if next_offset is not None:
        payload["next_offset"] = next_offset
    return payload


# ── 7. get_note ─────────────────────────────────────────────────────────

def get_note(args: dict) -> dict:
    note = note_model.get_note(args["note_id"])
    if note is None:
        return {"found": False, "note_id": args["note_id"]}
    # PA-D04: freshen the stale creation-time dossier labels (one keyed
    # read; the stored snapshot survives a failed lookup).
    (note,) = _freshen_dossier_labels([note], _live_dossiers([note]))
    return {
        "found": True,
        "note": {
            "id": note.get("id", ""),
            "dossier_id": note.get("dossier_id", ""),
            "dossier_file_number": note.get("dossier_file_number", ""),
            "dossier_title": note.get("dossier_title", ""),
            "title": note.get("title", ""),
            "content": note.get("content", ""),
            "category": note.get("category", ""),
            "pinned": bool(note.get("pinned")),
            "is_analyse": bool(note.get("is_analyse")),
            # The théorie's zones, so an edit_analyse operation can be aimed
            # without re-parsing the Markdown by eye; null for an ordinary
            # note (always emitted — the key is required).
            "structure": (
                _analyse_structure(note.get("content", "") or "", headings=True)
                if note.get("is_analyse") else None
            ),
            **_stamps(note),
        },
    }


@functools.lru_cache(maxsize=1)
def _seed_bodies() -> dict:
    """zone key → the stripped body the seeded théorie carries there.

    Parsed ONCE from ``models/note.ANALYSE_SEED`` — the text the note is
    created with — so « is_seed » compares against the real template,
    never a copy of it."""
    seed = note_model.ANALYSE_SEED
    parsed = analyse_blocs.parse(seed)
    bodies = {analyse_blocs.ENTETE: parsed.entete.body(seed).strip()}
    for zone in parsed.blocs:
        bodies.setdefault(zone.key, zone.body(seed).strip())
    return bodies


def _analyse_structure(content: str, *, headings: bool) -> dict:
    """Where the théorie's zones stand (``utils/analyse_blocs.parse``).

    *headings* only on the READ path (get_note): a write result carries
    letters, sizes and flags, never the note's text — it is stored 24 h
    in mcp_idempotency."""
    parsed = analyse_blocs.parse(content)
    seed = _seed_bodies()

    def _zone(zone) -> dict:
        out: dict[str, Any] = {}
        if headings:
            out["heading"] = zone.heading
        body = zone.body(content)
        out["chars"] = len(body)
        out["is_seed"] = body.strip() == seed.get(zone.key)
        return out

    length = len(content)
    return {
        "ok": parsed.ok,
        "editable": parsed.editable,
        "in_order": parsed.in_order,
        "missing": list(parsed.missing),
        "duplicates": list(parsed.duplicates),
        "entete": _zone(parsed.entete),
        "blocs": [{"bloc": zone.key, **_zone(zone)} for zone in parsed.blocs],
        "interstitial_chars": sum(b - a for a, b in parsed.gaps),
        "content_length": length,
        "max_length": note_model.CONTENT_MAX_LENGTH,
        "remaining_chars": max(0, note_model.CONTENT_MAX_LENGTH - length),
    }


def _analyse_note_ref(dossier_id: str) -> tuple[Optional[str], str]:
    """``(analyse_note_id, state)`` for get_dossier — STRICT lookup.

    Through ``find_analyse_note_strict``, never the fail-open display
    helper: a swallowed read error must read « unreadable », not
    « absent » — a caller told « absent » would call edit_analyse to create
    one, and the model refuses that too, but a report saying the analysis
    does not exist would still be false."""
    try:
        note = note_model.find_analyse_note_strict(dossier_id)
    except note_model.AnalyseDuplicateError:
        return None, "duplicate"
    except note_model.AnalyseLookupError:
        return None, "unreadable"
    if note is None:
        return None, "absent"
    return note.get("id") or None, "present"


# ── 8. list_documents ───────────────────────────────────────────────────

def _folder_paths(dossier_id: str) -> dict[str, str]:
    """folder_id → « Parent / Enfant » map from ONE folder query.

    Never per-row breadcrumb walks: get_folder_breadcrumb costs one doc
    read per ancestor (≤5), which per row would turn a 25-row listing into
    ~100 reads. get_folder_tree is a single dossier_id== query."""
    paths: dict[str, str] = {}

    def _walk(nodes: list[dict], prefix: str) -> None:
        for n in nodes:
            path = f"{prefix} / {n['name']}" if prefix else n.get("name", "")
            if n.get("id"):
                paths[n["id"]] = path
            _walk(n.get("children", []), path)

    _walk(folder_model.get_folder_tree(dossier_id), "")
    return paths


def _analyse_apercu(doc: dict) -> dict:
    """L'état d'analyse d'un document, pour une LIGNE de liste.

    Étroit à dessein : de quoi décider s'il reste du travail, pas de quoi
    dispenser d'ouvrir le document. Le résumé, l'extrait et les motifs
    restent hors de l'index — les recopier par ligne ferait d'une liste de
    cinquante documents une charge que le plafond ne tiendrait pas, et
    l'appelant a `get_document_text` pour le fond.

    `analysee` est le prédicat que le modèle lit : « ce document a-t-il
    déjà été qualifié ? ». `category_presumee` porte la mention que
    l'écran affiche, pour que le connecteur ne présente jamais une
    supposition avec l'autorité d'une détermination.
    """
    a = doc.get("analyse") or {}
    sous_nature = str(a.get("sous_nature") or "")
    return {
        "analysee": bool(sous_nature),
        "sous_nature": sous_nature,
        "nature_detectee": str(a.get("nature_detectee") or ""),
        "famille": str(a.get("famille") or ""),
        "niveau_protection": a.get("niveau_protection"),
        "privileges": list(a.get("privileges") or []),
        "analyse_confirmee": bool(a.get("confirme")),
        "divergence_protection": bool(a.get("divergence_protection")),
        # « analyse » = présumée, absente ou « juriste » = déterminée.
        "category_presumee": str(doc.get("category_source") or "juriste")
        == "analyse",
    }


def list_documents(args: dict) -> dict:
    limit = _limit_arg(args, 25)
    # Every document belongs to a dossier, so there is no « général » scope
    # here — only one file or the whole firm.
    scope = _resolve_scope(args, default="dossier", allowed=("dossier", "cabinet"))
    # The schema no longer marks dossier_id required (relaxing `required` is
    # additive on the wire), but _resolve_scope above still demands it
    # outside cabinet scope — so an omitted dossier_id stays a loud refusal
    # instead of silently becoming a firm-wide scan. Re-checking it here
    # would be dead code: the scope resolver has already raised.
    dossier_id = (args.get("dossier_id") or "").strip()

    kwargs: dict[str, Any] = {
        "dossier_id": dossier_id or None,
        "category": args.get("category"),
        "search": args.get("query"),
    }
    folder_id = args.get("folder_id")
    if folder_id:
        # Only pass folder_id when supplied: the model's default sentinel
        # (_UNSET) means "no folder filter", while None means dossier root.
        kwargs["folder_id"] = folder_id
    docs = document_model.list_documents(**kwargs)
    if folder_id and args.get("query"):
        # The model skips the folder filter when a search term is present
        # (search spans all folders) — re-apply it so folder_path stays
        # truthful.
        docs = [d for d in docs if d.get("folder_id") == folder_id]

    if args.get("date_from") or args.get("date_to"):
        # Filter on the document's EFFECTIVE date: its own document_date
        # (date-only midnight UTC) when set, else the upload instant's
        # Montréal calendar date — so legacy docs without the new field
        # stay findable by period.
        lo = (
            _parse_iso_date(args["date_from"], "date_from")
            if args.get("date_from") else None
        )
        hi = (
            _parse_iso_date(args["date_to"], "date_to")
            if args.get("date_to") else None
        )

        def _effective(doc: dict):
            own = _as_utc(doc.get("document_date"))
            if own:
                return own.date()
            ts = _as_utc(doc.get("created_at"))
            return ts.astimezone(MTL).date() if ts else None

        def _in_window(doc: dict) -> bool:
            d = _effective(doc)
            if d is None:
                return False
            return (lo is None or d >= lo) and (hi is None or d <= hi)

        docs = [d for d in docs if _in_window(d)]
    docs = _filter_updated_since(docs, args)
    matched_dossiers: Optional[int] = None
    if scope == "cabinet":
        keep = _dossier_status_filter(args)
        if keep is not None:
            matched_dossiers = len(keep)
            docs = [d for d in docs if d.get("dossier_id") in keep]

    # _folder_paths is ONE query per dossier — resolving it firm-wide would
    # be a query per row. In cabinet scope folder_path is "" (the key stays
    # required; the description says so).
    paths = _folder_paths(dossier_id) if scope != "cabinet" else {}
    next_cursor = None
    next_offset = None
    if scope == "cabinet":
        page, next_cursor, truncated = _cabinet_page(docs, args, limit)
    else:
        page, truncated, next_offset = _offset_page(docs, args, limit)
    page = _freshen_dossier_labels(page, _live_dossiers(page))
    items = []
    for doc in page:
        size = int(doc.get("file_size", 0) or 0)
        items.append(
            {
                "id": doc.get("id", ""),
                # Present in EVERY scope: a cabinet hit must be attributable
                # without a second call, and the gap was never scope-specific.
                "dossier_id": doc.get("dossier_id") or "",
                "dossier_file_number": doc.get("dossier_file_number", ""),
                "dossier_title": doc.get("dossier_title", ""),
                "display_name": doc.get("display_name", ""),
                "category": doc.get("category", ""),
                "file_type": doc.get("file_type", ""),
                "file_size": size,
                "file_size_display": document_model.format_file_size(size),
                "version": doc.get("version", 1),
                "folder_id": doc.get("folder_id"),
                # Resolved per row from the one-query map — "" = dossier
                # root (folder_id null) or a dangling folder reference.
                "folder_path": paths.get(doc.get("folder_id") or "", ""),
                "document_date": date_str(_as_utc(doc.get("document_date"))),
                # Les DEUX textes du document. `description` était un
                # troisième champ qui recopiait le résumé; il a été retiré
                # le 2026-08-31. `notes_internes` est le texte du juriste —
                # il paraît ici parce que c'est SON système, et qu'une note
                # « pièce D-4, à coter » est précisément ce qu'un appelant
                # doit voir avant de proposer un classement.
                "resume": str((doc.get("analyse") or {}).get("resume") or ""),
                "notes_internes": doc.get("notes_internes", ""),
                "genere_depuis": doc.get("genere_depuis", ""),
                "tags": doc.get("tags", []),
                # L'état de l'analyse — sans quoi un appelant ne peut pas
                # savoir ce qui a DÉJÀ été qualifié, et refait le travail à
                # chaque reprise (le défaut qui a tué le premier lot réel).
                # Une liste blanche étroite : le résumé, les motifs et
                # l'extrait vivent dans le document, pas dans un index.
                **_analyse_apercu(doc),
                **_stamps(doc),
            }
        )
    payload = _list_payload(items, truncated)
    payload["scope"] = scope
    payload["next_cursor"] = next_cursor
    payload["dossier_status_matched"] = matched_dossiers
    if next_offset is not None:
        payload["next_offset"] = next_offset
    if folder_id:
        crumbs = folder_model.get_folder_breadcrumb(dossier_id, folder_id)
        payload["folder_path"] = " / ".join(c["name"] for c in crumbs)
    return payload


# ── 9. list_parties ─────────────────────────────────────────────────────

def list_parties(args: dict) -> dict:
    limit = _limit_arg(args, 20)
    parties = partie_model.list_parties(
        type_filter=args.get("type"),
        role_filter=args.get("contact_role"),
        search=args.get("query"),
    )
    parties = _filter_updated_since(parties, args)
    page, truncated, next_offset = _offset_page(parties, args, limit)
    items = [
        {
            "id": p.get("id", ""),
            "display_name": partie_model.display_name(p),
            "type": p.get("type", ""),
            "contact_role": p.get("contact_role", ""),
            "is_organization": p.get("type") == "organization",
            # The SELECTED address block, not address_city: an organization's
            # address lives in work_address_* (the form hides the personal
            # block for a company), so reading the personal city reported
            # every organization as city-less.
            "city": selected_address(p).get("city", ""),
            **_stamps(p),
        }
        for p in page
    ]
    payload = _list_payload(items, truncated)
    if next_offset is not None:
        payload["next_offset"] = next_offset
    return payload


# ── 10. get_partie ──────────────────────────────────────────────────────

def _addr_str(value: Any) -> str:
    """Coerce a stored address component to the string the schema declares.

    The CardDAV PUT path can store a LIST here: vobject parses an ADR
    component with an unescaped comma (a well-known bug in non-DavX5
    clients — RFC 6350 requires ``\\,``) as a Python list, and
    ``models/partie`` sanitizes only str values, so the list is committed
    silently. Without this coercion, every later ``get_partie`` for that
    contact would emit an array where the declared outputSchema promises a
    string — a strict client would reject the contact forever, with
    nothing anywhere saying why.
    """
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value or ""


def _address_block(p: dict, prefix: str) -> dict:
    return {
        "street": _addr_str(p.get(f"{prefix}_street", "")),
        "unit": _addr_str(p.get(f"{prefix}_unit", "")),
        "city": _addr_str(p.get(f"{prefix}_city", "")),
        "province": _addr_str(p.get(f"{prefix}_province", "")),
        "postal_code": _addr_str(p.get(f"{prefix}_postal_code", "")),
        "country": _addr_str(p.get(f"{prefix}_country", "")),
    }


def get_partie(args: dict) -> dict:
    partie_id = args["partie_id"]
    p = partie_model.get_partie(partie_id)
    if p is None:
        return {"found": False, "partie_id": partie_id}

    dossier_refs = []
    for d in dossier_model.list_dossiers_for_partie(partie_id):
        # Three ways a contact can be referenced by a dossier since July
        # 2026: as a client, as an opposing party, or as a PARTY'S LAWYER
        # (avocat_ids mirror). Order matters: a lawyer who is also a party
        # reports the party relation.
        if partie_id in d.get("client_ids", []):
            relation = "client"
        elif partie_id in d.get("opposing_party_ids", []):
            relation = "partie_adverse"
        else:
            relation = "avocat"
        dossier_refs.append(
            {
                "id": d.get("id", ""),
                "file_number": d.get("file_number", ""),
                "title": d.get("title", ""),
                "status": d.get("status", ""),
                "relation": relation,
            }
        )

    card = {
        "id": p.get("id", ""),
        "type": p.get("type", ""),
        "contact_role": p.get("contact_role", ""),
        "display_name": partie_model.display_name(p),
        "prefix": p.get("prefix", ""),
        "first_name": p.get("first_name", ""),
        "last_name": p.get("last_name", ""),
        "organization_name": p.get("organization_name", ""),
        "trade_name": p.get("trade_name", ""),
        "governing_law": p.get("governing_law", ""),
        "language": p.get("language", ""),
        "gender": p.get("gender", ""),
        "pronouns": p.get("pronouns", ""),
        "job_title": p.get("job_title", ""),
        "job_role": p.get("job_role", ""),
        "organization": p.get("organization", ""),
        "email": p.get("email", ""),
        "email_work": p.get("email_work", ""),
        "phone_home": p.get("phone_home", ""),
        "phone_home_display": _phone(p.get("phone_home", "")),
        "phone_cell": p.get("phone_cell", ""),
        "phone_cell_display": _phone(p.get("phone_cell", "")),
        "phone_work": p.get("phone_work", ""),
        "phone_work_display": _phone(p.get("phone_work", "")),
        "fax": p.get("fax", ""),
        "fax_display": _phone(p.get("fax", "")),
        "address": _address_block(p, "address"),
        "work_address": _address_block(p, "work_address"),
        "bar_number": p.get("bar_number", ""),
        "company_neq": p.get("company_neq", ""),
        "identity_verified": p.get("identity_verified", ""),
        "identity_verified_date": iso_mtl(_as_utc(p.get("identity_verified_date"))),
        "identity_verified_notes": p.get("identity_verified_notes", ""),
        "conflict_check": p.get("conflict_check", ""),
        "conflict_check_date": iso_mtl(_as_utc(p.get("conflict_check_date"))),
        "conflict_check_notes": p.get("conflict_check_notes", ""),
        "kyc_document_ids": p.get("kyc_document_ids", []),
        "mandataires": p.get("mandataires", []),
        "notes": p.get("notes", ""),
        # Same gap as get_dossier: the card update_partie edits now carries
        # its etag and provenance.
        **_stamps(p),
    }
    return {"found": True, "partie": card, "dossiers": dossier_refs}


# ── 11. get_billing_snapshot ────────────────────────────────────────────

def _unbilled_by_dossier() -> tuple[list[dict], bool]:
    """Group the firm's unbilled work by dossier (PA-G09).

    Two bounded indexed reads (invoiced==False, date DESC — the /temps
    indexes) grouped in Python on the denormalized dossier labels; no
    per-dossier queries. The `non_facture` filter means « not yet
    invoiced » and INCLUDES non-billable time rows (their amount is zeroed
    at write) — hours re-apply `billable` so the breakdown matches
    get_unbilled_totals' semantics. The truncation flag is honest: past
    _FETCH_CAP unbilled rows, the breakdown can disagree with the exact
    aggregate totals beside it.
    """
    time_rows, time_cursor = time_entry_model.list_time_entries_page(
        billable_filter="non_facture", limit=_FETCH_CAP
    )
    exp_rows, exp_cursor = expense_model.list_expenses_page(
        billable_filter="non_facture", limit=_FETCH_CAP
    )
    per: dict[str, dict] = {}

    def _bucket(row: dict) -> dict:
        did = row.get("dossier_id", "")
        b = per.get(did)
        if b is None:
            b = per[did] = {
                "dossier_id": did,
                "file_number": row.get("dossier_file_number", ""),
                "title": row.get("dossier_title", ""),
                "unbilled_hours": 0.0,
                "_fees": 0,
                "_expenses": 0,
            }
        return b

    for e in time_rows:
        if not e.get("billable"):
            continue
        b = _bucket(e)
        b["unbilled_hours"] = round(
            b["unbilled_hours"] + float(e.get("hours") or 0), 1
        )
        b["_fees"] += int(e.get("amount") or 0)
    for e in exp_rows:
        b = _bucket(e)
        b["_expenses"] += int(e.get("amount") or 0)

    rows = []
    for b in sorted(
        per.values(), key=lambda r: r.get("file_number", ""), reverse=True
    ):
        row = {
            "dossier_id": b["dossier_id"],
            "file_number": b["file_number"],
            "title": b["title"],
            "unbilled_hours": b["unbilled_hours"],
        }
        _money(row, "unbilled_fees", b["_fees"])
        _money(row, "unbilled_expenses", b["_expenses"])
        rows.append(row)
    return rows, bool(time_cursor or exp_cursor)


def get_billing_snapshot(args: dict) -> dict:
    dossier_id = args.get("dossier_id")
    if not dossier_id:
        unbilled = time_entry_model.get_unbilled_totals()
        outstanding_rows = [
            inv
            for inv in invoice_model.list_invoices()
            if inv.get("status") in ("envoyée", "en_retard")
        ]
        payload: dict[str, Any] = {
            "scope": "global",
            "unbilled_hours": unbilled.get("hours", 0.0),
        }
        _money(payload, "unbilled", unbilled.get("amount", 0))
        # Unbilled DISBURSEMENTS were absent from the firm-wide figure
        # entirely (PA-G09) — the aggregation + its index already existed
        # for the /temps tab and simply was never called here.
        expense_unbilled = expense_model.get_filtered_expense_totals(
            billable_filter="non_facture"
        )
        _money(
            payload, "unbilled_expenses", expense_unbilled.get("amount", 0)
        )
        # Le total vient des MÊMES lignes que `outstanding_invoices`, et de
        # leur solde vivant : appeler get_outstanding_total ici referait la
        # lecture pour rien, et surtout laisserait deux mécanismes se
        # contredire dans une seule charge. La somme porte sur TOUTES les
        # lignes, jamais sur les 50 affichées — un total tronqué serait faux.
        _money(payload, "outstanding",
               sum(invoice_model.balance_of(inv) for inv in outstanding_rows))
        by_dossier, by_dossier_truncated = _unbilled_by_dossier()
        payload["by_dossier"] = by_dossier
        payload["by_dossier_truncated"] = by_dossier_truncated
        payload["outstanding_invoices"] = [
            _invoice_row(inv) for inv in outstanding_rows[:_UNBILLED_ROW_CAP]
        ]
        payload["outstanding_invoices_truncated"] = (
            len(outstanding_rows) > _UNBILLED_ROW_CAP
        )
        return payload

    # Absence is data, not zeros: a bad dossier_id must not fabricate an
    # all-zero billing picture.
    if dossier_model.get_dossier(dossier_id) is None:
        return {"found": False, "dossier_id": dossier_id}

    time_summary = time_entry_model.get_time_summary(dossier_id)
    expense_summary = expense_model.get_expense_summary(dossier_id)
    invoice_summary = invoice_model.get_invoice_summary(dossier_id)

    payload = {
        "scope": "dossier",
        "found": True,
        "dossier_id": dossier_id,
        "total_hours": time_summary.get("total_hours", 0.0),
        "unbilled_hours": time_summary.get("unbilled_hours", 0.0),
        "invoice_count": invoice_summary.get("count", 0),
    }
    _money(payload, "total_billable", time_summary.get("total_billable_amount", 0))
    _money(payload, "unbilled_fees", time_summary.get("unbilled_amount", 0))
    _money(payload, "total_expenses", expense_summary.get("total_expenses", 0))
    _money(payload, "unbilled_expenses", expense_summary.get("unbilled_expenses", 0))
    _money(payload, "total_invoiced", invoice_summary.get("total_invoiced", 0))
    _money(payload, "total_paid", invoice_summary.get("total_paid", 0))
    _money(
        payload, "total_outstanding", invoice_summary.get("total_outstanding", 0)
    )

    entries = time_entry_model.get_unbilled_time_entries(dossier_id)
    entry_rows = []
    for e in entries[:_UNBILLED_ROW_CAP]:
        row = {
            "id": e.get("id", ""),
            "date": date_str(_as_utc(e.get("date"))),
            "description": e.get("description", ""),
            "hours": e.get("hours", 0.0),
        }
        _money(row, "rate", e.get("rate", 0))
        _money(row, "amount", e.get("amount", 0))
        entry_rows.append(row)
    payload["unbilled_time_entries"] = entry_rows
    payload["unbilled_time_entries_truncated"] = len(entries) > _UNBILLED_ROW_CAP

    expenses = expense_model.get_unbilled_expenses(dossier_id)
    expense_rows = []
    for e in expenses[:_UNBILLED_ROW_CAP]:
        row = {
            "id": e.get("id", ""),
            "date": date_str(_as_utc(e.get("date"))),
            "description": e.get("description", ""),
            "category": e.get("category", ""),
            "taxable": bool(e.get("taxable")),
        }
        _money(row, "amount", e.get("amount", 0))
        expense_rows.append(row)
    payload["unbilled_expenses_list"] = expense_rows
    payload["unbilled_expenses_list_truncated"] = len(expenses) > _UNBILLED_ROW_CAP
    return payload


# ── 11b. list_time_entries / list_expenses ─────────────────────────────

def _billing_window(args: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """Parse the optional date_from/date_to arguments into UTC midnights.

    `date` on timeentries/expenses is date-only midnight UTC, so plain UTC
    calendar boundaries are exact — the Montréal widened-window treatment
    (list_hearings) applies to true instants only, not here.
    """
    out: list[Optional[datetime]] = []
    for key in ("date_from", "date_to"):
        raw = args.get(key)
        if raw:
            d = _parse_iso_date(raw, key)
            out.append(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
        else:
            out.append(None)
    return out[0], out[1]


def _billing_rows(
    page_fn, dossier_id: Optional[str], billable_filter: Optional[str],
    date_from: Optional[datetime], date_to: Optional[datetime],
    cursor: Optional[str] = None,
) -> tuple[list[dict], bool]:
    """Fetch a bounded window of time entries/expenses, routing around the
    one combination the server-side indexes don't cover.

    dossier_id + billable_filter TOGETHER is explicitly unsupported by
    `_filtered_query` (each pairing would need its own composite index) —
    and the page functions swallow the FAILED_PRECONDITION into an empty
    list, so passing both through would silently return nothing. That
    combination fetches by dossier_id + dates and applies the flag filter
    in Python over the ≤200-row window instead.

    The incoming `cursor` resumes the SERVER scan. The window cursor the
    model hands back is deliberately NOT returned: in the Python-filtered
    branch it points past rows this function dropped, so minting the next
    page from it would skip them silently. Callers mint from the last row
    they actually return instead (see `_billing_page`).
    """
    if dossier_id and billable_filter:
        rows, window_cursor = page_fn(
            dossier_id=dossier_id, date_from=date_from, date_to=date_to,
            limit=_FETCH_CAP, cursor=cursor,
        )
        if billable_filter == "billable":
            rows = [e for e in rows if e.get("billable")]
        elif billable_filter == "non_facture":
            rows = [e for e in rows if not e.get("invoiced")]
    else:
        rows, window_cursor = page_fn(
            dossier_id=dossier_id, billable_filter=billable_filter,
            date_from=date_from, date_to=date_to, limit=_FETCH_CAP,
            cursor=cursor,
        )
    return rows, window_cursor is not None


def _billing_page(
    rows: list[dict], limit: int, window_full: bool
) -> tuple[list[dict], Optional[str], bool]:
    """Slice a billing window to `limit` and mint the next cursor.

    The key is (date, id) — the model's own server-side ordering, so a
    resumed scan lands exactly where this page stopped. A legacy row missing
    either component cannot mint a handle: rather than emit a cursor that
    would mis-position the reader, we return None and let `truncated` say
    there is more. Silent mis-positioning in a billing statement is worse
    than an honest dead end.
    """
    page = rows[:limit]
    has_more = len(rows) > limit or window_full
    next_cursor = None
    if has_more and page:
        last = page[-1]
        if last.get("date") and last.get("id"):
            next_cursor = encode_cursor([last["date"], last["id"]])
    return page, next_cursor, has_more


def list_time_entries(args: dict) -> dict:
    """Firm-wide or dossier-scoped time entries — billed AND unbilled.

    The gap this closes (PA-G04): an invoiced entry used to vanish from the
    connector permanently, and reconstructing a week firm-wide cost one
    get_billing_snapshot call per dossier.
    """
    limit = _limit_arg(args, 25)
    date_from, date_to = _billing_window(args)
    rows, window_full = _billing_rows(
        time_entry_model.list_time_entries_page,
        args.get("dossier_id"), args.get("billable_filter"),
        date_from, date_to, args.get("cursor"),
    )
    page, next_cursor, truncated = _billing_page(rows, limit, window_full)
    items = []
    for e in page:
        row = {
            "id": e.get("id", ""),
            "dossier_id": e.get("dossier_id", ""),
            "dossier_file_number": e.get("dossier_file_number", ""),
            "dossier_title": e.get("dossier_title", ""),
            "date": date_str(_as_utc(e.get("date"))),
            "description": e.get("description", ""),
            "hours": float(e.get("hours") or 0),
            "billable": bool(e.get("billable")),
            "invoiced": bool(e.get("invoiced")),
            "invoice_id": e.get("invoice_id") or None,
            **_phase_pair(e),
            # created_via comes from _stamps: the same stored value, now
            # over a wider vocabulary (see the schema description).
            **_stamps(e),
        }
        _money(row, "rate", e.get("rate", 0))
        _money(row, "amount", e.get("amount", 0))
        items.append(row)
    payload = _list_payload(items, truncated)
    payload["next_cursor"] = next_cursor
    return payload


def list_expenses(args: dict) -> dict:
    """Firm-wide or dossier-scoped disbursements — billed AND unbilled."""
    limit = _limit_arg(args, 25)
    date_from, date_to = _billing_window(args)
    rows, window_full = _billing_rows(
        expense_model.list_expenses_page,
        args.get("dossier_id"), args.get("billable_filter"),
        date_from, date_to, args.get("cursor"),
    )
    page, next_cursor, truncated = _billing_page(rows, limit, window_full)
    items = []
    for e in page:
        row = {
            "id": e.get("id", ""),
            "dossier_id": e.get("dossier_id", ""),
            "dossier_file_number": e.get("dossier_file_number", ""),
            "dossier_title": e.get("dossier_title", ""),
            "date": date_str(_as_utc(e.get("date"))),
            "description": e.get("description", ""),
            "category": e.get("category", ""),
            "taxable": bool(e.get("taxable")),
            "invoiced": bool(e.get("invoiced")),
            "invoice_id": e.get("invoice_id") or None,
            **_phase_pair(e),
            # created_via comes from _stamps: the same stored value, now
            # over a wider vocabulary (see the schema description).
            **_stamps(e),
        }
        _money(row, "amount", e.get("amount", 0))
        items.append(row)
    payload = _list_payload(items, truncated)
    payload["next_cursor"] = next_cursor
    return payload


# ── 11c. list_deletions ─────────────────────────────────────────────────

def list_deletions(args: dict) -> dict:
    """The append-only deletion trail (PA-G06) — newest first.

    Closes the vanishing-deadline hole: a hard-deleted task used to leave
    NO trace anywhere, so a briefing could not distinguish a deliberate
    withdrawal from an accidental deletion. The trail records deletions
    from the moment it shipped — silence about anything earlier, and an
    empty answer past the 200-event window means « not in the recent
    window », never « never deleted ».
    """
    limit = _limit_arg(args, 25)
    rows = audit_event_model.list_recent(
        entity_type=args.get("entity_type"),
        dossier_id=args.get("dossier_id"),
        limit=_FETCH_CAP,
    )
    if args.get("date_from"):
        lo = _parse_iso_date(args["date_from"], "date_from")
        rows = [
            r
            for r in rows
            if (ts := _as_utc(r.get("at"))) is not None
            and ts.astimezone(MTL).date() >= lo
        ]
    truncated = len(rows) > limit
    items = []
    for r in rows[:limit]:
        snap = r.get("snapshot_min") or {}
        items.append({
            "id": r.get("id", ""),
            "at": iso_mtl(_as_utc(r.get("at"))),
            "entity_type": r.get("entity_type", ""),
            "entity_id": r.get("entity_id", ""),
            "dossier_id": r.get("dossier_id", ""),
            "title": snap.get("title", ""),
            "status": snap.get("status", ""),
        })
    return _list_payload(items, truncated)


# ── Invoice register (lot 4) ────────────────────────────────────────────
#
# « Impayé » is EXACTLY what get_outstanding_total sums server-side. The two
# definitions must stay identical, or the register and the billing snapshot
# disagree about money — the one contradiction a billing tool may never ship.
_INVOICE_UNPAID = ("envoyée", "en_retard")



def _order_invoices(rows: list[dict]) -> list[dict]:
    """Sort exactly as the model does server-side: date DESC, then id ASC.

    The MIXED directions are why this cannot use ``pagination.keyset_page``,
    whose single ``descending`` flag would order id DESC: a resumed page
    would then skip or repeat rows INSIDE a same-date group, and several
    invoices sharing one date is the normal case at month end. Two stable
    passes reproduce the server order — id ascending first, then date
    descending, which preserves the id order within each date.
    """
    by_id = sorted(rows, key=lambda r: str(r.get("id") or ""))
    return sorted(by_id, key=lambda r: _as_utc(r.get("date")) or _UTC_MIN,
                  reverse=True)


def _invoice_after_cursor(inv: dict, marker: Optional[tuple]) -> bool:
    """True when *inv* falls strictly after *marker* under date DESC, id ASC.

    Mirrors Firestore's ``start_after({"date": …, "id": …})`` on the same
    ordering, so the dossier-scoped (Python-paged) branch and the firm-wide
    (server-paged) branch consume and mint the SAME cursor token.
    """
    if marker is None:
        return True
    marker_date, marker_id = marker
    when = _as_utc(inv.get("date"))
    if when is None or marker_date is None:
        return True
    if when != marker_date:
        return when < marker_date
    return str(inv.get("id") or "") > str(marker_id)


def _invoice_cursor(inv: dict) -> Optional[str]:
    """Mint the resume token from a row, or None when it cannot key one."""
    when = _as_utc(inv.get("date"))
    if when and inv.get("id"):
        return encode_cursor([when, inv["id"]])
    return None


def list_invoices(args: dict) -> dict:
    """The invoice register — issued invoices, newest first.

    Routing is by argument, and the asymmetry is deliberate:

    * ``dossier_id`` given → the model's single-equality read (served by the
      automatic index) plus Python paging. Complete for any one file.
    * no ``dossier_id`` → ``list_invoices_page``, whose ``(date DESC, id ASC)``
      and ``(status, date DESC, id ASC)`` composite indexes are already
      deployed. Exact at any register size.

    ``status_group="impayée"`` runs TWO status-filtered queries and merges
    them, never a Firestore ``in`` + ``order_by`` + ``start_after``: that
    combination raises FAILED_PRECONDITION, which the model swallows into an
    empty list — a silently blank billing statement.
    """
    limit = _limit_arg(args, 25)
    dossier_id = (args.get("dossier_id") or "").strip()
    status = args.get("status")
    status_group = args.get("status_group")
    if status and status_group:
        raise ToolArgumentError(
            "`status` and `status_group` are mutually exclusive — pass one "
            "status, or the group, not both."
        )
    date_from, date_to = _billing_window(args)

    raw_cursor = args.get("cursor")
    decoded = decode_cursor(raw_cursor)
    marker: Optional[tuple] = None
    if decoded and len(decoded) == 2 and isinstance(decoded[0], datetime):
        marker = (decoded[0], str(decoded[1]))

    if status:
        statuses: tuple = (status,)
    elif status_group == "impayée":
        statuses = _INVOICE_UNPAID
    else:
        statuses = ()

    window_full = False
    if dossier_id:
        rows = invoice_model.list_invoices(
            dossier_id=dossier_id, date_from=date_from, date_to=date_to
        )
        if statuses:
            rows = [r for r in rows if r.get("status") in statuses]
    else:
        rows = []
        for one in (statuses or (None,)):
            window, nxt = invoice_model.list_invoices_page(
                status_filter=one, date_from=date_from, date_to=date_to,
                limit=_FETCH_CAP, cursor=raw_cursor,
            )
            rows.extend(window)
            window_full = window_full or nxt is not None

    rows = _order_invoices([r for r in rows if _invoice_after_cursor(r, marker)])
    page = rows[:limit]
    truncated = len(rows) > limit or window_full
    next_cursor = _invoice_cursor(page[-1]) if (truncated and page) else None

    payload = _list_payload([_invoice_row(inv) for inv in page], truncated)
    payload["next_cursor"] = next_cursor
    return payload


def _line_item_row(item: dict) -> dict:
    """One invoice line. Descriptions are rendered VERBATIM — they are what
    printed on the client's invoice, and paraphrasing one would misquote an
    accounting document."""
    row = {
        "id": item.get("id", ""),
        "type": item.get("type", ""),
        "source_id": item.get("source_id") or None,
        "date": date_str(_as_utc(item.get("date"))),
        "description": item.get("description", ""),
        "hours": float(item["hours"]) if item.get("hours") is not None else None,
        # The model's _default_line_item sets taxable=True; a missing key
        # must NOT read as « not taxable » — that is a tax error, silently.
        "taxable": bool(item.get("taxable", True)),
    }
    # Fee lines carry an hourly rate; expense lines have none. Emitted as
    # an explicit null rather than 0 — a zero rate is a rate.
    rate = item.get("rate")
    if rate is None:
        row["rate_cents"] = None
        row["rate_display"] = None
    else:
        _money(row, "rate", rate)
    _money(row, "amount", item.get("amount", 0))
    return row


def get_invoice(args: dict) -> dict:
    """One invoice with its totals and its line items."""
    invoice_id = args["invoice_id"]
    invoice, items = invoice_model.get_invoice_with_items(invoice_id)
    if invoice is None:
        return {"found": False, "invoice_id": invoice_id}

    record = _invoice_row(invoice)
    record.update({
        "dossier_title": invoice.get("dossier_title", ""),
        "client_id": invoice.get("client_id", ""),
        "notes": invoice.get("notes", ""),
        "payment_terms": invoice.get("payment_terms", ""),
        "gst_rate_display": format_rate_fr(invoice.get("gst_rate", 0), 100),
        "qst_rate_display": format_rate_fr(invoice.get("qst_rate", 0), 1000),
    })
    for key in ("subtotal_fees", "subtotal_expenses", "subtotal",
                "gst_amount", "qst_amount", "retainer_applied"):
        _money(record, key, invoice.get(key, 0))

    line_items = [_line_item_row(i) for i in items]
    # The mandate's « la somme des postes égale son total » really concerns
    # the SUBTOTAL — `total` carries the taxes on top. Emitted as a flag so a
    # drift is visible to the reader instead of being silently re-added.
    lines_total = sum(int(i.get("amount") or 0) for i in items)
    record["line_items"] = line_items
    _money(record, "line_items_total", lines_total)
    record["subtotal_matches_line_items"] = (
        lines_total == int(invoice.get("subtotal", 0))
    )

    warnings: list[str] = []
    if not items and int(invoice.get("subtotal", 0)) != 0:
        # get_invoice_with_items swallows a subcollection read failure into
        # [], and create_invoice refuses to mint an invoice with no line —
        # so an empty list on a non-zero invoice is ALWAYS a read failure,
        # never data. Saying so beats rendering a plausible empty invoice.
        warnings.append(
            "Les postes de cette facture n'ont pas pu être lus : le total "
            "n'est pas nul mais aucun poste n'est revenu. Vérifiez la "
            "facture dans l'application avant de vous fier à ce détail."
        )
    record["warnings"] = warnings
    return {"found": True, "invoice": record}


# ── 12. list_protocol_steps ─────────────────────────────────────────────

def _protocol_payload(p: dict, today: date, dossier: Optional[dict]) -> dict:
    return {
        "id": p.get("id", ""),
        "title": p.get("title", ""),
        "protocol_type": p.get("protocol_type", ""),
        "status": p.get("status", ""),
        "court": p.get("court", ""),
        # The mismatch flag surfaces protocols created BEFORE the regime
        # gate (PA-D03) — a cq_simplifié tracking arts. 535.x on a Superior
        # Court file is a litigation risk the payload must not hide.
        "dossier_tribunal": (dossier or {}).get("tribunal", ""),
        "regime_mismatch": protocol_model.regime_mismatch(
            p.get("protocol_type", ""), dossier
        ),
        "start_date": date_str(_as_utc(p.get("start_date"))),
        "end_date": date_str(_as_utc(p.get("end_date"))),
        "notes": p.get("notes", ""),
        # WHO took it out of « actif » (lot 1a): « auto » = the completion
        # of its last step — the one closure reopening a step undoes by
        # itself; « web » / « mcp » / … = a deliberate status change; '' =
        # actif, or closed before the field existed.
        "closed_by": p.get("closed_by") or "",
        "closed_at": iso_mtl(_as_utc(p.get("closed_at"))),
        "steps": [_step_row(s, today) for s in p.get("steps", [])],
        **_stamps(p),
    }


def list_protocol_steps(args: dict) -> dict:
    dossier_id = args["dossier_id"]
    include_history = bool(args.get("include_history", False))
    today = deadlines.today_mtl()

    # Derived-only overdue status (never calls check_overdue_steps, which
    # writes to Firestore — see Phase I non-goals).
    active = protocol_model.get_protocol_for_dossier(dossier_id, active_only=True)
    dossier = dossier_model.get_dossier(dossier_id)

    protocols: list[dict] = []
    if include_history:
        for meta in protocol_model.list_protocols_for_dossier(dossier_id)[:10]:
            full = protocol_model.get_protocol(meta.get("id", ""))
            if full:
                protocols.append(full)
    elif active:
        protocols.append(active)

    return {
        "dossier_id": dossier_id,
        "has_active_protocol": active is not None,
        "protocols": [_protocol_payload(p, today, dossier) for p in protocols],
    }


# ── 13. compute_judicial_deadline ───────────────────────────────────────

def compute_judicial_deadline(args: dict) -> dict:
    start = _parse_iso_date(args["start_date"], "start_date")
    delay_days = int(args["delay_days"])
    direction = args["direction"]

    if direction == "after":
        raw = start + timedelta(days=delay_days)
    else:
        raw = start - timedelta(days=delay_days)
    deadline = deadlines.compute_deadline(start, delay_days, direction)
    was_adjusted = deadline != raw

    adjustment_reason: Optional[str] = None
    if was_adjusted:
        if raw.weekday() == 5:
            landed = "a Saturday"
        elif raw.weekday() == 6:
            landed = "a Sunday"
        elif raw in deadlines.get_quebec_holidays(raw.year):
            landed = "a Québec statutory holiday"
        else:
            landed = "a non-juridical day"
        moved = "forward" if direction == "after" else "backward"
        adjustment_reason = (
            f"{raw.isoformat()} is {landed}; "
            f"extended {moved} to the nearest juridical day (art. 83 C.p.c.)"
        )

    return {
        "start_date": start.isoformat(),
        "delay_days": delay_days,
        "direction": direction,
        "raw_date": raw.isoformat(),
        "deadline": deadline.isoformat(),
        "was_adjusted": was_adjusted,
        "adjustment_reason": adjustment_reason,
    }


# ── 14. parse_court_file_number ─────────────────────────────────────────

def parse_court_file_number(args: dict) -> dict:
    result = reference.parse_court_file_number(args["court_file_number"])
    greffe = result.get("greffe") or {}
    juridiction = result.get("juridiction") or {}
    # An alpha prefix (TAL-594531…) resolves to a _FORUMS entry — its name
    # fills the same nullable `tribunal` key the judicial path uses, so a
    # client asking « which tribunal » gets the answer either way (PA-D09).
    forum = result.get("forum") or {}
    return {
        "greffe_number": result.get("greffe_number"),
        "juridiction_number": result.get("juridiction_number"),
        "palais_de_justice": greffe.get("palais_de_justice"),
        "district_judiciaire": greffe.get("district_judiciaire"),
        "point_de_service": greffe.get("point_de_service"),
        "tribunal": juridiction.get("tribunal") or forum.get("name"),
        "competence": juridiction.get("competence"),
        "greffe_type": juridiction.get("greffe_type"),
        "is_administrative": bool(result.get("is_administrative")),
        "parse_error": result.get("parse_error"),
    }


# ── 15. get_trust_balance ────────────────────────────────────────────────


def get_trust_balance(args: dict) -> dict:
    """Trust balances held for a dossier, per client (book / cleared / in
    transit). Absence is data: a bad dossier_id returns found=False, never
    all-zeros."""
    dossier_id = args.get("dossier_id")
    if dossier_model.get_dossier(dossier_id) is None:
        return {"found": False, "dossier_id": dossier_id}
    dossier = dossier_model.get_dossier(dossier_id)
    summary = trust_model.get_trust_summary(dossier_id)
    payload: dict[str, Any] = {
        "found": True,
        "dossier_id": dossier_id,
        "file_number": dossier.get("file_number", ""),
        "title": dossier.get("title", ""),
        "has_trust": summary["has_trust"],
    }
    _money(payload, "total", summary["total_cents"])
    by_client = []
    for c in summary["by_client"]:
        row = {"client_id": c["client_id"], "client_name": c["client_name"]}
        _money(row, "book", c["book_cents"])
        _money(row, "cleared", c["cleared_cents"])
        _money(row, "in_transit", c["in_transit_cents"])
        by_client.append(row)
    payload["by_client"] = by_client
    return payload


# ── 16. list_trust_transactions ──────────────────────────────────────────


def _parse_ymd(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_trust_transactions(args: dict) -> dict:
    """The trust register — carte-client (dossier_id + client_id) or the full
    journal. date / cleared_date are date-only via date_str; never emits the
    bank transit or account number (spec §9.4)."""
    limit = _limit_arg(args, 25)
    # NEWEST FIRST (G07 companion): the model orders by sequence ASCENDING,
    # so `limit=limit+1` used to return the 25 OLDEST register movements —
    # the exact opposite of « what's the current trust posture ». A bare
    # account_id rides the DESC composite index (list_transactions_page,
    # correct at any register size); every other shape has ASC-only indexes,
    # so it fetches a bounded window and re-sorts in Python — honest only
    # up to the window, hence the widened fetch + truncation flag.
    plain_account = bool(
        args.get("account_id")
        and not args.get("dossier_id")
        and not args.get("client_id")
        and not args.get("status")
        and not args.get("date_from")
        and not args.get("date_to")
    )
    if plain_account:
        rows, next_cursor = trust_model.list_transactions_page(
            args["account_id"], cursor=args.get("cursor"), limit=limit
        )
        truncated = next_cursor is not None
    else:
        # No cursor on the filtered shapes, deliberately: their indexes are
        # ASC-only, so resuming newest-first would need a new composite one
        # — forbidden for an MCP-only query. Emitting a cursor that silently
        # walked the register the wrong way round would be worse than
        # admitting the window is all there is. The description says so.
        next_cursor = None
        rows = trust_model.list_transactions(
            account_id=args.get("account_id"),
            dossier_id=args.get("dossier_id"),
            client_id=args.get("client_id"),
            date_from=_parse_ymd(args.get("date_from")),
            date_to=_parse_ymd(args.get("date_to")),
            status=args.get("status"),
            limit=_FETCH_CAP,
        )
        truncated = len(rows) > limit
        rows.sort(
            key=lambda r: (r.get("account_id", ""), r.get("sequence") or 0),
            reverse=True,
        )
    out = []
    for r in rows[:limit]:
        item = {
            "id": r.get("id", ""),
            "sequence": r.get("sequence", 0),
            "date": date_str(_as_utc(r.get("date"))),
            "file_number": r.get("dossier_file_number", ""),
            "counterparty": r.get("counterparty", ""),
            "client_name": r.get("client_name", ""),
            "purpose": r.get("purpose", ""),
            "method": r.get("method", ""),
            "direction": r.get("direction", ""),
            "status": r.get("status", ""),
            "cleared_date": date_str(_as_utc(r.get("cleared_date"))),
            "reversed": bool(r.get("reversed_by_id")),
            "balance_after_account_cents": int(r.get("balance_after_account", 0)),
            "balance_after_client_cents": int(r.get("balance_after_client", 0)),
        }
        _money(item, "amount", r.get("amount", 0))
        out.append(item)
    return {
        "transactions": out,
        "count": len(out),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


# ── 17. get_trust_snapshot ───────────────────────────────────────────────


def get_trust_snapshot(args: dict) -> dict:
    """Firm-wide trust picture (mirrors get_billing_snapshot). Emits account
    name + institution but NEVER the transit or account number (spec §9.4).

    Reconciliation state is per account (a reconciled account no longer
    masks a never-reconciled sibling), outstanding cheques are LISTED with
    their dates (stale-cheque monitoring sits in the same regulation as the
    monthly reconciliation), and by_dossier answers « which files hold
    trust money » without one get_trust_balance call per dossier."""
    snap = trust_model.get_firm_trust_snapshot()
    accounts = []
    for a in snap.get("accounts", []):
        row = {
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "institution": a.get("institution", ""),
            "account_type": a.get("account_type", ""),
            "last_reconciliation_date": date_str(
                a.get("last_reconciliation_date")
            ),
            "never_reconciled": bool(a.get("never_reconciled")),
            "reconciliation_overdue": bool(a.get("reconciliation_overdue")),
        }
        _money(row, "book_balance", a.get("book_balance", 0))
        _money(row, "bank_balance", a.get("bank_balance", 0))
        accounts.append(row)

    cheques = []
    outstanding_rows = snap.get("outstanding_rows", [])
    for e in outstanding_rows[:_UNBILLED_ROW_CAP]:
        cheque = {
            "id": e.get("id", ""),
            "account_id": e.get("account_id", ""),
            "date": date_str(_as_utc(e.get("date"))),
            "reference": e.get("reference", ""),
            "counterparty": e.get("counterparty", ""),
            "dossier_file_number": e.get("dossier_file_number", ""),
        }
        _money(cheque, "amount", e.get("amount", 0))
        cheques.append(cheque)

    dossier_rows = trust_model.list_dossiers_with_trust()
    by_dossier = []
    for d in dossier_rows[:_UNBILLED_ROW_CAP]:
        row = {
            "dossier_id": d.get("dossier_id", ""),
            "file_number": d.get("file_number", ""),
            "title": d.get("title", ""),
            "status": d.get("status", ""),
        }
        _money(row, "book_balance", d.get("book_cents", 0))
        _money(row, "cleared_balance", d.get("cleared_cents", 0))
        by_dossier.append(row)

    payload: dict[str, Any] = {"accounts": accounts}
    _money(payload, "total_held", snap.get("total_held_cents", 0))
    payload["outstanding_count"] = snap.get("outstanding_count", 0)
    _money(payload, "outstanding_total", snap.get("outstanding_total_cents", 0))
    payload["outstanding_cheques"] = cheques
    payload["outstanding_cheques_truncated"] = (
        len(outstanding_rows) > _UNBILLED_ROW_CAP
    )
    payload["in_transit_count"] = snap.get("in_transit_count", 0)
    _money(payload, "in_transit_total", snap.get("in_transit_total_cents", 0))
    payload["by_dossier"] = by_dossier
    payload["by_dossier_truncated"] = len(dossier_rows) > _UNBILLED_ROW_CAP
    payload["last_reconciliation_date"] = date_str(snap.get("last_reconciliation_date"))
    payload["reconciliation_overdue"] = bool(snap.get("reconciliation_overdue"))
    payload["reconciliation_never_performed"] = bool(
        snap.get("reconciliation_never_performed")
    )
    return payload


# ════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — the only handlers below this line mutate Firestore.
# Both write the `notes` collection and nothing else, and both MUST bump
# the dossier's DAV CTag (see the module docstring).
# ════════════════════════════════════════════════════════════════════════

# Markdown autolinks Word-processor-free research text is full of. They are
# converted to inline-link syntax BEFORE storage because security.sanitize
# deletes every `<…>` run: `<https://canlii.ca/t/abc>` would otherwise be
# silently erased, taking the citation with it.
_AUTOLINK_URL_RE = re.compile(r"<((?:https?|ftp)://[^<>\s]+)>")
_AUTOLINK_MAILTO_RE = re.compile(r"<mailto:([^<>\s]+)>")
_AUTOLINK_EMAIL_RE = re.compile(r"<([^<>\s@]+@[^<>\s@]+\.[^<>\s@]+)>")

_PROVENANCE_SEPARATOR = "\n\n---\n\n"


def _general_scope() -> dict:
    """« Général » as a dossier-shaped dict: no labels, always DAV-visible.

    Its collection has no lifecycle, so it is never drained the way a closed
    dossier is — a general note always reaches the phone.
    """
    return {"id": "", "file_number": "", "title": "", "status": "actif"}


def _today_mtl() -> date:
    """Today's Montréal calendar date (a note is stamped in local time)."""
    return datetime.now(MTL).date()


def _normalize_markdown(text: str) -> str:
    """Rewrite Markdown autolinks into inline-link syntax.

    Purely additive to the text's meaning: `<https://x>` and `[https://x](https://x)`
    render identically, but only the second survives the tag stripper.
    """
    text = _AUTOLINK_URL_RE.sub(lambda m: f"[{m.group(1)}]({m.group(1)})", text)
    text = _AUTOLINK_MAILTO_RE.sub(
        lambda m: f"[{m.group(1)}](mailto:{m.group(1)})", text
    )
    text = _AUTOLINK_EMAIL_RE.sub(
        lambda m: f"[{m.group(1)}](mailto:{m.group(1)})", text
    )
    return text


def _survives_storage(text: str, limit: int) -> bool:
    """True when ``security.sanitize`` would store *text* byte-identically.

    Checked with the REAL sanitizer rather than a re-implementation of its
    regex: the whole point is that the handler's prediction cannot drift
    from what actually happens on the way to Firestore.
    """
    return sanitize(text, max_length=limit) == text


# Deliberately free of any excerpt of the note. The message is returned to
# the client AND recorded on the `mcp.tool.*` span by `span()`'s
# record_exception, so an excerpt here would ship privileged legal research
# to Cloud Trace. Claude already holds the text it sent and can re-read the
# stored note with get_note, so a description beats a sample.
_CHEVRON_ADVICE = (
    "Réécrivez sans chevrons : utilisez [texte](url) pour les liens et "
    "« inférieur à » / « supérieur à » pour les comparaisons."
)


def _clean_note_text(raw: str, field: str) -> str:
    """Normalize autolinks, then refuse anything the sanitizer would eat.

    ``security.sanitize`` deletes every ``<…>`` run, so `a < b et b > c`
    loses « < b et b > » with no error and no signal — the caller would
    believe the research was saved intact. Normalization rescues autolinks;
    whatever still would not survive is refused LOUDLY.
    """
    cleaned = _normalize_markdown(raw)
    if not _survives_storage(cleaned, note_model.CONTENT_MAX_LENGTH):
        raise ToolArgumentError(
            f"« {field} » contient du texte entre chevrons qui serait "
            f"supprimé à l'enregistrement. {_CHEVRON_ADVICE}"
        )
    return cleaned


def _dav_resync(
    entity_id: str,
    *,
    old_dossier_id: Optional[str],
    new_dossier_id: Optional[str],
    created: bool,
) -> tuple[bool, bool]:
    """Tell DavX5 about one committed write; return ``(new_ok, old_ok)``.

    Runs ``dav.sync.relocation_plan`` — the ONE statement of the order
    (tombstone old → bump old → un-tombstone new → bump new on a move;
    un-tombstone + bump on a creation; a bump otherwise), shared with
    ``dav.sync.relocate_resource`` — through THIS module's primitives, so
    a test that patches ``handlers.bump_ctag`` sees every step.

    Each step runs in its OWN guard and every step is attempted: the write
    is ALREADY committed when this runs, so a failure must be reported,
    never raised — an exception escaping here would reach
    ``endpoint._tools_call`` as a retryable error and the model would
    write twice. It also keeps the halves independent: an old collection
    that cannot be told the resource left must not stop the new one from
    learning it arrived. ``new_ok`` — every step on the NEW collection
    succeeded (the caller's ``ctag_bumped``); ``old_ok`` — every step on
    the OLD one did (``True`` when there was no move).
    """
    from utils.logging_setup import log_unexpected

    new_scope = collection_for(new_dossier_id)
    try:
        plan = relocation_plan(
            entity_id,
            old_dossier_id=old_dossier_id,
            new_dossier_id=new_dossier_id,
            created=created,
        )
    except Exception:
        log_unexpected("mcp write: DAV resync could not be planned")
        return False, False
    steps = {
        "bump_ctag": lambda coll: bump_ctag(coll),
        "record_tombstone": lambda coll: record_tombstone(coll, entity_id),
        "remove_tombstone": lambda coll: remove_tombstone(coll, entity_id),
    }
    new_ok = old_ok = True
    for operation, collection in plan:
        try:
            steps[operation](collection)
        except Exception:
            log_unexpected(
                "mcp write: DAV resync step failed",
                step=operation, collection=collection,
            )
            if collection == new_scope:
                new_ok = False
            else:
                old_ok = False
    return new_ok, old_ok


def _bump_note_ctag(dossier_id: str, note_id: str, *, created: bool) -> bool:
    """Bump the dossier collection's CTag; return whether it succeeded.

    Deliberately swallows its own failure. The note is ALREADY committed by
    the time this runs, and letting the exception escape would hit
    ``endpoint._tools_call``'s blanket ``except Exception``, reporting a
    committed write as a failure — the model would retry and duplicate the
    note in a client's file. The caller surfaces the outcome as
    ``dav_synced`` instead.

    A write that does not move the record: :func:`_dav_resync` with the
    same dossier on both sides — on a creation, the stale tombstone a
    recycled id could still carry is removed first (RFC 6578 requires one
    response per href, and the sync-collection builder skips a tombstoned
    id).
    """
    new_ok, _ = _dav_resync(
        note_id, old_dossier_id=dossier_id, new_dossier_id=dossier_id,
        created=created,
    )
    return new_ok


def _write_result(
    note: dict, *, created: bool, dossier: Optional[dict],
) -> dict:
    """Shared success payload for both write tools.

    *dossier* is ``None`` only when the lookup itself failed (append path —
    ``get_dossier`` swallows read errors and returns ``None``). That is NOT
    the same as a closed dossier and must not be reported as one: the note
    exists and carries a dossier_id, so the collection almost certainly
    exists too. Claim nothing about visibility in that case.

    The CTag bump belongs here and not to the model: ``models/note.py``
    never imports ``dav/``, so a write that stops at the model is visible
    in the application and never reaches the phone.
    """
    dossier_id = note.get("dossier_id", "")
    bumped = _bump_note_ctag(
        dossier_id, note.get("id", ""), created=created
    )
    status = dossier.get("status", "") if dossier is not None else None
    # The per-dossier DAV collection only exposes live resources for
    # actif/en_attente dossiers, so a note on a closed file is stored and
    # visible in the web UI but never reaches the phone. Say so rather than
    # letting the user discover it.
    dav_visible = status is None or status in ("actif", "en_attente")
    payload: dict[str, Any] = {
        "created" if created else "appended": True,
        "note": {
            "id": note.get("id", ""),
            "dossier_id": dossier_id,
            "dossier_file_number": note.get("dossier_file_number", ""),
            "dossier_title": note.get("dossier_title", ""),
            "title": note.get("title", ""),
            "category": note.get("category", ""),
            "content_length": len(note.get("content", "") or ""),
            # The note AS WRITTEN — so the etag is the new one.
            **_stamps(note),
        },
        # Two distinct facts, deliberately not collapsed into one: whether
        # the sync trigger actually fired, and whether the phone will ever
        # see the result. A closed dossier bumps fine but stays invisible.
        "ctag_bumped": bumped,
        "dav_synced": bumped and dav_visible,
        "warnings": [],
    }
    if not bumped:
        payload["warnings"].append(
            "La note est enregistrée, mais la synchronisation DavX5 n'a pas pu "
            "être déclenchée. Elle apparaîtra sur l'appareil au prochain "
            "changement dans ce dossier. Ne pas réessayer l'écriture."
        )
    if not dav_visible:
        payload["warnings"].append(
            f"Le dossier est « {status} » : la note est enregistrée et visible "
            "dans l'application, mais les dossiers fermés ou archivés ne sont "
            "pas exposés à DavX5, donc elle n'apparaîtra pas sur le téléphone."
        )
    return payload


# ── 24. get_coverage_report ─────────────────────────────────────────────


def _coverage_dossier_view(d: dict) -> dict:
    """The projection the pure checks consume — plain data, no model calls.

    Derived values (prescription status, the taxonomy flag, valeur) are
    resolved HERE, once, so ``mcp/coverage.py`` stays importable without a
    Firestore client.
    """
    action = taxonomie.get_action(d.get("action", "") or "")
    return {
        "id": d.get("id", ""),
        "file_number": d.get("file_number", ""),
        "title": d.get("title", ""),
        "status": d.get("status", ""),
        "forum_type": d.get("forum_type") or "judiciaire",
        "tribunal": d.get("tribunal", ""),
        "court_file_number": d.get("court_file_number", ""),
        "action": d.get("action", ""),
        "action_a_valider": bool(action.a_valider) if action else False,
        "valeur_cents": d.get("valeur"),
        "prescription_status": dossier_model.derive_prescription(d)["status"],
        "opposing_parties": d.get("opposing_parties") or [],
        "significations": d.get("significations") or [],
        "client_ids": [c for c in (d.get("client_ids") or []) if c],
    }


def get_coverage_report(args: dict) -> dict:
    """Firm-wide hygiene sweep — which open files are missing something.

    Six round-trips for the whole firm, instead of one get_dossier per
    dossier. Every finding is an OBSERVATION: the connector cannot create a
    protocol, verify an identity or file a signification, and each detail
    string points at the application.
    """
    status = args.get("status", "actif")
    limit = _limit_arg(args, 25)
    requested = args.get("checks")

    scoped = dossier_model.list_dossiers(status_filter=status)
    views = [_coverage_dossier_view(d) for d in scoped]

    # ── Context, and the two guards that matter more than any check ─────
    skip: set = set()
    protocols = protocol_model.list_protocols(status_filter="actif")
    by_dossier: dict = {}
    for p in protocols:
        did = p.get("dossier_id", "")
        if did:
            by_dossier[did] = {
                "protocol_type": p.get("protocol_type", ""),
                "regime_mismatch": protocol_model.regime_mismatch(
                    p.get("protocol_type", ""),
                    next((d for d in scoped if d.get("id") == did), None),
                ),
            }
    # list_protocols swallows a read failure into []. Left unguarded,
    # PROTO_ABSENT would then fire on EVERY dossier in scope — a
    # false-manquement storm on a compliance report, which is worse than
    # reporting nothing.
    protocol_index_complete = bool(protocols) or not views
    if not protocol_index_complete:
        skip.update({"PROTO_ABSENT", "PROTO_REGIME"})

    client_ids = [cid for v in views for cid in v["client_ids"]]
    parties = partie_model.get_parties_bulk(client_ids) if client_ids else {}
    # Same reasoning, and the stake is higher: never report a client as
    # unverified because a read failed. That is a regulatory accusation.
    kyc_checked = bool(parties) or not client_ids
    kyc_reason = ""
    if not kyc_checked:
        skip.update({"CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE",
                     "CLIENT_INTROUVABLE"})
        kyc_reason = (
            "Les fiches des clients n'ont pas pu être lues ; les contrôles "
            "déontologiques sont écartés de ce rapport."
        )

    if requested:
        skip.update(set(coverage.ALL_CODES) - set(requested))

    ctx = {
        "active_protocol_dossiers": set(by_dossier),
        "active_protocols_by_dossier": by_dossier,
        "clients_of": lambda v: [
            parties[cid] for cid in v["client_ids"] if cid in parties
        ],
        "missing_clients_of": lambda v: [
            cid for cid in v["client_ids"] if cid not in parties
        ],
    }

    items = []
    by_code: dict = {}
    manquements = signalements = 0
    for view in views:
        findings = coverage.run_checks(view, ctx, skip=frozenset(skip))
        if not findings:
            continue
        for f in findings:
            by_code[f["code"]] = by_code.get(f["code"], 0) + 1
            if f["severity"] == coverage.MANQUEMENT:
                manquements += 1
            else:
                signalements += 1
        items.append({
            "dossier_id": view["id"],
            "file_number": view["file_number"],
            "title": view["title"],
            "status": view["status"],
            "manquements": sum(
                1 for f in findings if f["severity"] == coverage.MANQUEMENT
            ),
            "signalements": sum(
                1 for f in findings if f["severity"] == coverage.SIGNALEMENT
            ),
            "findings": findings,
        })

    # ── Cross-scope: the ghost task on a closed file ────────────────────
    # These fire on CLOSED dossiers, so under the default « actif » filter
    # they could never appear. One unfiltered dossier read classifies them.
    cross: list[dict] = []
    if "TACHE_OUVERTE_DOSSIER_FERME" not in skip or \
            "PROTO_ACTIF_DOSSIER_FERME" not in skip:
        closed = {
            d.get("id", ""): d for d in dossier_model.list_dossiers()
            if d.get("status") in coverage.CLOSED_STATUSES
        }
        if closed:
            if "TACHE_OUVERTE_DOSSIER_FERME" not in skip:
                open_tasks: list[dict] = []
                for st in ("à_faire", "en_cours"):
                    open_tasks.extend(task_model.list_tasks_by_status(st))
                stale: dict = {}
                for t in open_tasks:
                    did = t.get("dossier_id") or ""
                    if did in closed:
                        stale[did] = stale.get(did, 0) + 1
                for did, count in stale.items():
                    cross.append(_cross_finding(
                        "TACHE_OUVERTE_DOSSIER_FERME", closed[did],
                        f"{count} tâche(s) encore active(s) sur un dossier "
                        f"« {closed[did].get('status', '')} ». Fermez-les ou "
                        "rouvrez le dossier dans l'application.",
                    ))
            if "PROTO_ACTIF_DOSSIER_FERME" not in skip and protocol_index_complete:
                for did in by_dossier:
                    if did in closed:
                        cross.append(_cross_finding(
                            "PROTO_ACTIF_DOSSIER_FERME", closed[did],
                            "Protocole encore actif sur un dossier "
                            f"« {closed[did].get('status', '')} ».",
                        ))
    for f in cross:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    page, next_cursor, truncated = _cabinet_page(
        items, args, limit, key=lambda r: (r["file_number"], r["dossier_id"])
    )
    ran = [c for c in coverage.ALL_CODES if c not in skip]
    return {
        "scope": {
            "status": status,
            "dossiers_examined": len(views),
            "checks_run": ran,
            "checks_skipped": sorted(skip),
        },
        "summary": {
            "dossiers_with_findings": len(items),
            "manquements": manquements,
            "signalements": signalements,
            "by_code": [
                {
                    "code": code,
                    "label": coverage.LABEL_BY_CODE.get(code, code),
                    "severity": coverage.SEVERITY_BY_CODE.get(code, ""),
                    "count": count,
                }
                for code, count in sorted(by_code.items())
            ],
        },
        "items": page,
        "count": len(page),
        "truncated": truncated,
        "next_cursor": next_cursor,
        "cross_scope_findings": cross,
        "data_completeness": {
            "protocol_index_complete": protocol_index_complete,
            "kyc_checked": kyc_checked,
            "kyc_reason": kyc_reason,
        },
    }


def _cross_finding(code: str, dossier: dict, detail: str) -> dict:
    return {
        "code": code,
        "severity": coverage.SEVERITY_BY_CODE.get(code, coverage.SIGNALEMENT),
        "label": coverage.LABEL_BY_CODE.get(code, code),
        "dossier_id": dossier.get("id", ""),
        "file_number": dossier.get("file_number", ""),
        "title": dossier.get("title", ""),
        "status": dossier.get("status", ""),
        "detail": detail,
    }


# ── 18. create_note (WRITE) ─────────────────────────────────────────────

def create_note(args: dict) -> dict:
    return run_write("create_note", args, lambda: _create_note_impl(args))


def _create_note_impl(args: dict) -> dict:
    # An ABSENT dossier_id means « Général ». A SUPPLIED one must resolve:
    # models/note._validate no longer requires a dossier, so a hallucinated
    # UUID would otherwise be silently downgraded to a general note instead
    # of erroring — research filed where nobody will look for it. The
    # refusal message (advice tail included) is byte-identical to the
    # pre-consolidation inline block.
    dossier_id, dossier = _resolve_write_dossier(
        args,
        required=False,
        advice=(
            " N'omettez pas dossier_id pour contourner cette erreur : une "
            "note sans dossier va dans « Général »."
        ),
    )

    title = _clean_note_text((args.get("title") or "").strip(), "title")
    body = _clean_note_text((args.get("content") or "").strip(), "content")
    category = args.get("category") or "recherche"

    stamp = f"*Note rédigée par Claude le {format_date_fr(_today_mtl())}*"
    content = f"{stamp}\n\n{body}"
    # Post-condition on the EXACT string that will be stored. Checking the
    # parts is not enough: TAG_RE's `[^<>]` body matches across newlines, so
    # a join can create a match that neither half contained.
    if not _survives_storage(content, note_model.CONTENT_MAX_LENGTH):
        raise ToolArgumentError(
            "Le contenu assemblé ne peut pas être enregistré intact. "
            + _CHEVRON_ADVICE
        )

    # EXPLICIT whitelist — never `**args`. models/note.create_note used to
    # honour a caller-supplied `id` and set() the whole document, so a stray
    # `id` overwrote an existing note outright; it discards `id` and
    # `vjournal_uid` since lot 1a (only the DAV create's `dav_id` keyword
    # names a document), and this whitelist stays as the belt. `created_at`
    # is still passthrough and would corrupt the VJOURNAL (a non-datetime
    # created_at drops CREATED, the documented jtx Board NOT-NULL crash).
    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": title,
        "content": content,
        "category": category,
        "pinned": False,
    }
    note, errors = note_model.create_note(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _write_result(note, created=True, dossier=dossier)


# ── 19. append_to_note (WRITE) ──────────────────────────────────────────

def append_to_note(args: dict) -> dict:
    return run_write(
        "append_to_note", args, lambda: _append_to_note_impl(args)
    )


def _append_to_note_impl(args: dict) -> dict:
    note_id = (args.get("note_id") or "").strip()
    existing = note_model.get_note(note_id)
    if existing is None:
        raise ToolArgumentError(
            f"Note introuvable : {note_id}. Utilisez list_notes pour obtenir "
            "un note_id valide."
        )
    # The « Théorie de la cause » is written through edit_analyse ONLY: an
    # append at the END of the note would land after bloc H — outside every
    # bloc, where the eight-heading structure the lawyer works in cannot
    # place it. edit_analyse's append mode completes ONE bloc instead.
    # Message describes, never quotes (Cloud Trace).
    if existing.get("is_analyse"):
        raise ToolArgumentError(
            "Cette note est la théorie de la cause du dossier : elle ne se "
            "complète pas à la fin. Utilisez edit_analyse (mode « append » "
            "pour compléter un bloc, « replace » pour le réécrire). Rien n'a "
            "été modifié."
        )

    addition = _clean_note_text((args.get("content") or "").strip(), "content")
    stamp = f"*Ajouté par Claude le {format_date_fr(_today_mtl())}*"
    block = f"{_PROVENANCE_SEPARATOR}{stamp}\n\n{addition}"

    current = existing.get("content", "") or ""
    combined = current + block

    # Length FIRST: `_survives_storage` calls sanitize, which also truncates
    # at the cap, so an over-long note would otherwise trip the chevron
    # guard below and report the wrong reason.
    projected = len(combined)
    # Refuse BEFORE writing. security.sanitize truncates at
    # CONTENT_MAX_LENGTH with no exception and no flag, and update_note
    # then set()s the truncated document — the tail of the note would be
    # permanently lost behind a success envelope.
    if projected > note_model.CONTENT_MAX_LENGTH:
        raise ToolArgumentError(
            f"La note est trop longue : {len(current)} caractères déjà "
            f"enregistrés, plafond {note_model.CONTENT_MAX_LENGTH}. L'ajout de "
            f"{len(block)} caractères la dépasserait. Créez une nouvelle note "
            "avec create_note plutôt que de tronquer celle-ci."
        )

    # THE join guard. `_clean_note_text` cleared the addition in isolation,
    # but `update_note` sanitizes `current + block` as one string, and
    # TAG_RE (`<[^<>]*>`) matches across newlines. An unpaired « < » already
    # sitting in the note (legal for every other write path — the web form
    # and DAV PUT both accept it) plus any « > » in the addition — a
    # Markdown blockquote is the obvious one — makes the regex span the
    # join and delete the tail of the lawyer's note, the separator, and the
    # provenance stamp. Silently, behind an "appended: true" envelope.
    if not _survives_storage(combined, note_model.CONTENT_MAX_LENGTH):
        raise ToolArgumentError(
            "Ajout refusé : combinés, la note existante et votre texte "
            "contiennent une paire de chevrons qui ferait disparaître du "
            "contenu déjà enregistré (un « < » non fermé dans la note, suivi "
            "d'un « > » dans l'ajout — une citation Markdown « > » suffit). "
            "Relisez la note avec get_note, signalez le chevron à "
            "l'utilisateur, ou créez plutôt une nouvelle note avec "
            "create_note. Rien n'a été modifié."
        )

    dossier_id = existing.get("dossier_id", "")
    dossier = (
        dossier_model.get_dossier(dossier_id) if dossier_id else _general_scope()
    )
    # Whitelist of exactly one field: a stray dossier_id in the update would
    # move the note between dossiers — and between DAV collections, leaving
    # the origin collection un-bumped.
    #
    # Compare-and-set against the note this handler READ (plan rule 3):
    # `combined` is that read plus the block, and update_note sets the
    # whole content — so an edit landing in between (the phone, another
    # tab, a parallel append) used to be ERASED under a success envelope.
    # It is now refused, and nothing is written.
    note, errors = note_model.update_note(
        note_id, {"content": combined},
        expected_etag=concurrency.etag_of(existing),
    )
    _raise_if_stale(
        errors, tool="append_to_note", subject="Cette note a été modifiée",
        reread=lambda: note_model.get_note(note_id), readers=("get_note",),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    result = _write_result(note, created=False, dossier=dossier)
    result["appended_chars"] = len(block)
    return result


# ════════════════════════════════════════════════════════════════════════
# WP16 — the four entity creators (create-only, never modify, never delete)
# ════════════════════════════════════════════════════════════════════════

# task/hearing _sanitize_data caps EVERY string at 2000 characters — a far
# lower ceiling than notes' 100k, and the same silent-truncation class the
# note pre-checks exist for.
_ENTITY_FIELD_MAX = 2000
# models/dossier sanitizes `sommaire` at 5000, everything else at 2000.
_SOMMAIRE_MAX = 5000


def _clean_entity_text(
    raw: str, field: str, limit: Optional[int] = None
) -> str:
    """Length-then-chevron refusal at a field's REAL storage ceiling.

    *limit* exists because the ceiling is not uniform: ``models/dossier``
    sanitizes ``sommaire`` at 5000 and everything else at 2000. Refusing a
    3000-character sommaire « because it exceeds 2000 » would be a refusal
    naming a limit that does not apply to it.
    """
    ceiling = _ENTITY_FIELD_MAX if limit is None else limit
    value = (raw or "").strip()
    if len(value) > ceiling:
        raise ToolArgumentError(
            f"« {field} » dépasse {ceiling} caractères — il serait "
            "tronqué silencieusement à l'enregistrement. Raccourcissez, ou "
            "mettez le détail dans une note (create_note)."
        )
    if not _survives_storage(value, ceiling):
        raise ToolArgumentError(
            f"« {field} » contient du texte entre chevrons qui serait "
            f"supprimé à l'enregistrement. {_CHEVRON_ADVICE}"
        )
    return value


def _resolve_write_dossier(
    args: dict, *, required: bool, advice: str = ""
) -> tuple[str, dict]:
    """Resolve the write target dossier; refuse an unknown id, never
    downgrade (the create_note rule). ``required=False`` allows the
    « Général » fallback for agenda entities; ``advice`` appends a
    tool-specific sentence to the refusal (create_note's « n'omettez
    pas… » warning)."""
    dossier_id = (args.get("dossier_id") or "").strip()
    if dossier_id:
        dossier = dossier_model.get_dossier(dossier_id)
        if dossier is None:
            raise ToolArgumentError(
                f"Dossier introuvable : {dossier_id}. Utilisez list_dossiers "
                "ou get_dossier pour obtenir un dossier_id valide." + advice
            )
        return dossier_id, dossier
    if required:
        raise ToolArgumentError(
            "dossier_id est requis pour cette écriture — utilisez "
            "list_dossiers pour le trouver."
        )
    return "", _general_scope()


def _write_date(args: dict, key: str, *, required: bool) -> Optional[datetime]:
    """A YYYY-MM-DD argument as a date-only midnight-UTC datetime."""
    raw = (args.get(key) or "").strip()
    if not raw:
        if required:
            raise ToolArgumentError(f"`{key}` est requis (AAAA-MM-JJ).")
        return None
    d = _parse_iso_date(raw, key)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _resolve_phase_pair(args: dict) -> tuple[str, str]:
    """The optional Phase O pair, with the schema-documented ergonomics.

    Unknown codes never reach here (schema enums). ``sous_phase`` alone
    derives its parent from the prefix; a phase alone imputes to its ``-00``;
    a contradictory pair is refused in French BEFORE anything is written.
    Both omitted → ``("", "")`` = non renseignée.
    """
    phase, sous = phases.resolve_pair(
        args.get("phase") or "", args.get("sous_phase") or ""
    )
    if phase and sous and phases.phase_of(sous) != phase:
        raise ToolArgumentError(
            f"La sous-phase « {sous} » n'appartient pas à la phase "
            f"« {phase} ». Corrigez le couple ou omettez `phase` pour "
            "qu'elle soit déduite du préfixe."
        )
    return phase, sous


# « The caller did not say where the record was before this write » — the
# default of _entity_write_result's previous_dossier_id, distinct from None
# and "" (a record that WAS in « Général »).
_UNCHANGED: Any = object()


def _entity_write_result(
    entity_type: str,
    entity: dict,
    *,
    dossier: Optional[dict],
    dav_exposed: bool,
    verb: str = "created",
    created: bool = True,
    wrote: bool = True,
    previous_dossier_id: Any = _UNCHANGED,
) -> dict:
    """Success payload for the WP16 creators and WP17 recorders.

    DAV-exposed entities (task, hearing) carry ctag_bumped/dav_synced with
    the exact semantics of the note tools; time entries, expenses and
    dossier-array additions are not DAV-exposed and deliberately do NOT
    fake those keys.

    ``wrote=False`` means the call reached a NO-OP — ``complete_task`` on
    a task already in the requested state, and since lot 1b every agenda
    edit whose values are all already stored (``update_task``,
    ``update_note``, ``reopen_task``, ``update_hearing``, the protocol
    tools, an ``edit_analyse`` that changes nothing). Nothing
    was stored, so nothing must sync: bumping a CTag there would tell
    DavX5 to re-fetch a collection that did not change, and the two DAV
    warnings would describe a write that never happened. This carried on
    ``dry_run`` until 2026-08-27, which is why removing the preview would
    have silently started bumping on a no-op — the parameter was doing
    two unrelated jobs under one name. It has its own name now.

    A no-op still EMITS ``ctag_bumped``/``dav_synced`` (both false) on a
    DAV-exposed entity. The declared outputSchema REQUIRES them, so
    skipping the whole block instead of skipping only the bump would ship
    a payload a strict client rejects with nothing said on our side —
    exactly what ``tests/test_mcp_output_schemas.py`` exists to catch, and
    what it did catch the day ``wrote`` replaced ``dry_run`` here.

    *previous_dossier_id* — where the record was BEFORE this write — is for
    a write that can MOVE a DAV-exposed record to another dossier (lot
    1b's movers: ``update_task``, ``update_note``, ``update_hearing``).
    Passed, the resync is the full
    relocation (:func:`_dav_resync`) and the payload gains
    ``previous_collection_cleared``: ``false`` only when the record moved
    and the OLD collection could not be told it left — a stale copy may
    then stay on the phone in the old dossier. A tool that passes it must
    declare the key (``output_schemas._entity_write_result(…,
    relocates=True)``); a tool that does not never emits it, and
    ``tests/test_mcp_dav_resync.py`` derives the pairing.
    """
    payload: dict[str, Any] = {
        verb: True,
        "entity_type": entity_type,
        "entity": entity,
        "warnings": [],
    }
    relocating = previous_dossier_id is not _UNCHANGED
    if dav_exposed:
        old_cleared = True
        if not wrote:
            bumped = False
        elif relocating:
            bumped, old_cleared = _dav_resync(
                entity.get("id", ""),
                old_dossier_id=previous_dossier_id,
                new_dossier_id=entity.get("dossier_id") or "",
                created=created,
            )
        else:
            bumped = _bump_note_ctag(
                entity.get("dossier_id") or "", entity.get("id", ""),
                created=created,
            )
        status = dossier.get("status", "") if dossier is not None else None
        dav_visible = status is None or status in ("actif", "en_attente")
        payload["ctag_bumped"] = bumped
        payload["dav_synced"] = bumped and dav_visible
        if relocating:
            payload["previous_collection_cleared"] = old_cleared
        if wrote and not bumped:
            payload["warnings"].append(
                "L'écriture est enregistrée, mais la synchronisation DavX5 "
                "n'a pas pu être déclenchée. Elle apparaîtra sur l'appareil "
                "au prochain changement dans ce dossier. Ne pas réessayer."
            )
        if wrote and not old_cleared:
            payload["warnings"].append(
                "L'écriture est enregistrée, mais l'ancien dossier n'a pas "
                "pu être informé du déplacement : une copie périmée peut "
                "rester sur le téléphone dans l'ancien dossier. Ne pas "
                "réessayer."
            )
        if wrote and not dav_visible:
            payload["warnings"].append(
                f"Le dossier est « {status} » : l'entrée est enregistrée et "
                "visible dans l'application, mais les dossiers fermés ou "
                "archivés ne sont pas exposés à DavX5 — elle n'apparaîtra "
                "pas sur le téléphone."
            )
    return payload


# ── 34. get_reference_vocabulary (read) ─────────────────────────────────
# Les vocabulaires que les modèles VALIDENT mais n'énumèrent jamais dans
# leurs refus : « Domaine invalide. » ne dit pas quels domaines existent, et
# rien parmi les outils de lecture n'exposait la taxonomie. Sans cette
# fenêtre, la colonne « classification » du tableur ne pouvait qu'être
# devinée — puis refusée. Toutes les sources sont des modules PURS ou des
# tables en mémoire : aucune lecture Firestore, aucun littéral recopié.

_VOCAB_CAP = 200


def _vocab_domaines() -> list[dict]:
    return [
        {"code": code, "label": taxonomie.DOMAINE_LABELS.get(code, ""), "note": ""}
        for code in taxonomie.VALID_DOMAINES
        if code
    ]


def _vocab_actions(domaine: str) -> list[dict]:
    if domaine:
        actions = taxonomie.actions_for(domaine)
    else:
        actions = tuple(taxonomie.ACTIONS.values())
    return [
        {
            "code": a.code,
            "label": a.libelle,
            # Le délai est INDICATIF et le dit : la taxonomie suggère, elle
            # ne fixe jamais. « » là où la source ne porte pas de période
            # propre est une valeur voulue, pas une lacune.
            "note": a.delai or "",
        }
        for a in actions
    ]


def _vocab_prescription_types() -> list[dict]:
    from utils.recours import PRESCRIPTION_LABELS

    return [
        {"code": code, "label": label, "note": ""}
        for code, label in PRESCRIPTION_LABELS.items()
        if code
    ]


def _vocab_forums() -> list[dict]:
    rows: list[dict] = []
    for category, label, forums in reference.forums_by_category():
        for f in forums:
            rows.append(
                {"code": f.get("forum_key", ""), "label": f.get("name", ""),
                 "note": label}
            )
    return rows


def _vocab_districts() -> list[dict]:
    return [
        {"code": d, "label": d, "note": ""}
        for d in dossier_model.VALID_DISTRICTS
        if d
    ]


def _vocab_phases() -> list[dict]:
    rows: list[dict] = []
    for code in phases.VALID_PHASES:
        if not code:
            continue
        rows.append({
            "code": code,
            "label": phases.PHASE_LABELS.get(code, ""),
            "note": "phase",
        })
        for sc in phases.sous_codes_for(code):
            rows.append({
                "code": sc.code, "label": sc.libelle, "note": f"sous-code de {code}",
            })
    return rows


# ── Les vocabulaires de l'ANALYSE documentaire ──────────────────────────
#
# Ils manquaient. Les 42 sous-natures, les 7 privilèges et les 14 codes de
# preuve n'existaient pour un appelant que comme énums NUES du schéma de
# `record_document_analysis` : sans libellé, sans ancrage légal, sans
# niveau, et sans la `reserve` que `analyse_taxonomies` désigne pourtant
# comme « le texte que l'INTERFACE doit rendre à côté du code ».
#
# La compétence collée les porte, et c'est la voie normale — mais elle est
# du texte STATIQUE dans un Skill, que rien ne relie au code et dont aucun
# test ne détecte la dérive. Ces quatre `kind` sont l'assurance : la table
# elle-même, atteignable au moment où le modèle en a besoin.
#
# Tout tient dans le contrat existant `{code, label, note}` — l'information
# se replie dans `note` plutôt que d'élargir un schéma de sortie que 53
# outils partagent. Dérivé, jamais recopié.


def _vocab_sous_natures() -> list[dict]:
    from utils import analyse_taxonomies as tax

    return [
        {
            "code": code,
            "label": e.libelle,
            "note": " · ".join(
                p for p in (e.nature, e.famille.lower(), e.ancrage) if p
            ),
        }
        for code, e in tax.SOUS_NATURES.items()
    ]


def _vocab_privileges() -> list[dict]:
    """Le vocabulaire le plus conséquent : il porte le NIVEAU.

    La règle asymétrique du domaine vit ici — sous-estimer une protection
    est un manquement (art. 60.4 du Code des professions), la surestimer
    fait perdre du temps. La `reserve` entre donc dans la note : elle dit
    ce qu'un code ne garantit PAS, et la taire serait pire que l'omettre.
    """
    from utils import analyse_taxonomies as tax

    lignes = []
    for code, p in tax.PRIVILEGES.items():
        bouts = [f"niveau {p.niveau}"]
        if p.fondement:
            bouts.append(p.fondement)
        if p.implique:
            bouts.append("entraîne " + ", ".join(p.implique))
        if p.reserve:
            bouts.append("RÉSERVE : " + p.reserve)
        lignes.append({
            "code": code,
            "label": p.portee or code,
            "note": " · ".join(bouts),
        })
    return lignes


def _vocab_moyens_preuve() -> list[dict]:
    from utils import analyse_taxonomies as tax

    return [
        {"code": code, "label": libelle, "note": ancrage}
        for code, (libelle, ancrage) in tax.MOYENS_PREUVE.items()
    ]


def _vocab_qualifications_ecrit() -> list[dict]:
    from utils import analyse_taxonomies as tax

    return [
        {
            "code": code,
            "label": libelle,
            # La règle d'axe de l'Annexe C, portée par chaque ligne : sans
            # elle, un appelant apprend le refus par l'échec.
            "note": " · ".join(
                p for p in (ancrage, "exige moyen_preuve = ECRIT"
                            if code != "NON_DETERMINE" else "") if p
            ),
        }
        for code, (libelle, ancrage) in tax.QUALIFICATIONS_ECRIT.items()
    ]


_VOCABULARIES = {
    "domaines": _vocab_domaines,
    "prescription_types": _vocab_prescription_types,
    "forums": _vocab_forums,
    "districts": _vocab_districts,
    "phases": _vocab_phases,
    "sous_natures": _vocab_sous_natures,
    "privileges": _vocab_privileges,
    "moyens_preuve": _vocab_moyens_preuve,
    "qualifications_ecrit": _vocab_qualifications_ecrit,
}


def get_reference_vocabulary(args: dict) -> dict:
    kind = args.get("kind") or ""
    domaine = (args.get("domaine") or "").strip()
    if domaine and kind != "actions":
        raise ToolArgumentError(
            "`domaine` ne filtre que `kind: \"actions\"`."
        )
    if kind == "actions":
        if domaine and domaine not in taxonomie.VALID_DOMAINES:
            raise ToolArgumentError(
                f"Domaine inconnu : « {domaine} ». Appelez d'abord "
                "get_reference_vocabulary(kind=\"domaines\")."
            )
        rows = _vocab_actions(domaine)
    else:
        builder = _VOCABULARIES.get(kind)
        if builder is None:
            raise ToolArgumentError(f"Vocabulaire inconnu : « {kind} ».")
        rows = builder()

    truncated = len(rows) > _VOCAB_CAP
    return {
        "kind": kind,
        "domaine": domaine,
        "items": rows[:_VOCAB_CAP],
        "count": len(rows[:_VOCAB_CAP]),
        "truncated": truncated,
    }


# ── 35. find_imported (read) ────────────────────────────────────────────

_LEGACY_COLLECTIONS = (
    ("partie", "parties"),
    ("dossier", "dossiers"),
    ("time_entry", "timeentries"),
    ("expense", "expenses"),
    ("invoice", "invoices"),
)


def _legacy_label(entity_type: str, doc: dict) -> str:
    if entity_type == "partie":
        return partie_model.display_name(doc)
    if entity_type == "dossier":
        return f"{doc.get('file_number', '')} {doc.get('title', '')}".strip()
    if entity_type == "invoice":
        return doc.get("invoice_number", "")
    return doc.get("description", "")


def find_imported(args: dict) -> dict:
    """Retrouve ce qu'une reprise a déjà écrit, par son identifiant d'origine.

    La protection anti-doublon DURABLE : la clé d'idempotence expire en 24 h
    et une reprise s'étale sur des jours. Fail CLOSED — la question posée est
    « dois-je créer ? », et le connecteur ne peut rien supprimer.
    """
    from models import find_by_legacy_ref

    legacy_ref = (args.get("legacy_ref") or "").strip()
    if not legacy_ref:
        raise ToolArgumentError("`legacy_ref` est requis.")
    wanted_type = (args.get("entity_type") or "").strip()

    matches: list[dict] = []
    for entity_type, collection in _LEGACY_COLLECTIONS:
        if wanted_type and entity_type != wanted_type:
            continue
        for doc in find_by_legacy_ref(collection, legacy_ref):
            matches.append({
                "entity_type": entity_type,
                "id": doc.get("id", ""),
                "label": _legacy_label(entity_type, doc),
                "dossier_id": doc.get("dossier_id") or None,
            })
    return {"legacy_ref": legacy_ref, "matches": matches, "count": len(matches)}


# ── Lot Q — helpers partagés par les écritures de reprise ───────────────


def _supplied(args: dict, keys) -> dict:
    """Les seules clés que l'appelant a RÉELLEMENT fournies.

    Par PRÉSENCE, jamais ``args.get(k, defaut)``. Sur un modèle qui écrit le
    document entier (``{**existing, **data}`` puis ``set()``), injecter un
    défaut n'est pas une commodité : c'est une SUPPRESSION. La ligne la plus
    dangereuse de la famille serait ``args.get("billable", True)`` — elle
    refacturerait en silence une entrée délibérément non facturable.
    """
    return {k: args[k] for k in keys if k in args}


def _clean_partie_text(value, field: str) -> str:
    """Un champ de contact, nettoyé au plafond des entités (2000)."""
    return _clean_entity_text(str(value if value is not None else ""), field)


_ADDRESS_KEYS = ("street", "unit", "city", "province", "postal_code", "country")
# Un bloc partiel est refusé sur ces quatre-là ; « unit » et « postal_code »
# peuvent légitimement rester vides.
_ADDRESS_REQUIRED = ("street", "city", "province", "country")


def _require_address_bloc(args: dict, prefix: str) -> dict:
    """Les six clés d'un bloc d'adresse, ou aucune.

    ``models/partie._normalize`` appelle ``utils/validators.apply_address_
    defaults``, qui écrit Canada / Québec / Montréal DANS le dictionnaire de
    l'appelant dès qu'une rue est présente et que ces clés sont vides ; et
    ``update_partie`` fusionne ``{**existing, **data}``. Un contact torontois
    envoyé avec une rue et sans ville est donc silencieusement DÉMÉNAGÉ — sur
    une facture que le client recevra.

    La règle est délibérément plus grossière que la logique de l'injecteur :
    elle reste correcte si cette logique change.
    """
    keys = [f"{prefix}_{k}" for k in _ADDRESS_KEYS]
    present = [k for k in keys if k in args]
    if not present:
        return {}
    # Every one of the six must be PRESENT, not merely non-empty. The block
    # is written whole, so a key the caller omitted would go out as "" and
    # ERASE the stored value on a correction — losing an apartment number or
    # a postal code from the address a client is billed at. `unit` and
    # `postal_code` may be sent empty; they may not be left unsaid.
    absent = [k for k in keys if k not in args]
    if absent:
        raise ToolArgumentError(
            f"Une adresse se fournit en BLOC : {', '.join(keys)}. "
            f"Clé(s) absente(s) : {', '.join(absent)}. Envoyez les six — "
            "« unit » et « postal_code » peuvent valoir une chaîne vide, mais "
            "les omettre les effacerait, et un bloc partiel ferait remplacer "
            "les champs omis par les défauts Montréal / Québec / Canada."
        )
    blank = [
        f"{prefix}_{k}" for k in _ADDRESS_REQUIRED
        if not (args.get(f"{prefix}_{k}") or "").strip()
    ]
    if blank:
        raise ToolArgumentError(
            f"Une adresse se fournit en BLOC : {', '.join(keys)}. "
            f"Vide(s) : {', '.join(blank)}. Un bloc partiel ferait remplacer "
            "les champs omis par les défauts Montréal / Québec / Canada — le "
            "contact changerait de ville en silence."
        )
    return {k: _clean_partie_text(args.get(k, ""), k) for k in keys}


def _bump_parties_ctag() -> bool:
    """Bump le CTag du carnet d'adresses ; rend son succès.

    Les parties sont exposées en CardDAV et ``models/partie.py`` ne bumpe
    JAMAIS — le bump vit dans la route (trois sites), donc le connecteur doit
    le refaire. Sans lui, le contact est en base, visible dans
    l'application, et DavX5 ne le voit jamais : rien n'échoue, seul le
    téléphone est faux.

    Avale sa propre panne, pour la raison de ``_bump_note_ctag`` : le contact
    est DÉJÀ écrit, et laisser l'exception filer jusqu'au ``except Exception``
    de ``endpoint._tools_call`` rapporterait une écriture commise comme un
    échec — le modèle réessaierait et créerait un doublon.
    """
    try:
        bump_ctag("parties")
        return True
    except Exception:
        from utils.logging_setup import log_unexpected

        log_unexpected("mcp partie write: ctag bump failed")
        return False


def _derive_court_metadata(court_file_number: str) -> dict:
    """The judicial metadata a Québec court file number carries.

    Extracted verbatim from ``complete_dossier`` so the three dossier write
    paths derive it identically: a dossier citing a number its own cards
    cannot explain is exactly what the web form's parse step exists to
    prevent.
    """
    parsed = reference.parse_court_file_number(court_file_number)
    greffe = parsed.get("greffe") or {}
    juridiction = parsed.get("juridiction") or {}
    forum = parsed.get("forum") or {}
    return {
        "greffe_number": parsed.get("greffe_number") or "",
        "juridiction_number": parsed.get("juridiction_number") or "",
        "tribunal": juridiction.get("tribunal") or forum.get("name") or "",
        "competence": juridiction.get("competence") or "",
        "palais_de_justice": greffe.get("palais_de_justice") or "",
        "district_judiciaire": greffe.get("district_judiciaire") or "",
        "is_administrative_tribunal": bool(parsed.get("is_administrative")),
    }


def _resolve_party_entries(raw: Any, label: str) -> list[dict]:
    """Canonical party entries, every id RESOLVED server-side.

    Two reasons this cannot be a pass-through. First
    ``dossier._rebuild_party_mirrors`` subscripts ``c["id"]`` RAW, so an entry
    without an id raises an uncaught KeyError inside the model — an HTTP 500,
    not a validation error, and ``_validate`` never checks for the key.
    Second, ``name`` and ``avocat_name`` are SNAPSHOTS the caller must not be
    able to falsify: they are what a generated procedure cites.

    Junk roles are REFUSED, where the web route drops them silently — a
    connector that quietly discards half a party's roles reports a success
    that is not one.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolArgumentError(f"`{label}` doit être une liste.")
    entries: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ToolArgumentError(f"`{label}[{index}]` doit être un objet.")
        pid = (item.get("partie_id") or "").strip()
        if not pid:
            raise ToolArgumentError(
                f"`{label}[{index}].partie_id` est requis."
            )
        if pid in seen:
            raise ToolArgumentError(
                f"La partie {pid} figure deux fois dans `{label}`."
            )
        seen.add(pid)
        partie = partie_model.get_partie(pid)
        if partie is None:
            raise ToolArgumentError(
                f"Contact introuvable : {pid} ({label}[{index}]). Créez-le "
                "avec create_partie, ou utilisez list_parties pour obtenir un "
                "partie_id valide."
            )
        roles = item.get("roles") or []
        if not isinstance(roles, list):
            raise ToolArgumentError(f"`{label}[{index}].roles` doit être une liste.")
        unknown = [r for r in roles if r not in dossier_model.PARTY_ROLES]
        if unknown:
            raise ToolArgumentError(
                f"Rôle de partie inconnu dans `{label}[{index}]` : "
                + ", ".join(str(r) for r in unknown)
            )
        avocat_id = (item.get("avocat_partie_id") or "").strip()
        avocat_name = ""
        if avocat_id:
            if avocat_id == pid:
                raise ToolArgumentError(
                    "Une partie ne peut pas être son propre avocat."
                )
            avocat = partie_model.get_partie(avocat_id)
            if avocat is None:
                raise ToolArgumentError(
                    f"Avocat introuvable : {avocat_id} ({label}[{index}])."
                )
            avocat_name = partie_model.display_name(avocat)
        entries.append({
            "id": pid,
            "name": partie_model.display_name(partie),
            "roles": list(roles),
            "avocat_id": avocat_id,
            "avocat_name": avocat_name,
        })
    return entries


def _dossier_field_updates(args: dict, *, allow_clear: bool = False) -> dict:
    """The shared classification/financial block, coerced, BY PRESENCE.

    *allow_clear* separates the two tools that share this block.
    ``complete_dossier`` FILLS what is empty, so an empty value is nothing to
    do and is skipped. ``update_dossier`` REPLACES what it names, so an empty
    string must actually clear a text field — and for a field it cannot
    safely clear (a date, an amount, whose empty form is None or a derived
    value) it REFUSES, rather than returning « updated: true » having quietly
    done nothing to it.
    """
    out: dict[str, Any] = {}
    for field, kind in _COMPLETABLE_FIELDS.items():
        if field not in args:
            continue
        if args[field] in (None, ""):
            if not allow_clear:
                continue
            if kind != "str":
                raise ToolArgumentError(
                    f"`{field}` ne peut pas être vidé par le connecteur : sa "
                    "forme vide est une valeur nulle ou dérivée, pas une "
                    "chaîne. Corrigez-le dans l'application, ou fournissez "
                    "une vraie valeur."
                )
            out[field] = ""
            continue
        out[field] = _coerce_completable(
            field, kind, args[field],
            allow_zero=field in _ZERO_MEANINGFUL,
            limit=_SOMMAIRE_MAX if field == "sommaire" else None,
        )
    for field in ("prescription_notes",):
        if field in args:
            out[field] = _clean_entity_text(str(args[field] or ""), field)
    return out


def _forum_warnings(before: dict, after: dict) -> list[str]:
    """What ``normalize_forum`` silently discarded, said out loud.

    It clears ``district_judiciaire`` for an administrative or federal forum
    and FORCES ``court_file_number`` to « Préjudiciaire » before any
    proceedings — both correct, both invisible. The same disclosure shape the
    recomputed prescription date already gets.
    """
    warnings: list[str] = []
    if before.get("district_judiciaire") and not after.get("district_judiciaire"):
        warnings.append(
            "Le district judiciaire fourni a été écarté : il ne s'applique "
            f"pas à un forum « {after.get('forum_type', '')} »."
        )
    if (
        before.get("court_file_number")
        and after.get("court_file_number") != before.get("court_file_number")
    ):
        warnings.append(
            "Le numéro de cour a été remplacé par "
            f"« {after.get('court_file_number', '')} » : un dossier "
            "préjudiciaire n'en porte pas d'autre tant que rien n'est déposé."
        )
    return warnings


def _dossier_write_result(
    doc: dict, *, verb: str, warnings: list[str]
) -> dict:
    """Success payload of a dossier write.

    NO ctag keys: dossiers are not a DAV collection, and faking a sync that
    does not exist is the failure the note tools' warning text exists to
    avoid. A dossier created « fermé » simply is never advertised — there was
    never a collection to drain, which is why create may set a status and
    update may not.
    """
    derived = dossier_model.derive_prescription(doc)
    payload: dict[str, Any] = {
        verb: True,
        "entity_type": "dossier",
        "entity": {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("id", ""),
            "file_number": doc.get("file_number", ""),
            "label": doc.get("title", ""),
            "status": doc.get("status", ""),
            "legacy_ref": doc.get("legacy_ref", ""),
            # The etag the model just wrote: the next update_dossier
            # presents it.
            "etag": concurrency.etag_of(doc),
        },
        "prescription_date": date_str(_as_utc(doc.get("prescription_date"))),
        "prescription_status": derived["status"],
        "warnings": list(warnings),
    }
    return payload


def _clean_hours(raw: Any) -> float:
    """Hours at TWO decimals — the quarter-hour a legacy practice billed in.

    ``round(hours, 1)`` was silent corruption: ``round(0.25, 1) == 0.2``, so a
    0.25 h line at 300 $/h stored 60,00 $ where the paper invoice printed
    75,00 $ — and the invoice then failed its own reconciliation by a gap the
    caller could not close. Anything finer than two decimals is REFUSED, at
    the entry where the data is, rather than rounded and discovered three
    calls later at the invoice. (The subset validator has no ``multipleOf``,
    so this must be a handler check.)
    """
    try:
        hours = float(raw or 0)
    except (TypeError, ValueError):
        raise ToolArgumentError("`hours` doit être un nombre.")
    if hours <= 0:
        raise ToolArgumentError("`hours` doit être positif.")
    rounded = round(hours, 2)
    if abs(rounded - hours) > 1e-9:
        raise ToolArgumentError(
            f"`hours` : {hours} porte plus de deux décimales. Arrondir ici "
            "changerait le montant facturé — donnez la valeur au centième "
            "(0,25 pour un quart d'heure)."
        )
    return rounded


def _optional_phase_pair(args: dict) -> Optional[tuple[str, str]]:
    """The phase pair ONLY when the caller named one; ``None`` otherwise.

    ``_resolve_phase_pair({})`` returns ``("", "")``, and the models write the
    whole document — so writing that pair on an edit that never mentioned a
    phase would ERASE a stored classification. When either key IS present,
    BOTH are written: the models only call ``apply_sous_phase_default``, which
    imputes but never REPAIRS (``models.task`` has a repair path, time entries
    and expenses do not), so a phase-only retag against a foreign stored
    sub-code would be rejected by ``_validate``.
    """
    if "phase" not in args and "sous_phase" not in args:
        return None
    return _resolve_phase_pair(args)


def _stale_message(
    tool: str, subject: str, current: Optional[dict], *, bulk: bool = False,
    readers: Optional[tuple[str, ...]] = None,
) -> str:
    """The French refusal of an outdated edit — naming WHEN, never WHAT.

    The time of the last write is named (it tells the caller its read is
    old, and by how much); the path of that write is NOT: a maintenance
    script regenerates an etag without stamping ``updated_via``, so naming
    the via could name the wrong writer. Content is never quoted. The
    readers named are the tool's own ``"etag_readers"`` declaration — the
    same ones its ``expected_etag`` description names.

    The remedy names « l'enregistrement », never a pronoun: the subjects
    differ in gender (« Ce contact », « Cette entrée de temps »), and a
    « Relisez-le » after a feminine subject is a French error on a text
    the caller reads. *bulk* is the per-row reason of a batch
    reclassification: a batch item accepts no ``expected_etag`` (its schema
    refuses one), so telling that caller to « redo it with the current
    etag » would send it straight into a schema refusal — the remedy there
    is to re-read the row and send it again.

    A tool that accepts NO ``expected_etag`` at all (``append_to_note``,
    ``complete_task``, the three dossier recorders) still compare-and-sets
    against its own read (plan rule 3); its remedy is « re-read, then send
    the call again » — never « with its current etag », an argument its
    schema refuses — and it names its *readers* explicitly, since it
    declares no ``"etag_readers"``.
    """
    when = iso_mtl(_as_utc((current or {}).get("updated_at")))
    last = f" (dernière écriture : {when})" if when else ""
    named = readers if readers is not None else (
        TOOLS[tool].get("etag_readers") or ())
    reread = f" ({' ou '.join(named)})" if named else ""
    accepts_etag = TOOLS[tool].get("concurrency") in (
        CONCURRENCY_OPTIONAL, CONCURRENCY_REQUIRED)
    if not bulk and not accepts_etag:
        return (
            f"{subject} depuis la lecture qui fondait cet appel{last}. "
            f"Rien n'a été écrit. Relisez l'enregistrement{reread}, puis "
            "renvoyez l'appel : il partira de la version actuelle."
        )
    if bulk:
        return (
            f"{subject} pendant le traitement du lot{last}. Cette ligne n'a "
            f"pas été écrite : relisez l'enregistrement{reread}, puis "
            "renvoyez la ligne dans un nouvel appel."
        )
    return (
        f"{subject} depuis votre lecture{last}. Rien n'a été écrit. "
        f"Relisez l'enregistrement{reread} et refaites la modification "
        "avec son etag actuel."
    )


def _stale_refusal(
    tool: str, subject: str, current: Optional[dict],
    readers: Optional[tuple[str, ...]] = None,
) -> ToolArgumentError:
    # A LITERAL reason code, as every refusal's (test_mcp_jsonrpc sweeps it
    # and requires it documented under `mcp_write_refused`).
    return ToolArgumentError(
        _stale_message(tool, subject, current, readers=readers),
        reason="stale_etag",
    )


def _expected_etag(
    args: dict, existing: dict, *, tool: str, subject: str
) -> str:
    """The etag the model must commit against (plan rule 3).

    * The caller named one: it must still be the stored one, or the call is
      refused HERE — before any domain guard or payload work, because the
      useful answer to an outdated view is « re-read », not the first field
      error the outdated view happens to trip.
    * The caller named none: the handler compare-and-sets against the etag
      it has JUST read. Its guards and its payload were computed from that
      read — the append-only party arrays, the invoiced wall, the phase
      already carried — so a write landing between it and the commit must
      be refused, never silently overwritten.

    ``''`` is a real value: the etag of a legacy record that never had one.
    """
    current = concurrency.etag_of(existing)
    if "expected_etag" in args:
        wanted = str(args.get("expected_etag") or "")
        if wanted != current:
            raise _stale_refusal(tool, subject, existing)
        return wanted
    return current


def _raise_if_stale(
    errors: list[str], *, tool: str, subject: str,
    reread: Callable[[], Optional[dict]],
    readers: Optional[tuple[str, ...]] = None,
) -> None:
    """Turn the model's concurrency refusal into the named French refusal.

    The model refuses when a write landed between the handler's read and
    the commit; *reread* fetches the record again, best-effort, only to name
    the time of that write.
    """
    if not concurrency.is_stale(errors):
        return
    try:
        current = reread()
    except Exception:
        current = None
    raise _stale_refusal(tool, subject, current, readers)


def _refuse_if_invoiced(row: dict, kind: str) -> None:
    """Refuse an edit on an invoiced row, before the model is reached.

    The model refuses too; this pre-read exists so the refusal NAMES the
    invoice and the way out instead of surfacing a bare model error. (It
    was written for the `dry_run` contract, removed 2026-08-27, and is
    kept for the message.) Note the remedy named is real — voiding
    the invoice in the application releases every source still attached to
    it (``void_invoice`` sets invoiced back to False), which is the ONE way
    back. Since 2026-09-26 that void is REFUSED while a payment stands on
    the invoice (reverse it first, in « Administration » or « Fidéicommis »);
    the message below does not say so yet — a connector-visible text, it
    moves with the Lot 3 text commit and its consent train.
    """
    if not row.get("invoiced"):
        return
    raise ToolArgumentError(
        f"{kind} est déjà porté(e) à la facture "
        f"{row.get('invoice_id') or '(inconnue)'}. Le connecteur ne corrige "
        "jamais une entrée facturée — seule sa phase du litige reste "
        "reclassable (set_time_entry_phase, set_expense_phase) — et ne peut "
        "pas annuler une facture. "
        "Pour la libérer : annulez la facture dans l'application — ses "
        "entrées et déboursés redeviennent modifiables."
    )


def _apply_legacy_ref(args: dict, data: dict, collection: str) -> None:
    """Carry the import anchor onto a create, refusing a collision.

    One keyed query per create — the point of the anchor is that a resumed
    import finds what it already wrote instead of writing it twice, and
    detection without refusal would only report the damage after the fact.
    """
    if "legacy_ref" not in args:
        return
    ref = _clean_entity_text(str(args["legacy_ref"] or ""), "legacy_ref")
    if not ref:
        return
    _refuse_legacy_ref_collision(collection, ref)
    data["legacy_ref"] = ref


def _refuse_legacy_ref_collision(collection: str, legacy_ref: str) -> None:
    """Un legacy_ref déjà pris est un doublon en préparation.

    Requête à clé, donc bon marché — contrairement à un rapprochement de noms
    qui balaierait toute la collection à CHAQUE création, soit O(N²) sur
    l'opération même qui crée N contacts. C'est aussi la seule identité
    EXACTE dont dispose une reprise ; ``utils/rapprochement`` propose et ne
    tranche jamais, par doctrine.
    """
    if not legacy_ref:
        return
    from models import find_by_legacy_ref

    existing = find_by_legacy_ref(collection, legacy_ref, limit=1)
    if existing:
        raise ToolArgumentError(
            f"La référence d'origine « {legacy_ref} » est déjà portée par "
            f"l'enregistrement {existing[0].get('id', '?')}. Utilisez "
            "find_imported pour le retrouver, ou corrigez-le plutôt que d'en "
            "créer un second — rien ici ne peut supprimer un doublon."
        )


# ── 36. get_import_audit (read) ─────────────────────────────────────────

_AUDIT_INVOICE_CAP = 50


def _audit_dossier(args: dict) -> Optional[dict]:
    dossier_id = args.get("dossier_id")
    file_number = args.get("file_number")
    if bool(dossier_id) == bool(file_number):
        raise ToolArgumentError(
            "Provide exactly one of `dossier_id` or `file_number`"
        )
    if file_number:
        return dossier_model.get_dossier_by_file_number(file_number)
    return dossier_model.get_dossier(dossier_id)


def _audit_totals(rows: list[dict]) -> dict:
    invoiced = [r for r in rows if r.get("invoiced")]
    unbilled = [r for r in rows if not r.get("invoiced")]
    block = {
        "count": len(rows),
        "invoiced_count": len(invoiced),
        "uninvoiced_count": len(unbilled),
        "created_via_mcp_count": sum(
            1 for r in rows if r.get("created_via") == "mcp"
        ),
        "unphased_count": sum(1 for r in rows if not r.get("phase")),
    }
    _money(block, "amount", sum(int(r.get("amount") or 0) for r in rows))
    _money(
        block, "uninvoiced_amount",
        sum(int(r.get("amount") or 0) for r in unbilled),
    )
    return block


def get_import_audit(args: dict) -> dict:
    d = _audit_dossier(args)
    if d is None:
        return {
            "found": False,
            "dossier_id": args.get("dossier_id"),
            "file_number": args.get("file_number"),
        }

    did = d.get("id", "")
    entries, entries_cursor = time_entry_model.list_time_entries_page(
        dossier_id=did, limit=_FETCH_CAP
    )
    expenses, expenses_cursor = expense_model.list_expenses_page(
        dossier_id=did, limit=_FETCH_CAP
    )
    invoices = invoice_model.list_invoices(dossier_id=did)
    invoices_truncated = len(invoices) > _AUDIT_INVOICE_CAP
    invoices = invoices[:_AUDIT_INVOICE_CAP]

    invoice_blocks = []
    for inv in invoices:
        items = invoice_model.list_line_items(inv.get("id", ""))
        invoice_blocks.append({"invoice": inv, "line_items": items})

    # A truncated source window would make an invoice's own line items look
    # orphaned. Suppress the two checks that compare against it rather than
    # manufacture a « source introuvable » out of a paging boundary — the
    # coverage report's rule: a shortened report must never pass for a clean
    # one, and it must never accuse an import that is actually fine.
    # Two ways the source population can be untrustworthy, and BOTH must
    # suppress the checks that compare an invoice's line items against it.
    # A truncated page is the obvious one. The second is a swallowed read:
    # list_*_page fails OPEN to ([], None), so a Firestore blip is
    # indistinguishable from « this dossier has no work » — and then every
    # line item on every invoice looks orphaned, producing IMP-03/IMP-06
    # manquements against an import that is perfectly fine. When the
    # invoices themselves cite sources and the source population came back
    # empty, the state is ambiguous, so we suppress and SAY SO rather than
    # accuse.
    cites_sources = any(
        (item.get("source_id") or "")
        for block in invoice_blocks
        for item in block["line_items"]
    )
    sources_empty_but_cited = cites_sources and not entries and not expenses
    sources_complete = (
        entries_cursor is None
        and expenses_cursor is None
        and not sources_empty_but_cited
    )
    skip = frozenset() if sources_complete else frozenset(
        import_audit.NEEDS_COMPLETE_SOURCES
    )

    ctx = {
        "dossier": d,
        "time_entries": entries,
        "expenses": expenses,
        "invoices": invoice_blocks,
    }
    findings = import_audit.run_checks(ctx, skip=skip)

    rows_invoices = []
    for block in invoice_blocks:
        inv = block["invoice"]
        items = block["line_items"]
        row = {
            "id": inv.get("id", ""),
            "invoice_number": inv.get("invoice_number", ""),
            "date": date_str(_as_utc(inv.get("date"))),
            "status": inv.get("status", ""),
            "legacy_ref": inv.get("legacy_ref", ""),
            "line_count": len(items),
        }
        _money(row, "total", inv.get("total", 0))
        _money(
            row, "line_items_total",
            sum(int(i.get("amount") or 0) for i in items),
        )
        row["subtotal_matches_line_items"] = (
            None if not items
            else row["line_items_total_cents"] == int(inv.get("subtotal") or 0)
        )
        rows_invoices.append(row)

    return {
        "found": True,
        "dossier": _dossier_row(d),
        "completeness": {
            "has_client": bool(d.get("client_ids")),
            "closed_without_closed_date": bool(
                d.get("status") in import_audit.CLOSED_STATUSES
                and not d.get("closed_date")
            ),
            "hourly_rate_is_default": (
                int(d.get("hourly_rate") or 0)
                == int(dossier_model.field_defaults().get("hourly_rate") or 0)
            ),
            "legacy_ref": d.get("legacy_ref", ""),
        },
        "time": _audit_totals(entries),
        "expenses": _audit_totals(expenses),
        "invoices": rows_invoices,
        "findings": findings,
        "checks_skipped": sorted(skip),
        "truncated": bool(not sources_complete or invoices_truncated),
    }


# ── 37. create_partie (WRITE) ───────────────────────────────────────────

# Les champs d'identité que les deux outils de contact adressent. NI la
# conformité (identity_verified*, conflict_check*, kyc_document_ids), NI les
# mandataires, NI birth_date : une machine n'atteste pas qu'une identité a
# été vérifiée, et un `mandataires: []` partiel effacerait la liste.
_PARTIE_TEXT_FIELDS = (
    "prefix", "first_name", "last_name", "organization_name", "trade_name",
    "governing_law", "language", "gender", "pronouns", "job_title",
    "job_role", "organization", "email", "email_work", "phone_home",
    "phone_cell", "phone_work", "fax", "bar_number", "company_neq", "notes",
    "legacy_ref",
)


def _partie_payload(args: dict) -> dict:
    """Liste blanche PAR PRÉSENCE des champs de contact fournis."""
    data = {
        key: _clean_partie_text(value, key)
        for key, value in _supplied(args, _PARTIE_TEXT_FIELDS).items()
    }
    if "contact_role" in args:
        data["contact_role"] = args["contact_role"]
    data.update(_require_address_bloc(args, "address"))
    data.update(_require_address_bloc(args, "work_address"))
    return data


def _partie_entity(doc: dict) -> dict:
    return {
        "id": doc.get("id", ""),
        "dossier_id": "",
        "label": partie_model.display_name(doc),
        "type": doc.get("type", ""),
        "contact_role": doc.get("contact_role", ""),
        "legacy_ref": doc.get("legacy_ref", ""),
        # The etag the model just WROTE: the next update_partie presents it.
        "etag": concurrency.etag_of(doc),
    }


def _partie_write_result(doc: dict, *, verb: str) -> dict:
    payload: dict[str, Any] = {
        verb: True,
        "entity_type": "partie",
        "entity": _partie_entity(doc),
        "warnings": [],
    }
    bumped = _bump_parties_ctag()
    payload["ctag_bumped"] = bumped
    payload["dav_synced"] = bumped
    if not bumped:
        payload["warnings"].append(
            "Le contact est enregistré, mais la synchronisation CardDAV n'a "
            "pas pu être déclenchée. Il apparaîtra sur l'appareil au prochain "
            "changement du carnet d'adresses. Ne pas réessayer."
        )
    return payload


def create_partie(args: dict) -> dict:
    return run_write(
        "create_partie", args, lambda: _create_partie_impl(args)
    )


def _create_partie_impl(args: dict) -> dict:
    partie_type = args.get("type") or ""
    data = _partie_payload(args)
    data["type"] = partie_type

    # XOR miroir : le validateur du connecteur ne connaît pas oneOf, donc le
    # contrôle vit ici. Il refuse en français, en nommant le champ, avant
    # que le modèle ne soit atteint.
    if partie_type == "individual" and not data.get("last_name", "").strip():
        raise ToolArgumentError(
            "Un contact « individual » exige `last_name`."
        )
    if partie_type == "organization" and not data.get(
        "organization_name", ""
    ).strip():
        raise ToolArgumentError(
            "Un contact « organization » exige `organization_name`."
        )
    if partie_type == "individual" and data.get("organization_name", "").strip():
        raise ToolArgumentError(
            "Les champs de personne physique et de personne morale ne se "
            "mélangent pas : choisissez le `type` qui correspond."
        )
    if partie_type == "organization" and (
        data.get("first_name", "").strip() or data.get("last_name", "").strip()
    ):
        raise ToolArgumentError(
            "Les champs de personne physique et de personne morale ne se "
            "mélangent pas : choisissez le `type` qui correspond."
        )

    _refuse_legacy_ref_collision("parties", data.get("legacy_ref", ""))

    partie, errors = partie_model.create_partie(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _partie_write_result(partie, verb="created")


# ── 38. update_partie (WRITE — remplace la valeur nommée) ───────────────


def update_partie(args: dict) -> dict:
    return run_write(
        "update_partie", args, lambda: _update_partie_impl(args)
    )


def _update_partie_impl(args: dict) -> dict:
    partie_id = (args.get("partie_id") or "").strip()
    if not partie_id:
        raise ToolArgumentError("`partie_id` est requis.")
    # Pré-lecture dans le gestionnaire pour que la branche sèche refuse un id
    # inconnu à l'identique de l'appel réel.
    existing = partie_model.get_partie(partie_id)
    if existing is None:
        raise ToolArgumentError(
            f"Contact introuvable : {partie_id}. Utilisez list_parties ou "
            "find_imported pour obtenir un partie_id valide."
        )
    expected = _expected_etag(
        args, existing, tool="update_partie",
        subject="Ce contact a été modifié",
    )

    data = _partie_payload(args)
    # `id` n'est PAS adressable au schéma, et ne doit jamais l'être : le
    # modèle fusionne sans le re-fixer, donc un id fourni corromprait le CHAMP
    # id sans changer le chemin du document — ce qui casse en silence la
    # pagination par curseur et les scans de mandataires.
    data.pop("id", None)
    if not data:
        raise ToolArgumentError(
            "Aucun champ à corriger : fournissez au moins un champ."
        )

    if "legacy_ref" in data and data["legacy_ref"] != existing.get("legacy_ref", ""):
        _refuse_legacy_ref_collision("parties", data["legacy_ref"])

    partie, errors = partie_model.update_partie(
        partie_id, data, expected_etag=expected
    )
    _raise_if_stale(
        errors, tool="update_partie", subject="Ce contact a été modifié",
        reread=lambda: partie_model.get_partie(partie_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _partie_write_result(partie, verb="updated")


# ── 41-42. update_time_entry / update_expense (WRITE — remplacent) ──────


def _billing_edit(
    args: dict,
    *,
    id_key: str,
    kind: str,
    entity_type: str,
    getter,
    updater,
    text_fields: tuple,
    legacy_collection: str,
    entity_builder,
    tool: str,
    stale_subject: str,
) -> dict:
    """Shared body of the two billing editors.

    Presence-only whitelist throughout — the most dangerous line of the
    family would be ``args.get("billable", True)``: it would silently re-bill
    an entry deliberately marked non-billable AND rematerialise its amount,
    the model recomputing on every save. Same for a defaulted ``taxable``,
    which would add QST to a non-taxable disbursement.
    """
    row_id = (args.get(id_key) or "").strip()
    if not row_id:
        raise ToolArgumentError(f"`{id_key}` est requis.")
    existing = getter(row_id)
    if existing is None:
        raise ToolArgumentError(f"{kind} introuvable : {row_id}.")
    expected = _expected_etag(args, existing, tool=tool, subject=stale_subject)
    _refuse_if_invoiced(existing, kind)

    data: dict[str, Any] = {}
    for field in text_fields:
        if field in args:
            data[field] = _clean_entity_text(str(args[field] or ""), field)
    if "date" in args:
        when = _write_date(args, "date", required=True)
        if when is not None:
            data["date"] = when
    if "hours" in args:
        data["hours"] = _clean_hours(args["hours"])
    if "rate_cents" in args:
        rate = int(args["rate_cents"])
        if rate < 0:
            raise ToolArgumentError("`rate_cents` ne peut pas être négatif.")
        data["rate"] = rate
    if "amount_cents" in args:
        amount = int(args["amount_cents"])
        if amount <= 0:
            raise ToolArgumentError(
                "`amount_cents` doit être un montant positif en cents."
            )
        data["amount"] = amount
    for flag in ("billable", "taxable"):
        if flag in args:
            data[flag] = bool(args[flag])
    if "category" in args:
        data["category"] = args["category"]

    pair = _optional_phase_pair(args)
    if pair is not None:
        data["phase"], data["sous_phase"] = pair

    if "legacy_ref" in data and data["legacy_ref"] != existing.get("legacy_ref", ""):
        _refuse_legacy_ref_collision(legacy_collection, data["legacy_ref"])

    if not data:
        raise ToolArgumentError(
            "Aucun champ à modifier : fournissez au moins un champ."
        )


    row, errors = updater(row_id, data, expected_etag=expected)
    _raise_if_stale(
        errors, tool=tool, subject=stale_subject,
        reread=lambda: getter(row_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return {
        "updated": True,
        "entity_type": entity_type,
        "entity": entity_builder(row),
        "warnings": [],
    }


_TIME_ENTRY_TEXT = ("description", "legacy_ref")
_EXPENSE_TEXT = ("description", "legacy_ref")


def _time_entry_entity(doc: dict) -> dict:
    row = {
        "id": doc.get("id", ""),
        "dossier_id": doc.get("dossier_id", ""),
        "label": doc.get("description", ""),
        "date": date_str(_as_utc(doc.get("date"))),
        "hours": float(doc.get("hours") or 0),
        "billable": bool(doc.get("billable")),
        "invoiced": bool(doc.get("invoiced")),
        **_phase_pair(doc),
        # The etag of the row AS STORED after the write — never the one the
        # handler read before it (the next guarded edit presents this one).
        "etag": concurrency.etag_of(doc),
    }
    _money(row, "rate", doc.get("rate", 0))
    _money(row, "amount", doc.get("amount", 0))
    return row


def _expense_entity(doc: dict) -> dict:
    row = {
        "id": doc.get("id", ""),
        "dossier_id": doc.get("dossier_id", ""),
        "label": doc.get("description", ""),
        "date": date_str(_as_utc(doc.get("date"))),
        "category": doc.get("category", ""),
        "taxable": bool(doc.get("taxable")),
        "invoiced": bool(doc.get("invoiced")),
        **_phase_pair(doc),
        "etag": concurrency.etag_of(doc),
    }
    _money(row, "amount", doc.get("amount", 0))
    return row


def update_time_entry(args: dict) -> dict:
    return run_write(
        "update_time_entry", args,
        lambda: _billing_edit(
            args,
            id_key="time_entry_id", kind="Cette entrée de temps",
            entity_type="time_entry",
            getter=time_entry_model.get_time_entry,
            updater=time_entry_model.update_time_entry,
            text_fields=_TIME_ENTRY_TEXT,
            legacy_collection="timeentries",
            entity_builder=_time_entry_entity,
            tool="update_time_entry",
            stale_subject="Cette entrée de temps a été modifiée",
        ),
    )


def update_expense(args: dict) -> dict:
    return run_write(
        "update_expense", args,
        lambda: _billing_edit(
            args,
            id_key="expense_id", kind="Ce déboursé",
            entity_type="expense",
            getter=expense_model.get_expense,
            updater=expense_model.update_expense,
            text_fields=_EXPENSE_TEXT,
            legacy_collection="expenses",
            entity_builder=_expense_entity,
            tool="update_expense",
            stale_subject="Ce déboursé a été modifié",
        ),
    )


# ── 44-47. set_*_phase (+_bulk) (WRITE — reclassement de phase) ─────────
#
# The ONLY writes in the connector that pass the `invoiced` wall, and the
# reason they may is that the wall protects the invoice's MONEY figures
# while `phase`/`sous_phase` appear on NO invoice: line items are
# independent copies with no phase field, no gabarit placeholder resolves
# one, no DAV serializer emits one. They feed `budget.aggregate_actuals`,
# which counts billed work — which is precisely why a billed entry left
# unphased skews the dossier's budget-vs-actuals view.
#
# `update_time_entry` / `update_expense` keep their refusal INTACT. That is
# not timidity: their refusal is what makes their own output schemas
# (« invoiced: always false ») true, and it is what keeps a money field one
# guard away from a caller that only meant to retag.


_PHASE_NO_CODE = (
    "Aucun code de phase fourni : indiquez `phase`, `sous_phase`, ou les "
    "deux."
)


def _phase_request_rows(items: list, id_key: str) -> list[dict]:
    """One normalized request row per item, IN ORDER.

    ``reason`` set means the item is refused before anything is read — a
    blank id, no code at all, or a pair whose sub-code belongs to another
    phase. These are per-ITEM refusals on purpose: they name exactly what
    to fix and let the other rows through. The one whole-call refusal is
    the duplicate id below, because a row named twice with two codes has no
    single intended outcome to report.
    """
    rows: list[dict] = []
    for item in items:
        row_id = str((item or {}).get(id_key) or "").strip()
        row: dict[str, Any] = {
            "id": row_id, "phase": "", "sous_phase": "", "reason": None,
        }
        if not row_id:
            row["reason"] = f"`{id_key}` est requis."
            rows.append(row)
            continue
        if not (item.get("phase") or item.get("sous_phase")):
            row["reason"] = _PHASE_NO_CODE
            rows.append(row)
            continue
        try:
            # Same resolution as every other phased write: sous_phase alone
            # derives its parent, phase alone imputes to « -00 », and a
            # contradictory pair is refused BEFORE anything is written.
            row["phase"], row["sous_phase"] = _resolve_phase_pair(item)
        except ToolArgumentError as exc:
            row["reason"] = str(exc)
            rows.append(row)
            continue
        # The model validates too — but `run_write` short-circuits a dry run
        # WITHOUT calling it, so a code the schema enum happens not to cover
        # (a direct call, a widened enum) would be previewed as « applied »
        # and refused for real. Repeating the model's own check here is what
        # keeps the two answers identical.
        invalid = phases.validate_pair(row)
        if invalid:
            row["reason"] = "; ".join(invalid)
        elif not row["phase"]:
            row["reason"] = "Une phase du litige est requise."
        rows.append(row)
    return rows


def _refuse_duplicate_ids(rows: list[dict], id_key: str) -> None:
    seen: set[str] = set()
    for row in rows:
        row_id = row["id"]
        if not row_id:
            continue
        if row_id in seen:
            raise ToolArgumentError(
                f"`{id_key}` « {row_id} » figure deux fois dans le même "
                "appel. Un lot ne peut pas porter deux codes pour la même "
                "ligne : corrigez la liste et renvoyez-la."
            )
        seen.add(row_id)


def _phase_result_row(row: dict, doc: Optional[dict], outcome: str) -> dict:
    """One reported outcome. Codes echo the REQUEST when nothing was read."""
    pair = (
        _phase_pair(doc) if doc is not None
        else _phase_pair({"phase": row["phase"], "sous_phase": row["sous_phase"]})
    )
    return {
        "id": row["id"],
        "outcome": outcome,
        "reason": row["reason"],
        # null, never a default: asserting « not invoiced » about a row we
        # could not read would be inventing a fact about it.
        "dossier_id": doc.get("dossier_id", "") if doc is not None else None,
        "invoiced": bool(doc.get("invoiced")) if doc is not None else None,
        **pair,
    }


def _set_phase_impl(
    args: dict,
    *,
    id_key: str,
    entity_type: str,
    bulk_getter,
    setter,
    entity_builder,
    bulk: bool,
    tool: str,
    stale_subject: str,
) -> dict:
    """Shared body of the four reclassification tools.

    Every guard runs in the handler, ahead of the model, so a refusal names
    the offending row rather than surfacing a bare model error. The bulk
    form reports line by line in the ORDER ASKED, which is what makes a
    reclassification pass auditable against the request that produced it.

    Concurrency (plan rule 3). Every CHANGED row is compare-and-set: against
    the caller's ``expected_etag`` on the single tool when given, otherwise
    — and always on the bulk tools, which take no per-row token — against
    the etag of the row this handler has just read. A row already carrying
    the requested code is answered « unchanged » before any comparison:
    nothing can be lost by not writing, and refusing the no-op would break
    the replayability these tools promise. *tool* is the SINGLE tool's name
    (its ``etag_readers`` name the re-read in a refusal), for both forms.
    """
    if bulk:
        items = args.get("entries") or []
        if not items:
            raise ToolArgumentError(
                "`entries` doit contenir au moins une ligne."
            )
        if len(items) > PHASE_BULK_MAX:
            raise ToolArgumentError(
                f"`entries` est plafonné à {PHASE_BULK_MAX} lignes par appel "
                f"({len(items)} reçues). Découpez le lot."
            )
    else:
        items = [args]

    rows = _phase_request_rows(items, id_key)
    _refuse_duplicate_ids(rows, id_key)

    readable = [r["id"] for r in rows if r["id"] and r["reason"] is None]
    # Fails CLOSED by design (models.*.get_*_bulk propagates): degraded to
    # {} it would manufacture « introuvable » for every single row.
    fetched = bulk_getter(readable) if readable else {}

    results: list[dict] = []
    written_rows: dict[str, dict] = {}
    applied = unchanged = refused = 0
    for row in rows:
        if row["reason"] is not None:
            refused += 1
            results.append(_phase_result_row(row, None, "refused"))
            continue
        doc = fetched.get(row["id"])
        if doc is None:
            # The getters swallow a read error into a missing key, so the
            # model cannot tell « absent » from « unreadable » — and saying
            # only one of the two would be a guess.
            row["reason"] = f"Ligne introuvable ou illisible : {row['id']}."
            refused += 1
            results.append(_phase_result_row(row, None, "refused"))
            continue
        already = (doc.get("phase", ""), doc.get("sous_phase", "")) == (
            row["phase"], row["sous_phase"]
        )
        if already:
            unchanged += 1
            results.append(_phase_result_row(row, doc, "unchanged"))
            continue
        # Raises the named refusal when the single tool's caller presented
        # an etag the row no longer carries; the bulk form has none to
        # present, so it compares against its own read.
        expected = (
            _expected_etag(args, doc, tool=tool, subject=stale_subject)
            if not bulk else concurrency.etag_of(doc)
        )
        written, errors, changed = setter(
            row["id"], row["phase"], row["sous_phase"],
            expected_etag=expected,
        )
        if concurrency.is_stale(errors):
            # A write landed on this row between the read above and the
            # commit. The single tool raises the named refusal; the bulk
            # form reports it on the row and lets the others through. Only
            # WHEN is named, never the content.
            if not bulk:
                _raise_if_stale(
                    errors, tool=tool, subject=stale_subject,
                    reread=lambda: bulk_getter([row["id"]]).get(row["id"]),
                )
            row["reason"] = _stale_message(
                tool, stale_subject, None, bulk=bulk)
            refused += 1
            results.append(_phase_result_row(row, doc, "refused"))
            continue
        if errors:
            row["reason"] = "; ".join(errors)
            refused += 1
            results.append(_phase_result_row(row, doc, "refused"))
            continue
        if changed:
            applied += 1
        else:
            unchanged += 1
        written_rows[row["id"]] = written
        results.append(
            _phase_result_row(row, written, "applied" if changed else "unchanged")
        )

    warnings: list[str] = []
    if refused:
        warnings.append(
            f"{refused} ligne(s) refusée(s) — voir `reason`. Corrigez-les et "
            "renvoyez-les avec une NOUVELLE idempotency_key : rejouer la "
            "même clé rendrait ce rapport tel quel."
        )

    if not bulk:
        # The single tools raise on a refusal, like every other corrector —
        # a caller that named one row wants an error, not a report of one.
        if results[0]["outcome"] == "refused":
            raise ToolArgumentError(results[0]["reason"])
        # The entity is built from the row AS WRITTEN (the model hands back
        # the stored document, new etag included), never from the pre-write
        # read: its etag is what the next guarded edit must present, and the
        # pre-write one would be refused as stale on the very next call.
        entity_source = (
            written_rows.get(rows[0]["id"])
            or fetched.get(rows[0]["id"]) or {}
        )
        return {
            "updated": True,
            "entity_type": entity_type,
            "outcome": results[0]["outcome"],
            "entity": entity_builder(entity_source),
            "warnings": warnings,
        }

    from utils.logging_setup import log_mcp_event

    # `mcp_write` fires from the endpoint with entity_id: None — honest, a
    # batch has no single entity — so the audit trail gets its counts here.
    # COUNTS ONLY: no id list, no description, no amount.
    log_mcp_event(
        "mcp_phase_bulk", "success",
        entity_type=entity_type, requested=len(rows), applied=applied,
        unchanged=unchanged, refused=refused,
    )
    return {
        "updated": True,
        "entity_type": entity_type,
        "requested": len(rows),
        "applied": applied,
        "unchanged": unchanged,
        "refused": refused,
        "results": results,
        "warnings": warnings,
    }


_TIME_PHASE_KW = {
    "id_key": "time_entry_id",
    "entity_type": "time_entry",
    "bulk_getter": lambda ids: time_entry_model.get_time_entries_bulk(ids),
    "setter": lambda i, p, s, **kw: time_entry_model.set_time_entry_phase(
        i, p, s, **kw),
    "entity_builder": _time_entry_entity,
    "tool": "set_time_entry_phase",
    "stale_subject": "Cette entrée de temps a été modifiée",
}
_EXPENSE_PHASE_KW = {
    "id_key": "expense_id",
    "entity_type": "expense",
    "bulk_getter": lambda ids: expense_model.get_expenses_bulk(ids),
    "setter": lambda i, p, s, **kw: expense_model.set_expense_phase(
        i, p, s, **kw),
    "entity_builder": _expense_entity,
    "tool": "set_expense_phase",
    "stale_subject": "Ce déboursé a été modifié",
}


def set_time_entry_phase(args: dict) -> dict:
    return run_write(
        "set_time_entry_phase", args,
        lambda: _set_phase_impl(args, bulk=False, **_TIME_PHASE_KW),
    )


def set_expense_phase(args: dict) -> dict:
    return run_write(
        "set_expense_phase", args,
        lambda: _set_phase_impl(args, bulk=False, **_EXPENSE_PHASE_KW),
    )


def set_time_entry_phase_bulk(args: dict) -> dict:
    return run_write(
        "set_time_entry_phase_bulk", args,
        lambda: _set_phase_impl(args, bulk=True, **_TIME_PHASE_KW),
    )


def set_expense_phase_bulk(args: dict) -> dict:
    return run_write(
        "set_expense_phase_bulk", args,
        lambda: _set_phase_impl(args, bulk=True, **_EXPENSE_PHASE_KW),
    )


# ── 43. import_invoice (WRITE) ──────────────────────────────────────────


def import_invoice(args: dict) -> dict:
    return run_write(
        "import_invoice", args, lambda: _import_invoice_impl(args)
    )


def _import_invoice_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    entry_ids = list(args.get("time_entry_ids") or [])
    expense_ids = list(args.get("expense_ids") or [])
    if not entry_ids and not expense_ids:
        raise ToolArgumentError(
            "Fournissez au moins une entrée de temps (`time_entry_ids`) ou un "
            "déboursé (`expense_ids`) : une facture importée doit tracer à "
            "ses sources réelles."
        )

    invoice_number = (args.get("invoice_number") or "").strip()
    if not invoice_number:
        raise ToolArgumentError("`invoice_number` est requis.")
    when = _write_date(args, "date", required=True)
    due = _write_date(args, "due_date", required=False)

    expected_total = args.get("expected_total_cents")
    if not isinstance(expected_total, int) or isinstance(expected_total, bool):
        raise ToolArgumentError(
            "`expected_total_cents` est requis : c'est le grand total imprimé "
            "sur la facture papier, en cents."
        )

    # The client comes from the dossier, exactly as the web form does. A
    # dossier whose first client no longer resolves would otherwise produce a
    # blank address on an invoice the client already holds.
    clients = dossier.get("clients") or []
    client_id = clients[0].get("id", "") if clients else ""
    client_name = clients[0].get("name", "") if clients else ""
    billing_address = None
    if client_id:
        client_partie = partie_model.get_partie(client_id)
        if client_partie is None:
            raise ToolArgumentError(
                f"Le client du dossier ({client_id}) est introuvable : "
                "l'adresse de facturation serait vide sur une facture que le "
                "client détient déjà. Corrigez le dossier d'abord."
            )
        billing_address = invoice_model.billing_address_from(client_partie)

    # Snapshotted at creation, exactly as the web path does; only the
    # source moved to the settings/cabinet singleton.
    _cab = cabinet_dict()

    data: dict[str, Any] = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "client_id": client_id,
        "client_name": client_name,
        "date": when,
        "gst_number": _cab.get("gst_number", ""),
        "qst_number": _cab.get("qst_number", ""),
        "created_via": "mcp",
    }
    if billing_address is not None:
        data["billing_address"] = billing_address
    if due is not None:
        data["due_date"] = due
    for key, cap in (("notes", 1500), ("payment_terms", 500)):
        if key in args:
            value = str(args[key] or "")
            if len(value) > cap:
                raise ToolArgumentError(
                    f"`{key}` dépasse {cap} caractères — il serait tronqué "
                    "silencieusement à l'enregistrement."
                )
            data[key] = _clean_entity_text(value, key)
    if "retainer_applied_cents" in args:
        retainer = int(args["retainer_applied_cents"])
        if retainer < 0:
            raise ToolArgumentError(
                "`retainer_applied_cents` ne peut pas être négatif."
            )
        data["retainer_applied"] = retainer
    if "legacy_ref" in args:
        data["legacy_ref"] = _clean_entity_text(
            str(args["legacy_ref"] or ""), "legacy_ref"
        )
        _refuse_legacy_ref_collision("invoices", data["legacy_ref"])

    adjustment = args.get("adjustment")

    # Replay the model's number guards HERE so the refusal explains itself.
    # The likeliest failure of a resumed import is a number already taken,
    # and « ce numéro est déjà attribué » is worth far more to the caller
    # than the model's own error surfacing through the envelope.
    cleaned_number, number_errors = invoice_model._clean_imported_number(
        invoice_number
    )
    if number_errors:
        raise ToolArgumentError("; ".join(number_errors))
    if invoice_model.invoice_number_exists(cleaned_number):
        raise ToolArgumentError(
            f"Le numéro de facture « {cleaned_number} » existe déjà dans "
            "Pallas Athéna. Une facture importée conserve son numéro "
            "d'origine : vérifiez s'il n'a pas déjà été repris "
            "(find_imported)."
        )
    for label, ids in (("time_entry_ids", entry_ids),
                       ("expense_ids", expense_ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ToolArgumentError(
                f"`{label}` contient des identifiants en double : "
                + ", ".join(dupes)
                + ". Chaque source ne peut être facturée qu'une fois."
            )

    # Source pre-flight: the model refuses again under require_all_sources —
    # this pass exists only to name each offender by reason, which the model
    # cannot do as richly. The model's copy is the one that protects the
    # invoice actually written (a source flipped between the two reads is
    # never RETAINED, therefore never in source_refs, therefore invisible to
    # _SourceConflictError).

    invoice, errors = invoice_model.create_invoice(
        dossier_id, entry_ids, expense_ids, data,
        invoice_number=invoice_number,
        expected_total=expected_total,
        require_all_sources=True,
        adjustment=adjustment,
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))

    n_entries = len(entry_ids)
    n_expenses = len(expense_ids)
    return {
        "created": True,
        "entity_type": "invoice",
        "entity": _import_invoice_entity(invoice, dossier_id),
        "line_count": n_entries + n_expenses + (1 if adjustment else 0),
        "warnings": [
            f"Les {n_entries} entrée(s) et {n_expenses} déboursé(s) sont "
            "maintenant marqués facturés : le connecteur ne peut plus les "
            "modifier, sauf leur phase du litige (set_time_entry_phase, "
            "set_expense_phase). Pour défaire cet import, annulez la "
            "facture dans l'application — les entrées et déboursés "
            "redeviennent modifiables ; le numéro reste attaché à la "
            "facture annulée tant qu'elle n'est pas supprimée dans "
            "l'application.",
            "La facture est au BROUILLON. Le connecteur ne change jamais le "
            "statut d'une facture ni n'inscrit un paiement : promouvez-la "
            "dans l'application (brouillon → envoyée, puis le paiement à sa "
            "date historique), sinon le « Journal des honoraires » l'imprime "
            "avec 0 $ reçu.",
        ],
    }


def _import_invoice_entity(invoice: dict, dossier_id: str) -> dict:
    row = {
        "id": invoice.get("id", ""),
        "dossier_id": dossier_id,
        "label": invoice.get("invoice_number", ""),
        "invoice_number": invoice.get("invoice_number", ""),
        "date": date_str(_as_utc(invoice.get("date"))),
        "status": invoice.get("status", ""),
        "legacy_ref": invoice.get("legacy_ref", ""),
    }
    for key in ("subtotal_fees", "subtotal_expenses", "subtotal",
                "gst_amount", "qst_amount", "total"):
        _money(row, key, invoice.get(key, 0))
    return row


# ── 39. create_dossier (WRITE) ──────────────────────────────────────────


def create_dossier(args: dict) -> dict:
    return run_write(
        "create_dossier", args, lambda: _create_dossier_impl(args)
    )


def _create_dossier_impl(args: dict) -> dict:
    file_number = _clean_entity_text(args.get("file_number") or "", "file_number")
    title = _clean_entity_text(args.get("title") or "", "title")
    if not file_number:
        raise ToolArgumentError("`file_number` est requis.")
    if not title:
        raise ToolArgumentError("`title` est requis.")

    clients = _resolve_party_entries(args.get("clients"), "clients")
    if not clients:
        raise ToolArgumentError(
            "`clients` est requis et doit contenir au moins une partie."
        )
    opposing = _resolve_party_entries(
        args.get("opposing_parties"), "opposing_parties"
    )
    both = {c["id"] for c in clients} & {p["id"] for p in opposing}
    if both:
        raise ToolArgumentError(
            "Ces parties figurent à la fois comme client et comme partie "
            "adverse : " + ", ".join(sorted(both))
        )

    # Fail CLOSED. get_dossier_by_file_number RAISES on a query failure
    # precisely so this pre-check cannot read « absent » out of an outage and
    # mint a duplicate nothing can delete.
    if dossier_model.get_dossier_by_file_number(file_number) is not None:
        raise ToolArgumentError(
            f"Le numéro de dossier « {file_number} » existe déjà. Utilisez "
            "get_dossier pour le retrouver."
        )
    _refuse_legacy_ref_collision("dossiers", (args.get("legacy_ref") or "").strip())

    data: dict[str, Any] = {
        "file_number": file_number,
        "title": title,
        "clients": clients,
        "opposing_parties": opposing,
        "created_via": "mcp",
    }
    data.update(_dossier_field_updates(args))
    for key in ("forum_type", "forum", "district_judiciaire", "legacy_ref"):
        if key in args:
            data[key] = _clean_entity_text(str(args[key] or ""), key)
    if "status" in args:
        data["status"] = args["status"]
    for key in ("opened_date", "closed_date"):
        when = _write_date(args, key, required=False)
        if when is not None:
            data[key] = when

    if data.get("court_file_number") and (
        data.get("forum_type", "judiciaire") == "judiciaire"
    ):
        data.update(_derive_court_metadata(data["court_file_number"]))

    # normalize_forum is called by the ROUTE, never by the model. Skipping it
    # stores an inconsistent forum block — a préjudiciaire dossier without its
    # « Préjudiciaire » file number, so {{dossier.numero_cour}} fills blank.
    before = dict(data)
    dossier_model.normalize_forum(data)
    warnings = _forum_warnings(before, data)


    dossier, errors = dossier_model.create_dossier(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    warnings.extend(_prescription_warnings(dossier, data))
    if dossier.get("status") in ("fermé", "archivé"):
        warnings.append(
            f"Le dossier est créé « {dossier.get('status')} » : sa collection "
            "n'est pas annoncée à DavX5. C'est voulu pour une reprise "
            "historique — il n'y a rien à purger, la collection n'a jamais "
            "existé."
        )
    return _dossier_write_result(
        dossier, verb="created", warnings=warnings
    )


def _prescription_warnings(doc: dict, supplied: dict) -> list[str]:
    """Say when the « date pour agir » was COMPUTED rather than imported.

    ``_apply_prescription_deadline`` overwrites ``prescription_date`` as soon
    as ``droit_action_date`` and a periodic ``prescription_type`` coexist —
    silently, and the connector cannot force a historical value past it.
    """
    if not (supplied.get("droit_action_date") and supplied.get("prescription_type")):
        return []
    if not doc.get("prescription_date"):
        return []
    return [
        "La « date pour agir » a été CALCULÉE à partir du droit d'action et "
        f"du délai confirmé : {date_str(_as_utc(doc.get('prescription_date')))}. "
        "Elle n'est pas reprise telle quelle de l'ancien système — vérifiez-la."
    ]


# ── 40. update_dossier (WRITE — remplace la valeur nommée) ──────────────


def update_dossier(args: dict) -> dict:
    return run_write(
        "update_dossier", args, lambda: _update_dossier_impl(args)
    )


def _update_dossier_impl(args: dict) -> dict:
    dossier_id = (args.get("dossier_id") or "").strip()
    if not dossier_id:
        raise ToolArgumentError("`dossier_id` est requis.")

    # Tripwire. `status` is not declared, so additionalProperties: false
    # already rejects it — this carries the REASON, so a future schema slip
    # becomes a French refusal instead of a silent DavX5 desync.
    if "status" in args:
        raise ToolArgumentError(
            "Le statut d'un dossier ne se change pas par le connecteur : la "
            "fermeture exige la purge DavX5 du côté route "
            "(routes/dossiers._sync_dossier_dav_visibility), que les modèles "
            "n'appellent jamais. Un dossier fermé ici laisserait ses tâches, "
            "ses notes et ses audiences sur le téléphone pour toujours. "
            "Fixez le statut à la création, ou fermez-le dans l'application."
        )

    existing = dossier_model.get_dossier(dossier_id)
    if existing is None:
        raise ToolArgumentError(
            f"Dossier introuvable : {dossier_id}. Utilisez list_dossiers ou "
            "get_dossier pour obtenir un dossier_id valide."
        )
    # A trust entry regenerates the dossier's etag too, so a movement of the
    # client's funds since the caller's read also refuses the edit: the
    # refusal is right (the stored record changed), and re-reading is the
    # whole remedy.
    expected = _expected_etag(
        args, existing, tool="update_dossier",
        subject="Ce dossier a été modifié",
    )

    data = _dossier_field_updates(args, allow_clear=True)
    for key in ("title", "sommaire", "forum_type", "forum",
                "district_judiciaire", "legacy_ref"):
        if key in args:
            data[key] = _clean_entity_text(
                str(args[key] or ""), key,
                _SOMMAIRE_MAX if key == "sommaire" else None,
            )
    if "opened_date" in args:
        when = _write_date(args, "opened_date", required=False)
        if when is not None:
            data["opened_date"] = when

    # Party arrays are APPEND-only. _rebuild_party_mirrors recomputes
    # client_ids from whatever it is handed, with no diff and no warning:
    # passing [A] to a dossier holding [A, B] would DELETE B in silence and
    # report a success.
    for arg_key, field in (
        ("add_clients", "clients"),
        ("add_opposing_parties", "opposing_parties"),
    ):
        additions = _resolve_party_entries(args.get(arg_key), arg_key)
        if not additions:
            continue
        current = list(existing.get(field) or [])
        present = {c.get("id") for c in current}
        already = [a["id"] for a in additions if a["id"] in present]
        if already:
            raise ToolArgumentError(
                f"Ces parties figurent déjà dans `{field}` : "
                + ", ".join(sorted(already))
                + ". Rien n'a été écrit."
            )
        data[field] = current + additions

    if not data:
        raise ToolArgumentError(
            "Aucun champ à corriger : fournissez au moins un champ."
        )
    if "legacy_ref" in data and data["legacy_ref"] != existing.get("legacy_ref", ""):
        _refuse_legacy_ref_collision("dossiers", data["legacy_ref"])

    merged_forum = {
        **{k: existing.get(k, "") for k in
           ("forum_type", "forum", "district_judiciaire", "court_file_number",
            "tribunal", "competence", "palais_de_justice", "greffe_number",
            "juridiction_number")},
        **{k: v for k, v in data.items() if k in (
            "forum_type", "forum", "district_judiciaire", "court_file_number")},
    }
    if "court_file_number" in data and (
        merged_forum.get("forum_type", "judiciaire") == "judiciaire"
    ):
        merged_forum.update(_derive_court_metadata(data["court_file_number"]))
    touches_forum = any(
        k in data for k in
        ("forum_type", "forum", "district_judiciaire", "court_file_number")
    )
    warnings: list[str] = []
    if touches_forum:
        before = dict(merged_forum)
        dossier_model.normalize_forum(merged_forum)
        warnings = _forum_warnings(before, merged_forum)
        data.update(merged_forum)


    dossier, errors = dossier_model.update_dossier(
        dossier_id, data, expected_etag=expected
    )
    _raise_if_stale(
        errors, tool="update_dossier", subject="Ce dossier a été modifié",
        reread=lambda: dossier_model.get_dossier(dossier_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    warnings.extend(_prescription_warnings(dossier, data))
    return _dossier_write_result(
        dossier, verb="updated", warnings=warnings
    )


# ── 23. create_task (WRITE) ─────────────────────────────────────────────

def create_task(args: dict) -> dict:
    return run_write("create_task", args, lambda: _create_task_impl(args))


def _create_task_impl(args: dict) -> dict:
    # Tasks store None for « no dossier » (notes/hearings store "") —
    # collection_for handles all three, but the stored value must match
    # the model's convention.
    dossier_id, dossier = _resolve_write_dossier(args, required=False)
    title = _clean_entity_text(args.get("title") or "", "title")
    description = (args.get("description") or "").strip()
    stamp = f"*Créée par Claude le {format_date_fr(_today_mtl())}*"
    description = f"{description}\n\n{stamp}" if description else stamp
    description = _clean_entity_text(description, "description")
    due = _write_date(args, "due_date", required=False)
    phase, sous_phase = _resolve_phase_pair(args)

    # EXPLICIT whitelist. `status` is PINNED to « à_faire »: a caller-
    # supplied « terminée » would mint a completed task with a fabricated
    # completion timestamp (models/task auto-stamps completed_date) — and
    # create-only means creating WORK, never history.
    data = {
        "dossier_id": dossier_id or None,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": title,
        "description": description,
        "priority": args.get("priority") or "normale",
        "category": args.get("category") or "autre",
        "status": "à_faire",
        "due_date": due,
        "phase": phase,
        "sous_phase": sous_phase,
        "created_via": "mcp",
    }

    def _entity(doc: dict) -> dict:
        return {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("dossier_id") or "",
            "dossier_file_number": doc.get("dossier_file_number", ""),
            "dossier_title": doc.get("dossier_title", ""),
            "label": doc.get("title", ""),
            "date": date_str(doc.get("due_date")),
            "status": doc.get("status", ""),
            "priority": doc.get("priority", ""),
            "category": doc.get("category", ""),
            "phase": doc.get("phase", ""),
            "sous_phase": doc.get("sous_phase", ""),
        }

    task, errors = task_model.create_task(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "task", _entity(task), dossier=dossier, dav_exposed=True,
    )


# ── 24. create_hearing (WRITE) ──────────────────────────────────────────

def create_hearing(args: dict) -> dict:
    return run_write(
        "create_hearing", args, lambda: _create_hearing_impl(args)
    )


def _parse_hhmm(raw: str, name: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(raw.strip(), "%H:%M")
        return parsed.hour, parsed.minute
    except (ValueError, AttributeError):
        raise ToolArgumentError(f"`{name}` doit être une heure HH:MM.")


_DEFAULT_REMINDER = 1440
_HEARING_CREATE_STATUSES = ("à_confirmer", "confirmée")


def _clean_conference_uri(raw: Any) -> str:
    """The video link, refused (naming the field) unless http/https.

    ``conference_uri`` is rendered as an ``<a href>`` in the application:
    the model's whitelist (``is_safe_conference_uri``) is its only guard,
    and it is REPEATED here so the refusal names the field instead of
    arriving as the model's generic sentence."""
    uri = _clean_entity_text(str(raw or ""), "conference_uri")
    if not hearing_model.is_safe_conference_uri(uri):
        raise ToolArgumentError(
            "« conference_uri » doit être une adresse http:// ou https:// — "
            "tout autre schéma serait un lien dangereux dans l'application. "
            "Rien n'a été écrit."
        )
    return uri


def _hearing_modality_data(args: dict, *, defaults: bool) -> dict:
    """modalite / conference_uri / reminder_minutes, by PRESENCE.

    With *defaults* (a creation) the model's own defaults are written
    explicitly, so the result echoes what was stored rather than what the
    model would have filled in behind the handler's back."""
    data: dict[str, Any] = {}
    if "modalite" in args:
        _refuse_outside_vocabulary(
            args, "modalite", hearing_model.VALID_MODALITES)
        data["modalite"] = args["modalite"]
    elif defaults:
        data["modalite"] = "présentiel"
    if "conference_uri" in args:
        data["conference_uri"] = _clean_conference_uri(args["conference_uri"])
    elif defaults:
        data["conference_uri"] = ""
    if "reminder_minutes" in args:
        value = args["reminder_minutes"]
        if (isinstance(value, bool)
                or value not in hearing_model.VALID_REMINDER_MINUTES):
            raise ToolArgumentError(
                "« reminder_minutes » : valeur hors vocabulaire. Valeurs "
                "admises : "
                + ", ".join(str(v) for v in hearing_model.VALID_REMINDER_MINUTES)
                + ". Rien n'a été écrit."
            )
        data["reminder_minutes"] = int(value)
    elif defaults:
        data["reminder_minutes"] = _DEFAULT_REMINDER
    return data


def _modality_warnings(doc: dict, args: dict) -> list[str]:
    """A video link stored beside a modality that does not show it — said,
    since the model keeps the link (round-trip) and the caller would
    otherwise believe the phone and Outlook carry it."""
    if not ({"modalite", "conference_uri"} & args.keys()):
        return []
    if doc.get("conference_uri") and doc.get("modalite") != "visioconférence":
        return [
            "Le lien de visioconférence est enregistré, mais il n'est "
            "transmis au téléphone et à Outlook qu'en modalité "
            "« visioconférence »."
        ]
    return []


def _hearing_create_data(args: dict) -> tuple[dict, dict]:
    """The model payload of a creation, and the resolved dossier.

    SHARED by create_hearing and create_hearing_series — a series is N
    copies of this one prototype, so the two tools cannot build it two
    ways. An EXPLICIT whitelist: ``id``/``vevent_uid`` are never
    addressable (create_hearing honoured a caller-supplied id with a set()
    until lot 1a, which here would have overwritten an existing event; it
    discards them now — only the DAV create's ``dav_id`` keyword names a
    document — and this whitelist stays as the belt). ``status`` is only
    à_confirmer (the default) or confirmée, and ``confirmation`` stays ""
    (visible everywhere — this is not a Bookings import)."""
    dossier_id, dossier = _resolve_write_dossier(args, required=False)
    title = _clean_entity_text(args.get("title") or "", "title")
    if not title:
        raise ToolArgumentError("`title` est requis.")
    hearing_type = args.get("hearing_type") or "rencontre"
    if hearing_type not in hearing_model.VALID_HEARING_TYPES:
        raise ToolArgumentError(
            "« hearing_type » : valeur hors vocabulaire — voir l'énumération "
            "du schéma. Rien n'a été créé."
        )
    status = args.get("status") or "à_confirmer"
    if status not in _HEARING_CREATE_STATUSES:
        raise ToolArgumentError(
            "« status » : à la création, seulement « à_confirmer » ou "
            "« confirmée ». Un report, une annulation ou un événement déjà "
            "tenu se consignent ensuite avec update_hearing. Rien n'a été "
            "créé."
        )
    all_day = bool(args.get("all_day"))

    raw_date = (args.get("date") or "").strip()
    if not raw_date:
        raise ToolArgumentError("`date` est requise (AAAA-MM-JJ).")
    day = _parse_iso_date(raw_date, "date")
    if all_day or not args.get("start_time"):
        # All-day (or timeless) events live at midnight UTC — the
        # date-only convention of the calendar layer.
        start_dt = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end_dt = None
        all_day = True
    else:
        h, m = _parse_hhmm(args["start_time"], "start_time")
        start_dt = datetime(
            day.year, day.month, day.day, h, m, tzinfo=MTL
        ).astimezone(timezone.utc)
        end_dt = None
        if args.get("end_time"):
            eh, em = _parse_hhmm(args["end_time"], "end_time")
            end_dt = datetime(
                day.year, day.month, day.day, eh, em, tzinfo=MTL
            ).astimezone(timezone.utc)

    notes_text = (args.get("notes") or "").strip()
    stamp = f"*Créée par Claude le {format_date_fr(_today_mtl())}*"
    notes_text = f"{notes_text}\n\n{stamp}" if notes_text else stamp
    notes_text = _clean_entity_text(notes_text, "notes")

    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": title,
        "hearing_type": hearing_type,
        "start_datetime": start_dt,
        "end_datetime": end_dt,
        "all_day": all_day,
        "location": _clean_entity_text(args.get("location") or "", "location"),
        "court": _clean_entity_text(args.get("court") or "", "court"),
        "judge": _clean_entity_text(args.get("judge") or "", "judge"),
        "notes": notes_text,
        "status": status,
        **_hearing_modality_data(args, defaults=True),
        "created_via": "mcp",
    }
    return data, dossier


def _hearing_entity(doc: dict) -> dict:
    """The event AS STORED after a write — the shape every calendar write
    result shares (create_hearing, update_hearing). ``etag`` is the one the
    next edit presents."""
    all_day = bool(doc.get("all_day"))
    start = _as_utc(doc.get("start_datetime"))
    end = _as_utc(doc.get("end_datetime"))
    return {
        "id": doc.get("id", ""),
        "dossier_id": doc.get("dossier_id") or "",
        "dossier_file_number": doc.get("dossier_file_number", ""),
        "dossier_title": doc.get("dossier_title", ""),
        "label": doc.get("title", ""),
        "date": date_str(start) if all_day else iso_mtl(start),
        "end": date_str(end) if all_day else iso_mtl(end),
        "hearing_type": doc.get("hearing_type", ""),
        "forum": hearing_model.forum_of(doc.get("hearing_type", "")),
        "all_day": all_day,
        "status": str(doc.get("status") or ""),
        "modalite": str(doc.get("modalite") or "présentiel"),
        "conference_uri": str(doc.get("conference_uri") or ""),
        "reminder_minutes": _reminder_minutes(doc),
        "serie_id": str(doc.get("serie_id") or ""),
        "source": str(doc.get("source") or ""),
        "etag": concurrency.etag_of(doc),
    }


def _create_hearing_impl(args: dict) -> dict:
    data, dossier = _hearing_create_data(args)
    hearing, errors = hearing_model.create_hearing(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    payload = _entity_write_result(
        "hearing", _hearing_entity(hearing), dossier=dossier, dav_exposed=True,
    )
    payload["warnings"].extend(_modality_warnings(hearing, args))
    return payload


# ── create_hearing_series (WRITE, lot 1b L7) ───────────────────────────

def create_hearing_series(args: dict) -> dict:
    return run_write(
        "create_hearing_series", args,
        lambda: _create_hearing_series_impl(args),
    )


def _create_hearing_series_impl(args: dict) -> dict:
    """N linked occurrences in ONE atomic batch, through the model.

    The CTag bump rides INSIDE the model's batch (``bump_ctag_in_batch``):
    committing and then bumping would leave N events live in the
    application that DavX5 never re-syncs. So this handler never bumps —
    no ``_entity_write_result`` (which would bump a second time), and no
    ``list_series`` re-read after the commit (it propagates errors, and a
    raise there would report a committed series as a failure)."""
    data, dossier = _hearing_create_data(args)
    frequency = str(args.get("frequency") or "")
    if frequency not in recurrence.VALID_FREQUENCIES:
        raise ToolArgumentError(
            "« frequency » : valeur hors vocabulaire. Valeurs admises : "
            + ", ".join(recurrence.VALID_FREQUENCIES) + ". Rien n'a été créé."
        )
    has_count = args.get("count") is not None
    has_until = bool(str(args.get("until") or "").strip())
    if has_count == has_until:
        raise ToolArgumentError(
            "Indiquez EXACTEMENT une borne : `count` (1 à "
            f"{recurrence.MAX_SERIE_OCCURRENCES}) ou `until` (AAAA-MM-JJ) — "
            "une série doit se terminer, et d'une seule façon. Rien n'a été "
            "créé."
        )
    count: Optional[int] = None
    until: Optional[date] = None
    if has_count:
        count = args["count"]
        if (isinstance(count, bool) or not isinstance(count, int)
                or not 1 <= count <= recurrence.MAX_SERIE_OCCURRENCES):
            raise ToolArgumentError(
                "« count » doit être un entier de 1 à "
                f"{recurrence.MAX_SERIE_OCCURRENCES}. Rien n'a été créé."
            )
    else:
        until = _parse_iso_date(str(args["until"]), "until")

    occurrences, errors = hearing_model.create_hearing_series(
        data, frequency, count=count, until=until,
    )
    if errors:
        # The model's words (utils.recurrence.validate_rule — French, never
        # a truncation: a series past the ceiling is refused whole).
        raise ToolArgumentError("; ".join(errors) + " Rien n'a été créé.")

    first = occurrences[0]
    serie_id = str(first.get("serie_id") or "")
    status = dossier.get("status", "")
    dav_visible = status in ("actif", "en_attente")
    warnings: list[str] = []
    if not dav_visible:
        warnings.append(
            f"Le dossier est « {status} » : la série est enregistrée et "
            "visible dans l'application, mais les dossiers fermés ou "
            "archivés ne sont pas exposés à DavX5 — elle n'apparaîtra pas "
            "sur le téléphone."
        )
    first_day = hearing_model.occurrence_day(first)
    if first_day is not None and first_day < deadlines.today_mtl():
        warnings.append(
            "La première occurrence est déjà passée : la série commence "
            "dans le passé, comme demandé."
        )
    warnings.extend(_modality_warnings(first, args))
    log_hearing_series_event(
        "series_created", serie_id,
        occurrences=len(occurrences),
        dossier_id=first.get("dossier_id", "") or "",
        frequence=frequency,
        ctag_bumped=True,
        via="mcp",
    )

    all_day = bool(first.get("all_day"))

    def _when(value) -> Optional[str]:
        value = _as_utc(value)
        return date_str(value) if all_day else iso_mtl(value)

    return {
        "created": True,
        "entity_type": "hearing_series",
        "entity": {
            "id": serie_id,
            "dossier_id": first.get("dossier_id") or "",
            "dossier_file_number": first.get("dossier_file_number", ""),
            "dossier_title": first.get("dossier_title", ""),
            "label": first.get("title", ""),
            "date": _when(first.get("start_datetime")),
            "hearing_type": first.get("hearing_type", ""),
            "forum": hearing_model.forum_of(first.get("hearing_type", "")),
            "all_day": all_day,
            "status": str(first.get("status") or ""),
            "modalite": str(first.get("modalite") or "présentiel"),
        },
        "serie_id": serie_id,
        "frequency": frequency,
        "rule_label": recurrence.describe(first.get("serie_rule")),
        "occurrences_count": len(occurrences),
        "occurrences": [
            {
                "id": occ.get("id", ""),
                "start": _when(occ.get("start_datetime")),
                "end": _when(occ.get("end_datetime")),
                "etag": concurrency.etag_of(occ),
            }
            for occ in occurrences
        ],
        # The bump committed in the same batch as the occurrences.
        "ctag_bumped": True,
        "dav_synced": dav_visible,
        "warnings": warnings,
    }


# ── update_hearing (WRITE, lot 1b L7) ──────────────────────────────────

_HEARING_SUBJECT = "Cet événement a été modifié"
_RDV_SUBJECT = "Ce rendez-vous a été modifié"
_HEARING_EDIT_KEYS = (
    "title", "hearing_type", "date", "start_time", "end_time", "all_day",
    "location", "court", "judge", "modalite", "conference_uri",
    "reminder_minutes", "notes", "notes_append", "status",
    "detach_from_series", "dossier_id",
)
_HEARING_TEXT_KEYS = ("location", "court", "judge")
_HEARING_SLOT_KEYS = ("start_datetime", "end_datetime", "all_day")
# D10 (resolved by the lawyer 2026-09-27, reversing lot 1b's « editable
# with a warning ») — on a CONFIRMED Bookings rendez-vous the Outlook
# meeting the CLIENT holds is the reference, and nothing here writes back
# to it (the Outlook mirror never mirrors a Bookings source). So the
# connector changes only what that meeting does not carry: the dossier the
# consultation is filed under, and its notes. Anything else — its slot, its
# status, its wording, its modality — would make Athéna disagree with the
# client's invitation, and is refused, pointing to Outlook (the Bookings
# sync then flags the change in Réception, where it is applied). The web
# form is NOT restricted: the lawyer keeps his hand in the application.
_BOOKINGS_OPEN_ARGS = frozenset({"dossier_id", "notes", "notes_append"})
_HEARING_SLOT_ARGS = ("date", "start_time", "end_time", "all_day")
_HEARING_CANCELLED = (
    "L'événement est annulé : il sort de get_agenda, sa copie dans votre "
    "calendrier Outlook est retirée au prochain cycle (10 minutes au plus), "
    "et le téléphone le montre annulé."
)
# The other direction, said as loudly: a status that takes an event OUT of
# « annulée » undoes a cancellation, and the « jamais » list promises that
# never happens in silence (mcp/disclosure NEVERS « uncancel »).
_HEARING_UNCANCELLED = (
    "L'événement n'est plus annulé : il revient dans get_agenda, le "
    "téléphone ne le montre plus annulé, et sa copie Outlook est recréée "
    "au prochain cycle."
)
# A confirmed Bookings rendez-vous whose Outlook meeting has since been
# moved or cancelled (the sync records a divergence, never overwrites a
# confirmed event): notes or a dossier filed here would build on a slot that
# may no longer hold. Said, never blocked — Réception is where it is
# resolved. Since D10 the MOVER is not necessarily the client: the refusal
# below sends the lawyer to Outlook to reschedule, and the sync cannot tell
# his move from the client's — so the sentence names the meeting, never who
# moved it (« Le client a déplacé » would attribute his own reschedule to
# the client).
_BOOKINGS_DIVERGENCE_UNSEEN = (
    "La réunion Outlook de ce rendez-vous a été déplacée ou annulée (par le "
    "client, ou dans Outlook même), et l'écart n'a pas encore été traité : "
    "voyez la Réception (onglet « Rendez-vous »), d'où on l'applique à "
    "Athéna, avant de vous fier à cette version."
)


def _read_hearing(hearing_id: str) -> dict:
    """The event an edit is about — « introuvable » only when the store SAID
    so (the :func:`_read_task` rule: ``get_hearing`` answers ``None`` on a
    read error too)."""
    try:
        hearing = hearing_model.get_hearing_strict(hearing_id)
    except Exception:
        raise ToolArgumentError(
            "Lecture de l'événement impossible — réessayez. Rien n'a été "
            "modifié."
        )
    if not hearing:
        raise ToolArgumentError(
            f"Événement introuvable : {hearing_id}. Utilisez list_hearings "
            "ou get_agenda pour obtenir un hearing_id valide. Rien n'a été "
            "modifié."
        )
    return hearing


def _refuse_undecided_booking(hearing: dict) -> None:
    """A Bookings import still awaiting (or refused) is not an event of the
    calendar: the MCP reads never show one, and ``get_hearing`` — which
    does not filter — must not become a side door that edits it. The
    decision has its own tool."""
    confirmation = hearing.get("confirmation") or ""
    if confirmation == "refusée":
        raise ToolArgumentError(
            "Cette demande Bookings a été refusée : elle ne se modifie plus. "
            "Rien n'a été modifié."
        )
    if confirmation:
        raise ToolArgumentError(
            "Cet événement est une demande Bookings qui attend votre "
            "décision : il ne se modifie pas ici. Confirmez-la ou refusez-la "
            "avec decide_rendez_vous (liste : list_hearings, bookings "
            "« pending »). Rien n'a été modifié."
        )


def _is_confirmed_booking(hearing: dict) -> bool:
    """A « Bookings with me » reservation the lawyer has confirmed — the
    one kind of event whose Outlook meeting belongs to the CLIENT."""
    return (hearing.get("source") == "bookings"
            and not (hearing.get("confirmation") or ""))


def _booking_locked_args(args: dict, changed: list[str]) -> list[str]:
    """The arguments of this call that would CHANGE a confirmed Bookings
    rendez-vous beyond its dossier and notes (D10), in schema order.

    Judged on what the call would actually change (``changed``, computed on
    the renormalized document), never on presence alone: a value already
    stored is no disagreement with Outlook. A slot the model renormalizes on
    its own (a legacy event missing its end) names no argument, so it is
    never blamed on the caller."""
    changed_set = set(changed)
    slot_changed = bool(changed_set & set(_HEARING_SLOT_KEYS))
    names = []
    for key in _HEARING_EDIT_KEYS:
        if key not in args or key in _BOOKINGS_OPEN_ARGS:
            continue
        if key in _HEARING_SLOT_ARGS:
            hit = slot_changed
        elif key == "detach_from_series":
            hit = "serie_id" in changed_set
        else:
            hit = key in changed_set
        if hit:
            names.append(key)
    return names


def _refuse_locked_booking_change(args: dict, changed: list[str]) -> None:
    """D10 — a confirmed Bookings rendez-vous changes here only by its
    dossier and its notes. Refused BEFORE any write, naming the arguments,
    and pointing to where each change CAN be made: a reschedule or a
    cancellation in Outlook (the meeting the client holds — the Bookings
    sync carries exactly those two back, through Réception), anything else
    in the application, whose form is not restricted.

    « Anything else » is NOT sent to Outlook: the type, the reminder, the
    court and the judge are not on the Outlook meeting at all, and a title
    or a location edited there is never carried back (the sync compares
    the slot only). « Change it in Outlook » would send the user to a place
    that cannot do it."""
    names = _booking_locked_args(args, changed)
    if not names:
        return
    plural = len(names) > 1
    raise ToolArgumentError(
        f"{', '.join(f'`{n}`' for n in names)} "
        f"{'refusés' if plural else 'refusé'} : ce rendez-vous vient de "
        "Bookings, et c'est la réunion Outlook que le client détient qui "
        "fait foi — le connecteur n'en change que le dossier (dossier_id) "
        "et les notes (notes, notes_append). Pour le reporter ou l'annuler, "
        "faites-le dans Outlook : le report ou l'annulation y est repéré par "
        "la synchronisation Bookings (10 minutes au plus) et signalé dans la "
        "Réception (onglet « Rendez-vous »), d'où on l'applique à Athéna. "
        "Tout autre changement se fait dans l'application, dont le "
        "formulaire n'est pas restreint — la réunion Outlook, elle, ne suit "
        "pas. Rien n'a été modifié."
    )


def _hearing_slot_data(args: dict, existing: dict) -> dict:
    """The slot keys of an edit, from date / start_time / end_time /
    all_day — Montréal wall-clock in, UTC out.

    * An omitted day keeps the stored civil day (``occurrence_day``: the
      UTC date of an all-day event, the Montréal date of a timed one).
    * An omitted start hour keeps the stored MONTRÉAL wall-clock hour, and
      the instant is recomputed on the new day through ``mtl_to_utc`` — so
      « 9 h » stays 9 h across a daylight-saving change (the series rule,
      ``models.hearing._occurrence_slots``).
    * An omitted end is left to the model, which keeps the stored duration
      (``_renormalize_slot``) — or, on an all-day ↔ timed flip, the
      default slot.
    """
    if not any(k in args for k in ("date", "start_time", "end_time", "all_day")):
        return {}
    stored_all_day = bool(existing.get("all_day"))
    if "all_day" in args:
        all_day = bool(args["all_day"])
    elif "start_time" in args or "end_time" in args:
        all_day = False
    else:
        all_day = stored_all_day
    if all_day and ("start_time" in args or "end_time" in args):
        raise ToolArgumentError(
            "« all_day » vrai n'admet ni start_time ni end_time : un "
            "événement d'une journée entière n'a pas d'heure. Rien n'a été "
            "modifié."
        )
    toggled = all_day != stored_all_day
    if "date" in args:
        day = _parse_iso_date(str(args["date"]), "date")
    else:
        day = hearing_model.occurrence_day(existing)
    if day is None:
        raise ToolArgumentError(
            "`date` est requise : l'événement enregistré n'a pas de date "
            "lisible. Rien n'a été modifié."
        )
    data: dict[str, Any] = {}
    if "all_day" in args or toggled:
        data["all_day"] = all_day
    if all_day:
        if "date" in args or toggled:
            data["start_datetime"] = datetime(
                day.year, day.month, day.day, tzinfo=timezone.utc)
        return data

    if "start_time" in args:
        hh, mm = _parse_hhmm(str(args["start_time"]), "start_time")
        wall = dtime(hh, mm)
    elif stored_all_day:
        raise ToolArgumentError(
            "`start_time` est requis pour donner une heure à un événement "
            "d'une journée entière. Rien n'a été modifié."
        )
    else:
        stored_start = _as_utc(existing.get("start_datetime"))
        if not isinstance(stored_start, datetime):
            raise ToolArgumentError(
                "`start_time` est requis : l'événement enregistré n'a pas "
                "d'heure de début lisible. Rien n'a été modifié."
            )
        wall = stored_start.astimezone(MTL).time().replace(tzinfo=None)
    start = mtl_to_utc(datetime.combine(day, wall))
    if "date" in args or "start_time" in args or toggled:
        data["start_datetime"] = start
    if "end_time" in args:
        eh, em = _parse_hhmm(str(args["end_time"]), "end_time")
        end = mtl_to_utc(datetime.combine(day, dtime(eh, em)))
        if end <= start:
            raise ToolArgumentError(
                "« end_time » doit suivre l'heure de début, le même jour. "
                "Rien n'a été modifié."
            )
        data["end_datetime"] = end
    return data


def _appended_hearing_notes(existing: dict, raw: Any) -> str:
    """The notes AS THEY WILL BE STORED after an append — validated whole.

    The length is checked FIRST (``sanitize`` truncates at 2000 with no
    signal), then the post-condition ``sanitize(combined) == combined``:
    an unpaired ``<`` already in the notes plus a ``>`` in the addition
    would make the tag regex span the join and delete text — both halves
    pass a per-field check (the append_to_note lesson)."""
    addition = str(raw or "").strip()
    if not addition:
        raise ToolArgumentError(
            "« notes_append » est vide. Rien n'a été modifié.")
    stored = str(existing.get("notes") or "")
    stamp = f"*Ajouté par Claude le {format_date_fr(_today_mtl())}*"
    combined = (f"{stored}\n\n{stamp}\n{addition}" if stored.strip()
                else f"{stamp}\n{addition}")
    if len(combined) > _ENTITY_FIELD_MAX:
        raise ToolArgumentError(
            f"Les notes ainsi complétées dépasseraient {_ENTITY_FIELD_MAX} "
            f"caractères ({len(combined)}) — elles seraient tronquées à "
            "l'enregistrement. Raccourcissez l'ajout, ou versez le détail "
            "dans une note (create_note). Rien n'a été modifié."
        )
    if not _survives_storage(combined, _ENTITY_FIELD_MAX):
        raise ToolArgumentError(
            "Les notes ainsi complétées contiennent du texte entre chevrons "
            "qui serait supprimé à l'enregistrement (avec celui qu'elles "
            f"portent déjà). {_CHEVRON_ADVICE}"
        )
    return combined


def _hearing_changed_fields(
    existing: dict, data: dict, merged: dict,
) -> list[str]:
    """The model fields this edit changes. The slot is judged on the
    RENORMALIZED document (what the model will store), never on the raw
    arguments: a new start keeps the duration, so the end moves too."""
    changed = []
    for key in _HEARING_SLOT_KEYS:
        before, after = existing.get(key), merged.get(key)
        if key == "all_day":
            if bool(before) != bool(after):
                changed.append(key)
        elif _as_utc(before) != _as_utc(after):
            changed.append(key)
    for key, value in data.items():
        if key in _HEARING_SLOT_KEYS or key in (
                "dossier_id", "dossier_file_number", "dossier_title"):
            continue
        before = existing.get(key)
        if key == "reminder_minutes":
            if before != value:
                changed.append(key)
        elif (before or "") != (value or ""):
            changed.append(key)
    return changed


def update_hearing(args: dict) -> dict:
    return run_write("update_hearing", args, lambda: _update_hearing_impl(args))


def _update_hearing_impl(args: dict) -> dict:
    hearing_id = (args.get("hearing_id") or "").strip()
    if not hearing_id:
        raise ToolArgumentError("`hearing_id` est requis.")
    existing = _read_hearing(hearing_id)
    _refuse_undecided_booking(existing)
    if not any(key in args for key in _HEARING_EDIT_KEYS):
        raise ToolArgumentError(
            "Aucun champ à modifier : nommez au moins title, hearing_type, "
            "date, start_time, end_time, all_day, location, court, judge, "
            "modalite, conference_uri, reminder_minutes, notes, "
            "notes_append, status, detach_from_series ou dossier_id."
        )
    if "notes" in args and "notes_append" in args:
        raise ToolArgumentError(
            "`notes` (remplacer) et `notes_append` (ajouter) s'excluent : "
            "un seul par appel. Rien n'a été modifié."
        )
    _refuse_outside_vocabulary(
        args, "hearing_type", hearing_model.VALID_HEARING_TYPES)
    _refuse_outside_vocabulary(args, "status", hearing_model.VALID_STATUSES)

    # EXPLICIT whitelist, presence-based: an absent key is left alone, a
    # present one replaces. Never a server-owned key (source, confirmation,
    # graph_*, the requester, partie_id): the model refuses them outside
    # server_fields, and the series link moves only through unlink_hearing.
    data: dict[str, Any] = {}
    if "title" in args:
        title = _clean_entity_text(args.get("title") or "", "title")
        if not title:
            raise ToolArgumentError("« title » ne peut pas être vide.")
        data["title"] = title
    if "hearing_type" in args:
        data["hearing_type"] = args["hearing_type"]
    for key in _HEARING_TEXT_KEYS:
        if key in args:
            data[key] = _clean_entity_text(args.get(key) or "", key)
    data.update(_hearing_modality_data(args, defaults=False))
    if "status" in args:
        data["status"] = args["status"]
    if "notes" in args:
        data["notes"] = _clean_entity_text(args.get("notes") or "", "notes")
    elif "notes_append" in args:
        data["notes"] = _appended_hearing_notes(existing, args["notes_append"])
    data.update(_hearing_slot_data(args, existing))

    old_dossier = existing.get("dossier_id") or ""
    new_dossier = (
        (args.get("dossier_id") or "").strip() if "dossier_id" in args
        else old_dossier
    )
    moved = new_dossier != old_dossier
    serie_id = str(existing.get("serie_id") or "")
    detaching = bool(args.get("detach_from_series")) and bool(serie_id)
    if moved and serie_id and not detaching:
        raise ToolArgumentError(
            "`dossier_id` refusé : cet événement est une occurrence d'une "
            "série, et une série ne s'étend pas sur plusieurs dossiers. "
            "Détachez-la d'abord — detach_from_series: true, dans le même "
            "appel ou avant. Rien n'a été modifié."
        )

    merged = {**existing, **data}
    hearing_model._renormalize_slot(existing, data, merged)
    changed = _hearing_changed_fields(existing, data, merged)
    if moved:
        changed.append("dossier_id")
    if detaching:
        changed.append("serie_id")
    if _is_confirmed_booking(existing):
        _refuse_locked_booking_change(args, changed)
    notes_pre: list[str] = []
    if args.get("detach_from_series") and not serie_id:
        notes_pre.append(
            "Cet événement n'appartient à aucune série : detach_from_series "
            "n'avait rien à faire."
        )
    if not changed:
        return _hearing_edit_payload(
            existing, args, dossier=_dossier_for(old_dossier), changed=[],
            moved=False, old_dossier=old_dossier, wrote=False,
            detached=False, previous_serie_id="",
            previous_status=str(existing.get("status") or ""),
            notes=notes_pre,
        )

    # « terminée » records something that HAPPENED: never on a future day
    # (judged on the event as it will be stored, so a reschedule into the
    # future in the same call is caught too).
    if merged.get("status") == "terminée" and (
            "status" in changed or set(_HEARING_SLOT_KEYS) & set(changed)):
        day = hearing_model.occurrence_day(merged)
        if day is not None and day > deadlines.today_mtl():
            raise ToolArgumentError(
                "« status » terminée refusé : l'événement est daté du "
                f"{format_date_fr(day)} — un événement à venir ne peut pas "
                "être dit terminé. Rien n'a été modifié."
            )

    expected = _expected_etag(
        args, existing, tool="update_hearing", subject=_HEARING_SUBJECT)
    target: Optional[dict] = None
    if moved:
        new_dossier, target = _resolve_move(new_dossier)
        # The labels are re-snapshotted from the TARGET — what the phone
        # prints and what the Outlook mirror writes as « N/R ».
        data["dossier_id"] = new_dossier
        data["dossier_file_number"] = target.get("file_number", "")
        data["dossier_title"] = target.get("title", "")

    # The model re-validates the WHOLE merged document: a legacy event with
    # an out-of-vocabulary value fails for a reason invisible in the app.
    # Said here, before any write.
    errors = hearing_model._validate(merged)
    if errors:
        raise ToolArgumentError("; ".join(errors))

    if detaching:
        updated, errors = hearing_model.unlink_hearing(
            hearing_id, data, expected_etag=expected)
    else:
        updated, errors = hearing_model.update_hearing(
            hearing_id, data, expected_etag=expected)
    _raise_if_stale(
        errors, tool="update_hearing", subject=_HEARING_SUBJECT,
        reread=lambda: hearing_model.get_hearing(hearing_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))

    payload = _hearing_edit_payload(
        updated, args, dossier=target if moved else _dossier_for(old_dossier),
        changed=changed, moved=moved, old_dossier=old_dossier, wrote=True,
        detached=detaching, previous_serie_id=serie_id if detaching else "",
        previous_status=str(existing.get("status") or ""), notes=notes_pre,
    )
    if detaching:
        log_hearing_series_event(
            "series_unlinked", serie_id,
            hearing_id=hearing_id,
            dossier_id=updated.get("dossier_id") or "",
            ctag_bumped=payload["ctag_bumped"],
            via="mcp",
        )
    return payload


def _hearing_edit_payload(
    doc: dict, args: dict, *, dossier: Optional[dict], changed: list[str],
    moved: bool, old_dossier: str, wrote: bool, detached: bool,
    previous_serie_id: str, previous_status: str, notes: list[str],
) -> dict:
    payload = _entity_write_result(
        "hearing", _hearing_entity(doc), dossier=dossier, dav_exposed=True,
        verb="updated", created=False, wrote=wrote,
        previous_dossier_id=old_dossier,
    )
    bookings = doc.get("source") == "bookings"
    if not wrote:
        mirror = "unchanged"
    elif bookings:
        mirror = "not_mirrored"
    elif doc.get("status") == "annulée":
        mirror = "removed"
    else:
        mirror = "follows"
    payload.update({
        "changed_fields": changed,
        "moved": moved,
        "detached": detached,
        "previous_serie_id": previous_serie_id,
        "previous_status": previous_status,
        "outlook_mirror": mirror,
    })
    warnings = payload["warnings"]
    if bookings:
        # D10: only its dossier or its notes can have changed — neither is
        # carried by the client's Outlook meeting, so there is no Outlook
        # disagreement to warn about; only an unseen Outlook-side change is.
        divergence = doc.get("bookings_divergence") or {}
        if (isinstance(divergence, dict) and divergence.get("motif")
                and not divergence.get("vu")):
            warnings.append(_BOOKINGS_DIVERGENCE_UNSEEN)
    elif wrote and "status" in changed and doc.get("status") == "annulée":
        warnings.append(_HEARING_CANCELLED)
    elif (wrote and "status" in changed and previous_status == "annulée"
            and doc.get("status") != "annulée"):
        warnings.append(_HEARING_UNCANCELLED)
    if wrote and not bookings:
        # Never on a Bookings rendez-vous: D10 lets a modality or link
        # argument through there only when it resends the STORED value, so
        # the sentence would describe nothing this call did — and its
        # « transmis … à Outlook » is false for an event Athéna never
        # mirrors (the meeting is the client's).
        warnings.extend(_modality_warnings(doc, args))
    warnings.extend(notes)
    if not wrote:
        warnings.append(
            "Toutes les valeurs envoyées étaient déjà enregistrées : rien "
            "n'a été modifié."
        )
    return payload


# ── decide_rendez_vous (WRITE, OUTBOUND — lot 1b L7) ───────────────────

# The service's sentences are Réception's (« rechargez la page », « Ne
# cliquez pas… »). The connector says the same refusals in ITS terms,
# naming the argument that changes the outcome — never the requester.
_RDV_REFUSALS = {
    rendez_vous_service.ANNULE_PAR_LE_CLIENT: (
        "Le client a annulé ce rendez-vous : il ne peut plus être confirmé. "
        "action « refuser » le retire (sans rien envoyer au client). Rien "
        "n'a été écrit."
    ),
    rendez_vous_service.DEJA_REFUSE: (
        "Ce rendez-vous a déjà été refusé : il ne peut plus être confirmé. "
        "Rien n'a été écrit."
    ),
    rendez_vous_service.DEJA_CONFIRME_NE_SE_REFUSE_PLUS: (
        "Ce rendez-vous est déjà confirmé : il ne se refuse plus, et le "
        "connecteur ne l'annule pas non plus. Pour l'annuler, faites-le "
        "dans Outlook — la réunion que le client détient : la "
        "synchronisation Bookings le signale ensuite dans la Réception "
        "(onglet « Rendez-vous »). Rien n'a été écrit."
    ),
    rendez_vous_service.SANS_COURRIEL: (
        "Ce rendez-vous ne porte aucun courriel : aucun contact ne peut y "
        "être lié. Renvoyez l'appel avec lier_partie false pour confirmer "
        "sans contact. Rien n'a été écrit."
    ),
    rendez_vous_service.AUCUN_CONTACT: (
        "Aucun contact ne porte le courriel du demandeur : rien n'a été "
        "confirmé. Renvoyez l'appel avec lier_partie false, ou créez d'abord "
        "le contact (create_partie)."
    ),
    rendez_vous_service.CONTACTS_ILLISIBLES: (
        "Impossible de vérifier quel contact porte ce courriel — réessayez "
        "dans un moment. Rien n'a été écrit."
    ),
}
# The one post-cancellation sentence reworded for a caller that is a model:
# the client HAS been notified; calling again would call Outlook again.
_REFUS_NON_INSCRIT_MCP = (
    "La réunion Outlook a été annulée et le client prévenu, mais le refus "
    "n'a pas pu être inscrit dans Athéna. N'appelez PAS decide_rendez_vous "
    "de nouveau : la synchronisation Bookings (10 minutes au plus) marquera "
    "la demande « annulée par le client », et la réception pourra alors la "
    "retirer."
)
_DECISION_ALREADY_STORED = (
    "Cette décision était déjà enregistrée : rien n'a été écrit, et "
    "personne n'a été contacté."
)
_NO_CONTACT_LINKED = (
    "Aucun contact n'a été lié. Le formulaire d'ouverture de dossier ne "
    "part pas du connecteur : envoyez-le depuis la Réception au besoin."
)
# The race the service resolves in the refusal's favour: confirmed
# elsewhere while Outlook was being asked to cancel (lot 1b, L7 review).
_REFUSED_OVER_A_CONFIRMATION = (
    "Ce rendez-vous a été confirmé ailleurs (application ou autre appel) "
    "pendant ce refus : le refus l'emporte, et le rendez-vous est retiré "
    "du calendrier et du téléphone."
)
_REFUSED_OVER_A_CONFIRMATION_STALE_PHONE = (
    "Le téléphone n'a pas pu être averti de ce retrait : une copie du "
    "rendez-vous peut y rester. Ne réessayez pas — signalez-le au juriste."
)
# Outlook did NOT confirm the cancellation AND the local refusal failed:
# nothing is recorded, and a retry is the right answer — said in the
# connector's words. The service's warnings are Réception's, written for a
# refusal that WAS recorded (« Rendez-vous refusé, mais… »): relayed here
# they contradicted the refusal they sat in (lot 1b, L7 review).
_RDV_GRAPH_THEN_LOCAL_FAILED = {
    rendez_vous_service.ANNULATION_OUTLOOK_ECHOUEE: (
        "Outlook n'a pas pu annuler la réunion — le client n'a donc pas été "
        "prévenu — et le refus n'a pas pu être inscrit dans Athéna : rien "
        "n'a été fait. Réessayez dans un moment ; la même idempotency_key "
        "convient."
    ),
    rendez_vous_service.ANNULATION_OUTLOOK_INCERTAINE: (
        "L'annulation Outlook a échoué de façon inattendue — elle a PEUT-ÊTRE "
        "eu lieu, et le client été prévenu — et le refus n'a pas pu être "
        "inscrit dans Athéna. Vérifiez la réunion dans Outlook avant de "
        "réessayer."
    ),
}


def _read_rendez_vous(hearing_id: str) -> dict:
    """The request a decision is about, read STRICTLY — a blip must never
    read as « introuvable » about a live reservation."""
    try:
        hearing = hearing_model.get_hearing_strict(hearing_id)
    except Exception:
        raise ToolArgumentError(hearing_model.CONFIRMATION_READ_ERROR)
    if not hearing:
        raise ToolArgumentError(
            f"Rendez-vous introuvable : {hearing_id}. Les demandes en "
            "attente se lisent avec list_hearings (bookings « pending »). "
            "Rien n'a été écrit."
        )
    if hearing.get("source") != "bookings":
        raise ToolArgumentError(
            "Cet événement n'est pas une demande Bookings : "
            "decide_rendez_vous ne s'applique pas — un événement ordinaire "
            "se modifie avec update_hearing. Rien n'a été écrit."
        )
    return hearing


def decide_rendez_vous(args: dict) -> dict:
    return run_write(
        "decide_rendez_vous", args, lambda: _decide_rendez_vous_impl(args)
    )


def _decide_rendez_vous_impl(args: dict) -> dict:
    """Through ``services/rendez_vous`` — the ONE place a Bookings decision
    is taken, Réception's door too, so the two cannot decide two ways.

    What the service guarantees and this handler relies on: the version is
    compared BEFORE the Outlook call (a stale view is refused with nothing
    done, Outlook untouched); the cancellation text is fixed
    (``REFUS_MOTIF`` — no argument here can carry text to the client); the
    local write after a successful cancellation is unconditional, and its
    failure is a warned SUCCESS, never a retryable error — a retry would
    call Outlook again on a cancelled meeting. Such a result is stored
    under the idempotency key (required), so a same-key retry replays it
    instead of re-contacting anyone."""
    hearing_id = (args.get("hearing_id") or "").strip()
    if not hearing_id:
        raise ToolArgumentError("`hearing_id` est requis.")
    action = args.get("action")
    if action not in ("confirmer", "refuser"):
        raise ToolArgumentError(
            "« action » : « confirmer » ou « refuser ». Rien n'a été écrit.")
    if "expected_etag" not in args:
        raise ToolArgumentError(
            "`expected_etag` est requis : prenez-le sur la ligne de la "
            "demande (list_hearings, bookings « pending »). Une décision "
            "part de la version que l'utilisateur a vue — et un refus "
            "prévient le client. Rien n'a été écrit."
        )
    if action == "refuser" and "lier_partie" in args:
        raise ToolArgumentError(
            "`lier_partie` ne s'applique qu'à « confirmer ». Rien n'a été "
            "écrit."
        )
    existing = _read_rendez_vous(hearing_id)
    expected = str(args.get("expected_etag") or "")
    if action == "confirmer":
        written, errors, report = rendez_vous_service.confirmer(
            hearing_id, lier_partie=bool(args.get("lier_partie", True)),
            expected_etag=expected,
        )
    else:
        written, errors, report = rendez_vous_service.refuser(
            hearing_id, expected_etag=expected,
        )
    if errors:
        _raise_if_stale(
            errors, tool="decide_rendez_vous", subject=_RDV_SUBJECT,
            reread=lambda: hearing_model.get_hearing(hearing_id),
        )
        message = " ".join(_RDV_REFUSALS.get(e, e) for e in errors)
        if report.get("graph_attempted"):
            # Outlook was asked and did NOT confirm the cancellation, and
            # the local write failed too: nothing is recorded, and a retry
            # is the right answer (the service's contract). Never the
            # service's own warning, which says « Rendez-vous refusé ».
            message = _RDV_GRAPH_THEN_LOCAL_FAILED.get(
                str(report.get("warning") or ""),
                _RDV_GRAPH_THEN_LOCAL_FAILED[
                    rendez_vous_service.ANNULATION_OUTLOOK_INCERTAINE],
            )
        raise ToolArgumentError(message)
    return _decide_payload(str(action), written or existing, report)


def _decide_payload(action: str, doc: dict, report: dict) -> dict:
    confirming = action == "confirmer"
    changed = bool(report.get("changed"))
    graph_attempted = bool(report.get("graph_attempted"))
    graph_cancelled = bool(report.get("graph_cancelled"))
    local_written = changed if confirming else bool(report.get("local_written"))
    # A refusal concerns no collection — EXCEPT when another tab confirmed
    # the request during the Outlook call: the refusal, written after the
    # cancellation, wins, and the service takes the event back off the
    # phone (tombstone + bump). ctag_bumped false there said « nothing
    # synced » about the one refusal that did (lot 1b, L7 review).
    confirmed_meanwhile = (not confirming) and bool(
        report.get("confirmed_meanwhile"))
    if confirming:
        bumped = changed and bool(report.get("dav_synced"))
    else:
        bumped = confirmed_meanwhile and bool(report.get("dav_left"))
    visible = False
    if bumped:
        dossier = _dossier_for(doc.get("dossier_id"))
        status = dossier.get("status", "") if dossier is not None else None
        visible = status is None or status in ("actif", "en_attente")

    warnings: list[str] = []
    service_warning = str(report.get("warning") or "")
    if service_warning == rendez_vous_service.REFUS_NON_INSCRIT:
        warnings.append(_REFUS_NON_INSCRIT_MCP)
    elif service_warning:
        warnings.append(service_warning)
    if not changed and not graph_attempted:
        warnings.append(_DECISION_ALREADY_STORED)
    if confirming and changed and not report.get("partie_liee"):
        warnings.append(_NO_CONTACT_LINKED)
    if confirmed_meanwhile:
        warnings.append(_REFUSED_OVER_A_CONFIRMATION)
        if not report.get("dav_left"):
            warnings.append(_REFUSED_OVER_A_CONFIRMATION_STALE_PHONE)

    all_day = bool(doc.get("all_day"))
    start = _as_utc(doc.get("start_datetime"))
    return {
        "decided": True,
        "entity_type": "hearing",
        "entity": {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("dossier_id") or "",
            "dossier_file_number": doc.get("dossier_file_number", ""),
            "dossier_title": doc.get("dossier_title", ""),
            "label": doc.get("title", ""),
            "date": date_str(start) if all_day else iso_mtl(start),
            "status": str(doc.get("status") or ""),
            "all_day": all_day,
            "confirmation": str(doc.get("confirmation") or ""),
            "etag": concurrency.etag_of(doc),
        },
        "action": action,
        "changed": changed,
        "partie_liee": bool(report.get("partie_liee")),
        "partie_id": str(report.get("partie_id") or ""),
        "local_written": local_written,
        "graph_attempted": graph_attempted,
        "graph_cancelled": graph_cancelled,
        "client_notified": graph_cancelled,
        "cancellation_message": (
            rendez_vous_service.REFUS_MOTIF if graph_cancelled else None),
        "ctag_bumped": bumped,
        "dav_synced": bumped and visible,
        "warnings": warnings,
    }


# ── 25. create_time_entry (WRITE) ───────────────────────────────────────

def create_time_entry(args: dict) -> dict:
    return run_write(
        "create_time_entry", args,
        lambda: _create_time_entry_impl(args),
    )


def _create_time_entry_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    description = _clean_entity_text(
        args.get("description") or "", "description"
    )
    if not description:
        raise ToolArgumentError("`description` est requise.")
    when = _write_date(args, "date", required=True)
    hours = _clean_hours(args.get("hours"))
    billable = bool(args.get("billable", True))
    rate = args.get("rate_cents")
    if rate is None:
        # The dossier's hourly rate is the natural default — the same one
        # the web form prefills.
        rate = int(dossier.get("hourly_rate") or 0)

    phase, sous_phase = _resolve_phase_pair(args)

    # No provenance TEXT: descriptions print verbatim on invoices, and a
    # provenance sentence would leak into a client-facing billing
    # narrative. The stored created_via field carries it instead.
    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "date": when,
        "description": description,
        "hours": hours,
        "rate": int(rate),
        "billable": billable,
        "phase": phase,
        "sous_phase": sous_phase,
        "invoiced": False,
        "created_via": "mcp",
    }
    _apply_legacy_ref(args, data, "timeentries")

    def _entity(doc: dict) -> dict:
        row = {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("dossier_id", ""),
            "dossier_file_number": doc.get("dossier_file_number", ""),
            "dossier_title": doc.get("dossier_title", ""),
            "label": doc.get("description", ""),
            "date": date_str(doc.get("date")),
            "hours": float(doc.get("hours") or 0),
            "billable": bool(doc.get("billable")),
            "phase": doc.get("phase", ""),
            "sous_phase": doc.get("sous_phase", ""),
            # The etag AS STORED: update_time_entry's `expected_etag` text
            # sends the caller to « the last write result ».
            "etag": concurrency.etag_of(doc),
        }
        _money(row, "rate", doc.get("rate", 0))
        _money(row, "amount", doc.get("amount", 0))
        return row

    entry, errors = time_entry_model.create_time_entry(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "time_entry", _entity(entry), dossier=dossier, dav_exposed=False,
    )


# ── 26. create_expense (WRITE) ──────────────────────────────────────────

def create_expense(args: dict) -> dict:
    return run_write(
        "create_expense", args, lambda: _create_expense_impl(args)
    )


def _create_expense_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    description = _clean_entity_text(
        args.get("description") or "", "description"
    )
    if not description:
        raise ToolArgumentError("`description` est requise.")
    when = _write_date(args, "date", required=True)
    amount = int(args.get("amount_cents") or 0)
    if amount <= 0:
        raise ToolArgumentError(
            "`amount_cents` doit être un montant positif en cents."
        )

    phase, sous_phase = _resolve_phase_pair(args)

    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "date": when,
        "description": description,
        "category": args.get("category") or "autre",
        "amount": amount,
        "taxable": bool(args.get("taxable", True)),
        "phase": phase,
        "sous_phase": sous_phase,
        "invoiced": False,
        "created_via": "mcp",
    }
    _apply_legacy_ref(args, data, "expenses")

    def _entity(doc: dict) -> dict:
        row = {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("dossier_id", ""),
            "dossier_file_number": doc.get("dossier_file_number", ""),
            "dossier_title": doc.get("dossier_title", ""),
            "label": doc.get("description", ""),
            "date": date_str(doc.get("date")),
            "category": doc.get("category", ""),
            "taxable": bool(doc.get("taxable")),
            "phase": doc.get("phase", ""),
            "sous_phase": doc.get("sous_phase", ""),
            "etag": concurrency.etag_of(doc),   # as stored; see create_time_entry
        }
        _money(row, "amount", doc.get("amount", 0))
        return row

    expense, errors = expense_model.create_expense(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "expense", _entity(expense), dossier=dossier, dav_exposed=False,
    )


# ════════════════════════════════════════════════════════════════════════
# WP17 — dossier mutators: fill-only-if-empty + append-only recorders.
# Dossiers are NOT DAV-exposed — no CTag anywhere below.
# ════════════════════════════════════════════════════════════════════════

# The complete_dossier whitelist with each field's coercion kind.
# NOTHING else is addressable — not status, not parties, not labels.
_COMPLETABLE_FIELDS: dict[str, str] = {
    "domaine": "str",
    "action": "str",
    "action_precision": "str",
    "sommaire": "str",
    "mandate_type": "str",
    "court_file_number": "str",
    "prescription_type": "str",
    "fee_type": "str",
    "fee_notes": "str",
    "valeur": "cents",
    "hourly_rate": "cents",
    "flat_fee": "cents",
    "contingency_percent": "basis_points",
    "droit_action_date": "date",
    "date_avis": "date",
    "prise_action_date": "date",
}


# Fields where 0 is a REAL value, not an unset one. A pro bono or aide
# juridique dossier has an hourly rate of zero, and refusing it would both
# block the import and poison every time entry created afterwards —
# create_time_entry defaults rate_cents to the dossier's rate, so the file
# would silently bill at the model default of 300 $/h.
_ZERO_MEANINGFUL = frozenset({"hourly_rate"})


def _coerce_completable(
    field: str,
    kind: str,
    raw: Any,
    *,
    allow_zero: bool = False,
    limit: Optional[int] = None,
):
    if kind == "date":
        d = _parse_iso_date(str(raw), field)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if kind in ("cents", "basis_points"):
        value = int(raw)
        floor = 0 if allow_zero else 1
        if value < floor:
            raise ToolArgumentError(
                f"`{field}` doit être un entier "
                + ("positif ou nul." if allow_zero else "positif.")
            )
        return value
    return _clean_entity_text(str(raw), field, limit)


def _is_unset(current: Any, default: Any) -> bool:
    """« Empty » ≡ absent, empty, or still equal to the model default."""
    if current is None or current == "":
        return True
    return current == default


# The recorders' stale subject. A trust entry regenerates the dossier's
# etag too, so a movement of the client's funds since the read also
# refuses a recorder — the refusal is right (the stored record changed)
# and re-reading is the whole remedy, as for update_dossier.
_DOSSIER_CHANGED = "Ce dossier a été modifié"


# ── 27. complete_dossier (WRITE, fill-only-if-empty) ────────────────────

def complete_dossier(args: dict) -> dict:
    return run_write(
        "complete_dossier", args, lambda: _complete_dossier_impl(args)
    )


def _complete_dossier_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    defaults = dossier_model.field_defaults()

    updates: dict[str, Any] = {}
    conflicts: list[str] = []
    identical: list[str] = []
    for field, kind in _COMPLETABLE_FIELDS.items():
        if field not in args or args[field] in (None, ""):
            continue
        supplied = _coerce_completable(field, kind, args[field])
        current = dossier.get(field, defaults.get(field))
        if not _is_unset(current, defaults.get(field)):
            if current == supplied:
                identical.append(field)     # harmless no-op — skip quietly
            else:
                conflicts.append(field)
            continue
        updates[field] = supplied

    if conflicts:
        # ATOMIC refusal: a partial fill would leave the caller guessing
        # which half happened. « Add missing fields » never overwrites —
        # replacing a non-empty value is update_dossier's job (since lot Q;
        # this text still sent the caller to the application until lot 1b's
        # text sweep).
        raise ToolArgumentError(
            "Champs déjà renseignés (complete_dossier n'écrase jamais) : "
            + ", ".join(sorted(conflicts))
            + ". Rien n'a été écrit. Retirez-les de l'appel ; pour remplacer "
            "une valeur, c'est update_dossier, avec l'accord de l'avocat."
        )
    if not updates:
        raise ToolArgumentError(
            "Aucun champ à compléter : "
            + (
                "les champs fournis portent déjà ces valeurs."
                if identical
                else "fournissez au moins un champ du dictionnaire "
                "complete_dossier."
            )
        )

    # Filling the court file number mirrors the web form's parse step: the
    # judicial metadata derives from the number, and filling one without
    # the other would leave a dossier citing a number its own cards cannot
    # explain. Derived fields obey the same fill-only rule.
    if "court_file_number" in updates and (
        dossier.get("forum_type", "judiciaire") == "judiciaire"
    ):
        derived = _derive_court_metadata(updates["court_file_number"])
        for key, value in derived.items():
            if value in ("", None, False):
                continue
            if _is_unset(dossier.get(key, defaults.get(key)), defaults.get(key)):
                updates[key] = value

    def _payload(doc: dict) -> dict:
        derived_p = dossier_model.derive_prescription(doc)
        return {
            "completed": True,
            "entity_type": "dossier",
            "dossier_id": dossier_id,
            "file_number": doc.get("file_number", ""),
            "title": doc.get("title", ""),
            "fields_set": sorted(updates),
            "fields_already_identical": sorted(identical),
            "prescription_date": date_str(_as_utc(doc.get("prescription_date"))),
            "prescription_status": derived_p["status"],
            "warnings": [],
            # The dossier's etag AS STORED (review correction, lot 0a): a
            # following update_dossier presents it instead of re-reading.
            "dossier_etag": concurrency.etag_of(doc),
        }


    # Compare-and-set against the dossier read above (plan rule 3): the
    # fill-only-if-empty verdict was reached on THAT read, so a value set
    # in between must refuse the fill, never be overwritten by it.
    updated, errors = dossier_model.update_dossier(
        dossier_id, updates, expected_etag=concurrency.etag_of(dossier)
    )
    _raise_if_stale(
        errors, tool="complete_dossier", subject=_DOSSIER_CHANGED,
        reread=lambda: dossier_model.get_dossier(dossier_id),
        readers=("get_dossier",),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _payload(updated)


# ── 28. record_signification (WRITE, append-only) ───────────────────────

def record_signification(args: dict) -> dict:
    return run_write(
        "record_signification", args,
        lambda: _record_signification_impl(args),
    )


def _record_signification_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    # EXPLICIT whitelist entry; the model's _normalize_significations
    # validates partie-on-dossier / mode / date and the supersede chain.
    entry = {
        "partie_id": (args.get("partie_id") or "").strip(),
        "date": (args.get("date") or "").strip(),
        "mode": args.get("mode") or "huissier",
        "huissier_id": (args.get("huissier_id") or "").strip(),
        "pv_document_id": (args.get("pv_document_id") or "").strip(),
        "superseded_by": "",
        "confirmee": bool(args.get("confirmee")),
    }
    existing = [
        dict(s) for s in (dossier.get("significations") or [])
        if isinstance(s, dict)
    ]
    superseded_id = (args.get("supersedes") or "").strip()

    scratch = {**dossier, "significations": existing + [entry]}
    errors = dossier_model._normalize_significations(scratch)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    cleaned = scratch["significations"]
    # The freshly-appended entry is the one whose id none of the EXISTING
    # entries carried.
    known = {s.get("id") for s in existing}
    new_entry = next(s for s in cleaned if s.get("id") not in known)
    if superseded_id:
        # « supersedes » points at the PRIOR signification this one
        # replaces (the corrected-second-PV case) — recorded on the OLD
        # entry as superseded_by, per the model's chain direction.
        target = next(
            (s for s in cleaned if s.get("id") == superseded_id), None
        )
        if target is None:
            raise ToolArgumentError(
                f"Signification à remplacer introuvable : {superseded_id}. "
                "Lisez get_dossier pour les ids des significations."
            )
        target["superseded_by"] = new_entry["id"]

    def _payload(entry_doc: dict) -> dict:
        return _entity_write_result(
            "signification",
            {
                "id": entry_doc.get("id", ""),
                "dossier_id": dossier_id,
                "dossier_file_number": dossier.get("file_number", ""),
                "dossier_title": dossier.get("title", ""),
                "label": dossier_model.SIGNIFICATION_MODE_LABELS.get(
                    entry_doc.get("mode", ""), entry_doc.get("mode", "")
                ),
                "date": date_str(_as_utc(entry_doc.get("date"))),
                "partie_id": entry_doc.get("partie_id", ""),
                "mode": entry_doc.get("mode", ""),
                "confirmee": bool(entry_doc.get("confirmee")),
            },
            dossier=dossier, dav_exposed=False,
            verb="recorded",
        )

    # Compare-and-set against the dossier read above (plan rule 3):
    # `cleaned` is THAT read's register plus the new entry, written back
    # whole — two parallel calls used to each append to the same read, and
    # the second silently erased the first one's signification.
    updated, errors = dossier_model.update_dossier(
        dossier_id, {"significations": cleaned},
        expected_etag=concurrency.etag_of(dossier),
    )
    _raise_if_stale(
        errors, tool="record_signification", subject=_DOSSIER_CHANGED,
        reread=lambda: dossier_model.get_dossier(dossier_id),
        readers=("get_dossier",),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    stored = next(
        (s for s in updated.get("significations", [])
         if s.get("id") == new_entry["id"]),
        new_entry,
    )
    result = _payload(stored)
    # Top-level, never inside the entity (an array entry has no etag).
    result["dossier_etag"] = concurrency.etag_of(updated)
    return result


# ── 29. record_prescription_event (WRITE, append-only) ──────────────────

def record_prescription_event(args: dict) -> dict:
    return run_write(
        "record_prescription_event", args,
        lambda: _record_prescription_event_impl(args),
    )


def _record_prescription_event_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    entry = {
        "type": args.get("type") or "",
        "date": (args.get("date") or "").strip(),
        "end_date": (args.get("end_date") or "").strip(),
        "reference": (args.get("reference") or "").strip(),
        "document_id": (args.get("document_id") or "").strip(),
    }
    existing = [
        dict(e) for e in (dossier.get("prescription_events") or [])
        if isinstance(e, dict)
    ]
    scratch = {
        **dossier,
        "prescription_events": existing + [entry],
    }
    errors = dossier_model._normalize_prescription_events(scratch)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    cleaned = scratch["prescription_events"]
    known = {e.get("id") for e in existing}
    new_entry = next(e for e in cleaned if e.get("id") not in known)

    def _payload(entry_doc: dict, doc_for_derivation: dict) -> dict:
        derived = dossier_model.derive_prescription(doc_for_derivation)
        result = _entity_write_result(
            "prescription_event",
            {
                "id": entry_doc.get("id", ""),
                "dossier_id": dossier_id,
                "dossier_file_number": dossier.get("file_number", ""),
                "dossier_title": dossier.get("title", ""),
                "label": dossier_model.PRESCRIPTION_EVENT_LABELS.get(
                    entry_doc.get("type", ""), entry_doc.get("type", "")
                ),
                "date": date_str(_as_utc(entry_doc.get("date"))),
                "type": entry_doc.get("type", ""),
                "reference": entry_doc.get("reference", ""),
            },
            dossier=dossier, dav_exposed=False,
            verb="recorded",
        )
        # The point of recording the event: what the delay looks like NOW.
        result["prescription_status"] = derived["status"]
        result["prescription_date_effective"] = date_str(
            _as_utc(derived["date_effective"])
        )
        return result

    # Compare-and-set, for the reason record_signification gives: the
    # register is written back whole from this handler's read.
    updated, errors = dossier_model.update_dossier(
        dossier_id, {"prescription_events": cleaned},
        expected_etag=concurrency.etag_of(dossier),
    )
    _raise_if_stale(
        errors, tool="record_prescription_event", subject=_DOSSIER_CHANGED,
        reread=lambda: dossier_model.get_dossier(dossier_id),
        readers=("get_dossier",),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    stored = next(
        (e for e in updated.get("prescription_events", [])
         if e.get("id") == new_entry["id"]),
        new_entry,
    )
    result = _payload(stored, updated)
    result["dossier_etag"] = concurrency.etag_of(updated)
    return result


# ── 33. complete_task (WRITE — the CLOSING status change; reopen_task reopens) ──

# Terminal states plus « en_cours ». `à_faire` is refused: it is a full
# reopen, and update_task clears completed_date on it.
_COMPLETABLE_STATUSES = ("terminée", "annulée", "en_cours")
_TERMINAL_STATUSES = ("terminée", "annulée")


def _linked_step(task: dict) -> Optional[dict]:
    """Best-effort lookup of the protocol step this task is linked from.

    A linked task and its step share a dossier (``create_linked_tasks``
    copies dossier_id, and since lot 1a ``models/task.update_task``
    refuses to move a step-linked task away), so one indexed query finds
    it — no unbounded scan, and zero cost for a « Général » task.
    Best-effort by construction: a task moved between dossiers before that
    rule would evade it (``protocol.find_step_for_task``'s firm-wide
    fallback still reaches it — the MODEL's cascade uses it), which is why
    the result is DISCLOSED as an observation and never presented as a
    guarantee, and why the tool description says where it looks. Lot 1a
    planned to switch this lookup to ``find_step_for_task``; lot 1b kept it
    (the fallback streams every actif protocol of the firm, a second time
    for each completion, and pays it for every « Général » task this
    lookup costs nothing for) — ``reopen_task``, whose feasibility the
    model decides BEFORE the write, is the one that reads through it.
    """
    dossier_id = task.get("dossier_id") or ""
    if not dossier_id:
        return None
    try:
        protocol = protocol_model.get_protocol_for_dossier(
            dossier_id, active_only=True
        )
    except Exception:
        return None
    if not protocol:
        return None
    for step in protocol.get("steps", []) or []:
        if step.get("linked_task_id") == task.get("id"):
            return {"protocol": protocol, "step": step}
    return None


def _reread_step(protocol_id: str, step_id: str) -> tuple[str, bool]:
    """Re-read the step and its protocol AFTER the write; report reality.

    ``_sync_protocol_step`` swallows every exception, so a PREDICTED
    « complété » could be a lie. One keyed read is the difference between
    reporting what happened and reporting what should have happened.
    Returns (step_status, protocol_closed).
    """
    try:
        protocol = protocol_model.get_protocol(protocol_id)
    except Exception:
        return "", False
    if not protocol:
        return "", False
    closed = protocol.get("status") == "complété"
    for step in protocol.get("steps", []) or []:
        if step.get("id") == step_id:
            return step.get("status", ""), closed
    return "", closed


def complete_task(args: dict) -> dict:
    return run_write(
        "complete_task", args, lambda: _complete_task_impl(args)
    )


def _complete_task_impl(args: dict) -> dict:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        raise ToolArgumentError("`task_id` is required.")
    new_status = args.get("status") or "terminée"
    if new_status not in _COMPLETABLE_STATUSES:
        raise ToolArgumentError(
            "`status` must be one of: "
            + ", ".join(_COMPLETABLE_STATUSES)
            + ". Reopening a task to « à_faire » is reopen_task."
        )

    task = task_model.get_task(task_id)
    if not task:
        raise ToolArgumentError(
            f"Tâche introuvable : {task_id}. Vérifiez l'identifiant avec "
            "list_tasks — aucune tâche n'a été modifiée."
        )
    current = task.get("status", "")

    # Already in the requested state: report it and write NOTHING. No model
    # call, so no cascade, no CTag churn — which is what makes a scheduled
    # job safe to replay.
    if current == new_status:
        return _complete_task_payload(
            task, task, new_status, current,
            already=True, effect=_no_effect(),
        )

    # Already in the OTHER terminal state: refuse. Silently converting a
    # cancellation into a completion rewrites what the lawyer decided.
    if current in _TERMINAL_STATUSES and new_status in _TERMINAL_STATUSES:
        raise ToolArgumentError(
            f"Cette tâche est déjà « {current} ». La faire passer à "
            f"« {new_status} » réécrirait une décision : faites-le dans "
            "l'application si c'est voulu. Rien n'a été modifié."
        )

    # Closed → « en_cours » is a REOPEN, and it cascades: the model reverts
    # the linked protocol step (and completed_date). Reopening is its own
    # tool since lot 1b — reopen_task, which refuses BEFORE writing when the
    # step cannot follow and names the cancellation it would undo — so this
    # one stays a closing tool and says where to go.
    if current in _TERMINAL_STATUSES and new_status == "en_cours":
        raise ToolArgumentError(
            f"Cette tâche est close (« {current} ») : la remettre "
            "« en_cours » la rouvrirait — c'est l'outil reopen_task. Rien "
            "n'a été modifié."
        )

    data: dict[str, Any] = {"status": new_status}
    note = (args.get("completion_note") or "").strip()
    if note:
        stamp = f"\n\n*{_STATUS_STAMP[new_status]} par Claude le {_today_mtl()}*\n"
        combined = (task.get("description", "") or "") + stamp + note
        # Checked on the COMBINED string, not on the note alone: the model's
        # _sanitize_data truncates at the 2000-char task ceiling with no
        # exception and no flag, so a silent cut would land on the lawyer's
        # own earlier text.
        data["description"] = _clean_entity_text(combined, "completion_note")

    # The model guard, repeated so the refusal names its field (the dry run
    # this once served left the protocol 2026-08-27): update_task
    # re-validates the WHOLE merged document, so a legacy task carrying an
    # out-of-vocabulary category fails for a reason invisible in the
    # application.
    preview = {**task, **data}
    errors = task_model._validate(preview)
    if errors:
        raise ToolArgumentError("; ".join(errors))

    before = _linked_step(task)

    # Compare-and-set against the task read above (plan rule 3): every
    # verdict here — same state, the other terminal state, a reopen — and
    # the combined description were reached on THAT read. A cancellation
    # made in the application in between used to be silently turned into a
    # completion; it is now refused, and nothing is written (so no cascade).
    updated, errors = task_model.update_task(
        task_id, data, expected_etag=concurrency.etag_of(task)
    )
    _raise_if_stale(
        errors, tool="complete_task", subject="Cette tâche a été modifiée",
        reread=lambda: task_model.get_task(task_id), readers=("list_tasks",),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))

    effect = _no_effect()
    if before is not None:
        effect.update({
            "checked": True,
            "linked_step_found": True,
            "protocol_id": before["protocol"].get("id", ""),
            "step_id": before["step"].get("id", ""),
            "step_title": before["step"].get("title", ""),
            "step_status_before": before["step"].get("status", ""),
        })
        after, closed = _reread_step(
            before["protocol"].get("id", ""), before["step"].get("id", "")
        )
        effect["step_status_after"] = after
        effect["protocol_closed"] = closed
        if after == effect["step_status_before"]:
            effect["note"] = (
                "L'étape liée n'a pas changé d'état — la synchronisation du "
                "modèle avale ses erreurs. Vérifiez le protocole dans "
                "l'application."
            )
    elif task.get("dossier_id"):
        effect["checked"] = True

    return _complete_task_payload(
        task, updated, new_status, current,
        already=False, effect=effect,
    )


_STATUS_STAMP = {
    "terminée": "Complétée",
    "annulée": "Annulée",
    "en_cours": "Mise en cours",
}


def _no_effect() -> dict:
    """The protocol_step_effect object, ALWAYS present.

    Every key is emitted unconditionally (the schema auto-requires them),
    and `checked` says whether the lookup even ran — so an absent cascade
    is never confused with an unexamined one.
    """
    return {
        "checked": False,
        "linked_step_found": False,
        "protocol_id": "",
        "step_id": "",
        "step_title": "",
        "step_status_before": "",
        "step_status_after": "",
        "protocol_closed": False,
        "note": "",
    }


def _complete_task_payload(
    original: dict,
    result: dict,
    new_status: str,
    previous: str,
    *,
    already: bool,
    effect: dict,
) -> dict:
    dossier = None
    dossier_id = result.get("dossier_id") or ""
    if dossier_id:
        dossier = dossier_model.get_dossier(dossier_id)
    # The SAME entity shape every creator emits (_written_entity's core),
    # not a full task row: one contract for every write result.
    entity = {
        "id": result.get("id", ""),
        "dossier_id": result.get("dossier_id") or "",
        "dossier_file_number": result.get("dossier_file_number", ""),
        "dossier_title": result.get("dossier_title", ""),
        "label": result.get("title", ""),
        "date": date_str(result.get("due_date")),
        "status": result.get("status", ""),
        "previous_status": previous,
        "completed_date": iso_mtl(_as_utc(result.get("completed_date"))),
        "is_overdue": _task_row(result).get("is_overdue", False),
    }
    payload = _entity_write_result(
        "task", entity, dossier=dossier, dav_exposed=True,
        # An unchanged task is not a write: no CTag, no cascade, replayable.
        wrote=not already,
        verb="completed", created=False,
    )
    payload["already_completed"] = already
    payload["protocol_step_effect"] = effect
    if already:
        payload["warnings"].append(
            f"La tâche portait déjà le statut « {new_status} » : rien n'a "
            "été modifié."
        )
    if effect.get("protocol_closed"):
        payload["warnings"].append(
            "C'était la dernière étape ouverte : le PROTOCOLE ENTIER est "
            "passé à « complété ». Ses étapes n'apparaîtront plus dans "
            "get_agenda. Si ce n'était pas voulu : reopen_task sur cette "
            "tâche rouvre l'étape et réactive le protocole (ou l'application)."
        )
    return payload


# ════════════════════════════════════════════════════════════════════════
# Lot 1b — the agenda edits: update_task, reopen_task, update_note,
# edit_analyse
# ════════════════════════════════════════════════════════════════════════
#
# Each REPLACES a stored value on a DAV-exposed record (tasks, notes), so
# each reports its CTag bump through _entity_write_result — which runs the
# full relocation (dav.sync.relocation_plan: tombstone old → bump old →
# un-tombstone new → bump new) when the record MOVED to another dossier.
#
# The shared discipline, in this order:
# * ids resolve first — an unknown task, note or dossier is refused, never
#   downgraded (a note filed under « Général » because its dossier id did
#   not resolve is the silent loss create_note already refuses);
# * the model payload is built key by key, never `**args`;
# * a call whose every value is ALREADY stored writes NOTHING — no model
#   call, no etag churn, no CTag bump — and says so (`changed_fields: []`).
#   It is decided before the etag check: a request that changes nothing
#   needs no current version (the set_step_status rule), and it is what
#   makes a replay without an idempotency_key harmless;
# * otherwise the etag the caller names must still be the stored one, and
#   when it names none the handler compare-and-sets on its OWN read
#   (_expected_etag) — every payload here was computed from that read;
# * refusals name the field and never quote content (the span rule).

_TASK_SUBJECT = "Cette tâche a été modifiée"
_NOTE_SUBJECT = "Cette note a été modifiée"
_ANALYSE_SUBJECT = "La théorie de la cause a été modifiée"

_TASK_EDIT_KEYS = (
    "title", "description", "priority", "category", "due_date",
    "phase", "sous_phase", "dossier_id",
)
_NOTE_EDIT_KEYS = ("title", "category", "pinned", "content", "dossier_id")

# The dated provenance lines a REPLACEMENT opens with (plan rule 5 keeps the
# stored `updated_via`; these say it in the text the lawyer reads). Kept as
# (prefix, suffix) pairs so a body read back and re-sent — stamp included —
# is recognised and the stamp REPLACED rather than stacked.
_NOTE_REVISION_STAMP = (
    "*Révisée par Claude le ", " — version précédente conservée*",
)
_BLOC_REVISION_STAMP = (
    "*Bloc révisé par Claude le ", " — version précédente conservée*",
)
_ANALYSE_REWRITE_STAMP = (
    "*Théorie révisée par Claude le ", " — version précédente conservée*",
)
# A stamp line is short; a longer first line that happens to start and end
# like one is the lawyer's text, and is never removed.
_STAMP_LINE_MAX = 160


def _stamp_line(kind: tuple[str, str], day: date) -> str:
    return f"{kind[0]}{format_date_fr(day)}{kind[1]}"


def _without_leading_stamp(text: str, kind: tuple[str, str]) -> str:
    """*text* minus a first line that is an earlier stamp of *kind*.

    So a body read back with get_note and sent again does not pile the
    stamps up at its top. Linear — one partition, no regular expression
    (the CWE-1333 doctrine of utils/analyse_blocs). Anything that is not
    exactly a stamp line is left alone."""
    body = text.lstrip("\r\n")
    first, _sep, rest = body.partition("\n")
    line = first.rstrip("\r").strip()
    prefix, suffix = kind
    if (len(line) <= _STAMP_LINE_MAX and line.startswith(prefix)
            and line.endswith(suffix)):
        return rest.lstrip("\r\n")
    return text


def _dossier_for(dossier_id: Optional[str]) -> Optional[dict]:
    """The dossier a DAV-exposed record lives in, for the closed-dossier
    warning: « Général » when none; ``None`` when the read failed —
    _entity_write_result then claims nothing about visibility."""
    if not dossier_id:
        return _general_scope()
    return dossier_model.get_dossier(dossier_id)


def _refuse_outside_vocabulary(args: dict, key: str, allowed) -> None:
    """The model's vocabulary guard, repeated so the refusal names the
    field — and made stricter: the model lets ``""`` through (« no value »),
    which on an EDIT would erase the stored one."""
    if key in args and args.get(key) not in allowed:
        raise ToolArgumentError(
            f"« {key} » : valeur hors vocabulaire. Valeurs admises : "
            + ", ".join(allowed) + ". Rien n'a été modifié."
        )


def _read_task(task_id: str) -> dict:
    """The task an edit is about — « introuvable » only when the store SAID
    so. ``get_task`` answers ``None`` on a read error too, and « Tâche
    introuvable » about a task that exists would send the caller hunting
    for an id that was right all along."""
    try:
        task = task_model.get_task_strict(task_id)
    except Exception:
        raise ToolArgumentError(
            "Lecture de la tâche impossible — réessayez. Rien n'a été "
            "modifié."
        )
    if not task:
        raise ToolArgumentError(
            f"Tâche introuvable : {task_id}. Vérifiez l'identifiant avec "
            "list_tasks — rien n'a été modifié."
        )
    return task


def _read_note(note_id: str) -> dict:
    """The note an edit is about — the :func:`_read_task` rule."""
    try:
        note = note_model.get_note_strict(note_id)
    except Exception:
        raise ToolArgumentError(
            "Lecture de la note impossible — réessayez. Rien n'a été "
            "modifié."
        )
    if note is None:
        raise ToolArgumentError(
            f"Note introuvable : {note_id}. Utilisez list_notes pour obtenir "
            "un note_id valide. Rien n'a été modifié."
        )
    return note


def _resolve_move(dossier_id: str) -> tuple[str, dict]:
    """The target of a MOVE: ``""`` = « Général », anything else must
    resolve (refused, never downgraded)."""
    return _resolve_write_dossier({"dossier_id": dossier_id}, required=False)


# ── update_task (WRITE) ─────────────────────────────────────────────────

def _task_edit_entity(doc: dict) -> dict:
    return {
        "id": doc.get("id", ""),
        "dossier_id": doc.get("dossier_id") or "",
        "dossier_file_number": doc.get("dossier_file_number", ""),
        "dossier_title": doc.get("dossier_title", ""),
        "label": doc.get("title", ""),
        "date": date_str(doc.get("due_date")),
        "status": doc.get("status", ""),
        "priority": doc.get("priority", ""),
        "category": doc.get("category", ""),
        "phase": doc.get("phase", "") or "",
        "sous_phase": doc.get("sous_phase", "") or "",
        # The record AS STORED after this call — the next edit's etag.
        "etag": concurrency.etag_of(doc),
    }


def _task_changed_fields(existing: dict, data: dict) -> list[str]:
    changed = []
    for key, value in data.items():
        before = existing.get(key)
        if key == "due_date":
            if date_str(before) != date_str(value):
                changed.append(key)
        elif (before or "") != (value or ""):
            changed.append(key)
    return changed


def update_task(args: dict) -> dict:
    return run_write("update_task", args, lambda: _update_task_impl(args))


def _update_task_impl(args: dict) -> dict:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        raise ToolArgumentError("`task_id` est requis.")
    existing = _read_task(task_id)
    if not any(key in args for key in _TASK_EDIT_KEYS):
        raise ToolArgumentError(
            "Aucun champ à modifier : nommez au moins title, description, "
            "priority, category, due_date, phase, sous_phase ou dossier_id. "
            "Le statut se change avec complete_task ou reopen_task."
        )
    _refuse_outside_vocabulary(args, "priority", task_model.VALID_PRIORITIES)
    _refuse_outside_vocabulary(args, "category", task_model.VALID_CATEGORIES)

    # EXPLICIT whitelist, presence-based: an absent key is left alone, a
    # present one replaces. Never `status` — the non-toggle status tools
    # own it (complete_task / reopen_task), and the schema refuses it.
    data: dict[str, Any] = {}
    if "title" in args:
        title = _clean_entity_text(args.get("title") or "", "title")
        if not title:
            raise ToolArgumentError("« title » ne peut pas être vide.")
        data["title"] = title
    if "description" in args:
        data["description"] = _clean_entity_text(
            args.get("description") or "", "description")
    for key in ("priority", "category"):
        if key in args:
            data[key] = args[key]
    if "due_date" in args:
        data["due_date"] = _write_date(args, "due_date", required=False)
    pair = _optional_phase_pair(args)
    if pair is not None:
        data["phase"], data["sous_phase"] = pair

    old_dossier = existing.get("dossier_id") or ""
    new_dossier = (
        (args.get("dossier_id") or "").strip() if "dossier_id" in args
        else old_dossier
    )
    moved = new_dossier != old_dossier
    changed = _task_changed_fields(existing, data)
    if moved:
        changed.append("dossier_id")
    if not changed:
        return _task_edit_payload(
            existing, dossier=_dossier_for(old_dossier), changed=[],
            moved=False, old_dossier=old_dossier, wrote=False,
        )

    expected = _expected_etag(
        args, existing, tool="update_task", subject=_TASK_SUBJECT)
    target: Optional[dict] = None
    if moved:
        new_dossier, target = _resolve_move(new_dossier)
        # Tasks store None for « Général » (notes store ""): the model's
        # convention, which collection_for reads either way. The labels are
        # re-snapshotted from the TARGET — what the phone and the Outlook
        # mirror print.
        data["dossier_id"] = new_dossier or None
        data["dossier_file_number"] = target.get("file_number", "")
        data["dossier_title"] = target.get("title", "")

    # The model re-validates the WHOLE merged document: a legacy task with
    # an out-of-vocabulary value fails for a reason invisible in the app.
    # Said here, naming it, before any write.
    errors = task_model._validate({**existing, **data})
    if errors:
        raise ToolArgumentError("; ".join(errors))

    updated, errors = task_model.update_task(
        task_id, data, expected_etag=expected)
    _raise_if_stale(
        errors, tool="update_task", subject=_TASK_SUBJECT,
        reread=lambda: task_model.get_task(task_id),
    )
    if errors:
        # The step-link rule lives in the MODEL (lot 1a): a task a protocol
        # step links never changes dossier. Its words, naming the field.
        if moved and any(e in (task_model.STEP_LINKED_MOVE_REFUSED,
                               task_model.STEP_LINK_CHECK_FAILED)
                         for e in errors):
            raise ToolArgumentError("`dossier_id` refusé : " + " ".join(errors))
        raise ToolArgumentError("; ".join(errors))

    payload = _task_edit_payload(
        updated, dossier=target if moved else _dossier_for(old_dossier),
        changed=changed, moved=moved, old_dossier=old_dossier, wrote=True,
    )
    related = existing.get("related_note_id")
    if moved and related:
        note = note_model.get_note(related)
        if note is not None and (note.get("dossier_id") or "") != new_dossier:
            payload["warnings"].append(
                "Cette tâche reste liée à une note d'un autre dossier : sur "
                "le téléphone, jtx Board ne montre ce lien parent qu'entre "
                "deux éléments d'une même collection."
            )
    if "due_date" in changed:
        try:
            linked = protocol_model.find_step_for_task(
                task_id, updated.get("dossier_id"))
        except Exception:
            linked = None
        if linked is not None:
            payload["warnings"].append(
                "Cette tâche est liée à une étape de protocole : l'échéance "
                "de l'étape, elle, ne change pas."
            )
    return payload


def _task_edit_payload(
    doc: dict, *, dossier: Optional[dict], changed: list[str], moved: bool,
    old_dossier: str, wrote: bool,
) -> dict:
    payload = _entity_write_result(
        "task", _task_edit_entity(doc), dossier=dossier, dav_exposed=True,
        verb="updated", created=False, wrote=wrote,
        previous_dossier_id=old_dossier,
    )
    payload["changed_fields"] = changed
    payload["moved"] = moved
    if not wrote:
        payload["warnings"].append(
            "Toutes les valeurs envoyées étaient déjà enregistrées : rien "
            "n'a été modifié."
        )
    return payload


# ── reopen_task (WRITE) ─────────────────────────────────────────────────

_REOPEN_TARGETS = ("à_faire", "en_cours")


def _reopen_no_effect() -> dict:
    """The protocol_step_effect object, ALWAYS complete (the schema
    requires every key); ``checked`` says whether a lookup even ran."""
    return {
        "checked": False,
        "linked_step_found": False,
        "protocol_id": "",
        "step_id": "",
        "step_title": "",
        "step_status_before": "",
        "step_status_after": "",
        "protocol_status_before": "",
        "protocol_status_after": "",
        "protocol_reopened": False,
        "note": "",
    }


def _reread_step_state(
    protocol_id: str, step_id: str
) -> Optional[tuple[str, str]]:
    """``(step_status, protocol_status)`` re-read AFTER the write, or
    ``None`` when that re-read produced nothing to report.

    The cascade runs after the task commit and logs rather than raises, so
    a predicted outcome could be false; one keyed read reports what IS.
    ``get_protocol`` answers ``None`` on a read ERROR as well as on an
    absence, and a step it no longer lists has no status to report: both
    are ``None`` here, so the caller says « not re-read » instead of
    reporting an empty status — which a reader would take for « the
    protocol did not reopen »."""
    try:
        protocol = protocol_model.get_protocol(protocol_id)
    except Exception:
        return None
    if not protocol:
        return None
    step = next((s for s in protocol.get("steps", []) or []
                 if s.get("id") == step_id), None)
    if step is None:
        return None
    return step.get("status", ""), protocol.get("status", "")


def reopen_task(args: dict) -> dict:
    return run_write("reopen_task", args, lambda: _reopen_task_impl(args))


def _reopen_task_impl(args: dict) -> dict:
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        raise ToolArgumentError("`task_id` est requis.")
    target = args.get("status") or "à_faire"
    if target not in _REOPEN_TARGETS:
        raise ToolArgumentError(
            "`status` doit être « à_faire » ou « en_cours ». Pour clore une "
            "tâche, utilisez complete_task."
        )
    task = _read_task(task_id)
    current = task.get("status", "")

    # Already there: write NOTHING (no cascade, no CTag) — replayable.
    if current == target:
        return _reopen_payload(task, task, previous=current, already=True,
                               effect=_reopen_no_effect())
    if current == "à_faire":
        raise ToolArgumentError(
            "Cette tâche est déjà ouverte (« à_faire ») : pour la mettre "
            "« en_cours », utilisez complete_task avec status « en_cours ». "
            "Rien n'a été modifié."
        )
    if current not in ("terminée", "annulée", "en_cours"):
        raise ToolArgumentError(
            "Le statut enregistré de cette tâche est hors vocabulaire : "
            "corrigez-la dans l'application. Rien n'a été modifié."
        )
    # A cancellation is a DECISION: undoing it must be asked for in so many
    # words — never inferred from a reopen of « a task ».
    if current == "annulée" and args.get("reopen_cancelled") is not True:
        raise ToolArgumentError(
            "Cette tâche est « annulée » — une décision. La rouvrir exige "
            "`reopen_cancelled: true` : confirmez-le d'abord avec "
            "l'utilisateur. Rien n'a été modifié."
        )

    expected = _expected_etag(
        args, task, tool="reopen_task", subject=_TASK_SUBJECT)
    data = {"status": target}
    errors = task_model._validate({**task, **data})
    if errors:
        raise ToolArgumentError("; ".join(errors))

    # The before-state, for the report. The MODEL decides feasibility
    # (require_step_follow below) — this read only lets the payload say
    # what the cascade found and what it did.
    lookup_failed = False
    try:
        before = protocol_model.find_step_for_task(
            task_id, task.get("dossier_id"))
    except Exception:
        before, lookup_failed = None, True

    # require_step_follow: a linked « complété » step that cannot reopen
    # (protocol suspended, closed by hand, another protocol actif) refuses
    # the reopen BEFORE the task is written — never a task open beside a
    # completed step (plan catalogue, lot 1).
    updated, errors = task_model.update_task(
        task_id, data, expected_etag=expected, require_step_follow=True)
    _raise_if_stale(
        errors, tool="reopen_task", subject=_TASK_SUBJECT,
        reread=lambda: task_model.get_task(task_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))

    effect = _reopen_no_effect()
    if before is not None:
        protocol, step = before
        protocol_id = protocol.get("id", "")
        step_id = step.get("id", "")
        reread = _reread_step_state(protocol_id, step_id)
        step_after, protocol_after = reread if reread is not None else ("", "")
        effect.update({
            "checked": True,
            "linked_step_found": True,
            "protocol_id": protocol_id,
            "step_id": step_id,
            "step_title": step.get("title", ""),
            "step_status_before": step.get("status", ""),
            "step_status_after": step_after,
            "protocol_status_before": protocol.get("status", ""),
            "protocol_status_after": protocol_after,
            "protocol_reopened": (
                protocol.get("status", "") != "actif"
                and protocol_after == "actif"),
        })
        if reread is None:
            effect["note"] = (
                "La tâche est enregistrée, mais l'état de l'étape liée et de son "
                "protocole n'a pas pu être relu après l'écriture : "
                "step_status_after, protocol_status_after et "
                "protocol_reopened ne disent donc rien. Vérifiez le "
                "protocole dans l'application."
            )
        elif step.get("status") == "complété" and step_after == "complété":
            effect["note"] = (
                "L'étape liée est restée « complété » : sa synchronisation "
                "a échoué après l'écriture de la tâche. Vérifiez le "
                "protocole dans l'application."
            )
    elif lookup_failed:
        effect["note"] = (
            "La recherche de l'étape liée n'a pas pu être relue pour ce "
            "rapport : vérifiez le protocole dans l'application."
        )
    else:
        effect["checked"] = True

    return _reopen_payload(task, updated, previous=current, already=False,
                           effect=effect)


def _reopen_payload(
    original: dict, result: dict, *, previous: str, already: bool,
    effect: dict,
) -> dict:
    entity = {
        "id": result.get("id", ""),
        "dossier_id": result.get("dossier_id") or "",
        "dossier_file_number": result.get("dossier_file_number", ""),
        "dossier_title": result.get("dossier_title", ""),
        "label": result.get("title", ""),
        "date": date_str(result.get("due_date")),
        "status": result.get("status", ""),
        "previous_status": previous,
        "completed_date": iso_mtl(_as_utc(result.get("completed_date"))),
        "is_overdue": _task_row(result).get("is_overdue", False),
        "etag": concurrency.etag_of(result),
    }
    payload = _entity_write_result(
        "task", entity, dossier=_dossier_for(result.get("dossier_id")),
        dav_exposed=True, verb="reopened", created=False,
        wrote=not already,
    )
    payload["already_open"] = already
    payload["protocol_step_effect"] = effect
    if already:
        payload["warnings"].append(
            f"La tâche portait déjà le statut « {previous} » : rien n'a été "
            "modifié."
        )
    if effect.get("protocol_reopened"):
        payload["warnings"].append(
            "Le protocole que la clôture automatique avait refermé est de "
            "nouveau « actif » : ses échéances reparaissent dans get_agenda."
        )
    return payload


# ── update_note (WRITE) ─────────────────────────────────────────────────

def _note_edit_entity(doc: dict) -> dict:
    return {
        "id": doc.get("id", ""),
        "dossier_id": doc.get("dossier_id") or "",
        "dossier_file_number": doc.get("dossier_file_number", ""),
        "dossier_title": doc.get("dossier_title", ""),
        "label": doc.get("title", ""),
        "date": iso_mtl(_as_utc(doc.get("created_at"))),
        "category": doc.get("category", ""),
        "pinned": bool(doc.get("pinned")),
        "content_length": len(doc.get("content", "") or ""),
        "etag": concurrency.etag_of(doc),
    }


def _note_changed_fields(existing: dict, data: dict) -> list[str]:
    changed = []
    for key, value in data.items():
        before = existing.get(key)
        if key == "pinned":
            if bool(before) != bool(value):
                changed.append(key)
        elif (before or "") != (value or ""):
            changed.append(key)
    return changed


def _tasks_left_behind(note_id: str, new_dossier: str) -> Optional[int]:
    """How many tasks linked to this note (RELATED-TO) stay out of its new
    collection; ``None`` when that could not be read — never a false 0."""
    try:
        linked = task_model.list_tasks_for_note(note_id)
    except Exception:
        from utils.logging_setup import log_unexpected

        log_unexpected("mcp update_note: linked task count failed",
                       note_id=note_id)
        return None
    return sum(1 for t in linked
               if (t.get("dossier_id") or "") != (new_dossier or ""))


def update_note(args: dict) -> dict:
    return run_write("update_note", args, lambda: _update_note_impl(args))


def _update_note_impl(args: dict) -> dict:
    note_id = (args.get("note_id") or "").strip()
    if not note_id:
        raise ToolArgumentError("`note_id` est requis.")
    existing = _read_note(note_id)
    # The théorie de la cause has its own tool, which keeps its eight
    # headings; replaced wholesale here it could lose them. It also never
    # moves (the MODEL refuses that on every path since lot 1a).
    if existing.get("is_analyse"):
        raise ToolArgumentError(
            "Cette note est la théorie de la cause du dossier : elle se "
            "modifie avec edit_analyse (bloc par bloc ou en entier) et ne "
            "change jamais de dossier. Rien n'a été modifié."
        )
    if not any(key in args for key in _NOTE_EDIT_KEYS):
        raise ToolArgumentError(
            "Aucun champ à modifier : nommez au moins title, category, "
            "pinned, content ou dossier_id."
        )
    # D8: replacing prose demands the version it replaces — the revision
    # snapshot is taken against it, in the same transaction.
    if "content" in args and "expected_etag" not in args:
        raise ToolArgumentError(
            "`expected_etag` est requis pour remplacer le contenu d'une "
            "note : relisez-la avec get_note (ou list_notes) et renvoyez son "
            "etag. Rien n'a été modifié."
        )
    _refuse_outside_vocabulary(args, "category", note_model.VALID_CATEGORIES)

    data: dict[str, Any] = {}
    if "title" in args:
        title = _clean_note_text((args.get("title") or "").strip(), "title")
        if not title:
            raise ToolArgumentError("« title » ne peut pas être vide.")
        data["title"] = title
    if "category" in args:
        data["category"] = args["category"]
    if "pinned" in args:
        data["pinned"] = bool(args.get("pinned"))
    if "content" in args:
        body = _clean_note_text((args.get("content") or "").strip(), "content")
        body = _without_leading_stamp(body, _NOTE_REVISION_STAMP).strip()
        if not body:
            raise ToolArgumentError(
                "« content » ne peut pas être vide (la mention de révision "
                "seule ne fait pas une note)."
            )
        content = f"{_stamp_line(_NOTE_REVISION_STAMP, _today_mtl())}\n\n{body}"
        # Length FIRST — sanitize also truncates, so an over-long body would
        # otherwise trip the chevron check below and report the wrong reason.
        if len(content) > note_model.CONTENT_MAX_LENGTH:
            raise ToolArgumentError(
                f"Le contenu assemblé ({len(content)} caractères, mention de "
                f"révision comprise) dépasse le plafond de "
                f"{note_model.CONTENT_MAX_LENGTH} : raccourcissez-le. Rien "
                "n'a été modifié."
            )
        # Post-condition on the EXACT string stored (the join rule).
        if not _survives_storage(content, note_model.CONTENT_MAX_LENGTH):
            raise ToolArgumentError(
                "Le contenu assemblé ne peut pas être enregistré intact. "
                + _CHEVRON_ADVICE
            )
        data["content"] = content

    old_dossier = existing.get("dossier_id") or ""
    new_dossier = (
        (args.get("dossier_id") or "").strip() if "dossier_id" in args
        else old_dossier
    )
    moved = new_dossier != old_dossier
    changed = _note_changed_fields(existing, data)
    if moved:
        changed.append("dossier_id")
    if not changed:
        payload = _note_edit_payload(
            existing, dossier=_dossier_for(old_dossier), changed=[],
            moved=False, old_dossier=old_dossier, wrote=False,
            revision_id=None, left_behind=0,
        )
        return payload

    expected = _expected_etag(
        args, existing, tool="update_note", subject=_NOTE_SUBJECT)
    target: Optional[dict] = None
    if moved:
        new_dossier, target = _resolve_move(new_dossier)
        data["dossier_id"] = new_dossier
        data["dossier_file_number"] = target.get("file_number", "")
        data["dossier_title"] = target.get("title", "")

    note, errors = note_model.update_note(
        note_id, data, expected_etag=expected,
        revision="content" if "content" in changed else None,
    )
    _raise_if_stale(
        errors, tool="update_note", subject=_NOTE_SUBJECT,
        reread=lambda: note_model.get_note(note_id),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))

    left = _tasks_left_behind(note_id, new_dossier) if moved else 0
    return _note_edit_payload(
        note, dossier=target if moved else _dossier_for(old_dossier),
        changed=changed, moved=moved, old_dossier=old_dossier, wrote=True,
        revision_id=note.get("_revision_id") or None, left_behind=left,
    )


def _note_edit_payload(
    doc: dict, *, dossier: Optional[dict], changed: list[str], moved: bool,
    old_dossier: str, wrote: bool, revision_id: Optional[str],
    left_behind: Optional[int],
) -> dict:
    payload = _entity_write_result(
        "note", _note_edit_entity(doc), dossier=dossier, dav_exposed=True,
        verb="updated", created=False, wrote=wrote,
        previous_dossier_id=old_dossier,
    )
    payload["changed_fields"] = changed
    payload["moved"] = moved
    payload["revision_id"] = revision_id
    payload["linked_tasks_left_behind"] = left_behind
    if not wrote:
        payload["warnings"].append(
            "Toutes les valeurs envoyées étaient déjà enregistrées : rien "
            "n'a été modifié."
        )
    if left_behind is None:
        payload["warnings"].append(
            "Les tâches liées à cette note n'ont pas pu être comptées : "
            "vérifiez-les dans l'application."
        )
    elif left_behind:
        payload["warnings"].append(
            f"{left_behind} tâche(s) liée(s) à cette note reste(nt) dans "
            "leur dossier : sur le téléphone, jtx Board n'affiche plus ce "
            "lien parent."
        )
    return payload


# ── edit_analyse (WRITE) ────────────────────────────────────────────────

def _analyse_operations_text(
    current: str, operations: Any, day: date,
) -> tuple[str, list[dict], str]:
    """``(new content, blocs_changed, revision field)`` of an operation list.

    The locator is ``utils/analyse_blocs`` — it refuses rather than
    guesses, and re-parses its own result (every zone but the target
    byte-identical). A replaced bloc opens with a dated revision line; an
    appended block gets the « Ajouté par Claude » line — never a ``---``,
    which inside a bloc would read as a bloc boundary."""
    if (not isinstance(operations, list)
            or not 1 <= len(operations) <= ANALYSE_OPERATIONS_MAX):
        raise ToolArgumentError(
            f"`operations` : de 1 à {ANALYSE_OPERATIONS_MAX} opérations. "
            "Rien n'a été modifié."
        )
    ops: list[dict] = []
    changed: list[dict] = []
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ToolArgumentError(
                f"`operations[{index}]` doit être un objet {{bloc, mode, "
                "content}. Rien n'a été modifié.")
        bloc, mode, text = op.get("bloc"), op.get("mode"), op.get("content")
        if bloc not in analyse_blocs.ZONE_KEYS:
            raise ToolArgumentError(
                f"`operations[{index}].bloc` : « entete » ou une lettre de A "
                "à H. Rien n'a été modifié.")
        if mode not in analyse_blocs.MODES:
            raise ToolArgumentError(
                f"`operations[{index}].mode` : « replace » ou « append ». "
                "Rien n'a été modifié.")
        if not isinstance(text, str) or len(text) > CONTENT_MAX_CHARS:
            raise ToolArgumentError(
                f"`operations[{index}].content` : du texte, au plus "
                f"{CONTENT_MAX_CHARS} caractères. Rien n'a été modifié.")
        text = _normalize_markdown(text)
        if not _survives_storage(text, note_model.CONTENT_MAX_LENGTH):
            raise ToolArgumentError(
                f"`operations[{index}].content` (bloc {bloc}) contient du "
                "texte entre chevrons qui serait supprimé à "
                f"l'enregistrement. {_CHEVRON_ADVICE}")
        if mode == "replace":
            body = _without_leading_stamp(text, _BLOC_REVISION_STAMP)
            stamp = _stamp_line(_BLOC_REVISION_STAMP, day)
            text = f"{stamp}\n\n{body}" if body.strip() else stamp
        ops.append({"bloc": bloc, "mode": mode, "content": text})
        changed.append({"bloc": bloc, "mode": mode,
                        "chars": len(op.get("content") or "")})
    try:
        new = analyse_blocs.apply_operations(
            current, ops,
            stamp=f"*Ajouté par Claude le {format_date_fr(day)}*",
        )
    except analyse_blocs.BlocStructureError as exc:
        raise ToolArgumentError(f"{exc} Rien n'a été modifié.")
    field = f"bloc:{ops[0]['bloc']}" if len(ops) == 1 else "content"
    return new, changed, field


def _analyse_rewrite_text(full: Any, day: date) -> str:
    """The whole new théorie, validated, with its dated revision line placed
    right after the level-one title (the start of the entête's body)."""
    if not isinstance(full, str) or not full.strip():
        raise ToolArgumentError("« full » ne peut pas être vide.")
    text = _normalize_markdown(full)
    if len(text) > note_model.CONTENT_MAX_LENGTH:
        raise ToolArgumentError(
            f"« full » dépasse le plafond de {note_model.CONTENT_MAX_LENGTH} "
            "caractères. Rien n'a été modifié.")
    if not _survives_storage(text, note_model.CONTENT_MAX_LENGTH):
        raise ToolArgumentError(
            "« full » contient du texte entre chevrons qui serait supprimé à "
            f"l'enregistrement. {_CHEVRON_ADVICE}")
    try:
        parsed = analyse_blocs.validate_full_rewrite(text)
    except analyse_blocs.BlocStructureError as exc:
        raise ToolArgumentError(f"{exc} Rien n'a été modifié.")
    at = parsed.entete.body_start
    head = text[:at]
    rest = _without_leading_stamp(text[at:], _ANALYSE_REWRITE_STAMP)
    rest = rest.lstrip("\r\n")
    if head and not head.endswith(("\n", "\r")):
        head += "\n"
    stamp = _stamp_line(_ANALYSE_REWRITE_STAMP, day)
    new = f"{head}\n{stamp}\n\n{rest}" if head else f"{stamp}\n\n{rest}"
    try:
        analyse_blocs.validate_full_rewrite(new)
    except analyse_blocs.BlocStructureError as exc:
        raise ToolArgumentError(f"{exc} Rien n'a été modifié.")
    return new


def edit_analyse(args: dict) -> dict:
    return run_write("edit_analyse", args, lambda: _edit_analyse_impl(args))


def _edit_analyse_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    operations = args.get("operations")
    full = args.get("full")
    if operations is not None and full is not None:
        raise ToolArgumentError(
            "Donnez `operations` ou `full`, jamais les deux. Rien n'a été "
            "modifié."
        )

    # No operation: the idempotent init. ensure_analyse_note fails CLOSED
    # (a read error or a duplicate writes nothing) and says whether THIS
    # call created the note — the only case that bumps a CTag.
    if operations is None and full is None:
        note, errors, created = note_model.ensure_analyse_note(dossier_id)
        if errors:
            raise ToolArgumentError("; ".join(errors))
        return _analyse_payload(
            note, dossier, mode="created" if created else "found",
            wrote=created, created=created, blocs_changed=[],
            revision_id=None,
        )

    if "expected_etag" not in args:
        raise ToolArgumentError(
            "`expected_etag` est requis pour modifier la théorie de la "
            "cause : relisez-la avec get_note (son id est analyse_note_id "
            "de get_dossier), ou appelez edit_analyse sans operations ni "
            "full pour obtenir son etag. Rien n'a été modifié."
        )
    try:
        note = note_model.find_analyse_note_strict(dossier_id)
    except note_model.AnalyseDuplicateError:
        raise ToolArgumentError(note_model.ANALYSE_DUPLICATE_ERROR)
    except note_model.AnalyseLookupError:
        raise ToolArgumentError(
            "Impossible de lire la théorie de la cause de ce dossier — "
            "réessayez. Rien n'a été modifié."
        )
    if note is None:
        raise ToolArgumentError(
            "Ce dossier n'a pas encore de théorie de la cause : appelez "
            "edit_analyse sans operations ni full pour la créer, puis "
            "modifiez-la avec l'etag rendu. Rien n'a été modifié."
        )
    # The version first: every locator below works on the text this etag
    # names, and an operation aimed from an older read must be re-aimed.
    expected = _expected_etag(
        args, note, tool="edit_analyse", subject=_ANALYSE_SUBJECT)

    current = note.get("content", "") or ""
    day = _today_mtl()
    if operations is not None:
        new, changed, field = _analyse_operations_text(current, operations, day)
        mode = "blocs"
    else:
        new, changed, field = _analyse_rewrite_text(full, day), [], "content:rewrite"
        mode = "full"

    if new == current:
        return _analyse_payload(
            note, dossier, mode="unchanged", wrote=False, created=False,
            blocs_changed=[], revision_id=None,
        )
    # Length FIRST (sanitize truncates), then the post-condition on the
    # exact string stored — the join of an insertion with its neighbours.
    if len(new) > note_model.CONTENT_MAX_LENGTH:
        raise ToolArgumentError(
            f"La théorie de la cause atteindrait {len(new)} caractères, au-"
            f"delà du plafond de {note_model.CONTENT_MAX_LENGTH}. Rien n'a "
            "été modifié."
        )
    if not _survives_storage(new, note_model.CONTENT_MAX_LENGTH):
        raise ToolArgumentError(
            "Combinés, le texte existant et le vôtre contiennent une paire de "
            "chevrons qui ferait disparaître du contenu à l'enregistrement. "
            + _CHEVRON_ADVICE
        )

    # Payload whitelist of ONE key: title, category, is_analyse, dateless
    # and dossier_id are never touched here. The revision snapshot of the
    # WHOLE replaced text commits in the same transaction (plan rule 4).
    updated, errors = note_model.update_note(
        note.get("id", ""), {"content": new}, expected_etag=expected,
        revision=field,
    )
    _raise_if_stale(
        errors, tool="edit_analyse", subject=_ANALYSE_SUBJECT,
        reread=lambda: note_model.get_note(note.get("id", "")),
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _analyse_payload(
        updated, dossier, mode=mode, wrote=True, created=False,
        blocs_changed=changed,
        revision_id=updated.get("_revision_id") or None,
    )


def _analyse_payload(
    note: dict, dossier: Optional[dict], *, mode: str, wrote: bool,
    created: bool, blocs_changed: list[dict], revision_id: Optional[str],
) -> dict:
    content = note.get("content", "") or ""
    entity = {
        "id": note.get("id", ""),
        "dossier_id": note.get("dossier_id") or "",
        "dossier_file_number": note.get("dossier_file_number", ""),
        "dossier_title": note.get("dossier_title", ""),
        "label": note.get("title", ""),
        # A dateless jtx *Note*: it has no operative date.
        "date": None,
        "content_length": len(content),
        "etag": concurrency.etag_of(note),
    }
    payload = _entity_write_result(
        "note", entity, dossier=dossier, dav_exposed=True, verb="edited",
        created=created, wrote=wrote,
    )
    structure = _analyse_structure(content, headings=False)
    payload["mode"] = mode
    payload["structure"] = structure
    payload["blocs_changed"] = blocs_changed
    payload["revision_id"] = revision_id
    if mode == "unchanged":
        payload["warnings"].append(
            "Les opérations ne changeaient rien au texte : rien n'a été "
            "modifié."
        )
    if not structure["editable"]:
        payload["warnings"].append(
            "La théorie de la cause n'a pas ses huit titres « ## Bloc A » à "
            "« ## Bloc H » une fois chacun : toute opération par bloc sera "
            "refusée. Rétablissez-les, ou réécrivez-la en entier avec "
            "`full`."
        )
    return payload


# ════════════════════════════════════════════════════════════════════════
# Lot 1b (L6) — the protocols: create_protocol, update_protocol,
# add_protocol_step, update_protocol_step
# ════════════════════════════════════════════════════════════════════════
#
# Every write goes through services/protocoles.py (lot 1a, L2) — the door
# the web routes use too — so the rules are met where both paths meet
# them: the one-actif rule and the C.p.c. locks (model), the non-toggle
# step status and its cascade (model), the recompute that keeps completed
# steps and truly-confirmed CS dates (model), the linked-task alignment and
# its CTag bumps (service).
#
# A protocol and its steps are NOT DAV-exposed; the TASKS a protocol write
# creates or moves are. Those are the syncs reported here — `ctag_bumped`
# is about the linked tasks, and false when no task was written.
#
# L5's discipline, in this order: ids resolve first, STRICTLY (an
# unreadable protocol is never called missing); the model payload is built
# key by key; a call whose every value is already stored writes nothing and
# says so (decided before the etag check — a request that changes nothing
# needs no current version); the named etag must still be the stored one,
# else the handler compare-and-sets on its own read; the model guards are
# REPEATED so a refusal names its field; refusals never quote content.
# Results carry ids, dates, codes, counts and etags — never a step's notes
# or description (mcp_idempotency stores them 24 h).

_PROTOCOL_SUBJECT = "Ce protocole a été modifié"
_STEP_SUBJECT = "Cette étape a été modifiée"
_PROTOCOL_EDIT_KEYS = ("title", "notes", "court", "start_date", "status")
_STEP_FIELD_KEYS = (
    "deadline_date", "notes", "phase", "sous_phase", "title", "description",
    "cpc_reference",
)
# The C.p.c. text of a template (mandatory) step — the law's.
_STEP_LEGAL_KEYS = ("title", "description", "cpc_reference")
_STEP_TASK_OUTCOMES = ("none",) + protocol_service.ALIGN_OUTCOMES
# A CS template date sent back as it stands stays a SUGGESTION — said, since
# « rien n'a été modifié » alone reads as « already confirmed ».
_SUGGESTION_KEPT = (
    "La date envoyée est celle que l'étape porte déjà : elle reste une "
    "SUGGESTION du gabarit CS, qu'un changement de la date de début "
    "déplacera. Le connecteur ne confirme qu'une date qu'il CHANGE ; la "
    "confirmer telle quelle se fait dans l'application (« Confirmer cette "
    "date »)."
)


def _read_protocol(protocol_id: str) -> dict:
    """The protocol (with its steps) an edit is about — « introuvable »
    only when the store SAID so (the _read_task rule)."""
    try:
        protocol = protocol_model.get_protocol_strict(protocol_id)
    except Exception:
        raise ToolArgumentError(
            "Lecture du protocole impossible — réessayez. Rien n'a été "
            "modifié."
        )
    if protocol is None:
        raise ToolArgumentError(
            f"Protocole introuvable : {protocol_id}. Utilisez "
            "list_protocol_steps pour obtenir un protocol_id valide. Rien "
            "n'a été modifié."
        )
    return protocol


def _protocol_step(protocol: dict, step_id: str) -> dict:
    step = next((s for s in protocol.get("steps") or []
                 if s.get("id") == step_id), None)
    if step is None:
        raise ToolArgumentError(
            f"Étape introuvable dans ce protocole : {step_id}. Utilisez "
            "list_protocol_steps pour obtenir un step_id valide. Rien n'a "
            "été modifié."
        )
    return step


def _inactive_protocol_refusal(protocol: dict) -> ToolArgumentError:
    """The connector's words for the model's ``protocole_non_actif``: the
    way out is a tool here, not the web form's button."""
    status = protocol.get("status", "")
    label = protocol_model.STATUS_LABELS.get(status, status).lower()
    return ToolArgumentError(
        f"Ce protocole est « {label} » : ses étapes ne se modifient pas "
        "tant qu'il n'est pas réactivé (update_protocol, status "
        "« actif »). Rien n'a été écrit."
    )


def _is_inactive_error(errors: list[str]) -> bool:
    """The model's « protocol not actif » refusals — of a step addition or
    field edit (every caller's rule since D17) or of a step status — in any
    status: the protocol may have closed between the handler's read and
    the transaction."""
    messages = set()
    for status in protocol_model.VALID_STATUSES:
        messages.add(protocol_model.inactive_protocol_edit_error(status))
        messages.add(protocol_model._inactive_protocol_error(status))
    return any(e in messages for e in errors)


def _bump_task_collection(dossier_id: Optional[str]) -> bool:
    """Bump the DAV collection a protocol's linked task lives in; return
    whether it succeeded. Guarded: the task write it announces has ALREADY
    committed (the note-writer rule — an exception escaping here would read
    as a retryable failure, and the retry would write twice)."""
    from utils.logging_setup import log_unexpected

    try:
        bump_ctag(collection_for(dossier_id))
        return True
    except Exception:
        log_unexpected("mcp protocol write: task CTag bump failed")
        return False


def _dav_visible(dossier_id: str) -> bool:
    """Whether the dossier's DAV collection is advertised to DavX5. A
    failed read claims nothing (visible) — the _write_result rule."""
    dossier = dossier_model.get_dossier(dossier_id) if dossier_id else None
    if dossier is None:
        return True
    return dossier.get("status", "") in ("actif", "en_attente")


def _step_brief(s: dict, today: date, protocol: dict) -> dict:
    """A step in a WRITE result: ids, dates, codes and the etag — never
    its notes or description."""
    deadline = _as_utc(s.get("deadline_date"))
    return {
        "id": s.get("id", ""),
        "order": s.get("order", 0),
        "title": s.get("title", ""),
        "deadline_date": date_str(deadline),
        "deadline_offset_days": s.get("deadline_offset_days"),
        "status": derive_step_status(s.get("status", ""), deadline,
                                     today=today),
        "mandatory": bool(s.get("mandatory")),
        "deadline_locked": bool(s.get("deadline_locked")),
        "date_is_suggestion": protocol_model.date_needs_confirmation(
            protocol, s),
        "linked_task_id": s.get("linked_task_id") or None,
        **_phase_pair(s),
        "etag": concurrency.etag_of(s),
    }


def _protocol_entity(p: dict) -> dict:
    return {
        "id": p.get("id", ""),
        "dossier_id": p.get("dossier_id") or "",
        "dossier_file_number": p.get("dossier_file_number", ""),
        "dossier_title": p.get("dossier_title", ""),
        "label": p.get("title", ""),
        "date": date_str(_as_utc(p.get("start_date"))),
        "protocol_type": p.get("protocol_type", ""),
        "status": p.get("status", ""),
        "end_date": date_str(_as_utc(p.get("end_date"))),
        "closed_by": p.get("closed_by") or "",
        # The protocol AS STORED after this call — the next edit's etag.
        "etag": concurrency.etag_of(p),
    }


def _step_entity(protocol: dict, s: dict) -> dict:
    return {
        "id": s.get("id", ""),
        "dossier_id": protocol.get("dossier_id") or "",
        "dossier_file_number": protocol.get("dossier_file_number", ""),
        "dossier_title": protocol.get("dossier_title", ""),
        "label": s.get("title", ""),
        "date": date_str(_as_utc(s.get("deadline_date"))),
        "protocol_id": protocol.get("id", ""),
        "status": s.get("status", ""),
        "mandatory": bool(s.get("mandatory")),
        # The step AS STORED after this call — update_protocol_step's
        # next expected_etag.
        "etag": concurrency.etag_of(s),
    }


def _date_valued_changes(before: dict, data: dict, date_keys) -> list[str]:
    """The keys of *data* whose value differs from *before* — dates
    compared as calendar days, strings with ``None`` read as ``""``."""
    changed = []
    for key, value in data.items():
        if key in date_keys:
            if date_str(_as_utc(before.get(key))) != date_str(value):
                changed.append(key)
        elif (before.get(key) or "") != (value or ""):
            changed.append(key)
    return changed


def _reread_protocol(protocol_id: str) -> Optional[dict]:
    """The protocol re-read AFTER a write, or ``None`` — best-effort: the
    write has committed, and a failed re-read is reported, never raised."""
    try:
        return protocol_model.get_protocol(protocol_id)
    except Exception:
        return None


# ── create_protocol (WRITE) ─────────────────────────────────────────────

def create_protocol(args: dict) -> dict:
    return run_write(
        "create_protocol", args, lambda: _create_protocol_impl(args))


def _create_protocol_impl(args: dict) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    ptype = args.get("protocol_type") or ""
    if ptype not in protocol_model.VALID_PROTOCOL_TYPES:
        raise ToolArgumentError(
            "« protocol_type » : valeur hors vocabulaire. Valeurs admises : "
            + ", ".join(protocol_model.VALID_PROTOCOL_TYPES)
            + ". Rien n'a été écrit."
        )
    start = _write_date(args, "start_date", required=True)
    title = ""
    if "title" in args:
        title = _clean_entity_text(args.get("title") or "", "title")
    notes = ""
    if "notes" in args:
        notes = _clean_entity_text(args.get("notes") or "", "notes")

    # The model's two refusals, repeated so each names its field and the
    # way out (the model re-decides both inside its transaction).
    if protocol_model.regime_mismatch(ptype, dossier):
        reasons = protocol_model._regime_errors(dossier_id, ptype) or [
            "Le régime du gabarit ne gouverne pas le tribunal de ce dossier."
        ]
        raise ToolArgumentError(
            "`protocol_type` refusé : " + " ".join(reasons)
            + " Rien n'a été écrit."
        )
    try:
        active = protocol_model._get_active_protocols_strict(dossier_id)
    except Exception:
        raise ToolArgumentError(protocol_model.ACTIVE_CHECK_FAILED)
    if active:
        raise ToolArgumentError(
            "Ce dossier a déjà un protocole actif "
            f"({active[0].get('id', '')}) : un seul peut l'être à la fois. "
            "Suspendez-le ou complétez-le d'abord (update_protocol, "
            "status), puis recommencez. Rien n'a été écrit."
        )

    make_tasks = args.get("create_linked_tasks") is True
    protocol, errors, report = protocol_service.create_protocol(
        dossier, ptype, start, title=title, notes=notes,
        create_linked_tasks=make_tasks,
    )
    if errors:
        if protocol_model.ONE_ACTIVE_ERROR in errors:
            raise ToolArgumentError(
                "`dossier_id` refusé : " + " ".join(errors))
        raise ToolArgumentError("; ".join(errors))

    bumped = False
    if report["tasks_created"]:
        bumped = _bump_task_collection(dossier_id)
    visible = (dossier or {}).get("status", "") in ("actif", "en_attente")
    today = deadlines.today_mtl()
    steps = sorted(protocol.get("steps") or [],
                   key=lambda s: s.get("order", 0))
    payload: dict[str, Any] = {
        "created": True,
        "entity_type": "protocol",
        "entity": _protocol_entity(protocol),
        "steps": [_step_brief(s, today, protocol) for s in steps],
        "tasks_created": report["tasks_created"],
        "tasks_linked": report["tasks_linked"],
        "tasks_failed": report["tasks_failed"],
        "ctag_bumped": bumped,
        "dav_synced": bumped and visible,
        "warnings": [],
    }
    warnings = payload["warnings"]
    if ptype == "cs_ordinaire":
        warnings.append(
            "Protocole CS : les dates des étapes sont des SUGGESTIONS "
            "(date_is_suggestion) tant qu'elles ne sont pas confirmées, et "
            "une suggestion suit la date de début si celle-ci change. Une "
            "date CHANGÉE avec update_protocol_step (deadline_date) est "
            "confirmée ; confirmer une suggestion telle quelle se fait dans "
            "l'application (« Confirmer cette date ») — le connecteur ne le "
            "fait pas."
        )
    if not steps:
        warnings.append(
            "Protocole conventionnel : il n'a aucune étape. Ajoutez-les "
            "avec add_protocol_step."
        )
    if report["tasks_failed"] or report["tasks_linked"] < report["tasks_created"]:
        warnings.append(
            "Le protocole est enregistré, mais certaines tâches liées n'ont "
            "pas pu être créées ou rattachées à leur étape "
            "(linked_task_id null) — vérifiez avec list_protocol_steps. Ne "
            "pas réessayer : le protocole existe."
        )
    if report["tasks_created"] and not bumped:
        warnings.append(
            "Les tâches liées sont enregistrées, mais la synchronisation "
            "DavX5 n'a pas pu être déclenchée : elles apparaîtront sur "
            "l'appareil au prochain changement dans ce dossier. Ne pas "
            "réessayer."
        )
    if report["tasks_created"] and not visible:
        warnings.append(
            f"Le dossier est « {(dossier or {}).get('status', '')} » : les "
            "tâches liées sont enregistrées et visibles dans l'application, "
            "mais les dossiers fermés ou archivés ne sont pas exposés à "
            "DavX5."
        )
    return payload


# ── update_protocol (WRITE) ─────────────────────────────────────────────

def update_protocol(args: dict) -> dict:
    return run_write(
        "update_protocol", args, lambda: _update_protocol_impl(args))


def _update_protocol_impl(args: dict) -> dict:
    protocol_id = (args.get("protocol_id") or "").strip()
    if not protocol_id:
        raise ToolArgumentError("`protocol_id` est requis.")
    existing = _read_protocol(protocol_id)
    if not any(key in args for key in _PROTOCOL_EDIT_KEYS):
        raise ToolArgumentError(
            "Aucun champ à modifier : nommez au moins title, notes, court, "
            "start_date ou status. Les étapes se modifient avec "
            "update_protocol_step."
        )
    _refuse_outside_vocabulary(args, "status", protocol_model.VALID_STATUSES)

    # EXPLICIT whitelist, presence-based.
    data: dict[str, Any] = {}
    if "title" in args:
        title = _clean_entity_text(args.get("title") or "", "title")
        if not title:
            raise ToolArgumentError("« title » ne peut pas être vide.")
        data["title"] = title
    for key in ("notes", "court"):
        if key in args:
            data[key] = _clean_entity_text(args.get(key) or "", key)
    if "start_date" in args:
        if not (args.get("start_date") or "").strip():
            raise ToolArgumentError(
                "« start_date » ne peut pas être vide : un protocole a "
                "toujours une date de début. Rien n'a été modifié."
            )
        data["start_date"] = _write_date(args, "start_date", required=True)
    if "status" in args:
        data["status"] = args["status"]

    changed = _date_valued_changes(existing, data, ("start_date",))
    previous_status = existing.get("status", "")
    if not changed:
        return _protocol_edit_payload(
            existing, previous_status=previous_status, changed=[],
            report=None)

    expected = _expected_etag(
        args, existing, tool="update_protocol", subject=_PROTOCOL_SUBJECT)
    new_status = data.get("status", previous_status)
    if "status" in changed and new_status == "actif":
        # The model re-decides in its transaction; this names the field.
        try:
            others = protocol_model._get_active_protocols_strict(
                existing.get("dossier_id") or "", exclude_id=protocol_id)
        except Exception:
            raise ToolArgumentError(
                "`status` refusé : " + protocol_model.ACTIVE_CHECK_FAILED)
        if others:
            raise ToolArgumentError(
                "`status` « actif » refusé : un autre protocole est actif "
                f"dans ce dossier ({others[0].get('id', '')}) — un seul "
                "peut l'être à la fois. Suspendez-le ou complétez-le "
                "d'abord. Rien n'a été écrit."
            )

    protocol, errors, report = protocol_service.update_protocol(
        protocol_id, {key: data[key] for key in changed},
        expected_etag=expected,
    )
    _raise_if_stale(
        errors, tool="update_protocol", subject=_PROTOCOL_SUBJECT,
        reread=lambda: protocol_model.get_protocol(protocol_id),
    )
    if errors:
        if any(e in (protocol_model.OTHER_ACTIVE_ON_REACTIVATION,
                     protocol_model.ACTIVE_CHECK_FAILED) for e in errors):
            raise ToolArgumentError("`status` refusé : " + " ".join(errors))
        raise ToolArgumentError("; ".join(errors))
    return _protocol_edit_payload(
        protocol, previous_status=previous_status, changed=changed,
        report=report)


def _protocol_edit_payload(
    protocol: dict, *, previous_status: str, changed: list[str],
    report: Optional[dict],
) -> dict:
    """``report`` is the service's, or ``None`` when nothing was written."""
    wrote = report is not None
    report = report or {}
    steps = {s.get("id"): s for s in protocol.get("steps") or []}
    tasks = {t["step_id"]: t for t in report.get("tasks", [])}
    moved = []
    for entry in report.get("moved_steps", []):
        step = steps.get(entry.get("step_id"), {})
        linked = entry.get("linked_task_id") or None
        moved.append({
            "step_id": entry.get("step_id", ""),
            "title": step.get("title", ""),
            "from": date_str(_as_utc(entry.get("old"))),
            "to": date_str(_as_utc(entry.get("new"))),
            "etag": concurrency.etag_of(step),
            "linked_task_id": linked,
            "task_outcome": (tasks.get(entry.get("step_id"), {}).get("outcome")
                             or "none") if linked else "none",
        })
    preserved = [
        {"step_id": e.get("step_id", ""),
         "title": steps.get(e.get("step_id"), {}).get("title", ""),
         "reason": e.get("reason", "")}
        for e in report.get("preserved_steps", [])
    ]
    counts = {key: int(report.get(key, 0) or 0)
              for key in protocol_service.ALIGN_OUTCOMES}
    aligned = [t for t in report.get("tasks", []) if t["outcome"] == "aligned"]
    bumped = bool(aligned) and all(t["ctag_bumped"] for t in aligned)
    visible = _dav_visible(protocol.get("dossier_id") or "") if aligned else True
    status = protocol.get("status", "")
    open_steps = sum(1 for s in steps.values() if s.get("status") != "complété")
    payload: dict[str, Any] = {
        "updated": True,
        "entity_type": "protocol",
        "entity": {**_protocol_entity(protocol),
                   "previous_status": previous_status},
        "changed_fields": changed,
        "recompute": {"moved": moved, "preserved": preserved},
        "linked_tasks": counts,
        "open_steps": open_steps,
        "ctag_bumped": bumped,
        "dav_synced": bumped and visible,
        "warnings": [],
    }
    warnings = payload["warnings"]
    if not wrote:
        warnings.append(
            "Toutes les valeurs envoyées étaient déjà enregistrées : rien "
            "n'a été modifié."
        )
        return payload
    if "status" in changed and status != "actif":
        warnings.append(
            f"Le protocole est maintenant « {status} » : ses étapes quittent "
            "get_agenda, et compléter ou rouvrir une tâche liée ne fait plus "
            "suivre son étape tant qu'il n'est pas réactivé."
        )
        if status == "complété" and open_steps:
            warnings.append(
                f"{open_steps} étape(s) restent ouvertes dans ce protocole "
                "complété."
            )
    if "status" in changed and status == "actif":
        warnings.append(
            "Le protocole est de nouveau actif : ses étapes reparaissent "
            "dans get_agenda."
        )
    if counts["diverged"]:
        warnings.append(
            f"{counts['diverged']} tâche(s) liée(s) gardent leur propre "
            "échéance, modifiée à la main : elles n'ont pas été déplacées."
        )
    if counts["failed"] or counts["missing"]:
        warnings.append(
            f"{counts['failed'] + counts['missing']} tâche(s) liée(s) n'ont "
            "pas pu être mises à jour (ou sont introuvables ou illisibles) — "
            "vérifiez-les avec list_tasks. Le protocole, lui, est enregistré."
        )
    if aligned and not bumped:
        warnings.append(
            "Les tâches liées déplacées sont enregistrées, mais la "
            "synchronisation DavX5 n'a pas pu être déclenchée : elles "
            "apparaîtront sur l'appareil au prochain changement dans ce "
            "dossier. Ne pas réessayer."
        )
    if aligned and bumped and not visible:
        warnings.append(
            "Le dossier est fermé ou archivé : les tâches liées déplacées "
            "sont enregistrées, mais n'apparaîtront pas sur le téléphone."
        )
    return payload


# ── add_protocol_step (WRITE) ───────────────────────────────────────────

def add_protocol_step(args: dict) -> dict:
    return run_write(
        "add_protocol_step", args, lambda: _add_protocol_step_impl(args))


def _add_protocol_step_impl(args: dict) -> dict:
    protocol_id = (args.get("protocol_id") or "").strip()
    if not protocol_id:
        raise ToolArgumentError("`protocol_id` est requis.")
    protocol = _read_protocol(protocol_id)
    if protocol.get("status", "") != "actif":
        raise _inactive_protocol_refusal(protocol)
    title = _clean_entity_text(args.get("title") or "", "title")
    if not title:
        raise ToolArgumentError("« title » ne peut pas être vide.")
    data: dict[str, Any] = {"title": title}
    for key in ("description", "cpc_reference", "notes"):
        if key in args:
            data[key] = _clean_entity_text(args.get(key) or "", key)
    if "deadline_date" in args:
        data["deadline_date"] = _write_date(args, "deadline_date",
                                            required=False)
    phase, sous_phase = _resolve_phase_pair(args)
    if phase or sous_phase:
        data["phase"], data["sous_phase"] = phase, sous_phase

    make_task = args.get("create_linked_task") is True
    step, errors, report = protocol_service.add_step(
        protocol_id, data, create_linked_task=make_task)
    if errors:
        if _is_inactive_error(errors):
            raise _inactive_protocol_refusal(
                _reread_protocol(protocol_id) or protocol)
        raise ToolArgumentError("; ".join(errors))

    dossier_id = protocol.get("dossier_id") or ""
    bumped = _bump_task_collection(dossier_id) if report["task_created"] else False
    after = _reread_protocol(protocol_id)
    today = deadlines.today_mtl()
    payload: dict[str, Any] = {
        "created": True,
        "entity_type": "protocol_step",
        "entity": _step_entity(protocol, step),
        "step": _step_brief(step, today, protocol),
        "protocol_etag": concurrency.etag_of(after) if after else "",
        "linked_task_id": step.get("linked_task_id") or None,
        "task_created": bool(report["task_created"]),
        "ctag_bumped": bumped,
        "dav_synced": bumped and _dav_visible(dossier_id),
        "warnings": [],
    }
    warnings = payload["warnings"]
    if make_task and not report["task_created"]:
        warnings.append(
            "L'étape est enregistrée, mais sa tâche liée n'a pas pu être "
            "créée. Créez-la avec create_task si elle est nécessaire ; ne "
            "réessayez pas cet appel (il dédoublerait l'étape)."
        )
    elif report["task_created"] and not report["task_linked"]:
        warnings.append(
            "L'étape et une tâche sont enregistrées, mais le lien entre les "
            "deux n'a pas pu être posé : la tâche ne suivra pas l'étape. "
            "Ne pas réessayer."
        )
    if report["task_created"] and not bumped:
        warnings.append(
            "La tâche liée est enregistrée, mais la synchronisation DavX5 "
            "n'a pas pu être déclenchée : elle apparaîtra sur l'appareil au "
            "prochain changement dans ce dossier. Ne pas réessayer."
        )
    if after is None:
        warnings.append(
            "L'étape est enregistrée, mais le protocole n'a pas pu être relu "
            "pour en rendre l'etag (protocol_etag vide) : relisez-le avec "
            "list_protocol_steps avant de le modifier."
        )
    return payload


# ── update_protocol_step (WRITE) ────────────────────────────────────────

def update_protocol_step(args: dict) -> dict:
    return run_write(
        "update_protocol_step", args,
        lambda: _update_protocol_step_impl(args),
    )


def _no_status_change() -> dict:
    """The ``status_change`` object, ALWAYS complete (the schema requires
    every key); ``requested`` says whether the call asked for one."""
    return {
        "requested": False,
        "step_status_before": "",
        "step_status_after": "",
        "task_id": None,
        "task_status_before": "",
        "task_status_after": "",
        "task_sync": "none",
        "protocol_status_before": "",
        "protocol_status_after": "",
        "protocol_closed": False,
        "protocol_reopened": False,
        "note": "",
    }


def _no_linked_task(step: dict) -> dict:
    return {"task_id": step.get("linked_task_id") or None,
            "outcome": "none", "ctag_bumped": False}


def _update_protocol_step_impl(args: dict) -> dict:
    protocol_id = (args.get("protocol_id") or "").strip()
    step_id = (args.get("step_id") or "").strip()
    if not protocol_id or not step_id:
        raise ToolArgumentError("`protocol_id` et `step_id` sont requis.")
    protocol = _read_protocol(protocol_id)
    step = _protocol_step(protocol, step_id)
    fields = [key for key in _STEP_FIELD_KEYS if key in args]
    if not fields and "status" not in args:
        raise ToolArgumentError(
            "Aucun champ à modifier : nommez deadline_date, notes, phase, "
            "sous_phase, title, description, cpc_reference — ou status."
        )
    if fields and "status" in args:
        raise ToolArgumentError(
            "`status` ne se combine pas avec une modification de champs : "
            "ce seraient deux écritures distinctes, non atomiques. Modifiez "
            "d'abord les champs, puis le statut dans un second appel (avec "
            "l'etag rendu par le premier). Rien n'a été écrit."
        )
    if "status" in args:
        return _set_protocol_step_status(args, protocol, step)
    return _edit_protocol_step_fields(args, protocol, step)


def _edit_protocol_step_fields(args: dict, protocol: dict, step: dict) -> dict:
    protocol_id, step_id = protocol.get("id", ""), step.get("id", "")
    data: dict[str, Any] = {}
    for key in ("title", "description", "cpc_reference", "notes"):
        if key in args:
            value = _clean_entity_text(args.get(key) or "", key)
            if key == "title" and not value:
                raise ToolArgumentError("« title » ne peut pas être vide.")
            data[key] = value
    if "deadline_date" in args:
        data["deadline_date"] = _write_date(args, "deadline_date",
                                            required=False)
    pair = _optional_phase_pair(args)
    if pair is not None:
        data["phase"], data["sous_phase"] = pair

    # The model's C.p.c. locks, repeated so the refusal names the field.
    if step.get("mandatory"):
        for key in _STEP_LEGAL_KEYS:
            if key in data and (data[key] or "") != (step.get(key) or ""):
                raise ToolArgumentError(
                    f"`{key}` refusé : {protocol_model.LEGAL_TEXT_LOCKED}")
    if ("deadline_date" in data and date_str(data["deadline_date"])
            != date_str(_as_utc(step.get("deadline_date")))):
        if step.get("deadline_locked"):
            raise ToolArgumentError(
                f"`deadline_date` refusé : {protocol_model.DEADLINE_LOCKED} "
                "Rien n'a été enregistré.")
        if data["deadline_date"] is None and step.get("mandatory"):
            raise ToolArgumentError(
                "`deadline_date` refusé : "
                + protocol_model.MANDATORY_DEADLINE_REQUIRED)

    changed = _date_valued_changes(step, data, ("deadline_date",))
    # A CS suggestion sent back UNCHANGED is not confirmed: the model
    # confirms a date only when it changes (lot 1a — a notes save used to
    # « confirm » every date it re-posted), and the connector has no
    # confirmation flag. Silence here would let the caller believe the
    # date is now fixed, until a new start date moves it.
    kept_suggestion = (
        "deadline_date" in data and "deadline_date" not in changed
        and protocol_model.date_needs_confirmation(protocol, step)
    )
    if not changed:
        payload = _step_edit_payload(
            protocol, step, step, changed=[], date_confirmed_now=False,
            linked=_no_linked_task(step), status_change=_no_status_change(),
            wrote=False)
        if kept_suggestion:
            payload["warnings"].append(_SUGGESTION_KEPT)
        return payload
    expected = _expected_etag(
        args, step, tool="update_protocol_step", subject=_STEP_SUBJECT)
    if protocol.get("status", "") != "actif":
        raise _inactive_protocol_refusal(protocol)

    result, errors, report = protocol_service.update_step(
        protocol_id, step_id, data, expected_etag=expected,
    )
    _raise_if_stale(
        errors, tool="update_protocol_step", subject=_STEP_SUBJECT,
        reread=lambda: _stored_step(protocol_id, step_id),
    )
    if errors:
        if _is_inactive_error(errors):
            raise _inactive_protocol_refusal(
                _reread_protocol(protocol_id) or protocol)
        legal = next((k for k in _STEP_LEGAL_KEYS if k in data), "title")
        named = {protocol_model.LEGAL_TEXT_LOCKED: legal,
                 protocol_model.DEADLINE_LOCKED: "deadline_date",
                 protocol_model.MANDATORY_DEADLINE_REQUIRED: "deadline_date"}
        field = next((named[e] for e in errors if e in named), None)
        prefix = f"`{field}` refusé : " if field else ""
        raise ToolArgumentError(prefix + "; ".join(errors))

    entry = next(iter(report.get("tasks") or []), None)
    linked = (
        {"task_id": entry["task_id"], "outcome": entry["outcome"],
         "ctag_bumped": entry["ctag_bumped"]}
        if entry else _no_linked_task(result)
    )
    # Only a CS template date is a suggestion a recompute would move; the
    # model stamps every changed deadline, and « confirmed » is news only
    # there.
    confirmed_at = result.get("date_confirmed_at")
    confirmed_now = (
        protocol.get("protocol_type") == "cs_ordinaire"
        and step.get("deadline_offset_days") is not None
        and bool(confirmed_at)
        and confirmed_at != step.get("date_confirmed_at")
    )
    payload = _step_edit_payload(
        protocol, step, result, changed=changed,
        date_confirmed_now=confirmed_now,
        linked=linked, status_change=_no_status_change(), wrote=True)
    if kept_suggestion:
        payload["warnings"].append(_SUGGESTION_KEPT)
    return payload


def _stored_step(protocol_id: str, step_id: str) -> Optional[dict]:
    protocol = protocol_model.get_protocol(protocol_id) or {}
    return next((s for s in protocol.get("steps") or []
                 if s.get("id") == step_id), None)


def _set_protocol_step_status(args: dict, protocol: dict, step: dict) -> dict:
    protocol_id, step_id = protocol.get("id", ""), step.get("id", "")
    target = args.get("status")
    if target not in protocol_model.STEP_STATUS_TARGETS:
        raise ToolArgumentError(
            "« status » : « complété » ou « à_venir » seulement. Rien n'a "
            "été modifié.")
    before = step.get("status", "")
    # The state it already has writes nothing — no etag needed, no open
    # protocol needed, no cascade (the model's own non-toggle rule).
    if protocol_model._step_already_at(before, target):
        unchanged = {
            **_no_status_change(),
            "requested": True,
            "step_status_before": before,
            "step_status_after": before,
            "task_id": step.get("linked_task_id") or None,
            "protocol_status_before": protocol.get("status", ""),
            "protocol_status_after": protocol.get("status", ""),
            "note": (
                "Rien n'a été écrit : l'étape était déjà dans l'état "
                "demandé (la tâche liée n'a pas été relue)."),
        }
        return _step_edit_payload(
            protocol, step, step, changed=[], date_confirmed_now=False,
            linked=_no_linked_task(step), status_change=unchanged,
            wrote=False, already=target)
    expected = _expected_etag(
        args, step, tool="update_protocol_step", subject=_STEP_SUBJECT)

    # The model's gate, repeated so the refusal names the way out. The
    # model re-decides in its transaction and stays the authority.
    if target == "complété" and protocol.get("status", "") != "actif":
        raise _inactive_protocol_refusal(protocol)
    if target == "à_venir":
        blocked = protocol_model.step_reopen_refusal(protocol, step)
        if blocked is not None:
            message, reason = blocked
            if reason == "protocole_non_actif":
                raise _inactive_protocol_refusal(protocol)
            raise ToolArgumentError("`status` refusé : " + message)

    task_id = step.get("linked_task_id") or None
    task_before = task_model.get_task(task_id) if task_id else None
    result, errors, outcome = protocol_service.set_step_status(
        protocol_id, step_id, target, expected_etag=expected)
    _raise_if_stale(
        errors, tool="update_protocol_step", subject=_STEP_SUBJECT,
        reread=lambda: _stored_step(protocol_id, step_id),
    )
    if errors:
        if _is_inactive_error(errors):
            raise _inactive_protocol_refusal(
                _reread_protocol(protocol_id) or protocol)
        raise ToolArgumentError("`status` refusé : " + " ".join(errors))

    # RE-READ, never predicted: the cascade and the completion check run
    # after the step's commit and report rather than raise.
    after = _reread_protocol(protocol_id)
    task_after = task_model.get_task(task_id) if task_id else None
    step_after = next((s for s in (after or {}).get("steps") or []
                       if s.get("id") == step_id), None)
    protocol_before = protocol.get("status", "")
    protocol_after = (after or {}).get("status", "") if after else ""
    change = {
        "requested": True,
        "step_status_before": before,
        "step_status_after": (step_after or {}).get("status", ""),
        "task_id": task_id,
        "task_status_before": (task_before or {}).get("status", ""),
        "task_status_after": (task_after or {}).get("status", ""),
        "task_sync": outcome.get("task_sync") or "none",
        "protocol_status_before": protocol_before,
        "protocol_status_after": protocol_after,
        "protocol_closed": (
            protocol_before == "actif" and protocol_after == "complété"
            if after else bool(outcome.get("protocol_closed"))),
        "protocol_reopened": (
            protocol_before != "actif" and protocol_after == "actif"
            if after else bool(outcome.get("protocol_reopened"))),
        "note": "",
    }
    if after is None or step_after is None or (task_id and task_after is None):
        change["note"] = (
            "L'étape est enregistrée, mais son état, celui de sa tâche liée "
            "ou celui de son protocole n'a pas pu être relu après "
            "l'écriture (ou la tâche est introuvable) : les valeurs "
            "« after » vides ne disent rien. Vérifiez avec "
            "list_protocol_steps."
        )
    bumped = False
    if outcome.get("task_sync") == "synced":
        # The collection the TASK lives in — « Général » included (a task
        # stored without a dossier, linked before lot 1a refused moving a
        # linked task): the model's own bump used the task's, and an `or`
        # falling back to the protocol's dossier would bump — and report —
        # a collection the task is not in. The protocol's dossier only when
        # the task could not be read at all.
        known = task_after or task_before
        bumped = _bump_task_collection(
            known.get("dossier_id") if known is not None
            else protocol.get("dossier_id"))
    return _step_edit_payload(
        protocol, step, result, changed=["status"], date_confirmed_now=False,
        linked=_no_linked_task(result), status_change=change, wrote=True,
        bumped=bumped, after=after)


def _step_edit_payload(
    protocol: dict, before: dict, step: dict, *, changed: list[str],
    date_confirmed_now: bool, linked: dict, status_change: dict,
    wrote: bool, already: str = "", bumped: Optional[bool] = None,
    after: Any = _UNCHANGED,
) -> dict:
    """*after* — the protocol re-read after the write (the status path has
    it already); re-read here when not given, the stored one on a no-op."""
    dossier_id = protocol.get("dossier_id") or ""
    if after is _UNCHANGED:
        after = _reread_protocol(protocol.get("id", "")) if wrote else protocol
    if bumped is None:
        bumped = bool(linked.get("ctag_bumped"))
    synced = linked.get("outcome") == "aligned" or (
        status_change.get("task_sync") == "synced")
    payload: dict[str, Any] = {
        "updated": True,
        "entity_type": "protocol_step",
        "entity": _step_entity(protocol, step),
        "changed_fields": changed,
        "date_confirmed_now": date_confirmed_now,
        "linked_task": linked,
        "status_change": status_change,
        "protocol_etag": concurrency.etag_of(after) if after else "",
        "ctag_bumped": bumped,
        "dav_synced": bumped and _dav_visible(dossier_id),
        "warnings": [],
    }
    warnings = payload["warnings"]
    if not wrote:
        if already:
            warnings.append(
                f"L'étape est déjà « {before.get('status', '')} » — "
                f"« {already} » ne change rien : rien n'a été modifié."
            )
        else:
            warnings.append(
                "Toutes les valeurs envoyées étaient déjà enregistrées : "
                "rien n'a été modifié."
            )
        return payload
    if status_change.get("protocol_closed"):
        warnings.append(
            "C'était la dernière étape ouverte : le PROTOCOLE ENTIER est "
            "passé à « complété » (fermeture automatique). Ses étapes "
            "n'apparaîtront plus dans get_agenda ; rouvrir une de ses "
            "étapes (status « à_venir ») le réactive."
        )
    if status_change.get("protocol_reopened"):
        warnings.append(
            "Le protocole, fermé automatiquement à sa dernière étape, est de "
            "nouveau actif : ses échéances reparaissent dans get_agenda."
        )
    sync = status_change.get("task_sync")
    if sync == "skipped_cancelled":
        warnings.append(
            "La tâche liée est annulée : elle n'a pas été modifiée.")
    elif sync in ("failed", "missing"):
        warnings.append(
            "La tâche liée n'a pas pu être mise à jour (ou est introuvable "
            "ou illisible) — vérifiez-la avec list_tasks. L'étape, elle, "
            "est enregistrée.")
    outcome = linked.get("outcome")
    if outcome == "diverged":
        warnings.append(
            "La tâche liée garde sa propre échéance, modifiée à la main : "
            "elle n'a pas été déplacée.")
    elif outcome in ("failed", "missing"):
        warnings.append(
            "La tâche liée n'a pas pu suivre la nouvelle échéance (ou est "
            "introuvable ou illisible) — vérifiez-la avec list_tasks.")
    if synced and not bumped:
        warnings.append(
            "La tâche liée est enregistrée, mais la synchronisation DavX5 "
            "n'a pas pu être déclenchée : elle apparaîtra sur l'appareil au "
            "prochain changement dans ce dossier. Ne pas réessayer.")
    if date_confirmed_now:
        warnings.append(
            "Cette date est maintenant CONFIRMÉE : un changement de la date "
            "de début du protocole ne la déplacera plus.")
    if after is None:
        warnings.append(
            "L'étape est enregistrée, mais le protocole n'a pas pu être "
            "relu pour en rendre l'etag (protocol_etag vide).")
    return payload


# ════════════════════════════════════════════════════════════════════════
# Lecture du CONTENU d'un document (2026-08)
# ════════════════════════════════════════════════════════════════════════

# ── 48. get_document_text (read) ────────────────────────────────────────

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PAGE_RANGE_RE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")


def _parse_page_range(args: dict) -> tuple[int, Optional[int]]:
    """"4" = from page 4 onward; "2-6" = that inclusive range; absent = 1.."""
    raw = (args.get("page_range") or "").strip()
    if not raw:
        return 1, None
    match = _PAGE_RANGE_RE.match(raw)
    if not match:
        raise ToolArgumentError(
            f"page_range invalide : « {raw} ». Formats admis : « 4 » "
            "(à partir de la page 4) ou « 2-6 » (plage inclusive, base 1)."
        )
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else None
    if first < 1 or (last is not None and last < first):
        raise ToolArgumentError(
            f"page_range invalide : « {raw} » (base 1, borne de fin ≥ début)."
        )
    return first, last


_UNREADABLE_MESSAGES_FR = {
    "too_large": (
        "Ce document fait {size} — au-delà du plafond de "
        "{cap} pour l'extraction de texte par cet outil. Consultez-le "
        "dans l'application (fiche du document)."
    ),
    "unsupported_type": (
        "Type « {file_type} » : l'extraction de texte couvre le PDF et le "
        ".docx seulement. Consultez ce document dans l'application."
    ),
    "encrypted": (
        "Ce PDF est chiffré (protégé par mot de passe) — son texte ne peut "
        "pas être extrait. Consultez-le dans l'application."
    ),
    "invalid_pdf": (
        "Ce fichier ne se lit pas comme un PDF valide. Consultez-le dans "
        "l'application."
    ),
    "invalid_docx": (
        "Ce fichier ne se lit pas comme un .docx valide. Consultez-le dans "
        "l'application."
    ),
    "download_failed": (
        "Le fichier n'a pas pu être lu depuis le stockage. Réessayez ; si "
        "l'erreur persiste, vérifiez le document dans l'application."
    ),
    "no_storage_path": (
        "La fiche du document ne référence aucun fichier stocké — un "
        "enregistrement incomplet. Vérifiez-le dans l'application."
    ),
}


def _unreadable(doc: dict, reason: str) -> dict:
    size = document_model.format_file_size(int(doc.get("file_size") or 0))
    message = _UNREADABLE_MESSAGES_FR[reason].format(
        size=size,
        cap=document_model.format_file_size(
            document_model.DOCUMENT_TEXT_MAX_BYTES
        ),
        file_type=doc.get("file_type", ""),
    )
    return {
        "found": True,
        "readable": False,
        "document_id": doc.get("id", ""),
        "file_type": doc.get("file_type", ""),
        "reason": reason,
        "file_size_display": size,
        "message": message,
    }


def _docx_segments(paragraphs: list[str], cap: int) -> list[str]:
    """Greedy-pack paragraphs into segments of at most *cap* characters.

    A .docx has no native page concept the stdlib can see, so the
    « pages » of a .docx are these computed segments — deterministic for a
    given document, which is what makes page_range/next_page paging honest.
    A single paragraph longer than the cap is hard-split so every segment
    is ≤ cap by construction.
    """
    segments: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > cap:
            if current:
                segments.append(current)
                current = ""
            segments.append(paragraph[:cap])
            paragraph = paragraph[cap:]
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > cap:
            segments.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        segments.append(current)
    return segments


def get_document_text(args: dict) -> dict:
    document_id = (args.get("document_id") or "").strip()
    first, last = _parse_page_range(args)
    doc = document_model.get_document(document_id)
    if doc is None:
        return {"found": False, "document_id": document_id}

    file_type = doc.get("file_type", "")
    if file_type not in ("application/pdf", _DOCX_MIME):
        return _unreadable(doc, "unsupported_type")

    data, reason = document_model.get_document_bytes(document_id)
    if data is None:
        # `not_found` cannot happen here (the metadata read above succeeded
        # moments ago); everything else maps onto the honest-refusal branch.
        return _unreadable(doc, reason if reason != "not_found" else "download_failed")

    base = {
        "found": True,
        "readable": True,
        "document_id": document_id,
        "display_name": doc.get("display_name", ""),
        "file_type": file_type,
    }

    if file_type == "application/pdf":
        result = pdf_text.extract_pdf_pages(
            data,
            first_page=first,
            last_page=last,
            char_cap=DOCUMENT_TEXT_MAX_CHARS,
        )
        if not result.readable:
            return _unreadable(doc, result.reason)
        return {
            **base,
            "pagination_unit": "page",
            "page_count": result.page_count,
            "pages": [
                {
                    "page": p.page,
                    "text": p.text,
                    "has_text": p.has_text,
                    "page_truncated": p.page_truncated,
                }
                for p in result.pages
            ],
            "pages_without_text": result.pages_without_text,
            "truncated": result.truncated,
            "next_page": result.next_page,
            "warnings": result.warnings,
        }

    # .docx — computed segments (each ≤ the per-call cap by construction, so
    # exactly one segment is returned per call past the first small ones).
    try:
        paragraphs = pdf_text.extract_docx_text(data)
    except pdf_text.DocumentTextError as exc:
        return _unreadable(doc, exc.reason)
    segments = _docx_segments(
        [p for p in paragraphs], DOCUMENT_TEXT_MAX_CHARS
    )
    count = len(segments)
    warnings: list[str] = []
    if first > count:
        window: list[str] = []
        start = first
        warnings.append(f"first_page_beyond_document:{count}")
    else:
        start = first
        window = segments[first - 1 : (last if last is not None else count)]
    pages = []
    remaining = DOCUMENT_TEXT_MAX_CHARS
    stopped_before: Optional[int] = None
    for offset, text in enumerate(window):
        number = start + offset
        if len(text) > remaining:
            stopped_before = number
            break
        remaining -= len(text)
        pages.append(
            {
                "page": number,
                "text": text,
                "has_text": bool(text.strip()),
                "page_truncated": False,
            }
        )
    end_covered = stopped_before - 1 if stopped_before else (
        start + len(window) - 1 if window else first - 1
    )
    requested_end = last if last is not None else count
    truncated = stopped_before is not None and end_covered < min(
        requested_end, count
    )
    next_page = (
        stopped_before
        if truncated
        else (end_covered + 1 if end_covered < count else None)
    )
    return {
        **base,
        "pagination_unit": "segment",
        "page_count": count,
        "pages": pages,
        "pages_without_text": [
            p["page"] for p in pages if not p["has_text"]
        ],
        "truncated": truncated,
        "next_page": next_page,
        "warnings": warnings,
    }


# ── Analyse documentaire (SPEC Phase K §8-9) ─────────────────────────────
#
# ⚠ ÉCART ASSUMÉ avec la §9.2 : elle décrivait `analyser_document`, qui
# ENFILE une tâche et rend « en_attente ». Rien n'enfile ici : le modèle
# analyse, puis ENREGISTRE, synchrone. Le champ garde son `statut` pour
# qu'un pipeline asynchrone futur s'y branche sans migration.

_ANALYSE_ECHO = (
    "nature_detectee", "sous_nature", "famille", "privileges",
    "niveau_protection", "confiance", "confirme", "categorie_precedente",
    "categorie_remplacee", "remplace_un_choix_du_juriste",
    "champs_attendus_absents", "alerte_dispositif_detecte",
    "alerte_renonciation_possible", "analyse_id",
)

# Liste blanche EXPLICITE, jamais `**args` : `record_analyse` écrit un
# document complet, et un `id` ou un `category` transmis corromprait le champ
# sans changer le chemin du document (le piège du lot Q).
_ANALYSE_INPUTS = (
    "sous_nature", "privileges", "resume", "numero_dossier_cour", "tribunal",
    "district_judiciaire", "auteur", "parties_mentionnees",
    "date_document_str", "date_signature_str", "contient_dispositif",
    "dispositif", "indices_protection", "langue_detectee", "confiance",
    "extraction_tronquee",
    # Annexe C — les deux axes du droit de la preuve, plus l'apparence
    # d'original et la qualité de reconnaissance. Ajoutés au SCHÉMA le
    # 2026-08-27 et oubliés ICI : la compréhension de liste plus bas les
    # jetait donc en silence, si bien qu'un champ annoncé au modèle
    # n'atteignait jamais Firestore. Aucune erreur, aucun avertissement —
    # seulement une carte d'analyse incomplète que rien ne reliait à un
    # schéma. La vérification de l'époque portait sur `_analyse_derivee`
    # en direct, c'est-à-dire à côté de la frontière où le défaut vivait.
    "moyen_preuve", "qualification_ecrit", "parait_original",
    "qualite_reconnaissance",
)


def record_document_analysis(args: dict) -> dict:
    return run_write(
        "record_document_analysis",
        args,
        lambda: _record_document_analysis_impl(args),
    )


def _record_document_analysis_impl(args: dict) -> dict:
    document_id = (args.get("document_id") or "").strip()
    existing = document_model.get_document(document_id)
    if existing is None:
        raise ToolArgumentError(
            f"Document introuvable : {document_id}. L'identifiant provient "
            "de list_documents."
        )

    # Construite PAR PRÉSENCE : une clé absente n'entre pas, donc elle ne
    # peut pas écraser. Une clé présente et vide reste une instruction.
    sortie = {k: args[k] for k in _ANALYSE_INPUTS if k in args}

    dossier = None
    if existing.get("dossier_id"):
        dossier = dossier_model.get_dossier(existing["dossier_id"])

    # ⚠ Les gardes du modèle sont rejouées ICI, avant qu'il ne soit
    # atteint, pour que le refus nomme le code fautif — « sous-nature
    # inconnue : X » plutôt qu'une erreur de modèle nue. Un appel EST une
    # écriture — il n'existe aucun aperçu —, donc son refus doit être
    # immédiatement réparable par l'appelant, sans second aller-retour.
    champ, erreurs = document_model._analyse_derivee(
        sortie, document=existing, dossier=dossier
    )
    if erreurs:
        raise ToolArgumentError(" ".join(erreurs))


    updated, erreurs = document_model.record_analyse(
        document_id,
        sortie,
        declenche_par="mcp",
        modele=str(args.get("_modele") or ""),
        dossier=dossier,
    )
    if erreurs or updated is None:
        raise ToolArgumentError(" ".join(erreurs or ["Enregistrement refusé."]))

    stocke = updated.get("analyse") or {}
    # ⚠ La ligne de journal est posée APRÈS un commit, donc elle ne doit
    # JAMAIS pouvoir lever : `endpoint._tools_call` a un `except
    # Exception` de dernier recours, qui rapporterait comme ÉCHOUÉE
    # une écriture déjà commise — après quoi
    # le modèle réessaie et ajoute une SECONDE entrée au journal. C'est
    # le piège que le dépôt documente pour le bump de CTag ; il vaut
    # pour tout ce qui suit un commit. (Vécu le 2026-08-27 : un appel
    # sans son argument `outcome`, et le juriste a lu « échec » sur une
    # analyse parfaitement enregistrée.)
    try:
        from utils.logging_setup import log_mcp_event

        log_mcp_event(
            "mcp_document_analysed",
            "success",
            tool="record_document_analysis",
            document_id=document_id,
            sous_nature=stocke.get("sous_nature", ""),
            niveau_protection=stocke.get("niveau_protection"),
            categorie_remplacee=bool(stocke.get("categorie_remplacee")),
        )
    except Exception:  # jamais au prix de l'écriture
        # Mais jamais en silence non plus : une ligne d'audit qui
        # disparaît sans trace est le second défaut, pas la réparation du
        # premier.
        from utils.logging_setup import log_unexpected

        log_unexpected("mcp_document_analysed logging failed")
    return {
        "recorded": True,
        "document_id": document_id,
        "display_name": updated.get("display_name") or updated.get("filename") or "",
        "category": updated.get("category", ""),
        "category_source": updated.get("category_source", ""),
        "analyse": {k: stocke.get(k) for k in _ANALYSE_ECHO if k in stocke},
        "warnings": _analyse_warnings(stocke, existing),
    }


def _analyse_warnings(champ: dict, existing: dict) -> list[str]:
    """Ce que l'appelant doit lire, en français, hors du champ."""
    out: list[str] = []
    ancienne = str(existing.get("category") or "")
    nouvelle = str(champ.get("nature_detectee") or "")
    if (
        ancienne
        and ancienne != nouvelle
        and str(existing.get("category_source") or "juriste") == "juriste"
    ):
        out.append(
            f"La catégorie « {ancienne} », posée dans l'application, est "
            f"remplacée par « {nouvelle} ». La précédente reste au journal "
            "des analyses."
        )
    absents = champ.get("champs_attendus_absents") or []
    if absents:
        out.append(
            "Mentions attendues absentes : " + ", ".join(absents)
            + ". Une absence est un signal, pas une lacune à combler."
        )
    if champ.get("alerte_dispositif_detecte"):
        out.append(
            "Ce procès-verbal paraît porter le jugement lui-même — un "
            "jugement rendu à l'audience fait courir les délais d'appel. "
            "Signalé, non calculé."
        )
    if champ.get("alerte_renonciation_possible"):
        out.append(
            "Renonciation possible : le document porte des marques d'un "
            "régime protégé alors que sa nature le présume communiqué."
        )
    out.append(
        "Classification PRÉSUMÉE. Elle est visible dans l'application avec "
        "cette mention jusqu'à confirmation par l'avocat, qui est le seul "
        "geste pouvant la lever."
    )
    return out
