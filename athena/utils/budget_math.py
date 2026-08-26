"""Budget arithmetic — pure (no Firestore, no Flask).

Hoisted from ``models/budget.py`` (audit 2026-08-26): these helpers are
pure dict/list arithmetic over the Phase-O vocabulary, and
``utils/budget_pdf.py`` importing them from ``models`` was the tree's only
utils→models import (a lazy one, forced by ``models/__init__``'s
``firestore.Client()`` at import). The sanctioned direction is the
opposite — pure logic lives in ``utils`` and the model RE-EXPORTS (the
``taxonomie``/``phases`` shape), so every existing
``models.budget.budget_totals`` import path survives.
"""

import math

from utils import phases


def line_fees_cents(hours: float, rate: int) -> int:
    """hours × rate in integer cents (mirror of time_entry._compute_amount)."""
    product = hours * rate
    if not math.isfinite(product):
        return 0
    return int(round(product))


def budget_totals(budget: dict) -> dict:
    """Whole-budget totals: hours, fees, frais, grand total (cents)."""
    rate = int(budget.get("hourly_rate") or 0)
    hours = 0.0
    fees = 0
    frais = 0
    for line in budget.get("lines", []):
        h = float(line.get("hours") or 0)
        hours += h
        fees += line_fees_cents(h, rate)
        frais += int(line.get("frais_cents") or 0)
    return {
        "hours": round(hours, 2),
        "fees_cents": fees,
        "frais_cents": frais,
        "total_cents": fees + frais,
    }


def _phase_order() -> list[str]:
    """Display order: the ordered tronc, then modules in PHASES order."""
    ordered = list(phases.TRONC_ORDONNE)
    ordered += [
        code for code, p in phases.PHASES.items()
        if p.categorie == "module"
    ]
    return ordered


def group_lines_by_phase(lines: list[dict], hourly_rate: int) -> list[dict]:
    """Budget lines grouped by parent phase, with per-line fees and subtotals.

    Group order: TRONC_ORDONNE first, then modules in PHASES insertion
    order. Only phases that actually have lines appear.
    """
    by_phase: dict[str, list[dict]] = {}
    for line in lines:
        code = line.get("sous_phase", "")
        parent = phases.phase_of(code)
        row = {
            "sous_phase": code,
            "label": phases.SOUS_PHASE_LABELS.get(code, code),
            "hours": float(line.get("hours") or 0),
            "fees_cents": line_fees_cents(
                float(line.get("hours") or 0), hourly_rate
            ),
            "frais_cents": int(line.get("frais_cents") or 0),
        }
        by_phase.setdefault(parent, []).append(row)

    groups: list[dict] = []
    for code in _phase_order():
        rows = by_phase.get(code)
        if not rows:
            continue
        rows.sort(key=lambda r: r["sous_phase"])
        groups.append({
            "phase": code,
            "libelle": phases.PHASE_LABELS.get(code, code),
            "categorie": phases.PHASES[code].categorie,
            "lines": rows,
            "subtotal": {
                "hours": round(sum(r["hours"] for r in rows), 2),
                "fees_cents": sum(r["fees_cents"] for r in rows),
                "frais_cents": sum(r["frais_cents"] for r in rows),
            },
        })
    return groups
