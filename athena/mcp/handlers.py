"""The 14 read-only MCP tool handlers.

Each handler takes the validated ``arguments`` dict and returns a
JSON-serializable payload; the endpoint wraps it in the MCP envelope.
Handlers call EXISTING model/util functions only — no Firestore writes
happen anywhere on a tool path (no CTag bumping is ever needed).

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

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Optional

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
from tz import MTL
from utils import deadlines, taxonomie
from utils.format_fr import format_rate_fr
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


def _list_payload(items: list, truncated: bool) -> dict:
    return {"items": items, "count": len(items), "truncated": truncated}


def _hearing_row(h: dict) -> dict:
    all_day = bool(h.get("all_day"))
    start = _as_utc(h.get("start_datetime"))
    end = _as_utc(h.get("end_datetime"))
    return {
        "id": h.get("id", ""),
        "title": h.get("title", ""),
        "hearing_type": h.get("hearing_type", ""),
        "start": date_str(start) if all_day else iso_mtl(start),
        "end": date_str(end) if all_day else iso_mtl(end),
        "all_day": all_day,
        "location": h.get("location", ""),
        "court": h.get("court", ""),
        "judge": h.get("judge", ""),
        "status": h.get("status", ""),
        "notes": h.get("notes", ""),
        "dossier_id": h.get("dossier_id", "") or "",
        "dossier_file_number": h.get("dossier_file_number", ""),
        "dossier_title": h.get("dossier_title", ""),
    }


def _task_row(t: dict) -> dict:
    return {
        "id": t.get("id", ""),
        "title": t.get("title", ""),
        "description": t.get("description", ""),
        "priority": t.get("priority", ""),
        "status": t.get("status", ""),
        "category": t.get("category", ""),
        "due_date": date_str(t.get("due_date")),
        "completed_date": iso_mtl(_as_utc(t.get("completed_date"))),
        "dossier_id": t.get("dossier_id") or None,
        "dossier_file_number": t.get("dossier_file_number", ""),
        "dossier_title": t.get("dossier_title", ""),
        "related_note_id": t.get("related_note_id"),
    }


def _step_row(s: dict, now: datetime) -> dict:
    deadline = _as_utc(s.get("deadline_date"))
    # Calendar-date comparison (spec §10.12): a step due TODAY is not
    # overdue yet — deadline_date is a UTC calendar date.
    is_overdue = bool(
        deadline
        and deadline.astimezone(timezone.utc).date() < now.date()
        and s.get("status") != "complété"
    )
    return {
        "id": s.get("id", ""),
        "order": s.get("order", 0),
        "title": s.get("title", ""),
        "description": s.get("description", ""),
        "cpc_reference": s.get("cpc_reference", ""),
        "deadline_date": date_str(deadline),
        "status": s.get("status", ""),
        "mandatory": bool(s.get("mandatory")),
        "deadline_locked": bool(s.get("deadline_locked")),
        "date_confirmed": bool(s.get("date_confirmed")),
        "completed_date": iso_mtl(_as_utc(s.get("completed_date"))),
        "linked_task_id": s.get("linked_task_id"),
        "linked_hearing_id": s.get("linked_hearing_id"),
        "notes": s.get("notes", ""),
        "is_overdue": is_overdue,
    }


def _dossier_row(d: dict) -> dict:
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
        "clients": [c.get("name", "") for c in d.get("clients", [])],
        "opposing_parties": [p.get("name", "") for p in d.get("opposing_parties", [])],
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


def _prescription_row(d: dict, now: datetime) -> dict:
    pdate = _as_utc(d.get("prescription_date"))
    days_remaining: Optional[int] = None
    last_action: Optional[str] = None
    if pdate:
        # Countdown against the user's (Montreal) calendar date — UTC
        # "today" runs ahead of the user's evening by up to 5 hours.
        today = now.astimezone(MTL).date()
        days_remaining = max(0, (pdate.date() - today).days)
        last_action = deadlines.prev_juridical_day(pdate.date()).isoformat()
    return {
        "dossier_id": d.get("id", ""),
        "file_number": d.get("file_number", ""),
        "title": d.get("title", ""),
        "prescription_date": date_str(pdate),
        "days_remaining": days_remaining,
        "last_action_date": last_action,
        "prescription_notes": d.get("prescription_notes", ""),
    }


# ── 1. get_agenda ───────────────────────────────────────────────────────

def get_agenda(args: dict) -> dict:
    days_ahead = int(args.get("days_ahead", 14))
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)

    hearings = [
        _hearing_row(h)
        for h in hearing_model.list_hearings_in_range(now, cutoff, limit=100)
        if h.get("status") != "annulée"
    ]
    urgent_tasks = [
        {
            **_task_row(t),
            # due_date is a UTC calendar date — due today is not overdue.
            "is_overdue": bool(
                t.get("due_date")
                and _as_utc(t["due_date"]).astimezone(timezone.utc).date()
                < now.date()
            ),
        }
        for t in task_model.list_urgent_tasks(cutoff, limit=50)
    ]
    urgent_steps = [
        {
            **_step_row(s, now),
            "protocol_id": s.get("_protocol_id", ""),
            "protocol_title": s.get("_protocol_title", ""),
            "dossier_file_number": s.get("_dossier_file_number", ""),
        }
        for s in protocol_model.list_urgent_steps(cutoff, limit=50)
    ]
    alerts = [
        _prescription_row(d, now)
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
    _money(stats, "outstanding", invoice_model.get_outstanding_total())

    return {
        "window": {
            "from": now.astimezone(MTL).date().isoformat(),
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

    rows, next_cursor = dossier_model.list_dossiers_page(
        status_filter=status, limit=_FETCH_CAP
    )
    if query:
        rows = [
            d
            for d in rows
            if query
            in " ".join(
                [
                    d.get("file_number", ""),
                    d.get("title", ""),
                    d.get("court_file_number", ""),
                ]
            ).lower()
        ]
    truncated = next_cursor is not None or len(rows) > limit
    return _list_payload([_dossier_row(d) for d in rows[:limit]], truncated)


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
            "clients": d.get("clients", []),
            "opposing_parties": d.get("opposing_parties", []),
            "greffe_number": d.get("greffe_number", ""),
            "juridiction_number": d.get("juridiction_number", ""),
            "competence": d.get("competence", ""),
            "palais_de_justice": d.get("palais_de_justice", ""),
            "district_judiciaire": d.get("district_judiciaire", ""),
            "is_administrative_tribunal": bool(d.get("is_administrative_tribunal")),
            # Forum: "judiciaire" (a Québec judicial court, file number parsed)
            # or "autre" (an administrative tribunal / federal court whose name
            # is in `tribunal` and whose file number is unparsed).
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
            # SUGGESTED delay verbatim, never a computed one; delai_type says
            # whether it is prescription (P), déchéance (D) or an avis (A).
            "delai": action_obj.delai if action_obj else "",
            "delai_type": action_obj.delai_type if action_obj else "",
            "delai_point_depart": action_obj.point_depart if action_obj else "",
            "action_references": action_obj.references if action_obj else "",
            "prescription_type": d.get("prescription_type", ""),
            "prescription_label": PRESCRIPTION_LABELS.get(
                d.get("prescription_type", ""), ""
            ),
            "droit_action_date": date_str(_as_utc(d.get("droit_action_date"))),
            "prescription_notes": d.get("prescription_notes", ""),
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

    return {
        "found": True,
        "dossier": record,
        "summaries": {
            "tasks": task_model.get_task_summary(did),
            "hearings": hearing_model.get_hearing_summary(did),
            "notes": note_model.get_notes_summary(did),
            "documents": document_model.get_document_summary(did),
            "time": time_out,
            "expenses": expense_out,
            "invoices": invoice_out,
            "protocol": protocol_model.get_protocol_summary(did),
        },
    }


# ── 4. list_tasks ───────────────────────────────────────────────────────

def list_tasks(args: dict) -> dict:
    status = args.get("status")
    include_completed = bool(args.get("include_completed", False))
    limit = _limit_arg(args, 25)

    tasks = task_model.list_tasks(
        dossier_id=args.get("dossier_id"), status_filter=status
    )
    if not status and not include_completed:
        tasks = [t for t in tasks if t.get("status") in ("à_faire", "en_cours")]

    truncated = len(tasks) > limit
    return _list_payload([_task_row(t) for t in tasks[:limit]], truncated)


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

    truncated = window_full or len(rows) > limit
    payload = _list_payload([_hearing_row(h) for h in rows[:limit]], truncated)
    payload["window"] = {"from": date_from.isoformat(), "to": date_to.isoformat()}
    return payload


# ── 6. list_notes ───────────────────────────────────────────────────────

def list_notes(args: dict) -> dict:
    limit = _limit_arg(args, 20)
    notes = note_model.list_notes(dossier_id=args["dossier_id"])
    truncated = len(notes) > limit
    items = [
        {
            "id": n.get("id", ""),
            "title": n.get("title", ""),
            "category": n.get("category", ""),
            "pinned": bool(n.get("pinned")),
            "created_at": iso_mtl(_as_utc(n.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(n.get("updated_at"))),
            "content_preview": (n.get("content", "") or "")[:_NOTE_PREVIEW_CHARS],
        }
        for n in notes[:limit]
    ]
    return _list_payload(items, truncated)


# ── 7. get_note ─────────────────────────────────────────────────────────

def get_note(args: dict) -> dict:
    note = note_model.get_note(args["note_id"])
    if note is None:
        return {"found": False, "note_id": args["note_id"]}
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
            "created_at": iso_mtl(_as_utc(note.get("created_at"))),
            "updated_at": iso_mtl(_as_utc(note.get("updated_at"))),
        },
    }


# ── 8. list_documents ───────────────────────────────────────────────────

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

    truncated = len(docs) > limit
    items = []
    for doc in docs[:limit]:
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
                "description": doc.get("description", ""),
                "tags": doc.get("tags", []),
                "created_at": iso_mtl(_as_utc(doc.get("created_at"))),
            }
        )
    payload = _list_payload(items, truncated)
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
    truncated = len(parties) > limit
    items = [
        {
            "id": p.get("id", ""),
            "display_name": partie_model.display_name(p),
            "type": p.get("type", ""),
            "contact_role": p.get("contact_role", ""),
            "is_organization": p.get("type") == "organization",
            "city": p.get("address_city", ""),
        }
        for p in parties[:limit]
    ]
    return _list_payload(items, truncated)


# ── 10. get_partie ──────────────────────────────────────────────────────

def _address_block(p: dict, prefix: str) -> dict:
    return {
        "street": p.get(f"{prefix}_street", ""),
        "unit": p.get(f"{prefix}_unit", ""),
        "city": p.get(f"{prefix}_city", ""),
        "province": p.get(f"{prefix}_province", ""),
        "postal_code": p.get(f"{prefix}_postal_code", ""),
        "country": p.get(f"{prefix}_country", ""),
    }


def get_partie(args: dict) -> dict:
    partie_id = args["partie_id"]
    p = partie_model.get_partie(partie_id)
    if p is None:
        return {"found": False, "partie_id": partie_id}

    dossier_refs = []
    for d in dossier_model.list_dossiers_for_partie(partie_id):
        relation = (
            "client" if partie_id in d.get("client_ids", []) else "partie_adverse"
        )
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
        _money(payload, "outstanding", invoice_model.get_outstanding_total())
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


# ── 12. list_protocol_steps ─────────────────────────────────────────────

def _protocol_payload(p: dict, now: datetime) -> dict:
    return {
        "id": p.get("id", ""),
        "title": p.get("title", ""),
        "protocol_type": p.get("protocol_type", ""),
        "status": p.get("status", ""),
        "court": p.get("court", ""),
        "start_date": date_str(_as_utc(p.get("start_date"))),
        "end_date": date_str(_as_utc(p.get("end_date"))),
        "notes": p.get("notes", ""),
        "steps": [_step_row(s, now) for s in p.get("steps", [])],
    }


def list_protocol_steps(args: dict) -> dict:
    dossier_id = args["dossier_id"]
    include_history = bool(args.get("include_history", False))
    now = datetime.now(timezone.utc)

    # Derived-only overdue status (never calls check_overdue_steps, which
    # writes to Firestore — see Phase I non-goals).
    active = protocol_model.get_protocol_for_dossier(dossier_id, active_only=True)

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
        "protocols": [_protocol_payload(p, now) for p in protocols],
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
    return {
        "greffe_number": result.get("greffe_number"),
        "juridiction_number": result.get("juridiction_number"),
        "palais_de_justice": greffe.get("palais_de_justice"),
        "district_judiciaire": greffe.get("district_judiciaire"),
        "point_de_service": greffe.get("point_de_service"),
        "tribunal": juridiction.get("tribunal"),
        "competence": juridiction.get("competence"),
        "greffe_type": juridiction.get("greffe_type"),
        "is_administrative": bool(result.get("is_administrative")),
        "parse_error": result.get("parse_error"),
    }
