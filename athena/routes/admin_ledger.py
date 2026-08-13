"""Administration accounting routes — « comptabilité d'administration ».

The firm-side sibling of routes/trust.py: journal with read-time running
balances, entry create/EDIT/DELETE (editable until the reconciliation lock —
the deliberate divergence from the trust register), contre-passation, card
payments (two legs), receipts (pièces justificatives, direct-to-GCS), bank
and credit-card statement reconciliation, CSV/PDF exports, and the Lot P
projection (an « encaissement de facture » entry also records the payment on
the invoice — ``record_payment`` stays the single writer of ``amount_paid``;
this route always writes *current + delta* so the cumulative is preserved).

All @login_required, French UI, standard POST+redirect with inline error
boxes + HTTP 400. The receipt API endpoints exchange small JSON control
messages only — the bytes go browser→GCS (32 MB platform cap doctrine).
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from firebase_admin import storage
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import escape
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
from models.document import _sniff_header, build_attachment_disposition, sign_blob_url
from models.dossier import get_dossier, list_dossiers
from security import safe_internal_redirect
from utils.deadlines import today_mtl
from utils.format_fr import format_cents_fr
from utils.logging_setup import log_admin_ledger_event, log_unexpected

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_ledger", __name__, url_prefix="/administration")

# The simple kinds' direction is implied — the form has no Sens select.
_KIND_DIRECTION = {
    "encaissement_facture": "recette",
    "recette_autre": "recette",
    "dépense": "déboursé",
}


# ── Helpers ────────────────────────────────────────────────────────────────


def _is_htmx() -> bool:
    return request.headers.get("HX-Request") == "true"


def _parse_date(value: str):
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_cents(raw):
    """Parse a fr-CA / en amount string into integer cents, or None when
    blank/invalid (None, never 0 — the trust load-bearing distinction)."""
    if raw is None:
        return None
    s = str(raw).strip().replace(" ", " ").replace(" ", "").replace(" ", "").replace("$", "")
    if not s:
        return None
    s = s.replace(",", ".")
    if s.count(".") > 1:  # e.g. "1.234.56" — drop grouping dots
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


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
        "reconciliation_overdue": al._reconciliation_overdue(
            last_date, account_floor=account.get("created_at")
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
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return '<div class="px-3 py-2 text-sm text-gray-500">Tapez au moins 2 caractères…</div>'
    dossiers = list_dossiers(search=q)[:10]
    if not dossiers:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun dossier trouvé</div>'
    parts = ['<ul class="divide-y divide-gray-100">']
    for d in dossiers:
        parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' data-dossier-id="{escape(d["id"])}"'
            f' data-dossier-file-number="{escape(d.get("file_number", ""))}"'
            f' data-dossier-title="{escape(d.get("title", ""))}">'
            f'<span class="font-medium text-gray-900">{escape(d.get("file_number", ""))}</span>'
            f'<span class="text-gray-500 ml-1">{escape(d.get("title", ""))}</span></li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


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

    ctx = dict(
        accounts=accounts, account=account, rows=rows, header=_account_header(account),
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
    f = request.form
    kind = f.get("kind", "").strip()
    return {
        "account_id": f.get("account_id", "").strip(),
        "kind": kind,
        "direction": _KIND_DIRECTION.get(kind, ""),
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


def _form_context(entry, errors: list[str], mode: str = "create") -> dict:
    dossier = None
    if entry and entry.get("dossier_id"):
        dossier = get_dossier(entry["dossier_id"])
    return dict(
        accounts=al.list_accounts(status="actif"), entry=entry, dossier=dossier,
        mode=mode, errors=errors,
        factures=_factures_impayees() if mode == "create" else [],
        **_labels(),
    )


def _projeter_paiement(entry: dict) -> bool:
    """Lot P projection: an encaissement entry also records the payment on
    its invoice — ``record_payment`` stays the SINGLE writer of
    ``amount_paid``, and this always writes *current + delta* (re-read just
    before the call) so a manual correction on the invoice side is
    preserved. Returns True on success; on failure the ENTRY STANDS (the
    register is the book of record) and the caller shows a banner."""
    from models.invoice import get_invoice, record_payment

    try:
        inv = get_invoice(entry["invoice_id"])
        if inv is None:
            raise RuntimeError("invoice unreadable")
        new_total = int(inv.get("amount_paid", 0)) + int(entry["amount"])
        updated, errs = record_payment(
            entry["invoice_id"], new_total, paid_date=entry.get("date")
        )
        if errs:
            raise RuntimeError(errs[0])
    except Exception:
        log_unexpected("admin: invoice payment projection failed")
        log_admin_ledger_event(
            "admin_invoice_payment_projected", "failure",
            transaction_id=entry.get("id"), invoice_id=entry.get("invoice_id"),
        )
        return False
    log_admin_ledger_event(
        "admin_invoice_payment_projected", transaction_id=entry.get("id"),
        invoice_id=entry.get("invoice_id"),
    )
    return True


def _reduire_paiement(entry: dict) -> bool:
    """Reversal counterpart of :func:`_projeter_paiement`: reduce the
    invoice's recorded payment by the reversed entry's amount. Passes the
    EXISTING paid_date through so a partial reduction does not stamp today
    (record_payment nulls it itself at zero). The narrow payée→envoyée undo
    is exactly the model behaviour this exists to trigger."""
    from models.invoice import get_invoice, record_payment

    try:
        inv = get_invoice(entry["invoice_id"])
        if inv is None:
            raise RuntimeError("invoice unreadable")
        new_total = int(inv.get("amount_paid", 0)) - int(entry["amount"])
        if new_total < 0:
            # An inconsistency between the register and the invoice (a
            # projection that never committed, a manual correction in
            # between). A max(0, …) clamp here would convert it into
            # SILENT data loss — other recorded payments erased with no
            # signal. Surface it instead: banner → manual correction.
            raise RuntimeError("recorded payment below the reversed amount")
        updated, errs = record_payment(
            entry["invoice_id"], new_total, paid_date=inv.get("paid_date")
        )
        if errs:
            raise RuntimeError(errs[0])
    except Exception:
        log_unexpected("admin: invoice payment reduction failed")
        log_admin_ledger_event(
            "admin_invoice_payment_projected", "failure",
            transaction_id=entry.get("id"), invoice_id=entry.get("invoice_id"),
        )
        return False
    log_admin_ledger_event(
        "admin_invoice_payment_projected", transaction_id=entry.get("id"),
        invoice_id=entry.get("invoice_id"), reduced=True,
    )
    return True


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
    entry, errors = al.create_transaction(data)
    if errors:
        return render_template(
            "administration/form.html", **_form_context(data, errors),
        ), 400

    # « Déjà compensée » — the ROUTE composes create-then-clear; the model
    # keeps its create-always-en_circulation purity.
    params = {}
    if request.form.get("deja_compensee") == "1":
        _, clear_errors = al.clear_transaction(entry["id"], entry.get("date"))
        if clear_errors:
            # The lawyer asserted the entry already figures on the statement;
            # recording the opposite silently would surface only at the next
            # reconciliation — banner, like every other partial failure here.
            log_unexpected("admin: auto-clear after create failed", exc_info=False)
            params["avertissement"] = "compensation"

    if entry.get("invoice_id") and not _projeter_paiement(entry):
        params["avertissement"] = "facture"
    return redirect(url_for("admin_ledger.entry_detail", tx_id=entry["id"], **params))


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
    return render_template(
        "administration/detail.html", entry=entry, account=account,
        reversal=reversal, reverses=reverses, other_leg=other_leg,
        invoice=invoice, lock_reason=lock_reason,
        avertissement=request.args.get("avertissement", ""),
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
    data = _entry_form_data()
    data.pop("account_id", None)  # immutable on edit
    data.pop("invoice_id", None)  # linkage is create-only
    updated, errors = al.update_transaction(tx_id, data)
    if errors:
        merged = {**entry, **{k: v for k, v in data.items() if v is not None}}
        return render_template(
            "administration/form.html", **_form_context(merged, errors, mode="edit"),
        ), 400
    return redirect(url_for("admin_ledger.entry_detail", tx_id=tx_id))


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
    _, errors = al.clear_transaction(tx_id, cleared_date)
    params = {"avertissement": "compensation"} if errors else {}
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
    reversal, errors = al.reverse_transaction(tx_id, reason, reversal_date=reversal_date)
    if errors:
        return render_template(
            "administration/reverse_confirm.html", entry=original, errors=errors,
            **_labels(),
        ), 400
    params = {}
    if original.get("invoice_id") and not _reduire_paiement(original):
        params["avertissement"] = "facture"
    return redirect(url_for("admin_ledger.entry_detail", tx_id=reversal["id"], **params))


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
    leg, errors = al.create_card_payment(
        bank_account_id=f.get("bank_account_id", "").strip(),
        card_account_id=f.get("card_account_id", "").strip(),
        amount=_parse_cents(f.get("amount", "")) or 0,
        date_value=_parse_date(f.get("date", "")),
        method=f.get("method", "virement").strip(),
        reference=f.get("reference", "").strip(),
        description=f.get("description", "").strip(),
    )
    if errors:
        return render_template(
            "administration/card_payment_form.html", banks=banks, cards=cards,
            errors=errors, form=f.to_dict(), **_labels(),
        ), 400
    return redirect(url_for("admin_ledger.entry_detail", tx_id=leg["id"]))


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

    user_id = session.get("user_id", "unknown")
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

    user_id = session.get("user_id", "unknown")
    if not objet.startswith(f"staging/{user_id}/"):
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

    updated, errors = al.attach_receipt(
        tx_id, dest_path, nom, content_type, int(blob.size or 0)
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
    return jsonify({"ok": True})


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


@admin_bp.route("/comptes/<account_id>")
@login_required
def account_detail(account_id: str):
    account = al.get_account(account_id)
    if not account:
        return render_template("errors/404.html"), 404
    return render_template(
        "administration/account_detail.html", account=account,
        header=_account_header(account),
        reconciliations=al.list_reconciliations(account_id), **_labels(),
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
        return render_template("administration/reconciliation_form.html",
                               accounts=accounts, errors=[], form={}, **_labels())
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
    try:
        txs, truncated = al.list_register(account_id, date_from=date_from, date_to=date_to)
    except Exception:
        log_unexpected("admin register read failed")
        txs, truncated = [], False
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
            "categorie": ADMIN_CATEGORY_LABELS.get(tx.get("category") or "", ""),
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
    )
