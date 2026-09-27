"""Protocol management routes — create, detail, edit, steps, completion."""

from datetime import datetime, timezone

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import escape

from auth import login_required
from utils import deadlines, phases
from models.audit_event import record_deletion
from models.dossier import get_dossier
from models.protocol import (
    CLOSED_BY_AUTO,
    PROTOCOL_TYPE_COLORS,
    PROTOCOL_TYPE_LABELS,
    PROTOCOL_TYPE_SHORT_LABELS,
    PROTOCOL_TYPE_TRIBUNAL,
    STATUS_LABELS,
    STEP_STATUS_COLORS,
    STEP_STATUS_LABELS,
    VALID_PROTOCOL_TYPES,
    VALID_STATUSES,
    check_overdue_steps,
    date_needs_confirmation,
    delete_protocol,
    delete_step,
    get_protocol,
    get_protocol_for_dossier,
    list_protocols,
    regime_mismatch,
)
from routes import edit_conflict
from routes._helpers import is_htmx, parse_date_input
from services import protocoles as protocol_service

protocols_bp = Blueprint("protocols", __name__, url_prefix="/protocoles")


_is_htmx = is_htmx


_parse_date = parse_date_input


def _template_context() -> dict:
    """Return shared template context for protocol views."""
    return {
        "protocol_type_labels": PROTOCOL_TYPE_LABELS,
        "protocol_type_short_labels": PROTOCOL_TYPE_SHORT_LABELS,
        "protocol_type_colors": PROTOCOL_TYPE_COLORS,
        "status_labels": STATUS_LABELS,
        "step_status_labels": STEP_STATUS_LABELS,
        "step_status_colors": STEP_STATUS_COLORS,
        "valid_protocol_types": VALID_PROTOCOL_TYPES,
        "valid_statuses": VALID_STATUSES,
        # Phase O — the custom-step phase select + the step-row phase chip.
        "phase_labels": phases.PHASE_LABELS,
        "sous_phase_labels": phases.SOUS_PHASE_LABELS,
    }


def _regime_mismatches(dossier: dict | None) -> dict[str, bool]:
    """Per-template mismatch flags for the wizard's radio-card warnings."""
    return {
        t: regime_mismatch(t, dossier) for t in PROTOCOL_TYPE_TRIBUNAL
    }


# ── List ────────────────────────────────────────────────────────────────


@protocols_bp.route("/")
@login_required
def protocol_list() -> str:
    """Render the protocol list view."""
    status_filter = request.args.get("status", "").strip()
    type_filter = request.args.get("type", "").strip()

    protocols = list_protocols(
        status_filter=status_filter or None,
        protocol_type_filter=type_filter or None,
    )

    ctx = _template_context()
    ctx.update(
        protocols=protocols,
        status_filter=status_filter,
        type_filter=type_filter,
    )

    if _is_htmx():
        return render_template("protocols/_protocol_rows.html", **ctx)

    return render_template("protocols/list.html", **ctx)


# ── Create (wizard flow) ───────────────────────────────────────────────


@protocols_bp.route("/new")
@login_required
def protocol_new() -> str:
    """Render the protocol creation page. Requires dossier_id query param."""
    dossier_id = request.args.get("dossier_id", "")
    if not dossier_id:
        return redirect(url_for("dossiers.dossier_list"))

    dossier = get_dossier(dossier_id)
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))

    # Check if dossier already has an active protocol
    existing = get_protocol_for_dossier(dossier_id, active_only=True)
    if existing:
        return redirect(
            url_for("protocols.protocol_detail", protocol_id=existing["id"])
        )

    ctx = _template_context()
    ctx.update(dossier=dossier, protocol=None, errors=[],
               regime_mismatches=_regime_mismatches(dossier))
    return render_template("protocols/form.html", **ctx)


@protocols_bp.route("/", methods=["POST"])
@login_required
def protocol_create() -> str:
    """Handle protocol creation form submission."""
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    protocol_type = f.get("protocol_type", "").strip()
    start_date = _parse_date(f.get("start_date", ""))
    auto_create_tasks = f.get("auto_create_tasks") == "on"

    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        ctx = _template_context()
        ctx.update(dossier=None, protocol=None, errors=["Dossier introuvable."])
        return render_template("protocols/form.html", **ctx)

    # The service builds the protocol's snapshot fields from the dossier
    # (court = its tribunal, the file number and title labels). Linked
    # tasks only when asked: the service defaults to none, and the wizard
    # passes its checkbox — unchecked by default — explicitly.
    protocol, errors, report = protocol_service.create_protocol(
        dossier, protocol_type, start_date,
        title=f.get("title", ""), notes=f.get("notes", ""),
        create_linked_tasks=auto_create_tasks,
    )

    if errors:
        ctx = _template_context()
        shown = {
            "title": f.get("title", "").strip()
            or protocol_service.DEFAULT_TITLE,
            "notes": f.get("notes", "").strip(),
            "protocol_type": protocol_type,
            "start_date": start_date,
        }
        ctx.update(dossier=dossier, protocol=shown, errors=errors,
                   regime_mismatches=_regime_mismatches(dossier))
        return render_template("protocols/form.html", **ctx)

    params = {}
    if report["tasks_failed"] or report["tasks_linked"] < report["tasks_created"]:
        params["message"] = (
            "Protocole créé. Certaines tâches liées n'ont pas pu être "
            "créées ou rattachées à leur étape — vérifiez les étapes sans "
            "« Voir la tâche liée »."
        )
    target = url_for("protocols.protocol_detail", protocol_id=protocol["id"],
                     **params)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Detail ──────────────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>")
@login_required
def protocol_detail(protocol_id: str) -> str:
    """Render the protocol detail view with timeline."""
    # The step button's and the step forms' outcomes come back as a
    # redirect carrying ?erreur= / ?message=.
    return _render_detail(
        protocol_id,
        erreur=request.args.get("erreur", ""),
        message=request.args.get("message", ""),
    )


# The page's banner on a protocol that is not « actif » (D17, 2026-09-27):
# its steps can be neither added nor edited — the MODEL refuses both on
# every path — so the page hides those controls and says why, rather than
# offering buttons that can only be refused.
INACTIVE_STEPS_BANNER = (
    "Ce protocole n'est pas actif : réactivez-le (Modifier le protocole → "
    "Statut « Actif ») avant de modifier ses étapes."
)
# ... except the one change the model still accepts there: reopening a
# completed step of a protocol the CASCADE closed, which reactivates it.
AUTO_CLOSED_REOPEN_HINT = (
    "Rouvrir une étape complétée le réactive aussi : il a été fermé par sa "
    "dernière étape."
)


def _render_detail(
    protocol_id: str,
    *,
    erreur: str = "",
    message: str = "",
    conflict: dict | None = None,
    step_form: dict | None = None,
):
    """The detail page — also the 200 re-render of a refused step edit.

    *step_form* keeps a refused inline step form OPEN with what was
    submitted (``step_id``, ``deadline_date`` as posted, ``notes``,
    ``confirm_date``) and the etag it now stands for: the submitted one on
    a validation error, the current one after a conflict (the banner then
    says why, and a second save is a deliberate overwrite).
    """
    # Flips first (check_overdue_steps reads + writes internally), then ONE
    # fresh read — the old read→check→reload chain streamed the whole steps
    # subcollection three times per detail view. The flip is a derived
    # stamp: it never regenerates a step's etag.
    check_overdue_steps(protocol_id)
    protocol = get_protocol(protocol_id)
    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    ctx = _template_context()
    ctx["protocol"] = protocol
    ctx["erreur"] = erreur
    ctx["message"] = message
    ctx["conflict"] = conflict
    ctx["step_form"] = step_form
    # The model's D17 rule, mirrored for the controls: add/edit only on an
    # « actif » protocol; on one the cascade closed, the reopen button of a
    # completed step stays (set_step_status reactivates the protocol).
    status = protocol.get("status", "")
    ctx["steps_editable"] = status == "actif"
    ctx["steps_reopenable"] = (
        status == "complété" and protocol.get("closed_by") == CLOSED_BY_AUTO
    )
    ctx["inactive_banner"] = INACTIVE_STEPS_BANNER
    ctx["auto_closed_hint"] = AUTO_CLOSED_REOPEN_HINT

    # Compute progress
    steps = protocol.get("steps", [])
    total = len(steps)
    completed = sum(1 for s in steps if s.get("status") == "complété")
    ctx["progress_pct"] = int((completed / total * 100) if total > 0 else 0)
    ctx["steps_completed"] = completed
    ctx["steps_total"] = total

    # Days remaining per step — Montréal day, prorogued deadline
    # (utils.deadlines.days_until): due today = 0, and the count goes
    # negative only once the step is truly late. Aligned with
    # get_protocol_summary and the MCP is_overdue rule (2026-08-02).
    today = deadlines.today_mtl()
    for step in steps:
        deadline = step.get("deadline_date")
        if deadline and step.get("status") != "complété":
            step["_days_remaining"] = deadlines.days_until(deadline, today=today)
        else:
            step["_days_remaining"] = None
        # « À modifier » + the confirmation box: the recompute's own test,
        # never the raw flag (a notes-only save set it before lot 1a).
        step["_needs_confirmation"] = date_needs_confirmation(protocol, step)

    return render_template("protocols/detail.html", **ctx)


# ── Edit protocol metadata ─────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/edit")
@login_required
def protocol_edit(protocol_id: str) -> str:
    """Render the protocol edit form."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    dossier = get_dossier(protocol["dossier_id"]) if protocol.get("dossier_id") else None

    ctx = _template_context()
    ctx.update(protocol=protocol, dossier=dossier, errors=[], edit_mode=True)
    return render_template("protocols/form.html", **ctx)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _alignment_notice(report: dict) -> list[str]:
    """What happened to the linked tasks of the steps that moved."""
    parts = []
    if report.get("aligned"):
        parts.append(_count(report["aligned"], "tâche liée a suivi son étape",
                            "tâches liées ont suivi leur étape") + ".")
    diverged = report.get("diverged", 0)
    if diverged == 1:
        parts.append("1 tâche liée garde sa propre échéance, modifiée à la "
                     "main : elle n'a pas été déplacée.")
    elif diverged:
        parts.append(f"{diverged} tâches liées gardent leur propre échéance, "
                     "modifiée à la main : elles n'ont pas été déplacées.")
    stuck = report.get("failed", 0) + report.get("missing", 0)
    if stuck:
        parts.append(
            _count(stuck, "tâche liée n'a pas pu être mise à jour",
                   "tâches liées n'ont pas pu être mises à jour")
            + " — vérifiez depuis la fiche de la tâche.")
    return parts


def _update_notice(report: dict) -> str:
    """The banner after a protocol save, or ``""`` when nothing moved."""
    preserved = report.get("preserved_completed", 0) + report.get(
        "preserved_confirmed", 0)
    if not report.get("moved") and not preserved:
        return ""
    parts = [
        "Date de début modifiée : "
        + _count(report.get("moved", 0), "échéance recalculée",
                 "échéances recalculées") + "."
    ]
    if preserved:
        parts.append(
            _count(preserved, "échéance conservée", "échéances conservées")
            + " (étape complétée ou date confirmée).")
    parts += _alignment_notice(report)
    return " ".join(parts)


@protocols_bp.route("/<protocol_id>", methods=["POST"])
@login_required
def protocol_update(protocol_id: str) -> str:
    """Handle protocol metadata edit.

    The form carries the etag it was rendered from: a protocol changed
    meanwhile — a step completed on the phone that closed it, a second
    tab — re-renders at 200 with the amber banner and the submitted
    values, never overwritten. A start-date change recomputes the steps in
    the model's own write and carries their linked tasks along (service).
    """
    expected = edit_conflict.submitted_etag()
    f = request.form
    data = {
        "title": f.get("title", "").strip(),
        "notes": f.get("notes", "").strip(),
    }
    # Only when posted: the old default « actif » reactivated a completed or
    # suspended protocol on any save that did not carry the field.
    if f.get("status"):
        data["status"] = f.get("status", "")
    # The model recomputes the step deadlines in the SAME write when the
    # date changes (preserving completed steps and confirmed CS dates); an
    # unchanged date is a no-op there.
    new_start_date = _parse_date(f.get("start_date", ""))
    if new_start_date:
        data["start_date"] = new_start_date

    protocol = get_protocol(protocol_id)
    if not protocol:
        return redirect(url_for("dossiers.dossier_list"))

    _updated, errors, report = protocol_service.update_protocol(
        protocol_id, data, expected_etag=expected)

    if errors:
        errors, conflict, etag = edit_conflict.resolve_refusal(
            errors,
            submitted=expected,
            reread=lambda: get_protocol(protocol_id),
            compare_url=url_for("protocols.protocol_detail",
                                protocol_id=protocol_id),
        )
        shown = {**protocol, **data, "id": protocol_id, "etag": etag}
        dossier = get_dossier(protocol["dossier_id"]) if protocol.get("dossier_id") else None
        ctx = _template_context()
        ctx.update(protocol=shown, dossier=dossier, errors=errors,
                   conflict=conflict, edit_mode=True)
        return render_template("protocols/form.html", **ctx)

    notice = _update_notice(report)
    target = url_for("protocols.protocol_detail", protocol_id=protocol_id,
                     **({"message": notice} if notice else {}))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Delete protocol ─────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/delete", methods=["POST"])
@login_required
def protocol_delete(protocol_id: str) -> str:
    """Delete a protocol and redirect to dossier."""
    protocol = get_protocol(protocol_id)
    dossier_id = protocol.get("dossier_id") if protocol else None

    success, error = delete_protocol(protocol_id)

    if success:
        # Append-only deletion trail (PA-G06) — a deleted protocol takes
        # its whole step timeline with it, which is worth remembering.
        record_deletion(
            "protocol", protocol_id,
            dossier_id=dossier_id or "",
            title=(protocol or {}).get("title", ""),
            status=(protocol or {}).get("protocol_type", ""),
        )

    target = (
        url_for("dossiers.dossier_detail", dossier_id=dossier_id)
        if dossier_id
        else url_for("dossiers.dossier_list")
    )

    if _is_htmx():
        if success:
            resp = redirect(target)
            resp.headers["HX-Redirect"] = target
            return resp
        return f'<div class="text-red-600 text-sm">{escape(error)}</div>', 422

    return redirect(target)


# ── Step operations ─────────────────────────────────────────────────────


@protocols_bp.route("/<protocol_id>/steps", methods=["POST"])
@login_required
def step_add(protocol_id: str) -> str:
    """Add a new custom step to the protocol."""
    f = request.form
    step_data = {
        "title": f.get("title", "").strip(),
        "description": f.get("description", "").strip(),
        "cpc_reference": f.get("cpc_reference", "").strip(),
        "deadline_date": _parse_date(f.get("deadline_date", "")),
        # Phase O — optional on a custom step; the model imputes the -00
        # sub-code when a phase is chosen (D-4/D-15).
        "phase": f.get("phase", ""),
    }

    # No linked task from the web form (it offers none) — said explicitly.
    _step, errors, _report = protocol_service.add_step(
        protocol_id, step_data, create_linked_task=False)

    # A refusal travels as a 2xx redirect with ?erreur=: the old 422
    # fragment was never swapped by htmx, and this is a full-page form.
    target = url_for("protocols.protocol_detail", protocol_id=protocol_id,
                     **({"erreur": errors[0]} if errors else {}))
    resp = redirect(target)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target
    return resp


def _stored_step(protocol_id: str, step_id: str) -> dict | None:
    protocol = get_protocol(protocol_id) or {}
    return next((s for s in protocol.get("steps", []) or []
                 if s.get("id") == step_id), None)


@protocols_bp.route("/<protocol_id>/steps/<step_id>", methods=["POST"])
@login_required
def step_update(protocol_id: str, step_id: str) -> str:
    """Update a step (deadline, notes, explicit date confirmation).

    The inline form carries the step's etag. A refusal — a stale etag (the
    phone, a second tab or the connector changed the step), a locked or
    cleared deadline — re-renders the page at 200 with that step's form
    open on the submitted values: the old 422 fragment was never swapped.
    A changed deadline carries the linked task along (service).
    """
    expected = edit_conflict.submitted_etag()
    f = request.form
    data = {}

    if f.get("deadline_date") is not None:
        data["deadline_date"] = _parse_date(f.get("deadline_date", ""))
    if f.get("notes") is not None:
        data["notes"] = f.get("notes", "").strip()
    # A posted `status` is never forwarded: a step changes status through
    # the « Compléter / Rouvrir » button (set_step_status) only — a status
    # written here skipped the task cascade and the completion check, and
    # the model now refuses it.
    if f.get("confirm_date"):
        data["confirm_date"] = True

    step, errors, report = protocol_service.update_step(
        protocol_id, step_id, data, expected_etag=expected)

    if errors:
        errors, conflict, etag = edit_conflict.resolve_refusal(
            errors,
            submitted=expected,
            reread=lambda: _stored_step(protocol_id, step_id),
            compare_url=url_for("protocols.protocol_detail",
                                protocol_id=protocol_id),
        )
        return _render_detail(
            protocol_id,
            erreur=" ".join(errors),
            conflict=conflict,
            step_form={
                "step_id": step_id,
                "deadline_date": f.get("deadline_date"),
                "notes": f.get("notes"),
                "confirm_date": bool(f.get("confirm_date")),
                "etag": etag,
            },
        )

    notice = ""
    if report.get("aligned") or report.get("diverged") or report.get(
            "failed") or report.get("missing"):
        notice = " ".join(["Échéance modifiée."] + _alignment_notice(report))
    target = url_for("protocols.protocol_detail", protocol_id=protocol_id,
                     **({"message": notice} if notice else {}))
    resp = redirect(target)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target
    return resp


def _step_target(protocol_id: str, step_id: str) -> str:
    """The state the step button was rendered to reach.

    Every button posts ``target`` (``complété`` or ``à_venir``) since
    2026-09-26. A page rendered before that posts nothing: its intent is
    read off the STORED status, the way the old toggle did — the very
    staleness the target removes, accepted only for pages already open at
    deploy time.
    """
    target = request.form.get("target")
    if target is not None:
        return target.strip()
    protocol = get_protocol(protocol_id) or {}
    step = next(
        (s for s in protocol.get("steps", []) if s.get("id") == step_id), {}
    )
    return "à_venir" if step.get("status") == "complété" else "complété"


# What the lawyer is told after a click, beyond the step itself.
_STEP_ALREADY = {
    "complété": "Cette étape était déjà complétée — rien n'a été changé.",
    "à_venir": "Cette étape était déjà ouverte — rien n'a été changé.",
}
_TASK_SYNC_NOTICE = {
    "skipped_cancelled": (
        "La tâche liée est annulée : elle n'a pas été modifiée."
    ),
    "failed": (
        "La tâche liée n'a pas pu être mise à jour — vérifiez-la depuis sa "
        "fiche."
    ),
    # « ou illisible »: get_task FAILS OPEN, so a Firestore read error
    # reaches the cascade as « no such task » — the two cannot be told
    # apart there, and asserting the wrong one is worse than naming both
    # (the lot-Q « introuvable ou illisible » rule).
    "missing": (
        "La tâche liée est introuvable ou n'a pas pu être lue : elle n'a pas "
        "été modifiée."
    ),
}


def _step_notice(target: str, outcome: dict) -> str:
    """The French banner for a committed (or no-op) step click, or ``""``."""
    if not outcome.get("changed"):
        return _STEP_ALREADY.get(target, "")
    parts = []
    notice = _TASK_SYNC_NOTICE.get(outcome.get("task_sync", ""))
    if notice:
        parts.append("Étape mise à jour. " + notice)
    if outcome.get("protocol_closed"):
        parts.append(
            "Toutes les étapes sont complétées : le protocole est maintenant "
            "complété."
        )
    if outcome.get("protocol_reopened"):
        parts.append(
            "Le protocole, fermé à sa dernière étape, est de nouveau actif."
        )
    return " ".join(parts)


@protocols_bp.route("/<protocol_id>/steps/<step_id>/complete", methods=["POST"])
@login_required
def step_complete(protocol_id: str, step_id: str) -> str:
    """Complete or reopen a step, as the clicked button asked.

    Never a toggle (``set_step_status``): a stale page asking for the state
    the step is already in writes nothing and cascades nothing. The linked
    task's DAV collection is bumped by the cascade itself. Every outcome
    travels back as a redirect with ``?erreur=`` / ``?message=`` — the
    button is a full-page form, and a 4xx fragment would never render
    (the old route also redirected a refusal SILENTLY on this form).
    """
    target = _step_target(protocol_id, step_id)
    _step, errors, outcome = protocol_service.set_step_status(
        protocol_id, step_id, target)

    params = {}
    if errors:
        params["erreur"] = errors[0]
    else:
        notice = _step_notice(target, outcome)
        if notice:
            params["message"] = notice

    target_url = url_for(
        "protocols.protocol_detail", protocol_id=protocol_id, **params
    )
    resp = redirect(target_url)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target_url
    return resp


@protocols_bp.route("/<protocol_id>/steps/<step_id>/delete", methods=["POST"])
@login_required
def step_delete(protocol_id: str, step_id: str) -> str:
    """Delete a custom (non-mandatory) step."""
    protocol = get_protocol(protocol_id)
    step = next(
        (s for s in (protocol or {}).get("steps", []) if s.get("id") == step_id),
        None,
    )
    success, error = delete_step(protocol_id, step_id)

    if success:
        record_deletion(
            "protocol_step", step_id,
            dossier_id=(protocol or {}).get("dossier_id", "") or "",
            title=(step or {}).get("title", ""),
            status=(step or {}).get("status", ""),
        )

    if not success and _is_htmx():
        return f'<div class="text-red-600 text-sm p-3">{escape(error)}</div>', 422

    target = url_for("protocols.protocol_detail", protocol_id=protocol_id)
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)
