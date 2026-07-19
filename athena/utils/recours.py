"""Recourse & prescription domain logic for dossiers (Québec civil law).

Pure functions and reference tables — no Firestore, no Flask — so the
prescription deadline ("date pour agir") and the value class are computed
identically wherever they are needed, and stay fully unit-testable.

Two reference tables live here and are meant to be edited in ONE place:

* ``PRESCRIPTION_PERIODS`` — the delay options offered in the dossier form,
  each mapping to an ``(amount, unit)`` period (or ``None`` for an
  imprescriptible / to-be-determined recourse).
* ``VALUE_CLASSES`` / ``TOP_CLASS`` — map the amount in dispute ("valeur")
  to a class (Roman numeral). Each band's upper bound is inclusive at the cent.

Note: ``compute_date_pour_agir`` extends a deadline that lands on a
non-juridical day (weekend or Québec statutory holiday) forward to the next
juridical day. It is still an indicative computation — verify every deadline.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from utils import taxonomie
from utils.deadlines import add_jours_ouvrables, next_juridical_day

# A period is (amount, unit); the unit drives which calendar arithmetic runs.
Period = tuple[int, str]

DAYS, MONTHS, YEARS = "jours", "mois", "ans"

# ── Delay periods ───────────────────────────────────────────────────────
# key -> (French label shown in the dropdown, Period | None).
# ``None`` = no automatic deadline (imprescriptible, or "autre" to set by hand).
#
# NOT ALL OF THESE ARE PRESCRIPTION. The field is named ``prescription_type``
# for continuity, but the list also carries *déchéance* and *avis* delays the
# taxonomy needs (utils/taxonomie.py `delai_types` records which is which — a
# déchéance neither suspends nor interrupts, so the distinction is not
# cosmetic). Sorted by ascending duration: this is the dropdown order.
#
# LABELS ARE DELIBERATELY GENERIC ("3 ans", not "3 ans, art. 2925 C.c.Q.").
# One period serves many articles — the 1-year period alone covers art. 1635
# (paulienne), 929 (possesseur troublé), 2929 (diffamation) and 115 LNT — so
# baking a single article into the label mislabels every other use. The article
# now travels with the taxonomy action (``utils.taxonomie`` `references`),
# which is where it is actually specific.
PRESCRIPTION_PERIODS: dict[str, tuple[str, "Period | None"]] = {
    "5_jours": ("5 jours", (5, DAYS)),
    "10_jours": ("10 jours", (10, DAYS)),
    "15_jours": ("15 jours", (15, DAYS)),
    "30_jours": ("30 jours", (30, DAYS)),
    "45_jours": ("45 jours", (45, DAYS)),
    "60_jours": ("60 jours", (60, DAYS)),
    "90_jours": ("90 jours", (90, DAYS)),
    "3_mois": ("3 mois", (3, MONTHS)),
    "6_mois": ("6 mois", (6, MONTHS)),
    "9_mois": ("9 mois", (9, MONTHS)),
    "1_an": ("1 an", (1, YEARS)),
    "2_ans": ("2 ans", (2, YEARS)),
    "3_ans": ("3 ans", (3, YEARS)),
    "5_ans": ("5 ans", (5, YEARS)),
    "7_ans": ("7 ans", (7, YEARS)),
    "10_ans": ("10 ans", (10, YEARS)),
    "30_ans": ("30 ans", (30, YEARS)),
    "imprescriptible": ("Imprescriptible", None),
    "autre": ("Autre / à déterminer", None),
}

# Valid keys for model-level validation; "" means "non définie".
VALID_PRESCRIPTION_TYPES: tuple[str, ...] = ("",) + tuple(PRESCRIPTION_PERIODS)

# key -> label, for the dropdown and the detail card (includes the empty state).
PRESCRIPTION_LABELS: dict[str, str] = {
    "": "Non définie",
    **{key: label for key, (label, _period) in PRESCRIPTION_PERIODS.items()},
}


def prescription_period(prescription_type: str) -> "Period | None":
    """Return the ``(amount, unit)`` period for a delay type, else ``None``.

    ``None`` covers four distinct cases the caller cannot tell apart — unknown
    key, "" (non définie), "autre", and "imprescriptible" — all of which mean
    "no automatic deadline". ``models.dossier._apply_prescription_deadline``
    is what disambiguates imprescriptible (clears the date) from unset/autre
    (leaves any hand-entered date alone).
    """
    entry = PRESCRIPTION_PERIODS.get(prescription_type or "")
    return entry[1] if entry else None


# ── Value classes (montant en litige → classe) ──────────────────────────
# Amount in dispute → class (Roman numeral). Each band's upper bound is
# INCLUSIVE at the cent (15 000,00 $ → Classe I; 15 000,01 $ → Classe II), so
# thresholds are held in integer cents to avoid float ambiguity at the edge.
VALUE_CLASSES: tuple[tuple[int, str], ...] = (
    (1_500_000, "I"),      # 0,01 $ – 15 000,00 $
    (8_500_000, "II"),     # 15 000,01 $ – 85 000,00 $
    (30_000_000, "III"),   # 85 000,01 $ – 300 000,00 $
)
TOP_CLASS = "IV"           # 300 000,01 $ et plus


def compute_class(valeur_cents: "int | None") -> "str | None":
    """Return the class (Roman numeral) for an amount in dispute, or ``None``.

    ``valeur`` is stored in integer cents (the app-wide money convention);
    each band's upper bound is inclusive at the cent.
    """
    if valeur_cents is None:
        return None
    for upper_cents, label in VALUE_CLASSES:
        if valeur_cents <= upper_cents:
            return label
    return TOP_CLASS


# ── Date pour agir (limitation deadline) ────────────────────────────────
def _add_years(moment: datetime, years: int) -> datetime:
    """Add whole calendar years, clamping 29 Feb → 28 Feb in common years."""
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, day=28)


def _add_months(moment: datetime, months: int) -> datetime:
    """Add whole calendar months, clamping the day to the target month's last.

    The clamp is the month analogue of ``_add_years``' 29 Feb rule: 31 January
    + 1 mois is 28/29 February, not 3 March. ``calendar.monthrange`` supplies
    the target month's length, so leap years need no special case.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _add_period(moment: datetime, amount: int, unit: str) -> datetime:
    """Add a period, dispatching on its unit."""
    if unit == DAYS:
        return moment + timedelta(days=amount)
    if unit == MONTHS:
        return _add_months(moment, amount)
    if unit == YEARS:
        return _add_years(moment, amount)
    raise ValueError(f"Unité de délai inconnue : {unit!r}")


def compute_date_pour_agir(
    droit_action_date: "datetime | None", prescription_type: str
) -> "datetime | None":
    """Compute the deadline to act = ``droit_action_date`` + the delay period.

    Returns ``None`` when the start date is missing or the delay type carries
    no fixed duration (imprescriptible / autre / non définie). When the raw
    deadline lands on a non-juridical day (weekend or Québec statutory
    holiday), it is extended forward to the next juridical day. The result is
    indicative — every limitation deadline must still be verified.
    """
    if not droit_action_date:
        return None
    period = prescription_period(prescription_type)
    if period is None:
        return None
    raw = _add_period(droit_action_date, *period)
    adjusted = next_juridical_day(raw.date())
    return datetime(
        adjusted.year, adjusted.month, adjusted.day, tzinfo=timezone.utc
    )


# ── Échéancier par type de délai (orchestration layer) ──────────────────
# ``compute_echeances`` dispatches on the taxonomy action's ``delai_types``
# and NEVER introduces new date arithmetic: every dated échéance goes through
# ``compute_date_pour_agir`` / ``_add_period`` + ``next_juridical_day`` (the
# art. 52 Loi d'interprétation forward report), the sole exception being the
# additive business-day unit routed through ``deadlines.add_jours_ouvrables``.

# Unit used ONLY by AVIS_PERIODS — never a PRESCRIPTION_PERIODS key.
JOURS_OUVRABLES = "jours_ouvrables"

# Notice periods (Annexe B). The calendar keys REUSE the PRESCRIPTION_PERIODS
# entries; only 3_jours_ouvrables is new, and it never enters the
# prescription dropdown (VALID_PRESCRIPTION_TYPES / PRESCRIPTION_LABELS are
# untouched — pinned by test).
AVIS_PERIODS: dict[str, tuple[str, Period]] = {
    "3_jours_ouvrables": ("3 jours ouvrables", (3, JOURS_OUVRABLES)),
    "15_jours": PRESCRIPTION_PERIODS["15_jours"],
    "30_jours": PRESCRIPTION_PERIODS["30_jours"],
    "60_jours": PRESCRIPTION_PERIODS["60_jours"],
    "9_mois": PRESCRIPTION_PERIODS["9_mois"],
}

# Prescription acquisitive (PA): the adverse-possession maturity per action.
# The dossier's own prescription_type stays "" for these (pinned by test) —
# the period is the taxonomy's, not the lawyer's extinctive dropdown.
PA_PERIODS: dict[str, Period] = {
    "IMM-06": (10, YEARS),   # art. 2918 C.c.Q.
}

# Suggested period for the « délai raisonnable » (R) rows, offered only on
# explicit request and clearly marked indicative.
_R_SUGGESTION_KEY = "30_jours"

_TOKEN_MESSAGES = {
    "N": "Aucun délai — le recours s'exerce en tout temps.",
    "I": "Imprescriptible — aucune échéance automatique.",
    "S": "Le délai suit le recours substantiel — classer aussi le droit exercé.",
    "V": "Saisie manuelle obligatoire — délai variable ou à qualifier.",
    "F": "Fenêtre rétrospective — aucun rappel prospectif.",
}


class Echeance(NamedTuple):
    """One deadline (or checklist item) produced by ``compute_echeances``."""

    role: str                     # "principale" | "avis" | "defensive"
    date: Optional[datetime]      # via the existing arithmetic, or None
    niveau: str                   # "rouge" | "orange" | "normal" | "info" | "aucun"
    libelle: str
    note: str


def _date_from_period(start: datetime, period: Period) -> datetime:
    """Date a period from *start* — composing the EXISTING mechanics only.

    Calendar units run through ``_add_period`` + ``next_juridical_day``
    (identical to ``compute_date_pour_agir``'s tail); the business-day unit
    dispatches to ``deadlines.add_jours_ouvrables`` (whose result is juridical
    by construction). Returns a UTC-midnight datetime.
    """
    amount, unit = period
    if unit == JOURS_OUVRABLES:
        adjusted = add_jours_ouvrables(start.date(), amount)
    else:
        raw = _add_period(start, amount, unit)
        adjusted = next_juridical_day(raw.date())
    return datetime(adjusted.year, adjusted.month, adjusted.day, tzinfo=timezone.utc)


def _a_valider_note(action: "taxonomie.Action | None") -> str:
    return " Qualification à valider." if action and action.a_valider else ""


def _principale_note(action: "taxonomie.Action | None",
                     tokens: tuple[str, ...]) -> str:
    """The principale's note: rigueur (D) / relief (DR) / secondary facets."""
    parts: list[str] = []
    if "D" in tokens:
        parts.append("Délai de rigueur — ni interruption ni suspension en principe.")
    elif "DR" in tokens:
        relief = taxonomie.DR_RELIEF_NOTES.get(
            action.code if action else "",
            "mécanisme de relief prévu par la loi habilitante",
        )
        parts.append(f"Déchéance relevable — {relief}.")
    for secondary in tokens:
        if secondary in ("I", "N") and len(tokens) > 1:
            parts.append(
                f"Volet « {taxonomie.DELAI_TYPE_LABELS[secondary]} » : "
                "voir le libellé de l'action."
            )
    note = " ".join(parts) + _a_valider_note(action)
    return note.strip()


def compute_echeances(
    action_code: str,
    date_depart: "datetime | None",
    prescription_type: str = "",
    *,
    date_depart_avis: "datetime | None" = None,
    avis_confirmes: tuple[int, ...] = (),
    inclure_suggestion_raisonnable: bool = False,
) -> tuple[Echeance, ...]:
    """Type-aware deadline orchestration (spec § 6) — suggestions only.

    The principale is ``compute_date_pour_agir`` VERBATIM whenever the
    lawyer's confirmed period computes (PE/D/DR and the unclassified default
    — strict backwards compatibility, pinned by test); the tokens only set
    the ``niveau`` (rouge D / orange DR) and the note. PA yields a single
    *defensive* échéance (interrupt the adverse possession before maturity).
    R yields no firm date unless the 30-day indicative suggestion is
    requested. N/I/S/V/F yield a dateless échéance carrying the token's
    message. Avis échéances are driven by ``action.avis`` — never by the A
    token alone — and a ``conditionnel`` avis is only dated once its index
    appears in ``avis_confirmes``; without ``date_depart_avis`` (each avis
    has its OWN starting point — délivrance du bien, cause d'action…) or
    without a computable ``delai_key``, it degrades to a dateless checklist
    item. The avis échéance never replaces the principale.
    """
    action = taxonomie.get_action(action_code)
    tokens = action.delai_types if action else ()
    out: list[Echeance] = []

    niveau_qualite = ("rouge" if "D" in tokens
                      else "orange" if "DR" in tokens else None)

    # PA is exclusive and defensive: the date field is the START of the
    # adverse possession; the deadline is its maturity. Never extinctive.
    if "PA" in tokens:
        d = (
            _date_from_period(date_depart, PA_PERIODS[action.code])
            if date_depart and action and action.code in PA_PERIODS
            else None
        )
        return (
            Echeance(
                "defensive", d, "normal",
                "Maturité de la possession adverse — interrompre avant cette date",
                "Point de départ = début de la possession adverse."
                + _a_valider_note(action),
            ),
        )

    if date_depart and prescription_period(prescription_type):
        # The lawyer's confirmed period is authoritative — identical call,
        # identical arithmetic, art. 52 report included.
        d = compute_date_pour_agir(date_depart, prescription_type)
        out.append(Echeance(
            "principale", d, niveau_qualite or "normal", "Date pour agir",
            _principale_note(action, tokens),
        ))
    else:
        primary = next((t for t in tokens if t != "A"), None)
        if primary == "R":
            if inclure_suggestion_raisonnable and date_depart:
                d = compute_date_pour_agir(date_depart, _R_SUGGESTION_KEY)
                out.append(Echeance(
                    "principale", d, niveau_qualite or "info",
                    "Suggestion — délai raisonnable",
                    "Indicatif — délai raisonnable jurisprudentiel "
                    "(30 jours suggérés)."
                    + (" Délai de rigueur si qualifié en déchéance."
                       if "D" in tokens else "")
                    + _a_valider_note(action),
                ))
            else:
                out.append(Echeance(
                    "principale", None, niveau_qualite or "info",
                    "Délai raisonnable",
                    "Pas de date ferme — suggestion indicative de 30 jours "
                    "sur demande." + _a_valider_note(action),
                ))
        elif primary in _TOKEN_MESSAGES:
            note = _TOKEN_MESSAGES[primary]
            secondary = next(
                (t for t in tokens if t not in ("A", primary)), None
            )
            if secondary:
                note += (f" Régime secondaire : "
                         f"{taxonomie.DELAI_TYPE_LABELS[secondary]}.")
            out.append(Echeance(
                "principale", None, "aucun",
                taxonomie.DELAI_TYPE_LABELS[primary],
                note + _a_valider_note(action),
            ))
        else:
            # PE/D/DR without a computable period, or an unclassified /
            # unknown / -99 code: the current behavior survives verbatim
            # (compute_date_pour_agir returns None without a period).
            out.append(Echeance(
                "principale",
                compute_date_pour_agir(date_depart, prescription_type),
                niveau_qualite or "normal", "Date pour agir",
                _principale_note(action, tokens),
            ))

    # Avis: driven by action.avis, NOT the A token (COR-11 carries A with no
    # avis; RCV-03 carries avis with no A — both by binding annex content).
    for i, avis in enumerate(action.avis if action else ()):
        if avis.conditionnel and i not in avis_confirmes:
            continue
        if avis.delai_key in AVIS_PERIODS and date_depart_avis:
            out.append(Echeance(
                "avis",
                _date_from_period(date_depart_avis, AVIS_PERIODS[avis.delai_key][1]),
                "normal", avis.libelle, avis.sanction,
            ))
        else:
            checklist_note = f"{avis.point_depart} — {avis.reference}"
            if avis.sanction:
                checklist_note += f" · {avis.sanction}"
            out.append(Echeance("avis", None, "info", avis.libelle, checklist_note))
    return tuple(out)
