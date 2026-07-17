"""Trust accounting routes — « comptabilité en fidéicommis » (Phase K).

Journal de caisse + carte-client (two views of one register), account
management, bank reconciliation, and CSV/PDF exports. All @login_required,
French UI. Standard POST+redirect with inline error boxes; HTMX only for the
autocompletes and the reconciliation live variance. No request-size or CSRF
exemption is needed (ordinary form POSTs).
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import escape

from auth import login_required
from models.dossier import get_dossier, list_dossiers
from models import trust
from models.trust import (
    ACCOUNT_STATUS_LABELS,
    ACCOUNT_TYPE_LABELS,
    BARREAU_COLUMNS,
    DIRECTION_LABELS,
    METHOD_LABELS,
    PURPOSE_LABELS,
    RECONCILIATION_STATUS_LABELS,
    TX_STATUS_LABELS,
    VALID_ACCOUNT_TYPES,
    VALID_DIRECTIONS,
    VALID_METHODS,
    VALID_PURPOSES,
    VALID_TX_STATUSES,
)
from pagination import PAGE_SIZE, cursor_pagination, parse_trail
from security import safe_internal_redirect
from utils.logging_setup import log_trust_event

trust_bp = Blueprint("trust", __name__, url_prefix="/fideicommis")


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
    """Parse a fr-CA / en amount string ("1 234,56", "1234.56", "$1,000") into
    integer cents, or None when blank/invalid."""
    if raw is None:
        return None
    s = str(raw).strip().replace(" ", " ").replace(" ", "").replace("$", "")
    if not s:
        return None
    # Treat comma as decimal separator (fr-CA), tolerate a trailing/removed one.
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
        "purpose_labels": PURPOSE_LABELS,
        "method_labels": METHOD_LABELS,
        "direction_labels": DIRECTION_LABELS,
        "tx_status_labels": TX_STATUS_LABELS,
        "account_type_labels": ACCOUNT_TYPE_LABELS,
        "account_status_labels": ACCOUNT_STATUS_LABELS,
        "reconciliation_status_labels": RECONCILIATION_STATUS_LABELS,
        "valid_purposes": VALID_PURPOSES,
        "valid_methods": VALID_METHODS,
        "valid_directions": VALID_DIRECTIONS,
        "valid_tx_statuses": VALID_TX_STATUSES,
        "valid_account_types": VALID_ACCOUNT_TYPES,
        "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _account_header(account: dict) -> dict:
    """The journal/account header block: book, bank, outstanding, in-transit,
    last reconciliation + overdue badge."""
    account_id = account["id"]
    outstanding = trust.list_outstanding(account_id)
    in_transit = trust.list_in_transit(account_id)
    completed = [
        r for r in trust.list_reconciliations(account_id) if r.get("status") == "complétée"
    ]
    last_date = max(
        (trust._as_utc(r.get("period_end")) for r in completed), default=None
    )
    return {
        "book_balance": account.get("book_balance", 0),
        "bank_balance": account.get("bank_balance", 0),
        "outstanding_count": len(outstanding),
        "outstanding_total": sum(int(e.get("amount", 0)) for e in outstanding),
        "in_transit_count": len(in_transit),
        "in_transit_total": sum(int(e.get("amount", 0)) for e in in_transit),
        "last_reconciliation_date": last_date,
        "reconciliation_overdue": trust._reconciliation_overdue(last_date),
    }


# ── Autocompletes (HTMX) ───────────────────────────────────────────────────


@trust_bp.route("/dossier-search")
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
        # The dossier's clients ride along on the row so the client <select> can
        # be populated with no second request (a dossier has one or two clients).
        clients = [
            {"id": c.get("id", ""), "name": c.get("name", "")}
            for c in d.get("clients", [])
        ]
        parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' data-dossier-id="{escape(d["id"])}"'
            f' data-dossier-file-number="{escape(d.get("file_number", ""))}"'
            f' data-dossier-title="{escape(d.get("title", ""))}"'
            f' data-clients="{escape(json.dumps(clients, ensure_ascii=False))}">'
            f'<span class="font-medium text-gray-900">{escape(d.get("file_number", ""))}</span>'
            f'<span class="text-gray-500 ml-1">{escape(d.get("title", ""))}</span></li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


@trust_bp.route("/client-search")
@login_required
def client_search() -> str:
    """Clients of ONE dossier only (§4.3 scope) — funds are held for a client."""
    dossier_id = request.args.get("dossier_id", "").strip()
    if not dossier_id:
        return '<div class="px-3 py-2 text-sm text-gray-500">Choisissez d\'abord un dossier</div>'
    dossier = get_dossier(dossier_id)
    clients = dossier.get("clients", []) if dossier else []
    if not clients:
        return '<div class="px-3 py-2 text-sm text-gray-500">Aucun client au dossier</div>'
    parts = ['<ul class="divide-y divide-gray-100">']
    for c in clients:
        parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' data-client-id="{escape(c.get("id", ""))}"'
            f' data-client-name="{escape(c.get("name", ""))}">'
            f'{escape(c.get("name", ""))}</li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


@trust_bp.route("/counterparty-suggest")
@login_required
def counterparty_suggest() -> str:
    """Suggest the dossier's parties as TEXT (stored as a string, never a FK)."""
    dossier_id = request.args.get("dossier_id", "").strip()
    dossier = get_dossier(dossier_id) if dossier_id else None
    names = []
    if dossier:
        for group in ("clients", "opposing_parties"):
            names.extend(p.get("name", "") for p in dossier.get(group, []))
    names = [n for n in names if n]
    if not names:
        return ""
    parts = ['<ul class="divide-y divide-gray-100">']
    for n in names:
        parts.append(
            f'<li class="px-3 py-2 cursor-pointer hover:bg-gray-50 text-sm"'
            f' data-counterparty="{escape(n)}">{escape(n)}</li>'
        )
    parts.append("</ul>")
    return "\n".join(parts)


# ── Journal de caisse ──────────────────────────────────────────────────────


@trust_bp.route("/")
@login_required
def journal():
    accounts = trust.list_accounts()
    if not accounts:
        return render_template("trust/list.html", accounts=[], account=None, rows=[],
                               pagination=None, header=None, filters={}, **_labels())

    account_id = request.args.get("account_id") or accounts[0]["id"]
    account = next((a for a in accounts if a["id"] == account_id), accounts[0])
    account_id = account["id"]

    status = request.args.get("status") or None
    direction = request.args.get("direction") or None
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    if status not in VALID_TX_STATUSES:
        status = None
    if direction not in VALID_DIRECTIONS:
        direction = None

    filtered = bool(status or direction or date_from or date_to)
    pagination = None
    if filtered:
        rows = trust.list_transactions(
            account_id=account_id, status=status, direction=direction,
            date_from=date_from, date_to=date_to, limit=200,
        )
        rows = list(reversed(rows))  # newest first for display
    else:
        cursor = request.args.get("cursor", "") or None
        trail = parse_trail(request.args.get("trail", ""))
        rows, next_cursor = trust.list_transactions_page(
            account_id, cursor=cursor, limit=PAGE_SIZE
        )
        pagination = cursor_pagination(
            cursor=cursor, trail=trail, next_cursor=next_cursor,
            url=url_for("trust.journal"), target="#trust-rows",
            extra_vals={"account_id": account_id},
        )

    ctx = dict(
        accounts=accounts, account=account, rows=rows, pagination=pagination,
        header=_account_header(account),
        filters={"status": status or "", "direction": direction or "",
                 "date_from": request.args.get("date_from", ""),
                 "date_to": request.args.get("date_to", ""), "account_id": account_id},
        **_labels(),
    )
    if _is_htmx():
        return render_template("trust/_transaction_rows.html", **ctx)
    return render_template("trust/list.html", **ctx)


# ── Entry create ───────────────────────────────────────────────────────────


def _entry_form_data() -> dict:
    f = request.form
    return {
        "account_id": f.get("account_id", "").strip(),
        "direction": f.get("direction", "").strip(),
        "amount": _parse_cents(f.get("amount", "")),
        "purpose": f.get("purpose", "").strip(),
        "method": f.get("method", "").strip(),
        "counterparty": f.get("counterparty", "").strip(),
        "dossier_id": f.get("dossier_id", "").strip() or None,
        "client_id": f.get("client_id", "").strip() or None,
        # Two mutually-exclusive ways to back a fee transfer: a Pallas Athéna
        # invoice NUMBER (resolved to an id below) or an external number.
        "invoice_number": f.get("invoice_number", "").strip(),
        "invoice_external_ref": f.get("invoice_external_ref", "").strip(),
        "reference": f.get("reference", "").strip(),
        "description": f.get("description", "").strip(),
        "date": _parse_date(f.get("date", "")),
    }


def _resolve_invoice_number(data: dict) -> list[str]:
    """Resolve the Pallas Athéna invoice NUMBER (e.g. « 2026-F001 ») to its id
    within the dossier, setting ``data['invoice_id']``. A number that does not
    resolve is a HARD error — never silently treated as external, which would
    skip the amount check. Only meaningful for a fee transfer."""
    data["invoice_id"] = None
    number = (data.get("invoice_number") or "").strip()
    if data.get("purpose") != "virement_honoraires" or not number:
        return []
    dossier_id = data.get("dossier_id")
    if not dossier_id:
        return ["Sélectionnez le dossier avant d'indiquer une facture."]
    from models.invoice import list_invoices

    matches = [
        inv
        for inv in list_invoices(dossier_id=dossier_id)
        if inv.get("invoice_number") == number
    ]
    if not matches:
        return [f"Aucune facture « {number} » dans ce dossier de Pallas Athéna."]
    data["invoice_id"] = matches[0]["id"]
    return []


@trust_bp.route("/nouvelle")
@login_required
def entry_new():
    accounts = trust.list_accounts(status="actif")
    dossier_id = request.args.get("dossier_id", "").strip() or None
    locked = request.args.get("locked") == "1"
    dossier = get_dossier(dossier_id) if dossier_id else None
    return render_template(
        "trust/form.html", accounts=accounts, entry=None, dossier=dossier,
        locked=locked, errors=[], **_labels(),
    )


@trust_bp.route("/", methods=["POST"])
@login_required
def entry_create():
    data = _entry_form_data()
    # correction is reserved for reversals — refuse it at the route (spec §7).
    if data.get("purpose") == "correction":
        data["purpose"] = ""
    errors = _resolve_invoice_number(data)
    entry = None
    if not errors:
        entry, errors = trust.create_transaction(data)
    if errors:
        accounts = trust.list_accounts(status="actif")
        dossier = get_dossier(data["dossier_id"]) if data.get("dossier_id") else None
        return render_template(
            "trust/form.html", accounts=accounts, entry=data, dossier=dossier,
            locked=request.form.get("locked") == "1", errors=errors, **_labels(),
        ), 400
    return redirect(url_for("trust.entry_detail", tx_id=entry["id"]))


@trust_bp.route("/<tx_id>")
@login_required
def entry_detail(tx_id: str):
    entry = trust.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    account = trust.get_account(entry.get("account_id"))
    reversal = trust.get_transaction(entry["reversed_by_id"]) if entry.get("reversed_by_id") else None
    reverses = trust.get_transaction(entry["reverses_id"]) if entry.get("reverses_id") else None
    other_leg = (
        trust.get_transaction(entry["related_transaction_id"])
        if entry.get("related_transaction_id") else None
    )
    return render_template(
        "trust/detail.html", entry=entry, account=account, reversal=reversal,
        reverses=reverses, other_leg=other_leg, **_labels(),
    )


# ── Clearing ───────────────────────────────────────────────────────────────


@trust_bp.route("/<tx_id>/compenser", methods=["POST"])
@login_required
def entry_clear(tx_id: str):
    cleared_date = _parse_date(request.form.get("cleared_date", "")) or datetime.now(timezone.utc)
    _, errors = trust.clear_transaction(tx_id, cleared_date)
    return_to = safe_internal_redirect(
        request.form.get("return_to", ""), url_for("trust.entry_detail", tx_id=tx_id)
    )
    return redirect(return_to)


@trust_bp.route("/compenser-lot", methods=["POST"])
@login_required
def entry_clear_bulk():
    tx_ids = request.form.getlist("tx_ids")
    cleared_date = _parse_date(request.form.get("cleared_date", "")) or datetime.now(timezone.utc)
    trust.clear_transactions_bulk(tx_ids, cleared_date)
    return_to = safe_internal_redirect(
        request.form.get("return_to", ""), url_for("trust.journal")
    )
    return redirect(return_to)


# ── Reversal (contre-passation) ────────────────────────────────────────────


@trust_bp.route("/<tx_id>/contrepasser")
@login_required
def entry_reverse_confirm(tx_id: str):
    entry = trust.get_transaction(tx_id)
    if not entry:
        return render_template("errors/404.html"), 404
    return render_template("trust/reverse_confirm.html", entry=entry, errors=[], **_labels())


@trust_bp.route("/<tx_id>/contrepasser", methods=["POST"])
@login_required
def entry_reverse(tx_id: str):
    reason = request.form.get("reason", "").strip()
    reversal, errors = trust.reverse_transaction(tx_id, reason)
    if errors:
        entry = trust.get_transaction(tx_id)
        return render_template(
            "trust/reverse_confirm.html", entry=entry, errors=errors, **_labels()
        ), 400
    return redirect(url_for("trust.entry_detail", tx_id=reversal["id"]))


# ── Inter-dossier transfer ─────────────────────────────────────────────────


@trust_bp.route("/virement", methods=["GET", "POST"])
@login_required
def transfer():
    accounts = trust.list_accounts(status="actif")
    if request.method == "GET":
        return render_template("trust/transfer_form.html", accounts=accounts, errors=[], form={}, **_labels())
    f = request.form
    leg, errors = trust.create_inter_dossier_transfer(
        account_id=f.get("account_id", "").strip(),
        from_dossier_id=f.get("from_dossier_id", "").strip(),
        from_client_id=f.get("from_client_id", "").strip(),
        to_dossier_id=f.get("to_dossier_id", "").strip(),
        to_client_id=f.get("to_client_id", "").strip(),
        amount=_parse_cents(f.get("amount", "")) or 0,
        description=f.get("description", "").strip(),
        method=f.get("method", "virement").strip(),
        reference=f.get("reference", "").strip(),
    )
    if errors:
        return render_template(
            "trust/transfer_form.html", accounts=accounts, errors=errors,
            form=f.to_dict(), **_labels(),
        ), 400
    return redirect(url_for("trust.entry_detail", tx_id=leg["id"]))


# ── Carte-client + consolidated client view ────────────────────────────────


@trust_bp.route("/carte/<dossier_id>/<client_id>")
@login_required
def card(dossier_id: str, client_id: str):
    dossier = get_dossier(dossier_id)
    if not dossier:
        return render_template("errors/404.html"), 404
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    rows = trust.list_card_transactions(dossier_id, client_id, date_from, date_to)
    client_name = next(
        (c.get("name", "") for c in dossier.get("clients", []) if c.get("id") == client_id), ""
    )
    book = int((dossier.get("trust_balance_by_client") or {}).get(client_id, 0))
    cleared = int((dossier.get("trust_cleared_by_client") or {}).get(client_id, 0))
    return render_template(
        "trust/card.html", dossier=dossier, client_id=client_id,
        client_name=client_name, rows=rows,
        book_cents=book, cleared_cents=cleared, in_transit_cents=book - cleared,
        filters={"date_from": request.args.get("date_from", ""),
                 "date_to": request.args.get("date_to", "")},
        **_labels(),
    )


@trust_bp.route("/client/<client_id>")
@login_required
def client_consolidated(client_id: str):
    """« Vue de gestion » across dossiers — NOT a register, no export, no control."""
    from models.dossier import list_dossiers_for_partie

    dossiers = list_dossiers_for_partie(client_id)
    rows = []
    total = 0
    for d in dossiers:
        book = int((d.get("trust_balance_by_client") or {}).get(client_id, 0))
        cleared = int((d.get("trust_cleared_by_client") or {}).get(client_id, 0))
        if book == 0 and cleared == 0:
            continue
        rows.append({
            "dossier_id": d["id"], "file_number": d.get("file_number", ""),
            "title": d.get("title", ""), "book_cents": book,
            "cleared_cents": cleared, "in_transit_cents": book - cleared,
        })
        total += book
    return render_template(
        "trust/client_consolidated.html", client_id=client_id, rows=rows,
        total_cents=total, **_labels(),
    )


# ── Accounts ───────────────────────────────────────────────────────────────


@trust_bp.route("/comptes/")
@login_required
def accounts_list():
    return render_template("trust/accounts_list.html", accounts=trust.list_accounts(), **_labels())


@trust_bp.route("/comptes/nouveau", methods=["GET", "POST"])
@login_required
def account_new():
    if request.method == "GET":
        return render_template("trust/account_form.html", account=None, errors=[], **_labels())
    account, errors = trust.create_account(_account_form_data())
    if errors:
        return render_template(
            "trust/account_form.html", account=request.form.to_dict(), errors=errors, **_labels()
        ), 400
    return redirect(url_for("trust.account_detail", account_id=account["id"]))


@trust_bp.route("/comptes/<account_id>")
@login_required
def account_detail(account_id: str):
    account = trust.get_account(account_id)
    if not account:
        return render_template("errors/404.html"), 404
    return render_template(
        "trust/account_detail.html", account=account, header=_account_header(account),
        reconciliations=trust.list_reconciliations(account_id), **_labels(),
    )


@trust_bp.route("/comptes/<account_id>/edit", methods=["GET", "POST"])
@login_required
def account_edit(account_id: str):
    account = trust.get_account(account_id)
    if not account:
        return render_template("errors/404.html"), 404
    if request.method == "GET":
        return render_template("trust/account_form.html", account=account, errors=[], **_labels())
    updated, errors = trust.update_account(account_id, _account_form_data())
    if errors:
        merged = {**account, **request.form.to_dict()}
        return render_template("trust/account_form.html", account=merged, errors=errors, **_labels()), 400
    return redirect(url_for("trust.account_detail", account_id=account_id))


def _account_form_data() -> dict:
    f = request.form
    return {
        "name": f.get("name", "").strip(),
        "account_type": f.get("account_type", "général").strip(),
        "institution": f.get("institution", "").strip(),
        "transit": f.get("transit", "").strip(),
        "account_number_last4": f.get("account_number_last4", "").strip(),
        "status": f.get("status", "actif").strip(),
        "notes": f.get("notes", "").strip(),
    }


# ── Reconciliation ─────────────────────────────────────────────────────────


@trust_bp.route("/conciliations/")
@login_required
def reconciliations_list():
    return render_template(
        "trust/reconciliations_list.html",
        reconciliations=trust.list_reconciliations(),
        accounts={a["id"]: a for a in trust.list_accounts()}, **_labels(),
    )


@trust_bp.route("/conciliations/nouvelle", methods=["GET", "POST"])
@login_required
def reconciliation_new():
    accounts = trust.list_accounts(status="actif")
    if request.method == "GET":
        return render_template("trust/reconciliation_form.html", accounts=accounts, errors=[], form={}, **_labels())
    f = request.form
    rec, errors = trust.create_reconciliation(
        account_id=f.get("account_id", "").strip(),
        period_end=_parse_date(f.get("period_end", "")),
        statement_balance=_parse_cents(f.get("statement_balance", "")) or 0,
    )
    if errors:
        return render_template(
            "trust/reconciliation_form.html", accounts=accounts, errors=errors,
            form=f.to_dict(), **_labels(),
        ), 400
    return redirect(url_for("trust.reconciliation_worksheet", rec_id=rec["id"]))


@trust_bp.route("/conciliations/<rec_id>")
@login_required
def reconciliation_worksheet(rec_id: str):
    rec = trust.get_reconciliation(rec_id)
    if not rec:
        return render_template("errors/404.html"), 404
    account = trust.get_account(rec["account_id"])
    outstanding = trust.list_outstanding(rec["account_id"])
    in_transit = trust.list_in_transit(rec["account_id"])
    return render_template(
        "trust/reconciliation_worksheet.html", rec=rec, account=account,
        outstanding=outstanding, in_transit=in_transit, **_labels(),
    )


@trust_bp.route("/conciliations/<rec_id>/completer", methods=["POST"])
@login_required
def reconciliation_complete(rec_id: str):
    cleared_ids = request.form.getlist("cleared_tx_ids")
    rec, errors = trust.complete_reconciliation(rec_id, cleared_ids)
    if errors:
        current = trust.get_reconciliation(rec_id)
        account = trust.get_account(current["account_id"]) if current else None
        outstanding = trust.list_outstanding(current["account_id"]) if current else []
        in_transit = trust.list_in_transit(current["account_id"]) if current else []
        return render_template(
            "trust/reconciliation_worksheet.html", rec=current, account=account,
            outstanding=outstanding, in_transit=in_transit, errors=errors, **_labels(),
        ), 400
    return redirect(url_for("trust.reconciliation_worksheet", rec_id=rec_id))


# ── Exports (spec §8) — TWO-column « Recette » / « Crédit » ─────────────────

# CSV: (key, label). PDF: (key, label, width_ratio). Both consume to_barreau_row.
_CSV_COLUMNS = list(BARREAU_COLUMNS)
_PDF_WIDTHS = [8, 10, 20, 20, 14, 10, 9, 9, 9]
_PDF_COLUMNS = [(k, label, w) for (k, label), w in zip(BARREAU_COLUMNS, _PDF_WIDTHS)]
_CENTS_KEYS = list(trust.BARREAU_CENTS_KEYS)


def _export_rows(txs: list[dict], view: str) -> list[dict]:
    """Project + mark rows for export (§8.3): the date is pre-rendered to a
    string so an « * » can flag an en_circulation (uncleared) row; « (annulée) »
    is already applied to « Objet » by to_barreau_row."""
    out = []
    for tx in txs:
        row = trust.to_barreau_row(tx, view)
        d = trust._as_utc(row.get("date"))
        s = d.strftime("%Y-%m-%d") if isinstance(d, datetime) else ""
        if tx.get("status") == "en_circulation":
            s = f"{s} *"
        row["date"] = s
        out.append(row)
    return out


@trust_bp.route("/export/<fmt>")
@login_required
def journal_export(fmt: str):
    account_id = request.args.get("account_id", "").strip()
    account = trust.get_account(account_id) if account_id else None
    if account is None:
        accounts = trust.list_accounts()
        if not accounts:
            return "Aucun compte", 404
        account = accounts[0]
        account_id = account["id"]
    status = request.args.get("status") or None
    direction = request.args.get("direction") or None
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))
    if status not in VALID_TX_STATUSES:
        status = None
    if direction not in VALID_DIRECTIONS:
        direction = None
    txs = trust.list_transactions(
        account_id=account_id, status=status, direction=direction,
        date_from=date_from, date_to=date_to, limit=5000,
    )
    rows = _export_rows(txs, "journal")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subtitle = (
        f"Compte : {account.get('name', '')} — {account.get('institution', '')}"
        "  ·  * = en circulation (non compensé)"
    )
    log_trust_event("trust_export", format=fmt, view="journal", row_count=len(rows))
    return _render_export(fmt, rows, "Journal de caisse — recettes et déboursés",
                          subtitle, f"journal_fideicommis_{day}")


@trust_bp.route("/carte/<dossier_id>/<client_id>/export/<fmt>")
@login_required
def card_export(dossier_id: str, client_id: str, fmt: str):
    dossier = get_dossier(dossier_id)
    if not dossier:
        return "Dossier introuvable", 404
    txs = trust.list_card_transactions(dossier_id, client_id)
    rows = _export_rows(txs, "carte")
    client_name = next(
        (c.get("name", "") for c in dossier.get("clients", []) if c.get("id") == client_id), ""
    )
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subtitle = f"Client : {client_name} — Dossier : {dossier.get('file_number', '')}"
    log_trust_event("trust_export", format=fmt, view="carte", row_count=len(rows))
    return _render_export(fmt, rows, "Carte-client", subtitle,
                          f"carte_{dossier.get('file_number', 'client')}_{day}")


def _render_export(fmt: str, rows: list[dict], title: str, subtitle: str, filename: str):
    if fmt == "csv":
        from utils.export_csv import export_csv
        return export_csv(
            rows=rows, columns=_CSV_COLUMNS, filename=f"{filename}.csv",
            cents_fields=_CENTS_KEYS,
        )
    if fmt == "pdf":
        from utils.export_pdf import export_pdf
        return export_pdf(
            rows=rows, columns=_PDF_COLUMNS, title=title, subtitle=subtitle,
            filename=f"{filename}.pdf", cents_fields=_CENTS_KEYS, landscape=True,
        )
    return "Format non supporté", 400
