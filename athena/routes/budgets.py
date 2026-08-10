"""Budget routes — per-dossier fee estimates by litigation phase.

Full-page form (versioned, append-only — see models/budget.py), version
history, and the two PDF exports: « Estimation des frais et honoraires »
(client document, no actuals) and « Suivi budgétaire » (with actuals).
All @login_required, French UI, standard CSRF (no exemption), POST+redirect
with inline error boxes (the trust pattern).
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from flask import Blueprint, Response, redirect, render_template, request, url_for

from auth import login_required
from models import budget as budget_model
from models.dossier import get_dossier
from models.expense import list_expenses
from models.time_entry import list_time_entries
from security import safe_internal_redirect
from utils import phases
from utils.logging_setup import log_dossier_event

budgets_bp = Blueprint("budgets", __name__, url_prefix="/budgets")

VALID_VARIANTES = ("estimation", "suivi")


def _parse_cents(raw) -> int:
    """fr-CA / en amount string → integer cents; 0 when blank/invalid."""
    if raw is None:
        return 0
    s = str(raw).strip().replace(" ", "").replace(" ", "")
    s = s.replace(" ", "").replace("$", "")
    if not s:
        return 0
    s = s.replace(",", ".")
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        return int(
            (Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    except Exception:
        return 0


def _parse_budget_lines_json(raw: str) -> list[dict]:
    """Explicit whitelist over the Alpine repeater's hidden JSON.

    Never ``**entry`` — the state round-trips through the browser. Type
    coercion and validation belong to the model (_normalize_lines); this
    only shapes the payload (frais arrive as a fr-CA dollar string).
    """
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        out.append({
            "sous_phase": str(entry.get("sous_phase") or ""),
            "hours": entry.get("hours") or 0,
            "frais_cents": _parse_cents(entry.get("frais")),
        })
    return out


def _form_seed(dossier: dict, latest: dict | None) -> dict:
    """The form's non-executable JSON seed.

    Creation: the 9 tronc phases, each with its full sub-code grid at zero
    (zero rows are dropped at save, so unused codes cost nothing). Edition:
    the same grid per phase present in the latest version, values restored —
    plus any module phase that version budgeted. Modules not present are
    offered through « Ajouter un module ».
    """
    existing: dict[str, dict] = {}
    if latest:
        for line in latest.get("lines", []):
            existing[line.get("sous_phase", "")] = line

    def _group(code: str) -> dict:
        p = phases.PHASES[code]
        lines = []
        for sc in p.sous_codes:
            prev = existing.get(sc.code)
            lines.append({
                "sous_phase": sc.code,
                "label": sc.libelle,
                "hours": float(prev.get("hours") or 0) if prev else 0,
                "frais": (
                    f"{prev.get('frais_cents', 0) / 100:.2f}".replace(".", ",")
                    if prev and prev.get("frais_cents") else ""
                ),
            })
        return {"phase": code, "libelle": p.libelle,
                "categorie": p.categorie, "lines": lines}

    seeded = list(phases.TRONC_ORDONNE)
    if latest:
        for line in latest.get("lines", []):
            parent = phases.phase_of(line.get("sous_phase", ""))
            if parent and parent not in seeded:
                seeded.append(parent)

    groups = [_group(code) for code in seeded]
    modules = [
        {
            "phase": code,
            "libelle": p.libelle,
            "categorie": p.categorie,
            "lines": [
                {"sous_phase": sc.code, "label": sc.libelle,
                 "hours": 0, "frais": ""}
                for sc in p.sous_codes
            ],
        }
        for code, p in phases.PHASES.items()
        if p.categorie == "module" and code not in seeded
    ]
    rate = (
        int(latest.get("hourly_rate") or 0) if latest
        else int(dossier.get("hourly_rate") or 0)
    )
    return {
        "groups": groups,
        "modules_disponibles": modules,
        "rate_display": f"{rate / 100:.2f}".replace(".", ","),
    }


@budgets_bp.route("/nouveau")
@login_required
def budget_form() -> str:
    dossier_id = request.args.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))
    latest = budget_model.get_latest_budget(dossier_id)
    return render_template(
        "budgets/form.html",
        dossier=dossier,
        latest=latest,
        seed=_form_seed(dossier, latest),
        errors=[],
        note_value=(latest.get("note", "") if latest else ""),
        return_to=request.args.get("return_to", ""),
    )


@budgets_bp.route("/", methods=["POST"])
@login_required
def budget_create() -> str:
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))
    data = {
        "dossier_id": dossier_id,
        "hourly_rate": _parse_cents(f.get("hourly_rate")),
        "note": f.get("note", "").strip(),
        "lines": _parse_budget_lines_json(f.get("lines_json", "")),
    }
    return_to = f.get("return_to", "")

    budget, errors = budget_model.create_budget(data)
    if errors:
        latest = budget_model.get_latest_budget(dossier_id)
        return render_template(
            "budgets/form.html",
            dossier=dossier,
            latest=latest,
            seed=_form_seed(dossier, latest),
            errors=errors,
            note_value=data["note"],
            return_to=return_to,
        ), 400

    log_dossier_event(
        "budget_saved", dossier_id,
        budget_id=budget["id"], version=budget["version"],
        line_count=len(budget["lines"]),
    )
    target = safe_internal_redirect(
        return_to,
        url_for("dossiers.dossier_detail", dossier_id=dossier_id, tab="budget"),
    )
    return redirect(target)


@budgets_bp.route("/historique")
@login_required
def budget_history() -> str:
    dossier_id = request.args.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    if not dossier:
        return redirect(url_for("dossiers.dossier_list"))
    versions = budget_model.list_budget_versions(dossier_id)
    rows = [
        {**b, "totals": budget_model.budget_totals(b)} for b in versions
    ]
    return render_template(
        "budgets/history.html",
        dossier=dossier,
        versions=rows,
        return_to=request.args.get("return_to", ""),
    )


@budgets_bp.route("/<budget_id>/export/<variante>")
@login_required
def budget_export(budget_id: str, variante: str) -> Response:
    if variante not in VALID_VARIANTES:
        return Response("Format non supporté.", status=400,
                        mimetype="text/plain; charset=utf-8")
    budget = budget_model.get_budget(budget_id)
    if not budget:
        return Response("Budget introuvable.", status=404,
                        mimetype="text/plain; charset=utf-8")
    dossier = get_dossier(budget.get("dossier_id", "")) or {}

    view = None
    actuals = None
    if variante == "suivi":
        entries = list_time_entries(dossier_id=budget["dossier_id"])
        exps = list_expenses(dossier_id=budget["dossier_id"])
        actuals = budget_model.aggregate_actuals(entries, exps)
        view = budget_model.build_budget_view(budget, actuals)

    from utils.budget_pdf import build_budget_pdf
    from utils.cabinet import cabinet_dict

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_no = (dossier.get("file_number") or "dossier").replace("/", "-")
    resp = build_budget_pdf(
        variant=variante,
        dossier=dossier,
        budget=budget,
        view=view,
        actuals=actuals,
        cabinet=cabinet_dict(),
        filename=f"budget_{variante}_{file_no}_{date_str}.pdf",
    )
    log_dossier_event(
        "budget_exported", budget.get("dossier_id", ""),
        budget_id=budget_id, version=budget.get("version", 0),
        variant=variante,
    )
    return resp
