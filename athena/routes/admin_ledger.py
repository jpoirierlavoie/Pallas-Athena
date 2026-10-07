"""Administration accounting routes — « comptabilité d'administration ».

The firm-side sibling of routes/trust.py: journal with read-time running
balances, entry create/EDIT/DELETE (editable until the reconciliation lock —
the deliberate divergence from the trust register), contre-passation, card
payments (two legs), receipts (pièces justificatives, direct-to-GCS), bank
and credit-card statement reconciliation, CSV/PDF exports. An « encaissement
de facture » also records the payment on its invoice — since lot 5a INSIDE
the model's own transaction (``models/admin_ledger.create_transaction``
stages ``invoice.payment_updates``; a reversal reduces it the same way), so
this module projects nothing any more: there is no « entry saved, payment
failed » state left to show a banner for. The edit form carries the etag of
the version it shows (``routes/edit_conflict``, D9). Every write goes
through ``services/comptabilite`` (lot 5a, step 3) — the ONE orchestration
the connector will share — and « déjà compensée » is a single create, born
compensée, never create-then-clear.

The four natures outside the firm's results (2026-10-07,
``models/admin_ledger.NON_RESULT_KINDS``) ride the same form: the lawyer's
« Prélèvement » and « Apport », and one « Virement interne » whose « Sens »
names the stored kind. They show as plain text, like every kind. The
account page (for the calendar year) and the PDF show his « Avoir de
l'avocat » (``owner_equity``) — only over a register read whole; the
journal header does not (the lawyer's decision, 2026-10-07).

All @login_required, French UI, standard POST+redirect with inline error
boxes + HTTP 400. The receipt API endpoints exchange small JSON control
messages only — the bytes go browser→GCS (32 MB platform cap doctrine).
"""

import logging
import uuid
from datetime import datetime, timezone

from firebase_admin import storage
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from auth import login_required
from config import Config
from models import admin_ledger as al
from models.admin_ledger import (
    ACCOUNT_STATUS_LABELS,
    ACCOUNT_TYPE_LABELS,
    ADMIN_CATEGORY_LABELS,
    ADMIN_EXPENSE_CATEGORIES,
    BALANCE_LABELS,
    DIRECTION_LABELS,
    KIND_LABELS,
    MAX_RECEIPT_SIZE,
    METHOD_LABELS,
    RECEIPT_EXTENSIONS,
    RECEIPT_MIME_TYPES,
    RECONCILIATION_STATUS_LABELS,
    TX_STATUS_LABELS,
    VALID_ACCOUNT_TYPES,
    VALID_KINDS,
    VALID_METHODS,
    VALID_TX_STATUSES,
    direction_labels_for,
)
from models.audit_event import record_deletion
from services import comptabilite
from services import pieces_justificatives
from models.document import (
    _sniff_header,
    build_attachment_disposition,
    is_canonical_uuid4,
    sign_blob_url,
)
from models.dossier import get_dossier
from security import safe_internal_redirect
from utils.deadlines import today_mtl
from utils.format_fr import format_cents_fr, parse_cents_or_none
from utils.logging_setup import (
    log_admin_ledger_event,
    log_unexpected,
    sanitize_log_value,
)
from utils import storage_identity
from routes import edit_conflict
from routes._helpers import dossier_search_fragment, is_htmx, parse_date_input

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_ledger", __name__, url_prefix="/administration")

# The route never derives a direction, and never reads a posted
# « direction »: the MODEL implies it from the kind (models/admin_ledger
# ._KIND_DIRECTION, lot 0b). One rule, wherever the call comes from. The
# form's one « Sens » select (``sens_virement``) belongs to its « Virement
# interne » choice and names WHICH of the two stored kinds the entry is
# (virement_interne_sortant / _entrant) — a kind, whose sign the model then
# derives like any other (``_entry_form_data``).
_SENS_REQUIS = "Indiquez le sens du virement interne : sortant ou entrant."

# ── Helpers ────────────────────────────────────────────────────────────────


_is_htmx = is_htmx


_parse_date = parse_date_input


# Amount parsing lives in utils.format_fr (None when blank/invalid, never
# 0 — the trust load-bearing distinction). This module's copy and trust's
# had silently DRIFTED on narrow-NBSP handling (audit 2026-08-26).
_parse_cents = parse_cents_or_none


def _labels() -> dict:
    return {
        "kind_labels": KIND_LABELS,
        "method_labels": METHOD_LABELS,
        "direction_labels": DIRECTION_LABELS,
        "category_labels": ADMIN_CATEGORY_LABELS,
        "tx_status_labels": TX_STATUS_LABELS,
        "account_type_labels": ACCOUNT_TYPE_LABELS,
        "account_status_labels": ACCOUNT_STATUS_LABELS,
        "balance_labels": BALANCE_LABELS,
        "reconciliation_status_labels": RECONCILIATION_STATUS_LABELS,
        "valid_kinds": VALID_KINDS,
        "valid_methods": VALID_METHODS,
        "valid_categories": ADMIN_EXPENSE_CATEGORIES,
        "valid_tx_statuses": VALID_TX_STATUSES,
        "valid_account_types": VALID_ACCOUNT_TYPES,
        "direction_labels_for": direction_labels_for,
        # MONTRÉAL, not UTC: the model refuses future dates on today_mtl(),
        # so a UTC « today » would make the form's own default date bounce
        # every evening after 20:00 (the 2026-08-02 evening-band class).
        "today": today_mtl().strftime("%Y-%m-%d"),
    }


def _account_header(account: dict) -> dict:
    """Journal/account header: display balance, en circulation / en transit,
    last reconciliation + overdue badge."""
    account_id = account["id"]
    outstanding = al.list_outstanding(account_id)
    in_transit = al.list_in_transit(account_id)
    completed = [
        r for r in al.list_reconciliations(account_id) if r.get("status") == "complétée"
    ]
    last_date = max(
        (al._as_utc(r.get("period_end")) for r in completed), default=None
    )
    account_type = account.get("account_type", "")
    return {
        "display_balance": al.display_balance(
            account_type, int(account.get("ledger_balance", 0))
        ),
        "balance_label": BALANCE_LABELS.get(account_type, "Solde"),
        "outstanding_count": len(outstanding),
        "outstanding_total": sum(int(e.get("amount", 0)) for e in outstanding),
        "in_transit_count": len(in_transit),
        "in_transit_total": sum(int(e.get("amount", 0)) for e in in_transit),
        "last_reconciliation_date": last_date,
        # Aucune conciliation n'est due après clôture (revue 2026-08-15).
        "reconciliation_overdue": (
            False if account.get("status") == "fermé"
            else al._reconciliation_overdue(
                last_date, account_floor=account.get("created_at")
            )
        ),
    }


def _factures_impayees() -> list[dict]:
    """The GLOBAL unpaid-invoice list for the encaissement select — an admin
    recette may pay the invoice of ANY dossier, so no dossier cascade (the
    invoice determines the dossier, not the reverse). Only what the model's
    transactional verification will accept: issued statuses, live balance
    > 0. Fails OPEN to [] — a picker aid, never the verdict."""
    from models.invoice import balance_of, list_invoices

    out = []
    try:
        for status in ("envoyée", "en_retard"):
            for inv in list_invoices(status_filter=status):
                solde = balance_of(inv)
                if solde <= 0:
                    continue
                out.append({
                    "id": inv.get("id", ""),
                    "invoice_number": inv.get("invoice_number", ""),
                    "dossier": inv.get("dossier_file_number", ""),
                    "solde_cents": solde,
                    "solde_fmt": format_cents_fr(solde),
                })
    except Exception:
        logger.warning("admin: unpaid-invoice list failed")
        return []
    out.sort(key=lambda r: r["invoice_number"])
    return out


# ── Autocomplete (HTMX) ────────────────────────────────────────────────────


@admin_bp.route("/dossier-search")
@login_required
def dossier_search() -> str:
    return dossier_search_fragment(request.args.get("q", ""))


# ── Journal ────────────────────────────────────────────────────────────────


@admin_bp.route("/")
@login_required
def journal():
    accounts = al.list_accounts()
    if not accounts:
        return render_template("administration/list.html", accounts=[], account=None,
                               rows=[], header=None, filters={}, opening=None,
                               truncated=False, show_solde=False, **_labels())

    account_id = request.args.get("account_id") or accounts[0]["id"]
    account = next((a for a in accounts if a["id"] == account_id), accounts[0])
    account_id = account["id"]

    status = request.args.get("status") or None
    kind = request.args.get("kind") or None
    category = request.args.get("category") or None
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    if status not in VALID_TX_STATUSES:
        status = None
    if kind not in VALID_KINDS:
        kind = None
    if category not in ADMIN_EXPENSE_CATEGORIES:
        category = None

    rows, truncated = al.list_register(account_id, date_from, date_to)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if category:
        rows = [r for r in rows if r.get("category") == category]

    # The Solde column renders ONLY when no content filter is active — a
    # running balance over a filtered subset is a false figure; hide it
    # rather than lie. Date bounds are fine: the opening carries everything
    # before the period.
    show_solde = not (status or kind or category) and not truncated
    opening = None
    if show_solde:
        try:
            if date_from is not None:
                opening_cents, had_prior = al.opening_ledger_balance(account_id, date_from)
            else:
                opening_cents, had_prior = 0, False
            opening = {"cents": opening_cents, "had_prior": had_prior}
            balances = al.running_balances(rows, opening=opening_cents)
            for r, b in zip(rows, balances):
                r["_solde"] = b
        except Exception:
            log_unexpected("admin journal balance computation failed")
            show_solde = False
            opening = None

    header = _account_header(account)
    ctx = dict(
        accounts=accounts, account=account, rows=rows, header=header,
        opening=opening, truncated=truncated, show_solde=show_solde,
        filters={"status": status or "", "kind": kind or "", "category": category or "",
                 "date_from": request.args.get("date_from", ""),
                 "date_to": request.args.get("date_to", ""), "account_id": account_id},
        **_labels(),
    )
    if _is_htmx():
        return render_template("administration/_transaction_rows.html", **ctx)
    return render_template("administration/list.html", **ctx)


# ── Entry create / edit / delete ───────────────────────────────────────────


def _entry_form_data() -> dict:
    """The entry form, as the data the service receives.

    « Virement interne » + its « Sens » select (``sens_virement``) become the
    stored kind (``INTERNAL_TRANSFER_KIND_BY_SENS``). A missing or unknown
    sens leaves the form's own value (``INTERNAL_TRANSFER_FORM_KIND``), which
    no model call ever receives: both routes refuse it first (``_SENS_REQUIS``).

    The four natures outside the results carry no category, no split, no
    invoice — and a prélèvement or an apport no dossier. Their fields stay
    hidden (and disabled) in the form, but a stale value the browser still
    posts (no Alpine, a dossier picked before the Type changed) is
    NEUTRALIZED here, its key KEPT: the model judges only what a write
    carries and reads these empties as absent, while a present empty
    ``dossier_id`` is what clears a dossier left on an entry that becomes
    the lawyer's own money (``dossier_interdit`` otherwise)."""
    f = request.form
    kind = f.get("kind", "").strip()
    if kind == al.INTERNAL_TRANSFER_FORM_KIND:
        kind = al.INTERNAL_TRANSFER_KIND_BY_SENS.get(
            f.get("sens_virement", "").strip(), kind)
    data = {
        "account_id": f.get("account_id", "").strip(),
        "kind": kind,
        "amount": _parse_cents(f.get("amount", "")),
        "method": f.get("method", "").strip(),
        "counterparty": f.get("counterparty", "").strip(),
        "category": f.get("category", "").strip(),
        "net_amount": _parse_cents(f.get("net_amount", "")),
        "gst_amount": _parse_cents(f.get("gst_amount", "")),
        "qst_amount": _parse_cents(f.get("qst_amount", "")),
        "supplier_invoice_ref": f.get("supplier_invoice_ref", "").strip(),
        "invoice_id": f.get("invoice_id", "").strip(),
        "dossier_id": f.get("dossier_id", "").strip() or None,
        "reference": f.get("reference", "").strip(),
        "description": f.get("description", "").strip(),
        "date": _parse_date(f.get("date", "")),
    }
    if kind in al.NON_RESULT_KINDS:
        data.update(category="", net_amount=None, gst_amount=None,
                    qst_amount=None, invoice_id="")
        if kind in al.OWNER_KINDS:
            data["dossier_id"] = None
    return data


def _refuse_sens_requis(account_id: str, tx_id: str = "") -> list[str]:
    """« Virement interne » posted without a valid sens: nothing is written.
    The typed refusal line the model's own refusals use (OBSERVABILITY.md),
    ids and the machine reason only."""
    log_admin_ledger_event(
        "admin_transaction_refused", "refused",
        transaction_id=tx_id or None,
        account_id=sanitize_log_value(account_id) if account_id else None,
        reason="sens_requis",
    )
    return [_SENS_REQUIS]


def _kind_choices(mode: str, stored_kind: str) -> tuple[list, list]:
    """The form's « Type » and « Sens » options, ``[(value, label)]`` —
    derived from the model's vocabulary, never restated here.

    A create offers ``FORM_KINDS`` and both sens. An edit offers the stored
    kind and the passages ``KIND_MOVES`` permits from it (the model refuses
    any other, ``changement_de_sens``) in the form's values — the two
    virement codes are ONE « Virement interne » — and, for the virement,
    only the sens those passages keep: the bank movement's direction is a
    fact, only its nature changes."""
    sens_order = list(al.INTERNAL_TRANSFER_KIND_BY_SENS)
    if mode == "edit":
        kinds: list[str] = []
        sens: list[str] = []
        for kind in (stored_kind,) + tuple(al.KIND_MOVES.get(stored_kind, ())):
            value, kind_sens = al.form_kind(kind)
            if value and value not in kinds:
                kinds.append(value)
            if kind_sens and kind_sens not in sens:
                sens.append(kind_sens)
        known = list(al.FORM_KINDS)
        kinds.sort(key=lambda k: known.index(k) if k in known else len(known))
        sens.sort(key=sens_order.index)
    else:
        kinds, sens = list(al.FORM_KINDS), sens_order
    return (
        [(k, al.INTERNAL_TRANSFER_FORM_LABEL if k == al.INTERNAL_TRANSFER_FORM_KIND
          else KIND_LABELS.get(k, k)) for k in kinds],
        [(s, al.INTERNAL_TRANSFER_SENS_LABELS[s]) for s in sens],
    )


def _form_context(entry, errors: list[str], mode: str = "create",
                  conflict=None, *, stored_kind: str = "") -> dict:
    """The entry form's context. *entry* is what the form shows — the stored
    entry, or what a refused POST sent; *stored_kind* (an edit) is the kind
    on file, which alone decides the passages offered (default: the entry's
    own kind)."""
    dossier = None
    if entry and entry.get("dossier_id"):
        dossier = get_dossier(entry["dossier_id"])
    if mode == "edit" and not stored_kind:
        stored_kind = (entry or {}).get("kind") or ""
    kind_choices, sens_choices = _kind_choices(mode, stored_kind)
    # The stored code → the form's « Type » value and « Sens », for the edit
    # and for the re-render of a refused POST (« virement_interne » with no
    # sens comes back as itself, its sens blank).
    kind_value, sens_value = al.form_kind((entry or {}).get("kind") or "")
    if kind_value not in {value for value, _label in kind_choices}:
        # An empty or forged kind (a hand-made POST) never reaches the
        # select: the kind on file for an edit, the first choice otherwise.
        kind_value, sens_value = (al.form_kind(stored_kind) if mode == "edit"
                                  else (kind_choices[0][0], ""))
    sens_values = [value for value, _label in sens_choices]
    if sens_value not in sens_values:
        sens_value = ""
    if not sens_value and len(sens_values) == 1:
        # The one sens a passage keeps is a fact, not a choice.
        sens_value = sens_values[0]
    return dict(
        accounts=al.list_accounts(status="actif"), entry=entry, dossier=dossier,
        mode=mode, errors=errors, conflict=conflict,
        factures=_factures_impayees() if mode == "create" else [],
        kind_choices=kind_choices, sens_choices=sens_choices,
        form_kind_value=kind_value, form_sens_value=sens_value,
        **_labels(),
    )


@admin_bp.route("/nouvelle")
@login_required
def entry_new():
    return render_template(
        "administration/form.html", **_form_context(None, []),
    )


@admin_bp.route("/", methods=["POST"])
@login_required
def entry_create():
    data = _entry_form_data()
    if data["kind"] == al.INTERNAL_TRANSFER_FORM_KIND:
        return render_template(
            "administration/form.html",
            **_form_context(data, _refuse_sens_requis(data["account_id"])),
        ), 400
    # « Déjà compensée » — born compensée at the entry's own date, in the
    # SAME commit (lot 5a): the old create-then-clear could half-fail and
    # leave the entry « en circulation » under a banner. An encaissement's
    # payment is written on its invoice in that commit too, or the whole
    # entry is refused.
    cleared_date = data.get("date") if request.form.get("deja_compensee") == "1" else None
    report = comptabilite.enregistrer_ecriture_administration(
        data, cleared_date=cleared_date,
    )
    if report["errors"]:
        return render_template(
            "administration/form.html", **_form_context(data, report["errors"]),
        ), 400
    return redirect(url_for("admin_ledger.entry_detail", tx_id=report["entry"]["id"]))


@admin_bp.route("/<tx_id>")
@login_required
def entry_detail(tx_id: str):
    entry = al.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    account = al.get_account(entry.get("account_id"))
    reversal = al.get_transaction(entry["reversed_by_id"]) if entry.get("reversed_by_id") else None
    reverses = al.get_transaction(entry["reverses_id"]) if entry.get("reverses_id") else None
    other_leg = (
        al.get_transaction(entry["related_transaction_id"])
        if entry.get("related_transaction_id") else None
    )
    invoice = None
    if entry.get("invoice_id"):
        from models.invoice import balance_of, get_invoice

        invoice = get_invoice(entry["invoice_id"])
        if invoice is not None:
            invoice["_balance"] = balance_of(invoice)
    lock_reason = al._entry_lock_reason(
        entry, al.get_lock_floor(entry.get("account_id", ""))
    )
    # The copy of the receipt in the dossier (« Mandat › Déboursés »): read
    # for DISPLAY, failing open to None — then the card claims nothing and
    # offers nothing. The button's document id is minted HERE, so a double
    # submission of one page lands on ONE document.
    admissible = pieces_justificatives.admissible(entry)
    copies = pieces_justificatives.etat_des_copies(entry)
    # A dossier the store SAYS is gone: the button could only be refused, so
    # the card says why instead. Read only when the button would be offered,
    # and fail-open (an unreadable dossier keeps today's button).
    dossier_introuvable = bool(
        admissible and copies is not None and not copies["courante"]
        and pieces_justificatives.dossier_introuvable(entry)
    )
    return render_template(
        "administration/detail.html", entry=entry, account=account,
        reversal=reversal, reverses=reverses, other_leg=other_leg,
        invoice=invoice, lock_reason=lock_reason,
        avertissement=request.args.get("avertissement", ""),
        copie_admissible=admissible, copies=copies,
        copie_dossier_introuvable=dossier_introuvable,
        copie_document_id=str(uuid.uuid4()),
        **_labels(),
    )


@admin_bp.route("/<tx_id>/modifier", methods=["GET", "POST"])
@login_required
def entry_edit(tx_id: str):
    entry = al.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    lock_reason = al._entry_lock_reason(
        entry, al.get_lock_floor(entry.get("account_id", ""))
    )
    if request.method == "GET":
        if lock_reason:
            # The lock is re-verified inside the model transaction; here it
            # just spares a dead-end form.
            return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id))
        return render_template(
            "administration/form.html", **_form_context(entry, [], mode="edit"),
        )
    # D9 (lot 5a): the version the form was rendered from. None for a page
    # opened before the field existed — the model then checks nothing, as
    # before; a malformed value is a French 400 and nothing runs.
    expected = edit_conflict.submitted_etag()
    data = _entry_form_data()
    data.pop("account_id", None)  # immutable on edit
    data.pop("invoice_id", None)  # linkage is create-only
    if data["kind"] == al.INTERNAL_TRANSFER_FORM_KIND:
        # Nothing reaches the model; the refusal below re-renders with the
        # SUBMITTED etag, like any validation error.
        errors = _refuse_sens_requis(entry.get("account_id") or "", tx_id)
    else:
        errors = comptabilite.modifier_ecriture_administration(
            tx_id, data, expected_etag=expected,
        )["errors"]
    if not errors:
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id))

    stale, _others = edit_conflict.split_stale(errors)
    current = None
    if stale:
        current = al.get_transaction(tx_id)
        if current is not None and _lock_reason_now(current):
            # Changed since the form opened AND now locked — typically
            # cleared or reconciled meanwhile (by Claude, a second tab, a
            # reconciliation). An edit form for an entry that can no longer
            # be edited would be a dead end: the detail page says why.
            return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                    avertissement="verrouillee"))
    errors, conflict, etag = edit_conflict.resolve_refusal(
        errors, submitted=expected, reread=lambda: current,
        compare_url=url_for("admin_ledger.entry_detail", tx_id=tx_id),
    )
    # A stale save re-renders over the version stored NOW (with its etag —
    # the next save is a deliberate overwrite, made after reading the
    # banner); a validation error over the POST-time read, carrying the
    # SUBMITTED etag, so the original version keeps protecting the retry.
    base = current if (conflict and current) else entry
    merged = {**base, **{k: v for k, v in data.items() if v is not None}}
    merged["etag"] = etag
    # The passages offered follow the kind ON FILE (the next save is judged
    # against it), the selection what was submitted.
    context = _form_context(merged, errors, mode="edit", conflict=conflict,
                            stored_kind=base.get("kind") or "")
    # A stale re-render answers 200 like every edit form's (plan rule 11);
    # a validation refusal keeps this module's 400.
    return render_template("administration/form.html", **context), (
        200 if conflict else 400)


def _lock_reason_now(entry: dict):
    """The entry's lock reason as stored NOW. An unreadable lock floor
    degrades to the structural clauses alone (compensée, reversal member,
    linkage) — the model re-checks the floor inside its transaction on the
    next save anyway."""
    try:
        floor = al.get_lock_floor(entry.get("account_id", ""))
    except Exception:
        log_unexpected("admin: lock floor unreadable after a stale edit")
        floor = None
    return al._entry_lock_reason(entry, floor)


@admin_bp.route("/<tx_id>/supprimer", methods=["POST"])
@login_required
def entry_delete(tx_id: str):
    entry = al.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    if entry.get("kind") == "paiement_carte":
        legs, errors = al.delete_card_payment(tx_id)
        deleted = legs or []
    else:
        doc, errors = al.delete_transaction(tx_id)
        deleted = [doc] if doc else []
    if errors or not deleted:
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id))
    # House deletions registry — write side lives in the routes, AFTER the
    # committed delete (the audit_event doctrine). EVERY deleted row gets a
    # trail entry (a card payment removes two). The receipt blob, if any,
    # is deliberately kept: a supporting document outlives its entry.
    for doc in deleted:
        record_deletion(
            "admin_transaction", doc.get("id", ""),
            dossier_id=doc.get("dossier_id") or "",
            title=f"{KIND_LABELS.get(doc.get('kind', ''), '')} — "
                  f"{doc.get('counterparty', '')}",
            status=doc.get("status", ""),
        )
    return redirect(url_for("admin_ledger.journal",
                            account_id=deleted[0].get("account_id", "")))


# ── Ventilation (HTMX) ─────────────────────────────────────────────────────


@admin_bp.route("/ventilation")
@login_required
def ventilation():
    """« Ventiler depuis le montant » — the ONE Python implementation of the
    gross split re-renders the three fields prefilled (no JS re-derivation
    of the rounding). The fields stay editable: real receipts carry tips,
    exempt items and partial TPS."""
    # hx-include="#montant-input" serializes the field by its NAME (amount).
    montant = _parse_cents(request.args.get("amount", ""))
    if montant is None or montant <= 0:
        net, tps, tvq = None, None, None
    else:
        net, tps, tvq = al.extract_taxes_from_gross(montant)
    return render_template(
        "administration/_ventilation_fields.html",
        net_amount=net, gst_amount=tps, qst_amount=tvq,
    )


# ── Clearing / reversal ────────────────────────────────────────────────────


@admin_bp.route("/<tx_id>/compenser", methods=["POST"])
@login_required
def entry_clear(tx_id: str):
    # Default = MONTRÉAL midnight — datetime.now(utc) is already tomorrow
    # every evening after 20:00 and the model's future check would refuse it.
    d = today_mtl()
    cleared_date = _parse_date(request.form.get("cleared_date", "")) or datetime(
        d.year, d.month, d.day, tzinfo=timezone.utc
    )
    # D9 (lot 5b review): the version the detail page showed. A clearing
    # says « this amount, at this date, is on my statement » — and the
    # connector can correct an entry until it is cleared. None for a page
    # rendered before the field: nothing is checked, as before.
    expected = edit_conflict.submitted_etag()
    report = comptabilite.compenser_administration(
        [tx_id], cleared_date,
        expected_etags=None if expected is None else {tx_id: expected},
    )
    errors = report["errors"]
    params = ({"avertissement": "compensation_modifiee" if report.get("stale")
               else "compensation"} if errors else {})
    return_to = safe_internal_redirect(
        request.form.get("return_to", ""), ""
    )
    if errors or not return_to:
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id, **params))
    return redirect(return_to)


@admin_bp.route("/<tx_id>/contrepasser")
@login_required
def entry_reverse_confirm(tx_id: str):
    entry = al.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    return render_template(
        "administration/reverse_confirm.html", entry=entry, errors=[], **_labels()
    )


@admin_bp.route("/<tx_id>/contrepasser", methods=["POST"])
@login_required
def entry_reverse(tx_id: str):
    original = al.get_transaction(tx_id)
    if not original:
        return render_template("errors/404.html"), 404
    reason = request.form.get("reason", "").strip()
    reversal_date = _parse_date(request.form.get("reversal_date", ""))
    # D9 (lot 5b review): the version the confirmation page described — its
    # text (both entries annulée, or a reversal en circulation) follows the
    # stored status, which the connector's clearing can move. None for a
    # page rendered before the field: nothing is checked, as before.
    expected = edit_conflict.submitted_etag()
    report = comptabilite.contrepasser_ecriture_administration(
        tx_id, reason, reversal_date=reversal_date, expected_etag=expected,
    )
    if report["errors"]:
        if report.get("stale"):
            # Re-rendered over the entry as it is NOW, with its etag — 200,
            # like every stale re-render (routes/edit_conflict).
            current = al.get_transaction(tx_id) or original
            return render_template(
                "administration/reverse_confirm.html", entry=current,
                errors=[], stale=True, reason=reason, **_labels(),
            )
        return render_template(
            "administration/reverse_confirm.html", entry=original,
            errors=report["errors"], reason=reason, **_labels(),
        ), 400
    # An encaissement's payment was reduced on its invoice in the reversal's
    # own commit (lot 5a) — or the reversal was refused with it.
    return redirect(url_for("admin_ledger.entry_detail", tx_id=report["reversal"]["id"]))


# ── Card payment (two legs) ────────────────────────────────────────────────


@admin_bp.route("/paiement-carte", methods=["GET", "POST"])
@login_required
def card_payment():
    accounts = al.list_accounts(status="actif")
    banks = [a for a in accounts if a.get("account_type") == "opérations"]
    cards = [a for a in accounts if a.get("account_type") == "carte_crédit"]
    if request.method == "GET":
        return render_template(
            "administration/card_payment_form.html", banks=banks, cards=cards,
            errors=[], form={}, **_labels(),
        )
    f = request.form
    report = comptabilite.enregistrer_paiement_carte(
        bank_account_id=f.get("bank_account_id", "").strip(),
        card_account_id=f.get("card_account_id", "").strip(),
        amount=_parse_cents(f.get("amount", "")) or 0,
        date_value=_parse_date(f.get("date", "")),
        method=f.get("method", "virement").strip(),
        reference=f.get("reference", "").strip(),
        description=f.get("description", "").strip(),
    )
    if report["errors"]:
        return render_template(
            "administration/card_payment_form.html", banks=banks, cards=cards,
            errors=report["errors"], form=f.to_dict(), **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.entry_detail", tx_id=report["entry"]["id"]))


# ── Receipts (pièce justificative) — direct-to-GCS ─────────────────────────


@admin_bp.route("/api/televersement", methods=["POST"])
@login_required
def api_televersement():
    """Open a resumable GCS session for a receipt — the routes/documents.py
    twin, on the RECEIPT whitelist (PDF/JPG/PNG/TIFF, ≤ 10 MB: a supplier
    invoice is a photo or a PDF, and those magics are unambiguous)."""
    donnees = request.get_json(silent=True) or {}
    nom = str(donnees.get("name") or "")
    try:
        size = int(donnees.get("size"))
    except (TypeError, ValueError):
        size = -1

    ext = "." + nom.rsplit(".", 1)[1].lower() if "." in nom else ""
    if ext not in RECEIPT_EXTENSIONS:
        return jsonify({"erreur": (
            "Type de fichier non autorisé. Formats acceptés : PDF, JPG, PNG, TIFF."
        )}), 422
    if size <= 0 or size > MAX_RECEIPT_SIZE:
        return jsonify({
            "erreur": "Une pièce justificative doit faire entre 1 octet et 10 Mo."
        }), 422

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": storage_identity.public_message(exc)}), 503
    printable = "".join(ch for ch in nom if ch.isprintable())
    safe = secure_filename(printable) or "recu"
    objet = f"staging/{user_id}/{uuid.uuid4()}/{safe}"
    # Session CORS origin = the PAGE's origin. TLS terminates upstream of
    # gunicorn and there is no ProxyFix — request.scheme would read « http »
    # and the browser would refuse the PUT; https is FORCED in production.
    scheme = "https" if Config.ENV == "production" else (request.scheme or "http")
    try:
        blob = storage.bucket().blob(objet)
        url = blob.create_resumable_upload_session(
            content_type=RECEIPT_MIME_TYPES[ext], size=size,
            origin=f"{scheme}://{request.host}",
        )
    except Exception:
        logger.exception("admin: resumable-session open failed")
        return jsonify({
            "erreur": "Erreur lors de l'ouverture du téléversement. Réessayez."
        }), 503
    return jsonify({"url": url, "objet": objet})


@admin_bp.route("/<tx_id>/api/recu", methods=["POST"])
@login_required
def api_recu(tx_id: str):
    """Finalize a receipt: sniff a 512-byte probe of the staging object,
    rewrite it (GCS-side) to the transaction's firm-level path, attach the
    metadata, and CONSUME the staging blob in both outcomes. Replacing a
    receipt deletes the previous blob (NotFound tolerated)."""
    donnees = request.get_json(silent=True) or {}
    objet = str(donnees.get("objet") or "")
    nom = str(donnees.get("name") or "").strip() or "recu"

    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable as exc:
        return jsonify({"erreur": storage_identity.public_message(exc)}), 503
    if not objet.startswith(f"staging/{user_id}/"):
        return jsonify({"erreur": "Requête invalide."}), 400
    # staging/{uid}/{uuid4}/{nom} — EXACTEMENT la forme que frappe
    # api_televersement ci-dessus, et que routes/documents.api_finaliser
    # exige déjà. Le seul préfixe laissait classer comme pièce justificative
    # l'objet d'un billet de téléversement du connecteur
    # (staging/{uid}/mcp/{billet}/upload{ext}) — sans le contrôle de taille
    # et d'empreinte du billet, sans son échéance, sans claim_ticket — et le
    # CONSOMMAIT, si bien que finalize_upload répondait ensuite « rien
    # reçu ». Même chose pour une archive staging/{uid}/exports/… : refusées
    # ici, avant tout reload ni delete.
    segments = objet.split("/")
    if (len(segments) != 4 or not segments[3]
            or not is_canonical_uuid4(segments[2])):
        return jsonify({"erreur": "Requête invalide."}), 400
    entry = al.get_transaction(tx_id)
    if entry is None:
        return jsonify({"erreur": "Écriture introuvable."}), 404

    bucket = storage.bucket()
    try:
        blob = bucket.blob(objet)
        blob.reload()
    except Exception:
        logger.exception("admin: staging blob reload failed")
        return jsonify({"erreur": "Fichier téléversé introuvable. Réessayez."}), 422

    def _consume_staging():
        try:
            blob.delete()
        except Exception:
            logger.warning("admin: staging cleanup failed")

    ext = "." + nom.rsplit(".", 1)[1].lower() if "." in nom else ""
    if ext not in RECEIPT_EXTENSIONS or int(blob.size or 0) > MAX_RECEIPT_SIZE:
        _consume_staging()
        return jsonify({"erreur": "Type ou taille de fichier non autorisé."}), 422
    try:
        header = blob.download_as_bytes(start=0, end=511)
    except Exception:
        _consume_staging()
        return jsonify({"erreur": "Lecture du fichier impossible. Réessayez."}), 422
    content_type = _sniff_header(header, ext)
    if content_type != RECEIPT_MIME_TYPES[ext]:
        _consume_staging()
        return jsonify({
            "erreur": "Le contenu du fichier ne correspond pas à son extension."
        }), 422

    safe = secure_filename("".join(ch for ch in nom if ch.isprintable())) or "recu"
    dest_path = f"users/{user_id}/administration/{tx_id}/{safe}"
    try:
        dest = bucket.blob(dest_path)
        token, _, _ = dest.rewrite(blob)
        while token is not None:
            token, _, _ = dest.rewrite(blob, token=token)
        dest.content_type = content_type
        dest.content_disposition = "attachment"
        dest.patch()
    except Exception:
        logger.exception("admin: receipt rewrite failed")
        _consume_staging()
        return jsonify({"erreur": "Erreur lors de la sauvegarde. Réessayez."}), 422

    # The receipt's MD5 travels with it (the staging object's — the firm
    # copy holds the same bytes): it names WHICH receipt the entry carries,
    # so its copy in the dossier is told apart from a replaced one's.
    md5 = blob.md5_hash if isinstance(blob.md5_hash, str) else ""
    updated, errors = al.attach_receipt(
        tx_id, dest_path, nom, content_type, int(blob.size or 0), md5=md5,
    )
    _consume_staging()
    if errors or updated is None:
        return jsonify({"erreur": " ".join(errors) or "Sauvegarde impossible."}), 422
    previous = updated.get("_previous_receipt_path")
    if previous and previous != dest_path:
        try:
            bucket.blob(previous).delete()
        except Exception:
            logger.warning("admin: previous receipt cleanup failed")
    # The page to return to is built HERE, on success too: the upload
    # widget navigates to it rather than reloading, so a banner an earlier
    # failure left in the address (?avertissement=versement) never
    # survives a receipt that was filed.
    suivant = url_for("admin_ledger.entry_detail", tx_id=tx_id)
    if pieces_justificatives.admissible(updated):
        # A dépense of a dossier: its COPY goes to the dossier's « Mandat ›
        # Déboursés », under the staging object's id (validated above), so
        # a retried finalization lands on ONE document. The receipt IS
        # attached whatever happens here: a failure is a banner on the
        # entry page, never an error the upload widget would show as
        # « refusé ».
        try:
            versement = pieces_justificatives.verser_recu_au_dossier(
                tx_id, user_id, document_id=segments[2],
            )
        except Exception:
            log_unexpected("admin: receipt copy to dossier failed",
                           transaction_id=tx_id)
            versement = None
        if versement is None or versement.document is None:
            suivant = url_for("admin_ledger.entry_detail", tx_id=tx_id,
                              avertissement=_versement_code(versement))
    return jsonify({"ok": True, "suivant": suivant})


# The CLOSED ?avertissement= codes of a copy that could not be filed. The two
# LASTING refusals get their own banner — « réessayez » cannot help when the
# entry's dossier is gone, nor when the receipt changed under the call (the
# page is stale); every other refusal reads as the generic « versement ».
_VERSEMENT_CODES = {
    pieces_justificatives.REASON_DOSSIER_INTROUVABLE:
        "versement_dossier_introuvable",
    pieces_justificatives.REASON_RECU_MODIFIE: "versement_piece_modifiee",
}


def _versement_code(versement: "pieces_justificatives.Versement | None") -> str:
    """The closed banner code of a copy that was NOT filed (*versement* is
    ``None`` when the service raised)."""
    if versement is None:
        return "versement"
    if versement.code == pieces_justificatives.INADMISSIBLE:
        return "versement_inadmissible"
    return _VERSEMENT_CODES.get(versement.reason, "versement")


@admin_bp.route("/<tx_id>/recu/verser", methods=["POST"])
@login_required
def recu_verser(tx_id: str):
    """« Verser une copie au dossier » — file a COPY of the entry's pièce
    justificative in its dossier's « Mandat › Déboursés » folder
    (``services/pieces_justificatives``). The document id comes from the
    page (minted at render), so a double submission lands on ONE document;
    every outcome returns to the entry page, a refusal under a CLOSED
    ``?avertissement=`` code — the card itself says what was filed."""
    entry = al.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    if not pieces_justificatives.admissible(entry):
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                avertissement="versement_inadmissible"))
    try:
        user_id = storage_identity.request_uid()
    except storage_identity.StorageIdentityUnavailable:
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                avertissement="versement"))
    document_id = request.form.get("document_id", "")
    if not is_canonical_uuid4(document_id):
        document_id = str(uuid.uuid4())
    try:
        versement = pieces_justificatives.verser_recu_au_dossier(
            tx_id, user_id, document_id=document_id,
        )
    except Exception:
        log_unexpected("admin: receipt copy to dossier failed",
                       transaction_id=tx_id)
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                avertissement="versement"))
    if versement.document is None:
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                avertissement=_versement_code(versement)))
    return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id))


@admin_bp.route("/<tx_id>/recu")
@login_required
def recu(tx_id: str):
    entry = al.get_transaction(tx_id)
    if not entry or not entry.get("receipt_storage_path"):
        return render_template("errors/404.html"), 404
    try:
        blob = storage.bucket().blob(entry["receipt_storage_path"])
        url = sign_blob_url(blob, {
            "response-content-disposition": build_attachment_disposition(
                entry.get("receipt_filename") or "recu"
            ),
            "response-content-type": entry.get("receipt_file_type")
            or "application/octet-stream",
        })
    except Exception:
        log_unexpected("admin: receipt signing failed")
        return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id,
                                avertissement="recu"))
    return redirect(url)


# ── Accounts ───────────────────────────────────────────────────────────────


@admin_bp.route("/comptes/")
@login_required
def accounts_list():
    return render_template(
        "administration/accounts_list.html",
        snapshot=al.get_firm_admin_snapshot(), **_labels(),
    )


def _account_form_data() -> dict:
    f = request.form
    return {
        "name": f.get("name", "").strip(),
        "account_type": f.get("account_type", "opérations").strip(),
        "institution": f.get("institution", "").strip(),
        "transit": f.get("transit", "").strip(),
        "account_number_last4": f.get("account_number_last4", "").strip(),
        "status": f.get("status", "actif").strip(),
        "notes": f.get("notes", "").strip(),
    }


@admin_bp.route("/comptes/nouveau", methods=["GET", "POST"])
@login_required
def account_new():
    if request.method == "GET":
        return render_template("administration/account_form.html", account=None,
                               errors=[], **_labels())
    account, errors = al.create_account(_account_form_data())
    if errors:
        return render_template(
            "administration/account_form.html", account=request.form.to_dict(),
            errors=errors, **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.account_detail", account_id=account["id"]))


def _avoir_annee_civile(account_id: str) -> dict:
    """The account's « Avoir de l'avocat » for the calendar year to date —
    Jan 1 to today, on Montréal's calendar (``today_mtl``). Three outcomes,
    never confused: ``avoir`` (a complete read), ``avoir_indisponible`` (the
    read failed — shown as such, never as zero) and neither (a truncated
    read: a partial figure is not shown)."""
    today = today_mtl()
    jan1 = datetime(today.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    out = {"avoir": None, "avoir_indisponible": False,
           "avoir_period": _journal_period_label(jan1, end)}
    try:
        rows, truncated = al.list_register(account_id, jan1, end)
    except Exception:
        log_unexpected("admin: owner-equity register read failed",
                       account_id=account_id)
        out["avoir_indisponible"] = True
        return out
    if not truncated:
        out["avoir"] = al.owner_equity(rows)
    return out


@admin_bp.route("/comptes/<account_id>")
@login_required
def account_detail(account_id: str):
    account = al.get_account(account_id)
    if not account:
        return render_template("errors/404.html"), 404
    return render_template(
        "administration/account_detail.html", account=account,
        header=_account_header(account),
        reconciliations=al.list_reconciliations(account_id),
        **_avoir_annee_civile(account["id"]), **_labels(),
    )


@admin_bp.route("/comptes/<account_id>/edit", methods=["GET", "POST"])
@login_required
def account_edit(account_id: str):
    account = al.get_account(account_id)
    if not account:
        return render_template("errors/404.html"), 404
    if request.method == "GET":
        return render_template("administration/account_form.html", account=account,
                               errors=[], **_labels())
    updated, errors = al.update_account(account_id, _account_form_data())
    if errors:
        merged = {**account, **request.form.to_dict()}
        return render_template("administration/account_form.html", account=merged,
                               errors=errors, **_labels()), 400
    return redirect(url_for("admin_ledger.account_detail", account_id=account_id))


# ── Reconciliation ─────────────────────────────────────────────────────────


@admin_bp.route("/conciliations/")
@login_required
def reconciliations_list():
    return render_template(
        "administration/reconciliations_list.html",
        reconciliations=al.list_reconciliations(),
        accounts={a["id"]: a for a in al.list_accounts()}, **_labels(),
    )


@admin_bp.route("/conciliations/nouvelle", methods=["GET", "POST"])
@login_required
def reconciliation_new():
    accounts = al.list_accounts(status="actif")
    if request.method == "GET":
        # « Concilier CE compte » depuis le hub / le détail : présélection
        # par query string. Un id inconnu est inoffensif (aucune option ne
        # correspond) ; create_reconciliation reste le garde au POST.
        prefill = {"account_id": request.args.get("account_id", "")}
        return render_template("administration/reconciliation_form.html",
                               accounts=accounts, errors=[], form=prefill, **_labels())
    f = request.form
    statement_cents = _parse_cents(f.get("statement_balance", ""))
    if statement_cents is None:
        rec, errors = None, ["Le solde du relevé est requis."]
    else:
        rec, errors = al.create_reconciliation(
            account_id=f.get("account_id", "").strip(),
            period_end=_parse_date(f.get("period_end", "")),
            statement_balance=statement_cents,
        )
    if errors:
        return render_template(
            "administration/reconciliation_form.html", accounts=accounts,
            errors=errors, form=f.to_dict(), **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.reconciliation_worksheet", rec_id=rec["id"]))


def _worksheet_context(rec: dict) -> dict:
    """One seam for the GET render and the 400 re-renders (the trust rule:
    the displayed numbers can never drift from the completion gate). The
    statement figure crosses into LEDGER SIGN here — a card statement
    states the solde dû."""
    account = al.get_account(rec["account_id"])
    return {
        "rec": rec,
        "account": account,
        "statement_ledger": al.statement_to_ledger(
            (account or {}).get("account_type", ""),
            int(rec.get("statement_balance", 0)),
        ),
        **al.reconciliation_as_of_context(rec["account_id"], rec["period_end"]),
    }


@admin_bp.route("/conciliations/<rec_id>")
@login_required
def reconciliation_worksheet(rec_id: str):
    rec = al.get_reconciliation(rec_id)
    if not rec:
        return render_template("errors/404.html"), 404
    return render_template(
        "administration/reconciliation_worksheet.html",
        **_worksheet_context(rec), **_labels(),
    )


@admin_bp.route("/conciliations/<rec_id>/completer", methods=["POST"])
@login_required
def reconciliation_complete(rec_id: str):
    cleared_ids = request.form.getlist("cleared_tx_ids")
    rec, errors = al.complete_reconciliation(rec_id, cleared_ids)
    if errors:
        current = al.get_reconciliation(rec_id)
        if not current:
            return render_template("errors/404.html"), 404
        return render_template(
            "administration/reconciliation_worksheet.html",
            **_worksheet_context(current), errors=errors, **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.reconciliation_worksheet", rec_id=rec_id))


@admin_bp.route("/conciliations/<rec_id>/abandonner", methods=["POST"])
@login_required
def reconciliation_abandon(rec_id: str):
    ok, errors = al.delete_reconciliation(rec_id)
    if errors:
        current = al.get_reconciliation(rec_id)
        if not current:
            return redirect(url_for("admin_ledger.reconciliations_list"))
        return render_template(
            "administration/reconciliation_worksheet.html",
            **_worksheet_context(current), errors=errors, **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.reconciliations_list"))


# ── Exports ────────────────────────────────────────────────────────────────

_CSV_COLUMNS = [
    ("date", "Date"),
    ("counterparty", "Fournisseur / Source"),
    # The entry's kind (KIND_LABELS): a prélèvement, an apport or a
    # virement interne carries no category, and a book of account must
    # still say what each line is.
    ("nature", "Nature"),
    ("categorie", "Catégorie"),
    ("supplier_invoice_ref", "N° facture fournisseur"),
    ("n_ref", "N/Réf"),
    ("mode", "Mode"),
    ("statut", "Statut"),
    ("net", "Net"),
    ("tps", "TPS"),
    ("tvq", "TVQ"),
    ("recette", "Recette"),
    ("debours", "Déboursé"),
    ("solde", "Solde"),
    ("description", "Description"),
]
_CENTS_KEYS = ["net", "tps", "tvq", "recette", "debours", "solde"]


def _ventilation_signed(tx: dict) -> tuple:
    """``(net, tps, tvq)`` SIGNED for display and totals, or three Nones.

    A dépense (and a déboursé correction) shows its ventilation positive; a
    RECETTE correction — the reversal of a dépense, which carries the
    original's ventilation copied — shows it NEGATIVE, so the tax columns
    and the period's Σ TPS/Σ TVQ NET a reversed expense to zero. Without
    the sign, the CTI/RTI figure a book of account feeds to the tax return
    claims credits for reversed purchases. Rows with no ventilation
    (recettes, card payments, exempt/unventilated) show blanks."""
    if tx.get("kind") not in ("dépense", "correction"):
        return None, None, None
    n, g, q = (
        int(tx.get(k) or 0)
        for k in ("net_amount", "gst_amount", "qst_amount")
    )
    if not (n or g or q):
        return None, None, None
    sign = 1 if tx.get("direction") == "déboursé" else -1
    return sign * n, sign * g, sign * q


def _export_rows(txs: list[dict], soldes) -> list[dict]:
    """Project entries for the CSV. ``soldes`` is the running-balance list
    (aligned with txs) or None — a filtered export leaves the Solde column
    BLANK rather than printing a false running figure. « * » flags an
    en_circulation row on the date, the trust convention. Ventilation via
    :func:`_ventilation_signed` (a reversed dépense NETS)."""
    out = []
    for i, tx in enumerate(txs):
        d = al._as_utc(tx.get("date"))
        s = d.strftime("%Y-%m-%d") if isinstance(d, datetime) else ""
        if tx.get("status") == "en_circulation":
            s = f"{s} *"
        direction = tx.get("direction", "")
        amount = int(tx.get("amount") or 0)
        net, tps, tvq = _ventilation_signed(tx)
        out.append({
            "date": s,
            "counterparty": tx.get("counterparty", ""),
            "nature": KIND_LABELS.get(tx.get("kind", ""), tx.get("kind", "")),
            "categorie": ADMIN_CATEGORY_LABELS.get(tx.get("category") or "", ""),
            "supplier_invoice_ref": tx.get("supplier_invoice_ref", ""),
            "n_ref": tx.get("dossier_file_number", ""),
            "mode": METHOD_LABELS.get(tx.get("method", ""), tx.get("method", "")),
            "statut": TX_STATUS_LABELS.get(tx.get("status", ""), tx.get("status", "")),
            "net": net,
            "tps": tps,
            "tvq": tvq,
            "recette": amount if direction == "recette" else None,
            "debours": amount if direction == "déboursé" else None,
            "solde": soldes[i] if soldes is not None else None,
            "description": tx.get("description", ""),
        })
    return out


def _journal_period_label(date_from, date_to) -> str:
    if date_from and date_to:
        return (f"Période du {date_from.strftime('%Y-%m-%d')} "
                f"au {date_to.strftime('%Y-%m-%d')}")
    if date_from:
        return f"À compter du {date_from.strftime('%Y-%m-%d')}"
    if date_to:
        return f"Jusqu'au {date_to.strftime('%Y-%m-%d')}"
    return "Depuis l'ouverture du compte"


def _account_line(account: dict) -> str:
    """Name — institution — ••••1234 (the trust redaction discipline)."""
    parts = [account.get("name", "") or "Compte d'administration"]
    if account.get("institution"):
        parts.append(account["institution"])
    if account.get("account_number_last4"):
        parts.append(f"••••{account['account_number_last4']}")
    return " — ".join(parts)


@admin_bp.route("/export/<fmt>")
@login_required
def journal_export(fmt: str):
    account_id = request.args.get("account_id", "").strip()
    account = al.get_account(account_id) if account_id else None
    if account is None:
        accounts = al.list_accounts()
        if not accounts:
            return "Aucun compte", 404
        account = accounts[0]
        account_id = account["id"]
    status = request.args.get("status") or None
    kind = request.args.get("kind") or None
    category = request.args.get("category") or None
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    if status not in VALID_TX_STATUSES:
        status = None
    if kind not in VALID_KINDS:
        kind = None
    if category not in ADMIN_EXPENSE_CATEGORIES:
        category = None

    if fmt == "pdf":
        return _journal_pdf(account, account_id, date_from, date_to)
    if fmt != "csv":
        return "Format non supporté", 400

    try:
        rows, truncated = al.list_register(account_id, date_from, date_to)
    except Exception:
        log_unexpected("admin register read failed")
        return "Lecture du registre impossible. Réessayez.", 503
    filtered = bool(status or kind or category)
    soldes = None
    if not filtered and not truncated:
        try:
            opening, _had = (
                al.opening_ledger_balance(account_id, date_from)
                if date_from is not None else (0, False)
            )
            soldes = al.running_balances(rows, opening=opening)
        except Exception:
            log_unexpected("admin export balance computation failed")
            soldes = None
    if status:
        rows = [r for r in rows if r.get("status") == status]
        soldes = None
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
        soldes = None
    if category:
        rows = [r for r in rows if r.get("category") == category]
        soldes = None

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_admin_ledger_event("admin_export", format="csv", account_id=account_id,
                           row_count=len(rows))
    from utils.export_csv import export_csv

    export_rows = _export_rows(rows, soldes)
    if truncated:
        # The screen banners and the PDF prints its avertissement — the CSV
        # must carry its own marker, or an incomplete export is
        # indistinguishable from a complete one.
        export_rows.append({
            "date": "AVERTISSEMENT",
            "counterparty": (
                "Registre tronqué — cette exportation ne couvre pas toute "
                "la période demandée."
            ),
        })
    return export_csv(
        rows=export_rows, columns=_CSV_COLUMNS,
        filename=f"journal_administration_{day}.csv", cents_fields=_CENTS_KEYS,
    )


def _journal_pdf(account: dict, account_id: str, date_from, date_to):
    """The legal-landscape journal. Like the trust register it deliberately
    IGNORES the content filters (a book of account is complete — only a
    complete one lets « report + Σ recettes − Σ déboursés = solde de
    clôture » verify) and honours the period. Degrades with a printed
    notice rather than a generic error page."""
    from utils.admin_journal_pdf import build_admin_journal_pdf

    notices: list[str] = []
    read = True
    try:
        txs, truncated = al.list_register(account_id, date_from=date_from, date_to=date_to)
    except Exception:
        log_unexpected("admin register read failed")
        txs, truncated, read = [], False, False
        notices.append(
            "AVERTISSEMENT : les inscriptions n'ont pas pu être lues. Ce "
            "document ne contient AUCUNE inscription — cela ne signifie "
            "pas que la période est vide. Ne l'utilisez pas comme registre."
        )

    opening_cents = None
    opening_label = ""
    soldes = None
    if not truncated:
        try:
            if date_from is not None:
                carried, had_prior = al.opening_ledger_balance(account_id, date_from)
                opening_cents = carried
                opening_label = (
                    f"SOLDE REPORTÉ AU {date_from.strftime('%Y-%m-%d')}"
                    if had_prior else
                    f"SOLDE REPORTÉ AU {date_from.strftime('%Y-%m-%d')} — "
                    "aucune inscription antérieure"
                )
            else:
                carried = 0
            soldes = al.running_balances(txs, opening=carried)
        except Exception:
            log_unexpected("admin opening balance read failed")
            notices.append(
                "Avertissement : le solde reporté n'a pas pu être établi. "
                "Les inscriptions ci-dessous sont complètes, mais la colonne "
                "« Solde » est laissée vide."
            )
    if truncated:
        notices.append(
            "Avertissement : le registre a été tronqué — cette feuille ne "
            "couvre pas toute la période demandée."
        )

    rows = []
    for i, tx in enumerate(txs):
        d = al._as_utc(tx.get("date"))
        objet = tx.get("counterparty", "")
        direction = tx.get("direction", "")
        amount = int(tx.get("amount") or 0)
        net, tps, tvq = _ventilation_signed(tx)
        rows.append({
            "date": d.strftime("%Y-%m-%d") if isinstance(d, datetime) else "",
            "counterparty": objet,
            # The sheet has no « Type » column (COLUMNS is pinned): a row
            # with no category — a recette, a card payment, a prélèvement,
            # an apport, a virement interne — says its nature there.
            "categorie": (ADMIN_CATEGORY_LABELS.get(tx.get("category") or "", "")
                          or KIND_LABELS.get(tx.get("kind", ""), tx.get("kind", ""))),
            "facture": tx.get("supplier_invoice_ref", "") or tx.get("invoice_number", ""),
            "mode": METHOD_LABELS.get(tx.get("method", ""), tx.get("method", "")),
            "net": net,
            "tps": tps,
            "tvq": tvq,
            "recette": amount if direction == "recette" else None,
            "debours": amount if direction == "déboursé" else None,
            "solde": soldes[i] if soldes is not None else None,
            "en_circulation": tx.get("status") == "en_circulation",
        })

    # The closing tax block — the CTI/RTI payoff. _ventilation_signed makes
    # a reversal's ventilation NEGATIVE, so a dépense contre-passée nets to
    # zero here (and in the printed columns) instead of over-claiming input
    # tax credits on a purchase that was reversed.
    tps_total = sum(r["tps"] or 0 for r in rows)
    tvq_total = sum(r["tvq"] or 0 for r in rows)
    # The « Avoir de l'avocat » line — printed only over a register read
    # whole: a failed read prints no inscriptions, and a truncated one would
    # understate the figure. The Recette/Déboursé totals above stay
    # inclusive (report + recettes − déboursés = solde).
    avoir = al.owner_equity(txs) if read and not truncated else None

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_admin_ledger_event("admin_export", format="pdf", account_id=account_id,
                           row_count=len(rows))
    return build_admin_journal_pdf(
        rows,
        account_line=_account_line(account),
        period=_journal_period_label(date_from, date_to),
        filename=f"journal_administration_{day}.pdf",
        opening_cents=opening_cents,
        opening_label=opening_label,
        tps_total=tps_total,
        tvq_total=tvq_total,
        notices=notices,
        avoir=avoir,
    )
