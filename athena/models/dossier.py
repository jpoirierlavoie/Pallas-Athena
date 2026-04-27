"""Dossier (case file) Firestore CRUD and RFC-5545 VJOURNAL serialization."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

logger = logging.getLogger(__name__)

# Firestore collection path
COLLECTION = "dossiers"

# Valid enum values
VALID_MATTER_TYPES = (
    "litige_civil",
    "litige_commercial",
    "recouvrement",
    "injonction",
    "familial",
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
VALID_FEE_TYPES = ("hourly", "flat", "contingency", "mixed")
VALID_STATUSES = ("actif", "en_attente", "fermé", "archivé")

# Display labels (French)
MATTER_TYPE_LABELS = {
    "litige_civil": "Litige civil",
    "litige_commercial": "Litige commercial",
    "recouvrement": "Recouvrement",
    "injonction": "Injonction",
    "familial": "Familial",
    "autre": "Autre",
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
}


def _default_doc() -> dict:
    """Return a dict with every dossier field set to its default value."""
    return {
        "id": "",
        "file_number": "",
        "title": "",
        # Parties on the dossier (arrays of {id, name} dicts)
        "clients": [],
        "client_ids": [],
        "opposing_parties": [],
        "opposing_party_ids": [],
        # Case classification
        "matter_type": "litige_civil",
        "court_file_number": "",
        "district_judiciaire": "",
        "tribunal": "",
        "competence": "",
        "palais_de_justice": "",
        "greffe_number": "",
        "juridiction_number": "",
        "is_administrative_tribunal": False,
        # Role of the lawyer's client
        "role": "demandeur",
        # Financial
        "hourly_rate": 25000,
        "flat_fee": None,
        "fee_type": "hourly",
        # Status
        "status": "actif",
        "opened_date": None,
        "closed_date": None,
        # Prescription
        "prescription_date": None,
        "prescription_notes": "",
        # Notes
        "notes": "",
        "internal_notes": "",
        # Metadata
        "created_at": None,
        "updated_at": None,
        "etag": "",
        # DAV
        "vjournal_uid": "",
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

    if not data.get("title", "").strip():
        errors.append("Le titre du dossier est requis.")

    if not data.get("clients"):
        errors.append("Au moins un client doit être associé au dossier.")

    if not data.get("file_number", "").strip():
        errors.append("Le numéro de dossier est requis.")

    if data.get("matter_type", "") not in VALID_MATTER_TYPES:
        errors.append("Type de dossier invalide.")

    if data.get("status", "") not in VALID_STATUSES:
        errors.append("Statut invalide.")

    fee_type = data.get("fee_type", "")
    if fee_type and fee_type not in VALID_FEE_TYPES:
        errors.append("Type d'honoraires invalide.")

    return errors


def _suggest_next_file_number() -> str:
    """Suggest the next sequential file number for the current year."""
    year = datetime.now(timezone.utc).year
    try:
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("file_number", ">=", f"{year}-"))
            .where(filter=FieldFilter("file_number", "<=", f"{year}-\uf8ff"))
        )
        docs = list(query.stream())
        if not docs:
            return f"{year}-001"

        max_seq = 0
        for doc in docs:
            d = doc.to_dict()
            fn = d.get("file_number", "")
            parts = fn.split("-", 1)
            if len(parts) == 2:
                try:
                    seq = int(parts[1])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    continue
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

    try:
        db.collection(COLLECTION).document(dossier_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

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
    return doc


def get_dossier(dossier_id: str) -> Optional[dict]:
    """Fetch a single dossier by ID."""
    try:
        doc = db.collection(COLLECTION).document(dossier_id).get()
        if doc.exists:
            return _migrate_parties(doc.to_dict())
    except Exception as exc:
        logger.warning("get_dossier failed for %s: %s", dossier_id, exc)
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
            logger.warning("update_dossier: duplicate-check query failed for %s: %s", dossier_id, exc)

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    # Sync flat ID arrays
    merged["client_ids"] = [c["id"] for c in merged.get("clients", [])]
    merged["opposing_party_ids"] = [p["id"] for p in merged.get("opposing_parties", [])]

    # Auto-set closed_date when status changes to fermé or archivé
    if (
        merged.get("status") in ("fermé", "archivé")
        and existing.get("status") not in ("fermé", "archivé")
    ):
        merged["closed_date"] = now
    elif merged.get("status") in ("actif", "en_attente") and existing.get(
        "status"
    ) in ("fermé", "archivé"):
        merged["closed_date"] = None

    try:
        db.collection(COLLECTION).document(dossier_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_dossier(dossier_id: str) -> tuple[bool, str]:
    """Delete a dossier. Returns (success, error_message)."""
    existing = get_dossier(dossier_id)
    if not existing:
        return False, "Dossier introuvable."

    try:
        db.collection(COLLECTION).document(dossier_id).delete()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def suggest_file_number() -> str:
    """Public wrapper for auto-suggesting the next file number."""
    return _suggest_next_file_number()


def count_dossiers_for_partie(partie_id: str) -> int:
    """Count how many dossiers reference a given partie (as client or opposing)."""
    try:
        q1 = db.collection(COLLECTION).where(filter=FieldFilter("client_ids", "array_contains", partie_id))
        q2 = db.collection(COLLECTION).where(filter=FieldFilter("opposing_party_ids", "array_contains", partie_id))
        ids = {doc.id for doc in q1.stream()} | {doc.id for doc in q2.stream()}
        return len(ids)
    except Exception:
        return 0


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

    # DESCRIPTION — combine notes
    desc_parts = []
    if dossier.get("notes"):
        desc_parts.append(dossier["notes"])
    if dossier.get("internal_notes"):
        desc_parts.append(f"[Notes internes] {dossier['internal_notes']}")
    if desc_parts:
        journal.add("description", "\n\n".join(desc_parts))

    # STATUS mapping
    status_map = {
        "actif": "FINAL",
        "en_attente": "DRAFT",
        "fermé": "CANCELLED",
        "archivé": "CANCELLED",
    }
    journal.add("status", status_map.get(dossier.get("status", ""), "DRAFT"))

    # CATEGORIES
    categories = []
    if dossier.get("matter_type"):
        label = MATTER_TYPE_LABELS.get(dossier["matter_type"], dossier["matter_type"])
        categories.append(label)
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
