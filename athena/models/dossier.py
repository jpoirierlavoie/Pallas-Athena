"""Dossier (case file) Firestore CRUD and RFC-5545 VJOURNAL serialization."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from models import aggregation_values, db, reference
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils import taxonomie
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils.recours import (
    VALID_PRESCRIPTION_TYPES,
    compute_date_pour_agir,
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
    "transactionnel",
    "consultation",
    "autre",
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
VALID_ROLES = (
    "demandeur",
    "défendeur",
    "intervenant",
    "mis en cause",
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
    "judiciaire": "Judiciaire",
    "transactionnel": "Transactionnel",
    "consultation": "Consultatif",
    "autre": "Autre",
}
FORUM_TYPE_LABELS = {
    "judiciaire": "Tribunal de droit commun",
    "administratif": "Tribunal administratif",
    "federal": "Cour ou tribunal fédéral",
    "prejudiciaire": "Préjudiciaire",
}
# Retired type-de-mandat keys → current vocabulary, applied on read
# (_migrate_mandate_type). "mediation_arbitrage" was dropped July 2026 and has
# no clean equivalent, so it falls back to "autre"; the user re-classifies it
# via the edit form. Without this, editing such a dossier would trip
# _validate's mandate_type check.
_MANDATE_TYPE_MIGRATION = {
    "mediation_arbitrage": "autre",
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
COURT_LABELS = {c: c for c in VALID_COURTS}
STATUS_LABELS = {
    "actif": "Actif",
    "en_attente": "En attente",
    "fermé": "Fermé",
    "archivé": "Archivé",
}
ROLE_LABELS = {
    "demandeur": "Demandeur",
    "défendeur": "Défendeur",
    "intervenant": "Intervenant",
    "mis en cause": "Mis en cause",
    "autre": "Autre",
}
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
        # Role of the lawyer's client
        "role": "demandeur",
        # Financial
        "hourly_rate": 25000,
        "flat_fee": None,
        "contingency_percent": None,          # basis points: 2500 = 25,00 %
        "fee_type": "hourly",
        "fee_notes": "",
        # Status
        "status": "actif",
        "opened_date": None,
        "closed_date": None,
        # Recours & prescription
        "action": "",
        "action_precision": "",
        "valeur": None,
        "prescription_type": "",
        "droit_action_date": None,
        "prescription_date": None,
        "prescription_notes": "",
        # Metadata
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


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre du dossier est requis.")

    if not data.get("clients"):
        errors.append("Au moins un client doit être associé au dossier.")

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


def _apply_prescription_deadline(doc: dict) -> None:
    """Recompute ``prescription_date`` (the "date pour agir") in place.

    The limitation deadline is derived from the recourse fields:
    ``droit_action_date`` + the ``prescription_type`` period. An imprescriptible
    recourse clears it. When the type/start date don't drive a computation
    (unset, or "autre"), any existing/legacy ``prescription_date`` is left
    untouched — so older dossiers carrying a manually-set date are never wiped.
    """
    p_type = doc.get("prescription_type", "")
    if p_type == "imprescriptible":
        doc["prescription_date"] = None
    elif doc.get("droit_action_date") and prescription_period(p_type):
        doc["prescription_date"] = compute_date_pour_agir(
            doc.get("droit_action_date"), p_type
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


def create_dossier(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
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
    etag = str(uuid.uuid4())
    vjournal_uid = str(uuid.uuid4())

    # Ensure flat ID arrays are in sync with the object arrays
    merged["client_ids"] = [c["id"] for c in merged.get("clients", [])]
    merged["opposing_party_ids"] = [p["id"] for p in merged.get("opposing_parties", [])]

    merged.update(
        {
            "id": dossier_id,
            "opened_date": merged.get("opened_date") or now,
            "created_at": now,
            "updated_at": now,
            "etag": etag,
            "vjournal_uid": vjournal_uid,
            "dav_href": f"/dav/journals/{dossier_id}.ics",
        }
    )

    # Closure date mirrors update_dossier: auto-stamp when a dossier is created
    # already closed/archived (unless the form supplied one); empty otherwise.
    if merged.get("status") in ("fermé", "archivé"):
        merged["closed_date"] = merged.get("closed_date") or now
    else:
        merged["closed_date"] = None

    # Derive the prescription deadline ("date pour agir") from the recourse fields.
    _apply_prescription_deadline(merged)

    try:
        db.collection(COLLECTION).document(dossier_id).set(merged)
    except Exception:
        log_unexpected("dossier write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


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
    _migrate_domaine(doc)
    _migrate_mandate_type(doc)
    _migrate_forum_type(doc)
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
            # Default: opened_date, newest first
            results.sort(
                key=lambda d: d.get("opened_date")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )

        return results
    except Exception:
        return []


def list_dossiers_page(
    status_filter: Optional[str] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
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
    try:
        query = db.collection(COLLECTION)
        if status_filter and status_filter in VALID_STATUSES:
            query = query.where(filter=FieldFilter("status", "==", status_filter))
        query = query.order_by(
            "opened_date", direction=firestore.Query.DESCENDING
        ).order_by("id", direction=firestore.Query.DESCENDING)

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
    dossier_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing dossier. Returns (updated_doc, errors)."""
    existing = get_dossier(dossier_id)
    if not existing:
        return None, ["Dossier introuvable."]

    merged = {**existing, **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors

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
                    return None, ["Ce numéro de dossier existe déjà."]
        except Exception as exc:
            logger.warning(
                "update_dossier: duplicate-check query failed for %s: %s",
                sanitize_log_value(dossier_id), exc,
            )

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    # Sync flat ID arrays
    merged["client_ids"] = [c["id"] for c in merged.get("clients", [])]
    merged["opposing_party_ids"] = [p["id"] for p in merged.get("opposing_parties", [])]

    # Closure date: auto-determined when the dossier is closed/archived, but
    # user-editable. Respect a date supplied on the form; otherwise keep the
    # existing one, falling back to `now` on the closing transition. An
    # active/pending dossier is never closed, so it carries no closure date.
    if merged.get("status") in ("fermé", "archivé"):
        if not merged.get("closed_date"):
            merged["closed_date"] = existing.get("closed_date") or now
    else:
        merged["closed_date"] = None

    # Derive the prescription deadline ("date pour agir") from the recourse fields.
    _apply_prescription_deadline(merged)

    try:
        db.collection(COLLECTION).document(dossier_id).set(merged)
    except Exception:
        log_unexpected("dossier write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]

    return merged, []


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


def list_prescription_alerts(cutoff: datetime, limit: int = 50) -> list[dict]:
    """Return active dossiers with a prescription date on or before *cutoff*.

    Both filters run server-side (``status == actif`` AND
    ``prescription_date <= cutoff``), ordered by prescription_date ascending
    and bounded — requires the ``dossiers`` composite index
    (status ASC, prescription_date ASC); see ``firestore.indexes.json``.
    Dossiers without a prescription date are excluded automatically: Firestore
    range filters never match null/missing values, matching the previous
    Python-side behaviour. Legacy party fields are migrated on read, like
    every other dossier read path.

    Returns [] on failure (the dashboard degrades gracefully).
    """
    try:
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("status", "==", "actif"))
            .where(filter=FieldFilter("prescription_date", "<=", cutoff))
            .order_by("prescription_date")
            .limit(limit)
        )
        alerts = [_migrate_parties(doc.to_dict()) for doc in query.stream()]
        if len(alerts) >= limit:
            # Prescription deadlines must never be silently truncated.
            logger.warning(
                "list_prescription_alerts: result window full (limit=%d) — "
                "some alerts may be hidden", limit,
            )
        return alerts
    except Exception as exc:
        logger.warning("list_prescription_alerts: query failed: %s", exc)
        return []


def count_dossiers_for_partie(partie_id: str) -> int:
    """Count how many dossiers reference a given partie (as client or opposing).

    Returns 0 on query failure — display-only callers degrade gracefully.
    Safety checks must use :func:`count_dossiers_for_partie_strict`.
    """
    try:
        return count_dossiers_for_partie_strict(partie_id)
    except Exception:
        return 0


def count_dossiers_for_partie_strict(partie_id: str) -> int:
    """Like :func:`count_dossiers_for_partie` but propagates query errors.

    Used by FK safety checks (e.g. partie deletion) that must fail CLOSED
    when the count cannot be established.
    """
    q1 = db.collection(COLLECTION).where(filter=FieldFilter("client_ids", "array_contains", partie_id))
    q2 = db.collection(COLLECTION).where(filter=FieldFilter("opposing_party_ids", "array_contains", partie_id))
    ids = {doc.id for doc in q1.stream()} | {doc.id for doc in q2.stream()}
    return len(ids)


def list_dossiers_for_partie(partie_id: str) -> list[dict]:
    """Return all dossiers linked to a partie, newest first."""
    try:
        q1 = db.collection(COLLECTION).where(filter=FieldFilter("client_ids", "array_contains", partie_id))
        q2 = db.collection(COLLECTION).where(filter=FieldFilter("opposing_party_ids", "array_contains", partie_id))
        seen: set[str] = set()
        results: list[dict] = []
        for doc in q1.stream():
            d = _migrate_parties(doc.to_dict())
            if d.get("id") not in seen:
                seen.add(d["id"])
                results.append(d)
        for doc in q2.stream():
            d = _migrate_parties(doc.to_dict())
            if d.get("id") not in seen:
                seen.add(d["id"])
                results.append(d)
        results.sort(
            key=lambda d: d.get("opened_date") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return results
    except Exception:
        return []


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
