"""Budget model — validation, versioning, aggregation, view (pure paths).

CI-only in the phase-fields sense: imports models (Firestore client at
import) but exercises only the pure layer — no I/O.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from models import budget as budget_model
from utils import phases


# ── _normalize_lines ────────────────────────────────────────────────────────


def test_valid_lines_pass_and_zero_rows_drop_silently():
    lines, errors = budget_model._normalize_lines([
        {"sous_phase": "PRE-01", "hours": 2.0, "frais_cents": 0},
        {"sous_phase": "PRE-02", "hours": 0, "frais_cents": 0},   # seeded, untouched
        {"sous_phase": "INT-02", "hours": 0, "frais_cents": 10500},
    ])
    assert errors == []
    assert [ln["sous_phase"] for ln in lines] == ["PRE-01", "INT-02"]


def test_adm_and_hor_are_refused():
    # D-14: withdrawn from the client quote — the form never offers them,
    # the model blocks a hand-crafted POST.
    for code in ("ADM-01", "HOR-00"):
        _, errors = budget_model._normalize_lines(
            [{"sous_phase": code, "hours": 1.0, "frais_cents": 0}]
        )
        assert any("ADM et HOR" in e for e in errors), code


def test_unknown_code_duplicate_and_junk_are_refused():
    _, errors = budget_model._normalize_lines(
        [{"sous_phase": "ZZZ-01", "hours": 1.0, "frais_cents": 0}]
    )
    assert any("inconnu" in e for e in errors)

    _, errors = budget_model._normalize_lines([
        {"sous_phase": "PRE-01", "hours": 1.0, "frais_cents": 0},
        {"sous_phase": "PRE-01", "hours": 2.0, "frais_cents": 0},
    ])
    assert any("double" in e for e in errors)

    _, errors = budget_model._normalize_lines(
        [{"sous_phase": "PRE-01", "hours": "abc", "frais_cents": 0}]
    )
    assert any("Heures invalides" in e for e in errors)

    _, errors = budget_model._normalize_lines(
        [{"sous_phase": "PRE-01", "hours": float("nan"), "frais_cents": 0}]
    )
    assert any("Heures invalides" in e for e in errors)

    _, errors = budget_model._normalize_lines(
        [{"sous_phase": "PRE-01", "hours": 1.0, "frais_cents": -5}]
    )
    assert any("Frais invalides" in e for e in errors)


def test_validate_requires_dossier_lines_and_sane_rate():
    errors = budget_model._validate({
        "dossier_id": "", "hourly_rate": -1, "version": 0, "lines": [],
    })
    joined = " ".join(errors)
    assert "dossier" in joined
    assert "taux horaire" in joined.lower()
    assert "au moins une ligne" in joined
    assert budget_model._validate({
        "dossier_id": "d1", "hourly_rate": 30000, "version": 1,
        "lines": [{"sous_phase": "PRE-01", "hours": 1.0, "frais_cents": 0}],
    }) == []


# ── Versioning ──────────────────────────────────────────────────────────────


def test_next_version():
    assert budget_model._next_version([]) == 1
    assert budget_model._next_version([{"version": 1}, {"version": 2}]) == 3
    # A double-submit duplicate never blocks the sequence.
    assert budget_model._next_version([{"version": 2}, {"version": 2}]) == 3


# ── Totals + grouping ───────────────────────────────────────────────────────


def _budget(lines, rate=30000):
    return {"hourly_rate": rate, "lines": lines}


def test_budget_totals_rounding_mirrors_time_entry():
    b = _budget([
        {"sous_phase": "PRE-01", "hours": 1.5, "frais_cents": 2500},
        {"sous_phase": "CTS-01", "hours": 0.1, "frais_cents": 0},
    ])
    totals = budget_model.budget_totals(b)
    assert totals["hours"] == 1.6
    assert totals["fees_cents"] == 45000 + 3000
    assert totals["frais_cents"] == 2500
    assert totals["total_cents"] == 48000 + 2500


def test_group_lines_by_phase_order_tronc_then_modules():
    b_lines = [
        {"sous_phase": "EXP-02", "hours": 3.0, "frais_cents": 150000},
        {"sous_phase": "PRE-01", "hours": 1.0, "frais_cents": 0},
        {"sous_phase": "AUD-02", "hours": 6.0, "frais_cents": 0},
    ]
    groups = budget_model.group_lines_by_phase(b_lines, 30000)
    assert [g["phase"] for g in groups] == ["PRE", "AUD", "EXP"]
    exp = groups[-1]
    assert exp["subtotal"]["frais_cents"] == 150000
    assert exp["subtotal"]["fees_cents"] == 90000


# ── aggregate_actuals ───────────────────────────────────────────────────────


def test_actuals_billable_only_and_expenses_all():
    actuals = budget_model.aggregate_actuals(
        time_entries=[
            {"sous_phase": "CTS-01", "hours": 2.0, "amount": 60000,
             "billable": True},
            {"sous_phase": "CTS-01", "hours": 5.0, "amount": 0,
             "billable": False},   # excluded: hours AND amount
        ],
        expenses=[
            {"sous_phase": "CTS-01", "amount": 10500},
        ],
    )
    slot = actuals["by_sous_phase"]["CTS-01"]
    assert slot["hours"] == 2.0
    assert slot["fees_cents"] == 60000
    assert slot["frais_cents"] == 10500
    assert actuals["by_phase"]["CTS"]["fees_cents"] == 60000


def test_actuals_phase_only_imputes_to_00_and_unphased_never_lost():
    actuals = budget_model.aggregate_actuals(
        time_entries=[
            {"phase": "INT", "sous_phase": "", "hours": 1.0, "amount": 30000,
             "billable": True},
            {"hours": 4.0, "amount": 120000, "billable": True},  # legacy
        ],
        expenses=[{"amount": 999}],  # legacy expense, no phase
    )
    assert actuals["by_sous_phase"]["INT-00"]["fees_cents"] == 30000
    assert actuals["unphased"]["hours"] == 4.0
    assert actuals["unphased"]["fees_cents"] == 120000
    assert actuals["unphased"]["frais_cents"] == 999


# ── build_budget_view ───────────────────────────────────────────────────────


def _view(budget_lines, actual_entries, rate=30000, expenses=()):
    budget = _budget(budget_lines, rate) if budget_lines is not None else None
    actuals = budget_model.aggregate_actuals(actual_entries, list(expenses))
    return budget_model.build_budget_view(budget, actuals)


def test_view_thresholds():
    lines = [{"sous_phase": "CTS-01", "hours": 10.0, "frais_cents": 0}]
    # 10 h × 300 $ = 3000,00 $ budget.

    def entry(amount):
        return [{"sous_phase": "CTS-01", "hours": 1.0, "amount": amount,
                 "billable": True}]

    assert _view(lines, entry(239_700))["rows"][0]["level"] == "ok"    # 79.9 %
    assert _view(lines, entry(240_000))["rows"][0]["level"] == "warn"  # 80 %
    assert _view(lines, entry(300_300))["rows"][0]["level"] == "over"  # 100.1 %


def test_view_zero_budget_phase_with_consumption():
    view = _view(
        [{"sous_phase": "PRE-01", "hours": 1.0, "frais_cents": 0}],
        [{"sous_phase": "EXP-02", "hours": 2.0, "amount": 60000,
          "billable": True}],
    )
    exp_row = next(r for r in view["rows"] if r["phase"] == "EXP")
    assert exp_row["budget_cents"] == 0
    assert exp_row["pct"] is None          # never a ZeroDivisionError
    assert exp_row["level"] == "none"


def test_view_unphased_row_and_totals():
    view = _view(
        [{"sous_phase": "PRE-01", "hours": 10.0, "frais_cents": 0}],
        [{"hours": 2.0, "amount": 60000, "billable": True}],  # unphased
    )
    assert view["has_unphased"] is True
    assert view["unphased"]["total_cents"] == 60000
    # Unphased consumption still counts toward the global figure.
    assert view["actual_total_cents"] == 60000
    assert view["budget_total_cents"] == 300000
    assert view["pct"] == 20.0


def test_view_without_budget():
    view = _view(None, [{"sous_phase": "CTS-01", "hours": 1.0,
                         "amount": 30000, "billable": True}])
    assert view["budget_total_cents"] == 0
    assert view["pct"] is None
    assert view["rows"][0]["phase"] == "CTS"


# ── cabinet ────────────────────────────────────────────────────────────────


def test_cabinet_dict_carries_telecopieur():
    from utils.cabinet import cabinet_dict

    assert "telecopieur" in cabinet_dict()


# ── Budget PDF (utils/budget_pdf.py — the two client-facing variants) ──────


_CABINET = {
    "nom": "Me Jason Poirier Lavoie",
    "adresse_civique": "4970, chemin de la Côte-des-Neiges, suite 9",
    "ville": "Montréal",
    "province": "QC",
    "code_postal": "H3V 1A4",
    "telephone": "(514) 737-2525",
    "telecopieur": "(514) 737-6565",
    "courriel": "reception@poirierlavoie.ca",
}

_DOSSIER = {"file_number": "2026-001", "title": "Tremblay c. Lavoie"}


def _sample_budget():
    from datetime import datetime, timezone

    return {
        "id": "b-1",
        "dossier_id": "d-1",
        "version": 2,
        "hourly_rate": 30000,
        "note": "Hypothèse : dossier contesté.",
        "lines": [
            {"sous_phase": "PRE-01", "hours": 1.5, "frais_cents": 0},
            {"sous_phase": "CTS-01", "hours": 8.0, "frais_cents": 10500},
            {"sous_phase": "EXP-02", "hours": 3.0, "frais_cents": 250000},
        ],
        "created_at": datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    }


def _sample_actuals():
    from models import budget as budget_model

    return budget_model.aggregate_actuals(
        [
            {"sous_phase": "CTS-01", "hours": 2.0, "amount": 60000,
             "billable": True},
            {"sous_phase": "CTS-02", "hours": 1.0, "amount": 30000,
             "billable": True},   # consumed but NOT budgeted — must appear
            {"hours": 4.0, "amount": 120000, "billable": True},  # unphased
        ],
        [{"sous_phase": "EXP-02", "amount": 50000}],
    )


def test_budget_pdf_both_variants_serve_noto_serif_only():
    from models import budget as budget_model
    from utils.budget_pdf import build_budget_pdf

    budget = _sample_budget()
    actuals = _sample_actuals()
    view = budget_model.build_budget_view(budget, actuals)

    for variant, view_arg, actuals_arg in (
        ("estimation", None, None),
        ("suivi", view, actuals),
    ):
        resp = build_budget_pdf(
            variant=variant, dossier=_DOSSIER, budget=budget,
            view=view_arg, actuals=actuals_arg, cabinet=_CABINET,
            filename=f"budget_{variant}.pdf",
        )
        assert resp.status_code == 200, variant
        assert resp.mimetype == "application/pdf"
        assert resp.data.startswith(b"%PDF")
        # Font purity — the paginated firm footer is exercised (full cabinet
        # dict), so an implicit-Helvetica drawString would be caught here.
        assert b"NotoSerif" in resp.data, variant
        assert b"Helvetica" not in resp.data, variant


def test_budget_pdf_story_carries_fr_ca_amounts_and_disclaimer():
    # PDF content streams are compressed — inspect the story layer instead.
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph

    from models import budget as budget_model
    from utils import budget_pdf

    budget = _sample_budget()
    actuals = _sample_actuals()
    view = budget_model.build_budget_view(budget, actuals)

    def _texts(flowables, out):
        for f in flowables:
            if isinstance(f, Paragraph):
                out.append(f.text)
            inner = getattr(f, "_content", None)
            if inner:
                _texts(inner, out)
            data = getattr(f, "_cellvalues", None)
            if data:
                for row in data:
                    for cell in row:
                        out.append(str(cell))
        return out

    story = budget_pdf._build_story(
        "suivi", _DOSSIER, budget, view, actuals,
        budget_pdf._styles(), LETTER[1] - 30 * mm,
    )
    texts = _texts(story, [])
    joined = "\n".join(texts)
    # fr-CA money: NBSP thousands + comma decimals (8 h × 300 $ = 2 400,00 $).
    assert "2 400,00 $" in joined
    # The consumed-but-unbudgeted sub-code appears.
    assert "Demande reconventionnelle" in joined
    # Legacy unphased consumption is shown, never lost.
    assert "Non renseignée" in joined
    # Disclaimer verbatim + grand total + hourly rate block.
    assert "Les taxes de vente sont en sus." in joined
    assert "GRAND TOTAL" in joined
    assert "300,00 $" in joined


def test_budget_pdf_unknown_variant_refused_at_route_level():
    from routes.budgets import VALID_VARIANTES

    assert VALID_VARIANTES == ("estimation", "suivi")
