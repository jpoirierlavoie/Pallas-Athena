"""Administration accounting (« comptabilité d'administration ») — August 2026.

The firm-side sibling of the trust register (Phase K, ``models/trust.py``):
ONE collection (``admin_transactions``) carrying the operations bank account
and the corporate credit card — firm revenue and operating expenses, with an
OPTIONAL dossier linkage (rent has none; a bailiff payment may name a file).

A PARALLEL module by design — ``models/trust.py`` is never generalized. The
trust harness is copied (``_TxnAbort`` + French abort map, reads-then-guards-
then-compute-then-writes transactions, the two-step ``en_circulation`` →
``compensée`` | ``annulée`` lifecycle, the reconciliation subsystem with its
as-of resurrection sets and etag sentinel), and exactly three trust-specific
mechanisms are DROPPED, deliberately (user decisions 2026-08-13):

* **No backdating guard, no frozen per-row balances.** ``date`` is the free
  ECONOMIC date (the past is always writable while its period is open; the
  future is refused — a book records what happened); ``sequence`` remains the
  immutable INSERTION order (the audit cursor). Running balances are computed
  AT READ TIME in ``(date, sequence)`` order — the shape trust's own newest
  code (``list_register``) converged on. There is no ``balance_after_*``
  column to keep exact, so there is nothing for a backdated entry to break.
* **Editable until the reconciliation lock.** An entry may be edited or
  deleted while it is not LOCKED; a completed reconciliation locks its whole
  period (see ``_entry_lock_reason`` for the exact predicate). After the
  lock: correction by contre-passation only, exactly like trust. Edits keep
  a bounded on-document ``revisions`` trail; deletions go through the house
  deletions registry (``models/audit_event`` — written by the ROUTE, after
  the committed delete, like every other entity).
* **No overdraft control** (art. 3 is a trust rule — an operations account
  may legitimately overdraw) and therefore no ``cleared`` balance and no
  ``bank_balance`` denormalization. ONE denormalized figure survives:
  ``admin_accounts.ledger_balance`` (Σ of ``admin_delta`` over every row,
  every status — annulée pairs net out), maintained transactionally and
  re-proved by ``scripts/verify_admin_integrity.py``.

Two account types, ONE storage convention: amounts are always positive and
``direction`` carries the sign. A credit-card charge is a ``déboursé`` (the
ledger runs negative; the « Solde dû » shown is the positive owed amount), a
card payment or refund a ``recette``. The account TYPE decides display only
(labels + the statement-sign conversion at reconciliation).

The first section is the pure, Firestore-free layer: the delta/display
arithmetic, the TPS/TVQ ventilation (incl. ``extract_taxes_from_gross``) and
the running-balance computation. It carries the test suite
(``tests/test_admin_ledger.py``). ``reconciliation_variance`` is IMPORTED
from ``models.trust`` — pure, generic bank arithmetic, already tested.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models import db
from models.trust import reconciliation_variance  # pure, generic — import, don't copy
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from tz import to_mtl
from utils.deadlines import today_mtl
from utils.logging_setup import (
    log_admin_ledger_event,
    log_unexpected,
    sanitize_log_value,
)
from utils.tracing_setup import span

logger = logging.getLogger(__name__)

# ── Collection + counter names ─────────────────────────────────────────────
ACCOUNTS_COLLECTION = "admin_accounts"
TRANSACTIONS_COLLECTION = "admin_transactions"
RECONCILIATIONS_COLLECTION = "admin_reconciliations"
COUNTERS_COLLECTION = "counters"

DOSSIERS_COLLECTION = "dossiers"
INVOICES_COLLECTION = "invoices"

# Statuses that count an invoice as issued (encaissement target) — mirrors
# trust._ISSUED_INVOICE_STATUSES (kept local: vocabulary doctrine).
_ISSUED_INVOICE_STATUSES = ("envoyée", "en_retard")


def _counter_id(account_id: str) -> str:
    """Firestore doc id of an account's monotonic sequence counter."""
    return f"admin-{account_id}"


# ── Closed vocabularies ────────────────────────────────────────────────────
VALID_ACCOUNT_TYPES = ("opérations", "carte_crédit")
VALID_ACCOUNT_STATUSES = ("actif", "fermé")

VALID_DIRECTIONS = ("recette", "déboursé")
VALID_TX_STATUSES = ("en_circulation", "compensée", "annulée")

# ``kind`` replaces trust's deontological ``purpose`` vocabulary. The two
# structural kinds (``paiement_carte``, ``correction``) are minted only by
# their dedicated write paths and refused at create/edit.
VALID_KINDS = (
    "encaissement_facture",
    "recette_autre",
    "dépense",
    "paiement_carte",
    "correction",
)
REVERSAL_KIND = "correction"
# The only kinds the create form (and an edit) may carry.
_SIMPLE_KINDS = ("encaissement_facture", "recette_autre", "dépense")
# The only kinds an EDIT may keep or become (invoice-linked entries are
# reverse-only — see ``_entry_lock_reason``).
_EDITABLE_KINDS = ("recette_autre", "dépense")

VALID_METHODS = (
    "chèque", "virement", "prélèvement", "dépôt_direct", "carte", "comptant", "autre",
)

# Firm OPERATING-expense categories — a THIRD vocabulary, never shared with
# the litigation disbursements of ``models/expense.py`` (client-rebillable)
# nor the document categories (house doctrine: vocabularies are never shared
# across entities). A ``huissier``/``sténographe`` déboursé here is the
# FIRM-side payment; the client-billable expense record stays in /temps.
ADMIN_EXPENSE_CATEGORIES = (
    "loyer",
    "internet",
    "téléphone",
    "abonnements",
    "équipement",
    "fournitures",
    "assurances",
    "cotisations_professionnelles",
    "formation",
    "publicité",
    "honoraires_professionnels",
    "huissier",
    "sténographe",
    "frais_bancaires",
    "intérêts",
    "taxes_permis",
    "autre",
)

# ── French display labels ──────────────────────────────────────────────────
ACCOUNT_TYPE_LABELS = {
    "opérations": "Compte d'opérations",
    "carte_crédit": "Carte de crédit",
}
ACCOUNT_STATUS_LABELS = {"actif": "Actif", "fermé": "Fermé"}
BALANCE_LABELS = {"opérations": "Solde", "carte_crédit": "Solde dû"}

DIRECTION_LABELS = {"recette": "Recette", "déboursé": "Déboursé"}
# A card's directions read as charge/payment — storage is identical, only
# the label adapts (templates go through ``direction_labels_for``, never a
# hardcoded string).
CARD_DIRECTION_LABELS = {
    "recette": "Paiement / Remboursement",
    "déboursé": "Charge",
}
TX_STATUS_LABELS = {
    "en_circulation": "En circulation",
    "compensée": "Compensée",
    "annulée": "Annulée",
}
KIND_LABELS = {
    "encaissement_facture": "Encaissement de facture",
    "recette_autre": "Autre recette",
    "dépense": "Dépense",
    "paiement_carte": "Paiement de carte",
    "correction": "Correction",
}
METHOD_LABELS = {
    "chèque": "Chèque",
    "virement": "Virement",
    "prélèvement": "Prélèvement",
    "dépôt_direct": "Dépôt direct",
    "carte": "Carte",
    "comptant": "Comptant",
    "autre": "Autre",
}
ADMIN_CATEGORY_LABELS = {
    "loyer": "Loyer",
    "internet": "Internet",
    "téléphone": "Téléphone",
    "abonnements": "Abonnements",
    "équipement": "Équipement",
    "fournitures": "Fournitures de bureau",
    "assurances": "Assurances",
    "cotisations_professionnelles": "Cotisations professionnelles",
    "formation": "Formation continue",
    "publicité": "Publicité et développement",
    "honoraires_professionnels": "Honoraires professionnels",
    "huissier": "Huissier",
    "sténographe": "Sténographe",
    "frais_bancaires": "Frais bancaires",
    "intérêts": "Intérêts",
    "taxes_permis": "Taxes et permis",
    "autre": "Autre",
}
RECONCILIATION_STATUS_LABELS = {"brouillon": "Brouillon", "complétée": "Complétée"}


def direction_labels_for(account_type: str) -> dict:
    """The Sens labels an account's rows display — charge/paiement on a card."""
    return CARD_DIRECTION_LABELS if account_type == "carte_crédit" else DIRECTION_LABELS


# ── Receipt (pièce justificative) constants ────────────────────────────────
# Deliberately narrower than documents' ALLOWED_EXTENSIONS: a supplier
# invoice is a PDF or a photo, and these five types carry unambiguous magic
# bytes (no PK/OLE2 container disambiguation needed).
RECEIPT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif")
RECEIPT_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}
MAX_RECEIPT_SIZE = 10 * 1024 * 1024  # 10 MB — un reçu est une photo ou un PDF

# Bounded on-document revisions trail (the editable-until-lock audit).
_REVISIONS_CAP = 25


# ═══════════════════════════════════════════════════════════════════════════
# Pure functions — no Firestore, no Flask, no now(). These carry the test
# suite (tests/test_admin_ledger.py).
# ═══════════════════════════════════════════════════════════════════════════

# Québec sales-tax constants for the gross split. LOCAL on purpose (the
# vocabulary doctrine forbids importing another entity's constants);
# ``tests/test_admin_ledger.py`` pins them against invoice.GST_RATE_BPS /
# QST_RATE_BPS so a rate change cannot silently diverge.
_GST = Decimal("0.05")
_QST = Decimal("0.09975")  # NOT compounded on GST (since 2013)
_GROSS_DIVISOR = Decimal("1") + _GST + _QST  # 1.14975
_CENT = Decimal("1")


def admin_delta(direction: str, amount: int) -> int:
    """Signed ledger contribution of one entry: ``+amount`` recette,
    ``-amount`` déboursé — STATUS-BLIND (annulée rows count; they net to
    zero only with their reversal, exactly like trust's book balance). The
    single arithmetic atom of the module."""
    return amount if direction == "recette" else -amount


def display_balance(account_type: str, ledger_balance: int) -> int:
    """The figure shown beside ``BALANCE_LABELS[account_type]``. A card's
    ledger runs NEGATIVE when money is owed; « Solde dû » is the positive
    owed amount."""
    if account_type == "carte_crédit":
        return -ledger_balance
    return ledger_balance


def statement_to_ledger(account_type: str, statement_balance: int) -> int:
    """A card statement states the SOLDE DÛ (positive when owed); the ledger
    runs negative when owed. Convert ONCE, then trust's variance formula
    holds unchanged in ledger sign for both account types."""
    if account_type == "carte_crédit":
        return -statement_balance
    return statement_balance


def extract_taxes_from_gross(gross: int) -> tuple[int, int, int]:
    """Split a taxes-included total into ``(net, tps, tvq)`` integer cents.

    ``net = round(gross / 1.14975)``, then TPS/TVQ forward from the net
    (Decimal, ROUND_HALF_UP — the invoice.compute_totals discipline), the
    rounding remainder imputed to the NET so ``net + tps + tvq == gross``
    EXACTLY. A UI convenience — real receipts carry tips, exempt items and
    partial TPS, so the three fields stay editable after the split."""
    g = int(gross)
    net = int((Decimal(g) / _GROSS_DIVISOR).quantize(_CENT, rounding=ROUND_HALF_UP))
    tps = int((Decimal(net) * _GST).quantize(_CENT, rounding=ROUND_HALF_UP))
    tvq = int((Decimal(net) * _QST).quantize(_CENT, rounding=ROUND_HALF_UP))
    return net + (g - (net + tps + tvq)), tps, tvq


def validate_ventilation(
    direction: str, amount: int, net, tps, tvq
) -> tuple[Optional[dict], str]:
    """Normalize the TPS/TVQ split of one entry.

    Returns ``({net_amount, gst_amount, qst_amount}, "")`` or
    ``(None, abort_reason)``.

    * a **recette** carries no ventilation — stray values (the form's hidden
      x-show fields) are silently zeroed, the trust stray-invoice-fields rule;
    * a **déboursé** left entirely blank defaults to ``net = amount``,
      taxes 0 (an unventilated expense claims no ITC/ITR — conservative);
    * a **déboursé** with any figure must satisfy
      ``net + tps + tvq == amount`` with each part a non-negative int.
    """
    def _norm(v) -> Optional[int]:
        if v is None or v == "":
            return 0
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        return v

    if direction != "déboursé":
        return {"net_amount": 0, "gst_amount": 0, "qst_amount": 0}, ""

    n, g, q = _norm(net), _norm(tps), _norm(tvq)
    if n is None or g is None or q is None or n < 0 or g < 0 or q < 0:
        return None, "ventilation_invalide"
    if n == 0 and g == 0 and q == 0:
        return {"net_amount": int(amount), "gst_amount": 0, "qst_amount": 0}, ""
    if n + g + q != int(amount):
        return None, "ventilation_invalide"
    return {"net_amount": n, "gst_amount": g, "qst_amount": q}, ""


def running_balances(txs: list[dict], opening: int = 0) -> list[int]:
    """Running ledger balance AFTER each row, computed from scratch — the
    RENDER path here (trust's ``recompute_running_balances`` promoted from
    verification-only). The caller must pass ``txs`` in ``(date, sequence)``
    order (what ``list_register`` returns) and the period's opening balance."""
    running = int(opening)
    out: list[int] = []
    for tx in txs:
        running += admin_delta(tx.get("direction", ""), int(tx.get("amount", 0)))
        out.append(running)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Firestore data-access layer. Fails CLOSED everywhere: a read failure during
# a mutation aborts it; list views propagate errors to the route.
# ═══════════════════════════════════════════════════════════════════════════


class _TxnAbort(Exception):
    """Raised inside a transaction to abort with a machine-stable reason
    (the trust._TxnAbort shape)."""

    def __init__(self, reason: str, value: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.value = value


# Machine-stable abort reason → French user message.
_ABORT_MESSAGES = {
    "compte_introuvable": "Compte d'administration introuvable.",
    "compte_fermé": "Ce compte est fermé.",
    "montant_invalide": "Le montant doit être un nombre entier de cents positif.",
    "direction_invalide": "Le sens de l'opération est invalide.",
    "type_invalide": "Le type d'opération est invalide.",
    "type_non_modifiable": (
        "Le type de cette écriture ne peut pas être modifié. Corrigez par une "
        "contre-passation puis une nouvelle écriture."
    ),
    "mode_invalide": "Le mode est invalide.",
    "catégorie_invalide": "La catégorie de dépense est invalide.",
    "catégorie_requise": "Une catégorie est requise pour une dépense.",
    "date_requise": "La date de l'opération est requise.",
    "date_future": "La date ne peut être dans le futur — le registre consigne ce qui est arrivé.",
    "contrepartie_requise": "La contrepartie (payeur / fournisseur) est requise.",
    "dossier_introuvable": "Dossier introuvable.",
    "ventilation_invalide": (
        "La ventilation (net + TPS + TVQ) doit égaler le montant du déboursé, "
        "chaque part étant un montant non négatif."
    ),
    "période_verrouillée": (
        "Cette période est couverte par une conciliation complétée. "
        "Corrigez par une contre-passation datée d'aujourd'hui."
    ),
    "écriture_verrouillée": (
        "Cette écriture est verrouillée (période conciliée, écriture compensée "
        "ou membre d'une contre-passation). Corrigez par une contre-passation."
    ),
    "écriture_liée_facture": (
        "Cette écriture est liée à une facture — elle ne se modifie ni ne se "
        "supprime. Contre-passez-la : le paiement enregistré sur la facture "
        "sera réduit d'autant."
    ),
    "écriture_liée_fideicommis": (
        "Cette recette provient d'un paiement d'honoraires du fidéicommis. "
        "Contre-passez plutôt le virement au fidéicommis — la recette suivra."
    ),
    "paiement_carte_indivisible": (
        "Un paiement de carte a deux jambes indissociables — supprimez ou "
        "contre-passez le paiement, jamais une seule jambe."
    ),
    "encaissement_carte_interdit": (
        "Une facture s'encaisse au compte d'opérations, jamais à la carte de crédit."
    ),
    "facture_introuvable": "Facture introuvable.",
    "facture_non_émise": "La facture doit être émise (envoyée ou en retard).",
    "facture_requise": "Un encaissement de facture doit nommer la facture encaissée.",
    "encaissement_excède_solde": (
        "Le montant excède le solde dû de la facture. Pour un trop-perçu, "
        "encaissez l'excédent au compte en fidéicommis."
    ),
    "motif_requis": "Un motif de contre-passation est requis.",
    "déjà_contrepassée": "Cette écriture a déjà été contre-passée.",
    "correction_non_contre_passable": (
        "Une écriture de correction ne se contre-passe pas — réinscrivez "
        "plutôt l'écriture voulue (pour un encaissement, une nouvelle "
        "écriture remettra la facture à jour)."
    ),
    "compensation_période_verrouillée": (
        "La date de compensation tombe dans une période déjà conciliée — "
        "elle réécrirait une preuve close. Utilisez la date réelle du relevé "
        "(nécessairement postérieure)."
    ),
    "écriture_introuvable": "Écriture introuvable.",
    "date_contre_passation_invalide": (
        "La date de contre-passation doit se situer entre la date de "
        "l'écriture originale et aujourd'hui, hors période conciliée."
    ),
    "compensation_invalide": (
        "Impossible de compenser : écriture déjà compensée ou annulée, "
        "date de compensation antérieure à l'écriture, ou future."
    ),
    "comptes_incompatibles": (
        "Un paiement de carte va d'un compte d'opérations vers une carte de crédit."
    ),
    "conciliation_introuvable": "Conciliation introuvable.",
    "conciliation_non_brouillon": "Cette conciliation est déjà complétée.",
    "conciliation_variance": "La conciliation n'est pas équilibrée (écart non nul).",
    "conciliation_modifiée": "Le registre a changé pendant la conciliation. Veuillez recommencer.",
    "conciliation_période_future": "La date de fin de période ne peut être dans le futur.",
    "conciliation_écriture_invalide": (
        "Une des écritures sélectionnées n'est pas conciliable pour cette période "
        "(déjà compensée, annulée, modifiée ou postérieure à la fin de période)."
    ),
    "relevé_requis": "Le solde du relevé est requis.",
}


def _sanitize_data(data: dict) -> dict:
    """Sanitize every top-level string value (the trust/invoice discipline)."""
    out: dict = {}
    for key, val in data.items():
        out[key] = sanitize(val, max_length=2000) if isinstance(val, str) else val
    return out


def _as_utc(value):
    """Normalize a datetime to tz-aware UTC (for order/date comparisons)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return value


def _midnight_utc(value) -> Optional[datetime]:
    """Snap a date/datetime to midnight UTC (date-only storage convention)."""
    v = _as_utc(value)
    if not isinstance(v, datetime):
        return None
    return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)


# ── admin_accounts CRUD + counter ──────────────────────────────────────────


def _default_account() -> dict:
    return {
        "id": "",
        "name": "",
        "account_type": "opérations",
        "institution": "",
        "transit": "",
        "account_number_last4": "",
        "ledger_balance": 0,
        "opened_date": None,
        "closed_date": None,
        "status": "actif",
        "notes": "",
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _validate_account(data: dict) -> list[str]:
    errors: list[str] = []
    if not data.get("name", "").strip():
        errors.append("Le nom du compte est requis.")
    if data.get("account_type") not in VALID_ACCOUNT_TYPES:
        errors.append("Type de compte invalide.")
    transit = data.get("transit", "")
    if transit and (not transit.isdigit() or len(transit) > 5):
        errors.append("Le numéro de transit doit comporter au plus 5 chiffres.")
    last4 = data.get("account_number_last4", "")
    if last4 and (not last4.isdigit() or len(last4) > 4):
        errors.append("N'inscrivez que les 4 derniers chiffres du compte ou de la carte.")
    if data.get("status") not in VALID_ACCOUNT_STATUSES:
        errors.append("Statut de compte invalide.")
    # Unlike trust, NO zero-balance close rule: an operations account may
    # close overdrawn, a card may close carrying a balance. « fermé » blocks
    # new entries only.
    return errors


def create_account(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create an administration account. The balance is system-owned (0)."""
    merged = {**_default_account(), **_sanitize_data(data)}
    errors = _validate_account(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    account_id = str(uuid.uuid4())
    merged.update({
        "id": account_id,
        "ledger_balance": 0,
        "opened_date": _as_utc(merged.get("opened_date")) or now,
        "closed_date": None,
        "status": "actif",
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    })
    try:
        db.collection(ACCOUNTS_COLLECTION).document(account_id).set(merged)
    except Exception:
        log_unexpected("admin account write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def get_account(account_id: str) -> Optional[dict]:
    try:
        doc = db.collection(ACCOUNTS_COLLECTION).document(account_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("admin account read failed")
        return None


def list_accounts(status: Optional[str] = None) -> list[dict]:
    """List accounts (fails CLOSED: propagates read errors to the route)."""
    query = db.collection(ACCOUNTS_COLLECTION)
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))
    accounts = [d.to_dict() for d in query.stream()]
    accounts.sort(key=lambda a: a.get("name", ""))
    return accounts


def update_account(account_id: str, data: dict) -> tuple[Optional[dict], list[str]]:
    """Update account METADATA only — the balance and type are never editable."""
    existing = get_account(account_id)
    if not existing:
        return None, ["Compte introuvable."]
    editable = {
        k: v
        for k, v in _sanitize_data(data).items()
        if k in ("name", "institution", "transit", "account_number_last4", "notes", "status")
    }
    merged = {**existing, **editable}
    errors = _validate_account(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())
    if merged.get("status") == "fermé":
        merged["closed_date"] = existing.get("closed_date") or now
    else:
        merged["closed_date"] = None
    try:
        db.collection(ACCOUNTS_COLLECTION).document(account_id).set(merged)
    except Exception:
        log_unexpected("admin account write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


# ── The reconciliation lock ────────────────────────────────────────────────


def _read_lock_floor(account_id: str, txn=None) -> Optional[datetime]:
    """``period_end`` of the account's latest COMPLETED reconciliation — the
    lock floor. ``None`` when nothing was ever completed. Streams the
    account's reconciliations (few — monthly) on the (account_id,
    period_end DESC) composite; runnable inside a transaction. Read errors
    propagate (fail CLOSED — a mutation must not proceed on an unknown
    floor)."""
    q = (
        db.collection(RECONCILIATIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("period_end", direction=firestore.Query.DESCENDING)
    )
    for snap in q.stream(transaction=txn):
        r = snap.to_dict() or {}
        if r.get("status") == "complétée":
            return _as_utc(r.get("period_end"))
    return None


def get_lock_floor(account_id: str) -> Optional[datetime]:
    """Public read of the lock floor (route-level display/pre-checks)."""
    return _read_lock_floor(account_id)


def _entry_lock_reason(tx: dict, lock_floor: Optional[datetime]) -> Optional[str]:
    """Why an entry refuses EDIT/DELETE — ``None`` when it is unlocked.

    The four structural clauses (user decision 2026-08-13, « modifiable
    jusqu'au verrou »):
      (a) dated on or before the lock floor — a completed reconciliation
          proved its period against the bank; editing behind it would
          silently falsify a closed proof (date-based deliberately: an
          entry left OUTSTANDING by the reconciliation still participated
          in its variance at its then-amount);
      (b) ``compensée`` — the bank confirmed the figure (an ``annulée``
          entry is caught by (c): only a reversal mints one);
      (c) member of a contre-passation pair — the pair is the audit trail;
      (d) a ``paiement_carte`` leg — two legs, one economic event: delete
          the payment (both legs), never one side.
    Plus two linkage clauses: an invoice-linked entry and a trust-sourced
    recette correct through their OWN paths (reversal / the trust side).

    Reversal (contre-passation) is NOT gated here — reversing a locked
    entry is legal and sound: the as-of resurrection sets keep every
    completed reconciliation re-provable.
    """
    if tx.get("kind") == "paiement_carte":
        return "paiement_carte_indivisible"
    if tx.get("reverses_id") or tx.get("reversed_by_id"):
        return "écriture_verrouillée"
    if tx.get("status") == "compensée":
        return "écriture_verrouillée"
    if tx.get("status") == "annulée":
        return "écriture_verrouillée"
    if tx.get("trust_transaction_id"):
        return "écriture_liée_fideicommis"
    if tx.get("invoice_id"):
        return "écriture_liée_facture"
    if lock_floor is not None:
        tx_date = _as_utc(tx.get("date"))
        if tx_date is not None and tx_date.date() <= lock_floor.date():
            return "période_verrouillée"
    return None


# ── Transaction assembly + prechecks ───────────────────────────────────────


def _build_transaction_doc(
    *,
    tx_id: str,
    account_id: str,
    sequence: int,
    date_value,
    direction: str,
    kind: str,
    amount: int,
    method: str,
    counterparty: str,
    category: Optional[str],
    ventilation: dict,
    description: str,
    reference: str,
    supplier_invoice_ref: str,
    dossier: Optional[dict],
    dossier_id: Optional[str],
    invoice_id: Optional[str],
    invoice_number: str,
    trust_transaction_id: Optional[str],
    now: datetime,
    status: str = "en_circulation",
    cleared_date=None,
    reconciliation_id: Optional[str] = None,
    reverses_id: Optional[str] = None,
    reversed_by_id: Optional[str] = None,
    related_transaction_id: Optional[str] = None,
) -> dict:
    """Assemble a full admin_transactions doc, snapshotting the dossier
    labels off the read dossier. ``date`` is stored midnight UTC. There is
    deliberately NO frozen balance field — balances are computed at read."""
    dossier_file_number = ""
    dossier_title = ""
    if dossier:
        dossier_file_number = dossier.get("file_number", "")
        dossier_title = dossier.get("title", "")
    return {
        "id": tx_id,
        "account_id": account_id,
        "sequence": sequence,
        "date": _midnight_utc(date_value),
        "direction": direction,
        "kind": kind,
        "amount": amount,
        "net_amount": int(ventilation.get("net_amount", 0)),
        "gst_amount": int(ventilation.get("gst_amount", 0)),
        "qst_amount": int(ventilation.get("qst_amount", 0)),
        "category": category,
        "counterparty": counterparty,
        "description": description,
        "reference": reference,
        # Supplier's own invoice number — distinct from ``reference`` (a
        # reconciliation matches cheque numbers; a tax audit matches
        # supplier invoice numbers). Never named invoice_external_ref,
        # which in trust means a pre-Athéna FEE invoice.
        "supplier_invoice_ref": supplier_invoice_ref,
        "method": method,
        "dossier_id": dossier_id,
        "dossier_file_number": dossier_file_number,
        "dossier_title": dossier_title,
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "trust_transaction_id": trust_transaction_id,
        "receipt_storage_path": None,
        "receipt_filename": "",
        "receipt_file_type": "",
        "receipt_file_size": 0,
        "status": status,
        "cleared_date": _midnight_utc(cleared_date) if cleared_date else None,
        "reconciliation_id": reconciliation_id,
        "reverses_id": reverses_id,
        "reversed_by_id": reversed_by_id,
        "related_transaction_id": related_transaction_id,
        "revisions": [],
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    }


def _validate_business(clean: dict) -> tuple[Optional[dict], list[str]]:
    """No-read guards shared by create and update: vocabulary membership,
    direction/kind coherence, category presence, ventilation, date sanity.
    Returns ``(normalized_ventilation, errors)``."""
    amount = clean.get("amount")
    direction = clean.get("direction", "")
    kind = clean.get("kind", "")
    method = clean.get("method", "")
    counterparty = (clean.get("counterparty") or "").strip()
    category = clean.get("category") or None

    def _msg(reason: str) -> tuple[None, list[str]]:
        return None, [_ABORT_MESSAGES[reason]]

    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return _msg("montant_invalide")
    if direction not in VALID_DIRECTIONS:
        return _msg("direction_invalide")
    if kind not in _SIMPLE_KINDS:
        # paiement_carte and correction are minted only by their own paths.
        return _msg("type_invalide")
    if kind == "encaissement_facture" and direction != "recette":
        return _msg("type_invalide")
    if kind == "dépense" and direction != "déboursé":
        return _msg("type_invalide")
    if method not in VALID_METHODS:
        return _msg("mode_invalide")
    if not counterparty:
        return _msg("contrepartie_requise")
    if kind == "dépense":
        if not category:
            return _msg("catégorie_requise")
        if category not in ADMIN_EXPENSE_CATEGORIES:
            return _msg("catégorie_invalide")
    else:
        # Category belongs to expenses alone; stray values are dropped.
        clean["category"] = None

    tx_date = clean.get("date")
    if _midnight_utc(tx_date) is None:
        return _msg("date_requise")
    if _midnight_utc(tx_date).date() > today_mtl():
        # The ONE clock is Montréal (utils/deadlines doctrine) — a UTC
        # evening must not refuse a date that is still « today ».
        return _msg("date_future")

    ventilation, reason = validate_ventilation(
        direction, amount,
        clean.get("net_amount"), clean.get("gst_amount"), clean.get("qst_amount"),
    )
    if ventilation is None:
        return _msg(reason)
    return ventilation, []


# ── create_transaction ─────────────────────────────────────────────────────


def create_transaction(data: dict) -> tuple[Optional[dict], list[str]]:
    """Append one entry to an administration register, transactionally.

    The economic ``date`` is FREE in the past (down to the lock floor — a
    period proved by a completed reconciliation refuses new entries) and
    refused in the future. There is no backdating guard and no overdraft
    control — deliberate divergences from trust (module docstring). An
    ``encaissement_facture`` additionally reads its invoice inside the
    transaction: issued status, and ``amount <= amount_due - amount_paid``
    (the LIVE balance — stricter than trust's frozen amount_due check), on
    an ``opérations`` account only. Returns ``(entry, [])`` or
    ``(None, [french_errors])``.
    """
    clean = _sanitize_data(data)
    if not clean.get("account_id"):
        return None, [_ABORT_MESSAGES["compte_introuvable"]]
    ventilation, errors = _validate_business(clean)
    if errors:
        return None, errors

    account_id = clean["account_id"]
    direction = clean["direction"]
    kind = clean["kind"]
    amount = int(clean["amount"])
    tx_date = clean.get("date")
    dossier_id = clean.get("dossier_id") or None
    invoice_id = clean.get("invoice_id") or None
    trust_transaction_id = clean.get("trust_transaction_id") or None
    if kind != "encaissement_facture":
        invoice_id = None
    elif not invoice_id:
        return None, [_ABORT_MESSAGES["facture_requise"]]
    else:
        # The INVOICE determines the dossier on an encaissement — a caller-
        # supplied dossier_id (the form's hidden picker field surviving a
        # kind switch) must never misattribute the receipt in the register.
        dossier_id = None

    account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    counter_ref = db.collection(COUNTERS_COLLECTION).document(_counter_id(account_id))
    dossier_ref = db.collection(DOSSIERS_COLLECTION).document(dossier_id) if dossier_id else None
    invoice_ref = db.collection(INVOICES_COLLECTION).document(invoice_id) if invoice_id else None
    tx_id = str(uuid.uuid4())
    tx_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    now = datetime.now(timezone.utc)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _create(txn) -> None:
        # 1. READS (all before any write)
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        account = acc_snap.to_dict()
        counter_snap = counter_ref.get(transaction=txn)
        seq_current = (
            int((counter_snap.to_dict() or {}).get("seq", 0)) if counter_snap.exists else 0
        )
        lock_floor = _read_lock_floor(account_id, txn)

        dossier = None
        if dossier_ref is not None:
            d_snap = dossier_ref.get(transaction=txn)
            if not d_snap.exists:
                raise _TxnAbort("dossier_introuvable")
            dossier = d_snap.to_dict()

        invoice = None
        if invoice_ref is not None:
            i_snap = invoice_ref.get(transaction=txn)
            if not i_snap.exists:
                raise _TxnAbort("facture_introuvable")
            invoice = i_snap.to_dict()

        # 2. GUARDS
        if account.get("status") != "actif":
            raise _TxnAbort("compte_fermé")
        if lock_floor is not None and _midnight_utc(tx_date).date() <= lock_floor.date():
            raise _TxnAbort("période_verrouillée")
        if kind == "encaissement_facture":
            if account.get("account_type") != "opérations":
                raise _TxnAbort("encaissement_carte_interdit")
            if invoice.get("status") not in _ISSUED_INVOICE_STATUSES:
                raise _TxnAbort("facture_non_émise")
            balance = int(invoice.get("amount_due", 0)) - int(invoice.get("amount_paid", 0))
            if amount > balance:
                raise _TxnAbort("encaissement_excède_solde")

        # 3. COMPUTE — the entry's snapshots. On an encaissement the invoice
        # ALONE carries the dossier (dossier_id was nulled above): linkage
        # and labels both come off the invoice, never a caller value.
        invoice_number = ""
        if invoice is not None:
            invoice_number = invoice.get("invoice_number", "")
            dossier = {
                "file_number": invoice.get("dossier_file_number", ""),
                "title": invoice.get("dossier_title", ""),
            }
            result["dossier_id"] = invoice.get("dossier_id") or None
        seq = seq_current + 1
        entry = _build_transaction_doc(
            tx_id=tx_id, account_id=account_id, sequence=seq, date_value=tx_date,
            direction=direction, kind=kind, amount=amount,
            method=clean["method"], counterparty=clean["counterparty"].strip(),
            category=clean.get("category"), ventilation=ventilation,
            description=clean.get("description", ""),
            reference=clean.get("reference", ""),
            supplier_invoice_ref=clean.get("supplier_invoice_ref", ""),
            dossier=dossier,
            dossier_id=result.get("dossier_id", dossier_id),
            invoice_id=invoice_id, invoice_number=invoice_number,
            trust_transaction_id=trust_transaction_id, now=now,
        )

        # 4. WRITES (single commit)
        txn.set(tx_ref, entry)
        txn.set(counter_ref, {"seq": seq, "updated_at": now})
        txn.update(account_ref, {
            "ledger_balance": int(account.get("ledger_balance", 0))
            + admin_delta(direction, amount),
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        result["entry"] = entry

    try:
        with span("admin.transaction", direction=direction, kind=kind):
            _create(transaction)
    except _TxnAbort as abort:
        log_admin_ledger_event(
            "admin_transaction_refused", "refused",
            account_id=account_id, reason=abort.reason,
        )
        return None, [_ABORT_MESSAGES.get(abort.reason, "Opération refusée.")]
    except Exception as exc:
        logger.error(
            "admin create_transaction failed for account %s: %s",
            sanitize_log_value(account_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    entry = result["entry"]
    log_admin_ledger_event(
        "admin_transaction_created", transaction_id=tx_id,
        account_id=account_id, invoice_id=invoice_id,
        direction=direction, kind=kind, sequence=entry["sequence"],
    )
    return entry, []


# ── update_transaction / delete_transaction (editable until the lock) ──────

# Fields an edit may change. Everything else — identity, linkage, lifecycle —
# is immutable (correction path: reversal).
_EDITABLE_FIELDS = (
    "date", "direction", "kind", "amount", "category",
    "net_amount", "gst_amount", "qst_amount",
    "counterparty", "description", "reference", "supplier_invoice_ref",
    "method", "dossier_id",
)


def update_transaction(tx_id: str, data: dict) -> tuple[Optional[dict], list[str]]:
    """Edit an UNLOCKED entry in place, transactionally.

    The lock predicate (``_entry_lock_reason``) is re-read inside the
    transaction; the account's ``ledger_balance`` is adjusted by
    ``delta_new - delta_old``; the change lands in the entry's bounded
    ``revisions`` trail. ``kind`` may only move between the two simple
    kinds — an invoice-linked, card-payment or correction entry never
    reaches here (locked)."""
    clean = _sanitize_data(data)
    now = datetime.now(timezone.utc)
    tx_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _update(txn) -> None:
        snap = tx_ref.get(transaction=txn)
        if not snap.exists:
            raise _TxnAbort("écriture_introuvable")
        existing = snap.to_dict()
        account_id = existing.get("account_id")
        account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        account = acc_snap.to_dict()
        lock_floor = _read_lock_floor(account_id, txn)

        reason = _entry_lock_reason(existing, lock_floor)
        if reason:
            raise _TxnAbort(reason)

        merged = {**existing}
        for k in _EDITABLE_FIELDS:
            if k in clean:
                merged[k] = clean[k]
        merged["dossier_id"] = merged.get("dossier_id") or None

        # Kind moves only within the simple, unlinked kinds.
        if merged.get("kind") not in _EDITABLE_KINDS or existing.get("kind") not in _EDITABLE_KINDS:
            if merged.get("kind") != existing.get("kind"):
                raise _TxnAbort("type_non_modifiable")

        ventilation, verrors = _validate_business(merged)
        if verrors:
            # Re-raise through the abort channel with the matching reason.
            for reason_key, msg in _ABORT_MESSAGES.items():
                if msg == verrors[0]:
                    raise _TxnAbort(reason_key)
            raise _TxnAbort("montant_invalide")
        # The NEW date must also sit above the lock floor.
        if lock_floor is not None and _midnight_utc(merged.get("date")).date() <= lock_floor.date():
            raise _TxnAbort("période_verrouillée")

        dossier = None
        new_dossier_id = merged.get("dossier_id")
        if new_dossier_id and new_dossier_id != existing.get("dossier_id"):
            d_snap = db.collection(DOSSIERS_COLLECTION).document(new_dossier_id).get(
                transaction=txn
            )
            if not d_snap.exists:
                raise _TxnAbort("dossier_introuvable")
            dossier = d_snap.to_dict()

        merged.update(ventilation)
        merged["date"] = _midnight_utc(merged.get("date"))
        merged["counterparty"] = (merged.get("counterparty") or "").strip()
        if dossier is not None:
            merged["dossier_file_number"] = dossier.get("file_number", "")
            merged["dossier_title"] = dossier.get("title", "")
        elif not new_dossier_id:
            merged["dossier_file_number"] = ""
            merged["dossier_title"] = ""

        # Revisions trail: only fields that actually changed.
        changes = {}
        for k in _EDITABLE_FIELDS:
            if merged.get(k) != existing.get(k):
                changes[k] = [existing.get(k), merged.get(k)]
        if not changes:
            result["entry"] = existing
            result["noop"] = True
            return
        revisions = list(existing.get("revisions") or [])
        revisions.append({"at": now, "changes": changes})
        merged["revisions"] = revisions[-_REVISIONS_CAP:]
        merged["updated_at"] = now
        merged["etag"] = str(uuid.uuid4())

        delta = admin_delta(merged["direction"], int(merged["amount"])) - admin_delta(
            existing.get("direction", ""), int(existing.get("amount", 0))
        )
        txn.set(tx_ref, merged)
        txn.update(account_ref, {
            "ledger_balance": int(account.get("ledger_balance", 0)) + delta,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        result["entry"] = merged
        result["fields"] = sorted(changes)

    try:
        _update(transaction)
    except _TxnAbort as abort:
        log_admin_ledger_event(
            "admin_transaction_refused", "refused",
            transaction_id=tx_id, reason=abort.reason,
        )
        return None, [_ABORT_MESSAGES.get(abort.reason, "Modification refusée.")]
    except Exception as exc:
        logger.error(
            "admin update_transaction failed for %s: %s",
            sanitize_log_value(tx_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    if not result.get("noop"):
        log_admin_ledger_event(
            "admin_transaction_updated", transaction_id=tx_id,
            account_id=result["entry"].get("account_id"),
            fields=result.get("fields", []),
        )
    return result["entry"], []


def delete_transaction(tx_id: str) -> tuple[Optional[dict], list[str]]:
    """Hard-delete an UNLOCKED entry, transactionally.

    A ``paiement_carte`` leg refuses here — ``delete_card_payment`` removes
    BOTH legs. Returns ``(deleted_doc, [])`` so the ROUTE can record the
    deletion in the house registry (``models/audit_event`` — write side
    lives in the callers, after the committed delete). The sequence number
    is never reused; a gap plus the deletions registry IS the trail."""
    tx_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    now = datetime.now(timezone.utc)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _delete(txn) -> None:
        snap = tx_ref.get(transaction=txn)
        if not snap.exists:
            raise _TxnAbort("écriture_introuvable")
        existing = snap.to_dict()
        account_id = existing.get("account_id")
        account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        account = acc_snap.to_dict()
        lock_floor = _read_lock_floor(account_id, txn)
        reason = _entry_lock_reason(existing, lock_floor)
        if reason:
            raise _TxnAbort(reason)

        txn.delete(tx_ref)
        txn.update(account_ref, {
            "ledger_balance": int(account.get("ledger_balance", 0))
            - admin_delta(existing.get("direction", ""), int(existing.get("amount", 0))),
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        result["entry"] = existing

    try:
        _delete(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Suppression refusée.")]
    except Exception as exc:
        logger.error(
            "admin delete_transaction failed for %s: %s",
            sanitize_log_value(tx_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la suppression. Veuillez réessayer."]

    entry = result["entry"]
    log_admin_ledger_event(
        "admin_transaction_deleted", transaction_id=tx_id,
        account_id=entry.get("account_id"),
    )
    return entry, []


def delete_card_payment(tx_id: str) -> tuple[Optional[list[dict]], list[str]]:
    """Delete BOTH legs of an unlocked card payment, atomically (lock clause
    (d): one economic event, two rows — never one side alone). Returns the
    LIST of deleted legs so the route can record BOTH in the deletions
    registry — two rows disappeared, two trail entries."""
    now = datetime.now(timezone.utc)
    tx_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _delete_pair(txn) -> None:
        snap = tx_ref.get(transaction=txn)
        if not snap.exists:
            raise _TxnAbort("écriture_introuvable")
        leg_a = snap.to_dict()
        if leg_a.get("kind") != "paiement_carte" or not leg_a.get("related_transaction_id"):
            raise _TxnAbort("écriture_introuvable")
        other_ref = db.collection(TRANSACTIONS_COLLECTION).document(
            leg_a["related_transaction_id"]
        )
        o_snap = other_ref.get(transaction=txn)
        if not o_snap.exists:
            raise _TxnAbort("écriture_introuvable")
        leg_b = o_snap.to_dict()

        accounts = {}
        for leg in (leg_a, leg_b):
            aid = leg.get("account_id")
            if aid not in accounts:
                aref = db.collection(ACCOUNTS_COLLECTION).document(aid)
                asnap = aref.get(transaction=txn)
                if not asnap.exists:
                    raise _TxnAbort("compte_introuvable")
                accounts[aid] = (aref, asnap.to_dict())
            floor = _read_lock_floor(aid, txn)
            # The pair-membership clause obviously matches — check the REST
            # of the lock (compensée, reversal pair, period) per leg.
            probe = {**leg, "kind": "", "related_transaction_id": None}
            reason = _entry_lock_reason(probe, floor)
            if reason:
                raise _TxnAbort(reason)

        for leg in (leg_a, leg_b):
            txn.delete(db.collection(TRANSACTIONS_COLLECTION).document(leg["id"]))
        for aid, (aref, acc) in accounts.items():
            delta = sum(
                -admin_delta(leg.get("direction", ""), int(leg.get("amount", 0)))
                for leg in (leg_a, leg_b)
                if leg.get("account_id") == aid
            )
            txn.update(aref, {
                "ledger_balance": int(acc.get("ledger_balance", 0)) + delta,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        result["legs"] = [leg_a, leg_b]

    try:
        _delete_pair(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Suppression refusée.")]
    except Exception as exc:
        logger.error("admin delete_card_payment failed: %s", type(exc).__name__)
        return None, ["Erreur lors de la suppression. Veuillez réessayer."]

    for leg in result["legs"]:
        log_admin_ledger_event(
            "admin_transaction_deleted", transaction_id=leg["id"],
            account_id=leg.get("account_id"),
        )
    return result["legs"], []


# ── clear_transaction / clear_transactions_bulk ────────────────────────────


def _clear_entries(
    tx_ids: list, cleared_date, reconciliation_id: Optional[str],
    _reason_out: Optional[dict] = None,
) -> tuple[list[dict], list[str]]:
    """Clear en_circulation entries to compensée, all-or-nothing, in one
    transaction. No balance moves (no bank_balance here) — but the account
    etag IS regenerated: clearing changes the as-of context, and the
    reconciliation completion sentinel must see it. ``_reason_out`` receives
    the machine reason on refusal so the caller can surface the RIGHT
    French message (the locked-period refusal must not read as a generic
    « date invalide »)."""
    if _reason_out is None:
        _reason_out = {}
    if not tx_ids:
        return [], []
    cd = _midnight_utc(cleared_date)
    now = datetime.now(timezone.utc)
    # Montréal clock, like every date guard in this module — a UTC check
    # would stamp (or refuse) tomorrow's date every evening after 20:00.
    if cd is None or cd.date() > today_mtl():
        return [], list(tx_ids)

    tx_refs = [db.collection(TRANSACTIONS_COLLECTION).document(t) for t in tx_ids]
    transaction = db.transaction()
    outcome: dict = {"cleared": [], "failed": []}

    @firestore.transactional
    def _txn(txn) -> None:
        entries = []
        account_id = None
        failed = []
        for ref in tx_refs:
            snap = ref.get(transaction=txn)
            e = snap.to_dict() if snap.exists else None
            ed = _as_utc(e.get("date")) if e else None
            if (
                not e
                or e.get("status") != "en_circulation"
                or (ed is not None and cd.date() < ed.date())
            ):
                failed.append(ref.id)
                continue
            if account_id is None:
                account_id = e.get("account_id")
            elif e.get("account_id") != account_id:
                failed.append(ref.id)
                continue
            entries.append(e)
        if failed or not entries:
            outcome["failed"] = failed or list(tx_ids)
            raise _TxnAbort("compensation_invalide")

        account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        # A cleared_date INSIDE a completed (locked) period would rewrite that
        # period's as-of picture: the reconciliation counted the entry as
        # outstanding, and a backdated clearing stops the resurrection set
        # from resurrecting it — the completed rec would no longer re-prove.
        # The real bank clearing of an unticked item is necessarily AFTER the
        # statement date, so this refuses only falsified dates.
        lock_floor = _read_lock_floor(account_id, txn)
        if lock_floor is not None and cd.date() <= lock_floor.date():
            outcome["failed"] = list(tx_ids)
            _reason_out["reason"] = "compensation_période_verrouillée"
            raise _TxnAbort("compensation_période_verrouillée")

        cleared_docs = []
        for ref, e in zip(tx_refs, entries):
            update = {
                "status": "compensée",
                "cleared_date": cd,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            }
            if reconciliation_id is not None:
                update["reconciliation_id"] = reconciliation_id
            txn.update(ref, update)
            cleared_docs.append({**e, **update})
        txn.update(account_ref, {"updated_at": now, "etag": str(uuid.uuid4())})
        outcome["cleared"] = cleared_docs

    try:
        _txn(transaction)
    except _TxnAbort:
        return [], outcome["failed"] or list(tx_ids)
    except Exception as exc:
        logger.error("admin clear entries failed: %s", type(exc).__name__)
        return [], list(tx_ids)
    return outcome["cleared"], []


def clear_transaction(tx_id: str, cleared_date) -> tuple[Optional[dict], list[str]]:
    """Step 2 of the lifecycle: mark one en_circulation entry compensée."""
    outcome_holder: dict = {}
    cleared, failed = _clear_entries([tx_id], cleared_date, None,
                                     _reason_out=outcome_holder)
    if failed or not cleared:
        reason = outcome_holder.get("reason", "compensation_invalide")
        return None, [_ABORT_MESSAGES.get(reason, _ABORT_MESSAGES["compensation_invalide"])]
    entry = cleared[0]
    log_admin_ledger_event(
        "admin_transaction_cleared", transaction_id=tx_id,
        account_id=entry.get("account_id"),
    )
    return entry, []


def clear_transactions_bulk(tx_ids: list, cleared_date) -> tuple[int, list[str]]:
    """Clear many entries at once, all-or-nothing."""
    cleared, failed = _clear_entries(list(tx_ids), cleared_date, None)
    if failed:
        return 0, failed
    for entry in cleared:
        log_admin_ledger_event(
            "admin_transaction_cleared", transaction_id=entry.get("id"),
            account_id=entry.get("account_id"),
        )
    return len(cleared), []


# ── reverse_transaction ────────────────────────────────────────────────────


def reverse_transaction(
    tx_id: str,
    reason: str,
    reversal_date=None,
    *,
    allow_linked: bool = False,
) -> tuple[Optional[dict], list[str]]:
    """Contre-passation: mint an opposite ``correction`` entry (trust's
    status algebra verbatim — reversing ``en_circulation`` stamps BOTH
    annulée; reversing ``compensée`` births an ``en_circulation`` reversal).

    ``reversal_date`` defaults to today and must sit between the original's
    date and today, above the lock floor (free dates make the trust
    dated-now rule optional; the constraint keeps the period sheets sane).
    A ``paiement_carte`` leg reverses BOTH legs atomically. A trust-sourced
    recette refuses unless ``allow_linked`` — the fidéicommis side is the
    source of truth for that movement, and ITS reversal calls back here
    with the flag."""
    reason = (reason or "").strip()
    if not reason:
        return None, [_ABORT_MESSAGES["motif_requis"]]

    orig_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    now = datetime.now(timezone.utc)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _reverse(txn) -> None:
        o_snap = orig_ref.get(transaction=txn)
        if not o_snap.exists:
            raise _TxnAbort("écriture_introuvable")
        original = o_snap.to_dict()
        if original.get("reversed_by_id"):
            raise _TxnAbort("déjà_contrepassée")
        if original.get("kind") == REVERSAL_KIND:
            # A reversal-of-reversal would double-count the copied ventilation
            # on the reports and can never re-link an invoice (correction rows
            # carry invoice_id=None, so the payment projection would drift).
            # The undo of a mistaken reversal is a FRESH entry — for an
            # encaissement the new entry re-records the payment itself.
            raise _TxnAbort("correction_non_contre_passable")
        if original.get("trust_transaction_id") and not allow_linked:
            raise _TxnAbort("écriture_liée_fideicommis")

        legs = [original]
        if original.get("kind") == "paiement_carte" and original.get("related_transaction_id"):
            other_ref = db.collection(TRANSACTIONS_COLLECTION).document(
                original["related_transaction_id"]
            )
            other_snap = other_ref.get(transaction=txn)
            if not other_snap.exists:
                raise _TxnAbort("écriture_introuvable")
            other = other_snap.to_dict()
            if other.get("reversed_by_id"):
                raise _TxnAbort("déjà_contrepassée")
            legs.append(other)

        # Per-account reads: account doc + counter + lock floor.
        accounts: dict = {}
        for leg in legs:
            aid = leg.get("account_id")
            if aid in accounts:
                continue
            aref = db.collection(ACCOUNTS_COLLECTION).document(aid)
            asnap = aref.get(transaction=txn)
            if not asnap.exists:
                raise _TxnAbort("compte_introuvable")
            cref = db.collection(COUNTERS_COLLECTION).document(_counter_id(aid))
            csnap = cref.get(transaction=txn)
            seq = int((csnap.to_dict() or {}).get("seq", 0)) if csnap.exists else 0
            accounts[aid] = {
                "ref": aref, "doc": asnap.to_dict(), "counter_ref": cref,
                "seq": seq, "floor": _read_lock_floor(aid, txn), "delta": 0,
            }

        # Reversal date: [original date, today], above every touched floor.
        # The DEFAULT reads the Montréal clock, like the guard below it — a
        # datetime.now(utc) default is already TOMORROW every evening after
        # 20:00, so an undated contre-passation refused itself outright for
        # four hours a day (the 2026-08-02 evening-band class, caught by the
        # frozen-clock fixture the day after this module shipped).
        rd = _midnight_utc(reversal_date) if reversal_date else _midnight_utc(today_mtl())
        if rd is None or rd.date() > today_mtl():
            raise _TxnAbort("date_contre_passation_invalide")
        for leg in legs:
            od = _as_utc(leg.get("date"))
            if od is not None and rd.date() < od.date():
                raise _TxnAbort("date_contre_passation_invalide")
        for info in accounts.values():
            if info["floor"] is not None and rd.date() <= info["floor"].date():
                raise _TxnAbort("date_contre_passation_invalide")

        reversals = []
        orig_updates = []
        for leg in legs:
            aid = leg["account_id"]
            info = accounts[aid]
            orig_dir = leg.get("direction")
            amount = int(leg.get("amount", 0))
            rev_dir = "déboursé" if orig_dir == "recette" else "recette"
            orig_status = leg.get("status")
            if orig_status == "en_circulation":
                orig_new_status = "annulée"
                rev_status = "annulée"
            else:  # compensée — original stays compensée
                orig_new_status = orig_status
                rev_status = "en_circulation"

            info["seq"] += 1
            # Ledger arithmetic is status-blind: only the reversal's own
            # signed amount moves the balance (an annulée flip changes
            # nothing — annulée rows still count in the ledger).
            info["delta"] += admin_delta(rev_dir, amount)

            rev_id = str(uuid.uuid4())
            reversal = _build_transaction_doc(
                tx_id=rev_id, account_id=aid, sequence=info["seq"], date_value=rd,
                direction=rev_dir, kind=REVERSAL_KIND, amount=amount,
                method=leg.get("method", ""), counterparty=leg.get("counterparty", ""),
                category=leg.get("category"),
                ventilation={
                    "net_amount": int(leg.get("net_amount", 0)),
                    "gst_amount": int(leg.get("gst_amount", 0)),
                    "qst_amount": int(leg.get("qst_amount", 0)),
                },
                description=reason,
                reference=leg.get("reference", ""),
                supplier_invoice_ref=leg.get("supplier_invoice_ref", ""),
                dossier=None, dossier_id=leg.get("dossier_id"),
                invoice_id=None, invoice_number=leg.get("invoice_number", ""),
                trust_transaction_id=None, now=now,
                status=rev_status, reverses_id=leg["id"],
            )
            reversal["dossier_file_number"] = leg.get("dossier_file_number", "")
            reversal["dossier_title"] = leg.get("dossier_title", "")
            reversals.append(reversal)
            orig_updates.append((leg, orig_new_status, rev_id))

        for reversal in reversals:
            txn.set(
                db.collection(TRANSACTIONS_COLLECTION).document(reversal["id"]), reversal
            )
        for leg, new_status, rev_id in orig_updates:
            txn.update(db.collection(TRANSACTIONS_COLLECTION).document(leg["id"]), {
                "status": new_status,
                "reversed_by_id": rev_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        for info in accounts.values():
            txn.set(info["counter_ref"], {"seq": info["seq"], "updated_at": now})
            txn.update(info["ref"], {
                "ledger_balance": int(info["doc"].get("ledger_balance", 0)) + info["delta"],
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        result["reversals"] = reversals
        result["original"] = original

    try:
        with span("admin.transaction", direction="reversal", kind=REVERSAL_KIND):
            _reverse(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Contre-passation refusée.")]
    except Exception as exc:
        logger.error(
            "admin reverse_transaction failed for %s: %s",
            sanitize_log_value(tx_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la contre-passation. Veuillez réessayer."]

    for reversal in result["reversals"]:
        log_admin_ledger_event(
            "admin_transaction_reversed", transaction_id=reversal["id"],
            account_id=reversal.get("account_id"), reverses_id=reversal.get("reverses_id"),
        )
    return result["reversals"][0], []


# ── create_card_payment — one event, two legs ──────────────────────────────


def create_card_payment(
    bank_account_id: str,
    card_account_id: str,
    amount: int,
    date_value,
    method: str,
    reference: str = "",
    description: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Pay the corporate card FROM the operations account: ONE Firestore
    transaction writing leg A (bank, ``déboursé``) and leg B (card,
    ``recette``), kind ``paiement_carte``, cross-linked via
    ``related_transaction_id``, both ``en_circulation`` (each leg clears on
    its OWN statement). Reversing or deleting one leg carries the other."""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None, [_ABORT_MESSAGES["montant_invalide"]]
    if method not in VALID_METHODS:
        return None, [_ABORT_MESSAGES["mode_invalide"]]
    d = _midnight_utc(date_value)
    if d is None:
        return None, [_ABORT_MESSAGES["date_requise"]]
    if d.date() > today_mtl():
        return None, [_ABORT_MESSAGES["date_future"]]

    description = sanitize(description or "", max_length=2000)
    reference = sanitize(reference or "", max_length=2000)
    now = datetime.now(timezone.utc)
    leg_a_id = str(uuid.uuid4())
    leg_b_id = str(uuid.uuid4())
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _pay(txn) -> None:
        infos = {}
        for aid in (bank_account_id, card_account_id):
            aref = db.collection(ACCOUNTS_COLLECTION).document(aid)
            asnap = aref.get(transaction=txn)
            if not asnap.exists:
                raise _TxnAbort("compte_introuvable")
            cref = db.collection(COUNTERS_COLLECTION).document(_counter_id(aid))
            csnap = cref.get(transaction=txn)
            seq = int((csnap.to_dict() or {}).get("seq", 0)) if csnap.exists else 0
            infos[aid] = {
                "ref": aref, "doc": asnap.to_dict(), "counter_ref": cref,
                "seq": seq, "floor": _read_lock_floor(aid, txn),
            }
        bank = infos[bank_account_id]["doc"]
        card = infos[card_account_id]["doc"]
        if bank.get("account_type") != "opérations" or card.get("account_type") != "carte_crédit":
            raise _TxnAbort("comptes_incompatibles")
        for info in infos.values():
            if info["doc"].get("status") != "actif":
                raise _TxnAbort("compte_fermé")
            if info["floor"] is not None and d.date() <= info["floor"].date():
                raise _TxnAbort("période_verrouillée")

        zero = {"net_amount": 0, "gst_amount": 0, "qst_amount": 0}
        infos[bank_account_id]["seq"] += 1
        leg_a = _build_transaction_doc(
            tx_id=leg_a_id, account_id=bank_account_id,
            sequence=infos[bank_account_id]["seq"], date_value=d,
            direction="déboursé", kind="paiement_carte", amount=amount,
            method=method, counterparty=card.get("name", "Carte de crédit"),
            category=None, ventilation=zero, description=description,
            reference=reference, supplier_invoice_ref="",
            dossier=None, dossier_id=None, invoice_id=None, invoice_number="",
            trust_transaction_id=None, now=now,
            related_transaction_id=leg_b_id,
        )
        infos[card_account_id]["seq"] += 1
        leg_b = _build_transaction_doc(
            tx_id=leg_b_id, account_id=card_account_id,
            sequence=infos[card_account_id]["seq"], date_value=d,
            direction="recette", kind="paiement_carte", amount=amount,
            method=method, counterparty=bank.get("name", "Compte d'opérations"),
            category=None, ventilation=zero, description=description,
            reference=reference, supplier_invoice_ref="",
            dossier=None, dossier_id=None, invoice_id=None, invoice_number="",
            trust_transaction_id=None, now=now,
            related_transaction_id=leg_a_id,
        )

        txn.set(db.collection(TRANSACTIONS_COLLECTION).document(leg_a_id), leg_a)
        txn.set(db.collection(TRANSACTIONS_COLLECTION).document(leg_b_id), leg_b)
        for aid, leg in ((bank_account_id, leg_a), (card_account_id, leg_b)):
            info = infos[aid]
            txn.set(info["counter_ref"], {"seq": info["seq"], "updated_at": now})
            txn.update(info["ref"], {
                "ledger_balance": int(info["doc"].get("ledger_balance", 0))
                + admin_delta(leg["direction"], amount),
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        result["legs"] = [leg_a, leg_b]

    try:
        with span("admin.transaction", direction="transfer", kind="paiement_carte"):
            _pay(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Paiement refusé.")]
    except Exception as exc:
        logger.error("admin card payment failed: %s", type(exc).__name__)
        return None, ["Erreur lors du paiement. Veuillez réessayer."]

    log_admin_ledger_event(
        "admin_card_payment_created", transaction_id=leg_a_id,
        account_id=bank_account_id, card_account_id=card_account_id,
    )
    return result["legs"][0], []


# ── attach_receipt — the pièce justificative metadata ──────────────────────


def attach_receipt(
    tx_id: str, storage_path: str, filename: str, content_type: str, size: int
) -> tuple[Optional[dict], list[str]]:
    """Set the four receipt fields on an entry — the one post-create
    mutation OUTSIDE the lock (an annulée or reconciled entry keeps the
    right to its supporting document; a receipt moves no money, so the
    account etag is deliberately untouched). Returns the PREVIOUS storage
    path under ``_previous_receipt_path`` so the caller can delete the
    replaced blob."""
    existing = get_transaction(tx_id)
    if existing is None:
        return None, [_ABORT_MESSAGES["écriture_introuvable"]]
    now = datetime.now(timezone.utc)
    updates = {
        "receipt_storage_path": storage_path,
        "receipt_filename": sanitize(filename or "", max_length=300),
        "receipt_file_type": sanitize(content_type or "", max_length=100),
        "receipt_file_size": int(size or 0),
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    }
    try:
        db.collection(TRANSACTIONS_COLLECTION).document(tx_id).update(updates)
    except Exception:
        log_unexpected("admin receipt attach failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    log_admin_ledger_event(
        "admin_receipt_attached", transaction_id=tx_id,
        account_id=existing.get("account_id"), size=int(size or 0),
    )
    merged = {**existing, **updates}
    merged["_previous_receipt_path"] = existing.get("receipt_storage_path")
    return merged, []


# ── Queries — read-time balances on (date, sequence) order ─────────────────


def get_transaction(tx_id: str) -> Optional[dict]:
    try:
        doc = db.collection(TRANSACTIONS_COLLECTION).document(tx_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("admin transaction read failed")
        return None


def list_register(
    account_id: str, date_from=None, date_to=None, limit: int = 10000
) -> tuple[list[dict], bool]:
    """One account's COMPLETE register for a period, in ``(date, sequence)``
    order — the ledger order, now that dates are free. Date bounds pushed to
    Firestore on the (account_id, date, sequence) composite; ``truncated``
    returned, never swallowed. Fails CLOSED (propagates)."""
    query = db.collection(TRANSACTIONS_COLLECTION).where(
        filter=FieldFilter("account_id", "==", account_id)
    )
    df = _midnight_utc(date_from)
    dt = _midnight_utc(date_to)
    if df is not None:
        query = query.where(filter=FieldFilter("date", ">=", df))
    if dt is not None:
        # date is stored at midnight, so « <= end of day dt » is « < dt+1 ».
        query = query.where(filter=FieldFilter("date", "<", dt + timedelta(days=1)))
    query = query.order_by("date").order_by("sequence")
    rows = [d.to_dict() for d in query.limit(limit + 1).stream()]
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


def opening_ledger_balance(account_id: str, as_of_exclusive) -> tuple[int, bool]:
    """Carried-forward ledger balance the day BEFORE *as_of_exclusive*:
    Σ ``admin_delta`` over the FULL pre-period history (there is no frozen
    column to read back — the deliberate trade of this module). Returns
    ``(cents, had_prior_entry)`` — the flag separates « reporté : 0,00 $ »
    from « nothing precedes this period ».

    Cost, stated honestly: the whole pre-period history streams (single-user
    volumes — a few thousand rows a year — make this a sub-second read; a
    year-end checkpoint row is the FUTURE mitigation, deliberately not
    built). Raises when the read truncates: a partial sum is a wrong
    balance, and a register must fail rather than lie."""
    cutoff = _midnight_utc(as_of_exclusive)
    if cutoff is None:
        return 0, False
    rows, truncated = list_register(
        account_id, None, cutoff - timedelta(days=1), limit=100000
    )
    if truncated:
        raise RuntimeError("admin ledger: opening-balance read truncated")
    total = sum(
        admin_delta(r.get("direction", ""), int(r.get("amount", 0))) for r in rows
    )
    return total, bool(rows)


def book_balance_as_of(account_id: str, as_of) -> int:
    """Ledger balance at the END of day *as_of* — Σ ``admin_delta`` over
    rows dated <= as_of. Correct by construction under free dates (no
    ordering invariant to lean on). Raises on truncation (fail CLOSED)."""
    rows, truncated = list_register(account_id, None, as_of, limit=100000)
    if truncated:
        raise RuntimeError("admin ledger: as-of balance read truncated")
    return sum(
        admin_delta(r.get("direction", ""), int(r.get("amount", 0))) for r in rows
    )


def list_recent_page(
    account_id: str, cursor: Optional[str] = None, limit: int = PAGE_SIZE
) -> tuple[list[dict], Optional[str]]:
    """« Dernières écritures » widget: newest INSERTIONS first (sequence
    DESC — the audit order, deliberately not the ledger order; no running
    balance is shown here). Fails CLOSED."""
    query = (
        db.collection(TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence", direction=firestore.Query.DESCENDING)
    )
    values = decode_cursor(cursor)
    if values and len(values) == 1:
        query = query.start_after({"sequence": values[0]})
    docs = [d.to_dict() for d in query.limit(limit + 1).stream()]
    next_cursor = None
    if len(docs) > limit:
        docs = docs[:limit]
        next_cursor = encode_cursor([docs[-1].get("sequence")])
    return docs, next_cursor


def find_by_trust_transaction(trust_tx_id: str) -> Optional[dict]:
    """The admin recette a trust fee payment auto-created (single-field
    equality — auto-indexed). Fails OPEN to None (a display/orchestration
    aid, never the register)."""
    try:
        q = (
            db.collection(TRANSACTIONS_COLLECTION)
            .where(filter=FieldFilter("trust_transaction_id", "==", trust_tx_id))
            .limit(2)
        )
        rows = [d.to_dict() for d in q.stream()]
    except Exception:
        logger.warning("admin find_by_trust_transaction failed")
        return None
    return rows[0] if rows else None


def sum_invoice_receipts(invoice_id: str) -> int:
    """Σ of ``encaissement_facture`` amounts linked to an invoice whose
    economic effect still stands — the recomputable cumulative behind the
    record_payment projection (single-field equality, auto-indexed).

    Excludes annulée rows AND reversed ones (``reversed_by_id`` set): a
    compensée encaissement corrected by contre-passation (the bounced-
    cheque case) stays « compensée » in the register, but its payment was
    reduced by ``_reduire_paiement`` — counting it would report a false
    mismatch against ``amount_paid`` after the one correction flow the
    two-step lifecycle exists for. Raises on read failure (fail CLOSED —
    callers project money from this)."""
    q = db.collection(TRANSACTIONS_COLLECTION).where(
        filter=FieldFilter("invoice_id", "==", invoice_id)
    )
    total = 0
    for snap in q.stream():
        r = snap.to_dict() or {}
        if (
            r.get("kind") == "encaissement_facture"
            and r.get("status") != "annulée"
            and not r.get("reversed_by_id")
        ):
            total += int(r.get("amount", 0))
    return total


def list_invoice_receipts(invoice_id: str) -> list[dict]:
    """The register entries imputed on an invoice, oldest first.

    The reading counterpart of :func:`sum_invoice_receipts`, and it differs
    from it on two points, both deliberate.

    It KEEPS the annulée and contre-passées rows. That function computes a
    cumulative and must drop anything whose economic effect no longer
    stands; this one feeds the invoice's « Paiements » block, whose whole
    job is to show what happened — a receipt that was reversed is part of
    the history, and hiding it would leave the reader wondering why the
    balance moved. The caller renders the status in full.

    It fails OPEN (``[]`` + a warning), the posture of
    :func:`find_by_trust_transaction`: a display aid must never take a page
    down. ``sum_invoice_receipts`` keeps its fail-closed posture — money is
    projected from it.

    Single-field equality, served by the automatic index. The sort is in
    PYTHON, like ``_list_cleared_after``: ordering server-side would demand
    an ``(invoice_id, date)`` composite for a display block, and the row
    count per invoice is a handful.
    """
    if not invoice_id:
        return []
    try:
        q = db.collection(TRANSACTIONS_COLLECTION).where(
            filter=FieldFilter("invoice_id", "==", invoice_id)
        )
        rows = [snap.to_dict() or {} for snap in q.stream()]
    except Exception:
        logger.warning("admin list_invoice_receipts failed")
        return []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda r: (r.get("date") or epoch, int(r.get("sequence") or 0)))
    return rows


# ── Reconciliation as-of machinery ─────────────────────────────────────────


def _list_by_status(account_id: str, status: str, direction: str) -> list[dict]:
    """All entries of one status+direction on the account (composite
    (account_id, status, direction, sequence))."""
    q = (
        db.collection(TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .where(filter=FieldFilter("status", "==", status))
        .where(filter=FieldFilter("direction", "==", direction))
        .order_by("sequence")
    )
    return [d.to_dict() for d in q.stream()]


def _list_en_circ(account_id: str, direction: str, as_of) -> list[dict]:
    rows = _list_by_status(account_id, "en_circulation", direction)
    if as_of is not None:
        cutoff = _as_utc(as_of).date()
        rows = [r for r in rows if _as_utc(r.get("date")).date() <= cutoff]
    return rows


def list_outstanding(account_id: str, as_of=None) -> list[dict]:
    """Outstanding déboursés (cheques / posted card charges) at as_of."""
    return _list_en_circ(account_id, "déboursé", as_of)


def list_in_transit(account_id: str, as_of=None) -> list[dict]:
    """In-transit recettes (deposits / card payments-credits) at as_of."""
    return _list_en_circ(account_id, "recette", as_of)


def _list_cleared_after(account_id: str, as_of) -> list[dict]:
    """Resurrection set (b): entries dated <= as_of still outstanding AT
    as_of because they cleared later. Counted in the as-of variance, never
    tickable (already compensée). The trust logic, ported intact."""
    cutoff = _as_utc(as_of).date()
    rows = _list_by_status(account_id, "compensée", "déboursé") + _list_by_status(
        account_id, "compensée", "recette"
    )
    kept = []
    for r in rows:
        cd = r.get("cleared_date")
        if not isinstance(cd, (datetime, date)):
            continue
        if _as_utc(r.get("date")).date() <= cutoff and _as_utc(cd).date() > cutoff:
            kept.append(r)
    kept.sort(key=lambda r: int(r.get("sequence", 0)))
    return kept


def _list_annulled_after(account_id: str, as_of) -> list[dict]:
    """Resurrection set (c): an annulée ORIGINAL dated <= as_of whose
    reversal postdates as_of was still en_circulation at as_of. Fail CLOSED
    on an unreadable reversal row."""
    cutoff = _as_utc(as_of).date()
    rows = _list_by_status(account_id, "annulée", "déboursé") + _list_by_status(
        account_id, "annulée", "recette"
    )
    by_id = {r.get("id"): r for r in rows}
    kept = []
    for r in rows:
        reverser_id = r.get("reversed_by_id")
        if not reverser_id or r.get("reverses_id"):
            continue
        if _as_utc(r.get("date")).date() > cutoff:
            continue
        reverser = by_id.get(reverser_id)
        if reverser is None:
            reverser = get_transaction(reverser_id)
            if reverser is None:
                raise RuntimeError("admin ledger: reversal row unreadable for as-of")
        if _as_utc(reverser.get("date")).date() > cutoff:
            kept.append(r)
    kept.sort(key=lambda r: int(r.get("sequence", 0)))
    return kept


def reconciliation_as_of_context(account_id: str, as_of) -> dict:
    """Every as-of input the worksheet render and the completion gate share
    (the trust one-seam rule — the two can never drift). ``book_as_of`` is a
    Python Σ over the period (no frozen column exists to read back). Raises
    on any read failure — never a silently wrong worksheet."""
    outstanding = list_outstanding(account_id, as_of=as_of)
    in_transit = list_in_transit(account_id, as_of=as_of)
    cleared_later = _list_cleared_after(account_id, as_of)
    annulled_later = _list_annulled_after(account_id, as_of)
    fixed = cleared_later + annulled_later
    return {
        "book_as_of": book_balance_as_of(account_id, as_of),
        "outstanding": outstanding,
        "in_transit": in_transit,
        "cleared_later": cleared_later,
        "annulled_later": annulled_later,
        "fixed_outstanding_total": sum(
            int(e.get("amount", 0)) for e in fixed if e.get("direction") == "déboursé"
        ),
        "fixed_in_transit_total": sum(
            int(e.get("amount", 0)) for e in fixed if e.get("direction") == "recette"
        ),
    }


# ── Reconciliations ────────────────────────────────────────────────────────


def create_reconciliation(
    account_id: str, period_end, statement_balance: int
) -> tuple[Optional[dict], list[str]]:
    """Open a brouillon reconciliation. At most one brouillon per account;
    period_end after the last complétée, never future. For a card the
    statement balance is entered AS THE STATEMENT STATES IT (solde dû,
    positive when owed) — the sign conversion happens at variance time."""
    if get_account(account_id) is None:
        return None, [_ABORT_MESSAGES["compte_introuvable"]]
    if statement_balance is None:
        return None, [_ABORT_MESSAGES["relevé_requis"]]
    if not isinstance(statement_balance, int) or isinstance(statement_balance, bool):
        return None, ["Le solde du relevé doit être un montant en cents."]
    pe = _midnight_utc(period_end)
    if pe is None:
        return None, ["La date de fin de période est requise."]
    # Montréal clock: a UTC check would accept « tomorrow » every evening —
    # and a completed reconciliation with a tomorrow period_end would LOCK
    # the account for a day (every entry date ≤ floor refused, every later
    # one refused as future), with no undo (a complétée never deletes).
    if pe.date() > today_mtl():
        return None, [_ABORT_MESSAGES["conciliation_période_future"]]

    existing = list_reconciliations(account_id)
    if any(r.get("status") == "brouillon" for r in existing):
        return None, ["Une conciliation est déjà en cours pour ce compte."]
    completed = [r for r in existing if r.get("status") == "complétée"]
    if completed:
        last_pe = max(_as_utc(r.get("period_end")).date() for r in completed)
        if pe.date() <= last_pe:
            return None, ["La période doit suivre la dernière conciliation complétée."]

    now = datetime.now(timezone.utc)
    rec_id = str(uuid.uuid4())
    doc = {
        "id": rec_id,
        "account_id": account_id,
        "period_end": pe,
        "statement_balance": int(statement_balance),
        "book_balance": 0,
        "outstanding_total": 0,
        "in_transit_total": 0,
        "variance": 0,
        "status": "brouillon",
        "completed_date": None,
        "cleared_transaction_ids": [],
        "notes": "",
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    }
    try:
        db.collection(RECONCILIATIONS_COLLECTION).document(rec_id).set(doc)
    except Exception:
        log_unexpected("admin reconciliation write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return doc, []


def get_reconciliation(rec_id: str) -> Optional[dict]:
    try:
        doc = db.collection(RECONCILIATIONS_COLLECTION).document(rec_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("admin reconciliation read failed")
        return None


def list_reconciliations(account_id: Optional[str] = None) -> list[dict]:
    """List reconciliations, newest period first."""
    query = db.collection(RECONCILIATIONS_COLLECTION)
    if account_id:
        query = query.where(filter=FieldFilter("account_id", "==", account_id))
    query = query.order_by("period_end", direction=firestore.Query.DESCENDING)
    return [d.to_dict() for d in query.stream()]


def delete_reconciliation(rec_id: str) -> tuple[bool, list[str]]:
    """Abandon a DRAFT reconciliation (transactional status re-read — the
    trust TOCTOU close). A complétée is the lock's audit trail: never."""
    rec_ref = db.collection(RECONCILIATIONS_COLLECTION).document(rec_id)
    transaction = db.transaction()
    info: dict = {}

    @firestore.transactional
    def _abandon(txn) -> None:
        snap = rec_ref.get(transaction=txn)
        if not snap.exists:
            raise _TxnAbort("conciliation_introuvable")
        rec = snap.to_dict()
        if rec.get("status") != "brouillon":
            raise _TxnAbort("conciliation_non_brouillon")
        info["account_id"] = rec.get("account_id")
        txn.delete(rec_ref)

    try:
        _abandon(transaction)
    except _TxnAbort as abort:
        return False, [_ABORT_MESSAGES.get(abort.reason, "Conciliation refusée.")]
    except Exception as exc:
        logger.error("admin delete_reconciliation failed: %s", type(exc).__name__)
        return False, ["Erreur lors de la suppression. Veuillez réessayer."]

    log_admin_ledger_event(
        "admin_reconciliation_abandoned", reconciliation_id=rec_id,
        account_id=info.get("account_id"),
    )
    return True, []


def complete_reconciliation(
    rec_id: str, cleared_tx_ids: list
) -> tuple[Optional[dict], list[str]]:
    """Finalize a reconciliation — the act that LOCKS the period.

    Same skeleton as trust: an as-of pre-pass computes the gate (variance
    must be 0), then one transaction re-reads and commits. TWO admin-specific
    turns: the statement figure passes through ``statement_to_ledger`` (a
    card statement states the solde dû), and the in-txn per-entry re-read
    verifies status AND ETAG against the pre-pass — entries are EDITABLE
    here until this very lock, so « the date is immutable » no longer
    carries the concurrency duty it did in trust."""
    rec = get_reconciliation(rec_id)
    if rec is None:
        return None, [_ABORT_MESSAGES["conciliation_introuvable"]]
    if rec.get("status") != "brouillon":
        return None, [_ABORT_MESSAGES["conciliation_non_brouillon"]]

    account_id = rec["account_id"]
    account = get_account(account_id)
    if account is None:
        return None, [_ABORT_MESSAGES["compte_introuvable"]]
    account_etag = account.get("etag")
    period_end = _as_utc(rec.get("period_end"))
    statement_ledger = statement_to_ledger(
        account.get("account_type", ""), int(rec.get("statement_balance", 0))
    )
    checked_ids = set(cleared_tx_ids or [])

    try:
        ctx = reconciliation_as_of_context(account_id, period_end)
    except Exception as exc:
        logger.error("admin reconciliation as-of read failed: %s", type(exc).__name__)
        return None, ["Erreur lors de la conciliation. Veuillez réessayer."]
    tickable = {e["id"]: e for e in ctx["outstanding"] + ctx["in_transit"]}
    if not checked_ids.issubset(tickable.keys()):
        return None, [_ABORT_MESSAGES["conciliation_écriture_invalide"]]
    remaining = [e for eid, e in tickable.items() if eid not in checked_ids]
    outstanding_after = ctx["fixed_outstanding_total"] + sum(
        int(e["amount"]) for e in remaining if e.get("direction") == "déboursé"
    )
    in_transit_after = ctx["fixed_in_transit_total"] + sum(
        int(e["amount"]) for e in remaining if e.get("direction") == "recette"
    )
    book_balance = ctx["book_as_of"]
    variance = reconciliation_variance(
        statement_ledger, book_balance, outstanding_after, in_transit_after
    )
    if variance != 0:
        log_admin_ledger_event(
            "admin_reconciliation_variance", "refused",
            reconciliation_id=rec_id, account_id=account_id, variance_cents=variance,
        )
        return None, [_ABORT_MESSAGES["conciliation_variance"]]

    now = datetime.now(timezone.utc)
    rec_ref = db.collection(RECONCILIATIONS_COLLECTION).document(rec_id)
    account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    pre_pass = {eid: tickable[eid] for eid in checked_ids}
    tx_refs = [db.collection(TRANSACTIONS_COLLECTION).document(t) for t in checked_ids]
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _complete(txn) -> None:
        r_snap = rec_ref.get(transaction=txn)
        if not r_snap.exists or r_snap.to_dict().get("status") != "brouillon":
            raise _TxnAbort("conciliation_non_brouillon")
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        acc = acc_snap.to_dict()
        # Etag sentinel: every admin write path (create/update/delete/clear/
        # reverse) regenerates the account etag, so any register movement
        # between the pre-pass and this commit is caught here.
        if acc.get("etag") != account_etag:
            raise _TxnAbort("conciliation_modifiée")

        entries = []
        for ref in tx_refs:
            snap = ref.get(transaction=txn)
            e = snap.to_dict() if snap.exists else None
            # Status AND etag re-verified: an edit (date, amount, direction)
            # between pre-pass and commit regenerates the entry etag.
            if (
                not e
                or e.get("status") != "en_circulation"
                or e.get("etag") != pre_pass[e.get("id", "")].get("etag")
            ):
                raise _TxnAbort("conciliation_modifiée")
            entries.append(e)

        cd = _midnight_utc(period_end)
        for ref, e in zip(tx_refs, entries):
            txn.update(ref, {
                "status": "compensée",
                "cleared_date": cd,
                "reconciliation_id": rec_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        txn.update(account_ref, {"updated_at": now, "etag": str(uuid.uuid4())})
        finalized = {
            **rec,
            "status": "complétée",
            "book_balance": book_balance,
            "outstanding_total": outstanding_after,
            "in_transit_total": in_transit_after,
            "variance": 0,
            "completed_date": now,
            "cleared_transaction_ids": [e["id"] for e in entries],
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        }
        txn.set(rec_ref, finalized)
        result["reconciliation"] = finalized
        result["cleared_count"] = len(entries)

    try:
        with span("admin.reconcile", account_id=account_id, cleared_count=len(checked_ids)):
            _complete(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Conciliation refusée.")]
    except Exception as exc:
        logger.error("admin complete_reconciliation failed: %s", type(exc).__name__)
        return None, ["Erreur lors de la conciliation. Veuillez réessayer."]

    log_admin_ledger_event(
        "admin_reconciliation_completed", reconciliation_id=rec_id,
        account_id=account_id, cleared_count=result["cleared_count"],
    )
    return result["reconciliation"], []


# ── Firm snapshot + reconciliation-overdue predicate ───────────────────────

# Copied from models/trust.py (PA-D06 doctrine baked in) rather than imported:
# the pair is private there, and the two modules must be free to diverge —
# but as of 2026-08-13 the logic is IDENTICAL; a fix on one side should be
# mirrored on the other.
RECONCILIATION_GRACE_DAYS = 30


def _month_end_on_or_before(d: date) -> date:
    """Latest calendar month-end on or before *d* (a date)."""
    next_month_first = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    this_month_end = next_month_first - timedelta(days=1)
    if d == this_month_end:
        return d
    return d.replace(day=1) - timedelta(days=1)


def _reconciliation_overdue(
    last_period_end,
    now: Optional[datetime] = None,
    account_floor: Optional[datetime] = None,
) -> bool:
    """True when some month-end whose grace has expired is unreconciled
    (see trust._reconciliation_overdue for the two PA-D06 failure modes
    this shape fixes)."""
    now = now or datetime.now(timezone.utc)
    # Montréal calendar day, never UTC's — the evening-band doctrine; see the
    # trust twin for the full rationale. Mirror fixes both sides or neither.
    today = to_mtl(now).date()
    due_through = _month_end_on_or_before(
        today - timedelta(days=RECONCILIATION_GRACE_DAYS)
    )
    if account_floor is not None and to_mtl(_as_utc(account_floor)).date() > due_through:
        return False
    if last_period_end is None:
        return True
    return _as_utc(last_period_end).date() < due_through


def get_firm_admin_snapshot() -> dict:
    """Firm-wide administration picture (journal header + nav aids). Each
    account row gains its display balance and reconciliation state. Fails
    CLOSED on the account list (the caller decides); per-account extras
    degrade to safe defaults."""
    now = datetime.now(timezone.utc)
    accounts = [dict(a) for a in list_accounts()]
    recs = list_reconciliations()
    completed = [r for r in recs if r.get("status") == "complétée"]
    any_overdue = False
    for a in accounts:
        a["display_balance"] = display_balance(
            a.get("account_type", ""), int(a.get("ledger_balance", 0))
        )
        a["balance_label"] = BALANCE_LABELS.get(a.get("account_type", ""), "Solde")
        mine = [
            _as_utc(r.get("period_end"))
            for r in completed
            if r.get("account_id") == a["id"]
        ]
        a_last = max(mine, default=None)
        a["last_reconciliation_date"] = a_last
        a["never_reconciled"] = a_last is None
        # Aucune conciliation n'est due après clôture (miroir trust — voir
        # le commentaire du jumeau ; revue 2026-08-15).
        if a.get("status") == "fermé":
            a["reconciliation_overdue"] = False
        else:
            a["reconciliation_overdue"] = _reconciliation_overdue(
                a_last, now, account_floor=a.get("created_at")
            )
        any_overdue = any_overdue or a["reconciliation_overdue"]
    return {
        "accounts": accounts,
        "reconciliation_overdue": any_overdue,
    }
