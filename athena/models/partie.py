"""Partie (contact/party) Firestore CRUD and vCard 4.0 serialization."""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

import vobject

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import AlreadyExists
from models import aggregation_values, concurrency, dav_ids, db, provenance
from pagination import PAGE_SIZE, decode_cursor, encode_cursor
from security import sanitize
from utils import kyc
from utils.logging_setup import log_unexpected, sanitize_log_value
from utils.validators import (
    apply_address_defaults,
    normalize_email,
    normalize_phone,
    normalize_postal_code,
    validate_email,
    validate_phone,
    validate_postal_code,
)

logger = logging.getLogger(__name__)

# Firestore collection path (nested under a single-user root)
COLLECTION = "parties"

# Valid values for enum fields
VALID_TYPES = ("individual", "organization")
VALID_CONTACT_ROLES = (
    "client",
    "partie_adverse",
    "avocat_adverse",
    "témoin",
    "expert",
    "huissier",
    "notaire",
    "autre",
)
VALID_PREFIXES = ("Me", "M.", "Mme", "")
VALID_LANGUAGES = ("fr", "en", "es", "")
VALID_GENDERS = ("M", "F", "O", "N", "U", "")
VALID_PRONOUNS = (
    "il/lui",
    "elle",
    "iel",
    "he/him",
    "she/her",
    "they/them",
    "",
)
# The two compliance checks — vocabulary and provenance rules live in the
# pure utils/kyc.py (D7, lot 4a), shared with mcp/coverage.py.
VALID_IDENTITY_STATUSES = kyc.IDENTITY_STATUSES
VALID_CONFLICT_STATUSES = kyc.CONFLICT_STATUSES

# Contact-role display labels (French)
ROLE_LABELS = {
    "client": "Client",
    "partie_adverse": "Partie",
    "avocat_adverse": "Avocat(e)",
    "témoin": "Témoin",
    "expert": "Expert(e)",
    "huissier": "Huissier(ère)",
    "notaire": "Notaire",
    "autre": "Autre",
}

# The length of one representation's notes — the cap every other string of
# this model gets from _sanitize_data, which never reached into the list.
MANDATAIRE_NOTES_MAX = 2000

# Mandataire / représentation kinds
MANDATAIRE_KIND_LABELS = {
    "mandataire": "Mandataire",
    "tuteur": "Tuteur(trice)",
    "curateur": "Curateur(trice)",
    "représentant_légal": "Représentant(e) légal(e)",
    "autre": "Autre",
}


def _default_doc() -> dict:
    """Return a dict with every partie field set to its default value."""
    return {
        "id": "",
        "type": "individual",
        "contact_role": "client",
        # Individual
        "first_name": "",
        "last_name": "",
        "prefix": "",
        # Date de naissance — DATE SEULE, stockée à minuit UTC (convention
        # dossier.opened_date). Ne JAMAIS la rendre via to_mtl : la conversion
        # vers Montréal la reculerait d'un jour.
        "birth_date": None,
        # Organization (personne morale)
        "organization_name": "",   # Legal name (nom légal)
        "trade_name": "",          # Trade / "doing business as" name (nom d'emprunt)
        "governing_law": "",       # Constituting statute (loi constitutive)
        # Demographics
        "language": "",
        "gender": "",
        "pronouns": "",
        # Professional coordinates
        "job_title": "",
        "job_role": "",
        "organization": "",
        # Personal contact
        "email": "",
        "phone_home": "",
        "phone_cell": "",
        # Professional contact
        "email_work": "",
        "phone_work": "",
        "fax": "",
        # Personal address
        "address_street": "",
        "address_unit": "",
        "address_city": "",
        "address_province": "Québec",
        "address_postal_code": "",
        "address_country": "Canada",
        # Work address
        "work_address_street": "",
        "work_address_unit": "",
        "work_address_city": "",
        "work_address_province": "",
        "work_address_postal_code": "",
        "work_address_country": "Canada",
        # Legal identifiers
        "bar_number": "",
        "company_neq": "",
        # KYC / Compliance. *_date is when the CURRENT decided status was
        # inscribed; *_source who inscribed it ("juriste" | "mcp"; "" = the
        # lawyer, every record before lot 4a); *_confirmed_* the lawyer's
        # « Confirmer » of a Claude inscription (D7, utils/kyc.py). All
        # model-owned: popped from every caller payload by _normalize.
        "identity_verified": "non_vérifié",
        "identity_verified_date": None,
        "identity_verified_notes": "",
        "identity_verified_source": "",
        "identity_verified_confirmed_at": None,
        "identity_verified_confirmed_by": "",
        "kyc_document_ids": [],
        "conflict_check": "non_vérifié",
        "conflict_check_date": None,
        "conflict_check_notes": "",
        "conflict_check_source": "",
        "conflict_check_confirmed_at": None,
        "conflict_check_confirmed_by": "",
        # Mandataires (list of {"id", "kind", "notes"})
        "mandataires": [],
        # Notes
        "notes": "",
        # Metadata (set by create/update)
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
        "vcard_uid": "",
        "dav_href": "",
    }


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _coerce_birth_date(brut) -> Optional[datetime]:
    """Coerce a birth date to midnight UTC, or None when unusable.

    Accepts a datetime, a date, or an « AAAA-MM-JJ » string (the form, the
    vCard BDAY parser and the portal all speak that shape). The result is a
    DATE-ONLY value pinned at midnight UTC — same convention as
    ``dossier.opened_date``, which is why it must never be rendered through
    ``to_mtl`` (Montréal would move it to the previous day).
    """
    if isinstance(brut, datetime):
        return datetime(brut.year, brut.month, brut.day, tzinfo=timezone.utc)
    if isinstance(brut, date):
        return datetime(brut.year, brut.month, brut.day, tzinfo=timezone.utc)
    if isinstance(brut, str):
        texte = brut.strip()
        if not texte:
            return None
        try:
            jour = date.fromisoformat(texte[:10])
        except ValueError:
            return None
        return datetime(jour.year, jour.month, jour.day, tzinfo=timezone.utc)
    return None


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []
    client_type = data.get("type", "individual")

    if client_type == "individual":
        if not data.get("last_name", "").strip():
            errors.append("Le nom de famille est requis.")
    elif client_type == "organization":
        if not data.get("organization_name", "").strip():
            errors.append("Le nom légal de la personne morale est requis.")
    else:
        errors.append("Type de contact invalide.")

    if data.get("contact_role", "") not in VALID_CONTACT_ROLES:
        errors.append("Rôle de contact invalide.")

    # Date de naissance — normalisée à minuit UTC (date seule) et jamais dans
    # le futur. Une chaîne « AAAA-MM-JJ » est acceptée : le formulaire, le DAV
    # (BDAY) et le portail la fournissent tous sous cette forme.
    brut_naissance = data.get("birth_date")
    if brut_naissance not in (None, ""):
        normalisee = _coerce_birth_date(brut_naissance)
        if normalisee is None:
            errors.append("Date de naissance invalide.")
        elif normalisee > datetime.now(timezone.utc):
            errors.append("La date de naissance ne peut pas être dans le futur.")
        else:
            data["birth_date"] = normalisee
    elif brut_naissance == "":
        data["birth_date"] = None

    # Phone validation
    for field, label in [
        ("phone_home", "Téléphone domicile"),
        ("phone_cell", "Cellulaire"),
        ("phone_work", "Téléphone professionnel"),
        ("fax", "Télécopieur"),
    ]:
        raw = data.get(field, "").strip()
        if raw:
            normalized, err = validate_phone(raw)
            if err:
                errors.append(f"{label} : {err}")
            else:
                data[field] = normalized  # store normalized value

    # Email validation
    for field, label in [
        ("email", "Courriel"),
        ("email_work", "Courriel professionnel"),
    ]:
        raw = data.get(field, "").strip()
        if raw:
            normalized, err = validate_email(raw)
            if err:
                errors.append(f"{label} : {err}")
            else:
                data[field] = normalized

    # Postal code validation
    for prefix in ("address", "work_address"):
        country = data.get(f"{prefix}_country", "CA")
        raw_pc = data.get(f"{prefix}_postal_code", "").strip()
        if raw_pc:
            normalized, err = validate_postal_code(raw_pc, country)
            if err:
                errors.append(f"Code postal ({prefix.replace('_', ' ')}) : {err}")
            else:
                data[f"{prefix}_postal_code"] = normalized

    # Mandataires (list of representations) validation
    mandataires = data.get("mandataires") or []
    if not isinstance(mandataires, list):
        errors.append("Format des mandataires invalide.")
        mandataires = []

    own_id = data.get("id", "")
    seen_ids: set[str] = set()
    for idx, entry in enumerate(mandataires, start=1):
        if not isinstance(entry, dict):
            errors.append(f"Mandataire #{idx} : format invalide.")
            continue
        mid = (entry.get("id") or "").strip()
        kind = (entry.get("kind") or "").strip()
        if not mid:
            errors.append(f"Mandataire #{idx} : aucun contact sélectionné.")
            continue
        if own_id and mid == own_id:
            errors.append("Un contact ne peut pas être son propre mandataire.")
            continue
        if mid in seen_ids:
            errors.append(f"Mandataire #{idx} : doublon.")
            continue
        seen_ids.add(mid)
        target = get_partie(mid)
        target_label = display_name(target) if target else f"#{idx}"
        if not target:
            errors.append(f"Mandataire #{idx} : contact introuvable.")
        else:
            if target.get("type") != "individual":
                errors.append(
                    f"Mandataire « {target_label} » : "
                    "doit être une personne physique."
                )
            elif target.get("contact_role") != data.get("contact_role"):
                errors.append(
                    f"Mandataire « {target_label} » : "
                    "doit avoir le même rôle que la partie représentée."
                )
        if kind not in MANDATAIRE_KIND_LABELS:
            errors.append(
                f"Mandataire « {target_label} » : "
                "type de représentation invalide."
            )

    return errors


def _normalize(data: dict) -> dict:
    """Normalize input data before validation."""
    # Apply address defaults
    for prefix in ("address", "work_address"):
        data = apply_address_defaults(data, prefix)

    # Normalize phones
    for field in ("phone_home", "phone_cell", "phone_work", "fax"):
        raw = data.get(field, "").strip()
        if raw:
            normalized = normalize_phone(raw)
            if normalized:
                data[field] = normalized

    # Normalize emails
    for field in ("email", "email_work"):
        raw = data.get(field, "").strip()
        if raw:
            normalized = normalize_email(raw)
            if normalized:
                data[field] = normalized

    # Normalize postal codes
    for prefix in ("address", "work_address"):
        country = data.get(f"{prefix}_country", "CA")
        raw_pc = data.get(f"{prefix}_postal_code", "").strip()
        if raw_pc:
            normalized = normalize_postal_code(raw_pc, country)
            if normalized:
                data[f"{prefix}_postal_code"] = normalized

    # Sanitize the mandataires list: drop empties, dedupe by id, coerce types.
    # ONLY when the caller actually supplied the key. Writing it unconditionally
    # made every PARTIAL update destructive: update_partie merges
    # {**existing, **data}, so an injected empty list overwrote the stored
    # mandataires. update_kyc_status and link_kyc_document already pass partial
    # dicts, and the portal's field-by-field apply (L3) is partial by design.
    if "mandataires" in data:
        raw_list = data.get("mandataires") or []
        if not isinstance(raw_list, list):
            raw_list = []
        cleaned: list[dict] = []
        seen: set[str] = set()
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            cleaned.append({
                "id": mid,
                "kind": str(entry.get("kind") or "").strip(),
                # _sanitize_data sanitizes TOP-LEVEL strings only: an
                # entry's notes used to be stored with no tag strip and no
                # length cap (lot 4a, step 2).
                "notes": sanitize(
                    str(entry.get("notes") or "").strip(),
                    max_length=MANDATAIRE_NOTES_MAX,
                ),
            })
        data["mandataires"] = cleaned

    # Drop any legacy single-mandataire fields so they don't get re-saved.
    for legacy_key in ("mandataire_id", "mandataire_kind", "mandataire_notes"):
        data.pop(legacy_key, None)

    # The compliance provenance is the MODEL's to stamp (D7): a date, a
    # source or a confirmation arriving in a payload — the web form, a
    # CardDAV PUT, the connector — is dropped, never stored. Otherwise a
    # caller could forge « confirmé par le juriste » onto a Claude write.
    for key in kyc.PROVENANCE_KEYS:
        data.pop(key, None)

    return data


def display_name(partie: dict) -> str:
    """Compute a human-readable display name."""
    if partie.get("type") == "organization":
        return partie.get("organization_name", "")
    parts = [
        partie.get("prefix", ""),
        partie.get("first_name", ""),
        partie.get("last_name", ""),
    ]
    return " ".join(p for p in parts if p).strip()


def _migrate_mandataires(partie: Optional[dict]) -> Optional[dict]:
    """Translate legacy single-mandataire fields into the new list format.

    Older docs stored a single mandataire as `mandataire_id` /
    `mandataire_kind` / `mandataire_notes`. The current schema uses a
    `mandataires` list of `{id, kind, notes}`. This helper rewrites
    on read; the legacy keys are popped from the in-memory dict so
    they aren't re-saved on the next `set()` (which is a full replace).
    """
    if not isinstance(partie, dict):
        return partie
    if not partie.get("mandataires"):
        legacy_id = (partie.get("mandataire_id") or "").strip()
        if legacy_id:
            partie["mandataires"] = [{
                "id": legacy_id,
                "kind": partie.get("mandataire_kind") or "mandataire",
                "notes": partie.get("mandataire_notes") or "",
            }]
        else:
            partie["mandataires"] = []
    for legacy_key in ("mandataire_id", "mandataire_kind", "mandataire_notes"):
        partie.pop(legacy_key, None)
    return partie


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_partie(
    data: dict,
    *,
    dav_id: Optional[str] = None,
    dav_uid: Optional[str] = None,
    kyc_source: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors).

    The id and the vCard UID are minted here — an ``id`` or ``vcard_uid``
    inside *data* is DISCARDED, whoever sends it: honouring a forwarded
    ``id`` would let a caller pick (and, through ``set()``, overwrite) an
    existing contact. The web form, Réception and the connector all pass
    dicts.

    ``dav_id`` / ``dav_uid`` (keyword-only) serve ONE caller, the CardDAV
    PUT create branch. A CardDAV client names the new resource in its URL
    and carries its own UID; minting fresh ones stored the contact under an
    id the phone never learns — every later GET/PUT of its href 404'd and a
    duplicate with another UID synced down (lot 0b, 2026-09-27; the
    ``create_task`` fix, lot 0b B3, for contacts). With ``dav_id`` the
    document is written with ``create()``, never ``set()``: CardDAV reaches
    this branch because its read found nothing, and that read FAILS OPEN (a
    read error reads as « absent »), so a contact already stored under that
    id is refused (``[DAV_ID_TAKEN]``) instead of silently overwritten. An
    unusable name returns ``[DAV_ID_INVALID]``. ``dav_uid`` is kept only
    when it can be stored verbatim (``dav_ids.client_uid``), and is ignored
    without ``dav_id``. This is the documented Rule-6 exception for
    CardDAV-created contacts: their document id is the client's name.

    ``kyc_source`` (keyword-only, NO default that names anyone): a contact
    created with a DECIDED identity or conflict status needs it — the date
    is stamped now and the source recorded (``utils.kyc``); without it such
    a creation is refused. The web form passes ``"juriste"``; Réception,
    CardDAV and the connector never carry a decided status.
    """
    data.pop("id", None)
    data.pop("vcard_uid", None)
    if dav_id is not None and not dav_ids.valid_resource_id(dav_id):
        return None, [dav_ids.DAV_ID_INVALID]

    data = _normalize(data)
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    for field in kyc.FIELDS:
        refusal = kyc.apply_status_transition(
            merged, None, data, field, source=kyc_source, now=now)
        if refusal:
            return None, [refusal]
    partie_id = dav_id if dav_id is not None else str(uuid.uuid4())
    vcard_uid = (
        (dav_ids.client_uid(dav_uid) if dav_id is not None else None)
        or str(uuid.uuid4())
    )

    merged.update(
        {
            "id": partie_id,
            "vcard_uid": vcard_uid,
            "dav_href": f"/dav/addressbook/{partie_id}.vcf",
        }
    )
    provenance.stamp_create(merged, now)

    try:
        ref = db.collection(COLLECTION).document(partie_id)
        if dav_id is not None:
            ref.create(merged)
        else:
            ref.set(merged)
    except AlreadyExists:
        return None, [dav_ids.DAV_ID_TAKEN]
    except Exception:
        log_unexpected("partie write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, partie_id)

    return merged, []


def get_parties_bulk(partie_ids: list[str]) -> dict[str, dict]:
    """Fetch many contacts in ONE round-trip. Returns {id: doc} for ids that
    exist — a missing id is simply absent, which is itself information.

    Mirrors ``models.dossier.get_dossiers_bulk``. Document-ID lookups need no
    index. Added for the MCP coverage report, whose two deontological checks
    (conflict of interest, identity verification) read a dossier's clients:
    the alternative — ``list_parties(role_filter="client")`` — SILENTLY
    UNDER-REPORTS, because ``contact_role`` belongs to the CONTACT, not to
    the dossier link, so a client recorded under any other role vanishes from
    the check. Under-reporting in silence on a regulatory obligation is the
    worst failure available here.

    Fails open to ``{}`` like its dossier twin; callers must treat an empty
    result as « could not read », never as « none of them exist ».
    """
    unique_ids = [p for p in dict.fromkeys(partie_ids) if p]
    if not unique_ids:
        return {}
    try:
        refs = [db.collection(COLLECTION).document(pid) for pid in unique_ids]
        result: dict[str, dict] = {}
        for snap in db.get_all(refs):
            if snap.exists:
                result[snap.id] = _migrate_mandataires(snap.to_dict())
        return result
    except Exception as exc:
        logger.warning("get_parties_bulk failed: %s", exc)
        return {}


def get_partie(partie_id: str) -> Optional[dict]:
    """Fetch a single partie by ID."""
    try:
        doc = db.collection(COLLECTION).document(partie_id).get()
        if doc.exists:
            return _migrate_mandataires(doc.to_dict())
    except Exception as exc:
        logger.warning("get_partie failed for %s: %s", sanitize_log_value(partie_id), exc)
    return None


def get_partie_strict(partie_id: str) -> Optional[dict]:
    """The contact, ``None`` when it does not exist — and RAISES on a read
    error, unlike :func:`get_partie`, which swallows it into ``None``.

    For a caller whose ``None`` means something — the connector's contact
    writes of lot 4b, whose « Contact introuvable » would otherwise answer an
    outage and send the caller hunting for (or re-creating) a contact that
    exists. Same shape as ``get_partie`` (mandataires migrated); the
    ``dossier.get_dossier_strict`` precedent."""
    doc = db.collection(COLLECTION).document(partie_id).get()
    if not doc.exists:
        return None
    return _migrate_mandataires(doc.to_dict() or {})


def list_parties_strict() -> list[dict]:
    """Every contact — the CardDAV address book — a read failure PROPAGATES.

    The address book's member listing (PROPFIND Depth:1, sync-collection,
    addressbook-query) used :func:`list_parties`, which answers a blip with
    ``[]``: an EMPTY 207 the client reads as « every contact is gone »
    (a PROPFIND-based sync deletes each local card not listed). The caller
    answers 503 + ``Retry-After`` instead. Unordered.
    """
    return [doc.to_dict() for doc in db.collection(COLLECTION).stream()]


def list_parties(
    type_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict]:
    """Return clients, optionally filtered by type, role, or search term."""
    try:
        query = db.collection(COLLECTION)

        if type_filter and type_filter in VALID_TYPES:
            query = query.where(filter=FieldFilter("type", "==", type_filter))

        if role_filter and role_filter in VALID_CONTACT_ROLES:
            query = query.where(filter=FieldFilter("contact_role", "==", role_filter))

        results = [doc.to_dict() for doc in query.stream()]

        # Sort in Python to avoid requiring Firestore composite indexes
        results.sort(
            key=lambda c: c.get("updated_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            reverse=True,
        )

        # Client-side search filtering (Firestore doesn't support full-text)
        if search:
            term = search.lower()
            filtered = []
            for c in results:
                searchable = " ".join(
                    [
                        c.get("first_name", ""),
                        c.get("last_name", ""),
                        c.get("organization_name", ""),
                        c.get("trade_name", ""),
                        c.get("email", ""),
                        c.get("phone_cell", ""),
                        c.get("phone_home", ""),
                        c.get("phone_work", ""),
                    ]
                ).lower()
                if term in searchable:
                    filtered.append(c)
            results = filtered

        return results
    except Exception:
        return []


def index_by_email_strict() -> dict[str, dict]:
    """``{lower-cased address: partie}`` over ``email`` and ``email_work``.

    A STRICT read: errors propagate (the ``list_mandataire_referrers``
    precedent — one stream of the whole collection, single-user dataset).
    Written for the Bookings rendez-vous (``services/rendez_vous.py``):
    the card Réception renders names the contact whose address matches the
    requester's EXACTLY, and confirming links that same contact — computed
    here, server-side, never taken from a caller. A fail-open read
    (``list_parties`` swallows errors into ``[]``) would have read an
    outage as « aucune partie » and offered the onboarding form to an
    existing client.

    Precedence, when two contacts carry the same address: the MOST
    RECENTLY UPDATED wins (ties keep the stream's id order — Python's sort
    is stable under ``reverse``), and within one contact the personal
    address before the professional one. It is exactly what Réception's
    scan over ``list_parties()`` always did, so moving the scan here
    changed no card. Addresses are matched lower-cased and stripped on
    both sides: a legacy contact saved before normalization still matches.
    """
    parties = []
    for doc in db.collection(COLLECTION).stream():
        data = doc.to_dict()
        if data:
            data.setdefault("id", doc.id)
            parties.append(data)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    parties.sort(key=lambda p: p.get("updated_at") or floor, reverse=True)
    index: dict[str, dict] = {}
    for p in parties:
        for key in ("email", "email_work"):
            value = str(p.get(key) or "").strip().lower()
            if value:
                index.setdefault(value, p)
    return index


def _page_query(role_filter: Optional[str] = None) -> "firestore.Query":
    """The filtered, (updated_at DESC, id ASC)-ordered parties query.

    ONE builder shared by :func:`list_parties_page` and
    :func:`count_parties_page` — see the note in :func:`count_parties_page`.
    """
    query = db.collection(COLLECTION)
    if role_filter and role_filter in VALID_CONTACT_ROLES:
        query = query.where(filter=FieldFilter("contact_role", "==", role_filter))
    return query.order_by(
        "updated_at", direction=firestore.Query.DESCENDING
    ).order_by("id")


def count_parties_page(role_filter: Optional[str] = None) -> Optional[int]:
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
        values = aggregation_values(_page_query(role_filter).count(alias="n").get())
        n = values.get("n")
        return int(n) if n is not None else None
    except Exception as exc:
        logger.warning("count_parties_page: aggregation failed: %s", exc)
        return None


def list_parties_page(
    role_filter: Optional[str] = None,
    limit: int = PAGE_SIZE,
    cursor: Optional[str] = None,
    offset: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Return one page of parties plus an opaque cursor for the next page.

    Cursor-mode counterpart of :func:`list_parties` for the list view:
    reads ~``limit`` documents per page (server-side ``order_by`` +
    ``start_after``) instead of streaming the whole collection. The legacy
    :func:`list_parties` remains the path for search and exports.

    Sort order: ``updated_at DESC, id ASC``. The ``id`` field mirrors the
    document ID and is always set — a stable tiebreaker when several docs
    share the same ``updated_at``. Requires the composite indexes
    ``(updated_at DESC, id ASC)`` and
    ``(contact_role ASC, updated_at DESC, id ASC)``.
    """
    # Before the try: a caller naming a position TWICE is a programming error,
    # and swallowing it into ([], None) would render an empty list with no
    # explanation anywhere.
    if offset and cursor:
        raise ValueError("cursor et offset s'excluent — chacun nomme une position")
    try:
        query = _page_query(role_filter)
        if offset:
            # An ABSOLUTE page read, for a « Fin » / « ±N » leap. Same
            # ordering, so the same index — Firestore emits ONE query
            # (order_by -> offset -> limit). It bills the skipped documents,
            # which is why `page` is clamped before it ever reaches here.
            query = query.offset(offset)

        # decode_cursor yields values in encode order: [updated_at, id].
        # Anything malformed (None or wrong arity) degrades to page 1.
        values = decode_cursor(cursor)
        if values and len(values) == 2:
            query = query.start_after(
                {"updated_at": values[0], "id": values[1]}
            )

        # Fetch one extra row to know whether a next page exists.
        docs = [doc.to_dict() for doc in query.limit(limit + 1).stream()]

        next_cursor = None
        if len(docs) > limit:
            docs = docs[:limit]
            last = docs[-1]
            next_cursor = encode_cursor([last.get("updated_at"), last.get("id")])

        return docs, next_cursor
    except Exception as exc:
        # PII-free: log only the exception type, never document contents.
        logger.warning("list_parties_page failed: %s", type(exc).__name__)
        return [], None


def update_partie(
    partie_id: str,
    data: dict,
    *,
    expected_etag: Optional[str] = None,
    kyc_source: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Update an existing partie. Returns (updated_doc, errors).

    ``expected_etag`` (keyword-only): when given, the write commits only if
    the stored etag is still that one — checked in a transaction — and
    returns ``[concurrency.STALE_ETAG_ERROR]`` otherwise, having written
    nothing. ``None`` is the unchanged single ``set()``: the CardDAV PUT,
    a page rendered before its form carried an etag, and the three callers
    that merge a PARTIAL payload onto the record read here — Réception's
    field-by-field apply (``routes/reception``), ``update_kyc_status`` and
    ``link_kyc_document`` — which name only what they change, so a later
    field is never reverted by them.

    Every representation that leaves the ``mandataires`` list — the web
    form posts it whole — is journaled in ``audit_events`` (``mandataire``)
    after the commit (lot 4a).

    ``kyc_source`` (keyword-only; ``"juriste"`` | ``"mcp"``) names who
    decides when the payload CHANGES an identity or conflict status (D7,
    ``utils.kyc.apply_status_transition``). There is deliberately NO
    default naming anyone: a status transition without it is REFUSED
    (fail closed), so an omission can never stamp a Claude write as the
    lawyer's. An unchanged status — the web form re-submitting a presumed
    value — needs no source and changes no provenance. With ``"mcp"`` the
    write is compare-and-set against the version read here even without
    *expected_etag*: the « never over the lawyer's attestation » rule is
    checked on that read, and a blind ``set()`` would revert a decision
    landing in between.
    """
    doc, errors, _journaled = _update_partie(
        partie_id, data, expected_etag, kyc_source)
    return doc, errors


def _update_partie(
    partie_id: str,
    data: dict,
    guard: Optional[str],
    kyc_source: Optional[str] = None,
) -> tuple[Optional[dict], list[str], int]:
    """:func:`update_partie`'s body, plus the number of mandataire detaches
    journaled — which the mandataire helpers report. *guard* is the public
    ``expected_etag``, same contract (``tests/test_concurrency_models.py``
    proves it through the public wrapper).
    """
    expected_etag = guard
    existing = get_partie(partie_id)
    if not existing:
        return None, ["Contact introuvable."], 0
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR], 0
    # A CLAUDE write is compare-and-set against the version read HERE even
    # when the caller asserts none (D7). On the legacy blind set(), a
    # lawyer's decision or « Confirmer » landing between this read and the
    # commit would be REVERTED to Claude's presumed status — the rule
    # « never over the lawyer's attestation » below was checked on a read
    # the write then ignored. The web form (kyc_source « juriste ») keeps
    # its path.
    if expected_etag is None and kyc_source == kyc.SOURCE_MCP:
        expected_etag = concurrency.etag_of(existing)

    data = _normalize(data)
    merged = {**existing, **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors, 0
    errors = _mandataire_fitness_errors(partie_id, existing, merged)
    if errors:
        return None, errors, 0

    now = datetime.now(timezone.utc)
    provenance.stamp_update(merged, now)

    # KYC stamps (PA-D07, D7 — utils/kyc.apply_status_transition): each
    # *_date answers « when was the CURRENT decided status inscribed »,
    # never « when was the field last touched », and only a TRANSITION
    # moves the provenance. « non_vérifié » clears date, source and
    # confirmation, so the invariant date-present ⇔ status-decided
    # self-heals pre-fix rows on their next KYC edit. Presence-gated: a
    # partial update that does not carry the key never touches the check
    # (on a full-document set, injecting a default IS a deletion).
    for field in kyc.FIELDS:
        refusal = kyc.apply_status_transition(
            merged, existing, data, field, source=kyc_source, now=now)
        if refusal:
            return None, [refusal], 0

    try:
        concurrency.commit_document(
            db.collection(COLLECTION).document(partie_id), merged,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR], 0
    except concurrency.Vanished:
        return None, ["Contact introuvable."], 0
    except Exception:
        log_unexpected("partie write failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."], 0
    provenance.note_commit(COLLECTION, partie_id)

    # AFTER the commit, best-effort: a journal failure never fails the save.
    journaled = _journal_mandataire_detaches(partie_id, existing, merged)
    return merged, [], journaled


def _mandataire_entries(partie: Optional[dict]) -> list[dict]:
    """The stored representations, dict entries only (never raises)."""
    return [
        e for e in ((partie or {}).get("mandataires") or [])
        if isinstance(e, dict) and str(e.get("id") or "").strip()
    ]


def _journal_mandataire_detaches(
    partie_id: str, existing: dict, saved: dict
) -> int:
    """One ``audit_events`` row (``mandataire``) per representation that
    LEFT the contact; the count. The mandataire contact itself stays — the
    row says a LINK went: ``entity_id`` is the mandataire, ``title`` the
    represented contact's name, ``status`` the kind. Best-effort, after the
    commit, never raises (``models/audit_event``'s one documented exception
    to « written by the callers »: the web form posts the list WHOLE, so
    only the model sees every detach)."""
    try:
        from models import audit_event  # local: keeps the model graph flat

        after = {str(e["id"]).strip() for e in _mandataire_entries(saved)}
        represented = display_name(saved)
        count = 0
        for entry in _mandataire_entries(existing):
            mid = str(entry["id"]).strip()
            if mid in after:
                continue
            if audit_event.record_deletion(
                "mandataire", mid, title=represented,
                status=str(entry.get("kind") or ""),
            ) is not None:
                count += 1
        return count
    except Exception:
        log_unexpected("mandataire detach journal failed", partie_id=partie_id)
        return 0


# The refusal when the reverse-reference check cannot be established. A
# constant, so a caller can tell « could not verify » apart from a refusal
# that names the represented contacts.
MANDATAIRE_CHECK_UNAVAILABLE = (
    "Impossible de vérifier si ce contact représente un autre contact : "
    "rien n'a été enregistré. Veuillez réessayer."
)


def list_mandataire_referrers(partie_id: str) -> list[dict]:
    """Every OTHER partie that lists *partie_id* as one of its mandataires.

    A STRICT read: errors propagate, so a caller that refuses on the
    strength of the answer (``delete_partie``, the role/type guard of
    ``update_partie``) fails CLOSED instead of reading an outage as « nobody
    is represented ». Streams the whole collection (single-user dataset,
    small) and also honours the legacy single-mandataire field of
    not-yet-migrated documents. An empty id is refused before any read.
    """
    if not isinstance(partie_id, str) or not partie_id.strip():
        raise ValueError("list_mandataire_referrers needs a partie id")
    referrers: list[dict] = []
    for doc in db.collection(COLLECTION).stream():
        other = doc.to_dict()
        if not other or (other.get("id") or doc.id) == partie_id:
            continue
        ids = {
            str(entry.get("id") or "").strip()
            for entry in (other.get("mandataires") or [])
            if isinstance(entry, dict)
        }
        legacy = str(other.get("mandataire_id") or "").strip()
        if legacy:
            ids.add(legacy)
        if partie_id in ids:
            referrers.append(other)
    return referrers


def _find_mandataire_references(partie_id: str) -> list[str]:
    """Return display names of parties listing *partie_id* as a mandataire.

    Fail CLOSED: errors propagate to delete_partie, which refuses the
    deletion when references cannot be established.
    """
    return [
        display_name(other) or other.get("id", "")
        for other in list_mandataire_referrers(partie_id)
    ]


def _joined_names(names: list[str]) -> str:
    """« A, B, C et 2 autres » — the delete_partie wording, shared."""
    shown = ", ".join(names[:3])
    more = len(names) - 3
    if more > 0:
        shown += f" et {more} autre{'s' if more > 1 else ''}"
    return shown


def _mandataire_fitness_errors(
    partie_id: str, existing: dict, merged: dict
) -> list[str]:
    """Refuse a role/type change that unfits this contact as a mandataire.

    ``_validate`` checks the FORWARD rule only — each of MY mandataires must
    be an individual of MY role. Nothing checked the reverse: changing the
    role or the type of a contact that SOMEONE ELSE lists as mandataire left
    that other contact violating the forward rule, so every later edit of
    the REPRESENTED contact was refused — the web form and a CardDAV PUT
    alike (422, which DavX5 swallows) — with nothing pointing at the cause.

    The collection scan runs ONLY on an actual role or type change. It
    refuses only the representations the change BREAKS — a referrer this
    contact fits TODAY (an individual of that referrer's role) and would no
    longer fit. A pair that is ALREADY a legacy mismatch never refuses:
    the change cannot make it worse, and refusing would lock out a repair —
    including a partial one, where one referrer is repaired and another,
    already broken, stays as it was. A failed scan refuses (fail closed)
    with :data:`MANDATAIRE_CHECK_UNAVAILABLE`.
    """
    old_type = existing.get("type")
    old_role = existing.get("contact_role")
    new_type = merged.get("type")
    new_role = merged.get("contact_role")
    if new_type == old_type and new_role == old_role:
        return []
    try:
        referrers = list_mandataire_referrers(partie_id)
    except Exception:
        log_unexpected("partie mandataire reference check failed")
        return [MANDATAIRE_CHECK_UNAVAILABLE]

    def _fits(kind: object, role: object, other: dict) -> bool:
        return kind == "individual" and other.get("contact_role") == role

    broken = [
        other for other in referrers
        if _fits(old_type, old_role, other)
        and not _fits(new_type, new_role, other)
    ]
    if not broken:
        return []
    names = _joined_names(
        [display_name(other) or other.get("id", "") for other in broken]
    )
    return [
        f"Ce contact est mandataire de {names} : il doit rester une personne "
        "physique du même rôle que chaque contact qu'il représente. Retirez "
        "d'abord cette représentation, puis changez son rôle ou son type."
    ]


# ── Mandataires, one representation at a time (lot 4a) ───────────────────
#
# The web form posts the whole ``mandataires`` list; the connector (lot 4b)
# adds, corrects and removes ONE representation. These helpers build the
# new list from the STORED one — every other entry written back as stored,
# or the call refused when sanitation would alter one (_save_mandataires) —
# and save it through update_partie, whose forward rule (_validate) and
# reverse rule (_mandataire_fitness_errors) every path meets. They name the
# refusal first where the forward rule would only say « Mandataire #2 ».
# The caller bumps the ``parties`` CTag when ``report["changed"]`` (the
# etag regenerates although vCard never carries mandataires: skipping the
# bump makes DavX5's next If-Match PUT 412).

MANDATAIRE_NOT_LISTED = (
    "Ce contact n'est pas un mandataire du contact représenté."
)
MANDATAIRE_ALREADY_LISTED = (
    "Ce contact est déjà mandataire du contact représenté, avec un autre "
    "type ou d'autres notes : corrigez cette représentation plutôt que de "
    "l'ajouter une seconde fois."
)
MANDATAIRE_NOTHING_TO_CHANGE = (
    "Rien à modifier : précisez le type de représentation ou les notes."
)


def _clean_mandataire_notes(notes: object) -> tuple[str, Optional[str]]:
    """``(notes, refusal)`` — bounded, and never silently altered.

    The form path lets ``_normalize`` sanitize (the web's convention); a
    one-entry write REFUSES instead, since truncating or stripping the
    caller's text in silence would store something it did not ask for.
    """
    if not isinstance(notes, str):
        return "", "Les notes de la représentation doivent être du texte."
    clean = notes.strip()
    if len(clean) > MANDATAIRE_NOTES_MAX:
        return "", (
            "Les notes de la représentation dépassent "
            f"{MANDATAIRE_NOTES_MAX} caractères : rien n'a été enregistré."
        )
    if sanitize(clean, max_length=MANDATAIRE_NOTES_MAX) != clean:
        return "", (
            "Les notes de la représentation contiennent des chevrons (< >) "
            "qui seraient retirés : reformulez-les. Rien n'a été enregistré."
        )
    return clean, None


def _mandataire_report(partie_id: str, mandataire_id: str) -> dict:
    return {"changed": False, "partie_id": partie_id,
            "mandataire_id": mandataire_id}


def _resolve_pair(
    partie_id: str, mandataire_id: str, guard: Optional[str],
) -> tuple[Optional[dict], Optional[str]]:
    """The represented contact, read and checked against *guard* (the
    public ``expected_etag``) — or a refusal."""
    if not partie_id or not mandataire_id:
        return None, "Le contact représenté et le mandataire sont requis."
    existing = get_partie(partie_id)
    if not existing:
        return None, "Contact introuvable."
    if not concurrency.matches(existing, guard):
        return None, concurrency.STALE_ETAG_ERROR
    return existing, None


def _save_mandataires(
    partie_id: str, existing: dict, entries: list[dict],
    *, named: Optional[str] = None,
) -> tuple[Optional[dict], list[str], int]:
    """Commit a rebuilt list, compare-and-set against the version read.

    Every entry but *named* (the one whose notes the caller supplied, and
    which ``_clean_mandataire_notes`` already vetted) is written back AS
    STORED — and ``_normalize`` sanitizes every entry's notes on the way.
    A legacy note typed before lot 4a (no tag strip, no cap) that
    sanitation would alter is therefore REFUSED here rather than stripped
    in silence: correcting one representation must never rewrite
    another's text behind the caller's back (the web form, which the
    lawyer reads before saving, keeps sanitizing — the web's convention).
    """
    for entry in entries:
        mid = str(entry.get("id") or "").strip()
        if mid == named:
            continue
        stored = str(entry.get("notes") or "").strip()
        if sanitize(stored, max_length=MANDATAIRE_NOTES_MAX) != stored:
            return None, [
                "Les notes enregistrées d'une autre représentation "
                f"(mandataire {mid}) contiennent des chevrons (< >) ou "
                f"dépassent {MANDATAIRE_NOTES_MAX} caractères : les "
                "enregistrer de nouveau les modifierait. Corrigez d'abord "
                "ces notes. Rien n'a été enregistré."
            ], 0
    return _update_partie(
        partie_id, {"mandataires": entries}, concurrency.etag_of(existing)
    )


def add_partie_mandataire(
    partie_id: str,
    mandataire_id: str,
    *,
    kind: str,
    notes: str = "",
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Add ONE representation to a contact — ``(doc, errors, report)``.

    The ``delete_folder`` deviation from ``(doc, errors)``: the caller must
    tell « added » apart from « already so ». Refused, each by name: an
    unknown contact or mandataire, the contact itself, a mandataire that is
    not an individual or not of the represented contact's role, an unknown
    *kind*, notes too long or altered by sanitation, and an entry already
    listed with a DIFFERENT kind or notes (the same one is a no-op success,
    ``report["changed"]`` False, nothing written). Compare-and-set against
    the version read here (after checking *expected_etag* when given).
    """
    partie_id = str(partie_id or "").strip()
    mandataire_id = str(mandataire_id or "").strip()
    report = _mandataire_report(partie_id, mandataire_id)
    existing, refusal = _resolve_pair(partie_id, mandataire_id, expected_etag)
    if refusal:
        return None, [refusal], report
    if mandataire_id == partie_id:
        return None, ["Un contact ne peut pas être son propre mandataire."], report
    if kind not in MANDATAIRE_KIND_LABELS:
        return None, ["Type de représentation invalide."], report
    clean_notes, refusal = _clean_mandataire_notes(notes)
    if refusal:
        return None, [refusal], report
    target = get_partie(mandataire_id)
    if not target:
        return None, ["Mandataire introuvable."], report
    if target.get("type") != "individual":
        return None, ["Un mandataire doit être une personne physique."], report
    if target.get("contact_role") != existing.get("contact_role"):
        return None, [
            "Un mandataire doit avoir le même rôle que le contact représenté."
        ], report

    entries = _mandataire_entries(existing)
    report.update({"kind": kind, "notes": clean_notes,
                   "mandataires_count": len(entries)})
    for entry in entries:
        if str(entry["id"]).strip() != mandataire_id:
            continue
        if (entry.get("kind") == kind
                and str(entry.get("notes") or "") == clean_notes):
            return existing, [], report
        return None, [MANDATAIRE_ALREADY_LISTED], report

    new_entry = {"id": mandataire_id, "kind": kind, "notes": clean_notes}
    saved, errors, _journaled = _save_mandataires(
        partie_id, existing, [dict(e) for e in entries] + [new_entry],
        named=mandataire_id)
    if errors:
        return None, errors, report
    report["changed"] = True
    report["mandataires_count"] = len(_mandataire_entries(saved))
    return saved, [], report


def update_partie_mandataire(
    partie_id: str,
    mandataire_id: str,
    *,
    kind: Optional[str] = None,
    notes: Optional[str] = None,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Correct ONE representation's kind and/or notes — ``(doc, errors,
    report)``. ``None`` leaves either alone; at least one is required. The
    entry must be listed. An unchanged request writes nothing."""
    partie_id = str(partie_id or "").strip()
    mandataire_id = str(mandataire_id or "").strip()
    report = _mandataire_report(partie_id, mandataire_id)
    if kind is None and notes is None:
        return None, [MANDATAIRE_NOTHING_TO_CHANGE], report
    existing, refusal = _resolve_pair(partie_id, mandataire_id, expected_etag)
    if refusal:
        return None, [refusal], report
    if kind is not None and kind not in MANDATAIRE_KIND_LABELS:
        return None, ["Type de représentation invalide."], report
    clean_notes = None
    if notes is not None:
        clean_notes, refusal = _clean_mandataire_notes(notes)
        if refusal:
            return None, [refusal], report

    entries = [dict(e) for e in _mandataire_entries(existing)]
    index = next((i for i, e in enumerate(entries)
                  if str(e["id"]).strip() == mandataire_id), None)
    if index is None:
        return None, [MANDATAIRE_NOT_LISTED], report
    entry = entries[index]
    new_kind = kind if kind is not None else str(entry.get("kind") or "")
    new_notes = (clean_notes if clean_notes is not None
                 else str(entry.get("notes") or ""))
    report.update({"kind": new_kind, "notes": new_notes,
                   "mandataires_count": len(entries)})
    if (new_kind == str(entry.get("kind") or "")
            and new_notes == str(entry.get("notes") or "")):
        return existing, [], report

    entries[index] = {**entry, "kind": new_kind, "notes": new_notes}
    saved, errors, _journaled = _save_mandataires(
        partie_id, existing, entries, named=mandataire_id)
    if errors:
        return None, errors, report
    report["changed"] = True
    return saved, [], report


def remove_partie_mandataire(
    partie_id: str,
    mandataire_id: str,
    *,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str], dict]:
    """Detach ONE representation — the mandataire contact itself stays.

    ``(doc, errors, report)``; refused when the entry is not listed. The
    detach is journaled in ``audit_events`` (``mandataire``) after the
    commit — ``report["journaled"]`` says whether that row was written.
    """
    partie_id = str(partie_id or "").strip()
    mandataire_id = str(mandataire_id or "").strip()
    report = _mandataire_report(partie_id, mandataire_id)
    report["journaled"] = False
    existing, refusal = _resolve_pair(partie_id, mandataire_id, expected_etag)
    if refusal:
        return None, [refusal], report
    entries = [dict(e) for e in _mandataire_entries(existing)]
    kept = [e for e in entries if str(e["id"]).strip() != mandataire_id]
    if len(kept) == len(entries):
        return None, [MANDATAIRE_NOT_LISTED], report
    removed = next(e for e in entries if str(e["id"]).strip() == mandataire_id)
    report.update({"kind": str(removed.get("kind") or ""),
                   "mandataires_count": len(kept)})

    saved, errors, journaled = _save_mandataires(partie_id, existing, kept)
    if errors:
        return None, errors, report
    report["changed"] = True
    report["journaled"] = journaled > 0
    return saved, [], report


# The three refusals of ``delete_partie`` that are NOT a reference
# conflict. Constants, so a caller can map each to its own answer without
# parsing French — the CardDAV DELETE answers 404 / 503 / 500 for these
# and 409 for every other refusal (a dossier link, a represented contact),
# whose text is dynamic and may NAME a contact.
PARTIE_NOT_FOUND = "Contact introuvable."
PARTIE_DELETE_CHECK_UNAVAILABLE = (
    "Impossible de vérifier les références de ce contact. "
    "Veuillez réessayer."
)
PARTIE_DELETE_FAILED = "Erreur lors de la suppression. Veuillez réessayer."


def delete_partie(partie_id: str) -> tuple[bool, str]:
    """Delete a partie. Returns (success, error_message).

    Refuses deletion while the partie is still referenced by a dossier
    (as client or opposing party) or listed as a mandataire by another
    partie — the FK safety check applies to every caller (UI + DAV).
    A refusal message is either one of :data:`PARTIE_NOT_FOUND`,
    :data:`PARTIE_DELETE_CHECK_UNAVAILABLE`, :data:`PARTIE_DELETE_FAILED`,
    or a reference conflict whose text may name the represented contacts —
    never log it.
    """
    existing = get_partie(partie_id)
    if not existing:
        return False, PARTIE_NOT_FOUND

    # FK safety — fail CLOSED: if either check cannot be established the
    # deletion is refused rather than risking dangling references.
    # Local import: avoids any circular-import risk between models.
    from models.dossier import count_dossiers_for_partie_strict

    try:
        linked_count = count_dossiers_for_partie_strict(partie_id)
        referencing = _find_mandataire_references(partie_id)
    except Exception as exc:
        logger.warning(
            "delete_partie: FK check failed for %s: %s",
            sanitize_log_value(partie_id), type(exc).__name__,
        )
        return False, PARTIE_DELETE_CHECK_UNAVAILABLE

    if linked_count > 0:
        return False, (
            f"Impossible de supprimer : ce contact est lié à {linked_count} "
            f"dossier{'s' if linked_count > 1 else ''}. "
            "Retirez-le d'abord de ces dossiers."
        )

    if referencing:
        names = _joined_names(referencing)
        return False, (
            f"Impossible de supprimer : ce contact est mandataire de {names}. "
            "Retirez d'abord cette représentation."
        )

    try:
        db.collection(COLLECTION).document(partie_id).delete()
        return True, ""
    except Exception:
        log_unexpected("partie delete failed")
        return False, PARTIE_DELETE_FAILED


def update_kyc_status(
    partie_id: str,
    field: str,
    status: str,
    notes: Optional[str] = None,
    *,
    source: str,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """Update identity_verified or conflict_check with auto-dated timestamp.

    *notes* is PRESENCE-GATED, like every partial update of this model:
    ``None`` (the default) leaves the stored ``{field}_notes`` untouched;
    a string — the empty one included — REPLACES it, so clearing the
    notes is an explicit ``notes=""``. The default used to be ``""``, and
    since ``update_partie`` merges ``{**existing, **data}`` before a
    full-document ``set()``, a status change made without notes ERASED
    the lawyer's compliance notes, in silence (lot 0b, 2026-09-27).

    *source* is a REQUIRED keyword with no default (D7): ``"juriste"`` or
    ``"mcp"`` — an omission must never stamp a Claude write as the
    lawyer's attestation. With ``"mcp"`` the status is PRESUMED until the
    lawyer confirms it (:func:`confirm_kyc_status`), and ANY write —
    status or notes — on a check the lawyer decided or confirmed is
    refused (``kyc.LAWYER_ATTESTATION``). That refusal is decided on the
    version read here, so an ``"mcp"`` write is ALWAYS compare-and-set
    against it (or against *expected_etag* when given): a decision the
    lawyer makes in between refuses it (stale) instead of being written
    over. An invalid source is a programming error and raises.
    """
    if source not in kyc.VALID_SOURCES:
        raise ValueError(f"unknown KYC source: {source!r}")
    if field not in kyc.FIELDS:
        return None, ["Champ invalide."]
    if status not in kyc.STATUSES[field]:
        return None, ["Statut invalide."]
    if source == kyc.SOURCE_MCP:
        existing = get_partie(partie_id)
        if not existing:
            return None, ["Contact introuvable."]
        if kyc.is_decided(existing, field):
            return None, [kyc.LAWYER_ATTESTATION]
        # The check above is only as good as the version it read: the write
        # lands on THAT version or is refused. A notes-only write carries no
        # transition, so update_partie's own rule would let it through onto
        # a decision (or a « Confirmer ») the lawyer made in between.
        if expected_etag is None:
            expected_etag = concurrency.etag_of(existing)

    update_data: dict = {field: status}
    if notes is not None:
        update_data[kyc.notes_key(field)] = sanitize(notes, max_length=2000)
    return update_partie(
        partie_id, update_data, expected_etag=expected_etag, kyc_source=source)


# The refusals of confirm_kyc_status — constants for the route.
KYC_NOTHING_TO_CONFIRM = (
    "Rien à confirmer : cette vérification n'est pas une inscription de "
    "Claude en attente de confirmation."
)
KYC_CONFIRM_NEEDS_VERSION = (
    "La version de la fiche est requise pour confirmer : rechargez la page, "
    "puis confirmez de nouveau."
)
KYC_CONFIRM_APP_ONLY = (
    "Une vérification de conformité ne se confirme que dans l'application."
)


def confirm_kyc_status(
    partie_id: str,
    field: str,
    *,
    par: str,
    expected_etag: Optional[str] = None,
) -> tuple[Optional[dict], list[str]]:
    """The lawyer's « Confirmer » of a PRESUMED check (D7) — the ONE path
    that turns a Claude inscription into the lawyer's attestation.

    *expected_etag* is REQUIRED in fact (``None`` is refused with
    :data:`KYC_CONFIRM_NEEDS_VERSION`): a confirmation says « I read THIS
    version », so it may only land on the version the page showed. Also
    refused: an unknown contact, a stale version, a check that is not
    presumed (:data:`KYC_NOTHING_TO_CONFIRM`), and — structurally — the
    connector as the writer (:data:`KYC_CONFIRM_APP_ONLY`): Claude can
    never confirm its own inscription. *par* names who confirms.

    The write is a PARTIAL ``update()`` of exactly the two confirmation
    keys and the stamp, compare-and-set in a transaction
    (``concurrency.commit_fields``) — never the merged ``set()``, so an
    unrelated legacy invalid field (a malformed phone) cannot block a
    compliance confirmation, and an inscription landing between the page
    and the click can never be confirmed unseen. The route bumps the CTag.
    """
    if field not in kyc.FIELDS:
        return None, ["Champ invalide."]
    who = str(par or "").strip()
    if not who:
        raise ValueError("confirm_kyc_status needs the confirming party")
    if provenance.current_via() == "mcp":
        return None, [KYC_CONFIRM_APP_ONLY]
    if expected_etag is None:
        return None, [KYC_CONFIRM_NEEDS_VERSION]
    existing = get_partie(partie_id)
    if not existing:
        return None, ["Contact introuvable."]
    if not concurrency.matches(existing, expected_etag):
        return None, [concurrency.STALE_ETAG_ERROR]
    if not kyc.is_presumed(existing, field):
        return None, [KYC_NOTHING_TO_CONFIRM]

    now = datetime.now(timezone.utc)
    fields = {
        kyc.confirmed_at_key(field): now,
        kyc.confirmed_by_key(field): who[:100],
        **provenance.update_fields(now),
    }
    try:
        concurrency.commit_fields(
            db.collection(COLLECTION).document(partie_id), fields,
            expected_etag=expected_etag,
            read_etag=concurrency.etag_of(existing),
        )
    except concurrency.StaleWrite:
        return None, [concurrency.STALE_ETAG_ERROR]
    except concurrency.Vanished:
        return None, ["Contact introuvable."]
    except Exception:
        log_unexpected("partie KYC confirmation failed")
        return None, ["Erreur lors de la sauvegarde. Veuillez réessayer."]
    provenance.note_commit(COLLECTION, partie_id)
    return {**existing, **fields}, []


def link_kyc_document(
    partie_id: str, document_id: str
) -> tuple[Optional[dict], list[str]]:
    """Append a document ID to kyc_document_ids."""
    existing = get_partie(partie_id)
    if not existing:
        return None, ["Contact introuvable."]

    ids = list(existing.get("kyc_document_ids", []))
    if document_id not in ids:
        ids.append(document_id)

    return update_partie(partie_id, {"kyc_document_ids": ids})


# ── vCard 4.0 serialization ──────────────────────────────────────────────


def partie_to_vcard(partie: dict) -> str:
    """Serialize a partie dict to a vCard 4.0 string (RFC 6350)."""
    card = vobject.vCard()

    # VERSION — force 4.0
    card.add("version").value = "4.0"

    # FN (formatted name)
    fn = display_name(partie)
    card.add("fn").value = fn

    # N (structured name)
    n = card.add("n")
    n.value = vobject.vcard.Name(
        family=partie.get("last_name", ""),
        given=partie.get("first_name", ""),
        prefix=partie.get("prefix", ""),
    )

    # ORG
    org_value = (
        partie.get("organization_name", "")
        if partie.get("type") == "organization"
        else partie.get("organization", "")
    )
    if org_value:
        card.add("org").value = [org_value]

    # BDAY — vCard 4.0 date value, « AAAAMMJJ » (RFC 6350 §6.2.5). Émise pour
    # les personnes physiques seulement : une personne morale n'a pas de date
    # de naissance, et un carnet Android l'afficherait comme un anniversaire.
    naissance = partie.get("birth_date")
    if naissance and partie.get("type") != "organization":
        jour = _coerce_birth_date(naissance)
        if jour is not None:
            card.add("bday").value = jour.strftime("%Y%m%d")

    # TITLE
    if partie.get("job_title"):
        card.add("title").value = partie["job_title"]

    # ROLE
    if partie.get("job_role"):
        card.add("role").value = partie["job_role"]

    # EMAIL
    if partie.get("email"):
        email_prop = card.add("email")
        email_prop.value = partie["email"]
        email_prop.type_param = "HOME"

    if partie.get("email_work"):
        email_prop = card.add("email")
        email_prop.value = partie["email_work"]
        email_prop.type_param = "WORK"

    # TEL
    for field, tel_type in [
        ("phone_home", "HOME"),
        ("phone_cell", "CELL"),
        ("phone_work", "WORK"),
        ("fax", "FAX"),
    ]:
        if partie.get(field):
            tel = card.add("tel")
            tel.value = partie[field]
            tel.type_param = tel_type

    # ADR — home
    home_parts = [
        partie.get("address_street", ""),
        partie.get("address_city", ""),
        partie.get("address_province", ""),
        partie.get("address_postal_code", ""),
        partie.get("address_country", ""),
    ]
    if any(home_parts):
        adr = card.add("adr")
        adr.value = vobject.vcard.Address(
            street=partie.get("address_street", ""),
            city=partie.get("address_city", ""),
            region=partie.get("address_province", ""),
            code=partie.get("address_postal_code", ""),
            country=partie.get("address_country", ""),
            extended=partie.get("address_unit", ""),
        )
        adr.type_param = "HOME"

    # ADR — work
    work_parts = [
        partie.get("work_address_street", ""),
        partie.get("work_address_city", ""),
        partie.get("work_address_province", ""),
        partie.get("work_address_postal_code", ""),
        partie.get("work_address_country", ""),
    ]
    if any(work_parts):
        adr = card.add("adr")
        adr.value = vobject.vcard.Address(
            street=partie.get("work_address_street", ""),
            city=partie.get("work_address_city", ""),
            region=partie.get("work_address_province", ""),
            code=partie.get("work_address_postal_code", ""),
            country=partie.get("work_address_country", ""),
            extended=partie.get("work_address_unit", ""),
        )
        adr.type_param = "WORK"

    # NOTE
    if partie.get("notes"):
        card.add("note").value = partie["notes"]

    # CATEGORIES (contact role label)
    role_label = ROLE_LABELS.get(partie.get("contact_role", ""), "Autre")
    card.add("categories").value = [role_label]

    # UID
    card.add("uid").value = partie.get("vcard_uid", "")

    # REV
    updated = partie.get("updated_at")
    if updated:
        if hasattr(updated, "strftime"):
            card.add("rev").value = updated.strftime("%Y%m%dT%H%M%SZ")

    # Serialize to string, then append vCard 4.0 properties that vobject
    # doesn't natively support.
    vcf = card.serialize()

    # Force VERSION:4.0 (vobject defaults to 3.0)
    vcf = vcf.replace("VERSION:3.0", "VERSION:4.0")

    # Append LANG, GENDER, X-PRONOUN before the final END:VCARD
    extra_lines = []
    if partie.get("language"):
        extra_lines.append(f"LANG:{partie['language']}")
    if partie.get("gender"):
        extra_lines.append(f"GENDER:{partie['gender']}")
    if partie.get("pronouns"):
        extra_lines.append(f"X-PRONOUN:{partie['pronouns']}")

    if extra_lines:
        vcf = vcf.replace(
            "END:VCARD", "\r\n".join(extra_lines) + "\r\nEND:VCARD"
        )

    return vcf


def vcard_to_partie(vcard_str: str) -> dict:
    """Parse a vCard 4.0 string into a partie dict (for CardDAV PUT)."""
    card = vobject.readOne(vcard_str)
    data: dict = {}

    # N
    if hasattr(card, "n"):
        n = card.n.value
        data["last_name"] = getattr(n, "family", "")
        data["first_name"] = getattr(n, "given", "")
        data["prefix"] = getattr(n, "prefix", "")

    # ORG
    if hasattr(card, "org"):
        org_val = card.org.value
        if isinstance(org_val, list) and org_val:
            data["organization"] = org_val[0]

    # TITLE, ROLE
    if hasattr(card, "title"):
        data["job_title"] = card.title.value
    if hasattr(card, "role"):
        data["job_role"] = card.role.value

    # BDAY — la clé est OMISE quand la propriété est absente, jamais mise à
    # None : update_partie fusionne {**existing, **data}, donc une clé
    # présente-mais-vide EFFACE, tandis qu'une clé absente survit. Un client
    # CardDAV qui ne gère pas BDAY ne doit pas pouvoir supprimer la date au
    # premier PUT (même règle de non-effacement que CONFERENCE côté hearings).
    # « AAAAMMJJ » (vCard 4.0) comme « AAAA-MM-JJ » (3.0) sont acceptés ; une
    # date partielle (« --0317 », année inconnue) est ignorée.
    if hasattr(card, "bday"):
        brut = (card.bday.value or "").strip()
        compact = brut.replace("-", "")
        if len(compact) == 8 and compact.isdigit():
            jour = _coerce_birth_date(
                f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
            )
            if jour is not None:
                data["birth_date"] = jour

    # Determine type (organization if no last_name but has org)
    if not data.get("last_name") and data.get("organization"):
        data["type"] = "organization"
        data["organization_name"] = data.pop("organization", "")
    else:
        data["type"] = "individual"

    # EMAIL
    if hasattr(card, "email_list"):
        for em in card.email_list:
            etype = getattr(em, "type_param", "")
            if isinstance(etype, str):
                etype = etype.upper()
            elif isinstance(etype, list):
                etype = ",".join(e.upper() for e in etype)
            else:
                etype = ""
            if "WORK" in etype:
                data["email_work"] = em.value
            else:
                data["email"] = em.value

    # TEL
    if hasattr(card, "tel_list"):
        for tel in card.tel_list:
            ttype = getattr(tel, "type_param", "")
            if isinstance(ttype, str):
                ttype = ttype.upper()
            elif isinstance(ttype, list):
                ttype = ",".join(t.upper() for t in ttype)
            else:
                ttype = ""
            if "FAX" in ttype:
                field = "fax"
            elif "CELL" in ttype:
                field = "phone_cell"
            elif "WORK" in ttype:
                field = "phone_work"
            else:
                field = "phone_home"
            raw_tel = tel.value
            normalized = normalize_phone(raw_tel)
            data[field] = normalized if normalized else raw_tel

    # ADR
    if hasattr(card, "adr_list"):
        for adr in card.adr_list:
            atype = getattr(adr, "type_param", "")
            if isinstance(atype, str):
                atype = atype.upper()
            elif isinstance(atype, list):
                atype = ",".join(a.upper() for a in atype)
            else:
                atype = ""
            addr = adr.value
            if "WORK" in atype:
                data["work_address_street"] = getattr(addr, "street", "")
                data["work_address_unit"] = getattr(addr, "extended", "")
                data["work_address_city"] = getattr(addr, "city", "")
                data["work_address_province"] = getattr(addr, "region", "")
                data["work_address_postal_code"] = getattr(addr, "code", "")
                data["work_address_country"] = getattr(addr, "country", "")
            else:
                data["address_street"] = getattr(addr, "street", "")
                data["address_unit"] = getattr(addr, "extended", "")
                data["address_city"] = getattr(addr, "city", "")
                data["address_province"] = getattr(addr, "region", "")
                data["address_postal_code"] = getattr(addr, "code", "")
                data["address_country"] = getattr(addr, "country", "")

    # NOTE
    if hasattr(card, "note"):
        data["notes"] = card.note.value

    # CATEGORIES → contact_role
    if hasattr(card, "categories"):
        cat_val = card.categories.value
        if isinstance(cat_val, list) and cat_val:
            label = cat_val[0]
        else:
            label = str(cat_val)
        # Reverse-lookup from label to key. Include the pre-rename labels
        # ("Partie adverse" → "Partie", "Avocat(e) adverse" → "Avocat(e)")
        # as aliases so vCards synced before the rename round-trip back to
        # their original role instead of silently degrading to "autre".
        reverse_map = {v: k for k, v in ROLE_LABELS.items()}
        reverse_map.setdefault("Partie adverse", "partie_adverse")
        reverse_map.setdefault("Avocat(e) adverse", "avocat_adverse")
        data["contact_role"] = reverse_map.get(label, "autre")

    # UID
    if hasattr(card, "uid"):
        data["vcard_uid"] = card.uid.value

    # Parse raw lines for LANG, GENDER, X-PRONOUN (not supported by vobject)
    for line in vcard_str.splitlines():
        upper_line = line.upper()
        if upper_line.startswith("LANG:"):
            data["language"] = line.split(":", 1)[1].strip()
        elif upper_line.startswith("GENDER:"):
            data["gender"] = line.split(":", 1)[1].strip()
        elif upper_line.startswith("X-PRONOUN:"):
            data["pronouns"] = line.split(":", 1)[1].strip()

    return data
