"""Expense Firestore CRUD and summary functions."""

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, billing_move, concurrency, db, provenance
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils import phases
from utils.logging_setup import log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "expenses"

# Valid expense categories
VALID_CATEGORIES = (
    "signification",
    "expertise",
    "transcription",
    "deplacement",
    "photocopie",
    "timbre_judiciaire",
    "autre",
)

# Display labels (French)
CATEGORY_LABELS = {
    "signification": "Signification",
    "expertise": "Expertise",
    "transcription": "Transcription",
    "deplacement": "Déplacement",
    "photocopie": "Photocopie",
    "timbre_judiciaire": "Timbre judiciaire",
    "autre": "Autre",
}

# Phase-of-litigation vocabulary (Phase O, axis 1) — lives in utils/phases.py,
# NOT here. ORTHOGONAL to `category` above (D-11: type of disbursement ≠
# phase of litigation — never overload one with the other).
VALID_PHASES = phases.VALID_PHASES
VALID_SOUS_PHASES = phases.VALID_SOUS_PHASES
PHASE_LABELS = phases.PHASE_LABELS
SOUS_PHASE_LABELS = phases.SOUS_PHASE_LABELS

# Keys :func:`update_expense` never takes from its caller — the twin of
# ``time_entry._PROTECTED_ON_UPDATE`` (read its comment): the billing link
# (``models.invoice`` alone writes it), the identity, and the write's own
# provenance stamp.
_PROTECTED_ON_UPDATE = frozenset({
    "id", "invoiced", "invoice_id",
    "created_at", "created_via",
    "updated_at", "updated_via", "mcp_updated_at", "etag",
})


class _Refused(Exception):
    """A refusal raised inside a write transaction — nothing is written.

    ``errors`` is the French list the ``(doc, errors)`` convention returns.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__(errors[0] if errors else "")
        self.errors = errors


def _default_doc() -> dict:
    """Return a dict with every expense field set to its default value."""
    return {
        "id": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "date": None,
        "description": "",
        "category": "autre",
        # Phase O — "" = non renseignée (legacy docs are never backfilled)
        "phase": "",
        "sous_phase": "",
        "amount": 0,          # cents
        "taxable": True,
        "receipt_document_id": None,
        "invoiced": False,
        "invoice_id": None,
        # Identifiant de l'enregistrement dans le système d'origine,
        # posé par la reprise historique (août 2026) ; « » partout
        # ailleurs. C'est l'ancre anti-doublon DURABLE : la clé
        # d'idempotence MCP expire en 24 h et une reprise s'étale sur
        # des jours, donc seul un identifiant porté par la donnée
        # elle-même permet à une reprise interrompue de retrouver ce
        # qu'elle a déjà écrit. Jamais sérialisé en vCard ni en iCal.
        "legacy_ref": "",
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette dépense.")

    if not data.get("date"):
        errors.append("La date est requise.")

    if not data.get("description", "").strip():
        errors.append("La description est requise.")

    category = data.get("category", "")
    if category and category not in VALID_CATEGORIES:
        errors.append("Catégorie invalide.")

    # math.isfinite rejects NaN/Infinity (NaN passes "<= 0" comparisons)
    amount = data.get("amount", 0)
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        errors.append("Le montant doit être supérieur à zéro.")

    errors.extend(phases.validate_pair(data))

    return errors


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_expense(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    phases.apply_sous_phase_default(merged)

    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    expense_id = str(uuid.uuid4())

    merged["id"] = expense_id
    provenance.stamp_create(merged, now)

    try:
        db.collection(COLLECTION).document(expense_id).set(merged)
    except Exception:
        log_unexpected("expense write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, expense_id)

    return merged, []


def get_expense_strict(expense_id: str) -> Optional[dict]:
    """One expense — a read failure PROPAGATES; ``None`` only when the store
    answered « no such document » (see ``time_entry.get_time_entry_strict``)."""
    doc = db.collection(COLLECTION).document(expense_id).get()
    if doc.exists:
        return doc.to_dict()
    return None


def get_expense(expense_id: str) -> Optional[dict]:
    """Fetch a single expense by ID (fail-open: ``None`` on a read error)."""
    try:
        return get_expense_strict(expense_id)
    except Exception as exc:
        logger.warning("get_expense failed for %s: %s", sanitize_log_value(expense_id), exc)
    return None


def list_expenses(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return expenses, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if billable_filter == "non_facture":
            results = [r for r in results if not r.get("invoiced")]

        if date_from:
            results = [r for r in results if r.get("date") and r["date"] >= date_from]
        if date_to:
            results = [r for r in results if r.get("date") and r["date"] <= date_to]

        # Sort by date descending
        results.sort(
            key=lambda e: e.get("date") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        return results
    except Exception:
        return []


def _filtered_query(
    dossier_id: Optional[str],
    billable_filter: Optional[str],
    date_from: Optional[datetime],
    date_to: Optional[datetime],
) -> "firestore.Query":
    """Build the filtered, (date DESC, id DESC)-ordered expense query.

    Shared by :func:`list_expenses_page` and
    :func:`get_filtered_expense_totals` so the exact same composite index
    serves both the page reads and the totals aggregation. The ``date``
    range filters ride the primary order field (no extra index dimension).
    Only ``non_facture`` is meaningful for expenses (mirrors
    :func:`list_expenses`); the dossier_id + billable_filter combination is
    NOT supported server-side — callers route it through the legacy
    :func:`list_expenses` full scan.

    The refusal is ENFORCED here, not only documented (audit 2026-08-26) —
    same rationale as time_entry._filtered_query: every failure path
    swallows FAILED_PRECONDITION into empty/zero, so an unguarded caller
    would produce a silently empty list with a $0 total.
    """
    if dossier_id and billable_filter:
        raise ValueError(
            "combinaison dossier_id + billable_filter non indexée — "
            "passer par list_expenses (balayage legacy)"
        )
    query = db.collection(COLLECTION)
    if dossier_id:
        query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))
    if billable_filter == "non_facture":
        query = query.where(filter=FieldFilter("invoiced", "==", False))
    if date_from:
        query = query.where(filter=FieldFilter("date", ">=", date_from))
    if date_to:
        query = query.where(filter=FieldFilter("date", "<=", date_to))
    return (
        query
        .order_by("date", direction=firestore.Query.DESCENDING)
        .order_by("id", direction=firestore.Query.DESCENDING)
    )


def list_expenses_page(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of expenses plus an opaque next-page cursor.

    Firestore-native cursor pagination: ``order_by(date DESC, id DESC)``
    with ``start_after`` — reads ~``limit`` docs per page instead of
    streaming the whole collection (the ``id`` field mirrors the document ID
    and is always set, giving a total order for ties on ``date``).
    ``list_expenses`` remains the full-scan path for exports, summaries and
    the dossier_id + billable_filter combination.
    """
    # Before the try: a caller naming a position TWICE is a programming
    # error, and swallowing it into ([], None) would render an empty list
    # with no explanation anywhere.
    if offset and cursor:
        raise ValueError("cursor et offset s'excluent — chacun nomme une position")
    try:
        query = _filtered_query(dossier_id, billable_filter, date_from, date_to)
        values = decode_cursor(cursor)
        if offset:
            # An ABSOLUTE page read, for a « Fin » / « ±N » leap. Same
            # ordering, so the same index — and Firestore emits ONE query
            # (order_by -> offset -> limit). It bills the skipped documents,
            # which is why `page` is clamped before it ever gets here.
            query = query.offset(offset)
        elif values and len(values) == 2:
            # decode_cursor preserves encode order: [date, id]
            query = query.start_after({"date": values[0], "id": values[1]})
        docs = [d.to_dict() for d in query.limit(limit + 1).stream()]
        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("date"), last.get("id")])
        return docs, next_cursor
    except Exception as exc:
        # PII-free: log the exception only, never filter values or doc content.
        logger.warning("list_expenses_page: paginated query failed: %s", exc)
        return [], None


def count_expenses_page(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> Optional[int]:
    """Rows in the filtered set, or None when the count could not be read.

    Runs on the SAME query object as the page read — order_by INCLUDED — so
    the SAME index serves both. An aggregation forwards its nested query
    verbatim, and a COUNT adds no aggregated field to trail the index
    (contrast the SUM tails). Dropping the order_by makes the backend apply
    its own implicit ordering, which is a THIRD ordering that FAILS —
    measured 2026-09-07: `dossier_id ==` plus a `date` range without the
    order_by answers « the query requires an index ».

    That shared ordering is also what keeps the count HONEST: an order_by
    excludes documents missing the key, from the count and the page read
    alike, so the two can never disagree (cross-checked against a bare
    collection COUNT: gap of 0 on all five collections).

    Returns None, NEVER 0, on failure — « Page 7 / 0 » is a confident lie,
    and a plausible zero is the shape of the June-2026 incident.
    """
    try:
        values = aggregation_values(
            _filtered_query(dossier_id, billable_filter, date_from, date_to)
            .count(alias="n")
            .get()
        )
        n = values.get("n")
        return int(n) if n is not None else None
    except Exception as exc:
        logger.warning("count_expenses_page: aggregation failed: %s", exc)
        return None


def get_filtered_expense_totals(
    dossier_id: Optional[str] = None,
    billable_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> dict:
    """Return ``{"amount": int}`` over the list-view filters.

    Server-side SUM aggregation replacing the legacy "materialize
    everything, sum in Python" total on the /temps/ dépenses tab. Built on
    the same ordered query as :func:`list_expenses_page`, but the
    aggregation needs its own composite index per filter — (filter,
    date DESC, id DESC, amount DESC): the SUM field must trail the index,
    direction matching the sort. Returns a safe zero on failure — a broken
    total must never break the list view.
    """
    try:
        query = _filtered_query(dossier_id, billable_filter, date_from, date_to)
        agg_query = query.sum("amount", alias="amount")
        values = aggregation_values(agg_query.get())
        amount = values.get("amount", 0) or 0
        return {"amount": int(round(amount))}
    except Exception as exc:
        logger.warning("get_filtered_expense_totals: aggregation failed: %s", exc)
        return {"amount": 0}


def update_expense(
    expense_id: str, data: dict, *, expected_etag: Optional[str] = None
) -> tuple[Optional[dict], list[str]]:
    """Update an existing expense. Returns (updated_doc, errors).

    The twin of ``time_entry.update_time_entry`` — read that docstring: ONE
    transaction whatever the caller passes (lot 0b, 2026-09-26), the etag
    comparison and the invoiced refusal on the transaction's own read, and
    the document written merged from it, so an invoice committing between
    the read and the write can no longer have ``invoiced``/``invoice_id``
    written back to ``False``/``None``. ``expected_etag`` (keyword-only): a
    stale one returns ``[STALE_ETAG_ERROR]`` and writes nothing; ``None``
    asserts nothing about the version. The amount stays caller-set, never
    recomputed. A key of :data:`_PROTECTED_ON_UPDATE` in *data* is ignored.
    A CHANGED ``dossier_id`` moves the disbursement by
    :func:`move_expense`'s rule (``models.billing_move``): the target
    re-read in this transaction, its labels taken from that read.
    """
    changes = {
        key: value for key, value in _sanitize_data(data).items()
        if key not in _PROTECTED_ON_UPDATE
    }
    ref = db.collection(COLLECTION).document(expense_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        # Read FIRST; every check below reads this snapshot (the twin's
        # comment says why).
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Dépense introuvable."])
        existing = snap.to_dict() or {}
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        if existing.get("invoiced"):
            raise _Refused(["Impossible de modifier une dépense déjà facturée."])

        merged = {**existing, **changes}
        # A CHANGED dossier is a move: models/billing_move's rule (see the
        # twin in time_entry.update_time_entry).
        target = str(merged.get("dossier_id") or "").strip()
        if target and target != str(existing.get("dossier_id") or ""):
            fields = billing_move.target_fields(transaction, ref, target)
            if fields is None:
                raise _Refused([billing_move.TARGET_NOT_FOUND])
            merged.update(fields)
        phases.apply_sous_phase_default(merged)
        errors = _validate(merged)
        if errors:
            raise _Refused(errors)

        provenance.stamp_update(merged, datetime.now(timezone.utc))
        transaction.set(ref, merged)
        return merged

    try:
        merged = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors
    except Exception:
        log_unexpected("expense write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, expense_id)

    return merged, []


MOVE_INVOICED_ERROR = (
    "Impossible de déplacer une dépense déjà facturée : annulez d'abord la "
    "facture qui la porte — l'annulation libère ses déboursés —, puis "
    "déplacez-la. Rien n'a été déplacé."
)


def move_expense(
    expense_id: str,
    to_dossier: dict,
    *,
    from_dossier_id: str,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """File an UN-INVOICED disbursement under another dossier (lot 3a).

    The twin of ``time_entry.move_time_entry`` — read that docstring: the
    same ``(doc, errors, moved)`` triple, the same six checks in the same
    order inside ONE transaction, the same partial ``update()`` of
    :data:`models.billing_move.LABEL_KEYS` plus the provenance stamp. The
    amount, the category, the taxable flag and the phase stay as stored.
    """
    to_id = billing_move.target_id(to_dossier)
    if not to_id:
        return None, [billing_move.TARGET_REQUIRED], False
    if not billing_move.is_addressable(expense_id):
        return None, ["Dépense introuvable."], False
    ref = db.collection(COLLECTION).document(expense_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, bool]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Dépense introuvable."])
        existing = snap.to_dict() or {}
        if existing.get("invoiced"):
            raise _Refused([MOVE_INVOICED_ERROR])
        stored = str(existing.get("dossier_id") or "")
        if stored == to_id:
            return existing, False
        if stored != str(from_dossier_id or "").strip():
            raise _Refused([billing_move.from_mismatch_error(existing)])
        if not concurrency.matches(existing, expected_etag):
            raise _Refused([concurrency.STALE_ETAG_ERROR])
        fields = billing_move.target_fields(transaction, ref, to_id)
        if fields is None:
            raise _Refused([billing_move.TARGET_NOT_FOUND])
        written = {**fields, **provenance.update_fields(datetime.now(timezone.utc))}
        transaction.update(ref, written)
        return {**existing, **written}, True

    try:
        doc, moved = _apply(db.transaction())
    except _Refused as refusal:
        return None, refusal.errors, False
    except Exception:
        log_unexpected("expense move failed")
        return None, ["Erreur lors du déplacement. Veuillez réessayer."], False
    if moved:
        provenance.note_commit(COLLECTION, expense_id)
    return doc, [], moved


def get_expenses_bulk(expense_ids: list[str]) -> dict[str, dict]:
    """Fetch many disbursements in ONE round-trip. Returns {id: doc}.

    Twin of ``models.time_entry.get_time_entries_bulk``, and it **fails
    CLOSED** for the same reason: its caller is a write path, where a read
    failure degraded to ``{}`` would manufacture a refusal for every item.
    """
    unique_ids = [e for e in dict.fromkeys(expense_ids) if e]
    if not unique_ids:
        return {}
    refs = [db.collection(COLLECTION).document(eid) for eid in unique_ids]
    return {
        snap.id: snap.to_dict() for snap in db.get_all(refs) if snap.exists
    }


def set_expense_phase(
    expense_id: str, phase: str, sous_phase: str,
    *, expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], bool]:
    """Reclassify a disbursement's litigation phase — INVOICED OR NOT.

    Twin of ``models.time_entry.set_time_entry_phase``; read that docstring
    for why the ``invoiced`` wall does not apply to this pair and why the
    write is a partial ``update()`` of the pair plus its stamp rather than a
    merged ``set()``.
    Returns ``(doc, errors, changed)``; an unchanged pair writes nothing.
    ``expected_etag`` follows the twin's contract exactly: a stale one
    refuses a CHANGED pair and writes nothing, an unchanged pair is
    answered before the comparison.
    """
    existing = get_expense(expense_id)
    if not existing:
        return None, ["Dépense introuvable."], False

    resolved, sous = phases.resolve_pair(phase, sous_phase)
    pair = {"phase": resolved, "sous_phase": sous}
    # Validate FIRST: an unknown sub-code leaves the parent underived, and
    # reporting that as « phase requise » would send the caller to fix the
    # wrong half.
    errors = phases.validate_pair(pair)
    if errors:
        return None, errors, False
    if not pair["phase"]:
        # Reclassifying means ASSIGNING a code. « Hors phase » (HOR) is the
        # vocabulary's own answer for unclassifiable work — blanking is a
        # regression this path deliberately cannot perform.
        return None, ["Une phase du litige est requise."], False

    if (existing.get("phase", ""), existing.get("sous_phase", "")) == (
        pair["phase"], pair["sous_phase"]
    ):
        return existing, [], False

    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], False

    now = datetime.now(timezone.utc)
    stamp = provenance.update_fields(now)
    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(expense_id),
            {
                "phase": pair["phase"],
                "sous_phase": pair["sous_phase"],
                **stamp,
            },
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR], False
    except concurrency.Vanished:
        return None, ["Dépense introuvable."], False
    except Exception:
        log_unexpected("expense phase write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], False
    provenance.note_commit(COLLECTION, expense_id)

    return {**existing, **pair, **stamp}, [], True


def delete_expense(expense_id: str) -> tuple[bool, str]:
    """Delete an expense. Returns (success, error_message).

    The twin of ``time_entry.delete_time_entry``: the read, the invoiced
    refusal and the delete in ONE transaction (lot 0b, 2026-09-26), so an
    invoice committing between the check and the delete can no longer leave
    a line item citing a deleted disbursement.
    """
    ref = db.collection(COLLECTION).document(expense_id)

    @firestore.transactional
    def _apply(transaction) -> None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _Refused(["Dépense introuvable."])
        if (snap.to_dict() or {}).get("invoiced"):
            raise _Refused(["Impossible de supprimer une dépense déjà facturée."])
        transaction.delete(ref)

    try:
        _apply(db.transaction())
    except _Refused as refusal:
        return False, refusal.errors[0]
    except Exception:
        log_unexpected("expense delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."
    return True, ""


# ── Summary & batch operations ────────────────────────────────────────────


def get_expense_summary(dossier_id: str) -> dict:
    """Return totals for a dossier: total_expenses, unbilled_expenses."""
    entries = list_expenses(dossier_id=dossier_id)
    total = 0
    unbilled = 0

    for e in entries:
        amt = e.get("amount", 0)
        total += amt
        if not e.get("invoiced"):
            unbilled += amt

    return {
        "total_expenses": total,
        "unbilled_expenses": unbilled,
    }


def get_unbilled_expenses(dossier_id: str) -> list[dict]:
    """Return expenses not yet invoiced for a dossier."""
    entries = list_expenses(dossier_id=dossier_id)
    return [e for e in entries if not e.get("invoiced")]


def list_expenses_strict(dossier_id: str) -> list[dict]:
    """Every disbursement of *dossier_id*, unordered — PROPAGATES a read
    error. The twin of ``time_entry.list_time_entries_strict`` (same
    callers, same reason: an empty read must never become an invoice
    without the dossier's disbursements, nor a budget view showing no
    consumption). Single-field equality: automatic index."""
    if not dossier_id:
        return []
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("dossier_id", "==", dossier_id)
    )
    return [doc.to_dict() or {} for doc in query.stream()]


def get_unbilled_expenses_strict(dossier_id: str) -> list[dict]:
    """:func:`get_unbilled_expenses`'s selection — the same predicate —
    over :func:`list_expenses_strict`: a read error PROPAGATES."""
    return [e for e in list_expenses_strict(dossier_id) if not e.get("invoiced")]


def mark_expenses_invoiced(expense_ids: list[str], invoice_id: str) -> list[str]:
    """Update expenses as invoiced. Returns the IDs that failed to update.

    Note: invoice creation no longer uses this helper — it flips sources
    inside its own transaction. Kept for callers needing a standalone flip.
    """
    now = datetime.now(timezone.utc)
    failed_ids: list[str] = []
    for eid in expense_ids:
        try:
            db.collection(COLLECTION).document(eid).update({
                "invoiced": True,
                "invoice_id": invoice_id,
                **provenance.update_fields(now),
            })
        except Exception as exc:
            logger.warning(
                "mark_expenses_invoiced failed for %s: %s",
                sanitize_log_value(eid), exc,
            )
            failed_ids.append(eid)
    return failed_ids
