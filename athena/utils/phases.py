"""Taxonomie des phases du litige (axe 1) — Phase O.

Pure reference data and pure helpers — no Firestore, no Flask — mirroring
``utils/taxonomie.py`` so the vocabulary stays unit-testable and importable
from anywhere (``mcp/tools.py`` derives its enums from this module directly,
the ``_COVERAGE_CODES`` precedent: a pure module can never drift from the
schemas that expose it).

Source: « SPEC_PHASE_O_PHASAGE.md » (10 août 2026), Annexe A — the reference
data below is transcribed verbatim from that approved spec. Like taxonomie.py,
**the content of this table changes only on an approved spec** — never edit a
row by hand. Decisions that shape this module:

- D-1  This axis is ORTHOGONAL to the action taxonomy (``taxonomie.py``) and
       to the existing ``category`` fields of tasks/expenses (D-11). Never
       merge, derive, or overload one with the other.
- D-2  A phase is an ORGANIZATION/BILLING construct, never the source of a
       delay — no ``ref_delai``/``ref_fondement`` here, by design.
- D-3  Codes are strict ASCII (the DAV round-trip through Android offers no
       NFC/NFD guarantee — ASCII is what protects CATEGORIES); labels are
       accented French.
- D-9  Every phase receives a synthesized ``-00`` « Général » and ``-99``
       « Autre (préciser) » — except ``HOR``, which carries only ``HOR-00``
       (its label comes from Annexe A.4, not the synthesized « Général »).
- D-13 « CTS » is a visual anagram of the action domaine « CST » — never in
       the same field, but human-facing output must show the LABEL, the code
       staying technical (CSV columns, logs, CATEGORIES).
"""

import functools
from typing import NamedTuple, Optional

# categorie ∈ {"tronc", "module", "transversal", "residuel"}. Only the tronc
# is ordered (``ordre`` 1-9); calendar-duration analytics may only ever be
# computed over the ordered tronc (spec §10 — mixing modules in falsifies
# every interval).


class SousCode(NamedTuple):
    code: str  # ASCII, e.g. "CTS-02"
    libelle: str  # accented French


class Phase(NamedTuple):
    code: str  # ASCII, e.g. "CTS"
    libelle: str
    categorie: str  # tronc | module | transversal | residuel
    ordre: Optional[int]  # rank within the ordered tronc; None elsewhere
    facturable_defaut: bool  # False for ADM and HOR (D-8/D-14)
    portee: str  # one-line scope description (organizational, D-2)
    sous_codes: tuple[SousCode, ...]


def _phase(
    code: str,
    libelle: str,
    categorie: str,
    ordre: Optional[int],
    facturable_defaut: bool,
    portee: str,
    sous: tuple[tuple[str, str], ...],
) -> Phase:
    """Build a Phase, synthesizing the D-9 ``-00``/``-99`` sub-codes.

    Explicit sub-codes come from Annexe A; the synthesized pair brackets them
    (``-00`` first — it is the default imputation — ``-99`` last, mirroring
    the taxonomy's « Autre (préciser) » convention). ``HOR`` is the one phase
    whose sub-code is explicit (A.4) and receives no synthesis.
    """
    if code == "HOR":
        codes = tuple(SousCode(c, l) for c, l in sous)
    else:
        codes = (
            (SousCode(f"{code}-00", "Général"),)
            + tuple(SousCode(c, l) for c, l in sous)
            + (SousCode(f"{code}-99", "Autre (préciser)"),)
        )
    return Phase(code, libelle, categorie, ordre, facturable_defaut, portee, codes)


# Annexe A.1 order preserved (ADM first — the most common non-litigation
# imputation — then the ordered tronc, the modules, the residual).
PHASES: dict[str, Phase] = {
    p.code: p
    for p in (
        _phase(
            "ADM", "Administration du dossier", "transversal", None, False,
            "Gestion administrative du dossier, de l'ouverture à la fermeture.",
            (
                ("ADM-01", "Ouverture, vérification d'identité et de conflits"),
                ("ADM-02", "Mandat et convention d'honoraires"),
                ("ADM-03", "Rapports et gestion de la relation client"),
                ("ADM-04", "Facturation et suivi des comptes"),
                ("ADM-05", "Fermeture et conservation du dossier"),
            ),
        ),
        _phase(
            "PRE", "Préjudiciaire", "tronc", 1, True,
            "Travaux antérieurs à l'introduction : consultation, étude, avis, mises en demeure.",
            (
                ("PRE-01", "Consultation initiale"),
                ("PRE-02", "Étude du dossier"),
                ("PRE-03", "Recherche et avis juridique"),
                ("PRE-04", "Mise en demeure et avis préalables"),
            ),
        ),
        _phase(
            "PRD", "Prévention et règlement", "tronc", 2, True,
            "Modes de prévention et de règlement des différends, avant ou pendant l'instance.",
            (
                ("PRD-01", "Négociation et pourparlers"),
                ("PRD-02", "Médiation"),
                ("PRD-03", "Conférence de règlement à l'amiable"),
                ("PRD-04", "Transaction et quittance"),
            ),
        ),
        _phase(
            "INT", "Introduction de l'instance", "tronc", 3, True,
            "Rédaction, signification et dépôt de la demande ; protocole de l'instance.",
            (
                ("INT-01", "Demande introductive d'instance"),
                ("INT-02", "Signification et dépôt"),
                ("INT-03", "Protocole de l'instance"),
            ),
        ),
        _phase(
            "CTS", "Contestation", "tronc", 4, True,
            "Actes de contestation : défense, demande reconventionnelle et réponses.",
            (
                ("CTS-01", "Défense (écrite ou orale)"),
                ("CTS-02", "Demande reconventionnelle"),
                ("CTS-03", "Réponse et défense reconventionnelle"),
            ),
        ),
        _phase(
            "INR", "Interrogatoires et engagements", "tronc", 5, True,
            "Interrogatoires préalables et gestion des engagements.",
            (
                ("INR-01", "Interrogatoire de la partie adverse"),
                ("INR-02", "Interrogatoire du client ou d'un témoin"),
                ("INR-03", "Engagements et suivis"),
            ),
        ),
        _phase(
            "MEE", "Mise en état et gestion", "tronc", 6, True,
            "Communication de la preuve, correspondance et gestion de l'instance.",
            (
                ("MEE-01", "Communication de la preuve et des pièces"),
                ("MEE-02", "Correspondance avec la partie adverse"),
                ("MEE-03", "Gestion de l'instance et prolongation des délais"),
            ),
        ),
        _phase(
            "INS", "Inscription", "tronc", 7, True,
            "Mise en état du dossier et inscription pour instruction et jugement.",
            (
                ("INS-01", "Déclaration de mise en état et inscription"),
                ("INS-02", "Appel du rôle provisoire et fixation"),
            ),
        ),
        _phase(
            "AUD", "Instruction", "tronc", 8, True,
            "Préparation de l'instruction et jours d'audience.",
            (
                # D-19: the ONE sanctioned nature-of-work split — preparation
                # bills hourly, hearing days often flat.
                ("AUD-01", "Préparation de l'instruction"),
                ("AUD-02", "Audience"),
            ),
        ),
        _phase(
            "JUG", "Jugement et suites", "tronc", 9, True,
            "Analyse du jugement, rapport au client, frais de justice et suites.",
            (
                ("JUG-01", "Analyse du jugement et rapport au client"),
                ("JUG-02", "Frais de justice et mémoire de frais"),
                ("JUG-03", "Rectification et rétractation"),
            ),
        ),
        _phase(
            "PRL", "Moyens préliminaires", "module", None, True,
            "Moyens préliminaires soulevés avant la défense.",
            (
                ("PRL-01", "Moyen déclinatoire"),
                ("PRL-02", "Moyen d'irrecevabilité"),
                ("PRL-03", "Autre moyen préliminaire"),
            ),
        ),
        _phase(
            "PRV", "Mesures provisionnelles", "module", None, True,
            "Mesures provisionnelles et ordonnances de sauvegarde.",
            (
                ("PRV-01", "Injonction interlocutoire"),
                ("PRV-02", "Saisie avant jugement"),
                ("PRV-03", "Ordonnance de sauvegarde"),
            ),
        ),
        _phase(
            "INC", "Demandes en cours d'instance", "module", None, True,
            "Demandes incidentes en cours d'instance.",
            (
                ("INC-01", "Demande en cours d'instance (incident)"),
                ("INC-02", "Mise en cause, intervention ou appel en garantie"),
                ("INC-03", "Modification d'un acte de procédure"),
            ),
        ),
        _phase(
            "EXP", "Expertise", "module", None, True,
            "Expertise : mandat, rapport, contre-expertise, expert à l'instruction.",
            (
                ("EXP-01", "Sélection et mandat de l'expert"),
                ("EXP-02", "Rapport d'expertise et suivi"),
                ("EXP-03", "Contre-expertise"),
                ("EXP-04", "Expert à l'instruction"),
            ),
        ),
        _phase(
            "EXE", "Exécution", "module", None, True,
            "Exécution du jugement et mesures postérieures.",
            (
                ("EXE-01", "Formalités postérieures au jugement"),
                ("EXE-02", "Mesures d'exécution"),
            ),
        ),
        _phase(
            "APP", "Appel", "module", None, True,
            "Appel : permission, mémoire et audition.",
            (
                ("APP-01", "Permission ou déclaration d'appel"),
                ("APP-02", "Mémoire d'appel"),
                ("APP-03", "Audition en appel"),
            ),
        ),
        _phase(
            "CJU", "Contrôle judiciaire", "module", None, True,
            "Pourvoi en contrôle judiciaire d'une décision administrative.",
            (
                ("CJU-01", "Pourvoi en contrôle judiciaire"),
                ("CJU-02", "Mémoire en contrôle judiciaire"),
                ("CJU-03", "Audition en contrôle judiciaire"),
            ),
        ),
        _phase(
            "HOR", "Hors phase", "residuel", None, False,
            "Résiduel — travail inclassable dans une phase.",
            (("HOR-00", "Hors phase (résiduel — aucune ventilation)"),),
        ),
    )
}


# ── Derived constants (Annexe B — computed, nothing hand-doubled) ──────────

SOUS_CODES: dict[str, SousCode] = {
    sc.code: sc for phase in PHASES.values() for sc in phase.sous_codes
}

# "" is a valid value for both — a document need not be phased (the D-6
# requirement lives at the WEB FORM, never in the model: a hard model
# requirement would 422 every DavX5 task PUT, break the protocol step
# auto-created tasks, and contradict the optional MCP parameters).
VALID_PHASES: tuple[str, ...] = ("",) + tuple(PHASES)
VALID_SOUS_PHASES: tuple[str, ...] = ("",) + tuple(SOUS_CODES)

PHASE_LABELS: dict[str, str] = {
    "": "Non renseignée",
    **{code: p.libelle for code, p in PHASES.items()},
}
SOUS_PHASE_LABELS: dict[str, str] = {
    "": "Non renseignée",
    **{code: sc.libelle for code, sc in SOUS_CODES.items()},
}

PHASES_NON_FACTURABLES: frozenset[str] = frozenset(
    code for code, p in PHASES.items() if not p.facturable_defaut
)

TRONC_ORDONNE: tuple[str, ...] = tuple(
    p.code
    for p in sorted(
        (p for p in PHASES.values() if p.categorie == "tronc"),
        key=lambda p: p.ordre or 0,
    )
)


# ── Pure helpers ───────────────────────────────────────────────────────────


def get_phase(code: str) -> Optional[Phase]:
    return PHASES.get(code or "")


def phase_of(sous_code: str) -> str:
    """The parent phase code of a sub-code, or "" when unknown.

    Derived from the code prefix rather than a reverse index — the prefix IS
    the relationship (the ``domaine_of`` pattern), and the models' ``_validate``
    rely on that to reject a ``phase``/``sous_phase`` pair that disagrees.
    """
    sc = SOUS_CODES.get(sous_code or "")
    return sc.code.split("-", 1)[0] if sc else ""


def default_sous_phase(phase_code: str) -> str:
    """The ``-00`` imputation of a phase (D-4 default), "" for unknown."""
    return f"{phase_code}-00" if phase_code in PHASES else ""


def sous_codes_for(phase_code: str) -> tuple[SousCode, ...]:
    phase = PHASES.get(phase_code or "")
    return phase.sous_codes if phase else ()


def sous_phase_label(code: str) -> str:
    """« Libellé [CODE] » for a sub-code, "" for unknown (action_label pattern)."""
    sc = SOUS_CODES.get(code or "")
    return f"{sc.libelle} [{sc.code}]" if sc else ""


def validate_pair(data: dict) -> list[str]:
    """Presence-gated validation of a ``phase``/``sous_phase`` pair (French).

    Mirrors the domaine/action block of ``models/dossier._validate``: gate on
    KEY PRESENCE (updates merge ``{**existing, **data}`` and a legacy document
    read straight from Firestore has neither key — an unconditional check
    would lock it out of editing), "" is valid for both, and the cross-check
    derives from the code prefix. Shared by the three phased models and the
    protocol steps so the four surfaces cannot drift.
    """
    errors: list[str] = []
    phase = data.get("phase", "")
    if "phase" in data and phase not in VALID_PHASES:
        errors.append("Phase invalide.")
    sous = data.get("sous_phase", "")
    if "sous_phase" in data and sous not in VALID_SOUS_PHASES:
        errors.append("Sous-phase invalide.")
    elif sous and phase and phase_of(sous) != phase:
        # The cascading picker cannot produce this pair, but a hand-crafted
        # POST or an MCP call can — left unchecked the two would silently
        # disagree in every report (the spec's « zone à incohérence »).
        errors.append("La sous-phase choisie n'appartient pas à la phase choisie.")
    return errors


def apply_sous_phase_default(doc: dict) -> None:
    """D-4: a phased document with no sub-code imputes to the phase's ``-00``.

    Mutates *doc* in place (called on the merged dict just before validation
    in the models' ``create_*``/``update_*``). A blank phase stays blank —
    never invent an imputation.
    """
    if doc.get("phase") and not doc.get("sous_phase"):
        doc["sous_phase"] = default_sous_phase(doc["phase"])


@functools.lru_cache(maxsize=1)
def form_payload() -> dict:
    """The whole table as JSON-ready data for the cascading phase picker.

    Embedded as a non-executable ``<script type="application/json">`` block
    (the taxonomie ``form_payload`` pattern — never a fetch(): a raw fetch
    carries no X-Firebase-AppCheck header and the cascade must work on first
    paint). Cached and READ-ONLY — every caller shares it; only ever fed to
    Jinja's ``|tojson``. ``facturable_defaut`` rides along so the time form
    can uncheck « Facturable » when ADM/HOR is picked (D-8), without a second
    data block.
    """
    return {
        code: {
            "libelle": p.libelle,
            "categorie": p.categorie,
            "facturable_defaut": p.facturable_defaut,
            "portee": p.portee,
            "sous_codes": [
                {"code": sc.code, "label": f"{sc.libelle} [{sc.code}]"}
                for sc in p.sous_codes
            ],
        }
        for code, p in PHASES.items()
    }
