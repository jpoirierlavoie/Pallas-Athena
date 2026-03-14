"""Partie (contact/party) Firestore CRUD and vCard 4.0 serialization."""

import uuid
from datetime import datetime, timezone
from typing import Optional

import vobject

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

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
VALID_IDENTITY_STATUSES = ("non_vérifié", "vérifié", "exempté")
VALID_CONFLICT_STATUSES = ("non_vérifié", "vérifié", "conflit_détecté")

# Contact-role display labels (French)
ROLE_LABELS = {
    "client": "Client",
    "partie_adverse": "Partie adverse",
    "avocat_adverse": "Avocat(e) adverse",
    "témoin": "Témoin",
    "expert": "Expert(e)",
    "huissier": "Huissier(ère)",
    "notaire": "Notaire",
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
        # Organization
        "organization_name": "",
        "contact_person": "",
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
        "address_province": "QC",
        "address_postal_code": "",
        "address_country": "CA",
        # Work address
        "work_address_street": "",
        "work_address_unit": "",
        "work_address_city": "",
        "work_address_province": "",
        "work_address_postal_code": "",
        "work_address_country": "CA",
        # Legal identifiers
        "bar_number": "",
        "company_neq": "",
        # KYC / Compliance
        "identity_verified": "non_vérifié",
        "identity_verified_date": None,
        "identity_verified_notes": "",
        "kyc_document_ids": [],
        "conflict_check": "non_vérifié",
        "conflict_check_date": None,
        "conflict_check_notes": "",
        # Notes
        "notes": "",
        # Metadata (set by create/update)
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


def _validate(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []
    client_type = data.get("type", "individual")

    if client_type == "individual":
        if not data.get("last_name", "").strip():
            errors.append("Le nom de famille est requis.")
    elif client_type == "organization":
        if not data.get("organization_name", "").strip():
            errors.append("Le nom de l'organisation est requis.")
    else:
        errors.append("Type de contact invalide.")

    if data.get("contact_role", "") not in VALID_CONTACT_ROLES:
        errors.append("Rôle de contact invalide.")

    email = data.get("email", "").strip()
    if email and "@" not in email:
        errors.append("Adresse courriel invalide.")

    email_work = data.get("email_work", "").strip()
    if email_work and "@" not in email_work:
        errors.append("Adresse courriel professionnelle invalide.")

    return errors


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


# ── CRUD ──────────────────────────────────────────────────────────────────


def create_partie(data: dict) -> tuple[Optional[dict], list[str]]:
    """Validate, generate IDs, write to Firestore. Returns (doc, errors)."""
    merged = {**_default_doc(), **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    partie_id = str(uuid.uuid4())
    etag = str(uuid.uuid4())
    vcard_uid = str(uuid.uuid4())

    merged.update(
        {
            "id": partie_id,
            "created_at": now,
            "updated_at": now,
            "etag": etag,
            "vcard_uid": vcard_uid,
            "dav_href": f"/dav/addressbook/{partie_id}.vcf",
        }
    )

    try:
        db.collection(COLLECTION).document(partie_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def get_partie(partie_id: str) -> Optional[dict]:
    """Fetch a single partie by ID."""
    try:
        doc = db.collection(COLLECTION).document(partie_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


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


def update_partie(
    partie_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update an existing partie. Returns (updated_doc, errors)."""
    existing = get_partie(partie_id)
    if not existing:
        return None, ["Contact introuvable."]

    merged = {**existing, **_sanitize_data(data)}
    errors = _validate(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    # Auto-set verification dates on status change
    if (
        data.get("identity_verified")
        and data["identity_verified"] != existing.get("identity_verified")
    ):
        merged["identity_verified_date"] = now

    if (
        data.get("conflict_check")
        and data["conflict_check"] != existing.get("conflict_check")
    ):
        merged["conflict_check_date"] = now

    try:
        db.collection(COLLECTION).document(partie_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_partie(partie_id: str) -> tuple[bool, str]:
    """Delete a partie. Returns (success, error_message)."""
    existing = get_partie(partie_id)
    if not existing:
        return False, "Contact introuvable."

    try:
        db.collection(COLLECTION).document(partie_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def update_kyc_status(
    partie_id: str, field: str, status: str, notes: str = ""
) -> tuple[Optional[dict], list[str]]:
    """Update identity_verified or conflict_check with auto-dated timestamp."""
    if field not in ("identity_verified", "conflict_check"):
        return None, ["Champ invalide."]

    valid = (
        VALID_IDENTITY_STATUSES
        if field == "identity_verified"
        else VALID_CONFLICT_STATUSES
    )
    if status not in valid:
        return None, ["Statut invalide."]

    update_data = {
        field: status,
        f"{field}_notes": sanitize(notes, max_length=2000),
    }
    return update_partie(partie_id, update_data)


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
                data["fax"] = tel.value
            elif "CELL" in ttype:
                data["phone_cell"] = tel.value
            elif "WORK" in ttype:
                data["phone_work"] = tel.value
            else:
                data["phone_home"] = tel.value

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
        # Reverse-lookup from label to key
        reverse_map = {v: k for k, v in ROLE_LABELS.items()}
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
