"""Coverage checks — the pure half of ``get_coverage_report`` (lot 5).

Answers « which open files are missing something », in one call instead of
one ``get_dossier`` per dossier.

**No model imports.** This module pulls in nothing from ``models``:
``models/__init__.py`` builds the Firestore client at import time, and
``mcp/tools.py`` imports the registry below at startup. Every predicate here
takes plain dicts the handler already fetched, so the whole check suite is
unit-testable with no Firestore client and no mocking — same discipline as
``mcp/output_schemas.py``. (The package ``__init__`` still reads config, so
a test importing it sets the usual env vars; nothing here touches a
database.)

Two vocabularies, both French and both closed:

* ``manquement`` — something the file is REQUIRED to have. The two
  deontological checks (conflict of interest, identity verification) live
  here; they are regulatory obligations, not preferences of data entry.
* ``signalement`` — something worth a look, not a breach.

**A finding is an observation, never an instruction.** Every ``detail``
string says what to do IN THE APPLICATION. The connector cannot create a
protocol, verify an identity or file a signification, and a report that
implied otherwise would invite a write this connector must never make.
"""

from typing import Any, Callable, Optional

MANQUEMENT = "manquement"
SIGNALEMENT = "signalement"

# Statuses that close a file. A task or protocol still open on one of these
# is the « ghost » case the audit reported.
CLOSED_STATUSES = ("fermé", "archivé")

# `identity_verified` has THREE decided states, not two: « exempté » is a
# legitimate terminal outcome (a client the regulation exempts), and the
# mandate's literal rule (≠ vérifié) would raise a false manquement on it —
# on a deontological check, which is where a false positive costs the most
# credibility. « conflit_détecté » is likewise decided: the check WAS run.
IDENTITY_DECIDED = ("vérifié", "exempté")
CONFLICT_DECIDED = ("vérifié", "conflit_détecté")


def _judicial(dossier: dict) -> bool:
    """A file before a court of general jurisdiction, where the C.p.c.
    obligations below apply. Administrative and federal forums have their
    own rules; « prejudiciaire » means nothing has been filed at all."""
    return (dossier.get("forum_type") or "judiciaire") == "judiciaire"


def _opposing_ids(dossier: dict) -> list[str]:
    return [
        str(p.get("id"))
        for p in (dossier.get("opposing_parties") or [])
        if isinstance(p, dict) and p.get("id")
    ]


def operative_significations(dossier: dict) -> set:
    """Party ids served by a signification that no sibling supersedes.

    ``superseded_by`` handles the corrected-second-PV case: the operative
    service is the one nothing replaces. Restricted to OPPOSING parties on
    purpose — the model's own validation admits a client id too (arts.
    145/147 delays run per party), so counting the raw register would let a
    signification on one's own client mask a defendant never served.
    """
    opposing = set(_opposing_ids(dossier))
    return {
        str(s.get("partie_id"))
        for s in (dossier.get("significations") or [])
        if isinstance(s, dict)
        and not (s.get("superseded_by") or "")
        and str(s.get("partie_id")) in opposing
    }


# ── The checks ──────────────────────────────────────────────────────────
# Each predicate returns a French `detail` string when it fires, else None.
# `needs` names the extra context a check consumes, so the handler can skip
# — and DECLARE it skipped — when that context could not be read.


def _proto_absent(d: dict, ctx: dict) -> Optional[str]:
    if not _judicial(d):
        return None
    if not (d.get("court_file_number") or "").strip():
        return None          # nothing filed yet — NO_COUR_ABSENT covers it
    if d.get("id") in ctx["active_protocol_dossiers"]:
        return None
    return (
        "Instance liée, mais aucun protocole actif. Créez-le dans "
        "l'application (onglet Protocole du dossier)."
    )


def _proto_regime(d: dict, ctx: dict) -> Optional[str]:
    proto = ctx["active_protocols_by_dossier"].get(d.get("id"))
    if not proto or not proto.get("regime_mismatch"):
        return None
    return (
        f"Le protocole « {proto.get('protocol_type', '')} » ne correspond pas "
        f"au tribunal du dossier ({d.get('tribunal') or 'non précisé'}). Ses "
        "échéances sont suspectes : reprenez-le dans l'application."
    )


def _sign_absente(d: dict, ctx: dict) -> Optional[str]:
    if not _judicial(d) or not _opposing_ids(d):
        return None
    if d.get("significations"):
        return None
    return (
        f"{len(_opposing_ids(d))} partie(s) adverse(s) au dossier et aucune "
        "signification consignée. Inscrivez-les dans l'application si elles "
        "ont eu lieu."
    )


def _sign_partielle(d: dict, ctx: dict) -> Optional[str]:
    if not _judicial(d):
        return None
    opposing = _opposing_ids(d)
    served = operative_significations(d)
    if not opposing or not served or len(served) >= len(opposing):
        return None
    return (
        f"{len(served)} partie(s) adverse(s) signifiée(s) sur {len(opposing)}. "
        "Les délais des arts. 145 et 147 C.p.c. courent PAR PARTIE — vérifiez "
        "les manquantes dans l'application."
    )


def _prescription_a_verifier(d: dict, ctx: dict) -> Optional[str]:
    if d.get("prescription_status") != "a_verifier":
        return None
    return (
        "La prescription n'est pas qualifiable : ni délai confirmé ni date "
        "pour agir calculable. Complétez le recours dans l'application."
    )


def _taxonomie_a_valider(d: dict, ctx: dict) -> Optional[str]:
    if not d.get("action_a_valider"):
        return None
    return (
        f"La qualification de l'action ({d.get('action') or '—'}) est à "
        "valider aux sources : le délai suggéré peut ne pas être le bon."
    )


def _no_cour_absent(d: dict, ctx: dict) -> Optional[str]:
    if not _judicial(d):
        return None
    number = (d.get("court_file_number") or "").strip()
    # « Préjudiciaire » is the placeholder a pre-litigation file carries so
    # gabarits can cite something; on a judiciaire file it means the real
    # number was never entered.
    if number and number != "Préjudiciaire":
        return None
    return "Forum judiciaire sans numéro de cour saisi."


def _valeur_absente(d: dict, ctx: dict) -> Optional[str]:
    # `is None`, NEVER a falsy test: 0 is a value (a purely declaratory
    # recourse), and treating it as absent would nag about a decided field.
    if d.get("valeur_cents") is not None:
        return None
    return (
        "Valeur en litige non saisie — la classe (I à IV) et le tarif en "
        "dépendent."
    )


def _conflit_non_verifie(d: dict, ctx: dict) -> Optional[str]:
    unresolved = [
        c for c in ctx["clients_of"](d)
        if c.get("conflict_check") not in CONFLICT_DECIDED
    ]
    if not unresolved:
        return None
    return (
        f"Vérification des conflits non faite pour {len(unresolved)} "
        "client(s). Obligation déontologique — à consigner dans la fiche du "
        "contact."
    )


def _identite_non_verifiee(d: dict, ctx: dict) -> Optional[str]:
    unresolved = [
        c for c in ctx["clients_of"](d)
        if c.get("identity_verified") not in IDENTITY_DECIDED
    ]
    if not unresolved:
        return None
    return (
        f"Identité non vérifiée pour {len(unresolved)} client(s). "
        "« Exempté » compte comme décidé — à consigner dans la fiche."
    )


def _client_introuvable(d: dict, ctx: dict) -> Optional[str]:
    missing = ctx["missing_clients_of"](d)
    if not missing:
        return None
    # Without this check a deleted contact reads as « verified » (it is
    # simply absent from the KYC scan) and the deontological checks above
    # would report a clean file that has no client at all.
    return (
        f"{len(missing)} client(s) référencé(s) au dossier n'existe(nt) plus "
        "dans les contacts. Le dossier cite une partie introuvable."
    )


CHECKS: tuple = (
    ("PROTO_ABSENT", MANQUEMENT, "Protocole absent", _proto_absent, "protocols"),
    ("PROTO_REGIME", MANQUEMENT, "Régime de protocole inadéquat", _proto_regime, "protocols"),
    ("SIGN_ABSENTE", MANQUEMENT, "Aucune signification consignée", _sign_absente, None),
    ("SIGN_PARTIELLE", MANQUEMENT, "Signification partielle", _sign_partielle, None),
    ("CONFLIT_NON_VERIFIE", MANQUEMENT, "Vérification des conflits non faite", _conflit_non_verifie, "kyc"),
    ("IDENTITE_NON_VERIFIEE", MANQUEMENT, "Identité non vérifiée", _identite_non_verifiee, "kyc"),
    ("CLIENT_INTROUVABLE", MANQUEMENT, "Client introuvable", _client_introuvable, "kyc"),
    ("PRESCRIPTION_A_VERIFIER", SIGNALEMENT, "Prescription non qualifiée", _prescription_a_verifier, None),
    ("TAXONOMIE_A_VALIDER", SIGNALEMENT, "Taxonomie à valider", _taxonomie_a_valider, None),
    ("NO_COUR_ABSENT", SIGNALEMENT, "Numéro de cour absent", _no_cour_absent, None),
    ("VALEUR_ABSENTE", SIGNALEMENT, "Valeur en litige absente", _valeur_absente, None),
)

# Cross-scope: these fire on CLOSED files, so under the default « actif »
# filter they could never appear. The ghost task on a closed dossier is one
# of the audit's motivating examples, so they run firm-wide and report into
# their own array rather than being silently unreachable.
CROSS_SCOPE_CODES = ("TACHE_OUVERTE_DOSSIER_FERME", "PROTO_ACTIF_DOSSIER_FERME")

ALL_CODES: tuple = tuple(c[0] for c in CHECKS) + CROSS_SCOPE_CODES

SEVERITY_BY_CODE: dict = {
    **{code: severity for code, severity, _l, _f, _n in CHECKS},
    "TACHE_OUVERTE_DOSSIER_FERME": SIGNALEMENT,
    "PROTO_ACTIF_DOSSIER_FERME": SIGNALEMENT,
}

LABEL_BY_CODE: dict = {
    **{code: label for code, _s, label, _f, _n in CHECKS},
    "TACHE_OUVERTE_DOSSIER_FERME": "Tâche ouverte sur dossier fermé",
    "PROTO_ACTIF_DOSSIER_FERME": "Protocole actif sur dossier fermé",
}


def run_checks(
    dossier: dict,
    ctx: dict,
    *,
    skip: frozenset = frozenset(),
) -> list[dict]:
    """Run every applicable check over ONE dossier. Pure.

    ``skip`` names the codes the handler suppressed because their context
    could not be read — never silently: the caller lists them in
    ``checks_skipped`` so a shortened report cannot pass for a clean one.
    """
    findings: list[dict] = []
    for code, severity, label, predicate, _needs in CHECKS:
        if code in skip:
            continue
        detail = predicate(dossier, ctx)
        if detail:
            findings.append({
                "code": code,
                "severity": severity,
                "label": label,
                "detail": detail,
            })
    return findings
