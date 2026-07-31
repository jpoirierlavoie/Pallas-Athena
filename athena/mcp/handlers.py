"""The 29 MCP tool handlers — 20 read-only, plus 9 create-only writes.

Each handler takes the validated ``arguments`` dict and returns a
JSON-serializable payload; the endpoint wraps it in the MCP envelope.
Handlers call EXISTING model/util functions only.

**Read handlers must never write to Firestore.** The invariant survives the
write tools in this narrowed form: only the handlers named in
:data:`mcp.tools.WRITE_TOOLS` mutate anything, every one of them is
CREATE-ONLY (a new note/task/hearing/time-entry/expense, an appended
register entry, a fill-only-if-empty dossier field — never an edit, never a
delete), and the writable collections are ``notes``, ``tasks``,
``hearings``, ``timeentries``, ``expenses`` and ``dossiers`` (arrays +
empty fields only). That is why, for example, ``list_protocol_steps``
derives overdue status by date comparison instead of calling
``check_overdue_steps``, which writes. (Note the request path itself does
write outside the tool path: ``bearer.stamp_token_last_used``,
``oauth.touch_client``, and ``mcp/write_support.py``'s idempotency
records.)

**Every note write MUST bump the dossier's CTag.** ``models/note.py`` never
bumps — bumping lives in the caller (``routes/notes.py``,
``dav/dossier_collections.py``). A tool path that writes a note and skips
``bump_ctag(f"dossier:{dossier_id}")`` leaves the note visible in the web UI
while DavX5 silently never re-syncs it: nothing errors, and only the phone
is wrong.

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

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Optional

from dav.sync import bump_ctag, collection_for, remove_tombstone
from mcp.write_support import run_write
from pagination import decode_cursor, encode_cursor, keyset_page
from models import audit_event as audit_event_model
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
from tz import MTL
from utils import deadlines, taxonomie
from utils.format_fr import format_date_fr, format_rate_fr
from utils.recours import PRESCRIPTION_LABELS, compute_class
from utils.taxonomie import DOMAINE_LABELS
from utils.validators import format_phone_display

from mcp.tools import ToolArgumentError, date_str, format_cents, iso_mtl

# Bounded superset size for Python-side post-filtering (§10.1): never more
# than 200 docs fetched per tool call, never a new composite index.
_FETCH_CAP = 200
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


def _stamps(doc: dict) -> dict:
    """The created_at/updated_at pair every row now carries (PA-G05).

    Both are stored on every entity (Architecture Rule 7) — the gap was
    emission-only. True instants → iso_mtl, nullable for pre-Rule-7 legacy
    docs. Note updated_at is NOISY: DAV round-trips, protocol-step syncs
    and bulk folder moves all re-stamp it without content changing."""
    return {
        "created_at": iso_mtl(_as_utc(doc.get("created_at"))),
        "updated_at": iso_mtl(_as_utc(doc.get("updated_at"))),
    }


def _hearing_row(h: dict) -> dict:
    all_day = bool(h.get("all_day"))
    start = _as_utc(h.get("start_datetime"))
    end = _as_utc(h.get("end_datetime"))
    modalite = h.get("modalite", "présentiel")
    return {
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
        **_stamps(h),
    }


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
        "date_confirmed": bool(s.get("date_confirmed")),
        "completed_date": iso_mtl(_as_utc(s.get("completed_date"))),
        "linked_task_id": s.get("linked_task_id"),
        "linked_hearing_id": s.get("linked_hearing_id"),
        "notes": s.get("notes", ""),
        "is_overdue": is_overdue,
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
    row = {
        "id": inv.get("id", ""),
        "invoice_number": inv.get("invoice_number", ""),
        "dossier_id": inv.get("dossier_id", ""),
        "dossier_file_number": inv.get("dossier_file_number", ""),
        "client_name": inv.get("client_name", ""),
        "date": date_str(_as_utc(inv.get("date"))),
        "due_date": date_str(_as_utc(inv.get("due_date"))),
        "status": inv.get("status", ""),
    }
    _money(row, "total", inv.get("total", 0))
    _money(row, "amount_due", inv.get("amount_due", 0))
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

    # The window OPENS at midnight Montréal, not at the instant `now`.
    # Anchoring on the instant dropped a 09:00 hearing at 09:01 from a
    # window whose own `from` claimed the day was included.
    window_start = datetime.combine(today, dtime.min, tzinfo=MTL).astimezone(
        timezone.utc
    )
    raw_hearings = [
        h
        for h in hearing_model.list_hearings_in_range(
            min(window_start, now), cutoff, limit=100
        )
        if h.get("status") != "annulée"
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
            "to": cutoff.astimezone(MTL).date().isoformat(),
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
        rows, _ = dossier_model.list_dossiers_page(limit=_FETCH_CAP)
        wanted = file_number.strip().lower()
        match = next(
            (d for d in rows if d.get("file_number", "").lower() == wanted), None
        )
        d = dossier_model.get_dossier(match["id"]) if match else None
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
            "created_at": iso_mtl(_as_utc(d.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(d.get("updated_at"))),
        }
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

def list_hearings(args: dict) -> dict:
    limit = _limit_arg(args, 25)
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
    payload["window"] = {"from": date_from.isoformat(), "to": date_to.isoformat()}
    payload["next_cursor"] = next_cursor
    return payload


# ── 6. list_notes ───────────────────────────────────────────────────────

def list_notes(args: dict) -> dict:
    limit = _limit_arg(args, 20)
    dossier_id = args.get("dossier_id")
    # category + query are pure model passthrough (models/note.py already
    # filters both, Python-side over its fetch — PA-G08); the enum at the
    # schema gate matters, because the model silently NO-OPS on an unknown
    # category value instead of erroring.
    category = args.get("category")
    search = args.get("query")
    # include_analyse=True: the MCP read paths EXPOSE the « Théorie de la
    # cause » note (read-only — append_to_note refuses it). The model's
    # default would silently hide it from Claude.
    if dossier_id:
        notes = note_model.list_notes(
            dossier_id=dossier_id, category=category, search=search,
            include_analyse=True,
        )
    else:
        # « Général »: notes attached to no dossier. Filtered in Python —
        # the model has no "no dossier" query (see dav/dossier_collections
        # ._collection_members for the same constraint).
        notes = [
            n
            for n in note_model.list_notes(
                category=category, search=search, include_analyse=True
            )
            if not n.get("dossier_id")
        ]
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
    page, truncated, next_offset = _offset_page(notes, args, limit)
    items = [
        {
            "id": n.get("id", ""),
            "title": n.get("title", ""),
            "category": n.get("category", ""),
            "pinned": bool(n.get("pinned")),
            "is_analyse": bool(n.get("is_analyse")),
            "created_at": iso_mtl(_as_utc(n.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(n.get("updated_at"))),
            "content_preview": (n.get("content", "") or "")[:_NOTE_PREVIEW_CHARS],
        }
        for n in page
    ]
    payload = _list_payload(items, truncated)
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
            "created_at": iso_mtl(_as_utc(note.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(note.get("updated_at"))),
        },
    }


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


def list_documents(args: dict) -> dict:
    limit = _limit_arg(args, 25)
    dossier_id = args["dossier_id"]

    kwargs: dict[str, Any] = {
        "dossier_id": dossier_id,
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

    paths = _folder_paths(dossier_id)
    page, truncated, next_offset = _offset_page(docs, args, limit)
    items = []
    for doc in page:
        size = int(doc.get("file_size", 0) or 0)
        items.append(
            {
                "id": doc.get("id", ""),
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
                "description": doc.get("description", ""),
                "tags": doc.get("tags", []),
                **_stamps(doc),
            }
        )
    payload = _list_payload(items, truncated)
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
            "city": p.get("address_city", ""),
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
        "created_at": iso_mtl(_as_utc(p.get("created_at"))),
        "updated_at": iso_mtl(_as_utc(p.get("updated_at"))),
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
        _money(payload, "outstanding", invoice_model.get_outstanding_total())
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


def _bump_note_ctag(dossier_id: str, note_id: str, *, created: bool) -> bool:
    """Bump the dossier collection's CTag; return whether it succeeded.

    Deliberately swallows its own failure. The note is ALREADY committed by
    the time this runs, and letting the exception escape would hit
    ``endpoint._tools_call``'s blanket ``except Exception``, reporting a
    committed write as a failure — the model would retry and duplicate the
    note in a client's file. The caller surfaces the outcome as
    ``dav_synced`` instead.
    """
    scope = collection_for(dossier_id)
    try:
        if created:
            # A recycled id could still carry a tombstone from a previous
            # delete; RFC 6578 requires one response per href, and the
            # sync-collection builder skips a tombstoned id.
            remove_tombstone(scope, note_id)
        bump_ctag(scope)
        return True
    except Exception:
        from utils.logging_setup import log_unexpected

        log_unexpected("mcp note write: ctag bump failed", dossier_id=dossier_id)
        return False


def _write_result(
    note: dict, *, created: bool, dossier: Optional[dict],
    dry_run: bool = False,
) -> dict:
    """Shared success payload for both write tools.

    *dossier* is ``None`` only when the lookup itself failed (append path —
    ``get_dossier`` swallows read errors and returns ``None``). That is NOT
    the same as a closed dossier and must not be reported as one: the note
    exists and carries a dossier_id, so the collection almost certainly
    exists too. Claim nothing about visibility in that case.

    ``dry_run`` skips the CTag bump (nothing was written, nothing must
    sync) and replaces the outcome warnings with the simulation notice.
    """
    dossier_id = note.get("dossier_id", "")
    if dry_run:
        bumped = False
    else:
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
            "created_at": iso_mtl(_as_utc(note.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(note.get("updated_at"))),
        },
        # Two distinct facts, deliberately not collapsed into one: whether
        # the sync trigger actually fired, and whether the phone will ever
        # see the result. A closed dossier bumps fine but stays invisible.
        "ctag_bumped": bumped,
        "dav_synced": bumped and dav_visible,
        "warnings": [],
    }
    if dry_run:
        payload["warnings"].append(
            "Simulation (dry_run) : rien n'a été écrit. Relancez sans "
            "dry_run pour enregistrer."
        )
        return payload
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


# ── 18. create_note (WRITE) ─────────────────────────────────────────────

def create_note(args: dict) -> dict:
    return run_write("create_note", args, lambda dry: _create_note_impl(args, dry))


def _create_note_impl(args: dict, dry_run: bool) -> dict:
    dossier_id = (args.get("dossier_id") or "").strip()
    # An ABSENT dossier_id means « Général ». A SUPPLIED one must resolve:
    # models/note._validate no longer requires a dossier, so a hallucinated
    # UUID would otherwise be silently downgraded to a general note instead
    # of erroring — research filed where nobody will look for it.
    if dossier_id:
        dossier = dossier_model.get_dossier(dossier_id)
        if dossier is None:
            raise ToolArgumentError(
                f"Dossier introuvable : {dossier_id}. Utilisez list_dossiers "
                "ou get_dossier pour obtenir un dossier_id valide. N'omettez "
                "pas dossier_id pour contourner cette erreur : une note sans "
                "dossier va dans « Général »."
            )
    else:
        dossier = _general_scope()

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

    # EXPLICIT whitelist — never `**args`. models/note.create_note honours a
    # caller-supplied `id` and then does an unconditional full-document
    # set(), so a stray `id` would overwrite an existing note outright;
    # `vjournal_uid` and `created_at` are equally passthrough and would
    # corrupt the VJOURNAL (a non-datetime created_at drops CREATED, the
    # documented jtx Board NOT-NULL crash).
    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "title": title,
        "content": content,
        "category": category,
        "pinned": False,
    }
    if dry_run:
        # Everything above ran — resolution, chevron checks, assembly. The
        # preview carries the exact would-be content length; no id exists
        # because nothing was written.
        preview = {**data, "id": "", "created_at": None, "updated_at": None}
        return _write_result(
            preview, created=True, dossier=dossier, dry_run=True
        )
    note, errors = note_model.create_note(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _write_result(note, created=True, dossier=dossier)


# ── 19. append_to_note (WRITE) ──────────────────────────────────────────

def append_to_note(args: dict) -> dict:
    return run_write(
        "append_to_note", args, lambda dry: _append_to_note_impl(args, dry)
    )


def _append_to_note_impl(args: dict, dry_run: bool) -> dict:
    note_id = (args.get("note_id") or "").strip()
    existing = note_model.get_note(note_id)
    if existing is None:
        raise ToolArgumentError(
            f"Note introuvable : {note_id}. Utilisez list_notes pour obtenir "
            "un note_id valide."
        )
    # The « Théorie de la cause » note is read-only through the connector:
    # it is the lawyer's structured working analysis, edited only in the
    # app's Analyse sheet. Message describes, never quotes (Cloud Trace).
    if existing.get("is_analyse"):
        raise ToolArgumentError(
            "Cette note est la théorie de la cause du dossier — en lecture "
            "seule via le connecteur. Elle se modifie dans l'application "
            "(feuille « Analyse » du dossier). Pour consigner des recherches, "
            "créez ou complétez une autre note du dossier."
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
    if dry_run:
        preview = {**existing, "content": combined}
        result = _write_result(
            preview, created=False, dossier=dossier, dry_run=True
        )
        result["appended_chars"] = len(block)
        return result
    # Whitelist of exactly one field: a stray dossier_id in the update would
    # move the note between dossiers — and between DAV collections, leaving
    # the origin collection un-bumped.
    note, errors = note_model.update_note(note_id, {"content": combined})
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


def _clean_entity_text(raw: str, field: str) -> str:
    """Length-then-chevron refusal at the task/hearing field ceiling."""
    value = (raw or "").strip()
    if len(value) > _ENTITY_FIELD_MAX:
        raise ToolArgumentError(
            f"« {field} » dépasse {_ENTITY_FIELD_MAX} caractères — il serait "
            "tronqué silencieusement à l'enregistrement. Raccourcissez, ou "
            "mettez le détail dans une note (create_note)."
        )
    if not _survives_storage(value, _ENTITY_FIELD_MAX):
        raise ToolArgumentError(
            f"« {field} » contient du texte entre chevrons qui serait "
            f"supprimé à l'enregistrement. {_CHEVRON_ADVICE}"
        )
    return value


def _resolve_write_dossier(
    args: dict, *, required: bool
) -> tuple[str, dict]:
    """Resolve the write target dossier; refuse an unknown id, never
    downgrade (the create_note rule). ``required=False`` allows the
    « Général » fallback for agenda entities."""
    dossier_id = (args.get("dossier_id") or "").strip()
    if dossier_id:
        dossier = dossier_model.get_dossier(dossier_id)
        if dossier is None:
            raise ToolArgumentError(
                f"Dossier introuvable : {dossier_id}. Utilisez list_dossiers "
                "ou get_dossier pour obtenir un dossier_id valide."
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


def _entity_write_result(
    entity_type: str,
    entity: dict,
    *,
    dossier: Optional[dict],
    dav_exposed: bool,
    dry_run: bool,
    verb: str = "created",
) -> dict:
    """Success payload for the WP16 creators and WP17 recorders.

    DAV-exposed entities (task, hearing) carry ctag_bumped/dav_synced with
    the exact semantics of the note tools; time entries, expenses and
    dossier-array additions are not DAV-exposed and deliberately do NOT
    fake those keys.
    """
    payload: dict[str, Any] = {
        verb: True,
        "entity_type": entity_type,
        "entity": entity,
        "warnings": [],
    }
    if dav_exposed:
        if dry_run:
            bumped = False
        else:
            bumped = _bump_note_ctag(
                entity.get("dossier_id") or "", entity.get("id", ""),
                created=True,
            )
        status = dossier.get("status", "") if dossier is not None else None
        dav_visible = status is None or status in ("actif", "en_attente")
        payload["ctag_bumped"] = bumped
        payload["dav_synced"] = bumped and dav_visible
        if not dry_run and not bumped:
            payload["warnings"].append(
                "L'écriture est enregistrée, mais la synchronisation DavX5 "
                "n'a pas pu être déclenchée. Elle apparaîtra sur l'appareil "
                "au prochain changement dans ce dossier. Ne pas réessayer."
            )
        if not dry_run and not dav_visible:
            payload["warnings"].append(
                f"Le dossier est « {status} » : l'entrée est enregistrée et "
                "visible dans l'application, mais les dossiers fermés ou "
                "archivés ne sont pas exposés à DavX5 — elle n'apparaîtra "
                "pas sur le téléphone."
            )
    if dry_run:
        payload["warnings"].append(
            "Simulation (dry_run) : rien n'a été écrit. Relancez sans "
            "dry_run pour enregistrer."
        )
    return payload


# ── 23. create_task (WRITE) ─────────────────────────────────────────────

def create_task(args: dict) -> dict:
    return run_write("create_task", args, lambda dry: _create_task_impl(args, dry))


def _create_task_impl(args: dict, dry_run: bool) -> dict:
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
        }

    if dry_run:
        return _entity_write_result(
            "task", _entity({**data, "id": ""}),
            dossier=dossier, dav_exposed=True, dry_run=True,
        )
    task, errors = task_model.create_task(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "task", _entity(task), dossier=dossier, dav_exposed=True,
        dry_run=False,
    )


# ── 24. create_hearing (WRITE) ──────────────────────────────────────────

def create_hearing(args: dict) -> dict:
    return run_write(
        "create_hearing", args, lambda dry: _create_hearing_impl(args, dry)
    )


def _parse_hhmm(raw: str, name: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(raw.strip(), "%H:%M")
        return parsed.hour, parsed.minute
    except (ValueError, AttributeError):
        raise ToolArgumentError(f"`{name}` doit être une heure HH:MM.")


def _create_hearing_impl(args: dict, dry_run: bool) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=False)
    title = _clean_entity_text(args.get("title") or "", "title")
    if not title:
        raise ToolArgumentError("`title` est requis.")
    hearing_type = args.get("hearing_type") or "rencontre"
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

    # EXPLICIT whitelist — `id`/`vevent_uid` must never be addressable
    # (create_hearing HONOURS a caller-supplied id, the CalDAV-PUT
    # affordance, which here would overwrite an existing event); status
    # stays the model default « à_confirmer » and `confirmation` stays ""
    # (visible everywhere — this is not a Bookings import).
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
        "created_via": "mcp",
    }

    def _entity(doc: dict) -> dict:
        d_all_day = bool(doc.get("all_day"))
        start = _as_utc(doc.get("start_datetime"))
        return {
            "id": doc.get("id", ""),
            "dossier_id": doc.get("dossier_id") or "",
            "dossier_file_number": doc.get("dossier_file_number", ""),
            "dossier_title": doc.get("dossier_title", ""),
            "label": doc.get("title", ""),
            "date": date_str(start) if d_all_day else iso_mtl(start),
            "hearing_type": doc.get("hearing_type", ""),
            "forum": hearing_model.forum_of(doc.get("hearing_type", "")),
            "all_day": d_all_day,
        }

    if dry_run:
        return _entity_write_result(
            "hearing", _entity({**data, "id": ""}),
            dossier=dossier, dav_exposed=True, dry_run=True,
        )
    hearing, errors = hearing_model.create_hearing(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "hearing", _entity(hearing), dossier=dossier, dav_exposed=True,
        dry_run=False,
    )


# ── 25. create_time_entry (WRITE) ───────────────────────────────────────

def create_time_entry(args: dict) -> dict:
    return run_write(
        "create_time_entry", args,
        lambda dry: _create_time_entry_impl(args, dry),
    )


def _create_time_entry_impl(args: dict, dry_run: bool) -> dict:
    dossier_id, dossier = _resolve_write_dossier(args, required=True)
    description = _clean_entity_text(
        args.get("description") or "", "description"
    )
    if not description:
        raise ToolArgumentError("`description` est requise.")
    when = _write_date(args, "date", required=True)
    hours = round(float(args.get("hours") or 0), 1)
    if hours <= 0:
        raise ToolArgumentError("`hours` doit être positif (incréments de 0,1).")
    billable = bool(args.get("billable", True))
    rate = args.get("rate_cents")
    if rate is None:
        # The dossier's hourly rate is the natural default — the same one
        # the web form prefills.
        rate = int(dossier.get("hourly_rate") or 0)

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
        "invoiced": False,
        "created_via": "mcp",
    }

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
        }
        _money(row, "rate", doc.get("rate", 0))
        _money(row, "amount", doc.get("amount", 0))
        return row

    if dry_run:
        preview = {
            **data, "id": "",
            "amount": int(round(hours * int(rate))) if billable else 0,
        }
        return _entity_write_result(
            "time_entry", _entity(preview),
            dossier=dossier, dav_exposed=False, dry_run=True,
        )
    entry, errors = time_entry_model.create_time_entry(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "time_entry", _entity(entry), dossier=dossier, dav_exposed=False,
        dry_run=False,
    )


# ── 26. create_expense (WRITE) ──────────────────────────────────────────

def create_expense(args: dict) -> dict:
    return run_write(
        "create_expense", args, lambda dry: _create_expense_impl(args, dry)
    )


def _create_expense_impl(args: dict, dry_run: bool) -> dict:
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

    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "date": when,
        "description": description,
        "category": args.get("category") or "autre",
        "amount": amount,
        "taxable": bool(args.get("taxable", True)),
        "invoiced": False,
        "created_via": "mcp",
    }

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
        }
        _money(row, "amount", doc.get("amount", 0))
        return row

    if dry_run:
        return _entity_write_result(
            "expense", _entity({**data, "id": ""}),
            dossier=dossier, dav_exposed=False, dry_run=True,
        )
    expense, errors = expense_model.create_expense(data)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _entity_write_result(
        "expense", _entity(expense), dossier=dossier, dav_exposed=False,
        dry_run=False,
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


def _coerce_completable(field: str, kind: str, raw: Any):
    if kind == "date":
        d = _parse_iso_date(str(raw), field)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if kind in ("cents", "basis_points"):
        value = int(raw)
        if value <= 0:
            raise ToolArgumentError(f"`{field}` doit être un entier positif.")
        return value
    return _clean_entity_text(str(raw), field)


def _is_unset(current: Any, default: Any) -> bool:
    """« Empty » ≡ absent, empty, or still equal to the model default."""
    if current is None or current == "":
        return True
    return current == default


# ── 27. complete_dossier (WRITE, fill-only-if-empty) ────────────────────

def complete_dossier(args: dict) -> dict:
    return run_write(
        "complete_dossier", args, lambda dry: _complete_dossier_impl(args, dry)
    )


def _complete_dossier_impl(args: dict, dry_run: bool) -> dict:
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
        # changing a non-empty value is the lawyer's act, in the app.
        raise ToolArgumentError(
            "Champs déjà renseignés (jamais écrasés par le connecteur) : "
            + ", ".join(sorted(conflicts))
            + ". Rien n'a été écrit. Retirez-les de l'appel, ou modifiez-les "
            "dans l'application."
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
        parsed = reference.parse_court_file_number(updates["court_file_number"])
        greffe = parsed.get("greffe") or {}
        juridiction = parsed.get("juridiction") or {}
        forum = parsed.get("forum") or {}
        derived = {
            "greffe_number": parsed.get("greffe_number") or "",
            "juridiction_number": parsed.get("juridiction_number") or "",
            "tribunal": juridiction.get("tribunal") or forum.get("name") or "",
            "competence": juridiction.get("competence") or "",
            "palais_de_justice": greffe.get("palais_de_justice") or "",
            "district_judiciaire": greffe.get("district_judiciaire") or "",
            "is_administrative_tribunal": bool(parsed.get("is_administrative")),
        }
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
        }

    if dry_run:
        preview = {**dossier, **updates}
        dossier_model._apply_prescription_deadline(preview)
        result = _payload(preview)
        result["warnings"].append(
            "Simulation (dry_run) : rien n'a été écrit. Relancez sans "
            "dry_run pour enregistrer."
        )
        return result

    updated, errors = dossier_model.update_dossier(dossier_id, updates)
    if errors:
        raise ToolArgumentError("; ".join(errors))
    return _payload(updated)


# ── 28. record_signification (WRITE, append-only) ───────────────────────

def record_signification(args: dict) -> dict:
    return run_write(
        "record_signification", args,
        lambda dry: _record_signification_impl(args, dry),
    )


def _record_signification_impl(args: dict, dry_run: bool) -> dict:
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
            dossier=dossier, dav_exposed=False, dry_run=dry_run,
            verb="recorded",
        )

    if dry_run:
        return _payload(new_entry)
    updated, errors = dossier_model.update_dossier(
        dossier_id, {"significations": cleaned}
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    stored = next(
        (s for s in updated.get("significations", [])
         if s.get("id") == new_entry["id"]),
        new_entry,
    )
    return _payload(stored)


# ── 29. record_prescription_event (WRITE, append-only) ──────────────────

def record_prescription_event(args: dict) -> dict:
    return run_write(
        "record_prescription_event", args,
        lambda dry: _record_prescription_event_impl(args, dry),
    )


def _record_prescription_event_impl(args: dict, dry_run: bool) -> dict:
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
            dossier=dossier, dav_exposed=False, dry_run=dry_run,
            verb="recorded",
        )
        # The point of recording the event: what the delay looks like NOW.
        result["prescription_status"] = derived["status"]
        result["prescription_date_effective"] = date_str(
            _as_utc(derived["date_effective"])
        )
        return result

    if dry_run:
        return _payload(new_entry, {**dossier, "prescription_events": cleaned})
    updated, errors = dossier_model.update_dossier(
        dossier_id, {"prescription_events": cleaned}
    )
    if errors:
        raise ToolArgumentError("; ".join(errors))
    stored = next(
        (e for e in updated.get("prescription_events", [])
         if e.get("id") == new_entry["id"]),
        new_entry,
    )
    return _payload(stored, updated)
