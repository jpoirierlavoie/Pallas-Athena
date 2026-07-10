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


def _role_feminin(ctx: _Context) -> Optional[str]:
    if not ctx.dossier:
        return None
    return _ROLE_FEMININ.get(ctx.dossier.get("role") or "")


def _role_label(ctx: _Context) -> Optional[str]:
    """Capitalized display label of the client's litigation role."""
    if not ctx.dossier:
        return None
    return _ROLE_LABEL.get(ctx.dossier.get("role") or "")


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
    "rôle": "dossier.role_feminin",
    "demandeur": "dossier.demandeur",
    "défendeur": "dossier.defendeur",
    "demandeur_avec_civilité": "dossier.demandeur_avec_civilite",
    "demandeur_avec_civilite": "dossier.demandeur_avec_civilite",
    "défendeur_avec_civilité": "dossier.defendeur_avec_civilite",
    "défendeur_avec_civilite": "dossier.defendeur_avec_civilite",
    "adresse_demandeur": "dossier.adresse_demandeur",
    "adresse_défendeur": "dossier.adresse_defendeur",
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
