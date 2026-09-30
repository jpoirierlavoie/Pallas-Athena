"""Budget Firestore model — per-dossier fee/disbursement estimates by phase.

The first sequel of Phase O (SPEC_PHASE_O_PHASAGE.md §10): the protocol ↔
budget ↔ time join. A budget is a set of lines keyed by litigation-phase
SUB-CODE (utils/phases.py), each carrying estimated hours and disbursements;
fees derive from hours × the budget's frozen hourly rate.

APPEND-ONLY, VERSIONED — deliberately NO ``update_*``/``delete_*`` (the
trust/audit_event pattern). Every save mints a NEW immutable version; the
newest one is authoritative and the older ones stay readable. The reason is
deontological, not technical (spec §10): the duty to inform the client of
the foreseeable cost — and of anything likely to change it — requires
proving WHEN that information was given. An overwritten budget destroys
that proof.

Versioning is a TRANSACTIONAL per-dossier counter since lot 3a (step 2),
``counters/budget-{dossier_id}`` — the invoice/trust/admin counter pattern.
It used to be Python-side (1 + the max of a non-transactional read): two
parallel saves (a double-submit, a second tab, the connector's future
``create_budget_version``) both read the same history and minted two
documents under ONE version number, and a « based on version N » check on
top of it would have meant nothing. :func:`create_budget` now reads the
counter AND the dossier's stored versions inside ONE transaction, refuses
a stale ``base_version``, and writes the new version and the counter
together. ``get_latest_budget`` keeps its ``(version, created_at)``
tie-break for the duplicates minted before.

Phase is DERIVED from the sub-code prefix, never stored per line (the
prefix IS the relationship — the phases.py doctrine). ADM and HOR are
REFUSED in a budget (D-14: withdrawn from the client quote). Unlike the
three phased collections, ``sous_phase`` here is required non-empty — a
budget has no DAV/MCP/legacy write path, so the hard requirement is safe.
"""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import db, provenance
from security import sanitize
from utils import phases
from utils.logging_setup import log_unexpected

logger = logging.getLogger(__name__)

COLLECTION = "budgets"
COUNTERS_COLLECTION = "counters"

# The keys a caller may give a new version; the rest (id, version, the
# provenance stamp) is the model's. Anything else a caller sends is IGNORED
# — never stored.
_CREATE_KEYS = ("dossier_id", "hourly_rate", "note", "lines")

# Consumption alert threshold (spec §10: the trigger of the deontological
# duty to inform the client BEFORE the envelope is exceeded). Computed on
# DOLLARS (fees + disbursements), not hours — dollars are what was quoted.
ALERT_THRESHOLD_PCT = 80.0


def _default_doc() -> dict:
    """Every budget field with its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "version": 0,          # int ≥ 1, per-dossier, minted at create
        "hourly_rate": 0,      # cents/hour — FROZEN into the version
        "note": "",            # free-text assumptions, optional
        "lines": [],           # [{"sous_phase", "hours", "frais_cents"}]
        "created_at": None,
        # Immutable doc: updated_at/etag never change after create — kept
        # anyway (Architecture Rule 7 uniformity; the documented exceptions
        # all involve derived-key doc IDs, which this is not).
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


# ── Pure layer (no Firestore — carries the test suite) ─────────────────────


def _normalize_lines(raw_lines: list) -> tuple[list[dict], list[str]]:
    """Coerce and validate budget lines.

    The repeater discipline (the prescription_events pattern): a line whose
    hours AND frais are both zero is dropped SILENTLY (the form seeds every
    sub-code at zero — only meaningful lines persist); a malformed one
    errors loudly in French. Returns (clean_lines, errors).
    """
    errors: list[str] = []
    clean: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw_lines, list):
        return [], ["Lignes de budget invalides."]
    for entry in raw_lines:
        if not isinstance(entry, dict):
            errors.append("Ligne de budget invalide.")
            continue
        code = str(entry.get("sous_phase") or "").strip()
        try:
            hours = float(entry.get("hours") or 0)
        except (TypeError, ValueError):
            errors.append(f"Heures invalides sur la ligne « {code or '?'} ».")
            continue
        try:
            frais = int(entry.get("frais_cents") or 0)
        except (TypeError, ValueError):
            errors.append(f"Frais invalides sur la ligne « {code or '?'} ».")
            continue
        if not math.isfinite(hours) or hours < 0:
            errors.append(f"Heures invalides sur la ligne « {code or '?'} ».")
            continue
        if frais < 0:
            errors.append(f"Frais invalides sur la ligne « {code or '?'} ».")
            continue
        if hours == 0 and frais == 0:
            continue  # seeded row left untouched — dropped silently
        if code not in phases.SOUS_CODES:
            errors.append(f"Sous-code de phase inconnu : « {code or '?' } ».")
            continue
        if phases.phase_of(code) in phases.PHASES_NON_FACTURABLES:
            # D-14: ADM/HOR are withdrawn from the client quote. The form
            # never offers them; a hand-crafted POST is refused here.
            errors.append(
                "Les phases ADM et HOR ne figurent pas dans un budget "
                "d'honoraires."
            )
            continue
        if code in seen:
            errors.append(f"Sous-code en double : « {code} ».")
            continue
        seen.add(code)
        clean.append({
            "sous_phase": code,
            "hours": round(hours, 2),
            "frais_cents": frais,
        })
    return clean, errors


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    if not str(data.get("dossier_id") or "").strip():
        errors.append("Un dossier doit être associé au budget.")
    rate = data.get("hourly_rate", 0)
    if not isinstance(rate, int) or rate < 0:
        errors.append("Le taux horaire est invalide.")
    version = data.get("version", 0)
    if not isinstance(version, int) or version < 1:
        errors.append("Version de budget invalide.")
    if not data.get("lines"):
        errors.append("Le budget doit contenir au moins une ligne.")
    return errors


def _next_version(existing: list[dict]) -> int:
    """1 + the highest existing version (1 on an empty history)."""
    if not existing:
        return 1
    return 1 + max(int(b.get("version") or 0) for b in existing)


# Pure budget arithmetic lives in utils/budget_math.py since the audit of
# 2026-08-26 (utils/budget_pdf.py was the tree's only utils->models import).
# Re-exported here so every existing models.budget import path and test
# survives -- the taxonomie/phases re-export shape.
from utils.budget_math import (  # noqa: E402,F401  (re-exports)
    _phase_order,
    budget_totals,
    group_lines_by_phase,
)


def aggregate_actuals(time_entries: list[dict], expenses: list[dict]) -> dict:
    """Actual consumption by sub-code/phase, from already-loaded lists.

    Callers load ``list_time_entries(dossier_id=…)`` and
    ``list_expenses(dossier_id=…)`` ONCE and pass the lists — never a second
    scan. Time: only BILLABLE entries count (hours AND amounts — the
    practitioner's decision: consumption as it is worked, the deontological
    trigger, not invoicing). Expenses: all count. Bucket resolution: the
    entry's ``sous_phase`` when valid; else its ``phase`` imputed to the
    phase's ``-00``; else the ``unphased`` bucket — legacy work is shown
    apart, NEVER silently lost.
    """
    def _bucket(doc: dict) -> Optional[str]:
        sc = str(doc.get("sous_phase") or "")
        if sc in phases.SOUS_CODES:
            return sc
        ph = str(doc.get("phase") or "")
        if ph in phases.PHASES:
            return phases.default_sous_phase(ph)
        return None

    by_sous: dict[str, dict] = {}
    unphased = {"hours": 0.0, "fees_cents": 0, "frais_cents": 0}

    def _slot(code: Optional[str]) -> dict:
        if code is None:
            return unphased
        return by_sous.setdefault(
            code, {"hours": 0.0, "fees_cents": 0, "frais_cents": 0}
        )

    for entry in time_entries:
        if not entry.get("billable"):
            continue
        slot = _slot(_bucket(entry))
        slot["hours"] += float(entry.get("hours") or 0)
        slot["fees_cents"] += int(entry.get("amount") or 0)

    for exp in expenses:
        slot = _slot(_bucket(exp))
        slot["frais_cents"] += int(exp.get("amount") or 0)

    by_phase: dict[str, dict] = {}
    for code, vals in by_sous.items():
        parent = phases.phase_of(code)
        agg = by_phase.setdefault(
            parent, {"hours": 0.0, "fees_cents": 0, "frais_cents": 0}
        )
        agg["hours"] += vals["hours"]
        agg["fees_cents"] += vals["fees_cents"]
        agg["frais_cents"] += vals["frais_cents"]

    for slot in list(by_sous.values()) + list(by_phase.values()) + [unphased]:
        slot["hours"] = round(slot["hours"], 2)

    return {"by_sous_phase": by_sous, "by_phase": by_phase,
            "unphased": unphased}


def _level(pct: Optional[float]) -> str:
    if pct is None:
        return "none"
    if pct > 100.0:
        return "over"
    if pct >= ALERT_THRESHOLD_PCT:
        return "warn"
    return "ok"


def build_budget_view(budget: Optional[dict], actuals: dict) -> dict:
    """Merge a budget (may be None) with actuals into display rows.

    One row per phase that is either budgeted or consumed; the percentage is
    computed on DOLLARS (fees + frais) — the cost quoted to the client — and
    is None when the phase has no envelope (never a ZeroDivisionError). An
    ``unphased`` row surfaces legacy consumption with no phase code.
    """
    groups = group_lines_by_phase(
        budget.get("lines", []), int(budget.get("hourly_rate") or 0)
    ) if budget else []
    budgeted = {g["phase"]: g for g in groups}
    actual_by_phase = actuals.get("by_phase", {})

    rows: list[dict] = []
    for code in _phase_order():
        g = budgeted.get(code)
        a = actual_by_phase.get(code)
        if not g and not a:
            continue
        b_fees = g["subtotal"]["fees_cents"] if g else 0
        b_frais = g["subtotal"]["frais_cents"] if g else 0
        b_hours = g["subtotal"]["hours"] if g else 0.0
        a_fees = a["fees_cents"] if a else 0
        a_frais = a["frais_cents"] if a else 0
        a_hours = a["hours"] if a else 0.0
        b_total = b_fees + b_frais
        a_total = a_fees + a_frais
        pct = (a_total / b_total * 100.0) if b_total > 0 else None
        rows.append({
            "phase": code,
            "libelle": phases.PHASE_LABELS.get(code, code),
            "budget_hours": b_hours,
            "budget_cents": b_total,
            "actual_hours": round(a_hours, 2),
            "actual_cents": a_total,
            "ecart_cents": b_total - a_total,
            "pct": round(pct, 1) if pct is not None else None,
            "level": _level(pct),
        })

    unphased = actuals.get("unphased") or {}
    unphased_total = int(unphased.get("fees_cents") or 0) + int(
        unphased.get("frais_cents") or 0
    )
    has_unphased = unphased_total > 0 or float(unphased.get("hours") or 0) > 0

    totals_b = budget_totals(budget) if budget else {
        "hours": 0.0, "fees_cents": 0, "frais_cents": 0, "total_cents": 0,
    }
    total_actual = sum(r["actual_cents"] for r in rows) + unphased_total
    total_actual_hours = round(
        sum(r["actual_hours"] for r in rows)
        + float(unphased.get("hours") or 0), 2
    )
    total_pct = (
        total_actual / totals_b["total_cents"] * 100.0
        if totals_b["total_cents"] > 0 else None
    )
    return {
        "rows": rows,
        "unphased": {**unphased, "total_cents": unphased_total},
        "has_unphased": has_unphased,
        "budget_total_cents": totals_b["total_cents"],
        "budget_total_hours": totals_b["hours"],
        "actual_total_cents": total_actual,
        "actual_total_hours": total_actual_hours,
        "ecart_total_cents": totals_b["total_cents"] - total_actual,
        "pct": round(total_pct, 1) if total_pct is not None else None,
        "level": _level(total_pct),
    }


# ── Firestore layer (append-only — NO update_*, NO delete_*) ───────────────


# The save's refusal when the version history cannot be read (lot 0b).
VERSION_READ_ERROR = (
    "Les versions existantes de ce budget n'ont pas pu être lues : aucune "
    "version n'a été enregistrée, pour ne pas en créer une en double. "
    "Réessayez dans un instant ; les montants saisis n'ont pas été conservés."
)


# The save's refusal when a newer version was minted since the caller read
# the budget (lot 3a) — a fixed sentence, so a caller recognises it
# (:func:`is_version_conflict`) without parsing French.
BASE_VERSION_CONFLICT = (
    "Une version plus récente de ce budget a été enregistrée entre-temps — "
    "dans un autre onglet ou par le connecteur. Rien n'a été enregistré : "
    "relisez la version actuelle, puis enregistrez de nouveau."
)
BASE_VERSION_INVALID = "Version de référence du budget invalide."
DOSSIER_ID_INVALID = "Un dossier doit être associé au budget."


class _Refused(Exception):
    """A refusal raised inside the save's transaction — nothing written."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(errors[0] if errors else "")
        self.errors = errors


class _ReadFailed(Exception):
    """The counter or the stored versions could not be read."""


def is_version_conflict(errors) -> bool:
    """True when a :func:`create_budget` error list is the stale-base refusal."""
    return BASE_VERSION_CONFLICT in list(errors or ())


def _counter_ref(dossier_id: str):
    return db.collection(COUNTERS_COLLECTION).document(f"budget-{dossier_id}")


def create_budget(
    data: dict, *, base_version: Optional[int] = None,
) -> tuple[Optional[dict], list[str]]:
    """Mint a NEW immutable version. Returns (doc, errors).

    ONE transaction (lot 3a, step 2), reads before writes:

    1. the per-dossier counter ``counters/budget-{dossier_id}`` and the
       dossier's stored versions — the current version is the higher of
       the two (the stored versions cover every version minted before the
       counter existed, and any an old instance mints during a rolling
       deploy). A read failure REFUSES (:data:`VERSION_READ_ERROR`): a
       version number minted from a failed read would be a duplicate — the
       lot 0b rule, now inside the transaction;
    2. *base_version* given and not the current version →
       :data:`BASE_VERSION_CONFLICT`, nothing written: the caller built its
       budget on an older version (a stale tab, a concurrent save, the
       connector). ``None`` asserts nothing — the web page rendered before
       its form carried the field. ``0`` means « no budget yet »;
    3. the validation (lines, rate, dossier);
    4. the new version (current + 1) and the counter, written together.

    Two parallel saves on the same base: the first commits, the second's
    transaction aborts on the counter it read, and its retry sees the new
    version and refuses (with a base) or takes the next number (without) —
    never two documents under one number.

    Only the keys of :data:`_CREATE_KEYS` are taken from *data*; the version
    and the provenance stamp (``created_via`` included — the path actually
    writing) are the model's.
    """
    if base_version is not None and (
        isinstance(base_version, bool) or not isinstance(base_version, int)
        or base_version < 0
    ):
        return None, [BASE_VERSION_INVALID]
    fields = {k: data[k] for k in _CREATE_KEYS if k in (data or {})}
    merged = {**_default_doc(), **_sanitize_data(fields)}
    lines, line_errors = _normalize_lines(merged.get("lines") or [])
    merged["lines"] = lines

    dossier_id = str(merged.get("dossier_id") or "").strip()
    if not dossier_id or "/" in dossier_id:
        return None, line_errors + [DOSSIER_ID_INVALID]
    merged["dossier_id"] = dossier_id
    counter_ref = _counter_ref(dossier_id)
    budget_id = str(uuid.uuid4())
    budget_ref = db.collection(COLLECTION).document(budget_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        try:
            snap = counter_ref.get(transaction=transaction)
            stored = _list_budget_versions_strict(
                dossier_id, transaction=transaction)
        except Exception as exc:
            raise _ReadFailed() from exc
        seq = int((snap.to_dict() or {}).get("seq") or 0) if snap.exists else 0
        current = max(seq, _next_version(stored) - 1)
        if base_version is not None and base_version != current:
            raise _Refused([BASE_VERSION_CONFLICT])

        doc = dict(merged)
        doc["version"] = current + 1
        errors = line_errors + _validate(doc)
        if errors:
            raise _Refused(errors)
        now = datetime.now(timezone.utc)
        doc.update({"id": budget_id, **provenance.create_fields(now)})
        transaction.set(counter_ref, {"seq": current + 1, "updated_at": now})
        transaction.create(budget_ref, doc)
        return doc

    try:
        doc = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except _ReadFailed:
        log_unexpected("budget version read failed", dossier_id=dossier_id)
        return None, [VERSION_READ_ERROR]
    except Exception:
        log_unexpected("budget write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, budget_id)
    return doc, []


def get_budget(budget_id: str) -> Optional[dict]:
    try:
        doc = db.collection(COLLECTION).document(budget_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_budget failed: %s", exc)
    return None


def _list_budget_versions_strict(
    dossier_id: str, *, transaction=None,
) -> list[dict]:
    """All versions of a dossier's budget, newest first — PROPAGATES a read
    error. The reader for a SAVE (``create_budget``, through its
    *transaction*): a version number minted from a failed read would be a
    duplicate. Display paths use the fail-open :func:`list_budget_versions`.

    ``where dossier_id ==`` + PYTHON sort on (version, created_at) DESC —
    deliberately no Firestore order_by, so no composite index. The
    created_at tie-break keeps get_latest_budget deterministic even if a
    double-submit ever minted two docs with the same version number.
    """
    if not dossier_id:
        return []
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )
    rows = [doc.to_dict() for doc in query.stream(transaction=transaction)]
    rows.sort(
        key=lambda b: (
            int(b.get("version") or 0),
            b.get("created_at")
            or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return rows


def list_budget_versions_strict(dossier_id: str) -> list[dict]:
    """All versions, newest first — PROPAGATES a read error. The public
    door of :func:`_list_budget_versions_strict` for a caller outside this
    module that must tell « no budget » from « unreadable »: the
    connector's ``get_budget`` and ``create_budget_version`` (lot 3b), whose
    answer a write — or a client quote — is based on."""
    return _list_budget_versions_strict(dossier_id)


def list_budget_versions(dossier_id: str) -> list[dict]:
    """All versions of a dossier's budget, newest first — FAILS OPEN to
    ``[]`` (the history page, the Budget tab, the form's seed: display
    only). Never mint a version number from it; see
    :func:`_list_budget_versions_strict`."""
    try:
        return _list_budget_versions_strict(dossier_id)
    except Exception as exc:
        logger.warning("list_budget_versions failed: %s", exc)
        return []


def get_latest_budget(dossier_id: str) -> Optional[dict]:
    versions = list_budget_versions(dossier_id)
    return versions[0] if versions else None
