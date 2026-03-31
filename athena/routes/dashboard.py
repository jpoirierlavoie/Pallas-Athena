"""Dashboard route — Phase 11: at-a-glance summary after login."""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template

from auth import login_required

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
    """Return non-cancelled hearings in a date range, chronologically."""
    try:
        from models.hearing import list_hearings
        hearings = list_hearings(date_from=date_from, date_to=date_to)
        return [
            h for h in hearings
            if h.get("status") not in ("annulée",)
        ]
    except Exception:
        return []


def _get_urgent_tasks(now: datetime) -> list[dict]:
    """Return tasks due within 14 days or overdue (not completed/cancelled)."""
    try:
        from models.task import list_tasks
        all_tasks = list_tasks()
        cutoff = now + timedelta(days=14)
        urgent = []
        for t in all_tasks:
            if t.get("status") in ("terminée", "annulée"):
                continue
            due = t.get("due_date")
            if due and due <= cutoff:
                t["_overdue"] = due < now
                urgent.append(t)
        # Overdue first, then by due_date ascending
        urgent.sort(key=lambda t: (not t.get("_overdue", False), t.get("due_date") or now))
        return urgent
    except Exception:
        return []


def _get_urgent_protocol_steps(now: datetime) -> list[dict]:
    """Return protocol steps due within 14 days or overdue."""
    try:
        from models.protocol import list_protocols, get_protocol
        protocols = list_protocols(status_filter="actif")
        urgent_steps: list[dict] = []
        cutoff = now + timedelta(days=14)

        for proto in protocols:
            full = get_protocol(proto["id"])
            if not full:
                continue
            for step in full.get("steps", []):
                if step.get("status") == "complété":
                    continue
                deadline = step.get("deadline_date")
                if deadline and deadline <= cutoff:
                    step["_protocol_title"] = full.get("title", "")
                    step["_protocol_id"] = full["id"]
                    step["_dossier_file_number"] = full.get("dossier_file_number", "")
                    step["_overdue"] = deadline < now
                    urgent_steps.append(step)

        urgent_steps.sort(
            key=lambda s: (not s.get("_overdue", False), s.get("deadline_date") or now)
        )
        return urgent_steps
    except Exception:
        return []


def _get_prescription_alerts(now: datetime) -> list[dict]:
    """Return dossiers with prescription dates within 60 days."""
    try:
        from models.dossier import list_dossiers
        from utils.deadlines import prev_juridical_day
        dossiers = list_dossiers(status_filter="actif")
        cutoff = now + timedelta(days=60)
        alerts = []
        for d in dossiers:
            pdate = d.get("prescription_date")
            if pdate and pdate <= cutoff:
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
    """Return counts and amounts for the dashboard stats cards."""
    stats: dict = {
        "open_dossiers": 0,
        "unbilled_hours": 0.0,
        "unbilled_amount": 0,
        "outstanding_invoices": 0,
    }
    try:
        from models.dossier import list_dossiers
        active = list_dossiers(status_filter="actif")
        pending = list_dossiers(status_filter="en_attente")
        stats["open_dossiers"] = len(active) + len(pending)
    except Exception:
        pass

    try:
        from models.time_entry import list_time_entries
        entries = list_time_entries()
        for e in entries:
            if e.get("billable") and not e.get("invoiced"):
                stats["unbilled_hours"] += e.get("hours", 0)
                stats["unbilled_amount"] += e.get("amount", 0)
        stats["unbilled_hours"] = round(stats["unbilled_hours"], 1)
    except Exception:
        pass

    try:
        from models.invoice import list_invoices
        invoices = list_invoices(status_filter="envoyée")
        outstanding = sum(inv.get("amount_due", 0) for inv in invoices)
        # Also include overdue
        overdue_invoices = list_invoices(status_filter="en_retard")
        outstanding += sum(inv.get("amount_due", 0) for inv in overdue_invoices)
        stats["outstanding_invoices"] = outstanding
    except Exception:
        pass

    return stats
