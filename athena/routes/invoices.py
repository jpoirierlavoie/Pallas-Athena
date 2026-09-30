"""Invoice management routes — list, create, detail, status updates."""

from datetime import datetime, timezone
from typing import Optional

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
from pagination import (
    PAGE_SIZE,
    cursor_pagination,
    paginate,
    parse_trail,
    resolve_page,
    total_pages_of,
)
from security import safe_internal_redirect, sanitize
from utils import storage_identity
from utils.cabinet import cabinet_dict
from utils.format_fr import format_rate_fr
from models.invoice import (
    DRAFT_FIELDS,
    DRAFT_NOTES_MAX_LENGTH,
    DRAFT_PAYMENT_TERMS_MAX_LENGTH,
    STATUS_LABELS,
    VALID_STATUSES,
    available_transitions,
    balance_of,
    billing_address_from,
    count_invoices_page,
    create_invoice,
    delete_invoice,
    draft_edit_refusal,
    expense_split,
    get_invoice,
    get_invoice_with_items,
    list_invoices,
    list_invoices_page,
    list_line_items,
    number_year_warning,
    update_invoice_draft,
    update_status,
    void_invoice_report,
)
from models.admin_ledger import (
    METHOD_LABELS,
    TX_STATUS_LABELS,
    list_invoice_receipts,
)
from models.audit_event import record_deletion
from models.trust import list_invoice_fee_payments
from models.dossier import get_dossier, list_dossiers
from models.folder import get_folder
from models.partie import get_partie
from models.time_entry import get_unbilled_time_entries
from models.expense import get_unbilled_expenses
from routes import edit_conflict
from services import note_honoraires
from routes._helpers import is_htmx, parse_date_input

invoices_bp = Blueprint("invoices", __name__, url_prefix="/factures")


_is_htmx = is_htmx


_parse_date = parse_date_input


def _template_context() -> dict:
    """Return shared template context for invoice views.

    No firm block: the client-facing document is the Word note d'honoraires
    (``/factures/<id>/note-docx``, whose letterhead comes from the gabarit),
    and the detail page is a DATA sheet — it never restates the firm's own
    identity. The tax numbers a given invoice was issued under live on the
    invoice itself (``gst_number``/``qst_number``, snapshotted at creation),
    which is what the sheet shows.
    """
    return {"status_labels": STATUS_LABELS}


# ── Invoice list ─────────────────────────────────────────────────────────


@invoices_bp.route("/")
@login_required
def invoice_list() -> str:
    """Render the invoice list with optional filters."""
    status_filter = request.args.get("status", "")
    # Normalize garbage status values to "no filter" so the list, its
    # pagination, and the export links all agree on the same semantics
    # (legacy list_invoices returned 0 rows; list_invoices_page would
    # silently ignore the filter).
    if status_filter not in VALID_STATUSES:
        status_filter = ""
    dossier_id = request.args.get("dossier_id", "").strip()
    date_from = _parse_date(request.args.get("date_from", ""))
    date_to = _parse_date(request.args.get("date_to", ""))

    if dossier_id:
        # Dossier-scoped fallback: reached only via deep links, and
        # list_invoices already narrows server-side on dossier_id, so the
        # scan is bounded by that dossier's invoice count. Sliced with the
        # legacy paginate() — not worth a third composite index.
        page = request.args.get("page", 1, type=int)
        invoices = list_invoices(
            status_filter=status_filter or None,
            dossier_id=dossier_id,
            date_from=date_from,
            date_to=date_to,
        )
        invoices, pagination = paginate(invoices, page)
        pagination["url"] = url_for("invoices.invoice_list")
        pagination["target"] = "#invoice-rows"
        # The filter inputs travel via hx-include; dossier_id is not a form
        # field, so it must ride along explicitly on next/prev clicks.
        pagination["extra_vals"] = {"dossier_id": dossier_id}
    else:
        # Default browse path: Firestore-native cursor pagination
        # (~PAGE_SIZE reads per page). Status and the date range are
        # pushed server-side by list_invoices_page.
        cursor = request.args.get("cursor", "") or None
        trail = parse_trail(request.args.get("trail", ""))
        # The count is issued on the CURSOR branch only. It rides the same
        # query builder as the page read, so the same index serves both,
        # and it fails to None (never 0) — which HIDES the leap controls
        # rather than asserting a total the code does not have.
        total = count_invoices_page(
            status_filter=status_filter or None,
            date_from=date_from,
            date_to=date_to,
        )
        page_no, page_offset = resolve_page(
            request.args.get("page", type=int),
            total_pages_of(total),
            has_cursor=bool(cursor),
        )
        if page_offset:
            cursor, trail = None, []
        invoices, next_cursor = list_invoices_page(
            status_filter=status_filter or None,
            date_from=date_from,
            date_to=date_to,
            limit=PAGE_SIZE,
            cursor=cursor,
            offset=page_offset,
        )
        # No extra_vals needed: pagination links hx-include
        # "#filters input, #filters select", which carries the active
        # status + date filters on every next/prev click.
        pagination = cursor_pagination(
            cursor=cursor,
            trail=trail,
            next_cursor=next_cursor,
            url=url_for("invoices.invoice_list"),
            target="#invoice-rows",
            page=page_no,
            total=total,
        )

    # Le solde VIVANT, annoté côté route — jamais calculé dans le gabarit :
    # une facture héritée peut n'avoir NI `amount_due` NI `amount_paid`, et une
    # soustraction en Jinja lèverait là où `balance_of` tolère l'absence.
    # (Le patron est celui de routes/admin_ledger._balance.)
    for inv in invoices:
        inv["_balance"] = balance_of(inv)

    ctx = _template_context()
    ctx.update(
        invoices=invoices,
        status_filter=status_filter,
        dossier_id=dossier_id,
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        pagination=pagination,
    )

    if _is_htmx():
        return render_template("invoices/_invoice_rows.html", **ctx)

    return render_template("invoices/list.html", **ctx)


# ── Invoice creation flow ────────────────────────────────────────────────


@invoices_bp.route("/new")
@login_required
def invoice_new() -> str:
    """Step 1: Select a dossier, then show unbilled items."""
    dossier_id = request.args.get("dossier_id", "").strip()
    return_to = request.args.get("return_to", "")

    ctx = _template_context()
    ctx["return_to"] = return_to

    if not dossier_id:
        # Show dossier selector
        dossiers = list_dossiers(status_filter="actif")
        ctx["dossiers"] = dossiers
        ctx["errors"] = []
        return render_template("invoices/create.html", **ctx)

    # Load dossier + unbilled items
    dossier = get_dossier(dossier_id)
    if not dossier:
        ctx["dossiers"] = list_dossiers(status_filter="actif")
        ctx["errors"] = ["Dossier introuvable."]
        return render_template("invoices/create.html", **ctx)

    unbilled_entries = get_unbilled_time_entries(dossier_id)
    unbilled_expenses = get_unbilled_expenses(dossier_id)

    # Get first client info for billing address
    client_partie = None
    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    clients = dossier.get("clients", [])
    if clients:
        client_partie = get_partie(clients[0].get("id", ""))
        if client_partie:
            billing_address = billing_address_from(client_partie)

    today = datetime.now(timezone.utc)

    ctx.update(
        dossier=dossier,
        unbilled_entries=unbilled_entries,
        unbilled_expenses=unbilled_expenses,
        billing_address=billing_address,
        client_name=clients[0].get("name", "") if clients else "",
        client_id=clients[0].get("id", "") if clients else "",
        invoice_date=today.strftime("%Y-%m-%d"),
        errors=[],
    )
    return render_template("invoices/create.html", **ctx)


@invoices_bp.route("/unbilled/<dossier_id>")
@login_required
def unbilled_items(dossier_id: str) -> str:
    """HTMX endpoint: return unbilled items for a dossier."""
    dossier = get_dossier(dossier_id)
    if not dossier:
        return '<p class="text-red-600 text-sm">Dossier introuvable.</p>', 404

    unbilled_entries = get_unbilled_time_entries(dossier_id)
    unbilled_expenses = get_unbilled_expenses(dossier_id)

    # Get client info
    clients = dossier.get("clients", [])
    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    if clients:
        client_partie = get_partie(clients[0].get("id", ""))
        if client_partie:
            billing_address = billing_address_from(client_partie)

    today = datetime.now(timezone.utc)

    ctx = _template_context()
    ctx.update(
        dossier=dossier,
        unbilled_entries=unbilled_entries,
        unbilled_expenses=unbilled_expenses,
        billing_address=billing_address,
        client_name=clients[0].get("name", "") if clients else "",
        client_id=clients[0].get("id", "") if clients else "",
        invoice_date=today.strftime("%Y-%m-%d"),
        errors=[],
    )
    return render_template("invoices/_unbilled_items.html", **ctx)


@invoices_bp.route("/", methods=["POST"])
@login_required
def invoice_create() -> str:
    """Handle invoice creation form submission."""
    f = request.form
    dossier_id = f.get("dossier_id", "").strip()
    return_to = f.get("return_to", "")

    dossier = get_dossier(dossier_id)
    if not dossier:
        ctx = _template_context()
        ctx["dossiers"] = list_dossiers(status_filter="actif")
        ctx["errors"] = ["Dossier introuvable."]
        ctx["return_to"] = return_to
        return render_template("invoices/create.html", **ctx)

    # Collect selected items
    selected_entry_ids = f.getlist("selected_entries")
    selected_expense_ids = f.getlist("selected_expenses")

    # Build client info
    clients = dossier.get("clients", [])
    client_name = clients[0].get("name", "") if clients else ""
    client_id = clients[0].get("id", "") if clients else ""

    billing_address = {"name": "", "street": "", "unit": "", "city": "", "province": "QC", "postal_code": ""}
    if client_id:
        client_partie = get_partie(client_id)
        if client_partie:
            billing_address = billing_address_from(client_partie)

    # The tax numbers are SNAPSHOTTED onto the invoice at creation and never
    # rewritten (a draft correction reaches no money figure and no tax
    # number) — an invoice reads back under the numbers it was issued with.
    # Only their source changed: the settings/cabinet singleton instead of
    # Config.FIRM_*. The model refuses to issue an invoice charging a tax
    # under an EMPTY number (models.invoice.issuance_refusals).
    #
    # No provision (retainer_applied) is sent: an invoice issued here is
    # born with none — a provision is applied after sending, by a
    # « paiement d'honoraires » drawn on the trust account — and the model
    # forces it to 0 on this path whatever arrives. The form's provision
    # field (rendered only under a `retainer_balance` nothing ever wrote)
    # went with it.
    _cab = cabinet_dict()

    data = {
        "dossier_id": dossier_id,
        "dossier_file_number": dossier.get("file_number", ""),
        "dossier_title": dossier.get("title", ""),
        "client_id": client_id,
        "client_name": client_name,
        "billing_address": billing_address,
        "date": _parse_date(f.get("invoice_date", "")),
        "due_date": _parse_date(f.get("due_date", "")),
        "notes": f.get("notes", "").strip(),
        "payment_terms": f.get("payment_terms", "").strip(),
        "gst_number": _cab.get("gst_number", ""),
        "qst_number": _cab.get("qst_number", ""),
    }

    # require_all_sources: a selection that went stale while the page was
    # open (an entry billed in another tab, moved, deleted — or marked
    # non-billable) is REFUSED by name. Without it the model skipped such a
    # source in silence and issued a shorter invoice than the one the page
    # showed.
    invoice, errors = create_invoice(
        dossier_id, selected_entry_ids, selected_expense_ids, data,
        require_all_sources=True,
    )

    if errors:
        unbilled_entries = get_unbilled_time_entries(dossier_id)
        unbilled_expenses = get_unbilled_expenses(dossier_id)
        # The refusal re-renders what the lawyer SUBMITTED — the selection
        # above all. The page's default is « every unbilled item ticked »,
        # so without this a refused save (a stale entry, an empty tax
        # number…) silently re-ticked the entries the lawyer had
        # deliberately left out, and the next « Créer » billed them. Only
        # ids still listed as unbilled are kept: a source billed or moved
        # in the meantime has left the list, and its checkbox with it.
        chosen_entries = set(selected_entry_ids)
        chosen_expenses = set(selected_expense_ids)
        ctx = _template_context()
        ctx.update(
            dossier=dossier,
            unbilled_entries=unbilled_entries,
            unbilled_expenses=unbilled_expenses,
            billing_address=billing_address,
            client_name=client_name,
            client_id=client_id,
            invoice_date=f.get("invoice_date", ""),
            errors=errors,
            return_to=return_to,
            initial_entry_ids=[e.get("id") for e in unbilled_entries
                               if e.get("id") in chosen_entries],
            initial_expense_ids=[e.get("id") for e in unbilled_expenses
                                 if e.get("id") in chosen_expenses],
            form_due_date=f.get("due_date", ""),
            form_notes=f.get("notes", ""),
            form_payment_terms=f.get("payment_terms", ""),
        )
        return render_template("invoices/create.html", **ctx)

    # On success, prefer the caller's URL when supplied (e.g. dossier hub) so the
    # user lands back where they started rather than on the invoice detail —
    # unless there is something to SAY about the invoice just created: its
    # number follows the year of today, not of its date, and the lawyer
    # learns that on the invoice's own sheet, before sending it.
    fallback = url_for("invoices.invoice_detail", invoice_id=invoice["id"])
    target = safe_internal_redirect(return_to, fallback)
    warning = number_year_warning(
        invoice.get("date"), invoice.get("invoice_number", "")
    )
    if warning:
        target = url_for(
            "invoices.invoice_detail", invoice_id=invoice["id"], message=warning
        )
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp

    return redirect(target)


# ── Invoice detail ───────────────────────────────────────────────────────


@invoices_bp.route("/<invoice_id>")
@login_required
def invoice_detail(invoice_id: str) -> str:
    """Render the invoice data sheet.

    A structured reading of what is STORED — never a facsimile of the client
    document: that is the Word note d'honoraires. Nothing here is recomputed
    (``compute_totals`` runs at creation only); the one derived figure is the
    live balance, and it is labelled as such.
    """
    invoice, items = get_invoice_with_items(invoice_id)
    if not invoice:
        return redirect(url_for("invoices.invoice_list"))

    # Split by type. Everything that is not a fee is a disbursement — the
    # same rule utils/invoice_docx.py applies. Matching on == "expense"
    # would let a line item of any other type vanish from a page whose whole
    # purpose is to show what the invoice holds.
    fee_items = [i for i in items if i.get("type") == "fee"]
    expense_items = [i for i in items if i.get("type") != "fee"]

    # The accounting entries imputed on this invoice. READ-ONLY: a payment
    # is recorded in « Administration » and nowhere else, so this sheet
    # reports rather than accepts. Fails open to [] — a display aid must
    # not take the page down.
    paiements = list_invoice_receipts(invoice_id)

    # Available status transitions. available_transitions, jamais la table :
    # elle seule sait qu'un « payée » adossé au grand livre ne se rouvre pas
    # à la main, ni qu'une facture portant un paiement ne s'annule pas. Un
    # bouton qui s'afficherait pour être refusé serait un défaut de
    # conception. Les écritures déjà lues lui sont remises : une écriture
    # debout sous un amount_paid à 0 (la dérive) ferme aussi « Annuler ».
    transitions = available_transitions(invoice, receipts=paiements)
    if "annulée" in transitions:
        # Le dernier cas : un paiement d'honoraires du fidéicommis debout
        # dont la recette d'administration automatique a échoué (fail-open)
        # — ni montant inscrit ni écriture d'administration, mais
        # void_invoice_report refuserait. Lu seulement quand « Annuler »
        # serait autrement offert : une requête de plus, jamais sur une
        # facture déjà fermée à l'annulation.
        transitions = available_transitions(
            invoice, receipts=paiements,
            trust_payments=list_invoice_fee_payments(invoice_id),
        )

    ctx = _template_context()
    ctx.update(
        invoice=invoice,
        fee_items=fee_items,
        expense_items=expense_items,
        transitions=transitions,
        # amount_due is frozen at issuance; the live balance is derived.
        balance=balance_of(invoice),
        # Rates as STORED on this invoice (GST ×100, QST ×1000) rather than
        # today's statutory rates hardcoded in the markup — an invoice issued
        # under a different rate must read back under that rate.
        gst_rate_display=format_rate_fr(invoice.get("gst_rate") or 0, 100),
        qst_rate_display=format_rate_fr(invoice.get("qst_rate") or 0, 1000),
        paiements=paiements,
        method_labels=METHOD_LABELS,
        tx_status_labels=TX_STATUS_LABELS,
        # A brouillon is corrected on its own page (notes, terms, due date,
        # billing address) — never its figures.
        can_edit_draft=not draft_edit_refusal(invoice),
        return_to=request.args.get("return_to", ""),
        # Rebond des actions de la fiche (statut, annulation, suppression) :
        # leur refus —
        # ou ce qu'une annulation a laissé de côté — revient ici en bandeau.
        # htmx n'échange que les 2xx, et ces boutons sont des formulaires
        # pleine page : une redirection est la seule voie qui s'affiche.
        erreur=sanitize(request.args.get("erreur", ""), max_length=500),
        message=sanitize(request.args.get("message", ""), max_length=2000),
    )
    return render_template("invoices/detail.html", **ctx)


# ── Draft correction ────────────────────────────────────────────────────


def _draft_form_values(invoice: dict) -> dict:
    """The form as the STORED brouillon fills it — the due date as the
    date input wants it, formatted here (date-only: strftime, never to_mtl).
    """
    due = invoice.get("due_date")
    return {
        "id": invoice.get("id", ""),
        "etag": invoice.get("etag") or "",
        "notes": invoice.get("notes", "") or "",
        "payment_terms": invoice.get("payment_terms", "") or "",
        "due_date": due.strftime("%Y-%m-%d") if hasattr(due, "strftime") else "",
        "refresh_billing_address": False,
    }


def _render_draft_form(
    invoice: dict,
    form: dict,
    *,
    errors: Optional[list[str]] = None,
    conflict: Optional[dict] = None,
) -> str:
    ctx = _template_context()
    ctx.update(
        invoice=invoice,
        form=form,
        errors=errors or [],
        conflict=conflict,
        notes_max=DRAFT_NOTES_MAX_LENGTH,
        payment_terms_max=DRAFT_PAYMENT_TERMS_MAX_LENGTH,
    )
    return render_template("invoices/draft_form.html", **ctx)


@invoices_bp.route("/<invoice_id>/brouillon")
@login_required
def invoice_draft_edit(invoice_id: str) -> str:
    """The correction form of a brouillon — notes, terms, due date, and a
    fresh snapshot of the client's billing address. Never its figures."""
    invoice = get_invoice(invoice_id)
    if not invoice:
        return redirect(url_for("invoices.invoice_list"))
    refusal = draft_edit_refusal(invoice)
    if refusal:
        return _back_to_detail(invoice_id, erreur=refusal)
    return _render_draft_form(invoice, _draft_form_values(invoice))


@invoices_bp.route("/<invoice_id>/brouillon", methods=["POST"])
@login_required
def invoice_draft_update(invoice_id: str) -> str:
    """Save a brouillon correction, guarded by the version the page showed.

    A refusal re-renders at 200 with the SUBMITTED values (full-page form;
    htmx only swaps a 2xx): a stale save with the amber banner and the
    CURRENT etag, a validation error with the submitted one
    (``routes/edit_conflict``). A draft that stopped being one in the
    meantime — sent from another tab — sends the lawyer back to its sheet
    with the reason: there is nothing left to correct here.
    """
    expected = edit_conflict.submitted_etag()
    f = request.form
    # By PRESENCE, never ``f.get(k, "")``: on this model a present empty
    # key ERASES (« '' clears » the notes) while an absent one survives, so
    # a POST lacking a field must leave that field alone rather than blank
    # it. The page always posts all three.
    changes = {k: f[k] for k in DRAFT_FIELDS if k in f}
    refresh = f.get("refresh_billing_address") == "on"
    _doc, errors, _changed = update_invoice_draft(
        invoice_id, changes,
        expected_etag=expected, refresh_billing_address=refresh,
    )
    if not errors:
        return _back_to_detail(invoice_id)

    errors, conflict, etag = edit_conflict.resolve_refusal(
        errors,
        submitted=expected,
        reread=lambda: get_invoice(invoice_id),
        compare_url=url_for("invoices.invoice_detail", invoice_id=invoice_id),
    )
    current = get_invoice(invoice_id)
    if not current:
        return redirect(url_for("invoices.invoice_list"))
    refusal = draft_edit_refusal(current)
    if refusal:
        return _back_to_detail(invoice_id, erreur=refusal)
    # The submitted values where the POST carried them, the stored ones
    # otherwise — a field the POST left out was left alone by the model.
    form = {
        **_draft_form_values(current),
        **changes,
        "id": invoice_id,
        "etag": etag,
        "refresh_billing_address": refresh,
    }
    return _render_draft_form(current, form, errors=errors, conflict=conflict)


# ── Note d'honoraires (Word) — Phase H.2 ────────────────────────────────


def _note_error(message: str) -> str:
    """HTMX error fragment (status 200 so htmx 2.0.4 swaps it, like Phase H)."""
    return (
        '<div class="p-3 text-sm text-red-600 bg-red-50 border border-red-200 '
        f'rounded-xl">{escape(message)}</div>'
    )


def _note_location(doc: dict, folder: Optional[dict]) -> dict:
    """Where the note landed, as the success fragment says it.

    ``folder_path`` — « Mandat › Factures »: the folder the SERVICE reports
    (``NoteGeneree.folder``), preceded by its parent's name when one keyed
    read gives it (``get_folder`` fails open — the folder's name alone,
    then); never a name written here, so the fragment cannot say « Projets »
    of a note filed elsewhere. A REUSED note (none on the web, which always
    regenerates) reports no folder: its own ``folder_id`` is read instead.
    ``at_root`` — the document carries no folder at all.
    """
    dossier_id = str(doc.get("dossier_id") or "")
    if folder is None and doc.get("folder_id") and dossier_id:
        folder = get_folder(dossier_id, doc["folder_id"])
    name = str((folder or {}).get("name") or "").strip()
    path = name
    parent_id = (folder or {}).get("parent_folder_id")
    if name and parent_id and dossier_id:
        parent_name = str((get_folder(dossier_id, parent_id) or {})
                          .get("name") or "").strip()
        if parent_name:
            path = f"{parent_name} › {name}"
    return {"folder_path": path, "at_root": not doc.get("folder_id")}


@invoices_bp.route("/<invoice_id>/note-docx", methods=["POST"])
@login_required
def invoice_note_docx(invoice_id: str) -> Response | str:
    """Fill the note-d'honoraires template from this invoice and save the
    .docx into the dossier's « Mandat › Factures » folder (§9.2; « Projets »
    until the default folder tree).

    A request adapter since lot 3a (step 2): the generation is
    ``services.note_honoraires`` — the ONE assembly the connector will use
    too — and a refusal (logged there) comes back as its fragment. The web
    button has always filed a new note on every click: ``regenerate=True``
    keeps it so. The note is filed under the SESSION's uid
    (``request_uid``), obtained only once the note is filled.
    """
    try:
        note = note_honoraires.generer_note_honoraires(
            invoice_id,
            regenerate=True,
            resolve_uid=storage_identity.request_uid,
        )
    except note_honoraires.NoteRefusee as refusal:
        return _note_error(refusal.message)
    doc = note.document

    if not _is_htmx():
        return redirect(url_for("documents.document_detail", document_id=doc["id"]))
    return render_template(
        "invoices/_note_generated.html",
        generated={
            "display_name": doc.get("display_name", ""),
            "detail_url": url_for("documents.document_detail", document_id=doc["id"]),
            "download_url": url_for("documents.document_download", document_id=doc["id"]),
            **_note_location(doc, note.folder),
        },
    )


# ── Status transitions ──────────────────────────────────────────────────


def _back_to_detail(invoice_id: str, **params: str):
    """Redirect to the invoice sheet, carrying a banner in its query string.

    The detail page's action buttons are full-page forms; a refusal used to
    be dropped on the floor (the non-htmx branch redirected with nothing,
    and the htmx branch answered a 422 htmx never swaps).
    """
    target = url_for(
        "invoices.invoice_detail", invoice_id=invoice_id,
        **{k: v for k, v in params.items() if v},
    )
    resp = redirect(target)
    if _is_htmx():
        resp.headers["HX-Redirect"] = target
    return resp


@invoices_bp.route("/<invoice_id>/status", methods=["POST"])
@login_required
def invoice_update_status(invoice_id: str) -> str:
    """Update invoice status (never to « annulée » — that is /void)."""
    expected = edit_conflict.submitted_etag()
    new_status = request.form.get("status", "").strip()
    success, error = update_status(invoice_id, new_status, expected_etag=expected)
    if not success:
        return _back_to_detail(invoice_id, erreur=error)
    return _back_to_detail(invoice_id)


def _void_outcome_message(report: dict) -> str:
    """What a successful void left aside, in French — '' when nothing was.

    Ids only: they are what the lawyer needs to find the rows, and they
    carry no client data.
    """
    parts = []
    foreign = report.get("foreign_source_ids") or []
    missing = report.get("missing_source_ids") or []
    if foreign:
        parts.append(
            f"{len(foreign)} entrée(s) ou déboursé(s) de cette facture sont "
            "aujourd'hui portés à une AUTRE facture et n'ont pas été "
            f"touchés : {', '.join(foreign)}."
        )
    if missing:
        parts.append(
            f"{len(missing)} source(s) de ses lignes n'existent plus — rien à "
            f"libérer : {', '.join(missing)}."
        )
    if not parts:
        return ""
    return "Facture annulée. " + " ".join(parts)


@invoices_bp.route("/<invoice_id>/void", methods=["POST"])
@login_required
def invoice_void(invoice_id: str) -> str:
    """Void an invoice and release linked entries/expenses."""
    expected = edit_conflict.submitted_etag()
    report, errors = void_invoice_report(invoice_id, expected_etag=expected)
    if errors:
        return _back_to_detail(invoice_id, erreur=errors[0])
    return _back_to_detail(invoice_id, message=_void_outcome_message(report))


@invoices_bp.route("/<invoice_id>/delete", methods=["POST"])
@login_required
def invoice_delete(invoice_id: str) -> str:
    """Delete a cancelled invoice."""
    return_to = request.form.get("return_to", "")
    existing, _ = get_invoice_with_items(invoice_id)
    success, error = delete_invoice(invoice_id)

    if success:
        # Append-only deletion trail (PA-G06) — only annulée invoices can
        # get here; the trail keeps the number that disappeared.
        record_deletion(
            "invoice", invoice_id,
            dossier_id=(existing or {}).get("dossier_id", ""),
            title=(existing or {}).get("invoice_number", ""),
            status=(existing or {}).get("status", ""),
        )

    if not success:
        # Same bounce as /status and /void: this button is a full-page form,
        # the old non-htmx branch redirected with nothing, and the htmx
        # branch answered a 422 htmx never swaps — the refusal (sources
        # still attached, say) vanished without a word.
        return _back_to_detail(invoice_id, erreur=error)

    target = safe_internal_redirect(return_to, url_for("invoices.invoice_list"))
    if _is_htmx():
        resp = redirect(target)
        resp.headers["HX-Redirect"] = target
        return resp
    return redirect(target)


# ── Export ───────────────────────────────────────────────────────────────


_EXPORT_COLUMNS_CSV = [
    ("invoice_number", "N° facture"),
    ("date", "Date"),
    ("dossier_file_number", "Dossier"),
    ("client_name", "Client"),
    ("subtotal", "Sous-total"),
    ("gst_amount", "TPS"),
    ("qst_amount", "TVQ"),
    ("total", "Total"),
    ("status", "Statut"),
]

# The PDF export is the « Journal des honoraires » (utils/journal_pdf.py),
# which owns its own thirteen columns — no generic column list here.


def _export_filters() -> tuple[str, str, "datetime | None", "datetime | None"]:
    """The list filters an export must honour: (status, dossier_id, from, to)."""
    return (
        request.args.get("status", ""),
        request.args.get("dossier_id", "").strip(),
        _parse_date(request.args.get("date_from", "")),
        _parse_date(request.args.get("date_to", "")),
    )


def _filtered_invoices() -> list[dict]:
    status_filter, dossier_id, date_from, date_to = _export_filters()
    return list_invoices(
        status_filter=status_filter or None,
        dossier_id=dossier_id or None,
        date_from=date_from,
        date_to=date_to,
    )


def _get_export_invoices() -> list[dict]:
    """Fetch and pre-process invoices for export, respecting current filters."""
    from utils.export_csv import prepare_export_rows

    return prepare_export_rows(
        _filtered_invoices(), label_maps={"status": STATUS_LABELS}
    )


def _journal_rows(invoices: list[dict]) -> list[dict]:
    """One « Journal des honoraires » row per invoice, amounts in cents.

    Costs one line-item read per invoice: the taxable/non-taxable split of
    disbursements is not stored on the invoice document (see
    ``models.invoice.expense_split``). Deliberately uncapped — an accounting
    journal that silently stopped at N rows would be worse than a slow one.
    """
    rows: list[dict] = []
    for inv in invoices:
        # Fee-only invoice: the split is (0, 0) by construction
        # (expense_split only carves the non-taxable part out of the stored
        # subtotal_expenses), so the subcollection read would be pure waste
        # on the most common row.
        if int(inv.get("subtotal_expenses") or 0) == 0:
            debours_tx, debours_ntx = 0, 0
        else:
            items = list_line_items(inv.get("id", ""))
            debours_tx, debours_ntx = expense_split(inv, items)
        date = inv.get("date")
        rows.append({
            "date": date.strftime("%Y-%m-%d")
            if date and hasattr(date, "strftime") else "",
            "reference": inv.get("dossier_file_number", ""),
            "client": inv.get("client_name", ""),
            "numero": inv.get("invoice_number", ""),
            "honoraires": int(inv.get("subtotal_fees") or 0),
            "debours_tx": debours_tx,
            "debours_ntx": debours_ntx,
            "sous_total": int(inv.get("subtotal") or 0),
            "tps": int(inv.get("gst_amount") or 0),
            "tvq": int(inv.get("qst_amount") or 0),
            "total": int(inv.get("total") or 0),
            "recu": int(inv.get("amount_paid") or 0),
            "solde": balance_of(inv),
            "annulee": inv.get("status") == "annulée",
        })
    return rows


def _journal_subtitle() -> str:
    """The active filters, spelled out — a journal must say what it covers."""
    status_filter, dossier_id, date_from, date_to = _export_filters()
    parts: list[str] = []
    if date_from and date_to:
        parts.append(
            f"Période du {date_from.strftime('%Y-%m-%d')} "
            f"au {date_to.strftime('%Y-%m-%d')}"
        )
    elif date_from:
        parts.append(f"À compter du {date_from.strftime('%Y-%m-%d')}")
    elif date_to:
        parts.append(f"Jusqu'au {date_to.strftime('%Y-%m-%d')}")
    else:
        parts.append("Toutes les factures")
    if status_filter:
        parts.append(f"Statut : {STATUS_LABELS.get(status_filter, status_filter)}")
    if dossier_id:
        parts.append("Un seul dossier")
    return " · ".join(parts)


@invoices_bp.route("/export/csv")
@login_required
def export_csv_route() -> Response:
    """Export invoices as CSV."""
    from utils.export_csv import export_csv

    rows = _get_export_invoices()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return export_csv(
        rows=rows,
        columns=_EXPORT_COLUMNS_CSV,
        filename=f"factures_{date_str}.csv",
        cents_fields=["subtotal", "gst_amount", "qst_amount", "total"],
    )


@invoices_bp.route("/export/pdf")
@login_required
def export_pdf_route() -> Response:
    """Export the « Journal des honoraires » (Barreau model, legal landscape)."""
    from utils.journal_pdf import build_journal_pdf

    invoices = _filtered_invoices()
    # Chronological: a journal reads oldest first, where the screen list
    # reads newest first.
    invoices.sort(
        key=lambda i: i.get("date") or datetime.min.replace(tzinfo=timezone.utc)
    )
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return build_journal_pdf(
        _journal_rows(invoices),
        subtitle=_journal_subtitle(),
        filename=f"journal-honoraires_{date_str}.pdf",
    )
