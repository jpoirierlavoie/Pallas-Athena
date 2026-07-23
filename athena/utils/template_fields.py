"""Field catalog and resolution for docx template generation (Phase H).

Pure functions — no Firestore, no Flask. Callers pass already-loaded
dicts (dossier, parties, firm info) plus today's date; everything here is
fully unit-testable.

Placeholder taxonomy:

* **auto** — resolvable from the catalog (``dossier.*``, ``client.*``,
  ``adverse.*``, ``destinataire.*``, ``cabinet.*``, ``date.*``), directly
  or through :data:`FLAT_ALIASES`. Matched **case-insensitively**, so
  ``{{TRIBUNAL}}`` resolves like ``{{tribunal}}``; when the placeholder is
  written in ALL-CAPS the resolved value is upper-cased to match (legal
  headings — ``{{TRIBUNAL}}`` → ``COUR SUPÉRIEURE``).
* **manual** — the short letter-metadata fields in :data:`MANUAL_FIELDS`
  (objet, privilège, mode de transmission, pièces jointes, référence
  externe): prompted in the generation popup, some with a default.
* **passthrough** — everything else: free-form legal content (the former
  ALL-CAPS "blocks" such as ``{{FAITS}}`` / ``{{CONCLUSIONS}}``), the
  ``{{civilité}}`` / ``{{salutations}}`` fields, and any unknown name.
  These are NOT filled by the app and NOT prompted — the ``{{name}}`` is
  left verbatim in the generated .docx for the user to complete in Word.

There is deliberately no "block" concept and no civilité auto-resolution:
civilité belongs in letters but never in court procedures, so the user
places and fills it themselves (a passthrough field).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from utils import taxonomie
from utils.format_fr import format_cents_fr, format_rate_fr
from utils.recours import PRESCRIPTION_LABELS, compute_class
from utils.validators import format_phone_display

# ── Vocabulary ──────────────────────────────────────────────────────────

SLOTS = ("dossier", "client", "adverse", "destinataire")

_ROLE_FEMININ = {
    "demandeur": "demanderesse",
    "défendeur": "défenderesse",
    "intervenant": "intervenante",
    "mis en cause": "mise en cause",
}

# Capitalized display label for the client's litigation role (mirrors
# models.dossier.ROLE_LABELS — kept local so this module stays importable
# without the Firestore client).
_ROLE_LABEL = {
    "demandeur": "Demandeur",
    "défendeur": "Défendeur",
    "intervenant": "Intervenant",
    "mis en cause": "Mis en cause",
    "autre": "Autre",
}

# Mirrors of the models.dossier label maps — kept local for the same reason as
# _ROLE_LABEL (Firestore-free import). KEEP IN SYNC with models/dossier.py
# (MANDATE_TYPE_LABELS / FEE_TYPE_LABELS).
#
# There is NO domaine mirror: utils.taxonomie is Firestore-free, so both this
# module and models/dossier.py import DOMAINE_LABELS from it directly. That is
# the shape to prefer — a mirror drifts silently and asymmetrically (the detail
# card would show the new label while every generated .docx showed
# « [CHAMP MANQUANT : …] », because _dossier_labelled returns None on a key it
# does not know).
_MANDATE_TYPE_LABEL = {
    "judiciaire": "Judiciaire (ad litem)",
    "service_conseils": "Service-conseils",
    "general": "Général",
    "special": "Spécial",
}
_FEE_TYPE_LABEL = {
    "hourly": "Horaire",
    "flat": "Forfaitaire",
    "contingency": "Contingence",
    "mixed": "Mixte",
    # Rate-less arrangements → format_honoraires renders the label alone.
    "pro_bono": "Pro bono",
    "aide_juridique": "Aide juridique",
}

# Professional roles whose work address is preferred when present (§6.4).
_WORK_PREFERRED_ROLES = {"avocat_adverse", "expert", "huissier", "notaire"}

_FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
)

# Deliberately manual fields (§6.6) — no data source; the popup renders a
# scalar input (or select) with the suggested default.
MANUAL_FIELDS: dict[str, dict] = {
    "procédure": {"default": "", "options": None},
    "disposition": {"default": "", "options": None},
    "privilège": {
        "default": "",
        "options": ["SOUS TOUTES RÉSERVES", "PERSONNEL ET CONFIDENTIEL", "—"],
    },
    "transmission_lettre": {
        "default": "",
        "options": ["courriel", "huissier", "poste recommandée", "télécopieur"],
    },
    "objet_lettre": {"default": "", "options": None},
    "pièces_jointes": {"default": "Aucune", "options": None},
    "référence_externe": {"default": "", "options": None},
}

# NOTE — ``salutations`` and every civilité field are deliberately NOT
# resolved or prompted: they are passthrough (left as ``{{name}}`` in the
# output). The closing formula and the recipient's title must appear in
# letters but never in court procedures, so the user writes them in Word.


def is_uppercase_name(name: str) -> bool:
    """True when *name* has at least one letter and no lowercase letter.

    Drives the case-matching rule: an ALL-CAPS placeholder (``{{TRIBUNAL}}``)
    gets its resolved value upper-cased so it matches a legal-heading style.
    """
    return any(c.isalpha() for c in name) and name == name.upper()


def french_long_date(d: date) -> str:
    """``date(2026, 4, 25)`` → ``"25 avril 2026"`` (``1er`` for the 1st)."""
    day = "1er" if d.day == 1 else str(d.day)
    return f"{day} {_FRENCH_MONTHS[d.month - 1]} {d.year}"


def fallback_value(name: str, is_auto: bool) -> str:
    """Visible French placeholder for a missing value (§6.7 — exact strings).

    Auto-resolvable field left empty → data was missing; anything else
    (manual, block, unknown) left empty → the user must complete it.
    Generation never fails because of a missing value.
    """
    if is_auto:
        return f"[CHAMP MANQUANT : {name}]"
    return f"[À COMPLÉTER : {name}]"


# ── Partie helpers ──────────────────────────────────────────────────────

def _display_name(partie: dict) -> str:
    """Full name WITH the honorific — mirror of models.partie.display_name.

    Used only by the ``…_avec_civilite`` twin fields; the default name
    fields use :func:`_nom_bare`. Kept local so this module stays importable
    without the Firestore client.
    """
    if partie.get("type") == "organization":
        return partie.get("organization_name", "")
    parts = [partie.get("prefix", ""), partie.get("first_name", ""),
             partie.get("last_name", "")]
    return " ".join(p for p in parts if p).strip()


def _nom_bare(partie: dict) -> str:
    """Full name WITHOUT the honorific — the DEFAULT for ``…nom_complet``.

    A person cited in a procedure is named bare ("Jean Tremblay"); the
    civility is opt-in via the ``…_avec_civilite`` twin field. Organizations
    have no honorific, so this equals their legal name.
    """
    if partie.get("type") == "organization":
        return partie.get("organization_name", "")
    parts = [partie.get("first_name", ""), partie.get("last_name", "")]
    return " ".join(p for p in parts if p).strip()


# Honorific prefixes that models.partie.display_name prepends to a person's
# name. The demandeur/défendeur position fields resolve from the stored
# snapshot name (which already carries the prefix), so the default strips a
# leading one; the "_avec_civilite" twin keeps it.
_CIVILITY_PREFIXES = ("Me", "Mme", "M.")


def _strip_civility_prefix(name: str) -> str:
    """Drop a leading honorific (Me / Mme / M.) from a display name."""
    stripped = name.strip()
    for prefix in _CIVILITY_PREFIXES:
        if stripped.startswith(prefix + " "):
            return stripped[len(prefix) + 1:].strip()
    return stripped


def _selected_address(partie: dict) -> tuple[dict[str, str], bool]:
    """Return (address fields, is_work) per the §6.4 preference rule."""
    prefer_work = bool(
        partie.get("contact_role") in _WORK_PREFERRED_ROLES
        and (partie.get("work_address_street") or "").strip()
    )
    prefix = "work_address" if prefer_work else "address"
    fields = ("street", "unit", "city", "province", "postal_code", "country")
    addr = {k: (partie.get(f"{prefix}_{k}") or "").strip() for k in fields}
    return addr, prefer_work


def _adresse_civique(addr: dict[str, str]) -> Optional[str]:
    if not addr["street"]:
        return None
    if addr["unit"]:
        return f"{addr['street']}, {addr['unit']}"
    return addr["street"]


def _one_line_address(addr: dict[str, str]) -> Optional[str]:
    """`"{street}, {unit, }{city} ({province}) {postal}"` — country appended
    only when not Canada. Full province/country names post-Phase B."""
    if not addr["street"] or not addr["city"]:
        return None
    out = f"{addr['street']}, "
    if addr["unit"]:
        out += f"{addr['unit']}, "
    out += addr["city"]
    if addr["province"]:
        out += f" ({addr['province']})"
    if addr["postal_code"]:
        out += f" {addr['postal_code']}"
    if addr["country"] and addr["country"] != "Canada":
        out += f", {addr['country']}"
    return out


def _telephone(partie: dict) -> Optional[str]:
    for key in ("phone_work", "phone_cell", "phone_home"):
        number = (partie.get(key) or "").strip()
        if number:
            try:
                return format_phone_display(number)
            except Exception:
                return number
    return None


# ── Resolution context ──────────────────────────────────────────────────

@dataclass
class _Context:
    dossier: Optional[dict]
    client: Optional[dict]
    adverse: Optional[dict]
    destinataire: Optional[dict]
    firm: dict
    today: date


def _dossier_field(key: str) -> Callable[[_Context], Optional[str]]:
    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        return ctx.dossier.get(key) or None

    return resolver


def _dossier_money(key: str) -> Callable[[_Context], Optional[str]]:
    """Resolver for a dossier money field (integer cents) → fr-CA string."""
    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        cents = ctx.dossier.get(key)
        if cents is None:
            return None
        return format_cents_fr(int(cents))

    return resolver


def _dossier_percent(key: str) -> Callable[[_Context], Optional[str]]:
    """Resolver for a dossier rate field (basis points) → fr-CA percent."""
    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        bps = ctx.dossier.get(key)
        if bps is None:
            return None
        return format_rate_fr(int(bps), 100)

    return resolver


def _dossier_date(key: str) -> Callable[[_Context], Optional[str]]:
    """Resolver for a dossier date field (midnight-UTC datetime) → French long
    date, using the stored UTC calendar date (no Montréal shift)."""
    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        value = ctx.dossier.get(key)
        if value is None or not hasattr(value, "year"):
            return None
        return french_long_date(value)

    return resolver


def _dossier_classe(ctx: _Context) -> Optional[str]:
    """Value class (Roman numeral I–IV) computed from dossier.valeur."""
    if not ctx.dossier:
        return None
    return compute_class(ctx.dossier.get("valeur"))


def _dossier_prescription(ctx: _Context) -> Optional[str]:
    """French label of the dossier's prescription type (unset → unresolved)."""
    if not ctx.dossier:
        return None
    ptype = ctx.dossier.get("prescription_type") or ""
    return PRESCRIPTION_LABELS.get(ptype) if ptype else None


def _dossier_domaine(ctx: _Context) -> Optional[str]:
    """French label of the dossier's domaine (unset → unresolved)."""
    if not ctx.dossier:
        return None
    code = ctx.dossier.get("domaine") or ""
    return taxonomie.DOMAINE_LABELS.get(code) if code else None


def _dossier_action(ctx: _Context) -> Optional[str]:
    """The action as « Libellé [CODE] » — what a procedure cites."""
    if not ctx.dossier:
        return None
    return taxonomie.action_label(ctx.dossier.get("action") or "") or None


def _action_attr(attr: str) -> Callable[[_Context], Optional[str]]:
    """Resolver for one field of the dossier's taxonomy action."""

    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        action = taxonomie.get_action(ctx.dossier.get("action") or "")
        return getattr(action, attr) or None if action else None

    return resolver


def _role_feminin(ctx: _Context) -> Optional[str]:
    if not ctx.dossier:
        return None
    return _ROLE_FEMININ.get(ctx.dossier.get("role") or "")


def _role_label(ctx: _Context) -> Optional[str]:
    """Capitalized display label of the client's litigation role."""
    if not ctx.dossier:
        return None
    return _ROLE_LABEL.get(ctx.dossier.get("role") or "")


def _dossier_labelled(
    key: str, labels: dict[str, str]
) -> Callable[[_Context], Optional[str]]:
    """Resolver mapping a dossier enum field to its French label (unset → None)."""
    def resolver(ctx: _Context) -> Optional[str]:
        if not ctx.dossier:
            return None
        return labels.get(ctx.dossier.get(key) or "") or None

    return resolver


def format_honoraires_parts(dossier: Optional[dict]) -> Optional[tuple[str, str]]:
    """(type label, rate part) for the fee arrangement, or None.

    ("Horaire", "250,00 $/h"), ("Mixte", "250,00 $/h + 5 000,00 $ + 25 %"),
    ("Pro bono", "") — the rate part is "" for the rate-less types or when no
    component has a stored value. The dossier detail « Mandat » card renders
    the two parts itself (rate greyed in parentheses); gabarits get them
    joined by :func:`format_honoraires`.
    """
    if not dossier:
        return None
    fee_type = dossier.get("fee_type") or ""
    label = _FEE_TYPE_LABEL.get(fee_type)
    if not label:
        return None
    parts: list[str] = []
    hourly = dossier.get("hourly_rate")
    flat = dossier.get("flat_fee")
    percent = dossier.get("contingency_percent")
    if fee_type in ("hourly", "mixed") and hourly:
        parts.append(f"{format_cents_fr(int(hourly))}/h")
    if fee_type in ("flat", "mixed") and flat:
        parts.append(format_cents_fr(int(flat)))
    if fee_type in ("contingency", "mixed") and percent:
        parts.append(format_rate_fr(int(percent), 100))
    return label, " + ".join(parts)


def format_honoraires(dossier: Optional[dict]) -> Optional[str]:
    """« Type d'honoraires et taux » as one string, or None.

    « Horaire — 250,00 $/h », « Forfaitaire — 5 000,00 $ »,
    « Contingence — 25 % », « Mixte — 250,00 $/h + 5 000,00 $ + 25 % ». A
    component with no stored value is omitted; a type with none of them → the
    label alone. This joined form is the gabarit {{dossier.honoraires}} —
    keep its format stable; the detail card composes the parts itself.
    """
    parts = format_honoraires_parts(dossier)
    if parts is None:
        return None
    label, rate = parts
    return f"{label} — {rate}" if rate else label


def retention_date(closed_date):
    """Dossier document-retention date = closure date + 7 years, or None.

    Feb-29 closures fall back to Feb-28 of the target year. Shared with the
    dossier detail « Mandat » card (routes.dossiers). Indicative only — never a
    legal deadline. Preserves the input's UTC calendar date (no Montréal shift).
    """
    if not closed_date or not hasattr(closed_date, "year"):
        return None
    try:
        return closed_date.replace(year=closed_date.year + 7)
    except ValueError:
        return closed_date.replace(year=closed_date.year + 7, day=28)


def _dossier_honoraires(ctx: _Context) -> Optional[str]:
    return format_honoraires(ctx.dossier)


def _dossier_retention(ctx: _Context) -> Optional[str]:
    if not ctx.dossier:
        return None
    rd = retention_date(ctx.dossier.get("closed_date"))
    return french_long_date(rd) if rd else None


def _sides(ctx: _Context) -> tuple[Optional[list], Optional[list], Optional[dict], Optional[dict]]:
    """(demandeur names[], défendeur names[], demandeur partie, défendeur partie).

    Positions derive from dossier.role: our side (clients) is the
    demandeur when role == demandeur, the défendeur when role == défendeur;
    other roles → unresolved (§6.2). The representative partie of each
    side is the corresponding slot selection (client / adverse), which
    defaults to the side's first entry.
    """
    if not ctx.dossier:
        return None, None, None, None
    role = ctx.dossier.get("role")
    clients = ctx.dossier.get("clients") or []
    opposing = ctx.dossier.get("opposing_parties") or []
    if role == "demandeur":
        return clients, opposing, ctx.client, ctx.adverse
    if role == "défendeur":
        return opposing, clients, ctx.adverse, ctx.client
    return None, None, None, None


def _joined_names(entries: Optional[list], *, with_civility: bool = False) -> Optional[str]:
    """Join a side's party names for {{dossier.demandeur}}/{{…defendeur}}.

    The stored snapshot name carries the honorific (models.partie.display_name
    prepends the prefix). By DEFAULT a procedure intitulé names the party
    bare, so the civility is stripped; the ``…_avec_civilite`` twin passes
    ``with_civility=True`` to keep it.
    """
    if not entries:
        return None
    names = [
        (e.get("name", "") if with_civility else _strip_civility_prefix(e.get("name", "")))
        for e in entries
        if e.get("name")
    ]
    names = [n.strip() for n in names if n and n.strip()]
    return ", ".join(names) if names else None


def _side_names(index: int, *, with_civility: bool = False) -> Callable[[_Context], Optional[str]]:
    def resolver(ctx: _Context) -> Optional[str]:
        return _joined_names(_sides(ctx)[index], with_civility=with_civility)

    return resolver


def _side_address(index: int) -> Callable[[_Context], Optional[str]]:
    def resolver(ctx: _Context) -> Optional[str]:
        partie = _sides(ctx)[index + 2]
        if not partie:
            return None
        addr, _ = _selected_address(partie)
        return _one_line_address(addr)

    return resolver


def _partie(slot: str, fn: Callable[[dict], Optional[str]]) -> Callable[[_Context], Optional[str]]:
    def resolver(ctx: _Context) -> Optional[str]:
        partie = getattr(ctx, slot)
        if not partie:
            return None
        return fn(partie) or None

    return resolver


def _individual_field(key: str) -> Callable[[dict], Optional[str]]:
    def fn(partie: dict) -> Optional[str]:
        if partie.get("type") == "organization":
            return None
        return partie.get(key) or None

    return fn


def _organisation(partie: dict) -> Optional[str]:
    return partie.get("organization") or partie.get("organization_name") or None


def _addr_component(key: str) -> Callable[[dict], Optional[str]]:
    def fn(partie: dict) -> Optional[str]:
        addr, _ = _selected_address(partie)
        return addr[key] or None

    return fn


def _courriel(partie: dict) -> Optional[str]:
    _, is_work = _selected_address(partie)
    return (partie.get("email_work") if is_work else partie.get("email")) or None


def _firm_field(key: str) -> Callable[[_Context], Optional[str]]:
    def resolver(ctx: _Context) -> Optional[str]:
        return (ctx.firm or {}).get(key) or None

    return resolver


def _partie_fields(slot: str) -> dict[str, tuple[Optional[str], Callable]]:
    return {
        # Bare by default; the twin keeps the honorific (Me / M. / Mme).
        f"{slot}.nom_complet": (slot, _partie(slot, _nom_bare)),
        f"{slot}.nom_complet_avec_civilite": (slot, _partie(slot, _display_name)),
        f"{slot}.prenom": (slot, _partie(slot, _individual_field("first_name"))),
        f"{slot}.nom": (slot, _partie(slot, _individual_field("last_name"))),
        f"{slot}.organisation": (slot, _partie(slot, _organisation)),
        f"{slot}.adresse_civique": (
            slot, _partie(slot, lambda p: _adresse_civique(_selected_address(p)[0]))
        ),
        f"{slot}.ville": (slot, _partie(slot, _addr_component("city"))),
        f"{slot}.province": (slot, _partie(slot, _addr_component("province"))),
        f"{slot}.code_postal": (slot, _partie(slot, _addr_component("postal_code"))),
        f"{slot}.pays": (slot, _partie(slot, _addr_component("country"))),
        f"{slot}.adresse_complete": (
            slot, _partie(slot, lambda p: _one_line_address(_selected_address(p)[0]))
        ),
        f"{slot}.courriel": (slot, _partie(slot, _courriel)),
        f"{slot}.telephone": (slot, _partie(slot, _telephone)),
        f"{slot}.numero_barreau": (
            slot, _partie(slot, lambda p: p.get("bar_number") or None)
        ),
    }


# canonical field name -> (slot | None, resolver(_Context) -> Optional[str])
CATALOG: dict[str, tuple[Optional[str], Callable[[_Context], Optional[str]]]] = {
    # dossier.* (§6.1)
    "dossier.titre": ("dossier", _dossier_field("title")),
    # Free-text case summary. May hold several paragraphs — fill_docx expands
    # blank-line-separated chunks into cloned paragraphs (value-driven).
    "dossier.sommaire": ("dossier", _dossier_field("sommaire")),
    "dossier.numero_cour": ("dossier", _dossier_field("court_file_number")),
    "dossier.reference_interne": ("dossier", _dossier_field("file_number")),
    "dossier.tribunal": ("dossier", _dossier_field("tribunal")),
    "dossier.chambre": ("dossier", _dossier_field("competence")),
    "dossier.district": ("dossier", _dossier_field("district_judiciaire")),
    "dossier.palais": ("dossier", _dossier_field("palais_de_justice")),
    "dossier.role": ("dossier", _dossier_field("role")),
    "dossier.role_feminin": ("dossier", _role_feminin),
    "dossier.role_label": ("dossier", _role_label),
    # Derived party positions (§6.2) — bare by default, "_avec_civilite" twin
    "dossier.demandeur": ("dossier", _side_names(0)),
    "dossier.defendeur": ("dossier", _side_names(1)),
    "dossier.demandeur_avec_civilite": ("dossier", _side_names(0, with_civility=True)),
    "dossier.defendeur_avec_civilite": ("dossier", _side_names(1, with_civility=True)),
    "dossier.adresse_demandeur": ("dossier", _side_address(0)),
    "dossier.adresse_defendeur": ("dossier", _side_address(1)),
    # dossier.* recours & prescription (see utils/recours.py, utils/taxonomie.py)
    "dossier.domaine": ("dossier", _dossier_domaine),
    "dossier.action": ("dossier", _dossier_action),
    "dossier.action_code": ("dossier", _dossier_field("action")),
    "dossier.action_libelle": ("dossier", _action_attr("libelle")),
    "dossier.precision": ("dossier", _dossier_field("action_precision")),
    "dossier.delai": ("dossier", _action_attr("delai")),
    # `references` split (July 2026): `dossier.reference` keeps its meaning —
    # the statutory source of the DELAY (now `ref_delai`) — and the new
    # `dossier.fondement` cites the seat of the right of action.
    "dossier.reference": ("dossier", _action_attr("ref_delai")),
    "dossier.fondement": ("dossier", _action_attr("ref_fondement")),
    "dossier.point_depart": ("dossier", _action_attr("point_depart")),
    # « Objet » was renamed « Action » (July 2026). The old placeholder is kept
    # pointing at the action label so existing gabarits keep filling — it now
    # renders « Libellé [CODE] » instead of the old free text.
    "dossier.objet": ("dossier", _dossier_action),
    "dossier.valeur": ("dossier", _dossier_money("valeur")),
    "dossier.classe": ("dossier", _dossier_classe),
    "dossier.prescription": ("dossier", _dossier_prescription),
    "dossier.droit_action": ("dossier", _dossier_date("droit_action_date")),
    "dossier.date_pour_agir": ("dossier", _dossier_date("prescription_date")),
    # dossier.* mandate / fees / lifecycle (« Mandat » card)
    "dossier.type_mandat": ("dossier", _dossier_labelled("mandate_type", _MANDATE_TYPE_LABEL)),
    # « Type de dossier » became « Domaine » (July 2026); the old placeholder
    # keeps resolving, now to the domaine label.
    "dossier.type_dossier": ("dossier", _dossier_domaine),
    "dossier.type_honoraires": ("dossier", _dossier_labelled("fee_type", _FEE_TYPE_LABEL)),
    "dossier.honoraires": ("dossier", _dossier_honoraires),
    "dossier.taux_horaire": ("dossier", _dossier_money("hourly_rate")),
    "dossier.forfait": ("dossier", _dossier_money("flat_fee")),
    "dossier.pourcentage": ("dossier", _dossier_percent("contingency_percent")),
    "dossier.notes_honoraires": ("dossier", _dossier_field("fee_notes")),
    "dossier.ouverture": ("dossier", _dossier_date("opened_date")),
    "dossier.fermeture": ("dossier", _dossier_date("closed_date")),
    "dossier.retention": ("dossier", _dossier_retention),
    # cabinet.* (§6.5)
    "cabinet.nom": (None, _firm_field("nom")),
    "cabinet.adresse_civique": (None, _firm_field("adresse_civique")),
    "cabinet.ville": (None, _firm_field("ville")),
    "cabinet.province": (None, _firm_field("province")),
    "cabinet.code_postal": (None, _firm_field("code_postal")),
    "cabinet.telephone": (None, _firm_field("telephone")),
    "cabinet.courriel": (None, _firm_field("courriel")),
    # date.* (§6.5)
    "date.aujourdhui": (None, lambda ctx: french_long_date(ctx.today)),
    "date.aujourdhui_iso": (None, lambda ctx: ctx.today.isoformat()),
    # Partie slots (§6.3)
    **_partie_fields("client"),
    **_partie_fields("adverse"),
    **_partie_fields("destinataire"),
}


def _register_civility_variants() -> None:
    """Register accented spellings of the ``_avec_civilite`` twins.

    A French-typed ``{{…avec_civilité}}`` then resolves like the ASCII
    canonical ``{{…avec_civilite}}`` (matching is case-insensitive but not
    accent-insensitive).
    """
    for name in [n for n in CATALOG if n.endswith("_avec_civilite")]:
        CATALOG[name.replace("_avec_civilite", "_avec_civilité")] = CATALOG[name]


_register_civility_variants()

# Flat alias table (§6.6 — exhaustive; compatibility with the four
# existing gabarits and the user's Claude.ai skills).
FLAT_ALIASES: dict[str, str] = {
    "district": "dossier.district",
    "numero_dossier": "dossier.numero_cour",
    "tribunal": "dossier.tribunal",
    "chambre": "dossier.chambre",
    "référence_interne": "dossier.reference_interne",
    "intitulé_dossier": "dossier.titre",
    "sommaire": "dossier.sommaire",
    "rôle": "dossier.role_feminin",
    "demandeur": "dossier.demandeur",
    "défendeur": "dossier.defendeur",
    "demandeur_avec_civilité": "dossier.demandeur_avec_civilite",
    "demandeur_avec_civilite": "dossier.demandeur_avec_civilite",
    "défendeur_avec_civilité": "dossier.defendeur_avec_civilite",
    "défendeur_avec_civilite": "dossier.defendeur_avec_civilite",
    "adresse_demandeur": "dossier.adresse_demandeur",
    "adresse_défendeur": "dossier.adresse_defendeur",
    "valeur": "dossier.valeur",
    "classe": "dossier.classe",
    "prescription": "dossier.prescription",
    "droit_action": "dossier.droit_action",
    "date_pour_agir": "dossier.date_pour_agir",
    "type_mandat": "dossier.type_mandat",
    "type_dossier": "dossier.type_dossier",
    # Taxonomy (July 2026). `objet` had no flat alias before — a skill emitting
    # {{objet}} fell silently into passthrough — so it gains one here, aliased
    # to the action like its namespaced twin.
    "domaine": "dossier.domaine",
    "action": "dossier.action",
    "objet": "dossier.objet",
    "précision": "dossier.precision",
    "precision": "dossier.precision",
    "délai": "dossier.delai",
    "delai": "dossier.delai",
    "référence_action": "dossier.reference",
    "reference_action": "dossier.reference",
    "fondement": "dossier.fondement",
    "référence_fondement": "dossier.fondement",
    "reference_fondement": "dossier.fondement",
    "point_départ": "dossier.point_depart",
    "point_depart": "dossier.point_depart",
    "date_ouverture": "dossier.ouverture",
    "date_fermeture": "dossier.fermeture",
    "rétention": "dossier.retention",
    "retention": "dossier.retention",
    "ville_procédure": "cabinet.ville",
    "ville_lettre": "cabinet.ville",
    "date_procédure": "date.aujourdhui",
    "date_lettre": "date.aujourdhui",
    "prénom_récipient": "destinataire.prenom",
    "nom_récipient": "destinataire.nom",
    "cabinet_récipient": "destinataire.organisation",
    "adresse_civique_récipient": "destinataire.adresse_civique",
    "ville_récipient": "destinataire.ville",
    "province_récipient": "destinataire.province",
    "code_postal_récipient": "destinataire.code_postal",
    "pays_récipient": "destinataire.pays",
}


# Case-insensitive lookup indexes: a placeholder resolves to a catalog
# field whatever its case, so ``{{TRIBUNAL}}``/``{{Tribunal}}`` behave like
# ``{{tribunal}}``. ``str.lower()`` folds French accents (``É`` → ``é``),
# and the canonical catalog/alias keys are unique when lower-cased.
_ALIAS_CI: dict[str, str] = {flat.lower(): canonical for flat, canonical in FLAT_ALIASES.items()}
_CATALOG_CI: dict[str, str] = {name.lower(): name for name in CATALOG}


def _canonical_for(name: str) -> Optional[str]:
    """Canonical catalog field for *name* (case-insensitive), or None.

    A flat alias wins over a same-spelled namespaced field (the alias table
    is the compatibility layer for the existing gabarits).
    """
    key = name.lower()
    if key in _ALIAS_CI:
        return _ALIAS_CI[key]
    if key in _CATALOG_CI:
        return _CATALOG_CI[key]
    return None


# ── Public API (§6.8) ───────────────────────────────────────────────────

@dataclass
class Classification:
    """Placeholder buckets (see the module docstring).

    * ``auto`` — name → canonical catalog field (case-insensitive match).
    * ``manual`` — known :data:`MANUAL_FIELDS` prompted in the popup.
    * ``passthrough`` — everything else (former ALL-CAPS blocks, civilité,
      salutations, unknown names): left verbatim in the output for Word.
    """
    auto: dict[str, str] = field(default_factory=dict)
    manual: list[str] = field(default_factory=list)
    passthrough: list[str] = field(default_factory=list)
    slots_required: set[str] = field(default_factory=set)


def classify_placeholders(names: list[str]) -> Classification:
    """Classify placeholder names into auto / manual / passthrough.

    ``auto`` maps each name to its canonical catalog field (case-insensitive,
    through :data:`FLAT_ALIASES` when flat); ``slots_required`` is the union
    of the slots those fields need. Anything the app does not fill —
    free-form content, civilité, salutations, unknown names — lands in
    ``passthrough`` and is left untouched in the generated document.
    """
    result = Classification()
    for name in names:
        canonical = _canonical_for(name)
        if canonical is not None:
            result.auto[name] = canonical
            slot = CATALOG[canonical][0]
            if slot:
                result.slots_required.add(slot)
        elif name in MANUAL_FIELDS:
            result.manual.append(name)
        else:
            result.passthrough.append(name)
    return result


def resolve_values(
    names: list[str],
    *,
    dossier: Optional[dict],
    client: Optional[dict],
    adverse: Optional[dict],
    destinataire: Optional[dict],
    firm: dict,
    today: date,
) -> dict[str, str]:
    """Resolve every auto-resolvable name that has non-empty source data.

    Matching is case-insensitive; a value resolved for an ALL-CAPS
    placeholder is upper-cased so it matches a legal-heading style
    (``{{TRIBUNAL}}`` → ``COUR SUPÉRIEURE``). Names absent from the result
    are unresolved — the popup shows them as empty inputs, and a blank
    submission yields the visible French placeholder from
    :func:`fallback_value`.
    """
    ctx = _Context(
        dossier=dossier,
        client=client,
        adverse=adverse,
        destinataire=destinataire,
        firm=firm or {},
        today=today,
    )
    resolved: dict[str, str] = {}
    for name in names:
        canonical = _canonical_for(name)
        if canonical is None:
            continue
        entry = CATALOG.get(canonical)
        if entry is None:
            continue
        value = entry[1](ctx)
        if isinstance(value, str):
            value = value.strip()
        if value:
            resolved[name] = value.upper() if is_uppercase_name(name) else value
    return resolved
