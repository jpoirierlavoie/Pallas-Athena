"""Dashboard route — Phase 11: at-a-glance summary after login."""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template

from auth import login_required

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index() -> str:
    """Render the main dashboard with schedule, urgent items, and stats."""
    now = datetime.now(timezone.utc)

    # ── Short-term schedule: hearings in the next 7 days ──────────────
    short_term_hearings = _get_hearings_in_range(now, now + timedelta(days=7))

    # ── Long-term planning: hearings in the next 2 months ─────────────
    long_term_hearings = _get_hearings_in_range(
        now + timedelta(days=7), now + timedelta(days=60)
    )

    # ── Urgent items ──────────────────────────────────────────────────
    urgent_tasks = _get_urgent_tasks(now)
    urgent_protocol_steps = _get_urgent_protocol_steps(now)
    prescription_alerts = _get_prescription_alerts(now)

    # ── Quick stats ───────────────────────────────────────────────────
    stats = _get_quick_stats()

    return render_template(
        "dashboard/index.html",
        short_term_hearings=short_term_hearings,
        long_term_hearings=long_term_hearings,
        urgent_tasks=urgent_tasks,
        urgent_protocol_steps=urgent_protocol_steps,
        prescription_alerts=prescription_alerts,
        stats=stats,
        now=now,
    )


# ── Data helpers ─────────────────────────────────────────────────────────


def _get_hearings_in_range(date_from: datetime, date_to: datetime) -> list[dict]:
    """Return non-cancelled hearings in a date range, chronologically.

    The date range, ordering, and bound (100 rows) run server-side via
    ``list_hearings_in_range``; only the cancelled-status exclusion stays
    in Python, over the already-small result.
    """
    try:
        from models.hearing import list_hearings_in_range
        hearings = list_hearings_in_range(date_from, date_to)
        return [
            h for h in hearings
            if h.get("status") not in ("annulée",)
        ]
    except Exception:
        return []


def _get_urgent_tasks(now: datetime) -> list[dict]:
    """Return tasks due within 14 days or overdue (not completed/cancelled).

    The status/due-date filters, ordering, and bound run server-side via
    ``list_urgent_tasks`` (tasks without a due_date are excluded by the
    range filter, matching the previous behaviour); the overdue-first
    re-sort happens here over the bounded result.
    """
    try:
        from models.task import list_urgent_tasks
        cutoff = now + timedelta(days=14)
        urgent = list_urgent_tasks(cutoff)
        for t in urgent:
            due = t.get("due_date")
            t["_overdue"] = bool(due and due < now)
        # Overdue first, then by due_date ascending
        urgent.sort(key=lambda t: (not t.get("_overdue", False), t.get("due_date") or now))
        return urgent
    except Exception:
        return []


def _get_urgent_protocol_steps(now: datetime) -> list[dict]:
    """Return protocol steps due within 14 days or overdue.

    One collection-group query plus one batched parent-protocol fetch
    (``list_urgent_steps``) replaces the previous per-protocol N+1 scan.
    The model attaches ``_protocol_title`` / ``_protocol_id`` /
    ``_dossier_file_number``; the overdue flag and re-sort happen here.
    """
    try:
        from models.protocol import list_urgent_steps
        cutoff = now + timedelta(days=14)
        urgent_steps = list_urgent_steps(cutoff)
        for step in urgent_steps:
            deadline = step.get("deadline_date")
            step["_overdue"] = bool(deadline and deadline < now)

        urgent_steps.sort(
            key=lambda s: (not s.get("_overdue", False), s.get("deadline_date") or now)
        )
        return urgent_steps
    except Exception:
        return []


def _get_prescription_alerts(now: datetime) -> list[dict]:
    """Return dossiers with prescription dates within 60 days.

    Both filters (status actif, prescription_date <= cutoff) run
    server-side, bounded, via ``list_prescription_alerts``; the
    juridical-day computation stays here.
    """
    try:
        from models.dossier import list_prescription_alerts
        from utils.deadlines import prev_juridical_day
        cutoff = now + timedelta(days=60)
        alerts = []
        for d in list_prescription_alerts(cutoff):
            pdate = d.get("prescription_date")
            if not pdate:
                continue  # defensive — the range filter excludes these
            d["_days_remaining"] = max(0, (pdate - now).days)
            pdate_as_date = pdate.date() if hasattr(pdate, "date") else pdate
            last_action = prev_juridical_day(pdate_as_date)
            d["_last_action_date"] = last_action
            d["_last_action_differs"] = last_action != pdate_as_date
            alerts.append(d)
        alerts.sort(key=lambda d: d.get("prescription_date") or now)
        return alerts
    except Exception:
        return []


def _get_quick_stats() -> dict:
    """Return counts and amounts for the dashboard stats cards.

    Each stat is a single server-side aggregation query (COUNT/SUM) —
    O(1) payload instead of full-collection scans. Each stat degrades
    independently: a failure leaves its safe default in place.
    """
    stats: dict = {
        "open_dossiers": 0,
        "unbilled_hours": 0.0,
        "unbilled_amount": 0,
        "outstanding_invoices": 0,
    }
    try:
        from models.dossier import count_open
        stats["open_dossiers"] = count_open()
    except Exception as exc:
        logger.warning("dashboard: open dossiers stat failed: %s", exc)

    try:
        from models.time_entry import get_unbilled_totals
        totals = get_unbilled_totals()
        stats["unbilled_hours"] = totals.get("hours", 0.0)
        stats["unbilled_amount"] = totals.get("amount", 0)
    except Exception as exc:
        logger.warning("dashboard: unbilled time stat failed: %s", exc)

    try:
        from models.invoice import get_outstanding_total
        stats["outstanding_invoices"] = get_outstanding_total()
    except Exception as exc:
        logger.warning("dashboard: outstanding invoices stat failed: %s", exc)

    return stats
