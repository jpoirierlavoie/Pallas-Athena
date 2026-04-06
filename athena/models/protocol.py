"""Case protocol Firestore CRUD — protocols and protocol steps."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.deadlines import compute_deadline as _judicial_deadline

from google.cloud.firestore_v1.base_query import FieldFilter
from models import db
from security import sanitize

# Firestore collection path
COLLECTION = "protocols"
STEPS_SUBCOLLECTION = "steps"

# Valid enum values
VALID_PROTOCOL_TYPES = ("cq_simplifié", "cs_ordinaire", "conventionnel")
VALID_STATUSES = ("actif", "complété", "suspendu")
VALID_STEP_STATUSES = ("à_venir", "en_cours", "complété", "en_retard")
VALID_COURTS = (
    "Cour du Québec",
    "Cour supérieure",
    "Cour d'appel",
    "Tribunal administratif",
    "Arbitrage",
    "autre",
)

# Display labels (French)
PROTOCOL_TYPE_LABELS = {
    "cq_simplifié": "CQ — Procédure simplifiée",
    "cs_ordinaire": "CS — Procédure ordinaire",
    "conventionnel": "Conventionnel",
}
PROTOCOL_TYPE_SHORT_LABELS = {
    "cq_simplifié": "CQ Simplifié",
    "cs_ordinaire": "CS Ordinaire",
    "conventionnel": "Conventionnel",
}
STATUS_LABELS = {
    "actif": "Actif",
    "complété": "Complété",
    "suspendu": "Suspendu",
}
STEP_STATUS_LABELS = {
    "à_venir": "À venir",
    "en_cours": "En cours",
    "complété": "Complété",
    "en_retard": "En retard",
}

# Color mapping for protocol type badges
PROTOCOL_TYPE_COLORS = {
    "cq_simplifié": "bg-blue-100 text-blue-700",
    "cs_ordinaire": "bg-indigo-100 text-indigo-700",
    "conventionnel": "bg-gray-200 text-gray-700",
}
STEP_STATUS_COLORS = {
    "à_venir": "bg-gray-100 text-gray-600",
    "en_cours": "bg-blue-100 text-blue-700",
    "complété": "bg-green-100 text-green-700",
    "en_retard": "bg-red-100 text-red-700",
}

# ── Protocol Templates ──────────────────────────────────────────────────

CQ_TEMPLATE_STEPS = [
    {
        "order": 1,
        "title": "Signification de l'avis d'assignation",
        "description": "",
        "cpc_reference": "art. 145 C.p.c.",
        "deadline_offset_days": 0,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 2,
        "title": "Avis de la partie demanderesse",
        "description": "",
        "cpc_reference": "art. 535.4 C.p.c.",
        "deadline_offset_days": 20,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 3,
        "title": "Dénonciation des moyens préliminaires",
        "description": "",
        "cpc_reference": "art. 535.5 C.p.c.",
        "deadline_offset_days": 45,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 4,
        "title": "Avis de la partie défenderesse",
        "description": "",
        "cpc_reference": "art. 535.6 C.p.c.",
        "deadline_offset_days": 95,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 5,
        "title": "Conférence de gestion",
        "description": "",
        "cpc_reference": "art. 535.8 C.p.c.",
        "deadline_offset_days": 110,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 6,
        "title": "Conférence de règlement à l'amiable",
        "description": "",
        "cpc_reference": "art. 535.12 C.p.c.",
        "deadline_offset_days": 145,
        "mandatory": True,
        "deadline_locked": True,
    },
    {
        "order": 7,
        "title": "Inscription pour instruction et jugement",
        "description": "",
        "cpc_reference": "art. 535.13 C.p.c.",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": True,
    },
]

CS_TEMPLATE_STEPS = [
    {
        "order": 1,
        "title": "Signification de l'avis d'assignation",
        "description": "",
        "cpc_reference": "art. 145(1) C.p.c.",
        "deadline_offset_days": 0,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 2,
        "title": "Réponse",
        "description": "",
        "cpc_reference": "art. 145(2) C.p.c.",
        "deadline_offset_days": 15,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 3,
        "title": "Premier protocole de l'instance",
        "description": "",
        "cpc_reference": "art. 149(2) C.p.c.",
        "deadline_offset_days": 45,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 4,
        "title": "Interrogatoires préalables",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 120,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 5,
        "title": "Expertises (rapports d'experts)",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 150,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 6,
        "title": "Conférence de règlement à l'amiable",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 7,
        "title": "Conférence de gestion",
        "description": "",
        "cpc_reference": "",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
    },
    {
        "order": 8,
        "title": "Inscription pour instruction et jugement",
        "description": "",
        "cpc_reference": "art. 173(1) C.p.c.",
        "deadline_offset_days": 180,
        "mandatory": True,
        "deadline_locked": False,
    },
]


def get_template(protocol_type: str) -> list[dict]:
    """Return the step template for a given protocol type."""
    if protocol_type == "cq_simplifié":
        return [dict(s) for s in CQ_TEMPLATE_STEPS]
    elif protocol_type == "cs_ordinaire":
        return [dict(s) for s in CS_TEMPLATE_STEPS]
    return []


# ── Default docs ────────────────────────────────────────────────────────


def _default_protocol() -> dict:
    """Return a dict with every protocol field set to its default value."""
    return {
        "id": "",
        "dossier_id": None,
        "dossier_file_number": "",
        "dossier_title": "",
        "title": "Protocole de l'instance",
        "protocol_type": "",
        "start_date": None,
        "end_date": None,
        "court": "",
        "notes": "",
        "status": "actif",
        "created_at": None,
        "updated_at": None,
        "etag": "",
    }


def _default_step() -> dict:
    """Return a dict with every step field set to its default value."""
    return {
        "id": "",
        "order": 0,
        "title": "",
        "description": "",
        "cpc_reference": "",
        "deadline_date": None,
        "deadline_offset_days": None,
        "mandatory": False,
        "deadline_locked": False,
        "status": "à_venir",
        "completed_date": None,
        "linked_task_id": None,
        "linked_hearing_id": None,
        "notes": "",
        "date_confirmed": False,
        "created_at": None,
        "updated_at": None,
    }


# ── Sanitization & validation ───────────────────────────────────────────


def _sanitize_data(data: dict) -> dict:
    """Sanitize all string values in *data*."""
    out: dict = {}
    for key, val in data.items():
        if isinstance(val, str):
            out[key] = sanitize(val, max_length=2000)
        else:
            out[key] = val
    return out


def _validate_protocol(data: dict) -> list[str]:
    """Return a list of validation error messages (empty = valid)."""
    errors: list[str] = []

    if not data.get("dossier_id"):
        errors.append("Le dossier est requis.")

    ptype = data.get("protocol_type", "")
    if ptype not in VALID_PROTOCOL_TYPES:
        errors.append("Type de protocole invalide.")

    if not data.get("start_date"):
        errors.append("La date de début est requise.")

    status = data.get("status", "")
    if status and status not in VALID_STATUSES:
        errors.append("Statut invalide.")

    return errors


def _validate_step(data: dict) -> list[str]:
    """Return a list of validation error messages for a step."""
    errors: list[str] = []

    if not data.get("title", "").strip():
        errors.append("Le titre de l'étape est requis.")

    status = data.get("status", "")
    if status and status not in VALID_STEP_STATUSES:
        errors.append("Statut d'étape invalide.")

    return errors


def _compute_deadline(start_date: datetime, offset_days: int) -> datetime:
    """Compute a protocol step deadline using judicial delay rules (art. 83 C.p.c.)."""
    result_date = _judicial_deadline(start_date.date(), offset_days, direction="after")
    return datetime.combine(result_date, datetime.min.time(), timezone.utc)


def _compute_end_date(start_date: datetime, steps: list[dict]) -> datetime:
    """Compute the protocol end date as the latest step deadline."""
    max_offset = 0
    max_date = start_date
    for step in steps:
        if step.get("deadline_offset_days") is not None:
            offset = step["deadline_offset_days"]
            if offset > max_offset:
                max_offset = offset
        if step.get("deadline_date") and step["deadline_date"] > max_date:
            max_date = step["deadline_date"]
    computed = start_date + timedelta(days=max_offset)
    return max(computed, max_date)


# ── Helpers ─────────────────────────────────────────────────────────────


def _get_active_protocols(dossier_id: str) -> list[dict]:
    """Return all protocols with status 'actif' for a dossier."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        ).where(
            filter=FieldFilter("status", "==", "actif")
        )
        return [doc.to_dict() for doc in query.stream()]
    except Exception:
        return []


# ── CRUD ────────────────────────────────────────────────────────────────


def create_protocol(
    dossier_id: str,
    protocol_type: str,
    start_date: datetime,
    data: dict,
    auto_create_tasks: bool = False,
) -> tuple[Optional[dict], list[str]]:
    """Create a protocol with auto-generated steps. Returns (doc, errors)."""
    merged = {**_default_protocol(), **_sanitize_data(data)}
    merged["dossier_id"] = dossier_id
    merged["protocol_type"] = protocol_type
    merged["start_date"] = start_date

    errors = _validate_protocol(merged)
    if errors:
        return None, errors

    # Check: only one active protocol per dossier
    active_protocols = _get_active_protocols(dossier_id)
    if active_protocols:
        return None, [
            "Ce dossier a déjà un protocole actif. "
            "Complétez ou suspendez le protocole existant avant d'en créer un nouveau."
        ]

    now = datetime.now(timezone.utc)
    protocol_id = str(uuid.uuid4())

    # Generate steps from template
    template_steps = get_template(protocol_type)
    step_docs = []
    for tmpl in template_steps:
        step = {**_default_step(), **tmpl}
        step["id"] = str(uuid.uuid4())
        step["created_at"] = now
        step["updated_at"] = now
        if step["deadline_offset_days"] is not None:
            step["deadline_date"] = _compute_deadline(
                start_date, step["deadline_offset_days"]
            )
        # For CS type, mark dates as unconfirmed (needs user edit)
        if protocol_type == "cs_ordinaire":
            step["date_confirmed"] = False
        else:
            step["date_confirmed"] = True
        step_docs.append(step)

    # Compute end date
    if step_docs:
        merged["end_date"] = _compute_end_date(start_date, step_docs)
    else:
        merged["end_date"] = start_date

    merged.update({
        "id": protocol_id,
        "created_at": now,
        "updated_at": now,
        "etag": str(uuid.uuid4()),
    })

    # Batch write protocol + all steps
    try:
        batch = db.batch()
        proto_ref = db.collection(COLLECTION).document(protocol_id)
        batch.set(proto_ref, merged)

        for step in step_docs:
            step_ref = proto_ref.collection(STEPS_SUBCOLLECTION).document(
                step["id"]
            )
            batch.set(step_ref, step)

        batch.commit()
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    # Auto-create linked tasks if requested
    if auto_create_tasks and step_docs:
        _auto_create_tasks_for_steps(protocol_id, merged, step_docs)

    merged["steps"] = step_docs
    return merged, []


def get_protocol(protocol_id: str) -> Optional[dict]:
    """Fetch a single protocol by ID, with all its steps."""
    try:
        doc = db.collection(COLLECTION).document(protocol_id).get()
        if not doc.exists:
            return None
        protocol = doc.to_dict()

        # Load steps subcollection
        steps_ref = (
            db.collection(COLLECTION)
            .document(protocol_id)
            .collection(STEPS_SUBCOLLECTION)
        )
        steps = [s.to_dict() for s in steps_ref.stream()]
        steps.sort(key=lambda s: s.get("order", 0))
        protocol["steps"] = steps
        return protocol
    except Exception:
        return None


def get_protocol_for_dossier(
    dossier_id: str, active_only: bool = True
) -> Optional[dict]:
    """Return a protocol for a dossier.

    If active_only is True (default), returns only the 'actif' protocol.
    If active_only is False, returns the most recent protocol regardless of status.
    """
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        results = [doc.to_dict() for doc in query.stream()]

        # Prefer active protocol
        for r in results:
            if r.get("status") == "actif":
                return get_protocol(r["id"])

        if active_only:
            return None

        # Fall back to most recent
        if results:
            results.sort(
                key=lambda p: p.get("created_at") or datetime.min.replace(
                    tzinfo=timezone.utc
                ),
                reverse=True,
            )
            return get_protocol(results[0]["id"])

        return None
    except Exception:
        return None


def list_protocols_for_dossier(dossier_id: str) -> list[dict]:
    """Return all protocols for a dossier, newest first. Steps are NOT loaded."""
    try:
        query = db.collection(COLLECTION).where(
            filter=FieldFilter("dossier_id", "==", dossier_id)
        )
        results = [doc.to_dict() for doc in query.stream()]
        results.sort(
            key=lambda p: p.get("created_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            reverse=True,
        )
        return results
    except Exception:
        return []


def list_protocols(
    status_filter: Optional[str] = None,
    protocol_type_filter: Optional[str] = None,
) -> list[dict]:
    """Return all protocols, optionally filtered. Steps are NOT loaded."""
    try:
        query = db.collection(COLLECTION)

        if status_filter and status_filter in VALID_STATUSES:
            query = query.where(
                filter=FieldFilter("status", "==", status_filter)
            )

        results = [doc.to_dict() for doc in query.stream()]

        if protocol_type_filter and protocol_type_filter in VALID_PROTOCOL_TYPES:
            results = [
                r for r in results
                if r.get("protocol_type") == protocol_type_filter
            ]

        # Sort by created_at descending (newest first)
        results.sort(
            key=lambda p: p.get("created_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            reverse=True,
        )
        return results
    except Exception:
        return []


def update_protocol(
    protocol_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update protocol metadata. Returns (updated_doc, errors)."""
    existing = get_protocol(protocol_id)
    if not existing:
        return None, ["Protocole introuvable."]

    steps = existing.pop("steps", [])
    merged = {**existing, **_sanitize_data(data)}

    errors = _validate_protocol(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now
    merged["etag"] = str(uuid.uuid4())

    try:
        db.collection(COLLECTION).document(protocol_id).set(merged)
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    merged["steps"] = steps
    return merged, []


def delete_protocol(protocol_id: str) -> tuple[bool, str]:
    """Delete a protocol and all its steps. Returns (success, error_message)."""
    existing = get_protocol(protocol_id)
    if not existing:
        return False, "Protocole introuvable."

    try:
        batch = db.batch()
        proto_ref = db.collection(COLLECTION).document(protocol_id)

        # Delete all steps
        for step in existing.get("steps", []):
            step_ref = proto_ref.collection(STEPS_SUBCOLLECTION).document(
                step["id"]
            )
            batch.delete(step_ref)

        batch.delete(proto_ref)
        batch.commit()
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


# ── Step operations ─────────────────────────────────────────────────────


def add_step(
    protocol_id: str, step_data: dict
) -> tuple[Optional[dict], list[str]]:
    """Add a custom step to a protocol. Returns (step_doc, errors)."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    merged = {**_default_step(), **_sanitize_data(step_data)}

    errors = _validate_step(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    step_id = str(uuid.uuid4())

    # Auto-assign order (append to end)
    existing_steps = protocol.get("steps", [])
    max_order = max((s.get("order", 0) for s in existing_steps), default=0)
    merged["order"] = max_order + 1

    merged.update({
        "id": step_id,
        "created_at": now,
        "updated_at": now,
        "date_confirmed": True,
    })

    try:
        db.collection(COLLECTION).document(protocol_id).collection(
            STEPS_SUBCOLLECTION
        ).document(step_id).set(merged)

        # Update protocol etag and updated_at
        db.collection(COLLECTION).document(protocol_id).update({
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def update_step(
    protocol_id: str, step_id: str, data: dict
) -> tuple[Optional[dict], list[str]]:
    """Update a step. Validates locked deadlines. Returns (step_doc, errors)."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    existing_step = None
    for s in protocol.get("steps", []):
        if s["id"] == step_id:
            existing_step = s
            break
    if not existing_step:
        return None, ["Étape introuvable."]

    # Prevent changing deadline on locked steps
    if existing_step.get("deadline_locked"):
        if "deadline_date" in data and data["deadline_date"] != existing_step.get("deadline_date"):
            return None, [
                "Cette échéance est prescrite par la loi et ne peut pas être modifiée."
            ]

    merged = {**existing_step, **_sanitize_data(data)}

    errors = _validate_step(merged)
    if errors:
        return None, errors

    now = datetime.now(timezone.utc)
    merged["updated_at"] = now

    # If user explicitly sets a date on CS protocol, mark as confirmed
    if "deadline_date" in data:
        merged["date_confirmed"] = True

    try:
        db.collection(COLLECTION).document(protocol_id).collection(
            STEPS_SUBCOLLECTION
        ).document(step_id).set(merged)

        db.collection(COLLECTION).document(protocol_id).update({
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
    except Exception as exc:
        return None, [f"Erreur lors de la sauvegarde : {exc}"]

    return merged, []


def delete_step(
    protocol_id: str, step_id: str
) -> tuple[bool, str]:
    """Delete a step. Cannot delete mandatory steps. Returns (success, error)."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return False, "Protocole introuvable."

    target_step = None
    for s in protocol.get("steps", []):
        if s["id"] == step_id:
            target_step = s
            break
    if not target_step:
        return False, "Étape introuvable."

    if target_step.get("mandatory"):
        return False, "Les étapes obligatoires ne peuvent pas être supprimées."

    try:
        now = datetime.now(timezone.utc)
        db.collection(COLLECTION).document(protocol_id).collection(
            STEPS_SUBCOLLECTION
        ).document(step_id).delete()

        db.collection(COLLECTION).document(protocol_id).update({
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })
        return True, ""
    except Exception as exc:
        return False, f"Erreur lors de la suppression : {exc}"


def complete_step(
    protocol_id: str, step_id: str
) -> tuple[Optional[dict], list[str]]:
    """Mark a step as complete, sync linked task. Returns (step_doc, errors)."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    target_step = None
    for s in protocol.get("steps", []):
        if s["id"] == step_id:
            target_step = s
            break
    if not target_step:
        return None, ["Étape introuvable."]

    now = datetime.now(timezone.utc)

    if target_step.get("status") == "complété":
        # Un-complete: revert to à_venir
        new_data = {
            "status": "à_venir",
            "completed_date": None,
        }
    else:
        new_data = {
            "status": "complété",
            "completed_date": now,
        }

    step, errors = update_step(protocol_id, step_id, new_data)
    if errors:
        return None, errors

    # Sync linked task if present
    if step and step.get("linked_task_id"):
        _sync_task_status(step["linked_task_id"], step["status"])

    # Check if all steps are complete — auto-complete protocol
    _check_protocol_completion(protocol_id)

    return step, []


def uncomplete_step(
    protocol_id: str, step_id: str
) -> tuple[Optional[dict], list[str]]:
    """Revert a completed step back to à_venir."""
    return update_step(protocol_id, step_id, {
        "status": "à_venir",
        "completed_date": None,
    })


def recompute_deadlines(
    protocol_id: str, new_start_date: datetime
) -> tuple[Optional[dict], list[str]]:
    """Recalculate all offset-based deadlines from a new start date."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return None, ["Protocole introuvable."]

    now = datetime.now(timezone.utc)

    try:
        batch = db.batch()
        proto_ref = db.collection(COLLECTION).document(protocol_id)

        for step in protocol.get("steps", []):
            if step.get("deadline_offset_days") is not None:
                new_deadline = _compute_deadline(
                    new_start_date, step["deadline_offset_days"]
                )
                step["deadline_date"] = new_deadline
                step["updated_at"] = now

                step_ref = proto_ref.collection(
                    STEPS_SUBCOLLECTION
                ).document(step["id"])
                batch.set(step_ref, step)

        # Update protocol start/end date
        steps = protocol.get("steps", [])
        end_date = _compute_end_date(new_start_date, steps)

        batch.update(proto_ref, {
            "start_date": new_start_date,
            "end_date": end_date,
            "updated_at": now,
            "etag": str(uuid.uuid4()),
        })

        batch.commit()
    except Exception as exc:
        return None, [f"Erreur lors du recalcul : {exc}"]

    return get_protocol(protocol_id), []


def check_overdue_steps(protocol_id: str) -> int:
    """Scan steps and update status to en_retard where overdue. Returns count."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return 0

    now = datetime.now(timezone.utc)
    count = 0

    for step in protocol.get("steps", []):
        deadline = step.get("deadline_date")
        if (
            deadline
            and deadline < now
            and step.get("status") not in ("complété",)
        ):
            if step.get("status") != "en_retard":
                try:
                    db.collection(COLLECTION).document(
                        protocol_id
                    ).collection(STEPS_SUBCOLLECTION).document(
                        step["id"]
                    ).update({"status": "en_retard", "updated_at": now})
                    count += 1
                except Exception:
                    pass
            else:
                count += 1

    return count


# ── Summary ─────────────────────────────────────────────────────────────


def get_protocol_summary(dossier_id: str) -> dict:
    """Return protocol summary for a dossier (active protocol only)."""
    protocol = get_protocol_for_dossier(dossier_id, active_only=True)
    if not protocol:
        all_protos = list_protocols_for_dossier(dossier_id)
        return {
            "has_protocol": False,
            "has_history": len(all_protos) > 0,
            "total": 0,
            "completed": 0,
            "overdue": 0,
            "upcoming": 0,
        }

    steps = protocol.get("steps", [])
    now = datetime.now(timezone.utc)
    completed = [s for s in steps if s.get("status") == "complété"]
    overdue = [
        s for s in steps
        if s.get("deadline_date")
        and s["deadline_date"] < now
        and s.get("status") not in ("complété",)
    ]
    upcoming = [
        s for s in steps
        if s.get("deadline_date")
        and s["deadline_date"] >= now
        and (s["deadline_date"] - now).days <= 7
        and s.get("status") not in ("complété",)
    ]

    all_protos = list_protocols_for_dossier(dossier_id)
    return {
        "has_protocol": True,
        "has_history": len(all_protos) > 1,
        "protocol_id": protocol["id"],
        "protocol_type": protocol.get("protocol_type", ""),
        "total": len(steps),
        "completed": len(completed),
        "overdue": len(overdue),
        "upcoming": len(upcoming),
    }


# ── Task sync helpers ───────────────────────────────────────────────────


def _auto_create_tasks_for_steps(
    protocol_id: str, protocol: dict, steps: list[dict]
) -> None:
    """Create linked tasks for each protocol step."""
    try:
        from dav.sync import bump_ctag
        from models.task import create_task, update_task

        dossier_id = protocol.get("dossier_id")

        for step in steps:
            task_data = {
                "title": step["title"],
                "description": (
                    f"Étape du protocole — {protocol.get('title', '')}"
                ),
                "dossier_id": dossier_id,
                "dossier_file_number": protocol.get("dossier_file_number", ""),
                "dossier_title": protocol.get("dossier_title", ""),
                "due_date": step.get("deadline_date"),
                "priority": "normale",
                "status": "à_faire",
                "category": "suivi",
            }
            task, errors = create_task(task_data)
            if task:
                # Bump per-dossier CTag so DavX5 picks up the new task
                if dossier_id:
                    bump_ctag(f"dossier:{dossier_id}")
                else:
                    bump_ctag("tasks")
                # Link task to step
                try:
                    db.collection(COLLECTION).document(
                        protocol_id
                    ).collection(STEPS_SUBCOLLECTION).document(
                        step["id"]
                    ).update({"linked_task_id": task["id"]})
                except Exception:
                    pass
    except Exception:
        pass


_SYNCING: set[str] = set()  # Circular sync guard


def _sync_task_status(task_id: str, step_status: str) -> None:
    """Sync task status when protocol step status changes."""
    if task_id in _SYNCING:
        return
    _SYNCING.add(task_id)
    try:
        from models.task import update_task

        if step_status == "complété":
            update_task(task_id, {"status": "terminée"})
        elif step_status in ("à_venir", "en_cours"):
            update_task(task_id, {"status": "à_faire"})
    except Exception:
        pass
    finally:
        _SYNCING.discard(task_id)


def _check_protocol_completion(protocol_id: str) -> None:
    """Auto-complete protocol if all steps are complete."""
    protocol = get_protocol(protocol_id)
    if not protocol:
        return

    steps = protocol.get("steps", [])
    if not steps:
        return

    all_complete = all(s.get("status") == "complété" for s in steps)
    if all_complete and protocol.get("status") == "actif":
        try:
            now = datetime.now(timezone.utc)
            db.collection(COLLECTION).document(protocol_id).update({
                "status": "complété",
                "updated_at": now,
                "etag": str(uuid.uuid4()),
            })
        except Exception:
            pass
