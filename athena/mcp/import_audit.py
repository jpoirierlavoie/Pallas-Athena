"""Import checks — the pure half of ``get_import_audit`` (lot Q).

Answers « ce dossier a-t-il été repris correctement ? » after a historical
import: the work, the disbursements and the invoices that were just
transcribed, checked against each other in one call.

**No model imports**, exactly like ``mcp/coverage.py``: ``models/__init__.py``
builds the Firestore client at import time and ``mcp/tools.py`` imports the
registry below at startup. Every predicate takes plain dicts the handler
already fetched, so the whole suite is unit-testable with no Firestore and no
mocking.

Same two closed French vocabularies as the coverage report:

* ``manquement`` — a real inconsistency in the data as written.
* ``signalement`` — worth a look, not a breach.

**A finding is an observation, never an instruction.** The connector cannot
delete a duplicate entry, cannot void an invoice and cannot move one out of
brouillon; every ``detail`` says what to do IN THE APPLICATION.

**A failed read must never become a manquement.** When the handler could not
read a dossier's sources completely, the checks that compare an invoice's line
items against them are SUPPRESSED and named in ``checks_skipped`` — a
shortened report must not pass for a clean one. Inventing « source
introuvable » out of a truncated window would accuse an import that is
actually fine.
"""

from typing import Optional

MANQUEMENT = "manquement"
SIGNALEMENT = "signalement"

CLOSED_STATUSES = ("fermé", "archivé")

# Codes whose predicate compares an invoice's line items against the dossier's
# fetched sources. Meaningless — and actively misleading — when that fetch was
# truncated.
NEEDS_COMPLETE_SOURCES = ("IMP-03", "IMP-06")


def _rows(ctx: dict) -> list[dict]:
    """Time entries and disbursements together, for the checks that treat
    « billable work » as one population."""
    return list(ctx.get("time_entries") or []) + list(ctx.get("expenses") or [])


def _source_ids(ctx: dict) -> set:
    return {r.get("id") for r in _rows(ctx) if r.get("id")}


def _invoice_ids(ctx: dict) -> set:
    return {
        entry.get("invoice", {}).get("id")
        for entry in (ctx.get("invoices") or [])
        if entry.get("invoice", {}).get("id")
    }


# ── IMP-01 ────────────────────────────────────────────────────────────────


def _travail_non_facture_sur_dossier_ferme(ctx: dict) -> Optional[str]:
    """The signature of a half-finished import.

    It also silently inflates ``get_unbilled_totals``, which has no
    dossier-status filter: work stranded on a closed file keeps counting as
    billable across the whole practice.
    """
    if (ctx.get("dossier") or {}).get("status") not in CLOSED_STATUSES:
        return None
    stranded = [r for r in _rows(ctx) if not r.get("invoiced")]
    if not stranded:
        return None
    return (
        f"{len(stranded)} entrée(s) ou déboursé(s) non facturé(s) sur un "
        "dossier fermé — la signature d'une reprise interrompue avant sa "
        "facture. Ils gonflent aussi le total des heures non facturées du "
        "cabinet, qui ne filtre pas sur le statut du dossier. Facturez-les "
        "ou supprimez-les dans l'application."
    )


# ── IMP-02 ────────────────────────────────────────────────────────────────


def _total_ne_correspond_pas_aux_postes(ctx: dict) -> Optional[str]:
    offenders: list[str] = []
    for entry in ctx.get("invoices") or []:
        invoice = entry.get("invoice") or {}
        items = entry.get("line_items") or []
        if not items:
            # Empty items on a non-zero invoice means the READ failed, not
            # that the invoice is empty. Saying « le total ne correspond
            # pas » there would blame the data for a transport problem.
            continue
        somme = sum(int(i.get("amount") or 0) for i in items)
        if somme != int(invoice.get("subtotal") or 0):
            offenders.append(
                f"{invoice.get('invoice_number', '?')} "
                f"(postes {somme} ¢, sous-total {invoice.get('subtotal', 0)} ¢)"
            )
    if not offenders:
        return None
    return (
        "Le sous-total stocké et la somme des postes divergent sur : "
        + ", ".join(offenders)
        + ". Ne recalculez pas en silence — la facture a été émise sous le "
        "montant stocké ; annulez-la et refaites-la dans l'application si "
        "elle est fausse."
    )


# ── IMP-03 ────────────────────────────────────────────────────────────────


def _facture_cite_une_source_introuvable(ctx: dict) -> Optional[str]:
    known = _source_ids(ctx)
    orphans: list[str] = []
    for entry in ctx.get("invoices") or []:
        invoice = entry.get("invoice") or {}
        for item in entry.get("line_items") or []:
            source_id = item.get("source_id") or ""
            # An adjustment line carries no source by design — it is the one
            # line item in the system that traces to nothing, which is why
            # the tool that creates it demands a description.
            if not source_id:
                continue
            if source_id not in known:
                orphans.append(f"{invoice.get('invoice_number', '?')} → {source_id}")
    if not orphans:
        return None
    return (
        "Des postes citent une source qui n'existe plus : "
        + ", ".join(orphans)
        + ". La facture reste lisible, mais son détail ne se reconstitue plus."
    )


# ── IMP-04 ────────────────────────────────────────────────────────────────


def _entrees_possiblement_en_double(ctx: dict) -> Optional[str]:
    groups: dict[tuple, list[str]] = {}
    for row in _rows(ctx):
        key = (
            str(row.get("date")),
            (row.get("description") or "").strip(),
            int(row.get("amount") or 0),
        )
        if not key[1]:
            continue
        groups.setdefault(key, []).append(row.get("id", ""))
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        return None
    total = sum(len(v) for v in dupes.values())
    return (
        f"{len(dupes)} groupe(s) totalisant {total} lignes partagent la même "
        "date, la même description et le même montant. Une reprise relancée "
        "après l'expiration de la clé d'idempotence (24 h) produit exactement "
        "cela. Un doublon NON FACTURÉ se supprime dans l'application ; une "
        "fois facturé, il faut d'abord y annuler la facture."
    )


# ── IMP-05 ────────────────────────────────────────────────────────────────


def _dossier_ferme_sans_date_de_fermeture(ctx: dict) -> Optional[str]:
    dossier = ctx.get("dossier") or {}
    if dossier.get("status") not in CLOSED_STATUSES:
        return None
    if dossier.get("closed_date"):
        return None
    return (
        "Le dossier est fermé sans date de fermeture. La rétention (fermeture "
        "+ 7 ans) ne peut pas se calculer, et les gabarits citent un champ "
        "vide. Renseignez-la dans l'application : le connecteur ne fixe la "
        "date de fermeture qu'à la CRÉATION du dossier."
    )


# ── IMP-06 ────────────────────────────────────────────────────────────────


def _entree_facturee_sans_facture(ctx: dict) -> Optional[str]:
    known = _invoice_ids(ctx)
    ghosts = [
        r.get("id", "")
        for r in _rows(ctx)
        if r.get("invoiced") and (r.get("invoice_id") or "") not in known
    ]
    if not ghosts:
        return None
    return (
        f"{len(ghosts)} entrée(s) portent « facturée » avec une facture "
        "introuvable dans ce dossier. Elles sont définitivement immodifiables "
        "tant que la référence n'est pas levée : ni le connecteur ni "
        "l'application ne modifient une entrée facturée."
    )


# ── IMP-07 ────────────────────────────────────────────────────────────────


def _facture_importee_encore_au_brouillon(ctx: dict) -> Optional[str]:
    drafts = [
        (e.get("invoice") or {}).get("invoice_number", "?")
        for e in (ctx.get("invoices") or [])
        if (e.get("invoice") or {}).get("status") == "brouillon"
    ]
    if not drafts:
        return None
    return (
        f"{len(drafts)} facture(s) encore au brouillon : "
        + ", ".join(drafts)
        + ". Le connecteur ne change JAMAIS le statut d'une facture ni "
        "n'inscrit un paiement. Tant qu'elles ne sont pas promues dans "
        "l'application (brouillon → envoyée, puis le paiement à sa date "
        "historique), le « Journal des honoraires » les imprime avec 0 $ reçu "
        "et le total en solde, et le sommaire du dossier lit « payé 0 »."
    )


CHECKS: tuple = (
    ("IMP-01", SIGNALEMENT, "Travail non facturé sur dossier fermé",
     _travail_non_facture_sur_dossier_ferme),
    ("IMP-02", MANQUEMENT, "Total incohérent avec les postes",
     _total_ne_correspond_pas_aux_postes),
    ("IMP-03", MANQUEMENT, "Poste citant une source introuvable",
     _facture_cite_une_source_introuvable),
    ("IMP-04", SIGNALEMENT, "Entrées possiblement en double",
     _entrees_possiblement_en_double),
    ("IMP-05", SIGNALEMENT, "Dossier fermé sans date de fermeture",
     _dossier_ferme_sans_date_de_fermeture),
    ("IMP-06", MANQUEMENT, "Entrée facturée sans facture",
     _entree_facturee_sans_facture),
    ("IMP-07", SIGNALEMENT, "Facture importée encore au brouillon",
     _facture_importee_encore_au_brouillon),
)

ALL_CODES: tuple = tuple(c[0] for c in CHECKS)

SEVERITY_BY_CODE: dict = {code: severity for code, severity, _l, _f in CHECKS}
LABEL_BY_CODE: dict = {code: label for code, _s, label, _f in CHECKS}


def run_checks(ctx: dict, *, skip: frozenset = frozenset()) -> list[dict]:
    """Run every applicable check over ONE dossier's import. Pure.

    ``skip`` names the codes the handler suppressed because their context
    could not be read completely — never silently: the caller lists them in
    ``checks_skipped``.
    """
    findings: list[dict] = []
    for code, severity, label, predicate in CHECKS:
        if code in skip:
            continue
        detail = predicate(ctx)
        if detail:
            findings.append({
                "code": code,
                "severity": severity,
                "label": label,
                "detail": detail,
            })
    return findings
