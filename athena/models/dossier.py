"""Dossier (case file) Firestore CRUD and RFC-5545 VJOURNAL serialization."""

import uuid
from datetime import datetime, timezone
from typing import Optional

import icalendar

from models import db
from security import sanitize

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
        "client_id": "",
        "client_name": "",
        # Case classification
        "matter_type": "litige_civil",
        "court": "",
        "district": "",
        "court_file_number": "",
        # Parties
        "role": "demandeur",
        "opposing_party": "",
        "opposing_counsel": "",
        "opposing_counsel_firm": "",
        "opposing_counsel_phone": "",
        "opposing_counsel_email": "",
        # Financial
        "hourly_rate": 25000,
        "flat_fee": None,
        "fee_type": "hourly",
        "retainer_amount": 0,
        "retainer_balance": 0,
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

    if not data.get("client_id", "").strip():
        errors.append("Un client doit être associé au dossier.")

    if not data.get("file_number", "").strip():
        errors.append("Le numéro de dossier est requis.")

    if data.get("matter_type", "") not in VALID_MATTER_TYPES:
        errors.append("Type de dossier invalide.")

    if data.get("status", "") not in VALID_STATUSES:
        errors.append("Statut invalide.")

    fee_type = data.get("fee_type", "")
    if fee_type and fee_type not in VALID_FEE_TYPES:
        errors.append("Type d'honoraires invalide.")

    email = data.get("opposing_counsel_email", "").strip()
    if email and "@" not in email:
        errors.append("Adresse courriel de l'avocat adverse invalide.")

    return errors


def _suggest_next_file_number() -> str:
    """Suggest the next sequential file number for the current year."""
    year = datetime.now(timezone.utc).year
    try:
        query = (
            db.collection(COLLECTION)
            .where("file_number", ">=", f"{year}-")
            .where("file_number", "<=", f"{year}-\uf8ff")
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
                    pass
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
            .where("file_number", "==", merged["file_number"])
            .limit(1)
            .get()
        )
        if list(existing):
            return None, ["Ce numéro de dossier existe déjà."]
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    dossier_id = str(uuid.uuid4())
    etag = str(uuid.uuid4())
    vjournal_uid = str(uuid.uuid4())

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


def get_dossier(dossier_id: str) -> Optional[dict]:
    """Fetch a single dossier by ID."""
    try:
        doc = db.collection(COLLECTION).document(dossier_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception:
        pass
    return None


def list_dossiers(
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    client_id: Optional[str] = None,
    sort_by: str = "opened_date",
) -> list[dict]:
    """Return dossiers, optionally filtered by status, search, or client."""
    try:
        query = db.collection(COLLECTION)

        if status_filter and status_filter in VALID_STATUSES:
            query = query.where("status", "==", status_filter)

        if client_id:
            query = query.where("client_id", "==", client_id)

        results = [doc.to_dict() for doc in query.stream()]

        # Client-side search (Firestore doesn't support full-text)
        if search:
            term = search.lower()
            filtered = []
            for d in results:
                searchable = " ".join(
                    [
                        d.get("file_number", ""),
                        d.get("title", ""),
                        d.get("client_name", ""),
                        d.get("court_file_number", ""),
                    ]
                ).lower()
                if term in searchable:
                    filtered.append(d)
            results = filtered

        # Sort
        if sort_by == "file_number":
            results.sort(key=lambda d: d.get("file_number", ""), reverse=True)
        elif sort_by == "client_name":
            results.sort(key=lambda d: d.get("client_name", "").lower())
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
                .where("file_number", "==", merged["file_number"])
                .limit(1)
                .get()
            )
            for d in dup:
                if d.id != dossier_id:
                    return None, ["Ce numéro de dossier existe déjà."]
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

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


def count_dossiers_for_client(client_id: str) -> int:
    """Count how many dossiers reference a given client."""
    try:
        query = db.collection(COLLECTION).where("client_id", "==", client_id)
        return len(list(query.stream()))
    except Exception:
        return 0


def list_dossiers_for_client(client_id: str) -> list[dict]:
    """Return all dossiers linked to a client, newest first."""
    return list_dossiers(client_id=client_id)


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
    if dossier.get("client_id"):
        journal.add("x-pallas-client-id", dossier["client_id"])
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

        client_id = component.get("x-pallas-client-id")
        if client_id:
            data["client_id"] = str(client_id)

        court_file = component.get("x-pallas-court-file")
        if court_file:
            data["court_file_number"] = str(court_file)

        break  # Only process first VJOURNAL

    return data
