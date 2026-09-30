"""Dossier (case file) Firestore CRUD and RFC-5545 VJOURNAL serialization."""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

import icalendar

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, concurrency, db, provenance, reference
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils import taxonomie
from utils.deadlines import next_juridical_day
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils import deadlines
from utils.recours import (
    VALID_PRESCRIPTION_TYPES,
    compute_date_pour_agir,
    compute_echeances,
    prescription_period,
)

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "dossiers"

# Valid enum values
#
# Domaine + action — the two-level taxonomy (July 2026), replacing the old
# free-form « Type de dossier » (matter_type) and « Objet » (free text).
# Vocabulary and labels live in utils/taxonomie.py, NOT here: a 162-row legal
# table has no business in the Firestore layer, and template_fields.py needs
# it without the Firestore client. "" is valid for both — a dossier need not
# be classified.
VALID_DOMAINES = taxonomie.VALID_DOMAINES
VALID_ACTIONS = taxonomie.VALID_ACTIONS

# Type de mandat — nature of the engagement (new July 2026).
VALID_MANDATE_TYPES = (
    "judiciaire",
    "service_conseils",
    "general",
    "special",
)
VALID_COURTS = (
    "Cour supérieure",
    "Cour du Québec",
    "Tribunal administratif",
    "Cour d'appel",
    "Cour des petites créances",
    "autre",
)
VALID_DISTRICTS = (
    "Montréal",
    "Québec",
    "Laval",
    "Longueuil",
    "Gatineau",
    "Sherbrooke",
    "Trois-Rivières",
    "Saguenay",
    "Drummondville",
    "Saint-Hyacinthe",
    "Saint-Jean-sur-Richelieu",
    "Joliette",
    "Rimouski",
    "Rouyn-Noranda",
    "Val-d'Or",
    "autre",
)
# Per-party litigation roles (July 2026 rework). Each entry of `clients`/
# `opposing_parties` carries `roles: [...] ⊆ PARTY_ROLES` — a party may hold
# several (e.g. défendeur + demandeur reconventionnel). The dossier-level
# `role` field is DERIVED from the first client that has one (see
# _derive_role); it survives only because the gabarit placeholders
# ({{dossier.role}}, role_feminin, and the demandeur/défendeur positions in
# utils/template_fields.py) read it.
PARTY_ROLES = (
    "demandeur",
    "défendeur",
    "demandeur reconventionnel",
    "défendeur reconventionnel",
    "mis en cause",
    "intervenant",
    "appelant",
    "intimé",
    "requérant",
    "autre",
)
VALID_FEE_TYPES = (
    "hourly", "flat", "contingency", "mixed", "pro_bono", "aide_juridique",
)
VALID_STATUSES = ("actif", "en_attente", "fermé", "archivé")
# Forum (July 2026, four-way — replaced the binary judiciaire/"autre" toggle):
# "judiciaire" = Québec judicial court (file number parsed); "administratif" /
# "federal" = a body picked from reference._FORUMS (file number stored
# verbatim); "prejudiciaire" = no proceedings filed yet — only the district
# is entered, and the file number is forced to PREJUDICIAIRE_FILE_NUMBER so
# gabarits can cite it until a real number crushes it via the parser.
VALID_FORUM_TYPES = ("judiciaire", "administratif", "federal", "prejudiciaire")
_FORUM_TYPE_CATEGORY = {
    "administratif": reference.ADMINISTRATIF,
    "federal": reference.FEDERAL,
}
PREJUDICIAIRE_FILE_NUMBER = "Préjudiciaire"

# Display labels (French)
#
# Domaine labels are NOT redefined here — they derive from the taxonomy table,
# so there is exactly one place to edit. (Contrast MANDATE_TYPE_LABELS /
# FEE_TYPE_LABELS below, which utils/template_fields.py must mirror by hand.)
DOMAINE_LABELS = taxonomie.DOMAINE_LABELS
MANDATE_TYPE_LABELS = {
    "judiciaire": "Judiciaire (ad litem)",
    "service_conseils": "Service-conseils",
    "general": "Général",
    "special": "Spécial",
}
FORUM_TYPE_LABELS = {
    "judiciaire": "Tribunal de droit commun",
    "administratif": "Tribunal administratif",
    "federal": "Cour ou tribunal fédéral",
    "prejudiciaire": "Préjudiciaire",
}
# Retired type-de-mandat keys → current vocabulary, applied on read
# (_migrate_mandate_type). The vocabulary was reworked July 2026 to
# « Judiciaire (ad litem) / Service-conseils / Général / Spécial » (user
# decision); "consultation" → "service_conseils" (same meaning),
# "transactionnel" → "special", and everything else ("autre", and the older
# "mediation_arbitrage" that used to fold into "autre") → "general". Without
# this, editing a dossier that still carries a retired key would trip
# _validate's mandate_type check.
_MANDATE_TYPE_MIGRATION = {
    "consultation": "service_conseils",
    "transactionnel": "special",
    "autre": "general",
    "mediation_arbitrage": "general",
}
# Legacy « Type de dossier » (matter_type) → « Domaine », applied on read
# (_migrate_domaine). Only the UNAMBIGUOUS keys are mapped:
#
#   action_dommages → ""  because it is genuinely ambiguous: damages can be
#                         contractual (CON-02) or extracontractual (RCV-*).
#                         Guessing would silently mislabel the file's whole
#                         liability regime (art. 1458 al. 2 C.c.Q. non-cumul).
#   autre           → ""  it said nothing to begin with.
#
# "" renders « — » until the user classifies the dossier on the next edit.
# The old subject-matter keys (litige_civil/litige_commercial/familial) were
# already folded into "autre" by the July 2026 reclassification, so they
# arrive here as "autre" and land on "" too.
_MATTER_TYPE_TO_DOMAINE = {
    "recouvrement": "REC",
    "injonction": "INJ",
    "recours_extraordinaire": "CJP",
    "vice_cache": "CON",
    "action_dommages": "",
    "autre": "",
    "litige_civil": "",
    "litige_commercial": "",
    "familial": "",
}
STATUS_LABELS = {
    "actif": "Actif",
    "en_attente": "En attente",
    "fermé": "Fermé",
    "archivé": "Archivé",
}
PARTY_ROLE_LABELS = {
    "demandeur": "Demandeur",
    "défendeur": "Défendeur",
    "demandeur reconventionnel": "Demandeur reconventionnel",
    "défendeur reconventionnel": "Défendeur reconventionnel",
    "mis en cause": "Mis en cause",
    "intervenant": "Intervenant",
    "appelant": "Appelant",
    "intimé": "Intimé",
    "requérant": "Requérant",
    "autre": "Autre",
}
ROLE_LABELS = PARTY_ROLE_LABELS
FEE_TYPE_LABELS = {
    "hourly": "Horaire",
    "flat": "Forfaitaire",
    "contingency": "Contingence",
    "mixed": "Mixte",
    # Rate-less arrangements: no taux/forfait/pourcentage input applies, so
    # format_honoraires renders the label alone.
    "pro_bono": "Pro bono",
    "aide_juridique": "Aide juridique",
}


def field_defaults() -> dict:
    """Public view of the default document — the definition of « unset ».

    The MCP complete_dossier tool fills ONLY fields whose current value is
    empty or still equal to this default (a dossier whose hourly_rate is
    the 30000 default counts as « never set »). Exposed as a function so
    the tool cannot drift from the real defaults.
    """
    return _default_doc()


def _default_doc() -> dict:
    """Return a dict with every dossier field set to its default value."""
    return {
        "id": "",
        "file_number": "",
        "title": "",
        # Free-text case summary, shown in its own card on the detail page.
        "sommaire": "",
        # Parties on the dossier (arrays of {id, name} dicts)
        "clients": [],
        "client_ids": [],
        "opposing_parties": [],
        "opposing_party_ids": [],
        # Flat mirror of every avocat_id carried by a party entry (July
        # 2026) — serves the array_contains FK check on partie deletion.
        "avocat_ids": [],
        # Case classification. Domaine/action default to UNSET rather than to
        # a guess: the old matter_type defaulted to "action_dommages", which
        # silently classified every new dossier as an unrelated recourse.
        "domaine": "",
        "mandate_type": "judiciaire",
        "court_file_number": "",
        "district_judiciaire": "",
        "tribunal": "",
        "competence": "",
        "palais_de_justice": "",
        "greffe_number": "",
        "juridiction_number": "",
        "is_administrative_tribunal": False,
        # Forum — see VALID_FORUM_TYPES. "judiciaire" = a Québec judicial
        # court whose file number the parser resolves; "administratif"/
        # "federal" = a body from the reference list (`forum` slug), file
        # number stored unparsed; "prejudiciaire" = nothing filed yet
        # (district only, file number forced to « Préjudiciaire »).
        "forum_type": "judiciaire",
        "forum": "",
        # DERIVED (July 2026): first role of the first client that has one —
        # never user-entered. Kept for the gabarits. See _derive_role.
        "role": "",
        # Financial
        "hourly_rate": 30000,
        "flat_fee": None,
        "contingency_percent": None,          # basis points: 2500 = 25,00 %
        "fee_type": "hourly",
        "fee_notes": "",
        # Status
        "status": "actif",
        "opened_date": None,
        "closed_date": None,
        # Trust accounting (fidéicommis, Phase K). Book + cleared balances,
        # maintained transactionally by models/trust.py — never edited through
        # the dossier form. Default 0 / {} so callers never see None (mirrored
        # on read by _migrate_trust).
        "trust_balance": 0,
        "trust_balance_by_client": {},
        "trust_cleared_by_client": {},
        # Recours & prescription
        "action": "",
        "action_precision": "",
        "valeur": None,
        "prescription_type": "",
        "droit_action_date": None,
        "prescription_date": None,
        # Confirmed prior-notice date (avis préalable) — manual, optional,
        # NEVER auto-derived (each avis has its own starting point: délivrance
        # du bien, cause d'action… — not droit_action_date). Additive July
        # 2026; absent on legacy docs means "no confirmed avis".
        "date_avis": None,
        # Date de l'acte qui a interrompu la prescription — la demande déposée
        # (art. 2892 C.c.Q.). MANUELLE et jamais dérivée, comme date_avis :
        # seul le juriste sait ce qui a été fait et quand. Sa présence TAIT
        # l'alerte de prescription partout (list_prescription_alerts,
        # _attach_prescription_warnings) ; la vider la réveille. Additive
        # juillet 2026 ; absente sur un dossier hérité = aucune action prise,
        # ce qui est le bon défaut.
        "prise_action_date": None,
        # Événements de prescription (juillet 2026, PA-G02 — décision
        # utilisateur 2026-07-30 : modèle d'événements complet). Saisis
        # MANUELLEMENT (formulaire + outil MCP record_prescription_event),
        # jamais dérivés. Le prescription_date BRUT ci-dessus n'est JAMAIS
        # recalculé à partir d'eux (provenance — le test épinglé
        # test_deadline_never_touches_prise_action_date survit) ; la
        # projection dérivée vit dans derive_prescription(). Chaque entrée :
        # {id, type ∈ VALID_PRESCRIPTION_EVENT_TYPES, date (date seule à
        # minuit UTC), end_date (suspension seulement), reference,
        # document_id}. Additif — absent sur un dossier hérité = aucun
        # événement.
        "prescription_events": [],
        "prescription_notes": "",
        # Significations (juillet 2026, PA-G01 — le manque le plus coûteux
        # de l'audit : « quand chaque défendeur a-t-il été signifié » était
        # sans réponse, la seule trace vivant dans des NOMS de fichiers).
        # Tableau sur le document (précédent mandataires — pas de
        # collection, pas d'index) ; saisi MANUELLEMENT (formulaire +
        # record_signification). Chaque entrée : {id, partie_id (une partie
        # AU dossier — les délais des art. 145/147 courent PAR partie),
        # date (date seule à minuit UTC), mode ∈ VALID_SIGNIFICATION_MODES,
        # huissier_id, pv_document_id, superseded_by (l'entrée que celle-ci
        # remplace — le cas du second PV Solo), confirmee}. La dérivation
        # des échéances de réponse est une phase ULTÉRIEURE — le tableau
        # est la couture.
        "significations": [],
        # Metadata
        # Identifiant de l'enregistrement dans le système d'origine,
        # posé par la reprise historique (août 2026) ; « » partout
        # ailleurs. C'est l'ancre anti-doublon DURABLE : la clé
        # d'idempotence MCP expire en 24 h et une reprise s'étale sur
        # des jours, donc seul un identifiant porté par la donnée
        # elle-même permet à une reprise interrompue de retrouver ce
        # qu'elle a déjà écrit. Jamais sérialisé en vCard ni en iCal.
        "legacy_ref": "",
        "created_at": None,
        "updated_at": None,
        "etag": "",
        # DAV
        "vjournal_uid": "",
        "dav_href": "",
    }


_SOMMAIRE_MAX_LENGTH = 5000


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*.

    ``sommaire`` is a long-form summary and gets a wider bound than the
    single-line fields (mirrors ``models.note``'s content/field split).
    """
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            limit = _SOMMAIRE_MAX_LENGTH if key == "sommaire" else 2000
            out[key] = sanitize(val, max_length=limit)
        else:
            out[key] = val
    return out


def normalize_forum(data: dict) -> None:
    """Reconcile the forum fields in place, authoritatively over client input.

    Called by the route before validation, so validation and the write see a
    consistent forum.

    - "judiciaire" (or legacy/absent) → no forum; the parsed judicial metadata
      already in ``data`` stands. Whatever a préjudiciaire dossier held is
      CRUSHED here by the parser's output once a real number is entered.
    - "administratif"/"federal" → the selected forum's name IS the
      ``tribunal``, and the Québec judicial-court fields (greffe/juridiction/
      district/palais/competence) do not apply, so they are cleared;
      ``is_administrative_tribunal`` is True only for an administrative
      tribunal, never a federal court.
    - "prejudiciaire" → nothing is filed yet: only the user-entered
      ``district_judiciaire`` is kept, every other judicial field is cleared,
      and ``court_file_number`` is FORCED to ``PREJUDICIAIRE_FILE_NUMBER`` so
      a gabarit's ``{{dossier.numero_cour}}`` cites « Préjudiciaire ».
    """
    forum_type = data.get("forum_type", "")

    if forum_type == "prejudiciaire":
        data["forum"] = ""
        data["court_file_number"] = PREJUDICIAIRE_FILE_NUMBER
        data["tribunal"] = ""
        data["competence"] = ""
        data["palais_de_justice"] = ""
        data["greffe_number"] = ""
        data["juridiction_number"] = ""
        data["is_administrative_tribunal"] = False
        return

    if forum_type not in _FORUM_TYPE_CATEGORY:
        data["forum"] = ""
        return

    forum = reference.get_forum(data.get("forum", ""))
    if not forum or forum["category"] != _FORUM_TYPE_CATEGORY[forum_type]:
        # Invalid/blank/cross-category slug — _validate will reject it; don't
        # wipe the judicial fields on an about-to-fail submission.
        return
    data["tribunal"] = forum["name"]
    data["competence"] = ""
    data["district_judiciaire"] = ""
    data["palais_de_justice"] = ""
    data["greffe_number"] = ""
    data["juridiction_number"] = ""
    data["is_administrative_tribunal"] = forum_type == "administratif"


def _derive_role(data: dict) -> None:
    """Derive the dossier-level ``role`` from the per-party roles, in place.

    ``role`` stopped being user-entered in July 2026: the source of truth is
    ``clients[].roles``, and this keeps the derived field equal to the first
    role of the first client that has one (the clients' side, which is what
    the old field described). It survives only for the gabarits —
    ``{{dossier.role}}``, its feminine form, and the demandeur/défendeur
    intitulé positions all read it. Editing ``role`` by hand is pointless:
    the next save recomputes it.
    """
    data["role"] = next(
        (c["roles"][0] for c in data.get("clients", []) if c.get("roles")),
        "",
    )


def _rebuild_party_mirrors(data: dict) -> None:
    """Rebuild the flat ID mirrors from the object arrays, in place.

    ``client_ids``/``opposing_party_ids`` serve the ``array_contains``
    queries; ``avocat_ids`` (July 2026) does the same for the per-party
    avocat links, so the partie-deletion FK check can refuse to delete a
    lawyer still referenced by a dossier. Single-field indexes only — no
    composite needed.
    """
    clients = data.get("clients", [])
    opposing = data.get("opposing_parties", [])
    data["client_ids"] = [c["id"] for c in clients]
    data["opposing_party_ids"] = [p["id"] for p in opposing]
    data["avocat_ids"] = sorted(
        {e["avocat_id"] for e in clients + opposing if e.get("avocat_id")}
    )


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre du dossier est requis.")

    if not data.get("clients"):
        errors.append("Au moins un client doit être associé au dossier.")

    # Per-party roles/avocat (July 2026). The form's whitelist parser cannot
    # produce an invalid role, but a hand-crafted POST can; and a party
    # cannot be its own lawyer.
    for entry in list(data.get("clients", [])) + list(
        data.get("opposing_parties", [])
    ):
        for role in entry.get("roles", []) or []:
            if role not in PARTY_ROLES:
                errors.append("Rôle de partie invalide.")
                break
        if entry.get("avocat_id") and entry.get("avocat_id") == entry.get("id"):
            errors.append("Une partie ne peut pas être son propre avocat.")

    if not data.get("file_number", "").strip():
        errors.append("Le numéro de dossier est requis.")

    # domaine/action are presence-gated like mandate_type: a legacy dossier
    # read straight from Firestore has neither, and an unconditional check
    # would lock it out of editing entirely. "" is a valid value for both.
    domaine = data.get("domaine", "")
    if "domaine" in data and domaine not in VALID_DOMAINES:
        errors.append("Domaine invalide.")

    action = data.get("action", "")
    if "action" in data and action not in VALID_ACTIONS:
        errors.append("Action invalide.")
    elif action and domaine and taxonomie.domaine_of(action) != domaine:
        # The cascading picker cannot produce this pair, but a hand-crafted
        # POST can. Left unchecked it would show an action under a domaine it
        # does not belong to, and the two would disagree in every gabarit.
        errors.append("L'action choisie n'appartient pas au domaine choisi.")

    # mandate_type is absent on legacy dossiers read directly (no form pass);
    # only validate it when the caller actually supplied a value.
    if "mandate_type" in data and data.get("mandate_type", "") not in VALID_MANDATE_TYPES:
        errors.append("Type de mandat invalide.")

    # forum_type is presence-gated (legacy dossiers predate it → default
    # "judiciaire" on read). "administratif"/"federal" require a forum slug of
    # the MATCHING category — the form's two pickers cannot cross categories,
    # but a hand-crafted POST can. "judiciaire"/"prejudiciaire" need no forum.
    if "forum_type" in data:
        forum_type = data.get("forum_type", "")
        forum = reference.get_forum(data.get("forum", ""))
        if forum_type not in VALID_FORUM_TYPES:
            errors.append("Type de forum invalide.")
        elif forum_type in _FORUM_TYPE_CATEGORY and (
            not forum or forum["category"] != _FORUM_TYPE_CATEGORY[forum_type]
        ):
            errors.append("Veuillez sélectionner le tribunal ou la cour.")

    if data.get("status", "") not in VALID_STATUSES:
        errors.append("Statut invalide.")

    if data.get("prescription_type", "") not in VALID_PRESCRIPTION_TYPES:
        errors.append("Type de prescription invalide.")

    fee_type = data.get("fee_type", "")
    if fee_type and fee_type not in VALID_FEE_TYPES:
        errors.append("Type d'honoraires invalide.")

    # contingency_percent is stored in basis points (2500 = 25,00 %).
    percent = data.get("contingency_percent")
    if percent is not None and not 0 <= percent <= 10000:
        errors.append("Le pourcentage de contingence doit être entre 0 et 100 %.")

    return errors


# Les quatre événements du C.c.Q. que le modèle sait porter. Vocabulaire
# FERMÉ — un cinquième type est une décision de contenu juridique, pas un
# ajout de code.
VALID_PRESCRIPTION_EVENT_TYPES = (
    "interruption_depot",           # art. 2892/2896 — demande en justice
    "interruption_reconnaissance",  # art. 2898 — reconnaissance du droit
    "suspension",                   # art. 2904 s. — impossibilité d'agir
    "renonciation",                 # art. 2883 s. — renonciation à la
                                    # prescription acquise/écoulée
)
PRESCRIPTION_EVENT_LABELS = {
    "interruption_depot": "Interruption — demande en justice (art. 2892)",
    "interruption_reconnaissance": "Interruption — reconnaissance (art. 2898)",
    "suspension": "Suspension (art. 2904)",
    "renonciation": "Renonciation (art. 2883)",
}


def _coerce_event_date(raw) -> Optional[datetime]:
    """Coerce an event date to date-only midnight UTC (house convention)."""
    if isinstance(raw, datetime):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
    return None


def _normalize_prescription_events(doc: dict) -> list[str]:
    """Coerce + clean ``prescription_events`` in place; return errors.

    Never INJECTS: callers always operate on a merged doc that already
    carries the key (from ``_default_doc`` or the stored document), so a
    partial update that does not touch the events cannot erase them (the
    ``_normalize``-injection lesson from models/partie). Blank rows from
    the form repeater are dropped silently; malformed ones error loudly.
    """
    raw = doc.get("prescription_events")
    if raw is None:
        doc["prescription_events"] = []
        return []
    if not isinstance(raw, list):
        doc["prescription_events"] = []
        return ["Événements de prescription invalides."]
    errors: list[str] = []
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        etype = str(entry.get("type") or "").strip()
        if not etype and not entry.get("date"):
            continue  # blank repeater row
        if etype not in VALID_PRESCRIPTION_EVENT_TYPES:
            errors.append("Type d'événement de prescription invalide.")
            continue
        when = _coerce_event_date(entry.get("date"))
        if when is None:
            errors.append(
                "Chaque événement de prescription requiert une date valide "
                "(AAAA-MM-JJ)."
            )
            continue
        end = _coerce_event_date(entry.get("end_date"))
        if etype == "suspension":
            if end is None:
                errors.append("Une suspension requiert une date de fin.")
                continue
            if end < when:
                errors.append(
                    "La fin d'une suspension ne peut précéder son début."
                )
                continue
        else:
            end = None
        cleaned.append({
            "id": str(entry.get("id") or "").strip() or str(uuid.uuid4()),
            "type": etype,
            "date": when,
            "end_date": end,
            "reference": sanitize(
                str(entry.get("reference") or ""), max_length=300
            ),
            "document_id": str(entry.get("document_id") or "").strip(),
        })
    cleaned.sort(key=lambda e: e["date"])
    doc["prescription_events"] = cleaned
    return errors


# Modes de signification — art. 110 s. C.p.c. Vocabulaire fermé.
VALID_SIGNIFICATION_MODES = (
    "personnelle",      # à la personne même
    "domicile",         # à domicile / personne raisonnable
    "huissier",         # par huissier (le mode usuel)
    "notification",     # notification (courriel, poste…)
    "avocat",           # à l'avocat de la partie
    "publication",      # par avis public (art. 136 C.p.c.)
)
SIGNIFICATION_MODE_LABELS = {
    "personnelle": "Personnelle",
    "domicile": "À domicile",
    "huissier": "Par huissier",
    "notification": "Notification",
    "avocat": "À l'avocat",
    "publication": "Par avis public",
}


def _normalize_significations(doc: dict) -> list[str]:
    """Coerce + clean ``significations`` in place; return errors.

    Same discipline as ``_normalize_prescription_events``: never injects a
    key, drops blank repeater rows silently, errors loudly on junk.
    ``partie_id`` must reference a party ON the dossier (arts. 145/147
    delays run per party — a signification on a stranger is meaningless);
    ``superseded_by`` must reference a SIBLING entry (the second-PV case).
    """
    raw = doc.get("significations")
    if raw is None:
        doc["significations"] = []
        return []
    if not isinstance(raw, list):
        doc["significations"] = []
        return ["Significations invalides."]
    party_ids = {
        str(p.get("id"))
        for p in list(doc.get("clients") or []) + list(
            doc.get("opposing_parties") or []
        )
        if isinstance(p, dict) and p.get("id")
    }
    errors: list[str] = []
    cleaned: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        partie_id = str(entry.get("partie_id") or "").strip()
        if not partie_id and not entry.get("date"):
            continue  # blank repeater row
        mode = str(entry.get("mode") or "").strip()
        if mode not in VALID_SIGNIFICATION_MODES:
            errors.append("Mode de signification invalide.")
            continue
        when = _coerce_event_date(entry.get("date"))
        if when is None:
            errors.append(
                "Chaque signification requiert une date valide (AAAA-MM-JJ)."
            )
            continue
        if partie_id not in party_ids:
            errors.append(
                "La partie signifiée doit être une partie au dossier."
            )
            continue
        cleaned.append({
            "id": str(entry.get("id") or "").strip() or str(uuid.uuid4()),
            "partie_id": partie_id,
            "date": when,
            "mode": mode,
            "huissier_id": str(entry.get("huissier_id") or "").strip(),
            "pv_document_id": str(entry.get("pv_document_id") or "").strip(),
            "superseded_by": str(entry.get("superseded_by") or "").strip(),
            "confirmee": bool(entry.get("confirmee")),
        })
    ids = {e["id"] for e in cleaned}
    for e in cleaned:
        if e["superseded_by"] and e["superseded_by"] not in ids:
            errors.append(
                "Une signification remplacée doit référencer une autre "
                "signification du dossier."
            )
    if errors:
        return errors
    cleaned.sort(key=lambda e: e["date"])
    doc["significations"] = cleaned
    return []


def derive_prescription(doc: dict) -> dict:
    """The ONE derivation seam: {status, date_effective} from the raw fields.

    Consumed by ``list_prescription_alerts`` (dashboard + MCP get_agenda),
    ``routes/dossiers._attach_prescription_warnings`` (list pastille + card
    colour) and the MCP ``get_dossier`` — the three-surface parity rule: a
    dossier silenced on one surface must be silenced on all.

    The RAW ``prescription_date`` is never touched (provenance; the pinned
    test) — this projects an EFFECTIVE picture beside it:

    - ``interruption_depot`` (art. 2892) → status « interrompue »,
      effective None: per art. 2896 the interruption lasts until judgment,
      so computing a date would be inventing one. The legacy
      ``prise_action_date`` folds in as an implicit depot event at READ
      time — no storage migration, silencing semantics unchanged.
    - ``interruption_reconnaissance`` / ``renonciation`` → a NEW period of
      the same confirmed duration runs from the event date
      (``compute_date_pour_agir`` — the house arithmetic, art. 2879 forward
      report included). No confirmed period → effective None, « a_verifier ».
    - ``suspension`` → shifts the current effective deadline by the
      suspension's length, then forward to the next jour ouvrable on the
      CIVIL calendar (art. 2879 C.c.Q. — never art. 82 C.p.c.'s 26 Dec /
      2 Jan, which govern procedure, not prescription).
    - Statuses: courante | interrompue | echue | imprescriptible |
      a_verifier. « interrompue » means DECLARED by the lawyer — whether
      the demande was served within 60 days (art. 2892 al. 1) is not
      recorded and not asserted.
    """
    p_type = doc.get("prescription_type", "")
    if p_type == "imprescriptible":
        return {"status": "imprescriptible", "date_effective": None}

    events = [
        e for e in (doc.get("prescription_events") or [])
        if isinstance(e, dict) and e.get("date")
    ]
    if doc.get("prise_action_date"):
        events = events + [{
            "type": "interruption_depot",
            "date": doc["prise_action_date"],
            "end_date": None,
        }]
    events.sort(key=lambda e: _coerce_event_date(e.get("date")) or datetime.min.replace(tzinfo=timezone.utc))

    effective = doc.get("prescription_date")
    period = prescription_period(p_type)
    for ev in events:
        etype = ev.get("type")
        when = _coerce_event_date(ev.get("date"))
        if etype == "interruption_depot":
            # Interruption continues until judgment (art. 2896) — nothing
            # after it changes the picture until the instance ends.
            return {"status": "interrompue", "date_effective": None}
        if etype in ("interruption_reconnaissance", "renonciation"):
            effective = (
                compute_date_pour_agir(when, p_type) if period else None
            )
        elif etype == "suspension" and effective is not None:
            end = _coerce_event_date(ev.get("end_date"))
            if end and when:
                shifted = (effective + (end - when)).date()
                adjusted = next_juridical_day(
                    shifted, regime=deadlines.CIVIL
                )
                effective = datetime(
                    adjusted.year, adjusted.month, adjusted.day,
                    tzinfo=timezone.utc,
                )

    if effective is None:
        return {"status": "a_verifier", "date_effective": None}
    # Montréal day + prorogation (2026-08-02) — prescription dates are
    # already prorogued at computation, so this only fixes the clock: the
    # old UTC date flipped « echue » up to five hours before the lawyer's
    # own midnight. CIVIL calendar, the one the date was computed on: read
    # back on the procedural one, a prescription acquired at the end of
    # Friday 26 December 2025 would read « courante » through Monday the
    # 29th (art. 82 C.p.c. prorogation that prescription does not get).
    status = (
        "echue"
        if deadlines.is_past_due(effective, regime=deadlines.CIVIL)
        else "courante"
    )
    return {"status": status, "date_effective": effective}


def _apply_prescription_deadline(doc: dict) -> None:
    """Recompute ``prescription_date`` (the "date pour agir") in place.

    The limitation deadline is derived from the recourse fields:
    ``droit_action_date`` + the ``prescription_type`` period. An imprescriptible
    recourse clears it. When the type/start date don't drive a computation
    (unset, or "autre"), any existing/legacy ``prescription_date`` is left
    untouched — so older dossiers carrying a manually-set date are never wiped.

    Since July 2026 the date is read from ``compute_echeances``' principale
    (the type-aware orchestration layer), whose PE/D/DR path calls
    ``compute_date_pour_agir`` verbatim — identical arithmetic, pinned by
    test. The direct-call fallback covers the actions with no dated
    principale (a PA action returns only a defensive échéance) so a manually
    confirmed period still computes exactly as before. ``date_avis`` is never
    touched here — it is manual, confirmed by the lawyer on the form.
    """
    p_type = doc.get("prescription_type", "")
    if p_type == "imprescriptible":
        doc["prescription_date"] = None
        return
    if not (doc.get("droit_action_date") and prescription_period(p_type)):
        return
    echeances = compute_echeances(
        doc.get("action", ""), doc.get("droit_action_date"), p_type
    )
    principale = next(
        (e for e in echeances if e.role == "principale" and e.date), None
    )
    doc["prescription_date"] = (
        principale.date
        if principale
        else compute_date_pour_agir(doc.get("droit_action_date"), p_type)
    )


def _suggest_next_file_number() -> str:
    """Suggest the next sequential file number for the current year.

    Reads only the lexicographically highest file number of the year
    (``order_by DESC`` + ``limit(1)`` over the same year-prefix range filter)
    instead of materializing every dossier of the year. Generated numbers are
    zero-padded to three digits, so lexicographic order matches numeric order
    for the sequences this function emits; a non-padded manually-assigned
    number can skew the suggestion, but uniqueness is still enforced at
    creation time and the suggestion remains user-editable.
    """
    year = datetime.now(timezone.utc).year
    try:
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("file_number", ">=", f"{year}-"))
            .where(filter=FieldFilter("file_number", "<=", f"{year}-\uf8ff"))
            .order_by("file_number", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        docs = list(query.stream())
        if not docs:
            return f"{year}-001"

        fn = (docs[0].to_dict() or {}).get("file_number", "")
        max_seq = 0
        parts = fn.split("-", 1)
        if len(parts) == 2:
            try:
                max_seq = int(parts[1])
            except ValueError:
                max_seq = 0
        return f"{year}-{max_seq + 1:03d}"
    except Exception:
        return f"{year}-001"


# ── CRUD ──────────────────────────────────────────────────────────────────


def _today_midnight_utc() -> datetime:
    """Today on the MONTRÉAL calendar, stored at midnight UTC — the house
    date-only convention, for the auto-stamped ``opened_date`` /
    ``closed_date`` (lot 4a).

    Both fields are DATE-ONLY values: rendered through ``strftime`` on the
    Aperçu card and the form, emitted by ``mcp.tools.date_str``, and fed to
    ``retention_date``. They used to be stamped with
    ``datetime.now(timezone.utc)`` — a timestamp — so a dossier closed after
    20:00 (19:00 in winter) read TOMORROW's date, and its retention date a
    day late, with no error anywhere (the 2026-08-02 evening-band class,
    fixed for trust in lot 0b by the same helper). A value the caller
    supplies is kept as supplied.
    """
    t = deadlines.today_mtl()
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc)


def create_dossier(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    # A contact on BOTH sides of a new dossier is refused outright: there is
    # no legacy record to spare here (see _party_shape_errors).
    errors = _party_shape_errors(None, merged)
    if errors:
        return None, errors
    errors = (
        _normalize_prescription_events(merged)
        + _normalize_significations(merged)
        + _validate(merged)
    )
    if errors:
        return None, errors

    # Check file_number uniqueness
    try:
        existing = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("file_number", "==", merged["file_number"]))
            .limit(1)
            .get()
        )
        if list(existing):
            return None, ["Ce numéro de dossier existe déjà."]
    except Exception as exc:
        logger.warning("create_dossier: duplicate-check query failed: %s", exc)

    now = datetime.now(timezone.utc)
    dossier_id = str(uuid.uuid4())
    vjournal_uid = str(uuid.uuid4())

    # Derived role + flat ID mirrors, from the per-party arrays
    _derive_role(merged)
    _rebuild_party_mirrors(merged)

    merged.update(
        {
            "id": dossier_id,
            "opened_date": merged.get("opened_date") or _today_midnight_utc(),
            "vjournal_uid": vjournal_uid,
            "dav_href": f"/dav/journals/{dossier_id}.ics",
        }
    )
    provenance.stamp_create(merged, now)

    # Closure date mirrors update_dossier: auto-stamp when a dossier is created
    # already closed/archived (unless the form supplied one); empty otherwise.
    # Date-only — today in Montréal at midnight UTC, never `now`.
    if merged.get("status") in ("fermé", "archivé"):
        merged["closed_date"] = merged.get("closed_date") or _today_midnight_utc()
    else:
        merged["closed_date"] = None

    # Derive the prescription deadline ("date pour agir") from the recourse fields.
    _apply_prescription_deadline(merged)

    try:
        db.collection(COLLECTION).document(dossier_id).set(merged)
    except Exception:
        log_unexpected("dossier write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, dossier_id)

    return merged, []


def _normalize_party_entry(entry: dict) -> dict:
    """Give a party entry the full July-2026 shape in place.

    Legacy entries are bare ``{id, name}`` snapshots; the rework added
    ``roles`` (⊆ PARTY_ROLES, possibly several per party), ``avocat_id``
    (link into the parties collection) and ``avocat_name`` (snapshot).
    Normalizing on read keeps every consumer — templates, MCP emission,
    the avocat_ids mirror rebuild — free of per-key existence checks.
    """
    entry.setdefault("roles", [])
    entry.setdefault("avocat_id", "")
    entry.setdefault("avocat_name", "")
    return entry


def _migrate_parties(doc: dict) -> dict:
    """Migrate legacy single-client / text opposing fields to arrays."""
    # Legacy single client_id → clients array
    if doc.get("client_id") and not doc.get("clients"):
        doc["clients"] = [{"id": doc["client_id"], "name": doc.get("client_name", "")}]
        doc["client_ids"] = [doc["client_id"]]
    # Legacy opposing_party text → opposing_parties array (skip, no ID)
    if not doc.get("clients"):
        doc.setdefault("clients", [])
    if not doc.get("client_ids"):
        doc["client_ids"] = [c["id"] for c in doc.get("clients", [])]
    if not doc.get("opposing_parties"):
        doc.setdefault("opposing_parties", [])
    if not doc.get("opposing_party_ids"):
        doc["opposing_party_ids"] = [p["id"] for p in doc.get("opposing_parties", [])]

    for entry in doc.get("clients", []):
        _normalize_party_entry(entry)
    for entry in doc.get("opposing_parties", []):
        _normalize_party_entry(entry)
    # One-time seeding: before the July-2026 rework the litigation role was a
    # single dossier-level field describing the CLIENTS' side. If no client
    # carries a per-party role yet, the stored role becomes the first
    # client's — so every legacy dossier keeps its meaning with no manual
    # re-entry, and _derive_role then reproduces the same value on save.
    clients = doc.get("clients", [])
    if (
        clients
        and not any(c.get("roles") for c in clients)
        and doc.get("role") in PARTY_ROLES
    ):
        clients[0]["roles"] = [doc["role"]]

    _migrate_domaine(doc)
    _migrate_mandate_type(doc)
    _migrate_forum_type(doc)
    _migrate_trust(doc)
    return doc


def _migrate_forum_type(doc: dict) -> dict:
    """Split the retired "autre" forum_type into administratif/federal in place.

    The binary judiciaire/"autre" toggle became a four-way vocabulary in July
    2026; the stored forum slug's category says which branch an "autre" doc
    belongs to. Same contract as :func:`_migrate_mandate_type`: called from
    :func:`_migrate_parties`, so every read path sees a current value and the
    write-back happens on the next ``set()``. A dangling slug (removed from
    the reference table) falls back to "judiciaire" with the forum cleared —
    the tribunal name it wrote at save time survives as plain text.
    """
    if doc.get("forum_type") != "autre":
        return doc
    forum = reference.get_forum(doc.get("forum", ""))
    if forum and forum["category"] == reference.FEDERAL:
        doc["forum_type"] = "federal"
    elif forum:
        doc["forum_type"] = "administratif"
    else:
        doc["forum_type"] = "judiciaire"
        doc["forum"] = ""
    return doc


def _migrate_mandate_type(doc: dict) -> dict:
    """Normalize a retired type-de-mandat key to the current vocabulary in place.

    Same contract as :func:`_migrate_matter_type`: called from
    :func:`_migrate_parties`, so every read path (detail, lists, MCP) sees a
    current key and editing a dossier that still carries a retired one no
    longer trips ``_validate``. The write-back happens on the next ``set()``.
    """
    mt = doc.get("mandate_type")
    if mt in _MANDATE_TYPE_MIGRATION:
        doc["mandate_type"] = _MANDATE_TYPE_MIGRATION[mt]
    return doc


def _migrate_trust(doc: dict) -> dict:
    """Default the trust-accounting fields on legacy dossiers in place (Phase K).

    Same contract as the other read-time migrations (called from
    :func:`_migrate_parties`): a dossier that predates Phase K carries none of
    the three fields, and every caller — detail, lists, MCP, and
    ``models/trust.py`` itself — must see ``0`` / ``{}`` rather than ``None``.
    ``setdefault`` never clobbers a real stored value; the write-back happens on
    the next ``set()``. No backfill — a dossier with no trust entries has a zero
    balance by construction.
    """
    doc.setdefault("trust_balance", 0)
    doc.setdefault("trust_balance_by_client", {})
    doc.setdefault("trust_cleared_by_client", {})
    return doc


def _migrate_domaine(doc: dict) -> dict:
    """Fold legacy ``matter_type`` / ``objet`` into ``domaine`` / ``action_precision``.

    Called from :func:`_migrate_parties`, so every dossier read path (detail,
    lists, MCP) sees the taxonomy fields, and editing a legacy dossier does not
    trip ``_validate``. The write-back happens on the next ``set()``
    (purge-on-save, like the party migrations).

    ORDERING IS LOAD-BEARING: ``get_dossier`` runs this *inside*
    ``_strip_removed_fields(_migrate_parties(...))``, so the legacy keys are
    still present when this reads them, and are popped straight after. Reverse
    the nesting and the legacy data is destroyed unread.

    Both migrations are ``setdefault``-shaped — they never overwrite a value
    the taxonomy era already wrote.
    """
    if not doc.get("domaine"):
        matter_type = doc.get("matter_type")
        if matter_type in _MATTER_TYPE_TO_DOMAINE:
            doc["domaine"] = _MATTER_TYPE_TO_DOMAINE[matter_type]

    # The old « Objet » was free text and cannot be mapped onto an action code,
    # so it is preserved verbatim as the précision rather than discarded — the
    # same field the taxonomy's « Autre (préciser) » rows need.
    if not doc.get("action_precision") and doc.get("objet"):
        doc["action_precision"] = doc["objet"]
    return doc


# Fields removed and popped on read so the next set() purges them from the
# stored document (the purge-on-save pattern partie._migrate_mandataires uses).
#
#   notes / internal_notes — removed July 2026, superseded by the standalone
#     `notes` collection.
#   matter_type / objet — superseded July 2026 by the domaine/action taxonomy.
#     _migrate_domaine reads them first (see the ordering note above).
_REMOVED_FIELDS = ("notes", "internal_notes", "matter_type", "objet")


def _strip_removed_fields(doc: dict) -> dict:
    """Drop removed legacy fields in place so the next save purges them."""
    for key in _REMOVED_FIELDS:
        doc.pop(key, None)
    return doc


def get_dossier(dossier_id: str) -> Optional[dict]:
    """Fetch a single dossier by ID."""
    try:
        doc = db.collection(COLLECTION).document(dossier_id).get()
        if doc.exists:
            return _strip_removed_fields(_migrate_parties(doc.to_dict()))
    except Exception as exc:
        logger.warning("get_dossier failed for %s: %s", sanitize_log_value(dossier_id), exc)
    return None


def get_dossier_strict(dossier_id: str) -> Optional[dict]:
    """The dossier, ``None`` when it does not exist — and RAISES on a read
    error, unlike :func:`get_dossier`, which swallows it into ``None``.

    For a caller whose ``None`` means something (fixups of lot 2A: a
    template rename checked against its source dossier must tell « that
    dossier was deleted » from « the store could not answer »). Same shape
    as ``get_dossier``: migrations applied, removed fields purged."""
    doc = db.collection(COLLECTION).document(dossier_id).get()
    if not doc.exists:
        return None
    return _strip_removed_fields(_migrate_parties(doc.to_dict() or {}))


def get_dossier_by_file_number(file_number: str) -> Optional[dict]:
    """The dossier bearing *file_number*, or None when none does.

    RAISES on query failure, deliberately unlike ``get_dossier``, which
    swallows to None. A caller whose next move is « it does not exist, so
    create it » must fail CLOSED: a swallowed error reads as « absent » and
    mints a duplicate — and the MCP connector, which can never delete
    anything, has no way back from one.

    Exists because the connector's file_number lookup used to filter
    ``list_dossiers_page(limit=200)`` in Python — the 200 most recently
    OPENED dossiers. The dossiers of a historical import are by construction
    the OLDEST opened_dates in the base, so past 200 dossiers the question
    « does 2014-007 already exist? » answered « no » for a dossier that does.

    The match is EXACT after stripping (a Firestore equality is), where the
    old Python filter folded case. File numbers here are « YYYY-NNN », so
    the fold never mattered; the exactness is stated in the tool description
    so a caller does not read a miss as proof of absence.

    Returns the SAME shape as ``get_dossier`` — migrations applied, removed
    fields purged — so a caller never has two shapes to handle.
    """
    wanted = (file_number or "").strip()
    if not wanted:
        return None
    docs = list(
        db.collection(COLLECTION)
        .where(filter=FieldFilter("file_number", "==", wanted))
        .limit(1)
        .stream()
    )
    if not docs:
        return None
    return _strip_removed_fields(_migrate_parties(docs[0].to_dict() or {}))


def get_dossiers_bulk(dossier_ids: list[str]) -> dict[str, dict]:
    """Fetch many dossiers in a single round-trip. Returns {id: doc} for ids that exist."""
    unique_ids = [d for d in dict.fromkeys(dossier_ids) if d]
    if not unique_ids:
        return {}
    try:
        refs = [db.collection(COLLECTION).document(did) for did in unique_ids]
        snapshots = db.get_all(refs)
        result: dict[str, dict] = {}
        for snap in snapshots:
            if snap.exists:
                result[snap.id] = _migrate_parties(snap.to_dict())
        return result
    except Exception as exc:
        logger.warning("get_dossiers_bulk failed: %s", exc)
        return {}


def list_dossiers(
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "opened_date",
) -> list[dict]:
    """Return dossiers, optionally filtered by status or search."""
    try:
        query = db.collection(COLLECTION)

        if status_filter and status_filter in VALID_STATUSES:
            query = query.where(filter=FieldFilter("status", "==", status_filter))

        results = [_migrate_parties(doc.to_dict()) for doc in query.stream()]

        # Client-side search (Firestore doesn't support full-text)
        if search:
            term = search.lower()
            filtered = []
            for d in results:
                client_names = " ".join(c.get("name", "") for c in d.get("clients", []))
                searchable = " ".join(
                    [
                        d.get("file_number", ""),
                        d.get("title", ""),
                        client_names,
                        d.get("court_file_number", ""),
                    ]
                ).lower()
                if term in searchable:
                    filtered.append(d)
            results = filtered

        # Sort
        if sort_by == "file_number":
            results.sort(key=lambda d: d.get("file_number", ""), reverse=True)
        else:
            # Default: opened_date, newest first — same-day ties by creation.
            results.sort(key=_newest_opened_first_key, reverse=True)

        return results
    except Exception:
        return []


def list_dossiers_by_status_strict(status: str) -> list[dict]:
    """Every dossier of *status*, newest opened first — the QUERY strict,
    each DOCUMENT tolerant (fixes of lot 4, for the DavX5 discovery).

    Written for the root Depth:1 PROPFIND (``dav/__init__.py``), which
    advertises one collection per active dossier. It used to read through
    :func:`list_dossiers`, which fails OPEN twice over:

    * a failed QUERY answered ``[]`` — and discovery then advertised ZERO
      dossier collections in a well-formed 207, a false statement about the
      firm (DavX5 then re-probes every collection it holds, one PROPFIND
      each, and deletes those that answer 403/404/410 — see
      :func:`get_dossier_for_dav`, whose strictness is what actually keeps
      them through an outage). Here the query failure PROPAGATES: the
      caller answers 503 + ``Retry-After``;
    * one DOCUMENT the party migration cannot read (a legacy client entry
      with no ``id`` — ``_migrate_parties`` raises a ``KeyError``) raised
      inside the same ``try`` and emptied the WHOLE status. Here it is
      SKIPPED, logged through the typed helper (ERROR, the document id
      only — never a title or a name), and every other dossier is listed.
      Such a dossier's own collection answers 404 (:func:`get_dossier_for_dav`
      applies the same per-document rule, :func:`_dav_document`), so
      advertising it would only point the phone at a dead URL.

    A document whose stored ``id`` disagrees with its document id is skipped
    the same way (the collection URL is the document id); an absent ``id``
    is taken from the document id. A stored ``opened_date`` that is not a
    timezone-aware datetime sorts as the oldest instead of raising in the
    sort (a ``TypeError`` there
    would have turned one bad date into a 503 of the whole discovery). An
    unknown *status* raises ``ValueError`` before any read:
    :func:`list_dossiers` IGNORES a filter outside :data:`VALID_STATUSES`
    and returns every dossier, which would advertise the whole firm.

    Same order as :func:`list_dossiers`'s default (newest opened first,
    same-day ties by creation), so the listing it replaced is reproduced
    byte for byte.
    """
    if status not in VALID_STATUSES:
        raise ValueError("list_dossiers_by_status_strict needs a valid status")
    query = db.collection(COLLECTION).where(
        filter=FieldFilter("status", "==", status))
    keyed: list[tuple[tuple[datetime, float], dict]] = []
    for snap in query.stream():
        try:
            doc = _dav_document(snap)
            opened, tie = _newest_opened_first_key(doc)
            if not isinstance(opened, datetime) or opened.tzinfo is None:
                opened = datetime.min.replace(tzinfo=timezone.utc)
            keyed.append(((opened, tie), doc))
        except Exception:
            log_unexpected("list_dossiers_by_status_strict: document skipped",
                           dossier_id=snap.id)
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [doc for _key, doc in keyed]


def _dav_document(snap) -> dict:
    """*snap*'s dossier as the DAV layer serves it — RAISES when it cannot.

    The per-document half of the discovery rule, shared by
    :func:`list_dossiers_by_status_strict` (the root listing) and
    :func:`get_dossier_for_dav` (a dossier collection's own requests), so
    the root and the collection can never disagree about which documents
    they serve: a stored ``id`` that is not the document id raises (every
    href is built from the document id), an absent one is taken from it,
    and the party migration runs — a legacy entry it cannot read raises.
    """
    doc = snap.to_dict() or {}
    stored_id = doc.get("id")
    if stored_id is None:
        doc["id"] = snap.id
    elif stored_id != snap.id:
        raise ValueError("stored id differs from the document id")
    return _migrate_parties(doc)


def get_dossier_for_dav(dossier_id: str) -> Optional[dict]:
    """The dossier a DAV collection URL names — STRICT on the read, TOLERANT
    on the document (review of the fixes of lot 4).

    For ``dav.dossier_collections._resolve_scope`` (PROPFIND, REPORT and
    PUT of ``/dav/dossier-<id>/``), which read through the fail-open
    :func:`get_dossier`: a Firestore blip answered **404** — the one answer
    DavX5 acts on destructively. Its collection refresh (davx5-ose
    ``HomeSetRefresher``, then ``CollectionsWithoutHomeSetRefresher``) marks
    every collection the home-set listing did not return « without
    home-set » — ALL of them when the root PROPFIND itself failed, a 503
    included, since that catch rethrows nothing but 403/404/410 — then
    PROPFINDs each one at Depth:0 and DELETES it locally on a 403/404/410,
    while any other HTTP error aborts the refresh (retried later) and keeps
    it. So during an outage the root's 503 protects nothing by itself: the
    collection's own answer decides, and it must be a 503 too. Here the
    READ propagates (the caller answers 503 + ``Retry-After``).

    ``None``: the document does not exist — or exists but DAV cannot serve
    it (:func:`_dav_document` raised: a stored ``id`` not its own, a legacy
    party entry the migration cannot read), logged by id. The collection
    then answers 404 exactly as the root leaves it out of discovery: a
    legacy entry behaves as it did before (the fail-open read swallowed the
    same error), and a stored ``id`` not its own — once served under an
    href naming that stored id, itself a dead URL — now 404s too. A 503
    there would abort, refresh after refresh, the re-probe of every
    collection queued behind it. Same shape as :func:`get_dossier`
    (migrations applied, removed fields purged).
    """
    snap = db.collection(COLLECTION).document(dossier_id).get()
    if not snap.exists:
        return None
    try:
        return _strip_removed_fields(_dav_document(snap))
    except Exception:
        log_unexpected("get_dossier_for_dav: document unusable",
                       dossier_id=snap.id)
        return None


def _newest_opened_first_key(dossier: dict) -> tuple[datetime, float]:
    """Sort key for « newest opened first » in the PYTHON-sorted lists
    (:func:`list_dossiers`, :func:`list_dossiers_for_partie`).

    ``opened_date`` is DATE-ONLY since lot 4a (midnight UTC), so two
    dossiers opened the same day tie on it — and a stable sort then kept
    the stream order, the document-id order of random UUIDs: the partie's
    fiche and the dossier search listed a day's dossiers shuffled, where
    the old timestamp stamps had kept them chronological. ``created_at``
    (a true timestamp on every document, Rule 7) breaks the tie. The
    cursor-paginated list orders in Firestore on ``(opened_date, id)`` and
    cannot take this key without a new index (documented).
    """
    opened = (dossier.get("opened_date")
              or datetime.min.replace(tzinfo=timezone.utc))
    created = dossier.get("created_at")
    # As a number, so a missing or malformed created_at sorts last instead
    # of raising a TypeError the fail-open caller would turn into « [] ».
    tie = created.timestamp() if hasattr(created, "timestamp") else 0.0
    return opened, tie


def _page_query(status_filter: Optional[str] = None) -> "firestore.Query":
    """The filtered, (opened_date DESC, id DESC)-ordered dossier query.

    ONE builder shared by :func:`list_dossiers_page` and
    :func:`count_dossiers_page`, so the page read and its total can never
    ride different indexes — the property that makes « zero new indexes »
    a fact about the code rather than a comment.
    """
    query = db.collection(COLLECTION)
    if status_filter and status_filter in VALID_STATUSES:
        query = query.where(filter=FieldFilter("status", "==", status_filter))
    return query.order_by(
        "opened_date", direction=firestore.Query.DESCENDING
    ).order_by("id", direction=firestore.Query.DESCENDING)


def count_dossiers_page(status_filter: Optional[str] = None) -> Optional[int]:
    """Rows in the filtered set, or None when the count could not be read.

    Runs on the SAME query object as the page read — order_by INCLUDED — so
    the SAME composite index serves both. An aggregation forwards its nested
    query verbatim, and a COUNT adds no aggregated field to trail the index.
    Dropping the order_by makes the backend apply its own implicit ordering,
    a THIRD ordering that FAILS (measured 2026-09-07 against production).

    That shared ordering also keeps the count HONEST: an order_by excludes
    documents missing the key, from the count and the page read alike, so
    the two can never disagree (cross-checked against a bare collection
    COUNT: gap of 0).

    Returns None, NEVER 0, on failure — « Page 7 / 0 » is a confident lie.
    """
    try:
        values = _aggregation_values(_page_query(status_filter).count(alias="n").get())
        n = values.get("n")
        return int(n) if n is not None else None
    except Exception as exc:
        logger.warning("count_dossiers_page: aggregation failed: %s", exc)
        return None


def list_dossiers_page(
    status_filter: Optional[str] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of dossiers via Firestore-native cursor pagination.

    Replicates :func:`list_dossiers`'s default ordering (``opened_date``
    descending, newest first) with ``id`` as a deterministic tiebreaker,
    reading ~``limit`` documents per page instead of streaming the whole
    collection. ``status_filter`` is applied server-side when set; no filter
    means the « tous » tab.

    Required composite indexes (see ``firestore.indexes.json``):
    - (status ASC, opened_date DESC, id DESC) — status tabs
    - (opened_date DESC, id DESC) — « tous »

    Returns ``(rows, next_cursor)`` where ``next_cursor`` is an opaque token
    for the next page, or None on the last page. A malformed cursor degrades
    to the first page. Returns ``([], None)`` on query failure.
    """
    # Before the try: a caller naming a position TWICE is a programming error,
    # and swallowing it into ([], None) would render an empty list with no
    # explanation anywhere.
    if offset and cursor:
        raise ValueError("cursor et offset s'excluent — chacun nomme une position")
    try:
        query = _page_query(status_filter)
        if offset:
            # An ABSOLUTE page read, for a « Fin » / « ±N » leap. Same
            # ordering, so the same index — Firestore emits ONE query
            # (order_by -> offset -> limit). It bills the skipped documents,
            # which is why `page` is clamped before it ever reaches here.
            query = query.offset(offset)

        # decode_cursor yields the values in encode order: [opened_date, id].
        # start_after takes a {field_path: value} dict matched to the
        # order_by fields (google-cloud-firestore 2.27 BaseQuery API).
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            query = query.start_after({"opened_date": values[0], "id": values[1]})

        # Fetch one extra row to learn whether a next page exists.
        docs = [
            _migrate_parties(doc.to_dict())
            for doc in query.limit(limit + 1).stream()
        ]
        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("opened_date"), last.get("id")])
        return docs, next_cursor
    except Exception as exc:
        # PII-free: log only the exception type, never dossier content.
        logger.warning("list_dossiers_page: query failed: %s", type(exc).__name__)
        return [], None


def update_dossier(
    dossier_id: str, data: dict, *, expected_etag: Optional[str] = None
) -> tuple[Optional[dict], list[str]]:
    """Update an existing dossier. Returns (updated_doc, errors).

    ``expected_etag`` (keyword-only): when given, the write commits only if
    the stored etag is still that one, checked in a transaction
    (``models.concurrency``); a stale one returns ``[STALE_ETAG_ERROR]``
    and writes nothing. ``None`` is the unchanged path — including its
    last-moment re-read of the trust fields, which the guarded path does
    not need (see the comment at that re-read).

    Party links (lot 4a). Whatever the caller — the web form, which posts
    the whole ``clients`` / ``opposing_parties`` arrays, or the connector —
    the rules below are computed from the ARRAYS, never from the
    ``client_ids`` mirror (a caller never sends the mirror; it is rebuilt
    only after validation, so comparing it would compare the old mirror
    with itself and never fire):

    * a contact may not become a client AND an opposing party
      (:func:`_party_shape_errors` — grow-only, so a legacy duplicate never
      locks the dossier out of editing);
    * a party a signification names cannot leave the dossier;
    * a client who has EVER had a trust entry on the dossier cannot leave
      its clients (:func:`_client_trust_errors`, fail closed);
    * every link that leaves the dossier is journaled in ``audit_events``
      (``dossier_party``) after the commit (:func:`_journal_party_detaches`).
    """
    doc, errors, _journaled = _update_dossier(dossier_id, data, expected_etag)
    return doc, errors


def _update_dossier(
    dossier_id: str, data: dict, guard: Optional[str]
) -> tuple[Optional[dict], list[str], int]:
    """:func:`update_dossier`'s body, plus the number of party detaches
    journaled — which the link helpers below report. *guard* is the public
    ``expected_etag``, same contract; every other caller goes through the
    public wrapper (the one ``tests/test_concurrency_models.py`` proves).
    """
    expected_etag = guard
    existing = get_dossier(dossier_id)
    if not existing:
        return None, ["Dossier introuvable."], 0
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], 0

    merged = {**existing, **_sanitize_data(data)}
    # The link rules first: each names what it refuses, where the generic
    # signification check below would only say « la partie signifiée doit
    # être une partie au dossier ».
    errors = _party_shape_errors(existing, merged)
    if errors:
        return None, errors, 0
    errors = (
        _normalize_prescription_events(merged)
        + _normalize_significations(merged)
        + _validate(merged)
    )
    if errors:
        return None, errors, 0
    # Reads the trust register — only when a client actually leaves, and
    # only once the record is otherwise valid.
    errors = _client_trust_errors(dossier_id, existing, merged)
    if errors:
        return None, errors, 0

    # Check file_number uniqueness (if changed)
    if merged["file_number"] != existing.get("file_number"):
        try:
            dup = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("file_number", "==", merged["file_number"]))
                .limit(1)
                .get()
            )
            for d in dup:
                if d.id != dossier_id:
                    return None, ["Ce numéro de dossier existe déjà."], 0
        except Exception as exc:
            logger.warning(
                "update_dossier: duplicate-check query failed for %s: %s",
                sanitize_log_value(dossier_id), exc,
            )

    now = datetime.now(timezone.utc)
    provenance.stamp_update(merged, now)

    # Sync flat ID arrays
    _derive_role(merged)
    _rebuild_party_mirrors(merged)

    # Closure date: auto-determined when the dossier is closed/archived, but
    # user-editable. Respect a date supplied on the form; otherwise keep the
    # existing one, falling back to TODAY (Montréal, date-only — never `now`,
    # a timestamp that reads as tomorrow after 20:00) on the closing
    # transition. An active/pending dossier is never closed, so it carries no
    # closure date: reopening clears it (the model rule; the previous value
    # is not kept anywhere).
    if merged.get("status") in ("fermé", "archivé"):
        if not merged.get("closed_date"):
            merged["closed_date"] = (
                existing.get("closed_date") or _today_midnight_utc()
            )
    else:
        merged["closed_date"] = None

    # Derive the prescription deadline ("date pour agir") from the recourse fields.
    _apply_prescription_deadline(merged)

    # Trust balances live on this document but are owned EXCLUSIVELY by
    # models/trust.py, which updates them transactionally (Phase K). This
    # full-document set() would otherwise persist whatever `existing` held when
    # this function started — clobbering a trust write that committed in the
    # meantime. A stale-HIGH cleared balance could later permit an overdraft,
    # so re-read the three fields at the last moment and let the trust layer's
    # values win. Best-effort: on a read blip, keep `existing`'s already-fresh
    # values rather than block an unrelated dossier edit — the register in
    # trust_transactions is the source of truth and
    # scripts/verify_trust_integrity.py catches any residual drift.
    #
    # The GUARDED path (expected_etag given) skips this re-read: it is
    # subsumed, atomically, by the etag comparison. Every trust write to a
    # dossier regenerates its etag (models/trust.py stamps each dossier
    # update with provenance.update_fields), so a trust write that landed
    # since `existing` was read makes the transaction refuse the whole save
    # as stale — nothing is clobbered, and nothing is refreshed either.
    if expected_etag is None:
        try:
            snap = db.collection(COLLECTION).document(dossier_id).get()
            if snap.exists:
                current = snap.to_dict() or {}
                for _tf in (
                    "trust_balance",
                    "trust_balance_by_client",
                    "trust_cleared_by_client",
                ):
                    if _tf in current:
                        merged[_tf] = current[_tf]
        except Exception as exc:
            logger.warning(
                "update_dossier: trust-field refresh failed for %s: %s",
                sanitize_log_value(dossier_id), type(exc).__name__,
            )

    try:
        concurrency.commit_document(
            db.collection(COLLECTION).document(dossier_id), merged,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR], 0
    except concurrency.Vanished:
        return None, ["Dossier introuvable."], 0
    except Exception:
        log_unexpected("dossier write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], 0
    provenance.note_commit(COLLECTION, dossier_id)

    # AFTER the commit, best-effort: a journal failure never fails the save
    # (the audit_events doctrine — the write already happened).
    journaled = _journal_party_detaches(dossier_id, existing, merged)
    return merged, [], journaled


# ── Party links (lot 4a) ──────────────────────────────────────────────────
#
# A dossier's parties are two arrays of ``{id, name, roles, avocat_id,
# avocat_name}`` entries. The web form posts them WHOLE; the connector (lot
# 4b) edits one entry at a time through the helpers below. The rules live
# here, in the model, so both paths meet the same ones.

PARTY_SIDES: tuple[str, ...] = ("clients", "opposing_parties")
PARTY_SIDE_LABELS = {
    "clients": "les clients",
    "opposing_parties": "les parties adverses",
}

# Refusals a caller may want to recognise — constants, never parsed French.
PARTY_TRUST_CHECK_UNAVAILABLE = (
    "Impossible de vérifier le registre du fidéicommis de ce dossier : rien "
    "n'a été enregistré. Veuillez réessayer."
)
PARTY_NOT_ON_DOSSIER = "Cette partie ne figure pas au dossier."
PARTY_ON_BOTH_SIDES = (
    "Ce contact figure à la fois parmi les clients et parmi les parties "
    "adverses du dossier : précisez le côté visé."
)
PARTY_LAST_CLIENT = (
    "C'est le seul client du dossier : il ne peut pas en être retiré. "
    "Ajoutez d'abord l'autre client, puis retirez celui-ci."
)
PARTY_INVALID_SIDE = "Côté invalide : « clients » ou « opposing_parties »."
PARTY_NOTHING_TO_CHANGE = (
    "Rien à modifier : précisez les rôles ou l'avocat de la partie."
)
PARTY_NAMES_UNREADABLE = (
    "Les fiches des contacts n'ont pas pu être lues : aucun nom n'a été "
    "rafraîchi. Veuillez réessayer."
)
PARTY_DOSSIERS_UNREADABLE = (
    "Les dossiers de ce contact n'ont pas pu être lus : aucun nom n'a été "
    "rafraîchi. Veuillez réessayer."
)
# The per-contact refresh is bounded — each dossier is its own write.
REFRESH_MAX_DOSSIERS = 50


def _entry_ids(entries) -> list[str]:
    """The ids of a party array, in order, blanks skipped (never raises —
    an id-less entry is ``_rebuild_party_mirrors``' KeyError to raise)."""
    out: list[str] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            pid = str(entry.get("id") or "").strip()
            if pid:
                out.append(pid)
    return out


def _entry_names(*arrays) -> dict[str, str]:
    """``{partie_id: snapshot name}`` over party arrays (first wins)."""
    names: dict[str, str] = {}
    for entries in arrays:
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("id"):
                names.setdefault(
                    str(entry["id"]).strip(), str(entry.get("name") or "").strip()
                )
    return names


def _joined_labels(labels: list[str]) -> str:
    """« A, B, C et 2 autres » — the partie model's wording."""
    shown = ", ".join(labels[:3])
    more = len(labels) - 3
    if more > 0:
        shown += f" et {more} autre{'s' if more > 1 else ''}"
    return shown


def _party_shape_errors(existing: Optional[dict], merged: dict) -> list[str]:
    """The two PURE link rules — no read. *existing* is ``None`` on a create.

    **Cross-side, grow-only.** One contact may not be a client and an
    opposing party of the same dossier: procedures, the conflict check and
    the coverage report would all read it wrong. The rule is that the
    intersection of the two sides may never GROW: a hard rule would refuse
    every later save of a legacy dossier that already carries a duplicate
    (the model re-validates the whole record on every update — the « legacy
    invalid field blocks every edit » trap). On a create nothing is legacy,
    so any intersection is growth.

    **A served party stays.** A party a signification names (superseded or
    not — the register is append-only) cannot leave the dossier: the delays
    of arts. 145/147 C.p.c. run per party. The rule reads the significations
    of *merged*, so a web form that removes the party AND the erroneous
    signification in one save is a correction, not a refusal.
    """
    before = existing or {}
    after_c = set(_entry_ids(merged.get("clients")))
    after_o = set(_entry_ids(merged.get("opposing_parties")))
    before_c = set(_entry_ids(before.get("clients")))
    before_o = set(_entry_ids(before.get("opposing_parties")))
    errors: list[str] = []

    grown = (after_c & after_o) - (before_c & before_o)
    if grown:
        names = _entry_names(merged.get("clients"), merged.get("opposing_parties"))
        shown = _joined_labels([names.get(pid) or pid for pid in sorted(grown)])
        errors.append(
            "Un même contact ne peut pas être à la fois client et partie "
            f"adverse du dossier : {shown}."
        )

    if existing is not None:
        left = (before_c | before_o) - (after_c | after_o)
        served = {
            str(s.get("partie_id") or "").strip()
            for s in (merged.get("significations") or [])
            if isinstance(s, dict)
        }
        blocked = sorted(left & served)
        if blocked:
            names = _entry_names(before.get("clients"), before.get("opposing_parties"))
            shown = _joined_labels([names.get(pid) or pid for pid in blocked])
            errors.append(
                "Une partie signifiée ne peut pas être retirée du dossier : "
                f"{shown} — une signification au dossier la nomme."
            )
    return errors


def _client_trust_errors(
    dossier_id: str, existing: Optional[dict], merged: dict
) -> list[str]:
    """Refuse to take a client with trust history out of the dossier's clients.

    A trust entry names a (dossier, client) couple, and every later entry
    for that client is refused when the client is no longer a client of the
    dossier (``models.trust``, « client_hors_dossier ») — so removing a
    client who holds, or ever held, funds here strands them: the carte-client
    and the dossier's trust tab lose the client's name, and nothing can move
    the money out. « EVER », not « currently »: a zero balance still leaves a
    permanent register that must go on naming its client.

    History is a key of ``trust_balance_by_client`` / ``trust_cleared_by_
    client`` OR any ``trust_transactions`` row for the couple — a STRICT read
    (``trust.list_transactions`` propagates) on the existing ``(dossier_id,
    client_id, sequence)`` index. Any failure refuses the save (fail closed).
    An opposing party is never a trust client: its removal reads nothing.
    """
    if existing is None:
        return []
    after = set(_entry_ids(merged.get("clients")))
    removed = [
        pid for pid in dict.fromkeys(_entry_ids(existing.get("clients")))
        if pid not in after
    ]
    if not removed:
        return []
    keys = set((existing.get("trust_balance_by_client") or {}).keys()) | set(
        (existing.get("trust_cleared_by_client") or {}).keys()
    )
    held = [pid for pid in removed if pid in keys]
    try:
        from models import trust as trust_model  # local: trust imports us

        for pid in removed:
            if pid in held:
                continue
            if trust_model.list_transactions(
                dossier_id=dossier_id, client_id=pid, limit=1
            ):
                held.append(pid)
    except Exception:
        log_unexpected(
            "dossier party removal: trust register unreadable",
            dossier_id=dossier_id,
        )
        return [PARTY_TRUST_CHECK_UNAVAILABLE]
    if not held:
        return []
    names = _entry_names(existing.get("clients"))
    shown = _joined_labels([names.get(pid) or pid for pid in held])
    return [
        f"{shown} a eu des sommes en fidéicommis dans ce dossier : il ne peut "
        "pas en être retiré comme client — le registre du fidéicommis doit "
        "toujours pouvoir le nommer."
    ]


def _journal_party_detaches(
    dossier_id: str, existing: dict, saved: dict
) -> int:
    """One ``audit_events`` row per link that LEFT the dossier; the count.

    A detach removes a LINK — the contact stays — but it erases a stored
    array entry, and the journal is what answers « what vanished » for a
    sync-aware reader (``list_deletions``). Per side: an id on side S before
    and not after is journaled with ``status = S``, except a MOVE (absent
    from the other side before, present there after), which is a correction
    of the side, not a detach. Best-effort and never raises: it runs after
    the commit.
    """
    try:
        from models import audit_event  # local: keeps the model graph flat

        before_c = set(_entry_ids(existing.get("clients")))
        before_o = set(_entry_ids(existing.get("opposing_parties")))
        after_c = set(_entry_ids(saved.get("clients")))
        after_o = set(_entry_ids(saved.get("opposing_parties")))
        names = _entry_names(existing.get("clients"), existing.get("opposing_parties"))
        count = 0
        for side, before, after, other_before, other_after in (
            ("clients", before_c, after_c, before_o, after_o),
            ("opposing_parties", before_o, after_o, before_c, after_c),
        ):
            for pid in sorted(before - after):
                if pid in other_after and pid not in other_before:
                    continue  # moved to the other side — not a detach
                if audit_event.record_deletion(
                    "dossier_party", pid, dossier_id=dossier_id,
                    title=names.get(pid, ""), status=side,
                ) is not None:
                    count += 1
        return count
    except Exception:
        log_unexpected("dossier party detach journal failed", dossier_id=dossier_id)
        return 0


def _locate_party(
    dossier: dict, partie_id: str, side: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """``(side, None)`` — or ``(None, refusal)``.

    A contact on BOTH sides (a legacy duplicate the grow-only rule tolerates)
    needs *side*; everywhere else the side is found, and a *side* that does
    not hold the contact is refused rather than silently corrected.
    """
    if side is not None and side not in PARTY_SIDES:
        return None, PARTY_INVALID_SIDE
    sides = [s for s in PARTY_SIDES if partie_id in _entry_ids(dossier.get(s))]
    if side is not None:
        if side not in sides:
            return None, (
                f"Cette partie ne figure pas parmi {PARTY_SIDE_LABELS[side]} "
                "du dossier."
            )
        return side, None
    if not sides:
        return None, PARTY_NOT_ON_DOSSIER
    if len(sides) > 1:
        return None, PARTY_ON_BOTH_SIDES
    return sides[0], None


def _entry_index(entries: list, partie_id: str) -> int:
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == partie_id:
            return index
    raise LookupError(partie_id)  # _locate_party found it: cannot happen


def _prescription_moved(before: dict, after: Optional[dict]) -> bool:
    """Every save re-derives ``prescription_date`` from the recourse fields
    (:func:`_apply_prescription_deadline`), so even a link edit can move it
    — the report says so rather than let a limitation date move in silence."""
    if after is None:
        return False
    return before.get("prescription_date") != after.get("prescription_date")


def update_dossier_party(
    dossier_id: str,
    partie_id: str,
    *,
    side: Optional[str] = None,
    roles: Optional[list] = None,
    avocat_id: Optional[str] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Change ONE party's roles and/or lawyer on a dossier.

    Returns ``(dossier, errors, report)`` — a documented deviation from the
    ``(doc, errors)`` convention, the ``delete_folder`` precedent: a caller
    must be able to say « changed » apart from « already so », and what the
    derived dossier-level ``role`` became.

    *roles* is a FULL replacement (``[]`` allowed; ⊆ ``PARTY_ROLES``, no
    duplicate); *avocat_id* ``""`` removes the lawyer, a contact id sets it
    (resolved here, its name snapshotted — never supplied by the caller).
    ``None`` leaves either alone; at least one must be given. The array is
    rebuilt from the STORED one: every other entry — and this entry's own
    name snapshot — is written back as stored. An unchanged request writes
    nothing (``report["changed"]`` False). The save is compare-and-set
    against the etag read here (after checking *expected_etag* when given),
    so a write landing in between refuses instead of being reverted.
    """
    partie_id = str(partie_id or "").strip()
    report: dict = {"changed": False, "partie_id": partie_id}
    if not partie_id:
        return None, ["Une partie est requise."], report
    if roles is None and avocat_id is None:
        return None, [PARTY_NOTHING_TO_CHANGE], report

    existing = get_dossier(dossier_id)
    if not existing:
        return None, ["Dossier introuvable."], report
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    found_side, refusal = _locate_party(existing, partie_id, side)
    if refusal:
        return None, [refusal], report

    stored = list(existing.get(found_side) or [])
    index = _entry_index(stored, partie_id)
    entry = stored[index]
    roles_before = list(entry.get("roles") or [])
    avocat_before = str(entry.get("avocat_id") or "")

    new_roles = roles_before
    if roles is not None:
        if not isinstance(roles, (list, tuple)):
            return None, ["Les rôles doivent être une liste."], report
        clean = [str(r) for r in roles]
        if any(r not in PARTY_ROLES for r in clean):
            return None, ["Rôle de partie invalide."], report
        if len(set(clean)) != len(clean):
            return None, ["Un même rôle figure deux fois."], report
        new_roles = clean

    new_avocat = avocat_before
    new_avocat_name = str(entry.get("avocat_name") or "")
    if avocat_id is not None:
        new_avocat = str(avocat_id or "").strip()
        if new_avocat == partie_id:
            return None, ["Une partie ne peut pas être son propre avocat."], report
        if new_avocat and new_avocat != avocat_before:
            from models import partie as partie_model  # local: no model cycle

            avocat = partie_model.get_partie(new_avocat)
            if avocat is None:
                return None, ["Avocat introuvable."], report
            new_avocat_name = partie_model.display_name(avocat)
        elif not new_avocat:
            new_avocat_name = ""

    report.update({
        "side": found_side,
        "name": str(entry.get("name") or ""),
        "roles_before": roles_before,
        "roles_after": new_roles,
        "avocat_id_before": avocat_before,
        "avocat_id_after": new_avocat,
        "avocat_name_after": new_avocat_name,
        "role_before": str(existing.get("role") or ""),
        "role_after": str(existing.get("role") or ""),
        "prescription_date_moved": False,
    })
    if new_roles == roles_before and new_avocat == avocat_before:
        return existing, [], report

    rebuilt = dict(entry)
    rebuilt["roles"] = new_roles
    rebuilt["avocat_id"] = new_avocat
    rebuilt["avocat_name"] = new_avocat_name
    stored[index] = rebuilt
    saved, errors, _journaled = _update_dossier(
        dossier_id, {found_side: stored}, concurrency.etag_of(existing),
    )
    if errors:
        return None, errors, report
    report["changed"] = True
    report["role_after"] = str(saved.get("role") or "")
    report["prescription_date_moved"] = _prescription_moved(existing, saved)
    return saved, [], report


def remove_dossier_party(
    dossier_id: str,
    partie_id: str,
    *,
    side: Optional[str] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Detach ONE party from a dossier — the contact itself is untouched.

    Returns ``(dossier, errors, report)`` (see :func:`update_dossier_party`).
    Refused, in this order: the party is not on the dossier (or is on both
    sides and *side* is missing); it is the LAST client; a signification
    names it (superseded or not) — unless it stays on the other side of a
    legacy duplicate, which is the repair of that duplicate; it is a client
    with trust history on the dossier (through :func:`update_dossier`, which
    enforces it for every caller, fail closed). The detach is journaled in
    ``audit_events`` (``dossier_party``) after the commit;
    ``report["journaled"]`` says whether that row was written.
    """
    partie_id = str(partie_id or "").strip()
    report: dict = {"changed": False, "partie_id": partie_id}
    if not partie_id:
        return None, ["Une partie est requise."], report

    existing = get_dossier(dossier_id)
    if not existing:
        return None, ["Dossier introuvable."], report
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], report
    found_side, refusal = _locate_party(existing, partie_id, side)
    if refusal:
        return None, [refusal], report

    stored = list(existing.get(found_side) or [])
    index = _entry_index(stored, partie_id)
    entry = stored[index]
    other_side = "opposing_parties" if found_side == "clients" else "clients"
    stays = partie_id in _entry_ids(existing.get(other_side))
    report.update({
        "side": found_side,
        "name": str(entry.get("name") or ""),
        "was_first_client": found_side == "clients" and index == 0,
        "role_before": str(existing.get("role") or ""),
        "role_after": str(existing.get("role") or ""),
        "journaled": False,
        "prescription_date_moved": False,
    })

    if found_side == "clients" and not [
        pid for pid in _entry_ids(stored) if pid != partie_id
    ]:
        return None, [PARTY_LAST_CLIENT], report
    if not stays:
        served = [
            s for s in (existing.get("significations") or [])
            if isinstance(s, dict)
            and str(s.get("partie_id") or "").strip() == partie_id
        ]
        if served:
            n = len(served)
            return None, [
                f"Cette partie a reçu {n} signification"
                f"{'s' if n > 1 else ''} au dossier : une partie signifiée ne "
                "peut pas en être retirée."
            ], report

    remaining = [e for i, e in enumerate(stored) if i != index]
    saved, errors, journaled = _update_dossier(
        dossier_id, {found_side: remaining}, concurrency.etag_of(existing),
    )
    if errors:
        return None, errors, report
    report["changed"] = True
    report["journaled"] = journaled > 0
    report["role_after"] = str(saved.get("role") or "")
    report["prescription_date_moved"] = _prescription_moved(existing, saved)
    return saved, [], report


def _refresh_one(
    dossier: dict, contacts: dict[str, dict], only: Optional[str] = None,
) -> dict:
    """Re-snapshot one dossier's party and lawyer names; its report row.

    *only* restricts the refresh to the snapshots of ONE contact (as a
    party and as a party's lawyer) — the per-contact mode must never touch
    another party's name.
    """
    from models import partie as partie_model  # local: no model cycle

    dossier_id = str(dossier.get("id") or "")
    row: dict = {
        "dossier_id": dossier_id,
        "file_number": str(dossier.get("file_number") or ""),
        "outcome": "unchanged",
        "reason": "",
        "changes": [],
        "missing_partie_ids": [],
        "prescription_date_moved": False,
        # The dossier's etag AS STORED after this row — the one read here on
        # « unchanged » / « refused », the one written on « applied » — so a
        # caller's next edit of that dossier needs no re-read. Named
        # `dossier_etag`, never `etag`: this row is a REPORT, not a record
        # (tests/test_provenance's sweep reads an « etag » key as a write).
        "dossier_etag": concurrency.etag_of(dossier),
    }
    missing: list[str] = []
    changes: list[dict] = []
    data: dict = {}
    for side in PARTY_SIDES:
        rebuilt: list = []
        side_changed = False
        for entry in dossier.get(side) or []:
            if not isinstance(entry, dict):
                rebuilt.append(entry)
                continue
            new_entry = dict(entry)
            for id_key, name_key in (("id", "name"), ("avocat_id", "avocat_name")):
                pid = str(entry.get(id_key) or "").strip()
                if not pid or (only is not None and pid != only):
                    continue
                contact = contacts.get(pid)
                if contact is None:
                    # Never blank a snapshot: a missing contact keeps the
                    # name the procedures cite, and the report says so.
                    if pid not in missing:
                        missing.append(pid)
                    continue
                live = partie_model.display_name(contact).strip()
                before = str(entry.get(name_key) or "")
                if live and live != before:
                    new_entry[name_key] = live
                    side_changed = True
                    changes.append({
                        "partie_id": pid, "side": side, "field": name_key,
                        "before": before, "after": live,
                    })
            rebuilt.append(new_entry)
        if side_changed:
            data[side] = rebuilt
    row["missing_partie_ids"] = missing
    if not data:
        return row
    saved, errors, _journaled = _update_dossier(
        dossier_id, data, concurrency.etag_of(dossier)
    )
    if errors:
        row["outcome"] = "refused"
        row["reason"] = "; ".join(errors)
        return row
    row["outcome"] = "applied"
    row["changes"] = changes
    row["dossier_etag"] = concurrency.etag_of(saved)
    row["prescription_date_moved"] = _prescription_moved(dossier, saved)
    return row


def refresh_party_names(
    *, dossier_id: Optional[str] = None, partie_id: Optional[str] = None
) -> tuple[list[dict], list[str]]:
    """Re-snapshot party and lawyer names from the CURRENT contacts.

    The ``name`` / ``avocat_name`` of a party entry are snapshots taken when
    the party was added — what a generated procedure cites — and a contact
    correction never reaches them (nor should it silently: a fan-out write on
    every contact edit would be invisible). This is the explicit refresh,
    for ONE dossier (*dossier_id*) or for every dossier citing ONE contact
    (*partie_id* — as a party or as a party's lawyer; at most
    :data:`REFRESH_MAX_DOSSIERS`). Exactly one selector.

    Returns ``(rows, errors)``: *errors* refuses the whole call and nothing
    was written; otherwise one row per dossier — ``applied`` (with each
    change, before and after), ``unchanged`` (no write), or ``refused`` (the
    save was refused, its reason given; the other dossiers go on). A contact
    that no longer exists keeps its snapshot and is reported in
    ``missing_partie_ids``. Each row carries the dossier's etag as stored
    after it (``dossier_etag``). Per contact, ONLY that contact's snapshots
    move — never another party's name on the same dossier. Only dossiers
    are written: an invoice, a trust entry or an already-generated document
    keeps the name it was issued with. Each save is compare-and-set against
    the version read here.
    """
    from models import partie as partie_model  # local: no model cycle

    did = str(dossier_id or "").strip()
    pid = str(partie_id or "").strip()
    if bool(did) == bool(pid):
        return [], ["Précisez un dossier OU un contact — exactement un des deux."]

    if pid:
        contact = partie_model.get_partie(pid)
        if contact is None:
            return [], ["Contact introuvable."]
        try:
            dossiers = list_dossiers_for_partie_strict(pid)
        except Exception:
            log_unexpected("refresh party names: dossiers unreadable")
            return [], [PARTY_DOSSIERS_UNREADABLE]
        if len(dossiers) > REFRESH_MAX_DOSSIERS:
            return [], [
                f"Ce contact figure dans {len(dossiers)} dossiers (plus de "
                f"{REFRESH_MAX_DOSSIERS}) : rafraîchissez-les un dossier à "
                "la fois."
            ]
        contacts = {pid: contact}
        return [_refresh_one(d, contacts, only=pid) for d in dossiers], []

    dossier = get_dossier(did)
    if not dossier:
        return [], ["Dossier introuvable."]
    ids: list[str] = []
    for side in PARTY_SIDES:
        for entry in dossier.get(side) or []:
            if not isinstance(entry, dict):
                continue
            for key in ("id", "avocat_id"):
                value = str(entry.get(key) or "").strip()
                if value:
                    ids.append(value)
    ids = list(dict.fromkeys(ids))
    contacts = partie_model.get_parties_bulk(ids) if ids else {}
    if ids and not contacts:
        # get_parties_bulk fails OPEN to {} — never read that as « every
        # contact vanished » (the coverage report's kyc_checked idiom).
        return [], [PARTY_NAMES_UNREADABLE]
    return [_refresh_one(dossier, contacts)], []


# Child collections checked before a dossier may be deleted:
# (collection name, singular French label, plural French label)
_CHILD_COLLECTIONS = (
    ("documents", "document", "documents"),
    ("timeentries", "entrée de temps", "entrées de temps"),
    ("expenses", "dépense", "dépenses"),
    ("invoices", "facture", "factures"),
    ("hearings", "audience", "audiences"),
    ("tasks", "tâche", "tâches"),
    ("notes", "note", "notes"),
    ("protocols", "protocole", "protocoles"),
    ("folders", "répertoire de documents", "répertoires de documents"),
    # Trust (Phase K): a dossier that has EVER had a fidéicommis entry can never
    # be deleted — the register is permanent, even at a zero balance. Because
    # trust_transactions rows are never hard-deleted, the same count>0 refusal
    # enforces "ever existed" (spec §6.3). Archive the dossier instead.
    ("trust_transactions", "opération fiduciaire", "opérations fiduciaires"),
)


def _count_dossier_children(dossier_id: str) -> list[tuple[int, str]]:
    """Count child records referencing a dossier.

    Returns a list of (count, French label) tuples for every child type
    that still has at least one record linked to the dossier.
    """
    remaining: list[tuple[int, str]] = []
    for collection_name, singular, plural in _CHILD_COLLECTIONS:
        # Fail CLOSED: a count that cannot be established must refuse the
        # deletion rather than risk orphaning children — let errors propagate
        # to delete_dossier, which aborts.
        query = db.collection(collection_name).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        count = sum(1 for _ in query.stream())
        if count > 0:
            remaining.append((count, singular if count == 1 else plural))
    return remaining


def delete_dossier(dossier_id: str) -> tuple[bool, str]:
    """Delete a dossier. Returns (success, error_message).

    Deletion is REFUSED while child records (time entries, expenses,
    invoices, hearings, tasks, notes, protocols, documents, folders)
    still reference the dossier. Silently cascading the destruction of
    billing/legal records — or orphaning confidential Storage blobs with
    no UI path to purge them — would be worse than blocking.
    """
    existing = get_dossier(dossier_id)
    if not existing:
        return False, "Dossier introuvable."

    try:
        remaining = _count_dossier_children(dossier_id)
    except Exception as exc:
        logger.warning(
            "delete_dossier: child check failed for %s: %s",
            sanitize_log_value(dossier_id), type(exc).__name__,
        )
        return False, (
            "Impossible de vérifier le contenu du dossier. "
            "Veuillez réessayer."
        )
    if remaining:
        details = ", ".join(f"{count} {label}" for count, label in remaining)
        return False, (
            f"Impossible de supprimer : le dossier contient encore {details}. "
            "Archivez le dossier ou supprimez d'abord son contenu."
        )

    try:
        db.collection(COLLECTION).document(dossier_id).delete()
        return True, ""
    except Exception:
        log_unexpected("dossier delete failed")
        return False, "Erreur lors de la suppression. Veuillez réessayer."


def suggest_file_number() -> str:
    """Public wrapper for auto-suggesting the next file number."""
    return _suggest_next_file_number()


# Shared implementation lives in models/__init__.py; aliased so this module's
# helpers (and their tests) keep a stable local name.
_aggregation_values = aggregation_values


def count_open() -> int:
    """Count open dossiers (actif or en_attente) via a COUNT aggregation.

    A single server-side COUNT over ``status in (actif, en_attente)``
    replaces the dashboard's two full list scans. The ``in`` filter on a
    single field is served by the automatic single-field index on
    ``status`` — no composite index required for COUNT.

    Returns 0 on failure (graceful degradation for the dashboard stat).
    """
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("status", "in", ["actif", "en_attente"])
        )
        values = _aggregation_values(query.count(alias="open").get())
        return int(values.get("open", 0) or 0)
    except Exception as exc:
        logger.warning("count_open: aggregation query failed: %s", exc)
        return 0


# The statuses whose dossiers still carry a running limitation period. An
# « en_attente » dossier is ON HOLD, not closed: its prescription keeps
# running in law whatever the file's workflow state, so it must alert
# exactly like an « actif » one. Until lot 0b (2026-09-27) the query read
# ``status == actif`` only, and putting a dossier on hold silenced its
# deadline on the dashboard AND in the MCP briefing, in perfect silence.
PRESCRIPTION_ALERT_STATUSES: tuple[str, ...] = ("actif", "en_attente")


def list_prescription_alerts(cutoff: datetime, limit: int = 50) -> list[dict]:
    """Return open dossiers with a prescription date on or before *cutoff*.

    « Open » means every status of :data:`PRESCRIPTION_ALERT_STATUSES`
    (``actif`` and ``en_attente``). ONE query PER STATUS, each on the
    existing ``dossiers`` composite index (status ASC, prescription_date
    ASC) — ``status == <s>`` AND ``prescription_date <= cutoff``, ordered by
    prescription_date ascending and bounded by *limit* — merged and sorted
    here in Python. Never a single ``status in (…)`` + ``order_by``: that is
    a different query shape, and a shape the index does not serve fails
    until an index builds, degrading this view to an EMPTY LIST — the worst
    possible failure mode for a limitation deadline. Dossiers without a
    prescription date are excluded automatically: Firestore range filters
    never match null/missing values. Legacy party fields are migrated on
    read, like every other dossier read path.

    A dossier carrying a ``prise_action_date`` is dropped: the recourse has
    been filed, so the deadline no longer looms. That filter lives HERE, and
    in Python, for three reasons:

    * both consumers must agree — the dashboard (``routes/dashboard.py``) and
      the MCP ``get_agenda`` tool (``mcp/handlers.py``). Silencing only the
      dashboard would leave Claude warning about a prescription that has
      already been interrupted — advice that is actively wrong;
    * a third ``.where()`` would need a new composite index, and until it
      finished building the query would fail and the view degrade to an EMPTY
      LIST — the worst possible failure mode for a limitation deadline;
    * a server-side ``== None`` would not match documents where the key is
      simply ABSENT, which is every pre-existing dossier (the field is
      additive, with no migration). Every current alert would vanish, in
      silence.

    Each status query degrades ON ITS OWN: a failed read is logged (ERROR,
    ``log_unexpected``) and skipped, so an outage of one never hides the
    other's alerts; one row the party migration cannot read is alerted as
    stored rather than dropping its whole query. The
    « result window full » warning is likewise judged PER QUERY, on its RAW
    count. Rows are de-duplicated by id: the two reads are not one
    snapshot, so a dossier whose status flips between them would otherwise
    alert twice. The result may therefore hold up to ``2 * limit`` rows.

    Returns [] when every query failed (the dashboard degrades gracefully).
    """
    raw: list[dict] = []
    seen: set[str] = set()
    for status in PRESCRIPTION_ALERT_STATUSES:
        try:
            query = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("status", "==", status))
                .where(filter=FieldFilter("prescription_date", "<=", cutoff))
                .order_by("prescription_date")
                .limit(limit)
            )
            snaps = list(query.stream())
        except Exception:
            # A limitation-deadline list silently missing a status is not a
            # routine warning: ERROR, typed helper, the status as a field
            # (never the exception text in the message — the traceback goes
            # through the RedactionFilter).
            log_unexpected("list_prescription_alerts: status query failed",
                           status=status)
            continue
        rows: list[dict] = []
        for snap in snaps:
            doc = snap.to_dict() or {}
            try:
                rows.append(_migrate_parties(doc))
            except Exception:
                # One legacy row the party migration cannot read used to
                # drop its WHOLE status query (the comprehension sat inside
                # the try above). The row is alerted as stored — the
                # migration only reshapes party fields, and the prescription
                # fields the alert reads are untouched by it.
                log_unexpected("list_prescription_alerts: row migration failed",
                               dossier_id=str(doc.get("id") or ""))
                rows.append(doc)
        if len(rows) >= limit:
            # Prescription deadlines must never be silently truncated. Checked
            # on the RAW count of THIS query, before the silencing filter: a
            # silenced dossier still consumes a slot, so a full window means
            # real alerts are hidden beyond it — which must still be said.
            logger.warning(
                "list_prescription_alerts: result window full "
                "(status=%s, limit=%d) — some alerts may be hidden",
                status, limit,
            )
        for d in rows:
            key = d.get("id") or ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            raw.append(d)
    # One merged chronology, oldest raw date first — the order each query
    # already had on its own. Every row carries a date (the range filter
    # excludes documents without one).
    raw.sort(key=lambda d: d.get("prescription_date"))

    # Silencing + the effective window run through derive_prescription
    # (WP13): a depot event — or the legacy prise_action_date it folds
    # in — silences (art. 2896: interrupted until judgment); a
    # reconnaissance/suspension pushes the EFFECTIVE date, possibly past
    # the cutoff. Events only push dates LATER, so the raw-date server
    # query over-fetches, never under-fetches — no new index. Rows kept
    # with no effective date are « a_verifier »: alerted, flagged
    # unverified, never silently dropped.
    out = []
    for d in raw:
        try:
            derived = derive_prescription(d)
        except Exception:
            # A row the derivation cannot read is ALERTED as « a_verifier »
            # on its raw date, never dropped — and it no longer empties the
            # whole list, as it did through the old enclosing try. A bug in
            # the one derivation seam is unexpected: ERROR, typed helper.
            log_unexpected("list_prescription_alerts: derivation failed")
            derived = {"status": "a_verifier", "date_effective": None}
        if derived["status"] in ("interrompue", "imprescriptible"):
            continue
        eff = derived["date_effective"]
        if eff is not None and eff > cutoff:
            continue
        d["prescription_status"] = derived["status"]
        d["prescription_date_effective"] = eff
        out.append(d)
    return out


def count_dossiers_for_partie_strict(partie_id: str) -> int:
    """Count how many dossiers reference a partie, propagating query errors.

    Used by FK safety checks (e.g. partie deletion) that must fail CLOSED
    when the count cannot be established.
    """
    q1 = db.collection(COLLECTION).where(filter=FieldFilter("client_ids", "array_contains", partie_id))
    q2 = db.collection(COLLECTION).where(filter=FieldFilter("opposing_party_ids", "array_contains", partie_id))
    # avocat_ids (July 2026): a partie linked as a party's LAWYER is
    # referenced too — deleting it would leave a dead clickable link.
    q3 = db.collection(COLLECTION).where(filter=FieldFilter("avocat_ids", "array_contains", partie_id))
    ids = (
        {doc.id for doc in q1.stream()}
        | {doc.id for doc in q2.stream()}
        | {doc.id for doc in q3.stream()}
    )
    return len(ids)


def _dossiers_for_partie(partie_id: str) -> list[dict]:
    """The three ``array_contains`` reads behind both listers — propagates.

    ONE query body, so the display lister and the strict one can never
    disagree on what « the dossiers of this contact » means.
    """
    q1 = db.collection(COLLECTION).where(filter=FieldFilter("client_ids", "array_contains", partie_id))
    q2 = db.collection(COLLECTION).where(filter=FieldFilter("opposing_party_ids", "array_contains", partie_id))
    q3 = db.collection(COLLECTION).where(filter=FieldFilter("avocat_ids", "array_contains", partie_id))
    seen: set[str] = set()
    results: list[dict] = []
    for query in (q1, q2, q3):
        for doc in query.stream():
            d = _migrate_parties(doc.to_dict())
            if d.get("id") not in seen:
                seen.add(d["id"])
                results.append(d)
    # Newest opened first, same-day ties by creation (date-only
    # opened_date since lot 4a — see _newest_opened_first_key).
    results.sort(key=_newest_opened_first_key, reverse=True)
    return results


def list_dossiers_for_partie(partie_id: str) -> list[dict]:
    """Return all dossiers linked to a partie, newest first.

    Fails OPEN to ``[]`` — a display reader (the contact's fiche). A caller
    that DECIDES on the answer uses :func:`list_dossiers_for_partie_strict`.
    """
    try:
        return _dossiers_for_partie(partie_id)
    except Exception:
        return []


def list_dossiers_for_partie_strict(partie_id: str) -> list[dict]:
    """:func:`list_dossiers_for_partie`, but read errors PROPAGATE.

    For a write that relies on the answer (the per-contact name refresh,
    the connector's KYC eligibility from lot 4b): read through the display
    lister, an outage would read as « this contact is on no dossier ». An
    empty id is refused before any read (an ``array_contains ""`` would
    answer nothing, but « nothing » is not an answer to a blank question).
    """
    if not isinstance(partie_id, str) or not partie_id.strip():
        raise ValueError("list_dossiers_for_partie_strict needs a partie id")
    return _dossiers_for_partie(partie_id)


# ── RFC-5545 VJOURNAL serialization ───────────────────────────────────────


def dossier_to_vjournal(dossier: dict) -> str:
    """Serialize a dossier dict to an RFC-5545 VJOURNAL string."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Dossier//FR")
    cal.add("version", "2.0")

    journal = icalendar.Journal()
    journal.add("uid", dossier.get("vjournal_uid", ""))
    journal.add("summary", f"{dossier.get('file_number', '')} — {dossier.get('title', '')}")

    # DTSTART = opened_date
    opened = dossier.get("opened_date")
    if opened and hasattr(opened, "date"):
        journal.add("dtstart", opened.date())

    # STATUS mapping
    status_map = {
        "actif": "FINAL",
        "en_attente": "DRAFT",
        "fermé": "CANCELLED",
        "archivé": "CANCELLED",
    }
    journal.add("status", status_map.get(dossier.get("status", ""), "DRAFT"))

    # CATEGORIES — the domaine label, then the action if the dossier has one.
    # Unlike the old matter_type line, an unknown key resolves to nothing
    # rather than leaking a raw key like `litige_civil` as a French category.
    categories = []
    domaine_label = DOMAINE_LABELS.get(dossier.get("domaine", ""), "")
    if dossier.get("domaine") and domaine_label:
        categories.append(domaine_label)
    action_label = taxonomie.action_label(dossier.get("action", ""))
    if action_label:
        categories.append(action_label)
    if categories:
        journal.add("categories", categories)

    # LAST-MODIFIED
    updated = dossier.get("updated_at")
    if updated:
        journal.add("last-modified", updated)

    # SEQUENCE (use etag change count — just use 0 for now)
    journal.add("sequence", 0)

    # Custom properties for round-trip fidelity
    if dossier.get("file_number"):
        journal.add("x-pallas-file-number", dossier["file_number"])
    for client in dossier.get("clients", []):
        journal.add("x-pallas-client-id", client["id"])
    if dossier.get("court_file_number"):
        journal.add("x-pallas-court-file", dossier["court_file_number"])
    if dossier.get("prescription_date") and hasattr(
        dossier["prescription_date"], "date"
    ):
        journal.add(
            "x-pallas-prescription",
            dossier["prescription_date"].date().isoformat(),
        )

    cal.add_component(journal)
    return cal.to_ical().decode("utf-8")


def vjournal_to_dossier(ical_str: str) -> dict:
    """Parse an RFC-5545 VJOURNAL string into a dossier dict (for DAV PUT)."""
    cal = icalendar.Calendar.from_ical(ical_str)
    data: dict = {}

    for component in cal.walk():
        if component.name != "VJOURNAL":
            continue

        # UID
        uid = component.get("uid")
        if uid:
            data["vjournal_uid"] = str(uid)

        # SUMMARY → title
        summary = component.get("summary")
        if summary:
            summary_str = str(summary)
            # Try to split "file_number — title"
            if " — " in summary_str:
                parts = summary_str.split(" — ", 1)
                data["file_number"] = parts[0].strip()
                data["title"] = parts[1].strip()
            else:
                data["title"] = summary_str

        # DESCRIPTION → notes
        desc = component.get("description")
        if desc:
            data["notes"] = str(desc)

        # STATUS
        status = component.get("status")
        if status:
            status_str = str(status).upper()
            reverse_map = {
                "FINAL": "actif",
                "DRAFT": "en_attente",
                "CANCELLED": "fermé",
            }
            data["status"] = reverse_map.get(status_str, "actif")

        # DTSTART → opened_date
        dtstart = component.get("dtstart")
        if dtstart:
            dt = dtstart.dt
            if hasattr(dt, "hour"):
                data["opened_date"] = dt
            else:
                data["opened_date"] = datetime.combine(
                    dt, datetime.min.time(), tzinfo=timezone.utc
                )

        # Custom X- properties
        file_num = component.get("x-pallas-file-number")
        if file_num:
            data["file_number"] = str(file_num)

        # Collect all x-pallas-client-id values
        client_ids = []
        for line in component.property_items():
            if line[0].upper() == "X-PALLAS-CLIENT-ID":
                client_ids.append(str(line[1]))
        if client_ids:
            data["client_ids"] = client_ids

        court_file = component.get("x-pallas-court-file")
        if court_file:
            data["court_file_number"] = str(court_file)

        break  # Only process first VJOURNAL

    return data
