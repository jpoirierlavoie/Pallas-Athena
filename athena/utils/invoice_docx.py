"""Invoice → note-d'honoraires context builder (Phase H.2).

Pure function — no Firestore, no Flask (the caller loads the invoice and
line items). Maps a stored ``invoices/{id}`` document + its line items to:

* ``.values``     — scalar fields: ``facture.*`` (§6.2) plus the Phase H
                    header namespaces (``destinataire.*``/``dossier.*``/
                    ``cabinet.*``/``date.*``) resolved through the field
                    catalog, so the note reuses the same header fields as
                    letters and procedures;
* ``.rows``       — ``region -> list[row dict]`` for the three tables (§6.3);
* ``.conditions`` — ``si_*`` flags driving conditional-region removal (§5.4).

Every money figure is READ from the invoice and only *formatted* — the
builder performs no tax arithmetic (§7.2). Its only arithmetic is integer
addition of line-item cents for the two derived disbursement subtotals
(§6.4), which is exact.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from utils.format_fr import (
    format_cents_fr,
    format_cents_fr_parens,
    format_date_fr,
    format_hours_fr,
    format_rate_fr,
)
from utils.template_fields import CATALOG, FLAT_ALIASES, resolve_values

# The invoice stores GST ×100 and QST ×1000 (models.invoice) — see
# utils.format_fr.format_rate_fr.
_GST_SCALE = 100
_QST_SCALE = 1000

REGIONS = ("ligne_honoraire", "ligne_debours_tx", "ligne_debours_ntx")


@dataclass
class InvoiceContext:
    values: dict[str, str] = field(default_factory=dict)
    rows: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    conditions: dict[str, bool] = field(default_factory=dict)


def _as_date(value) -> Optional[date]:
    """A stored date-only field (midnight-UTC datetime) → its UTC calendar date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _date_str(value) -> str:
    d = _as_date(value)
    return format_date_fr(d) if d else ""


def _partie_from_billing_address(billing: dict) -> dict:
    """Synthetic partie from the invoice's ``billing_address`` snapshot (§6.6).

    Used when the client partie was deleted — the invoice is the record of
    what was billed, so its snapshot must still render. Typed as an
    organization so ``destinataire.nom_complet`` resolves to the snapshot name
    and the address fields feed ``destinataire.adresse_complete``.
    """
    billing = billing or {}
    return {
        "type": "organization",
        "organization_name": billing.get("name", ""),
        "address_street": billing.get("street", ""),
        "address_unit": billing.get("unit", ""),
        "address_city": billing.get("city", ""),
        "address_province": billing.get("province", ""),
        "address_postal_code": billing.get("postal_code", ""),
        "address_country": "Canada",
        "contact_role": "client",
    }


def _facture_values(invoice: dict, line_items: list[dict]) -> dict[str, str]:
    """The ``facture.*`` scalar fields (§6.2). All figures read, not computed."""
    fees = [li for li in line_items if li.get("type") == "fee"]
    expenses = [li for li in line_items if li.get("type") != "fee"]
    taxable = [e for e in expenses if e.get("taxable", True)]
    non_taxable = [e for e in expenses if not e.get("taxable", True)]

    # Derived subtotals — exact integer-cent addition (§6.4).
    st_tx = sum(int(e.get("amount", 0) or 0) for e in taxable)
    st_ntx = sum(int(e.get("amount", 0) or 0) for e in non_taxable)

    total_hours = sum(float(f.get("hours") or 0) for f in fees)
    rates = {int(f.get("rate") or 0) for f in fees}
    taux_horaire = format_cents_fr(next(iter(rates))) if len(rates) == 1 else ""

    return {
        "facture.numero": invoice.get("invoice_number", ""),
        "facture.date": _date_str(invoice.get("date")),
        "facture.date_echeance": _date_str(invoice.get("due_date")),
        "facture.sous_total_honoraires": format_cents_fr(invoice.get("subtotal_fees", 0)),
        "facture.sous_total_debours_tx": format_cents_fr(st_tx),
        "facture.sous_total_debours_ntx": format_cents_fr(st_ntx),
        "facture.total_honoraires": format_cents_fr(invoice.get("subtotal_fees", 0)),
        "facture.total_debours_tx": format_cents_fr(st_tx),
        "facture.total_debours_ntx": format_cents_fr(st_ntx),
        "facture.total_avant_taxes": format_cents_fr(invoice.get("subtotal", 0)),
        "facture.tps_taux": format_rate_fr(invoice.get("gst_rate", 500), _GST_SCALE),
        "facture.tps_numero": invoice.get("gst_number", ""),
        "facture.tps_montant": format_cents_fr(invoice.get("gst_amount", 0)),
        "facture.tvq_taux": format_rate_fr(invoice.get("qst_rate", 9975), _QST_SCALE),
        "facture.tvq_numero": invoice.get("qst_number", ""),
        "facture.tvq_montant": format_cents_fr(invoice.get("qst_amount", 0)),
        "facture.total_apres_taxes": format_cents_fr(invoice.get("total", 0)),
        "facture.avances_fideicommis": format_cents_fr_parens(
            invoice.get("retainer_applied", 0)
        ),
        "facture.solde": format_cents_fr(invoice.get("amount_due", 0)),
        "facture.nombre_heures": format_hours_fr(total_hours),
        "facture.taux_horaire": taux_horaire,
    }


def _build_rows(line_items: list[dict]) -> dict[str, list[dict[str, str]]]:
    """Split line items into the three region row-lists, in line-item order."""
    honoraire: list[dict] = []
    debours_tx: list[dict] = []
    debours_ntx: list[dict] = []
    for li in line_items:
        date_str = _date_str(li.get("date"))
        description = li.get("description", "") or ""
        if li.get("type") == "fee":
            honoraire.append({
                "h.date": date_str,
                "h.description": description,
                "h.temps": format_hours_fr(li.get("hours") or 0),
            })
        else:
            row = {
                "d.date": date_str,
                "d.description": description,
                "d.cout": format_cents_fr(li.get("amount", 0)),
            }
            (debours_tx if li.get("taxable", True) else debours_ntx).append(row)
    return {
        "ligne_honoraire": honoraire,
        "ligne_debours_tx": debours_tx,
        "ligne_debours_ntx": debours_ntx,
    }


def build_invoice_context(
    invoice: dict,
    line_items: list[dict],
    *,
    firm: dict,
    destinataire: Optional[dict],
    dossier: Optional[dict],
    today: date,
) -> InvoiceContext:
    """Build the note-d'honoraires context from a stored invoice (§6).

    ``destinataire`` is the client partie; when ``None`` (partie deleted) the
    billing_address snapshot is used so generation never fails (§6.6). Header
    fields are resolved through the Phase H catalog by canonical name.
    """
    dest = destinataire or _partie_from_billing_address(invoice.get("billing_address") or {})

    # Header namespaces via the Phase H catalog — both CANONICAL names and the
    # flat ALIASES the procedures/letters gabarits use, so a note template can
    # use the identical placeholders ({{numero_dossier}} as well as
    # {{dossier.numero_cour}}). Resolving the whole set is cheap and pure;
    # unused fields are ignored by the fill engine.
    values = resolve_values(
        list(CATALOG) + list(FLAT_ALIASES),
        dossier=dossier,
        client=None,
        adverse=None,
        destinataire=dest,
        firm=firm or {},
        today=today,
    )
    values.update(_facture_values(invoice, line_items))

    rows = _build_rows(line_items)
    conditions = {
        "si_honoraires": bool(rows["ligne_honoraire"]),
        "si_debours_tx": bool(rows["ligne_debours_tx"]),
        "si_debours_ntx": bool(rows["ligne_debours_ntx"]),
    }
    return InvoiceContext(values=values, rows=rows, conditions=conditions)
