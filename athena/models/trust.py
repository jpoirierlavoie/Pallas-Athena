"""Trust accounting (« comptabilité en fidéicommis ») — Phase K.

Two registers built on ONE collection (``trust_transactions``): the *journal
de caisse* (unfiltered, chronological, running balance) and the *carte-client*
(the same rows filtered to one ``(dossier_id, client_id)`` couple). See
``SPEC_PHASE_K_FIDEICOMMIS.md``.

Binding divergences from the house patterns (spec §2), all deliberate:

* **No ``update_*`` / ``delete_*``.** An accounting register is append-only and
  corrected only by reversal. The one controlled mutation is the two-step
  lifecycle (``en_circulation`` → ``compensée`` | ``annulée``); exactly three
  fields are write-once mutable (``status``, ``cleared_date``,
  ``reconciliation_id``).
* **The overdraft control lives inside the Firestore transaction**, on the same
  read-set as the write (§5). It is the module's most serious guarantee.
* **The module fails CLOSED.** Any read failure during balance verification
  aborts the write; list views surface an error state, never a silently empty
  register.

The first section is the **pure**, Firestore-free layer (spec §6.1): the balance
arithmetic, the disbursement control, the reconciliation variance, and the
Barreau-column projection. It carries the test suite (``tests/test_trust.py``).
The Firestore data-access functions live in the second section and are the only
part that touches ``db``.
"""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from models import db
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils.logging_setup import log_trust_event, log_unexpected, sanitize_log_value
from utils.tracing_setup import span

logger = logging.getLogger(__name__)

# ── Collection + counter names ─────────────────────────────────────────────
ACCOUNTS_COLLECTION = "trust_accounts"
TRANSACTIONS_COLLECTION = "trust_transactions"
RECONCILIATIONS_COLLECTION = "trust_reconciliations"
COUNTERS_COLLECTION = "counters"


def _counter_id(account_id: str) -> str:
    """Firestore doc id of an account's monotonic sequence counter (§3.5)."""
    return f"trust-{account_id}"


# ── Closed vocabularies (spec §3) ──────────────────────────────────────────
VALID_ACCOUNT_TYPES = ("général", "spécial")
VALID_ACCOUNT_STATUSES = ("actif", "fermé")

VALID_DIRECTIONS = ("recette", "déboursé")
VALID_TX_STATUSES = ("en_circulation", "compensée", "annulée")

VALID_PURPOSES = (
    "avance_honoraires",
    "dépôt_client",
    "règlement",
    "virement_honoraires",
    "remise_client",
    "déboursé_tiers",
    "virement_inter_dossiers",
    "intérêts",
    "frais_bancaires",
    "correction",
    "autre",
)
VALID_METHODS = ("chèque", "virement", "traite", "dépôt_direct", "comptant")

# Purposes allowed on an entry that has NO dossier/client beneficiary
# (bank interest, bank fees, and reversals of those). Everything else must
# name a (dossier, client) couple. See §5 guard "objet_sans_dossier_invalide".
NO_DOSSIER_PURPOSES = ("intérêts", "frais_bancaires", "correction")

# Reserved for entries minted by ``reverse_transaction`` only — rejected at the
# create route/path so reversal stays the sole way to produce one (§3.2, §5.2).
REVERSAL_PURPOSE = "correction"

VALID_RECONCILIATION_STATUSES = ("brouillon", "complétée")

# ── French display labels ──────────────────────────────────────────────────
DIRECTION_LABELS = {"recette": "Recette", "déboursé": "Déboursé"}
TX_STATUS_LABELS = {
    "en_circulation": "En circulation",
    "compensée": "Compensée",
    "annulée": "Annulée",
}
PURPOSE_LABELS = {
    "avance_honoraires": "Avance d'honoraires",
    "dépôt_client": "Dépôt du client",
    "règlement": "Règlement",
    "virement_honoraires": "Virement d'honoraires",
    "remise_client": "Remise au client",
    "déboursé_tiers": "Déboursé à un tiers",
    "virement_inter_dossiers": "Virement inter-dossiers",
    "intérêts": "Intérêts",
    "frais_bancaires": "Frais bancaires",
    "correction": "Correction",
    "autre": "Autre",
}
METHOD_LABELS = {
    "chèque": "Chèque",
    "virement": "Virement",
    "traite": "Traite",
    "dépôt_direct": "Dépôt direct",
    "comptant": "Comptant",
}
ACCOUNT_TYPE_LABELS = {"général": "Général", "spécial": "Spécial"}
ACCOUNT_STATUS_LABELS = {"actif": "Actif", "fermé": "Fermé"}
RECONCILIATION_STATUS_LABELS = {
    "brouillon": "Brouillon",
    "complétée": "Complétée",
}

# ── Barreau register columns, normative order (spec §8) ─────────────────────
# Column 7 « Recette / Crédit » is split into TWO per-direction columns
# (« Recette » for recettes, « Crédit » for déboursés) — the confirmed Barreau
# sheet shape (user decision 2026-07-16), a deliberate divergence from the
# single-signed column the written spec §8 assumed. ``to_barreau_row`` is the
# single mapping the HTML table, CSV and PDF all consume.
BARREAU_COLUMNS = (
    ("date", "Date"),
    ("n_ref", "N/Réf"),
    ("counterparty", "Somme reçue de / Bénéficiaire du débours"),
    ("client", "Client pour qui la somme est reçue ou le débours est effectué"),
    ("objet", "Objet de la recette ou du débours"),
    ("mode", "Mode du retrait"),
    ("recette", "Recette"),
    ("credit", "Crédit"),
    ("solde", "Solde"),
)

# Keys whose values are integer cents (exports divide by 100 for display).
BARREAU_CENTS_KEYS = ("recette", "credit", "solde")


# ═══════════════════════════════════════════════════════════════════════════
# Pure functions (spec §6.1) — no Firestore, no Flask, no now(). These carry
# the test suite. Mirror invoice.compute_totals: module-level, importable
# without the client.
# ═══════════════════════════════════════════════════════════════════════════


def compute_deltas(direction: str, amount: int, status: str) -> dict:
    """Per-entry contribution of one entry to each of the three balances (§4.2).

    Returns ``{"book": int, "cleared": int, "bank": int}`` — the signed amount
    this single entry, in the given ``direction``/``status``, contributes.
    ``amount`` is always positive; ``direction`` carries the sign.

    * **book** — ``+amount`` for a recette, ``-amount`` for a déboursé, in
      **every** status **including ``annulée``**. An annulée entry nets to zero
      only *together with its reversal*, and the register is chronological, so
      both must count (§4.2). This is the value shown as « Solde ».
    * **cleared** — the control balance (never shown as « Solde »). Receipts
      count **only once ``compensée``**; disbursements count while
      ``compensée`` **or** ``en_circulation`` (a written cheque commits the
      funds immediately); ``annulée`` contributes nothing.
    * **bank** — the reconciliation anchor: **only ``compensée``** entries,
      ``+amount`` recette / ``-amount`` déboursé.

    This is the atom of every operation (spec §4.4):
    a *create* applies ``compute_deltas(dir, amt, "en_circulation")``; a status
    change (*clear* / *annul*) applies the difference of the new and old
    contributions; a *reversal* is simply a new entry whose contribution is
    ``compute_deltas`` of its own state. Summed in sequence order it yields the
    running book balance (``recompute_running_balances``).
    """
    signed = amount if direction == "recette" else -amount

    book = signed  # every status, including annulée

    if status == "annulée":
        cleared = 0
    elif direction == "recette":
        cleared = amount if status == "compensée" else 0
    else:  # déboursé — committed while compensée OR en_circulation
        cleared = -amount

    bank = signed if status == "compensée" else 0

    return {"book": book, "cleared": cleared, "bank": bank}


def check_disbursement_allowed(cleared_balance: int, amount: int) -> tuple[bool, str]:
    """The overdraft control (spec §4.3): a déboursé may only draw on cleared
    funds. Allowed iff ``cleared_balance - amount >= 0`` (exactly zero is
    legal). Returns ``(ok, reason)`` — ``reason`` is a machine-stable abort
    string when refused, ``""`` when allowed.

    Confirmed as REQUIRED (user decision 2026-07-16): keep this control; do not
    relax to a book-balance-only check.
    """
    if cleared_balance - amount < 0:
        return False, "solde_compensé_insuffisant"
    return True, ""


def reconciliation_variance(
    statement_balance: int,
    book_balance: int,
    outstanding_cheques: int,
    deposits_in_transit: int,
) -> int:
    """Bank-reconciliation variance in cents (spec §3.3). MUST be 0 to complete.

    ``variance = statement_balance + deposits_in_transit
                 - outstanding_cheques - book_balance``
    """
    return statement_balance + deposits_in_transit - outstanding_cheques - book_balance


def to_barreau_row(tx: dict, view: str) -> dict:
    """Project a trust transaction onto the Barreau register columns (§8).

    ``view`` ∈ ``{"journal", "carte"}`` selects which frozen balance lands in
    « Solde » — ``balance_after_account`` for the journal, ``balance_after_client``
    for the carte-client. Column 7 is split into « Recette » (recettes) and
    « Crédit » (déboursés); the inapplicable amount column is ``None`` so it
    renders blank rather than ``0,00 $``. An ``annulée`` row appends
    « (annulée) » to « Objet » (§8.3). Returns a dict keyed by ``BARREAU_COLUMNS``
    keys — the single source for the HTML table, CSV and PDF.

    The « Recette »/« Crédit » amounts and « Solde » are returned as integer
    cents (exports divide by 100); ``date`` is returned as its raw stored value
    for the export layer to format.
    """
    direction = tx.get("direction", "")
    amount = tx.get("amount", 0)
    status = tx.get("status", "")

    objet = PURPOSE_LABELS.get(tx.get("purpose", ""), tx.get("purpose", ""))
    if status == "annulée":
        objet = f"{objet} (annulée)"

    solde_key = "balance_after_account" if view == "journal" else "balance_after_client"

    return {
        "date": tx.get("date"),
        "n_ref": tx.get("dossier_file_number", ""),
        "counterparty": tx.get("counterparty", ""),
        "client": tx.get("client_name", ""),
        "objet": objet,
        "mode": METHOD_LABELS.get(tx.get("method", ""), tx.get("method", "")),
        "recette": amount if direction == "recette" else None,
        "credit": amount if direction == "déboursé" else None,
        "solde": tx.get(solde_key, 0),
    }


def recompute_running_balances(txs: list[dict], view: str) -> list[int]:
    """Recompute the running **book** balance for each row from scratch, in the
    order given — verification only (spec §13); never used to render.

    Sums ``compute_deltas(...)["book"]`` down ``txs`` (which the caller must
    pass in ``sequence`` order — the whole account for ``view="journal"``, one
    ``(dossier, client)`` couple for ``view="carte"``). Returns the running
    book balance *after* each row, in input order, for comparison against the
    frozen ``balance_after_account`` / ``balance_after_client`` on the rows.
    """
    running = 0
    out: list[int] = []
    for tx in txs:
        running += compute_deltas(
            tx.get("direction", ""), tx.get("amount", 0), tx.get("status", "")
        )["book"]
        out.append(running)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Firestore data-access layer (spec §5–§7). Append-only register: no update or
# delete of a transaction. The overdraft control and backdating guard live
# INSIDE the transaction; every read failure aborts (fails CLOSED).
# ═══════════════════════════════════════════════════════════════════════════

from datetime import timedelta  # noqa: E402  (kept beside its only users)

DOSSIERS_COLLECTION = "dossiers"
INVOICES_COLLECTION = "invoices"

# Statuses that count an invoice as issued (fee transfer target).
_ISSUED_INVOICE_STATUSES = ("envoyée", "en_retard")


class _TxnAbort(Exception):
    """Raised inside a trust transaction to abort with a machine-stable reason
    (mirrors invoice._SourceConflictError). ``value`` carries an optional int
    (e.g. a variance) for the caller's log, never for the user message."""

    def __init__(self, reason: str, value: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.value = value


# Machine-stable abort reason → French user message.
_ABORT_MESSAGES = {
    "compte_introuvable": "Compte en fidéicommis introuvable.",
    "compte_fermé": "Ce compte est fermé.",
    "montant_invalide": "Le montant doit être un nombre entier de cents positif.",
    "direction_invalide": "Le sens de l'opération est invalide.",
    "objet_invalide": "L'objet de l'opération est invalide.",
    "mode_invalide": "Le mode est invalide.",
    "date_requise": "La date de l'opération est requise.",
    "contrepartie_requise": "La contrepartie (« Somme reçue de / Bénéficiaire ») est requise.",
    "bénéficiaire_incohérent": "Indiquez à la fois le dossier et le client, ou aucun des deux.",
    "dossier_introuvable": "Dossier introuvable.",
    "client_hors_dossier": "Le client sélectionné n'est pas un client de ce dossier.",
    "objet_sans_dossier_invalide": (
        "Sans dossier, seuls les intérêts, frais bancaires et corrections sont permis."
    ),
    "antidatage_refusé": (
        "La date ne peut être antérieure à la dernière écriture du compte. "
        "Corrigez plutôt par une contre-passation datée d'aujourd'hui."
    ),
    "virement_direction": "Un virement d'honoraires doit être un déboursé.",
    "facture_introuvable": "Facture introuvable.",
    "facture_non_émise": "La facture doit être émise (envoyée ou en retard).",
    "facture_autre_dossier": "La facture appartient à un autre dossier.",
    "virement_excède_facture": "Le montant dépasse le solde dû de la facture.",
    "facture_requise": (
        "Un virement d'honoraires doit être appuyé par une facture : indiquez une "
        "facture de Pallas Athéna ou, pour une facture antérieure, son numéro externe."
    ),
    "facture_ambiguë": (
        "Indiquez soit une facture de Pallas Athéna, soit un numéro de facture "
        "externe — jamais les deux."
    ),
    "solde_compensé_insuffisant": (
        "Solde compensé insuffisant : un déboursé ne peut puiser que dans les fonds "
        "déjà compensés. Attendez la compensation des dépôts en transit."
    ),
    "compteur_indisponible": "Impossible d'allouer le numéro de séquence. Veuillez réessayer.",
    "motif_requis": "Un motif de contre-passation est requis.",
    "déjà_contrepassée": "Cette écriture a déjà été contre-passée.",
    "écriture_introuvable": "Écriture introuvable.",
    "compensation_invalide": (
        "Impossible de compenser : écriture déjà compensée ou annulée, "
        "date de compensation antérieure à l'écriture, ou future."
    ),
    "conciliation_introuvable": "Conciliation introuvable.",
    "conciliation_non_brouillon": "Cette conciliation est déjà complétée.",
    "conciliation_variance": "La conciliation n'est pas équilibrée (écart non nul).",
    "conciliation_modifiée": "Le compte a changé pendant la conciliation. Veuillez recommencer.",
    "conciliation_période_future": "La date de fin de période ne peut être dans le futur.",
    "conciliation_écriture_invalide": (
        "Une des écritures sélectionnées n'est pas conciliable pour cette période "
        "(déjà compensée, annulée ou postérieure à la fin de période)."
    ),
    "relevé_requis": "Le solde du relevé est requis.",
    "transfert_identique": "La source et la destination doivent être différentes.",
}


def _sanitize_data(data: dict) -> dict:
    """Sanitize every top-level string value (mirrors invoice._sanitize_data)."""
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


def _sub(a: dict, b: dict) -> dict:
    return {k: a[k] - b[k] for k in a}


# ── trust_accounts CRUD + counter (spec §3.1, §3.5) ────────────────────────


def _default_account() -> dict:
    return {
        "id": "",
        "name": "",
        "account_type": "général",
        "dossier_id": None,
        "client_id": None,
        "institution": "",
        "transit": "",
        "account_number_last4": "",
        "book_balance": 0,
        "bank_balance": 0,
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
    if data.get("account_type") == "spécial":
        if not data.get("dossier_id"):
            errors.append("Un compte spécial doit être rattaché à un dossier.")
        if not data.get("client_id"):
            errors.append("Un compte spécial doit être rattaché à un client.")
    transit = data.get("transit", "")
    if transit and (not transit.isdigit() or len(transit) > 5):
        errors.append("Le numéro de transit doit comporter au plus 5 chiffres.")
    last4 = data.get("account_number_last4", "")
    if last4 and (not last4.isdigit() or len(last4) > 4):
        errors.append("N'inscrivez que les 4 derniers chiffres du compte.")
    if data.get("status") not in VALID_ACCOUNT_STATUSES:
        errors.append("Statut de compte invalide.")
    # A closed account must be at a zero book balance (§3.1). book_balance is
    # never supplied by a form — on update it comes from `existing`.
    if data.get("status") == "fermé" and int(data.get("book_balance", 0)) != 0:
        errors.append("Un compte ne peut être fermé que si son solde aux livres est nul.")
    return errors


def create_account(data: dict) -> tuple[Optional[dict], list[str]]:
    """Create a trust account. Balances are system-owned (always seeded to 0)."""
    merged = {**_default_account(), **_sanitize_data(data)}
    errors = _validate_account(merged)
    if errors:
        return None, errors
    now = datetime.now(timezone.utc)
    account_id = str(uuid.uuid4())
    merged.update({
        "id": account_id,
        "book_balance": 0,
        "bank_balance": 0,
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
        log_unexpected("trust account write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


def get_account(account_id: str) -> Optional[dict]:
    try:
        doc = db.collection(ACCOUNTS_COLLECTION).document(account_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("trust account read failed")
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
    """Update account METADATA only — balances and structure are never editable."""
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
        log_unexpected("trust account write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return merged, []


# ── Transaction assembly helpers ───────────────────────────────────────────


def _read_last_transaction(account_id: str, txn) -> Optional[dict]:
    """Newest entry for an account, read inside the transaction (backdating
    guard). One indexed query (account_id ASC, sequence DESC, limit 1)."""
    q = (
        db.collection(TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence", direction=firestore.Query.DESCENDING)
        .limit(1)
    )
    docs = list(q.stream(transaction=txn))
    return docs[0].to_dict() if docs else None


def _build_transaction_doc(
    *,
    tx_id: str,
    account_id: str,
    sequence: int,
    date,
    direction: str,
    amount: int,
    purpose: str,
    method: str,
    counterparty: str,
    dossier: Optional[dict],
    dossier_id: Optional[str],
    client_id: Optional[str],
    reference: str,
    description: str,
    invoice_id: Optional[str],
    balance_after_account: int,
    balance_after_client: int,
    now: datetime,
    invoice_external_ref: str = "",
    status: str = "en_circulation",
    cleared_date=None,
    reconciliation_id: Optional[str] = None,
    reverses_id: Optional[str] = None,
    reversed_by_id: Optional[str] = None,
    related_transaction_id: Optional[str] = None,
) -> dict:
    """Assemble a full trust_transactions doc, snapshotting the dossier/client
    labels off the read dossier (spec §3.2). ``date`` is stored midnight UTC."""
    client_name = ""
    dossier_file_number = ""
    dossier_title = ""
    if dossier:
        dossier_file_number = dossier.get("file_number", "")
        dossier_title = dossier.get("title", "")
        if client_id:
            for c in dossier.get("clients", []):
                if c.get("id") == client_id:
                    client_name = c.get("name", "")
                    break
    return {
        "id": tx_id,
        "account_id": account_id,
        "sequence": sequence,
        "date": _midnight_utc(date),
        "dossier_file_number": dossier_file_number,
        "counterparty": counterparty,
        "client_name": client_name,
        "purpose": purpose,
        "method": method,
        "direction": direction,
        "amount": amount,
        "balance_after_account": balance_after_account,
        "balance_after_client": balance_after_client,
        "dossier_id": dossier_id,
        "dossier_title": dossier_title,
        "client_id": client_id,
        "reference": reference,
        "description": description,
        "status": status,
        "cleared_date": _midnight_utc(cleared_date) if cleared_date else None,
        "reconciliation_id": reconciliation_id,
        "invoice_id": invoice_id,
        # Number of an invoice that predates Pallas Athéna (no invoice row to
        # link). Recorded, NOT verifiable — see the create guard.
        "invoice_external_ref": invoice_external_ref,
        "reverses_id": reverses_id,
        "reversed_by_id": reversed_by_id,
        "related_transaction_id": related_transaction_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    }


def _precheck_transaction(clean: dict) -> list[str]:
    """Guards that need no Firestore read (spec §5 step 2, the cheap subset)."""
    amount = clean.get("amount")
    direction = clean.get("direction", "")
    purpose = clean.get("purpose", "")
    method = clean.get("method", "")
    counterparty = (clean.get("counterparty") or "").strip()
    dossier_id = clean.get("dossier_id") or None
    client_id = clean.get("client_id") or None

    def _msg(reason: str) -> list[str]:
        return [_ABORT_MESSAGES[reason]]

    if not clean.get("account_id"):
        return _msg("compte_introuvable")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return _msg("montant_invalide")
    if direction not in VALID_DIRECTIONS:
        return _msg("direction_invalide")
    # correction is reserved for reverse_transaction — reject it on the create path.
    if purpose not in VALID_PURPOSES or purpose == REVERSAL_PURPOSE:
        return _msg("objet_invalide")
    if method not in VALID_METHODS:
        return _msg("mode_invalide")
    if not counterparty:
        return _msg("contrepartie_requise")
    if (dossier_id is None) != (client_id is None):
        return _msg("bénéficiaire_incohérent")
    if dossier_id is None and purpose not in NO_DOSSIER_PURPOSES:
        return _msg("objet_sans_dossier_invalide")
    if clean.get("date") is None:
        return _msg("date_requise")
    return []


# ── create_transaction — the core transaction (spec §5) ────────────────────


def create_transaction(data: dict) -> tuple[Optional[dict], list[str]]:
    """Append one entry to a trust register inside a Firestore transaction (§5).

    Append-only. The overdraft control (§4.3) and backdating guard run INSIDE
    the transaction on the same read-set as the write. Any read failure aborts
    (fails CLOSED). Returns ``(entry, [])`` or ``(None, [french_errors])``.
    """
    clean = _sanitize_data(data)
    errors = _precheck_transaction(clean)
    if errors:
        return None, errors

    account_id = clean["account_id"]
    direction = clean["direction"]
    purpose = clean["purpose"]
    method = clean["method"]
    counterparty = clean["counterparty"].strip()
    dossier_id = clean.get("dossier_id") or None
    client_id = clean.get("client_id") or None
    invoice_id = clean.get("invoice_id") or None
    invoice_external_ref = (clean.get("invoice_external_ref") or "").strip()
    # Invoice backing only applies to a fee transfer; drop stray values the
    # form's hidden (x-show) fields may still submit on any other purpose.
    if purpose != "virement_honoraires":
        invoice_id = None
        invoice_external_ref = ""
    amount = int(clean["amount"])
    tx_date = clean.get("date")

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
        last_tx = _read_last_transaction(account_id, txn)

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
        if dossier_id is not None and client_id not in (dossier.get("client_ids") or []):
            raise _TxnAbort("client_hors_dossier")
        if last_tx is not None:
            last_date = _as_utc(last_tx.get("date"))
            if last_date is not None and _as_utc(tx_date).date() < last_date.date():
                raise _TxnAbort("antidatage_refusé")
        # A fee transfer must be BACKED BY AN INVOICE. Two ways to satisfy that:
        #   1. a linked Pallas Athéna invoice — fully verifiable (issued, same
        #      dossier, amount <= solde dû); or
        #   2. an external invoice number, for an invoice that predates Pallas
        #      Athéna and has no row to link (user decision 2026-07-17). The
        #      amount CANNOT be verified in that case — the register records what
        #      the lawyer attests. Never both, never neither.
        if purpose == "virement_honoraires":
            if direction != "déboursé":
                raise _TxnAbort("virement_direction")
            if invoice is not None:
                if invoice_external_ref:
                    raise _TxnAbort("facture_ambiguë")
                if invoice.get("status") not in _ISSUED_INVOICE_STATUSES:
                    raise _TxnAbort("facture_non_émise")
                if invoice.get("dossier_id") != dossier_id:
                    raise _TxnAbort("facture_autre_dossier")
                if amount > int(invoice.get("amount_due", 0)):
                    raise _TxnAbort("virement_excède_facture")
            elif not invoice_external_ref:
                raise _TxnAbort("facture_requise")

        book_map = dict((dossier or {}).get("trust_balance_by_client") or {})
        cleared_map = dict((dossier or {}).get("trust_cleared_by_client") or {})
        current_cleared = int(cleared_map.get(client_id, 0)) if client_id else 0

        # 3. Overdraft control (§4.3) — reversals bypass this, creates do not.
        if direction == "déboursé":
            ok, _reason = check_disbursement_allowed(current_cleared, amount)
            if not ok:
                raise _TxnAbort("solde_compensé_insuffisant")

        # 4. COMPUTE (a create is always en_circulation)
        seq = seq_current + 1
        deltas = compute_deltas(direction, amount, "en_circulation")
        book_after_account = int(account.get("book_balance", 0)) + deltas["book"]
        book_after_client = int(book_map.get(client_id, 0)) + deltas["book"] if client_id else 0
        cleared_after_client = current_cleared + deltas["cleared"] if client_id else 0

        entry = _build_transaction_doc(
            tx_id=tx_id, account_id=account_id, sequence=seq, date=tx_date,
            direction=direction, amount=amount, purpose=purpose, method=method,
            counterparty=counterparty, dossier=dossier, dossier_id=dossier_id,
            client_id=client_id, reference=clean.get("reference", ""),
            description=clean.get("description", ""), invoice_id=invoice_id,
            invoice_external_ref=invoice_external_ref,
            balance_after_account=book_after_account,
            balance_after_client=book_after_client, now=now,
        )

        # 5. WRITES (single commit)
        txn.set(tx_ref, entry)
        txn.set(counter_ref, {"seq": seq, "updated_at": now})
        txn.update(account_ref, {
            "book_balance": book_after_account,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        if dossier_ref is not None and client_id:
            book_map[client_id] = book_after_client
            cleared_map[client_id] = cleared_after_client
            txn.update(dossier_ref, {
                "trust_balance_by_client": book_map,
                "trust_cleared_by_client": cleared_map,
                "trust_balance": sum(int(v) for v in book_map.values()),
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        result["entry"] = entry

    try:
        with span("trust.transaction", direction=direction, purpose=purpose, dossier_id=dossier_id):
            _create(transaction)
    except _TxnAbort as abort:
        if abort.reason == "solde_compensé_insuffisant":
            log_trust_event(
                "trust_overdraft_refused", "refused",
                dossier_id=dossier_id, account_id=account_id,
                reason="insufficient_cleared_balance",
            )
        else:
            log_trust_event(
                "trust_transaction_refused", "refused",
                account_id=account_id, dossier_id=dossier_id, reason=abort.reason,
            )
        return None, [_ABORT_MESSAGES.get(abort.reason, "Opération refusée.")]
    except Exception as exc:
        logger.error(
            "create_transaction failed for account %s: %s",
            sanitize_log_value(account_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    entry = result["entry"]
    log_trust_event(
        "trust_transaction_created", transaction_id=tx_id,
        dossier_id=dossier_id, account_id=account_id,
        direction=direction, purpose=purpose, sequence=entry["sequence"],
    )
    return entry, []


# ── clear_transaction / clear_transactions_bulk (spec §5.1) ────────────────


def _clear_entries(
    tx_ids: list, cleared_date, reconciliation_id: Optional[str]
) -> tuple[list[dict], list[str]]:
    """Clear en_circulation entries to compensée, all-or-nothing, in one
    transaction. Returns ``(cleared_docs, failed_ids)``; any failure aborts the
    whole batch (spec §5.1). Clearing a recette adds to the client's cleared
    balance; both directions move the account's bank balance."""
    if not tx_ids:
        return [], []
    cd = _midnight_utc(cleared_date)
    today = datetime.now(timezone.utc)
    now = today
    if cd is None or cd.date() > today.date():
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
        account = acc_snap.to_dict()

        dossier_ids = {
            e["dossier_id"]
            for e in entries
            if e.get("direction") == "recette" and e.get("dossier_id") and e.get("client_id")
        }
        dossier_snaps = {}
        for did in dossier_ids:
            dref = db.collection(DOSSIERS_COLLECTION).document(did)
            dsnap = dref.get(transaction=txn)
            dossier_snaps[did] = (dref, dsnap.to_dict() if dsnap.exists else None)

        bank_delta = 0
        cleared_add: dict = {}
        for e in entries:
            amt = int(e.get("amount", 0))
            bank_delta += amt if e.get("direction") == "recette" else -amt
            if e.get("direction") == "recette" and e.get("dossier_id") and e.get("client_id"):
                cleared_add.setdefault(e["dossier_id"], {})
                cleared_add[e["dossier_id"]][e["client_id"]] = (
                    cleared_add[e["dossier_id"]].get(e["client_id"], 0) + amt
                )

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

        txn.update(account_ref, {
            "bank_balance": int(account.get("bank_balance", 0)) + bank_delta,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        for did, (dref, ddoc) in dossier_snaps.items():
            if ddoc is None:
                continue
            cmap = dict(ddoc.get("trust_cleared_by_client") or {})
            for cid, add in cleared_add.get(did, {}).items():
                cmap[cid] = int(cmap.get(cid, 0)) + add
            txn.update(dref, {
                "trust_cleared_by_client": cmap,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        outcome["cleared"] = cleared_docs

    try:
        _txn(transaction)
    except _TxnAbort:
        return [], outcome["failed"] or list(tx_ids)
    except Exception as exc:
        logger.error("clear entries failed: %s", type(exc).__name__)
        return [], list(tx_ids)
    return outcome["cleared"], []


def clear_transaction(tx_id: str, cleared_date) -> tuple[Optional[dict], list[str]]:
    """Step 2 of the lifecycle: mark one en_circulation entry compensée (§5.1)."""
    cleared, failed = _clear_entries([tx_id], cleared_date, None)
    if failed or not cleared:
        return None, [_ABORT_MESSAGES["compensation_invalide"]]
    entry = cleared[0]
    log_trust_event(
        "trust_transaction_cleared", transaction_id=tx_id,
        account_id=entry.get("account_id"), dossier_id=entry.get("dossier_id"),
    )
    return entry, []


def clear_transactions_bulk(tx_ids: list, cleared_date) -> tuple[int, list[str]]:
    """Clear many entries at once, all-or-nothing (§5.1). Returns
    ``(cleared_count, failed_ids)`` — on any failure ``(0, failed_ids)``."""
    cleared, failed = _clear_entries(list(tx_ids), cleared_date, None)
    if failed:
        return 0, failed
    for entry in cleared:
        log_trust_event(
            "trust_transaction_cleared", transaction_id=entry.get("id"),
            account_id=entry.get("account_id"), dossier_id=entry.get("dossier_id"),
        )
    return len(cleared), []


# ── reverse_transaction (spec §5.2) ────────────────────────────────────────


def reverse_transaction(tx_id: str, reason: str) -> tuple[Optional[dict], list[str]]:
    """Contre-passation: create an opposite « correction » entry; never edit the
    original's amount or direction (§5.2). Reversing an ``en_circulation`` entry
    stamps BOTH annulée; reversing a ``compensée`` entry creates an
    ``en_circulation`` reversal. The overdraft control does NOT apply here."""
    reason = (reason or "").strip()
    if not reason:
        return None, [_ABORT_MESSAGES["motif_requis"]]

    orig_ref = db.collection(TRANSACTIONS_COLLECTION).document(tx_id)
    now = datetime.now(timezone.utc)
    rev_id = str(uuid.uuid4())
    rev_ref = db.collection(TRANSACTIONS_COLLECTION).document(rev_id)
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

        account_id = original.get("account_id")
        account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
        counter_ref = db.collection(COUNTERS_COLLECTION).document(_counter_id(account_id))
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        account = acc_snap.to_dict()
        counter_snap = counter_ref.get(transaction=txn)
        seq_current = (
            int((counter_snap.to_dict() or {}).get("seq", 0)) if counter_snap.exists else 0
        )

        dossier_id = original.get("dossier_id")
        client_id = original.get("client_id")
        dossier = None
        dossier_ref = None
        if dossier_id and client_id:
            dossier_ref = db.collection(DOSSIERS_COLLECTION).document(dossier_id)
            d_snap = dossier_ref.get(transaction=txn)
            dossier = d_snap.to_dict() if d_snap.exists else None

        orig_dir = original.get("direction")
        amount = int(original.get("amount", 0))
        rev_dir = "déboursé" if orig_dir == "recette" else "recette"
        orig_status = original.get("status")

        if orig_status == "en_circulation":
            orig_new_status = "annulée"
            rev_status = "annulée"
        else:  # compensée — original stays compensée
            orig_new_status = orig_status
            rev_status = "en_circulation"

        # Operation delta = reversal contribution + original's transition (if any)
        rev_contrib = compute_deltas(rev_dir, amount, rev_status)
        if orig_new_status != orig_status:
            orig_delta = _sub(
                compute_deltas(orig_dir, amount, orig_new_status),
                compute_deltas(orig_dir, amount, orig_status),
            )
        else:
            orig_delta = {"book": 0, "cleared": 0, "bank": 0}
        total = {k: rev_contrib[k] + orig_delta[k] for k in rev_contrib}

        seq = seq_current + 1
        book_after_account = int(account.get("book_balance", 0)) + total["book"]
        book_map = dict((dossier or {}).get("trust_balance_by_client") or {})
        cleared_map = dict((dossier or {}).get("trust_cleared_by_client") or {})
        book_after_client = (
            int(book_map.get(client_id, 0)) + total["book"] if client_id else 0
        )

        reversal = _build_transaction_doc(
            tx_id=rev_id, account_id=account_id, sequence=seq, date=now,
            direction=rev_dir, amount=amount, purpose=REVERSAL_PURPOSE,
            method=original.get("method", ""), counterparty=original.get("counterparty", ""),
            dossier=None, dossier_id=dossier_id, client_id=client_id,
            reference=original.get("reference", ""), description=reason, invoice_id=None,
            balance_after_account=book_after_account, balance_after_client=book_after_client,
            now=now, status=rev_status, cleared_date=None, reverses_id=tx_id,
        )
        # Copy the frozen snapshots off the original (dossier=None above).
        reversal["dossier_file_number"] = original.get("dossier_file_number", "")
        reversal["dossier_title"] = original.get("dossier_title", "")
        reversal["client_name"] = original.get("client_name", "")

        txn.set(rev_ref, reversal)
        txn.set(counter_ref, {"seq": seq, "updated_at": now})
        txn.update(orig_ref, {
            "status": orig_new_status,
            "reversed_by_id": rev_id,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        txn.update(account_ref, {
            "book_balance": book_after_account,
            "bank_balance": int(account.get("bank_balance", 0)) + total["bank"],
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        if dossier_ref is not None and dossier is not None and client_id:
            book_map[client_id] = book_after_client
            cleared_map[client_id] = int(cleared_map.get(client_id, 0)) + total["cleared"]
            txn.update(dossier_ref, {
                "trust_balance_by_client": book_map,
                "trust_cleared_by_client": cleared_map,
                "trust_balance": sum(int(v) for v in book_map.values()),
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        result["reversal"] = reversal
        result["annulled"] = rev_status == "annulée"

    try:
        with span("trust.transaction", direction="reversal", purpose=REVERSAL_PURPOSE, dossier_id=None):
            _reverse(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Contre-passation refusée.")]
    except Exception as exc:
        logger.error(
            "reverse_transaction failed for %s: %s",
            sanitize_log_value(tx_id), type(exc).__name__,
        )
        return None, ["Erreur lors de la contre-passation. Veuillez réessayer."]

    reversal = result["reversal"]
    log_trust_event(
        "trust_transaction_reversed", transaction_id=rev_id,
        account_id=reversal.get("account_id"), dossier_id=reversal.get("dossier_id"),
        reverses_id=tx_id, annulled=result["annulled"],
    )
    return reversal, []


# ── create_inter_dossier_transfer (spec §6.4) ──────────────────────────────


def create_inter_dossier_transfer(
    account_id: str,
    from_dossier_id: str,
    from_client_id: str,
    to_dossier_id: str,
    to_client_id: str,
    amount: int,
    description: str,
    method: str,
    reference: str,
) -> tuple[Optional[dict], list[str]]:
    """Move funds between two (dossier, client) couples within ONE account, as
    two linked ``compensée`` legs (§6.4). The overdraft control applies to the
    source leg. ``account_id`` is required here (the spec signature omitted it,
    but every leg needs an account); a single « général » account is the norm."""
    if (from_dossier_id, from_client_id) == (to_dossier_id, to_client_id):
        return None, [_ABORT_MESSAGES["transfert_identique"]]
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None, [_ABORT_MESSAGES["montant_invalide"]]
    if method not in VALID_METHODS:
        return None, [_ABORT_MESSAGES["mode_invalide"]]

    description = sanitize(description or "", max_length=2000)
    reference = sanitize(reference or "", max_length=2000)
    now = datetime.now(timezone.utc)
    leg_a_id = str(uuid.uuid4())
    leg_b_id = str(uuid.uuid4())

    account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
    counter_ref = db.collection(COUNTERS_COLLECTION).document(_counter_id(account_id))
    from_ref = db.collection(DOSSIERS_COLLECTION).document(from_dossier_id)
    to_ref = db.collection(DOSSIERS_COLLECTION).document(to_dossier_id)
    transaction = db.transaction()
    result: dict = {}

    @firestore.transactional
    def _transfer(txn) -> None:
        acc_snap = account_ref.get(transaction=txn)
        if not acc_snap.exists:
            raise _TxnAbort("compte_introuvable")
        account = acc_snap.to_dict()
        counter_snap = counter_ref.get(transaction=txn)
        seq_current = (
            int((counter_snap.to_dict() or {}).get("seq", 0)) if counter_snap.exists else 0
        )
        f_snap = from_ref.get(transaction=txn)
        t_snap = to_ref.get(transaction=txn)
        if not f_snap.exists or not t_snap.exists:
            raise _TxnAbort("dossier_introuvable")
        from_dossier = f_snap.to_dict()
        to_dossier = t_snap.to_dict()

        if account.get("status") != "actif":
            raise _TxnAbort("compte_fermé")
        if from_client_id not in (from_dossier.get("client_ids") or []):
            raise _TxnAbort("client_hors_dossier")
        if to_client_id not in (to_dossier.get("client_ids") or []):
            raise _TxnAbort("client_hors_dossier")

        from_book = dict(from_dossier.get("trust_balance_by_client") or {})
        from_cleared = dict(from_dossier.get("trust_cleared_by_client") or {})
        to_book = dict(to_dossier.get("trust_balance_by_client") or {})
        to_cleared = dict(to_dossier.get("trust_cleared_by_client") or {})

        # Overdraft control on the source leg (a déboursé).
        ok, _reason = check_disbursement_allowed(int(from_cleared.get(from_client_id, 0)), amount)
        if not ok:
            raise _TxnAbort("solde_compensé_insuffisant")

        def _client_name(dossier, cid):
            for c in dossier.get("clients", []):
                if c.get("id") == cid:
                    return c.get("name", "")
            return ""

        seq_a = seq_current + 1
        seq_b = seq_current + 2
        # Both legs compensée immediately — the funds never leave the account.
        # Source (déboursé) & destination (recette) net to zero on the account.
        from_book_after = int(from_book.get(from_client_id, 0)) - amount
        from_cleared_after = int(from_cleared.get(from_client_id, 0)) - amount
        to_book_after = int(to_book.get(to_client_id, 0)) + amount
        to_cleared_after = int(to_cleared.get(to_client_id, 0)) + amount

        leg_a = _build_transaction_doc(
            tx_id=leg_a_id, account_id=account_id, sequence=seq_a, date=now,
            direction="déboursé", amount=amount, purpose="virement_inter_dossiers",
            method=method, counterparty=_client_name(to_dossier, to_client_id),
            dossier=from_dossier, dossier_id=from_dossier_id, client_id=from_client_id,
            reference=reference, description=description, invoice_id=None,
            balance_after_account=int(account.get("book_balance", 0)),  # net 0
            balance_after_client=from_book_after, now=now,
            status="compensée", cleared_date=now, related_transaction_id=leg_b_id,
        )
        leg_b = _build_transaction_doc(
            tx_id=leg_b_id, account_id=account_id, sequence=seq_b, date=now,
            direction="recette", amount=amount, purpose="virement_inter_dossiers",
            method=method, counterparty=_client_name(from_dossier, from_client_id),
            dossier=to_dossier, dossier_id=to_dossier_id, client_id=to_client_id,
            reference=reference, description=description, invoice_id=None,
            balance_after_account=int(account.get("book_balance", 0)),  # net 0
            balance_after_client=to_book_after, now=now,
            status="compensée", cleared_date=now, related_transaction_id=leg_a_id,
        )

        txn.set(db.collection(TRANSACTIONS_COLLECTION).document(leg_a_id), leg_a)
        txn.set(db.collection(TRANSACTIONS_COLLECTION).document(leg_b_id), leg_b)
        txn.set(counter_ref, {"seq": seq_b, "updated_at": now})
        # Account book & bank net to zero, but bump the audit metadata.
        txn.update(account_ref, {"updated_at": now, "etag": str(uuid.uuid4())})

        from_book[from_client_id] = from_book_after
        from_cleared[from_client_id] = from_cleared_after
        txn.update(from_ref, {
            "trust_balance_by_client": from_book,
            "trust_cleared_by_client": from_cleared,
            "trust_balance": sum(int(v) for v in from_book.values()),
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        to_book[to_client_id] = to_book_after
        to_cleared[to_client_id] = to_cleared_after
        txn.update(to_ref, {
            "trust_balance_by_client": to_book,
            "trust_cleared_by_client": to_cleared,
            "trust_balance": sum(int(v) for v in to_book.values()),
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        result["legs"] = [leg_a, leg_b]

    try:
        with span("trust.transaction", direction="transfer", purpose="virement_inter_dossiers", dossier_id=from_dossier_id):
            _transfer(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Virement refusé.")]
    except Exception as exc:
        logger.error("inter-dossier transfer failed: %s", type(exc).__name__)
        return None, ["Erreur lors du virement. Veuillez réessayer."]

    legs = result["legs"]
    for leg in legs:
        log_trust_event(
            "trust_transaction_created", transaction_id=leg["id"],
            dossier_id=leg.get("dossier_id"), account_id=account_id,
            direction=leg["direction"], purpose="virement_inter_dossiers",
            sequence=leg["sequence"],
        )
    return legs[0], []


# ── Reconciliation (spec §3.3, §7) ─────────────────────────────────────────


def list_outstanding(account_id: str, as_of=None) -> list[dict]:
    """Outstanding cheques: en_circulation déboursés on the account (index #4)."""
    return _list_en_circ(account_id, "déboursé", as_of)


def list_in_transit(account_id: str, as_of=None) -> list[dict]:
    """Deposits in transit: en_circulation recettes on the account (index #4)."""
    return _list_en_circ(account_id, "recette", as_of)


def _list_by_status(account_id: str, status: str, direction: str) -> list[dict]:
    """All entries of one status+direction on the account, sequence order.

    Same query shape for every status value — the composite index #4
    (account_id, status, direction, sequence) serves them all (equality
    slots share the composite; no new index).
    """
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


def book_balance_as_of(account_id: str, as_of) -> int:
    """Book balance at the END of day *as_of*: the frozen
    ``balance_after_account`` of the highest-sequence entry dated <= as_of;
    0 when no entry qualifies.

    Exact because per-account dates are NON-DECREASING in sequence order —
    the backdating guard refuses an earlier date and reversals/transfer legs
    are dated now — so the first row of a ``sequence DESC`` stream (index #1,
    the ``list_transactions_page`` index) whose date <= as_of IS the last
    entry of the day. No SUM, no recomputation, no new index. Read errors
    propagate (fail CLOSED — callers catch).
    """
    cutoff = _as_utc(as_of).date()
    q = (
        db.collection(TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence", direction=firestore.Query.DESCENDING)
    )
    for snap in q.stream():
        row = snap.to_dict()
        if _as_utc(row.get("date")).date() <= cutoff:
            return int(row.get("balance_after_account", 0))
    return 0


def opening_book_balance(account_id: str, as_of_exclusive) -> tuple[int, bool]:
    """Carried-forward book balance the DAY BEFORE *as_of_exclusive*.

    The « solde reporté » a period register opens on (art. 38 RLRQ c. B-1,
    r. 5 wants the running balance after each entry; a period sheet is only
    readable if it says what it opened on). Returns
    ``(cents, had_prior_entry)`` — the flag separates « reporté : 0,00 $ »
    from « no entry precedes this period at all », which a book of account
    must not blur into an ambiguous zero.

    Exact for the reason :func:`book_balance_as_of` is: per-account dates are
    NON-DECREASING in sequence order — the backdating guard refuses an
    earlier date and reversals are dated now — so the last entry dated before
    D carries the balance the period opens on, already FROZEN in
    ``balance_after_account``. No SUM, no recomputation.

    Uses the SAME query shape as :func:`book_balance_as_of` — ``(account_id
    ==, sequence DESC)``, index #1, in production since Phase K — and stops at
    the first row dated before the cutoff.

    It read ONE document until 2026-08-11: ``date < D`` ordered ``date DESC,
    sequence DESC``, on the belief that Firestore serves a composite index's
    complete inverse. It does — but « complete » means EVERY field flips,
    ``account_id`` included, so the inverse of ``(account_id ASC, date ASC,
    sequence ASC)`` is ``(account_id DESC, …)`` and not the ``(account_id ASC,
    date DESC, sequence DESC)`` this needed. An equality field looks
    direction-free from the query side and is not, in the index. Firestore
    answered FAILED_PRECONDITION and the export served a generic error page.
    A book of account is not the place to bet on an index-direction subtlety:
    the proven shape costs the entries booked since the period opened, which
    is what a reconciliation already reads.

    Read errors propagate (fail CLOSED — the caller decides what a register
    does when it cannot be trusted).
    """
    cutoff = _midnight_utc(as_of_exclusive)
    if cutoff is None:
        return 0, False
    q = (
        db.collection(TRANSACTIONS_COLLECTION)
        .where(filter=FieldFilter("account_id", "==", account_id))
        .order_by("sequence", direction=firestore.Query.DESCENDING)
    )
    for snap in q.stream():
        row = snap.to_dict() or {}
        row_date = _as_utc(row.get("date"))
        if isinstance(row_date, datetime) and row_date < cutoff:
            return int(row.get("balance_after_account", 0)), True
    return 0, False


def implied_opening_balance(first_tx: dict) -> int:
    """The opening balance the period's FIRST entry implies, read back from
    its own frozen running balance. Pure — no read.

    ``balance_after_account`` minus that entry's own book contribution IS the
    balance standing just before it. Cross-checking it against
    :func:`opening_book_balance` costs nothing and catches the one thing that
    would make the register silently wrong: a broken date/sequence invariant
    (an entry written outside ``create_transaction``, a reset counter). A
    mismatch is reported on the sheet, never swallowed.
    """
    delta = compute_deltas(
        first_tx.get("direction", ""),
        int(first_tx.get("amount") or 0),
        first_tx.get("status", ""),
    )
    return int(first_tx.get("balance_after_account", 0)) - delta["book"]


def _list_cleared_after(account_id: str, as_of) -> list[dict]:
    """Resurrection set: entries dated <= as_of that were STILL outstanding at
    as_of because they cleared later (``cleared_date`` > as_of — a later
    statement, or a manual « Compenser » after the period). Counted as
    outstanding/in-transit in the as-of variance; NEVER tickable (they are
    already compensée). Both directions, sequence order."""
    cutoff = _as_utc(as_of).date()
    rows = _list_by_status(account_id, "compensée", "déboursé") + _list_by_status(
        account_id, "compensée", "recette"
    )
    kept = []
    for r in rows:
        cd = r.get("cleared_date")
        if not isinstance(cd, (datetime, date)):
            # A compensée entry without a cleared_date is a data-integrity
            # impossibility — treat as not-resurrected; the variance will say
            # so loudly rather than this helper guessing.
            continue
        if (
            _as_utc(r.get("date")).date() <= cutoff
            and _as_utc(cd).date() > cutoff
        ):
            kept.append(r)
    kept.sort(key=lambda r: int(r.get("sequence", 0)))
    return kept


def _list_annulled_after(account_id: str, as_of) -> list[dict]:
    """Annulée exactness: an annulée ORIGINAL dated <= as_of whose reversal
    happened AFTER as_of was still en_circulation at as_of. The reversal is a
    ``correction`` row dated at reversal time, linked via ``reversed_by_id``;
    rows born annulée (the correction minted by reversing an en_circulation
    entry, ``reverses_id`` set) were never outstanding. The reverser normally
    sits in the same annulée result set; keyed ``get_transaction`` fallback
    otherwise — a failed read there raises (fail CLOSED)."""
    cutoff = _as_utc(as_of).date()
    rows = _list_by_status(account_id, "annulée", "déboursé") + _list_by_status(
        account_id, "annulée", "recette"
    )
    by_id = {r.get("id"): r for r in rows}
    kept = []
    for r in rows:
        reverser_id = r.get("reversed_by_id")
        if not reverser_id or r.get("reverses_id"):
            continue  # not an original (or not reversed — impossible for annulée)
        if _as_utc(r.get("date")).date() > cutoff:
            continue
        reverser = by_id.get(reverser_id)
        if reverser is None:
            reverser = get_transaction(reverser_id)
            if reverser is None:
                raise RuntimeError("trust: reversal row unreadable for as-of")
        if _as_utc(reverser.get("date")).date() > cutoff:
            kept.append(r)
    kept.sort(key=lambda r: int(r.get("sequence", 0)))
    return kept


def reconciliation_as_of_context(account_id: str, as_of) -> dict:
    """Every as-of input a worksheet render and a completion gate share, so
    the two can never drift (the server gate stays the only authority; the
    worksheet's client-side variance is UX). Raises on any read failure
    (fail CLOSED — never a silently wrong worksheet). Keys:

      book_as_of               int — frozen book balance at end of as_of
      outstanding              list — (a) en_circulation déboursés <= as_of, TICKABLE
      in_transit               list — (a) en_circulation recettes <= as_of, TICKABLE
      cleared_later            list — (b) compensée after the period, read-only
      annulled_later           list — (c) annulée after the period, read-only
      fixed_outstanding_total  int — Σ déboursés of (b)+(c)
      fixed_in_transit_total   int — Σ recettes  of (b)+(c)
    """
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


def create_reconciliation(
    account_id: str, period_end, statement_balance: int
) -> tuple[Optional[dict], list[str]]:
    """Open a brouillon reconciliation (§3.3). At most one brouillon per
    account; period_end must be after the last complétée reconciliation."""
    if get_account(account_id) is None:
        return None, [_ABORT_MESSAGES["compte_introuvable"]]
    if statement_balance is None:
        # Backstop of the route-level fix: a blank/unparseable amount must
        # error, never silently become 0,00 $ (0 itself is a LEGITIMATE
        # statement balance — an emptied account reads exactly zero).
        return None, [_ABORT_MESSAGES["relevé_requis"]]
    if not isinstance(statement_balance, int) or isinstance(statement_balance, bool):
        return None, ["Le solde du relevé doit être un montant en cents."]
    pe = _midnight_utc(period_end)
    if pe is None:
        return None, ["La date de fin de période est requise."]
    if pe.date() > datetime.now(timezone.utc).date():
        # A bank statement cannot postdate today — and completion stamps
        # cleared_date = period_end, which the clear-guard convention
        # (no future cleared_date) must keep honouring.
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
        "outstanding_cheques_total": 0,
        "deposits_in_transit_total": 0,
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
        log_unexpected("trust reconciliation write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    return doc, []


def get_reconciliation(rec_id: str) -> Optional[dict]:
    try:
        doc = db.collection(RECONCILIATIONS_COLLECTION).document(rec_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("trust reconciliation read failed")
        return None


def list_reconciliations(account_id: Optional[str] = None) -> list[dict]:
    """List reconciliations, newest period first (index #8)."""
    query = db.collection(RECONCILIATIONS_COLLECTION)
    if account_id:
        query = query.where(filter=FieldFilter("account_id", "==", account_id))
    query = query.order_by("period_end", direction=firestore.Query.DESCENDING)
    return [d.to_dict() for d in query.stream()]


def delete_reconciliation(rec_id: str) -> tuple[bool, list[str]]:
    """Abandon a DRAFT reconciliation. Only a brouillon may be deleted — a
    complétée rec is the audit trail behind stamped reconciliation_ids. A
    brouillon has never mutated any transaction (stamping happens only at
    completion), so deleting it violates no append-only invariant; it simply
    unblocks the one-brouillon guard. Transactional (status re-read then
    delete) to close the TOCTOU with a concurrent completion. Fails CLOSED.
    """
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
        logger.error("delete_reconciliation failed: %s", type(exc).__name__)
        return False, ["Erreur lors de la suppression. Veuillez réessayer."]

    log_trust_event(
        "trust_reconciliation_abandoned",
        reconciliation_id=rec_id,
        account_id=info.get("account_id"),
    )
    return True, []


def complete_reconciliation(
    rec_id: str, cleared_tx_ids: list
) -> tuple[Optional[dict], list[str]]:
    """Finalize a reconciliation: clear the checked entries, stamp them with
    the reconciliation id, and mark it complétée — but ONLY if the variance is
    zero (§3.3). Refuses otherwise (logs the variance).

    Every figure is AS OF ``period_end`` — never as of now — so a
    RETROACTIVE reconciliation balances: the book balance is the frozen
    running balance at the end of the period, and entries cleared/annulled
    AFTER the period count as still outstanding (the resurrection sets). Only
    still-en_circulation entries dated <= period_end are tickable; an item
    left unticked stays en_circulation and carries to the NEXT period's
    worksheet (cross-statement outstanding cheques). All writes are one
    transaction; a concurrent change to the account (etag sentinel) aborts.
    """
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
    statement_balance = int(rec.get("statement_balance", 0))
    checked_ids = set(cleared_tx_ids or [])

    # Pre-pass: the as-of context (fresh reads) feeds the gate + the log.
    # Streams cannot run inside the transaction (keyed reads only there);
    # the etag sentinel + in-txn per-entry re-reads carry the concurrency
    # duty the old « bank courant + deltas == relevé » equality used to.
    try:
        ctx = reconciliation_as_of_context(account_id, period_end)
    except Exception as exc:
        logger.error(
            "reconciliation as-of read failed: %s", type(exc).__name__
        )
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
        statement_balance, book_balance, outstanding_after, in_transit_after
    )
    if variance != 0:
        log_trust_event(
            "trust_reconciliation_variance", "refused",
            reconciliation_id=rec_id, account_id=account_id, variance_cents=variance,
        )
        return None, [_ABORT_MESSAGES["conciliation_variance"]]

    # Commit: clear the checked entries + stamp + finalize, atomically. The
    # transaction re-reads and re-verifies balance so a concurrent change aborts.
    now = datetime.now(timezone.utc)
    rec_ref = db.collection(RECONCILIATIONS_COLLECTION).document(rec_id)
    account_ref = db.collection(ACCOUNTS_COLLECTION).document(account_id)
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
        # Etag sentinel: every trust write path regenerates the account etag,
        # so any register movement between the pre-pass (whose as-of numbers
        # passed the gate) and this commit is caught here.
        if acc.get("etag") != account_etag:
            raise _TxnAbort("conciliation_modifiée")

        entries = []
        for ref in tx_refs:
            snap = ref.get(transaction=txn)
            e = snap.to_dict() if snap.exists else None
            # The entry's date is immutable, so the pre-pass <= period_end
            # check needs no re-verification — only the status can move.
            if not e or e.get("status") != "en_circulation":
                raise _TxnAbort("conciliation_modifiée")
            entries.append(e)

        # Affected dossiers (recette clears touch cleared maps).
        dossier_ids = {
            e["dossier_id"]
            for e in entries
            if e.get("direction") == "recette" and e.get("dossier_id") and e.get("client_id")
        }
        dossier_snaps = {}
        for did in dossier_ids:
            dref = db.collection(DOSSIERS_COLLECTION).document(did)
            dsnap = dref.get(transaction=txn)
            dossier_snaps[did] = (dref, dsnap.to_dict() if dsnap.exists else None)

        bank_delta = 0
        cleared_add: dict = {}
        for e in entries:
            amt = int(e.get("amount", 0))
            bank_delta += amt if e.get("direction") == "recette" else -amt
            if e.get("direction") == "recette" and e.get("dossier_id") and e.get("client_id"):
                cleared_add.setdefault(e["dossier_id"], {})
                cleared_add[e["dossier_id"]][e["client_id"]] = (
                    cleared_add[e["dossier_id"]].get(e["client_id"], 0) + amt
                )

        # bank_balance is INCREMENTED, never set to the statement figure: the
        # ticked items did clear at the bank (on or before the statement
        # date), so today's cleared balance includes them — but for a
        # RETROACTIVE period the old « = statement » equality is false (later
        # clearings already sit in bank_balance). += preserves exactly the
        # Σ-of-cleared-deltas invariant that verify_trust_integrity recomputes.
        post_clear_bank = int(acc.get("bank_balance", 0)) + bank_delta

        cd = _midnight_utc(period_end)
        for ref, e in zip(tx_refs, entries):
            txn.update(ref, {
                "status": "compensée",
                "cleared_date": cd,
                "reconciliation_id": rec_id,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        txn.update(account_ref, {
            "bank_balance": post_clear_bank,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        for did, (dref, ddoc) in dossier_snaps.items():
            if ddoc is None:
                continue
            cmap = dict(ddoc.get("trust_cleared_by_client") or {})
            for cid, add in cleared_add.get(did, {}).items():
                cmap[cid] = int(cmap.get(cid, 0)) + add
            txn.update(dref, {
                "trust_cleared_by_client": cmap,
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        finalized = {
            **rec,
            "status": "complétée",
            "book_balance": book_balance,
            "outstanding_cheques_total": outstanding_after,
            "deposits_in_transit_total": in_transit_after,
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
        with span("trust.reconcile", account_id=account_id, cleared_count=len(checked_ids)):
            _complete(transaction)
    except _TxnAbort as abort:
        return None, [_ABORT_MESSAGES.get(abort.reason, "Conciliation refusée.")]
    except Exception as exc:
        logger.error("complete_reconciliation failed: %s", type(exc).__name__)
        return None, ["Erreur lors de la conciliation. Veuillez réessayer."]

    log_trust_event(
        "trust_reconciliation_completed", reconciliation_id=rec_id,
        account_id=account_id, cleared_count=result["cleared_count"],
    )
    return result["reconciliation"], []


# ── Queries + summaries ────────────────────────────────────────────────────


def get_transaction(tx_id: str) -> Optional[dict]:
    try:
        doc = db.collection(TRANSACTIONS_COLLECTION).document(tx_id).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        log_unexpected("trust transaction read failed")
        return None


def list_transactions(
    account_id: Optional[str] = None,
    dossier_id: Optional[str] = None,
    client_id: Optional[str] = None,
    date_from=None,
    date_to=None,
    status: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """List register rows. Primary filter (carte vs journal) is pushed to
    Firestore + ordered by sequence; the rest are applied in Python over the
    bounded fetch. Fails CLOSED (propagates read errors to the route)."""
    if dossier_id and client_id:
        query = (
            db.collection(TRANSACTIONS_COLLECTION)
            .where(filter=FieldFilter("dossier_id", "==", dossier_id))
            .where(filter=FieldFilter("client_id", "==", client_id))
            .order_by("sequence")
        )
    elif account_id:
        query = (
            db.collection(TRANSACTIONS_COLLECTION)
            .where(filter=FieldFilter("account_id", "==", account_id))
            .order_by("sequence")
        )
    else:
        query = db.collection(TRANSACTIONS_COLLECTION).order_by("sequence")

    rows = [d.to_dict() for d in query.limit(max(limit * 3, limit)).stream()]
    df = _as_utc(date_from).date() if date_from else None
    dt = _as_utc(date_to).date() if date_to else None
    out = []
    for r in rows:
        if status and r.get("status") != status:
            continue
        if direction and r.get("direction") != direction:
            continue
        rd = _as_utc(r.get("date"))
        if df and (rd is None or rd.date() < df):
            continue
        if dt and (rd is None or rd.date() > dt):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def list_register(
    account_id: str, date_from=None, date_to=None, limit: int = 10000
) -> tuple[list[dict], bool]:
    """One account's COMPLETE register for a period, chronological.

    Returns ``(rows, truncated)``. The register the art. 38 sheet prints:
    every entry, every status, both directions — a book of account is
    complete by definition, and only a complete one lets « report + Σ
    recettes − Σ déboursés = solde de clôture » verify.

    Date bounds are pushed to FIRESTORE on the existing ``(account_id, date,
    sequence)`` composite, unlike :func:`list_transactions`, which fetches
    ``limit * 3`` rows and filters them in Python — for a period sheet that
    reads the whole account to print one month. The ``truncated`` flag is
    returned rather than swallowed: a register that stopped early without
    saying so would read as a complete one. Fails CLOSED (propagates).
    """
    query = db.collection(TRANSACTIONS_COLLECTION).where(
        filter=FieldFilter("account_id", "==", account_id)
    )
    df = _midnight_utc(date_from)
    dt = _midnight_utc(date_to)
    if df is not None:
        query = query.where(filter=FieldFilter("date", ">=", df))
    if dt is not None:
        # date is stored at midnight, so « <= end of day dt » is « < dt+1 »;
        # comparing against midnight(dt) directly would drop that whole day.
        query = query.where(
            filter=FieldFilter("date", "<", dt + timedelta(days=1))
        )
    query = query.order_by("date").order_by("sequence")
    rows = [d.to_dict() for d in query.limit(limit + 1).stream()]
    if len(rows) > limit:
        return rows[:limit], True
    return rows, False


def list_transactions_page(
    account_id: str, cursor: Optional[str] = None, limit: int = PAGE_SIZE
) -> tuple[list[dict], Optional[str]]:
    """Journal cursor pagination for one account, newest sequence first
    (index #1). ``sequence`` is a total order, so it is the only cursor key —
    no ``id`` tiebreaker (spec §11). Fails CLOSED: propagates on error."""
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


def list_card_transactions(
    dossier_id: str, client_id: str, date_from=None, date_to=None
) -> list[dict]:
    """Carte-client rows: one (dossier, client) couple, chronological ASC by
    sequence (index #3). Fails CLOSED."""
    return list_transactions(
        dossier_id=dossier_id, client_id=client_id,
        date_from=date_from, date_to=date_to, limit=1000,
    )


def list_dossier_transactions(dossier_id: str, limit: int = 10) -> list[dict]:
    """Recent entries across ALL clients of a dossier (the dossier tab). Merges
    per-client queries (index #3) — a dossier has few clients — over the union
    of current client_ids and any client that ever held funds here."""
    from models.dossier import get_dossier

    dossier = get_dossier(dossier_id)
    if not dossier:
        return []
    client_ids = set(dossier.get("client_ids") or [])
    client_ids |= set((dossier.get("trust_balance_by_client") or {}).keys())
    merged: list[dict] = []
    for cid in client_ids:
        merged.extend(list_card_transactions(dossier_id, cid))
    merged.sort(key=lambda t: t.get("sequence", 0), reverse=True)
    return merged[:limit]


def get_trust_summary(dossier_id: str) -> dict:
    """Per-dossier trust picture for the dossier tab + MCP. ``in_transit`` is
    ``book - cleared`` per client (deposits in transit; annulée pairs net out),
    so it needs no query."""
    from models.dossier import get_dossier  # lazy: avoid any import cycle

    dossier = get_dossier(dossier_id)
    if not dossier:
        return {"total_cents": 0, "by_client": [], "has_trust": False}
    book_map = dossier.get("trust_balance_by_client") or {}
    cleared_map = dossier.get("trust_cleared_by_client") or {}
    names = {c.get("id"): c.get("name", "") for c in dossier.get("clients", [])}
    by_client = []
    for cid in sorted(set(book_map) | set(cleared_map)):
        book = int(book_map.get(cid, 0))
        cleared = int(cleared_map.get(cid, 0))
        by_client.append({
            "client_id": cid,
            "client_name": names.get(cid, ""),
            "book_cents": book,
            "cleared_cents": cleared,
            "in_transit_cents": book - cleared,
        })
    return {
        "total_cents": int(dossier.get("trust_balance", 0)),
        "by_client": by_client,
        "has_trust": bool(by_client),
    }


# Grace after a month-end before its unreconciled state counts as overdue —
# the monthly-reconciliation heuristic (Règlement, comptabilité en
# fidéicommis). The predicate reasons on the latest month-end whose grace
# has EXPIRED, never merely on last month's.
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
    """True when some month-end whose grace has expired is unreconciled.

    The original predicate compared only against LAST month's end, behind a
    grace early-return. Two silent failure modes followed (PA-D06): a
    NEVER-reconciled account could read False on ~344 days of the year
    (the None branch sat below the early-return — and in February it was
    unreachable outright, Jan 31 + 30 d landing in March); and arrears older
    than one month were invisible (last reconciled Nov 30, asked on Feb 15:
    January was still in grace, so December's overdue reconciliation
    reported False).

    Now: ``due_through`` = the latest month-end at least
    RECONCILIATION_GRACE_DAYS in the past — the most recent period whose
    reconciliation is DUE. Overdue ⇔ the last completed reconciliation ends
    before it (or none exists). *account_floor* (account created_at)
    exempts an account younger than its first due month-end.
    """
    now = now or datetime.now(timezone.utc)
    due_through = _month_end_on_or_before(
        (now - timedelta(days=RECONCILIATION_GRACE_DAYS)).date()
    )
    if account_floor is not None and _as_utc(account_floor).date() > due_through:
        return False
    if last_period_end is None:
        return True
    return _as_utc(last_period_end).date() < due_through


def get_firm_trust_snapshot() -> dict:
    """Firm-wide trust picture for the dashboard + MCP. Totals are summed in
    Python over bounded lists — no SUM aggregation (spec §6.2).

    Reconciliation state is PER ACCOUNT (each account row gains
    last_reconciliation_date / never_reconciled / reconciliation_overdue) and
    the firm flag is their OR — a single reconciled account used to mask
    every never-reconciled sibling. The outstanding/in-transit rows, already
    fetched for the totals, are returned instead of discarded so stale
    cheques become visible (each row annotated with its account_id).
    """
    now = datetime.now(timezone.utc)
    accounts = [dict(a) for a in list_accounts()]
    total_held = sum(int(a.get("book_balance", 0)) for a in accounts)
    recs = list_reconciliations()
    completed = [r for r in recs if r.get("status") == "complétée"]

    outstanding_rows: list[dict] = []
    in_transit_count = in_transit_total = 0
    any_overdue = False
    for a in accounts:
        out = list_outstanding(a["id"])
        itr = list_in_transit(a["id"])
        for e in out:
            outstanding_rows.append({**e, "account_id": a["id"]})
        in_transit_count += len(itr)
        in_transit_total += sum(int(e.get("amount", 0)) for e in itr)

        mine = [
            _as_utc(r.get("period_end"))
            for r in completed
            if r.get("account_id") == a["id"]
        ]
        a_last = max(mine, default=None)
        a["last_reconciliation_date"] = a_last
        a["never_reconciled"] = a_last is None
        a["reconciliation_overdue"] = _reconciliation_overdue(
            a_last, now, account_floor=a.get("created_at")
        )
        any_overdue = any_overdue or a["reconciliation_overdue"]

    last_date = max(
        (_as_utc(r.get("period_end")) for r in completed), default=None
    )
    return {
        "accounts": accounts,
        "total_held_cents": total_held,
        "outstanding_count": len(outstanding_rows),
        "outstanding_total_cents": sum(
            int(e.get("amount", 0)) for e in outstanding_rows
        ),
        "outstanding_rows": outstanding_rows,
        "in_transit_count": in_transit_count,
        "in_transit_total_cents": in_transit_total,
        "last_reconciliation_date": last_date,
        "reconciliation_overdue": any_overdue,
        "reconciliation_never_performed": bool(accounts) and not completed,
    }


def list_dossiers_with_trust(limit: int = 200) -> list[dict]:
    """Dossiers currently or previously holding trust funds, for the firm
    snapshot's by_dossier view.

    Streams the dossiers collection (the web list view already does — no
    index, no inequality query: an inequality on trust_balance would miss
    legacy docs where the field is ABSENT and net-zero dossiers whose
    per-client maps are non-empty). A dossier qualifies when its
    trust_balance_by_client map has any entry. Fails OPEN to [] — this is a
    display aid, never the register.
    """
    rows: list[dict] = []
    try:
        for snap in db.collection("dossiers").stream():
            d = snap.to_dict() or {}
            by_client = d.get("trust_balance_by_client") or {}
            if not by_client:
                continue
            cleared = d.get("trust_cleared_by_client") or {}
            rows.append({
                "dossier_id": d.get("id", snap.id),
                "file_number": d.get("file_number", ""),
                "title": d.get("title", ""),
                "status": d.get("status", ""),
                "book_cents": int(d.get("trust_balance", 0)),
                "cleared_cents": sum(int(v) for v in cleared.values()),
            })
            if len(rows) >= limit:
                break
    except Exception as exc:
        logger.warning("list_dossiers_with_trust failed: %s", type(exc).__name__)
        return []
    rows.sort(key=lambda r: r.get("file_number", ""), reverse=True)
    return rows
