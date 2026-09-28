"""Invoice Firestore CRUD, tax computation, and line-item management.

``create_invoice`` has two branches. The ordinary one — the GENERATED path —
allocates a year-sequential number from the transactional counter. The
IMPORT branch (keyword-only arguments, unreachable from ``request.form``)
keeps a number the previous system already issued, never touches the
counter, and refuses rather than write anything it cannot reconcile — a book
of account is complete or it is wrong.

Everything ``create_invoice`` decides before its transaction lives in ONE
function, :func:`plan_invoice` — the source selection, the refusals, the
totals, the issuance checks and the warnings — so that a preview of an
invoice (the connector's read tool, lot 3b) and the write can never drift:
the retired ``dry_run`` doubled every write into two model calls that the
handlers had to keep in step by hand, and that is exactly the drift this
shape removes.

A brouillon is corrected by :func:`update_invoice_draft` — notes, payment
terms, due date and a fresh billing-address snapshot, never a money figure
or a line item.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, concurrency, db, provenance
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils.deadlines import today_mtl
from utils.format_fr import format_cents_fr
from utils.logging_setup import log_invoice_event, log_unexpected, sanitize_log_value

logger = logging.getLogger(__name__)


# Firestore collection paths
COLLECTION = "invoices"
LINE_ITEMS_SUB = "lineitems"
COUNTERS_COLLECTION = "counters"


class _SourceConflictError(Exception):
    """Raised when a billing source changed concurrently during invoicing."""


class _DuplicateNumberError(Exception):
    """Raised inside the creation transaction when an IMPORTED number is
    already borne by another invoice — aborts, never writes."""


class _StatusRefused(Exception):
    """Refusal raised inside the status transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries.
    """

    def __init__(self, message: str, reason: str = "transition_refusee") -> None:
        super().__init__(message)
        self.reason = reason


class _VoidRefused(Exception):
    """Refusal raised inside the void transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries;
    the French sentence is ``str(exc)``.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class _DraftRefused(Exception):
    """Refusal raised inside the draft-edit transaction (aborts, never writes).

    ``reason`` is the machine-stable code the observability event carries.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# The two registers that can carry a payment imputed on an invoice. String
# LITERALS on purpose — importing models.admin_ledger / models.trust here
# would close an import cycle (both import this module). The test suite pins
# them against each module's own TRANSACTIONS_COLLECTION.
_ADMIN_TRANSACTIONS = "admin_transactions"
_TRUST_TRANSACTIONS = "trust_transactions"

# The keys ``create_invoice`` accepts from its caller's ``data``. Everything
# else on the stored document is decided HERE — the number, the totals, the
# retained sources, the provenance — and the payment state above all: a
# caller-supplied ``status``/``amount_paid``/``paid_date`` used to reach
# storage verbatim, which only the two callers' own whitelists prevented
# (a « payée » invoice with a payment on no ledger is exactly what the
# single-writer doctrine of 2026-08-17 exists to make impossible). A key
# outside this set is dropped, never merged.
_CREATE_DATA_KEYS = frozenset({
    "dossier_id", "dossier_file_number", "dossier_title",
    "client_id", "client_name", "billing_address",
    "date", "due_date", "notes", "payment_terms",
    "retainer_applied", "gst_number", "qst_number",
    "legacy_ref",
    # Honoured by provenance.stamp_create only when it is a VALID via — the
    # connector's creators have always named themselves.
    "created_via",
})


# Valid statuses and their French labels
VALID_STATUSES = ("brouillon", "envoyée", "payée", "en_retard", "annulée")

#: Les statuts d'une facture ÉMISE — celle qui peut encore recevoir un
#: paiement, et dont le solde constitue une créance. Recopié plutôt
#: qu'importé de `models.admin_ledger` / `models.trust`, qui en tiennent
#: chacun le leur : doctrine du vocabulaire du dépôt.
_ISSUED_STATUSES = ("envoyée", "en_retard")

STATUS_LABELS = {
    "brouillon": "Brouillon",
    "envoyée": "Envoyée",
    "payée": "Payée",
    "en_retard": "En retard",
    "annulée": "Annulée",
}

# Allowed status transitions.
#
# « payée » ne se POSE plus à la main (2026-08-17). La comptabilité étant
# l'unique écrivain d'un paiement, le seul chemin vers ce statut est la
# bascule automatique de record_payment — qui écrit le champ directement et
# n'emprunte PAS cette table. Un bouton « Marquer comme payée » rouvrait
# exactement la porte que le retrait du formulaire d'encaissement venait de
# fermer : un statut affirmant un paiement, sans montant, sans date, invisible
# au grand livre. Dix-neuf factures en production sont dans cet état.
#
# En retour, « payée » cesse d'être un cul-de-sac : la sortie existe pour
# corriger les statuts posés à la main AVANT cette doctrine. Elle est refermée
# dès qu'un paiement est INSCRIT — voir available_transitions, sans quoi
# « Rouvrir » puis « Annuler » libérerait les heures d'une facture réellement
# encaissée. (void_invoice regarde désormais l'argent lui-même — 2026-09-26 —
# mais la sortie refermée reste la première ligne : un bouton ne s'affiche
# pas pour être refusé.)
#
# « annulée » figure dans la table comme une ACTION offerte par la fiche, et
# cette action emprunte void_invoice — jamais update_status, qui la refuse
# d'entrée (le changement de statut nu laissait toutes les sources
# facturées pour toujours : void refusait ensuite « déjà annulée » et
# delete_invoice refusait les références restées accrochées).
#
# « en_retard » gagne « envoyée » pour une raison propre : rien n'écrit ce
# statut automatiquement, donc il se pose à la main, et sans autre sortie
# qu'« annulée » une facture marquée en retard par erreur ne se corrigerait
# que destructivement.
STATUS_TRANSITIONS = {
    "brouillon": ("envoyée", "annulée"),
    "envoyée": ("en_retard", "annulée"),
    "en_retard": ("envoyée", "annulée"),
    "payée": ("envoyée",),
}


def _standing_admin_encaissement(row: dict) -> bool:
    """An administration entry whose payment on its invoice still STANDS.

    The predicate of ``models.admin_ledger.sum_invoice_receipts`` —
    ``encaissement_facture``, not annulée, not contre-passée — copied rather
    than imported (import cycle; the test suite pins the two against each
    other on the same rows).
    """
    return (
        row.get("kind") == "encaissement_facture"
        and row.get("status") != "annulée"
        and not row.get("reversed_by_id")
    )


def _standing_trust_fee_payment(row: dict) -> bool:
    """A trust « paiement d'honoraires » (``virement_honoraires``) that still
    stands: not annulée, not contre-passé. Its reversal is minted as a
    ``correction`` row carrying no invoice_id, and the original gains
    ``reversed_by_id`` — an annulée original is the uncleared case."""
    return (
        row.get("purpose") == "virement_honoraires"
        and row.get("status") != "annulée"
        and not row.get("reversed_by_id")
    )


def available_transitions(
    invoice: dict,
    *,
    receipts: Optional[list[dict]] = None,
    trust_payments: Optional[list[dict]] = None,
) -> tuple[str, ...]:
    """Les transitions que CETTE facture peut réellement prendre.

    ``STATUS_TRANSITIONS`` est indexée sur le seul statut : elle ne sait pas
    distinguer un « payée » posé à la main d'un « payée » qu'un encaissement a
    produit. C'est pourtant cette différence qui décide du chemin de
    correction — le premier se rouvre ici, le second se corrige par
    contre-passation, qui réduit le paiement ET rouvre la facture d'elle-même
    (``services/encaissements.reduire_paiement``).

    Sans ce filtre, la réouverture serait un trou : « Rouvrir » puis
    « Annuler » libérerait les heures et dépenses d'une facture réellement
    encaissée. ``void_invoice`` le refuse désormais lui-même, mais une
    autorité qui offrirait l'action pour la voir refusée serait un défaut de
    conception.

    Pour la même raison, « annulée » n'est plus offerte dès qu'un paiement
    est INSCRIT (``amount_paid > 0``, quel que soit le statut) :
    ``void_invoice`` la refuserait. *receipts* — les écritures
    d'administration imputées sur la facture, que la fiche lit déjà
    (``list_invoice_receipts``) — ferme aussi le cas de dérive où une
    écriture tient encore alors que ``amount_paid`` vaut 0.
    *trust_payments* — les écritures du fidéicommis qui la visent
    (``models.trust.list_invoice_fee_payments``) — ferme le dernier : un
    paiement d'honoraires debout dont la recette d'administration
    automatique a échoué (elle est fail-open), donc sans montant inscrit ni
    écriture d'administration. Une fonction pure ne voit que ce qu'on lui
    donne ; si une lecture d'affichage a échoué, ``void_invoice_report``
    relit tout dans sa transaction et refuse, et la route affiche le motif
    en bandeau.

    UNE seule autorité, consommée par ``update_status`` ET par la fiche : un
    bouton qui s'affiche pour être refusé est un défaut de conception.
    """
    current = invoice.get("status", "")
    allowed = STATUS_TRANSITIONS.get(current, ())
    paid = int(invoice.get("amount_paid", 0) or 0) > 0
    if current == "payée" and paid:
        allowed = tuple(s for s in allowed if s != "envoyée")
    if (
        paid
        or any(_standing_admin_encaissement(r) for r in (receipts or ()))
        or any(_standing_trust_fee_payment(r) for r in (trust_payments or ()))
    ):
        allowed = tuple(s for s in allowed if s != "annulée")
    return allowed

# Tax rates stored as basis points (×100 for GST, ×1000 for QST precision)
GST_RATE_BPS = 500       # 5.00%
QST_RATE_BPS = 9975      # 9.975%

DEFAULT_PAYMENT_TERMS = "Payable dans les 30 jours suivant la date de facturation."

# ── Imported (historical) invoice numbers ────────────────────────────────
# A number carried over from the practice's previous system. Free-form on
# purpose — it is ANOTHER system's numbering and never ours to reshape (an
# accounting artifact already sent to a client is never renumbered) — but
# BOUNDED, because it prints on the note d'honoraires, exports to CSV and
# folds into a generated document's display name.
IMPORTED_NUMBER_MAX_LENGTH = 32
_IMPORTED_NUMBER_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " ./-"
)

# The adjustment line's own ceiling. It prints verbatim on the client's
# invoice, so an over-long value is REFUSED rather than sanitize()d down —
# see _adjustment_line_item.
ADJUSTMENT_DESCRIPTION_MAX_LENGTH = 500


def _default_doc() -> dict:
    """Return a dict with every invoice field set to its default value."""
    return {
        "id": "",
        "invoice_number": "",
        "dossier_id": "",
        "dossier_file_number": "",
        "dossier_title": "",
        "client_id": "",
        "client_name": "",
        "billing_address": {
            "name": "",
            "street": "",
            "unit": "",
            "city": "",
            "province": "QC",
            "postal_code": "",
        },
        "date": None,
        "due_date": None,
        "status": "brouillon",
        # Financials (all in cents)
        "subtotal_fees": 0,
        "subtotal_expenses": 0,
        "subtotal": 0,
        "gst_rate": GST_RATE_BPS,
        "gst_amount": 0,
        "qst_rate": QST_RATE_BPS,
        "qst_amount": 0,
        "total": 0,
        "retainer_applied": 0,
        "amount_due": 0,
        # Payment received (lot P, July 2026). Until this existed, payment
        # was representable ONLY as status == "payée": no amount, no date,
        # and a partial payment could not be expressed at all. `amount_due`
        # is frozen at issuance and stays non-zero on a paid invoice, so it
        # is NOT a balance — the live balance is amount_due − amount_paid,
        # DERIVED (see `balance_of`), never stored.
        "amount_paid": 0,          # cents, 0 ≤ amount_paid ≤ total
        "paid_date": None,         # date-only at midnight UTC; None = unpaid
        # Tax numbers (from config, snapshotted at creation)
        "gst_number": "",
        "qst_number": "",
        "notes": "",
        "payment_terms": DEFAULT_PAYMENT_TERMS,
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


def _default_line_item() -> dict:
    """Return a dict with every line item field set to its default value."""
    return {
        "id": "",
        "type": "fee",
        "source_id": "",
        "date": None,
        "description": "",
        "hours": None,
        "rate": None,
        "amount": 0,
        "taxable": True,
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        elif isinstance(val, dict):
            out[key] = _sanitize_data(val)
        else:
            out[key] = val
    return out


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id", "").strip():
        errors.append("Un dossier doit être associé à cette facture.")

    if not data.get("date"):
        errors.append("La date de la facture est requise.")

    return errors


# ── Tax computation ──────────────────────────────────────────────────────


def compute_totals(line_items: list[dict]) -> dict:
    """Compute subtotals, taxes, and total from a list of line items.

    All monetary values are integer cents. Uses Python Decimal for tax
    computation to avoid floating-point issues, as required by CLAUDE.md.
    """
    subtotal_fees = 0
    subtotal_expenses = 0
    taxable_amount = 0

    for item in line_items:
        amt = item.get("amount", 0)
        if item.get("type") == "fee":
            subtotal_fees += amt
        else:
            subtotal_expenses += amt
        if item.get("taxable", True):
            taxable_amount += amt

    subtotal = subtotal_fees + subtotal_expenses

    # Tax computation with Decimal (QST is NOT compounded on GST since 2013)
    taxable_dec = Decimal(taxable_amount)
    gst_amount = int(
        (taxable_dec * Decimal("0.05")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    qst_amount = int(
        (taxable_dec * Decimal("0.09975")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    total = subtotal + gst_amount + qst_amount

    return {
        "subtotal_fees": subtotal_fees,
        "subtotal_expenses": subtotal_expenses,
        "subtotal": subtotal,
        "gst_amount": gst_amount,
        "qst_amount": qst_amount,
        "total": total,
    }


# ── Invoice number generation ────────────────────────────────────────────


def _scan_max_invoice_seq(prefix: str) -> int:
    """Return the highest existing invoice sequence for *prefix* via a scan.

    Used only to seed the transactional counter the first time it is created
    for a given year, so pre-counter numbering continues without duplicates.
    """
    max_seq = 0
    for doc in db.collection(COLLECTION).stream():
        num = doc.to_dict().get("invoice_number", "")
        if num.startswith(prefix):
            try:
                max_seq = max(max_seq, int(num[len(prefix):]))
            except (ValueError, IndexError):
                continue
    return max_seq


# « YYYY-FNNN » — the canonical scheme again since the 2026-08-12 user
# decision (the per-file « {file_number}-NN » scheme of 2026-07-17 lasted
# four weeks — the six invoices it minted keep their numbers for ever, an
# accounting artifact sent to a client is never renumbered). The year is the
# MONTRÉAL calendar year (``today_mtl`` — the house clock seam; the UTC year
# would stamp a Dec 31 evening invoice with the next millésime). Sequence:
# 3-digit zero-padded, rolling to 4+ past 999, from the monotonic
# transactional counter ``counters/invoices-{year}`` — never reused, never
# decremented; a deleted invoice leaves a hole. On the first use of a year
# the counter is seeded by ``_scan_max_invoice_seq`` so pre-counter numbering
# continues without duplicates (per-file numbers never match the « YYYY-F »
# prefix and are ignored). A number is never guessed, which could mint a
# colliding one: any failure aborts the creation.
#
# The allocation happens INSIDE ``create_invoice``'s own transaction
# (2026-09-26). It used to run in a transaction of its own, committed BEFORE
# the invoice's: a source conflict, a duplicate import number or any other
# abort of the invoice transaction then left the number consumed with no
# invoice under it — a hole in the fee-journal sequence the lawyer could not
# explain. Now the counter write commits with the invoice or not at all.
# There is deliberately no standalone allocator any more: one that commits on
# its own IS the defect.
#
# An IMPORTED historical number never reaches the counter (user decision
# 2026-08-16): ``create_invoice`` resolves it through
# ``_clean_imported_number``, and on that path the counter is neither read
# nor written.


def _invoice_counter_ref(year: str):
    """The transactional counter of *year*'s « YYYY-F » sequence."""
    return db.collection(COUNTERS_COLLECTION).document(f"invoices-{year}")


def _counter_seed(counter_ref, prefix: str) -> int:
    """The seed of a year's FIRST allocation: the highest number already
    issued under *prefix*, or 0 when the counter already exists.

    Read OUTSIDE the invoice transaction on purpose — a full-collection
    stream does not belong inside one — and only when the counter document
    is absent. A concurrent first-of-year creation computes the same seed;
    both transactions then read the absent counter and contend, and the
    loser's retry re-reads the counter the winner wrote. Raises on any read
    failure: seeding from a guess could reissue a number.
    """
    if counter_ref.get().exists:
        return 0
    return _scan_max_invoice_seq(prefix)


def _next_invoice_number(counter_snapshot, seed: int, prefix: str) -> tuple[str, int]:
    """``(number, next_seq)`` from the counter as read INSIDE the transaction."""
    current = (
        int((counter_snapshot.to_dict() or {}).get("seq", 0))
        if counter_snapshot.exists else 0
    )
    next_seq = max(current, seed) + 1
    return f"{prefix}{next_seq:03d}", next_seq


# ── Billing address snapshot ─────────────────────────────────────────────


def billing_address_from(partie: dict) -> dict:
    """Snapshot the billing address from a partie record.

    Lifted VERBATIM from ``routes/invoices._build_billing_address`` so the web
    form and the MCP connector cannot drift on which block an invoice
    snapshots — the same client billed at two different addresses depending
    on which surface issued the invoice is exactly the failure this prevents.

    Behaviour is deliberately UNCHANGED: work address first, personal
    fallback, province defaulting to « QC ».
    ``utils.template_fields.selected_address`` is the ONE authority on which
    block a given ROLE should try first, and this function predates it and
    does work-then-personal unconditionally. Reconciling the two would MOVE
    the address of existing invoices and is a separate decision — do not
    « fix » it here. Note also the shape: {name, street, unit, city,
    province, postal_code}. ``utils/invoice_docx._partie_from_billing_address``
    maps ``billing["name"]`` into the recipient of the Word note d'honoraires,
    and ``selected_address`` returns no ``name`` key at all — swapping to it
    would produce documents with a blank addressee.
    """
    from models.partie import display_name

    name = display_name(partie)
    # Prefer work address if available, fall back to personal
    if partie.get("work_address_street"):
        return {
            "name": name,
            "street": partie.get("work_address_street", ""),
            "unit": partie.get("work_address_unit", ""),
            "city": partie.get("work_address_city", ""),
            "province": partie.get("work_address_province", "QC"),
            "postal_code": partie.get("work_address_postal_code", ""),
        }
    return {
        "name": name,
        "street": partie.get("address_street", ""),
        "unit": partie.get("address_unit", ""),
        "city": partie.get("address_city", ""),
        "province": partie.get("address_province", "QC"),
        "postal_code": partie.get("address_postal_code", ""),
    }


# ── Historical import: number, adjustment line ───────────────────────────


def _is_live_sequence_number(number: str) -> bool:
    """True when *number* falls in the namespace our own counter owns.

    Deliberately the WHOLE « {current Montréal year}-F » prefix, matched the
    way ``_scan_max_invoice_seq`` matches it — NOT « prefix + digits ».
    ``int()`` strips whitespace, so « 2026-F 12 » fails ``str.isdigit`` yet
    still parses to 12 in the seeding scan; anything sharing the prefix is
    ours.

    PAST years are not in the namespace: their counter can never be created
    again, ``today_mtl`` only moving forward. FUTURE years are not either,
    for a different reason — their counter does not exist yet, so its first
    use SEEDS from ``_scan_max_invoice_seq``, which sees the imported number
    and continues above it. The CURRENT year is the one hole: its counter
    already exists, seeding will never run for it again, and a planted
    number would be handed out a second time.
    """
    return number.startswith(f"{today_mtl().strftime('%Y')}-F")


def invoice_number_exists(number: str) -> bool:
    """True when an invoice already bears *number*.

    RAISES on query failure. The caller is a dry run asking « would the real
    call be refused? », and a swallowed error answering « no » is exactly the
    lie the dry-run contract exists to prevent. The authoritative check stays
    inside ``create_invoice``'s transaction; this one exists so a preview can
    refuse what the write would refuse.
    """
    wanted = (number or "").strip()
    if not wanted:
        return False
    docs = list(
        db.collection(COLLECTION)
        .where(filter=FieldFilter("invoice_number", "==", wanted))
        .limit(1)
        .stream()
    )
    return bool(docs)


def _clean_imported_number(raw: object) -> tuple[str, list[str]]:
    """Validate a historical invoice number. Returns ("", [erreurs]) on refusal.

    ``sanitize`` is deliberately NOT used here: its whitelist is wider and it
    TRUNCATES. Silently shortening an accounting identifier is exactly the
    corruption this refuses to commit — over-long is an error, never a
    haircut.
    """
    if not isinstance(raw, str):
        return "", ["Le numéro de facture importé doit être une chaîne."]
    number = raw.strip()
    if not number:
        return "", ["Le numéro de facture importé est requis."]
    if len(number) > IMPORTED_NUMBER_MAX_LENGTH:
        return "", [
            "Le numéro de facture importé dépasse "
            f"{IMPORTED_NUMBER_MAX_LENGTH} caractères. Il n'est jamais "
            "tronqué : corrigez-le."
        ]
    bad = sorted({c for c in number if c not in _IMPORTED_NUMBER_CHARS})
    if bad:
        return "", [
            "Le numéro de facture importé contient des caractères non "
            f"admis : {' '.join(bad)}. Lettres, chiffres, espace, point, "
            "barre oblique et trait d'union seulement."
        ]
    if _is_live_sequence_number(number):
        year = today_mtl().strftime("%Y")
        return "", [
            f"« {number} » appartient à la numérotation courante de Pallas "
            f"Athéna ({year}-F…) : un numéro importé ne peut pas emprunter "
            "le millésime en cours, sinon le compteur le réattribuerait plus "
            "tard."
        ]
    return number, []


def _adjustment_line_item(adjustment: dict) -> tuple[Optional[dict], list[str]]:
    """Materialize a named adjustment as ONE extra line item.

    The escape hatch for a legacy invoice whose printed total cannot be
    reconstructed from its lines — a courtesy write-down, a rounding, a
    credit. The gap is written ON the invoice rather than hidden by a
    tolerance or refused outright (user decision 2026-08-16).

    Typed ``fee`` because a courtesy write-down reduces HONORAIRES: that puts
    it in the « Honoraires » column of the Barreau fee journal and leaves
    ``expense_split`` untouched, which only carves up disbursements. It is
    the ONE line item in the system carrying no ``source_id`` — which is why
    the caller must NAME it: an unexplained amount on a client's invoice is
    worse than a refusal.

    ``taxable`` decides whether the adjustment moves the tax base. True (the
    default) reproduces an invoice whose GST/QST were computed on the
    reduced amount; False reproduces one discounted after tax.
    """
    if not isinstance(adjustment, dict):
        return None, ["L'ajustement doit être un objet."]

    amount = adjustment.get("amount_cents")
    if not isinstance(amount, int) or isinstance(amount, bool):
        return None, ["`adjustment.amount_cents` doit être un entier de cents."]
    if amount == 0:
        return None, [
            "Un ajustement de 0 $ n'explique rien : retirez-le, ou donnez le "
            "montant réel de l'écart."
        ]

    raw_description = adjustment.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        return None, [
            "`adjustment.description` est requise : l'écart doit être nommé "
            "sur la facture (« Remise de courtoisie », « Arrondi »…)."
        ]
    description = raw_description.strip()
    if len(description) > ADJUSTMENT_DESCRIPTION_MAX_LENGTH:
        return None, [
            "`adjustment.description` dépasse "
            f"{ADJUSTMENT_DESCRIPTION_MAX_LENGTH} caractères."
        ]
    # Length is already refused above, so this can only strip markup — it can
    # never truncate a description the caller believed was stored whole.
    description = sanitize(description, max_length=ADJUSTMENT_DESCRIPTION_MAX_LENGTH)
    if not description:
        return None, ["`adjustment.description` est vide après nettoyage."]

    taxable = adjustment.get("taxable", True)
    if not isinstance(taxable, bool):
        return None, ["`adjustment.taxable` doit être un booléen."]

    return {
        **_default_line_item(),
        "id": str(uuid.uuid4()),
        "type": "fee",
        "source_id": "",
        "date": None,
        "description": description,
        "hours": None,
        "rate": None,
        "amount": amount,
        "taxable": taxable,
    }, []


# ── The plan of an invoice: every decision taken before the transaction ─


@dataclass
class InvoicePlan:
    """What an invoice built from these sources would be — computed, never
    written.

    Returned by :func:`plan_invoice` WHETHER OR NOT the invoice may be
    issued: a refused plan still carries what it could compute (the skip
    reason of every source, the lines retained so far) so that a preview can
    show WHY, from the same computation the write runs.

    * ``skipped`` — ``{source id: French reason}``, one entry per source
      that cannot become a line (missing, already billed and on which
      invoice, another dossier's, non-billable on the generated path);
    * ``errors`` — the refusals, in French; empty means ``create_invoice``
      proceeds to its number and its transaction. ``refusal_reason`` is the
      machine-stable code of the first one, for the observability event;
    * ``warnings`` — true facts the caller should SHOW, never refusals (the
      number's year differs from the invoice date's; a provision supplied on
      the generated path was ignored).
    """

    line_items: list[dict] = field(default_factory=list)
    valid_entry_ids: list[str] = field(default_factory=list)
    valid_expense_ids: list[str] = field(default_factory=list)
    # etag captured at pre-read time; the transaction re-checks it so a
    # concurrent content edit (hours/amount) aborts instead of snapshotting
    # a stale value into the line items.
    expected_etags: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    retainer_applied: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refusal_reason: str = ""

    def refuse(self, errors: list[str], reason: str) -> "InvoicePlan":
        self.errors = list(errors)
        self.refusal_reason = reason
        return self


#: Where the firm's GST/QST registration numbers are entered — named by
#: every refusal that needs them, so the lawyer knows the one fix.
SETTINGS_TAX_NUMBERS_PATH = "« Paramètres → Profil du cabinet »"


def number_year_warning(invoice_date: object, number: str) -> str:
    """'' unless *number* is one of OUR sequence numbers (« YYYY-F… ») whose
    year is not the year of *invoice_date*.

    The number follows the Montréal year of the day the invoice is ISSUED
    (``today_mtl``), never its date — the documented doctrine, and the only
    one that keeps a year's fee-journal sequence contiguous. So an invoice
    dated 31 December and issued on 2 January carries the NEW year's number.
    That is correct, and surprising: this sentence says it before the
    invoice is sent. A number from another system (an import) is not ours to
    comment on — but this function judges the SHAPE only, and an imported
    number may well read « 2019-F014 », so only the generated path may ask
    it (``plan_invoice`` and the web form do; never hand it an import).

    String operations only, no pattern — pure, and linear by construction.
    """
    number = (number or "").strip()
    if not (len(number) >= 6 and number[:4].isdigit() and number[4:6] == "-F"):
        return ""
    when = invoice_date.date() if isinstance(invoice_date, datetime) else invoice_date
    if not isinstance(when, date) or str(when.year) == number[:4]:
        return ""
    return (
        f"La facture est datée de {when.year}, mais son numéro suit l'année "
        f"de son émission ({number[:4]}-F…) : un numéro de facture suit "
        "toujours l'année où la facture est créée, jamais sa date. Vérifiez "
        "la date avant d'envoyer la facture."
    )


def _calendar_day(value: object) -> Optional[date]:
    """The UTC calendar date of a date-only field (stored at midnight UTC)."""
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return None


def issuance_refusals(
    merged: dict,
    totals: dict,
    *,
    generated: bool,
    client: Optional[dict],
) -> list[tuple[str, str]]:
    """What forbids ISSUING this invoice to its client — pure.

    Returns ``[(French refusal, machine reason), …]``; empty means none.
    *client* is the partie ``merged["client_id"]`` names, as READ by the
    caller (``None`` when the id names nothing on file, or is blank).

    On BOTH paths:

    * a ``client_id`` that names no contact on file. The billing address is
      FROZEN on the invoice, so a snapshot of a client that does not resolve
      is a blank address on a document the client will hold. (An imported
      invoice for a dossier that genuinely has NO client stays possible: the
      previous system issued it, and it is reproduced, not reissued.)

    On the GENERATED path only — an invoice this application issues today:

    * no client at all: an invoice is issued TO a client;
    * a blank client name, or a billing address the caller could not
      snapshot (its name is empty — ``billing_address_from`` always fills it
      from a contact on file, so an empty one means the caller's own read of
      the contact failed);
    * taxes charged under an EMPTY registration number. The numbers are
      snapshotted from the firm profile and never rewritten (there is no
      update of a money figure on an invoice), and CLAUDE.md records 50+
      invoices issued with blank numbers before the profile could hold
      them. Checked per tax, and only when that tax is actually charged: a
      wholly non-taxable invoice needs neither;
    * a due date before the invoice date.

    The import path reproduces historical paper verbatim and is spared
    those four: its numbers, due dates and client are what was sent.
    """
    out: list[tuple[str, str]] = []
    client_id = str(merged.get("client_id") or "").strip()
    if client_id and client is None:
        out.append((
            "Le client du dossier est introuvable : l'adresse de facturation, "
            "figée sur la facture, serait vide. Corrigez les parties du "
            "dossier, puis recréez la facture. Rien n'a été créé.",
            "client_introuvable",
        ))
    if not generated:
        return out
    if not client_id:
        out.append((
            "Le dossier n'a aucun client : une facture ne s'émet qu'au client "
            "d'un dossier. Ajoutez le client au dossier, puis recréez la "
            "facture. Rien n'a été créé.",
            "aucun_client",
        ))
    elif client is not None:
        if not str(merged.get("client_name") or "").strip():
            out.append((
                "Le nom du client du dossier est vide : il s'imprime sur la "
                "facture et au journal des honoraires. Corrigez la fiche du "
                "dossier, puis recréez la facture. Rien n'a été créé.",
                "nom_client_vide",
            ))
        billing = merged.get("billing_address") or {}
        if not str(billing.get("name") or "").strip():
            out.append((
                "L'adresse de facturation du client n'a pas pu être établie "
                "(lecture de sa fiche impossible) : rien n'a été créé. "
                "Réessayez dans un instant.",
                "adresse_non_etablie",
            ))
    for amount_key, number_key, label, reason in (
        ("gst_amount", "gst_number", "TPS", "numero_tps_vide"),
        ("qst_amount", "qst_number", "TVQ", "numero_tvq_vide"),
    ):
        if (int(totals.get(amount_key) or 0) > 0
                and not str(merged.get(number_key) or "").strip()):
            out.append((
                f"Le numéro d'inscription {label} du cabinet est vide : une "
                f"facture qui porte la {label} doit l'indiquer. Saisissez-le "
                f"dans {SETTINGS_TAX_NUMBERS_PATH}, puis recréez la facture. "
                "Rien n'a été créé.",
                reason,
            ))
    invoice_day = _calendar_day(merged.get("date"))
    due_day = _calendar_day(merged.get("due_date"))
    if invoice_day and due_day and due_day < invoice_day:
        out.append((
            f"La date d'échéance ({due_day.isoformat()}) précède la date de la "
            f"facture ({invoice_day.isoformat()}). Rien n'a été créé.",
            "echeance_anterieure",
        ))
    return out


def invoice_document_from(data: Optional[dict]) -> dict:
    """The invoice document a creation starts from — the ONE builder.

    Defaults, then from *data* only the keys of ``_CREATE_DATA_KEYS``,
    sanitized. :func:`plan_invoice` judges THIS document, so every caller
    that asks it a question — ``create_invoice``, and the connector's
    preview — must build its document here: a preview assembling its own
    (unfiltered, unsanitized) would judge another invoice than the one the
    write stores, the very drift ``plan_invoice`` exists to prevent. (A
    client name made only of markup, say, is emptied by ``sanitize`` and
    refused as blank — by both, or by neither.) Pure: reads nothing.
    """
    return {
        **_default_doc(),
        **_sanitize_data(
            {k: v for k, v in (data or {}).items() if k in _CREATE_DATA_KEYS}
        ),
    }


def _read_client_strict(client_id: str) -> Optional[dict]:
    """The contact *client_id* names, or ``None`` when none is on file.

    RAISES on a read failure — never the fail-open ``get_partie``, whose
    ``None`` on an outage would read as « this client does not exist » and
    refuse, or worse, as a contact whose address is blank. Through this
    module's own client, so the store an invoice is written to is the store
    its client is checked in.
    """
    from models import partie as partie_model

    snap = db.collection(partie_model.COLLECTION).document(client_id).get()
    return (snap.to_dict() or {}) if snap.exists else None


def plan_invoice(
    dossier_id: str,
    entry_ids: list[str],
    expense_ids: list[str],
    merged: dict,
    *,
    generated: bool,
    require_all_sources: bool,
    adjustment: Optional[dict] = None,
    expected_total: Optional[int] = None,
) -> InvoicePlan:
    """Everything ``create_invoice`` decides before its transaction — ONE
    implementation, so a preview and the write cannot drift.

    *merged* is the invoice document as :func:`invoice_document_from` builds
    it from the caller's data — never one a caller assembles itself, or the
    preview and the write would judge two different documents.
    *generated* — the invoice gets a number of our own sequence
    (``create_invoice`` without ``invoice_number``); ``False`` is the
    historical import. Reads the sources and the client; writes nothing,
    allocates nothing.

    In order, the first refusal wins (its error list is returned as a
    whole), exactly as ``create_invoice`` always refused:

    1. the invoice fields (``_validate``) and an empty selection;
    2. each source is read; one that cannot become a line is SKIPPED with
       its reason — missing or unreadable, already billed (the reason names
       the invoice, so a stale selection points at what billed it),
       another dossier's, and, on the generated path, NON-BILLABLE: time the
       lawyer marked « non facturable » has no amount and must never print
       its hours on a client's invoice (a legacy entry that predates the
       flag carries no key and stays billable). A historical import may
       reproduce such a 0 $ line and keeps it;
    3. with *require_all_sources*, a duplicated id or ANY skipped source
       refuses, naming each offender by reason — without it the skip is
       silent (no caller of the application passes False any more);
    4. no line left; a malformed *adjustment*;
    5. *expected_total* — exact, never a tolerance;
    6. the provision: on the GENERATED path it is forced to 0 — a provision
       is applied later, by a « paiement d'honoraires » drawn on the trust
       account, and deducting it here too would count the same money twice
       (the provision trap of 2026-08-17); a non-zero value supplied there
       is ignored and said so in ``warnings``. On the import path it is kept
       and bounded to ``[0, total]``;
    7. the issuance checks (:func:`issuance_refusals`), the client being
       read STRICTLY — a read that fails refuses, it never passes.

    Warnings: the number's year versus the invoice date's
    (:func:`number_year_warning`, on the generated path — the number will
    be of the current Montréal year).
    """
    from models.time_entry import get_time_entry
    from models.expense import get_expense

    plan = InvoicePlan()
    entry_ids = list(entry_ids or [])
    expense_ids = list(expense_ids or [])

    errors = _validate(merged)
    if not entry_ids and not expense_ids:
        errors.append("Sélectionnez au moins une entrée de temps ou une dépense.")
    if errors:
        return plan.refuse(errors, "validation")

    # Build line items from selected sources. A source that is missing,
    # already invoiced, owned by a different dossier or (generated path)
    # non-billable is SKIPPED — only the sources that actually become line
    # items get flipped to invoiced. Why each was dropped is kept even
    # when require_all_sources is off, so the reason is computed once, in
    # the one place that actually knows it.
    for eid in entry_ids:
        entry = get_time_entry(eid)
        if not entry:
            # get_time_entry swallows a read failure into None, so the model
            # genuinely cannot tell a deleted entry from an unreadable one.
            # Say both rather than assert the wrong one.
            plan.skipped[eid] = "introuvable ou illisible"
            continue
        if entry.get("invoiced"):
            billed_on = entry.get("invoice_id") or ""
            plan.skipped[eid] = (
                f"déjà facturée (facture {billed_on})" if billed_on
                else "déjà facturée"
            )
            continue
        if entry.get("dossier_id") != dossier_id:
            plan.skipped[eid] = "rattachée à un autre dossier"
            continue
        if generated and entry.get("billable") is False:
            plan.skipped[eid] = "non facturable"
            continue
        plan.valid_entry_ids.append(eid)
        plan.expected_etags[eid] = entry.get("etag", "")
        plan.line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "fee",
            "source_id": eid,
            "date": entry.get("date"),
            "description": entry.get("description", ""),
            "hours": entry.get("hours", 0),
            "rate": entry.get("rate", 0),
            "amount": entry.get("amount", 0),
            "taxable": True,
        })

    for eid in expense_ids:
        expense = get_expense(eid)
        if not expense:
            plan.skipped[eid] = "introuvable ou illisible"
            continue
        if expense.get("invoiced"):
            billed_on = expense.get("invoice_id") or ""
            plan.skipped[eid] = (
                f"déjà facturé (facture {billed_on})" if billed_on
                else "déjà facturé"
            )
            continue
        if expense.get("dossier_id") != dossier_id:
            plan.skipped[eid] = "rattaché à un autre dossier"
            continue
        plan.valid_expense_ids.append(eid)
        plan.expected_etags[eid] = expense.get("etag", "")
        plan.line_items.append({
            **_default_line_item(),
            "id": str(uuid.uuid4()),
            "type": "expense",
            "source_id": eid,
            "date": expense.get("date"),
            "description": expense.get("description", ""),
            "hours": None,
            "rate": None,
            "amount": expense.get("amount", 0),
            "taxable": expense.get("taxable", True),
        })

    if require_all_sources:
        # A repeated id appends two identical line items and doubles the fee:
        # the build loops above do not dedupe, and the web form cannot
        # produce a duplicate.
        for label, ids in (
            ("time_entry_ids", entry_ids),
            ("expense_ids", expense_ids),
        ):
            seen: set[str] = set()
            dupes: set[str] = set()
            for source_id in ids:
                if source_id in seen:
                    dupes.add(source_id)
                seen.add(source_id)
            if dupes:
                return plan.refuse([
                    f"`{label}` contient des identifiants en double : "
                    + ", ".join(sorted(dupes))
                    + ". Chaque source ne peut être facturée qu'une fois."
                ], "ids_en_double")
        if plan.skipped:
            grouped: dict[str, list[str]] = {}
            for sid, reason in plan.skipped.items():
                grouped.setdefault(reason, []).append(sid)
            details = " ; ".join(
                f"{reason} : {', '.join(sorted(ids))}"
                for reason, ids in sorted(grouped.items())
            )
            return plan.refuse([
                f"Sources inutilisables (rien n'a été écrit) — {details}."
            ], "sources_inutilisables")

    if not plan.line_items:
        return plan.refuse(["Aucune entrée valide sélectionnée."], "aucune_ligne")

    if adjustment is not None:
        adjustment_item, adjustment_errors = _adjustment_line_item(adjustment)
        if adjustment_errors:
            return plan.refuse(adjustment_errors, "ajustement")
        plan.line_items.append(adjustment_item)

    totals = compute_totals(plan.line_items)
    plan.totals = totals

    if expected_total is not None:
        if not isinstance(expected_total, int) or isinstance(expected_total, bool):
            return plan.refuse(
                ["`expected_total` doit être un entier de cents."], "total_attendu"
            )
        if expected_total != totals["total"]:
            gap = totals["total"] - expected_total
            supplied = len(entry_ids) + len(expense_ids)
            return plan.refuse([
                f"Le total reconstitué ({totals['total']} ¢) ne correspond "
                f"pas au total attendu ({expected_total} ¢) — écart "
                f"{gap:+d} ¢. Calculé : honoraires {totals['subtotal_fees']} ¢, "
                f"déboursés {totals['subtotal_expenses']} ¢, "
                f"TPS {totals['gst_amount']} ¢, TVQ {totals['qst_amount']} ¢, "
                f"sur {len(plan.line_items)} ligne(s) retenue(s) pour {supplied} "
                "source(s) fournie(s). Aucune facture n'a été créée."
            ], "total_attendu")

    retainer_applied = merged.get("retainer_applied", 0)
    if generated:
        if retainer_applied not in (0, None, False):
            plan.warnings.append(
                "La provision indiquée n'a pas été déduite : sur une facture "
                "émise ici, une provision s'impute APRÈS l'envoi, par un "
                "« paiement d'honoraires » tiré du fidéicommis — la déduire "
                "aussi à la création compterait deux fois le même argent."
            )
        plan.retainer_applied = 0
    else:
        # Retainer must stay within [0, total] so amount_due can never go
        # negative.
        if (
            not isinstance(retainer_applied, int)
            or isinstance(retainer_applied, bool)
            or retainer_applied < 0
            or retainer_applied > totals["total"]
        ):
            return plan.refuse([
                "La provision appliquée doit être comprise entre 0 $ et le "
                "total de la facture."
            ], "provision_hors_bornes")
        plan.retainer_applied = retainer_applied

    client_id = str(merged.get("client_id") or "").strip()
    client = None
    if client_id:
        try:
            client = _read_client_strict(client_id)
        except Exception:
            log_unexpected("plan_invoice: client read failed")
            return plan.refuse([
                "Impossible de vérifier le client du dossier (lecture "
                "impossible). Rien n'a été créé : réessayez dans un instant."
            ], "client_illisible")
    refusals = issuance_refusals(merged, totals, generated=generated, client=client)
    if refusals:
        return plan.refuse([m for m, _ in refusals], refusals[0][1])

    if generated:
        warning = number_year_warning(
            merged.get("date"), f"{today_mtl().strftime('%Y')}-F"
        )
        if warning:
            plan.warnings.append(warning)
    return plan


# ── CRUD ─────────────────────────────────────────────────────────────────


def create_invoice(
    dossier_id: str,
    selected_entry_ids: list[str],
    selected_expense_ids: list[str],
    data: dict,
    *,
    invoice_number: Optional[str] = None,
    expected_total: Optional[int] = None,
    require_all_sources: bool = False,
    adjustment: Optional[dict] = None,
) -> tuple[Optional[dict], list[str]]:
    """Create an invoice with line items from selected time entries and expenses.

    Every decision taken before the write — the source selection, the
    refusals, the totals, the issuance checks — is :func:`plan_invoice`'s,
    called with this call's own flags; a refused plan is returned as the
    error list and NOTHING is read beyond it (no counter, no number). The
    invoice document, all line items, and the invoiced=True flips for the
    retained sources are then committed in a single Firestore transaction
    that re-reads each source, so a concurrent invoicing aborts the whole
    creation (no orphan invoices, no double-billing). Returns (invoice,
    errors).

    Without ``invoice_number`` this is the GENERATED path: the invoice this
    application issues today, which :func:`plan_invoice` holds to the
    issuance rules (a client that exists, its billing address, the firm's
    tax numbers, a due date not before the date, no NON-BILLABLE time, and
    NO provision — ``retainer_applied`` is forced to 0). Its number follows
    the Montréal year of today (:func:`number_year_warning` names the case
    where that is not the year of its date).

    The four keyword-only arguments serve the historical import:

    * *invoice_number* — a number carried over from the previous system. The
      year counter is then **never read and never advanced**; without it the
      counter allocates as always. It cannot arrive through *data* (which
      ``merged.update`` clobbers), so ``request.form`` can never forge one.
    * *expected_total* — the grand total printed on the paper invoice. On any
      difference the creation is refused, with the gap, the breakdown and the
      retained-versus-supplied source count. No tolerance: one cent of
      silent drift is how a book of account starts lying.
    * *require_all_sources* — turn the silent skip into a refusal naming every
      offender. This lives in the model and not only in a caller because the
      skip happens in the pre-read: a skipped id never enters
      ``source_refs``, so ``_SourceConflictError`` can never catch it and a
      caller's pre-flight is a TOCTOU snapshot the model may not honour. The
      web form passes it too since lot 3a: a stale selection (an entry
      billed or moved in another tab) is refused by name instead of
      producing a silently shorter invoice.
    * *adjustment* — a named, caller-justified extra line item (see
      ``_adjustment_line_item``) for a legacy total the lines cannot
      reconstruct.

    Ordering is the contract: a refused import consumes nothing and never
    even reads the counter; a refused GENERATED invoice consumes nothing
    either, since its number is allocated inside the same transaction that
    writes the invoice (every read — sources, counter — before any write).

    Only the keys of ``_CREATE_DATA_KEYS`` are taken from *data*; the
    payment state is forced (``brouillon``, nothing paid): an invoice is born
    unpaid, and only the accounting register may say otherwise.
    """
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    merged = invoice_document_from(data)

    plan = plan_invoice(
        dossier_id, selected_entry_ids, selected_expense_ids, merged,
        generated=invoice_number is None,
        require_all_sources=require_all_sources,
        adjustment=adjustment,
        expected_total=expected_total,
    )
    if plan.errors:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason=plan.refusal_reason,
                          dossier_id=dossier_id)
        return None, plan.errors

    now = datetime.now(timezone.utc)
    invoice_id = str(uuid.uuid4())
    line_items = plan.line_items
    valid_entry_ids = plan.valid_entry_ids
    valid_expense_ids = plan.valid_expense_ids
    expected_etags = plan.expected_etags
    totals = plan.totals
    retainer_applied = plan.retainer_applied

    # Resolve the number LAST, so every refusal above happens before the
    # counter is touched. On the imported branch it is never touched at all.
    # Note the name `invoice_number` is NOT rebound — the transaction closure
    # below reads the parameter to decide whether to run the uniqueness read.
    counter_ref = None
    number_prefix = ""
    seed = 0
    if invoice_number is None:
        # The generated number is allocated INSIDE the invoice transaction
        # below. Only the first-of-year seed is read here (see
        # _counter_seed). Any failure aborts the creation — never fall back
        # to a guessed number.
        year = today_mtl().strftime("%Y")
        number_prefix = f"{year}-F"
        counter_ref = _invoice_counter_ref(year)
        try:
            seed = _counter_seed(counter_ref, number_prefix)
        except Exception:
            log_unexpected("create_invoice: invoice number seeding failed")
            return None, [
                "Impossible de générer le numéro de facture. Veuillez réessayer."
            ]
        resolved_number = ""  # decided inside the transaction
    else:
        resolved_number, number_errors = _clean_imported_number(invoice_number)
        if number_errors:
            log_invoice_event("invoice_refused", "", outcome="refused",
                              operation="create", reason="numero_importe_refuse",
                              dossier_id=dossier_id)
            return None, number_errors

    merged.update({
        "id": invoice_id,
        "invoice_number": resolved_number,
        "dossier_id": dossier_id,
        **totals,
        "gst_rate": GST_RATE_BPS,
        "qst_rate": QST_RATE_BPS,
        "retainer_applied": retainer_applied,
        "amount_due": totals["total"] - retainer_applied,
        # Born unpaid, whatever the caller sent — see _CREATE_DATA_KEYS.
        "status": "brouillon",
        "amount_paid": 0,
        "paid_date": None,
    })
    provenance.stamp_create(merged, now)

    # Set default due date if not provided
    if not merged.get("due_date") and merged.get("date"):
        merged["due_date"] = merged["date"] + timedelta(days=30)

    invoice_ref = db.collection(COLLECTION).document(invoice_id)
    source_refs = [
        db.collection(TE_COLLECTION).document(eid) for eid in valid_entry_ids
    ] + [
        db.collection(EXP_COLLECTION).document(eid) for eid in valid_expense_ids
    ]

    transaction = db.transaction()

    @firestore.transactional
    def _txn_create(txn: firestore.Transaction) -> None:
        # All reads must precede all writes in a Firestore transaction.
        # Re-read every retained source so a concurrent invoicing (or
        # deletion / dossier reassignment) aborts this creation entirely.
        for ref in source_refs:
            snap = ref.get(transaction=txn)
            src = snap.to_dict() if snap.exists else None
            if (
                not src
                or src.get("invoiced")
                or src.get("dossier_id") != dossier_id
                or src.get("etag", "") != expected_etags.get(ref.id, "")
            ):
                raise _SourceConflictError(ref.id)

        # Still a READ, so it must precede every write below. Import path
        # only: on the generated path the monotonic counter already
        # guarantees uniqueness, and paying a query on the web form's hot
        # path would buy nothing. Single-field equality — served by the
        # automatic index, no composite.
        if invoice_number is not None:
            clash = list(
                db.collection(COLLECTION)
                .where(filter=FieldFilter("invoice_number", "==", resolved_number))
                .limit(1)
                .stream(transaction=txn)
            )
            if clash:
                raise _DuplicateNumberError(resolved_number)

        # The last READ: the year counter, generated path only. Read after
        # the source checks so a conflict aborts before it is even looked
        # at, and written below WITH the invoice — an abort of this
        # transaction, for any reason, leaves the counter where it was.
        next_seq = 0
        if counter_ref is not None:
            number, next_seq = _next_invoice_number(
                counter_ref.get(transaction=txn), seed, number_prefix
            )
            merged["invoice_number"] = number

        if counter_ref is not None:
            txn.set(counter_ref, {"seq": next_seq, "updated_at": now})
        txn.set(invoice_ref, merged)
        for item in line_items:
            txn.set(
                invoice_ref.collection(LINE_ITEMS_SUB).document(item["id"]),
                item,
            )
        for ref in source_refs:
            txn.update(ref, {
                "invoiced": True,
                "invoice_id": invoice_id,
                **provenance.update_fields(now),
            })

    try:
        _txn_create(transaction)
    except _SourceConflictError:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason="source_modifiee",
                          dossier_id=dossier_id)
        return None, [
            "Certaines entrées sélectionnées ont été modifiées ou facturées entre-temps. Veuillez réessayer."
        ]
    except _DuplicateNumberError as exc:
        log_invoice_event("invoice_refused", "", outcome="refused",
                          operation="create", reason="numero_existant",
                          dossier_id=dossier_id)
        return None, [
            f"Le numéro de facture « {exc} » existe déjà dans Pallas Athéna. "
            "Une facture importée conserve son numéro d'origine : vérifiez "
            "s'il n'a pas déjà été repris."
        ]
    # The uniqueness read lives inside the transaction, so a Firestore blip
    # lands in the generic handler below and REFUSES — deliberately unlike
    # create_dossier's file_number check, which fails open. A duplicated
    # number in a legal accounting register is invisible to the lawyer and
    # unrepairable without renumbering an artifact already sent to a client;
    # a blocked web form on a transient error is merely annoying.
    except Exception:
        log_unexpected("create_invoice: transaction failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event(
        "invoice_created", invoice_id,
        dossier_id=dossier_id,
        source_count=len(valid_entry_ids) + len(valid_expense_ids),
        generated_number=invoice_number is None,
    )

    return merged, []


def get_invoice(invoice_id: str) -> Optional[dict]:
    """Fetch a single invoice by ID (without line items)."""
    try:
        doc = db.collection(COLLECTION).document(invoice_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as exc:
        logger.warning("get_invoice failed for %s: %s", sanitize_log_value(invoice_id), exc)
    return None


def get_invoice_with_items(invoice_id: str) -> tuple[Optional[dict], list[dict]]:
    """Fetch an invoice and all its line items. Returns (invoice, items)."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return None, []

    items: list[dict] = []
    try:
        docs = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        items = [d.to_dict() for d in docs]
        # Sort by date
        items.sort(
            key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
        )
    except Exception as exc:
        logger.warning(
            "get_invoice_with_items: line items load failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )

    return invoice, items


def get_invoice_with_items_strict(
    invoice_id: str,
) -> tuple[Optional[dict], list[dict]]:
    """The invoice and its line items — a read failure PROPAGATES.

    ``(None, [])`` means the store answered « no such invoice » (or the id
    cannot name one) — never « the read failed ». The reader of a caller
    that WRITES on the strength of the answer (``services.note_honoraires``,
    lot 3a): :func:`get_invoice_with_items` swallows a failed line-item
    read into ``[]``, and a note d'honoraires filled from that printed the
    stored totals over empty tables — a client-facing document that looked
    complete. Same sort as the fail-open reader.
    """
    if not isinstance(invoice_id, str) or not invoice_id or "/" in invoice_id:
        return None, []
    invoice_ref = db.collection(COLLECTION).document(invoice_id)
    snap = invoice_ref.get()
    if not snap.exists:
        return None, []
    items = [d.to_dict() or {} for d in invoice_ref.collection(LINE_ITEMS_SUB).stream()]
    items.sort(
        key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
    )
    return snap.to_dict() or {}, items


def line_items_missing(invoice: dict, items: list[dict]) -> bool:
    """True when *invoice* shows a non-zero subtotal yet no line item was
    read — the reads cannot say what the invoice bills. The one rule of the
    void (``void_invoice_report`` refuses on it) and of the note
    d'honoraires (which would print totals over empty tables)."""
    return not items and int(invoice.get("subtotal", 0) or 0) != 0


def list_line_items(invoice_id: str) -> list[dict]:
    """The line items of one invoice, without re-reading the invoice.

    :func:`get_invoice_with_items` costs an extra document read the journal
    export does not need — it already holds every invoice document. Fails
    open to ``[]``: a journal row must still print (its stored totals are
    authoritative) even when the detail is unreadable.
    """
    try:
        docs = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception as exc:
        logger.warning(
            "list_line_items failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )
        return []


def expense_split(invoice: dict, line_items: list[dict]) -> tuple[int, int]:
    """Disbursements split as ``(taxable_cents, non_taxable_cents)``.

    The Barreau's « Journal des honoraires » wants that split, which the
    invoice document does not store — only the ``subtotal_expenses`` total.
    So the STORED total stays authoritative and the items are used solely to
    carve out the non-taxable part: the two columns then always add back to
    ``subtotal_expenses``, and a row whose items are missing or unreadable
    still ties (everything falls under taxable, which is the ``taxable:
    True`` default the tax was computed under) instead of silently
    under-reporting the sheet's own subtotal.

    Fees are excluded — ``create_invoice`` always writes them ``taxable:
    True``, so they are the journal's « Honoraires » column whole.
    """
    non_taxable = sum(
        int(i.get("amount") or 0)
        for i in line_items
        if i.get("type") != "fee" and not i.get("taxable", True)
    )
    total_expenses = int(invoice.get("subtotal_expenses") or 0)
    return total_expenses - non_taxable, non_taxable


def list_invoices(
    status_filter: Optional[str] = None,
    dossier_id: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> list[dict]:
    """Return invoices, optionally filtered."""
    try:
        query = db.collection(COLLECTION)

        if dossier_id:
            query = query.where(filter=FieldFilter("dossier_id", "==", dossier_id))

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side filters
        if status_filter:
            results = [r for r in results if r.get("status") == status_filter]

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


def _page_query(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> "firestore.Query":
    """The filtered, (date DESC, id ASC)-ordered invoice query.

    ONE builder shared by :func:`list_invoices_page` and
    :func:`count_invoices_page`, so the page read and its total can never
    ride different indexes. The date range targets the PRIMARY sort field,
    so it consumes no extra index dimension.
    """
    query = db.collection(COLLECTION)
    if status_filter and status_filter in VALID_STATUSES:
        query = query.where(filter=FieldFilter("status", "==", status_filter))
    if date_from:
        query = query.where(filter=FieldFilter("date", ">=", date_from))
    if date_to:
        query = query.where(filter=FieldFilter("date", "<=", date_to))
    return query.order_by(
        "date", direction=firestore.Query.DESCENDING
    ).order_by("id")


def count_invoices_page(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> Optional[int]:
    """Rows in the filtered set, or None when the count could not be read.

    Runs on the SAME query object as the page read - order_by INCLUDED - so
    the SAME composite index serves both. An aggregation forwards its nested
    query verbatim, and a COUNT adds no aggregated field to trail the index.
    Dropping the order_by makes the backend apply its own implicit ordering,
    a THIRD ordering that FAILS (measured 2026-09-07 against production).

    That shared ordering also keeps the count HONEST: an order_by excludes
    documents missing the key, from the count and the page read alike, so
    the two can never disagree (cross-checked against a bare collection
    COUNT: gap of 0).

    Returns None, NEVER 0, on failure - a rendered "/ 0" is a confident lie.
    """
    try:
        values = _aggregation_values(
            _page_query(status_filter, date_from, date_to).count(alias="n").get()
        )
        n = values.get("n")
        return int(n) if n is not None else None
    except Exception as exc:
        logger.warning("count_invoices_page: aggregation failed: %s", exc)
        return None


def list_invoices_page(
    status_filter: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of invoices plus an opaque cursor for the next page.

    Cursor-mode counterpart of :func:`list_invoices` for the list view:
    reads ~``limit`` documents per page (server-side filters + ``order_by``
    + ``start_after``) instead of streaming the whole collection. The
    legacy :func:`list_invoices` remains the path for exports, summaries
    and dossier-scoped views.

    The status filter is an equality and the optional date range targets
    ``date`` — the same field as the primary sort — so both composite
    indexes serve every filter combination here:
    ``(date DESC, id ASC)`` and ``(status ASC, date DESC, id ASC)``.

    Sort order: ``date DESC, id ASC``. The ``id`` field mirrors the
    document ID and is always set — a stable tiebreaker when several
    invoices share the same date.
    """
    if offset and cursor:
        raise ValueError(
            "cursor et offset s'excluent \u2014 chacun nomme une position"
        )
    try:
        query = _page_query(status_filter, date_from, date_to)
        if offset:
            # An ABSOLUTE page read, for a leap control. Same ordering, so
            # the same index - Firestore emits ONE query
            # (order_by -> offset -> limit). It bills the skipped documents,
            # which is why `page` is clamped before it ever reaches here.
            query = query.offset(offset)

        # decode_cursor yields values in encode order: [date, id].
        # Anything malformed (None or wrong arity) degrades to page 1.
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            query = query.start_after({"date": values[0], "id": values[1]})

        # Fetch one extra row to know whether a next page exists.
        docs = [doc.to_dict() for doc in query.limit(limit + 1).stream()]

        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("date"), last.get("id")])

        return docs, next_cursor
    except Exception as exc:
        # PII-free: log only the exception type, never document contents.
        logger.warning("list_invoices_page failed: %s", type(exc).__name__)
        return [], None


def balance_of(invoice: dict) -> int:
    """The live balance in cents: what was due at issuance, less what came in.

    DERIVED, never stored — a stored balance drifts the moment either side
    of the subtraction is written without it. Note the trap it replaces:
    ``amount_due`` is frozen at issuance and stays non-zero on a fully paid
    invoice, so it has never been a balance despite reading like one.
    """
    return int(invoice.get("amount_due", 0)) - int(invoice.get("amount_paid", 0))


def record_payment(
    invoice_id: str,
    amount_paid: int,
    paid_date: Optional[datetime] = None,
) -> tuple[Optional[dict], list[str]]:
    """Record (or correct) the amount received on an invoice.

    Owns BOTH payment fields and the status flip in one transaction:

    * balance reaches zero → the invoice flips to ``payée``;
    * a CORRECTION that reopens a balance undoes that flip, back to
      ``envoyée``.

    That second half is not a nicety. ``payée`` does now have a manual exit
    (``payée → envoyée``), but ``available_transitions`` closes it the moment
    a payment is RECORDED — a ledger-backed ``payée`` corrects by
    contre-passation, never by hand. So for the ONE status this function can
    set, the undo below remains the only way back, and an erroneous amount
    would still strand the invoice without it. Its caller is
    ``routes/admin_ledger._reduire_paiement`` (a reversed encaissement), not
    a form on the invoice: that form was the second, ledger-blind writer of
    ``amount_paid`` and was removed. The undo is deliberately NARROW: it only
    reverses a flip this function could have made (status ``payée`` with a
    recorded payment that no longer covers the invoice). A ``payée`` set by
    hand, with no payment recorded, is never touched — that status is the
    lawyer's statement, not an inference of ours.

    ``amount_paid = 0`` clears the payment entirely (the full correction).
    Returns ``(updated_invoice, errors)``; fails CLOSED on any read error.
    """
    errors: list[str] = []
    try:
        amount = int(amount_paid)
    except (TypeError, ValueError):
        return None, ["Le montant encaissé doit être un nombre entier de cents."]
    if amount < 0:
        return None, ["Le montant encaissé ne peut pas être négatif."]

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _PaymentRefused("Facture introuvable.")
        invoice = snap.to_dict() or {}

        status = invoice.get("status", "")
        if status == "annulée":
            raise _PaymentRefused(
                "Cette facture est annulée : aucun encaissement ne peut y "
                "être porté."
            )
        if status == "brouillon":
            raise _PaymentRefused(
                "Cette facture est encore un brouillon : envoyez-la avant "
                "d'y porter un encaissement."
            )

        # Cap on what is OWED, not on the invoice total. With a retainer
        # applied, amount_due < total, and capping on `total` let a payment
        # land between the two — producing a NEGATIVE balance with nothing
        # to explain it. An overpayment is a real event, but recording it
        # here would silently corrupt the balance rather than report it.
        due = int(invoice.get("amount_due", 0))
        if amount > due:
            raise _PaymentRefused(
                "Le montant encaissé ne peut pas dépasser le solde dû "
                f"({due / 100:.2f} $). Pour un trop-perçu, portez-le au "
                "fidéicommis plutôt qu'à la facture."
            )

        now = datetime.now(timezone.utc)
        updates: dict = {
            "amount_paid": amount,
            # A cleared payment has no date; keeping a stale one would read
            # as « paid on that day » for an invoice carrying no payment.
            "paid_date": paid_date if amount > 0 else None,
            **provenance.update_fields(now),
        }

        due = int(invoice.get("amount_due", 0))
        had_recorded_payment = int(invoice.get("amount_paid", 0)) > 0
        if amount >= due and status in ("envoyée", "en_retard"):
            updates["status"] = "payée"
        elif amount < due and status == "payée" and had_recorded_payment:
            # Undo OUR OWN flip only — see the docstring.
            updates["status"] = "envoyée"

        transaction.update(ref, updates)
        return {**invoice, **updates}

    try:
        return _apply(db.transaction()), errors
    except _PaymentRefused as refusal:
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice operation failed")
        return None, ["Erreur. Veuillez réessayer."]


class _PaymentRefused(Exception):
    """Refusal raised inside the payment transaction (aborts, never writes)."""


#: Why « annulée » is refused by update_status — shown to the web user AND,
#: later, to the connector. It names the one path that releases the sources.
VOID_BY_STATUS_REFUSED = (
    "Une facture ne s'annule pas par un changement de statut : utilisez "
    "« Annuler » sur la fiche de la facture — c'est la seule voie qui libère "
    "ses entrées de temps et ses déboursés."
)


def update_status(
    invoice_id: str,
    new_status: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[bool, str]:
    """Transition an invoice to a new status. Returns (success, error).

    :func:`update_status_report` for a caller that needs only the verdict —
    the historical signature, kept for the web route and the scripts.
    """
    _doc, errors = update_status_report(
        invoice_id, new_status, expected_etag=expected_etag)
    if errors:
        return False, errors[0]
    return True, ""


def update_status_report(
    invoice_id: str,
    new_status: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Transition an invoice to a new status — ``(invoice, errors)``.

    The invoice returned is the one WRITTEN (its new etag included), so a
    caller that hands the version back — the connector's
    ``update_invoice`` (lot 3b) — never re-reads it after the commit.

    « annulée » is refused FIRST, before anything is read: a bare status
    write left every time entry and disbursement flagged invoiced for good
    (``void_invoice`` then refused « déjà annulée » and ``delete_invoice``
    refused the references still hanging on). Voiding goes through
    :func:`void_invoice`, and only there.

    The read, the checks and the write run in ONE transaction. They used to
    be a read then an unconditional ``update()``, so a concurrent automatic
    flip to « payée » by ``record_payment`` could be overwritten by
    « en retard ». *expected_etag* — the version the caller's page was
    rendered from — is compared INSIDE that transaction
    (``models.concurrency``); ``None`` asserts nothing (a page rendered
    before its form carried one, a script).
    """
    if new_status == "annulée":
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason="annulation_par_statut")
        return None, [VOID_BY_STATUS_REFUSED]

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[str, dict]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _StatusRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()

        current = invoice.get("status", "")
        if new_status not in available_transitions(invoice):
            # Nommer la voie plutôt que de refuser sèchement : la seule raison
            # qu'une facture payée ait de rester fermée est qu'un encaissement
            # l'adosse, et cet encaissement a son propre chemin de correction.
            if current == "payée" and new_status == "envoyée":
                raise _StatusRefused(
                    "Cette facture porte un encaissement inscrit au grand livre. "
                    "Contre-passez l'écriture dans « Administration » — la facture "
                    "rouvrira d'elle-même."
                )
            if new_status == "payée":
                raise _StatusRefused(
                    "Une facture se marque payée par un encaissement inscrit au "
                    "registre d'administration, jamais à la main."
                )
            raise _StatusRefused(
                f"Transition de « {STATUS_LABELS.get(current, current)} » vers "
                f"« {STATUS_LABELS.get(new_status, new_status)} » non permise."
            )

        updates = {
            "status": new_status,
            **provenance.update_fields(datetime.now(timezone.utc)),
        }
        transaction.update(ref, updates)
        return current, {**invoice, **updates}

    try:
        previous, written = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _StatusRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="status", reason=refusal.reason)
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice status update failed")
        return None, ["Erreur. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event("invoice_status_changed", invoice_id,
                      from_status=previous, to_status=new_status)
    return written, []


#: The ceiling of a void's stated reason — refused beyond, never truncated.
VOID_REASON_MAX_LENGTH = 500


def invalid_void_reason(reason: object) -> list[str]:
    """Why a void's *reason* is refused rather than altered — ``[]`` if fine.

    It is stored on the invoice and shown on its sheet; ``sanitize`` would
    truncate it and delete a « < … > » run in silence, so both are refused
    first (the draft texts' rule). ``''`` is fine: the web form states none.
    """
    if not isinstance(reason, str):
        return ["Le motif de l'annulation doit être un texte."]
    if len(reason) > VOID_REASON_MAX_LENGTH:
        return [
            f"Le motif de l'annulation dépasse {VOID_REASON_MAX_LENGTH} "
            f"caractères ({len(reason)}) : il n'est jamais tronqué — "
            "raccourcissez-le."
        ]
    if sanitize(reason, max_length=VOID_REASON_MAX_LENGTH) != reason:
        return [
            "Le motif de l'annulation contient un passage entre chevrons "
            "(< … >) qui serait supprimé à l'enregistrement : retirez les "
            "chevrons."
        ]
    return []


def void_invoice_report(
    invoice_id: str,
    *,
    expected_etag: Optional[str] = None,
    reason: str = "",
) -> tuple[Optional[dict], list[str]]:
    """Void an invoice — status « annulée » — and release its sources.

    *reason* — why it is voided — is stored as ``void_reason`` beside
    ``voided_at``, the instant of the void (lot 3b: the connector's void
    demands one; the web form states none, ``''``). It is judged before
    anything is read (:func:`invalid_void_reason`): over-long or carrying a
    « < … > » run, it is REFUSED, never truncated.

    Returns ``(report, errors)``. The report::

        {"invoice": <the invoice as written>,
         "released_time_entry_ids": [...], "released_expense_ids": [...],
         "foreign_source_ids": [...], "missing_source_ids": [...]}

    ONE Firestore transaction, every read before any write:

    * the invoice (its status, its etag, its ``amount_paid``);
    * its line items, and the ``timeentries``/``expenses`` whose
      ``invoice_id`` still names it — the UNION is released, so a void can
      never leave behind a source ``delete_invoice`` would later find
      stranded;
    * the administration entries and the trust payments imputed on it.

    Refused — nothing written — when the invoice is missing, already
    annulée, payée, not the version *expected_etag* names, or when money
    still stands on it: a recorded ``amount_paid``, a standing
    ``encaissement_facture`` in « Administration », a standing
    ``virement_honoraires`` in « Fidéicommis ». It used to check the status
    only: voiding a partly paid invoice released its hours for re-billing
    while the payment stayed on the voided invoice — the client billed
    twice for the same work. The refusal names WHERE to reverse the
    payment first: the trust register when a trust fee payment is involved
    (it is the source of truth of that pair, and reverses the linked admin
    receipt itself), « Administration » otherwise.

    It NEVER refuses because of a source. A source whose ``invoice_id`` is
    empty is released — rewritten only if it is still flagged invoiced, so
    an already-released row keeps its etag; one that names ANOTHER invoice
    has been billed again since — it is left untouched and reported in
    ``foreign_source_ids``; a source document that no longer exists has
    nothing to release and is reported in ``missing_source_ids``. Refusing
    on either would strand the invoice for good — the victims of the
    read-then-set race on the time-entry form are exactly such invoices,
    and the duplicate invoice the race produced is the one the lawyer needs
    to void. What IS refused is a line-item subcollection that comes back
    EMPTY under a non-zero subtotal: the reads could not tell which sources
    to release, and guessing would be worse than stopping (fail closed —
    as is any read that raises).
    """
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    reason_errors = invalid_void_reason(reason)
    if reason_errors:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason="motif_invalide")
        return None, reason_errors
    reason = reason.strip()

    invoice_ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> dict:
        snap = invoice_ref.get(transaction=transaction)
        if not snap.exists:
            raise _VoidRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}

        status = invoice.get("status", "")
        if status == "annulée":
            raise _VoidRefused("Cette facture est déjà annulée.", "deja_annulee")
        if status == "payée":
            raise _VoidRefused(
                "Impossible d'annuler une facture déjà payée : contre-passez "
                "d'abord l'encaissement qui l'a soldée, ou rouvrez-la si le "
                "statut a été posé à la main.",
                "payee",
            )
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()

        items = [
            d.to_dict() or {}
            for d in invoice_ref.collection(LINE_ITEMS_SUB).stream(
                transaction=transaction
            )
        ]
        admin_rows = [
            d.to_dict() or {}
            for d in db.collection(_ADMIN_TRANSACTIONS)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .stream(transaction=transaction)
        ]
        trust_rows = [
            d.to_dict() or {}
            for d in db.collection(_TRUST_TRANSACTIONS)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .stream(transaction=transaction)
        ]

        # Money first, the trust register first of all: it is the source of
        # truth of a fee payment and of the admin receipt it minted.
        standing_admin = [r for r in admin_rows if _standing_admin_encaissement(r)]
        trust_linked = any(_standing_trust_fee_payment(r) for r in trust_rows) or any(
            r.get("trust_transaction_id") for r in standing_admin
        )
        if trust_linked:
            raise _VoidRefused(
                "Cette facture est acquittée, en tout ou en partie, par un "
                "paiement d'honoraires tiré du fidéicommis : contre-passez ce "
                "paiement dans « Fidéicommis » (la recette d'administration "
                "liée suit d'elle-même), puis annulez la facture. Rien n'a été "
                "annulé.",
                "paiement_fideicommis",
            )
        amount_paid = int(invoice.get("amount_paid", 0) or 0)
        if amount_paid > 0:
            raise _VoidRefused(
                f"Cette facture porte un encaissement de "
                f"{format_cents_fr(amount_paid)} : contre-passez l'écriture "
                "dans « Administration », puis annulez la facture — sinon ses "
                "heures redeviendraient facturables alors que le paiement "
                "resterait sur une facture annulée. Rien n'a été annulé.",
                "paiement_inscrit",
            )
        if standing_admin:
            raise _VoidRefused(
                "Une écriture d'encaissement du registre d'administration est "
                "encore imputée sur cette facture : contre-passez-la dans "
                "« Administration », puis annulez la facture. Rien n'a été "
                "annulé.",
                "encaissement_administration",
            )

        if line_items_missing(invoice, items):
            raise _VoidRefused(
                "Les lignes de cette facture sont introuvables alors que son "
                "sous-total n'est pas nul : impossible de savoir quelles "
                "entrées libérer. Rien n'a été annulé.",
                "lignes_illisibles",
            )

        # The sources: every one a line item names, plus every one that
        # still names this invoice. {(collection, id): data | None}.
        sources: dict[tuple[str, str], Optional[dict]] = {}
        for col in (TE_COLLECTION, EXP_COLLECTION):
            for d in (
                db.collection(col)
                .where(filter=FieldFilter("invoice_id", "==", invoice_id))
                .stream(transaction=transaction)
            ):
                sources[(col, d.id)] = d.to_dict() or {}
        for item in items:
            source_id = item.get("source_id", "")
            if not source_id:
                continue  # the named adjustment line — nothing to release
            col = TE_COLLECTION if item.get("type") == "fee" else EXP_COLLECTION
            if (col, source_id) in sources:
                continue
            source_snap = (
                db.collection(col).document(source_id).get(transaction=transaction)
            )
            sources[(col, source_id)] = (
                (source_snap.to_dict() or {}) if source_snap.exists else None
            )

        now = datetime.now(timezone.utc)
        report: dict = {
            "released_time_entry_ids": [],
            "released_expense_ids": [],
            "foreign_source_ids": [],
            "missing_source_ids": [],
        }
        releases = []
        for (col, source_id), data in sources.items():
            if data is None:
                report["missing_source_ids"].append(source_id)
                continue
            points_at = data.get("invoice_id") or ""
            if points_at and points_at != invoice_id:
                report["foreign_source_ids"].append(source_id)
                continue
            # Already released (the race victim: invoiced False, no
            # invoice_id): reported as released, but NOT rewritten. A
            # « harmless » write would still regenerate its etag and stamp
            # updated_via on a row nothing changed — and an edit form open on
            # that entry would then refuse its next save as stale.
            if data.get("invoiced") or points_at:
                releases.append((col, source_id))
            key = (
                "released_time_entry_ids" if col == TE_COLLECTION
                else "released_expense_ids"
            )
            report[key].append(source_id)

        # ── writes ──
        for col, source_id in releases:
            transaction.update(db.collection(col).document(source_id), {
                "invoiced": False,
                "invoice_id": None,
                **provenance.update_fields(now),
            })
        stamp = {
            "status": "annulée",
            "void_reason": reason,
            "voided_at": now,
            **provenance.update_fields(now),
        }
        transaction.update(invoice_ref, stamp)
        for key in report:
            report[key].sort()
        report["invoice"] = {**invoice, **stamp}
        return report

    try:
        report = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR]
    except _VoidRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="void", reason=refusal.reason)
        return None, [str(refusal)]
    except Exception:
        log_unexpected("invoice void failed")
        return None, ["Erreur lors de l'annulation. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, invoice_id)
    log_invoice_event(
        "invoice_voided", invoice_id,
        dossier_id=report["invoice"].get("dossier_id", ""),
        released_count=(
            len(report["released_time_entry_ids"])
            + len(report["released_expense_ids"])
        ),
        foreign_count=len(report["foreign_source_ids"]),
        missing_count=len(report["missing_source_ids"]),
    )
    return report, []


def void_invoice(
    invoice_id: str, *, expected_etag: Optional[str] = None,
) -> tuple[bool, str]:
    """:func:`void_invoice_report` for a caller that needs only the verdict.

    Returns ``(success, error)`` — the historical signature, kept for its
    callers.
    """
    report, errors = void_invoice_report(invoice_id, expected_etag=expected_etag)
    if errors:
        return False, errors[0]
    return True, ""


# ── Draft corrections ────────────────────────────────────────────────────

#: The fields a brouillon may correct — and nothing else. No money figure,
#: no line item, no client, no number: those are what the invoice IS, and
#: changing one means voiding it and issuing a new one.
DRAFT_FIELDS = ("notes", "payment_terms", "due_date")
#: The connector's import caps (``mcp/handlers._import_invoice_impl``), so a
#: brouillon correctable on one surface is correctable on the other.
DRAFT_NOTES_MAX_LENGTH = 1500
DRAFT_PAYMENT_TERMS_MAX_LENGTH = 500


def draft_edit_refusal(invoice: dict) -> str:
    """'' when *invoice* is a brouillon, else why it cannot be corrected.

    The one sentence, shown by the web form before it renders and by
    :func:`update_invoice_draft` inside its transaction.
    """
    status = invoice.get("status", "")
    if status == "brouillon":
        return ""
    if status == "annulée":
        return "Cette facture est annulée : elle ne se modifie plus."
    label = STATUS_LABELS.get(status, status)
    if status == "payée":
        return (
            f"Seul un brouillon se modifie : cette facture est « {label} »."
        )
    return (
        f"Seul un brouillon se modifie : cette facture est « {label} ». Une "
        "facture émise ne se corrige qu'en l'annulant — possible tant "
        "qu'aucun paiement n'y est inscrit —, puis en en émettant une "
        "nouvelle."
    )


class _DraftInvalid(Exception):
    """A changed value the draft refuses (raised in the transaction)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _draft_text_errors(
    text: str, *, label: str, cap: int, required: bool,
) -> list[str]:
    """Why a CHANGED draft text is refused rather than altered — [] if fine.

    ``sanitize`` truncates and deletes angle-bracket runs in silence; on a
    document the client receives, a note that loses « < b et b > » with a
    success message is worse than a refusal. So the length is checked first,
    and the value must come out of ``sanitize`` unchanged.
    """
    if required and not text:
        return [f"{label} ne peuvent pas être vides."]
    if len(text) > cap:
        return [
            f"{label} dépassent {cap} caractères ({len(text)}) : elles ne "
            "sont jamais tronquées — raccourcissez-les."
        ]
    if sanitize(text, max_length=cap) != text:
        return [
            f"{label} contiennent un passage entre chevrons (< … >) qui serait "
            "supprimé à l'enregistrement : retirez les chevrons."
        ]
    return []


def _parse_draft_due_date(value: object) -> tuple[Optional[datetime], list[str]]:
    """A due date as the date-only convention stores it: midnight UTC."""
    if isinstance(value, datetime):
        day = _calendar_day(value)
    elif isinstance(value, date):
        day = value
    elif isinstance(value, str) and value.strip():
        try:
            day = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None, ["La date d'échéance est invalide (AAAA-MM-JJ attendu)."]
    else:
        return None, ["La date d'échéance est requise."]
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), []


# {field: (French label, cap, required)} — the two draft texts.
_DRAFT_TEXT_RULES = {
    "notes": ("Les notes", DRAFT_NOTES_MAX_LENGTH, False),
    "payment_terms": (
        "Les conditions de paiement", DRAFT_PAYMENT_TERMS_MAX_LENGTH, True),
}


def _draft_text(value: object) -> str:
    """A draft text as it is COMPARED and written: LF line breaks, stripped.

    A browser submits every line break of a ``<textarea>`` as CRLF (HTML
    form submission normalizes them so), while the connector's import and
    every script store LF. Compared raw, a multi-line note read back
    unchanged from the page differed from the stored one on EVERY save:
    it was rewritten, reported among the changed fields — and, being
    « changed », re-judged against the cap with each break counted twice,
    so a 1 499-character imported note made an unrelated due-date
    correction REFUSED. The same text under two line-ending conventions is
    one text. ``None`` (a legacy key never set) reads as empty.
    """
    text = "" if value is None else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def update_invoice_draft(
    invoice_id: str,
    changes: dict,
    *,
    expected_etag: Optional[str],
    refresh_billing_address: bool = False,
) -> tuple[Optional[dict], list[str], list[str]]:
    """Correct a BROUILLON — ``(invoice, errors, changed_fields)``.

    There was no way to fix a draft at all: a typo in its notes, a wrong due
    date, a client who moved before the invoice left — each could only be
    « fixed » by voiding the draft, which retires its number for ever.

    *changes* holds any of :data:`DRAFT_FIELDS`, by PRESENCE (an absent key
    leaves the field alone):

    * ``notes`` — at most :data:`DRAFT_NOTES_MAX_LENGTH` characters; ``''``
      clears them;
    * ``payment_terms`` — at most :data:`DRAFT_PAYMENT_TERMS_MAX_LENGTH`;
      ``''`` is refused (they print on the invoice);
    * ``due_date`` — a ``datetime``/``date`` or ``'YYYY-MM-DD'``, never
      before the invoice date.

    Text is stripped and its line breaks read as LF (a browser posts a
    textarea's as CRLF — :func:`_draft_text`); a CHANGED value that is
    over-long, or that ``sanitize`` would alter, is REFUSED — never
    truncated or stripped. A value EQUAL to the stored one (under that one
    line-ending convention) is not a change and is not re-judged: the web
    form resubmits every field, and a brouillon written before these caps
    (web notes went to 2 000 characters) must stay correctable in its other
    fields. Any other key is refused by name. With
    *refresh_billing_address*, the frozen billing address is re-snapshotted
    from the invoice's client as it is on file NOW (``billing_address_from``,
    the one builder) — read inside the transaction; a client that does not
    resolve, or a read that fails, refuses: an address is never blanked.

    ONE Firestore transaction, every read before the write:

    1. the invoice (missing → refused); it must still be a brouillon;
    2. the client, when refreshing;
    3. the fields that actually CHANGE are computed, and only they are
       judged (the texts; the due date against the invoice date). When
       none changes, nothing is written and the stored invoice is returned
       with ``changed_fields == []``. This comes BEFORE the etag check on
       purpose: a replay of a save that already succeeded (a double
       submit, a retried call) is a no-op, not a false conflict;
    4. *expected_etag* — the version the caller read — must be the stored
       one (``None`` asserts nothing: a page rendered before its form
       carried an etag). Required as a KEYWORD: every caller states what it
       read, even when that is « nothing »;
    5. a PARTIAL ``update()`` of the changed keys plus the provenance stamp.
       Never the merged full-document ``set()``: no money figure, no line
       item, no status is even reachable from here.

    The third member of the return deviates from the house ``(doc,
    errors)`` convention on purpose (the ``set_*_phase`` precedent): a
    caller must tell « applied » from « already so » without re-deriving it.
    """
    changes = dict(changes or {})
    unknown = sorted(k for k in changes if k not in DRAFT_FIELDS)
    if unknown:
        return None, [
            "Champ non modifiable sur un brouillon : " + ", ".join(unknown)
            + ". Seuls les notes, les conditions de paiement, la date "
            "d'échéance et l'adresse de facturation se corrigent."
        ], []
    if not changes and not refresh_billing_address:
        return None, ["Aucune modification demandée."], []

    wanted: dict = {}
    errors: list[str] = []
    for key, (label, _cap, _required) in _DRAFT_TEXT_RULES.items():
        if key not in changes:
            continue
        if not isinstance(changes[key], str):
            errors.append(f"{label} : une chaîne de caractères est attendue.")
        else:
            wanted[key] = _draft_text(changes[key])
    if "due_date" in changes:
        due, errs = _parse_draft_due_date(changes["due_date"])
        errors += errs
        wanted["due_date"] = due
    if errors:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="validation")
        return None, errors, []

    from models import partie as partie_model

    ref = db.collection(COLLECTION).document(invoice_id)

    @firestore.transactional
    def _apply(transaction) -> tuple[dict, list[str]]:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise _DraftRefused("Facture introuvable.", "introuvable")
        invoice = snap.to_dict() or {}
        refusal = draft_edit_refusal(invoice)
        if refusal:
            raise _DraftRefused(refusal, "pas_un_brouillon")

        target = dict(wanted)
        if refresh_billing_address:
            client_id = str(invoice.get("client_id") or "").strip()
            if not client_id:
                raise _DraftRefused(
                    "Cette facture n'a pas de client : aucune adresse de "
                    "facturation à rafraîchir.",
                    "aucun_client",
                )
            client_snap = (
                db.collection(partie_model.COLLECTION).document(client_id)
                .get(transaction=transaction)
            )
            if not client_snap.exists:
                raise _DraftRefused(
                    "Le client de la facture est introuvable : l'adresse de "
                    "facturation ne peut pas être rafraîchie. Rien n'a été "
                    "enregistré.",
                    "client_introuvable",
                )
            target["billing_address"] = billing_address_from(
                client_snap.to_dict() or {}
            )

        # A text is compared under ONE line-ending convention (_draft_text):
        # the page's CRLF and the store's LF are the same text, never a
        # change — otherwise every multi-line note would be rewritten and
        # re-judged on a save that did not touch it.
        changed = sorted(
            k for k, v in target.items()
            if (_draft_text(invoice.get(k)) if k in _DRAFT_TEXT_RULES
                else invoice.get(k)) != v
        )
        invalid: list[str] = []
        for key in changed:
            if key in _DRAFT_TEXT_RULES:
                label, cap, required = _DRAFT_TEXT_RULES[key]
                invalid += _draft_text_errors(
                    target[key], label=label, cap=cap, required=required)
        if "due_date" in changed:
            invoice_day = _calendar_day(invoice.get("date"))
            due_day = _calendar_day(target["due_date"])
            if invoice_day and due_day < invoice_day:
                invalid.append(
                    f"La date d'échéance ({due_day.isoformat()}) précède la "
                    f"date de la facture ({invoice_day.isoformat()})."
                )
        if invalid:
            raise _DraftInvalid(invalid)
        if not changed:
            return invoice, []
        if not concurrency.matches(invoice, expected_etag):
            raise concurrency.StaleWrite()
        updates = {k: target[k] for k in changed}
        updates.update(provenance.update_fields(datetime.now(timezone.utc)))
        transaction.update(ref, updates)
        return {**invoice, **updates}, changed

    try:
        doc, changed = _apply(db.transaction())
    except concurrency.StaleWrite:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="stale_etag")
        return None, [concurrency.STALE_ETAG_ERROR], []
    except _DraftInvalid as invalid:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason="validation")
        return None, list(invalid.errors), []
    except _DraftRefused as refusal:
        log_invoice_event("invoice_refused", invoice_id, outcome="refused",
                          operation="draft", reason=refusal.reason)
        return None, [str(refusal)], []
    except Exception:
        log_unexpected("invoice draft update failed")
        return None, [
            "Erreur lors de l'enregistrement du brouillon. Rien n'a été "
            "enregistré : réessayez."
        ], []
    if changed:
        provenance.note_commit(COLLECTION, invoice_id)
        log_invoice_event(
            "invoice_draft_updated", invoice_id,
            fields_changed=changed,
            billing_refreshed="billing_address" in changed,
        )
    return doc, [], changed


def delete_invoice(invoice_id: str) -> tuple[bool, str]:
    """Delete a cancelled invoice and its line items. Returns (success, error)."""
    from models.time_entry import COLLECTION as TE_COLLECTION
    from models.expense import COLLECTION as EXP_COLLECTION

    invoice = get_invoice(invoice_id)
    if not invoice:
        return False, "Facture introuvable."

    if invoice.get("status") != "annulée":
        return False, "Seule une facture annulée peut être supprimée."

    # Guard: refuse deletion while any source still references this invoice.
    # void_invoice should have released them; this protects the stranded case.
    # Invoice number reuse is prevented by the monotonic counter
    # (counters/invoices-{year}), so hard deletion never frees a number.
    try:
        stranded = list(
            db.collection(TE_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        ) or list(
            db.collection(EXP_COLLECTION)
            .where(filter=FieldFilter("invoice_id", "==", invoice_id))
            .limit(1)
            .stream()
        )
        if stranded:
            return False, (
                "Des entrées de temps ou des dépenses référencent encore cette "
                "facture. Elles doivent être libérées avant la suppression."
            )
    except Exception as exc:
        logger.error(
            "delete_invoice: reference check failed for %s: %s",
            sanitize_log_value(invoice_id), exc,
        )
        return False, "Erreur lors de la vérification des références. Veuillez réessayer."

    try:
        # Delete all line items in the subcollection
        items_ref = (
            db.collection(COLLECTION)
            .document(invoice_id)
            .collection(LINE_ITEMS_SUB)
            .stream()
        )
        for item_doc in items_ref:
            item_doc.reference.delete()

        # Delete the invoice document
        db.collection(COLLECTION).document(invoice_id).delete()
        return True, ""
    except Exception:
        log_unexpected("invoice delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


# Shared implementation lives in models/__init__.py; aliased so this module's
# helpers (and their tests) keep a stable local name.
_aggregation_values = aggregation_values


def get_outstanding_total() -> int:
    """Le SOLDE total (cents) encore dû sur les factures émises.

    Σ ``balance_of`` — ``amount_due − amount_paid`` — sur les statuts
    « envoyée » et « en retard ». Somme PYTHON sur une lecture bornée, jamais
    une agrégation Firestore, et le choix est délibéré :

    * ``amount_due`` est FIGÉ à l'émission et reste à pleine valeur sur une
      facture réglée ; le sommer surestimait les créances de tout ce qui avait
      été encaissé — invisible tant qu'aucun paiement n'était inscrit ;
    * une agrégation ne sait pas soustraire deux champs. Il en faudrait deux,
      donc un index ``(status, amount_due, amount_paid)`` qui n'existe pas, et
      dont l'absence échoue en SILENCE : la fonction avale l'exception et rend
      0, indiscernable de « rien n'est dû » (l'incident de juin 2026).

    Le dépôt vote déjà pour la somme Python sur cette collection —
    ``routes/admin_ledger._factures_impayees``,
    ``routes/trust._factures_emises``, ``routes/invoices._journal_rows`` — et
    ``models/trust`` en a fait sa doctrine. Le volume se compte en dizaines.

    Rend 0 en cas d'échec (dégradation gracieuse de la tuile du tableau de
    bord), la posture d'origine.
    """
    try:
        total = 0
        for statut in _ISSUED_STATUSES:
            for inv in list_invoices(status_filter=statut):
                total += balance_of(inv)
        return total
    except Exception as exc:
        logger.warning("get_outstanding_total: failed: %s", exc)
        return 0


def get_invoice_summary(dossier_id: str) -> dict:
    """Return invoice summary for a dossier."""
    invoices = list_invoices(dossier_id=dossier_id)
    total_invoiced = 0
    total_paid = 0
    count = 0

    for inv in invoices:
        if inv.get("status") == "annulée":
            continue
        count += 1
        total_invoiced += inv.get("total", 0)
        if inv.get("status") == "payée":
            total_paid += inv.get("total", 0)

    return {
        "count": count,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "total_outstanding": total_invoiced - total_paid,
    }
