# SPEC — Phase K : Comptabilité en fidéicommis

Target: Claude Code. Read `CLAUDE.md` first — this spec assumes every convention in it and
calls out each deliberate divergence explicitly. Where this spec and `CLAUDE.md` conflict,
**this spec wins for the `trust_*` collections only**; everywhere else `CLAUDE.md` governs.

Status: design approved, not implemented.

---

## 1. Scope

Build the two registers required by the *Règlement sur la comptabilité et les normes
d'exercice professionnel des avocats* (RLRQ c. B-1, r. 5):

1. **Journal de caisse (recettes et déboursés)** — every movement on a trust account,
   all clients, chronological, running balance.
2. **Carte-client (grand livre auxiliaire)** — the same movements filtered to one
   beneficiary, with its own running balance.

Plus the machinery those registers require to be trustworthy: bank reconciliation,
reversal-only correction, and a hard overdraft control.

### 1.1 Core architectural decision

**The two registers are two views of one collection.** The journal is the unfiltered
chronological view; the carte-client is the same rows filtered by `(dossier_id, client_id)`
with a per-beneficiary running balance. There is exactly one write path and exactly one
source of truth.

Do **not** create per-dossier subcollections. Reasons, in order of weight:

- Some entries belong to no dossier (bank interest, bank fees, bank error corrections).
  A hierarchy under `dossiers/` has nowhere to put them, and they must appear in the journal.
- The register's sequence number must be continuous per account. Subcollections would need
  a global counter anyway, plus a `collection_group` index to reassemble order — the cost of
  both models with the benefit of neither.
- Bank reconciliation is per account, not per dossier. Outstanding cheques and deposits in
  transit cut across every dossier.
- Inter-dossier transfers become two linked writes in two containers instead of one
  `related_transaction_id`.

This is also Architecture Rule 2 (`CLAUDE.md`): Firestore collections are top-level.

### 1.2 Out of scope for Phase K

- **Comptes spéciaux (special accounts).** The schema carries `account_type` so the
  discriminant exists from day one, but only `général` is exercised. Do not build the UI.
- **Séquestre / escrow for two adverse parties.** "Le client pour qui" has no single
  answer; this belongs in a special account. Excluded.
- **Fonds d'études juridiques remittance workflow.** Interest is recorded as an entry
  (`purpose="intérêts"`, no dossier); computing and remitting it is not built.
- **Trust write access via MCP.** Read-only, as with every existing tool.

---

## 2. Binding divergences from `CLAUDE.md`

Claude Code will otherwise apply the default patterns. These four are deliberate.

### 2.1 No `update_*`, no `delete_*` — reversal only

An accounting register is never edited and never deleted. The model exports:

```python
def create_transaction(data: dict) -> tuple[Optional[dict], list[str]]
def get_transaction(tx_id: str) -> Optional[dict]
def list_transactions(...) -> list[dict]
def list_transactions_page(...) -> tuple[list[dict], Optional[str]]
def reverse_transaction(tx_id: str, reason: str) -> tuple[Optional[dict], list[str]]
def clear_transaction(tx_id: str, cleared_date: date) -> tuple[Optional[dict], list[str]]
def clear_transactions_bulk(tx_ids: list[str], cleared_date: date) -> tuple[int, list[str]]
# NO update_transaction. NO delete_transaction. No route may exist for either.
```

There is no `/fideicommis/<id>/edit`. There is no delete button. `create_transaction` is
append-only.

### 2.2 Narrow, write-once mutability window (§2.1 exception)

The two-step lifecycle (§4) requires one controlled mutation. **Exactly three fields are
mutable, all write-once:**

| Field | Transition | Set by |
|---|---|---|
| `status` | `en_circulation` → `compensée` \| `annulée` | `clear_transaction` / `reverse_transaction` |
| `cleared_date` | `None` → date | `clear_transaction` |
| `reconciliation_id` | `None` → id | `complete_reconciliation` |

Never date → other date. Never date → `None`. Never `compensée` → anything. A clearing
mistake is corrected by reversal, like any other.

Every other field is frozen at creation. `updated_at` **does** move on the clearing write
(it is a write); `created_at` never does. `etag` is regenerated on the clearing write per
Rule 7, but is vestigial here — trust collections are not DAV-exposed and no conditional
request reads it.

### 2.3 The overdraft control lives in a Firestore transaction, nowhere else

A negative client balance is a trust shortfall — the single most serious failure this
module can produce. It cannot live in a route, a form validator, or an optimistic check.
It lives inside the transaction, on the same read-set as the write. See §5.

### 2.4 The module fails CLOSED, everywhere

Every other module degrades gracefully to an empty list on a read error. This one does
not. Any read failure during balance verification aborts the write. Any list view that
cannot read returns an error state, never an empty register — an empty register that
should have rows is worse than an error message.

---

## 3. Firestore data model

Three new top-level collections, plus fields on `dossiers` and one counter.

### 3.1 `trust_accounts/{accountId}`

```python
{
    "name": str,                          # "Compte général en fidéicommis"
    "account_type": "général" | "spécial",
    "dossier_id": str | None,             # required iff account_type == "spécial"
    "client_id": str | None,              # required iff account_type == "spécial"

    "institution": str,                   # "Banque Nationale du Canada"
    "transit": str,                       # 5 digits
    "account_number_last4": str,          # LAST 4 ONLY — never store the full number

    # Denormalized, maintained transactionally (§5)
    "book_balance": int,                  # cents — every en_circulation + compensée entry
    "bank_balance": int,                  # cents — compensée entries only; the reconciliation anchor

    "opened_date": datetime,
    "closed_date": datetime | None,
    "status": "actif" | "fermé",
    "notes": str,
    # + id, created_at, updated_at, etag
}
```

**Never store the full account number.** Last 4 identify it for a human; the full number
is a payment credential.

`status="fermé"` requires `book_balance == 0`; enforce in `_validate`.

### 3.2 `trust_transactions/{transactionId}` — the register

Field names map 1:1 onto the Barreau column set. The mapping is normative (§8).

```python
{
    "account_id": str,
    "sequence": int,                      # continuous per account, never resets, never reused

    # ── Barreau columns ───────────────────────────────────────────────
    "date": datetime,                     # "Date" — DATE-ONLY, midnight UTC
    "dossier_file_number": str,           # "N/Réf" — "" when no dossier
    "counterparty": str,                  # "Somme reçue de / Bénéficiaire du débours"
    "client_name": str,                   # "Client pour qui la somme est reçue ou le
                                          #  débours est effectué" — snapshot, "" if none
    "purpose": str,                       # "Objet de la recette ou du débours" (vocab below)
    "method": str,                        # "Mode du retrait" (vocab below)
    "direction": "recette" | "déboursé",  # drives which of Recette/Crédit is populated
    "amount": int,                        # cents, ALWAYS POSITIVE — direction carries the sign
    "balance_after_account": int,         # "Solde" in the journal view — frozen at creation
    "balance_after_client": int,          # "Solde" in the carte-client view — frozen at creation
    # ──────────────────────────────────────────────────────────────────

    # Beneficiary — nullable (bank interest, bank fees)
    "dossier_id": str | None,
    "dossier_title": str,                 # snapshot
    "client_id": str | None,

    "reference": str,                     # cheque no., wire confirmation no.
    "description": str,                   # free text; also carries the reversal reason

    # Two-step lifecycle (§4)
    "status": "en_circulation" | "compensée" | "annulée",
    "cleared_date": datetime | None,      # DATE-ONLY, midnight UTC
    "reconciliation_id": str | None,

    # Links
    "invoice_id": str | None,             # required iff purpose == "virement_honoraires"
    "reverses_id": str | None,            # this entry reverses that one
    "reversed_by_id": str | None,         # that entry reverses this one
    "related_transaction_id": str | None, # the other leg of an inter-dossier transfer
    # + id, created_at, updated_at, etag
}
```

**`purpose` vocabulary** (`VALID_PURPOSES`, French, matches the Barreau column):

`avance_honoraires` · `dépôt_client` · `règlement` · `virement_honoraires` ·
`remise_client` · `déboursé_tiers` · `virement_inter_dossiers` · `intérêts` ·
`frais_bancaires` · `correction` · `autre`

**`method` vocabulary** (`VALID_METHODS`):

`chèque` · `virement` · `traite` · `dépôt_direct` · `comptant`

`purpose="correction"` is reserved for entries created by `reverse_transaction`. Reject it
at the create route.

#### Why two frozen balances

The "Solde" column has two values depending on the view. Both are computed inside the
transaction — which already reads both balances — and frozen on the row. Exports become
direct projections: no recomputation, no possible divergence between screen, CSV and PDF.
A verification script (§13) recomputes both from the sequence and compares.

#### `counterparty` is text, never a FK

Do not resolve it to `parties`. Two reasons:

1. Most counterparties are not parties — the bank, the greffe, the SAAQ, an insurer, a
   one-off supplier. A FK would pollute the contact book with non-contacts.
2. A FK breaks register immutability. Fixing a typo in a party's name would silently
   rewrite every historical row. The register must show what was on the cheque when it was
   written.

Same reasoning as the existing `invoices.billing_address` snapshot and the denormalized
`dossier_title` fields. `client_name` follows it: snapshot at creation, `client_id` kept
for filtering.

The form may autocomplete from the dossier's parties. What is stored is the string.

### 3.3 `trust_reconciliations/{reconciliationId}`

```python
{
    "account_id": str,
    "period_end": datetime,               # DATE-ONLY, last day of the month
    "statement_balance": int,             # cents, from the bank statement
    "book_balance": int,                  # cents, snapshot at completion
    "outstanding_cheques_total": int,     # cents, positive
    "deposits_in_transit_total": int,     # cents, positive
    "variance": int,                      # cents — MUST be 0 to complete
    "status": "brouillon" | "complétée",
    "completed_date": datetime | None,
    "cleared_transaction_ids": list[str], # entries this reconciliation stamped compensée
    "notes": str,
    # + id, created_at, updated_at, etag
}
```

**Variance formula** (implement as a pure function, unit-tested):

```
variance = statement_balance
         + deposits_in_transit_total
         - outstanding_cheques_total
         - book_balance
```

`complete_reconciliation` refuses unless `variance == 0`. Refusal emits
`log_trust_event("reconciliation_variance", ...)` at WARNING.

Only one `brouillon` per account at a time. `period_end` must be after the last
`complétée` reconciliation's `period_end`.

### 3.4 Fields added to `dossiers/{dossierId}`

```python
"trust_balance": int,                     # cents — book, all clients on the dossier
"trust_balance_by_client": {str: int},    # client_id → cents, BOOK balance
"trust_cleared_by_client": {str: int},    # client_id → cents, CLEARED balance (the control)
```

Absent on legacy documents. `_migrate_parties` in `models/dossier.py` already runs on
read — extend it (or add `_migrate_trust`) to default all three to `0` / `{}` so callers
never see `None`. Do not backfill; a dossier with no trust entries has a zero balance by
construction.

### 3.5 Counter

`counters/trust-{account_id}` with a `seq` field. Same transactional mechanic as
`counters/invoices-{year}` in `models/invoice.py`: read inside the transaction, increment,
write. **Never resets** — `sequence` is a total order over the account's life, forever.
Allocation failure aborts the write; there is no fallback number.

---

## 4. Balance semantics and the two-step lifecycle

This section is the heart of the module. Read it twice.

### 4.1 The lifecycle

Every entry is recorded **when it is made** (cheque written, funds received), not when it
reaches the bank. It then has two states:

```
create_transaction                clear_transaction
       │                                 │
       ▼                                 ▼
 en_circulation ──────────────────► compensée      (normal path)
       │
       └──────────────────────────► annulée        (reverse_transaction on an
                                                     uncleared entry — no bank
                                                     movement will ever occur)
```

`compensée` is terminal. `annulée` is terminal. There is no path back.

- **`en_circulation`** — recorded, not yet on a bank statement. A disbursement is an
  outstanding cheque; a receipt is a deposit in transit.
- **`compensée`** — appeared on a bank statement. `cleared_date` set, `reconciliation_id`
  set if cleared through a reconciliation.
- **`annulée`** — resolved without ever touching the bank (cheque cancelled or
  stale-dated, keying error caught before deposit). Reachable **only** from
  `en_circulation`, **only** via `reverse_transaction`, which stamps both the original and
  its reversal `annulée` in the same transaction. Excluded from outstanding-cheque and
  deposit-in-transit lists so it never pollutes a reconciliation.

### 4.2 The three balances

| Balance | Definition | Stored on | Used for |
|---|---|---|---|
| **book** | Σ(receipts) − Σ(disbursements), **all statuses including `annulée`** | `dossiers.trust_balance_by_client`, `trust_accounts.book_balance` | The "Solde" column. What the register shows. |
| **cleared** | Σ(receipts WHERE `compensée`) − Σ(disbursements WHERE `compensée` or `en_circulation`) | `dossiers.trust_cleared_by_client` | **The control.** Never displayed as "Solde". |
| **bank** | Σ(receipts WHERE `compensée`) − Σ(disbursements WHERE `compensée`) | `trust_accounts.bank_balance` | Reconciliation anchor; must equal the statement. |

Three consequences, each of which Claude Code will get wrong if not stated:

**Book includes `annulée`.** An `annulée` entry and its reversal net to zero, so including
them is arithmetically free — and *required*, because the register is chronological. If
entry #5 is cancelled and reversed at #12, entries #5–#11 must still show the balance as
it stood at the time. That is what the register showed then; that is what it must show
now. The running balance is history, not a current-state projection.

**Cleared excludes `annulée` entirely.** An `en_circulation` disbursement subtracts from
`cleared` immediately (the money is committed). Annulling it must add that back. Its
`annulée` reversal must contribute nothing, or the funds would be double-counted.

**Cleared is asymmetric on purpose.** Receipts count only once cleared; disbursements
count the moment they are written. Conservative on both sides.

### 4.3 The control: `cleared_balance_client >= 0`

**A disbursement may only draw against cleared funds.** Formally, after applying the
entry:

```
cleared_balance_client(dossier_id, client_id) >= 0
```

This is the only balance control. It subsumes the book control:
`cleared ≤ book` always, so `cleared_after ≥ 0` ⟹ `book_after ≥ 0`.

**Why the control is at `(dossier, client)` and not at `client`.** The reasoning is
asymmetric: if every `(dossier, client)` couple is ≥ 0, every client is necessarily ≥ 0.
The converse is false — a client at +10 000 $ overall can hide a dossier at −1 000 $. The
fine-grained control satisfies both readings of the Règlement; the coarse one satisfies
only one. In substance: a settlement received in dossier A belongs to the client, but
applying it to fees in dossier B is not automatic — it requires the client's instruction.
A system that silently lets B draw on A erases from the books a decision that must be
documented. The release valve is the inter-dossier transfer (§6.4), which makes the
movement visible, dated, and attachable to the authorization.

**Client scope.** `client_id` must appear in the referenced dossier's `client_ids` array.
You do not hold funds in trust for an opposing party, a witness, or an expert. Enforce in
`_validate`; reject otherwise.

**Mandataires need no special handling.** Funds held for a minor represented by a tutor:
`client_id` is the minor (the holder); the tutor appears in `counterparty` as text. The
`mandataires[]` list on the partie already documents the relationship.

> ### ⚠️ Verify before implementing
>
> The cleared-funds rule (a disbursement may not draw on an uncleared deposit) is standard
> trust accounting and guards the classic failure: the client's cheque bounces after you
> have disbursed, and other clients' money covers the gap. **I have not verified the exact
> provision in RLRQ c. B-1, r. 5, and have not invented one.**
>
> The default here is conservative because the cost is asymmetric: relaxing the control
> later is deleting one guard clause in `_check_disbursement_allowed`; adding it later
> means backfilling cleared balances across a register that is supposed to be frozen.
>
> The friction is real and should be understood before accepting it: you cannot pay a
> bailiff the same day the client's cheque arrives. If the Règlement does not require it,
> remove the `cleared_after < 0` branch and keep `book_after < 0` — nothing else changes.

### 4.4 Balance deltas per operation

Implement as a pure function; unit-test the table.

| Operation | book | cleared | bank |
|---|---|---|---|
| Create receipt X | +X | 0 | 0 |
| Create disbursement X | −X | **−X** | 0 |
| Clear receipt X | 0 | **+X** | +X |
| Clear disbursement X | 0 | 0 | −X |
| Annul `en_circulation` receipt X (pair) | 0 (nets) | 0 | 0 |
| Annul `en_circulation` disbursement X (pair) | 0 (nets) | **+X** | 0 |
| Reverse `compensée` receipt X | −X | −X | 0 (until cleared) |
| Reverse `compensée` disbursement X | +X | 0 | 0 (until cleared) |

Sanity check on the annul-disbursement row: `create` took `cleared −X`. Annulling excludes
the entry from `cleared`, so `+X` restores it; the `annulée` reversal receipt contributes
`0` because `annulée` receipts are excluded. Net effect on `cleared`: `0`. ✓

Reversing a `compensée` receipt produces a disbursement that starts `en_circulation` —
so it takes `cleared −X` immediately, per the create row. This is correct: a bounced
deposit removes the funds the moment you know about it.

---

## 5. `create_transaction` — the Firestore transaction

Model on `invoice.create_invoice`, which is the existing transactional reference. Nothing
may be read outside the transaction and reused inside it.

```
@firestore.transactional
def _create_txn(transaction, data):

    #  1. READS (all inside the transaction)
    account  = read trust_accounts/{account_id}          # → book_balance, bank_balance, status
    counter  = read counters/trust-{account_id}          # → seq
    last_tx  = read most recent entry for this account   # → last date (backdating guard)
    dossier  = read dossiers/{dossier_id}   if dossier_id  # → the two balance maps, client_ids
    invoice  = read invoices/{invoice_id}   if invoice_id

    #  2. GUARDS — every failure ABORTS. No partial write, ever.
    account.status == "actif"                       else abort "compte_fermé"
    amount > 0                                      else abort "montant_invalide"
    direction in ("recette", "déboursé")            else abort "direction_invalide"
    purpose in VALID_PURPOSES and != "correction"   else abort "objet_invalide"
    method in VALID_METHODS                         else abort "mode_invalide"
    counterparty non-empty                          else abort "contrepartie_requise"

    # Beneficiary coherence
    (dossier_id is None) == (client_id is None)     else abort "bénéficiaire_incohérent"
    if dossier_id:
        dossier exists                              else abort "dossier_introuvable"
        client_id in dossier.client_ids             else abort "client_hors_dossier"
    else:
        purpose in ("intérêts","frais_bancaires","correction")
                                                    else abort "objet_sans_dossier_invalide"

    # No backdating — sequence order must match date order (§4.2)
    date >= last_tx.date                            else abort "antidatage_refusé"

    # Fee transfer
    if purpose == "virement_honoraires":
        direction == "déboursé"                     else abort "virement_direction"
        invoice exists                              else abort "facture_introuvable"
        invoice.status in ("envoyée","en_retard")   else abort "facture_non_émise"
        invoice.dossier_id == dossier_id            else abort "facture_autre_dossier"
        amount <= invoice.amount_due                else abort "virement_excède_facture"

    #  3. THE OVERDRAFT CONTROL (§4.3)
    if direction == "déboursé":
        cleared_after = dossier.trust_cleared_by_client.get(client_id, 0) - amount
        if cleared_after < 0:
            log_trust_event("overdraft_refused", outcome="refused",
                            reason="insufficient_cleared_balance",
                            dossier_id=..., account_id=...)      # NO amounts, NO names
            abort "solde_compensé_insuffisant"

    #  4. COMPUTE
    seq                   = counter.seq + 1
    book_after_account    = account.book_balance    ± amount
    book_after_client     = dossier.trust_balance_by_client.get(client_id, 0) ± amount
    cleared_after_client  = dossier.trust_cleared_by_client.get(client_id, 0)
                            - (amount if direction == "déboursé" else 0)

    #  5. WRITES (single commit)
    write trust_transactions/{uuid4}   status="en_circulation", cleared_date=None,
                                       sequence=seq,
                                       balance_after_account=book_after_account,
                                       balance_after_client=book_after_client,
                                       dossier_file_number / dossier_title / client_name
                                            = snapshots read above
    write counters/trust-{account_id}  seq = seq
    write trust_accounts/{account_id}  book_balance = book_after_account
    write dossiers/{dossier_id}        trust_balance_by_client[client_id]  = book_after_client
                                       trust_cleared_by_client[client_id]  = cleared_after_client
                                       trust_balance = Σ(trust_balance_by_client.values())
```

Notes:

- **`bank_balance` is untouched here.** It moves only in `clear_transaction`.
- **The backdating guard** exists because a frozen running balance is meaningless without a
  stable order. `sequence` is authoritative; `date` must be monotonic along it. An error
  found in July is corrected by a reversal dated July, not by rewriting June.
- **Reading `last_tx` inside the transaction** costs one indexed query
  (`account_id` + `sequence DESC`, limit 1). Acceptable — this is not a hot path.
- **On any Firestore read error: abort.** Do not degrade (§2.4).

### 5.1 `clear_transaction(tx_id, cleared_date)`

Transactional:

```
READ transaction, account, dossier (if any)
GUARDS: status == "en_circulation"       else abort "déjà_compensée" / "annulée"
        cleared_date >= transaction.date else abort "compensation_antérieure"
        cleared_date <= today            else abort "compensation_future"
WRITE:  transaction.status = "compensée"; cleared_date; updated_at; etag
        account.bank_balance ± amount                       (per §4.4)
        if direction == "recette" and dossier:
            dossier.trust_cleared_by_client[client_id] += amount
```

`clear_transactions_bulk` applies the same guards per entry inside one transaction; if any
entry fails, **the whole batch aborts** and the failing ids are returned. Partial clearing
of a reconciliation batch is worse than none.

### 5.2 `reverse_transaction(tx_id, reason)`

Transactional. Creates a new entry; never edits the original's amount or direction.

```
READ original, account, dossier, counter
GUARDS: original.reversed_by_id is None  else abort "déjà_contrepassée"
        reason non-empty                 else abort "motif_requis"

reversal = new entry:
    direction   = opposite of original
    amount      = original.amount
    purpose     = "correction"
    counterparty, dossier_id, client_id, dossier_file_number,
    dossier_title, client_name, method  = copied from original
    reverses_id = original.id
    description = reason
    date        = today            # NOT the original's date — no backdating (§5)
    sequence    = next

if original.status == "en_circulation":
    # Neither entry will ever appear on a statement.
    original.status = "annulée"
    reversal.status = "annulée"
    reversal.cleared_date = None
    # cleared adjustment (§4.4): if original was a disbursement, cleared += amount
else:  # compensée
    reversal.status = "en_circulation"
    # normal create deltas apply to the reversal

original.reversed_by_id = reversal.id
recompute book / cleared / balances_after for the reversal exactly as in create
```

**The overdraft control does not apply to a reversal.** It is a correction of a movement
that already happened; blocking it would trap the register in an inconsistent state. This
is the one documented exemption, and it is why `purpose="correction"` is refused at the
create route: reversal is the only way to produce one.

---

## 6. Model layer — `models/trust.py`

One module. Follows the `_normalize` → `_sanitize_data` → `_validate` pipeline.

### 6.1 Pure functions (no Firestore — these carry the test suite)

Mirror `invoice.compute_totals`: pure, module-level, importable without the client.

```python
def compute_deltas(direction: str, amount: int, status: str) -> dict
    # → {"book": int, "cleared": int, "bank": int} — the §4.4 table

def check_disbursement_allowed(cleared_balance: int, amount: int) -> tuple[bool, str]
    # → (False, "solde_compensé_insuffisant") when cleared_balance - amount < 0

def reconciliation_variance(statement_balance: int, book_balance: int,
                            outstanding_cheques: int, deposits_in_transit: int) -> int

def to_barreau_row(tx: dict, view: "journal" | "carte") -> dict
    # → the 8 Barreau columns in order (§8). `view` picks which frozen balance
    #   lands in "Solde". Single source for CSV, PDF and the HTML table.

def recompute_running_balances(txs: list[dict], view: str) -> list[int]
    # Verification only (§13). Never used to render.
```

### 6.2 Firestore functions

```python
def create_account(data) -> tuple[Optional[dict], list[str]]
def get_account(account_id) -> Optional[dict]
def list_accounts(status=None) -> list[dict]
def update_account(account_id, data) -> tuple[Optional[dict], list[str]]   # metadata only,
                                                                          # never balances
def create_transaction(data) -> tuple[Optional[dict], list[str]]
def get_transaction(tx_id) -> Optional[dict]
def list_transactions(account_id=None, dossier_id=None, client_id=None,
                      date_from=None, date_to=None, status=None,
                      direction=None, limit=200) -> list[dict]
def list_transactions_page(account_id, cursor=None, limit=PAGE_SIZE) -> tuple[list, Optional[str]]
def list_card_transactions(dossier_id, client_id,
                           date_from=None, date_to=None) -> list[dict]
def reverse_transaction(tx_id, reason) -> tuple[Optional[dict], list[str]]
def clear_transaction(tx_id, cleared_date) -> tuple[Optional[dict], list[str]]
def clear_transactions_bulk(tx_ids, cleared_date) -> tuple[int, list[str]]

def list_outstanding(account_id, as_of=None) -> list[dict]      # déboursé + en_circulation
def list_in_transit(account_id, as_of=None) -> list[dict]       # recette + en_circulation

def create_reconciliation(account_id, period_end, statement_balance) -> tuple[...]
def get_reconciliation(rec_id) -> Optional[dict]
def list_reconciliations(account_id=None) -> list[dict]
def complete_reconciliation(rec_id, cleared_tx_ids) -> tuple[Optional[dict], list[str]]

def get_trust_summary(dossier_id) -> dict
    # → {"total_cents", "by_client": [{"client_id","client_name",
    #     "book_cents","cleared_cents","in_transit_cents"}], "has_trust": bool}
def get_firm_trust_snapshot() -> dict
    # → {"accounts":[…], "total_held_cents", "outstanding_count",
    #     "last_reconciliation_date", "reconciliation_overdue": bool}
```

**No SUM aggregations anywhere.** `list_outstanding` / `list_in_transit` return bounded
lists (a handful of cheques) and the totals are summed in Python. This is deliberate: it
sidesteps the June 2026 index trap entirely (`CLAUDE.md`, Known Gotchas — an index that
serves a paginated list does **not** serve its SUM aggregation; the query 400s and the
total silently degrades to zero). A trust total that silently reads zero is not an
acceptable failure mode. If a future SUM becomes necessary, it needs its own index whose
trailing fields are the aggregated fields in alphabetical order with directions matching
the query's last sort.

### 6.3 `delete_dossier` interaction

`models/dossier.delete_dossier` already refuses while child records exist and fails CLOSED.
**Add trust transactions to that check**, and make the trust condition stricter: a dossier
that has *ever* had a trust entry can never be deleted, even at a zero balance — the
register is permanent. Archive it.

### 6.4 Inter-dossier transfer

`create_inter_dossier_transfer(from_dossier_id, from_client_id, to_dossier_id,
to_client_id, amount, description, method, reference) -> tuple[Optional[dict], list[str]]`

One transaction, two entries:

- Leg A: `direction="déboursé"`, `purpose="virement_inter_dossiers"`, on the source couple
  — **the overdraft control applies**.
- Leg B: `direction="recette"`, `purpose="virement_inter_dossiers"`, on the destination
  couple.
- Same `date`, same `amount`, consecutive `sequence`, mutually linked via
  `related_transaction_id`.
- Both are created `compensée` with `cleared_date = date` — the funds never leave the
  account, so there is no bank movement to wait for and nothing to reconcile. This is the
  only path that creates a `compensée` entry directly; document it at the call site.
- `counterparty` on both legs: the client name of the *other* leg.

Refuse when `(from_dossier_id, from_client_id) == (to_dossier_id, to_client_id)`.

---

## 7. Routes — `routes/trust.py`, prefix `/fideicommis`

All `@login_required`. French UI. HTMX for dynamic fragments. No route exceeds the default
1 MB request cap — **no `security.py` exemption is needed**.

| Route | Method | Purpose |
|---|---|---|
| `/fideicommis/` | GET | **Journal de caisse** — cursor pagination, filters: compte, période, statut, direction. Header: solde aux livres, solde bancaire, chèques en circulation, dépôts en transit, date de la dernière conciliation (badge « Conciliation en retard » past month-end + 30 d) |
| `/fideicommis/nouvelle` | GET | Entry form |
| `/fideicommis/` | POST | Create → detail. Refuses `purpose="correction"` |
| `/fideicommis/<id>` | GET | Detail: all fields, both frozen balances, status, links to the reversal pair / other transfer leg / invoice |
| `/fideicommis/<id>/compenser` | POST | Step 2 — single clear (`cleared_date` in the form) |
| `/fideicommis/compenser-lot` | POST | Bulk clear (checkboxes, from the reconciliation screen) |
| `/fideicommis/<id>/contrepasser` | GET | Confirmation dialog, mandatory `reason` field |
| `/fideicommis/<id>/contrepasser` | POST | Reversal |
| `/fideicommis/virement` | GET/POST | Inter-dossier transfer form / submit |
| `/fideicommis/carte/<dossier_id>/<client_id>` | GET | **Carte-client** — chronological ASC, running balance from `balance_after_client`. Header: solde aux livres + solde compensé + dépôts en transit, with the difference explained in plain French |
| `/fideicommis/client/<client_id>` | GET | Consolidated read-only view across dossiers. **Not a register** — no control attaches, no export, labelled « Vue de gestion » |
| `/fideicommis/comptes/` | GET | Account list |
| `/fideicommis/comptes/nouveau` | GET/POST | Create |
| `/fideicommis/comptes/<id>` | GET | Detail |
| `/fideicommis/comptes/<id>/edit` | GET/POST | Metadata only — balances are not editable and no form field may target them |
| `/fideicommis/conciliations/` | GET | Reconciliation list |
| `/fideicommis/conciliations/nouvelle` | GET/POST | Start: pick account + period_end + statement_balance |
| `/fideicommis/conciliations/<id>` | GET | Worksheet: outstanding cheques + deposits in transit with checkboxes, live variance, « Compléter » disabled until variance = 0 |
| `/fideicommis/conciliations/<id>/completer` | POST | Complete (refuses if variance ≠ 0) |
| `/fideicommis/export/csv` \| `/pdf` | GET | Journal export (§8) — honours the current filters |
| `/fideicommis/carte/<dossier_id>/<client_id>/export/csv` \| `/pdf` | GET | Card export (§8) |
| `/fideicommis/dossier-search` | GET | HTMX autocomplete — same pattern as `tasks.dossier-search` |
| `/fideicommis/client-search` | GET | HTMX — **clients of the selected dossier only** (`?dossier_id=`), reflecting the §4.3 scope rule |
| `/fideicommis/counterparty-suggest` | GET | HTMX — suggests the dossier's parties as *text*; selection fills the input, stores no id |

### 7.1 Dossier tab

Add `fideicommis` to the `/dossiers/<id>/tab/<tab_name>` loader. Current tabs:
`apercu`, `temps`, `facturation`, `audiences`, `taches`, `protocole`, `documents`.

Content: one card per client on the dossier (solde aux livres, solde compensé, dépôts en
transit), a link to each carte-client, the last 10 entries, and « Nouvelle écriture »
(dossier locked). Only render the tab when `dossier.trust_balance != 0` or entries exist —
otherwise the empty state invites the first entry.

### 7.2 Dashboard

Add to the quick-stats row: **« Sommes en fidéicommis »** (total held) and, when a
reconciliation is more than 30 days past a month-end, a warning line in the alerts
section next to the prescription alerts.

---

## 8. Exports — CSV (option A) and PDF (option C)

**Column order is normative.** Both registers use the same eight columns; only the "Solde"
source and the row filter differ. `to_barreau_row` (§6.1) is the single mapping — HTML,
CSV and PDF all consume it.

| # | Header (exact string) | Source |
|---|---|---|
| 1 | `Date` | `date` → `%Y-%m-%d` |
| 2 | `N/Réf` | `dossier_file_number` (empty when no dossier) |
| 3 | `Somme reçue de / Bénéficiaire du débours` | `counterparty` |
| 4 | `Client pour qui la somme est reçue ou le débours est effectué` | `client_name` |
| 5 | `Objet de la recette ou du débours` | `purpose` → French label |
| 6 | `Mode du retrait` | `method` → French label |
| 7 | `Recette / Crédit` | signed: `amount/100` if `recette`, `-amount/100` if `déboursé` |
| 8 | `Solde` | `balance_after_account/100` (journal) \| `balance_after_client/100` (carte) |

Column 7 note: the Barreau's sheet titles one column "Recette / Crédit". Render it as a
single signed amount — negative for a `déboursé`. If the physical sheet turns out to have
two separate columns, `to_barreau_row` is the only place to change (verify — §14).

### 8.1 CSV

Use `utils/export_csv.py` as-is. UTF-8 BOM is already handled. `cents_fields=["recette",
"solde"]` divides by 100 with two decimals. No new dependency.

### 8.2 PDF

Use `utils/export_pdf.py` (reportlab). **Landscape** — eight columns, two of them long.
Suggested width ratios: `[8, 10, 20, 20, 14, 10, 9, 9]`.

Header block above the table (both registers):

```
JOURNAL DE CAISSE — RECETTES ET DÉBOURSÉS        (or: CARTE-CLIENT)
{FIRM_NAME}
Compte : {account.name} — {institution} (…{last4})
Période : {date_from} au {date_to}
Client : {client_name} — Dossier : {file_number}   ← carte only
Solde au début : X XXX,XX $      Solde à la fin : X XXX,XX $
Généré le {date} — non concilié après le {last_reconciliation.period_end}
```

Footer: `Page N de M`. Do **not** stamp any signature or attestation.

### 8.3 Marking uncleared entries

`en_circulation` rows carry a marker in both exports (asterisk on the date + a legend:
`* En circulation au {date} — non compensé`). A register whose rows are all presented as
final when some have not cleared misrepresents the account. `annulée` rows carry `(annulée)`
appended to column 5.

---

## 9. MCP tools (Phase I integration)

Three new **read-only** tools. Total goes 14 → 17. Register in `mcp/tools.py`, implement in
`mcp/handlers.py`, composing `models/trust.py` only.

### 9.1 `get_trust_balance`

```
input:  {"dossier_id": str}              # required
output: {"dossier_id", "file_number", "title",
         "total_cents", "total_display",
         "by_client": [{"client_id", "client_name",
                        "book_cents", "book_display",
                        "cleared_cents", "cleared_display",
                        "in_transit_cents", "in_transit_display"}],
         "has_trust": bool}
```

### 9.2 `list_trust_transactions`

```
input:  {"account_id"?: str, "dossier_id"?: str, "client_id"?: str,
         "date_from"?: "YYYY-MM-DD", "date_to"?: "YYYY-MM-DD",
         "status"?: "en_circulation"|"compensée"|"annulée",
         "limit"?: int (≤50)}
output: {"transactions": [{"id", "sequence", "date", "file_number",
                           "counterparty", "client_name", "purpose", "method",
                           "direction", "amount_cents", "amount_display",
                           "balance_after_account_cents", "balance_after_client_cents",
                           "status", "cleared_date", "reversed": bool}],
         "count", "truncated": bool}
```

`dossier_id` + `client_id` together ⇒ the carte-client. Neither ⇒ the journal.

### 9.3 `get_trust_snapshot`

```
input:  {}
output: {"accounts": [{"id","name","institution","account_type",
                       "book_balance_cents","book_balance_display",
                       "bank_balance_cents","bank_balance_display"}],
         "total_held_cents", "total_held_display",
         "outstanding_count", "outstanding_total_cents",
         "in_transit_count", "in_transit_total_cents",
         "last_reconciliation_date", "reconciliation_overdue": bool}
```

Mirrors `get_billing_snapshot`.

### 9.4 Binding MCP constraints

- **Read-only.** No handler may reach `create_*`, `clear_*`, `reverse_*`, or any write.
  `list_protocol_steps` is the precedent — it derives overdue status by comparison rather
  than calling the writing `check_overdue_steps`.
- **`date` and `cleared_date` are date-only** (midnight UTC). Emit via `mcp.tools.date_str`.
  **Never `iso_mtl`, never `to_mtl`** — a Montréal conversion shifts them to the previous
  day (`CLAUDE.md`, Known Gotchas). This is the single most likely bug in this section.
- Money as `*_cents` + fr-CA `*_display`, per the existing convention.
- Lists capped at 50 with `truncated`.
- **Never emit `account_number_last4`, `transit`, or `institution`** in
  `list_trust_transactions`. `get_trust_snapshot` may emit the account name and institution;
  never the transit or last4.
- **No MCP-only composite index.** Reuse the §11 indexes; apply anything else in the handler
  over a bounded fetch (≤200 docs).
- The `MCP_ENABLED` kill switch already covers these routes — no extra wiring.

### 9.5 Consent screen

The scope stays `athena:read` — do **not** add a second scope in this phase.

**But update `templates/mcp/consent.html`** to name trust data explicitly in the French
consent text. Granting a connector access to trust balances is a materially different
disclosure from granting access to dossiers, and the consent screen is where that is
disclosed. A separate `athena:trust` scope (grantable and revocable independently) is a
reasonable later refinement; it is out of scope here.

Reuse existing class strings — `athena/mcp/` and `templates/mcp/` are covered by the
`@source "../../templates"` scan, but a genuinely new utility class still forces the full
recompile-and-rehash procedure. Primary buttons are `bg-gray-900`, not `bg-indigo-600`.

---

## 10. Templates — `templates/trust/`

```
templates/trust/
├── list.html                # journal de caisse
├── _transaction_rows.html   # HTMX rows + pagination
├── detail.html
├── form.html                # new entry
├── transfer_form.html       # inter-dossier
├── card.html                # carte-client
├── _card_rows.html
├── client_consolidated.html # « Vue de gestion » — not a register
├── accounts_list.html
├── account_form.html
├── account_detail.html
├── reconciliations_list.html
├── reconciliation_form.html
├── reconciliation_worksheet.html
└── _reconcile_rows.html     # checkboxes + live variance
```

Plus `templates/dossiers/_tab_fideicommis.html`.

**CSS rule (hard).** Reuse class strings that already exist in the compiled
`static/vendor/app.<hash>.css`. Any genuinely new utility class forces the full recompile,
re-hash, and updates to `base.html`, `auth/login.html`, the `PRECACHE` list in
`static/sw.js`, and the `_EARLY_HINTS_*` lists in `security.py` — with the old hashed file
deleted. Never edit a vendored asset in place. Classes must be complete string literals in
templates / `routes/*.py` / `models/*.py`, or safelisted in `app.input.css` — dynamically
assembled names are purged at compile time.

Mobile-first (375 px), 44 px touch targets, `#FAFAFA` / `gray-900` / `bg-gray-900`
buttons. Script order at the end of `<body>` is load-bearing under Rocket Loader — do not
touch it.

### 10.1 Two visual rules that carry meaning

**Never present the cleared balance as "Solde".** The register's Solde is the book balance.
The cleared balance is a control. Where both appear (card header, entry form), label them:

```
Solde aux livres : 5 000,00 $
Disponible (compensé) : 0,00 $ · 5 000,00 $ en dépôt de transit
```

The gap between the two is the whole point of the two-step model. Hiding it invites the
user to believe the control is broken.

**The form must explain a refusal.** When `create_transaction` refuses on
`solde_compensé_insuffisant`, do not surface a generic error. Show the cleared balance, the
requested amount, and the in-transit entries with their dates — the user needs to know
*when* the funds become available.

---

## 11. Firestore composite indexes

Add to `firestore.indexes.json`. **Deploy before the code ships** — until an index builds,
the query fails (`firebase deploy --only firestore:indexes --project athena-pallas`).

| # | Collection | Fields | Serves |
|---|---|---|---|
| 1 | `trust_transactions` | `account_id` ASC, `sequence` DESC | Journal, cursor pagination |
| 2 | `trust_transactions` | `account_id` ASC, `sequence` ASC | Last-entry read in the transaction (limit 1, DESC via #1 — verify one direction suffices) |
| 3 | `trust_transactions` | `dossier_id` ASC, `client_id` ASC, `sequence` ASC | Carte-client |
| 4 | `trust_transactions` | `account_id` ASC, `status` ASC, `direction` ASC, `sequence` ASC | Outstanding cheques / deposits in transit |
| 5 | `trust_transactions` | `account_id` ASC, `date` ASC, `sequence` ASC | Journal export by period |
| 6 | `trust_transactions` | `dossier_id` ASC, `client_id` ASC, `date` ASC, `sequence` ASC | Card export by period |
| 7 | `trust_transactions` | `invoice_id` ASC, `sequence` ASC | Entries linked to an invoice |
| 8 | `trust_reconciliations` | `account_id` ASC, `period_end` DESC | Reconciliation list |

**`sequence` is the tiebreaker, not `id`.** It is already a total order per account, so the
`id` tiebreaker the other list views use is unnecessary here. Cursor pagination encodes
`sequence`.

Index #5/#6 note: Firestore requires the range field (`date`) to lead the `order_by`. Order
by `date` then `sequence`.

**No SUM aggregation index** — see §6.2 for why.

---

## 12. Observability

Add to `utils/logging_setup.py` and document in `athena/OBSERVABILITY.md` (that file is the
registry — extend it, do not bypass it).

```python
log_trust_event(event, outcome="success", *, transaction_id=None, dossier_id=None,
                account_id=None, reconciliation_id=None, reason=None, **extra)
# logger: pallas.trust
```

| `event` | Outcome | Severity | Fields |
|---|---|---|---|
| `trust_transaction_created` | success | INFO | `transaction_id`, `direction`, `purpose`, `sequence` |
| `trust_transaction_cleared` | success | INFO | `transaction_id` |
| `trust_transaction_reversed` | success | INFO | `transaction_id`, `reverses_id`, `annulled: bool` |
| `trust_overdraft_refused` | refused | **WARNING** | `dossier_id`, `account_id`, `reason` = `insufficient_cleared_balance` |
| `trust_transaction_refused` | refused | WARNING | `reason` — machine-stable, from the §5 abort strings |
| `trust_reconciliation_completed` | success | INFO | `reconciliation_id`, `account_id`, `cleared_count` |
| `trust_reconciliation_variance` | refused | WARNING | `reconciliation_id`, `variance_cents` |
| `trust_export` | success | INFO | `format`, `view`, `row_count` |

**Never log amounts, counterparty names, or client names.** Cloud Logging is not the audit
trail — Firestore is. IDs, directions, purposes and counts only. `variance_cents` is the
single exception: it is a control failure with no client attached, and it is useless
without the number. The `RedactionFilter` is a backstop, not the policy.

Spans (`utils/tracing_setup.py`):

| Span | Attributes |
|---|---|
| `trust.transaction` | `direction`, `purpose`, `dossier_id` — **no amounts** |
| `trust.reconcile` | `account_id`, `cleared_count` |
| `mcp.tool.get_trust_balance` etc. | per the existing `mcp.tool.*` convention |

---

## 13. Tests — `athena/tests/test_trust.py`

The pure functions in §6.1 carry the suite; no Firestore needed. Non-negotiable cases:

**Balance arithmetic**
- `compute_deltas` for all eight rows of the §4.4 table.
- Annul-disbursement pair: net cleared effect is exactly `0`.
- Reversing a `compensée` receipt produces an `en_circulation` disbursement taking
  `cleared −X` immediately.
- Book includes `annulée`; cleared excludes it.

**The control**
- `check_disbursement_allowed(0, 1)` → refused.
- `check_disbursement_allowed(10000, 10000)` → allowed (exactly zero is legal).
- `check_disbursement_allowed(10000, 10001)` → refused.
- Cleared 0 / book 5000 → a 1 ¢ disbursement is refused (the deposit-in-transit case).

**Validation**
- `client_id` absent from `dossier.client_ids` → refused.
- `amount <= 0` → refused.
- `date` earlier than the last entry's → refused (backdating).
- `purpose="correction"` from the create path → refused.
- `virement_honoraires` exceeding `invoice.amount_due` → refused.
- `virement_honoraires` on a `brouillon` invoice → refused.
- No dossier + `purpose="avance_honoraires"` → refused.

**Reversal**
- Double reversal → refused.
- Reversal of `en_circulation` → both `annulée`.
- Reversal of `compensée` → reversal `en_circulation`.
- Reversal always uses today's date, never the original's.

**Clearing**
- `cleared_date` before `date` → refused.
- `cleared_date` in the future → refused.
- Clearing a `compensée` entry → refused.
- Bulk clear with one bad id → whole batch aborts, zero entries cleared.

**Reconciliation**
- Variance formula, including a signed case where the statement exceeds the book.
- Complete with variance ≠ 0 → refused.

**Exports**
- `to_barreau_row` emits the eight columns in the exact §8 order with the exact header
  strings.
- `view="journal"` puts `balance_after_account` in Solde; `view="carte"` puts
  `balance_after_client`.
- A `déboursé` renders column 7 negative.

**MCP**
- `date` and `cleared_date` pass through `date_str`, never `iso_mtl`. Assert on an entry
  dated the 1st of a month that the output is that date, not the previous day.
- `list_trust_transactions` never emits `transit` or `account_number_last4`.

**Verification script** — `scripts/verify_trust_integrity.py`:
recomputes both running balances from `sequence` order and compares against the frozen
`balance_after_*` on every row; recomputes the denormalized dossier/account balances and
compares. Prints a report; **writes nothing**. Run before any inspection.

---

## 14. Verify before writing code

I have not verified these against RLRQ c. B-1, r. 5 and have not invented answers. Each
changes something concrete.

1. **The cleared-funds control** (§4.3, boxed). Affects the transaction. Conservative
   default in place; the cost of relaxing later is one clause, the cost of adding later is
   a backfill.
2. **Column 7 shape.** One signed column, or two (`Recette` and `Crédit`)? Only
   `to_barreau_row` changes.
3. **Register retention.** No purge exists anywhere in this spec; if the Règlement fixes a
   retention period, the exports are the artifact and the retention is an ops procedure,
   not code. Confirm nothing must be built.
4. **Recording deadline.** If the Règlement fixes a delay between the movement and its
   entry, the backdating guard (§5) may need a matching forward guard — an entry dated
   more than N days ago should at least warn.
5. **Is the auxiliary ledger required per client or per mandate?** The `(dossier, client)`
   control covers both readings, so this affects the *default card view* only, not the
   schema.

---

## 15. `CLAUDE.md` updates required

Update in the same commit — the document is the canonical reference and drifts silently
otherwise:

- **Architecture Rules** — a Rule 6-style documented exception for §2.1/§2.2 (no
  `update_*`/`delete_*`; three write-once mutable fields).
- **Firestore Data Model** — `trust_accounts`, `trust_transactions`,
  `trust_reconciliations`; the three new `dossiers` fields; `counters/trust-{account_id}`.
- **Routes Reference** — the `/fideicommis/*` table; `fideicommis` added to the dossier tab
  list; the MCP table's tool count 14 → 17.
- **Model Layer Reference** — `models/trust.py`; the `delete_dossier` trust condition.
- **Directory Structure** — `models/trust.py`, `routes/trust.py`, `templates/trust/`,
  `tests/test_trust.py`, `scripts/verify_trust_integrity.py`.
- **Known Gotchas** — add:
  - *Trust: the register's "Solde" is the **book** balance; the cleared balance is a
    control and must never be displayed under that label.*
  - *Trust: `sequence`, not `date`, is the register's order. Backdating is refused —
    correct with a reversal dated today.*
  - *Trust: `annulée` entries count in the book balance (they net with their reversal) and
    are excluded from the cleared balance. Getting this backwards double-counts funds.*
  - *Trust: `trust_transactions.date` and `.cleared_date` are date-only — `date_str` in
    MCP output, never `iso_mtl`.*
- **Phase History** — a Phase K entry.
- **`athena/OBSERVABILITY.md`** — the `log_trust_event` table (§12) and the two spans.

---

## 16. Implementation order

Bottom-up. Each step is independently testable; do not skip ahead to the UI.

1. `models/trust.py` pure functions (§6.1) + `tests/test_trust.py` for them. **Green before
   anything touches Firestore.**
2. Indexes (§11) → deploy → verify READY in the console.
3. `trust_accounts` CRUD + the counter.
4. `create_transaction` — the full transaction (§5). This is the module. Get it right.
5. `clear_transaction` / `clear_transactions_bulk` / `reverse_transaction`.
6. `create_inter_dossier_transfer`.
7. Reconciliation model + `complete_reconciliation`.
8. `log_trust_event` + spans + `OBSERVABILITY.md`.
9. Routes + templates: journal → entry form → card → accounts → reconciliation.
10. Dossier tab + dashboard stat.
11. Exports (CSV, then PDF).
12. MCP tools + consent-screen text.
13. `scripts/verify_trust_integrity.py`.
14. `CLAUDE.md` (§15).

**Zero new Python dependencies.** `utils/export_csv.py` and `utils/export_pdf.py` already
cover both export formats. Nothing here needs `openpyxl` — the `.xlsx` fill engine was
considered and rejected (`sharedStrings.xml` indexing, A1 reference recomputation on every
cloned row, `<dimension>` rewriting, per-cell styles: a project in itself, for a register
Excel opens from CSV without complaint).
