"""Budget routes — per-dossier fee estimates by litigation phase.

Full-page form (versioned, append-only — see models/budget.py), version
history, and the two PDF exports: « Estimation des frais et honoraires »
(client document, no actuals) and « Suivi budgétaire » (with actuals).
All @login_required, French UI, standard CSRF (no exemption), POST+redirect
with inline error boxes (the trust pattern).

Since lot 3a (step 2) the form carries the version it was built on — a
hidden ``base_version``, the latest version number (0 = no budget yet) —
and ``create_budget`` refuses a save whose base is no longer the latest (a
second tab, the connector). The refusal re-renders at 200 with the amber
banner, the SUBMITTED values and the CURRENT version as the new base, so
the next save is a deliberate one (plan rule 11). A form rendered before
the field existed skips the check; a malformed value is a French 400.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from flask import (
    Blueprint,
    Response,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from models import budget as budget_model
from models.dossier import get_dossier
from models.expense import list_expenses
from models.time_entry import list_time_entries
from routes import edit_conflict
from security import safe_internal_redirect
from utils import phases
from utils.logging_setup import log_dossier_event

budgets_bp = Blueprint("budgets", __name__, url_prefix="/budgets")

VALID_VARIANTES = ("estimation", "suivi")

BASE_VERSION_FIELD = "base_version"
# A version number, as the form renders it. Anything else in the field did
# not come from a page this application rendered.
_BASE_VERSION_MAX_DIGITS = 6
BASE_VERSION_MALFORMED = (
    "Requête invalide : la version du budget transmise par le formulaire "
    "est illisible. Rien n'a été enregistré — rechargez la page, puis "
    "refaites la modification."
)


def _submitted_base_version() -> Optional[int]:
    """The version the submitted form was built on, or ``None``.

    ``None`` — the field is absent or blank (a page rendered before this
    deploy): the model skips the check, as before. A present value that is
    not a version number aborts with a French 400; nothing is written.
    """
    raw = request.form.get(BASE_VERSION_FIELD)
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    if not (raw.isascii() and raw.isdigit()) or len(raw) > _BASE_VERSION_MAX_DIGITS:
        response = make_response(BASE_VERSION_MALFORMED, 400)
        response.mimetype = "text/plain"
        abort(response)
    return int(raw)


def _version_of(budget: Optional[dict]) -> int:
    """A budget's version number — 0 for « no budget yet »."""
    return int((budget or {}).get("version") or 0)


def _offered_phase(code: str) -> bool:
    """True when the form offers the phase of sub-code *code* (the tronc and
    the modules — never ADM/HOR, which the model refuses anyway)."""
    parent = phases.phase_of(code)
    if not parent:
        return False
    return parent in phases.TRONC_ORDONNE or phases.PHASES[parent].categorie == "module"


def _submitted_as_version(data: dict) -> dict:
    """The SUBMITTED budget shaped like a stored version, for the form seed
    of a conflict re-render — so the lawyer's figures survive the refusal.

    Only the lines the form itself could have produced are kept (a known,
    offered sub-code, numeric hours and frais): the seed is display, the
    next save re-validates everything.
    """
    lines = []
    for line in data.get("lines") or []:
        code = str(line.get("sous_phase") or "")
        if code not in phases.SOUS_CODES or not _offered_phase(code):
            continue
        try:
            hours = float(line.get("hours") or 0)
            frais = int(line.get("frais_cents") or 0)
        except (TypeError, ValueError):
            continue
        lines.append({"sous_phase": code, "hours": hours, "frais_cents": frais})
    return {"lines": lines, "hourly_rate": int(data.get("hourly_rate") or 0)}


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
        conflict=None,
        base_version=_version_of(latest),
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
    base_version = _submitted_base_version()
    data = {
        "dossier_id": dossier_id,
        "hourly_rate": _parse_cents(f.get("hourly_rate")),
        "note": f.get("note", "").strip(),
        "lines": _parse_budget_lines_json(f.get("lines_json", "")),
    }
    return_to = f.get("return_to", "")

    budget, errors = budget_model.create_budget(data, base_version=base_version)
    if errors and budget_model.is_version_conflict(errors):
        # A newer version exists: the lawyer's figures come back as typed,
        # under the banner, with the CURRENT version as the new base — the
        # next save is then deliberate. 200, as every stale re-render of an
        # edit form (plan rule 11; a validation error below stays a 400).
        latest = budget_model.get_latest_budget(dossier_id)
        current = _version_of(latest)
        log_dossier_event(
            "budget_version_conflict", dossier_id,
            base_version=base_version, current_version=current,
        )
        conflict = edit_conflict.conflict_context(
            latest,
            compare_url=url_for("budgets.budget_history", dossier_id=dossier_id),
            note=(
                f"La version en vigueur est la v{current} : enregistrer "
                f"créera la v{current + 1}, qui deviendra la version de "
                f"référence — la v{current} reste consultable dans "
                "l'historique."
            ),
        )
        return render_template(
            "budgets/form.html",
            dossier=dossier,
            latest=latest,
            seed=_form_seed(dossier, _submitted_as_version(data)),
            errors=[],
            conflict=conflict,
            base_version=current,
            note_value=data["note"],
            return_to=return_to,
        )
    if errors:
        latest = budget_model.get_latest_budget(dossier_id)
        # The SUBMITTED base travels back (plan rule 11): the original
        # version keeps protecting the retry — and a page that carried none
        # re-renders without the field.
        return render_template(
            "budgets/form.html",
            dossier=dossier,
            latest=latest,
            seed=_form_seed(dossier, latest),
            errors=errors,
            conflict=None,
            base_version=base_version,
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
